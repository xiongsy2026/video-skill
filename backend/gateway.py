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
import threading as _threading

_d = _os.path.dirname(_os.path.abspath(__file__))
while _d and not _os.path.exists(_os.path.join(_d, "config.py")):
    _d = _os.path.dirname(_d)
if _d and _d not in _sys.path:
    _sys.path.insert(0, _d)

from config import settings  # noqa: E402

CAPABILITIES = ["text.complete", "image.generate", "image.edit", "video.generate", "video.analyze", "lipsync", "transcribe", "audio.generate"]

# (capability, purpose) -> route。model/base_url/api_key 存 settings 的字段名，运行时解析，不落明文。
# provider/client 仅内部与日志用，不出现在客户对话。凭证字段全部映射 config.py（唯一 API 真相源）。
# 注册表=全量目录（供 caps/resolve/meter 用）；call() 适配器渐进接，未接的 purpose 抛 NotImplementedError（安全回滚）。
# ⚠️ client/fn 必须对应 backend/services 下【真实存在】的模块；不确定的一律标 "TODO(...)"，不臆造。
#    实时可用性/模型清单以 app.py:85 的 GET /api/health/models 为权威源（并发探各上游，返回 name/kind/model/status）。
ROUTES = {
    # —— 文本 / 多模态脑（gpt hub 503 抖动、grok degraded → 暂移出；agnes 默认、mimo 备选，二者实测稳）——
    ("text.complete", "agnes"):     {"provider": "agnes",  "client": "openai_compat",      "fn": "chat",                "fields": {"base_url": "AGNES_BASE_URL",   "api_key": "AGNES_API_KEY",   "model": "AGNES_TEXT_MODEL"}},
    ("text.complete", "mimo"):      {"provider": "mimo",   "client": "openai_compat",      "fn": "chat",                "fields": {"base_url": "MIMO_BASE_URL",    "api_key": "MIMO_API_KEY",    "model": None}},
    # 2026-10-04 删掉两条：("text.complete","zexapi") 与 ("text.complete","qwen38max")
    # —— 连测 3 轮全不通（zexapi 聊天分组无可用渠道 503；阿里百炼整把 key 欠费 Arrearage，管理台早已屏蔽）。
    # 留着只会让人以为"换条 purpose 就能兜底"。欠费/无渠道修好后要加回来，先看 ADR 的下线记录。
    # —— 2026-10-04 定：gpt 系【只认 hub.nocannobb】，下面两条就是全部 gpt 腿。
    # —— 2026-10-01 全中转实测后补的推理脑（模型名集中在 config，.env 可覆盖）——
    ("text.complete", "gemini38"):  {"provider": "zexapi",     "client": "openai_compat", "fn": "chat", "fields": {"base_url": "OMNI_BASE_URL",           "api_key": "OMNI_API_KEY",    "model": "TEXT_MODEL_GEMINI38"}},
    ("text.complete", "claude48"):  {"provider": "zexapi",     "client": "openai_compat", "fn": "chat", "fields": {"base_url": "OMNI_BASE_URL",           "api_key": "OMNI_API_KEY",    "model": "TEXT_MODEL_CLAUDE48"}},
    ("text.complete", "deepseekv4"):{"provider": "zexapi",     "client": "openai_compat", "fn": "chat", "fields": {"base_url": "OMNI_BASE_URL",           "api_key": "OMNI_API_KEY",    "model": "TEXT_MODEL_DEEPSEEKV4"}},
    ("text.complete", "gpt61"):     {"provider": "nocannobb",  "client": "openai_compat", "fn": "chat", "fields": {"base_url": "GPT55_BASE_URL",          "api_key": "GPT55_API_KEY",   "model": "TEXT_MODEL_GPT61"}},
    # 同一个 hub 的第二把 key 走这条（2026-10-04）：不复用 GPT55_API_KEY，免得换密钥把 gpt61 那条弄死
    ("text.complete", "gpt56"):     {"provider": "nocannobb",  "client": "openai_compat", "fn": "chat", "fields": {"base_url": "GPT55_BASE_URL",          "api_key": "GPT56_API_KEY",   "model": "TEXT_MODEL_GPT56"}},
    ("text.complete", "mimo_pro"):  {"provider": "mimo",       "client": "openai_compat", "fn": "chat", "fields": {"base_url": "MIMO_BASE_URL",           "api_key": "MIMO_API_KEY",    "model": "TEXT_MODEL_MIMO_PRO"}},
    # —— 看图 / 看视频（多模态理解）——
    ("video.analyze", "default"):   {"provider": "agnes",  "client": "agnes",              "fn": "analyze_media",       "fields": {"base_url": "AGNES_BASE_URL",   "api_key": "AGNES_API_KEY",   "model": "AGNES_TEXT_MODEL"}},
    ("video.analyze", "mimo"):      {"provider": "mimo",   "client": "mimo",               "fn": "analyze_media",       "fields": {"base_url": "MIMO_BASE_URL",    "api_key": "MIMO_API_KEY",    "model": None}},
    # —— 生图 ——
    ("image.generate", "default"):  {"provider": "agnes",    "client": "agnes_image_client",   "fn": "create_image",        "fields": {"base_url": "AGNES_BASE_URL",   "api_key": "AGNES_API_KEY",   "model": "AGNES_IMAGE_MODEL"}},
    # ⚠️ detail 是【流水线级】(detail_image_agent.run_pipeline: LLM 分场景 + 双引擎 + task_id 状态机，8090 的
    #    /api/detail-image 正在跑它)；要接网关得先把里面的单模型原语剥出来，别把编排搬第二遍。
    ("image.generate", "detail"):   {"provider": "ideogram", "client": "detail_image_agent",  "fn": "run_pipeline(流水线级)", "fields": {"base_url": "RUNNINGHUB_IDEOGRAM4_BASE_URL", "api_key": "RUNNINGHUB_IDEOGRAM4_API_KEY", "model": None}},
    ("image.generate", "generic"):  {"provider": "image2",   "client": "image2_client",        "fn": "Image2Client.text_to_image_sync", "fields": {"base_url": "IMAGE2_BASE_URL",  "api_key": "IMAGE2_API_KEY",  "model": None}},
    # zexapi nano_banana（/v1/videos 提交轮询·同 omni 形·复用 OMNI 凭证）——已实现，见 call() 的 nano 分支
    ("image.generate", "nano"):     {"provider": "zexapi",   "client": "omni_client", "fn": "submit_omni_task(model=nano_banana_pro-2K)", "fields": {"base_url": "OMNI_BASE_URL",  "api_key": "OMNI_API_KEY",  "model": None}},
    # —— 网页/CLI 型生图（没有 api_key，靠登录态；网关仍然预扣/结算/审计，只是这一条腿走子进程）——
    # doubao: subprocess 调 video-ui 的 `doubao.py gen`（Playwright 驱动本机 Chrome）；gflow: `gflow image t2i|i2i --json`
    ("image.generate", "doubao"):   {"provider": "doubao-web", "client": "VIDEO_UI_ROOT/doubao.py", "fn": "gen --ref --out --model",
                                     "fields": {"base_url": None, "api_key": None, "model": "DOUBAO_IMAGE_MODEL"}},
    ("image.generate", "gflow"):    {"provider": "gflow-cli", "client": "GFLOW_EXE", "fn": "image t2i|i2i --json",
                                     "fields": {"base_url": None, "api_key": None, "model": "GFLOW_IMAGE_MODEL"}},
    # —— 图片编辑 / 换脸（真实入口 video_face_swap.run_face_swap:53，分段+on_log，也是流水线级）——
    ("image.edit", "faceswap"):     {"provider": "runninghub", "client": "video_face_swap", "fn": "run_face_swap", "fields": {"base_url": "RUNNINGHUB_IDEOGRAM4_BASE_URL", "api_key": "RUNNINGHUB_ENTERPRISE_API_KEY", "model": None}},
    # —— 生视频 ——
    ("video.generate", "omni-t2v"):      {"provider": "omni",     "client": "omni_generate",      "fn": "run_omni",          "fields": {"base_url": "OMNI_BASE_URL",    "api_key": "OMNI_API_KEY",    "model": "OMNI_MODEL_T2V"}},
    # —— Seedance（vinted.cam 直连·/v1/videos·model=seedance2.5/2.0；见 _vinted_video）——
    ("video.generate", "seedance-2.5"):  {"provider": "vinted",   "client": "vinted_video", "fn": "generate", "fields": {"base_url": "VINTED_VIDEO_API_BASE", "api_key": "VINTED_VIDEO_API_KEY", "model": None}},
    ("video.generate", "seedance-2.0"):  {"provider": "vinted",   "client": "vinted_video", "fn": "generate", "fields": {"base_url": "VINTED_VIDEO_API_BASE", "api_key": "VINTED_VIDEO_API_KEY", "model": None}},
    ("video.generate", "agnes-v"):       {"provider": "agnes",    "client": "agnes_video_client", "fn": "create_flash_task", "fields": {"base_url": "AGNES_BASE_URL",   "api_key": "AGNES_API_KEY",   "model": "AGNES_VIDEO_FLASH_MODEL"}},
    ("video.generate", "clone-r2v"):     {"provider": "wan3",     "client": "wan3_client",        "fn": "run_one_segment",   "fields": {"base_url": "RUNNINGHUB_WAN3_BASE_URL", "api_key": "RUNNINGHUB_WAN3_API_KEY", "model": None}},
    ("video.generate", "wan3-dashscope"):{"provider": "dashscope","client": "TODO(无现成 client·要照 dashscope 视频生成协议新写)", "fn": "-", "fields": {"base_url": "DASHSCOPE_BASE_URL", "api_key": "DASHSCOPE_API_KEY", "model": "DASHSCOPE_WAN3_MODEL"}},
    ("video.generate", "h3"):            {"provider": "runninghub", "client": "h3_director_client", "fn": "submit_h3_director+poll_and_download", "fields": {"base_url": "RUNNINGHUB_H3_BASE_URL", "api_key": "RUNNINGHUB_ENTERPRISE_API_KEY", "model": None}},
    ("video.generate", "motion"):        {"provider": "runninghub","client": "runninghub_client", "fn": "submit_motion_transfer+poll_task_until_done","fields": {"base_url": "RUNNINGHUB_MOTION_WAN22_BASE_URL", "api_key": "RUNNINGHUB_MOTION_WAN22_API_KEY", "model": None}},
    # —— 对口型（真实码在流水线模块里，非独立 client；run_lip_sync 吃本地路径、无 api_key 形参=流水线级）——
    ("lipsync", "default"):         {"provider": "runninghub", "client": "video_translate_pipeline", "fn": "run_lip_sync",  "fields": {"base_url": "RUNNINGHUB_IDEOGRAM4_BASE_URL", "api_key": "RUNNINGHUB_LIPSYNC_API_KEY", "model": None}},
    ("lipsync", "vo"):              {"provider": "runninghub", "client": "voiceover_replace", "fn": "_run_lipsync_workflow(私有,接前先提公开入口)","fields": {"base_url": "RUNNINGHUB_IDEOGRAM4_BASE_URL", "api_key": "RUNNINGHUB_ENTERPRISE_API_KEY", "model": None}},
    # —— 转写（本地 ASR，免费，零密钥：FunASR paraformer-zh → faster_whisper 回退；仓库里没有 whisperx）——
    ("transcribe", "asr"):          {"provider": "local-asr", "client": "video_translate_pipeline", "fn": "run_asr", "fields": {"base_url": None, "api_key": None, "model": None}},
    # —— 音频生成（火山引擎·豆包语音 seed-audio：一条请求同时出【音效+配乐+多角色配音】）——
    # 鉴权是 X-Api-Key 头（不是 Bearer）；成功回二进制音频体 ⇒ 必须给 output_path 落盘，别只回链接。
    ("audio.generate", "seed-audio"): {"provider": "bytedance-audio", "client": "gateway._seed_audio", "fn": "POST /api/v3/tts/create",
                                       "fields": {"base_url": "BYTEDANCE_AUDIO_BASE_URL", "api_key": "BYTEDANCE_AUDIO_API_KEY", "model": "BYTEDANCE_AUDIO_MODEL"}},
}

