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
    GET  /api/spend/approvals                   人批准的条子有哪几张、各还剩几段（只读）
    POST /api/spend/guard {capability,purpose,account,count,session}
                                                花钱批准闸的 HTTP 入口：只【消耗】批准、永不创建批准；只听环回
  命令行轨不变：`python backend/gateway.py call <cap> [purpose] --account X --json '{...}'`。
  ⚠️ 无鉴权：只给本机自用；若要从客户机/公网调用，先加 token + 挪到服务端（见 docs/ADR-model-gateway.md）。

跑法（默认端口 8100）:  python backend/model_admin.py
"""
from __future__ import annotations

import base64
import hashlib
import io
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
from backend import gateway, hypit_bridge, skill_form_page  # noqa: E402

ENV_PATH = os.environ.get("MODEL_ADMIN_ENV", os.path.join(ROOT, ".env"))
HEALTH_URL = os.environ.get("MODEL_ADMIN_HEALTH", "http://127.0.0.1:8090/api/health/models")
TASK_NAME = "WeeksirModelAdmin"
SECRET = re.compile(r"_(?:API_?KEY|TOKEN|SECRET)(?:_OLD)?$")

# 🔴 `/api/film/file` 的唯一白名单（内联 + 下载都读这两行）。闸 scripts/check-film-download.py
#    以这里为权威源，客户端（super-clone-ui 交付卡）那份【必须 ⊆ 这份】—— 两份各抄一遍，
#    迟早变成"按钮点出 404"。json 只许内联不许下载：它是看板读数据用的，
#    而 productions/<task>/outputs/sr_*/phase_state.json 里存过明文厂商 key。
FILM_INLINE_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                     "webp": "image/webp", "mp4": "video/mp4", "mov": "video/quicktime",
                     "webm": "video/webm", "json": "application/json"}
FILM_DOWNLOADABLE = tuple(k for k in FILM_INLINE_TYPES if k != "json")


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


def media_upload(body: dict) -> tuple:
    """POST /api/media/upload —— 把 productions/ 里的一个素材传到供应商公网口，拿 fileName + download_url。

    🔴 这条口【读本机文件并送到第三方】，判据要比起"读文件"更严：
       只放 productions/ 根内（realpath 判定，不信"字符串里没有 .."）、只放媒体扩展名
       （FILM_DOWNLOADABLE 已经把 json 排除在外 —— phase_state.json 里存过明文厂商 key）、
       体积上限由网关判、响应里永不出现 key。
       不计费、不建预扣 ⇒ 不需要批准条子；代价是任何本机进程都能用它占我们 RH 账号的存储，
       发客户前必须和 /api/hypit/cli 那批一起做只读化 + 鉴权（见 docs/ADR-model-gateway.md）。
    """
    f = str(body.get("file") or "")
    if not f:
        return 400, {"ok": False, "error": "body 缺 file（本地素材的绝对路径）"}
    proots = [os.environ.get("DSH_PROD_ROOT")
              or os.path.join(os.environ.get("DSH_WORKSPACE") or r"C:\dsh", "productions")]
    p = os.path.realpath(f)
    if not any(p.startswith(os.path.realpath(r) + os.sep) for r in proots):
        return 403, {"ok": False, "error": "FORBIDDEN：只允许 productions/ 根内的文件（realpath 判定）"}
    ext = os.path.splitext(p)[1].lower().lstrip(".")
    if ext not in FILM_DOWNLOADABLE:
        return 400, {"ok": False, "error": "只允许这些扩展名: %s" % ", ".join(sorted(FILM_DOWNLOADABLE))}
    if not os.path.isfile(p):
        return 404, {"ok": False, "error": "文件不存在"}
    try:
        out = gateway.upload_media(p,
                                   provider=str(body.get("provider") or "runninghub"),
                                   key_hint=str(body.get("key_hint") or ""),
                                   account=str(body.get("account") or "local"))
    except Exception as e:                                    # noqa: BLE001
        # 网关自己炸了要说清是网关炸，不许伪装成"上游拒绝"
        return 500, {"ok": False, "error": "media_upload 异常 %s: %s" % (type(e).__name__, str(e)[:200])}
    return (200 if out.get("ok") else 502), out


def spend_guard(body: dict) -> tuple:
    """花钱批准闸的 HTTP 入口：给"真正干活的那一端"（现在是 8091 动作迁移工作室）在发起前来问一次。

    形状和 model_call 一样（异常不外抛、路由错归 400），但语义相反的一层要注意：
    这个口【只会消耗批准】，永远没有"创建条子"的端点 —— 那等于让机器自己替用户点头。
    预计扣分由网关按价目算，调用方自带价格一律不理（防 credits=0 放行）。
    """
    cap = str(body.get("capability") or "")
    if not cap:
        return 400, {"ok": False, "error": "body 缺 capability"}
    try:
        out = gateway.guard_spend_from_source(
            cap, str(body.get("purpose") or ""), str(body.get("account") or "local"),
            int(body.get("count") or 1), str(body.get("session") or ""))
    except gateway.RouteError as e:
        return 400, {"ok": False, "error": str(e)}
    except Exception as e:
        return 400, {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:200])}
    return 200, out


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


# ── 花钱批准页（2026-10-08）──────────────────────────────────────────────
# 为什么要有它：人证这一步原来要人在终端里敲命令，产品负责人连着否了三次
#   （"太麻烦了换个方式"、"也不要输入口令了"、"我不是说了不要powershell"、"客户那里懂这个"）。
#   ⇒ 客户和运营都只会点按钮，所以批准必须是【一张看得懂的卡 + 一个按钮】，零命令行、零口令。
# 为什么点它算人证：按钮的点击发生在人眼看到的这张页面上；agent 只能提请（建 pending），
#   它没有鼠标。⚠️ 诚实边界：码会随 /api/spend/pending 回到页面源码里，所以"码"不是防伪手段，
#   防伪手段是【这次点击】；终局形态是把它做成 DSH 内核审批卡（approval/decided 只有 UI 的
#   onClick 能 resolve，且没有应答者时内核 fail-closed）。
APPROVAL_PAGE = r"""<!doctype html><html lang=zh><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>视频生成批准 · WeekSir</title>
<style>
""" + skill_form_page.WS_TOKENS + r"""
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:760px;margin:0 auto;padding:28px 18px 80px}
header{display:flex;align-items:center;gap:14px;padding-bottom:18px;border-bottom:1px solid var(--line);margin-bottom:22px}
header img{height:34px;width:auto;object-fit:contain}
.slogan{color:var(--mut);font-size:13px;border-left:1px solid var(--line);padding-left:14px}
h1{font-size:17px;margin:0 0 4px;font-weight:600}
.sub{color:var(--mut);font-size:13px;margin:0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px;margin:0 0 14px}
.card.busy{opacity:.55}
.route{font-size:16px;font-weight:600}
.route span{color:var(--mut);font-weight:400;font-size:13px}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:14px 0}
.kv .k{color:var(--mut);font-size:12px}
.kv .v{font-size:15px;font-weight:600}
.v.money{color:var(--warn)}
.scope{background:color-mix(in srgb, var(--brand) 9%, transparent);border:1px solid color-mix(in srgb, var(--brand) 32%, transparent);border-radius:10px;padding:10px 12px;color:var(--fg);font-size:13px}
.acts{display:flex;gap:10px;margin-top:16px}
button{flex:1;padding:11px 14px;border-radius:10px;border:1px solid var(--line);background:transparent;color:var(--fg);font-size:14px;cursor:pointer}
button.ok{background:var(--brand);border-color:var(--brand);color:var(--on-brand);font-weight:600}
button.ok:hover{filter:brightness(1.1)}
button.no:hover{border-color:var(--bad);color:var(--bad)}
.done{color:var(--ok);font-size:13px;margin-top:10px}
.err{color:var(--bad);font-size:13px;margin-top:10px}
.empty{text-align:center;color:var(--mut);padding:52px 0}
.empty b{display:block;color:var(--fg);font-size:16px;margin-bottom:6px}
.foot{color:var(--mut);font-size:12px;margin-top:26px;text-align:center}
</style>
<div class=wrap>
<header>
  <img src="/assets/weeksir-logo.png" alt="WeekSir">
  <div><h1>视频生成批准</h1></div>
  <div class=slogan>专注电商AI落地</div>
</header>
<div id=list></div>
<p class=foot>本页只听 127.0.0.1 · 每 3 秒自动刷新 · 15 分钟不点自动作废</p>
</div>
<script>
const E=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
/* 🔴 页面要认得"我是来看哪条会话的"（2026-10-11 现网：agent 没建条子就印了 /approval 链接，
   客户点进来看到的是【别的会话】的卡或一张空页，却没有人告诉他这条会话根本没有可批的东西）。
   ?session= 有 ⇒ 只画这条会话的卡，一条都没有就明说；没有 ⇒ 照旧全列（运维从裸地址进来）。 */
const MYSID=(new URLSearchParams(location.search).get('session')||'').trim();
const tail=s=>String(s||'').replace(/^session-/,'').slice(0,8)||'未知';
function card(p){
  const d=document.createElement('div');d.className='card';d.dataset.id=p.id;
  const mine=!MYSID||(p.session||'')===MYSID;
  const left=Math.max(0,Math.round(p.left_seconds/60));
  d.innerHTML=`<div class=route>${E(p.capability)} / ${E(p.purpose)} <span>· ${E(p.account)}</span>
  <span style="float:right;color:${mine?'var(--mut)':'#c9543e'}">会话 ${E(tail(p.session))}${MYSID?(mine?' · 就是这条':' · 不是这条会话'):''}</span></div>
  <div class=grid>
    <div class=kv><div class=k>要跑几段</div><div class=v>${E(p.count)} 段</div></div>
    <div class=kv><div class=k>最多扣多少分</div><div class="v money">${E(p.credits)} 分</div></div>
    <div class=kv><div class=k>多久内有效</div><div class=v>${left} 分钟</div></div>
  </div>
  <div class=scope>这次要做的：${E(p.scope)||'（没写）'}</div>
  <div class=acts><button class=ok>批准，开始跑</button><button class=no>这次不跑</button></div>
  <div class=msg></div>`;
  if(!mine)d.style.opacity=.5;
  d.querySelector('.ok').onclick=()=>act(d,'/api/spend/approve',p.id,'已批准');
  d.querySelector('.no').onclick=()=>act(d,'/api/spend/deny',p.id,'已拒绝');
  return d;}
async function act(d,url,id,word){
  d.classList.add('busy');d.querySelectorAll('button').forEach(b=>b.disabled=true);
  try{
    const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({pending_id:id})});
    const j=await r.json();
    d.querySelector('.msg').className=j.ok?'done':'err';
    d.querySelector('.msg').textContent=j.ok?word+' ✓ 任务可以开始了':('没生效：'+(j.reason||j.error||'未知'));
    if(!j.ok)d.classList.remove('busy');
  }catch(e){d.classList.remove('busy');d.querySelectorAll('button').forEach(b=>b.disabled=false);
    d.querySelector('.msg').className='err';d.querySelector('.msg').textContent='8100 没回话：'+e;}
}
async function load(){
  let rows=[];
  try{rows=(await (await fetch('/api/spend/pending')).json()).pending||[];}
  catch(e){document.getElementById('list').innerHTML='<div class=card><div class=err>判定口 8100 没回话（不是"没有待批"）：'+E(e)+'</div></div>';return;}
  const box=document.getElementById('list');
  if(MYSID){
    const mine=rows.filter(p=>(p.session||'')===MYSID);
    if(!mine.length){
      if(box.dataset.last==='none-mine'&&box.children.length)return;
      box.innerHTML='<div class=empty><b>这条会话（会话 '+E(tail(MYSID))+'）没有待批条子</b>'
        +'Agent 还没跑 <code>gateway.py approve</code> ⇒ 这里没有任何属于这条会话的可批项。'
        '<br>不要点别的会话的卡：那一下点的是【别的方案】的额度。'
        '<br><span style=color:var(--mut)>让它先建条子（建完会自动弹批准窗），再回来看这一页。</span></div>';
      box.dataset.last='none-mine';return;}
    rows=mine;}
  if(!rows.length){
    if(box.dataset.last==='empty'&&box.children.length)return;   // 空态不重绘，免得闪
    box.innerHTML='<div class=empty><b>现在没有等你批的任务</b>Agent 提请批准时会立刻出现在这里</div>';
    box.dataset.last='empty';return;}
  box.dataset.last='rows';box.innerHTML='';
  rows.forEach(p=>box.appendChild(card(p)));}
