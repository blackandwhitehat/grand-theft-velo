"""A gamepad on the Pi (Bluetooth or USB), read through Linux evdev and passed to the bridge.

Works with a DualSense or DualShock 4 (kernel driver hid-playstation / hid-sony), Xbox pads and most others.
Axis ranges are read from the device, so they do not need to be known in advance.
"""
import asyncio, time

try:
    import evdev
    from evdev import ecodes as E
except ImportError:  # the web page says so instead of the service failing to start
    evdev = None

# chiaki button bits
CROSS, MOON, BOX, PYRAMID, LEFT, RIGHT, UP, DOWN, L1, R1, L3, R3, OPTIONS, SHARE, TOUCHPAD, PS = (1 << i for i in range(16))


class Yaw:
    """Handlebar angle from a controller strapped to the bars.

    The gyro's turn rate is measured about the direction of gravity (taken from the accelerometer), so it does not
    matter which way up the controller is mounted. A gyro drifts, so: its resting offset is learned whenever the
    controller is still, and the angle is pulled slowly back to straight (you ride straight most of the time).
    """
    RETURN_S = 40.0          # time constant of the pull back to straight
    STILL_DPS = 1.5          # below this much wobble the controller counts as still

    def __init__(self):
        self.angle, self.at = 0.0, 0.0
        self.bias = None
        self._g = None
        self._t = None
        self._still = []

    def center(self):
        self.angle = 0.0

    def update(self, acc, gyro, t):
        dt = 0.0 if self._t is None else t - self._t
        self._t = t
        if dt <= 0 or dt > 0.25:                       # first sample, or a gap: do not integrate across it
            dt = 0.0
        g = self._g = list(acc) if self._g is None else [0.98 * a + 0.02 * b for a, b in zip(self._g, acc)]
        # learn the resting offset from any second in which the gyro barely changes
        self._still.append((t, tuple(gyro)))
        while self._still and t - self._still[0][0] > 1.0:
            self._still.pop(0)
        if len(self._still) >= 20 and t - self._still[0][0] > 0.9:
            cols = list(zip(*[s[1] for s in self._still]))
            mean = [sum(c) / len(c) for c in cols]
            ref = self.bias or (0.0, 0.0, 0.0)
            # steady, and close to the offset already known: a constant turn is steady too, but nowhere near it
            if all(max(c) - min(c) < self.STILL_DPS for c in cols) and all(abs(m - r) < 3.0 for m, r in zip(mean, ref)):
                self.bias = mean
        if self.bias is None:
            return self.angle                         # not calibrated yet: hold the controller still for a second
        norm = (g[0] ** 2 + g[1] ** 2 + g[2] ** 2) ** 0.5 or 1.0
        rate = sum((w - b) * a for w, b, a in zip(gyro, self.bias, g)) / norm
        if abs(rate) < 0.15:
            rate = 0.0
        self.angle = max(-70.0, min(70.0, (self.angle + rate * dt) * (1.0 - dt / self.RETURN_S)))
        self.at = t
        return self.angle


