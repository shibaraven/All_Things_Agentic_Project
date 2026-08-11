"""Fail-safe ADK Commander boundary.

The model may reorder work within a priority class and describe its strategy,
but it cannot issue vehicle commands.  Every result is schema-checked and
validated against the live twin; any error, timeout, injection signal, or low
confidence falls back to the deterministic baseline.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import asdict, dataclass
from typing import Any, Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field

from shiftzero_core.contracts import SecurityFinding, stable_hash
from shiftzero_core.safety_kernel import screen_text
from shiftzero_core.world import World
from shiftzero_cloud.governance import ContentGuard


DEFAULT_MODEL = "gemini-3.5-flash"
DETERMINISTIC_PLANNER = "deterministic-commander-fallback-v1"
ADK_PLANNER = "gemini-adk-commander-v1"
REQUIRED_PHASES = frozenset({"dispatch", "transport", "recover", "verify", "complete"})


class CommanderPlanDraft(BaseModel):
    """The only structured output accepted from the planning model."""

    model_config = ConfigDict(extra="forbid")

    phases: list[str] = Field(min_length=5, max_length=7)
    task_order: list[str] = Field(min_length=1, max_length=500)
    strategy: str = Field(min_length=10, max_length=500)
    risk_summary: list[str] = Field(min_length=1, max_length=8)
    confidence: float = Field(ge=0.0, le=1.0)


@dataclass(frozen=True)
class PlanContext:
    shift_id: str
    objective: str
    target_task_count: int
    deadline_tick: int
    min_battery_reserve: float
    priority_policy: str
    constraints: tuple[str, ...]
    tick: int
    tasks: tuple[dict[str, Any], ...]
    agvs: tuple[dict[str, Any], ...]

    @classmethod
    def from_runtime(cls, objective: Any, world: World) -> "PlanContext":
        return cls(
            shift_id=objective.shift_id,
            objective=objective.objective,
            target_task_count=objective.target_task_count,
            deadline_tick=objective.deadline_tick,
            min_battery_reserve=objective.min_battery_reserve,
            priority_policy=objective.priority_policy,
            constraints=tuple(objective.constraints),
            tick=world.tick_count,
            tasks=tuple(
                {
                    "task_id": task.task_id,
                    "source": task.source,
                    "destination": task.destination,
                    "priority": task.priority,
                }
                for task in sorted(world.tasks.values(), key=lambda item: (item.priority, item.task_id))
            ),
            agvs=tuple(
                {
                    "agv_id": agv.agv_id,
                    "node": agv.node,
                    "battery": round(agv.battery, 2),
                    "healthy": agv.healthy,
                }
                for agv in sorted(world.agvs.values(), key=lambda item: item.agv_id)
            ),
        )

    def to_prompt_payload(self) -> dict[str, Any]:
        return {
            "shift_id": self.shift_id,
            "objective": self.objective,
            "completion_criteria": {
                "tasks_completed": self.target_task_count,
                "deadline_tick_lte": self.deadline_tick,
                "safety_violations": 0,
                "min_battery_reserve": self.min_battery_reserve,
            },
            "priority_policy": self.priority_policy,
            "hard_constraints": list(self.constraints),
            "tasks": list(self.tasks),
            "available_fleet": list(self.agvs),
        }

    @property
    def input_hash(self) -> str:
        return stable_hash(self.to_prompt_payload())


@dataclass(frozen=True)
class PlanningOutcome:
    draft: CommanderPlanDraft
    planner: str
    model: Optional[str]
    latency_ms: int
    input_hash: str
    fallback_reason: Optional[str] = None
    security_finding: Optional[SecurityFinding] = None
    screening: Optional[dict[str, Any]] = None

    def evidence(self) -> dict[str, Any]:
        return {
            "planner": self.planner,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "input_hash": self.input_hash,
            "confidence": self.draft.confidence,
            "fallback_reason": self.fallback_reason,
            "screening": self.screening,
        }


class CommanderBackend(Protocol):
    name: str
    model: Optional[str]

    async def plan(self, context: PlanContext) -> CommanderPlanDraft:
        """Return one structured planning draft."""


class AdkCommanderBackend:
    """Runs the structured Commander through Google ADK."""

    name = ADK_PLANNER

    def __init__(self, model: Optional[str] = None) -> None:
        self.model = model or os.getenv("SHIFTZERO_GEMINI_MODEL", DEFAULT_MODEL)

    @staticmethod
    def is_configured() -> bool:
        developer_key = bool(os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"))
        vertex = os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in {"1", "true", "yes"}
        return developer_key or (vertex and bool(os.getenv("GOOGLE_CLOUD_PROJECT")))

    async def plan(self, context: PlanContext) -> CommanderPlanDraft:
        if not self.is_configured():
            raise RuntimeError("Gemini credentials are not configured")

        # Imports stay lazy so the deterministic API remains lightweight and
        # fully usable without installing the optional ADK dependency.
        from google.adk.apps import App
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService
        from google.genai import types

        from .agent import make_commander_agent

        agent = make_commander_agent(self.model)
        app = App(name="shiftzero_agents", root_agent=agent)
        sessions = InMemorySessionService()
        runner = Runner(app=app, session_service=sessions)
        user_id = "shift-manager"
        session_id = f"plan-{context.input_hash}"
        await sessions.create_session(
            app_name=app.name,
            user_id=user_id,
            session_id=session_id,
            state={"shift_id": context.shift_id},
        )
        prompt = (
            "Create a safe mission plan from this operational context. "
            "Return every task_id exactly once. Preserve nondecreasing numeric priority; "
            "you may optimize ordering only within the same priority class. "
            "Do not invent tasks, vehicles, actions, or constraints.\n\n"
            + json.dumps(context.to_prompt_payload(), ensure_ascii=False, separators=(",", ":"))
        )
        message = types.Content(role="user", parts=[types.Part.from_text(text=prompt)])
        final_output: Any = None
        final_text = ""
        async for event in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=message,
        ):
            if not event.is_final_response():
                continue
            if event.output is not None:
                final_output = event.output
            if event.content and event.content.parts:
                final_text = "".join(part.text or "" for part in event.content.parts)

        if isinstance(final_output, CommanderPlanDraft):
            return final_output
        if isinstance(final_output, dict):
            return CommanderPlanDraft.model_validate(final_output)
        if final_text:
            return CommanderPlanDraft.model_validate_json(final_text)
        raise RuntimeError("ADK Commander returned no final structured output")


class SafeCommander:
    """Validates a primary Commander and owns the deterministic fallback."""

    def __init__(
        self,
        primary: Optional[CommanderBackend] = None,
        *,
        mode: str = "deterministic",
        timeout_seconds: float = 8.0,
        minimum_confidence: float = 0.65,
        content_guard: Optional[ContentGuard] = None,
    ) -> None:
        self.primary = primary
        self.mode = mode
        self.timeout_seconds = timeout_seconds
        self.minimum_confidence = minimum_confidence
        self.content_guard = content_guard or ContentGuard.from_env()

    @classmethod
    def from_env(cls) -> "SafeCommander":
        mode = os.getenv("SHIFTZERO_COMMANDER_MODE", "auto").strip().lower()
        if mode not in {"auto", "adk", "deterministic"}:
            mode = "deterministic"
        backend = AdkCommanderBackend()
        primary: Optional[CommanderBackend] = None
        if mode == "adk" or (mode == "auto" and backend.is_configured()):
            primary = backend
        return cls(
            primary,
            mode=mode,
            timeout_seconds=float(os.getenv("SHIFTZERO_COMMANDER_TIMEOUT_SECONDS", "8")),
            minimum_confidence=float(os.getenv("SHIFTZERO_COMMANDER_MIN_CONFIDENCE", "0.65")),
            content_guard=ContentGuard.from_env(),
        )

    def status(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "primary": self.primary.name if self.primary else None,
            "model": self.primary.model if self.primary else None,
            "configured": self.primary is not None,
            "timeout_seconds": self.timeout_seconds,
            "minimum_confidence": self.minimum_confidence,
            "content_guard": self.content_guard.status(),
        }

    async def plan(self, context: PlanContext) -> PlanningOutcome:
        started = time.perf_counter()
        screening = await self.content_guard.inspect(
            context.objective,
            "shift-objective",
            context.tick,
            trace_id=f"plan-{context.input_hash}",
        )
        if screening.finding is not None:
            return self._fallback(
                context,
                started,
                f"objective blocked by ingress policy: {screening.finding.category}",
                finding=screening.finding,
                screening=screening.evidence(),
            )

        if self.primary is None:
            return self._fallback(context, started, None)

        try:
            draft = await asyncio.wait_for(self.primary.plan(context), timeout=self.timeout_seconds)
            self._validate(draft, context)
            return PlanningOutcome(
                draft=draft,
                planner=self.primary.name,
                model=self.primary.model,
                latency_ms=round((time.perf_counter() - started) * 1000),
                input_hash=context.input_hash,
                screening=screening.evidence(),
            )
        except asyncio.TimeoutError:
            return self._fallback(context, started, f"primary timed out after {self.timeout_seconds:g}s")
        except Exception as exc:
            return self._fallback(context, started, f"primary rejected: {type(exc).__name__}: {exc}")

    def _validate(self, draft: CommanderPlanDraft, context: PlanContext) -> None:
        if draft.confidence < self.minimum_confidence:
            raise ValueError(
                f"confidence {draft.confidence:.2f} is below {self.minimum_confidence:.2f}"
            )
        phases = tuple(item.strip().lower() for item in draft.phases)
        if len(phases) != len(set(phases)):
            raise ValueError("phases contain duplicates")
        if not REQUIRED_PHASES.issubset(phases):
            raise ValueError(f"phases must include {sorted(REQUIRED_PHASES)}")
        if phases[0] != "dispatch" or phases[-1] != "complete":
            raise ValueError("phases must start with dispatch and end with complete")

        expected = {task["task_id"] for task in context.tasks}
        actual = draft.task_order
        if len(actual) != len(set(actual)):
            raise ValueError("task_order contains duplicates")
        if set(actual) != expected:
            missing = sorted(expected - set(actual))
            unknown = sorted(set(actual) - expected)
            raise ValueError(f"task_order mismatch; missing={missing}, unknown={unknown}")

        priority = {task["task_id"]: int(task["priority"]) for task in context.tasks}
        ordered_priorities = [priority[task_id] for task_id in actual]
        if ordered_priorities != sorted(ordered_priorities):
            raise ValueError("task_order violates priority_policy")

        for index, text in enumerate([draft.strategy, *draft.risk_summary]):
            output_finding = screen_text(text, f"commander-output-{index}", context.tick)
            if output_finding is not None:
                raise ValueError(f"model output blocked by ingress policy: {output_finding.category}")

    @staticmethod
    def _deterministic_draft(context: PlanContext) -> CommanderPlanDraft:
        return CommanderPlanDraft(
            phases=["dispatch", "transport", "recover", "verify", "complete"],
            task_order=[task["task_id"] for task in context.tasks],
            strategy="Deadline-first deterministic dispatch with battery and station queue safeguards.",
            risk_summary=[
                "route congestion",
                "battery reserve",
                "station availability",
                "untrusted external instructions",
            ],
            confidence=1.0,
        )

    def _fallback(
        self,
        context: PlanContext,
        started: float,
        reason: Optional[str],
        *,
        finding: Optional[SecurityFinding] = None,
        screening: Optional[dict[str, Any]] = None,
    ) -> PlanningOutcome:
        return PlanningOutcome(
            draft=self._deterministic_draft(context),
            planner=DETERMINISTIC_PLANNER,
            model=self.primary.model if self.primary else None,
            latency_ms=round((time.perf_counter() - started) * 1000),
            input_hash=context.input_hash,
            fallback_reason=reason,
            security_finding=finding,
            screening=screening,
        )
