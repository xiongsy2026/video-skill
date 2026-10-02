"""
Gemini 内置工作流单独运行脚本。

用法：
  python run_gemini_workflow.py --check-login
  python run_gemini_workflow.py --video d:\path\input.mp4 --product "产品描述"
"""
import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path

from config import settings
from extract_json_utils import normalize_gemini_result

GEMINI_URL = "https://gemini.google.com/app"

PROMPT = """1:1爆款复刻提示词标准(15秒万能版)流程说明

流程一：

1.1、输入原视频提取分镜和提供ffmepeg进行精准抽帧（gemin3）:输入原视频+产品=新分镜脚本+分镜抽帧点json
角色设定：
你是工业级视频结构解析引擎（Video Structural Parsing Engine），只负责"视频还原 + 可执行时间轴 + 抽帧指令生成"。

你的输出必须可直接用于：
- 结构化数据存储（纯JSON）
- FFmpeg抽帧执行
- 分镜图拼接与视频复刻

禁止任何剧情推测、画面脑补或文字润色等创作行为。

====================
🚨 核心原则
====================
1. 100%还原真实视频：只描述"发生了什么"，不描述"为什么发生"或"感觉如何"。
2. 所有分镜切分必须基于"真实动作锚点"或"口播语义转折"。
3. 严格遵守JSON格式与指定字段，禁止自行增删字段或输出冗长说明。

====================
🧠 Step 1：视频解析与锚点提取
====================
在生成分镜前，后台自动判断并提取：
1. 是否有口播（有/无）
2. 切分依据（有口播看语义/无口播看镜头与动作）
3. 动作锚点（如：出现物品、举起、展示、递近、强调手势等，每个Shot必须基于一个锚点）

====================
🎬 Step 2：分镜脚本输出（核心层）
====================
去除冗余信息，输出严格的JSON数组。每个分镜（Shot）必须包含以下字段，且内容需高度概括（不超过10个字/字段，口播除外）：
- "shot": 编号 (整数)
- "场景": (如：室内 / 室外 / 街拍)
- "人物": (核心特征描述，如：白裙女性 / 仅双手)
- "画面内容": (纯客观事实，如：举起银色包包)
- "人物动作": (核心动作提取，如：食指强调 / 抚摸皮革)
- "景别": (特写 / 近景 / 中景 / 全景 / 远景)
- "音效": (特定环境音或音效，没有则填"无")
- "口播": (原句，听不清填"听不清"，无则填"无")
微动作与表情（需标注：眼神轨迹、头部微晃、重心转移等自然律动）
声音情感与语气（需详细标注：重音词汇、语速变化、停顿点、情绪形容词）
-
还需要包含统一的内容：视觉风格、拍摄参数、音效设计，动作设计和声音与情绪工程（包含：情感TTS韵律提取、真人呼吸感建模、禁画外音）！

同时识别客户上传的@产品，输出替换了新产品的分镜json
每个shot的输出维度示例：

[
  {
    "shot": 1,
    "场景": "室内",
    "人物": "白裙女性",
    "产品": "银色包包",
    "画面内容": "居中站立提包",
    "人物动作": "做"嘘"及指引手势",
    "产品动作": "单手自然提包，包位于身体前侧，作为待展示主体",
    "景别": "中景",
    "构图": "中心构图",
    "运镜": "固定机位",
    "音效": "无",
    "口播": "姐妹们今天挖到宝了",
    "声音语气": {
      "语速": "中速偏快",
      "语调": "开场上扬，像私下分享",
      "重音": ""挖到宝了"重读",
      "停顿": ""姐妹们"后可有轻微停顿",
      "情绪": "兴奋、神秘、分享感",
      "呼吸感": "自然轻呼吸，不要播音腔"
    },
    "面部微表情": {
      "眼神方向": "直视镜头",
      "眉眼变化": "轻微挑眉，带一点吸引注意力的神态",
      "嘴角状态": "自然带笑",
      "表情说明": "先神秘，再转入分享感"
    }
  }
]

====================
⚙️ Step 3：FFmpeg执行层（独立时间轴）
====================
提取每个Shot最具代表性的一帧，生成用于 `ffmpeg -ss` 的精确时间点（秒），输出为独立JSON数组：
[
  { "shot": 1, "time": 0.83 },
  { "shot": 2, "time": 2.10 }
]

====================
🖼 Step 4：分镜图拼接规则
====================
基于Shot总数输出拼图布局建议（不生成图片）：
- 1~5个Shot → 单行排列或2x3
- 6~8个Shot → 4×2
- 9~12个Shot → 3×4 / 4×3
规则：只允许说明Shot编号的排列方式，禁止其他解释。

====================
📦 输出结构要求
====================
必须严格按照以下顺序输出结果：
【一】解析依据（简述口播情况与主要切分锚点，50字以内）
【二】15秒分镜脚本（输出Step 2的JSON代码块）
【三】FFmpeg时间轴（输出Step 3的JSON代码块）
【四】分镜拼图规则（简述Step 4布局规则）

产品替换信息：
__PRODUCT__
"""


