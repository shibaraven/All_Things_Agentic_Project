# ShiftZero Command Center

Hosted judge-facing read model for ShiftZero. It shows the nine-AGV operational twin, KPI, decision activity, incidents, governed action traces, five Google ADK Agent manifests, and Google Cloud evidence.

```bash
npm install
npm run dev
npm test
```

Set `NEXT_PUBLIC_SHIFTZERO_API_URL` at build time to target another API. The hosted build defaults to the deployed ShiftZero Cloud Run URL.

Security properties:

- No `SHIFTZERO_DEMO_TOKEN`, `X-Demo-Token`, or other credential is bundled into client code.
- Live Cloud Run state is read-only from the browser.
- Judge buttons switch to an isolated fixed-seed replay; they do not mutate production state.
- The page falls back to replay when the API or SSE stream is unavailable.
- Cloud Console links are supplied by the backend evidence endpoint, not hard-coded credentials.

Production builds and server-rendered content are verified by `tests/rendered-html.test.mjs`.
