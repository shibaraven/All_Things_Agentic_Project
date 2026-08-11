"""
ShiftZero — safety_kernel.py
============================
Deterministic Safety Kernel（§4.2、§8.2）。

這一層**不是 LLM**。它是 Agent 與車體之間唯一的閘門：
沒有 ActionTicket，就不會有任何 command 被發到 Pub/Sub / Edge Adapter。

拒絕即為硬限制；任何未知欄位、未知 action、過期 proposal、
身分不符或安全條件不足，一律 fail closed（SP-08）。

驗證順序刻意由「便宜且與世界狀態無關」排到「需要讀 twin」，
讓被拒絕的 proposal 盡早短路，也讓 reason code 穩定可測。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional, Union

from .contracts import (
    AGENT_ALLOWED_ACTIONS,
    ActionProposal,
    ActionTicket,
    ActionType,
    AgvMode,
    POLICY_VERSION,
    RejectCode,
    Rejection,
    SecurityFinding,
    TaskStatus,
    make_id,
    schema_errors,
)
from .world import MOVE_COST_PCT, SECONDS_PER_TICK, STATIONS, World

TICKET_TTL_SECONDS = 30

#: 會造成車體移動的 action。EMERGENCY_STOP 期間一律禁止（SP-01）。
MOVEMENT_ACTIONS = frozenset(
    {
        ActionType.ASSIGN_TASK.value,
        ActionType.REASSIGN_TASK.value,
        ActionType.REROUTE.value,
        ActionType.REQUEST_CHARGE.value,
        ActionType.RESUME_AGV.value,
    }
)

ValidationOutcome = Union[ActionTicket, Rejection]


# --------------------------------------------------------------------------
# Ingress screening（Model Armor 的本機備援；雲端版改呼叫 Model Armor API）
# --------------------------------------------------------------------------
_INJECTION_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"ignore\s+(all\s+)?(previous\s+|prior\s+)?(safety\s+)?(rules|instructions|policies)", "instruction_override"),
    (r"disregard\s+(the\s+)?(safety|policy|guardrail)", "instruction_override"),
    (r"(disable|bypass|turn\s+off|override)\s+(the\s+)?(safety|kernel|guardrail|policy|limit)", "safety_bypass"),
    (r"(maximum|max|full)\s+speed", "unsafe_parameter"),
    (r"set\s+(the\s+)?(speed\s*limit|safety\s*threshold)", "unsafe_parameter"),
    (r"clear\s+(the\s+)?emergency", "safety_bypass"),
    (r"you\s+are\s+now\s+(in\s+)?(admin|root|maintenance\s+override)", "role_escalation"),
    (r"(reveal|print|show)\s+(your\s+)?(system\s+prompt|credentials|service\s+account)", "exfiltration"),
)


def screen_text(text: str, source: str, tick: int, trace_id: str = "") -> Optional[SecurityFinding]:
    """
    對外部文字（maintenance note、WMS message、operator free text）做入口篩檢。

    回傳 finding 代表「這段內容不得被轉成 ActionProposal」。
    注意：這只是第一層。即使這層漏失，Safety Kernel 仍會擋下（§6.4）。
    """
    lowered = text.lower()
    for pattern, category in _INJECTION_PATTERNS:
        match = re.search(pattern, lowered)
        if match:
            start = max(0, match.start() - 20)
            return SecurityFinding(
                finding_id=make_id("sec", source, pattern, tick),
                source=source,
                category=category.upper(),
                blocked_reason=f"matched ingress rule: {category}",
                excerpt=text[start : match.end() + 20].strip(),
                detected_at=tick,
                trace_id=trace_id,
            )
    return None


# --------------------------------------------------------------------------
@dataclass
class SafetyKernel:
    policy_version: str = POLICY_VERSION
    ticket_ttl_seconds: int = TICKET_TTL_SECONDS

    _idempotency: dict[str, str] = field(default_factory=dict)
    _issued: dict[str, ActionTicket] = field(default_factory=dict)
    _used: set[str] = field(default_factory=set)
    rejections: list[Rejection] = field(default_factory=list)
    findings: list[SecurityFinding] = field(default_factory=list)

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _now(world: World) -> int:
        return world.tick_count * SECONDS_PER_TICK

    def _reject(self, proposal: ActionProposal, code: RejectCode, detail: str) -> Rejection:
        rej = Rejection(
            action_id=proposal.action_id,
            agent_id=proposal.agent_id,
            code=code,
            detail=detail,
            policy_version=self.policy_version,
        )
        self.rejections.append(rej)
        if code in (RejectCode.R_FORBIDDEN_ACTION, RejectCode.R_IDENTITY):
            self.findings.append(
                SecurityFinding(
                    finding_id=make_id("sec", proposal.action_id, code.value),
                    source=proposal.agent_id,
                    category="PRIVILEGE_ESCALATION"
                    if code is RejectCode.R_IDENTITY
                    else "SAFETY_BYPASS_ATTEMPT",
                    blocked_reason=detail,
                    excerpt=f"{proposal.action_type} on {proposal.target_id}",
                    detected_at=0,
                )
            )
        return rej

    # ------------------------------------------------------------ validate
    def validate(self, proposal: ActionProposal, world: World) -> ValidationOutcome:
        now = self._now(world)

        # 1. schema / forbidden action（SP-07、SP-08）
        err = schema_errors(proposal)
        if err:
            return self._reject(proposal, err[0], err[1])

        # 2. identity least privilege
        allowed = AGENT_ALLOWED_ACTIONS.get(proposal.agent_id)
        if allowed is None:
            return self._reject(proposal, RejectCode.R_IDENTITY, f"unknown agent {proposal.agent_id}")
        if proposal.action_type not in allowed:
            return self._reject(
                proposal,
                RejectCode.R_IDENTITY,
                f"{proposal.agent_id} is not permitted to issue {proposal.action_type}",
            )

        # 3. proposal TTL（SP-06 / S09 stale proposal）
        if now > proposal.expires_at:
            return self._reject(
                proposal,
                RejectCode.R_PROPOSAL_EXPIRED,
                f"proposal expired at {proposal.expires_at}, now {now}",
            )

        # 4. state staleness — 決策必須基於當前的 Operational Twin
        if proposal.state_version is not None and proposal.state_version != world.state_version:
            return self._reject(
                proposal,
                RejectCode.R_STALE_STATE,
                f"proposal saw v{proposal.state_version}, twin is v{world.state_version}",
            )

        # 5. idempotency（S05 duplicate command）
        if proposal.idempotency_key in self._idempotency:
            return self._reject(
                proposal,
                RejectCode.R_DUPLICATE,
                f"idempotency_key already executed as {self._idempotency[proposal.idempotency_key]}",
            )

        # 6. emergency stop（SP-01）
        if world.emergency_stop and proposal.action_type in MOVEMENT_ACTIONS:
            return self._reject(
                proposal, RejectCode.R_EMERGENCY_STOP, "EMERGENCY_STOP active; movement forbidden"
            )

        checked: list[str] = ["schema", "identity", "ttl", "idempotency", "emergency_stop"]
        params = proposal.parameters

        # 7. 實體存在性
        for key in ("agv_id", "from_agv", "to_agv"):
            if key in params and params[key] not in world.agvs:
                return self._reject(
                    proposal, RejectCode.R_UNKNOWN_ENTITY, f"unknown agv {params[key]!r}"
                )
        if "task_id" in params and params["task_id"] not in world.tasks:
            return self._reject(
                proposal, RejectCode.R_UNKNOWN_ENTITY, f"unknown task {params['task_id']!r}"
            )
        if "station_id" in params and params["station_id"] not in STATIONS:
            return self._reject(
                proposal, RejectCode.R_UNKNOWN_ENTITY, f"unknown station {params['station_id']!r}"
            )
        checked.append("entities")

        # 8. 站點可用性（必須早於電量估算：離線站點是硬前提）
        if proposal.action_type == ActionType.REQUEST_CHARGE.value:
            if params["station_id"] in world.offline_stations:
                return self._reject(
                    proposal, RejectCode.R_STATION_OFFLINE, f"{params['station_id']} is offline"
                )
        if proposal.action_type in (ActionType.ASSIGN_TASK.value, ActionType.REASSIGN_TASK.value):
            task = world.tasks[params["task_id"]]
            if {task.source, task.destination} & world.offline_stations:
                return self._reject(
                    proposal,
                    RejectCode.R_STATION_OFFLINE,
                    f"{task.task_id} touches an offline station",
                )
        checked.append("station_online")

        # 9. 車輛可用性（SP-05）
        target_agv_key = "to_agv" if proposal.action_type == ActionType.REASSIGN_TASK.value else "agv_id"
        if target_agv_key in params and proposal.action_type in MOVEMENT_ACTIONS:
            agv = world.agvs[params[target_agv_key]]
            if proposal.action_type != ActionType.RESUME_AGV.value and agv.mode in (
                AgvMode.BLOCKED.value,
                AgvMode.DISCONNECTED.value,
                AgvMode.PAUSED.value,
            ):
                return self._reject(
                    proposal,
                    RejectCode.R_VEHICLE_UNAVAILABLE,
                    f"{agv.agv_id} is {agv.mode}; movement command refused (SP-05)",
                )
            checked.append("vehicle_available")

        # 9. 任務唯一指派（SP-04）
        if proposal.action_type in (ActionType.ASSIGN_TASK.value, ActionType.REASSIGN_TASK.value):
            task = world.tasks[params["task_id"]]
            if task.status == TaskStatus.COMPLETED.value:
                return self._reject(
                    proposal, RejectCode.R_DOUBLE_ASSIGN, f"{task.task_id} already completed"
                )
            # SP-04 的車輛側對偶：一台車同時只能有一個 active assignment。
            # 少了這條，同一個 tick 內「恢復交接」與「一般派工」會同時把
            # 兩個任務指到同一台車，留下永遠完成不了的孤兒任務。
            receiver = world.agvs[params[target_agv_key]]
            if receiver.task_id is not None and receiver.task_id != params["task_id"]:
                return self._reject(
                    proposal,
                    RejectCode.R_DOUBLE_ASSIGN,
                    f"{receiver.agv_id} already holds {receiver.task_id} (SP-04)",
                )

            holder = task.assigned_agv
            if proposal.action_type == ActionType.ASSIGN_TASK.value and holder is not None:
                return self._reject(
                    proposal,
                    RejectCode.R_DOUBLE_ASSIGN,
                    f"{task.task_id} already assigned to {holder} (SP-04)",
                )
            if proposal.action_type == ActionType.REASSIGN_TASK.value:
                if holder != params["from_agv"]:
                    return self._reject(
                        proposal,
                        RejectCode.R_DOUBLE_ASSIGN,
                        f"{task.task_id} is held by {holder}, not {params['from_agv']}",
                    )
                if params["from_agv"] == params["to_agv"]:
                    return self._reject(
                        proposal, RejectCode.R_SCHEMA, "from_agv and to_agv must differ"
                    )
                # 禁止承載中直接交接（§6.3 Safety）
                if world.agvs[params["from_agv"]].load_id is not None:
                    return self._reject(
                        proposal,
                        RejectCode.R_VEHICLE_UNAVAILABLE,
                        f"{params['from_agv']} is carrying {world.agvs[params['from_agv']].load_id};"
                        " handoff while loaded is forbidden",
                    )
            checked.append("single_assignment")

        # 10. 電量保留（SP-02）
        if proposal.action_type in (ActionType.ASSIGN_TASK.value, ActionType.REASSIGN_TASK.value):
            agv = world.agvs[params[target_agv_key]]
            task = world.tasks[params["task_id"]]
            projected = world.projected_battery(agv, task)
            if projected < world.min_battery_reserve:
                return self._reject(
                    proposal,
                    RejectCode.R_BATTERY_RESERVE,
                    f"{agv.agv_id} projected {projected:.1f}% < reserve"
                    f" {world.min_battery_reserve:.0f}% (SP-02)",
                )
            checked.append("battery_reserve")

        # 12. 路徑可行性（SP-03：沒有可預約的路徑就不能發車）
        if proposal.action_type in MOVEMENT_ACTIONS and target_agv_key in params:
            agv = world.agvs[params[target_agv_key]]
            goal = self._goal_node(proposal, world)
            if goal is not None:
                if world.plan_path(agv.node, goal) is None:
                    return self._reject(
                        proposal,
                        RejectCode.R_NO_RESERVATION,
                        f"no reservable route from {agv.node} to {goal} (SP-03)",
                    )
                checked.append("route_reservable")

        # 通過 → 發出 ticket
        ticket = ActionTicket(
            ticket_id=make_id("tkt", proposal.action_id, proposal.idempotency_key),
            action_id=proposal.action_id,
            agent_id=proposal.agent_id,
            action_type=proposal.action_type,
            policy_version=self.policy_version,
            constraints_checked=tuple(checked),
            issued_at=now,
            expires_at=now + self.ticket_ttl_seconds,
            state_version=world.state_version,
        )
        self._idempotency[proposal.idempotency_key] = ticket.ticket_id
        self._issued[ticket.ticket_id] = ticket
        return ticket

    def _goal_node(self, proposal: ActionProposal, world: World) -> Optional[str]:
        params = proposal.parameters
        at = proposal.action_type
        if at == ActionType.ASSIGN_TASK.value:
            return STATIONS[world.tasks[params["task_id"]].source]
        if at == ActionType.REASSIGN_TASK.value:
            return STATIONS[world.tasks[params["task_id"]].source]
        if at == ActionType.REQUEST_CHARGE.value:
            return STATIONS[params["station_id"]]
        if at == ActionType.REROUTE.value:
            agv = world.agvs[params["agv_id"]]
            return world._goal_for_mode(agv)
        return None

    # -------------------------------------------------------------- consume
    def consume(self, ticket: ActionTicket, world: World) -> Optional[Rejection]:
        """發車前最後一道檢查（SP-06）。回傳 None 代表可執行。"""
        now = self._now(world)
        if ticket.ticket_id in self._used:
            return Rejection(
                ticket.action_id, ticket.agent_id, RejectCode.R_TICKET_INVALID, "ticket already used"
            )
        issued = self._issued.get(ticket.ticket_id)
        if issued is None:
            return Rejection(
                ticket.action_id, ticket.agent_id, RejectCode.R_TICKET_INVALID, "unknown ticket"
            )
        # ticket_id 不是 bearer token。執行端必須拿到 Kernel 當初簽發的完整
        # immutable ticket；只重用一個有效 ID、再竄改 identity/action/state
        # 仍必須 fail closed（SP-06）。
        if ticket != issued:
            return Rejection(
                ticket.action_id,
                ticket.agent_id,
                RejectCode.R_TICKET_INVALID,
                "ticket contents do not match the issued ticket",
            )
        if now > ticket.expires_at:
            return Rejection(
                ticket.action_id, ticket.agent_id, RejectCode.R_TICKET_INVALID, "ticket expired"
            )
        if world.emergency_stop and ticket.action_type in MOVEMENT_ACTIONS:
            return Rejection(
                ticket.action_id,
                ticket.agent_id,
                RejectCode.R_EMERGENCY_STOP,
                "EMERGENCY_STOP raised after issue",
            )
        self._used.add(ticket.ticket_id)
        return None

    # ---------------------------------------------------------------- stats
    def stats(self) -> dict[str, int]:
        by_code: dict[str, int] = {}
        for rej in self.rejections:
            by_code[rej.code.value] = by_code.get(rej.code.value, 0) + 1
        return {
            "tickets_issued": len(self._issued),
            "tickets_used": len(self._used),
            "rejections": len(self.rejections),
            "security_findings": len(self.findings),
            **by_code,
        }
