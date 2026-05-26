#!/usr/bin/env python3
"""
ml_labeler.py — Railway-backed session labeler
Fetches unlabeled raw sessions from Railway API, saves labels via API.
Supports: BOW (litter box) | FOOD (feed pad) | WATER (water pad)

Usage:
  RAILWAY_URL=https://bow-iot-backend-production.up.railway.app python ml_labeler.py
  http://localhost:8080
"""

import json, os, threading, time
import requests
from datetime import datetime, timezone
from functools import wraps
from flask import Flask, jsonify, request, Response

# ── Config ────────────────────────────────────────────────────────────────────
RAILWAY_URL   = os.environ.get("RAILWAY_URL", "https://bow-iot-backend-production.up.railway.app")
HTTP_PORT     = int(os.environ.get("PORT", 8080))
POLL_INTERVAL = 6   # seconds between Railway fetches
FETCH_LIMIT   = 100

ACCESS_USER = os.environ.get("ACCESS_USER", "bow")
ACCESS_PASS = os.environ.get("ACCESS_PASS", "")   # ถ้าไม่ set = ไม่มี auth (local mode)

app  = Flask(__name__)

# ── Basic Auth ────────────────────────────────────────────────────────────────
def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not ACCESS_PASS:
            return f(*args, **kwargs)
        auth = request.authorization
        if not auth or auth.username != ACCESS_USER or auth.password != ACCESS_PASS:
            return Response("Login required", 401,
                            {"WWW-Authenticate": 'Basic realm="Bow ML Labeler"'})
        return f(*args, **kwargs)
    return decorated
lock = threading.Lock()

sessions_cache = {}   # id → full session dict (includes points)
pending        = []   # list of session summary dicts shown in UI
labeled        = []   # recent labels (local display only, last 50)

railway_ok     = [False]  # connection status for UI indicator

# ── Railway poller ────────────────────────────────────────────────────────────

