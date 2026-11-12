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


def _quiet_child_windows(force: bool | None = None) -> int:
    """【本进程没有控制台】时，给每个 subprocess 子进程补上 CREATE_NO_WINDOW。返回 1=装了 / 0=不需要装。

    为什么要有这一道（2026-10-22 现网"桌面上很多 cmd 窗口一闪而过"）：8100 是 pythonw，它点火的 runner
    也是 pythonw；父进程没有控制台时，Windows 会给每个【控制台子系统】的子进程现开一个控制台 = 一个新黑框。
    引擎里 96 个 spawn 点只有 2 个自己带了隐藏标志，逐个改既不现实也挡不住下一次新增。
    根修在 `model_admin._spawn_kickoff`（给 runner 一个建好即隐藏的控制台，子孙继承）；这一道是【兜底】，
    管的是别的入口：agent 手敲、别的常驻服务、以及还没重启的旧版 8100 点起来的那台。
    只包 `subprocess.Popen` 这一个名字：`run/call/check_output/check_call` 都在模块内查它，一处生效全覆盖
    （有 `import subprocess 里单独 from 拿 Popen` 的写法就会漏，闸 U4 数着）。
    调用方自己指定过控制台形态（NEW_CONSOLE / NO_WINDOW / DETACHED）的一律不动。
    `force` 只给闸做两条路的测试缝用（生产调用一律不传）。
    """
    if _os.name != "nt":
        return 0
    try:
        import ctypes
        import subprocess as _sp
        if force is False:
            return 0                       # 测试缝：按"有自己的控制台"那条路走
        if force is None and ctypes.windll.kernel32.GetConsoleWindow():
            return 0                       # 有自己的控制台 ⇒ 子孙继承，桌面本来就没多出窗
        _real = _sp.Popen
        if getattr(_real, "_weeksir_quiet", False):
            return 0                           # 已装过（幂等：重复 import 不许叠两层）
        _CONSOLE_FLAGS = 0x00000010 | 0x08000000 | 0x00000008   # NEW_CONSOLE | NO_WINDOW | DETACHED

        class _QuietPopen(_real):
            _weeksir_quiet = True

            def __init__(self, *a, **kw):
                f = int(kw.get("creationflags") or 0)
                if not (f & _CONSOLE_FLAGS):
                    kw["creationflags"] = f | 0x08000000
                super().__init__(*a, **kw)

        _sp.Popen = _QuietPopen
        return 1
    except Exception:                          # noqa: BLE001 —— 窗口罩子坏了绝不能带走一条花钱的调用
        return 0


_quiet_child_windows()

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
    # 🔴 2026-10-13 用户指令：gpt-5.6 这两条腿（gpt56 / gpt-compile）**已屏蔽** —— 键在
    #    `model_prices.json.disabled`，`resolve()` 在【预扣之前】就拒（0 分、不发 HTTP、面板显示"已屏蔽"），
    #    路由定义与单价原样留着（要恢复就是删那两行屏蔽）。强度边界：这一层只管走网关的调用，
    #    video-skill 里那批 OPENAI_*/gpt-5.6-sol 直连腿照样在跑，网关物理上看不见（见 ADR『账本外直连』）。
    # —— grok-4.6（2026-10-13 接，用户点名）——
    # 同一个 hub（hub.nocannobb.com/v1）的【另一把 key】：SR_GROK_*（sha8 见 .env，源码无明文）。
    # 之前它只在 8090 进程里直连（prompt_reverser / fuse-product / hypit_agent），不进预扣·结算·台账；
    # 挂上这条 purpose 之后，任何要 grok-4.6 的地方一律走 gateway，账本才有这一笔。
    # 按 token 结算（同 text.complete 其余腿：token_rate 10 分/1k，整条单价只当报价档）。
    ("text.complete", "grok46"):    {"provider": "nocannobb",  "client": "openai_compat", "fn": "chat", "fields": {"base_url": "SR_GROK_BASE_URL", "api_key": "SR_GROK_API_KEY", "model": "SR_GROK_MODEL"}},
    # 2026-10-13 用户点名接入：qwen3.8-27b（ninfer / api.qn-einnp.com）。模型名只留一份 ⇒ config.TEXT_MODEL_QWEN38_27B。
    # 同 model id 的【阿里百炼】那条腿 ("text.complete","qwen38"/provider=dashscope) 同日由用户裁定删除：
    # 那把 key 整片被账户欠费拦住（2026-10-13 实调仍回 400 Arrearage），留着只会被当成可兜底的腿。
    # 充值后要加回来就复用这条的 model 字段，base_url/api_key 换成 DASHSCOPE_CHAT_BASE_URL / DASHSCOPE_API_KEY。
    ("text.complete", "qwen38-ninfer"): {"provider": "qn-ninfer", "client": "openai_compat", "fn": "chat", "fields": {"base_url": "QN_NINFER_BASE_URL", "api_key": "QN_NINFER_API_KEY", "model": "TEXT_MODEL_QWEN38_27B"}},
    ("text.complete", "mimo_pro"):  {"provider": "mimo",       "client": "openai_compat", "fn": "chat", "fields": {"base_url": "MIMO_BASE_URL",           "api_key": "MIMO_API_KEY",    "model": "TEXT_MODEL_MIMO_PRO"}},
    # —— magpie 本地池的 free 腿（2026-10-20 接入；**主腿顺序唯一源 = config.MAGPIE_TEXT_LANES**）——
    # purpose `magpie` 是【入口别名】= 表头那条腿（现在是 deepseek-v4.1-flash，用户 2026-10-20 点名），
    # 走 invoke 时按表逐条换腿；显式点名 magpie_mimo / magpie_glm 就只钉那一条、失败也不换。
    # 🔴 这三条【不吃视频】：桥的 chat part 白名单只有 text/image_url/file，带 video_url 时 prompt_tokens 一字不涨
    #   （2026-10-20 自测 scratch/probe8099/magpie_probe.py）。⇒ 只挂文本与看图(图)，分镜腿走 video.analyze/default。
    ("text.complete", "magpie"):         {"provider": "magpie", "client": "openai_compat", "fn": "chat", "fields": {"base_url": "MAGPIE_BASE_URL", "api_key": "MAGPIE_API_KEY", "model": "MAGPIE_DEEPSEEK_MODEL"}},
    ("text.complete", "magpie_deepseek"): {"provider": "magpie", "client": "openai_compat", "fn": "chat", "fields": {"base_url": "MAGPIE_BASE_URL", "api_key": "MAGPIE_API_KEY", "model": "MAGPIE_DEEPSEEK_MODEL"}},
    ("text.complete", "magpie_mimo"):     {"provider": "magpie", "client": "openai_compat", "fn": "chat", "fields": {"base_url": "MAGPIE_BASE_URL", "api_key": "MAGPIE_API_KEY", "model": "MAGPIE_MIMO_MODEL"}},
    ("text.complete", "magpie_glm"):      {"provider": "magpie", "client": "openai_compat", "fn": "chat", "fields": {"base_url": "MAGPIE_BASE_URL", "api_key": "MAGPIE_API_KEY", "model": "MAGPIE_GLM_MODEL"}},
    # GLM-5.3-Flash（模型卡 display_name；id=qoder-cn/gfmodel）= mimo 直连腿的兜底位（config.MIMO_FALLBACK_LANE）。
    # 🔴 只兜【文本与看图】：它不吃视频（39MB 片递进去 Δ=0、自述没收到），所以分镜腿不在这张兜底表里。
    ("text.complete", "magpie_glmflash"):  {"provider": "magpie", "client": "openai_compat", "fn": "chat", "fields": {"base_url": "MAGPIE_BASE_URL", "api_key": "MAGPIE_API_KEY", "model": "MAGPIE_GLMFLASH_MODEL"}},
    # —— 看图 / 看视频（多模态理解）——
    ("video.analyze", "default"):   {"provider": "agnes",  "client": "agnes",              "fn": "analyze_media",       "fields": {"base_url": "AGNES_BASE_URL",   "api_key": "AGNES_API_KEY",   "model": "AGNES_TEXT_MODEL"}},
    ("video.analyze", "mimo"):      {"provider": "mimo",   "client": "mimo",               "fn": "analyze_media",       "fields": {"base_url": "MIMO_BASE_URL",    "api_key": "MIMO_API_KEY",    "model": "TEXT_MODEL_MIMO"}},
    # 2026-10-15 用户定"超级复刻的推理模型全部用 mimo-v2.6-pro" ⇒ 看图腿单开 pro 档
    #（模型名唯一源 = config 的 TEXT_MODEL_MIMO_PRO；flash 那条留着，别的线还在用）。
    ("video.analyze", "mimo_pro"):  {"provider": "mimo",   "client": "mimo",               "fn": "analyze_media",       "fields": {"base_url": "MIMO_BASE_URL",    "api_key": "MIMO_API_KEY",    "model": "TEXT_MODEL_MIMO_PRO"}},
    # 看图（产品图/脸图，只有 image part）2026-10-20 起走 magpie free 腿；主腿 = config 的 vision 表表头。
    # 🔴 这几条【只给图】：带视频时桥会丢 part（2026-10-22 夜实测 qfmodel pt 42→82、dfmodel 57→97，
    #    两条都答"没收到视频"）⇒ 吃视频的分镜腿只能走小米直连（正对照 pt 13→3265、逐秒动作全读出来）。
    #    ⚠️ 原注释说的"视频理解仍走 video.analyze/default（agnes）"已不成立：agnes 2026-10-22 被下架屏蔽。
    ("video.analyze", "magpie"):    {"provider": "magpie", "client": "openai_compat",      "fn": "analyze_media",       "fields": {"base_url": "MAGPIE_BASE_URL", "api_key": "MAGPIE_API_KEY", "model": "MAGPIE_DEEPSEEK_MODEL"}},
    # 点名某一格 magpie 腿（调用方明确点名就不换腿，失败就如实失败 —— 判据在 magpie_lane_specs）
    ("video.analyze", "magpie_qfmodel"): {"provider": "magpie", "client": "openai_compat",   "fn": "analyze_media",       "fields": {"base_url": "MAGPIE_BASE_URL", "api_key": "MAGPIE_API_KEY", "model": "MAGPIE_QFMODEL_MODEL"}},
    ("video.analyze", "magpie_mimo"):    {"provider": "magpie", "client": "openai_compat",   "fn": "analyze_media",       "fields": {"base_url": "MAGPIE_BASE_URL", "api_key": "MAGPIE_API_KEY", "model": "MAGPIE_MIMO_MODEL"}},
    ("video.analyze", "magpie_glmflash"): {"provider": "magpie", "client": "openai_compat",   "fn": "analyze_media",       "fields": {"base_url": "MAGPIE_BASE_URL", "api_key": "MAGPIE_API_KEY", "model": "MAGPIE_GLMFLASH_MODEL"}},

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
    # —— Prime 独立工作台（wan3.youzan666.vip · Wan 3.0 Prime，2026-10-08 新渠道）——
    # 🔴 2026-10-30 用户定【wan3.0 固定换这一家】：`video.generate/wan3-prime` 从 tianyue 换芯到
    #   youzan666.vip 的 **Prime 独立工作台**，模型固定 `wan3.0-video-prime`，不许失败换档（同 10-22 那条规矩）。
    #   契约是当场从它自己的 API 文档抓的（0 消耗只读：GET /v1/models 回参数卡 + 首页 docs 段 + /app.js 示例）：
    #     · 创建 `POST /v1/tasks` + **必须带 Idempotency-Key**（8—100 位 [A-Za-z0-9_-]，相同编号不能改任务内容）
    #     · 查询 `GET /v1/tasks/:id`；主动核对 `POST /v1/tasks/:id/refresh`；目录 `GET /v1/models`
    #     · 素材 `POST /v1/assets?kind=&filename=`（body=裸文件 + Content-Type=mime）⇒ 回 `asset.kind` 与【签名 url】
    #       图片≤20MB / 视频≤100MB / 音频≤15MB；引用时 type 必须等于回执的 asset.kind
    #     · 上限：reference_image ≤10、reference_video ≤5、reference_audio ≤5、media 总 ≤20，数组顺序=引用顺序
    #     · 状态机 queued/submitting/processing/unknown(待核对) → succeeded/failed；成片在 `task.videoUrl`，金额 `task.quotedAmount` ⚡
    #   ⚠️ 与 tianyue 那三条真区别（这次换芯换到了什么）：
    #     ① **平台自己有幂等口** ⇒ "提交后崩溃"不再只能靠盘上的号，重放同一个 key 它明说 replayed、不重复预扣；
    #     ② **素材能直接传它自己的 /v1/assets（视频 100MB）** ⇒ 不再必须先跑 gateway upload 拿公网外链；
    #     ③ **按【秒】计费且有实时单价**（prime 720P=0.15 ⚡/秒、1080P=0.20 ⚡/秒；标准版 0.18）⇒ 价目口径从按次改按秒。
    #   旧那三条注释（/v1/videos/generations、GET /v1/videos/{id}）是 tianyue/xjai 时代的形状，这里已不适用：
    #   现网实测 `GET /v1/videos/generations` 回 404 NOT_FOUND（0 分只读探出来的，不是照抄文档）。
    ("video.generate", "wan3-prime"):   {"provider": "yz30", "client": "gateway._yz30_video_run",
                                         "fn": "POST /v1/assets(素材) → POST /v1/tasks(+Idempotency-Key) → GET /v1/tasks/{id} → 落盘",
                                         "fields": {"base_url": "WAN3_PRIME_YZ_BASE_URL", "api_key": "WAN3_PRIME_YZ_API_KEY",
                                                    "model": "WAN3_PRIME_YZ_MODEL"}},
    # （2026-10-30『tianyue lane 移除』：直出工作台那条 `video.generate/wan30-studio`（tianyue 适配器）
    #   整条拆除 —— 页面/服务/闸一起删，tianyue 在本文件不再有任何活路由。wan3.0 只剩下面这条 yz30。）
    # —— 满血API（manxueapi.com · 2026-10-13 接）三条视频腿，适配器与参数表见 _MANXUE_VIDEO ——
    # 模型名按用户点名的三条：H3 = 1ren-minimax-h3、Seedance 2.0 = 1ren-dance-2-0、Seedance 2.5 = 1ren-dance-2.5-2
    # （"dance" 就是即梦 Seedance：对方文档写着"c-dance/1ren-dance 模型名原样发进来，由 new-api 渠道映射为供应商模型"）。
    # ⚠️ 参数依据不齐：h3 与 2.5-2 有【线上参数卡】；2-0 到今天仍不在 /api/model-capabilities 里，
    #    用的是对方文档那句"客户端内置兜底"⇒ 时长先按 4~15 秒封顶，等线上卡出现再放宽（别拿真提交去试档）。
    # 🔴 2.5-2 的素材上限是 9 图 /【0 视频】/【0 音频】⇒ 这条【挂不了参考视频】。要"参考视频+1080P"的复刻形状
    #    得用 c-dance2.5-pro-1080（30 图/10 视频/10 音频、1.38 积分/秒），那条没接，要接说一句。
    # 没接的：guanfang-seedance-2 / -2-5 / -fast / -mini（价目 price=0、unit=None、无参数卡 = 看着能选、一提交就撞）。
    # 🔴 与 video.generate/h3（RunningHub 导演台）是两条不同上游、两套坏法，故意不合并：
    #    走 manxue 这条不碰 RunningHub 视频禁令（rh_video_guard），也不吃 RH 那把 key 的账。
    ("video.generate", "h3-manxue"):        {"provider": "manxue", "client": "gateway._manxue_video_run",
                                             "fn": "POST /v1/videos → GET /v1/videos/{id} → 落盘",
                                             "fields": {"base_url": "MANXUE_H3_BASE_URL", "api_key": "MANXUE_H3_API_KEY", "model": "MANXUE_H3_MODEL"}},
    ("video.generate", "seedance-2.0-manxue"): {"provider": "manxue", "client": "gateway._manxue_video_run",
                                             "fn": "POST /v1/videos → GET /v1/videos/{id} → 落盘",
                                             "fields": {"base_url": "MANXUE_H3_BASE_URL", "api_key": "MANXUE_H3_API_KEY", "model": "MANXUE_SEEDANCE2_MODEL"}},
    ("video.generate", "seedance-2.5-manxue"): {"provider": "manxue", "client": "gateway._manxue_video_run",
                                             "fn": "POST /v1/videos → GET /v1/videos/{id} → 落盘",
                                             "fields": {"base_url": "MANXUE_H3_BASE_URL", "api_key": "MANXUE_H3_API_KEY", "model": "MANXUE_SEEDANCE25_MODEL"}},
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
# 2026-10-13 同因清掉 dashscope —— qwen38 那条腿被删后，没有任何路由再用这个 provider。
_MODEL_FALLBACK = {"agnes": "agnes-3.0-flash", "mimo": "mimo-v2.6-flash", "zexapi": "gemini-3.5-flash",
                   "nocannobb": "gpt-6.1-sol"}

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
    # grok-4.6：整条 1 分只当报价档，真扣按 token（实测同题波动大，对账看 charged+tokens）
    ("text.complete", "grok46"): 1,
    ("text.complete", "mimo_pro"): 3, ("text.complete", "gemini38"): 6, ("text.complete", "deepseekv4"): 7,
    # magpie 三条 free 腿：上游是 free 池（不校验 key），但**账面照 agnes 的口径记**——
    #   agnes-3.0-flash 也是免费上游、登记 1 分、按 token 结算，这是本仓既有的处理方式。
    # 🔴 不许登记 0 分：settle 走 basis=usage（按能力定），实测一条 290 tok 的调用实扣 3 分；
    #    预估写 0、实扣扣几百 = 报价那一格在说谎（"单价/预计总扣分"是铁律里的硬格）。
    #    一次编译的 prompt 是 1 万 tok 量级 ⇒ 真扣按 token，报价时现读 prices，别拿这条 1 分当上限。
    ("text.complete", "magpie"): 1, ("text.complete", "magpie_deepseek"): 1,
    ("text.complete", "magpie_mimo"): 1, ("text.complete", "magpie_glm"): 1,
    # qoder 池的 GLM-5.3-Flash（= mimo 直连腿的兜底位，2026-10-30 用户点名）：同池同口径，照上面几条记 1。
    ("text.complete", "magpie_glmflash"): 1,
    # ninfer 这条：整条单价只当报价档，真扣按 token（token_rate 10 分/1k）
    ("text.complete", "qwen38-ninfer"): 1,
    ("video.analyze", "default"): 6, ("video.analyze", "mimo"): 6, ("video.analyze", "mimo_pro"): 6,
    ("video.analyze", "magpie"): 6,          # 同 default/mimo 档：看图一次是几千 tok 量级，预估写 0 会在报价上说谎
    ("video.analyze", "magpie_qfmodel"): 6, ("video.analyze", "magpie_mimo"): 6,
    ("video.analyze", "magpie_glmflash"): 6,   # 同上：看图一次几千 tok 量级，预估写 0 会在报价上说谎

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
    # 🔴 2026-10-30 换芯到 youzan666 Prime ⇒ 单位从【分/次】改成【分/秒】（这条平台按秒计费，不是按次）：
    #   上游实时单价（GET /v1/models，0 分只读，单位 = ⚡/秒）：prime 720P=0.15、prime 1080P=0.20、标准版 0.18。
    #   这里登记 4 分/秒 = **占位·未实测**，取的是"和换芯前同量级"：旧档 60 分/条 × 一条 15 秒段 ⇒ 4 分/秒。
    #   两个口径的差（720P 实际比 1080P 便宜 25%）【故意不拆】：价目键只有 (capability,purpose)，
    #   一个键两个数是 10-22 那次撤 `TIANYUE_DURATION_MODE` 时定过不能重犯的形态 ⇒ 统一按高的 1080P 估
    #   （估高只是多留一点批准额度，估低会在第 N 段被自己的闸拦停）。
    #   ⚠️ 报价必须带"占位"两个字；跑完第一条拿 `task.quotedAmount`(⚡) 与渠道账上的真实扣费回来订这个数
    #      （改 model_prices.json 就行，不用改码），同时定死 ⚡→分 的比例。
    ("video.generate", "wan3-prime"): 4,
    # （2026-10-30『tianyue lane 移除』：wan30-studio 的 60 分/次价目随 lane 一起拆。）
    # 满血API（manxue）三条腿 · 2026-10-13 接。上游单位已核实（不再是没有单位）：
    #   /api/pricing 的 model_price 单位 = 【积分】，且与对方公告逐条对上 ——
    #   1ren-dance-2-0   1.8 积分/call（一条封顶 30 秒）   ← 公告原文"sd2.0 的 720 视频 30 秒只要 1.8 积分"
    #   1ren-dance-2.5-2 6   积分/call（同上）             ← 公告原文"sd2.5 的 720 视频 30 秒只要 6 积分"
    #   1ren-minimax-h3  0.002 积分/runtime_second（15 秒 ≈ 0.03 积分，基本不要钱）
    # 下面登记的是【上游积分 1:1 借来记账】：这台机器的"1 元=几分"还没定（见 ADR），所以绝对值别当钱看，
    # 但**相对比例是真的**（2.5 = 2.0 的 3.3 倍、H3 ≈ 免费）——这比三条都填 60 诚实得多。
    # 🔴 跑完第一条要拿对方账上的真实扣费回来订这三个数并改 model_prices.json；在那之前报价必须带"占位"。
    ("video.generate", "h3-manxue"): 1,
    ("video.generate", "seedance-2.0-manxue"): 2, ("video.generate", "seedance-2.5-manxue"): 6,
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
        if purpose in ("motion-rpa", "h3-rpa",
                       "seedance-2.0-manxue", "seedance-2.5-manxue"):
            # 两条浏览器 RPA 腿：上游按【次/段】计，一次任务 = 一段成片，时长不是我方
            # 计费档（RPA 烧上游自己账户的 GPU 秒）。
            # 传 duration 只用于展示/入参，别乘进价格；count>1 = 一次报多条，按条数乘。
            # 🔴 manxue 的两条 dance 也归这一档：/api/pricing 给的是 price_unit=call（1.8 / 6 积分一条，
            #    一条封顶 30 秒）⇒ 与秒数无关，不能按 10 秒档乘（乘了就是把 1.8 积分报成 5.4）。
            est = base * max(1, int((params or {}).get("count") or 1))
        elif purpose == "wan3-prime":
            # 🔴 2026-10-30 换芯：youzan666 的 Prime 是 **billingUnit=second**（参数卡实测，不是猜）
            #    ⇒ base 是【分/秒】，必须乘真实秒数；还按次乘就是把 15 秒段报成 4 分（真数是 60 分）。
            #    段数走 count：一次报 N 段就乘 N（批准条子的段数口径同源）。
            dur = int((params or {}).get("duration") or 0)
            if dur <= 0:
                # 没给秒数不许偷偷按 1 秒估 ⇒ 按价目注释那条上限 30 秒估（宁可往高报）
                dur = 30
            est = base * dur * max(1, int((params or {}).get("count") or 1))
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