load();setInterval(load,3000);
</script>
</body></html>
"""


# ── 技能表单字段表（2026-10-08，docs/ADR-one-shot-inchat-form.md）──────────────────────
# 唯一源 = 工作区 `.dsh/skills/<id>/SKILL.md` 的 frontmatter `form:` 段。
# 这里只做【解析 + 投递】：DSH 前端读不到 .dsh/skills 目录（只有 sessions.list / skills.remote.list，
# 都不含 SKILL.md 正文），所以由 8100 把它读出来给渲染器 —— 但字段表本身不在这里第二份维护。
SKILLS_DIR = os.environ.get("WEEKSIR_SKILLS_DIR") or r"C:\dsh\.dsh\skills"
_FORM_TYPES = ("text", "textarea", "select", "number", "images", "video", "audio", "locked")
_FORM_MEDIA = ("images", "video", "audio")   # 走文件、不走文本的三种（一条表里允许各出现多次）
_FORM_MAX_DEFAULT = {"images": 7, "video": 1, "audio": 1}
# 纯中文文件名洗完是空的，要用兜底词拼名字。🔴 兜底词必须按槽类型给：视频槽兜成 `01-image.mp4`
# 会让人以为传错了文件（2026-10-09 真页面上就这么显示过），音频槽同理不能叫 image。
_FORM_FALLBACK_WORD = {"images": "image", "video": "video", "audio": "audio"}


def _range_spec(raw: str):
    """字段表第 5 列在 number/images/video 上复用来写区间（select 拿它写分号选项）：
       `10-90` ⇒ (10, 90)｜`7` ⇒ (None, 7)｜空 ⇒ (None, None)。
       媒体字段没写就退到 _FORM_MAX_DEFAULT —— 张数上限只能是【字段表里那一处】，
       不在渲染器里再写第二份（两份必然漂，漂了就变成"客户能选第 8 张、服务端拒"）。"""
    s = (raw or "").strip()
    m = re.match(r"^(\d+)\s*-\s*(\d+)$", s)
    if m:
        return int(m.group(1)), int(m.group(2))
    if re.match(r"^\d+$", s):
        return None, int(s)
    return None, None


def parse_skill_form(text: str) -> list:
    """从一段 SKILL.md 里抠出 `form:` 段，解析成字段数组。行格式：
       key|type|label|必填|选项|引擎参数   （第 4 列 yes/no，第 6 列是后端真实入参名）
    刻意不用 yaml：少一个依赖、少一种解析漂移。解析不出来就返回空数组（表单不显示，不影响聊天照跑）。
    """
    lines = (text or "").splitlines()
    try:
        a = lines.index("---")
        fm = lines[a + 1:]
        for i, ln in enumerate(fm):
            if ln.strip() == "---":
                fm = fm[:i]
                break
    except ValueError:
        return []
    try:
        s = next(i for i, ln in enumerate(fm) if ln.startswith("form:"))
    except StopIteration:
        return []
    out = []
    for ln in fm[s + 1:]:
        if ln.strip() and not ln.startswith((" ", "\t")):
            break                        # 缩进块结束
        if "|" not in ln:
            continue
        cols = [c.strip() for c in ln.strip().split("|")]
        if len(cols) < 6 or not cols[0] or cols[1] not in _FORM_TYPES:
            continue
        ftype = cols[1]
        lo, hi = (None, None) if ftype == "select" else _range_spec(cols[4])
        if ftype in _FORM_MEDIA and hi is None:
            hi = _FORM_MAX_DEFAULT[ftype]
        rec = {"key": cols[0], "type": ftype, "label": cols[2],
               "required": cols[3] == "yes",
               "options": [o.strip() for o in cols[4].split(";") if o.strip()]
                          if ftype == "select" else [],
               "param": cols[5], "min": lo, "max": hi}
        if ftype == "locked":
            # 🔒 只读行没有引擎入参可写（它是"给客户看清楚不让他改的东西"），
            # 所以第 5 列在这里当【显示值】用；param 留空并由 submit/必填校验一并跳过。
            rec["value"] = cols[4]
            rec["param"] = ""
        # 第 7 列（可选）= 这个槽自己的体积上限。为什么必须有：引擎各自的上限不一样 ——
        # 反推的 handler 硬拦"视频 >15MB（MiMo 限制）"，而通用上限是 200MB；
        # 没有这一列，表单就会让客户选一个【必被引擎拒】的文件（按钮黄了、提交红了）。
        # 🔴 第 7 列起是【可选尾巴】，每格要么是体积（`15MB`），要么是 `k=v`，顺序无关、不写就没有。
        #    （不各占一个固定位置：位置列加到十几列后没人记得住第几列是什么，老线一改就全漂。）
        #      15MB                这个槽自己的体积上限 —— 引擎各自不同（反推的 handler 硬拦 15MB，
        #                          通用上限是 200MB），不写就会让客户选一个【必被引擎拒】的文件
        #      group=素材          分组标题（同组按出现顺序排；一条都不写=退回按类型自动分组，老线不变）
        #      hint=…              格下面的说明；媒体槽则当槽内占位文案
        #      show=voice_mode=timbre_swap,rewrite_voice   条件显隐（只有列出的值才显示）
        #      mark=选填 · 换人时必给   角标文案（不写=按必填回退成「必填」）
        #      note=…              locked 行 🔒 后面那半句灰字
        for extra in cols[6:]:
            extra = extra.strip()
            if not extra:
                continue
            mb = re.match(r"^(\d+(?:\.\d+)?)\s*([KMG]?B)?$", extra, re.I)
            if mb:
                mult = {"B": 1, "KB": 1024, "MB": 1048576, "GB": 1073741824}[
                    (mb.group(2) or "B").upper()]
                rec["max_bytes"] = int(float(mb.group(1)) * mult)
                continue
            if "=" not in extra:
                continue
            k, v = extra.split("=", 1)
            k, v = k.strip().lower(), v.strip()
            if k == "group" and v:
                rec["group"] = v
            elif k == "hint" and v:
                rec["hint"] = v
            elif k == "mark" and v:
                rec["marker"] = v
            elif k == "note" and v:
                rec["note"] = v
            elif k == "show" and "=" in v:
                fk, fv = v.split("=", 1)
                want = [x.strip() for x in fv.split(",") if x.strip()]
                if fk.strip() and want:
                    rec["show_if"] = {"key": fk.strip(), "values": want}
        out.append(rec)
    return out


def _field_hidden(fd: dict, values: dict) -> bool:
    """这一档现在该不该藏这个字段。判据只认字段表里那一条 `show=键=值1,值2`。

    select 提交的是"value label"整串 ⇒ 取第一个 token 当值比（和 form_submit 收值同一套口径，
    两边算法不一致就会出现"前端藏起来了、后端还说必填没填"那种最难查的坏法）。"""
    cond = fd.get("show_if")
    if not cond:
        return False
    cur = str((values or {}).get(cond["key"]) or "").strip().split(" ")[0]
    return cur not in cond["values"]


def skill_forms_view() -> dict:
    """扫工作区所有 skill，返回带表单字段的那几张。只读、无副作用。"""
    forms = {}
    try:
        for name in sorted(os.listdir(SKILLS_DIR)):
            p = os.path.join(SKILLS_DIR, name, "SKILL.md")
            if not os.path.isfile(p):
                continue
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    fields = parse_skill_form(f.read())
            except OSError:
                continue
            if fields:
                forms[name] = fields
    except OSError as e:
        return {"ok": False, "error": "读不到技能目录 %s：%s" % (SKILLS_DIR, str(e)[:120]), "forms": {}}
    return {"ok": True, "dir": SKILLS_DIR, "forms": forms}


# ── 对话内表单：门控 + 载荷（2026-10-08 用户定的挂点判据）────────────────────────
# 🔴 挂点 = 【明确意图之后】。这个产品的一条总纪律是"新会话一条 UI 都不许出现，只有用户显式点了
#    skill 才挂东西"（工作区 AGENTS.md『右栏 UI 铁律』）。表单同理：信号源就是意图登记簿
#    （hypit_bridge.intent_map()，两个源合并 + TTL 已经在那一份里），不在这里第二份实现。
# 意图档 ≠ 技能目录名：`one-shot-video` 登记的是 product-video 档（只起排他作用），所以映射写在
#    各 SKILL.md 的 frontmatter `intent:` 键上 —— 加了新 skill 不用改这里的代码。
# 🔴 提交【不经过输入框】：用户明确否过"把表单挂在对话框上"（会压住 composer），也否过
#    "脚本往共享输入框里写字"。所以提交只落一份会话级载荷，agent 下一轮读它、按 param 名原样入参。
WORKSPACE_DIR = os.environ.get("DSH_WORKSPACE") or r"C:\dsh"
FORM_PAYLOAD_PATH = os.path.join(WORKSPACE_DIR, ".dsh", "form_payload.json")
FORM_PREFILL_PATH = os.path.join(WORKSPACE_DIR, ".dsh", "form_prefill.json")
FORM_UPLOAD_DIR = os.path.join(WORKSPACE_DIR, ".dsh", "form-uploads")
FORM_MAX_FILES = 7                    # 与 SKILL.md 里"商品图 1~7 张"同口径
FORM_MAX_FILE_BYTES = 8 * 1024 * 1024
FORM_MAX_VIDEO_BYTES = 200 * 1024 * 1024   # 爆款/参考视频：引擎收的是本地路径，不上公网，给到 200MB
FORM_IMAGE_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}
FORM_VIDEO_EXT = {"video/mp4": "mp4", "video/quicktime": "mov", "video/webm": "webm"}
FORM_AUDIO_EXT = {"audio/mpeg": "mp3", "audio/wav": "wav", "audio/x-wav": "wav",
                  "audio/mp4": "m4a", "audio/aac": "aac", "audio/ogg": "ogg", "audio/flac": "flac"}
FORM_MAX_AUDIO_BYTES = 20 * 1024 * 1024     # 音色参考只有 1–15 秒，20MB 远够用；给的是防误传整轨的兜底


def _form_ext_map(ftype: str) -> dict:
    """槽类型 → 允许的 MIME 表。🔴 唯一一处：原来这五个地方各写一遍 `video ? VIDEO : IMAGE` 三元，
    加第三种媒体类型就得改五处，漏一处就是"页面让你选、服务端偷偷按图片收"。"""
    return FORM_VIDEO_EXT if ftype == "video" else (FORM_AUDIO_EXT if ftype == "audio" else FORM_IMAGE_EXT)


def _form_max_bytes(ftype: str) -> int:
    return FORM_MAX_VIDEO_BYTES if ftype == "video" else (
        FORM_MAX_AUDIO_BYTES if ftype == "audio" else FORM_MAX_FILE_BYTES)


def parse_skill_intent(text: str) -> str:
    """frontmatter 里的 `intent: <档>`；没有就返回空串（= 这个 skill 的表单永远不会被挂出来）。"""
    lines = (text or "").splitlines()
    try:
        a = lines.index("---")
    except ValueError:
        return ""
    for ln in lines[a + 1:]:
        if ln.strip() == "---":
            break
        if ln.startswith("intent:"):
            return ln.split(":", 1)[1].strip().strip("'\"")
    return ""


def skill_form_index() -> dict:
    """{意图档: {"skill": 目录名, "fields": [...]}}。唯一源还是各 SKILL.md，这里只是把两张表对起来。"""
    out = {}
    view = skill_forms_view()
    forms = view.get("forms") or {}
    for name, fields in forms.items():
        try:
            with open(os.path.join(SKILLS_DIR, name, "SKILL.md"), encoding="utf-8",
                      errors="replace") as f:
                intent = parse_skill_intent(f.read())
        except OSError:
            intent = ""
        if intent:
            out[intent] = {"skill": name, "fields": fields}
    return out


def _form_payloads() -> dict:
    try:
        with open(FORM_PAYLOAD_PATH, encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _form_payload_live(data: dict, sid: str) -> dict:
    """按意图同一个 TTL 判活（登记过期 ⇒ 填过的参数也不再算数，别拿几天前的表去入参）。"""
    rec = data.get(sid)
    if not isinstance(rec, dict):
        return {}
    if time.time() - float(rec.get("ts") or 0) > hypit_bridge.INTENT_TTL_SEC:
        return {}
    return rec


def _form_prefills() -> dict:
    try:
        with open(FORM_PREFILL_PATH, encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _form_prefill_live(data: dict, sid: str, skill: str) -> dict:
    """回填按【同一条意图 TTL】判活，并且【技能换了就作废】—— 换 skill 再登记时，
    上一条线回填的参数对新线基本是错的（同 AGENTS.md『换产品线 = 再登记一次覆盖』）。"""
    rec = data.get(sid)
    if not isinstance(rec, dict) or rec.get("skill") != skill:
        return {}
    if time.time() - float(rec.get("ts") or 0) > hypit_bridge.INTENT_TTL_SEC:
        return {}
    return rec


def _form_media_exts(ftype: str) -> tuple:
    """这个槽类型允许的扩展名（带点、小写）。派生自唯一源 `_form_ext_map`，不在别处再抄名单。"""
    return tuple("." + str(v).lstrip(".").lower() for v in _form_ext_map(ftype).values())


def _form_slot_filled(f: dict, defaults: dict, media: dict) -> bool:
    """这一格现在算不算【有值】。

    🔴 两份 key 空间不一样，用错就变成"明明回填了却报没填"：
       文字格的值在 `defaults` 里、键是 field 的 **key**；
       素材格的路径在 `media` 里、键是 `_form_prefill_paths` 写的**引擎参数名 param**
       （两个键都认一下，因为 param 缺省就等于 key，而别的写入方可能按 key 落）。
    隐藏格（写了 `show=` 且当前值不命中）不参与：提交时本来就不收它，拿它卡必填会把人永久锁在入口外。
    """
    if _field_hidden(f, defaults):
        return True
    k = str(f.get("key") or "")
    if f.get("type") in _FORM_MEDIA:
        m = media or {}
        return bool(m.get(str(f.get("param") or k)) or m.get(k))
    return bool(str((defaults or {}).get(k) or "").strip())


def form_unfilled_required(fields, defaults, media) -> list:
    """必填格里【到现在还没值】的那些（回填值或回填素材都算有值）。
    判据抽成纯函数 ⇒ 闸直接跑值表，不靠人在场。locked 行天生无入参，不参与。"""
    out = []
    for f in fields or []:
        if f.get("type") == "locked" or not f.get("required"):
            continue
        if not str(f.get("key") or ""):
            continue
        if not _form_slot_filled(f, defaults, media):
            out.append(str(f["key"]))
    return out


def _form_referenced_paths(media) -> set:
    got = set()
    for v in (media or {}).values():
        for one in (v if isinstance(v, (list, tuple)) else [v]):
            got.add(os.path.realpath(str(one)))
    return got


def form_loose_materials(roots, media) -> list:
    """素材根里【已经躺着文件、但这次回填没引用】的那些（诊断用）。

    roots 传这条任务的 `inputs/` **和** `src/`：只扫 inputs/ 抓不到现网那次事故（2026-10-10 ef1ba285
    —— 原片下载后留在 `src/`，`inputs/` 空着，于是表单上"原片"那一格永远空，客户看到的还是
    一张要他自己补素材的表）。
    🔴 不扫 `outputs/` 与 `frames/`：那是派生物。把抽帧图当"客户给的素材"会天天误拦入口。
    """
    got = _form_referenced_paths(media)
    exts = tuple(sorted({e for t in _FORM_MEDIA for e in _form_media_exts(t)}))
    out, seen = [], set()
    for root in (roots or []):
        root = str(root or "").strip()
        if not root or not os.path.isdir(root):
            continue
        for dp, _dn, fn in os.walk(root):
            for f in sorted(fn):
                p = os.path.realpath(os.path.join(dp, f))
                if f.lower().endswith(exts) and p not in got and p not in seen:
                    seen.add(p)
                    out.append(p)
    return out


def form_slot_material_candidates(fields, defaults, media, roots) -> dict:
    """{必填还没值的素材格: [盘上【类型对得上】却没被这一格引用的文件]}。

    按类型配是这条判据不变成"天天误拦"的关键：`product_images` 空着、而 src/ 里只有一条视频 ⇒
    不命中（视频填不进图片槽），不拒。命中了才说明"你手上正好有这一格要的料，却没挂上去"。
    """
    cand = {}
    loose = form_loose_materials(roots, media)
    if not loose:
        return cand
    items = [(os.path.splitext(p)[1].lower(), p) for p in loose]
    for f in fields or []:
        if f.get("type") not in _FORM_MEDIA or not f.get("required"):
            continue
        if not str(f.get("key") or "") or _form_slot_filled(f, defaults, media):
            continue
        exts = set(_form_media_exts(f["type"]))
        hit = [p for (e, p) in items if e in exts]
        if hit:
            cand[str(f["key"])] = hit
    return cand


def form_prefill_gap(fields, defaults, media, roots) -> list:
    """🔴 判据（不是诊断）：这些必填素材格【盘上就有对得上类型的料】却没回填 ⇒ 入口该拒。

    为什么合成留在服务端：runner 和 `intent.py` 都要用同一个结论，谁也不许自己再解析字段表
    （两处各写一遍"哪些格算素材格"就是第二份判据，漂了的形状是"脚本说行、入口说不行"）。
    空表 = 放行 —— 客户手上确实没有的素材留空是对的，不该拦。
    """
    return sorted(form_slot_material_candidates(fields, defaults, media, roots))


def form_gate_view(sid: str) -> dict:
    """GET /api/skills/form?session= —— 一次回齐"该不该挂、挂哪个、字段表、是否已提交"。"""
    sid = str(sid or "").strip()
    if not sid:
        return {"ok": False, "error": "缺 session", "show": False}
    rec = (hypit_bridge.intent_map() or {}).get(sid)
    intent = str((rec or {}).get("skill") or "").strip()
    if not intent:
        return {"ok": True, "show": False, "reason": "no-intent", "session": sid}
    hit = skill_form_index().get(intent)
    if not hit:
        return {"ok": True, "show": False, "reason": "intent-has-no-form",
                "session": sid, "intent": intent}
    payload = _form_payload_live(_form_payloads(), sid)
    same = bool(payload) and payload.get("skill") == hit["skill"]
    pre_raw = _form_prefill_live(_form_prefills(), sid, hit["skill"])
    pre = {} if same else pre_raw      # 交过表 ⇒ 页面不再拿回填覆盖客户自己填的（载荷更权威）
    out = {"ok": True, "show": True, "session": sid, "intent": intent,
           "skill": hit["skill"], "fields": _form_fields_wire(hit["fields"]),
           "params": (payload.get("params") or {}) if same else {},
           "submitted": same,
           "task": str((rec or {}).get("task") or ""),
           # 🔴 素材根只由服务端下发：intent.py 以前自己拼 `.dsh/form-uploads/agent/<param>`，
           #    那是第二份路径规则（而且是跨会话共享的那一份 ⇒ 同名互相覆盖）。
           "task_dir": _form_task_dir(sid),
           "upload_root": _form_sid_dir(sid)}
    if pre:
        # 🔴 回填只进 defaults，绝不进 submitted ⇒ 客户仍需自己点【提交这张表】。
        out["defaults"] = pre.get("defaults") or {}
        out["default_files"] = pre.get("media") or {}
        out["prefill_dropped"] = pre.get("dropped") or []
        # 回填依据：页面上挂在「AI 已填 · 可改」的悬停里。客户要能分清哪些是自己填的、
        # 哪些是 agent 猜的 —— 分不清的话，填错的归因会全落到我们头上（2026-10-09 产品的要求）。
        out["prefill_notes"] = pre.get("notes") or {}
    # 🔴 "这张表 agent 看过没有"要能被机器问到（不靠 agent 自述）。跑过一次 prefill —— 哪怕是
    #    `--set '{}'` —— 就是"我看过了"的声明；没跑过 ⇒ 右栏不弹表，报价也不该给。
    #    2026-10-11 现网 16:30：agent 登记了意图、把默认值写在对话里说"可在提交前改"，
    #    但那张表从没回填过 ⇒ 客户手上没有任何可改的地方，approve 照样建得出条子。
    #    ⚠️ 这里必须读【原始记录】而不是 `pre`：客户一旦交过表，`pre` 会被故意清空（载荷更权威），
    #       拿 `pre` 判"回填过没有"会把【客户自己填完了】的单误判成没回填。
    out["prefilled"] = bool(pre_raw)
    out["prefill_ts"] = float(pre_raw.get("ts") or 0.0)
    # 🔴 回填覆盖率（2026-10-10 现网：agent 只回填了声音档一格，原片明明已经下载却让客户自己再选）：
    #    unfilled_required = 到现在还没值的必填格；loose_materials = 素材根里躺着、却没被引用的文件；
    #    prefill_gap = 两者的【合成判据】（必填素材格缺值 + 根里有闲料）。
    #    runner 与 intent.py 都只读结论，不许自己再解析字段表 ⇒ 判据只在这一份。
    #    🔴 算的是【生效值】：交过表就以载荷为准，否则以回填为准。2026-10-11 现网发现只对着回填算 ⇒
    #       一条已经交齐的会话恒报"必填没填"，那是判据自己算错，会把合格对象判红（比漏判更坏）。
    _def = dict((pre.get("defaults") or {}) if not same else {})
    _med = dict((pre.get("media") or {}) if not same else {})
    if same:
        for k, v in ((payload.get("params") or {}) if same else {}).items():
            fd = next((f for f in hit["fields"] if f["key"] == k or f.get("param") == k), None)
            if v is None or v == "":
                continue
            if fd is not None and fd["type"] in _FORM_MEDIA:
                lst = v if isinstance(v, list) else [v]
                _med[k] = lst
                if fd.get("param"):
                    _med[fd["param"]] = lst
            else:
                _def[k] = v
    # 素材根 = inputs/（回填唯一认的根）+ src/（agent 自己下载/派生的地方）。
    # 加上 src/ 是为了抓"原片已下载却留在 src、表上原片永远空"那一次事故；
    # outputs/、frames/ 故意不扫 —— 派生物当客户素材判会天天误拦。
    _roots = [out["upload_root"]] if out.get("upload_root") else []
    if out.get("task_dir"):
        _roots.append(os.path.join(out["task_dir"], "src"))
    out["unfilled_required"] = form_unfilled_required(hit["fields"], _def, _med)
    out["loose_materials"] = form_loose_materials(_roots, _med)
    out["prefill_gap"] = form_prefill_gap(hit["fields"], _def, _med, _roots)
    return out


def _form_safe_name(name: str, i: int, ext: str, fallback: str = "image", sha8: str = "") -> str:
    """落盘文件名：`序号-sha8前8-ascii名.扩展名`。

    🔴 为什么把 sha8 塞进文件名（2026-10-10 用户认的口径）：这台机器多会话并发，"同名不同内容"
    已经咬过两次（deliver_pack 早就是比内容哈希、不认文件名）。sha8 进名字 = 撞名当场看得见，
    而不是等到交付贴图才发现拿错图。

    只留 ASCII，防止把 `..`/分隔符/奇奇怪怪的字符带进我们写死的目录。

    扩展名要【整段剥掉】再拼回我们认的那一种。以前用的是"删掉最后一个点"，
    于是 "a.mp4" 变成 "amp4" 再拼 ".mp4" ⇒ `01-amp4.mp4`，"p.png" 变 `01-ppng.png`；
    文件名和人对不上，重试与清理都认不出是同一张（图片/视频都中招）。

    纯中文名（"爆款样本.mp4"）洗完是空的 ⇒ 用 fallback 兜底。兜底词要按【槽的类型】给：
    视频槽兜成 `01-image.mp4` 会让人以为传错了文件（2026-10-09 真页面上就这么显示的）。
    """
    keep = re.sub(r"[^A-Za-z0-9._-]", "", str(name or ""))
    keep = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", keep)
    keep = (keep[:40] or fallback).strip("._-") or fallback
    tag = ("%s-" % sha8[:8]) if re.fullmatch(r"[0-9a-f]{6,8}", str(sha8 or "")) else ""
    return "%02d-%s%s.%s" % (i + 1, tag, keep, ext)


def _form_task_dir(sid: str) -> str:
    """这条会话的任务目录（唯一源 = 意图记录里的 `task_dir`，由 scripts/intent.py 登记时建）。

    🔴 为什么素材要落在 productions/<task>/inputs 而不是 .dsh/form-uploads：
      ① 一个任务一个根 ⇒ 两条会话回填同名文件不再互相覆盖（原来 agent/<param> 是全局共享的）；
      ② 素材与 BRIEF/TIMELINE/outputs 同根，人一眼看得清这条片子用过哪些图；
      ③ 守卫的界从"全局一根"收成"每任务一根"⇒ 跨任务读文件反而更不可能。
    拿不到 task_dir（老会话、没登记任务名的线）⇒ 返回空串，调用方退回 .dsh/form-uploads/<hash>。
    """
    try:
        rec = (hypit_bridge.intent_map() or {}).get(str(sid)) or {}
    except Exception:
        return ""
    d = str(rec.get("task_dir") or "").strip()
    if not d:
        return ""
    root = os.path.realpath(WORKSPACE_DIR)
    p = os.path.realpath(d)
    return p if p.startswith(root + os.sep) and os.path.isdir(p) else ""


def _form_sid_dir(sid: str) -> str:
    """这条会话【唯一】的素材根。有任务目录就用 `<task>/inputs`，否则退回会话哈希目录。
    所有写入与所有 realpath 守卫都必须走这里 —— 判点写第二处就会漂成"存进去的读不出来"。"""
    t = _form_task_dir(sid)
    if t:
        return os.path.join(t, "inputs")
    # 会话 id 本身是 uuid 形状，但它是【外部输入】⇒ 不进目录名，只进哈希。
    h = hashlib.sha256(str(sid).encode("utf-8")).hexdigest()[:12]
    return os.path.join(FORM_UPLOAD_DIR, h)


def _form_fields_wire(fields):
    """给门控答复用的字段表：把【服务端真正的允许集】原样下发（accept / max_bytes）。

    🔴 为什么必须下发而不是让前端自己写：前端要判"这一格算不算填完"来决定提交按钮是灰是黄，
    而 MIME 与体积上限的权威判据在 `_form_store_files` 里。前端自己抄一份 ⇒ 一定会漂，
    漂了就变成"按钮是黄的、提交却被服务端拒"（客户看到的是我们的 bug，不是他的错）。
    """
    out = []
    for f in fields:
        d = dict(f)
        if f["type"] in _FORM_MEDIA:
            d["accept"] = sorted(_form_ext_map(f["type"]).keys())
            d["max_bytes"] = f.get("max_bytes") or _form_max_bytes(f["type"])
            d["max"] = f.get("max") or _FORM_MAX_DEFAULT[f["type"]]
        out.append(d)
    return out


def form_media_file(sid, param, name):
    """表单上传目录的【只读】回显口（缩略图/预览用）。

    🔴 参数为什么是 param 而不是字段 key：门控回给前端的 `default_files` 是按【引擎 param】键的
       （那就是最终入参形状），而落盘子目录是按【字段 key】。两头对不上过一次的后果是
       "HTTP 200 但图出不来" —— 最难查的那种坏法。这里做一次映射，两边各留各的语义。
    三道闸：只听环回（路由层）+ realpath 必须落在【这条会话的素材根】内 + 扩展名必须命中该槽允许集。
    """
    root = os.path.realpath(_form_sid_dir(sid))
    intent = str(((hypit_bridge.intent_map() or {}).get(sid) or {}).get("skill") or "").strip()
    fields = ((skill_form_index().get(intent) or {}).get("fields") or [])
    fd = next((f for f in fields if f.get("param") == param), None)
    if fd is None:
        return None, None, "这个 param 不在字段表里"
    exts = _form_ext_map(fd["type"])
    safe = re.sub(r"[^A-Za-z0-9._-]", "", str(name or ""))
    slot = re.sub(r"[^A-Za-z0-9._-]", "", str(fd["key"]))[:24] or "slot"
    p = os.path.realpath(os.path.join(_form_sid_dir(sid), slot, safe))
    if not p.startswith(root + os.sep) or not os.path.isfile(p):
        # 【遗留兜底】2026-10-10 之前 agent 回填复制进的是全局共享的 `.dsh/form-uploads/agent/<param>/`
        # （跨会话同名会互相覆盖，已废）。新规矩：intent.py 复制进 `<本会话素材根>/<字段名>/`，
        # 与客户端上传同一套布局 ⇒ 上面那条主路径直接命中，不需要这一支。留着只为让改规矩之前
        # 回填过的会话还能看图，不迁移历史文件。
        alt = os.path.realpath(os.path.join(FORM_UPLOAD_DIR, "agent", str(param), safe))
        if (alt.startswith(os.path.realpath(FORM_UPLOAD_DIR) + os.sep) and os.path.isfile(alt)):
            p = alt
        else:
            return None, None, "文件不在这条会话的素材目录里（或不在这个槽的目录里）"
    ext = os.path.splitext(p)[1].lstrip(".").lower()
    if ext not in set(exts.values()):
        return None, None, "这个槽不收 .%s" % ext
    if os.path.getsize(p) > (fd.get("max_bytes") or _form_max_bytes(fd["type"])):
        return None, None, "文件超过这个槽的体积上限"
    try:
        with open(p, "rb") as fh:
            by_ext = {v: k for k, v in exts.items()}     # 反查：扩展名 → mime（写成 mime→扩展名那份是入盘用的）
            return fh.read(), by_ext[ext], ""
    except OSError as e:
        return None, None, str(e)[:80]


def _form_store_files(sid, fields, files):
    """把表单的【文件槽】落盘 ⇒ (｛引擎param: 路径或路径数组｝, 错误串)。

    🔴 一张表可以有多个文件槽（super-clone 要"产品图 1-10 + 面部图 + 场景图"三种），所以文件项
       必须带 `field`；只有【单个】槽时允许省略（现渲染器就是省略的）。多槽而没标 field ⇒ 拒，
       不猜放哪一格 —— 猜 = 把客户的脸图当产品图塞进引擎。
    单值还是数组也按字段表判：`video` 且上限 1 ⇒ 字符串（引擎收 `video_path`），其余 ⇒ 数组
       （`product_image_paths` / `video_paths`）。每个槽自己一个子目录，免得两个槽同名互相覆盖。
    """
    media = [f for f in fields if f["type"] in _FORM_MEDIA]
    files = files if isinstance(files, list) else []
    if not files:
        return {}, ""
    if not media:
        return {}, "这个技能的表单没有文件字段"
    buckets = {}
    for it in files:
        it = it if isinstance(it, dict) else {}
        fk = str(it.get("field") or "").strip()
        if not fk and len(media) == 1:
            fk = media[0]["key"]
        fd = next((f for f in media if f["key"] == fk), None)
        if fd is None:
            return {}, ("这张表有多个文件槽，每个文件要写明 field（不猜放哪一格）"
                        if not fk else "文件槽不在字段表里：%s" % fk[:40])
        buckets.setdefault(fd["key"], []).append(it)
    out = {}
    for fk, items in buckets.items():
        fd = next(f for f in media if f["key"] == fk)
        cap = fd.get("max") or _FORM_MAX_DEFAULT[fd["type"]]
        if len(items) > cap:
            return {}, "%s 最多 %d 个" % (fd["label"], cap)
        is_video = fd["type"] == "video"
        ext_map = _form_ext_map(fd["type"])
        cap_bytes = fd.get("max_bytes") or _form_max_bytes(fd["type"])
        slot = os.path.join(_form_sid_dir(sid), re.sub(r"[^A-Za-z0-9._-]", "", fk)[:24] or "slot")
        try:
            os.makedirs(slot, exist_ok=True)
        except OSError as e:
            return {}, "建上传目录失败：%s" % str(e)[:80]
        paths = []
        root_real = os.path.realpath(_form_sid_dir(sid))
        for i, item in enumerate(items):
            fn_in = str(item.get("name") or "")
            # 🔴 两种形状都要收：客户在表单里新选的走 base64；agent 回填过的文件已经在上传目录里，
            #    页面只把 path 原样带回来（让它重编码一次等于把同一张图存两份，还会把"AI 已填"变成"未知来源"）。
            #    path 是外部输入 ⇒ 与 _form_prefill_paths 同一道闸：realpath 必须落在 FORM_UPLOAD_DIR 内。
            if item.get("path") and not item.get("data"):
                p = os.path.realpath(str(item.get("path") or ""))
                allow = set(ext_map.values())
                if not p.startswith(root_real + os.sep) or not os.path.isfile(p):
                    return {}, "%s 第 %d 个文件不在表单上传目录里（不接任意路径）" % (fd["label"], i + 1)
                if os.path.splitext(p)[1].lstrip(".").lower() not in allow:
                    return {}, "%s 第 %d 个的扩展名不被这个槽接受" % (fd["label"], i + 1)
                if os.path.getsize(p) > cap_bytes:
                    return {}, "%s 第 %d 个超过 %dMB" % (fd["label"], i + 1, cap_bytes // 1048576)
                paths.append(p)
                continue
            mime = str(item.get("type") or "").split(";")[0].strip().lower()
            ext = ext_map.get(mime)
            if not ext:
                return {}, "%s：只收 %s，收到 %s" % (
                    fd["label"], "/".join(sorted(set(ext_map.values()))), mime[:30])
            try:
                blob = base64.b64decode(str((item or {}).get("data") or ""), validate=True)
            except Exception:
                return {}, "%s 第 %d 个不是合法 base64" % (fd["label"], i + 1)
            if not blob or len(blob) > cap_bytes:
                # 上限文案按量级给单位：写死 MB 会让 1KB 的槽显示"超过 0MB"（客户看不出下限在哪）
                cap_txt = ("%.1fMB" % (cap_bytes / 1048576.0)) if cap_bytes >= 1048576 \
                    else "%dKB" % max(1, cap_bytes // 1024)
                return {}, "%s 第 %d 个为空或超过 %s" % (fd["label"], i + 1, cap_txt)
            fn = os.path.join(slot, _form_safe_name(str(item.get("name") or ""), i, ext,
                                                    _FORM_FALLBACK_WORD.get(fd["type"], "image"),
                                                    hashlib.sha256(blob).hexdigest()[:8]))
            try:
                with open(fn, "wb") as f:
                    f.write(blob)
            except OSError as e:
                return {}, "存文件失败：%s" % str(e)[:80]
            paths.append(fn)
        out[fd["param"]] = (paths[0] if (is_video and cap == 1) else paths)
    return out, ""


def form_submit(body: dict) -> dict:
    """POST /api/skills/form —— 校验后写会话级载荷。返回的 params 就是引擎入参形状。"""
    body = body if isinstance(body, dict) else {}
    sid = str(body.get("session") or "").strip()
    skill = str(body.get("skill") or "").strip()
    if not sid or not skill:
        return {"ok": False, "error": "缺 session 或 skill"}
    rec = (hypit_bridge.intent_map() or {}).get(sid)
    intent = str((rec or {}).get("skill") or "").strip()
    hit = skill_form_index().get(intent)
    if not hit or hit["skill"] != skill:
        # 🔴 没登记意图（或登记的 skill 不是这个）⇒ 拒。这条就是"先明确意图，才有 UI"的落地。
        return {"ok": False, "error": "no-intent-for-this-skill", "intent": intent}
    fields = hit["fields"]
    values = body.get("values") if isinstance(body.get("values"), dict) else {}
    params, missing = {}, []
    for fd in fields:
        if fd["type"] in _FORM_MEDIA or fd["type"] == "locked":
            continue
        if _field_hidden(fd, values):
            # 条件显隐：这一档不该有这个字段 ⇒ 既不要求、也不写进引擎入参。
            # 规则只有一份（字段表里那个 show=），前端按同一条判，这里不另发明一套。
            continue
        v = str(values.get(fd["key"]) or "").strip()
        if not v:
            if fd["required"]:
                missing.append(fd["label"])
            continue
        if fd["type"] == "select" and fd["options"] and v.split(" ")[0] not in [
                o.split(" ")[0] for o in fd["options"]]:
            return {"ok": False, "error": "选项不在字段表里：%s=%s" % (fd["key"], v[:40])}
        if fd["type"] == "number":
            try:
                n = int(float(v))
            except ValueError:
                return {"ok": False, "error": "要填数字：%s=%s" % (fd["key"], v[:40])}
            if fd["min"] is not None and n < fd["min"] or fd["max"] is not None and n > fd["max"]:
                return {"ok": False, "error": "超出字段表给的区间：%s=%d（允许 %s~%s）" % (
                    fd["key"], n, fd["min"], fd["max"])}
            params[fd["param"]] = n
            continue
        params[fd["param"]] = v.split(" ")[0] if fd["type"] == "select" else v
    if missing:
        return {"ok": False, "error": "必填没填：" + "、".join(missing)}

    stored, ferr = _form_store_files(sid, fields, body.get("files"))
    if ferr:
        return {"ok": False, "error": ferr}
    params.update(stored)
    # 🔴 必填的【文件槽】同样要卡：过去只卡文本必填 ⇒ "爆款视频必填"写在字段表里却没人拦，
    #    客户空提交照样 ok:true，到引擎那侧才 FileNotFoundError —— 报在错的层，客户看不出是没传视频。
    for fd in fields:
        # 🔴 隐藏格必须一起跳过：这条判据前端(isShown)与服务端(_field_hidden)是同一条，前半段文本用了、
        #    后半段媒体漏了 ⇒ super-clone 在"原声保留"下 参考音频 是隐藏格，客户提交却被"必填没交：参考音频"
        #    拒掉（2026-10-10 现网：同一条会话连点 8 次全被这条拒，agent 那边永远 not-submitted、
        #    结论还误判成"8100 没配 super-clone 这一档"）。
        if _field_hidden(fd, values):
            continue
        if fd["type"] in _FORM_MEDIA and fd["required"] and fd["param"] not in params:
            missing.append(fd["label"])
    if missing:
        return {"ok": False, "error": "必填没交：" + "、".join(missing)}
    saved = sum(len(v) if isinstance(v, list) else 1 for v in stored.values())
    # 🔴 提交即挂号【待醒】：客户点【提交这张表】就等于授权开工（2026-10-09 定的口径），
    #    但 agent 只在轮到自己说话时才动 ⇒ 没有这一格，链路就断在"表交了没人看见"（T13 实测抓到的洞）。
    #    叫醒动作在宿主插件里（只有那一侧拿得到 Agent.followup），这里只负责【记着该叫谁】。
    ts = time.time()
    resp = _form_write(FORM_PAYLOAD_PATH, sid, {
        "skill": skill, "intent": intent, "params": params, "ts": ts,
        "task": str(rec.get("task") or ""), "files": saved,
        "wake": {"pending": True, "at": ts}}, "载荷")
    # 🔴 交表即点火（2026-10-12 用户定）：客户点【提交这张表】= 授权开工，那"开工"的第一段
    #    （0 分编译）就不该等 agent 想起来跑。结果原样带回响应 ⇒ 页面上看得见"起没起、为什么没起"。
    #    只起 `--stage compile`：不花钱、不建预扣；跑完由它回来挂 compiled 事件（见 compiled_report）。
    if isinstance(resp, dict) and resp.get("ok"):
        resp["kickoff"] = kickoff_compile(sid, skill, rec)
    return resp


# ── 交表即点火（2026-10-12 用户定：提交表单 = 直接进 skill 管线跑编译，不要"先给 agent 分析再进管线"）──
# 切点【写死在编译】：服务端只允许起 `--stage compile`（0 分、不建预扣、不进执行器）；
# 编译产物出来 ⇒ 挂一条 `compiled` 事件叫醒 agent 去报价、建条子；**花钱那一下仍只有 agent 能发起，
# 且仍要人在批准窗点一次**。理由不是客气：提交后要盯终态、失败要补跑、交付要贴图，那些是 agent 的活；
# 而"程序自己扣钱"一旦开口，范围变化（加段数/换路由）就没人拦了。
# 🔴 这不算"给 8100 长出执行端点"：命令不来自请求体 —— 只有白名单里那条 runner、参数固定四个、
#    路径自己拼、无 shell；外部没有任何办法借它跑别的程序。
KICKOFF_RUNNERS = {"super-clone": os.path.join(".dsh", "skills", "super-clone", "runner", "run.py")}
KICKOFF_STAGE = "compile"          # 🔴 写死。改成别的值 = 让服务端自己花钱，闸 K2 会红
COMPILE_WAKE_PATH = os.path.join(WORKSPACE_DIR, ".dsh", "compile_wake.json")


def _proc_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def kickoff_compile(sid: str, skill: str, rec: dict) -> dict:
    """客户点【提交这张表】那一下起一条只读编译。返回人话状态，进 form_submit 的响应 ⇒ 页面上看得见。"""
    import subprocess
    rel = KICKOFF_RUNNERS.get(skill)
    if not rel:
        return {"started": False, "why": "这条线没登记可点火的 runner ⇒ 仍由 agent 决定怎么跑（不猜命令）"}
    runner = os.path.join(WORKSPACE_DIR, rel)
    tdir = str((rec or {}).get("task_dir") or "").strip()
    if not os.path.isfile(runner):
        return {"started": False, "why": "runner 不在盘上（%s）⇒ 不起" % runner}
    if not tdir or not os.path.isdir(tdir):
        return {"started": False, "why": "这条会话的意图里没有任务目录 ⇒ 不起（不猜往哪写）"}
    pidf = os.path.join(tdir, ".compile.pid")
    try:
        old = (io.open(pidf, encoding="utf-8").read() or "").strip() if os.path.isfile(pidf) else ""
    except OSError:
        old = ""
    if old and _proc_alive(old):
        return {"started": False, "reused": True, "pid": int(old),
                "why": "这条任务已经有一台编译在跑 ⇒ 不起第二台（同名产物互相覆盖）"}
    logp = os.path.join(tdir, "compile.log")
    try:                                  # 🔴 日志打不开不许挡住点火（今天 8100 就是死在这形状上）
        fh = open(logp, "a", encoding="utf-8", errors="replace")
    except OSError:
        fh = subprocess.DEVNULL
        logp = "（日志不可开 ⇒ 丢弃，但照常起）"
    cmd = [sys.executable, runner, "--from-form", "--session", sid, "--stage", KICKOFF_STAGE]
    try:
        p = subprocess.Popen(cmd, cwd=WORKSPACE_DIR, stdout=fh, stderr=fh, stdin=subprocess.DEVNULL,
                             creationflags=(0x00000008 | 0x00000200) if os.name == "nt" else 0)
    except OSError as e:
        return {"started": False, "why": "起进程失败：%s: %s" % (type(e).__name__, str(e)[:120])}
    try:
        with open(pidf, "w", encoding="utf-8") as f:
            f.write(str(p.pid))
    except OSError:
        pass
    return {"started": True, "pid": p.pid, "log": logp, "cmd": " ".join(cmd[-4:])}


def compiled_report(body: dict) -> dict:
    """POST /api/skills/form/compiled —— runner 跑完编译回来挂号（第三份唤醒来源）。
    🔴 只认【服务端自己起过的那条】：`.compile.pid` 在、且 pid 就是刚跑完的那个 ⇒ 才挂。
       否则任何本机进程 POST 一下就能凭空造一条"待醒"，那是把唤醒链变成注入面。"""
    body = body if isinstance(body, dict) else {}
    sid = str(body.get("session") or "").strip()
    pid = body.get("pid")
    rec = (hypit_bridge.intent_map() or {}).get(sid)
    tdir = str((rec or {}).get("task_dir") or "").strip()
    if not sid or not tdir:
        return {"ok": False, "error": "没有这条会话或它没有任务目录 ⇒ 不挂号"}
    pidf = os.path.join(tdir, ".compile.pid")
    if not os.path.isfile(pidf):
        return {"ok": False, "error": "这台编译不是我起的（没有 .compile.pid）⇒ 不挂号"}
    try:
        mine = (io.open(pidf, encoding="utf-8").read() or "").strip()
    except OSError:
        mine = ""
    if pid is not None and str(pid).strip() != mine:
        return {"ok": False, "error": "pid 对不上（挂号的 %s ≠ 我起的 %s）⇒ 拒" % (pid, mine or "无")}
    ts = time.time()
    out = _form_write(COMPILE_WAKE_PATH, sid, {
        "session": sid, "skill": str((rec or {}).get("skill") or ""), "ts": ts,
        "ok": bool(body.get("ok")), "error": str(body.get("error") or "")[:200],
        "task": str((rec or {}).get("task") or ""), "task_dir": tdir,
        "wake": {"pending": True, "at": ts}}, "编译待醒")
    try:
        os.remove(pidf)                   # 跑完了就把"我在跑"这个牌子摘掉，允许改参数重交后再起一台
    except OSError:
        pass
    return out


def compile_wake_queue(data: dict) -> list:
    """纯函数：编译跑完了该叫醒谁。四条规则与 form_wake_queue / approval_wake_queue 同构。"""
    out = []
    now = time.time()
    for sid, rec in (data or {}).items():
        if not isinstance(rec, dict):
            continue
        w = rec.get("wake") or {}
        if not w.get("pending"):
            continue
        at = float(w.get("at") or rec.get("ts") or 0)
        if not at or now - at > hypit_bridge.INTENT_TTL_SEC:
            continue
        out.append({"kind": "compiled", "session": sid, "at": at, "ok": bool(rec.get("ok")),
                    "error": str(rec.get("error") or ""), "skill": str(rec.get("skill") or ""),
                    "task": str(rec.get("task") or "")})
    return out


def form_wake_queue(data: dict) -> list:
    """纯函数：从载荷里算出【该叫醒哪几条会话】。判据只在这里一份，插件不自己判。

    四条规则（每条都有对应断言）：
      1. 只有【真提交过】的记录算数 —— agent 回填写的是 defaults，不进这份表（prefill ≠ submitted）；
      2. 过期（同 intent TTL）不算 —— 几天前填的表不该今天把人叫醒；
      3. `wake.pending` 为假 ⇒ 不叫（叫过一次就别每 5 秒重叫 = 无限烧 token）；
      4. 重新提交带新的 `at` ⇒ 又算一次待醒（客户改了参数重交，agent 必须再动一次）。
    """
    out = []
    now = time.time()
    for sid, rec in (data or {}).items():
        if not isinstance(rec, dict):
            continue
        w = rec.get("wake")
        if not isinstance(w, dict) or not w.get("pending"):
            continue
        ts = float(rec.get("ts") or 0)
        if not ts or now - ts > hypit_bridge.INTENT_TTL_SEC:
            continue
        out.append({"kind": "form", "session": str(sid), "skill": str(rec.get("skill") or ""),
                    "task": str(rec.get("task") or ""), "at": float(w.get("at") or ts)})
    return sorted(out, key=lambda x: x["at"])


def _is_loopback(host) -> bool:
    """环回判据【只这一份】：/api/spend/guard、/api/media/upload、表单写口、唤醒两个口都读它。
    原来这五个地方各抄一遍元组 —— 抄第 N 遍的代价是"哪天要认 ::ffff:127.0.0.1 得改五处，
    漏一处就是往安全口上漏"。"""
    s = str(host or "").strip()
    return s in ("127.0.0.1", "::1", "localhost") or s.startswith("::ffff:127.0.0.1")


