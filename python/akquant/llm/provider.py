"""OpenAI-compatible Responses API adapter with strict structured output."""

from __future__ import annotations

import json
import time
from typing import Any

from .config import ProviderConfig, RequestConfig
from .schemas import LLMTradeAnalysis, ProviderResult, ProviderUsage


class ResponsesProvider:
    def __init__(self, provider: ProviderConfig, request: RequestConfig) -> None:
        self.provider = provider
        self.request = request

    def analyze(
        self, *, instructions: str, input_text: str, cache_key: str | None = None
    ) -> ProviderResult:
        if not self.provider.ready:
            return ProviderResult(
                analysis=LLMTradeAnalysis.unavailable("未配置模型 API Key"),
                raw_text="",
                latency_ms=0,
            )
        from openai import OpenAI

        client = OpenAI(
            api_key=self.provider.sk,
            base_url=self.provider.url.rstrip("/"),
            timeout=self.request.timeout_seconds,
            max_retries=self.request.max_retries,
        )
        started = time.perf_counter()
        schema = _strict_json_schema(LLMTradeAnalysis.model_json_schema())
        if self.provider.api_style == "chat_completions":
            return self._analyze_chat(
                client=client,
                instructions=instructions,
                input_text=input_text,
                schema=schema,
                started=started,
            )
        request_arguments = dict(
            model=self.provider.model,
            instructions=instructions,
            input=input_text,
            temperature=self.request.temperature,
            max_output_tokens=self.request.max_output_tokens,
            prompt_cache_key=cache_key,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "llm_trade_analysis",
                    "schema": schema,
                    "strict": True,
                }
            },
        )
        if self.request.stream:
            with client.responses.stream(**request_arguments) as stream:
                response = stream.get_final_response()
        else:
            response = client.responses.create(**request_arguments)
        raw_text = str(getattr(response, "output_text", "") or "")
        analysis = LLMTradeAnalysis.model_validate_json(raw_text)
        usage = getattr(response, "usage", None)
        input_details = getattr(usage, "input_tokens_details", None)
        return ProviderResult(
            analysis=analysis,
            usage=ProviderUsage(
                input_tokens=getattr(usage, "input_tokens", None),
                output_tokens=getattr(usage, "output_tokens", None),
                cached_tokens=getattr(input_details, "cached_tokens", None),
            ),
            response_id=getattr(response, "id", None),
            raw_text=raw_text,
            latency_ms=round((time.perf_counter() - started) * 1000),
        )

    def _analyze_chat(
        self,
        *,
        client: Any,
        instructions: str,
        input_text: str,
        schema: dict[str, Any],
        started: float,
    ) -> ProviderResult:
        """Use the OpenAI-compatible Chat Completions surface for DS/Qwen."""
        schema_text = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        request_arguments: dict[str, Any] = {
            "model": self.provider.model,
            "messages": [
                {"role": "system", "content": instructions},
                {
                    "role": "user",
                    "content": (
                        f"{input_text}\n<output_json_schema>{schema_text}"
                        "</output_json_schema>"
                    ),
                },
            ],
            "temperature": self.request.temperature,
            "max_tokens": self.request.max_output_tokens,
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        # DeepSeek v4 models enable thinking by default.  The reasoning tokens
        # are not part of the structured JSON result and can consume the whole
        # output budget, leaving message.content empty.  Disable thinking for
        # this compact, schema-constrained single-turn analysis.  Do not send
        # the vendor-specific extension to unrelated OpenAI-compatible APIs.
        if "deepseek" in self.provider.url.lower() or self.provider.model.lower().startswith(
            "deepseek"
        ):
            request_arguments["extra_body"] = {"thinking": {"type": "disabled"}}
        response = client.chat.completions.create(**request_arguments)
        raw_text = str(response.choices[0].message.content or "")
        analysis = LLMTradeAnalysis.model_validate_json(raw_text)
        usage = getattr(response, "usage", None)
        prompt_details = getattr(usage, "prompt_tokens_details", None)
        return ProviderResult(
            analysis=analysis,
            usage=ProviderUsage(
                input_tokens=getattr(usage, "prompt_tokens", None),
                output_tokens=getattr(usage, "completion_tokens", None),
                cached_tokens=getattr(prompt_details, "cached_tokens", None),
            ),
            response_id=getattr(response, "id", None),
            raw_text=raw_text,
            latency_ms=round((time.perf_counter() - started) * 1000),
        )


def _strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Make every object property required as mandated by strict Responses schemas."""
    copied = json.loads(json.dumps(schema))

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict):
                node["required"] = list(properties)
                node["additionalProperties"] = False
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(copied)
    return copied


def safe_provider_error(exc: Exception) -> LLMTradeAnalysis:
    """Return a user-safe failure without exposing URLs, headers, or secrets."""
    reason = type(exc).__name__
    # Pydantic validation failures are safe to summarize by field path.  This
    # turns an opaque ``ValidationError`` into an actionable audit message
    # without persisting the model response or any credentials.
    errors_method = getattr(exc, "errors", None)
    if callable(errors_method):
        try:
            details = []
            for item in list(errors_method())[:3]:
                location = ".".join(str(part) for part in item.get("loc", ()))
                message = str(item.get("msg") or "invalid")[:100]
                details.append(f"{location or 'root'}: {message}")
            if details:
                reason = "字段校验失败（" + "; ".join(details) + "）"
        except Exception:  # noqa: BLE001 - diagnostics must never mask fallback
            pass
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        reason = str(body.get("message") or body.get("code") or reason)
    try:
        parsed = json.loads(str(exc))
        reason = str(parsed.get("error", {}).get("message") or reason)
    except (ValueError, TypeError, AttributeError):
        pass
    return LLMTradeAnalysis.unavailable(f"模型调用失败: {reason[:160]}")
