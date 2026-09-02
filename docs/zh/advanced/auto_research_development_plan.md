# AKQuant 本地持仓池自动研究与交易开发计划

## 目标与边界

本系统服务于 A 股短线趋势波段，使用复盘中心本地状态文件作为唯一账户来源：

- `positions`：当前持仓、数量、成本；
- `watchlist`：观察票池；
- `manual_trades`：本地模拟交易流水。

系统不接入真实账户，不向券商下单。自动执行只写入本地模拟持仓和交易记录。

## 总体闭环

```text
本地持仓池/观察池
  → 数据快照与版本
  → 特征/因子计算
  → 标签生成
  → 传统选股与组合构建
  → ML challenger
  → AKQuant 回测
  → 绩效、风险、稳健性评估
  → 研究记录与经验总结
  → 发布审批/自动交易门控
  → 更新本地模拟持仓
```

## P0：研究基础设施

### 必须完成

- [x] 数据集版本与输入哈希；
- [x] 当前持仓池/观察池快照；
- [x] Feature Registry；
- [x] 统一标签生成器；
- [x] 研究运行记录和实验工件；
- [x] 数据质量与未来函数检查；
- [x] 结果可重放。

### 验收标准

同一数据版本、特征版本、标签版本和参数版本重复运行时，输入和结果可追溯。

## P1：传统策略自动研究

### 必须完成

- [x] 趋势跟随基线；
- [x] 突破/回踩基线；
- [x] 动量与市场状态基线；
- [x] 横截面排名与仓位分配；
- [x] 真实交易成本、滑点、T+1、涨跌停约束（日线近似）；
- [x] Grid Search + Walk-Forward；
- [x] 多目标绩效评估；
- [x] HTML/JSON/Parquet 研究报告（Parquet 引擎可选）。

### 验收标准

当前持仓池和观察池可以一键完成选股、组合构建、回测和报告生成。

## P2：ML 增强

### 必须完成

- [x] Logistic Regression 基线；
- [x] Gradient Boosting challenger；
- [x] Meta-Labeling（以传统 selection score 作为基信号）；
- [x] 概率校准（Platt sigmoid）；
- [x] AUC、Brier、LogLoss、Precision@K、Calibration Error；
- [x] Logistic 正则强度网格与 AUC/Brier 联合门槛筛选；原始概率基线仅保留为审计对照；
- [x] 特征重要性；
- [x] 时间外训练/测试；
- [x] challenger 只评估、不自动替换线上逻辑。

### 验收标准

ML 模型必须在样本外窗口与传统策略对比，不能只看训练集指标，也不能把不可用概率伪装成中性概率。

## P3：优化与发布

### 已完成的完整版

- [x] Optuna TPE Bayesian sampler；
- [x] MedianPruner 中间步骤剪枝；
- [x] SQLite trial 数据库、命名 study 与断点续跑；
- [x] 保守复合目标优化 + Pareto 多目标筛选；
- [x] 完整 CPCV 组合折叠及逐折 purge/embargo；
- [x] Deflated Sharpe Ratio、PBO 近似稳健性指标；
- [x] Champion/Challenger 注册；
- [x] 发布申请、门槛审计、批准/拒绝；
- [x] 注册表版本快照、发布历史和一键回滚；
- [x] 模型发布管理页面；
- [x] 纸面交易门控和本地发布记录；
- [x] 每个工作日 19:00 自动运行本地纸面研究与交易。
- [x] 本地自动交易同时最多持有 4 只标的，卖出优先、买入按评分/上涨概率/板块强度排序；
- [x] `momentum_regime v4` 波动率过滤、风险调整动量、ATR 移动止损、最长持仓和过热动量约束；
- [x] 重复回测向量化预计算，Optuna/WFO/CPCV 复用同一份指标序列；
- [x] 定时入口按 `run_id` 等待后台任务，禁止超时后回退产生重复交易。

### 后续可选的研究增强（不阻塞 P3 发布闭环）

- [ ] 基于完整收益路径矩阵的学术口径 DSR/PBO 与 Bootstrap 置信区间；
- [ ] 多机分布式 Optuna worker 和远程数据库；
- [ ] 纸面灰度发布与自动回滚观察窗口。

## P4：高级模型与经验系统

