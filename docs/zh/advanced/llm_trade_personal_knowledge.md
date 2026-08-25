# A 股短线趋势波段个人交易知识库

> 状态：可编辑事实源。版本：`2026-08-26.v1`。
>
> 本文由用户原始交易笔记提炼而成。规则只用于复盘与条件化交易建议，不直接下单。后续可将新的编号笔记粘贴到文末“待整理经验收件箱”，再去重、拆卡和升级版本。

`source_batch: USER-NOTES-20260826-01` · `updated_at: 2026-08-26` · `strategy_style: a_share_short_term_trend_swing`

## 1. 使用与编辑约定

- `status: active` 才能进入检索；`draft` 只保存、不进入 Prompt；`deprecated` 保留历史但禁用。
- `always_apply: true` 只用于每次都必须遵守的短规则；其余规则按市场、板块、形态和持仓状态检索。
- 一张卡只表达一个主要决策。重复经验合并，矛盾经验通过 `conflicts_with` 和适用条件解决。
- 个股名称只放入案例，不作为永久规则；缺少日期、输入快照和结果的案例保持 `draft`。
- 模型必须返回使用过的 `rule_id` / `case_id`，未检索到的经验不得自行引用。

固定 Prompt 只压缩注入以下规则：

`RULE-RISK-101`、`RULE-RISK-102`、`RULE-ENV-101`、`RULE-SIGNAL-101`、`RULE-POSITION-101`。

## 2. 仓位与全局风控

### RULE-RISK-101 仓位是长期胜率的放大器

`type: hard_rule` · `scope: all` · `tags: 仓位, 风险预算, 止损` · `priority: 100` · `always_apply: true` · `status: active`

- 规则：先确定单笔账户可承受回撤，再反推仓位；不能先有个股预期、再为仓位找理由。
- 解释：20% 仓位、单票止损 2%～3% 时，账户损失约 0.4%～0.6%；单票盈利 5%～10% 时，账户贡献约 1%～2%。这是风险收益示例，不代表每笔固定使用 20%。
- 动作：逻辑初步成立但反馈不足时使用小仓；只有趋势、板块和反馈改善后才允许加仓。
- 禁止：因“相对安全”“逻辑很好”跳过仓位和止损约束。

### RULE-RISK-102 仓随势动并执行连续负反馈停手机制

`type: hard_rule` · `scope: all` · `tags: 仓随势动, 负反馈, 停手` · `priority: 100` · `always_apply: true` · `status: active`

- 规则：仓位随市场环境、板块有效性和交易反馈变化，不随情绪变化。
- 降仓/停手条件：题材失效；市场成交下降且板块东拉西扯；连续两次同方向交易没有正反馈或连续两次亏损。
- 动作：降低总仓位和交易频率；连续负反馈后暂停同方向新开仓，等待新的市场/板块共振。
- 恢复：必须出现可验证的新信号，不能仅因休息了一段时间自动恢复满频率。

### RULE-ENV-101 环境优先于个股，没有主线时等待

`type: hard_rule` · `scope: market, entry` · `tags: 环境, 主线, 成交额, 等待` · `priority: 100` · `always_apply: true` · `status: active`

- 规则：大盘风险、市场成交和赚钱效应优先于单只股票逻辑。暂停交易是主动风控。
- 条件：没有清晰主线、成交额下降、快速轮动、板块缺乏持续赚钱效应时，“可买可不买则不买”。
- 动作：降低仓位和频率；用户仍决定验证时，只能把“买入”计划压缩到最小风险仓位，不新增“试仓”动作枚举。
- 反证：市场、板块、核心与成交同时形成持续共振后，才可恢复正常风险预算。

### RULE-EXEC-101 普通交易者以趋势波段为主

`type: strategy_rule` · `scope: all` · `tags: 趋势波段, 极短线, 执行` · `priority: 90` · `always_apply: false` · `status: active`

