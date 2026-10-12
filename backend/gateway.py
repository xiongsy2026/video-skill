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
    ("text.complete", "mimo"):      {"provider": "mimo",   "client": "openai_compat",      "fn": "chat",                "fields": {"base_url": "MIMO_BASE_URL",    "api_key": "MIMO_API_KEY",    "model": "TEXT_MODEL_MIMO"}},
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
    ("text.complete", "gpt56"):     {"provider": "nocannobb",  "client": "openai_compat", "fn": "chat", "fields": {"base_url": "GPT55_BASE_URL",          "api_key": "GPT55_API_KEY",   "model": "TEXT_MODEL_GPT56"}},
    # 2026-10-09 超级复刻编译腿：gpt56 走 GPT55_API_KEY（hub 上实测活的那把；GPT56_API_KEY 401 失效），
    # 模型固定 gpt-5.6-sol（超级复刻 Step1.5 编译用的就是它）。
    ("text.complete", "gpt-compile"): {"provider": "nocannobb", "client": "openai_compat", "fn": "chat", "fields": {"base_url": "GPT55_BASE_URL", "api_key": "GPT55_API_KEY", "model": "TEXT_MODEL_GPT56"}},
    ("text.complete", "mimo_pro"):  {"provider": "mimo",       "client": "openai_compat", "fn": "chat", "fields": {"base_url": "MIMO_BASE_URL",           "api_key": "MIMO_API_KEY",    "model": "TEXT_MODEL_MIMO_PRO"}},
    # —— 看图 / 看视频（多模态理解）——
    ("video.analyze", "default"):   {"provider": "agnes",  "client": "agnes",              "fn": "analyze_media",       "fields": {"base_url": "AGNES_BASE_URL",   "api_key": "AGNES_API_KEY",   "model": "AGNES_TEXT_MODEL"}},
    ("video.analyze", "mimo"):      {"provider": "mimo",   "client": "mimo",               "fn": "analyze_media",       "fields": {"base_url": "MIMO_BASE_URL",    "api_key": "MIMO_API_KEY",    "model": "TEXT_MODEL_MIMO"}},
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
    # 🔴 ("video.generate","wan3-dashscope") 已于 2026-10-09【删除】：它的 client 字段一直是
    #    "TODO(无现成 client…)"、fn="-" ⇒ 没有适配器，选了必失败，而且 model 还写着 wan3.0-video-prime
    #    （和 yz-prime 那条同名）= 一个"看起来能选"的假选项。墓碑留在 model_prices.json.disabled，
    #    防有人从价目表把它复活；判据见 scripts/check-wan3-rh-cut.py K10（钉"真删了 + 拒因是无此路由"）。
    ("video.generate", "h3"):            {"provider": "runninghub", "client": "h3_director_client", "fn": "submit_h3_director+poll_and_download", "fields": {"base_url": "RUNNINGHUB_H3_BASE_URL", "api_key": "RUNNINGHUB_ENTERPRISE_API_KEY", "model": None}},
    ("video.generate", "motion"):        {"provider": "runninghub","client": "runninghub_client", "fn": "submit_motion_transfer+poll_task_until_done","fields": {"base_url": "RUNNINGHUB_MOTION_WAN22_BASE_URL", "api_key": "RUNNINGHUB_MOTION_WAN22_API_KEY", "model": None}},
    # —— 动作迁移·第二条腿：【浏览器 RPA】路（Playwright 登录 RH 账号池 → 操作公共工作流
    # 2106377827540103169 → 等节点 285 出片 → 下 final.mp4）。2026-10-08 从 8091（D:\nzdh）剥离进
    # backend/services/motion_rpa/，本层直调本地包。和上面那条 API 腿是两个上游工作流、两套价，
    # 故意不合并：合并了就没人知道这次扣的是哪条。provider 以 local 开头 ⇒ 管理台不做 HTTP 探活
    # （探它 = 真去跑一条工作流、要花钱，探活不许花钱）。
    ("video.generate", "motion-rpa"):    {"provider": "local-motion-rpa","client": "backend.services.motion_rpa", "fn": "submit_run | submit_step5 → 轮询 get_task → 落盘 final.mp4","fields": {"base_url": None, "api_key": None, "model": None}},
    # （2026-10-08 用户点名移除 WAN 3.0 的 xjai.beauty 两条入口 wan3 / wan3-prime，连适配器一起下线）
    # —— H3 的【第二条腿】：本机 8093 的浏览器 RPA 工作台（Playwright 登录 RunningHub → 注入 [12] 画布 →
    # 点运行 → 抓 [7] SaveVideo 下载）。和上面 video.generate/h3（RunningHub OpenAPI 直连）是两个入口、
    # 两套坏法（key 欠费/工作流改版 vs 登录态过期/串行锁被占），所以不合并。
    # provider 以 local 开头 ⇒ 管理台不做 HTTP 探活（探它=真开浏览器跑一条，探活不许花钱）。
    ("video.generate", "h3-rpa"):        {"provider": "local-h3rpa", "client": "gateway._h3_rpa_run", "fn": "POST /api/generate → 轮询 /api/status/<id> → 校验落盘", "fields": {"base_url": "H3_RPA_BASE_URL", "api_key": None, "model": None}},
    # （2026-10-08 用户点名移除 WAN 3.0 的 youzan666.vip 三条入口 wan3-youzan / wan3-youzan-prime / wan3-smart，
    #   连适配器一起下线）
    # —— Prime 独立工作台（prime.youzan666.vip · Wan 3.0 Prime，2026-10-08 新渠道）——
    # 契约（工作台首页 API 文档 + GET /v1/models 参数卡，均 0 消耗只读探测核对过）：
    # 提交 POST /v1/videos/generations {model,prompt,duration,resolution,ratio,media} → 任务号；
    # 轮询 GET /v1/videos/{id}；直链落盘。鉴权 Bearer（也认 X-API-Key）。
    # 计费按【次】（billing_unit=call）：每次成功生成计 1 次、与秒数无关 ⇒ 计价走 meter() 的按次例外。
    # 参考素材六类（首帧/尾帧/参考图/参考视频/参考音频/文件）只吃 HTTPS 外链，见 _yz_prime_*。
    ("video.generate", "wan3-prime"):   {"provider": "yz-prime", "client": "gateway._yz_prime_run",
                                         "fn": "POST /v1/videos/generations → GET /v1/videos/{id} → 落盘",
                                         "fields": {"base_url": "PRIME_YZ_BASE_URL", "api_key": "PRIME_YZ_API_KEY", "model": "PRIME_YZ_MODEL"}},
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
_MODEL_FALLBACK = {"agnes": "agnes-3.0-flash", "mimo": "mimo-v2.6-flash", "zexapi": "gemini-3.5-flash",
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
    ("video.generate", "omni-t2v"): 40, ("video.generate", "agnes-v"): 30,
    ("video.generate", "seedance-2.5"): 60, ("video.generate", "seedance-2.0"): 50,
    ("video.generate", "h3"): 60, ("video.generate", "motion"): 50,
    # ⚠️ 动作迁移 RPA 腿 60 分/段是【占位·未实测】（2026-10-06 用户拍的占位值）：上游 RunningHub 按 GPU 秒真扣钱。
    #    按段计不按秒（一次 RPA = 一段成片），见 meter() 里那条例外。跑通第一条后拿上游消耗校准、改 model_prices.json。
    ("video.generate", "motion-rpa"): 60,
    # H3 的浏览器 RPA 腿：花的是【对方 RunningHub 账户的 GPU 秒】（这台机器登录的那个账号），我方按次记账。
    # 60 = 照同一条 H3 的 API 腿（video.generate/h3=60）借的量级。
    # ✅ 2026-10-06 21:41→21:50 经网关实跑通第一条：1 段 6 秒 / 480×864(9:16) / plus / 墙钟 572.8s /
    #    信封 charged=60 basis=flat（productions/2026-10-06-H3-RPA-9x16链路实测/outputs/h3-rpa-916-01.mp4）。
    # ⚠️ 但 60 仍是【占位·未校准】：链路通了 ≠ 知道对方账户实扣多少人民币 —— 那要拿 RunningHub 账单对。
    ("video.generate", "h3-rpa"): 60,
    # ⚠️ Prime 独立工作台（wan3-prime）按【次】计费（上游 billing_unit=call）：每次成功生成计 1 次、
    #    与视频秒数无关（提交预占 1 次 / 确认失败退回 1 次 / 结果待确认保留预占 —— 那是【平台侧】的次数
    #    口径，我方积分的预扣/回滚照走 invoke() 那套，两边不混账）。
    #    60 分/次 = 【占位·未实测】：照同档视频借的量级，只用于报价；跑完第一条拿 /v1/balance 的次数
    #    消耗校准并改 model_prices.json。
    ("video.generate", "wan3-prime"): 60,
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
        if purpose in ("motion-rpa", "h3-rpa", "wan3-prime"):
            # 两条浏览器 RPA 腿 + Prime 独立工作台：上游按【次/段】计，一次任务 = 一段成片，时长不是我方
            # 计费档（RPA 烧上游自己账户的 GPU 秒、Prime 工作台 billing_unit=call 与秒数无关）。
            # 传 duration 只用于展示/入参，别乘进价格；count>1 = 一次报多条，按条数乘。
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
            # 🔴 连接级异常（超时/拒连）不许在第一候选上就判死：`/models`（不带 v1）可能挂到超时，
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
            ("video.generate", "h3-rpa"), ("video.generate", "wan3-prime"),
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


ANALYZE_ATTEMPTS = 3   # 只读能力，重发不双花；3 次仍不中就照实报上游故障


def _analyze_retryable(err) -> bool:
    """看图看片失败的死因分类（纯函数，闸跑值表）。只放行"再发一次可能成"的形态。

    为什么这里可以重发、而"结果未知不重发"那条禁令不管：video.analyze 是只读能力
    —— 不落产物、不占生成额度、上游失败即 0 token，重发最多是再问一遍，不会双花。
    那条禁令管的是花钱出片的腿（提交成功但结果未知 ⇒ 重发=再买一条）。
    """
    s = str(err or "")
    if not s:
        return False
    if s.startswith("HTTP 4"):        # 参数/鉴权/合规被拒：重发还是拒，别浪费 11MB 上传
        return False
    if s.startswith("HTTP 5"):        # 上游 5xx、中转代理抖（AgnesAI_error/do_request_failed）
        return True
    if s.startswith(("URLError", "Connection", "TimeoutError", "socket.timeout", "OSError",
                     "RemoteDisconnected", "BadStatusLine", "http.client")):
        return True
    low = s.lower()
    return ("上游返回空内容" in s) or ("timed out" in low) or ("请求上游失败" in s)


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
    import time
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
    # 上游对大文件会间歇性回空/500（实测同一个 8.25MB mov：3 成 2 败），一次不中就判死太脆。
    last_err, usage = None, {}
    for attempt in range(ANALYZE_ATTEMPTS):
        try:
            with urllib.request.urlopen(req, timeout=240) as resp:
                out = _json.loads(resp.read().decode("utf-8"))
            text = ((out.get("choices") or [{}])[0].get("message") or {}).get("content")
            usage = out.get("usage") or {}
            if text:
                return {"success": True, "text": text, "error": None, "usage": usage,
                        "attempts": attempt + 1}
            last_err = "上游返回空内容"
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}"
        except Exception as e:  # 连接/超时
            last_err = f"{type(e).__name__}: {e}"
        if attempt + 1 < ANALYZE_ATTEMPTS and _analyze_retryable(last_err):
            time.sleep(3.0 * (attempt + 1))
            continue
        break
    return {"success": False, "text": None,
            "error": f"{last_err}（已试 {attempt + 1}/{ANALYZE_ATTEMPTS} 次，可判定为上游故障而非格式不支持）",
            "usage": usage, "attempts": attempt + 1}


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
    # 🔴 传输层决定"会不会弹浏览器窗"：ui_automation 必须有头（gflow 自己写明 reCAPTCHA 对无头直接 403），
    #    sapisidhash 是 cookie+httpx、抓指纹那次硬编码 headless ⇒ 不弹。默认走后者（config.GFLOW_TRANSPORT）。
    transport = str(_get("GFLOW_TRANSPORT") or "ui_automation").strip()
    if transport and transport != "ui_automation":
        env["GFLOW_CLI_TRANSPORT"] = transport
        env["GFLOW_CLI_EXPERIMENTAL_TRANSPORTS"] = "1"   # 不打开的话 CLI 的 Choice 列表里没这个值
        env["GFLOW_CLI_HEADLESS"] = "true"                # 这条传输本来就不需要可见窗口
    with _BROWSER_LOCK:
        # gflow 自己说这条只读检查"慢网可能要 ~45s" ⇒ 30s 会把【探测超时】误报成【没登录】
        #（2026-10-10 实测踩到：auth status 手工跑退 0，适配器却拿到 code=None）。
        code, so, se, why = _run_cli(exe, ["auth", "status"], 75.0, env=env)      # 先验登录态，别白跑一条生成
        if code is None:
            return {"success": False,
                    "error": "gflow 登录探测没回话（%s）⇒ 不算没登录，也不发起生成。"
                             "先确认到 Google 的网络通不通，再重试同一条。" % (why or "超时")}
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
        # 🔴 这里原来写的是 `_j.loads(...)` —— `_j` 只是本文件里【别的功能】的函数内别名，
        #    在这个函数里从来没绑过 ⇒ NameError 被下面的 `except Exception` 静默吃掉，
        #    于是 gflow 的 `--json` 路径【从来没被用过】，每次都在走"扫目录取最新 png"的兜底
        #    （同目录多会话并发时会拿错文件）。模块级别名是 `_json`。
        found = _dig(_json.loads(so), ("path", "file", "output_path", "local_path", "dest", "saved"))
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
    sid = _session_of(params)
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
                                           node_info_list=nodes_fn(up, vals),
                                           spend_kind="edit")   # 去水印/超分=加工，不是生成视频（2026-10-09 口径）
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


# ==== 动作迁移（motion-rpa · RunningHub 浏览器 RPA 腿）====
# 2026-10-08 从 8091（D:\nzdh 女装工作室）剥离进网关轨：五步工作流在 backend/services/motion_rpa/，
# 这里【直调本地包】，不再有 HTTP 转调、不再要求 8091 起着。
# 花钱的动作发生在本机的 Playwright 里（登录 RH 账号池 → 打开公共工作流 2106377827540103169 →
# 往节点塞素材 → 点运行 → 等节点 285 出片 → 下载 final.mp4）。
# 🔴 批准条子在【网关这一端】invoke() 消费（video.generate ∈ SPEND_APPROVAL_REQUIRED）：
#    8091 时代"条子在 8091 端消费"的 SPEND_APPROVAL_AT_SOURCE 豁免已随剥离撤销
#    （花钱动作和条子现在同进程，只消费一次，用户也只批一次）。
# 🔴 60 分/段是【占位】：上游按 GPU 秒真扣钱，而"1 元=几分"未定（见 PRICE_ROUTES 同类注释）；
#    跑通第一条后按上游消耗校准，改 model_prices.json 即可，不用动代码。


