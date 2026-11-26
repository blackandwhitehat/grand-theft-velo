"""The gtv service: web page, sensors, gamepad and the bridge, in one process."""
import asyncio, base64, hmac, os, time

from aiohttp import web

from . import __version__, config, gamepad, ps5, rides
from .ble import Sensors
from .bridge import Bridge

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


class App:
    def __init__(self):
        self.cfg = config.load()
        self.lines = []
        self.sensors = Sensors(self.cfg, self.log)
        self.pad = gamepad.Gamepad(self.log)
        self.bridge = Bridge(self.cfg, self.log)
        self.brake = 0
        self.brake_state = "not set"
        self._brake_obj = None
        self.ps5_state = "unknown"
        self.starting = None
        self.rec = rides.Recorder(self.log)

    def log(self, text):
        self.lines = (self.lines + [time.strftime("%H:%M:%S ") + text])[-200:]
        print(text, flush=True)

    # ---- background work --------------------------------------------------------------------------------------
    def setup_brake(self):
        if self._brake_obj:
            self._brake_obj.close()
            self._brake_obj = None
        self.brake, pin = 0, self.cfg.get("brake_gpio")
        if pin in (None, ""):
            self.brake_state = "not set"
            return
        try:
            from gpiozero import Button
            b = Button(int(pin), pull_up=True, bounce_time=0.02)      # a switch between the pin and ground
            b.when_pressed = lambda: setattr(self, "brake", 1)
            b.when_released = lambda: setattr(self, "brake", 0)
            self._brake_obj, self.brake_state = b, f"watching GPIO{pin}"
        except Exception as e:  # noqa: BLE001
            self.brake_state = f"unavailable: {str(e)[:80]}"

    async def feed_task(self):
        """Twenty times a second: tell the bridge what the rider and the gamepad are doing."""
        while True:
            power, cadence, _ = self.sensors.snapshot()
            self.pad.steer_address = self.cfg.get("pad_steer_address")
            self.pad.want_motion = bool(self.cfg.get("pad_steer")) and self.sensors.steering_state != "connected"
            self.bridge.send("bike power=%.0f cadence=%.0f steer=%.1f brake=%d" % (power, cadence, self.steer(), self.brake))
            line = self.pad.line()
            if line:
                self.bridge.send(line)
            await asyncio.sleep(0.05)

    def steer_source(self):
        """'sensor' (Sterzo), 'gamepad' (controller on the bars) or None."""
        if self.sensors.steering_state == "connected":
            return "sensor"
        if self.cfg.get("pad_steer") and self.pad.has_motion:
            return "gamepad"
        return None

    def steer(self):
        src = self.steer_source()
        if src == "sensor":
            return self.sensors.snapshot()[2]
        if src == "gamepad":
            a = self.pad.motion.angle
            return -a if self.cfg.get("steer_invert") else a
        return 0.0

    async def record_task(self):
        """Once a second: log the ride while ride mode is on."""
        while True:
            await asyncio.sleep(1)
            if self.bridge.enabled and not self.rec.on:
                self.rec.start()
            elif not self.bridge.enabled and self.rec.on:
                self.rec.stop()
            if self.rec.on:
                power, cadence, _ = self.sensors.snapshot()
                self.rec.add(power, cadence, self.sensors.heart_rate())

    async def ps5_task(self):
        while True:
            try:
                self.ps5_state = await ps5.state(self.cfg["ps5"].get("host"))
            except Exception:  # noqa: BLE001
                self.ps5_state = "unknown"
            await asyncio.sleep(5)

    async def start_ride(self):
        p5 = self.cfg["ps5"]
        if not (p5.get("host") and p5.get("regist_key_hex")):
            return {"ok": False, "error": "pair with the PS5 first"}
        self.ps5_state = await ps5.state(p5["host"])
        if self.ps5_state == "standby":
            self.log("ps5: asleep, sending wake-up")
            self.bridge.send("ctl wake")
            for _ in range(40):
                await asyncio.sleep(1.5)
                self.ps5_state = await ps5.state(p5["host"])
                if self.ps5_state == "awake":
                    await asyncio.sleep(6)          # the console answers before Remote Play is ready
                    break
            else:
                return {"ok": False, "error": "the PS5 did not wake up; turn it on and try again"}
        elif self.ps5_state != "awake":
            return {"ok": False, "error": "the PS5 is not answering at " + p5["host"]}
        self.bridge.set_enabled(True, connect_now=True)
        self.log("ride: started")
        return {"ok": True}

    def stop_ride(self):
        self.bridge.set_enabled(False)
        self.log("ride: stopped")

    # ---- web --------------------------------------------------------------------------------------------------
    def status(self):
        power, cadence, steer = self.sensors.snapshot()
        b = self.bridge.status
        rumble = self.bridge.rumble
        return {
            "version": __version__, "ride": self.bridge.enabled, "starting": bool(self.starting and not self.starting.done()),
            "trainer": {"state": self.sensors.trainer_state, "info": self.sensors.trainer_info, "power": round(power), "cadence": round(cadence)},
            "heart": {"state": self.sensors.heart_state, "bpm": self.sensors.heart_rate()},
            "recording": {"on": self.rec.on, "id": self.rec.id if self.rec.on else None, "moving_s": self.rec.samples, "distance_m": round(self.rec.distance)},
            "steering": {"state": self.sensors.steering_state, "angle": round(self.steer(), 1), "source": self.steer_source(),
                         "pad_motion": self.pad.has_motion, "pad_calibrated": self.pad.motion.bias is not None, "pad_angle": round(self.pad.motion.angle, 1)},
            "gamepad": {"state": self.pad.state, "name": self.pad.name, "pads": self.pad.list()},
            "brake": {"state": self.brake_state, "pressed": self.brake},
            "ps5": {"state": self.ps5_state, "session": b.get("session"), "fps": b.get("fps", 0), "last_end": b.get("last_end", "")},
            "output": {"r2": b.get("r2", 0), "l2": b.get("l2", 0), "lx": b.get("lx", 0), "buttons": b.get("buttons", 0)},
            "rumble": {"left": rumble[0], "right": rumble[1]} if time.monotonic() - rumble[2] < 1.5 else {"left": 0, "right": 0},
            "config": config.public(self.cfg), "log": self.lines[-25:],
        }

    def save(self):
        config.save(self.cfg)

    def routes(self):
        r = web.RouteTableDef()

        @r.get("/")
        async def index(_):
            return web.FileResponse(os.path.join(WEB, "index.html"), headers={"Cache-Control": "no-store"})

        @r.get("/api/status")
        async def status(_):
            return web.json_response(self.status())

        @r.post("/api/scan")
        async def scan(_):
            return web.json_response({"devices": await self.sensors.scan(8.0)})

        @r.post("/api/devices")
        async def devices(req):
            d = await req.json()
            for key in ("trainer", "steering", "heart"):
                if key in d:
                    v = d[key]
                    self.cfg[key] = {"address": v["address"], "name": v.get("name", v["address"])} if v else None
            if "brake_gpio" in d:
                self.cfg["brake_gpio"] = int(d["brake_gpio"]) if str(d["brake_gpio"]).strip().isdigit() else None
                self.setup_brake()
                self.bridge.push_config()
            if "pad_steer_address" in d:
                self.cfg["pad_steer_address"] = d["pad_steer_address"] or None
            if "pad_steer" in d:
                self.cfg["pad_steer"] = bool(d["pad_steer"])
            if "steer_invert" in d:
                self.cfg["steer_invert"] = bool(d["steer_invert"])
            if "cadence_watts" in d:
                self.cfg["cadence_watts"] = max(0.5, min(6.0, float(d["cadence_watts"])))
            self.save()
            if set(d) - {"heart", "pad_steer", "pad_steer_address", "steer_invert"}:          # choosing a heart rate strap must not drop the trainer
                self.sensors.reconnect()
            return web.json_response({"ok": True})

        @r.get("/api/rides")
        async def rides_list(_):
            return web.json_response({"rides": [x for x in rides.list_rides() if x["id"] != (self.rec.id if self.rec.on else None)]})

        @r.get("/api/rides/{id}.tcx")
        async def rides_tcx(req):
            try:
                body = rides.tcx(req.match_info["id"])
            except (OSError, ValueError):
                return web.Response(status=404, text="no such ride")
            name = "grand-theft-velo-" + req.match_info["id"] + ".tcx"
            return web.Response(text=body, content_type="application/vnd.garmin.tcx+xml", headers={"Content-Disposition": f'attachment; filename="{name}"'})

        @r.post("/api/rides/{id}/delete")
        async def rides_delete(req):
            try:
                rides.delete(req.match_info["id"])
            except (OSError, ValueError):
                return web.json_response({"ok": False, "error": "no such ride"})
            return web.json_response({"ok": True})

        @r.post("/api/steer/center")
        async def steer_center(_):
            if self.steer_source() == "gamepad":
                self.pad.motion.center(); self.log("steering: controller centered")
                return web.json_response({"ok": True, "center": 0})
            if self.sensors.steering_state != "connected":
                return web.json_response({"ok": False, "error": "the steering sensor is not connected"})
            self.cfg["steer_center"] = round(self.sensors.steer_raw, 1); self.save()
            self.log("steering: center set to %.1f degrees" % self.cfg["steer_center"])
            return web.json_response({"ok": True, "center": self.cfg["steer_center"]})

        @r.post("/api/mapping")
        async def mapping(req):
            d = await req.json()
            limits = {"full_watts": (60, 1500), "min_watts": (0, 300), "sprint_watts": (0, 2500), "sprint_hz": (1, 12),
                      "steer_full": (5, 60), "steer_deadzone": (0, 10), "idle_s": (20, 3600), "stop_brake_s": (0, 6)}
            for k, (lo, hi) in limits.items():
                if k in d:
                    self.cfg["mapping"][k] = max(lo, min(hi, float(d[k])))
            self.save(); self.bridge.push_config()
            return web.json_response({"ok": True, "mapping": self.cfg["mapping"]})

        @r.post("/api/grade")
        async def grade(req):
            g = max(-10.0, min(20.0, float((await req.json()).get("grade", 0))))
            self.cfg["grade"] = g; self.save()
            return web.json_response({"ok": True, "applied": await self.sensors.set_grade(g)})

        @r.post("/api/ride")
        async def ride(req):
            d = await req.json()
            if "autostart" in d:
                self.cfg["autostart"] = bool(d["autostart"]); self.save()
            if d.get("on") is True:
                if not (self.starting and not self.starting.done()):
                    self.starting = asyncio.ensure_future(self.start_ride())
                return web.json_response(await self.starting)
            if d.get("on") is False:
                self.stop_ride()
            return web.json_response({"ok": True})

        @r.get("/api/ps5/discover")
        async def discover(_):
            return web.json_response({"consoles": await ps5.discover(), "login_url": ps5.LOGIN_URL})

        @r.post("/api/ps5/host")
        async def host(req):
            d = await req.json()
            self.cfg["ps5"]["host"], self.cfg["ps5"]["name"] = d["host"].strip(), d.get("name") or "PS5"
            self.save(); self.bridge.restart()
            return web.json_response({"ok": True})

        @r.post("/api/ps5/account")
        async def account(req):
            d = await req.json()
            try:
                self.cfg["ps5"]["account_hex"] = (await ps5.account_from_redirect(d["redirect"]) if d.get("redirect") else ps5.account_from_base64(d["base64"]))
            except Exception as e:  # noqa: BLE001
                return web.json_response({"ok": False, "error": str(e)[:200]})
            self.save()
            return web.json_response({"ok": True})

        @r.post("/api/ps5/register")
        async def register(req):
            p5 = self.cfg["ps5"]
            if not p5.get("host") or not p5.get("account_hex"):
                return web.json_response({"ok": False, "error": "choose the console and sign in first"})
            res = await ps5.register(p5["host"], p5["account_hex"], (await req.json()).get("pin", ""))
            if res.get("ok"):
                p5["regist_key_hex"], p5["morning_hex"] = res["regist_key_hex"], res["morning_hex"]
                p5["name"] = res.get("nickname") or p5.get("name")
                self.save(); self.bridge.restart(); self.log("ps5: paired with " + str(p5["name"]))
                return web.json_response({"ok": True, "name": p5["name"]})
            return web.json_response(res)

        @r.post("/api/ps5/forget")
        async def forget(_):
            self.stop_ride()
            self.cfg["ps5"].update(regist_key_hex=None, morning_hex=None)
            self.save(); self.bridge.restart()
            return web.json_response({"ok": True})

        @r.post("/api/pad/scan")
        async def pad_scan(_):
            return web.json_response({"pads": await gamepad.scan_pads(10)})

        @r.post("/api/pad/pair")
        async def pad_pair(req):
            return web.json_response(await gamepad.pair_pad((await req.json())["address"]))

        return r

    async def run(self):
        self.setup_brake()
        password = os.environ.get("GTV_PASSWORD")

        @web.middleware
        async def auth(request, handler):
            if password:
                try:
                    user_pw = base64.b64decode(request.headers.get("Authorization", "")[6:]).decode().split(":", 1)[1]
                except Exception:  # noqa: BLE001
                    user_pw = ""
                if not hmac.compare_digest(user_pw, password):
                    return web.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="Grand Theft Velo"'})
            return await handler(request)

        app = web.Application(middlewares=[auth])
        app.add_routes(self.routes())
        runner = web.AppRunner(app)
        await runner.setup()
        port = int(os.environ.get("GTV_PORT", "8080"))
        await web.TCPSite(runner, os.environ.get("GTV_BIND", "0.0.0.0"), port).start()
        self.log(f"Grand Theft Velo {__version__}: open http://<this device>:{port}/")
        tasks = [self.sensors.trainer_task(), self.sensors.steering_task(), self.pad.task(), self.bridge.task(), self.feed_task(), self.ps5_task(), self.sensors.heart_task(), self.record_task()]
        if self.cfg.get("autostart"):
            async def auto():
                await asyncio.sleep(8)
                self.log("ride: autostart: " + str(await self.start_ride()))
            tasks.append(auto())
        await asyncio.gather(*tasks)


def main():
    try:
        asyncio.run(App().run())
    except KeyboardInterrupt:
        pass
