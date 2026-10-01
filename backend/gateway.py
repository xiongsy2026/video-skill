"""Weeksir 模型网关（薄层）。

对齐 PRD §4.4：skill/调用方只见「能力名 + purpose」，由本层解析出 provider / model / 凭证，
凭证一律从 config(.env 外置) 读取，源码里不写死、CLI 不回显密钥。

本期(1.0) 落地：能力注册表 + resolve() + 测通 test() + 积分计量桩 meter()。
call() 的能力级参数适配器在 ③「runner 接入网关」时对着真实入口补，避免臆测签名。

导入自任意位置：向上找到含 config.py 的仓库根再加入 sys.path。
"""
from __future__ import annotations

import os as _os
import sys as _sys

_d = _os.path.dirname(_os.path.abspath(__file__))
while _d and not _os.path.exists(_os.path.join(_d, "config.py")):
    _d = _os.path.dirname(_d)
if _d and _d not in _sys.path:
    _sys.path.insert(0, _d)

from config import settings  # noqa: E402

CAPABILITIES = ["text.complete", "image.generate", "image.edit", "video.generate", "video.analyze", "lipsync", "transcribe"]

# (capability, purpose) -> route。model/base_url/api_key 存 settings 的字段名，运行时解析，不落明文。
# provider/client 仅内部与日志用，不出现在客户对话。凭证字段全部映射 config.py（唯一 API 真相源）。
# 注册表=全量目录（供 caps/resolve/meter 用）；call() 适配器渐进接，未接的 purpose 抛 NotImplementedError（安全回滚）。
# ⚠️ client/fn 必须对应 backend/services 下【真实存在】的模块；不确定的一律标 "TODO(...)"，不臆造。
#    实时可用性/模型清单以 app.py:85 的 GET /api/health/models 为权威源（并发探各上游，返回 name/kind/model/status）。
ROUTES = {
    # —— 文本 / 多模态脑（gpt hub 503 抖动、grok degraded → 暂移出；agnes 默认、mimo 备选，二者实测稳）——
    ("text.complete", "agnes"):     {"provider": "agnes",  "client": "openai_compat",      "fn": "chat",                "fields": {"base_url": "AGNES_BASE_URL",   "api_key": "AGNES_API_KEY",   "model": "AGNES_TEXT_MODEL"}},
    ("text.complete", "mimo"):      {"provider": "mimo",   "client": "openai_compat",      "fn": "chat",                "fields": {"base_url": "MIMO_BASE_URL",    "api_key": "MIMO_API_KEY",    "model": None}},
    ("text.complete", "zexapi"):    {"provider": "zexapi", "client": "openai_compat",      "fn": "chat",                "fields": {"base_url": "OMNI_BASE_URL",    "api_key": "OMNI_API_KEY",    "model": None}},
    # —— 看图 / 看视频（多模态理解）——
    ("video.analyze", "default"):   {"provider": "agnes",  "client": "agnes",              "fn": "analyze_media",       "fields": {"base_url": "AGNES_BASE_URL",   "api_key": "AGNES_API_KEY",   "model": "AGNES_TEXT_MODEL"}},
    ("video.analyze", "mimo"):      {"provider": "mimo",   "client": "mimo",               "fn": "analyze_media",       "fields": {"base_url": "MIMO_BASE_URL",    "api_key": "MIMO_API_KEY",    "model": None}},
    # —— 生图 ——
    ("image.generate", "default"):  {"provider": "agnes",    "client": "agnes_image_client",   "fn": "create_image",        "fields": {"base_url": "AGNES_BASE_URL",   "api_key": "AGNES_API_KEY",   "model": "AGNES_IMAGE_MODEL"}},
    ("image.generate", "detail"):   {"provider": "ideogram", "client": "runninghub_client",    "fn": "generate_detail_image", "fields": {"base_url": "RUNNINGHUB_IDEOGRAM4_BASE_URL", "api_key": "RUNNINGHUB_IDEOGRAM4_API_KEY", "model": None}},
    ("image.generate", "generic"):  {"provider": "image2",   "client": "image2_client",        "fn": "create_image",        "fields": {"base_url": "IMAGE2_BASE_URL",  "api_key": "IMAGE2_API_KEY",  "model": None}},
    # zexapi nano_banana（/v1/videos 提交轮询·同 omni 形·复用 OMNI 凭证）
    ("image.generate", "nano"):     {"provider": "zexapi",   "client": "TODO(zexapi /v1/videos,同omni形)", "fn": "submit_image", "fields": {"base_url": "OMNI_BASE_URL",  "api_key": "OMNI_API_KEY",  "model": None}},
    # —— 图片编辑 / 换脸（真实模块 video_face_swap.py）——
    ("image.edit", "faceswap"):     {"provider": "runninghub", "client": "video_face_swap", "fn": "swap_face", "fields": {"base_url": "RUNNINGHUB_IDEOGRAM4_BASE_URL", "api_key": "RUNNINGHUB_ENTERPRISE_API_KEY", "model": None}},
    # —— 生视频 ——
    ("video.generate", "omni-t2v"):      {"provider": "omni",     "client": "omni_client",        "fn": "run_omni",          "fields": {"base_url": "OMNI_BASE_URL",    "api_key": "OMNI_API_KEY",    "model": "OMNI_MODEL_T2V"}},
    # —— Seedance（vinted.cam 直连·/v1/videos·model=seedance2.5/2.0；见 _vinted_video）——
    ("video.generate", "seedance-2.5"):  {"provider": "vinted",   "client": "vinted_video", "fn": "generate", "fields": {"base_url": "VINTED_VIDEO_API_BASE", "api_key": "VINTED_VIDEO_API_KEY", "model": None}},
    ("video.generate", "seedance-2.0"):  {"provider": "vinted",   "client": "vinted_video", "fn": "generate", "fields": {"base_url": "VINTED_VIDEO_API_BASE", "api_key": "VINTED_VIDEO_API_KEY", "model": None}},
    ("video.generate", "agnes-v"):       {"provider": "agnes",    "client": "agnes_video_client", "fn": "create_flash_task", "fields": {"base_url": "AGNES_BASE_URL",   "api_key": "AGNES_API_KEY",   "model": "AGNES_VIDEO_FLASH_MODEL"}},
    ("video.generate", "clone-r2v"):     {"provider": "wan3",     "client": "wan3_client",        "fn": "run_one_segment",   "fields": {"base_url": "RUNNINGHUB_WAN3_BASE_URL", "api_key": "RUNNINGHUB_WAN3_API_KEY", "model": None}},
    ("video.generate", "wan3-dashscope"):{"provider": "dashscope","client": "TODO(无独立client,dashscope待接)", "fn": "generate",        "fields": {"base_url": "DASHSCOPE_BASE_URL", "api_key": "DASHSCOPE_API_KEY", "model": "DASHSCOPE_WAN3_MODEL"}},
    ("video.generate", "h3"):            {"provider": "manxue",   "client": "manxue_h3_client",   "fn": "generate",          "fields": {"base_url": "MANXUE_H3_BASE_URL", "api_key": "MANXUE_H3_API_KEY", "model": "MANXUE_H3_MODEL"}},
    ("video.generate", "motion"):        {"provider": "runninghub","client": "TODO(runninghub_client/工作流,待核)", "fn": "transfer_motion","fields": {"base_url": "RUNNINGHUB_MOTION_WAN22_BASE_URL", "api_key": "RUNNINGHUB_MOTION_WAN22_API_KEY", "model": None}},
    # —— 对口型（RunningHub 工作流；无独立 client，走 runninghub_client，待核）——
    ("lipsync", "default"):         {"provider": "runninghub", "client": "TODO(runninghub_client工作流)", "fn": "lipsync",  "fields": {"base_url": "RUNNINGHUB_IDEOGRAM4_BASE_URL", "api_key": "RUNNINGHUB_LIPSYNC_API_KEY", "model": None}},
    ("lipsync", "vo"):              {"provider": "runninghub", "client": "TODO(runninghub_client工作流)", "fn": "vo_lipsync","fields": {"base_url": "RUNNINGHUB_IDEOGRAM4_BASE_URL", "api_key": "RUNNINGHUB_ENTERPRISE_API_KEY", "model": None}},
    # —— 转写（本地 whisperx，免费；无独立 client，待接）——
    ("transcribe", "whisperx"):     {"provider": "whisperx-local", "client": "TODO(本地whisperx,待接)", "fn": "transcribe", "fields": {"base_url": None, "api_key": None, "model": None}},
}