def _motion_rpa_payload(params: dict) -> tuple:
    """花钱【之前】的校验，返回 (vals, err)。三种入口，缺件一律本地拦：

  - 只捞不重投：给 job_id（=任务库里的 task_id）⇒ 不做任何提交，只轮询它
  - 补跑第五步：给 task_id（前四步产物已在盘上）⇒ motion_rpa.submit_step5（只重跑 RPA 那步）
  - 整条流水线：product 模式要 product_url+video_url+model_image_path，
    creator 模式要 raw_product_path+source_video_path ⇒ motion_rpa.submit_run（自己会跑到第五步）

（必填形状按原 8091 womenswear/router.py:_start_task 的真实校验抄，不照 README/PRD 抄——那边写模特图可选，代码是必填。）
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
    task_id 是提交后才产生的，所以 run 模式只能在拿到 task_id 后立刻登记 ——
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
    """本地调 backend/services/motion_rpa 的五步流水线，盯到终态。job_id/task_id 给了就【只捞不重投】（重投=双花）。"""
    import shutil
    import time
    from pathlib import Path

    from backend.services import motion_rpa as mr

    vals, err = _motion_rpa_payload(params)
    if err:
        return {"success": False, "error": err}
    poll_timeout = float(params.get("poll_timeout") or 2400)
    poll_interval = float(params.get("poll_interval") or 10)
    sid = _session_of(params)

    def _fail(msg, job_id=""):
        return {"success": False, "job_id": job_id or vals["task_id"] or None, "error": msg}

    tid = vals["task_id"]
    if vals["mode"] == "poll":
        # 只捞不重投：任务已在跑，这里只看它到哪了（重投=双花，RPA 还占着全局串行锁）
        pass
    elif vals["mode"] == "run":
        body = dict(vals["body"])
        body["session"] = sid      # 会话主人写进任务档案（历史按它过滤）
        try:
            # 🔴 submit_run 自己会一路跑到 Step5（pipeline 末尾就是 RPA+账号池），这里绝不再补 step5
            tid = mr.submit_run(body)
        except ValueError as e:
            return _fail("动作迁移：%s" % e)
        except Exception as e:
            return _fail("动作迁移：提交异常 %s: %s（若已建任务请用 job_id 捞回，别重投）"
                         % (type(e).__name__, str(e)[:200]))
    else:  # step5：前四步已备好，只重跑第五步（含"这条 RPA 失败了换号再跑一次"）
        tid = vals["task_id"]
        try:
            mr.submit_step5(tid, timeout=vals["timeout"], workflow_url=vals["workflow_url"], session=sid)
        except ValueError as e:
            return _fail("动作迁移：%s" % e, tid)
        except Exception as e:
            return _fail("动作迁移：Step5 触发失败（任务 %s 还在，先查它别重投）%s: %s"
                         % (tid, type(e).__name__, str(e)[:200]), tid)

    if vals["mode"] != "poll":
        expect = vals["output_path"] or str(Path(mr.work_dir_of(tid)) / "final.mp4")
        _register_delivery_motion_rpa(sid, str(params.get("task") or ""), expect,
                                      int(params.get("budget_credits") or 0))

    start, fails, last = time.time(), 0, None
    while time.time() - start < poll_timeout:
        time.sleep(poll_interval)
        try:
            last = mr.get_task(tid, include_logs=False)
            fails = 0
        except Exception as e:
            fails += 1
            if fails >= 6:
                return _fail("动作迁移：查询连续失败 6 次（任务 %s 可能还在跑，别重提交）%s: %s"
                             % (tid, type(e).__name__, str(e)[:200]), tid)
            time.sleep(min(poll_interval, 3 * fails))
            continue
        if last is None:
            # 本地查库查不到 = 这个 job_id 真没有（不是抖动），别挂 2400 秒
            return _fail("动作迁移：查无任务 %s（job_id 认错或这条任务从没建过）" % tid, tid)
        st = str(last.get("status") or "")
        if st == "SUCCESS":
            src = Path(mr.work_dir_of(tid)) / "final.mp4"
            if not src.is_file():
                return _fail("动作迁移：任务回 SUCCESS 但盘上没有 %s" % src, tid)
            size = src.stat().st_size
            if size < 1024:      # 空文件/错误页也可能是"成功"，别把它当 mp4 交出去
                return _fail("动作迁移：成片只有 %d 字节，不像视频（%s）" % (size, src), tid)
            path = str(src)
            if vals["output_path"]:
                p = Path(vals["output_path"])
                p.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(src, p)
                except Exception as e:
                    return _fail("动作迁移：成片拷到 %s 失败 %s: %s（原件还在 %s）"
                                 % (p, type(e).__name__, str(e)[:200], src), tid)
                path = str(p)
            return {"success": True, "path": path,
                    "url": last.get("final_video_url") or None,
                    "job_id": tid, "engine": vals["engine"],
                    "account": last.get("rh_task_id") or "",
                    "upstream": {k: last.get(k) for k in ("stage", "progress", "current_step")},
                    "error": None}
        if st in ("FAILED", "CANCELED", "CANCELLED"):
            # 上游文案二次不可信（"请填写提示词"那类），把原始 error + 失败步骤一起交回
            return _fail("动作迁移：任务失败于 %s ⇒ %s"
                         % (last.get("failed_step") or last.get("current_step") or "?",
                            str(last.get("error") or st)[:300]), tid)
    return _fail("动作迁移：轮询超时 %ss（task %s 已留存，可继续查，不重提交）"
                 % (int(poll_timeout), tid), tid)


# （2026-10-08 用户点名移除）WAN 3.0 的 xjai.beauty 两条（wan3 / wan3-prime）与 youzan666.vip 三条
# （wan3-youzan / wan3-youzan-prime / wan3-smart）连同适配器 _xjai_* / _yz_* 整块下线。


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
不照前端猜。参考图走【本地文件上传】（只吃本地路径，公网 URL 一律拒）。
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


# ==== Prime 独立工作台腿（prime.youzan666.vip · wan3.0-video-prime，2026-10-08 新渠道）====
# 契约（工作台首页 API 文档 + GET /v1/models 参数卡，均 0 消耗只读探测核对过）：
#   提交 POST /v1/videos/generations {model,prompt,duration,resolution,ratio,media} → 任务号；
#   轮询 GET /v1/videos/{id}（不存在回 404 TASK_NOT_FOUND）→ 拿直链落盘；次数余额 GET /v1/balance。
#   鉴权 Authorization: Bearer <key>（也认 X-API-Key）。时长正整数秒缺省 30、本工作台不设上限（上游校验）；
#   分辨率只 1080P；ratio 收 adaptive/16:9/4:3/1:1/3:4/9:16；参考素材六类只吃 HTTPS 外链。
# 🔴 「失效 Key 最多换 5 次（首加 5 次重试，最多 6 把）」是【平台号池自己换上游 Key】的策略 ——
#    它对上游每把 Key 只提交一次、自动换 Key 不重复扣次数；我方只持一把工作台 Key，不自己换 Key、
#    不自己重投（自投 = 跟平台的"提交预占 1 次"对着干 = 双花）。
# 🔴 结果未知不重发：提交/查询拿不到确定答案就报 job_id 留给上层（job_id 只捞不重投，或查 /v1/balance
#    对次数），绝不自动重投。
# 🔴 下载凭据纪律（沿旧 youzan 腿）：首探不带 Authorization；仅 401/403 且直链仍指向 base 主机才补一次
#    Bearer；跨域重定向剥掉 Authorization/X-API-Key（见 _yz_prime_http 的 _StripAuthRedirect）。

_PRIME_YZ_RATIOS = ("adaptive", "16:9", "4:3", "1:1", "3:4", "9:16")
_PRIME_YZ_MEDIA = ("first_frame", "last_frame", "reference_image", "reference_video", "reference_audio", "file")
# 简写参数 → media type：给列表时逐条落成 {type,url}
_PRIME_YZ_ALIAS = {"image_urls": "reference_image", "video_urls": "reference_video", "audio_urls": "reference_audio"}
# 上游状态词（工作台没公布状态枚举，按通用词收）：不在两张表里的（queued/running/…）一律继续等
_PRIME_YZ_OK = ("success", "succeeded", "completed", "complete", "done", "finished", "ok")
_PRIME_YZ_BAD = ("failed", "error", "cancelled", "canceled", "rejected", "expired", "timeout")


def _yz_prime_http(method: str, url: str, headers: dict | None = None, body: bytes | None = None,
                   timeout: float = 60.0):
    """Prime 工作台的【唯一出网口】（回归闸就打这个缝）。返回 (status:int, resp_headers:dict, body:bytes)。

    🔴 跨域重定向剥凭据：跟进重定向时目标主机 ≠ 原主机就不把 Authorization/X-API-Key 递出去。
    HTTPError(4xx/5xx) 不往外抛，翻译成 (code, headers, body) 让上层分流；连接级异常照常抛。
    """
    import urllib.error
    import urllib.parse
    import urllib.request

    host = urllib.parse.urlsplit(url).netloc.lower()

    class _StripAuthRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, new_headers, new_url):
            new = super().redirect_request(req, fp, code, msg, new_headers, new_url)
            if new is not None and urllib.parse.urlsplit(new_url).netloc.lower() != host:
                for bag in (new.headers, new.unredirected_hdrs):   # 大小写不敏感地剥，防 add_header 的首字母规则漏网
                    for k in list(bag):
                        if k.lower() in ("authorization", "x-api-key"):
                            bag.pop(k, None)
            return new

    req = urllib.request.Request(url, data=body, headers=dict(headers or {}), method=str(method).upper())
    opener = urllib.request.build_opener(_StripAuthRedirect)
    try:
        with opener.open(req, timeout=timeout) as r:
            return int(r.status), dict(r.headers or {}), r.read() or b""
    except urllib.error.HTTPError as e:
        return int(e.code), dict(e.headers or {}), (e.read() or b"")


def _yz_prime_payload(params: dict) -> tuple:
    """花钱【之前】的校验，返回 (vals, err)。两种入口：

  - 只捞不重投：给 job_id（=提交回的任务号）⇒ 只 GET /v1/videos/<id>，一个 POST 都不发
  - 新任务：prompt 必给 + output_path 必给 ⇒ POST /v1/videos/generations

