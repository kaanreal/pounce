"""bulk availability scanning via mojang's batch lookup endpoint.
returns only profiles that exist, so anything missing from the response
is free (or held by a banned account, which a claim attempt would reveal).
"""
import asyncio
import string
from itertools import product

import aiohttp

from .common import API_MOJANG, log
from .mojang import RateLimiter
from .store import event

BATCH_URL = f"{API_MOJANG}/profiles/minecraft"
BATCH_SIZE = 10


async def _scan_batches(session, limiter, names, on_batch=None):
    """returns set of names that did NOT resolve to a profile"""
    names = list(dict.fromkeys(names))  # dedupe, keep order
    free = set()
    total = len(names)
    log().info("scanning %d names in %d batches", total, (total + BATCH_SIZE - 1) // BATCH_SIZE)
    # the limiter alone allows instant bursts which mojang punishes with 429,
    # so pace batches evenly like the watcher does
    spacing = limiter.window / limiter.max
    for i in range(0, total, BATCH_SIZE):
        await asyncio.sleep(spacing)
        await limiter.acquire()
        chunk = names[i:i + BATCH_SIZE]
        try:
            async with session.post(BATCH_URL, json=chunk) as r:
                if r.status == 429:
                    log().warning("batch scan ratelimited, backing off")
                    await asyncio.sleep(120)
                    continue  # retry this chunk
                if r.status != 200:
                    body = await r.text()
                    raise RuntimeError(f"batch endpoint failed: {r.status} {body[:200]}")
                found = {p["name"].lower() for p in await r.json(content_type=None)}
        except aiohttp.ClientError as e:
            log().warning("batch network error (%s), retrying after pause", e)
            await asyncio.sleep(10)
            continue
        free.update(n for n in chunk if n.lower() not in found)
        if on_batch:
            on_batch(i + len(chunk), total)
    return free


def three_letter_names(include_pure_alpha=False):
    alphabet = string.ascii_lowercase + string.digits + "_"
    for combo in product(alphabet, repeat=3):
        name = "".join(combo)
        if not include_pure_alpha and name.isalpha():
            continue  # watcher already covers these
        yield name


async def scan_free(session, cfg, names, label):
    limiter = RateLimiter(cfg["limiter_max"], cfg["limiter_window_s"])

    def progress(done, total):
        if done % 500 == 0 or done == total:
            log().info("[%s] %d/%d", label, done, total)

    free = await _scan_batches(session, limiter, names, on_batch=progress)
    log().info("[%s] found %d free", label, len(free))
    event("SCAN", detail=f"{label}: {len(free)} free of scanned")
    return sorted(free)
