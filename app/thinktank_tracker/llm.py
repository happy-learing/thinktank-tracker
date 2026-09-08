from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from .config import AppConfig, get_api_key
from .schemas import AssessmentPayload, DateAssistPayload, ReviewSuggestionPayload


SYSTEM_PROMPT = """你是严谨的国际战略研究编辑。只根据给出的文章内容判断，不把模型判断当成日期、体裁或篇幅证据。
纳入范围：宏观层面的国际关系、大国竞争、大国战略、国际思潮/意识形态/秩序之争；或微观八类：关键基础设施、科技发展竞争、关键矿产资源、具体地缘政治竞争、粮食能源等关键战略物资、国际政治经贸规则制度博弈、具有战略影响力的跨国企业、战略支点国家。
排除：与大国竞争或国际战略无直接关联的小地区一般研究、性别、儿童、教育、国内微观经济、城市建设，以及新闻、短评、活动、播客或视频简介。
只有关注主题属于文章的核心研究问题、主要论证或主要结论时才可纳入。偶然提及中国、美国、欧盟、战略、能源、科技或供应链不能构成纳入理由。国内政策、环境治理、金融监管等研究只有在国际战略或规则竞争是主论题时才可纳入。
同一文章只能选择一个 primary_category；其他相关类只放 secondary_categories。strategic_nexus 必须说明战略关联为何属于主论题。core_claim_zh 必须是文章本身的核心结论，不是主题复述。证据最多三条，每条必须是输入中可逐字回查的简短片段。排除时填写 exclusion_code。
只输出一个有效 JSON 对象，不要输出 Markdown 或额外文字。JSON 字段示例：
{"decision":"include","primary_category":"macro","secondary_categories":[],"title_en":"English title","title_zh":"中文标题","core_claim_zh":"一句话核心结论","reasoning_zh":"判断理由","strategic_nexus":"战略关联属于主论题的依据","exclusion_code":"","evidence":["原文证据"],"confidence":0.9}"""

SUGGESTION_PROMPT = """你是严谨的智库研究人工复核字段助手。根据页面确定性元数据、URL 和提取正文提出字段建议，但不得声称模型已经确认日期或体裁，也不得独立推翻既有主题决定。
主题分类只能使用 macro、critical_infrastructure、technology_competition、critical_minerals、geopolitical_competition、strategic_materials、rules_and_institutions、strategic_multinationals、pivot_states。
decision 只能是 include、exclude、needs_review 三者之一。用户提供“既有主题决定”时必须原样照抄；没有既有主题决定时才可建议 needs_review。日期必须输出 YYYY-MM-DD 或 null，并在 date_evidence 中指出正文或页面证据；没有明确证据时必须为 null，不能把系统当前日期当作发布日期。content_type 只能原样照抄“确定性体裁”；该值为 unknown 时输出空字符串，不得根据标题或正文猜测。核心观点必须是文章的研究结论。
只输出一个有效 JSON 对象，字段必须使用以下名称，不得改名或添加前缀：
{"decision":"needs_review","suggested_date":null,"date_evidence":"","content_type":"report","primary_category":null,"secondary_categories":[],"title_en":"","title_zh":"","core_claim_zh":"","reasoning_zh":"","evidence":[],"confidence":0.0,"field_confidences":{"content_type":0.0,"title_zh":0.0}}"""

_MODEL_SEMAPHORE = threading.Semaphore(1)


@dataclass
class ModelResult:
    payload: AssessmentPayload | None
    raw_response: str = ""
    error: str = ""


@dataclass
class SuggestionResult:
    payload: ReviewSuggestionPayload | None
    raw_response: str = ""
    error: str = ""


@dataclass
class DateAssistResult:
    payload: DateAssistPayload | None
    raw_response: str = ""
    error: str = ""


