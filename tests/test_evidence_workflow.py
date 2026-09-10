from datetime import date
import json
from types import SimpleNamespace

import pytest
from docx import Document
from thinktank_tracker.config import AppConfig
from thinktank_tracker.constants import CandidateStatus
from thinktank_tracker.crawler import FetchAttempt, FetchError, FetchedResource, WebCrawler
from thinktank_tracker.llm import OpenAICompatibleProvider
from thinktank_tracker.models import Assessment, Candidate, CrawlRun, Source, SourceRun
from thinktank_tracker.selection import extraction_gate, fact_issues, policy_snapshot, run_policy
from thinktank_tracker.url_utils import collect_content_type_evidence, resolve_content_type
import thinktank_tracker.exporters as exporters


def answer(evidence="Strategic rivalry drives institutional change.", decision="include", score=0.02):
    return dict(decision=decision, research_substance="substantive", primary_category="macro",
        title_en="Strategic rivalry", title_zh="战略竞争", core_claim_zh="竞争推动制度变化。",
        reasoning_zh="论证国际制度竞争", strategic_nexus="国际竞争是主论题", exclusion_code="outside_scope" if decision=="exclude" else "",
        evidence=[evidence], confidence=score)


@pytest.mark.parametrize("verdict,quote,expected", [
    ("supported", "Strategic rivalry drives institutional change.", "supported"),
    ("contradicted", "Strategic rivalry drives institutional change.", "contradicted"),
    ("supported", "An invented quote.", "uncertain"),
])
def test_model_review_requires_actual_source_evidence(monkeypatch, verdict, quote, expected):
    provider = OpenAICompatibleProvider(AppConfig())
    outputs = iter([answer(), {"verdict":verdict,"reason":"核验结论","evidence":[quote]}])
    monkeypatch.setattr(provider, "_request", lambda *a, **kw: json.dumps(next(outputs)))
    result = provider.assess("Title", "Institute", "unknown", "Strategic rivalry drives institutional change.")
    assert result.verification_status == expected
    assert result.input_complete
    assert result.payload.confidence == 0.02  # Self-reported numbers do not determine verification.
    assert result.request_count == 2


def test_long_body_is_covered_in_contiguous_segments_without_silent_truncation(monkeypatch):
    provider = OpenAICompatibleProvider(AppConfig(max_model_input_chars=5000))
    source = "Strategic rivalry drives institutional change. " * 310
    prompts = []
    def request(messages, **kwargs):
        prompt = messages[1]["content"]
        prompts.append(prompt)
        if "schema_model" in kwargs:
            return json.dumps({"verdict":"supported","reason":"各段论证一致","evidence":answer()["evidence"]})
        return json.dumps(answer())
    monkeypatch.setattr(provider, "_request", request)
    result = provider.assess("Title", "Institute", "report", source)
    assert result.verification_status == "supported", result.error
    assert all(len(prompt) <= 5000 for prompt in prompts)
    spans = [row for row in result.segment_audit if "start" in row]
    assert len(spans) > 1
    assert spans[0]["start"] == 0 and spans[-1]["end"] == len(source)
    assert all(a["end"] == b["start"] for a,b in zip(spans,spans[1:]))
    assert result.request_count == len(spans) + 2


def test_exceeding_document_budget_never_claims_complete_analysis(monkeypatch):
    provider=OpenAICompatibleProvider(AppConfig(max_model_input_chars=5000,max_model_chunks=1))
    monkeypatch.setattr(provider,"_request",lambda *a,**kw:pytest.fail("over-budget input should be detected before API calls"))
    result=provider.assess("Title","Source","report","Evidence. "*1000)
    assert result.payload is None and not result.input_complete
    assert result.request_count == 0


