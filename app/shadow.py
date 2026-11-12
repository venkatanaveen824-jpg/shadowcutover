"""Shadow proxy: the core of ShadowCutover.

Modes
  shadow : legacy path serves the user; the agent runs on the same request in parallel
           and only its intended result is recorded and compared. Zero user impact.
  canary : CANARY_PCT of requests use the agent's result as the write; the rest stay shadow.
  live   : the agent's result is written to the legacy system of record.

Canary and live are locked until the cutover readiness gate passes.
"""
import asyncio
import os
import random
import time
import uuid

import httpx

from . import store
from .agent import run_agent
from .fields import SCORED_FIELDS, normalize
from .legacy import build_envelope

MIN_RUNS = int(os.getenv("CUTOVER_MIN_RUNS", "10"))
THRESHOLD = float(os.getenv("CUTOVER_THRESHOLD", "98"))
CANARY_PCT = float(os.getenv("CANARY_PCT", "10"))
MAX_RETRIES = 3

CONFIG = {"mode": "shadow"}
_client: httpx.AsyncClient | None = None


def set_client(client: httpx.AsyncClient):
    global _client
    _client = client


def readiness() -> dict:
    s = store.summary(window=max(MIN_RUNS, 50))
    reasons = []
    if s["runs"] < MIN_RUNS:
        reasons.append(f"need {MIN_RUNS} shadow runs, have {s['runs']}")
    if s["exact_match_rate"] < THRESHOLD:
        reasons.append(f"exact match {s['exact_match_rate']}% is below {THRESHOLD}%")
    return {"ready": not reasons, "reasons": reasons, "threshold": THRESHOLD, "min_runs": MIN_RUNS, **s}


def set_mode(mode: str) -> dict:
    if mode not in ("shadow", "canary", "live"):
        raise ValueError("mode must be shadow, canary or live")
    if mode != "shadow":
        r = readiness()
        if not r["ready"]:
            raise PermissionError("Cutover gate not passed: " + "; ".join(r["reasons"]))
    CONFIG["mode"] = mode
    return {"mode": mode}


def compare(legacy_form: dict, agent_form: dict) -> dict:
    diffs = []
    for f in SCORED_FIELDS:
        if normalize(f, legacy_form.get(f)) != normalize(f, agent_form.get(f)):
            diffs.append({"field": f, "legacy": legacy_form.get(f, ""), "agent": agent_form.get(f, "")})
    scored = len(SCORED_FIELDS)
    matched = scored - len(diffs)
    return {"matched": matched, "scored": scored, "field_match_pct": round(100 * matched / scored, 1),
            "exact": not diffs, "diffs": diffs}


async def _legacy_write(form: dict, correlation_id: str) -> tuple[str, float]:
    t0 = time.perf_counter()
    try:
        r = await _client.post(
            "/legacy/soap",
            content=build_envelope(form),
            headers={"Content-Type": "text/xml", "SOAPAction": "UpdateAccount", "X-Correlation-ID": correlation_id},
            timeout=10,
        )
        status = "OK" if r.status_code == 200 else f"HTTP {r.status_code}"
    except httpx.HTTPError as exc:
        status = f"ERROR {exc.__class__.__name__}"
    return status, round((time.perf_counter() - t0) * 1000, 1)


async def _write_with_retry(form: dict, correlation_id: str) -> tuple[str, float, int]:
    delay, total_ms = 0.2, 0.0
    for attempt in range(1, MAX_RETRIES + 1):
        status, ms = await _legacy_write(form, correlation_id)
        total_ms += ms
        if status == "OK":
            return status, total_ms, attempt
        await asyncio.sleep(delay)
        delay *= 2
    return status, total_ms, MAX_RETRIES


async def submit(note: str, staff_form: dict | None) -> dict:
    correlation_id = str(uuid.uuid4())
    mode = CONFIG["mode"]
    route = mode
    if mode == "canary":
        route = "live" if random.uniform(0, 100) < CANARY_PCT else "shadow"

    agent_task = asyncio.to_thread(run_agent, note)

    if route == "shadow":
        if staff_form is None:
            raise ValueError("shadow mode needs the staff-entered legacy form")
        (legacy_status, legacy_ms), agent = await asyncio.gather(_legacy_write(staff_form, correlation_id), agent_task)
        served_by = "legacy"
        queued = False
    else:
        agent = await agent_task
        if agent["issues"]:
            # never write a form that failed validation; fall back to the human path
            legacy_status, legacy_ms, served_by, queued = "SKIPPED (agent validation failed)", 0.0, "human-review", False
        else:
            legacy_status, legacy_ms, attempts = await _write_with_retry(agent["form"], correlation_id)
            served_by, queued = "agent", False
            if legacy_status != "OK":
                store.queue_outbox(correlation_id, agent["form"], attempts)
                queued = True

    cmp = compare(staff_form, agent["form"]) if staff_form else {
        "matched": 0, "scored": 0, "field_match_pct": 0.0, "exact": False, "diffs": []}

    store.record_run({
        "correlation_id": correlation_id, "route": route, "legacy_status": legacy_status, "legacy_ms": legacy_ms,
        "agent_ms": agent["latency_ms"], "agent_cost": agent["cost_usd"], "agent_mode": agent["mode"],
        **cmp, "issues": agent["issues"],
    })

    return {
        "correlation_id": correlation_id,
        "mode": mode,
        "route": route,
        "served_by": served_by,
        "legacy_status": legacy_status,
        "legacy_ms": legacy_ms,
        "queued_to_outbox": queued,
        "agent": agent,
        "comparison": cmp,
    }