def _probe_reachable(base_url: str, api_key: str, proxy: str = ""):
    """生成类端点：探 /models 或 /v1/models 做鉴权，不提交计费任务。
    ⚠️ 别只会拼 /v1/models：RunningHub 这类非 OpenAI 风格网关，/models=200 而 /v1/models=401
       ——之前据此把 wan3/faceswap/motion/detail 全判成"鉴权失败"，是假故障。
    ⚠️ proxy：这台机器对某些域名【直连会被 reset】（tianyue 10-20、image2 10-29 同一病因）。
       探活不走真出图那条路的代理，就会把活渠道报成 unreachable —— 探针必须走被测实际走的那条路。"""
    import time
    import urllib.error
    import urllib.request
    t = time.time()
    base = str(base_url).rstrip("/")
    last = None
    _op = urllib.request.build_opener(
        *[urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if str(proxy or "").strip() else [])
    for path in ("/models", "/v1/models"):
        t0 = time.time()   # 🔴 每条请求【各自】计时：原来 ms 在发请求之前算，于是所有可达腿都显示 0ms，
                           #    面板的"平均延迟"KPI 一直是假的（2026-10-04 接去水印腿时撞见）
        try:
            with _op.open(urllib.request.Request(
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
            ("video.generate", "h3-manxue"), ("video.generate", "seedance-2.0-manxue"),
            ("video.generate", "seedance-2.5-manxue"),
            ("audio.generate", "seed-audio"),
            ("video.edit", "dewatermark"), ("video.edit", "enhance"),
            ("lipsync", "default"),
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
    if r["provider"] == "yz30":
        # 🔴 这条渠道没有 chat 补全口，通用的 `_probe_reachable` 在这儿只会探出一个假"坏"。
        #    用它自己的【只读】口：GET /v1/tasks（列自己的任务，0 分、一条单都不建）。
        #    绝不拿 POST /v1/tasks 探活 —— 那一次就是一条真任务 + 一次真预扣（"别拿真花钱的端点验闸活没活"）。
        import time as _tt
        t0 = _tt.time()
        st, _h, raw = _yz30_http("GET", str(r["base_url"]).rstrip("/") + "/v1/tasks",
                                 headers={"Authorization": "Bearer " + str(r["api_key"]),
                                          "User-Agent": BROWSER_UA}, timeout=25.0)
        ms = int((_tt.time() - t0) * 1000)
        txt = (raw or b"").decode("utf-8", "replace")
        if st == 200:
            try:
                n = len((__import__("json").loads(txt) or {}).get("items") or [])
            except Exception:                                    # noqa: BLE001
                n = "?"
            return {"provider": r["provider"], "model": r["model"] or _YZ30_MODEL,
                    "status": "live-ok", "detail": "GET /v1/tasks 200，名下 %s 条任务（0 分只读）" % n, "ms": ms}
        if st in (401, 403):
            return {"provider": r["provider"], "model": r["model"] or _YZ30_MODEL, "status": "auth-fail",
                    "detail": "HTTP %s：%s（key 好坏只在这一家里看这一行）" % (st, txt[:160]), "ms": ms}
        if st is None:
            return {"provider": r["provider"], "model": r["model"] or _YZ30_MODEL, "status": "unreachable",
                    "detail": txt[:200] or "无应答", "ms": ms}
        return {"provider": r["provider"], "model": r["model"] or _YZ30_MODEL,
                "status": "upstream-5xx" if (st or 0) >= 500 else "bad-request",
                "detail": "HTTP %s：%s" % (st, txt[:180]), "ms": ms}
    if capability in _LIVE_CHAT:
        model = r["model"] or _MODEL_FALLBACK.get(r["provider"])
        if not model:
            return {"provider": r["provider"], "model": "(无)", "status": "no-model",
                    "detail": "route 没配 model、也没有 provider 兜底名；不敢发 null（上游会当 unknown-model 判 400）", "ms": None}
        status, detail, ms = _probe_chat(r["base_url"], r["api_key"], model)
        return {"provider": r["provider"], "model": model, "status": status, "detail": detail, "ms": ms}
    px = str(_get("IMAGE2_PROXY_URL") or "").strip() if r["provider"] == "image2" else ""
    status, detail, ms = _probe_reachable(r["base_url"], r["api_key"], px)
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


# 看图腿的传输上限：长边超过这个数就先缩放再发（2026-10-20 实测定下来的）。
# 现网形状：一张 7,608,526 字节的客户产品图 → base64 ≈10MB 直发 ⇒ magpie 那边 220–420 秒、两图那次直接超时，
# 而 agnes 回的 image_tokens 只有 1014（上游本来就把它压到很小的视觉 token 预算）⇒ **慢是传输，不是模型**。
# 口径照 8099 的实测值（长边 ≤1560，8 张原图 19MB→6.6MB 后识别与融合都够用）；
# 🔴 只缩图，视频一律原样 —— 视频那一份是抽帧事实，动它等于动分镜质量，不在这个闸的范围里。
ANALYZE_IMAGE_MAX_EDGE = 1560
ANALYZE_SHRINK_FROM_BYTES = 200_000      # 小图不折腾：PIL 重编码本身有损


def _shrink_image_bytes(raw: bytes, max_edge: int) -> tuple:
    """返回 (bytes, resized)。缩放失败绝不拦住看图 —— 原样发，并如实记 resized=False。"""
    from io import BytesIO
    try:
        from PIL import Image
        img = Image.open(BytesIO(raw))
        w, h = img.size
        if max(w, h) <= max_edge:
            return raw, False
        img.thumbnail((max_edge, max_edge))
        img = img.convert("RGB")
        out = BytesIO()
        img.save(out, format="JPEG", quality=88, optimize=True)
        nb = out.getvalue()
        return (nb, True) if len(nb) < len(raw) else (raw, False)
    except Exception:                                       # noqa: BLE001
        return raw, False


def analyze_media(path: str, question: str = "用一句话描述这个画面的关键信息。",
                  model: str | None = None, base_url: str | None = None,
                  api_key: str | None = None, max_tokens: int = 300,
                  max_edge: int = ANALYZE_IMAGE_MAX_EDGE,
                  paths: list | None = None) -> dict:
    """看图/看视频 → 返回文字结论（同步，走 openai 兼容 /chat/completions）。

    图片用 image_url、视频用 video_url 的 data URL（agnes-3.0-flash 实测两者都能理解）。
    图片默认按 `max_edge` 长边缩放（0 = 不缩），视频不缩；返回里如实带
    `bytes_before/bytes_after/resized` —— 没有这三格，"这次发出去的是原图还是缩过的"只能猜。

    🔴 `paths`（多图一次请求）是给【抽帧补充腿】开的口子（2026-10-30 用户定"改成这样的流程"）：
       同一条片的 8 帧拆成 8 次调用 = 8 份预估、8 次墙钟（qfmodel 实测一次 36s ⇒ 5 分钟），
       而且模型看不到帧与帧的关系（同一件产品在第 2 帧和第 7 帧长得一不一样，只有同一次请求里比得出来）。
       参考源 D:\\prompt_reverse_8099 就是 8 帧一次递进去读出叠加文字与瓶形的。
       边界：只许【全是图】或【只有一条视频】，混着给直接拒 —— 视频+多图这个形状没有任何一家渠道实测过，
       静默丢掉其中一半比报错坏得多（丢视频是静默的，这条已经栽过一次）。
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
    _video_ext = (".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v")
    _all = [str(p) for p in ([path] + list(paths or [])) if str(p or "").strip()]
    _uniq = []
    for p in _all:
        if p not in _uniq:
            _uniq.append(p)
    bytes_before = bytes_after = 0
    resized_any = False
    parts = []
    for p in _uniq:
        if not _os.path.isfile(p):
            return {"success": False, "text": None, "error": f"文件不存在: {p}"}
        ext = _os.path.splitext(p)[1].lower()
        is_video = ext in _video_ext
        raw = open(p, "rb").read()
        bytes_before += len(raw)
        resized = False
        if not is_video and max_edge and len(raw) > ANALYZE_SHRINK_FROM_BYTES:
            raw, resized = _shrink_image_bytes(raw, int(max_edge))
        # 🔴 mime 必须跟【真发出去的字节】一致：缩放后一定是 JPEG，还挂 image/png 就是拿假类型发真数据
        mime = ("video/mp4" if is_video else
                ("image/jpeg" if resized else (mimetypes.guess_type(p)[0] or "image/jpeg")))
        bytes_after += len(raw)
        resized_any = resized_any or resized
        parts.append({"video": is_video,
                      "url": "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode())})
    if len(parts) > 1 and any(x["video"] for x in parts):
        return {"success": False, "text": None,
                "error": "一次请求里混了视频与多图（%d 个部件）⇒ 拒：这条形状没有任何渠道实测过，"
                         "而【丢掉其中一半是静默的】。视频单独发，抽帧补充腿请全给图。" % len(parts)}
    content = [{"type": "text", "text": question}]
    for x in parts:
        content.append({"type": "video_url", "video_url": {"url": x["url"]}} if x["video"]
                       else {"type": "image_url", "image_url": {"url": x["url"]}})
    payload = {
        "model": model or "agnes-3.0-flash",
        "messages": [{"role": "user", "content": content}],
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
                        "attempts": attempt + 1, "bytes_before": bytes_before,
                        "bytes_after": bytes_after, "resized": resized_any,
                        "media_parts": len(parts)}
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
            "usage": usage, "attempts": attempt + 1, "bytes_before": bytes_before,
            "bytes_after": bytes_after, "resized": resized_any, "media_parts": len(parts)}


def _chat_compat(base_url: str, api_key: str, model: str, messages: list,
                 temperature: float = 0.7, max_tokens: int = 4000, extra: dict | None = None):
    """通用 OpenAI 兼容 /chat/completions 文本调用（agnes/gpt 等皆可）。

    返回 (text, error, usage)：usage 用上游返回值，按它结算真实积分（见 settle_credits）。
    `extra` 只给 magpie 那条腿用（关思考的两个旋钮要按次数递进送）；默认 None ⇒ 其它腿的 payload 一字不变。
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
    if extra:
        payload.update({k: v for k, v in extra.items() if v is not None})
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


# ==== magpie 本地池（127.0.0.1:3425）三条 free 腿的腿序与保护规则（2026-10-20）====
# 接法照 8099 那份（D:\prompt_reverse_8099\backend\services\gpt_client.py:40-95），但**唯一源搬到网关**：
# 腿序、每条试几次、429 的语义、预算下限、关思考的旋钮，全仓只在这几行；调用方读结论不抄表。
MAGPIE_MIN_BUDGET = 1800
# 🔴 预算下限是主保护，不是装饰：实测 max_tokens=60 时 60 个 token 整段进 reasoning、content=None
#   （HTTP 200、usage 正常，看起来像"模型没话说"，其实是预算被思考吃光）。
MAGPIE_NO_THINK = {"reasoning_effort": "low", "thinking": {"type": "disabled"}}
# 两个旋钮都送：实测 deepseek 只认前者、glm 只认后者、mimo 两个都不理（关不干净）⇒ 送一对才覆盖。
# 腿序的**唯一源在 config**（MAGPIE_TEXT_LANES / MAGPIE_VISION_LANES，2026-10-20 用户定主腿=deepseek），
# 这里只负责解析与执行 —— 换主腿不用动码。表里没写的 purpose 一律拒，不猜一条"大概是它"的腿。
MAGPIE_LANE_FIELDS = {"magpie_deepseek": "MAGPIE_DEEPSEEK_MODEL", "magpie_mimo": "MAGPIE_MIMO_MODEL",
                      "magpie_glm": "MAGPIE_GLM_MODEL",
                      # qoder 池那条：`qoder-cn/qfmodel` 的 display_name 就是 Qwen3.8-Flash。
                      # 2026-10-22 夜真产品图四方对照里唯一"结构+OCR+不脑补"三项全对的一条 ⇒ 看图主腿。
                      "magpie_qfmodel": "MAGPIE_QFMODEL_MODEL",
                      # qoder 池的 GLM-5.3-Flash（模型卡 display_name）：mimo 直连腿的兜底位，2026-10-29 用户点名。
                      "magpie_glmflash": "MAGPIE_GLMFLASH_MODEL",
                      # 入口别名 `magpie` = 这张表的表头那条腿（回执/面板显示用；真走的腿以 lane 为准）
                      "magpie": "MAGPIE_DEEPSEEK_MODEL"}



def magpie_lanes(kind: str = "text") -> tuple:
    """这条池的腿序唯一源（现读 config）：返回 ((purpose, attempts), …)。

    config 串形如 `magpie_deepseek:3,magpie_mimo:2,magpie_glm:2`；写错（空、purpose 不在名单、次数不是数）
    ⇒ 返回空表 ⇒ 调用方拒着不发请求，而不是悄悄用默认表跑出一条"没人指定过"的腿。
    """
    raw = str(getattr(settings, "MAGPIE_VISION_LANES" if str(kind) == "vision"
                      else "MAGPIE_TEXT_LANES", "") or "")
    out = []
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        pur, _, n = piece.partition(":")
        pur = pur.strip()
        if pur not in MAGPIE_LANE_FIELDS:
            return ()                                    # 名字认错 ⇒ 整张表作废（半张表更危险）
        try:
            out.append((pur, max(1, int(n or 1))))
        except ValueError:
            return ()
    return tuple(out)


def magpie_lane_kind(messages) -> str:
    """按【真发出去的 part】判该走哪张腿表 —— 不看调用方嘴上说什么。"""
    try:
        for m in (messages or []):
            cont = (m or {}).get("content")
            if isinstance(cont, list) and any(isinstance(p, dict) and p.get("type") in ("image_url", "image", "input_image")
                                              for p in cont):
                return "vision"
    except Exception:                                          # noqa: BLE001
        pass
    return "text"


def magpie_lane_specs(purpose: str = "magpie", kind: str = "text") -> list:
    """把腿序表变成【可走的腿】：每条带 purpose/model/retries，模型名现读 config 那三个字段。

    · purpose="magpie"（入口腿）⇒ 走整张表；
    · purpose=magpie_deepseek / magpie_glm ⇒ 只钉那一条（调用方明确点名就不换腿，失败就如实失败）；
    · 🔴 点名的腿不在这张表里 ⇒ 返回空表 + 由调用方出声（glm 带多图实测拿不到合法 JSON，
      所以它根本不在 vision 表里 —— 悄悄塞进去等于把 8099 踩过的坑再踩一遍）。
    """
    table = magpie_lanes(kind)
    p = str(purpose or "magpie")
    if p in ("magpie", ""):
        picked = list(table)
    else:
        picked = [(pp, n) for pp, n in table if pp == p]
        if not picked:
            return []
    out = []
    for pp, n in picked:
        model = str(getattr(settings, MAGPIE_LANE_FIELDS.get(pp, ""), "") or "").strip()
        if model:
            out.append({"purpose": pp, "model": model, "retries": int(n)})
    return out


def magpie_429_is_transient(err: str) -> bool:
    """它的 429 = 上游端点间歇不可用（实测隔 6-20 秒重打就通），**不是额度** ⇒ 同一条腿退避，不换腿。
    按关键词判死会把好腿跳掉，所以这条判据单独一份。"""
    return "HTTP 429" in str(err or "")


def magpie_failure_text(errs: list) -> str:
    """逐条腿的拒因拼成人话 —— 这条函数存在的理由是我今天写过一版 `" | ".join(errs)[-3:]`：
    那取的是【最后 3 个字符】，于是唯一有用的信息（上游按出口 IP 拒绝）被截成 `magpie 三条 free 腿全部失败：ava`，
    看的人只会以为是模型坏了 —— 定性盲区就是故障本身（同"5xx 分支丢上游返回体"那一条）。"""
    joined = " ｜ ".join(str(e)[:150] for e in (errs or [])) or "一条都没试"
    if any(("current network" in str(e)) or ("abusive traffic" in str(e)) for e in (errs or [])):
        return ("magpie 上的 opencode-zen 免费档被上游按【出口 IP】拒绝（403 abusive traffic，"
                "它自己写明与账号无关）⇒ 不是模型坏了、也不是这条线接错；换出口/换池子才会通。逐条拒因：%s" % joined)
    return "magpie 各腿全部失败：%s" % joined


def _magpie_chat(base_url: str, api_key: str, lanes: list, messages: list,
                 temperature: float = 0.7, max_tokens: int = 4000, retries: int = 3,
                 sleep: float = 8.0, _call=None):
    """按腿序走：每条腿试 retries 次（429 退避重试），非 429 的失败才换下一条腿。

    第 2 次起带上关思考的两个旋钮（8099 实测有效的那对）。返回 (text, error, usage, lane)。
    `_call`/`sleep` 是给回归闸换桩用的缝 —— 判据本身是纯的，闸不该真发请求、也不该真等 8 秒。
    """
    import time as _t
    call = _call or _chat_compat
    budget = max(int(max_tokens or 0), MAGPIE_MIN_BUDGET)      # 预算下限：夹上去，不是拒
    errs = []
    for lane in (lanes or []):
        model = lane.get("model") or ""
        for attempt in range(1, int(lane.get("retries") or retries) + 1):
            extra = dict(MAGPIE_NO_THINK) if attempt > 1 else None
            text, err, usage = call(base_url, api_key, model, messages,
                                   temperature=temperature, max_tokens=budget, extra=extra)
            if text:
                return text, None, usage, {"purpose": lane.get("purpose"), "model": model,
                                           "attempt": attempt, "attempts_all": len(lanes)}
            # 🔴 这里**不截断**：诊断关键词（abusive traffic / current network）出现在原文 140 字之后，
            #    早先版本在这行就 [:90] ⇒ 拼文案那层永远命中不到，人只看到"各腿全部失败"（同一个 bug 的第二层）。
            #    截断只放在展示层 `magpie_failure_text`，判据读的是完整串。
            errs.append("%s#%s %s" % (lane.get("purpose"), attempt, err))
            if magpie_429_is_transient(err):
                _t.sleep(sleep)                                 # 同一条腿再打，不换腿
                continue
            break                                               # 不是抖动 ⇒ 换下一条腿
    return None, magpie_failure_text(errs), {}, {}


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


# ═══ wan3.0 共用件（2026-10-30『tianyue lane 移除』后只剩这些；wan3.0 出片腿本体 = _yz30_* 那节）═══
# · _wan30_http：出网口（跨域重定向剥凭据）。_wan30_download（yz30 与 manxue 的落盘腿共用）走它。
# · _wan30_note_job：任务号【当场】落盘（wan3_job.json），provider 由调用点显式给。
# · _wan30_download：下载成片，同名不静默覆盖（旧的留 .prev-<sha8>）、<1KB 判失败。
def _wan30_http(method: str, url: str, headers: dict | None = None, body: bytes | None = None,
                timeout: float = 60.0):
    """出网口（照 manxue 那条的纪律）：跨域重定向剥凭据，返回 (status, resp_headers, body)。

    🔴 重定向【自己跟】，不许 urllib 内置的那个跟：它会把 Authorization 原样带到新主机
    （实测把 Bearer 递到了别的 host），下面那个跨主机剥凭据的分支也就永远走不到。"""
    import urllib.error
    import urllib.parse
    import urllib.request

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None                      # 交回给我们自己跟（好在跨主机时剥凭据）

    hdrs = dict(headers or {})
    handlers: list = [_NoRedirect]
    opener = urllib.request.build_opener(*handlers)
    cur, hops = url, 0
    while True:
        req = urllib.request.Request(cur, data=body, headers=hdrs, method=method)
        try:
            with opener.open(req, timeout=timeout) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                loc = e.headers.get("Location") or ""
                hops += 1
                if not loc or hops > 4:
                    return e.code, dict(e.headers), e.read()
                nxt = urllib.parse.urljoin(cur, loc)
                if urllib.parse.urlsplit(nxt).netloc != urllib.parse.urlsplit(cur).netloc:
                    for k in list(hdrs):
                        if k.lower() in ("authorization", "x-api-key"):
                            hdrs.pop(k, None)          # 跨主机不递凭据
                cur = nxt
                continue
            return e.code, dict(e.headers), e.read()
        except Exception as e:                           # noqa: BLE001
            return None, {}, ("%s: %s" % (type(e).__name__, str(e)[:160])).encode("utf-8", "replace")


def _wan30_note_job(params: dict, tid: str, model: str = "", lane: str = "",
                    provider: str = "") -> str:
    """任务号【当场】落盘，就在成片旁边（wan3_job.json）。号不落盘，进程一死就只能重投 = 双花。

    🔴 `provider` 必须由调用点【显式】给，这里不再写死 "tianyue"：2026-10-30 换芯那天我复用这个函数，
       要是还留着那个字面量，`wan3_job.json` 就会写着 tianyue 而实际打的是 youzan666 ——
       捞回的人照那份号去找错的家（"回执写 A、实跑 B"这一类，这里第三次犯）。
       拿不到身份就如实写"没记"，不许猜一个看起来合理的填上。
    """
    import json
    import time
    out = str((params or {}).get("output_path") or "")
    p = _os.path.join(_os.path.dirname(_os.path.abspath(out)), "wan3_job.json") if out else ""
    if not p:
        return ""
    try:
        _os.makedirs(_os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"kind": "wan3_job", "task_id": tid, "submitted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                       "lane": lane or "video.generate/wan3-prime",
                       "provider": str(provider or "").strip() or "没记", "model": model,
                       "resolution": str((params or {}).get("resolution") or ""), "aspect_ratio": str((params or {}).get("ratio") or ""),
                       "duration": (params or {}).get("duration"), "status": "queued",
                       "note": "号一拿到就落盘；捞回只给 job_id（只 GET，不重投）"}, f, ensure_ascii=False, indent=1)
    except OSError:
        return ""
    return p


def _wan30_download(url: str, dest: str, base_url: str, api_key: str) -> tuple:
    """下载成片到 dest，返回 (path|None, nbytes, err)。被 yz30 与 manxue 两条视频腿共用。

    纪律（原 yz-prime 那条的，2026-10-16 换芯时保留）：首探不带凭据（公开直链不该要 Authorization），
    只有同主机才补一次 Bearer；同名成片【不许静默覆盖】（旧的留 .prev-<sha8>）；<1KB 判失败。"""
    import hashlib
    dest = str(dest or "")
    if not url or not dest:
        return None, 0, "没给直链或落盘路径"
    st, _, raw = _wan30_http("GET", url, headers={"User-Agent": "Mozilla/5.0"}, timeout=600.0)
    if (st is None or not raw) and base_url and url.startswith(str(base_url).rstrip("/")):
        st, _, raw = _wan30_http("GET", url, headers={"Authorization": "Bearer " + str(api_key or ""),
                                                      "User-Agent": "Mozilla/5.0"}, timeout=600.0)
    if not raw:
        return None, 0, "取不下来（HTTP %s）" % st
    n = len(raw)
    if n < 1024:
        return None, n, "只有 %d 字节，不像视频（HTTP %s）" % (n, st)
    try:
        _os.makedirs(_os.path.dirname(_os.path.abspath(dest)) or ".", exist_ok=True)
        if _os.path.isfile(dest):                        # 同名不静默覆盖：旧的留 .prev-<sha8>
            with open(dest, "rb") as f:
                old_sha = hashlib.sha256(f.read()).hexdigest()[:8]
            if old_sha != hashlib.sha256(raw).hexdigest()[:8]:
                _os.replace(dest, dest + ".prev-" + old_sha)
        with open(dest, "wb") as f:
            f.write(raw)
    except OSError as e:
        return None, n, "落盘失败：%s" % e
    return dest, n, ""


# ==== 满血API（manxueapi.com）视频腿 · 2026-10-13 接：H3 + Seedance 2.0 / 2.5 ====
# 契约（对方 Apifox 文档 + 0 消耗只读探测核对过）：
#   提交 POST /v1/videos {model,prompt,ratio,duration,resolution,images[],videos[],audios[]} → 任务号 id（兼容 task_id）
#   轮询 GET /v1/videos/{task_id}（建议 5s）：queued / in_progress → completed / failed，失败读 error.message
#   取直链优先级（对方文档明给的顺序）video.url > video_url > output.url > output[0].url > result.url
#     > result_url > metadata.url > 顶层 url；兜底 GET /v1/videos/{id}/content（new-api 本地路由，可匿名、透传 Range）
# 三条红线沿用 prime 那条的形状：自带 job_id = 只捞不重投、素材只吃 http(s) 外链（本地路径在【本地】就拒，
# 0 分 0 请求）、下载首探不带凭据（只有同主机才补一次 Bearer）。
# 🔴 参数唯一源 = GET /api/model-capabilities 2026-10-13 的线上快照（原样存在 C:\dsh\scratch\manxue-probe.json），
#   不是文档那份 49 条目录 —— 对方自己写着"目录列出不代表每个 Key 均有调用权限"。
#   文档标"插件兜底"的那几条（1ren-dance-2-0 / -2-2 / 2.5-2~-4 / c-dance2.5-pro-720 / guanfang-seedance-*）
#   不进这张表：那些时长与分辨率不是服务端承诺，接进来等于把"能不能跑"推给一次真花钱的网络往返。
_MANXUE_VIDEO = {
    "1ren-minimax-h3": {
        # 线上参数卡（有）：5~15 秒、720p/480p、9 图/3 视频/3 音频
        "durations": tuple(range(5, 16)), "resolutions": ("720p", "480p"),
        "ratios": ("1:1", "16:9", "9:16"), "media": (9, 3, 3),
        "modes": ("referenceImage",),
        "default": (10, "16:9", "720p")},
    "1ren-dance-2-0": {
        # ⚠️ 没有服务端参数卡（2026-10-13 现读 /api/model-capabilities 仍不列这条）⇒ 下面这份是对方
        #    文档里【客户端内置兜底】那一档（4~15 秒 / 720p / 9-3-3），不是服务端承诺。
        #    价格按公告与 /api/pricing 双向对上：1.8 积分/次（一次封顶 30 秒）⇒ 计价走 meter() 的按次例外。
        #    公告说 30 秒也行，但兜底表只到 15 秒、线上卡没有 ⇒ 先按 15 秒封顶，越界在【本地】拒（0 分 0 请求）；
        #    等 /api/model-capabilities 出现这条再放宽，别拿真提交去试探对方的档。
        "durations": tuple(range(4, 16)), "resolutions": ("720p",),
        "ratios": ("16:9", "9:16", "1:1"), "media": (9, 3, 3),
        "modes": ("textToVideo", "referenceImage"),
        "default": (10, "16:9", "720p"), "params_source": "客户端兜底"},
    "1ren-dance-2.5-2": {
        # 🔴 线上参数卡（2026-10-13 已恢复并列出）：时长只有离散四档 5/10/15/30、分辨率只有 720p，
        #    素材上限 = 9 图 /【0 视频】/【0 音频】⇒ 这条挂不了参考视频（复刻类要挂参考视频得用
        #    c-dance2.5-pro-1080：30 图/10 视频/10 音频、1080p、按秒 1.38 积分）。
        "durations": (5, 10, 15, 30), "resolutions": ("720p",),
        "ratios": ("1:1", "16:9", "9:16"), "media": (9, 0, 0),
        "modes": ("textToVideo", "referenceImage", "firstFrame", "firstAndTailFrame"),
        "default": (15, "16:9", "720p"), "params_source": "线上参数卡"},
}
_MANXUE_VIDEO_PURPOSES = ("h3-manxue", "seedance-2.0-manxue", "seedance-2.5-manxue")
# 别的腿常用的别名（video.generate 那几族都这么写）⇒ 只在这一个口收一次，agent 不用记两家方言
_MANXUE_MEDIA_ALIAS = {"images": "image_urls", "videos": "video_urls", "audios": "audio_urls"}
_MANXUE_OK = ("completed", "success", "succeeded", "done", "finished")
_MANXUE_BAD = ("failed", "error", "cancelled", "canceled", "rejected", "expired", "timeout")
# 对方文档给的取链顺序，逐条写成路径元组（拿 _dig 猜会猜错优先级，把封面图当成片交付出去）
_MANXUE_URL_PATHS = (("video", "url"), ("video_url",), ("output", "url"), ("result", "url"),
                     ("result_url",), ("metadata", "url"), ("url",))


# ═══════════════════════════════════════════════════════════════════════════
# youzan666 Prime —— `video.generate/wan3-prime`（2026-10-30 用户定"wan3.0固定换"）
#
# 这条腿的契约不是照抄文档，是 0 分只读探出来 + 从它自己的 /app.js 示例里读的：
#   · 创建 `POST /v1/tasks`，【必须】带 `Idempotency-Key`（8—100 位 字母/数字/_/-，相同编号不能改任务内容）
#   · 查询 `GET /v1/tasks/:id`；主动核对 `POST /v1/tasks/:id/refresh`；目录 `GET /v1/models`
#   · 素材 `POST /v1/assets?kind=&filename=`（body=裸文件，Content-Type=mime）⇒ 回 `asset.kind` + 签名 `url`
#   · 状态机 queued/submitting/processing/unknown(待核对) → succeeded/failed；成片 `task.videoUrl`，金额 `task.quotedAmount` ⚡
#   · 上限 duration 2—30【真实秒】、prime 只出 720P/1080P、media 图≤10 视频≤5 音频≤5 总≤20（数组顺序=引用顺序）
#   · 计费 = 按秒（参数卡 billingUnit=second）：prime 720P 0.15 ⚡/秒、1080P 0.20 ⚡/秒
#
# 🔴 这次换芯真正换到什么（比 tianyue 那条多的三样，都是老事故的解药）：
#   ① **平台侧幂等**：提交后进程死了/结果未知 ⇒ 用同一个 key 重发，它回 `replayed` 且不重复预扣。
#      tianyue 那条只能"只捞不重投"是因为【没有】这个东西；现在有了，但上层纪律照旧（先盘上的号），
#      幂等是兜底、不是许可证 —— 否则"记不住号就重投"这个习惯会被养回来。
#   ② **素材走它自己的 /v1/assets，视频上限 100MB** ⇒ 不再必须先 `gateway upload` 拿公网外链
#      （那条 29MB 的原片在这家能直接传）。
#   ③ **有实时价目口**（GET /v1/models 带 ⚡/秒）⇒ 报价的单价有出处；`GET /v1/tasks/<乱号>` 回 404
#      TASK_NOT_FOUND = 一个 0 分判别口（探"这条腿活没活"不必花钱）。
# ═══════════════════════════════════════════════════════════════════════════

_YZ30_MODEL = "wan3.0-video-prime"
_YZ30_RATIOS = ("adaptive", "16:9", "9:16", "1:1", "4:3", "3:4", "21:9")
_YZ30_RESOLUTIONS = ("720P", "1080P")          # prime 参数卡只这两档（480P 是标准版的）
_YZ30_DUR_MIN, _YZ30_DUR_MAX = 2, 30
_YZ30_MEDIA_LIMIT = {"reference_image": 10, "reference_video": 5, "reference_audio": 5}
_YZ30_MEDIA_TOTAL = 20
_YZ30_ASSET_MAX_BYTES = {"reference_image": 20 * 1024 * 1024,
                         "reference_video": 100 * 1024 * 1024,
                         "reference_audio": 15 * 1024 * 1024}
_YZ30_EXT_KIND = {".png": "reference_image", ".jpg": "reference_image", ".jpeg": "reference_image",
                  ".webp": "reference_image", ".mp4": "reference_video", ".mov": "reference_video",
                  ".wav": "reference_audio", ".mp3": "reference_audio"}
_YZ30_ACTIVE = ("queued", "submitting", "processing", "unknown")
_YZ30_OK = ("succeeded",)
_YZ30_BAD = ("failed",)


def lane_bills_per_second(capability: str, purpose: str) -> bool:
    """这条 lane 是按【秒】计价还是按【条/段】计价 ⇒ 唯一判据 = 路由的 provider（不靠模型名尾巴猜）。

    🔴 为什么换芯必须带上这一条：`unit_price` 存的一直是【分/段】，`count_from_args` 与报价正文都按
       "段数 × 单价" 算。2026-10-30 把 wan3-prime 换到 youzan666（billingUnit=second、4 分/秒）之后，
       还按段乘 ⇒ 一条 40 秒三段片批 12 分，而真要花 160 分 ⇒【批的比花的少】，
       第 2 段会被自己的批准闸拦停（AGENTS.md 里"填少了会在第 2 段被自己的闸拦停"那一格，只是这次不是手滑，是口径错）。
       所以"按秒"这件事必须有主人：单价是分/秒，报价写分/秒，条子上限按 Σ秒 × 分/秒 算。
    """
    prov = str((ROUTES.get((capability, purpose)) or {}).get("provider") or "")
    return prov == "yz30"


def lane_price_unit_label(capability: str, purpose: str) -> str:
    """给报价/价目行用的单位词 —— 只有一处定义，别处不许再拼"分/段"。"""
    return "分/秒" if lane_bills_per_second(capability, purpose) else "分/段"


def lane_resolution_choices(capability: str, purpose: str) -> tuple:
    """这条 lane 认哪些分辨率 ⇒ 按【路由的 provider】取那一份唯一源，给调用方做本地预检。

    🔴 为什么不发一份合并表：合并 = 两条渠道的参数都能过本地预检，出事查不到是谁放行的
       （2026-10-30 换芯时 `wan3_client._prime_resolution` 还写死读 `gateway._WAN30_RESOLUTIONS`，
        那是 tianyue 那一条的名单 —— 名字里带 prime、身体守的是别家，正是这一类）。
    取不到 ⇒ 回空元组，调用方必须【按拒处理】，不许退回一份自造的字面量名单。
    """
    prov = str((ROUTES.get((capability, purpose)) or {}).get("provider") or "")
    if prov == "yz30":
        return _YZ30_RESOLUTIONS
    return ()


def lane_ratio_choices(capability: str, purpose: str) -> tuple:
    """同上，画幅名单。yz30 那七档是从它自己首页表单的 option 里读的（含 21:9）。"""
    prov = str((ROUTES.get((capability, purpose)) or {}).get("provider") or "")
    if prov == "yz30":
        return _YZ30_RATIOS
    return ()


def _yz30_http(method: str, url: str, headers: dict | None = None, body: bytes | None = None,
               timeout: float = 60.0):
    """这一家的唯一出网口（探活与回归都打这个缝）。返回 (status, headers, body_bytes)。

    🔴 不挂 TIANYUE_PROXY_URL：那台本机代理是给 api.tianyue.xyz 被 reset 那条腿配的，
       别的渠道默认直连（要挂就在这家自己的键上配 `YZ30_PROXY_URL`，别共享一个开关）。
       跨域重定向【剥掉 Authorization 再跟】（_wan30_http 那条同纪律：内置重定向会把 Bearer 递到别的 host）。
    """
    import urllib.error
    import urllib.parse
    import urllib.request

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    px = str(_get("YZ30_PROXY_URL") or "").strip()
    handlers: list = [_NoRedirect]
    if px:
        handlers.append(urllib.request.ProxyHandler({"http": px, "https": px}))
    op = urllib.request.build_opener(*handlers)
    cur = str(url)
    hdrs = dict(headers or {})
    m = str(method).upper()
    data = body
    for _hop in range(4):
        try:
            with op.open(urllib.request.Request(cur, data=data, method=m, headers=hdrs),
                         timeout=timeout) as r:
                return r.status, dict(r.headers or {}), r.read()
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                loc = (e.headers or {}).get("Location") or ""
                if not loc:
                    return e.code, dict(e.headers or {}), (e.read() or b"")
                nxt = urllib.parse.urljoin(cur, loc)
                if urllib.parse.urlsplit(nxt).netloc != urllib.parse.urlsplit(cur).netloc:
                    hdrs = {k: v for k, v in hdrs.items() if str(k).lower() != "authorization"}
                if e.code == 303 and m != "GET":
                    m, data = "GET", None
                cur, e_body = nxt, (e.read() if hasattr(e, "read") else b"")
                continue
            try:
                raw = e.read() or b""
            except Exception:                                    # noqa: BLE001
                raw = b""
            return e.code, dict(e.headers or {}), raw
        except Exception as e:                                   # noqa: BLE001
            return None, {}, ("%s: %s" % (type(e).__name__, str(e)[:200])).encode("utf-8")
    return None, {}, b"redirects too many times"


def yz30_payload_problems(params: dict | None, media_known: bool = False) -> list:
    """【本地】入参判据（纯函数，0 分、不发请求）⇒ 闸能整块抠出来跑值表。

    `media_known=False` 时只判形状（URL 还没解析成本地/外链）；True 时连"这一条到底能不能送"一起判。
    """
    p = params or {}
    bad = []
    if str(p.get("job_id") or "").strip():
        return bad                                   # 只捞不重投：号都给了就不再判提交参数
    if not str(p.get("prompt") or "").strip():
        bad.append("要给 prompt（编译器写出的那一段）")
    out = str(p.get("output_path") or "").strip()
    if not out:
        bad.append("要给 output_path（成片要落到盘上、能出示才算交付）")
    try:
        dur = int(float(p.get("duration")))
    except (TypeError, ValueError):
        dur = 0
        bad.append("duration 要 2—30 的整数秒（收到 %r）—— 这家按秒计费，秒数是价格的一部分" % (p.get("duration"),))
    if dur and not (_YZ30_DUR_MIN <= dur <= _YZ30_DUR_MAX):
        bad.append("duration=%d 超出这条卡片的 %d—%d 秒（GET /v1/models 的 durations 实测）"
                   % (dur, _YZ30_DUR_MIN, _YZ30_DUR_MAX))
    res = str(p.get("resolution") or "1080P").strip().upper().replace(" ", "")
    if res not in _YZ30_RESOLUTIONS:
        bad.append("resolution 只认 %s（收到 %r）⇒ prime 参数卡只有这两档，480P 是标准版的"
                   % ("/".join(_YZ30_RESOLUTIONS), p.get("resolution")))
    ratio = str(p.get("ratio") or p.get("aspect_ratio") or "adaptive").strip()
    if ratio not in _YZ30_RATIOS:
        bad.append("ratio 只认 %s（收到 %r）" % ("/".join(_YZ30_RATIOS), ratio))
    counts = {"reference_image": len(p.get("image_urls") or []),
              "reference_video": len(p.get("video_urls") or []),
              "reference_audio": len(p.get("audio_urls") or [])}
    for k, n in counts.items():
        if n > _YZ30_MEDIA_LIMIT[k]:
            bad.append("%s 最多 %d 项（给了 %d）" % (k, _YZ30_MEDIA_LIMIT[k], n))
    tot = sum(counts.values())
    if tot > _YZ30_MEDIA_TOTAL:
        bad.append("media 总计最多 %d 项（给了 %d）" % (_YZ30_MEDIA_TOTAL, tot))
    if not tot and not media_known:
        bad.append("一条参考素材都没有 ⇒ 拒：这条 lane 是【全能参考】复刻，没有素材就等于让模型凭空编产品")
    return bad


def _yz30_idem_key(params: dict | None) -> str:
    """幂等编号：同一次提交（同一个落点 + 同一份内容）永远算出同一个 key。

    🔴 为什么由【我们】派生而不是每次随机：平台的语义是"相同编号不能修改任务内容"，
       所以只要参数没变，重发就是恢复原任务（它回 replayed、不重复预扣）；
       参数变了 key 自然变 ⇒ 不会出现"拿旧编号提交新内容"被平台拒。
       随机 uuid（它自家前端那种 `web_<uuid>`）只防同一次点击双击，防不了"进程死了重跑一遍"。
    """
    import hashlib
    import json as _json
    p = params or {}
    parts = [str(p.get("output_path") or ""), str(p.get("prompt") or ""),
             str(p.get("duration") or ""), str(p.get("resolution") or ""),
             str(p.get("ratio") or p.get("aspect_ratio") or ""),
             _json.dumps([p.get("image_urls") or [], p.get("video_urls") or [],
                          p.get("audio_urls") or []], ensure_ascii=False, sort_keys=True)]
    return "sc-" + hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:40]


def _yz30_media_item(kind: str, ref: str, base: str, api_key: str) -> tuple:
    """一条素材 ⇒ ({"type","url"} | None, 说明, err)。本地文件走 /v1/assets，外链原样递。"""
    import os as _os
    import urllib.parse as _up
    u = str(ref or "").strip()
    if not u:
        return None, "空", None
    if u.lower().startswith(("http://", "https://")):
        return {"type": kind, "url": u}, "外链原样引用（平台自己去拉）", None
    if not _os.path.isfile(u):
        return None, "", "素材既不是外链也不在盘上：%s" % u[:80]
    size = _os.path.getsize(u)
    if size > _YZ30_ASSET_MAX_BYTES[kind]:
        return None, "", "本地素材 %s 是 %d MB，超过这一类在平台的上传上限 %d MB" % (
            _os.path.basename(u), size // 1024 // 1024, _YZ30_ASSET_MAX_BYTES[kind] // 1024 // 1024)
    mime = _guess_mime_kind(u, kind)[1]
    q = _up.urlencode({"kind": kind, "filename": _os.path.basename(u)})
    st, _h, raw = _yz30_http("POST", "%s/v1/assets?%s" % (base.rstrip("/"), q),
                             headers={"Authorization": "Bearer " + api_key,
                                      "Content-Type": mime, "User-Agent": BROWSER_UA},
                             body=open(u, "rb").read(), timeout=600.0)
    try:
        j = __import__("json").loads((raw or b"").decode("utf-8", "replace"))
    except Exception:                                            # noqa: BLE001
        return None, "", "上传回执不是 JSON（HTTP %s）：%s" % (st, (raw or b"")[:160].decode("utf-8", "replace"))
    if st is None or st >= 400 or not (j.get("ok") is True or j.get("asset") or j.get("url")):
        err = ((j.get("error") or {}).get("message") if isinstance(j.get("error"), dict) else None) or j.get("message")
        return None, "", "上传失败（HTTP %s）：%s" % (st, str(err or (raw or b"")[:200].decode("utf-8", "replace")))
    asset = j.get("asset") or {}
    url = str(j.get("url") or asset.get("url") or "").strip()
    got_kind = str(asset.get("kind") or "").strip()
    if got_kind and got_kind != kind:
        # 🔴 平台文档明写"type 必须等于上传回执 asset.kind"，回执不一致就是它会核对失败 ⇒ 当场停，别硬送
        return None, "", "平台回执的素材类型是 %r，与我们要引用的 %r 不一致 ⇒ 停（文档：页面会核对素材 ID 与实际类型）" % (got_kind, kind)
    if not url:
        return None, "", "上传回里没有签名 url（HTTP %s）：%s" % (st, str(j)[:200])
    return {"type": kind, "url": url}, "已传平台 /v1/assets（%s，%d 字节）" % (mime, size), None


def _guess_mime_kind(path: str, want_kind: str) -> tuple:
    """按扩展名定 (kind, mime)；认不出就退回"要的那一类"的通用 mime，并让上传去接受平台裁决。"""
    import mimetypes
    import os as _os
    ext = _os.path.splitext(str(path))[1].lower()
    kind = _YZ30_EXT_KIND.get(ext) or want_kind
    mime = mimetypes.guess_type(str(path))[0] or {
        "reference_image": "image/png", "reference_video": "video/mp4",
        "reference_audio": "audio/wav"}.get(kind, "application/octet-stream")
    return kind, mime


def yz30_has_video_stream(path: str) -> tuple:
    """🔴 "到手"的判据 = ffprobe 读得出【视频流】，不是状态码 200、也不是 completed。
       （2026-10-21 现网那条：status=completed、直链 200、Content-Type video/mp4，内容是 457 字节的 S3 NoSuchKey。）
    """
    import json as _json
    import subprocess
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=codec_type,width,height:format=duration",
                            "-of", "json", str(path)],
                           capture_output=True, text=True, encoding="utf-8", timeout=60)
    except Exception as e:                                       # noqa: BLE001
        return False, "ffprobe 没跑起来（%s）⇒ 不能判这条片到手" % str(e)[:120]
    try:
        j = _json.loads(r.stdout or "{}")
    except Exception:                                            # noqa: BLE001
        return False, "ffprobe 输出读不出：%s" % (r.stdout or r.stderr or "")[:160]
    st = (j.get("streams") or [{}])[0]
    if st.get("codec_type") != "video":
        return False, "文件里没有视频流（ffprobe 看到 %r）" % (st.get("codec_type") or "空")
    dur = None
    try:
        dur = float((j.get("format") or {}).get("duration") or 0) or None
    except (TypeError, ValueError):
        dur = None
    return True, "%sx%s %s codec=%s 时长=%s" % (st.get("width"), st.get("height"),
                                                st.get("codec_name"), st.get("codec_type"),
                                                ("%.2fs" % dur) if dur else "读不到")


