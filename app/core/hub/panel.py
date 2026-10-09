#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------------
# @FileName    : panel.py
# @Description : Hub 聚合面板：一屏看全所有服务，并可远程启停/下发指令
#               详细数据（权益曲线/成交明细）点进各服务自己的面板看
# @Project     : AlphaQuant
# -------------------------------------------------------------------------------
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from twisted.web.resource import Resource
from twisted.web.server import Site

from app.core.bus.filebus import FileBus
from app.utils.jsonio import dumps

HUB_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>AlphaQuant 服务编排</title>
<style>
  :root{--bg:#f5f7fa;--panel:#fff;--line:#e3e8ef;--text:#1f2937;--muted:#6b7280;
        --accent:#2563eb;--up:#d92b2b;--down:#0f9d58;--warn:#d97706;--ok:#0f9d58;--bad:#d92b2b;
        --shadow:0 1px 3px rgba(16,24,40,.08),0 1px 2px rgba(16,24,40,.04)}
  @media (prefers-color-scheme: dark){:root{--bg:#0f1420;--panel:#161d2c;--line:#26304a;
        --text:#e5e9f0;--muted:#94a3b8;--shadow:0 1px 3px rgba(0,0,0,.4)}}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
       font:13px/1.5 -apple-system,"Segoe UI","Microsoft YaHei",Roboto,sans-serif}
  header{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:14px;flex-wrap:wrap;
         padding:12px 20px;background:var(--panel);border-bottom:1px solid var(--line);box-shadow:var(--shadow)}
  header h1{font-size:15px;margin:0;display:flex;align-items:center;gap:8px}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--accent);display:inline-block}
  .meta{display:flex;gap:16px;flex-wrap:wrap;color:var(--muted);font-size:12px}
  .meta b{color:var(--text)}
  .spacer{flex:1}
  button{cursor:pointer;border:1px solid var(--line);background:var(--panel);color:var(--text);
         border-radius:6px;padding:5px 11px;font-size:12px;transition:.15s}
  button:hover{border-color:var(--accent);color:var(--accent)}
  button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
  button.primary:hover{color:#fff;opacity:.9}
  button.danger:hover{border-color:var(--bad);color:var(--bad)}
  button.mini{padding:2px 8px;font-size:11px;border-radius:5px}
  main{padding:16px 20px 60px;display:flex;flex-direction:column;gap:16px;max-width:1680px;margin:0 auto}
  .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
  .kpi{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px;box-shadow:var(--shadow)}
  .kpi .k{color:var(--muted);font-size:11px;letter-spacing:.4px}
  .kpi .v{font-size:20px;font-weight:650;margin-top:4px;font-variant-numeric:tabular-nums}
  .svc-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:12px}
  .svc{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px;box-shadow:var(--shadow)}
  .svc .top{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:8px}
  .svc .nm{font-weight:650;font-size:13px}
  .svc .role{color:var(--muted);font-size:11px;margin-top:2px}
  .svc .rowline{display:flex;gap:10px;flex-wrap:wrap;color:var(--muted);font-size:11px;margin:6px 0}
  .svc .rowline b{color:var(--text);font-weight:600}
  .svc .acts{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}
  .pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;border:1px solid transparent}
  .pill.ok{background:rgba(15,157,88,.12);color:var(--ok);border-color:rgba(15,157,88,.3)}
  .pill.off{background:rgba(107,114,128,.14);color:var(--muted);border-color:rgba(107,114,128,.3)}
  .pill.bad{background:rgba(217,43,43,.12);color:var(--bad);border-color:rgba(217,43,43,.3)}
  .pill.warn{background:rgba(217,119,6,.12);color:var(--warn);border-color:rgba(217,119,6,.3)}
  .pill.info{background:rgba(37,99,235,.12);color:var(--accent);border-color:rgba(37,99,235,.3)}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;box-shadow:var(--shadow)}
  .panel>h2{margin:0;padding:11px 16px;font-size:13px;border-bottom:1px solid var(--line)}
  .panel>.body{padding:14px 16px}
  .table-wrap{overflow:auto;border:1px solid var(--line);border-radius:8px}
  table{width:100%;border-collapse:collapse;font-size:12px;min-width:820px}
  th,td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}
  th{background:rgba(37,99,235,.04);color:var(--muted);font-weight:600;font-size:11px}
  tbody tr:hover{background:rgba(37,99,235,.045)}
  .num{text-align:right;font-variant-numeric:tabular-nums}
  .up{color:var(--up)} .down{color:var(--down)} .muted{color:var(--muted)}
  a{color:var(--accent);text-decoration:none}
  a:hover{text-decoration:underline}
  .bus-topic{border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin-bottom:8px}
  .bus-topic .k{font-weight:600;font-size:12px}
  .bus-topic pre{margin:6px 0 0;font:11px/1.5 ui-monospace,Consolas,monospace;color:var(--muted);
                 white-space:pre-wrap;word-break:break-all;max-height:110px;overflow:auto}
  #toast{position:fixed;right:18px;bottom:18px;z-index:80;display:flex;flex-direction:column;gap:8px}
  .toast{background:#111827;color:#fff;padding:9px 14px;border-radius:8px;font-size:12px;max-width:420px}
  .toast.bad{background:#b91c1c}
</style>
</head>
<body>
<header>
  <h1><span class="dot"></span>AlphaQuant 服务编排</h1>
  <div class="meta">
    <span>Hub <b id="hub-addr">-</b></span>
    <span>状态目录 <b id="state-dir">-</b></span>
    <span>心跳超时 <b id="hb-timeout">-</b>s</span>
    <span>更新 <b id="updated">-</b></span>
  </div>
  <div class="spacer"></div>
  <button id="btn-start-all" class="primary">启动全部</button>
  <button id="btn-stop-all" class="danger">停止全部</button>
</header>

<main>
  <section id="kpis" class="kpis"></section>
  <section>
    <div class="svc-grid" id="services"></div>
  </section>
  <section class="panel">
    <h2>全部任务（跨服务聚合）</h2>
    <div class="body"><div class="table-wrap"><table>
      <thead><tr><th>服务</th><th>任务ID</th><th>标的</th><th>策略</th><th>状态</th>
      <th class="num">权益</th><th class="num">总盈亏</th><th class="num">持仓</th><th class="num">成交</th><th>操作</th></tr></thead>
      <tbody id="task-rows"></tbody></table></div></div>
  </section>
  <section class="panel">
    <h2>协作总线（服务间共享的行情 / 新闻）</h2>
    <div class="body" id="bus"><span class="muted">加载中…</span></div>
  </section>
</main>
<div id="toast"></div>

<script>
(function(){
  var REFRESH = 2000, timer = null, last = null;
  function $(id){ return document.getElementById(id); }
  function esc(s){ return String(s==null?'':s).replace(/[&<>"]/g,function(c){
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];}); }
  function money(v,d){ d=d==null?2:d; if(v==null||isNaN(v)) return '-';
      return Number(v).toLocaleString('zh-CN',{minimumFractionDigits:d,maximumFractionDigits:d}); }
  function sgn(v,d){ if(v==null||isNaN(v)) return '-'; return (v>0?'+':'')+money(v,d); }
  function cls(v){ return v>0?'up':(v<0?'down':'muted'); }
  function toast(m,bad){ var e=document.createElement('div'); e.className='toast'+(bad?' bad':'');
      e.textContent=m; $('toast').appendChild(e); setTimeout(function(){e.remove();}, bad?5200:2600); }
  function api(p,o){ return fetch(p,o).then(function(r){ return r.text().then(function(t){
      var d=null; try{ d=t?JSON.parse(t):null; }catch(e){ d={raw:t}; }
      if(!r.ok) throw new Error((d&&(d.error||d.raw))||('HTTP '+r.status)); return d; }); }); }
  function post(p,b){ return api(p,{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(b||{})}); }

  function healthPill(h){
    var m={healthy:'ok',stopped:'off',stale:'bad',unknown:'warn'};
    var t={healthy:'健康',stopped:'已停止',stale:'失联',unknown:'未知'};
    return '<span class="pill '+(m[h]||'off')+'">'+(t[h]||h||'-')+'</span>';
  }

  function render(data){
    var svcs = data.services || [];
    var hub = data.hub || {};
    $('hub-addr').textContent = (hub.host||'')+':'+(hub.port||'');
    $('state-dir').textContent = hub.state_dir||'-';
    $('hb-timeout').textContent = hub.heartbeat_timeout||'-';
    $('updated').textContent = (data.updated_at||'').replace('T',' ').slice(11,19);

    var alive=0, tasks=0, eq=0, pnl=0, trades=0;
    svcs.forEach(function(s){
      if(s.alive) alive++;
      var st=s.state||{}, sum=st.summary||{};
      tasks += sum.task_count||0; eq += sum.total_equity||0;
      pnl += sum.total_pnl||0; trades += sum.trade_count||0;
    });
    var cards=[['服务总数', svcs.length, '存活 '+alive],
               ['任务总数', tasks, '跨服务聚合'],
               ['总权益', money(eq), '全服务合计'],
               ['总盈亏', sgn(pnl), '含浮动'],
               ['成交笔数', trades, '']];
    $('kpis').innerHTML = cards.map(function(c,i){
      var c2 = (i===3 && typeof c[1]==='string') ? (c[1].charAt(0)==='+'?'up':(c[1].charAt(0)==='-'?'down':'')) : '';
      return '<div class="kpi"><div class="k">'+esc(c[0])+'</div><div class="v '+c2+'">'+
        (typeof c[1]==='number'?money(c[1],0):esc(c[1]))+'</div><div class="k">'+esc(c[2])+'</div></div>';
    }).join('');

    $('services').innerHTML = svcs.map(function(s){
      var sp=s.spec||{}, st=s.state||{}, sum=st.summary||{}, mon=st.monitor||{};
      var age = s.heartbeat_age==null ? '-' : (s.heartbeat_age+'s 前');
      return '<div class="svc">'+
        '<div class="top"><div><div class="nm">'+esc(s.service_id)+'</div>'+
          '<div class="role">'+esc(s.role||'')+(sp.mode?(' · '+esc(sp.mode)):'')+'</div></div>'+
          healthPill(s.health)+'</div>'+
        '<div class="rowline">'+
          '<span>PID <b>'+(s.pid||'-')+'</b></span>'+
          '<span>心跳 <b>'+age+'</b></span>'+
          '<span>重启 <b>'+(s.restarts||0)+'</b></span>'+
          '<span>发布 <b>'+esc((sp.publish||[]).join(',')||'—')+'</b></span>'+
          '<span>订阅 <b>'+esc((sp.subscribe||[]).join(',')||'—')+'</b></span>'+
        '</div>'+
        '<div class="rowline">'+
          '<span>任务 <b>'+(sum.task_count||0)+'</b></span>'+
          '<span>权益 <b>'+money(sum.total_equity||0)+'</b></span>'+
          '<span class="'+cls(sum.total_pnl)+'">盈亏 <b>'+sgn(sum.total_pnl||0)+'</b></span>'+
          '<span>成交 <b>'+(sum.trade_count||0)+'</b></span>'+
          '<span>快照 <b>'+(mon.writes||0)+'</b> 份</span>'+
        '</div>'+
        '<div class="acts">'+
          (s.panel_url ? '<a href="'+esc(s.panel_url)+'" target="_blank"><button class="mini">打开详情面板</button></a>' : '')+
          (s.alive ? '<button class="mini danger" data-act="stop" data-id="'+esc(s.service_id)+'">停止</button>'
                   : '<button class="mini primary" data-act="start" data-id="'+esc(s.service_id)+'">启动</button>')+
          '<button class="mini" data-act="restart" data-id="'+esc(s.service_id)+'">重启</button>'+
        '</div>'+
      '</div>';
    }).join('');

    var rows=[];
    svcs.forEach(function(s){
      var st=s.state||{};
      (st.tasks||[]).forEach(function(t){
        var ret = (t.equity && t.initial_cash) ? (t.total_pnl/t.initial_cash*100) : 0;
        rows.push('<tr><td>'+esc(s.service_id)+'</td><td><b>'+esc(t.task_id)+'</b></td>'+
          '<td>'+esc(t.symbol)+'</td><td>'+esc(t.strategy)+'</td><td>'+esc(t.status)+'</td>'+
          '<td class="num">'+money(t.equity)+'</td>'+
          '<td class="num '+cls(t.total_pnl)+'">'+sgn(t.total_pnl)+'</td>'+
          '<td class="num">'+(t.position_size||0)+'</td>'+
          '<td class="num">'+(t.trade_count||0)+'</td>'+
          '<td><div class="acts">'+
            (t.status==='RUNNING'
              ? '<button class="mini" data-act="pause" data-id="'+esc(s.service_id)+'" data-task="'+esc(t.task_id)+'">暂停</button>'
              : '<button class="mini" data-act="resume" data-id="'+esc(s.service_id)+'" data-task="'+esc(t.task_id)+'">恢复</button>')+
            '<button class="mini danger" data-act="remove" data-id="'+esc(s.service_id)+'" data-task="'+esc(t.task_id)+'">删除</button>'+
          '</div></td></tr>');
      });
    });
    $('task-rows').innerHTML = rows.length ? rows.join('')
      : '<tr><td colspan="10" class="muted">没有运行中的任务</td></tr>';
  }

  function loadBus(){
    api('/api/bus').then(function(d){
      var ts = d.topics || [];
      if(!ts.length){ $('bus').innerHTML='<span class="muted">总线还没有任何消息</span>'; return; }
      $('bus').innerHTML = ts.map(function(t){
        var msgs = (t.recent||[]).map(function(r){
          return '  ['+String(r.ts||'').slice(11,19)+'] '+String(r.node||'')+' → '+
                 JSON.stringify(r.payload||{}).slice(0,160);
        }).join('\n');
        return '<div class="bus-topic"><div class="k">'+esc(t.topic)+
          ' <span class="muted">('+ (t.size_bytes/1024).toFixed(1) +' KB)</span></div>'+
          '<pre>'+esc(msgs || '（无消息）')+'</pre></div>';
      }).join('');
    }).catch(function(e){ $('bus').innerHTML='<span class="muted">读取总线失败: '+esc(e.message)+'</span>'; });
  }

  function act(path, body, label){
    return post(path, body).then(function(r){
      if(r && r.ok === false){ toast(label+' 失败: '+(r.error||''), true); return r; }
      toast(label+' 成功'); refresh(); return r;
    }).catch(function(e){ toast(label+' 失败: '+e.message, true); });
  }

  document.addEventListener('click', function(ev){
    var b = ev.target.closest('button'); if(!b || !b.dataset.act) return;
    var a=b.dataset.act, sid=b.dataset.id, tid=b.dataset.task;
    if(a==='start')   return act('/api/service/'+encodeURIComponent(sid)+'/start', {}, '启动服务');
    if(a==='stop'){
      if(!confirm('确认停止服务 '+sid+' ？')) return;
      return act('/api/service/'+encodeURIComponent(sid)+'/stop', {}, '停止服务');
    }
    if(a==='restart'){
      if(!confirm('确认重启服务 '+sid+' ？')) return;
      return act('/api/service/'+encodeURIComponent(sid)+'/restart', {}, '重启服务');
    }
    if(a==='pause')   return act('/api/service/'+encodeURIComponent(sid)+'/command',
      {action:'pause_task', task_id:tid}, '暂停任务');
    if(a==='resume')  return act('/api/service/'+encodeURIComponent(sid)+'/command',
      {action:'resume_task', task_id:tid}, '恢复任务');
    if(a==='remove'){
      if(!confirm('确认删除任务 '+tid+' ？')) return;
      return act('/api/service/'+encodeURIComponent(sid)+'/command',
        {action:'remove_task', task_id:tid}, '删除任务');
    }
    if(b.id==='btn-start-all') return act('/api/hub/start-all', {}, '启动全部');
    if(b.id==='btn-stop-all'){
      if(!confirm('确认停止全部服务？')) return;
      return act('/api/hub/stop-all', {}, '停止全部');
    }
  });

  function refresh(){
    api('/api/hub').then(function(d){ last=d; render(d); })
      .catch(function(e){ $('services').innerHTML='<div class="muted">连接失败: '+esc(e.message)+'</div>'; });
  }
  refresh(); loadBus();
  timer = setInterval(refresh, REFRESH);
  setInterval(loadBus, 5000);
})();
</script>
</body>
</html>
"""


class HubResource(Resource):
    """Hub 面板与编排 API。"""

    isLeaf = True

    def __init__(self, supervisor: Any, bus_dir: str = "") -> None:
        Resource.__init__(self)
        self.supervisor = supervisor
        self.bus_dir = bus_dir or None

    # ---------------- 输出 ----------------
    def _json(self, request, payload: Any, status: int = 200) -> bytes:
        request.setResponseCode(status)
        request.setHeader(b"content-type", b"application/json; charset=utf-8")
        request.setHeader(b"cache-control", b"no-store")
        return dumps(payload).encode("utf-8")

    @staticmethod
    def _segments(request) -> List[str]:
        return [s for s in request.path.decode("utf-8", "replace").split("/") if s]

    @staticmethod
    def _body(request) -> Dict[str, Any]:
        raw = request.content.read()
        if not raw:
            return {}
        import json

        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    # ---------------- GET ----------------
    def render_GET(self, request):  # noqa: N802
        seg = self._segments(request)
        try:
            if not seg:
                request.setHeader(b"content-type", b"text/html; charset=utf-8")
                request.setHeader(b"cache-control", b"no-store")
                return HUB_HTML.encode("utf-8")
            if seg[0] == "favicon.ico":
                request.setResponseCode(204)
                return b""
            if seg[:1] != ["api"]:
                return self._json(request, {"error": "not found"}, 404)

            if seg[1:2] == ["hub"]:
                data = self.supervisor.status(with_state=True)
                data["updated_at"] = datetime.now().isoformat()
                return self._json(request, data)
            if seg[1:2] == ["bus"]:
                return self._json(request, self._bus_view())
            if seg[1:2] == ["service"] and len(seg) >= 3:
                st = self.supervisor.service_status(seg[2])
                st["commands"] = self.supervisor.list_commands(seg[2])
                return self._json(request, st)
            if seg[1:2] == ["config"]:
                return self._json(request, self.supervisor.hub.to_dict())
            return self._json(request, {"error": f"未知接口: /{'/'.join(seg)}"}, 404)
        except Exception as exc:
            return self._json(request, {"error": str(exc)}, 500)

    # ---------------- POST ----------------
    def render_POST(self, request):  # noqa: N802
        seg = self._segments(request)
        body = self._body(request)
        try:
            if seg[:1] != ["api"]:
                return self._json(request, {"ok": False, "error": "not found"}, 404)

            if seg[1:2] == ["hub"] and len(seg) >= 3:
                if seg[2] == "start-all":
                    return self._json(request, {"ok": True, "result": self.supervisor.start_all()})
                if seg[2] == "stop-all":
                    return self._json(request, {"ok": True, "result": self.supervisor.stop_all()})
                if seg[2] == "poll":
                    self.supervisor.poll()
                    return self._json(request, {"ok": True})

            if seg[1:2] == ["service"] and len(seg) >= 4:
                sid, action = seg[2], seg[3]
                if action == "start":
                    return self._json(request, self.supervisor.start_service(sid))
                if action == "stop":
                    return self._json(request, self.supervisor.stop_service(sid))
                if action == "restart":
                    return self._json(request, self.supervisor.restart_service(sid))
                if action == "command":
                    return self._json(request, self.supervisor.send_command(sid, body))
                if action in ("pause", "resume", "remove"):
                    mapping = {"pause": "pause_task", "resume": "resume_task",
                               "remove": "remove_task"}
                    return self._json(request, self.supervisor.send_command(
                        sid, {"action": mapping[action], "task_id": body.get("task_id", "")}))
            return self._json(request, {"ok": False, "error": f"未知接口: /{'/'.join(seg)}"}, 404)
        except Exception as exc:
            return self._json(request, {"ok": False, "error": str(exc)}, 500)

    # ---------------- 总线视图 ----------------
    def _bus_view(self) -> Dict[str, Any]:
        bus = FileBus(base_dir=self.bus_dir, node_id="hub-panel")
        out: List[Dict[str, Any]] = []
        for topic in bus.topics():
            try:
                size = bus.topic_path(topic).stat().st_size
            except OSError:
                size = 0
            out.append({
                "topic": topic,
                "size_bytes": size,
                "recent": bus.tail(topic, 8),
            })
        return {"bus_dir": str(bus.base_dir), "topics": out}


def build_hub_site(supervisor: Any, bus_dir: str = "") -> Site:
    return Site(HubResource(supervisor, bus_dir=bus_dir))


__all__ = ["HUB_HTML", "HubResource", "build_hub_site"]
