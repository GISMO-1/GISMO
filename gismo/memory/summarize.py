"""Run summarization: build memory items from completed runs."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from gismo.core.models import Run, Task, TaskStatus


@dataclass(frozen=True)
class RunSummaryItem:
    """A single memory item produced by run summarization."""

    key: str
    kind: str
    value: Any
    tags: list[str]
    confidence: str
    source: str = "system"


@dataclass(frozen=True)
class RunSummaryPlan:
    """A plan of memory items to write for a run. Not yet applied."""

    run_id: str
    namespace: str
    items: list[RunSummaryItem]
    generated_at: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _task_status_value(task: Task) -> str:
    if isinstance(task.status, TaskStatus):
        return task.status.value
    return str(task.status)


def build_run_summary_plan(
    *,
    run: Run,
    tasks: list[Task],
    namespace: str,
    confidence: str = "medium",
    include_outputs: bool = False,
) -> RunSummaryPlan:
    """Build a RunSummaryPlan from a run and its tasks.

    Returns a plan describing what memory items would be written.
    Does not write anything.
    """
    now = _utc_now()
    items: list[RunSummaryItem] = []

    status_counts: dict[str, int] = {}
    for task in tasks:
        label = _task_status_value(task)
        status_counts[label] = status_counts.get(label, 0) + 1

    succeeded = status_counts.get(TaskStatus.SUCCEEDED.value, 0)
    failed = status_counts.get(TaskStatus.FAILED.value, 0)

    created_at_str = (
        run.created_at.isoformat()
        if hasattr(run.created_at, "isoformat")
        else str(run.created_at)
    )

    summary_value: dict[str, Any] = {
        "run_id": run.id,
        "label": run.label or None,
        "task_count": len(tasks),
        "succeeded": succeeded,
        "failed": failed,
        "status_counts": status_counts,
        "created_at": created_at_str,
        "summarized_at": now,
    }

    tags = ["run_summary"]
    if run.label:
        tags.append(f"run_label:{run.label}")

    items.append(
        RunSummaryItem(
            key=f"run/{run.id}/summary",
            kind="summary",
            value=summary_value,
            tags=tags,
            confidence=confidence,
        )
    )

    if include_outputs:
        for task in tasks:
            task_value: dict[str, Any] = {
                "run_id": run.id,
                "task_id": task.id,
                "title": task.title,
                "status": _task_status_value(task),
                "summarized_at": now,
            }
            if task.output_json is not None:
                task_value["output"] = task.output_json
            if task.error:
                task_value["error"] = task.error
            if task.status_reason:
                task_value["status_reason"] = task.status_reason
            task_tags = [
                "run_summary",
                "task_output",
                f"task_status:{_task_status_value(task)}",
            ]
            items.append(
                RunSummaryItem(
                    key=f"run/{run.id}/task/{task.id}",
                    kind="summary",
                    value=task_value,
                    tags=task_tags,
                    confidence=confidence,
                )
            )

    return RunSummaryPlan(
        run_id=run.id,
        namespace=namespace,
        items=items,
        generated_at=now,
    )
