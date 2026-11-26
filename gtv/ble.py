"""Bluetooth LE: the trainer (any FTMS / Cycling Power / cadence device) and the Elite Sterzo steering block."""
import asyncio, struct, time

from bleak import BleakClient, BleakScanner

from . import parsers as P


class Sensors:
    def __init__(self, cfg, log):
        self.cfg, self.log = cfg, log
        self.power = self.cadence = self.steer = self.steer_raw = 0.0
        self.power_at = self.cadence_at = self.steer_at = 0.0
        self.trainer_state, self.steering_state, self.heart_state = "not chosen", "not chosen", "not chosen"
        self.hr, self.hr_at = 0, 0.0
        self.trainer_info = {}
        self._scan_lock = asyncio.Lock()        # BlueZ allows one scan at a time
        self._trainer_client = None
        self._control = None                    # "ftms" | "fec" | None
        self.has_power = False
        self._restart = asyncio.Event()

    # ---- scanning ---------------------------------------------------------------------------------------------
    async def scan(self, seconds=8.0):
        async with self._scan_lock:
            found = await BleakScanner.discover(timeout=seconds, return_adv=True)
        out = []
        for dev, adv in found.values():
            name = adv.local_name or dev.name or ""
            kind = P.classify(name, adv.service_uuids)
            if kind:
                out.append({"address": dev.address, "name": name or dev.address, "kind": kind, "rssi": adv.rssi})
        return sorted(out, key=lambda d: -d["rssi"])

    async def _find(self, address):
        async with self._scan_lock:
            dev = await BleakScanner.find_device_by_address(address, timeout=8.0)
        if not dev:
            # A sensor left connected by an earlier run of this service does not advertise, so it is never
            # found. We are not connected to it here, so any link BlueZ still holds is stale: drop it.
            try:
                p = await asyncio.create_subprocess_exec("bluetoothctl", "disconnect", address,
                                                         stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await asyncio.wait_for(p.wait(), 8)
            except Exception:  # noqa: BLE001
                pass
        return dev

    def reconnect(self):
        """Call after the chosen devices change."""
        self._restart.set()

    # ---- trainer ----------------------------------------------------------------------------------------------
    async def trainer_task(self):
        while True:
            chosen = self.cfg.get("trainer")
            if not chosen:
                self.trainer_state = "not chosen"
                await self._wait_restart(3)
                continue
            try:
                self.trainer_state = "searching"
                dev = await self._find(chosen["address"])
                if not dev:
                    await self._wait_restart(3)
                    continue
                self.trainer_state = "connecting"
                async with BleakClient(dev, timeout=20) as c:
                    self._trainer_client = c
                    await self._trainer_session(c, chosen)
            except Exception as e:  # noqa: BLE001 - any Bluetooth failure means reconnect
                self.log(f"trainer: {type(e).__name__}: {str(e)[:100]}")
            self._trainer_client, self._control, self.has_power = None, None, False
            self.power = self.cadence = 0.0
            self.trainer_state = "searching" if self.cfg.get("trainer") else "not chosen"
            await self._wait_restart(3)

    async def _trainer_session(self, c, chosen):
        chars = {ch.uuid.lower() for s in c.services for ch in s.characteristics}
        crank_cps, crank_csc = P.CrankCadence(1024), P.CrankCadence(1024)
        sources = []

        def on_cps(_, data):
            watts, rpm = P.parse_cycling_power(bytes(data), crank_cps)
            self.power, self.power_at = float(watts), time.monotonic()
            if rpm is not None:
                self.cadence, self.cadence_at = rpm, time.monotonic()

        def on_ftms(_, data):
            d = P.parse_indoor_bike(bytes(data))
            now = time.monotonic()
            if "power" in d and P.CPS_MEASUREMENT not in chars:
                self.power, self.power_at = float(d["power"]), now
            if "cadence" in d and now - self.cadence_at > 2:      # prefer crank-based cadence when it is flowing
                self.cadence, self.cadence_at = d["cadence"], now

        def on_csc(_, data):
            rpm = P.parse_csc(bytes(data), crank_csc)
            if rpm is not None and P.CPS_MEASUREMENT not in chars:   # the power service's crank data is preferred
                self.cadence, self.cadence_at = rpm, time.monotonic()

        for uuid, cb, label in ((P.CPS_MEASUREMENT, on_cps, "power"), (P.FTMS_INDOOR_BIKE, on_ftms, "FTMS"), (P.CSC_MEASUREMENT, on_csc, "cadence")):
            if uuid in chars:
                await c.start_notify(uuid, cb)
                sources.append(label)
        self.has_power = P.CPS_MEASUREMENT in chars or P.FTMS_INDOOR_BIKE in chars
        if not sources:
            raise RuntimeError("this device has no power, FTMS or cadence service")
        self._control = "ftms" if P.FTMS_CONTROL in chars else "fec" if P.FEC_WRITE in chars else None
        if self._control == "ftms":
            await c.start_notify(P.FTMS_CONTROL, lambda *_: None)
            await c.write_gatt_char(P.FTMS_CONTROL, b"\x00", response=True)   # request control
        self.trainer_info = {"name": chosen.get("name"), "data": sources, "control": self._control or "none"}
        self.trainer_state = "connected"
        self.log(f"trainer: connected to {chosen.get('name')} (data: {', '.join(sources)}; resistance control: {self._control or 'none'})")
        await self.set_grade(self.cfg.get("grade", 0.0))
        self._restart.clear()
        while c.is_connected and not self._restart.is_set():
            await asyncio.sleep(1)

    async def set_grade(self, grade):
        """Road gradient in percent on a controllable trainer. Returns False when the trainer cannot be controlled."""
        c = self._trainer_client
        if not c or not c.is_connected or not self._control:
            return False
        try:
            if self._control == "ftms":
                await c.write_gatt_char(P.FTMS_CONTROL, P.ftms_simulation(grade), response=True)
            else:
                await c.write_gatt_char(P.FEC_WRITE, P.fec_track_resistance(grade), response=False)
            return True
        except Exception as e:  # noqa: BLE001
            self.log(f"trainer: could not set gradient: {str(e)[:100]}")
            return False

    # ---- steering ---------------------------------------------------------------------------------------------
    async def steering_task(self):
        while True:
            chosen = self.cfg.get("steering")
            if not chosen:
                self.steering_state = "not chosen"
                await self._wait_restart(3)
                continue
            try:
                self.steering_state = "searching"
                dev = await self._find(chosen["address"])
                if not dev:
                    await self._wait_restart(3)
                    continue
                self.steering_state = "connecting"
                async with BleakClient(dev, timeout=20) as c:
                    challenge = asyncio.get_running_loop().create_future()
                    ready = [0.0]      # readings before the sensor is unlocked are meaningless (it sends -34, then not-a-number)

                    def on_tx(_, data):
                        data = bytes(data)
                        if len(data) >= 4 and data[:2] == b"\x03\x10" and not challenge.done():
                            challenge.set_result(data[2] | (data[3] << 8))

                    def on_angle(_, data):
                        if len(data) >= 4:
                            a = struct.unpack_from("<f", bytes(data), 0)[0]
                            if a != a or abs(a) > 90 or not ready[0] or time.monotonic() < ready[0]:
                                return
                            self.steer_raw = a
                            a -= float(self.cfg.get("steer_center") or 0)
                            self.steer = -a if self.cfg.get("steer_invert") else a
                            self.steer_at = time.monotonic()

                    await c.start_notify(P.STERZO_TX, on_tx)
                    await c.start_notify(P.STERZO_ANGLE, on_angle)
                    await c.write_gatt_char(P.STERZO_RX, b"\x03\x10", response=True)
                    try:
                        r = P.sterzo_response(await asyncio.wait_for(challenge, 4))
                        await c.write_gatt_char(P.STERZO_RX, bytes((0x03, 0x11, r & 0xFF, r >> 8)), response=True)
                        await asyncio.sleep(1)
                        await c.write_gatt_char(P.STERZO_RX, b"\x02\x02", response=True)
                    except asyncio.TimeoutError:
                        self.log("steering: no challenge received; newer firmware may not need one")
                    ready[0] = time.monotonic() + 1.5
                    await asyncio.sleep(1.6)
                    self.steering_state = "connected"
                    self.log(f"steering: connected to {chosen.get('name')}")
                    self._restart.clear()
                    while c.is_connected and not self._restart.is_set():
                        await asyncio.sleep(1)
                    if not c.is_connected:
                        self.log("steering: connection lost")
            except Exception as e:  # noqa: BLE001
                self.log(f"steering: {type(e).__name__}: {str(e)[:100]}")
            self.steer = 0.0
            self.steering_state = "searching" if self.cfg.get("steering") else "not chosen"
            await self._wait_restart(3)

    # ---- heart rate -------------------------------------------------------------------------------------------
    async def heart_task(self):
        while True:
            chosen = self.cfg.get("heart")
            if not chosen:
                self.heart_state = "not chosen"
                await asyncio.sleep(3)
                continue
            try:
                self.heart_state = "searching"
                dev = await self._find(chosen["address"])
                if not dev:
                    await asyncio.sleep(3)
                    continue
                self.heart_state = "connecting"
                async with BleakClient(dev, timeout=20) as c:
                    def on_hr(_, data):
                        bpm = P.parse_heart_rate(bytes(data))
                        if bpm:
                            self.hr, self.hr_at = bpm, time.monotonic()

                    await c.start_notify(P.HR_MEASUREMENT, on_hr)
                    self.heart_state = "connected"
                    self.log(f"heart rate: connected to {chosen.get('name')}")
                    while c.is_connected and self.cfg.get("heart") == chosen:
                        await asyncio.sleep(1)
            except Exception as e:  # noqa: BLE001
                self.log(f"heart rate: {type(e).__name__}: {str(e)[:100]}")
            self.heart_state = "searching" if self.cfg.get("heart") else "not chosen"
            await asyncio.sleep(3)

    def heart_rate(self):
        return self.hr if time.monotonic() - self.hr_at < 5 else 0

    async def _wait_restart(self, seconds):
        try:
            await asyncio.wait_for(self._restart.wait(), seconds)
        except asyncio.TimeoutError:
            pass
        self._restart.clear()

    # ---- what the bridge gets ---------------------------------------------------------------------------------
    def snapshot(self):
        """(watts, rpm, steering degrees) right now; stale readings count as zero so nothing is left held down."""
        now = time.monotonic()
        cadence = self.cadence if now - self.cadence_at < 3 else 0.0
        if self.has_power:
            power = self.power if now - self.power_at < 3 else 0.0
        else:                                   # cadence-only sensor: assume a power from the cadence
            power = cadence * float(self.cfg.get("cadence_watts", 2.0))
        steer = self.steer if now - self.steer_at < 3 else 0.0
        return power, cadence, steer
