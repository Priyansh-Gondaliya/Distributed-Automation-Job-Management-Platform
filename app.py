"""Backward-compatible entrypoint — prefer `python run.py`."""
from run import app, _run_controller

if __name__ == "__main__":
    _run_controller()
