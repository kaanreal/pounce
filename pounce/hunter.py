"""flip hunter: when we know a name drops somewhere inside a time window
(owner changed away, exact second unknown), poll its availability rapidly
and claim the moment it goes free."""
import asyncio

from .common import fmt_ts, green, log, red, yellow
from .mojang import check_name, claim_name
from .store import event, kv_get, set_state


async def hunt(session, cfg, target, token_getter):
    """poll availability until the name flips free, then claim immediately.
    gives up after drop_hi + post_margin."""
    name = target["name"]
    lo = target["drop_lo"]
    hi = target["drop_hi"]
    pre = cfg["hunt_pre_margin_s"]
    post = cfg["hunt_post_margin_s"]
    interval = cfg["hunt_poll_interval_s"]

    log().info("hunting %s (drops between %s and %s)", name, fmt_ts(lo), fmt_ts(hi))
    event("HUNT_START", name)

    # idle until shortly before the window opens
    loop = asyncio.get_event_loop()
    while loop.time() < lo - pre:
        await asyncio.sleep(min(30, lo - pre - max(loop.time(), 0)))

    # warm the token now, not during the critical flip second
    token = await token_getter()

    polls = 0
    while True:
        res = await check_name(session, name)
        polls += 1

        if res["status"] == "free":
            log().info("%s flipped FREE after %d polls, claiming!", green(name), polls)
            if kv_get("auto_claim_paused") == "1" or not cfg.get("auto_claim"):
                event("ALERT", name, "flipped free but auto claim disabled/paused")
                log().warning("%s is FREE but claiming is disabled! claim manually NOW", red(name))
                set_state(name, "missed")
                return False
            status, body = await claim_name(session, token, name)
            if status == 200:
                set_state(name, "claimed")
                event("CLAIM_SUCCESS", name, "flip hunt")
                log().info(green(f">>> {name} IS YOURS <<<"))
                return True
            set_state(name, "missed")
            event("CLAIM_FAIL", name, f"{status} {body}")
            log().warning("claim failed for %s: http %s %s", red(name), status, body)
            return False

        if res["status"] == "ratelimited":
            log().debug("hunt %s ratelimited, backing off", name)
            await asyncio.sleep(max(5.0, interval * 5))
            continue

        if time.time() > hi + post:
            # never flipped: owner probably reclaimed during days 30-37
            set_state(name, "watching")
            event("HUNT_GIVEUP", name, "window closed without a flip")
            log().info(yellow("hunt %s: window closed, no flip (owner likely kept it)"), name)
            return False

        await asyncio.sleep(interval)
