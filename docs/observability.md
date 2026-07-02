# How to observe a task

Langfuse is the **source of truth** for "where is this task and why did it do
that". Every run also writes a local, dependency-free trace file as a fallback.

## The Langfuse trace (primary)

One trace per task, named after the request and correlated by `task_id`
(carried in the root span's metadata). Under it:

- **One span per node** — `intake`, `spec`, `prompt`, `coding`, `verifier`,
  `pr`, `human`, `memory` — with start/end times (latency), the node's **input**
  (status, outer iteration, tokens spent so far) and its **output**: the partial
  state the node returned, which in LangGraph *is* the state diff. Reading the
  spans top-to-bottom is reading the task's routing history — a second
  `coding` + `verifier` pair means the verifier issued a `revise`.
- **`llm-call` child spans** under `spec` / `prompt` / `verifier` — the gateway
  call's model and token usage.
- **`sdk-turn-N` child spans** under `coding` — one per Agent SDK agent-loop
  turn, with the tools it invoked and its token usage. The `coding` span's
  metadata carries `turns`, `subtype` and `cost_usd`; its output carries
  `tests_passed`, `code_diff_chars` and cumulative `tokens_used`.
- **Terminal tag** on the trace: `done`, `escalated`, or `failure` (with the
  error message on a crash).

### Where to look

Langfuse runs locally (the `observability-langfuse-web-1` container):

1. Open **http://localhost:3100** and log in.
2. **Tracing → Traces** — the newest trace is your run; the name is the task
   text. Or filter by metadata `task_id`.
3. Click through the node spans; the I/O panes answer "what did this node see
   and change", the child spans answer "what did each model call cost".

The credentials live in 1Password (`Dev-Secrets/langfuse-general-testing`);
`LANGFUSE_HOST` must point at `http://localhost:3100` (a cloud host with these
keys 401s and tracing degrades to a no-op — the run still works, but silently
untraced; the startup log warns `Langfuse credentials rejected`).

## The local run trace (fallback)

Always written, no network needed:

```
<WORKSPACE_ROOT>/.stromboli/runs/<task_id>/trace.md      # human-readable
<WORKSPACE_ROOT>/.stromboli/runs/<task_id>/trace.jsonl   # machine-readable
```

One record per node with the same output summary the Langfuse span gets
(status, `tokens_used`, verdict, diff size, test results). The run's log line
`Run trace: …` prints the exact directory.

## Live progress

The runner logs one line per Agent SDK turn as it streams
(`coding turn 3: tools=['Edit'] output_tokens=…`) and one line per sandbox
invocation (`Sandbox: docker run …`). Set `STROMBOLI_LOG_FILE` to persist logs;
finished sandbox containers are kept (labelled `stromboli=sandbox`) for
`docker logs` inspection.

## Budgets you'll see enforced

- `MAX_INNER_TURNS` — SDK agent-loop turns per coding attempt (SDK-enforced;
  a bounded exit shows as `subtype=error_max_turns`, not a failure).
- `MAX_OUTER_REVISIONS` — revise edges before the verdict gate escalates.
- `MAX_TOKENS_PER_TASK` — cumulative `tokens_used` (coder turns + gateway
  calls); once exceeded the verdict gate refuses further revise cycles and
  escalates to the human interrupt.
