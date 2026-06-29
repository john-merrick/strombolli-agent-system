"""The Stromboli watchtower — a local web dashboard over the runs registry.

A read-mostly FastAPI surface the operator opens in a browser to see, live: what
is running and in which node, per-node timing, the SDK turn feed, tokens/cost,
the PR, and the outcome — plus a completed-task roll-up for reporting. It is also
the control plane: a Cancel button sets the cooperative cancel flag (the run
stops cleanly at the next node boundary), with an optional force-kill.

Everything reads from :class:`~stromboli.observability.runs.RunsRegistry`, so the
dashboard runs as a separate process from the task runs (multi-process SQLite).
"""

from __future__ import annotations

import os
import signal
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from stromboli.observability.runs import RunsRegistry


def create_dashboard(registry: RunsRegistry) -> FastAPI:
    """Build the dashboard FastAPI app over ``registry``."""
    app = FastAPI(title="Stromboli Watchtower")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _INDEX_HTML

    @app.get("/api/summary")
    def summary() -> dict[str, Any]:
        return registry.summary()

    @app.get("/api/runs")
    def runs() -> list[dict[str, Any]]:
        return registry.list_runs()

    @app.get("/api/runs/{run_id}")
    def run(run_id: str) -> dict[str, Any]:
        detail = registry.get_run(run_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="run not found")
        return detail

    @app.post("/api/runs/{run_id}/cancel")
    def cancel(run_id: str, force: bool = False) -> JSONResponse:
        detail = registry.get_run(run_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="run not found")
        registry.request_cancel(run_id)  # cooperative: stops at next node boundary
        killed = False
        if force and detail.get("pid"):
            try:
                os.kill(int(detail["pid"]), signal.SIGTERM)
                killed = True
            except (ProcessLookupError, PermissionError, ValueError):
                killed = False
        return JSONResponse({"cancel_requested": True, "killed": killed})

    return app


_INDEX_HTML = """<!doctype html><html><head><meta charset=utf-8>
<title>Stromboli Watchtower</title>
<style>
 body{font:14px/1.45 -apple-system,Segoe UI,sans-serif;margin:0;background:#0e1116;color:#d7dde5}
 header{padding:12px 18px;background:#161b22;border-bottom:1px solid #30363d;display:flex;gap:24px;align-items:center}
 h1{font-size:16px;margin:0;color:#fff} .muted{color:#8b949e}
 .wrap{display:flex;gap:16px;padding:16px;align-items:flex-start}
 .col{flex:1;min-width:0} .card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px;margin-bottom:12px}
 .row{display:flex;justify-content:space-between;gap:8px;padding:8px;border-radius:6px;cursor:pointer}
 .row:hover{background:#1f2630} table{width:100%;border-collapse:collapse} td,th{text-align:left;padding:6px 8px;border-bottom:1px solid #21262d}
 .pill{padding:2px 8px;border-radius:10px;font-size:12px} .running{background:#1f6feb;color:#fff} .done{background:#238636;color:#fff}
 .escalated{background:#9e6a03;color:#fff} .failed{background:#da3633;color:#fff} .cancelled{background:#6e7681;color:#fff}
 button{background:#21262d;color:#d7dde5;border:1px solid #30363d;border-radius:6px;padding:4px 10px;cursor:pointer}
 button.danger{border-color:#da3633;color:#ff7b72} pre{white-space:pre-wrap;word-break:break-word;background:#0d1117;padding:8px;border-radius:6px;max-height:40vh;overflow:auto}
 h2{font-size:13px;text-transform:uppercase;letter-spacing:.5px;color:#8b949e;margin:0 0 8px}
 a{color:#58a6ff}
</style></head><body>
<header><h1>🍕 Stromboli Watchtower</h1><span id=sum class=muted>loading…</span></header>
<div class=wrap>
 <div class=col>
  <div class=card><h2>Running</h2><div id=running class=muted>—</div></div>
  <div class=card><h2>Recent</h2><table id=recent><tbody></tbody></table></div>
 </div>
 <div class=col><div class=card><h2>Detail</h2><div id=detail class=muted>select a run</div></div></div>
</div>
<script>
const fmtAgo=t=>{if(!t)return'';let s=Math.max(0,Math.floor(Date.now()/1000-t));return s<60?s+'s':Math.floor(s/60)+'m'+(s%60)+'s'};
const pill=s=>`<span class="pill ${s}">${s}</span>`;
let sel=null;
async function j(u,o){const r=await fetch(u,o);return r.json()}
async function refresh(){
 const s=await j('/api/summary');
 document.getElementById('sum').textContent=`${s.total_runs} runs · ${JSON.stringify(s.by_status)} · ${s.total_tokens} tok · $${(s.total_cost_usd||0).toFixed(2)}`;
 const runs=await j('/api/runs');
 const run=runs.filter(r=>r.status==='running');
 document.getElementById('running').innerHTML = run.length? run.map(r=>`
   <div class=row onclick="show('${r.run_id}')"><div><b>${r.task_name||r.run_id}</b><br>
   <span class=muted>node: ${r.current_node||'?'} · ${fmtAgo(r.started_at)} · ${r.total_tokens} tok</span></div>
   <div><button class=danger onclick="event.stopPropagation();cancel('${r.run_id}',false)">cancel</button></div></div>`).join('') : '<span class=muted>nothing running</span>';
 document.getElementById('recent').innerHTML='<tbody>'+runs.map(r=>`<tr class=row onclick="show('${r.run_id}')">
   <td>${pill(r.status)}</td><td>${(r.task_name||r.run_id).slice(0,40)}</td>
   <td>${r.ended_at?fmtAgo(r.ended_at)+' ago':'—'}</td><td>${r.pr_url?`<a href="${r.pr_url}" target=_blank>PR</a>`:''}</td></tr>`).join('')+'</tbody>';
 if(sel) show(sel,true);
}
async function show(id,quiet){ sel=id; const r=await j('/api/runs/'+id);
 const ev=(r.node_events||[]).map(e=>`${e.node} <span class=muted>${e.phase}</span>`).join(' → ');
 const turns=(r.turns||[]).map(t=>`#${t.idx_} [${t.tools}] ${t.output_tokens||''}`).join('\\n');
 document.getElementById('detail').innerHTML=`<div><b>${r.task_name||id}</b> ${pill(r.status)}
  ${r.status==='running'?`<button class=danger onclick="cancel('${id}',true)">force kill</button>`:''}</div>
  <p class=muted>node: ${r.current_node||'—'} · ${r.total_tokens} tok · $${(r.total_cost_usd||0).toFixed(2)} ${r.pr_url?`· <a href="${r.pr_url}" target=_blank>PR</a>`:''} ${r.error?'· <span style=color:#ff7b72>'+r.error+'</span>':''}</p>
  <h2>Phases</h2><pre>${ev||'—'}</pre><h2>SDK turns</h2><pre>${turns||'—'}</pre>`;
}
async function cancel(id,force){ await j('/api/runs/'+id+'/cancel?force='+force,{method:'POST'}); refresh(); }
refresh(); setInterval(refresh,2000);
</script></body></html>"""


__all__ = ["create_dashboard"]
