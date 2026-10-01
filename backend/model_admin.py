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
import time
import urllib.request
from urllib.parse import parse_qs, urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)  # 让 config 的 env_file=".env" 与启动目录无关（作为服务常驻时更稳）

import config  # noqa: E402
from backend import gateway, hypit_bridge  # noqa: E402

ENV_PATH = os.environ.get("MODEL_ADMIN_ENV", os.path.join(ROOT, ".env"))
HEALTH_URL = os.environ.get("MODEL_ADMIN_HEALTH", "http://127.0.0.1:8090/api/health/models")
TASK_NAME = "WeeksirModelAdmin"
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


# ---- 价目 / 默认路由 / 积分流水（model_prices.json + 本地账本，改价不用动代码）----

def prices_view() -> dict:
    ext = gateway.external_config()
    rows = []
    for (cap, purpose), route in gateway.ROUTES.items():
        rows.append({
            "capability": cap, "purpose": purpose, "provider": route["provider"],
            "price": gateway.unit_price(cap, purpose),
            "builtin": gateway.PRICE_ROUTES.get((cap, purpose)),
            "overridden": f"{cap}/{purpose}" in ext["prices"],
            "is_default": purpose == (ext["defaults"].get(cap) or gateway.DEFAULT_PURPOSE.get(cap)),
        })
    return {"path": gateway.price_path(), "external": bool(ext["prices"]),
            "rows": rows, "defaults": {c: (ext["defaults"].get(c) or gateway.DEFAULT_PURPOSE.get(c))
                                       for c in gateway.CAPABILITIES},
            "purposes": {c: [p for (cc, p) in gateway.ROUTES if cc == c] for c in gateway.CAPABILITIES}}


def _validate_prices(body: dict) -> tuple:
    """返回 (prices, defaults, disabled, errors)。只认注册表里真实存在的 cap/purpose，防手滑写出幽灵路由。"""
    errs, prices = [], {}
    for k, v in (body.get("prices") or {}).items():
        if "/" not in str(k) or tuple(str(k).split("/", 1)) not in gateway.ROUTES:
            errs.append(f"未知路由: {k}")
            continue
        try:
            n = int(v)
        except (TypeError, ValueError):
            errs.append(f"{k} 的价格不是整数: {v!r}")
            continue
        if not 0 <= n <= 100000:
            errs.append(f"{k} 价格超范围(0~100000): {n}")
            continue
        prices[str(k)] = n
    defaults = {}
    valid_default = {c: [p for (cc, p) in gateway.ROUTES if cc == c] for c in gateway.CAPABILITIES}
    for c, p in (body.get("defaults") or {}).items():
        if c not in valid_default or p not in valid_default[c]:
            errs.append(f"默认路由 {c}={p} 不存在（可选: {', '.join(valid_default.get(c, [])) or '未知能力'}）")
            continue
        defaults[str(c)] = str(p)
    disabled = {}
    for item in (body.get("disabled") or []):
        k, reason = (item, "") if isinstance(item, str) else (item.get("key", ""), item.get("reason", ""))
        if "/" not in str(k) or tuple(str(k).split("/", 1)) not in gateway.ROUTES:
            errs.append(f"要屏蔽的路由不存在: {k}")
            continue
        disabled[str(k)] = str(reason or "")[:80]
    return prices, defaults, disabled, errs


def save_prices(body: dict) -> tuple:
    prices, defaults, disabled, errs = _validate_prices(body)
    if errs:
        return 400, {"errors": errs}
    path = gateway.price_path()
    try:
        data = json.loads(open(path, encoding="utf-8").read())
    except Exception:
        data = {"_说明": "统一模型网关价目与默认路由：prices 键=capability/purpose，defaults 键=capability。"}
    data["prices"] = {**data.get("prices", {}), **prices}
    data["defaults"] = {**data.get("defaults", {}), **defaults}
    if "disabled" in body:            # 面板每次提交的是"当前全部被屏蔽的"完整快照 → 覆盖语义
        data["disabled"] = disabled
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, path)   # 原子换；mtime 变 → gateway 下一次调用自动重载
    return 200, {"prices_written": len(prices), "defaults_written": len(defaults),
                 "disabled": disabled, "path": path}


