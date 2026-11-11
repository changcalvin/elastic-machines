"""SQLite: routing state per conversation and a metadata log per request. Never prompt text."""
import json
import os
import sqlite3
import time

from .tuning import DEFAULT_RUNG, TARGET, TIGHTEN_AT, TOP_RUNG, Segment, wilson

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    key TEXT PRIMARY KEY,
    tier INTEGER NOT NULL,
    task TEXT,
    rung INTEGER,  -- the strictness it was routed under
    last_prompt TEXT,  -- hash of the last prompt that got an answer, so retries aren't re-asks
    last_at REAL
);
CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY,
    at REAL NOT NULL,
    team TEXT NOT NULL,
    task TEXT,
    tier TEXT NOT NULL,
    requested_model TEXT NOT NULL,
    rung INTEGER,
    verdict TEXT NOT NULL,  -- worse, same, better, no_reference or failed
    shadow_usd REAL NOT NULL,
    judge_usd REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS segments (
    name TEXT PRIMARY KEY,  -- task:code, team:platform
    rung INTEGER NOT NULL,
    tighten REAL NOT NULL,
    loosen REAL NOT NULL,
    checks INTEGER NOT NULL,
    worse INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS moves (
    id INTEGER PRIMARY KEY,
    at REAL NOT NULL,
    segment TEXT NOT NULL,
    from_rung INTEGER NOT NULL,
    to_rung INTEGER NOT NULL,
    checks INTEGER NOT NULL,
    worse INTEGER NOT NULL,
    check_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY,
    at REAL NOT NULL,
    team TEXT NOT NULL,
    session TEXT,  -- the client's session id, so a chat can look up its own costs
    conversation TEXT,
    requested_model TEXT NOT NULL,
    model TEXT NOT NULL,
    tier TEXT,
    route TEXT NOT NULL,  -- routed, sticky, escalated, round_up, retried or passthrough
    answers TEXT,  -- Jev's answers as JSON, for replay and calibration
    router_ms REAL,
    router_usd REAL,
    status INTEGER,
    stop_reason TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    cost_usd REAL,
    baseline_usd REAL
);
"""

# ponytail: blocking calls on the event loop, sub-millisecond in WAL mode. Move to a writer thread if p99 suffers.
db = sqlite3.connect(os.environ.get("DB_PATH", "gateway.db"), check_same_thread=False, isolation_level=None)
db.row_factory = sqlite3.Row
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA synchronous=NORMAL")
db.executescript(SCHEMA)
if "session" not in {column["name"] for column in db.execute("PRAGMA table_info(requests)")}:
    db.execute("ALTER TABLE requests ADD COLUMN session TEXT")  # databases made before sessions were logged
db.execute("CREATE INDEX IF NOT EXISTS requests_by_session ON requests (session)")


def conversation(key: str) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM conversations WHERE key = ?", (key,)).fetchone()


def add_conversation(key: str, tier: int, task: str | None = None, rung: int | None = None) -> None:
    db.execute("INSERT OR IGNORE INTO conversations (key, tier, task, rung) VALUES (?, ?, ?, ?)", (key, tier, task, rung))


def set_tier(key: str, tier: int) -> None:
    db.execute("UPDATE conversations SET tier = ? WHERE key = ?", (tier, key))


def answered(key: str, prompt: str) -> None:
    db.execute("UPDATE conversations SET last_prompt = ?, last_at = ? WHERE key = ?", (prompt, time.time(), key))


def insert(table: str, row: dict) -> int:
    row = {**row, "at": time.time()}
    columns = ", ".join(row)
    values = ", ".join(f":{name}" for name in row)
    return db.execute(f"INSERT INTO {table} ({columns}) VALUES ({values})", row).lastrowid


def log(**row) -> None:
    row["answers"] = json.dumps(row["answers"]) if row.get("answers") else None
    insert("requests", row)


def log_check(**row) -> int:
    return insert("checks", row)


def spent_today() -> float:
    day_ago = time.time() - 86400
    return db.execute(
        "SELECT COALESCE(SUM(shadow_usd + judge_usd), 0) FROM checks WHERE at > ?", (day_ago,)
    ).fetchone()[0]


def segment(name: str) -> Segment:
    row = db.execute("SELECT rung, tighten, loosen, checks, worse FROM segments WHERE name = ?", (name,)).fetchone()
    return Segment(*row) if row else Segment()


def current_rung(task: str, team: str) -> int:
    return max(segment(f"task:{task}").rung, segment(f"team:{team}").rung)


def save_segment(name: str, segment: Segment) -> None:
    db.execute("INSERT OR REPLACE INTO segments VALUES (?, ?, ?, ?, ?, ?)", (name, *segment))


def clear_team_evidence() -> None:
    db.execute("UPDATE segments SET tighten = 0, loosen = 0 WHERE name LIKE 'team:%'")


def log_move(name: str, before: Segment, after: Segment, worse: bool, check_id: int) -> None:
    db.execute(
        "INSERT INTO moves (at, segment, from_rung, to_rung, checks, worse, check_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (time.time(), name, before.rung, after.rung, before.checks + 1, before.worse + worse, check_id),
    )


TOTALS = """
SELECT team,
    COUNT(*) AS requests,
    COALESCE(SUM(cost_usd), 0) AS spend_usd,
    COALESCE(SUM(baseline_usd), 0) AS baseline_usd,
    COALESCE(SUM(router_usd), 0) AS router_usd,
    COALESCE(SUM(cost_usd IS NULL AND status = 200), 0) AS unpriced,
    COUNT(DISTINCT CASE WHEN tier IS NOT NULL THEN conversation END) AS conversations,
    COUNT(DISTINCT CASE WHEN route = 'escalated' THEN conversation END) AS escalated
FROM requests
"""


def summary() -> dict:
    mix: dict[str, dict[str, int]] = {}
    for row in db.execute("SELECT team, tier, COUNT(*) AS n FROM requests WHERE tier IS NOT NULL GROUP BY team, tier"):
        mix.setdefault(row["team"], {})[row["tier"]] = row["n"]
    checks = dict(db.execute("SELECT team, SUM(shadow_usd + judge_usd) FROM checks GROUP BY team").fetchall())
    teams = db.execute(TOTALS + " GROUP BY team ORDER BY spend_usd DESC")
    return {
        "total": {**db.execute(TOTALS).fetchone(), "checks_usd": sum(checks.values())},
        "teams": [
            {**row, "tiers": mix.get(row["team"], {}), "checks_usd": checks.get(row["team"], 0.0)}
            for row in map(dict, teams)
        ],
    }


QUALITY_WINDOW = 500  # checks behind the headline estimate
ROLLING_CHECKS = 50  # checks behind each point on the trend
MAX_POINTS = 240
JUDGED = "verdict IN ('worse', 'same', 'better')"


def estimate(worse: int, checks: int) -> dict:
    low, high = wilson(worse, checks)
    return {"checks": checks, "worse": worse, "quality": 1 - worse / checks if checks else None, "low": 1 - high, "high": 1 - low}


def quality() -> dict:
    """The live estimate, each segment's strictness, the rolling trend and the moves on it."""
    rows = db.execute(f"SELECT id, team, task, verdict FROM checks WHERE {JUDGED} ORDER BY id").fetchall()
    recent = rows[-QUALITY_WINDOW:]
    is_worse = [row["verdict"] == "worse" for row in rows]

    step = max(1, len(rows) // MAX_POINTS)
    first = max(1, min(ROLLING_CHECKS, len(rows)))
    trend = [  # counted back from the newest check, which downsampling must never drop
        {"check": end, **estimate(sum(is_worse[max(0, end - ROLLING_CHECKS) : end]), min(end, ROLLING_CHECKS))}
        for end in reversed(range(len(rows), first - 1, -step))
    ]

    position = {row["id"]: index + 1 for index, row in enumerate(rows)}
    moves = [
        {**row, "position": position.get(row["check_id"])}
        for row in map(dict, db.execute("SELECT * FROM moves ORDER BY id"))
    ]

    segments = []
    for row in db.execute("SELECT name, rung, tighten FROM segments ORDER BY name LIKE 'team:%', rung DESC, name"):
        kind, _, name = row["name"].partition(":")
        matching = [r for r in recent if r[kind] == name]
        segments.append({
            "kind": kind,
            "name": name,
            "rung": row["rung"],
            "default_rung": DEFAULT_RUNG,
            "at_limit": row["rung"] == TOP_RUNG and row["tighten"] >= TIGHTEN_AT,
            **estimate(sum(r["verdict"] == "worse" for r in matching), len(matching)),
        })

    return {
        "target": TARGET,
        "rolling": ROLLING_CHECKS,
        "overall": estimate(sum(r["verdict"] == "worse" for r in recent), len(recent)),
        "segments": segments,
        "trend": trend,
        "moves": moves,
        "spent_today_usd": spent_today(),
    }


REQUEST_COLUMNS = """id, at, team, requested_model, model, tier, route, status, stop_reason,
    input_tokens, output_tokens, cache_read_tokens, cost_usd, baseline_usd, router_usd, router_ms"""


def recent(limit: int) -> list[dict]:
    rows = db.execute(f"SELECT {REQUEST_COLUMNS} FROM requests ORDER BY id DESC LIMIT ?", (limit,))
    return [dict(row) for row in rows]


def session_requests(team: str, session: str) -> list[dict]:
    rows = db.execute(
        f"SELECT {REQUEST_COLUMNS} FROM requests WHERE session = ? AND team = ? ORDER BY id", (session, team)
    )
    return [dict(row) for row in rows]
