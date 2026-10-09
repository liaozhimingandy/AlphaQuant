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
  <div class="seg" id="freq">
    <button data-v="1000">1s</button>
    <button data-v="2000" class="on">2s</button>
    <button data-v="5000">5s</button>
    <button data-v="0">暂停</button>
  </div>
  <button id="btn-snap">立即快照</button>
  <button id="btn-stop" class="danger">停止引擎</button>
</header>

<main>
  <section id="kpis" class="kpis"></section>

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
      <div id="col-off" class="hint">本引擎未装配采集服务（回测模式或已关闭）。</div>
      <div id="col-on" class="hidden">
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
        日内数据默认只在交易时段采集（含收盘后 30 分钟缓冲），日线不限时段。</p>
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

  function renderKpis(o){
    var ts = o.tasks || [], eq = 0, pnl = 0, trades = 0, pos = 0, cash = 0;
    ts.forEach(function(t){ eq += t.equity||0; pnl += t.total_pnl||0;
      trades += t.trade_count||0; pos += t.position_size||0; cash += t.cash||0; });
    var m = o.monitor || {};
    var cards = [
      ['运行状态', (o.engine||{}).status||'-', '空闲: ' + String((o.engine||{}).idle)],
      ['任务数', ts.length, '标的 ' + (o.symbol_count||0) + ' 个'],
      ['总权益', money(eq), '现金 ' + money(cash)],
      ['总盈亏', sgn(pnl), '含浮动盈亏'],
      ['成交笔数', trades, '持仓合计 ' + pos + ' 股'],
      ['订单流', (o.orders||0), '快照 ' + (m.writes||0) + ' 份'],
      ['上次快照', m.last_snapshot_at ? m.last_snapshot_at.replace('T',' ').slice(11,19) : '-',
       '间隔 ' + (m.snapshot_interval||'-') + 's']
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
  function renderCollector(c){
    var on = c && c.available;
    $('col-off').classList.toggle('hidden', !!on);
    $('col-on').classList.toggle('hidden', !on);
    if(!on){ $('col-hint').textContent = '(未启用)'; return; }

    var jobs = c.jobs || [], st = c.state || {};
    $('col-hint').textContent = '(' + jobs.length + ' 个任务 · 交易时段限定: '
      + String(c.trading_hours_only) + ' · 下限 ' + (c.min_interval||0) + 's)';
    if(!jobs.length){
      $('col-rows').innerHTML = '<tr><td colspan="9" class="muted">没有配置采集标的。'
        + '配置见 ' + esc(c.config||'config/collector.json') + '</td></tr>'; return;
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
      $('snap-info').textContent = (m.writes||0) + ' 份';
      renderKpis(o); renderComponents(o); renderTasks(o); renderAudit(o);
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

  // 表格里的频率输入框回车/失焦即生效——改频率不该还要点第二个按钮
  document.addEventListener('change', function(ev){
    var el = ev.target;
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

  refresh(); loadLogs(); loadCollector();
  timer = setInterval(refresh, REFRESH);
  setInterval(loadLogs, 5000);
  setInterval(loadCollector, 5000);
})();
</script>
</body>
</html>
"""

__all__ = ["DASHBOARD_HTML"]
