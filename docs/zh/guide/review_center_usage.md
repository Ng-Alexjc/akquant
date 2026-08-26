# AKQuant 复盘中心使用指南

本文档说明当前本地复盘中心的安装、启动、数据源、传统指标、个人交易经验、大模型分析、回测、反馈和持续优化流程。

系统面向 A 股收盘后短线趋势波段分析。大模型只分析用户在当前观察票池或持仓票池中勾选的股票，不执行真实交易。

## 1. 系统组成与运行边界

复盘中心由三部分组成：

- 传统分析：行情、K 线、均线、动量、波动率、趋势评分和双时间窗口概率。
- LLM 分析：读取传统结果、最近日 K、分钟 K、持仓、市场环境和个人交易经验，生成结构化评价。
- 融合与记录：传统结果为基线，LLM 只能在约束范围内修正，并保存分析、Token 用量、反馈和未来结果标签。

妙想是可选的数据增强接口。当前已接入妙想的个股数据、所属板块、板块强度和全市场上涨/下跌家数；主要指数报价仍由东方财富提供。妙想市场宽度不可用时才降级到东方财富样本，并明确标记来源和状态。

当前 LLM 还会通过只读 `mx_ashare_finance_data` 获取所选个股的近期行情、换手率、成交额、估值、行业和风险指标摘要。妙想数据会标记来源与时间戳，并经过行数截断后进入 Prompt，避免原始表格无限增加 Token。

为兼容旧 Prompt，市场上下文同时提供 `market_breadth` 和别名 `market_breadth2`，两者内容一致；字段缺失时会返回明确的 `unavailable` 状态。

系统当前只读妙想工具；模拟交易接口仅用于本地复盘记账，禁止连接真实下单工具。

## 2. 依赖安装

### 2.1 前置要求

- Windows 10/11
- Python 3.10 或更高版本（当前环境为 Python 3.12）
- Visual Studio 2022 Build Tools
- “使用 C++ 的桌面开发”工作负载
- MSVC v143 和 Windows SDK

Build Tools 不提供图形化 IDE，但必须能找到 `cl.exe` 和 `link.exe`。

### 2.2 推荐：使用项目专用虚拟环境

在 PowerShell 中执行：

```powershell
cd D:\vscode_poject\akquant\akquant

# 第一次执行时创建环境
python -m venv .venv

# 激活环境
.\.venv\Scripts\Activate.ps1

# 安装项目及复盘中心 AI 依赖
python -m pip install --upgrade pip
python -m pip install -e ".[review-ai]"

# 检查依赖
python -m pip check
```

如果 PowerShell 禁止执行激活脚本，可只对当前窗口放行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\.venv\Scripts\Activate.ps1
```

如果需要显式加载 MSVC 环境：

```powershell
$vsDevShell = 'C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\Tools\Launch-VsDevShell.ps1'
& $vsDevShell -Arch amd64 -HostArch amd64
.\.venv\Scripts\Activate.ps1
```

验证：

```powershell
where.exe cl
where.exe link
python -c "import akquant; print('akquant import: OK')"
```

项目声明使用 NumPy 2.x。不要在项目环境中为了迁就旧的 Anaconda 全局包而强行降级 NumPy；应使用 `.venv` 隔离环境。

## 3. 启动、停止与状态检查

### 3.1 启动

```powershell
cd D:\vscode_poject\akquant\akquant
.\.venv\Scripts\Activate.ps1
python scripts/review_center_server.py
```

浏览器打开：

<http://127.0.0.1:8765/akquant_review_center.html>

当前服务也可以由后台进程启动。日志文件为项目根目录下的 `review_center_server.log` 和 `review_center_server.err.log`。

### 3.2 状态接口

```text
http://127.0.0.1:8765/api/ai/status
```

重点字段：

- `ready: true`：当前 LLM Provider 已配置并可调用。
- `miaoxiang_ready: false`：妙想未启用或尚未填写 Key，这是允许的正常状态。
- `missing`：缺少的 Provider 配置字段。

### 3.3 停止

在运行服务器的终端按 `Ctrl+C`。如果是后台进程，可先查看 Python 进程，再只停止命令行为 `review_center_server.py` 的进程，不要结束其他 Python 程序。

## 4. API Key 与 Provider 设置

### 4.1 Codex/OpenAI 兼容 Provider

本地配置文件是：

```text
D:\vscode_poject\akquant\akquant\llm_trade.local.yaml
```

该文件已被 `.gitignore` 忽略。API Key 只填写在本地文件，不要提交到 Git，也不要粘贴到聊天记录。

配置示例：

```yaml
active_provider: deepseek

