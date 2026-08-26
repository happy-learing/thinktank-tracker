from __future__ import annotations

import json
import re
from dataclasses import dataclass

import httpx
from pydantic import ValidationError

from .config import AppConfig, get_api_key
from .schemas import AssessmentPayload


SYSTEM_PROMPT = """你是严谨的国际战略研究编辑。只根据给出的文章全文判断，不把模型判断当成日期、体裁或篇幅证据。
纳入范围：宏观层面的国际关系、大国竞争、大国战略、国际思潮/意识形态/秩序之争；或微观八类：关键基础设施、科技发展竞争、关键矿产资源、具体地缘政治竞争、粮食能源等关键战略物资、国际政治经贸规则制度博弈、具有战略影响力的跨国企业、战略支点国家。
排除：与大国竞争或国际战略无直接关联的小地区一般研究、性别、儿童、教育、国内微观经济、城市建设，以及新闻、短评、活动、播客或视频简介。
同一文章只能选择一个 primary_category；其他相关类只放 secondary_categories。输出必须符合 JSON Schema。core_claim_zh 必须是文章本身的核心结论，不是主题复述。证据最多三条，每条为简短原文证据片段。"""


@dataclass
class ModelResult:
    payload: AssessmentPayload | None
    raw_response: str = ""
    error: str = ""


def _endpoint(base_url: str, style: str) -> str:
    base = base_url.rstrip("/")
    leaf = "responses" if style == "responses" else "chat/completions"
    return base if base.endswith(f"/{leaf}") else f"{base}/{leaf}"


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

    def _request(self, messages: list[dict[str, str]], strict: bool = True) -> str:
        api_key = get_api_key()
        if not api_key:
            raise RuntimeError("尚未在 Windows Credential Manager 中配置模型 API 密钥")
        schema = AssessmentPayload.model_json_schema()
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        if self.config.api_style == "responses":
            payload: dict = {
                "model": self.config.model_name,
                "input": messages,
            }
            if strict:
                payload["text"] = {
                    "format": {"type": "json_schema", "name": "assessment", "strict": True, "schema": schema}
                }
        else:
            payload = {"model": self.config.model_name, "messages": messages, "temperature": 0}
            if strict:
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "assessment", "strict": True, "schema": schema},
                }
        with httpx.Client(timeout=self.config.request_timeout_seconds) as client:
            response = client.post(_endpoint(self.config.api_base_url, self.config.api_style), headers=headers, json=payload)
            # Some compatible providers implement JSON mode but reject json_schema.
            if response.status_code in {400, 404, 422} and strict:
                return self._request(messages, strict=False)
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
        return str(data["choices"][0]["message"]["content"])

    def assess(self, title: str, source_name: str, content_type: str, text: str) -> ModelResult:
        clipped = text[: self.config.max_model_input_chars]
        user_prompt = (
            f"来源智库：{source_name}\n标题：{title}\n确定性体裁：{content_type}\n"
            f"以下为提取的文章全文（可能因长度上限截断）：\n\n{clipped}"
        )
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
        try:
            raw = self._request(messages)
            payload = AssessmentPayload.model_validate(_extract_json(raw))
            return ModelResult(payload, raw)
        except (httpx.HTTPError, KeyError, ValueError, ValidationError, RuntimeError) as first_error:
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