class Pad:
    """One connected controller."""

    def __init__(self, log, dev):
        self.log, self.dev = log, dev
        self.name, self.state = dev.name, "connected"
        self.address = (dev.uniq or dev.path).upper()        # Bluetooth address, or the device path for USB
        self.axes = {"lx": 0, "ly": 0, "rx": 0, "ry": 0, "l2": 0, "r2": 0}
        self.buttons = 0
        self.touch_click = 0
        self.at = 0.0
        self.motion = Yaw()                  # steering from the controller's own motion sensors
        self.has_motion = False
        self.want_motion = False             # the sensors report 250 times a second, so only read them when asked

    @staticmethod
    def _is_gamepad(dev):
        caps = dev.capabilities()
        keys, absx = caps.get(E.EV_KEY, []), [a[0] for a in caps.get(E.EV_ABS, [])]
        name = dev.name.lower()
        if "motion sensor" in name or "touchpad" in name:
            return False
        return E.BTN_SOUTH in keys and E.ABS_X in absx and E.ABS_Y in absx

    def _find(self):
        for path in evdev.list_devices():
            try:
                dev = evdev.InputDevice(path)
                if self._is_gamepad(dev):
                    return dev
                dev.close()
            except OSError:
                continue
        return None

    @staticmethod
    def _find_touchpad(pad):
        """PlayStation controllers show up as several devices; the touchpad click (BTN_LEFT) is on its own one."""
        for path in evdev.list_devices():
            try:
                dev = evdev.InputDevice(path)
                if "touchpad" in dev.name.lower() and dev.uniq == pad.uniq and dev.phys == pad.phys:
                    return dev
                dev.close()
            except OSError:
                continue
        return None

    @staticmethod
    def _find_sibling(pad, word):
        for path in evdev.list_devices():
            try:
                dev = evdev.InputDevice(path)
                if word in dev.name.lower() and dev.uniq == pad.uniq and dev.phys == pad.phys:
                    return dev
                dev.close()
            except OSError:
                continue
        return None

    async def _motion_supervisor(self, pad):
        """Read the motion sensors only while steering by controller is switched on."""
        task = None
        try:
            while True:
                if self.want_motion and (task is None or task.done()):
                    dev = self._find_sibling(pad, "motion")
                    task = asyncio.ensure_future(self._motion_task(dev)) if dev else None
                elif not self.want_motion and task is not None:
                    task.cancel(); task = None; self.has_motion = False
                await asyncio.sleep(1)
        finally:
            if task is not None:
                task.cancel()
            self.has_motion = False

    async def _motion_task(self, dev):
        """Feed the accelerometer and gyro (PlayStation controllers expose them as a third device) to Yaw."""
        res = {code: (ai.resolution or 1) for code, ai in dev.capabilities().get(E.EV_ABS, [])}
        acc, gyro = [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]
        self.has_motion = True
        self.motion = Yaw()
        try:
            async for ev in dev.async_read_loop():
                if ev.type == E.EV_ABS:
                    if E.ABS_X <= ev.code <= E.ABS_Z:
                        acc[ev.code - E.ABS_X] = ev.value / res[ev.code]            # g
                    elif E.ABS_RX <= ev.code <= E.ABS_RZ:
                        gyro[ev.code - E.ABS_RX] = ev.value / res[ev.code]          # degrees per second
                elif ev.type == E.EV_SYN and ev.code == E.SYN_REPORT:
                    self.motion.update(acc, gyro, ev.timestamp())
        except OSError:
            pass
        self.has_motion = False

    async def _touch_task(self, dev):
        try:
            async for ev in dev.async_read_loop():
                if ev.type == E.EV_KEY and ev.code == E.BTN_LEFT:
                    self.touch_click = 1 if ev.value else 0
                    self.at = time.monotonic()
        except OSError:
            pass
        self.touch_click = 0

    async def run(self):
        """Read this controller until it disconnects."""
        dev = self.dev
        if True:
            self.log(f"gamepad: {dev.name} connected")
            keymap = {E.BTN_SOUTH: CROSS, E.BTN_EAST: MOON, E.BTN_WEST: BOX, E.BTN_NORTH: PYRAMID, E.BTN_TL: L1, E.BTN_TR: R1,
                      E.BTN_THUMBL: L3, E.BTN_THUMBR: R3, E.BTN_START: OPTIONS, E.BTN_SELECT: SHARE, E.BTN_MODE: PS}
            name = dev.name.lower()
            if not any(k in name for k in ("dualsense", "wireless controller", "playstation", "sony")):
                # Xbox-style pads report X and Y swapped relative to their position; map by position instead
                keymap[E.BTN_WEST], keymap[E.BTN_NORTH] = PYRAMID, BOX
            info = {code: ai for code, ai in dev.capabilities().get(E.EV_ABS, [])}

            def stick(code, value):
                ai = info[code]; mid = (ai.max + ai.min) / 2; half = (ai.max - ai.min) / 2 or 1
                v = (value - mid) / half
                return 0 if abs(v) < 0.06 else int(max(-1, min(1, v)) * 32767)

            def trigger(code, value):
                ai = info[code]; span = (ai.max - ai.min) or 1
                return int(max(0, min(1, (value - ai.min) / span)) * 255)

            hat = {"x": 0, "y": 0}
            tp = self._find_touchpad(dev)
            tp_task = asyncio.ensure_future(self._touch_task(tp)) if tp else None
            mo_task = asyncio.ensure_future(self._motion_supervisor(dev))
            try:
                async for ev in dev.async_read_loop():
                    if ev.type == E.EV_KEY and ev.code in keymap:
                        self.buttons = self.buttons | keymap[ev.code] if ev.value else self.buttons & ~keymap[ev.code]
                    elif ev.type == E.EV_ABS:
                        c = ev.code
                        if c == E.ABS_X: self.axes["lx"] = stick(c, ev.value)
                        elif c == E.ABS_Y: self.axes["ly"] = stick(c, ev.value)
                        elif c == E.ABS_RX: self.axes["rx"] = stick(c, ev.value)
                        elif c == E.ABS_RY: self.axes["ry"] = stick(c, ev.value)
                        elif c == E.ABS_Z: self.axes["l2"] = trigger(c, ev.value)
                        elif c == E.ABS_RZ: self.axes["r2"] = trigger(c, ev.value)
                        elif c in (E.ABS_HAT0X, E.ABS_HAT0Y):
                            hat["x" if c == E.ABS_HAT0X else "y"] = ev.value
                            self.buttons &= ~(LEFT | RIGHT | UP | DOWN)
                            self.buttons |= (LEFT if hat["x"] < 0 else RIGHT if hat["x"] > 0 else 0) | (UP if hat["y"] < 0 else DOWN if hat["y"] > 0 else 0)
                    self.at = time.monotonic()
            except OSError:
                pass
            if tp_task:
                tp_task.cancel()
            if mo_task:
                mo_task.cancel()
            self.has_motion = False
            self.touch_click = 0
            self.log(f"gamepad: {self.name} disconnected")
            self.buttons, self.axes = 0, dict.fromkeys(self.axes, 0)
            self.state = "none"


