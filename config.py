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
    TEXT_MODEL_GPT61: str = "gpt-6.1-sol"                # hub.nocannobb OK（4.0s·28 tok）必须带浏览器 UA 才过 Cloudflare
    TEXT_MODEL_QWEN38MAX: str = "qwen3.8-max"            # dashscope compatible-mode OK（1.2s·97 tok）
    TEXT_MODEL_MIMO_PRO: str = "mimo-v2.6-pro"           # mimo OK（6.1s·232 tok）上一题样本最有人味
    TEXT_MODEL_DEEPSEEKV4: str = "deepseek-v4-pro"       # zexapi/manxue BUDGET：900 预算返回空，需 ≥2000
    
    # API 配置
    # 切到 hub.nocannobb.com 中转（codex.theyouyi.site 网关 60s 超时会 504）
    GPT55_BASE_URL: str = "https://hub.nocannobb.com/v1"
    GPT55_API_KEY: str = ""

    # OpenAI 兼容 API（视频翻译用：ASR Whisper + GPT翻译）
    # 切到 hub.nocannobb.com 中转（codex.theyouyi.site 网关 60s 超时会 504）
    OPENAI_API_KEY: str = ""
    OPENAI_BASE_URL: str = "https://hub.nocannobb.com/v1"
    OPENAI_MODEL: str = "gpt-5.6-sol"
    
    IMAGE2_BASE_URL: str = "https://image1.vibelearning.top/v1"
    IMAGE2_API_KEY: str = ""

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
    # 注：b64d2b434d58401c8bbaf8b9260f34fd 是旧 key（部分工作流返回 414）
    # 新消费级 key 可访问更多工作流
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
    REPLICA_WAN3_MIMO_MEDIA_RESOLUTION: str = "max"  # MiMo 媒体分辨率
    REPLICA_WAN3_MIMO_SPLIT_MODE: str = "scene_cut" # 按完整分镜切分
    REPLICA_WAN3_MAX_SEGMENTS: int = 10             # 最大段数（防超长视频切太多）
    REPLICA_WAN3_LLM_ENGINE: str = "gpt"             # LLM 引擎：gpt / grok（Grok 网关 524 时改走 GPT）
    REPLICA_WAN3_CONCURRENCY: int = 3               # N 段并发提交数
    REPLICA_WAN3_RESOLUTION: str = "480P"           # wan3.0 输出分辨率（480P/720P/1080P）
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

    # 1人漫剧 H3（manxueapi 网关，OpenAI 兼容协议）
    MANXUE_H3_BASE_URL: str = "https://manxueapi.com"
    MANXUE_H3_API_KEY: str = ""
    MANXUE_H3_MODEL: str = "1ren-minimax-h3"
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
