"""Pure functions for the Bluetooth cycling profiles. No I/O, so they are unit tested on any machine."""
import struct

UUID = lambda short: f"0000{short:04x}-0000-1000-8000-00805f9b34fb"  # noqa: E731
FTMS_SERVICE, FTMS_INDOOR_BIKE, FTMS_CONTROL = UUID(0x1826), UUID(0x2AD2), UUID(0x2AD9)
CPS_SERVICE, CPS_MEASUREMENT = UUID(0x1818), UUID(0x2A63)
CSC_SERVICE, CSC_MEASUREMENT = UUID(0x1816), UUID(0x2A5B)
FEC_SERVICE = "6e40fec1-b5a3-f393-e0a9-e50e24dcca9e"
FEC_NOTIFY = "6e40fec2-b5a3-f393-e0a9-e50e24dcca9e"
FEC_WRITE = "6e40fec3-b5a3-f393-e0a9-e50e24dcca9e"
HR_SERVICE = "0000180d-0000-1000-8000-00805f9b34fb"
HR_MEASUREMENT = "00002a37-0000-1000-8000-00805f9b34fb"
STERZO_SERVICE = "347b0001-7635-408b-8918-8ff3949ce592"
STERZO_ANGLE = "347b0030-7635-408b-8918-8ff3949ce592"
STERZO_RX = "347b0031-7635-408b-8918-8ff3949ce592"
STERZO_TX = "347b0032-7635-408b-8918-8ff3949ce592"


class CrankCadence:
    """Cadence from cumulative crank revolutions and the last crank event time (both 16-bit, wrapping)."""

    def __init__(self, ticks_per_second=1024):
        self.ticks = ticks_per_second
        self.revs = self.t = None
        self.rpm = 0.0
        self.stale = 0

    def update(self, revs, t):
        if self.revs is not None:
            d_revs, d_time = (revs - self.revs) & 0xFFFF, (t - self.t) & 0xFFFF
            if d_time:
                rpm = d_revs * 60 * self.ticks / d_time
                if rpm < 250:          # a wrapped or duplicated packet can give nonsense
                    self.rpm = rpm
                self.stale = 0
            else:
                self.stale += 1
                if self.stale > 3:     # no new crank event for several packets: the rider stopped
                    self.rpm = 0.0
        self.revs, self.t = revs, t
        return self.rpm


def parse_cycling_power(data, crank=None):
    """Cycling Power Measurement 0x2A63 -> (watts, rpm or None)."""
    flags, power = struct.unpack_from("<Hh", data, 0)
    off = 4
    if flags & 0x01: off += 1      # pedal power balance
    if flags & 0x04: off += 2      # accumulated torque
    if flags & 0x10: off += 6      # wheel revolutions + event time
    rpm = None
    if flags & 0x20 and len(data) >= off + 4 and crank is not None:
        rpm = crank.update(*struct.unpack_from("<HH", data, off))
    return max(0, power), rpm


def parse_indoor_bike(data):
    """FTMS Indoor Bike Data 0x2AD2 -> dict with any of speed_kmh, cadence, power."""
    flags = struct.unpack_from("<H", data, 0)[0]
    off, out = 2, {}
    if not flags & 0x01:
        out["speed_kmh"] = struct.unpack_from("<H", data, off)[0] / 100; off += 2
    if flags & 0x02: off += 2                                  # average speed
    if flags & 0x04:
        out["cadence"] = struct.unpack_from("<H", data, off)[0] / 2; off += 2
    if flags & 0x08: off += 2                                  # average cadence
    if flags & 0x10: off += 3                                  # total distance
    if flags & 0x20: off += 2                                  # resistance level
    if flags & 0x40 and len(data) >= off + 2:
        out["power"] = max(0, struct.unpack_from("<h", data, off)[0]); off += 2
    return out


def parse_csc(data, crank):
    """Cycling Speed and Cadence 0x2A5B -> rpm or None (crank data only)."""
    flags = data[0]
    off = 1 + (6 if flags & 0x01 else 0)
    if flags & 0x02 and len(data) >= off + 4:
        return crank.update(*struct.unpack_from("<HH", data, off))
    return None


def parse_heart_rate(data):
    """Heart Rate Measurement (0x2A37): beats per minute, 8 or 16 bit depending on the first flag."""
    if len(data) < 2:
        return None
    return struct.unpack_from("<H", data, 1)[0] if data[0] & 1 and len(data) >= 3 else data[1]


def sterzo_response(challenge):
    """Answer to the Elite Sterzo's 16-bit challenge (challenge and answer are little-endian on the wire)."""
    n = challenge % 11
    rotated = ((challenge << n) | (challenge >> (16 - n))) & 0xFFFF
    return (((challenge + 38550) & 0xFFFF) ^ rotated) % 65336


def ftms_simulation(grade_percent, wind_ms=0.0, crr=0.004, cw=0.51):
    """FTMS Set Indoor Bike Simulation Parameters (op 0x11)."""
    return b"\x11" + struct.pack("<hhBB", int(wind_ms * 1000), int(round(grade_percent * 100)), int(round(crr / 0.0001)), int(round(cw / 0.01)))


def fec_frame(page):
    """Wrap an 8-byte ANT+ FE-C page for Tacx 'FE-C over BLE' (acknowledged message, XOR checksum)."""
    assert len(page) == 8
    body = bytes((0xA4, 0x09, 0x4F, 0x05)) + bytes(page)
    x = 0
    for b in body:
        x ^= b
    return body + bytes((x,))


def fec_track_resistance(grade_percent, crr=0.004):
    """FE-C page 51: gradient."""
    raw = max(0, min(40000, int(round((grade_percent + 200) / 0.01))))
    return fec_frame(bytes((0x33, 0xFF, 0xFF, 0xFF, 0xFF)) + struct.pack("<H", raw) + bytes((int(round(crr / 5e-5)),)))


def classify(name, service_uuids):
    """What a scanned device probably is: 'trainer', 'power', 'cadence', 'steering', 'heart' or None."""
    u = {s.lower() for s in (service_uuids or [])}
    n = (name or "").lower()
    if STERZO_SERVICE in u or "sterzo" in n:
        return "steering"
    if FTMS_SERVICE in u or FEC_SERVICE in u:
        return "trainer"
    if CPS_SERVICE in u:
        return "trainer" if any(k in n for k in ("neo", "tacx", "kickr", "flux", "hub", "direto", "suito", "h3", "volt", "wattbike", "trainer")) else "power"
    if CSC_SERVICE in u:
        return "cadence"
    if HR_SERVICE in u or any(k in n for k in ("hrm", "tickr", "polar h", "polar oh", "polar verity", "heart", "whoop", "coospo", "magene h")):
        return "heart"
    if any(k in n for k in ("tacx", "kickr", "wahoo core", "zwift hub", "direto", "suito", "saris h", "jetblack", "wattbike", "elite ")):
        return "trainer"
    return None