DEFAULT_PURPOSE = {
    "text.complete": "agnes",
    "image.generate": "default",
    "image.edit": "faceswap",
    "video.generate": "omni-t2v",
    "video.analyze": "default",
    "lipsync": "default",
    "transcribe": "asr",
    "audio.generate": "seed-audio",
}

# provider 默认模型名（route 未带 model 字段时兜底，避免把 A 家模型名发给 B 家端点）
# 2026-10-04 清掉三个死条目：gpt / openai / manxue —— 没有任何路由再用这些 provider，
# 留着等于宣称"有兜底"，实际一调就 RouteError。（gpt 系现在只有 nocannobb 两条腿。）
_MODEL_FALLBACK = {"agnes": "agnes-3.0-flash", "mimo": "mimo-v2.5", "zexapi": "gemini-3.5-flash",
                   "nocannobb": "gpt-6.1-sol", "dashscope": "qwen3.8-max"}

# ⚠️ 必须带浏览器 User-Agent：hub.nocannobb.com 这类 Cloudflare 前置的中转对无 UA 的脚本请求回
#    403 error code: 1010（带 UA 即 200）。之前我把这个当成"key 坏了"，是误判（见 ADR）。
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


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
    if is_disabled(capability, purpose):
        why = external_config()["disabled"].get(f"{capability}/{purpose}", "")
        raise RouteError(f"{capability}/{purpose} 已在管理台屏蔽"
                         + (f"（{why}）" if why else "")
                         + "；换一条 purpose，或去 8100 面板重新启用")
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
    # 推理脑按 2026-10-01 实测 token 量分档（约 100 tok ≈ 1 分；思考型模型预算要留足）
    ("text.complete", "gpt61"): 1, ("text.complete", "claude48"): 2,
    # gpt-5.6-sol 这条：整条单价只当报价，真扣按 token 结算（同族同档取 1 分）
    ("text.complete", "gpt56"): 1,
    ("text.complete", "mimo_pro"): 3, ("text.complete", "gemini38"): 6, ("text.complete", "deepseekv4"): 7,
    ("video.analyze", "default"): 6, ("video.analyze", "mimo"): 6,
    ("image.generate", "default"): 4, ("image.generate", "detail"): 8, ("image.generate", "generic"): 4, ("image.generate", "nano"): 4,
    # 网页/CLI 两条腿：我方账本先记 0（不谎报扣费）。豆包网页版不花钱；gflow 烧的是 Google Flow 额度，
    # 那是【另一个账户的配额】不是我方现金 ⇒ 等"1 元=几分"定了再一起补价。
    ("image.generate", "doubao"): 0, ("image.generate", "gflow"): 0,
    ("image.edit", "faceswap"): 20,
    ("video.generate", "omni-t2v"): 40, ("video.generate", "agnes-v"): 30, ("video.generate", "clone-r2v"): 50,
    ("video.generate", "seedance-2.5"): 60, ("video.generate", "seedance-2.0"): 50,
    ("video.generate", "wan3-dashscope"): 50, ("video.generate", "h3"): 60, ("video.generate", "motion"): 50,
    ("lipsync", "default"): 20, ("lipsync", "vo"): 24,
    ("transcribe", "asr"): 0,   # 本地 ASR 免费
    # ⚠️ 音频这条是【分/千字】不是分/条（上游按字符计费）。0 = 还没实测单价、也没定"1 元=几分"，
    #    所以先记 0 分：能跑通链路、不谎报扣费。定价后改 model_prices.json 的这一个数即可，不用动代码。
    ("audio.generate", "seed-audio"): 0,
}
PRICE_BY_CAP = {"text.complete": 1, "image.generate": 4, "image.edit": 20,
                "video.generate": 40, "video.analyze": 6, "lipsync": 20, "transcribe": 0,
                "audio.generate": 0}