- [ ] TCN/LSTM/Transformer challenger；
- [ ] 强化学习仓位/执行 challenger；
- [ ] LLM 生成候选因子和策略假设；
- [ ] 自动失败归因；
- [ ] 经验卡片审核和知识检索闭环。

## 当前实现状态（2026-08-31）

### 已完成

- 本地持仓池驱动的基线回测；
- 研究运行记录 `research_runs`；
- 次日/五日标签回填；
- 趋势强度、波动率、突破距离、回撤距离、成交量 Z-Score；
- 轻量 challenger 训练评估；
- 信号中心中的回测、标签、指标、训练和本地模拟执行按钮；
- 研究 API：`/api/research/status`、`/api/research/runs`、`/api/research/run`、`/api/research/execute`。
- `research_artifacts/` 输出 JSON + HTML；安装 Parquet 引擎时额外输出 Parquet；
- `full` 已串联数据快照、质量检查、三类策略、Walk-Forward、标签、绩效和 challenger 训练。
- P3 已增加 Pareto 多目标优化、Purged Walk-Forward、稳健性指标和模型注册；自动交易已从研究流水线拆分为独立 `daily_trade` 动作。
- P3 完整版已接入 Optuna 4.x：TPE、MedianPruner、`research_artifacts/optuna_trials.sqlite3` 持久化和同名 study 断点续跑；
- P3 验证已升级为完整 CPCV，每个测试组组合独立生成训练/测试折，并保存 purge、embargo 与标签窗口隔离范围；
- P3 已增加 `/api/models`、发布门槛、申请、批准、拒绝和回滚 API，以及 `model_management.html` 管理页面；
- 发布只允许 Challenger 申请，审批时重新计算门槛，强制覆盖必须填写原因；每次发布和回滚都会保存注册表快照。
- 复盘中心服务已内置工作日 19:00 原生调度，直接执行本地 Python 交易逻辑，不启动 Codex/LLM/Agent；原 Codex 应用自动化“AKQuant 每日19点本地纸面交易”已暂停，避免重复触发。
- 已增加当前用户级 Windows 登录自启动项 `AKQuantReviewCenter`，仅静默启动本地复盘服务，保证重启后内置调度可继续工作。
- `scripts/run_daily_auto.py` 作为命令行/故障恢复入口，只调用 `/api/automation/trade`：刷新当日行情，读取已发布 Champion 与指标规则，执行本地模拟买卖；不回测、不训练、不优化、不发布模型、不调用 LLM/Agent。交易审计独立写入 `automation_runs`。
- `momentum_regime v4` 已在 40 只观察/持仓标的、14,431 行数据上完成真实 Optuna + CPCV/PBO 研究，并以非强制审批发布为 Champion；发布时 10/10 门槛通过。
- 当前 Champion 最优回测指标：Sharpe 4.385、总收益 85.86%、最大回撤 -3.82%；CPCV 平均 Sharpe 2.644、正收益折比例 90%。
- v6 已将波动率、风险调整动量、ATR 移动止损和最长持仓风控统一到 `trend_swing` 与 `breakout_pullback`；突破回踩增加严格模式及“趋势确认 + 突破/回踩排序叠加”模式，Optuna 会先验证一组确定性稳健基准再继续 TPE 搜索。
- v6 三个 Challenger 的发布门槛均为 10/10：`trend_swing` Sharpe 4.385、回撤 -3.82%、CPCV 平均 Sharpe 2.644；`breakout_pullback` Sharpe 4.385、回撤 -3.82%、CPCV 平均 Sharpe 3.916、正收益折比例 100%。当前仍保留 v4 Champion，未自动发布 v6。
- 2026-08-31 自动补跑已完成两笔本地模拟买入，最终持仓 4 只；后续买入按排序因持仓上限被审计跳过。

### 本轮开发目标（已完成）

完成 P0-P2 的可运行版本：

1. 统一研究数据、特征、标签和工件；
2. 将传统策略、横截面组合和 WFO 接入本地持仓池；
3. 将 ML challenger、Meta-Labeling、校准和特征重要性接入研究流程。

### 安全原则

- LLM、ML 和新因子不得绕过本地硬风控；
- 研究默认只生成候选，不自动发布；
- 页面手动模拟执行必须显式确认；交易日 19:00 自动任务通过门控后无需人工确认；
- 所有失败任务都要记录状态和错误；
- 没有足够样本时返回 `insufficient_data`，不填充虚假的有效指标。
