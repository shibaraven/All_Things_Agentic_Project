"""ShiftZero FastAPI G1 vertical slice."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from copy import deepcopy
from typing import Any, Awaitable, Callable, Literal, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field

from shiftzero_core.contracts import stable_hash

from .runtime import InvalidTransitionError, ShiftNotFoundError, ShiftRuntime


LOCAL_DEMO_TOKEN = "shiftzero-local-demo"


class ShiftCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective: str = Field(
        default="Complete 42 pallet movements before the deadline while preserving safety and battery reserve.",
        min_length=10,
        max_length=500,
    )
    target_task_count: int = Field(default=42, ge=1, le=500)
    deadline_tick: int = Field(default=1080, ge=1, le=100_000)
    min_battery_reserve: float = Field(default=25.0, ge=5.0, le=80.0)
    priority_policy: str = Field(default="deadline_then_priority", min_length=3, max_length=100)
    constraints: list[str] = Field(
        default_factory=lambda: ["no_double_assignment", "route_required", "emergency_stop_wins"],
        max_length=20,
    )
    seed: int = 20260808


class AdvanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticks: int = Field(default=1, ge=1, le=5_000)


class IncidentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal[
        "BLOCK_AGV",
        "CLEAR_BLOCKS",
        "LOW_BATTERY",
        "DISCONNECT_AGV",
        "STATION_OFFLINE",
        "STATION_RESTORE",
        "PROMPT_ATTACK",
    ]
    agv_id: Optional[str] = Field(default=None, pattern=r"^AGV\d{2}$")
    battery: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    station: Optional[str] = Field(default=None, pattern=r"^[ICO]\d$")
    text: Optional[str] = Field(default=None, max_length=2_000)

    def payload(self) -> dict[str, Any]:
        return self.model_dump(exclude={"kind"}, exclude_none=True)


class IdempotencyConflictError(RuntimeError):
    pass


class IdempotencyCache:
    def __init__(self, max_entries: int = 1_000) -> None:
        self.max_entries = max_entries
        self._entries: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    async def execute(
        self,
        *,
        scope: str,
        key: Optional[str],
        payload: dict[str, Any],
        operation: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        if not key:
            return await operation()
        digest = stable_hash(payload)
        cache_key = (scope, key)
        async with self._lock:
            existing = self._entries.get(cache_key)
            if existing is not None:
                if existing[0] != digest:
                    raise IdempotencyConflictError(
                        "Idempotency-Key was already used with a different request payload"
                    )
                return deepcopy(existing[1])
            response = await operation()
            if len(self._entries) >= self.max_entries:
                self._entries.pop(next(iter(self._entries)))
            self._entries[cache_key] = (digest, deepcopy(response))
            return response


def _sse(event: dict[str, Any]) -> str:
    data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return f"id: {event['id']}\nevent: {event['event_type']}\ndata: {data}\n\n"


def create_app(
    runtime: Optional[ShiftRuntime] = None,
    *,
    demo_token: Optional[str] = None,
) -> FastAPI:
    configured_token = demo_token or os.getenv("SHIFTZERO_DEMO_TOKEN")
    if runtime is None and os.getenv("K_SERVICE") and not configured_token:
        raise RuntimeError("SHIFTZERO_DEMO_TOKEN must be set for Cloud Run")
    service = runtime or ShiftRuntime(
        tick_interval_seconds=float(os.getenv("SHIFTZERO_TICK_INTERVAL_SECONDS", "0.05"))
    )
    token = configured_token or LOCAL_DEMO_TOKEN
    idempotency = IdempotencyCache()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        await service.close()

    app = FastAPI(
        title="ShiftZero Operations API",
        version="0.2.0-g3",
        description="Governed objective-to-simulator vertical slice for the ShiftZero demo fleet.",
        lifespan=lifespan,
    )
    app.state.runtime = service
    app.state.demo_token_is_default = token == LOCAL_DEMO_TOKEN
    cors_origins = [
        item.strip()
        for item in os.getenv(
            "SHIFTZERO_CORS_ORIGINS",
            "http://localhost:3000,http://127.0.0.1:3000,http://localhost:5173,http://127.0.0.1:5173",
        ).split(",")
        if item.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Idempotency-Key", "X-Demo-Token"],
    )

    async def require_mutation_token(
        x_demo_token: Optional[str] = Header(default=None, alias="X-Demo-Token"),
    ) -> None:
        if x_demo_token != token:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid demo token")

    @app.exception_handler(ShiftNotFoundError)
    async def shift_not_found(_: Request, exc: ShiftNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": f"shift not found: {exc}"})

    @app.exception_handler(InvalidTransitionError)
    async def invalid_transition(_: Request, exc: InvalidTransitionError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(IdempotencyConflictError)
    async def idempotency_conflict(_: Request, exc: IdempotencyConflictError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    async def invalid_demo_input(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    app.add_exception_handler(KeyError, invalid_demo_input)
    app.add_exception_handler(ValueError, invalid_demo_input)

    @app.get("/health", tags=["system"])
    @app.get("/healthz", tags=["system"], include_in_schema=False)
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "shiftzero-api",
            "version": app.version,
            "active_shift": service.objective.shift_id if service.objective else None,
            "default_demo_token": app.state.demo_token_is_default,
            "commander": service.commander.status(),
        }

    @app.get("/api/agent/status", tags=["agent"])
    async def agent_status() -> dict[str, Any]:
        return {
            **service.commander.status(),
            "last_plan": service.planning_outcome.evidence() if service.planning_outcome else None,
        }

    @app.get("/api/agents", tags=["agent"])
    async def agent_fleet() -> dict[str, Any]:
        from shiftzero_agents.agent import agent_fleet_status

        return agent_fleet_status()

    @app.get("/api/evidence/status", tags=["evidence"])
    async def evidence_status(request: Request) -> dict[str, Any]:
        from shiftzero_cloud.telemetry import telemetry_status

        project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
        region = os.getenv("GOOGLE_CLOUD_REGION", "asia-east1")
        service_name = os.getenv("K_SERVICE", "shiftzero-api")
        revision = os.getenv("K_REVISION")
        base_url = str(request.base_url).rstrip("/")
        snapshot = service.snapshot(include_tasks=False) if service.objective else None
        return {
            "backend": {
                "provider": "Google Cloud Run" if os.getenv("K_SERVICE") else "local",
                "service": service_name,
                "revision": revision,
                "region": region,
                "url": base_url,
            },
            "gemini": service.commander.status(),
            "agent_fleet": snapshot["agent_fleet"] if snapshot else None,
            "cloud_evidence": (
                snapshot["cloud_evidence"]
                if snapshot
                else {**service.evidence.status(), "trace": telemetry_status()}
            ),
            "active_shift": (
                {
                    "shift_id": snapshot["shift_id"],
                    "state": snapshot["shift_state"],
                    "tasks_completed": snapshot["kpi"]["tasks_completed"],
                    "tasks_total": snapshot["kpi"]["tasks_total"],
                    "safety_violations": snapshot["kpi"]["safety_violations"],
                    "trace_coverage": snapshot["kpi"].get("trace_coverage"),
                }
                if snapshot
                else None
            ),
            "console_links": {
                "cloud_run": (
                    f"https://console.cloud.google.com/run/detail/{region}/{service_name}/metrics"
                    f"?project={project_id}"
                    if project_id
                    else None
                ),
                "trace": (
                    f"https://console.cloud.google.com/traces/list?project={project_id}"
                    if project_id
                    else None
                ),
                "firestore": (
                    f"https://console.cloud.google.com/firestore/databases/-default-/data/panel/"
                    f"shiftzero_shifts?project={project_id}"
                    if project_id
                    else None
                ),
                "pubsub": (
                    f"https://console.cloud.google.com/cloudpubsub/topic/detail/shiftzero-events"
                    f"?project={project_id}"
                    if project_id
                    else None
                ),
            },
        }

    @app.post(
        "/api/shifts",
        status_code=status.HTTP_201_CREATED,
        tags=["shift"],
        dependencies=[Depends(require_mutation_token)],
    )
    async def create_shift(
        body: ShiftCreateRequest,
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        payload = body.model_dump(mode="json")
        return await idempotency.execute(
            scope="POST:/api/shifts",
            key=idempotency_key,
            payload=payload,
            operation=lambda: service.create_objective(
                objective=body.objective,
                target_task_count=body.target_task_count,
                deadline_tick=body.deadline_tick,
                min_battery_reserve=body.min_battery_reserve,
                priority_policy=body.priority_policy,
                constraints=tuple(body.constraints),
                seed=body.seed,
            ),
        )

    @app.post(
        "/api/shifts/{shift_id}/start",
        tags=["shift"],
        dependencies=[Depends(require_mutation_token)],
    )
    async def start_shift(
        shift_id: str,
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        return await idempotency.execute(
            scope=f"POST:/api/shifts/{shift_id}/start",
            key=idempotency_key,
            payload={"shift_id": shift_id},
            operation=lambda: service.start(shift_id),
        )

    @app.post(
        "/api/shifts/{shift_id}/pause",
        tags=["shift"],
        dependencies=[Depends(require_mutation_token)],
    )
    async def pause_shift(
        shift_id: str,
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        return await idempotency.execute(
            scope=f"POST:/api/shifts/{shift_id}/pause",
            key=idempotency_key,
            payload={"shift_id": shift_id},
            operation=lambda: service.pause(shift_id),
        )

    @app.post(
        "/api/shifts/{shift_id}/resume",
        tags=["shift"],
        dependencies=[Depends(require_mutation_token)],
    )
    async def resume_shift(
        shift_id: str,
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        return await idempotency.execute(
            scope=f"POST:/api/shifts/{shift_id}/resume",
            key=idempotency_key,
            payload={"shift_id": shift_id},
            operation=lambda: service.resume(shift_id),
        )

    @app.get("/api/shifts/{shift_id}", tags=["shift"])
    async def get_shift(shift_id: str) -> dict[str, Any]:
        service._require(shift_id)
        return service.snapshot(include_tasks=True)

    @app.get("/api/agvs", tags=["fleet"])
    async def get_agvs(shift_id: str = Query(...)) -> dict[str, Any]:
        service._require(shift_id)
        snapshot = service.snapshot(include_tasks=False)
        return {
            "shift_id": shift_id,
            "shift_state": snapshot["shift_state"],
            "state_version": snapshot["kpi"]["state_version"],
            "agvs": snapshot["agvs"],
        }

    @app.get("/api/actions/{action_id}/trace", tags=["evidence"])
    async def get_action_trace(action_id: str) -> dict[str, Any]:
        return service.action_trace(action_id)

    @app.get("/api/incidents/{incident_id}/trace", tags=["evidence"])
    async def get_incident_trace(incident_id: str) -> dict[str, Any]:
        return service.incident_trace(incident_id)

    @app.post(
        "/api/demo/advance",
        tags=["demo"],
        dependencies=[Depends(require_mutation_token)],
    )
    async def advance_demo(
        body: AdvanceRequest,
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        shift_id = service.objective.shift_id if service.objective else "none"
        payload = body.model_dump(mode="json")
        return await idempotency.execute(
            scope=f"POST:/api/demo/advance:{shift_id}",
            key=idempotency_key,
            payload=payload,
            operation=lambda: service.advance(body.ticks),
        )

    @app.post(
        "/api/demo/incidents",
        tags=["demo"],
        dependencies=[Depends(require_mutation_token)],
    )
    async def inject_incident(
        body: IncidentRequest,
        shift_id: str = Query(...),
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        payload = body.model_dump(mode="json", exclude_none=True)
        return await idempotency.execute(
            scope=f"POST:/api/demo/incidents:{shift_id}",
            key=idempotency_key,
            payload=payload,
            operation=lambda: service.inject(shift_id, body.kind, body.payload()),
        )

    @app.post(
        "/api/demo/reset",
        tags=["demo"],
        dependencies=[Depends(require_mutation_token)],
    )
    async def reset_demo(
        shift_id: str = Query(...),
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        return await idempotency.execute(
            scope=f"POST:/api/demo/reset:{shift_id}",
            key=idempotency_key,
            payload={"shift_id": shift_id},
            operation=lambda: service.reset(shift_id),
        )

    @app.get("/api/events", tags=["events"])
    async def get_events(
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=200, ge=1, le=1_000),
    ) -> dict[str, Any]:
        events = service.events_after(after=after, limit=limit)
        return {"events": events, "next_cursor": events[-1]["id"] if events else after}

    @app.get("/api/events/stream", tags=["events"])
    async def stream_events(
        request: Request,
        after: int = Query(default=0, ge=0),
        once: bool = Query(default=False),
    ) -> StreamingResponse:
        async def generate():
            queue = service.subscribe()
            cursor = after
            try:
                for event in service.events_after(after=cursor, limit=1_000):
                    cursor = event["id"]
                    yield _sse(event)
                if once:
                    return
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield ": heartbeat\n\n"
                        continue
                    if event["id"] <= cursor:
                        continue
                    cursor = event["id"]
                    yield _sse(event)
            finally:
                service.unsubscribe(queue)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


app = create_app()
