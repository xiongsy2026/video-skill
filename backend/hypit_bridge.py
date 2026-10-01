"""hypit Provider 适配面：把统一模型网关包成 hypit「自写 Provider」期望的作业协议。

协议出处 = 分发内样板 `examples/provider-package`（README「Follow a video request」），
我们两端都是自己写 ⇒ 满足上游那条硬约束「OpenAI 兼容标签不代表协议匹配」：

  POST /api/hypit/uploads   请求体=媒体字节，头 content-type（可选 x-person-reference） → {"url": "..."}
  POST /api/hypit/videos    {"model","prompt","duration","resolution","ratio","references":[{url,person}],
                             "firstFrame","lastFrame","account"}                          → {"id"}
  GET  /api/hypit/videos/<id>                                                              → {"status","output","error"}
  GET  /api/hypit/rates?model=<m>            → {"description", ...}   （hypit 的 pricing 只读检查用）
  GET  /api/hypit/assets/<name>              结果文件，collect 步骤用（样板规定：签名 URL 不带账号密钥）

生成动作全部转成 `gateway.invoke(...)` 在后台线程里跑 ⇒ 积分预扣/回滚、路由、审计、屏蔽位
与 CLI/`/api/model/call` **完全同一套**，不另开第二套账。
"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import threading
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPLOAD_DIR = os.path.join(ROOT, "outputs", "_hypit_uploads")
OUT_DIR = os.path.join(ROOT, "outputs", "_hypit_jobs")
DB_PATH = os.path.join(ROOT, ".gateway_jobs.sqlite")
# 必须用 localhost 而不是 127.0.0.1：hypit 的 address()/pricing source 校验只放 https 或 hostname=localhost。
PUBLIC_BASE = os.environ.get("MODEL_GATEWAY_PUBLIC", "http://localhost:8100")

# hypit 送来的 wire model → 我们的 (capability, purpose)。只登记真能出片的，别的直接拒（不悄悄换模型）。
HYPIT_MODELS = {
    "seedance-2-mini": ("video.generate", "seedance-2.0"),
    "seedance-2.0": ("video.generate", "seedance-2.0"),
    "seedance-2.5": ("video.generate", "seedance-2.5"),
    "omni-flash-10s": ("video.generate", "omni-t2v"),
}
# 本适配面真实能带的输入：只有图片参考（vinted/zexapi 都只吃 image_urls，且要公网可取 URL）。
# 视频/音频参考带不过去 ⇒ 按 hypit 的规矩在 supports 里拒绝，绝不静默丢弃。
MAX_SECONDS = 10
RESOLUTIONS = ("720p",)
# hypit studio 的工作台端口（agent 起 `hypit studio --run X --port 5179`）。端口不给 HTTP 参数控制：
# 这个探测口是 ACAO:* 的，能指定端口就等于给本机任意网页做端口扫描。
STUDIO_PORT = int(os.environ.get("HYPIT_STUDIO_PORT") or 5179)


def token_required():
    """鉴权开关：.env 的 MODEL_GATEWAY_TOKEN 非空即要求 Bearer 匹配（空=保持现状，仅本机）。"""
    from config import settings
    return str(getattr(settings, "MODEL_GATEWAY_TOKEN", "") or "")


def authorized(header_value: str | None) -> bool:
    need = token_required()
    if not need:
        return True
    return bool(header_value) and header_value.strip().removeprefix("Bearer ").strip() == need


def _db():
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, model TEXT, state TEXT,"
                " route TEXT, credits INTEGER DEFAULT 0, output TEXT, error TEXT, request TEXT,"
                " created REAL, updated REAL)")
    con.commit()
    return con


def _touch(jid, **fields):
    con = _db()
    cols = ", ".join(f"{k}=?" for k in fields)
    con.execute(f"UPDATE jobs SET {cols}, updated=? WHERE id=?", (*fields.values(), time.time(), jid))
    con.commit()
    con.close()


def _job(jid):
    con = _db()
    row = con.execute("SELECT id,model,state,route,credits,output,error FROM jobs WHERE id=?", (jid,)).fetchone()
    con.close()
    if not row:
        return None
    return {"id": row[0], "model": row[1], "status": row[2], "route": row[3],
            "credits": row[4] or 0, "output": row[5], "error": row[6]}


def ext_for(media_type: str) -> str:
    return {"image/png": ".png", "image/webp": ".webp", "image/jpeg": ".jpg",
            "video/mp4": ".mp4", "audio/mpeg": ".mp3", "audio/wav": ".wav"}.get(media_type, ".bin")


def _imgbb_url(path: str) -> str | None:
    """传图床拿公网 URL：vinted/zexapi 这类上游要能自己抓取，loopback 地址它们取不到。"""
    from config import settings
    key = str(getattr(settings, "IMGBB_API_KEY", "") or "")
    if not key:
        return None
    import urllib.error
    import urllib.parse
    import urllib.request
    b64 = base64.b64encode(open(path, "rb").read()).decode()
    data = urllib.parse.urlencode({"key": key, "image": b64, "name": os.path.basename(path)}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request("https://api.imgbb.com/1/upload", data=data), timeout=60) as r:
            return str(json.loads(r.read().decode())["data"]["url"])
    except Exception:
        return None


def handle_upload(body: bytes, media_type: str) -> dict:
    """POST /uploads → {url}。落本地 + 尽量换公网 URL（图），换不到就退回本机地址并让 supports 去拒。"""
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    name = f"{uuid.uuid4().hex}{ext_for(media_type)}"
    path = os.path.join(UPLOAD_DIR, name)
    with open(path, "wb") as f:
        f.write(body)
    url = _imgbb_url(path) if media_type.startswith("image/") else None
    return {"url": url or f"{PUBLIC_BASE}/api/hypit/uploads/files/{name}", "local": path,
            "public": bool(url)}


def serve_upload(name: str):
    path = os.path.normpath(os.path.join(UPLOAD_DIR, os.path.basename(name)))
    if not path.startswith(os.path.normpath(UPLOAD_DIR)) or not os.path.isfile(path):
        return None, None
    data = open(path, "rb").read()
    return data, ("image/png" if path.endswith(".png") else
                  "image/jpeg" if path.endswith(".jpg") else
                  "image/webp" if path.endswith(".webp") else "application/octet-stream")


def check_support(body: dict) -> tuple:
    """能带就收、不能带就明说（hypit 的 plan 会把这句话原样给用户看）。"""
    model = str(body.get("model") or "")
    if model not in HYPIT_MODELS:
        return False, f"本服务不提供模型 {model or '(空)'}；可选: {', '.join(sorted(HYPIT_MODELS))}"
    try:
        dur = float(body.get("duration") or 5)
    except (TypeError, ValueError):
        return False, "duration 必须是数字"
    if dur > MAX_SECONDS:
        return False, f"本服务最长渲染 {MAX_SECONDS} 秒，不是 {dur:g} 秒"
    res = str(body.get("resolution") or RESOLUTIONS[0])
    if res not in RESOLUTIONS:
        return False, f"本服务只出 {'/'.join(RESOLUTIONS)}，不出 {res}"
    for field in ("videoReferences", "audioReferences"):
        if body.get(field):
            return False, f"本服务不接受 {field}（上游只收图片参考；不静默丢弃）"
    refs = body.get("references") or []
    if not isinstance(refs, list):
        return False, "references 必须是数组"
    for item in refs:
        u = item.get("url") if isinstance(item, dict) else item
        if not str(u or "").startswith("http"):
            return False, "references 里每项都要带可抓取 url"
    return True, ""


def _run(jid: str, body: dict):
    cap, purpose = HYPIT_MODELS[str(body.get("model"))]
    params = {"prompt": str(body.get("prompt") or ""),
              "duration": int(float(body.get("duration") or 5)),
              "aspect_ratio": str(body.get("ratio") or "9:16"),
              "resolution": str(body.get("resolution") or RESOLUTIONS[0])}
    urls = [str((r.get("url") if isinstance(r, dict) else r)) for r in (body.get("references") or [])]
    for extra in ("firstFrame", "lastFrame"):
        if body.get(extra):
            urls.append(str(body[extra]))
    if urls:
        params["image_urls"] = urls
    os.makedirs(OUT_DIR, exist_ok=True)
    params["output_dir"] = params["output_path"] = os.path.join(OUT_DIR, f"{jid}")
    _touch(jid, state="running")
    from backend import gateway
    try:
        env = gateway.invoke(cap, purpose, str(body.get("account") or "hypit"), params)
    except Exception as e:      # invoke 内部已兜异常；这里只防 resolve 段抛 RouteError
        _touch(jid, state="failed", error=json.dumps({"code": "GATEWAY_ERROR", "message": str(e)[:400]},
                                                     ensure_ascii=False))
        return
    res = env.get("result") or {}
    credits = (env.get("credits") or {}).get("charged") or 0
    if not env.get("ok"):
        _touch(jid, state="failed", credits=credits,
               error=json.dumps({"code": "GATEWAY_FAILED",
                                 "message": str(env.get("error") or res.get("error") or "生成失败")[:400]},
                                ensure_ascii=False))
        return
    path = res.get("path") or ""
    name = ""
    if path and os.path.isfile(path):
        name = f"{jid}{os.path.splitext(path)[1] or '.mp4'}"
        try:
            os.replace(path, os.path.join(OUT_DIR, name))
        except OSError:
            name = ""
    if not name and not res.get("url"):
        _touch(jid, state="failed", credits=credits,
               error=json.dumps({"code": "NO_OUTPUT", "message": "上游报成功但既无文件也无 URL"}, ensure_ascii=False))
        return
    output = f"{PUBLIC_BASE}/api/hypit/assets/{name}" if name else str(res.get("url"))
    _touch(jid, state="succeeded", credits=credits, output=output)


def handle_submit(body: dict) -> tuple:
    ok, reason = check_support(body)
    if not ok:
        return 400, {"error": {"code": "UNSUPPORTED_INPUT", "message": reason}}
    jid = uuid.uuid4().hex[:16]
    model = str(body.get("model"))
    con = _db()
    con.execute("INSERT INTO jobs(id,model,state,route,created,updated,request) VALUES(?,?, 'queued',?,?,?,?)",
                (jid, model, "/".join(HYPIT_MODELS[model]), time.time(), time.time(),
                 json.dumps(body, ensure_ascii=False)[:4000]))
    con.commit()
    con.close()
    threading.Thread(target=_run, args=(jid, body), daemon=True).start()
    return 200, {"id": jid}


def studio_status() -> dict:
    """右栏 presence 信号：DSH 插件在 dsh-app:// 源里 fetch，Vite 的 CORS 只放行白名单 origin，
    浏览器直连 5179 会被拦 ⇒ 由 8100 代探一次，回 ACAO:* 的紧凑状态（不泄露原文，只给计数）。"""
    import urllib.error
    import urllib.request
    url = "http://localhost:%d/__studio/library?section=tasks" % STUDIO_PORT
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            data = json.loads(r.read().decode("utf-8", "replace") or "{}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"up": False, "port": STUDIO_PORT, "error": type(e).__name__}
    tasks = data.get("tasks") or []
    running = [t for t in tasks if str(t.get("status", "")).lower() not in ("", "finished", "failed", "cancelled", "saved")]
    return {"up": True, "port": STUDIO_PORT, "tasks": len(tasks), "running": len(running),
            "artifacts": len(data.get("artifacts") or [])}


def handle_get(jid: str) -> dict:
    j = _job(jid)
    if not j:
        return {"error": {"code": "NOT_FOUND", "message": f"没有这个任务 {jid}"}}
    out = {"id": j["id"], "status": j["status"], "model": j["model"],
           "credits": j["credits"], "route": j["route"]}
    if j["output"]:
        out["output"] = j["output"]
    if j["error"]:
        out["error"] = json.loads(j["error"])
    return out


def handle_rates(query: dict) -> dict:
    from backend import gateway
    model = (query.get("model") or [""])[0]
    if model not in HYPIT_MODELS:
        return {"error": {"code": "UNKNOWN_MODEL", "message": f"未登记 {model}"}}
    cap, purpose = HYPIT_MODELS[model]
    unit = gateway.unit_price(cap, purpose)
    return {"model": model, "route": f"{cap}/{purpose}", "credits_per_10s": unit,
            "description": (f"本服务（Weeksir 网关 {cap}/{purpose}）：{RESOLUTIONS[0]}、最长 {MAX_SECONDS} 秒、"
                            f"只收图片参考；扣费 {unit} 分/10 秒（文本/看图才按 token 结算），"
                            f"上游实付单价未录入 ⇒ 这只是内部记账单位，不是对客报价")}


def serve_asset(name: str):
    path = os.path.normpath(os.path.join(OUT_DIR, os.path.basename(name)))
    if not path.startswith(os.path.normpath(OUT_DIR)) or not os.path.isfile(path):
        return None, None
    ext = os.path.splitext(path)[1].lower()
    return open(path, "rb").read(), {".mp4": "video/mp4", ".mov": "video/quicktime",
                                     ".webm": "video/webm", ".jpg": "image/jpeg", ".png": "image/png"}.get(ext,
                                                                                                            "application/octet-stream")