DEFAULT_PURPOSE = {
    "text.complete": "agnes",
    "image.generate": "default",
    "image.edit": "faceswap",
    "video.generate": "omni-t2v",
    "video.analyze": "default",
    "lipsync": "default",
    "transcribe": "whisperx",
}

# provider 默认模型名（route 未带 model 字段时兜底，避免把 A 家模型名发给 B 家端点）
_MODEL_FALLBACK = {"agnes": "agnes-3.0-flash", "mimo": "mimo-v2.5", "gpt": "gpt-5.6-sol", "openai": "gpt-5.6-sol", "zexapi": "gemini-3.5-flash"}


class RouteError(Exception):
    pass


def _get(name):
    return getattr(settings, name, "") if name else ""


def resolve(capability: str, purpose: str | None = None) -> dict:
    """解析能力(+purpose) → 路由与凭证（凭证从 settings/.env 取，源码无明文）。"""
    if capability not in CAPABILITIES:
        raise RouteError(f"未知能力: {capability}")
    purpose = purpose or default_purpose(capability)
    route = ROUTES.get((capability, purpose))
    if not route:
        raise RouteError(f"{capability} 无 purpose={purpose}；可选: "
                         + ", ".join(p for (c, p) in ROUTES if c == capability))
    f = route["fields"]
    return {
        "capability": capability,
        "purpose": purpose,
        "provider": route["provider"],
        "client": route["client"],
        "fn": route["fn"],
        "base_url": _get(f.get("base_url")),
        "model": _get(f.get("model")) if f.get("model") else None,
        "api_key": _get(f.get("api_key")),          # 内部/适配用；CLI 不回显
        "api_key_field": f.get("api_key"),
    }


