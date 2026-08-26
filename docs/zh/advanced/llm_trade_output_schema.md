# 传统逻辑、大模型与融合结果的统一输出结构

> 状态：Schema 1.3 已接入代码；本文同时作为可编辑字段与业务定义基准。

## 1. 设计原则

1. 同时保留传统、大模型、融合三层结果。
2. 页面展示与审计存档使用同一结构。
3. 大模型只产生 `llm` 部分；`fusion` 和硬风控由本地确定性程序生成。
4. 未经联合校准，不把传统概率和大模型主观概率简单平均。
5. 每个交易动作是条件计划，不是立即下单命令。
6. 所有结果必须带数据、策略、Prompt、知识和模型版本。
7. 所有策略统一服务于 A 股短线趋势波段；预测周期和标签版本必须明确。
8. “有效中性”“未知”“数据不足”“模型不可用”是不同状态，不得用同一个 0.5 或“观望”代替。

## 2. 顶层结构

```json
{
  "schema_version": "1.2",
  "analysis_id": "string",
  "as_of": "ISO-8601",
  "strategy_style": "a_share_short_term_trend_swing",
  "analysis_schedule": "after_daily_close",
  "selection_mode": "user_selected_pool",
  "prediction_horizons": ["next_trading_day", "next_5_trading_days"],
  "instrument": {},
  "data_quality": {},
  "traditional": {},
  "llm": {},
  "fusion": {},
  "plans": {},
  "risk": {},
  "audit": {}
}
```

## 3. 字段定义

### 3.0 risk（顶层风险结构）

```json
{
  "risk_status": "valid",
  "risk_level": "medium",
  "risk_factors": [],
  "invalidation_conditions": [],
  "stop_loss_enabled": true,
  "stop_loss_price": 12.10,
  "stop_loss_exit_ratio": 1.0,
  "clear_enabled": true,
  "clear_exit_ratio": 1.0,
  "human_review_required": false
}
```

`risk_status=unavailable` 表示风险数据或模型不可用，不能解释为低风险；止损和清仓退出比例固定为 1.0。

### 3.1 instrument

```json
{
  "symbol": "000001",
  "name": "示例股票",
  "market": "A股",
  "pool": "持仓",
  "sector_ids": [],
  "current_price": 12.10,
  "position": {
    "quantity": 1000,
    "available_quantity": null,
    "entry_price": 12.50,
    "return": -0.032,
    "portfolio_weight": null
  }
}
```

`pool` 建议枚举：`观察`、`持仓`、`候选`。

### 3.2 data_quality

```json
{
  "status": "complete",
  "score": 92,
  "stale": false,
  "model_fallback_probability": false,
  "missing_fields": [],
  "conflicts": [],
  "source_timestamps": {
    "stock": "ISO-8601",
    "market": "ISO-8601",
    "sector": "ISO-8601",
    "external_events": "ISO-8601"
  }
}
```

`status` 建议枚举：`complete`、`partial`、`stale`、`invalid`。

### 3.3 traditional

```json
{
  "strategy_id": "review_center_momentum_logit_v1",
  "strategy_version": "string",
  "threshold_version": "string",
  "assessment_status": "valid_directional",
  "unavailable_reason": null,
  "selection_rank": 1,
  "score": 82.0,
  "score_components": [
    {
      "name": "momentum20",
      "value": 0.12,
      "contribution": 18.0
    }
  ],
  "probabilities": {
    "next_trading_day": {
      "value": 0.66,
      "valid": true,
      "validation": {
        "method": "walk_forward",
        "sample_count": 179,
        "positive_rate": 0.52,
        "accuracy": 0.58,
        "brier_score": 0.238,
        "auc": 0.61,
        "precision": 0.59,
        "recall": 0.55,
        "decision_threshold": 0.54,
        "calibrated": true,
        "calibration_method": "platt",
        "evaluation_start": "ISO-8601 date",
        "evaluation_end": "ISO-8601 date"
      }
    },
    "next_5_trading_days": {
      "value": 0.63,
      "valid": true,
      "validation": {}
    }
  },
  "trend": "多头排列",
  "trend_direction": "上升",
  "action": "强势买入",
  "triggers": [],
  "support_price": 110.0,
  "resistance_price": 125.0,
  "stop_price": 112.0,
  "take_profit_price": 132.0
}
```

`assessment_status` 统一建议枚举：`valid_directional`、`valid_neutral`、`insufficient_data`、`unavailable`、`degraded`。

