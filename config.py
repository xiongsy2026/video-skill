"""
视频爆款复刻工作流 - 配置文件
"""
import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录
BASE_DIR = Path(__file__).parent

class Settings(BaseSettings):
    """应用配置"""
    
    # MiMo API 配置
    MIMO_BASE_URL: str = "https://token-plan-cn.xiaomimimo.com/v1"
    MIMO_API_KEY: str = ""

    # === 统一网关「文本推理脑」注册表（2026-10-01 全中转实测结果，模型名集中在这里、不散在代码里）===
    # 每条 = 网关的一个 text.complete purpose；.env 可覆盖成别的型号名。
    # 注释里的标记：OK=实测出词 / BUDGET=实测能跑但思考会吃满小预算(max_tokens 需 ≥2000) / 需充值·开通见 ADR
    TEXT_MODEL_GEMINI38: str = "gemini-3.8-flash"        # zexapi OK（5.7s·908 tok）gemini-3.5 那条已无渠道
    TEXT_MODEL_CLAUDE48: str = "claude-opus-4-8"         # zexapi OK（4.7s·139 tok）最省 token
    TEXT_MODEL_GPT61: str = "gpt-6.1-sol"                # hub.nocannobb OK（4.0s·28 tok）必须带浏览器 UA
    TEXT_MODEL_GPT56: str = "gpt-5.6-sol"                # hub.nocannobb 同 hub 另一条；用 GPT56_API_KEY 才过 Cloudflare
    TEXT_MODEL_QWEN38MAX: str = "qwen3.8-max"            # dashscope compatible-mode OK（1.2s·97 tok）
    TEXT_MODEL_MIMO_PRO: str = "mimo-v2.6-pro"           # mimo OK（6.1s·232 tok）上一题样本最有人味
    TEXT_MODEL_QWEN38_27B: str = "qwen3.8-27b"          # 唯一在用出处 = 下面的 ninfer 腿（qn-ninfer）；阿里百炼那条同名模型的腿 2026-10-13 已删（欠费）
    # === ninfer（api.qn-einnp.com/ninfer/v1）：qwen3.8-27b 当前唯一能出词的一家 ===
    # 2026-10-13 用户给了 base_url + key 要求接入，先测通再挂。0 消耗只读探测 GET /models 回
    #   {"data":[{"id":"qwen3.8-27b","max_model_len":131072,"owned_by":"ninfer"}]} —— 这把 key 底下只有这一个模型；
    # 一次真 chat 0.4 秒回文字 "2"，usage = prompt 27 / completion 2 / total 29（还带 timings 与 reasoning_tokens）。
    # 🔴 key 只从 .env 的 QN_NINFER_API_KEY 读，源码里不留默认值。
    QN_NINFER_BASE_URL: str = "https://api.qn-einnp.com/ninfer/v1"
    QN_NINFER_API_KEY: str = ""
    # 🔴 上游这个 key 的 2.6 只有 flash 与 pro 两档（2026-10-09 GET /v1/models 实测：
    #    mimo-v2.5 / -asr / -pro / -tts×3 / mimo-v2.6-flash / -pro）⇒ 裸 "mimo-v2.6" 不存在，别写。
    #    这条同时喂 text.complete/mimo 与 video.analyze/mimo（看图看片的备选眼）。
    TEXT_MODEL_MIMO: str = "mimo-v2.6-flash"             # mimo OK（2026-10-09 实测 91 tok·1 分·出词正常）
    TEXT_MODEL_DEEPSEEKV4: str = "deepseek-v4-pro"       # zexapi/manxue BUDGET：900 预算返回空，需 ≥2000

    # === magpie 本地池（127.0.0.1:3425，OpenAI 兼容）—— 2026-10-20 接进网关的三条 free 腿 ===
    # 起因：小米那把 token-plan-cn 整把 429 `quota exhausted`（mimo-v2.6-pro 直连与网关两条路同一把 key，
    #   实测 text.complete/mimo_pro 当场 429），超级复刻的 分镜/编译/看图 三条腿全吃它 ⇒ 一起断。
    # 2026-10-20 自测（C:\dsh\scratch\probe8099\magpie_probe.py，本地 free、0 分）：
    #   · 纯文本 三条都出正文；带 1 图 三条都认对（prompt_tokens 291→593 / 481→687 / 345→770 = 真吃图）
    #   · 🔴 带视频三条都不吃：prompt_tokens 291→291 / 481→480 / 345→345（一个字节没涨）、三条都答"没收到"。
    #     模型卡上 mimo 写着 in=text,image,audio,video、glm 写 in=…video —— 卡不算数，桥算数：
    #     magpie 的 chat part 白名单只有 text/image_url/file。⇒ **吃视频的腿（Step1 分镜）不能用这三条**。
    #   · 预算下限：max_tokens=60 时 60 个 token 整段进 reasoning、content=None ⇒ 见 gateway.MAGPIE_MIN_BUDGET。
    # 🔴 key 实测不校验（不带/乱写都 200），这里给一个**非空占位**而不是空串：网关的
    #    `_chat_compat`/`analyze_media` 都按「base+key 缺一个就本地拒」写（那是 fail-closed，不该为这条腿放开）。
    #    哪天池子开了鉴权，把真 key 写进 .env 的 MAGPIE_API_KEY，不动码。
    MAGPIE_BASE_URL: str = "http://127.0.0.1:3425/v1"
    MAGPIE_API_KEY: str = "magpie-local-unvalidated"
    MAGPIE_MIMO_MODEL: str = "opencode-zen/mimo-v2.6-flash-free"
    MAGPIE_DEEPSEEK_MODEL: str = "opencode-zen/deepseek-v4.1-flash-free"
    MAGPIE_GLM_MODEL: str = "opencode-zen/glm-5.3-flash-free"
    # 🔴 2026-10-22 夜新增：qoder 池的 `qfmodel` 就是 **Qwen3.8-Flash**（模型卡 display_name 原话），
    #    卡上 input=[text,image]、1M 上下文。今晚拿真产品图 + 同一道题四方对照，它是唯一
    #    "结构看对（认出顶部那个紫色旋盖）+ logo 读对（jio）+ 不补画面里没有的成分" 的一条，36.1s。
    #    ⚠️ 它【不吃视频】：裸问 pt=42 → 带 10.2MB 原片 pt=82（Δ=40 = 只有题目文字），
    #    且模型自己答"没有收到视频" ⇒ 分镜腿绝不能指它（和 opencode 那三条同一个毛病：桥丢 video part）。
    MAGPIE_QFMODEL_MODEL: str = "qoder-cn/qfmodel"

    # 🔴 腿序（主腿→兜底）也在这里，不在网关写死 —— **2026-10-20 用真 payload 实测定的序**
    #    （题面 = 盘上真产物：9 镜 storyboard + 2 张产品图 + STEP1_5 系统合同，166KB，预算 8192；
    #     判据 = 管线真契约【单条纯文本 Wan3.0 prompt】+ Shot 行数 + 图N 不越界，每腿打 3 次）：
    #      deepseek-v4.1-flash  3/3 合格｜墙钟 17.1/17.9/20.6s｜图N 每次=[1,2] 引用全中
    #      mimo-v2.6-flash      3/3 合格｜墙钟 69.8/200.8/75.5s（慢 3–12 倍）｜🔴 图N 引用漂：两次只引 [1]、一次【整条没引用】
    #      glm-5.3-flash        0/3｜三次都把 8192 预算【整段烧在 reasoning】⇒ 正文 0 字、finish=length、白等 85–117s
    #    ⇒ glm 从两张表里【整条摘掉】：留一个 0/3 的兜底位不是保险，是多花 100 秒再失败一次。
    #    ⚠️ 他说的"glm 上下文不够"实测下来不是窗口不够（卡上 200k），是**思考把输出预算吃光** —— 修法不同。
    MAGPIE_TEXT_LANES: str = "magpie_deepseek:3,magpie_mimo:2"
    # 🔴 带图表（看图腿走它）2026-10-22 夜按【新实测】改序：qfmodel 主 → opencode mimo free → deepseek 兜。
    #    依据是今晚同一张真产品图、同一道题的四方对照（判据=结构看对没 / logo 读对没 / 有没有补画面外）：
    #      qoder-cn/qfmodel(Qwen3.8-Flash)  ✅ 认出紫色旋盖、读对 jio、明写"规格成分本面未见，不按常识补"｜36.1s
    #      opencode mimo-v2.6-flash-free     ✅ 结构与 OCR 都对（mimo 家族看图一直比它文本稳）｜实测 37.1s
    #      opencode deepseek-v4.1-flash-free 🔴 把真实存在的盖子看成"未开口、无盖子或旋盖"｜20.1s
    #      qoder-cn/dfmodel(DeepSeek-Flash)  🔴 同样"未见外露的旋盖" + logo 读成 jie ｜🔴 374.4s（6 分 14 秒，不能用）
    #    ⚠️ 三条都不吃视频（qfmodel 实测 Δ=40）⇒ 这张表只服务【看图】，分镜腿另走 mimo 直连。
    MAGPIE_VISION_LANES: str = "magpie_qfmodel:2,magpie_mimo:2,magpie_deepseek:1"


    # API 配置
    # 切到 hub.nocannobb.com 中转（codex.theyouyi.site 网关 60s 超时会 504）
    GPT55_BASE_URL: str = "https://hub.nocannobb.com/v1"
    GPT55_API_KEY: str = ""
    # 同一个 hub 的第二把 key（2026-10-04 用户给的，专给 gpt-5.6-sol 这条腿）。
    # ⚠️ 不覆盖 GPT55_API_KEY：那条正在给 text.complete/gpt61 用，换掉会把能跑的腿弄死。
    GPT56_API_KEY: str = ""

    # OpenAI 兼容 API（视频翻译用：ASR Whisper + GPT翻译）
    # 切到 hub.nocannobb.com 中转（codex.theyouyi.site 网关 60s 超时会 504）
    OPENAI_API_KEY: str = ""
    OPENAI_BASE_URL: str = "https://hub.nocannobb.com/v1"
    OPENAI_MODEL: str = "gpt-5.6-sol"
    
    IMAGE2_BASE_URL: str = "https://image1.vibelearning.top/v1"
    IMAGE2_API_KEY: str = ""
    # GPT Image 2.5（该端点 /models 实测三型：gpt-image-2、gpt-image-2.5-sunburst、gpt-image-2.5-flare）。
    # sunburst=保真默认（Framewright gpt_image_2_5 的 default_model_id）；要提速档就在 .env 覆盖成 gpt-image-2.5-flare。
    IMAGE2_IMAGE25_MODEL: str = "gpt-image-2.5-sunburst"

    # zexapi Omni 配置（Google Gemini Omni Flash 1.1 中转，文生/图生视频）
    OMNI_BASE_URL: str = "https://zexapi.com"
    OMNI_API_KEY: str = ""
    OMNI_MODEL_T2V: str = "omni_flash-10s"       # 文生/参考图/视频修改
    OMNI_MODEL_FL: str = "omni_flash-10s-fl"     # 首尾帧图生视频

    # Agnes（国际站 OpenAI 兼容：文本 / 视频）
    AGNES_BASE_URL: str = "https://apihub.agnes-ai.com/v1"
    AGNES_API_KEY: str = ""
    AGNES_TEXT_MODEL: str = "agnes-3.0-flash"
    # 选定的免费最新图片模型（文生图 / 图生图 / 多图合成）
    AGNES_IMAGE_MODEL: str = "agnes-image-2.5-flash"
    # 选定的免费最新视频模型（720P，文生/图参考；不支持参考视频）
    AGNES_VIDEO_FLASH_MODEL: str = "agnes-video-2.5-flash"
    AGNES_VIDEO_MODEL: str = "agnes-video-2.5"
    
    # Grok API 配置（视频生成）
    GROK_BASE_URL: str = "https://api.x.ai/v1"
    GROK_API_KEY: str = ""

    # 结构性复刻专用 Grok（结构迁移推理）
    # hub.nocannobb.com 网关，OpenAI 兼容协议，xai grok-4.6
    SR_GROK_BASE_URL: str = "https://hub.nocannobb.com/v1"
    SR_GROK_API_KEY: str = ""
    SR_GROK_MODEL: str = "grok-4.6"
    
    # Gemini 配置
    GEMINI_PROFILE_DIR: str = str(BASE_DIR / "gemini_profile")
    CHROME_PATH: str = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

    # RunningHub API 配置（动作迁移）
    # 注：仓库里曾把旧 key 写在注释中（部分工作流对它返回 414）——那把已进 git 历史、撤不回，
    #     只能在 RunningHub 侧作废轮换。密钥一律只从 .env 读，源码/注释里都不许留。
    RUNNINGHUB_API_KEY: str = ""
    RUNNINGHUB_API_KEY_OLD: str = ""
    # 企业级 API Key（不受 810 限制，可调用任意工作流）
    RUNNINGHUB_ENTERPRISE_API_KEY: str = ""  # 国际站企业 key（DreamID-V 换脸、HeyGem 对口型等）
    # H3 标准模型专用 Key
    RUNNINGHUB_H3_API_KEY: str = ""

    # === 动作迁移模式：前端可选（"multi"=多图V9 / "single"=单图AI App）===
    # 默认值（可被前端请求覆盖）。多图一致性更好，单图速度更快但易穿模
    RUNNINGHUB_MOTION_MODE_DEFAULT: str = "multi"

    # ── 多图模式（V9 ComfyUI 工作流，主图+人脸+场景+全身，一致性高，不穿模）──
    # 299=主参考图(替换后关键帧), 507=人脸, 508=场景, 509=人物全身, 510/517=预留位(不传值但保留节点)
    # 545=VHS_LoadVideo(视频输入), 515=多参开关(AnyBooleanSwitch, 始终true)
    RUNNINGHUB_MOTION_MULTI_WORKFLOW_ID: str = "2061660674434101250"
    RUNNINGHUB_MOTION_MULTI_IS_AI_APP: bool = False
    RUNNINGHUB_MOTION_MULTI_IMAGE_NODES: str = "299,507,508,509,510,517"
    RUNNINGHUB_MOTION_MULTI_VIDEO_NODE: str = "545"
    RUNNINGHUB_MOTION_MULTI_MULTISWITCH_NODE: str = "515"

    # ── 单图模式（AI App 工作流，仅主图，速度快但易穿模）──
    # 299=主参考图(image), 275=视频(video)
    RUNNINGHUB_MOTION_SINGLE_WORKFLOW_ID: str = "2068645846073307137"
    RUNNINGHUB_MOTION_SINGLE_IS_AI_APP: bool = True
    RUNNINGHUB_MOTION_SINGLE_IMAGE_NODES: str = "299"
    RUNNINGHUB_MOTION_SINGLE_VIDEO_NODE: str = "275"
    RUNNINGHUB_MOTION_SINGLE_MULTISWITCH_NODE: str = ""

    # ── Wan2.2 Animate 模式（AI App 工作流 2079191464839847938）──
    # 节点：57=主参考图, 413=动作源视频, 935=fps, 933=宽, 934=高, 939=正向prompt, 940=负向prompt
    # 适用：人物/产品动作迁移，30s 长视频稳定，手指发丝细节好；需 plus 实例
    RUNNINGHUB_MOTION_WAN22_WORKFLOW_ID: str = "2079191464839847938"
    RUNNINGHUB_MOTION_WAN22_IS_AI_APP: bool = True
    RUNNINGHUB_MOTION_WAN22_IMAGE_NODE: str = "57"
    RUNNINGHUB_MOTION_WAN22_VIDEO_NODE: str = "413"
    RUNNINGHUB_MOTION_WAN22_FPS_NODE: str = "935"
    RUNNINGHUB_MOTION_WAN22_WIDTH_NODE: str = "933"
    RUNNINGHUB_MOTION_WAN22_HEIGHT_NODE: str = "934"
    RUNNINGHUB_MOTION_WAN22_POS_PROMPT_NODE: str = "939"
    RUNNINGHUB_MOTION_WAN22_NEG_PROMPT_NODE: str = "940"
    # Wan2.2 默认输出参数（9:16 竖屏）
    RUNNINGHUB_MOTION_WAN22_WIDTH: int = 720
    RUNNINGHUB_MOTION_WAN22_HEIGHT: int = 1280
    RUNNINGHUB_MOTION_WAN22_FPS: int = 24
    RUNNINGHUB_MOTION_WAN22_POS_PROMPT: str = "最佳质量"
    RUNNINGHUB_MOTION_WAN22_NEG_PROMPT: str = (
        "模糊，色调艳丽，磨皮，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，"
        "整体发灰，最差质量，低质量，JPEG压缩残留，丑陋的，残缺的，多余的手指，画得不好的手部，"
        "画得不好的脸部，畸形的，毁容的，形态畸形的肢体，手指融合，静止不动的画面，杂乱的背景，"
        "三条腿，背景人很多，倒着走"
    )
    # Wan2.2 工作流使用独立账号的 key（key 与工作流绑定，必须配套）
    RUNNINGHUB_MOTION_WAN22_API_KEY: str = ""
    # Wan2.2 工作流在国际站（.ai），不走默认国内站
    RUNNINGHUB_MOTION_WAN22_BASE_URL: str = "https://www.runninghub.ai"

    # === Wan3.0 爆款复刻（视频+图片+提示词 → 复刻新视频）===
    # 用户在 RunningHub 部署的 wan3.0-i2v / wan3.0-fl2v 工作流（ComfyUI 或 AI App）
    # 工作流需暴露：image 节点（产品图）、video 节点（爆款视频）、prompt 节点（H3 prompt）
    RUNNINGHUB_WAN3_WORKFLOW_ID: str = ""  # 用户在 RH 部署后填入工作流 ID
    # 🔴 2026-10-09 用户指令"把 runninghub 的 wan3.0 断了" ⇒ 默认 False = 拒绝提交新任务。
    #    开关只管"断不断"，管不到"要不要批准"：设成 True 之后仍然必须有人的批准条子
    #    （两道都在 wan3_client._wan3_submit_guard 这一刀上，不在网关前门——8090 那些腿不走近门）。
    #    断之前已提交的任务不受影响：query_task / poll / download 照常，可用 taskId 捞回。
    WAN3_RH_ENABLED: bool = False
    RUNNINGHUB_WAN3_IS_AI_APP: bool = False  # True=AI App / False=ComfyUI 工作流
    RUNNINGHUB_WAN3_API_KEY: str = ""  # 国际站企业 key（wan3.0 标准模型 API 专用）
    RUNNINGHUB_WAN3_BASE_URL: str = "https://www.runninghub.ai"  # 国际站（默认）/ 国内站 https://www.runninghub.cn
    RUNNINGHUB_WAN3_IMAGE_NODE: str = ""  # 产品图节点 ID（如 "57"）
    RUNNINGHUB_WAN3_VIDEO_NODE: str = ""  # 爆款视频节点 ID（如 "413"）
    RUNNINGHUB_WAN3_PROMPT_NODE: str = ""  # prompt 节点 ID（如 "939"）
    RUNNINGHUB_WAN3_FPS_NODE: str = ""  # 输出 fps 节点 ID（可选）
    RUNNINGHUB_WAN3_WIDTH_NODE: str = ""  # 输出宽度节点 ID（可选）
    RUNNINGHUB_WAN3_HEIGHT_NODE: str = ""  # 输出高度节点 ID（可选）
    RUNNINGHUB_WAN3_WIDTH: int = 720
    RUNNINGHUB_WAN3_HEIGHT: int = 1280
    RUNNINGHUB_WAN3_FPS: int = 24
    RUNNINGHUB_WAN3_INSTANCE_TYPE: str = "plus"  # wan3.0 必须 48G 显存

    # === 阿里 DashScope 官方 wan3.0 API（直连千川3.0，替代 RunningHub 中转）===
    # 文档：https://help.aliyun.com/zh/model-studio/wan3-video-generation-api-reference
    # 端点：POST https://dashscope.aliyuncs.com/api/v1/services/aigc/video-generation/video-synthesis
    # 关键 Header：X-DashScope-Async: enable
    # 8100 的 Bearer 令牌；留空=不校验（现状，仅本机 127.0.0.1）。要给 hypit/客户机调就必须设。
    MODEL_GATEWAY_TOKEN: str = ""

    DASHSCOPE_BASE_URL: str = "https://dashscope.aliyuncs.com/api/v1"
    DASHSCOPE_CHAT_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"  # OpenAI 兼容口（chat 用这个，不是上面那个）
    DASHSCOPE_API_KEY: str = ""
    DASHSCOPE_WAN3_MODEL: str = "wan3.0-video-prime"  # 高速版（wan3.0-video 标准版）
    DASHSCOPE_WAN3_RESOLUTION: str = "480P"  # 480P / 720P / 1080P
    DASHSCOPE_WAN3_RATIO: str = "16:9"  # adaptive / 16:9 / 9:16 / 1:1 / 4:3 / 3:4

    # === Wan3.0 爆款复刻 Pipeline（长视频→切分→LLM反推→N段并发生成→合并）===
    # 调用 RunningHub wan3.0 标准 API (/openapi/v2/alibaba/wan-3.0/reference-to-video)
    # 4 步工作流：MiMo切分 → LLM反推 → N段并发 → ffmpeg合并
    # 严格遵循 wan3-prompt-builder skill v3.1.0 规范
    REPLICA_WAN3_MIMO_FPS: float = 4.0              # MiMo 采样帧率
    REPLICA_WAN3_MIMO_MEDIA_RESOLUTION: str = "max"  # MiMo 媒体分辨率（合法值 default/low/medium/high/max）
    REPLICA_WAN3_MIMO_SPLIT_MODE: str = "scene_cut" # 按完整分镜切分
    REPLICA_WAN3_MAX_SEGMENTS: int = 10             # 最大段数（防超长视频切太多）
    # LLM 引擎：magpie / mimo / grok / gpt。
    #    2026-10-15 用户定"超级复刻的推理模型全部用 mimo-v2.6-pro"⇒ 默认曾是 mimo；
    # 🔴 2026-10-20 换默认成 magpie：那把小米 key 整把 429 `quota exhausted`（实测 text.complete/mimo_pro
    #    当场拒、0 分），编译/看图/分镜三条腿一起断 ⇒ 用户裁定"按 8099 的模型接法，但要把这些模型放到网关上调用"。
    #    换引擎不用改码：.env 写 REPLICA_WAN3_LLM_ENGINE=mimo（额度恢复后切回）。
    # ⚠️ 强度边界按引擎分两种，别混：magpie 这条【经 gateway.invoke】⇒ 有预扣/结算/台账（free 池记 0 分、token 照记）；
    #    mimo/grok/gpt 三条仍自己拼 OpenAI client 直连厂商（`_compiler_llm` 里如实写 ledger="off"），换它们不产生台账。
    REPLICA_WAN3_LLM_ENGINE: str = "magpie"
    # ③审查腿（Step2 Lint）单独用哪个引擎：空=跟②编译腿同一条。
    #   取值只能是 _compiler_llm 认的四个引擎名：magpie / mimo / grok / gpt。
    #   🔴 故意【不收网关 purpose 名】：`text.complete/mimo` 是经网关扣账的腿，`engine=mimo` 是直连厂商
    #      不扣账的腿 —— 同一个词两种口径就是这个字段上的假保险，所以一个字段只许一种方言。
    #   ⚠️ 显式点名的引擎挂了就是挂了：审查会如实打一行 ⚠ 并"保留 Layer1 结果"，不会悄悄换一家审。
    REPLICA_WAN3_LINT_ENGINE: str = ""
    # Step1 分镜腿用哪家：gateway=现读网关 video.analyze 那条 lane 的 base/key/model（默认 agnes-3.0-flash，
    #   实测同一种 video_url part 真吃：video_tokens=240）；mimo=回小米直连（额度恢复后切回）。
    # 🔴 这一腿不许换成 magpie 那三条 free：桥丢 video part（带视频时 prompt_tokens 一字不涨）。
    # ⚠️ 只改调用点传参：pipeline/step1_mimo_parse.py 是 BOUNDARY.md 整文件保护的那份，一个字不动。
    REPLICA_WAN3_STORYBOARD_ENGINE: str = "gateway"
    # 分镜腿用小米哪个型号（engine=mimo 时才读）：空=回落 TEXT_MODEL_MIMO_PRO。
    # 2026-10-22 用户点名 mimo-v2.6-flash ⇒ .env 写 REPLICA_WAN3_STORYBOARD_MODEL=mimo-v2.6-flash。
    REPLICA_WAN3_STORYBOARD_MODEL: str = ""
    # 看图腿（Step0 素材分析）= 一条【有序链】，每一项是网关 `video.analyze` 的一条 purpose，
    # 串形 `purpose[:试几次],…`；写一条裸名字（老写法 `mimo_pro`）= 只有一条腿的链，行为照旧。
    # 🔴 2026-10-22 夜用户按当晚实测拍的序：qoder 的 Qwen3.8-Flash 主 → opencode 的 mimo-v2.6-flash-free 第二
    #    → 小米直连 mimo-v2.6-flash 第三。同一张真产品图、同一道题的判据（结构/OCR/有没有补画面外）：
    #      magpie_qfmodel  ✅ 认出紫色旋盖 + 读对 jio + 明写"规格成分本面未见，不按常识补"｜36.1s｜pt 42→2603
    #      magpie_mimo     ✅ 结构与 OCR 都对｜37.1s
    #      mimo            ✅ 都对（小米直连、走网关所以有账）｜20.2s｜图 2401 tok｜实扣 34 分
    #      被否的两条：deepseek free 把真实存在的盖子看成"无盖子或旋盖"；dfmodel 同错 + logo 读成 jie + 374.4s
    #    ⚠️ 免批不免账，而且【预估 6 分、实扣 33–40 分/次】⇒ 熔断 120 分只够三条素材，报价一律读台账 charged。
    #    🔴 链上前三条【都不吃视频】（qfmodel/dfmodel 实测带 10.2MB 原片 pt 只涨 40 = 只有题目文字；
    #       opencode 那三条同理）⇒ 这条链只服务看图，【分镜腿】仍只能小米直连 mimo-v2.6-flash
    #       （当晚正对照：pt 13→3265、逐秒动作与画面文字全读出来了）。
    REPLICA_WAN3_ANALYZE_PURPOSE: str = "magpie_qfmodel:1,magpie_mimo:1,mimo:1"

    REPLICA_WAN3_CONCURRENCY: int = 3               # N 段并发提交数
    REPLICA_WAN3_RESOLUTION: str = "1080P"          # wan3.0 输出分辨率：默认档 = 网关那条 lane 的主档（名单唯一源 gateway._WAN30_RES_MODEL，现支持 720P/1080P；这里曾写 480P，而那条 lane 物理上没有 480p 档）
    REPLICA_WAN3_ASPECT_RATIO: str = "adaptive"     # 画幅：adaptive / 9:16 / 16:9 等
    REPLICA_WAN3_AUDIO: bool = True                 # 是否有声输出

    # === One-Shot Replica：爆款视频 → Seedance 复刻 ===
    # 流程：上传爆款视频 → MiMo 解析画面+音频 → GPT/Grok 反推 Seedance prompt → RH sparkvideo RPA 生成
    # RH sparkvideo 端点：2.0/2.0-fast/2.0-mini/2.0-multimodal 全可用，2.5 预留
    # engine 优先级："grok"（grok-4.6，默认）> "gpt"（gpt-5.6-sol，备用）
    ONESHOT_REPLICA_DEFAULT_ENGINE: str = "grok"
    # Seedance 时长选项（秒），sparkvideo-2.0 支持 4-15s
    ONESHOT_REPLICA_DURATIONS: str = "5,8,10,15"
    # === 详情图生成（产品图+产品名 → LLM 推理 N 个场景 prompt → ideogram4 文生图）===
    # ideogram4 AI App 工作流（国际站 .ai 域名），节点：205=prompt, 37=aspect_ratio(COMBO)+megapixels
    RUNNINGHUB_IDEOGRAM4_WORKFLOW_ID: str = "2074766296583065602"
    RUNNINGHUB_IDEOGRAM4_IS_AI_APP: bool = True
    RUNNINGHUB_IDEOGRAM4_API_KEY: str = ""  # 国际站企业 key
    RUNNINGHUB_IDEOGRAM4_BASE_URL: str = "https://www.runninghub.ai"  # 国际站
    RUNNINGHUB_IDEOGRAM4_PROMPT_NODE: str = "205"
    RUNNINGHUB_IDEOGRAM4_RATIO_NODE: str = "37"
    # aspect_ratio 的 COMBO fieldData（工作流节点定义，原样透传）
    RUNNINGHUB_IDEOGRAM4_RATIO_FIELD_DATA: str = (
        '["COMBO", {"default": "1:1 (Square)", "options": '
        '["1:1 (Square)", "2:3 (Portrait Photo)", "3:2 (Photo)", '
        '"3:4 (Portrait Standard)", "4:3 (Standard)", '
        '"9:16 (Portrait Widescreen)", "16:9 (Widescreen)", "21:9 (Ultrawide)"], '
        '"tooltip": "The aspect ratio for the output dimensions.", "multiselect": false}]'
    )
    RUNNINGHUB_IDEOGRAM4_MEGAPIXELS: str = "2"  # 2=200W pixels (1=100W pixels)
    RUNNINGHUB_IDEOGRAM4_INSTANCE_TYPE: str = "default"
    # 默认生成数量与上限
    DETAIL_IMAGE_DEFAULT_COUNT: int = 4
    DETAIL_IMAGE_MAX_COUNT: int = 8
    # 默认画幅（电商详情图常用竖版 3:4）
    DETAIL_IMAGE_DEFAULT_ASPECT_RATIO: str = "3:4 (Portrait Standard)"

    # 默认时长（秒）
    ONESHOT_REPLICA_DEFAULT_DURATION: str = "5"
    # 默认比例
    ONESHOT_REPLICA_DEFAULT_RATIO: str = "9:16"
    # 默认分辨率
    ONESHOT_REPLICA_DEFAULT_RESOLUTION: str = "720p"

    # === 视频去水印（RunningHub AI App：清 Sora / Seedance 的文字与图标）===
    # 2026-10-04 接入。AI App 号 2012077432525824001，节点：
    #   142=video（上传后拿到的 fileName，不是 download_url） 143=时长秒 138=帧率(16|24) 128=清理提示词
    # 🔴 与 H3 同一家：上游按 GPU 秒真扣钱，结果链接 24 小时失效 ⇒ 网关侧强制 output_path 落盘。
    RUNNINGHUB_DEWATERMARK_APP_ID: str = "2012077432525824001"
    RUNNINGHUB_DEWATERMARK_BASE_URL: str = "https://www.runninghub.ai"
    # 密钥不单独开字段：与 faceswap 同路，直接用企业 key（RUNNINGHUB_ENTERPRISE_API_KEY），
    # 单独一个空字段会让管理面板把这条报成"密钥未配置"——那是配置的形态，不是上游的事实。
    RUNNINGHUB_DEWATERMARK_VIDEO_NODE: str = "142"
    RUNNINGHUB_DEWATERMARK_DURATION_NODE: str = "143"
    RUNNINGHUB_DEWATERMARK_FPS_NODE: str = "138"
    RUNNINGHUB_DEWATERMARK_PROMPT_NODE: str = "128"
    RUNNINGHUB_DEWATERMARK_PROMPT: str = ("Remove watermarks and remove Sora text and icons, "
                                          "as well as Seedance text and icons")
    RUNNINGHUB_DEWATERMARK_FPS: str = "24"
    RUNNINGHUB_DEWATERMARK_INSTANCE_TYPE: str = "default"   # default=24G | plus=48G | ultra=84G

    # === 视频高清修复（RunningHub AI App 1990029249488801793，2026-10-05 接入）===
    # 吃【本地视频文件】（提交时上传拿 fileName），输出 4K 修复片。与 dewatermark 同一套协议：
    #   upload → run/ai-app(nodeInfoList) → query → 落盘。
    # 🔴 节点号必须从对方工作流里填进来（.env 覆盖），空 = 花钱前拦截（同 dewatermark 规矩：
    #   节点号只住 config，代码里不留字面量兜底）。
    RUNNINGHUB_ENHANCE_APP_ID: str = "1990029249488801793"
    RUNNINGHUB_ENHANCE_BASE_URL: str = "https://www.runninghub.ai"
    # 节点号来自该 App 官方 API 文档的调用示例（2026-10-05 用户提供的文档），.env 可覆盖。
    RUNNINGHUB_ENHANCE_VIDEO_NODE: str = "16"    # 上传后 fileName 进的视频输入节点（原帧率/原时长/HD 升档）
    # 可配置节点（.env 覆盖；空 = 该档位节点未填，提交前拦截或走工作流内置默认）：
    RUNNINGHUB_ENHANCE_RESOLUTION_NODE: str = "71"   # 输出分辨率档位节点：短边像素
    RUNNINGHUB_ENHANCE_RESOLUTION_FIELD: str = "value"
    RUNNINGHUB_ENHANCE_RESOLUTION_VALUE: str = "1088"  # 文档示例值（短边 1088 像素）；4K 可改 2160
    RUNNINGHUB_ENHANCE_MODEL_NODE: str = ""        # 模型/算法选择节点的 nodeId（若工作流可切）
    RUNNINGHUB_ENHANCE_MODEL_FIELD: str = "value"
    RUNNINGHUB_ENHANCE_MODEL_VALUE: str = ""
    RUNNINGHUB_ENHANCE_INSTANCE_TYPE: str = "default"   # default=24G | plus=48G | ultra=84G

    # === Seedance 版本端点映射 ===
    # key=版本代号, value=基础端点名（不含 /text-to-video 等后缀）
    # 实际调用时按是否传 image_path 自动追加 /text-to-video 或 /image-to-video
    ONESHOT_SEEDANCE_ENDPOINTS: str = (
        "2.0=rhart-video/sparkvideo-2.0,"
        "2.0-fast=rhart-video/sparkvideo-2.0-fast,"
        "2.0-mini=rhart-video/sparkvideo-2.0-mini,"
        "2.5=rhart-video/sparkvideo-2.5"
    )
    ONESHOT_SEEDANCE_DEFAULT_VERSION: str = "2.0"
    # === vinted.cam 聚合视频 API（Seedance 2.0/2.5 实际上游）===
    # 提交 POST {base}/v1/videos，轮询 GET /v1/videos/{id}，下载 GET /v1/videos/{id}/signed_url?download=1
    VINTED_VIDEO_API_BASE: str = "https://vinted.cam"
    VINTED_VIDEO_API_KEY: str = ""
    # （2026-10-08 用户点名移除）xjai.beauty 两条（wan3 / wan3-prime）与 youzan666.vip 三条
    # （wan3-youzan / wan3-youzan-prime / wan3-smart）的 WAN 3.0 字段块连同适配器一起下线。
    # === api.tianyue.xyz：wan3.0 出片腿（2026-10-16 换芯，替掉 wan3.youzan666.vip 那条）===
    # 用户 2026-10-16 定："新的api，只接着一个wan3.0到网关上，之前的wan3.0 满血，删除"
    #   ⇒ PRIME_YZ_*（youzan666 域名+Key 成对换、余额两桶那套）连同 _yz_prime_* 适配器整条删掉。
    # 契约（渠道方 API 文档 + 0 消耗只读探测核对过，实测细节见 C:\dsh\docs\tianyue-api.md）：
    #   提交 POST /v1/videos {model,prompt,duration,video_duration,aspect_ratio,resolution,
    #                          image_urls[],audio_urls[],video_urls[]} → 任务号；轮询 GET /v1/videos/{id}。
    #   素材只收公网 http/https 外链（本地文件先走 gateway upload）；没有余额口/列表口（都 404）。
    # 🔴 key 只从 .env 的 TIANYUE_API_KEY 读，源码里不留默认值（全仓明文 key 清零那条纪律）。
    # ⚠️ 计费口径未实测（文档有按次/按秒两种，模型表无价格字段）；2026-10-16 实测这把 key 余额 ¥0，
    #    但 2026-10-20 同一把 key 已能真建单（探针里有 2 发 completed）⇒ 账户已充值；
    #    单价仍然【看不见】（这家没有对账口：/v1/balance 与 GET /v1/videos 都 404）⇒ 只能去渠道后台核。
    TIANYUE_BASE_URL: str = "https://api.tianyue.xyz"
    TIANYUE_API_KEY: str = ""
    # 🔴 2026-10-20 用户定：默认【两条 wan3.0】—— 主 + 兜底 `PIX-Wan30-pro-1080p-1055-se`。
    #    兜底只在【确定没建上单】时才换（判据 = gateway.wan30_fallback_allowed，白名单里只有 submit_rejected）；
    #    拿到过任务号 / 结果未知 ⇒ 绝不换第二条，那两种情况再 POST 就是双花（这家没对账口）。
    #    ⚠️ 事故留痕：今天 .env 曾把本行覆盖成 PIX ⇒ "表单锁 ME、机器实跑 PIX"，已在 .env 改回 ME。
    # 🔴 2026-10-21 17:2x 用户定：1080P 档主模型换成 `ME-WAN3-prime-1080p-1055`，
    #    替换掉 `ME-wan30-1080p-1055`（那条当天在现网被渠道回过"上游余额不足/建单失败"，
    #    另一会话已把 720P 档换成 S-Wan30-prime-720p-1050-se）。⚠️ 新这条【我们还没跑过】⇒
    #    它的入参合法域是未实测，别照旧模型的规矩假设它能过（时长/媒体上限都未知，先小样本试）。
    # 🔴 2026-10-22 用户定（推翻 10-21 的"1080P 按段长二选一"）：**超级复刻一条片子里全部走
    #    `ME-WAN3-prime-1080p-1055`**，短段不许掉进按秒计费的 PIX —— 一条片子里两段用两个模型、
    #    两种计费口径，画面与账都会漂（用户原话"不然会乱"）。
    #    实现上不改判据、只【摘掉第二条候选】：`wan30_accepted_models()` 读这两键，
    #    兜底键留空 ⇒ 候选表只剩一条 ⇒ `wan30_pick_1080p_by_duration()` 两向都回 prime（它本来就
    #    "表里没有第二条就原样返回，不猜、不从价目里捞第三条"）。要恢复二选一：把本键填回 PIX。
    TIANYUE_WAN30_MODEL: str = "ME-WAN3-prime-1080p-1055"
    TIANYUE_WAN30_FALLBACK_MODEL: str = ""
    # 出网代理：2026-10-20 实测【这台机器直连 api.tianyue.xyz 被 reset】(WinError 10054)，走 127.0.0.1:10808 才通。
    # 🔴 不配它，超级复刻的出片腿今天一条都发不出去（与模型、与兜底都无关）。
    # 只在 tianyue 这一条腿内部建 ProxyHandler 用它 ⇒ 不动全局环境变量、不牵连别的项目；
    # 代价要认：Bearer 会经过本机代理进程（可见、可回滚：把本行清空就回到直连）。
    TIANYUE_PROXY_URL: str = "http://127.0.0.1:10808"
    # （2026-10-22 撤掉 TIANYUE_DURATION_MODE 这一键）计费口径不再是一个全局开关：
    # 一个键管两条不同档本身就是 bug —— 它为按秒那条设成 seconds 之后，按次那条也被按秒发。
    # 唯一判据换成【模型名尾巴的 -se】（`gateway.wan30_is_per_second()`：-se=按秒传真实秒数，
    # 无尾巴=按次固定传 1）。⚠️ .env 里若还留着 TIANYUE_DURATION_MODE=seconds 那一行，现在没人读它。
    # === H3 的第二条腿：本机 C:\Minimax-H3 的浏览器 RPA 工作台（8093）===
    # video.generate/h3-rpa 转调它：POST /api/generate → 轮询 /api/status/<id> → 按盘上 mp4 判成败。
    # 它是【登录态型】服务（Playwright 复用 runninghub_profile/，没有 api_key 可验），所以网关不做 HTTP 探活。
    H3_RPA_BASE_URL: str = "http://127.0.0.1:8093"
    # === 火山引擎·豆包语音「音频生成」HTTP 非流式（seed-audio）===
    # POST {base}/api/v3/tts/create，鉴权走 X-Api-Key 头（不是 Bearer）；成功回二进制音频体。
    # 文档口径=按字符计费（不是按条），所以网关这条的 unit_price 记「分/千字」。
    BYTEDANCE_AUDIO_BASE_URL: str = "https://openspeech.bytedance.com"
    BYTEDANCE_AUDIO_API_KEY: str = ""
    BYTEDANCE_AUDIO_MODEL: str = "seed-audio-1.0"

    # === 网页/CLI 型生图通道（没有 API key，靠【登录态】；网关只做统一入口 + 账本）===
    # 豆包生图 = Playwright 驱动本机 Chrome 上的豆包网页版；真码在 video-ui 里（不在本仓），
    # 所以网关用 subprocess 调它的 CLI 契约，不 import 跨仓模块。
    # ⚠️ DOUBAO_PYTHON 必须是【装了 playwright 的那个解释器】= 跑 video-ui(:8765) 的那个，
    #    不是网关自己所在的 .venv（venv 里没装 playwright，子进程一上来就 ModuleNotFound）。
    VIDEO_UI_ROOT: str = r"C:\dsh\scratch\video-ui\video-ui"
    DOUBAO_PYTHON: str = r"C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"
    DOUBAO_IMAGE_MODEL: str = "Seedream 4.5"
    # gflow = Google Flow 命令行（生图走 Imagen/Nano Banana）。它有两种传输：
    #   ui_automation = 驱动网页，gflow 自己写明【必须有头】（reCAPTCHA Enterprise 对无头直接 403）；
    #   sapisidhash   = 从 Chrome cookie DB 读 SAPISID + 纯 httpx 发请求，抓浏览器指纹那一次 Playwright
    #                   是 headless=True 硬编码 ⇒ 不再弹浏览器窗（2026-10-10 用户选这条）。
    # ⚠️ sapisidhash 上游标 experimental：Google WAF 会拒"auth 对但指纹不对"的请求，而且可能回
    #    **空媒体的假 200** —— 所以 gateway._gflow_image 的"退 0 但没落盘文件 ⇒ 判失败"这条不许拆。
    # 想回到有头老路：改成 ui_automation，或在 .env 写 GFLOW_TRANSPORT=ui_automation（不用改代码）。
    GFLOW_TRANSPORT: str = "sapisidhash"
    GFLOW_EXE: str = r"C:\Users\Administrator\.local\bin\gflow-cli.exe"
    GFLOW_IMAGE_MODEL: str = "nano2"        # nano2 | nano-pro | nano2-lite | image4 | imagen4
    GFLOW_PROJECT: str = "75cd0b95-7ffb-4df1-8d5b-688ff9e20d10"
    # MiMo Step1 调用参数（参考爆款复刻 1:1 流程）
    ONESHOT_MIMO_FPS: float = 4.0
    ONESHOT_MIMO_MEDIA_RESOLUTION: str = "max"
    ONESHOT_MIMO_SPLIT_MODE: str = "scene_cut"

    # === 无人物产品动作迁移模式（product）===
    # 适用：原视频无人物，主体为产品（地毯/手部/餐桌等），易穿模时使用
    # 策略：CLIP+关键词切片主体出现段 + 光流谷值切动作子段 + 空段保留原视频 + ffmpeg 拼接
    # 工作流：rhtv.runninghub.ai 国际站 AI App（Wan21 SCAIL2）
    MOTION_PRODUCT_BASE_URL: str = "https://rhtv.runninghub.ai"
    MOTION_PRODUCT_API_KEY: str = ""
    MOTION_PRODUCT_WORKFLOW_ID: str = "2067130016248455169"
    MOTION_PRODUCT_IS_AI_APP: bool = True
    MOTION_PRODUCT_INSTANCE_TYPE: str = "default"
    # 节点配置（Wan21 SCAIL2 工作流：24=image, 25=video, 42=width, 43=height, 48=text）
    MOTION_PRODUCT_IMAGE_NODE: str = "24"
    MOTION_PRODUCT_VIDEO_NODE: str = "25"
    MOTION_PRODUCT_WIDTH_NODE: str = "42"
    MOTION_PRODUCT_HEIGHT_NODE: str = "43"
    MOTION_PRODUCT_TEXT_NODE: str = "48"
    # 默认输出参数
    MOTION_PRODUCT_WIDTH: int = 720
    MOTION_PRODUCT_HEIGHT: int = 1280
    MOTION_PRODUCT_TEXT_PROMPT: str = "女孩在跳舞"
    MOTION_PRODUCT_FPS: int = 24
    MOTION_PRODUCT_POS_PROMPT: str = "最佳质量"
    MOTION_PRODUCT_NEG_PROMPT: str = (
        "模糊，色调艳丽，磨皮，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，"
        "整体发灰，最差质量，低质量，JPEG压缩残留，丑陋的，残缺的，多余的手指，画得不好的手部，"
        "画得不好的脸部，畸形的，毁容的，形态畸形的肢体，手指融合，静止不动的画面，杂乱的背景，"
        "三条腿，背景人很多，倒着走"
    )
    # 主体检测参数
    MOTION_PRODUCT_SAMPLE_FPS: float = 2.0      # 主体检测采样率（帧/秒）
    MOTION_PRODUCT_SCORE_THRESHOLD: float = 0.5  # 主体置信度阈值（>=视为出现）
    MOTION_PRODUCT_GAP_TOLERANCE: float = 0.5   # 容忍秒数：主体短暂消失不切（小于此秒数合并）
    # 动作边界参数
    MOTION_PRODUCT_MIN_SEGMENT: float = 0.5     # 最短动作段时长（秒）
    MOTION_PRODUCT_MAX_SEGMENT: float = 3.0     # 最长动作段时长（秒，超出强制切）
    MOTION_PRODUCT_FLOW_WINDOW: int = 5        # 光流滑窗大小（帧）
    MOTION_PRODUCT_FLOW_VALLEY_RATIO: float = 0.5  # 谷值阈值（相对该段均值的比例）
    # 拼接参数
    MOTION_PRODUCT_TRANSITION_MS: int = 100    # 段间过渡时长（毫秒，xfade）
    MOTION_PRODUCT_KEEP_ORIGINAL_FOR_EMPTY: bool = True  # 主体不出现段保留原视频
    # 关键帧选取策略：first=段首帧，flow_peak=段内光流峰值帧（手部姿态变化最大处）
    MOTION_PRODUCT_KEYFRAME_POLICY: str = "first"

    # === 手部动作预设（preset="hand" 时覆盖上面）===
    # 适用：仅手部特写、无人物出镜的精细动作场景
    # 特点：更短的动作段（避免长段穿模）、更高光流采样率、关键帧取光流峰值帧
    MOTION_PRODUCT_HAND_KEYWORD: str = "手"        # 默认主体关键词
    MOTION_PRODUCT_HAND_MIN_SEGMENT: float = 0.3   # 更短段，精细切
    MOTION_PRODUCT_HAND_MAX_SEGMENT: float = 2.0   # 更短段上限
    MOTION_PRODUCT_HAND_FLOW_WINDOW: int = 3      # 更小滑窗，捕捉快速手部动作
    MOTION_PRODUCT_HAND_FLOW_VALLEY_RATIO: float = 0.4  # 更低阈值，多切点
    MOTION_PRODUCT_HAND_DENSE_FPS: int = 12       # 更高光流采样率
    MOTION_PRODUCT_HAND_SAMPLE_FPS: float = 3.0   # 更高主体检测采样率
    MOTION_PRODUCT_HAND_SCORE_THRESHOLD: float = 0.5
    MOTION_PRODUCT_HAND_GAP_TOLERANCE: float = 0.3
    MOTION_PRODUCT_HAND_KEYFRAME_POLICY: str = "flow_peak"  # 关键帧取光流峰值帧

    # ── 共享节点 ID（两种模式相同）──
    # 422=宽度(value), 264=fps(value), 266=控制强度(value)
    # 297=lora_strength(value), 451=steps(value), 450=cfg(value), 470=模式选择(select)
    RUNNINGHUB_MOTION_WIDTH_NODE: str = "422"
    RUNNINGHUB_MOTION_FPS_NODE: str = "264"
    RUNNINGHUB_MOTION_CN_NODE: str = "266"
    RUNNINGHUB_MOTION_LORA_NODE: str = "297"
    RUNNINGHUB_MOTION_STEPS_NODE: str = "451"
    RUNNINGHUB_MOTION_CFG_NODE: str = "450"
    RUNNINGHUB_MOTION_SELECT_NODE: str = "470"
    # 默认参数值
    # cn_strength=0.5（中强度锁产品，避免新老鞋交替），width=900（长边像素），steps=15（高质量），cfg=16
    RUNNINGHUB_MOTION_CN_STRENGTH: float = 0.5
    RUNNINGHUB_MOTION_WIDTH: int = 900
    RUNNINGHUB_MOTION_STEPS: int = 15
    RUNNINGHUB_MOTION_CFG: float = 16.0
    RUNNINGHUB_MOTION_LORA_STRENGTH: float = 1.0
    RUNNINGHUB_MOTION_SELECT: int = 2
    
    # RunningHub 视频翻译工作流配置
    RUNNINGHUB_FACESWAP_WORKFLOW_ID: str = "2028374986792116225"  # DreamID-V 换人 AI 应用
    # 视频换背景：默认可测应用 ID；节点未齐时前端会提示「探测应用」
    RUNNINGHUB_BGSWAP_APP_ID: str = "2058405935890128898"  # 视频换背景（支持运镜）
    # 超级复刻「换口播」对口型 ComfyUI 工作流（用户提供）
    RUNNINGHUB_VO_LIPSYNC_WORKFLOW_ID: str = "2088573214279876610"
    RUNNINGHUB_VO_LIPSYNC_VIDEO_NODE: str = "228"
    RUNNINGHUB_VO_LIPSYNC_VIDEO_FIELD: str = "video"
    RUNNINGHUB_VO_LIPSYNC_AUDIO_NODE: str = "125"
    RUNNINGHUB_VO_LIPSYNC_AUDIO_FIELD: str = "audio"
    # 额外换背景应用 JSON 数组，覆盖/追加目录。例：
    # [{"key":"my_bg","name":"自定义换背景","app_id":"...","is_ai_app":true,
    #   "video_node":{"nodeId":"1","fieldName":"video"},
    #   "image_node":{"nodeId":"2","fieldName":"image"}}]
    RUNNINGHUB_BGSWAP_APPS_JSON: str = ""
    # 对口型工作流（LatentSync 数字人对口型，替代原 HeyGem）
    # 工作流 ID 2044675464228245506
    # 节点：228/video(原视频), 125/audio(校对音频), 343/fps, 345/width, 344/height, 342/duration, 347/desc
    RUNNINGHUB_LIPSYNC_WORKFLOW_ID: str = "2044675464228245506"
    # 对口型工作流使用专用企业级 key（与默认 key 隔离，避免额度抢占）
    RUNNINGHUB_LIPSYNC_API_KEY: str = ""
    # 对口型工作流节点配置
    RUNNINGHUB_LIPSYNC_VIDEO_NODE: str = "228"        # 视频输入节点
    RUNNINGHUB_LIPSYNC_AUDIO_NODE: str = "125"        # 音频输入节点
    RUNNINGHUB_LIPSYNC_FPS_NODE: str = "343"         # 帧率节点
    RUNNINGHUB_LIPSYNC_WIDTH_NODE: str = "345"       # 宽度节点
    RUNNINGHUB_LIPSYNC_HEIGHT_NODE: str = "344"      # 高度节点
    RUNNINGHUB_LIPSYNC_DURATION_NODE: str = "342"    # 时长节点
    RUNNINGHUB_LIPSYNC_DESC_NODE: str = "347"        # 说话状态描述节点
    RUNNINGHUB_LIPSYNC_FPS: int = 30                  # 输出帧率
    RUNNINGHUB_LIPSYNC_WIDTH: int = 720               # 输出宽度
    RUNNINGHUB_LIPSYNC_HEIGHT: int = 1280             # 输出高度
    RUNNINGHUB_LIPSYNC_DESC: str = "人物在说话"        # 说话状态描述
    RUNNINGHUB_VOICE_CLONE_APP_ID: str = "1965614643077070850"  # IndexTTS 声音克隆 AI 应用
    # H3 导演台 ComfyUI 工作流 ID（MiniMaxH3Director 节点，支持多段分镜/首尾帧/参考素材/v2v/rv2v 全模式）
    # fl2va 工作流（t2v / i2v / fl2v 使用 fl2va UNET）
    RUNNINGHUB_H3_DIRECTOR_WORKFLOW_ID: str = "2084788947984666625"
    # ref2va 工作流（r2v / v2v / rv2v 使用 ref2va UNET）
    # 默认与 fl2va 相同；如已部署独立的 ref2va 工作流，改为对应 ID
    RUNNINGHUB_H3_DIRECTOR_REF2VA_WORKFLOW_ID: str = "2084788947984666625"
    # 导演台核心节点 ID（MiniMaxH3Director 节点）
    RUNNINGHUB_H3_DIRECTOR_NODE_ID: str = "12"
    # H3 走 RunningHub 国际站（与 .env 里的企业 key 匹配）；网关路由表以此显示健康
    RUNNINGHUB_H3_BASE_URL: str = "https://www.runninghub.ai"

    # 满血API（manxueapi 网关）—— base/key 是【渠道级】的，不只 H3 用：2026-10-13 起三条视频腿共用
    # （h3-manxue / seedance-2.0-manxue / seedance-2.5-manxue，参数卡见 gateway._MANXUE_VIDEO）。
    MANXUE_H3_BASE_URL: str = "https://manxueapi.com"
    MANXUE_H3_API_KEY: str = ""
    MANXUE_H3_MODEL: str = "1ren-minimax-h3"
    # 🔴 Seedance 走 1ren-dance 这两条（对方文档：dance 名原样发进来、由 new-api 渠道映射成供应商的 Seedance）：
    #    2-0 = sd2.0（1.8 积分/条·封顶30秒）、2.5-2 = sd2.5（6 积分/条·同上），2026-10-13 公告"已恢复"。
    #    没接字面名 guanfang-seedance-2 / -2-5：价目 price=0、unit=None、无参数卡，接进来就是假选项。
    #    要 1080P+参考视频的 2.5 得另加 c-dance2.5-pro-1080（30图/10视频/10音频、1.38 积分/秒）——未接。
    MANXUE_SEEDANCE2_MODEL: str = "1ren-dance-2-0"
    MANXUE_SEEDANCE25_MODEL: str = "1ren-dance-2.5-2"
    # imgbb 图床 API（manxue 网关 images 字段要求公网URL，需用 imgbb 转换本地图片）
    IMGBB_API_KEY: str = ""

    # Replicate API（云端 SAM2 + ProPainter 去 logo）
    REPLICATE_API_TOKEN: str = ""

    # ProPainter 字幕消除配置
    PROPAINTER_DIR: str = str(BASE_DIR / "ProPainter")  # ProPainter 仓库目录
    SUBTITLE_DETECT_METHOD: str = "auto"  # "auto" | "ocr" | "bottom"
    PROPAINTER_DEVICE: str = "cuda:0"     # 推理设备

    # 今日头条二创配置
    TOUTIAO_OUTPUT_DIR: str = str(BASE_DIR / "outputs" / "toutiao")  # 头条二创产物目录
    TOUTIAO_LOGIN_TIMEOUT: int = 180  # 扫码登录超时（秒）
    TOUTIAO_HEADLESS: bool = False    # 发布过程是否无头模式（False便于扫码和调试）
    TOUTIAO_DEFAULT_IMAGE_COUNT: int = 5  # 默认配图数量
    TOUTIAO_DEFAULT_PUBLISH_TYPES: str = "article,micro"  # 默认发布类型，逗号分隔
    
    # weeksir.com 账号（Step5 生成腿 RPA 登录用）
    # 🔴 2026-10-08：原来明文写死在 pipeline/step5_video_generate.py:186-187（另散在 13 个未入库文件里）
    #    ⇒ 源码不留值，只从 .env 读；取不到时 step5 会明确报错而不是拿空串去登录。
    #    ⚠️ 那把密码已经进过对话与多处文件 ⇒ 必须轮换（轮换只改 .env，代码不用动）。
    WEEKSIR_USERNAME: str = ""
    WEEKSIR_PASSWORD: str = ""

    # 文件存储
    UPLOAD_DIR: str = str(BASE_DIR / "uploads")
    OUTPUT_DIR: str = str(BASE_DIR / "outputs")
    FRONTEND_DIR: str = str(BASE_DIR / "frontend")
    
    # 服务配置
    HOST: str = "0.0.0.0"
    PORT: int = 8090
    
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", env_file_encoding="utf-8", case_sensitive=True, extra="ignore")

settings = Settings()

# 确保目录存在
os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
os.makedirs(settings.OUTPUT_DIR, exist_ok=True)
