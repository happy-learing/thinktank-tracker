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


def _pdf_parser_self_test() -> bool:
    try:
        from io import BytesIO

        from pypdf import PdfWriter

        from thinktank_tracker.extractors import extract_pdf_document

        buffer = BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        writer.write(buffer)
        document = extract_pdf_document(
            buffer.getvalue(),
            "https://local.invalid/portable-self-test.pdf",
            pdf_parser_mode="anydoc_first",
        )
        attempts = document.extraction_audit.get("attempts", [])
        engines = [
            item.get("engine")
            for item in attempts
            if isinstance(item, dict)
        ]
        return (
            document.extraction_status == "scanned_pdf"
            and document.extraction_audit.get("fallback_reason") == "anydoc_needs_ocr"
            and engines == ["anydoc", "pypdf"]
        )
    except Exception:
        return False


def _choose_port(preferred: int | None = None) -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", preferred or 0))
        return int(sock.getsockname()[1])


def _service_ready(url: str) -> bool:
    try:
        response = httpx.get(f"{url}/health", timeout=1)
        payload = response.json()
        return (
            response.status_code == 200
            and payload.get("status") == "ok"
            and isinstance(payload.get("database_version"), int)
        )
    except (httpx.HTTPError, ValueError):
        return False


def _open_browser(url: str) -> bool:
    target = f"{url.rstrip('/')}/#launcher-{time.time_ns()}"
    startfile = getattr(os, "startfile", None)
    if startfile is not None:
        try:
            startfile(target)
            logger.info("browser launch requested through Windows shell url=%s", target)
            return True
        except OSError:
            logger.exception("Windows shell could not open browser url=%s", target)
    try:
        opened = bool(webbrowser.open(target, new=2, autoraise=True))
        if opened:
            logger.info("browser launch requested through fallback url=%s", target)
        return opened
    except webbrowser.Error:
        logger.exception("fallback browser launch failed url=%s", target)
        return False


def _open_when_ready(url: str) -> None:
    for _ in range(60):
        if _service_ready(url):
            _open_browser(url)
            return
        time.sleep(0.25)
    logger.error("local service did not become ready for browser launch url=%s", url)


def _read_existing_url(lock_path: Path) -> str | None:
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        port = int(payload["port"])
        if not 1 <= port <= 65535:
            return None
        return f"http://127.0.0.1:{port}"
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None


def _write_endpoint(endpoint_path: Path, port: int) -> None:
    temporary = endpoint_path.with_name(f"{endpoint_path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps({"port": port, "pid": os.getpid()}),
            encoding="utf-8",
        )
        temporary.replace(endpoint_path)
    finally:
        temporary.unlink(missing_ok=True)


def _open_existing_instance(endpoint_path: Path, legacy_lock_path: Path) -> bool:
    for _ in range(40):
        for path in (endpoint_path, legacy_lock_path):
            existing = _read_existing_url(path)
            if existing and _service_ready(existing):
                return _open_browser(existing)
        time.sleep(0.125)
    return False


def main() -> None:
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    if os.getenv("THINKTANK_PDF_SELF_TEST") == "1":
        raise SystemExit(0 if _pdf_parser_self_test() else 1)
    PATHS.ensure()
    bundled_browsers = PATHS.root / "runtime" / "browsers"
    if bundled_browsers.exists():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(bundled_browsers))
    configure_logging()
    logger.info("launcher initialized root=%s", PATHS.root)
    lock_path = PATHS.workspace / "tracker.lock"
    endpoint_path = PATHS.workspace / "tracker.endpoint.json"
    lock_file = lock_path.open("a+b")
    lock_file.seek(0)
    try:
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        lock_file.close()
        if os.getenv("THINKTANK_NO_BROWSER") != "1":
            if not _open_existing_instance(endpoint_path, lock_path):
                logger.error("running instance was detected but could not be reopened")
        return
    requested_port = os.getenv("THINKTANK_PORT", "").strip()
    try:
        port = _choose_port(int(requested_port) if requested_port else None)
    except (ValueError, OSError):
        port = _choose_port()
    _write_endpoint(endpoint_path, port)
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
            endpoint_path.unlink(missing_ok=True)
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            lock_file.close()


if __name__ == "__main__":
    main()
