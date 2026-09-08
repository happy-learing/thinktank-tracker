from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace

from fastapi.testclient import TestClient
from bs4 import BeautifulSoup
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import thinktank_tracker.web as web
from thinktank_tracker.config import AppConfig
from thinktank_tracker.constants import (
    CandidateStatus,
    CoverageStatus,
    Decision,
    REVIEW_CONTENT_TYPES,
    RunStatus,
)
from thinktank_tracker.database import Base
from thinktank_tracker.llm import SuggestionResult
from thinktank_tracker.models import (
    Candidate,
    CandidateAttachment,
    CrawlRun,
    Assessment,
    ModelSuggestion,
    Source,
    SourceConfiguration,
    SourceEntry,
    SourceEntryRun,
    SourceRun,
)
from thinktank_tracker.schemas import ReviewSuggestionPayload


def test_dashboard_and_static_assets_load() -> None:
    with TestClient(web.app) as client:
        assert client.get("/health").json() == {
            "status": "ok",
            "database_version": web.CURRENT_SCHEMA_VERSION,
        }
        assert client.get("/").status_code == 200
        assert "智库研究追踪" in client.get("/").text
        assert "/static/app.css?v=0.6.1" in client.get("/").text
        assert client.get("/sources").status_code == 200
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
        preview = client.get("/sources/preview")
        assert preview.status_code == 200
        assert "确认智库清单" in preview.text
        assert "检索入口" in preview.text


def isolated_web_database(monkeypatch):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)

    @contextmanager
    def scope():
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(web, "SessionLocal", factory)
    monkeypatch.setattr(web, "session_scope", scope)
    return factory


def seed_completed_run(factory):
    with factory() as session:
        source = Source(name="Route Institute", homepage_url="https://route.example/", publication_url="https://route.example/reports/")
        session.add(source)
        session.flush()
        entry = SourceEntry(
            source_id=source.id,
            url="https://route.example/reports/",
            canonical_url="https://route.example/reports",
        )
        run = CrawlRun(
            start_date=date(2026, 8, 2),
            end_date=date(2026, 8, 20),
            status=RunStatus.COMPLETED,
        )
        session.add_all([entry, run])
        session.flush()
        source_run = SourceRun(
            run_id=run.id,
            source_id=source.id,
            status=CoverageStatus.COMPLETE,
        )
        session.add(source_run)
        session.flush()
        session.add(
            SourceEntryRun(
                run_id=run.id,
                source_run_id=source_run.id,
                source_entry_id=entry.id,
                status=CoverageStatus.COMPLETE,
            )
        )
        candidate = Candidate(
            run_id=run.id,
            source_id=source.id,
            canonical_url="https://route.example/reports/item",
            original_url="https://route.example/reports/item/",
            title_original="Route research title",
            published_date=date(2026, 8, 8),
            date_status="confirmed",
            date_evidence="meta",
            content_type="report",
            content_type_evidence="URL",
            extracted_text="strategy " * 1600,
            language="en",
            english_word_count=1600,
            extraction_status="complete",
            status=CandidateStatus.NEEDS_REVIEW,
        )
        session.add(candidate)
        session.commit()
        return source.id, entry.id, run.id, candidate.id


def test_source_entry_and_run_soft_delete_restore_without_losing_history(monkeypatch) -> None:
    factory = isolated_web_database(monkeypatch)
    source_id, entry_id, run_id, candidate_id = seed_completed_run(factory)

    with TestClient(web.app) as client:
        assert client.post(f"/sources/{source_id}/entries/{entry_id}/delete", follow_redirects=False).status_code == 303
        assert "Route Institute" in client.get("/trash").text
        assert client.post(f"/trash/entries/{entry_id}/restore", follow_redirects=False).status_code == 303

        assert client.post(f"/runs/{run_id}/delete", follow_redirects=False).status_code == 303
        assert client.get(f"/runs/{run_id}").status_code == 404
        assert client.get(f"/candidates/{candidate_id}").status_code == 404
        assert "Route research title" not in client.get("/review").text
        assert client.post(f"/trash/runs/{run_id}/restore", follow_redirects=False).status_code == 303
        assert client.get(f"/runs/{run_id}").status_code == 200
        assert client.get(f"/candidates/{candidate_id}").status_code == 200

    with factory() as session:
        assert session.get(SourceEntry, entry_id).deleted_at is None
        assert session.get(CrawlRun, run_id).deleted_at is None
        assert session.get(Candidate, candidate_id).title_original == "Route research title"