某周期的 `valid=false` 时，该周期 `value` 必须为 `null`，并说明是依赖缺失、样本不足、标签单一还是训练异常。禁止再用 `0.5` 作为不可用回退。`valid_neutral` 表示模型有效且概率/证据确实落入版本化的中性区间。

Brier Score、AUC、Precision/Recall 和概率校准字段在样本不足或数学上不可计算时使用 `null`，同时输出原因，不能填 0。

### 3.4 llm

该部分由大模型生成并通过 Schema 校验。

```json
{
  "assessment_status": "valid_directional",
  "unavailable_reason": null,
  "score": 76.0,
  "subjective_up_probabilities": {
    "next_trading_day": 0.61,
    "next_5_trading_days": 0.64
  },
  "confidence": 0.72,
  "trend_direction": "偏强",
  "market_regime": "震荡偏强",
  "market_effect": "顺风",
  "sector_strength": "强",
  "sector_effect": "顺风",
  "stance": "支持但等待回踩",
  "suggested_action": "等待买入",
  "operation_advice": "等待回踩支撑并确认承接后再评估。",
  "model_output": "模型摘要：技术结构偏强但量能不足；大盘顺风，板块数据未知。当前以持仓保护和支撑确认优先，跌破失效位执行风控。",
  "next_day_scenario": {
    "base_case": "围绕短期均线震荡，先观察承接。",
    "bullish_case": "放量站稳关键价并获得板块共振。",
    "bearish_case": "跌破支撑且无法快速收回。",
    "confirmation_signals": [],
    "invalidation_signals": [],
    "open_plan": "高开不追，等待回踩确认。",
    "hold_plan": "已有仓按支撑和板块强度管理。",
    "exit_plan": "破位并放量时执行减仓或退出。"
  },
  "facts_used": [],
  "evidence": [
    {
      "type": "support",
      "reference": "stock.technical.ma20",
      "summary": "现价位于 MA20 之上"
    }
  ],
  "counter_evidence": [],
  "inferences": [],
  "knowledge_refs": [],
  "invalidation_conditions": [],
  "missing_information": [],
  "requires_human_review": false
}
```

`market_effect` / `sector_effect` 建议枚举：`顺风`、`中性`、`逆风`、`未知`。

`suggested_action` 建议枚举：

- `观察`；
- `等待买入`；
- `买入`；
- `加仓`；
- `持有`；
- `减仓`；
- `卖出`；
- `止损`；
- `清仓`。

当 `assessment_status` 为 `insufficient_data` 或 `unavailable` 时，`suggested_action` / `final_action` 使用 `null`，原因写入状态和 `unavailable_reason`，不创建替代交易动作。

### 3.5 fusion

该部分由本地融合引擎生成，不由 LLM 自由填写。

```json
{
  "mode": "rule_adjustment",
  "assessment_status": "valid_directional",
  "final_score": 80.7,
  "final_up_probabilities": {
    "next_trading_day": {
      "value": 0.649,
      "source": "rule_adjusted",
      "calibrated": false
    },
    "next_5_trading_days": {
      "value": 0.632,
      "source": "rule_adjusted",
      "calibrated": false
    }
  },
  "weights": {
    "traditional": 0.78,
    "llm": 0.22
  },
  "adjustments": {
    "score_delta": -1.3,
    "next_day_probability_delta": -0.011,
    "next_5_days_probability_delta": 0.002,
    "action_step_delta": -1
  },
  "final_confidence": 0.64,
  "final_trend_direction": "偏强",
  "final_action": "等待买入",
  "conflict_level": "minor",
  "conflict_reasons": [],
  "applied_rules": [],
  "vetoes": [],
  "human_review_required": false,
  "summary": "传统信号偏强，大模型支持方向但建议等待回踩确认。"
}
```

`mode` 建议枚举：

- `shadow`：大模型不改变传统结果；
- `rule_adjustment`：大模型按有限规则修正；
- `calibrated_fusion`：经样本外验证的联合融合。

本项目首版固定使用 `rule_adjustment`；`shadow` 仅作为 Schema 兼容/诊断枚举保留，不作为默认运行模式。

每个周期的 `source` 必须明确，例如：

- `traditional_uncalibrated`；
- `traditional_calibrated`；
- `rule_adjusted`；
- `joint_calibrated`；
- `unavailable`。

### 3.6 plans

所有计划使用相同结构：

