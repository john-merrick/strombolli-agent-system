# Stromboli

Stromboli is an agentic coding-triage system. A task captured and spec'd in
Notion dispatches a self-hosted Python worker that builds the change via the
Ralph loop in an isolated git worktree, runs CI, opens a pull request, and
writes the results back to Notion.

## Development

The project is managed with [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev      # install runtime + dev dependencies
uv run pytest            # tests
uv run ruff check .      # lint
uv run mypy              # type check
```

## Running the worker

Once `.env` is populated (see Configuration), start the worker with:

```bash
uv run python -m stromboli            # serves on 127.0.0.1:8000 by default
```

Override the bind address with `STROMBOLI_HOST` / `STROMBOLI_PORT` and the log
level with `STROMBOLI_LOG_LEVEL`. The entrypoint loads settings (failing fast on
any missing variable), assembles the full build pipeline
(`stromboli.app.create_stromboli_app`), and serves the dispatch API behind the
Cloudflare Tunnel. For Langfuse tracing also install the optional extra:

```bash
uv sync --extra dev --extra observability
```

## Configuration

All configuration is loaded from environment variables, optionally backed by a
local `.env` file (see `.env.example` for the full list). Every variable is
required; the worker fails fast on startup naming any that is missing.

`.env` is gitignored — never commit real secrets. Copy the example to start:

```bash
cp .env.example .env
```

## Build engines

Stromboli ships two interchangeable build engines, selected by the optional
`STROMBOLI_ENGINE` environment variable. Both produce the same `BuildResult`,
which flows through one shared integrate/finalize path (open a PR, route
Review/Complete, write feedback back, record the Langfuse trace), so switching
engines never changes how a build is integrated.

| `STROMBOLI_ENGINE` | Engine | Notes |
|--------------------|--------|-------|
| `ralph` (default)  | The Ralph loop (`stromboli.loop`) | Compiles the task Spec into `scripts/prd.json` and iterates `claude -p` over it. Unchanged, battle-tested default. |
| `graph`            | The multi-agent graph engine (`stromboli.engine`) | A deterministic policy drives four agent nodes (below). Opt-in; flip the flag to try it. |

The default is `ralph` — the graph engine is strictly additive and only runs
when you set `STROMBOLI_ENGINE=graph`.

### The graph engine

A pure `DeterministicPolicy` (the brain) maps the on-disk build state to the
next action; the `GraphEngine` orchestrator dispatches the matching node,
persists state to `.stromboli/state.json` (so a build is resumable), and meters
every agent's cost into the circuit breaker. Four nodes do the work:

- **Planner** — decomposes the task Spec into independently-verifiable units
  (each with an acceptance criterion and a definition of done).
- **Worker** — writes real code and tests in the worktree for one unit.
- **Verifier** — judges the diff against the criterion and flags *hollow tests*;
  a hollow or unmet verdict never integrates.
- **Reflector** — on a rejection, produces concrete feedback for the next worker
  attempt, or requests a re-plan when a unit is mis-specified.

Between worker attempts, the **objective gate** runs the target repo's real
checks. Units that exhaust their attempt budget (K), stall (no-progress), or a
breaker pre-empt all route the whole build to **Review** — Stromboli never ships
a partial build, and there is no auto-merge.

### Per-repo gate config

The objective gate defaults to `uv run pytest -q`, `ruff check .`, and `mypy`,
distinguishing a tooling *crash* (e.g. pytest exit 2–5) from a check *failure*.
Repos that build differently can override the commands by supplying
`gate_commands` to `BuildDeps` (e.g. a `make test` target or a different type
checker). The gate runner short-circuits on the first non-passing command.

### Debugging a build

When a graph build fails or routes to Review, three places explain why — read
them in this order:

1. **Notion feedback summary** — the top-level triage: completed count, PR link,
   blocked items + reasons, breaker note. Start here.
2. **`.stromboli/` in the worktree** (ships with the PR) — the build's black box:
   - `transcripts/NNN-<action>.md` — one file per step, in order, with the exact
     prompt each agent saw, its raw output, and (for worker steps) the objective
     gate's command + result. This is where you tell "the agent was wrong" from
     "my criterion was ambiguous."
   - `state.json` — the enriched timeline (`history`): each step's action,
     outcome, verdict/gate details, and a pointer to its transcript.
   - `feedback.md` — the append-only trail of every reflection / gate failure.
3. **Langfuse** — cross-run cost / token / latency aggregates per span.

Process logs go to stderr; set `STROMBOLI_LOG_FILE` to also persist them (and
full tracebacks) to a file — the console handler is kept either way.

### Live smoke (manual, behind the flag)

The graph engine is validated end-to-end by hand before being made default:
run one real Notion task through it in a sandbox repo, then inspect the Langfuse
trace (planner / worker / verifier / reflector spans) and the resulting PR.

```bash
STROMBOLI_ENGINE=graph uv run python -m stromboli
```

A run succeeds when a real task produces a verified PR, or a Review entry with
coherent feedback, end-to-end. No auto-merge — a human reviews the PR.

## Dispatch API

The worker exposes a small FastAPI surface (`stromboli.api.create_app`):

| Method & path             | Auth                 | Behaviour |
|---------------------------|----------------------|-----------|
| `GET /healthz`            | none                 | Returns `200 {"status": "ok"}` for liveness checks. |
| `POST /stromboli/dispatch`| `X-Stromboli-Secret` | Validates the shared secret, returns `202 Accepted` immediately, and processes the build asynchronously. |

### `POST /stromboli/dispatch` contract

Request body:

```json
{ "page_id": "<notion-task-page-id>" }
```

The request **must** carry the shared secret in the `X-Stromboli-Secret`
header, matching `DISPATCH_SHARED_SECRET`. A missing or invalid secret returns
`401 Unauthorized` and the build is not scheduled. A valid request returns
`202 Accepted` with `{ "status": "accepted", "page_id": "..." }`; the build runs
in the background so the caller is never blocked on the build duration.

### Cloudflare Tunnel mapping

The worker runs on a self-hosted machine and is exposed publicly via a
[Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/).
Point a public hostname at the locally-bound app (e.g. `127.0.0.1:8000`):

```yaml
# ~/.cloudflared/config.yml
tunnel: <tunnel-uuid>
credentials-file: /path/to/<tunnel-uuid>.json
ingress:
  - hostname: stromboli.example.com
    service: http://127.0.0.1:8000
  - service: http_status:404
```

`TUNNEL_PUBLIC_URL` should be set to the public hostname (e.g.
`https://stromboli.example.com`) so the Notion automation can reach the worker.

### Notion "Ready checked" automation trigger

In the Notion task database, configure a database automation:

* **Trigger:** the `Ready` checkbox property is checked (`Ready` → checked).
* **Action:** send a webhook `POST` to `${TUNNEL_PUBLIC_URL}/stromboli/dispatch`
  with the `X-Stromboli-Secret` header set to `DISPATCH_SHARED_SECRET` and a JSON
  body of `{ "page_id": "<this page's id>" }`.

The worker re-reads the task and re-validates the dispatch guard
(`Ready == true AND Assigned to == Agent AND Status == To do`) before building,
so the automation only needs to fire the webhook — claim safety lives in the
worker (see the dispatch guard story).

## CI verification workflow

Every PR the worker opens is gated on the same checks the project enforces
locally: tests, lint, and type checks. The reusable workflow lives at
[`.github/workflows/ci.yml`](.github/workflows/ci.yml). It runs on this repo's
own pull requests and is also exposed as a reusable workflow (`workflow_call`),
so a target repo can opt in with a thin caller workflow:

```yaml
# .github/workflows/stromboli-ci.yml in the TARGET repo
name: CI
on: pull_request
jobs:
  verify:
    uses: snarktank/stromboli/.github/workflows/ci.yml@main
```

The check **fails the PR** when any of tests, lint, or types fail, so the worker
never lands a red branch. CodeQL and the spec eval suite are explicitly deferred
to phase 2 (see Non-Goals).
