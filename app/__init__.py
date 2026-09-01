"""
AutoControl — Flask application package.

Scripts are NEVER executed on this machine. Workers run scripts locally.
"""
from __future__ import annotations

from flask import Flask


def create_app() -> Flask:
    from flask import request
    from agentation import AgentationConfig, inject_agentation

    from app import config, database
    from app.blueprints.api import api_bp
    from app.blueprints.web import web_bp

    app = Flask(__name__)
    app.config["SECRET_KEY"] = config.SECRET_KEY

    database.init_schema()
    database.migrate_old_scraper_reports()
    database.migrate_reports_page_backfill()

    app.register_blueprint(api_bp)
    app.register_blueprint(web_bp)

    agentation_config = AgentationConfig(
        position="bottom-right",
        theme="auto",
        default_detail="standard",
    )

    # Human‑readable datetime filter
    @app.template_filter("human_dt")
    def human_dt(value):
        """Convert a datetime string to a friendly format.
        Expected format: ISO‑like string stored in DB (in UTC).
        Returns "Never Run" if falsy, otherwise "Today HH:MM AM/PM",
        "Tomorrow HH:MM AM/PM" or a full date for other days.
        Converts UTC to IST (+5:30).
        """
        if not value:
            return "Never Run"
        from datetime import datetime, timedelta, timezone
        try:
            if isinstance(value, str):
                dt = datetime.strptime(value.replace("T", " ").split(".")[0], "%Y-%m-%d %H:%M:%S")
            else:
                dt = value
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt = dt.astimezone(timezone(timedelta(hours=5, minutes=30)))
        except Exception:
            return value

        now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
        today = now.date()
        tomorrow = today + timedelta(days=1)
        if dt.date() == today:
            prefix = "Today"
        elif dt.date() == tomorrow:
            prefix = "Tomorrow"
        else:
            return dt.strftime("%b %d, %Y %I:%M %p")
        return f"{prefix} {dt.strftime('%I:%M %p')}"

    import base64
    import re
    from markupsafe import Markup

    @app.template_filter("format_details")
    def format_details(s):
        if not s:
            return ""
        from html import escape

        path_re = re.compile(
            r"((?:[a-zA-Z]:\\|/)[\w\\/.-]+|\b[\w.-]+\.(?:py|bat|exe|json|log|txt|csv|sql)\b)"
        )
        ip_re = re.compile(r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b")
        id_re = re.compile(r"#[0-9]+")
        target_re = re.compile(r"^Target:\s*(.+?)\s*\(#(\d+)\)\s*$", re.I)
        section_re = re.compile(
            r"^(?:([+\-−~])\s*)?"
            r"("
            r"Workers(?: added| removed| changed)?|"
            r"Scripts(?: added| removed| changed)?|"
            r"Schedules(?: added| removed| changed)?|"
            r"Folders(?: added| removed| changed)?|"
            r"Scheduler view(?: added| removed)?|"
            r"Can set days|"
            r"No permission changes"
            r")"
            r"(?:\s*\((\d+)\))?\s*:?\s*(.*)$",
            re.I,
        )

        def _highlight(escaped: str) -> str:
            out = path_re.sub(r'<span class="detail-highlight">\1</span>', escaped)
            out = ip_re.sub(r'<span class="detail-highlight">\g<0></span>', out)
            out = id_re.sub(r'<span class="detail-highlight">\g<0></span>', out)
            return out

        def _split_items(rest: str) -> list[str]:
            rest = (rest or "").strip()
            if not rest or rest.lower() == "none":
                return []
            # Items are joined with "; " — keep "… +N more" as its own chip.
            return [p.strip() for p in rest.split(";") if p.strip()]

        def _tone_for(label: str) -> str:
            l = (label or "").lower()
            if "added" in l or l.startswith("+"):
                return "is-add"
            if "removed" in l or l.startswith("-") or l.startswith("−"):
                return "is-rem"
            if "changed" in l or "can set days" in l or l.startswith("~"):
                return "is-chg"
            return ""

        lines = [ln.strip() for ln in str(s).split("\n") if ln.strip()]
        if lines and target_re.match(lines[0]):
            tm = target_re.match(lines[0])
            target_name = escape(tm.group(1))
            target_id = escape(tm.group(2))
            sections_html = []
            for raw in lines[1:]:
                sm = section_re.match(raw)
                if not sm:
                    sections_html.append(
                        f'<div class="detail-perm-row"><div class="detail-perm-vals">'
                        f'<span class="detail-chip">{_highlight(escape(raw))}</span>'
                        f"</div></div>"
                    )
                    continue
                prefix = (sm.group(1) or "").strip()
                key_raw = sm.group(2) or ""
                count = sm.group(3)
                rest = (sm.group(4) or "").strip()
                key_l = key_raw.lower()
                if key_l == "no permission changes":
                    sections_html.append(
                        '<div class="detail-perm-row">'
                        '<div class="detail-perm-vals"><span class="detail-empty">No permission changes</span></div>'
                        "</div>"
                    )
                    continue
                # Normalize "+ Workers" style into "Workers added" for display.
                display_key = key_raw
                if prefix and key_l in ("workers", "scripts", "schedules", "folders", "scheduler view"):
                    verb = {"+": "added", "-": "removed", "−": "removed", "~": "changed"}.get(prefix, "")
                    if verb:
                        display_key = f"{key_raw} {verb}"
                items = _split_items(rest)
                # Hide empty / zero / none sections (keeps older verbose logs tidy too).
                if key_l != "can set days":
                    if (count is not None and str(count) == "0") or not items:
                        continue
                tone = _tone_for(display_key if not prefix else f"{prefix} {key_raw}")
                if key_l == "can set days":
                    tone = "is-chg"
                count_html = (
                    f'<span class="detail-count">{escape(count)}</span>'
                    if count is not None
                    else (
                        f'<span class="detail-count">{len(items)}</span>'
                        if items and key_l != "can set days"
                        else ""
                    )
                )
                if key_l == "can set days":
                    if not rest:
                        continue
                    vals = f'<span class="detail-pill is-yes">{escape(rest)}</span>'
                else:
                    chips = []
                    for it in items:
                        more = it.startswith("…") or it.startswith("...")
                        cls = "detail-chip is-more" if more else "detail-chip"
                        chips.append(f'<span class="{cls}">{escape(it)}</span>')
                    vals = "".join(chips)
                row_cls = f"detail-perm-row {tone}".strip()
                key_cls = f"detail-perm-key {tone}".strip()
                sections_html.append(
                    f'<div class="{row_cls}">'
                    f'<div class="{key_cls}">{escape(display_key)}{count_html}</div>'
                    f'<div class="detail-perm-vals">{vals}</div>'
                    f"</div>"
                )
            html = (
                '<div class="detail-stack is-perm">'
                f'<div class="detail-perm-head">'
                f'<span class="detail-perm-label">Granted to</span>'
                f'<span class="detail-perm-target">{target_name}</span>'
                f'<span class="detail-perm-id">#{target_id}</span>'
                f"</div>"
                f'<div class="detail-perm-sections">{"".join(sections_html)}</div>'
                "</div>"
            )
            return Markup(html)

        rendered = []
        for raw in lines:
            rendered.append(_highlight(escape(raw)))
        if len(rendered) > 1:
            html = '<div class="detail-stack">' + "".join(
                f'<div class="detail-line">{ln}</div>' for ln in rendered
            ) + "</div>"
        else:
            html = rendered[0] if rendered else _highlight(escape(str(s)))
        return Markup(html)

    @app.template_filter("b64encode")
    def b64encode_filter(s):
        if not s:
            return ""
        import json
        if isinstance(s, (dict, list)):
            s = json.dumps(s, ensure_ascii=False)
        if isinstance(s, str):
            s = s.encode("utf-8")
        return base64.b64encode(s).decode("utf-8")

    @app.template_filter("report_error_json")
    def report_error_json(s):
        """Safe JSON for <script type=application/json> error payloads."""
        import json
        if s is None or s == "":
            return Markup("null")
        if isinstance(s, (dict, list)):
            obj = s
        elif isinstance(s, str):
            try:
                obj = json.loads(s)
            except Exception:
                obj = {"raw": s}
        else:
            obj = {"raw": str(s)}
        dumped = json.dumps(obj, ensure_ascii=False)
        dumped = dumped.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
        return Markup(dumped)

    @app.after_request
    def add_header(response):
        if "Cache-Control" not in response.headers:
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"

        try:
            if response.content_type and response.content_type.startswith("text/html"):
                html = response.get_data(as_text=True)
                html = inject_agentation(
                    html,
                    agentation_config,
                    route=request.path,
                )
                response.set_data(html)
        except Exception as e:
            print(f"Agentation Error: {e}")

        return response

    @app.template_filter("human_duration")
    def human_duration(seconds):
        if seconds is None:
            return "—"
        try:
            sec = float(seconds)
        except (TypeError, ValueError):
            return seconds
        if sec < 0:
            sec = 0
        if sec < 60:
            if abs(sec - round(sec)) < 0.05:
                return f"{int(round(sec))}s"
            return f"{sec:.1f}s"
        if sec < 3600:
            mins = int(sec // 60)
            rem = int(round(sec % 60))
            if rem == 0:
                return f"{mins} min" if mins != 1 else "1 min"
            return f"{mins} min {rem}s"
        hours = int(sec // 3600)
        mins = int((sec % 3600) // 60)
        if mins == 0:
            return f"{hours} hr" if hours != 1 else "1 hr"
        return f"{hours} hr {mins} min"

    return app
