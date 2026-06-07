from __future__ import annotations

import os
import threading
import time
from pathlib import Path


_LOCK = threading.Lock()


def default_log_path() -> Path:
    env_path = os.environ.get("PAPER_OA_RUN_LOG")
    if env_path:
        return Path(env_path)
    return Path.cwd() / "run.log"


def reset(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    path.write_text("", encoding="utf-8")
    os.environ["PAPER_OA_RUN_LOG"] = str(path)


def write(message: str, component: str = "app") -> None:
    path = default_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"{timestamp} [{component}] {message}\n"
    with _LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
