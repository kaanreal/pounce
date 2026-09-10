"""timed burst sniper for names with a known exact droptime,
plus server clock sync via Date headers."""
import asyncio
import statistics
import time
from email.utils import parsedate_to_datetime

from .common import MCSERVICES, fmt_duration, green, log, red
from .mojang import check_name, claim_name
from .store import event, kv_set, set_state


async def estimate_offset(session, samples=7):
    """offset = server_clock - local_clock, seconds. date header has 1s
    granularity so treat each sample as covering [t, t+1)."""
    offsets = []
    url = f"{MCSERVICES}/"  # any response carries a Date header
    for _ in range(samples):
        local = time.time()
        async with session.get(url) as r:
            await r.read()
            date_hdr = r.headers.get("Date")
        rtt = time.time() - local
        if not date_hdr:
            continue
        server = parsedate_to_datetime(date_hdr).timestamp()
        offsets.append((server + 0.5) - (local + rtt / 2))
    if not offsets:
        raise RuntimeError("could not read server time")
    return statistics.median(offsets), max(abs(o - statistics.median(offsets)) for o in offsets)


class Clock:
    def __init__(self, offset):
        self.offset = offset
        self._mono0 = time.monotonic()
        self._wall0 = time.time()

    def now(self):
        """server time right now"""
        return self._wall0 + self.offset + (time.monotonic() - self._mono0)

    async def sleep_until(self, server_ts):
        while True:
            remaining = server_ts - self.now()
            if remaining <= 0:
                return
            await asyncio.sleep(min(remaining, 1.0))


def classify_claim(status, body):
    if status == 200:
        return "won"
    if status == 403:
        detail = ""
        if isinstance(body, dict):
            detail = str((body.get("details") or {}).get("status", body.get("errorMessage", "")))
        return f"forbidden ({detail or 'taken / cooldown / your own 30 day lock'})"
    if status == 429:
        return "ratelimited"
    if status == -1:
        return f"network error: {body}"
    return f"http {status}"


async def snipe(session, cfg, name, droptime, token_getter, dry_run=False):
    """fire PUT attempts around droptime until one lands.
    droptime: unix seconds, exact."""
    lead = cfg["put_lead_ms"] / 1000
    spacing = cfg["put_spacing_ms"] / 1000
    attempts = cfg["put_attempts"]

    if not dry_run:
        await token_getter()
    offset, err = await estimate_offset(session)
    clock = Clock(offset)
    log().info("clock offset vs %s: %+.3fs (+-%.2fs)", MCSERVICES.split("//")[1], offset, err)

    wait_s = droptime - clock.now() - lead
    if wait_s > 0:
        log().info("%s drops in %s, arming (%d attempts, %.0fms apart)",
                   name, fmt_duration(wait_s), attempts, spacing * 1000)
        await clock.sleep_until(droptime - lead)
    else:
        log().warning("droptime already passed by %.1fs, firing immediately", -wait_s)

    result = None
    for i in range(attempts):
        if dry_run:
            log().info("[dry-run] attempt %d: would PUT %s", i + 1, name)
        else:
            status, body = await claim_name(session, token, name)
            verdict = classify_claim(status, body)
            log().info("attempt %d -> %s", i + 1, green(verdict) if status == 200 else red(verdict))
            event("CLAIM_ATTEMPT", name, f"{status} {verdict}")
            if status == 200:
                result = "won"
                break
            # 403 here just means still inside mojang's hold window if our
            # droptime anchor was a bit early. keep the burst going, the
            # press phase after handles a still-locked name.
            # 429 / 5xx / network: back off slightly and keep trying
        await asyncio.sleep(spacing)

    if result is None:
        result = await _press_until_lift(session, cfg, name, token)

    if dry_run:
        log().info("[dry-run] done")
        return "dry-run"

    if result == "won":
        set_state(name, "claimed")
        event("CLAIM_SUCCESS", name, "timed snipe")
        log().info(green(f">>> {name} IS YOURS <<<"))
        if cfg.get("pause_after_claim"):
            kv_set("auto_claim_paused", "1")
            log().warning("auto claiming paused (you just used your name change). "
                          "`resume` when you want it active again")
        return "won"

    event("CLAIM_GIVEUP", name, result)
    log().warning("%s: no luck (%s)", name, result)
    set_state(name, "missed")
    return result


async def _press_until_lift(session, cfg, name, token):
    """burst over but no win: the name is probably still inside mojang's
    hold because our droptime anchor was a little early. keep pressing on a
    widening ladder while the availability api still lists it as free.
    the instant the hold lifts one of these lands."""
    for delay in cfg.get("flip_retry_delays", [2, 10, 30, 120, 300, 900, 1800, 3600]):
        await asyncio.sleep(delay)
        try:
            res = await check_name(session, name)
        except Exception as e:
            log().warning("%s press check failed: %s", name, e)
            continue
        if res["status"] != "free":
            return "lost"  # someone took it while we waited
        status, body = await claim_name(session, token, name)
        verdict = classify_claim(status, body)
        log().info("press at +%ds -> %s", delay, green(verdict) if status == 200 else red(verdict))
        event("CLAIM_ATTEMPT", name, f"{status} {verdict}")
        if status == 200:
            return "won"
    return "gave up early / saw nothing land"