class Gamepad:
    """Every connected controller, merged into one.

    Two controllers can be used at once: for example one strapped to the handlebars as the steering sensor and one
    in your hands. Buttons from all of them count, and each stick or trigger takes whichever is pushed furthest.
    """

    def __init__(self, log):
        self.log = log
        self.pads = {}                       # device path -> (Pad, task)
        self.want_motion = False
        self.steer_address = None            # which controller is on the handlebars, when there are several
        self._idle = Yaw()

    @property
    def state(self):
        return "evdev not installed" if not evdev else "connected" if self.pads else "none"

    @property
    def name(self):
        return " + ".join(p.name for p, _ in self.pads.values()) or None

    def _steer_pad(self):
        pads = [p for p, _ in self.pads.values()]
        for p in pads:
            if self.steer_address and p.address == self.steer_address.upper():
                return p
        return pads[0] if pads else None

    @property
    def motion(self):
        p = self._steer_pad()
        return p.motion if p else self._idle

    @property
    def has_motion(self):
        p = self._steer_pad()
        return bool(p and p.has_motion)

    def list(self):
        sp = self._steer_pad()
        return [{"address": p.address, "name": p.name, "on_bars": p is sp} for p, _ in self.pads.values()]

    async def task(self):
        if not evdev:
            return
        while True:
            for path in [k for k, (_, t) in self.pads.items() if t.done()]:
                del self.pads[path]
            for path in evdev.list_devices():
                if path in self.pads:
                    continue
                try:
                    dev = evdev.InputDevice(path)
                    if Pad._is_gamepad(dev):
                        pad = Pad(self.log, dev)
                        self.pads[path] = (pad, asyncio.ensure_future(pad.run()))
                    else:
                        dev.close()
                except OSError:
                    continue
            sp = self._steer_pad()
            for p, _ in self.pads.values():
                p.want_motion = self.want_motion and p is sp
            await asyncio.sleep(1)

    def line(self):
        """The bridge's 'pad' line, or None when no gamepad is connected."""
        pads = [p for p, _ in self.pads.values() if p.state == "connected"]
        if not pads:
            return None
        a = {k: max((p.axes[k] for p in pads), key=abs) for k in ("lx", "ly", "rx", "ry", "l2", "r2")}
        buttons = 0
        for p in pads:
            buttons |= p.buttons | (TOUCHPAD if p.touch_click else 0)
        return f"pad lx={a['lx']} ly={a['ly']} rx={a['rx']} ry={a['ry']} l2={a['l2']} r2={a['r2']} buttons={buttons}"


