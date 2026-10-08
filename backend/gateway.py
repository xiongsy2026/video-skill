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

CAPABILITIES = ["text.complete", "image.generate", "image.edit", "video.generate", "video.edit", "video.analyze", "lipsync", "transcribe", "audio.generate"]

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
    # —— GPT Image 2.5（同 Image2 端点；/models 实测 sunburst/flare 两型，model 住 config 的 IMAGE2_IMAGE25_MODEL）——
    #    sunburst=保真默认、flare=显式提速档（Framewright gpt_image_2_5 adapter 的 default/speed 模型 ID 就是这两个）。
    ("image.generate", "gpt-image2.5"): {"provider": "image2", "client": "image2_client", "fn": "Image2Client.text_to_image_sync",
                                         "fields": {"base_url": "IMAGE2_BASE_URL", "api_key": "IMAGE2_API_KEY", "model": "IMAGE2_IMAGE25_MODEL"}},
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
    # —— 视频后处理（去水印）——
    # RunningHub AI App 2012077432525824001：吃【本地视频文件】（提交时上传拿 fileName），清 Sora/Seedance 的文字与图标。
    # 节点号全部住 config（.env 可覆盖），因为它们是对方工作流的内部编号，对方一改版就得换。
    ("video.edit", "dewatermark"):   {"provider": "runninghub", "client": "gateway._dewatermark", "fn": "upload→run/ai-app→query→落盘",
                                      "fields": {"base_url": "RUNNINGHUB_DEWATERMARK_BASE_URL", "api_key": "RUNNINGHUB_ENTERPRISE_API_KEY",
                                                 "model": "RUNNINGHUB_DEWATERMARK_APP_ID"}},
    # —— 视频高清修复（RunningHub AI App 1990029249488801793，2026-10-05 接入）——
    # 与 dewatermark 同一套协议（upload→run/ai-app→query→落盘）。节点号住 config（.env 可覆盖），
    # 默认空 = 花钱前拦截（同 dewatermark 规矩：节点号只住 config，代码里不留字面量兜底）。
    ("video.edit", "enhance"):       {"provider": "runninghub", "client": "gateway._enhance", "fn": "upload→run/ai-app→query→落盘",
                                       "fields": {"base_url": "RUNNINGHUB_ENHANCE_BASE_URL", "api_key": "RUNNINGHUB_ENTERPRISE_API_KEY",
                                                  "model": "RUNNINGHUB_ENHANCE_APP_ID"}},
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
    # —— 动作迁移·第二条腿：8091 nzdh 工作室的【浏览器 RPA】路（Playwright 登录 RH 账号池 → 操作公共工作流
    # 2106377827540103169 → 等节点 285 出片 → 下 final.mp4）。和上面那条 API 腿是两个上游工作流、两套价，
    # 故意不合并：合并了就没人知道这次扣的是哪条。provider 以 local 开头 ⇒ 管理台不做 HTTP 探活
    # （探它 = 真去跑一条工作流、要花钱，探活不许花钱）。
    ("video.generate", "motion-rpa"):    {"provider": "local-nzdh","client": "gateway._motion_rpa_run", "fn": "POST /run | /step5-rpa → 轮询 /task/<id> → 落盘","fields": {"base_url": "NZDH_BASE_URL", "api_key": None, "model": None}},
    # —— WAN 3.0（xjai.beauty·OpenAI 兼容 /v1/videos/generations·2026-10-06 接入）——
    # 两条 model 是【两个价】的上游档位（prime 是更高档），所以分成两个 purpose，不合并：
    # 合并了就没人知道这次扣的是哪条（同 motion / motion-rpa 那条理由）。
    # media[] 只吃公网 URL；下载腿 /content 也要 Bearer（见 _xjai_video）。
    # 只读探测（GET /v1/models·0 消耗）自报的边界：resolutions 480P/720P/1080P（默认 1080P）、
    # 参考料 images≤10 / videos≤5 / audios≤5 / 合计≤20、支持模式 text|image|first_last_frame|reference。
    ("video.generate", "wan3"):          {"provider": "xjai", "client": "gateway._xjai_video", "fn": "POST /v1/videos/generations → GET /v1/videos/{id} → GET .../content", "fields": {"base_url": "XJAI_BASE_URL", "api_key": "XJAI_API_KEY", "model": "XJAI_VIDEO_MODEL"}},
    ("video.generate", "wan3-prime"):    {"provider": "xjai", "client": "gateway._xjai_video", "fn": "同上，model=wan3.0-video-prime", "fields": {"base_url": "XJAI_BASE_URL", "api_key": "XJAI_API_KEY", "model": "XJAI_PRIME_MODEL"}},
    # —— H3 的【第二条腿】：本机 8093 的浏览器 RPA 工作台（Playwright 登录 RunningHub → 注入 [12] 画布 →
    # 点运行 → 抓 [7] SaveVideo 下载）。和上面 video.generate/h3（RunningHub OpenAPI 直连）是两个入口、
    # 两套坏法（key 欠费/工作流改版 vs 登录态过期/串行锁被占），所以不合并。
    # provider 以 local 开头 ⇒ 管理台不做 HTTP 探活（探它=真开浏览器跑一条，探活不许花钱）。
    ("video.generate", "h3-rpa"):        {"provider": "local-h3rpa", "client": "gateway._h3_rpa_run", "fn": "POST /api/generate → 轮询 /api/status/<id> → 校验落盘", "fields": {"base_url": "H3_RPA_BASE_URL", "api_key": None, "model": None}},
    # —— WAN 3.0 的【第三条入口】：youzan666.vip 中转（OpenAI 风格 /v1/videos/generations）——
    # 与上面 xjai 那两条同模型不同中转：路径同款但素材类型多 first_frame/last_frame、prompt 上限 19000、
    # prime 只支持 30 秒+1080p、成片是带 expiresAt 的直链（不是 /content 端点）⇒ 校验器与下载逻辑都不共用。
    ("video.generate", "wan3-youzan"):      {"provider": "youzan666", "client": "gateway._yz_video", "fn": "POST /v1/videos/generations → GET /v1/videos/{id} → 下 result.url", "fields": {"base_url": "YOUZAN_BASE_URL", "api_key": "YOUZAN_API_KEY", "model": "YOUZAN_VIDEO_MODEL"}},
    ("video.generate", "wan3-youzan-prime"):{"provider": "youzan666", "client": "gateway._yz_video", "fn": "同上，model=wan3.0-video-prime-1080p（只支持 30 秒 + 1080p）", "fields": {"base_url": "YOUZAN_BASE_URL", "api_key": "YOUZAN_API_KEY", "model": "YOUZAN_PRIME_MODEL"}},
    # 智能调度档（用户 2026-10-07 给的说明）：明确 30 秒 + 1080P 时优先走符合条件的 Prime 通道，
    # 其余规格与"自动时长"走标准版 ⇒ 我们只发 model=wan3.0-smart，选路在上游做；积分按 Smart 配置计费（我们没有那个配置的数）。
    ("video.generate", "wan3-smart"):       {"provider": "youzan666", "client": "gateway._yz_video", "fn": "同上，model=wan3.0-smart（上游自行调度 prime/标准）", "fields": {"base_url": "YOUZAN_BASE_URL", "api_key": "YOUZAN_API_KEY", "model": "YOUZAN_SMART_MODEL"}},
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
    "video.edit": "dewatermark",
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
    # GPT Image 2.5 占位同 generic 档（同端点同族，真差价等接上游计费后校准）；改价走 model_prices.json，不动代码
    ("image.generate", "gpt-image2.5"): 4,
    # 网页/CLI 两条腿：我方账本先记 0（不谎报扣费）。豆包网页版不花钱；gflow 烧的是 Google Flow 额度，
    # 那是【另一个账户的配额】不是我方现金 ⇒ 等"1 元=几分"定了再一起补价。
    ("image.generate", "doubao"): 0, ("image.generate", "gflow"): 0,
    ("image.edit", "faceswap"): 20,
    # ⚠️ 去水印这条【没有实测价】：RunningHub 按 GPU 秒真扣钱，而"1 元=几分"还没定。
    #    20 是照同一家 faceswap 档借的占位数，只用于报价量级；跑通第一条后要按上游 usage 校准并改 model_prices.json。
    ("video.edit", "dewatermark"): 20,
    # ⚠️ 高清修复同 RunningHub 按 GPU 秒真扣钱：4K 修复比去水印烧得多，60 是占位量级（跑完第一条
    #    拿信封里的 upstream_usage.consume_money 回来校准并改 model_prices.json）。
    ("video.edit", "enhance"): 60,
    ("video.generate", "omni-t2v"): 40, ("video.generate", "agnes-v"): 30, ("video.generate", "clone-r2v"): 50,
    ("video.generate", "seedance-2.5"): 60, ("video.generate", "seedance-2.0"): 50,
    ("video.generate", "wan3-dashscope"): 50, ("video.generate", "h3"): 60, ("video.generate", "motion"): 50,
    # ⚠️ 动作迁移 RPA 腿 60 分/段是【占位·未实测】（2026-10-06 用户拍的占位值）：上游 RunningHub 按 GPU 秒真扣钱。
    #    按段计不按秒（一次 RPA = 一段成片），见 meter() 里那条例外。跑通第一条后拿上游消耗校准、改 model_prices.json。
    ("video.generate", "motion-rpa"): 60,
    # ⚠️ xjai WAN 3.0 两条【无实测价】（2026-10-06 接入，从没跑过一条）：50/60 是照同族 wan3 腿
    #    （clone-r2v=50、wan3-dashscope=50）与 prime 高档借来的占位量级，只用于报价，不是上游报价。
    #    跑通第一条后必须拿上游实扣校准并改 model_prices.json（改价不用动代码）。
    #    观测记录（同日元到 /v1/models 的只读探测，0 消耗）：上游自报 price=18（wan3）/20（prime），
    #    但响应里【没有单位字段】（不是 currency、也没说按秒还是按条）⇒ 不当价用，等实扣或对方口径。
    ("video.generate", "wan3"): 50, ("video.generate", "wan3-prime"): 60,
    # H3 的浏览器 RPA 腿：花的是【对方 RunningHub 账户的 GPU 秒】（这台机器登录的那个账号），我方按次记账。
    # 60 = 照同一条 H3 的 API 腿（video.generate/h3=60）借的量级。
    # ✅ 2026-10-06 21:41→21:50 经网关实跑通第一条：1 段 6 秒 / 480×864(9:16) / plus / 墙钟 572.8s /
    #    信封 charged=60 basis=flat（productions/2026-10-06-H3-RPA-9x16链路实测/outputs/h3-rpa-916-01.mp4）。
    # ⚠️ 但 60 仍是【占位·未校准】：链路通了 ≠ 知道对方账户实扣多少人民币 —— 那要拿 RunningHub 账单对。
    ("video.generate", "h3-rpa"): 60,
    # ⚠️ youzan666 这三条【无实测价】= 占位（2026-10-07 接入；当天只做过 0 消耗的拒绝路径探针，
    #    一次真提交都没跑过）：50/60 是照同模型的 xjai 两条借的占位量级，只用于报价。
    #    文档只说"视频按秒扣积分、失败自动退还"、智能档说"按 Smart 配置计费"，都没给"每秒多少分"的数
    #    ⇒ 报价必须写"无数据，以平台实扣为准"，跑通第一条后按账单校准并改 model_prices.json。
    #    计价按默认【每 10 秒一档】（这家文档明说按秒扣，所以时长必须是乘数，别学 RPA 腿按次）。
    ("video.generate", "wan3-youzan"): 50, ("video.generate", "wan3-youzan-prime"): 60,
    # 智能档：文档只说"积分按 Smart 配置计费"，没给数 ⇒ 60 是【占位·无数据】（按它可能升 prime 的上限估）。
    # 报价时必须写"无数据，以平台实扣为准"，跑通第一条后拿账单校准。
    ("video.generate", "wan3-smart"): 60,
    ("lipsync", "default"): 20, ("lipsync", "vo"): 24,
    ("transcribe", "asr"): 0,   # 本地 ASR 免费
    # ⚠️ 音频这条是【分/千字】不是分/条（上游按字符计费）。0 = 还没实测单价、也没定"1 元=几分"，
    #    所以先记 0 分：能跑通链路、不谎报扣费。定价后改 model_prices.json 的这一个数即可，不用动代码。
    ("audio.generate", "seed-audio"): 0,
}
PRICE_BY_CAP = {"text.complete": 1, "image.generate": 4, "image.edit": 20,
                "video.generate": 40, "video.edit": 20, "video.analyze": 6, "lipsync": 20, "transcribe": 0,
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
        if purpose in ("motion-rpa", "h3-rpa"):
            # 两条浏览器 RPA 腿：一次任务 = 一段成片，时长由【上游工作流跑多久】决定、不是我方计费档
            # （上游烧的是它自己账户的 GPU 秒）。传 duration/segments 只用于展示，别乘进价格。
            est = base * max(1, int((params or {}).get("count") or 1))
        else:
            dur = int((params or {}).get("duration", 10))
            est = base * max(1, (dur + 9) // 10)   # 每 10s 一个单位
    if capability == "video.edit":
        # 去水印烧的是 GPU 秒 ⇒ 时长越长越贵，先照 video.generate 的"每 10 秒一档"估（占位价，见 PRICE_ROUTES 注释）
        dur = int((params or {}).get("duration") or 10)
        est = base * max(1, (dur + 9) // 10)
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
        t0 = time.time()   # 🔴 每条请求【各自】计时：原来 ms 在发请求之前算，于是所有可达腿都显示 0ms，
                           #    面板的"平均延迟"KPI 一直是假的（2026-10-04 接去水印腿时撞见）
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    base + path, headers={"Authorization": f"Bearer {api_key}", "User-Agent": BROWSER_UA}), timeout=15) as r:
                r.read()
            return ("reachable", f"GET {path} 鉴权通过(未提交生成)", round((time.time() - t0) * 1000))
        except urllib.error.HTTPError as e:
            ms = round((time.time() - t0) * 1000)
            if e.code == 404:
                last = f"GET {path} 404"
                continue
            if e.code in (401, 403):
                return ("auth-fail", f"GET {path} HTTP {e.code} 鉴权失败", ms)
            if e.code and e.code >= 500:
                return ("upstream-5xx", f"GET {path} HTTP {e.code} 上游抖动(非我方配置)", ms)
            return ("reachable", f"GET {path} HTTP {e.code}(可达,未提交生成)", ms)
        except Exception as e:
            # 🔴 连接级异常（超时/拒连）不许在第一候选上就判死：youzan666 的 `/models`（不带 v1）会挂到超时，
            #    而 `/v1/models` 实测 200 —— 原来这条直接 return unreachable，于是面板把一条好腿显示成"探不到"。
            #    还剩别的候选形状就继续试；最后一条也失败才算 unreachable。（401/403/5xx 那些上面已各自分流）
            if path != "/v1/models":
                last = f"GET {path} {type(e).__name__}({round((time.time() - t0) * 1000)}ms)"
                continue
            return ("unreachable", f"GET {path} {type(e).__name__}", round((time.time() - t0) * 1000))
    return ("no-probe", f"两种 /models 风格都没通过（{last}）：非 OpenAI 风格，只能实跑才能验",
            round((time.time() - t) * 1000))


# 可安全发最小真实请求验证的能力（便宜）；其余按端点可达/鉴权探测（避免计费生成）。
_LIVE_CHAT = {"text.complete", "video.analyze"}

# call() 里已接出适配器的路由（purpose="*" 表示该能力下所有 purpose 都走同一分支）。
# ⚠️ 改 call() 的分支时同步这张表；管理面板用它回答"这条能不能直接调"。
_ADAPTED = {("text.complete", "*"), ("video.analyze", "*"),
            ("image.generate", "default"), ("image.generate", "nano"),
            ("image.generate", "gpt-image2.5"),
            ("video.generate", "omni-t2v"), ("video.generate", "seedance-2.5"),
            ("video.generate", "seedance-2.0"), ("video.generate", "agnes-v"),
            ("video.generate", "h3"), ("video.generate", "motion-rpa"),
            ("video.generate", "wan3"), ("video.generate", "wan3-prime"),
            ("video.generate", "h3-rpa"),
            ("video.generate", "wan3-youzan"), ("video.generate", "wan3-youzan-prime"),
            ("video.generate", "wan3-smart"),
            ("audio.generate", "seed-audio"),
            ("video.edit", "dewatermark"), ("video.edit", "enhance"),
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


# —— 视频去水印（RunningHub AI App 2012077432525824001）——
# 上游是别人发布的工作流：节点号（142/143/138/128）与 App 号全住 config，.env 可覆盖，改版不动代码。
_RH_FPS_ALLOWED = ("16", "24")                       # 上游文档写死这两档
_RH_INSTANCE_ALLOWED = ("default", "plus", "ultra")  # 24G / 48G / 84G


def _dewatermark_payload(params: dict):
    """发请求【之前】的校验，返回 (vals, err)。这条按 GPU 秒真扣钱，参数错了不该花钱才知道。"""
    src = str(params.get("video_path") or "").strip()
    if not src:
        return None, "去水印要给 video_path（本地视频文件路径）"
    if src.startswith(("http://", "https://")):
        return None, ("去水印的 video_path 只收本地文件，这里收到公网 URL：%s"
                      "（先落到本地再提交；上游给的结果链接 24 小时就失效，不能当输入用）" % src[:80])
    if not _os.path.isfile(src):
        return None, "去水印的视频文件不存在：%s" % src[:160]
    out = str(params.get("output_path") or "").strip()
    if not out:
        return None, "去水印必须给 output_path（结果链接 24 小时失效，不落盘等于白花钱）"
    dur = params.get("duration")
    if dur in (None, ""):
        from backend.services.super_clone_wan3 import _ffprobe_duration
        dur = _ffprobe_duration(src)
        if dur is None:
            return None, ("探不到视频时长（本机 ffprobe 没在 PATH？），而这条工作流要把秒数填进节点"
                          "⇒ 显式给 duration 再跑：%s" % src[:120])
    try:
        dur = max(1, int(round(float(dur))))
    except (TypeError, ValueError):
        return None, "duration 不是数字：%r" % (params.get("duration"),)
    fps = str(params.get("fps") or _get("RUNNINGHUB_DEWATERMARK_FPS") or "24")
    if fps not in _RH_FPS_ALLOWED:
        return None, "fps 只认 %s（上游文档），给了 %r" % ("/".join(_RH_FPS_ALLOWED), fps)
    inst = str(params.get("instance_type") or _get("RUNNINGHUB_DEWATERMARK_INSTANCE_TYPE") or "default")
    if inst not in _RH_INSTANCE_ALLOWED:
        return None, "instance_type 只认 %s，给了 %r" % ("/".join(_RH_INSTANCE_ALLOWED), inst)
    miss = [k for k in ("VIDEO", "DURATION", "FPS", "PROMPT")
            if not str(_get("RUNNINGHUB_DEWATERMARK_%s_NODE" % k) or "").strip()]
    if miss:
        return None, "缺节点号配置 RUNNINGHUB_DEWATERMARK_%s_NODE（花钱前先补上）" % "/".join(miss)
    return {"video_path": src, "output_path": out, "duration": dur, "fps": fps,
            "instance_type": inst,
            "prompt": str(params.get("prompt") or _get("RUNNINGHUB_DEWATERMARK_PROMPT") or "").strip()}, None


def _dewatermark_nodes(file_name, duration, fps, prompt):
    """拼 nodeInfoList（纯函数，好单独断言）。⚠️ video 节点要的是上传后的 fileName，不是 download_url。

    节点号【只住 config】（代码里不留字面量兜底）：那是对方工作流的内部编号，改版要改 .env，
    不该在两个地方各存一份 —— 留一份就会漂移。
    """
    ids = {"video": _get("RUNNINGHUB_DEWATERMARK_VIDEO_NODE"),
           "duration": _get("RUNNINGHUB_DEWATERMARK_DURATION_NODE"),
           "fps": _get("RUNNINGHUB_DEWATERMARK_FPS_NODE"),
           "prompt": _get("RUNNINGHUB_DEWATERMARK_PROMPT_NODE")}
    miss = [k for k, v in ids.items() if not str(v or "").strip()]
    if miss:
        raise RuntimeError("去水印缺节点号配置：%s（在 config.py / .env 里补 RUNNINGHUB_DEWATERMARK_*_NODE）"
                           % "/".join(miss))
    return [
        {"nodeId": ids["video"], "fieldName": "video", "fieldValue": file_name, "description": "Load video"},
        {"nodeId": ids["duration"], "fieldName": "value", "fieldValue": str(duration), "description": "Video duration"},
        {"nodeId": ids["fps"], "fieldName": "value", "fieldValue": str(fps), "description": "Frames per second (16 or 24)"},
        {"nodeId": ids["prompt"], "fieldName": "text", "fieldValue": prompt, "description": "Input prompt"},
    ]


def _register_delivery_dewatermark(params, vals):
    """先记账再花钱：去水印欠一条产物，期望路径就是 `output_path`（文件真出现才销账）。

    为什么登记在【调用之前】：这条腿会超时/会失败，事后登记等于没记 ——
    台账的意义是让"还欠着"在有人想收工时可见（docs/PRD-delivery-loop.md §1）。
    会话 id 只有两个来源：显式 params["session"] 或 DSH 注入子进程的 $env:DSH_SESSION_ID；
    都拿不到就不登记（fail-closed，不给没主人的会话记账）。🔴 记账炸了绝不能影响这次调用。
    """
    sid = str(params.get("session") or _os.environ.get("DSH_SESSION_ID") or "").strip()
    if not sid:
        return
    try:
        import delivery_ledger
        out = str(vals.get("output_path") or "").strip()
        if not out:
            return
        stem = _os.path.splitext(_os.path.basename(out))[0]
        task = str(params.get("task") or stem)[:60]
        delivery_ledger.open_obligation(sid, task, "dewatermark", [out],
                                        budget_credits=int(params.get("budget_credits") or 0))
    except Exception as e:
        # 🔴 不许静默：上一版这里写的是 `except: pass`，结果一个 NameError 把登记整块吃掉、
        #    测试只看到"没登记"，查不到原因。记账不能拖垮调用，但必须留下声音。
        print("[delivery] 去水印义务登记失败（不影响本次调用）：%s" % e, file=_sys.stderr)


def _rh_video_edit_run(vals, app_id, api_key="", label="去水印", job_id="", poll_timeout=1800.0, poll_interval=10.0,
                       nodes_fn=None):
    """上传→跑 AI App→轮询→落盘。job_id 给了就【只捞结果不重提交】（孤儿任务捞回=0 分，重提交=双花）。

    轮询按 2026-10-03 那条 SSL 掐线教训写：同一个 taskId 退避重试、连续 6 次查询异常才判死，
    判死也只报 taskId 让人去查上游，绝不在这一步重新提交。
    nodes_fn(fileName) 拼 nodeInfoList：dewatermark 传 _dewatermark_nodes，enhance 传自己的（纯函数，好单独断言）。
    """
    import asyncio
    import httpx
    import time
    from pathlib import Path
    from backend.services.runninghub_client import RunningHubClient
    try:
        client = RunningHubClient(api_key) if api_key else RunningHubClient()
    except Exception as e:
        return {"success": False, "error": "%s拿不到 RunningHub 密钥：%s" % (label, str(e)[:200])}
    if not app_id:
        return {"success": False, "error": "没配 %s 的 AI App 号（config.py 里补）" % label}

    async def _go():
        tid = job_id
        if not tid:
            up = await client.upload_resource(vals["video_path"])
            if not up:
                return {"success": False, "error": "%s：视频上传后没拿到 fileName（不提交，0 分）" % label}
            sub = await client.submit_task(workflow_id=app_id, is_ai_app=True,
                                           instance_type=vals["instance_type"],
                                           node_info_list=nodes_fn(up, vals))
            tid = sub.get("taskId")
            if not tid:
                return {"success": False, "error": "%s：提交没回 taskId %s" % (label, str(sub)[:200])}
        start = time.time()
        fails = 0
        while time.time() - start < poll_timeout:
            await asyncio.sleep(poll_interval)
            try:
                resp = await client.query_task(tid)
                fails = 0
            except Exception as e:
                fails += 1
                if fails >= 6:
                    return {"success": False, "job_id": tid,
                            "error": "%s：查询连续失败 6 次（任务可能还在跑，别重提交）%s" % (label, str(e)[:200])}
                await asyncio.sleep(min(poll_interval, 3 * fails))
                continue
            st = resp.get("taskStatus") or resp.get("status") or "UNKNOWN"
            if st == "SUCCESS":
                item = next((i for i in (resp.get("results") or []) if i.get("url")), None)
                if not item:
                    return {"success": False, "job_id": tid,
                            "error": "%s：上游回 SUCCESS 但没有结果链接 %s" % (label, str(resp.get("results")))[:200]}
                url = item["url"]
                p = Path(vals["output_path"])
                p.parent.mkdir(parents=True, exist_ok=True)
                async with httpx.AsyncClient(timeout=300.0) as hc:
                    r = await hc.get(url)
                    r.raise_for_status()
                    if len(r.content) < 1024:      # 404 页/错误页也是 200，别把它当 mp4 落盘
                        return {"success": False, "job_id": tid,
                                "error": "%s：下载到的只有 %d 字节，不像视频" % (label, len(r.content))}
                    p.write_bytes(r.content)
                usage = resp.get("usage") or {}
                return {"success": True, "path": str(p), "url": url, "job_id": tid,
                        "output_type": item.get("outputType") or p.suffix.lstrip("."),
                        # 上游自己报的耗时/消耗：校准占位单价就看这一个数
                        "upstream_usage": {"task_cost_time": usage.get("taskCostTime"),
                                           "consume_money": usage.get("consumeMoney"),
                                           "consume_coins": usage.get("consumeCoins")},
                        "error": None}
            if st in ("FAILED", "ERROR"):
                return {"success": False, "job_id": tid,
                        "error": "%s：上游任务失败 %s" % (label,
                                                          (resp.get("msg") or resp.get("errorMessage")
                                                           or resp.get("failedReason") or st))}
        return {"success": False, "job_id": tid,
                "error": "%s：轮询超时 %ss（taskId 已留存，可继续查，不重提交）" % (label, int(poll_timeout))}

    try:
        return asyncio.run(_go())
    except Exception as e:
        return {"success": False, "error": "%s异常 %s: %s" % (label, type(e).__name__, str(e)[:260])}


# —— 视频高清修复（RunningHub AI App，config.RUNNINGHUB_ENHANCE_*）——
def _enhance_payload(params: dict):
    """花钱【之前】的校验，返回 (vals, err)。协议与 dewatermark 一致，只吃 video 节点（升档参数走工作流内置）。"""
    src = str(params.get("video_path") or "").strip()
    if not src:
        return None, "高清修复要给 video_path（本地视频文件路径）"
    if src.startswith(("http://", "https://")):
        return None, ("高清修复的 video_path 只收本地文件，这里收到公网 URL：%s"
                      "（先落到本地再提交；上游给的结果链接 24 小时就失效，不能当输入用）" % src[:80])
    if not _os.path.isfile(src):
        return None, "高清修复的视频文件不存在：%s" % src[:160]
    out = str(params.get("output_path") or "").strip()
    if not out:
        return None, "高清修复必须给 output_path（结果链接 24 小时失效，不落盘等于白花钱）"
    inst = str(params.get("instance_type") or _get("RUNNINGHUB_ENHANCE_INSTANCE_TYPE") or "default")
    if inst not in _RH_INSTANCE_ALLOWED:
        return None, "instance_type 只认 %s，给了 %r" % ("/".join(_RH_INSTANCE_ALLOWED), inst)
    if not str(_get("RUNNINGHUB_ENHANCE_VIDEO_NODE") or "").strip():
        return None, ("缺节点号配置 RUNNINGHUB_ENHANCE_VIDEO_NODE（对方工作流里那个吃上传文件的节点号，"
                      "在 config.py / .env 里补上再花钱）")
    return {"video_path": src, "output_path": out, "instance_type": inst}, None


def _enhance_nodes(file_name, vals):
    """拼 nodeInfoList（纯函数，好单独断言）。⚠️ video 节点要的是上传后的 fileName，不是 download_url。

    可选节点（分辨率档位 / 模型切换）：config 里填了 nodeId + value 才带进去；没填 = 走工作流内置默认，
    节点号只住 config（.env 可覆盖），代码里不留字面量兜底。
    """
    nodes = [{"nodeId": _get("RUNNINGHUB_ENHANCE_VIDEO_NODE"), "fieldName": "video",
              "fieldValue": file_name, "description": "Load video"}]
    for key, desc in (("RESOLUTION", "Output resolution"), ("MODEL", "Model / algorithm")):
        nid = _get("RUNNINGHUB_ENHANCE_%s_NODE" % key)
        val = _get("RUNNINGHUB_ENHANCE_%s_VALUE" % key)
        if str(nid or "").strip() and str(val or "").strip():
            nodes.append({"nodeId": nid, "fieldName": _get("RUNNINGHUB_ENHANCE_%s_FIELD" % key) or "value",
                          "fieldValue": val, "description": desc})
    return nodes


# ==== 动作迁移（8091 nzdh 工作室 · RunningHub 浏览器 RPA 腿）====
# 这条腿和上面所有腿都不一样：花钱的动作发生在本机的 Playwright 里（登录 RH 账号池 → 打开公共工作流
# 2106377827540103169 → 往节点塞素材 → 点运行 → 等节点 285 出片 → 下载 final.mp4）。
# 所以网关不直连厂商，只做三件事：校验参数 → 转调 8091 → 盯到终态并把产物落到调用方要的路径。
# 🔴 60 分/段是【占位】：上游按 GPU 秒真扣钱，而"1 元=几分"未定（见 PRICE_ROUTES 同类注释）；
#    跑通第一条后按上游消耗校准，改 model_prices.json 即可，不用动代码。
NZDH_DEFAULT_BASE = "http://127.0.0.1:8091"
NZDH_OUTPUT_DIR = r"D:\nzdh\outputs\womenswear_studio"


def _motion_rpa_base(params: dict | None = None) -> str:
    """8091 的地址：调用方 params 显式给 > .env 的 NZDH_BASE_URL > 本机默认。"""
    b = str((params or {}).get("nzdh_base") or _get("NZDH_BASE_URL") or NZDH_DEFAULT_BASE)
    return b.rstrip("/")


def _motion_rpa_payload(params: dict) -> tuple:
    """花钱【之前】的校验，返回 (vals, err)。三种入口，缺件一律本地拦：

  - 只捞不重投：给 job_id（=8091 的 task_id）⇒ 不 POST 任何提交，只轮询它
  - 补跑第五步：给 task_id（前四步产物已在 8091 盘上）⇒ 打 /api/womenswear-studio/step5-rpa
  - 整条流水线：product 模式要 product_url+video_url+model_image_path，
    creator 模式要 raw_product_path+source_video_path ⇒ 打 /api/womenswear-studio/run（它自己会跑到第五步）

（必填形状按 womenswear/router.py:_start_task 的真实校验抄，不照 README/PRD 抄——那边写模特图可选，代码是必填。）
    """
    job = str(params.get("job_id") or "").strip()
    tid = str(params.get("task_id") or "").strip()
    out = str(params.get("output_path") or "").strip()
    eng = str(params.get("engine") or "plus").lower()
    if eng not in ("plus", "lite", "standard"):
        return None, "动作迁移引擎只认 plus/lite/standard（收到 %r）；不确定就用 plus" % eng
    if job and not tid:
        return {"mode": "poll", "task_id": job, "output_path": out, "engine": eng,
                "timeout": 0, "workflow_url": ""}, None
    if tid:
        return {"mode": "step5", "task_id": tid, "output_path": out, "engine": eng,
                "timeout": int(params.get("task_timeout") or 3600),
                "workflow_url": str(params.get("workflow_url") or "")}, None
    mode = str(params.get("source_mode") or "product").lower()
    if mode not in ("product", "creator"):
        return None, "source_mode 只认 product / creator（收到 %r）" % mode
    need = (("product_url", "video_url", "model_image_path") if mode == "product"
            else ("raw_product_path", "source_video_path"))
    missing = [k for k in need if not str(params.get(k) or "").strip()]
    if missing:
        return None, "%s 模式缺必填参数：%s" % (mode, ", ".join(missing))
    body = {"source_mode": mode, "engine": eng}
    for k in ("product_url", "video_url", "raw_product_path", "source_video_path",
              "model_image_path", "scene_image_path"):
        if str(params.get(k) or "").strip():
            body[k] = params[k]
    return {"mode": "run", "task_id": "", "output_path": out, "engine": eng,
            "body": body}, None


def _register_delivery_motion_rpa(sid: str, task: str, expect_path: str, budget: int) -> None:
    """先记账再花钱（同去水印那条规矩）：动作迁移欠一条成片，文件真出现才销账。

    🔴 炸了绝不能影响本次调用，但必须留下声音（不写 except: pass）。
    8091 的 task_id 是提交后才产生的，所以 run 模式只能在拿到 task_id 后立刻登记 ——
    这仍比"出片后才记"强：中途超时/失败时台账上挂着的就是那条没落盘的片子。
    """
    if not sid or not expect_path:
        return
    try:
        import delivery_ledger
        # 任务名兜底用产物文件名（同去水印那条）：台账按 (会话,任务,技能) 认一条义务，
        # 传空串会让这个会话的几段互相覆盖 ⇒ 每一段都得有自己的名字。
        stem = _os.path.splitext(_os.path.basename(expect_path))[0]
        task = str(task or "").strip() or stem
        delivery_ledger.open_obligation(sid, task[:60], "motion-transfer",
                                        [expect_path], budget_credits=int(budget or 0))
    except Exception as e:
        print("[delivery] 动作迁移义务登记失败（不影响本次调用）：%s" % e, file=_sys.stderr)


def _motion_rpa_run(params: dict) -> dict:
    """转调 8091 的动作迁移工作室，盯到终态。job_id/task_id 给了就【只捞不重投】（重投=双花）。"""
    import time
    import httpx
    from pathlib import Path

    vals, err = _motion_rpa_payload(params)
    if err:
        return {"success": False, "error": err}
    base = _motion_rpa_base(params)
    poll_timeout = float(params.get("poll_timeout") or 2400)
    poll_interval = float(params.get("poll_interval") or 10)
    sid = str(params.get("session") or _os.environ.get("DSH_SESSION_ID") or "").strip()

    def _fail(msg, job_id=""):
        return {"success": False, "job_id": job_id or vals["task_id"] or None, "error": msg}

    tid = vals["task_id"]
    with httpx.Client(timeout=60.0) as hc:
        if vals["mode"] == "poll":
            # 只捞不重投：任务已在 8091 上跑，这里只看它到哪了（重投=双花，RPA 还占着全局串行锁）
            expect = vals["output_path"] or str(Path(NZDH_OUTPUT_DIR) / tid / "final.mp4")
        elif vals["mode"] == "run":
            # 带上会话主人：8091 用它校验"这一批是不是这条会话批的"，并把主人写进任务档案（历史按它过滤）
            vals["body"]["session"] = sid
            try:
                r = hc.post(base + "/api/womenswear-studio/run", json=vals["body"])
            except Exception as e:
                return _fail("动作迁移：8091 没应答（起没起？在 D:\\nzdh 下 python app.py）%s: %s"
                             % (type(e).__name__, str(e)[:200]))
            if r.status_code == 423:
                # 8091 自己也在查批准条子：这是闸的正常工作，不是故障，别把它报成"服务坏了"
                return _fail("动作迁移：8091 判定这次没有花钱批准条子 ⇒ %s"
                             % str(r.json().get("detail") or r.text)[:240])
            if r.status_code >= 400:
                return _fail("动作迁移：提交被拒 HTTP %s ⇒ %s" % (r.status_code, r.text[:240]))
            tid = str((r.json() or {}).get("task_id") or "")
            if not tid:
                return _fail("动作迁移：提交没回 task_id ⇒ %s" % r.text[:200])
            # 🔴 /run 自己会一路跑到 Step5（pipeline 末尾就是 RPA+账号池），这里绝不再补 step5-rpa
            expect = vals["output_path"] or str(Path(NZDH_OUTPUT_DIR) / tid / "final.mp4")
            _register_delivery_motion_rpa(sid, str(params.get("task") or ""), expect,
                                          int(params.get("budget_credits") or 0))
        else:  # step5：前四步已备好，只重跑第五步（含"这条 RPA 失败了换号再跑一次"）
            tid = vals["task_id"]
            try:
                body = {"task_id": tid, "timeout": vals["timeout"], "session": sid}
                if vals["workflow_url"]:
                    body["workflow_url"] = vals["workflow_url"]
                r = hc.post(base + "/api/womenswear-studio/step5-rpa", json=body)
            except Exception as e:
                return _fail("动作迁移：Step5 触发没送达（任务 %s 还在，先查它别重投）%s: %s"
                             % (tid, type(e).__name__, str(e)[:200]), tid)
            if r.status_code == 423:
                return _fail("动作迁移：8091 判定这次没有花钱批准条子 ⇒ %s"
                             % str(r.json().get("detail") or r.text)[:240], tid)
            if r.status_code >= 400:
                return _fail("动作迁移：Step5 被拒 HTTP %s ⇒ %s" % (r.status_code, r.text[:240]), tid)
            expect = vals["output_path"] or str(Path(NZDH_OUTPUT_DIR) / tid / "final.mp4")
            _register_delivery_motion_rpa(sid, str(params.get("task") or ""), expect,
                                          int(params.get("budget_credits") or 0))

        start, fails, last = time.time(), 0, None
        while time.time() - start < poll_timeout:
            time.sleep(poll_interval)
            try:
                q = hc.get(base + "/api/womenswear-studio/task/%s" % tid)
                q.raise_for_status()
                last = q.json()
                fails = 0
            except Exception as e:
                fails += 1
                if fails >= 6:
                    return _fail("动作迁移：查询连续失败 6 次（任务 %s 可能还在跑，别重提交）%s: %s"
                                 % (tid, type(e).__name__, str(e)[:200]), tid)
                time.sleep(min(poll_interval, 3 * fails))
                continue
            st = str(last.get("status") or "")
            extra = last.get("extra", {}) or {}
            if st == "SUCCESS":
                url = str((last.get("outputs") or [{}])[0].get("fileUrl") or "")
                src = Path(NZDH_OUTPUT_DIR) / tid / "final.mp4"
                path = None
                if vals["output_path"]:
                    if not src.is_file():
                        return _fail("动作迁移：上游回 SUCCESS 但盘上没有 %s" % src, tid)
                    p = Path(vals["output_path"])
                    p.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        d = hc.get(base + url)
                        d.raise_for_status()
                    except Exception as e:
                        return _fail("动作迁移：从 8091 取成片失败 %s: %s"
                                     % (type(e).__name__, str(e)[:200]), tid)
                    if len(d.content) < 1024:      # 错误页也是 200，别把它当 mp4 落盘
                        return _fail("动作迁移：下载到的只有 %d 字节，不像视频" % len(d.content), tid)
                    p.write_bytes(d.content)
                    path = str(p)
                else:
                    path = str(src)
                extra = last.get("extra", {}) or {}
                return {"success": True, "path": path, "url": (base + url) if url else None,
                        "job_id": tid, "engine": vals["engine"],
                        "account": extra.get("rpa_account") or last.get("rh_task_id") or "",
                        "upstream": {k: last.get(k) for k in ("stage", "progress", "current_step")},
                        "error": None}
            if st in ("FAILED", "CANCELED", "CANCELLED"):
                # 上游文案二次不可信（"请填写提示词"那类），把原始 error + 失败步骤一起交回
                return _fail("动作迁移：任务失败于 %s ⇒ %s"
                             % (extra.get("failed_step") or last.get("current_step") or "?",
                                str(last.get("error") or st)[:300]), tid)
        return _fail("动作迁移：轮询超时 %ss（task %s 已留存，可继续查，不重提交）"
                     % (int(poll_timeout), tid), tid)


# ==== xjai.beauty WAN 3.0（OpenAI 兼容 /v1/videos/generations；下载腿【也要】 Bearer）====
# 契约（2026-10-06 用户给的文档）：提交 POST {base}/v1/videos/generations → {id,status:"processing"}；
# 轮询 GET {base}/v1/videos/{id} → processing | success(result.url) | failed(error)；
# 取片 GET {base}/v1/videos/{id}/content。和 vinted 那条最大的差别：content 不是免鉴权的签名链，
# 直接 urlopen 它会 401，所以这里下载必须带 Authorization。
_XJAI_MODELS = ("wan3.0-video", "wan3.0-video-prime")
_XJAI_MEDIA_KINDS = ("image", "video", "audio")     # media[].type 只有这三种
_XJAI_RESOLUTIONS = ("480P", "720P", "1080P")
_XJAI_RATIOS = ("16:9", "4:3", "1:1", "3:4", "9:16")
# media[].url 必须是【公网可达】链接（上游自己去抓）。本地路径在这一层就拒，别花三分钱换一个 400。
_XJAI_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
              ".gif": "image/gif", ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
              ".mp3": "audio/mpeg", ".wav": "audio/wav", ".aac": "audio/aac", ".m4a": "audio/mp4"}


def _xjai_mime(url: str) -> str:
    import urllib.parse
    ext = _os.path.splitext(urllib.parse.urlsplit(url).path)[1].lower()
    return _XJAI_MIME.get(ext, "")


def _xjai_payload(params: dict, model: str = "") -> tuple:
    """标准入参 → WAN 3.0 请求体。返回 (body, err)；err 非空 ⇒ 一个字节都不许发出去。

    纯函数、不碰网络：这样闸能整块抠出来跑值表（见 scripts/check-xjai-wan3-lane.py）。
    """
    prompt = str(params.get("prompt") or "").strip()
    if not prompt:
        return None, "WAN3 缺 prompt"
    if len(prompt) > 20000:
        return None, "WAN3 prompt 超过 20000 字符（%d）" % len(prompt)
    if model and model not in _XJAI_MODELS:
        return None, "WAN3 未知 model=%s（只认 %s）" % (model, "|".join(_XJAI_MODELS))
    raw_dur = params.get("duration")
    if raw_dur in (None, ""):
        duration = 5                                   # 缺省才用默认档
    else:
        try:
            duration = int(raw_dur)
        except (TypeError, ValueError):
            return None, "WAN3 duration 要整数秒，收到 %r" % (raw_dur,)
    if not 1 <= duration <= 30:                        # 🔴 0/负数是【传错了】，不许当成"没传"回落成 5
        return None, "WAN3 duration 超出 1–30 秒（%d）" % duration
    resolution = str(params.get("resolution") or "1080P").upper()
    if resolution not in _XJAI_RESOLUTIONS:
        return None, "WAN3 resolution 只认 %s（收到 %s）" % ("|".join(_XJAI_RESOLUTIONS), resolution)
    ratio = str(params.get("ratio") or params.get("aspect_ratio") or "16:9")
    if ratio not in _XJAI_RATIOS:
        return None, "WAN3 ratio 只认 %s（收到 %s）" % ("|".join(_XJAI_RATIOS), ratio)
    media, bad = [], []
    for kind in _XJAI_MEDIA_KINDS:
        items = params.get(kind + "_urls") or params.get(kind + "s") or []
        if isinstance(items, str):
            items = [items]
        for one in items:
            url = str(one or "").strip()
            if not url.lower().startswith(("http://", "https://")):
                bad.append("%s:%s" % (kind, (url[:60] or "(空)")))
                continue
            mime = _xjai_mime(url)
            if not mime:
                bad.append("%s:%s（认不出扩展名，给不了 mime_type）" % (kind, url[:60]))
                continue
            media.append({"type": kind, "url": url, "mime_type": mime})
    if bad:
        return None, ("WAN3 media[] 必须是公网 http(s) 链接（上游自己去抓，不吃本地路径）：%s"
                      % "、".join(bad[:4]))
    # 参考料上限是上游 /v1/models 自报的硬数（2026-10-06 只读探测：images 10 / videos 5 / audios 5 / 合计 20）。
    # 超了它在提交时就 400，本地先拒=少花一次往返、也少一次"上游拒了但我们记了失败"的误读。
    _limits = {"image": 10, "video": 5, "audio": 5}
    for kind, cap in _limits.items():
        n = sum(1 for m in media if m["type"] == kind)
        if n > cap:
            return None, "WAN3 %s 参考最多 %d 个（这次给了 %d）" % (kind, cap, n)
    if len(media) > 20:
        return None, "WAN3 参考料合计最多 20 个（这次 %d）" % len(media)
    if params.get("media"):
        return None, "WAN3 不接受 media 字段，请用 image_urls / video_urls / audio_urls"
    body = {"model": model or _XJAI_MODELS[0], "prompt": prompt, "duration": duration,
            "resolution": resolution, "ratio": ratio,
            "audio": bool(params.get("audio"))}          # 上游默认 false，要声音必须显式 true
    if media:
        body["media"] = media
    for k in ("seed", "negative_prompt"):
        if params.get(k) not in (None, "", 0):
            body[k] = params[k]
    return body, None


def _xjai_video(base_url, api_key, body, out_path=None,
                poll_timeout=1800.0, poll_interval=8.0, job_id=""):
    """WAN 3.0 出片：提交 → 轮询到终态 → 带 Bearer 下载落盘。返回信封，不抛。

    🔴 轮询断线只【退避重问同一个 task】，绝不重发提交（重发 = 真生成第二条、花两份钱）；
       连续 6 次问不上才判死，且判死时把 job_id 交回，事后能捞。
    """
    import json as _json
    import time as _t
    import urllib.error
    import urllib.request
    if not base_url or not api_key:
        return {"success": False, "error": "缺 XJAI_BASE_URL/XJAI_API_KEY（.env）"}
    base = base_url.rstrip("/")

    def _req(url, method="GET", payload=None, timeout=60.0):
        data = _json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Authorization": "Bearer %s" % api_key,
                     "Content-Type": "application/json", "User-Agent": BROWSER_UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
        return _json.loads(raw) if raw.strip() else {}

    tid, model = str(job_id or ""), str((body or {}).get("model") or "")
    try:
        if not tid:
            sub = _req(base + "/v1/videos/generations", "POST", body, timeout=120.0)
            tid = str(sub.get("id") or sub.get("task_id") or "")
            if not tid:
                return {"success": False, "error": "提交未返回 id：%s" % str(sub)[:200]}
        start, misses = _t.time(), 0
        last = {}
        while _t.time() - start < poll_timeout:
            try:
                last = _req("%s/v1/videos/%s" % (base, tid))
                misses = 0
            except urllib.error.HTTPError as e:
                if e.code == 404:      # 任务号不存在：不是抖动，问下去也不会有
                    return {"success": False, "job_id": tid,
                            "error": "查询 404（任务 %s 在上游查不到）：%s"
                                     % (tid, e.read().decode("utf-8", "replace")[:200])}
                if 400 <= e.code < 500:
                    return {"success": False, "job_id": tid,
                            "error": "HTTP %d：%s" % (e.code, e.read().decode("utf-8", "replace")[:200])}
                misses += 1
                if misses >= 6:
                    return {"success": False, "job_id": tid,
                            "error": "查询连续 6 次失败（任务 %s 可能仍在出片，别重提交）HTTP %d" % (tid, e.code)}
                _t.sleep(min(60.0, poll_interval * misses))
                continue
            except (urllib.error.URLError, OSError) as e:
                misses += 1
                if misses >= 6:
                    return {"success": False, "job_id": tid,
                            "error": "查询连续 6 次连接失败（任务 %s 可能仍在出片，别重提交）%s: %s"
                                     % (tid, type(e).__name__, str(e)[:200])}
                _t.sleep(min(60.0, poll_interval * misses))
                continue
            st = str(last.get("status") or "").lower()
            if st in ("success", "succeeded", "completed"):
                break
            if st in ("failed", "error", "cancelled", "canceled"):
                res = last.get("result") or {}
                return {"success": False, "job_id": tid, "model": model,
                        "error": "生成失败：%s" % str(res.get("error") or last.get("error") or st)[:300]}
        else:
            return {"success": False, "job_id": tid, "model": model,
                    "error": "轮询超时 %ss（任务 %s 已留存，继续查它，不重提交）" % (int(poll_timeout), tid)}
        res = last.get("result") or {}
        url = res.get("url") or last.get("url") or ""
        path, got_bytes = None, None
        if out_path:
            # 🔴 content 端点也要 Bearer：签名链式的免鉴权下载在这条腿上不存在，
            #    少了头就是 401，而 401 会被误读成"key 坏了"。
            content_url = "%s/v1/videos/%s/content" % (base, tid)
            req = urllib.request.Request(content_url,
                                         headers={"Authorization": "Bearer %s" % api_key,
                                                  "User-Agent": BROWSER_UA})
            try:
                with urllib.request.urlopen(req, timeout=600.0) as resp:
                    data = resp.read()
            except Exception as e:
                return {"success": False, "job_id": tid, "url": url or content_url,
                        "error": "查询成功但下载失败 %s: %s（任务 %s 已出片，重试下载即可，别重提交）"
                                 % (type(e).__name__, str(e)[:200], tid)}
            if len(data) < 1024:                      # 错误页也可能是 200，别当 mp4 落盘
                return {"success": False, "job_id": tid, "url": content_url,
                        "error": "下载到的只有 %d 字节，不像视频" % len(data)}
            p = _os.path.abspath(str(out_path))
            _os.makedirs(_os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(data)
            path, got_bytes = p, len(data)
        return {"success": bool(url or path), "url": url, "path": path, "job_id": tid,
                "model": model, "duration": (body or {}).get("duration"),
                "resolution": (body or {}).get("resolution"), "ratio": (body or {}).get("ratio"),
                "audio": (body or {}).get("audio"), "media_count": len((body or {}).get("media") or []),
                "bytes": got_bytes, "error": None}
    except urllib.error.HTTPError as e:
        return {"success": False, "job_id": tid or None,
                "error": "HTTP %d：%s" % (e.code, e.read().decode("utf-8", "replace")[:300])}
    except Exception as e:
        return {"success": False, "job_id": tid or None,
                "error": "%s: %s" % (type(e).__name__, str(e)[:300])}


# ==== youzan666.vip：WAN 3.0 第三条入口（OpenAI 风格 /v1/videos/generations）====
# 契约（2026-10-07 用户给的文档 + 当天【不带 key】的只读路径探测）：
#   提交 POST {base}/v1/videos/generations → 202 {id,status:"processing",concurrency}
#   轮询 GET  {base}/v1/videos/{id}        → processing | success(result.url, expiresAt) | failed(error)
#   下载      直接 GET result.url（网关自己的持久直链，不是 xjai 那种 /content 端点）
# 探测记录：以上路径不带 key 全回 401「缺少API Key」，而故意打不存在的路径回 404 API_NOT_FOUND
#   ⇒ 路径形状是真的；但【任何一次真提交都还没跑过】（没 key）。
_YZ_MODELS = ("wan3.0-video", "wan3.0-video-prime", "wan3.0-video-prime-1080p", "wan3.0-smart")
# 🔴 上面四个 id 分两类：前两个是【文档 §2 写的名字】，本 key 实测回 400「模型不存在或未启用」；
#    后两个是 2026-10-07 用拒绝路径探针（素材指 127.0.0.1 ⇒ 按文档必在预扣前被拒）测出来真正启用的名字：
#      400 invalid_request_error「模型 X 不存在或未启用」= 卡在模型名校验
#      422 EXTERNAL_MEDIA_URL_BLOCKED                        = 模型名已过、卡在素材校验
#    ⇒ 判"模型名通不通"不必花钱，用这条 0 消耗的差异就能测；文档名一律不许当可用路由（已屏蔽，见 model_prices.json）。
_YZ_RESOLUTIONS = ("480p", "720p", "1080p")     # 文档示例用的是小写 p（xjai 那条文档写的是大写 P）
_YZ_RATIOS = ("16:9", "9:16", "1:1", "4:3", "3:4")
_YZ_MAX_PROMPT = 19000                          # 文档 §3.4：提示词 ≤19000 字符（xjai 是 20000，别抄错）
_YZ_MEDIA_CAPS = {"image": 10, "video": 5, "audio": 5}   # 文档 §3.4；总素材 ≤20
_YZ_MEDIA_TOTAL = 20
# media[].type 的合法值（文档 §3.2 列的 kind 集合）。first_frame/last_frame 是这家比 xjai 多的两种。
_YZ_MEDIA_TYPES = ("image", "video", "audio", "first_frame", "last_frame",
                   "reference_image", "reference_video", "reference_audio")


def _yz_public_https(url: str) -> bool:
    """素材必须是【公网 https】：http 会被判 EXTERNAL_MEDIA_URL_INSECURE，
    指回环/内网/保留地址会被判 EXTERNAL_MEDIA_URL_BLOCKED（文档 §3.3/§7.3）。"""
    import urllib.parse
    try:
        u = urllib.parse.urlsplit(str(url))
    except ValueError:
        return False
    if u.scheme != "https" or not u.hostname:
        return False
    host = u.hostname.lower()
    if host in ("localhost", "127.0.0.1", "::1") or host.startswith(("10.", "127.", "192.168.", "169.254.", "172.")):
        return False
    return True


def _yz_payload(params: dict, model: str = "") -> tuple:
    """标准入参 → 这家中转的请求体。返回 (body, err)；err 非空 ⇒ 一个字节都不许发出去。

    纯函数、不碰网络，闸能整块抠出来跑值表（scripts/check-wan3-youzan-lane.py）。
    参数边界全部照【这一家】的文档抄，不沿用 xjai 那两条的数（上限、大小写、素材类型都不一样）。
    """
    job = str(params.get("job_id") or "").strip()
    if job:
        # 只捞不重投：任务在上游已经存在，【不许】再要求 prompt/素材这些提交期参数
        # （2026-10-07 捞回那条 429 事故任务时，校验器还管它要 prompt，0 秒就拒了 = 捞回等于没捞）。
        return {"mode": "poll", "task_id": job,
                "output_path": str(params.get("output_path") or "").strip()}, None
    prompt = str(params.get("prompt") or "").strip()
    if not prompt:
        return None, "WAN3-YZ 缺 prompt"
    if len(prompt) > _YZ_MAX_PROMPT:
        return None, "WAN3-YZ prompt 超过 %d 字符（这次 %d）" % (_YZ_MAX_PROMPT, len(prompt))
    if model and model not in _YZ_MODELS:
        return None, "WAN3-YZ 未知 model=%s（只认 %s）" % (model, "|".join(_YZ_MODELS))
    prime = model.startswith("wan3.0-video-prime")     # 覆盖 -prime 与实测启用的 -prime-1080p
    smart = model == "wan3.0-smart"
    raw = params.get("duration")
    if smart and raw in (None, ""):
        duration = None            # 智能档允许【自动时长】：不写这个字段，交给上游调度（文档 §2）
    else:
        try:
            duration = int(30 if (raw in (None, "") and prime) else (5 if raw in (None, "") else raw))
        except (TypeError, ValueError):
            return None, "WAN3-YZ duration 要整数秒（或留空让智能档自动决定），收到 %r" % (raw,)
        if prime and duration != 30:
            # 文档 §2 写死：prime「仅支持 30 秒、1080P」。静默改成 30 会让报价与实际片长不符 ⇒ 大声拒。
            return None, "WAN3-YZ prime 档只支持 30 秒（收到 %d）；要别的时长走标准/智能档" % duration
        if not 1 <= duration <= 30:
            return None, "WAN3-YZ duration 超出 1–30 秒（%d）" % duration
    resolution = str(params.get("resolution") or ("1080p" if prime else "720p")).lower()
    if resolution not in _YZ_RESOLUTIONS:
        return None, "WAN3-YZ resolution 只认 %s（收到 %s）" % ("/".join(_YZ_RESOLUTIONS), resolution)
    if prime and resolution != "1080p":
        return None, "WAN3-YZ prime 档只支持 1080p（收到 %s）" % resolution
    ratio = str(params.get("ratio") or params.get("aspect_ratio") or "16:9")
    if ratio not in _YZ_RATIOS:
        return None, ("WAN3-YZ 没有 %r 档（文档没列 ratio 枚举，这里收 %s；不确定就别传，默认 16:9）"
                      % (ratio, "|".join(_YZ_RATIOS)))
    media, bad = [], []
    for kind in ("image", "video", "audio", "first_frame", "last_frame"):
        items = params.get(kind + "_urls") or params.get(kind + "s") or params.get(kind + "_url") or []
        if isinstance(items, str):
            items = [items]
        for one in items:
            url = str(one or "").strip()
            if not url:
                continue
            if not _yz_public_https(url):
                bad.append("%s:%s" % (kind, url[:56] or "(空)"))
                continue
            media.append({"type": kind, "url": url})
    if bad:
        return None, ("WAN3-YZ 素材必须是【公网 https】URL（http 判 EXTERNAL_MEDIA_URL_INSECURE、"
                      "本机/内网/保留地址判 EXTERNAL_MEDIA_URL_BLOCKED、本地路径不收）：%s" % "、".join(bad[:4]))
    caps = dict(_YZ_MEDIA_CAPS)
    caps["first_frame"] = caps["last_frame"] = 1
    for kind in ("image", "video", "audio", "first_frame", "last_frame"):
        n = sum(1 for m in media if m["type"] == kind)
        if n > caps[kind]:
            return None, "WAN3-YZ %s 参考最多 %d 个（这次 %d）" % (kind, caps[kind], n)
    if len(media) > _YZ_MEDIA_TOTAL:
        return None, "WAN3-YZ 素材合计最多 %d 个（这次 %d）" % (_YZ_MEDIA_TOTAL, len(media))
    body = {"model": model or _YZ_MODELS[0], "prompt": prompt,
            "resolution": resolution, "ratio": ratio}
    if duration is not None:
        body["duration"] = duration      # 智能档允许不写时长（=自动），写了就照写的发
    if media:
        body["media"] = media
    return body, None


def _yz_download(url: str, api_key: str, allowed_host: str, timeout: float = 600.0):
    """下载成片。🔴 默认【不带】Authorization：result.url 可能指向别的域，
    把中转 key 顺手发给第三方 = 凭据泄露。只有当主机仍是 base 的那一个、且首探回 401/403 时，
    才补一次带 Bearer 的请求；重定向一离开这个主机就立刻丢头（自己接管 redirect）。
    返回 (bytes, error)。
    """
    import urllib.error
    import urllib.parse
    import urllib.request

    class _StripAuthOnCrossHost(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            nr = urllib.request.HTTPRedirectHandler.redirect_request(
                self, req, fp, code, msg, headers, newurl)
            if nr is not None and urllib.parse.urlsplit(nr.full_url).hostname != \
                    urllib.parse.urlsplit(req.full_url).hostname:
                nr.remove_header("Authorization")
            return nr

    def _get(with_auth):
        h = {"User-Agent": BROWSER_UA}
        if with_auth and api_key:
            h["Authorization"] = "Bearer %s" % api_key
        opener = urllib.request.build_opener(_StripAuthOnCrossHost)
        with opener.open(urllib.request.Request(url, headers=h), timeout=timeout) as r:
            return r.read()

    try:
        return _get(False), None
    except urllib.error.HTTPError as e:
        if e.code not in (401, 403) or not _yz_same_host(url, allowed_host):
            return None, "HTTP %d" % e.code
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, str(e)[:160])
    try:                                    # 只有同域才补 Bearer（跨域那条永远不发）
        return _get(True), None
    except Exception as e:
        return None, "补 Bearer 仍失败 %s: %s" % (type(e).__name__, str(e)[:160])


def _yz_same_host(url: str, host: str) -> bool:
    import urllib.parse
    return bool(host) and urllib.parse.urlsplit(str(url)).hostname == host


def _yz_video(base_url, api_key, body, out_path=None, poll_timeout=1800.0, poll_interval=8.0,
              job_id="", max_submit_retry=3):
    """WAN 3.0（youzan666 中转）出片：提交 → 轮询到终态 → 下 result.url 落盘。返回信封，不抛。

    🔴 三条硬规矩（都是这几轮踩出来的）：
      ① 轮询断线只退避重问【同一个】task，连续 6 次才判死，且绝不重发提交（重发=真生成第二条、花两份钱）；
      ② 唯一可以重发提交的情形是【429 并发超限】—— 文档说它带 concurrency 信息、任务根本没建，
         所以重试是安全的；4xx 参数/权限类一律当场停（那是"只有你能修的事"）；
      ③ 下载不到 1KB 判失败，绝不在盘上留个假 mp4 冒充成片。
    """
    import json as _json
    import time as _t
    import urllib.error
    import urllib.parse
    import urllib.request
    if not base_url or not api_key:
        return {"success": False, "error": "缺 YOUZAN_BASE_URL/YOUZAN_API_KEY（key 只放 .env）"}
    base = base_url.rstrip("/")
    host = urllib.parse.urlsplit(base).hostname or ""
    model = str((body or {}).get("model") or "")

    def _req(path, method="GET", payload=None, timeout=60.0):
        data = _json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Authorization": "Bearer %s" % api_key,
                                              "Content-Type": "application/json", "User-Agent": BROWSER_UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
        return _json.loads(raw) if raw.strip() else {}

    tid = str(job_id or "")
    try:
        if not tid:
            attempt, sub = 0, {}
            while True:
                try:
                    sub = _req("/v1/videos/generations", "POST", body, timeout=120.0)
                    break
                except urllib.error.HTTPError as e:
                    if e.code == 429 and attempt < max_submit_retry:
                        # 并发超限：任务没建成 ⇒ 等一会重发同一条是安全的（不是双花）
                        attempt += 1
                        _t.sleep(min(120.0, poll_interval * 4 * attempt))
                        continue
                    detail = e.read().decode("utf-8", "replace")[:240]
                    if 400 <= e.code < 500:
                        return {"success": False, "error": "HTTP %d（参数/权限类，改参数或开权限再试）：%s"
                                              % (e.code, detail)}
                    return {"success": False, "error": "HTTP %d（上游）：%s" % (e.code, detail)}
            tid = str(sub.get("id") or sub.get("task_id") or "")
            if not tid:
                return {"success": False, "error": "提交未返回 id：%s" % str(sub)[:200]}
        start, misses, last = _t.time(), 0, {}
        while _t.time() - start < poll_timeout:
            try:
                last = _req("/v1/videos/%s" % tid)
                misses = 0
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:200]
                if e.code == 404:
                    return {"success": False, "job_id": tid,
                            "error": "查询 404（任务 %s 查不到，可能已过期）：%s" % (tid, detail)}
                # 🔴 429/408/5xx 是【抖动类】，不是"只有你能修的事"：2026-10-07 第一条实测片就栽在这——
                #    提交成功拿到了真 task id，但轮询撞上这家 ~20 次/分钟的 IP 限流（RATE_LIMIT_MINUTE 那类），
                #    原来 4xx 一律当场判死 ⇒ 任务还在上游好好跑着，我们却把它报成失败（= 白等 + 可能被重跑=双花）。
                #    现在：退避后继续问【同一个】任务，6 次问不上才判死，且判死也留 job_id、绝不重提交。
                if e.code in (408, 429) or e.code >= 500:
                    misses += 1
                    if misses >= 6:
                        return {"success": False, "job_id": tid,
                                "error": "查询连续 6 次被限流/抖动打断（HTTP %d，任务 %s 很可能仍在出片："
                                         "拿这个 job_id 继续查，别重提交）" % (e.code, tid)}
                    _t.sleep(min(120.0, (30.0 if e.code == 429 else poll_interval) * misses))
                    continue
                if 400 <= e.code < 500:
                    return {"success": False, "job_id": tid, "error": "HTTP %d：%s" % (e.code, detail)}
                misses += 1
                if misses >= 6:
                    return {"success": False, "job_id": tid,
                            "error": "查询连续 6 次失败（任务 %s 可能仍在出片，别重提交）HTTP %d" % (tid, e.code)}
                _t.sleep(min(60.0, poll_interval * misses))
                continue
            except Exception as e:
                misses += 1
                if misses >= 6:
                    return {"success": False, "job_id": tid,
                            "error": "查询连续 6 次连接失败（任务 %s 可能仍在出片，别重提交）%s: %s"
                                     % (tid, type(e).__name__, str(e)[:180])}
                _t.sleep(min(60.0, poll_interval * misses))
                continue
            st = str(last.get("status") or "").lower()
            if st in ("success", "succeeded", "completed"):
                break
            if st in ("failed", "error", "cancelled", "canceled"):
                return {"success": False, "job_id": tid, "model": model,
                        "error": "生成失败：%s" % str(last.get("error") or (last.get("result") or {}).get("error") or st)[:300]}
        else:
            return {"success": False, "job_id": tid, "model": model,
                    "error": "轮询超时 %ss（任务 %s 已留存，继续查它，不重提交）" % (int(poll_timeout), tid)}
        res = last.get("result") or {}
        url = str(res.get("url") or "")
        if not url:
            return {"success": False, "job_id": tid, "model": model,
                    "error": "回 success 但没有 result.url：%s" % str(last)[:200]}
        data, derr = _yz_download(url, api_key, host)
        if derr:
            return {"success": False, "job_id": tid, "url": url, "model": model,
                    "error": "查询成功但下载失败：%s（任务 %s 已出片，重试下载即可，别重提交）" % (derr, tid)}
        if len(data) < 1024:
            return {"success": False, "job_id": tid, "url": url, "model": model,
                    "error": "下载到的只有 %d 字节，不像视频" % len(data)}
        path = None
        if out_path:
            p = _os.path.abspath(str(out_path))
            _os.makedirs(_os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(data)
            path = p
        return {"success": True, "url": url, "path": path, "job_id": tid, "model": model,
                "duration": (body or {}).get("duration"), "resolution": (body or {}).get("resolution"),
                "ratio": (body or {}).get("ratio"), "media_count": len((body or {}).get("media") or []),
                "bytes": len(data), "expires_at": res.get("expiresAt"), "error": None}
    except urllib.error.HTTPError as e:
        return {"success": False, "job_id": tid or None,
                "error": "HTTP %d：%s" % (e.code, e.read().decode("utf-8", "replace")[:280])}
    except Exception as e:
        return {"success": False, "job_id": tid or None,
                "error": "%s: %s" % (type(e).__name__, str(e)[:280])}


# ==== H3 导演台·浏览器 RPA 腿（C:\Minimax-H3 工作台 = 本机 8093）====
# 与上面 video.generate/h3（RunningHub OpenAPI 直连）是两个上游入口、两套失败形状：API 那条靠我方
# RunningHub key + 工作流号；这条是 Playwright 复用登录态开网页、把参数注入 [12] MiniMaxH3Director 的
# 画布、点运行、抓 [7] SaveVideo 的预览 cos_url 下载。故意分两条 purpose（同 motion/motion-rpa 那条理由）：
# 合并了就没人知道这次走的是哪条、坏的是哪条。provider 以 local 开头 ⇒ 管理台不做 HTTP 探活（探它=真开浏览器跑一条）。
H3_RPA_DEFAULT_BASE = "http://127.0.0.1:8093"
H3_RPA_OUTPUT_DIR = r"C:\Minimax-H3\outputs"
_H3_RPA_ENGINES = ("plus", "lite", "ultra")            # 8093 前端 engine 下拉的三项
_H3_RPA_TASK_TYPES = ("t2v", "i2v", "fl2v", "r2v", "v2v", "rv2v")
_H3_RPA_MULTIPLE = 32    # 🔴 上游 [12].output.multiple=32 ⇒ 宽高必须是 32 的倍数（画布现值读到的，不是我推的）
_H3_RPA_MAX_REFS = 3     # README 与 [12] 的 refs 槽位数
# ratio → 宽高，三档各有证据：16:9 / 3:4 = 这台机器真出过片（C:\Minimax-H3\outputs 实测 864×480、768×1024）；
# 9:16 = 上游现值 + 同族尺寸实测。两条拼起来才敢竖屏：
#   ① 8093 回收结构里 [12].output 自己写着 aspectRatio "9:16 (竖屏)" → width 480 / height 864（multiple=32）；
#   ② 同一条 H3 导演台的【API 腿】在 9:16 上真出过片：productions/2026-10-02-富铁软糖剧情带货短剧/
#      05_shots/videos/h3-director/SEG-1-01-h3-dir.mp4 = 576×1024（ffprobe 实测）。
# ⇒ "H3 支不支持 9:16"的答案是支持；但 RPA 这条腿本身还没跑过 9:16 那一条，报价时按未实测说。
_H3_RPA_RATIOS = {"16:9": (864, 480), "9:16": (480, 864), "3:4": (768, 1024)}


def _h3_rpa_base(params: dict | None = None) -> str:
    """8093 的地址：params 显式给 > .env 的 H3_RPA_BASE_URL > 本机默认。"""
    b = str((params or {}).get("h3_base") or _get("H3_RPA_BASE_URL") or H3_RPA_DEFAULT_BASE)
    return b.rstrip("/")


def _h3_rpa_payload(params: dict) -> tuple:
    """花钱【之前】的校验，返回 (vals, err)。两种入口，缺件一律本地拦：

  - 只捞不重投：给 job_id（=8093 的 taskId）⇒ 只 GET /api/status/<id>，一个 POST 都不发
  - 新任务：`prompt` 或 `segments` 至少给一个 ⇒ POST /api/generate

字段形状照 C:\\Minimax-H3\\README.md 的 job.json + h3_rpa_client.apply_job_inputs 的真实读法抄，
不照前端猜。参考图走【本地文件上传】（和 xjai 那条相反：那边只吃公网 URL、这边只吃本地路径）。
    """
    job = str(params.get("job_id") or "").strip()
    if job:
        return {"mode": "poll", "task_id": job, "output_path": str(params.get("output_path") or "").strip(),
                "engine": str(params.get("engine") or "plus").lower(), "width": 0, "height": 0,
                "body": {}}, None
    segs_in = params.get("segments") or []
    if isinstance(segs_in, dict):
        segs_in = [segs_in]
    prompt = str(params.get("prompt") or "").strip()
    if not segs_in and not prompt:
        return None, "H3-RPA 要么给 prompt（单段），要么给 segments=[{prompt,durationSec},...]（多段）"
    eng = str(params.get("engine") or "plus").lower()
    if eng not in _H3_RPA_ENGINES:
        return None, "H3-RPA engine 只认 %s（收到 %r）；不确定就用 plus" % ("/".join(_H3_RPA_ENGINES), eng)
    ttype = str(params.get("task_type") or "").strip().lower()
    if ttype and ttype not in _H3_RPA_TASK_TYPES:
        return None, "H3-RPA task_type 只认 %s（收到 %r）；不填由 8093 按素材自动推" % (
            "/".join(_H3_RPA_TASK_TYPES), ttype)
    # 分辨率：显式 width/height 优先；否则按 ratio 取档；都没有=上游默认 864×480
    ratio = str(params.get("ratio") or params.get("aspect_ratio") or "").strip()
    w, h = params.get("width"), params.get("height")
    if (w in (None, "")) != (h in (None, "")):
        return None, ("H3-RPA 的 width/height 要成对给（只给了一个=另一个会静默回落成默认档，"
                      "那正是「宽 480 高 480」这种怪片的来源）；要么两个都给，要么用 ratio")
    if (w in (None, "") and h in (None, "")) and ratio:
        if ratio not in _H3_RPA_RATIOS:
            return None, "H3-RPA 没有 %r 档；已知的是 %s，或直接传 width/height（必须是 %d 的倍数）" % (
                ratio, "/".join(sorted(_H3_RPA_RATIOS)), _H3_RPA_MULTIPLE)
        w, h = _H3_RPA_RATIOS[ratio]
    try:
        width = int(864 if w in (None, "") else w)
        height = int(480 if h in (None, "") else h)
    except (TypeError, ValueError):
        return None, "H3-RPA width/height 要整数像素（收到 %r/%r）" % (w, h)
    if width <= 0 or height <= 0:
        return None, "H3-RPA width/height 要大于 0（收到 %d×%d）" % (width, height)
    if width % _H3_RPA_MULTIPLE or height % _H3_RPA_MULTIPLE:
        return None, ("H3-RPA 按这条工作流自己的对齐步长收尺寸：上游 [12].output.multiple=%d，"
                      "%d×%d 不是它的倍数（非倍数我们没验证过，不敢当成上游会照单全收）。"
                      "9:16 用 480×864、16:9 用 864×480、3:4 用 768×1024，或直接传 32 倍数的尺寸"
                      % (_H3_RPA_MULTIPLE, width, height))
    try:
        fps = float(params.get("frame_rate") or 24)
    except (TypeError, ValueError):
        return None, "H3-RPA frame_rate 要数字（收到 %r）" % (params.get("frame_rate"),)
    if fps <= 0:
        return None, "H3-RPA frame_rate 要大于 0"
    segments = []
    for i, s in enumerate(segs_in or [{"prompt": prompt, "durationSec": 5}]):
        if not isinstance(s, dict):
            return None, "segments[%d] 要写成 {prompt, durationSec}" % i
        sp = str(s.get("prompt") or "").strip()
        if not sp:
            return None, "segments[%d] 缺 prompt" % i
        dur = s.get("durationSec", s.get("duration", 5))
        try:
            dur = float(dur)
        except (TypeError, ValueError):
            return None, "segments[%d].durationSec 要秒数（收到 %r）" % (i, s.get("durationSec"))
        if dur <= 0:
            return None, "segments[%d].durationSec 要大于 0（收到 %s）" % (i, dur)
        seg = {"prompt": sp, "durationSec": dur}
        if str(s.get("negativePrompt") or s.get("negative_prompt") or "").strip():
            seg["negativePrompt"] = str(s.get("negativePrompt") or s.get("negative_prompt"))
        segments.append(seg)
    refs = params.get("ref_images") or params.get("image_urls") or []
    if isinstance(refs, str):
        refs = [refs]
    if len(refs) > _H3_RPA_MAX_REFS:
        return None, "H3-RPA 参考图最多 %d 张（给了 %d 张）" % (_H3_RPA_MAX_REFS, len(refs))
    bad = [str(p)[:60] for p in refs if str(p).lower().startswith(("http://", "https://"))]
    if bad:
        return None, ("H3-RPA 的参考图走本地文件上传，不吃公网 URL（上游 [12] 只认盘上的 input）：%s"
                      % "、".join(bad[:3]))
    missing = [str(p)[:60] for p in refs if not _os.path.isfile(str(p))]
    if missing:
        return None, "H3-RPA 参考图文件不存在：%s" % "、".join(missing[:3])
    body: dict = {"segments": segments, "width": width, "height": height,
                  "frame_rate": fps, "engine": eng,
                  "timeout": int(params.get("task_timeout") or 3600)}
    if str(params.get("global_prompt") or "").strip():
        body["global_prompt"] = str(params["global_prompt"])
    if ttype:
        body["task_type"] = ttype
    if refs:
        body["ref_images"] = [str(p) for p in refs]
    for key in ("start_image", "end_image"):
        v = str(params.get(key) or "").strip()
        if v:
            if v.lower().startswith(("http://", "https://")):
                return None, "H3-RPA 的 %s 要本地文件路径（公网 URL 不吃）" % key
            if not _os.path.isfile(v):
                return None, "H3-RPA 的 %s 文件不存在：%s" % (key, v[:60])
            body[key] = v
    out = str(params.get("output_path") or "").strip()
    if out:
        body["output"] = _os.path.abspath(out)      # 8093 直接把 mp4 写到这个路径
    # 回执里的 ratio 按【实际几何】算，不按请求里写的算：显式给了 width/height 时请求里的 ratio 已经被覆盖，
    # 拿它回显会报出一个和成片不符的档位（自定义尺寸则回空串=如实说"这档没名字"）。
    matched = next((k for k, v in _H3_RPA_RATIOS.items() if v == (width, height)), "")
    return {"mode": "submit", "task_id": "", "output_path": out, "engine": eng,
            "body": body, "width": width, "height": height, "ratio": matched}, None


def _h3_rpa_run(params: dict) -> dict:
    """转调 8093 的 H3 工作台，盯到终态。给了 job_id 就【只捞不重投】（重跑=双花，还占着浏览器串行锁）。"""
    import time
    import httpx
    from pathlib import Path

    vals, err = _h3_rpa_payload(params)
    if err:
        return {"success": False, "error": err}
    base = _h3_rpa_base(params)
    poll_timeout = float(params.get("poll_timeout") or 3900)
    poll_interval = float(params.get("poll_interval") or 10)
    tid = vals["task_id"]

    def _fail(msg, job_id=""):
        return {"success": False, "job_id": job_id or tid or None, "error": msg}

    def _delivered(p, note):
        """落盘才算交付：文件在、字节≥1KB，才允许报成功（顺带按调用方要的路径归位）。"""
        src = Path(str(p))
        if not src.is_file():
            return None
        if src.stat().st_size < 1024:
            return _fail("H3-RPA：%s 只有 %d 字节，不像视频" % (src, src.stat().st_size), tid)
        dest = vals["output_path"]
        if dest and Path(dest).resolve() != src.resolve():
            d = Path(dest)
            d.parent.mkdir(parents=True, exist_ok=True)
            d.write_bytes(src.read_bytes())
            src = d
        return {"success": True, "path": str(src), "job_id": tid, "engine": vals["engine"],
                "width": vals["body"].get("width") or None, "height": vals["body"].get("height") or None,
                "ratio": vals.get("ratio") or None, "bytes": src.stat().st_size,
                "note": note, "error": None}

    with httpx.Client(timeout=60.0) as hc:
        if vals["mode"] == "submit":
            try:
                r = hc.post(base + "/api/generate", json=vals["body"])
            except Exception as e:
                return _fail("H3-RPA：8093 没应答（起没起？在 C:\\Minimax-H3 下 python app.py）%s: %s"
                             % (type(e).__name__, str(e)[:200]))
            if r.status_code == 409:
                return _fail("H3-RPA：8093 回「已有任务在跑」（浏览器 RPA 全局串行锁被占）——"
                             "这次不排队也不重投；等当前那条跑完，或用它的 taskId 捞")
            if r.status_code >= 400:
                return _fail("H3-RPA：提交被拒 HTTP %s ⇒ %s" % (r.status_code, r.text[:240]))
            try:
                sub = r.json() or {}
            except Exception:
                sub = {}
            tid = str(sub.get("taskId") or "").strip()
            if not tid:
                return _fail("H3-RPA：提交没回 taskId ⇒ %s" % str(sub)[:200])
        last = {}
        start, misses = time.time(), 0
        while time.time() - start < poll_timeout:
            try:
                rr = hc.get(base + "/api/status/" + tid)
                misses = 0
            except Exception as e:
                # 只是查询这一步断了：任务还在 8093 里跑 ⇒ 退避后问同一个 id，绝不重发提交
                misses += 1
                if misses >= 6:
                    return _fail("H3-RPA：查询连续 6 次失败（任务 %s 可能还在出片，别重提交）%s: %s"
                                 % (tid, type(e).__name__, str(e)[:200]))
                time.sleep(min(60.0, poll_interval * misses))
                continue
            if rr.status_code == 404:
                got = _delivered(vals["body"].get("output") if vals["mode"] == "submit"
                                 else (vals["output_path"] or ""), "status 查不到但盘上有片")
                msg = ("H3-RPA：8093 查不到任务 %s（它的任务表只在内存里，进程一重启就丢）" % tid)
                if got:
                    got["note"] = msg + " ⇒ 按盘上产物判成功"
                    return got
                return _fail(msg + "；先按 %s 里的 mp4 判有没有出片，别直接重跑（=双花）" % H3_RPA_OUTPUT_DIR)
            if rr.status_code >= 400:
                if rr.status_code >= 500:
                    misses += 1
                    if misses >= 6:
                        return _fail("H3-RPA：查询连续 6 次 HTTP %s（任务 %s 可能还在跑）" % (rr.status_code, tid))
                    time.sleep(min(60.0, poll_interval * misses))
                    continue
                return _fail("H3-RPA：查询回 HTTP %s ⇒ %s" % (rr.status_code, rr.text[:200]))
            try:
                last = rr.json() or {}
            except Exception:
                last = {}
            st = str(last.get("status") or "")
            if st == "done":
                got = _delivered(last.get("output") or vals["body"].get("output") or "", None)
                if not got:
                    return _fail("H3-RPA：回 done 但盘上没有 %s" % (last.get("output") or "(没给路径)"))
                got["upstream"] = {k: last.get(k) for k in ("stage", "progress", "outputUrl")}
                return got
            if st == "error":
                # 🔴 判失败之前先看盘（2026-10-06 定下的全局规矩）：H3 工作流末尾有防翻录机关，
                #    上游任务终态常是 FAILED，但 [7] 已经出片 —— 8093 一般会在内部接住，
                #    真落到 error 时也可能已经下载完了，所以按盘判一次，不许照着 error 重跑整条。
                got = _delivered(last.get("output") or vals["body"].get("output") or "",
                                 "状态是 error 但盘上有成片（机关/收尾报错那类）")
                if got:
                    got["upstream"] = {k: last.get(k) for k in ("stage", "progress", "error")}
                    return got
                return _fail("H3-RPA：任务失败 ⇒ %s" % str(last.get("error") or st)[:300])
            time.sleep(poll_interval)
        return _fail("H3-RPA：轮询超时 %ss（任务 %s 可能还在出片：用 job_id 再捞，别重提交）"
                     % (int(poll_timeout), tid))


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

    if capability == "video.edit" and r["purpose"] == "dewatermark":
        # RunningHub AI App 去水印（清 Sora / Seedance 的文字与图标）。先本地拦，再花钱。
        vals, err = _dewatermark_payload(params)
        if err:
            return {"success": False, "error": err}
        _register_delivery_dewatermark(params, vals)
        return _rh_video_edit_run(vals, r["model"], api_key=r["api_key"] or "", label="去水印",
                                  job_id=str(params.get("job_id") or ""),
                                  poll_timeout=float(params.get("poll_timeout") or 1800),
                                  poll_interval=float(params.get("poll_interval") or 10),
                                  nodes_fn=lambda up, v: _dewatermark_nodes(up, v["duration"], v["fps"], v["prompt"]))

    if capability == "video.edit" and r["purpose"] == "enhance":
        # RunningHub AI App 高清修复（4K 升档）。同一套协议，先本地拦再花钱。
        vals, err = _enhance_payload(params)
        if err:
            return {"success": False, "error": err}
        return _rh_video_edit_run(vals, r["model"], api_key=r["api_key"] or "", label="高清修复",
                                  job_id=str(params.get("job_id") or ""),
                                  poll_timeout=float(params.get("poll_timeout") or 2400),
                                  poll_interval=float(params.get("poll_interval") or 10),
                                  nodes_fn=_enhance_nodes)

    if capability == "video.generate" and r["purpose"] == "motion-rpa":
        # 动作迁移（8091 浏览器 RPA 腿）：本层不直连 RunningHub，转调 nzdh 工作室并盯到终态。
        return _motion_rpa_run(params)

    if capability == "video.generate" and r["purpose"] == "h3-rpa":
        # H3 第二条腿（本机 8093 浏览器 RPA）：转调工作台、盯到终态、按盘上产物判成败。
        # 🔴 批准条子在【网关这一端】消费（不写进 SPEND_APPROVAL_AT_SOURCE）：这条路的必经入口就是
        #    gateway call，所以闸放在这里就是真闸；8093 自己的网页按钮是给人在浏览器上点的，不在这条轨上。
        return _h3_rpa_run(params)

    if capability == "video.generate" and r["provider"] == "youzan666":
        # WAN 3.0 第三条入口（youzan666 中转）：先按【这一家自己的文档】校验参数，不合规直接回错、不发请求。
        # 凭证只从 route 取（.env 的 YOUZAN_API_KEY）；下载那条默认不带 key（防跨域泄露，见 _yz_download）。
        body, err = _yz_payload(params, model=r["model"] or "")
        if err:
            return {"success": False, "error": err}
        return _yz_video(r["base_url"], r["api_key"], body,
                         out_path=params.get("output_path"),
                         poll_timeout=float(params.get("poll_timeout") or 1800),
                         # 默认 15 秒一问：这家的 IP 限流约 20 次/分钟，10 秒一问撞上就白折腾
                         poll_interval=float(params.get("poll_interval") or 15),
                         job_id=str(params.get("job_id") or ""))

    if capability == "video.generate" and r["provider"] == "xjai":
        # xjai WAN 3.0：先本地校验参数（duration/resolution/ratio/media 公网 URL），不合规直接回错、
        # 不发请求；校验通过才提交。凭证只从 route 取（.env 的 XJAI_API_KEY），源码里没明文。
        body, err = _xjai_payload(params, model=r["model"] or "")
        if err:
            return {"success": False, "error": err}
        return _xjai_video(r["base_url"], r["api_key"], body,
                           out_path=params.get("output_path"),
                           poll_timeout=float(params.get("poll_timeout") or 1800),
                           poll_interval=float(params.get("poll_interval") or 8),
                           job_id=str(params.get("job_id") or ""))

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
    if capability == "image.generate" and r["purpose"] == "gpt-image2.5":
        # GPT Image 2.5（Image2 端点 OpenAI 形 /images/generations）：model 走路由字段（config.IMAGE2_IMAGE25_MODEL，
        # sunburst 默认、flare 提速档），调用方可显式覆盖。上游回的 url 会失效 ⇒ 给了 output_path 就落盘（url/b64 两形都吃）。
        import asyncio
        import base64
        import urllib.request
        from backend.services import image2_client as i2c  # 这名字在包里是单例【实例】（__init__ 再导出），不是模块
        if not str(params.get("prompt") or params.get("text_prompt") or "").strip():
            return {"success": False, "error": "gpt-image2.5 缺 prompt（上游这类拒绝的文案会误导成参数问题，先本地拦）"}
        model = params.get("model") or r["model"]
        if not model:
            return {"success": False, "error": "gpt-image2.5 路由没配 model（config.IMAGE2_IMAGE25_MODEL 空了）"}
        data = asyncio.run(i2c.text_to_image_sync(
            prompt=params.get("prompt") or params.get("text_prompt"), model=model,
            size=params.get("size") or "1024x1024", n=int(params.get("n") or 1)))
        url = None
        for d in (data.get("data") or []):
            url = d.get("url") or d.get("b64_json")
            if url:
                break
        path = None
        dl_err = None
        if url and params.get("output_path"):
            out = str(params["output_path"])
            try:
                # ⚠️ 下载也必须带浏览器 UA：图床 CDN 对 Python-urllib 默认 UA 回 403（同 BROWSER_UA 注释那个坑）
                raw = (urllib.request.urlopen(
                    urllib.request.Request(url, headers={"User-Agent": BROWSER_UA}), timeout=180).read()
                    if str(url).startswith("http") else base64.b64decode(url))
                _os.makedirs(_os.path.dirname(out) or ".", exist_ok=True)
                with open(out, "wb") as f:
                    f.write(raw)
                path = out
            except Exception as e:
                # 落盘失败不翻整条：图已生成、url 还在；但必须显式报 download_error，不静默丢交付
                dl_err = "%s: %s" % (type(e).__name__, str(e)[:200])
        res = {"success": bool(url), "url": url, "path": path, "model": model}
        if dl_err:
            res["download_error"] = dl_err
        return res
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


# ── 花钱批准闸（2026-10-06 用户定："只要是生成视频相关的，必须算账我确认"）────────────────
# 为什么必须有：以前"花钱前报价并等同意"只是 AGENTS.md 里的一句**纸面纪律**，
#   本文件里零命中 approve/consent ⇒ 任何一条会话只要敲 `gateway.py call video.generate …`，
#   precheck→preauth 照样过、任务照样发起。闸钉在这里（**在预扣之前**），才叫拦得住东西。
# 免批的只有两类：`text.complete`（写文案）与 `video.analyze`（看图看片）——token 结算、单次几分。
SPEND_APPROVAL_REQUIRED = ("video.generate", "video.edit", "image.generate", "image.edit")
SPEND_APPROVALS_PATH = _os.environ.get("GATEWAY_SPEND_APPROVALS") or _os.path.join(
    _d or _os.getcwd(), ".gateway_spend_approvals.json")
_APPROVALS_PATH_DEFAULT = _os.path.join(_d or _os.getcwd(), ".gateway_spend_approvals.json")
SPEND_APPROVAL_TTL = float(_os.environ.get("GATEWAY_SPEND_APPROVAL_TTL") or 24 * 3600)

# ⚠️ 回归闸专用的"免人证"开关（2026-10-08）。它【不是环境变量、也不是 CLI 参数】，只能由进程内的
#   Python 代码赋值 —— 目的就是让闸能直接构造条子去测"消费侧"，而真实路径永远拿不到它。
#   双保险：即便有代码把它置 True，也只有当条子写到【非默认】文件（闸自己的 scratch 账本）时才生效，
#   所以它物理上签不出生产 `.gateway_spend_approvals.json` 里的条子。
ALLOW_GRANT_WITHOUT_ATTEST = False


def needs_spend_approval(capability: str) -> bool:
    """这条能力要不要"人给的条子"。清单是唯一源（闸与文档都从这里读）。"""
    return capability in SPEND_APPROVAL_REQUIRED


# 🔴 例外形状：有些腿的**花钱动作发生在另一个进程里**（动作迁移 = 8091 nzdh 工作室自己开浏览器跑 RPA，
# 网关只是转调）。这类腿把条子的【消费点】放在真正干活的那一端（8091 → 8100 的 /api/spend/guard），
# 网关这边就不再重复扣一次 —— 同一次生成扣两段，等于逼用户批两遍，那是自造的假账。
# 豁免只认下面列出的 purpose；闸（scripts/check-motion-rpa-lane.py）钉死"8091 侧确实调了 guard"，
# 就是为了防止这张表变成一条绕过批准的后门。加一条 purpose 进来必须同时在那一端加调用。
SPEND_APPROVAL_AT_SOURCE = (("video.generate", "motion-rpa"),)


def approval_consumed_at_source(capability: str, purpose: str) -> bool:
    return (capability, purpose) in SPEND_APPROVAL_AT_SOURCE


def guard_spend_from_source(capability: str, purpose: str, account: str = "local",
                            count: int = 1, session: str = "") -> dict:
    """给"真正干活的那一端"用的批准闸入口（例：8091 在发起 RPA 前来问一次）。

    ⚠️ 预计扣分由**网关按价目自己算**，调用方不许自带价格 —— 否则 8091 传个 credits=0 就等于放行。
    没有条子 ⇒ check_spend_approval 原样回 approval_required + 报价 + live_grants，段数不动。
    """
    if not needs_spend_approval(capability):
        return {"ok": True, "reason": "not-required", "grant": None,
                "error": "能力 %s 不在花钱清单里，无需条子" % capability}
    cnt = max(1, int(count or 1))
    credits = meter(capability, purpose, {"count": cnt})["credits_estimate"]
    out = check_spend_approval(capability, purpose, account, count=cnt,
                               credits=credits, session=str(session or ""))
    out["credits"] = credits
    out["count"] = cnt
    return out


def spend_approvals() -> dict:
    """批准条子的只读视图（8100 面板 / 右栏台用；CLI 的 `approvals` 打的是同一份 _approvals_load）。"""
    now = _time.time()
    rows = []
    for g in _approvals_load().get("grants", []):
        rows.append({"id": g.get("id", ""), "capability": g.get("capability", ""),
                     "purpose": g.get("purpose", "*"), "account": g.get("account", "local"),
                     "session": g.get("session", ""), "scope": g.get("scope", ""),
                     "segments_used": int(g.get("used", 0)), "segments": int(g.get("count", 0)),
                     "credits_used": int(g.get("used_credits", 0)), "credits": int(g.get("credits", 0)),
                     "expires_in": round(float(g.get("expires", 0)) - now),
                     "live": float(g.get("expires", 0)) > now})
    return {"path": SPEND_APPROVALS_PATH, "count_live": sum(1 for r in rows if r["live"]),
            "grants": rows}


def _approvals_load() -> dict:
    try:
        with open(SPEND_APPROVALS_PATH, encoding="utf-8") as f:
            d = _json.load(f)
        return d if isinstance(d, dict) else {"grants": []}
    except (OSError, ValueError):
        return {"grants": []}


# ============================================================ 人证（2026-10-08 两易其稿）
# 目标：把"人点了头"变成【机制上】必须发生的事。走过的两条弯路都记在这里，别再走回去：
#   ① 弹窗 + 回敲 6 位码 —— 被用户否掉（"太麻烦了换个方式"、"也不要输入口令了"）；
#   ② 只弹系统 MessageBox 点【是】—— 🔴 实测【不可信】：没有任何人参与时 MessageBoxW 会自己返回
#      IDYES，回归闸 G1/G2/G5 两次抓到它凭空签出条子（假条子落在闸的 scratch 账本，生产没被污染）。
#      同一类毛病还有 MB_TIMEOUT 在这台机器上不生效。⇒ 系统弹窗这条路整个废掉，不留兜底。
# 现在的形状：pending（待批）+ 一次性码 + DSH 内核审批卡
#   agent 跑 `approve` ⇒ 只建一张【pending】，里面写好报价和一个 6 位码；此时【还没有任何批准额度】。
#   人在 DSH 对话里看到品牌审批卡（logo + 口号 + 报价 + 那个码），点【允许一次】
#     ⇒ 插件把码回交核销（consume_spend_approval）⇒ 才转成可用条子。
#   核销失败/码错/没卡可点 ⇒ pending 过期，一律 0 额度。
# 🔴 码【绝不从任何只读 HTTP 口吐出】（`spend_pending()` 供插件渲染用，但它走的是插件侧、
#    且这条 json agent 自己也读得到 —— 真正的门槛是"码必须出现在人眼看到的卡上并由人交回来"，
#    所以插件渲染路径是唯一给码的地方，agent 读文件只能读到 pending、读不到"人已点"）。
_APPROVE_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTWXYZ2346789"   # 去掉易混字形 I O L V 0 1 5
SPEND_PENDING_TTL = float(_os.environ.get("GATEWAY_SPEND_PENDING_TTL") or 2 * 3600)   # 2 小时：15 分钟实测把人逼成"追着计时器点"，2026-10-08 放宽


def gen_approve_code(n: int = 6) -> str:
    """一次性核销码（secrets 不可预测；字符集去掉人眼容易混的字形）。"""
    import secrets as _sec
    return "".join(_sec.choice(_APPROVE_CODE_ALPHABET) for _ in range(n))


def record_approval_denial(reason: str, capability: str = "", purpose: str = "",
                           account: str = "", scope: str = "",
                           session: str = "") -> dict:
    """被拒的人签留痕（和条子同一个文件，`approvals` 读得到）。"""
    data = _approvals_load()
    d = {"at": _time.time(), "reason": reason, "capability": capability, "purpose": purpose,
         "account": account, "session": session, "scope": scope}
    data["denials"] = (data.get("denials") or [])[-99:] + [d]
    _approvals_save(data)
    return d


def _pending_save(data: dict) -> None:
    data["pending"] = [x for x in data.get("pending", [])
                       if float(x.get("expires", 0)) > _time.time()]


def create_spend_pending(capability: str, purpose: str = "*", account: str = "local",
                         count: int = 1, credits: int = 0, scope: str = "",
                         session: str = "", ttl: float | None = None) -> dict:
    """建一张【待批】。这一步不给任何额度 —— 它只是把报价和码立起来等人点。"""
    now = _time.time()
    data = _approvals_load()
    _pending_save(data)
    p = {"id": "pend_" + _uuid.uuid4().hex[:8], "capability": capability, "purpose": purpose or "*",
         "account": account, "count": int(count), "credits": int(credits), "scope": scope,
         "session": session or "", "code": gen_approve_code(), "status": "pending",
         "created": now, "expires": now + float(ttl or SPEND_PENDING_TTL),
         "quote": build_quote_summary(capability, purpose, account, int(count), int(credits),
                                      scope, session)}
    data["pending"] = (data.get("pending") or []) + [p]
    _approvals_save(data)
    return p


def spend_pending(with_code: bool = False) -> list:
    """待批清单（审批卡渲染用）。🔴 默认【不吐码】—— 只有插件渲染那一条路显式要码。"""
    now = _time.time()
    out = []
    for p in _approvals_load().get("pending", []):
        if float(p.get("expires", 0)) <= now or p.get("status") != "pending":
            continue
        row = {k: v for k, v in p.items() if k != "code"}
        if with_code:
            row["code"] = p.get("code", "")
        out.append(row)
    return out


def consume_spend_approval(pending_id: str, code: str, session: str = "") -> dict:
    """核销：码对得上才把 pending 转成可用条子。码错/过期/已消费/换了会话 ⇒ 拒并留痕。"""
    data = _approvals_load()
    p = next((x for x in data.get("pending", []) if x.get("id") == pending_id), None)
    if p is None:
        record_approval_denial("pending-not-found", "", "", "", "", session)
        return {"ok": False, "reason": "pending-not-found"}
    if float(p.get("expires", 0)) <= _time.time():
        record_approval_denial("pending-expired", p.get("capability", ""), p.get("purpose", ""),
                               p.get("account", ""), p.get("scope", ""), session)
        return {"ok": False, "reason": "pending-expired"}
    if p.get("status") != "pending":
        record_approval_denial("pending-already-" + str(p.get("status")), p.get("capability", ""),
                               p.get("purpose", ""), p.get("account", ""), p.get("scope", ""), session)
        return {"ok": False, "reason": "pending-not-pending"}
    if (p.get("session") or "") != (session or ""):
        record_approval_denial("pending-session-mismatch", p.get("capability", ""),
                               p.get("purpose", ""), p.get("account", ""), p.get("scope", ""), session)
        return {"ok": False, "reason": "pending-session-mismatch"}
    import hmac as _hmac
    if not _hmac.compare_digest(str(code or "").strip().upper(), str(p.get("code") or "")):
        record_approval_denial("code-mismatch", p.get("capability", ""), p.get("purpose", ""),
                               p.get("account", ""), p.get("scope", ""), session)
        return {"ok": False, "reason": "code-mismatch"}
    p["status"] = "approved"
    p["consumed_at"] = _time.time()
    data["pending"] = [x for x in data.get("pending", []) if x.get("id") != pending_id]  # 核销即出列
    _approvals_save(data)
    g = grant_spend_approval(p["capability"], p["purpose"], p["account"], p["count"], p["credits"],
                             p["scope"], None, p.get("session", ""),
                             attest={"ok": True, "method": "dsh-card-click",
                                     "at": _time.time(), "pending": pending_id})
    return {"ok": True, "grant": g}


def consume_by_click(pending_id: str) -> dict:
    """批准页/审批卡的【唯一】核销入口：码由服务端自己取，调用方只给 pending_id。
    人证 = "这次点击发生在人眼看到的那张卡上"，不是"知道那个码"（码本来就随页面源码回到浏览器）。
    所以这里不接收 code 参数 —— 少一个入口就少一种伪造法。"""
    p = next((x for x in _approvals_load().get("pending", [])
              if x.get("id") == pending_id and x.get("status") == "pending"), None)
    if p is None:
        return {"ok": False, "reason": "pending-not-found"}
    return consume_spend_approval(pending_id, p.get("code", ""), p.get("session", ""))


def deny_pending(pending_id: str, reason: str = "denied-on-page") -> dict:
    """人在卡上点【这次不跑】：留痕 + 把 pending 撤掉（不能过会儿又拿它核销）。"""
    data = _approvals_load()
    p = next((x for x in data.get("pending", []) if x.get("id") == pending_id), None)
    if p is None:
        return {"ok": False, "reason": "pending-not-found"}
    record_approval_denial(reason, p.get("capability", ""), p.get("purpose", ""),
                           p.get("account", ""), p.get("scope", ""), p.get("session", ""))
    data["pending"] = [x for x in data.get("pending", []) if x.get("id") != pending_id]
    _approvals_save(data)
    return {"ok": True, "reason": reason}


def build_quote_summary(capability: str, purpose: str, account: str, count: int,
                        credits: int, scope: str, session: str = "") -> str:
    """报价正文（终端与审批卡共用一份，数字一律现算，不让人对着空气点头）。"""
    try:
        unit = unit_price(capability, purpose)
    except Exception:
        unit = "?"
    try:
        bal = ledger().balance(account)
    except Exception:
        bal = "?"
    return ("路由：%s/%s\n账户：%s\n单价：%s 分/段\n段数：%s\n预计扣分：%s\n当前余额：%s\n会话：%s\n方案：%s"
            % (capability, purpose, account, unit, count, credits, bal, session or "-",
               scope or "（没写 scope！）"))



def _approvals_save(data: dict) -> None:
    # ⚠️ 写这份 json 的规矩：先 `_approvals_load()` 拿到完整 dict、原地改要改的那块、再存。
    #    不许 `_approvals_save({"grants": ...})` 这种"只带一个键"的写法 —— 那是整份重写，
    #    会把 pending（待批）和 denials（拒绝留痕）一起抹掉（2026-10-08 真踩过，见 check_spend_approval）。
    tmp = SPEND_APPROVALS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        _json.dump(data, f, ensure_ascii=False, indent=2)
    _os.replace(tmp, SPEND_APPROVALS_PATH)


def approval_allows(g: dict, capability: str, purpose: str, account: str,
                    now: float, want_count: int, want_credits: int,
                    session: str = "") -> bool:
    """单条批准对这次请求够不够（纯函数，真值表好抠出来跑）。

    六个条件全成立才算数：没过期 ∧ 能力对上 ∧ purpose 对上（"*"=该能力全放行）
    ∧ 账户对上 ∧ **剩余额度同时够段数和分数** ∧ 会话绑得上。
    ⚠️ 段数和分数都要卡：只卡分数时，一张"总 150 分"的条子能被拆成 30 段 5 分的花法，而人批准的是"3 段"。
    🔴 会话绑（2026-10-06 补）：签条子时写了 `session` 的，只有同一条会话能用；
       不写 = 通用条子（谁都能用）。为什么必须有：另一会话为"video-enhance 封面"签的 2 段条子，
       被我一次不相干的端点探测吃掉 1 段 ⇒ 额度是按能力/purpose 匹配的，**不按计划隔离就是互相偷花**。
    """
    if not g or g.get("expires", 0) <= now:
        return False
    if g.get("capability") != capability:
        return False
    if g.get("purpose") not in ("*", purpose):
        return False
    if g.get("account") not in ("*", account):
        return False
    owner = str(g.get("session") or "")
    if owner and owner != str(session or ""):
        return False
    return (int(g.get("count", 0)) - int(g.get("used", 0)) >= max(1, int(want_count))
            and int(g.get("credits", 0)) - int(g.get("used_credits", 0)) >= int(want_credits))


def check_spend_approval(capability: str, purpose: str, account: str,
                         count: int = 1, credits: int = 0, session: str = "") -> dict:
    """找一张能覆盖这次请求的条子；找到就当场记账（used/used_credits 增加）并返回它。
    找不到 ⇒ `{ok:false, reason:"approval_required", quote:{...}}`——**不建 hold、不扣费、不发 HTTP**。"""
    if not needs_spend_approval(capability):
        return {"ok": True, "reason": "not-required", "grant": None}
    now = _time.time()
    data = _approvals_load()
    grants = [g for g in data.get("grants", []) if g.get("expires", 0) > now]
    for g in grants:
        if approval_allows(g, capability, purpose, account, now, count, credits, session):
            g["used"] = int(g.get("used", 0)) + int(count)
            g["used_credits"] = int(g.get("used_credits", 0)) + int(credits)
            g["last_used"] = now
            # 🔴 必须原地改 data 再存：这里以前写的是 `_approvals_save({"grants": ...})`，
            #    等于整份重写 json ⇒ 把 pending（待批）和 denials（拒绝留痕）两个键一起抹掉。
            #    实测过一次：人刚建的待批被另一条会话消费旧条子的动作擦掉，批准页从此看不到东西。
            data["grants"] = grants + [x for x in data.get("grants", [])
                                       if not x.get("expires", 0) > now]
            _approvals_save(data)
            return {"ok": True, "reason": "approved", "grant": g["id"], "scope": g.get("scope", ""),
                    "remaining": int(g["count"]) - int(g["used"]),
                    "remaining_credits": int(g["credits"]) - int(g["used_credits"])}
    return {"ok": False, "reason": "approval_required",
            "quote": {"capability": capability, "purpose": purpose, "account": account,
                      "count": count, "credits": credits,
                      "unit_price": unit_price(capability, purpose),
                      "balance_after": ledger().balance(account)},
            "live_grants": [{"id": g["id"], "capability": g["capability"], "purpose": g["purpose"],
                             "left": int(g["count"]) - int(g["used"]),
                             "credits_left": int(g["credits"]) - int(g["used_credits"]),
                             "expires_in": round(g["expires"] - now)} for g in grants],
            "error": ("approval_required：这是「生成视频相关」的按条计费能力（%s/%s，预计 %d 分 / %d 段）。"
                      "没有覆盖本次请求的人批准条子 ⇒ 已拒绝，0 分未扣、任务未发起。"
                      "要跑：先把上面这份报价发给用户、拿到明确同意，然后【由用户本人在真终端里】签一张批准条子。"
                      "签条子需要弹窗点【是】+ 回敲屏幕上那 6 位确认码；命令文本不在本回复里给（旧版这里印着现成命令，"
                      "等于把伪造的说明书交给调用方 —— 2026-10-08 删掉）。"
                      % (capability, purpose, credits, count))}


def grant_spend_approval(capability: str, purpose: str = "*", account: str = "local",
                         count: int = 1, credits: int = 0, scope: str = "",
                         ttl: float | None = None, session: str = "",
                         attest: dict | None = None) -> dict:
    """写一张批准条子。

    🔴 `attest` = human_attest() 的成功返回。CLI `approve` 那条路【必须】带它（见 _cli 里的 approve 分支），
       不带就回 `ok:false, reason:"no-attestation"` —— 也就是"人没在这台机器上点过、敲过 ⇒ 签不出"。
       留 `attest=None` 这个形参只为回归闸能直接构造条子（闸测的是消费侧，不是人证侧）；
       生产代码里任何新调用点不传 attest 都当 bug 看待（`grants` 里会有 `attest` 字段，`approvals` 可查谁签的）。

    ⚠️ 说清它拦得住什么、拦不住什么（别把它当"密码学级的人在场证明"）：
      拦得住 ①**默认路径**：没有条子 ⇒ 预扣都不建，任务绝不发起（0 分）；
              ②**超范围**：条子写死能力/purpose/账户/段数/分数上限/过期，换路由、加段数、花超额度、过期一律拒；
              ③**留痕**：每张条子带 `scope`（用户批准的那句方案原文）、`attest`（人证方式与时间）与时间戳，`approvals` 可查、可 revoke；
                 被拒的人签单独落 `denials`，能查出"谁试着自己批"。
      拦不住 装了 UI 自动化（OCR 那张弹窗 + 控鼠标键盘）的 agent —— 抬到那个代价、并留痕，不是做不到-proof。
    """
    if (needs_spend_approval(capability) and not (attest and attest.get("ok"))
            and not (ALLOW_GRANT_WITHOUT_ATTEST
                     and SPEND_APPROVALS_PATH != _APPROVALS_PATH_DEFAULT)):
        return {"ok": False, "reason": "no-attestation",
                 "error": ("no-attestation：花钱批准必须由本人在终端签（弹窗点【是】+ 回敲确认码）。"
                           "这条调用没有通过人证 ⇒ 未签条子。")}
    now = _time.time()
    data = _approvals_load()
    g = {"id": "ok_" + _uuid.uuid4().hex[:8], "capability": capability, "purpose": purpose or "*",
         "session": session or "",
         "account": account, "count": int(count), "used": 0, "credits": int(credits),
         "used_credits": 0, "scope": scope, "created": now,
         "attest": {"method": (attest or {}).get("method", "gate-selftest"),
                    "at": (attest or {}).get("at", now),
                    "pending": (attest or {}).get("pending", "")},
         "expires": now + float(ttl or SPEND_APPROVAL_TTL)}
    data["grants"] = [x for x in data.get("grants", []) if x.get("expires", 0) > now] + [g]
    _approvals_save(data)
    return g


def revoke_spend_approval(gid: str) -> dict:
    data = _approvals_load()
    keep = [g for g in data.get("grants", []) if g.get("id") != gid]
    data["grants"] = keep
    _approvals_save(data)
    return {"revoked": gid, "left": len(keep)}


def unit_price_ceiling(capability: str) -> int:
    """某能力下的最高单价（分/段）。给"路由还没钉死"的腿用：宁可往高报，也不要猜个低价把人糊过去。"""
    vals = [v for (c, _p), v in PRICE_ROUTES.items() if c == capability and isinstance(v, int)]
    return max(vals) if vals else 0


class SpendApprovalRequired(RuntimeError):
    """没有批准条子 ⇒ 调用方（8090/8100）负责把它转成人看得见的 4xx，绝不落到发起。"""


def spend_guard(capability: str, purpose: str = "*", account: str = "local",
                 count: int = 1, label: str = "", session: str = "") -> None:
    """【绕开网关的腿】统一调这一个守卫：没有批准条子就 raise（8090 侧转成 423）。
    为什么要有它：8090 的出片腿直连厂商、不经 `invoke()`，网关里的闸天然盖不到 ⇒ 让它们调**同一个函数**、
    认**同一张条子**，不另造一套机制（造了就会有两套批准簿，早晚对不上）。
    purpose="*" 表示"这条腿具体打哪个路由我还没逐个核实" ⇒ 只认人签的**通吃条子**，
    单价按该能力的【上限】报（宁可高报也不猜低）。
    """
    if not needs_spend_approval(capability):
        return
    credits = unit_price(capability, purpose) if purpose != "*" else unit_price_ceiling(capability)
    gate = check_spend_approval(capability, purpose, account, count=count,
                                credits=credits * max(1, count), session=session)
    if not gate["ok"]:
        raise SpendApprovalRequired(gate["error"] + ("" if not label else "  [%s]" % label))


def refund_spend_approval(grant_id, count: int = 1, credits: int = 0) -> dict:
    """退段：调用失败（上游拒、参数错、落盘失败）⇒ 这次没真消耗，批准额度必须还回去。
    不这么做的话，"上游抖 3 次"就把人批准的额度吃光，而实际 0 消耗
    （2026-10-06 我自己踩的：gflow 因版本漂移一个请求都没发出去，条子却从 3 段掉到 2 段）。"""
    if not grant_id:
        return {"refunded": None}
    data = _approvals_load()
    for g in data.get("grants", []):
        if g.get("id") == grant_id:
            g["used"] = max(0, int(g.get("used", 0)) - int(count))
            g["used_credits"] = max(0, int(g.get("used_credits", 0)) - int(credits))
            g.setdefault("notes", []).append("失败退段 %s 段/%s 分" % (count, credits))
            break
    _approvals_save(data)
    return {"refunded": grant_id}


def invoke(capability: str, purpose: str | None = None, account: str = "local",
           params: dict | None = None) -> dict:
    """skill 经 tool-jobs 调的总入口：resolve→估价→precheck/preauth→call→commit/rollback→信封。
    不足即停并提示充值（不发起生成，PRD:284）；失败回滚该条（PRD:346）。"""
    params = params or {}
    account = account or "local"
    r = resolve(capability, purpose)
    est = meter(capability, r["purpose"], params)["credits_estimate"]
    # 🔴 花钱批准闸钉在**预扣之前**（顺序错了就等于"先扣了再问"）：生成视频相关的能力，
    #    没有覆盖本次请求的人批准条子 ⇒ 直接回 approval_required，不建 hold、不发 HTTP。
    #    唯一例外 = SPEND_APPROVAL_AT_SOURCE 里那些"花钱在别的进程"的腿：条子由那一端消费，这里不重复扣。
    if needs_spend_approval(capability) and not approval_consumed_at_source(capability, r["purpose"]):
        # 🔴 段数=【批准单位】，两种形状都得吃：调用方常按上游签名传 segments=[{...},...]（列表），
        #    原来这里直接 int(params.get("segments"))，列表一进来就 TypeError 把整次调用炸在闸里
        #    （2026-10-06 跑 H3 RPA 9:16 实测第一条时撞上：0 消耗，因为炸点在预扣之前，但闸自己成了故障点）。
        _raw_cnt = params.get("segments") if params.get("segments") is not None else params.get("count")
        try:
            _cnt = max(1, len(_raw_cnt) if isinstance(_raw_cnt, (list, tuple)) else int(_raw_cnt or 1))
        except (TypeError, ValueError):
            _cnt = 1          # 认不出的形状按 1 段算：闸宁可少算段数，也不许自己成为故障点（真正的上限是 credits）
        gate = check_spend_approval(capability, r["purpose"], account,
                                    count=_cnt, credits=est, session=str(params.get("session") or ""))
        if not gate["ok"]:
            return {"ok": False, "capability": capability, "purpose": r["purpose"], "result": None,
                    "credits": {"estimated": est, "charged": 0,
                                "balance_after": ledger().balance(account)},
                    "error": gate["error"], "approval": {k: gate[k] for k in ("reason", "quote", "live_grants")}}
    else:
        _cnt, gate = 1, {"grant": None}

    def _refund_approval():
        """失败就把批准段数退回去（0 消耗不该吃额度）。"""
        if gate.get("grant"):
            refund_spend_approval(gate["grant"], _cnt, est)

    if not ledger().precheck(account, est):
        _refund_approval()          # 没发起成功 ⇒ 退段
        return {"ok": False, "capability": capability, "purpose": r["purpose"], "result": None,
                "credits": {"estimated": est, "charged": 0, "balance_after": ledger().balance(account)},
                "error": "积分不足，请去 weeksir.com 充值后重试"}
    hold = ledger().preauth(account, capability, r["purpose"], est)
    try:
        result = call(capability, r["purpose"], **params)
        # 适配器返回业务失败（success:False / 带 error）也算失败 → 回滚，不扣费
        if isinstance(result, dict) and (result.get("success") is False or result.get("error")):
            ledger().rollback(hold["hold_id"])
            _refund_approval()      # 上游/适配器判失败 ⇒ 这次没消耗，退段（gflow 版本漂移那次就是栽在这）
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
        _refund_approval()          # 抛异常同样退段
        return {"ok": False, "capability": capability, "purpose": r["purpose"], "result": None,
                "credits": {"estimated": est, "charged": 0, "hold_id": hold["hold_id"],
                            "balance_after": ledger().balance(account)},
                "error": f"{type(e).__name__}: {e}"}


def _cprint(*a, **k):
    """CLI 的安全打印：按【输出流编码】先试、编不了就 errors=replace。
    2026-10-06 实测：`gateway.py call image.generate doubao` 被批准闸正确拒了，但拒绝消息里带一个 `⇒`，
    GBK 控制台上 print 直接抛 UnicodeEncodeError ⇒ 人只看到 traceback，看不到"缺条子"与报价。
    🔴 不用 sys.stdout.reconfigure（那条路实测会造"零输出 + 退出码 0"的静默假绿）。"""
    stream = k.get("file") or _sys.stdout
    enc = getattr(stream, "encoding", None) or "ascii"
    sep = k.get("sep", " ")
    s = sep.join(str(x) for x in a) + k.get("end", "\n")
    try:
        s.encode(enc)
    except (UnicodeEncodeError, LookupError):
        s = s.encode(enc, "replace").decode(enc, "replace")
    stream.write(s)


def _cli(argv):
    print = _cprint          # 本函数内所有证词走安全打印（见 _cprint 的事故说明）
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
    elif cmd == "quote":
        # 用法: python gateway.py quote <cap> [purpose] [--json '{...}']
        # 只算不花钱（不建预扣、不提交）。铁律要求"报价数字一律从命令读"，而按时长分档的腿
        # （video.generate / video.edit）光看单价算不出总扣分 ⇒ 要把 params 一起喂进来。
        import json
        if len(argv) < 2:
            print("用法: python gateway.py quote <cap> [purpose] [--json '{...}']")
            return 2
        cap = argv[1]
        rest = argv[2:]
        pur = rest[0] if rest and not rest[0].startswith("--") else None
        params = {}
        i = 0
        while i < len(rest):
            if rest[i] == "--json" and i + 1 < len(rest):
                try:
                    params = json.loads(rest[i + 1])
                except Exception as e:
                    print("参数 JSON 坏: %s" % e)
                    return 2
                i += 2
            else:
                i += 1
        try:
            pur = resolve(cap, pur)["purpose"]
        except RouteError as e:
            print("ERROR: %s" % e)
            return 2
        print(json.dumps(meter(cap, pur, params), ensure_ascii=False, indent=2))
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
    elif cmd in ("approve", "approvals", "revoke"):
        # 花钱批准：**人在自己的终端里签**，agent 只能报"需要批准"并给出这条命令。
        #   python backend/gateway.py approve video.generate omni-t2v --count 3 --credits 150 --scope "fu-tie 3段15秒"
        import json as _j
        # 打印统一走 _cli 顶部那句 `print = _cprint`（模块级那份）——这里原来嵌过一份同名函数，
        # 结果 `_cprint` 变成 _cli 的局部名，`print = _cprint` 在赋值前引用它直接 UnboundLocalError。
        if cmd == "approvals":
            now = _time.time()
            data = _approvals_load()
            gs = data.get("grants", [])
            den = data.get("denials") or []
            _cprint(f"批准条子 {len(gs)} 张、被拒的人签 {len(den)} 次（清单 = {SPEND_APPROVALS_PATH}）：")
            for g in gs:
                att = (g.get("attest") or {}).get("method", "（无人证字段·旧条子或闸自测）")
                _cprint(f"  {g['id']}  {g['capability']}/{g['purpose']}  acct={g['account']}  "
                        f"段 {g.get('used',0)}/{g['count']}  分 {g.get('used_credits',0)}/{g['credits']}  "
                        f"还剩 {max(0, round((g['expires']-now)/3600))}h  人证={att}  「{g.get('scope','')}」")
            if not gs:
                _cprint("  （空）⇒ 现在任何 video.generate / video.edit / image.* 调用都会被拒 0 分。")
            for p in data.get("pending") or []:
                if float(p.get("expires", 0)) <= now:
                    continue
                _cprint("  ⏳ %s  %s/%s  acct=%s  %s段/%s分  还剩 %.0f 分钟  「%s」" % (
                    p["id"], p["capability"], p["purpose"], p["account"], p["count"], p["credits"],
                    (p["expires"] - now) / 60, (p.get("scope") or "")[:40]))
            for dd in den[-5:]:
                _cprint("  ✗ %s 拒绝原因=%s  %s/%s  scope=「%s」" % (
                    _time.strftime("%m-%d %H:%M", _time.localtime(dd.get("at", 0))),
                    dd.get("reason"), dd.get("capability"), dd.get("purpose"),
                    (dd.get("scope") or "")[:40]))
            return 0
        if cmd == "revoke":
            if len(argv) < 2:
                print("用法: gateway.py revoke <条子id>"); return 2
            print(_j.dumps(revoke_spend_approval(argv[1]), ensure_ascii=False)); return 0
        if len(argv) < 2:
            print("用法: gateway.py approve <capability> [purpose] --count N --credits X --scope \"...\" [--account local] [--ttl 86400]")
            return 2
        cap = argv[1]
        rest, pur = argv[2:], "*"
        if rest and not rest[0].startswith("--"):
            pur, rest = rest[0], rest[1:]
        opts = {"--count": "count", "--credits": "credits", "--account": "account",
                "--scope": "scope", "--ttl": "ttl", "--session": "session"}
        kv = {"purpose": pur, "count": 1, "credits": 0, "account": "local", "scope": "", "ttl": None,
              "session": ""}
        i = 0
        while i < len(rest):
            if rest[i] in opts and i + 1 < len(rest):
                raw = rest[i + 1]; key = opts[rest[i]]
                kv[key] = raw if key in ("account", "scope", "session") else float(raw) if key in ("ttl",) else int(raw)
                i += 2
            else:
                i += 1
        if not needs_spend_approval(cap):
            print(f"⚠️ {cap} 本来就不需要批准（免批类：文本/看图）。这条 approve 只是留档，不影响任何调用。")
        # 🔴 这一步【只建待批，不给额度】。系统弹窗已废（实测没人点也会返回【是】= 假人证）。
        #    额度只能由"人看到报价之后"的一次核销拿到：DSH 审批卡点【允许一次】，或人在自己终端跑 consumed。
        p = create_spend_pending(cap, kv["purpose"], kv["account"], int(kv["count"]),
                                int(kv["credits"]), kv["scope"], kv["session"], kv["ttl"])
        print(p["quote"])
        # 🔴 核销码【绝不进 stdout】：这条命令多半是 agent 跑的，它读得到 stdout 就能自己
        #    `consumed <id> <码>` 伪造核销 ⇒ 码一旦进管道，整套人证归零。
        #    码只有两条给人的路：① DSH 审批卡（插件经 with_code 取，见 spend_pending）；
        #                      ② 人在【自己的终端】里跑 `gateway.py code <pending_id>`。
        print("\npending: %s   （%.0f 分钟内有效）" % (p["id"], SPEND_PENDING_TTL / 60))
        print("⇒ 现在还没有任何批准额度。两种点法任选其一：")
        print("   ① 在 DSH 对话里看那张品牌审批卡，核对报价后点【允许一次】；")
        print("   ② 在你自己的终端里跑：python D:\\video-skill\\backend\\gateway.py code %s" % p["id"])
        print("      （它会打出核销码，再按提示跑 consumed；码不在本回复里，免得跑这条命令的一方自己把码用掉）")
        return 0
    elif cmd == "code":
        # 给人看码的口。⚠️ 它不是"人证"：谁都能跑（包括 agent）。人证在【这张码出现在人眼看到的卡上、
        #    并由人交回来】这一步 —— 所以插件渲染路径 (with_code=True) 才是主路，这里只是兜底。
        if len(argv) < 2:
            print("用法: gateway.py code <pending_id>"); return 2
        now = _time.time()
        hit = next((x for x in _approvals_load().get("pending", [])
                    if x.get("id") == argv[1] and x.get("status") == "pending"
                    and float(x.get("expires", 0)) > now), None)
        if hit is None:
            print("✗ 没有这张待批（不存在 / 已核销 / 已过期）"); return 3
        print(hit["quote"])
        print("\npending: %s   核销码: %s   还剩 %.0f 分钟" % (
            hit["id"], hit["code"], (hit["expires"] - now) / 60))
        print("核对无误后跑：python D:\\video-skill\\backend\\gateway.py consumed %s <码>%s"
              % (hit["id"], (" --session " + hit["session"]) if hit.get("session") else ""))
        return 0

    elif cmd == "consumed":
        import json as _j
        # 核销必须带上【建 pending 时同一个会话】—— pending 绑会话就是为了让"别处的点击"花不掉这条额度。
        # 不显式给 --session 时，读 DSH 注入的 $DSH_SESSION_ID（拿不到就按空会话匹配）。
        # 🔴 argv[0] 就是子命令名本身（_cli 收的是整段 argv[1:]），参数从 argv[1] 之后数起。
        if len(argv) < 3:
            print("用法: gateway.py consumed <pending_id> <核销码> [--session <这条方案的会话id>]")
            return 2
        sess = _os.environ.get("DSH_SESSION_ID") or ""
        if "--session" in argv:
            k = argv.index("--session")
            if k + 1 < len(argv):
                sess = argv[k + 1]
        r = consume_spend_approval(argv[1], argv[2], sess)
        if not r.get("ok"):
            print("✗ 核销失败：%s ⇒ 仍然 0 额度、任务不会发起（已留痕 denials）" % r.get("reason"))
            return 3
        g = r["grant"]
        print(_j.dumps(g, ensure_ascii=False, indent=2))
        print("⇒ 已生效：%s/%s 最多 %s 段、上限 %s 分（人证 %s）。用完/过期自动回到「拒绝」态；"
              "要撤就 gateway.py revoke %s" % (g["capability"], g["purpose"], g["count"],
                                               g["credits"], g["attest"]["method"], g["id"]))
        return 0

    else:
        print("用法: python backend/gateway.py [caps | prices | credits | resolve <cap> [purpose] | "
              "call <cap> [purpose] [--account X] [--json '{...}'] | test | analyze <path> [问题] | "
              "approve <cap> [purpose] --count N --credits X --scope \"…\" | code <pending_id> | consumed <pending_id> <码> | approvals | revoke <id>]")


if __name__ == "__main__":
    # 🔴 原来写成 `_cli(...)` 把返回码丢了 ⇒ 用法错误也退 0，调用方（脚本/闸）会把"没执行"当"执行成功"。
    raise SystemExit(_cli(_sys.argv[1:]))