def ensure_outputs_dir() -> Path:
    out_dir = Path(settings.OUTPUT_DIR) / "gemini"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


async def launch_context(headless: bool):
    # 惰性导入：playwright 只有真跑 Gemini 浏览器工作流才需要。放在模块顶层会让
    # `import backend.services.gemini_client`（经 services/__init__）连带失败，
    # 进而把不需要浏览器的 video.generate/omni-t2v 也一起打死（8100 的 .venv 没装 playwright）。
    from playwright.async_api import async_playwright
    playwright = await async_playwright().start()
    launch_kwargs = {
        "user_data_dir": settings.GEMINI_PROFILE_DIR,
        "headless": headless,
        "viewport": {"width": 1440, "height": 1000},
        "locale": "zh-CN",
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--start-maximized",
        ],
    }
    chrome = (settings.CHROME_PATH or "").strip()
    if chrome and Path(chrome).exists():
        launch_kwargs["executable_path"] = chrome
    context = await playwright.chromium.launch_persistent_context(**launch_kwargs)
    return playwright, context


async def open_gemini(page):
    try:
        await page.goto(GEMINI_URL, wait_until="domcontentloaded", timeout=60000)
    except Exception as exc:
        if "ERR_ABORTED" not in str(exc):
            raise
    await page.wait_for_timeout(8000)
    title = await page.title()
    url = page.url
    try:
        body = await page.locator("body").inner_text(timeout=15000)
    except Exception:
        body = ""
    return title, url, body


def looks_logged_out(url: str, body: str) -> bool:
    text = body.lower()
    return (
        "signin" in url.lower()
        or "accounts.google" in url.lower()
        or "登录" in body
        or "sign in" in text
        or "此浏览器或应用可能不安全" in body
    )


async def check_login(headless: bool = False) -> dict:
    playwright, context = await launch_context(headless=headless)
    try:
        page = context.pages[0] if context.pages else await context.new_page()
        title, url, body = await open_gemini(page)
        status = {
            "title": title,
            "url": url,
            "logged_in": not looks_logged_out(url, body),
            "body_preview": body[:500],
        }
        return status
    finally:
        await context.close()
        await playwright.stop()


async def click_first(page, selectors, timeout=5000):
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            await locator.wait_for(state="visible", timeout=timeout)
            await locator.click()
            return selector
        except Exception:
            continue
    raise RuntimeError(f"没有找到可点击元素: {selectors}")


async def start_new_chat(page):
    selectors = [
        "button[aria-label='发起新对话']",
        "button[aria-label*='新对话']",
        "button:has-text('发起新对话')",
        "button:has-text('新建聊天')",
    ]
    try:
        used = await click_first(page, selectors, timeout=5000)
        await page.wait_for_timeout(5000)
        return used
    except Exception:
        return "current-chat"


async def fill_prompt(page, prompt: str):
    selectors = [
        "div[aria-label='为 Gemini 输入提示'][role='textbox']",
        "rich-textarea div[contenteditable='true']",
        "div[contenteditable='true']",
        "textarea",
        "[role='textbox']",
    ]
    for selector in selectors:
        try:
            box = page.locator(selector).last
            await box.wait_for(state="visible", timeout=8000)
            await box.click()
            await page.keyboard.insert_text(prompt)
            return selector
        except Exception:
            continue
    raise RuntimeError("没有找到 Gemini 输入框")


async def upload_video(page, video_path: str):
    path = Path(video_path)
    if not path.exists():
        raise FileNotFoundError(f"视频文件不存在: {video_path}")

    # Gemini 当前中文界面：先点“上传和工具”，再通过隐藏本地上传按钮触发 filechooser。
    try:
        async with page.expect_file_chooser(timeout=10000) as fc_info:
            await click_first(page, [
                "button[aria-label='上传和工具']",
                "button[aria-label*='上传和工具']",
                "button[aria-label*='上传']",
                "button[aria-label*='工具']",
            ], timeout=8000)
            await page.wait_for_timeout(1200)
            hidden_buttons = [
                "button.hidden-local-upload-button",
                "button.hidden-local-file-upload-button",
                "button.hidden-local-file-image-selector-button",
                "button:has-text('上传文件')",
                "button:has-text('从设备上传')",
            ]
            await click_first(page, hidden_buttons, timeout=3000)
        chooser = await fc_info.value
        await chooser.set_files(str(path))
        await page.wait_for_timeout(20000)
        return "filechooser:hidden-local-upload-button"
    except Exception:
        pass

    file_inputs = page.locator("input[type='file']")
    count = await file_inputs.count()
    if count > 0:
        await file_inputs.first.set_input_files(str(path))
        await page.wait_for_timeout(20000)
        return "input[type=file]"

    raise RuntimeError("没有找到 Gemini 上传入口")