def test_source_soft_delete_restore_preserves_history(monkeypatch) -> None:
    factory = isolated_web_database(monkeypatch)
    source_id, _, run_id, candidate_id = seed_completed_run(factory)

    with TestClient(web.app) as client:
        deleted = client.post(f"/sources/{source_id}/delete", follow_redirects=False)
        assert deleted.status_code == 303
        assert "Route Institute" not in client.get("/sources").text
        trash = client.get("/trash")
        assert "Route Institute" in trash.text
        assert client.get(f"/runs/{run_id}").status_code == 200
        assert client.get(f"/candidates/{candidate_id}").status_code == 200
        restored = client.post(
            f"/trash/sources/{source_id}/restore", follow_redirects=False
        )
        assert restored.status_code == 303
        assert "Route Institute" in client.get(restored.headers["location"]).text

    with factory() as session:
        source = session.get(Source, source_id)
        assert source.deleted_at is None
        assert source.enabled is True
        assert source.configuration_id is not None
        assert session.get(Candidate, candidate_id).source_id == source_id


def test_running_task_cannot_be_deleted(monkeypatch) -> None:
    factory = isolated_web_database(monkeypatch)
    with factory() as session:
        run = CrawlRun(start_date=date(2026, 8, 2), end_date=date(2026, 8, 3), status=RunStatus.RUNNING)
        session.add(run)
        session.commit()
        run_id = run.id
    with TestClient(web.app) as client:
        response = client.post(f"/runs/{run_id}/delete", follow_redirects=False)
        assert response.status_code == 303
        assert "level=warning" in response.headers["location"]
    with factory() as session:
        assert session.get(CrawlRun, run_id).deleted_at is None


def test_source_cannot_be_reenabled_without_an_active_entry(monkeypatch) -> None:
    factory = isolated_web_database(monkeypatch)
    source_id, entry_id, _, _ = seed_completed_run(factory)
    with TestClient(web.app) as client:
        client.post(f"/sources/{source_id}/entries/{entry_id}/delete", follow_redirects=False)
        client.post(f"/sources/{source_id}/toggle", follow_redirects=False)
        blocked = client.post(f"/sources/{source_id}/toggle", follow_redirects=False)
        assert "level=warning" in blocked.headers["location"]
    with factory() as session:
        assert session.get(Source, source_id).enabled is False


def test_permanent_run_purge_requires_delete_and_creates_backup_first(monkeypatch) -> None:
    factory = isolated_web_database(monkeypatch)
    _, _, run_id, candidate_id = seed_completed_run(factory)
    backups: list[str] = []
    monkeypatch.setattr(web, "create_database_backup", lambda: backups.append("created"))

    with TestClient(web.app) as client:
        client.post(f"/runs/{run_id}/delete", follow_redirects=False)
        blocked = client.post(
            f"/trash/runs/{run_id}/purge",
            data={"confirm": "wrong"},
            follow_redirects=False,
        )
        assert "level=warning" in blocked.headers["location"]
        assert backups == []
        success = client.post(
            f"/trash/runs/{run_id}/purge",
            data={"confirm": "DELETE"},
            follow_redirects=False,
        )
        assert success.status_code == 303
        assert backups == ["created"]

    with factory() as session:
        assert session.get(CrawlRun, run_id) is None
        assert session.get(Candidate, candidate_id) is None


