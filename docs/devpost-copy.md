# Devpost Submission Copy

## Project name

ShiftZero - Autonomous Factory Operations Agent Fleet

## Tagline

An autonomous factory operations agent fleet that plans pallet movements, recovers from failures, and blocks unsafe instructions without a human dispatcher.

## Inspiration / problem

Factories already have WMS, fleet managers, PLCs, and dashboards, yet routine exceptions still pull humans back into dispatch. A blocked aisle, a low-battery vehicle, or a malicious maintenance note can fragment decisions across operations and security. We wanted one control plane that can pursue an outcome while preserving deterministic safety authority.

## What it does

A supervisor enters one objective: move 42 pallets before the deadline while maintaining a 25% battery reserve. ShiftZero's Gemini-powered Google ADK Agent Fleet creates the mission plan, scores dispatch candidates, reads warehouse constraints, proposes recovery, and screens untrusted content. A deterministic Safety Kernel validates identity, schema, state freshness, route, battery, emergency stop, and idempotency before issuing a single-use ActionTicket.

The demo runs nine AGV digital twins. We inject a blocked route, force one AGV below reserve, and submit a prompt-injection attack. ShiftZero recovers without manual dispatch and completes 42/42 with zero safety violations and full action trace coverage.

## How we built it

- Gemini 3.5 Flash on Vertex AI and Google ADK for a root Operations Commander plus four bounded specialist agents
- FastAPI on Cloud Run for lifecycle, authenticated mutations, idempotency, SSE, and trace read models
- A zero-dependency deterministic Python twin and Safety Kernel
- Model Armor plus deterministic application policy for prompt/tool ingress
- Firestore for durable shift/event evidence and Pub/Sub for decoupled domain events
- OpenTelemetry exporting planning spans to Cloud Trace
- Secret Manager and a least-privilege Cloud Run runtime identity
- A hosted React/vinext Command Center with map, KPI, incident trace, Agent manifests, and cloud-proof links

## Architecture and data sources

The only operational data source in the hackathon build is a fixed-seed digital twin generated for this project. It contains no customer or proprietary industrial data. Agent tools receive typed facts; they cannot access the live World object or actuator interface. Cloud evidence is asynchronous and never grants execution authority.

## Challenges

The hardest failures were interactions, not individual algorithms. An idle AGV occupying an outbound port could deadlock every delivery. Nearest-charger routing caused queue collapse. A blocked and low-battery vehicle required recovery in the correct order. Within one tick, recovery and normal dispatch could target one AGV. Moving safety to a Kernel that re-reads live state solved conflicts independent agents could not see.

We also separated public demo interaction from production writes. The hosted UI carries no secret: live state is readable, while judge buttons operate an isolated deterministic replay. Cloud mutation testing uses a Secret Manager-backed token.

## Accomplishments

- 16/16 deterministic operational and safety scenarios
- 8/8 Agent Fleet and plan-boundary checks
- 11/11 API lifecycle, trace, SSE, and idempotency checks
- Five identical full three-event rehearsals: tick 188, 42/42, zero safety violations, zero manual intervention, 100% trace coverage
- Deployed Cloud Run backend and hosted Command Center
- Firestore, Pub/Sub, Model Armor, Cloud Trace, Secret Manager, and IAM integrations with visible evidence

## What we learned

Agentic does not mean letting a model perform every step. The strongest architecture uses an LLM for ambiguity, explicit tools for facts, deterministic policy for authority, and event evidence for trust. Recovery cannot be marked complete because an agent said so; renewed progress in the twin must prove it.

## What's next

First, add Agent Engine session persistence and Memory Bank for approved cross-shift lessons. Next, put the system in read-only shadow mode against a VDA5050/MQTT lab fleet. Then introduce Agent Gateway and supervised low-risk actions before any bounded production autonomy.

## Honest deployment disclosure

The hackathon backend is deployed on Cloud Run, not Agent Engine. Model Armor is called directly through its regional API; Agent Gateway and Memory Bank are roadmap items and are not claimed as deployed. The simulator is not a certified industrial controller.

## Links to insert before submission

- Hosted app: <https://shiftzero-command-center.mingjen.chatgpt.site>
- Cloud API: <https://shiftzero-api-846056234587.asia-east1.run.app>
- Source repository: `ADD_PUBLIC_GITHUB_URL`
- Demo video: `ADD_YOUTUBE_OR_VIMEO_URL`
- Architecture image: `docs/architecture.png`