- 规则：极短线对信息、速度和执行要求过高；本系统以短线趋势波段为主。
- 动作：先用受控仓位验证逻辑，再根据趋势和反馈调整；弱市中最低成本验证可以压缩到一手，但系统动作仍记为“买入”，不新增“试仓”枚举；不因盘中刺激切换为无计划的超短追涨。

## 3. 信号、均线与入场

### RULE-SIGNAL-101 观察信号不等于介入信号

`type: hard_rule` · `scope: entry` · `tags: 多因子确认, 均线, 资金` · `priority: 100` · `always_apply: true` · `status: active`

- 规则：五日线、十日线、分时均线、零轴、净流入、题材或单只核心走强，任何单一信号都不足以触发买入。
- 最低确认：市场环境、板块强度、个股主动性/承接、资金净额和仓位条件至少形成可解释组合。
- 输出：只有回踩均线或形态未坏时，最多给“观察/持有等待”，不能直接认定为买点。

### RULE-TECH-101 不同参考线承担不同功能

`type: experience_rule` · `scope: entry, holding` · `tags: 五日线, 十日线, 分时均线, 零轴` · `priority: 85` · `always_apply: false` · `status: active`

- 五日线：判断短趋势和持仓去留锚点。
- 十日线：观察较深回踩后的趋势完整性，不替代五日线的短周期职责。
- 分时均线：判断日内承接和主动性。
- 零轴：判断能否从弱转强。
- 约束：使用哪条线取决于原始交易周期，不能把多条线混成一条万能规则。

### RULE-TECH-102 零轴、分时均线和资金净额组合确认

`type: experience_rule` · `scope: intraday, entry, holding` · `tags: 零轴, 分时均线, 资金净额` · `priority: 85` · `always_apply: false` · `status: active`

- 健康主动：价格围绕或重新站稳分时均线，零轴显示强弱改善，资金净额有真实买盘配合。
- 风险信号：脱离均线直线急拉但不能封板，或资金净额不配合；首次明显拐头时优先减仓。
- 环境修正：跌破分时均线不等于绝对弱，还要结合开盘价、量能、压力位和板块表现。

### RULE-ENTRY-101 缩量轮动只低吸等待，不追已启动方向

`type: experience_rule` · `scope: entry` · `market_regime: 轮动, 震荡` · `tags: 缩量, 电风扇, 低吸, 不追红盘` · `priority: 90` · `always_apply: false` · `status: active`

- 条件：成交缩量、板块快速轮动、方向持续性不足。
- 规则：等待靠近五日线、启动点或清晰支撑的低成本位置；已拉升方向的空间收窄、回撤风险上升。
- 动作：已有票轮动冲高可减仓；逻辑未坏且有成本优势的弱势票可等轮动；场外无成本优势时宁可错过。
- 禁止：追逐红盘脉冲或把轮动当成趋势启动。

### RULE-ENTRY-102 高开日和弱票尾盘拉升先观察

`type: experience_rule` · `scope: entry` · `tags: 高开, 竞价, 尾盘拉升, 盈亏比` · `priority: 85` · `always_apply: false` · `status: active`

- 条件：指数明显高开、板块竞价抢筹、核心大幅高开，或弱势票尾盘突然拉升。
- 规则：高开压缩盈亏比；弱票尾盘拉升可能是拿货、消息博弈或诱多，动机不能仅靠价格猜测。
- 动作：等待回踩、次日开盘后的分时承接与资金确认，不在方向未明时追入。

### RULE-ENTRY-103 不低抛高接，换仓必须有独立买点

`type: execution_rule` · `scope: entry, holding` · `tags: 预设买点, 换仓, 情绪交易` · `priority: 90` · `always_apply: false` · `status: active`

- 规则：低位回踩不信、拉高后再追是情绪驱动；同级别个股之间随意换仓容易两头落空。
- 动作：在交易前定义买点、失效点和最大追价；换仓前验证新标的地位、相对主动性和独立介入点。
- 接受：错过计划买点后等待下一结构，不通过追高“修复踏空感”。

