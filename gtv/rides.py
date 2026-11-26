"""Ride log: one sample a second while ride mode is on, kept as a small file per ride, exported as TCX.

TCX is plain XML that Strava, Garmin Connect, TrainingPeaks and intervals.icu all accept. The game does not tell
us how far the bike went, so speed and distance are what your power would give on a flat road outdoors.
"""
import json, os, time
from xml.sax.saxutils import escape

DIR = os.environ.get("GTV_RIDES", os.path.expanduser("~/.local/share/gtv/rides"))
MIN_SAMPLES = 30          # a ride shorter than this is thrown away when it stops


def flat_speed(watts, mass_kg=85.0, cda=0.32, crr=0.005, rho=1.2):
    """Meters per second on a flat road for this power (bisection on drag + rolling resistance)."""
    if watts <= 0:
        return 0.0
    lo, hi = 0.0, 30.0
    for _ in range(40):
        v = (lo + hi) / 2
        if 0.5 * rho * cda * v ** 3 + crr * mass_kg * 9.81 * v > watts:
            hi = v
        else:
            lo = v
    return lo


def _path(ride_id):
    if not ride_id.replace("-", "").isdigit():          # ids are dates; nothing else may reach the filesystem
        raise ValueError("bad ride id")
    return os.path.join(DIR, ride_id + ".jsonl")


def read(ride_id):
    with open(_path(ride_id)) as f:
        rows = [json.loads(ln) for ln in f if ln.strip()]
    return [r for r in rows if "t" in r]


def summary(ride_id, rows=None):
    rows = read(ride_id) if rows is None else rows
    if not rows:
        return None
    n = len(rows); hr = [r["hr"] for r in rows if r.get("hr")]
    return {"id": ride_id, "start": rows[0]["t"], "moving_s": n, "elapsed_s": int(rows[-1]["t"] - rows[0]["t"]) + 1,
            "avg_watts": round(sum(r["w"] for r in rows) / n), "max_watts": max(r["w"] for r in rows),
            "avg_cadence": round(sum(r["c"] for r in rows) / n), "avg_hr": round(sum(hr) / len(hr)) if hr else None,
            "max_hr": max(hr) if hr else None, "distance_m": round(rows[-1]["d"]), "kj": round(sum(r["w"] for r in rows) / 1000)}


def list_rides():
    out = []
    try:
        names = sorted(os.listdir(DIR), reverse=True)
    except FileNotFoundError:
        return out
    for name in names:
        if name.endswith(".jsonl"):
            try:
                s = summary(name[:-6])
            except (OSError, ValueError, KeyError):
                continue
            if s:
                out.append(s)
    return out


def delete(ride_id):
    os.remove(_path(ride_id))


def tcx(ride_id):
    rows = read(ride_id)
    if not rows:
        raise FileNotFoundError(ride_id)
    s = summary(ride_id, rows)
    iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2" '
           'xmlns:ns3="http://www.garmin.com/xmlschemas/ActivityExtension/v2">',
           '<Activities><Activity Sport="Biking">', f'<Id>{iso(rows[0]["t"])}</Id>',
           f'<Lap StartTime="{iso(rows[0]["t"])}"><TotalTimeSeconds>{s["moving_s"]}</TotalTimeSeconds>'
           f'<DistanceMeters>{s["distance_m"]}</DistanceMeters><Calories>{s["kj"]}</Calories>'
           '<Intensity>Active</Intensity><TriggerMethod>Manual</TriggerMethod><Track>']
    for r in rows:
        hr = f'<HeartRateBpm><Value>{int(r["hr"])}</Value></HeartRateBpm>' if r.get("hr") else ""
        out.append(f'<Trackpoint><Time>{iso(r["t"])}</Time><DistanceMeters>{r["d"]:.1f}</DistanceMeters>{hr}'
                   f'<Cadence>{int(min(254, r["c"]))}</Cadence><Extensions><ns3:TPX><ns3:Speed>{r["v"]:.2f}</ns3:Speed>'
                   f'<ns3:Watts>{int(r["w"])}</ns3:Watts></ns3:TPX></Extensions></Trackpoint>')
    out.append('</Track></Lap><Notes>' + escape("Grand Theft Velo") + '</Notes>'
               '<Creator xsi:type="Device_t" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><Name>Grand Theft Velo</Name>'
               '<UnitId>0</UnitId><ProductID>0</ProductID></Creator></Activity></Activities></TrainingCenterDatabase>')
    return "\n".join(out)


class Recorder:
    """add() once a second. Seconds with no pedaling are left out, so the file holds moving time only."""

    def __init__(self, log):
        self.log, self.id, self._f, self.samples, self.distance, self.started = log, None, None, 0, 0.0, 0.0

    @property
    def on(self):
        return self._f is not None

    def start(self):
        if self.on:
            return
        os.makedirs(DIR, exist_ok=True)
        self.started, self.samples, self.distance = time.time(), 0, 0.0
        base = self.id = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.started)); n = 1
        while os.path.exists(_path(self.id)):               # two rides started in the same second
            n += 1; self.id = f"{base}-{n}"
        self._f = open(_path(self.id), "a", buffering=1)
        self._f.write(json.dumps({"start": self.started, "version": 1}) + "\n")

    def add(self, watts, cadence, hr):
        if not self.on or (watts <= 0 and cadence <= 0):
            return
        v = flat_speed(watts)
        self.distance += v; self.samples += 1
        row = {"t": round(time.time(), 1), "w": round(watts), "c": round(cadence), "v": round(v, 2), "d": round(self.distance, 1)}
        if hr:
            row["hr"] = round(hr)
        self._f.write(json.dumps(row) + "\n")

    def stop(self):
        if not self.on:
            return None
        self._f.close(); self._f = None
        if self.samples < MIN_SAMPLES:
            delete(self.id); self.log("ride log: too short to keep"); return None
        s = summary(self.id)
        self.log("ride log: saved %d min, %d W average, %.1f km" % (s["moving_s"] // 60, s["avg_watts"], s["distance_m"] / 1000))
        return s
