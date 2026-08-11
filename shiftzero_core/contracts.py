"""
ShiftZero — contracts.py
========================
版本化的資料契約。這一層完全不含 LLM，也不 import 任何雲端 SDK，
因此可以在本機 headless 執行、可以被 pytest 覆蓋，也可以直接搬到
Cloud Run / Agent Runtime 上使用。

對應規劃書：§9.1 核心實體、§9.3 ActionProposal、§8.2 Hard Safety Policies。

設計原則
--------
1. Agent 只能輸出 typed ActionProposal，不得輸出自由文字命令。
2. 任何未知 action_type / 未知欄位 → fail closed（SP-08）。
3. 所有時間以「模擬邏輯時鐘（epoch 秒）」表示，確保 replay 可重現。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Optional

CONTRACT_VERSION = "1.0.0"
POLICY_VERSION = "sp-2026.08.08"


# --------------------------------------------------------------------------
# 列舉
# --------------------------------------------------------------------------
class ActionType(str, Enum):
    """Safety Kernel 認識的全部 action。不在此列的一律 fail closed。"""

    ASSIGN_TASK = "ASSIGN_TASK"
    REASSIGN_TASK = "REASSIGN_TASK"
    REROUTE = "REROUTE"
    REQUEST_CHARGE = "REQUEST_CHARGE"
    PAUSE_AGV = "PAUSE_AGV"
    RESUME_AGV = "RESUME_AGV"
    CANCEL_TASK = "CANCEL_TASK"


#: 明確列出「Agent 永遠不得提出」的動作。這些字串刻意保留，
#: 讓 prompt injection demo 可以打在一個具名的拒絕原因上，而不是打在
#: 「不認識的字串」這種模糊結果上。
FORBIDDEN_ACTION_NAMES = frozenset(
    {
        "SET_SPEED_LIMIT",
        "SET_SAFETY_THRESHOLD",
        "CLEAR_EMERGENCY_STOP",
        "DISABLE_SAFETY_KERNEL",
        "OVERRIDE_ZONE_LOCK",
    }
)


class RejectCode(str, Enum):
    """拒絕原因碼。UI 與 audit log 都直接使用這組值。"""

    OK = "OK"
    R_UNKNOWN_ACTION = "R_UNKNOWN_ACTION"          # SP-08
    R_FORBIDDEN_ACTION = "R_FORBIDDEN_ACTION"      # SP-07
    R_UNKNOWN_FIELD = "R_UNKNOWN_FIELD"            # SP-08
    R_SCHEMA = "R_SCHEMA"                          # SP-08
    R_IDENTITY = "R_IDENTITY"                      # least privilege
    R_EMERGENCY_STOP = "R_EMERGENCY_STOP"          # SP-01
    R_BATTERY_RESERVE = "R_BATTERY_RESERVE"        # SP-02
    R_NO_RESERVATION = "R_NO_RESERVATION"          # SP-03
    R_DOUBLE_ASSIGN = "R_DOUBLE_ASSIGN"            # SP-04
    R_VEHICLE_UNAVAILABLE = "R_VEHICLE_UNAVAILABLE"  # SP-05
    R_PROPOSAL_EXPIRED = "R_PROPOSAL_EXPIRED"      # SP-06 (proposal TTL)
    R_TICKET_INVALID = "R_TICKET_INVALID"          # SP-06 (ticket 過期/重用/身分不符)
    R_DUPLICATE = "R_DUPLICATE"                    # idempotency
    R_STALE_STATE = "R_STALE_STATE"                # state version 不符
    R_STATION_OFFLINE = "R_STATION_OFFLINE"
    R_UNKNOWN_ENTITY = "R_UNKNOWN_ENTITY"


class IncidentType(str, Enum):
    BLOCKED = "BLOCKED"
    LOW_BATTERY = "LOW_BATTERY"
    DISCONNECTED = "DISCONNECTED"
    STATION_OFFLINE = "STATION_OFFLINE"
    SECURITY_BLOCKED = "SECURITY_BLOCKED"


class AgvMode(str, Enum):
    IDLE = "IDLE"
    TO_SOURCE = "TO_SOURCE"
    LOADING = "LOADING"
    TO_DEST = "TO_DEST"
    UNLOADING = "UNLOADING"
    TO_CHARGER = "TO_CHARGER"
    CHARGING = "CHARGING"
    PAUSED = "PAUSED"
    BLOCKED = "BLOCKED"
    DISCONNECTED = "DISCONNECTED"


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    ASSIGNED = "ASSIGNED"
    IN_TRANSIT = "IN_TRANSIT"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class ShiftState(str, Enum):
    DRAFT = "DRAFT"
    PLANNING = "PLANNING"
    RUNNING = "RUNNING"
    DEGRADED = "DEGRADED"
    RECOVERING = "RECOVERING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


# --------------------------------------------------------------------------
# Agent Identity / least privilege（§4.2、§8.3 privilege escalation）
# --------------------------------------------------------------------------
#: 每個 agent_id 只能提出被列出的 action。這張表就是 IAM 的應用層映射，
#: 部署到 Google Cloud 時同一張表會鏡射成 per-tool IAM binding。
AGENT_ALLOWED_ACTIONS: dict[str, frozenset[str]] = {
    "operations-commander-v1": frozenset(),  # 只做規劃，不得直接動車
    "fleet-dispatcher-v1": frozenset(
        {
            ActionType.ASSIGN_TASK.value,
            ActionType.REASSIGN_TASK.value,
            ActionType.REROUTE.value,
            ActionType.REQUEST_CHARGE.value,
        }
    ),
    "recovery-coordinator-v1": frozenset(
        {
            ActionType.PAUSE_AGV.value,
            ActionType.RESUME_AGV.value,
            ActionType.REASSIGN_TASK.value,
            ActionType.REROUTE.value,
            ActionType.CANCEL_TASK.value,
        }
    ),
    "warehouse-context-v1": frozenset(),
    "security-governance-v1": frozenset(),  # 有 finding 權，無派車權
}

#: 每個 action 必填 / 選填的參數。多一個欄位就 fail closed。
REQUIRED_PARAMS: dict[str, frozenset[str]] = {
    ActionType.ASSIGN_TASK.value: frozenset({"agv_id", "task_id"}),
    ActionType.REASSIGN_TASK.value: frozenset({"from_agv", "to_agv", "task_id"}),
    ActionType.REROUTE.value: frozenset({"agv_id"}),
    ActionType.REQUEST_CHARGE.value: frozenset({"agv_id", "station_id"}),
    ActionType.PAUSE_AGV.value: frozenset({"agv_id"}),
    ActionType.RESUME_AGV.value: frozenset({"agv_id"}),
    ActionType.CANCEL_TASK.value: frozenset({"task_id"}),
}

OPTIONAL_PARAMS: dict[str, frozenset[str]] = {
    ActionType.ASSIGN_TASK.value: frozenset({"reason_code"}),
    ActionType.REASSIGN_TASK.value: frozenset({"reason_code"}),
    ActionType.REROUTE.value: frozenset({"reason_code", "avoid_nodes"}),
    ActionType.REQUEST_CHARGE.value: frozenset({"reason_code"}),
    ActionType.PAUSE_AGV.value: frozenset({"reason_code"}),
    ActionType.RESUME_AGV.value: frozenset({"reason_code"}),
    ActionType.CANCEL_TASK.value: frozenset({"reason_code"}),
}


# --------------------------------------------------------------------------
# 核心 dataclass
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ActionProposal:
    """Agent 唯一被允許的輸出格式（§9.3）。"""

    action_id: str
    shift_id: str
    agent_id: str
    action_type: str            # 刻意用 str 而非 Enum：未知值必須進得來才能被拒絕
    target_id: str
    parameters: dict[str, Any]
    idempotency_key: str
    issued_at: int              # 邏輯時鐘（epoch 秒）
    expires_at: int             # proposal TTL
    rationale: str = ""
    confidence: float = 1.0
    constraints: tuple[str, ...] = ()
    state_version: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "shift_id": self.shift_id,
            "agent_id": self.agent_id,
            "action_type": self.action_type,
            "target_id": self.target_id,
            "parameters": dict(self.parameters),
            "constraints": list(self.constraints),
            "idempotency_key": self.idempotency_key,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "rationale": self.rationale,
            "confidence": self.confidence,
            "state_version": self.state_version,
        }


@dataclass(frozen=True)
class ActionTicket:
    """Safety Kernel 驗證通過後才會發出。沒有 ticket 就沒有 command。"""

    ticket_id: str
    action_id: str
    agent_id: str
    action_type: str
    policy_version: str
    constraints_checked: tuple[str, ...]
    issued_at: int
    expires_at: int
    state_version: int


@dataclass(frozen=True)
class Rejection:
    """驗證失敗。必須帶明確 reason code（FR-018）。"""

    action_id: str
    agent_id: str
    code: RejectCode
    detail: str
    policy_version: str = POLICY_VERSION

    @property
    def ok(self) -> bool:
        return False


@dataclass(frozen=True)
class ExecutionResult:
    action_id: str
    ticket_id: str
    status: str                 # EXECUTED / FAILED / SKIPPED
    started_at: int
    completed_at: int
    state_version: int
    error_code: Optional[str] = None
    detail: str = ""


@dataclass
class AuditEvent:
    """每個決策都要能回答：誰、何時、為何、做了什麼、結果如何（§3.2 Explain）。"""

    trace_id: str
    tick: int
    actor: str
    event_type: str
    decision: str
    input_hash: str
    detail: str = ""
    correlation_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "tick": self.tick,
            "actor": self.actor,
            "event_type": self.event_type,
            "decision": self.decision,
            "input_hash": self.input_hash,
            "detail": self.detail,
            "correlation_id": self.correlation_id,
        }


@dataclass
class Incident:
    incident_id: str
    type: IncidentType
    severity: str
    source: str
    affected_entities: list[str]
    detected_at: int
    status: str = "OPEN"          # OPEN / RECOVERING / CLOSED
    resolution: str = ""
    closed_at: Optional[int] = None
    last_attempt_tick: int = -(10**9)
    attempts: int = 0


@dataclass
class SecurityFinding:
    finding_id: str
    source: str
    category: str                 # PROMPT_INJECTION / PRIVILEGE_ESCALATION / ...
    blocked_reason: str
    excerpt: str
    detected_at: int
    trace_id: str = ""


# --------------------------------------------------------------------------
# 工具函式
# --------------------------------------------------------------------------
def stable_hash(payload: Any) -> str:
    """決定性雜湊。用於 audit input_hash 與 replay 驗證。"""
    blob = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def make_id(prefix: str, *parts: Any) -> str:
    """不使用 uuid4：要可重播就不能有隨機性。"""
    return f"{prefix}-{stable_hash(list(parts))}"


def schema_errors(proposal: ActionProposal) -> Optional[tuple[RejectCode, str]]:
    """純 schema 檢查（不看世界狀態）。回傳 None 表示通過。"""
    if proposal.action_type in FORBIDDEN_ACTION_NAMES:
        return (
            RejectCode.R_FORBIDDEN_ACTION,
            f"agents may never propose {proposal.action_type} (SP-07)",
        )

    known = {a.value for a in ActionType}
    if proposal.action_type not in known:
        return (RejectCode.R_UNKNOWN_ACTION, f"unknown action_type {proposal.action_type!r} (SP-08)")

    if not isinstance(proposal.parameters, dict):
        return (RejectCode.R_SCHEMA, "parameters must be an object")

    required = REQUIRED_PARAMS[proposal.action_type]
    optional = OPTIONAL_PARAMS[proposal.action_type]
    keys = set(proposal.parameters)

    missing = required - keys
    if missing:
        return (RejectCode.R_SCHEMA, f"missing parameters: {sorted(missing)}")

    unknown = keys - required - optional
    if unknown:
        return (RejectCode.R_UNKNOWN_FIELD, f"unknown parameters: {sorted(unknown)} (SP-08)")

    if not proposal.idempotency_key:
        return (RejectCode.R_SCHEMA, "idempotency_key is mandatory")

    if not (0.0 <= float(proposal.confidence) <= 1.0):
        return (RejectCode.R_SCHEMA, "confidence out of range")

    return None


def as_jsonl(events: Iterable[AuditEvent]) -> str:
    return "\n".join(json.dumps(e.to_dict(), ensure_ascii=False) for e in events)
