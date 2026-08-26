"""High-level orchestration for selected-symbol LLM analysis."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .config import TradeAIConfig, load_trade_ai_config
from .fusion import fuse
from .knowledge import KnowledgeBase
from .prompts import PromptTemplate
from .provider import ResponsesProvider, safe_provider_error
from .schemas import LLMTradeAnalysis, NextDayScenario, ProviderResult
from .storage import AnalysisStorage


class TradeAnalysisService:
    def __init__(self, config: TradeAIConfig | None = None) -> None:
        self.config = config or load_trade_ai_config()
        self.prompt = PromptTemplate.load(
            self.config.base_dir / "docs/zh/advanced/llm_trade_prompt_template.md"
        )
        self.knowledge = KnowledgeBase(
            self.config.resolve_path(self.config.knowledge.path)
        )
        self.provider = ResponsesProvider(self.config.provider, self.config.request)
        self.storage = AnalysisStorage(
            self.config.resolve_path(self.config.storage.database_path),
            self.config.resolve_path(self.config.storage.raw_directory),
        )

    def status(self) -> dict[str, Any]:
        provider = self.config.provider
        return {
            "enabled": provider.enabled,
            "ready": provider.ready,
            "provider": self.config.active_provider,
            "provider_url": provider.url,
            "model": provider.model,
            "missing": []
            if provider.ready
            else ["providers.%s.sk" % self.config.active_provider],
            "config_path": str(self.config.config_path),
            "prompt_version": self.prompt.version,
            "prompt_sha256": self.prompt.sha256,
            "knowledge_version": self.knowledge.version,
            "knowledge_sha256": self.knowledge.sha256,
            "miaoxiang_ready": bool(
                self.config.miaoxiang.enabled and self.config.miaoxiang.em_api_key
            ),
        }

    def analyze(
        self, context: dict[str, Any], *, account_equity: float = 100000.0, persist: bool = True
    ) -> dict[str, Any]:
        instrument = dict(context.get("instrument") or {})
        symbol = str(instrument.get("symbol") or "")
        if not symbol:
            raise ValueError("缺少 instrument.symbol")
        traditional = dict(context.get("traditional") or {})
        as_of = str(context.get("as_of") or datetime.now(timezone.utc).isoformat())
        knowledge = self.knowledge.retrieve(
            context, self.config.knowledge.max_retrieved_rules
        )
        provider_context = {
            "schema_version": "1.3",
            "analysis_as_of": as_of,
            "prediction_horizons": self.config.strategy.prediction_horizons,
            "data_quality": context.get("data_quality") or {},
            "market_context": context.get("market_context") or {},
            "sector_context": context.get("sector_context") or {},
            "portfolio_context": context.get("portfolio_context") or {},
            "stock_context": _compact_stock_context(
                context.get("stock_context") or instrument
            ),
            "traditional_analysis": _compact_traditional(traditional),
            "retrieved_trading_knowledge": knowledge,
            "external_events": context.get("external_events") or [],
        }
        request_text = self.prompt.dynamic_input(provider_context)
        schema_text = json.dumps(
            LLMTradeAnalysis.model_json_schema(),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        # Keep the schema in the dynamic request as well as provider metadata:
        # this makes the exact model input auditable and helps providers that
        # only implement JSON mode understand the contract.
        request_text += f"\n\n<output_schema>{schema_text}</output_schema>"
        if self.config.provider.ready:
            try:
                provider_result = self.provider.analyze(
                    instructions=self.prompt.instructions,
                    input_text=request_text,
                    cache_key=(
                        f"akquant-review:{self.prompt.sha256[:16]}:"
                        f"{self.knowledge.version}:{self.config.provider.model}"
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                provider_result = ProviderResult(
                    analysis=safe_provider_error(exc), raw_text="", latency_ms=0
                )
        else:
            provider_result = ProviderResult(
                analysis=LLMTradeAnalysis.unavailable("未配置模型 API Key"),
                raw_text="",
                latency_ms=0,
            )
        provider_result.analysis = _ensure_model_output(provider_result.analysis)
        provider_result.analysis = _normalize_existing_breaks(
            provider_result.analysis, context
        )
        provider_result.analysis = _ensure_next_day_scenario(
            provider_result.analysis, context
        )
        data_quality = (
            float((context.get("data_quality") or {}).get("score", 100)) / 100
        )
        fusion = fuse(
            traditional,
            provider_result.analysis,
            self.config.fusion,
            position=instrument.get("position"),
            account_equity=account_equity,
            data_quality=data_quality,
        )
        analysis_id = AnalysisStorage.content_id(symbol, as_of, provider_context)
        result = {
            "schema_version": "1.3",
            "analysis_id": analysis_id,
            "as_of": as_of,
            "strategy_style": self.config.strategy.style,
            "analysis_schedule": self.config.strategy.analysis_schedule,
            "selection_mode": self.config.strategy.selection_mode,
            "prediction_horizons": self.config.strategy.prediction_horizons,
            "instrument": instrument,
            "data_quality": context.get("data_quality") or {},
            "market_context": context.get("market_context") or {},
            "sector_context": context.get("sector_context") or {},
            "portfolio_context": context.get("portfolio_context") or {},
            "stock_context": context.get("stock_context") or {},
            "traditional": traditional,
            "llm": provider_result.analysis.model_dump(mode="json"),
            "fusion": fusion,
            "plans": fusion["plans"],
            "audit": {
                "prompt_version": self.prompt.version,
                "prompt_sha256": self.prompt.sha256,
                "knowledge_version": self.knowledge.version,
                "knowledge_sha256": self.knowledge.sha256,
                "traditional_strategy_version": "review_center_momentum_logit_dual_horizon_v2",
                "provider": self.config.active_provider,
                "provider_model": self.config.provider.model,
                "response_id": provider_result.response_id,
                "latency_ms": provider_result.latency_ms,
                "usage": provider_result.usage.model_dump(),
                "prompt_instructions": self.prompt.instructions,
                "prompt_process": self.prompt.process,
                "prompt_output_requirements": self.prompt.output_requirements,
                "prompt_template_sha256": self.prompt.sha256,
                "output_schema_sha256": hashlib.sha256(schema_text.encode("utf-8")).hexdigest(),
                "output_schema": json.loads(schema_text),
            },
        }
        result["risk"] = fusion.get("risk") or {}
        if persist:
            self.storage.save(
                result,
                raw_request=json.dumps(
                {
                    "instructions": self.prompt.instructions,
                    "process": self.prompt.process,
                    "output_requirements": self.prompt.output_requirements,
                    "dynamic_input": request_text,
                    "output_schema": json.loads(schema_text),
                },
                ensure_ascii=False,
                    separators=(",", ":"),
                ),
                raw_response=provider_result.raw_text,
                usage=provider_result.usage.model_dump(),
                oversized_token_threshold=self.config.storage.oversized_token_threshold,
            )
            self.storage.retention_cleanup(
                normal_days=self.config.storage.raw_normal_days,
                oversized_days=self.config.storage.raw_oversized_days,
            )
        return result

    def replay(self, analysis_id: str, *, persist: bool = False) -> dict[str, Any]:
        """Re-run a historical request with the current prompt/provider.

        Historical raw requests are immutable snapshots.  Replay is opt-in and
        does not publish or alter fusion parameters; it is intended for
        walk-forward comparison and manual review.
        """
        original = self.storage.get(analysis_id)
        if not original or not original.get("_raw_path"):
            return {"status": "unavailable", "reason": "找不到带原始输入的历史分析", "analysis_id": analysis_id}
        try:
            raw = json.loads(Path(original["_raw_path"]).read_text(encoding="utf-8"))
            envelope = json.loads(raw.get("request") or "{}")
            dynamic = str(envelope.get("dynamic_input") or "")
            match = re.search(r"<analysis_context>(.*)</analysis_context>", dynamic, flags=re.S)
            if not match:
                raise ValueError("历史请求缺少 analysis_context")
            context = json.loads(match.group(1))
        except Exception as exc:  # noqa: BLE001
            return {"status": "unavailable", "reason": f"历史输入解析失败: {type(exc).__name__}", "analysis_id": analysis_id}
        replayed = self.analyze(
            context,
            account_equity=float((context.get("portfolio_context") or {}).get("account_equity") or 100000.0),
            persist=persist,
        )
        return {"status": "valid", "replay_of": analysis_id, "result": replayed}


def _ensure_model_output(analysis: LLMTradeAnalysis) -> LLMTradeAnalysis:
    """Provide a concise user-facing summary when a provider omits the field.

    Some Chat Completions-compatible providers honor JSON mode but do not
    reliably populate every nullable field from the appended schema.  The
    fallback is assembled only from already validated, visible fields; it
    never exposes ``reasoning_content`` or any hidden chain-of-thought.
    """
    if analysis.assessment_status == "unavailable" or (analysis.model_output or "").strip():
        return analysis
    parts: list[str] = ["模型摘要："]
    if analysis.trend_direction and analysis.trend_direction != "未知":
        parts.append(f"趋势{analysis.trend_direction}；")
    if analysis.stance:
        parts.append(f"立场为“{analysis.stance}”。")
    if analysis.operation_advice:
        parts.append(f"操作上，{analysis.operation_advice}")
    risks = [item.summary for item in analysis.counter_evidence if item.summary]
    if risks:
        parts.append(f"主要反向证据：{'；'.join(risks[:2])}。")
    if analysis.invalidation_conditions:
        parts.append(f"失效条件：{analysis.invalidation_conditions[0]}")
    summary = "".join(parts).strip()
    return analysis.model_copy(update={"model_output": summary[:600]})


def _normalize_existing_breaks(
    analysis: LLMTradeAnalysis, context: dict[str, Any]
) -> LLMTradeAnalysis:
    """Prevent an already-broken MA60 condition being shown as a new trigger."""
    technical = dict((context.get("traditional") or {}))
    close = float(technical.get("close") or 0)
    ma60 = float(technical.get("ma60") or 0)
    if not (close > 0 and ma60 > 0 and close < ma60):
        return analysis
    replacements = (
        ("若跌破MA60", "当前已在MA60下方，关注重新站回后再次失守"),
        ("跌破MA60则清仓", "MA60下方属于当前中期弱势，只有重新站回后再次失守才作为清仓触发"),
        ("跌破 MA60", "当前位于 MA60 下方（非新的跌破触发）"),
    )
    advice = analysis.operation_advice or ""
    changed_advice = advice
    for old, new in replacements:
        changed_advice = changed_advice.replace(old, new)
    changed_conditions = []
    for condition in analysis.invalidation_conditions:
        value = condition
        for old, new in replacements:
            value = value.replace(old, new)
        changed_conditions.append(value)
    if changed_advice == advice and changed_conditions == analysis.invalidation_conditions:
        return analysis
    return analysis.model_copy(
        update={
            "operation_advice": changed_advice,
            "invalidation_conditions": changed_conditions,
        }
    )


def _ensure_next_day_scenario(
    analysis: LLMTradeAnalysis, context: dict[str, Any]
) -> LLMTradeAnalysis:
    scenario = analysis.next_day_scenario
    technical = dict(context.get("traditional") or {})
    close = float(technical.get("close") or 0)
    ma5 = float(technical.get("ma5") or 0)
    ma20 = float(technical.get("ma20") or 0)
    support = float(technical.get("support_price") or ma20 or 0)
    resistance = float(technical.get("resistance_price") or 0)
    if not scenario.base_case:
        scenario = NextDayScenario(
            base_case=f"围绕 MA5 {ma5:.2f} 与 MA20 {ma20:.2f} 震荡，先观察承接和量能。" if ma5 and ma20 else "先观察开盘方向、量能和分时承接。",
            bullish_case=f"放量站稳 {max(close, ma5):.2f}，并向压力位 {resistance:.2f} 推进。" if resistance else "放量站稳短期均线，板块同步走强。",
            bearish_case=f"跌破支撑 {support:.2f} 且无法快速收回，弱势风险扩大。" if support else "跌破关键支撑且量能放大。",
            confirmation_signals=["分时价格站稳均线", "量能与板块同步改善"],
            invalidation_signals=[f"跌破支撑 {support:.2f}" if support else "跌破关键支撑", "板块核心同步走弱"],
            open_plan="高开先观察回踩承接，不追；平开或低开重点看支撑能否快速收回。",
            hold_plan="已有仓位以支撑、分时承接和板块共振决定继续持有或减仓。",
            exit_plan=f"跌破 {support:.2f} 且放量、无法收回时按风险计划减仓或退出。" if support else "破位且无法收回时执行减仓或退出。",
        )
    # Keep the machine-readable scenario as the single canonical forecast.
    # Some models echo it inside operation_advice; remove that echoed suffix so
    # the UI can render one merged, consistently formatted section.
    advice = re.split(r"\s*明日(?:走势)?预案\s*[:：]", analysis.operation_advice or "", maxsplit=1)[0].strip()
    return analysis.model_copy(
        update={"next_day_scenario": scenario, "operation_advice": advice[:800]}
    )


def _compact_traditional(traditional: dict[str, Any]) -> dict[str, Any]:
    probabilities: dict[str, Any] = {}
    metric_names = (
        "sample_count",
        "accuracy",
        "brier_score",
        "auc",
        "precision",
        "recall",
        "calibrated",
        "calibrated_brier_score",
    )
    for horizon, result in (traditional.get("probabilities") or {}).items():
        validation = result.get("validation") or {}
        probabilities[horizon] = {
            "value": result.get("value"),
            "valid": result.get("valid"),
            "assessment_status": result.get("assessment_status"),
            "training_samples": result.get("training_samples"),
            "unavailable_reason": result.get("unavailable_reason"),
            "validation": {
                name: validation.get(name)
                for name in metric_names
                if name in validation
            },
        }
    fields = (
        "strategy_id",
        "strategy_version",
        "threshold_version",
        "assessment_status",
        "unavailable_reason",
        "selection_score",
        "trend",
        "trend_direction",
        "close",
        "ma5",
        "ma20",
        "ma60",
        "momentum20",
        "momentum60",
        "rsi14",
        "volume_ratio",
        "atr14",
        "support_price",
        "resistance_price",
        "action",
        "trigger",
        "reason",
        "evaluation",
        "rules",
        "decision",
    )
    compact = {name: traditional.get(name) for name in fields if name in traditional}
    compact["probabilities"] = probabilities
    return compact


def _compact_stock_context(stock_context: dict[str, Any]) -> dict[str, Any]:
    compact = dict(stock_context)
    technical = compact.get("technical")
    if isinstance(technical, dict):
        compact["technical"] = _compact_traditional(technical)
    return compact
