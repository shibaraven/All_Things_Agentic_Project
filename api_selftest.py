#!/usr/bin/env python3
"""Executable G1 API vertical-slice acceptance test."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from services.api.app import create_app
from services.api.runtime import ShiftRuntime


TOKEN = "api-selftest-token"
AUTH = {"X-Demo-Token": TOKEN}


def mutation_headers(key: str) -> dict[str, str]:
    return {**AUTH, "Idempotency-Key": key}


def require(condition: bool, detail: str) -> None:
    if not condition:
        raise AssertionError(detail)


def main() -> int:
    runtime = ShiftRuntime(auto_run=False)
    app = create_app(runtime, demo_token=TOKEN)
    checks: list[str] = []

    with TestClient(app) as client:
        health = client.get("/health")
        require(health.status_code == 200 and health.json()["status"] == "ok", "health failed")
        legacy_health = client.get("/healthz")
        require(
            legacy_health.status_code == 200 and legacy_health.json()["status"] == "ok",
            "legacy healthz failed",
        )
        checks.append("health")

        agents = client.get("/api/agents")
        require(agents.status_code == 200, "agent registry endpoint failed")
        require(agents.json()["fleet_size"] == 5, "agent registry did not expose five agents")
        evidence = client.get("/api/evidence/status")
        require(evidence.status_code == 200, "cloud evidence endpoint failed")
        require(
            evidence.json()["cloud_evidence"]["configured"] is False,
            "local API unexpectedly enabled cloud evidence",
        )
        checks.append("agent registry + cloud evidence status")

        objective = {
            "objective": "Complete 42 pallet movements with no safety violations.",
            "target_task_count": 42,
            "deadline_tick": 1080,
            "min_battery_reserve": 25.0,
            "priority_policy": "deadline_then_priority",
            "constraints": ["route_required", "no_double_assignment"],
            "seed": 20260808,
        }
        denied = client.post("/api/shifts", json=objective)
        require(denied.status_code == 403, "mutation endpoint accepted a missing demo token")

        created = client.post(
            "/api/shifts",
            json=objective,
            headers=mutation_headers("create-demo-shift"),
        )
        require(created.status_code == 201, f"create failed: {created.text}")
        created_body = created.json()
        shift_id = created_body["shift_id"]
        require(created_body["shift_state"] == "DRAFT", "new shift was not DRAFT")
        require(len(created_body["agvs"]) == 9, "expected 9 AGVs")

        duplicate_create = client.post(
            "/api/shifts",
            json=objective,
            headers=mutation_headers("create-demo-shift"),
        )
        require(duplicate_create.json() == created_body, "idempotent create response changed")
        conflicting = client.post(
            "/api/shifts",
            json={**objective, "target_task_count": 41},
            headers=mutation_headers("create-demo-shift"),
        )
        require(conflicting.status_code == 409, "idempotency payload conflict was not rejected")
        checks.append("objective + idempotency")

        started = client.post(
            f"/api/shifts/{shift_id}/start",
            headers=mutation_headers("start-demo-shift"),
        )
        require(started.status_code == 200, f"start failed: {started.text}")
        started_body = started.json()
        require(started_body["shift_state"] == "RUNNING", "shift did not enter RUNNING")
        require(
            started_body["mission_plan"]["planner"] == "deterministic-commander-fallback-v1",
            "fallback mission plan missing",
        )
        require(len(started_body["mission_plan"]["task_order"]) == 42, "plan is incomplete")
        checks.append("plan + start")

        advanced = client.post(
            "/api/demo/advance",
            json={"ticks": 10},
            headers=mutation_headers("advance-10"),
        )
        require(advanced.status_code == 200, f"advance failed: {advanced.text}")
        advanced_body = advanced.json()
        require(advanced_body["kpi"]["tick"] == 10, "operational twin did not advance")
        require(advanced_body["kpi"]["actions_executed"] > 0, "no validated action executed")
        duplicate_advance = client.post(
            "/api/demo/advance",
            json={"ticks": 10},
            headers=mutation_headers("advance-10"),
        )
        require(duplicate_advance.json()["kpi"]["tick"] == 10, "advance was executed twice")
        checks.append("kernel → twin")

        read_model = client.get(f"/api/shifts/{shift_id}")
        require(read_model.status_code == 200, "shift read model unavailable")
        require(len(read_model.json()["tasks"]) == 42, "task read model incomplete")
        fleet = client.get("/api/agvs", params={"shift_id": shift_id})
        require(fleet.status_code == 200 and len(fleet.json()["agvs"]) == 9, "fleet read failed")
        checks.append("read model")

        attack = client.post(
            "/api/demo/incidents",
            params={"shift_id": shift_id},
            json={"kind": "PROMPT_ATTACK"},
            headers=mutation_headers("inject-prompt-attack"),
        )
        require(attack.status_code == 200, f"prompt injection failed: {attack.text}")
        attack_detail = attack.json()["detail"]
        require(attack_detail["ingress_blocked"], "ingress screening missed prompt attack")
        require(attack_detail["policy_code"] == "R_FORBIDDEN_ACTION", "kernel did not block attack")
        checks.append("security block")

        events = client.get("/api/events", params={"after": 0, "limit": 1000}).json()["events"]
        event_types = {event["event_type"] for event in events}
        require("plan.created" in event_types, "plan.created event missing")
        require("action.executed" in event_types, "execution event missing")
        require("security.blocked" in event_types, "security event missing")
        executed_event = next(event for event in events if event["event_type"] == "action.executed")
        action_id = executed_event["payload"]["correlation_id"]
        action_trace = client.get(f"/api/actions/{action_id}/trace")
        require(action_trace.status_code == 200, "action trace unavailable")
        trace_body = action_trace.json()
        require(trace_body["proposal"] is not None, "trace is missing proposal")
        require(trace_body["policy_decision"]["decision"] == "APPROVED", "trace is missing policy decision")
        require(trace_body["ticket"] is not None, "trace is missing ticket")
        require(trace_body["execution_result"]["status"] == "EXECUTED", "trace is missing result")
        sse = client.get("/api/events/stream", params={"after": 0, "once": "true"})
        require(sse.status_code == 200, "SSE replay failed")
        require(sse.headers["content-type"].startswith("text/event-stream"), "wrong SSE media type")
        require("event: plan.created" in sse.text, "SSE did not contain plan event")
        checks.append("events + trace + SSE")

        paused = client.post(
            f"/api/shifts/{shift_id}/pause",
            headers=mutation_headers("pause-demo-shift"),
        )
        require(paused.json()["shift_state"] == "PAUSED", "pause failed")
        blocked_advance = client.post(
            "/api/demo/advance",
            json={"ticks": 1},
            headers=mutation_headers("advance-while-paused"),
        )
        require(blocked_advance.status_code == 409, "paused shift still advanced")
        resumed = client.post(
            f"/api/shifts/{shift_id}/resume",
            headers=mutation_headers("resume-demo-shift"),
        )
        require(resumed.json()["shift_state"] in ("RUNNING", "RECOVERING"), "resume failed")
        checks.append("pause/resume")

        completed = client.post(
            "/api/demo/advance",
            json={"ticks": 1080},
            headers=mutation_headers("advance-to-complete"),
        )
        require(completed.status_code == 200, f"completion advance failed: {completed.text}")
        completed_body = completed.json()
        require(completed_body["shift_state"] == "COMPLETED", "shift did not complete")
        require(completed_body["kpi"]["tasks_completed"] == 42, "not all tasks completed")
        require(completed_body["kpi"]["safety_violations"] == 0, "safety violation detected")
        checks.append("42/42 completion")

        reset = client.post(
            "/api/demo/reset",
            params={"shift_id": shift_id},
            headers=mutation_headers("reset-demo-shift"),
        )
        require(reset.status_code == 200, f"reset failed: {reset.text}")
        reset_body = reset.json()
        require(reset_body["shift_state"] == "DRAFT", "reset did not return to DRAFT")
        require(reset_body["kpi"]["tick"] == 0, "reset did not restore tick zero")
        require(reset_body["snapshot_hash"] == created_body["snapshot_hash"], "fixed-seed reset drifted")
        checks.append("deterministic reset")

    print(
        json.dumps(
            {
                "passed": len(checks),
                "checks": checks,
                "shift_id": shift_id,
                "completed_tick": completed_body["kpi"]["tick"],
                "tasks_completed": completed_body["kpi"]["tasks_completed"],
                "safety_violations": completed_body["kpi"]["safety_violations"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
