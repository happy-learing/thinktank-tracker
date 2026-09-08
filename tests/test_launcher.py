from __future__ import annotations

import json

from thinktank_tracker import launcher


def test_browser_launch_uses_windows_shell_with_unique_url(monkeypatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(launcher.os, "startfile", opened.append, raising=False)
    monkeypatch.setattr(launcher.time, "time_ns", lambda: 123456)

    assert launcher._open_browser("http://127.0.0.1:43120") is True
    assert opened == ["http://127.0.0.1:43120/#launcher-123456"]


def test_second_launch_reads_unlocked_endpoint_and_opens_running_instance(tmp_path, monkeypatch) -> None:
    endpoint = tmp_path / "tracker.endpoint.json"
    legacy_lock = tmp_path / "tracker.lock"
    endpoint.write_text(json.dumps({"port": 43120, "pid": 42}), encoding="utf-8")
    legacy_lock.write_text("locked legacy payload", encoding="utf-8")
    opened: list[str] = []
    monkeypatch.setattr(launcher, "_service_ready", lambda url: url.endswith(":43120"))
    monkeypatch.setattr(launcher, "_open_browser", lambda url: opened.append(url) or True)

    assert launcher._open_existing_instance(endpoint, legacy_lock) is True
    assert opened == ["http://127.0.0.1:43120"]


def test_endpoint_reader_rejects_invalid_ports(tmp_path) -> None:
    endpoint = tmp_path / "tracker.endpoint.json"
    endpoint.write_text('{"port": 70000}', encoding="utf-8")

    assert launcher._read_existing_url(endpoint) is None

