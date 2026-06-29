# Orchestrator comparison: Prefect vs Windmill (for Stromboli triage)

Both wrap the **same** triage flow via `stromboli.orchestration.phases.TriagePhases`
(the LangGraph nodes + Langfuse stay the engine). You're comparing the *tools*.

| | **Prefect 3** (`orchestration/prefect_flow.py`) | **Windmill** (`windmill/`) |
|---|---|---|
| Flow defined in | Pure Python (`@flow`/`@task`) | Python scripts + OpenFlow YAML (or the UI builder) |
| Control flow (branches, revise loop) | Plain Python (`if`, `for`) — trivial | `branchone` modules + a UI **while-loop**; visual |
| Dashboard | Prefect UI (`prefect server start`, :4200): run graph, states, logs, retries, cancel | Windmill UI: flow graph, per-step logs/timings, retries, cancel, plus a flow **editor** |
| Scheduling | `poll_flow.serve(interval=…)` deployment | A **schedule** on `poll` |
| Where it runs | **Your** Python env (imports `stromboli` directly) — easy | Windmill **workers** must have `stromboli` installed (custom image) — more setup |
| Secrets | your env / `op run` | Windmill variables/secrets |
| Long-running coding step | fine (task can run minutes; `retries=` on `coding`) | fine; step timeout/retries configurable |
| Durability / resume after crash | limited (re-run the flow) | limited (re-run) — for true durable resume use Temporal |
| Best when | you want **code-first, lives next to the app**, minimal infra | you want a **self-hosted UI to build/tune flows** + non-devs editing them |

## Run each

**Prefect**
```bash
uv sync --extra orchestration
prefect server start                      # dashboard → http://127.0.0.1:4200
# one task:
op run --env-file=.env -- uv run python -c \
  "from stromboli.orchestration.prefect_flow import triage_flow; triage_flow('<page-id>')"
# autonomous poll on a schedule:
op run --env-file=.env -- uv run python -m stromboli.orchestration.prefect_flow
```

**Windmill** — see [`windmill/README.md`](../windmill/README.md): `wmill sync push`,
ensure workers have `stromboli` + env, run/schedule the flow; UI at your Windmill host.

## Recommendation
- **Prefect** is the lighter, code-first fit that lives in this repo and needs no
  worker image — best if the team is Python-first and wants "Airflow but small."
- **Windmill** wins if you want a **self-hosted visual flow editor + UI** that
  non-engineers can read/tune, and you're willing to run workers with Stromboli.
- For **durable, resumable** long agentic runs (rate-limit pause→resume, crash
  recovery) neither is ideal — that's **Temporal** (a third spike if wanted).
- In all cases keep **LangGraph** (per-task engine) + **Langfuse** (node traces,
  tokens, cost, evals). The orchestrator is the scheduler/supervisor/visualizer.
