"""AI agent that turns a staff case note into the 15-field legacy form.

Built as a small LangGraph graph: extract -> validate. Extraction uses an LLM when
OPENAI_API_KEY is set; otherwise it falls back to a deterministic rule-based
extractor so the demo runs anywhere without keys or cost.
"""
import json
import os
import re
import time
from typing import TypedDict

from langgraph.graph import END, StateGraph

from .fields import FORM_FIELDS

PRICE_PER_1K_TOKENS = float(os.getenv("PRICE_PER_1K_TOKENS", "0.005"))
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

STATE_CODES = {"new york": "NY", "texas": "TX", "kansas": "KS", "illinois": "IL", "california": "CA"}


class AgentState(TypedDict, total=False):
    note: str
    form: dict
    issues: list
    tokens: int
    mode: str


# ---------------- extraction ----------------

def _rule_extract(note: str) -> dict:
    """Deterministic extractor for the sample note style. Deliberately not perfect,
    so the shadow comparison shows real mismatches to work on."""
    f = {k: "" for k in FORM_FIELDS}
    n = " ".join(note.split())

    m = re.search(r"\b(ACC-\d{4,6})\b", n)
    f["account_id"] = m.group(1) if m else ""

    m = re.search(r"for ([A-Z][a-z]+) ([A-Z][a-zA-Z'\-]+)", n)
    if m:
        f["first_name"], f["last_name"] = m.group(1), m.group(2)

    m = re.search(r"DOB (\d{4}-\d{2}-\d{2})", n)
    f["date_of_birth"] = m.group(1) if m else ""

    m = re.search(r"(\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]\d{4})", n)
    f["phone"] = m.group(1) if m else ""

    m = re.search(r"([\w.+\-]+@[\w\-]+\.[\w.]+)", n)
    f["email"] = m.group(1).rstrip(".") if m else ""

    m = re.search(r"address (?:is |to )?(\d+ [\w .]+?), ([A-Za-z .]+), ([A-Z]{2}|[A-Za-z ]+?) (\d{5})", n)
    if m:
        f["street"], f["city"] = m.group(1).strip(), m.group(2).strip()
        st = m.group(3).strip()
        f["state"] = st if len(st) == 2 else STATE_CODES.get(st.lower(), st)
        f["zip"] = m.group(4)

    low = n.lower()
    if "paper" in low or "postal" in low:
        f["mail_preference"] = "paper"
    elif "email" in low and ("prefers email" in low or "by email" in low or "email only" in low):
        f["mail_preference"] = "email"

    m = re.search(r"language (?:is )?([A-Z][a-z]+)", n) or re.search(r"speaks ([A-Z][a-z]+)", n)
    f["preferred_language"] = m.group(1) if m else "English"

    if "address" in low and ("moved" in low or "new address" in low or "change address" in low):
        f["request_type"] = "address_change"
    elif "phone" in low and "update" in low:
        f["request_type"] = "contact_update"
    else:
        f["request_type"] = "general_update"

    m = re.search(r"effective (\d{4}-\d{2}-\d{2})", n)
    f["effective_date"] = m.group(1) if m else ""

    m = re.search(r"Note: (.+)$", n)
    f["notes"] = m.group(1).strip() if m else ""
    return f


def _llm_extract(note: str) -> tuple[dict, int]:
    from openai import OpenAI  # optional dependency

    client = OpenAI()
    prompt = (
        "Extract an account update form from the staff note. Return JSON with exactly these keys: "
        + ", ".join(FORM_FIELDS)
        + ". Use ISO dates (YYYY-MM-DD), 2-letter US state codes, request_type one of "
        "address_change|contact_update|general_update, mail_preference email|paper. "
        "Use empty string when a value is not in the note. Do not guess."
    )
    resp = client.chat.completions.create(
        model=MODEL,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": prompt}, {"role": "user", "content": note}],
        temperature=0,
    )
    data = json.loads(resp.choices[0].message.content)
    form = {k: str(data.get(k, "") or "") for k in FORM_FIELDS}
    return form, resp.usage.total_tokens if resp.usage else 0


def extract_node(state: AgentState) -> AgentState:
    if os.getenv("OPENAI_API_KEY"):
        try:
            form, tokens = _llm_extract(state["note"])
            return {"form": form, "tokens": tokens, "mode": "llm"}
        except Exception as exc:  # fall back rather than fail the shadow run
            return {"form": _rule_extract(state["note"]), "tokens": 0, "mode": f"rules (llm error: {exc.__class__.__name__})"}
    return {"form": _rule_extract(state["note"]), "tokens": 0, "mode": "rules"}


def validate_node(state: AgentState) -> AgentState:
    form, issues = state["form"], []
    for key in ("account_id", "last_name", "request_type"):
        if not form.get(key):
            issues.append(f"missing {key}")
    for key in ("date_of_birth", "effective_date"):
        v = form.get(key, "")
        if v and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            issues.append(f"{key} not ISO date")
    if form.get("zip") and not re.fullmatch(r"\d{5}", form["zip"]):
        issues.append("zip not 5 digits")
    return {"issues": issues}


def build_graph():
    g = StateGraph(AgentState)
    g.add_node("extract", extract_node)
    g.add_node("validate", validate_node)
    g.set_entry_point("extract")
    g.add_edge("extract", "validate")
    g.add_edge("validate", END)
    return g.compile()


GRAPH = build_graph()


def run_agent(note: str) -> dict:
    t0 = time.perf_counter()
    out = GRAPH.invoke({"note": note})
    ms = (time.perf_counter() - t0) * 1000
    tokens = out.get("tokens", 0)
    return {
        "form": out["form"],
        "issues": out.get("issues", []),
        "latency_ms": round(ms, 1),
        "tokens": tokens,
        "cost_usd": round(tokens / 1000 * PRICE_PER_1K_TOKENS, 6),
        "mode": out.get("mode", "rules"),
    }
