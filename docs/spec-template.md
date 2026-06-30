# Writing a task spec that won't be flagged ambiguous

Stromboli's **Spec node** (`src/stromboli/nodes/spec.py`) turns your task request
into a structured `Spec` and sets `ambiguous=true` **only when it can't pin down
*testable* acceptance criteria**. If that flag is set, the task suspends to
`Queued` for clarification instead of being coded.

So the one rule is:

> **Could someone write a pass/fail test from this request without guessing?**
> Yes → it flows `spec → prompt → coding → verify`. No → it goes to `Queued`.

## The framework — fill these 5 (they are the `Spec` schema)

| # | Field | What to write | Make-or-break? |
|---|---|---|---|
| 1 | **Goal** | One sentence: what exists/changes when done. | — |
| 2 | **Acceptance criteria** | 2–5 **checkable** bullets — each a pass/fail fact. | ⭐ this *is* the gate |
| 3 | **Affected paths** | Which repo + files/modules (needs the **Project relation** set). | needed to build |
| 4 | **Inputs / outputs** | Concrete data **source**, output **format** + **destination**. | ⭐ commonly missing |
| 5 | **Constraints** | Libraries to use/avoid, style, what NOT to touch. | — |

**Test for criterion #2:** each bullet should read like a test —
*"running `X` produces `Y`"*, *"`GET /foo` returns 200 with field Z"*,
*"`pytest tests/test_bar.py` passes"*. Vague verbs (*"compiles", "handles",
"similar to"*) are exactly what trips the flag.

## Copy-paste template (put this in the task's **Spec** field)

```
Goal: <one sentence>
Acceptance criteria:
- <pass/fail check 1>
- <pass/fail check 2>
Affected paths: <files/dirs>
Inputs: <where data comes from>
Outputs: <format + destination>
Constraints: <use/avoid; don't touch>
```

## Example: ambiguous → buildable

**Before (flagged ambiguous):**
> Create a weekly scheduled researcher agent (similar framework) that compiles
> all research papers and long reads processed this week.

*Why flagged: the "framework" is undefined, the source of papers is unspecified,
and the output format/destination is missing — none of it is testable.*

**After (passes the gate):**
```
Goal: Add a weekly job that emails a markdown digest of that week's saved research items.
Acceptance criteria:
- build_weekly_digest(since: date) -> str returns markdown grouping items by
  topic with title + link + a 2-line summary.
- Items are read from Notion DB <DB_ID>, filtered to Created >= since.
- A cron entry runs it every Monday 08:00 and emails the output to me@x.com via
  the existing SMTP client.
- pytest tests/test_digest.py passes (fake Notion + fake mailer).
Affected paths: src/<repo>/digest.py, tests/test_digest.py, scheduler config.
Inputs: Notion DB <DB_ID>.
Outputs: markdown, delivered by email.
Constraints: reuse the existing Notion client + mailer; no new web framework.
```

## Two practical notes
- **Set the Project relation.** Even a perfect spec can't *build* without the
  repo set — that's a separate field, but required (a missing repo parks the
  task to Review on resume).
- **It helps downstream too.** The same acceptance criteria are what the
  (non-Claude) verifier checks the diff against — a sharp spec raises the
  pass-on-first-try rate, not just clears the ambiguity gate.