def wake_view() -> dict:
    """GET /api/skills/form/wake：宿主插件每 5 秒问一次。只读、不改状态。

    两份来源合并成【一条队列】（表单提交 + 批准核销），按 at 排序 —— 判据不许分两处问：
    插件问两次就有"两次之间状态变了"的缝，也迟早有人只接一半。
    ⚠️ URL 里那个 "form" 是历史名：改路径要和宿主插件【同时】重启，中间窗口里整条唤醒链会静默停摆，
       风险大于名字带来的好处，所以留着，函数名与判据都按真实形状叫。
    """
    items = (form_wake_queue(_form_payloads()) + approval_wake_queue(_approval_wake_load())
             + compile_wake_queue(_compile_wake_load()))
    return {"ok": True, "pending": sorted(items, key=lambda x: x["at"])}


def wake_ack(body: dict) -> dict:
    """POST /api/skills/form/wake/ack —— 按 kind 分流到三份队列。

    🔴 kind 必填（form|approval|compiled），不猜：猜错的那一下会去清另一份队列，
       而那边的 `at` 恰好也可能对得上（同一秒内提交/批准/编译完），错清 = 那一格永远没人叫。
    """
    kind = str((body or {}).get("kind") or "")
    if kind == "approval":
        return approval_wake_ack(body)
    if kind == "form":
        return form_wake_ack(body)
    if kind == "compiled":
        return compile_wake_ack(body)
    return {"ok": False, "error": "ack 必须带 kind=form|approval|compiled（不猜是哪份队列）"}


