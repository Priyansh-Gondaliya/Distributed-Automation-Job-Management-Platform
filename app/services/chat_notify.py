"""Send Schedule Tracking action notes to the internal Chat API.

Token stays on the server (CHAT_BOT_TOKEN). Posts to the schedules channel
and optionally a DM for the assigned DFMS username.
"""
from __future__ import annotations

from typing import Any, Optional

import requests

from app import config

CHAT_ACTIONS = (
    "bug",
    "failed",
    "recheck",
    "maintenance",
    "testing",
    "completed",
    "pending",
)

ACTION_TO_TRACKING = {
    "bug": "failed",
    "failed": "failed",
    "recheck": "pending",
    "maintenance": "in_progress",
    "testing": "in_progress",
    "completed": "completed",
    "pending": "pending",
}


def normalize_action(value: Optional[str]) -> Optional[str]:
    raw = (value or "").strip().lower().replace(" ", "_")
    if raw == "maintanance":
        raw = "maintenance"
    if raw == "test":
        raw = "testing"
    if raw not in CHAT_ACTIONS:
        return None
    return raw


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.CHAT_BOT_TOKEN}",
        "Content-Type": "application/json",
    }


def _post_message(payload: dict[str, Any]) -> dict[str, Any]:
    url = f"{config.CHAT_API_BASE}/chat.postMessage"
    try:
        resp = requests.post(url, headers=_headers(), json=payload, timeout=10)
    except requests.RequestException as exc:
        return {"ok": False, "error": str(exc), "status_code": 0}
    body: dict[str, Any] = {}
    try:
        body = resp.json() if resp.content else {}
    except ValueError:
        body = {"raw": (resp.text or "")[:500]}
    ok = bool(body.get("ok")) if isinstance(body, dict) and "ok" in body else resp.ok
    err = ""
    if isinstance(body, dict):
        err = str(body.get("error") or body.get("detail") or "")
    if not ok and not err:
        err = f"HTTP {resp.status_code}"
    return {
        "ok": ok,
        "status_code": resp.status_code,
        "error": err,
        "body": body if isinstance(body, dict) else {},
    }


def build_message(
    *,
    action: str,
    note: str,
    script_name: str,
    schedule_id: int,
    worker_name: str,
    assigned_user: str,
    sender: str,
) -> str:
    label = action.replace("_", " ").title()
    lines = [
        f"[{label}] Schedule #{schedule_id}",
        f"Script: {script_name or '—'}",
        f"Worker: {worker_name or '—'}",
        f"Assigned user: @{assigned_user}" if assigned_user else "Assigned user: —",
        f"From: {sender or 'admin'}",
    ]
    text = (note or "").strip()
    if text:
        lines.append("")
        lines.append(text)
    return "\n".join(lines)


def build_folder_message(
    *,
    action: str,
    note: str,
    folder_name: str,
    folder_id: int,
    script_count: int,
    worker_name: str,
    assigned_user: str,
    sender: str,
) -> str:
    label = action.replace("_", " ").title()
    lines = [
        f"[{label}] Folder #{folder_id}",
        f"Folder: {folder_name or '—'}",
        f"Scripts: {script_count}",
        f"Workers: {worker_name or '—'}",
        f"Assigned user: @{assigned_user}" if assigned_user else "Assigned user: —",
        f"From: {sender or 'admin'}",
    ]
    text = (note or "").strip()
    if text:
        lines.append("")
        lines.append(text)
    return "\n".join(lines)


def build_desktop_notify_body(
    *,
    action: str,
    note: str,
    subject: str,
    assigned_user: str = "",
    sender: str = "",
) -> str:
    """Compact toast body for the worker PC system notification."""
    label = (action or "").replace("_", " ").title() or "Update"
    lines = [f"[{label}] {subject or 'Schedule'}"]
    if assigned_user:
        lines.append(f"Assigned: @{assigned_user.lstrip('@')}")
    if sender:
        lines.append(f"From: {sender}")
    text = (note or "").strip()
    if text:
        lines.append("")
        lines.append(text)
    # Toast text fields are short; keep readable without truncating the note harshly.
    body = "\n".join(lines)
    if len(body) > 900:
        body = body[:897] + "..."
    return body


def send_schedule_chat(
    *,
    text: str,
    assigned_user: str = "",
) -> dict[str, Any]:
    if not (config.CHAT_BOT_TOKEN or "").strip():
        return {"ok": False, "error": "CHAT_BOT_TOKEN is not configured"}
    channel = (config.CHAT_SCHEDULES_CHANNEL or "73").strip()
    channel_result = _post_message({"channel": channel, "text": text})
    dm_result = None
    user = (assigned_user or "").strip().lstrip("@")
    if config.CHAT_ALSO_DM_USER and user:
        dm_result = _post_message({"user": user, "text": text})
    ok = bool(channel_result.get("ok")) or bool(dm_result and dm_result.get("ok"))
    errors = []
    if not channel_result.get("ok"):
        errors.append(f"channel {channel}: {channel_result.get('error') or 'failed'}")
    if dm_result is not None and not dm_result.get("ok"):
        errors.append(f"user {user}: {dm_result.get('error') or 'failed'}")
    return {
        "ok": ok,
        "channel": channel,
        "channel_ok": bool(channel_result.get("ok")),
        "dm_ok": None if dm_result is None else bool(dm_result.get("ok")),
        "error": "; ".join(errors),
    }
