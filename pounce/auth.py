"""Microsoft login -> Xbox Live -> XSTS -> minecraft services token.

default flow: authorization code flow through login.live.com using the
official launcher's client id (already approved for the minecraft api).
you open a link in any browser, log in, and paste back the url you land on.
that url contains the auth code, nothing sensitive beyond it.

the device code flow also exists but only works with your own azure app
after mojang approves it (aka.ms/mce-reviewappid).

stores everything in data/tokens.json (chmod 600). that file is full access
to the account, treat it like a password.
"""
import asyncio
import json
import os
import stat
import time
from urllib.parse import parse_qs, urlencode, urlparse

import aiohttp

from .common import MCSERVICES, TOKENS_PATH, load_config, log

MS_DEVICE_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode"
MS_TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
LIVE_AUTHORIZE = "https://login.live.com/oauth20_authorize.srf"
LIVE_TOKEN = "https://login.live.com/oauth20_token.srf"
LIVE_REDIRECT = "https://login.live.com/oauth20_desktop.srf"
XBL_URL = "https://user.auth.xboxlive.com/user/authenticate"
XSTS_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"
# the launcher's own client id, already approved for the minecraft api.
# only usable with the login.live.com endpoints above.
LAUNCHER_SCOPE = "service::user.auth.xboxlive.com::MBI_SSL"

SCOPE = "XboxLive.signin offline_access"


def save_tokens(tokens):
    TOKENS_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKENS_PATH.write_text(json.dumps(tokens, indent=2) + "\n")
    os.chmod(TOKENS_PATH, stat.S_IRUSR | stat.S_IWUSR)


def load_tokens():
    if not TOKENS_PATH.exists():
        return None
    try:
        return json.loads(TOKENS_PATH.read_text())
    except Exception:
        return None


async def _post_form(session, url, data):
    async with session.post(url, data=data) as r:
        return r.status, await r.json(content_type=None)


async def _post_json(session, url, payload):
    async with session.post(url, json=payload) as r:
        return r.status, await r.json(content_type=None)


async def browser_login(session, cfg):
    """launcher-style login: open link in a browser anywhere, paste back the
    url of the page you land on. works with the launcher client id."""
    cid = cfg["client_id"]
    q = urlencode({
        "client_id": cid,
        "response_type": "code",
        "redirect_uri": LIVE_REDIRECT,
        "scope": LAUNCHER_SCOPE,
        "prompt": "select_account",
        # launcher-specific params, they skip some consent pages
        "lw": "1",
        "fl": "dob,easi2",
        "xsup": "1",
        "nopa": "2",
    })
    print()
    print("  open this in any browser and log in with the microsoft account")
    print("  that owns your minecraft profile:")
    print()
    print(f"  {LIVE_AUTHORIZE}?{q}")
    print()
    print("  after logging in the page lands on a blank/success page.")
    print("  copy its FULL url from the address bar and paste it here")
    print(f"  (it starts with {LIVE_REDIRECT}?code=...)")
    pasted = input("\n> ").strip()

    code = parse_qs(urlparse(pasted).query).get("code")
    if not code:
        raise RuntimeError(
            "no ?code= found in what you pasted. log in again and copy the "
            "address bar url exactly after the page settles"
        )
    status, tok = await _post_form(session, LIVE_TOKEN, {
        "client_id": cid,
        "code": code[0],
        "redirect_uri": LIVE_REDIRECT,
        "grant_type": "authorization_code",
        "scope": LAUNCHER_SCOPE,
    })
    if status != 200 or "access_token" not in tok:
        raise RuntimeError(f"token exchange failed: {status} {tok}")

    # save the refresh token first, so even if the xbox/minecraft chain
    # hiccups we can rebuild everything later without re-login
    save_tokens({"refresh_token": tok["refresh_token"], "flow": "browser"})
    tokens = await _exchange_all(session, tok["access_token"], tok["refresh_token"])
    tokens["flow"] = "browser"
    save_tokens(tokens)
    log().info("logged in as %s (%s)", tokens.get("name"), tokens.get("uuid"))
    return tokens


async def device_login(session, cfg):
    """interactive one-time login. prints a link and code, you auth in a browser."""
    cid = cfg["client_id"]
    status, d = await _post_form(session, MS_DEVICE_URL, {"client_id": cid, "scope": SCOPE})
    if status != 200:
        raise RuntimeError(f"device code request failed: {status} {d}")
    print()
    print(f"  open  {d['verification_uri']}")
    print(f"  code  {d['user_code']}")
    print()
    interval = int(d.get("interval", 5))
    deadline = time.time() + int(d.get("expires_in", 900))
    while time.time() < deadline:
        await asyncio.sleep(interval)
        status, tok = await _post_form(
            session,
            MS_TOKEN_URL,
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": cid,
                "device_code": d["device_code"],
            },
        )
        if status == 200:
            break
        err = tok.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        raise RuntimeError(f"login failed: {err}: {tok.get('error_description')}")
    else:
        raise RuntimeError("device code expired before you finished logging in")

    save_tokens({"refresh_token": tok["refresh_token"], "flow": "device"})
    tokens = await _exchange_all(session, tok["access_token"], tok["refresh_token"])
    tokens["flow"] = "device"
    save_tokens(tokens)
    log().info("logged in as %s (%s)", tokens.get("name"), tokens.get("uuid"))
    return tokens