# ---- 可外置配置：价目 + 每能力默认路由 → model_prices.json（改价/换默认供应商=改这个文件，不动代码）----
# 文件按 mtime 懒加载（面板保存后下一次调用即生效）；缺失或损坏则回落上面两张内置表，绝不让管线跑不动。
_PRICE_PATH = _os.environ.get("MODEL_PRICES") or _os.path.join(_d or _os.getcwd(), "model_prices.json")
_ext = {"mtime": None, "prices": {}, "defaults": {}, "disabled": {}, "token_rate": {}}


def price_path() -> str:
    return _PRICE_PATH


def external_config() -> dict:
    try:
        mt = _os.path.getmtime(_PRICE_PATH)
    except OSError:
        return {"prices": {}, "defaults": {}, "disabled": {}, "token_rate": {}}
    if _ext["mtime"] != mt:
        prices: dict = {}
        defaults: dict = {}
        disabled: dict = {}
        rates: dict = {}
        try:
            with open(_PRICE_PATH, encoding="utf-8") as f:
                raw = _json.loads(f.read())
            prices = {f"{k}": int(v) for k, v in (raw.get("prices") or {}).items()}
            defaults = {f"{k}": str(v) for k, v in (raw.get("defaults") or {}).items()}
            d = raw.get("disabled") or []
            if isinstance(d, dict):
                disabled = {str(k): str(v or "") for k, v in d.items()}
            else:                                    # 也接受 ["cap/pur", ...] 手写形式
                disabled = {str(k): "" for k in d}
            rates = {str(k): float(v) for k, v in (raw.get("token_rate") or {}).items()}
        except Exception as e:
            print(f"[gateway] model_prices.json 未生效（用内置价目）: {type(e).__name__}: {e}")
        _ext.update(prices=prices, defaults=defaults, disabled=disabled, token_rate=rates, mtime=mt)
    return _ext


def is_disabled(capability: str, purpose: str) -> bool:
    return f"{capability}/{purpose}" in external_config()["disabled"]


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

# 分 / 1k token（口径 = 1 分约 100 token）。思考型模型同一问题的消耗实测波动 100 倍以上
# （28 → 4396，gpt-5.6-sol 22366，见 ADR），所以整条单价只能当"粗略预估/报价"，真实扣费按上游 usage 结算；
# 没有费率的能力（生图/生视频，本来就按条计费）继续按条计。
TOKEN_RATE_DEFAULT = {"text.complete": 10.0, "video.analyze": 10.0}


def token_rate(capability: str):
    return external_config()["token_rate"].get(capability) or TOKEN_RATE_DEFAULT.get(capability)


def settle_credits(capability: str, purpose: str, result, estimated: int) -> tuple:
    """按上游 usage 算实扣。返回 (charged, tokens, basis)。
    basis= usage 按 token 结算 / usage-capped 超护栏被截 / flat 无 usage 或不按 token 计。"""
    tokens = 0
    if isinstance(result, dict):
        usage = result.get("usage") or {}
        try:
            tokens = int(usage.get("total_tokens") or result.get("tokens") or 0)
        except (TypeError, ValueError):
            tokens = 0
    rate = token_rate(capability)
    if not rate or tokens <= 0:
        return estimated, tokens, "flat"
    charged = max(1, int(round(tokens / 1000.0 * rate)))
    guard = max(estimated * 8, 200)  # 护栏：单条封顶（≈20k token），防上游跑飞把客户积分一次掏空
    if charged > guard:
        return guard, tokens, "usage-capped"
    return charged, tokens, "usage"