def test_label_phrase_matching_has_word_boundaries_and_keeps_conflicts():
    assert resolve_content_type("https://example.org/item",["Policy Brief No. 42"])[0] == "policy brief"
    assert resolve_content_type("https://example.org/item",["reportage"])[0] != "report"
    kind, evidence, _ = resolve_content_type("https://example.org/item",["Report / Commentary"])
    assert kind == "unknown" and "体裁冲突" in evidence
    facts = collect_content_type_evidence("https://example.org/item",["Policy Brief No. 42"])
    assert facts[0].raw_value == "Policy Brief No. 42"
    assert facts[0].match_kind == "card_keyword"


def test_unknown_facts_do_not_stop_body_analysis_and_cannot_enter_report():
    item = Candidate(content_type="unknown", date_status="missing", language="en",
        english_word_count=1600, extracted_text="Available complete research.", extraction_status="complete")
    policy=policy_snapshot(AppConfig())
    assert extraction_gate(item,date(2026,8,1),date(2026,8,31),policy)[0] == CandidateStatus.DISCOVERED
    assert len(fact_issues(item,date(2026,8,1),date(2026,8,31),policy)) == 2


def test_substance_mode_is_explicit_and_snapshot_does_not_follow_global_edits():
    config=AppConfig(selection_mode="substance",min_english_words=1500)
    snapshot=policy_snapshot(config)
    run=CrawlRun(selection_policy_json=json.dumps(snapshot))
    config.selection_mode="strict"
    item=Candidate(content_type="commentary",date_status="confirmed",published_date=date(2026,8,3),
        language="en",english_word_count=900,extracted_text="Short but substantive.",extraction_status="complete")
    assert not fact_issues(item,date(2026,8,1),date(2026,8,31),run_policy(run))
    assert run_policy(run)["mode"] == "substance"


def test_http_403_reaches_recovery_and_preserves_actual_status(monkeypatch,tmp_path):
    crawler=WebCrawler(AppConfig(per_domain_delay_seconds=0))
    def blocked(url):
        raise FetchError("HTTP 403",[FetchAttempt(url,403,"http_error")],code="http_error")
    monkeypatch.setattr(crawler,"_request",blocked)
    monkeypatch.setattr(crawler,"_cache_file",lambda *a:tmp_path/"page.html")
    monkeypatch.setattr(crawler,"_render_with_scrapling",lambda url:(b"<html><article>Research</article></html>",url+"final/",200))
    try:
        result=crawler.fetch("https://example.org/publications/")
    finally:
        crawler.close()
    assert result.status_code == 200 and result.url.endswith("final/")
    assert [a.status_code for a in result.attempts] == [403,200]


def test_browser_error_page_cannot_be_cached_as_success(monkeypatch,tmp_path):
    crawler=WebCrawler(AppConfig(per_domain_delay_seconds=0))
    monkeypatch.setattr(crawler,"_render_with_scrapling",lambda url:(b"<title>Access denied</title>",url,403))
    monkeypatch.setattr(crawler,"_cache_file",lambda *a:tmp_path/"bad.html")
    try:
        with pytest.raises(FetchError):
            crawler.render_resource("https://example.org",stealth_first=True)
    finally:
        crawler.close()
    assert not (tmp_path/"bad.html").exists()