async def bluetoothctl(*args, timeout=20):
    p = await asyncio.create_subprocess_exec("bluetoothctl", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError:
        p.kill(); return ""
    return out.decode(errors="replace")


async def scan_pads(seconds=10):
    """Classic-Bluetooth scan for controllers in pairing mode. Returns [{address, name}]."""
    await bluetoothctl("--timeout", str(seconds), "scan", "on", timeout=seconds + 5)
    out, pads = await bluetoothctl("devices"), []
    for ln in out.splitlines():
        parts = ln.split(" ", 2)
        if len(parts) == 3 and parts[0] == "Device" and any(k in parts[2].lower() for k in ("controller", "dualsense", "dualshock", "xbox", "gamepad", "8bitdo", "pro controller")):
            pads.append({"address": parts[1], "name": parts[2]})
    return pads


async def pair_pad(address):
    """Pair, trust and connect in ONE bluetoothctl session with an agent registered.

    Separate one-shot commands have no agent, so the controller ends up "paired" but not bonded and
    BlueZ then refuses its input connection: no /dev/input device ever appears.
    """
    p = await asyncio.create_subprocess_exec("bluetoothctl", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    seen = ""

    async def say(line, wait_for=(), timeout=3.0):
        nonlocal seen
        start = len(seen)
        p.stdin.write((line + "\n").encode()); await p.stdin.drain()
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                chunk = await asyncio.wait_for(p.stdout.read(4096), 0.5)
            except asyncio.TimeoutError:
                chunk = b""
            seen += chunk.decode(errors="replace")
            if any(w in seen[start:].lower() for w in wait_for):
                break
        return seen[start:]

    def verdict(out):
        low = out.lower()
        lines = [ln.strip() for ln in out.splitlines() if any(w in ln.lower() for w in ("success", "succeeded", "failed", "already", "not available"))]
        return ("successful" in low or "succeeded" in low or "alreadyexists" in low or "already" in low) and "failed" not in low, (lines[-1][-120:] if lines else "no answer")

    steps = []
    try:
        await say("agent NoInputNoOutput", ("registered",)); await say("default-agent", ("successful",))
        await say("remove " + address, ("removed", "not available"), 4)        # clear a half-made pairing; a pad in pairing mode is found again
        await say("scan on", ("discovery started",), 3)
        await say("", ("] device " + address.lower(),), 12)
        for cmd, waits, t in (("pair", ("pairing successful", "failed to pair", "not available"), 25), ("trust", ("succeeded", "failed", "not available"), 5),
                              ("connect", ("connection successful", "failed to connect", "not available"), 20)):
            ok, detail = verdict(await say(f"{cmd} {address}", waits, t))
            steps.append((cmd, ok, detail))
        await say("scan off", (), 1)
        info = await say("info " + address, ("bonded",), 3)
        bonded = "bonded: yes" in info.lower()
        steps.append(("bond", bonded, "bonded" if bonded else "not bonded: put the controller back in pairing mode and try again"))
    finally:
        try:
            p.stdin.write(b"quit\n"); await p.stdin.drain()
            await asyncio.wait_for(p.wait(), 3)
        except Exception:  # noqa: BLE001
            p.kill()
    return {"ok": steps[-1][1], "steps": [{"step": s, "ok": ok, "detail": d} for s, ok, d in steps]}
