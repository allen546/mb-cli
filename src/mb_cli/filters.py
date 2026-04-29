"""Task filtering and view helpers."""

from __future__ import annotations


def matches_subject(task: dict, subject: str) -> bool:
    """Return *True* if *task*'s class name contains *subject* (case-insensitive)."""
    class_name = task.get("class_name")
    if not class_name:
        return False
    return subject.casefold() in class_name.casefold()


def filter_result_by_subject(result: dict, subject: str) -> dict:
    """Filter a crawl result dict in-place by subject and update summary counts."""
    result["upcoming"] = [t for t in result["upcoming"] if matches_subject(t, subject)]
    result["past"] = [t for t in result["past"] if matches_subject(t, subject)]
    result["overdue"] = [t for t in result["overdue"] if matches_subject(t, subject)]
    result["summary"] = {
        "upcoming_count": len(result["upcoming"]),
        "past_count": len(result["past"]),
        "overdue_count": len(result["overdue"]),
        "total_count": len(result["upcoming"])
        + len(result["past"])
        + len(result["overdue"]),
    }
    result["subject_filter"] = subject
    return result


def result_views(result: dict, requested_view: str) -> dict:
    """Return only the requested view section from a crawl result."""
    if requested_view == "upcoming":
        return {"upcoming": result["upcoming"], "past": [], "overdue": []}
    if requested_view == "past":
        return {"upcoming": [], "past": result["past"], "overdue": []}
    if requested_view == "overdue":
        return {"upcoming": [], "past": [], "overdue": result["overdue"]}
    return {
        "upcoming": result["upcoming"],
        "past": result["past"],
        "overdue": result["overdue"],
    }


def find_task_by_id(result: dict, task_id: str) -> dict | None:
    """Find a task by its ID across all views."""
    for task in result["upcoming"] + result["past"] + result["overdue"]:
        if task.get("id") == task_id:
            return task
    return None