providers:
  deepseek:
    enabled: true
    provider_type: openai_compatible
    api_style: chat_completions
    url: https://api.deepseek.com/v1
    sk: "填写本地 DeepSeek Key"
    model: deepseek-chat
```

当前首选使用 DeepSeek Chat Completions。启动服务后访问 `/api/ai/status`，确认 `ready` 为 `true`。

当前本机运行配置已切换为 DeepSeek 进行接入测试；CodexAPIs 配置仍保留，可随时将 `active_provider` 改回 `codexapis`。没有 DeepSeek Key 时，`ready` 会为 `false`，但页面和传统分析仍可正常运行。

### 4.2 DeepSeek 或 Qwen

可在同一文件中填写对应 Provider，然后修改 `active_provider`：

```yaml
active_provider: deepseek

providers:
  deepseek:
    enabled: true
    provider_type: openai_compatible
    api_style: chat_completions
    url: https://api.deepseek.com/v1
    sk: "填写本地 DeepSeek Key"
    model: deepseek-chat
```

Qwen 使用 `https://dashscope.aliyuncs.com/compatible-mode/v1` 和 `qwen-plus` 示例配置。修改配置后需要重启服务。

### 4.3 妙想（可选）

没有妙想 Key 时保持：

```yaml
miaoxiang:
  enabled: false
  em_api_key: ""
```

取得 Key 后，只在本地填写：

```yaml
miaoxiang:
  enabled: true
  url: https://mxapi.eastmoney.com/mxds/mcp
  em_api_key: "填写本地妙想 Key"
  allow_read_tools: true
  read_tool_allowlist:
    - mx_index_block_finance_data
  allow_simulated_trade_writes: false
  # 个股事实压缩上限
  stock_max_tables: 6
  stock_max_rows: 8
  stock_max_value_chars: 80
  stock_max_total_chars: 5000
  # 新闻/公告默认关闭；开启会增加外部文本 token 消耗
  news_enabled: false
  news_days: 3
  news_max_items: 3
  news_max_chars: 1200
```

首次使用前先调用 `/api/miaoxiang/tools` 获取官方 `tools/list` 返回的真实工具名。当前已核验并启用 `mx_index_block_finance_data` 只读工具；其他工具仍需单独审核后再加入白名单。自选管理写入操作还需要先确认目标自选组。任何真实交易、下单、买入、卖出工具都不启用。

## 5. 页面使用与数据刷新

1. 在“观察票池”或“持仓票池”中搜索股票并增加。
2. 持仓票可以直接编辑成本价和持股数量。
3. 主表默认以卡片显示统一融合结果，每只个股无需横向滚动即可看到趋势、次日概率、5 日概率、评分、操作建议、价格区间、仓位、卖出比例、止损和质量状态；交易信号卡片不显示股票代码。
4. 勾选股票后点击“分析选中”，只对勾选股票调用 LLM。
5. 点击股票名称或代码查看 K 线；展开“传统 / LLM / 融合明细”查看审计信息。
6. 页面刷新使用 180 秒行情缓存；需要立即请求时使用页面刷新动作，不要高频循环刷新。

LLM 输入审计现在会同时包含当前个股的完整持仓字段、全持仓/观察池摘要、传统阈值快照及本次实际触发的传统动作/理由；这些内容可在历史原始请求中核对。

