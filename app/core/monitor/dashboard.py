#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : dashboard.py
# @Description : 监控面板前端：单文件自包含 HTML/CSS/JS
#               刻意不引任何 CDN —— 内网/离线环境也能打开，图表用 canvas 手绘
#               配色遵守 A 股约定：涨=红，跌=绿
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>AlphaQuant 运行监控</title>
<style>
  :root{
    --bg:#f5f7fa; --panel:#ffffff; --line:#e3e8ef; --text:#1f2937; --muted:#6b7280;
    --accent:#2563eb; --up:#d92b2b; --down:#0f9d58; --warn:#d97706;
    --ok:#0f9d58; --bad:#d92b2b; --shadow:0 1px 3px rgba(16,24,40,.08),0 1px 2px rgba(16,24,40,.04);
  }
  @media (prefers-color-scheme: dark){
    :root{ --bg:#0f1420; --panel:#161d2c; --line:#26304a; --text:#e5e9f0; --muted:#94a3b8;
           --shadow:0 1px 3px rgba(0,0,0,.4); }
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
       font:13px/1.5 -apple-system,"Segoe UI","Microsoft YaHei",Roboto,sans-serif}
  header{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:14px;
         padding:12px 20px;background:var(--panel);border-bottom:1px solid var(--line);box-shadow:var(--shadow);flex-wrap:wrap}
  header h1{font-size:15px;margin:0;letter-spacing:.3px;display:flex;align-items:center;gap:8px}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--accent);display:inline-block}
  .meta{display:flex;gap:16px;flex-wrap:wrap;color:var(--muted);font-size:12px}
  .meta b{color:var(--text);font-weight:600}
  .spacer{flex:1}
  button{cursor:pointer;border:1px solid var(--line);background:var(--panel);color:var(--text);
         border-radius:6px;padding:5px 11px;font-size:12px;transition:.15s}
  button:hover{border-color:var(--accent);color:var(--accent)}
  button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
  button.primary:hover{opacity:.9;color:#fff}
  button.danger:hover{border-color:var(--bad);color:var(--bad)}
  button.mini{padding:2px 8px;font-size:11px;border-radius:5px}
  main{padding:16px 20px 60px;display:flex;flex-direction:column;gap:16px;max-width:1680px;margin:0 auto}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;box-shadow:var(--shadow)}
  .panel>h2{margin:0;padding:11px 16px;font-size:13px;border-bottom:1px solid var(--line);
            display:flex;align-items:center;gap:8px}
  .panel>.body{padding:14px 16px}
  .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
  .kpi{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px;box-shadow:var(--shadow)}
  .kpi .k{color:var(--muted);font-size:11px;letter-spacing:.4px}
  .kpi .v{font-size:20px;font-weight:650;margin-top:4px;font-variant-numeric:tabular-nums}
  .kpi .s{color:var(--muted);font-size:11px;margin-top:2px}
  .comp-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px}
  .comp{border:1px solid var(--line);border-radius:8px;padding:10px 12px}
  .comp .top{display:flex;align-items:center;justify-content:space-between;gap:8px}
  .comp .nm{font-weight:600;font-size:12px}
  .comp .det{color:var(--muted);font-size:11px;margin-top:6px;word-break:break-all}
  .pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;border:1px solid transparent}
  .pill.run{background:rgba(15,157,88,.12);color:var(--ok);border-color:rgba(15,157,88,.3)}
  .pill.off{background:rgba(107,114,128,.14);color:var(--muted);border-color:rgba(107,114,128,.3)}
  .pill.err{background:rgba(217,43,43,.12);color:var(--bad);border-color:rgba(217,43,43,.3)}
  .pill.warn{background:rgba(217,119,6,.12);color:var(--warn);border-color:rgba(217,119,6,.3)}
  .pill.info{background:rgba(37,99,235,.12);color:var(--accent);border-color:rgba(37,99,235,.3)}
  .table-wrap{overflow:auto;border:1px solid var(--line);border-radius:8px}
  table{width:100%;border-collapse:collapse;font-size:12px;min-width:1000px}
  th,td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}
  th{background:rgba(37,99,235,.04);color:var(--muted);font-weight:600;position:sticky;top:0;font-size:11px;letter-spacing:.3px}
  tbody tr:hover{background:rgba(37,99,235,.045)}
  tbody tr.sel{background:rgba(37,99,235,.09)}
  .num{text-align:right;font-variant-numeric:tabular-nums}
  .up{color:var(--up)} .down{color:var(--down)} .muted{color:var(--muted)}
  .acts{display:flex;gap:5px;flex-wrap:wrap}
  #add-task{margin-top:12px;display:flex;gap:10px;align-items:flex-start;flex-wrap:wrap}
  textarea{width:100%;min-height:120px;font:12px/1.5 ui-monospace,Consolas,monospace;padding:10px;
           border:1px solid var(--line);border-radius:8px;background:transparent;color:var(--text);resize:vertical}
  .hint{color:var(--muted);font-size:11px;margin:4px 0 0}
  pre#logs{margin:0;max-height:320px;overflow:auto;font:11.5px/1.55 ui-monospace,Consolas,monospace;
           white-space:pre-wrap;word-break:break-all;color:var(--muted)}
  .drawer{position:fixed;top:0;right:0;bottom:0;width:min(760px,94vw);background:var(--panel);
          border-left:1px solid var(--line);box-shadow:-8px 0 24px rgba(16,24,40,.14);z-index:40;
          display:flex;flex-direction:column;transition:transform .22s ease}
  .drawer.hidden{transform:translateX(102%)}
  .drawer header{position:static;box-shadow:none}
  .drawer .body{overflow:auto;padding:16px;display:flex;flex-direction:column;gap:14px}
  .grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
  .kv{border:1px solid var(--line);border-radius:8px;padding:8px 10px}
  .kv .k{color:var(--muted);font-size:11px}
  .kv .v{font-weight:600;font-variant-numeric:tabular-nums;margin-top:2px}
  canvas{width:100%;height:220px;display:block}
  #toast{position:fixed;right:18px;bottom:18px;z-index:80;display:flex;flex-direction:column;gap:8px}
  .toast{background:#111827;color:#fff;padding:9px 14px;border-radius:8px;font-size:12px;opacity:.94;max-width:420px}
  .toast.bad{background:#b91c1c}
  .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  .seg{display:inline-flex;border:1px solid var(--line);border-radius:6px;overflow:hidden}
  .seg button{border:0;border-radius:0}
  .seg button.on{background:var(--accent);color:#fff}
  .tabs{display:flex;gap:4px;padding:8px 18px 0;border-bottom:1px solid var(--line);
        background:var(--panel);position:sticky;top:0;z-index:5}
  .tabs button{background:transparent;border:1px solid transparent;border-bottom:none;
        border-radius:8px 8px 0 0;padding:8px 18px;cursor:pointer;color:var(--muted);
        font-size:13px;font-weight:600}
  .tabs button:hover{color:var(--fg)}
  .tabs button.on{background:var(--bg);border-color:var(--line);color:var(--accent)}
  label{display:flex;flex-direction:column;gap:3px;font-size:11px;color:var(--muted)}
  label input,label select{font-size:12px}
  main.hidden,#page-backtest.hidden,#page-live.hidden{display:none}
  h3{font-weight:600;color:var(--fg)}
</style>
</head>
<body>
<header>
  <h1><span class="dot"></span>AlphaQuant 运行监控</h1>
  <span id="status" class="pill off">-</span>
  <div class="meta">
    <span>运行 <b id="run-id">-</b></span>
    <span>模式 <b id="mode">-</b></span>
    <span>已运行 <b id="uptime">-</b></span>
    <span>面板 <b id="addr">-</b></span>
    <span>快照 <b id="snap-info">-</b></span>
  </div>
  <div class="spacer"></div>
  <select id="snap-mode" title="快照策略（本页刷新与快照落盘是两件事）">
    <option value="on_event">快照: 有操作才存</option>
    <option value="on_change">快照: 内容变了才存</option>
    <option value="interval">快照: 定时间隔存</option>
    <option value="off">快照: 关闭</option>
  </select>
  <div class="seg" id="freq">
    <button data-v="1000">1s</button>
    <button data-v="2000" class="on">2s</button>
    <button data-v="5000">5s</button>
    <button data-v="0">暂停</button>
  </div>
  <button id="btn-snap">立即快照</button>
  <button id="btn-stop" class="danger">停止引擎</button>
</header>

<nav class="tabs" id="tabs">
  <button data-page="live" class="on">实盘监控</button>
  <button data-page="backtest">回测监控</button>
</nav>

<main id="page-live">
  <section id="kpis" class="kpis"></section>

  <section class="panel" id="live-panel">
    <h2>实盘账户 <span class="muted" id="live-hint"></span></h2>
    <div class="body">
      <div id="live-off" class="hint">本引擎未启用实盘网关（运行模式非 LIVE）。</div>
      <div id="live-on" class="hidden">
        <div class="grid2" id="live-kv"></div>
        <h3 style="margin:14px 0 6px;font-size:13px">
          券商账户 <span class="muted" id="live-acct-hint">（来自券商源，不是本地账本的推算）</span>
        </h3>
        <div class="grid2" id="live-acct"></div>
        <div class="row" style="margin-top:10px;flex-wrap:wrap;gap:8px">
          <button id="btn-reconcile" class="primary">立即对账</button>
          <button id="btn-refresh-acct">刷新账户</button>
          <button id="btn-cancel-all" class="danger">撤销全部未结订单</button>
          <span class="hint" id="live-msg"></span>
        </div>
        <h3 style="margin:14px 0 6px;font-size:13px">券商持仓 <span class="muted" id="live-pos-hint"></span></h3>
        <div class="table-wrap">
          <table>
            <thead><tr>
              <th>标的</th><th class="num">持仓</th><th class="num">可卖</th>
              <th class="num">成本价</th>
            </tr></thead>
            <tbody id="live-pos-rows"><tr><td colspan="4" class="muted">无持仓</td></tr></tbody>
          </table>
        </div>
        <h3 style="margin:14px 0 6px;font-size:13px">未结订单 <span class="muted" id="live-pending-hint"></span></h3>
        <div class="table-wrap">
          <table>
            <thead><tr>
              <th>时间</th><th>任务</th><th>标的</th><th>方向</th>
              <th class="num">委托量</th><th class="num">已成交</th>
              <th>状态</th><th>委托价</th><th>原因</th>
            </tr></thead>
            <tbody id="live-pending-rows"><tr><td colspan="9" class="muted">无</td></tr></tbody>
          </table>
        </div>
        <p class="hint">实盘下单只是"报单成功"，成交要靠券商的成交回报驱动。
        对账是账户级的：一个券商账户对应 N 个策略，逐任务对账在账户里必然对不上。
        <code>LIVE_STRICT_RECONCILE=true</code> 时对账不一致会直接阻断交易。</p>
      </div>
    </div>
  </section>

  <section class="panel">
    <h2>组件 <span class="muted" id="comp-hint"></span></h2>
    <div class="body"><div id="components" class="comp-grid"></div></div>
  </section>

  <section class="panel">
    <h2>任务 <span class="muted" id="task-hint"></span></h2>
    <div class="body">
      <div class="table-wrap">
        <table>
          <thead><tr>
            <th>任务ID</th><th>标的</th><th>策略</th><th>状态</th>
            <th class="num">权益</th><th class="num">总盈亏</th><th class="num">收益率</th>
            <th class="num">持仓</th><th class="num">成交</th><th class="num">回撤</th><th>操作</th>
          </tr></thead>
          <tbody id="task-rows"><tr><td colspan="11" class="muted">加载中…</td></tr></tbody>
        </table>
      </div>

      <div id="add-task">
        <div style="flex:1;min-width:320px">
          <textarea id="spec" spellcheck="false"></textarea>
          <p class="hint">粘贴任务 JSON（可参考 config/tasks.json 里的单条结构）。改配置即可增删策略，无需重启引擎。</p>
          <div class="row" style="margin-top:8px">
            <button class="primary" id="btn-add">添加任务</button>
            <button id="btn-tpl">填入示例</button>
            <span class="hint" id="add-msg"></span>
          </div>
        </div>
      </div>
    </div>
  </section>

  <section class="panel">
    <h2>行情采集 <span class="muted" id="col-hint"></span></h2>
    <div class="body">
      <div id="col-status" style="margin-bottom:8px"><span class="muted">加载中…</span></div>
      <div id="col-off" class="hint hidden">本引擎未装配采集服务。</div>
      <div id="col-on">
        <div class="table-wrap">
          <table>
            <thead><tr>
              <th>标的</th><th>粒度</th><th>频率</th><th class="num">跑次</th>
              <th class="num">累计入库</th><th class="num">失败</th>
              <th>上次运行</th><th>最近错误</th><th>操作</th>
            </tr></thead>
            <tbody id="col-rows"><tr><td colspan="9" class="muted">加载中…</td></tr></tbody>
          </table>
        </div>
        <div class="row" style="margin-top:10px;flex-wrap:wrap;gap:8px">
          <input id="col-sym" placeholder="标的代码，如 600519" style="width:170px">
          <select id="col-period" style="width:110px">
            <option value="1d">日线 1d</option>
            <option value="1">1 分钟</option>
            <option value="5">5 分钟</option>
            <option value="15">15 分钟</option>
            <option value="30">30 分钟</option>
            <option value="60">60 分钟</option>
          </select>
          <input id="col-int" placeholder="频率 30s/5m/1h" style="width:150px">
          <button id="btn-col-add">新增采集</button>
          <button id="btn-col-now" class="primary">立即采集</button>
          <button id="btn-col-reload">重载配置</button>
          <span class="hint" id="col-msg"></span>
        </div>
        <p class="hint">频率支持 30s / 5m / 1h / 1d，受 <code>min_interval</code> 下限保护。
        日内数据默认只在交易时段采集（含收盘后 30 分钟缓冲），日线不限时段。
        采集服务默认装配；纯回测模式会装配但停用（只读历史，联网采集没有意义）。</p>
      </div>
    </div>
  </section>

  <section class="panel">
    <h2>控制指令留痕 <span class="muted" id="audit-hint"></span></h2>
    <div class="body"><div class="table-wrap">
      <table><thead><tr><th>时间</th><th>指令</th><th>细节</th></tr></thead>
      <tbody id="audit-rows"><tr><td colspan="3" class="muted">暂无</td></tr></tbody></table>
    </div></div>
  </section>

  <section class="panel">
    <h2>运行日志 <span class="muted" id="log-hint"></span></h2>
    <div class="body"><pre id="logs">加载中…</pre></div>
  </section>
</main>

<!-- ==================== 回测监控 ==================== -->
<main id="page-backtest" class="hidden">
  <section class="panel">
    <h2>新建回测 <span class="muted">提交后台执行，不阻塞实盘</span></h2>
    <div class="body">
      <div class="row" style="flex-wrap:wrap;gap:8px;align-items:flex-end">
        <label>标的<br><input id="bt-symbol" placeholder="000001" style="width:120px"></label>
        <label>开始<br><input id="bt-start" type="date" style="width:150px"></label>
        <label>结束<br><input id="bt-end" type="date" style="width:150px"></label>
        <label>策略<br><select id="bt-strategy" style="width:180px"></select></label>
        <label>初始资金<br><input id="bt-cash" type="number" style="width:110px"></label>
        <label>策略参数<br><input id="bt-params" placeholder="fast=5 slow=20" style="width:170px"></label>
        <label>数据源<br>
          <select id="bt-source" style="width:110px">
            <option value="auto">auto</option>
            <option value="db">db</option>
            <option value="csv">csv</option>
            <option value="remote">remote</option>
          </select>
        </label>
        <label>复权<br>
          <select id="bt-adjust" style="width:90px">
            <option value="qfq">qfq</option>
            <option value="hfq">hfq</option>
            <option value="none">none</option>
          </select>
        </label>
        <button id="btn-bt-run" class="primary">开始回测</button>
        <span class="hint" id="bt-msg"></span>
      </div>
      <div class="row hidden" id="bt-spec-wrap" style="margin-top:10px">
        <div style="flex:1;min-width:320px">
          <label>规则定义(JSON) —— 策略选 <code>declarative</code> 时使用</label>
          <textarea id="bt-spec" spellcheck="false" style="min-height:96px"></textarea>
          <div class="row" style="margin-top:6px">
            <button id="btn-bt-tpl">填入示例规则</button>
            <span class="hint" id="bt-spec-msg"></span>
          </div>
        </div>
      </div>
      <p class="hint" id="bt-defaults-hint">回测在工作线程执行，不占用交易链路。完成后结果落 SQLite，
      下面的列表和曲线都从库里读 —— 重启服务也不会丢。
      表单里没填的字段用 <code>config/backtest.json</code> 里的默认值。</p>
    </div>
  </section>

  <section class="panel">
    <h2>回测作业 <span class="muted" id="bt-job-hint"></span></h2>
    <div class="body"><div class="table-wrap">
      <table>
        <thead><tr>
          <th>作业</th><th>状态</th><th>标的</th><th>策略</th>
          <th>区间</th><th>耗时</th><th>错误</th>
        </tr></thead>
        <tbody id="bt-job-rows"><tr><td colspan="7" class="muted">暂无</td></tr></tbody>
      </table>
    </div></div>
  </section>

  <section class="panel">
    <h2>历史回测结果 <span class="muted" id="bt-hint"></span></h2>
    <div class="body"><div class="table-wrap">
      <table>
        <thead><tr>
          <th>时间</th><th>标的</th><th>策略</th><th>区间</th>
          <th class="num">收益</th><th class="num">回撤</th><th class="num">夏普</th>
          <th class="num">成交</th><th class="num">胜率</th><th>操作</th>
        </tr></thead>
        <tbody id="bt-rows"><tr><td colspan="10" class="muted">暂无回测记录</td></tr></tbody>
      </table>
    </div></div>
  </section>

  <section class="panel hidden" id="bt-detail">
    <h2><span id="bt-d-title">回测详情</span>
      <span class="muted" id="bt-d-hint"></span></h2>
    <div class="body">
      <div class="grid2" id="bt-d-kv"></div>
      <div class="panel" style="margin-top:12px">
        <h2>权益曲线</h2><div class="body"><canvas id="bt-d-chart"></canvas></div>
      </div>
      <div class="panel" style="margin-top:12px">
        <h2>完整指标</h2><div class="body"><pre id="bt-d-metrics">-</pre></div>
      </div>
    </div>
  </section>
</main>

<aside id="drawer" class="drawer hidden">
  <header>
    <h1><span class="dot"></span><span id="d-title">任务详情</span></h1>
    <div class="spacer"></div>
    <button id="d-close">关闭</button>
  </header>
  <div class="body">
    <div class="grid2" id="d-kv"></div>
    <div class="panel"><h2>权益曲线</h2><div class="body"><canvas id="d-chart"></canvas></div></div>
    <div class="panel"><h2>风控链</h2><div class="body" id="d-risk"></div></div>
    <div class="panel"><h2>成交明细 <span class="muted" id="d-trade-hint"></span></h2>
      <div class="body"><div class="table-wrap"><table>
        <thead><tr><th>时间</th><th>方向</th><th class="num">数量</th><th class="num">价格</th>
        <th class="num">手续费</th><th class="num">实现盈亏</th><th>来源</th><th>原因</th></tr></thead>
        <tbody id="d-trades"></tbody></table></div></div>
    </div>
    <div class="panel"><h2>订单流 <span class="muted" id="d-order-hint"></span></h2>
      <div class="body"><div class="table-wrap"><table>
        <thead><tr><th>时间</th><th>方向</th><th class="num">数量</th><th>状态</th><th>原因/否决</th></tr></thead>
        <tbody id="d-orders"></tbody></table></div></div>
    </div>
    <div class="panel"><h2>相关新闻</h2><div class="body" id="d-news"><span class="muted">暂无</span></div></div>
    <div class="panel"><h2>异常</h2><div class="body" id="d-errors"><span class="muted">无</span></div></div>
  </div>
</aside>

<div id="toast"></div>

<script>
(function(){
  var REFRESH = 2000, timer = null, selected = null, lastOverview = null;
  // 当前页面。实盘页按 REFRESH 轮询；回测页轮询更慢（回测是分钟级的事），
  // 而且只在切过去的时候才轮询，避免白跑请求。
  var PAGE = 'live', btTimer = null, btSelected = null;
  // 回测默认参数（来自 config/backtest.json）与能力清单
  var BT_DEFAULTS = {}, CAPS = {}, SNAP_MODE_READY = false;

  function $(id){ return document.getElementById(id); }
  function esc(s){ return String(s==null?'':s).replace(/[&<>"]/g, function(c){
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }
  function money(v, d){ d = d==null?2:d; if(v==null||isNaN(v)) return '-';
      return Number(v).toLocaleString('zh-CN',{minimumFractionDigits:d,maximumFractionDigits:d}); }
  function cls(v){ return v>0?'up':(v<0?'down':'muted'); }
  function sgn(v,d){ if(v==null||isNaN(v)) return '-';
      return (v>0?'+':'') + money(v,d); }
  function ago(sec){ sec = Number(sec||0);
      if(sec<60) return sec.toFixed(0)+'s';
      if(sec<3600) return (sec/60).toFixed(1)+'min';
      if(sec<86400) return (sec/3600).toFixed(1)+'h';
      return (sec/86400).toFixed(1)+'d'; }

  function toast(msg, bad){
    var el = document.createElement('div');
    el.className = 'toast' + (bad?' bad':''); el.textContent = msg;
    $('toast').appendChild(el);
    setTimeout(function(){ el.remove(); }, bad?5200:2800);
  }

  function api(path, opts){
    return fetch(path, opts).then(function(r){
      return r.text().then(function(t){
        var data = null;
        try { data = t ? JSON.parse(t) : null; } catch(e){ data = {raw:t}; }
        if(!r.ok) throw new Error((data && (data.error||data.raw)) || ('HTTP '+r.status));
        return data;
      });
    });
  }
  function post(path, body){
    return api(path, {method:'POST', headers:{'Content-Type':'application/json'},
                      body: JSON.stringify(body||{})});
  }

  function statusPill(s){
    var m = {RUNNING:'run', INITIALIZING:'info', STOPPING:'warn', STOPPED:'off',
             PAUSED:'info', ERROR:'err'};
    return '<span class="pill ' + (m[s]||'off') + '">' + esc(s||'-') + '</span>';
  }
  function taskPill(s){
    var m = {RUNNING:'run', CREATED:'info', PAUSED:'warn', STOPPED:'off', ERROR:'err'};
    return '<span class="pill ' + (m[s]||'off') + '">' + esc(s||'-') + '</span>';
  }

  // 同步"快照策略"下拉框。只在首次同步，避免用户正在选择时被轮询覆盖回去。
  function syncSnapMode(mode){
    var sel = $('snap-mode');
    if(!sel || SNAP_MODE_READY || !mode) return;
    sel.value = mode;
    SNAP_MODE_READY = true;
  }

  function renderKpis(o){
    var ts = o.tasks || [], eq = 0, pnl = 0, trades = 0, pos = 0, cash = 0;
    ts.forEach(function(t){ eq += t.equity||0; pnl += t.total_pnl||0;
      trades += t.trade_count||0; pos += t.position_size||0; cash += t.cash||0; });
    var m = o.monitor || {};
    var modeTxt = {on_event:'有操作才存', on_change:'内容变了才存',
                   interval:'定时存', off:'已关闭'}[m.snapshot_mode] || (m.snapshot_mode||'-');
    var cards = [
      ['运行状态', (o.engine||{}).status||'-', '空闲: ' + String((o.engine||{}).idle)],
      ['任务数', ts.length, '标的 ' + (o.symbol_count||0) + ' 个'],
      ['总权益', money(eq), '现金 ' + money(cash)],
      ['总盈亏', sgn(pnl), '含浮动盈亏'],
      ['成交笔数', trades, '持仓合计 ' + pos + ' 股'],
      ['订单流', (o.orders||0), '快照 ' + (m.writes||0) + ' 份'],
      ['快照策略', modeTxt,
       '触发 ' + (m.triggers||0) + ' 次 · ' +
       (m.last_snapshot_at ? ('上次 ' + m.last_snapshot_at.replace('T',' ').slice(11,19)) : '尚未落盘')]
    ];
    $('kpis').innerHTML = cards.map(function(c, i){
      var v = c[1];
      var c2 = (i===3 && typeof v === 'string' && v.charAt(0)==='+') ? 'up'
             : (i===3 && typeof v === 'string' && v.charAt(0)==='-') ? 'down' : '';
      return '<div class="kpi"><div class="k">' + esc(c[0]) + '</div>' +
             '<div class="v ' + c2 + '">' + (typeof v === 'number' ? money(v,0) : esc(v)) + '</div>' +
             '<div class="s">' + esc(c[2]) + '</div></div>';
    }).join('');
  }

  function renderComponents(o){
    var cs = o.components || [];
    $('comp-hint').textContent = '(' + cs.length + ')';
    $('components').innerHTML = cs.map(function(c){
      var state = (c.healthy === false) ? 'err' : (c.enabled ? 'run' : 'off');
      var label = c.enabled ? (c.state||'-') : 'disabled';
      return '<div class="comp">' +
        '<div class="top"><span class="nm">' + esc(c.name) + '</span>' +
        '<span class="pill ' + state + '">' + esc(label) + '</span></div>' +
        '<div class="det">' + esc(c.health_detail||'') + '</div>' +
        '<div class="acts" style="margin-top:8px">' +
        '<button class="mini" data-act="comp" data-name="' + esc(c.name) + '" data-op="' +
        (c.enabled?'disable':'enable') + '">' + (c.enabled?'禁用':'启用') + '</button>' +
        '</div></div>';
    }).join('');
  }

  function renderTasks(o){
    var ts = o.tasks || [];
    $('task-hint').textContent = '(' + ts.length + ')';
    if(!ts.length){ $('task-rows').innerHTML =
      '<tr><td colspan="11" class="muted">当前没有任务。用下方表单添加，或检查 config/tasks.json</td></tr>'; return; }
    $('task-rows').innerHTML = ts.map(function(t){
      var ret = t.equity && t.initial_cash ? (t.total_pnl / t.initial_cash * 100) : 0;
      var sel = (selected === t.task_id) ? ' class="sel"' : '';
      return '<tr' + sel + '>' +
        '<td><b>' + esc(t.task_id) + '</b></td>' +
        '<td>' + esc(t.symbol) + '</td>' +
        '<td>' + esc(t.strategy) + '</td>' +
        '<td>' + taskPill(t.status) + '</td>' +
        '<td class="num">' + money(t.equity) + '</td>' +
        '<td class="num ' + cls(t.total_pnl) + '">' + sgn(t.total_pnl) + '</td>' +
        '<td class="num ' + cls(ret) + '">' + sgn(ret,2) + '%</td>' +
        '<td class="num">' + (t.position_size||0) + '</td>' +
        '<td class="num">' + (t.trade_count||0) + '</td>' +
        '<td class="num">' + ((t.drawdown||0)*100).toFixed(2) + '%</td>' +
        '<td><div class="acts">' +
          '<button class="mini" data-act="detail" data-id="' + esc(t.task_id) + '">详情</button>' +
          (t.status==='RUNNING'
            ? '<button class="mini" data-act="pause" data-id="' + esc(t.task_id) + '">暂停</button>'
            : '<button class="mini" data-act="resume" data-id="' + esc(t.task_id) + '">恢复</button>') +
          '<button class="mini danger" data-act="remove" data-id="' + esc(t.task_id) + '">删除</button>' +
        '</div></td></tr>';
    }).join('');
  }

  // ---------------- 行情采集 ----------------
  // 采集区块单独轮询 /api/collector：它是秒级循环的 IO 服务，
  // 状态变化比任务表快，跟 overview 一起刷的话间隔不合适。
  //
  // 三种状态要分开表达，否则用户看到"未启用"不知道该改哪里：
  //   未装配  —— 只有老版本才会出现
  //   已装配·停用 —— 说清原因（回测模式 / 显式关闭），并仍然允许手动补采
  //   已装配·运行 —— 正常展示任务表
  function renderCollector(c){
    var available = !!(c && c.available);
    $('col-off').classList.toggle('hidden', available);
    if(!available){
      $('col-hint').textContent = '(未装配)';
      return;
    }

    var jobs = c.jobs || [], st = c.state || {};
    var enabled = !!c.enabled, running = !!c.running;
    var reason = c.disabled_reason || '';

    if(!enabled){
      $('col-hint').textContent = '(已装配 · 已停用)';
      $('col-status').innerHTML = '<span class="pill off">已停用</span>' +
        (reason ? ' <span class="muted">' + esc(reason) + '</span>' : '');
    } else if(!running){
      $('col-hint').textContent = '(已装配 · 未调度)';
      $('col-status').innerHTML = '<span class="pill warn">未调度</span>' +
        ' <span class="muted">没有启用中的任务</span>';
    } else {
      $('col-hint').textContent = '(' + jobs.length + ' 个任务 · 交易时段限定: '
        + String(c.trading_hours_only) + ' · 下限 ' + (c.min_interval||0) + 's)';
      $('col-status').innerHTML = '<span class="pill run">运行中</span>' +
        ' <span class="muted">' + esc((c.buckets||[]).length) + ' 档频率</span>';
    }

    if(!jobs.length){
      $('col-rows').innerHTML = '<tr><td colspan="9" class="muted">没有配置采集标的。' +
        '在下面新增，或编辑 ' + esc(c.config||'config/collector.json') + '</td></tr>';
      return;
    }
    $('col-rows').innerHTML = jobs.map(function(j){
      var s = st[j.symbol + ':' + j.period] || {};
      var last = s.last_run_at ? (s.last_run_at||'').replace('T',' ').slice(0,19) : '—';
      var err = s.last_error || '';
      return '<tr>' +
        '<td><b>' + esc(j.symbol) + '</b></td>' +
        '<td>' + esc(j.period==='1d'?'日线':(j.period+' 分钟')) + '</td>' +
        '<td>' + (j.enabled
            ? '<input class="ci" data-sym="' + esc(j.symbol) + '" data-per="' + esc(j.period) +
              '" value="' + esc(j.interval_human) + '" style="width:70px">'
            : '<span class="muted">已禁用</span>') + '</td>' +
        '<td class="num">' + (s.runs||0) + '</td>' +
        '<td class="num">' + money(s.rows||0,0) + '</td>' +
        '<td class="num ' + ((s.errors||0)>0?'down':'muted') + '">' + (s.errors||0) + '</td>' +
        '<td class="muted">' + esc(last) + '</td>' +
        '<td class="muted" title="' + esc(err) + '">' + esc(err ? err.slice(0,42) : '') + '</td>' +
        '<td><div class="acts">' +
          '<button class="mini" data-act="col-now" data-sym="' + esc(j.symbol) + '">补采</button>' +
          '<button class="mini danger" data-act="col-del" data-sym="' + esc(j.symbol) +
            '" data-per="' + esc(j.period) + '">移除</button>' +
        '</div></td></tr>';
    }).join('');
  }

  function loadCollector(){
    api('/api/collector').then(renderCollector).catch(function(e){
      $('col-hint').textContent = '(加载失败: ' + e.message + ')';
    });
  }

  function renderAudit(o){
    var rows = o.audit || [];
    $('audit-hint').textContent = '(' + rows.length + ')';
    if(!rows.length){ $('audit-rows').innerHTML = '<tr><td colspan="3" class="muted">暂无</td></tr>'; return; }
    $('audit-rows').innerHTML = rows.slice().reverse().map(function(r){
      return '<tr><td>' + esc((r.at||'').replace('T',' ').slice(0,19)) + '</td>' +
             '<td>' + esc(r.action) + '</td><td class="muted">' + esc(JSON.stringify(r.detail||{})) + '</td></tr>';
    }).join('');
  }

  function drawChart(canvas, pts){
    var dpr = window.devicePixelRatio || 1;
    var w = canvas.clientWidth || 600, h = canvas.clientHeight || 220;
    canvas.width = w*dpr; canvas.height = h*dpr;
    var ctx = canvas.getContext('2d'); ctx.setTransform(dpr,0,0,dpr,0,0);
    ctx.clearRect(0,0,w,h);
    var css = getComputedStyle(document.body);
    var line = css.getPropertyValue('--line')||'#ddd', muted = css.getPropertyValue('--muted')||'#888';
    var up = css.getPropertyValue('--up')||'#d92b2b';
    if(!pts || pts.length < 2){
      ctx.fillStyle = muted; ctx.font = '12px sans-serif';
      ctx.fillText('权益曲线数据不足（需要至少 2 个点）', 12, h/2); return; }
    var pad = {l:58, r:10, t:12, b:22};
    var vals = pts.map(function(p){ return Number(p[1]); });
    var mn = Math.min.apply(null, vals), mx = Math.max.apply(null, vals);
    if(mx === mn){ mx += 1; mn -= 1; }
    var span = mx - mn, lo = mn - span*0.06, hi = mx + span*0.06;
    var X = function(i){ return pad.l + i*(w-pad.l-pad.r)/(pts.length-1); };
    var Y = function(v){ return pad.t + (hi-v)*(h-pad.t-pad.b)/(hi-lo); };
    // 网格 + y 轴
    ctx.strokeStyle = line; ctx.lineWidth = 1; ctx.font = '10px sans-serif'; ctx.fillStyle = muted;
    for(var k=0;k<=4;k++){
      var v = lo + (hi-lo)*k/4, y = Y(v);
      ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w-pad.r, y); ctx.stroke();
      ctx.fillText(v.toFixed(2), 4, y+3);
    }
    // 面积 + 折线
    var grd = ctx.createLinearGradient(0, pad.t, 0, h-pad.b);
    grd.addColorStop(0, 'rgba(37,99,235,.22)'); grd.addColorStop(1, 'rgba(37,99,235,0)');
    ctx.beginPath(); ctx.moveTo(X(0), Y(vals[0]));
    for(var i=1;i<pts.length;i++) ctx.lineTo(X(i), Y(vals[i]));
    ctx.lineTo(X(pts.length-1), h-pad.b); ctx.lineTo(X(0), h-pad.b); ctx.closePath();
    ctx.fillStyle = grd; ctx.fill();
    ctx.beginPath(); ctx.moveTo(X(0), Y(vals[0]));
    for(var j=1;j<pts.length;j++) ctx.lineTo(X(j), Y(vals[j]));
    ctx.strokeStyle = '#2563eb'; ctx.lineWidth = 1.6; ctx.stroke();
    // 末点
    var lx = X(pts.length-1), ly = Y(vals[vals.length-1]);
    ctx.beginPath(); ctx.arc(lx, ly, 3, 0, Math.PI*2);
    ctx.fillStyle = (vals[vals.length-1] >= vals[0]) ? up : '#0f9d58'; ctx.fill();
    // x 轴首尾时间
    ctx.fillStyle = muted;
    ctx.fillText(String(pts[0][0]).slice(0,10), pad.l, h-6);
    var last = String(pts[pts.length-1][0]).slice(0,10);
    ctx.fillText(last, w-pad.r-ctx.measureText(last).width, h-6);
  }

  function renderDetail(d){
    $('d-title').textContent = d.task_id + ' · ' + d.symbol;
    var rows = [
      ['状态', d.status], ['策略', d.strategy], ['权益', money(d.equity)],
      ['现金', money(d.cash)], ['总盈亏', sgn(d.total_pnl)], ['收益率',
        (d.initial_cash ? sgn(d.total_pnl/d.initial_cash*100,2)+'%' : '-')],
      ['持仓', (d.position_size||0) + ' 股'], ['均价', money(d.avg_price,3)],
      ['现价', money(d.last_price,3)], ['回撤', ((d.drawdown||0)*100).toFixed(2)+'%'],
      ['成交', d.trade_count], ['K线', d.bar_count],
      ['持仓K线', d.hold_bars], ['距上次成交', d.bars_since_trade]
    ];
    $('d-kv').innerHTML = rows.map(function(r){
      var c = (r[0]==='总盈亏'||r[0]==='收益率') ? cls(d.total_pnl) : '';
      return '<div class="kv"><div class="k">' + esc(r[0]) + '</div><div class="v ' + c + '">' +
             esc(r[1]) + '</div></div>';
    }).join('');

    $('d-risk').innerHTML = (d.risk_rules||[]).length
      ? (d.risk_rules||[]).map(function(r){
          return '<span class="pill info" style="margin:2px 4px 2px 0">' + esc(r.name) +
                 (Object.keys(r.params||{}).length ? ' ' + esc(JSON.stringify(r.params)) : '') + '</span>';
        }).join('')
      : '<span class="muted">未配置风控</span>';

    var tr = d.trades || [];
    $('d-trade-hint').textContent = '(' + tr.length + ')';
    $('d-trades').innerHTML = tr.length ? tr.slice().reverse().map(function(t){
      return '<tr><td>' + esc((t.dt||'').replace('T',' ').slice(0,19)) + '</td>' +
        '<td class="' + (t.side==='BUY'?'up':'down') + '">' + esc(t.side) + '</td>' +
        '<td class="num">' + t.size + '</td><td class="num">' + money(t.price,3) + '</td>' +
        '<td class="num">' + money(t.fee,2) + '</td>' +
        '<td class="num ' + cls(t.realized_pnl) + '">' + sgn(t.realized_pnl) + '</td>' +
        '<td>' + esc(t.source) + '</td><td class="muted">' + esc(t.reason) + '</td></tr>';
    }).join('') : '<tr><td colspan="8" class="muted">暂无成交</td></tr>';

    var od = d.orders || [];
    $('d-order-hint').textContent = '(' + od.length + ')';
    $('d-orders').innerHTML = od.length ? od.slice().reverse().map(function(o){
      return '<tr><td>' + esc((o.created_at||'').replace('T',' ').slice(0,19)) + '</td>' +
        '<td class="' + (o.side==='BUY'?'up':'down') + '">' + esc(o.side) + '</td>' +
        '<td class="num">' + o.size + '</td><td>' + esc(o.status) + '</td>' +
        '<td class="muted">' + esc(o.reject_reason || o.reason) + '</td></tr>';
    }).join('') : '<tr><td colspan="5" class="muted">暂无订单</td></tr>';

    var nw = d.news || [];
    $('d-news').innerHTML = nw.length ? nw.slice().reverse().map(function(n){
      return '<div class="kv" style="margin-bottom:6px"><div class="k">' +
        esc((n.published_at||'').slice(0,19)) + ' · ' + esc(n.analyzer) +
        ' · score=' + esc(n.score) + ' conf=' + esc(n.confidence) + '</div>' +
        '<div class="v">' + esc(n.reason || n.news_fingerprint) + '</div></div>';
    }).join('') : '<span class="muted">暂无</span>';

    var er = d.errors || [];
    $('d-errors').innerHTML = er.length
      ? er.map(function(e){ return '<div class="muted">· ' + esc(e) + '</div>'; }).join('')
      : '<span class="muted">无</span>';

    requestAnimationFrame(function(){ drawChart($('d-chart'), d.equity_curve||[]); });
  }

  function refresh(){
    api('/api/overview').then(function(o){
      lastOverview = o;
      var e = o.engine || {}, m = o.monitor || {};
      $('status').outerHTML = '<span id="status" class="pill ' +
        ({RUNNING:'run',INITIALIZING:'info',STOPPING:'warn',STOPPED:'off',ERROR:'err'}[e.status]||'off') +
        '">' + esc(e.status||'-') + '</span>';
      $('run-id').textContent = e.run_id || '-';
      $('mode').textContent = e.mode || '-';
      $('uptime').textContent = ago(e.uptime_sec);
      $('addr').textContent = (m.host||'') + ':' + (m.port||'');
      $('snap-info').textContent = (m.writes||0) + ' 份' +
        (m.snapshot_pending ? ' (待写)' : '');
      syncSnapMode(m.snapshot_mode);
      renderKpis(o); renderComponents(o); renderTasks(o); renderAudit(o);
      if(PAGE === 'live') loadLive();
      if(selected){ loadDetail(selected, true); }
    }).catch(function(err){
      $('status').outerHTML = '<span id="status" class="pill err">离线</span>';
      $('logs').textContent = '连接面板失败: ' + err.message;
    });
  }

  function loadLogs(){
    api('/api/logs?n=200').then(function(d){
      var lines = d.lines || [];
      $('log-hint').textContent = '(' + lines.length + ' 行，源: ' + (d.source||'-') + ')';
      var pre = $('logs'), atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 30;
      pre.textContent = lines.join('\n');
      if(atBottom) pre.scrollTop = pre.scrollHeight;
    }).catch(function(){});
  }

  function loadDetail(id, silent){
    api('/api/tasks/' + encodeURIComponent(id)).then(function(d){
      if(!d || d.error){ toast(d && d.error || '任务不存在', true); return; }
      selected = d.task_id;
      renderDetail(d);
      $('drawer').classList.remove('hidden');
      if(!silent) renderTasks(lastOverview || {tasks:[]});
    }).catch(function(e){ toast('加载任务详情失败: ' + e.message, true); });
  }

  function act(path, body, label){
    return post(path, body).then(function(r){
      if(r && r.ok === false){ toast(label + ' 失败: ' + (r.error||''), true); return r; }
      toast(label + ' 成功');
      return refresh();
    }).catch(function(e){ toast(label + ' 失败: ' + e.message, true); });
  }

  document.addEventListener('click', function(ev){
    var b = ev.target.closest('button'); if(!b) return;

    // ---- 页面切换 ----
    var page = b.getAttribute('data-page');
    if(page){ switchPage(page); return; }

    // ---- 实盘操作 ----
    if(b.id === 'btn-reconcile'){
      post('/api/live/reconcile', {}).then(function(r){
        if(r && r.ok){
          toast(r.ok === false ? '对账发现不一致' : '对账一致', r.ok === false);
        } else toast('对账失败: ' + ((r && r.error) || ''), true);
        loadLive();
      }).catch(function(e){ toast('对账失败: ' + e.message, true); });
      return;
    }
    if(b.id === 'btn-cancel-all'){
      if(!confirm('确认撤销所有未结订单？实盘上这会立即向券商发出撤单请求。')) return;
      post('/api/live/cancel-all', {}).then(function(r){
        toast(r && r.ok ? ('已请求撤销 ' + (r.cancelled||0) + ' 笔') : ('失败: ' + ((r&&r.error)||'')), !(r&&r.ok));
        loadLive();
      }).catch(function(e){ toast('撤单失败: ' + e.message, true); });
      return;
    }

    // ---- 回测 ----
    if(b.id === 'btn-bt-run'){
      var strat = $('bt-strategy').value;
      var body = {
        symbol: $('bt-symbol').value.trim(),
        start: $('bt-start').value || (BT_DEFAULTS.start || '2020-01-01'),
        end: $('bt-end').value || new Date().toISOString().slice(0,10),
        strategy: strat,
        cash: Number($('bt-cash').value || BT_DEFAULTS.cash || 100000),
        params: $('bt-params').value.trim(),
        data_source: $('bt-source').value,
        adjust: $('bt-adjust').value,
      };
      if(!body.symbol){ $('bt-msg').textContent = '请填写标的代码'; return; }
      // 声明式策略：把规则 JSON 塞进 params.spec，后端会解析给 DeclarativeStrategy
      if(strat === 'declarative'){
        var raw = $('bt-spec').value.trim();
        if(!raw){ $('bt-spec-msg').textContent = '请填写规则定义，或点「填入示例规则」'; return; }
        var spec = null;
        try { spec = JSON.parse(raw); }
        catch(e){ $('bt-spec-msg').textContent = '规则不是合法 JSON: ' + e.message; return; }
        $('bt-spec-msg').textContent = '';
        body.spec = spec;
      }
      $('bt-msg').textContent = '提交中…';
      post('/api/backtest/run', body).then(function(r){
        if(r && r.ok){
          $('bt-msg').textContent = '已提交 ' + r.job.job_id + '，正在后台执行…';
          toast('回测已提交: ' + r.job.job_id);
          loadBacktest();
        } else {
          $('bt-msg').textContent = '失败: ' + ((r && r.error) || '未知错误');
          toast('回测提交失败', true);
        }
      }).catch(function(e){ $('bt-msg').textContent = '失败: ' + e.message; });
      return;
    }
    if(b.id === 'btn-bt-tpl'){
      $('bt-spec').value = JSON.stringify(SPEC_TEMPLATE, null, 2);
      $('bt-spec-msg').textContent = '已填入示例：5/20 金叉买（需同时站上均线）、死叉卖';
      return;
    }
    if(b.id === 'btn-refresh-acct'){
      post('/api/live/refresh', {}).then(function(r){
        toast(r && r.ok ? '已从券商刷新账户' : ('刷新失败: ' + ((r&&r.error)||'')), !(r&&r.ok));
        loadLive();
      }).catch(function(e){ toast('刷新失败: ' + e.message, true); });
      return;
    }
    var btAct = b.getAttribute('data-act');
    if(btAct === 'bt-detail'){ loadBacktestDetail(b.getAttribute('data-id')); return; }

    var act_ = b.getAttribute('data-act');
    if(act_ === 'detail'){ loadDetail(b.getAttribute('data-id')); return; }
    if(act_ === 'pause'){ act('/api/tasks/' + encodeURIComponent(b.getAttribute('data-id')) + '/pause', {}, '暂停任务'); return; }
    if(act_ === 'resume'){ act('/api/tasks/' + encodeURIComponent(b.getAttribute('data-id')) + '/resume', {}, '恢复任务'); return; }
    if(act_ === 'remove'){
      var id = b.getAttribute('data-id');
      if(!confirm('确认删除任务 ' + id + ' ？该任务将立即停止并从运行时移除。')) return;
      act('/api/tasks/' + encodeURIComponent(id) + '/remove', {}, '删除任务'); return;
    }
    if(act_ === 'comp'){
      var nm = b.getAttribute('data-name'), op = b.getAttribute('data-op');
      act('/api/components/' + encodeURIComponent(nm) + '/' + op, {}, (op==='enable'?'启用':'禁用') + '组件'); return;
    }
    // ---- 行情采集 ----
    if(act_ === 'col-now'){
      var sym = b.getAttribute('data-sym');
      b.disabled = true; b.textContent = '采集中…';
      post('/api/collector/now', {symbol: sym}).then(function(r){
        b.disabled = false; b.textContent = '补采';
        if(r && r.ok){ toast('补采完成，入库 ' + (r.rows||0) + ' 条'); loadCollector(); }
        else toast('补采失败: ' + ((r && r.error) || '未知错误'), true);
      }).catch(function(e){
        b.disabled = false; b.textContent = '补采';
        toast('补采失败: ' + e.message, true);
      });
      return;
    }
    if(act_ === 'col-del'){
      var ds = b.getAttribute('data-sym'), dp = b.getAttribute('data-per');
      if(!confirm('移除采集任务 ' + ds + ' / ' + dp + ' ？')) return;
      post('/api/collector/remove', {symbol: ds, period: dp}).then(function(r){
        if(r && r.ok){ toast('已移除'); loadCollector(); }
        else toast('移除失败: ' + ((r && r.error) || ''), true);
      }).catch(function(e){ toast('移除失败: ' + e.message, true); });
      return;
    }
    if(b.id === 'btn-col-add'){
      var cs = $('col-sym').value.trim(), cp = $('col-period').value, civ = $('col-int').value.trim();
      if(!cs){ $('col-msg').textContent = '请填写标的代码'; return; }
      $('col-msg').textContent = '提交中…';
      post('/api/collector/add', {symbol: cs, period: cp, interval: civ || undefined})
        .then(function(r){
          if(r && r.ok){ $('col-msg').textContent = ''; toast('采集任务已添加'); loadCollector(); }
          else { $('col-msg').textContent = '失败: ' + ((r && r.error) || '未知错误'); }
        }).catch(function(e){ $('col-msg').textContent = '失败: ' + e.message; });
      return;
    }
    if(b.id === 'btn-col-now'){
      $('col-msg').textContent = '后台采集中…';
      post('/api/collector/now', {symbol: $('col-sym').value.trim()}).then(function(r){
        $('col-msg').textContent = '';
        if(r && r.ok) toast('补采完成，入库 ' + (r.rows||0) + ' 条');
        else toast('补采失败: ' + ((r && r.error) || ''), true);
        loadCollector();
      }).catch(function(e){ $('col-msg').textContent = ''; toast('补采失败: ' + e.message, true); });
      return;
    }
    if(b.id === 'btn-col-reload'){
      post('/api/collector/reload', {}).then(function(r){
        if(r && r.ok){ toast('配置已热加载'); loadCollector(); }
        else toast('重载失败: ' + ((r && r.error) || ''), true);
      }).catch(function(e){ toast('重载失败: ' + e.message, true); });
      return;
    }
    if(b.id === 'btn-snap'){ act('/api/command', {action:'snapshot'}, '快照落盘'); return; }
    if(b.id === 'btn-stop'){
      if(!confirm('确认停止引擎？会执行优雅退出，处理完当前任务后关闭。')) return;
      act('/api/engine/stop', {graceful:true}, '停止引擎'); return;
    }
    if(b.id === 'd-close'){ $('drawer').classList.add('hidden'); selected = null;
      renderTasks(lastOverview||{tasks:[]}); return; }
    if(b.id === 'btn-tpl'){
      $('spec').value = JSON.stringify({
        task_id: 'manual-' + Date.now().toString(36),
        name: '手动新增-双均线', symbol: '000001', enabled: true,
        initial_cash: 100000, warmup_bars: 30,
        sizer: {type:'percent', pct:0.2, lot_size:100},
        strategy: {type:'ma_cross', params:{fast:5, slow:20}},
        risk: [{type:'cooldown', min_bars:1}, {type:'cash_reserve'}]
      }, null, 2);
      return;
    }
    if(b.id === 'btn-add'){
      var raw = $('spec').value.trim();
      if(!raw){ $('add-msg').textContent = '请先填写任务 JSON'; return; }
      var spec; try { spec = JSON.parse(raw); }
      catch(e){ $('add-msg').textContent = 'JSON 解析失败: ' + e.message; toast('JSON 解析失败', true); return; }
      $('add-msg').textContent = '提交中…';
      post('/api/tasks', spec).then(function(r){
        if(r && r.ok){ toast('任务已添加: ' + (r.task_id||'')); $('add-msg').textContent = ''; $('spec').value=''; refresh(); }
        else { $('add-msg').textContent = '失败: ' + (r && r.error || '未知错误');
               toast('添加任务失败', true); }
      }).catch(function(e){ $('add-msg').textContent = '失败: ' + e.message; toast('添加任务失败', true); });
      return;
    }
    var f = b.getAttribute('data-v');
    if(f !== null){
      Array.prototype.forEach.call($('freq').children, function(x){ x.classList.remove('on'); });
      b.classList.add('on');
      REFRESH = Number(f);
      if(timer){ clearInterval(timer); timer = null; }
      if(REFRESH > 0) timer = setInterval(refresh, REFRESH);
      toast(REFRESH>0 ? ('刷新间隔 ' + (REFRESH/1000) + 's') : '已暂停自动刷新');
    }
  });

  // ==================== 实盘账户面板 ====================
  function renderLive(d){
    var on = d && d.available;
    $('live-off').classList.toggle('hidden', !!on);
    $('live-on').classList.toggle('hidden', !on);
    if(!on){ $('live-hint').textContent = '(未启用)'; return; }

    var st = d.stats || {}, rc = d.last_reconcile || {};
    $('live-hint').textContent = '(' + (d.gateway||'-') +
      (d.endpoint ? ' · 接入点 ' + d.endpoint : '') + ')';
    var conn = d.connected;
    var rcOk = rc.ok;
    var kv = [
      ['网关', (d.gateway||'-') + (conn ? '' : ' · 已断开'), conn ? 'ok' : 'down'],
      ['接入点', d.endpoint || '(直接构造)', ''],
      ['账号', d.account_id || '(未配置)', ''],
      ['档位', d.readonly ? '只读（只查不下单）' : '可下单',
        d.readonly ? 'warn' : ''],
      ['接入任务', (d.tasks||0) + ' 个', ''],
      ['回报轮询', (d.poll_interval||0) + 's', ''],
      ['已成交', String(st.fills||0) + ' 笔', ''],
      ['无法归属回报', String(st.unknown||0) + ' 笔', (st.unknown||0)>0 ? 'warn' : ''],
      ['对账', rcOk === true ? '一致' : (rcOk === false ? '不一致' : '未对账'),
        rcOk === true ? 'ok' : (rcOk === false ? 'down' : '')],
    ];
    $('live-kv').innerHTML = kv.map(function(r){
      return '<div class="kv"><span class="k">' + esc(r[0]) + '</span>' +
             '<span class="v ' + r[2] + '">' + esc(r[1]) + '</span></div>';
    }).join('');

    // ---- 券商账户（来自券商源）----
    var acct = d.broker_account || {};
    $('live-acct-hint').textContent = d.account_error
      ? ('读取失败: ' + d.account_error)
      : (d.account_at ? ('更新于 ' + d.account_at.replace('T',' ').slice(11,19) +
          ' · 每 ' + (d.account_interval||0) + 's 刷新') : '尚未读到');
    if(!Object.keys(acct).length){
      $('live-acct').innerHTML = '<div class="kv"><span class="k">账户</span>' +
        '<span class="v muted">尚无数据，可点「刷新账户」</span></div>';
    } else {
      var akv = [
        ['总资产', money(acct.total_asset), ''],
        ['可用资金', money(acct.available), ''],
        ['冻结', money(acct.frozen), (acct.frozen||0)>0 ? 'warn' : ''],
        ['持仓市值', money(acct.market_value), ''],
      ];
      $('live-acct').innerHTML = akv.map(function(r){
        return '<div class="kv"><span class="k">' + esc(r[0]) + '</span>' +
               '<span class="v ' + r[2] + '">' + esc(r[1]) + '</span></div>';
      }).join('');
    }
    var poss = d.broker_positions || {};
    var syms = Object.keys(poss);
    $('live-pos-hint').textContent = '(' + syms.length + ' 个标的)';
    if(!syms.length){
      $('live-pos-rows').innerHTML = '<tr><td colspan="4" class="muted">无持仓</td></tr>';
    } else {
      $('live-pos-rows').innerHTML = syms.sort().map(function(s){
        var p = poss[s] || {};
        return '<tr><td><b>' + esc(s) + '</b></td>' +
          '<td class="num">' + (p.size||0) + '</td>' +
          '<td class="num ' + ((p.sellable||0) < (p.size||0) ? 'warn' : '') + '">' +
            (p.sellable||0) + '</td>' +
          '<td class="num">' + money(p.avg_price, 3) + '</td></tr>';
      }).join('');
    }

    if(rcOk === false){
      var parts = [];
      if((rc.position_diffs||[]).length)
        parts.push('持仓差异 ' + rc.position_diffs.map(function(x){
          return x.symbol + ' 本地' + x.local + '/券商' + x.broker; }).join('，'));
      if(rc.cash_diff) parts.push('资金差异 ' + money(rc.cash_diff.delta));
      if((rc.open_order_diffs||[]).length) parts.push('挂单差异 ' + (rc.open_order_diffs||[]).length + ' 组');
      $('live-msg').textContent = parts.join(' | ');
      $('live-msg').className = 'hint down';
    } else { $('live-msg').textContent = ''; $('live-msg').className = 'hint'; }

    var po = d.pending_orders || [];
    $('live-pending-hint').textContent = '(' + (d.pending_count||0) + ')';
    if(!po.length){
      $('live-pending-rows').innerHTML = '<tr><td colspan="9" class="muted">无未结订单</td></tr>';
      return;
    }
    $('live-pending-rows').innerHTML = po.map(function(o){
      return '<tr><td>' + esc((o.created_at||'').replace('T',' ').slice(11,19)) + '</td>' +
        '<td>' + esc(o.task_id) + '</td><td>' + esc(o.symbol) + '</td>' +
        '<td class="' + (o.side==='BUY'?'up':'down') + '">' + esc(o.side) + '</td>' +
        '<td class="num">' + (o.size||0) + '</td>' +
        '<td class="num">' + (o.filled_size||0) + '</td>' +
        '<td>' + orderPill(o.status) + '</td>' +
        '<td class="num">' + money(o.price) + '</td>' +
        '<td class="muted">' + esc((o.reject_reason||o.reason||'').slice(0,30)) + '</td></tr>';
    }).join('');
  }

  function orderPill(s){
    var m = {FILLED:'run', SUBMITTED:'info', PARTIAL:'warn', PENDING:'info',
             CANCELLED:'off', REJECTED:'err'};
    return '<span class="pill ' + (m[s]||'off') + '">' + esc(s||'-') + '</span>';
  }

  function loadLive(){
    api('/api/live').then(renderLive).catch(function(e){
      $('live-hint').textContent = '(加载失败: ' + e.message + ')';
    });
  }

  // ==================== 回测页面 ====================
  function loadStrategies(){
    // 从后端能力清单取，不硬编码 —— 否则新增策略后下拉框里看不到，
    // 用户手填又会遇到"未注册的策略"。
    api('/api/strategies').then(function(d){
      CAPS = d || {};
      var sel = $('bt-strategy');
      var names = d.backtest_strategies || [], userSet = {};
      (d.user_strategies || []).forEach(function(n){ userSet[n] = 1; });
      if(names.length){
        var keep = sel.value;
        sel.innerHTML = '';
        names.forEach(function(n){
          var o = document.createElement('option');
          o.value = n;
          // 标注来源：用户自己写的策略和框架自带的，排错时得能一眼分清
          o.textContent = n + (userSet[n] ? '  [自定义]' : '');
          sel.appendChild(o);
        });
        if(keep && names.indexOf(keep) >= 0) sel.value = keep;
      }
      applyBtDefaults(d.backtest_defaults || {});
      toggleSpecBox();
      $('bt-params').placeholder = '如 fast=5 slow=20';
    }).catch(function(){});
  }

  // 表单里没填的字段用 config/backtest.json 的默认值。
  // 这样"面板跑的"和"命令行跑的"用的是同一份参数，结果可比。
  function applyBtDefaults(dd){
    BT_DEFAULTS = dd || {};
    if(!$('bt-symbol').value) $('bt-symbol').value = BT_DEFAULTS.symbol || '000001';
    if(!$('bt-start').value) $('bt-start').value = BT_DEFAULTS.start || '2020-01-01';
    if(!$('bt-cash').value) $('bt-cash').value = BT_DEFAULTS.cash || 100000;
    if(BT_DEFAULTS.data_source) $('bt-source').value = BT_DEFAULTS.data_source;
    if(BT_DEFAULTS.adjust) $('bt-adjust').value = BT_DEFAULTS.adjust;
    var sp = BT_DEFAULTS.strategy_params || {};
    var keys = Object.keys(sp);
    if(keys.length && !$('bt-params').value)
      $('bt-params').value = keys.map(function(k){ return k + '=' + sp[k]; }).join(' ');
    if(!BT_DEFAULTS.source) return;
    $('bt-defaults-hint').innerHTML =
      '默认参数来自 <code>' + esc(BT_DEFAULTS.source) + '</code>' +
      '：资金 ' + money(BT_DEFAULTS.cash, 0) + ' · 手续费 ' + BT_DEFAULTS.commission +
      ' · 滑点 ' + BT_DEFAULTS.slippage + ' · 默认策略 ' + esc(BT_DEFAULTS.strategy) +
      '。表单里填了就以填的为准（改 <code>config/backtest.json</code> 可换默认值）。';
  }

  // 只有声明式策略需要"规则定义"输入框
  function toggleSpecBox(){
    var isSpec = $('bt-strategy').value === 'declarative';
    $('bt-spec-wrap').classList.toggle('hidden', !isSpec);
  }

  var SPEC_TEMPLATE = {
    entry: { all: [
      { cross_up: { left: 'ma', right: 'ma',
                    left_params: { period: 5 }, right_params: { period: 20 } } },
      { factor: 'ma_spread', op: 'gt', value: 0,
        params: { fast: 5, slow: 20 } }
    ]},
    exit: { cross_down: { left: 'ma', right: 'ma',
                          left_params: { period: 5 }, right_params: { period: 20 } } }
  };

  function renderBacktest(d){
    var jobs = d.jobs || [], results = d.results || [];
    $('bt-job-hint').textContent = '(' + jobs.length + ' 个作业 · 并发上限 '
      + ((d.stats||{}).max_concurrent || '-') + ')';
    if(!jobs.length){
      $('bt-job-rows').innerHTML = '<tr><td colspan="7" class="muted">暂无</td></tr>';
    } else {
      $('bt-job-rows').innerHTML = jobs.map(function(j){
        var p = j.params || {};
        var pill = {QUEUED:'info', RUNNING:'warn', DONE:'run', FAILED:'err'}[j.status] || 'off';
        return '<tr><td><b>' + esc(j.job_id) + '</b></td>' +
          '<td><span class="pill ' + pill + '">' + esc(j.status) + '</span></td>' +
          '<td>' + esc(p.symbol) + '</td><td>' + esc(p.strategy) + '</td>' +
          '<td class="muted">' + esc(p.start) + '~' + esc(p.end) + '</td>' +
          '<td class="num">' + (j.elapsed != null ? j.elapsed.toFixed(1) + 's' : '-') + '</td>' +
          '<td class="down">' + esc((j.error||'').slice(0,40)) + '</td></tr>';
      }).join('');
    }

    $('bt-hint').textContent = '(' + results.length + ' 条)';
    if(!results.length){
      $('bt-rows').innerHTML = '<tr><td colspan="10" class="muted">暂无回测记录</td></tr>';
      return;
    }
    $('bt-rows').innerHTML = results.map(function(r){
      return '<tr>' +
        '<td class="muted">' + esc(String(r.created_at||'').replace('T',' ').slice(0,19)) + '</td>' +
        '<td><b>' + esc(r.symbol) + '</b></td>' +
        '<td>' + esc(r.strategy) + '</td>' +
        '<td class="muted">' + esc(r.start_date) + '~' + esc(r.end_date) + '</td>' +
        '<td class="num ' + cls(r.total_return) + '">' + sgn((r.total_return||0)*100,2) + '%</td>' +
        '<td class="num down">' + ((r.max_drawdown||0)*100).toFixed(2) + '%</td>' +
        '<td class="num">' + (r.sharpe||0).toFixed(2) + '</td>' +
        '<td class="num">' + (r.trade_count||0) + '</td>' +
        '<td class="num">' + ((r.win_rate||0)*100).toFixed(1) + '%</td>' +
        '<td><button class="mini" data-act="bt-detail" data-id="' + r.id + '">详情</button></td>' +
        '</tr>';
    }).join('');
  }

  function loadBacktest(){
    api('/api/backtest?limit=30').then(renderBacktest).catch(function(e){
      $('bt-hint').textContent = '(加载失败: ' + e.message + ')';
    });
  }

  function loadBacktestDetail(id){
    api('/api/backtest/' + id).then(function(d){
      if(!d || d.error){ toast(d && d.error || '加载失败', true); return; }
      btSelected = id;
      $('bt-detail').classList.remove('hidden');
      $('bt-d-title').textContent = d.symbol + ' · ' + d.strategy;
      $('bt-d-hint').textContent = d.start_date + '~' + d.end_date;
      var kv = [
        ['作业', d.task_id], ['初始资金', money(d.initial_cash)],
        ['期末权益', money(d.final_equity)],
        ['总收益', sgn((d.total_return||0)*100,2) + '%'],
        ['最大回撤', ((d.max_drawdown||0)*100).toFixed(2) + '%'],
        ['夏普', (d.sharpe||0).toFixed(3)],
        ['成交笔数', d.trade_count], ['胜率', ((d.win_rate||0)*100).toFixed(1) + '%'],
      ];
      $('bt-d-kv').innerHTML = kv.map(function(r){
        return '<div class="kv"><span class="k">' + esc(r[0]) + '</span>' +
               '<span class="v">' + esc(r[1]) + '</span></div>';
      }).join('');
      var m = d.metrics || {};
      $('bt-d-metrics').textContent = JSON.stringify(m, null, 2);
      var pts = (d.equity_curve || []).map(function(p){ return [p[0], p[1]]; });
      requestAnimationFrame(function(){ drawChart($('bt-d-chart'), pts); });
      $('bt-detail').scrollIntoView({behavior:'smooth', block:'start'});
    }).catch(function(e){ toast('加载回测详情失败: ' + e.message, true); });
  }

  function switchPage(page){
    PAGE = page;
    $('page-live').classList.toggle('hidden', page !== 'live');
    $('page-backtest').classList.toggle('hidden', page !== 'backtest');
    Array.prototype.forEach.call($('tabs').children, function(b){
      b.classList.toggle('on', b.getAttribute('data-page') === page);
    });
    if(btTimer){ clearInterval(btTimer); btTimer = null; }
    if(page === 'backtest'){
      loadBacktest();
      // 回测页 3 秒刷一次足够：作业是分钟级的
      btTimer = setInterval(loadBacktest, 3000);
    }
  }

  // 表格里的频率输入框回车/失焦即生效——改频率不该还要点第二个按钮
  document.addEventListener('change', function(ev){
    var el = ev.target;
    if(el.id === 'snap-mode'){
      post('/api/snapshot/mode', {mode: el.value}).then(function(r){
        if(r && r.ok){ toast('快照策略已切换: ' + el.value); refresh(); }
        else { toast('切换失败: ' + ((r&&r.error)||''), true); SNAP_MODE_READY = false; }
      }).catch(function(e){ toast('切换失败: ' + e.message, true); SNAP_MODE_READY = false; });
      return;
    }
    if(el.id === 'bt-strategy'){ toggleSpecBox(); return; }
    if(!el.classList || !el.classList.contains('ci')) return;
    var sym = el.getAttribute('data-sym'), per = el.getAttribute('data-per');
    var val = el.value.trim();
    if(!val) return;
    post('/api/collector/interval', {symbol: sym, period: per, interval: val})
      .then(function(r){
        if(r && r.ok && r.affected) toast('频率已改为 ' + val);
        else toast('改频率失败: ' + ((r && (r.error || ('未影响任何任务，当前值: ' + val))) || ''), true);
        loadCollector();
      }).catch(function(e){ toast('改频率失败: ' + e.message, true); });
  });

  window.addEventListener('resize', function(){
    if(selected){ api('/api/tasks/' + encodeURIComponent(selected))
      .then(function(d){ drawChart($('d-chart'), d.equity_curve||[]); }).catch(function(){}); }
  });

  refresh(); loadLogs(); loadCollector(); loadLive(); loadStrategies();
  $('bt-end').value = new Date().toISOString().slice(0,10);
  timer = setInterval(refresh, REFRESH);
  setInterval(loadLogs, 5000);
  setInterval(loadCollector, 5000);
})();
</script>
</body>
</html>
"""

__all__ = ["DASHBOARD_HTML"]