### RULE-ENTRY-104 天量阴线不是便宜

`type: risk_rule` · `scope: entry, holding` · `tags: 天量阴线, 抛压, 尾盘确认` · `priority: 90` · `always_apply: false` · `status: active`

- 规则：大成交阴线说明分歧和抛压都强，跌幅大不等于低风险。
- 动作：未企稳前不因“便宜”低吸；盘中跳水至少等待尾盘结构、承接和板块状态确认。

## 4. 板块、核心与市场结构

### RULE-SECTOR-101 点火必须检查带动性和跟随

`type: experience_rule` · `scope: sector, entry` · `tags: 点火, 共振, 跟随, 增量资金` · `priority: 95` · `always_apply: false` · `status: active`

- 有效点火：容量/核心主动放量，同方向前排、中军和后排同步增量，产业链有跟随。
- 无效点火：孤立涨停、单一容量票脉冲、后排不跟，更多是存量资金博弈。
- 动作：无跟随时不得因单票强势上调板块强度或追入后排。

### RULE-SECTOR-102 核心必须具备带队能力

`type: experience_rule` · `scope: sector, entry, holding` · `tags: 核心, 龙头, 中军, 该强不强` · `priority: 95` · `always_apply: false` · `status: active`

- 规则：龙头和趋势容量票是板块锚点；杂毛高开、单股脉冲或一字试盘不能替代核心定调。
- 弱势信号：个股“应该上板却没有上板”、核心长时间磨蹭、冲板失败且板块不跟。
- 动作：承认资金没有执行逻辑；已有仓优先减风险，不以“逻辑正确”为理由继续加码。

### RULE-SECTOR-103 分歧不足时新资金缺少舒适成本

`type: market_rule` · `scope: market, sector` · `tags: 分歧, 存量博弈, 做T` · `priority: 80` · `always_apply: false` · `status: active`

- 条件：市场“要弱不弱、要强不强”，强势方向没有充分换手或回踩。
- 规则：场外资金缺少舒适成本，场内资金以做 T 为主，点火容易遭遇存量抛压。
- 动作：降低追涨概率，等待充分分歧后的承接、回流或新资金证据。

### RULE-SECTOR-104 相对安全不等于绝对安全

`type: risk_rule` · `scope: sector, position` · `tags: 容量, 相对安全, 赚钱效应` · `priority: 90` · `always_apply: false` · `status: active`

- 规则：个股在板块中更稳、容量更大，只表示相对风险较低；板块没有赚钱效应时仍可能大幅波动。
- 动作：必须同时使用小仓、均线/结构失效点和止损，不因“容量票”取消风险控制。

### RULE-SECTOR-105 板块高潮后的退潮是过程

`type: experience_rule` · `scope: sector, holding` · `tags: 高潮, 分歧, 反抽, 退潮` · `priority: 75` · `always_apply: false` · `status: active`

- 规则：板块高潮不等于次日立即结束；趋势退潮常经历分歧、反抽和套牢盘释放。
- 动作：不机械预测“一步跌完”，按核心强弱、跟随、成交和反抽质量分阶段处理仓位。

### RULE-ANCHOR-101 风险锚走弱时取消原计划

`type: hard_rule` · `scope: entry, holding` · `tags: 风险锚, 核心锚, 计划失效` · `priority: 95` · `always_apply: false` · `status: active`

- 规则：每笔交易可预先指定板块核心、产业链大票或风险事件作为锚；锚点是计划条件，不是事后解释。
- 触发：锚点跌破预定结构、失去分时承接或出现明确风险状态。
- 动作：取消新开仓/加仓；已有仓按破位程度减仓、止损或清仓，即使个股叙事仍在。

## 5. 持仓、加仓与退出

