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
# 上游 vinted 的瞬时错误（实测：503 参考图下载繁忙、400 参考图解析失败、生成超时且明确退积分）⇒ 原话让重试。
# ⚠️ 不含"轮询超时"：那是我们自己等够了，上游可能还在出片，盲目重试会重复花费。
RETRYABLE = ("502", "503", "504", "service_unavailable", "参考图", "生成超时")
# 策略/身份类拒绝：重试也不会变，只会白等 15~20 分钟（实测 "参考图未通过肖像保护" 含"参考图"三字，
# 会被上面那档误判成可重试 ⇒ 必须先排除）。
NON_RETRYABLE = ("肖像", "本人肖像", "内容不合规", "违规", "敏感")


def _retryable(msg: str) -> bool:
    return not any(k in msg for k in NON_RETRYABLE) and any(k in msg for k in RETRYABLE)
MAX_ATTEMPTS = 3
RETRY_WAITS = (30, 60)
# 谁起的 studio 登记在这（{session, run, port, ts}）。studio 本身没有 DSH 会话概念，
# 不登记主人就会在每个会话里都弹右栏（2026-10-01 实际踩过：omni 会话被弹出 Hypit 工作台）。
STUDIO_MARK = os.environ.get("HYPIT_STUDIO_MARK") or os.path.join(ROOT, ".gateway_studio.json")
# 诊断用：记下右栏探测实际带来的会话 id（运维据此知道该给哪个会话 claim；上限 30 条）
_SEEN_SESSIONS = {}
STUDIO_CACHE_TTL = 4.0
_STUDIO_CACHE = {"ts": 0.0, "port": None, "res": {"up": False, "error": "not-probed"}}
# 代探结果短缓存：presence 每 4 秒被每个右栏窗口打一次，没缓存就是每个窗口每次都去撞一次死端口。
STUDIO_CACHE_TTL = 5.0
_STUDIO_CACHE = {"ts": 0.0, "port": None, "res": {"up": False, "error": "cold"}}


def studio_claim(session: str, run: str = "", port: int = STUDIO_PORT) -> dict:
    rec = {"session": str(session or ""), "run": str(run or ""), "port": int(port), "ts": time.time()}
    with open(STUDIO_MARK, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False)
    return rec


def studio_release() -> None:
    try:
        os.remove(STUDIO_MARK)
    except OSError:
        pass


def _studio_owner() -> dict:
    try:
        with open(STUDIO_MARK, encoding="utf-8") as f:
            rec = json.load(f)
        return rec if isinstance(rec, dict) else {}
    except (OSError, ValueError):
        return {}


def _probe_studio(port: int) -> dict:
    """探一次 studio 的只读口；结果缓存 5 秒——多个右栏窗口共用一份，免得各自每 4 秒去撞死端口。"""
    import urllib.error
    import urllib.request
    now = time.time()
    if _STUDIO_CACHE["port"] == port and now - _STUDIO_CACHE["ts"] < STUDIO_CACHE_TTL:
        return _STUDIO_CACHE["res"]
    url = "http://localhost:%d/__studio/library?section=tasks" % port
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            data = json.loads(r.read().decode("utf-8", "replace") or "{}")
        tasks = data.get("tasks") or []
        running = [t for t in tasks
                   if str(t.get("status", "")).lower() not in ("", "finished", "failed", "cancelled", "saved")]
        res = {"up": True, "tasks": len(tasks), "running": len(running),
               "artifacts": len(data.get("artifacts") or [])}
    except (urllib.error.URLError, OSError, ValueError) as e:
        res = {"up": False, "error": type(e).__name__}
    _STUDIO_CACHE.update(port=port, ts=now, res=res)
    return res


def studio_status(session: str | None = None) -> dict:
    """右栏 presence 信号：DSH 插件在 dsh-app:// 源里 fetch，Vite 的 CORS 只放行白名单 origin，
    浏览器直连 5179 会被拦 ⇒ 由 8100 代探一次，回 ACAO:* 的紧凑状态（不泄露原文，只给计数）。

    带 session 时按主人过滤：只有起 studio 的那个会话看得到（没登记主人就谁都不弹）。
    """
    owner = _studio_owner()
    port = int(owner.get("port") or STUDIO_PORT)
    if session is not None:
        _SEEN_SESSIONS[str(session)] = time.time()
        if len(_SEEN_SESSIONS) > 30:
            for old in sorted(_SEEN_SESSIONS, key=_SEEN_SESSIONS.get)[:len(_SEEN_SESSIONS) - 30]:
                _SEEN_SESSIONS.pop(old, None)
        if str(owner.get("session") or "") != str(session):
            return {"up": False, "port": port, "reason": "not-owner"}
    res = dict(_probe_studio(port))
    res.update(port=port, run=owner.get("run") or "", session=owner.get("session") or "")
    if session is None:   # 不带 session = 运维视角：顺便给出右栏实际用过的会话 id
        res["seen"] = [{"session": k, "age": round(time.time() - v)}
                       for k, v in sorted(_SEEN_SESSIONS.items(), key=lambda kv: -kv[1])]
    return res


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


