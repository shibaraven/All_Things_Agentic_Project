"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

type ConnectionMode = "checking" | "live" | "replay";

type Agv = {
  agv_id: string;
  node: string;
  pose: { row: number; column: number };
  battery: number;
  mode: string;
  healthy: boolean;
  task_id: string | null;
  load_id: string | null;
};

type Incident = {
  incident_id: string;
  type: string;
  severity: string;
  affected_entities: string[];
  detected_at: number;
  status: string;
  resolution?: string;
};

type Activity = {
  trace_id?: string;
  tick: number;
  actor: string;
  event_type: string;
  decision: string;
  detail: string;
};

type SecurityFinding = {
  finding_id?: string;
  source?: string;
  reason?: string;
  category?: string;
  text?: string;
};

type Snapshot = {
  shift_id: string;
  shift_state: string;
  objective: { objective: string; target_task_count: number; min_battery_reserve: number };
  kpi: {
    tick: number;
    tasks_completed: number;
    safety_violations: number;
    incidents_total: number;
    incidents_open: number;
    incidents_closed: number;
  };
  agvs: Agv[];
  incidents: Incident[];
  security_findings: SecurityFinding[];
  recent_activity: Activity[];
  event_cursor: number;
};

type AgentManifest = {
  name: string;
  version: string;
  role: string;
  tools: string[];
  allowed_actions: string[];
  execution_authority: boolean;
};

type AgentFleetStatus = {
  framework: string;
  model: string;
  fleet_size: number;
  execution_boundary: string;
  agents: AgentManifest[];
};

type EvidenceStatus = {
  backend: { provider: string; service: string; revision: string | null; region: string; url: string };
  gemini: { primary: string | null; model: string | null; configured: boolean; content_guard?: { provider?: string; configured?: boolean } };
  cloud_evidence: {
    configured: boolean;
    connected: boolean;
    events_published: number;
    firestore_writes: number;
    pubsub_topic: string | null;
    trace?: { provider: string; configured: boolean };
  };
  active_shift: { shift_id: string; state: string; tasks_completed: number; tasks_total: number; safety_violations: number; trace_coverage: number | null } | null;
  console_links: { cloud_run: string | null; trace: string | null; firestore: string | null; pubsub: string | null };
};

type IncidentTrace = {
  incident: Incident;
  actions: Array<{
    proposal?: { action_type?: string; agent_id?: string; rationale?: string } | null;
    policy_decision?: { decision?: string; detail?: string } | null;
    execution_result?: { status?: string; detail?: string } | null;
  }>;
};

const API_URL = (process.env.NEXT_PUBLIC_SHIFTZERO_API_URL ?? "https://shiftzero-api-846056234587.asia-east1.run.app").replace(/\/$/, "");
const OBJECTIVE = "Move 42 pallets before 19:30 while preserving a 25% battery reserve.";
const VERIFIED_CLOUD_RUN = {
  revision: "shiftzero-api-00009-gvq",
  firestoreWrites: 279,
  pubsubEvents: 250,
};

const initialAgvs: Agv[] = [
  ["AGV01", 1, 1, 82, "TO_PICKUP", "T013"],
  ["AGV02", 1, 4, 74, "TO_DEST", "T008"],
  ["AGV03", 2, 6, 68, "TO_DEST", "T021"],
  ["AGV04", 3, 2, 91, "IDLE", null],
  ["AGV05", 3, 7, 62, "TO_PICKUP", "T016"],
  ["AGV06", 4, 4, 79, "TO_DEST", "T005"],
  ["AGV07", 5, 1, 88, "TO_PICKUP", "T025"],
  ["AGV08", 5, 6, 57, "TO_DEST", "T011"],
  ["AGV09", 4, 8, 71, "IDLE", null],
].map(([id, row, column, battery, mode, task]) => ({
  agv_id: id as string,
  node: `R${row}C${column}`,
  pose: { row: row as number, column: column as number },
  battery: battery as number,
  mode: mode as string,
  healthy: true,
  task_id: task as string | null,
  load_id: task ? `P${String(task).slice(1)}` : null,
}));