def form_wake_ack(body: dict) -> dict:
    """POST /api/skills/form/wake/ack {session, at}：把这一条标成"已叫醒"。

    🔴 必须回带我们发出去的那个 `at` 才清 —— 不带或对不上就不清。
    否则"插件正去叫醒旧提交"的时候客户又交了一次新的，旧 ack 会把新那格一起抹掉
    （= 客户重交了表却永远没人叫）。这是这条链路唯一安全的幂等写法。
    """
    sid = str((body or {}).get("session") or "")
    at = (body or {}).get("at")
    data = _form_payloads()
    rec = data.get(sid)
    if not sid or not isinstance(rec, dict):
        return {"ok": False, "error": "没有这条会话的载荷：%s" % (sid or "(空)")}
    w = rec.get("wake") or {}
    try:
        same = bool(at) and abs(float(w.get("at") or 0) - float(at)) < 1e-6
    except (TypeError, ValueError):
        same = False
    if not same:
        return {"ok": False, "error": "ack 的 at 与待发队列不符（不清队列，避免抹掉更新的提交）",
                "pending": bool(w.get("pending"))}
    if not w.get("pending"):
        return {"ok": True, "already": True}
    rec["wake"] = dict(w, pending=False, acked_at=time.time())
    return _form_write(FORM_PAYLOAD_PATH, sid, rec, "唤醒回执")


