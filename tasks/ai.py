"""Natural-language helpers behind a swappable interface.

Contract (keep it stable so the implementation can be swapped for an LLM):

    parse_task_text(text: str, today: datetime.date) -> dict
        {"title": str, "description": str, "due_date": "YYYY-MM-DD" | None,
         "recurrence": "daily" | "weekly" | "monthly" | "yearly" | None,
         "priority": "low" | "medium" | "high"}

    break_down_task(task: Task) -> list[dict]
        Each dict has the same keys as a TaskSerializer payload.

Callers must treat the output as untrusted and validate it with TaskSerializer.
"""
import calendar
import json
import re
import urllib.request
from datetime import date, timedelta

from django.conf import settings


class ParserUnavailable(Exception):
    """The configured parser cannot run (e.g. LLM_API_KEY is missing)."""


WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = {name.lower(): index for index, name in enumerate(calendar.month_name) if name}
MONTHS.update({name.lower(): index for index, name in enumerate(calendar.month_abbr) if name})

_WEEKDAY_RE = "|".join(WEEKDAYS)
_MONTH_RE = "|".join(sorted(MONTHS, key=len, reverse=True))
_ORDINAL_RE = r"(\d{1,2})(?:st|nd|rd|th)"

_LEADING_PHRASES = re.compile(
    r"^(?:please\s+)?(?:remind me to|remind me|don'?t forget to|i need to|i have to|i must|todo:?|task:?)\s+",
    re.IGNORECASE,
)

_RECURRENCE_PATTERNS = [
    (re.compile(r"\b(?:of\s+)?(?:every|each)\s+day\b|\bdaily\b", re.I), "daily"),
    (re.compile(r"\b(?:of\s+)?(?:every|each)\s+week\b|\bweekly\b", re.I), "weekly"),
    (re.compile(r"\b(?:of\s+)?(?:every|each)\s+month\b|\bmonthly\b", re.I), "monthly"),
    (re.compile(r"\b(?:of\s+)?(?:every|each)\s+year\b|\byearly\b|\bannually\b", re.I), "yearly"),
]
_EVERY_WEEKDAY_RE = re.compile(rf"\b(?:on\s+)?(?:every|each)\s+({_WEEKDAY_RE})s?\b", re.I)

_PRIORITY_PATTERNS = [
    (re.compile(r"\b(?:high[-\s]priority|urgent(?:ly)?|asap|important)\b", re.I), "high"),
    (re.compile(r"\blow[-\s]priority\b", re.I), "low"),
]

_TRAILING_JUNK_RE = re.compile(r"(?:\s+\b(?:on|by|at|the|of|for|in|due|before)\b)+\s*$", re.I)


def _next_weekday(today, weekday_index, force_next_week=False):
    days_ahead = (weekday_index - today.weekday()) % 7
    if days_ahead == 0 or force_next_week:
        days_ahead = days_ahead or 7
    return today + timedelta(days=days_ahead)


def _clamped(year, month, day):
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def _next_day_of_month(today, day):
    if day >= today.day and day <= calendar.monthrange(today.year, today.month)[1]:
        return date(today.year, today.month, day)
    year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return _clamped(year, month, day)


def _next_month_day(today, month, day):
    if not 1 <= day <= 31:
        return None
    candidate = _clamped(today.year, month, day)
    return candidate if candidate >= today else _clamped(today.year + 1, month, day)


