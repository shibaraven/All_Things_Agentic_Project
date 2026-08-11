"""Read-only tools exposed to the ADK Agent Fleet.

These tools return advice and evidence only. They cannot access the live
``World`` object, issue ActionTickets, or mutate vehicle state.
"""

from __future__ import annotations

from typing import Any

from shiftzero_core.contracts import AGENT_ALLOWED_ACTIONS, OPTIONAL_PARAMS, REQUIRED_PARAMS
from shiftzero_core.safety_kernel import screen_text


def score_fleet_candidate(
    agv_id: str,
    distance: int,
    battery: float,
    reserve: float,
    priority: int,
    station_queue: int = 0,
    route_conflict: bool = False,
) -> dict[str, Any]:
    """Score one candidate without changing fleet state."""
    headroom = battery - reserve
    score = distance + max(0.0, 40.0 - headroom) * 0.35 + priority * 2 + station_queue * 4
    if route_conflict:
        score += 1_000
    return {
        "agv_id": agv_id,
        "eligible": battery >= reserve and not route_conflict,
        "score": round(score, 3),
        "battery_headroom": round(headroom, 3),
        "route_conflict": route_conflict,
    }


def query_warehouse_constraint(
    source: str,
    destination: str,
    source_online: bool = True,
    destination_online: bool = True,
    destination_queue: int = 0,
) -> dict[str, Any]:
    """Return capacity and station constraints for a proposed transport."""
    allowed = source_online and destination_online and destination_queue < 4
    return {
        "source": source,
        "destination": destination,
        "allowed": allowed,
        "destination_queue": destination_queue,
        "reason": "available" if allowed else "station unavailable or queue capacity reached",
    }


def classify_recovery(
    incident_type: str,
    agv_id: str,
    loaded: bool = False,
    battery: float = 100.0,
    reserve: float = 25.0,
) -> dict[str, Any]:
    """Describe an allowed recovery sequence without executing it."""
    kind = incident_type.strip().upper()
    if kind == "BLOCKED":
        actions = ["PAUSE_AGV", "REASSIGN_TASK" if not loaded else "REROUTE", "RESUME_AGV"]
    elif kind == "LOW_BATTERY":
        actions = ["REASSIGN_TASK", "REQUEST_CHARGE"] if not loaded else ["REROUTE", "REQUEST_CHARGE"]
    elif kind == "DISCONNECTED":
        actions = ["PAUSE_AGV", "REASSIGN_TASK"]
    elif kind == "STATION_OFFLINE":
        actions = ["PAUSE_AGV", "REROUTE"]
    else:
        actions = ["PAUSE_AGV"]
    return {
        "incident_type": kind,
        "agv_id": agv_id,
        "loaded": loaded,
        "battery": battery,
        "below_reserve": battery < reserve,
        "recommended_actions": actions,
        "requires_safety_kernel": True,
    }


def screen_untrusted_content(text: str, source: str = "external-tool") -> dict[str, Any]:
    """Apply the deterministic ingress policy used beneath Model Armor."""
    finding = screen_text(text, source, tick=0)
    return {
        "allowed": finding is None,
        "category": finding.category if finding else None,
        "blocked_reason": finding.blocked_reason if finding else None,
    }


def authorize_proposal(agent_id: str, action_type: str, parameters: dict[str, Any]) -> dict[str, Any]:
    """Check the application IAM map and exact parameter schema."""
    allowed_actions = AGENT_ALLOWED_ACTIONS.get(agent_id, frozenset())
    required = REQUIRED_PARAMS.get(action_type)
    optional = OPTIONAL_PARAMS.get(action_type, frozenset())
    keys = frozenset(parameters)
    if action_type not in allowed_actions:
        return {"authorized": False, "reason": "identity is not allowed to propose this action"}
    if required is None or not required.issubset(keys):
        return {"authorized": False, "reason": "missing required parameters"}
    if keys - required - optional:
        return {"authorized": False, "reason": "unknown parameters fail closed"}
    return {"authorized": True, "reason": "proposal may proceed to the Safety Kernel"}