async def send_prompt(page):
    await page.wait_for_timeout(2000)
    input_box = page.locator("div[aria-label='为 Gemini 输入提示'][role='textbox']").last
    selectors = [
        "button[aria-label='发送']",
        "button[aria-label*='Send']",
        "button[aria-label*='发送']",
        "button[aria-label*='提交']",
        "button:has([data-testid='send-button'])",
        "button:has-text('发送')",
        "button:has-text('提交')",
    ]
    for selector in selectors:
        try:
            button = page.locator(selector).last
            await button.wait_for(state="visible", timeout=15000)
            await button.click(force=True)
            await page.wait_for_timeout(2500)
            try:
                text = await input_box.inner_text(timeout=3000)
            except Exception:
                text = ""
            if text.strip() in ["", "\n"]:
                return selector
        except Exception:
            continue

    clicked = await page.evaluate("""
    () => {
      const buttons = [...document.querySelectorAll('button')];
      const btn = buttons.reverse().find(b => {
        const label = (b.getAttribute('aria-label') || '').trim();
        return (label === '发送' || label.includes('发送') || label === '提交') && !b.disabled;
      });
      if (!btn) return false;
      btn.click();
      return true;
    }
    """)
    if clicked:
        await page.wait_for_timeout(2500)
        return "js:button[aria-label*=发送]"

    raise RuntimeError("没有找到可用的 Gemini 发送按钮")


async def wait_generation_done(page, timeout_ms: int = 600000):
    start = datetime.now()
    stable_rounds = 0
    last_text = ""

    while (datetime.now() - start).total_seconds() * 1000 < timeout_ms:
        await page.wait_for_timeout(3000)
        body = await page.locator("body").inner_text(timeout=10000)
        if body == last_text:
            stable_rounds += 1
        else:
            stable_rounds = 0
            last_text = body

        lower = body.lower()
        busy = any(token in lower for token in [
            "generating",
            "analyzing",
            "uploading",
            "正在生成",
            "正在分析",
            "正在上传",
            "停止生成",
            "stop generating",
        ])
        has_answer = "【一】" in body and ("```" in body or "【三】" in body)
        if stable_rounds >= 4 and not busy and has_answer:
            return body

    raise TimeoutError("等待 Gemini 生成超时")


async def analyze_video(video_path: str, product: str, headless: bool = False) -> dict:
    playwright, context = await launch_context(headless=headless)
    try:
        page = context.pages[0] if context.pages else await context.new_page()
        title, url, body = await open_gemini(page)
        if looks_logged_out(url, body):
            raise RuntimeError("Gemini 登录态无效。请先运行 python gemini_login.py 用系统 Chrome 登录。")

        chat_selector = await start_new_chat(page)
        uploaded_by = await upload_video(page, video_path)
        prompt = PROMPT.replace("__PRODUCT__", product or "无")
        used_input = await fill_prompt(page, prompt)
        sent_by = await send_prompt(page)
        response_text = await wait_generation_done(page)

        out_dir = ensure_outputs_dir()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        raw_path = out_dir / f"gemini_response_{stamp}.txt"
        shots_path = out_dir / f"storyboard_{stamp}.json"
        timeline_path = out_dir / f"timeline_{stamp}.json"
        raw_path.write_text(response_text, encoding="utf-8")

        parsed = normalize_gemini_result(response_text)
        # 【二】分镜脚本JSON（独立文件）
        shots_path.write_text(
            json.dumps(parsed["shots"], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # 【三】FFmpeg关键帧时间轴JSON（独立文件）
        timeline_path.write_text(
            json.dumps(parsed["timeline"], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        result = {
            "success": True,
            "raw_path": str(raw_path),
            "shots_path": str(shots_path),
            "timeline_path": str(timeline_path),
            "chat_selector": chat_selector,
            "uploaded_by": uploaded_by,
            "input_selector": used_input,
            "send_selector": sent_by,
            "shots_count": parsed["shots_count"],
            "timeline_count": parsed["timeline_count"],
        }
        return result
    finally:
        await context.close()
        await playwright.stop()


async def main():
    parser = argparse.ArgumentParser(description="Gemini 内置视频解析工作流")
    parser.add_argument("--check-login", action="store_true", help="只检查 Gemini 登录态")
    parser.add_argument("--video", help="待解析视频路径")
    parser.add_argument("--product", default="", help="产品描述或替换产品信息")
    parser.add_argument("--headless", action="store_true", help="无头模式运行")
    args = parser.parse_args()

    if args.check_login:
        result = await check_login(headless=args.headless)
    elif args.video:
        result = await analyze_video(args.video, args.product, headless=args.headless)
    else:
        raise SystemExit("请传入 --check-login 或 --video")

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