def _yz30_get_task(tid: str, base: str, api_key: str, refresh: bool = False) -> tuple:
    """查一条任务（只读）。refresh=True 走 POST /v1/tasks/:id/refresh（主动核对原线路）。"""
    import json as _json
    path = "/v1/tasks/%s" % _urllib_quote(str(tid or ""))
    if refresh:
        path += "/refresh"
    st, _h, raw = _yz30_http("POST" if refresh else "GET", base.rstrip("/") + path,
                             headers={"Authorization": "Bearer " + api_key,
                                      "Content-Type": "application/json",
                                      "User-Agent": BROWSER_UA},
                             body=b"{}" if refresh else None, timeout=120.0)
    try:
        j = _json.loads((raw or b"").decode("utf-8", "replace"))
    except Exception:                                            # noqa: BLE001
        return None, {}, "查询回执不是 JSON（HTTP %s）：%s" % (st, (raw or b"")[:180].decode("utf-8", "replace"))
    if st is None or st >= 400:
        err = (j.get("error") or {}) if isinstance(j.get("error"), dict) else {}
        return None, j, "查询 HTTP %s：%s %s" % (st, err.get("code") or "", str(err.get("message") or j)[:200])
    return (j.get("task") or j.get("data") or j), j, None


def _urllib_quote(s: str) -> str:
    import urllib.parse
    return urllib.parse.quote(s, safe="")