async def _exchange_all(session, msa_access, msa_refresh):
    xbl = await _xbl(session, msa_access)
    xsts = await _xsts(session, xbl["Token"])
    uhs = xsts["DisplayClaims"]["xui"][0]["uhs"]
    mc = await _mc_token(session, uhs, xsts["Token"])
    profile = await _profile(session, mc["access_token"])
    return {
        "refresh_token": msa_refresh,
        "mc_token": mc["access_token"],
        "mc_expires": time.time() + int(mc.get("expires_in", 86400)),
        "uhs": uhs,
        "uuid": profile.get("id"),
        "name": profile.get("name"),
        "saved_at": time.time(),
    }


async def _xbl(session, msa_token):
    payload = {
        "Properties": {
            "AuthMethod": "RPS",
            "SiteName": "user.auth.xboxlive.com",
            "RpsTicket": f"d={msa_token}",
        },
        "RelyingParty": "http://auth.xboxlive.com",
        "TokenType": "JWT",
    }
    # the d= prefix requirement depends on where the msa token came from,
    # so try both. xbox returns empty bodies on failures, hence "None".
    base_props = {"AuthMethod": "RPS", "SiteName": "user.auth.xboxlive.com"}
    last_err = None
    for ticket in (f"d={msa_token}", msa_token):
        payload = {
            "Properties": dict(base_props, RpsTicket=ticket),
            "RelyingParty": "http://auth.xboxlive.com",
            "TokenType": "JWT",
        }
        status, d = await _post_json(session, XBL_URL, payload)
        if status == 200:
            return d
        last_err = (status, d)
    raise RuntimeError(f"xbl auth failed: {last_err[0]} {last_err[1]}")


async def _xsts(session, xbl_token):
    payload = {
        "Properties": {"SandboxId": "RETAIL", "UserTokens": [xbl_token]},
        # minecraft services requires the token to be issued for this
        # relying party; the old http://xboxlive.com one gets 401'd later
        "RelyingParty": "rp://api.minecraftservices.com/",
        "TokenType": "JWT",
    }
    status, d = await _post_json(session, XSTS_URL, payload)
    if status != 200:
        code = (d.get("XErr") if isinstance(d, dict) else None) or status
        reasons = {
            2148916233: "no xbox account on this microsoft account",
            2148916235: "xbox unavailable in your country",
            2148916236: "adult verification needed on the xbox account",
            2148916237: "child account needs family settings fixed",
            2148916238: "child account: add it to a family group",
        }
        reason = reasons.get(code, d)
        raise RuntimeError(f"xsts auth failed ({code}): {reason}")
    return d


async def _mc_token(session, uhs, xsts_token):
    status, d = await _post_json(
        session,
        f"{MCSERVICES}/authentication/login_with_xbox",
        {"identityToken": f"XBL3.0 x={uhs};{xsts_token}"},
    )
    if status != 200:
        # most common cause: azure app without minecraft api permission
        raise RuntimeError(f"minecraft token failed: {status} {d}")
    return d


async def _profile(session, mc_token):
    from .mojang import get_profile

    status, d = await get_profile(session, mc_token)
    if status == 404:
        raise RuntimeError("this microsoft account does not own minecraft java (no profile)")
    if status != 200:
        raise RuntimeError(f"profile fetch failed: {status} {d}")
    return d


async def get_mc_token(session, cfg, force=False):
    """valid cached mc token, refreshed through the whole chain when needed"""
    tokens = load_tokens()
    if tokens is None:
        raise RuntimeError("not logged in yet: run `python sniper.py login` first")
    if not force and tokens.get("mc_token") and tokens.get("mc_expires", 0) > time.time() + 600:
        return tokens["mc_token"]
    log().info("refreshing auth chain...")
    flow = tokens.get("flow", "browser")
    if flow == "browser":
        status, tok = await _post_form(session, LIVE_TOKEN, {
            "client_id": cfg["client_id"],
            "refresh_token": tokens["refresh_token"],
            "grant_type": "refresh_token",
            "scope": LAUNCHER_SCOPE,
        })
    else:
        status, tok = await _post_form(session, MS_TOKEN_URL, {
            "grant_type": "refresh_token",
            "client_id": cfg["client_id"],
            "refresh_token": tokens["refresh_token"],
            "scope": SCOPE,
        })
    if status != 200:
        raise RuntimeError(f"token refresh failed ({tok.get('error')}): log in again with `login`")
    tokens = await _exchange_all(session, tok["access_token"], tok["refresh_token"])
    tokens["flow"] = flow
    save_tokens(tokens)
    return tokens["mc_token"]


async def whoami(session, cfg):
    token = await get_mc_token(session, cfg)
    profile = await _profile(session, token)
    tokens = load_tokens() or {}
    return {
        "name": profile.get("name"),
        "uuid": profile.get("id"),
        "token_expires_in": int((tokens.get("mc_expires", 0)) - time.time()),
    }
