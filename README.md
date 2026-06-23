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

## Configuration

All configuration is loaded from environment variables, optionally backed by a
local `.env` file (see `.env.example` for the full list). Every variable is
required; the worker fails fast on startup naming any that is missing.

`.env` is gitignored — never commit real secrets. Copy the example to start:

```bash
cp .env.example .env
```

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
