#!/usr/bin/env python3
"""mc-sniper: watch minecraft name drops, snipe them automatically.

typical setup on a raspberry pi:

    python sniper.py login
    python sniper.py add4l            # queue all 4 letter dictionary words
    python sniper.py add3l            # queue all 17.5k three letter combos
    python sniper.py prioritize cat   # the ones you actually want to claim
    python sniper.py run              # leave it alone forever
"""
import argparse
import asyncio
import itertools
import re
import string
import sys
from datetime import datetime, timezone

import aiohttp

from pounce import auth, store, words
from pounce.common import (
    DATA_DIR,
    cyan,
    fmt_duration,
    fmt_ts,
    green,
    load_config,
    red,
    setup_logging,
    yellow,
)
from pounce.importer import ScrapeBlocked, fetch_namemc_drops, import_rows
from pounce.mojang import RateLimiter, check_name
from pounce.timed import snipe as timed_snipe
from pounce.watcher import Watcher


def parse_iso(s):
    """iso8601 to unix ts. naive timestamps are treated as local time."""
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.timestamp()


async def _session():
    return aiohttp.ClientSession()


# ---------------------------------------------------------------- commands

async def cmd_login(cfg, args):
    async with await _session() as s:
        if args.device:
            await auth.device_login(s, cfg)
        else:
            await auth.browser_login(s, cfg)


async def cmd_whoami(cfg, args):
    async with await _session() as s:
        me = await auth.whoami(s, cfg)
    print(f"account : {green(me['name'])}")
    print(f"uuid    : {me['uuid']}")
    print(f"token   : valid for another {fmt_duration(me['token_expires_in'])}")
    paused = store.kv_get("auto_claim_paused")
    if paused == "1":
        print(yellow("auto claim is PAUSED after your last win (`resume` to re-enable)"))


async def cmd_check(cfg, args):
    limiter = RateLimiter(60, 60)
    async with await _session() as s:
        for name in args.names:
            res = await check_name(s, name, limiter)
            if res["status"] == "taken":
                print(f"{name:<16} {red('taken')}   owner {res['uuid']}")
            elif res["status"] == "free":
                print(f"{name:<16} {green('FREE')}")
            elif res["status"] == "ratelimited":
                print(f"{name:<16} {yellow('ratelimited, try again later')}")
                break
            else:
                print(f"{name:<16} {yellow('error')}   {res.get('code')}")


async def cmd_add(cfg, args):
    droptime = parse_iso(args.at) if args.at else None
    store.upsert_target(args.name, priority=1, source="manual", droptime=droptime, note=args.note)
    when = f" drops at {fmt_ts(droptime)}" if droptime else ""
    print(f"added {green(args.name)} (priority 1){when}")


async def cmd_add3l(cfg, args):
    alphabet = string.ascii_lowercase + (string.digits + "_" if args.digits else "")
    names = ("".join(c) for c in itertools.product(alphabet, repeat=3))
    store.bulk_add(names, priority=0, source="bulk3l")
    total = len(store.list_targets())
    print(f"queued all {len(alphabet)**3:,} three letter combos ({total:,} targets total)")
    print(yellow("they are priority 0: watched only, auto claim off until you `prioritize` some"))


async def cmd_add4l(cfg, args):
    if args.file:
        raw = [w.strip().lower() for w in open(args.file)]
        four = sorted({w for w in raw if re.fullmatch(r"[a-z_]{4}", w)})
    else:
        _, count, used_dict = words.build_wordlist(dict_path=args.dict)
        four = words.load_good_words()
        src = f"system dictionary ({count} four letter words)" if used_dict else "built-in curated list"
        print(f"wordlist from {src} -> {len(four)} names")
    store.bulk_add(four, priority=0, source="bulk4l")
    print(f"queued {len(four):,} four letter names")


async def cmd_import_namemc(cfg, args):
    good = words.load_good_words()
    async with await _session() as s:
        try:
            rows = await fetch_namemc_drops(s, pages=args.pages)
        except ScrapeBlocked as e:
            print(red(str(e)))
            sys.exit(1)
    added, skipped = import_rows(rows, good)
    print(f"imported {added}, skipped {len(skipped)}")
    for name, why in list(skipped.items())[:10]:
        print(f"  {name}: {why}")


