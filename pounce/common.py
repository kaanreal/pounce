"""Shared paths, config, constants and small helpers."""
import json
import logging
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "sniper.db"
TOKENS_PATH = DATA_DIR / "tokens.json"
CONFIG_PATH = DATA_DIR / "config.json"
WORDS_PATH = DATA_DIR / "good4l.txt"

API_MOJANG = "https://api.mojang.com"
MCSERVICES = "https://api.minecraftservices.com"

# old name is held this long after the owner changes away
DROP_DELAY_S = 37 * 24 * 3600

# public client id used by most community minecraft tools.
# ideally register your own azure app and put its id in config.json,
# but new apps need mojang's approval for the minecraft api permission,
# which is why the default is what it is.
DEFAULT_CLIENT_ID = "00000000402b5328"

DEFAULT_CONFIG = {
    "client_id": DEFAULT_CLIENT_ID,
    # auto claim when a watched name flips to free (priority >= 1 targets only)
    "auto_claim": True,
    # claim flips on bulk/priority-0 targets too (risky: uses your one shot)
    "auto_claim_all": False,
    # stop claiming anything after a win until you run `resume`
    "pause_after_claim": True,
    # availability check pacing (api.mojang.com allows ~600 per 10 min per ip)
    "check_spacing_s": 1.2,
    "limiter_max": 550,
    "limiter_window_s": 600,
    # batch sweep: names checked per request (max 10), full fleet pass ~45 min
    "batch_slice": 10,
    # after a drop, keep re-firing at these intervals while the name stays free
    "flip_retry_delays": [10, 30, 90, 300],
    # flip hunting (polling a name around its drop window)
    "hunt_pre_margin_s": 120,
    "hunt_post_margin_s": 600,
    "hunt_poll_interval_s": 1.0,
    "hunt_max_concurrent": 1,
    # timed burst sniping
    "put_lead_ms": 250,
    "put_spacing_ms": 150,
    "put_attempts": 25,
}


def log():
    return logging.getLogger("sniper")


def setup_logging(verbose=False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text()))
        except Exception as e:
            log().warning("config.json unreadable (%s), using defaults", e)
    return cfg


def save_config(cfg):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")


def now() -> float:
    return time.time()


def fmt_ts(ts):
    if not ts:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def fmt_duration(seconds):
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


# tiny ansi helpers, no dependency on color libs
def _c(code, s):
    return f"\033[{code}m{s}\033[0m"


def green(s):
    return _c("32", s)


def red(s):
    return _c("31", s)


def yellow(s):
    return _c("33", s)


def cyan(s):
    return _c("36", s)


def bold(s):
    return _c("1", s)
