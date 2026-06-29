"""Windmill script — the Notion front-end: list Ready task ids to triage.

Attach a Windmill **schedule** to this script (e.g. every 30s); for each id it
returns, trigger the ``stromboli_triage`` flow (via a flow-per-item, or a small
runner). Returns ``[{task_id, name}]`` so the schedule/flow can fan out.
"""

from stromboli.integrations.notion import NotionTaskClient
from stromboli.settings import load_settings


def main() -> list[dict]:
    settings = load_settings()
    notion = NotionTaskClient(settings.notion_token)
    return [
        {"task_id": t.page_id, "name": t.name}
        for t in notion.query_ready_tasks(settings.notion_task_db_id)
    ]