def _extract_due_date(text, today):
    """Return (due_date | None, text_with_date_phrase_removed)."""
    rules = [
        (r"\b(?:on\s+|by\s+)?(\d{4})-(\d{2})-(\d{2})\b",
         lambda m: date(int(m[1]), int(m[2]), int(m[3]))),
        (r"\b(?:on\s+|by\s+)?the\s+day\s+after\s+tomorrow\b|\b(?:on\s+|by\s+)?day\s+after\s+tomorrow\b",
         lambda m: today + timedelta(days=2)),
        (r"\b(?:by\s+)?tomorrow\b", lambda m: today + timedelta(days=1)),
        (r"\b(?:by\s+)?(?:today|tonight)\b", lambda m: today),
        (r"\bin\s+(\d{1,3})\s+days?\b", lambda m: today + timedelta(days=int(m[1]))),
        (r"\bin\s+(\d{1,2})\s+weeks?\b", lambda m: today + timedelta(weeks=int(m[1]))),
        (r"\b(?:by\s+)?next\s+week\b", lambda m: today + timedelta(days=7)),
        (rf"\b(?:on\s+|by\s+)?next\s+({_WEEKDAY_RE})\b",
         lambda m: _next_weekday(today, WEEKDAYS.index(m[1].lower()), force_next_week=True)),
        (rf"\b(?:on\s+|by\s+|this\s+)?({_WEEKDAY_RE})\b",
         lambda m: _next_weekday(today, WEEKDAYS.index(m[1].lower()))),
        (rf"\b(?:on\s+|by\s+)?(?:the\s+)?(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH_RE})\b",
         lambda m: _next_month_day(today, MONTHS[m[2].lower()], int(m[1]))),
        (rf"\b(?:on\s+|by\s+)?({_MONTH_RE})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b",
         lambda m: _next_month_day(today, MONTHS[m[1].lower()], int(m[2]))),
        (rf"\b(?:on\s+|by\s+)?(?:the\s+)?{_ORDINAL_RE}(?:\s+of\s+the\s+month)?\b",
         lambda m: _next_day_of_month(today, int(m[1])) if 1 <= int(m[1]) <= 31 else None),
    ]
    for pattern, resolve in rules:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        try:
            due = resolve(match)
        except ValueError:
            due = None
        if due is not None:
            return due, text[:match.start()] + " " + text[match.end():]
    return None, text


def _clean_title(text):
    title = re.sub(r"\s+", " ", text).strip(" ,.;:!-")
    title = _TRAILING_JUNK_RE.sub("", title).strip(" ,.;:!-")
    return title[:1].upper() + title[1:]


def _parse_with_rules(text, today):
    original = text.strip()
    working = _LEADING_PHRASES.sub("", original)
    recurrence, due_date, priority = None, None, "medium"

    for pattern, level in _PRIORITY_PATTERNS:
        if pattern.search(working):
            priority = level
            working = pattern.sub(" ", working)
            break

    weekday_match = _EVERY_WEEKDAY_RE.search(working)
    if weekday_match:
        recurrence = "weekly"
        due_date = _next_weekday(today, WEEKDAYS.index(weekday_match[1].lower()))
        working = working[:weekday_match.start()] + " " + working[weekday_match.end():]
    else:
        for pattern, value in _RECURRENCE_PATTERNS:
            if pattern.search(working):
                recurrence = value
                working = pattern.sub(" ", working, count=1)
                break

    if due_date is None:
        due_date, working = _extract_due_date(working, today)

    return {
        "title": _clean_title(working),
        "description": original,
        "due_date": due_date.isoformat() if due_date else None,
        "recurrence": recurrence,
        "priority": priority,
    }


_LLM_SYSTEM_PROMPT = (
    "You convert a to-do sentence into JSON with exactly these keys: "
    '"title" (short imperative string), "description" (string), '
    '"due_date" ("YYYY-MM-DD" or null), '
    '"recurrence" (one of "daily","weekly","monthly","yearly" or null), '
    '"priority" (one of "low","medium","high"). Reply with JSON only.'
)


def _parse_with_llm(text, today):
    if not settings.LLM_API_KEY:
        raise ParserUnavailable("LLM_API_KEY is not configured")
    payload = {
        "model": settings.LLM_MODEL,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": _LLM_SYSTEM_PROMPT},
            {"role": "user", "content": f"Today is {today.isoformat()}. Task: {text}"},
        ],
    }
    request = urllib.request.Request(
        settings.LLM_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {settings.LLM_API_KEY}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=settings.LLM_TIMEOUT_SECONDS) as response:
        body = json.loads(response.read().decode("utf-8"))
    return json.loads(body["choices"][0]["message"]["content"])


def parse_task_text(text, today):
    """Turn free text into a task dict. Swap the implementation via settings.TASK_PARSER."""
    if settings.TASK_PARSER == "llm":
        return _parse_with_llm(text, today)
    return _parse_with_rules(text, today)


_SPLIT_RE = re.compile(r"\s*(?:,|;|\band then\b|\bthen\b|\band\b)\s*", re.IGNORECASE)


def break_down_task(task):
    """Split "Buy milk, eggs and bread" into parts; fall back to a plan/do/review template."""
    parts = [part.strip(" .") for part in _SPLIT_RE.split(task.title) if part and part.strip(" .")]
    if len(parts) < 2:
        parts = [f"Plan: {task.title}", f"Do: {task.title}", f"Review: {task.title}"]
    return [
        {
            "title": (part[:1].upper() + part[1:])[:200],
            "description": f"Subtask of: {task.title}",
            "due_date": task.due_date.isoformat() if task.due_date else None,
            "priority": task.priority,
            "completed": False,
        }
        for part in parts
    ]
