"""the always-on daemon: cycles through watched names looking for ownership
changes (which reveal future droptimes), starts flip hunters for cooldown
targets, arms timed snipes for known droptimes, and can claim flips itself."""
import asyncio
import time

import aiohttp

from .auth import get_mc_token
from .common import (
    cyan,
    fmt_ts,
    green,
    log,
    red,
    yellow,
)
from .hunter import hunt
from .mojang import RateLimiter, batch_lookup, check_name, claim_name
from .store import (
    event,
    kv_get,
    kv_set,
    due_targets,
    mark_batch,
    pending_hunts,
    set_state,
    upcoming_timed,
)
from .timed import snipe


class Watcher:
    def __init__(self, cfg):
        self.cfg = cfg
        self.limiter = RateLimiter(cfg["limiter_max"], cfg["limiter_window_s"])
        self.hunt_tasks = {}
        self.timed_keys = set()
        self.session = None
        self._token_cache = {"value": None, "fetched": 0}
        self._checks = 0
        self._started = time.time()

    async def token(self):
        """cached mc token, refreshed when older than 30 min"""
        now_ = time.time()
        if self._token_cache["value"] and now_ - self._token_cache["fetched"] < 1800:
            return self._token_cache["value"]
        tok = await get_mc_token(self.session, self.cfg)
        self._token_cache = {"value": tok, "fetched": now_}
        return tok

    def paused(self):
        return kv_get("auto_claim_paused") == "1"

    def may_claim(self, target):
        if self.paused() or not self.cfg.get("auto_claim"):
            return False
        if target.get("priority", 0) >= 1 or self.cfg.get("auto_claim_all"):
            return True
        return False

    async def try_claim_flip(self, target):
        """a watched name just became free. this is the money path."""
        name = target["name"]
        if self.paused():
            event("ALERT", name, "flipped free but auto claim is paused")
            log().warning("%s flipped FREE but claiming is paused! claim manually now", red(name))
            return False
        if not self.may_claim(target):
            event("ALERT", name, "flipped free, priority too low to auto claim")
            log().warning("%s flipped FREE (priority %d, auto claim off for it)", yellow(name), target.get("priority", 0))
            return False
        try:
            token = await self.token()
        except Exception as e:
            event("ALERT", name, f"flip missed, no valid token: {e}")
            log().error("%s flipped FREE but token refresh failed: %s", red(name), e)
            return False
        status, body = await claim_name(self.session, token, name)
        if status == 200:
            set_state(name, "claimed")
            event("CLAIM_SUCCESS", name, "cycle flip")
            log().info(green(f">>> {name} IS YOURS <<<"))
            if self.cfg.get("pause_after_claim"):
                kv_set("auto_claim_paused", "1")
                log().warning("auto claiming paused after the win (`resume` to re-enable)")
            return True
        event("CLAIM_FAIL", name, f"{status} {body}")
        log().warning("claim of %s failed: http %s %s", name, status, body)
        return False

    async def _flip_flow(self, target):
        """a watched name just went free. claim it, and if the first try
        hits a transient error keep re-firing while it stays free.
        burst now, then a retry ladder, then give up."""
        name = target["name"]
        if not self.may_claim(target):
            return

        async def one_shot():
            tok = await self.token()
            status, body = await claim_name(self.session, tok, name)
            if status == 200:
                set_state(name, "claimed")
                event("CLAIM_SUCCESS", name, "drop burst")
                log().info(green(f">>> {name} IS YOURS <<<"))
                if self.cfg.get("pause_after_claim"):
                    kv_set("auto_claim_paused", "1")
                    log().warning("auto claiming paused after the win"
                                  " (`resume` to re-enable)")
            else:
                event("CLAIM_FAIL", name, f"{status} {body}")
                log().warning("claim of %s failed: http %s %s", name, status, body)
            return status

        if await one_shot() == 200:
            return
        # transient failures (429 / 5xx / net): keep trying while it stays free
        for delay in self.cfg.get("flip_retry_delays", [10, 30, 90, 300]):
            await asyncio.sleep(delay)
            if self.paused():
                break
            res = await check_name(self.session, name, self.limiter)
            if res["status"] == "free":
                if await one_shot() == 200:
                    return
            else:
                event("TAKEN_AGAIN", name, "someone grabbed it while we retried")
                log().info("%s got taken while we retried, moving on", yellow(name))
                return
        set_state(name, "missed")

    def handle_batch(self, targets, present, ts):
        """one sweep slice. flags list of (target) that just dropped."""
        dropped = []
        for t in targets:
            name = t["name"]
            was_present = bool(t.get("present")) or bool(t.get("owner_uuid"))
            is_present = name.lower() in present
            if was_present and not is_present:
                lo = t.get("last_present") or t.get("last_checked") or ts
                event("FLIP_FREE", name, f"freeing since ~{fmt_ts(lo)}")
                log().info("%s just went FREE (since %s)!", cyan(name), fmt_ts(lo))
                dropped.append(t)
            elif not was_present and is_present:
                event("TAKEN", name)
                log().info("%s got taken", yellow(name))
            mark_batch(name, is_present, t.get("owner_uuid") if is_present else "", ts)
        return dropped

    def _handle_dropped(self, target):
        name = target["name"]
        if self.paused():
            event("ALERT", name, "flipped free but auto claim is paused")
            log().warning("%s flipped FREE but claiming is paused! claim manually now",
                          red(name))
            return
        if not self.may_claim(target):
            event("ALERT", name, "flipped free, priority too low to auto claim")
            log().warning("%s flipped FREE (priority %d, auto claim off for it)",
                          yellow(name), target.get("priority", 0))
            return
        asyncio.ensure_future(self._flip_flow(target))

    def schedule_timed(self):
        horizon = 6 * 3600
        for t in upcoming_timed(horizon):
            if t["priority"] < 1:
                continue
            key = (t["name"], t["droptime"])
            if key in self.timed_keys:
                continue
            self.timed_keys.add(key)
            task = asyncio.ensure_future(
                self._timed_wrapper(t["name"], t["droptime"])
            )
            log().info("armed timed snipe: %s at %s", cyan(t["name"]), fmt_ts(t["droptime"]))
            self.hunt_tasks[f"timed:{t['name']}:{t['droptime']}"] = task

    async def _timed_wrapper(self, name, droptime):
        try:
            await snipe(self.session, self.cfg, name, droptime, self.token)
        except Exception as e:
            event("ERROR", name, f"timed snipe crashed: {e}")
            log().error("timed snipe for %s crashed: %s", name, e)

    def schedule_hunts(self):
        if not self.cfg.get("auto_claim") or self.paused():
            return
        active = [k for k, t in self.hunt_tasks.items() if not t.done()]
        slots = self.cfg["hunt_max_concurrent"] - sum(1 for k in active if not k.startswith("timed:"))
        for t in pending_hunts(self.cfg["hunt_pre_margin_s"]):
            if slots <= 0:
                break
            if t["priority"] < 1 or t["name"] in self.hunt_tasks:
                continue
            self.hunt_tasks[t["name"]] = asyncio.ensure_future(self._hunt_wrapper(t))
            slots -= 1

    async def _hunt_wrapper(self, target):
        try:
            await hunt(self.session, self.cfg, target, self.token)
        except Exception as e:
            event("ERROR", target["name"], f"hunt crashed: {e}")
            log().error("hunt for %s crashed: %s", target["name"], e)

    def reap_tasks(self):
        done = [k for k, t in self.hunt_tasks.items() if t.done]
        for k in done:
            del self.hunt_tasks[k]

    async def run_forever(self):
        cfg = self.cfg
        connector = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300)
        async with aiohttp.ClientSession(connector=connector) as session:
            self.session = session
            log().info("watcher running. ctrl+c to stop")
            while True:
                try:
                    self.schedule_timed()
                    self.schedule_hunts()
                    self.reap_tasks()

                    hunting = [k for k, t in self.hunt_tasks.items()
                               if not t.done() and not k.startswith("timed:")]
                    if hunting:
                        # hunts get the rate limit budget
                        await asyncio.sleep(2)
                        continue

                    slice_ = due_targets(self.cfg.get("batch_slice", 10))
                    if not slice_:
                        await asyncio.sleep(5)
                        continue

                    present = await batch_lookup(
                        session, self.limiter, [t["name"] for t in slice_])
                    if present is None:
                        await asyncio.sleep(min(max(cfg["check_spacing_s"], 0.5), 5))
                        continue

                    ts = time.time()
                    for t in self.handle_batch(slice_, present, ts):
                        self._handle_dropped(t)
                    self._checks += len(slice_)
                    if self._checks % 2000 == 0:
                        rate = self._checks / (time.time() - self._started)
                        log().info("progress: %d names sampled (%.0f/min)",
                                   self._checks, rate * 60)
                    await asyncio.sleep(max(cfg["check_spacing_s"], 0.8))
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    event("ERROR", detail=str(e))
                    log().error("watcher loop error: %s", e)
                    await asyncio.sleep(10)


async def run_watcher(cfg):
    w = Watcher(cfg)
    task = asyncio.ensure_future(w.run_forever())
    try:
        await task
    except (KeyboardInterrupt, asyncio.CancelledError):
        log().info("stopping watcher...")
        for t in w.hunt_tasks.values():
            t.cancel()