def _yz30_video_run(params: dict, route: dict | None = None) -> dict:
    """youzan666 的 wan3.0 Prime 出片腿。**单次尝试 + 平台幂等兜底**：
      · 提交前参数全在本地判（0 分不发）；
      · 号一拿到就落盘（`wan3_job.json`，复用 `_wan30_note_job` 那一处）；
      · 结果未知 ⇒ 【只捞不重投】—— 即使平台有幂等，也不拿重发当恢复手段（那会把"记不住号就重投"养成习惯）；
      · 到手判据 = ffprobe 读得出视频流，不是 completed。
    """
    import json as _json
    import time as _t
    base = str(params.get("base_url") or (route or {}).get("base_url")
               or _get("WAN3_PRIME_YZ_BASE_URL") or "").rstrip("/")
    api_key = str(params.get("api_key") or (route or {}).get("api_key")
                  or _get("WAN3_PRIME_YZ_API_KEY") or "").strip()
    model = str((route or {}).get("model") or _get("WAN3_PRIME_YZ_MODEL") or _YZ30_MODEL).strip()
    lane = "yz30/%s" % ((route or {}).get("purpose") or "wan3-prime")
    if not (base and api_key):
        return {"success": False, "kind": "submit_param",
                "error": "wan3-prime(yz30)：config/.env 缺 WAN3_PRIME_YZ_BASE_URL 或 WAN3_PRIME_YZ_API_KEY"}
    # 🔴 固定一条模型（用户 2026-10-30"wan3.0固定换"+2026-10-22"不许失败换档"）：
    #    名字不是这一条就【不发请求】——悄悄换模型 = 批的不是跑的。
    if model != _YZ30_MODEL:
        return {"success": False, "kind": "submit_param",
                "error": "wan3-prime(yz30)：这条 lane 固定走 %r，现在解出来的是 %r ⇒ 拒（0 分、不发）。"
                         "要换档 = 换 skill、重新报价，不在这里传参切换。" % (_YZ30_MODEL, model)}
    out = str(params.get("output_path") or "").strip()
    tid = str(params.get("job_id") or "").strip()

    # ── 捞回：给了号就只 GET，一个 POST 都不发（重投=双花）──
    if tid:
        task, _j, qerr = _yz30_get_task(tid, base, api_key)
        if qerr:
            return {"success": False, "kind": "query", "job_id": tid, "error": "wan3-prime(yz30)：" + qerr}
        st = str((task or {}).get("status") or "").strip().lower()
        if st == "unknown":
            # 只有【待核对】才去 POST /v1/tasks/:id/refresh（平台的"主动核对原线路"）。
            # 🔴 processing/queued 不许也发这一 POST：捞回口的不变量是"给了号就只 GET、一个 POST 都不发"
            #    （闸 Y1 钉的就是这条）。给 processing 发 refresh 不但不必要，还会把"只 GET"这个可核对的形状
            #    变成"看不出来是捞的还是发的"。
            _t2, _j2, _e2 = _yz30_get_task(tid, base, api_key, refresh=True)
            if _t2:
                task, st = _t2, str((_t2 or {}).get("status") or "").strip().lower()
        url = str((task or {}).get("videoUrl") or (task or {}).get("url") or "").strip()
        if st not in _YZ30_OK or not url:
            return {"success": False, "kind": "query", "job_id": tid, "upstream_status": st,
                    "error": "wan3-prime(yz30)：任务 %s 还不可交付（status=%s，直链=%s，%s）"
                             % (tid, st, url or "无", str((task or {}).get("errorMessage") or "")[:160])}
        path, nbytes, derr = _wan30_download(url, out, base, api_key)
        if not path:
            return {"success": False, "kind": "stop", "job_id": tid,
                    "error": "wan3-prime(yz30)：直链落盘失败（%s，url=%s）—— 出了片没落盘：用号再捞，别重提交"
                             % (derr or "?", url[:120])}
        okv, probe = yz30_has_video_stream(path)
        if not okv:
            return {"success": False, "kind": "stop", "job_id": tid,
                    "error": "wan3-prime(yz30)：落盘的文件【读不出视频流】（%s）⇒ 不算到手，用号再捞" % probe}
        return {"success": True, "kind": "ok", "path": path, "bytes": nbytes, "job_id": tid,
                "model": model, "url": url, "upstream_status": st, "recovered": True,
                "quoted_amount": (task or {}).get("quotedAmount"), "billing_unit": "second",
                "video_probe": probe, "calls": 1}

    bad = yz30_payload_problems(params)
    if bad:
        return {"success": False, "kind": "submit_param", "error": "wan3-prime(yz30)：" + "；".join(bad)}
    dur = int(float(params.get("duration")))
    res = str(params.get("resolution") or "1080P").strip().upper().replace(" ", "")
    ratio = str(params.get("ratio") or params.get("aspect_ratio") or "adaptive").strip()

    media, notes = [], []
    for kind, key in (("reference_image", "image_urls"), ("reference_video", "video_urls"),
                      ("reference_audio", "audio_urls")):
        for ref in (params.get(key) or []):
            item, note, err = _yz30_media_item(kind, ref, base, api_key)
            if err:
                return {"success": False, "kind": "submit_param",
                        "error": "wan3-prime(yz30)：素材 %s 送不出去 ⇒ 【一个任务都没建、0 消耗】：%s" % (kind, err)}
            media.append(item)
            notes.append("%s: %s" % (kind, note))

    body = _json.dumps({"model": model, "prompt": str(params.get("prompt") or "").strip(),
                        "duration": dur, "resolution": res, "ratio": ratio, "media": media},
                       ensure_ascii=False).encode("utf-8")
    idem = _yz30_idem_key(params)
    st, _h, raw = _yz30_http("POST", base + "/v1/tasks",
                             headers={"Authorization": "Bearer " + api_key,
                                      "Content-Type": "application/json",
                                      "Idempotency-Key": idem, "User-Agent": BROWSER_UA},
                             body=body, timeout=180.0)
    try:
        j = _json.loads((raw or b"").decode("utf-8", "replace"))
    except Exception:                                            # noqa: BLE001
        # 🔴 结果未知：这条【不自动重发】。幂等 key 让"同一份参数再发一次"是安全的，
        #    但安全不等于该做 —— 先按盘上的号判，没号就说明连单都没建上，那时才允许上层重投。
        return {"success": False, "kind": "submit_unknown", "job_id": None, "idem_key": idem,
                "error": "wan3-prime(yz30)：提交回的不是 JSON（HTTP %s）：%s ⇒ 结果未知。"
                         "同一个 Idempotency-Key(%s) 重发是安全的（平台按编号恢复、不重复预扣），"
                         "但先看盘上有没有 wan3_job.json，别把已建上的单再建一遍"
                         % (st, (raw or b"")[:200].decode("utf-8", "replace"), idem)}
    if st is None or st >= 400:
        err = (j.get("error") or {}) if isinstance(j.get("error"), dict) else {}
        return {"success": False, "kind": "submit_rejected", "idem_key": idem,
                "error": "wan3-prime(yz30)：建单被拒（HTTP %s %s）：%s"
                         % (st, err.get("code") or "", str(err.get("message") or j)[:260])}
    task = j.get("task") or {}
    tid = str(task.get("id") or task.get("task_id") or j.get("id") or "").strip()
    if not tid:
        return {"success": False, "kind": "submit_unknown", "idem_key": idem,
                "error": "wan3-prime(yz30)：回成功了但【没有任务号】：%s ⇒ 别当成功，也别盲目重发" % str(j)[:220]}
    _wan30_note_job(params, tid, model, lane, provider="yz30")      # 号当场落盘（成功失败都写，进程死了还能捞）
    replayed = bool(j.get("replayed"))

    # ── 盯到终态 ──
    deadline = _t.time() + float(params.get("poll_timeout") or 1800)
    stt = str(task.get("status") or "").strip().lower()
    every = 8.0
    refreshed = False
    last = task
    while _t.time() < deadline:
        if stt in _YZ30_OK or stt in _YZ30_BAD:
            break
        if stt == "unknown" and not refreshed:
            _tz, _jz, _ez = _yz30_get_task(tid, base, api_key, refresh=True)
            refreshed = True
            if _tz:
                last, stt = _tz, str((_tz or {}).get("status") or "").strip().lower()
            continue
        _t.sleep(every)
        got, _jj, qerr = _yz30_get_task(tid, base, api_key)
        if qerr:
            continue                                             # 查询抖动：不因为一次读失败就判死
        last, stt = (got or {}), str((got or {}).get("status") or "").strip().lower()
    if stt in _YZ30_BAD:
        return {"success": False, "kind": "upstream_failed", "job_id": tid, "idem_key": idem,
                "upstream_status": stt, "error": "wan3-prime(yz30)：上游判死（%s）：%s"
                         % (str((last or {}).get("errorCode") or "TASK_FAILED"),
                            str((last or {}).get("errorMessage") or "")[:240])}
    if stt not in _YZ30_OK:
        return {"success": False, "kind": "submit_unknown", "job_id": tid, "idem_key": idem,
                "error": "wan3-prime(yz30)：盯到超时仍非终态（最后状态=%s，号=%s）⇒ 用号只捞，别重投"
                         % (stt or "(空)", tid)}
    url = str((last or {}).get("videoUrl") or (last or {}).get("url") or "").strip()
    if not url:
        return {"success": False, "kind": "stop", "job_id": tid,
                "error": "wan3-prime(yz30)：succeeded 但回里没有 videoUrl ⇒ 不算到手：%s" % str(last)[:220]}
    path, nbytes, derr = _wan30_download(url, out, base, api_key)
    if not path:
        return {"success": False, "kind": "stop", "job_id": tid,
                "error": "wan3-prime(yz30)：直链落盘失败（%s，url=%s）—— 用号再捞，别重提交"
                         % (derr or "?", url[:120])}
    okv, probe = yz30_has_video_stream(path)
    if not okv:
        return {"success": False, "kind": "stop", "job_id": tid,
                "error": "wan3-prime(yz30)：落盘的文件【读不出视频流】（%s）⇒ 不算到手，用号再捞" % probe}
    return {"success": True, "kind": "ok", "path": path, "bytes": nbytes, "job_id": tid,
            "model": model, "url": url, "upstream_status": stt, "calls": 1, "replayed": replayed,
            "quoted_amount": (last or {}).get("quotedAmount"), "billing_unit": "second",
            "duration": dur, "resolution": res, "ratio": ratio, "idem_key": idem,
            "video_probe": probe, "media_notes": notes}


def _manxue_http(method: str, url: str, headers: dict | None = None, body: bytes | None = None,
                 timeout: float = 60.0):
    """manxue 的【唯一出网口】（探活与回归都打这个缝）。返回 (status, headers, body)。

    HTTPError 不往外抛，翻成状态码让上层分流；跨域重定向剥掉 Authorization / X-API-Key（同 _wan30_http）。
    """
    import urllib.error
    import urllib.parse
    import urllib.request

    host = urllib.parse.urlsplit(url).netloc.lower()

    class _StripAuthRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, new_headers, new_url):
            new = super().redirect_request(req, fp, code, msg, new_headers, new_url)
            if new is not None and urllib.parse.urlsplit(new_url).netloc.lower() != host:
                for bag in (new.headers, new.unredirected_hdrs):
                    for k in list(bag):
                        if k.lower() in ("authorization", "x-api-key"):
                            bag.pop(k, None)
            return new

    hdrs = {"User-Agent": BROWSER_UA}                     # 这家前面也可能挂 CF：UA 永远带上
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=hdrs, method=str(method).upper())
    opener = urllib.request.build_opener(_StripAuthRedirect)
    try:
        with opener.open(req, timeout=timeout) as r:
            return int(r.status), dict(r.headers or {}), r.read() or b""
    except urllib.error.HTTPError as e:
        return int(e.code), dict(e.headers or {}), (e.read() or b"")


def _manxue_pick_url(obj):
    """按对方文档那条优先级取成片直链；output 可能是数组，逐条试。"""
    for path in _MANXUE_URL_PATHS:
        cur = obj
        for k in path:
            if isinstance(cur, list):
                cur = next((x for x in cur if isinstance(x, dict)), None)
            if not isinstance(cur, dict):
                cur = None
                break
            cur = cur.get(k)
        if isinstance(cur, str) and cur.strip().startswith("http"):
            return cur.strip()
    return ""


def _manxue_media(params: dict, model: str, card: dict):
    """收齐 images / videos / audios 并按参数卡核形状与数量。返回 (dict, err)。

    🔴 只吃公网 http(s) 外链：上游自己去拉，本机路径递过去只会换回一次失败往返（往返还计费的那种）。
    缺直链的正确做法是 `gateway.py upload <文件>`（0 分、记账、返回 file_name 与 download_url）。
    """
    got = {}
    for i, key in enumerate(("images", "videos", "audios")):
        raw = params.get(key)
        if raw is None:
            raw = params.get(_MANXUE_MEDIA_ALIAS[key])
        vals = [raw] if isinstance(raw, str) else list(raw or [])
        vals = [str(v).strip() for v in vals if str(v or "").strip()]
        bad = [v[:60] for v in vals if not v.lower().startswith(("http://", "https://"))]
        if bad:
            return None, ("manxue 的 %s 只吃公网 http(s) 外链，收到本地路径：%s —— 先跑 "
                          "`python D:\\video-skill\\backend\\gateway.py upload <文件>` 拿直链再提交（0 分、记账）"
                          % (key, "、".join(bad)[:160]))
        if len(vals) > card["media"][i]:
            return None, ("manxue %s 的 %s 最多 %d 个（参数卡上限，收到 %d 个）"
                          % (model, key, card["media"][i], len(vals)))
        if vals:
            got[key] = vals
    return got, None