账户上下文会从本地复盘账本计算初始资金、可用现金、已实现盈亏、持仓市值和总资产。若其他持仓没有可验证现价，总资产会标记为 `partial`/`null`，不会猜测余额。

当前数据范围：

- 日 K：数据源请求最近 360 个自然日，传给 LLM 最近 10 根。
- 分钟 K：从 09:30 到刷新时间，最多覆盖 3 个交易日；周期由实际行数自适应选择，不固定为某一个分钟周期。
- 分析时点：收盘后。分钟 K 只用于复核当天及近期日内结构。

妙想个股基本面默认只查询一次并缓存 180 秒，随后仅保留有限的语义字段、最多 6 张表/每表 8 行、单值 80 字符、总计 5000 字符，并在输入中标记 `truncated` 和实际限制。公告/新闻默认不调用；需要时将 `news_enabled` 设为 `true`，并把官方只读工具 `mx_finance_search_news`、`mx_finance_search_notice`（以 `/api/miaoxiang/tools` 的实际名称为准）加入 `read_tool_allowlist`。启用后只取最近 3 天、最多 3 条、总文本不超过 1200 字，统一为标题、时间、来源、摘要和风险标签，且按股票缓存 180 秒。`unavailable` 表示未启用或不可用，不表示“没有风险”。

个股事实查询会优先提取主力/超大单/大单资金净流入等少量资金流字段；板块查询在同一次板块请求中尝试提取最多 5 只核心/龙头的今日与近 5 日表现，放入 `sector_context.core_performance`。这两块均有长度上限，不会把完整板块成分或原始资金流明细传给 LLM。

## 6. 指标查看与增加

### 6.1 页面可查看的指标

页面顶部“策略核心指标”显示：

- 上证指数、创业板指数涨跌幅
- 初始资金、期末权益、已实现盈亏、浮动盈亏、总净盈亏
- 策略收益、当前持仓数量、仓位比例
- 交易数、盈利笔数、胜率、最大回撤、夏普比率

交易明细可查看收益率、持仓时长、MAE（最大不利 excursion）和 MFE（最大有利 excursion）。

传统信号中还会显示短线趋势、选择评分、均线、ATR、动量、支撑/压力和次日/5 日概率。概率不可计算时显示不可用，不用 0.5 伪装成中性。

### 6.2 在策略中增加自定义指标

AKQuant 支持预计算指标和增量指标两种方式。已有指标开发规范见：

- [自定义指标指南](custom_indicator.md)
- [指标组合实战手册](talib_indicator_playbook.md)

预计算指标适合对完整 DataFrame 一次性计算；增量指标适合逐 Bar 更新、多个标的和断点续跑。指标加入策略后，应先在传统回测中验证，再决定是否将其加入复盘中心信号。

### 6.3 让新指标进入复盘与 LLM

仅在策略文件中注册指标，不会自动进入 LLM。正式接入需要同步完成：

1. 在传统分析输出中增加稳定字段名和单位。
2. 在复盘中心主表或明细中增加显示字段。
3. 在 `_build_ai_context` 的 `traditional` / `stock_context.technical` 中加入该字段。
4. 在 Prompt 模板中说明指标定义、方向、阈值和缺失处理。
5. 加测试，确认历史数据不足时状态为不可用而不是填充假值。

建议每次只增加一个指标或一组强相关指标，并记录版本号，避免回测结果无法追溯。

## 7. 个人交易经验知识库

知识库文件：

```text
docs/zh/advanced/llm_trade_personal_knowledge.md
```

它是可编辑的 Markdown 规则库，适合保存不敏感的交易原则、约束、触发条件、反例和适用环境。当前知识被整理为规则卡片，包含 `RULE-*` 编号、状态、标签、规则和执行含义。

### 7.1 增加经验的格式

复制下面模板追加到文件：

