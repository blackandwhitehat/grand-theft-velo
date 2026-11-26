"""Settings and pairing, kept in one JSON file that only the owner can read."""
import json, os, tempfile

PATH = os.environ.get("GTV_CONFIG", os.path.expanduser("~/.config/gtv/config.json"))
DEFAULTS = {
    "trainer": None,          # {"address": .., "name": ..}
    "steering": None,         # {"address": .., "name": ..}  (Elite Sterzo), or None for gamepad / no steering
    "heart": None,            # {"address": .., "name": ..}  any Bluetooth heart rate strap or armband
    "brake_gpio": None,       # BCM pin number of a switch wired to ground, or None
    "steer_invert": False,
    "pad_steer_address": None,   # with two controllers: the one on the handlebars
    "pad_steer": False,       # steer with a controller strapped to the handlebars (its motion sensors)
    "steer_center": 0.0,      # what the steering sensor reads with the bars straight (set by "Center the bars")
    "cadence_watts": 2.0,     # watts assumed per rpm when the sensor reports cadence only
    "grade": 0.0,             # resistance sent to a controllable trainer, as a road gradient in percent
    "autostart": False,       # start ride mode when the service starts
    "mapping": {"full_watts": 250, "min_watts": 20, "sprint_watts": 400, "sprint_hz": 6,
                "steer_full": 30, "steer_deadzone": 1.5, "idle_s": 300,
                "stop_brake_s": 2.0},   # with no brake switch: brake this long when you stop pedaling (0 = coast),
    "ps5": {"host": None, "name": None, "account_hex": None, "regist_key_hex": None, "morning_hex": None},
}


def _merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load():
    try:
        with open(PATH) as f:
            return _merge(DEFAULTS, json.load(f))
    except FileNotFoundError:
        return _merge(DEFAULTS, {})


def save(cfg):
    os.makedirs(os.path.dirname(PATH), mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(PATH))
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, PATH)


def public(cfg):
    """The settings without secrets, for the web page."""
    out = json.loads(json.dumps(cfg))
    ps5 = out["ps5"]
    out["ps5"] = {"host": ps5["host"], "name": ps5["name"], "has_account": bool(ps5["account_hex"]),
                  "paired": bool(ps5["regist_key_hex"] and ps5["morning_hex"])}
    return out
