"""ManageBac HTTP client — login, parse task tiles, crawl all views."""

from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from datetime import datetime
from urllib.parse import urljoin, unquote

import requests
from bs4 import BeautifulSoup

from .cache import ResponseCache

log = logging.getLogger(__name__)

# Retryable HTTP status codes (server errors that may resolve on retry)
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}


def parse_due_date(due_date_str: str, now_ref: datetime | None = None) -> datetime | None:
    """Parse due date string with year wrapping correction."""
    if not due_date_str:
        return None
    try:
        cleaned = re.sub(r"^[A-Za-z]+,\s*", "", due_date_str).strip()
        ref = now_ref or datetime.now()
        current_year = ref.year

        dt = None
        for fmt in ("%b %d, %I:%M %p", "%b %d"):
            try:
                parsed = datetime.strptime(f"{cleaned} {current_year}", f"{fmt} %Y")
                dt = parsed
                break
            except ValueError:
                continue

        if dt:
            diff = dt - ref
            if diff.days > 180:
                dt = dt.replace(year=current_year - 1)
            elif diff.days < -180:
                dt = dt.replace(year=current_year + 1)
            return dt
    except Exception:
        pass
    return None


class ManageBacClient:
    """HTTP client for ManageBac with session-based auth.

    Parameters
    ----------
    school : str
        School subdomain, e.g. ``"bj80"`` for ``bj80.managebac.cn``.
    domain : str
        Base domain.  ``"managebac.com"`` (default) or ``"managebac.cn"``
        for mainland-China instances.
    """

    def __init__(
        self,
        school: str,
        domain: str = "managebac.com",
        cache: ResponseCache | None = None,
        verify: bool | str = True,
        retry: int = 3,
        request_delay: float = 1.0,
    ):
        self.school = school.replace(f".{domain}", "")
        self.domain = domain
        self.base = f"https://{self.school}.{domain}"
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.session.verify = verify
        self.student_name: str | None = None
        self.cache = cache or ResponseCache()
        self.retry = retry
        self.request_delay = request_delay
        self._last_request_time: float = 0.0
        self._last_url: str | None = None
        self._url_locks: dict[str, threading.Lock] = {}
        self._url_locks_mutex = threading.Lock()

    # ── Auth ────────────────────────────────────────────────────────────

    def login(self, email: str, password: str, remember: bool = True) -> bool:
        """Authenticate with email + password.  Returns *True* on success."""
        r = self._request_with_retry("GET", f"{self.base}/login")
        soup = BeautifulSoup(r.text, "html.parser")
        token_el = soup.find("input", {"name": "authenticity_token"})
        if not token_el:
            log.warning("could not find authenticity_token on login page")
            return False

        r = self._request_with_retry(
            "POST",
            f"{self.base}/sessions",
            data={
                "authenticity_token": token_el["value"],
                "login": email,
                "password": password,
                "remember_me": "1" if remember else "0",
                "commit": "Sign in",
            },
            allow_redirects=True,
        )

        log.debug("login POST final url=%s status=%d", r.url, r.status_code)
        # A successful login always redirects away from /sessions.
        # If we land back on /sessions (200 with no redirect) the credentials
        # were rejected by the server (wrong password, MFA, etc.).
        if "/sessions" in r.url and not r.history:
            log.warning("login failed — server returned 200 on /sessions (bad credentials?)")
            return False
        if "/login" in r.url or "login" in r.url.split("/")[-1]:
            log.warning("login failed — redirected back to login page")
            return False
        return True

    def set_cookie(self, cookie_value: str) -> None:
        """Inject a ``_managebac_session`` cookie directly."""
        self.session.cookies.set(
            "_managebac_session",
            cookie_value,
            domain=f"{self.school}.{self.domain}",
        )

    def invalidate_cache(self) -> None:
        """Clear the entire response cache."""
        self.cache.invalidate()

    # ── Retry logic ─────────────────────────────────────────────────────

    def _is_retryable(self, exc: Exception) -> bool:
        if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
            return True
        if isinstance(exc, requests.HTTPError) and exc.response is not None:
            return exc.response.status_code in _RETRYABLE_STATUS_CODES
        return False

    def _request_with_retry(self, method: str, url: str, **kwargs) -> requests.Response:
        # Enforce rate limit / delay between requests
        now = time.time()
        elapsed = now - getattr(self, "_last_request_time", 0.0)
        min_delay = getattr(self, "request_delay", 1.0)
        if elapsed < min_delay:
            sleep_time = (min_delay - elapsed) * random.uniform(0.75, 1.25)
            time.sleep(max(0.0, sleep_time))
        self._last_request_time = time.time()

        headers = kwargs.pop("headers", {}) or {}
        if self._last_url and "Referer" not in headers:
            headers["Referer"] = self._last_url
        last_exc: Exception | None = None
        for attempt in range(self.retry + 1):
            try:
                r = self.session.request(method, url, headers=headers, **kwargs)
                r.raise_for_status()
                self._last_url = url
                return r
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc
                if attempt < self.retry:
                    delay = 2**attempt
                    log.warning(
                        "%s %s failed (attempt %d/%d), retrying in %ds...",
                        method,
                        url,
                        attempt + 1,
                        self.retry + 1,
                        delay,
                    )
                    time.sleep(delay)
            except requests.HTTPError as exc:
                if self._is_retryable(exc) and attempt < self.retry:
                    last_exc = exc
                    delay = 2**attempt
                    log.warning(
                        "%s %s returned %d (attempt %d/%d), retrying in %ds...",
                        method,
                        url,
                        exc.response.status_code,
                        attempt + 1,
                        self.retry + 1,
                        delay,
                    )
                    time.sleep(delay)
                else:
                    raise
        raise last_exc  # type: ignore[misc]

    # ── Internal helpers ────────────────────────────────────────────────

    def _get_url_lock(self, url: str) -> threading.Lock:
        with self._url_locks_mutex:
            if url not in self._url_locks:
                self._url_locks[url] = threading.Lock()
            return self._url_locks[url]

    def _get(self, path: str, bypass_cache: bool = False) -> BeautifulSoup:
        url = f"{self.base}{path}"
        if not bypass_cache:
            cached = self.cache.get(url)
            if cached is not None:
                body, status = cached
                soup = BeautifulSoup(body, "html.parser")
                if "/login" in url:
                    raise RuntimeError("Session expired or invalid — redirected to login")
                self._capture_student_name(soup)
                return soup

        lock = self._get_url_lock(url)
        with lock:
            if not bypass_cache:
                cached = self.cache.get(url)
                if cached is not None:
                    body, status = cached
                    soup = BeautifulSoup(body, "html.parser")
                    if "/login" in url:
                        raise RuntimeError("Session expired or invalid — redirected to login")
                    self._capture_student_name(soup)
                    return soup

            try:
                r = self._request_with_retry("GET", url)
                if "/login" in r.url:
                    raise RuntimeError("Session expired or invalid — redirected to login")
                self.cache.put(url, r.text, r.status_code)
                soup = BeautifulSoup(r.text, "html.parser")
                self._capture_student_name(soup)
                return soup
            except Exception as e:
                cached = self.cache.get(url, allow_stale=True)
                if cached is not None:
                    body, status = cached
                    log.warning("Request to %s failed (%s) — loading stale cached content", url, e)
                    soup = BeautifulSoup(body, "html.parser")
                    self._capture_student_name(soup)
                    return soup
                raise

    def _capture_student_name(self, soup: BeautifulSoup) -> None:
        if self.student_name:
            return
        for a in soup.find_all("a", href="/student/profile"):
            text = a.get_text(strip=True)
            if text and "Manage" in text:
                self.student_name = (
                    text.split("Manage")[0].strip().rstrip("\u2014\u2015- ")
                )
                return

    # ── Tile parsing (Faria "f-tile" UI) ────────────────────────────────

    def _parse_tile(self, tile) -> dict | None:
        a = tile.find("a", class_=re.compile(r"f-tile__title-link"))
        if not a:
            return None
        title = a.get_text(strip=True)
        link = a.get("href", "")

        # Task ID
        task_id = None
        if "core_tasks/" in link:
            task_id = link.split("core_tasks/")[-1].rstrip("/")
        elif link:
            task_id = link.rstrip("/").split("/")[-1]

        # Due date & class name
        desc = tile.find("div", class_="f-tile__description")
        due_date = class_name = None
        if desc:
            for s in desc.find_all("span", recursive=True):
                t = s.get_text(strip=True)
                cls = s.get("class", [])
                if not t or "badge" in str(cls) or "fi" in str(cls):
                    continue
                if not due_date and re.search(r"[A-Z][a-z]{2}\s+\d", t):
                    due_date = t
                    break
            class_link = desc.find("a", href=re.compile(r"/student/classes/"))
            if class_link:
                class_name = class_link.get_text(strip=True)

        # Labels / badges
        labels = []
        for badge in tile.find_all("span", class_=re.compile(r"^badge$|color-box")):
            badge_text = badge.get_text(strip=True)
            if badge_text and badge_text not in labels:
                labels.append(badge_text)

        # Grade
        grade_letter = grade_score = None
        suffix = tile.find("div", class_=re.compile(r"f-tile__suffix"))
        if suffix:
            score_div = suffix.find("div", class_=re.compile(r"f-task-score"))
            if score_div:
                h4 = score_div.find("h4")
                p = score_div.find("p")
                grade_letter = h4.get_text(strip=True) if h4 else None
                grade_score = p.get_text(" ", strip=True) if p else None
            else:
                raw = suffix.get_text(" ", strip=True)
                if raw:
                    grade_score = raw

        return {
            "title": title,
            "link": f"{self.base}{link}" if link.startswith("/") else link,
            "id": task_id,
            "due_date": due_date,
            "class_name": class_name,
            "labels": labels or None,
            "grade_letter": grade_letter,
            "grade_score": grade_score,
        }

    def _parse_tasks_page(self, soup: BeautifulSoup) -> list[dict]:
        self._capture_student_name(soup)
        tiles = soup.find_all("div", class_=re.compile(r"f-task-tile"))
        return [t for tile in tiles if (t := self._parse_tile(tile))]

    def _has_next_page(self, soup: BeautifulSoup, page: int, view: str) -> bool:
        next_page = page + 1
        # 1. Look for any link with page={next} (most robust — don't require view param)
        for a in soup.find_all("a", href=re.compile(rf"page={next_page}")):
            return True
        # 2. Look for rel="next" link
        if soup.find("a", rel="next"):
            return True
        # 3. Look for a "next" button (class or aria-label containing "next")
        for el in soup.find_all(["a", "button"], attrs={"rel": "next"}):
            return True
        for el in soup.find_all(["a", "button"]):
            classes = " ".join(el.get("class", []))
            aria = el.get("aria-label", "")
            if "next" in classes.lower() or "next" in aria.lower():
                if not el.get("disabled") and "disabled" not in classes.lower():
                    return True
        return False

    def _text_from_block(self, node, limit: int | None = None) -> str | None:
        if not node:
            return None
        text = node.get_text("\n", strip=True)
        if not text:
            return None
        return text[:limit] if limit else text

    def _extract_attachments(self, soup: BeautifulSoup) -> list[dict]:
        attachments: list[dict] = []
        seen: set[tuple[str, str]] = set()

        for link in soup.find_all("a", href=True):
            href = (link.get("href") or "").strip()
            if not href:
                continue
            lower_href = href.casefold()
            if lower_href.startswith(("javascript:", "mailto:", "tel:", "#")):
                continue

            link_classes = " ".join(link.get("class", []))
            parent_classes = (
                " ".join(link.parent.get("class", [])) if link.parent else ""
            )
            context = f"{link_classes} {parent_classes}".casefold()
            text = link.get_text(" ", strip=True)
            filename_hint = href.split("?")[0].rstrip("/").split("/")[-1]
            looks_like_file = bool(
                re.search(r"\.[a-z0-9]{2,8}$", filename_hint, re.IGNORECASE)
            )
            attachmentish = (
                "fr-file" in context
                or "/attachments/" in lower_href
                or "/uploads/" in lower_href
                or any(
                    marker in context
                    for marker in (
                        "attachment",
                        "resource",
                        "upload",
                        "download",
                        "file",
                    )
                )
            )
            if not (looks_like_file or attachmentish):
                continue

            url = urljoin(f"{self.base}/", href)
            source = "description"
            if link.find_parent(class_=re.compile(r"discussion", re.IGNORECASE)):
                source = "discussion"
            elif (
                link.find_parent(class_=re.compile(r"dropbox|submission|coursework", re.IGNORECASE))
                or link.find_parent("tr", class_=re.compile(r"file", re.IGNORECASE))
            ):
                source = "submission"

            name = (
                text or link.get("data-name") or filename_hint or url.rsplit("/", 1)[-1]
            )
            if not name:
                continue
            name = unquote(name)
            if source == "description":
                data_name = link.get("data-name")
                if data_name:
                    name = unquote(data_name)
                elif (
                    text
                    and "\n" not in text
                    and re.search(r"\.[a-z0-9]{2,8}(?:\s|$)", text, re.IGNORECASE)
                ):
                    name = unquote(text.splitlines()[0].strip())
            elif source == "submission" and text.casefold() in {
                "view teacher feedback",
                "view feedback",
            }:
                continue

            base_url = url.split("?")[0]
            key = (name, base_url)
            if key in seen:
                continue
            seen.add(key)
            attachments.append(
                {
                    "name": name,
                    "url": url,
                    "source": source,
                }
            )

        return attachments

    # ── CSRF helper ─────────────────────────────────────────────────────

    def _get_csrf(self, soup: BeautifulSoup) -> str | None:
        meta = soup.find("meta", {"name": "csrf-token"})
        return meta["content"] if meta else None

    # ── Notifications ───────────────────────────────────────────────────

    def get_notification_token(self) -> tuple[str, str]:
        """Extract MNN hub endpoint and JWT from the notifications page.

        Returns ``(hub_endpoint, jwt_token)``.
        """
        soup = self._get("/student/notifications")
        trigger = soup.find("a", class_="js-messages-and-notifications-trigger")
        if not trigger:
            raise RuntimeError("Could not find notification trigger on page")
        return (
            trigger.get("data-mnn-hub-endpoint", ""),
            trigger.get("data-token", ""),
        )

    # ── File submission ─────────────────────────────────────────────────

    def submit_file(self, class_id: str, task_id: str, file_path: str) -> dict:
        """Upload a file to a task's dropbox.

        Returns ``{"ok": True, "filename": ..., "task_url": ...}``.
        """
        from pathlib import Path

        dropbox_path = f"/student/classes/{class_id}/core_tasks/{task_id}/dropbox"
        soup = self._get(dropbox_path, bypass_cache=True)
        csrf = self._get_csrf(soup)
        if not csrf:
            raise RuntimeError("Could not find CSRF token on dropbox page")

        form = soup.find("form", id=lambda x: x and x.startswith("edit_dropbox"))
        if not form:
            raise RuntimeError("Could not find upload form on dropbox page")

        upload_url = f"{self.base}/student/classes/{class_id}/core_tasks/{task_id}/dropbox/upload"
        p = Path(file_path)
        if not p.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        with p.open("rb") as fh:
            files = {
                "dropbox[assets_attributes][0][file]": (p.name, fh),
            }
            data = {
                "_method": "patch",
                "authenticity_token": csrf,
                "commit": "Upload Files",
            }
            headers = {
                "X-CSRF-Token": csrf,
                "X-Requested-With": "XMLHttpRequest",
            }
            r = self._request_with_retry(
                "POST", upload_url, data=data, files=files, headers=headers
            )

        task_url = f"{self.base}/student/classes/{class_id}/core_tasks/{task_id}"
        self.invalidate_cache()
        return {
            "ok": True,
            "filename": p.name,
            "task_url": task_url,
        }

    def get_submissions(self, class_id: str, task_id: str) -> list[dict]:
        """List current submissions on a task's dropbox page."""
        dropbox_path = f"/student/classes/{class_id}/core_tasks/{task_id}/dropbox"
        try:
            soup = self._get(dropbox_path)
        except Exception as e:
            return [{"error": str(e)}]

        submissions: list[dict] = []
        # Look for submitted file rows in the dropbox table
        for row in soup.find_all("tr"):
            link = row.find("a", href=True)
            if not link:
                continue
            href = link.get("href", "")
            if "/attachments/" not in href:
                continue
            name = link.get_text(strip=True)
            if not name or name.lower() in ("view teacher feedback", "view feedback"):
                continue
            submissions.append(
                {
                    "name": name,
                    "url": f"{self.base}{href}" if href.startswith("/") else href,
                }
            )
        return submissions

    # ── Calendar ────────────────────────────────────────────────────────

    def get_calendar_events(self, start: str, end: str) -> list[dict]:
        """Fetch calendar events for a date range via the JSON API.

        *start* and *end* are ``YYYY-MM-DD`` strings.
        """
        url = f"{self.base}/student/events.json?start={start}&end={end}"
        cached = self.cache.get(url)
        if cached is not None:
            body, _status = cached
            events = json.loads(body)
        else:
            lock = self._get_url_lock(url)
            with lock:
                cached = self.cache.get(url)
                if cached is not None:
                    body, _status = cached
                    events = json.loads(body)
                else:
                    r = self._request_with_retry(
                        "GET",
                        f"{self.base}/student/events.json",
                        params={"start": start, "end": end},
                    )
                    if "/login" in r.url:
                        raise RuntimeError("Session expired or invalid — redirected to login")
                    self.cache.put(url, r.text, r.status_code)
                    events = r.json()
        return [
            {
                "id": e.get("id"),
                "title": e.get("title"),
                "start": e.get("start"),
                "end": e.get("end"),
                "all_day": e.get("allDay", False),
                "description": BeautifulSoup(
                    e.get("description", ""), "html.parser"
                ).get_text("\n", strip=True)[:500]
                if e.get("description")
                else None,
                "type": e.get("type"),
                "category": e.get("category"),
                "url": f"{self.base}{e['url']}"
                if e.get("url", "").startswith("/")
                else e.get("url"),
                "color": e.get("backgroundColor"),
            }
            for e in events
        ]

    def get_ical_feed(self) -> str:
        """Fetch the raw iCal feed content.

        Scrapes the calendar page to find the webcal token, then fetches the
        iCal file via HTTP.
        """
        soup = self._get("/student/calendar")
        link = soup.find("a", href=re.compile(r"webcal://"))
        if not link:
            raise RuntimeError("Could not find webcal link on calendar page")
        ical_url = link["href"].replace("webcal://", "https://")
        cached = self.cache.get(ical_url)
        if cached is not None:
            return cached[0]
        lock = self._get_url_lock(ical_url)
        with lock:
            cached = self.cache.get(ical_url)
            if cached is not None:
                return cached[0]
            r = self._request_with_retry("GET", ical_url)
            self.cache.put(ical_url, r.text, r.status_code)
            return r.text

    # ── Timetable ───────────────────────────────────────────────────────

    def get_timetable(self, start_date: str | None = None) -> dict:
        """Scrape the weekly timetable.

        *start_date* is a ``YYYY-MM-DD`` string.  Defaults to today.
        Returns ``{"days": [...], "lessons": [...]}``.
        """
        params = ""
        if start_date:
            params = f"?start_date={start_date}"
        soup = self._get(f"/student/timetables/weekly{params}")

        table = soup.find("table", class_="f-timetable")
        if not table:
            raise RuntimeError("Could not find timetable table on page")

        # Parse column headers (day names)
        thead = table.find("thead")
        headers: list[dict] = []
        if thead:
            for th in thead.find_all("th")[1:]:  # skip "Period" column
                text = th.get_text(strip=True)
                headers.append(
                    {
                        "header": text,
                        "is_today": "table-active-th" in (th.get("class") or []),
                    }
                )

        # Parse rows
        lessons: list[dict] = []
        tbody = table.find("tbody") or table
        for row in tbody.find_all("tr"):
            cells = row.find_all(["th", "td"])
            if len(cells) < 2:
                continue
            period_label = cells[0].get_text(strip=True)
            for col_idx, td in enumerate(cells[1:]):
                link = td.find("a", class_=re.compile(r"f-timetable-item"))
                if not link:
                    continue

                body = link.find(class_="f-box-item__body")
                if not body:
                    continue

                # Time
                time_el = body.find("small", class_="color-secondary")
                time_slot = time_el.get_text(strip=True) if time_el else None

                # Subject / class name
                subject_el = body.find("p", class_="fw-semibold")
                subject = subject_el.get_text(strip=True) if subject_el else None

                # Skip homeroom / attendance rows with no subject
                if not subject:
                    continue

                # Year group
                year_els = body.find_all("p", class_="text-truncate")
                year = year_els[0].get_text(strip=True) if year_els else None

                # Teacher
                teacher = None
                teacher_els = [
                    p
                    for p in body.find_all("p")
                    if "text-truncate" in " ".join(p.get("class", []))
                ]
                if len(teacher_els) >= 2:
                    teacher = teacher_els[-1].get_text(strip=True)

                # Room — bare <p> with no classes (the last element)
                room = None
                all_ps = body.find_all("p")
                if all_ps:
                    last_p = all_ps[-1]
                    if not last_p.get("class"):
                        room = last_p.get_text(strip=True) or None

                # Class ID from popover URL
                content_url = link.get("data-bs-content-url", "")
                class_id = None
                m = re.search(r"ib_class_id=(\d+)", content_url)
                if m:
                    class_id = m.group(1)

                day_header = headers[col_idx] if col_idx < len(headers) else {}
                lessons.append(
                    {
                        "period": period_label,
                        "day": day_header.get("header", ""),
                        "is_today": day_header.get("is_today", False),
                        "time": time_slot,
                        "subject": subject,
                        "year": year,
                        "teacher": teacher,
                        "room": room,
                        "class_id": class_id,
                    }
                )

        return {"days": headers, "lessons": lessons}

    # ── Class grades ────────────────────────────────────────────────────

    def get_classes(self) -> dict[str, str]:
        """Fetch dashboard and extract all class IDs and class names."""
        soup = self._get("/student/dashboard")
        seen: dict[str, str] = {}
        for a in soup.find_all("a", href=True):
            href = a["href"]
            match = re.search(r"^/student/classes/(\d+)/?$", href.split("?")[0].rstrip("/"))
            if match:
                class_id = match.group(1)
                name = a.get_text(" ", strip=True)
                if name and not any(kw in name.lower() for kw in ("all classes", "browse", "view")):
                    seen[class_id] = name
        return seen

    def get_class_grades(self, class_id: str, bypass_cache: bool = False) -> dict:
        """Fetch all grades for a class and compute expected grade.

        Returns ``{"tasks": [...], "categories": [...], "grade_scale": {...}, "expected_grade": ...}``.
        """
        soup = self._get(f"/student/classes/{class_id}/core_tasks", bypass_cache=bypass_cache)

        # Grade scale
        chart = soup.find("div", class_="assignments-progress-chart")
        grade_scale: dict = {}
        if chart:
            raw_labels = chart.get("data-grade-labels", "{}")
            try:
                grade_scale = {int(k): v for k, v in json.loads(raw_labels).items()}
            except Exception:
                pass

        # Category weights
        categories: list[dict] = []
        cat_table = soup.find("div", id="categories-table")
        if cat_table:
            for item in cat_table.find_all("div", class_="list-item"):
                cells = item.find_all("div", class_="cell")
                if len(cells) >= 2:
                    cat_name = cells[0].get_text(strip=True)
                    weight_str = cells[1].get_text(strip=True).rstrip("%")
                    if cat_name.lower() in ("category", ""):
                        continue  # skip header row
                    try:
                        weight = float(weight_str) / 100.0
                    except ValueError:
                        weight = 0.0
                    categories.append({"name": cat_name, "weight": weight})

        # Tasks with grades
        tasks: list[dict] = []
        for card in soup.find_all("div", class_="fusion-card-item"):
            title_el = card.find(class_="title")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            link_el = title_el.find("a")
            href = link_el.get("href", "") if link_el else ""
            task_id_match = re.search(r"/core_tasks/(\d+)", href)

            # Grade
            grade_letter = None
            assessment_cell = card.find(class_=re.compile(r"assessment-cell|task-score")) or card
            grade_el = assessment_cell.find(class_=re.compile(r"\bgrade\b"))
            if grade_el:
                grade_letter = grade_el.get_text(strip=True)
            
            if not grade_letter:
                not_assessed_els = assessment_cell.find_all(class_=re.compile(r"not-assessed"))
                for el in not_assessed_els:
                    txt = el.get_text(strip=True)
                    if txt:
                        grade_letter = txt
                        break

            if not grade_letter:
                not_applicable_el = assessment_cell.find(class_=re.compile(r"not-applicable"))
                if not_applicable_el:
                    grade_letter = not_applicable_el.get_text(strip=True)
                else:
                    na_el = assessment_cell.find(lambda tag: tag.name in {"div", "span"} and tag.get_text(strip=True) == "N/A")
                    if na_el:
                        grade_letter = "N/A"

            # Points
            points_el = card.find("div", class_="points")
            points_text = points_el.get_text(strip=True) if points_el else None

            # Submission status
            status_el = card.find(
                "span", class_=re.compile(r"\b(submitted|not-submitted)\b")
            )
            status = status_el.get_text(strip=True) if status_el else None

            # Category and badge labels
            labels: list[str] = []
            labels_set = card.find("div", class_="labels-set")
            if labels_set:
                for lbl in labels_set.find_all("div", class_="label"):
                    t = lbl.get_text(strip=True)
                    if t:
                        labels.append(t)
                for badge in labels_set.find_all("span", class_="badge-label"):
                    t = badge.get_text(strip=True)
                    if t:
                        labels.append(t)

            # Robust status detection from labels if status element is absent or generic
            if not status:
                labels_lower = [l.lower() for l in labels]
                if "submitted" in labels_lower:
                    status = "submitted"
                elif "pending" in labels_lower or "not submitted" in labels_lower:
                    status = "not-submitted"

            # Parse submit button
            dropbox_link = card.find("a", href=re.compile(r"/core_tasks/\d+/dropbox"))
            has_submit_btn = bool(dropbox_link)

            # Parse due date
            due_date = None
            date_badge = card.find(class_="date-badge")
            if date_badge:
                m_el = date_badge.find(class_="month")
                d_el = date_badge.find(class_="day")
                if m_el and d_el:
                    month = m_el.get_text(strip=True)
                    day = d_el.get_text(strip=True)
                    due_date_base = f"{month} {day}"

                    due_el = card.find(class_="due-date")
                    time_str = ""
                    if due_el:
                        due_text = due_el.get_text(" ", strip=True)
                        time_match = re.search(r"(\d{1,2}:\d{2}\s*(?:AM|PM|am|pm))", due_text)
                        if time_match:
                            time_str = time_match.group(1)

                    if time_str:
                        due_date = f"{due_date_base}, {time_str}"
                    else:
                        due_date = due_date_base

            tasks.append(
                {
                    "title": title,
                    "task_id": task_id_match.group(1) if task_id_match else None,
                    "url": f"{self.base}{href}" if href.startswith("/") else href,
                    "due_date": due_date,
                    "grade_letter": grade_letter,
                    "points": points_text,
                    "status": status,
                    "category": labels[0] if labels else None,
                    "labels": labels or None,
                    "has_submit_button": has_submit_btn,
                }
            )

        # Compute expected grade from chart data
        expected = self._compute_expected_grade(chart, grade_scale, categories)

        return {
            "tasks": tasks,
            "categories": categories,
            "grade_scale": grade_scale,
            "expected_grade": expected,
        }

    def _compute_expected_grade(
        self,
        chart,
        grade_scale: dict,
        categories: list[dict],
    ) -> dict | None:
        """Compute weighted expected grade from the Highcharts data-series."""
        if not chart or not grade_scale:
            return None

        raw_series = chart.get("data-series", "[]")
        try:
            series = json.loads(raw_series)
        except Exception:
            return None

        if not series:
            return None

        # The chart uses a 0-11 numeric scale mapped to letter grades
        # We need to figure out which category each task belongs to
        # from the task list — but the chart only has names.
        # Compute a simple unweighted average from the chart data.
        scores: list[float] = []
        for item in series:
            data_points = item.get("data", [])
            if data_points:
                scores.append(float(data_points[0]))

        if not scores:
            return None

        avg_score = sum(scores) / len(scores)
        # Clamp to scale range
        idx = max(0, min(round(avg_score), max(grade_scale.keys())))
        letter = grade_scale.get(idx, str(idx))

        return {
            "average_score": round(avg_score, 2),
            "letter_grade": letter,
            "num_graded": len(scores),
            "note": "Unweighted average from chart data",
        }

    # ── Grade frequency ─────────────────────────────────────────────────

    def count_grade_frequencies(self, class_filter: str | None = None) -> dict:
        """Count frequency of each grade letter across all or one class.

        Returns ``{"grades": {"A": 5, "B": 3, ...}, "total": N, "classes": [...]}``.
        """
        result = self.crawl_all(max_pages=5, fetch_details=False)
        seen: dict[str, str] = {}
        for task in result["upcoming"] + result["past"] + result["overdue"]:
            link = task.get("link", "")
            m = re.search(r"/student/classes/(\d+)/", link)
            cname = task.get("class_name", "")
            if m and cname:
                seen[m.group(1)] = cname

        target_classes: list[tuple[str, str]]
        if class_filter:
            target_classes = [
                (cid, cn)
                for cid, cn in seen.items()
                if class_filter.lower() in cn.lower()
            ]
            if not target_classes:
                return {
                    "error": f"No class matching '{class_filter}'",
                    "available": list(seen.values()),
                }
        else:
            target_classes = list(seen.items())

        freq: dict[str, int] = {}
        classes_used: list[dict] = []
        for cid, cname in target_classes:
            grades = self.get_class_grades(cid)
            classes_used.append({"id": cid, "name": cname})
            for task in grades.get("tasks", []):
                letter = task.get("grade_letter")
                if letter:
                    freq[letter] = freq.get(letter, 0) + 1

        return {
            "grades": dict(sorted(freq.items())),
            "total": sum(freq.values()),
            "classes": classes_used,
        }

    # ── Public crawl methods ────────────────────────────────────────────

    def get_tasks_by_view(self, view: str, max_pages: int = 10) -> list[dict]:
        """Crawl one view (``upcoming`` / ``past`` / ``overdue``)."""
        all_tasks: list[dict] = []
        for page in range(1, max_pages + 1):
            soup = self._get(f"/student/tasks_and_deadlines?view={view}&page={page}")
            tasks = self._parse_tasks_page(soup)
            if not tasks:
                break
            for t in tasks:
                t["view"] = view
            all_tasks.extend(tasks)
            log.info("%s page %d: %d items", view, page, len(tasks))
            if not self._has_next_page(soup, page, view):
                break
        return all_tasks

    def get_task_detail(self, task_path: str, from_hint: bool = False, bypass_cache: bool = False) -> dict | None:
        """Fetch one task's detail page for task body, attachments, and submission info.
        
        If from_hint is True, hits the event popover hint page instead of the full detail page.
        """
        if task_path.startswith("http"):
            task_path = task_path.replace(self.base, "")

        task_match = re.search(r"(/student/classes/\d+/core_tasks/\d+)", task_path)
        if task_match:
            task_path = task_match.group(1)

        if from_hint:
            m = re.search(r"/student/classes/(\d+)/core_tasks/(\d+)", task_path)
            if m:
                class_id, task_id = m.group(1), m.group(2)
                task_path = f"/student/classes/{class_id}/events/{task_id}/hint"

        try:
            soup = self._get(task_path, bypass_cache=bypass_cache)
        except Exception as e:
            return {"error": str(e)}

        detail: dict = {}
        main_content = soup.find("main") or soup

        # Parse card details from the detail page if present
        card = soup.find(class_="fusion-card-item")
        if card:
            # Parse grade_letter
            grade_letter = None
            assessment_cell = card.find(class_=re.compile(r"assessment-cell|task-score")) or card
            grade_el = assessment_cell.find(class_=re.compile(r"\bgrade\b"))
            if grade_el:
                grade_letter = grade_el.get_text(strip=True)
            
            if not grade_letter:
                not_assessed_els = assessment_cell.find_all(class_=re.compile(r"not-assessed"))
                for el in not_assessed_els:
                    txt = el.get_text(strip=True)
                    if txt:
                        grade_letter = txt
                        break

            if not grade_letter:
                not_applicable_el = assessment_cell.find(class_=re.compile(r"not-applicable"))
                if not_applicable_el:
                    grade_letter = not_applicable_el.get_text(strip=True)
                else:
                    na_el = assessment_cell.find(lambda tag: tag.name in {"div", "span"} and tag.get_text(strip=True) == "N/A")
                    if na_el:
                        grade_letter = "N/A"
            detail["grade_letter"] = grade_letter

            # Parse points
            points_el = card.find("div", class_="points")
            if points_el:
                detail["grade_score"] = points_el.get_text(strip=True)

            # Parse submit button
            dropbox_link = soup.find("a", href=re.compile(r"/core_tasks/\d+/dropbox"))
            has_submit_btn = bool(dropbox_link)
            detail["has_submit_button"] = has_submit_btn

            # Parse status
            status_el = card.find("span", class_=re.compile(r"\b(submitted|not-submitted)\b"))
            status = status_el.get_text(strip=True) if status_el else None
            labels = []
            labels_set = card.find("div", class_="labels-set")
            if labels_set:
                for lbl in labels_set.find_all("div", class_="label"):
                    t = lbl.get_text(strip=True)
                    if t and t not in labels:
                        labels.append(t)
                for badge in labels_set.find_all("span", class_="badge-label"):
                    t = badge.get_text(strip=True)
                    if t and t not in labels:
                        labels.append(t)
            if not status:
                labels_lower = [l.lower() for l in labels]
                if "submitted" in labels_lower:
                    status = "submitted"
                elif "pending" in labels_lower or "not submitted" in labels_lower:
                    status = "not-submitted"
            detail["status"] = status
            detail["labels"] = labels

        if not from_hint:
            dropbox = main_content.find(class_=re.compile(r"dropbox|submission|coursework"))
            submission_text = self._text_from_block(dropbox)
            if submission_text:
                detail["submission"] = submission_text

            comments = []
            seen_comment_texts: set[str] = set()
            for discussion in main_content.find_all(
                "div", class_=re.compile(r"\bdiscussion\b", re.IGNORECASE)
            )[:5]:
                body = discussion.find(
                    "div", class_=re.compile(r"fr-view|fix-body-margins", re.IGNORECASE)
                )
                if not body:
                    continue
                text = self._text_from_block(body, limit=2000)
                if text and text not in seen_comment_texts:
                    seen_comment_texts.add(text)
                    comments.append(text)
            if comments:
                detail["comments"] = comments

        # Description parsing
        desc = None
        if from_hint:
            desc = main_content.find(class_="fr-view") or main_content.find(class_="fix-body-margins")
        else:
            desc_heading = main_content.find(
                lambda tag: (
                    tag.name in {"h3", "h4", "h5", "th"}
                    and tag.get_text(" ", strip=True) == "Description"
                )
            )
            if desc_heading:
                desc = desc_heading.find_next(
                    "div",
                    class_=re.compile(r"fr-view|fix-body-margins|show-more", re.IGNORECASE),
                )
            if not desc:
                desc = main_content.find(
                    class_=re.compile(r"description|task-body", re.IGNORECASE)
                )

        description_text = self._text_from_block(desc)
        if description_text:
            detail["description"] = description_text

        attachments = self._extract_attachments(main_content)
        if attachments:
            detail["attachments"] = attachments
        return detail if detail else None

    def find_task_by_id(self, task_id: str, max_pages: int = 50) -> dict | None:
        """Search page-by-page across all views for a specific task ID, stopping as soon as found."""
        for view in ("overdue", "upcoming", "past"):
            for page in range(1, max_pages + 1):
                soup = self._get(f"/student/tasks_and_deadlines?view={view}&page={page}")
                tasks = self._parse_tasks_page(soup)
                if not tasks:
                    break
                for t in tasks:
                    t_id_match = re.search(r"(\d+)$", t.get("link", "").split("?")[0].rstrip("/"))
                    t_id = t_id_match.group(1) if t_id_match else None
                    if t_id == task_id or t.get("id") == task_id:
                        t["view"] = view
                        return t
                if not self._has_next_page(soup, page, view):
                    break
        return None

    def crawl_index(self) -> dict:
        """Lightweight check: upcoming page 1 + notifications hub.

        Returns a minimal dict suitable for daemon diffing.  Only two HTTP
        requests regardless of how many tasks exist.
        """
        upcoming = self.get_tasks_by_view("upcoming", 1)

        notifications: dict = {"unread_count": 0, "items": []}
        try:
            hub_endpoint, token = self.get_notification_token()
            if hub_endpoint:
                from .notifications import MNNHubClient, hub_for_domain

                if not hub_endpoint:
                    hub_endpoint = hub_for_domain(self.domain)
                hub = MNNHubClient(hub_endpoint, token)
                stats = hub.stats()
                result = hub.list(page=1, per_page=10, filter_="unread")
                notifications = {
                    "unread_count": stats.get("unread_count", 0),
                    "items": result.get("items", []),
                }
        except Exception as exc:
            log.warning("notifications fetch failed: %s", exc)

        return {
            "student_name": self.student_name,
            "school": self.school,
            "base_url": self.base,
            "crawled_at": datetime.now().isoformat(),
            "upcoming": upcoming,
            "notifications": notifications,
        }

    def crawl_all(
        self,
        max_pages: int = 10,
        fetch_details: bool = False,
    ) -> dict:
        """Crawl all tasks by compiling from active classes core_tasks pages."""
        log.info("Discovering classes from dashboard...")
        classes = {}
        try:
            classes = self.get_classes()
        except Exception as e:
            log.warning("Failed to discover classes from dashboard: %s", e)

        upcoming = []
        past = []
        overdue = []

        if not classes:
            log.warning("No classes found. Falling back to paginated dashboard crawling...")
            upcoming = self.get_tasks_by_view("upcoming", max_pages)
            past = self.get_tasks_by_view("past", max_pages)
            overdue = self.get_tasks_by_view("overdue", max_pages)
        else:
            log.info("Fetching tasks from %d classes...", len(classes))
            for class_id, class_name in classes.items():
                try:
                    class_data = self.get_class_grades(class_id, bypass_cache=False)
                    for t in class_data.get("tasks", []):
                        task_id = t.get("task_id")
                        if not task_id:
                            continue

                        labels = t.get("labels") or []
                        status = t.get("status")

                        labels_lower = [l.lower() for l in labels]
                        is_submitted = False
                        if "submitted" in labels_lower or status == "submitted":
                            is_submitted = True

                        grade_letter = t.get("grade_letter")
                        grade_score = t.get("points")

                        is_zero_score = False
                        if grade_score:
                            if re.match(r"^\s*0\s*/", grade_score):
                                is_zero_score = True

                        has_score = bool(grade_score and grade_score.strip() and grade_score.strip() != "-")
                        has_letter = bool(grade_letter and grade_letter.strip())
                        has_completed_grade = (has_score or has_letter) and not is_zero_score

                        is_not_assessed = "not assessed yet" in labels_lower or (bool(grade_letter) and "not assessed" in grade_letter.lower())
                        has_submit_btn = bool(t.get("has_submit_button", False))
                        is_unfinished = has_submit_btn and (not is_submitted) and (not has_completed_grade) and (not is_not_assessed)

                        due_date = t.get("due_date")
                        due_dt = parse_due_date(due_date)

                        reconstructed_task = {
                            "id": task_id,
                            "title": t.get("title"),
                            "class_name": class_name,
                            "due_date": due_date,
                            "link": t.get("url"),
                            "grade_letter": grade_letter,
                            "grade_score": t.get("points"),
                            "labels": labels or None,
                            "status": "submitted" if is_submitted else "not-submitted",
                            "has_submit_button": has_submit_btn,
                        }

                        if due_dt and due_dt > datetime.now():
                            reconstructed_task["view"] = "upcoming"
                            upcoming.append(reconstructed_task)
                        elif is_unfinished:
                            reconstructed_task["view"] = "overdue"
                            overdue.append(reconstructed_task)
                        else:
                            reconstructed_task["view"] = "past"
                            past.append(reconstructed_task)
                except Exception as e:
                    log.warning("Failed to crawl tasks for class %s: %s", class_name, e)

        # Retrieve notifications
        notifications: dict = {"unread_count": 0, "items": []}
        try:
            hub_endpoint, token = self.get_notification_token()
            if hub_endpoint:
                from .notifications import MNNHubClient, hub_for_domain

                if not hub_endpoint:
                    hub_endpoint = hub_for_domain(self.domain)
                hub = MNNHubClient(hub_endpoint, token)
                stats = hub.stats()
                result = hub.list(page=1, per_page=10, filter_="unread")
                notifications = {
                    "unread_count": stats.get("unread_count", 0),
                    "items": result.get("items", []),
                }
        except Exception as exc:
            log.warning("notifications fetch failed: %s", exc)

        if fetch_details:
            items = [t for t in upcoming + past + overdue if t.get("link")]
            log.info("Fetching details for %d tasks via /hint...", len(items))
            for i, task in enumerate(items):
                detail = self.get_task_detail(task["link"], from_hint=True)
                if detail:
                    task["detail"] = detail
                if (i + 1) % 5 == 0:
                    log.info("  detail %d/%d", i + 1, len(items))

        return {
            "student_name": self.student_name,
            "school": self.school,
            "base_url": self.base,
            "crawled_at": datetime.now().isoformat(),
            "upcoming": upcoming,
            "past": past,
            "overdue": overdue,
            "notifications": notifications,
            "summary": {
                "upcoming_count": len(upcoming),
                "past_count": len(past),
                "overdue_count": len(overdue),
            },
        }