```json
{
  "buy": {
    "enabled": true,
    "priority": 1,
    "price_zone": {
      "mode": "pullback_support",
      "basis": ["ma5", "ma20", "breakout_level"],
      "anchor_price": 47.6,
      "lower": 47.0,
      "upper": 48.2,
      "allowed_deviation": 0.6,
      "max_chase_price": 48.5,
      "price_plan_version": "short_swing_price_v1"
    },
    "trigger_conditions": [
      "回踩支撑区后收盘重新站稳",
      "成交量不显著放大下跌"
    ],
    "trend_confirmation": [
      "MA20 不向下加速",
      "板块强度不转为逆风"
    ],
    "invalidation_conditions": [
      "有效跌破指定支撑",
      "市场进入高风险状态"
    ],
    "position": {
      "target_weight": 0.10,
      "max_weight": 0.15,
      "quantity": 2000,
      "target_exit_ratio": null,
      "quantity_rounding": "floor_to_100_shares",
      "sizing_basis": "account_equity_and_risk_budget"
    },
    "notes": []
  },
  "add": {},
  "hold": {},
  "reduce": {},
  "sell": {},
  "stop_loss": {},
  "clear": {}
}
```

动作含义建议：

| 动作 | 含义 |
| --- | --- |
| 买入 | 空仓建立正常仓位 |
| 加仓 | 已有持仓且趋势/风险收益继续改善 |
| 持有 | 不新增风险，继续持仓 |
| 减仓 | 部分降低仓位，不代表逻辑完全失效 |
| 卖出 | 基于目标、趋势或再平衡的计划退出，允许部分卖出，但必须填写明确比例/数量 |
| 止损 | 价格/逻辑失效后的风险退出，用户口语中的“割肉”归入此类；卖出 100% 可用持仓 |
| 清仓 | 非止损原因下持仓逻辑完全失效或风险禁止继续持有；卖出 100% 可用持仓 |

`stop_loss.position.target_exit_ratio` 和 `clear.position.target_exit_ratio` 固定为 `1.0`。普通 `sell` 允许 `(0, 1]`，由实际仓位、浮盈亏、趋势破坏程度、市场/板块风险、流动性和可用数量动态推荐，再由本地交易单位与风控校正；无法给出明确比例时需要人工确认，不能输出模糊的“部分”。价格区间按下述版本化规则计算。

止损和清仓允许同时为 `enabled=true`，因为两者可以表达不同触发路径；例如价格跌破结构触发止损，重大风险或持仓逻辑完全失效触发清仓。页面当前动作只选择已经实际触发且优先级最高的一项，不能因为两个计划同时存在而重复计算卖出数量。

买入、加仓、减仓、卖出、止损和清仓在启用时必须同时输出：

- `target_weight` / `target_exit_ratio`：占账户权益或当前持仓的比例；
- `quantity`：按当时价格、可用资金/持仓和 A 股 100 股交易单位向下取整后的股数；
- 计算依据和受哪些账户/风险约束截断。

### 3.6.1 短线趋势波段价格区间规则

价格计划使用本地确定性规则 `short_swing_price_v1` 计算，LLM 只能选择形态、提供证据或建议收紧风险，不能任意改写公式。初始参数如下，后续通过 Walk-Forward 回测调整：

| 场景 | 锚点与区间 | 初始允许偏差 |
| --- | --- | --- |
| 回踩买入 | 从 MA5、MA20、最近有效突破位中选择最接近且未失效的支撑作为锚点 | 下沿 `锚点 - min(0.35×ATR14, 锚点×1.2%)`；上沿 `锚点 + min(0.25×ATR14, 锚点×0.8%)` |
| 突破买入 | 以最近有效压力/突破位为锚点，先确认价格超过锚点 | 确认缓冲 `max(0.10×ATR14, 1个最小报价单位)`；最大追价为确认价再加 `min(0.50×ATR14, 确认价×1.5%)` |
| 加仓 | 只允许盈利持仓或趋势继续确认后的回踩/再突破，不允许因亏损摊薄成本 | 沿用回踩或突破区间，但还需满足加仓后不超过最大仓位 |
| 目标卖出 | 以最近有效压力位、前高或既定止盈位为锚点 | 区间为 `锚点 ± min(0.30×ATR14, 锚点×1.0%)` |
| 趋势卖出 | 以 MA20/MA60、结构支撑或趋势失效位为触发点 | 收盘确认破位后执行，不为等待反弹额外放宽区间 |
| 止损 | 综合结构止损、波动止损和现有 8% 硬止损，选择风险更严格且仍低于当前有效价格的触发价 | 结构缓冲为 `max(0.25×ATR14, 支撑位×0.5%)`；任何情况下不得突破 8% 硬止损上限 |

通用约束：

