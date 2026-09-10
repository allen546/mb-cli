"""Task filtering and view helpers."""

from __future__ import annotations

from datetime import datetime
import re



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
    _update_summary_counts(result)
    result["subject_filter"] = subject
    return result


from .task_status import (
    GradeStatus,
    SubmissionStatus,
    classify_task_view,
    get_grade_status,
    get_submission_status,
    is_submitted_badge,
    is_task_completed,
    is_task_graded,
    is_task_submitted,
    is_task_submitted_or_graded,
    is_task_todo,
)

# Alias for backward compatibility
is_task_unfinished = is_task_todo


def matches_graded(task: dict, graded: bool) -> bool:
    """Return *True* if the task's graded state matches the *graded* query."""
    is_graded = get_grade_status(task) == GradeStatus.GRADED
    return is_graded == graded


def matches_submitted(task: dict, submitted: bool) -> bool:
    """Return *True* if the task's submission state matches the *submitted* query."""
    return is_task_submitted(task) == submitted


def matches_grade_query(task: dict, query: str) -> bool:
    """Return *True* if the task's grade matches the *query*.

    Supports:
      - Letters (e.g. "B" matches "B", "B+", "B-", whereas "B-" matches only "B-")
      - GPA to letter mappings (e.g. "4.0" -> "A", "A+", "3.7" -> "A-", etc.)
    """
    gl = task.get("grade_letter") or ""
    gs = task.get("grade_score") or ""
    
    # Try to find a grade code from letter or score (e.g. "A+", "B-", "A")
    grade_val = gl.strip().upper()
    if not grade_val:
        # Check if score starts with a grade letter (some lists output "A+ (95/100)")
        match = re.match(r"^([A-F][+-]?)\b", gs.strip().upper())
        if match:
            grade_val = match.group(1)

    if not grade_val:
        return False

    q = query.strip().upper()

    # Mappings from GPA to letter grades
    gpa_mapping = {
        "4.0": ["A", "A+"],
        "3.7": ["A-"],
        "3.3": ["B+"],
        "3.0": ["B"],
        "2.7": ["B-"],
        "2.3": ["C+"],
        "2.0": ["C"],
        "1.7": ["C-"],
        "1.3": ["D+"],
        "1.0": ["D"],
        "0.0": ["F"],
    }
    if q in gpa_mapping:
        return grade_val in gpa_mapping[q]

    # Letter matching logic:
    # If query is a single letter (A, B, C, D, F), match any modifier (+, -)
    if len(q) == 1 and q.isalpha():
        return grade_val.startswith(q)

    # Otherwise exact match (e.g. "B-" matches only "B-")
    return grade_val == q


def _update_summary_counts(result: dict) -> None:
    """Recalculate summary counts in-place for a result dict."""
    result["summary"] = {
        "upcoming_count": len(result["upcoming"]),
        "past_count": len(result["past"]),
        "overdue_count": len(result["overdue"]),
        "total_count": len(result["upcoming"])
        + len(result["past"])
        + len(result["overdue"]),
    }


def matches_tag(task: dict, tag_query: str) -> bool:
    """Return *True* if the task's labels match the logical tag query (case-insensitive).

    Supports:
      - OR operators: 'tagA,tagB', 'tagA|tagB', 'tagA or tagB'
      - AND operators: 'tagA+tagB', 'tagA&tagB', 'tagA and tagB'
    """
    labels = task.get("labels") or []
    if not labels:
        return False
    
    label_set = {lbl.casefold() for lbl in labels}
    query = tag_query.casefold()

    # Check for OR operators first
    or_splitters = [",", "|", " or "]
    for splitter in or_splitters:
        if splitter in query:
            parts = [p.strip() for p in query.split(splitter) if p.strip()]
            return any(
                any(part in lbl for lbl in label_set)
                for part in parts
            )

    # Check for AND operators
    and_splitters = ["+", "&", " and "]
    for splitter in and_splitters:
        if splitter in query:
            parts = [p.strip() for p in query.split(splitter) if p.strip()]
            return all(
                any(part in lbl for lbl in label_set)
                for part in parts
            )

    # Single tag fallback
    return any(query in lbl for lbl in label_set)


def matches_completed(task: dict, completed: bool) -> bool:
    """Return *True* if the task's completion state matches the *completed* query."""
    return is_task_completed(task) == completed


def filter_result_by_status(
    result: dict,
    graded: bool | None = None,
    submitted: bool | None = None,
    grade: str | None = None,
    tag: str | None = None,
    completed: bool | None = None,
) -> dict:
    """Filter a crawl result dict in-place by status/grade/tag/completed attributes and update counts."""
    for section in ("upcoming", "past", "overdue"):
        tasks = result.get(section, [])
        if graded is not None:
            tasks = [t for t in tasks if matches_graded(t, graded)]
        if submitted is not None:
            tasks = [t for t in tasks if matches_submitted(t, submitted)]
        if grade is not None:
            tasks = [t for t in tasks if matches_grade_query(t, grade)]
        if tag is not None:
            tasks = [t for t in tasks if matches_tag(t, tag)]
        if completed is not None:
            tasks = [t for t in tasks if matches_completed(t, completed)]
        result[section] = tasks

    _update_summary_counts(result)
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
