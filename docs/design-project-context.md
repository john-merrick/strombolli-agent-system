# Design — per-project context for the Spec node

Status: **implemented, green**. Brainstormed + built 2026-06-30.

## Problem
Tasks were specced "cold": the Spec model (Gemini) saw only the raw request +
memory snippets, never the project's conventions. So acceptance criteria missed
project norms, and requests that a project doc would have clarified got flagged
`ambiguous` → `Queued` (e.g. the "Weekly researcher" task).

## Decision log
- **DL-1.** Project context = the file at the Project's **`Context Root`** GitHub
  *blob* URL (e.g. `…/blob/main/README.md`). Self-contained: parse
  `owner/repo/ref/path`, fetch via the GitHub contents API, base64-decode. No
  filename convention, no repo cross-reference. *(Alt considered: repo-root
  `CLAUDE.md` convention, or a directory + filename — rejected; the explicit URL
  is simpler and handles README/monorepo/renamed files.)*
- **DL-2.** Inject into the **Spec node only**. The coder auto-loads `CLAUDE.md`
  from its worktree; prompt node + verifier are out of scope.
- **DL-3.** Blank / unresolvable `Context Root` → **proceed with no context**
  (never a failure or a Queued).
- **DL-4.** Budget ~**8k chars** (head-biased); **per-process cache** keyed by
  URL; fetch at the **ref embedded in the URL**; trusted input.

## Shape (where it lives)
- `integrations/project_context.py` — `parse_blob_url`, `github_file_fetcher`
  (blob URL → file text via GitHub API), and `make_project_context(notion, fetch,
  budget)` → a `Callable[[StromboliState], str]` that resolves the Project's
  `Context Root`, fetches + truncates, labels it, and caches per URL. Every
  failure path returns `""`.
- `integrations/notion.py` — `PROP_CONTEXT_ROOT = "Context Root"`,
  `parse_context_root(page)`, and `NotionTaskClient.get_project_context_url(task)`.
- `nodes/spec.py` — `make_spec(..., project_context=…)`; when it returns a
  non-empty block it's prepended to the Spec prompt as *"Project conventions
  (from <url>): …"* (guidance, not hard constraints).
- Wiring — `GraphDeps.project_context`, built in `_deps_from_settings`
  (`make_project_context(notion, fetch=github_file_fetcher(github_token))`),
  passed into `make_spec` in both the `build_graph` and the phases paths.

## Notion setup (per project)
Add a **`Context Root`** rich-text property to the Project, set to the GitHub blob
URL of the context file. Verified live on **AI News** →
`https://github.com/john-merrick/ai_news_agent/blob/main/README.md`.

## Tests
`tests/integrations/test_project_context.py` (parse, label, truncate, cache,
blank/404/notion-error → ""), `tests/nodes/test_spec.py` (injection into the
prompt), `tests/integrations/test_notion.py` (`parse_context_root`). All offline
with fakes — no live GitHub/Notion.
