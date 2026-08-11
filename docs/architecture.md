# ShiftZero Architecture

## Control and trust boundaries

ShiftZero deliberately separates reasoning, authorization, execution, and evidence.

1. The Command Center reads a projection from FastAPI and subscribes to SSE. Its public judge controls use local replay; the browser never receives the production mutation token.
2. Model Armor and a deterministic local screen classify untrusted objective/tool content before agent reasoning. Cloud failure falls back to local policy.
3. The Google ADK root Agent owns mission planning. Four specialist ADK sub-agents have narrowly scoped, read-only tools. Their manifests explicitly set `execution_authority=false`.
4. Fleet and Recovery logic produces `ActionProposal` objects. The Safety Kernel re-reads the live twin and validates caller identity, exact parameters, state version, route, reservation, battery reserve, emergency stop, and idempotency.
5. An approved proposal becomes a short-lived, single-use `ActionTicket`. Only this ticket can reach the deterministic twin executor.
6. Events remain available in the API read model. A queue-backed bridge asynchronously persists evidence to Firestore, publishes it to Pub/Sub, and exports OpenTelemetry planning spans to Cloud Trace.

## State versus memory

The operational source of truth is the in-process deterministic twin during one demo shift. Firestore stores durable evidence snapshots and event replay records, not control authority. No LLM memory is allowed to determine live pose, reservation, emergency stop, load state, or battery safety.

The hackathon build does not claim that Vertex AI Memory Bank or Agent Gateway is deployed. Those remain production-hardening items after the competition.

## Failure behavior

| Failure | Behavior |
|---|---|
| Gemini/ADK timeout or invalid plan | Deterministic commander fallback emits `plan.fallback` |
| Model Armor unavailable | Local policy screen remains active and records fallback reason |
| Firestore/Pub/Sub unavailable | Evidence queue reports errors; the control loop continues |
| Stale or malformed proposal | Safety Kernel rejects without issuing a ticket |
| Duplicate command | Idempotency returns the prior response / execution is consumed once |
| Browser has no mutation token | Live writes receive HTTP 403; replay remains demonstrable |

## Deployed Google Cloud resources

- Cloud Run service: `shiftzero-api` in `asia-east1`
- Runtime identity: `shiftzero-runtime@PROJECT_ID.iam.gserviceaccount.com`
- Vertex AI Gemini region: `global`
- Model Armor template: `shiftzero-ingress` in `asia-southeast1`
- Firestore database: `(default)`
- Pub/Sub topic: `shiftzero-events`
- Secret Manager secret: `shiftzero-demo-token`
- Cloud Trace exporter: OpenTelemetry batch span processor

Resource names are configuration, not credentials.
