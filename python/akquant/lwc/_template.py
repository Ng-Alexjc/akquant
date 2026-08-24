r"""LWC 复盘 HTML 模板与安全渲染.

单文件自包含:内联 vendored LWC standalone JS + 数据 payload。渲染时:

- 标题经 :func:`html.escape` 转义,防 HTML 注入;
- payload 用 JSON 序列化后把 ``<`` / ``>`` / ``&`` 转成 ``\uXXXX``,
  防止 ``</script>`` 提前闭合 script 标签(XSS);
- 用占位符 ``.replace()`` 注入,避免 ``str.format`` 与 JS/CSS 花括号冲突。
"""

from __future__ import annotations

import html
import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

_ASSETS = Path(__file__).parent / "assets"
_LWC_JS = _ASSETS / "lightweight-charts.standalone.production.js"


@lru_cache(maxsize=1)
def _load_lwc_js() -> str:
    """读取 vendored LWC standalone JS(带缓存)."""
    return _LWC_JS.read_text(encoding="utf-8")


def _safe_json(obj: Any) -> str:
    """JSON 序列化并转义可闭合 script 标签的字符,可安全嵌入 <script>."""
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return raw.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


# 占位符用 %%NAME%% 形式,避开 JS/CSS 的 {}。页面 chrome 用 CSS 变量,
# 明暗切换时只改 <html data-theme>,无需重建 DOM。
_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh" data-theme="%%INIT_THEME%%">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>%%TITLE%%</title>
<style>
  :root[data-theme="light"]{--bg:#f5f7fb;--surface:#ffffff;--surface-soft:#f8fafc;--text:#162033;--muted:#68758a;--grid:#e6ebf2;--border:#e1e7ef;--accent:#2864dc;--up:#d32f2f;--down:#2e7d32;--shadow:0 10px 32px rgba(20,35,60,.07);}
  :root[data-theme="dark"]{--bg:#111827;--surface:#172033;--surface-soft:#1c2940;--text:#e8edf8;--muted:#9ba9bf;--grid:#2c3951;--border:#2d3a52;--accent:#74a7ff;--up:#ff5252;--down:#69f0ae;--shadow:0 10px 32px rgba(0,0,0,.24);}
  *{box-sizing:border-box} html,body{margin:0;min-height:100%;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",Roboto,sans-serif;}
  button,select,input{font:inherit} button{cursor:pointer}.app{max-width:1680px;margin:0 auto;padding:20px 24px 28px}.topbar{display:flex;align-items:center;gap:16px;margin-bottom:20px}.brand{min-width:0}.eyebrow{margin:0 0 4px;color:var(--accent);font-size:12px;font-weight:700;letter-spacing:.08em}.brand h1{margin:0;font-size:21px;letter-spacing:-.02em}.controls{display:flex;align-items:center;gap:8px;margin-left:auto}.select,.button{height:34px;padding:0 10px;border:1px solid var(--border);border-radius:8px;background:var(--surface);color:var(--text);font-size:13px}.button{font-weight:600}.button:hover,.trade-row:hover,.pool-main:hover{border-color:var(--accent)}.button.secondary{background:transparent}.button.danger{color:var(--down)}.metrics{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:12px;margin-bottom:16px}.metric{min-width:0;padding:14px 15px;background:var(--surface);border:1px solid var(--border);border-radius:12px;box-shadow:var(--shadow)}.metric .label{display:block;color:var(--muted);font-size:12px;margin-bottom:6px}.metric strong{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:20px;letter-spacing:-.03em}.metric strong.positive{color:var(--up)}.metric strong.negative{color:var(--down)}.workspace{display:grid;grid-template-columns:minmax(0,1.75fr) minmax(320px,.75fr);gap:16px}.stack{display:grid;gap:16px;align-content:start}.card{min-width:0;overflow:hidden;background:var(--surface);border:1px solid var(--border);border-radius:12px;box-shadow:var(--shadow)}.card-header{display:flex;align-items:center;gap:10px;min-height:48px;padding:11px 14px;border-bottom:1px solid var(--border)}.card-header h2{margin:0;font-size:14px}.card-header .hint{margin-left:auto;overflow:hidden;color:var(--muted);font-size:12px;text-overflow:ellipsis;white-space:nowrap}.chart{width:100%}.price-chart{height:470px}.equity-chart{height:190px}.filter{margin-left:auto}.trade-detail{min-height:170px;padding:15px}.empty{padding:26px 15px;color:var(--muted);font-size:13px;text-align:center}.detail-title{display:flex;align-items:center;gap:8px;margin-bottom:14px;font-size:15px;font-weight:700}.badge{padding:3px 7px;border-radius:999px;background:var(--surface-soft);color:var(--muted);font-size:11px;font-weight:700}.detail-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px 16px}.detail-item span{display:block;color:var(--muted);font-size:11px;margin-bottom:3px}.detail-item strong{font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.trade-list{max-height:570px;overflow:auto;padding:8px}.trade-row{width:100%;display:grid;grid-template-columns:1fr auto;gap:8px;align-items:center;padding:11px 10px;margin:0 0 4px;border:1px solid transparent;border-radius:9px;background:transparent;color:var(--text);text-align:left}.trade-row.active{border-color:var(--accent);background:var(--surface-soft)}.trade-row .trade-main{min-width:0}.trade-row .trade-symbol{display:block;overflow:hidden;font-size:13px;font-weight:700;text-overflow:ellipsis;white-space:nowrap}.trade-row .trade-meta{display:block;margin-top:3px;color:var(--muted);font-size:11px}.trade-row .trade-pnl{font-size:13px;font-weight:700}.positive{color:var(--up)!important}.negative{color:var(--down)!important}.footer-note{padding:10px 14px;border-top:1px solid var(--border);color:var(--muted);font-size:11px}.range-reset{margin-left:auto}.pool-form{display:flex;gap:8px;flex-wrap:wrap;padding:10px 12px;border-bottom:1px solid var(--border);background:var(--surface-soft)}.pool-form input{flex:1;min-width:130px;height:32px;padding:0 9px;border:1px solid var(--border);border-radius:7px;background:var(--surface);color:var(--text)}.pool-results{width:100%;display:grid;gap:5px}.pool-result{padding:7px 9px;border:1px solid var(--border);border-radius:7px;background:var(--surface);color:var(--text);text-align:left}.pool-table-wrap{overflow-x:auto}.pool-table{width:100%;min-width:940px;border-collapse:collapse;font-size:12px}.pool-table th{padding:9px 10px;background:var(--surface-soft);color:var(--muted);font-size:11px;font-weight:700;text-align:right;white-space:nowrap;border-bottom:1px solid var(--border)}.pool-table th:first-child,.pool-table td:first-child{text-align:left}.pool-table td{padding:8px 10px;border-bottom:1px solid var(--border);text-align:right;white-space:nowrap;vertical-align:middle}.pool-table tbody tr:last-child td{border-bottom:0}.pool-table tbody tr:hover{background:var(--surface-soft)}.pool-symbol{display:inline-flex;flex-direction:column;gap:2px;min-width:112px;padding:0;border:0;background:transparent;color:var(--text);text-align:left}.pool-symbol strong{font-size:12px}.pool-symbol span{color:var(--muted);font-size:11px}.pool-input{width:86px;height:30px;padding:0 7px;border:1px solid var(--border);border-radius:7px;background:var(--surface);color:var(--text);text-align:right}.pool-table .pool-actions{justify-content:flex-end}@media(max-width:1100px){.metrics{grid-template-columns:repeat(3,minmax(0,1fr))}.workspace{grid-template-columns:1fr}.trade-list{max-height:310px}}@media(max-width:650px){.app{padding:14px}.topbar{align-items:flex-start;flex-direction:column}.controls{width:100%;margin-left:0}.controls .select{flex:1}.metrics{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.metric{padding:11px}.metric strong{font-size:17px}.price-chart{height:390px}.detail-grid{gap:10px}.card-header{flex-wrap:wrap}.card-header .hint{margin-left:0;max-width:100%}.pool-table{min-width:940px}}
  .trade-form{display:grid;gap:8px;padding:10px 12px;border-bottom:1px solid var(--border);background:var(--surface-soft)}.trade-form .trade-search{display:flex;gap:8px}.trade-form input{min-width:0;height:32px;padding:0 9px;border:1px solid var(--border);border-radius:7px;background:var(--surface);color:var(--text)}.trade-form .trade-search input{flex:1}.trade-fields{display:grid;grid-template-columns:1fr 1fr;gap:8px}.trade-actions{display:flex;gap:8px}.trade-actions .button{flex:1}.button.trade-buy{color:var(--up);border-color:var(--up)}.button.trade-sell{color:var(--down);border-color:var(--down)}.signal-list{padding:8px}.signal-row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px;align-items:center;padding:10px;border-bottom:1px solid var(--border)}.signal-row:last-child{border-bottom:0}.signal-main{min-width:0}.signal-symbol{display:block;font-size:13px;font-weight:700}.signal-meta{display:block;margin-top:3px;color:var(--muted);font-size:11px}.signal-action{font-size:13px;font-weight:800}.signal-action.buy{color:var(--up)}.signal-action.sell{color:var(--down)}.signal-action.add{color:#d08b20}.signal-action.hold{color:var(--muted)}
  /* 首行展示市场温度；账户与交易指标在桌面端固定排成两行。 */
  :root[data-theme="light"] .button.danger{color:#dc4c64}
  :root[data-theme="dark"] .button.danger{color:#fa7387}
  .metrics{grid-template-columns:repeat(14,minmax(0,1fr))}
  .metric{grid-column:span 2}
  .metric.market-index{grid-column:span 7;padding:17px 20px}
  .metric.market-index strong{font-size:25px}
  @media(max-width:1100px){.metrics{grid-template-columns:repeat(6,minmax(0,1fr))}.metric{grid-column:span 1}.metric.market-index{grid-column:span 3}}
  @media(max-width:650px){.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.metric.market-index{grid-column:span 2}}
</style>
</head>
<body>
<main class="app">
  <header class="topbar">
    <div class="brand"><p class="eyebrow">AKQUANT · TRADE REVIEW CENTER</p><h1>%%TITLE%%</h1></div>
    <div class="controls">
      <select id="sym" class="select" aria-label="标的选择"></select>
      <select id="trade-filter" class="select" aria-label="交易结果筛选"><option value="all">全部交易</option><option value="win">盈利交易</option><option value="loss">亏损交易</option></select>
      <a id="report-link" class="button secondary" href="" target="_blank" rel="noopener" hidden>策略回测报告 ↗</a>
      <button id="refresh-data" class="button secondary" type="button" aria-label="刷新当前复盘表数据">↻ 刷新</button>
      <button id="theme-toggle" class="button" type="button" aria-label="切换明暗主题">🌙 暗色</button>
    </div>
  </header>
  <section id="metrics" class="metrics" aria-label="策略核心指标"></section>
  <section class="workspace">
    <div class="stack">
      <section class="card"><div class="card-header"><h2>价格与交易时点</h2><span id="price-hint" class="hint">MA5 橙色 · MA10 紫色 · 下方柱状图为成交量</span><button id="range-reset" class="button secondary range-reset" type="button">查看全部</button></div><div id="price-chart" class="chart price-chart"></div></section>
      <section class="card"><div class="card-header"><h2>观察票池</h2><span id="watch-count" class="hint"></span></div><form id="watch-form" class="pool-form"><input id="watch-search" type="search" placeholder="输入 A 股代码或名称" autocomplete="off"/><button class="button" type="submit">增加</button><div id="watch-results" class="pool-results" hidden></div></form><div id="watch-list" class="pool-table-wrap"></div><div class="footer-note">点击股票名称或号码查看 K 线；行情按打开页面时刷新。</div></section>
      <section class="card"><div class="card-header"><h2>账户权益曲线</h2><span id="equity-hint" class="hint">回测期间的账户市值变化</span></div><div id="equity-chart" class="chart equity-chart"></div></section>
    </div>
    <aside class="stack">
      <section class="card"><div class="card-header"><h2>交易</h2><span class="hint">仅记录复盘模拟交易</span></div><form id="trade-form" class="trade-form"><div class="trade-search"><input id="trade-search" type="search" placeholder="输入股票名称或代码" autocomplete="off"/><button class="button" type="submit">查找</button></div><div id="trade-results" class="pool-results" hidden></div><div id="trade-selected" class="badge">尚未选择标的</div><div class="trade-fields"><input id="trade-price" type="number" min="0" step="any" placeholder="成交价格" aria-label="模拟成交价格"/><input id="trade-quantity" type="number" min="1" step="1" placeholder="成交数量" aria-label="模拟成交数量"/></div><div class="trade-actions"><button id="trade-buy" class="button trade-buy" type="button">买入</button><button id="trade-sell" class="button trade-sell" type="button">卖出</button></div></form></section>
      <section class="card"><div class="card-header"><h2>交易详情</h2><span id="selection-hint" class="hint">尚未选择交易</span></div><div id="trade-detail" class="trade-detail"></div></section>
      <section class="card"><div class="card-header"><h2>交易信号</h2><span class="hint">依据待接入策略分析</span></div><div id="signal-list" class="signal-list"></div></section>
      <section class="card"><div class="card-header"><h2>持仓票池</h2><span id="position-count" class="hint"></span></div><form id="position-form" class="pool-form"><input id="position-search" type="search" placeholder="输入 A 股代码或名称" autocomplete="off"/><button class="button" type="submit">增加</button><div id="position-results" class="pool-results" hidden></div></form><div id="position-list" class="pool-table-wrap"></div><div class="footer-note">点击股票名称或号码切换 K 线；持仓成本和持股数可直接编辑。</div></section>
      <section class="card"><div class="card-header"><h2>交易列表</h2><span id="trade-count" class="hint"></span></div><div id="trade-list" class="trade-list"></div><div class="footer-note">点击交易可联动定位 K 线、成交标记与明细。</div></section>
    </aside>
  </section>
</main>
<script>%%LWC_JS%%</script>
<script id="akq-data" type="application/json">%%DATA%%</script>
<script>%%APP_JS%%</script>
</body>
</html>
"""

# 前端逻辑:v5 API(addSeries(SeriesType,...) / createSeriesMarkers)。
# payload 主题无关(volume 带 up 布尔、marker 带 buy 布尔),颜色在此按当前
# 主题动态计算;切换主题只 applyOptions + 重新上色,不重建数据(大数据量友好)。
_APP_JS = """
(function(){
  var LWC = window.LightweightCharts;
  var cfg = JSON.parse(document.getElementById('akq-data').textContent);
  var themes = cfg.themes || {}, all = cfg.symbols || [], trades = cfg.trades || [], manualTrades = [];
  var summary = cfg.summary || {}, cur = cfg.initial_theme in themes ? cfg.initial_theme : 'light';
  var sel = document.getElementById('sym'), filter = document.getElementById('trade-filter');
  var toggle = document.getElementById('theme-toggle'), reportLink = document.getElementById('report-link'), refreshData = document.getElementById('refresh-data'), metricHost = document.getElementById('metrics');
  var detailHost = document.getElementById('trade-detail'), listHost = document.getElementById('trade-list');
  var tradeForm = document.getElementById('trade-form'), tradeSearch = document.getElementById('trade-search'), tradeResults = document.getElementById('trade-results'), tradeSelected = document.getElementById('trade-selected'), tradePrice = document.getElementById('trade-price'), tradeQuantity = document.getElementById('trade-quantity'), tradeBuy = document.getElementById('trade-buy'), tradeSell = document.getElementById('trade-sell'), signalHost = document.getElementById('signal-list');
  var positionHost = document.getElementById('position-list'), positionCount = document.getElementById('position-count');
  var watchHost = document.getElementById('watch-list'), watchCount = document.getElementById('watch-count');
  var positionForm = document.getElementById('position-form'), positionSearch = document.getElementById('position-search'), positionResults = document.getElementById('position-results');
  var watchForm = document.getElementById('watch-form'), watchSearch = document.getElementById('watch-search'), watchResults = document.getElementById('watch-results');
  var countHost = document.getElementById('trade-count'), selectionHint = document.getElementById('selection-hint');
  var priceHint = document.getElementById('price-hint'), reset = document.getElementById('range-reset');
  var priceHost = document.getElementById('price-chart'), equityHost = document.getElementById('equity-chart');
  var curIdx = cfg.initial_symbol_index || 0, activeTradeId = null, selectedTradeSymbol = null, poolState = {watchlist:[],positions:[],manual_trades:[],signals:[]};
  function T(){return themes[cur] || {};}
  function money(value){return new Intl.NumberFormat('zh-CN',{maximumFractionDigits:2}).format(Number(value || 0));}
  function pct(value){var n=Number(value || 0);return (n>=0?'+':'')+n.toFixed(2)+'%';}
  function signedMoney(value){var n=Number(value || 0);return (n>=0?'+':'')+money(n);}
  function classFor(value){return Number(value || 0)>=0?'positive':'negative';}
  function manualAccount(){var initial=Number(summary.initial_equity||100000),positions=poolState.positions||[],realized=manualTrades.reduce(function(total,trade){return total+Number(trade.net_pnl||0);},0),marketValue=positions.reduce(function(total,item){return total+Number(item.current_price||item.entry_price||0)*Number(item.quantity||0);},0),unrealized=positions.reduce(function(total,item){return total+(Number(item.current_price||item.entry_price||0)-Number(item.entry_price||0))*Number(item.quantity||0);},0),totalPnl=realized+unrealized,equity=initial+totalPnl;return {initial:initial,equity:equity,realized:realized,unrealized:unrealized,marketValue:marketValue,totalPnl:totalPnl,returnPct:initial?totalPnl/initial*100:0,positionCount:positions.length,shareCount:positions.reduce(function(total,item){return total+Number(item.quantity||0);},0),positionPct:equity?marketValue/equity*100:0};}
  function applyLocalTrade(trade){var symbol=trade.symbol,price=Number(trade.price||0),quantity=Number(trade.quantity||0),position=(poolState.positions||[]).find(function(item){return item.symbol===symbol;});if(trade.action==='buy'){if(position){var oldQuantity=Number(position.quantity||0),oldCost=Number(position.entry_price||0);position.quantity=oldQuantity+quantity;position.entry_price=((oldQuantity*oldCost)+(quantity*price))/position.quantity;}else{(poolState.positions||(poolState.positions=[])).push({symbol:symbol,name:trade.name||symbol,quantity:quantity,entry_price:price,current_price:price,previous_price:price,change_pct:0,volume_change_pct:0});}}else if(position){position.quantity=Number(position.quantity||0)-quantity;if(position.quantity<=1e-12)poolState.positions=poolState.positions.filter(function(item){return item.symbol!==symbol;});}}
  function displayTime(value){if(!value)return '—';var date=new Date(value);return isNaN(date.getTime())?String(value):date.toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});}
  function appendText(parent, tag, text, className){var el=document.createElement(tag);if(className)el.className=className;el.textContent=text;parent.appendChild(el);return el;}
  function api(path, options){return fetch(path, options || {}).then(function(response){return response.json().then(function(body){if(!response.ok)throw new Error(body.error || ('请求失败 '+response.status));return body;});});}
  function jsonOptions(method, body){return {method:method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)};}
  function showResults(host, items, kind){host.replaceChildren();host.hidden=!items.length;if(!items.length)return;items.forEach(function(item){var button=document.createElement('button');button.type='button';button.className='pool-result';button.textContent=item.symbol+' · '+item.name+' · '+(item.market || 'A股');button.addEventListener('click',function(){addPool(kind,item);});host.appendChild(button);});}
  function showTradeResults(items){tradeResults.replaceChildren();tradeResults.hidden=!items.length;if(!items.length)return;items.forEach(function(item){var button=document.createElement('button');button.type='button';button.className='pool-result';button.textContent=item.symbol+' · '+item.name;button.addEventListener('click',function(){selectedTradeSymbol=item.symbol;tradeSelected.textContent=item.name+' · '+item.symbol;tradeResults.replaceChildren();tradeResults.hidden=true;api('/api/stocks/kline?symbol='+encodeURIComponent(item.symbol)).then(function(body){tradePrice.value=body.current_price || '';}).catch(function(){});});tradeResults.appendChild(button);});}
  function searchPool(input, host, kind){var q=input.value.trim();if(!q){showResults(host,[],kind);return;}api('/api/stocks/search?q='+encodeURIComponent(q)).then(function(body){showResults(host,body.items || [],kind);}).catch(function(error){showResults(host,[]);window.alert(error.message);});}
  function addPool(kind, item){var endpoint=kind==='watch'?'watchlist':'positions';var body={symbol:item.symbol};if(kind==='position'){body.quantity=0;body.cost=0;}api('/api/'+endpoint,jsonOptions('POST',body)).then(function(){positionSearch.value=kind==='position'?'':'';watchSearch.value=kind==='watch'?'':'';showResults(kind==='position'?positionResults:watchResults,[]);return refreshPools();}).catch(function(error){window.alert(error.message);});}
  function removePool(kind, symbol){if(!window.confirm('确定从'+(kind==='watch'?'观察':'持仓')+'票池删除 '+symbol+'？'))return;api('/api/'+(kind==='watch'?'watchlist':'positions')+'?symbol='+encodeURIComponent(symbol),{method:'DELETE'}).then(refreshPools).catch(function(error){window.alert(error.message);});}
  function fmtQuote(item){if(item.quote_error)return '行情获取失败';return '现价 '+money(item.current_price)+' · 昨日 '+money(item.previous_price)+' · '+pct(item.change_pct)+' · 量比 '+pct(item.volume_change_pct);}
  function volumeRatio(item){var ratio=1+Number(item.volume_change_pct||0)/100;return Number.isFinite(ratio)?ratio.toFixed(2)+'x':'—';}
  function ensureSeries(symbol){var target=all.findIndex(function(series){return series.symbol===symbol;});if(target>=0)return Promise.resolve(target);return api('/api/stocks/kline?symbol='+encodeURIComponent(symbol)).then(function(body){if(body.series){body.series.name=body.name||body.symbol;all.push(body.series);var option=document.createElement('option');option.value=all.length-1;option.textContent=body.symbol;sel.appendChild(option);return all.length-1;}throw new Error('没有可用 K 线数据');});}
  function openPoolSymbol(item){ensureSeries(item.symbol).then(function(target){sel.value=String(target);activeTradeId=null;filter.value='all';drawSymbol(target,true);}).catch(function(error){window.alert(error.message);});}
  function savePosition(item, quantity, cost, row){var q=Number(quantity), c=Number(cost);if(!Number.isFinite(q)||!Number.isFinite(c)||q<0||c<0){window.alert('数量和成本必须是非负数字');return;}api('/api/positions',jsonOptions('PUT',{symbol:item.symbol,quantity:q,cost:c})).then(function(){item.quantity=q;item.entry_price=c;renderPositions();renderSignals();renderMetrics();renderTrades();}).catch(function(error){window.alert(error.message);});}
  function searchTrade(){var q=tradeSearch.value.trim();if(!q){showTradeResults([]);return;}api('/api/stocks/search?q='+encodeURIComponent(q)).then(function(body){showTradeResults(body.items || []);}).catch(function(error){showTradeResults([]);window.alert(error.message);});}
  function submitSimulatedTrade(action){if(!selectedTradeSymbol){window.alert('请先查找并选择标的');return;}var price=Number(tradePrice.value), quantity=Number(tradeQuantity.value);if(!Number.isFinite(price)||price<=0||!Number.isFinite(quantity)||quantity<=0){window.alert('请输入正数价格和数量');return;}api('/api/simulated-trades',jsonOptions('POST',{symbol:selectedTradeSymbol,action:action,price:price,quantity:quantity})).then(function(body){if(!body.trade)throw new Error('交易记录保存失败');manualTrades=manualTrades.filter(function(item){return item.id!==body.trade.id;});manualTrades.push(normalizeManualTrade(body.trade));applyLocalTrade(body.trade);addManualMarker(manualTrades[manualTrades.length-1]);activeTradeId=body.trade.id;renderPositions();renderSignals();renderTrades();renderMetrics();return ensureSeries(body.trade.symbol);}).then(function(target){sel.value=String(target);drawSymbol(target,true);return refreshPools();}).then(function(){renderTrades();renderMetrics();tradePrice.value='';tradeQuantity.value='';}).catch(function(error){window.alert(error.message);});}
  function signalClass(action){return action==='买入'?'buy':action==='卖出'?'sell':action==='加仓'?'add':'hold';}
  function renderSignals(){signalHost.replaceChildren();var items=(poolState.signals || []).filter(function(signal){return signal.pool==='持仓'||signal.action==='买入'||signal.action==='卖出';});if(!items.length){appendText(signalHost,'div','当前没有持仓或明确买卖信号。','empty');return;}items.forEach(function(signal){var row=document.createElement('div');row.className='signal-row';var main=document.createElement('div');main.className='signal-main';appendText(main,'span',signal.name+' · '+signal.symbol,'signal-symbol');appendText(main,'span',(signal.pool || '')+' · 建议价 '+money(signal.suggested_price)+' · '+signal.reason,'signal-meta');var action=document.createElement('span');action.className='signal-action '+signalClass(signal.action);action.textContent=signal.action || '观望';row.appendChild(main);row.appendChild(action);row.addEventListener('click',function(){openPoolSymbol(signal);});signalHost.appendChild(row);});}
  if(typeof cfg.report_url==='string' && cfg.report_url){var reportHref=cfg.report_url;reportLink.href=reportHref+(reportHref.indexOf('?')>=0?'&':'?')+'live=1';reportLink.hidden=false;}
  all.forEach(function(s,i){var option=document.createElement('option');option.value=i;option.textContent=s.symbol;sel.appendChild(option);});
  var chartOptions={autoSize:true,timeScale:{timeVisible:!!cfg.intraday,secondsVisible:false},crosshair:{mode:LWC.CrosshairMode.Normal},handleScroll:true,handleScale:true};
  var priceChart=LWC.createChart(priceHost,chartOptions);
   var candle=priceChart.addSeries(LWC.CandlestickSeries,{},0);
   var ma5Line=priceChart.addSeries(LWC.LineSeries,{color:'#f59e0b',lineWidth:2,lastValueVisible:true,priceLineVisible:false},0);
   var ma10Line=priceChart.addSeries(LWC.LineSeries,{color:'#8b5cf6',lineWidth:2,lastValueVisible:true,priceLineVisible:false},0);
  var volume=priceChart.addSeries(LWC.HistogramSeries,{priceFormat:{type:'volume'},priceScaleId:''},1);
  volume.priceScale().applyOptions({scaleMargins:{top:0.8,bottom:0}});
  if(priceChart.panes && priceChart.panes()[1])priceChart.panes()[1].setHeight(104);
  var markerPrim=LWC.createSeriesMarkers(candle,[]);
  var equityChart=LWC.createChart(equityHost,{autoSize:true,timeScale:{timeVisible:!!cfg.intraday,secondsVisible:false},handleScroll:true,handleScale:true});
  var equityLine=equityChart.addSeries(LWC.LineSeries,{lineWidth:2,lastValueVisible:true,priceLineVisible:false});
  var equityData=(cfg.equity_curve || []).map(function(point){return {time:point.time,value:point.value};});
  equityLine.setData(equityData);
  function movingAverage(candles,period){var values=[],sum=0;for(var i=0;i<candles.length;i++){var close=Number(candles[i].close||0);sum+=close;if(i>=period)sum-=Number(candles[i-period].close||0);if(i>=period-1)values.push({time:candles[i].time,value:sum/period});}return values;}
  function setMovingAverages(series){var candles=series.candles||[];ma5Line.setData(movingAverage(candles,5));ma10Line.setData(movingAverage(candles,10));}
  function colorVol(series){var t=T();return (series.volume || []).map(function(v){return {time:v.time,value:v.value,color:v.up?t.up:t.down};});}
  function colorMarkers(series){var t=T();return (series.markers || []).map(function(m){return {time:m.time,position:m.position,shape:m.shape,text:m.text,color:m.buy?t.up:t.down};});}
  function styleChart(chart){var t=T();chart.applyOptions({layout:{background:{color:t.bg},textColor:t.text},grid:{vertLines:{color:t.grid},horzLines:{color:t.grid}},rightPriceScale:{borderColor:t.grid},timeScale:{borderColor:t.grid}});}
  function applyTheme(){var t=T();document.documentElement.setAttribute('data-theme',cur);toggle.textContent=cur==='dark'?'☀️ 亮色':'🌙 暗色';styleChart(priceChart);styleChart(equityChart);candle.applyOptions({upColor:t.up,downColor:t.down,borderUpColor:t.up,borderDownColor:t.down,wickUpColor:t.up,wickDownColor:t.down});equityLine.applyOptions({color:t.accent || t.up});ma5Line.applyOptions({color:'#f59e0b'});ma10Line.applyOptions({color:'#8b5cf6'});rebuildManualMarkers();var s=all[curIdx];if(s){setMovingAverages(s);volume.setData(colorVol(s));markerPrim.setMarkers(colorMarkers(s));}}
  function normalizeManualTrade(raw){var isBuy=raw.action==='buy';return {id:raw.id,symbol:raw.symbol,name:raw.name||raw.symbol,side:isBuy?'买入':'卖出',event_time:raw.time,entry_time:raw.time,exit_time:raw.time,entry_chart_time:null,exit_chart_time:null,entry_price:isBuy?Number(raw.price):null,exit_price:isBuy?null:Number(raw.price),quantity:Number(raw.quantity||0),net_pnl:Number(raw.net_pnl||0),return_pct:null,mae:null,mfe:null,duration_bars:null,manual_action:raw.action,_matched_entry:null,_matched_exit:null};}
  function manualTradeDay(value){var text=String(value||'');if(/^[0-9]{4}-[0-9]{2}-[0-9]{2}/.test(text))return text.slice(0,10);var date=new Date(text);return isNaN(date.getTime())?'':date.toISOString().slice(0,10);}
  function manualCandleIndex(candles, value){var day=manualTradeDay(value);if(!candles.length)return -1;var index=candles.findIndex(function(point){return String(point.time)>=day;});return index<0?candles.length-1:index;}
  function calculateManualStats(symbol){var series=all.find(function(item){return item.symbol===symbol;}),candles=series&&series.candles||[];if(!candles.length)return;var symbolTrades=manualTrades.filter(function(trade){return trade.symbol===symbol;}).sort(function(a,b){return String(a.event_time).localeCompare(String(b.event_time));}),lots=[];symbolTrades.forEach(function(trade){trade._matched_entry=null;trade._matched_exit=null;});symbolTrades.forEach(function(trade){if(trade.manual_action==='buy'){lots.push({trade:trade,remaining:Number(trade.quantity||0)});return;}var remaining=Number(trade.quantity||0);while(remaining>1e-12&&lots.length){var lot=lots[0],matched=Math.min(remaining,lot.remaining);if(!trade._matched_entry)trade._matched_entry=lot.trade;if(!lot.trade._matched_exit)lot.trade._matched_exit=trade;lot.remaining-=matched;remaining-=matched;if(lot.remaining<=1e-12)lots.shift();}});symbolTrades.forEach(function(trade){var entry=trade.manual_action==='buy'?trade:trade._matched_entry,exit=trade.manual_action==='sell'?trade:trade._matched_exit;if(!entry||!Number(entry.entry_price||0)){trade.mae=null;trade.mfe=null;trade.return_pct=null;trade.duration_bars=null;return;}var start=manualCandleIndex(candles,entry.event_time),end=manualCandleIndex(candles,(exit||trade).event_time);if(start<0)return;if(end<start)end=start;var window=candles.slice(start,end+1),entryPrice=Number(entry.entry_price),high=Math.max(entryPrice,Math.max.apply(null,window.map(function(point){return Number(point.high||point.close||0);}))),low=Math.min(entryPrice,Math.min.apply(null,window.map(function(point){return Number(point.low||point.close||0);}))),endPrice=exit?Number(exit.exit_price||0):Number(candles[end].close||0);trade.entry_time=entry.event_time;trade.exit_time=exit?exit.event_time:candles[end].time;trade.entry_price=entryPrice;trade.exit_price=exit?endPrice:null;trade.entry_chart_time=candles[start].time;trade.exit_chart_time=candles[end].time;trade.duration_bars=end-start;trade.mfe=(high/entryPrice-1)*100;trade.mae=(low/entryPrice-1)*100;trade.return_pct=(endPrice/entryPrice-1)*100;});}
  function bindTradeToSeries(trade){if(trade&&trade.manual_action)calculateManualStats(trade.symbol);return trade;}
  function addManualMarker(trade){var target=all.findIndex(function(series){return series.symbol===trade.symbol;});if(target<0)return;var series=all[target],candles=series.candles||[];if(!candles.length)return;bindTradeToSeries(trade);var time=trade.manual_action==='buy'?trade.entry_chart_time:trade.exit_chart_time;if(!time)time=candles[candles.length-1].time;series.markers=(series.markers || []).filter(function(marker){return !marker.manual || marker.manual_id!==trade.id;});series.markers.push({time:time,buy:trade.manual_action==='buy',position:trade.manual_action==='buy'?'belowBar':'aboveBar',shape:trade.manual_action==='buy'?'arrowUp':'arrowDown',text:(trade.manual_action==='buy'?'买入':'卖出')+' @'+money(trade.manual_action==='buy'?trade.entry_price:trade.exit_price),manual:true,manual_id:trade.id});if(target===curIdx)markerPrim.setMarkers(colorMarkers(series));}
  function rebuildManualMarkers(){all.forEach(function(series){series.markers=(series.markers || []).filter(function(marker){return !marker.manual;});});manualTrades.forEach(function(trade){addManualMarker(bindTradeToSeries(trade));});}
  function seriesName(series){if(series&&series.name)return series.name;var symbol=series&&series.symbol;var poolItem=(poolState.positions||[]).concat(poolState.watchlist||[]).find(function(item){return item.symbol===symbol;});if(poolItem&&poolItem.name)return poolItem.name;var trade=manualTrades.find(function(item){return item.symbol===symbol&&item.name;});return trade?trade.name:(symbol||'未选择标的');}
  function drawSymbol(i, fit){var series=all[i];if(!series)return;curIdx=i;rebuildManualMarkers();candle.setData(series.candles || []);setMovingAverages(series);volume.setData(colorVol(series));markerPrim.setMarkers(colorMarkers(series));priceHint.textContent=seriesName(series)+' · MA5 橙色 · MA10 紫色 · 点击交易定位到对应区间';if(fit)priceChart.timeScale().fitContent();renderTrades();}
  function visibleTrades(){var allTrades=trades.concat(manualTrades);return allTrades.filter(function(trade){if(filter.value==='win')return Number(trade.net_pnl||0)>0;if(filter.value==='loss')return Number(trade.net_pnl||0)<=0;return true;});}
  function tradeLabel(trade){if(trade.name)return trade.name;var poolItem=(poolState.positions||[]).concat(poolState.watchlist||[]).find(function(item){return item.symbol===trade.symbol;});return poolItem&&poolItem.name?poolItem.name:trade.symbol;}
  function renderMetrics(){metricHost.replaceChildren();var account=manualAccount(),tradeCount=manualTrades.length,winners=manualTrades.filter(function(trade){return Number(trade.net_pnl||0)>0;}).length,drawdown=Number(summary.max_drawdown_pct||0),indices=poolState.indices||[],shanghai=indices.find(function(item){return item.name==='上证指数';}),chinext=indices.find(function(item){return item.name==='创业板指数';}),shanghaiPct=shanghai&&!shanghai.quote_error&&Number.isFinite(Number(shanghai.change_pct))?Number(shanghai.change_pct):null,chinextPct=chinext&&!chinext.quote_error&&Number.isFinite(Number(chinext.change_pct))?Number(chinext.change_pct):null;var cards=[['上证指数涨跌幅',shanghaiPct==null?'—':pct(shanghaiPct),shanghaiPct==null?'':classFor(shanghaiPct),'market-index'],['创业板指数涨跌幅',chinextPct==null?'—':pct(chinextPct),chinextPct==null?'':classFor(chinextPct),'market-index'],['初始资金',money(account.initial),''],['期末权益',money(account.equity),''],['已实现盈亏',signedMoney(account.realized),classFor(account.realized)],['浮动盈亏',signedMoney(account.unrealized),classFor(account.unrealized)],['总净盈亏',signedMoney(account.totalPnl),classFor(account.totalPnl)],['策略收益',pct(account.returnPct),classFor(account.returnPct)],['当前持仓数量',account.positionCount+' 只',''],['仓位百分比',pct(account.positionPct),classFor(account.positionPct)],['交易数',String(tradeCount),''],['盈利笔数',String(winners),''],['胜率',tradeCount?(winners/tradeCount*100).toFixed(1)+'%':'0.0%',''],['最大回撤',drawdown?'−'+Math.abs(drawdown).toFixed(2)+'%':'0.00%','negative'],['夏普比率',Number(summary.sharpe_ratio||0).toFixed(2),classFor(summary.sharpe_ratio)]];cards.forEach(function(card){var node=document.createElement('article');node.className='metric'+(card[3]?' '+card[3]:'');appendText(node,'span',card[0],'label');appendText(node,'strong',card[1],card[2]);metricHost.appendChild(node);});if(typeof equityLine!=='undefined'&&equityData.length){var next=equityData.slice();next[next.length-1]={time:next[next.length-1].time,value:account.equity};equityLine.setData(next);}}
  function detailItem(parent,label,value,className){var node=document.createElement('div');node.className='detail-item';appendText(node,'span',label);appendText(node,'strong',value,className);parent.appendChild(node);}
  function renderDetail(trade){detailHost.replaceChildren();if(!trade){appendText(detailHost,'div','从列表中选择一笔交易，查看它的进出场、MAE/MFE，并让 K 线图定位到该区间。','empty');selectionHint.textContent='尚未选择交易';return;}selectionHint.textContent=tradeLabel(trade)+' · '+displayTime(trade.entry_time);var title=document.createElement('div');title.className='detail-title';appendText(title,'span',tradeLabel(trade));appendText(title,'span',trade.side,'badge');appendText(title,'span',signedMoney(trade.net_pnl),classFor(trade.net_pnl));detailHost.appendChild(title);var grid=document.createElement('div');grid.className='detail-grid';if(trade.manual_action){detailItem(grid,'模拟动作',trade.manual_action==='buy'?'买入':'卖出',trade.manual_action==='buy'?'positive':'negative');detailItem(grid,'成交价格',money(trade.manual_action==='buy'?trade.entry_price:trade.exit_price));detailItem(grid,'成交数量',money(trade.quantity));detailItem(grid,'已实现盈亏',signedMoney(trade.net_pnl),classFor(trade.net_pnl));detailItem(grid,'收益率',pct(trade.return_pct),classFor(trade.return_pct));detailItem(grid,'最大有利 MFE',trade.mfe==null?'—':pct(trade.mfe),'positive');detailItem(grid,'最大不利 MAE',trade.mae==null?'—':pct(trade.mae),'negative');}else{detailItem(grid,'入场',displayTime(trade.entry_time)+' @ '+money(trade.entry_price));detailItem(grid,'出场',displayTime(trade.exit_time)+' @ '+money(trade.exit_price));detailItem(grid,'收益率',pct(trade.return_pct),classFor(trade.return_pct));detailItem(grid,'持仓',String(trade.duration_bars==null?'—':trade.duration_bars)+' 根K线');detailItem(grid,'最大有利 MFE',trade.mfe==null?'—':pct(trade.mfe),'positive');detailItem(grid,'最大不利 MAE',trade.mae==null?'—':pct(trade.mae),'negative');}detailHost.appendChild(grid);}
  function tableCell(row,text,className){var cell=document.createElement('td');if(className)cell.className=className;cell.textContent=text;row.appendChild(cell);return cell;}
  function symbolButton(item,field){var button=document.createElement('button');button.type='button';button.className='pool-symbol';if(field==='name'){appendText(button,'strong',item.name || 'A股');appendText(button,'span','点击查看 K 线');}else{appendText(button,'strong',item.symbol);appendText(button,'span','A股');}button.addEventListener('click',function(){openPoolSymbol(item);});return button;}
  function renderPoolTable(host,kind,items){host.replaceChildren();if(!items.length){appendText(host,'div',kind==='position'?'当前没有持仓标的。':'当前没有观察标的。','empty');return;}var wrap=document.createElement('div');wrap.className='pool-table-wrap';var table=document.createElement('table');table.className='pool-table';var headers=kind==='position'?['股票名称','号码','持仓成本','持股数','当前价格','今日涨跌幅','量比','持仓收益','操作']:['股票名称','号码','自选价格','当前价格','今日涨跌幅','至今涨跌幅','量比','操作'];var thead=document.createElement('thead');var headRow=document.createElement('tr');headers.forEach(function(label){var th=document.createElement('th');th.textContent=label;headRow.appendChild(th);});thead.appendChild(headRow);table.appendChild(thead);var body=document.createElement('tbody');items.forEach(function(item){var row=document.createElement('tr');var nameCell=document.createElement('td');nameCell.appendChild(symbolButton(item,'name'));row.appendChild(nameCell);var codeCell=document.createElement('td');codeCell.appendChild(symbolButton(item,'code'));row.appendChild(codeCell);if(kind==='position'){var costCell=document.createElement('td');var cost=document.createElement('input');cost.type='number';cost.min='0';cost.step='any';cost.value=item.entry_price || 0;cost.className='pool-input';cost.setAttribute('aria-label',item.symbol+' 成本');costCell.appendChild(cost);row.appendChild(costCell);var qtyCell=document.createElement('td');var qty=document.createElement('input');qty.type='number';qty.min='0';qty.step='any';qty.value=item.quantity || 0;qty.className='pool-input';qty.setAttribute('aria-label',item.symbol+' 持股数');qtyCell.appendChild(qty);row.appendChild(qtyCell);var pnl=(Number(item.current_price||0)-Number(item.entry_price||0))*Number(item.quantity||0);tableCell(row,money(item.current_price),'');tableCell(row,pct(item.change_pct),classFor(item.change_pct));tableCell(row,volumeRatio(item),classFor(item.volume_change_pct));tableCell(row,signedMoney(pnl),classFor(pnl));var actionCell=document.createElement('td');var actions=document.createElement('div');actions.className='pool-actions';var save=document.createElement('button');save.type='button';save.className='button position-save';save.textContent='保存';save.addEventListener('click',function(){savePosition(item,qty.value,cost.value,row);});var del=document.createElement('button');del.type='button';del.className='button secondary danger';del.textContent='删除';del.addEventListener('click',function(){removePool('position',item.symbol);});actions.appendChild(save);actions.appendChild(del);actionCell.appendChild(actions);row.appendChild(actionCell);}else{var selfPrice=Number(item.self_price||item.current_price||0);var sincePct=selfPrice?((Number(item.current_price||0)/selfPrice-1)*100):0;tableCell(row,money(selfPrice),'');tableCell(row,money(item.current_price),'');tableCell(row,pct(item.change_pct),classFor(item.change_pct));tableCell(row,pct(sincePct),classFor(sincePct));tableCell(row,volumeRatio(item),classFor(item.volume_change_pct));var actionCell=document.createElement('td');var del=document.createElement('button');del.type='button';del.className='button secondary danger';del.textContent='删除';del.addEventListener('click',function(){removePool('watch',item.symbol);});actionCell.appendChild(del);row.appendChild(actionCell);}body.appendChild(row);});table.appendChild(body);wrap.appendChild(table);host.appendChild(wrap);}
  function renderPositions(){var items=poolState.positions || [];positionCount.textContent=items.length+' 个';renderPoolTable(positionHost,'position',items);}
  function renderWatchlist(){var items=poolState.watchlist || [];watchCount.textContent=items.length+' 个';renderPoolTable(watchHost,'watch',items);}
  function mergeBacktestPositions(body){if(body.initialized || !cfg.positions || !cfg.positions.length)return Promise.resolve(body);return Promise.all(cfg.positions.map(function(position){return api('/api/positions',jsonOptions('POST',{symbol:position.symbol,quantity:position.quantity,cost:position.entry_price})).catch(function(){return null;});})).then(function(){return api('/api/pools');});}
  function refreshPools(force){var suffix=force?'?refresh=1':'';return api('/api/pools'+suffix).then(function(body){return mergeBacktestPositions(body).then(function(next){poolState=next || body;manualTrades=(poolState.manual_trades || []).map(normalizeManualTrade);renderPositions();renderWatchlist();renderSignals();renderMetrics();rebuildManualMarkers();renderTrades();focusFirstTrade();});}).catch(function(error){renderPositions();renderWatchlist();renderSignals();renderMetrics();renderTrades();focusFirstTrade();window.console.warn(error);});}
  function renderTrades(){var items=visibleTrades();listHost.replaceChildren();countHost.textContent='当前 '+items.length+' / 共 '+(trades.length+manualTrades.length)+' 笔';if(!items.length){appendText(listHost,'div','当前筛选条件下没有交易。','empty');renderDetail(null);return;}if(activeTradeId && !items.some(function(item){return item.id===activeTradeId;}))activeTradeId=null;items.forEach(function(trade){var row=document.createElement('button');row.type='button';row.className='trade-row'+(trade.id===activeTradeId?' active':'');row.setAttribute('aria-label','查看 '+tradeLabel(trade)+' 交易详情');var main=document.createElement('span');main.className='trade-main';appendText(main,'span',tradeLabel(trade)+' · '+trade.side,'trade-symbol');appendText(main,'span',displayTime(trade.entry_time)+' → '+displayTime(trade.exit_time),'trade-meta');var pnl=document.createElement('span');pnl.className='trade-pnl '+classFor(trade.net_pnl);pnl.textContent=signedMoney(trade.net_pnl);row.appendChild(main);row.appendChild(pnl);row.addEventListener('click',function(){selectTrade(trade);});listHost.appendChild(row);});var active=items.find(function(item){return item.id===activeTradeId;});renderDetail(active || null);}
  function selectTrade(trade){activeTradeId=trade.id;ensureSeries(trade.symbol).then(function(target){bindTradeToSeries(trade);if(trade.manual_action)addManualMarker(trade);sel.value=String(target);drawSymbol(target,true);focusTrade(trade);renderDetail(trade);}).catch(function(error){window.alert(error.message);});}
  function focusFirstTrade(){var items=visibleTrades();if(!activeTradeId&&items.length)selectTrade(items[0]);}
  function focusTrade(trade){var series=all[curIdx];if(!series || !trade.entry_chart_time)return;var candles=series.candles || [];var start=candles.findIndex(function(point){return point.time===trade.entry_chart_time;});var end=candles.findIndex(function(point){return point.time===trade.exit_chart_time;});if(start<0)return;if(end<start)end=start;var from=candles[Math.max(0,start-6)],to=candles[Math.min(candles.length-1,end+6)];if(from&&to)priceChart.timeScale().setVisibleRange({from:from.time,to:to.time});}
  sel.addEventListener('change',function(){activeTradeId=null;drawSymbol(parseInt(sel.value,10)||0,true);});
  filter.addEventListener('change',function(){activeTradeId=null;renderTrades();});
  toggle.addEventListener('click',function(){cur=cur==='dark'?'light':'dark';applyTheme();});
  reset.addEventListener('click',function(){priceChart.timeScale().fitContent();activeTradeId=null;renderTrades();});
  refreshData.addEventListener('click',function(){refreshData.disabled=true;refreshData.textContent='↻ 刷新中';refreshPools(true).finally(function(){refreshData.disabled=false;refreshData.textContent='↻ 刷新';});});
  tradeForm.addEventListener('submit',function(event){event.preventDefault();searchTrade();});
  tradeBuy.addEventListener('click',function(){submitSimulatedTrade('buy');});
  tradeSell.addEventListener('click',function(){submitSimulatedTrade('sell');});
  tradeSearch.addEventListener('input',function(){if(!tradeSearch.value.trim())showTradeResults([]);});
  positionForm.addEventListener('submit',function(event){event.preventDefault();searchPool(positionSearch,positionResults,'position');});
  watchForm.addEventListener('submit',function(event){event.preventDefault();searchPool(watchSearch,watchResults,'watch');});
  positionSearch.addEventListener('input',function(){if(!positionSearch.value.trim())showResults(positionResults,[]);});
  watchSearch.addEventListener('input',function(){if(!watchSearch.value.trim())showResults(watchResults,[]);});
  renderMetrics();sel.value=String(curIdx);applyTheme();drawSymbol(curIdx,true);equityChart.timeScale().fitContent();renderDetail(null);refreshPools();
})();
"""


def _to_js_theme(colors: dict[str, str]) -> dict[str, str]:
    """把 ``THEMES`` 条目转成前端用的短键色板."""
    return {
        "up": colors["up_color"],
        "down": colors["down_color"],
        "bg": colors["bg_color"],
        "grid": colors["grid_color"],
        "text": colors["text_color"],
        "accent": "#74a7ff" if colors["bg_color"].lower() == "#1e1e1e" else "#2864dc",
    }


def render_review_html(
    payload: dict[str, Any],
    title: str,
    intraday: bool,
    themes: Optional[dict[str, dict[str, str]]] = None,
    initial_theme: str = "light",
    initial_symbol_index: int = 0,
    report_url: Optional[str] = None,
) -> str:
    """把 payload 渲染成离线自包含的复盘 HTML 字符串(支持页内明暗切换).

    :param payload: :func:`.._payload.build_review_payload` 的返回值.
    :param title: 报告标题(将被 HTML 转义).
    :param intraday: 是否日内(影响时间轴显示时分).
    :param themes: ``{"light": {...}, "dark": {...}}`` 主题色板;缺省用 ``THEMES``.
    :param initial_theme: 初始主题键(``"light"`` / ``"dark"``).
    :param initial_symbol_index: 初始展示的标的下标.
    :param report_url: 可选的策略回测报告相对路径或 http(s) URL.
    :return: 完整 HTML 文本.
    """
    if themes is None:
        from ..plot.utils import THEMES

        themes = THEMES
    init = initial_theme if initial_theme in themes else "light"
    data = dict(payload)
    data["themes"] = {name: _to_js_theme(cols) for name, cols in themes.items()}
    data["initial_theme"] = init
    data["intraday"] = bool(intraday)
    data["initial_symbol_index"] = int(initial_symbol_index)
    data["report_url"] = report_url
    safe_title = html.escape(str(title))
    # 顺序敏感:先注入受控内容与静态 JS,最后注入 DATA,
    # 避免用户数据里的 "%%...%%" 字面量被后续替换误伤(DATA 之后无替换)。
    ordered = [
        ("%%TITLE%%", safe_title),
        ("%%INIT_THEME%%", init),
        ("%%APP_JS%%", _APP_JS),
        ("%%LWC_JS%%", _load_lwc_js()),
        ("%%DATA%%", _safe_json(data)),
    ]
    out = _HTML_TEMPLATE
    for token, value in ordered:
        out = out.replace(token, value)
    return out