def credits_view(limit: int = 30) -> dict:
    led = gateway.ledger()
    out = led.audit(limit)
    out["accounts"] = led.accounts()
    out["ledger_path"] = led.path
    return out


# ---- 探活缓存 + 管理台看板（页面秒开，状态随点随新；全量测通是后台动作）----
_HEALTH = {"ts": 0.0, "routes": {}, "summary": "还没测过（点右上「全量测通」）"}


def health_all() -> dict:
    out = gateway.test_models(health_url=HEALTH_URL)
    _HEALTH["routes"].update(out["routes"])
    _HEALTH["summary"] = out["summary"]
    _HEALTH["ts"] = time.time()
    return out


def health_one(capability: str, purpose: str) -> tuple:
    try:
        v = gateway.test_route(capability, purpose)
    except gateway.RouteError as e:
        return 400, {"error": str(e)}
    _HEALTH["routes"][f"{capability}/{purpose}"] = v
    _HEALTH["ts"] = time.time()
    return 200, v


def board_view() -> dict:
    """按能力聚合的管理看板：路由/入口/模型/密钥就绪/单价/默认/是否已接适配器 + 上次探活状态。"""
    ext = gateway.external_config()
    s = _fresh()                      # 每次新建 Settings：刚改的 .env 立刻反映在看板上
    cards = []
    for cap in gateway.CAPABILITIES:
        dft = ext["defaults"].get(cap) or gateway.DEFAULT_PURPOSE.get(cap)
        rows = []
        for (c, pur), route in gateway.ROUTES.items():
            if c != cap:
                continue
            f = route["fields"]
            kf, bf, mf = f.get("api_key"), f.get("base_url"), f.get("model")
            rows.append({
                "purpose": pur, "provider": route["provider"],
                "entry": f'{route["client"]}.{route["fn"]}',
                "adapted": gateway.is_adapted(c, pur),
                "price": gateway.unit_price(c, pur),
                "overridden": f"{c}/{pur}" in ext["prices"],
                "is_default": pur == dft,
                "model": ((getattr(s, mf, "") if mf else "") or "(默认)"),
                "base_url": ((getattr(s, bf, "") if bf else "") or "(本地)"),
                "key_env": kf or "(无需)",
                "key_present": bool(getattr(s, kf, "")) if kf else True,
                "disabled": ext["disabled"].get(f"{c}/{pur}"),
                "probe": _HEALTH["routes"].get(f"{c}/{pur}"),
            })
        cards.append({"capability": cap, "default": dft, "rows": rows})
    adapted_n = sum(1 for (c, p) in gateway.ROUTES if gateway.is_adapted(c, p))
    rates = "; ".join(f"{c} {gateway.token_rate(c)}分/1k" for c in gateway.CAPABILITIES if gateway.token_rate(c))
    return {"cards": cards, "prices_path": gateway.price_path(), "prices_external": bool(ext["prices"]), "token_rates": rates,
            "routes_total": len(gateway.ROUTES), "routes_adapted": adapted_n,
            "probe": {"ts": _HEALTH["ts"], "summary": _HEALTH["summary"], "routes": _HEALTH["routes"]},
            "adapter": f"gateway.py 与 model_admin.py 同进程；改这两个文件要重启计划任务 {TASK_NAME}"}