def _fetch_railway():
    while True:
        try:
            r = requests.get(
                f"{RAILWAY_URL}/api/sense-pad/raw-sessions",
                params={"unlabeled": "true", "limit": FETCH_LIMIT},
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            railway_ok[0] = True

            with lock:
                existing_ids = {s["id"]: i for i, s in enumerate(pending)}
                for sess in data:
                    sid = sess["id"]
                    sessions_cache[sid] = sess
                    # Always refresh metadata for existing entries (may arrive after first fetch)
                    if sid in existing_ids:
                        idx = existing_ids[sid]
                        pending[idx]["activity"]        = sess.get("activity")
                        pending[idx]["weight_g"]        = round(sess["weight_g"],        1) if sess.get("weight_g")        is not None else None
                        pending[idx]["waste_g"]         = round(sess["waste_g"],         1) if sess.get("waste_g")         is not None else None
                        pending[idx]["peak"]            = round(sess.get("peakTotal_g",  0), 1)
                        pending[idx]["catWeight_g"]     = round(sess["catWeight_g"],     1) if sess.get("catWeight_g")     is not None else None
                        pending[idx]["foodConsumed_g"]  = round(sess["foodConsumed_g"],  1) if sess.get("foodConsumed_g")  is not None else None
                        pending[idx]["waterFromFood_g"] = round(sess["waterFromFood_g"], 1) if sess.get("waterFromFood_g") is not None else None
                        pending[idx]["foodProfile"]     = sess.get("foodProfile")
                    if sid not in existing_ids:
                        # Derive actual session time from sessionId (unix timestamp)
                        try:
                            from datetime import timezone, timedelta
                            TZ_BKK = timezone(timedelta(hours=7))
                            ts_s = int(sess["sessionId"]) / 1000 \
                                   if int(sess["sessionId"]) > 1e12 \
                                   else int(sess["sessionId"])
                            ts_dt = datetime.fromtimestamp(ts_s, tz=TZ_BKK)
                            start_str = ts_dt.strftime("%Y-%m-%d %H:%M:%S")
                        except Exception:
                            start_str = (sess.get("uploadedAt") or "")[:19].replace("T", " ")

                        pending.append({
                            "id":              sid,
                            "sessionId":       sess["sessionId"],
                            "catId":           sess.get("catId"),
                            "padType":         sess.get("padType", "bow"),
                            "device":          sess.get("padType", "bow"),
                            "start":           start_str,
                            "uploadedAt":      (sess.get("uploadedAt") or "")[:19].replace("T", " "),
                            "durationMs":      sess.get("durationMs", 0),
                            "pointCount":      sess.get("pointCount", 0),
                            "peak":            round(sess.get("peakTotal_g", 0), 1),
                            # Bow pad metadata
                            "activity":        sess.get("activity"),
                            "weight_g":        round(sess["weight_g"],        1) if sess.get("weight_g")        is not None else None,
                            "waste_g":         round(sess["waste_g"],         1) if sess.get("waste_g")         is not None else None,
                            # Food pad metadata
                            "catWeight_g":     round(sess["catWeight_g"],     1) if sess.get("catWeight_g")     is not None else None,
                            "foodConsumed_g":  round(sess["foodConsumed_g"],  1) if sess.get("foodConsumed_g")  is not None else None,
                            "waterFromFood_g": round(sess["waterFromFood_g"], 1) if sess.get("waterFromFood_g") is not None else None,
                            "foodProfile":     sess.get("foodProfile"),
                        })
        except Exception as e:
            railway_ok[0] = False
            print(f"[POLL] Railway error: {e}")

        time.sleep(POLL_INTERVAL)

# ── API ───────────────────────────────────────────────────────────────────────

@app.route("/api/state")
@require_auth
def api_state():
    with lock:
        return jsonify({
            "railway_ok": railway_ok[0],
            "pending":    sorted(pending, key=lambda s: s["start"]),
            "labeled":    list(labeled[-20:]),
        })

@app.route("/api/label", methods=["POST"])
@require_auth
def api_label():
    body     = request.json or {}
    sess_id  = body.get("id")
    activity = body.get("activity", "")
    behavior = body.get("behavior", "")
    notes    = body.get("notes", "")

    with lock:
        sess = next((s for s in pending if s["id"] == sess_id), None)
    if not sess:
        return jsonify({"ok": False, "error": "not found"}), 404

    try:
        r = requests.patch(
            f"{RAILWAY_URL}/api/sense-pad/raw-sessions/{sess_id}/label",
            json={"wasteType": activity, "behavior": behavior,
                  "notes": notes, "labeledBy": "user"},
            timeout=10,
        )
        if r.status_code != 200:
            return jsonify({"ok": False, "error": r.text}), r.status_code
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    with lock:
        pending[:] = [s for s in pending if s["id"] != sess_id]
        sessions_cache.pop(sess_id, None)
        labeled.append({
            "id":         sess_id,
            "device":     sess.get("device", "bow"),
            "activity":   activity,
            "behavior":   behavior,
            "duration_s": round(sess.get("durationMs", 0) / 1000, 1),
            "labeled_at": datetime.now().strftime("%H:%M:%S"),
        })
        if len(labeled) > 50:
            labeled.pop(0)

    print(f"[LABEL] {sess.get('device')} id={sess_id} {activity}/{behavior}")
    return jsonify({"ok": True})

@app.route("/api/skip", methods=["POST"])
@require_auth
def api_skip():
    sess_id = (request.json or {}).get("id")
    with lock:
        pending[:] = [s for s in pending if s["id"] != sess_id]
        sessions_cache.pop(sess_id, None)
    return jsonify({"ok": True})

@app.route("/api/delete", methods=["POST"])
@require_auth
def api_delete():
    sess_id = (request.json or {}).get("id")
    try:
        r = requests.delete(
            f"{RAILWAY_URL}/api/sense-pad/raw-sessions/{sess_id}",
            timeout=10,
        )
        if r.status_code not in (200, 404):
            return jsonify({"ok": False, "error": r.text}), r.status_code
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    with lock:
        pending[:] = [s for s in pending if s["id"] != sess_id]
        sessions_cache.pop(sess_id, None)
    print(f"[DELETE] id={sess_id}")
    return jsonify({"ok": True})

@app.route("/api/session_data")
@require_auth
def api_session_data():
    sess_id = request.args.get("id", type=int)
    with lock:
        sess = sessions_cache.get(sess_id)
    if not sess:
        return jsonify([])

    raw = sess.get("points") or []
    result = []
    for p in raw:
        if isinstance(p, list):
            # [t_ms, total, fl, fr, rl, rr, dw, std]
            result.append({
                "t_ms": p[0] if len(p) > 0 else 0,
                "tot":  p[1] if len(p) > 1 else 0,
                "fl":   p[2] if len(p) > 2 else 0,
                "fr":   p[3] if len(p) > 3 else 0,
                "rl":   p[4] if len(p) > 4 else 0,
                "rr":   p[5] if len(p) > 5 else 0,
                "dw":   p[6] if len(p) > 6 else 0,
                "std":  p[7] if len(p) > 7 else 0,
            })
        elif isinstance(p, dict):
            result.append({
                "t_ms": p.get("t_ms", 0),
                "tot":  p.get("total", p.get("tot", 0)),
                "fl":   p.get("fl",  p.get("net",  0)),
                "fr":   p.get("fr",  p.get("base", 0)),
                "rl":   p.get("rl",  0),
                "rr":   p.get("rr",  0),
                "dw":   p.get("dw",  p.get("dlt", 0)),
                "std":  p.get("std", 0),
            })
    return jsonify(result)

@app.route("/")
@require_auth
def index():
    return Response(HTML, mimetype="text/html")

# ── HTML ──────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ML Labeler</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#111;color:#ddd;padding:14px;display:flex;flex-direction:column;gap:12px;font-size:14px}
header{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:8px 12px;background:#181818;border-radius:8px;border:1px solid #2a2a2a}
.dot{width:9px;height:9px;border-radius:50%;background:#333;flex-shrink:0;transition:background .4s}
.dot.on{background:#4fc3f7;box-shadow:0 0 8px #4fc3f7}
.dot.food.on{background:#a5d6a7;box-shadow:0 0 8px #a5d6a7}
.dot.water.on{background:#80deea;box-shadow:0 0 8px #80deea}
.layout{display:flex;gap:12px;align-items:flex-start}
.charts{flex:1;display:flex;flex-direction:column;gap:10px;min-width:0}
.panel{width:340px;flex-shrink:0;display:flex;flex-direction:column;gap:10px}
.card{background:#181818;border:1px solid #2a2a2a;border-radius:10px;padding:12px 14px}
.card .ttl{font-size:.75em;color:#666;text-transform:uppercase;letter-spacing:.1em;margin-bottom:10px;font-weight:600}
.filter-bar{display:flex;gap:6px;margin-bottom:10px;flex-wrap:wrap}
.f-btn{background:#1e1e1e;border:1px solid #333;color:#666;border-radius:6px;padding:5px 14px;font-size:.78em;font-weight:700;cursor:pointer;transition:all .15s;font-family:inherit}
.f-btn.active-all  {background:#252525;border-color:#555;color:#ccc}
.f-btn.active-bow  {background:#0d2535;border-color:#4fc3f7;color:#4fc3f7}
.f-btn.active-food {background:#0d2510;border-color:#a5d6a7;color:#a5d6a7}
.f-btn.active-water{background:#0a2528;border-color:#80deea;color:#80deea}
.sess{background:#1e1e1e;border:1px solid #333;border-radius:8px;padding:12px;margin-bottom:8px}
.sess .meta{font-size:.85em;color:#aaa;margin-bottom:10px;line-height:1.8}
.sess .meta .time{color:#4fc3f7;font-weight:700;font-size:1em}
.sess.food-sess .meta .time{color:#a5d6a7}
.sess.water-sess .meta .time{color:#80deea}
.tag{border-radius:4px;padding:1px 7px;font-size:.72em;font-weight:700;letter-spacing:.04em}
.tag-bow  {background:#0d2535;color:#4fc3f7}
.tag-food {background:#0d250d;color:#a5d6a7}
.tag-water{background:#0a2528;color:#80deea}
.btns{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:4px}
.grp-label{font-size:.7em;color:#555;text-transform:uppercase;letter-spacing:.06em;margin:7px 0 4px;font-weight:700}
button{border:none;border-radius:6px;padding:7px 11px;font-size:.82em;font-family:inherit;cursor:pointer;transition:all .15s;font-weight:600}
button:hover{opacity:.85;transform:translateY(-1px)}
.b-u  {background:#1565c0;color:#e3f2fd}
.b-f  {background:#5d2e22;color:#ffd7c4}
.b-uf {background:#4a148c;color:#e8d5ff}
.b-g  {background:#1b4d20;color:#c8f0cb}
.b-sl {background:#1a2a3a;color:#b0bec5}
.b-v  {background:#1e3040;color:#b3d9f5}
.b-dg {background:#2a2010;color:#ffe082}
.b-x  {background:#2a2a2a;color:#777}
.b-sc {background:#2a1a00;color:#ffb74d}
.b-sd {background:#1a2a1a;color:#a5d6a7}
.b-rb {background:#2a1a1a;color:#ef9a9a}
.b-pb {background:#1a2010;color:#c5e1a5}
.b-eat{background:#1b4d20;color:#c8f0cb}
.b-snf{background:#1e2a1e;color:#81c784}
.b-vis{background:#1e3040;color:#b3d9f5}
.b-fx {background:#2a2a2a;color:#777}
.b-ff {background:#2a1800;color:#ffcc80}
.b-rb2{background:#2a1a1a;color:#ef9a9a}
.b-pb2{background:#1a2010;color:#c5e1a5}
.b-drk{background:#0a2535;color:#80deea}
.b-snw{background:#0a1e22;color:#4dd0e1}
.b-fw {background:#0a2020;color:#26c6da}
.b-rw {background:#1a1a2a;color:#80cbc4}
.b-pw {background:#0a1a0a;color:#80deea}
.b-lick{background:#1a3a1a;color:#a5d6a7;padding:6px 12px}
.b-bit {background:#2a2010;color:#ffcc80;padding:6px 12px}
.b-chm {background:#3a1a0a;color:#ff8a65;padding:6px 12px}
.b-nor {background:#1a237e;color:#c5cae9;padding:6px 12px}
.b-rst {background:#7f1010;color:#ffcdd2;padding:6px 12px}
.b-dif {background:#7a3500;color:#ffe0b2;padding:6px 12px}
.b-lng {background:#0d3050;color:#80deea;padding:6px 12px}
.b-shrt{background:#1e2a1e;color:#a5d6a7;padding:6px 12px}
.b-skip{background:#1a1a1a;color:#555;padding:4px 10px;font-size:.75em}
.b-del {background:#2a1010;color:#ef5350;padding:4px 10px;font-size:.75em}
.b-view{background:#1a301a;color:#66bb6a;padding:5px 10px}
.beh-wrap{display:none;margin-top:8px}
.beh-label{font-size:.72em;color:#555;margin-bottom:5px;text-transform:uppercase;letter-spacing:.05em}
.done{font-size:.82em;color:#555;line-height:2}
.done b{color:#ccc}
.day-header{font-size:.78em;color:#555;padding:8px 0 4px;border-top:1px solid #252525;margin-top:4px;font-weight:700}
input.note-input{width:100%;background:#141414;border:1px solid #2e2e2e;color:#aaa;padding:6px 8px;font-size:.82em;border-radius:6px;margin:7px 0 8px;font-family:inherit}
input.note-input:focus{outline:none;border-color:#4fc3f7;color:#ddd}
.row-btns{display:flex;gap:6px;align-items:center}
.chart-tabs{display:flex;gap:6px;margin-bottom:8px}
.ct-btn{background:#1e1e1e;border:1px solid #2a2a2a;color:#555;border-radius:6px;padding:4px 12px;font-size:.75em;font-weight:700;cursor:pointer;font-family:inherit}
.ct-btn.on{border-color:#4fc3f7;color:#4fc3f7;background:#0d2030}
.ct-btn.food-on{border-color:#a5d6a7;color:#a5d6a7;background:#0d200d}
.ct-btn.water-on{border-color:#80deea;color:#80deea;background:#0a1e22}
.no-data{color:#333;font-size:.82em;padding:24px 0;text-align:center}
@media(max-width:800px){.layout{flex-direction:column}.panel{width:100%}}
</style>
</head>
<body>
<header>
  <span class="dot" id="dotRailway"></span>
  <span style="font-size:.85em;color:#888">Railway</span>
  <span id="hdrRailway" style="font-size:.85em;color:#555">connecting...</span>
  &nbsp;|&nbsp;
  <span style="font-size:.82em;color:#444">BOW</span>
  <span class="dot" id="dotBow"></span>
  <span style="font-size:.82em;color:#444">FOOD</span>
  <span class="dot food" id="dotFood"></span>
  <span style="font-size:.82em;color:#444">WATER</span>
  <span class="dot water" id="dotWater"></span>
  <span id="viewBanner" style="font-size:.85em;color:#66bb6a;font-weight:700;margin-left:8px"></span>
  <button id="btnClear" style="background:#1a1a1a;color:#555;padding:5px 12px;font-size:.8em;border:none;border-radius:6px;cursor:pointer;font-family:inherit;display:none" onclick="clearView()">✕ Clear</button>
</header>

<div class="layout">
  <div class="charts">
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <span class="ttl" style="margin:0">Weight (g)</span>
        <div class="chart-tabs">
          <button class="ct-btn on"       id="ctBow"   onclick="setChartSrc('bow')">BOW</button>
          <button class="ct-btn"          id="ctFood"  onclick="setChartSrc('food')">FOOD</button>
          <button class="ct-btn"          id="ctWater" onclick="setChartSrc('water')">WATER</button>
        </div>
      </div>
      <canvas id="cW" height="110"></canvas>
    </div>
    <div class="card"><div class="ttl" id="cCttl">Corner Load (g)</div><canvas id="cC" height="80"></canvas></div>
    <div class="card"><div class="ttl">dW/dt &amp; StdDev</div><canvas id="cS" height="65"></canvas></div>
  </div>

  <div class="panel">
    <div class="card" id="pPend">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <span style="font-size:.85em;font-weight:700;color:#ccc">PENDING &nbsp;<span id="pCount" style="color:#4fc3f7">0</span></span>
        <span style="font-size:.75em;color:#444" id="lastFetch"></span>
      </div>
      <div class="filter-bar">
        <button class="f-btn active-all" id="fAll"   onclick="setFilter('all')">All</button>
        <button class="f-btn"            id="fBow"   onclick="setFilter('bow')">🚽 BOW</button>
        <button class="f-btn"            id="fFood"  onclick="setFilter('food')">🍲 FOOD</button>
        <button class="f-btn"            id="fWater" onclick="setFilter('water')">💧 WATER</button>
      </div>
      <div id="pendList" style="max-height:60vh;overflow-y:auto;padding-right:3px"></div>
    </div>
    <div class="card"><div class="ttl">Labeled (recent)</div><div id="doneList" class="done"></div></div>
  </div>
</div>

<script>
let filterMode = 'all';
let chartSrc   = 'bow';
let viewSessId = null;
let allPending = [];
let lastPendCount = -1, errN = 0;
const pending_activity = {};

function mkLine(id,ds){
  return new Chart(document.getElementById(id),{
    type:'line',data:{labels:[],datasets:ds},
    options:{animation:false,responsive:true,
      plugins:{legend:{labels:{color:'#888',boxWidth:10,font:{size:11},padding:10}}},
      scales:{
        x:{ticks:{color:'#555',maxTicksLimit:8,font:{size:11}},grid:{color:'#1e1e1e'}},
        y:{ticks:{color:'#777',font:{size:11}},grid:{color:'#1e1e1e'}}
      }}});
}
const cW=mkLine('cW',[
  {label:'Total',data:[],borderColor:'#4fc3f7',borderWidth:1.5,pointRadius:0,tension:.2,fill:false},
  {label:'Net',  data:[],borderColor:'#a5d6a7',borderWidth:1,  pointRadius:0,tension:.2,fill:false},
]);
const cC=mkLine('cC',[
  {label:'FL',data:[],borderColor:'#ef9a9a',borderWidth:1,pointRadius:0,tension:.2,fill:false},
  {label:'FR',data:[],borderColor:'#ffcc80',borderWidth:1,pointRadius:0,tension:.2,fill:false},
  {label:'RL',data:[],borderColor:'#a5d6a7',borderWidth:1,pointRadius:0,tension:.2,fill:false},
  {label:'RR',data:[],borderColor:'#90caf9',borderWidth:1,pointRadius:0,tension:.2,fill:false},
]);
const cS=mkLine('cS',[
  {label:'dW/dt', data:[],borderColor:'#ce93d8',borderWidth:1.5,pointRadius:0,tension:.3,fill:false},
  {label:'StdDev',data:[],borderColor:'#f48fb1',borderWidth:1,  pointRadius:0,tension:.2,fill:false},
]);

function setChartSrc(src){
  chartSrc=src;
  document.getElementById('ctBow').className   ='ct-btn'+(src==='bow'  ?' on':'');
  document.getElementById('ctFood').className  ='ct-btn'+(src==='food' ?' food-on':'');
  document.getElementById('ctWater').className ='ct-btn'+(src==='water'?' water-on':'');
  if(src==='food'){
    document.getElementById('cCttl').textContent='Net / Base (g)';
    cC.data.datasets[0].label='Net'; cC.data.datasets[1].label='Base';
    cC.data.datasets[2].label='—';   cC.data.datasets[3].label='—';
  } else if(src==='water'){
    document.getElementById('cCttl').textContent='Water Weight (g)';
    cC.data.datasets[0].label='Level'; cC.data.datasets[1].label='—';
    cC.data.datasets[2].label='—';     cC.data.datasets[3].label='—';
  } else {
    document.getElementById('cCttl').textContent='Corner Load (g)';
    cC.data.datasets[0].label='FL'; cC.data.datasets[1].label='FR';
    cC.data.datasets[2].label='RL'; cC.data.datasets[3].label='RR';
  }
  cC.update('none');
}

function setChart(ch,pts,fns){
  const step=Math.max(1,Math.floor(pts.length/200));
  const sampled=pts.filter((_,i)=>i%step===0);
  ch.data.labels=sampled.map(p=>{const ms=p.t_ms||0;const s=Math.floor(ms/1000);return s+'s';});
  ch.data.datasets.forEach((d,i)=>{d.data=sampled.map(fns[i]);});
  ch.update('none');
}

function setFilter(f){
  filterMode=f;
  ['All','Bow','Food','Water'].forEach(x=>{
    const el=document.getElementById('f'+x);
    const key=x.toLowerCase();
    el.className='f-btn'+(f===key?' active-'+key:(f==='all'&&x==='All'?' active-all':''));
  });
  renderPending(allPending);
}

function renderPending(sessions){
  allPending=sessions;
  document.getElementById('pCount').textContent=sessions.length;
  const filtered=filterMode==='all'?sessions:sessions.filter(s=>s.padType===filterMode);
  const pl=document.getElementById('pendList');
  const groups={};
  filtered.forEach(s=>{const d=s.start.slice(0,10);if(!groups[d])groups[d]=[];groups[d].push(s);});
  let html='';
  Object.keys(groups).sort().reverse().forEach(day=>{
    html+=`<div class="day-header">📅 ${day} &nbsp;(${groups[day].length})</div>`;
    groups[day].sort((a,b)=>b.start.localeCompare(a.start)).forEach(s=>{html+=sessCard(s);});
  });
  pl.innerHTML=html||'<div class="no-data">ไม่มี session รอ label</div>';
}

function sessCard(s){
  const id=s.id, pad=s.padType||'bow';
  const timeStr=s.start.slice(11,19)||s.uploadedAt.slice(11,19);
  const dateStr=s.start.slice(0,10);
  const dur_s=Math.round((s.durationMs||0)/1000);
  const isBow=pad==='bow', isFood=pad==='food', isWater=pad==='water';

  const tag = isBow   ? `<span class="tag tag-bow">BOW</span>`
            : isFood  ? `<span class="tag tag-food">FOOD</span>`
                      : `<span class="tag tag-water">WATER</span>`;
  const peakLabel = isWater ? `drop <b>${s.peak}g</b>` : `peak <b>${s.peak}g</b>`;

  const bowSection=`
    <div class="grp-label">🐱 Cat</div>
    <div class="btns">
      <button class="b-u"  onclick="label(${id},'URINE')">💧 Urine</button>
      <button class="b-f"  onclick="label(${id},'FECES')">💩 Feces</button>
      <button class="b-uf" onclick="label(${id},'URINE+FECES')">💧💩 Both</button>
      <button class="b-g"  onclick="label(${id},'GROOMING')">🐱 Groom</button>
      <button class="b-sl" onclick="labelDirect(${id},'SLEEP')">😴 Sleep</button>
      <button class="b-v"  onclick="labelDirect(${id},'VISIT')">👁 Visit</button>
      <button class="b-dg" onclick="labelDirect(${id},'DIG')">🏖 Dig</button>
      <button class="b-x"  onclick="labelDirect(${id},'FALSE')">✕ False</button>
    </div>
    <div class="grp-label">🧑 Owner</div>
    <div class="btns">
      <button class="b-sc" onclick="labelDirect(${id},'SCOOP')">🧹 Scoop</button>
      <button class="b-sd" onclick="labelDirect(${id},'SAND')">⛏ Sand</button>
      <button class="b-rb" onclick="labelDirect(${id},'REMOVE_BOX')">📤 Remove Box</button>
      <button class="b-pb" onclick="labelDirect(${id},'PLACE_BOX')">📥 Place Box</button>
    </div>
    <div class="beh-wrap" id="beh${id}">
      <div class="beh-label">Behavior</div>
      <div style="display:flex;gap:5px;flex-wrap:wrap">
        <button class="b-nor" onclick="setBeh(${id},'normal')">✓ Normal</button>
        <button class="b-rst" onclick="setBeh(${id},'restless')">↯ Restless</button>
        <button class="b-dif" onclick="setBeh(${id},'difficulty')">⚠ Difficulty</button>
      </div>
    </div>`;

  const foodSection=`
    <div class="grp-label">🍲 Food Monitor</div>
    <div class="btns">
      <button class="b-eat" onclick="label(${id},'EATING')">🍽 Eating</button>
      <button class="b-snf" onclick="labelDirect(${id},'SNIFFING')">👃 Sniffing</button>
      <button class="b-vis" onclick="labelDirect(${id},'VISIT')">👁 Visit</button>
      <button class="b-fx"  onclick="labelDirect(${id},'FALSE')">✕ False</button>
    </div>
    <div class="grp-label">🧑 Owner</div>
    <div class="btns">
      <button class="b-ff"  onclick="labelDirect(${id},'FILL_FOOD')">🥣 Fill Food</button>
      <button class="b-rb2" onclick="labelDirect(${id},'REMOVE_BOWL')">📤 Remove Bowl</button>
      <button class="b-pb2" onclick="labelDirect(${id},'PLACE_BOWL')">📥 Place Bowl</button>
    </div>
    <div class="beh-wrap" id="beh${id}">
      <div class="beh-label">Eating Style</div>
      <div style="display:flex;gap:5px;flex-wrap:wrap">
        <button class="b-lick" onclick="setBeh(${id},'Licking')">👅 Licking</button>
        <button class="b-bit"  onclick="setBeh(${id},'Biting')">🦷 Biting</button>
        <button class="b-chm"  onclick="setBeh(${id},'Chomping')">⚡ Chomping</button>
      </div>
    </div>`;

  const waterSection=`
    <div class="grp-label">💧 Water Monitor</div>
    <div class="btns">
      <button class="b-drk" onclick="label(${id},'DRINKING')">💧 Drinking</button>
      <button class="b-snw" onclick="labelDirect(${id},'SNIFFING')">👃 Sniffing</button>
      <button class="b-vis" onclick="labelDirect(${id},'VISIT')">👁 Visit</button>
      <button class="b-fx"  onclick="labelDirect(${id},'FALSE')">✕ False</button>
    </div>
    <div class="grp-label">🧑 Owner</div>
    <div class="btns">
      <button class="b-fw"  onclick="labelDirect(${id},'FILL_WATER')">🪣 Fill Water</button>
      <button class="b-rw"  onclick="labelDirect(${id},'REMOVE_BOWL')">📤 Remove Bowl</button>
      <button class="b-pw"  onclick="labelDirect(${id},'PLACE_BOWL')">📥 Place Bowl</button>
    </div>
    <div class="beh-wrap" id="beh${id}">
      <div class="beh-label">Drinking Style</div>
      <div style="display:flex;gap:5px;flex-wrap:wrap">
        <button class="b-lng"  onclick="setBeh(${id},'long_drink')">🕐 Long</button>
        <button class="b-shrt" onclick="setBeh(${id},'short_drink')">⚡ Short</button>
      </div>
    </div>`;

  const section = isBow ? bowSection : isFood ? foodSection : waterSection;
  const sessClass = isBow ? '' : isFood ? ' food-sess' : ' water-sess';

  // Activity / metadata / sessionId meta line
  let extraMeta = '';
  if (s.activity) {
    extraMeta += `<span style="color:#aaa">🏷 ${s.activity}</span>`;
    if (isBow) {
      if (s.weight_g != null && s.weight_g > 0) extraMeta += ` &nbsp;·&nbsp; <span style="color:#81c784">🐱 ${s.weight_g}g</span>`;
      if (s.waste_g  != null && s.waste_g  > 0) extraMeta += ` &nbsp;·&nbsp; <span style="color:#ffb74d">💩 ${s.waste_g}g</span>`;
    }
    if (isFood) {
      if (s.catWeight_g    != null && s.catWeight_g    > 0) extraMeta += ` &nbsp;·&nbsp; <span style="color:#81c784">🐱 ${s.catWeight_g}g</span>`;
      if (s.foodConsumed_g != null && s.foodConsumed_g > 0) extraMeta += ` &nbsp;·&nbsp; <span style="color:#a5d6a7">🍽 ${s.foodConsumed_g}g</span>`;
      if (s.waterFromFood_g!= null && s.waterFromFood_g> 0) extraMeta += ` &nbsp;·&nbsp; <span style="color:#80deea">💧 ${s.waterFromFood_g}g</span>`;
      if (s.foodProfile)                                     extraMeta += ` &nbsp;·&nbsp; <span style="color:#ffcc80">📦 ${s.foodProfile}</span>`;
    }
  }
  if (s.sessionId) extraMeta += `${extraMeta?' &nbsp;·&nbsp; ':''}<span style="color:#555;font-size:.72em">SID:${s.sessionId}</span>`;

  return `<div class="sess${sessClass}" id="s${id}">
    <div class="meta">
      <span class="time">${timeStr}</span>&nbsp;${tag}&nbsp;
      <span class="info">${dateStr} &nbsp;·&nbsp; ${dur_s}s &nbsp;·&nbsp; ${peakLabel} &nbsp;·&nbsp; ${s.pointCount||0}pts${s.catId?` &nbsp;·&nbsp; cat#${s.catId}`:''}</span>
    </div>
    ${extraMeta ? `<div style="font-size:.78em;margin:4px 0 6px;line-height:1.6">${extraMeta}</div>` : ''}
    ${section}
    <input class="note-input" id="note${id}" placeholder="notes (optional)">
    <div class="row-btns">
      <button class="b-view" onclick="viewSession(${id},'${pad}','${timeStr}')">📊 View</button>
      <button class="b-skip" onclick="skip(${id})">skip</button>
      <button class="b-del"  onclick="deleteSess(${id})">🗑</button>
    </div>
  </div>`;
}

function label(id, act){
  pending_activity[id]=act;
  document.getElementById('beh'+id).style.display='block';
}
function setBeh(id, beh){
  _save(id, pending_activity[id], beh, document.getElementById('note'+id)?.value||'');
}
function labelDirect(id, act){
  _save(id, act, '', document.getElementById('note'+id)?.value||'');
}
function _save(id, activity, behavior, notes){
  fetch('/api/label',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id, activity, behavior, notes})})
    .then(r=>r.json()).then(d=>{
      if(d.ok){ lastPendCount=-1; document.getElementById('s'+id)?.remove(); }
      else alert('Error: '+d.error);
    });
}
function skip(id){
  fetch('/api/skip',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id})}).then(()=>{
      lastPendCount=-1;
      document.getElementById('s'+id)?.remove();
    });
}
function deleteSess(id){
  if(!confirm('ลบ session นี้ออกจาก DB ถาวร?')) return;
  fetch('/api/delete',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id})}).then(r=>r.json()).then(d=>{
      if(d.ok){ lastPendCount=-1; document.getElementById('s'+id)?.remove(); }
      else alert('Error: '+d.error);
    });
}

function viewSession(id, pad, timeStr){
  document.getElementById('viewBanner').textContent=`⏪ [${pad.toUpperCase()}] ${timeStr}`;
  document.getElementById('btnClear').style.display='';
  viewSessId=id;
  fetch(`/api/session_data?id=${id}`).then(r=>r.json()).then(pts=>{
    if(!pts.length){alert('No raw data for this session');return;}
    const isFood=pad==='food', isWater=pad==='water';
    setChart(cW, pts,[p=>p.tot, isFood?p=>p.fl:isWater?p=>p.fl:p=>null]);
    setChart(cC, pts,[p=>p.fl,  p=>p.fr, p=>p.rl, p=>p.rr]);
    setChart(cS, pts,[p=>p.dw,  p=>p.std]);
    setChartSrc(pad);
  });
}

function clearView(){
  viewSessId=null;
  document.getElementById('viewBanner').textContent='';
  document.getElementById('btnClear').style.display='none';
  cW.data.labels=[]; cW.data.datasets.forEach(d=>d.data=[]); cW.update('none');
  cC.data.labels=[]; cC.data.datasets.forEach(d=>d.data=[]); cC.update('none');
  cS.data.labels=[]; cS.data.datasets.forEach(d=>d.data=[]); cS.update('none');
}

async function poll(){
  try{
    const r=await fetch('/api/state',{cache:'no-store'});
    const s=await r.json(); errN=0;

    const ok=s.railway_ok;
    document.getElementById('dotRailway').className='dot'+(ok?' on':'');
    document.getElementById('hdrRailway').textContent=ok?'connected':'disconnected';
    document.getElementById('hdrRailway').style.color=ok?'#4fc3f7':'#ef5350';

    const counts={bow:0,food:0,water:0};
    (s.pending||[]).forEach(p=>{ if(counts[p.padType]!==undefined) counts[p.padType]++; });
    document.getElementById('dotBow').className  ='dot'+(counts.bow  >0?' on':'');
    document.getElementById('dotFood').className ='dot food'+(counts.food >0?' on':'');
    document.getElementById('dotWater').className='dot water'+(counts.water>0?' on':'');

    const pend=s.pending||[];
    if(pend.length!==lastPendCount){ renderPending(pend); lastPendCount=pend.length; }

    const now=new Date();
    document.getElementById('lastFetch').textContent=`${now.getHours()}:${String(now.getMinutes()).padStart(2,'0')}:${String(now.getSeconds()).padStart(2,'0')}`;

    const done=s.labeled||[];
    document.getElementById('doneList').innerHTML=done.slice().reverse().map(l=>
      `<div>✓ <b>[${l.device}]</b> <b>${l.activity}</b>${l.behavior?' ('+l.behavior+')':''} &nbsp;${l.duration_s}s &nbsp;<span style="color:#333">${l.labeled_at}</span></div>`
    ).join('');
  }catch(e){
    errN++;
    if(errN>3){
      document.getElementById('dotRailway').className='dot';
      document.getElementById('hdrRailway').textContent='error';
      document.getElementById('hdrRailway').style.color='#ef5350';
    }
  }
}
poll(); setInterval(poll,2000);
</script>
</body>
</html>"""

# ── Start background poller (runs under both gunicorn and direct python) ──────
print(f"[CONFIG] Railway URL: {RAILWAY_URL}")
threading.Thread(target=_fetch_railway, daemon=True).start()

# ── main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"[WEB] http://localhost:{HTTP_PORT}")
    app.run(host="0.0.0.0", port=HTTP_PORT, debug=False, use_reloader=False)
