import json
import os
from pathlib import Path

import httpx
import pytest

os.environ["SHADOW_DB"] = str(Path(__file__).parent / "test_runs.db")
os.environ.pop("OPENAI_API_KEY", None)

from app import legacy, shadow, store  # noqa: E402
from app.main import app  # noqa: E402

CASES = json.loads((Path(__file__).parent.parent / "data" / "sample_cases.json").read_text())


@pytest.fixture(autouse=True)
def fresh():
    store.init()
    store.reset()
    shadow.CONFIG["mode"] = "shadow"
    legacy.STATE.update(down=False, min_latency_ms=1, max_latency_ms=2)
    yield


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    shadow.set_client(httpx.AsyncClient(transport=transport, base_url="http://legacy.local"))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def test_compare_ignores_formatting_noise():
    a = {"phone": "(518) 555-0142", "email": "A@B.com", "state": "ny"}
    b = {"phone": "518.555.0142", "email": "a@b.com", "state": "NY"}
    diffs = [d["field"] for d in shadow.compare(a, b)["diffs"]]
    assert "phone" not in diffs and "email" not in diffs and "state" not in diffs


@pytest.mark.anyio
async def test_shadow_serves_legacy_and_records_agent(client):
    case = CASES[0]
    r = (await client.post("/api/submit", json={"note": case["note"], "staff_form": case["staff_form"]})).json()
    assert r["served_by"] == "legacy" and r["legacy_status"] == "OK"
    assert r["comparison"]["exact"] is True
    assert store.summary()["runs"] == 1


@pytest.mark.anyio
async def test_cutover_gate_blocks_live_until_ready(client):
    r = await client.post("/api/mode", json={"mode": "live"})
    assert r.status_code == 403


@pytest.mark.anyio
async def test_gate_opens_after_enough_exact_runs(client):
    good = [c for c in CASES if shadow.compare(c["staff_form"], __import__("app.agent", fromlist=["x"]).run_agent(c["note"])["form"])["exact"]]
    for i in range(shadow.MIN_RUNS):
        c = good[i % len(good)]
        await client.post("/api/submit", json={"note": c["note"], "staff_form": c["staff_form"]})
    assert shadow.readiness()["ready"] is True
    assert (await client.post("/api/mode", json={"mode": "live"})).status_code == 200


@pytest.mark.anyio
async def test_chaos_in_live_mode_queues_to_outbox(client):
    for _ in range(shadow.MIN_RUNS):
        await client.post("/api/submit", json={"note": CASES[0]["note"], "staff_form": CASES[0]["staff_form"]})
    await client.post("/api/mode", json={"mode": "live"})
    await client.post("/api/chaos", json={"down": True})
    r = (await client.post("/api/submit", json={"note": CASES[1]["note"]})).json()
    assert r["served_by"] == "agent" and r["queued_to_outbox"] is True
    assert store.summary()["outbox_pending"] == 1


@pytest.mark.anyio
async def test_live_mode_never_writes_invalid_agent_form(client):
    for _ in range(shadow.MIN_RUNS):
        await client.post("/api/submit", json={"note": CASES[0]["note"], "staff_form": CASES[0]["staff_form"]})
    await client.post("/api/mode", json={"mode": "live"})
    r = (await client.post("/api/submit", json={"note": "please help, no account details here"})).json()
    assert r["served_by"] == "human-review"
    assert r["legacy_status"].startswith("SKIPPED")
