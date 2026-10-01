"""Weeksir 模型网关：管理面板 + 统一调用端点（纯标准库、无 web 框架依赖）。

- 只绑 127.0.0.1；密钥只写不回显（页面/接口只给 key_present）。
- 读写对象是仓库根的 .env（可用环境变量 MODEL_ADMIN_ENV 覆盖，便于自测不碰真 .env）。
- 能力/provider/字段口径全部来自 backend.gateway.ROUTES，单一真相源。
- "测试"= gateway 离线凭证就绪检查；若 8090 在跑，再附带在线 /api/health/models 结果。
- **本机常驻，同时也是统一模型 API 的唯一 HTTP 入口**（原挂 8090 的 model_router 已并入此处）：
    GET  /api/model/caps                        能力 + purposes
    GET  /api/model/resolve?capability=&purpose=  脱敏摘要 + 积分预估
    POST /api/model/call {capability,purpose,account,params} → gateway.invoke（预扣/回滚/信封）
    GET  /api/model/health                      各 route 真探活
  命令行轨不变：`python backend/gateway.py call <cap> [purpose] --account X --json '{...}'`。
  ⚠️ 无鉴权：只给本机自用；若要从客户机/公网调用，先加 token + 挪到服务端（见 docs/ADR-model-gateway.md）。

跑法（默认端口 8100）:  python backend/model_admin.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from urllib.parse import parse_qs, urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)  # 让 config 的 env_file=".env" 与启动目录无关（作为服务常驻时更稳）

import config  # noqa: E402
from backend import gateway  # noqa: E402

ENV_PATH = os.environ.get("MODEL_ADMIN_ENV", os.path.join(ROOT, ".env"))
HEALTH_URL = os.environ.get("MODEL_ADMIN_HEALTH", "http://127.0.0.1:8090/api/health/models")
SECRET = re.compile(r"_(?:API_?KEY|TOKEN|SECRET)(?:_OLD)?$")


def _fresh():
    """每次都新建 Settings() 以反映最新 .env（不用 import 期缓存的单例）。"""
    return config.Settings()


def read_env() -> dict:
    d = {}
    if os.path.exists(ENV_PATH):
        for line in open(ENV_PATH, encoding="utf-8"):
            m = re.match(r"\s*([A-Za-z_][A-Z0-9_]*)\s*=(.*)$", line)
            if m:
                d[m.group(1)] = m.group(2).strip()
    return d


def env_upsert(fields: dict) -> int:
    """把非空字段写入 .env（存在则替换、否则追加）。返回写入条数。空值忽略（避免误清空密钥）。"""
    lines = open(ENV_PATH, encoding="utf-8").read().split("\n") if os.path.exists(ENV_PATH) else []
    done = set()
    for key, val in fields.items():
        val = (val or "").strip()
        if not val or not re.fullmatch(r"[A-Za-z_][A-Z0-9_]*", key):
            continue
        replaced = False
        for i, ln in enumerate(lines):
            if re.match(rf"\s*{re.escape(key)}\s*=", ln):
                lines[i] = f"{key}={val}"
                replaced = True
                break
        if not replaced:
            lines.append(f"{key}={val}")
        done.add(key)
    body = "\n".join(l for l in lines if l != "") + "\n"
    open(ENV_PATH, "w", encoding="utf-8").write(body)
    return len(done)


def config_view() -> list:
    s = _fresh()
    rows = []
    for (cap, purpose), route in gateway.ROUTES.items():
        f = route["fields"]
        row = {"capability": cap, "purpose": purpose, "provider": route["provider"], "fields": []}
        for kind, fname in [("base_url", f.get("base_url")), ("model", f.get("model")), ("api_key", f.get("api_key"))]:
            if not fname:
                continue
            cur = getattr(s, fname, "") or ""
            if SECRET.search(fname):
                row["fields"].append({"kind": kind, "env_key": fname, "secret": True,
                                      "set": bool(cur), "shown": "" if not cur else "••••••••"})
            else:
                row["fields"].append({"kind": kind, "env_key": fname, "secret": False,
                                      "set": bool(cur), "shown": cur})
        rows.append(row)
    return rows


# ---- 统一模型 API（HTTP 契约）：与 CLI `gateway.py call` 同一实现，凭证不出本机 ----

def model_caps() -> dict:
    return {"capabilities": gateway.CAPABILITIES,
            "routes": {f"{c}/{p}": gateway.ROUTES[(c, p)]["provider"] for (c, p) in gateway.ROUTES}}


def model_resolve(query: dict) -> tuple:
    cap = (query.get("capability") or [""])[0]
    purpose = (query.get("purpose") or [""])[0] or None
    if not cap:
        return 400, {"error": "缺 ?capability="}
    try:
        r = gateway.resolve(cap, purpose)
        est = gateway.meter(cap, r["purpose"], None)["credits_estimate"]
    except gateway.RouteError as e:
        return 400, {"error": str(e)}
    return 200, {"route": gateway.safe_summary(r), "credits_estimate": est}


def model_call(body: dict) -> tuple:
    cap = str(body.get("capability") or "")
    if not cap:
        return 400, {"error": "body 缺 capability"}
    try:
        # invoke 的 resolve 段在 try 外，未接/未知 purpose 会抛 RouteError → 归 400
        env = gateway.invoke(cap, body.get("purpose"), str(body.get("account") or "local"),
                             body.get("params") or {})
    except gateway.RouteError as e:
        return 400, {"error": str(e)}
    return 200, env


PAGE = """<!doctype html><html lang=zh><meta charset=utf-8>
<title>Weeksir 模型配置</title>
<style>
 body{font:14px/1.5 system-ui,"Microsoft YaHei",sans-serif;background:#0f1115;color:#e6e6e6;margin:24px}
 h1{font-size:18px} table{border-collapse:collapse;width:100%}
 th,td{border:1px solid #2a2f3a;padding:6px 8px;text-align:left;vertical-align:top}
 th{background:#171b22} .cap{color:#7aa2ff}.p{color:#f0a35e}
 input{width:100%;box-sizing:border-box;background:#0b0d11;color:#e6e6e6;border:1px solid #333;padding:4px}
 button{background:#2b6cff;color:#fff;border:0;border-radius:6px;padding:6px 12px;cursor:pointer;margin:4px 6px 0 0}
 .hint{color:#8a93a6;font-size:12px;margin:8px 0}
 #log{white-space:pre-wrap;background:#0b0d11;border:1px solid #333;padding:8px;margin-top:12px;min-height:60px}
</style>
<h1>Weeksir 模型配置（写 .env，密钥只写不回显；仅本机 127.0.0.1）</h1>
<div class=hint>改完点“保存”，管线每次 job 新进程会自动读新值。要改某密钥就在该行填新值（留空=不动）。</div>
<table id=t><thead><tr><th>能力/purpose</th><th>provider</th><th>字段</th><th>当前</th><th>填新值</th></tr></thead><tbody></tbody></table>
<button onclick="save()">保存</button><button onclick="test()">测试连通</button>
<div id=log></div>
<script>
let DATA=[];
async function load(){const r=await fetch('/api/config');DATA=await r.json();render();}
function render(){const tb=document.querySelector('#t tbody');tb.innerHTML='';
 DATA.forEach((row,i)=>{row.fields.forEach((f,j)=>{const tr=document.createElement('tr');
  const first=(j===0);
  tr.innerHTML=`<td>${first?`<span class=cap>${row.capability}</span>/<span class=p>${row.purpose}</span>`:''}</td>`+
   `<td>${first?row.provider:''}</td><td>${f.kind} <code>${f.env_key}</code></td>`+
   `<td>${f.secret?(f.set?'已设置('+ (f.shown)+')':'未设置'):(f.shown||'未设置')}</td>`+
   `<td><input data-k="${f.env_key}" placeholder="${f.secret?'填新密钥':'填新值'}"></td>`;
  tb.appendChild(tr);});});}
async function save(){const body={};document.querySelectorAll('input[data-k]').forEach(el=>{if(el.value.trim())body[el.dataset.k]=el.value.trim();});
 const r=await fetch('/api/config',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});
 const j=await r.json();log('已保存 '+j.written+' 项 → .env');load();}
async function test(){const r=await fetch('/api/test');const j=await r.json();log(JSON.stringify(j,null,2));}
function log(t){document.getElementById('log').textContent=t;}
load();
</script>"""


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        if self.path == "/" or self.path.startswith("/index"):
            self._send(200, PAGE, "text/html")
        elif self.path.startswith("/api/config"):
            self._send(200, json.dumps(config_view(), ensure_ascii=False))
        elif self.path.startswith("/api/test"):
            out = gateway.test_models(health_url=HEALTH_URL)
            self._send(200, json.dumps(out, ensure_ascii=False))
        elif u.path == "/api/model/caps":
            self._send(200, json.dumps(model_caps(), ensure_ascii=False))
        elif u.path == "/api/model/resolve":
            code, payload = model_resolve(parse_qs(u.query))
            self._send(code, json.dumps(payload, ensure_ascii=False))
        elif u.path == "/api/model/health":
            out = gateway.test_models(health_url=HEALTH_URL)
            self._send(200, json.dumps(out, ensure_ascii=False))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n) if n else b"{}"
        if u.path == "/api/model/call":
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except Exception as e:
                self._send(400, json.dumps({"error": f"bad json: {e}"}))
                return
            code, payload = model_call(body if isinstance(body, dict) else {})
            self._send(code, json.dumps(payload, ensure_ascii=False))
        elif self.path.startswith("/api/config"):
            try:
                fields = json.loads(raw.decode("utf-8") or "{}")
            except Exception as e:
                self._send(400, json.dumps({"error": f"bad json: {e}"}))
                return
            written = env_upsert(fields)
            self._send(200, json.dumps({"written": written}))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def log_message(self, *a):  # 静音访问日志，避免噪声
        pass


def main():
    port = int(os.environ.get("MODEL_ADMIN_PORT", "8100"))
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    print(f"Weeksir model admin on http://127.0.0.1:{port}  (ENV={ENV_PATH})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
