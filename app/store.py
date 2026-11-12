"""SQLite flight recorder for every shadow run and every live write attempt."""
import json
import os
import sqlite3
import threading
import time

DB_PATH = os.getenv("SHADOW_DB", "shadow_runs.db")
_lock = threading.Lock()


def _conn():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _lock, _conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, correlation_id TEXT, route TEXT,
                legacy_status TEXT, legacy_ms REAL,
                agent_ms REAL, agent_cost REAL, agent_mode TEXT,
                matched INTEGER, scored INTEGER, field_match_pct REAL, exact INTEGER,
                diffs TEXT, issues TEXT)"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, correlation_id TEXT, payload TEXT, attempts INTEGER, status TEXT)"""
        )


def record_run(row: dict):
    with _lock, _conn() as c:
        c.execute(
            """INSERT INTO runs (ts, correlation_id, route, legacy_status, legacy_ms, agent_ms, agent_cost,
               agent_mode, matched, scored, field_match_pct, exact, diffs, issues)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                time.time(), row["correlation_id"], row["route"], row["legacy_status"], row["legacy_ms"],
                row["agent_ms"], row["agent_cost"], row["agent_mode"], row["matched"], row["scored"],
                row["field_match_pct"], int(row["exact"]), json.dumps(row["diffs"]), json.dumps(row["issues"]),
            ),
        )


def queue_outbox(correlation_id: str, payload: dict, attempts: int):
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO outbox (ts, correlation_id, payload, attempts, status) VALUES (?,?,?,?, 'pending')",
            (time.time(), correlation_id, json.dumps(payload), attempts),
        )


def recent(limit: int = 20) -> list[dict]:
    with _lock, _conn() as c:
        rows = c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["diffs"], d["issues"] = json.loads(d["diffs"]), json.loads(d["issues"])
        out.append(d)
    return out


def summary(window: int = 50) -> dict:
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT legacy_ms, agent_ms, agent_cost, field_match_pct, exact, legacy_status FROM runs "
            "ORDER BY id DESC LIMIT ?",
            (window,),
        ).fetchall()
        pending = c.execute("SELECT COUNT(*) FROM outbox WHERE status='pending'").fetchone()[0]
    n = len(rows)
    if not n:
        return {"runs": 0, "exact_match_rate": 0, "avg_field_match_pct": 0, "p95_legacy_ms": 0,
                "p95_agent_ms": 0, "total_agent_cost": 0, "legacy_errors": 0, "outbox_pending": pending}

    def p95(vals):
        vals = sorted(v for v in vals if v is not None)
        return round(vals[min(len(vals) - 1, int(0.95 * len(vals)))], 1) if vals else 0

    return {
        "runs": n,
        "exact_match_rate": round(100 * sum(r["exact"] for r in rows) / n, 1),
        "avg_field_match_pct": round(sum(r["field_match_pct"] for r in rows) / n, 1),
        "p95_legacy_ms": p95([r["legacy_ms"] for r in rows]),
        "p95_agent_ms": p95([r["agent_ms"] for r in rows]),
        "total_agent_cost": round(sum(r["agent_cost"] for r in rows), 4),
        "legacy_errors": sum(1 for r in rows if r["legacy_status"] != "OK"),
        "outbox_pending": pending,
    }


def reset():
    with _lock, _conn() as c:
        c.execute("DELETE FROM runs")
        c.execute("DELETE FROM outbox")