```markdown
### RULE-SIGNAL-NEW：规则标题

- status: active
- tags: 趋势, 板块, 持仓
- priority: normal
- rule: 用一句话写可执行规则。
- rationale: 说明为什么这样做。
- trigger: 写出确认条件。
- invalidation: 写出失效条件。
- action: 写出买入、持有、减仓或退出动作。
- examples: 可选，记录历史案例。
```

## 10. 每日分析、历史重放与持续优化

### 9.1 每日 LLM 分析

页面“分析选中”仍只分析勾选标的。若要对当前持仓和观察池执行一次完整收盘后批量分析，可调用：

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8765/api/ai/daily-run `
  -ContentType 'application/json' -Body '{}'
```

该接口不会创建系统计划任务，也不会自动下单。可使用 Windows 任务计划程序在收盘后调用上面的命令；是否启用计划任务由用户自行决定。

### 9.2 历史 LLM 重放

每次分析的结构化结果长期保存，完整 Prompt/Schema/原始响应按 30 天（超长记录 7 天）留存。使用历史 `analysis_id` 重放：

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8765/api/ai/replay `
  -ContentType 'application/json' -Body '{"analysis_id":"填入ID","persist":false}'
```

`persist=false` 为对比试跑，结果不会保留；`persist=true` 才会保存为新的分析记录。重放使用历史输入快照，不读取未来行情，适合比较 Prompt 或模型版本变化。

批量重放默认只返回待确认数量；确认会产生新的 LLM 调用并受当前 Key 配额限制：

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8765/api/ai/replay-batch `
  -ContentType 'application/json' -Body '{"limit":20,"confirm":true,"persist":false}'
```

### 9.3 训练与发布门槛

访问 `/api/ai/training` 可查看真实标签数量、Brier Score、AUC、Precision/Recall 和校准误差。样本不足时返回 `insufficient_data`；达到数据量后也只生成候选评估，不会自动改变线上融合权重。模型升级必须经过样本外 Walk-Forward、风险/回撤比较和人工确认后再发布。

要求：

- 一条经验只表达一个可执行规则。
- 避免“感觉强”“适当加仓”等无法验证的词，尽量写成条件和动作。
- `status: draft` 只做记录，不会注入 Prompt；确认后改为 `active`。
- 固定硬规则和动态检索规则都要保留唯一 ID，修改原规则时不要复用到另一条含义完全不同的规则。

知识库每次修改都会改变知识版本和哈希，分析记录会保存当时的版本，便于复盘。

## 8. 回测与复盘闭环

### 8.1 传统策略回测

传统策略回测仍按 AKQuant 原有方式运行，建议至少包含：

- 明确起止日期、标的池和初始资金。
- 手续费、滑点、最小交易单位和 A 股交易约束。
- 不使用未来数据；信号必须在当根 Bar 可获得的信息上计算。
- 输出收益、最大回撤、夏普、胜率、交易数和逐笔交易。

具体回测入口和策略生命周期请参考 [策略开发教材](../textbook/05_strategy.md) 与仓库中的 `examples/`。

### 8.2 复盘中心模拟交易

页面中的模拟买入/卖出只写入本地复盘状态，用于记录实际观察或手工复盘，不连接券商。交易明细和权益曲线可在页面查看。

### 8.3 LLM 分析结果与未来标签

每次分析会保存到本地 SQLite：

```text
review_center_ai.sqlite3
```

保存内容包括：

- 传统、LLM、融合结果和最终动作
- Prompt 与个人知识版本哈希
- 输入、输出、缓存 Token
- 次日和 5 日概率
- 后续真实价格标签
- MAE、MFE、最大回撤、是否触发止损
- 人工反馈

原始请求/响应按配置保留，普通记录默认 30 天；超过配置 Token 阈值的大请求默认 7 天。原始目录为 `review_center_ai_raw/`，这些文件已加入 Git 忽略。

标签定义：

