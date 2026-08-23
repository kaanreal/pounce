"""import upcoming name drops.

namemc's upcoming-drops page is the only public-ish source of exact droptimes,
but it sits behind cloudflare and its html changes without notice. this scraper
is best effort: when it gets blocked it says so clearly and you can fall back
to `import-file` (paste rows from any site) or `add NAME --at ISO`.
"""
import re
from datetime import datetime, timezone

import aiohttp

from .common import log
from .store import event, upsert_target
from .words import load_good_words

NAMEMC_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class ScrapeBlocked(Exception):
    pass


def _drops_url(pages_back=0):
    t = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    url = f"https://namemc.com/minecraft-names?sort=asc&time={t}"
    if pages_back:
        url += f"&page={pages_back + 1}"
    return url


def parse_drops_html(html):
    """pull (name, droptime_iso) pairs out of namemc table rows"""
    drops = []
    # rows look like: <a href="/profile/NAME">NAME</a> ... <time ...="ISO">
    row_re = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
    name_re = re.compile(r'href="/profile/([A-Za-z0-9_]{3,16})"')
    time_re = re.compile(r'datetime="([^"]+)"')
    for row in row_re.findall(html):
        m = name_re.search(row)
        t = time_re.search(row)
        if m and t:
            drops.append((m.group(1), t.group(1)))
    return drops


async def fetch_namemc_drops(session, pages=1):
    all_drops = []
    for p in range(pages):
        url = _drops_url(p)
        async with session.get(url, headers={"User-Agent": NAMEMC_UA}) as r:
            body = await r.text()
            if r.status != 200 or "Just a moment" in body or "cf-chl" in body:
                raise ScrapeBlocked(
                    f"cloudflare blocked the request (status {r.status}). "
                    "use `import-file` or `add NAME --at ISO` instead"
                )
            all_drops.extend(parse_drops_html(body))
    return all_drops


def import_rows(rows, good_words=None):
    """rows: iterable of (name, droptime_str|None). returns (added, skipped_reasons)"""
    added, skipped = 0, {}
    for name, droptime in rows:
        name = name.strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{3,16}", name):
            skipped[name] = "invalid format"
            continue
        ts = None
        if droptime:
            try:
                s = droptime.replace("Z", "+00:00")
                dt = datetime.fromisoformat(s)
                if dt.tzinfo is None:
                    dt = dt.astimezone()
                ts = dt.timestamp()
            except ValueError:
                skipped[name] = f"bad time: {droptime}"
                continue
        # 3 letters always interesting, 4 letters only real words
        interesting = len(name) == 3 or (len(name) == 4 and good_words and name.lower() in good_words)
        if not interesting:
            skipped[name] = "not 3L / not a real word"
            continue
        upsert_target(name, priority=1, source="droplist", droptime=ts)
        added += 1
        event("IMPORT", name, f"droptime={ts}")
    return added, skipped
