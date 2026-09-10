"""Mojang / Minecraft services HTTP calls with a shared rate limiter."""
import asyncio
import json
from collections import deque
from urllib.parse import quote

import aiohttp

from .common import API_MOJANG, MCSERVICES, log


class RateLimiter:
    """sliding window limiter. api.mojang.com allows roughly 600 req / 10 min per ip,
    we default to 550 per 600s so normal interactive use still fits."""

    def __init__(self, max_events, window_s):
        self.max = max_events
        self.window = window_s
        self._times = deque()
        self._lock = asyncio.Lock()

    async def acquire(self):
        while True:
            async with self._lock:
                t = asyncio.get_event_loop().time()
                while self._times and t - self._times[0] > self.window:
                    self._times.popleft()
                if len(self._times) < self.max:
                    self._times.append(t)
                    return
                wait = min(self.window - (t - self._times[0]) + 0.05, 30)
            log().debug("rate limiter: waiting %.1fs", wait)
            await asyncio.sleep(wait)


def _headers(token=None):
    h = {"User-Agent": "mc-sniper/1.0"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


async def name_history(session, uuid, limiter=None):
    """public name history for a uuid: list of {name, changedToAt?} oldest
    first. changedToAt on a non-first entry is the exact instant the owner
    renamed into it, which is also the instant the previous name freed."""
    if limiter:
        await limiter.acquire()
    try:
        async with session.get(
            f"{API_MOJANG}/user/profile/{uuid}/names", headers=_headers()
        ) as r:
            if r.status != 200:
                return None
            return await r.json()
    except aiohttp.ClientError:
        return None


async def check_name(session, name, limiter=None):
    """availability via public lookup. returns dict:
    status: taken | free | ratelimited | error"""
    if limiter:
        await limiter.acquire()
    url = f"{API_MOJANG}/users/profiles/minecraft/{quote(name)}"
    try:
        async with session.get(url, headers=_headers()) as r:
            if r.status == 200:
                data = await r.json(content_type=None)
                return {"status": "taken", "uuid": (data or {}).get("id"), "name": (data or {}).get("name")}
            if r.status in (204, 404):
                return {"status": "free", "uuid": None, "name": None}
            if r.status == 429:
                return {"status": "ratelimited", "uuid": None, "name": None}
            return {"status": "error", "code": r.status, "uuid": None, "name": None}
    except aiohttp.ClientError as e:
        return {"status": "error", "code": str(e), "uuid": None, "name": None}


async def get_profile(session, token):
    """own profile via minecraft services, needs bearer token"""
    async with session.get(f"{MCSERVICES}/minecraft/profile", headers=_headers(token)) as r:
        return r.status, await r.json(content_type=None)


async def batch_lookup(session, limiter, names):
    """check up to 10 names at once: returns set of existing (taken) names,
    or None on 429/network error (caller should retry without consuming state)."""
    await limiter.acquire()
    try:
        async with session.post(f"{API_MOJANG}/profiles/minecraft", json=list(names)) as r:
            if r.status == 429:
                log().warning("batch sweep rate limited, backing off")
                return None
            if r.status != 200:
                log().warning("batch sweep http %d: %s", r.status, (await r.text())[:120])
                return None
            return {p["name"].lower() for p in await r.json(content_type=None)}
    except aiohttp.ClientError as e:
        log().warning("batch sweep network error (%s)", e)
        return None


async def claim_name(session, token, name):
    """the actual snipe: PUT the new name on our account.
    200 = won. 403 = taken / cooldown / we changed names <30 days ago.
    429 = rate limited. 5xx = their servers struggling (common on big drops)."""
    url = f"{MCSERVICES}/minecraft/profile/name/{quote(name)}"
    try:
        async with session.put(url, headers=_headers(token), json={}) as r:
            body = await r.text()
            try:
                body_json = json.loads(body)
            except Exception:
                body_json = None
            return r.status, body_json if body_json is not None else body
    except aiohttp.ClientError as e:
        return -1, str(e)