def test_manual_model_suggestion_is_audited_without_confirming_or_approving(monkeypatch) -> None:
    factory = isolated_web_database(monkeypatch)
    _, _, _, candidate_id = seed_completed_run(factory)

    class FakeProvider:
        def __init__(self, _config):
            self.config = SimpleNamespace(model_name="deepseek-test", max_model_input_chars=60_000)

        def configured(self):
            return True

        def suggest_review(self, **_snapshot):
            payload = ReviewSuggestionPayload(
                decision=Decision.INCLUDE,
                suggested_date=date(2026, 8, 9),
                date_evidence="正文首页日期",
                content_type="reports",
                primary_category="macro",
                secondary_categories=["technology_competition"],
                title_en="Suggested English title",
                title_zh="建议中文标题",
                core_claim_zh="建议核心观点。",
                reasoning_zh="正文涉及战略竞争。",
                evidence=["战略竞争证据"],
                confidence=0.88,
                field_confidences={"suggested_date": 0.6, "title_zh": 0.9},
            )
            return SuggestionResult(payload, '{"decision":"include"}')

    monkeypatch.setattr(web, "OpenAICompatibleProvider", FakeProvider)
    with TestClient(web.app) as client:
        response = client.post(f"/candidates/{candidate_id}/model-suggestions")
        assert response.status_code == 200
        assert "Suggested English title" in response.text
        assert "应用只填写当前表单" in response.text
        assert 'name="confirm_date"' not in response.text

    with factory() as session:
        candidate = session.get(Candidate, candidate_id)
        suggestion = session.scalar(select(ModelSuggestion).where(ModelSuggestion.candidate_id == candidate_id))
        assert candidate.status == CandidateStatus.NEEDS_REVIEW
        assert candidate.published_date == date(2026, 8, 8)
        assert candidate.date_status == "confirmed"
        assert candidate.review is None
        assert suggestion.title_zh == "建议中文标题"
        assert suggestion.provider_model == "deepseek-test"
        assert suggestion.decision == Decision.NEEDS_REVIEW


def test_manual_review_only_lists_requested_types_and_custom(monkeypatch) -> None:
    factory = isolated_web_database(monkeypatch)
    _, _, _, candidate_id = seed_completed_run(factory)

    with TestClient(web.app) as client:
        response = client.get(f"/candidates/{candidate_id}")

    soup = BeautifulSoup(response.text, "lxml")
    select_node = soup.select_one('select[name="content_type"]')
    assert select_node is not None
    assert [option.get("value") for option in select_node.select("option")] == [
        "",
        *AppConfig().content_types,
        "__custom__",
    ]


def test_manual_review_shows_pdf_parser_and_fallback_reason(monkeypatch) -> None:
    factory = isolated_web_database(monkeypatch)
    _, _, _, candidate_id = seed_completed_run(factory)
    with factory() as session:
        session.add(
            CandidateAttachment(
                candidate_id=candidate_id,
                url="https://example.org/report.pdf",
                canonical_url="https://example.org/report.pdf",
                link_text="Report PDF",
                extraction_status="complete",
                extraction_audit_json=json.dumps(
                    {
                        "final_engine": "pypdf",
                        "final_engine_version": "6.16.2",
                        "fallback_reason": "anydoc_malformed",
                        "fallback_detail": "AnyDoc 转换失败: broken xref",
                        "ocr_pages": [],
                        "attempts": [],
                    },
                    ensure_ascii=False,
                ),
            )
        )
        session.commit()

    with TestClient(web.app) as client:
        response = client.get(f"/candidates/{candidate_id}")

    assert response.status_code == 200
    assert "解析器：pypdf 6.16.2" in response.text
    assert "回退原因：AnyDoc 转换失败: broken xref" in response.text


