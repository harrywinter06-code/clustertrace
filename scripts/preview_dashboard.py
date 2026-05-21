"""Launcher that points the dashboard at the demo DB and runs on a fixed port."""
import os
from pathlib import Path

os.environ["CLUSTERTRACE_DB"] = str(Path(__file__).parent.parent / "demo-traces" / "traces.db")
import uvicorn

uvicorn.run("clustertrace.dashboard.app:app", host="127.0.0.1", port=7790, log_level="warning")