def safe_summary(r: dict) -> dict:
    """脱敏摘要：隐去密钥值，只显 provider/model 与 key 是否就绪。"""
    return {
        "capability": r["capability"], "purpose": r["purpose"],
        "provider": r["provider"], "model": r["model"] or "(默认)",
        "base_url": r["base_url"] or "(未配)", "key_present": bool(r["api_key"]),
    }


# ---- 积分价目：按 (capability,purpose) 逐条；视频按 10s 为单位折算 ----
# 相对值占位（可后续调）；账本单一源在 weeksir.com，这里只是"这次调用值多少积分"的价目。
PRICE_ROUTES = {
    ("text.complete", "agnes"): 1, ("text.complete", "mimo"): 1,
    ("video.analyze", "default"): 6, ("video.analyze", "mimo"): 6,
    ("image.generate", "default"): 4, ("image.generate", "detail"): 8, ("image.generate", "generic"): 4, ("image.generate", "nano"): 4,
    ("image.edit", "faceswap"): 20,
    ("video.generate", "omni-t2v"): 40, ("video.generate", "agnes-v"): 30, ("video.generate", "clone-r2v"): 50,
    ("video.generate", "seedance-2.5"): 60, ("video.generate", "seedance-2.0"): 50,
    ("video.generate", "wan3-dashscope"): 50, ("video.generate", "h3"): 60, ("video.generate", "motion"): 50,
    ("lipsync", "default"): 20, ("lipsync", "vo"): 24,
    ("transcribe", "whisperx"): 0,   # 本地免费
}
PRICE_BY_CAP = {"text.complete": 1, "image.generate": 4, "image.edit": 20,
                "video.generate": 40, "video.analyze": 6, "lipsync": 20, "transcribe": 0}

# ---- 可外置配置：价目 + 每能力默认路由 → model_prices.json（改价/换默认供应商=改这个文件，不动代码）----
# 文件按 mtime 懒加载（面板保存后下一次调用即生效）；缺失或损坏则回落上面两张内置表，绝不让管线跑不动。
_PRICE_PATH = _os.environ.get("MODEL_PRICES") or _os.path.join(_d or _os.getcwd(), "model_prices.json")
_ext = {"mtime": None, "prices": {}, "defaults": {}}


def price_path() -> str:
    return _PRICE_PATH


def external_config() -> dict:
    try:
        mt = _os.path.getmtime(_PRICE_PATH)
    except OSError:
        return {"prices": {}, "defaults": {}}
    if _ext["mtime"] != mt:
        prices: dict = {}
        defaults: dict = {}
        try:
            with open(_PRICE_PATH, encoding="utf-8") as f:
                raw = _json.loads(f.read())
            prices = {f"{k}": int(v) for k, v in (raw.get("prices") or {}).items()}
            defaults = {f"{k}": str(v) for k, v in (raw.get("defaults") or {}).items()}
        except Exception as e:
            print(f"[gateway] model_prices.json 未生效（用内置价目）: {type(e).__name__}: {e}")
        _ext.update(prices=prices, defaults=defaults, mtime=mt)
    return _ext


def default_purpose(capability: str) -> str:
    return external_config()["defaults"].get(capability) or DEFAULT_PURPOSE[capability]


def unit_price(capability: str, purpose: str) -> int:
    ext = external_config()["prices"]
    key = f"{capability}/{purpose}"
    if key in ext:
        return ext[key]
    if (capability, purpose) in PRICE_ROUTES:
        return PRICE_ROUTES[(capability, purpose)]
    return PRICE_BY_CAP.get(capability, 1)