def test_draft_includes_only_verified_ready_items_and_lists_exceptions(db_session,monkeypatch,tmp_path):
    source=Source(name="Institute",homepage_url="https://example.org/",publication_url="https://example.org/reports/")
    run=CrawlRun(start_date=date(2026,8,1),end_date=date(2026,8,31),selection_policy_json=json.dumps(policy_snapshot(AppConfig())))
    db_session.add_all([source,run]); db_session.flush()
    db_session.add(SourceRun(run_id=run.id,source_id=source.id,source_name_snapshot="Saved Institute",status="incomplete",stop_reason="Page 2 unavailable"))
    for index in range(3):
        row=Candidate(run_id=run.id,source_id=source.id,canonical_url=f"https://example.org/report/{index}",original_url=f"https://example.org/report/{index}",
            title_original=f"Original {index}",title_en=f"Research {index}",title_zh="研究",authors_json='["Alice Example"]' if index!=1 else '[]',
            published_date=date(2026,8,3),date_status="confirmed",content_type="report",language="en",english_word_count=1600,
            extracted_text="Research evidence.",extraction_status="complete",status=CandidateStatus.MODEL_INCLUDED,
            metadata_json=json.dumps({"selection":{"input_complete":True,"verification_status":"supported" if index!=2 else "contradicted","evidence_verified":True,"research_substance":"substantive"}}))
        db_session.add(row);db_session.flush()
        db_session.add(Assessment(candidate_id=row.id,decision="include",primary_category="macro"))
    db_session.flush()
    monkeypatch.setattr(exporters,"PATHS",SimpleNamespace(exports=tmp_path))
    path=exporters.export_draft_report(db_session,run.id)
    content="\n".join(p.text for p in Document(path).paragraphs)
    assert "自动草稿" in content and "自动筛选 1 条" in content
    assert "Research 0" in content and "Research 1" not in content and "Research 2" not in content
    assert "Original 1" in content and "作者待核实" in content
    assert "Original 2" in content and "全文分析及证据复核未通过" in content
    assert "Page 2 unavailable" in content
    assert all(row.review is None for row in run.candidates)


def test_view_pdf_is_primary_and_visible_byline_beats_cms_account():
    from thinktank_tracker.extractors import discover_pdf_links, extract_html_document
    html = b'''<html><script type="application/ld+json">{"@type":"Article","author":{"name":"Institute"}}</script>
    <main><article><div class="post-publication__header"><h1>Research title</h1><p class="list--authors"><a>Alice Example</a><a>Bob Example</a></p></div>
    <p>Overview of the study.</p><a href="/uploads/Policy_Brief_414.pdf?type=pdf">View PDF</a></article></main></html>'''
    attachments=discover_pdf_links(html,"https://example.org/study")
    assert attachments[0].role == "primary"
    doc=extract_html_document(html,"https://example.org/study")
    assert doc.authors == ["Alice Example","Bob Example"]


def test_epc_author_sidebar_does_not_absorb_related_publication_authors():
    from thinktank_tracker.adapters import get_adapter
    from thinktank_tracker.extractors import extract_html_document
    raw=b'''<main><h1>Current research</h1><div class="page-authors-author"><a href="/expert/alice">Alice Example</a></div>
    <section><div class="publication-item-author">by Unrelated Person</div></section><p>Current body.</p></main>'''
    doc=extract_html_document(raw,"https://www.epc.eu/publication/current/",author_selectors=get_adapter("epc").detail_author_selectors)
    assert doc.authors == ["Alice Example"]


def test_verification_format_error_is_repaired_without_manual_intervention(monkeypatch):
    provider=OpenAICompatibleProvider(AppConfig())
    quote=answer()["evidence"][0]
    outputs=iter([answer(),{"verdict":"supported","reason":"verified","evidence":[quote]*5},
        {"verdict":"supported","reason":"verified","evidence":[quote]}])
    monkeypatch.setattr(provider,"_request",lambda *a,**kw:json.dumps(next(outputs)))
    result=provider.assess("Title","Source","report",quote)
    assert result.verification_status == "supported"
    assert result.request_count == 3
    assert "verification_format_error" in result.segment_audit[0]


def test_release_versions_match_program_and_request_identity():
    from pathlib import Path
    import tomllib
    from thinktank_tracker import __version__
    from thinktank_tracker.crawler import USER_AGENT
    root=Path(__file__).resolve().parents[1]
    assert __version__ == (root/"VERSION").read_text().strip()
    assert __version__ == tomllib.loads((root/"pyproject.toml").read_text())["project"]["version"]
    assert f"ThinkTankTracker/{__version__}" in USER_AGENT
