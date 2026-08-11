# Evidence Index

This directory is the submission evidence pack. It intentionally excludes credentials and raw secret output.

| Artifact | Meaning |
|---|---|
| `rehearsal-5x-local.json` | Five deterministic three-event demo runs with identical 42/42 outcomes |
| `cloud-e2e-latest.json` | Reviewed authenticated Cloud Run acceptance: 15/15 checks, 42/42 tasks, zero safety violations |
| `cloud-resources.json` | Sanitized resource inventory for the accepted revision and connected Google Cloud services |
| Command Center cloud-proof panel | Runtime revision, Firestore/Pub/Sub counters, content guard and trace status |
| Cloud Console links from `/api/evidence/status` | Direct proof for Cloud Run, Firestore, Pub/Sub and Trace |

Generate local evidence:

```bash
python rehearsal_selftest.py --output docs/evidence/rehearsal-5x-local.json
```

Generate cloud evidence in an authenticated environment:

```bash
python cloud_e2e.py > docs/evidence/cloud-e2e-latest.json
```

The checked-in JSON has been reviewed. It contains resource and revision identifiers, but no credential or secret value.