- 次日标签：下一交易日收盘价高于分析时当前收盘价。
- 5 日标签：第 5 个交易日收盘价高于分析时当前收盘价。
- 路径标签：期间 MAE、MFE、最大回撤和止损路径同时记录。

当后续 K 线数据出现时，系统在刷新票池时自动补齐待标注记录。

## 9. 反馈、评估与持续优化

### 9.1 查看表现

接口：

```text
http://127.0.0.1:8765/api/ai/performance
```

报告分别统计次日和 5 日窗口：

- `sample_count`：已完成标签的样本数。
- `brier_score`：概率预测误差，越低越好。
- `auc`：排序区分能力，标签只有单一类别时可能为空。
- `precision` / `recall`：按 0.5 概率阈值转成方向后的精确率和召回率。
- `calibration_error`：平均预测概率与实际上涨比例的偏差，越低越好。

样本不足时报告会返回 `insufficient_data`，不要据此调参或发布新版本。

### 9.2 记录人工反馈

通过接口记录对单次分析的判断：

```powershell
$body = @{
  analysis_id = "填写分析结果中的 analysis_id"
  feedback_type = "action_review"
  value = "agree"
  note = "记录当时为什么同意或不同意"
} | ConvertTo-Json

Invoke-RestMethod `
  -Uri http://127.0.0.1:8765/api/ai/feedback `
  -Method Post `
  -ContentType 'application/json' `
  -Body $body
```

建议统一使用有限的反馈类型，例如 `action_review`、`probability_review`、`data_quality`、`knowledge_rule`，并在 `note` 中记录可复核原因。

### 9.3 当前“持续优化”是什么

当前系统已经具备可训练式的数据闭环，但不会未经确认自动改写模型权重或自动发布策略：

```text
每日分析 → 保存预测 → 次日/5日标签 → 计算指标 → 人工反馈
       → 发现偏差 → 修改传统阈值/Prompt/知识规则 → 新版本回测
```

可优化的对象分三层：

1. 传统方法：均线、动量、波动率、阈值和硬风控；先做历史滚动回测。
2. LLM 方法：Prompt、输出约束、知识规则选择和上下文压缩；保持结构化输出不变。
3. 融合方法：传统与 LLM 权重、概率修正上限和动作最多变化一级；先用真实样本评估再调整。

初始融合约束为传统 70%、LLM 最大 30%，评分最大修正 ±10，概率最大修正 ±0.08，动作最多改变一级；8% 持仓硬止损不能被 LLM 降级。模型升级的最小样本量、评估窗口和发布门槛必须根据首批真实数据确定，不预先伪造最优阈值。

### 9.4 推荐的月度迭代流程

1. 固定当前代码、Prompt 和知识库版本，导出表现报告。
2. 按次日/5 日分别检查 Brier、AUC、Precision、Recall 和校准误差。
3. 按市场环境、板块和持仓/观察池分组检查错误案例。
4. 只提出一个可验证修改，例如新增一条规则或调整一个阈值。
5. 在不重用未来数据的滚动区间重新回测。
6. 对比旧版本和新版本的收益、回撤、概率校准、止损路径及交易频率。
7. 通过后更新版本号；未通过则保留为 draft，不覆盖线上配置。

## 10. 常见问题

### 妙想没有 Key，页面是否能运行？

能。行情、指数、传统策略和已配置的 LLM Provider 均可继续使用；只有板块强度、市场广度、情绪和外部事件增强字段暂时缺失。

### LLM 不可用时概率为什么不是 50%？

“模型不可用”和“真实中性”必须区分。不可用时概率为 `null` 并显示状态，避免把网络故障或数据不足伪装成中性预测。

### 修改 YAML 后没有生效？

服务启动时读取配置，修改后必须停止并重新启动 `review_center_server.py`。检查 `/api/ai/status` 的 `config_path`、`provider` 和 `ready`。

### 如何确保不误触真实交易？

当前妙想交易写工具永久禁用；页面模拟交易仅写本地复盘账本。不要把券商密钥或真实交易工具加入本项目配置。
