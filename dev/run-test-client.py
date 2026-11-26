#!/usr/bin/env python3
"""Runs rptest with the existing playactor pairing. Secrets are passed in the environment and never printed."""
import base64, json, os, socket, subprocess, sys
HOST = os.environ.get("RP_HOST") or sys.exit("set RP_HOST to the console address")
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(3)
s.sendto(b"SRCH * HTTP/1.1\ndevice-discovery-protocol-version:00030010\n", (HOST, 9302))
try: status = s.recvfrom(2048)[0].decode(errors="replace").split("\n")[0].strip()
except socket.timeout: status = "no answer"
print("PS5 status:", status)
if "200" not in status: sys.exit("PS5 is not awake; not connecting")
c = list(json.load(open(os.path.expanduser("~/.config/playactor/credentials.json"))).values())[0]
r = c["registration"]
env = dict(os.environ, RP_HOST=HOST, RP_REGIST_KEY_HEX=r["PS5-RegistKey"], RP_MORNING_HEX=r["RP-Key"],
           RP_ACCOUNT_HEX=base64.b64decode(c["accountId"]).hex())
sys.exit(subprocess.call([os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "native", "rptest")] + sys.argv[1:], env=env))
