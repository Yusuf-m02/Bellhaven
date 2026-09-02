"""Local review app: approve or reject each proposal, then push to the CRM.

Standard library only (http.server + sqlite3). Nothing is written to the CRM
except from an explicit "Apply approved" action, and that only ever executes
proposals whose status is 'approved'.
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import pipeline, store

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bellhaven CRM - review queue</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#1b1d21;--mut:#6b7280;--line:#e3e6ea;
--ok:#1a7f4b;--okbg:#e7f6ee;--no:#b42318;--nobg:#fdecea;--warn:#8a5a00;--warnbg:#fdf3e0;
--blue:#1d4ed8;--bluebg:#e8eefc}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
header{background:#12161c;color:#fff;padding:14px 22px;display:flex;gap:18px;
align-items:center;flex-wrap:wrap;position:sticky;top:0;z-index:10}
header h1{font-size:16px;margin:0;font-weight:600;letter-spacing:.2px}
header .sp{flex:1}
.counts span{margin-right:14px;font-size:12.5px;color:#9aa4b2}
.counts b{color:#fff}
button{font:inherit;border:1px solid var(--line);background:#fff;border-radius:6px;
padding:6px 12px;cursor:pointer}
button:hover{background:#f0f2f5}
button.primary{background:var(--blue);border-color:var(--blue);color:#fff}
button.ok{background:var(--okbg);border-color:#b7e2ca;color:var(--ok)}
button.no{background:var(--nobg);border-color:#f3c4bf;color:var(--no)}
button:disabled{opacity:.45;cursor:not-allowed}
.wrap{max-width:1120px;margin:0 auto;padding:18px 22px 90px}
.filters{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}
.filters button.on{background:#12161c;color:#fff;border-color:#12161c}
.card{background:var(--card);border:1px solid var(--line);border-radius:9px;
margin-bottom:12px;overflow:hidden}
.card.applied{opacity:.62}
.hd{display:flex;gap:12px;padding:13px 16px;align-items:flex-start}
.hd h3{margin:0 0 3px;font-size:14.5px;font-weight:600}
.meta{font-size:12px;color:var(--mut)}
.tag{display:inline-block;font-size:11px;padding:2px 8px;border-radius:20px;
border:1px solid var(--line);margin-right:6px;white-space:nowrap}
.tag.confident{background:var(--bluebg);border-color:#c3d3f7;color:var(--blue)}
.tag.review{background:var(--warnbg);border-color:#f0dcb4;color:var(--warn)}
.tag.approved{background:var(--okbg);border-color:#b7e2ca;color:var(--ok)}
.tag.rejected{background:var(--nobg);border-color:#f3c4bf;color:var(--no)}
.tag.applied{background:#eef0f3;color:#333}
.body{padding:0 16px 14px;border-top:1px solid var(--line);padding-top:12px}
.body p{margin:0 0 10px}
h4{margin:14px 0 6px;font-size:11.5px;text-transform:uppercase;letter-spacing:.07em;
color:var(--mut)}
ul{margin:0;padding-left:18px}
li{margin:2px 0}
pre{background:#0f1319;color:#dfe6ef;padding:11px 13px;border-radius:6px;
overflow:auto;font-size:12px;margin:0}
.acts{display:flex;gap:8px;padding:0 16px 14px;align-items:center;flex-wrap:wrap}
.hide{display:none}
.bar{position:fixed;left:0;right:0;bottom:0;background:#fff;border-top:1px solid var(--line);
padding:11px 22px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.bar .sp{flex:1}
.note{font-size:12px;color:var(--mut)}
.diff{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px}
.diff .k{color:var(--mut)}
.diff .old{color:var(--no);text-decoration:line-through}
.diff .new{color:var(--ok);font-weight:600}
#toast{position:fixed;bottom:70px;left:50%;transform:translateX(-50%);background:#12161c;
color:#fff;padding:9px 16px;border-radius:7px;font-size:13px;opacity:0;transition:.2s}
#toast.on{opacity:1}
</style></head><body>
<header>
  <h1>Bellhaven CRM &mdash; review queue</h1>
  <div class="counts" id="counts"></div>
  <div class="sp"></div>
  <button onclick="expandAll()">Expand all</button>
</header>
<div class="wrap">
  <div class="filters" id="filters"></div>
  <div id="list"></div>
</div>
<div class="bar">
  <button class="ok" onclick="bulk('approved')">Approve all visible</button>
  <button class="no" onclick="bulk('rejected')">Reject all visible</button>
  <div class="sp"></div>
  <span class="note" id="applynote"></span>
  <button onclick="doApply(true)">Dry run</button>
  <button class="primary" onclick="doApply(false)">Apply approved to CRM</button>
</div>
<div id="toast"></div>
<script>
let ALL=[], FILTER='pending';
const KINDS={create_account:'Create',reparent:'Re-parent',chow:'CHOW',
chow_link:'CHOW link',update_fields:'Field fix',mark_duplicate:'Duplicate',
ambiguous_match:'Ambiguous',offsite:'Off-site'};
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function toast(m){const t=document.getElementById('toast');t.textContent=m;t.className='on';
setTimeout(()=>t.className='',2200);}

async function load(){
  const r=await fetch('/api/proposals');ALL=await r.json();
  const c={};ALL.forEach(p=>c[p.status]=(c[p.status]||0)+1);
  document.getElementById('counts').innerHTML=
    ['pending','approved','rejected','applied','failed','obsolete']
    .filter(k=>c[k]).map(k=>`<span>${k} <b>${c[k]}</b></span>`).join('');
  const kinds=[...new Set(ALL.map(p=>p.kind))].sort();
  document.getElementById('filters').innerHTML=
    [['pending','Pending'],['approved','Approved'],['rejected','Rejected'],
     ['applied','Applied'],['all','All']].map(([k,l])=>
      `<button class="${FILTER===k?'on':''}" onclick="setF('${k}')">${l}</button>`).join('')
    +' &nbsp; '+kinds.map(k=>
      `<button class="${FILTER==='kind:'+k?'on':''}" onclick="setF('kind:${k}')">${KINDS[k]||k}</button>`).join('');
  render();
}
function setF(f){FILTER=f;load();}
function visible(){
  if(FILTER==='all')return ALL;
  if(FILTER.startsWith('kind:'))return ALL.filter(p=>p.kind===FILTER.slice(5));
  return ALL.filter(p=>p.status===FILTER);
}
function diffHtml(p){
  const rows=[];
  for(const a of p.actions){
    if(a.op==='create'){
      rows.push(`<div class="k">CREATE new account</div>`);
      for(const [k,v] of Object.entries(a.fields))
        if(k!=='note')rows.push(`<div><span class="k">${esc(k)}</span> = <span class="new">${esc(v)}</span></div>`);
    }else{
      rows.push(`<div class="k">PATCH ${esc(a.account_id)}</div>`);
      for(const [k,v] of Object.entries(a.fields)){
        if(k==='note')continue;
        const before=(p.before&&p.before[a.account_id]&&p.before[a.account_id][k])||'';
        rows.push(`<div><span class="k">${esc(k)}</span> <span class="old">${esc(before)||'(empty)'}</span> &rarr; <span class="new">${esc(v)}</span></div>`);
      }
    }
  }
  return `<div class="diff">${rows.join('')}</div>`;
}
function render(){
  const v=visible();
  document.getElementById('list').innerHTML = v.length? v.map(p=>`
  <div class="card ${p.status==='applied'?'applied':''}" id="c-${p.fingerprint}">
    <div class="hd">
      <div style="flex:1">
        <h3>${esc(p.title)}</h3>
        <div class="meta">
          <span class="tag">${KINDS[p.kind]||p.kind}</span>
          <span class="tag ${p.severity}">${p.severity}</span>
          <span class="tag ${p.status}">${p.status}</span>
          ${p.location_name?esc(p.location_name)+' &middot; ':''}${esc(p.account_id||'')}
          &middot; <code>${p.fingerprint.slice(0,8)}</code>
        </div>
      </div>
      <button onclick="tog('${p.fingerprint}')">Details</button>
    </div>
    <div class="body hide" id="b-${p.fingerprint}">
      <p>${esc(p.rationale)}</p>
      <h4>Proposed writes</h4>${diffHtml(p)}
      <h4>Evidence</h4><ul>${p.evidence.map(e=>`<li>${esc(e)}</li>`).join('')}</ul>
      <h4>Note written to the record</h4>
      <pre>${esc(p.actions.map(a=>a.fields&&a.fields.note).filter(Boolean).join('\\n\\n')||'(none)')}</pre>
      ${p.decision_note?`<h4>Reviewer note</h4><p>${esc(p.decision_note)}</p>`:''}
      ${p.apply_result_json?`<h4>Apply result</h4><pre>${esc(p.apply_result_json)}</pre>`:''}
    </div>
    <div class="acts">
      <button class="ok" ${['applied','rejected','approved'].includes(p.status)?'disabled':''}
        onclick="decide('${p.fingerprint}','approved')">Approve</button>
      <button class="no" ${['applied','rejected'].includes(p.status)?'disabled':''}
        onclick="decide('${p.fingerprint}','rejected')">Reject</button>
      ${p.status==='approved'?`<button onclick="decide('${p.fingerprint}','pending')">Undo</button>`:''}
    </div>
  </div>`).join('') : '<p class="note">Nothing here.</p>';
}
function tog(fp){document.getElementById('b-'+fp).classList.toggle('hide');}
function expandAll(){document.querySelectorAll('.body').forEach(e=>e.classList.remove('hide'));}
async function decide(fp,d){
  const note=d==='rejected'?(prompt('Why reject? (optional)')||''):'';
  await fetch('/api/decide',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({fingerprint:fp,decision:d,note})});
  load();
}
async function bulk(d){
  const fps=visible().filter(p=>!['applied','rejected'].includes(p.status)).map(p=>p.fingerprint);
  if(!fps.length)return toast('nothing to do');
  if(!confirm(`${d==='approved'?'Approve':'Reject'} ${fps.length} proposal(s)?`))return;
  await fetch('/api/bulk',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({fingerprints:fps,decision:d})});
  load();
}
async function doApply(dry){
  const n=ALL.filter(p=>p.status==='approved').length;
  if(!n)return toast('no approved proposals');
  if(!dry&&!confirm(`Write ${n} approved proposal(s) to the CRM?`))return;
  document.getElementById('applynote').textContent='working...';
  const r=await fetch('/api/apply',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({dry_run:dry})});
  const j=await r.json();
  document.getElementById('applynote').textContent=
    dry?`${j.calls.length} call(s) planned`:`applied ${j.applied}, failed ${j.failed}, skipped ${j.skipped}`;
  if(dry)alert(JSON.stringify(j.calls,null,2).slice(0,6000));
  load();
}
load();
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    store_: store.Store = None  # type: ignore[assignment]

    def log_message(self, fmt, *args):  # quieter console
        pass

    # ------------------------------------------------------------ plumbing ---
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, default=str).encode(), "application/json")

    def _read(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    # ---------------------------------------------------------------- GETs ---
    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        if u.path == "/api/proposals":
            status = (parse_qs(u.query).get("status") or [None])[0]
            rows = self.store_.list_proposals(status)
            accounts = _account_index()
            for r in rows:
                r["actions"] = json.loads(r.pop("actions_json"))
                r["evidence"] = json.loads(r.pop("evidence_json"))
                r["before"] = {a["account_id"]: accounts.get(a["account_id"], {})
                               for a in r["actions"] if a.get("account_id")}
            return self._json(rows)
        if u.path == "/api/summary":
            return self._json(self.store_.counts())
        return self._send(404, b"not found", "text/plain")

    # --------------------------------------------------------------- POSTs ---
    def do_POST(self):  # noqa: N802
        u = urlparse(self.path)
        try:
            if u.path == "/api/decide":
                b = self._read()
                self.store_.decide(b["fingerprint"], b["decision"],
                                   by="review-app", note=b.get("note", ""))
                return self._json({"ok": True})
            if u.path == "/api/bulk":
                b = self._read()
                done = 0
                for fp in b["fingerprints"]:
                    try:
                        self.store_.decide(fp, b["decision"], by="review-app",
                                           note=b.get("note", ""))
                        done += 1
                    except ValueError:
                        pass
                return self._json({"ok": True, "decided": done})
            if u.path == "/api/apply":
                b = self._read()
                summary = pipeline.stage_apply(self.store_, dry_run=bool(b.get("dry_run")))
                return self._json(summary)
        except Exception as e:  # noqa: BLE001 - surface it in the UI
            return self._json({"ok": False, "error": str(e)}, 400)
        return self._send(404, b"not found", "text/plain")


def _account_index() -> dict:
    from . import crm
    try:
        return {a["account_id"]: a for a in crm.load_snapshot()}
    except Exception:  # noqa: BLE001
        return {}


def serve(host: str, port: int) -> None:
    Handler.store_ = store.Store()
    counts = Handler.store_.counts()
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"Review app on http://{host}:{port}   proposals: {counts}")
    print("Ctrl-C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
