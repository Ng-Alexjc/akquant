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

## 当前实现状态（2026-08-30）

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
- P3 已增加 Pareto 多目标优化、Purged Walk-Forward、稳健性指标、模型注册和 `daily_auto` 自动交易动作。
- P3 完整版已接入 Optuna 4.x：TPE、MedianPruner、`research_artifacts/optuna_trials.sqlite3` 持久化和同名 study 断点续跑；
- P3 验证已升级为完整 CPCV，每个测试组组合独立生成训练/测试折，并保存 purge、embargo 与标签窗口隔离范围；
- P3 已增加 `/api/models`、发布门槛、申请、批准、拒绝和回滚 API，以及 `model_management.html` 管理页面；
- 发布只允许 Challenger 申请，审批时重新计算门槛，强制覆盖必须填写原因；每次发布和回滚都会保存注册表快照。
- 已创建应用自动化“AKQuant 每日19点本地纸面交易”，只调用 `scripts/run_daily_auto.py`，仅写入本地状态。

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