def meter(capability: str, purpose: str, params: dict | None = None) -> dict:
    base = unit_price(capability, purpose)
    est = base
    if capability == "video.generate":
        dur = int((params or {}).get("duration", 10))
        est = base * max(1, (dur + 9) // 10)   # 每 10s 一个单位
    return {"capability": capability, "purpose": purpose, "unit_price": base,
            "credits_estimate": est, "ledger": "STUB", "note": "TODO 3.0: weeksir.com 预扣/回滚/流水"}


def _probe_chat(base_url: str, api_key: str, model: str | None, text: str = "ping"):
    """最小真实 chat 调用（文本/视觉脑用）。返回 (status, detail, ms)。"""
    import json as _json
    import time
    import urllib.error
    import urllib.request
    t = time.time()
    payload = {"model": model or "gpt", "messages": [{"role": "user", "content": text}], "max_tokens": 8}
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=_json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            out = _json.loads(r.read().decode("utf-8"))
        c = ((out.get("choices") or [{}])[0].get("message") or {}).get("content")
        return ("live-ok", f"返回 {len(c or '')} 字", round((time.time() - t) * 1000))
    except urllib.error.HTTPError as e:
        return ("live-fail", f"HTTP {e.code}", round((time.time() - t) * 1000))
    except Exception as e:
        return ("unreachable", type(e).__name__, round((time.time() - t) * 1000))


def _probe_reachable(base_url: str, api_key: str):
    """生成类端点：GET /models 探活+鉴权，不提交计费任务。返回 (status, detail, ms)。"""
    import time
    import urllib.error
    import urllib.request
    t = time.time()
    base = base_url.rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    req = urllib.request.Request(base + "/models", headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
        return ("reachable", "端点可达/鉴权通过(未提交生成)", round((time.time() - t) * 1000))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return ("auth-fail", f"HTTP {e.code} 鉴权失败", round((time.time() - t) * 1000))
        return ("reachable", f"HTTP {e.code}(端点可达,非/models风格,未提交生成)", round((time.time() - t) * 1000))
    except Exception as e:
        return ("unreachable", type(e).__name__, round((time.time() - t) * 1000))


# 可安全发最小真实请求验证的能力（便宜）；其余按端点可达/鉴权探测（避免计费生成）。
_LIVE_CHAT = {"text.complete", "video.analyze"}


def test_models(health_url: str = "http://127.0.0.1:8090/api/health/models") -> dict:
    """真·测通：文本/视觉脑发最小真实 chat 请求；生成类做端点可达+鉴权探测（不提交计费任务）。"""
    results = {}
    for (cap, pur) in ROUTES:
        r = resolve(cap, pur)
        key = f"{cap}/{pur}"
        if not r["api_key"] or not r["base_url"]:
            results[key] = {"provider": r["provider"], "model": r["model"] or "(默认)",
                            "status": "no-creds", "detail": "密钥或 base_url 未配置", "ms": None}
            continue
        if cap in _LIVE_CHAT:
            status, detail, ms = _probe_chat(r["base_url"], r["api_key"], r["model"])
        else:
            status, detail, ms = _probe_reachable(r["base_url"], r["api_key"])
        results[key] = {"provider": r["provider"], "model": r["model"] or "(默认)",
                        "status": status, "detail": detail, "ms": ms}
    ok = sum(1 for v in results.values() if v["status"] in ("live-ok", "reachable"))
    return {"routes": results, "summary": f"{ok}/{len(results)} 通", "all_ok": ok == len(results)}


def analyze_media(path: str, question: str = "用一句话描述这个画面的关键信息。",
                  model: str | None = None, base_url: str | None = None,
                  api_key: str | None = None, max_tokens: int = 300) -> dict:
    """看图/看视频 → 返回文字结论（同步，走 openai 兼容 /chat/completions）。

    图片用 image_url、视频用 video_url 的 data URL（agnes-3.0-flash 实测两者都能理解）。
    返回 {"success", "text", "error"}，不抛异常。
    """
    import base64
    import json as _json
    import mimetypes
    import urllib.error
    import urllib.request

    if not base_url or not api_key:
        return {"success": False, "text": None, "error": "analyze 缺少 base_url/api_key（检查 .env 的 AGNES/MIMO 配置）"}
    if not _os.path.isfile(path):
        return {"success": False, "text": None, "error": f"文件不存在: {path}"}

    ext = _os.path.splitext(path)[1].lower()
    is_video = ext in (".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v")
    mime = "video/mp4" if is_video else (mimetypes.guess_type(path)[0] or "image/jpeg")
    b64 = base64.b64encode(open(path, "rb").read()).decode()
    data_url = f"data:{mime};base64,{b64}"
    part = ({"type": "video_url", "video_url": {"url": data_url}} if is_video
            else {"type": "image_url", "image_url": {"url": data_url}})
    payload = {
        "model": model or "agnes-3.0-flash",
        "messages": [{"role": "user", "content": [{"type": "text", "text": question}, part]}],
        "max_tokens": max_tokens,
    }
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=_json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=240) as resp:
            out = _json.loads(resp.read().decode("utf-8"))
        text = ((out.get("choices") or [{}])[0].get("message") or {}).get("content")
        return {"success": bool(text), "text": text, "error": None if text else "上游返回空内容"}
    except urllib.error.HTTPError as e:
        return {"success": False, "text": None, "error": f"HTTP {e.code}: {e.read().decode('utf-8','replace')[:300]}"}
    except Exception as e:  # 连接/超时
        return {"success": False, "text": None, "error": f"{type(e).__name__}: {e}"}


def _chat_compat(base_url: str, api_key: str, model: str, messages: list,
                 temperature: float = 0.7, max_tokens: int = 4000):
    """通用 OpenAI 兼容 /chat/completions 文本调用（agnes/gpt 等皆可）。返回 (text, error)。"""
    import json as _json
    import urllib.error
    import urllib.request
    if not base_url or not api_key:
        return None, "缺少 base_url/api_key（检查 .env 的 AGNES 配置）"
    base = base_url.rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"   # agnes base 已带 /v1；zexapi(OMNI_BASE_URL) 不带，统一补
    payload = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    req = urllib.request.Request(
        base + "/chat/completions",
        data=_json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            out = _json.loads(resp.read().decode("utf-8"))
        text = ((out.get("choices") or [{}])[0].get("message") or {}).get("content")
        return (text, None) if text else (None, "上游返回空内容")
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.read().decode('utf-8','replace')[:200]}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def _vinted_video(base_url, api_key, prompt, model="seedance2.0fast", duration=5, ratio="9:16",
                  resolution="720p", image_urls=None, out_path=None, poll_timeout=900, poll_interval=10):
    """vinted.cam 视频生成（Seedance）。契约见 https://vinted.cam/developers：
    POST /v1/videos→id；GET /v1/videos/{id} status(succeeded/failed)；GET .../signed_url?download=1→url。
    返回 {success, url, path, job_id, error}，不抛。"""
    import json as _json
    import time as _t
    import urllib.error
    import urllib.request
    if not base_url or not api_key:
        return {"success": False, "error": "缺 VINTED_VIDEO_API_BASE/KEY"}
    base = base_url.rstrip("/")

    def _req(url, method="GET", payload=None, timeout=60):
        data = _json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Authorization": "Bearer %s" % api_key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return _json.loads(r.read().decode("utf-8"))

    try:
        body = {"prompt": prompt, "model": model, "duration": int(duration), "ratio": ratio, "resolution": resolution}
        if image_urls:
            body["image_urls"] = image_urls
        sub = _req(base + "/v1/videos", "POST", body)
        job_id = sub.get("id") or sub.get("job_id") or sub.get("task_id")
        if not job_id:
            return {"success": False, "error": "提交未返回 id: %s" % str(sub)[:200]}
        start = _t.time()
        status = None
        while _t.time() - start < poll_timeout:
            st = _req("%s/v1/videos/%s" % (base, job_id))
            status = str(st.get("status", "")).lower()
            if status == "succeeded":
                break
            if status == "failed":
                return {"success": False, "job_id": job_id, "error": "生成失败: %s" % str(st)[:200]}
            _t.sleep(poll_interval)
        else:
            return {"success": False, "job_id": job_id, "error": "轮询超时 %ds" % poll_timeout}
        signed = _req("%s/v1/videos/%s/signed_url?download=1" % (base, job_id))
        url = signed.get("url")
        path = None
        if url and out_path:
            with urllib.request.urlopen(url, timeout=300) as resp, open(out_path, "wb") as f:
                f.write(resp.read())
            path = out_path
        return {"success": bool(url), "url": url, "path": path, "job_id": job_id, "error": None}
    except urllib.error.HTTPError as e:
        return {"success": False, "error": "HTTP %d: %s" % (e.code, e.read().decode("utf-8", "replace")[:200])}
    except Exception as e:
        return {"success": False, "error": "%s: %s" % (type(e).__name__, e)}


def call(capability: str, purpose: str | None = None, **params) -> dict:
    """能力级分发：调用方只给能力名+标准参数，本层按路由注入 model/凭证并转调现有 client。

    本期(③ omni 切片) 仅实现 video.generate/omni-t2v → omni_generate.run_omni；
    其余能力在后续 ③ 切片对着各自 runner 签名补适配器。
    """
    r = resolve(capability, purpose)
    if capability == "video.generate" and r["purpose"] == "omni-t2v":
        from backend.services import omni_generate  # repo 根已由 bootstrap 加入 sys.path
        return omni_generate.run_omni(
            prompt=params["prompt"],
            model=params.get("model") or r["model"],              # 网关集中控制模型
            duration=params.get("duration", 10),
            aspect_ratio=params.get("aspect_ratio", "16:9"),
            output_dir=params.get("output_dir"),
            api_key=params.get("api_key") or r["api_key"],        # 凭证来自 settings/.env
            video_url=params.get("video_url"),
            image_urls=params.get("image_urls"),
        )
    if capability == "video.analyze":
        return analyze_media(
            path=params["path"],
            question=params.get("question") or "用一句话描述这个画面的关键信息。",
            model=r["model"] or _MODEL_FALLBACK.get(r["provider"]), base_url=r["base_url"], api_key=r["api_key"],
            max_tokens=params.get("max_tokens", 300),
        )
    if capability == "text.complete":
        messages = params.get("messages") or [{"role": "user", "content": params["prompt"]}]
        text, err = _chat_compat(
            r["base_url"], r["api_key"], params.get("model") or r["model"] or _MODEL_FALLBACK.get(r["provider"], ""), messages,
            temperature=params.get("temperature", 0.7), max_tokens=params.get("max_tokens", 4000),
        )
        return {"success": bool(text), "text": text, "error": err}
    if capability == "image.generate" and r["purpose"] == "default":
        import asyncio
        from backend.services import agnes_image_client as agnes_img
        data = asyncio.run(agnes_img.create_image(
            params["prompt"],
            size=params.get("size", "1K"),
            ratio=params.get("ratio", "1:1"),
            images=params.get("images"),
        ))
        url = None
        for d in (data.get("data") or []):
            url = d.get("url") or d.get("b64_json")
            if url:
                break
        path = None
        if url and params.get("output_path") and url.startswith("http"):
            path = asyncio.run(agnes_img.download_file(url, params["output_path"]))
        return {"success": bool(url), "url": url, "path": path, "raw": data}
    if capability == "image.generate" and r["purpose"] == "nano":
        # zexapi /v1/videos（同 omni 形），model=nano_banana_pro-2K；复用 omni_client 真码
        from backend.services import omni_client as oc
        sub = oc.submit_omni_task(
            prompt=params["prompt"],
            image_urls=params.get("image_urls") or params.get("images"),
            model=params.get("model") or "nano_banana_pro-2K",
            api_key=r["api_key"],
        )
        final = oc.wait_for_completion(sub["task_id"], timeout=params.get("poll_timeout", 600), api_key=r["api_key"])
        url = final.get("video_url")  # fetch_omni_task 多字段兼容，图 url 落这里
        if final.get("status") != "completed" or not url:
            return {"success": False, "url": url, "task_id": sub["task_id"],
                    "error": final.get("error") or f"status={final.get('status')}"}
        path = None
        if params.get("output_path"):
            path = oc.download_video(url, params["output_path"], api_key=r["api_key"])
        return {"success": True, "url": url, "path": path, "task_id": sub["task_id"]}
    if capability == "video.generate" and r["purpose"].startswith("seedance"):
        model = "seedance2.5" if r["purpose"] == "seedance-2.5" else "seedance2.0"
        return _vinted_video(
            r["base_url"], r["api_key"], params["prompt"], model=model,
            duration=params.get("duration", 5), ratio=params.get("ratio", "9:16"),
            resolution=params.get("resolution", "720p"),
            image_urls=params.get("image_urls") or params.get("images"),
            out_path=params.get("output_path"), poll_timeout=params.get("poll_timeout", 900),
        )
    raise NotImplementedError(
        f"{capability}/{r['purpose']} 适配器待 ③ 后续切片接入（client={r['client']}.{r['fn']}）"
    )


# ---- 积分账本适配层：1.0 本地 sqlite 桩，接口 = weeksir.com 账本（P2 平替，调用方不改）----
# 原则（PRD §6.x）：单一账本、调用前预扣、失败回滚、扣在客户(member)身份；DSH 不另建用户表。
import json as _json
import sqlite3 as _sqlite3
import time as _time
import uuid as _uuid

_LEDGER_PATH = _os.environ.get("GATEWAY_LEDGER") or _os.path.join(_d or _os.getcwd(), ".gateway_ledger.sqlite")
_DEFAULT_BALANCE = int(_os.environ.get("GATEWAY_DEFAULT_BALANCE", "100000"))  # 桩：自用小额度充足


class Ledger:
    """本地积分账本桩。四接口与未来 weeksir.com API 对齐：precheck/preauth/commit/rollback。"""

    def __init__(self, path: str | None = None):
        self.path = path or _LEDGER_PATH
        con = self._con()
        con.executescript(
            "CREATE TABLE IF NOT EXISTS accounts(member TEXT PRIMARY KEY, balance INTEGER);"
            "CREATE TABLE IF NOT EXISTS holds(hold_id TEXT PRIMARY KEY, member TEXT, amount INTEGER, status TEXT);"
            "CREATE TABLE IF NOT EXISTS ledger(member TEXT, delta INTEGER, note TEXT, ts REAL);"
        )
        if "route" not in [r[1] for r in con.execute("PRAGMA table_info(holds)")]:
            con.execute("ALTER TABLE holds ADD COLUMN route TEXT")  # 逐路由审计用；旧行留 NULL
        con.commit(); con.close()

    def _con(self):
        return _sqlite3.connect(self.path)

    def balance(self, member: str) -> int:
        con = self._con()
        row = con.execute("SELECT balance FROM accounts WHERE member=?", (member,)).fetchone()
        if row is None:
            con.execute("INSERT INTO accounts(member,balance) VALUES(?,?)", (member, _DEFAULT_BALANCE))
            con.commit()
            bal = _DEFAULT_BALANCE
        else:
            bal = row[0]
        con.close()
        return bal

    def precheck(self, member: str, need: int) -> bool:
        return self.balance(member) >= need

    def preauth(self, member: str, capability: str, purpose: str, cost: int) -> dict:
        hold_id = _uuid.uuid4().hex[:12]
        con = self._con()
        self.balance(member)  # 确保有行
        con.execute("UPDATE accounts SET balance=balance-? WHERE member=?", (cost, member))
        con.execute("INSERT INTO holds(hold_id,member,amount,status,route) VALUES(?,?,?,'pending',?)",
                    (hold_id, member, cost, f"{capability}/{purpose}"))
        con.execute("INSERT INTO ledger(member,delta,note,ts) VALUES(?,?,?,?)",
                    (member, -cost, f"preauth {capability}/{purpose}", _time.time()))
        con.commit(); con.close()
        return {"hold_id": hold_id, "amount": cost, "balance_after": self.balance(member)}

    def commit(self, hold_id: str, actual: int) -> dict:
        con = self._con()
        row = con.execute("SELECT member,amount,status FROM holds WHERE hold_id=?", (hold_id,)).fetchone()
        if not row or row[2] != "pending":
            con.close(); return {"ok": False, "reason": "hold 不存在或已终结"}
        member, held = row[0], row[1]
        diff = held - actual  # 预扣多了就退差
        if diff:
            con.execute("UPDATE accounts SET balance=balance+? WHERE member=?", (diff, member))
            con.execute("INSERT INTO ledger(member,delta,note,ts) VALUES(?,?,?,?)",
                        (member, diff, f"settle {hold_id}", _time.time()))
        con.execute("UPDATE holds SET status='done', amount=? WHERE hold_id=?", (actual, hold_id))
        con.commit(); con.close()
        return {"ok": True, "charged": actual, "balance_after": self.balance(member)}

    def rollback(self, hold_id: str) -> dict:
        con = self._con()
        row = con.execute("SELECT member,amount,status FROM holds WHERE hold_id=?", (hold_id,)).fetchone()
        if not row or row[2] != "pending":
            con.close(); return {"ok": False, "reason": "hold 不存在或已终结"}
        member, held = row[0], row[1]
        con.execute("UPDATE accounts SET balance=balance+? WHERE member=?", (held, member))
        con.execute("INSERT INTO ledger(member,delta,note,ts) VALUES(?,?,?,?)",
                    (member, held, f"rollback {hold_id}", _time.time()))
        con.execute("UPDATE holds SET status='rolled_back' WHERE hold_id=?", (hold_id,))
        con.commit(); con.close()
        return {"ok": True, "refunded": held, "balance_after": self.balance(member)}

    # ---- 只读视图（给管理面板做"余额/流水/调用审计"）----
    def accounts(self) -> list:
        con = self._con()
        rows = con.execute("SELECT member,balance FROM accounts ORDER BY member").fetchall()
        con.close()
        return [{"member": m, "balance": b} for m, b in rows]

    def audit(self, limit: int = 30) -> dict:
        """按 路由×状态 聚合（done=真扣了这些分、rolled_back=已退），加最近流水。"""
        con = self._con()
        by_route = [{"route": r or "(旧记录未记路由)", "status": s, "calls": n, "credits": c}
                    for r, s, n, c in con.execute(
                        "SELECT route,status,COUNT(*),SUM(amount) FROM holds GROUP BY route,status"
                        " ORDER BY route,status")]
        recent = [{"hold_id": h, "member": m, "route": r or "(旧)", "amount": a, "status": s}
                  for h, m, r, a, s in con.execute(
                      "SELECT hold_id,member,route,amount,status FROM holds ORDER BY rowid DESC LIMIT ?", (limit,))]
        flow = [{"member": m, "delta": d, "note": n, "ts": t} for m, d, n, t in con.execute(
            "SELECT member,delta,note,ts FROM ledger ORDER BY ts DESC LIMIT ?", (limit,))]
        con.close()
        return {"by_route": by_route, "recent_holds": recent, "flow": flow}


_ledger = None


def ledger() -> Ledger:
    global _ledger
    if _ledger is None:
        _ledger = Ledger()
    return _ledger


def invoke(capability: str, purpose: str | None = None, account: str = "local",
           params: dict | None = None) -> dict:
    """skill 经 tool-jobs 调的总入口：resolve→估价→precheck/preauth→call→commit/rollback→信封。
    不足即停并提示充值（不发起生成，PRD:284）；失败回滚该条（PRD:346）。"""
    params = params or {}
    account = account or "local"
    r = resolve(capability, purpose)
    est = meter(capability, r["purpose"], params)["credits_estimate"]
    if not ledger().precheck(account, est):
        return {"ok": False, "capability": capability, "purpose": r["purpose"], "result": None,
                "credits": {"estimated": est, "charged": 0, "balance_after": ledger().balance(account)},
                "error": "积分不足，请去 weeksir.com 充值后重试"}
    hold = ledger().preauth(account, capability, r["purpose"], est)
    try:
        result = call(capability, r["purpose"], **params)
        # 适配器返回业务失败（success:False / 带 error）也算失败 → 回滚，不扣费
        if isinstance(result, dict) and (result.get("success") is False or result.get("error")):
            ledger().rollback(hold["hold_id"])
            return {"ok": False, "capability": capability, "purpose": r["purpose"], "result": result,
                    "credits": {"estimated": est, "charged": 0, "hold_id": hold["hold_id"],
                                "balance_after": ledger().balance(account)},
                    "error": result.get("error") or "生成失败"}
        charged = est  # P0 按预估扣；3.0 用实际消耗回写
        done = ledger().commit(hold["hold_id"], charged)
        return {"ok": True, "capability": capability, "purpose": r["purpose"], "result": result,
                "credits": {"estimated": est, "charged": charged, "hold_id": hold["hold_id"],
                            "balance_after": done.get("balance_after", ledger().balance(account))},
                "error": None}
    except Exception as e:
        ledger().rollback(hold["hold_id"])
        return {"ok": False, "capability": capability, "purpose": r["purpose"], "result": None,
                "credits": {"estimated": est, "charged": 0, "hold_id": hold["hold_id"],
                            "balance_after": ledger().balance(account)},
                "error": f"{type(e).__name__}: {e}"}


def _cli(argv):
    cmd = argv[0] if argv else "caps"
    if cmd == "caps":
        for cap in CAPABILITIES:
            purposes = ", ".join(p for (c, p) in ROUTES if c == cap)
            print(f"{cap:16} purposes: {purposes}")
    elif cmd == "resolve":
        cap = argv[1]
        pur = argv[2] if len(argv) > 2 else None
        print(safe_summary(resolve(cap, pur)))
    elif cmd == "prices":
        cfg = external_config()
        print(f"价目源: {price_path()}  →  外置 {len(cfg['prices'])} 条 / 默认路由 {len(cfg['defaults'])} 条"
              + ("" if cfg["prices"] else "（文件缺失或损坏，当前用内置兜底表）"))
        for cap in CAPABILITIES:
            dft = cfg["defaults"].get(cap) or DEFAULT_PURPOSE.get(cap)
            for (c, p) in ROUTES:
                if c == cap:
                    print(f"  {'★' if p == dft else ' '} {cap}/{p:<14} {unit_price(cap, p):>4} 分")
    elif cmd == "credits":
        led = ledger()
        print("余额:", _json.dumps(led.accounts(), ensure_ascii=False))
        for r in led.audit()["by_route"]:
            print(f"  {r['route']:<30} {r['status']:<12} {r['calls']:>3} 次 {r['credits']:>6} 分")
    elif cmd == "test":
        res = test_models()
        print(f"真·测通结果  {res['summary']}")
        for key, v in res["routes"].items():
            ms = f"{v['ms']}ms" if v["ms"] is not None else "-"
            print(f"  {v['status']:<11} {key:<26} {v['provider']}/{v['model']:<22} {ms:>8}  {v['detail']}")
    elif cmd == "analyze":
        # 用法: python gateway.py analyze <图片/视频路径> [问题...]
        import json
        if len(argv) < 2:
            print("用法: python gateway.py analyze <媒体路径> [问题]")
            return 2
        path = argv[1]
        q = " ".join(argv[2:]) or "用一句话描述这个画面的关键信息。"
        print(json.dumps(call("video.analyze", path=path, question=q), ensure_ascii=False, indent=2))
    elif cmd == "call":
        # 用法: python gateway.py call <capability> [purpose] [--account <member>] [--json '<params>']
        # skill 经 tool-jobs 调模型的唯一入口；返回归一 JSON（含 credits）。
        import json
        if len(argv) < 2:
            print("用法: python gateway.py call <cap> [purpose] [--account X] [--json '{...}']")
            return 2
        cap = argv[1]
        rest = argv[2:]
        pur = None
        if rest and not rest[0].startswith("--"):
            pur, rest = rest[0], rest[1:]
        account, params = "local", {}
        i = 0
        while i < len(rest):
            if rest[i] == "--account" and i + 1 < len(rest):
                account = rest[i + 1]; i += 2
            elif rest[i] == "--json" and i + 1 < len(rest):
                params = json.loads(rest[i + 1]); i += 2
            else:
                i += 1
        print(json.dumps(invoke(cap, pur, account, params), ensure_ascii=False, indent=2))
    else:
        print("用法: python gateway.py [caps | resolve <cap> [purpose] | call <cap> [purpose] "
              "[--account X] [--json '{...}'] | test | analyze <path> [问题]]")


if __name__ == "__main__":
    _cli(_sys.argv[1:])