### RULE-POSITION-101 已有仓与新开仓使用不同等待标准

`type: hard_rule` · `scope: entry, holding` · `tags: 车上车外, 利润垫, 成本优势` · `priority: 100` · `always_apply: true` · `status: active`

- 已有仓：低成本、小仓或有利润垫时，可以在逻辑未坏且未破位的前提下等待修复、反包或做 T。
- 趋势仓：板块未明确退潮、核心仍有反包能力且仓位可控时，不因单根均线瞬时失守机械清仓，但必须保留结构止损。
- 新开仓：是在重新承担风险，必须等待五日线/启动点、板块共振、日内承接和合理盈亏比。
- 无利润垫：弱势里赌反抽会把可控交易变成不可控回撤，等待容忍度应低于有利润垫持仓。
- 约束：同一句“可以等”必须标明适用于持仓还是场外资金。

### RULE-POSITION-102 套利票与主线票使用不同持有标准

`type: position_rule` · `scope: holding` · `tags: 套利, 主线, 次日反馈` · `priority: 90` · `always_apply: false` · `status: active`

- 套利票：要求较快兑现；次日无正反馈、不能连续走强或套利逻辑消失时离场。
- 主线票：可结合核心地位、板块是否退潮和趋势结构给予更大波动容忍度。
- 禁止：用主线“格局”替套利交易找继续持有的借口。

### RULE-POSITION-103 做 T 必须有利润垫和稳定执行能力

`type: execution_rule` · `scope: holding` · `tags: 做T, 利润垫, 落袋` · `priority: 85` · `always_apply: false` · `status: active`

- 前提：已有底仓/利润垫、清晰支撑压力、能够稳定执行低吸高抛。
- 品种区别：T+0 品种先低吸、高抛兑现日内价差；普通 A 股只能围绕既有底仓、可用持仓和清晰支撑执行。
- 退出：连续无法做出合理价差，或做 T 使盈利持续回吐时，停止操作并及时退出。
- 禁止：把日内套利做成追涨杀跌，或用做 T 为长期被套寻找理由。

### RULE-POSITION-104 尖角拉升和封板失败优先降风险

`type: position_rule` · `scope: holding` · `tags: 尖角拉升, 炸板, 该强不强` · `priority: 90` · `always_apply: false` · `status: active`

- 风险信号：脱离均线的尖角急拉、封板失败、冲高后缺乏持续承接。
- 动作：已有仓首次明显拐头优先减仓；等待回落后能否重新站稳分时均线及板块是否重新共振。

### RULE-ADD-101 既有弱仓原则上不补，条件性加仓必须有独立理由

`type: position_rule` · `scope: add, holding` · `tags: 加仓, 不摊薄, 指数拖累` · `priority: 95` · `always_apply: false` · `status: active`

- 已有弱仓：走弱先判断是否破位，原则上不为摊薄成本补仓。
- 条件性加仓：仅在原介入理由仍有效、个股不领跌、主要由指数拖累、关键位承接确认且加仓后风险预算仍合格时考虑。
- 禁止：用“逻辑没变”忽略板块退潮、锚点失效或资金持续流出。

### RULE-POSITION-105 板块共振跳水时区分持仓处理与新开仓

`type: position_rule` · `scope: entry, holding` · `tags: 共振跳水, 恐慌, 回流` · `priority: 85` · `always_apply: false` · `status: active`

- 已有仓：未触发结构止损时，不因板块瞬时共振跳水恐慌割在最低点；观察是否破位和是否有回流。
- 新开仓：必须等待容量核心或强分支带动回流，不能把“持仓可等”复制成“场外可买”。

## 6. 跌幅、外部映射与事件

### RULE-CONTEXT-101 大跌是否可低吸取决于原趋势

`type: experience_rule` · `scope: entry` · `tags: 大跌, 上升趋势, 震荡市` · `priority: 85` · `always_apply: false` · `status: active`

