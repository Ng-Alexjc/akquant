# AKQuant 方案 2 前端设计 QA

**Source visual truth**

- `design_samples/qa/option-2-reference-normalized.png`
- 方案 2 的深色交易数据中台视觉语言；用户追加要求将上证、创业板、账户收益、账户权益置于“今日研判”最上方，因此该信息条属于已确认的产品性扩展。

**Implementation evidence**

- `design_samples/qa/review-center-implementation-1440.png`
- `design_samples/qa/backtest-report-implementation.png`
- `design_samples/qa/option2-top-comparison-normalized.png`
- Current browser verification additionally covers `signal_center.html`, the independent signal-center route.

**Viewport and normalization**

- Source: 1265 × 712 px, normalized desktop capture, CSS viewport equivalent 1265 × 712, density 1.
- Review center: 1265 × 712 px, CSS viewport 1265 × 712, density 1.
- Backtest report: 1265 × 712 px, CSS viewport 1265 × 712, density 1.
- Additional responsive verification: 768 × 900 CSS px in the in-app browser.
- State: dark theme, live review-center data loaded, default report summary state.

**Full-view comparison evidence**

- The implementation preserves the reference's fixed navigation rail, compact top command area, dark navy panels, blue-violet accent, dense metric bands, large chart workspace, and right-side signal queue.
- The user-requested market/account strip is intentionally inserted above the strategy judgement strip. It does not displace primary navigation or remove any original functional module.
- The backtest report carries the same navigation, typography hierarchy, panel density, status colors, and chart surface tokens, so the two pages read as one system.

**Focused region comparison evidence**

- Above-the-fold comparison is recorded in `design_samples/qa/option2-top-comparison-normalized.png` because the user-requested information hierarchy change is concentrated in this region.
- Market/account values, strategy judgement, compact KPI band, chart header, and signal queue were inspected at readable size.

**Required fidelity surfaces**

- Fonts and typography: system-safe Chinese stack with Inter preference, tabular numerals, compact 9–24 px hierarchy, consistent weights and line heights; no actionable mismatch.
- Spacing and layout rhythm: 12–15 px panel rhythm, compact 34–44 px controls/headers, aligned grid borders, and consistent navigation widths; no actionable mismatch after the 820 px report breakpoint fix.
- Colors and visual tokens: navy background, layered blue panels, blue-violet primary accent, red/green market semantics, and muted slate text are consistent across both pages.
- Image and icon fidelity: existing AKQuant logo asset is reused; interface icons use the Phosphor icon library. No placeholder, emoji, CSS-art, or handcrafted SVG replacement remains in the redesigned surfaces.
- Copy and content: page titles, market/account labels, strategy status, signal queue, report modules, and live-data notices are product-specific and tied to real page data.

**Interaction and browser verification**

- Theme switch toggled dark → light → dark successfully.
- Review-center refresh completed and returned the button to its enabled state.
- Backtest-report refresh completed with the live-data success notice.
- Review-to-report and report-to-review link targets were verified from the rendered DOM.
- Review-to-signal-center navigation opens `signal_center.html`; the independent page loads the complete signal queue, supports 全部/可执行/观察 filters and search, and expands detailed traditional/LLM/fusion reasoning.
- Review center no longer renders the account equity curve; the report retains the consolidated 权益与回撤 chart section. The report no longer renders the 交易复盘 K 线 section.
- 768 px report layout switches from the side rail to a top navigation and has no horizontal page overflow (`scrollWidth 753 <= innerWidth 768`).
- Browser console checked on both pages: no error-level entries.

**Findings**

- No remaining P0, P1, or P2 issues.
- P3: the compact metric band may benefit from optional user-configurable KPI ordering in a later product iteration; this is outside the selected visual implementation scope.

**Comparison history**

1. Initial responsive pass found a P2 issue at 768 px: the backtest report retained the compressed side rail and produced horizontal overflow.
2. Fix: moved the full-width/top-navigation breakpoint from 760 px to 820 px.
3. Post-fix evidence: 768 px layout uses a single-column shell with static top navigation and no page-level horizontal overflow.
4. Feature split pass: moved the full signal queue to `signal_center.html`, replaced the review-center queue with a compact entry card, and removed the report's 交易复盘 section while retaining equity and drawdown reporting.

**Implementation checklist**

- [x] Scheme 2 visual language applied to review center.
- [x] Shanghai index, ChiNext index, account return, and account equity placed at the top of Today Judgement.
- [x] Strategy judgement, KPI band, K-line workspace, signals, positions, watchlist, and trades retained.
- [x] Backtest report restyled with matching chart theme and navigation.
- [x] Desktop and tablet-width browser verification completed.
- [x] Independent signal center verified with live queue data and expandable reasoning sections.
- [x] Account equity curve consolidated into the backtest report; transaction-review K-line section removed.

final result: passed