def meter(capability: str, purpose: str, params: dict | None = None) -> dict:
    base = unit_price(capability, purpose)
    est = base
    if capability == "video.generate":
        dur = int((params or {}).get("duration", 10))
        est = base * max(1, (dur + 9) // 10)   # 每 10s 一个单位
    if capability == "audio.generate":
        # 上游按字符计费 ⇒ 单价是"分/千字"，一条请求向下取整到 1 个千字单位（100 字也按 1 千字算）
        text = str((params or {}).get("prompt") or (params or {}).get("text_prompt") or "")
        est = base * max(1, -(-len(text) // 1000))
    return {"capability": capability, "purpose": purpose, "unit_price": base,
            "credits_estimate": est, "ledger": "STUB", "note": "TODO 3.0: weeksir.com 预扣/回滚/流水"}


def _openai_v1(base_url: str) -> str:
    """OpenAI 兼容端点统一补 /v1（agnes base 自带、zexapi/OMNI 不带，之前漏补导致 405）。"""
    b = str(base_url).rstrip("/")
    return b if b.endswith("/v1") else b + "/v1"


# 上游 5xx（502/503/504）= 中转自己抖动或无渠道，不是我们 key/配置坏：单独归一档，别混进"需要处理"。
# 4xx（401/403/400/404）才是"只有你能修的事"（欠费/未开通/模型名错/路径错）→ 保留 live-fail 让面板报警。
def _fail_status(code: int) -> str:
    return "upstream-5xx" if code and code >= 500 else "live-fail"


def _probe_chat(base_url: str, api_key: str, model: str | None, text: str = "ping"):
    """最小真实 chat 调用（文本/视觉脑用）。返回 (status, detail, ms)。
    ⚠️ model 必须已解析出真名：发 null 会被上游当 "unknown-model" 判 400（之前 mimo 的假故障就这么来的）。"""
    import json as _json
    import time
    import urllib.error
    import urllib.request
    t = time.time()
    # max_tokens 给足：agnes/mimo 这类会先烧"思考"token，只给 8 会返回空内容、把坏配置探成 live-ok
    payload = {"model": model, "messages": [{"role": "user", "content": text}], "max_tokens": 64}
    req = urllib.request.Request(
        _openai_v1(base_url) + "/chat/completions",
        data=_json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "User-Agent": BROWSER_UA},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            out = _json.loads(r.read().decode("utf-8"))
        c = ((out.get("choices") or [{}])[0].get("message") or {}).get("content")
        if not (c or "").strip():
            return ("empty-response", "HTTP 200 但 content 空（预算太小或该模型不宜当文本脑）", round((time.time() - t) * 1000))
        return ("live-ok", f"返回 {len(c)} 字", round((time.time() - t) * 1000))
    except urllib.error.HTTPError as e:
        return (_fail_status(e.code), f"HTTP {e.code}", round((time.time() - t) * 1000))
    except Exception as e:
        return ("unreachable", type(e).__name__, round((time.time() - t) * 1000))


def _probe_reachable(base_url: str, api_key: str):
    """生成类端点：探 /models 或 /v1/models 做鉴权，不提交计费任务。
    ⚠️ 别只会拼 /v1/models：RunningHub 这类非 OpenAI 风格网关，/models=200 而 /v1/models=401
       ——之前据此把 wan3/faceswap/motion/detail 全判成"鉴权失败"，是假故障。"""
    import time
    import urllib.error
    import urllib.request
    t = time.time()
    base = str(base_url).rstrip("/")
    last = None
    for path in ("/models", "/v1/models"):
        ms = round((time.time() - t) * 1000)
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    base + path, headers={"Authorization": f"Bearer {api_key}", "User-Agent": BROWSER_UA}), timeout=15) as r:
                r.read()
            return ("reachable", f"GET {path} 鉴权通过(未提交生成)", ms)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                last = f"GET {path} 404"
                continue
            if e.code in (401, 403):
                return ("auth-fail", f"GET {path} HTTP {e.code} 鉴权失败", ms)
            if e.code and e.code >= 500:
                return ("upstream-5xx", f"GET {path} HTTP {e.code} 上游抖动(非我方配置)", ms)
            return ("reachable", f"GET {path} HTTP {e.code}(可达,未提交生成)", ms)
        except Exception as e:
            return ("unreachable", f"GET {path} {type(e).__name__}", ms)
    return ("no-probe", f"两种 /models 风格都 404（{last}）：非 OpenAI 风格，只能实跑才能验", round((time.time() - t) * 1000))


# 可安全发最小真实请求验证的能力（便宜）；其余按端点可达/鉴权探测（避免计费生成）。
_LIVE_CHAT = {"text.complete", "video.analyze"}

# call() 里已接出适配器的路由（purpose="*" 表示该能力下所有 purpose 都走同一分支）。
# ⚠️ 改 call() 的分支时同步这张表；管理面板用它回答"这条能不能直接调"。
_ADAPTED = {("text.complete", "*"), ("video.analyze", "*"),
            ("image.generate", "default"), ("image.generate", "nano"),
            ("video.generate", "omni-t2v"), ("video.generate", "seedance-2.5"),
            ("video.generate", "seedance-2.0"), ("video.generate", "agnes-v"),
            ("video.generate", "h3"), ("audio.generate", "seed-audio"),
            ("image.generate", "doubao"), ("image.generate", "gflow")}


def is_adapted(capability: str, purpose: str) -> bool:
    return (capability, "*") in _ADAPTED or (capability, purpose) in _ADAPTED


def test_route(capability: str, purpose: str) -> dict:
    """单条路由探活（不提交计费任务）。返回 {provider, model, status, detail, ms}。
    ⚠️ 已屏蔽/未知 purpose 会让 resolve 抛 RouteError：这里必须吃掉，否则并发探活整批炸掉。"""
    try:
        r = resolve(capability, purpose)
    except RouteError as e:
        known = (capability, purpose) in ROUTES
        provider = ROUTES.get((capability, purpose), {}).get("provider", "?")
        return {"provider": provider, "model": "-",
                "status": "disabled" if known and is_disabled(capability, purpose) else "config-error",
                "detail": str(e), "ms": None}
    if r["provider"].startswith("local"):
        return {"provider": r["provider"], "model": r["model"] or "(本地)", "status": "local",
                "detail": "本地引擎·无需密钥，不在 HTTP 探活范围（要用得实跑）", "ms": None}
    if r["provider"] == "gflow-cli":
        # 网页/CLI 型腿没有密钥可验，能验的是【登录态】。auth status 是 gflow 自己的只读检查
        # （不开浏览器、不烧额度），video-ui 的状态接口用的就是它。
        exe = _get("GFLOW_EXE") or ""
        if not _os.path.isfile(exe):
            return {"provider": r["provider"], "model": r["model"] or "(默认)", "status": "config-error",
                    "detail": "GFLOW_EXE 不存在：%s（没装就 pip install gflow-cli，或把路径配进 .env）" % exe, "ms": None}
        code, so, se, why = _run_cli(exe, ["auth", "status"], 30.0)
        if why:
            return {"provider": r["provider"], "model": r["model"] or "(默认)", "status": "unreachable",
                    "detail": why, "ms": None}
        return {"provider": r["provider"], "model": r["model"] or "(默认)",
                "status": "live-ok" if code == 0 else "auth-fail",
                "detail": ("Google 会话在（auth status 0）" if code == 0
                           else "没登录：跑 gflow auth login") + " | " + _tail(so or se, n=160), "ms": None}
    if r["provider"] == "doubao-web":
        py, root = _get("DOUBAO_PYTHON"), _get("VIDEO_UI_ROOT")
        script, state = _os.path.join(root or "", "doubao.py"), _os.path.join(root or "", "doubao_state.json")
        miss = [n for n, p in (("DOUBAO_PYTHON", py), ("doubao.py", script)) if not (p and _os.path.isfile(p))]
        if miss:
            return {"provider": r["provider"], "model": r["model"] or "(默认)", "status": "config-error",
                    "detail": "缺 %s（网页腿要装 playwright 的那个解释器 + video-ui 的 doubao.py）" % "/".join(miss),
                    "ms": None}
        code, so, se, why = _run_cli(py, ["-c", "import playwright"], 60.0)
        if code != 0:
            return {"provider": r["provider"], "model": r["model"] or "(默认)", "status": "config-error",
                    "detail": "那个解释器 import playwright 失败：%s" % (why or _tail(se, so, n=160)), "ms": None}
        logged = _os.path.isfile(state)
        return {"provider": r["provider"], "model": r["model"] or "(默认)",
                "status": "reachable" if logged else "auth-fail",
                "detail": ("登录态文件在（%s）；真通不通要实跑一条" % _os.path.basename(state) if logged
                           else "没登录态：python doubao.py login 手动登一次"), "ms": None}
    if not r["api_key"] or not r["base_url"]:
        return {"provider": r["provider"], "model": r["model"] or "(默认)",
                "status": "no-creds", "detail": "密钥或 base_url 未配置", "ms": None}
    if capability in _LIVE_CHAT:
        model = r["model"] or _MODEL_FALLBACK.get(r["provider"])
        if not model:
            return {"provider": r["provider"], "model": "(无)", "status": "no-model",
                    "detail": "route 没配 model、也没有 provider 兜底名；不敢发 null（上游会当 unknown-model 判 400）", "ms": None}
        status, detail, ms = _probe_chat(r["base_url"], r["api_key"], model)
        return {"provider": r["provider"], "model": model, "status": status, "detail": detail, "ms": ms}
    status, detail, ms = _probe_reachable(r["base_url"], r["api_key"])
    return {"provider": r["provider"], "model": r["model"] or "(默认)", "status": status, "detail": detail, "ms": ms}


def test_models(health_url: str = "http://127.0.0.1:8090/api/health/models", deadline: float = 20.0) -> dict:
    """真·测通：并发探 21 条，但整批只等 deadline 秒。
    ⚠️ mimo 这类"长思考"模型单次可跑到 50s+，而 urllib 的 timeout 只是无活动超时、管不住总时长；
       超时的条目标 slow，由面板如实显示，不让管理台被一条慢腿拖死。"""
    from concurrent.futures import ThreadPoolExecutor, wait
    pairs = list(ROUTES)
    results = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(test_route, c, p): f"{c}/{p}" for (c, p) in pairs}
        done, not_done = wait(futs, timeout=deadline)
        for f in done:
            results[futs[f]] = f.result()
        for f in not_done:
            results[futs[f]] = {"provider": "-", "model": "-", "status": "slow",
                                "detail": f"{deadline:.0f}s 内没回来（上游在响应但很慢，如长思考模型），不代表坏",
                                "ms": None}
    ok = sum(1 for v in results.values() if v["status"] in ("live-ok", "reachable"))
    unknown = sum(1 for v in results.values()
                  if v["status"] in ("no-probe", "no-model", "local", "empty-response", "disabled", "config-error", "slow", "upstream-5xx"))
    tail = f"，另有 {unknown} 条未判定(探不了/太慢/已屏蔽/本地引擎等)" if unknown else ""
    return {"routes": results, "summary": f"{ok}/{len(results)} 通{tail}", "all_ok": ok == len(results)}


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
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "User-Agent": BROWSER_UA},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=240) as resp:
            out = _json.loads(resp.read().decode("utf-8"))
        text = ((out.get("choices") or [{}])[0].get("message") or {}).get("content")
        return {"success": bool(text), "text": text, "error": None if text else "上游返回空内容",
                "usage": out.get("usage") or {}}
    except urllib.error.HTTPError as e:
        return {"success": False, "text": None, "error": f"HTTP {e.code}: {e.read().decode('utf-8','replace')[:300]}"}
    except Exception as e:  # 连接/超时
        return {"success": False, "text": None, "error": f"{type(e).__name__}: {e}"}


