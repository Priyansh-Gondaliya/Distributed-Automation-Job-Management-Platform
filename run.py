"""
AutoControl controller entrypoint.

Usage:
  python run.py
"""
import logging

from app import create_app, config
from app.services.scheduler import start_scheduler

app = create_app()
start_scheduler(app)


class _HideLocalhostBanner(logging.Filter):
    """Werkzeug always advertises 127.0.0.1 when host is 0.0.0.0 — drop that line."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if "127.0.0.1" in msg and "Running on" in msg:
            return False
        return True


def _run_controller() -> None:
    logging.getLogger("werkzeug").addFilter(_HideLocalhostBanner())
    print(f"Controller starting — bind {config.HOST}:{config.PORT}")
    print(f"Dashboard: {config.PUBLIC_URL}/")
    print(
        f"Database: postgres://{config.PGUSER}@{config.PGHOST}:{config.PGPORT}/"
        f"{config.PGDATABASE} (schema={config.PGSCHEMA}, tables=tbl_dfms_*)"
    )
    app.run(
        host=config.HOST,
        port=config.PORT,
        debug=True,
        use_reloader=False,
        threaded=True,
    )


if __name__ == "__main__":
    _run_controller()
