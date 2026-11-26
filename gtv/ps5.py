"""The console side: find it, look up the PSN account id, pair, and check whether it is awake."""
import asyncio, base64, json, os, socket
from urllib.parse import parse_qs, quote, urlparse

import aiohttp

# The public client id Sony's own Remote Play apps use; the same values chiaki-ng's psn-account-id script uses.
# Not a secret of this project: these are the public identifiers of Sony's own Remote Play app, the same ones
# every open Remote Play client (chiaki and others) uses to look up a PlayStation account id.
CLIENT_ID, CLIENT_SECRET = "ba495a24-818c-472b-b12d-ff231c1b5745", "mvaiZkRsAsI1IBkY"
REDIRECT = "https://remoteplay.dl.playstation.net/remoteplay/redirect"
LOGIN_URL = ("https://auth.api.sonyentertainmentnetwork.com/2.0/oauth/authorize?service_entity=urn:service-entity:psn"
             f"&response_type=code&client_id={CLIENT_ID}&redirect_uri={REDIRECT}&scope=psn:clientapp&request_locale=en_US"
             "&ui=pr&service_logo=ps&layout_type=popup&smcid=remoteplay&prompt=always&PlatformPrivacyWs1=minimal&")
TOKEN_URL = "https://auth.api.sonyentertainmentnetwork.com/2.0/oauth/token"
SEARCH = b"SRCH * HTTP/1.1\ndevice-discovery-protocol-version:00030010\n"
NATIVE = os.environ.get("GTV_NATIVE", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "native"))


def _parse(reply, addr):
    text = reply.decode(errors="replace")
    fields = dict(ln.split(":", 1) for ln in text.split("\n")[1:] if ":" in ln)
    first = text.split("\n")[0]
    return {"host": addr, "name": fields.get("host-name", "PS5").strip(), "type": fields.get("host-type", "").strip(),
            "state": "awake" if " 200 " in first else "standby" if " 620 " in first else first.strip()}


def _probe(targets, wait):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(wait)
    for t in targets:
        try:
            s.sendto(SEARCH, (t, 9302))
        except OSError:
            pass
    found = {}
    try:
        while True:
            data, addr = s.recvfrom(2048)
            found[addr[0]] = _parse(data, addr[0])
    except socket.timeout:
        pass
    s.close()
    return [c for c in found.values() if c["type"] == "PS5"]


async def discover():
    """Consoles answering on the local network."""
    return await asyncio.get_running_loop().run_in_executor(None, _probe, ["255.255.255.255"], 2.5)


async def state(host):
    """'awake', 'standby' or 'unreachable' for one console address."""
    if not host:
        return "not set"
    found = await asyncio.get_running_loop().run_in_executor(None, _probe, [host], 1.5)
    return found[0]["state"] if found else "unreachable"


async def account_from_redirect(redirect_url):
    """Exchange the address the PlayStation sign-in page ends on for the account id (16 hex characters)."""
    code = parse_qs(urlparse(redirect_url.strip()).query).get("code", [None])[0]
    if not code:
        raise ValueError("that address has no sign-in code in it; copy the whole address of the page that says 'redirect'")
    auth = aiohttp.BasicAuth(CLIENT_ID, CLIENT_SECRET)
    async with aiohttp.ClientSession() as s:
        body = f"grant_type=authorization_code&code={code}&redirect_uri={REDIRECT}&"
        async with s.post(TOKEN_URL, auth=auth, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}) as r:
            tok = await r.json(content_type=None)
            if "access_token" not in tok:
                raise ValueError("PlayStation refused the code; codes work once and expire quickly, so sign in again")
        async with s.get(TOKEN_URL + "/" + quote(tok["access_token"]), auth=auth) as r:
            info = await r.json(content_type=None)
    if "user_id" not in info:
        raise ValueError("PlayStation did not return an account id")
    return int(info["user_id"]).to_bytes(8, "little").hex()


def account_from_base64(text):
    """Accept an account id copied from chiaki or playactor (base64 of 8 bytes)."""
    raw = base64.b64decode(text.strip())
    if len(raw) != 8:
        raise ValueError("an account id is 8 bytes")
    return raw.hex()


async def register(host, account_hex, pin):
    """Pair using the 8-digit code from Settings > System > Remote Play > Link Device."""
    pin = "".join(ch for ch in str(pin) if ch.isdigit())
    if len(pin) != 8:
        return {"ok": False, "error": "the code is 8 digits"}
    env = dict(os.environ, RP_HOST=host, RP_ACCOUNT_HEX=account_hex, RP_PIN=pin)
    p = await asyncio.create_subprocess_exec(os.path.join(NATIVE, "register"), env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, _ = await asyncio.wait_for(p.communicate(), 45)
    except asyncio.TimeoutError:
        p.kill()
        return {"ok": False, "error": "the console did not answer"}
    for ln in reversed(out.decode(errors="replace").splitlines()):
        if ln.startswith("{"):
            return json.loads(ln)
    return {"ok": False, "error": "pairing tool gave no result"}