def _structured_excerpt(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    conclusion_indexes = [
        index
        for index, line in enumerate(lines)
        if re.search(r"\b(conclusion|conclusions|recommendation|recommendations|summary)\b|结论|建议", line, re.I)
    ]
    tail_blocks: list[str] = []
    for index in conclusion_indexes[-3:]:
        tail_blocks.append("\n".join(lines[index : index + 24]))
    tail_blocks.append("\n".join(lines[-80:]))
    tail = "\n\n[结论与尾部证据]\n" + "\n\n".join(dict.fromkeys(tail_blocks))
    head_budget = min(limit, max(min(1000, limit), limit - len(tail)))
    return (text[:head_budget] + tail[: max(0, limit - head_budget)])[:limit]


def _bounded_prompt(prefix: str, text: str, limit: int) -> str:
    """Keep the complete user message within the configured input ceiling."""
    ceiling = max(1, int(limit))
    body_reserve = min(4000, max(1000, ceiling // 4))
    prefix_budget = max(0, ceiling - body_reserve)
    if len(prefix) > prefix_budget:
        marker = "\n[元数据已按输入上限截断]\n"
        prefix = prefix[: max(0, prefix_budget - len(marker))] + marker
    return prefix + _structured_excerpt(text, ceiling - len(prefix))


def _endpoint(base_url: str, style: str) -> str:
    base = base_url.rstrip("/")
    leaf = "responses" if style == "responses" else "chat/completions"
    return base if base.endswith(f"/{leaf}") else f"{base}/{leaf}"


def _api_root(base_url: str) -> str:
    base = base_url.rstrip("/")
    for leaf in ("/chat/completions", "/responses", "/models"):
        if base.endswith(leaf):
            return base[: -len(leaf)]
    return base


def _extract_json(text: str) -> dict:
    value = text.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*|\s*```$", "", value, flags=re.IGNORECASE)
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", value, flags=re.DOTALL)
        if not match:
            raise
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("模型输出不是 JSON 对象")
    return parsed


class OpenAICompatibleProvider:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def configured(self) -> bool:
        return bool(self.config.api_base_url and self.config.model_name and get_api_key())

    def _is_deepseek(self) -> bool:
        host = (urlsplit(self.config.api_base_url).hostname or "").lower()
        return self.config.provider_kind == "deepseek" or host == "api.deepseek.com"

    def _request(
        self,
        messages: list[dict[str, str]],
        output_mode: str | None = None,
        schema_model=AssessmentPayload,
    ) -> str:
        api_key = get_api_key()
        if not api_key:
            raise RuntimeError("尚未在 Windows Credential Manager 中配置模型 API 密钥")
        schema = schema_model.model_json_schema()
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        initial_mode = output_mode or ("json_object" if self._is_deepseek() else "json_schema")
        modes: list[str | None] = [initial_mode]
        if initial_mode == "json_schema":
            modes.append("json_object")
        modes.append(None)

        with httpx.Client(timeout=self.config.request_timeout_seconds) as client:
            response = None
            for index, mode in enumerate(dict.fromkeys(modes)):
                if self.config.api_style == "responses":
                    payload: dict = {
                        "model": self.config.model_name,
                        "input": messages,
                        "max_output_tokens": 4000,
                    }
                    if mode == "json_schema":
                        payload["text"] = {
                            "format": {"type": "json_schema", "name": "assessment", "strict": True, "schema": schema}
                        }
                    elif mode == "json_object":
                        payload["text"] = {"format": {"type": "json_object"}}
                else:
                    payload = {
                        "model": self.config.model_name,
                        "messages": messages,
                        "temperature": 0,
                        "max_tokens": 4000,
                    }
                    if self._is_deepseek():
                        payload["thinking"] = {"type": "disabled"}
                    if mode == "json_schema":
                        payload["response_format"] = {
                            "type": "json_schema",
                            "json_schema": {"name": "assessment", "strict": True, "schema": schema},
                        }
                    elif mode == "json_object":
                        payload["response_format"] = {"type": "json_object"}
                with _MODEL_SEMAPHORE:
                    response = client.post(
                        _endpoint(self.config.api_base_url, self.config.api_style),
                        headers=headers,
                        json=payload,
                    )
                if response.status_code not in {400, 404, 422} or index == len(dict.fromkeys(modes)) - 1:
                    break
            assert response is not None
            response.raise_for_status()
            data = response.json()
        if self.config.api_style == "responses":
            if isinstance(data.get("output_text"), str):
                return data["output_text"]
            chunks: list[str] = []
            for item in data.get("output", []):
                for content in item.get("content", []):
                    if isinstance(content.get("text"), str):
                        chunks.append(content["text"])
            return "".join(chunks)
        content = data["choices"][0]["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError("模型返回了空内容")
        return content

    def probe(self) -> tuple[bool, str]:
        if not self.configured():
            return False, "模型配置不完整：请填写服务地址、模型名和 API 密钥"
        messages = [
            {"role": "system", "content": "只输出有效 JSON 对象。"},
            {"role": "user", "content": '输出 JSON：{"ok":true}'},
        ]
        try:
            payload = _extract_json(self._request(messages, output_mode="json_object"))
            if payload.get("ok") is not True:
                return False, "接口已响应，但没有返回预期的 JSON 内容"
            return True, f"连接成功：{self.config.model_name}"
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            try:
                detail = exc.response.json().get("error", {}).get("message", "")
            except (ValueError, AttributeError):
                detail = ""
            suffix = f"：{detail}" if detail else ""
            return False, f"模型接口返回 HTTP {status}{suffix}"
        except Exception as exc:
            return False, f"模型连接失败：{exc}"

    def list_models(self) -> list[str]:
        api_key = get_api_key()
        if not api_key:
            raise RuntimeError("尚未在 Windows Credential Manager 中配置模型 API 密钥")
        endpoint = f"{_api_root(self.config.api_base_url)}/models"
        with httpx.Client(timeout=self.config.request_timeout_seconds) as client:
            with _MODEL_SEMAPHORE:
                response = client.get(endpoint, headers={"Authorization": f"Bearer {api_key}"})
            response.raise_for_status()
            payload = response.json()
        rows = payload.get("data", []) if isinstance(payload, dict) else []
        return sorted(
            {
                str(item.get("id", "")).strip()
                for item in rows
                if isinstance(item, dict) and str(item.get("id", "")).strip()
            }
        )

    def assess(
        self,
        title: str,
        source_name: str,
        content_type: str,
        text: str,
        metadata: dict | None = None,
    ) -> ModelResult:
        context = metadata or {}
        prompt_prefix = (
            f"来源智库：{source_name}\n标题：{title}\n确定性体裁：{content_type}\n"
            f"官网摘要：{context.get('abstract', '')}\n"
            f"官网标签：{', '.join(context.get('tags', []))}\n"
            f"正文结构：{' | '.join(context.get('headings', []))}\n"
            "以下为提取的文章全文（可能因长度上限截断）：\n\n"
        )
        user_prompt = _bounded_prompt(
            prompt_prefix,
            text,
            self.config.max_model_input_chars,
        )
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
        try:
            raw = self._request(messages)
        except (httpx.HTTPError, KeyError, RuntimeError) as request_error:
            return ModelResult(None, "", f"模型请求失败: {request_error}")
        try:
            payload = AssessmentPayload.model_validate(_extract_json(raw))
            return ModelResult(payload, raw)
        except (ValueError, ValidationError) as first_error:
            repair = messages + [
                {
                    "role": "user",
                    "content": f"上一输出校验失败：{first_error}。只重新输出一个符合 schema 的 JSON 对象。",
                }
            ]
            try:
                raw = self._request(repair)
                payload = AssessmentPayload.model_validate(_extract_json(raw))
                return ModelResult(payload, raw)
            except Exception as second_error:
                return ModelResult(None, locals().get("raw", ""), f"模型格式修复失败: {second_error}")

    def locate_publication_date(self, title: str, url: str, text: str) -> DateAssistResult:
        excerpt = text[:12000]
        messages = [
            {
                "role": "system",
                "content": (
                    "你只负责从提供的网页原文中定位当前文章的发布日期。不得使用当前日期、"
                    "更新时间、活动日期或参考文献日期。evidence_quote 必须逐字复制输入中的短句。"
                    "只输出 JSON：{\"published_date\":\"YYYY-MM-DD 或 null\","
                    "\"evidence_quote\":\"原文短句\",\"confidence\":0.0}"
                ),
            },
            {
                "role": "user",
                "content": f"标题：{title}\nURL：{url}\n网页原文：\n{excerpt}",
            },
        ]
        try:
            raw = self._request(messages, output_mode="json_object", schema_model=DateAssistPayload)
            payload = DateAssistPayload.model_validate(_extract_json(raw))
            return DateAssistResult(payload, raw)
        except Exception as exc:
            return DateAssistResult(None, locals().get("raw", ""), f"模型日期定位失败: {exc}")

    def suggest_review(
        self,
        *,
        title: str,
        source_name: str,
        original_url: str,
        published_date: str,
        date_status: str,
        date_evidence: str,
        content_type: str,
        content_type_evidence: str,
        text: str,
        authoritative_decision: str = "",
        authoritative_reasoning: str = "",
    ) -> SuggestionResult:
        prompt_prefix = (
            f"来源智库：{source_name}\n原始标题：{title}\n原文 URL：{original_url}\n"
            f"当前日期：{published_date or '未知'}（状态：{date_status}）\n日期证据：{date_evidence}\n"
            f"确定性体裁：{content_type}\n体裁证据：{content_type_evidence}\n"
            f"既有主题决定：{authoritative_decision or '无'}\n"
            f"既有主题理由：{authoritative_reasoning or '无'}\n\n"
            "提取正文：\n"
        )
        user_prompt = _bounded_prompt(
            prompt_prefix,
            text,
            self.config.max_model_input_chars,
        )
        messages = [
            {"role": "system", "content": SUGGESTION_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
        try:
            raw = self._request(messages, schema_model=ReviewSuggestionPayload)
            payload = ReviewSuggestionPayload.model_validate(_extract_json(raw))
            return SuggestionResult(payload, raw)
        except (httpx.HTTPError, KeyError, ValueError, ValidationError, RuntimeError) as first_error:
            repair = messages + [
                {
                    "role": "user",
                    "content": (
                        f"上一输出校验失败：{first_error}。decision 只能是 include、exclude、"
                        "needs_review；必须使用系统消息列出的原字段名，只重新输出符合 schema 的 JSON 对象。"
                    ),
                }
            ]
            try:
                raw = self._request(repair, schema_model=ReviewSuggestionPayload)
                payload = ReviewSuggestionPayload.model_validate(_extract_json(raw))
                return SuggestionResult(payload, raw)
            except Exception as second_error:
                return SuggestionResult(
                    None,
                    locals().get("raw", ""),
                    f"模型建议格式修复失败: {second_error}",
                )