# ==== 批准核销也要叫醒同一条链（2026-10-10 用户："我点了审批不执行，还得回一句点了条子，这个逻辑有问题"）====
# 形状与表单那条【逐条同构】：人点一下 = 一个必然发生的事件 ⇒ 当场挂号待醒，宿主插件领走开新一轮。
# 差别只在"被叫醒之后该干什么"：表单那条是"开始做"，批准这条是"这一刀已经批了，继续跑"。
# 🔴 队列与【批准簿分开存】：.gateway_spend_approvals.json 是钱的账，往里塞 UI 状态 = 两个写者抢同一份文件
#    （当年"旧 ack 抹掉客户重交的新一次"就是这个形状，判据只能有一份、状态只能有一个主人）。
APPROVAL_WAKE_PATH = os.path.join(WORKSPACE_DIR, ".dsh", "approval_wake.json")


def _approval_wake_load() -> dict:
    try:
        with open(APPROVAL_WAKE_PATH, encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def approval_wake_note(grant: dict) -> dict:
    """人在批准窗/批准页点上【批准，开始跑】⇒ 给那条会话挂一格待醒（按条子 id 为键，一条一格）。

    🔴 只在核销成功之后调用：被拒的点击不挂号（拒了就没有任何该动的活），
       条子缺 id 或缺会话也不挂号 —— 猜一个会话去叫醒 = 替别人花批准额度（2026-10-06 那颗雷）。
    """
    g = grant or {}
    gid, sid = str(g.get("id") or ""), str(g.get("session") or "")
    if not gid or not sid:
        return {"ok": False, "error": "条子没有 id 或会话 ⇒ 不挂号（不猜该叫谁）"}
    ts = time.time()
    rec = {"grant_id": gid, "session": sid, "capability": str(g.get("capability") or ""),
           "purpose": str(g.get("purpose") or ""), "account": str(g.get("account") or ""),
           "count": g.get("count"), "credits": g.get("credits"),
           "scope": str(g.get("scope") or ""), "expires": g.get("expires"), "ts": ts,
           "wake": {"pending": True, "at": ts}}
    return _form_write(APPROVAL_WAKE_PATH, gid, rec, "批准待醒")


def approval_wake_queue(data: dict) -> list:
    """纯函数：从核销留痕里算出【该叫醒哪几条会话】。四条规则与 form_wake_queue 同构。

    1. 只有【人真点过】的条子算数（pending 建出来不算 —— 那还没批）；
    2. 过期不算（同 intent TTL，几天前批的条子不该今天把人叫醒）；
    3. `wake.pending` 为假 ⇒ 不叫（叫过一次就别每 5 秒重叫 = 无限烧 token）；
    4. 同一会话再批一条 ⇒ 新的一格、再叫一次（不同条子互不覆盖，所以按 grant_id 存而不是按会话存）。
    """
    out = []
    now = time.time()
    for gid, rec in (data or {}).items():
        if not isinstance(rec, dict):
            continue
        w = rec.get("wake")
        if not isinstance(w, dict) or not w.get("pending"):
            continue
        ts = float(rec.get("ts") or 0)
        if not ts or now - ts > hypit_bridge.INTENT_TTL_SEC:
            continue
        out.append({"kind": "approval", "session": str(rec.get("session") or ""),
                    "grant_id": str(gid), "at": float(w.get("at") or ts),
                    "capability": str(rec.get("capability") or ""),
                    "purpose": str(rec.get("purpose") or ""),
                    "count": rec.get("count"), "credits": rec.get("credits"),
                    "scope": str(rec.get("scope") or "")})
    return sorted(out, key=lambda x: x["at"])


def approval_wake_ack(body: dict) -> dict:
    """POST /api/skills/form/wake/ack {kind:"approval", grant_id, at} ⇒ 销这一格。

    🔴 与表单那份同一条幂等规矩：必须回带服务端发出去的 `at` 才对得上，
       否则"插件正去叫旧那条"的时候人又批了一条新的，旧 ack 会把新那格抹掉。
    """
    gid = str((body or {}).get("grant_id") or "")
    at = (body or {}).get("at")
    data = _approval_wake_load()
    rec = data.get(gid)
    if not gid or not isinstance(rec, dict):
        return {"ok": False, "error": "没有这条条子的待醒记录：%s" % (gid or "(空)")}
    w = rec.get("wake") or {}
    try:
        same = bool(at) and abs(float(w.get("at") or 0) - float(at)) < 1e-6
    except (TypeError, ValueError):
        same = False
    if not same:
        return {"ok": False, "error": "ack 的 at 与待发队列不符（不清队列，避免抹掉更新的批准）",
                "pending": bool(w.get("pending"))}
    if not w.get("pending"):
        return {"ok": True, "already": True}
    rec["wake"] = dict(w, pending=False, acked_at=time.time())
    return _form_write(APPROVAL_WAKE_PATH, gid, rec, "批准唤醒回执")


def _compile_wake_load() -> dict:
    try:
        with open(COMPILE_WAKE_PATH, encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def compile_wake_ack(body: dict) -> dict:
    """POST /api/skills/form/wake/ack {kind:"compiled", session, at} ⇒ 销这一格。
    与前两份同一条幂等规矩：必须回带服务端发出去的那个 `at`，否则不清（别抹掉更新的一次编译）。"""
    sid = str((body or {}).get("session") or "")
    at = (body or {}).get("at")
    data = _compile_wake_load()
    rec = data.get(sid)
    if not sid or not isinstance(rec, dict):
        return {"ok": False, "error": "没有这条会话的编译待醒：%s" % (sid or "(空)")}
    w = rec.get("wake") or {}
    try:
        same = bool(at) and abs(float(w.get("at") or 0) - float(at)) < 1e-6
    except (TypeError, ValueError):
        same = False
    if not same:
        return {"ok": False, "error": "ack 的 at 与待发队列不符（不清队列，避免抹掉更新的一次编译）",
                "pending": bool(w.get("pending"))}
    if not w.get("pending"):
        return {"ok": True, "already": True}
    rec["wake"] = dict(w, pending=False, acked_at=time.time())
    return _form_write(COMPILE_WAKE_PATH, sid, rec, "编译唤醒回执")


def _form_write(path, sid, rec, what):
    """两份表单文件（提交载荷 / agent 回填）共用同一次【读-改-写】：按会话为键、tmp+原子替换。
    写失败必须明说 —— 静默丢过一次（WinError5 撞占用），丢的那次表现为"客户填了表 agent 却说没收到"。"""
    data = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data[sid] = rec
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        if not hypit_bridge.atomic_replace(tmp, path):
            return {"ok": False, "error": "写%s失败（文件被占用，重试已用完）" % what}
    except OSError as e:
        return {"ok": False, "error": "写%s失败：%s" % (what, str(e)[:80])}
    return {"ok": True, "skill": rec.get("skill"), "params": rec.get("params"),
            "count": rec.get("files", 0)}


def form_prefill(body: dict) -> dict:
    """agent 回填：把分析出来的参数【预填】进这张表，等客户自己点提交。

    🔴 **prefill ≠ submitted**：这里只写 `form_prefill.json`（回显成 defaults），
       载荷文件 `form_payload.json` 一个字都不碰 ⇒ `form_gate_view` 的 `submitted` 仍为 false。
       否则就等于 agent 替客户交了表 —— 和 2026-10-08 定的"表单不代发/不代点头"是同一条线。
    🔴 判据（必填、选项归一、param 名、未知 key 丢弃）仍只走 `form_submit` 那一份：这里先按同一
       份字段表洗一遍 key，但【不判必填】—— 回填本来就是半成品，拿必填去卡客户还没填的表是错的。
    文件项收 `path`（agent 已经把聊天附件复制进表单上传目录），路径必须 realpath 落在那个目录里。
    """
    body = body if isinstance(body, dict) else {}
    sid = str(body.get("session") or "").strip()
    skill = str(body.get("skill") or "").strip()
    if not sid or not skill:
        return {"ok": False, "error": "缺 session 或 skill"}
    rec = (hypit_bridge.intent_map() or {}).get(sid)
    intent = str((rec or {}).get("skill") or "").strip()
    hit = skill_form_index().get(intent)
    if not hit or hit["skill"] != skill:
        return {"ok": False, "error": "no-intent-for-this-skill", "intent": intent}
    fields = hit["fields"]
    values = body.get("values") if isinstance(body.get("values"), dict) else {}
    defaults, dropped = {}, []
    for fd in fields:
        if fd["type"] in _FORM_MEDIA:
            continue
        v = str(values.get(fd["key"]) or "").strip()
        if not v:
            continue
        if fd["type"] == "select" and fd["options"] and v.split(" ")[0] not in [
                o.split(" ")[0] for o in fd["options"]]:
            dropped.append(fd["key"])        # 选项不在字段表里 ⇒ 丢，不臆造一个引擎入参
            continue
        defaults[fd["key"]] = v
    unknown = [k for k in values if k not in {f["key"] for f in fields}]
    # 回填【依据】：agent 要能说清"这个值是凭什么填的"，页面把它挂在「AI 已填 · 可改」的悬停上。
    # 只留字段表里认得的 key，长度掐死 —— 这是给客户看的说明文字，不是 agent 的自由输出区。
    raw_notes = body.get("notes") if isinstance(body.get("notes"), dict) else {}
    notes = {k: str(v)[:120] for k, v in raw_notes.items() if k in {f["key"] for f in fields} and v}
    media, merr = {}, ""
    files = body.get("files") if isinstance(body.get("files"), list) else []
    if files:
        media, merr = _form_prefill_paths(sid, fields, files)
        if merr:
            return {"ok": False, "error": merr}
    return _form_write(FORM_PREFILL_PATH, sid, {
        "skill": skill, "intent": intent, "defaults": defaults, "media": media,
        "ts": time.time(), "dropped": dropped + unknown, "notes": notes}, "回填")


def form_route_dispatch(body):
    """POST /api/skills/form 的分发：**一次请求只写一份**，stage 决定写哪一份。

    🔴 这条函数为什么单独存在（2026-10-09 实测抓出的缺陷）：路由原来写成
    "先无条件 form_submit，再按 stage 补一次 form_prefill" ⇒ 一发回填会把【载荷】也写了，
    门控当场回 submitted=True —— 那就是 agent 替客户交了表，正是 prefill≠submitted 这条红线。
    闸当时全绿没抓到，因为它测的是"直接调函数"，分发顺序只在路由层暴露（源码顺序断言是弱证据）。
    所以分发抽成这一个纯函数，闸直接对它跑值表（哪一档调了谁、调了几次）。
    """
    body = body if isinstance(body, dict) else {}
    stage = str(body.get("stage") or "").strip().lower()
    if stage == "prefill":       # 大小写/空格不参与判档
        return form_prefill(body)
    if stage == "attempt":       # 客户点了提交但没成交 ⇒ 只记台账，不碰载荷（见 form_attempt）
        return form_attempt(body)
    return form_submit(body)


FORM_ATTEMPT_PATH = os.path.join(WORKSPACE_DIR, ".dsh", "form_attempts.json")
FORM_ATTEMPT_KEEP = 20           # 每条会话留最近 20 次点击

def _form_attempts():
    try:
        with open(FORM_ATTEMPT_PATH, encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}

def form_attempt(body):
    """记下【客户点了提交、但没走到"已提交"】的每一次：缺哪几格、当时页头是哪条会话哪个任务、服务端拒因。

    🔴 为什么必须有（2026-10-10）：客户报"点了提交没反应"，盘上唯一能查的载荷文件里那条会话一行都没有
       ⇒ 分不清是"点的是别的会话的表"、"这张表还差一格被前端拦下"、还是"fetch 根本没发出去"。
       没有现场，就只能靠猜 —— 而这次连 agent 都猜错了（它拿一条 18 小时前、别的会话的旧载荷
       当"你提交到了别的会话"的证据）。这条台账把"该发生却没发生"变成查得到。
    🔴 它【只增台账】：不写 form_payload.json、不写 form_prefill.json，也不参与 submitted / wake 任何判据
       —— 否则"点一下"就能伪造出"客户已授权开工"，那正是 prefill≠submitted 那条红线要防的形状。
    """
    body = body if isinstance(body, dict) else {}
    sid = str(body.get("session") or "").strip()[:64]
    if not sid:
        return {"ok": False, "error": "缺 session"}
    items = list((_form_attempts().get(sid) or {}).get("items") or [])
    items.append({"ts": round(time.time(), 3),
                  "skill": str(body.get("skill") or "")[:32],
                  "task": str(body.get("task") or "")[:48],
                  "missing": [str(x)[:24] for x in (body.get("missing") or [])][:12],
                  "reason": str(body.get("reason") or "")[:160]})
    rec = {"skill": str(body.get("skill") or "")[:32], "ts": items[-1]["ts"], "items": items[-FORM_ATTEMPT_KEEP:]}
    out = _form_write(FORM_ATTEMPT_PATH, sid, rec, "提交尝试台账")
    return {"ok": bool(out.get("ok")), "count": len(rec["items"])}

def form_attempts_view(n: int = 10) -> dict:
    """GET /api/skills/form/attempts —— 最近 n 次点击（跨会话合并按时间倒序）。只读。"""
    rows = []
    for sid, rec in _form_attempts().items():
        for it in (rec or {}).get("items") or []:
            r = dict(it)
            r["session"] = sid
            rows.append(r)
    rows.sort(key=lambda x: -float(x.get("ts") or 0))
    return {"ok": True, "path": FORM_ATTEMPT_PATH, "recent": rows[:max(1, int(n or 10))]}


def _form_prefill_paths(sid, fields, files):
    """回填的文件只认【已经在表单上传目录里】的路径：agent 要做的是先把聊天附件复制进去，
    而不是让这条口变成任意文件读取器（`path` 是外部输入 ⇒ realpath 必须落在 FORM_UPLOAD_DIR 内，
    扩展名也必须命中这个槽的允许集）。"""
    media = [f for f in fields if f["type"] in _FORM_MEDIA]
    if not media:
        return {}, "这个技能的表单没有文件字段"
    root = os.path.realpath(_form_sid_dir(sid))
    buckets = {}
    for it in files:
        it = it if isinstance(it, dict) else {}
        fk = str(it.get("field") or "").strip()
        if not fk and len(media) == 1:
            fk = media[0]["key"]
        fd = next((f for f in media if f["key"] == fk), None)
        if fd is None:
            return {}, ("这张表有多个文件槽，每个回填项要写明 field"
                        if not fk else "文件槽不在字段表里：%s" % fk[:40])
        p = os.path.realpath(str(it.get("path") or ""))
        if not p.startswith(root + os.sep) or not os.path.isfile(p):
            return {}, "回填的文件不在表单上传目录里：%s" % os.path.basename(p)[:48]
        ext = os.path.splitext(p)[1].lstrip(".").lower()
        allow = set(_form_ext_map(fd["type"]).values())
        if ext not in allow:
            return {}, "%s：不收 .%s（这个槽收 %s）" % (fd["label"], ext, "/".join(sorted(allow)))
        buckets.setdefault(fk, []).append(p)
    out = {}
    for fk, paths in buckets.items():
        fd = next(f for f in media if f["key"] == fk)
        cap = fd.get("max") or _FORM_MAX_DEFAULT[fd["type"]]
        if len(paths) > cap:
            return {}, "%s 最多 %d 个（回填了 %d 个）" % (fd["label"], cap, len(paths))
        # 🔴 一律回数组，单槽也不拍平成字符串：这份 media 唯一的消费者是页面
        #    （gate view 的 default_files），而页面里 `if (!Array.isArray(l)) delete …`
        #    会把非数组【整格删掉】⇒ 原片明明回填成功了，界面上却永远是空的（2026-10-10 实测）。
        #    引擎要的单条路径由提交侧自己取，不在这里提前变形。
        out[fd["param"]] = paths
    return out, ""


def form_presence_view(sid):
    """右栏"填参数"那一档的 presence（判定并成一个口的老规矩：新增一档就在 presence 里回，别开第二个口）。

    🔴 三条判据：
    1. `intent` = 这条会话登记的意图档【配了字段表】。没配表的线（omni/hypit 已冻结、纯提示词类）
       这一档恒 false ⇒ 不会凭空多出一个空台。
    2. `up` 恒 True：这一档画的是 8100 自己托管的表单页，不是引擎 ⇒ "引擎死了"不该把输入口一起藏掉
       （工作区 AGENTS.md：存活只决定台里显示什么）。
    3. 台里【不含价格数字】，点头仍只有 /approval 那一次点击。
    """
    rec = (hypit_bridge.intent_map() or {}).get(str(sid or "")) or {}
    intent = str(rec.get("skill") or "").strip()
    hit = skill_form_index().get(intent)
    if not intent:
        return {"intent": False, "up": True, "alive": True, "reason": "no-intent"}
    if not hit:
        # 🔴 intent 必须是布尔：判定口的契约里 intent 决定页签弹不弹。
        #    上一版这里写成 {"intent": False, ..., "intent": intent}（重复 key，Python 留后者）
        #    ⇒ 回了字符串 "omni"，JS 里恒 truthy ⇒ 【没配表的线也会弹一张空台】。
        return {"intent": False, "up": True, "alive": True, "reason": "intent-has-no-form",
                "intent_skill": intent}
    live = _form_payload_live(_form_payloads(), str(sid or "")) or {}
    pre = _form_prefill_live(_form_prefills(), str(sid or ""), hit["skill"]) or {}
    submitted = bool(live.get("params"))
    # 🔴 2026-10-11 用户改判：弹表条件 = 【登记了意图 + 这条线配了字段表】，回填不再是前置。
    #    10-10 那版把"agent 回填过"当弹表条件，代价是现网实测【点完 skill 到看见表空窗 38 分钟】
    #    （20:18 登记 → 20:56 才出表），中间客户什么都不知道、也催不动。
    #    压力没有取消，只是【换了位置】：回填仍是"报价/建批准条子"的硬前置
    #    （gateway.form_prefill_blockers，2026-10-11 已落 + 闸 F 组）⇒ 空表可以弹，但空表换不来一次扣费。
    #    保留 prefilled 这个字段：页面与判定口都要能说出"agent 还没看过素材"，不许静默。
    surfaced = True
    return {"intent": True, "has_input": True, "up": True, "alive": True, "source": "explicit",
            "skill": hit["skill"], "intent_skill": intent, "task": str(rec.get("task") or ""),
            "surfaced": True, "submitted": submitted, "prefilled": bool(pre),
            # 已提交但还没被插件叫醒 ⇒ 页面可以显示"参数已收到，正在开始跑"。
            # 🔴 这只是显示用的【状态】，不是第二份判据：要不要叫醒只由 form_wake_queue 那一份算。
            "wake_pending": bool((live.get("wake") or {}).get("pending")),
            "url": "http://127.0.0.1:%d/ui/%s?session=%s" % (
                _admin_port(), hit["skill"], urllib.parse.quote(str(sid or "")))}


def _admin_port():
    try:
        return int(os.environ.get("MODEL_ADMIN_PORT") or 8100)
    except ValueError:
        return 8100


# 把表单视图注册进判定口（hypit_bridge 不 import model_admin，避免环；这一行就是那根单向线）。
hypit_bridge.register_form_view(form_presence_view)


def delivery_pending_view(sid):
    """交付欠账的读视图（右栏 / 收工拦截 / 回归闸共用同一个口径，不开第二个口）。

    🔴 三态纪律（presence 五修的同一条）：读不到台账 ⇒ `known:false`，意思是"没查清"，
       绝不能报成"这条会话不欠东西" —— 那会让拦截器以为可以收工。
    期望项有两种：文件路径（omni / 去水印）与 job_id（hypit 的腿，提交时还没有文件名），
    所以 hypit 的销账要靠 jobs 表的终态判据 `delivery_job_done`。
    """
    try:
        import delivery_ledger as DL
        rows = DL.pending(session=sid, job_done=hypit_bridge.delivery_job_done)
        now = time.time()
        items = []
        for r in rows[:12]:
            items.append({"task": r.get("task"), "skill": r.get("skill"), "session": r.get("session"),
                          "outstanding": len(r.get("outstanding") or []),
                          "delivered": len(r.get("delivered") or []),
                          "paths": [(i.get("path") or ("job:" + str(i.get("job_id") or "")))
                                    for i in (r.get("outstanding") or [])][:12],
                          "age_sec": int(now - float(r.get("created_at") or now)),
                          "budget_credits": r.get("budget_credits") or 0})
        return {"known": True, "session": sid or "", "count": len(rows),
                "outstanding_credits": sum(int(r.get("budget_credits") or 0) for r in rows),
                "items": items}
    except Exception as e:
        return {"known": False, "session": sid or "", "error": str(e)[:200]}


class H(BaseHTTPRequestHandler):
    # 206/Range 是 HTTP/1.1 的特性，1.0 下浏览器把视频当整段下载 ⇒ 播得了、拖不动。
    # 切 1.1 的前提是全部分支都带 Content-Length（本类只有 _send/_send_file 两处写口，都带）。
    protocol_version = "HTTP/1.1"
    # 🔴 keep-alive 必须有空闲超时。HTTP/1.1 下每条连接会一直挂在 handle() 里等下一个请求
    #    （BaseHTTPRequestHandler.timeout 默认 None = 永不超时），右栏插件 5 秒一轮 + 表单页 8 秒一轮
    #    ⇒ 连接只增不减。2026-10-11 现网就是这么攒到 21 条、accept 积压打满，新连接直接
    #    【连接被拒】—— 端口还 LISTENING、进程还在，但整个 8100 不再答话（表单页与批准页同时死）。
    #    20 秒比任何一轮轮询都长、比一次人工操作短；只掐空闲连接，正在传的不受影响。
    timeout = 20

    def _send(self, code, body, ctype="application/json", cors=False):
        b = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        if cors:   # 只给"本机状态只读口"开：DSH 右栏插件在 dsh-app:// 源里轮询，不带 CORS 就 fetch 不到
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(b)

    def _not_loopback(self, what="视频生成批准"):
        """非环回就 403 并返回 True（调用方直接 return）。判据只在 _is_loopback 那一份。"""
        if not _is_loopback(self.client_address[0]):
            self._send(403, json.dumps({"ok": False, "error": "FORBIDDEN：%s 只听环回请求" % what},
                                       ensure_ascii=False))
            return True
        return False

    def _send_file(self, data, ctype, disposition=None, cors=False):   # 二进制：不能带 charset，否则 hypit collect 的 content-type 判定会脏
        total = len(data)
        start, end = 0, total - 1
        state, part = 200, False
        spec = (self.headers.get("Range") or "").strip()
        if total and spec.startswith("bytes="):
            first = spec[6:].split(",")[0].strip()   # 只认第一段：播放器本就逐段请求，多段合成在这台本机没意义
            if first.count("-") == 1:
                a, bnd = first.split("-", 1)
                if a == "" and bnd.isdigit():                    # bytes=-N ⇒ 尾 N 字节
                    start = max(0, total - int(bnd))
                elif a.isdigit():                                 # bytes=A-B（B 可空＝到末尾）
                    start = int(a)
                    if bnd and not bnd.isdigit():
                        start = None
                    elif bnd:
                        end = min(int(bnd), end)
                else:
                    start = None
            if start is not None and start >= total:              # 越界要明说 416，别回 200 全量骗人
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % total)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            part = start is not None and start <= end
        body = data[start:end + 1] if part else data
        self.send_response(206 if part else 200)
        self.send_header("Accept-Ranges", "bytes")
        if part:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, total))
        self.send_header("Content-Type", ctype)
        if cors:
            # 大文件改走这条口后不能丢 CORS：DSH 右栏/看板是跨源 <img> 取图的
            self.send_header("Access-Control-Allow-Origin", "*")
        if disposition:
            # 交付卡上的【下载视频】按钮靠这一条把"导航"变成"下载"（跨源时 <a download> 属性会被忽略，
            # 只有 attachment 头一定生效）。
            self.send_header("Content-Disposition", disposition)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _hypit_denied(self):
        if hypit_bridge.authorized(self.headers.get("Authorization")):
            return False
        self._send(401, json.dumps({"error": {"code": "UNAUTHORIZED",
                                              "message": "缺/错 MODEL_GATEWAY_TOKEN"}}, ensure_ascii=False))
        return True

    def _export_denied(self):
        """导出口：Bearer 对 ⇒ 放行；没带 token 时只认本机 origin（DSH 插件里不能嵌 token）。"""
        if hypit_bridge.export_authorized(self.headers.get("Authorization"), self.headers.get("Origin")):
            return False
        self._send(403, json.dumps({"error": {"code": "FORBIDDEN",
                                              "message": "导出口只收 Bearer 或本机 origin 的请求"}}, ensure_ascii=False),
                   cors=True)
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
        elif u.path == "/api/spend/approvals":
            # 只读：现在有哪几张"人批准的条子"、各还剩几段。没有创建条子的 GET/POST 口（见 spend_guard）。
            self._send(200, json.dumps(gateway.spend_approvals(), ensure_ascii=False))
        elif u.path in ("/approval", "/approval/"):
            self._send(200, APPROVAL_PAGE, "text/html")
        elif u.path == "/api/spend/pending":
            # 给批准页渲染用：只读、无副作用、【不含核销码】（核销走 consume_by_click，码由服务端自取）。
            # path/count_raw 是诊断字段：查"批准页看不到待批"用 —— 两个进程读的不是同一份 json 时一眼可见。
            rows = [{"id": p["id"], "capability": p["capability"], "purpose": p["purpose"],
                     "account": p["account"], "count": p["count"], "credits": p["credits"],
                     "scope": p.get("scope", ""), "session": p.get("session", ""),
                     "left_seconds": max(0, round(float(p["expires"]) - time.time()))}
                    for p in gateway.spend_pending()]
            self._send(200, json.dumps({"pending": rows,
                                        "path": gateway.SPEND_APPROVALS_PATH,
                                        "count_raw": len(gateway._approvals_load().get("pending") or [])},
                                       ensure_ascii=False), cors=True)
        elif u.path.startswith("/ui/"):
            # 技能表单页：【一个模板】服务所有 skill —— 字段表由页面自己问 /api/skills/form 要。
            # 同源相对路径是这条设计的根据：页面出自 8100，接口也在 8100 ⇒ 右栏 iframe 不需要跨口。
            self._send(200, skill_form_page.render_form_page(), "text/html", cors=True)
        elif u.path == "/api/skills/form/file":
            # 表单上传目录的只读回显（缩略图）。三道闸见 form_media_file()，且只听环回。
            if self._not_loopback():
                return
            q = parse_qs(u.query)
            data, ctype, err = form_media_file((q.get("session") or [""])[0],
                                               (q.get("param") or [""])[0],
                                               (q.get("name") or [""])[0])
            if data is None:
                self._send(404, json.dumps({"ok": False, "error": err}, ensure_ascii=False), cors=True)
            else:
                self._send_file(data, ctype, cors=True)
        elif u.path == "/api/skills/form":
            # 对话内表单的唯一读口：按会话意图门控（没登记意图 ⇒ show:false，一条 UI 都不出）。
            q = parse_qs(u.query)
            self._send(200, json.dumps(form_gate_view((q.get("session") or [""])[0]),
                                       ensure_ascii=False), cors=True)
        elif u.path == "/api/skills/form/wake":
            # 待醒队列：宿主插件问"该叫醒哪几条会话"。🔴 只听环回 —— 这份表暴露了
            # 本机有哪些会话在干什么，局域网里别的机器不该看见。
            if self._not_loopback("/api/skills/form/wake"):
                return
            self._send(200, json.dumps(wake_view(), ensure_ascii=False))
        elif u.path == "/api/skills/form/attempts":
            # 只读：客户点了【提交】但没成交的最近几次（缺哪格 / 哪条会话 / 拒因）。
            # 🔴 只听环回：里面带会话 id 与任务名；并且它【不是】第二份"已提交"判据 —— 只是现场。
            if self._not_loopback("/api/skills/form/attempts"):
                return
            q = parse_qs(u.query)
            self._send(200, json.dumps(form_attempts_view((q.get("n") or ["10"])[0]), ensure_ascii=False))
        elif u.path == "/assets/weeksir-logo.png":
            try:
                with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets",
                                       "weeksir-logo.png"), "rb") as f:
                    self._send_file(f.read(), "image/png")
            except OSError:
                self._send(404, json.dumps({"error": {"code": "NOT_FOUND"}}))
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
            # DSH 右栏 presence：代探 hypit studio 是否活着。只读、带 ACAO:*。
            # 必须带 ?session=：归属=「这个会话拥有 studio 正在服务的那个 run」，
            # 不带就只回运维视图（up 恒 false）——studio 是单实例无会话概念，拿"端口活着"当
            # 跨会话信号会在每个会话都弹页签（2026-10-01/10-02 三次复发）。
            q = parse_qs(u.query)
            sid = (q.get("session") or [None])[0]
            self._send(200, json.dumps(hypit_bridge.studio_status(sid), ensure_ascii=False), cors=True)
        elif u.path == "/api/hypit/export":
            # 导出进度：?id=<导出id>。只读，带 ACAO:* 让右栏能轮询。
            q = parse_qs(u.query)
            self._send(200, json.dumps(hypit_bridge.export_status((q.get("id") or [""])[0]), ensure_ascii=False),
                       cors=True)
        elif u.path == "/api/workbench/presence":
            # 右栏唯一的判定口：这条会话该挂哪个台、台活着没。只读、带 ACAO:*。
            q = parse_qs(u.query)
            sid = (q.get("session") or [None])[0]
            self._send(200, json.dumps(hypit_bridge.workbench_presence(sid), ensure_ascii=False), cors=True)
        elif u.path == "/api/delivery/pending":
            # 交付欠账读口（docs/PRD-delivery-loop.md §5）：只读、带 CORS，写不进任何东西。
            q = parse_qs(u.query)
            sid = (q.get("session") or [None])[0]
            self._send(200, json.dumps(delivery_pending_view(sid), ensure_ascii=False), cors=True)
        elif u.path == "/api/film/pipeline":
            # 短剧带货全流程看板数据口:读 productions/<task>/ 各阶段文件,组织成"输入→中间→输出"。
            # 只读、带 CORS(右栏 iframe 轮询)。task 缺省给最近的 film 项目。
            import sys, os as _os, json as _json
            task = (parse_qs(u.query).get("task") or [""])[0]
            if not task:
                # 缺省:扫 productions/ 找最近改过的含 00_project 的项目
                proots = [_os.environ.get("DSH_PROD_ROOT") or _os.path.join(_os.environ.get("DSH_WORKSPACE") or r"C:\dsh", "productions")]
                cand = []
                for pr in proots:
                    if _os.path.isdir(pr):
                        for d in _os.listdir(pr):
                            dd = _os.path.join(pr, d)
                            if _os.path.isdir(_os.path.join(dd, "00_project")):
                                cand.append((dd, _os.path.getmtime(dd)))
                cand.sort(key=lambda x: -x[1])
                task = _os.path.basename(cand[0][0]) if cand else ""
            import importlib
            sys.path.insert(0, r"C:\dsh")
            try:
                fps = importlib.import_module("_film_pipeline_status")
                payload = fps.film_pipeline(task)
            except Exception as e:
                payload = {"error": "读项目失败: %s: %s" % (type(e).__name__, str(e)[:150]), "task": task}
            self._send(200, json.dumps(payload, ensure_ascii=False), cors=True)
        elif u.path == "/api/film/file":
            # 看板页托管项目内图片/视频:GET ?task=&rel=[&dl=1],只放行 productions/<task>/ 内的媒体/JSON。
            # 防路径穿越:realpath 后必须在项目根内。带 Range(MP4 播放器要 206)。
            # 🔴 dl=1 ⇒ 回 Content-Disposition: attachment，交付卡上的【下载视频】按钮就指这里。
            #    下载腿只放媒体扩展名：json 是看板读数据用的，不能变成"把 productions 下任意状态文件下载走"的口子
            #    （outputs/sr_*/phase_state.json 里就有明文厂商 key）。
            import os as _os, urllib.parse as _up
            task = (parse_qs(u.query).get("task") or [""])[0]
            rel = _up.unquote((parse_qs(u.query).get("rel") or [""])[0])
            qs = parse_qs(u.query)
            want_dl = (qs.get("dl") or [""])[0].lower() in ("1", "true", "yes")
            proots = [_os.environ.get("DSH_PROD_ROOT") or _os.path.join(_os.environ.get("DSH_WORKSPACE") or r"C:\dsh", "productions")]
            if not task or not rel:
                self._send(400, json.dumps({"error": "缺 task 或 rel"}), cors=True); return
            target = None
            # 🔴 越界判定以前只看 rel（`".." in rel`），task 完全没查 ⇒ ?task=..%2F..%2F..&rel=Windows/win.ini
            #    这种拼法能跳出 productions/。这条口现在还要管【下载】，必须按 realpath 判根，
            #    不能靠"字符串里有没有 .."（normpath 会把它吃掉，符号链接更不管用）。
            for pr in proots:
                p = _os.path.realpath(_os.path.join(pr, task, rel))
                root = _os.path.realpath(pr)
                if p.startswith(root + _os.sep) and _os.path.isfile(p):
                    target = p; break
            if not target:
                self._send(404, json.dumps({"error": "文件不存在或越界"}), cors=True); return
            ext = _os.path.splitext(target)[1].lower().lstrip(".")
            # 🔴 键是"png/mp4"这种【不带点】的扩展名：以前拿 ".mp4" 去查这张表永远查不中，
            #    于是所有看板直链都回 application/octet-stream —— 视频在浏览器里变成"下载"而不是"播放"，
            #    图片也不内联显示（交付三件套里的"直链"这一件其实是坏的）。
            ctype = FILM_INLINE_TYPES.get(ext)
            # 🔴 未知扩展名以前回落到 octet-stream 照样发出去 = productions/ 下【任何】文件都能被读走
            #    （.env、.py、状态 json 全在内）⇒ 白名单必须真拦：不在表里就 404，不发内容、不发类型。
            if not ctype:
                self._send(404, json.dumps({"error": "该类型不托管"}, ensure_ascii=False), cors=True); return
            if want_dl and ext not in FILM_DOWNLOADABLE:
                self._send(404, json.dumps({"error": "只有图片/视频可下载"}, ensure_ascii=False), cors=True); return
            with open(target, "rb") as f:
                data = f.read()
            disp = None
            if want_dl:
                name = _os.path.basename(target)
                ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", name) or "download"
                disp = "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (ascii_name, _up.quote(name, safe=""))
            # 下载腿与【所有二进制】都走 _send_file（带 Range、不带 charset）。
            # 🔴 以前这条只认 mp4 ⇒ >1MB 的 PNG/JPG 落到 _send 分支，被盲加 `; charset=utf-8`
            #    （`image/png; charset=utf-8` 是错的：二进制没有字符集）。判据按【类型】不按大小：
            #    图片/视频一律二进制出口，json 等文本仍走 _send（看板要 CORS + 字符集）。
            # 曾经的第二个坑：条件写成 ext == ".mp4"，而 ext 早被 lstrip(".") 洗成不带点 ⇒ 恒假，
            #    大 MP4 从来没走过 Range 分支、直链拖不动（2026-10-08 修）。
            if want_dl or ctype.startswith(("image/", "video/")):
                self._send_file(data, ctype, disposition=disp, cors=True)
            else:
                self._send(200, data, ctype, cors=True)
        elif u.path == "/api/film/projects":
            # 全流程看板的项目下拉:列 productions/ 下含 00_project 的 film 项目。
            import importlib, sys as _sys
            _sys.path.insert(0, r"C:\dsh")
            try:
                fps = importlib.import_module("_film_pipeline_status")
                self._send(200, json.dumps(fps.film_projects(), ensure_ascii=False), cors=True)
            except Exception as e:
                self._send(500, json.dumps({"error": "读项目列表失败: %s: %s" % (type(e).__name__, str(e)[:150])}), cors=True)
        elif u.path == "/api/film/pipeline":
            import importlib, sys as _sys
            _sys.path.insert(0, r"C:\dsh")
            task = (parse_qs(u.query).get("task") or [""])[0]
            try:
                fps = importlib.import_module("_film_pipeline_status")
                payload = fps.film_pipeline(task)
            except Exception as e:
                payload = {"error": "读项目失败: %s: %s" % (type(e).__name__, str(e)[:150]), "task": task}
            self._send(200, json.dumps(payload, ensure_ascii=False), cors=True)
        elif u.path == "/api/film/board":
            # 看板页(8100 同域,iframe 直接 src)
            import importlib, sys as _sys
            _sys.path.insert(0, r"C:\dsh")
            try:
                fps = importlib.import_module("_film_pipeline_status")
                self._send(200, fps.film_board_html(), "text/html; charset=utf-8")
            except Exception as e:
                self._send(500, "<html><body>看板页加载失败: %s: %s</body></html>" % (type(e).__name__, str(e)[:150]), "text/html; charset=utf-8")
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
        elif u.path == "/api/hypit/export":
            if self._export_denied():
                return
            try:
                body = json.loads(raw.decode("utf-8", "replace") or "{}")
            except Exception as e:
                self._send(400, json.dumps({"error": {"code": "BAD_JSON", "message": str(e)}}, ensure_ascii=False), cors=True)
                return
            code, payload = hypit_bridge.handle_export(body if isinstance(body, dict) else {})
            self._send(code, json.dumps(payload, ensure_ascii=False), cors=True)
        elif u.path == "/api/spend/guard":
            # 只环回：消耗批准这件事不该由局域网里别的机器替本机用户做。
            if self._not_loopback("/api/spend/guard"):
                return
            try:
                body = json.loads(raw.decode("utf-8", "replace") or "{}")
            except Exception as e:
                self._send(400, json.dumps({"ok": False, "error": "bad json: %s" % e}, ensure_ascii=False))
                return
            code, payload = spend_guard(body if isinstance(body, dict) else {})
            self._send(code, json.dumps(payload, ensure_ascii=False))
        elif u.path == "/api/media/upload":
            # 素材上传口（不计费、不建预扣）。🔴 只听环回：这条口会把本机文件送到第三方，
            # 局域网里别的机器不该替本机用户做这件事。
            if self._not_loopback("/api/media/upload"):
                return
            try:
                body = json.loads(raw.decode("utf-8", "replace") or "{}")
            except Exception as e:
                self._send(400, json.dumps({"ok": False, "error": "bad json: %s" % e}, ensure_ascii=False))
                return
            code, payload = media_upload(body if isinstance(body, dict) else {})
            self._send(code, json.dumps(payload, ensure_ascii=False))
        elif u.path == "/api/skills/form":
            # 对话内表单的唯一写口。🔴 只听环回（往本机写文件 + 写"这条会话的引擎入参"，
            # 不是局域网里别的机器该替本机做的事）。
            # ⚠️ 本类【没有 do_OPTIONS】⇒ 前端必须用简单请求（Content-Type: text/plain）发，
            #    用 application/json 会触发预检、直接撞 501，表现为"提交没反应"。
            if self._not_loopback():
                return
            try:
                body = json.loads(raw.decode("utf-8", "replace") or "{}")
            except Exception as e:
                self._send(400, json.dumps({"ok": False, "error": "bad json: %s" % e},
                                           ensure_ascii=False), cors=True)
                return
            out = form_route_dispatch(body if isinstance(body, dict) else {})
            self._send(200, json.dumps(out, ensure_ascii=False), cors=True)
        elif u.path == "/api/skills/form/wake/ack":
            # 插件叫醒之后来销账。只清【它领走的那一次】(at 对得上)，不清整条 ——
            # 客户在插件路上又重交了一次时，旧 ack 不能把新那格抹掉（那就又变成没人叫）。
            if self._not_loopback("/api/skills/form/wake/ack"):
                return
            try:
                body = json.loads(raw.decode("utf-8", "replace") or "{}")
            except Exception as e:
                self._send(400, json.dumps({"ok": False, "error": "bad json: %s" % e},
                                           ensure_ascii=False))
                return
            r = wake_ack(body if isinstance(body, dict) else {})
            self._send(200 if r.get("ok") else 409, json.dumps(r, ensure_ascii=False))
        elif u.path == "/api/skills/form/compiled":
            # 交表即点火的那台编译跑完回来挂号（第三份唤醒来源）。🔴 只听环回 + 只认服务端自己起的 pid，
            # 否则任何本机进程 POST 一下就能凭空造一条"待醒"，把唤醒链变成注入面。
            if self._not_loopback("/api/skills/form/compiled"):
                return
            try:
                body = json.loads(raw.decode("utf-8", "replace") or "{}")
            except Exception as e:
                self._send(400, json.dumps({"ok": False, "error": "bad json: %s" % e},
                                           ensure_ascii=False))
                return
            r = compiled_report(body if isinstance(body, dict) else {})
            self._send(200 if r.get("ok") else 409, json.dumps(r, ensure_ascii=False))
        elif u.path in ("/api/spend/approve", "/api/spend/deny"):
            # 花钱批准页的两个动作口。🔴 只听环回（和 /api/spend/guard 同一条理由：批准/拒绝
            # 是本机用户的事，不该由局域网里别的机器替他点）。这里【没有】创建 pending 的口 ——
            # 提请由 agent 跑 gateway.py approve 完成，它只能建待批、拿不到额度。
            if self._not_loopback():
                return
            try:
                body = json.loads(raw.decode("utf-8", "replace") or "{}")
            except Exception as e:
                self._send(400, json.dumps({"ok": False, "error": "bad json: %s" % e}, ensure_ascii=False))
                return
            pid = str((body or {}).get("pending_id") or "")
            if not pid:
                self._send(400, json.dumps({"ok": False, "error": "缺 pending_id"}, ensure_ascii=False))
                return
            if u.path.endswith("deny"):
                r = gateway.deny_pending(pid)
            else:
                r = gateway.consume_by_click(pid)     # 码由服务端自己取，页面不需要知道
                # 🔴 人点掉批准 = 一个必然发生的事件 ⇒ 当场给那条会话挂号待醒（宿主插件 5 秒内领走、开新一轮）。
                #    缺这一格就是 2026-10-10 那句"我点了审批不执行，还得回一句点了条子"：
                #    agent 只在轮到自己说话时才动，没人叫醒它，它就只能等下一句人话。
                #    置顶批准窗（approval_popup.py）走的也是这个口 ⇒ 两个壳共用同一次挂号，不各写一份。
                #    ⚠️ 点【这次不跑】(deny) 故意【不】挂号：那一下的语义是"停"，叫醒 agent 没有活可派，
                #       只会多烧一轮 token；agent 那边照旧靠 approvals/过期判据自己收口。
                if r.get("ok"):
                    r["wake"] = approval_wake_note(r.get("grant") or {})
            self._send(200 if r.get("ok") else 409, json.dumps(r, ensure_ascii=False))
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
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    except OSError as e:
        # 🔴 端口被占 ⇒ 立刻退非 0，绝不留一个"活着但没在服务"的进程。
        #    2026-10-11 现网同时挂着两份 model_admin（一份抢到 8100、一份一个端口都没有），
        #    看门狗与人都只看得见端口那份，另一份成了查不出来的僵尸。
        sys.stderr.write("bind 127.0.0.1:%d failed: %s: %s ⇒ 已有实例在跑，本进程退出\n"
                         % (port, type(e).__name__, e))
        sys.stderr.flush()
        sys.exit(3)

    try:
        n = hypit_bridge.reconcile_orphans()   # 别让上个进程留下的 job 永远挂着 running（hypit 会一直等）
        held = hypit_bridge.holds_pending()
        if held:                        # 只报不自动退：自动退可能把别的进程正在花的分退掉
            print("[startup] 遗留未结算预扣 %d 笔 / 共 %d 分（holds-rollback <id> 逐笔回收）"
                  % (len(held), sum(h["amount"] or 0 for h in held)))
        if n:
            print(f"reconciled {n} orphaned hypit job(s) from the previous process")
    except Exception as e:
        print(f"hypit job reconcile skipped: {e}")
    try:
        n = hypit_bridge.export_reconcile()    # 导出同理：重启打断的那条要判死，别一直显示 building
        if n:
            print(f"reconciled {n} interrupted hypit export(s)")
    except Exception as e:
        print(f"hypit export reconcile skipped: {e}")
    print(f"Weeksir model admin on http://127.0.0.1:{port}  (ENV={ENV_PATH})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