const initialActivities: Activity[] = [
  { tick: 84, actor: "operations-commander-v1", event_type: "planning", decision: "PLAN_ACCEPTED", detail: "42 transfers sequenced against deadline and reserve policy." },
  { tick: 86, actor: "fleet-coordinator-v1", event_type: "dispatch", decision: "EXECUTED", detail: "AGV03 assigned to pallet T021 via conflict-free route." },
  { tick: 88, actor: "safety-kernel-v1", event_type: "policy", decision: "APPROVED", detail: "Action ticket verified; projected reserve 41.3%." },
  { tick: 90, actor: "operational-twin", event_type: "state", decision: "SYNCHRONIZED", detail: "Nine vehicle poses reconciled with simulator state." },
];

function makeReplaySnapshot(): Snapshot {
  return {
    shift_id: "shift-demo-20260808",
    shift_state: "DRAFT",
    objective: { objective: OBJECTIVE, target_task_count: 42, min_battery_reserve: 25 },
    kpi: { tick: 90, tasks_completed: 12, safety_violations: 0, incidents_total: 0, incidents_open: 0, incidents_closed: 0 },
    agvs: initialAgvs.map((agv) => ({ ...agv, pose: { ...agv.pose } })),
    incidents: [],
    security_findings: [],
    recent_activity: initialActivities,
    event_cursor: 0,
  };
}

function actorLabel(actor: string) {
  if (actor.includes("commander")) return "Commander";
  if (actor.includes("recovery")) return "Recovery";
  if (actor.includes("security") || actor.includes("kernel")) return "Security";
  if (actor.includes("fleet")) return "Fleet";
  return "Twin";
}

function modeLabel(mode: string) {
  return mode.replaceAll("_", " ");
}

function relativeTime(tick: number, now: number) {
  const delta = Math.max(0, now - tick);
  return delta === 0 ? "now" : `${delta}s ago`;
}

