"""ShadowCutover: run an AI agent in shadow mode next to a legacy system, measure it,
and only allow cutover once it has earned it."""
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import legacy, shadow, store

BASE = Path(__file__).parent
SAMPLES = json.loads((BASE.parent / "data" / "sample_cases.json").read_text())


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.init()
    legacy_url = os.getenv("LEGACY_URL")
    if legacy_url:  # point at a real legacy SOAP host
        client = httpx.AsyncClient(base_url=legacy_url)
    else:  # in-process mock legacy system
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://legacy.local")
    shadow.set_client(client)
    yield
    await client.aclose()


app = FastAPI(title="ShadowCutover", version="0.1.0", lifespan=lifespan)
app.include_router(legacy.router)


class SubmitRequest(BaseModel):
    note: str
    staff_form: dict | None = None


class ModeRequest(BaseModel):
    mode: str


class ChaosRequest(BaseModel):
    down: bool


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")


@app.get("/api/samples")
def samples():
    return SAMPLES


@app.post("/api/submit")
async def submit(req: SubmitRequest):
    try:
        return await shadow.submit(req.note, req.staff_form)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/status")
def status():
    return {"mode": shadow.CONFIG["mode"], "legacy_down": legacy.STATE["down"], "readiness": shadow.readiness(),
            "recent": store.recent(15)}


@app.post("/api/mode")
def mode(req: ModeRequest):
    try:
        return shadow.set_mode(req.mode)
    except PermissionError as exc:
        raise HTTPException(403, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/chaos")
def chaos(req: ChaosRequest):
    legacy.STATE["down"] = req.down
    return {"legacy_down": req.down}


@app.post("/api/reset")
def reset():
    store.reset()
    shadow.CONFIG["mode"] = "shadow"
    legacy.STATE["down"] = False
    return {"ok": True}
