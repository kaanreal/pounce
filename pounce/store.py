"""sqlite storage for targets and events. plain sqlite3, WAL mode."""
import sqlite3
import threading
from contextlib import contextmanager

from .common import DB_PATH, now

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS targets(
  name TEXT PRIMARY KEY,
  priority INTEGER DEFAULT 0,
  source TEXT DEFAULT 'manual',
  state TEXT DEFAULT 'watching',
  owner_uuid TEXT,
  last_checked REAL,
  last_change_at REAL,
  drop_lo REAL,
  drop_hi REAL,
  droptime REAL,
  note TEXT,
  present INTEGER DEFAULT 0,
  last_seen REAL,
  last_present REAL
);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  kind TEXT NOT NULL,
  name TEXT,
  detail TEXT
);
CREATE TABLE IF NOT EXISTS kv(
  key TEXT PRIMARY KEY,
  value TEXT
);
"""


def _migrate(c):
    """add new columns to tables created by older schema versions"""
    cols = {r["name"] for r in c.execute("PRAGMA table_info(targets)")}
    for name, ddl in {
        "present": "ALTER TABLE targets ADD COLUMN present INTEGER DEFAULT 0",
        "last_seen": "ALTER TABLE targets ADD COLUMN last_seen REAL",
        "last_present": "ALTER TABLE targets ADD COLUMN last_present REAL",
    }.items():
        if name not in cols:
            c.execute(ddl)
    # existing rows with a known owner count as currently present
    c.execute(
        "UPDATE targets SET present=1 WHERE present=0 "
        "AND owner_uuid IS NOT NULL AND owner_uuid != ''"
    )


@contextmanager
def conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        c = sqlite3.connect(DB_PATH, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            c.execute("PRAGMA journal_mode=WAL")
            yield c
            c.commit()
        finally:
            c.close()


def init_db():
    with conn() as c:
        c.executescript(SCHEMA)
        _migrate(c)


def upsert_target(name, priority=None, source=None, droptime=None, note=None):
    with conn() as c:
        row = c.execute("SELECT * FROM targets WHERE name=?", (name,)).fetchone()
        if row is None:
            c.execute(
                "INSERT INTO targets(name, priority, source, droptime, note) VALUES(?,?,?,?,?)",
                (name, priority or 0, source or "manual", droptime, note),
            )
        else:
            sets, args = [], []
            if priority is not None:
                sets.append("priority=?"); args.append(priority)
            if source is not None:
                sets.append("source=?"); args.append(source)
            if droptime is not None:
                sets.append("droptime=?"); args.append(droptime)
            if note is not None:
                sets.append("note=?"); args.append(note)
            if sets:
                args.append(name)
                c.execute(f"UPDATE targets SET {', '.join(sets)} WHERE name=?", args)


def prioritize_all():
    with conn() as c:
        cur = c.execute("UPDATE targets SET priority=1 WHERE priority=0")
        return cur.rowcount


def bulk_add(names, priority, source):
    with conn() as c:
        c.executemany(
            "INSERT OR IGNORE INTO targets(name, priority, source) VALUES(?,?,?)",
            [(n, priority, source) for n in names],
        )


def count_by_state():
    with conn() as c:
        return {r["state"]: r["n"] for r in c.execute(
            "SELECT state, COUNT(*) AS n FROM targets GROUP BY state")}


def get_target(name):
    with conn() as c:
        row = c.execute("SELECT * FROM targets WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None


def remove_target(name):
    with conn() as c:
        c.execute("DELETE FROM targets WHERE name=?", (name,))


def list_targets(state=None):
    q = "SELECT * FROM targets"
    args = ()
    if state:
        q += " WHERE state=?"
        args = (state,)
    q += " ORDER BY priority DESC, name"
    with conn() as c:
        return [dict(r) for r in c.execute(q, args).fetchall()]


def next_due_target():
    """oldest-checked watching target, never-checked ones first"""
    with conn() as c:
        row = c.execute(
            "SELECT * FROM targets WHERE state IN ('watching','missed') "
            "ORDER BY (last_checked IS NULL) DESC, last_checked ASC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def mark_checked(name, owner_uuid, state="watching"):
    with conn() as c:
        c.execute(
            "UPDATE targets SET owner_uuid=?, state=?, last_checked=?, last_change_at=last_change_at WHERE name=?",
            (owner_uuid, state, now(), name),
        )


def set_cooldown(name, lo, hi):
    with conn() as c:
        c.execute(
            "UPDATE targets SET state='cooldown', last_checked=?, last_change_at=?, drop_lo=?, drop_hi=? WHERE name=?",
            (now(), now(), lo, hi, name),
        )


def mark_batch(name, present, owner_uuid, ts):
    """batch sweep bookkeeping: presence flag + last sample timestamps.
    present=0 clears owner_uuid so a future re-free is not double-fired."""
    with conn() as c:
        c.execute(
            "UPDATE targets SET present=?, last_seen=?, "
            "last_present=CASE WHEN ?=1 THEN ? ELSE last_present END, "
            "owner_uuid=?, state='watching', last_checked=? WHERE name=?",
            (1 if present else 0, ts, 1 if present else 0,
             ts if present else 0, owner_uuid, ts, name),
        )


def due_targets(n):
    """oldest-checked watching targets, never-checked ones first, up to n"""
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM targets WHERE state IN ('watching','missed') "
            "ORDER BY (last_checked IS NULL) DESC, priority DESC, last_checked ASC LIMIT ?",
            (n,),
        ).fetchall()
        return [dict(r) for r in rows]


def set_state(name, state):
    with conn() as c:
        c.execute("UPDATE targets SET state=?, last_checked=? WHERE name=?", (state, now(), name))


def set_droptime(name, droptime):
    with conn() as c:
        c.execute("UPDATE targets SET droptime=? WHERE name=?", (droptime, name))


def pending_hunts(pre_margin_s):
    """cooldown targets whose hunt window is opening"""
    t = now()
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM targets WHERE state='cooldown' AND ? >= drop_lo - ? ORDER BY drop_lo ASC",
            (t, pre_margin_s),
        ).fetchall()
        return [dict(r) for r in rows]


def upcoming_timed(horizon_s):
    """targets with a known exact droptime inside the horizon"""
    t = now()
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM targets WHERE droptime IS NOT NULL AND state NOT IN ('claimed','dead') "
            "AND droptime BETWEEN ? AND ? ORDER BY droptime ASC",
            (t, t + horizon_s),
        ).fetchall()
        return [dict(r) for r in rows]


def event(kind, name=None, detail=None):
    with conn() as c:
        c.execute("INSERT INTO events(ts, kind, name, detail) VALUES(?,?,?,?)", (now(), kind, name, detail))


def recent_events(limit=30):
    with conn() as c:
        rows = c.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def kv_get(key, default=None):
    with conn() as c:
        row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def kv_set(key, value):
    with conn() as c:
        c.execute("INSERT INTO kv(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