def _manxue_video_payload(params: dict, model: str):
    """花钱【之前】的校验 + 请求体拼装（这三族共用扁平形，只有 guanfang-seedance-* 用 metadata 形、未接）。"""
    card = _MANXUE_VIDEO.get(str(model or ""))
    if not card:
        return None, ("manxue 模型 %r 没有线上参数卡（这张表只收 %s）"
                      % (model, " / ".join(_MANXUE_VIDEO)))
    job = str(params.get("job_id") or "").strip()
    out = str(params.get("output_path") or "").strip()
    if job:                                   # 只捞：一个提交请求都不发
        if not out:
            return None, "manxue 捞回要给 output_path（成片要落到盘上、能出示才算交付）"
        return {"mode": "poll", "task_id": job, "output_path": out, "body": {}}, None
    prompt = str(params.get("prompt") or "").strip()
    if not prompt:
        return None, "manxue 要给 prompt（文字描述永远必填）"
    if not out:
        return None, "manxue 要给 output_path（成片要落到盘上、能出示才算交付）"
    d_dur, d_ratio, d_res = card["default"]
    try:
        dur = int(d_dur if params.get("duration") in (None, "") else params.get("duration"))
    except (TypeError, ValueError):
        return None, "manxue 的 duration 要整数秒（收到 %r）" % (params.get("duration"),)
    if dur not in card["durations"]:
        contiguous = len(card["durations"]) == max(card["durations"]) - min(card["durations"]) + 1
        span = ("%d~%d 秒" % (min(card["durations"]), max(card["durations"]))) if contiguous \
            else ("只收离散档 %s 秒" % ("/".join(str(d) for d in card["durations"])))
        return None, "manxue %s 时长 %s（收到 %d 秒）" % (model, span, dur)
    ratio = str(params.get("ratio") or params.get("aspect_ratio") or d_ratio).strip()
    if ratio not in card["ratios"]:
        return None, "manxue %s 画幅只收 %s（收到 %r）" % (model, "/".join(card["ratios"]), ratio)
    res = str(params.get("resolution") or d_res).strip().lower()
    if res not in card["resolutions"]:
        return None, ("manxue %s 分辨率只收 %s（收到 %r）—— 不接的档不是【降画质能跑】，是没这个档"
                      % (model, "/".join(card["resolutions"]), res))
    media, err = _manxue_media(params, model, card)
    if err:
        return None, err
    if not any(media.values()) and card["modes"] == ("referenceImage",):
        return None, ("manxue %s 的参数卡只列 modes=referenceImage —— 至少给一张参考图或一条参考视频"
                      "（这条不能纯文生视频；要纯文字起步用 seedance-2.0-manxue / seedance-2.5-manxue，"
                      "它们的 modes 里有 textToVideo）" % model)
    body = {"model": model, "prompt": prompt, "ratio": ratio, "duration": dur, "resolution": res}
    body.update(media)
    # enable_acceleration：文档只说 V1 发、V2 不发，没给默认值 ⇒ 不自己造默认，调用方显式给了才带。
    if params.get("enable_acceleration") is not None:
        body["enable_acceleration"] = bool(params.get("enable_acceleration"))
    return {"mode": "submit", "task_id": "", "output_path": out, "body": body,
            "duration": dur, "resolution": res, "ratio": ratio}, None


def manxue_job_path(output_path: str) -> str:
    """manxue 的任务号留痕住哪：成片旁边（同目录、固定名）。判据只在这一份。"""
    import os as _o
    out = str(output_path or "").strip()
    return _o.path.join(_o.path.dirname(_o.path.abspath(out)), "manxue_job.json") if out else ""


def read_manxue_job(output_path: str) -> dict:
    import json as _j
    import os as _o
    p = manxue_job_path(output_path)
    if not p or not _o.path.isfile(p):
        return {}
    try:
        return _j.loads(open(p, encoding="utf-8").read()) or {}
    except Exception:                                   # noqa: BLE001
        return {}


def _manxue_note_job(params: dict, tid: str, model: str, route_name: str) -> str:
    """号一拿到就落盘（成功失败都写）。理由同 wan3 那条：前台命令到 10 分钟被工具强杀，
    号只在内存里 = 进程一死就只能重投 = 双花。写失败不影响这次生成，但要把原因说出来。"""
    import json as _j
    import os as _o
    import time as _t
    out = str(params.get("output_path") or "").strip()
    p = manxue_job_path(out)
    if not p or not str(tid or "").strip():
        return ""
    rec = {"kind": "manxue_job", "task_id": str(tid), "model": str(model or ""),
           "route": route_name, "output_path": _o.path.abspath(out),
           "submitted_at": round(_t.time(), 3), "pid": _o.getpid()}
    try:
        _o.makedirs(_o.path.dirname(p), exist_ok=True)
        old = read_manxue_job(out)
        if old.get("task_id") and old["task_id"] != rec["task_id"]:
            rec["prev_task_ids"] = (old.get("prev_task_ids") or []) + [old["task_id"]]
        tmp = "%s.%d.tmp" % (p, rec["pid"])
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(_j.dumps(rec, ensure_ascii=False, indent=1))
            _o.replace(tmp, p)
        finally:
            if _o.path.isfile(tmp):
                try:
                    _o.remove(tmp)
                except OSError:
                    pass
        return p
    except Exception as e:                              # noqa: BLE001
        print("[manxue_job] 任务号没落盘（这次生成照常，但进程一死就没法只读捞回）：%s: %s"
              % (type(e).__name__, str(e)[:120]))
        return ""


def _manxue_video_once(params: dict, base: str, api_key: str, model: str, route_name: str) -> dict:
    """提交一次 → 盯到终态 → 落盘。失败时【带上上游返回体】—— prime 那条把 5xx 的 body 丢了，
    结果每次事故盘上都只剩三个字符，谁也没法定性（2026-10-13 现网教训，新腿先补上）。"""
    import json as _j
    import time

    vals, err = _manxue_video_payload(params, model)
    if err:
        return {"success": False, "kind": "submit_param", "error": err}
    if params.get("model") and str(params.get("model")) != model:
        return {"success": False, "kind": "submit_param",
                "error": "manxue 这条路由钉的是 %s（收到 model=%r）—— 换模型就换 purpose，不许自带"
                         % (model, params.get("model"))}
    out = vals["output_path"]
    tid = vals["task_id"]
    auth = {"Authorization": "Bearer " + api_key}

    def _text(raw):
        return (raw or b"").decode("utf-8", "replace")[:300]

    def _fail(msg, kind="stop"):
        return {"success": False, "kind": kind, "job_id": tid or None, "error": "manxue：" + msg}

    if vals["mode"] == "submit":
        body = dict(vals["body"])
        try:
            st, _, raw = _manxue_http("POST", base + "/v1/videos", auth,
                                      _j.dumps(body, ensure_ascii=False).encode("utf-8"), timeout=120.0)
        except Exception as e:                          # noqa: BLE001
            return _fail("提交时连接断了（%s: %s），任务可能已建，用 output_path 旁那份留痕的号只捞，别重投"
                         % (type(e).__name__, str(e)[:160]), "submit_unknown")
        try:
            sub = _j.loads((raw or b"").decode("utf-8", "replace")) or {}
        except Exception:                               # noqa: BLE001
            sub = {}
        tid = str(sub.get("id") or sub.get("task_id") or "").strip()
        e_msg = str(((sub.get("error") or {}) if isinstance(sub.get("error"), dict) else {}).get("message") or "")
        if st not in (200, 201, 202) and not tid:
            # 参数/合规类拒绝：不建任务 ⇒ 改参数再发不算双花（4xx），5xx 则要说"结果未知"（同样带原文）
            kind = "submit_param" if 400 <= st < 500 else "submit_unknown"
            return _fail("提交回 HTTP %s%s，上游原文：%s"
                         % (st, ("（error.message=%s）" % e_msg) if e_msg else "", _text(raw)), kind)
        if not tid:
            return _fail("提交回 HTTP %s 但挖不到任务号（回包形状变了？原文：%s）" % (st, _text(raw)),
                         "submit_unknown")
        _manxue_note_job(params, tid, model, route_name)

    interval = float(params.get("poll_interval") or 5)          # 对方文档建议 5s
    deadline = time.time() + float(params.get("poll_timeout") or 1800)
    misses, last = 0, {}
    while time.time() < deadline:
        try:
            st, _, raw = _manxue_http("GET", base + "/v1/videos/" + str(tid), auth, timeout=60.0)
        except Exception as e:                          # noqa: BLE001
            misses += 1
            if misses >= 6:
                return _fail("查询连续 %d 次断线（%s: %s）—— 任务 %s 还挂着，别重发，用号只捞"
                             % (misses, type(e).__name__, str(e)[:120], tid), "submit_unknown")
            time.sleep(min(60.0, interval * misses))
            continue
        if st == 404:
            return _fail("查不到任务 %s（TASK_NOT_FOUND）—— 先确认这次扣没扣（对方的 /v1/models 同 key 可查额度），"
                         "确认没扣才允许重发" % tid)
        if st in (401, 403):
            return _fail("查询被拒 HTTP %s（这把 Key 失效或没这个模型的权限）—— 任务 %s 还挂着，别重发" % (st, tid))
        if not (200 <= st < 300):
            misses += 1
            if misses >= 6:
                return _fail("查询连续 %d 次 HTTP %s，最后一次原文：%s" % (misses, st, _text(raw)))
            time.sleep(min(60.0, interval * misses))
            continue
        misses = 0
        try:
            last = _j.loads((raw or b"").decode("utf-8", "replace")) or {}
        except Exception:                               # noqa: BLE001
            last = {}
        st_txt = str(_dig(last, ("status", "state", "task_status")) or "").strip().lower()
        if st_txt in _MANXUE_BAD:
            return _fail("任务 %s 终态=%s ｜ error.message=%s ｜ 原文：%s"
                         % (tid, st_txt, _dig(last, ("error_message", "fail_reason")) or "", _text(raw)))
        url = _manxue_pick_url(last)
        if st_txt in _MANXUE_OK or url:
            break
        time.sleep(interval)
    else:
        return _fail("盯到超时（%s 秒）仍非终态，最后一次状态=%s，任务 %s 还没死，用号只捞别重投"
                     % (int(params.get("poll_timeout") or 1800), st_txt or "(空)", tid), "submit_unknown")

    path, nbytes, derr = (None, 0, "没有直链")
    if url:
        # 落盘复用 prime 那条的下载纪律：首探不带凭据、同名不许静默覆盖（旧的留 .prev-<sha8>）、<1KB 判失败
        path, nbytes, derr = _wan30_download(url, out, base, api_key)
    if not path:
        path, nbytes, derr = _wan30_download(base + "/v1/videos/%s/content" % tid, out, base, api_key)
    if not path:
        return _fail("任务 %s 已 %s 但片没落盘：%s（直链=%s）" % (tid, st_txt or "completed", derr, url or "(无)"))
    return {"success": True, "path": path, "bytes": nbytes, "job_id": tid, "model": model,
            "route": route_name, "status": st_txt or "completed",
            "seconds": vals.get("duration"), "ratio": vals.get("ratio"), "resolution": vals.get("resolution"),
            "video_url": url, "usage": last.get("usage") or {}, "raw_status": last}


def _manxue_video_run(params: dict, route: dict | None = None) -> dict:
    """manxue 三条视频腿的入口（call() 按 purpose 分发到这里）。

    🔴 没有重试环（和 wan3-prime 那条相反）：这条渠道【没有】"失败不计次"的实测证据 —— 它的计价是
    积分、call 与秒两种单位混着用，一次失败到底扣没扣我们还没在对方账上核过。所以在看清真实扣费之前，
    一次调用只提交一次，失败由人决定要不要再来（自己重投 = 可能双花）。跑通一条后若要加环，
    判据必须像 prime 那样先有【平台侧计费读数】可核对，不能凭"多半不扣"。
    """
    route = route or {}
    base = str(params.get("base_url") or route.get("base_url") or _get("MANXUE_H3_BASE_URL") or "").rstrip("/")
    api_key = str(params.get("api_key") or route.get("api_key") or _get("MANXUE_H3_API_KEY") or "").strip()
    model = str(params.get("model") or route.get("model") or "").strip()
    route_name = "video.generate/%s" % (route.get("purpose") or "h3-manxue")
    if not base or not api_key:
        return {"success": False,
                "error": "manxue 没配 base_url/api_key（.env 的 MANXUE_H3_BASE_URL / MANXUE_H3_API_KEY）"}
    if not model:
        return {"success": False, "error": "manxue 路由没带 model 字段（config 的 MANXUE_*_MODEL 空了）"}
    return _manxue_video_once(params, base, api_key, model, route_name)


# ── 口型同步这条腿（lipsync / RunningHub LatentSync，2026-10-14 接进网关）────────────
# 🔴 为什么必须有这条腿（用户 2026-10-13 裁定："这个对话的目标就是改口型，wan3.0 没法直出了"）：
#    wan3-prime 的接口里【没有口型这个入参】—— 素材名单 _WAN30_MEDIA 只有
#    reference_image/video/audio/file，payload 里零个 lip/mouth/sync 字段（现读核实过）；
#    而改口播那条腿 `voiceover_replace.run_narration_swap` 自己的文档串就写着
#    "不做口型同步：嘴形与新词对不上是这一档的已知代价" ⇒ 口型只能是一条【后处理腿】，
#    不是某个模型参数。以前它挂在 ROUTES/价目上却没有适配器（call() 一路落到
#    NotImplementedError）⇒ 这条能力物理上调不动，账本里 lipsync 的调用数 = 0。
# ⚠️ 强度边界：单价 20 分/段是【登记值·未实测】（这条腿从没跑过一次，耗时与成功率都无数据）；
#    LatentSync 只重画【有人脸的说话段】，所以带货片里的产品特写段不会被它动到 —— 这正是
#    selective 那条形状的用途，别拿整条 run_lip_sync 当默认。
_LIPSYNC_KEYS = ("video_path", "audio_path", "output_dir", "segments")


def lipsync_problems(params: dict | None) -> list:
    """纯判据：返回【问题清单】，空表 = 可以发。判在发请求之前 ⇒ 参数错绝不预扣、绝不扣分。
    🔴 未知 key 也算问题：这条腿只有一个合法形状，多出来的字段要么是被拼错的参数名，
       要么是上层以为"传了就生效"（wan3-limits 第 14 条：不得静默丢传参）。"""
    p = dict(params or {})
    out = []
    for k in _LIPSYNC_KEYS[:3]:
        if not str(p.get(k) or "").strip():
            out.append("缺必填 `%s`" % k)
    for k in p:
        if k not in _LIPSYNC_KEYS:
            out.append("不认识的参数 `%s`（这条腿只收 %s）" % (k, "/".join(_LIPSYNC_KEYS)))
    for k in _LIPSYNC_KEYS[:2]:
        v = str(p.get(k) or "").strip()
        if v and not _os.path.isfile(v):
            out.append("`%s` 指向的文件不在盘上：%s" % (k, v))
    segs = p.get("segments")
    if segs is not None and not isinstance(segs, list):
        out.append("`segments` 要是 ASR 段列表（[{start,end},…]），给了 %s" % type(segs).__name__)
    return out


def _lipsync_leg(params: dict) -> dict:
    """走网关轨做口型同步：给了 segments ⇒ 只重画有人脸的说话段；没给 ⇒ 整条送 LatentSync。
    两种形状都收在同一对入口函数上（video_translate_pipeline），不另造第三条。"""
    import asyncio as _a
    from backend.services import video_translate_pipeline as _vtp
    vp = str(params["video_path"])
    ap = str(params["audio_path"])
    od = str(params["output_dir"])
    segs = params.get("segments")
    if segs:
        out = _a.run(_vtp.run_selective_lip_sync(vp, ap, list(segs), od))
        mode = "selective(%d 段 ASR)" % len(segs)
    else:
        out = _a.run(_vtp.run_lip_sync(vp, ap, od))
        mode = "full"
    ok = bool(out) and _os.path.isfile(str(out))
    return {"success": ok, "path": str(out or ""), "url": "", "task_id": "",
            "mode": mode,
            **({} if ok else {"error": "口型同步没落出文件（上游回了 %r）" % (out,)})}


