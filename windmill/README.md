# Stromboli on Windmill (orchestrator comparison B)

The same triage flow as the Prefect version, composed from the shared
`stromboli.orchestration.phases.TriagePhases` so it runs the *real* Stromboli
logic (LangGraph nodes + Langfuse) — Windmill only orchestrates + visualizes.

## Files
- `f/stromboli/triage_step.py` — one parameterized step (`main(phase, state)`),
  called once per phase so each shows as a node in the Windmill flow graph.
- `f/stromboli/route.py` — stateless branch decisions (ambiguous / verdict).
- `f/stromboli/poll.py` — the Notion front-end: returns Ready task ids (attach a
  Windmill **schedule** to fan out a triage flow per id).
- `f/stromboli/stromboli_triage.flow.yaml` — the OpenFlow (import + tune in UI).

## Deploy
1. Install the [Windmill CLI](https://www.windmill.dev/docs/advanced/cli):
   `npm i -g windmill-cli` and `wmill workspace add …`.
2. Push the scripts + flow:
   ```bash
   cd windmill && wmill sync push
   ```
3. **Worker prerequisites (the key Windmill caveat):** the scripts `import
   stromboli`, which is **not on PyPI**, so a stock Windmill Python worker can't
   resolve it. You must run Stromboli on the worker. Options:
   - a **custom worker image** with `pip install /path/to/stromboli` (+ git,
     docker, and the bundled `claude` CLI for the coding node), or
   - Windmill's **uv**/relative-import support pointed at the repo.
   The worker also needs the same env as `.env` (NOTION_TOKEN, LITELLM_*,
   GITHUB_TOKEN, WORKSPACE_ROOT, optional CODER_AUTH_MODE=subscription with the
   `claude` login mounted). Set these as Windmill **variables/secrets**.
4. In the Windmill UI: open the flow, and wrap **coding → verify** in a
   **while-loop** (condition: `results.route_verdict == "revise"`, max
   iterations = `MAX_OUTER_REVISIONS`) to get the outer revise recursion. The
   YAML ships the linear path + the two branches; the loop is a one-knob UI add
   (Windmill's strength is visual flow editing).

## Run / observe
- Manually: open `stromboli_triage`, run with `{ "task_id": "<notion-page-id>" }`.
- Autonomously: schedule `poll` (e.g. every 30s) and have it trigger the flow per
  returned id. Windmill's UI shows each run's node graph, per-step logs, timings,
  retries, and a Cancel button — the watchtower equivalent.
