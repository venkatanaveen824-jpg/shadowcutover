"""Replay the sample cases against a running ShadowCutover server and print the shadow report.

Usage:
    python scripts/replay.py --url http://localhost:7860 --rounds 2
"""
import argparse
import json
from pathlib import Path

import httpx

CASES = json.loads((Path(__file__).parent.parent / "data" / "sample_cases.json").read_text())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:7860")
    ap.add_argument("--rounds", type=int, default=1)
    args = ap.parse_args()

    with httpx.Client(base_url=args.url, timeout=30) as c:
        for _ in range(args.rounds):
            for case in CASES:
                r = c.post("/api/submit", json={"note": case["note"], "staff_form": case["staff_form"]}).json()
                cmp = r["comparison"]
                fields = ", ".join(d["field"] for d in cmp["diffs"]) or "none"
                print(f"{case['id']:<8} match {cmp['field_match_pct']:>5}%  legacy {r['legacy_ms']:>7} ms  "
                      f"agent {r['agent']['latency_ms']:>7} ms  diffs: {fields}")
        s = c.get("/api/status").json()["readiness"]
        print("\nShadow report")
        for k in ("runs", "exact_match_rate", "avg_field_match_pct", "p95_legacy_ms", "p95_agent_ms", "total_agent_cost"):
            print(f"  {k:<22} {s[k]}")
        print(f"  cutover ready          {s['ready']}  {'; '.join(s['reasons'])}")


if __name__ == "__main__":
    main()
