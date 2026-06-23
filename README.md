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