PAGE = r"""<!doctype html><html lang=zh><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Weeksir 模型网关 · 管理台</title>
<style>
:root{--bg:#0e1116;--panel:#151a23;--line:#252c3a;--fg:#e7ecf3;--mut:#8d97a8;--ok:#3fb37f;--bad:#e2564d;--warn:#dfa44b;--acc:#4f83ff;--mute:#3a4353}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,"Microsoft YaHei",sans-serif;padding-bottom:80px}
header{position:sticky;top:0;z-index:9;background:#0e1116ee;border-bottom:1px solid var(--line);padding:12px 18px}
.t{font-size:16px;margin:0;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.tag{font-size:11px;color:var(--mut);border:1px solid var(--line);border-radius:20px;padding:2px 8px;font-weight:400}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:10px}
.kpis{display:flex;gap:8px;flex-wrap:wrap}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:5px 10px;min-width:76px}
.kpi b{display:block;font-size:15px;line-height:1.2}
.kpi span{font-size:11px;color:var(--mut)}
button{background:var(--acc);color:#fff;border:0;border-radius:8px;padding:6px 12px;cursor:pointer;font-size:13px}
button.ghost{background:#222a38;color:#c8d2e2}
button.mini{padding:3px 9px;font-size:12px;background:#222a38;color:#c8d2e2;border:1px solid var(--line)}
button:disabled{opacity:.5;cursor:progress}
main{padding:16px 18px}
#warn{margin:0 0 12px}
.banner{border:1px solid var(--line);border-left:4px solid var(--warn);background:#1a1710;border-radius:8px;padding:8px 12px;font-size:13px;margin-bottom:8px}
.banner.bad{border-left-color:var(--bad);background:#1c1214}
.banner.mut{border-left-color:var(--mute);background:#12161d;color:var(--mut)}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;margin:0 0 14px;overflow:hidden}
.card>h2{font-size:14px;margin:0;padding:10px 12px;background:#191f29;border-bottom:1px solid var(--line);display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.card>h2 .n{color:var(--mut);font-weight:400;font-size:12px}
.lanes{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:10px;padding:12px}
.lane{border:1px solid var(--line);border-radius:10px;padding:9px 10px;background:#111620}
.lane.h{border-color:#33405c}
.lane.off{opacity:.62;border-style:dashed}
.lh{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.lh b{font-size:13px}
.pv{font-size:11px;color:var(--mut)}
.chip{font-size:11px;border-radius:20px;padding:2px 8px;border:1px solid var(--line);color:var(--mut);margin-left:auto;white-space:nowrap}
.chip.ok{color:#9be3c3;border-color:#2c5f47;background:#132520}
.chip.bad{color:#ffc3bd;border-color:#6b2f2b;background:#261415}
.chip.flaky{color:#f3d08a;border-color:#6b5426;background:#241d10}
.chip.warn{color:#f5d9a5;border-color:#6b5426;background:#241d10}
.chip.mute{color:#9aa5b8}
.det{font-size:11px;color:var(--mut);margin-top:5px;word-break:break-all}
.grid{display:grid;grid-template-columns:auto 1fr;gap:3px 8px;margin-top:8px;font-size:12px}
.grid dt{color:var(--mut)}
.grid dd{margin:0;font-family:ui-monospace,Consolas,monospace;font-size:11px;word-break:break-all}
.foot{display:flex;align-items:center;gap:8px;margin-top:8px}
.foot label{display:flex;align-items:center;gap:4px;font-size:12px;color:var(--mut);cursor:pointer}
.foot input[type=number]{width:64px;background:#0c1016;color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:3px 5px}
.foot .sp{margin-left:auto}
.dot{font-size:11px}
.dot.on{color:var(--ok)}.dot.off{color:var(--bad)}
input[type=text],input[type=password]{width:100%;background:#0c1016;color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:5px 6px;font-size:12px}
table{border-collapse:collapse;width:100%;font-size:12px}
th,td{border-bottom:1px solid var(--line);padding:5px 7px;text-align:left;vertical-align:top}
th{color:var(--mut);font-weight:400;background:#171d26}
details{background:var(--panel);border:1px solid var(--line);border-radius:12px;margin:0 0 14px}
details>summary{cursor:pointer;padding:10px 12px;font-size:14px}
details>div{padding:0 12px 12px}
#log{position:fixed;left:0;right:0;bottom:0;background:#0b0e13;border-top:1px solid var(--line);padding:6px 18px;font-size:12px;max-height:74px;overflow:auto;color:var(--mut)}
#log div:first-child{color:var(--fg)}
.mut{color:var(--mut);font-size:12px}
code{color:#9fb3d1}
</style>
<header>
  <p class=t>Weeksir 统一模型网关 · 管理台 <span class=tag>127.0.0.1:8100</span><span class=tag>无鉴权 · 仅运维本机</span><span class=tag id=ptag>价目源 -</span></p>
  <div class=row>
    <div class=kpis id=kpis></div>
    <div style="margin-left:auto;display:flex;gap:8px">
      <button onclick="testAll(this)">全量测通</button>
      <button class=ghost onclick="boot()">刷新</button>
      <button class=ghost onclick="saveAll()">保存价目/默认</button>
    </div>
  </div>
</header>
<main>
  <div id=warn></div>
  <div id=cards></div>
  <details id=keysBox><summary>密钥与端点（写 <code>.env</code> · 只写不回显 · 同一个 key 被多条路由共用时只列一次）</summary><div>
    <div class=row><button onclick="saveKeys(this)">保存密钥</button><span class=mut>改完若要正在跑的 8100 也用上新值，需重启计划任务 <code>WeeksirModelAdmin</code>；skill 走 tool-jobs 每次新进程，立即生效。</span></div>
    <table id=kt><thead><tr><th>环境变量</th><th>类型</th><th>当前</th><th>填新值</th><th>用在</th></tr></thead><tbody></tbody></table>
  </div></details>
  <details open><summary>积分余额与调用审计（本地账本桩 · 3.0 平替 weeksir.com 真账本）</summary><div>
    <div class=row><button class=ghost onclick="loadCredits()">刷新</button><span class=mut>账本 <code id=lpath></code></span></div>
    <div class=lanes>
      <div class=lane><h3 style="font-size:13px;margin:0 0 6px">余额</h3><table id=at><thead><tr><th>member</th><th>余额</th></tr></thead><tbody></tbody></table></div>
      <div class=lane><h3 style="font-size:13px;margin:0 0 6px">按路由聚合</h3><table id=bt><thead><tr><th>路由</th><th>状态</th><th>次</th><th>实扣</th><th>预估</th><th>token</th></tr></thead><tbody></tbody></table></div>
      <div class=lane><h3 style="font-size:13px;margin:0 0 6px">最近调用</h3><table id=rt><thead><tr><th>member</th><th>路由</th><th>预估</th><th>实扣</th><th>token</th><th>状态</th></tr></thead><tbody></tbody></table></div>
    </div>
  </div></details>
</main>
<div id=log></div>
<script>
let BOARD=null, KEYS=null, BAL=0;
const CEL="st__", OKS=["live-ok","reachable"], BADS=["live-fail","auth-fail","unreachable","config-error"];
function chip(r){
  if(!r||!r.status) return '<span class="chip mute">未测</span>';
  const s=r.status, c=BADS.includes(s)?"bad":(OKS.includes(s)?"ok":"warn");
  const ms=r.ms==null?"":(" "+r.ms+"ms");
  return '<span class="chip '+c+'">'+s+ms+'</span>';
}
function esc(t){return String(t==null?"":t).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}
function laneHTML(cap,r){
  const dft=r.is_default, off=r.disabled!=null, key=cap+'/'+r.purpose;
  const st=off?'<span class="chip warn">已屏蔽</span>':chip(r.probe);
  return '<div class="lane'+(dft?' h':'')+(off?' off':'')+'" id="lane__'+cap+'__'+r.purpose+'">'+
   '<div class="lh"><b>'+esc(r.purpose)+'</b><span class="pv">'+esc(r.provider)+' · '+esc(r.model)+'</span>'+
   '<span id="'+CEL+cap+'__'+r.purpose+'">'+st+'</span></div>'+
   '<div class="grid"><dt>密钥</dt><dd><span class="dot '+(r.key_present?'on">就绪':'off">缺')+'</span> '+esc(r.key_env)+
   '</dd><dt>入口</dt><dd>'+esc(r.entry)+'</dd><dt>端点</dt><dd>'+esc(r.base_url)+'</dd>'+
   '<dt>适配器</dt><dd>'+(r.adapted?'<span class="dot on">已接·可直接调</span>':'<span class="dot" style="color:#dfa44b">待接适配器</span>')+(r.overridden?'':' <span class=pv>(价=内置兜底)</span>')+'</dd>'+
   (off?'<dt>屏蔽原因</dt><dd>'+esc(r.disabled||'(未填)')+'</dd>':'')+'</div>'+
   '<div class="det" id="det__'+cap+'__'+r.purpose+'">'+esc(off?'屏蔽中：resolve 会直接拒绝这条路由，不会白跑也不会扣费':(r.probe?r.probe.detail:'点「测」探这条（不提交计费任务）'))+'</div>'+
   '<div class="foot"><label>积分/次 <input type="number" min="0" data-p="'+key+'" value="'+r.price+'"></label>'+
   '<label><input type="radio" name="def_'+cap+'" data-cap="'+cap+'" data-pur="'+r.purpose+'"'+(dft?' checked':'')+'>默认</label>'+
   '<label title="取消勾选=屏蔽这条路由（skill 指定它时网关直接拒绝并说明原因）"><input type="checkbox" data-on="'+key+'"'+(off?'':' checked')+'>启用</label>'+
   '<span class=sp></span><button class=mini onclick="testOne(\''+cap+'\',\''+r.purpose+'\',this)">测</button></div></div>';
}
function renderBoard(){
  document.getElementById('ptag').textContent=(BOARD.prices_external?"价目外置 ":"⚠ 价目回落内置表 ")+String(BOARD.prices_path).split("\\").pop()+(BOARD.token_rates?(" · 按token结算 "+BOARD.token_rates):"");
  const host=document.getElementById('cards');
  host.innerHTML=BOARD.cards.map(c=>{
    const live=c.rows.filter(r=>r.disabled==null);
    const n=c.rows.length, offd=n-live.length;
    const ok=live.filter(r=>r.probe&&OKS.includes(r.probe.status)).length;
    const bad=live.filter(r=>r.probe&&BADS.includes(r.probe.status)).length;
    return '<div class="card"><h2>'+esc(c.capability)+'<span class=n>'+n+' 条路由 · 默认 <b style="color:#c8d2e2">'+esc(c.default)+'</b>'+
      (live.some(r=>r.probe)?' · 通 '+ok+'/'+live.length+(bad?' · <span style="color:#e2564d">坏 '+bad+'</span>':''):'')+
      (offd?' · <span style="color:#dfa44b">屏蔽 '+offd+'</span>':'')+'</span></h2>'+
      '<div class="lanes">'+c.rows.map(r=>laneHTML(c.capability,r)).join('')+'</div></div>';
  }).join('');
  kpis(); warnBar();
}
function kpis(){
  const all=[];BOARD.cards.forEach(c=>c.rows.forEach(r=>all.push(r)));
  const rs=all.filter(r=>r.disabled==null), off=all.length-rs.length;
  const ok=rs.filter(r=>r.probe&&OKS.includes(r.probe.status)).length;
  const bad=rs.filter(r=>r.probe&&BADS.includes(r.probe.status)).length;
  const flaky=rs.filter(r=>r.probe&&["upstream-5xx","slow"].includes(r.probe.status)).length;
  const no=rs.filter(r=>r.probe&&!OKS.includes(r.probe.status)&&!BADS.includes(r.probe.status)&&!["upstream-5xx","slow"].includes(r.probe.status)).length;
  const un=rs.filter(r=>!r.probe).length;
  const slow=rs.filter(r=>r.probe&&r.probe.ms!=null);
  const avg=slow.length?Math.round(slow.reduce((a,b)=>a+b.probe.ms,0)/slow.length)+'ms':'-';
  const ts=BOARD.probe&&BOARD.probe.ts?new Date(BOARD.probe.ts*1000).toLocaleTimeString():'-';
  document.getElementById('kpis').innerHTML=
    '<div class="kpi"><b>'+(BOARD.probe&&BOARD.probe.summary||(ok+'/'+rs.length))+'</b><span>探活结果 · '+ts+'</span></div>'+
    '<div class="kpi"><b style="color:'+(bad?'#e2564d':'#3fb37f')+'">'+bad+'</b><span>要你处理(欠费/未开通/配错)</span></div>'+
    '<div class="kpi"><b style="color:'+(off?'#dfa44b':'#8d97a8')+'">'+off+'</b><span>已屏蔽(不计故障)</span></div>'+
    '<div class="kpi"><b style="color:'+(flaky?'#f3d08a':'#8d97a8')+'">'+flaky+'</b><span>上游抖动(非我方)</span></div>'+
    '<div class="kpi"><b>'+no+'</b><span>探不了(要实跑)</span></div>'+
    '<div class="kpi"><b>'+un+'</b><span>未测</span></div>'+
    '<div class="kpi"><b>'+BOARD.routes_adapted+'/'+BOARD.routes_total+'</b><span>已接适配器</span></div>'+
    '<div class="kpi"><b>'+avg+'</b><span>平均延迟</span></div>'+
    '<div class="kpi"><b>'+BAL+'</b><span>账本余额合计</span></div>';
}
function warnBar(){
  const bad=[],no=[],offd=[],flaky=[];
  BOARD.cards.forEach(c=>c.rows.forEach(r=>{
    if(r.disabled!=null){offd.push(c.capability+'/'+r.purpose);return;}
    if(!r.probe)return;
    const k=c.capability+'/'+r.purpose;
    if(r.probe.status==="upstream-5xx"||r.probe.status==="slow")flaky.push(k+' → '+esc(r.probe.detail||r.probe.status)+(r.probe.ms!=null?(' · 上次 '+r.probe.ms+'ms'):''));
    else if(BADS.includes(r.probe.status))bad.push(k+' → '+r.probe.status+' '+esc(r.probe.detail||''));
    else if(!OKS.includes(r.probe.status))no.push(k);
  }));
  let h='';
  if(bad.length)h+='<div class="banner bad"><b>要你处理 '+bad.length+' 条（欠费 / 未开通 / 模型名或端点错——只有你能修）</b><br>'+bad.join('<br>')+'</div>';
  if(flaky.length)h+='<div class="banner"><b>上游抖动 '+flaky.length+' 条：中转自己回 5xx 或超时，<b>不是我们的 key 或配置坏</b>，先别屏蔽</b><br>'+flaky.join('<br>')+'</div>';
  if(no.length)h+='<div class="banner mut">探不了(非计费探测能判断，只能实跑验)：'+no.map(esc).join('、')+'</div>';
  if(offd.length)h+='<div class="banner mut">已屏蔽(网关会直接拒绝这些路由)：'+offd.map(esc).join('、')+'</div>';
  if(BOARD.prices_external===false)h+='<div class="banner"><b>价目回落内置表</b>：model_prices.json 缺失或损坏，改价请先修文件</div>';
  document.getElementById('warn').innerHTML=h;
}
async function loadBoard(){BOARD=await (await fetch('/api/model/board')).json();renderBoard();}
async function testOne(cap,pur,btn){
  btn.disabled=true;btn.textContent='探…';
  const j=await (await fetch('/api/model/test?capability='+encodeURIComponent(cap)+'&purpose='+encodeURIComponent(pur))).json();
  btn.disabled=false;btn.textContent='测';
  const card=BOARD.cards.find(c=>c.capability===cap), row=card&&card.rows.find(r=>r.purpose===pur);
  if(!row)return log('探活失败：'+JSON.stringify(j));
  row.probe=j.error?{status:"config-error",detail:j.error,ms:null}:j;
  document.getElementById(CEL+cap+'__'+pur).innerHTML=chip(row.probe);
  document.getElementById('det__'+cap+'__'+pur).textContent=row.probe.detail||'';
  kpis();warnBar();
  log(cap+'/'+pur+' → '+row.probe.status+(row.probe.ms!=null?(' '+row.probe.ms+'ms'):''));
}
async function testAll(btn){
  btn.disabled=true;const old=btn.textContent;btn.textContent='并发探活中(最多 20 秒，不烧钱)…';
  try{const j=await (await fetch('/api/model/health')).json();
    BOARD.cards.forEach(c=>c.rows.forEach(r=>{const v=(j.routes||{})[c.capability+'/'+r.purpose];if(v)r.probe=v;}));
    BOARD.probe={ts:Date.now()/1000,summary:j.summary};
    renderBoard();log('全量测通：'+j.summary);
  }catch(e){log('全量测通失败：'+e);}
  btn.textContent=old;btn.disabled=false;
}
async function saveAll(){
  const prices={},defaults={},disabled=[];
  document.querySelectorAll('#cards input[data-p]').forEach(el=>{if(el.value!=='')prices[el.dataset.p]=Number(el.value);});
  document.querySelectorAll('#cards input[type=radio][data-cap]:checked').forEach(el=>{defaults[el.dataset.cap]=el.dataset.pur;});
  document.querySelectorAll('#cards input[type=checkbox][data-on]').forEach(el=>{if(!el.checked)disabled.push({key:el.dataset.on,reason:'管理台屏蔽(上游故障/暂不用)'});});
  const res=await fetch('/api/model/prices',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({prices,defaults,disabled})});
  const j=await res.json();
  if(!res.ok)return log('保存失败：'+((j.errors&&j.errors.length)?j.errors.join(' / '):j.error));
  log('已保存：价目 '+j.prices_written+' 条、默认路由 '+j.defaults_written+' 条、屏蔽 '+Object.keys(j.disabled||{}).length+' 条 → 下一次网关调用即生效（无需重启）');
  await loadBoard();
}
async function loadKeys(){
  const rows=await (await fetch('/api/config')).json();const map={};
  rows.forEach(r=>r.fields.forEach(f=>{const m=map[f.env_key]||(map[f.env_key]={env:f.env_key,kind:f.kind,secret:f.secret,set:f.set,shown:f.shown,uses:[]});
    if(f.kind==='api_key'||f.secret){m.secret=true;m.set=m.set||f.set;m.shown=f.shown||m.shown;} else if(!m.shown&&f.shown){m.shown=f.shown;}
    m.uses.push(r.capability+'/'+r.purpose);}));
  KEYS=Object.values(map);
  document.querySelector('#kt tbody').innerHTML=KEYS.map(k=>'<tr><td><code>'+esc(k.env)+'</code></td><td>'+esc(k.kind)+'</td>'+
    '<td>'+(k.secret?(k.set?'<span class="dot on">已设置</span>':'<span class="dot off">未设置</span>'):'<span class=mut>'+esc(k.shown||'未设置')+'</span>')+'</td>'+
    '<td><input type=text data-k="'+esc(k.env)+'" placeholder="'+(k.secret?'留空=不改；填新值即覆盖':'留空=不改')+'"></td>'+
    '<td class="mut">'+k.uses.length+' 条路由</td></tr>').join('');
}
async function saveKeys(btn){
  const body={};document.querySelectorAll('#kt input[data-k]').forEach(el=>{if(el.value.trim())body[el.dataset.k]=el.value.trim();});
  if(!Object.keys(body).length)return log('没有要写的字段（全部留空=不改）');
  btn.disabled=true;
  const j=await (await fetch('/api/config',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)})).json();
  btn.disabled=false;
  log('已写 '+j.written+' 项 → .env（skill 侧新进程立即生效；本 8100 进程要重启才用新值）');
  await loadKeys();
}
function fill(sel,rows,cols){const tb=document.querySelector(sel+' tbody');tb.innerHTML=rows.map(r=>'<tr>'+r.map(c=>'<td>'+esc(c)+'</td>').join('')+'</tr>').join('')||'<tr><td colspan='+cols+' class=mut>（暂无记录）</td></tr>';}
async function loadCredits(){
  const j=await (await fetch('/api/model/credits?limit=20')).json();
  document.getElementById('lpath').textContent=j.ledger_path;
  fill('#at',j.accounts.map(a=>[a.member,a.balance]),2);
  fill('#bt',j.by_route.map(x=>[x.route,x.status,x.calls,x.credits,x.est,x.tokens||0]),6);
  fill('#rt',j.recent_holds.map(x=>[x.member,x.route,x.est==null?x.amount:x.est,x.amount,x.tokens||0,x.status]),6);
  BAL=j.accounts.reduce((a,b)=>a+b.balance,0)||0;
  if(BOARD)kpis();
}
function log(t){const d=document.createElement('div');d.textContent=new Date().toLocaleTimeString()+'  '+t;
  const l=document.getElementById('log');l.insertBefore(d,l.firstChild);while(l.children.length>30)l.removeChild(l.lastChild);}
async function boot(){await loadBoard();await loadCredits();await loadKeys();log('管理台就绪 · 点任意「测」单条探活，或右上「全量测通」');}
boot();
</script>
"""


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json", cors=False):
        b = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        if cors:   # 只给"本机状态只读口"开：DSH 右栏插件在 dsh-app:// 源里轮询，不带 CORS 就 fetch 不到
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(b)

    def _send_file(self, data, ctype):   # 二进制：不能带 charset，否则 hypit collect 的 content-type 判定会脏
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _hypit_denied(self):
        if hypit_bridge.authorized(self.headers.get("Authorization")):
            return False
        self._send(401, json.dumps({"error": {"code": "UNAUTHORIZED",
                                              "message": "缺/错 MODEL_GATEWAY_TOKEN"}}, ensure_ascii=False))
        return True

    def _read_body(self) -> bytes:
        n = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        u = urlparse(self.path)
        if self.path == "/" or self.path.startswith("/index"):
            self._send(200, PAGE, "text/html")
        elif self.path.startswith("/api/config"):
            self._send(200, json.dumps(config_view(), ensure_ascii=False))
        elif self.path.startswith("/api/test"):
            self._send(200, json.dumps(health_all(), ensure_ascii=False))
        elif u.path == "/api/model/caps":
            self._send(200, json.dumps(model_caps(), ensure_ascii=False))
        elif u.path == "/api/model/resolve":
            code, payload = model_resolve(parse_qs(u.query))
            self._send(code, json.dumps(payload, ensure_ascii=False))
        elif u.path == "/api/model/health":
            self._send(200, json.dumps(health_all(), ensure_ascii=False))
        elif u.path == "/api/model/board":
            self._send(200, json.dumps(board_view(), ensure_ascii=False))
        elif u.path == "/api/model/test":
            q = parse_qs(u.query)
            cap = (q.get("capability") or [""])[0]
            pur = (q.get("purpose") or [""])[0] or None
            if not cap:
                self._send(400, json.dumps({"error": "缺 ?capability="}, ensure_ascii=False))
                return
            code, payload = health_one(cap, pur)
            self._send(code, json.dumps(payload, ensure_ascii=False))
        elif u.path == "/api/model/prices":
            self._send(200, json.dumps(prices_view(), ensure_ascii=False))
        elif u.path == "/api/model/credits":
            q = parse_qs(u.query)
            lim = int((q.get("limit") or ["30"])[0] or 30)
            self._send(200, json.dumps(credits_view(max(1, min(lim, 200))), ensure_ascii=False))
        elif u.path.startswith("/api/hypit/assets/"):
            data, ctype = hypit_bridge.serve_asset(u.path.rsplit("/", 1)[-1])
            self._send_file(data, ctype) if data else self._send(404, json.dumps({"error": {"code": "NOT_FOUND"}}))
        elif u.path.startswith("/api/hypit/uploads/files/"):
            data, ctype = hypit_bridge.serve_upload(u.path.rsplit("/", 1)[-1])
            self._send_file(data, ctype) if data else self._send(404, json.dumps({"error": {"code": "NOT_FOUND"}}))
        elif u.path == "/api/hypit/studio":
            # DSH 右栏 presence：代探 hypit studio(:5179) 是否活着。只读、不要 Bearer、带 ACAO:*。
            # 带 ?session= 时按"谁起的 studio"过滤——studio 没有会话概念，不过滤就会在每个会话都弹页签。
            q = parse_qs(u.query)
            sid = (q.get("session") or [None])[0]
            self._send(200, json.dumps(hypit_bridge.studio_status(sid), ensure_ascii=False), cors=True)
        elif u.path == "/api/hypit/rates":
            if self._hypit_denied():
                return
            self._send(200, json.dumps(hypit_bridge.handle_rates(parse_qs(u.query)), ensure_ascii=False))
        elif u.path.startswith("/api/hypit/videos/"):
            if self._hypit_denied():
                return
            self._send(200, json.dumps(hypit_bridge.handle_get(u.path.rsplit("/", 1)[-1]), ensure_ascii=False))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n) if n else b"{}"
        if u.path == "/api/hypit/uploads":
            if self._hypit_denied():
                return
            media = (self.headers.get("Content-Type") or "application/octet-stream").split(";")[0].strip()
            out = hypit_bridge.handle_upload(raw, media)
            self._send(200, json.dumps({"url": out["url"], "public": out["public"]}, ensure_ascii=False))
        elif u.path == "/api/hypit/videos":
            if self._hypit_denied():
                return
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except Exception as e:
                self._send(400, json.dumps({"error": {"code": "BAD_JSON", "message": str(e)}}, ensure_ascii=False))
                return
            code, payload = hypit_bridge.handle_submit(body if isinstance(body, dict) else {})
            self._send(code, json.dumps(payload, ensure_ascii=False))
        elif u.path == "/api/model/call":
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except Exception as e:
                self._send(400, json.dumps({"error": f"bad json: {e}"}))
                return
            code, payload = model_call(body if isinstance(body, dict) else {})
            self._send(code, json.dumps(payload, ensure_ascii=False))
        elif u.path == "/api/model/prices":
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except Exception as e:
                self._send(400, json.dumps({"error": f"bad json: {e}"}, ensure_ascii=False))
                return
            code, payload = save_prices(body if isinstance(body, dict) else {})
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
