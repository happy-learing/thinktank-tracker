from __future__ import annotations

from thinktank_tracker.crawler import bundled_chromium_executable
from thinktank_tracker.maintenance import _bundled_chromium_present


def test_bundled_chromium_detection_requires_full_chromium(tmp_path) -> None:
    browser_root = tmp_path / "runtime" / "browsers"
    executable = (
        browser_root
        / "chromium-1234"
        / "chrome-win64"
        / "chrome.exe"
    )
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"test")

    assert _bundled_chromium_present(browser_root) is True


def test_bundled_chromium_detection_rejects_headless_shell_only(tmp_path) -> None:
    browser_root = tmp_path / "runtime" / "browsers"
    executable = (
        browser_root
        / "chromium_headless_shell-1234"
        / "chrome-headless-shell-win64"
        / "chrome-headless-shell.exe"
    )
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"test")

    assert _bundled_chromium_present(browser_root) is False


def test_bundled_chromium_detection_rejects_non_browser_tools(tmp_path) -> None:
    browser_root = tmp_path / "runtime" / "browsers"
    browser_root.mkdir(parents=True)
    (browser_root / "ffmpeg.exe").write_bytes(b"test")

    assert _bundled_chromium_present(browser_root) is False


def test_bundled_chromium_selection_uses_numeric_revision(tmp_path) -> None:
    browser_root = tmp_path / "runtime" / "browsers"
    older = browser_root / "chromium-999" / "chrome-win64" / "chrome.exe"
    newer = browser_root / "chromium-1234" / "chrome-win64" / "chrome.exe"
    older.parent.mkdir(parents=True)
    newer.parent.mkdir(parents=True)
    older.write_bytes(b"old")
    newer.write_bytes(b"new")

    assert bundled_chromium_executable(browser_root) == newer