- 上升趋势：大跌可能形成低吸机会，但仍需结构未坏、板块/核心有修复能力和明确止损。
- 震荡/弱势：无逻辑大跌后的弱修复容易反复收割，不因跌幅大自动产生买点。

### RULE-CONTEXT-102 外盘映射看相对强弱，不只看方向

`type: market_rule` · `scope: market, entry` · `tags: 外盘, 映射, 相对强弱` · `priority: 75` · `always_apply: false` · `status: active`

- 规则：比较 A 股相对外围的涨跌幅、承接和持续性；A 股跌得更多、涨得更少时，继续按外围方向下注会失去可控性。
- 动作：外盘只作为环境证据，不单独触发交易。

### RULE-CONTEXT-103 介入前评估隔日流动性

`type: risk_rule` · `scope: entry` · `tags: 隔日流动性, 缩量, 后排` · `priority: 90` · `always_apply: false` · `status: active`

- 规则：当天能涨不代表下一个交易日有人承接，缩量环境中的后排题材风险更高。
- 检查：核心地位、成交容量、板块跟随、次日潜在接力资金和退出通道。
- 动作：隔日承接不清晰时降低仓位或放弃新开仓。

### RULE-CONTEXT-104 盘中消息先看资金反应

`type: event_rule` · `scope: market, sector, holding` · `tags: 消息, 资金反应, 筹码` · `priority: 80` · `always_apply: false` · `status: active`

- 顺序：先看核心、板块、成交和净额的实际反应，再判断消息可信度、筹码博弈和后续发酵。
- 动作：同时预设承受不了时的减仓/退出条件；不因消息叙事取消风控。

### RULE-RISK-103 严重异动价提前降低尾盘风险

`type: risk_rule` · `scope: holding` · `tags: 严重异动, 尾盘竞价, 提前处理` · `priority: 95` · `always_apply: false` · `status: active`

- 规则：不赌价格“刚好不触发”严重异动或监管风险阈值。
- 动作：在不可控的尾盘竞价前提前一档处理大部分风险仓位，仅保留符合风险预算的部分。

## 7. 个股案例草稿（暂不进入检索）

以下内容来自原始笔记，但缺少交易日期、代码、完整输入和结果，统一保持 `status: draft`：

| case_id | 涉及标的 | 待验证经验 | 缺失信息 |
| --- | --- | --- | --- |
| CASE-DRAFT-001 | 协创、工业富联 | 板块内相对稳健/容量较大不代表板块无赚钱效应时绝对安全 | 日期、代码、板块快照、入场和结果 |
| CASE-DRAFT-002 | 宝鼎 | 孤立涨停且产业链不跟，可能是短线资金自我博弈 | 日期、代码、跟随数据和后续收益 |
| CASE-DRAFT-003 | 通威 | 套利方向次日不兑现、不能连阳时应快速退出 | 日期、代码、套利逻辑和退出结果 |
| CASE-DRAFT-004 | 斯菱/风华、宇晶/云南锗业 | 风险锚走弱时应取消个股原计划 | 完整名称/代码、日期、锚点阈值和结果 |
| CASE-DRAFT-005 | 新易盛 | 核心跌破分时均线时后排风险放大 | 日期、代码、板块样本和后续路径 |

案例补全并通过复盘前，不得作为硬规则或在 Prompt 中引用。

## 8. 待整理经验收件箱

以后可以直接按原始编号形式追加到这里，无需先改写成规则。建议每批增加日期和来源：

```markdown
### INBOX-YYYYMMDD-01

status: draft
source: 手工复盘
market_date: YYYY-MM-DD 或 unknown

1. 原始经验……
2. 原始经验……
```

整理时执行：去重 → 拆分单一判断 → 标记适用环境/持仓状态 → 补充所需数据 → 指定动作与失效条件 → 检查冲突 → 升级知识版本。原始批次保留引用，不直接进入模型固定前缀。