def _chat_compat(base_url: str, api_key: str, model: str, messages: list,
                 temperature: float = 0.7, max_tokens: int = 4000):
    """通用 OpenAI 兼容 /chat/completions 文本调用（agnes/gpt 等皆可）。

    返回 (text, error, usage)：usage 用上游返回值，按它结算真实积分（见 settle_credits）。
    """
    import json as _json
    import urllib.error
    import urllib.request
    if not base_url or not api_key:
        return None, "缺少 base_url/api_key（检查 .env 的 AGNES 配置）", {}
    base = base_url.rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"   # agnes base 已带 /v1；zexapi(OMNI_BASE_URL) 不带，统一补
    payload = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    req = urllib.request.Request(
        base + "/chat/completions",
        data=_json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "User-Agent": BROWSER_UA}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            out = _json.loads(resp.read().decode("utf-8"))
        text = ((out.get("choices") or [{}])[0].get("message") or {}).get("content")
        usage = out.get("usage") or {}
        return (text, None, usage) if text else (None, "上游返回空内容", usage)
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.read().decode('utf-8','replace')[:200]}", {}
    except Exception as e:
        return None, f"{type(e).__name__}: {e}", {}


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
                                     headers={"Authorization": "Bearer %s" % api_key, "Content-Type": "application/json", "User-Agent": BROWSER_UA})
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
        misses = 0
        while _t.time() - start < poll_timeout:
            try:
                st = _req("%s/v1/videos/%s" % (base, job_id))
                misses = 0
            except (urllib.error.URLError, OSError) as e:
                # 只是轮询这一步断了：上游任务号还在 ⇒ 退避后继续问同一个任务。
                # 🔴 绝不重发提交（那会真的生成第二条、花两份钱）；连续 6 次问不上才判死，并留 job_id。
                misses += 1
                if misses >= 6:
                    return {"success": False, "job_id": job_id,
                            "error": "轮询连续 %d 次连接失败（上游任务 %s 可能仍在出片）：%s" % (misses, job_id, e)}
                _t.sleep(min(60, poll_interval * misses))
                continue
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


def _dig(obj, keys, depth: int = 0):
    """在不知道返回形状时按 keys 顺序挖第一个非空值（dict/list 递归，限深 3 防自引用绕圈）。"""
    if depth > 3 or obj is None:
        return None
    if isinstance(obj, dict):
        for k in keys:                                   # 先按 key 找（越具体越优先，避免抓到无关 id）
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        for v in obj.values():                           # 再钻进任意容器（实测响应把结果塞在 data:[{...}] 里）
            if isinstance(v, (dict, list)):
                got = _dig(v, keys, depth + 1)
                if got:
                    return got
        return None
    if isinstance(obj, list):
        for item in obj:
            got = _dig(item, keys, depth + 1)
            if got:
                return got
    return None


# ---- 网页/CLI 型生图腿（豆包网页版 / Google Flow 命令行）----
# 两条腿都没有 API key，只有【一个登录态】：豆包=本机 Chrome profile，gflow=一个 Google 会话，
# 而且都是驱动网页（Playwright / ui_automation）。并发两条会去抢同一个浏览器 ⇒ 网关这里串行化。
_BROWSER_LOCK = _threading.Lock()