字段形状照工作台首页 API 文档 + GET /v1/models 参数卡（0 消耗只读探测核对过），不照前端猜。
参考素材六类（首帧/尾帧/参考图/参考视频/参考音频/文件）只吃 HTTPS 外链（平台自己去拉）。
    """
    job = str(params.get("job_id") or "").strip()
    out = str(params.get("output_path") or "").strip()
    if job:
        if not out:
            return None, "wan3-prime 捞回要给 output_path（片要落到盘上、能出示才算交付）"
        return {"mode": "poll", "task_id": job, "output_path": out,
                "duration": None, "resolution": None, "ratio": None, "body": {}}, None
    prompt = str(params.get("prompt") or "").strip()
    if not prompt:
        return None, "wan3-prime 要给 prompt（文字描述）"
    if not out:
        return None, "wan3-prime 要给 output_path（成片要落到盘上、能出示才算交付）"
    dur_raw = params.get("duration", 30)
    if dur_raw in (None, ""):
        dur_raw = 30
    if isinstance(dur_raw, bool):
        return None, "wan3-prime duration 要正整数秒（收到 %r）" % (dur_raw,)
    try:
        dur_f = float(dur_raw)
    except (TypeError, ValueError):
        return None, "wan3-prime duration 要正整数秒（收到 %r）" % (dur_raw,)
    if dur_f != int(dur_f) or dur_f <= 0:
        return None, "wan3-prime duration 要正整数秒（收到 %r）" % (dur_raw,)
    dur = int(dur_f)                       # 本工作台不设秒数上限，上限由上游校验（参数卡 durations.max=null）
    res_in = params.get("resolution")
    res = str(res_in if res_in not in (None, "") else "1080P").strip().upper().replace(" ", "")
    if res != "1080P":
        return None, "wan3-prime 分辨率只出 1080P（工作台参数卡写死的档）；收到 %r" % (res_in,)
    ratio = str(params.get("ratio") or params.get("aspect_ratio") or "adaptive").strip()
    if ratio not in _PRIME_YZ_RATIOS:
        return None, "wan3-prime ratio 只认 %s（收到 %r）" % ("/".join(_PRIME_YZ_RATIOS), ratio)
    media: list = []

    def _add(mtype, url, where):
        u = str(url or "").strip()
        if not u:
            return None
        if not u.lower().startswith("https://"):
            return "%s 只吃 HTTPS 外链（平台自己去拉，不吃本地路径/HTTP）：%s" % (where, u[:80])
        media.append({"type": mtype, "url": u})
        return None

    for key in ("first_frame", "last_frame"):          # 首/尾帧同名参数，各最多 1 条
        v = params.get(key)
        if v in (None, "", [], ()):
            continue
        if isinstance(v, (list, tuple)):
            if len(v) > 1:
                return None, "wan3-prime %s 最多 1 条（给了 %d）" % (key, len(v))
            v = v[0] if v else ""
        e = _add(key, v, "wan3-prime %s" % key)
        if e:
            return None, e
    for i, m in enumerate(params.get("media") or []):  # 显式列表 [{type, url}, ...]
        if not isinstance(m, dict):
            return None, "wan3-prime media[%d] 要写成 {type, url}" % i
        t = str(m.get("type") or "").strip()
        if t not in _PRIME_YZ_MEDIA:
            return None, "wan3-prime media[%d].type 只认 %s（收到 %r）" % (i, "/".join(_PRIME_YZ_MEDIA), t)
        if t in ("first_frame", "last_frame") and any(x["type"] == t for x in media):
            return None, "wan3-prime %s 最多 1 条" % t
        e = _add(t, m.get("url"), "wan3-prime media[%d]" % i)
        if e:
            return None, e
    for key, t in _PRIME_YZ_ALIAS.items():             # 简写：image_urls/video_urls/audio_urls 逐条落同类
        vals_ = params.get(key)
        if vals_ in (None, "", [], ()):
            continue
        if isinstance(vals_, str):
            vals_ = [vals_]
        for j, u in enumerate(vals_):
            e = _add(t, u, "wan3-prime %s[%d]" % (key, j))
            if e:
                return None, e
    body = {"prompt": prompt, "duration": dur, "resolution": res, "ratio": ratio}
    if media:
        body["media"] = media
    return {"mode": "submit", "task_id": "", "output_path": out,
            "duration": dur, "resolution": res, "ratio": ratio, "body": body}, None


def _yz_prime_download(url: str, dest: str, base_url: str, api_key: str) -> tuple:
    """直链落盘，返回 (path|None, bytes:int, err|None)。凭据纪律见本节头注；<1KB 判失败、不留假 mp4。"""
    import urllib.parse

    def _once(auth):
        hdrs = {"User-Agent": BROWSER_UA}
        if auth:
            hdrs["Authorization"] = "Bearer " + api_key
        return _yz_prime_http("GET", url, hdrs, timeout=180.0)

    st, _, raw = _once(False)                 # 首探不带凭据：直链多半是对象存储签名 URL
    if st in (401, 403):
        same = urllib.parse.urlsplit(url).netloc.lower() == urllib.parse.urlsplit(base_url).netloc.lower()
        if same and api_key:                  # 只有同主机才补一次 Bearer，别把 key 递给外链
            st, _, raw = _once(True)
    raw = raw or b""
    if st != 200:
        return None, 0, "下载回 HTTP %s（%d 字节）" % (st, len(raw))
    if len(raw) < 1024:
        return None, 0, "下载到 %d 字节，不像视频（不落盘，免得盘上躺着假 mp4）" % len(raw)
    from pathlib import Path
    import hashlib
    p = Path(dest)
    p.parent.mkdir(parents=True, exist_ok=True)
    # 🔴 同名不许悄悄覆盖（2026-10-10 现网：v3 成功那次把已经交付给人看过的成片原地盖掉了，
    #    盘上只有一份 mp4、两份历史，台账从此对不上）。先留一份带旧内容哈希的副本，再落新的。
    if p.exists():
        old = p.read_bytes()
        if hashlib.sha256(old).hexdigest()[:8] != hashlib.sha256(raw).hexdigest()[:8]:
            try:
                p.rename(p.with_name(p.name + ".prev-" + hashlib.sha256(old).hexdigest()[:8]))
            except OSError:
                pass                      # 保不住旧的也不许丢掉新的
    p.write_bytes(raw)
    return str(p), len(raw), None


def _yz_prime_reconcile(base: str, api_key: str):
    """0 分对账：读平台自己的次数与最近任务，用来判"这次到底建上没建上、花掉几次"。

    🔴 为什么必须有：这条 lane 上游按【次】计费、失败不计次，而我们过去只有"结果未知【不要重发】"
    一句话 —— 于是要么白丢一条片子（今天 20:15/20:16：平台明明建了任务，我们 5xx 分支没去挖回包里的
    任务号，号丢了就捞不回来），要么靠重投赌一把（真双花风险）。有了对账，两种都不猜。
    拿不到就返回 None ⇒ 上层一律当"停手人工核"，因为【对账口没回话】不等于【没建上】。
    """
    import json
    try:
        st, _, raw = _yz_prime_http("GET", base + "/v1/balance",
                                    {"Authorization": "Bearer " + api_key}, timeout=30.0)
        if st != 200:
            return None
        d = json.loads((raw or b"").decode("utf-8", "replace")) or {}
    except Exception:
        return None
    bal = d.get("balance") or {}
    ts = d.get("tasks") or {}
    rec = {"remaining": bal.get("remainingCalls"), "consumed": bal.get("usedCalls"),
           "reserved": ts.get("reservedCalls"),
           "failed": (ts.get("byStatus") or {}).get("failed"),
           "succeeded": (ts.get("byStatus") or {}).get("succeeded"),
           "recent": {}}
    for t in (d.get("recent") or []):
        tid = str((t or {}).get("id") or "").strip()
        if not tid:
            continue
        b = (t or {}).get("billing") or {}
        rec["recent"][tid] = {"status": str((t or {}).get("status") or "").lower(),
                              "charged": int(b.get("charged_calls") or 0),
                              "reserved": int(b.get("reserved_calls") or 0),
                              "url": (t or {}).get("video_url") or ""}
    return rec


def prime_retry_verdict(kind, task_id, seen_ids, rec, attempts_left):
    """纯判据：失败这一次之后，该【重投】、该【捞回】、还是该【停】。抽成函数是为了能被闸整块抠出来跑值表。

    kind： submit_unknown（连接断/5xx/2xx 但没号，结果未知）｜submit_param（4xx，明确被拒）
          task_failed（上游判失败）｜poll_timeout｜deliver_failed（已出片没取下来）
    🔴 一条红线：只有"对账证明没建上"或"建了但计费 0"才允许重投；其余一律停或捞。
    """
    if kind == "submit_param":
        return "stop", "提交被明确拒（参数/合规/Key）⇒ 重投一样错，要改的是写法"
    if kind in ("poll_timeout", "deliver_failed"):
        return "stop", "任务已建、甚至已出片 ⇒ 只许用 job_id 捞回（重投=再花一次次数）"
    if not isinstance(rec, dict):
        return "stop", "对账口 /v1/balance 没回话 ⇒ 不许把它当成『没建上』，停手人工核次数"
    fresh = [t for t in (rec.get("recent") or {}) if t not in set(seen_ids or ())]
    if kind == "submit_unknown":
        if task_id:
            return "poll", "回包里挖到了任务号 ⇒ 轮询它，别当未知"
        if fresh:
            return "poll:" + fresh[0], ("我方报未知、平台却已建任务 %s ⇒ 去捞它（这就是今天 20:15/20:16 白丢的那两次）"
                                         % fresh[0])
        if int(rec.get("reserved") or 0) > 0:
            return "stop", "平台有预占中任务 ⇒ 多半还在排队，重投就是双花"
        if attempts_left <= 0:
            return "stop", "退避重投已用满（每次对账都显示没建上）⇒ 停，报对账证据"
        return "retry", "平台没有本次任务、consumed 没涨 ⇒ 这次根本没建上，重投不计次"
    if kind == "task_failed":
        mine = (rec.get("recent") or {}).get(str(task_id or ""))
        if not mine:
            return "stop", "查不到这个任务的计费状态 ⇒ 人工对账再决定，不在这里赌"
        if int(mine.get("charged") or 0) > 0:
            return "stop", "任务失败但【已计费】⇒ 再来一次就是真花钱，先报对账"
        if attempts_left <= 0:
            return "stop", "退避重投已用满（实测这条 lane 要试几次才成）"
        return "retry", "任务判失败且计费 0 ⇒ 上游没扣次数，同参数重投是免费的"
    return "stop", "未知 kind（宁停不猜）"


def _yz_prime_once(params: dict, route: dict | None, base: str, api_key: str, model: str) -> dict:
    """提交一次 → 轮询到终态 → 落盘。【单次尝试】的判据，重试环在 _yz_prime_run 里。

    三条红线（本节头注）：不自己换 Key、结果未知不重发（除非对账证明没建上）、下载不乱递凭据。
    成功信封带 calls:1 —— 上游每次成功生成计 1 次、与秒数无关（billing_unit=call）。
    """
    import json
    import time

    vals, err = _yz_prime_payload(params)
    if err:
        return {"success": False, "kind": "submit_param", "error": err}
    # 🔴 调用方不许自带 model（2026-10-03 的"mini→h3 马甲"教训）：路由里写死是哪个模型，
    #    收下来的 model 不一致就大声拒，绝不"悄悄按别的模型跑"。
    if params.get("model") and str(params.get("model")) != model:
        return {"success": False, "kind": "submit_param",
                "error": "wan3-prime 这条工作台只出 %s（收到 model=%r）" % (model, params.get("model"))}
    poll_timeout = float(params.get("poll_timeout") or 3900)
    poll_interval = float(params.get("poll_interval") or 10)
    tid = vals["task_id"]
    out = vals["output_path"]

    def _fail(msg, job_id="", kind="stop"):
        return {"success": False, "kind": kind, "job_id": job_id or tid or None, "error": msg}

    def _unknown(msg, job_id=""):
        return _fail("wan3-prime：%s —— 结果未知【先对账，别直接重发】（用 job_id 只捞；或查 %s/v1/balance 对次数）"
                     % (msg, base), job_id, "submit_unknown")

    def _text(raw):
        return (raw or b"").decode("utf-8", "replace")[:300]

    if vals["mode"] == "submit":
        body = dict(vals["body"])
        body["model"] = model
        try:
            st, _, raw = _yz_prime_http("POST", base + "/v1/videos/generations",
                                        {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
                                        json.dumps(body, ensure_ascii=False).encode("utf-8"), timeout=120.0)
        except Exception as e:
            return _unknown("提交时连接断了（%s: %s），任务可能已建" % (type(e).__name__, str(e)[:160]))
        if st in (401, 403):
            return _fail("wan3-prime：Key 被拒（HTTP %s）—— 换 Key 是【平台号池】的事（它对上游每把 Key 只提交一次、"
                         "自动换 Key 不重复扣次数），我方只持一把工作台 Key、不自己换；先去 prime.youzan666.vip "
                         "看这把 Key 的额度/状态" % st)
        # 🔴 非 2xx 也先挖任务号：今天 20:15/20:16 就是 502 带着任务号回来的，过去这条判据没接，
        #    号一丢就捞不回来，只能重投（而平台那边任务其实建上了）。
        try:
            _sub = json.loads((raw or b"").decode("utf-8", "replace")) or {}
        except Exception:
            _sub = {}
        maybe_tid = _dig(_sub, ("task_id", "taskId", "id")) or ""
        if 400 <= st < 500 and not maybe_tid:
            # 明确被拒且没有任务号 ⇒ 平台不建任务、预占会退 ⇒ 改参数重发不算双花
            return _fail("wan3-prime：提交被拒 HTTP %s ⇒ %s" % (st, _text(raw)), kind="submit_param")
        if st < 200 or st >= 300:
            return _unknown("提交回 HTTP %s%s" % (st, ("（回包带任务号 %s）" % maybe_tid) if maybe_tid else ""),
                            maybe_tid)
        tid = maybe_tid
        if not tid:
            return _unknown("提交回 2xx 但挖不到任务号（回包形状变了？回文：%s）" % _text(raw), "")
        vals["task_id"] = tid

    deadline = time.time() + poll_timeout
    misses = 0
    while time.time() < deadline:
        try:
            st, _, raw = _yz_prime_http("GET", base + "/v1/videos/" + str(tid),
                                        {"Authorization": "Bearer " + api_key}, timeout=60.0)
        except Exception as e:
            misses += 1                            # 查询断线 = 抖动：退避重问【同一个】任务号，不重发
            if misses >= 6:
                return _unknown("查询连续 %d 次断线（%s: %s）" % (misses, type(e).__name__, str(e)[:120]), tid)
            time.sleep(min(60.0, poll_interval * misses))
            continue
        if st == 404:
            return _fail("wan3-prime：查不到任务 %s（TASK_NOT_FOUND）—— 先查 %s/v1/balance 看次数动没动过、"
                         "确认一次没扣再决定重发；job_id 留着" % (tid, base), tid)
        if st in (401, 403):
            return _fail("wan3-prime：查询被拒 HTTP %s（Key 失效/额度不可用？换 Key 是平台号池的事，我方不换）"
                         "—— 任务 %s 还挂着，别重发" % (st, tid), tid)
        if st < 200 or st >= 300:                  # 5xx/怪码当查询抖动：退避重问同一个任务号
            misses += 1
            if misses >= 6:
                return _unknown("查询连续 %d 次 HTTP %s" % (misses, st), tid)
            time.sleep(min(60.0, poll_interval * misses))
            continue
        misses = 0
        try:
            obj = json.loads((raw or b"").decode("utf-8", "replace")) or {}
        except Exception:
            obj = {}
        st_txt = str(_dig(obj, ("status", "state", "task_status")) or "").strip().lower()
        url = _dig(obj, ("video_url", "output_url", "result_url", "download_url", "url", "output")) or ""
        bad = st_txt in _PRIME_YZ_BAD

        def _deliver(u, note):
            last = ""
            for attempt in range(3):               # CDN 抖一下就整条判死太脆：取片自重试 3 次再定
                p, n, derr = _yz_prime_download(u, out, base, api_key)
                if p:
                    return {"success": True, "kind": "ok", "path": p, "job_id": tid, "calls": 1, "model": model,
                            "duration": vals.get("duration"), "resolution": vals.get("resolution"),
                            "ratio": vals.get("ratio"), "bytes": n, "note": note, "error": None}
                last = derr or ""
                time.sleep(min(30.0, (poll_interval or 5)) * (attempt + 1))
            return _fail("wan3-prime：直链取不下来（%s）—— 任务已出片没落盘：用 job_id 再捞，别重提交" % last, tid,
                         "deliver_failed")

        if url and not bad:
            return _deliver(url, "wan3-prime 按【次】计费：本次成功生成计 1 次（calls=1），与秒数无关")
        if bad and url:
            # 🔴 判失败之前先看产物（全局规矩）：状态是失败但直链给出来了 —— 先取片验字节再定，别照 failed 重跑
            got = _deliver(url, "状态是 %s 但直链有片（按产物判成功；上游原文：%s）"
                           % (st_txt, str(_dig(obj, ("error", "message", "msg", "fail_reason", "detail")) or "")[:160]))
            if got.get("success"):
                return got
        if bad:
            return _fail("wan3-prime：任务失败 ⇒ %s" % (str(_dig(obj, ("error", "message", "msg", "fail_reason", "detail"))
                                                     or st_txt)[:300]), tid, "task_failed")
        time.sleep(poll_interval)                  # 还在跑（queued/running/…）
    return _fail("wan3-prime：轮询超时 %ss（任务 %s 可能还在出片：用 job_id 再捞，别重提交）"
                 % (int(poll_timeout), tid), tid, "poll_timeout")


def _yz_prime_run(params: dict, route: dict | None = None) -> dict:
    """Prime 独立工作台（wan3.0-video-prime）：**一次批准内部**的重试环。

    为什么要重试环在【这里】而不是让上层再 call 一次：上层每次 call 都要过花钱闸、吃一段批准额度
    ⇒ 上游 23% 的成功率会变成"用户要点四次批准"。这条 lane 的失败按平台账【计费 0】⇒
    同一次生成内部重投不花次数、也不该再要点击；真花钱的那一次（已计费/已出片）永远只捞不重投。
    ⚠️ 能力差异（不假装支持）：请求体只有 prompt/duration/resolution/ratio/media，没有 RH 那个 audio 开关。
    """
    import time

    base = str(params.get("base_url") or (route or {}).get("base_url") or _get("PRIME_YZ_BASE_URL") or "").rstrip("/")
    api_key = str(params.get("api_key") or (route or {}).get("api_key") or _get("PRIME_YZ_API_KEY") or "").strip()
    model = str((route or {}).get("model") or _get("PRIME_YZ_MODEL") or "wan3.0-video-prime")
    if not base or not api_key:
        return {"success": False,
                "error": "wan3-prime 没配 base_url/api_key（.env 的 PRIME_YZ_BASE_URL / PRIME_YZ_API_KEY）"}

    max_attempts = max(1, int(params.get("prime_attempts") or 4))
    waits = params.get("prime_backoff") or [45, 120, 240]
    # 只捞不重投的那种调用（调用方自带 job_id ⇒ 适配器进 poll 模式）不参与重试环：它本来就是"捞"。
    # ⚠️ 判据字段是 job_id 不是 task_id：`_yz_prime_payload` 认 job_id 才切 poll 模式，
    #    写成 task_id 会【静默重新提交】= 自己造双花（闸 R15/R22 钉这条）。
    if params.get("job_id"):
        return _yz_prime_once(params, route, base, api_key, model)

    pre = _yz_prime_reconcile(base, api_key)
    if isinstance(pre, dict) and pre.get("remaining") is not None and int(pre["remaining"]) <= 0:
        return {"success": False,
                "error": ("wan3-prime：平台次数已用尽（remainingCalls=0，成功过 %s 次、失败 %s 次）⇒ "
                          "先去 prime.youzan666.vip 续次数，再来提交。0 分未扣、任务未发起。"
                          % (pre.get("consumed"), pre.get("failed")))}
    seen = set((pre or {}).get("recent") or {}) if isinstance(pre, dict) else set()

    tries = []
    p2 = dict(params)
    attempt = 0
    while True:
        attempt += 1
        r = _yz_prime_once(p2, route, base, api_key, model)
        kind = str(r.get("kind") or "stop")
        if r.get("success"):
            r["prime_attempts"] = attempt
            r["prime_tries"] = tries
            return r
        rec = _yz_prime_reconcile(base, api_key)
        act, why = prime_retry_verdict(kind, str(r.get("job_id") or ""), seen, rec, max_attempts - attempt)
        tries.append({"attempt": attempt, "kind": kind, "task_id": r.get("job_id") or "",
                      "verdict": act, "why": why, "ts": round(time.time(), 3)})
        if act == "stop":
            r["prime_attempts"] = attempt
            r["prime_tries"] = tries
            r["prime_reconcile"] = ({k: rec.get(k) for k in ("remaining", "consumed", "reserved",
                                                            "failed", "succeeded")} if isinstance(rec, dict) else None)
            r["error"] = str(r.get("error") or "") + "｜重试判据=%s：%s" % (act, why)
            return r
        if act.startswith("poll:"):
            p2 = dict(params)
            p2["job_id"] = act.split(":", 1)[1]        # 必须是 job_id：payload 靠它切【只捞】模式
            seen.add(p2["job_id"])
            continue
        seen |= set((rec or {}).get("recent") or {}) if isinstance(rec, dict) else set()
        w = waits[min(attempt - 1, len(waits) - 1)]
        time.sleep(float(w))


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
        # 动作迁移（浏览器 RPA 腿）：本层不直连 RunningHub，直调 services/motion_rpa 并盯到终态。
        # 🔴 批准条子在【网关这一端】invoke() 消费（2026-10-08 剥离后花钱动作和条子同进程，只批一次）。
        return _motion_rpa_run(params)

    if capability == "video.generate" and r["purpose"] == "h3-rpa":
        # H3 第二条腿（本机 8093 浏览器 RPA）：转调工作台、盯到终态、按盘上产物判成败。
        # 🔴 批准条子在【网关这一端】消费（不写进 SPEND_APPROVAL_AT_SOURCE）：这条路的必经入口就是
        #    gateway call，所以闸放在这里就是真闸；8093 自己的网页按钮是给人在浏览器上点的，不在这条轨上。
        return _h3_rpa_run(params)

    if capability == "video.generate" and r["purpose"] == "wan3-prime":
        # Prime 独立工作台（prime.youzan666.vip · wan3.0-video-prime）：按【次】计费，盯到终态、落盘。
        # 🔴 批准条子在【网关这一端】消费（不写进 SPEND_APPROVAL_AT_SOURCE）：这条路的必经入口就是
        #    gateway call，闸放在这里就是真闸；工作台自己的网页按钮不在这条轨上。
        return _yz_prime_run(params, r)

    if capability == "video.analyze":
        _am_model = r["model"] or _MODEL_FALLBACK.get(r["provider"])
        # 🔴 思考型视觉脑会把预算整段烧在 reasoning 上：实测 mimo-v2.6-flash 看一段 5s 小片，
        #    max_tokens=300 → completion 300 里 299 是 reasoning、正文 0 字 ⇒ 上游回 200 空内容，
        #    看起来像"这个模型不能识别视频"，其实是被预算卡死（2500 时 708 reasoning + 正确正文）。
        #    只对实测过的 mimo 兜底；别的模型别凭猜加（要加先跑一条真的）。
        _am_budget = params.get("max_tokens") or (2500 if str(_am_model or "").startswith("mimo") else 300)
        return analyze_media(
            path=params["path"],
            question=params.get("question") or "用一句话描述这个画面的关键信息。",
            model=_am_model, base_url=r["base_url"], api_key=r["api_key"],
            max_tokens=_am_budget,
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
        # 🔴 ts 是 2026-10-09 补的：没有它，"进程被杀留下的 pending 预扣"永远是盲的 ——
        #    既判不出哪笔早该回收，也没法在退之前告诉人"这笔躺了 3 天"。旧行留 NULL（显示"无时间戳"）。
        for col, typ in (("route", "TEXT"), ("est", "INTEGER"), ("tokens", "INTEGER"), ("ts", "REAL")):
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
        con.execute("INSERT INTO holds(hold_id,member,amount,status,route,est,ts) "
                    "VALUES(?,?,?,'pending',?,?,?)",
                    (hold_id, member, cost, f"{capability}/{purpose}", cost, _time.time()))
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

    def pending_holds(self, limit: int = 50) -> list:
        """未结算的预扣。⚠️ holds 表【没有 ts】⇒ 进程被杀留下的 pending 是永久的，只能按 id 指名退。

        为什么要有这个只读口：AGENTS.md 早就写着"遗留预扣用 holds-pending / holds-rollback 指名回收"，
        但这两个动词在代码里【根本不存在】（2026-10-09 自查抓到）——文档跑到代码前面 = agent 照着敲就懵。
        """
        con = self._con()
        try:
            rows = con.execute("SELECT hold_id,member,amount,route,est,tokens,ts FROM holds "
                               "WHERE status='pending' ORDER BY rowid DESC LIMIT ?",
                               (int(limit),)).fetchall()
        except Exception:                                    # noqa: BLE001
            rows = con.execute("SELECT hold_id,member,amount,status FROM holds "
                               "WHERE status='pending' LIMIT ?", (int(limit),)).fetchall()
            con.close()
            return [{"hold_id": r[0], "member": r[1], "amount": r[2], "route": "", "est": None,
                     "tokens": None} for r in rows]
        con.close()
        now = _time.time()
        out = []
        for r in rows:
            ts = r[6]
            out.append({"hold_id": r[0], "member": r[1], "amount": r[2], "route": r[3] or "",
                        "est": r[4], "tokens": r[5],
                        "age_hours": (round((now - float(ts)) / 3600, 1) if ts else None),
                        "ts": ts})
        return out

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
# 🔴 2026-10-09 用户最终口径："除了生图片和推理，其他都要审批" ⇒ 判据【反过来写】：
#   默认要批，只有下面这份免批白名单放行。
#   为什么必须反过来：正向清单（"只有这几类要批"）会在新增能力时【静默漏掉】——
#   video.edit 就是因为不在清单里被"免批"跑掉真分（去水印 20 分/次、超分 60 分/次，比生视频还贵），
#   用户原话："去水印不属于生视频吗，为什么不通过我审核"。
#   反向之后：任何新能力（含别人以后加的）默认落到"要批"，想免批必须在这里显式登记一行。
#   ⚠️ 免批 ≠ 免账：这三类照旧走 precheck→preauth→settle→失败回滚，扣分与留痕一样，只是不用人点头。
SPEND_APPROVAL_EXEMPT = (
    "image.generate",    # 生图片
    "text.complete",     # 推理（文本脑）
    "video.analyze",     # 看图看片 = 推理，产出是文字不是片子
)
# 派生视图（给"哪些要批"的人/闸看；判据本身在 needs_spend_approval）
SPEND_APPROVAL_REQUIRED = tuple(c for c in CAPABILITIES if c not in SPEND_APPROVAL_EXEMPT)
SPEND_APPROVALS_PATH = _os.environ.get("GATEWAY_SPEND_APPROVALS") or _os.path.join(
    _d or _os.getcwd(), ".gateway_spend_approvals.json")
_APPROVALS_PATH_DEFAULT = _os.path.join(_d or _os.getcwd(), ".gateway_spend_approvals.json")
SPEND_APPROVAL_TTL = float(_os.environ.get("GATEWAY_SPEND_APPROVAL_TTL") or 24 * 3600)

# ⚠️ 回归闸专用的"免人证"开关（2026-10-08）。它【不是环境变量、也不是 CLI 参数】，只能由进程内的
#   Python 代码赋值 —— 目的就是让闸能直接构造条子去测"消费侧"，而真实路径永远拿不到它。
#   双保险：即便有代码把它置 True，也只有当条子写到【非默认】文件（闸自己的 scratch 账本）时才生效，
#   所以它物理上签不出生产 `.gateway_spend_approvals.json` 里的条子。
ALLOW_GRANT_WITHOUT_ATTEST = False


def _session_of(params) -> str:
    """这条调用属于哪条会话 —— 【唯一一处读法】：params 里给了就用，否则回落 DSH 注入的环境变量。

    🔴 为什么单独抽出来（2026-10-10 现网）：原来这个表达式在三处各写一遍，其中 :2871 批准闸那处
       漏了 env 回落 ⇒ 凡是只靠 env 拿会话的腿（wan3-prime 就是这样把会话递给 :167 的守卫、却没塞进
       params["session"]），到批准闸就变成空串 ⇒ 按会话绑的条子永远匹配不上 ⇒ 拒。
       表现还特别坏：拒因被上层吞成 `generation_failed`，账本 0 记录，谁都不知道是"会话没传上来"。
       两边读法不一致 = 一条安全判据悄悄失效，而所有单测都还是绿的。
    """
    p = params if isinstance(params, dict) else {}
    return str(p.get("session") or _os.environ.get("DSH_SESSION_ID") or "").strip()


def needs_spend_approval(capability: str) -> bool:
    """这条能力要不要"人给的条子"。判据唯一源 = SPEND_APPROVAL_EXEMPT（🔴 默认要批）。"""
    return str(capability or "") not in SPEND_APPROVAL_EXEMPT


# 🔴 例外形状：有些腿的**花钱动作可能发生在另一个进程里**。这类腿把条子的【消费点】放在真正干活的
# 那一端（经 8100 的 /api/spend/guard），网关这边不重复扣一次 —— 同一次生成扣两段，等于逼用户批两遍，
# 那是自造的假账。⚠️ 这张表是后门风险：加一条 purpose 进来，必须同时在对应那一端加 guard 调用，
# 配对关系由 scripts/check-motion-rpa-lane.py 钉着，防止它变成一条绕过批准的后门。
# 2026-10-08：动作迁移从 8091 剥离进本进程（backend/services/motion_rpa）后，花钱动作和条子同进程 ⇒
# 撤掉 ("video.generate","motion-rpa")，条子回到 invoke() 里消费一次。现在表是【空的】。
SPEND_APPROVAL_AT_SOURCE = ()


def approval_consumed_at_source(capability: str, purpose: str) -> bool:
    return (capability, purpose) in SPEND_APPROVAL_AT_SOURCE


def guard_spend_from_source(capability: str, purpose: str, account: str = "local",
                            count: int = 1, session: str = "") -> dict:
    """给"真正花钱的那一端"（另一个进程里的腿）用的批准闸入口：发起前先来问一次。

    ⚠️ 预计扣分由**网关按价目自己算**，调用方不许自带价格 —— 否则传个 credits=0 就等于放行。
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


