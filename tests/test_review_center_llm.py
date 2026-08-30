"""Unit tests for the review-center LLM foundation and constrained fusion."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_llm():
    name = "_test_review_llm"
    if name in sys.modules:
        return sys.modules[name]
    package = ROOT / "python" / "akquant" / "llm"
    spec = importlib.util.spec_from_file_location(
        name, package / "__init__.py", submodule_search_locations=[str(package)]
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


LLM = _load_llm()


def _submodule(name: str):
    import importlib

    return importlib.import_module(f"{LLM.__name__}.{name}")


def _llm_analysis(score: float = 100.0, probability: float = 1.0):
    schemas = _submodule("schemas")
    return schemas.LLMTradeAnalysis(
        assessment_status="valid_directional",
        unavailable_reason=None,
        score=score,
        subjective_up_probabilities={
            "next_trading_day": probability,
            "next_5_trading_days": probability,
        },
        confidence=1.0,
        trend_direction="上升",
        market_regime="偏强",
        market_effect="顺风",
        sector_strength="强",
        sector_effect="顺风",
        stance="支持",
        suggested_action="加仓",
        operation_advice="等待条件确认后执行。",
        facts_used=[],
        evidence=[],
        counter_evidence=[],
        inferences=[],
        knowledge_refs=[],
        invalidation_conditions=[],
        missing_information=[],
        requires_human_review=False,
    )


def test_unavailable_probability_is_null_not_half() -> None:
    traditional = _submodule("traditional")
    result = traditional.predict_all_horizons([10.0] * 20, [100.0] * 20)
    assert result["probabilities"]["next_trading_day"]["value"] is None
    assert result["probabilities"]["next_5_trading_days"]["value"] is None
    assert result["assessment_status"] == "insufficient_data"


def test_strict_provider_schema_requires_all_object_properties() -> None:
    """Strict Responses schemas include nullable fields in required arrays."""
    schemas = _submodule("schemas")
    provider = _submodule("provider")
    schema = provider._strict_json_schema(schemas.LLMTradeAnalysis.model_json_schema())
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["additionalProperties"] is False


def test_example_config_supports_responses_deepseek_and_qwen() -> None:
    """The editable provider registry covers both API compatibility styles."""
    config = _submodule("config").load_trade_ai_config(base_dir=ROOT)
    assert config.providers["codexapis"].api_style == "responses"
    assert config.providers["deepseek"].api_style == "chat_completions"
    assert config.providers["qwen"].api_style == "chat_completions"


def test_miaoxiang_uses_official_auth_header() -> None:
    """Choice 妙想 authentication uses em_api_key, not Bearer auth."""
    config_module = _submodule("config")
    client = _submodule("miaoxiang").MiaoxiangClient(
        config_module.MiaoxiangConfig(
            enabled=True,
            em_api_key="secret-placeholder",
        )
    )
    assert client._headers() == {"em_api_key": "secret-placeholder"}
    try:
        client.authorize_tool("unknown_write_or_read_tool")
    except PermissionError as exc:
        assert "白名单" in str(exc)
    else:
        raise AssertionError("unreviewed 妙想 tools must fail closed")


def test_fusion_caps_score_probability_and_action_step() -> None:
    fusion = _submodule("fusion")
    config = _submodule("config").FusionConfig()
    traditional = {
        "available": True,
        "assessment_status": "valid_directional",
        "selection_score": 60.0,
        "close": 10.0,
        "ma5": 9.8,
        "ma20": 9.5,
        "ma60": 9.0,
        "atr14": 0.3,
        "support_price": 9.5,
        "resistance_price": 10.5,
        "momentum20": 0.05,
        "trend_direction": "偏强",
        "probabilities": {
            "next_trading_day": {"value": 0.50},
            "next_5_trading_days": {"value": 0.50},
        },
    }
    result = fusion.fuse(traditional, _llm_analysis(), config)
    assert result["adjustments"]["score_delta"] <= 10.0
    assert result["final_up_probabilities"]["next_trading_day"]["value"] <= 0.58
    assert abs(result["adjustments"]["action_step_delta"]) <= 1
    assert result["price_plan"]["version"] == "short_swing_price_v1"


def test_hard_stop_cannot_be_downgraded_by_llm() -> None:
    """An 8% holding loss remains a full stop regardless of LLM optimism."""
    fusion = _submodule("fusion")
    traditional = {
        "available": True,
        "assessment_status": "valid_directional",
        "selection_score": 80.0,
        "close": 9.0,
        "ma5": 8.9,
        "ma20": 8.8,
        "ma60": 8.5,
        "atr14": 0.3,
        "support_price": 8.7,
        "resistance_price": 9.5,
        "momentum20": 0.05,
        "probabilities": {
            "next_trading_day": {"value": 0.7},
            "next_5_trading_days": {"value": 0.7},
        },
    }
    result = fusion.fuse(
        traditional,
        _llm_analysis(),
        _submodule("config").FusionConfig(),
        position={"quantity": 1000, "entry_price": 10.0},
    )
    assert result["final_action"] == "止损"
    assert result["plans"]["stop_loss"]["position"]["target_exit_ratio"] == 1.0
    assert result["vetoes"]


def test_knowledge_retrieval_keeps_five_fixed_rules() -> None:
    knowledge = _submodule("knowledge").KnowledgeBase(
        ROOT / "docs" / "zh" / "advanced" / "llm_trade_personal_knowledge.md"
    )
    result = knowledge.retrieve({"market": "缩量轮动", "position": "持仓"}, 4)
    assert len(result["fixed_rules"]) == 5
    assert len(result["retrieved_rules"]) == 4
    assert all(item["id"].startswith("RULE-") for item in result["retrieved_rules"])


def test_personal_knowledge_v2_retrieves_new_execution_rules() -> None:
    knowledge = _submodule("knowledge").KnowledgeBase(
        ROOT / "docs" / "zh" / "advanced" / "llm_trade_personal_knowledge.md"
    )
    assert knowledge.version == "2026-08-30.v2"
    card_ids = [card.card_id for card in knowledge.cards]
    assert len(card_ids) == len(set(card_ids))
    active_ids = {card.card_id for card in knowledge.cards if card.status == "active"}
    expected = {
        "RULE-ENTRY-105",
        "RULE-ENTRY-106",
        "RULE-SECTOR-106",
        "RULE-SECTOR-107",
        "RULE-POSITION-106",
        "RULE-MARKET-102",
        "RULE-EXEC-103",
    }
    assert expected <= active_ids

    open_rules = knowledge.retrieve(
        {
            "market_context": "缩量竞价抢筹，等待开盘方向",
            "sector_context": "中军迟迟不能封板，缺少增量资金",
            "stock_context": "水下靠近五日线，观察低吸",
        },
        12,
    )["retrieved_rules"]
    open_ids = {item["id"] for item in open_rules}
    assert "RULE-ENTRY-102" in open_ids
    assert "RULE-SECTOR-107" in open_ids
    assert "RULE-ENTRY-106" in open_ids

    holding_rules = knowledge.retrieve(
        {
            "portfolio_context": "长期被套且没有利润垫，反弹先减仓再等买点加回",
            "market_context": "极弱阴跌，观察次日反馈",
        },
        12,
    )["retrieved_rules"]
    holding_ids = {item["id"] for item in holding_rules}
    assert "RULE-POSITION-106" in holding_ids
    assert "RULE-MARKET-102" in holding_ids
    assert "RULE-MARKET-101" in holding_ids


def test_storage_persists_labels_and_performance(tmp_path: Path) -> None:
    storage = _submodule("storage").AnalysisStorage(
        tmp_path / "ai.sqlite3", tmp_path / "raw"
    )
    result = {
        "analysis_id": "ai-1",
        "as_of": "2026-01-01T15:00:00+08:00",
        "instrument": {"symbol": "000001", "current_price": 10.0},
        "traditional": {"close": 10.0},
        "fusion": {
            "assessment_status": "valid_directional",
            "final_action": "买入",
            "final_score": 70.0,
            "final_up_probabilities": {
                "next_trading_day": {"value": 0.6},
                "next_5_trading_days": {"value": 0.7},
            },
        },
        "audit": {},
    }
    storage.save(result, raw_request="{}", raw_response=json.dumps({"ok": True}))
    storage.label_outcome(
        "ai-1",
        [
            {"close": 10.2, "high": 10.3, "low": 9.9},
            {"close": 10.1, "high": 10.4, "low": 10.0},
            {"close": 10.4, "high": 10.5, "low": 10.1},
            {"close": 10.5, "high": 10.6, "low": 10.3},
            {"close": 10.8, "high": 10.9, "low": 10.4},
        ],
        stop_price=9.8,
    )
    report = storage.performance_report()
    assert storage.latest("000001")["analysis_id"] == "ai-1"
    assert report["next_trading_day"]["sample_count"] == 1
    assert report["next_5_trading_days"]["sample_count"] == 1


def test_model_release_approval_snapshot_and_rollback(tmp_path: Path) -> None:
    storage = _submodule("storage").AnalysisStorage(
        tmp_path / "release.sqlite3", tmp_path / "raw"
    )
    storage.register_model(
        "champion-v1",
        model_name="trend_swing",
        role="champion",
        status="active_paper",
        version="v1",
        metrics={"sharpe_ratio": 0.5, "max_drawdown_pct": -8.0},
    )
    storage.register_model(
        "challenger-v2",
        model_name="momentum_regime",
        role="challenger",
        status="evaluated",
        version="v2",
        metrics={
            "best": {
                "sharpe_ratio": 0.8,
                "max_drawdown_pct": -7.0,
                "trade_count": 20,
            },
            "completed_trial_count": 30,
            "cpcv_status": "valid",
            "cpcv": {
                "status": "valid",
                "summary": {
                    "mean_test_sharpe": 0.3,
                    "positive_test_fold_ratio": 0.7,
                },
            },
            "pbo": 0.25,
        },
    )
    request = storage.request_model_release("challenger-v2", note="candidate ready")
    assert request["gate"]["passed"] is True
    with pytest.raises(ValueError, match="已有待审批申请"):
        storage.request_model_release("challenger-v2")
    with pytest.raises(ValueError, match="只有 Challenger"):
        storage.request_model_release("champion-v1")
    assert storage.active_champion()["model_id"] == "champion-v1"
    with pytest.raises(ValueError, match="历史 Champion"):
        storage.rollback_model_release(target_model_id="challenger-v2")
    published = storage.approve_model_release(
        request["request_id"], approved_by="tester", note="approved"
    )
    assert published["previous_model_id"] == "champion-v1"
    assert storage.active_champion()["model_id"] == "challenger-v2"
    assert storage.get_model("challenger-v2")["version_snapshot"]
    rolled_back = storage.rollback_model_release(actor="tester", note="rollback")
    assert rolled_back["model_id"] == "champion-v1"
    assert storage.active_champion()["model_id"] == "champion-v1"
    assert [item["action"] for item in storage.list_model_releases()] == [
        "rollback",
        "publish",
    ]


def test_review_template_contains_selected_ai_analysis_and_all_headers() -> None:
    """The review-center table exposes the confirmed unified result columns."""
    path = ROOT / "python" / "akquant" / "lwc" / "_template.py"
    spec = importlib.util.spec_from_file_location("_review_template_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    text = module._HTML_TEMPLATE + module._APP_JS
    assert "分析选中" in text
    assert "显示最近365天" in text
    assert "观察”已隐藏" in text
    assert "展开选股细分理由 / LLM / 融合依据" in text
    assert "formatProbabilitySummary" in text
    assert "signal-detail-panel" in text
    assert "signal-card-list" in text
    assert "尚未对该股票运行 LLM 分析" in text


def test_review_center_links_to_model_release_management() -> None:
    text = (ROOT / "akquant_review_center.html").read_text(encoding="utf-8")
    assert 'href="model_management.html"' in text
    assert "模型发布" in text
    assert "模型输出" in text
    for header in (
        "池/持仓",
        "现价/收益",
        "次日概率",
        "5日概率",
        "融合信号",
        "操作建议",
        "大盘/板块",
        "仓位%/数量",
        "卖出比例/止损",
        "质量/冲突",
    ):
        assert header in text


def test_model_output_fallback_uses_visible_fields_only() -> None:
    schemas = _submodule("schemas")
    service = _submodule("service")
    analysis = schemas.LLMTradeAnalysis(
        assessment_status="valid_directional",
        score=60,
        subjective_up_probabilities={
            "next_trading_day": 0.52,
            "next_5_trading_days": 0.55,
        },
        confidence=0.6,
        trend_direction="偏强",
        market_regime="震荡",
        market_effect="中性",
        sector_strength="未知",
        sector_effect="未知",
        stance="谨慎持有",
        suggested_action="持有",
        operation_advice="等待量能确认。",
        facts_used=[],
        evidence=[],
        counter_evidence=[
            schemas.Evidence(type="counter", reference="x", summary="量能不足")
        ],
        inferences=[],
        knowledge_refs=[],
        invalidation_conditions=["跌破支撑位"],
        missing_information=[],
        requires_human_review=False,
    )
    result = service._ensure_model_output(analysis)
    assert result.model_output
    assert "量能不足" in result.model_output
    assert "reasoning_content" not in result.model_output