def _mimo_fallback(capability: str, r: dict, params: dict, why: str) -> dict:
    """mimo 直连腿失败 ⇒ 退到 `config.MIMO_FALLBACK_LANE` 指定的那条 magpie 腿（2026-10-29 用户点名 GLM-5.3-Flash）。

    · 为什么可以在这里递归 `call()`：预扣/结算在上一层 `invoke()`，一次调用只一张 hold ⇒ 换腿不再扣一遍。
    · 🔴 **视频题绝不退**：magpie 的桥会丢 `video_url` part（2026-10-29 实测 39MB 原片递进去 pt 与裸问一模一样、
      模型自述"没有收到视频"）⇒ 退过去就是拿看不见视频的腿答视频 = 假成功，比失败更坏。宁可如实失败。
    · 兜底腿没配 / 路由里没有 ⇒ 如实把原拒因带出来，不静默退回"就当没配过"。
    """
    fb = str(_get("MIMO_FALLBACK_LANE") or "").strip()
    if not fb:
        return {"success": False, "text": None, "usage": {},
                "error": "%s（未兜底：MIMO_FALLBACK_LANE 是空的）" % str(why)[:160]}
    if (capability, fb) not in ROUTES:
        return {"success": False, "text": None, "usage": {},
                "error": "%s（未兜底：ROUTES 里没有 %s/%s 这条腿）" % (str(why)[:160], capability, fb)}
    if str(capability) == "video.analyze":   # 🔴 别写成 ：
        #    分发段那行是这个字面量，check-mimo-lane-version 的 B 组用 split(那个字面量)[1] 取段，
        #    写成一模一样就会把闸切到这个辅助函数身上（2026-10-30 B1/B4/B5 三条假红的原因）。
        ext = _os.path.splitext(str(params.get("path") or ""))[1].lower()
        if ext in (".mp4", ".mov", ".avi", ".mkv", ".webm"):
            return {"success": False, "text": None, "usage": {},
                    "error": "%s（这条是【视频】题：兜底腿在 magpie 桥上吃不到视频 ⇒ 不换腿，避免假成功）" % str(why)[:160]}
    got = call(capability, fb, **params) or {}
    if got.get("success") and got.get("text"):
        return {"success": True, "text": got.get("text"), "error": None,
                "usage": got.get("usage") or {}, "lane": "magpie/%s" % fb,
                "fallback_from": "%s/%s" % (capability, r.get("purpose")),
                "fallback_because": str(why)[:200]}
    return {"success": False, "text": None, "usage": got.get("usage") or {},
            "error": "mimo 失败(%s) 且兜底腿 %s 也没出词(%s)" % (str(why)[:110], fb, str(got.get("error"))[:140])}


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
        # 🔴 2026-10-30 用户定"wan3.0固定换"：这条身份现在指 youzan666 的 Prime（/v1/tasks + 幂等 + /v1/assets），
        #    不再与 wan30-studio 共用 tianyue 那个适配器。两条腿【分开】是必须的：
        #    它们计费口径不同（这家 billingUnit=second 有实时价目；tianyue 那条按次且没有对账口），
        #    共用一个适配器就会让"人批的价"和"实际花的口径"又分家（10-22 撤 TIANYUE_DURATION_MODE 同类的错）。
        return _yz30_video_run(params, r)

    if capability == "video.generate" and r["purpose"] in _MANXUE_VIDEO_PURPOSES:
        # 满血API（manxueapi.com）三条视频腿共用一个适配器：模型名由路由钉死（h3 / seedance 2.0 / 2.5），
        # 参数按 /api/model-capabilities 的线上卡在【本地】先判（0 分不发请求），一次调用只提交一次。
        return _manxue_video_run(params, r)

    if capability == "video.analyze":
        _am_model = r["model"] or _MODEL_FALLBACK.get(r["provider"])
        # 🔴 思考型视觉脑会把预算整段烧在 reasoning 上：实测 mimo-v2.6-flash 看一段 5s 小片，
        #    max_tokens=300 → completion 300 里 299 是 reasoning、正文 0 字 ⇒ 上游回 200 空内容，
        #    看起来像"这个模型不能识别视频"，其实是被预算卡死（2500 时 708 reasoning + 正确正文）。
        #    只对实测过的 mimo 兜底；别的模型别凭猜加（要跑先跑一条真的）。
        _am_budget = params.get("max_tokens") or (2500 if str(_am_model or "").startswith("mimo") else 300)
        _am_q = params.get("question") or "用一句话描述这个画面的关键信息。"
        # 🔴 抽帧补充腿一次递多张（2026-10-30）：`paths` 是图列表，`path` 仍兼容单文件。
        #    analyze_media 里对两者去重，所以这里直接把两份都传过去、由它拼部件 —— 不在这里再判一次形状，
        #    免得"哪个是权威"又变成两处。视频与多图混给由 analyze_media 明拒。
        _am_paths = [str(x) for x in (params.get("paths") or []) if str(x or "").strip()]
        _am_first = params.get("path") or (_am_paths[0] if _am_paths else "")
        if not _am_first:
            return {"success": False, "text": None, "error": "video.analyze 没给 path/paths（一个文件都没有 ⇒ 不发请求）"}
        if r.get("provider") == "magpie":
            # 看图腿走 magpie：带图实测三条都认得对，但 🔴 只给图 —— 视频 part 会被桥丢掉（见 ROUTES 那条注释）。
            # 模型名以 "opencode-zen/mimo-" 开头，上面那句 startswith("mimo") 判不到它 ⇒ 这里按腿表另夹一次。
            lanes = magpie_lane_specs(r.get("purpose"), "vision")
            if not lanes:
                return {"success": False, "text": None,
                        "error": "magpie 的带图腿表里没有这条 purpose（glm 带多图实测 finish=length，故意不进表）⇒ 不换腿、不假装跑过"}
            _meta_cap = {}                                      # 最后一次真发出去的字节/部件数（给物证用）

            def _media_call(base_url, api_key, model, _msgs, temperature=0.7, max_tokens=4000, extra=None):
                got = analyze_media(path=_am_first, paths=_am_paths, question=_am_q, model=model,
                                    base_url=base_url, api_key=api_key, max_tokens=max_tokens)
                for _k in ("bytes_before", "bytes_after", "resized", "media_parts", "attempts"):
                    _meta_cap[_k] = got.get(_k)
                return got.get("text"), got.get("error"), got.get("usage") or {}
            _t, _e, _u, _lane = _magpie_chat(r["base_url"], r["api_key"], lanes, [],
                                            temperature=params.get("temperature", 0.7),
                                            max_tokens=max(int(_am_budget or 0), MAGPIE_MIN_BUDGET),
                                            _call=_media_call)
            # 🔴 把"这次发出去的是什么"带回去：没有这几格，"8 帧到底有没有缩过、真发出去几个部件"只能猜，
            #    而抽帧补充腿的代价（实测 173 分/17324 tok）恰恰由这两样决定。
            return {"success": bool(_t), "text": _t, "error": _e, "usage": _u, "lane": _lane, **_meta_cap}
        _am = analyze_media(
            path=_am_first,
            paths=_am_paths,
            question=_am_q,
            model=_am_model, base_url=r["base_url"], api_key=r["api_key"],
            max_tokens=_am_budget,
        )
        if not (_am or {}).get("text") and r.get("provider") == "mimo":
            return _mimo_fallback(capability, r, params, (_am or {}).get("error") or "上游回空正文")
        return _am
    if capability == "text.complete":
        messages = params.get("messages") or [{"role": "user", "content": params["prompt"]}]
        if r.get("provider") == "magpie":
            # 文本腿走 magpie：腿序/次数/429 退避/预算下限全读网关那一张表（magpie_lanes），
            # 带不带图由【真发出去的 part】决定 —— 带图自动排除 glm，不靠调用方声明。
            lanes = magpie_lane_specs(r.get("purpose"), magpie_lane_kind(messages))
            if not lanes:
                return {"success": False, "text": None, "usage": {},
                        "error": "magpie 腿表里解不出这条 purpose 的模型名（config 的 MAGPIE_*_MODEL 没配？）⇒ 不发请求"}
            text, err, usage, lane = _magpie_chat(
                r["base_url"], r["api_key"], lanes, messages,
                temperature=params.get("temperature", 0.7), max_tokens=params.get("max_tokens", 4000))
            return {"success": bool(text), "text": text, "error": err, "usage": usage, "lane": lane}
        text, err, usage = _chat_compat(
            r["base_url"], r["api_key"], params.get("model") or r["model"] or _MODEL_FALLBACK.get(r["provider"], ""), messages,
            temperature=params.get("temperature", 0.7), max_tokens=params.get("max_tokens", 4000),
        )
        if not text and r.get("provider") == "mimo":
            return _mimo_fallback(capability, r, params, err or "上游回空正文")
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
                # ⚠️ 也必须带本机代理：结果图挂在 multimodal.vibelearning.top，这台机器【直连它会被 reset】
                #    （与出图那一刀同一个病因，见 config.IMAGE2_PROXY_URL）⇒ 只换 UA 不换代理 = 图生成了但落不了盘。
                _px = str(_get("IMAGE2_PROXY_URL") or "").strip()
                _op = urllib.request.build_opener(
                    *[urllib.request.ProxyHandler({"http": _px, "https": _px})] if _px else [])
                raw = (_op.open(
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
    if capability == "lipsync":
        _lp = lipsync_problems(params)
        if _lp:
            # 本地拒 ⇒ 不建 hold、不扣分、一个请求都不发（和 _WAN30_MEDIA 那条同一形状）
            return {"success": False,
                    "error": "lipsync 入参不合格：%s ⇒ 已拒，0 分未扣、任务未发起。" % "；".join(_lp)}
        return _lipsync_leg(params)
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
    # 🔴 免批【只有这三条】。2026-10-22 用户定案：`video.generate` 从这张名单里摘回去
    #   （原话"选方案1，把video.generate从免批名单摘回去"），推翻 2026-10-21 那句
    #   "video-skill 的工作流生视频都不需要审批，和 dsh 是独立的"。
    #   为什么必须收：超级复刻那条链的收口是【人点一次审批链接 ⇒ 服务端直接投 wan3.0】。
    #   video.generate 一免批，那条链接就变成装饰 —— approve 建不建条子都不影响提交，
    #   `kickoff_submit()` 五道护栏里的"人证 = dsh-card-click"永远不会被触发
    #   （现网实测：needs_spend_approval("video.generate","wan3-prime") 当时回 False）。
    # ⚠️ 代价要认（他点头时已说明）：这一改同时重新卡住三处，都读同一个 needs_spend_approval，
    #    不各抄一份 ——
    #      ① gateway.invoke() 的 video.generate 各腿（wan3-prime / wan30-studio / h3 / seedance /
    #         manxue / rpa）⇒ 没条子一律 approval_required、0 分未扣；
    #      ② 8090 各提交口的 `_spend_or_423()`（h3-studio / one-shot-replica / replica-wan3 /
    #         hook-extend）⇒ 那四个口回到"没条子 = 423、任务不提交"；
    #      ③ 8100 的待批/审批链接对 video.generate 重新是必经一格。
    #   哪天要"只免 8090 那条工作台、不免 DSH agent"，正解是给调用方加【来源身份】再按身份判，
    #   不是把整条能力放回这张白名单 —— 10-21 那次松开又收回来，就是这么一条教训。
    #   判据与回归：scripts/check-spend-approval.py（G1e 钉"白名单里不许混进任何视频/音频产出类"）。
)
# 🔴 按【来源】免批，不是把整条能力放回上面那张全局白名单（3524 行那条教训的正解）。
#   2026-10-22 深夜用户定：「本系统不需要批准」——人在 8090 工作台点「提交」本身就是同意。
#   而同日另一个会话定「video.generate 摘回要批」的理由是 DSH 那条链靠审批链接当唯一人证。
#   两条都要 ⇒ 只有启动脚本写明 WEEKSIR_ENTRY=workbench 的进程（=8090）免这一格；DSH agent 会话照旧要条子。
#   ⚠️ 只加"花钱产出类"里的入口，不许往这里塞 image.edit/video.edit/lipsync —— 那些在两条入口都该要批。
SPEND_APPROVAL_EXEMPT_WORKBENCH = ("video.generate",)
# 🔴 2026-10-12 用户口径："本系统（账号知识库裂变出片）不需要人工填表、不需要批条子" ⇒
#   有人把它登记成了 purpose 级的 ("video.generate","wan3-prime")。那一行**顺手把超级复刻的
#   花钱确认也一起免掉了**（wan3-prime 正是 super-clone 合同里写死的 spend），而用户同日给的
#   五步流程里第 3 步就是"编译结果拿到，agent 进行视频价格确认" ⇒ 那一行让流程少一格。
#   规矩照旧【默认要批】，免批必须显式登记；但登记要按【谁的花钱腿】来，不许借用别的线的 purpose：
#   账号知识库裂变那条线要免批，就给它自己一个新 purpose（例如 video.generate/kb-fission），
#   在那条线里用那条 purpose 调 —— 一条 lane 一个身份，这样"免掉谁的批"永远查得出来。
# 2026-10-21：用户定"video-skill 的工作流生视频都不需要审批，和 dsh 是独立的" ⇒ 整条能力已经在上面
#   `SPEND_APPROVAL_EXEMPT` 里松开，这里【不再重复登记】("video.generate","wan30-studio")。
#   同一个免批理由印在两个地方，将来撤一条就会留下一条没人认领的白名单。
SPEND_APPROVAL_EXEMPT_PURPOSES = (
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


def workbench_entry() -> bool:
    """本进程是不是"工作台入口"（8090 服务）—— 只认启动脚本写进环境的那一个。

    🔴 不认 params 里自报的 entry/source：自报身份只能用于显示分组，拿它当安全判据等于给人一条绕过批准的命令。
    ⚠️ 现读 env（不在 import 期算一次）：让回归闸能在同一进程里把两个方向都跑出来（开=免批、关=要批）。
    """
    return (_os.environ.get("WEEKSIR_ENTRY") or "").strip().lower() == "workbench"


def needs_spend_approval(capability: str, purpose: str = "") -> bool:
    """这条能力要不要"人给的条子"。判据唯一源 = SPEND_APPROVAL_EXEMPT（🔴 默认要批）
    + SPEND_APPROVAL_EXEMPT_PURPOSES（purpose 级免批；purpose="*" 或不传一律不免）
    + SPEND_APPROVAL_EXEMPT_WORKBENCH（只有工作台入口生效）。"""
    if str(capability or "") in SPEND_APPROVAL_EXEMPT:
        return False
    if workbench_entry() and str(capability or "") in SPEND_APPROVAL_EXEMPT_WORKBENCH:
        # 2026-10-22 深夜用户定「本系统不需要批准」：人在 8090 点「提交」本身就是同意，
        #   不再要第二遍点击。DSH 那条链（另一进程、环境里没有 WEEKSIR_ENTRY）不受影响，
        #   它收口的那张审批链接照旧是唯一人证 —— 那两个决定不冲突，冲突的只是"一刀切"。
        return False
    p = str(purpose or "").strip()
    if p and p != "*" and (str(capability or ""), p) in SPEND_APPROVAL_EXEMPT_PURPOSES:
        return False
    return True


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
    if not needs_spend_approval(capability, purpose):
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

# ── 段数的唯一机器源（2026-10-10 用户裁定：审批一次就行；2026-10-13 换成有主人的）──
# 为什么必须有：批准条子写死 count，而 count 此前是 agent 手填的 ⇒ 填 1 就要点 1 次；
#    填少了跑到第 2 段会被自己的闸拦下（钱没多花，但活停了），填多了等于替人多批。
# 🔴 2026-10-13 现网补的第二半：老判据只认 `TIMELINE.md` 和 `outputs/seg*`，而这条线既不产
#    TIMELINE、段目录又嵌在 `outputs/step2_wan3/chunk_01…03` ⇒ 数不到就回落"按单段建"，
#    人批了 1 段的量、机器花了 3 段的钱。现在段数由【算窗口的那一手】（Step 0）写进
#    `_submit_plan.json`，谁批谁读同一份；盘上没有它才退回数目录（并把来历如实说出来）。
SUBMIT_PLAN_NAME = "_submit_plan.json"      # 写侧唯一源 = super_clone_wan3.write_submit_plan()
_SEG_PREFIXES = ("seg", "chunk")            # 段目录两种命名：老写法 seg*、这条线的 chunk_NN


def _timeline_seg_table(path: str) -> int:
    """TIMELINE.md 里第一格表头为「段」那张表的行数；读不出来回 0。"""
    import io as _io
    n, seen = 0, False
    for ln in _io.open(path, encoding="utf-8", errors="replace").read().split(chr(10)):
        s = ln.strip()
        if not seen and s.startswith("|"):
            head = s.strip("|").split("|")[0].strip()
            if head in ("段", "段号", "Seg", "seg"):
                seen = True
            continue
        if seen:
            if not s.startswith("|"):
                break
            if s.replace("|", "").replace("-", "").replace(":", "").strip() == "":
                continue                      # 分隔行
            n += 1
    return n


def _seg_dirs_under(od: str, max_depth: int = 3) -> list:
    """outputs 下的段目录，返回【最深那一层】的那一组名字（取最深而不是逐层相加：
    chunk_01 里面还套着 step2_wan3/，相加会把同一批段数两遍）。"""
    import os as _os
    best: list = []
    for root, dirs, _files in _os.walk(od):
        rel = _os.path.relpath(root, od)
        depth = 0 if rel == "." else rel.count(_os.sep) + 1
        if depth >= max_depth:
            dirs[:] = []
            continue
        hits = sorted(d for d in dirs if str(d).lower().startswith(_SEG_PREFIXES))
        if hits:
            best = hits
    return best


def _submit_plan_read(task_dir: str):
    """找管线落的提交计划（任务目录根 → outputs/ 两处），没有回 None；坏 JSON 回 {'broken': 原因}。"""
    import json as _json
    import os as _os
    for cand in (_os.path.join(task_dir, SUBMIT_PLAN_NAME),
                 _os.path.join(task_dir, "outputs", SUBMIT_PLAN_NAME)):
        if not _os.path.isfile(cand):
            continue
        try:
            raw = _os.path.join(cand)
            data = _json.loads(open(raw, encoding="utf-8").read())
        except Exception as e:                            # noqa: BLE001
            return {"broken": "%s：%s" % (_os.path.basename(cand), str(e)[:120])}
        return {"plan": data, "path": cand}
    return None


def plan_segment_count(task_dir: str) -> dict:
    """返回 {count, source, detail}。count=0 表示【读不出来】⇒ 调用方必须拒，不许猜。"""
    import os as _os
    d = str(task_dir or "").strip()
    if not d or not _os.path.isdir(d):
        return {"count": 0, "source": "bad-dir", "detail": "目录不存在：%s" % d}

    tl_rows = 0
    tl = _os.path.join(d, "TIMELINE.md")
    if _os.path.isfile(tl):
        tl_rows = _timeline_seg_table(tl)

    # ① 管线算好的那份窗口 —— 有它就以它为准（它同时决定真提交几次，改窗口必重写它）
    sp = _submit_plan_read(d)
    if sp and sp.get("broken"):
        return {"count": 0, "source": "submit-plan-broken",
                "detail": "提交计划读不出来（%s）⇒ 段数没有主人，不许猜一个数建条子；"
                          "重跑 --stage compile 让它重新写一份" % sp["broken"]}
    if sp:
        plan = sp["plan"]
        try:
            n = int(plan.get("count") or 0)
        except (TypeError, ValueError):
            n = 0
        if n <= 0:
            return {"count": 0, "source": "submit-plan-empty",
                    "detail": "提交计划里 count=%s 不是正数 ⇒ 拒（重跑 --stage compile 让它自己写）"
                              % plan.get("count")}
        wins = plan.get("windows") or []
        desc = "、".join("%.2f-%.2fs" % (float(w.get("start", 0)), float(w.get("end", 0)))
                        for w in wins[:6]) or "无窗口"
        if tl_rows and tl_rows != n:
            return {"count": 0, "source": "plan-vs-timeline",
                    "detail": "两份事实打架：提交计划 %d 段（%s）／TIMELINE.md %d 行 ⇒ 先对齐再批，"
                              "拿哪一份建条子都是猜" % (n, desc, tl_rows)}
        tr = plan.get("trim") or {}
        win_note = ("，窗口 %.2f-%.2fs" % (float(tr.get("start", 0)), float(tr.get("end", 0)))) if tr else ""
        return {"count": n, "source": SUBMIT_PLAN_NAME,
                "detail": "管线 Step 0 算好的提交计划：%d 段（%s）%s；计划文件 %s"
                          % (n, desc, win_note, _os.path.relpath(sp["path"], d))}

    # ② 人审过的那张切段表
    if tl_rows:
        return {"count": tl_rows, "source": "TIMELINE.md", "detail": "人审过的那张切段表里 %d 行" % tl_rows}

    # ③ 兜底：数段目录（认 seg* 与 chunk*，允许嵌两层）
    od = _os.path.join(d, "outputs")
    if _os.path.isdir(od):
        segs = _seg_dirs_under(od)
        if segs:
            return {"count": len(segs), "source": "outputs/段目录",
                    "detail": "没有提交计划，按盘上段目录数出 %d 个（%s）⇒ 这条是【猜的兜底】，"
                              "跑一次 --stage compile 让管线自己写计划" % (len(segs), ", ".join(segs[:6]))}
    return {"count": 1, "source": "single-default",
            "detail": "没有提交计划、没有 TIMELINE.md、也没有段目录 ⇒ 按单段建条子；"
                      "真跑出来是多段时后面的段会被拒（不双花），要回来重新批"}


def plan_total_seconds(task_dir: str) -> tuple:
    """从这份提交计划里加出【总共要生成多少秒】：(秒数|None, 说明)。窗口读不到就回 None（不猜）。"""
    sp = _submit_plan_read(str(task_dir or ""))
    if not sp or sp.get("broken"):
        return None, "读不到提交计划（%s）" % (sp.get("detail") if sp else "目录里没有 _submit_plan.json")
    wins = (sp.get("plan") or {}).get("windows") or []
    tot = 0.0
    got = 0
    for w in wins:
        try:
            s = float(w.get("start")); e = float(w.get("end"))
        except (AttributeError, TypeError, ValueError):
            continue
        if e > s:
            tot += (e - s)
            got += 1
    if not got:
        return None, "计划里的 windows 读不出秒数（%d 项）" % len(wins)
    import math as _m
    return float(tot), "Σ窗口 %d 段 = %.2f 秒" % (got, tot)


def count_from_args(cap: str, purpose: str, task_dir: str):
    """把 --count-from 换成 (count, credits, 说明)。读不出来 ⇒ 抛 ValueError。

    🔴 credits 的算法【按这条 lane 的计费口径分岔】（`lane_bills_per_second`）：
       · 按条/段：count × 分/段（老形状，tianyue/RPA/manxue 那些）
       · 按秒：Σ秒 × 分/秒 —— 秒数只能从这份计划自己加，读不到就【拒建条子】，
         绝不退回"按段估一个数"：那正好造出"批 12 分、花 160 分"那种批的不是跑的条子。
    """
    r = plan_segment_count(task_dir)
    n = int(r.get("count") or 0)
    if n <= 0:
        raise ValueError("段数读不出来：%s ⇒ 不许建条子（猜一个数等于替客户多批）" % r.get("detail"))
    unit = int(unit_price(cap, purpose))
    if lane_bills_per_second(cap, purpose):
        secs, why = plan_total_seconds(task_dir)
        if secs is None:
            raise ValueError("%s/%s 是【按秒】计价的 lane，但 %s ⇒ 不许建条子。"
                             "正解：先让管线把提交计划 _submit_plan.json 写出来（--stage compile 那一刀就写），"
                             "再来报价；按段估一个数会把上限算少，第 2 段会被自己的闸拦停。"
                             % (cap, purpose, why))
        import math as _m
        ceil_secs = _m.ceil(secs)
        credits = unit * ceil_secs
        return (n, credits,
                "段数=%d 来源=%s；这条按【秒】计价：%d 分/秒 × %.2f 秒（进位 %d 秒）⇒ 上限 %d 分（%s）"
                % (n, r["source"], unit, secs, ceil_secs, credits, why))
    return n, n * unit, ("段数=%d 来源=%s；单价=%d 分 ⇒ 上限 %d 分（%s）"
                         % (n, r["source"], unit, n * unit, r["detail"]))


def submit_plan_id(task_dir: str) -> str:
    """提交计划的【指纹】：只算人批的那几量（段数 + 每条窗口 + trim + 参考片哈希），不含时间戳。
    读不到计划 ⇒ 空串（不在射程，不参与比对）。"""
    import hashlib as _h
    import json as _json
    sp = _submit_plan_read(str(task_dir or ""))
    if not sp or sp.get("broken"):
        return ""
    plan = sp["plan"]
    core = {"count": plan.get("count"), "windows": plan.get("windows"), "trim": plan.get("trim"),
            "source_sha8": plan.get("source_sha8")}
    return _h.sha256(_json.dumps(core, sort_keys=True, ensure_ascii=False)
                    .encode("utf-8")).hexdigest()[:8]


def _plan_root_near_params(params: dict) -> str:
    """从"片子要写进哪个目录"往上认【任务目录】（计划住在 <任务>/outputs/ 或 <任务>/ 根）。
    🔴 只认根那一份，不认段目录里的同名计划 —— 每个 chunk 里也有一份（count=1，说的是那一段），
       拿它当"这次批的范围"就等于把 1 段当成了整条计划。"""
    dirs = receipt_dirs(params)
    for d in dirs:
        if _os.path.isfile(_os.path.join(d, "outputs", SUBMIT_PLAN_NAME)):
            return d
    for d in dirs:
        if _os.path.isfile(_os.path.join(d, SUBMIT_PLAN_NAME)):
            return d
    return ""


def plan_drift_problems(capability: str, purpose: str, params: dict | None = None,
                        account: str = "local", session: str = "", now: float | None = None) -> list:
    """纯判据（消费【之前】）：本次提交所在的那份提交计划，必须就是人点批准时看到的那一份。
    空表 = 放行。三条不在射程，都要如实放行而不是假装管得住：
      · 这条腿不要批准（免批三类）；
      · 落点附近没有提交计划（别的线/没跑过 --stage compile）；
      · 命中的是【没有指纹的老条子】（2026-10-13 之前建的，迁移期一律放行，别把在途单子炸掉）。
    🔴 为什么必须有（2026-10-13 现网）：客户写「0-15s」，条子 scope 也写着「段1 0-15s」，
       实际提交的却是 `chunk_03`（6.3s 尾段）—— 因为"批的是哪一段"只存在于一句自然语言里，
       提交那一刀没有任何东西能和它比对。现在窗口落在 `_submit_plan.json` 里并有指纹，
       改窗口/改段数 = 指纹变 = 这张条子花不出去（要回来重新报价，不是悄悄换个范围）。
    """
    if not needs_spend_approval(capability, purpose):
        return []
    tdir = _plan_root_near_params(params)
    if not tdir:
        return []
    pid = submit_plan_id(tdir)
    if not pid:
        return []
    ts = float(now if now is not None else _time.time())
    grants = [g for g in (_approvals_load().get("grants") or [])
              if grant_lane_matches(g, capability, purpose, account, ts, session)]
    if not grants:
        return []                       # 没有候选条子 ⇒ 让批准闸去报"approval_required"，这里不抢话
    if any(str(g.get("plan_id") or "") in ("", pid) for g in grants):
        return []
    sp = _submit_plan_read(tdir) or {}
    plan = sp.get("plan") or {}
    want = "、".join("%.2f-%.2fs" % (float(w.get("start", 0)), float(w.get("end", 0)))
                    for w in (plan.get("windows") or [])[:6]) or "无窗口"
    mine = sorted({str(g.get("plan_id") or "?") for g in grants})
    return ["这次要提交的是 %d 段（%s），计划指纹 %s；而这张会话批过的条子签的是指纹 %s ⇒ 范围变了。"
            "批准认的是【那一份】切段方案，改窗口/改段数不叫扩范围、叫另一件事 ⇒ 重新报价、让人再点一次。"
            % (int(plan.get("count") or 0), want, pid, "/".join(mine))]

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
                         session: str = "", ttl: float | None = None,
                         plan_id: str = "") -> dict:
    """建一张【待批】。这一步不给任何额度 —— 它只是把报价和码立起来等人点。
    `plan_id` = 人这次批的是【哪一份提交计划】（段数 + 每条窗口 + 参考片哈希的指纹），
    核销时原样带到条子上，提交那一刀再比对一次 —— 见 `plan_drift_problems()`。"""
    now = _time.time()
    data = _approvals_load()
    _pending_save(data)
    p = {"id": "pend_" + _uuid.uuid4().hex[:8], "capability": capability, "purpose": purpose or "*",
         "account": account, "count": int(count), "credits": int(credits), "scope": scope,
         "plan_id": str(plan_id or ""),
         # 🔴 签上【此刻这条 lane 的渠道身份】：换上游（provider/模型/计费口径）就等于换路由，
         #    老条子必须在消费时失效、重新报价 —— 见 `spend_lane_stamp()` 那段（2026-10-30 补）。
         "lane_stamp": spend_lane_stamp(capability, purpose),
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
                             plan_id=p.get("plan_id", ""),
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


def known_balance(account: str):
    """账本里【真有这个账户】才回它的余额；查不到回 None。

    🔴 为什么不用 `ledger().balance()`：它对不认识的账户回**默认 100000**（实测
    `balance("__no_such__") == 100000`）⇒ 卡上/报价里那句"余额 100000 → 最低 99820"是个凭空数。
    现网 2026-10-11 08:23 那次报价的"余额 99544"就是这么来的（盘上真值 99532）。
    报价五格里"余额"这一格的出处必须是账本，不是解释器的默认值。
    """
    name = str(account or "").strip()
    if not name:
        return None
    try:
        rows = ledger().accounts() or []
    except Exception:                                       # noqa: BLE001
        return None
    for r in rows:
        if isinstance(r, dict) and str(r.get("member") or "").strip() == name:
            try:
                return int(r.get("balance"))
            except (TypeError, ValueError):
                return None
    return None


APPROVE_BUTTON = "确认发起 · 不可撤回"


def approval_notice(p: dict) -> dict:
    """批准卡上那段【点下去之后】—— 置顶窗与批准页共用这一份，谁都不许自己写一遍。

    🔴 为什么要这段（2026-10-12 用户："审核这里做一个批准确认，说明批准后发起任务没法撤回"）：
       卡上原有四格（要跑几段 / 最多扣多少分 / 多久内有效 / 这次要做的）全是【事前量】，
       没有一句说后果不可逆 —— 而"撤销"这个动作我们物理上签不出来（上游按次计费）。
       所以那一下点击必须带着它的后果一起被看见，而不是让人事后才发现回不去。
    🔴 同时必须写"不扣的情况"：只讲"不能撤回"不讲"失败本来就不该你买单"，那是拿恐惧换点头。
    数字一律现读（单价/余额都从账本与价目取），条子上写什么就说什么，不许另算一份。
    """
    p = p if isinstance(p, dict) else {}
    cap, pur = str(p.get("capability") or "?"), str(p.get("purpose") or "")
    lane = "%s/%s" % (cap, pur) if pur else cap
    count = p.get("count")
    credits = p.get("credits")
    acct = str(p.get("account") or "")
    bal = known_balance(acct)                               # None = 账本里没这个账户 ⇒ 不印余额
    spend = "（余额 %s → 最低 %s）" % (bal, bal - int(credits or 0)) if (
        isinstance(bal, int) and isinstance(credits, int)) else ""
    return {
        "headline": "点下去之后",
        "button": APPROVE_BUTTON,
        "bullets": [
            "这张条子当场生效，任务随即发起（%s · %s 段 · 最多 %s 分%s），不再问你第二次。"
            % (lane, count if count is not None else "?", credits if credits is not None else "?", spend),
            "🔴 不能撤回：上游按次计费，一旦交到厂商手里，撤销这个动作我们签不出来；已扣的分不退。",
            "不扣的情况：上游失败 / 超时 / 5xx ⇒ 不计次、预扣自动回滚（失败本来就不该你买单）。",
            "范围锁死：加段数、换路由、改片型 ⇒ 这张条子立刻失效，要重新报价、重新点一次。",
        ],
    }


def build_quote_summary(capability: str, purpose: str, account: str, count: int,
                        credits: int, scope: str, session: str = "") -> str:
    """报价正文（终端与审批卡共用一份，数字一律现算，不让人对着空气点头）。"""
    try:
        unit = unit_price(capability, purpose)
    except Exception:
        unit = "?"
    # 余额走 known_balance：账本里查不到就回 "?"，绝不拿解释器的默认 100000 当余额报给人。
    bal = known_balance(account)
    if bal is None:
        bal = "?"
    return ("路由：%s/%s\n账户：%s\n单价：%s %s\n段数：%s\n预计扣分：%s\n当前余额：%s\n会话：%s\n方案：%s"
            % (capability, purpose, account, unit, lane_price_unit_label(capability, purpose),
               count, credits, bal, session or "-", scope or "（没写 scope！）"))


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
    bal = known_balance(account)        # 与报价正文同一份判据：账本里没这个账户就是 None，不拿默认 100000 充数
    return {"session": session, "account": account,
            "pending_id": (pend[0].get("id") if pend else ""),
            "quote_text": (pend[0].get("quote") if pend else ""),
            "count": (pend[0].get("count") if pend else None),
            "credits": (pend[0].get("credits") if pend else None),
            "grant_ids": [g.get("id") for g in grants],
            "balance": bal, "charged": charged, "charged_total": sum(charged)}


def approval_session_state(sid: str, data: dict | None = None, now: float | None = None) -> dict:
    """这条会话在批准页上该看到哪一句 —— 判据【只在这一份】，页面只负责渲染。

    🔴 为什么要抽出来（2026-10-14 现网）：批准页原来只看 `/api/spend/pending`，然后对【任何】空结果
       印同一句"Agent 还没跑 gateway.py approve"。而那位客户的真实状态是"他 16:14 就点过批准了、
       条子 0/1 段还没被消费"——那句文案当场是错的，还会把人推去"让 agent 再建一张"
       （= 同一件事批两遍，正是这套条子机制最防的事）。
    五种状态各有各的人话：pending（等你点）/ approved-waiting（点过了，等 agent 花）/
    consumed（段数花完）/ expired（批过但过期作废）/ none（从没建过）。
    """
    import time as _t
    now = _t.time() if now is None else float(now)
    d = data if isinstance(data, dict) else _approvals_load()
    sid = str(sid or "")
    mine_p = [p for p in (d.get("pending") or []) if str(p.get("session") or "") == sid]
    mine_g = [g for g in (d.get("grants") or []) if str(g.get("session") or "") == sid]

    def _hhmm(ts):
        try:
            return _t.strftime("%H:%M", _t.localtime(float(ts)))
        except (TypeError, ValueError, OSError):
            return "?"

    def _left(ts):
        """剩余时长说人话。⚠️ 别只印 "16:14"：明天 16:14 到期会被读成"两分钟后作废"
        （2026-10-14 我自己写出这个歧义，在同一句本该防误解的话里）。"""
        try:
            sec = float(ts) - now
        except (TypeError, ValueError):
            return "时间未知"
        if sec <= 0:
            return "已过期"
        if sec >= 7200:
            return "还剩 %d 小时" % (sec // 3600)
        return "还剩 %d 分钟" % max(1, int(sec // 60))

    # head = 页面上那行加粗标题。措辞也留在服务端一份：页面只渲染，不许自己再分类、
    # 更不许再拼一套"状态→话术"（那会变成第二份判据，早晚和这里漂）。
    _HEAD = {"pending": "这里有一张等你批的条子",
             "approved-waiting": "你已经批过了 · 不用再点第二次",
             "consumed": "这张条子已经花完",
             "expired": "批过但已过期作废",
             "none": "这条会话没有待批条子"}
    # foot = 状态相关的那几行补充话。也留在这一份里：以前页面把"让它先建条子"和
    #        "15 分钟不点自动作废"写死在空态卡片尾巴上 ⇒ 对【已批准待消费】的会话，
    #        前一句让人去催 agent 再建一张（=批两遍），后一句把【待批】的 TTL 说成了这张条子的。
    _FOOT = {"pending": ["不要点别的会话的卡：那一下点的是【别的方案】的额度。",
                         "这张待批条子 15 分钟没人点就自动作废。"],
             "approved-waiting": ["agent 提交时会自动扣掉这一段；换了路由、加了段数、或改了提交计划，"
                                  "这张就花不出去 —— 那时要重新报价，不是再点一次。"],
             "consumed": ["要再跑就是新的范围：让 agent 重新报价，你再点一次。"],
             "expired": ["过期不补、也不会自动续 —— 要跑就得重新报价再批。"],
             "none": ["让 agent 先跑 gateway.py approve 建条子（建完会给一条审批链接），再回来看这一页。",
                      "不要点别的会话的卡：那一下点的是【别的方案】的额度。"]}

    def out(state, g=None, pid="", text=""):
        g = g or {}
        cnt = int(g.get("count") or 0)
        used = int(g.get("used") or 0)
        return {"state": state, "head": _HEAD.get(state, ""), "foot": _FOOT.get(state, []),
                "pending_id": pid or "", "grant_id": str(g.get("id") or ""),
                "used": used, "count": cnt, "credits": int(g.get("credits") or 0),
                "expires": float(g.get("expires") or 0), "attest_at": float((g.get("attest") or {}).get("at") or 0),
                "text": text}

    if mine_p:
        p = mine_p[-1]
        return out("pending", p, pid=str(p.get("id") or ""), text=(
            "这里有一张【等你批】的条子：%s / %s · %s 段 · 最多 %s 分 —— 点【批准，开始跑】之前，什么都不会跑。"
            % (p.get("capability"), p.get("purpose"), p.get("count"), p.get("credits"))))

    live = [g for g in mine_g if float(g.get("expires") or 0) > now]
    if live:
        g = next((x for x in live if int(x.get("count") or 0) - int(x.get("used") or 0) > 0), None)
        if g is not None:
            return out("approved-waiting", g, text=(
                "你已经批过了（%s）：这张条子还剩 %s/%s 段没花（最多 %s 分，%s）"
                "—— 不用再点第二次，现在该由 agent 去提交。"
                % (_hhmm((g.get("attest") or {}).get("at") or g.get("created")),
                   int(g.get("count") or 0) - int(g.get("used") or 0), g.get("count"),
                   g.get("credits"), _left(g.get("expires")))))
        g = live[-1]
        return out("consumed", g, text=(
            "这张条子已经花完（%s/%s 段 · %s 分）。要再跑就是【新的范围】——"
            "让 agent 重新报价、你再点一次，别拿这张旧条子续。"
            % (g.get("used"), g.get("count"), g.get("used_credits") or g.get("credits"))))

    if mine_g:
        g = next((x for x in mine_g if int(x.get("count") or 0) - int(x.get("used") or 0) > 0), None)
        if g is not None:
            return out("expired", g, text=(
                "批过但【已过期作废】（%s 到期，还剩 %s/%s 段没花）。过期不补、也不会自动续 —— "
                "要让 agent 重新报价再批一次。" % (_t.strftime("%m-%d %H:%M", _t.localtime(float(g.get("expires") or 0))),
                                                int(g.get("count") or 0) - int(g.get("used") or 0),
                                                g.get("count"))))
        g = mine_g[-1]
        return out("consumed", g, text=(
            "这张条子已经花完（%s/%s 段 · %s 分），且已过期。要再跑就是新的范围，得重新报价再批。"
            % (g.get("used"), g.get("count"), g.get("used_credits") or g.get("credits"))))

    return out("none", text=(
        "Agent 还没跑 gateway.py approve —— 这条会话从没建过条子，所以这里没有任何可批项。"
        "让它先建条子（建完会给一条审批链接），再回来看这一页。"))


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
    # 🔴 格子里写的不是数（"?" / "读不到"）**不算有出处**：数字解析器会直接跳过它，于是"五格全绿"
    #    其实是五格全空 —— 2026-10-12 把余额换成 known_balance 之后就是这个形状（正文印"当前余额：?"
    #    而判据读到 100000，B5 那条变异因为"改不动文本"静默拿了空集合）。
    for _f, _labels in _QUOTE_LABELS.items():
        if any(_f == f for f, _n in got):
            continue
        for _lb in _labels:
            _m = _re.search(r"%s[：:]\s*([^|\n\r]+)" % _re.escape(_lb), message or "")
            if _m and not _re.match(r"\s*\d", _m.group(1)):
                probs.append("%s 这一格写的不是数（%s）⇒ 没有出处就别贴这一格"
                             % (_f, _m.group(1).strip()[:24]))
                break
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
            "先跑 gateway.py approve 建条子（它会打出那条 ?session= 审批链接），再谈批准入口。" % "、".join(hit)]


def _approvals_save(data: dict) -> None:
    # ⚠️ 写这份 json 的规矩：先 `_approvals_load()` 拿到完整 dict、原地改要改的那块、再存。
    #    不许 `_approvals_save({"grants": ...})` 这种"只带一个键"的写法 —— 那是整份重写，
    #    会把 pending（待批）和 denials（拒绝留痕）一起抹掉（2026-10-08 真踩过，见 check_spend_approval）。
    tmp = SPEND_APPROVALS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        _json.dump(data, f, ensure_ascii=False, indent=2)
    _os.replace(tmp, SPEND_APPROVALS_PATH)


def spend_lane_stamp(capability: str, purpose: str) -> dict:
    """这条 lane 此刻的【渠道身份】：provider + 模型名 + 单价口径 + 单价。建条子时签进去，消费时比对。

    🔴 为什么换芯今天必须补这一格（2026-10-30）：条子过去只签 `能力/purpose/账户/段数/分数/会话/计划指纹`，
       而 `video.generate/wan3-prime` 这个 purpose 字符串【今天换了上游】（tianyue 按次 → yz30 按秒）。
       purpose 名没变 ⇒ 换芯之前建的那张条子照样能消费新渠道 = "人批的是 A 家的价、机器花的是 B 家的钱"，
       正是 AGENTS.md 里"换路由 ⇒ 这张条子立刻失效，要重新报价、重新点一次"那一句的物理漏洞。
       分数上限仍然卡得住总额，但【批的不是跑的】这件事本身已经违了 —— 所以判据要落在渠道身份上，不落在钱数上。
    读不到（路由被摘/resolve 抛）⇒ 回 {}：调用方按【不比对】处理并在报表里如实说"没签渠道身份"，
       不假装管得住（同 `plan_id` 那条强度边界）。
    """
    try:
        r = resolve(capability, purpose)
        return {"provider": str(r.get("provider") or ""),
                "model": str(r.get("model") or ""),
                "per_second": bool(lane_bills_per_second(capability, purpose)),
                "unit_price": int(unit_price(capability, purpose) or 0)}
    except Exception:                                        # noqa: BLE001
        return {}


def lane_stamp_problem(g: dict, capability: str, purpose: str) -> str:
    """这张条子的渠道身份和此刻这条 lane 还对不对得上。空串 = 对得上（或这张条子没签身份 = 老条子，放行）。"""
    old = (g or {}).get("lane_stamp")
    if not isinstance(old, dict) or not old:
        return ""
    cur = spend_lane_stamp(capability, purpose)
    if not cur:
        return ""
    if {k: str(old.get(k)) for k in cur} == {k: str(cur.get(k)) for k in cur}:
        return ""
    return ("批的时候是 %s/%s（%s %s 分%s），此刻这条 lane 是 %s/%s（%s %s 分%s）"
            % (old.get("provider") or "?", old.get("model") or "?", old.get("unit_price"),
               "分/秒" if old.get("per_second") else "分/段", "",
               cur.get("provider") or "?", cur.get("model") or "?", cur.get("unit_price"),
               "＝按秒" if cur.get("per_second") else "＝按段", ""))


def grant_lane_matches(g: dict, capability: str, purpose: str, account: str,
                       now: float, session: str = "", ignore_stamp: bool = False) -> bool:
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
    if owner and owner != str(session or ""):
        return False
    # 🔴 渠道身份（provider/模型/单价口径）变了 ⇒ 这张条子不算数，要重新报价（2026-10-30 换芯补）。
    #    `ignore_stamp=True` 只给只读探针用：它要报"这里有张同道条子，但它是给别家批的"，
    #    而消费口绝不能因为它"看起来同道"就放行。
    if not ignore_stamp and lane_stamp_problem(g, capability, purpose):
        return False
    return True


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
    pend = spend_pending(with_code=False) if needs_spend_approval(capability, purpose) else []
    hit = [g for g in live if approval_allows(g, capability, purpose, account, now, count, credits, session)]
    # 差在哪：同能力同 purpose 的活条子存在但不够 ⇒ 报段/分/会话三项，免得 agent 只能靠猜去试消费口
    near = [{"id": g["id"], "left": int(g.get("count", 0)) - int(g.get("used", 0)),
             "credits_left": int(g.get("credits", 0)) - int(g.get("used_credits", 0)),
             "owner_session": str(g.get("session") or ""),
             # 🔴 渠道身份不合的条子【照样列出来】，并写明"是给别家批的"：
             #    消费口判 False 之后如果探针里也看不见它，人只会看到"没有条子"，
             #    而真相是"有一条、但换芯作废了" —— 这两种情况要重新做的事完全不同。
             "lane_stamp_ok": (not lane_stamp_problem(g, capability, purpose)),
             "lane_stamp_why": lane_stamp_problem(g, capability, purpose),
             "expires_in": round(g.get("expires", 0) - now)}
            for g in live
            if grant_lane_matches(g, capability, purpose, account, now, session, ignore_stamp=True)]
    return {"read_only": True, "consumed": False,
            "capability": capability, "purpose": purpose, "account": account,
            "session": session or "", "need": count, "want_credits": credits,
            "requires_approval": needs_spend_approval(capability, purpose),
            "allowed": bool(hit) or not needs_spend_approval(capability, purpose),
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
            "next": ("直接提交：额度已生效，不需要再点批准" if (hit or not needs_spend_approval(capability, purpose))
                     else ("有人在等：待批已建、还没点 ⇒ 别重复建待批，也别调消费函数试探" if pend
                           else "需要批准：建【待批】并把报价发给用户，由他点一次"))}


def check_spend_approval(capability: str, purpose: str, account: str,
                         count: int = 1, credits: int = 0, session: str = "") -> dict:
    """找一张能覆盖这次请求的条子；找到就当场记账（used/used_credits 增加）并返回它。
    找不到 ⇒ `{ok:false, reason:"approval_required", quote:{...}}`——**不建 hold、不扣费、不发 HTTP**。"""
    if not needs_spend_approval(capability, purpose):
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
                      "要跑：先把这份报价原样发给用户、拿到明确同意，再由【用户本人】点一次审批链接"
                      "（建待批那一步打出来的那条 127.0.0.1:8100/approval?session= 地址；"
                      "2026-10-22 起审批只有这一条路，置顶批准窗已整条摘除）"
                      " —— agent 只能建【待批】，点之前 0 额度，它签不出。"
                      "（不在此处印建条子的命令：跑这条调用的就是 agent 自己，印出来等于把说明书递给要绕过的人。）"
                      "🔴 免批只有 %s 三类和登记过的 purpose 级 lane（%s），其余一律要批。"
                      % (capability, purpose, credits, count, "/".join(SPEND_APPROVAL_EXEMPT),
                         "/".join("%s/%s" % t for t in SPEND_APPROVAL_EXEMPT_PURPOSES)))}


def grant_spend_approval(capability: str, purpose: str = "*", account: str = "local",
                         count: int = 1, credits: int = 0, scope: str = "",
                         ttl: float | None = None, session: str = "",
                         attest: dict | None = None, plan_id: str = "") -> dict:
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
    if (needs_spend_approval(capability, purpose) and not (attest and attest.get("ok"))
            and not (ALLOW_GRANT_WITHOUT_ATTEST
                     and SPEND_APPROVALS_PATH != _APPROVALS_PATH_DEFAULT)):
        return {"ok": False, "reason": "no-attestation",
                 "error": ("no-attestation：这条调用没拿到人证 ⇒ 未签条子、0 额度。"
                           "正路：agent 跑 approve 建【待批】⇒ 把那条审批链接原样发给用户 ⇒ 由他点一次"
                           "【批准，开始跑】。🔴 不许把 code/consumed 命令交给用户去敲（那是已废的旧流程）。")}
    now = _time.time()
    data = _approvals_load()
    g = {"id": "ok_" + _uuid.uuid4().hex[:8], "capability": capability, "purpose": purpose or "*",
         "session": session or "",
         "account": account, "count": int(count), "used": 0, "credits": int(credits),
         "used_credits": 0, "scope": scope, "created": now,
         "plan_id": str(plan_id or ""),
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
    if not needs_spend_approval(capability, purpose):
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
        # 回执按【它自己声明的那份合同】核：合同同一档可以有多种形状（contract / contract-human）。
        # 2026-10-24 用户在 super-clone 登记了 contract-human（人工稿直投：人自己写的提示词，
        # 物理上没有编译器物证 storyboard/compile-bound），但 8090 那条 PUT prompt 界面腿一直没落码
        # ⇒ 人工稿的回执（版本 super-clone-wan3-human@1）拿默认 @12 编译合同核必然"版本不符"，
        # 直投提交不出去。这里接上：版本对上哪份合同就按哪份核，一份都对不上 ⇒ 照旧按默认核（fail-closed，
        # 假版本仍被"版本不符"拒）。必需步、手段白名单、物证判据（看图台账/负向锁定/指纹绑定）一格不降。
        cv = str(rec.get("contract_version") or "")
        contract = None
        for _var in ("", "human"):
            _c = _sc.contract_for(lane, variant=_var) if _var else _sc.contract_for(lane)
            if _c.get("version") and _c.get("steps") and _c["version"] == cv:
                contract = _c
                break
        problems = _sc.check_receipt(lane, rec, inputs=(), contract=contract)   # 指纹由管线自己核（它才知道输入清单）
        if not problems:
            return []
        return ["目录 %s 里的回执不合格：%s" % (d, "；".join(str(p) for p in problems)[:300])]
    return ["从 %s 往上 %d 层里都没有 %s ⇒ 这条成片不是从 %s 声明的入口跑出来的。"
            "要出片请跑该档 runner（它会在提交前写回执），而不是自己拼一次调用"
            % (dirs[0], CONTRACT_RECEIPT_WALK, _sc.RECEIPT_NAME, lane)]


# 🔴 2026-10-14 批准即点火的配套护栏：服务端为人批的那条方案起提交腿时，会在任务目录写一张
#    `.submit.pid`。这张牌活着 = 那一条已批方案【已经在被跑了】，第二次提交就是拿同一张条子的
#    下一个段去重复出片（同名产物互相覆盖、批准额度被花两遍）。
#    为什么必须落在网关而不是落在 runner：10-12 那次现网证明了"唤醒文案里写了别再起一台"
#    拦不住（11:51:50 点火 + 11:52:28 agent 自起 = 两台并发）。散文不是闸。
SUBMIT_PID_NAME = ".submit.pid"
# 🔴 一条 wan3-prime 实测 10–17 分钟/段，整条最多几十分钟。超过这个上限还"活着"的牌子按【死牌】算：
#    Windows 会复用 pid，一张没人摘的旧文件不能把这条任务永久锁住（永久锁 = 新的故障点）。
SUBMIT_PID_MAX_AGE = 6 * 3600
# env 只给回归闸留缝（闸在自己的临时目录造牌子，测完删）；线上没有"跳过这条守卫"的开关。
SUBMIT_PID_GUARD = (_os.environ.get("GATEWAY_SUBMIT_PID_GUARD") or "on").strip().lower() != "off"


def pid_alive(pid) -> bool:
    """这个 pid 现在真在跑吗。
    🔴 Windows 上 `os.kill(pid, 0)` 会对【已经退出、但句柄还被父进程握着】的子进程返回成功 ——
       而 8100 正是用 Popen 起的那条 runner，句柄就在它手里。拿它判活 ⇒ 那台早跑完了、牌子却被读成
       "还在跑" ⇒ 这张任务被永久挡住，客户补跑失败段也补不了（闸把合格对象判红，是最坏形状）。
       所以要拿 GetExitCodeProcess 比对 STILL_ACTIVE：只有 259 才算活着。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if _os.name == "nt":
        try:
            import ctypes
            import ctypes.wintypes
            _k32 = ctypes.windll.kernel32
            _h = _k32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
            if not _h:
                return False
            try:
                _code = ctypes.wintypes.DWORD()
                if not _k32.GetExitCodeProcess(_h, ctypes.byref(_code)):
                    return False
                return _code.value == 259                       # STILL_ACTIVE
            finally:
                _k32.CloseHandle(_h)
        except Exception:                                       # noqa: BLE001
            return False
    try:
        _os.kill(pid, 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def submit_card_state(pidfile: str) -> dict:
    """读一张 .submit.pid ⇒ {alive, pid, why}。**判据只这一份**：网关防撞与 8100 的复用检查都读它，
    两边各算一次迟早漂（漂的方向还不一定：一边误放行=双花，一边误拦=批准了却没人跑）。"""
    if not _os.path.isfile(pidfile):
        return {"alive": False, "pid": 0, "why": "没有牌子"}
    try:
        with open(pidfile, encoding="utf-8") as f:
            raw = (f.read() or "").strip()
    except OSError as e:                               # noqa: BLE001
        return {"alive": False, "pid": 0, "why": "牌子读不出（%s）" % type(e).__name__}
    try:
        pid = int(raw)
    except (TypeError, ValueError):
        return {"alive": False, "pid": 0, "why": "牌子上没有合法 pid（%r）" % raw[:24]}
    if not pid_alive(pid):
        return {"alive": False, "pid": pid, "why": "pid %s 已经不在" % pid}
    try:
        age = _time.time() - _os.path.getmtime(pidfile)
    except OSError:
        age = 0.0
    if age > SUBMIT_PID_MAX_AGE:
        return {"alive": False, "pid": pid,
                "why": "牌子躺了 %.0f 分钟，超过 %d 分钟上限 ⇒ 按死牌算（pid 会被系统复用）"
                       % (age / 60, SUBMIT_PID_MAX_AGE // 60)}
    return {"alive": True, "pid": pid, "why": "那台还在跑（pid %s）" % pid}


def kickoff_collision_problems(capability: str, purpose: str, params: dict | None = None) -> list:
    """返回【问题清单】，空表 = 放行。判在【消费批准之前】，顺序与回执那条同一条理由。"""
    params = params or {}
    if not SUBMIT_PID_GUARD:
        return []
    if str(params.get("job_id") or "").strip():      # 捞回已提交的任务：不是第二次提交，放行
        return []
    try:
        from backend import skill_contract as _sc
        lane = _sc.skill_for_spend(capability, purpose)
    except Exception:                                 # noqa: BLE001
        return []                                     # 合同读不到由 spend_contract_problems 负责拒，这里不重复判
    if not lane:
        return []                                     # 没合同认领的腿不在射程（别假装管得住）
    for d in receipt_dirs(params):
        st = submit_card_state(_os.path.join(d, SUBMIT_PID_NAME))
        if not st["alive"]:
            continue                                  # 没牌子 / 死牌 ⇒ 允许（补跑失败段是合法动作）
        if st["pid"] == _os.getpid():
            continue                                  # 牌子上就是本进程 = 服务端起的这条 runner 自己在提交
        return ["任务目录 %s 里有一台【服务端为这张已批条子起的】提交腿还在跑（pid %s，日志 submit.log）"
                "—— 这一条已经不需要你再提交：再 call 一次会拿同一张条子的下一段重复出片，"
                "同名产物互相覆盖，批准额度也被花两遍。要看跑到哪就读 submit.log，"
                "或问只读口 8100 的 /api/skills/obligation?session=<会话id>；"
                "那台退出之后（或牌子超过 %d 分钟按死牌算之后）再按失败的段补跑。"
                % (d, st["pid"], SUBMIT_PID_MAX_AGE // 60)]
    return []


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
    # 🔴 批准即点火（10-14）之后多的一刀：服务端已经替这条已批方案起了提交腿，
    #    再 call 一次 = 拿同一张条子的下一段重复出片。同样判在【消费之前】。
    _kc = kickoff_collision_problems(capability, r["purpose"], params)
    if _kc:
        return {"ok": False, "capability": capability, "purpose": r["purpose"], "result": None,
                "credits": {"estimated": est, "charged": 0,
                            "balance_after": ledger().balance(account)},
                "error": ("kickoff-collision：%s" % "；".join(_kc))[:700],
                "kickoff_collision": {"problems": _kc,
                                      "lane": skill_contract_lane_or_empty(capability, r["purpose"])}}
    # 🔴 批准的范围认指纹（2026-10-13）：这次要提交的窗口/段数必须就是人批的那一份计划。
    #    同样判在【消费之前】—— 判在之后会把不该花的段数先从人家批的额度里扣掉。
    _dp = plan_drift_problems(capability, r["purpose"], params, account=account,
                              session=_session_of(params))
    if _dp:
        return {"ok": False, "capability": capability, "purpose": r["purpose"], "result": None,
                "credits": {"estimated": est, "charged": 0,
                            "balance_after": ledger().balance(account)},
                "error": ("submit_plan（批的不是这一份）：%s" % "；".join(_dp))[:700],
                "plan_drift": _dp}
    # 🔴 花钱批准闸钉在**预扣之前**（顺序错了就等于"先扣了再问"）：生成视频相关的能力，
    #    没有覆盖本次请求的人批准条子 ⇒ 直接回 approval_required，不建 hold、不发 HTTP。
    #    唯一例外 = SPEND_APPROVAL_AT_SOURCE 里那些"花钱在别的进程"的腿：条子由那一端消费，这里不重复扣。
    if needs_spend_approval(capability, purpose) and not approval_consumed_at_source(capability, r["purpose"]):
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
        # 🔴 实扣必须【销账】（2026-10-13 修）：`Ledger.commit()` 早就写好了（退差额 + hold 翻 done +
        #    记 tokens），但 invoke() 的成功分支从来没调过它 —— 全仓 `ledger().commit(` 零调用点。
        #    后果不是"少一行日志"：账本永远只按【预估】扣，usage 结算出来的真实分数没落账，
        #    而且 hold 一直停在 pending ⇒ 和"进程被杀留下的泄漏预扣"长得一模一样，谁都分不清
        #    （现网证据：holds 表最后一笔 done 停在 10-11 00:02，之后所有成功调用都是 pending）。
        #    失败/异常两条分支照旧 rollback，所以这次只补成功那一头。
        done = ledger().commit(hold["hold_id"], charged, (tokens or None))
        if not done.get("ok"):
            # 销账失败不改写"这次调用成功了"这个事实（业务产物是真的），但必须留痕可查 ——
            # 静默吞掉就又回到"账本看着齐、实际没扣"那个盲区。
            print("[settle] 预扣没销掉 hold=%s：%s（余额仍停在预估 %s 分，实扣应为 %s 分）"
                  % (hold["hold_id"], done.get("reason"), est, charged))
        _am_seen = [str(x) for x in ([params.get("path")] + list(params.get("paths") or []))
                    if str(x or "").strip()]
        _am_seen = [p for i, p in enumerate(_am_seen) if p not in _am_seen[:i]]
        if capability == "video.analyze" and _am_seen:
            # 🔴 素材分析台账：跑完这条腿才记账 ⇒ agent 伪造不了『我看过图了』。
            #    写失败不影响业务（返回值里带原因），但【不许抛】——台账是留痕，不是新故障点。
            #    hold_id = 这次调用的预扣号，判据只认带凭证的行（没它就算 agent 手写的一条）。
            #    多图一次请求（抽帧补充腿）⇒ 每张都记一行、都带同一个 hold_id，但【实扣只挂第一行】：
            #    一次调用只有一张 hold，把 charged 复制 N 份会让报价里那个"台账合计"变成 N 倍假数。
            for _i, _p in enumerate(_am_seen):
                record_media_analysis(_p, capability, r["purpose"],
                                      _session_of(params), {
                    "charged": charged if _i == 0 else 0}, hold_id=(hold or {}).get("hold_id") or "")
        return {"ok": True, "capability": capability, "purpose": r["purpose"], "result": result,
                "credits": {"estimated": est, "charged": charged, "tokens": tokens, "basis": basis,
                            "hold_id": hold["hold_id"],
                            # 🔴 10-01 起这里写过 `done.get(...)` 而 `done` 从来没赋值 ⇒ 【每一次成功调用】
                            #    都在返回那一刻 NameError，被外层 except 吞成 ok:false + 回滚 + 退段 ⇒
                            #    成功看起来和失败一模一样（账本上只留 "preauth 紧跟 rollback"）。
                            #    现在 done 有值了：就是上面那次 commit 的结果，`settled` 把它摊在信封里。
                            "settled": bool(done.get("ok")),
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
#   · 拿错家（TIANYUE_* 是另一家）⇒ 直接 error
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
                    # 🔴 单位要跟着 lane 走（2026-10-30 换芯）：wan3-prime 现在是【分/秒】，
                    #    这一行还印"分"就等于给报价口喂了一个没有分母的数 —— 而报价那一格唯一的出处就是这行。
                    print(f"  {mark} {cap}/{p:<14} {unit_price(c, p):>4} %s  [{prov}] {extra}"
                          % lane_price_unit_label(c, p))
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
        blob["批准判据"] = ("默认要批：不在 SPEND_APPROVAL_EXEMPT 免批名单里，所以先建条子、由人点一次审批链接"
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
                        f"还剩 {max(0, round((g['expires']-now)/3600))}h  人证={att}  "
                        f"计划={g.get('plan_id') or '（旧条子·没指纹）'}  「{g.get('scope','')}」")
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
              "session": "", "count_from": "", "plan_id": "", "_c_given": False, "_x_given": False}
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
                kv["plan_id"] = submit_plan_id(kv["count_from"])   # 空 = 这份计划没指纹（老任务目录/没有提交计划）
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
            if kv["plan_id"]:
                print("   🔒 指纹 %s 已签进条子：改窗口/改段数 = 提交计划变 = 这张条子花不出去，"
                      "要回来重新报价（批的是【那一份】切段方案，不是「随便哪一段」）。" % kv["plan_id"])
            else:
                print("   ⚠️ 这份计划没有指纹（任务目录里没有 _submit_plan.json）⇒ 只能比段数，"
                      "比不了「具体哪一段」。跑一次 --stage compile 让管线把它写出来。")
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
                                int(kv["credits"]), kv["scope"], kv["session"], kv["ttl"],
                                plan_id=kv["plan_id"])
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
        # 🔴 链接一律带 ?session= —— 批准页现在按它筛卡（不带 = 客户看到的是全局列表，
        #    多会话并发时他能替别的方案点头）。这一行是 agent 唯一许贴给客户的那条地址。
        appr_url = "http://127.0.0.1:8100/approval?session=%s" % (kv.get("session") or "")
        # 🔴 给人看的只有【一条链接、一次点击】。2026-10-22 用户定"审批不要弹窗，就是一个审批链接，
        #    点了就直接提交任务" ⇒ 置顶批准窗整条摘掉（它从来不是第二种人证：窗和页面走的是同一个
        #    POST /api/spend/approve，额度仍只有服务端 consume_by_click 签得出，删壳不削弱人证）。
        #    原来这里连着印三条路（置顶窗 / DSH 审批卡 / 在自己终端跑 `gateway.py code …`），
        #    实际效果就是人要么点三次要么被 agent 追问"批了吗"；而那句 code 命令本身就违反既有禁令
        #    —— 跑 approve 的就是 agent，把命令印进 stdout 等于把说明书递给能自己核销的那一方（闸 G4）。
        print("⇒ 点这一条链接，【一次】就生效：人点上【批准，开始跑】那一下，服务端在同一次请求里"
              "自己起提交腿 ⇒ 不用再回一句“批了”，agent 只需要交付。")
        print("   %s" % appr_url)
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
    # 🔴 输出流只改 errors、不改 encoding：这一份 CLI 的提示文案里有 ⇒/⚠️/🔴 这类 GBK 装不下的
    #    字符，而"人/agent 在普通控制台（cp936）里直接敲 gateway.py approve|call"是受支持的用法。
    #    实测撞过两类后果：拒建条子时【原因】打不出来（调用方只看到 traceback）、
    #    以及换兜底腿那句 print 抛异常把整条提交腿打死。
    #    不把 encoding 改成 utf-8 是故意的：那会把 GBK 控制台下的这个 bug 永久遮住。
    try:
        _sys.stdout.reconfigure(errors="replace")
        _sys.stderr.reconfigure(errors="replace")
    except Exception:  # noqa: BLE001  不是 TextIOWrapper（被换过）就算了，不许因为装不上而退出
        pass
    # 🔴 原来写成 `_cli(...)` 把返回码丢了 ⇒ 用法错误也退 0，调用方（脚本/闸）会把"没执行"当"执行成功"。
    raise SystemExit(_cli(_sys.argv[1:]))