# ── 素材分析台账（2026-10-10 用户裁定：提交前必须真的看过每个素材）──────────────
# 🔴 看图免批但【不免账】：单价现读 prices（video.analyze = 6 分/次）。
# 只记 sha8：文件名可以撞、路径可以改，内容哈希才是身份（多会话并发咬过两次）。
MEDIA_ANALYSIS_LOG_DEFAULT = ".gateway_media_analysis.json"


def media_analysis_log_path() -> str:
    """自测后门：WEEKSIR_MEDIA_ANALYSIS_LOG 指哪写哪（只对临时文件有效，生产路径不受影响）。"""
    import os as _o
    got = (_o.environ.get("WEEKSIR_MEDIA_ANALYSIS_LOG") or "").strip()
    if got:
        return got
    return _o.path.join(_o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))),
                             MEDIA_ANALYSIS_LOG_DEFAULT)


def media_analysis_rows() -> list:
    import json as _j
    try:
        with open(media_analysis_log_path(), encoding="utf-8", errors="replace") as f:
            data = _j.load(f)
        return list(data.get("rows") or []) if isinstance(data, dict) else []
    except Exception:
        return []


def file_identity(path: str) -> dict:
    """素材身份 = 内容 sha8 + 字节数 + basename。读不到 ⇒ ok=False（不给免检牌）。"""
    import os as _o
    import hashlib as _h
    p = str(path or "")
    try:
        b = open(p, "rb").read()
    except Exception:
        return {"ok": False, "sha8": "", "bytes": 0, "name": _o.path.basename(p)}
    return {"ok": True, "sha8": _h.sha256(b).hexdigest()[:8], "bytes": len(b),
            "name": _o.path.basename(p)}


def record_media_analysis(path, capability, purpose, session="", credits=None, hold_id="") -> dict:
    """网关跑完一条看图腿之后调它。写失败不许影响业务（返回 ok=False + 原因）。
    🔴 `hold_id` = 这次调用的**凭证**（invoke 的预扣号）。没有它的行【不算证据】——
       2026-10-12 现网：agent 发现台账没记上，就 import 这个函数自己写了 3 条"看过图"（13 毫秒三条、credits=0），
       于是合同那句"不认 agent 自述、只认台账"被自证破功。凭证不要求不可伪造（同一个 Administrator 做不到），
       要求的是【伪造得贵 + 看得见】：要伪造就得连预扣记录一起编，而 `gateway.py analyses` 会当场显示没有尾号。"""
    import os as _o
    import json as _j
    import time as _time
    idn = file_identity(path)
    row = {"sha8": idn.get("sha8") or "", "name": idn.get("name") or "",
           "bytes": idn.get("bytes"), "capability": capability, "purpose": purpose,
           "session": str(session or ""), "ts": round(_time.time(), 3),
           "hold_id": str(hold_id or ""),
           "credits": (credits or {}).get("charged")}
    if not idn.get("ok"):
        row["error"] = "文件读不到 ⇒ 不记台账"
        return {"ok": False, "row": row}
    try:
        data = {"rows": media_analysis_rows()}
        data["rows"].append(row)
        data["rows"] = data["rows"][-4000:]      # 台账不许无界长
        tmp = media_analysis_log_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _j.dump(data, f, ensure_ascii=False, indent=1)
        _o.replace(tmp, media_analysis_log_path())
        return {"ok": True, "row": row}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:120]), "row": row}