def _run_cli(exe, args, timeout=300.0, cwd=None, env=None):
    """跑一条外部命令（绝不走 shell：参数是数组，防提示词里的引号/反引号变成命令）。

    返回 (exit_code|None, stdout, stderr, why)。FileNotFoundError / 超时都翻译成可读原因，
    不抛出去 —— 抛出去会让 invoke() 回滚预扣但调用方只看到 500。
    """
    import subprocess
    e = dict(_os.environ)
    e["PYTHONIOENCODING"] = "utf-8"
    e["GFLOW_CLI_UPDATE_CHECK"] = "0"        # 新版提示会混进 stdout，把 --json 污染成解析失败
    if env:
        e.update(env)
    try:
        p = subprocess.run([str(exe)] + [str(a) for a in args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=float(timeout),
                           cwd=cwd or None, env=e)
        return p.returncode, p.stdout or "", p.stderr or "", None
    except FileNotFoundError:
        return None, "", "", "找不到可执行文件: %s" % exe
    except _os.error as e2:
        return None, "", "", "启动失败 %s: %s" % (type(e2).__name__, str(e2)[:200])
    except Exception as e2:                                  # 含 subprocess.TimeoutExpired
        if type(e2).__name__ == "TimeoutExpired":
            return None, "", "", "超时 %ss（网页型通道本来就慢，可给 timeout 加大）" % timeout
        return None, "", "", "%s: %s" % (type(e2).__name__, str(e2)[:200])


def _tail(*parts, n=260):
    text = " | ".join(str(p).strip() for p in parts if p and str(p).strip())
    return text[-n:]


def _doubao_image(prompt, out_path, refs=None, model="", timeout=300.0, headless=False,
                  win_pos="", win_size="") -> dict:
    """豆包网页版生图（Seedream）：subprocess 调 video-ui 里的 `doubao.py gen`。

    headless=True 走 Chrome【新版无头】（Playwright 1.61 + channel=chrome，真渲染，和老
    headless 不是一回事）—— 2026-10-04 之前 doubao.py 的 launch() 在有登录态时硬编码有头，
    传什么都不算；那行已放开，所以这里 honored 不再看登录态文件在不在。
    win_pos/win_size = 有头时把窗口挪到屏内角落（不挡人）。⚠️ 只能屏内：挪出屏幕=未渲染=掉登录。
    它只落盘、不回 URL，所以 output_path 必填。
    """
    import time
    py, root = _get("DOUBAO_PYTHON"), _get("VIDEO_UI_ROOT")
    script = _os.path.join(root, "doubao.py") if root else ""
    if not (py and _os.path.isfile(py)):
        return {"success": False, "error": "DOUBAO_PYTHON 不存在：这条要指向【装了 playwright 的解释器】"
                                           "（= 跑 video-ui 那个），不是网关所在的 .venv：%s" % py}
    if not _os.path.isfile(script or ""):
        return {"success": False, "error": "VIDEO_UI_ROOT/doubao.py 找不到：%s" % script}
    if not str(prompt or "").strip():
        return {"success": False, "error": "image.generate 缺 prompt"}
    if not out_path:
        return {"success": False, "error": "豆包生图必须给 output_path（它只落盘，不回 URL）"}
    bad = [str(r) for r in (refs or []) if not _os.path.isfile(str(r))]
    if bad:
        return {"success": False, "error": "参考图必须是本机存在的文件（不收公网 URL）：%s" % ", ".join(bad[:3])}
    args = [script, "gen", prompt, "--out", out_path,
            "--model", model or _get("DOUBAO_IMAGE_MODEL") or "Seedream 4.5"]
    for r in refs or []:
        args += ["--ref", str(r)]
    if not headless:
        args.append("--headed")
    env = {}
    if win_pos:
        env["DOUBAO_WIN_POS"] = str(win_pos)
    if win_size:
        env["DOUBAO_WIN_SIZE"] = str(win_size)
    t = time.time()
    with _BROWSER_LOCK:
        code, so, se, why = _run_cli(py, args, timeout, cwd=root, env=env or None)
    ms = round((time.time() - t) * 1000)
    ok = code == 0 and _os.path.isfile(out_path) and _os.path.getsize(out_path) > 1024
    if why and "超时" in why:
        # 超时不一定是"慢"。Playwright 每步都往 _doubao_probe 写现场截图：gen_05_sent 是本次
        # 新写的 ⇒ 提示词已经发出去了、卡在等回图，那个位置最常见的就是图形验证码（实测）。
        sent = _os.path.join(root, "_doubao_probe", "gen_05_sent.png")
        if _os.path.isfile(sent) and _os.path.getmtime(sent) > t:
            why += "；但现场截图 gen_05_sent.png 是本次新写的 ⇒ 提示词已发出、卡在等回图，" \
                   "这个位置实测多半是豆包的图形验证码（无头尤其容易被拦）"
    return {"success": ok, "path": out_path if ok else None,
            "bytes": _os.path.getsize(out_path) if _os.path.isfile(out_path) else 0,
            "model": args[args.index("--model") + 1], "ms": ms, "exit_code": code,
            "headless": bool(headless), "window": (win_pos or win_size) and "%s/%s" % (win_pos, win_size) or "默认",
            "error": None if ok else (why or "豆包这条没落到文件。两种已知形态：① 登录态掉了（跑 "
                                             "python doubao.py login 重登）；② 【豆包弹了图形验证码】—— "
                                             "2026-10-04 实测无头跑到『发送提示词』成功、随后被"
                                             "『请选择所有符合上文描述的图片并拖拽』的验证窗口挡住，"
                                             "于是一直等到超时，所以这条默认有头。现场截图看 "
                                             "VIDEO_UI_ROOT/_doubao_probe/gen_05_sent.png。上游原文：%s"
                                      % _tail(se, so))}


def _gflow_image(prompt, out_path, refs=None, model="", aspect="9:16", project="", timeout=300.0,
                 headless=False) -> dict:
    """Google Flow 生图（Imagen / Nano Banana）：subprocess 调 `gflow image t2i|i2i --json`。

    给了参考图就走 i2i；`--project` 只在单条提示词时有效（多提示词 gflow 自己限制）。
    gflow 把文件写进 `--out` 目录并自己命名，所以先按 --json 里的路径找，找不到就取该目录里
    本次新生成的最新 png，再挪到调用方要的 output_path。
    ⚠️ 默认有头，但 headless 不是变量：2026-10-04 无头/有头各跑一次，两边都进了 Flow 项目页、
    UI 逐字段相同，都退 `WafRejectionError`(exit 10)。上游给的定性是
    `Flow's bot check scored this browser profile as unusual activity … nothing was charged`
    （KNOWN_ISSUES:PUBLIC_ERROR_UNUSUAL_ACTIVITY）—— 被打分的是 gflow 那个 Playwright profile，
    不是账号：`gflow auth status` 当场验过会话有效，且同一账号在浏览器里能正常出图。
    换 project、换提示词都试过，同样拒 ⇒ 解法只有"等几小时"或"换 profile"（gflow auth login 新建）。
    """
    import glob
    import shutil
    import time
    exe = _get("GFLOW_EXE") or ""
    if not _os.path.isfile(exe):
        import shutil as _sh
        exe = _sh.which("gflow") or ""
    if not exe:
        return {"success": False, "error": "gflow 可执行文件找不到（GFLOW_EXE 没配对，或没 pip 装 gflow-cli）"}
    if not str(prompt or "").strip():
        return {"success": False, "error": "image.generate 缺 prompt"}
    if not out_path:
        return {"success": False, "error": "gflow 生图必须给 output_path（Flow 侧链接会过期）"}
    refs = [str(r) for r in (refs or []) if r]
    bad = [r for r in refs if not _os.path.isfile(r)]
    if bad:
        return {"success": False, "error": "gflow 的 --ref 要本机文件路径（它自己负责上传）：%s" % ", ".join(bad[:3])}
    out_dir = _os.path.dirname(_os.path.abspath(out_path)) or _os.getcwd()
    _os.makedirs(out_dir, exist_ok=True)
    mode = "i2i" if refs else "t2i"
    before = set(glob.glob(_os.path.join(out_dir, "*.png")))
    t = time.time()
    env = {"GFLOW_CLI_HEADLESS": "true" if headless else "false"}
    with _BROWSER_LOCK:
        code, so, se, why = _run_cli(exe, ["auth", "status"], 30.0, env=env)      # 先验登录态，别白跑一条生成
        if code != 0:
            return {"success": False, "error": "gflow 没登录（auth status 退 %s）：跑 gflow auth login：%s"
                                                % (code, _tail(se, so))}
        args = ["image", mode, prompt, "--model", model or _get("GFLOW_IMAGE_MODEL") or "nano2",
                "--aspect", aspect or "9:16", "--out", out_dir, "--json"]
        for r in refs:
            args += ["--ref", r]
        proj = project or _get("GFLOW_PROJECT")
        if proj and not refs:
            args += ["--project", proj]
        code, so, se, why = _run_cli(exe, args, timeout, env=env)
    ms = round((time.time() - t) * 1000)
    if code != 0:
        return {"success": False, "error": "gflow image %s 退 %s：%s" % (mode, code, _tail(se, so)),
                "exit_code": code, "headless": headless, "ms": ms}
    found = None
    try:
        import json as _j
        found = _dig(_j.loads(so), ("path", "file", "output_path", "local_path", "dest", "saved"))
    except Exception:
        found = None                                      # 版本差异导致 --json 形变了：走目录扫描兜底
    if not (found and _os.path.isfile(str(found))):
        fresh = [p for p in glob.glob(_os.path.join(out_dir, "*.png")) if p not in before]
        found = max(fresh, key=_os.path.getmtime) if fresh else None
    if not found or not _os.path.isfile(str(found)):
        return {"success": False, "error": "gflow 回 0 但没找到落盘文件：%s" % _tail(so, se), "ms": ms}
    src = str(found)
    if _os.path.abspath(src) != _os.path.abspath(out_path):
        try:
            shutil.copy2(src, out_path)
        except OSError as e:
            return {"success": False, "error": "gflow 出图了但挪不到你要的路径 %s: %s" % (out_path, e),
                    "source": src, "ms": ms}
    return {"success": True, "path": out_path, "source": src, "bytes": _os.path.getsize(out_path),
            "model": args[args.index("--model") + 1], "mode": "image " + mode, "ms": ms,
            "headless": headless, "exit_code": code, "error": None}


def _seed_audio(base_url: str, api_key: str, text: str, out_path: str | None = None,
                model: str = "seed-audio-1.0", fmt: str = "mp3", sample_rate: int = 48000,
                pitch_rate: int = 0, speech_rate: int = 0, loudness_rate: int = 0,
                timeout: float = 300.0) -> dict:
    """火山引擎·豆包语音「音频生成」非流式：一条请求同时出音效+配乐+多角色配音。

    与其余适配器不同的两点，都是实测/文档口径逼出来的：
      1) 鉴权是 `X-Api-Key` 头，不是 `Authorization: Bearer`（401 时再按 Bearer 重试一次，
         这样文档口径变了也不用改代码，结果里回 `auth` 说明哪条通的）；
      2) 成功回的是【二进制音频体】而不是链接 ⇒ 不落盘就等于没拿到（上游不留档）。
    """
    import base64 as _b64
    import json as _json
    import time
    import urllib.error
    import urllib.request
    if not (base_url and api_key):
        return {"success": False, "error": "缺 BYTEDANCE_AUDIO_BASE_URL/API_KEY"}
    if not str(text or "").strip():
        return {"success": False, "error": "audio.generate 缺 prompt（这条上游的报错文案会误导成参数问题，先本地拦）"}
    if not out_path:
        return {"success": False, "error": "audio.generate 必须给 output_path（成功回二进制音频体，不落盘就没了）"}
    payload = {"model": model, "text_prompt": text,
               "audio_config": {"format": fmt, "sample_rate": int(sample_rate), "pitch_rate": int(pitch_rate),
                                "speech_rate": int(speech_rate), "loudness_rate": int(loudness_rate)},
               "watermark": {}}
    body = _json.dumps(payload, ensure_ascii=False).encode("utf-8")
    url = base_url.rstrip("/") + "/api/v3/tts/create"
    t = time.time()
    data = ctype = None
    used = "x-api-key"
    for attempt, headers in enumerate((
            {"Content-Type": "application/json", "X-Api-Key": api_key},
            {"Content-Type": "application/json", "Authorization": "Bearer " + api_key})):
        used = "bearer" if attempt else "x-api-key"
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                ctype = (r.headers.get("Content-Type") or "").lower()
                data = r.read()
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if e.code in (401, 403) and attempt == 0:
                continue                          # 只换鉴权形再试一次，不重投计费请求
            return {"success": False, "error": "HTTP %d: %s" % (e.code, detail), "http_status": e.code}
        except Exception as e:
            return {"success": False, "error": "%s: %s" % (type(e).__name__, str(e)[:260])}
    if data is None:
        return {"success": False, "error": "两次鉴权都没回内容"}
    ms = round((time.time() - t) * 1000)
    if "json" in ctype:                             # 有的区域/版本回 JSON：里面要么 base64 要么链接
        try:
            obj = _json.loads(data.decode("utf-8", "replace"))
        except Exception:
            return {"success": False, "error": "Content-Type 是 json 但解析不了: %s" % data[:200]}
        b64 = _dig(obj, ("data", "audio", "audio_data", "base64", "b64_json"))
        link = _dig(obj, ("url", "audio_url", "file_url", "download_url"))
        raw = None
        if isinstance(b64, str) and b64:
            try:
                raw = _b64.b64decode(b64)
            except Exception:
                raw = None
        if raw is None and isinstance(link, str) and link.startswith("http"):
            try:
                with urllib.request.urlopen(urllib.request.Request(link, headers={"User-Agent": BROWSER_UA}),
                                            timeout=timeout) as r:
                    raw = r.read()
            except Exception as e:
                return {"success": False, "error": "JSON 给了链接但下载失败 %s: %s" % (type(e).__name__, str(e)[:200]),
                        "url": link, "ms": ms}
        if not raw:
            return {"success": False, "error": "上游回 200 但没音频字段：%s" % str(obj)[:240], "ms": ms}
        data = raw
    if len(data) < 128:
        return {"success": False, "error": "音频体只有 %d 字节，判失败（不落假成功）" % len(data), "ms": ms}
    try:
        parent = _os.path.dirname(_os.path.abspath(out_path))
        if parent:
            _os.makedirs(parent, exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(data)
    except OSError as e:
        return {"success": False, "error": "写盘失败 %s: %s" % (type(e).__name__, e)}
    return {"success": True, "path": out_path, "bytes": len(data), "content_type": ctype or "audio/" + fmt,
            "chars": len(text), "model": model, "auth": used, "ms": ms, "error": None}


def _h3_payload(params: dict):
    """把网关的标准参数拼成 H3 导演台的一段输入，返回 (segments, task_type, error)。

    形态由【给了哪些参考】决定，不靠调用方记参数名：
      视频 + 图片 ⇒ rv2v（视频出动作、图片换主体——"动作照原片、鞋换新鞋"就是这条）
      只有视频    ⇒ v2v
      图片 ≥2     ⇒ r2v ｜ 图片 1 张 ⇒ i2v（首帧）｜ 都没有 ⇒ t2v
    H3 导演台只吃【本地文件】：提交时由 client 上传换 fileName，公网 URL 会在 client 里炸成
    FileNotFoundError（那是我们的错，不该让上游替我们报 400）。
    """
    refs = [str(p) for p in (params.get("image_paths") or []) if str(p).strip()]
    vids = [str(p) for p in (params.get("video_paths") or []) if str(p).strip()]
    for x in vids + refs:
        if x.startswith(("http://", "https://")):
            return None, None, ("H3 的 image_paths/video_paths 只收本地文件，这里收到公网 URL：%s"
                                "（先用 /uploads 落地再提交）" % x[:80])
        if not _os.path.isfile(x):
            return None, None, "H3 参考文件不存在：%s" % x[:120]
    if len(vids) > 3:
        return None, None, "H3 参考视频最多 3 条（上游端口表），这次给了 %d 条" % len(vids)
    if len(refs) > 9:
        return None, None, "H3 参考图片最多 9 条（上游端口表），这次给了 %d 条" % len(refs)
    # fl2v 首尾帧：显式 startImage + endImage 路径（尾帧继承链用）
    sp, ep = str(params.get("start_image") or "").strip(), str(params.get("end_image") or "").strip()
    for x in (sp, ep):
        if x and not _os.path.isfile(x):
            return None, None, "H3 首尾帧文件不存在：%s" % x[:120]
    seg = {"durationSec": float(params.get("duration") or 5), "prompt": params["prompt"],
           "negativePrompt": params.get("negative_prompt") or "bad video"}
    if sp:
        seg["startImagePath"] = sp
        refs = [r for r in refs if r != sp]
    if ep:
        seg["endImagePath"] = ep
        refs = [r for r in refs if r != ep]
    if vids:
        seg["referenceVideoPath"] = vids[0]
        if len(vids) > 1:
            seg["refVideoPaths"] = vids[1:]
        if refs:
            seg["refImagePaths"] = refs
        ttype = params.get("task_type") or ("rv2v" if refs else "v2v")
    elif sp or ep:
        ttype = params.get("task_type") or "fl2v"
        if refs:
            seg["refImagePaths"] = refs
    elif len(refs) >= 2:
        seg["refImagePaths"] = refs
        ttype = params.get("task_type") or "r2v"
    elif refs:
        seg["startImagePath"] = refs[0]
        ttype = params.get("task_type") or "i2v"
    else:
        ttype = params.get("task_type") or "t2v"
    return [seg], ttype, None


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
    if capability == "video.generate" and r["purpose"] == "agnes-v":
        # Agnes Video 2.5 **Flash**：上游走免费额度，所以这条是"能出片但不必占付费腿"的兜底。
        # 客户端只暴露 create_flash_task/wait_flash_task，且 Flash 不收参考视频（实测会 400）⇒ 本地先明拒，不静默丢字段。
        import asyncio
        from backend.services import agnes_video_client as agnes_v
        if params.get("video_urls") or params.get("videos"):
            return {"success": False,
                    "error": "Agnes Flash 不支持参考视频（会 400）；要参考视频走 seedance 或 agnes-2.5"}
        if params.get("firstFrame") or params.get("lastFrame") or params.get("first_frame") or params.get("last_frame"):
            return {"success": False, "error": "Agnes Flash 没有首尾帧模式（那是 agnes-2.5 的 keyframe）"}
        try:
            sub = asyncio.run(agnes_v.create_flash_task(
                params["prompt"],
                seconds=str(params.get("duration") or 5),
                aspect_ratio=params.get("aspect_ratio") or "9:16",
                images=params.get("image_urls") or params.get("images"),
                seed=params.get("seed")))
        except Exception as e:                                   # 客户端抛 RuntimeError 带上游原文
            return {"success": False, "error": "%s: %s" % (type(e).__name__, str(e)[:300])}
        vid = _dig(sub, ("video_id", "task_id", "id"))
        if not vid:
            return {"success": False, "error": "Agnes 提交没返回 video_id：%s" % str(sub)[:200]}
        try:
            done = asyncio.run(agnes_v.wait_flash_task(
                vid, poll_s=float(params.get("poll_interval") or 3.0),
                timeout_s=float(params.get("poll_timeout") or 600.0)))
        except Exception as e:
            return {"success": False, "job_id": vid, "error": "%s: %s（可据此查它到底出没出片）" % (type(e).__name__, str(e)[:220])}
        url = _dig(done, ("file", "video_url", "download_url", "url"))
        path = None
        if url and params.get("output_path"):
            path = asyncio.run(agnes_v.download_file(url, params["output_path"]))
        return {"success": bool(url), "url": url, "path": path, "job_id": vid,
                "error": None if url else "Agnes 回成功了但既没文件也没 URL：%s" % str(done)[:200]}

    if capability == "video.generate" and r["purpose"] == "h3":
        # RunningHub H3 导演台（ComfyUI 工作流）。工作流号从 .env 取（实测出片那条 = 2095474072795865090）。
        # 🔴 上游按 GPU 秒**真扣钱**（plus/48G 实例），不是 agnes 那种免费档；链接 24 小时失效 ⇒ 必须落盘。
        # 不直接用 h3_director_client.generate_video：它内部 submit+poll 但不回 taskId，失败就没法查上游（"别让人盲跑"这条不能破）。
        import asyncio
        from backend.services import h3_director_client as h3d
        from backend.services.runninghub_client import RunningHubClient
        if params.get("image_urls"):
            return {"success": False, "error": "H3 导演台吃的是本地文件（提交时上传拿 fileName），不支持公网 URL；"
                                               "先把参考图落到本地路径再用 image_paths"}
        if not str(params.get("prompt") or "").strip():
            return {"success": False, "error": "H3 缺 prompt（上游这类拒绝的文案会误导成参数问题，先本地拦）"}
        out = params.get("output_path")
        if not out:
            return {"success": False, "error": "H3 必须给 output_path（上游链接 24 小时就失效）"}
        segs, ttype, err = _h3_payload(params)
        if err:
            return {"success": False, "error": err}
        try:
            sub = asyncio.run(h3d.submit_h3_director(
                task_type=ttype, segments=segs,
                width=int(params.get("width") or 576), height=int(params.get("height") or 1024),
                global_prompt=params.get("global_prompt") or params["prompt"],
                seed=int(params.get("seed") or 0)))
        except Exception as e:
            return {"success": False, "error": "H3 提交失败 %s: %s" % (type(e).__name__, str(e)[:300])}
        tid = sub.get("taskId")
        if not tid:
            return {"success": False, "error": "H3 提交没回 taskId：%s" % str(sub)[:200]}
        try:
            path = asyncio.run(h3d.poll_and_download(tid, out, timeout=float(params.get("poll_timeout") or 2400)))
        except Exception as e:
            return {"success": False, "job_id": tid,
                    "error": "H3 %s: %s（可据此查上游任务）" % (type(e).__name__, str(e)[:260])}
        url = ""
        try:                       # 补一次查询只为拿回链接；拿不到不影响成片已落盘
            url = next((i.get("url", "") for i in (asyncio.run(RunningHubClient().query_task(tid)) or {}).get("results") or []
                        if i.get("url")), "")
        except Exception:
            pass
        return {"success": bool(path), "url": url, "path": path, "job_id": tid,
                "workflow_id": sub.get("workflow_id"), "unet_type": sub.get("unet_type"),
                "error": None if path else "H3 回成功了但没落到文件"}

    if capability == "video.analyze":
        return analyze_media(
            path=params["path"],
            question=params.get("question") or "用一句话描述这个画面的关键信息。",
            model=r["model"] or _MODEL_FALLBACK.get(r["provider"]), base_url=r["base_url"], api_key=r["api_key"],
            max_tokens=params.get("max_tokens", 300),
        )
    if capability == "text.complete":
        messages = params.get("messages") or [{"role": "user", "content": params["prompt"]}]
        text, err, usage = _chat_compat(
            r["base_url"], r["api_key"], params.get("model") or r["model"] or _MODEL_FALLBACK.get(r["provider"], ""), messages,
            temperature=params.get("temperature", 0.7), max_tokens=params.get("max_tokens", 4000),
        )
        return {"success": bool(text), "text": text, "error": err, "usage": usage}
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
    if capability == "image.generate" and r["purpose"] == "doubao":
        return _doubao_image(
            params.get("prompt") or params.get("text_prompt"),
            out_path=params.get("output_path"),
            refs=params.get("image_urls") or params.get("images") or params.get("refs"),
            model=params.get("model") or r["model"] or "",
            timeout=float(params.get("timeout") or 300),
            headless=bool(params.get("headless")),
            win_pos=str(params.get("win_pos") or ""), win_size=str(params.get("win_size") or ""))
    if capability == "image.generate" and r["purpose"] == "gflow":
        return _gflow_image(
            params.get("prompt") or params.get("text_prompt"),
            out_path=params.get("output_path"),
            refs=params.get("image_urls") or params.get("images") or params.get("refs"),
            model=params.get("model") or r["model"] or "",
            aspect=params.get("aspect") or params.get("ratio") or "9:16",
            project=params.get("project") or "",
            headless=bool(params.get("headless", False)),
            timeout=float(params.get("timeout") or 300))
    if capability == "audio.generate" and r["purpose"] == "seed-audio":
        text = params.get("prompt") or params.get("text_prompt")
        return _seed_audio(
            r["base_url"], r["api_key"], text,
            out_path=params.get("output_path"),
            model=params.get("model") or r["model"] or "seed-audio-1.0",
            fmt=params.get("format") or params.get("fmt") or "mp3",
            sample_rate=params.get("sample_rate", 48000),
            pitch_rate=params.get("pitch_rate", 0), speech_rate=params.get("speech_rate", 0),
            loudness_rate=params.get("loudness_rate", 0),
            timeout=float(params.get("timeout") or 300))
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
        has = {r[1] for r in con.execute("PRAGMA table_info(holds)")}
        for col, typ in (("route", "TEXT"), ("est", "INTEGER"), ("tokens", "INTEGER")):
            if col not in has:
                con.execute(f"ALTER TABLE holds ADD COLUMN {col} {typ}")   # 逐路由审计 + 预估vs实扣；旧行留 NULL
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
        con.execute("INSERT INTO holds(hold_id,member,amount,status,route,est) VALUES(?,?,?,'pending',?,?)",
                    (hold_id, member, cost, f"{capability}/{purpose}", cost))
        con.execute("INSERT INTO ledger(member,delta,note,ts) VALUES(?,?,?,?)",
                    (member, -cost, f"preauth {capability}/{purpose}", _time.time()))
        con.commit(); con.close()
        return {"hold_id": hold_id, "amount": cost, "balance_after": self.balance(member)}

    def commit(self, hold_id: str, actual: int, tokens: int | None = None) -> dict:
        con = self._con()
        row = con.execute("SELECT member,amount,status FROM holds WHERE hold_id=?", (hold_id,)).fetchone()
        if not row or row[2] != "pending":
            con.close(); return {"ok": False, "reason": "hold 不存在或已终结"}
        member, held = row[0], row[1]
        diff = held - actual  # 预扣多了就退差
        if diff:
            con.execute("UPDATE accounts SET balance=balance+? WHERE member=?", (diff, member))
            con.execute("INSERT INTO ledger(member,delta,note,ts) VALUES(?,?,?,?)",
                        (member, diff, f"settle {hold_id} 实扣{actual}/预估{held}"
                         + (f" tok={tokens}" if tokens else ""), _time.time()))
        con.execute("UPDATE holds SET status='done', amount=?, tokens=? WHERE hold_id=?", (actual, tokens, hold_id))
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
        by_route = [{"route": r or "(旧记录未记路由)", "status": s, "calls": n, "credits": c,
                     "est": e, "tokens": tk or 0}
                    for r, s, n, c, e, tk in con.execute(
                        "SELECT route,status,COUNT(*),SUM(amount),SUM(COALESCE(est,amount)),SUM(COALESCE(tokens,0))"
                        " FROM holds GROUP BY route,status ORDER BY route,status")]
        recent = [{"hold_id": h, "member": m, "route": r or "(旧)", "amount": a, "status": s,
                   "est": e if e is not None else a, "tokens": t or 0}
                  for h, m, r, a, s, e, t in con.execute(
                      "SELECT hold_id,member,route,amount,status,est,tokens FROM holds ORDER BY rowid DESC LIMIT ?",
                      (limit,))]
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
        charged, tokens, basis = settle_credits(capability, r["purpose"], result, est)
        done = ledger().commit(hold["hold_id"], charged, tokens)
        return {"ok": True, "capability": capability, "purpose": r["purpose"], "result": result,
                "credits": {"estimated": est, "charged": charged, "tokens": tokens, "basis": basis,
                            "hold_id": hold["hold_id"],
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
        try:
            print(safe_summary(resolve(cap, pur)))
        except RouteError as e:
            print(f"ERROR: {e}")
            return 2
    elif cmd == "prices":
        cfg = external_config()
        print(f"价目源: {price_path()}  →  外置 {len(cfg['prices'])} 条 / 默认路由 {len(cfg['defaults'])} 条"
              + ("" if cfg["prices"] else "（文件缺失或损坏，当前用内置兜底表）"))
        for cap in CAPABILITIES:
            dft = cfg["defaults"].get(cap) or DEFAULT_PURPOSE.get(cap)
            for (c, p) in ROUTES:
                if c == cap:
                    mark = "★" if p == dft else " "
                    if cfg["disabled"].get(f"{c}/{p}") is not None:
                        mark, extra = "✗", "已屏蔽 " + (cfg["disabled"].get(f"{c}/{p}") or "")
                    else:
                        extra = ""
                    print(f"  {mark} {cap}/{p:<14} {unit_price(c, p):>4} 分 {extra}")
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
        try:
            env = invoke(cap, pur, account, params)
        except RouteError as e:   # 未知/已屏蔽 purpose：仍回归一 JSON（skill 只认 ok/error，不该看到 traceback）
            env = {"ok": False, "capability": cap, "purpose": pur, "result": None,
                   "credits": {"estimated": 0, "charged": 0, "balance_after": None}, "error": str(e)}
        print(json.dumps(env, ensure_ascii=False, indent=2))
    else:
        print("用法: python backend/gateway.py [caps | prices | credits | resolve <cap> [purpose] | "
              "call <cap> [purpose] [--account X] [--json '{...}'] | test | analyze <path> [问题]]")


if __name__ == "__main__":
    _cli(_sys.argv[1:])
