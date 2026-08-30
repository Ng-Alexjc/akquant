(() => {
  "use strict";
  const actions = document.querySelector(".actions");
  if (!actions || document.querySelector(".research-advanced")) return;

  const full = document.getElementById("research-full");
  if (full) {
    full.innerHTML = '<i class="ph ph-play"></i>一键研究';
    full.title = "运行数据检查、标签、三类策略回测、P3 稳健性优化和候选模型评估";
  }
  const analyze = document.getElementById("analyze-selected");
  if (analyze) analyze.title = "只对勾选股票调用 LLM，不运行全池回测";
  const refresh = document.getElementById("refresh");
  if (refresh) refresh.title = "强制更新全池行情；普通页面切换只读取缓存";

  const advanced = document.createElement("details");
  advanced.className = "research-advanced";
  const summary = document.createElement("summary");
  summary.innerHTML = '<i class="ph ph-sliders-horizontal"></i>高级研究';
  summary.title = "故障排查或只运行某一研究阶段时使用";
  advanced.appendChild(summary);
  const panel = document.createElement("div");
  panel.className = "research-advanced-panel";
  advanced.appendChild(panel);

  const descriptions = {
    "research-backtest": "仅运行传统策略回测与 Walk-Forward",
    "research-labels": "补齐已有预测的次日/五日真实结果",
    "research-metrics": "重新汇总历史预测准确率与校准指标",
    "research-train": "训练并评估 ML challenger，不发布线上模型",
  };
  Object.entries(descriptions).forEach(([id, title]) => {
    const button = document.getElementById(id);
    if (button) {
      button.title = title;
      panel.appendChild(button);
    }
  });
  actions.insertBefore(advanced, document.getElementById("theme"));

  const execute = [...actions.querySelectorAll("button")].find((button) =>
    button.textContent.includes("执行本地模拟信号")
  );
  if (execute) execute.title = "按卖出优先、评分、上涨概率、板块强度排序写入本地模拟持仓";

  const style = document.createElement("style");
  style.textContent = `
    .research-advanced{position:relative}
    .research-advanced>summary{list-style:none;cursor:pointer;border:1px solid var(--line);border-radius:6px;padding:8px 11px;color:var(--muted);white-space:nowrap}
    .research-advanced>summary::-webkit-details-marker{display:none}
    .research-advanced[open]>summary{color:var(--text);border-color:var(--blue)}
    .research-advanced-panel{position:absolute;right:0;top:calc(100% + 8px);z-index:30;display:grid;gap:6px;min-width:220px;padding:10px;background:var(--panel);border:1px solid var(--line);border-radius:8px;box-shadow:0 14px 36px rgba(0,0,0,.35)}
    .research-advanced-panel button{width:100%;text-align:left;justify-content:flex-start}
    .hero #research-status.research-status-prominent{display:flex;align-items:center;gap:8px;min-height:36px;margin:10px 0 0;padding:8px 11px;border:1px solid var(--line);border-radius:7px;background:var(--soft);color:var(--text);font-size:11px;line-height:1.5}
    .hero #research-status.research-running{border-color:color-mix(in srgb,var(--blue) 70%,var(--line));box-shadow:inset 3px 0 0 var(--blue)}
    .hero #research-status.research-success{border-color:color-mix(in srgb,var(--down) 70%,var(--line));box-shadow:inset 3px 0 0 var(--down)}
    .hero #research-status.research-failed{border-color:color-mix(in srgb,var(--up) 70%,var(--line));box-shadow:inset 3px 0 0 var(--up)}
    .research-spinner{width:14px;height:14px;flex:0 0 auto;border:2px solid color-mix(in srgb,var(--blue) 30%,transparent);border-top-color:var(--blue);border-radius:50%;animation:research-spin .8s linear infinite}
    .research-toast{position:fixed;right:22px;bottom:22px;z-index:100;max-width:430px;padding:12px 14px;border:1px solid var(--line);border-radius:8px;background:#18233a;color:#eef2ff;box-shadow:0 14px 40px rgba(0,0,0,.45);font-size:11px;line-height:1.55;opacity:0;transform:translateY(8px);pointer-events:none;transition:.18s ease}
    .research-toast.show{opacity:1;transform:translateY(0)}
    .research-toast.failed{border-color:#7a3340;color:#ff9aa5}
    @keyframes research-spin{to{transform:rotate(360deg)}}
  `;
  document.head.appendChild(style);

  const statusNode = document.getElementById("research-status");
  const researchButtons = [
    "research-full",
    "research-backtest",
    "research-labels",
    "research-metrics",
    "research-train",
  ]
    .map((id) => document.getElementById(id))
    .filter(Boolean);
  const toast = document.createElement("div");
  toast.className = "research-toast";
  toast.setAttribute("role", "status");
  toast.setAttribute("aria-live", "polite");
  document.body.appendChild(toast);
  if (statusNode) {
    statusNode.classList.add("research-status-prominent");
    statusNode.setAttribute("role", "status");
    statusNode.setAttribute("aria-live", "polite");
  }

  const storageKey = "akquant_active_research_run";
  let activeRunId = sessionStorage.getItem(storageKey) || "";
  let activeStartedAt = null;
  let pendingSubmission = activeRunId === "pending";
  let toastTimer = 0;
  let pollTimer = 0;
  let elapsedTimer = 0;

  function notify(message, failed = false) {
    toast.textContent = message;
    toast.className = `research-toast show${failed ? " failed" : ""}`;
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => {
      toast.className = "research-toast";
    }, 6000);
  }

  function elapsedText(startedAt) {
    const start = new Date(startedAt || activeStartedAt || Date.now()).getTime();
    const seconds = Math.max(0, Math.floor((Date.now() - start) / 1000));
    const minutes = Math.floor(seconds / 60);
    return `${String(minutes).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
  }

  function setButtonsBusy(busy) {
    researchButtons.forEach((button) => {
      button.disabled = busy;
    });
    if (full) {
      full.innerHTML = busy
        ? '<span class="research-spinner" aria-hidden="true"></span>研究运行中'
        : '<i class="ph ph-play"></i>一键研究';
    }
  }

  function renderRunning(run) {
    activeRunId = run.run_id;
    activeStartedAt = run.started_at;
    pendingSubmission = false;
    sessionStorage.setItem(storageKey, activeRunId);
    setButtonsBusy(true);
    if (statusNode) {
      statusNode.className = "status research-status-prominent research-running";
      statusNode.innerHTML = `<span class="research-spinner" aria-hidden="true"></span><span>一键研究正在运行 · ${run.action} · 已用时 ${elapsedText(run.started_at)} · run_id=${run.run_id}<br>页面可以继续浏览，完成后会自动提示。</span>`;
    }
    window.clearInterval(elapsedTimer);
    elapsedTimer = window.setInterval(() => {
      if (activeRunId && statusNode) renderRunning({ ...run, run_id: activeRunId, started_at: activeStartedAt });
    }, 1000);
  }

  function finishRun(run) {
    const failed = run.status === "failed";
    const label = failed ? "研究失败" : "研究完成";
    const detail = failed ? run.error || "未返回错误详情" : `run_id=${run.run_id}`;
    sessionStorage.removeItem(storageKey);
    activeRunId = "";
    activeStartedAt = null;
    pendingSubmission = false;
    window.clearInterval(elapsedTimer);
    setButtonsBusy(false);
    if (statusNode) {
      statusNode.className = `status research-status-prominent ${failed ? "research-failed" : "research-success"}`;
      statusNode.textContent = `${label} · ${run.action} · ${detail}`;
    }
    notify(`${label}：${detail}`, failed);
    window.alert(`${label}\n${detail}`);
  }

  async function pollResearchRun() {
    try {
      const response = await fetch("/api/research/runs?limit=10", { cache: "no-store" });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
      const runs = Array.isArray(body.items) ? body.items : [];
      let run = activeRunId && activeRunId !== "pending"
        ? runs.find((item) => item.run_id === activeRunId)
        : null;
      if (!run) run = runs.find((item) => item.status === "running") || null;
      if (run && run.status === "running") {
        renderRunning(run);
        return;
      }
      if (activeRunId && activeRunId !== "pending") {
        const finished = runs.find((item) => item.run_id === activeRunId);
        if (finished && ["completed", "failed"].includes(finished.status)) finishRun(finished);
      } else if (pendingSubmission && statusNode) {
        statusNode.className = "status research-status-prominent research-running";
        statusNode.innerHTML = '<span class="research-spinner" aria-hidden="true"></span><span>一键研究已提交，正在创建任务记录…</span>';
      }
    } catch (error) {
      if (statusNode && (activeRunId || pendingSubmission)) {
        statusNode.textContent = `研究任务状态读取失败：${error.message || error}`;
      }
    }
  }

  async function submitOneClickResearch() {
    try {
      const response = await fetch("/api/research/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          action: "full",
          calendar_days: 60,
          refresh: true,
          background: true,
        }),
        cache: "no-store",
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
      activeRunId = body.run_id;
      pendingSubmission = false;
      sessionStorage.setItem(storageKey, activeRunId);
      renderRunning({
        run_id: body.run_id,
        action: body.action || "full",
        status: "running",
        started_at: body.started_at || activeStartedAt || new Date().toISOString(),
      });
      notify(`一键研究已在后台运行：${body.run_id}`);
    } catch (error) {
      sessionStorage.removeItem(storageKey);
      activeRunId = "";
      pendingSubmission = false;
      setButtonsBusy(false);
      if (statusNode) {
        statusNode.className = "status research-status-prominent research-failed";
        statusNode.textContent = `一键研究启动失败：${error.message || error}`;
      }
      notify(`一键研究启动失败：${error.message || error}`, true);
      window.alert(`一键研究启动失败\n${error.message || error}`);
    }
  }

  document.addEventListener(
    "click",
    (event) => {
      const button = event.target.closest("#research-full");
      if (!button) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      if (activeRunId) {
        notify(`已有研究任务正在运行：${activeRunId}`);
        return;
      }
      activeRunId = "pending";
      pendingSubmission = true;
      activeStartedAt = new Date().toISOString();
      sessionStorage.setItem(storageKey, activeRunId);
      setButtonsBusy(true);
      if (statusNode) {
        statusNode.className = "status research-status-prominent research-running";
        statusNode.innerHTML = '<span class="research-spinner" aria-hidden="true"></span><span>一键研究已提交，正在初始化数据与研究任务…</span>';
      }
      notify("一键研究已开始。页面会持续显示运行状态，完成后自动提示。");
      submitOneClickResearch();
    },
    true,
  );

  pollTimer = window.setInterval(pollResearchRun, 2000);
  pollResearchRun();
})();
