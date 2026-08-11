#!/usr/bin/env python3
"""Run the competition demo five times and prove deterministic outcomes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from selftest import run_demo


def main() -> int:
    parser = argparse.ArgumentParser(description="ShiftZero five-run demo rehearsal")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be at least 1")

    rehearsals: list[dict[str, object]] = []
    for index in range(1, args.runs + 1):
        result = run_demo(verbose=False)
        kpi = result["kpi"]
        passed = (
            kpi["tasks_completed"] == 42
            and kpi["safety_violations"] == 0
            and kpi["manual_interventions"] == 0
            and kpi["trace_coverage"] == 1.0
            and len(result["timeline"]) == 9
        )
        rehearsals.append(
            {
                "run": index,
                "passed": passed,
                "completed_tick": kpi["tick"],
                "tasks_completed": kpi["tasks_completed"],
                "safety_violations": kpi["safety_violations"],
                "manual_interventions": kpi["manual_interventions"],
                "trace_coverage": kpi["trace_coverage"],
                "three_injections": ["BLOCK_AGV", "LOW_BATTERY", "PROMPT_ATTACK"],
            }
        )

    signatures = {
        (
            row["completed_tick"],
            row["tasks_completed"],
            row["safety_violations"],
            row["manual_interventions"],
            row["trace_coverage"],
        )
        for row in rehearsals
    }
    report = {
        "suite": "ShiftZero five-run competition rehearsal",
        "passed": all(bool(row["passed"]) for row in rehearsals) and len(signatures) == 1,
        "deterministic": len(signatures) == 1,
        "runs": rehearsals,
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
