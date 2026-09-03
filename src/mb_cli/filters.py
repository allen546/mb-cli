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


def matches_graded(task: dict, graded: bool) -> bool:
    """Return *True* if the task's graded state matches the *graded* query."""
    has_grade = bool(task.get("grade_letter") or task.get("grade_score"))
    return has_grade == graded


def is_submitted_badge(badge: str) -> bool:
    """Return True if badge indicates submission without negative qualifiers ('not', 'un')."""
    b = str(badge or "").strip().lower()
    return "submitted" in b and "not" not in b and "un" not in b


def is_task_submitted(task: dict) -> bool:
    """Return True if task has been submitted based on status, labels, or details."""
    status = str(task.get("status") or "").lower()
    if status == "submitted":
        return True

    labels = task.get("labels") or []
    if any(is_submitted_badge(l) for l in labels):
        return True

    detail = task.get("detail") or {}
    if str(detail.get("status") or "").lower() == "submitted":
        return True
    if any(is_submitted_badge(l) for l in (detail.get("labels") or [])):
        return True
    if detail.get("submission") or detail.get("submissions"):
        return True

    return False


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


def is_task_unfinished(task: dict) -> bool:
    """Return True if task has_submit_button, is not submitted, has no completed passing grade (ignores 0/ score), and is not assessed yet."""
    if is_task_submitted(task):
        return False

    detail = task.get("detail") or {}
    labels = (task.get("labels") or []) + (detail.get("labels") or [])
    labels_lower = [str(l).lower() for l in labels]

    grade_letter = task.get("grade_letter") or detail.get("grade_letter")
    grade_score = task.get("grade_score") or detail.get("grade_score")

    is_zero_score = False
    if grade_score and re.match(r"^\s*0\s*/", str(grade_score)):
        is_zero_score = True

    has_score = bool(grade_score and str(grade_score).strip() and str(grade_score).strip() != "-")
    has_letter = bool(grade_letter and str(grade_letter).strip())
    has_completed_grade = (has_score or has_letter) and not is_zero_score

    is_not_assessed = "not assessed yet" in labels_lower or (
        bool(grade_letter) and "not assessed" in str(grade_letter).lower()
    )
    has_submit_btn = bool(
        task.get("has_submit_button", False)
        or detail.get("has_submit_button", False)
    )

    return has_submit_btn and not has_completed_grade and not is_not_assessed


def is_task_completed(task: dict) -> bool:
    """Return True if a task is not unfinished."""
    return not is_task_unfinished(task)


def classify_task_view(task: dict, now_ref: datetime | None = None) -> str:
    """Classify a task into 'upcoming', 'overdue', or 'past' based on due date and status."""
    from .client import parse_due_date

    now = now_ref or datetime.now()
    due_date = task.get("due_date")
    if isinstance(due_date, datetime):
        due_dt = due_date
    elif due_date:
        due_dt = parse_due_date(due_date, now_ref=now)
    else:
        due_dt = None

    if due_dt:
        if due_dt.tzinfo is not None and now.tzinfo is None:
            due_dt = due_dt.astimezone().replace(tzinfo=None)
        elif due_dt.tzinfo is None and now.tzinfo is not None:
            now = now.astimezone().replace(tzinfo=None)

        if due_dt > now:
            return "upcoming"

    if is_task_unfinished(task):
        return "overdue"
    return "past"


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