async def cmd_import_file(cfg, args):
    good = words.load_good_words()
    rows = []
    for line in open(args.path):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = re.split(r"[,\t]|\s{2,}", line, maxsplit=1)
        rows.append((parts[0].strip(), parts[1].strip() if len(parts) > 1 else None))
    added, skipped = import_rows(rows, good)
    print(f"imported {added}, skipped {len(skipped)}")


async def cmd_targets(cfg, args):
    rows = store.list_targets(state=args.state)
    counts = store.count_by_state()
    print(f"targets: {len(rows)}   states: {counts}")
    for t in rows[:args.limit]:
        drop = ""
        if t["droptime"]:
            drop = f"drops {fmt_ts(t['droptime'])}"
        elif t["drop_lo"]:
            drop = f"window {fmt_ts(t['drop_lo'])} .. {fmt_ts(t['drop_hi'])}"
        print(f"  {t['name']:<16} p{t['priority']} {t['state']:<9} "
              f"checked {fmt_ts(t['last_checked'])}  {drop}")


async def cmd_rm(cfg, args):
    for n in args.names:
        store.remove_target(n)
        print(f"removed {n}")


async def cmd_prioritize(cfg, args):
    if getattr(args, "all", False):
        n = store.prioritize_all()
        print(f"{green(str(n))} targets -> priority 1 (auto claim + hunts enabled)")
        return
    for n in args.names:
        store.upsert_target(n, priority=1)
        print(f"{green(n)} -> priority 1 (auto claim + hunts enabled)")


async def cmd_resume(cfg, args):
    store.kv_set("auto_claim_paused", "0")
    print("auto claiming re-enabled")


async def cmd_run(cfg, args):
    counts = store.count_by_state()
    if not counts:
        print(red("no targets yet. try `add4l`, `add3l`, `add NAME` or `import-namemc`"))
        sys.exit(1)
    print(f"target states: {counts}")
    me = None
    try:
        async with await _session() as s:
            me = await auth.whoami(s, cfg)
        print(f"logged in as {green(me['name'])}")
    except Exception as e:
        print(yellow(f"not logged in ({e}): watching still works, claiming does not"))
    await Watcher(cfg).run_forever()


async def cmd_snipe(cfg, args):
    droptime = parse_iso(args.at)
    async with await _session() as s:
        result = await timed_snipe(
            s, cfg, args.name, droptime,
            token_getter=lambda: auth.get_mc_token(s, cfg),
            dry_run=args.dry_run,
        )
    store.set_droptime(args.name, droptime)


async def cmd_hunt(cfg, args):
    lo, hi = parse_iso(args.frm), parse_iso(args.to)
    target = {"name": args.name, "drop_lo": lo, "drop_hi": hi}
    async with await _session() as s:
        token = lambda: auth.get_mc_token(s, cfg)
        await store.upsert_target(args.name, priority=1)
        from pounce.hunter import hunt

        await hunt(s, cfg, target, token)


async def cmd_scan_free(cfg, args):
    """find names that are free RIGHT NOW. watcher paused while scanning."""
    from pounce.batch import scan_free, three_letter_names
    from pounce.words import load_good_words

    results = {}
    async with await _session() as s:
        if args.words:
            results["4l words"] = await scan_free(s, cfg, sorted(load_good_words()), "4l words")
        if args.three:
            results["3char digit/underscore"] = await scan_free(
                s, cfg, three_letter_names(include_pure_alpha=args.pure_alpha_too),
                "3char")

    out = DATA_DIR / "free_found.txt"
    with open(out, "w") as f:
        for label, names in results.items():
            f.write(f"# {label}\n")
            for n in names:
                f.write(n + "\n")
    for label, names in results.items():
        print(f"\n{label}: {len(names)} free")
        print("  " + (", ".join(names[:80]) if names else "(none)"))
        if len(names) > 80:
            print(f"  ... and {len(names) - 80} more, see {out}")
    print(f"\nfull list saved to {out}")


