"""
Shared helpers for script `days = N` lookback variables.

Detection matches assignment lines such as:
  days = 1
  days=0
  days = 0
  days= 0
  Days = 5   (case-insensitive)
  indented / trailing comments allowed

`None` means the script has no days variable.
`0` is a valid value (variable present).
"""
from __future__ import annotations

import re
from typing import Any, Optional

# Line like: optional indent, "days", "=", integer, optional rest (comment/space)
_DAYS_LINE_RE = re.compile(
    r"^(\s*days\s*=\s*)(\d+)(.*)$",
    re.MULTILINE | re.IGNORECASE,
)


def extract_days_from_source(content: str) -> Optional[int]:
    """Return the first `days = N` integer in source, or None if absent."""
    if not content:
        return None
    match = _DAYS_LINE_RE.search(content)
    if not match:
        return None
    try:
        return int(match.group(2))
    except (TypeError, ValueError):
        return None


def rewrite_days_in_source(content: str, days: int) -> tuple[str, int]:
    """
    Replace every `days = N` assignment with the given integer.
    Returns (new_content, number_of_replacements).
    """
    if content is None:
        return "", 0
    days_i = int(days)

    def _repl(match: re.Match) -> str:
        return f"{match.group(1)}{days_i}{match.group(3)}"

    new_content, count = _DAYS_LINE_RE.subn(_repl, content)
    return new_content, int(count or 0)


def has_days_variable(days_value: Any) -> bool:
    """True when scripts.days / script_days is set (including 0)."""
    return days_value is not None and days_value != ""


def parse_days_input(raw: Any) -> tuple[Optional[int], Optional[str]]:
    """
    Parse UI/API days input.
    Returns (value, error). Empty/None → (None, None) meaning "clear / not provided".
    """
    if raw is None or raw == "":
        return None, None
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return None, "days must be a non-negative integer"
    if val < 0:
        return None, "days must be a non-negative integer"
    return val, None


def effective_days(schedule_days: Any, script_days: Any) -> Optional[int]:
    """Schedule override if set, else script default (may be 0)."""
    if schedule_days is not None and schedule_days != "":
        try:
            return int(schedule_days)
        except (TypeError, ValueError):
            pass
    if script_days is not None and script_days != "":
        try:
            return int(script_days)
        except (TypeError, ValueError):
            return None
    return None


def enrich_schedule_days_fields(sch: dict[str, Any]) -> dict[str, Any]:
    """Attach has_days_variable + effective_days on a schedule/folder-item row."""
    if not sch:
        return sch
    script_days = sch.get("script_days")
    if "has_days_variable" not in sch or sch.get("has_days_variable") is None:
        sch["has_days_variable"] = 1 if has_days_variable(script_days) else 0
    else:
        try:
            sch["has_days_variable"] = 1 if int(sch.get("has_days_variable") or 0) else 0
        except (TypeError, ValueError):
            sch["has_days_variable"] = 1 if has_days_variable(script_days) else 0
    sch["effective_days"] = effective_days(sch.get("days"), script_days)
    return sch