def analysis_missing_paths(paths, rows, session="") -> list:
    """🔴 "还缺哪几个素材没看过" —— 这条判据【只在这一份】。
    以前只有下面那个人话版，于是"缺哪些"这件事只能靠解析错误字符串，管线要自动补跑就得再抄一份判据
    （两份必然漂）。现在：判据回路径，文案由它套出来。"""
    have = {}
    for r in rows or []:
        if str(r.get("capability") or "") != "video.analyze":
            continue
        if not str(r.get("hold_id") or "").strip():
            continue                      # 🔴 没有调用凭证的行不算证据（agent 自己 import 记账函数写的那种）
        if session and str(r.get("session") or "") != str(session):
            continue                      # 别的会话看过 ≠ 这条线看过（报价与归属都按会话算）
        have[str(r.get("sha8") or "")] = r
    out, seen = [], set()
    for one in paths or []:
        idn = file_identity(one)
        key = idn.get("sha8") or ""
        if not idn.get("ok"):
            out.append(one)               # 读不到也要报：不给免检牌
        elif key in seen:
            continue                      # 同一张图给两次不算两个洞
        elif key not in have:
            out.append(one)
        seen.add(key)
    return out


def analysis_coverage_problems(paths, rows, session="") -> list:
    """纯判据（人话版）：paths 里每个素材都必须有一条同会话的 video.analyze 记录。空列表 = 覆盖。
    🔴 判据本体在 analysis_missing_paths，这里只贴标签 —— 补跑这件事已经不归 agent 做（2026-10-12），
       所以文案不再教它敲 CLI，改成"由管线补跑"，免得它又去手搓脚本。"""
    p = []
    for one in analysis_missing_paths(paths, rows, session):
        idn = file_identity(one)
        label = "%s(%s)" % (idn.get("name") or one, idn.get("sha8") or "无sha8")
        p.append("素材读不到 ⇒ 没法证明看过：%s" % label if not idn.get("ok")
                 else "没有看图记录：%s ⇒ 由管线补跑 video.analyze（6 分/次，免批不免账）" % label)
    return p

# ── 段数的唯一机器源（2026-10-10 用户裁定：审批一次就行）───────────────────────
# 为什么必须有：批准条子写死 count，而 count 此前是 agent 手填的 ⇒ 填 1 就要点 1 次；
#    填少了跑到第 2 段会被自己的闸拦下（钱没多花，但活停了），填多了等于替人多批。
#    判据落在盘上事实：人审过的那张切段表说了算，读不出来就如实说『按单段』。
def plan_segment_count(task_dir: str) -> dict:
    """返回 {count, source, detail}。count=0 表示【读不出来】⇒ 调用方必须拒，不许猜。"""
    import os as _os
    import io as _io
    d = str(task_dir or "").strip()
    if not d or not _os.path.isdir(d):
        return {"count": 0, "source": "bad-dir", "detail": "目录不存在：%s" % d}
    tl = _os.path.join(d, "TIMELINE.md")
    if _os.path.isfile(tl):
        lines = _io.open(tl, encoding="utf-8", errors="replace").read().split(chr(10))
        n, seen = 0, False
        for ln in lines:
            s = ln.strip()
            # 只认表头第一格是「段」的那张表 —— 别的表（产品图清单、风险点）不算切段
            if not seen and s.startswith("|"):
                head = s.strip("|").split("|")[0].strip()
                if head in ("段", "段号", "Seg", "seg"):
                    seen = True
                continue
            if seen:
                if not s.startswith("|"):
                    break
                bare = s.replace("|", "").replace("-", "").replace(":", "").strip()
                if bare == "":
                    continue                      # 分隔行
                n += 1
        if n:
            return {"count": n, "source": "TIMELINE.md", "detail": "人审过的那张切段表里 %d 行" % n}
    od = _os.path.join(d, "outputs")
    if _os.path.isdir(od):
        segs = sorted(x for x in _os.listdir(od)
                      if _os.path.isdir(_os.path.join(od, x)) and x.lower().startswith("seg"))
        if segs:
            return {"count": len(segs), "source": "outputs/seg*", "detail": "已切出 %d 个段目录" % len(segs)}
    return {"count": 1, "source": "single-default",
            "detail": "没有 TIMELINE.md 也没有 seg* 段目录 ⇒ 按单段建条子；"
                      "真跑出来是多段时后面的段会被拒（不双花），要回来重新批"}


def count_from_args(cap: str, purpose: str, task_dir: str):
    """把 --count-from 换成 (count, credits, 说明)。读不出来 ⇒ 抛 ValueError。"""
    r = plan_segment_count(task_dir)
    n = int(r.get("count") or 0)
    if n <= 0:
        raise ValueError("段数读不出来：%s ⇒ 不许建条子（猜一个数等于替客户多批）" % r.get("detail"))
    unit = int(unit_price(cap, purpose))
    return n, n * unit, ("段数=%d 来源=%s；单价=%d 分 ⇒ 上限 %d 分（%s）"
                         % (n, r["source"], unit, n * unit, r["detail"]))

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


# ── 报价出处判据（2026-10-11 现网 session-241ea3f7 08:23 复盘）───────────────────────
# 那条消息写"单价 60 / 段数 1 / 预计总扣分 60 / 实扣 40 / 余额 99544"，末尾还给了 /approval 那条链接。
# 盘上：approve 从没跑过（最新一张条子是 10-10 20:14 且不属于这条会话）⇒ 前三格加余额全部无出处；
#      素材分析台账是 40+40+38 三次 ⇒ "实扣 40" 少报两次；accounts 里 default 是 99532 ⇒ 余额差 12；
#      批准页里属于这条会话的行【一条都没有】⇒ 那句"批准入口"指的是空气。
# 🔴 判据只这一份：回归闸（scripts/check-quote-provenance.py）与批准页都读这里，不许抄第二份。
import re as _re

QUOTE_FIELDS = ("单价", "段数", "预计总扣分", "余额", "实扣")
_QUOTE_LABELS = {"单价": ("单价",), "段数": ("段数",),
                 "预计总扣分": ("预计总扣分", "预计扣分", "总扣分"),
                 "余额": ("当前余额", "余额"), "实扣": ("实扣", "实际扣分", "已扣分")}
# 只认这些字样当"批准入口"证据 —— 拿"批准/点头"这种散文词判定会误伤正常汇报。
_APPROVAL_ENTRY_MARKS = ("/approval", "批准入口", "批准页", "批准窗", "点【批准", "approval 页")


def quote_numbers_in(text: str) -> list:
    """从一条报价里抓「五格 → 数字」。允许 `60`、`60 分`、markdown 表格 `| 单价 | 60 |`、全角冒号。
    🔴 每个字段一条合并正则、别名按【长在前】排：`预计总扣分|预计扣分` 若分两次扫，
       "预计总扣分 60" 会被算成两条（短别名是长短语的子串）⇒ 判据会凭空翻倍报红。"""
    out = []
    for field, labels in _QUOTE_LABELS.items():
        pat = _re.compile("(?:%s)[^0-9]{0,6}(\\d+)" % "|".join(_re.escape(x) for x in labels))
        for m in pat.finditer(text or ""):
            out.append((field, int(m.group(1))))
    return out


def quote_facts(session: str = "", account: str = "local") -> dict:
    """现读【报价出处】。全只读：不建条子、不碰额度、0 消耗。"""
    data = _approvals_load()
    now = _time.time()
    pend = [p for p in (data.get("pending") or [])
            if (p.get("session") or "") == (session or "") and p.get("status") == "pending"
            and float(p.get("expires") or 0) > now]
    grants = [g for g in (data.get("grants") or [])
              if (g.get("session") or "") == (session or "") and float(g.get("expires") or 0) > now]
    charged = [int(r.get("credits") or 0) for r in media_analysis_rows()
               if (r.get("session") or "") == (session or "") and r.get("credits") is not None]
    try:
        bal = ledger().balance(account)
    except Exception:                                       # noqa: BLE001 —— 读不到就说读不到，不猜一个数
        bal = None
    return {"session": session, "account": account,
            "pending_id": (pend[0].get("id") if pend else ""),
            "quote_text": (pend[0].get("quote") if pend else ""),
            "count": (pend[0].get("count") if pend else None),
            "credits": (pend[0].get("credits") if pend else None),
            "grant_ids": [g.get("id") for g in grants],
            "balance": bal, "charged": charged, "charged_total": sum(charged)}


def quote_problems(message: str, facts: dict, unit=None) -> list:
    """纯函数（自己不读盘 ⇒ 能被整块抠出来跑真值表）。返回每条说不通的地方，空 = 五格都有出处。
    `unit` 由调用方现读 `unit_price(路由)` 传进来。
    🔴 五格的出处【不全是 pending】，别一锅端：单价的出处是价目表、余额的出处是账本，这两格不建条子也能核；
       段数与预计总扣分只有 approve 才知道（段数从盘上算、分数从段数算），所以它们【必须有 pending】才算有出处。
       把单价也绑到 pending 上 = 判据在替"没跑 approve"这件事重复喊，真正的错（余额写错、实扣少报）反而被淹掉。"""
    got = quote_numbers_in(message)
    if not got:
        return []
    probs, has_pending = [], bool(facts.get("pending_id") or facts.get("grant_ids"))
    for field, num in got:
        if field == "实扣":
            # "实扣" = 这条会话到现在真扣掉的分 ⇒ 只能等于台账合计；报单次数字 = 少报重复消耗
            if not facts.get("charged"):
                probs.append("实扣=%s 但台账里【没有这条会话的任何记录】⇒ 无出处" % num)
            elif num != int(facts.get("charged_total") or 0):
                probs.append("实扣=%s 与台账不符：这条会话共 %d 次 %s，合计 %s 分"
                             % (num, len(facts["charged"]), facts["charged"], facts["charged_total"]))
            continue
        if field == "单价":
            if unit is None:
                probs.append("单价=%s 无法核对：报价里那条路由在价目表里读不到单价"
                             "（没跑 prices 就不要写这一格）" % num)
            elif int(num) != int(unit):
                probs.append("单价=%s 与价目表不符（现读=%s）⇒ 凭记忆写价" % (num, unit))
            continue
        if field == "余额":
            if facts.get("balance") is None:
                probs.append("余额=%s 无法核对：账本读不到账户 %s 的余额" % (num, facts.get("account")))
            elif int(num) != int(facts["balance"]):
                probs.append("余额=%s 与账本不符（现读=%s，账户 %s）"
                             % (num, facts["balance"], facts.get("account")))
            continue
        if not has_pending:
            probs.append("%s=%s 无出处：这条会话【没有待批条子】⇒ approve 没跑过，"
                         "段数与总扣分只能从它的输出里抄，不许手写" % (field, num))
            continue
        want = {"段数": facts.get("count"), "预计总扣分": facts.get("credits")}[field]
        if want is None:
            probs.append("%s=%s 无法核对：盘上读不到对应值（不许拿消息里的数当自己的出处）" % (field, num))
        elif int(num) != int(want):
            probs.append("%s=%s 与盘上不符（应为 %s）" % (field, num, want))
    if facts.get("pending_id"):
        miss = [ln.strip() for ln in str(facts.get("quote_text") or "").splitlines()
                if ln.strip().split("：")[0] in ("路由", "单价", "段数", "预计扣分", "当前余额")
                and ln.strip() and ln.strip() not in (message or "")]
        if miss:
            probs.append("没把 approve 打出的原始报价行原样贴进消息：%s" % " / ".join(m[:28] for m in miss))
    return probs


def approval_entry_problems(message: str, facts: dict) -> list:
    """出现『批准入口/批准页/approval』字样 ⇒ 该会话必须有 pending 或未过期的条子。
    拦的是这个形状：印一条 http://127.0.0.1:8100/approval，而那张页面上没有属于这条会话的可批项。"""
    text = message or ""
    hit = sorted({w for w in _APPROVAL_ENTRY_MARKS if w in text})
    if not hit or facts.get("pending_id") or facts.get("grant_ids"):
        return []
    return ["消息里出现了批准入口字样（%s），但 approvals 里没有这条会话的待批/已批条子"
            " ⇒ 那个链接点进去是空的，客户看不到任何可批的东西。"
            "先跑 gateway.py approve 建条子（它自己会弹置顶批准窗），再谈批准入口。" % "、".join(hit)]


def _approvals_save(data: dict) -> None:
    # ⚠️ 写这份 json 的规矩：先 `_approvals_load()` 拿到完整 dict、原地改要改的那块、再存。
    #    不许 `_approvals_save({"grants": ...})` 这种"只带一个键"的写法 —— 那是整份重写，
    #    会把 pending（待批）和 denials（拒绝留痕）一起抹掉（2026-10-08 真踩过，见 check_spend_approval）。
    tmp = SPEND_APPROVALS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        _json.dump(data, f, ensure_ascii=False, indent=2)
    _os.replace(tmp, SPEND_APPROVALS_PATH)


