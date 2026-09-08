from __future__ import annotations

import thinktank_tracker.llm as llm
from thinktank_tracker.config import AppConfig
from thinktank_tracker.schemas import ReviewSuggestionPayload


class FakeResponse:
    status_code = 200

    def json(self):
        return {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"decision":"needs_review","primary_category":null,'
                            '"secondary_categories":[],"title_en":"Title","title_zh":"标题",'
                            '"core_claim_zh":"结论","reasoning_zh":"需要人工确认",'
                            '"evidence":["strategy"],"confidence":0.7}'
                        )
                    }
                }
            ]
        }

    def raise_for_status(self):
        return None


class FakeClient:
    calls: list[tuple[str, dict, dict]] = []

    def __init__(self, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def post(self, url, headers, json):
        self.calls.append((url, headers, json))
        return FakeResponse()


def test_deepseek_uses_chat_json_object_mode(monkeypatch) -> None:
    FakeClient.calls.clear()
    monkeypatch.setattr(llm, "get_api_key", lambda: "secret")
    monkeypatch.setattr(llm.httpx, "Client", FakeClient)
    provider = llm.OpenAICompatibleProvider(
        AppConfig(
            provider_kind="deepseek",
            api_base_url="https://api.deepseek.com",
            model_name="deepseek-v4-flash",
            api_style="chat_completions",
        )
    )

    result = provider.assess("Title", "Institute", "policy brief", "strategy " * 1600)

    assert result.payload is not None
    url, headers, payload = FakeClient.calls[0]
    assert url == "https://api.deepseek.com/chat/completions"
    assert headers["Authorization"] == "Bearer secret"
    assert payload["model"] == "deepseek-v4-flash"
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["thinking"] == {"type": "disabled"}
    assert "json_schema" not in str(payload["response_format"])


def test_assessment_complete_user_input_respects_configured_limit(monkeypatch) -> None:
    FakeClient.calls.clear()
    monkeypatch.setattr(llm, "get_api_key", lambda: "secret")
    monkeypatch.setattr(llm.httpx, "Client", FakeClient)
    provider = llm.OpenAICompatibleProvider(
        AppConfig(
            provider_kind="deepseek",
            api_base_url="https://api.deepseek.com",
            model_name="deepseek-v4-flash",
            max_model_input_chars=5000,
        )
    )

    result = provider.assess(
        "T" * 4000,
        "Institute",
        "policy_brief",
        "Opening evidence.\n" + ("body " * 4000) + "\nConclusion\nTail evidence.",
        metadata={"abstract": "A" * 4000, "tags": ["security"], "headings": ["Conclusion"]},
    )

    assert result.payload is not None
    user_message = FakeClient.calls[0][2]["messages"][1]["content"]
    assert len(user_message) <= 5000
    assert "[元数据已按输入上限截断]" in user_message
    assert "Opening evidence." in user_message


class FakeModelsResponse:
    def json(self):
        return {"data": [{"id": "deepseek-reasoner"}, {"id": "deepseek-chat"}, {"id": "deepseek-chat"}]}

    def raise_for_status(self):
        return None


class FakeModelsClient(FakeClient):
    calls: list[tuple[str, dict]] = []

    def get(self, url, headers):
        self.calls.append((url, headers))
        return FakeModelsResponse()


def test_model_list_uses_api_root_even_when_full_chat_endpoint_is_configured(monkeypatch) -> None:
    FakeModelsClient.calls.clear()
    monkeypatch.setattr(llm, "get_api_key", lambda: "secret")
    monkeypatch.setattr(llm.httpx, "Client", FakeModelsClient)
    provider = llm.OpenAICompatibleProvider(
        AppConfig(
            provider_kind="deepseek",
            api_base_url="https://api.deepseek.com/chat/completions",
            model_name="deepseek-chat",
        )
    )

    assert provider.list_models() == ["deepseek-chat", "deepseek-reasoner"]
    assert FakeModelsClient.calls[0][0] == "https://api.deepseek.com/models"


def test_deepseek_compatible_suggestion_aliases_are_normalized() -> None:
    payload = ReviewSuggestionPayload.model_validate(
        {
            "decision": "accept",
            "suggested_date": "2026-08-10",
            "suggested_content_type": "reports",
            "suggested_topics": ["technology_competition", "rules_and_institutions"],
            "core_conclusion": "Competition reshapes export-control institutions.",
            "reasoning_zh": "涉及芯片竞争和规则博弈。",
            "confidence": 0.9,
        }
    )
    assert payload.decision == "include"
    assert payload.content_type == "reports"
    assert payload.primary_category == "technology_competition"
    assert payload.secondary_categories == ["rules_and_institutions"]
    assert payload.core_claim_zh == "Competition reshapes export-control institutions."
