from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient

import thinktank_tracker.web as web
from thinktank_tracker.models import Source


def test_dashboard_and_static_assets_load() -> None:
    with TestClient(web.app) as client:
        assert client.get("/").status_code == 200
        assert "智库研究追踪" in client.get("/").text
        assert client.get("/static/app.css").status_code == 200
        assert client.get("/static/vendor/htmx.min.js").status_code == 200


def test_default_range_never_starts_after_today(db_session) -> None:
    db_session.add(
        Source(
            name="Current source",
            homepage_url="https://example.org/",
            publication_url="https://example.org/publications/",
            last_successful_date=date.today(),
        )
    )
    db_session.commit()
    assert web._default_range(db_session) == (date.today(), date.today())


def test_docx_import_requires_preview_confirmation(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(web, "PREVIEW_PATH", tmp_path / "preview.json")
    sample = web.PATHS.root.parent / "追踪智库.docx"
    with TestClient(web.app) as client:
        response = client.post(
            "/sources/import",
            files={"file": ("sources.docx", sample.read_bytes(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith("/sources/preview")
        assert web.PREVIEW_PATH.exists()