def grant_lane_matches(g: dict, capability: str, purpose: str, account: str,
                       now: float, session: str = "") -> bool:
    """这张条子和这次请求【是不是同一条道】：没过期 ∧ 能力对上 ∧ purpose 对上（"*"=该能力全放行）
    ∧ 账户对上 ∧ 会话绑得上。段数/分数够不够不在这里判（那是 `approval_allows` 的第二半）。

    🔴 为什么单独抽出来：只读探针（`peek_spend_approval`）要报"有几张同道的活条子、各差在哪"，
    而它绝不能碰额度判定以外的第二份规则 —— 判据分两处，迟早漂成"消费口认、只读口不认"，
    那时候 agent 看到的和网关判的就不是同一件事（2026-10-10 那次"点了没反应"就是这么来的）。
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
    return not (owner and owner != str(session or ""))


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
    if not grant_lane_matches(g, capability, purpose, account, now, session):
        return False
    return (int(g.get("count", 0)) - int(g.get("used", 0)) >= max(1, int(want_count))
            and int(g.get("credits", 0)) - int(g.get("used_credits", 0)) >= int(want_credits))


def peek_spend_approval(capability: str, purpose: str, account: str,
                        count: int = 1, credits: int = 0, session: str = "") -> dict:
    """🔴 只读版"这一批有没有人批过"——**绝不写盘、绝不清段**。

    为什么必须有这一条：判"批没批到"的老路是调 `check_spend_approval()`，而那个函数**判 ALLOWED 的那一下
    就是消费点**（当场 used += count）。2026-10-10 现网就是这么烧的：另一会话为了搞清"用户点没点"
    连调它两次，把一张 1 段条子的额度吃光（钱没扣、批准没了），随后它报"我马上正式提交"而盘上零预扣
    ⇒ 只能重开待批、让用户第二次点批准。门槛本该是一次点击，多出来的那一次是我们的 bug。

    判据与消费侧【共用同一个】 `approval_allows`，所以这里不会漂出第二份匹配规则：
    这里只是把"命中/差在哪"报出来，不改 `used`。
    """
    now = _time.time()
    data = _approvals_load()
    live = [g for g in data.get("grants", []) if g.get("expires", 0) > now]
    pend = spend_pending(with_code=False) if needs_spend_approval(capability) else []
    hit = [g for g in live if approval_allows(g, capability, purpose, account, now, count, credits, session)]
    # 差在哪：同能力同 purpose 的活条子存在但不够 ⇒ 报段/分/会话三项，免得 agent 只能靠猜去试消费口
    near = [{"id": g["id"], "left": int(g.get("count", 0)) - int(g.get("used", 0)),
             "credits_left": int(g.get("credits", 0)) - int(g.get("used_credits", 0)),
             "owner_session": str(g.get("session") or ""),
             "expires_in": round(g.get("expires", 0) - now)}
            for g in live
            if grant_lane_matches(g, capability, purpose, account, now, session)]
    return {"read_only": True, "consumed": False,
            "capability": capability, "purpose": purpose, "account": account,
            "session": session or "", "need": count, "want_credits": credits,
            "requires_approval": needs_spend_approval(capability),
            "allowed": bool(hit) or not needs_spend_approval(capability),
            "grant": (hit[0]["id"] if hit else ""),
            "left_after": (int(hit[0]["count"]) - int(hit[0]["used"]) - count) if hit else 0,
            "matching_grants": [g["id"] for g in hit],
            "candidates": near,
            "pending_tickets": [{"id": p.get("id"), "capability": p.get("capability"),
                                 "purpose": p.get("purpose"),
                                 "seconds_left": round(float(p.get("expires", 0)) - now),
                                 "account": p.get("account"),
                                 "owner_session": str(p.get("session") or "")}
                                for p in pend],
            "next": ("直接提交：额度已生效，不需要再点批准" if (hit or not needs_spend_approval(capability))
                     else ("有人在等：待批已建、还没点 ⇒ 别重复建待批，也别调消费函数试探" if pend
                           else "需要批准：建【待批】并把报价发给用户，由他点一次"))}


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
            "error": ("approval_required：这类能力默认要人批准（%s/%s，预计 %d 分 / %d 段）。"
                      "没有覆盖本次请求的批准条子 ⇒ 已拒绝，0 分未扣、任务未发起。"
                      "要跑：先把这份报价原样发给用户、拿到明确同意，再由【用户本人】在弹出的批准窗上"
                      "点一次批准 —— agent 只能建【待批】，点之前 0 额度，它签不出。"
                      "（不在此处印建条子的命令：跑这条调用的就是 agent 自己，印出来等于把说明书递给要绕过的人。）"
                      "🔴 免批只有 %s 三类，其余一律要批。"
                      % (capability, purpose, credits, count, "/".join(SPEND_APPROVAL_EXEMPT)))}


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
                 "error": ("no-attestation：这条调用没拿到人证 ⇒ 未签条子、0 额度。"
                           "正路：agent 跑 approve 建【待批】⇒ 批准窗自动弹到用户桌面 ⇒ 由他点一次"
                           "【批准，开始跑】。🔴 不许把 code/consumed 命令交给用户去敲（那是已废的旧流程）。")}
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


# ── 花钱那一刀认合同回执（2026-10-11 现网 session-241ea3f7 08:23 复盘）────────────────
# 那条会话绕过 runner 自己 `from backend import gateway` 调引擎，盘上没有 .skill_receipt.json，
# 而网关【只认批准条子】⇒ 合同对"花钱"这件事从来没有约束力：绕开入口 = 照样能扣费。
# 已核实 `verify_or_raise` 全仓只有 `super_clone_wan3.py` 一处调 ⇒ 判据绑在最重的入口上，
# 便宜的路一直开着。这条把它下移到唯一咽喉（所有 wan3 腿都经 gateway.invoke）。
CONTRACT_RECEIPT_WALK = 6          # 从成片落点往上找几层（回执写在段目录，也可能写在任务根）


def receipt_dirs(params: dict) -> list:
    """【片子要写进哪个目录，那个目录（或它的父目录）里就必须有一份合格回执】。
    🔴 不拿会话号找回执 —— 8090 那种常驻进程里没有 DSH_SESSION_ID，按会话找会直接漏掉它，
       而那恰恰是"绕开入口"最常待的地方。落点是调用方自己给的，赖不掉。"""
    out = str((params or {}).get("output_path") or "").strip()
    if not out:
        return []
    d = _os.path.dirname(_os.path.abspath(out))
    dirs = []
    for _ in range(CONTRACT_RECEIPT_WALK):
        if not d or d in dirs:
            break
        dirs.append(d)
        parent = _os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return dirs


def spend_contract_problems(capability: str, purpose: str, params: dict | None = None) -> list:
    """返回【问题清单】，空表 = 放行。
    · 只断提交，不断查询/捞回（带 job_id 的那条是去取回已提交的任务，重投=双花）。
    · 这条腿若不属于任何【声明了 spend 的合同档】⇒ 不在射程，明说不假装管得住。
    · 合同模块/反查本身出错 ⇒ 按不合格处理（判据读不到 ≠ 放行）。"""
    params = params or {}
    if str(params.get("job_id") or "").strip():
        return []
    try:
        from backend import skill_contract as _sc
    except Exception as e:                                   # noqa: BLE001
        return ["合同模块读不到（%s: %s）⇒ 无法核验回执，按不合格处理"
                % (type(e).__name__, str(e)[:120])]
    try:
        lane = _sc.skill_for_spend(capability, purpose)
    except Exception as e:                                   # noqa: BLE001
        return ["反查『这条花钱腿属于哪档合同』时出错（%s）⇒ 按不合格处理" % type(e).__name__]
    if not lane:
        return []
    dirs = receipt_dirs(params)
    if not dirs:
        return ["这条腿属于合同档 %s（spend=%s/%s），但参数里没有 output_path ⇒ 不知道去哪儿核验回执。"
                "成片落点都不给，等于产物无处可查，拒绝提交（0 消耗、不动批准额度）"
                % (lane, capability, purpose)]
    for d in dirs:
        rec = _sc.load_receipt(d)
        if not rec:
            continue
        problems = _sc.check_receipt(lane, rec, inputs=())   # 指纹由管线自己核（它才知道输入清单）
        if not problems:
            return []
        return ["目录 %s 里的回执不合格：%s" % (d, "；".join(str(p) for p in problems)[:300])]
    return ["从 %s 往上 %d 层里都没有 %s ⇒ 这条成片不是从 %s 声明的入口跑出来的。"
            "要出片请跑该档 runner（它会在提交前写回执），而不是自己拼一次调用"
            % (dirs[0], CONTRACT_RECEIPT_WALK, _sc.RECEIPT_NAME, lane)]


# 🔴 判定口地址可用 env 覆盖 —— 这是给回归闸留的【测试缝】：闸拿自己的桩服务判各分支，
#    不去碰线上 8100 的会话状态（否则测一次就往生产文件里写一条真回填）。
FORM_GATE_URL = _os.environ.get("GATEWAY_FORM_GATE") or "http://127.0.0.1:8100/api/skills/form?session="


def form_prefill_blockers(session: str) -> list:
    """【配了表单的那条线】报价之前必须回填过表 —— 判据不在这儿，只读 8100 的结论。
    为什么钉在 approve 上：这是花钱前唯一那一步【必然发生】的命令，绑在这里才不用指望 agent 自觉；
    绑在 runner 上（像回填覆盖率那条）拦不住"不进 runner、直接在对话里把默认值报给客户"的形状
    —— 2026-10-11 16:30 现网就是这样：登记了、没回填、`prefill_gap=["video"]`，条子照样建得出来。
    三条放行/拒绝线：① 这条会话登记的档没配表单 ⇒ 不适用；② 回填过（或客户自己交齐了）且缺口为空 ⇒ 放行；
    ③ 其余（没登记 / 没回填 / 缺口非空 / 8100 没回话）一律拒 —— 读不到不等于通过，fail-closed。"""
    sid = str(session or "").strip()
    if not sid or sid == "-":
        return ["approve 没带 --session ⇒ 无法判断这条会话回填过没有。带会话号再来（不猜一个 id）。"]
    import urllib.error
    import urllib.parse
    import urllib.request                       # gateway 顶部没有 import urllib —— 别拿 NameError 冒充"服务没回话"
    try:
        with urllib.request.urlopen(FORM_GATE_URL + urllib.parse.quote(sid), timeout=6) as r:
            j = _json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        return ["判定口 8100 没回话（%s）⇒ 确认不了这张表回填过没有，按【未回填】处理。"
                "先把 8100 起来（python svc_8100_guard.py ensure）再报价，"
                "别拿「客户在等」当理由跳过回填。" % type(e).__name__]
    except Exception as e:                                   # noqa: BLE001 —— 内部错要说清是内部错
        return ["读取判定口时【我方代码出错】（%s: %s）⇒ 按未回填处理，但这不是 8100 的故障，去修读它的那段。"
                % (type(e).__name__, str(e)[:100])]
    if str(j.get("reason") or "") == "intent-has-no-form":
        return []                                            # 这条线没有表单输入口 ⇒ 不在射程
    if str(j.get("reason") or "") == "no-intent":
        return ["这条会话还没登记意图 ⇒ 第一步就没走（`python scripts/intent.py <档> <任务名>`）。"
                "没有任务目录就没有素材归位、没有回填、表单也不弹，报价没有依据可核。"]
    if not j.get("ok"):
        return ["判定口回的不是合格结论（%s）⇒ 按未回填处理" % str(j.get("error") or j)[:120]]
    gaps = j.get("prefill_gap") or []
    loose = j.get("loose_materials") or []
    unfilled = j.get("unfilled_required") or []
    if not (j.get("prefilled") or j.get("submitted")):   # 客户自己交齐了 = 比回填更权威，算满足
        return ["这张表从没回填过 ⇒ 右栏不会弹，客户手上没有任何可改的地方，"
                "把默认值写在对话里等于用聊天当参数表（不进载荷、不进合同）。"
                "先跑 python scripts/intent.py prefill --set @文件（没有东西可填也要 --set '{}'，"
                "那是在声明「我看过了」）。"]
    if gaps:
        return ["回填不完整：槽 %s 还没值，而任务目录里已经躺着对得上类型的素材 %s ⇒ "
                "先归位再回填（客户给的素材只能进 inputs/，从 src/ 回填会被服务端拒）"
                % (gaps, [_os.path.basename(str(x)) for x in loose][:4])]
    if unfilled:
        return ["必填格还空着 %s ⇒ 这些要么由 agent 从素材里判出来回填，要么确实要客户自己给；"
                "前者没做就是漏做，后者没给就别急着报价" % unfilled]
    return []


def skill_contract_lane_or_empty(capability: str, purpose: str) -> str:
    """信封里那一格只报事实：这条腿对应哪档合同，查不到就空着（不猜一个档名上去）。"""
    try:
        from backend import skill_contract as _sc
        return _sc.skill_for_spend(capability, purpose) or ""
    except Exception:                                        # noqa: BLE001
        return ""


def invoke(capability: str, purpose: str | None = None, account: str = "local",
           params: dict | None = None) -> dict:
    """skill 经 tool-jobs 调的总入口：resolve→估价→precheck/preauth→call→commit/rollback→信封。
    不足即停并提示充值（不发起生成，PRD:284）；失败回滚该条（PRD:346）。"""
    params = params or {}
    account = account or "local"
    r = resolve(capability, purpose)
    est = meter(capability, r["purpose"], params)["credits_estimate"]
    # 🔴 合同回执判在【批准之前】：反过来的话，一次"没有回执"的调用会先把人批下来的段数吃掉
    #    再报失败 —— 那是拿别人的授权替自己的绕道买单（2026-10-10 已有会话这样吃空过别人的额度）。
    _cp = spend_contract_problems(capability, r["purpose"], params)
    if _cp:
        return {"ok": False, "capability": capability, "purpose": r["purpose"], "result": None,
                "credits": {"estimated": est, "charged": 0,
                            "balance_after": ledger().balance(account)},
                "error": ("skill_contract（花钱那一刀认回执）：%s" % "；".join(_cp))[:700],
                "contract": {"problems": _cp, "lane": skill_contract_lane_or_empty(capability,
                                                                                   r["purpose"])}}
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
                                    count=_cnt, credits=est, session=_session_of(params))
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
        if capability == "video.analyze" and params.get("path"):
            # 🔴 素材分析台账：跑完这条腿才记账 ⇒ agent 伪造不了『我看过图了』。
            #    写失败不影响业务（返回值里带原因），但【不许抛】——台账是留痕，不是新故障点。
            #    hold_id = 这次调用的预扣号，判据只认带凭证的行（没它就算 agent 手写的一条）。
            record_media_analysis(params.get("path"), capability, r["purpose"],
                                  _session_of(params), {
"charged": charged}, hold_id=(hold or {}).get("hold_id") or "")
        return {"ok": True, "capability": capability, "purpose": r["purpose"], "result": result,
                "credits": {"estimated": est, "charged": charged, "tokens": tokens, "basis": basis,
                            "hold_id": hold["hold_id"],
                            # 🔴 这里原来写的是 `done.get(...)` —— `done` 在本函数里从来没赋值过（10-01 起，
                            #    commit 9ace7d7），所以【每一次成功的调用】都会在返回那一刻 NameError，
                            #    被外层 except 吞成 ok:false + 回滚预扣 + 退段 ⇒ 成功看起来和失败一模一样。
                            #    账本上只留 "preauth 紧跟 rollback"，和真 502 无法区分（今天就被误导过一次）。
                            "balance_after": ledger().balance(account)},
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


# ─────────────────────────────────────────────────────────────────────────────
# 素材上传口（2026-10-09）：把"哪个域名 + 哪个端点 + 哪把 key"这件事收在一处。
# 起因是一条会话拿不到公网 URL、把原因归成"RH token 失效"，实测三种死法都与 key 好坏无关：
#   · /openapi/v1/upload_file 不存在 —— 但 HTTP 回【200】，body 里才是 {"code":404,"msg":"NOT_FOUND"}
#   · host 写成 runninghub.cn ⇒ code=1 "API Key不存在"（RH 的 key 按站点分，.ai 那把在 .cn 无效）
#   · 拿错家（PRIME_YZ_* 是另一家）⇒ 直接 error
# 设计决策与强度边界见 C:\dsh\docs\ADR-model-gateway.md「素材上传口」节。
# 🔴 这条口【不计费】：0 分、不建 hold、不进 SPEND_APPROVAL_REQUIRED —— 它不花钱，
#    但必须留痕（谁在什么时候传了什么），所以有独立台账，不往钱账本里塞 0 分流水。
MEDIA_UPLOAD_HOSTS = {"runninghub": "https://www.runninghub.ai"}
MEDIA_UPLOAD_ENDPOINTS = {"runninghub": "/openapi/v2/media/upload/binary"}
# key 优先级 = 各条腿【现在实际在用】的顺序，收口时不改行为（改了就会把某条腿的素材传到别的账号上）。
MEDIA_UPLOAD_KEY_ORDER = {
    "runninghub": {
        "": ("RUNNINGHUB_ENTERPRISE_API_KEY", "RUNNINGHUB_API_KEY"),
        "wan3": ("RUNNINGHUB_WAN3_API_KEY", "RUNNINGHUB_ENTERPRISE_API_KEY", "RUNNINGHUB_API_KEY"),
        "h3": ("RUNNINGHUB_H3_API_KEY", "RUNNINGHUB_ENTERPRISE_API_KEY", "RUNNINGHUB_API_KEY"),
        "motion": ("RUNNINGHUB_MOTION_WAN22_API_KEY", "RUNNINGHUB_ENTERPRISE_API_KEY", "RUNNINGHUB_API_KEY"),
        "lipsync": ("RUNNINGHUB_LIPSYNC_API_KEY", "RUNNINGHUB_ENTERPRISE_API_KEY", "RUNNINGHUB_API_KEY"),
    },
}
MEDIA_UPLOAD_MAX_BYTES = 200 * 1024 * 1024
_MEDIA_UPLOAD_LOG_DEFAULT = ".gateway_media_uploads.json"


def resolve_media_target(provider: str = "runninghub", key_hint: str = "") -> dict:
    """返回 {host, endpoint, url, key, key_src}；provider/hint 不认识就抛 ValueError（不静默换家）。

    🔴 取不到 key 也抛，不内置兜底 key —— 静默拿某把写死的 key 去花钱/占存储，那种事在账上对不到人。
    """
    p = str(provider or "runninghub").strip().lower()
    if p not in MEDIA_UPLOAD_HOSTS:
        raise ValueError("未知上传供应商 %r（可选: %s）" % (provider, ", ".join(sorted(MEDIA_UPLOAD_HOSTS))))
    table = MEDIA_UPLOAD_KEY_ORDER[p]
    h = str(key_hint or "").strip().lower()
    if h not in table:
        raise ValueError("%s 没有 key_hint=%r（可选: %s）"
                         % (p, key_hint, ", ".join(sorted(x or "(默认)" for x in table))))
    host, endpoint = MEDIA_UPLOAD_HOSTS[p], MEDIA_UPLOAD_ENDPOINTS[p]
    for name in table[h]:
        key = str(getattr(settings, name, "") or "").strip()
        if key:
            return {"host": host, "endpoint": endpoint, "url": host + endpoint,
                    "key": key, "key_src": name}
    raise ValueError("%s/%s 取不到密钥：按顺序试了 %s，都没有值"
                     % (p, h or "(默认)", " → ".join(table[h])))


def _media_upload_note(rec: dict) -> None:
    """台账：append-only JSON。写失败只回 error，绝不让上传白做一次（也绝不吞掉 key 本体）。"""
    import json as _j
    path = _os.environ.get("GATEWAY_MEDIA_UPLOADS") or _os.path.join(
        _os.path.dirname(_os.path.abspath(__file__)), _MEDIA_UPLOAD_LOG_DEFAULT)
    try:
        rows = []
        if _os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                rows = _j.loads(f.read() or "[]")
            if not isinstance(rows, list):
                rows = []
        rows.append(rec)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(_j.dumps(rows[-2000:], ensure_ascii=False, indent=1))
        _os.replace(tmp, path)
    except Exception as e:                                          # noqa: BLE001
        rec["_note_error"] = "%s: %s" % (type(e).__name__, str(e)[:120])


def upload_media(file_path: str, provider: str = "runninghub", key_hint: str = "",
                 account: str = "local") -> dict:
    """把本地素材传到供应商公网口，返回归一信封（**不计费、不建预扣**）。

    file_name 是【要填进媒体节点的那个引用名】（RH 要 openapi/<hash>.png，不是 download_url），
    download_url 才是公网可取直链 —— 两个都给，别让调用方猜哪个是哪个。
    """
    import hashlib
    import json as _j
    import mimetypes
    import time as _t
    import urllib.error
    import urllib.request
    import uuid as _u

    out = {"ok": False, "capability": "media.upload", "provider": str(provider).lower(),
           "purpose": str(key_hint or "").lower(), "account": account,
           "result": None, "credits": {"estimated": 0, "charged": 0, "basis": "free"},
           "error": None}
    rec = {"ts": round(_t.time(), 3), "provider": out["provider"], "key_hint": out["purpose"],
           "account": account, "file": "", "size": 0, "file_name": "", "key_sha8": "",
           "ok": False, "error": ""}
    try:
        tgt = resolve_media_target(out["provider"], out["purpose"])
        rec["key_sha8"] = hashlib.sha1(tgt["key"].encode("utf-8")).hexdigest()[:8]
        p = _os.path.abspath(str(file_path or ""))
        rec["file"] = p
        if not _os.path.isfile(p):
            raise ValueError("文件不存在或不是文件: %s" % p)
        size = _os.path.getsize(p)
        rec["size"] = size
        if size <= 0:
            raise ValueError("空文件不传（上游会回 200 + code!=0，白占一次台账）")
        if size > MEDIA_UPLOAD_MAX_BYTES:
            raise ValueError("文件 %d 字节超过上限 %d，不传" % (size, MEDIA_UPLOAD_MAX_BYTES))
        with open(p, "rb") as f:
            content = f.read()
        ctype = mimetypes.guess_type(p)[0] or "application/octet-stream"
        boundary = ("----weeksir" + _u.uuid4().hex).encode("ascii")
        head = (b"--" + boundary + b'\r\nContent-Disposition: form-data; name="file"; filename="'
                + _os.path.basename(p).encode("utf-8", "replace") + b'"\r\nContent-Type: '
                + ctype.encode("ascii", "replace") + b"\r\n\r\n")
        body = head + content + b"\r\n--" + boundary + b"--\r\n"
        req = urllib.request.Request(tgt["url"], data=body, method="POST", headers={
            "Authorization": "Bearer " + tgt["key"],
            "Content-Type": "multipart/form-data; boundary=" + boundary.decode("ascii"),
            "User-Agent": BROWSER_UA,
        })
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                raw = r.read()
            status = 200
        except urllib.error.HTTPError as e:
            raw = e.read() or b""
            status = e.code
        try:
            data = _j.loads(raw.decode("utf-8", "replace") or "{}")
        except Exception:                                            # noqa: BLE001
            data = {}
        payload = data.get("data") if isinstance(data.get("data"), dict) else {}
        file_name = str(payload.get("fileName") or "")
        url = str(payload.get("download_url") or payload.get("url") or "")
        # 🔴 状态码 200 不等于成功：v1 那个不存在的端点就是回 200 + body 里 code:404。
        code_ok = str(data.get("code")) in ("0", "None") or data.get("success") is True
        if status != 200 or not code_ok or not (file_name or url):
            raise RuntimeError("HTTP %s / code=%s msg=%s（没拿到 fileName 或 download_url，不算传上去了）"
                               % (status, data.get("code"), str(data.get("msg"))[:80]))
        out["ok"] = True
        out["result"] = {"file_name": file_name, "download_url": url, "size": size,
                         "content_type": ctype, "host": tgt["host"], "endpoint": tgt["endpoint"],
                         "key_src": tgt["key_src"]}
        rec["file_name"], rec["ok"] = file_name, True
        return out
    except Exception as e:                                           # noqa: BLE001
        out["error"] = "%s: %s" % (type(e).__name__, str(e)[:220])
        rec["error"] = out["error"][:300]
        return out
    finally:
        # 🔴 台账写不进去【不许】把已经成功的上传打断：早先直接 `_media_upload_note(rec)` 放在 finally 里，
        #    这个函数内部虽然自己 catch，但只要它本身炸（比如被 monkeypatch、磁盘满在 open 之前），
        #    异常就从 finally 逃出去盖掉 return ⇒ 素材明明传上去了、调用方拿到一个 traceback。
        try:
            _media_upload_note(rec)
        except Exception as e:                                       # noqa: BLE001
            out["ledger_error"] = "%s: %s" % (type(e).__name__, str(e)[:120])
        if rec.get("_note_error"):
            out["ledger_error"] = rec["_note_error"]   # 登记炸了 ≠ 上传炸了：结果照给，但要说出来


def media_uploads(limit: int = 20) -> list:
    """读台账尾部若干条（只给运维/闸看；里面永远没有 key 本体，只有 sha8）。"""
    import json as _j
    path = _os.environ.get("GATEWAY_MEDIA_UPLOADS") or _os.path.join(
        _os.path.dirname(_os.path.abspath(__file__)), _MEDIA_UPLOAD_LOG_DEFAULT)
    if not _os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            rows = _j.loads(f.read() or "[]")
        return rows[-int(limit):] if isinstance(rows, list) else []
    except Exception:                                                # noqa: BLE001
        return []


def pop_approval_window(pending_id: str, port: int = 8100) -> str:
    """把【置顶批准窗】弹到人面前。返回 ""=已弹；否则返回原因 —— 弹不出来必须说，
    不能让人以为"在等人点"（那正是当年 MessageBox 那版翻车的地方：没人点也当点了）。

    🔴 这不是"第二种人证"，判定逻辑一行都没改：额度仍只有服务端 consume_by_click 签得出，
       窗口只是同一个 `POST /api/spend/approve{pending_id}` 的另一个壳，核销码不进窗口/stdout。
       没人点 ⇒ pending 过期 ⇒ 回到拒绝态（fail-closed）。
       系统 MessageBox 是【已废弃】路线（实测无人参与会自返 IDYES），这里绝不用它。
    """
    if _os.name != "nt":
        return "非 Windows，不弹窗"
    if str(_os.environ.get("GATEWAY_APPROVAL_POPUP", "")).strip() == "0":
        return "已按 GATEWAY_APPROVAL_POPUP=0 抑制"
    import subprocess
    script = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "approval_popup.py")
    if not _os.path.isfile(script):
        return "找不到 approval_popup.py"
    py = _sys.executable or "python"
    if py.lower().endswith("python.exe"):
        alt = _os.path.join(_os.path.dirname(py), "pythonw.exe")
        if _os.path.isfile(alt):
            py = alt                       # 别在人桌面上留一个黑控制台
    try:
        subprocess.Popen([py, script, str(pending_id), "--port", str(port)],
                         stdin=None, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        return ""
    except Exception as e:                                          # noqa: BLE001
        return "%s: %s" % (type(e).__name__, str(e)[:80])


def _cli(argv):
    print = _cprint          # 本函数内所有证词走安全打印（见 _cprint 的事故说明）
    cmd = argv[0] if argv else "caps"
    if cmd == "caps":
        for cap in CAPABILITIES:
            purposes = ", ".join(p for (c, p) in ROUTES if c == cap)
            # 🔴 每条能力自带"要不要人批准"：判据不在这里重写，读 needs_spend_approval（唯一源仍是
            #    SPEND_APPROVAL_EXEMPT）。以前 caps 只回 purposes ⇒ agent 问不出批准要求，只能凭文档记忆，
            #    一漂就把"生图免批"外推到换脸上（2026-10-10 现网：它先弹批准、被我纠正后又反过来想免批）。
            print("%-16s 批准=%-4s purposes: %s" % (
                cap, "免批" if not needs_spend_approval(cap) else "要批", purposes))
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
                    # 🔴 provider 必须印在这一行上（2026-10-10 事故：agent 报价时凭记忆写了个上游名，
                    # 而这条唯一现读口只给"lane + 分数"，厂商标要再去翻 ROUTES ⇒ 两处一比就记错）。
                    prov = (ROUTES[(c, p)] or {}).get("provider", "")
                    print(f"  {mark} {cap}/{p:<14} {unit_price(c, p):>4} 分  [{prov}] {extra}")
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
        # 🔴 报价那一刻就把"这条腿要不要人批准"一起吐出来。判据不在这里重写 —— 读 needs_spend_approval
        #    （唯一源仍是 SPEND_APPROVAL_EXEMPT）。以前 quote 只回分数 ⇒ agent 问不出批准要求，
        #    只能凭 AGENTS.md 那段散文的记忆外推，一漂就把"生图免批"套到换脸（image.edit/faceswap）上。
        out = meter(cap, pur, params)
        need = needs_spend_approval(cap)
        blob = out if isinstance(out, dict) else {"meter": out}
        blob = dict(blob)
        blob["这条腿"] = "%s/%s" % (cap, pur)
        blob["需要批准"] = bool(need)
        blob["批准判据"] = ("默认要批：不在 SPEND_APPROVAL_EXEMPT 免批名单里，所以先建条子、由人点批准窗"
                          if need else "免批：在 SPEND_APPROVAL_EXEMPT 里（仍走预扣/结算/账本，不免账）")
        print(json.dumps(blob, ensure_ascii=False, indent=2))
    elif cmd == "credits":
        led = ledger()
        print("余额:", _json.dumps(led.accounts(), ensure_ascii=False))
        for r in led.audit()["by_route"]:
            print(f"  {r['route']:<30} {r['status']:<12} {r['calls']:>3} 次 {r['credits']:>6} 分")
    elif cmd == "holds":
        # 未结算的预扣（重启/被杀留下的）。只读、零副作用。
        rows = ledger().pending_holds(int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else 50)
        print(_json.dumps({"pending": rows, "count": len(rows),
                           "total_credit": sum(int(r["amount"] or 0) for r in rows),
                           "ledger": _LEDGER_PATH}, ensure_ascii=False, indent=1))
        return 0
    elif cmd == "holds-rollback":
        # 🔴 只认【精确 hold_id】，没有批量开关：预扣可能正被另一条进程花着，
        #    批量退 = 把正在花的分退回去再花一次（双花）。要退就一条条指名，退错还能 rollback 反悔不了。
        if len(argv) < 2 or str(argv[1]).strip() in ("all", "*", "--all", "-y") or str(argv[1]).startswith("--"):
            print("拒绝批量退：预扣可能正被别的进程花着（退回去=可以再花一次=双花）。"
                  "请按 id 指名：python backend/gateway.py holds-rollback <hold_id>（先跑 holds 看清单）")
            return 2
        r = ledger().rollback(argv[1].strip())
        print(_json.dumps(r, ensure_ascii=False))
        return 0 if r.get("ok") else 3
    elif cmd == "test":
        res = test_models()
        print(f"真·测通结果  {res['summary']}")
        for key, v in res["routes"].items():
            ms = f"{v['ms']}ms" if v["ms"] is not None else "-"
            print(f"  {v['status']:<11} {key:<26} {v['provider']}/{v['model']:<22} {ms:>8}  {v['detail']}")
    elif cmd == "analyze":
        # 用法: python gateway.py analyze <图片/视频路径> [问题...] [--account X] [--max-tokens N]
        # 🔴 这条子命令以前直接走 call()（能力分发器），而【记账/预扣/结算全在 invoke() 里】
        #    ⇒ 它看着是"网关的正规入口"，实际一条账都不留（2026-10-12 现网：agent 走这条路跑了三次分析，
        #    台账 0 条，还汇报"预扣/结算/台账齐全"；合同按 sha8 对账时它反而过不了）。
        #    现在：一条能力只有一个入口 —— 转 invoke()，与 call 子命令同轨。
        import json
        rest = list(argv[1:])
        acct = "local"
        mtok = None
        if "--account" in rest:
            i = rest.index("--account")
            if i + 1 >= len(rest):
                print("--account 后面要跟成员名")
                return 2
            acct, rest = rest[i + 1], rest[:i] + rest[i + 2:]
        if "--max-tokens" in rest:
            i = rest.index("--max-tokens")
            if i + 1 >= len(rest):
                print("--max-tokens 后面要跟整数（默认 300，看视频建议 1200+，否则正文被截成前几秒）")
                return 2
            try:
                mtok = int(rest[i + 1])
            except ValueError:
                print("--max-tokens 要整数")
                return 2
            rest = rest[:i] + rest[i + 2:]
        if not rest:
            print("用法: python gateway.py analyze <媒体路径> [问题...] [--account X] [--max-tokens N]")
            return 2
        path = rest[0]
        params = {"path": path,
                  "question": " ".join(rest[1:]) or "用一句话描述这个画面的关键信息。"}
        if mtok:
            params["max_tokens"] = mtok
        env = invoke("video.analyze", None, acct, params)
        print(json.dumps(env, ensure_ascii=False, indent=2))
        return 0 if env.get("ok") else 1
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
    elif cmd in ("approve", "approvals", "revoke", "allowance"):
        # 花钱批准：**人在自己的终端里签**，agent 只能报"需要批准"并给出这条命令。
        #   python backend/gateway.py approve video.generate omni-t2v --count 3 --credits 150 --scope "fu-tie 3段15秒"
        import json as _j
        # 打印统一走 _cli 顶部那句 `print = _cprint`（模块级那份）——这里原来嵌过一份同名函数，
        # 结果 `_cprint` 变成 _cli 的局部名，`print = _cprint` 在赋值前引用它直接 UnboundLocalError。
        if cmd == "allowance":
            # 🔴 只读："这一批批到没有、还剩几段" —— 别再用 approve/approvals 猜，更别调 check_spend_approval
            #    试探（那一下就是消费点，2026-10-10 就是这样把用户批过的额度吃光、逼人第二次点批准）。
            if len(argv) < 2:
                print('用法: gateway.py allowance <capability> [purpose] --account local --session <会话id> '
                      '[--count 1 --credits 60]\n'
                      '     例: gateway.py allowance video.generate wan3-prime --account local '
                      '--session "$env:DSH_SESSION_ID" --count 1 --credits 60')
                return 2
            cap = argv[1]
            rest, pur = argv[2:], "*"
            if rest and not rest[0].startswith("--"):
                pur, rest = rest[0], rest[1:]
            opts = {"--count": "count", "--credits": "credits", "--account": "account", "--session": "session"}
            kv = {"count": 1, "credits": 0, "account": "local", "session": ""}
            i = 0
            while i < len(rest):
                if rest[i] in opts and i + 1 < len(rest):
                    key = opts[rest[i]]; raw = rest[i + 1]
                    kv[key] = raw if key in ("account", "session") else int(raw)
                    i += 2
                else:
                    i += 1
            sid = kv["session"] or _session_of({})
            r = peek_spend_approval(cap, pur, kv["account"], count=kv["count"],
                                    credits=kv["credits"], session=sid)
            _cprint(_j.dumps(r, ensure_ascii=False, indent=1))
            _cprint("（这条命令只读：不建预扣、不扣分、不清段。额度只在真正提交那一下被消费。）")
            return 0 if r.get("allowed") else 4
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
            print("用法: gateway.py approve <capability> [purpose] --count-from <任务目录> --scope \"...\" [--account local] [--ttl 86400]"      "  ← 段数与分数从盘上算；或 --count N --credits X（自己填，跑一半被拒自负）")
            return 2
        cap = argv[1]
        rest, pur = argv[2:], "*"
        if rest and not rest[0].startswith("--"):
            pur, rest = rest[0], rest[1:]
        opts = {"--count": "count", "--credits": "credits", "--account": "account",
                "--scope": "scope", "--ttl": "ttl", "--session": "session",
                "--count-from": "count_from"}
        kv = {"purpose": pur, "count": 1, "credits": 0, "account": "local", "scope": "", "ttl": None,
              "session": "",
              "session": "", "count_from": "", "_c_given": False, "_x_given": False}
        i = 0
        while i < len(rest):
            if rest[i] in opts and i + 1 < len(rest):
                raw = rest[i + 1]; key = opts[rest[i]]
                kv[key] = raw if key in ("account", "scope", "session", "count_from") \
                    else float(raw) if key in ("ttl",) else int(raw)
                if rest[i] == "--count":
                    kv["_c_given"] = True
                if rest[i] == "--credits":
                    kv["_x_given"] = True
                i += 2
            else:
                i += 1
        if kv["count_from"]:
            try:
                _n, _c, _why = count_from_args(cap, pur, kv["count_from"])
            except ValueError as e:
                print("拒建条子：%s" % e)
                return 2
            if kv["_c_given"] and int(kv["count"]) != _n:
                print("--count=%s 与盘上算出的段数 %d 不一致（%s）⇒ 拒。" "要么去掉 --count 让它自己算，要么先改 TIMELINE 再批。"
                      % (kv["count"], _n, _why))
                return 2
            if kv["_x_given"] and int(kv["credits"]) != _c:
                print("--credits=%s 与 %d 段 × 现读单价算出的 %d 分不一致 ⇒ 拒（分数只许由单价推，不许凭记忆写）"
                      % (kv["credits"], _n, _c))
                return 2
            kv["count"], kv["credits"] = _n, _c
            print("🔒 段数与分数来自盘上事实：%s" % _why)
            print("   ⇒ 这一次点击覆盖 %d 段；换路由/加段数/超分数仍会被拒（不双花）。" % _n)
            if "按单段" in _why:
                print("   ⚠️ 这条按【单段】建：真跑出来是多段时，后面的段会被拒，需要回来重新批。")
        elif not kv["_c_given"] and needs_spend_approval(cap):
            print("提示：没给 --count-from <任务目录> ⇒ 段数是你手填的。要『批一次跑完整条』，"
                  "改用 --count-from 让段数从 TIMELINE/段目录算。")

        if not needs_spend_approval(cap):
            print(f"⚠️ {cap} 本来就不需要批准（免批类：文本/看图）。这条 approve 只是留档，不影响任何调用。")
        # 🔴 【有合同的那条线】报价之前必须回填过表 —— 判据只读 8100 的结论，网关不解析第二份字段表。
        #    为什么钉在这一步：approve 是花钱前唯一【必然发生】的命令，绑在这里才不用指望 agent 自觉；
        #    绑在 runner 上就拦不住「不进 runner、把默认值写在对话里」那种走法（2026-10-11 16:30 现网）。
        if skill_contract_lane_or_empty(cap, kv["purpose"]):
            _fb = form_prefill_blockers(kv["session"])
            if _fb:
                print("⚠️ 没有建待批条子 —— 0 分、0 额度，因为流程没走完：")
                for _x in _fb:
                    print("   - %s" % _x)
                print("   为什么拦在这儿：报价的根据应该是【客户在右栏看得见、可改的那张表】，"
                      "不回填就没有那张表；这条命令是花钱前唯一必经的一步，绑在这里才不靠自觉。")
                return 1
        # 🔴 这一步【只建待批，不给额度】。系统弹窗已废（实测没人点也会返回【是】= 假人证）。
        #    额度只能由"人看到报价之后"的一次核销拿到：DSH 审批卡点【允许一次】，或人在自己终端跑 consumed。
        p = create_spend_pending(cap, kv["purpose"], kv["account"], int(kv["count"]),
                                int(kv["credits"]), kv["scope"], kv["session"], kv["ttl"])
        print(p["quote"])
        # 🔴 报价必须【自带】那张计划表（2026-10-09 定的"花钱那一刀前后各一张表"）。
        #    以前靠 agent 记得跑 contract_tables.py —— 现网证伪：可跳过的步骤等于不存在的步骤，
        #    三条会话的报价都没带表。表由 approve 打出来，人复制这条报价就一定会看到。
        try:
            from backend import skill_contract as _sc    # repo 根已由 bootstrap 加入 sys.path
            _lane = _sc.skill_for_spend(cap, kv["purpose"])
            if _lane:
                print("\n" + _sc.plan_table(_lane))
            else:
                print("\n⚠️ 没有一条 skill 合同的 spend 等于 %s/%s ⇒ 出不了计划表。"
                      "这一档【没有合同约束】：表缺了就不要点头，先让它把 contract: 段补上。"
                      % (cap, kv["purpose"]))
        except Exception as _e:                           # noqa: BLE001
            print("⚠️ 计划表没带出来（%s: %s）⇒ 视同【没有表】，不要当成已经给人看过了。"
                  % (type(_e).__name__, str(_e)[:120]))
        # 🔴 核销码【绝不进 stdout】：这条命令多半是 agent 跑的，它读得到 stdout 就能自己
        #    `consumed <id> <码>` 伪造核销 ⇒ 码一旦进管道，整套人证归零。
        #    码只有两条给人的路：① DSH 审批卡（插件经 with_code 取，见 spend_pending）；
        #                      ② 人在【自己的终端】里跑 `gateway.py code <pending_id>`。
        print("\npending: %s   （%.0f 分钟内有效）" % (p["id"], SPEND_PENDING_TTL / 60))
        pop = pop_approval_window(p["id"])
        # 🔴 链接一律带 ?session= —— 批准页现在按它筛卡（不带 = 客户看到的是全局列表，
        #    多会话并发时他能替别的方案点头）。这一行是 agent 唯一许贴给客户的那条地址。
        appr_url = "http://127.0.0.1:8100/approval?session=%s" % (kv.get("session") or "")
        if not pop:
            print("⇒ 现在还没有任何批准额度。已把【置顶批准窗】弹到人桌面上："
                  "核对报价后点【批准，开始跑】才签出额度；不点 = 过期即拒（fail-closed）。")
            print("   弹窗没被人看到时的备选地址（要给人就用这条，带会话号）：%s" % appr_url)
        else:
            print("⇒ 现在还没有任何批准额度。⚠️ 弹窗没起来（%s）⇒ 请打开 %s 点一次。"
                  % (pop, appr_url))
        print("   备选：① DSH 对话里那张品牌审批卡点【允许一次】；"
              "② 人在【自己的终端】跑 gateway.py code %s（码不进本回复，免得跑命令的一方自己用掉）" % p["id"])
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
        sess = _session_of({})        # 同一处读法；--session 只是 CLI 显式覆盖（下面就是）
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

    elif cmd == "upload":
        # 用法: gateway.py upload <本地文件> [--provider runninghub] [--key-hint wan3|h3|motion|lipsync] [--account X]
        # 🔴 不计费：0 分、不建预扣、不要批准条子 —— 但它会把素材传进我们的 RH 账号，所以有独立台账。
        import json as _j
        if len(argv) < 2:
            print("用法: python backend/gateway.py upload <本地文件> "
                  "[--provider runninghub] [--key-hint wan3|h3|motion|lipsync] [--account X]")
            return 2
        prov = argv[argv.index("--provider") + 1] if "--provider" in argv and \
            argv.index("--provider") + 1 < len(argv) else "runninghub"
        hint = argv[argv.index("--key-hint") + 1] if "--key-hint" in argv and \
            argv.index("--key-hint") + 1 < len(argv) else ""
        acct = argv[argv.index("--account") + 1] if "--account" in argv and \
            argv.index("--account") + 1 < len(argv) else "local"
        r = upload_media(argv[1], provider=prov, key_hint=hint, account=acct)
        print(_j.dumps(r, ensure_ascii=False, indent=2))
        if r.get("ok"):
            d = r["result"]
            print("⇒ 0 分（不计费）。媒体节点要填的是 file_name=%s；download_url 只是给人看的直链。" % d["file_name"])
            return 0
        print("✗ 没传上去。别先怀疑「token 失效」——拒因在上面，按 host / 端点 / code 三项对（v1 端点会回 200 但 body 是 NOT_FOUND）")
        return 3

    elif cmd == "analyses":
        # 只读：看素材分析台账（sha8 前 8 位，不含任何凭据）。判『看过没看过』只认这里，不认谁嘴上说过。
        rows = media_analysis_rows()
        lim = None
        for a in argv[1:]:
            if a.isdigit():
                lim = int(a)
        tail = rows[-lim:] if lim else rows[-25:]
        print("素材分析台账 共 %d 条（显示最近 %d 条）· 文件=%s" % (len(rows), len(tail), media_analysis_log_path()))
        _nokey = 0
        for r in tail:
            _hk = str(r.get("hold_id") or "")
            if not _hk:
                _nokey += 1
            print("  %s  %-10s %-26s %s/%s  会话=%s  %s 分  凭证=%s  %s" % (
                _time.strftime("%m-%d %H:%M:%S", _time.localtime(r.get("ts") or 0)),
                (r.get("sha8") or "?"), r.get("name", "")[:26], r.get("capability"), r.get("purpose"),
                (str(r.get("session") or "")[:12] or "无"), r.get("credits"),
                (_hk[-8:] if _hk else "🔴无（不算证据）"), r.get("error") or ""))
        if _nokey:
            print("  🔴 上面有 %d 条【没有调用凭证】⇒ 判据不认它们（那种行是有人 import 记账函数手写的，"
                  "2026-10-12 现网发生过）" % _nokey)
        if not rows:
            print("  （空）⇒ 还没有任何一条素材被真看过；超级复刻提交前要求每个素材都在这里有一条记录。")
        return 0
    elif cmd == "uploads":
        import json as _j
        print(_j.dumps(media_uploads(int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else 20),
                       ensure_ascii=False, indent=1))
        return 0

    else:
        print("用法: python backend/gateway.py [caps | prices | credits | resolve <cap> [purpose] | "
              "call <cap> [purpose] [--account X] [--json '{...}'] | test | analyze <path> [问题] | "
              "upload <file> [--provider X] [--key-hint X] | uploads [N] | "
              "holds [N] | holds-rollback <hold_id> | "
              "approve <cap> [purpose] --count N --credits X --scope \"…\" | code <pending_id> | consumed <pending_id> <码> | approvals | revoke <id>]")


if __name__ == "__main__":
    # 🔴 原来写成 `_cli(...)` 把返回码丢了 ⇒ 用法错误也退 0，调用方（脚本/闸）会把"没执行"当"执行成功"。
    raise SystemExit(_cli(_sys.argv[1:]))
