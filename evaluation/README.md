# Evaluation

`cases.jsonl` is a compact, versioned evaluation set for agent planning, authorization, recovery, and ingress screening. Each line provides a case ID, component, input, and deterministic expectation.

Safety and objective completion are not graded by another model. Executable equivalents live in `selftest.py`, `agent_selftest.py`, `cloud_selftest.py`, and `api_selftest.py`.

Run the full deterministic suite:

```bash
python selftest.py --json
python agent_selftest.py
python cloud_selftest.py
python api_selftest.py
python rehearsal_selftest.py
```