def reconcile_orphans() -> int:
    """服务启动时调用：本服务单进程，队列里还挂着 queued/running 的行必然是上个进程留下的孤儿
    （线程已随进程消失，hypit 会一直 waiting 等不到结果）。2026-10-01 重启 8100 时真的打断了一条在跑的 build。"""
    con = _db()
    cur = con.execute(
        "UPDATE jobs SET state='failed', updated=?, error=? WHERE state IN ('queued','running')",
        (time.time(), json.dumps({"code": "GATEWAY_RESTARTED",
                                  "message": "网关重启时这条还在生成，执行线程已丢失、无法续查上游任务；请重跑这次 build"},
                                 ensure_ascii=False)))
    con.commit()
    n = cur.rowcount
    con.close()
    return n


def _normalize(body: dict) -> dict:
    """hypit 的 POST /videos 见过两种形状：字段平铺，以及 {"model":..,"input":{字段},"account":..}。
    只认平铺过一种 ⇒ 另一种 prompt 为空、上游回 422「请填写提示词」（2026-10-01 实跑踩过）。这里统一摊平。"""
    out = dict(body or {})
    for key in ("input", "params"):
        inner = out.pop(key, None)
        if isinstance(inner, dict):
            for k, v in inner.items():
                out.setdefault(str(k), v)
    return out


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
    env, attempts = None, 0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        attempts = attempt
        try:
            env = gateway.invoke(cap, purpose, str(body.get("account") or "hypit"), params)
        except Exception as e:      # invoke 内部已兜异常；这里只防 resolve 段抛 RouteError
            env = {"ok": False, "result": None, "error": "%s: %s" % (type(e).__name__, e), "_exc": True}
        if env.get("ok"):
            break
        # 上游 503 原文就是"请稍后使用同样的请求重试"（参考图下载繁忙）；生成超时它会退积分 ⇒ 重试不重复收钱
        why = str(env.get("error") or "")
        if attempt == MAX_ATTEMPTS or not _retryable(why):
            break
        time.sleep(RETRY_WAITS[min(attempt - 1, len(RETRY_WAITS) - 1)])
    res = env.get("result") or {}
    credits = (env.get("credits") or {}).get("charged") or 0
    if not env.get("ok"):
        msg = str(env.get("error") or res.get("error") or "生成失败")[:340]
        if isinstance(res, dict) and res.get("job_id"):
            msg += "（上游任务 %s，可据此查它到底出没出片）" % res["job_id"]
        if attempts > 1:
            msg += "（已重试 %d 次）" % (attempts - 1)
        elif any(k in msg for k in NON_RETRYABLE):
            msg += "（内容/身份策略拒绝：不重试，要换参考图或换路由）"
        _touch(jid, state="failed", credits=credits,
               error=json.dumps({"code": "GATEWAY_ERROR" if env.get("_exc") else "GATEWAY_FAILED",
                                 "message": msg}, ensure_ascii=False))
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
    body = _normalize(body if isinstance(body, dict) else {})   # 两种形状都接（见 _normalize）
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


if __name__ == "__main__":
    # studio 主人登记（skill 契约）：起 studio 前 claim、停掉后 release。
    #   python backend/hypit_bridge.py studio-claim <DSH会话id> <run名> [端口]
    #   python backend/hypit_bridge.py studio-release
    #   python backend/hypit_bridge.py studio-status [会话id]
    import sys
    args = sys.argv[1:]
    cmd = args[0] if args else ""
    if cmd == "studio-claim" and len(args) >= 2:
        print(json.dumps(studio_claim(args[1], args[2] if len(args) > 2 else "",
                                      int(args[3]) if len(args) > 3 else STUDIO_PORT), ensure_ascii=False))
    elif cmd == "studio-release":
        studio_release()
        print(json.dumps({"released": True}, ensure_ascii=False))
    elif cmd == "studio-status":
        print(json.dumps(studio_status(args[1] if len(args) > 1 else None), ensure_ascii=False))
    else:
        print(__doc__ or "", "用法见 studio-claim / studio-release / studio-status", file=sys.stderr)
        sys.exit(2)