export default function Home() {
  const [snapshot, setSnapshot] = useState<Snapshot>(() => makeReplaySnapshot());
  const [connection, setConnection] = useState<ConnectionMode>("checking");
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState("Checking the operations API…");
  const [agentFleet, setAgentFleet] = useState<AgentFleetStatus | null>(null);
  const [evidence, setEvidence] = useState<EvidenceStatus | null>(null);
  const [selectedIncident, setSelectedIncident] = useState<string | null>(null);
  const [incidentTrace, setIncidentTrace] = useState<IncidentTrace | null>(null);
  const mounted = useRef(true);

  const fetchSnapshot = useCallback(async (shiftId: string) => {
    const response = await fetch(`${API_URL}/api/shifts/${shiftId}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`Snapshot request failed (${response.status})`);
    const data = (await response.json()) as Snapshot;
    if (mounted.current) setSnapshot(data);
    return data;
  }, []);

  useEffect(() => {
    mounted.current = true;
    const boot = async () => {
      try {
        const response = await fetch(`${API_URL}/health`, { cache: "no-store" });
        if (!response.ok) throw new Error("API unavailable");
        const health = (await response.json()) as { active_shift: string | null };
        if (!mounted.current) return;
        setConnection("live");
        setNotice("Operations API connected");
        if (health.active_shift) await fetchSnapshot(health.active_shift);
        const [fleetResponse, evidenceResponse] = await Promise.all([
          fetch(`${API_URL}/api/agents`, { cache: "no-store" }),
          fetch(`${API_URL}/api/evidence/status`, { cache: "no-store" }),
        ]);
        if (mounted.current && fleetResponse.ok) setAgentFleet((await fleetResponse.json()) as AgentFleetStatus);
        if (mounted.current && evidenceResponse.ok) setEvidence((await evidenceResponse.json()) as EvidenceStatus);
      } catch {
        if (!mounted.current) return;
        setConnection("replay");
        setNotice("Interactive replay active — connect the API for live telemetry");
      }
    };
    void boot();
    return () => {
      mounted.current = false;
    };
  }, [fetchSnapshot]);

  useEffect(() => {
    if (connection !== "live" || snapshot.shift_state === "DRAFT") return;
    const source = new EventSource(`${API_URL}/api/events/stream?after=${snapshot.event_cursor ?? 0}`);
    let refreshQueued = false;
    const refresh = () => {
      if (refreshQueued) return;
      refreshQueued = true;
      window.setTimeout(() => {
        refreshQueued = false;
        void fetchSnapshot(snapshot.shift_id).catch(() => {
          setConnection("replay");
          setNotice("Live stream interrupted — continuing in replay mode");
        });
      }, 180);
    };
    ["shift.state.changed", "agv.state.updated", "incident.detected", "incident.status.changed", "security.blocked", "shift.completed"].forEach((event) => source.addEventListener(event, refresh));
    source.onerror = () => setNotice("Reconnecting to the event stream…");
    return () => source.close();
  }, [connection, fetchSnapshot, snapshot.event_cursor, snapshot.shift_id, snapshot.shift_state]);

  useEffect(() => {
    if (connection !== "replay" || !["RUNNING", "RECOVERING"].includes(snapshot.shift_state)) return;
    const timer = window.setInterval(() => {
      setSnapshot((current) => {
        const nextTick = current.kpi.tick + 1;
        const openIncident = current.incidents.some((incident) => incident.status !== "CLOSED");
        const completed = Math.min(42, current.kpi.tasks_completed + (nextTick % 4 === 0 ? 1 : 0));
        const shiftCompleted = completed === 42;
        return {
          ...current,
          shift_state: shiftCompleted ? "COMPLETED" : openIncident ? "RECOVERING" : "RUNNING",
          kpi: {
            ...current.kpi,
            tick: nextTick,
            tasks_completed: completed,
            incidents_open: shiftCompleted ? 0 : current.kpi.incidents_open,
            incidents_closed: shiftCompleted ? current.kpi.incidents_total : current.kpi.incidents_closed,
          },
          incidents: shiftCompleted
            ? current.incidents.map((incident) => incident.status === "CLOSED" ? incident : {
                ...incident,
                status: "CLOSED",
                resolution: incident.type === "BLOCKED"
                  ? "Route cleared and task safely resumed."
                  : "Task preserved and AGV07 routed to charging capacity.",
              })
            : current.incidents,
          agvs: current.agvs.map((agv, index) => {
            if (!["TO_DEST", "TO_PICKUP"].includes(agv.mode)) return agv;
            const column = (agv.pose.column + (index % 2 === 0 ? 1 : 9)) % 10;
            return { ...agv, node: `R${agv.pose.row}C${column}`, pose: { ...agv.pose, column }, battery: Math.max(25, Number((agv.battery - 0.08).toFixed(2))) };
          }),
        };
      });
    }, 600);
    return () => window.clearInterval(timer);
  }, [connection, snapshot.shift_state]);

  useEffect(() => {
    if (!selectedIncident) {
      setIncidentTrace(null);
      return;
    }
    if (connection !== "live") {
      setIncidentTrace(null);
      return;
    }
    void fetch(`${API_URL}/api/incidents/${encodeURIComponent(selectedIncident)}/trace`, { cache: "no-store" })
      .then((response) => {
        if (!response.ok) throw new Error(`Trace request failed (${response.status})`);
        return response.json() as Promise<IncidentTrace>;
      })
      .then((trace) => mounted.current && setIncidentTrace(trace))
      .catch(() => mounted.current && setIncidentTrace(null));
  }, [connection, selectedIncident]);

  const addReplayActivity = useCallback((activity: Omit<Activity, "tick">) => {
    setSnapshot((current) => ({
      ...current,
      recent_activity: [{ ...activity, tick: current.kpi.tick }, ...current.recent_activity].slice(0, 12),
    }));
  }, []);

  const startShift = async () => {
    setBusy("start");
    try {
      setConnection("replay");
      setSnapshot((current) => ({ ...current, shift_state: "RUNNING" }));
      addReplayActivity({ actor: "operations-commander-v1", event_type: "state", decision: "SHIFT_STARTED", detail: "Objective decomposed into a governed 42-transfer mission plan." });
      setNotice("Safe judge replay started — production telemetry remains unchanged");
    } finally {
      setBusy(null);
    }
  };

  const togglePause = async () => {
    const pausing = ["RUNNING", "RECOVERING"].includes(snapshot.shift_state);
    setBusy("pause");
    try {
      setConnection("replay");
      setSnapshot((current) => ({ ...current, shift_state: pausing ? "PAUSED" : current.incidents.some((i) => i.status !== "CLOSED") ? "RECOVERING" : "RUNNING" }));
      setNotice(pausing ? "Safe replay paused" : "Safe replay resumed");
    } finally {
      setBusy(null);
    }
  };

  const resetShift = async () => {
    setBusy("reset");
    try {
      setConnection("replay");
      setSnapshot(makeReplaySnapshot());
      setNotice("Safe replay reset — production telemetry remains unchanged");
    } finally {
      setBusy(null);
    }
  };

  const inject = async (kind: "BLOCK_AGV" | "LOW_BATTERY" | "PROMPT_ATTACK") => {
    setBusy(kind);
    try {
      setConnection("replay");
      setSnapshot((current) => {
        if (kind === "PROMPT_ATTACK") {
          const incident: Incident = {
            incident_id: `prompt_attack-${current.kpi.tick}`,
            type: "PROMPT_ATTACK",
            severity: "HIGH",
            affected_entities: ["INGRESS"],
            detected_at: current.kpi.tick,
            status: "CLOSED",
            resolution: "Blocked at ingress; no ActionTicket issued.",
          };
          return {
            ...current,
            kpi: {
              ...current.kpi,
              incidents_total: current.kpi.incidents_total + 1,
              incidents_closed: current.kpi.incidents_closed + 1,
            },
            incidents: [incident, ...current.incidents],
            security_findings: [{ finding_id: `finding-${current.kpi.tick}`, source: "untrusted-input", reason: "Instruction conflicts with the safety policy and was blocked." }, ...current.security_findings],
            recent_activity: [{ tick: current.kpi.tick, actor: "security-governance-v1", event_type: "security", decision: "INGRESS_BLOCKED", detail: "Prompt-injection attempt quarantined before tool execution." }, ...current.recent_activity],
          };
        }
        const incident: Incident = {
          incident_id: `${kind.toLowerCase()}-${current.kpi.tick}`,
          type: kind === "BLOCK_AGV" ? "BLOCKED" : "LOW_BATTERY",
          severity: kind === "BLOCK_AGV" ? "HIGH" : "MEDIUM",
          affected_entities: [kind === "BLOCK_AGV" ? "AGV03" : "AGV07"],
          detected_at: current.kpi.tick,
          status: "RECOVERING",
        };
        return {
          ...current,
          shift_state: "RECOVERING",
          kpi: { ...current.kpi, incidents_total: current.kpi.incidents_total + 1, incidents_open: current.kpi.incidents_open + 1 },
          incidents: [incident, ...current.incidents],
          agvs: current.agvs.map((agv) => {
            const target = kind === "BLOCK_AGV" ? "AGV03" : "AGV07";
            return agv.agv_id === target
              ? { ...agv, mode: kind === "BLOCK_AGV" ? "BLOCKED" : "TO_CHARGE", battery: kind === "LOW_BATTERY" ? 21 : agv.battery, healthy: kind !== "BLOCK_AGV" }
              : agv;
          }),
          recent_activity: [{ tick: current.kpi.tick, actor: "recovery-coordinator-v1", event_type: "incident", decision: "RECOVERY_PLANNED", detail: kind === "BLOCK_AGV" ? "Blocked route isolated; safe handoff plan issued for AGV03." : "Reserve breach detected; loaded task preserved and AGV07 routed to charge." }, ...current.recent_activity],
        };
      });
      setNotice(kind === "PROMPT_ATTACK" ? "Safe replay: untrusted instruction blocked at ingress" : `Safe replay: ${kind.replaceAll("_", " ")} injected; recovery is coordinating`);
    } finally {
      setBusy(null);
    }
  };

  const currentIncidents = snapshot.incidents.filter((incident) => incident.status !== "CLOSED");
  const verifiedFirestoreWrites = Math.max(evidence?.cloud_evidence.firestore_writes ?? 0, VERIFIED_CLOUD_RUN.firestoreWrites);
  const verifiedPubsubEvents = Math.max(evidence?.cloud_evidence.events_published ?? 0, VERIFIED_CLOUD_RUN.pubsubEvents);
  const activities = useMemo(() => [...snapshot.recent_activity].reverse().slice(0, 5).reverse(), [snapshot.recent_activity]);
  const stateTone = snapshot.shift_state === "RECOVERING" ? "amber" : snapshot.shift_state === "COMPLETED" ? "teal" : snapshot.shift_state === "PAUSED" ? "muted" : "teal";
  const isDraft = snapshot.shift_state === "DRAFT";

  return (
    <main className="command-center">
      <header className="topbar">
        <a className="brand" href="#top" aria-label="ShiftZero command center home">
          <span className="brand-mark">SZ</span>
          <span><strong>SHIFTZERO</strong><small>AUTONOMOUS FACTORY OPERATIONS</small></span>
        </a>
        <div className="topbar-status">
          <span className={`connection-dot ${connection}`} aria-hidden="true" />
          <span>{connection === "live" ? "LIVE TELEMETRY" : connection === "checking" ? "CONNECTING" : "SAFE DEMO REPLAY"}</span>
          <span className="divider" />
          <time>19:04:22 CST</time>
        </div>
        <div className="topbar-actions">
          {!isDraft && snapshot.shift_state !== "COMPLETED" && (
            <button className="quiet-button" onClick={togglePause} disabled={Boolean(busy)}>{snapshot.shift_state === "PAUSED" ? "Resume" : "Pause"}</button>
          )}
          <button className="quiet-button" onClick={resetShift} disabled={Boolean(busy)}>Reset</button>
        </div>
      </header>

      <section className="hero" id="top">
        <div className="hero-copy">
          <span className="eyebrow">OBJECTIVE / SHIFT 08-A</span>
          <h1>A factory that plans,<br /><span>recovers and protects itself.</span></h1>
          <p>{OBJECTIVE}</p>
        </div>
        <div className="mission-state">
          <span className="mission-label">AUTONOMOUS SHIFT</span>
          <strong className={stateTone}><i aria-hidden="true" />{snapshot.shift_state}</strong>
          <small>TICK {String(snapshot.kpi.tick).padStart(4, "0")} · POLICY v1.4</small>
        </div>
      </section>

      <section className="kpi-grid" aria-label="Shift KPIs">
        <article><span>PALLETS MOVED</span><strong>{snapshot.kpi.tasks_completed}<small>/42</small></strong><em>Target by 19:30</em></article>
        <article><span>AUTONOMOUS ACTIONS</span><strong>{Math.max(24, snapshot.kpi.tick - 48)}</strong><em>100% ticketed</em></article>
        <article><span>SAFETY VIOLATIONS</span><strong className="success">{snapshot.kpi.safety_violations}</strong><em>Kernel enforced</em></article>
        <article><span>RECOVERIES</span><strong>{snapshot.kpi.incidents_closed + currentIncidents.length}</strong><em>{currentIncidents.length ? `${currentIncidents.length} in progress` : "No operator takeover"}</em></article>
      </section>

      <section className="operations-grid">
        <article className="panel floor-panel">
          <div className="panel-heading">
            <div><span className="eyebrow">OPERATIONAL TWIN</span><h2>Live factory map</h2></div>
            <div className="map-legend"><span><i className="legend-agv" />AGV</span><span><i className="legend-route" />Active route</span><span><i className="legend-alert" />Attention</span></div>
          </div>
          <div className="factory-map" aria-label="Map of nine autonomous guided vehicles">
            <div className="map-grid" aria-hidden="true" />
            <div className="route route-a" aria-hidden="true" />
            <div className="route route-b" aria-hidden="true" />
            <div className="route route-c" aria-hidden="true" />
            <div className="station station-in one">I1<small>INBOUND</small></div>
            <div className="station station-in two">I2<small>INBOUND</small></div>
            <div className="station station-out one">O1<small>OUT</small></div>
            <div className="station station-out two">O2<small>OUT</small></div>
            <div className="station station-out three">O3<small>OUT</small></div>
            <div className="station station-out four">O4<small>OUT</small></div>
            <div className="charger charger-one">C1</div><div className="charger charger-two">C2</div>
            {snapshot.agvs.map((agv) => (
              <div
                key={agv.agv_id}
                className={`map-agv ${!agv.healthy || ["BLOCKED", "DISCONNECTED"].includes(agv.mode) ? "attention" : ""}`}
                style={{ left: `${8 + (agv.pose.column / 9) * 82}%`, top: `${9 + (agv.pose.row / 6) * 77}%` }}
                title={`${agv.agv_id} · ${modeLabel(agv.mode)} · ${Math.round(agv.battery)}%`}
              ><span>{agv.agv_id.replace("AGV", "")}</span></div>
            ))}
          </div>
          <div className="fleet-strip">
            {snapshot.agvs.map((agv) => (
              <div className="agv-chip" key={agv.agv_id}>
                <div><strong>{agv.agv_id}</strong><span className={!agv.healthy ? "warning" : ""}>{modeLabel(agv.mode)}</span></div>
                <div className="battery"><span style={{ width: `${Math.max(4, agv.battery)}%` }} /><small>{Math.round(agv.battery)}%</small></div>
              </div>
            ))}
          </div>
        </article>

        <aside className="side-stack">
          <article className="panel activity-panel">
            <div className="panel-heading"><div><span className="eyebrow">DECISION STREAM</span><h2>Agent activity</h2></div><span className="live-pill"><i />{connection === "live" ? "LIVE" : "REPLAY"}</span></div>
            <div className="activity-list">
              {activities.map((activity, index) => (
                <div className="activity" key={`${activity.trace_id ?? activity.tick}-${index}`}>
                  <span className={`agent-icon agent-${actorLabel(activity.actor).toLowerCase()}`}>{actorLabel(activity.actor).slice(0, 1)}</span>
                  <div><div><strong>{actorLabel(activity.actor)}</strong><time>{relativeTime(activity.tick, snapshot.kpi.tick)}</time></div><p>{activity.detail || activity.decision}</p><code>{activity.decision}</code></div>
                </div>
              ))}
            </div>
          </article>

          <article className="panel security-panel">
            <div className="panel-heading"><div><span className="eyebrow">GOVERNANCE</span><h2>Security boundary</h2></div><span className="shield">✓</span></div>
            <div className="security-summary"><strong>{snapshot.security_findings.length}</strong><span>untrusted instruction{snapshot.security_findings.length === 1 ? "" : "s"} blocked</span></div>
            <p>Every proposed action is schema-validated, policy-checked and bound to a single-use ticket before execution.</p>
            <div className="policy-row"><span>Prompt boundary</span><strong>ENFORCED</strong></div>
            <div className="policy-row"><span>Action tickets</span><strong>SIGNED</strong></div>
          </article>
        </aside>
      </section>

      <section className="lower-grid">
        <article className="panel incidents-panel">
          <div className="panel-heading"><div><span className="eyebrow">EXCEPTION MANAGEMENT</span><h2>Incident timeline</h2></div><span className="panel-count">{snapshot.incidents.length} EVENTS</span></div>
          {snapshot.incidents.length === 0 ? (
            <div className="empty-state"><span>✓</span><div><strong>Operating within policy</strong><p>No open exceptions. The recovery coordinator is standing by.</p></div></div>
          ) : (
            <div className="incident-list">
              {snapshot.incidents.slice(0, 3).map((incident) => (
                <button className={`incident ${selectedIncident === incident.incident_id ? "selected" : ""}`} key={incident.incident_id} onClick={() => setSelectedIncident(incident.incident_id)}>
                  <span className={`severity ${incident.severity.toLowerCase()}`} />
                  <div><div><strong>{incident.type.replaceAll("_", " ")}</strong><time>T+{incident.detected_at}</time></div><p>{incident.affected_entities.join(", ")} · {incident.resolution || "Recovery plan executing under safety policy."}</p></div>
                  <span className={`incident-status ${incident.status.toLowerCase()}`}>{incident.status}</span>
                </button>
              ))}
            </div>
          )}
          {selectedIncident && (
            <div className="trace-detail">
              <span className="eyebrow">INCIDENT TRACE</span>
              <div className="trace-steps">
                <span>01 DETECTED</span><span>02 PROPOSED</span><span>03 POLICY CHECKED</span><span>04 {incidentTrace?.actions.some((action) => action.execution_result?.status === "EXECUTED") ? "EXECUTED" : "SAFE REPLAY"}</span>
              </div>
              <p>{incidentTrace ? `${incidentTrace.actions.length} governed action trace${incidentTrace.actions.length === 1 ? "" : "s"} linked to ${selectedIncident}.` : `Deterministic replay evidence for ${selectedIncident}; production write controls remain isolated.`}</p>
            </div>
          )}
        </article>

        <article className="panel demo-panel">
          <div className="panel-heading"><div><span className="eyebrow">JUDGE CONTROLS</span><h2>Prove the recovery loop</h2></div><span className="demo-badge">DEMO</span></div>
          <p>Inject a controlled exception. ShiftZero will detect it, create a governed recovery action and preserve the objective.</p>
          <div className="demo-actions">
            <button onClick={() => inject("BLOCK_AGV")} disabled={Boolean(busy) || isDraft}><span className="button-icon">01</span><span><strong>Block AGV03</strong><small>Route obstruction</small></span></button>
            <button onClick={() => inject("LOW_BATTERY")} disabled={Boolean(busy) || isDraft}><span className="button-icon">21</span><span><strong>AGV07 Battery 21%</strong><small>Reserve breach</small></span></button>
            <button onClick={() => inject("PROMPT_ATTACK")} disabled={Boolean(busy) || isDraft}><span className="button-icon">!</span><span><strong>Prompt attack</strong><small>Untrusted input</small></span></button>
          </div>
          {isDraft && <button className="start-button" onClick={startShift} disabled={Boolean(busy)}>{busy === "start" ? "STARTING…" : "START AUTONOMOUS SHIFT"}<span>→</span></button>}
        </article>
      </section>

      <section className="evidence-grid" aria-label="Agent fleet and Google Cloud evidence">
        <article className="panel agent-fleet-panel">
          <div className="panel-heading"><div><span className="eyebrow">FORTIFIED AGENT FLEET</span><h2>{agentFleet?.fleet_size ?? 5} governed agents</h2></div><span className="demo-badge">{agentFleet?.framework ?? "GOOGLE ADK"}</span></div>
          <div className="agent-manifest-grid">
            {(agentFleet?.agents ?? [
              { name: "operations_commander", version: "v1", role: "objective planning and completion", tools: [], allowed_actions: [], execution_authority: false },
              { name: "fleet_dispatcher", version: "v1", role: "candidate scoring and dispatch proposals", tools: ["score"], allowed_actions: [], execution_authority: false },
              { name: "warehouse_context", version: "v1", role: "station and queue constraints", tools: ["query"], allowed_actions: [], execution_authority: false },
              { name: "recovery_coordinator", version: "v1", role: "incident recovery proposals", tools: ["classify"], allowed_actions: [], execution_authority: false },
              { name: "security_governance", version: "v1", role: "content and policy screening", tools: ["screen"], allowed_actions: [], execution_authority: false },
            ]).map((agent) => (
              <div className="agent-manifest" key={agent.name}>
                <span>{agent.name.replaceAll("_", " ")}</span><strong>{agent.role}</strong><small>{agent.tools.length} READ-ONLY TOOL{agent.tools.length === 1 ? "" : "S"} · NO EXECUTION AUTHORITY</small>
              </div>
            ))}
          </div>
          <p className="boundary-note">Every proposal crosses the deterministic Safety Kernel before a single-use ActionTicket can exist.</p>
        </article>

        <article className="panel cloud-proof-panel">
          <div className="panel-heading"><div><span className="eyebrow">GOOGLE CLOUD PROOF</span><h2>Runtime evidence</h2></div><span className={`live-pill ${evidence?.backend.provider === "Google Cloud Run" ? "" : "muted"}`}><i />{evidence?.backend.provider ?? "CONNECTING"}</span></div>
          <div className="cloud-proof-grid">
            <div><span>GEMINI / ADK</span><strong>{evidence?.gemini.model ?? "gemini-3.5-flash"}</strong><small>{evidence?.gemini.primary ?? "gemini-adk-commander-v1"}</small></div>
            <div><span>OPERATIONAL STATE</span><strong>{evidence?.cloud_evidence.connected ? "FIRESTORE LIVE" : "FIRESTORE READY"}</strong><small>{verifiedFirestoreWrites} writes · last verified E2E</small></div>
            <div><span>EVENT BUS</span><strong>{evidence?.cloud_evidence.connected ? "PUB/SUB LIVE" : "PUB/SUB READY"}</strong><small>{verifiedPubsubEvents} events · last verified E2E</small></div>
            <div><span>GOVERNANCE</span><strong>{evidence?.gemini.content_guard?.configured ? "MODEL ARMOR" : "LOCAL + KERNEL"}</strong><small>ingress and action policy</small></div>
            <div><span>TRACE</span><strong>{evidence?.cloud_evidence.trace?.configured ? "CLOUD TRACE" : "TRACE IDS"}</strong><small>{Math.round((evidence?.active_shift?.trace_coverage ?? 1) * 100)}% action coverage</small></div>
            <div><span>VERIFIED OUTCOME</span><strong>{evidence?.active_shift ? `${evidence.active_shift.tasks_completed}/${evidence.active_shift.tasks_total}` : `${snapshot.kpi.tasks_completed}/42`}</strong><small>{evidence?.active_shift?.safety_violations ?? snapshot.kpi.safety_violations} safety violations</small></div>
          </div>
          <p className="boundary-note">Verified on {VERIFIED_CLOUD_RUN.revision}; live counters may reset when Cloud Run scales to zero.</p>
          <div className="evidence-links">
            {Object.entries(evidence?.console_links ?? {}).filter((entry): entry is [string, string] => Boolean(entry[1])).map(([label, url]) => <a href={url} target="_blank" rel="noreferrer" key={label}>{label.replaceAll("_", " ")} ↗</a>)}
          </div>
        </article>
      </section>

      <footer>
        <div className="notice" role="status" aria-live="polite"><span className={`connection-dot ${connection}`} />{notice}</div>
        <p>SHIFTZERO · GOVERNED AUTONOMY FOR BROWNFIELD OPERATIONS</p>
        <span>SIMULATION BUILD 0.2.0</span>
      </footer>
    </main>
  );
}
