"""
ShiftZero — world.py
====================
9 台 AGV 的 deterministic digital twin，也就是規劃書所說的
「Operational Twin / live source of truth」（§5.2）。

關鍵紀律
--------
* 這裡沒有任何 LLM。pose、battery、node reservation、emergency state
  只存在這一層，永遠不從 Memory 讀。
* 同一個 seed + 同一組 injection 序列 → 完全相同的結果（NFR-001）。
* 世界狀態只能被 ActionTicket 改變。沒有 ticket 就沒有 command（FR-018）。

地圖：7 列 × 10 行的 route graph。取貨/卸貨站刻意放在**內側節點**
（四個鄰居）而不是角落（兩個鄰居），因為角落站點會在 9 車規模下
產生無法用局部規則解開的 head-on deadlock。

     c0  c1  c2  c3  c4  c5  c6  c7  c8  c9
r0    .   .   .   ·   .   .   .   .   .   .
r1    .  I1   .   .   ·   .   .   .  O1   .
r2    .   .   .   ·   .   .   ·   .  O2   .
r3   C1   .   .   .   .   .   .   .   .  C2
r4    .   .   .   ·   .   .   ·   .  O3   .
r5    .  I2   .   .   ·   .   .   .  O4   .
r6    .   .   .   ·   .   .   .   .   .   .
（· 為 AGV 起始停放位置）
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from .contracts import (
    ActionTicket,
    ActionType,
    AgvMode,
    Incident,
    IncidentType,
    TaskStatus,
    make_id,
    stable_hash,
)

ROWS, COLS = 7, 10

# --- 物理常數（單一來源，Safety Kernel 也讀這裡） ---------------------------
SECONDS_PER_TICK = 5
MOVE_COST_PCT = 0.5        # 每移動一格消耗的電量
IDLE_COST_PCT = 0.005
CHARGE_RATE_PCT = 3.5      # 每 tick 充電
CHARGE_TARGET_PCT = 85.0
LOAD_TICKS = 2
UNLOAD_TICKS = 2
# 交通規則。三個門檻各自解一種失敗模式：
#   繞路 (detour)  → 一般壅塞
#   讓路 (sidestep) → head-on deadlock
#   BLOCKED        → 真正的結構性阻塞，才需要 Recovery Agent 介入
WAIT_REPLAN_TICKS = 3
WAIT_SIDESTEP_TICKS = 8
WAIT_BLOCKED_TICKS = 30    # 排隊是正常現象；超過這個門檻才算 BLOCKED incident
DETOUR_SLACK = 6           # 可接受的繞路額外長度
REPLAN_COOLDOWN_TICKS = 5  # 避免在站點前來回震盪


def node_id(r: int, c: int) -> str:
    return f"r{r}c{c}"


STATIONS: dict[str, str] = {
    "I1": node_id(1, 1),
    "I2": node_id(5, 1),
    "C1": node_id(3, 0),
    "C2": node_id(3, 9),
    "O1": node_id(1, 8),
    "O2": node_id(2, 8),
    "O3": node_id(4, 8),
    "O4": node_id(5, 8),
}
#: 每個充電站有兩個充電位。只給一個節點的話，9 台車的車隊會在
#: 充電樁前排隊到耗盡電量——這是實測出來的失效模式，不是理論問題。
CHARGER_BAYS: dict[str, list[str]] = {
    "C1": [node_id(3, 0), node_id(2, 0)],
    "C2": [node_id(3, 9), node_id(2, 9)],
}

INBOUND = ["I1", "I2"]
OUTBOUND = ["O1", "O2", "O3", "O4"]
CHARGERS = ["C1", "C2"]
NODE_TO_STATION = {v: k for k, v in STATIONS.items()}


# --------------------------------------------------------------------------
@dataclass
class Agv:
    agv_id: str
    node: str
    battery: float
    mode: str = AgvMode.IDLE.value
    suspended_mode: Optional[str] = None
    task_id: Optional[str] = None
    load_id: Optional[str] = None
    path: list[str] = field(default_factory=list)
    timer: int = 0
    wait_ticks: int = 0
    charger: Optional[str] = None
    charger_node: Optional[str] = None
    replan_cooldown: int = 0
    last_seen_tick: int = 0
    moves: int = 0

    @property
    def available(self) -> bool:
        return self.mode in (AgvMode.IDLE.value,) and self.task_id is None

    @property
    def healthy(self) -> bool:
        return self.mode not in (
            AgvMode.BLOCKED.value,
            AgvMode.DISCONNECTED.value,
            AgvMode.PAUSED.value,
        )


@dataclass
class TransportTask:
    task_id: str
    source: str          # station key
    destination: str     # station key
    priority: int
    status: str = TaskStatus.PENDING.value
    assigned_agv: Optional[str] = None
    created_tick: int = 0
    completed_tick: Optional[int] = None


# --------------------------------------------------------------------------
class World:
    """Deterministic operational twin。"""

    def __init__(
        self,
        seed: int = 20260808,
        n_agvs: int = 9,
        n_tasks: int = 42,
        min_battery_reserve: float = 25.0,
        deadline_tick: int = 1080,        # 18:00 → 19:30 @ 5 s/tick
        shift_id: str = "shift-demo-42",
    ) -> None:
        self.seed = seed
        self.shift_id = shift_id
        self.tick_count = 0
        self.state_version = 0
        self.min_battery_reserve = min_battery_reserve
        self.deadline_tick = deadline_tick
        self.emergency_stop = False

        self.nodes: list[str] = [node_id(r, c) for r in range(ROWS) for c in range(COLS)]
        self.blocked_nodes: set[str] = set()
        self.offline_stations: set[str] = set()

        self.agvs: dict[str, Agv] = {}
        self.tasks: dict[str, TransportTask] = {}
        self.incidents: dict[str, Incident] = {}
        self.occupancy: dict[str, str] = {}
        self.safety_violations: list[str] = []
        self.completed_ticket_ids: set[str] = set()
        self._tick_incidents: list[Incident] = []

        self._rng_state = seed
        self._spawn_agvs(n_agvs)
        self._spawn_tasks(n_tasks)

    # -- deterministic pseudo random（刻意自己寫，避免依賴 random 模組版本） --
    def _rand(self) -> float:
        self._rng_state = (1103515245 * self._rng_state + 12345) % (2**31)
        return self._rng_state / (2**31)

    # ---------------------------------------------------------------- setup
    def _spawn_agvs(self, n: int) -> None:
        home = [
            node_id(0, 3), node_id(1, 4), node_id(2, 3), node_id(3, 4), node_id(4, 3),
            node_id(5, 4), node_id(6, 3), node_id(2, 6), node_id(4, 6),
        ]
        for i in range(n):
            agv_id = f"AGV{i + 1:02d}"
            battery = round(45.0 + self._rand() * 40.0, 1)
            node = home[i % len(home)]
            self.agvs[agv_id] = Agv(agv_id=agv_id, node=node, battery=battery)
            self.occupancy[node] = agv_id

    def _spawn_tasks(self, n: int) -> None:
        for i in range(n):
            src = INBOUND[i % len(INBOUND)]
            dst = OUTBOUND[int(self._rand() * len(OUTBOUND)) % len(OUTBOUND)]
            priority = 1 if i % 7 == 0 else 2 if i % 3 == 0 else 3
            tid = f"task-{100 + i}"
            self.tasks[tid] = TransportTask(
                task_id=tid, source=src, destination=dst, priority=priority
            )

    # ------------------------------------------------------------- geometry
    @staticmethod
    def coords(node: str) -> tuple[int, int]:
        r, c = node[1:].split("c")
        return int(r), int(c)

    def neighbors(self, node: str) -> list[str]:
        r, c = self.coords(node)
        out = []
        for dr, dc in ((-1, 0), (0, -1), (0, 1), (1, 0)):
            nr, nc = r + dr, c + dc
            if 0 <= nr < ROWS and 0 <= nc < COLS:
                out.append(node_id(nr, nc))
        return out

    def manhattan(self, a: str, b: str) -> int:
        ar, ac = self.coords(a)
        br, bc = self.coords(b)
        return abs(ar - br) + abs(ac - bc)

    def plan_path(
        self, start: str, goal: str, avoid: Optional[Iterable[str]] = None
    ) -> Optional[list[str]]:
        """A* over the route graph。回傳不含 start 的節點序列；無路徑回傳 None。"""
        blocked = set(self.blocked_nodes)
        # 失聯車輛是靜態障礙物，不是「等一下就會走」的鄰居。
        blocked |= {
            a.node for a in self.agvs.values() if a.mode == AgvMode.DISCONNECTED.value
        }
        if avoid:
            blocked |= set(avoid)
        blocked.discard(start)
        if goal in blocked:
            return None
        if start == goal:
            return []

        open_heap: list[tuple[int, int, str]] = [(self.manhattan(start, goal), 0, start)]
        came: dict[str, str] = {}
        best: dict[str, int] = {start: 0}
        while open_heap:
            _, g, cur = heapq.heappop(open_heap)
            if cur == goal:
                path = [cur]
                while path[-1] != start:
                    path.append(came[path[-1]])
                path.reverse()
                return path[1:]
            if g > best.get(cur, 1 << 30):
                continue
            for nxt in self.neighbors(cur):  # neighbors() 已是固定順序 → 決定性
                if nxt in blocked:
                    continue
                ng = g + 1
                if ng < best.get(nxt, 1 << 30):
                    best[nxt] = ng
                    came[nxt] = cur
                    heapq.heappush(open_heap, (ng + self.manhattan(nxt, goal), ng, nxt))
        return None

    # ---------------------------------------------------------- ticket exec
    def execute(self, ticket: ActionTicket, parameters: dict[str, Any]) -> tuple[str, str]:
        """執行已驗證的 ticket。回傳 (status, detail)。"""
        if ticket.ticket_id in self.completed_ticket_ids:
            return "SKIPPED", "ticket already consumed"
        self.completed_ticket_ids.add(ticket.ticket_id)

        at = ticket.action_type
        try:
            if at == ActionType.ASSIGN_TASK.value:
                detail = self._assign(parameters["agv_id"], parameters["task_id"])
            elif at == ActionType.REASSIGN_TASK.value:
                detail = self._reassign(
                    parameters["from_agv"], parameters["to_agv"], parameters["task_id"]
                )
            elif at == ActionType.REROUTE.value:
                detail = self._reroute(parameters["agv_id"])
            elif at == ActionType.REQUEST_CHARGE.value:
                detail = self._request_charge(parameters["agv_id"], parameters["station_id"])
            elif at == ActionType.PAUSE_AGV.value:
                detail = self._pause(parameters["agv_id"])
            elif at == ActionType.RESUME_AGV.value:
                detail = self._resume(parameters["agv_id"])
            elif at == ActionType.CANCEL_TASK.value:
                detail = self._cancel(parameters["task_id"])
            else:  # pragma: no cover - kernel 應已擋掉
                return "FAILED", f"unsupported action {at}"
        except KeyError as exc:
            return "FAILED", f"missing entity {exc}"

        self.state_version += 1
        return "EXECUTED", detail

    def _goal_for_mode(self, agv: Agv) -> Optional[str]:
        task = self.tasks.get(agv.task_id) if agv.task_id else None
        mode = agv.mode
        if mode in (AgvMode.BLOCKED.value, AgvMode.PAUSED.value) and agv.suspended_mode:
            mode = agv.suspended_mode
        if mode == AgvMode.TO_SOURCE.value and task:
            return STATIONS[task.source]
        if mode == AgvMode.TO_DEST.value and task:
            return STATIONS[task.destination]
        if mode == AgvMode.TO_CHARGER.value and agv.charger:
            return agv.charger_node or STATIONS[agv.charger]
        return None

    def _assign(self, agv_id: str, task_id: str) -> str:
        agv, task = self.agvs[agv_id], self.tasks[task_id]
        agv.task_id = task_id
        agv.mode = AgvMode.TO_SOURCE.value
        agv.wait_ticks = 0
        agv.path = self.plan_path(agv.node, STATIONS[task.source]) or []
        task.assigned_agv = agv_id
        task.status = TaskStatus.ASSIGNED.value
        return f"{task_id} -> {agv_id} ({task.source}->{task.destination})"

    def _reassign(self, from_agv: str, to_agv: str, task_id: str) -> str:
        src, dst, task = self.agvs[from_agv], self.agvs[to_agv], self.tasks[task_id]
        if src.task_id == task_id:
            src.task_id = None
            src.load_id = None
            src.path = []
            if src.mode in (AgvMode.BLOCKED.value, AgvMode.PAUSED.value):
                src.suspended_mode = AgvMode.IDLE.value
            elif src.mode in (AgvMode.TO_SOURCE.value, AgvMode.TO_DEST.value):
                src.mode = AgvMode.IDLE.value
        task.assigned_agv = None
        task.status = TaskStatus.PENDING.value
        self._assign(to_agv, task_id)
        return f"{task_id}: {from_agv} -> {to_agv}"

    def _reroute(self, agv_id: str) -> str:
        agv = self.agvs[agv_id]
        goal = self._goal_for_mode(agv)
        if goal is None:
            return f"{agv_id}: no active goal"
        path = self.plan_path(agv.node, goal)
        if path is None:
            return f"{agv_id}: no alternate route"
        agv.path = path
        agv.wait_ticks = 0
        return f"{agv_id}: rerouted, {len(path)} segments"

    def _request_charge(self, agv_id: str, station_id: str) -> str:
        agv = self.agvs[agv_id]
        agv.charger = station_id
        agv.charger_node = self.free_bay(station_id, exclude_agv=agv_id)
        agv.mode = AgvMode.TO_CHARGER.value
        agv.wait_ticks = 0
        agv.path = self.plan_path(agv.node, agv.charger_node) or []
        return f"{agv_id} -> charger {station_id}/{agv.charger_node} at {agv.battery:.1f}%"

    def free_bay(self, station_id: str, exclude_agv: str = "") -> str:
        bays = CHARGER_BAYS.get(station_id, [STATIONS[station_id]])
        targeted = {
            a.charger_node
            for a in self.agvs.values()
            if a.agv_id != exclude_agv and a.charger_node is not None
        }
        for bay in bays:
            holder = self.occupancy.get(bay)
            if bay not in targeted and (holder is None or holder == exclude_agv):
                return bay
        return bays[0]

    def _pause(self, agv_id: str) -> str:
        agv = self.agvs[agv_id]
        if agv.mode != AgvMode.DISCONNECTED.value:
            if agv.mode not in (AgvMode.PAUSED.value, AgvMode.BLOCKED.value) or (
                agv.suspended_mode is None
            ):
                agv.suspended_mode = agv.mode
            agv.mode = AgvMode.PAUSED.value
        agv.path = []
        return f"{agv_id} paused"

    def _resume(self, agv_id: str) -> str:
        agv = self.agvs[agv_id]
        suspended = agv.suspended_mode
        if suspended in (AgvMode.LOADING.value, AgvMode.UNLOADING.value) and agv.task_id:
            agv.mode = suspended
        elif suspended == AgvMode.TO_CHARGER.value and agv.charger:
            agv.mode = AgvMode.TO_CHARGER.value
        elif agv.task_id is None:
            agv.mode = AgvMode.IDLE.value
        elif agv.load_id is not None:
            agv.mode = AgvMode.TO_DEST.value
        else:
            agv.mode = AgvMode.TO_SOURCE.value
        agv.suspended_mode = None
        agv.wait_ticks = 0
        goal = self._goal_for_mode(agv)
        agv.path = (self.plan_path(agv.node, goal) or []) if goal else []
        return f"{agv_id} resumed"

    def _cancel(self, task_id: str) -> str:
        task = self.tasks[task_id]
        if task.assigned_agv:
            agv = self.agvs[task.assigned_agv]
            agv.task_id = None
            agv.load_id = None
            agv.path = []
            if agv.mode in (AgvMode.BLOCKED.value, AgvMode.PAUSED.value):
                agv.suspended_mode = AgvMode.IDLE.value
            else:
                agv.mode = AgvMode.IDLE.value
        task.assigned_agv = None
        task.status = TaskStatus.CANCELLED.value
        return f"{task_id} cancelled"

    # -------------------------------------------------------------- physics
    def tick(self) -> list[Incident]:
        """推進一個 tick，回傳本 tick 新增的 incident。"""
        self.tick_count += 1
        self._tick_incidents = []

        for agv_id in sorted(self.agvs):
            agv = self.agvs[agv_id]
            if agv.mode == AgvMode.DISCONNECTED.value:
                continue
            agv.last_seen_tick = self.tick_count

            if self.emergency_stop or agv.mode in (
                AgvMode.PAUSED.value,
                AgvMode.BLOCKED.value,
            ):
                agv.battery = max(0.0, agv.battery - IDLE_COST_PCT)
                continue

            if agv.mode == AgvMode.CHARGING.value:
                agv.battery = min(100.0, agv.battery + CHARGE_RATE_PCT)
                if agv.battery >= CHARGE_TARGET_PCT:
                    agv.charger = None
                    agv.charger_node = None
                    # 充電期間仍握有任務的車，充飽後必須回到原本的任務階段，
                    # 否則會變成「有 task 但 mode=IDLE」的孤兒狀態而永遠停擺。
                    if agv.task_id is not None:
                        agv.mode = (
                            AgvMode.TO_DEST.value if agv.load_id else AgvMode.TO_SOURCE.value
                        )
                    else:
                        agv.mode = AgvMode.IDLE.value
                    # 充飽後讓出充電位，否則車隊會被自己的閒置車輛鎖死。
                    self._sidestep(agv, exclude=set())
                    goal = self._goal_for_mode(agv)
                    if goal is not None and not agv.path:
                        agv.path = self.plan_path(agv.node, goal) or []
                continue

            if agv.mode in (AgvMode.LOADING.value, AgvMode.UNLOADING.value):
                agv.timer -= 1
                agv.battery = max(0.0, agv.battery - IDLE_COST_PCT)
                if agv.timer <= 0:
                    self._finish_handling(agv)
                continue

            if not agv.path and agv.mode == AgvMode.IDLE.value and agv.node in NODE_TO_STATION:
                # 閒置車輛不得佔用站點節點。少了這條規則，一台停在 outbound
                # port 上的空車會讓所有要卸貨的車永久卡住（實測過的死結）。
                self._sidestep(agv, exclude=set())
                continue

            if agv.path:
                self._step(agv)
            else:
                self._arrive(agv)

        for agv_id in sorted(self.agvs):
            agv = self.agvs[agv_id]
            if agv.battery <= 0.0 and agv.mode != AgvMode.CHARGING.value:
                marker = f"tick{self.tick_count}:{agv_id}:STRANDED_AT_ZERO_BATTERY"
                if not any(v.endswith("STRANDED_AT_ZERO_BATTERY") and agv_id in v for v in self.safety_violations):
                    self.safety_violations.append(marker)

        self._tick_incidents.extend(self._detect_low_battery())
        self._verify_recoveries()
        self.state_version += 1
        return self._tick_incidents

    def _step(self, agv: Agv) -> None:
        if agv.replan_cooldown > 0:
            agv.replan_cooldown -= 1
        nxt = agv.path[0]

        if nxt in self.blocked_nodes:
            goal = self._goal_for_mode(agv)
            path = self.plan_path(agv.node, goal) if goal else None
            if path:
                agv.path = path
                agv.wait_ticks = 0
            else:
                self._raise_blocked(agv, "no route to goal")
            return

        holder = self.occupancy.get(nxt)
        if holder is not None and holder != agv.agv_id:
            agv.wait_ticks += 1
            agv.battery = max(0.0, agv.battery - IDLE_COST_PCT)
            other = self.agvs[holder]

            # (0) 閒置車輛一律讓路。停著不動的空車是這張圖上最常見的死結來源。
            if other.mode == AgvMode.IDLE.value and not other.path:
                if self._sidestep(other, exclude={agv.node}):
                    return

            # (1) head-on deadlock：對方下一步就是我現在的位置 → 由 id 較小者讓路
            if other.path and other.path[0] == agv.node and agv.agv_id < other.agv_id:
                if self._sidestep(agv, exclude={nxt}):
                    return

            # (2) 週期性繞路，但有冷卻時間，避免在站點前來回震盪
            if agv.wait_ticks >= WAIT_REPLAN_TICKS and agv.replan_cooldown == 0:
                goal = self._goal_for_mode(agv)
                alt = self.plan_path(agv.node, goal, avoid={nxt}) if goal else None
                if alt is not None and len(alt) <= len(agv.path) + DETOUR_SLACK:
                    agv.path = alt
                    agv.wait_ticks = 0
                    agv.replan_cooldown = REPLAN_COOLDOWN_TICKS
                    return

            # (3) 塞太久 → 讓路到任一空鄰居，仍不行才判定 BLOCKED
            if agv.wait_ticks == WAIT_SIDESTEP_TICKS:
                self._sidestep(agv, exclude={nxt})
            elif agv.wait_ticks >= WAIT_BLOCKED_TICKS:
                self._raise_blocked(agv, f"congestion at {nxt}")
            return

        # SP-03：進入前必須取得該節點的 reservation（此處即 occupancy lock）
        del self.occupancy[agv.node]
        self.occupancy[nxt] = agv.agv_id
        agv.node = nxt
        agv.path.pop(0)
        agv.wait_ticks = 0
        agv.moves += 1
        agv.battery = max(0.0, agv.battery - MOVE_COST_PCT)
        if not agv.path:
            self._arrive(agv)

    def _sidestep(self, agv: Agv, exclude: set[str]) -> bool:
        """讓路：移到任一可用鄰居，再重新規劃路徑。"""
        goal = self._goal_for_mode(agv)
        for cand in self.neighbors(agv.node):
            if cand in exclude or cand in self.blocked_nodes or cand in self.occupancy:
                continue
            del self.occupancy[agv.node]
            self.occupancy[cand] = agv.agv_id
            agv.node = cand
            agv.moves += 1
            agv.wait_ticks = 0
            agv.replan_cooldown = REPLAN_COOLDOWN_TICKS
            agv.battery = max(0.0, agv.battery - MOVE_COST_PCT)
            agv.path = (self.plan_path(cand, goal) or []) if goal else []
            if not agv.path:
                self._arrive(agv)
            return True
        return False

    def _arrive(self, agv: Agv) -> None:
        # 「路徑為空」不等於「已抵達」。路徑可能是被中斷或被清空的；
        # 若尚未真的站在目標節點上就切換到 LOADING，任務會憑空完成。
        goal = self._goal_for_mode(agv)
        if goal is not None and agv.node != goal:
            path = self.plan_path(agv.node, goal)
            if path:
                agv.path = path
            else:
                self._raise_blocked(agv, "no route to goal")
            return

        if agv.mode == AgvMode.TO_SOURCE.value:
            agv.mode = AgvMode.LOADING.value
            agv.timer = LOAD_TICKS
        elif agv.mode == AgvMode.TO_DEST.value:
            agv.mode = AgvMode.UNLOADING.value
            agv.timer = UNLOAD_TICKS
        elif agv.mode == AgvMode.TO_CHARGER.value:
            agv.mode = AgvMode.CHARGING.value
        else:
            agv.battery = max(0.0, agv.battery - IDLE_COST_PCT)

    def _finish_handling(self, agv: Agv) -> None:
        task = self.tasks.get(agv.task_id) if agv.task_id else None
        if agv.mode == AgvMode.LOADING.value and task:
            agv.load_id = task.task_id
            task.status = TaskStatus.IN_TRANSIT.value
            agv.mode = AgvMode.TO_DEST.value
            path = self.plan_path(agv.node, STATIONS[task.destination])
            if path is None:
                self._raise_blocked(agv, "no route from source to destination")
                return
            agv.path = path
            if not path:
                agv.mode = AgvMode.UNLOADING.value
                agv.timer = UNLOAD_TICKS
        elif agv.mode == AgvMode.UNLOADING.value and task:
            task.status = TaskStatus.COMPLETED.value
            task.completed_tick = self.tick_count
            agv.load_id = None
            agv.task_id = None
            agv.mode = AgvMode.IDLE.value
            self._sidestep(agv, exclude=set())
        else:
            agv.mode = AgvMode.IDLE.value

    # ------------------------------------------------------------ incidents
    def _open_incident(
        self, itype: IncidentType, severity: str, source: str, entities: list[str]
    ) -> Optional[Incident]:
        key = make_id("inc", itype.value, sorted(entities))
        if key in self.incidents and self.incidents[key].status != "CLOSED":
            return None
        inc = Incident(
            incident_id=key,
            type=itype,
            severity=severity,
            source=source,
            affected_entities=entities,
            detected_at=self.tick_count,
        )
        self.incidents[key] = inc
        return inc

    def _raise_blocked(self, agv: Agv, why: str) -> None:
        if agv.mode != AgvMode.BLOCKED.value:
            agv.suspended_mode = agv.mode
        agv.mode = AgvMode.BLOCKED.value
        agv.path = []
        entities = [agv.agv_id] + ([agv.task_id] if agv.task_id else [])
        inc = self._open_incident(IncidentType.BLOCKED, "HIGH", f"{agv.agv_id}:{why}", entities)
        if inc:
            self._tick_incidents.append(inc)

    def _detect_low_battery(self) -> list[Incident]:
        out: list[Incident] = []
        for agv_id in sorted(self.agvs):
            agv = self.agvs[agv_id]
            if agv.mode in (
                AgvMode.CHARGING.value,
                AgvMode.TO_CHARGER.value,
                AgvMode.DISCONNECTED.value,
            ):
                continue
            if agv.battery < self.min_battery_reserve:
                inc = self._open_incident(
                    IncidentType.LOW_BATTERY,
                    "MEDIUM",
                    f"{agv_id}:battery={agv.battery:.1f}",
                    [agv_id] + ([agv.task_id] if agv.task_id else []),
                )
                if inc:
                    out.append(inc)
        return out

    def _verify_recoveries(self) -> None:
        """FR-015：incident 只能在確認 renewed progress 之後才關閉。"""
        for inc in self.incidents.values():
            if inc.status != "RECOVERING":
                continue
            tasks = [e for e in inc.affected_entities if e.startswith("task-")]
            agvs = [e for e in inc.affected_entities if e.startswith("AGV")]
            ok = True
            for tid in tasks:
                task = self.tasks[tid]
                if task.status == TaskStatus.COMPLETED.value:
                    continue
                holder = task.assigned_agv
                if holder is None or not self.agvs[holder].healthy:
                    ok = False
                    break
            if inc.type == IncidentType.LOW_BATTERY and agvs:
                agv = self.agvs[agvs[0]]
                if agv.mode not in (AgvMode.CHARGING.value, AgvMode.TO_CHARGER.value) and (
                    agv.battery < self.min_battery_reserve
                ):
                    ok = False
            elif inc.type == IncidentType.BLOCKED and agvs:
                if any(
                    self.agvs[agv_id].mode in (AgvMode.BLOCKED.value, AgvMode.PAUSED.value)
                    for agv_id in agvs
                ):
                    ok = False
            elif inc.type == IncidentType.DISCONNECTED and agvs:
                if any(self.agvs[agv_id].mode == AgvMode.DISCONNECTED.value for agv_id in agvs):
                    ok = False
            elif inc.type == IncidentType.STATION_OFFLINE:
                if any(station in self.offline_stations for station in inc.affected_entities):
                    ok = False
            if ok:
                inc.status = "CLOSED"
                inc.closed_at = self.tick_count
                inc.resolution = "verified by renewed progress"

    # ------------------------------------------------------------ injection
    def inject_block_zone(self, agv_id: str) -> list[str]:
        """封鎖某台 AGV 周圍的節點，模擬現場區域封閉（demo 事件 1）。"""
        agv = self.agvs[agv_id]
        blocked = [n for n in self.neighbors(agv.node)]
        self.blocked_nodes.update(blocked)
        self.state_version += 1
        return blocked

    def inject_battery(self, agv_id: str, pct: float) -> None:
        self.agvs[agv_id].battery = pct
        self.state_version += 1

    def inject_disconnect(self, agv_id: str) -> Incident:
        agv = self.agvs[agv_id]
        agv.mode = AgvMode.DISCONNECTED.value
        agv.path = []
        inc = self._open_incident(
            IncidentType.DISCONNECTED,
            "HIGH",
            f"{agv_id}:heartbeat lost",
            [agv_id] + ([agv.task_id] if agv.task_id else []),
        )
        self.state_version += 1
        return inc  # type: ignore[return-value]

    def inject_station_offline(self, station: str) -> Incident:
        self.offline_stations.add(station)
        inc = self._open_incident(
            IncidentType.STATION_OFFLINE, "MEDIUM", f"{station}:offline", [station]
        )
        self.state_version += 1
        return inc  # type: ignore[return-value]

    def restore_station(self, station: str) -> None:
        self.offline_stations.discard(station)
        self.state_version += 1

    def clear_blocked_nodes(self) -> None:
        self.blocked_nodes.clear()
        self.state_version += 1

    # ------------------------------------------------------------- read model
    def inbound_queue_len(self, station: str) -> int:
        """正在前往（或正在裝載）某站點的 AGV 數，用於站點排隊管制。"""
        n = 0
        for agv in self.agvs.values():
            if agv.task_id is None:
                continue
            task = self.tasks[agv.task_id]
            if task.source != station:
                continue
            if agv.mode in (AgvMode.TO_SOURCE.value, AgvMode.LOADING.value):
                n += 1
        return n

    def pending_tasks(self) -> list[TransportTask]:
        return [
            t
            for t in self.tasks.values()
            if t.status == TaskStatus.PENDING.value
            and t.source not in self.offline_stations
            and t.destination not in self.offline_stations
        ]

    def completed_count(self) -> int:
        return sum(1 for t in self.tasks.values() if t.status == TaskStatus.COMPLETED.value)

    def open_incidents(self) -> list[Incident]:
        return [i for i in self.incidents.values() if i.status in ("OPEN", "RECOVERING")]

    def charger_queue(self, station: str) -> int:
        return sum(
            1
            for a in self.agvs.values()
            if a.charger == station
            and a.mode in (AgvMode.TO_CHARGER.value, AgvMode.CHARGING.value)
        )

    def nearest_charger(self, node: str) -> str:
        """距離 + 佇列長度。只看距離的話全隊會擠向同一個充電樁並互相鎖死。"""
        return min(
            (c for c in CHARGERS if c not in self.offline_stations),
            key=lambda c: (
                self.manhattan(node, STATIONS[c])
                + 12 * max(0, self.charger_queue(c) - len(CHARGER_BAYS[c]) + 1),
                c,
            ),
        )

    def task_cost_estimate(self, agv: Agv, task: TransportTask) -> int:
        """完成任務所需的移動格數（含回充電站的保守估計）。"""
        to_src = self.manhattan(agv.node, STATIONS[task.source])
        to_dst = self.manhattan(STATIONS[task.source], STATIONS[task.destination])
        to_chg = self.manhattan(
            STATIONS[task.destination], STATIONS[self.nearest_charger(STATIONS[task.destination])]
        )
        return to_src + to_dst + to_chg

    def projected_battery(self, agv: Agv, task: TransportTask) -> float:
        return agv.battery - self.task_cost_estimate(agv, task) * MOVE_COST_PCT

    def kpi(self) -> dict[str, Any]:
        return {
            "tick": self.tick_count,
            "sim_seconds": self.tick_count * SECONDS_PER_TICK,
            "tasks_total": len(self.tasks),
            "tasks_completed": self.completed_count(),
            "incidents_total": len(self.incidents),
            "incidents_open": len(self.open_incidents()),
            "incidents_closed": sum(1 for i in self.incidents.values() if i.status == "CLOSED"),
            "safety_violations": len(self.safety_violations),
            "state_version": self.state_version,
        }

    def snapshot_hash(self) -> str:
        """replay 驗證用。任何 pose/battery/task 差異都會改變這個值。"""
        payload = {
            "tick": self.tick_count,
            "agvs": [
                [a.agv_id, a.node, round(a.battery, 2), a.mode, a.task_id, a.moves]
                for a in sorted(self.agvs.values(), key=lambda x: x.agv_id)
            ],
            "tasks": [
                [t.task_id, t.status, t.assigned_agv, t.completed_tick]
                for t in sorted(self.tasks.values(), key=lambda x: x.task_id)
            ],
            "blocked": sorted(self.blocked_nodes),
            "incidents": sorted((i.incident_id, i.status) for i in self.incidents.values()),
        }
        return stable_hash(payload)