def test_settings_save_unified_content_type_rules(monkeypatch) -> None:
    isolated_web_database(monkeypatch)
    captured: list[AppConfig] = []
    monkeypatch.setattr(web, "load_config", lambda: AppConfig())
    monkeypatch.setattr(web, "save_config", captured.append)

    with TestClient(web.app) as client:
        response = client.post(
            "/settings",
            data={
                "content_type_rules_present": "1",
                "content_type_rule_match_kind": ["url_path", "card_label"],
                "content_type_rule_match_value": ["essays", "Opinion"],
                "content_type_rule_content_type": ["research essay", "opinion"],
                "content_type_rule_action": ["allow", "exclude"],
                "pdf_parser_mode": "pypdf_only",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    normalized = [
        {key: value for key, value in rule.items() if key != "rule_id"}
        for rule in captured[0].content_type_rules
    ]
    assert normalized[:2] == [
        {
            "match_kind": "url_path",
            "match_value": "essays",
            "content_type": "research essay",
            "action": "allow",
        },
        {
            "match_kind": "card_label",
            "match_value": "opinion",
            "content_type": "opinion",
            "action": "exclude",
        },
    ]
    assert {
        "match_kind": "card_label",
        "match_value": "commissioned reports",
        "content_type": "report",
        "action": "allow",
    } in normalized
    assert captured[0].pdf_parser_mode == "pypdf_only"
    assert "2" in response.headers["location"] or "0" in response.headers["location"]


def test_manual_field_suggestion_cannot_contradict_initial_assessment(monkeypatch) -> None:
    factory = isolated_web_database(monkeypatch)
    _, _, _, candidate_id = seed_completed_run(factory)
    with factory() as session:
        session.add(
            Assessment(
                candidate_id=candidate_id,
                decision=Decision.EXCLUDE,
                reasoning_zh="自动初筛判定排除。",
                confidence=0.92,
                provider_model="deepseek-initial",
            )
        )
        session.commit()

    class ContradictingProvider:
        def __init__(self, _config):
            self.config = SimpleNamespace(model_name="deepseek-fields", max_model_input_chars=60_000)

        def configured(self):
            return True

        def suggest_review(self, **snapshot):
            assert snapshot["authoritative_decision"] == Decision.EXCLUDE
            return SuggestionResult(
                ReviewSuggestionPayload(
                    decision=Decision.INCLUDE,
                    content_type="commissioned reports",
                    reasoning_zh="第二次调用错误地建议纳入。",
                    confidence=0.9,
                ),
                '{"decision":"include"}',
            )

    monkeypatch.setattr(web, "OpenAICompatibleProvider", ContradictingProvider)
    with TestClient(web.app) as client:
        response = client.post(f"/candidates/{candidate_id}/model-suggestions")
        detail = client.get(f"/candidates/{candidate_id}")

    assert "主题决定沿用自动初筛结论" in response.text
    assert "第二次调用错误地建议纳入" not in response.text
    assert "commissioned reports" not in response.text
    assert "主题决定沿用自动初筛结论" in detail.text
    with factory() as session:
        suggestion = session.scalar(
            select(ModelSuggestion).where(ModelSuggestion.candidate_id == candidate_id)
        )
        assert suggestion.decision == Decision.EXCLUDE
        assert suggestion.content_type == "report"


def test_full_history_purge_requires_exact_confirmation_and_no_active_run(monkeypatch, tmp_path) -> None:
    calls: list[str] = []
    monkeypatch.setattr(web, "run_is_active", lambda: False)
    monkeypatch.setattr(
        web,
        "purge_history_with_backup",
        lambda: (tmp_path / "history-backup.zip", calls.append("purged") or 3),
    )
    with TestClient(web.app) as client:
        blocked = client.post(
            "/maintenance/history/purge",
            data={"confirm_text": "wrong"},
            follow_redirects=False,
        )
        assert "level=warning" in blocked.headers["location"]
        assert calls == []
        success = client.post(
            "/maintenance/history/purge",
            data={"confirm_text": "清空全部历史"},
            follow_redirects=False,
        )
        assert success.status_code == 303
        assert calls == ["purged"]


def test_source_configuration_routes_keep_duplicate_urls_independent(monkeypatch) -> None:
    factory = isolated_web_database(monkeypatch)
    with TestClient(web.app) as client:
        client.get("/sources")
        created = client.post(
            "/configurations",
            data={"name": "配置2"},
            follow_redirects=False,
        )
        assert created.status_code == 303
        with factory() as session:
            configurations = session.scalars(
                select(SourceConfiguration).order_by(SourceConfiguration.id)
            ).all()
            first, second = configurations
        for configuration, name in ((first, "First copy"), (second, "Second copy")):
            response = client.post(
                "/sources/save",
                data={
                    "configuration_id": configuration.id,
                    "name": name,
                    "homepage_url": "https://same.example/",
                    "publication_url": "https://same.example/reports/",
                    "ordered_by_date": "on",
                },
                follow_redirects=False,
            )
            assert response.status_code == 303
        with factory() as session:
            sources = session.scalars(select(Source).order_by(Source.configuration_id)).all()
            assert len(sources) == 2
            assert sources[0].configuration_id != sources[1].configuration_id
            assert sources[0].homepage_url == sources[1].homepage_url
            sources[0].name = "Changed"
            session.commit()
            assert sources[1].name == "Second copy"


def test_source_link_test_persists_result_for_enabled_entries(monkeypatch) -> None:
    factory = isolated_web_database(monkeypatch)
    with factory() as session:
        configuration = SourceConfiguration(name="配置1")
        session.add(configuration)
        session.flush()
        source = Source(
            configuration_id=configuration.id,
            name="Institute",
            homepage_url="https://test.example/",
            publication_url="https://test.example/reports/",
        )
        session.add(source)
        session.flush()
        entry = SourceEntry(
            source_id=source.id,
            url="https://test.example/reports/",
            canonical_url="https://test.example/reports",
        )
        session.add(entry)
        session.commit()
        configuration_id, source_id, entry_id = configuration.id, source.id, entry.id

    monkeypatch.setattr(
        web,
        "test_entries",
        lambda snapshots, _config: [
            SimpleNamespace(
                entry_id=snapshots[0].id,
                status="warning",
                final_url="https://test.example/reports/",
                http_status=200,
                discovered_count=0,
                accepted_count=0,
                message="连接成功，但首个栏目页未识别到文章卡片",
            )
        ],
    )
    with TestClient(web.app) as client:
        response = client.post(
            f"/configurations/{configuration_id}/sources/{source_id}/test",
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "level=warning" in response.headers["location"]
        page = client.get(f"/sources?configuration_id={configuration_id}")
        assert "测试警告" in page.text
        assert "未识别到文章卡片" in page.text
    with factory() as session:
        entry = session.get(SourceEntry, entry_id)
        assert entry.last_test_status == "warning"
        assert entry.last_tested_at is not None
        assert entry.last_test_http_status == 200


def test_entry_preview_shows_counts_authors_and_genre_evidence(monkeypatch) -> None:
    factory = isolated_web_database(monkeypatch)
    with factory() as session:
        configuration = SourceConfiguration(name="配置1")
        session.add(configuration)
        session.flush()
        source = Source(
            configuration_id=configuration.id,
            name="Institute",
            homepage_url="https://preview.example/",
            publication_url="https://preview.example/reports/",
        )
        session.add(source)
        session.flush()
        entry = SourceEntry(
            source_id=source.id,
            url="https://preview.example/reports/",
            canonical_url="https://preview.example/reports",
        )
        session.add(entry)
        session.commit()
        source_id, entry_id = source.id, entry.id

    class PreviewCrawler:
        def __init__(self, _config):
            pass

        def fetch(self, url):
            return SimpleNamespace(body=b"html", url=url)

        def close(self):
            pass

    monkeypatch.setattr(web, "WebCrawler", PreviewCrawler)
    monkeypatch.setattr(
        web,
        "discover_content_cards",
        lambda *_args, **_kwargs: [
            SimpleNamespace(
                fetch_url="https://preview.example/report/one",
                title="Strategic report",
                published_date=date(2026, 8, 12),
                content_type="policy brief",
                content_type_evidence="栏目卡片体裁标签：Policy Brief",
                authors=("Alice Example", "Bob Example"),
                author_evidence="栏目卡片作者选择器：.author",
            )
        ],
    )

    with TestClient(web.app) as client:
        page = client.get(f"/sources/{source_id}/entries/{entry_id}/preview")

    assert page.status_code == 200
    assert "识别 1 条，接收 1 条" in page.text
    assert "Alice Example、Bob Example" in page.text
    assert "栏目卡片体裁标签：Policy Brief" in page.text
    assert "栏目卡片作者选择器：.author" in page.text


def test_review_queue_groups_candidates_by_run_and_filters_rows(monkeypatch) -> None:
    factory = isolated_web_database(monkeypatch)
    _, _, first_run_id, first_candidate_id = seed_completed_run(factory)
    with factory() as session:
        first = session.get(Candidate, first_candidate_id)
        first_source_run = session.scalar(
            select(SourceRun).where(SourceRun.run_id == first_run_id)
        )
        first_source_run.source_name_snapshot = "任务时智库名称"
        first.source.name = "后来改名的智库"
        second_run = CrawlRun(
            start_date=date(2026, 8, 21),
            end_date=date(2026, 8, 28),
            status=RunStatus.COMPLETED,
            configuration_name_snapshot="配置2",
        )
        session.add(second_run)
        session.flush()
        second = Candidate(
            run_id=second_run.id,
            source_id=first.source_id,
            canonical_url="https://route.example/reports/second",
            original_url="https://route.example/reports/second/",
            title_original="Second task candidate",
            status=CandidateStatus.APPROVED,
        )
        session.add(second)
        session.commit()
        second_run_id = second_run.id

    with TestClient(web.app) as client:
        pending_page = client.get("/review?status=pending")
        assert f"任务 #{first_run_id}" in pending_page.text
        assert f"任务 #{second_run_id}" not in pending_page.text
        assert "Route research title" not in pending_page.text
        first_rows = client.get(f"/review/runs/{first_run_id}?status=pending")
        assert "Route research title" in first_rows.text
        assert "任务时智库名称" in first_rows.text
        assert "后来改名的智库" not in first_rows.text
        approved_page = client.get("/review?status=approved")
        assert f"任务 #{second_run_id}" in approved_page.text
        approved_rows = client.get(f"/review/runs/{second_run_id}?status=approved")
        assert "Second task candidate" in approved_rows.text
        assert "Route research title" not in approved_rows.text


def test_review_queue_paginates_twenty_task_groups(monkeypatch) -> None:
    factory = isolated_web_database(monkeypatch)
    source_id, _, first_run_id, _ = seed_completed_run(factory)
    with factory() as session:
        for index in range(20):
            run = CrawlRun(
                start_date=date(2026, 8, 1),
                end_date=date(2026, 8, 28),
                status=RunStatus.COMPLETED,
                configuration_name_snapshot=f"配置{index + 2}",
            )
            session.add(run)
            session.flush()
            session.add(
                Candidate(
                    run_id=run.id,
                    source_id=source_id,
                    canonical_url=f"https://route.example/reports/page-{index}",
                    original_url=f"https://route.example/reports/page-{index}/",
                    title_original=f"Paged candidate {index}",
                    status=CandidateStatus.NEEDS_REVIEW,
                )
            )
        session.commit()

    with TestClient(web.app) as client:
        first_page = BeautifulSoup(client.get("/review?status=pending").text, "html.parser")
        second_page = BeautifulSoup(
            client.get("/review?status=pending&page_number=2").text,
            "html.parser",
        )
    assert len(first_page.select("details.review-run-group")) == 20
    assert len(first_page.select("details.review-run-group[open]")) == 1
    assert len(second_page.select("details.review-run-group")) == 1
    assert f"任务 #{first_run_id}" in second_page.get_text(" ", strip=True)


def test_review_run_lazy_rows_have_no_five_hundred_candidate_limit(monkeypatch) -> None:
    factory = isolated_web_database(monkeypatch)
    source_id, _, run_id, _ = seed_completed_run(factory)
    with factory() as session:
        session.add_all(
            [
                Candidate(
                    run_id=run_id,
                    source_id=source_id,
                    canonical_url=f"https://route.example/reports/bulk-{index}",
                    original_url=f"https://route.example/reports/bulk-{index}/",
                    title_original=f"Bulk candidate {index}",
                    status=CandidateStatus.NEEDS_REVIEW,
                )
                for index in range(500)
            ]
        )
        session.commit()

    with TestClient(web.app) as client:
        fragment = BeautifulSoup(
            client.get(f"/review/runs/{run_id}?status=pending").text,
            "html.parser",
        )
    assert len(fragment.select("tbody tr")) == 501


def test_single_source_rerun_uses_current_entries_for_original_source(monkeypatch) -> None:
    factory = isolated_web_database(monkeypatch)
    source_id, _, run_id, _ = seed_completed_run(factory)
    with factory() as session:
        configuration = SourceConfiguration(name="配置1")
        session.add(configuration)
        session.flush()
        source = session.get(Source, source_id)
        source.configuration_id = configuration.id
        session.add(
            SourceEntry(
                source_id=source_id,
                url="https://route.example/new-research/",
                canonical_url="https://route.example/new-research",
            )
        )
        session.commit()
        configuration_id = configuration.id

    calls: list[tuple[tuple, dict]] = []

    def fake_create_run(*args, **kwargs):
        calls.append((args, kwargs))
        return 99

    monkeypatch.setattr(web, "create_run", fake_create_run)
    monkeypatch.setattr(web, "start_run_in_background", lambda _run_id: False)
    with TestClient(web.app) as client:
        response = client.post(
            f"/runs/{run_id}/sources/{source_id}/rerun",
            follow_redirects=False,
        )
    assert response.status_code == 303
    assert calls == [
        (
            (date(2026, 8, 2), date(2026, 8, 20), [source_id]),
            {
                "configuration_id": configuration_id,
                "mark_configuration_used": False,
            },
        )
    ]


def test_v4_json_import_creates_independent_configurations(monkeypatch, tmp_path) -> None:
    factory = isolated_web_database(monkeypatch)
    monkeypatch.setattr(web, "PREVIEW_PATH", tmp_path / "preview.json")
    payload = {
        "schema_version": 4,
        "configurations": [
            {
                "name": "配置A",
                "sources": [
                    {
                        "name": "One",
                        "homepage_url": "https://same.example/",
                        "entries": [{"url": "https://same.example/reports/"}],
                    }
                ],
            },
            {
                "name": "配置B",
                "sources": [
                    {
                        "name": "Two",
                        "homepage_url": "https://same.example/",
                        "entries": [{"url": "https://same.example/analysis/"}],
                    }
                ],
            },
        ],
    }
    with TestClient(web.app) as client:
        client.get("/sources")
        client.post(
            "/configurations",
            data={"name": "配置A"},
            follow_redirects=False,
        )
        with factory() as session:
            default_id = session.scalar(
                select(SourceConfiguration.id).order_by(SourceConfiguration.id)
            )
        preview = client.post(
            "/sources/import",
            data={"configuration_id": default_id},
            files={"file": ("sources.json", json.dumps(payload).encode("utf-8"), "application/json")},
            follow_redirects=False,
        )
        assert preview.status_code == 303
        assert "配置A" in client.get("/sources/preview").text
        assert client.post("/sources/confirm", follow_redirects=False).status_code == 303
    with factory() as session:
        configurations = session.scalars(
            select(SourceConfiguration).where(SourceConfiguration.deleted_at.is_(None))
        ).all()
        sources = session.scalars(select(Source).order_by(Source.configuration_id)).all()
        assert {row.name for row in configurations} >= {
            "配置A",
            "配置A（导入）",
            "配置B",
        }
        imported = [row for row in sources if row.name in {"One", "Two"}]
        assert len(imported) == 2
        assert imported[0].homepage_url == imported[1].homepage_url
        assert imported[0].configuration_id != imported[1].configuration_id
