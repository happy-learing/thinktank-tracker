from __future__ import annotations

import json
import logging
import msvcrt
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

import httpx
import uvicorn

from thinktank_tracker.logging_setup import configure_logging
from thinktank_tracker.paths import PATHS


logger = logging.getLogger(__name__)


def _choose_port(preferred: int | None = None) -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", preferred or 0))
        return int(sock.getsockname()[1])


def _open_when_ready(url: str) -> None:
    for _ in range(60):
        try:
            if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                webbrowser.open(url)
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.25)


def _read_existing_url(lock_path: Path) -> str | None:
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        port = int(payload["port"])
        return f"http://127.0.0.1:{port}"
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None


def main() -> None:
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    PATHS.ensure()
    bundled_browsers = PATHS.root / "runtime" / "browsers"
    if bundled_browsers.exists():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(bundled_browsers))
    configure_logging()
    logger.info("launcher initialized root=%s", PATHS.root)
    lock_path = PATHS.workspace / "tracker.lock"
    lock_file = lock_path.open("a+b")
    lock_file.seek(0)
    try:
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        existing = _read_existing_url(lock_path)
        if existing:
            webbrowser.open(existing)
        return
    requested_port = os.getenv("THINKTANK_PORT", "").strip()
    try:
        port = _choose_port(int(requested_port) if requested_port else None)
    except (ValueError, OSError):
        port = _choose_port()
    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(json.dumps({"port": port}).encode("utf-8"))
    lock_file.flush()
    url = f"http://127.0.0.1:{port}"
    logger.info("starting local server url=%s", url)
    if os.getenv("THINKTANK_NO_BROWSER") != "1":
        threading.Thread(target=_open_when_ready, args=(url,), daemon=True).start()
    try:
        uvicorn.run(
            "thinktank_tracker.web:app",
            host="127.0.0.1",
            port=port,
            log_level="info",
            log_config=None,
            access_log=False,
        )
    except BaseException:
        logger.exception("local server terminated unexpectedly")
        raise
    finally:
        try:
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            lock_file.close()


if __name__ == "__main__":
    main()
