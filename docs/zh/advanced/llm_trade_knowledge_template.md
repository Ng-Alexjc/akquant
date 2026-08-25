# 个人交易知识与经验模板（评审草案）

> 推荐把 Markdown 作为可人工维护的事实源。开发时可以建立 SQLite FTS5 或向量索引，但索引只是缓存，不能替代原文。
>
> 当前已经整理的真实规则见 [A 股短线趋势波段个人交易知识库](llm_trade_personal_knowledge.md)。本文只保留新增规则和案例的格式规范。

## 0. 策略适用范围

本知识库的统一策略语境是 **A 股短线趋势波段**。经验规则必须围绕趋势、量价、位置、市场/板块共振、入场节奏、持仓管理和风险退出编写。长期价值估值、纯消息追涨和高频微观结构经验不得混入同一规则集合；如未来确需支持，应建立独立策略 ID、知识版本和评价体系。

每日分析结果不会自动成为“正确经验”。只有在预测周期结束、真实标签回填并通过样本外复盘后，才能生成候选案例；候选案例还需规则校验或人工审核后才可进入正式检索库。

## 1. 全局硬规则

每条规则保持短小、单一、可判断。`always_apply: true` 的规则会进入固定 Prompt，因此数量应尽量少。

```yaml
---
id: RULE-RISK-001
title: 不在数据过期时给出买入建议
type: hard_rule
scope: [all]
tags: [数据质量, 风控]
priority: 100
always_apply: true
enabled: true
status: active
source_batch: USER-NOTES-YYYYMMDD-01
updated_at: 2026-08-25
---
```

规则：

> 个股、市场或板块关键数据过期时，不给出买入或加仓建议。设置 `assessment_status=insufficient_data`、交易动作为 `null`，等待刷新。

可执行判定：

- 关键数据 `stale=true`；或
- 个股与市场数据日期不一致且无法解释。

例外：无。

## 2. 形态经验规则

```yaml
---
id: RULE-ENTRY-001
title: 放量突破后不立即追高
type: experience_rule
scope: [entry]
market_regime: [震荡, 退潮]
tags: [突破, 追高, 回踩]
priority: 70
always_apply: false
enabled: true
status: active
source_batch: USER-NOTES-YYYYMMDD-01
updated_at: 2026-08-25
---
```

适用条件：

- 当日出现显著放量突破；
- 收盘距离短期支撑过远；
- 市场或板块不是强趋势顺风。

经验：

> 优先等待缩量回踩并重新站稳，不在单日加速后直接追高。

失效条件：

- 市场和板块均处于明确强趋势；
- 个股存在可验证的持续催化；
- 风险收益比仍满足已确认阈值。

建议动作：`等待买入`。

## 3. 持仓管理规则

```yaml
---
id: RULE-HOLD-001
title: 盈利持仓破趋势先减仓
type: position_rule
scope: [holding]
tags: [减仓, 趋势破位]
priority: 80
always_apply: false
enabled: true
updated_at: 2026-08-25
---
```

适用条件：

- 持仓仍盈利；
- 中期趋势首次破位；
- 尚未命中硬止损。

经验：

> 优先减仓观察，不必把计划卖出与硬止损混为一类。

需要的数据：

- 持仓成本和收益；
- 趋势破位定义；
- 可用持仓数量。

## 4. 历史案例

案例用于类比，不能当成硬规则。

```yaml
---
id: CASE-2026-001
title: 某板块退潮期追高失败
type: case
symbols: [示例代码]
market_regime: 退潮
tags: [追高, 板块退潮, 放量]
outcome: loss
status: validated
source_batch: USER-NOTES-YYYYMMDD-01
updated_at: 2026-08-25
---
```

当时信息：

- 大盘：
- 板块：
- 个股趋势：
- 成交量：
- 传统评分与概率：
- 买入原因：

结果：

- 入场：
- 最大有利 MFE：
- 最大不利 MAE：
- 最终收益：
- 持仓时间：

复盘结论：

> 用一到三句话说明真正可迁移的经验，不记录无法验证的情绪化结论。

下次检查项：

- [ ] 板块是否仍强于市场
- [ ] 是否属于加速段
- [ ] 是否有回踩确认
- [ ] 止损位置是否明确

## 5. 规则冲突

若规则可能冲突，应显式记录：

```yaml
conflicts_with:
  - RULE-TREND-003
conflict_resolution:
  - hard_rule 优先于 experience_rule
  - 持仓风控优先于新开仓机会
  - 数据质量规则优先于所有方向判断
```

## 6. Token 控制建议

- `always_apply` 只放真正每次必需的短规则；
- 一条规则只表达一个判断；
- 避免长篇行情故事，使用字段和条件；
- 案例保存完整原文，但注入 Prompt 时只传摘要；
- 每次只检索与当前市场状态、板块、形态和持仓状态相关的规则；
- 模型输出必须返回引用的 `rule_id` / `case_id`，方便验证检索是否有用。

## 7. 新增经验流程

用户可以继续按自然语言编号列表提供经验，不必预先整理。写入知识库时：

1. 保存原始批次 ID 和日期；
2. 合并语义重复的观点；
3. 将一条复杂观点拆成单一可判断规则；
4. 补充适用环境、持仓/场外身份、所需数据、动作和失效条件；
5. 个股名称进入案例，不直接成为永久规则；
6. 新规则先标记 `draft`，人工复核后改为 `active`；
7. 发现冲突时不覆盖旧规则，使用 `conflicts_with` 和优先级处理；
8. 更新知识版本并保留变更记录。

推荐原始收件箱格式：

```markdown
### INBOX-YYYYMMDD-01

status: draft
source: 手工复盘
market_date: YYYY-MM-DD 或 unknown

1. 原始经验……
2. 原始经验……
```
