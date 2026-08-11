"""
ShiftZero — control.py
======================
Deterministic Fleet Dispatcher + Recovery Coordinator，以及把
world / kernel / audit 串起來的 ControlLoop。

為什麼先寫決定性版本，再接 LLM？
--------------------------------
規劃書 §13 R3 把「LLM response 不穩定、Demo 卡住」列為高風險。
唯一有效的緩解是：讓整條 pipeline 在**完全沒有模型**的情況下也能跑完 42/42。
ADK Agent 接上來之後，它產生的 ActionProposal 走的是同一個 kernel、
同一個 execute()，這裡的實作就自動變成 timeout 時的 deterministic fallback。

因此本檔的 propose_* 函式同時是：
  (a) 離線可重播的基準線（NFR-001、S01-S10）
  (b) LLM agent 的 fallback policy
  (c) agent evaluation 的 reference behaviour（拿來比對 LLM 選車是否合理）
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from .contracts import (
    ActionProposal,
    ActionTicket,
    ActionType,
    AgvMode,
    AuditEvent,
    ExecutionResult,
    Incident,
    IncidentType,
    Rejection,
    TaskStatus,
    make_id,
    stable_hash,
)
from .safety_kernel import SafetyKernel
from .world import MOVE_COST_PCT, SECONDS_PER_TICK, STATIONS, World

DISPATCHER = "fleet-dispatcher-v1"
RECOVERY = "recovery-coordinator-v1"
COMMANDER = "operations-commander-v1"

PROPOSAL_TTL_SECONDS = 20

#: 同一取貨站同時允許的在途 AGV 數（station queue discipline）
MAX_STATION_QUEUE = 4

#: 恢復動作的重試間隔（tick）
RECOVERY_RETRY_TICKS = 5


def build_proposal(
    world: World,
    agent_id: str,
    action_type: str,
    target_id: str,
    parameters: dict[str, Any],
    idempotency_key: str,
    rationale: str = "",
    confidence: float = 1.0,
    ttl_seconds: int = PROPOSAL_TTL_SECONDS,
    state_version: Optional[int] = None,
) -> ActionProposal:
    now = world.tick_count * SECONDS_PER_TICK
    return ActionProposal(
        action_id=make_id("act", world.shift_id, world.tick_count, agent_id, action_type, target_id, parameters),
        shift_id=world.shift_id,
        agent_id=agent_id,
        action_type=action_type,
        target_id=target_id,
        parameters=parameters,
        idempotency_key=idempotency_key,
        issued_at=now,
        expires_at=now + ttl_seconds,
        rationale=rationale,
        confidence=confidence,
        state_version=state_version,
    )


# --------------------------------------------------------------------------
@dataclass
class Planner:
    """決定性派工與恢復邏輯。"""

    #: 選車權重。LLM 版本的 Fleet Dispatcher 會拿到同一組語意的 scoring tool。
    w_distance: float = 1.0
    w_battery: float = 0.35
    w_priority: float = 2.0
    task_rank: dict[str, int] = field(default_factory=dict)

    def set_task_order(self, task_order: tuple[str, ...]) -> None:
        """Apply a validated Commander order within the hard priority policy."""
        self.task_rank = {task_id: index for index, task_id in enumerate(task_order)}

    # ------------------------------------------------------------ scoring
    def score_candidate(self, world: World, agv_id: str, task_id: str) -> float:
        agv, task = world.agvs[agv_id], world.tasks[task_id]
        distance = world.manhattan(agv.node, STATIONS[task.source])
        headroom = world.projected_battery(agv, task) - world.min_battery_reserve
        battery_term = max(0.0, 40.0 - headroom)
        return (
            self.w_distance * distance
            + self.w_battery * battery_term
            + self.w_priority * task.priority
        )

    def best_candidate(
        self, world: World, task_id: str, exclude: tuple[str, ...] = ()
    ) -> Optional[str]:
        task = world.tasks[task_id]
        best, best_score = None, float("inf")
        for agv_id in sorted(world.agvs):
            if agv_id in exclude:
                continue
            agv = world.agvs[agv_id]
            if not agv.available or not agv.healthy:
                continue
            if world.projected_battery(agv, task) < world.min_battery_reserve:
                continue
            score = self.score_candidate(world, agv_id, task_id)
            if score < best_score:
                best, best_score = agv_id, score
        return best

    # ----------------------------------------------------------- proposals
    def propose(self, world: World) -> list[ActionProposal]:
        return self.propose_recovery(world) + self.propose_dispatch(world)

    def propose_dispatch(self, world: World) -> list[ActionProposal]:
        out: list[ActionProposal] = []
        pending = sorted(
            world.pending_tasks(),
            key=lambda task: (
                task.priority,
                self.task_rank.get(task.task_id, len(self.task_rank)),
                task.task_id,
            ),
        )
        taken: set[str] = set()
        queued = {s: world.inbound_queue_len(s) for s in {t.source for t in world.tasks.values()}}

        for task in pending:
            # 站點排隊管制：同一取貨站同時只放行 MAX_STATION_QUEUE 台車，
            # 否則車隊會在 inbound port 前擠成一團，實際吞吐反而下降。
            if queued.get(task.source, 0) >= MAX_STATION_QUEUE:
                continue
            agv_id = self.best_candidate(world, task.task_id, exclude=tuple(sorted(taken)))
            if agv_id is None:
                continue
            taken.add(agv_id)
            queued[task.source] = queued.get(task.source, 0) + 1
            out.append(
                build_proposal(
                    world,
                    DISPATCHER,
                    ActionType.ASSIGN_TASK.value,
                    task.task_id,
                    {"agv_id": agv_id, "task_id": task.task_id},
                    idempotency_key=f"{world.shift_id}:{task.task_id}:assign:{agv_id}:{world.tick_count}",
                    rationale=(
                        f"nearest available unit; projected battery "
                        f"{world.projected_battery(world.agvs[agv_id], task):.1f}%"
                    ),
                )
            )

        # 閒置且電量偏低的車主動去充電，避免之後才變成 incident
        for agv_id in sorted(world.agvs):
            agv = world.agvs[agv_id]
            if agv_id in taken or not agv.available or not agv.healthy:
                continue
            if agv.battery >= world.min_battery_reserve + 12.0:
                continue
            station = world.nearest_charger(agv.node)
            out.append(
                build_proposal(
                    world,
                    DISPATCHER,
                    ActionType.REQUEST_CHARGE.value,
                    agv_id,
                    {"agv_id": agv_id, "station_id": station},
                    idempotency_key=f"{world.shift_id}:{agv_id}:charge:{world.tick_count}",
                    rationale=f"idle at {agv.battery:.1f}%, topping up before next assignment",
                )
            )
        return out

    def propose_recovery(self, world: World) -> list[ActionProposal]:
        out: list[ActionProposal] = []
        for inc in sorted(world.incidents.values(), key=lambda i: i.incident_id):
            if inc.status not in ("OPEN", "RECOVERING"):
                continue
            # 恢復必須可以重試。第一次提案被 Safety Kernel 拒絕（例如當下
            # 沒有電量足夠的接手車）不代表事件已處理——只標記一次就再也不管，
            # 車輛會永遠停在 BLOCKED。
            if world.tick_count - inc.last_attempt_tick < RECOVERY_RETRY_TICKS:
                continue

            if inc.type is IncidentType.BLOCKED:
                proposals = self._recover_blocked(world, inc)
            elif inc.type is IncidentType.LOW_BATTERY:
                proposals = self._recover_low_battery(world, inc)
            elif inc.type is IncidentType.DISCONNECTED:
                proposals = self._recover_disconnected(world, inc)
            elif inc.type is IncidentType.STATION_OFFLINE:
                inc.status = "RECOVERING"
                continue
            else:
                proposals = []

            if proposals:
                inc.status = "RECOVERING"
                inc.last_attempt_tick = world.tick_count
                inc.attempts += 1
                out.extend(proposals)
        return out

    @staticmethod
    def _agv_of(inc: Incident) -> Optional[str]:
        for e in inc.affected_entities:
            if e.startswith("AGV"):
                return e
        return None

    @staticmethod
    def _task_of(inc: Incident) -> Optional[str]:
        for e in inc.affected_entities:
            if e.startswith("task-"):
                return e
        return None

    def _handoff(
        self, world: World, inc: Incident, agv_id: str, task_id: str, reason: str
    ) -> list[ActionProposal]:
        agv = world.agvs[agv_id]
        if agv.load_id is not None:
            return []  # 承載中不得交接；等待卸貨或現場排除
        target = self.best_candidate(world, task_id, exclude=(agv_id,))
        if target is None:
            return []
        return [
            build_proposal(
                world,
                RECOVERY,
                ActionType.REASSIGN_TASK.value,
                task_id,
                {"from_agv": agv_id, "to_agv": target, "task_id": task_id, "reason_code": reason},
                idempotency_key=f"{world.shift_id}:{task_id}:reassign:{target}",
                rationale=f"{agv_id} unavailable ({reason}); {target} is the best healthy candidate",
                confidence=0.9,
            )
        ]

    def _recover_blocked(self, world: World, inc: Incident) -> list[ActionProposal]:
        agv_id = self._agv_of(inc)
        if agv_id is None:
            return []
        agv = world.agvs[agv_id]
        task_id = self._task_of(inc)

        # 路徑恢復了 → 直接讓車輛回到工作
        goal = world._goal_for_mode(agv) if agv.task_id else None
        route_back = world.plan_path(agv.node, goal) if goal else None
        can_leave_without_goal = any(
            node not in world.blocked_nodes
            and world.occupancy.get(node) in (None, agv.agv_id)
            for node in world.neighbors(agv.node)
        )
        if agv.mode == AgvMode.BLOCKED.value and (
            (goal is not None and route_back is not None)
            or (goal is None and can_leave_without_goal)
        ):
            return [
                build_proposal(
                    world,
                    RECOVERY,
                    ActionType.RESUME_AGV.value,
                    agv_id,
                    {"agv_id": agv_id, "reason_code": "PATH_CLEARED"},
                    idempotency_key=f"{world.shift_id}:{agv_id}:resume:{world.tick_count}",
                    rationale="obstruction cleared; route to goal is reservable again",
                )
            ]

        if task_id and world.tasks[task_id].assigned_agv == agv_id:
            return self._handoff(world, inc, agv_id, task_id, "PATH_BLOCKED")
        return []

    def _recover_low_battery(self, world: World, inc: Incident) -> list[ActionProposal]:
        agv_id = self._agv_of(inc)
        if agv_id is None:
            return []
        agv = world.agvs[agv_id]
        task_id = self._task_of(inc)
        out: list[ActionProposal] = []

        if agv.mode in (AgvMode.BLOCKED.value, AgvMode.PAUSED.value):
            # SP-05 禁止對 BLOCKED/PAUSED 車輛下移動命令，包含前往充電。
            # 因此恢復順序必須是 RESUME → CHARGE，不能直接 CHARGE。
            return [
                build_proposal(
                    world,
                    RECOVERY,
                    ActionType.RESUME_AGV.value,
                    agv_id,
                    {"agv_id": agv_id, "reason_code": "LOW_BATTERY_NEEDS_CHARGER"},
                    idempotency_key=f"{world.shift_id}:{agv_id}:resume:{world.tick_count}",
                    rationale="vehicle must leave BLOCKED state before a charger run can be authorised",
                )
            ]

        if task_id and agv.task_id == task_id and agv.load_id is None:
            out.extend(self._handoff(world, inc, agv_id, task_id, "LOW_BATTERY"))

        if agv.load_id is None and agv.mode not in (
            AgvMode.CHARGING.value,
            AgvMode.TO_CHARGER.value,
        ):
            station = world.nearest_charger(agv.node)
            out.append(
                build_proposal(
                    world,
                    DISPATCHER,
                    ActionType.REQUEST_CHARGE.value,
                    agv_id,
                    {"agv_id": agv_id, "station_id": station, "reason_code": "LOW_BATTERY"},
                    idempotency_key=f"{world.shift_id}:{agv_id}:charge:{world.tick_count}",
                    rationale=f"battery {agv.battery:.1f}% below {world.min_battery_reserve:.0f}% reserve",
                )
            )
        return out

    def _recover_disconnected(self, world: World, inc: Incident) -> list[ActionProposal]:
        agv_id = self._agv_of(inc)
        task_id = self._task_of(inc)
        if agv_id is None or task_id is None:
            return []
        return self._handoff(world, inc, agv_id, task_id, "AGV_DISCONNECTED")


# --------------------------------------------------------------------------
@dataclass
class ControlLoop:
    """一個 tick 的完整閉環：observe → reason → act → validate → execute → explain。"""

    world: World
    kernel: SafetyKernel = field(default_factory=SafetyKernel)
    planner: Planner = field(default_factory=Planner)
    audit: list[AuditEvent] = field(default_factory=list)
    results: list[ExecutionResult] = field(default_factory=list)
    proposals: dict[str, ActionProposal] = field(default_factory=dict)
    tickets: dict[str, ActionTicket] = field(default_factory=dict)
    policy_decisions: dict[str, dict[str, Any]] = field(default_factory=dict)
    manual_interventions: int = 0

    def _log(self, actor: str, event_type: str, decision: str, payload: Any, detail: str = "", corr: str = "") -> None:
        self.audit.append(
            AuditEvent(
                trace_id=make_id("trace", self.world.shift_id, self.world.tick_count, len(self.audit)),
                tick=self.world.tick_count,
                actor=actor,
                event_type=event_type,
                decision=decision,
                input_hash=stable_hash(payload),
                detail=detail,
                correlation_id=corr,
            )
        )

    def submit(self, proposal: ActionProposal) -> tuple[str, str]:
        """外部（LLM agent 或測試）提出的 proposal 也走同一條路。"""
        self.proposals[proposal.action_id] = proposal
        outcome = self.kernel.validate(proposal, self.world)
        if isinstance(outcome, Rejection):
            self.policy_decisions[proposal.action_id] = {
                "decision": "REJECTED",
                "code": outcome.code.value,
                "detail": outcome.detail,
                "policy_version": outcome.policy_version,
            }
            self._log(
                proposal.agent_id,
                "policy_decision",
                f"REJECTED:{outcome.code.value}",
                proposal.to_dict(),
                outcome.detail,
                corr=proposal.action_id,
            )
            return "REJECTED", outcome.code.value

        ticket: ActionTicket = outcome
        self.tickets[proposal.action_id] = ticket
        self.policy_decisions[proposal.action_id] = {
            "decision": "APPROVED",
            "code": "OK",
            "policy_version": ticket.policy_version,
            "constraints_checked": list(ticket.constraints_checked),
            "state_version": ticket.state_version,
        }
        consume_error = self.kernel.consume(ticket, self.world)
        if consume_error is not None:
            self.policy_decisions[proposal.action_id] = {
                "decision": "REJECTED",
                "code": consume_error.code.value,
                "detail": consume_error.detail,
                "policy_version": consume_error.policy_version,
            }
            self._log(
                proposal.agent_id,
                "ticket_check",
                f"REJECTED:{consume_error.code.value}",
                proposal.to_dict(),
                consume_error.detail,
                corr=proposal.action_id,
            )
            return "REJECTED", consume_error.code.value

        started = self.world.tick_count * SECONDS_PER_TICK
        status, detail = self.world.execute(ticket, proposal.parameters)
        self.results.append(
            ExecutionResult(
                action_id=proposal.action_id,
                ticket_id=ticket.ticket_id,
                status=status,
                started_at=started,
                completed_at=self.world.tick_count * SECONDS_PER_TICK,
                state_version=self.world.state_version,
                detail=detail,
            )
        )
        self._log(
            proposal.agent_id,
            "execution",
            status,
            proposal.to_dict(),
            detail,
            corr=proposal.action_id,
        )
        return status, detail

    def action_trace(self, action_id: str) -> Optional[dict[str, Any]]:
        proposal = self.proposals.get(action_id)
        if proposal is None:
            return None
        ticket = self.tickets.get(action_id)
        result = next((item for item in reversed(self.results) if item.action_id == action_id), None)
        return {
            "action_id": action_id,
            "proposal": proposal.to_dict(),
            "policy_decision": self.policy_decisions.get(action_id),
            "ticket": asdict(ticket) if ticket else None,
            "execution_result": asdict(result) if result else None,
            "audit": [item.to_dict() for item in self.audit if item.correlation_id == action_id],
        }

    def step(self) -> list[Incident]:
        incidents = self.world.tick()
        for inc in incidents:
            self._log(
                "safety-monitor",
                "incident_detected",
                inc.type.value,
                {"id": inc.incident_id, "src": inc.source},
                inc.source,
                corr=inc.incident_id,
            )
        for proposal in self.planner.propose(self.world):
            self.submit(proposal)
        return incidents

    def run(self, max_ticks: int, stop_when_complete: bool = True) -> None:
        for _ in range(max_ticks):
            self.step()
            if stop_when_complete and self.world.completed_count() == len(self.world.tasks):
                break

    # -------------------------------------------------------------- reports
    def kpi(self) -> dict[str, Any]:
        k = self.world.kpi()
        k.update(
            {
                "manual_interventions": self.manual_interventions,
                "actions_executed": sum(1 for r in self.results if r.status == "EXECUTED"),
                "actions_rejected": len(self.kernel.rejections),
                "security_findings": len(self.kernel.findings),
                "audit_events": len(self.audit),
                "trace_coverage": 1.0
                if not self.results
                else sum(
                    1
                    for r in self.results
                    if any(a.correlation_id == r.action_id for a in self.audit)
                )
                / len(self.results),
            }
        )
        return k