- 所有价格按标的最小报价单位取整，并截断到下一交易日合法涨跌停范围；
- 若 ATR14、支撑/压力或复权信息无效，对应价格计划状态为 `insufficient_data`，不能用固定百分比硬补；
- 当开盘跳空越过整个买入区间时不追价，等待新的回踩/突破结构；
- 当价格直接越过止损或清仓触发价时，以可执行风险退出为目标，不使用买入式“允许偏差”；
- 价格区间、锚点、ATR、容差和版本必须随结果存档，便于回测成交率、滑点和错失率。

### 3.7 risk

```json
{
  "overall_level": "中",
  "market_risk": "中",
  "sector_risk": "低",
  "stock_risk": "中",
  "liquidity_risk": "未知",
  "event_risk": "未知",
  "hard_stop_triggered": false,
  "hard_constraints": [],
  "warnings": [],
  "disclaimer": "分析仅用于复盘与研究，不构成确定收益承诺。"
}
```

### 3.8 audit

```json
{
  "provider": "codexapis",
  "provider_type": "openai_compatible",
  "base_url_host": "www.codexapis.com",
  "model": "gpt-5.6-luna",
  "api_style": "responses",
  "prompt_version": "1.2-draft",
  "prompt_sha256": "string",
  "schema_version": "1.2-draft",
  "knowledge_version": "2026-08-26.v1",
  "knowledge_source": "llm_trade_personal_knowledge.md",
  "traditional_strategy_version": "string",
  "label_definition_version": "short_swing_v1",
  "market_snapshot_id": "string",
  "sector_snapshot_id": "string",
  "input_hash": "string",
  "observation_id": "string",
  "fusion_model_version": "string-or-null",
  "calibration_model_version": "string-or-null",
  "response_cache_hit": false,
  "provider_cache_read_tokens": null,
  "input_tokens": null,
  "output_tokens": null,
  "latency_ms": null,
  "raw_response_path": null,
  "raw_retention_days": 30,
  "raw_expire_at": "ISO-8601-or-null",
  "created_at": "ISO-8601"
}
```

## 4. 页面统一展示建议

复盘中心“交易信号”主表只把 `fusion` 作为最终操作来源，传统和 LLM 结果用于解释、对照和后续训练。下列主表字段默认全部显示，可调整顺序和宽度，底层输出 Schema 保持稳定。

桌面端主行表头草案：

| 股票 | 池/持仓 | 现价/持仓收益 | 短线趋势 | 次日概率 | 5日概率 | 评分 | 融合信号 | 操作建议 | 大盘/板块 | 触发价格 | 建议仓位%/数量 | 卖出比例/止损 | 质量/冲突 | 更新时间 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |

展开详情显示：

- 传统评分分项和阈值命中；
- 大模型支持/反对证据；
- 大盘和板块影响；
- 命中的个人经验；
- 各动作的条件计划；
- 数据缺失和风险否决；
- 模型、Prompt、策略、知识和数据版本。

传统和 LLM 原始结果默认不占主表列，只在展开详情中展示；主表中的“评分”“融合信号”和“操作建议”均来自最终 `fusion` 结果。

显示规则：

- `valid_neutral` 显示“中性”；
- `unavailable` 显示“不可用”及简短原因；
- `insufficient_data` 显示“数据不足”；
- 普通未知值显示 `—`；
- 上述状态不得统一渲染成“观望”或 `50%`。

桌面端使用可横向滚动的宽表，窄屏优先切换为摘要卡片/行展开；风险、失效和数据质量信息不得因宽度不足被静默删除。表头和默认可见列已经确认，框的位置与具体尺寸在实现时结合现有页面自适应，不在 Schema 中写死像素。

## 5. 字段确认结果

1. 次日标签为下一交易日收盘上涨；5 日标签为第 5 个交易日收盘上涨，并同时记录期间 MAE、MFE、最大回撤和止损路径。
2. 最终动作不包含“试仓”和“禁止买入”。数据或模型无效由 `assessment_status` 表达，不伪装成交易动作。
3. 止损和清仓允许同时为有效条件计划，但当前执行动作只能取已触发且优先级最高的一项。
4. 价格区间使用 `short_swing_price_v1` 的结构位、ATR 容差、突破缓冲和最大追价规则，参数版本化并接受 Walk-Forward 验证。
5. 建议仓位同时输出账户百分比和股票数量；数量按 100 股交易单位向下取整。
6. 页面主表草案的全部表头默认显示，并单独展示最终评分和操作建议；传统/LLM 原始分项在展开详情中显示。

分钟 K 周期采用本地自适应选择策略，最多覆盖 3 个交易日；实际周期和覆盖范围必须进入输入审计，但不改变以上输出字段约定。统一输出结构现已无待用户确认字段。