async def cmd_events(cfg, args):
    for e in reversed(store.recent_events(args.limit)):
        print(f"{fmt_ts(e['ts'])}  {e['kind']:<13} {e['name'] or '-':<16} {e['detail'] or ''}")


# ---------------------------------------------------------------- parser

def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="cmd", required=True)

    l = sub.add_parser("login", help="login with the microsoft account owning your profile")
    l.add_argument("--device", action="store_true",
                   help="device code flow instead (only for your own approved azure app)")
    sub.add_parser("whoami", help="show logged in account + token state")

    c = sub.add_parser("check", help="check availability of names right now")
    c.add_argument("names", nargs="+")

    a = sub.add_parser("add", help="add one name with top priority")
    a.add_argument("name")
    a.add_argument("--at", help="exact drop time, iso8601 (e.g. 2026-09-01T18:00:02)")
    a.add_argument("--note")

    sub.add_parser("add3l", help="queue every 3 letter name").add_argument(
        "--digits", action="store_true", help="also include digits and underscore (37^3 combos)")

    d = sub.add_parser("add4l", help="queue 4 letter dictionary words")
    d.add_argument("--dict", help="path to a dictionary file (default: system dictionary)")
    d.add_argument("--file", help="path to a custom candidate list, one word per line")

    n = sub.add_parser("import-namemc", help="scrape namemc upcoming drops (best effort)")
    n.add_argument("--pages", type=int, default=1)

    f = sub.add_parser("import-file", help="import lines of 'NAME' or 'NAME,DROPTIME_ISO'")
    f.add_argument("path")

    t = sub.add_parser("targets", help="list targets")
    t.add_argument("--state")
    t.add_argument("--limit", type=int, default=50)

    r = sub.add_parser("rm", help="remove targets")
    r.add_argument("names", nargs="+")

    pr = sub.add_parser("prioritize", help="enable auto claim + hunting for names")
    pr.add_argument("names", nargs="*")
    pr.add_argument("--all", action="store_true",
                    help="prioritize every target in the db")

    sub.add_parser("resume", help="re-enable auto claiming after a win")
    sub.add_parser("run", help="start the always-on watcher daemon")

    s = sub.add_parser("snipe", help="snipe one name at an exact time")
    s.add_argument("name")
    s.add_argument("--at", required=True)
    s.add_argument("--dry-run", action="store_true")

    h = sub.add_parser("hunt", help="manually hunt a name over a time window")
    h.add_argument("name")
    h.add_argument("--from", dest="frm", required=True)
    h.add_argument("--to", dest="to", required=True)

    e = sub.add_parser("events", help="recent events log")
    e.add_argument("--limit", type=int, default=30)

    sc = sub.add_parser("scan-free", help="scan for names free right now")
    sc.add_argument("--words", action="store_true", help="scan the 4 letter wordlist")
    sc.add_argument("--three", action="store_true", help="scan 3 char digit/underscore combos")
    sc.add_argument("--pure-alpha-too", action="store_true",
                    help="also rescan pure alpha 3l (watcher covers these anyway)")

    return p


COMMANDS = {
    "login": cmd_login,
    "whoami": cmd_whoami,
    "check": cmd_check,
    "add": cmd_add,
    "add3l": cmd_add3l,
    "add4l": cmd_add4l,
    "import-namemc": cmd_import_namemc,
    "import-file": cmd_import_file,
    "targets": cmd_targets,
    "rm": cmd_rm,
    "prioritize": cmd_prioritize,
    "resume": cmd_resume,
    "run": cmd_run,
    "snipe": cmd_snipe,
    "hunt": cmd_hunt,
    "scan-free": cmd_scan_free,
    "events": cmd_events,
}


def main():
    args = build_parser().parse_args()
    setup_logging(args.verbose)
    store.init_db()
    cfg = load_config()
    asyncio.run(COMMANDS[args.cmd](cfg, args))


if __name__ == "__main__":
    main()
