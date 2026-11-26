"""Runs the native bridge, feeds it over UDP and keeps its latest status."""
import asyncio, json, os, socket, time

NATIVE = os.environ.get("GTV_NATIVE", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "native"))
PORT = int(os.environ.get("BRIDGE_PORT", "9750"))


class Bridge:
    def __init__(self, cfg, log):
        self.cfg, self.log = cfg, log
        self.status = {"session": "not running", "enabled": 0}
        self.events = []
        self.rumble = (0, 0, 0.0)
        self.enabled = False
        self._proc = None
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._respawn = asyncio.Event()

    def send(self, line):
        try:
            self._sock.sendto(line.encode(), ("127.0.0.1", PORT))
        except OSError:
            pass

    def push_config(self):
        m = dict(self.cfg["mapping"])
        if self.cfg.get("brake_gpio") not in (None, ""):      # a real brake switch replaces brake-on-stop
            m["stop_brake_s"] = 0
        self.send("cfg enabled=%d " % self.enabled + " ".join(f"{k}={m[k]}" for k in m))

    def set_enabled(self, on, connect_now=False):
        self.enabled = bool(on)
        self.push_config()
        self.send("ctl poke" if on and connect_now else "ctl stop" if not on else "ctl none")

    def restart(self):
        """Call after the console pairing changes: the keys are read when the bridge starts."""
        self._respawn.set()
        if self._proc and self._proc.returncode is None:
            self.send("ctl quit")

    async def task(self):
        exe = os.path.join(NATIVE, "bridge")
        while True:
            if not os.path.exists(exe):
                self.status = {"session": "bridge is not built", "enabled": 0}
                await asyncio.sleep(5)
                continue
            p5 = self.cfg["ps5"]
            env = dict(os.environ, BRIDGE_PORT=str(PORT), RP_HOST=p5.get("host") or "", RP_REGIST_KEY_HEX=p5.get("regist_key_hex") or "",
                       RP_MORNING_HEX=p5.get("morning_hex") or "", RP_ACCOUNT_HEX=p5.get("account_hex") or "")
            self._respawn.clear()
            self._proc = await asyncio.create_subprocess_exec(exe, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            await asyncio.sleep(0.3)
            self.push_config()
            async for raw in self._proc.stdout:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if msg.get("status"):
                    self.status = msg
                else:
                    ev = msg.get("event")
                    if ev == "rumble":
                        self.rumble = (msg["left"], msg["right"], time.monotonic())
                    elif ev in ("connected", "ended", "disconnected", "connecting", "wake", "login_pin_needed"):
                        self.log("ps5: " + ev + (": " + str(msg.get("reason")) if msg.get("reason") else ""))
                    self.events = (self.events + [dict(msg, at=time.time())])[-40:]
            await self._proc.wait()
            self.status = {"session": "not running", "enabled": 0}
            await asyncio.sleep(1)
