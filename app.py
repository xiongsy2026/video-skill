"""
爆款复刻工作流 - FastAPI Web 服务
提供: 文件上传 API + 工作流执行 API + 状态轮询 + 视频回传
"""
import asyncio
import json
import uuid
import subprocess
import sys
import os
import time
import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
import httpx

from config import settings


class _SafeTextStream:
    """stdout/stderr 安全写入包装（2026-10-09 事故：TVC Step3 RPA 里一条 `✓`(U+2713) print
    在 GBK 控制台抛 UnicodeEncodeError，被外层 except 接住 ⇒ Phase2 直接标 failed）。

    纪律同 echo()：按【输出流自己的编码】先试编码、失败再 errors='replace'。
    ⚠️ 绝不用 `sys.stdout.reconfigure`（闸里已钉死）；pythonw 下底层流可能为 None ⇒ 静默丢掉。
    """

    def __init__(self, stream):
        self._stream = stream

    def write(self, s):
        try:
            stream = self._stream
            if stream is None:
                return 0
            enc = getattr(stream, "encoding", None) or "ascii"
            try:
                s.encode(enc)
            except (UnicodeEncodeError, LookupError):
                s = s.encode(enc, "replace").decode(enc, "replace")
            return stream.write(s)
        except Exception:
            return 0

    def flush(self):
        try:
            if self._stream is not None:
                self._stream.flush()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._stream, name)


if sys.stdout is not None:
    sys.stdout = _SafeTextStream(sys.stdout)
if sys.stderr is not None:
    sys.stderr = _SafeTextStream(sys.stderr)

# ── 全局任务状态 ──
tasks: dict = {}

# ── 文件上传目录 ──
UPLOAD_DIR = Path(settings.UPLOAD_DIR)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# ── 输出目录 ──
OUTPUT_DIR = Path(settings.OUTPUT_DIR)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def echo(msg="", err=False):
    """🔴 把【动态文本】写到 stdout/stderr 的唯一出口（2026-10-06 事故：8090 控制台是 GBK，
    子进程回传的日志里一个 `✓`(U+2713) 就让 `print(f"[SR-...] {msg}")` 抛 UnicodeEncodeError，
    被外层 except 接住 ⇒ **已经把 Phase1 跑完的任务被标成 failed**，钱花了、产物在盘上、状态却是死的。

    纪律：按【输出流自己的编码】先试编码、失败再 errors='replace'。
    ⚠️ 绝不用 `sys.stdout.reconfigure`：那条路实测会造"零输出 + 退出码 0"的静默假绿（闸里已钉死这条）。
    ⚠️ pythonw（无控制台）下 sys.stdout 可能是 None ⇒ 静默丢掉，绝不能让它把业务流程带崩。
    """
    try:
        stream = sys.stderr if err else sys.stdout
        if stream is None:
            return
        s = str(msg)
        enc = getattr(stream, "encoding", None) or "ascii"
        try:
            s.encode(enc)
        except (UnicodeEncodeError, LookupError):
            s = s.encode(enc, "replace").decode(enc, "replace")
        stream.write(s + "\n")
        stream.flush()
    except Exception:
        pass                      # 日志写不出去 = 只是看不见，不是任务坏了


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("=" * 60)
    print("爆款复刻工作流 - Web 服务启动")
    print(f"上传目录: {UPLOAD_DIR}")
    print(f"输出目录: {OUTPUT_DIR}")
    try:
        from db import init_db
        init_db()
    except Exception as e:
        print(f"历史数据库初始化失败: {e}")
    # 启动时恢复卡住的任务（INIT/RUNNING → FAILED + failed_step）
    try:
        from backend.services import task_store
        task_store.init_db()
        n = task_store.recover_running()
        if n > 0:
            print(f"[!] 恢复 {n} 个中断任务（已标记 FAILED，可断点续跑）")
    except Exception as e:
        print(f"[!] recover_running 失败: {e}")
    try:
        recovered_vt = _recover_vt_tasks_on_startup()
        if recovered_vt:
            print(f"恢复 {recovered_vt} 个视频翻译中断任务，可从成功步骤继续")
    except Exception as e:
        print(f"视频翻译任务恢复失败: {e}")
    # wan3.0 直出工作台的自动回传：扫 outputs/wan30_studio/*/wan3_job.json，
    # 凡是【有上游任务号、盘上没成片】的自己 GET 回来（只 GET，绝不 POST ⇒ 不可能双花）。
    # 放在启动这一格是故意的：任务号是提交那一刻就落盘的，人不需要记得也没法贴第二次。
    try:
        from backend.services import wan30_studio as _w30s
        _w30s.start_reconciler()
    except Exception as e:
        print(f"wan30-studio 自动回传启动失败: {type(e).__name__}: {e}")
    print("=" * 60)
    yield


app = FastAPI(title="爆款复刻工作流", lifespan=lifespan)

# ── 花钱批准闸的接入口（2026-10-06 用户定："只要是生成视频相关的，必须算账我确认"）──────
# 8090 的生成腿【直连厂商、不经 gateway.invoke()】⇒ 网关里那道闸盖不到这里，所以在 handler 入口调同一个守卫。
# purpose 给实路由（单价从价目表读）；还没核实清的就给 "*" ⇒ 只认人签的通吃条子、按该能力上限报（宁可高报不猜低）。
def _spend_or_423(capability: str, purpose: str = "*", label: str = "") -> None:
    from backend import gateway as _gw

    try:
        _gw.spend_guard(capability, purpose, "local", 1, label)
    except _gw.SpendApprovalRequired as _e:
        raise HTTPException(423, str(_e))


app.mount("/outputs", StaticFiles(directory=str(OUTPUT_DIR)), name="outputs")
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")

# 静态资源目录（参考图等）
STATIC_DIR = Path(settings.FRONTEND_DIR) / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

from backend.services.hypit_workspace import router as hypit_router
app.include_router(hypit_router)

# wan3.0 直出工作台（/wan30-studio + /api/wan30-studio/*）：花钱那一刀仍走 gateway.invoke，
# 本模块只做页面、素材换直链、报价与批准窗，不另造第二套判据（见该文件头注）。
from backend.services.wan30_studio import router as wan30_studio_router
app.include_router(wan30_studio_router)

# 统一模型网关的 HTTP 入口不在这里：它由常驻的 backend/model_admin.py(:8100) 提供
# （管理面 + 调用面同源）。8090 只做流水线任务服务器。


# ─────────────────────────────────────
# 页面路由
# ─────────────────────────────────────

@app.get("/api/health/models")
async def health_check_models():
    """检测各 LLM / 图片 / 视频模型 API 的可用性。

    并发检测所有上游 API，5s 超时，返回状态列表。
    用于首页顶部指示牌，快速定位哪个 API 出了故障。
    """
    import httpx
    import time as _time

    async def _check_openai_compat(name: str, kind: str, base_url: str, api_key: str, model: str = "") -> dict:
        """检测 OpenAI 兼容接口（GET /models）"""
        t0 = _time.time()
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                resp = await c.get(
                    f"{base_url.rstrip('/')}/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                latency = int((_time.time() - t0) * 1000)
                if resp.status_code == 200:
                    return {"name": name, "kind": kind, "model": model,
                            "status": "ok", "latency_ms": latency, "error": ""}
                # 401/403 = key 问题，但 API 可达
                if resp.status_code in (401, 403):
                    return {"name": name, "kind": kind, "model": model,
                            "status": "degraded", "latency_ms": latency,
                            "error": f"HTTP {resp.status_code}（鉴权异常，但 API 可达）"}
                return {"name": name, "kind": kind, "model": model,
                        "status": "degraded", "latency_ms": latency,
                        "error": f"HTTP {resp.status_code}"}
        except httpx.TimeoutException:
            return {"name": name, "kind": kind, "model": model,
                    "status": "down", "latency_ms": 5000, "error": "超时（5s）"}
        except Exception as e:
            return {"name": name, "kind": kind, "model": model,
                    "status": "down", "latency_ms": int((_time.time() - t0) * 1000),
                    "error": str(e)[:100]}

    async def _check_runninghub(name: str) -> dict:
        """检测 RunningHub（GET 根路径 ping）"""
        t0 = _time.time()
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                resp = await c.get("https://www.runninghub.cn")
                latency = int((_time.time() - t0) * 1000)
                if resp.status_code < 500:
                    return {"name": name, "kind": "video", "model": "RunningHub",
                            "status": "ok", "latency_ms": latency, "error": ""}
                return {"name": name, "kind": "video", "model": "RunningHub",
                        "status": "down", "latency_ms": latency,
                        "error": f"HTTP {resp.status_code}"}
        except httpx.TimeoutException:
            return {"name": name, "kind": "video", "model": "RunningHub",
                    "status": "down", "latency_ms": 5000, "error": "超时（5s）"}
        except Exception as e:
            return {"name": name, "kind": "video", "model": "RunningHub",
                    "status": "down", "latency_ms": int((_time.time() - t0) * 1000),
                    "error": str(e)[:100]}

    async def _check_vinted_models() -> list:
        """检测 vinted.cam 整块模型牌（Seedance 2.5 / 2.0 / 2.0 Fast / 2.0 mini）。

        一次 GET /v1/balance 验证 key+余额，一次 GET /v1/catalog 读取全部模型，
        每个模型出一个 chip（与 Agnes 同模式）。
        """
        import os as _os
        t0 = _time.time()
        base_url = (_os.environ.get("VINTED_VIDEO_API_BASE")
                    or getattr(settings, "VINTED_VIDEO_API_BASE", "") or "https://vinted.cam")
        api_key = (_os.environ.get("VINTED_VIDEO_API_KEY")
                   or getattr(settings, "VINTED_VIDEO_API_KEY", ""))

        def _chip(name: str, model: str, status: str, latency: int, error: str, extra: str = "") -> dict:
            return {"name": name, "kind": "video", "model": model,
                    "status": status, "latency_ms": latency, "error": error, "extra": extra}

        # 整块牌的模型清单（名称按展示顺序）
        pairs = [
            ("Seedance 2.5", "seedance2.5"),
            ("Seedance 2.0", "seedance2.0"),
            ("Seedance 2.0 Fast", "seedance2.0fast"),
            ("Seedance 2.0 mini", "seedance2.0mini"),
        ]
        if not api_key:
            return [_chip(n, m, "down", 0, "VINTED_VIDEO_API_KEY 未配置") for n, m in pairs]
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                headers = {"Authorization": f"Bearer {api_key}", "User-Agent": "vinted-video-client/1.0"}
                bal_resp, cat_resp = await asyncio.gather(
                    c.get(f"{base_url.rstrip('/')}/v1/balance", headers=headers),
                    c.get(f"{base_url.rstrip('/')}/v1/catalog", headers=headers),
                )
                latency = int((_time.time() - t0) * 1000)
                # 鉴权 / 余额
                if bal_resp.status_code in (401, 403):
                    err = f"HTTP {bal_resp.status_code}（API key 无效）"
                    return [_chip(n, m, "down", latency, err) for n, m in pairs]
                if bal_resp.status_code == 402:
                    return [_chip(n, m, "degraded", latency, "HTTP 402（余额不足）") for n, m in pairs]
                if bal_resp.status_code != 200:
                    err = f"balance HTTP {bal_resp.status_code}"
                    return [_chip(n, m, "degraded", latency, err) for n, m in pairs]
                balance = (bal_resp.json() or {}).get("balance", "?")
                # 目录核对每个模型是否在售
                cat_data = cat_resp.json() or {}
                catalog = cat_data.get("models") or []
                price_items = (cat_data.get("pricing") or {}).get("items") or []
                by_id = {x.get("id"): x for x in catalog}
                out = []
                for n, m in pairs:
                    info = by_id.get(m)
                    if info is None:
                        out.append(_chip(n, m, "degraded", latency, "鉴权成功，但目录中无此模型",
                                         f"余额 {balance} 积分"))
                        continue
                    durations = "/".join(f"{d}s" for d in info.get("durations", []))
                    price = ""
                    costs = sorted({x.get("credits") for x in price_items if x.get("model") == m})
                    if costs:
                        price = f" · {costs[0]} 积分/条" if len(costs) == 1 else f" · {costs[0]}-{costs[-1]} 积分/条"
                    out.append(_chip(n, m, "ok", latency, "",
                                     f"余额 {balance} 积分 · {durations}{price}"))
                return out
        except httpx.TimeoutException:
            return [_chip(n, m, "down", 5000, "超时（5s）") for n, m in pairs]
        except Exception as e:
            latency = int((_time.time() - t0) * 1000)
            return [_chip(n, m, "down", latency, str(e)[:100]) for n, m in pairs]

    async def _check_omni(name: str) -> dict:
        """检测 zexapi Omni（GET /v1/models，OpenAI 兼容协议）"""
        t0 = _time.time()
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                resp = await c.get(
                    f"{settings.OMNI_BASE_URL.rstrip('/')}/v1/models",
                    headers={"Authorization": f"Bearer {settings.OMNI_API_KEY}"},
                )
                latency = int((_time.time() - t0) * 1000)
                if resp.status_code == 200:
                    return {"name": name, "kind": "video", "model": settings.OMNI_MODEL_T2V,
                            "status": "ok", "latency_ms": latency, "error": ""}
                if resp.status_code in (401, 403):
                    return {"name": name, "kind": "video", "model": settings.OMNI_MODEL_T2V,
                            "status": "degraded", "latency_ms": latency,
                            "error": f"HTTP {resp.status_code}（鉴权异常，但 API 可达）"}
                return {"name": name, "kind": "video", "model": settings.OMNI_MODEL_T2V,
                        "status": "degraded", "latency_ms": latency,
                        "error": f"HTTP {resp.status_code}"}
        except httpx.TimeoutException:
            return {"name": name, "kind": "video", "model": settings.OMNI_MODEL_T2V,
                    "status": "down", "latency_ms": 5000, "error": "超时（5s）"}
        except Exception as e:
            return {"name": name, "kind": "video", "model": settings.OMNI_MODEL_T2V,
                    "status": "down", "latency_ms": int((_time.time() - t0) * 1000),
                    "error": str(e)[:100]}

    async def _check_agnes_models() -> list:
        """一次 GET /models，确认选定的文字 / 图片 / 视频模型。"""
        t0 = _time.time()
        pairs = [
            ("Agnes 3.0", settings.AGNES_TEXT_MODEL, "llm"),
            ("Agnes Image", settings.AGNES_IMAGE_MODEL, "image"),
            ("Agnes Video", settings.AGNES_VIDEO_FLASH_MODEL, "video"),
            ("Agnes 2.5", settings.AGNES_VIDEO_MODEL, "video"),
        ]

        def _chip(name: str, model: str, kind: str, status: str, latency: int, error: str) -> dict:
            return {"name": name, "kind": kind, "model": model,
                    "status": status, "latency_ms": latency, "error": error}

        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                resp = await c.get(
                    f"{settings.AGNES_BASE_URL.rstrip('/')}/models",
                    headers={"Authorization": f"Bearer {settings.AGNES_API_KEY}"},
                )
                latency = int((_time.time() - t0) * 1000)
                if resp.status_code == 200:
                    ids = [x.get("id") for x in (resp.json().get("data") or [])]
                    out = []
                    for name, model, kind in pairs:
                        if model in ids:
                            out.append(_chip(name, model, kind, "ok", latency, ""))
                        else:
                            out.append(_chip(name, model, kind, "degraded", latency, "鉴权成功，但列表中无此模型"))
                    return out
                if resp.status_code in (401, 403):
                    err = f"HTTP {resp.status_code}（鉴权异常）"
                    return [_chip(n, m, k, "degraded", latency, err) for n, m, k in pairs]
                if resp.status_code == 402:
                    return [_chip(n, m, k, "down", latency, "HTTP 402 欠费") for n, m, k in pairs]
                err = f"HTTP {resp.status_code}"
                return [_chip(n, m, k, "degraded", latency, err) for n, m, k in pairs]
        except httpx.TimeoutException:
            return [_chip(n, m, k, "down", 5000, "超时（5s）") for n, m, k in pairs]
        except Exception as e:
            latency = int((_time.time() - t0) * 1000)
            return [_chip(n, m, k, "down", latency, str(e)[:100]) for n, m, k in pairs]

    # 并发检测所有 API
    checks = [
        _check_openai_compat("MiMo", "llm", settings.MIMO_BASE_URL, settings.MIMO_API_KEY, settings.TEXT_MODEL_MIMO),
        _check_openai_compat("GPT", "llm", settings.OPENAI_BASE_URL, settings.OPENAI_API_KEY, settings.OPENAI_MODEL),
        _check_openai_compat("Grok", "llm", settings.SR_GROK_BASE_URL, settings.SR_GROK_API_KEY, settings.SR_GROK_MODEL),
        _check_openai_compat("Image2", "image", settings.IMAGE2_BASE_URL, settings.IMAGE2_API_KEY, "image2"),
        _check_openai_compat("H3", "video", settings.MANXUE_H3_BASE_URL, settings.MANXUE_H3_API_KEY, settings.MANXUE_H3_MODEL),
        _check_vinted_models(),
        _check_runninghub("RunningHub"),
        _check_omni("Omni"),
        _check_agnes_models(),
    ]
    raw = await asyncio.gather(*checks, return_exceptions=False)
    results = []
    for item in raw:
        if isinstance(item, list):
            results.extend(item)
        else:
            results.append(item)
    return {"success": True, "models": results}


# ==================== Framewright 工作台 ====================

class FramewrightIntakeRequest(BaseModel):
    brief: str = ""
    assets: List[str] = []
    extra: str = ""


class FramewrightCompileRequest(BaseModel):
    project_slug: str = "project"
    unit_slug: str = "unit"
    stage: str = "video_prompt"          # storyboard | keyframes | video_prompt
    target: str = "seedance_2_5"         # 视频: seedance_2_0/seedance_2_5/minimax_h3；图片: midjourney_v8_2/gpt_image_2_5
    director_mode: str = "auteur"        # auteur | apprentice | screenwriter
    materials: str = ""
    spine: str = ""
    answers: str = ""


@app.get("/api/framewright/status")
async def framewright_status():
    """Framewright 工作台状态：skill 安装、规范版本、注册路由。"""
    from backend.services import framewright_workbench as fw
    return {"success": True, **fw.get_status()}


@app.post("/api/framewright/intake")
async def framewright_intake(req: FramewrightIntakeRequest):
    """Step 1-2：Unified Director Intake（复述 + 制作解读 + 决策问题）。"""
    from backend.services import framewright_workbench as fw
    if not req.brief.strip():
        raise HTTPException(400, "请填写场景/剧本片段/视觉简述")
    return await fw.run_intake(req.brief, req.assets, req.extra)


@app.post("/api/framewright/compile")
async def framewright_compile(req: FramewrightCompileRequest):
    """Step 3-4：编译单个阶段 → 保存 .txt → 官方校验 → 交付 handoff。"""
    from backend.services import framewright_workbench as fw
    if not req.materials.strip():
        raise HTTPException(400, "请填写已批准的素材/Production Spine")
    return await fw.run_compile(
        project_slug=req.project_slug,
        unit_slug=req.unit_slug,
        stage=req.stage,
        target=req.target,
        director_mode=req.director_mode,
        materials=req.materials,
        spine=req.spine,
        answers=req.answers,
    )


@app.get("/framewright", response_class=HTMLResponse)
async def framewright_page():
    """Framewright 电影化编译工作台 - 二级页面"""
    html_path = Path(settings.FRONTEND_DIR) / "framewright.html"
    if not html_path.exists():
        raise HTTPException(404, "Framewright 工作台页面未找到")
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.get("/", response_class=HTMLResponse)
async def home():
    """首页 - 工作模式选择"""
    html_path = Path(settings.FRONTEND_DIR) / "index.html"
    if not html_path.exists():
        raise HTTPException(404, "首页未找到")
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.get("/replica", response_class=HTMLResponse)
async def replica_page():
    """爆款复刻工作流 - 二级页面"""
    html_path = Path(settings.FRONTEND_DIR) / "replica.html"
    if not html_path.exists():
        raise HTTPException(404, "爆款复刻页面未找到")
    return HTMLResponse(
        content=html_path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"},
    )

@app.get("/multi-shot", response_class=HTMLResponse)
async def multi_shot_page():
    """首尾帧无限生成 - 二级页面（复刻+原创两模式共用）"""
    html_path = Path(settings.FRONTEND_DIR) / "multi_shot.html"
    if not html_path.exists():
        raise HTTPException(404, "首尾帧无限生成页面未找到")
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.get("/toddler-chef", response_class=HTMLResponse)
async def toddler_chef_page():
    """涨粉·小孩做菜 - 二级页面"""
    html_path = Path(settings.FRONTEND_DIR) / "toddler_chef.html"
    if not html_path.exists():
        raise HTTPException(404, "小孩做菜页面未找到")
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.get("/toutiao", response_class=HTMLResponse)
async def toutiao_page():
    """今日头条二创 - 二级页面"""
    html_path = Path(settings.FRONTEND_DIR) / "toutiao.html"
    if not html_path.exists():
        raise HTTPException(404, "今日头条页面未找到")
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.get("/womenswear", response_class=HTMLResponse)
async def womenswear_page():
    """女装带货一体化工作台（商品链接+视频链接 → 最终视频）"""
    html_path = Path(settings.FRONTEND_DIR) / "womenswear_studio.html"
    if not html_path.exists():
        raise HTTPException(404, "女装带货工作台页面未找到")
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.get("/prompt-reverse", response_class=HTMLResponse)
async def prompt_reverse_page():
    """提示词反推工作台 - 上传爆款视频，AI 反推 H3 视频生成 prompt"""
    html_path = Path(settings.FRONTEND_DIR) / "prompt_reverse.html"
    if not html_path.exists():
        raise HTTPException(404, "提示词反推页面未找到")
    return HTMLResponse(
        content=html_path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"},
    )


@app.get("/replica-wan3", response_class=HTMLResponse)
async def replica_wan3_page():
    """爆款复刻 wan3.0 工作台 - 长视频+多图+多选框 → 切分+反推+并发生成+合并"""
    html_path = Path(settings.FRONTEND_DIR) / "replica_wan3.html"
    if not html_path.exists():
        raise HTTPException(404, "爆款复刻 wan3.0 页面未找到")
    return HTMLResponse(
        content=html_path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"},
    )


@app.get("/super-clone-wan3", response_class=HTMLResponse)
async def super_clone_wan3_page():
    """超级复刻 wan3.0 - 单模式「超级复刻」（不限时长，>15s 按 15s 切段拼接）"""
    html_path = Path(settings.FRONTEND_DIR) / "super_clone_wan3.html"
    if not html_path.exists():
        raise HTTPException(404, "超级复刻 wan3.0 页面未找到")
    return HTMLResponse(
        content=html_path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"},
    )


@app.get("/agnes-flash", response_class=HTMLResponse)
async def agnes_flash_page():
    """Agnes Video 2.5 Flash 工作台（文生/图参考，不支持参考视频）"""
    html_path = Path(settings.FRONTEND_DIR) / "agnes_flash.html"
    if not html_path.exists():
        raise HTTPException(404, "Agnes Flash 页面未找到")
    return HTMLResponse(
        content=html_path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"},
    )


@app.get("/agnes-image", response_class=HTMLResponse)
async def agnes_image_page():
    """Agnes Image 2.5 Flash 工作台（文生图 / 图生图）"""
    html_path = Path(settings.FRONTEND_DIR) / "agnes_image.html"
    if not html_path.exists():
        raise HTTPException(404, "Agnes Image 页面未找到")
    return HTMLResponse(
        content=html_path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"},
    )


@app.get("/agnes-25", response_class=HTMLResponse)
async def agnes_25_page():
    """Agnes Video 2.5 正式版工作台（可传 1 条参考视频）"""
    html_path = Path(settings.FRONTEND_DIR) / "agnes_25.html"
    if not html_path.exists():
        raise HTTPException(404, "Agnes 2.5 页面未找到")
    return HTMLResponse(
        content=html_path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"},
    )


@app.get("/one-shot", response_class=HTMLResponse)
async def one_shot_page():
    """爆款视频复刻 - MiMo 解析 → LLM 反推 → Seedance 生成"""
    html_path = Path(settings.FRONTEND_DIR) / "one_shot.html"
    if not html_path.exists():
        raise HTTPException(404, "爆款复刻页面未找到")
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


# ─────────────────────────────────────
# 一句话生视频 API (RunningHub)
# ─────────────────────────────────────

# 任务存储: { task_id: { status, prompt, model, ... } }
oneshot_tasks: dict = {}

# 8 个精选视频模型
ONESHOT_MODELS = {
    "1": {"name": "全能视频V3.1 Fast", "t2v": "rhart-video-v3.1-fast/text-to-video", "i2v": "rhart-video-v3.1-fast/image-to-video", "durations": ["8"], "default": "8"},
    "2": {"name": "全能视频X", "t2v": "rhart-video-g/text-to-video", "i2v": "rhart-video-g/image-to-video", "durations": ["5", "10"], "default": "5"},
    "3": {"name": "可灵 v3.0 Pro", "t2v": "kling-v3.0-pro/text-to-video", "i2v": "kling-v3.0-pro/image-to-video", "durations": ["5", "10"], "default": "5"},
    "4": {"name": "全能视频V3.1 Pro", "t2v": "rhart-video-v3.1-pro/text-to-video", "i2v": "rhart-video-v3.1-pro/image-to-video", "durations": ["8"], "default": "8"},
    "5": {"name": "Vidu Q3 Pro", "t2v": "vidu/text-to-video-q3-pro", "i2v": "vidu/image-to-video-q3-pro", "durations": ["5", "8"], "default": "5"},
    "6": {"name": "全能视频S", "t2v": "rhart-video-s/text-to-video", "i2v": "rhart-video-s/image-to-video", "durations": ["5", "10"], "default": "5"},
    "7": {"name": "海螺 Hailuo", "t2v": "minimax/hailuo-02/t2v-pro", "i2v": "minimax/hailuo-2.3-fast/image-to-video", "durations": ["6", "10"], "default": "6"},
    "8": {"name": "Seedance 2.0", "t2v": "rhart-video/sparkvideo-2.0/text-to-video", "i2v": "rhart-video/sparkvideo-2.0/image-to-video", "durations": ["4", "5", "6", "7", "8", "9", "10", "11", "12", "13", "14", "15"], "default": "5"},
}


class OneShotSubmitRequest(BaseModel):
    prompt: str
    model_id: str = "1"  # 1-8
    aspect_ratio: str = "16:9"  # 16:9 / 9:16 / 1:1
    duration: str = ""  # 留空用模型默认
    resolution: str = ""  # 留空用模型默认，部分模型支持 720p/1080p/4k
    image_path: Optional[str] = None  # 本地路径或 URL，若有则走 i2v


def _rh_resolve_api_key():
    """从环境变量或 openclaw.json 解析 RunningHub API Key"""
    key = os.environ.get("RUNNINGHUB_API_KEY", "").strip()
    if key:
        return key
    cfg_path = Path.home() / ".openclaw" / "openclaw.json"
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            entry = cfg.get("skills", {}).get("entries", {}).get("runninghub", {})
            v = entry.get("apiKey")
            if isinstance(v, str) and v.strip():
                return v.strip()
        except Exception:
            pass
    return None


@app.get("/api/one-shot/models")
async def oneshot_models():
    """返回 8 个模型清单（前端菜单用）"""
    return {"models": ONESHOT_MODELS}


@app.post("/api/one-shot/submit")
async def oneshot_submit(req: OneShotSubmitRequest):
    """提交一句话生视频任务"""
    if not req.prompt.strip():
        raise HTTPException(400, "prompt 不能为空")
    if req.model_id not in ONESHOT_MODELS:
        raise HTTPException(400, f"model_id 必须是 1-8，收到: {req.model_id}")

    api_key = _rh_resolve_api_key()
    if not api_key:
        raise HTTPException(500, "未配置 RUNNINGHUB_API_KEY，请先在 .env 或 ~/.openclaw/openclaw.json 中设置")

    model = ONESHOT_MODELS[req.model_id]
    is_i2v = bool(req.image_path)
    endpoint_name = model["i2v"] if is_i2v else model["t2v"]

    # 调用技能脚本
    rh_script = Path(__file__).parent / "skills" / "runninghub" / "scripts" / "runninghub.py"
    if not rh_script.exists():
        raise HTTPException(500, f"未找到 RunningHub 脚本: {rh_script}")

    # 导入脚本模块（不走 subprocess，方便复用函数）
    import importlib.util
    spec = importlib.util.spec_from_file_location("rh_runner", str(rh_script))
    rh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rh)

    endpoint_def = rh.find_endpoint(endpoint_name)
    if not endpoint_def:
        raise HTTPException(500, f"端点不存在: {endpoint_name}")

    # 构造 payload（直接构造，不走 build_payload 避开 argparse）
    payload = {}
    # prompt
    prompt_key = "prompt"
    for p in endpoint_def.get("params", []):
        if p.get("key") in ("prompt", "text"):
            prompt_key = p["key"]
            break
    payload[prompt_key] = req.prompt

    # 媒体输入
    if is_i2v:
        image_params = [p for p in endpoint_def.get("params", []) if p.get("type") == "IMAGE"]
        if not image_params:
            raise HTTPException(400, f"该模型不支持图生视频: {model['name']}")
        try:
            resolved = rh.resolve_media(api_key, req.image_path, force_upload=True)
            pk = image_params[0]["key"]
            if image_params[0].get("multiple"):
                payload[pk] = [resolved]
            else:
                payload[pk] = resolved
        except SystemExit as e:
            raise HTTPException(400, f"图片处理失败: {e}")

    # 可选参数映射（用户值优先 → endpoint default 兜底，必填字段必须有值）
    user_values = {
        "aspectRatio": req.aspect_ratio,
        "duration": req.duration,
        "resolution": req.resolution,
    }
    for p in endpoint_def.get("params", []):
        k = p.get("key")
        if k in (prompt_key,) or p.get("type") in ("IMAGE", "VIDEO", "AUDIO"):
            continue
        if k not in user_values:
            continue
        v = user_values[k]
        if not v:
            # 用户没传，用 endpoint default（避免必填字段为空被拒）
            v = p.get("default")
        if v:
            payload[k] = str(v)

    # 提交任务
    submit_url = f"{rh.BASE_URL}/{endpoint_def['endpoint']}"
    try:
        resp = rh.api_post(api_key, submit_url, payload)
    except SystemExit as e:
        raise HTTPException(500, f"提交失败: {e}")

    rh_task_id = resp.get("taskId")
    if not rh_task_id:
        raise HTTPException(500, f"未返回 taskId: {json.dumps(resp, ensure_ascii=False)}")

    # 内部 task_id
    task_id = f"oneshot_{int(time.time()*1000)}_{os.urandom(3).hex()}"
    out_dir = OUTPUT_DIR / "one_shot" / task_id
    out_dir.mkdir(parents=True, exist_ok=True)

    oneshot_tasks[task_id] = {
        "task_id": task_id,
        "rh_task_id": rh_task_id,
        "status": "pending",
        "prompt": req.prompt,
        "model_id": req.model_id,
        "model_name": model["name"],
        "endpoint": endpoint_name,
        "is_i2v": is_i2v,
        "aspect_ratio": req.aspect_ratio,
        "duration": req.duration,
        "resolution": req.resolution,
        "image_path": req.image_path,
        "out_dir": str(out_dir),
        "video_url": None,  # /outputs/... 形式
        "video_path": None,
        "cost": None,
        "duration_actual": None,
        "error": None,
        "created_at": datetime.now().isoformat(),
        "completed_at": None,
    }

    # 后台轮询（线程）
    import threading

    def _bg_poll(tid: str):
        t = oneshot_tasks[tid]
        try:
            url = f"{rh.BASE_URL}{rh.POLL_ENDPOINT}"
            elapsed = 0
            while elapsed < rh.MAX_POLL_SECONDS:
                time.sleep(rh.POLL_INTERVAL)
                elapsed += rh.POLL_INTERVAL
                r = rh.poll_once(t["api_key"] if "api_key" in t else api_key, url, t["rh_task_id"])
                if r is None:
                    continue
                status = r.get("status", "UNKNOWN")
                if status == "SUCCESS":
                    results = r.get("results") or []
                    if not results:
                        t["status"] = "failed"
                        t["error"] = "SUCCESS 但无 results"
                        t["completed_at"] = datetime.now().isoformat()
                        return
                    item = results[0]
                    result_url = item.get("url") or item.get("outputUrl")
                    if not result_url:
                        t["status"] = "failed"
                        t["error"] = "results[0] 无 url"
                        t["completed_at"] = datetime.now().isoformat()
                        return
                    ext = item.get("outputType") or "mp4"
                    local_path = Path(t["out_dir"]) / f"result.{ext}"
                    try:
                        full = rh.download_file(result_url, str(local_path))
                        rh.fix_mov_to_mp4(full)
                        full_path = Path(full)
                        try:
                            rel = full_path.relative_to(OUTPUT_DIR)
                            t["video_url"] = f"/outputs/{rel.as_posix()}"
                        except ValueError:
                            t["video_url"] = None
                        t["video_path"] = str(full_path)
                    except Exception as e:
                        t["status"] = "failed"
                        t["error"] = f"下载失败: {e}"
                        t["completed_at"] = datetime.now().isoformat()
                        return
                    usage = r.get("usage") or {}
                    t["cost"] = usage.get("consumeMoney") or usage.get("thirdPartyConsumeMoney")
                    t["duration_actual"] = usage.get("taskCostTime")
                    t["status"] = "completed"
                    t["completed_at"] = datetime.now().isoformat()
                    return
                if status == "FAILED":
                    t["status"] = "failed"
                    t["error"] = r.get("errorMessage", "Unknown error")
                    t["completed_at"] = datetime.now().isoformat()
                    return
        except Exception as e:
            t["status"] = "failed"
            t["error"] = f"轮询异常: {e}"
            t["completed_at"] = datetime.now().isoformat()

    th = threading.Thread(target=_bg_poll, args=(task_id,), daemon=True)
    th.start()

    return {"task_id": task_id, "rh_task_id": rh_task_id, "model_name": model["name"], "endpoint": endpoint_name}


@app.get("/api/one-shot/status/{task_id}")
async def oneshot_status(task_id: str):
    """轮询任务状态"""
    t = oneshot_tasks.get(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    # 返回时排除内部字段
    return {
        "task_id": t["task_id"],
        "status": t["status"],
        "prompt": t["prompt"],
        "model_name": t["model_name"],
        "endpoint": t["endpoint"],
        "is_i2v": t["is_i2v"],
        "video_url": t["video_url"],
        "cost": t["cost"],
        "duration_actual": t["duration_actual"],
        "error": t["error"],
        "created_at": t["created_at"],
        "completed_at": t["completed_at"],
    }


@app.get("/api/one-shot/tasks")
async def oneshot_list_tasks():
    """列出所有一句话生视频任务（最新在前）"""
    items = []
    for t in sorted(oneshot_tasks.values(), key=lambda x: x.get("created_at", ""), reverse=True):
        items.append({
            "task_id": t["task_id"],
            "status": t["status"],
            "prompt": t["prompt"][:80],
            "model_name": t["model_name"],
            "video_url": t["video_url"],
            "cost": t["cost"],
            "created_at": t["created_at"],
        })
    return {"tasks": items, "total": len(items)}


# ─────────────────────────────────────
# 今日头条二创 API
# ─────────────────────────────────────

# 头条任务状态存储
toutiao_tasks: dict = {}


class ToutiaoRunRequest(BaseModel):
    share_url: str
    publish_types: Optional[list[str]] = None
    target_image_count: int = 5


@app.post("/api/toutiao/run")
async def toutiao_run(req: ToutiaoRunRequest):
    """启动今日头条二创任务"""
    task_id = uuid.uuid4().hex[:12]
    toutiao_tasks[task_id] = {
        "status": "running",
        "step": 0,
        "state": "active",
        "log": "",
        "result": None,
        "error": None,
        "started_at": time.time(),
    }

    # 解析发布类型
    publish_types = req.publish_types or settings.TOUTIAO_DEFAULT_PUBLISH_TYPES.split(",")
    publish_types = [t.strip() for t in publish_types if t.strip()]

    async def run_pipeline():
        from backend.services.toutiao_pipeline import run_toutiao_recreate
        from backend.services.toutiao_publisher import STORAGE_STATE_PATH

        log_lines = []

        async def status_callback(msg: str):
            ts = time.strftime("%H:%M:%S")
            log_lines.append(f"[{ts}] {msg}")
            toutiao_tasks[task_id]["log"] = "\n".join(log_lines[-200:])  # 保留最近200行

        try:
            # 步骤标记辅助：根据 msg 中的【N/5】更新 step
            async def tracked_callback(msg: str):
                await status_callback(msg)
                if "【1/5】" in msg:
                    toutiao_tasks[task_id]["step"] = 0
                    toutiao_tasks[task_id]["state"] = "active"
                elif "【2/5】" in msg:
                    toutiao_tasks[task_id]["step"] = 1
                    toutiao_tasks[task_id]["state"] = "done"
                elif "【3/5】" in msg:
                    toutiao_tasks[task_id]["step"] = 2
                    toutiao_tasks[task_id]["state"] = "done"
                elif "【4/5】" in msg:
                    toutiao_tasks[task_id]["step"] = 3
                    toutiao_tasks[task_id]["state"] = "done"
                elif "【5/5】" in msg:
                    toutiao_tasks[task_id]["step"] = 4
                    toutiao_tasks[task_id]["state"] = "done"

            output_dir = Path(settings.TOUTIAO_OUTPUT_DIR) / task_id
            result = await run_toutiao_recreate(
                share_url=req.share_url,
                output_dir=str(output_dir),
                publish_types=publish_types,
                target_image_count=req.target_image_count,
                status_callback=tracked_callback,
            )
            # 把图片路径转换为可访问的 URL（如果在 outputs 下）
            image_urls = []
            for p in result.get("image_paths", []):
                p_obj = Path(p)
                if p_obj.is_absolute():
                    try:
                        rel = p_obj.relative_to(Path(settings.OUTPUT_DIR))
                        image_urls.append(f"/outputs/{rel.as_posix()}")
                    except ValueError:
                        image_urls.append(p)
                else:
                    image_urls.append(p)
            result["image_paths"] = image_urls

            toutiao_tasks[task_id]["result"] = result
            toutiao_tasks[task_id]["status"] = "completed"
            toutiao_tasks[task_id]["step"] = 5
            toutiao_tasks[task_id]["state"] = "done"
            await status_callback("全部完成")
        except Exception as e:
            toutiao_tasks[task_id]["status"] = "failed"
            toutiao_tasks[task_id]["error"] = str(e)
            await status_callback(f"失败: {e}")

    asyncio.create_task(run_pipeline())
    return {"success": True, "task_id": task_id}


@app.get("/api/toutiao/status/{task_id}")
async def toutiao_status(task_id: str):
    """查询头条任务状态"""
    if task_id not in toutiao_tasks:
        raise HTTPException(404, "任务不存在")
    return toutiao_tasks[task_id]


@app.get("/api/toutiao/login/status")
async def toutiao_login_status():
    """检查头条登录状态"""
    from backend.services.toutiao_publisher import STORAGE_STATE_PATH
    return {"logged_in": STORAGE_STATE_PATH.exists()}


@app.post("/api/toutiao/login")
async def toutiao_login():
    """触发扫码登录"""
    from backend.services.toutiao_publisher import ensure_login
    try:
        await ensure_login(force_relogin=True)
        return {"success": True}
    except Exception as e:
        return {"success": False, "detail": str(e)}


@app.get("/api/toutiao/douyin/login/status")
async def douyin_login_status():
    """检查抖音登录状态"""
    from backend.services.douyin_downloader import DOUYIN_STORAGE_STATE_PATH
    return {"logged_in": DOUYIN_STORAGE_STATE_PATH.exists()}


@app.post("/api/toutiao/douyin/login")
async def douyin_login():
    """触发抖音扫码登录"""
    from backend.services.douyin_downloader import ensure_douyin_login
    try:
        await ensure_douyin_login(force_relogin=True)
        return {"success": True}
    except Exception as e:
        return {"success": False, "detail": str(e)}


# ─────────────────────────────────────
# 文件上传 API
# ─────────────────────────────────────

@app.post("/api/upload/video")
async def upload_video(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(400, "文件名为空")
    ext = Path(file.filename).suffix or ".mp4"
    file_id = uuid.uuid4().hex[:12]
    save_name = f"video_{file_id}{ext}"
    save_path = UPLOAD_DIR / save_name
    with open(save_path, "wb") as f:
        content = await file.read()
        f.write(content)
    size_mb = len(content) / 1024 / 1024
    # MiMo-V2.5 限制 15MB，超过则自动压缩
    if size_mb > 15:
        compressed_path = UPLOAD_DIR / f"video_{file_id}_c.mp4"
        # 两轮压缩策略：先 crf=30 不改分辨率；若仍超 14MB，再降到 720p
        def _run_ffmpeg(args):
            return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", timeout=600)
        # 第一轮：保持分辨率，提高 crf 压缩
        p1 = _run_ffmpeg([
            "ffmpeg", "-y", "-i", str(save_path),
            "-c:v", "libx264", "-preset", "fast", "-crf", "30",
            "-c:a", "aac", "-b:a", "96k",
            str(compressed_path)
        ])
        if p1.returncode == 0 and compressed_path.exists():
            c_size = compressed_path.stat().st_size / 1024 / 1024
            if c_size <= 14:
                save_path.unlink(missing_ok=True)
                save_path = compressed_path
                size_mb = c_size
            else:
                # 第二轮：降到 720p 宽度，进一步压缩
                tmp2 = UPLOAD_DIR / f"video_{file_id}_c2.mp4"
                p2 = _run_ffmpeg([
                    "ffmpeg", "-y", "-i", str(compressed_path),
                    "-vf", "scale=720:-2",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "32",
                    "-c:a", "aac", "-b:a", "96k",
                    str(tmp2)
                ])
                compressed_path.unlink(missing_ok=True)
                if p2.returncode == 0 and tmp2.exists() and tmp2.stat().st_size / 1024 / 1024 <= 14:
                    save_path.unlink(missing_ok=True)
                    save_path = tmp2
                    size_mb = tmp2.stat().st_size / 1024 / 1024
                else:
                    tmp2.unlink(missing_ok=True)
                    save_path.unlink(missing_ok=True)
                    raise HTTPException(400, f"视频过大（{size_mb:.1f}MB），自动压缩失败：{p2.stderr[-300:] if p2.stderr else '未知错误'}")
        else:
            save_path.unlink(missing_ok=True)
            compressed_path.unlink(missing_ok=True)
            raise HTTPException(400, f"视频过大（{size_mb:.1f}MB），自动压缩失败：{p1.stderr[-300:] if p1.stderr else '未知错误'}")
    return {"success": True, "file_path": str(save_path), "filename": save_path.name, "size": save_path.stat().st_size}


class WomenswearParseRequest(BaseModel):
    share_url: str


@app.post("/api/womenswear/parse")
async def womenswear_parse(req: WomenswearParseRequest):
    from backend.services.douyin_mall_clothing import resolve_douyin_mall_clothing
    try:
        return {"success": True, "result": await resolve_douyin_mall_clothing(req.share_url)}
    except Exception as e:
        raise HTTPException(400, str(e))


class WomenswearImportRequest(BaseModel):
    image_url: str


@app.post("/api/womenswear/import-image")
async def womenswear_import_image(req: WomenswearImportRequest):
    from backend.services.douyin_mall_clothing import download_clothing_image
    if not req.image_url.startswith(("http://", "https://")):
        raise HTTPException(400, "商品图片地址无效")
    save_path = UPLOAD_DIR / f"douyin_clothing_{uuid.uuid4().hex[:12]}.jpg"
    try:
        await download_clothing_image(req.image_url, save_path)
        return {"success": True, "file_path": str(save_path), "preview_url": f"/api/result/file?path={save_path}"}
    except Exception as e:
        raise HTTPException(400, f"导入商品图片失败: {e}")


class WomenswearGenerateRequest(BaseModel):
    clothing_image_path: str
    model_image_path: str
    scene_image_path: str


class WomenswearFlatLayRequest(BaseModel):
    clothing_image_path: str


@app.post("/api/womenswear/flat-lay")
async def womenswear_flat_lay(req: WomenswearFlatLayRequest):
    """以原始商品图为参考，生成 3:4 白底服装平铺图，作为后续 9:16 产品图的服装真值。"""
    if not Path(req.clothing_image_path).exists():
        raise HTTPException(400, f"图片不存在: {req.clothing_image_path}")
    task_id = uuid.uuid4().hex[:12]
    toutiao_tasks[task_id] = {"status": "running", "result": None, "error": None}

    async def run_flat_lay():
        try:
            from backend.services.douyin_mall_clothing import generate_flat_lay_image, save_image_result
            result = await generate_flat_lay_image(req.clothing_image_path)
            output_path = Path(settings.OUTPUT_DIR) / "womenswear" / task_id / "flat_lay.png"
            await save_image_result(result, output_path)
            toutiao_tasks[task_id].update({
                "status": "completed",
                "result": {
                    "image_path": f"/outputs/{output_path.relative_to(Path(settings.OUTPUT_DIR)).as_posix()}",
                    "abs_path": str(output_path),
                },
            })
        except Exception as e:
            toutiao_tasks[task_id].update({"status": "failed", "error": str(e)})

    asyncio.create_task(run_flat_lay())
    return {"success": True, "task_id": task_id}


@app.post("/api/womenswear/generate")
async def womenswear_generate(req: WomenswearGenerateRequest):
    for path in (req.clothing_image_path, req.model_image_path, req.scene_image_path):
        if not Path(path).exists():
            raise HTTPException(400, f"图片不存在: {path}")
    task_id = uuid.uuid4().hex[:12]
    toutiao_tasks[task_id] = {"status": "running", "result": None, "error": None}

    async def run_generation():
        try:
            from backend.services.douyin_mall_clothing import generate_womenswear_image, save_image_result
            result = await generate_womenswear_image(req.clothing_image_path, req.model_image_path, req.scene_image_path)
            output_path = Path(settings.OUTPUT_DIR) / "womenswear" / task_id / "product_9x16.png"
            await save_image_result(result, output_path)
            toutiao_tasks[task_id].update({"status": "completed", "result": {"image_path": f"/outputs/{output_path.relative_to(Path(settings.OUTPUT_DIR)).as_posix()}"}})
        except Exception as e:
            toutiao_tasks[task_id].update({"status": "failed", "error": str(e)})

    asyncio.create_task(run_generation())
    return {"success": True, "task_id": task_id}


@app.get("/api/womenswear/status/{task_id}")
async def womenswear_status(task_id: str):
    if task_id not in toutiao_tasks:
        raise HTTPException(404, "任务不存在")
    return toutiao_tasks[task_id]


@app.post("/api/upload/image")
async def upload_image(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(400, "文件名为空")
    ext = Path(file.filename).suffix or ".png"
    file_id = uuid.uuid4().hex[:12]
    save_name = f"img_{file_id}{ext}"
    save_path = UPLOAD_DIR / save_name
    with open(save_path, "wb") as f:
        content = await file.read()
        f.write(content)
    return {
        "success": True,
        "path": f"/uploads/{save_name}",
        "file_path": str(save_path),
        "filename": save_name,
        "size": len(content),
    }


@app.post("/api/upload/audio")
async def upload_audio(file: UploadFile = File(...)):
    """上传参考音色音频（用于 IndexTTS 克隆）"""
    if not file.filename:
        raise HTTPException(400, "文件名为空")
    ext = Path(file.filename).suffix.lower()
    if ext not in (".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"):
        raise HTTPException(400, f"不支持的音频格式: {ext}，请上传 wav/mp3/m4a/flac")
    file_id = uuid.uuid4().hex[:12]
    save_name = f"aud_{file_id}{ext}"
    save_path = UPLOAD_DIR / save_name
    with open(save_path, "wb") as f:
        content = await file.read()
        f.write(content)
    return {
        "success": True,
        "path": f"/uploads/{save_name}",
        "file_path": str(save_path),
        "filename": save_name,
        "size": len(content),
    }


# ─────────────────────────────────────
# 工作流执行 API（子进程方式，彻底解决编码问题）
# ─────────────────────────────────────

class WorkflowRequest(BaseModel):
    video_path: str
    product_name: str
    product_image_path: Optional[str] = None
    product_image_paths: Optional[list] = None
    clothing_image_path: Optional[str] = None
    model_image_path: Optional[str] = None
    scene_image_path: Optional[str] = None
    mimo_api_key: Optional[str] = None
    skip_steps: list[float] = []
    video_model: str = "seedance"  # "seedance" | "h3"
    h3_duration: float = 15.0


@app.post("/api/workflow/run")
async def run_workflow(req: WorkflowRequest):
    if not req.product_name or not req.product_name.strip():
        raise HTTPException(status_code=400, detail="必须提供精确的新产品名称")
    task_id = uuid.uuid4().hex[:12]
    tasks[task_id] = {
        "status": "running",
        "step": 0,
        "step_name": "初始化",
        "progress": 0,
        "result": None,
        "error": None,
        "logs": ["启动爆款复刻工作流..."],
    }
    asyncio.create_task(_run_workflow_subprocess(task_id, req))
    return {"success": True, "task_id": task_id}


async def _run_workflow_subprocess(task_id: str, req: WorkflowRequest):
    """用子进程运行工作流（PYTHONUTF8=1 彻底解决编码问题）"""

    # 构造子进程参数 JSON
    params = {
        "video_path": req.video_path,
        "product_name": req.product_name,
        "product_image_path": req.product_image_path,
        "product_image_paths": req.product_image_paths,
        "clothing_image_path": req.clothing_image_path,
        "model_image_path": req.model_image_path,
        "scene_image_path": req.scene_image_path,
        "mimo_api_key": req.mimo_api_key or settings.MIMO_API_KEY,
        "base_output_dir": str(OUTPUT_DIR),
        "skip_steps": list(req.skip_steps),
        "video_model": req.video_model,
    }

    # 写入临时参数文件
    param_file = OUTPUT_DIR / f"_params_{task_id}.json"
    param_file.write_text(json.dumps(params, ensure_ascii=False), encoding="utf-8")

    # 结果文件
    result_file = OUTPUT_DIR / f"_result_{task_id}.json"

    # 子进程脚本：读取参数 → 执行工作流 → 写入结果
    runner_script = OUTPUT_DIR / f"_runner_{task_id}.py"
    runner_script.write_text(
        f'''
import json, sys, os, io, logging
os.environ["PYTHONUTF8"] = "1"
os.environ["PYTHONIOENCODING"] = "utf-8"
sys.path.insert(0, r"{Path(__file__).parent}")

# 强制 UTF-8 stdout/stderr（防止 httpx/openai 内部日志编码崩溃）
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
if hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)

# 抑制 httpx/openai DEBUG 日志（避免编码问题）
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

params = json.loads(open(r"{param_file}", encoding="utf-8").read())
try:
    from workflow import run_full_pipeline
    result = run_full_pipeline(
        video_path=params["video_path"],
        product_name=params["product_name"],
        product_image_path=params.get("product_image_path"),
        clothing_image_path=params.get("clothing_image_path"),
        model_image_path=params.get("model_image_path"),
        scene_image_path=params.get("scene_image_path"),
        mimo_api_key=params.get("mimo_api_key"),
        base_output_dir=params.get("base_output_dir", "outputs"),
        skip_steps=set(params.get("skip_steps", [])),
        product_image_paths=params.get("product_image_paths"),
        video_model=params.get("video_model", "seedance"),
        h3_workflow_id="",
        h3_duration=15.0,
    )
    with open(r"{result_file}", "w", encoding="utf-8") as f:
        json.dump({{"success": True, "result": result}}, f, ensure_ascii=False, default=str)
except Exception as e:
    import traceback
    tb = traceback.format_exc()
    with open(r"{result_file}", "w", encoding="utf-8") as f:
        json.dump({{"success": False, "error": str(e), "traceback": tb}}, f, ensure_ascii=False)
''',
        encoding="utf-8",
    )

    def _log(msg: str):
        tasks[task_id]["logs"].append(msg)

    try:
        # 运行子进程
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        process = await asyncio.create_subprocess_exec(
            sys.executable, "-X", "utf8", str(runner_script),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
            cwd=str(Path(__file__).parent),
        )

        # 实时读取输出（带15分钟超时）
        output_lines = []
        timeout_sec = 900  # 15分钟总超时
        start_time = asyncio.get_event_loop().time()

        while True:
            try:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=60.0)
            except asyncio.TimeoutError:
                # 60秒没新输出，检查总超时
                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed > timeout_sec:
                    process.kill()
                    tasks[task_id]["status"] = "failed"
                    tasks[task_id]["error"] = f"工作流超时（{timeout_sec}秒无完成）"
                    _log(f"工作流超时: 已运行{int(elapsed)}秒，强制终止")
                    return
                continue
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                output_lines.append(text)
                _log(text)
                start_time = asyncio.get_event_loop().time()  # 有输出就重置计时

        await process.wait()

        # 读取结果
        if result_file.exists():
            with open(result_file, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("success"):
                tasks[task_id]["status"] = "completed"
                tasks[task_id]["step"] = 5
                tasks[task_id]["step_name"] = "完成"
                tasks[task_id]["progress"] = 100
                tasks[task_id]["result"] = data["result"]
                _log("工作流执行完成！")
                # 保存到数据库
                try:
                    from db import save_result
                    r = data["result"]
                    save_result(
                        task_id=task_id,
                        run_dir=r.get("run_dir", ""),
                        status="completed",
                        video_path=r.get("step5", {}).get("local_video") or r.get("step5", {}).get("video_url") or "",
                        storyboard_path=r.get("step4", {}).get("poster") or r.get("step3", {}).get("storyboard") or r.get("step2", {}).get("storyboard") or "",
                        poster_path=r.get("step4", {}).get("poster") or "",
                        shot_count=r.get("shot_count", 0),
                    )
                except Exception:
                    pass
            else:
                tasks[task_id]["status"] = "failed"
                tasks[task_id]["error"] = data.get("error", "未知错误")
                _log(f"工作流失败: {data.get('error', '')}")
                if data.get("traceback"):
                    print(f"[Task {task_id}] Traceback:\n{data['traceback']}")
                # 保存失败记录
                try:
                    from db import save_result
                    save_result(
                        task_id=task_id,
                        run_dir=data.get("result", {}).get("run_dir", "") if isinstance(data.get("result"), dict) else "",
                        status="failed",
                        error=data.get("error", "未知错误"),
                    )
                except Exception:
                    pass
        else:
            tasks[task_id]["status"] = "failed"
            tasks[task_id]["error"] = "结果文件未生成"
            _log("工作流失败: 结果文件未生成")

    except Exception as e:
        tasks[task_id]["status"] = "failed"
        tasks[task_id]["error"] = str(e)
        _log(f"工作流异常: {e}")

    finally:
        # 清理临时文件
        for f in [param_file, runner_script]:
            if f.exists():
                try:
                    f.unlink()
                except Exception:
                    pass


@app.get("/api/workflow/status/{task_id}")
async def get_workflow_status(task_id: str):
    if task_id not in tasks:
        raise HTTPException(404, "任务不存在")
    return tasks[task_id]


@app.get("/api/workflow/tasks")
async def list_tasks():
    return {"tasks": tasks}


# ─────────────────────────────────────
# 结果文件 API
# ─────────────────────────────────────

@app.get("/api/result/file")
async def get_result_file(path: str):
    file_path = Path(path)
    if not file_path.exists():
        raise HTTPException(404, "文件不存在")
    # 根据扩展名设置 Content-Type
    suffix = file_path.suffix.lower()
    media_types = {
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".json": "application/json",
    }
    media_type = media_types.get(suffix, "application/octet-stream")
    return FileResponse(str(file_path), media_type=media_type)


@app.get("/api/result/video/{task_id}")
async def get_result_video(task_id: str):
    if task_id not in tasks:
        raise HTTPException(404, "任务不存在")
    task = tasks[task_id]
    if task["status"] != "completed" or not task.get("result"):
        raise HTTPException(400, "任务未完成或无结果")
    result = task["result"]
    step5_info = result.get("step5", {})
    video_url = step5_info.get("video_url")
    poster_path = result.get("step4", {}).get("poster")
    return {"video_url": video_url, "poster_path": poster_path, "run_dir": result.get("run_dir")}


# ─────────────────────────────────────
# 仅 Step5 API
# ─────────────────────────────────────

class Step5Request(BaseModel):
    video_path: str
    product_image_path: Optional[str] = None
    model_image_path: Optional[str] = None
    scene_image_path: Optional[str] = None
    storyboard_image_path: Optional[str] = None
    replaced_storyboard_image_path: Optional[str] = None


@app.post("/api/step5/run")
async def run_step5(req: Step5Request):
    task_id = uuid.uuid4().hex[:12]
    tasks[task_id] = {
        "status": "running",
        "step": 5,
        "step_name": "视频生成（爆款复刻）",
        "progress": 0,
        "result": None,
        "error": None,
        "logs": [],
    }

    # 同样用子进程运行 Step5
    params = {
        "video_path": req.video_path,
        "product_image_path": req.product_image_path,
        "model_image_path": req.model_image_path,
        "scene_image_path": req.scene_image_path,
        "storyboard_image_path": req.storyboard_image_path,
        "replaced_storyboard_image_path": req.replaced_storyboard_image_path,
    }
    param_file = OUTPUT_DIR / f"_s5_params_{task_id}.json"
    param_file.write_text(json.dumps(params, ensure_ascii=False), encoding="utf-8")
    result_file = OUTPUT_DIR / f"_s5_result_{task_id}.json"

    runner_script = OUTPUT_DIR / f"_s5_runner_{task_id}.py"
    runner_script.write_text(
        f'''
import json, sys, os, asyncio, io, logging
os.environ["PYTHONUTF8"] = "1"
os.environ["PYTHONIOENCODING"] = "utf-8"
sys.path.insert(0, r"{Path(__file__).parent}")

# 强制 UTF-8 stdout/stderr
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
if hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)

# 抑制 httpx/openai DEBUG 日志
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

params = json.loads(open(r"{param_file}", encoding="utf-8").read())
try:
    from pipeline.step5_video_generate import run
    result = run(
        video_path=params["video_path"],
        product_image_path=params.get("product_image_path"),
        model_image_path=params.get("model_image_path"),
        scene_image_path=params.get("scene_image_path"),
        output_dir=r"{OUTPUT_DIR / f"step5_{task_id}"}",
        headless=False,
        wait_for_result=True,
        timeout=600,
    )
    with open(r"{result_file}", "w", encoding="utf-8") as f:
        json.dump({{"success": True, "result": result}}, f, ensure_ascii=False, default=str)
except Exception as e:
    import traceback
    tb = traceback.format_exc()
    with open(r"{result_file}", "w", encoding="utf-8") as f:
        json.dump({{"success": False, "error": str(e), "traceback": tb}}, f, ensure_ascii=False)
''',
        encoding="utf-8",
    )

    asyncio.create_task(_run_step5_subprocess(task_id, runner_script, param_file, result_file))
    return {"success": True, "task_id": task_id}


async def _run_step5_subprocess(task_id: str, runner_script: Path, param_file: Path, result_file: Path):
    def _log(msg: str):
        tasks[task_id]["logs"].append(msg)

    try:
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        process = await asyncio.create_subprocess_exec(
            sys.executable, "-X", "utf8", str(runner_script),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
            cwd=str(Path(__file__).parent),
        )

        # 带超时的输出读取（10分钟总超时）
        timeout_sec = 600
        start_time = asyncio.get_event_loop().time()

        while True:
            try:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=60.0)
            except asyncio.TimeoutError:
                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed > timeout_sec:
                    process.kill()
                    tasks[task_id]["status"] = "failed"
                    tasks[task_id]["error"] = f"Step5超时（{timeout_sec}秒）"
                    _log(f"Step5超时: 已运行{int(elapsed)}秒，强制终止")
                    return
                continue
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                _log(text)
                start_time = asyncio.get_event_loop().time()

        await process.wait()

        if result_file.exists():
            with open(result_file, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("success"):
                tasks[task_id]["status"] = "completed"
                tasks[task_id]["progress"] = 100
                tasks[task_id]["result"] = data["result"]
            else:
                tasks[task_id]["status"] = "failed"
                tasks[task_id]["error"] = data.get("error", "未知错误")
        else:
            tasks[task_id]["status"] = "failed"
            tasks[task_id]["error"] = "结果文件未生成"

    except Exception as e:
        tasks[task_id]["status"] = "failed"
        tasks[task_id]["error"] = str(e)
    finally:
        for f in [param_file, runner_script]:
            if f.exists():
                try:
                    f.unlink()
                except Exception:
                    pass


# ─────────────────────────────────────
# 提示词管理 API
# ─────────────────────────────────────
from pydantic import BaseModel

class PromptUpdateRequest(BaseModel):
    step_key: str
    prompt_key: str
    value: str


@app.get("/api/prompts")
async def get_prompts():
    """获取所有提示词；Step1 返回当前不可覆盖的生产契约。"""
    from copy import deepcopy
    from prompts_loader import get_all
    from pipeline.step1_mimo_parse import SYSTEM_PROMPT
    data = deepcopy(get_all())
    step1 = data.get("step1_mimo_parse", {}).get("prompts", {}).get("SYSTEM_PROMPT")
    if step1:
        step1["value"] = SYSTEM_PROMPT
        step1["readonly"] = True
    return data


@app.post("/api/prompts/update")
async def update_prompt(req: PromptUpdateRequest):
    """更新单个提示词"""
    if req.step_key == "step1_mimo_parse" and req.prompt_key == "SYSTEM_PROMPT":
        raise HTTPException(status_code=409, detail="Step1生产提示词不可在线修改")
    from prompts_loader import update_prompt, reload
    ok = update_prompt(req.step_key, req.prompt_key, req.value)
    if ok:
        reload()
        return {"success": True, "message": "提示词已更新"}
    return {"success": False, "message": "未找到指定的提示词"}


@app.post("/api/prompts/reset")
async def reset_prompts():
    """重置所有提示词为默认值（重新加载文件）"""
    from prompts_loader import reload
    reload()
    return {"success": True, "message": "提示词已重新加载"}


# ============================================================================
# ===== 爆款复刻边界 START =====
# ⚠️ 以下代码段属于爆款复刻工作流（段保护），修改必须经过显式确认：
#   1. 运行 python scripts/check_replica_boundary.py 记录变更
#   2. 运行 pytest tests/test_replica_boundary.py -v 验证行为
#   3. 运行 python scripts/update_replica_boundary.py 更新签名基准
# 参见 BOUNDARY.md。本段保护范围直到匹配的 END 标记为止。
# ============================================================================
# ─────────────────────────────────────
# 爆款复刻历史记录 API
# ─────────────────────────────────────

def _load_storyboard_script(run_dir: str):
    """从 run_dir/step1/storyboard.json 读取分镜脚本，供前端展示。"""
    if not run_dir:
        return None
    import json as _json
    s1_json = Path(run_dir) / "step1" / "storyboard.json"
    if not s1_json.exists():
        return None
    try:
        return _json.loads(s1_json.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_step4_params(run_dir: str):
    """从 run_dir/step4/step4_params.json 读取 Step4 中间参数，供前端展示。

    返回 dict（含 video_type/template_name/image_roles/prompt/feedback 等），
    文件不存在返回 None。storyboard_json 字段过大，不回传（前端用 storyboard_script 单独拿）。
    """
    if not run_dir:
        return None
    import json as _json
    p = Path(run_dir) / "step4" / "step4_params.json"
    if not p.exists():
        return None
    try:
        data = _json.loads(p.read_text(encoding="utf-8"))
        # storyboard_json 单独已有接口，这里去掉避免重复传输
        data.pop("storyboard_json", None)
        return data
    except Exception:
        return None


@app.get("/api/replica/history")
async def replica_history():
    """获取爆款复刻历史记录。
    对 run_dir 为空的记录，按优先级匹配：
      1. _phase_state.json 里的 task_id 精确匹配（新任务）
      2. created_at 时间戳模糊匹配 run_* 目录（±5分钟，旧任务）"""
    import json as _json
    from db import get_history
    history = get_history()
    out_dir = Path(OUTPUT_DIR)

    # 策略1: 构建 task_id → run_dir 映射（扫描所有 run_* 目录的 _phase_state.json）
    tid_to_rundir = {}
    # 同时收集所有 run_dir 的修改时间，用于策略2
    run_dir_times = []  # [(datetime, run_dir_path)]
    for rd in out_dir.glob("run_*"):
        if not rd.is_dir():
            continue
        try:
            run_dir_times.append((datetime.fromtimestamp(rd.stat().st_mtime), rd))
        except Exception:
            pass
        state_file = rd / "_phase_state.json"
        if not state_file.exists():
            continue
        try:
            st = _json.loads(state_file.read_text(encoding="utf-8"))
            tid = st.get("task_id", "")
            if tid:
                tid_to_rundir[tid] = str(rd)
        except Exception:
            pass

    for item in history:
        if item.get("run_dir"):
            # 统一用正斜杠，避免前端 JS 字符串里反斜杠被转义
            item["run_dir"] = item["run_dir"].replace("\\", "/")
            continue
        tid = item.get("task_id", "")
        # 策略1: task_id 精确匹配
        if tid and tid in tid_to_rundir:
            item["run_dir"] = tid_to_rundir[tid].replace("\\", "/")
            continue
        # 策略2: created_at 时间模糊匹配（±5分钟内最近的 run_dir）
        created = item.get("created_at", "")
        if not created:
            continue
        try:
            dt_created = datetime.strptime(created, "%Y-%m-%d %H:%M:%S")
            best_dir = None
            best_diff = 999999
            for dt_rd, rd in run_dir_times:
                diff = abs((dt_rd - dt_created).total_seconds())
                if diff < best_diff:
                    best_diff = diff
                    best_dir = rd
            # 5分钟内才算匹配
            if best_dir and best_diff < 300:
                item["run_dir"] = str(best_dir).replace("\\", "/")
        except Exception:
            pass

    return {"history": history}


# ─────────────────────────────────────
# 爆款复刻 - 分阶段审核 API（监控分镜图/故事板）
# ─────────────────────────────────────
# 流程：
#   POST /api/replica/run-phased        → 启动 Phase1（Step1+2+3）→ awaiting_review (after_step3)
#   GET  /api/replica/review/{task_id}  → 查看待审图片（分镜图/故事板）
#   POST /api/replica/approve/{task_id} → 通过 → 执行下一阶段
#   POST /api/replica/reject/{task_id}  → 驳回 → 重做当前阶段
#   GET  /api/replica/status/{task_id}  → 查询任务状态

# 分阶段任务状态存储（独立于 tasks，避免冲突）
replica_phased_tasks: dict = {}

# Step5 (phase_3 / redo_phase_3) 全局并发锁：浏览器持久化上下文不允许并发访问同一 user_data_dir
# 多个 phase_3 子进程同时启动会导致 Chromium "ProcessSingleton" 错误
_step5_concurrency_lock = asyncio.Lock()


class PhasedWorkflowRequest(BaseModel):
    video_path: str
    product_name: str
    product_image_path: Optional[str] = None
    product_image_paths: list = None
    clothing_image_path: Optional[str] = None
    model_image_path: Optional[str] = None
    scene_image_path: Optional[str] = None
    mimo_api_key: Optional[str] = None
    video_model: str = "seedance"  # "seedance" | "h3" | "multi_replica" | "seedance_25"
    h3_duration: float = 15.0
    duration: str = "15s"   # 视频时长：5s/10s/15s/20s/25s/30s（≥20s 仅 seedance_25 支持）
    trim_start: Optional[float] = None   # 裁剪起点（秒，0-based），与 trim_end 同时提供时启用精准复刻（5-30s）
    trim_end: Optional[float] = None     # 裁剪终点（秒，0-based）


@app.post("/api/replica/run-phased")
async def replica_run_phased(req: PhasedWorkflowRequest):
    """启动分阶段审核模式 - Phase 1: Step1+2+3，完成后等待审核分镜图"""
    if not req.product_name or not req.product_name.strip():
        raise HTTPException(status_code=400, detail="必须提供精确的新产品名称")

    # 裁剪区间校验（5-30s）；同时提供时进行 ffmpeg 裁剪
    effective_video_path = req.video_path
    if req.trim_start is not None and req.trim_end is not None:
        try:
            s, e = float(req.trim_start), float(req.trim_end)
        except (TypeError, ValueError):
            raise HTTPException(400, "裁剪区间必须为数字")
        if s < 0 or e <= s:
            raise HTTPException(400, f"裁剪区间非法：start={s} end={e}")
        span = e - s
        if span < 5 - 0.05:
            raise HTTPException(400, f"裁剪区间过短：{span:.1f}s < 5s（最短 5s）")
        if span > 30 + 0.05:
            raise HTTPException(400, f"裁剪区间过长：{span:.1f}s > 30s（最长 30s）")
        # 探测原视频时长，确保裁剪终点不超过视频时长
        try:
            from backend.services.super_clone_wan3 import _ffprobe_duration, trim_video
            vd = _ffprobe_duration(req.video_path) or 0.0
            if vd > 0 and e > vd + 0.05:
                raise HTTPException(400, f"裁剪终点 {e:.1f}s 超过视频时长 {vd:.1f}s")
            # 执行裁剪到临时文件
            trimmed_path = str(OUTPUT_DIR / f"_trim_{uuid.uuid4().hex[:10]}.mp4")
            trim_video(req.video_path, s, e, trimmed_path)
            effective_video_path = trimmed_path
        except HTTPException:
            raise
        except Exception as ex:
            raise HTTPException(500, f"视频裁剪失败: {ex}")

    task_id = uuid.uuid4().hex[:12]
    replica_phased_tasks[task_id] = {
        "status": "running",
        "phase": "phase_1",
        "step_name": "Step1+2+3: 解析+抽帧+产品替换",
        "progress": 0,
        "run_dir": None,
        "review": None,
        "result": None,
        "error": None,
        "logs": ["启动分阶段审核模式 - Phase 1..."],
        "request": req.dict(),  # 保存原始请求，供 retry 重跑 phase_1 使用
        "effective_video_path": effective_video_path,  # 裁剪后实际送入 run_phase_1 的路径
    }
    asyncio.create_task(_run_phase_subprocess(task_id, "phase_1", req))
    # 启动时即写入 DB（status=running），确保历史记录立即可见
    try:
        from db import save_result
        save_result(task_id=task_id, run_dir="", status="running", error="")
    except Exception:
        pass
    return {"success": True, "task_id": task_id, "phase": "phase_1"}


async def _run_phase_subprocess(task_id: str, phase: str, req: Optional[PhasedWorkflowRequest] = None):
    """用子进程运行阶段任务（避免阻塞主进程）"""
    task = replica_phased_tasks[task_id]

    # 构造子进程脚本
    if phase == "phase_1":
        # 若已预裁剪（用户在前端选定 trim_start/trim_end），优先使用裁剪后的视频
        eff_path = task.get("effective_video_path") or req.video_path
        params = {
            "video_path": eff_path,
            "product_name": req.product_name,
            "product_image_path": req.product_image_path,
            "product_image_paths": req.product_image_paths or [],
            "clothing_image_path": req.clothing_image_path,
            "model_image_path": req.model_image_path,
            "scene_image_path": req.scene_image_path,
            "mimo_api_key": req.mimo_api_key or settings.MIMO_API_KEY,
            "base_output_dir": str(OUTPUT_DIR),
            "video_model": req.video_model,
            "h3_duration": req.h3_duration,
            "duration": req.duration,
            "task_id": task_id,
        }
        phase_func = "run_phase_1"
    elif phase == "phase_2":
        params = {"run_dir": task["run_dir"]}
        # 透传 Step4 修改意见（首次 phase_2 也支持，默认空）
        if task.get("redo_poster_feedback"):
            params["feedback"] = task["redo_poster_feedback"]
        if task.get("redo_poster_override_video_type"):
            params["override_video_type"] = task["redo_poster_override_video_type"]
        phase_func = "run_phase_2"
    elif phase == "phase_3":
        params = {"run_dir": task["run_dir"]}
        phase_func = "run_phase_3"
    elif phase == "phase_3_multi":
        # 首尾帧无限拼接·复刻模式（FL2VA）：调用 run_phase_3_multi(mode=replica)
        # 不加入 Step5 串行锁（manxue H3 API 不抢 weeksir 浏览器）
        params = {
            "run_dir": task["run_dir"],
            "mode": "replica",
            "segment_duration": task.get("segment_duration", 5),
        }
        phase_func = "run_phase_3_multi"
    elif phase == "phase_3_multi_director":
        # 首尾帧无限拼接·复刻模式（Director 满血版）：调用 run_phase_3_multi_director(mode=replica)
        # 同样不加 Step5 串行锁（RunningHub API 不抢浏览器）
        # segment_duration 从请求中读取（Director 支持 1-15 任意整数秒）
        req = task.get("request") or {}
        params = {
            "run_dir": task["run_dir"],
            "mode": "replica",
            "segment_duration": req.get("segment_duration", 4),
        }
        phase_func = "run_phase_3_multi_director"
    elif phase == "resume_phase_1":
        params = {"run_dir": task["run_dir"]}
        phase_func = "resume_phase_1"
    elif phase == "redo_phase_1":
        params = {"run_dir": task["run_dir"]}
        phase_func = "redo_phase_1"
    elif phase == "redo_phase_2":
        params = {"run_dir": task["run_dir"]}
        # 透传 Step4 修改意见（redo-poster 端点写入 task）
        if task.get("redo_poster_feedback"):
            params["feedback"] = task["redo_poster_feedback"]
        if task.get("redo_poster_override_video_type"):
            params["override_video_type"] = task["redo_poster_override_video_type"]
        phase_func = "redo_phase_2"
    elif phase == "re_reason":
        params = {"run_dir": task["run_dir"]}
        phase_func = "re_reason_step1_to_step3"
    else:
        task.update({"status": "failed", "error": f"未知阶段: {phase}"})
        return

    param_file = OUTPUT_DIR / f"_phased_params_{task_id}.json"
    param_file.write_text(json.dumps(params, ensure_ascii=False), encoding="utf-8")
    result_file = OUTPUT_DIR / f"_phased_result_{task_id}.json"

    # Step5 (phase_3 / redo_phase_3) 串行化：浏览器持久化上下文不支持并发访问同一 user_data_dir
    # 使用全局锁确保同时只有一个 Step5 子进程在跑，避免 Chromium "ProcessSingleton" 错误
    step5_lock_acquired = False
    if phase in ("phase_3", "redo_phase_3"):
        # 仅 seedance/h3 单镜头模式需要浏览器锁（weeksir_profile）
        # phase_3_multi（首尾帧无限拼接）走 manxue H3 API，不抢浏览器，不加锁
        print(f"[{task_id}] ⏳ Step5 等待浏览器锁（前序任务可能正在使用 weeksir_profile）...")
        task["logs"].append("⏳ 等待浏览器锁（其他任务正在生成视频）...")
        await _step5_concurrency_lock.acquire()
        step5_lock_acquired = True
        print(f"[{task_id}] 🔓 获取浏览器锁，开始执行 Step5")
        task["logs"].append("🔓 获取浏览器锁，开始执行 Step5")

    runner_script = OUTPUT_DIR / f"_phased_runner_{task_id}.py"
    runner_script.write_text(
        f'''
import json, sys, os, io, logging
os.environ["PYTHONUTF8"] = "1"
os.environ["PYTHONIOENCODING"] = "utf-8"
sys.path.insert(0, r"{Path(__file__).parent}")
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
if hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

params = json.loads(open(r"{param_file}", encoding="utf-8").read())
try:
    from workflow import {phase_func}
    result = {phase_func}(**params)
    with open(r"{result_file}", "w", encoding="utf-8") as f:
        json.dump({{"success": True, "result": result}}, f, ensure_ascii=False, default=str)
except Exception as e:
    import traceback
    tb = traceback.format_exc()
    with open(r"{result_file}", "w", encoding="utf-8") as f:
        json.dump({{"success": False, "error": str(e), "traceback": tb}}, f, ensure_ascii=False)
''',
        encoding="utf-8",
    )

    def _log(msg: str):
        task["logs"].append(msg)

    try:
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        process = await asyncio.create_subprocess_exec(
            sys.executable, "-X", "utf8", str(runner_script),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
            cwd=str(Path(__file__).parent),
        )

        timeout_sec = 900
        start_time = asyncio.get_event_loop().time()
        while True:
            try:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=10.0)
            except asyncio.TimeoutError:
                line = b""
            if line:
                msg = line.decode("utf-8", errors="replace").rstrip()
                _log(msg)
                echo(f"[{task_id}] {msg}")        # 父进程侧统一走 echo()（见 echo 的docstring）
                # 提前提取 run_dir（即使 phase 失败也能用于 retry）
                if "[Phase1] 运行目录:" in msg and not task.get("run_dir"):
                    try:
                        task["run_dir"] = msg.split("运行目录:", 1)[1].strip()
                    except Exception:
                        pass
            if process.stdout.at_eof():
                break
            if asyncio.get_event_loop().time() - start_time > timeout_sec:
                process.kill()
                task.update({"status": "failed", "error": f"{phase} 超时（{timeout_sec}s）"})
                try:
                    from db import save_result
                    save_result(task_id=task_id, run_dir=task.get("run_dir", "") or "",
                                status="failed", error=f"{phase} 超时（{timeout_sec}s）")
                except Exception:
                    pass
                return

        await asyncio.wait_for(process.wait(), timeout=30.0)

        # 读取结果
        if not result_file.exists():
            task.update({"status": "failed", "error": f"{phase} 未生成结果文件"})
            try:
                from db import save_result
                save_result(task_id=task_id, run_dir=task.get("run_dir", "") or "",
                            status="failed", error=f"{phase} 未生成结果文件")
            except Exception:
                pass
            return
        result_data = json.loads(result_file.read_text(encoding="utf-8"))

        if not result_data.get("success"):
            task.update({
                "status": "failed",
                "error": result_data.get("error", "未知错误"),
                "traceback": result_data.get("traceback"),
            })
            try:
                from db import save_result
                save_result(task_id=task_id, run_dir=task.get("run_dir", "") or "",
                            status="failed", error=result_data.get("error", "未知错误"))
            except Exception:
                pass
            return

        res = result_data["result"]
        run_dir = res.get("run_dir")
        review = res.get("review")
        new_phase = res.get("phase")

        # 更新任务状态
        task["run_dir"] = run_dir
        if new_phase == "completed":
            task.update({
                "status": "completed",
                "phase": "completed",
                "step_name": "视频生成完成",
                "progress": 100,
                "result": res.get("summary"),
            })
            try:
                from db import save_result
                summary = res.get("summary") or {}
                s5 = summary.get("step5", {}) if isinstance(summary, dict) else {}
                s4 = summary.get("step4", {}) if isinstance(summary, dict) else {}
                s3 = summary.get("step3", {}) if isinstance(summary, dict) else {}
                s2 = summary.get("step2", {}) if isinstance(summary, dict) else {}
                save_result(
                    task_id=task_id, run_dir=run_dir or "", status="completed",
                    video_path=(s5.get("local_video") or s5.get("video_url") or ""),
                    storyboard_path=(s4.get("poster") or s3.get("storyboard") or s2.get("storyboard") or ""),
                    poster_path=s4.get("poster") or "",
                    shot_count=summary.get("shot_count", 0) if isinstance(summary, dict) else 0,
                )
            except Exception:
                pass
        else:
            task.update({
                "status": "awaiting_review",
                "phase": new_phase,
                "step_name": "等待审核" if "redo" not in phase else "重做完成等待审核",
                "progress": 50 if new_phase == "after_step3" else 75,
                "review": review,
            })
            try:
                from db import save_result
                shots = (review or {}).get("shots") or []
                poster = (review or {}).get("poster") or {}
                storyboards = (review or {}).get("storyboards") or []
                save_result(
                    task_id=task_id, run_dir=run_dir or "",
                    status="awaiting_review",
                    storyboard_path=(storyboards[0]["path"] if storyboards else ""),
                    poster_path=poster.get("path", ""),
                    shot_count=len(shots),
                )
            except Exception:
                pass
        _log(f"[{phase}] 完成，当前阶段: {new_phase}")

    except Exception as e:
        import traceback
        task.update({"status": "failed", "error": str(e), "traceback": traceback.format_exc()})
        try:
            from db import save_result
            save_result(task_id=task_id, run_dir=task.get("run_dir", "") or "",
                        status="failed", error=str(e))
        except Exception:
            pass
    finally:
        # 释放 Step5 浏览器锁，让下一个等待中的 Step5 任务可以执行
        if step5_lock_acquired:
            _step5_concurrency_lock.release()
            print(f"[{task_id}] 🔒 释放浏览器锁")
        # 只删 runner_script，保留 param_file 供下次 approve 覆盖（避免竞态：前一次 finally 删掉了下次刚写入的 param）
        try:
            runner_script.unlink(missing_ok=True)
        except Exception:
            pass


def _restore_task_from_disk(task_id: str) -> bool:
    """从磁盘 _phase_state.json 恢复任务到内存（服务重启后内存清空时使用）。

    扫描所有 run_*/_phase_state.json，匹配 task_id，找到后写入 replica_phased_tasks。
    返回 True 表示恢复成功，False 表示未找到。
    """
    import json as _json
    _matched_run = None
    for _sf in Path(OUTPUT_DIR).glob("run_*/_phase_state.json"):
        try:
            _sd = _json.loads(_sf.read_text(encoding="utf-8"))
            if _sd.get("task_id", "").replace("run_", "") == task_id:
                _matched_run = _sf.parent
                break
        except Exception:
            pass
    if not _matched_run:
        return False
    _sd = _json.loads((_matched_run / "_phase_state.json").read_text(encoding="utf-8"))
    _phase = _sd.get("phase", "after_step3")
    replica_phased_tasks[task_id] = {
        "status": "awaiting_review",
        "phase": _phase,
        "step_name": f"恢复任务（{_phase}）",
        "run_dir": str(_matched_run),
        "review": None,
        "result": None,
        "error": None,
        "logs": [f"从磁盘恢复任务 {task_id}（phase={_phase}）"],
        "request": _sd,
    }
    return True


@app.get("/api/replica/review/{task_id}")
async def replica_get_review(task_id: str):
    """获取待审图片列表"""
    # 内存未命中时，从磁盘扫描 _phase_state.json 恢复（服务重启后内存清空仍可查看）
    if task_id not in replica_phased_tasks:
        if not _restore_task_from_disk(task_id):
            raise HTTPException(status_code=404, detail="任务不存在")
    task = replica_phased_tasks[task_id]
    # 实时从磁盘 _phase_state.json 同步 phase（外部脚本重做后内存可能过期）
    run_dir = task.get("run_dir")
    if run_dir:
        _state_file = Path(run_dir) / "_phase_state.json"
        if _state_file.exists():
            try:
                import json as _json
                _sd = _json.loads(_state_file.read_text(encoding="utf-8"))
                _disk_phase = _sd.get("phase")
                if _disk_phase and _disk_phase != task.get("phase"):
                    task["phase"] = _disk_phase
                    task["logs"].append(f"从磁盘同步 phase: {_disk_phase}")
            except Exception:
                pass
    # 实时从磁盘扫描产物（避免内存缓存与磁盘不同步，例如外部脚本重做 Step3 后内存未更新）
    review = {}
    if run_dir:
        from workflow import _collect_review_images
        try:
            review = _collect_review_images(Path(run_dir), task["phase"]) or {}
        except Exception:
            review = task.get("review") or {}
    else:
        review = task.get("review") or {}
    # 将绝对路径转为 /outputs/ 或 /uploads/ URL
    def _to_url(p: str) -> str:
        try:
            rel = Path(p).relative_to(OUTPUT_DIR)
            return f"/outputs/{rel.as_posix()}"
        except Exception:
            pass
        try:
            rel = Path(p).relative_to(UPLOAD_DIR)
            return f"/uploads/{rel.as_posix()}"
        except Exception:
            return p

    review_url = {"phase": review.get("phase")}
    if review.get("storyboards"):
        review_url["storyboards"] = [
            {"label": s["label"], "url": _to_url(s["path"])} for s in review["storyboards"]
        ]
    if review.get("shots"):
        review_url["shots"] = [
            {"label": s["label"], "url": _to_url(s["path"])} for s in review["shots"]
        ]
    if review.get("poster"):
        review_url["poster"] = {"label": review["poster"]["label"], "url": _to_url(review["poster"]["path"])}
    # 原帧（Step2 抽帧）转 URL，用于 Step3 审核界面对比"原帧 vs 替换后"
    if review.get("original_shots"):
        review_url["original_shots"] = [
            {"label": s["label"], "url": _to_url(s["path"])} for s in review["original_shots"]
        ]
    # 用户上传的参考图（产品图/服装图/模特图/场景图）
    if review.get("reference_images"):
        review_url["reference_images"] = [
            {"label": s["label"], "url": _to_url(s["path"]), "kind": s.get("kind", "")} for s in review["reference_images"]
        ]

    return {
        "status": "awaiting_review",
        "phase": task["phase"],
        "step_name": task["step_name"],
        "review": review_url,
        "run_dir": task["run_dir"],
        "storyboard_script": _load_storyboard_script(task.get("run_dir", "")),
        "step4_params": _load_step4_params(task.get("run_dir", "")),
    }


@app.post("/api/replica/approve/{task_id}")
async def replica_approve(task_id: str):
    """审核通过，继续执行下一阶段"""
    if task_id not in replica_phased_tasks:
        if not _restore_task_from_disk(task_id):
            raise HTTPException(status_code=404, detail="任务不存在")
    task = replica_phased_tasks[task_id]
    if task["status"] != "awaiting_review":
        raise HTTPException(status_code=400, detail=f"当前状态 {task['status']} 不可审核通过")

    current_phase = task["phase"]
    if current_phase == "after_step3":
        next_phase = "phase_2"
        step_name = "Step4: 故事板海报生成"
    elif current_phase == "after_step4":
        # 根据 video_model 分流：multi_replica 走 phase_3_multi（FL2VA 拼接），multi_replica_director 走 phase_3_multi_director（Director 一次生成），其他走 phase_3
        video_model = (task.get("request") or {}).get("video_model", "seedance")
        if video_model == "multi_replica":
            next_phase = "phase_3_multi"
            step_name = "Step5 首尾帧无限拼接（复刻·FL2VA）：H3 双帧并发生成"
        elif video_model == "multi_replica_director":
            next_phase = "phase_3_multi_director"
            step_name = "Step5 首尾帧无限拼接（复刻·Director）：H3 Director 一次生成"
        else:
            next_phase = "phase_3"
            step_name = "Step5: 视频生成"
    else:
        raise HTTPException(status_code=400, detail=f"当前阶段 {current_phase} 无后续阶段")

    task.update({
        "status": "running",
        "phase": next_phase,
        "step_name": step_name,
        "progress": task.get("progress", 0),
        "review": None,
        "logs": task["logs"] + [f"审核通过，继续执行 {next_phase}..."],
    })
    # 清掉可能残留的 Step4 修改意见，避免下次 phase_2/redo_phase_2 误读
    task.pop("redo_poster_feedback", None)
    task.pop("redo_poster_override_video_type", None)
    asyncio.create_task(_run_phase_subprocess(task_id, next_phase))
    return {"success": True, "task_id": task_id, "next_phase": next_phase}


@app.post("/api/replica/reject/{task_id}")
async def replica_reject(task_id: str, reason: str = ""):
    """审核驳回，重做当前阶段"""
    if task_id not in replica_phased_tasks:
        if not _restore_task_from_disk(task_id):
            raise HTTPException(status_code=404, detail="任务不存在")
    task = replica_phased_tasks[task_id]
    if task["status"] != "awaiting_review":
        raise HTTPException(status_code=400, detail=f"当前状态 {task['status']} 不可驳回")

    current_phase = task["phase"]
    if current_phase == "after_step3":
        redo_phase = "redo_phase_1"
        step_name = "重做 Step3: 产品替换"
    elif current_phase == "after_step4":
        redo_phase = "redo_phase_2"
        step_name = "重做 Step4: 故事板海报"
    else:
        raise HTTPException(
            status_code=400,
            detail=f"当前阶段 {current_phase} 不可重做（仅 after_step3/after_step4 可驳回重做）。"
                   f"该任务产物可能不完整，建议重新发起新任务。"
        )

    task.update({
        "status": "running",
        "phase": redo_phase,
        "step_name": step_name,
        "review": None,
        "logs": task["logs"] + [f"审核驳回（原因: {reason or '未提供'}），重做当前阶段..."],
    })
    # reject 是无差别重做（不带 feedback），清掉残留意见避免误读
    task.pop("redo_poster_feedback", None)
    task.pop("redo_poster_override_video_type", None)
    asyncio.create_task(_run_phase_subprocess(task_id, redo_phase))
    return {"success": True, "task_id": task_id, "redo_phase": redo_phase}


class RedoShotRequest(BaseModel):
    shot_num: int
    feedback: str = ""
    override_image_path: str = ""  # 可选，用户上传的替换参考图绝对路径。传入时只用此图，不传用原产品图


class RedoPosterRequest(BaseModel):
    """Step4 故事板海报修改意见重做请求"""
    feedback: str = ""  # 修改意见，非空时作为"必须严格执行"段追加到 prompt
    override_video_type: str = ""  # 视频类型手动覆盖：""/auto=自动判定，voiceover/showcase/action=强制指定


@app.post("/api/replica/reset/{task_id}")
async def replica_reset_task(task_id: str, target_phase: str = "after_step3"):
    """强制重置任务状态到 awaiting_review，用于从 failed 状态恢复重试。

    Args:
        task_id: 任务ID
        target_phase: 目标 phase（默认 after_step3，对应 redo_phase_1）
    """
    if task_id not in replica_phased_tasks:
        if not _restore_task_from_disk(task_id):
            raise HTTPException(status_code=404, detail="任务不存在")
    task = replica_phased_tasks[task_id]
    old_status = task.get("status")
    old_phase = task.get("phase")
    task.update({
        "status": "awaiting_review",
        "phase": target_phase,
        "step_name": f"恢复到 {target_phase}（待审核）",
        "error": None,
        "logs": task["logs"] + [f"强制重置: {old_status}/{old_phase} → awaiting_review/{target_phase}"],
    })
    # 同步磁盘 phase
    run_dir = task.get("run_dir")
    if run_dir:
        _state_file = Path(run_dir) / "_phase_state.json"
        if _state_file.exists():
            try:
                import json as _json
                _sd = _json.loads(_state_file.read_text(encoding="utf-8"))
                _sd["phase"] = target_phase
                _state_file.write_text(_json.dumps(_sd, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass
    return {"success": True, "task_id": task_id, "old_status": old_status, "new_status": "awaiting_review", "phase": target_phase}


@app.post("/api/replica/redo-poster/{task_id}")
async def replica_redo_poster(task_id: str, req: RedoPosterRequest):
    """带修改意见重做 Step4 故事板海报。

    在 after_step4 审核阶段，用户对海报不满意时，提交修改意见（可选 + 视频类型覆盖），
    重新生成故事板海报。feedback 作为强制段追加到 prompt，覆盖模板默认行为。
    """
    if task_id not in replica_phased_tasks:
        if not _restore_task_from_disk(task_id):
            raise HTTPException(status_code=404, detail="任务不存在")
    task = replica_phased_tasks[task_id]
    if task["status"] != "awaiting_review":
        raise HTTPException(status_code=400, detail=f"当前状态 {task['status']} 不可重做海报")
    if task["phase"] != "after_step4":
        raise HTTPException(status_code=400, detail=f"海报重做仅在 after_step4 阶段可用，当前: {task['phase']}")

    _vt = (req.override_video_type or "").strip().lower()
    if _vt and _vt not in ("auto", "voiceover", "showcase", "action"):
        raise HTTPException(status_code=400, detail=f"override_video_type 取值非法: {_vt}（应为 auto/voiceover/showcase/action）")

    # 把 feedback / override_video_type 写入 task，_run_phase_subprocess 的 redo_phase_2 分支会读取
    task["redo_poster_feedback"] = (req.feedback or "").strip()
    task["redo_poster_override_video_type"] = _vt

    task.update({
        "status": "running",
        "phase": "redo_phase_2",
        "step_name": "按修改意见重做 Step4: 故事板海报",
        "review": None,
        "logs": task["logs"] + [
            f"按修改意见重做 Step4（意见: {task['redo_poster_feedback'][:60] or '无'}{'...' if len(task['redo_poster_feedback']) > 60 else ''}，"
            f"视频类型: {_vt or '自动判定'}）..."
        ],
    })
    asyncio.create_task(_run_phase_subprocess(task_id, "redo_phase_2"))
    return {"success": True, "task_id": task_id, "redo_phase": "redo_phase_2"}


@app.post("/api/replica/redo-shot/{task_id}")
async def replica_redo_shot(task_id: str, req: RedoShotRequest):
    """精准重做单个 Shot 的产品替换图（不影响其他 Shot）。

    在 after_step3 审核阶段，用户对某个分镜不满意时，提交修改意见，
    只重新生成该 Shot 的图片，其他 Shot 保持不变。完成后重新进入 awaiting_review。

    支持并行重做：允许在 awaiting_review 或 running（重做进行中）状态下触发新重做，
    不切 status，每个 shot 独立跑后台，互不阻塞。每个 shot 用独立的子进程参数/
    结果文件（文件名带 shot_num 后缀），避免并发覆盖。
    """
    if task_id not in replica_phased_tasks:
        if not _restore_task_from_disk(task_id):
            raise HTTPException(status_code=404, detail="任务不存在")
    task = replica_phased_tasks[task_id]
    # 支持并行重做：允许在 awaiting_review 或 running（重做进行中）状态下触发新重做
    if task["status"] not in ("awaiting_review", "running", "redo_failed"):
        raise HTTPException(status_code=400, detail=f"当前状态 {task['status']} 不可重做单 Shot")
    if task["phase"] != "after_step3":
        raise HTTPException(status_code=400, detail=f"单 Shot 重做仅在 after_step3 阶段可用，当前: {task['phase']}")
    if req.shot_num < 1:
        raise HTTPException(status_code=400, detail="shot_num 必须 >= 1")
    # 校验 shot_num 不超过实际分镜数
    _storyboard = (task.get("request") or {}).get("storyboard") or []
    if _storyboard:
        _max_shot = len(_storyboard)
        if req.shot_num > _max_shot:
            raise HTTPException(status_code=400, detail=f"shot_num={req.shot_num} 超出分镜范围（1~{_max_shot}）")

    run_dir = task.get("run_dir")
    if not run_dir:
        raise HTTPException(status_code=400, detail="任务无 run_dir，无法重做")

    shot_num = req.shot_num

    # 并行重做：维护 redoing_shots 列表，避免对同一 shot 重复触发
    redoing = task.setdefault("redoing_shots", [])
    if shot_num in redoing:
        raise HTTPException(400, f"shot_{shot_num:02d} 正在重做中，请等待完成")
    redoing.append(shot_num)

    # 切到 running 状态（保留 review 数据），前端 polling 看到 running 就不会立即刷新 reviewCard，
    # 同时通过 redoing_shots 列表让前端知道哪些 Shot 在后台重做并显示 loading 蒙层
    task["status"] = "running"
    task["phase"] = "after_step3"
    task["step_name"] = f"重做 Shot {shot_num:02d}（LLM 分析+image2 重做中）"
    task["error"] = None
    task["logs"] = task["logs"] + [
        f"🔄 精准重做 Shot {shot_num:02d}（意见: {req.feedback or '无'}，"
        f"参考图: {'用户上传图' if req.override_image_path else '原产品图'}，后台并行）...",
        f"📋 接下来流程：① LLM 视觉分析原帧 → ② LLM 理解你的修改意见并调整提示词 → "
        f"③ 把原帧+产品图+调整后提示词一起交给 image2 重新生成 → ④ 完成后重新合并分镜拼图",
    ]

    # 文件名加 shot_num 后缀，避免并发覆盖
    param_file = OUTPUT_DIR / f"_phased_params_{task_id}_shot{shot_num:02d}.json"
    param_file.write_text(json.dumps({
        "run_dir": run_dir,
        "shot_num": shot_num,
        "feedback": req.feedback,
        "override_image_path": req.override_image_path,
    }, ensure_ascii=False), encoding="utf-8")
    result_file = OUTPUT_DIR / f"_phased_result_{task_id}_shot{shot_num:02d}.json"
    runner_script = OUTPUT_DIR / f"_phased_runner_{task_id}_shot{shot_num:02d}.py"

    runner_script.write_text(
        f'''
import json, sys, os, io, logging
os.environ["PYTHONUTF8"] = "1"
os.environ["PYTHONIOENCODING"] = "utf-8"
sys.path.insert(0, r"{Path(__file__).parent}")
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
if hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

params = json.loads(open(r"{param_file}", encoding="utf-8").read())
try:
    from workflow import redo_single_shot
    result = redo_single_shot(**params)
    with open(r"{result_file}", "w", encoding="utf-8") as f:
        json.dump({{"success": True, "result": result}}, f, ensure_ascii=False, default=str)
except Exception as e:
    import traceback
    tb = traceback.format_exc()
    with open(r"{result_file}", "w", encoding="utf-8") as f:
        json.dump({{"success": False, "error": str(e), "traceback": tb}}, f, ensure_ascii=False)
''',
        encoding="utf-8",
    )

    async def _run_redo_shot():
        try:
            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"

            process = await asyncio.create_subprocess_exec(
                sys.executable, "-X", "utf8", str(runner_script),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
                cwd=str(Path(__file__).parent),
            )

            timeout_sec = 480  # 单 shot 重做 8 分钟超时（含 Image2 503 重试）
            start_time = asyncio.get_event_loop().time()
            while True:
                try:
                    line = await asyncio.wait_for(process.stdout.readline(), timeout=10.0)
                except asyncio.TimeoutError:
                    line = b""
                if line:
                    msg = line.decode("utf-8", errors="replace").rstrip()
                    task["logs"].append(msg)
                    echo(f"[{task_id}/shot{shot_num:02d}] {msg}")
                if process.stdout.at_eof():
                    break
                if asyncio.get_event_loop().time() - start_time > timeout_sec:
                    process.kill()
                    task["logs"].append(f"[超时] Shot {shot_num:02d} 重做超过 {timeout_sec}s，已终止")
                    break

            await process.wait()

            if result_file.exists():
                res = json.loads(result_file.read_text(encoding="utf-8"))
                if res.get("success"):
                    result_data = res["result"]
                    review = result_data.get("review")
                    task.update({
                        "status": "awaiting_review",
                        "phase": "after_step3",
                        "step_name": "等待审核（单 Shot 重做完成）",
                        "progress": 50,
                        "review": review,
                        "logs": task["logs"] + [f"✓ Shot {shot_num:02d} 重做完成，等待再次审核"],
                    })
                else:
                    # 失败切到 redo_failed 状态（不再静默回 awaiting_review）
                    # 前端看到 redo_failed 后，"重新修改"按钮重新可用，
                    # 用户可调整 feedback / 更换 override_image_path 后再次提交
                    task.update({
                        "status": "redo_failed",
                        "phase": "after_step3",
                        "step_name": f"重做失败（Shot {shot_num:02d}，可重新提交修改意见）",
                        "progress": 50,
                        "error": res.get("error", "未知错误"),
                        "logs": task["logs"] + [
                            f"✗ Shot {shot_num:02d} 重做失败: {res.get('error', '未知错误')}",
                            f"💡 可调整修改意见或更换产品图后再次点击「重新修改」按钮重试",
                        ],
                    })
            else:
                task.update({
                    "status": "redo_failed",
                    "phase": "after_step3",
                    "step_name": f"重做异常（Shot {shot_num:02d}，可重新提交修改意见）",
                    "progress": 50,
                    "error": "未找到结果文件",
                    "logs": task["logs"] + [f"✗ Shot {shot_num:02d} 重做异常：未找到结果文件"],
                })
        except Exception as e:
            # 异常也切到 redo_failed（不再静默回 awaiting_review）
            task.update({
                "status": "redo_failed",
                "phase": "after_step3",
                "step_name": f"重做异常（Shot {shot_num:02d}，可重新提交修改意见）",
                "progress": 50,
                "error": str(e),
                "logs": task["logs"] + [
                    f"✗ Shot {shot_num:02d} 重做异常: {e}",
                    f"💡 可调整修改意见或更换产品图后再次点击「重新修改」按钮重试",
                ],
            })
        finally:
            # 无论成功失败，从 redoing 列表移除
            if shot_num in redoing:
                redoing.remove(shot_num)
            # 只删 runner_script 和 result_file，保留 param_file 供下次覆盖
            for f in [result_file, runner_script]:
                try:
                    if f.exists():
                        f.unlink()
                except Exception:
                    pass

    asyncio.create_task(_run_redo_shot())
    return {"success": True, "task_id": task_id, "shot_num": shot_num, "message": f"开始重做 Shot {shot_num:02d}（后台并行）"}


# ═══════════════════════════════════════════════════════════════════════
# 首尾帧无限拼接·原创管线（独立管线，无审核点，一气呵成）
# POST /api/replica/run-original  → 启动
# GET  /api/replica/original-status/{task_id}  → 查询状态
# ═══════════════════════════════════════════════════════════════════════

# 原创管线任务状态存储（独立于 replica_phased_tasks，避免 phase 字段冲突）
replica_original_tasks: dict = {}


class OriginalPipelineRequest(BaseModel):
    product_image_paths: list  # 产品图路径列表（必传，1-N 张）
    product_name: str  # 产品名称（必填）
    selling_points: str = ""
    audience: str = ""
    scene_requirement: str = ""
    aspect_ratio: str = "9:16"
    segment_duration: int = 5  # 每段时长 5/10/15
    total_duration: int = 30  # 总时长目标


@app.post("/api/replica/run-original")
async def replica_run_original(req: OriginalPipelineRequest):
    """启动首尾帧无限拼接·原创管线（一气呵成，无审核点）

    流程：gpt-6.1-sol 脚本 → Image2 关键帧 → H3 双帧并发生成 → ffmpeg 拼接
    """
    if not req.product_name or not req.product_name.strip():
        raise HTTPException(status_code=400, detail="必须提供产品名称")
    if not req.product_image_paths:
        raise HTTPException(status_code=400, detail="必须上传产品图")
    if req.segment_duration not in (5, 10, 15):
        raise HTTPException(status_code=400, detail="segment_duration 必须为 5/10/15")
    if req.total_duration < req.segment_duration * 2:
        raise HTTPException(status_code=400, detail=f"total_duration({req.total_duration}) 至少为 segment_duration({req.segment_duration}) 的 2 倍")

    task_id = uuid.uuid4().hex[:12]
    replica_original_tasks[task_id] = {
        "status": "running",
        "phase": "original_before_script",
        "step_name": "Step1: gpt-6.1-sol 推理分镜脚本",
        "progress": 0,
        "run_dir": None,
        "result": None,
        "error": None,
        "logs": ["启动首尾帧无限拼接·原创管线..."],
        "request": req.dict(),
        "video_url": None,
    }
    asyncio.create_task(_run_original_subprocess(task_id, req))
    try:
        from db import save_result
        save_result(task_id=task_id, run_dir="", status="running", error="")
    except Exception:
        pass
    return {"success": True, "task_id": task_id, "phase": "original_before_script"}


async def _run_original_subprocess(task_id: str, req: OriginalPipelineRequest):
    """用子进程运行原创管线（避免阻塞主进程）"""
    task = replica_original_tasks[task_id]

    params = {
        "product_image_paths": req.product_image_paths,
        "product_name": req.product_name,
        "selling_points": req.selling_points,
        "audience": req.audience,
        "scene_requirement": req.scene_requirement,
        "aspect_ratio": req.aspect_ratio,
        "segment_duration": req.segment_duration,
        "total_duration": req.total_duration,
        "base_output_dir": str(OUTPUT_DIR),
    }

    param_file = OUTPUT_DIR / f"_original_params_{task_id}.json"
    with open(param_file, "w", encoding="utf-8") as f:
        json.dump(params, f, ensure_ascii=False, indent=2)

    result_file = OUTPUT_DIR / f"_original_result_{task_id}.json"

    # 子进程脚本
    runner_script = OUTPUT_DIR / f"_original_runner_{task_id}.py"
    script_content = f'''# -*- coding: utf-8 -*-
import json, sys, os
sys.path.insert(0, r"{Path(__file__).resolve().parent}")
os.environ.setdefault("PYTHONUTF8", "1")
try:
    from workflow import run_original_pipeline
    result = run_original_pipeline(**{params!r})
    with open(r"{result_file}", "w", encoding="utf-8") as f:
        json.dump({{"success": True, "result": result}}, f, ensure_ascii=False, default=str)
except Exception as e:
    import traceback
    tb = traceback.format_exc()
    with open(r"{result_file}", "w", encoding="utf-8") as f:
        json.dump({{"success": False, "error": str(e), "traceback": tb}}, f, ensure_ascii=False)
'''
    runner_script.write_text(script_content, encoding="utf-8")

    task["logs"].append(f"启动子进程: python {runner_script.name}")

    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(runner_script),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(Path(__file__).resolve().parent),
    )

    # 实时读取输出
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            line_str = line.decode("utf-8", errors="replace").rstrip()
            if line_str:
                task["logs"].append(line_str)
                # 根据 log 内容更新进度状态
                if "[Step1]" in line_str:
                    task["phase"] = "original_after_script"
                    task["step_name"] = "Step1: gpt-6.1-sol 推理分镜脚本"
                elif "[Step3]" in line_str:
                    task["phase"] = "original_after_keyframes"
                    task["step_name"] = "Step3: Image2 生成关键帧"
                elif "[Step5]" in line_str:
                    task["phase"] = "original_video_generating"
                    task["step_name"] = "Step5: H3 双帧并发生成 + 拼接"
                elif "✓ 视频生成完成" in line_str:
                    task["step_name"] = "视频生成完成"
        await proc.wait()
    except Exception as e:
        task["logs"].append(f"子进程异常: {e}")

    # 读取结果
    if result_file.exists():
        try:
            with open(result_file, "r", encoding="utf-8") as f:
                result_data = json.load(f)
            if result_data.get("success"):
                result = result_data["result"]
                task["status"] = "success"
                task["phase"] = "original_completed"
                task["result"] = result
                task["run_dir"] = result.get("run_dir")
                task["video_url"] = _file_to_url(result.get("video", "")) if result.get("video") else None
                task["step_name"] = "原创管线完成"
                task["logs"].append("✓ 原创管线完成")
            else:
                task["status"] = "failed"
                task["error"] = result_data.get("error", "未知错误")
                task["phase"] = "original_failed"
                task["logs"].append(f"✗ 失败: {task['error']}")
                if result_data.get("traceback"):
                    task["logs"].append(result_data["traceback"][-500:])
        except Exception as e:
            task["status"] = "failed"
            task["error"] = f"结果解析失败: {e}"
            task["phase"] = "original_failed"
    else:
        task["status"] = "failed"
        task["error"] = "子进程未输出结果文件"
        task["phase"] = "original_failed"

    # 清理临时文件（保留 param_file 供下次 retry 覆盖，避免竞态删除）
    try:
        runner_script.unlink(missing_ok=True)
    except Exception:
        pass


@app.get("/api/replica/original-status/{task_id}")
async def replica_original_status(task_id: str):
    """查询原创管线任务状态"""
    if task_id not in replica_original_tasks:
        raise HTTPException(status_code=404, detail="任务不存在")
    return replica_original_tasks[task_id]


# ═══════════════════════════════════════════════════════════════════
# 首尾帧无限拼接·原创管线（Director 满血版）
# POST /api/replica/run-original-director  → 启动
# GET  /api/replica/original-director-status/{task_id}  → 查询状态
# ═══════════════════════════════════════════════════════════════════

replica_original_director_tasks: dict = {}


class OriginalDirectorPipelineRequest(BaseModel):
    product_image_paths: list  # 产品图路径列表（必传，1-N 张）
    product_name: str  # 产品名称（必填）
    selling_points: str = ""
    audience: str = ""
    scene_requirement: str = ""
    aspect_ratio: str = "9:16"
    segment_duration: int = 4  # 每段时长 1-15 任意整数秒（Director 支持）
    total_duration: int = 30  # 总时长目标


@app.post("/api/replica/run-original-director")
async def replica_run_original_director(req: OriginalDirectorPipelineRequest):
    """启动首尾帧无限拼接·原创管线（Director 满血版）

    与 /api/replica/run-original 的差异：
      - segment_duration 支持 1-15 任意整数秒（不再限制 5/10/15）
      - Step5 用 H3 Director 一次任务生成完整视频（无 ffmpeg 拼接，无转场）

    流程：gpt-6.1-sol 脚本 → Image2 关键帧 → H3 Director 一次生成
    """
    if not req.product_name or not req.product_name.strip():
        raise HTTPException(status_code=400, detail="必须提供产品名称")
    if not req.product_image_paths:
        raise HTTPException(status_code=400, detail="必须上传产品图")
    if not (1 <= req.segment_duration <= 15):
        raise HTTPException(status_code=400, detail="segment_duration 必须在 1-15 秒之间")
    if req.total_duration < req.segment_duration * 2:
        raise HTTPException(status_code=400, detail=f"total_duration({req.total_duration}) 至少为 segment_duration({req.segment_duration}) 的 2 倍")

    task_id = uuid.uuid4().hex[:12]
    replica_original_director_tasks[task_id] = {
        "status": "running",
        "phase": "original_director_before_script",
        "step_name": "Step1: gpt-6.1-sol 推理分镜脚本",
        "progress": 0,
        "run_dir": None,
        "result": None,
        "error": None,
        "logs": ["启动首尾帧无限拼接·原创管线（Director 满血版）..."],
        "request": req.dict(),
        "video_url": None,
        "engine": "director",
    }
    asyncio.create_task(_run_original_director_subprocess(task_id, req))
    try:
        from db import save_result
        save_result(task_id=task_id, run_dir="", status="running", error="")
    except Exception:
        pass
    return {"success": True, "task_id": task_id, "phase": "original_director_before_script", "engine": "director"}


async def _run_original_director_subprocess(task_id: str, req: OriginalDirectorPipelineRequest):
    """用子进程运行原创管线 Director 版（避免阻塞主进程）"""
    task = replica_original_director_tasks[task_id]

    params = {
        "product_image_paths": req.product_image_paths,
        "product_name": req.product_name,
        "selling_points": req.selling_points,
        "audience": req.audience,
        "scene_requirement": req.scene_requirement,
        "aspect_ratio": req.aspect_ratio,
        "segment_duration": req.segment_duration,
        "total_duration": req.total_duration,
        "base_output_dir": str(OUTPUT_DIR),
    }

    param_file = OUTPUT_DIR / f"_original_director_params_{task_id}.json"
    with open(param_file, "w", encoding="utf-8") as f:
        json.dump(params, f, ensure_ascii=False, indent=2)

    result_file = OUTPUT_DIR / f"_original_director_result_{task_id}.json"

    runner_script = OUTPUT_DIR / f"_original_director_runner_{task_id}.py"
    script_content = f'''# -*- coding: utf-8 -*-
import json, sys, os
sys.path.insert(0, r"{Path(__file__).resolve().parent}")
os.environ.setdefault("PYTHONUTF8", "1")
try:
    from workflow import run_original_pipeline_director
    result = run_original_pipeline_director(**{params!r})
    with open(r"{result_file}", "w", encoding="utf-8") as f:
        json.dump({{"success": True, "result": result}}, f, ensure_ascii=False, default=str)
except Exception as e:
    import traceback
    tb = traceback.format_exc()
    with open(r"{result_file}", "w", encoding="utf-8") as f:
        json.dump({{"success": False, "error": str(e), "traceback": tb}}, f, ensure_ascii=False)
'''
    runner_script.write_text(script_content, encoding="utf-8")

    task["logs"].append(f"启动子进程: python {runner_script.name}")

    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(runner_script),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(Path(__file__).resolve().parent),
    )

    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            line_str = line.decode("utf-8", errors="replace").rstrip()
            if line_str:
                task["logs"].append(line_str)
                if "[Step1]" in line_str:
                    task["phase"] = "original_director_after_script"
                    task["step_name"] = "Step1: gpt-6.1-sol 推理分镜脚本"
                elif "[Step3]" in line_str:
                    task["phase"] = "original_director_after_keyframes"
                    task["step_name"] = "Step3: Image2 生成关键帧"
                elif "[Step5]" in line_str:
                    task["phase"] = "original_director_video_generating"
                    task["step_name"] = "Step5: H3 Director 一次生成"
                elif "✓ 视频生成完成" in line_str:
                    task["step_name"] = "视频生成完成"
        await proc.wait()
    except Exception as e:
        task["logs"].append(f"子进程异常: {e}")

    if result_file.exists():
        try:
            with open(result_file, "r", encoding="utf-8") as f:
                result_data = json.load(f)
            if result_data.get("success"):
                result = result_data["result"]
                task["status"] = "success"
                task["phase"] = "original_director_completed"
                task["result"] = result
                task["run_dir"] = result.get("run_dir")
                task["video_url"] = _file_to_url(result.get("video", "")) if result.get("video") else None
                task["step_name"] = "原创管线（Director）完成"
                task["logs"].append("✓ 原创管线（Director）完成")
            else:
                task["status"] = "failed"
                task["error"] = result_data.get("error", "未知错误")
                task["phase"] = "original_director_failed"
                task["logs"].append(f"✗ 失败: {task['error']}")
                if result_data.get("traceback"):
                    task["logs"].append(result_data["traceback"][-500:])
        except Exception as e:
            task["status"] = "failed"
            task["error"] = f"结果解析失败: {e}"
            task["phase"] = "original_director_failed"
    else:
        task["status"] = "failed"
        task["error"] = "子进程未输出结果文件"
        task["phase"] = "original_director_failed"

    # 清理临时文件（保留 param_file 供下次 retry 覆盖，避免竞态删除）
    try:
        runner_script.unlink(missing_ok=True)
    except Exception:
        pass


@app.get("/api/replica/original-director-status/{task_id}")
async def replica_original_director_status(task_id: str):
    """查询原创管线（Director 版）任务状态"""
    if task_id not in replica_original_director_tasks:
        raise HTTPException(status_code=404, detail="任务不存在")
    return replica_original_director_tasks[task_id]


@app.get("/api/replica/status/{task_id}")
async def replica_phased_status(task_id: str):
    """查询分阶段任务状态"""
    if task_id not in replica_phased_tasks:
        raise HTTPException(status_code=404, detail="任务不存在")
    return replica_phased_tasks[task_id]


@app.get("/api/replica/active-task")
async def replica_active_task():
    """返回最近一个活动任务（running / awaiting_review），供前端刷新恢复用。
    后端进程重启后内存丢失，返回 none。"""
    # 优先 awaiting_review（需要用户立即操作），其次 running
    active = [
        (tid, t) for tid, t in replica_phased_tasks.items()
        if t.get("status") in ("running", "awaiting_review")
        and t.get("step_name") != "历史记录查看"  # 排除历史记录查看的临时任务
    ]
    if not active:
        return {"active": False}
    # awaiting_review 优先；同状态内按 tid 不变（最后启动的通常最新）
    active.sort(key=lambda kv: (0 if kv[1].get("status") == "awaiting_review" else 1, kv[0]))
    tid, t = active[0]
    return {
        "active": True,
        "task_id": tid,
        "status": t.get("status"),
        "step_name": t.get("step_name"),
        "phase": t.get("phase"),
    }


@app.post("/api/replica/restore-last")
async def replica_restore_last():
    """后端重启后从磁盘恢复最近一个任务到内存，供前端刷新恢复用。
    扫描 outputs 目录下最近的 _phase_state.json，重建到 replica_phased_tasks。
    只恢复 awaiting_review 状态（after_step3 / after_step4）的任务。"""
    import json as _json
    from workflow import _collect_review_images

    out_dir = Path(OUTPUT_DIR)
    # 扫描所有 run_* 目录，按修改时间倒序
    run_dirs = sorted(
        [d for d in out_dir.glob("run_*") if d.is_dir()],
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )
    for run_dir in run_dirs:
        state_file = run_dir / "_phase_state.json"
        if not state_file.exists():
            continue
        try:
            state = _json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        phase = state.get("phase", "")
        # 只恢复待审核状态
        if phase not in ("after_step3", "after_step4"):
            continue
        # 重建 review
        review = _collect_review_images(run_dir, phase)
        # 生成 task_id（用 run_dir 名后12位，保持稳定）
        task_id = run_dir.name[-12:]
        # 如果内存里已存在且活跃，跳过
        existing = replica_phased_tasks.get(task_id)
        if existing and existing.get("status") in ("running", "awaiting_review"):
            return {"restored": True, "task_id": task_id, "already_active": True}
        step_name = "Step4 故事板海报待审核" if phase == "after_step4" else "Step3 产品替换待审核"
        replica_phased_tasks[task_id] = {
            "status": "awaiting_review",
            "phase": phase,
            "step_name": step_name,
            "progress": 100,
            "run_dir": str(run_dir),
            "review": review,
            "result": None,
            "error": None,
            "logs": [
                f"↻ 从磁盘恢复任务: {run_dir.name}（{phase}）",
                f"待审核: {step_name}",
            ],
            "request": None,  # 原始请求丢失，retry/reject 会失败，但审核/redo可用
        }
        print(f"[Restore] 恢复任务 {task_id}: {run_dir.name} ({phase})")
        return {"restored": True, "task_id": task_id, "phase": phase, "run_dir": str(run_dir)}
    return {"restored": False}


@app.post("/api/replica/restore-by-run-dir")
async def replica_restore_by_run_dir(run_dir: str):
    """根据 run_dir 或 task_id 恢复指定任务到内存，供前端点击历史记录查看产物用。
    不依赖 _phase_state.json，直接从磁盘扫描产物（兼容旧任务）。
    额外返回视频信息和所有产物 URL，前端直接渲染无需依赖 task 状态机。

    支持两种入参：
    - run_dir: 目录名（如 run_20260807_183601）或绝对路径
    - task_id: 12 位任务 ID（如 50a412e3c8b3），会扫描所有 _phase_state.json 匹配
    """
    import json as _json
    from workflow import _collect_review_images

    run_path = Path(run_dir)
    if not run_path.is_absolute():
        run_path = Path(OUTPUT_DIR) / run_dir
    # 如果目录不存在，尝试把入参当 task_id 处理：扫描所有 _phase_state.json
    if not run_path.is_dir():
        _query = run_dir.strip().replace("run_", "")
        _matched_run = None
        for _sf in Path(OUTPUT_DIR).glob("run_*/_phase_state.json"):
            try:
                _sd = _json.loads(_sf.read_text(encoding="utf-8"))
                if _sd.get("task_id", "").replace("run_", "") == _query:
                    _matched_run = _sf.parent
                    break
            except Exception:
                pass
        if _matched_run:
            run_path = _matched_run
        else:
            return {"restored": False, "error": f"目录不存在且未找到 task_id={run_dir} 对应的运行记录"}

    # 读取状态文件（可选，没有也能工作）
    state = {}
    state_file = run_path / "_phase_state.json"
    if state_file.exists():
        try:
            state = _json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    # 优先从 _phase_state.json 读取真正的 task_id（格式如 4c1512752c82）；
    # fallback 用目录名后 12 位（旧任务兼容）
    task_id = (state.get("task_id", "") or "").replace("run_", "")
    if not task_id or len(task_id) < 12:
        task_id = run_path.name[-12:]
    # 查看模式：根据实际产物判定 phase，产物不完整时标记为 view_only（不可审核/驳回）
    has_step4 = (run_path / "step4" / "final_storyboard.jpg").exists()
    has_step3 = (run_path / "step3").exists() and any((run_path / "step3").glob("shot_*.png"))
    has_step1 = (run_path / "step1" / "storyboard.json").exists()
    if has_step4:
        view_phase = "after_step4"
    elif has_step3:
        view_phase = "after_step3"
    else:
        # 产物残缺（如 Step1 解析失败/中断），只允许查看不允许审核/驳回
        view_phase = "view_only"

    review = _collect_review_images(run_path, view_phase if view_phase != "view_only" else "after_step3")

    # 扫描视频文件（step5 目录）
    video_files = []
    step5_dir = run_path / "step5"
    if step5_dir.exists():
        for ext in ("*.mp4", "*.webm"):
            for vf in step5_dir.glob(ext):
                try:
                    rel = vf.relative_to(OUTPUT_DIR)
                    video_files.append({"url": f"/outputs/{rel.as_posix()}", "name": vf.name})
                except Exception:
                    pass

    # 扫描 step5_result.json 拿远程视频 URL
    remote_video_url = None
    s5r = step5_dir / "step5_result.json" if step5_dir.exists() else None
    if s5r and s5r.exists():
        try:
            rj = _json.loads(s5r.read_text(encoding="utf-8"))
            remote_video_url = rj.get("video_url")
        except Exception:
            pass

    # 重建到内存：优先保留 db 中已有的 failed 状态（用户手动标记失败后，刷新/重启不应被重置）
    step_name = "历史记录查看"
    restored_status = "awaiting_review"
    restored_phase = view_phase
    restored_error = None
    try:
        from db import _get_conn
        _conn = _get_conn()
        _row = _conn.execute(
            "SELECT status, error FROM workflow_history WHERE task_id = ?", (task_id,)
        ).fetchone()
        _conn.close()
        if _row and _row[0] == "failed":
            restored_status = "failed"
            restored_error = _row[1] or "历史任务标记为失败"
            # 失败阶段映射：after_step3 → phase_2, after_step4 → phase_3
            _phase_map = {"after_step3": "phase_2", "after_step4": "phase_3"}
            restored_phase = _phase_map.get(view_phase, view_phase)
            step_name = "失败（可重试）"
    except Exception:
        pass

    # ⚠️ 不要覆盖正在运行的任务（用户点查看历史时，可能当前任务还在跑 Step1/2/3）
    # 只返回查看数据，不写入 replica_phased_tasks，避免把 running 状态覆盖成 awaiting_review
    existing_task = replica_phased_tasks.get(task_id)
    if existing_task and existing_task.get("status") == "running":
        # 任务正在运行：返回查看快照，但不污染内存状态
        pass
    else:
        replica_phased_tasks[task_id] = {
            "status": restored_status,
            "phase": restored_phase,
            "step_name": step_name,
            "progress": 100,
            "run_dir": str(run_path),
            "review": review,
            "result": None,
            "error": restored_error,
            "logs": [f"↻ 历史记录查看: {run_path.name}"],
            "request": None,
        }

    # 将 review 路径转 URL
    def _to_url(p: str) -> str:
        try:
            rel = Path(p).relative_to(OUTPUT_DIR)
            return f"/outputs/{rel.as_posix()}"
        except Exception:
            return p

    def _file_to_url(p: str) -> str:
        """把任意本地文件路径转成可访问的 URL（uploads/outputs 都支持）"""
        if not p:
            return ""
        try:
            pp = Path(p)
            try:
                rel = pp.relative_to(UPLOAD_DIR)
                return f"/uploads/{rel.as_posix()}"
            except Exception:
                pass
            try:
                rel = pp.relative_to(OUTPUT_DIR)
                return f"/outputs/{rel.as_posix()}"
            except Exception:
                pass
        except Exception:
            pass
        return p

    review_url = {"phase": view_phase}
    if review.get("storyboards"):
        review_url["storyboards"] = [{"label": s["label"], "url": _to_url(s["path"])} for s in review["storyboards"]]
    if review.get("shots"):
        review_url["shots"] = [{"label": s["label"], "url": _to_url(s["path"])} for s in review["shots"]]
    if review.get("poster"):
        review_url["poster"] = {"label": review["poster"]["label"], "url": _to_url(review["poster"]["path"])}
    # 原帧（Step2 抽帧）转 URL，用于 Step3 审核界面对比"原帧 vs 替换后"
    if review.get("original_shots"):
        review_url["original_shots"] = [{"label": s["label"], "url": _to_url(s["path"])} for s in review["original_shots"]]
    # 用户上传的参考图（产品图/服装图/模特图/场景图）
    if review.get("reference_images"):
        review_url["reference_images"] = [
            {"label": s["label"], "url": _to_url(s["path"]), "kind": s.get("kind", "")} for s in review["reference_images"]
        ]

    # 读取 step1 分镜脚本（storyboard.json），供前端展示
    storyboard_script = _load_storyboard_script(str(run_path))

    # 提取表单输入信息
    # 优先从 _phase_state.json 读取；若不存在，从 pipeline_summary.json fallback
    summary = {}
    summary_file = run_path / "pipeline_summary.json"
    if summary_file.exists():
        try:
            summary = _json.loads(summary_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    def _get_state(key, fallback_key=""):
        v = state.get(key, "")
        if not v and fallback_key:
            v = summary.get(fallback_key, "")
        return v

    # 原始路径（用于回填表单 state，submit 时直接发给后端）
    raw_video = _get_state("video_path", "video")
    raw_model = _get_state("model_image_path", "model_image")
    raw_scene = _get_state("scene_image_path", "scene_image")
    raw_clothing = _get_state("clothing_image_path", "clothing_image")
    pimg_paths = []
    if state.get("product_image_paths"):
        pimg_paths = list(state["product_image_paths"])
    if state.get("product_image_path") and state["product_image_path"] not in pimg_paths:
        pimg_paths.insert(0, state["product_image_path"])
    if not pimg_paths and summary.get("product_image"):
        pimg_paths = [summary["product_image"]]

    # form_info: URL 形式（产物顶部显示用）
    # form_paths: 原始路径（回填表单 state 用）
    form_info = {
        "product_name": _get_state("product_name"),
        "video_model": _get_state("video_model"),
        "duration": _get_state("duration"),
        "video_path": _file_to_url(raw_video),
        "product_images": [{"url": _file_to_url(p), "name": Path(p).name} for p in pimg_paths if p],
        "model_image": _file_to_url(raw_model),
        "scene_image": _file_to_url(raw_scene),
        "clothing_image": _file_to_url(raw_clothing),
    }
    form_paths = {
        "video_path": raw_video,
        "product_image_paths": [p for p in pimg_paths if p],
        "model_image_path": raw_model,
        "scene_image_path": raw_scene,
        "clothing_image_path": raw_clothing,
    }

    print(f"[Restore] 历史查看 {task_id}: {run_path.name} (view_phase={view_phase}, status={restored_status}, videos={len(video_files)})")
    return {
        "restored": True,
        "task_id": task_id,
        "status": restored_status,
        "phase": restored_phase,
        "run_dir": str(run_path),
        "run_name": run_path.name,
        "original_phase": state.get("phase", "") or ("completed" if video_files else view_phase),
        "review": review_url,
        "video_files": video_files,
        "remote_video_url": remote_video_url,
        "form_info": form_info,
        "form_paths": form_paths,
        "storyboard_script": storyboard_script,
    }


@app.post("/api/replica/retry/{task_id}")
async def replica_retry(task_id: str):
    """失败后重试：根据失败阶段自动选择从哪一步续跑，不重头开始。

    策略：
    - phase_1 失败 + state 有 step2_result → redo_phase_1（只重做 Step3）
    - phase_1 失败 + 无 step2_result → 重跑 phase_1（Step1/2 失败，需原始 req）
    - phase_2 失败 → redo_phase_2（重做 Step4）
    - phase_3 失败 → 重跑 phase_3（重做 Step5）
    - redo_phase_1 失败 → 再 redo_phase_1
    - redo_phase_2 失败 → 再 redo_phase_2
    """
    if task_id not in replica_phased_tasks:
        if not _restore_task_from_disk(task_id):
            raise HTTPException(status_code=404, detail="任务不存在")
    task = replica_phased_tasks[task_id]
    if task["status"] != "failed":
        raise HTTPException(status_code=400, detail=f"当前状态 {task['status']} 不可重试（仅 failed 可重试）")

    failed_phase = task["phase"]
    run_dir = task.get("run_dir")

    # 判断重试阶段
    if failed_phase in ("phase_2", "redo_phase_2"):
        retry_phase = "redo_phase_2"
        step_name = "重试 Step4: 故事板海报生成"
    elif failed_phase == "phase_3":
        retry_phase = "phase_3"
        step_name = "重试 Step5: 视频生成"
    elif failed_phase in ("phase_1", "resume_phase_1", "redo_phase_1"):
        # 统一从 Phase1 检查点恢复，自动跳过已经成功的 Step。
        if not run_dir:
            raise HTTPException(status_code=400, detail="无法重试 Phase1：run_dir 为空")
        retry_phase = "resume_phase_1"
        step_name = "重试 Phase1: 从最近成功步骤继续"
    else:
        raise HTTPException(status_code=400, detail=f"失败阶段 {failed_phase} 不支持重试")

    # 构造重试请求
    if retry_phase == "phase_1":
        # 重跑整个 phase_1，需要原始 req
        req = PhasedWorkflowRequest(**task["request"])
        # 清理旧的 run_dir（避免混淆），用新 run_dir
        task["run_dir"] = None
    else:
        req = None
        if not run_dir:
            raise HTTPException(status_code=400, detail="无法重试：run_dir 为空")

    task.update({
        "status": "running",
        "phase": retry_phase,
        "step_name": step_name,
        "error": None,
        "traceback": None,
        "review": None,
        "logs": task["logs"] + [f"重试：{step_name}（从 {failed_phase} 恢复）..."],
    })
    asyncio.create_task(_run_phase_subprocess(task_id, retry_phase, req))
    return {"success": True, "task_id": task_id, "retry_phase": retry_phase, "step_name": step_name}


# ============================================================================
# ===== 爆款复刻边界 END =====
# 以上代码段属于爆款复刻工作流（段保护）。下方代码可自由修改。
# ============================================================================

# ─────────────────────────────────────
# 爆款复刻·辅助工具 API（非受保护段）
# ─────────────────────────────────────

@app.post("/api/replica/mark-failed/{task_id}")
async def replica_mark_failed(task_id: str, failed_phase: str = None):
    """将任务标记为失败，便于从失败点重新发起。

    Args:
        task_id: 任务 ID
        failed_phase: 指定失败阶段（phase_1/phase_2/phase_3/redo_phase_1/redo_phase_2）。
                      不传则保留当前 phase；若当前是 after_step3 等"等待审核"状态，
                      自动映射为 phase_2（retry 时跑 redo_phase_2）。
    """
    if task_id not in replica_phased_tasks:
        if not _restore_task_from_disk(task_id):
            raise HTTPException(status_code=404, detail="任务不存在")
    task = replica_phased_tasks[task_id]
    if task["status"] == "running":
        raise HTTPException(status_code=400, detail="任务正在运行，无法标记失败")

    old_phase = task["phase"]
    # 确定失败阶段：优先用传入参数，否则按当前 phase 映射
    new_phase = failed_phase
    if not new_phase:
        # after_step3 → phase_2（重做 Step4 故事板海报）
        # after_step4 → phase_3（重做 Step5 视频生成）
        phase_map = {
            "after_step3": "phase_2",
            "after_step4": "phase_3",
        }
        new_phase = phase_map.get(old_phase, old_phase)

    task.update({
        "status": "failed",
        "phase": new_phase,
        "error": f"用户手动标记失败（原阶段: {old_phase}）",
        "logs": task["logs"] + [f"标记失败：{old_phase} → {new_phase}，可通过重试从 {new_phase} 续跑"],
    })

    try:
        from db import save_result
        save_result(
            task_id=task_id, run_dir=task.get("run_dir", "") or "",
            status="failed", error=task["error"],
        )
    except Exception:
        pass

    return {
        "success": True, "task_id": task_id,
        "old_phase": old_phase, "new_phase": new_phase,
        "status": "failed",
        "message": f"已标记失败，可通过重试从 {new_phase} 续跑",
    }


# ─────────────────────────────────────
# 涨粉·小孩做菜 API（完全隔离）
# ─────────────────────────────────────

class ToddlerChefPipelineRequest(BaseModel):
    count: int = 1


@app.post("/api/toddler-chef/pipeline")
async def toddler_chef_pipeline(req: ToddlerChefPipelineRequest):
    """启动小孩做菜全流程（LLM→image2分镜图→RPA视频）"""
    from toddler_chef.agent import run_pipeline
    try:
        task_id = await run_pipeline(count=req.count)
        return {"task_id": task_id}
    except Exception as e:
        return {"detail": str(e)}


@app.get("/api/toddler-chef/status/{task_id}")
async def toddler_chef_status(task_id: str):
    """查询小孩做菜任务状态"""
    from toddler_chef.agent import get_task_status
    return get_task_status(task_id)


# ─────────────────────────────────────
# 结构性复刻 API（4步：分镜JSON→产品图→故事板→视频）
# ─────────────────────────────────────
# 流程：
#   POST /api/structural-replica/run-phased        → 启动 Phase1（Step1+Step3）→ awaiting_review (after_step3)
#   GET  /api/structural-replica/review/{task_id}  → 查看待审内容（分镜JSON/故事板海报）
#   POST /api/structural-replica/approve/{task_id} → 通过 → 执行 Phase2（Step4 视频生成）
#   POST /api/structural-replica/reject/{task_id}  → 驳回 → 重做 Phase1
#   GET  /api/structural-replica/status/{task_id}  → 查询任务状态
#   GET  /api/structural-replica/history           → 历史记录

sr_phased_tasks: dict = {}


class StructuralReplicaRequest(BaseModel):
    video_path: str
    product_name: str
    selling_points: str
    product_image_paths: list = None
    mimo_api_key: Optional[str] = None
    reasoning_engine: Optional[str] = "grok"  # grok | gpt
    video_model: Optional[str] = "h3"  # h3 | seedance


@app.post("/api/structural-replica/run-phased")
async def sr_run_phased(req: StructuralReplicaRequest):
    """启动结构性复刻 Phase1：MiMo生成15s分镜JSON + Image2生成故事板海报"""
    from backend.services import task_store
    if not req.product_name or not req.product_name.strip():
        raise HTTPException(status_code=400, detail="必须提供产品名称")
    if not req.selling_points or not req.selling_points.strip():
        raise HTTPException(status_code=400, detail="必须提供产品卖点描述")
    if not req.product_image_paths:
        raise HTTPException(status_code=400, detail="至少需要1张产品图")

    task_id = uuid.uuid4().hex[:12]

    # task_store 持久化（服务重启不丢）
    task_store.create(task_id, "structural_replica", status="running",
                      stage="Step1+3: MiMo分镜 + Image2故事板",
                      params={"video_path": req.video_path, "product_name": req.product_name,
                              "selling_points": req.selling_points,
                              "product_image_paths": req.product_image_paths or [],
                              "reasoning_engine": req.reasoning_engine or "grok",
                              "video_model": req.video_model or "h3"},
                      extra={"phase": "phase_1"})

    sr_phased_tasks[task_id] = {
        "status": "running",
        "phase": "phase_1",
        "step_name": "Step1+3: MiMo分镜 + Image2故事板",
        "progress": 0,
        "run_dir": None,
        "review": None,
        "result": None,
        "error": None,
        "logs": ["启动结构性复刻 Phase1..."],
    }
    asyncio.create_task(_sr_run_phase_subprocess(task_id, "phase_1", req))
    return {"success": True, "task_id": task_id, "phase": "phase_1"}


def _sr_sync_to_store(task_id: str, **extra):
    """同步 sr 内存任务状态到 task_store"""
    from backend.services import task_store
    t = sr_phased_tasks.get(task_id)
    if not t:
        return
    fields = {
        "status": str(t.get("status", "running")).upper(),
        "stage": t.get("step_name", ""),
        "progress": t.get("progress", 0),
        "work_dir": t.get("run_dir", ""),
        "error": t.get("error", "") or "",
    }
    if t.get("run_dir"):
        fields["extra"] = {"phase": t.get("phase", ""), "run_dir": t.get("run_dir", "")}
    fields.update(extra)
    try:
        task_store.update(task_id, **fields)
    except Exception:
        pass


# 盘上产物 → 任务应有的状态。键=phase_state.json 里那一档，值=(状态, 必须存在的产物字段)
_SR_DISK_STATES = {"after_step1": ("awaiting_review", "poster"),
                   "after_step3": ("awaiting_review", "poster"),
                   "after_step4": ("completed", "video"),
                   "after_step5": ("completed", "video")}


def _sr_state_from_disk(task: dict):
    """按**盘上真实产物**判这条 sr 任务该是什么状态；不成立回 None（那就维持原判）。

    为什么要有它：状态原来只信内存/DB 那个 `status` 字段，于是"活干完了、收尾时出了别的意外"
    会被永久标成 failed。2026-10-06 实测那次：父进程 GBK 回显抛 UnicodeEncodeError ⇒ 外层 except
    标 failed，而 Phase1 早已把分镜 JSON、故事板海报落到盘上、`phase_state.json` 都写成 after_step3 了
    ⇒ 待审页看不到、"通过"点不动（钱和时间都花了）。

    两处要从盘上取，不能只读 task 字段：
      run_dir —— 崩在回显时还没来得及写回任务；子进程第一行就打"运行目录: …"，`_log()` 已落进日志，就从那儿找。
      phase   —— 任务字段里的 phase 还是入口值 `phase_1`；真值在子进程自己写的 `phase_state.json` 里。
    🔴 只纠 `failed`：`RUNNING` 不动（产物可能是上一轮的、或子进程真在跑），拿它翻案会把在跑的活标成跑完。
    """
    if str((task or {}).get("status") or "").lower() != "failed":
        return None
    run_dir = str((task or {}).get("run_dir") or "")
    if not run_dir:
        for lg in (task or {}).get("logs") or []:
            m = re.search(r"运行目录[:：]\s*([A-Za-z]:[^\"'\s]+)", str(lg))
            if m and Path(m.group(1)).is_dir():
                run_dir = m.group(1)
                break
    rd = Path(run_dir) if run_dir else None
    if not (rd and rd.is_dir()):
        return None
    phase = str((task or {}).get("phase") or "")
    ps = rd / "phase_state.json"
    if ps.is_file():
        try:
            phase = str(json.loads(ps.read_text(encoding="utf-8")).get("phase") or phase)
        except Exception:
            pass
    want = _SR_DISK_STATES.get(phase)
    if not want:
        return None
    status, key = want
    try:
        from structural_replica_workflow import _collect_review_images
        review = _collect_review_images(rd, phase)
    except Exception:
        return None
    if not (review or {}).get(key):
        return None
    return status, review, run_dir, phase


def _sr_ensure_in_memory(task_id: str) -> bool:
    """确保 sr 任务在内存字典中。如果不在，从 task_store 恢复。

    Returns: True 如果任务存在（已在内存或成功恢复），False 如果任务不存在
    """
    from backend.services import task_store
    if task_id in sr_phased_tasks:
        return True
    t = task_store.get(task_id)
    if not t:
        return False
    params = t.get("params", {})
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except Exception:
            params = {}
    extra = t.get("extra", {})
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except Exception:
            extra = {}
    run_dir = t.get("work_dir", "")
    status = str(t.get("status", "")).lower()
    phase = extra.get("phase", "") if isinstance(extra, dict) else ""

    sr_phased_tasks[task_id] = {
        "status": status,
        "phase": phase,
        "step_name": t.get("stage", ""),
        "progress": t.get("progress", 0),
        "run_dir": run_dir,
        "review": None,
        "result": None,
        "error": t.get("error", "") or None,
        "logs": [lg.get("msg", "") for lg in task_store.get_logs(task_id, limit=100)],
    }

    # 如果有 run_dir 且 phase_state.json 存在，尝试恢复 review 和 phase
    if run_dir:
        ps_file = Path(run_dir) / "phase_state.json"
        if ps_file.exists():
            try:
                ps = json.loads(ps_file.read_text(encoding="utf-8"))
                if ps.get("review"):
                    sr_phased_tasks[task_id]["review"] = ps["review"]
                if ps.get("phase"):
                    sr_phased_tasks[task_id]["phase"] = ps["phase"]
                    phase = ps["phase"]
            except Exception:
                pass

    # 状态修正：盘上产物说了算（服务重启/日志炸掉都可能把已跑完的任务误标 failed）。
    # 🔴 规则只在 `_sr_state_from_disk` 里写一次——这里原来是第二份手写副本（`sr_status` 里还有第三份），
    #    三份各自的 phase 名单不一样，改一处漏两处。现在三处都读同一个函数。
    fix = _sr_state_from_disk(sr_phased_tasks[task_id])
    if fix:
        status, review, run_dir, phase = fix
        sr_phased_tasks[task_id].update({"status": status, "run_dir": run_dir, "phase": phase})
        if not sr_phased_tasks[task_id].get("review"):
            sr_phased_tasks[task_id]["review"] = review
    return True


async def _sr_run_phase_subprocess(task_id: str, phase: str, req: Optional[StructuralReplicaRequest] = None):
    """用子进程运行结构性复刻阶段任务"""
    task = sr_phased_tasks[task_id]

    if phase == "phase_1":
        params = {
            "video_path": req.video_path,
            "product_name": req.product_name,
            "selling_points": req.selling_points,
            "product_image_paths": req.product_image_paths or [],
            "mimo_api_key": req.mimo_api_key or settings.MIMO_API_KEY,
            "base_output_dir": str(OUTPUT_DIR),
            "reasoning_engine": (req.reasoning_engine or "grok").lower(),
            "video_model": (req.video_model or "h3").lower(),
        }
        phase_func = "run_phase_1"
    elif phase == "phase_2":
        params = {"run_dir": task["run_dir"]}
        phase_func = "run_phase_2"
    elif phase == "redo_phase_1":
        params = {"run_dir": task["run_dir"], "reject_reason": task.get("reject_reason", "")}
        phase_func = "redo_phase_1"
    else:
        task.update({"status": "failed", "error": f"未知阶段: {phase}"})
        return

    param_file = OUTPUT_DIR / f"_sr_params_{task_id}.json"
    param_file.write_text(json.dumps(params, ensure_ascii=False), encoding="utf-8")
    result_file = OUTPUT_DIR / f"_sr_result_{task_id}.json"

    runner_script = OUTPUT_DIR / f"_sr_runner_{task_id}.py"
    runner_script.write_text(
        f'''
import json, sys, os, io, logging
os.environ["PYTHONUTF8"] = "1"
os.environ["PYTHONIOENCODING"] = "utf-8"
sys.path.insert(0, r"{Path(__file__).parent}")
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
if hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
print("[runner] 启动", flush=True)

params = json.loads(open(r"{param_file}", encoding="utf-8").read())
try:
    from structural_replica_workflow import {phase_func}
    result = {phase_func}(**params)
    with open(r"{result_file}", "w", encoding="utf-8") as f:
        json.dump({{"success": True, "result": result}}, f, ensure_ascii=False, default=str)
except Exception as e:
    import traceback
    tb = traceback.format_exc()
    with open(r"{result_file}", "w", encoding="utf-8") as f:
        json.dump({{"success": False, "error": str(e), "traceback": tb}}, f, ensure_ascii=False)
''',
        encoding="utf-8",
    )

    def _log(msg: str):
        task["logs"].append(msg)
        try:
            from backend.services import task_store as _ts
            _ts.add_log(task_id, msg)
        except Exception:
            pass

    try:
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        process = await asyncio.create_subprocess_exec(
            sys.executable, "-X", "utf8", str(runner_script),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
            cwd=str(Path(__file__).parent),
        )

        # Phase1 (MiMo+Grok+Image2) 约 3-15 分钟（Grok 推理迁移较慢，最坏 12 分钟）；
        # Phase2 (H3 标准 API 2K) 约 5-30 分钟，最长 1 小时
        timeout_sec = 3600 if phase == "phase_2" else 1800
        start_time = asyncio.get_event_loop().time()
        # 记录 PID 便于排查"卡死 RUNNING"问题
        try:
            _log(f"[{phase}] 子进程启动 PID={process.pid}")
        except Exception:
            pass
        while True:
            try:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=10.0)
            except asyncio.TimeoutError:
                line = b""
            if line:
                msg = line.decode("utf-8", errors="replace").rstrip()
                _log(msg)
                echo(f"[SR-{task_id}] {msg}")     # 🔴 走 echo()：一个 ✓ 不许把跑完的任务判死
            # Windows asyncio 下 at_eof() 不一定可靠：进程已退出但 pipe EOF 可能不触发
            # → 用 returncode is not None 作为兜底信号，防止任务永久卡 RUNNING
            if process.stdout.at_eof() or process.returncode is not None:
                # 进程已退出（或 EOF）：排空 stdout 剩余 buffered 数据，避免丢失日志
                while not process.stdout.at_eof():
                    try:
                        rest = await asyncio.wait_for(process.stdout.readline(), timeout=2.0)
                    except asyncio.TimeoutError:
                        break
                    if not rest:
                        break
                    msg = rest.decode("utf-8", errors="replace").rstrip()
                    _log(msg)
                    echo(f"[SR-{task_id}] {msg}")  # 同上：排空缓冲那一段也不能被编码炸断
                if process.returncode is not None and process.returncode != 0:
                    _log(f"[{phase}] 子进程退出 code={process.returncode}")
                break
            if asyncio.get_event_loop().time() - start_time > timeout_sec:
                process.kill()
                task.update({"status": "failed", "error": f"{phase} 超时（{timeout_sec}s）"})
                _sr_sync_to_store(task_id)
                return

        await asyncio.wait_for(process.wait(), timeout=30.0)

        if not result_file.exists():
            task.update({"status": "failed", "error": f"{phase} 未生成结果文件"})
            _sr_sync_to_store(task_id)
            return
        result_data = json.loads(result_file.read_text(encoding="utf-8"))

        if not result_data.get("success"):
            task.update({
                "status": "failed",
                "error": result_data.get("error", "未知错误"),
                "traceback": result_data.get("traceback"),
            })
            _sr_sync_to_store(task_id)
            return

        res = result_data["result"]
        run_dir = res.get("run_dir")
        review = res.get("review")
        new_phase = res.get("phase")

        task["run_dir"] = run_dir
        if new_phase == "after_step4":
            # Phase2 完成 - 视频已生成
            video_path = review.get("video") if review else None
            task.update({
                "status": "completed",
                "phase": "completed",
                "step_name": "视频生成完成",
                "progress": 100,
                "result": {
                    "video_path": video_path,
                    "video_url": f"/outputs/{Path(video_path).relative_to(OUTPUT_DIR).as_posix()}" if video_path else None,
                },
                "review": review,
            })
        else:
            # Phase1 或 redo_phase_1 完成 - 等待审核
            task.update({
                "status": "awaiting_review",
                "phase": new_phase,
                "step_name": "等待审核故事板海报" if "redo" not in phase else "重做完成等待审核",
                "progress": 50,
                "review": review,
            })
        _log(f"[{phase}] 完成，当前阶段: {new_phase}")
        _sr_sync_to_store(task_id)

    except Exception as e:
        import traceback
        task.update({"status": "failed", "error": str(e), "traceback": traceback.format_exc()})
        _sr_sync_to_store(task_id)
    finally:
        # 只删 runner_script，保留 param_file 供下次覆盖（避免竞态删除）
        try:
            runner_script.unlink(missing_ok=True)
        except Exception:
            pass


@app.get("/api/structural-replica/review/{task_id}")
async def sr_get_review(task_id: str):
    """获取结构性复刻待审内容（分镜JSON/故事板海报）"""
    if not _sr_ensure_in_memory(task_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    task = sr_phased_tasks[task_id]

    # 如果内存中 review 为空但 phase=after_step3，从磁盘扫描恢复
    review = task.get("review")
    if not review and task.get("phase") in ("after_step3", "after_step1") and task.get("run_dir"):
        from structural_replica_workflow import _collect_review_images
        try:
            review = _collect_review_images(Path(task["run_dir"]), task["phase"])
            task["review"] = review  # 缓存到内存
        except Exception:
            review = None

    if not review or not review.get("poster"):
        return {
            "status": task["status"],
            "phase": task["phase"],
            "message": "当前无待审核内容",
            "review": None,
        }

    def _to_url(p: str) -> str:
        try:
            rel = Path(p).relative_to(OUTPUT_DIR)
            return f"/outputs/{rel.as_posix()}"
        except Exception:
            return p

    review_url = {"phase": review.get("phase")}
    if review.get("storyboard_json"):
        review_url["storyboard_json_url"] = _to_url(review["storyboard_json"])
    if review.get("poster"):
        review_url["poster_url"] = _to_url(review["poster"])

    return {
        "status": "awaiting_review",
        "phase": task["phase"],
        "step_name": task.get("step_name", ""),
        "review": review_url,
        "run_dir": task.get("run_dir", ""),
        "storyboard_script": _load_storyboard_script(task.get("run_dir", "")),
    }


@app.post("/api/structural-replica/approve/{task_id}")
async def sr_approve(task_id: str):
    """审核通过，继续执行 Phase2（视频生成）"""
    if not _sr_ensure_in_memory(task_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    task = sr_phased_tasks[task_id]
    if task["status"] != "awaiting_review":
        raise HTTPException(status_code=400, detail=f"当前状态 {task['status']} 不可审核通过")

    current_phase = task["phase"]
    if current_phase != "after_step3":
        raise HTTPException(status_code=400, detail=f"当前阶段 {current_phase} 无后续阶段")

    task.update({
        "status": "running",
        "phase": "phase_2",
        "step_name": "Step4: 视频生成（weeksir RPA）",
        "progress": 75,
        "review": None,
        "logs": task["logs"] + ["审核通过，开始生成视频..."],
    })
    _sr_sync_to_store(task_id)
    asyncio.create_task(_sr_run_phase_subprocess(task_id, "phase_2"))
    return {"success": True, "task_id": task_id, "next_phase": "phase_2"}


@app.post("/api/structural-replica/reject/{task_id}")
async def sr_reject(task_id: str, payload: dict = None):
    """审核驳回，重做 Phase1（重新生成故事板海报）

    接受 JSON body: {"reason": "用户输入的修改意见"}
    修改意见会传给 LLM，影响重新推理的分镜脚本。
    """
    if not _sr_ensure_in_memory(task_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    task = sr_phased_tasks[task_id]
    if task["status"] != "awaiting_review":
        raise HTTPException(status_code=400, detail=f"当前状态 {task['status']} 不可驳回")

    current_phase = task["phase"]
    if current_phase != "after_step3":
        raise HTTPException(status_code=400, detail=f"当前阶段 {current_phase} 不可重做")

    # 从 JSON body 提取修改意见
    reason = ""
    if payload and isinstance(payload, dict):
        reason = (payload.get("reason") or "").strip()

    task.update({
        "status": "running",
        "phase": "redo_phase_1",
        "step_name": "重做 Step1+3: 根据修改意见重新生成分镜和故事板",
        "review": None,
        "reject_reason": reason,
        "logs": task["logs"] + [f"审核驳回（修改意见: {reason or '未提供'}），重做 Phase1..."],
    })
    _sr_sync_to_store(task_id)
    asyncio.create_task(_sr_run_phase_subprocess(task_id, "redo_phase_1"))
    return {"success": True, "task_id": task_id, "redo_phase": "redo_phase_1"}


@app.get("/api/structural-replica/status/{task_id}")
async def sr_status(task_id: str):
    """查询结构性复刻任务状态（内存优先，task_store 兜底）"""
    from backend.services import task_store
    if task_id in sr_phased_tasks:
        task = sr_phased_tasks[task_id]
        # 🔴 判 failed 之前先问盘上的产物：Phase1 跑完 = 海报已经在盘上，那这条任务该是"待审"，
        #    不是"失败"。（2026-10-06：父进程 GBK 打印抛 UnicodeEncodeError ⇒ 外层 except 把它标成
        #    failed，钱与时间都花了、故事板也在盘上，人却看不到待审页、更点不了"通过"。）
        if str(task.get("status")) == "failed":
            fix = _sr_state_from_disk(task)
            if fix:
                want, review, run_dir, phase = fix
                task.update({"status": want, "review": review, "error": None,
                             "run_dir": run_dir, "phase": phase,
                             "step_name": "等待审核故事板海报" if want == "awaiting_review" else "视频生成完成",
                             "progress": 50 if want == "awaiting_review" else 100})
                task["logs"] = (task.get("logs") or []) + [
                    f"状态已按盘上产物纠正为 {want}（原判 failed 是日志写出炸的，不是流程失败）"]
                _sr_sync_to_store(task_id)
        return task
    # task_store 兜底（服务重启后内存清空）
    t = task_store.get(task_id)
    if t:
        logs = task_store.get_logs(task_id, limit=50)
        extra = t.get("extra", {})
        if isinstance(extra, str):
            try:
                import json as _json
                extra = _json.loads(extra)
            except Exception:
                extra = {}
        phase = extra.get("phase", "") if isinstance(extra, dict) else ""
        run_dir = t.get("work_dir", "")
        status = str(t.get("status", "")).lower()

        # 同一套纠正规则（内存里/不在内存都走 `_sr_state_from_disk`，别抄第二份）：
        # 服务重启把状态标丢、或崩在回显没写回 run_dir 的老任务，都靠盘上产物恢复到"待审/已完成"
        fix = _sr_state_from_disk({"status": status, "phase": phase, "run_dir": run_dir,
                                   "logs": [lg.get("msg", "") if isinstance(lg, dict) else lg for lg in logs]})
        review = None
        if fix:
            status, review, run_dir, phase = fix
            # 纠回待审/已完成后，那句"服务重启，任务标记为失败"就是历史噪音了：
            # 留着它会让人以为这条片子出了问题，其实产物好好的在盘上、下一步就是点"通过"。
            err_text = None
            try:
                task_store.update(task_id, error="")
            except Exception:
                pass
        else:
            err_text = t.get("error", "") or None

        return {
            "status": status,
            "phase": phase,
            "step_name": t.get("stage", ""),
            "progress": t.get("progress", 0),
            "run_dir": run_dir,
            "error": err_text,
            "logs": [lg.get("msg", "") for lg in logs],
            "review": review,
            "result": None,
            "task_id": task_id,
            "recovered": True,
        }
    return {"status": "not_found", "task_id": task_id}


@app.post("/api/structural-replica/resume/{task_id}")
async def sr_resume(task_id: str):
    """从 task_store 恢复中断的 sr 任务到内存（断点续传）"""
    from backend.services import task_store
    if not _sr_ensure_in_memory(task_id):
        raise HTTPException(status_code=404, detail="任务不存在于 task_store")
    task = sr_phased_tasks[task_id]
    status = task["status"]
    cur_phase = task["phase"]

    # 失败任务：重启子进程（run_phase_1/run_phase_2 内部检查产物，已完成步骤自动跳过）
    if status == "failed" and cur_phase in ("phase_1", "redo_phase_1", "phase_2", "redo_phase_2"):
        restart_phase = "phase_1" if cur_phase in ("phase_1", "redo_phase_1") else "phase_2"
        sr_phased_tasks[task_id].update({
            "status": "running",
            "step_name": f"重试 {restart_phase}（断点续传，已完成步骤自动跳过）",
            "error": None,
        })
        _sr_sync_to_store(task_id)
        asyncio.create_task(_sr_run_phase_subprocess(task_id, restart_phase))
        return {"success": True, "task_id": task_id, "status": "running", "phase": restart_phase, "restarted": True}

    return {"success": True, "task_id": task_id, "status": status, "phase": cur_phase}


@app.get("/api/structural-replica/history")
async def sr_history():
    """获取结构性复刻历史记录（task_store + 磁盘扫描兜底，按时间倒序）"""
    import json as _json
    from backend.services import task_store

    history = []
    seen_dirs = set()

    # 1. 从 task_store 查询所有 structural_replica 任务（真实 task_id）
    try:
        store_tasks = task_store.list_by_type("structural_replica", limit=50)
    except Exception:
        store_tasks = []

    for t in store_tasks:
        tid = t.get("task_id", "")
        rd = t.get("work_dir", "")
        if rd:
            seen_dirs.add(str(Path(rd)))
        status = str(t.get("status", "")).lower()
        extra = t.get("extra", {})
        if isinstance(extra, str):
            try:
                extra = _json.loads(extra)
            except Exception:
                extra = {}
        phase = extra.get("phase", "") if isinstance(extra, dict) else ""

        # 推断展示状态
        if status == "awaiting_review" or phase in ("after_step3", "after_step1"):
            display_status = "awaiting_review"
        elif status == "completed" or phase in ("completed", "after_step5"):
            display_status = "completed"
        elif status == "failed":
            display_status = "failed"
        elif status == "running":
            display_status = "running"
        else:
            display_status = "unknown"

        # 扫描产物
        poster = None
        video = None
        if rd:
            run_dir = Path(rd)
            if run_dir.exists():
                poster_candidates = list(run_dir.glob("step3/*poster*.png")) + list(run_dir.glob("step3/*poster*.jpg"))
                if poster_candidates:
                    poster = str(poster_candidates[0])
                video_candidates = list(run_dir.glob("step4/*.mp4")) + list(run_dir.glob("step5/*.mp4"))
                if video_candidates:
                    video = str(video_candidates[0])

        params = t.get("params", {})
        if isinstance(params, str):
            try:
                params = _json.loads(params)
            except Exception:
                params = {}
        product_name = params.get("product_name", "") if isinstance(params, dict) else ""

        history.append({
            "task_id": tid,  # 真实 task_id（可用于 status/resume/reject 接口）
            "product_name": product_name,
            "status": display_status,
            "phase": phase,
            "video_path": video,
            "poster_path": poster,
            "run_dir": rd,
            "created_at": t.get("created_at", ""),
        })

    # 2. 磁盘扫描兜底（task_store 未记录但磁盘存在产物的 sr_* 目录）
    for sf in Path(OUTPUT_DIR).glob("sr_*/phase_state.json"):
        try:
            run_dir = sf.parent
            if str(run_dir) in seen_dirs:
                continue
            sd = _json.loads(sf.read_text(encoding="utf-8"))
            phase = sd.get("phase", "after_step3")
            if phase in ("after_step3", "after_step1"):
                status = "awaiting_review"
            elif phase in ("completed", "after_step5"):
                status = "completed"
            else:
                status = "unknown"
            poster = None
            video = None
            poster_candidates = list(run_dir.glob("step3/*poster*.png")) + list(run_dir.glob("step3/*poster*.jpg"))
            if poster_candidates:
                poster = str(poster_candidates[0])
            video_candidates = list(run_dir.glob("step4/*.mp4")) + list(run_dir.glob("step5/*.mp4"))
            if video_candidates:
                video = str(video_candidates[0])
            history.append({
                "task_id": run_dir.name,  # 磁盘兜底用 run_dir 名（无法恢复，仅供查看产物）
                "product_name": sd.get("product_name"),
                "status": status,
                "phase": phase,
                "video_path": video,
                "poster_path": poster,
                "run_dir": str(run_dir),
                "created_at": "",
            })
        except Exception:
            pass

    # 按创建时间倒序（新任务在前）
    # 排序键必须同型：created_at 是数字、缺它时回落到 task_id 字符串 ⇒ 混类型一比就 TypeError
    # （2026-10-06 实测 /api/structural-replica/history 直接 500，历史页整页打不开）。
    def _hist_key(h):
        try:
            ts = int(float(str(h.get("created_at") or 0)))
        except (TypeError, ValueError):
            ts = 0
        return (ts, str(h.get("task_id") or ""))

    history.sort(key=_hist_key, reverse=True)
    return {"history": history}


@app.get("/structural-replica", response_class=HTMLResponse)
async def sr_page():
    """结构性复刻工作台"""
    html_path = Path(settings.FRONTEND_DIR) / "structural_replica.html"
    if not html_path.exists():
        raise HTTPException(404, "结构性复刻页面未找到")
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.post("/api/toddler-chef/upload-reference")
async def toddler_chef_upload_ref(file: UploadFile = File(...)):
    """上传固定孩子形象参考图"""
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="仅支持图片文件")
    ref_path = STATIC_DIR / "toddler_reference.png"
    content = await file.read()
    ref_path.write_bytes(content)
    return {"success": True, "url": f"/static/toddler_reference.png?_t={int(asyncio.get_event_loop().time())}"}


@app.get("/api/toddler-chef/history")
async def toddler_chef_history():
    """获取小孩做菜历史记录"""
    from toddler_chef.db import get_history
    return {"history": get_history()}


# ─────────────────────────────────────
# 动作迁移 API
# ─────────────────────────────────────

@app.get("/motion-transfer", response_class=HTMLResponse)
async def motion_transfer_page():
    """动作迁移页面"""
    html_path = Path(settings.FRONTEND_DIR) / "motion_transfer.html"
    if not html_path.exists():
        raise HTTPException(404, "动作迁移页面未找到")
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


# 动作迁移任务状态存储
motion_tasks: dict = {}


class MotionTransferRequest(BaseModel):
    video_path: str
    image_paths: List[str]  # 多参考图：至少 1 张，多张用于多角度精准比对
    width: int = 832
    height: int = 480
    steps: int = 4
    cfg: float = 1.0
    lora_strength: float = 0.8
    cn_strength: float = 1.0
    fps: int = 30
    max_frames: int = 81


@app.post("/api/motion-transfer/submit")
async def motion_transfer_submit(req: MotionTransferRequest):
    """提交动作迁移任务到 RunningHub"""
    from backend.services.runninghub_client import submit_motion_transfer

    task_id = uuid.uuid4().hex[:12]
    motion_tasks[task_id] = {
        "status": "SUBMITTING",
        "progress": 0,
        "outputs": [],
        "error": None,
        "rh_task_id": None,
        "params": req.model_dump(),
    }

    try:
        result = await submit_motion_transfer(
            video_path=req.video_path,
            image_paths=req.image_paths,
            width=req.width,
            height=req.height,
            steps=req.steps,
            cfg=req.cfg,
            lora_strength=req.lora_strength,
            cn_strength=req.cn_strength,
            fps=req.fps,
            max_frames=req.max_frames,
        )
        rh_task_id = result.get("taskId", "")
        motion_tasks[task_id]["rh_task_id"] = rh_task_id
        motion_tasks[task_id]["status"] = result.get("taskStatus", "QUEUED")
        motion_tasks[task_id]["image_count"] = result.get("image_count", len(req.image_paths))

        # 后台轮询
        asyncio.create_task(_poll_motion_task(task_id, rh_task_id))

        return {"success": True, "task_id": task_id, "rh_task_id": rh_task_id}

    except Exception as e:
        motion_tasks[task_id]["status"] = "FAILED"
        motion_tasks[task_id]["error"] = str(e)
        return {"success": False, "error": str(e)}


async def _poll_motion_task(task_id: str, rh_task_id: str):
    """后台轮询 RunningHub 任务状态"""
    from backend.services.runninghub_client import RunningHubClient
    from backend.services import task_store as _ts

    client = RunningHubClient()
    max_wait = 3600  # 1 小时
    start = time.time()
    last_st = "?"

    consecutive_errors = 0
    while time.time() - start < max_wait:
        await asyncio.sleep(30)
        elapsed = int(time.time() - start)
        remaining = max(0, max_wait - elapsed)
        try:
            status_data = await client.query_task(rh_task_id)
            consecutive_errors = 0
            task_status = status_data.get("taskStatus", "UNKNOWN")
            progress = status_data.get("progress", 0)

            motion_tasks[task_id]["status"] = task_status
            motion_tasks[task_id]["progress"] = progress

            # 每次查询都回写 stage，让前端看到进度
            stage_map = {"PENDING": "排队中", "RUNNING": "运行中", "SUCCESS": "成功", "FAILED": "失败"}
            st_cn = stage_map.get(task_status, task_status)
            stage_text = f"RH 任务 {rh_task_id} {st_cn}（{elapsed}s已用，{remaining}s剩余）"
            motion_tasks[task_id]["stage"] = stage_text
            try:
                _ts.update(task_id, stage=stage_text, progress=progress,
                           extra={**motion_tasks[task_id].get("extra", {}), "rh_task_id": rh_task_id})
                _ts.add_log(task_id, stage_text)
            except Exception:
                pass
            last_st = task_status

            if task_status == "SUCCESS":
                output = await client.get_task_output(rh_task_id)
                motion_tasks[task_id]["outputs"] = output.get("outputs", [])
                motion_tasks[task_id]["progress"] = 100
                motion_tasks[task_id]["stage"] = "完成"
                try:
                    _ts.update(task_id, status="SUCCESS", progress=100, stage="完成",
                               outputs=output.get("outputs", []))
                    _ts.add_log(task_id, "✅ 动作迁移完成", level="done")
                except Exception:
                    pass
                break
            if task_status == "FAILED":
                motion_tasks[task_id]["error"] = status_data.get("msg", "任务执行失败")
                try:
                    _ts.update(task_id, status="FAILED", error=status_data.get("msg", "任务执行失败"),
                               stage="失败")
                    _ts.add_log(task_id, f"❌ 动作迁移失败: {status_data.get('msg','')}", level="error")
                except Exception:
                    pass
                break

        except Exception as e:
            consecutive_errors += 1
            if consecutive_errors >= 3:
                motion_tasks[task_id]["status"] = "FAILED"
                motion_tasks[task_id]["error"] = f"RunningHub 连续查询失败: {e}"
                try:
                    _ts.update(task_id, status="FAILED", error=f"RunningHub 连续查询失败: {e}", stage="失败")
                except Exception:
                    pass
                break
            motion_tasks[task_id]["stage"] = f"RH 查询重试 {consecutive_errors}/3（{elapsed}s已用）"
            await asyncio.sleep(min(30, 8 * consecutive_errors))

    else:
        motion_tasks[task_id]["status"] = "TIMEOUT"
        motion_tasks[task_id]["error"] = f"任务超时（{max_wait}s），最后状态: {last_st}"
        try:
            _ts.update(task_id, status="FAILED", error=f"任务超时（{max_wait}s）", stage="超时")
        except Exception:
            pass


@app.get("/api/motion-transfer/status")
async def motion_transfer_status(task_id: str):
    """查询动作迁移任务状态"""
    if task_id not in motion_tasks:
        raise HTTPException(404, "任务不存在")
    t = motion_tasks[task_id]
    return {
        "success": True,
        "status": t["status"],
        "progress": t["progress"],
        "outputs": t.get("outputs", []),
        "error": t.get("error"),
        "message": f"RunningHub 任务 {t.get('rh_task_id', '')} 状态: {t['status']}",
    }


@app.post("/api/motion-transfer/inject")
async def motion_transfer_inject(req: dict):
    """把已生成好的本地视频作为任务结果注入（用于外部脚本完成后回传给前端）。

    请求体: {"video_path": "<绝对路径或 outputs 相对路径>"}
    """
    video_path = req.get("video_path", "")
    if not video_path:
        raise HTTPException(400, "缺少 video_path")
    p = Path(video_path)
    if not p.is_absolute():
        p = Path(settings.OUTPUT_DIR) / video_path
    if not p.exists():
        raise HTTPException(400, f"视频文件不存在: {p}")

    rel = p.relative_to(Path(settings.OUTPUT_DIR)).as_posix() if p.is_absolute() and p.is_relative_to(Path(settings.OUTPUT_DIR)) else None
    url = f"/outputs/{rel}" if rel else f"/outputs/{p.name}"

    task_id = uuid.uuid4().hex[:12]
    motion_tasks[task_id] = {
        "status": "SUCCESS",
        "progress": 100,
        "outputs": [{"fileUrl": url, "fileType": "mp4", "nodeId": "manual"}],
        "error": None,
        "rh_task_id": "external",
        "params": {},
    }
    return {"success": True, "task_id": task_id, "url": url}


# ─────────────────────────────────────
# H3 导演台 API（基于 ComfyUI_MiniMaxH3_Director 工作流）
# ─────────────────────────────────────

@app.get("/h3-director", response_class=HTMLResponse)
async def h3_director_page():
    """H3 导演台页面"""
    html_path = Path(settings.FRONTEND_DIR) / "h3_director.html"
    if not html_path.exists():
        raise HTTPException(404, "H3 导演台页面未找到")
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


# H3 导演台任务状态存储
h3_director_tasks: dict = {}


class H3DirectorSegment(BaseModel):
    """单段分镜"""
    durationSec: int = 5
    prompt: str = ""
    negativePrompt: str = "bad video"
    startImagePath: Optional[str] = None  # 本地绝对路径
    endImagePath: Optional[str] = None
    referenceVideoPath: Optional[str] = None  # v2v/rv2v 段级源视频片段
    refImagePaths: List[str] = []           # r2v/rv2v 参考图组（1-9）
    refAudioPaths: List[str] = []           # rv2v 参考音频（1-3）
    refVideoPaths: List[str] = []           # r2v/rv2v 段内参考视频（1-3）
    selected: bool = True                   # 选择运行勾选


class H3DirectorRequest(BaseModel):
    """H3 导演台任务请求"""
    task_type: str = "fl2v"  # t2v/i2v/fl2v/r2v/v2v/rv2v
    segments: List[H3DirectorSegment]
    frame_rate: int = 24
    width: int = 576
    height: int = 736
    ref_max_size: int = 736
    global_prompt: str = ""
    audio_mode: str = "generate"  # generate/original/silent
    steps: int = 20
    cfg: float = 1.0
    seed: int = 0
    sampler: str = "res_multistep"
    scheduler: str = "simple"
    shift_video: float = 12.0
    shift_audio: float = 3.0
    run_select_enabled: bool = False  # 选择运行开关
    fill_strategy: str = "cache"      # 未勾选段填充策略：cache/source/skip


@app.post("/api/h3-director/submit")
async def h3_director_submit(req: H3DirectorRequest):
    """提交 H3 导演台任务到 RunningHub"""
    from backend.services.h3_director_client import submit_h3_director

    if not req.segments:
        raise HTTPException(400, "至少需要 1 个分镜段")

    task_id = uuid.uuid4().hex[:12]
    h3_director_tasks[task_id] = {
        "status": "SUBMITTING",
        "progress": 0,
        "outputs": [],
        "error": None,
        "rh_task_id": None,
        "stage": "提交中",
        "params": req.model_dump(),
        "logs": [f"提交 H3 导演台任务（{req.task_type}，{len(req.segments)} 段）"],
        "video_path": None,
        "created_at": int(time.time()),
    }

    try:
        # 转换 segments 为字典列表
        seg_dicts = [s.model_dump() for s in req.segments]

        result = await submit_h3_director(
            task_type=req.task_type,
            segments=seg_dicts,
            frame_rate=req.frame_rate,
            width=req.width,
            height=req.height,
            ref_max_size=req.ref_max_size,
            global_prompt=req.global_prompt,
            audio_mode=req.audio_mode,
            steps=req.steps,
            cfg=req.cfg,
            seed=req.seed if req.seed > 0 else int(time.time()) % 1000000,
            sampler=req.sampler,
            scheduler=req.scheduler,
            shift_video=req.shift_video,
            shift_audio=req.shift_audio,
            run_select_enabled=req.run_select_enabled,
            fill_strategy=req.fill_strategy,
        )
        rh_task_id = result.get("taskId", "")
        h3_director_tasks[task_id]["rh_task_id"] = rh_task_id
        h3_director_tasks[task_id]["status"] = result.get("taskStatus", "QUEUED")
        h3_director_tasks[task_id]["stage"] = f"已提交 RH 任务 {rh_task_id}"
        h3_director_tasks[task_id]["logs"].append(f"✓ RH 任务已提交: {rh_task_id}")
        h3_director_tasks[task_id]["logs"].append(f"  分镜数: {result.get('segment_count')}")
        h3_director_tasks[task_id]["logs"].append(f"  任务类型: {req.task_type}")
        h3_director_tasks[task_id]["logs"].append(f"  UNET: {result.get('unet_type', '?')} | 工作流: {result.get('workflow_id', '?')}")

        # 后台轮询
        asyncio.create_task(_poll_h3_director_task(task_id, rh_task_id))

        return {"success": True, "task_id": task_id, "rh_task_id": rh_task_id}

    except Exception as e:
        h3_director_tasks[task_id]["status"] = "FAILED"
        h3_director_tasks[task_id]["error"] = str(e)
        h3_director_tasks[task_id]["logs"].append(f"✗ 提交失败: {e}")
        return {"success": False, "error": str(e), "task_id": task_id}


async def _poll_h3_director_task(task_id: str, rh_task_id: str):
    """后台轮询 H3 导演台任务，完成后下载视频"""
    from backend.services.h3_director_client import poll_and_download
    from backend.services import task_store as _ts

    output_dir = Path(settings.OUTPUT_DIR) / "h3_director"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{task_id}.mp4"

    task = h3_director_tasks[task_id]
    max_wait = 3600

    def on_progress(elapsed, status, msg):
        stage_map = {"PENDING": "排队中", "RUNNING": "运行中", "SUCCESS": "成功", "FAILED": "失败"}
        st_cn = stage_map.get(status, status)
        remaining = max(0, max_wait - elapsed)
        stage_text = f"RH 任务 {rh_task_id} {st_cn}（{elapsed}s已用，{remaining}s剩余）{(' - ' + msg) if msg else ''}"
        task["stage"] = stage_text
        task["status"] = status if status in ("SUCCESS", "FAILED") else task["status"]
        task["logs"].append(stage_text)
        # 限制日志数量
        if len(task["logs"]) > 200:
            task["logs"] = task["logs"][-100:]

    try:
        task["status"] = "RUNNING"
        task["stage"] = f"轮询 RH 任务 {rh_task_id}（30s 间隔，最长 1 小时）"
        task["logs"].append("→ 开始轮询（30秒间隔）")

        video_path = await poll_and_download(
            rh_task_id=rh_task_id,
            output_path=str(output_path),
            timeout=max_wait,
            on_progress=on_progress,
        )

        # 完成
        rel = Path(video_path).relative_to(Path(settings.OUTPUT_DIR)).as_posix()
        url = f"/outputs/{rel}"
        task["status"] = "SUCCESS"
        task["progress"] = 100
        task["stage"] = "完成"
        task["video_path"] = str(video_path)
        task["video_url"] = url
        task["outputs"] = [{"fileUrl": url, "fileType": "mp4", "nodeId": "h3_director"}]
        task["logs"].append(f"✓ 视频已下载: {url}")
        size_mb = Path(video_path).stat().st_size / 1024 / 1024
        task["logs"].append(f"  大小: {size_mb:.2f} MB")

        try:
            _ts.create(task_id, "h3_director",
                status="SUCCESS", progress=100, stage="完成",
                params=task.get("params", {}),
                outputs=task["outputs"], intermediate={})
        except Exception:
            pass

    except Exception as e:
        task["status"] = "FAILED"
        task["error"] = str(e)
        task["stage"] = "失败"
        task["logs"].append(f"✗ 任务失败: {e}")
        try:
            _ts.create(task_id, "h3_director",
                status="FAILED", error=str(e), stage="失败",
                params=task.get("params", {}), intermediate={})
        except Exception:
            pass


@app.get("/api/h3-director/status")
async def h3_director_status(task_id: str):
    """查询 H3 导演台任务状态"""
    if task_id not in h3_director_tasks:
        return {"success": False, "status": "not_found", "error": "任务不存在"}
    t = h3_director_tasks[task_id]
    return {
        "success": True,
        "status": t["status"],
        "progress": t["progress"],
        "stage": t.get("stage", ""),
        "outputs": t.get("outputs", []),
        "error": t.get("error"),
        "rh_task_id": t.get("rh_task_id"),
        "video_url": t.get("video_url"),
        "logs": t.get("logs", []),
        "params": t.get("params", {}),
        "created_at": t.get("created_at"),
    }


@app.get("/api/h3-director/history")
async def h3_director_history():
    """获取 H3 导演台历史记录（按创建时间倒序）"""
    history = []
    for tid, t in h3_director_tasks.items():
        params = t.get("params", {})
        history.append({
            "task_id": tid,
            "task_type": params.get("task_type", "fl2v"),
            "segment_count": len(params.get("segments", [])),
            "status": t["status"],
            "stage": t.get("stage", ""),
            "video_url": t.get("video_url"),
            "created_at": t.get("created_at"),
        })
    history.sort(key=lambda h: h.get("created_at", 0), reverse=True)
    return {"history": history}


# ─────────────────────────────────────
# 短剧带货 · 钩子灵感大脑（blockbuster-hook-brain SKILL）
# 输入 → SKILL 粗筛 → 向量检索 → MiMo 精排改写 → Prompt + 衔接指令
# ─────────────────────────────────────

class ShortDramaMatchRequest(BaseModel):
    product_category: str
    target_audience: str
    scene_type: str
    emotional_tone: str
    platform: str = "抖音"
    duration: int = 3
    supplement: str = ""
    product_image: Optional[str] = None  # 绝对路径（可选，多模态精排）


@app.get("/short-drama-agent", response_class=HTMLResponse)
async def short_drama_agent_page():
    html_path = Path(settings.FRONTEND_DIR) / "short_drama_agent.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.post("/api/short-drama/match")
async def short_drama_match(req: ShortDramaMatchRequest):
    """短剧带货钩子匹配：粗筛 + 向量检索 + LLM 精排改写"""
    import asyncio
    import logging
    logger = logging.getLogger("short-drama")

    try:
        # 1) 构造 SKILL 检索输入
        user_input = {
            "product_category": req.product_category,
            "target_audience": req.target_audience,
            "scene_type": req.scene_type,
            "emotional_tone": req.emotional_tone,
            "platform": req.platform,
            "duration": req.duration,
            "supplement": req.supplement,
        }
        if req.product_image:
            user_input["product_image"] = req.product_image

        # 2) 粗筛 + 向量检索
        from backend.services.hook_retriever import retrieve
        retrieve_result = retrieve(user_input, k=5)
        rough_count = len(retrieve_result.get("rough_candidates", []))
        vector_count = len(retrieve_result.get("vector_candidates", []))
        merged = retrieve_result.get("merged", [])
        logger.info(f"短剧钩子匹配：粗筛 {rough_count} 条 → 向量 top-{vector_count} → 合并 {len(merged)} 条")

        if not merged:
            return {
                "success": True,
                "data": {
                    "rough_count": rough_count,
                    "vector_count": vector_count,
                    "merged": [],
                    "ranked": {
                        "selected": [],
                        "rewritten_prompt": "",
                        "follow_up": {},
                        "adjustment_notes": "未匹配到任何钩子候选，请调整输入参数后重试。",
                    },
                },
            }

        # 3) MiMo 精排 + 改写
        from backend.services.hook_ranker import rank_and_rewrite
        try:
            ranked = await rank_and_rewrite(user_input, merged)
        except Exception as e:
            logger.warning(f"MiMo 精排失败，降级返回原始候选：{e}")
            # 降级：直接返回 top-1 候选 + 原始 prompt_template
            top = merged[0] if merged else {}
            ranked = {
                "selected": [{"id": top.get("id", ""), "title": top.get("title", ""), "reason": "MiMo 不可用，降级取 top-1"}],
                "rewritten_prompt": top.get("prompt_template", ""),
                "follow_up": {},
                "adjustment_notes": f"MiMo 精排失败（{e}），已降级为向量 top-1 候选的原始模板。",
            }

        return {
            "success": True,
            "data": {
                "rough_count": rough_count,
                "vector_count": vector_count,
                "merged": merged,
                "ranked": ranked,
            },
        }
    except Exception as e:
        import traceback
        logger.error(f"短剧钩子匹配失败：{e}\n{traceback.format_exc()}")
        return {"success": False, "error": str(e)}


# ─────────────────────────────────────
# 一键产品 TVC（3步：gpt-6.1-sol提示词 → image2 4K故事板 → RPA SD2成片）
# ─────────────────────────────────────

class TVCPipelineRequest(BaseModel):
    product_name: str
    audience: str
    ad_theme: str
    scene_requirement: str = ""
    aspect_ratio: str = "16:9"
    selling_points: str = ""
    product_image: str = ""                # 旧字段：单张产品图（兼容老前端）
    product_images: List[str] = []         # 新字段：多张产品参考图（不同角度/细节）
    clothing_image: str = ""               # 可选服装图（服装类必填）
    headless: bool = False
    duration: str = "15s"                  # 视频时长：5s/10s/15s/20s/25s/30s（2.5 才支持 20s+）
    model_value: str = ""                  # 模型：seedance-2.0-renlian / seedance-2.5（空则默认 2.0）


@app.get("/tvc", response_class=HTMLResponse)
async def tvc_page():
    html_path = Path(settings.FRONTEND_DIR) / "tvc.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.post("/api/tvc/upload")
async def tvc_upload(file: UploadFile = File(...)):
    """上传产品图/服装图"""
    ext = Path(file.filename).suffix.lower() or ".png"
    save_name = f"tvc_{uuid.uuid4().hex[:12]}{ext}"
    save_path = Path(settings.UPLOAD_DIR) / save_name
    content = await file.read()
    save_path.write_bytes(content)
    return {"success": True, "path": str(save_path), "url": f"/uploads/{save_name}"}


@app.post("/api/tvc/run")
async def tvc_run(req: TVCPipelineRequest):
    """启动 TVC 全流程"""
    from backend.services.tvc_agent import run_pipeline
    try:
        # 合并新旧字段：product_images 优先，回退到 product_image
        product_images: List[str] = list(req.product_images or [])
        if req.product_image and req.product_image not in product_images:
            product_images.insert(0, req.product_image)
        if not product_images:
            return {"success": False, "error": "请至少上传 1 张产品图"}
        task_id = await run_pipeline(
            product_image_paths=product_images,
            product_name=req.product_name,
            audience=req.audience,
            ad_theme=req.ad_theme,
            scene_requirement=req.scene_requirement,
            aspect_ratio=req.aspect_ratio,
            selling_points=req.selling_points,
            clothing_image_path=req.clothing_image or None,
            headless=req.headless,
            duration=req.duration,
            model_value=req.model_value or None,
        )
        return {"success": True, "task_id": task_id}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/tvc/status/{task_id}")
async def tvc_status(task_id: str):
    """查询 TVC 任务状态"""
    from backend.services.tvc_agent import get_task_status
    return get_task_status(task_id)


@app.get("/api/tvc/review/{task_id}")
async def tvc_get_review(task_id: str):
    """获取待审核内容（故事板图片路径 + 画布提示词）"""
    from backend.services.tvc_agent import get_review
    return get_review(task_id)


@app.post("/api/tvc/reload/{task_id}")
async def tvc_reload(task_id: str):
    """从 _state.json 重新加载单个任务到内存（用于脚本直接修改 state 后同步）"""
    import json
    from pathlib import Path
    from backend.services.tvc_agent import _tasks, _state_file, OUTPUT_DIR
    sf = _state_file(task_id)
    if not sf.exists():
        return {"status": "not_found", "task_id": task_id}
    try:
        task = json.loads(sf.read_text(encoding="utf-8"))
        _tasks[task_id] = task
        return {
            "status": "ok",
            "task_id": task_id,
            "task_status": task.get("status"),
            "phase": task.get("phase"),
            "storyboard_path": task.get("outputs", {}).get("storyboard_path"),
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.post("/api/tvc/approve/{task_id}")
async def tvc_approve(task_id: str):
    """审核通过 → 启动 Phase2 (Step3 视频生成)"""
    from backend.services.tvc_agent import approve_task
    result = approve_task(task_id)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "操作失败"))
    return result


@app.post("/api/tvc/reject/{task_id}")
async def tvc_reject(task_id: str, feedback: str = ""):
    """审核驳回 → 携带用户意见重做 Step2 (故事板)"""
    from backend.services.tvc_agent import reject_task
    result = reject_task(task_id, feedback=feedback)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "操作失败"))
    return result


@app.post("/api/tvc/retry/{task_id}")
async def tvc_retry(task_id: str):
    """失败后重试 → 按失败阶段自动续跑（Step3 失败重做 Step3；Step1/2 失败重做 Phase1）"""
    from backend.services.tvc_agent import retry_task
    result = retry_task(task_id)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "操作失败"))
    return result


@app.get("/api/tvc/history")
async def tvc_history():
    """获取 TVC 历史记录"""
    from backend.services.tvc_agent import list_history
    return {"history": list_history(limit=20)}


@app.get("/api/tvc/file")
async def tvc_file(path: str):
    """读取 TVC 产物文件（canvas_prompt.txt / storyboard.png 等）"""
    p = Path(path)
    if not p.exists() or not p.is_file():
        return {"detail": "文件不存在"}, 404
    # 故事板图片直接返回
    if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
        from fastapi.responses import FileResponse
        return FileResponse(str(p))
    # 文本文件返回内容
    if p.suffix.lower() in (".txt", ".json", ".md"):
        return {"content": p.read_text(encoding="utf-8")}
    return {"detail": "不支持的文件类型"}


# ─────────────────────────────────────
# 详情图生成（产品图+产品名 → LLM 推理 N 场景 → ideogram4 文生图）
# ─────────────────────────────────────

class DetailImageRequest(BaseModel):
    product_name: str
    product_image: str = ""                 # 单张产品图（兼容）
    product_images: List[str] = []          # 多张产品参考图
    count: int = 4                          # 生成数量（1-8）
    aspect_ratio: str = ""                  # 画幅（空则用默认 3:4）
    scene_style: str = ""                   # 场景要求（可选）
    engine: str = "image2"                  # 生图引擎：image2（默认）/ ideogram4（已隐藏）


@app.get("/detail-image", response_class=HTMLResponse)
async def detail_image_page():
    html_path = Path(settings.FRONTEND_DIR) / "detail_image.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.post("/api/detail-image/upload")
async def detail_image_upload(file: UploadFile = File(...)):
    """上传产品图"""
    ext = Path(file.filename).suffix.lower() or ".png"
    save_name = f"di_{uuid.uuid4().hex[:12]}{ext}"
    save_path = Path(settings.UPLOAD_DIR) / save_name
    content = await file.read()
    save_path.write_bytes(content)
    return {"success": True, "path": str(save_path), "url": f"/uploads/{save_name}"}


@app.post("/api/detail-image/run")
async def detail_image_run(req: DetailImageRequest):
    """启动详情图生成全流程"""
    from backend.services.detail_image_agent import run_pipeline
    try:
        product_images: List[str] = list(req.product_images or [])
        if req.product_image and req.product_image not in product_images:
            product_images.insert(0, req.product_image)
        if not product_images:
            return {"success": False, "error": "请至少上传 1 张产品图"}
        task_id = await run_pipeline(
            product_image_paths=product_images,
            product_name=req.product_name,
            count=req.count,
            aspect_ratio=req.aspect_ratio,
            scene_style=req.scene_style,
            engine=req.engine,
        )
        return {"success": True, "task_id": task_id}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/detail-image/status/{task_id}")
async def detail_image_status(task_id: str):
    """查询详情图任务状态"""
    from backend.services.detail_image_agent import get_task_status
    return get_task_status(task_id)


@app.get("/api/detail-image/history")
async def detail_image_history():
    """获取详情图历史记录"""
    from backend.services.detail_image_agent import list_history
    return {"history": list_history(limit=20)}


@app.get("/api/detail-image/file")
async def detail_image_file(path: str):
    """读取详情图产物文件"""
    p = Path(path)
    if not p.exists() or not p.is_file():
        return {"detail": "文件不存在"}, 404
    if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
        from fastapi.responses import FileResponse
        return FileResponse(str(p))
    if p.suffix.lower() in (".txt", ".json", ".md"):
        return {"content": p.read_text(encoding="utf-8")}
    return {"detail": "不支持的文件类型"}


# ─────────────────────────────────────
# 女装带货一体化流水线（商品链接 + 视频链接 → 最终视频）
# ─────────────────────────────────────

class WomenswearStudioRequest(BaseModel):
    """一体化流水线请求。

    source_mode: 来源模式 - link=链接模式（默认）/ upload=上传模式
                 link 模式：走完整 5 步，product_url + video_url 必填
                 upload 模式：跳过 Step1/Step4，直接用上传的本地路径
    product_url: 抖音商城商品链接（短链或长链） - link 模式必填
    video_url:   模仿的抖音视频链接（短链） - link 模式必填
    raw_product_path:    upload 模式必填，用户上传的原始商品图本地路径
    source_video_path:   upload 模式必填，用户上传的模仿视频本地路径
    model_image_path:    可选，自定义模特图
                         - link 模式：默认用 uploads/default_girl_model.jpg
                         - upload 模式：缺省时由 LLM 识别商品图决策
                           · 商品图含模特 → 直接用商品图本身做模特图
                           · 商品图只有服装 → 从模仿视频抽关键帧做模特图
    mode:                背景来源模式：dance=舞蹈（用固定背景图）/ drama=短剧（从模仿视频抽关键帧作背景）
                         仅 link 模式生效，upload 模式忽略（统一走 dance 行为：固定背景图）
    background_image_path: 舞蹈模式必填，固定背景图本地路径；短剧模式忽略
    scene_image_path:    已废弃（保留兼容），新流程按 mode 决定背景
    """
    source_mode: str = "link"
    product_url: str = ""
    video_url: str = ""
    raw_product_path: str = ""
    source_video_path: str = ""
    model_image_path: str = ""
    mode: str = "dance"
    background_image_path: str = ""
    scene_image_path: str = ""
    engine: str = "plus"  # plus=默认质量优先 / lite=速度优先（两者当前都走 RPA + plus workflow）


@app.post("/api/womenswear-studio/run")
async def womenswear_studio_run(req: WomenswearStudioRequest):
    """一键流水线：商品链接 + 模仿视频 → 最终动作迁移视频。

    流程（按 mode 决定背景来源）：
    1. 解析商品链接 → 下载原始商品图
    2. Image2 生成 3:4 白底平铺图
    3. Image2 生成 9:16 动作迁移参考图（白底图 + 模特图 + 背景图）
       - dance 舞蹈模式：背景=用户上传的固定背景图
       - drama 短剧模式：先下载模仿视频 → 抽中间关键帧作背景
    4. 下载抖音模仿视频（短剧模式可能已在 step3 前置下载，则跳过）
    5. RunningHub plus 模式跑动作迁移（参考图 + 模仿视频）
    6. 下载结果视频回传

    使用 motion_tasks 字典存储状态，前端轮询 /api/motion-transfer/status。
    """
    mode = (req.mode or "dance").lower()
    if mode not in ("dance", "drama", "beauty"):
        mode = "dance"
    if mode == "dance" and not req.background_image_path:
        # 舞蹈模式未传背景图，降级用默认背景范式图
        upload_dir0 = Path(settings.UPLOAD_DIR)
        req.background_image_path = str(upload_dir0 / "default_showcase_bg.jpg")
    # beauty 模式：从模仿视频抽关键帧美女作背景，相当于 drama 的明确命名版
    # 不需要 background_image_path / scene_image_path，自动从视频抽

    source_mode = (req.source_mode or "link").lower()
    if source_mode not in ("link", "upload"):
        source_mode = "link"
    # 入参校验
    if source_mode == "link":
        if not req.product_url:
            raise HTTPException(status_code=400, detail="link 模式必须提供 product_url")
        if not req.video_url:
            raise HTTPException(status_code=400, detail="link 模式必须提供 video_url")
    else:  # upload 模式
        if not req.raw_product_path:
            raise HTTPException(status_code=400, detail="upload 模式必须提供 raw_product_path")
        if not req.source_video_path:
            raise HTTPException(status_code=400, detail="upload 模式必须提供 source_video_path")
        # upload 模式忽略 dance/drama，统一按 dance 走（背景用固定默认图）
        mode = "dance"
        if not req.background_image_path:
            upload_dir0 = Path(settings.UPLOAD_DIR)
            req.background_image_path = str(upload_dir0 / "default_showcase_bg.jpg")

    task_id = uuid.uuid4().hex[:12]
    motion_tasks[task_id] = {
        "status": "INIT",
        "progress": 0,
        "outputs": [],
        "error": None,
        "rh_task_id": None,
        "params": {"product_url": req.product_url, "video_url": req.video_url,
                   "raw_product_path": req.raw_product_path, "source_video_path": req.source_video_path,
                   "model_image_path": req.model_image_path, "scene_image_path": req.scene_image_path,
                   "source_mode": source_mode,
                   "mode": mode, "background_image_path": req.background_image_path,
                   "engine": req.engine},
        "stage": "启动中",
        "intermediate": {},
        "extra": {"current_step": "init", "step_list": ["step1", "step2", "step3", "step4", "step5"],
                  "source_mode": source_mode, "mode": mode, "engine": req.engine},
    }
    # 同步写入 task_store 持久化
    try:
        from backend.services import task_store
        task_store.create(task_id, "womenswear",
            status="INIT", progress=0, stage="启动中",
            params=motion_tasks[task_id]["params"],
            intermediate={},
            extra=motion_tasks[task_id]["extra"])
    except Exception:
        pass

    upload_dir = Path(settings.UPLOAD_DIR)
    upload_dir.mkdir(parents=True, exist_ok=True)
    model_path = req.model_image_path or str(upload_dir / "default_girl_model.jpg")

    async def run_pipeline():
        try:
            await _womenswear_run(
                task_id,
                model_image_path=req.model_image_path,
                mode=mode,
                background_image_path=req.background_image_path,
                source_mode=source_mode,
                raw_product_path=req.raw_product_path,
                source_video_path=req.source_video_path,
                engine=req.engine,
            )
        except Exception as e:
            # _womenswear_run 已记录具体失败信息，避免用空字符串异常覆盖。
            if not motion_tasks[task_id].get("error"):
                motion_tasks[task_id].update({"status": "FAILED", "error": repr(e), "stage": "失败"})

    asyncio.create_task(run_pipeline())
    return {"success": True, "task_id": task_id}


class WomenswearStep5RpaRequest(BaseModel):
    """女装带货 Step5 RPA 链路请求。

    task_id: 已有 womenswear 任务 ID（需已完成 step1-4，产物 showcase.png + source_video.mp4 已就绪）
    timeout: RPA 等待预览视频生成的超时秒数（默认 1800s = 30 分钟）
    workflow_url: 可选，自定义 RunningHub 工作流 URL；默认用 RPA 客户端内置的 weeksir 工作流
    """
    task_id: str
    timeout: int = 1800
    workflow_url: str = ""


@app.post("/api/womenswear-studio/step5-rpa")
async def womenswear_studio_step5_rpa(req: WomenswearStep5RpaRequest):
    """女装带货 Step5 RPA 链路：用 Playwright + ComfyUI iframe 跑动作迁移。

    与默认 API 链路（submit_motion_transfer）的区别：
    - API 链路：直接 POST /openapi/v2/run/workflow/{id}，RunningHub 服务端执行
    - RPA 链路：本地浏览器打开工作流页面，通过 ComfyUI graph API 上传文件并点击运行
      → 适用于"带坏节点"工作流（API 调用会失败但作品预览界面能生成）

    前置条件：task_id 对应任务已完成 step1-4，work_dir 下 showcase.png + source_video.mp4 已存在。
    后台异步执行，前端轮询 /api/motion-transfer/status?task_id=xxx 拿进度。
    """
    from backend.services import task_store as _ts
    from backend.services.runninghub_rpa_client import run_motion_transfer_rpa

    tid = req.task_id
    if tid not in motion_tasks:
        # 尝试从 task_store 恢复（断点续跑场景）
        t_db = _ts.get(tid)
        if not t_db:
            raise HTTPException(404, f"任务不存在: {tid}")
        # 重建内存条目
        motion_tasks[tid] = {
            "status": t_db.get("status", "RUNNING"),
            "progress": t_db.get("progress", 0),
            "outputs": t_db.get("outputs", []),
            "params": t_db.get("params", {}),
            "stage": t_db.get("stage", ""),
            "rh_task_id": None,
            "intermediate": t_db.get("intermediate", {}),
            "extra": t_db.get("extra", {}),
        }

    # 校验 step1-4 产物
    work_dir = Path(settings.OUTPUT_DIR) / "womenswear_studio" / tid
    showcase_path = work_dir / "showcase.png"
    video_path = work_dir / "source_video.mp4"
    result_path = work_dir / "final.mp4"
    if not showcase_path.exists():
        raise HTTPException(400, f"Step3 产物缺失: {showcase_path}（请先完成 step1-4）")
    if not video_path.exists():
        raise HTTPException(400, f"Step4 产物缺失: {video_path}（请先完成 step1-4）")

    # 标记进入 Step5 RPA 链路
    motion_tasks[tid].update({
        "status": "RUNNING",
        "progress": 80,
        "stage": "Step5-RPA: 启动 Playwright + ComfyUI 工作流",
        "rh_task_id": None,
        "error": None,
    })
    extra = motion_tasks[tid].get("extra", {})
    # 注意：不要覆盖 engine（plus/lite/standard 是用户选的运行按钮），改用 link_type 标识链路类型
    extra.update({"current_step": "step5", "link_type": "rpa"})
    motion_tasks[tid]["extra"] = extra
    try:
        _ts.update(tid, status="RUNNING", progress=80,
                   stage="Step5-RPA: 启动 Playwright + ComfyUI 工作流",
                   error="",
                   extra={**extra, "current_step": "step5", "link_type": "rpa"})
        _ts.add_log(tid, "🚀 Step5 RPA 链路启动：用 Playwright 打开工作流页面")
    except Exception:
        pass

    async def _run_step5_rpa():
        try:
            _set_step_rpa(tid, "Step5-RPA: Playwright 浏览器连接中", 80)
            _set_step_rpa(tid, "Step5-RPA: 打开工作流页面 + 等待 ComfyUI iframe", 82)
            _set_step_rpa(tid, "Step5-RPA: 上传参考图到节点 299 + 上传视频到节点 275", 85)
            _set_step_rpa(tid, "Step5-RPA: 点击运行按钮（按 engine 选择）", 88)
            _set_step_rpa(tid, "Step5-RPA: 等待节点 285 保存完整视频（节点 600 随后报错）", 90)

            # 从 params/extra 中读取 engine，传给 RPA
            _engine = (motion_tasks[tid].get("params", {}).get("engine")
                       or motion_tasks[tid].get("extra", {}).get("engine")
                       or "plus")
            # extra 里可能被旧逻辑覆盖成 "rpa"，做下保护
            if _engine not in ("plus", "lite", "standard"):
                _engine = "plus"

            output = await run_motion_transfer_rpa(
                image_path=str(showcase_path),
                video_path=str(video_path),
                output_path=str(result_path),
                timeout=req.timeout,
                workflow_url=req.workflow_url or None,
                engine=_engine,
            )

            # 写回成功状态
            def _url(p: Path) -> str:
                try:
                    return f"/outputs/{p.relative_to(Path(settings.OUTPUT_DIR)).as_posix()}"
                except Exception:
                    return ""

            motion_tasks[tid].update({
                "status": "SUCCESS",
                "progress": 100,
                "outputs": [{"fileUrl": _url(result_path), "fileType": "mp4", "nodeId": "rpa-285"}],
                "stage": "完成（RPA 链路）",
                "current_step": "done",
            })
            try:
                _ts.update(tid, status="SUCCESS", progress=100,
                           stage="完成（RPA 链路）",
                           outputs=[{"fileUrl": _url(result_path), "fileType": "mp4", "nodeId": "rpa-285"}],
                           extra={**motion_tasks[tid].get("extra", {}), "current_step": "done"})
                inter = motion_tasks[tid].get("intermediate", {})
                if "step5" in inter:
                    inter["step5"]["output"] = {**inter["step5"].get("output", {}), "final_video": _url(result_path), "link_type": "rpa"}
                    motion_tasks[tid]["intermediate"] = inter
                    _ts.update(tid, intermediate=inter)
                _ts.add_log(tid, "✅ Step5-RPA 完成：预览视频已下载到本地", level="done")
            except Exception:
                pass
        except Exception as e:
            motion_tasks[tid].update({
                "status": "FAILED",
                "error": f"Step5-RPA 失败: {repr(e)}",
                "stage": "Step5-RPA 失败",
            })
            motion_tasks[tid].setdefault("extra", {})["failed_step"] = "step5"
            try:
                _ts.update(tid, status="FAILED", error=f"Step5-RPA 失败: {repr(e)}",
                           stage="Step5-RPA 失败",
                           extra={**motion_tasks[tid].get("extra", {}), "failed_step": "step5"})
                _ts.add_log(tid, f"❌ Step5-RPA 失败: {e}", level="error")
            except Exception:
                pass

    asyncio.create_task(_run_step5_rpa())
    return {"success": True, "task_id": tid, "link_type": "rpa"}


def _set_step_rpa(task_id: str, stage_text: str, progress: int):
    """Step5-RPA 专用：更新内存 + task_store + 日志（轻量辅助函数，避免依赖闭包变量）。"""
    from backend.services import task_store as _ts
    motion_tasks[task_id].update({"stage": stage_text, "progress": progress})
    try:
        _ts.update(task_id, stage=stage_text, progress=progress,
                   extra={**motion_tasks[task_id].get("extra", {}), "current_step": "step5", "link_type": "rpa"})
        _ts.add_log(task_id, stage_text)
    except Exception:
        pass


async def _womenswear_run(
    task_id: str,
    model_image_path: str = "",
    mode: str = "",
    background_image_path: str = "",
    source_mode: str = "",
    raw_product_path: str = "",
    source_video_path: str = "",
    engine: str = "plus",
):
    """女装带货一体化流水线核心逻辑，支持断点续跑 + 5-step 进度。

    扫描 work_dir 中已有产物（raw_product.jpg / flat_lay.png / showcase.png /
    source_video.mp4 / final.mp4），自动跳过已完成的步骤。
    每步完成写入 task_store（intermediate 存 input/output），失败记录 failed_step。

    source_mode: link=链接模式（走完整 5 步，product_url/video_url 必填）
                 upload=上传模式（Step1/Step4 跳过，直接用上传的本地路径，
                                   模特图来源由 LLM 决策）
    mode:        dance=舞蹈模式（背景=background_image_path）/ drama=短剧模式（背景=模仿视频关键帧）
                 仅 link 模式生效；upload 模式强制 dance

    Step5 固定走 RPA 链路：Playwright + ComfyUI iframe 抓取预览视频。

    upload 模式的模特图决策（仅当 model_image_path 为空时）：
        1. MiMo 识别 raw_product_path 商品图是否含模特
        2. 含模特 → 直接用商品图本身做模特图
        3. 仅服装 → GPT 从 source_video_path 抽 5 帧挑动作最清晰的一帧做模特图
    """
    from backend.services.douyin_mall_clothing import (
        resolve_douyin_mall_clothing, download_clothing_image,
        generate_flat_lay_image, generate_womenswear_image,
        generate_motion_reference_image, extract_video_keyframe, save_image_result,
        analyze_product_image_has_model, select_best_model_frame_from_video,
    )
    from backend.services.link_fetch import link_fetch_local
    from backend.services.runninghub_rpa_client import run_motion_transfer_rpa
    from backend.services import task_store

    # 5 个 step 定义：key, 名称, 进度阈值
    STEPS = [
        ("step1", "Step1: 解析商品链接 + 下载商品图", 15),
        ("step2", "Step2: Image2 生成白底平铺图", 40),
        ("step3", "Step3: Image2 生成动作迁移参考图", 65),
        ("step4", "Step4: 下载抖音模仿视频", 75),
        ("step5", "Step5: RunningHub 动作迁移", 100),
    ]

    def _url(p: Path) -> str:
        try:
            return f"/outputs/{p.relative_to(Path(settings.OUTPUT_DIR)).as_posix()}"
        except Exception:
            return ""

    def _set_step(step_key: str, stage_text: str, progress: int, **extra):
        """更新当前 step 状态到 motion_tasks + task_store + 日志。"""
        motion_tasks[task_id].update({"stage": stage_text, "progress": progress, "current_step": step_key, **extra})
        task_store.update(task_id, stage=stage_text, progress=progress, extra={**motion_tasks[task_id].get("extra", {}), "current_step": step_key, **extra})
        task_store.add_log(task_id, stage_text)

    def _save_intermediate(step_key: str, input_data: dict, output_data: dict):
        """保存某 step 的输入/输出到 intermediate。"""
        inter = motion_tasks[task_id].get("intermediate", {})
        inter[step_key] = {"input": input_data, "output": output_data, "ts": int(time.time())}
        motion_tasks[task_id]["intermediate"] = inter
        task_store.update(task_id, intermediate=inter)

    work_dir = Path(settings.OUTPUT_DIR) / "womenswear_studio" / task_id
    work_dir.mkdir(parents=True, exist_ok=True)
    motion_tasks[task_id].update({"status": "RUNNING", "error": None})

    upload_dir = Path(settings.UPLOAD_DIR)
    params = motion_tasks[task_id].get("params", {})
    # 优先从入参读，缺失时从 params 读（支持 retry/import 不传参恢复）
    source_mode = (source_mode or params.get("source_mode") or "link").lower()
    raw_product_path = raw_product_path or params.get("raw_product_path", "")
    source_video_path = source_video_path or params.get("source_video_path", "")
    model_path = model_image_path or params.get("model_image_path") or str(upload_dir / "default_girl_model.jpg")
    mode = (mode or params.get("mode") or "dance").lower()
    bg_path = background_image_path or params.get("background_image_path") or str(upload_dir / "default_showcase_bg.jpg")
    engine = (engine or params.get("engine") or "plus").lower()

    raw_img_path = work_dir / "raw_product.jpg"
    flat_path = work_dir / "flat_lay.png"
    show_path = work_dir / "showcase.png"
    keyframe_path = work_dir / "keyframe_bg.jpg"
    video_path = work_dir / "source_video.mp4"
    result_path = work_dir / "final.mp4"

    # 最终结果已存在：直接返回成功
    if result_path.exists():
        motion_tasks[task_id].update({
            "status": "SUCCESS", "progress": 100,
            "outputs": [{"fileUrl": _url(result_path), "fileType": "mp4"}],
            "stage": "完成（断点恢复）", "current_step": "done",
        })
        task_store.update(task_id, status="SUCCESS", progress=100, stage="完成（断点恢复）",
                          outputs=[{"fileUrl": _url(result_path), "fileType": "mp4"}],
                          extra={**motion_tasks[task_id].get("extra", {}), "current_step": "done"})
        return

    # ───── Step1: 解析商品链接 + 下载商品原图（upload 模式跳过，直接用上传图） ─────
    if source_mode == "upload":
        # 上传模式：直接用 raw_product_path，无需解析链接
        try:
            if not raw_product_path or not Path(raw_product_path).exists():
                raise RuntimeError(f"上传的原始商品图不存在: {raw_product_path}")
            # 复制到 work_dir 统一管理
            import shutil
            shutil.copyfile(raw_product_path, raw_img_path)
            _set_step("step1", "Step1: 使用上传的商品图（跳过链接解析）", 15, raw_product_url=_url(raw_img_path))
            _save_intermediate("step1",
                {"source_mode": "upload", "raw_product_path": raw_product_path},
                {"raw_product": _url(raw_img_path), "skipped": True, "source_mode": "upload"})
        except Exception as e:
            motion_tasks[task_id].update({"status": "FAILED", "error": f"Step1 失败: {e}", "stage": "Step1 失败", "failed_step": "step1"})
            task_store.update(task_id, status="FAILED", error=f"Step1 失败: {e}", stage="Step1 失败",
                              extra={**motion_tasks[task_id].get("extra", {}), "failed_step": "step1"})
            task_store.add_log(task_id, f"❌ Step1 失败: {e}", level="error")
            raise
    elif raw_img_path.exists():
        _set_step("step1", "Step1: 商品图已下载（跳过）", 15, raw_product_url=_url(raw_img_path))
        _save_intermediate("step1", {"product_url": params.get("product_url", "")}, {"raw_product": _url(raw_img_path), "skipped": True})
    else:
        try:
            _set_step("step1", "Step1: 解析商品链接", 5)
            info = await resolve_douyin_mall_clothing(params.get("product_url", ""))
            img_urls = info.get("clothing_image_urls") or info.get("image_urls") or []
            if not img_urls:
                raise RuntimeError("商品图解析失败")
            _set_step("step1", "Step1: 下载商品原图", 10)
            await download_clothing_image(img_urls[0], raw_img_path)
            _set_step("step1", "Step1: 商品图已下载", 15, raw_product_url=_url(raw_img_path), product_title=info.get("title", ""))
            _save_intermediate("step1",
                {"product_url": params.get("product_url", ""), "image_urls": img_urls[:3]},
                {"raw_product": _url(raw_img_path), "title": info.get("title", ""), "author": info.get("author", "")})
        except Exception as e:
            motion_tasks[task_id].update({"status": "FAILED", "error": f"Step1 失败: {e}", "stage": "Step1 失败", "failed_step": "step1"})
            task_store.update(task_id, status="FAILED", error=f"Step1 失败: {e}", stage="Step1 失败",
                              extra={**motion_tasks[task_id].get("extra", {}), "failed_step": "step1"})
            task_store.add_log(task_id, f"❌ Step1 失败: {e}", level="error")
            raise

    # ───── Step2: 生成白底平铺图（upload 模式跳过，不需要平铺图） ─────
    if source_mode == "upload":
        _set_step("step2", "Step2: 上传模式跳过白底平铺图生成", 40, flat_lay_url="")
        _save_intermediate("step2",
            {"source_mode": "upload", "raw_product": _url(raw_img_path)},
            {"flat_lay": "", "skipped": True, "source_mode": "upload"})
        task_store.add_log(task_id, "⏭️ Step2 上传模式跳过（不走链接模式的平铺图）")
    elif flat_path.exists():
        _set_step("step2", "Step2: 白底平铺图已生成（跳过）", 40, flat_lay_url=_url(flat_path))
        _save_intermediate("step2", {"raw_product": _url(raw_img_path)}, {"flat_lay": _url(flat_path), "skipped": True})
    else:
        try:
            _set_step("step2", "Step2: Image2 生成白底平铺图", 25)
            flat_result = await generate_flat_lay_image(str(raw_img_path))
            await save_image_result(flat_result, flat_path)
            _set_step("step2", "Step2: 白底平铺图已生成", 40, flat_lay_url=_url(flat_path))
            _save_intermediate("step2", {"raw_product": _url(raw_img_path)}, {"flat_lay": _url(flat_path)})
        except Exception as e:
            motion_tasks[task_id].update({"status": "FAILED", "error": f"Step2 失败: {e}", "stage": "Step2 失败", "failed_step": "step2"})
            task_store.update(task_id, status="FAILED", error=f"Step2 失败: {e}", stage="Step2 失败",
                              extra={**motion_tasks[task_id].get("extra", {}), "failed_step": "step2"})
            task_store.add_log(task_id, f"❌ Step2 失败: {e}", level="error")
            raise

    # ───── 视频下载辅助函数（参数化 step，舞蹈/短剧互不干扰） ─────
    # 质量校验：时长 ≥ 5s 且 大小 ≥ 400KB（防预览流，允许短小完整视频）
    def _validate_source_video(p: Path) -> tuple[bool, str]:
        """返回 (是否合格, 原因)。"""
        if not p.exists() or p.stat().st_size < 1024:
            return False, "文件不存在或过小"
        if p.stat().st_size < 400 * 1024:
            return False, f"文件大小 {p.stat().st_size/1024:.0f}KB < 400KB（疑似预览流）"
        try:
            try:
                from moviepy.editor import VideoFileClip  # moviepy v1.x
            except ImportError:
                from moviepy import VideoFileClip  # moviepy v2.x
            with VideoFileClip(str(p)) as clip:
                dur = clip.duration
            if dur < 5.0:
                return False, f"时长 {dur:.2f}s < 5s"
            return True, f"{dur:.2f}s / {p.stat().st_size/1024/1024:.2f}MB"
        except Exception as e:
            # moviepy 解析失败但文件够大，仍视为合格（避免误杀）
            return True, f"{p.stat().st_size/1024/1024:.2f}MB (时长解析失败: {e})"

    async def _download_source_video(step_key: str = "step4", stage_prefix: str = "Step4", base_progress: int = 70) -> dict:
        """确保 source_video.mp4 已下载且达标，返回 {quality, title, author}。失败抛异常。
        step_key/stage_prefix/base_progress 由调用方传入，避免短剧模式污染 step4 进度。
        """
        if video_path.exists() and video_path.stat().st_size > 1024:
            ok, reason = _validate_source_video(video_path)
            if ok:
                task_store.add_log(task_id, f"✅ 模仿视频已就绪: {reason}")
                return {"quality": reason, "title": "", "author": ""}
            # 不达标，删除重下
            try:
                video_path.unlink()
            except Exception:
                pass
            _set_step(step_key, f"{stage_prefix}: 已有视频不达标({reason})，重新下载", base_progress)
        _set_step(step_key, f"{stage_prefix}: 下载抖音模仿视频", base_progress)
        # 走下载这条腿的唯一入口（两次重试 + 断头片重下都在里面）。
        # 🔴 `analyze=True` 必须显式给：漏斗的默认是 False（批量存片只要文件），
        #    而这一条是动作迁移的准入门槛，原来靠 `download_douyin_video` 自己的默认值 True 吃到的。
        vid_info = await link_fetch_local(params.get("video_url", ""), out_dir=str(work_dir), analyze=True)
        src = vid_info.get("file_path")
        if not src or not Path(src).exists():
            raise RuntimeError("模仿视频下载失败")
        if Path(src).resolve() != video_path.resolve():
            import shutil
            shutil.copyfile(src, video_path)
        ok, reason = _validate_source_video(video_path)
        if not ok:
            raise RuntimeError(f"模仿视频不达标: {reason}")
        task_store.add_log(task_id, f"✅ 模仿视频质量: {reason}")
        return {"quality": reason, "title": vid_info.get("title", ""), "author": vid_info.get("author", "")}

    # ───── upload 模式：模特图来源决策（Step3 之前） ─────
    # 用户没上传模特图时，按商品图内容智能决策：
    #   1. MiMo 识别商品图是否含模特
    #   2. 含模特 → 用商品图本身做模特图
    #   3. 仅服装 → GPT 从模仿视频抽 5 帧挑动作最清晰的一帧做模特图
    # 注意：source_video_path 在 upload 模式下必填，此处直接用，避免 Step4 还没跑
    if source_mode == "upload" and not model_image_path and not params.get("model_image_path"):
        try:
            _set_step("step3", "Step3-upload: LLM 识别商品图（MiMo）", 48, llm_phase="mimo_analyze")
            has_model = await analyze_product_image_has_model(str(raw_img_path))
            if has_model:
                # 商品图含模特：直接用商品图本身做模特图
                model_path = str(raw_img_path)
                task_store.add_log(task_id, "✅ MiMo 识别商品图含模特，直接复用商品图作模特图")
                _save_intermediate("step3_pre", {"source": "mimo_analyze", "has_model": True},
                                   {"model_path": model_path, "decision": "use_product_as_model"})
            else:
                # 仅服装：用 GPT 从模仿视频挑动作帧做模特图
                # upload 模式 source_video_path 是用户上传的本地路径，直接用
                _set_step("step3", "Step3-upload: GPT 从模仿视频挑动作帧做模特图", 50, llm_phase="gpt_select_frame")
                # 检查上传的视频是否存在
                if not source_video_path or not Path(source_video_path).exists():
                    raise RuntimeError(f"上传的模仿视频不存在: {source_video_path}")
                # 抽帧 + GPT 挑选（输出到 work_dir/llm_frames/）
                llm_frame_dir = work_dir / "llm_frames"
                selected_frame = await select_best_model_frame_from_video(
                    source_video_path, llm_frame_dir, frame_count=5)
                model_path = selected_frame
                task_store.add_log(task_id, f"✅ GPT 选定模特帧: {selected_frame}")
                _save_intermediate("step3_pre", {"source": "gpt_select_frame", "has_model": False},
                                   {"model_path": model_path, "decision": "use_video_frame_as_model"})
        except Exception as e:
            motion_tasks[task_id].update({"status": "FAILED", "error": f"Step3-upload 模特图决策失败: {e}", "stage": "Step3 失败", "failed_step": "step3"})
            task_store.update(task_id, status="FAILED", error=f"Step3 失败: {e}", stage="Step3 失败",
                              extra={**motion_tasks[task_id].get("extra", {}), "failed_step": "step3"})
            task_store.add_log(task_id, f"❌ Step3-upload 模特图决策失败: {e}", level="error")
            raise

    # ───── Step3: 生成 9:16 动作迁移参考图（upload 模式跳过，直接用商品图/模特图做 RPA 输入） ─────
    if source_mode == "upload":
        # upload 模式：不生成 showcase.png，直接用 model_path 给 Step5 RPA
        _set_step("step3", "Step3: 上传模式跳过参考图生成（直接用商品图/模特图做 RPA 输入）", 65, showcase_url="")
        _save_intermediate("step3",
            {"source_mode": "upload", "model_path": model_path, "raw_product": _url(raw_img_path)},
            {"showcase": "", "skipped": True, "source_mode": "upload", "model_path": model_path})
        task_store.add_log(task_id, "⏭️ Step3 上传模式跳过（不走链接模式的参考图）")
    elif show_path.exists():
        _set_step("step3", "Step3: 参考图已生成（跳过）", 65, showcase_url=_url(show_path))
        _save_intermediate("step3", {"flat_lay": _url(flat_path), "model": model_path, "mode": mode}, {"showcase": _url(show_path), "skipped": True})
    elif mode in ("drama", "beauty"):
        # ── 短剧/美女模式：下载视频 → 抽关键帧 → 融合参考图，全部在 Step3 内完成 ──
        mode_label = "美女模式" if mode == "beauty" else "短剧模式"
        try:
            _set_step("step3", f"Step3-{mode_label}: 下载模仿视频以抽取关键帧", 45)
            vinfo = await _download_source_video(step_key="step3", stage_prefix="Step3", base_progress=45)
            _set_step("step3", f"Step3-{mode_label}: 抽取视频关键帧作背景", 52, source_video_url=_url(video_path), video_quality=vinfo["quality"])
            extract_video_keyframe(str(video_path), str(keyframe_path))
            scene_bg = str(keyframe_path)
            _set_step("step3", f"Step3-{mode_label}: 关键帧已抽取，Image2 替换美女（去字幕+替换人物）", 55, keyframe_url=_url(keyframe_path))
            show_result = await generate_motion_reference_image(str(flat_path), model_path, scene_bg)
            await save_image_result(show_result, show_path)
            _set_step("step3", f"Step3-{mode_label}: 动作迁移参考图已生成", 65, showcase_url=_url(show_path))
            _save_intermediate("step3", {"flat_lay": _url(flat_path), "model": model_path, "scene_bg": scene_bg, "mode": mode}, {"showcase": _url(show_path), "keyframe": _url(keyframe_path)})
        except Exception as e:
            motion_tasks[task_id].update({"status": "FAILED", "error": f"Step3 失败: {e}", "stage": "Step3 失败", "failed_step": "step3"})
            task_store.update(task_id, status="FAILED", error=f"Step3 失败: {e}", stage="Step3 失败",
                              extra={**motion_tasks[task_id].get("extra", {}), "failed_step": "step3"})
            task_store.add_log(task_id, f"❌ Step3 失败: {e}", level="error")
            raise
    else:
        # ── 舞蹈模式：用系统固定背景图融合参考图，不碰视频 ──
        try:
            scene_bg = bg_path
            _set_step("step3", "Step3: 舞蹈模式-Image2 生成动作迁移参考图", 50)
            show_result = await generate_motion_reference_image(str(flat_path), model_path, scene_bg)
            await save_image_result(show_result, show_path)
            _set_step("step3", "Step3: 舞蹈模式-动作迁移参考图已生成", 65, showcase_url=_url(show_path))
            _save_intermediate("step3", {"flat_lay": _url(flat_path), "model": model_path, "scene_bg": scene_bg, "mode": mode}, {"showcase": _url(show_path)})
        except Exception as e:
            motion_tasks[task_id].update({"status": "FAILED", "error": f"Step3 失败: {e}", "stage": "Step3 失败", "failed_step": "step3"})
            task_store.update(task_id, status="FAILED", error=f"Step3 失败: {e}", stage="Step3 失败",
                              extra={**motion_tasks[task_id].get("extra", {}), "failed_step": "step3"})
            task_store.add_log(task_id, f"❌ Step3 失败: {e}", level="error")
            raise

    # ───── Step4: 下载模仿视频 ─────
    # upload 模式跳过下载，直接用上传视频；link 模式按 dance/drama 分支
    if source_mode == "upload":
        try:
            if not source_video_path or not Path(source_video_path).exists():
                raise RuntimeError(f"上传的模仿视频不存在: {source_video_path}")
            # 复制到 work_dir
            import shutil
            shutil.copyfile(source_video_path, video_path)
            ok, reason = _validate_source_video(video_path)
            if not ok:
                raise RuntimeError(f"上传的模仿视频不达标: {reason}")
            _set_step("step4", f"Step4: 使用上传的模仿视频({reason})", 75, source_video_url=_url(video_path))
            _save_intermediate("step4",
                {"source_mode": "upload", "source_video_path": source_video_path},
                {"source_video": _url(video_path), "quality": reason, "skipped": True, "source_mode": "upload"})
        except Exception as e:
            motion_tasks[task_id].update({"status": "FAILED", "error": f"Step4 失败: {e}", "stage": "Step4 失败", "failed_step": "step4"})
            task_store.update(task_id, status="FAILED", error=f"Step4 失败: {e}", stage="Step4 失败",
                              extra={**motion_tasks[task_id].get("extra", {}), "failed_step": "step4"})
            task_store.add_log(task_id, f"❌ Step4 失败: {e}", level="error")
            raise
    elif mode in ("drama", "beauty"):
        # 短剧/美女模式：视频已在 Step3 下载并校验，确认仍存在后标记完成
        ok, reason = _validate_source_video(video_path)
        if not ok:
            # 视频被意外删除或损坏，重新下载
            vinfo2 = await _download_source_video(step_key="step4", stage_prefix="Step4", base_progress=70)
            reason = vinfo2["quality"]
            _set_step("step4", f"Step4: 模仿视频已重新下载({reason})", 75, source_video_url=_url(video_path))
            _save_intermediate("step4", {"video_url": params.get("video_url", ""), "mode": mode}, {"source_video": _url(video_path), "quality": reason, "title": vinfo2["title"], "author": vinfo2["author"]})
        else:
            _set_step("step4", f"Step4: 模仿视频已在Step3就绪({reason})", 75, source_video_url=_url(video_path))
            _save_intermediate("step4", {"video_url": params.get("video_url", ""), "mode": mode}, {"source_video": _url(video_path), "quality": reason, "skipped": True})
    else:
        # 舞蹈模式：此处下载视频
        try:
            vinfo = await _download_source_video()
            _set_step("step4", f"Step4: 模仿视频已就绪({vinfo['quality']})", 75, source_video_url=_url(video_path))
            _save_intermediate("step4", {"video_url": params.get("video_url", "")}, {"source_video": _url(video_path), "quality": vinfo["quality"], "title": vinfo["title"], "author": vinfo["author"]})
        except Exception as e:
            motion_tasks[task_id].update({"status": "FAILED", "error": f"Step4 失败: {e}", "stage": "Step4 失败", "failed_step": "step4"})
            task_store.update(task_id, status="FAILED", error=f"Step4 失败: {e}", stage="Step4 失败",
                              extra={**motion_tasks[task_id].get("extra", {}), "failed_step": "step4"})
            task_store.add_log(task_id, f"❌ Step4 失败: {e}", level="error")
            raise

    # ───── Step5: 动作迁移（RPA 链路：Playwright + ComfyUI iframe 抓取预览视频） ─────
    # upload 模式：image_path 用 model_path（商品图本身 或 GPT 挑的视频关键帧）
    # link 模式：image_path 用 show_path（Step3 生成的动作迁移参考图）
    if source_mode == "upload":
        rpa_image_path = model_path
        rpa_image_label = f"模特图({('商品图复用' if model_path == str(raw_img_path) else 'GPT挑帧')})"
    else:
        rpa_image_path = str(show_path)
        rpa_image_label = "动作迁移参考图(showcase)"
    try:
        _btn_label = "中间按钮(beveled-btn-left)" if engine == "plus" else ("左侧按钮(Lite/Standard)" if engine == "lite" else f"engine={engine} 按钮")
        _set_step("step5", f"Step5-RPA: 启动 Playwright + ComfyUI 工作流（引擎: {engine.upper()} → 点击{_btn_label}）", 80)
        _set_step("step5", f"Step5-RPA: 上传{rpa_image_label}(节点299) + 上传视频(节点275) + 服务端校验 hash 一致", 85)
        _set_step("step5", "Step5-RPA: 等待节点 285 生成完整视频（最长 30 分钟；若节点299报FileNotFoundError是RunningHub服务端文件丢失问题，非本地代码bug）", 90)
        # 中止检查：外部 abort 标志
        if motion_tasks[task_id].get("abort_requested"):
            raise RuntimeError("任务被用户中止")
        await run_motion_transfer_rpa(
            image_path=rpa_image_path,
            video_path=str(video_path),
            output_path=str(result_path),
            timeout=1800,
            engine=engine,
        )
        motion_tasks[task_id].update({
            "status": "SUCCESS", "progress": 100,
            "outputs": [{"fileUrl": _url(result_path), "fileType": "mp4"}],
            "stage": "完成（RPA 链路）", "current_step": "done",
        })
        task_store.update(task_id, status="SUCCESS", progress=100, stage="完成（RPA 链路）",
                          outputs=[{"fileUrl": _url(result_path), "fileType": "mp4"}],
                          extra={**motion_tasks[task_id].get("extra", {}), "current_step": "done"})
        _save_intermediate("step5",
            {"source_video": _url(video_path), "rpa_image": rpa_image_path, "rpa_image_label": rpa_image_label, "source_mode": source_mode},
            {"final_video": _url(result_path)})
        task_store.add_log(task_id, "✅ Step5-RPA 完成：预览视频已下载到本地", level="done")
    except Exception as e:
        motion_tasks[task_id].update({"status": "FAILED", "error": f"Step5 失败: {e}", "stage": "Step5 失败", "failed_step": "step5"})
        task_store.update(task_id, status="FAILED", error=f"Step5 失败: {e}", stage="Step5 失败",
                          extra={**motion_tasks[task_id].get("extra", {}), "failed_step": "step5"})
        task_store.add_log(task_id, f"❌ Step5 失败: {e}", level="error")
        raise


@app.get("/api/womenswear-studio/videos")
async def womenswear_studio_videos():
    """列出所有已生成的动作迁移视频。前端刷新即可回看，不依赖内存字典。"""
    base = Path(settings.OUTPUT_DIR) / "womenswear_studio"
    if not base.exists():
        return {"success": True, "videos": []}
    videos = []
    # 1. 各任务目录下的 final.mp4
    for d in sorted(base.iterdir(), key=lambda p: p.stat().st_mtime if p.is_dir() else 0, reverse=True):
        if not d.is_dir():
            continue
        f = d / "final.mp4"
        if f.exists() and f.stat().st_size > 1024:
            st = f.stat()
            videos.append({
                "task_id": d.name,
                "url": f"/outputs/womenswear_studio/{d.name}/final.mp4",
                "size_mb": round(st.st_size / 1024 / 1024, 2),
                "created_at": int(st.st_mtime),
                "created_at_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
                "raw_product_url": f"/outputs/womenswear_studio/{d.name}/raw_product.jpg" if (d / "raw_product.jpg").exists() else "",
                "flat_lay_url": f"/outputs/womenswear_studio/{d.name}/flat_lay.png" if (d / "flat_lay.png").exists() else "",
                "showcase_url": f"/outputs/womenswear_studio/{d.name}/showcase.png" if (d / "showcase.png").exists() else "",
                "source_video_url": f"/outputs/womenswear_studio/{d.name}/source_video.mp4" if (d / "source_video.mp4").exists() and (d / "source_video.mp4").stat().st_size > 1024 else "",
            })
    # 2. 根目录下 _rh_recovered_*.mp4（恢复的孤立 RH 任务产物）
    for f in sorted(base.glob("_rh_recovered_*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True):
        st = f.stat()
        rh_id = f.stem.replace("_rh_recovered_", "")
        videos.append({
            "task_id": f"rh_{rh_id}",
            "url": f"/outputs/womenswear_studio/{f.name}",
            "size_mb": round(st.st_size / 1024 / 1024, 2),
            "created_at": int(st.st_mtime),
            "created_at_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
            "rh_task_id": rh_id,
        })
    return {"success": True, "videos": videos}


@app.get("/api/womenswear-studio/history")
async def womenswear_studio_history(limit: int = 30):
    """列出所有女装带货任务（从 task_store 读取），含中间产物和状态。"""
    from backend.services import task_store
    tasks = task_store.list_by_type("womenswear", limit=limit)
    out = []
    for t in tasks:
        extra = t.get("extra", {})
        out.append({
            "task_id": t["task_id"],
            "status": t["status"],
            "progress": t.get("progress", 0),
            "stage": t.get("stage", ""),
            "error": t.get("error", ""),
            "current_step": extra.get("current_step", ""),
            "failed_step": extra.get("failed_step", ""),
            "product_title": extra.get("product_title", ""),
            "raw_product_url": extra.get("raw_product_url", ""),
            "flat_lay_url": extra.get("flat_lay_url", ""),
            "showcase_url": extra.get("showcase_url", ""),
            "source_video_url": extra.get("source_video_url", ""),
            "rh_task_id": t.get("rh_task_id", ""),
            "final_video_url": (t.get("outputs") or [{}])[0].get("fileUrl", "") if t.get("outputs") else "",
            "intermediate": t.get("intermediate", {}),
            "created_at": t.get("created_at", 0),
            "created_at_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t.get("created_at", 0))) if t.get("created_at") else "",
        })
    return {"success": True, "tasks": out}


@app.get("/api/womenswear-studio/task/{task_id}")
async def womenswear_studio_task_detail(task_id: str):
    """获取单个任务详情（含中间产物、日志），供前端点击历史记录查看。"""
    from backend.services import task_store
    t = task_store.get(task_id)
    if not t or t.get("task_type") != "womenswear":
        raise HTTPException(404, "任务不存在")
    extra = t.get("extra", {})
    inter = t.get("intermediate", {}) or {}
    s1 = (inter.get("step1", {}).get("output") or {})
    s2 = (inter.get("step2", {}).get("output") or {})
    s3 = (inter.get("step3", {}).get("output") or {})
    s4 = (inter.get("step4", {}).get("output") or {})
    raw_logs = task_store.get_logs(task_id, limit=200)
    logs = [{"message": l.get("msg", ""), "level": l.get("level", "info"),
             "ts": l.get("ts", 0),
             "ts_str": time.strftime("%H:%M:%S", time.localtime(l.get("ts", 0))) if l.get("ts") else ""}
            for l in raw_logs]
    return {
        "success": True,
        "task_id": task_id,
        "status": t["status"],
        "progress": t.get("progress", 0),
        "stage": t.get("stage", ""),
        "error": t.get("error", ""),
        "params": t.get("params", {}),
        "outputs": t.get("outputs", []),
        "intermediate": inter,
        "current_step": extra.get("current_step", ""),
        "failed_step": extra.get("failed_step", ""),
        "step_list": extra.get("step_list", ["step1", "step2", "step3", "step4", "step5"]),
        "product_title": extra.get("product_title", ""),
        "raw_product_url": extra.get("raw_product_url", "") or s1.get("raw_product", ""),
        "flat_lay_url": extra.get("flat_lay_url", "") or s2.get("flat_lay", ""),
        "showcase_url": extra.get("showcase_url", "") or s3.get("showcase", ""),
        "source_video_url": extra.get("source_video_url", "") or s4.get("source_video", ""),
        "step4_meta": {
            "title": s4.get("title", ""),
            "author": s4.get("author", ""),
            "quality": s4.get("quality", ""),
        },
        "rh_task_id": t.get("rh_task_id", "") or extra.get("rh_task_id", ""),
        "final_video_url": (t.get("outputs") or [{}])[0].get("fileUrl", "") if t.get("outputs") else "",
        "logs": logs,
        "created_at": t.get("created_at", 0),
        "created_at_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t.get("created_at", 0))) if t.get("created_at") else "",
    }


@app.post("/api/womenswear-studio/abort")
async def womenswear_studio_abort(task_id: str):
    """中止运行中的任务（设置 abort 标志，轮询循环检测后退出）。"""
    if task_id not in motion_tasks:
        from backend.services import task_store
        t_db = task_store.get(task_id)
        if not t_db:
            raise HTTPException(404, "任务不存在")
        motion_tasks[task_id] = {
            "status": t_db.get("status", "FAILED"),
            "params": t_db.get("params", {}),
            "extra": t_db.get("extra", {}),
        }
    motion_tasks[task_id]["abort_requested"] = True
    motion_tasks[task_id]["status"] = "FAILED"
    motion_tasks[task_id]["stage"] = "已中止（用户主动停止）"
    motion_tasks[task_id]["error"] = "任务被用户中止"
    motion_tasks[task_id].setdefault("extra", {})["failed_step"] = motion_tasks[task_id].get("current_step", "step5")
    try:
        from backend.services import task_store
        task_store.update(task_id, status="FAILED", stage="已中止（用户主动停止）",
                          error="任务被用户中止",
                          extra={**motion_tasks[task_id].get("extra", {}), "failed_step": motion_tasks[task_id].get("current_step", "step5")})
        task_store.add_log(task_id, "⛔ 任务被用户中止", level="error")
    except Exception:
        pass
    return {"success": True, "task_id": task_id, "status": "ABORTED"}


@app.post("/api/womenswear-studio/retry")
async def womenswear_studio_retry(task_id: str):
    """从失败步骤重试。扫描 work_dir 已有产物，跳过完成的步骤，从失败处继续。
    若内存丢失则从 task_store 恢复参数。"""
    # 内存丢失时从 task_store 恢复
    if task_id not in motion_tasks:
        from backend.services import task_store
        t_db = task_store.get(task_id)
        if not t_db or t_db.get("task_type") != "womenswear":
            raise HTTPException(404, "任务不存在")
        motion_tasks[task_id] = {
            "status": t_db.get("status", "FAILED"),
            "progress": t_db.get("progress", 0),
            "outputs": t_db.get("outputs", []),
            "error": t_db.get("error"),
            "params": t_db.get("params", {}),
            "stage": t_db.get("stage", ""),
            "rh_task_id": t_db.get("rh_task_id", ""),
            "intermediate": t_db.get("intermediate", {}),
            "extra": t_db.get("extra", {}),
        }
    t = motion_tasks[task_id]
    if t.get("status") == "RUNNING":
        raise HTTPException(400, "任务运行中，无法重试")
    if not t.get("params"):
        raise HTTPException(400, "缺少原始参数，无法重试")

    # 清除失败状态，准备重试
    failed_step = t.get("failed_step") or t.get("extra", {}).get("failed_step", "")
    t["status"] = "RUNNING"
    t["error"] = None
    t["abort_requested"] = False  # 清除中止标志，避免下次重试立即退出
    t.pop("failed_step", None)
    extra = t.get("extra", {})
    extra.pop("failed_step", None)
    extra["current_step"] = failed_step or "step1"
    t["extra"] = extra
    try:
        from backend.services import task_store
        task_store.update(task_id, status="RUNNING", error="", stage=f"重试 {failed_step or 'step1'}",
                          extra={**extra, "current_step": failed_step or "step1"})
        task_store.add_log(task_id, f"↻ 从 {failed_step or 'step1'} 重试")
    except Exception:
        pass

    async def retry_pipeline():
        try:
            await _womenswear_run(task_id)
        except Exception as e:
            motion_tasks[task_id].update({"status": "FAILED", "error": str(e), "stage": "失败"})

    asyncio.create_task(retry_pipeline())
    return {"success": True, "task_id": task_id, "retry_from": failed_step or "step1"}


@app.post("/api/womenswear-studio/import")
async def womenswear_studio_import(source_task_id: str, product_url: str = "", video_url: str = ""):
    """从已有工作目录导入为新任务（断点续跑入口）。

    把 outputs/womenswear_studio/{source_task_id}/ 下的产物拷贝到新 task_id 目录，
    创建 motion_tasks 条目（状态置为 FAILED 以便 retry 接口可调用），并自动触发重试。
    """
    import shutil
    src_dir = Path(settings.OUTPUT_DIR) / "womenswear_studio" / source_task_id
    if not src_dir.exists():
        raise HTTPException(404, f"源任务目录不存在: {source_task_id}")

    new_task_id = uuid.uuid4().hex[:12]
    dst_dir = Path(settings.OUTPUT_DIR) / "womenswear_studio" / new_task_id
    dst_dir.mkdir(parents=True, exist_ok=True)

    # 拷贝产物（统一重命名为 _womenswear_run 期望的文件名）
    name_map = {
        "raw_product.jpg": "raw_product.jpg",
        "flat_lay.png": "flat_lay.png",
        "showcase.png": "showcase.png",
        "source_video.mp4": "source_video.mp4",
        "final.mp4": "final.mp4",
    }
    # 兼容原始抖音下载文件名
    for f in src_dir.glob("douyin_*.mp4"):
        name_map[f.name] = "source_video.mp4"
        break

    copied = []
    for src_name, dst_name in name_map.items():
        src = src_dir / src_name
        if src.exists():
            shutil.copyfile(src, dst_dir / dst_name)
            copied.append(dst_name)

    # 从 source_task_id 的旧 params 文件读取参数（如果有）
    params_file = src_dir / "_params.json"
    saved_params = {}
    if params_file.exists():
        import json
        try:
            saved_params = json.loads(params_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    params = {
        "product_url": product_url or saved_params.get("product_url", ""),
        "video_url": video_url or saved_params.get("video_url", ""),
        "raw_product_path": saved_params.get("raw_product_path", ""),
        "source_video_path": saved_params.get("source_video_path", ""),
        "source_mode": saved_params.get("source_mode", "link"),
        "model_image_path": saved_params.get("model_image_path", ""),
        "scene_image_path": saved_params.get("scene_image_path", ""),
        "mode": saved_params.get("mode", "dance"),
        "background_image_path": saved_params.get("background_image_path", ""),
        "engine": saved_params.get("engine", "plus"),
    }

    motion_tasks[new_task_id] = {
        "status": "FAILED",  # 标记为失败，便于 retry 接口触发
        "progress": 75 if "source_video.mp4" in copied else 0,
        "outputs": [],
        "error": "导入任务，等待重试",
        "rh_task_id": None,
        "params": params,
        "stage": "已导入，等待重试",
        "intermediate": {},
        "extra": {"current_step": "init", "mode": params["mode"], "engine": params["engine"]},
        "flat_lay_url": f"/outputs/{dst_dir.relative_to(Path(settings.OUTPUT_DIR)).as_posix()}/flat_lay.png" if (dst_dir / "flat_lay.png").exists() else "",
        "showcase_url": f"/outputs/{dst_dir.relative_to(Path(settings.OUTPUT_DIR)).as_posix()}/showcase.png" if (dst_dir / "showcase.png").exists() else "",
    }

    # 自动触发重试
    async def import_retry():
        try:
            await _womenswear_run(new_task_id)
        except Exception as e:
            motion_tasks[new_task_id].update({"status": "FAILED", "error": str(e), "stage": "失败"})

    asyncio.create_task(import_retry())
    return {"success": True, "task_id": new_task_id, "copied": copied}


@app.get("/api/womenswear-studio/status")
async def womenswear_studio_status(task_id: str):
    """查询一体化流水线状态。比 motion-transfer/status 多了 stage 字段。
    若内存丢失则从 task_store 恢复。"""
    # 内存丢失时从 task_store 恢复
    if task_id not in motion_tasks:
        try:
            from backend.services import task_store
            t_db = task_store.get(task_id)
            if t_db and t_db.get("task_type") == "womenswear":
                motion_tasks[task_id] = {
                    "status": t_db.get("status", "UNKNOWN"),
                    "progress": t_db.get("progress", 0),
                    "outputs": t_db.get("outputs", []),
                    "error": t_db.get("error"),
                    "params": t_db.get("params", {}),
                    "stage": t_db.get("stage", ""),
                    "rh_task_id": t_db.get("rh_task_id", ""),
                    "intermediate": t_db.get("intermediate", {}),
                    "extra": t_db.get("extra", {}),
                    "product_title": t_db.get("extra", {}).get("product_title", ""),
                    "raw_product_url": t_db.get("extra", {}).get("raw_product_url", ""),
                    "flat_lay_url": t_db.get("extra", {}).get("flat_lay_url", ""),
                    "showcase_url": t_db.get("extra", {}).get("showcase_url", ""),
                    "source_video_url": t_db.get("extra", {}).get("source_video_url", ""),
                }
            else:
                raise HTTPException(404, "任务不存在")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(404, "任务不存在")
    t = motion_tasks[task_id]
    extra = t.get("extra", {})
    inter = t.get("intermediate", {}) or {}
    # 优先从顶层/extra 取，缺失则从 intermediate 兜底
    s1 = (inter.get("step1", {}).get("output") or {})
    s2 = (inter.get("step2", {}).get("output") or {})
    s3 = (inter.get("step3", {}).get("output") or {})
    s4 = (inter.get("step4", {}).get("output") or {})
    raw_product_url = t.get("raw_product_url", "") or extra.get("raw_product_url", "") or s1.get("raw_product", "")
    flat_lay_url = t.get("flat_lay_url", "") or extra.get("flat_lay_url", "") or s2.get("flat_lay", "")
    showcase_url = t.get("showcase_url", "") or extra.get("showcase_url", "") or s3.get("showcase", "")
    source_video_url = t.get("source_video_url", "") or extra.get("source_video_url", "") or s4.get("source_video", "")
    return {
        "success": True,
        "status": t["status"],
        "progress": t.get("progress", 0),
        "stage": t.get("stage", ""),
        "outputs": t.get("outputs", []),
        "error": t.get("error"),
        "product_title": t.get("product_title", "") or extra.get("product_title", ""),
        "raw_product_url": raw_product_url,
        "flat_lay_url": flat_lay_url,
        "showcase_url": showcase_url,
        "source_video_url": source_video_url,
        # 前端展示所需的输入参数与 step4 元信息
        "params": t.get("params", {}) or extra.get("params", {}),
        "step4_meta": {
            "title": s4.get("title", ""),
            "author": s4.get("author", ""),
            "quality": s4.get("quality", ""),
        },
        "rh_task_id": t.get("rh_task_id", "") or extra.get("rh_task_id", ""),
        "current_step": t.get("current_step", "") or extra.get("current_step", ""),
        "failed_step": t.get("failed_step", "") or extra.get("failed_step", ""),
        "step_list": extra.get("step_list", ["step1", "step2", "step3", "step4", "step5"]),
        "intermediate": inter,
        "logs": _womenswear_get_logs(task_id),
    }


def _womenswear_get_logs(task_id: str) -> list:
    """读取任务日志（从 task_store），转换字段为前端期望格式。"""
    try:
        from backend.services import task_store
        logs = task_store.get_logs(task_id, limit=100)
        return [{"message": l.get("msg", ""), "level": l.get("level", "info"),
                 "ts": l.get("ts", 0),
                 "ts_str": time.strftime("%H:%M:%S", time.localtime(l.get("ts", 0))) if l.get("ts") else ""}
                for l in logs]
    except Exception:
        return []


# ─────────────────────────────────────
# 视频发布工作台（RPA 一键发布抖音）
# ─────────────────────────────────────

# 发布任务状态存储
publish_tasks: dict = {}


class PublishRequest(BaseModel):
    """视频发布请求。

    video_path: 本地视频文件绝对路径或 /outputs/xxx URL
    title: 视频标题/描述
    tags: 标签列表（可选）
    schedule_time: 定时发布时间（ISO 格式字符串，如 "2026-08-06T22:30"），为空则立即发布
    """
    video_path: str
    title: str = ""
    tags: list = []
    schedule_time: str = ""


class PublishCopyRequest(BaseModel):
    """发布文案生成请求。

    video_path: 本地视频文件绝对路径或 /outputs/xxx URL
    product_info: 可选产品补充信息
    """
    video_path: str
    product_info: str = ""


@app.get("/publish", response_class=HTMLResponse)
async def publish_page():
    """视频发布工作台"""
    html_path = Path(settings.FRONTEND_DIR) / "publish.html"
    if not html_path.exists():
        raise HTTPException(404, "发布工作台页面未找到")
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.post("/api/publish/submit")
async def publish_submit(req: PublishRequest):
    """提交视频发布任务（RPA 自动化抖音创作者中心）"""
    # 解析视频路径：支持 /outputs/xxx 或绝对路径
    vp = req.video_path
    if vp.startswith("/outputs/"):
        vp = str(Path(settings.OUTPUT_DIR) / vp[len("/outputs/"):])
    if not Path(vp).exists():
        raise HTTPException(400, f"视频文件不存在: {vp}")

    task_id = uuid.uuid4().hex[:12]
    publish_tasks[task_id] = {
        "status": "INIT",
        "progress": 0,
        "stage": "启动中",
        "url": None,
        "error": None,
    }

    async def run_publish():
        try:
            from backend.services.douyin_publisher import publish_video

            async def cb(msg):
                publish_tasks[task_id].update({"status": "RUNNING", "stage": msg})

            publish_tasks[task_id].update({"status": "RUNNING", "stage": "开始发布", "progress": 10})
            result = await publish_video(
                video_path=vp,
                title=req.title,
                tags=req.tags,
                status_callback=cb,
                headless=False,
            )
            if result.get("success"):
                publish_tasks[task_id].update({
                    "status": "SUCCESS", "progress": 100,
                    "stage": "发布成功", "url": result.get("url", ""),
                })
            else:
                publish_tasks[task_id].update({
                    "status": "FAILED", "error": result.get("error", "发布失败"),
                    "stage": "失败",
                })
        except Exception as e:
            publish_tasks[task_id].update({"status": "FAILED", "error": str(e), "stage": "异常"})

    asyncio.create_task(run_publish())
    return {"success": True, "task_id": task_id}


@app.get("/api/publish/status")
async def publish_status(task_id: str):
    """查询发布任务状态"""
    if task_id not in publish_tasks:
        raise HTTPException(404, "任务不存在")
    t = publish_tasks[task_id]
    return {
        "success": True,
        "status": t["status"],
        "progress": t.get("progress", 0),
        "stage": t.get("stage", ""),
        "url": t.get("url"),
        "error": t.get("error"),
        "nickname": t.get("nickname"),
    }


@app.post("/api/publish/login")
async def publish_login():
    """触发抖音创作者中心登录（扫码 https://creator.douyin.com/）"""
    from backend.services.douyin_publisher import ensure_login_for_publish

    task_id = uuid.uuid4().hex[:12]
    publish_tasks[task_id] = {"status": "RUNNING", "stage": "等待扫码登录创作者中心", "progress": 0, "url": None, "error": None, "nickname": None}

    async def run_login():
        try:
            async def cb(msg):
                publish_tasks[task_id].update({"stage": msg})
            info = await ensure_login_for_publish(status_callback=cb, force_relogin=True)
            if info:
                nick = info.get("nickname", "") or ""
                publish_tasks[task_id].update({
                    "status": "SUCCESS", "progress": 100,
                    "stage": f"创作者中心登录成功{('（' + nick + '）') if nick else ''}",
                    "nickname": nick,
                })
            else:
                publish_tasks[task_id].update({"status": "FAILED", "error": "登录失败", "stage": "失败"})
        except Exception as e:
            publish_tasks[task_id].update({"status": "FAILED", "error": str(e), "stage": "异常"})

    asyncio.create_task(run_login())
    return {"success": True, "task_id": task_id}


@app.get("/api/publish/check-login")
async def publish_check_login():
    """快速检查已保存的抖音登录态（基于文件存在性 + cookie 内容）。

    注意：本接口不调用 Playwright 验证，避免阻塞前端。
    真正的登录态验证在发布时进行，失效会返回明确错误。
    """
    from backend.services.douyin_publisher import DOUYIN_STORAGE_STATE_PATH
    import json

    if not DOUYIN_STORAGE_STATE_PATH.exists():
        return {"success": True, "logged_in": False, "nickname": None, "message": "未保存登录态"}

    # 从 storage_state 文件中提取信息
    try:
        with open(DOUYIN_STORAGE_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        cookies = data.get("cookies", [])
        # 检查是否有登录态 cookie（严格判定，passport_csrf_token 未登录也下发）
        cookie_names = {c.get("name", "") for c in cookies}
        has_session = bool({"sessionid", "sessionid_ss", "sid_tt"} & cookie_names)
        if not has_session:
            # 没有 session 类 cookie，可能已失效
            try:
                DOUYIN_STORAGE_STATE_PATH.unlink()
            except Exception:
                pass
            try:
                from backend.services.douyin_publisher import DOUYIN_NICKNAME_PATH
                DOUYIN_NICKNAME_PATH.unlink()
            except Exception:
                pass
            return {"success": True, "logged_in": False, "nickname": None, "message": "登录态已失效，请重新扫码"}
        # 文件存在且有 session cookie，认为已登录
        # 昵称从独立文件读取（登录成功时保存）
        nickname = ""
        try:
            from backend.services.douyin_publisher import DOUYIN_NICKNAME_PATH
            if DOUYIN_NICKNAME_PATH.exists():
                nickname = DOUYIN_NICKNAME_PATH.read_text(encoding="utf-8").strip()
        except Exception:
            pass
        return {
            "success": True,
            "logged_in": True,
            "nickname": nickname,
            "message": "已保存登录态（实际有效性在发布时验证）",
        }
    except Exception as e:
        return {"success": False, "logged_in": False, "nickname": None, "message": str(e)}


# ---------------------------------------------------------------------------
# 分步式 RPA 发布会话（走一步停一步，等用户指令继续）
# ---------------------------------------------------------------------------

class PublishSessionRequest(BaseModel):
    """启动分步发布会话"""
    video_path: str
    title: str = ""
    tags: list = []


@app.post("/api/publish/session/start")
async def publish_session_start(req: PublishSessionRequest):
    """启动分步发布会话：检查登录 → 启动浏览器 → 打开发布页 → 暂停等待指令。

    返回 {session_id, paused, step, next_step, logs, screenshot, dom_info}
    后续前端调用 /session/{id}/next 继续下一步
    """
    from backend.services.douyin_publisher import PublishSession, publish_sessions

    vp = req.video_path
    if vp.startswith("/outputs/"):
        vp = str(Path(settings.OUTPUT_DIR) / vp[len("/outputs/"):])
    if not Path(vp).exists():
        raise HTTPException(400, f"视频文件不存在: {vp}")

    session_id = uuid.uuid4().hex[:12]
    session = PublishSession(
        video_path=vp,
        title=req.title,
        tags=req.tags,
        headless=False,  # 有头模式，便于观察
    )
    publish_sessions[session_id] = session

    try:
        result = await session.start()
        return {"session_id": session_id, **result}
    except Exception as e:
        # 启动失败，清理会话
        publish_sessions.pop(session_id, None)
        return {"session_id": session_id, "success": False, "error": str(e)}


@app.post("/api/publish/session/{session_id}/next")
async def publish_session_next(session_id: str):
    """执行下一步。返回 paused/done/failed 结果。"""
    from backend.services.douyin_publisher import publish_sessions

    session = publish_sessions.get(session_id)
    if not session:
        raise HTTPException(404, "会话不存在或已结束")

    try:
        result = await session.run_next()
        # 如果已结束，清理会话
        if result.get("finished"):
            publish_sessions.pop(session_id, None)
        return {"session_id": session_id, **result}
    except Exception as e:
        return {"session_id": session_id, "success": False, "error": str(e), "logs": list(session._logs)}


@app.post("/api/publish/session/{session_id}/abort")
async def publish_session_abort(session_id: str):
    """放弃会话，关闭浏览器。"""
    from backend.services.douyin_publisher import publish_sessions

    session = publish_sessions.pop(session_id, None)
    if not session:
        raise HTTPException(404, "会话不存在或已结束")

    try:
        await session.abort()
        return {"session_id": session_id, "success": True, "aborted": True, "logs": list(session._logs)}
    except Exception as e:
        return {"session_id": session_id, "success": False, "error": str(e)}


@app.get("/api/publish/session/{session_id}/status")
async def publish_session_status(session_id: str):
    """查询会话当前状态（不执行任何操作）。"""
    from backend.services.douyin_publisher import publish_sessions

    session = publish_sessions.get(session_id)
    if not session:
        raise HTTPException(404, "会话不存在或已结束")

    return {
        "session_id": session_id,
        "success": True,
        "step": session.current_step,
        "finished": session._finished,
        "aborted": session._aborted,
        "next_step": session.STEPS[session._step_index] if session._step_index < len(session.STEPS) and not session._finished else None,
        "logs": list(session._logs),
        "screenshot": session._last_screenshot,
        "dom_info": session._last_dom_info,
    }


@app.post("/api/publish/generate-copy")
async def publish_generate_copy(req: PublishCopyRequest):
    """根据视频内容自动生成抖音发布文案（标题 + 标签）。

    请求体: {"video_path": "相对或绝对路径", "product_info": "可选产品补充信息"}
    流程:
      1. ffmpeg 抽取视频中点一帧
      2. 调 MiMo 多模态识别服装类型
      3. 生成 ≤20 字标题 + 5 个标签
    """
    raw_path = (req.video_path or "").strip()
    product_info = (req.product_info or "").strip() or None

    if not raw_path:
        return {"success": False, "detail": "缺少 video_path"}

    # 解析为绝对路径
    p = Path(raw_path)
    if not p.is_absolute():
        # 如果是 /outputs/... 这种 URL 形式，去掉前导 /
        if raw_path.startswith("/"):
            p = (Path(__file__).parent / raw_path.lstrip("/"))
        else:
            p = Path(__file__).parent / raw_path
    video_abs = str(p)

    if not p.exists():
        return {"success": False, "detail": f"视频不存在: {video_abs}"}

    from backend.services.copy_generator import generate_publish_copy

    try:
        result = await generate_publish_copy(video_abs, product_info=product_info)
        return {
            "success": True,
            "clothing_type": result.get("clothing_type", ""),
            "title": result.get("title", ""),
            "tags": result.get("tags", []),
        }
    except Exception as e:
        return {"success": False, "detail": f"文案生成失败: {e}"}


# ─────────────────────────────────────
# 海螺 H3 Studio API
# ─────────────────────────────────────

@app.get("/h3-studio", response_class=HTMLResponse)
async def h3_studio_page():
    """海螺 H3 Studio - 多参/首尾帧视频生成"""
    html_path = Path(settings.FRONTEND_DIR) / "h3_studio.html"
    if not html_path.exists():
        raise HTTPException(404, "海螺 H3 Studio 页面未找到")
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


# H3 Studio 任务状态存储
h3_studio_tasks: dict = {}


class H3StudioRequest(BaseModel):
    """海螺 H3 Studio 生成请求

    channel: 渠道
      - "runninghub": RunningHub 导演台工作流（默认，支持 t2v/i2v/fl2v/r2v 全模式）
      - "manxue": 1人漫剧 H3（manxueapi 网关，OpenAI 兼容）
    mode: "reference" 多参模式 | "first_last_frame" 首尾帧模式
      - 多参模式: reference_images 0-3张参考图（0张走 t2v 纯文本）
      - 首尾帧模式: first_frame_path + last_frame_path
    """
    channel: str = "runninghub"  # "runninghub" | "manxue"
    mode: str  # "reference" | "first_last_frame"
    reference_images: Optional[list[str]] = None  # 多参模式参考图路径列表
    first_frame_path: Optional[str] = None  # 首尾帧模式首帧
    last_frame_path: Optional[str] = None  # 首尾帧模式尾帧
    prompt: str = ""
    duration: int = 6  # 秒
    duration_mode: str = "fixed"  # fixed / source
    source_duration: float = 0.0  # 原视频完整时长（duration_mode=source 时使用）
    aspect_ratio: str = "9:16"  # 短格式: "9:16" / "16:9" / "1:1" / "4:3" / "3:4"
    megapixels: float = 0.7  # 多参模式分辨率
    multi_view_mode: str = "split"  # manxue 渠道多视图处理: "split"(默认拆分) | "convert"(转实物图) | "off"


class H3CompilePromptRequest(BaseModel):
    """H3 提示词编译请求：自然语言 + 参考图 → H3 适配的专业提示词"""
    user_description: str  # 自然语言描述（必填）
    reference_images: Optional[list[str]] = None  # 参考图绝对路径列表（可选）
    channel: str = "runninghub"  # "runninghub" | "manxue" — manxue 渠道不使用 <Picture N> 引用符号


@app.post("/api/h3-studio/compile-prompt")
async def h3_studio_compile_prompt(req: H3CompilePromptRequest):
    """把自然语言描述 + 参考图编译成 H3 适配的专业提示词。

    根据参考图数量自动判定模式：
      - 0 张 → T2VA（纯文本）
      - 1 张 → I2VA（首帧）
      - 2 张 → FL2VA（首尾帧）
      - 3+ 张 → Ref2VA（全参考）
    """
    from backend.services.h3_prompt_compiler import compile_h3_prompt

    try:
        result = await compile_h3_prompt(
            user_description=req.user_description,
            image_paths=req.reference_images,
            channel=req.channel,
        )
        return {
            "success": True,
            "mode": result["mode"],
            "prompt": result["prompt"],
            "image_count": result["image_count"],
        }
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"提示词编译失败: {e}")


def _h3_prompt_duration(prompt: str, fallback: int) -> float:
    """从 H3 Prompt 的时间标记估算目标时长。"""
    values = [float(v) for v in re.findall(r"\b(\d+(?:\.\d+)?)s\b", prompt)]
    return max(values, default=float(fallback))


def _split_h3_prompt(prompt: str, total_duration: float, segment_duration: int = 15) -> list[tuple[str, float, float]]:
    """按 Shot 起始时间拆分长 Prompt，返回 (子提示词, 起点, 终点)。"""
    if total_duration <= segment_duration:
        return [(prompt, 0.0, total_duration)]

    shot_matches = list(re.finditer(r"\[Shot\s+\d+\]", prompt, re.IGNORECASE))
    if not shot_matches:
        raise ValueError("超过 15 秒的 Prompt 必须包含 [Shot N] 时间分镜，无法安全分段")

    prefix = prompt[:shot_matches[0].start()].strip()
    suffix_match = re.search(r"\n\s*(overall_soundscape\s*:|non_diegetic_music\s*:)", prompt, re.IGNORECASE)
    description_end = suffix_match.start() if suffix_match else len(prompt)
    description = prompt[shot_matches[0].start():description_end].strip()
    suffix = prompt[description_end:].strip()
    shots = []
    for index, match in enumerate(shot_matches):
        end = shot_matches[index + 1].start() if index + 1 < len(shot_matches) else len(description)
        text = description[match.start():end].strip()
        times = [float(v) for v in re.findall(r"\b(\d+(?:\.\d+)?)s\b", text)]
        shots.append((times[0] if times else 0.0, text))

    segments = []
    start = 0.0
    while start < total_duration - 0.01:
        end = min(start + segment_duration, total_duration)
        selected = [text for shot_start, text in shots if start <= shot_start < end]
        if not selected:
            selected = [shots[-1][1]]
        continuation = (
            f"This is segment {int(start // segment_duration) + 1} of a continuous video. "
            "Continue the same characters, wardrobe, product identity, environment, lighting, "
            "camera language, motion logic, and audio style from the adjacent segments. "
            "Do not introduce new people, disconnected hands, or a redesigned product."
        )
        body = "\n\n".join(part for part in (prefix, continuation, "\n".join(selected), suffix) if part)
        segments.append((body, start, end))
        start = end
    return segments


def _concat_h3_segments(segment_paths: list[str], output_path: str, target_duration: float) -> None:
    """统一编码后拼接 H3 子视频，并裁剪到目标时长。"""
    out = Path(output_path)
    list_path = out.with_name("concat_list.txt")
    list_path.write_text(
        "\n".join(f"file '{Path(path).resolve().as_posix()}'" for path in segment_paths),
        encoding="utf-8",
    )
    temp_path = out.with_name("joined_raw.mp4")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_path),
             "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-c:a", "aac", str(temp_path)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(temp_path), "-t", f"{target_duration:.3f}",
             "-c", "copy", str(out)],
            check=True, capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", errors="replace")[-1200:]
        raise RuntimeError(f"片段合并失败：{detail}") from exc


@app.post("/api/h3-studio/generate")
async def h3_studio_generate(req: H3StudioRequest):
    """启动海螺 H3 Studio 视频生成任务。

    🔴 这条腿【绕开网关】直连厂商 ⇒ 网关里那道批准闸盖不到它，所以在入口调同一个守卫、认同一张条子。
       没条子 ⇒ 423 拒绝，任务不提交、0 分不动（实测过）。
    """
    _spend_or_423("video.generate", "h3", "h3-studio 出片")

    if req.channel not in ("runninghub", "manxue"):
        raise HTTPException(400, "channel 必须是 runninghub 或 manxue")
    if req.mode not in ("reference", "first_last_frame"):
        raise HTTPException(400, "mode 必须是 reference 或 first_last_frame")

    if req.mode == "reference":
        # 导演台 / manxue 均允许 0 张图走 T2V 纯文本
        pass
    else:
        if not req.first_frame_path or not req.last_frame_path:
            raise HTTPException(400, "首尾帧模式必须提供首帧和尾帧图片")

    task_id = uuid.uuid4().hex[:12]
    h3_studio_tasks[task_id] = {
        "status": "running",
        "channel": req.channel,
        "mode": req.mode,
        "progress": 0,
        "stage": "",
        "logs": [],
        "result": None,
        "error": None,
        "started_at": time.time(),
    }

    async def _run():
        # 渠道分发：manxue 网关 / RunningHub 导演台工作流
        if req.channel == "manxue":
            from backend.services.manxue_h3_client import generate_video as h3_generate
            max_wait = 1800
        else:
            from backend.services.h3_director_client import generate_video as h3_generate
            max_wait = 3600

        def on_progress(stage, progress, msg):
            h3_studio_tasks[task_id]["stage"] = stage
            if progress:
                h3_studio_tasks[task_id]["progress"] = progress
            ts = time.strftime("%H:%M:%S")
            h3_studio_tasks[task_id]["logs"].append(f"[{ts}] [{stage}] {msg}")
            # 保留最近 200 行
            h3_studio_tasks[task_id]["logs"] = h3_studio_tasks[task_id]["logs"][-200:]

        segment_paths = []
        try:
            # 输出路径
            out_dir = OUTPUT_DIR / "h3_studio" / task_id
            out_dir.mkdir(parents=True, exist_ok=True)
            output_path = str(out_dir / "output.mp4")

            # manxue 渠道支持多视图处理参数，RunningHub 渠道不处理
            extra_kwargs = {}
            if req.channel == "manxue":
                extra_kwargs["multi_view_mode"] = req.multi_view_mode

            if req.duration_mode not in ("fixed", "source"):
                raise ValueError("duration_mode 必须是 fixed 或 source")
            prompt_duration = (
                max(float(req.source_duration), _h3_prompt_duration(req.prompt, req.duration))
                if req.duration_mode == "source"
                else float(req.duration)
            )
            segments = _split_h3_prompt(req.prompt, prompt_duration)
            segment_paths = []
            for segment_index, (segment_prompt, segment_start, segment_end) in enumerate(segments):
                segment_path = out_dir / f"segment_{segment_index + 1:02d}.mp4"
                on_progress(
                    "generating_segment",
                    int(segment_index / max(len(segments), 1) * 90),
                    f"生成第 {segment_index + 1}/{len(segments)} 段 ({segment_start:.2f}s-{segment_end:.2f}s)",
                )
                await h3_generate(
                    mode=req.mode,
                    output_path=str(segment_path),
                    reference_images=req.reference_images,
                    first_frame_path=req.first_frame_path,
                    last_frame_path=req.last_frame_path,
                    prompt=segment_prompt,
                    duration=min(15, max(5, int(round(segment_end - segment_start)))),
                    aspect_ratio=req.aspect_ratio,
                    megapixels=req.megapixels,
                    max_wait=max_wait,
                    progress_callback=lambda stage, progress, msg: on_progress(
                        stage, int((segment_index + progress / 100) / max(len(segments), 1) * 90), msg
                    ),
                    **extra_kwargs,
                )
                segment_paths.append(str(segment_path))

            if len(segment_paths) == 1:
                result_path = segment_paths[0]
            else:
                on_progress("stitching", 94, f"正在拼接 {len(segment_paths)} 个 H3 片段...")
                _concat_h3_segments(segment_paths, output_path, prompt_duration)
                result_path = output_path

            # 转换为可访问 URL
            try:
                rel = Path(result_path).relative_to(OUTPUT_DIR)
                video_url = f"/outputs/{rel.as_posix()}"
            except ValueError:
                video_url = result_path

            h3_studio_tasks[task_id]["status"] = "completed"
            h3_studio_tasks[task_id]["progress"] = 100
            h3_studio_tasks[task_id]["result"] = {
                "video_path": result_path,
                "video_url": video_url,
            }
            on_progress("done", 100, f"视频已生成: {video_url}")
        except Exception as e:
            task = h3_studio_tasks[task_id]
            task["status"] = "failed"
            task["error"] = str(e)
            task["partial_result"] = {
                "video_urls": [
                    f"/outputs/h3_studio/{task_id}/{Path(path).name}"
                    for path in segment_paths
                    if Path(path).exists()
                ],
                "segment_count": len(segment_paths),
                "can_stitch": len(segment_paths) > 1,
            }
            ts = time.strftime("%H:%M:%S")
            task["logs"].append(f"[{ts}] [failed] {e}")

    asyncio.create_task(_run())
    return {"success": True, "task_id": task_id, "mode": req.mode}


@app.get("/api/h3-studio/status")
async def h3_studio_status(task_id: str):
    """查询 H3 Studio 任务状态"""
    if task_id not in h3_studio_tasks:
        raise HTTPException(404, "任务不存在")
    return h3_studio_tasks[task_id]


# ─────────────────────────────────────
# 9 宫格带货工作流（三阶段：脚本 → 图提示词 → Ref2VA 视频提示词）
# ─────────────────────────────────────

class H3StoryboardRequest(BaseModel):
    """阶段 1：9 宫格分镜脚本 + 画外音话术"""
    product_image_path: str  # 产品图绝对路径
    product_name: str  # 产品名
    selling_points: list[str]  # 核心卖点列表
    segment_count: int = 2  # 段数（1-6 段，每段 15 秒）
    extra_context: str = ""  # 补充上下文（可选）


class H3ImagePromptsRequest(BaseModel):
    """阶段 2：9:16 竵屏图片生成提示词"""
    storyboard_json: dict  # 阶段 1 输出的 9 宫格脚本
    product_name: str


class H3VideoPromptsRequest(BaseModel):
    """阶段 3：Ref2VA 6 字段视频提示词"""
    storyboard_json: dict
    image_prompts_json: dict
    product_name: str
    product_image_path: str


@app.post("/api/h3-9grid/storyboard")
async def h3_9grid_storyboard(req: H3StoryboardRequest):
    """阶段 1：基于产品图+卖点，LLM 推理 N 段 9 宫格分镜脚本 + 画外音话术。"""
    from backend.services.h3_prompt_compiler import compile_h3_storyboard

    try:
        result = await compile_h3_storyboard(
            product_image_path=req.product_image_path,
            product_name=req.product_name,
            selling_points=req.selling_points,
            segment_count=req.segment_count,
            extra_context=req.extra_context,
        )
        return {
            "success": True,
            "storyboard": result["storyboard_json"],
            "raw": result["raw_response"],
        }
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"9 宫格脚本生成失败: {e}")


@app.post("/api/h3-9grid/image-prompts")
async def h3_9grid_image_prompts(req: H3ImagePromptsRequest):
    """阶段 2：把 9 宫格脚本转译为 9:16 竵屏图片生成提示词。"""
    from backend.services.h3_prompt_compiler import compile_h3_image_prompts

    try:
        result = await compile_h3_image_prompts(
            storyboard_json=req.storyboard_json,
            product_name=req.product_name,
        )
        return {
            "success": True,
            "image_prompts": result["image_prompts_json"],
            "raw": result["raw_response"],
        }
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"图片提示词生成失败: {e}")


@app.post("/api/h3-9grid/video-prompts")
async def h3_9grid_video_prompts(req: H3VideoPromptsRequest):
    """阶段 3：基于 9 宫格脚本+图片提示词，输出 N 段 Ref2VA 6 字段视频提示词 JSON。"""
    from backend.services.h3_prompt_compiler import compile_h3_video_prompts

    try:
        result = await compile_h3_video_prompts(
            storyboard_json=req.storyboard_json,
            image_prompts_json=req.image_prompts_json,
            product_name=req.product_name,
            product_image_path=req.product_image_path,
        )
        return {
            "success": True,
            "video_prompts": result["video_prompts_json"],
            "raw": result["raw_response"],
        }
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"视频提示词生成失败: {e}")


# ── 阶段 2.5：9 图 i2i 关键帧生成 ──

class H3GenerateImagesRequest(BaseModel):
    """阶段 2.5：以产品图为参考，i2i 生成 N 段 × 9 张 9:16 竵屏关键帧"""
    image_prompts_json: dict  # 阶段 2 输出的图片提示词 JSON
    product_image_path: str  # 原始产品图绝对路径
    size: str = "1024x1536"  # 图片尺寸，默认接近 9:16
    max_concurrency: int = 3  # 单段内 i2i 并发上限


# 9 图生成任务状态存储
h3_9grid_image_tasks: dict = {}
H3_9GRID_OUTPUT_DIR = OUTPUT_DIR / "h3_9grid"
H3_9GRID_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


@app.post("/api/h3-9grid/upload-product")
async def h3_9grid_upload_product(file: UploadFile = File(...)):
    """上传 9 宫格工作流的产品图（前端阶段 1 使用）"""
    ext = Path(file.filename).suffix.lower() or ".png"
    save_name = f"ng_product_{uuid.uuid4().hex[:12]}{ext}"
    save_path = UPLOAD_DIR / save_name
    content = await file.read()
    save_path.write_bytes(content)
    return {"success": True, "path": str(save_path), "url": f"/uploads/{save_name}"}


@app.post("/api/h3-9grid/generate-images")
async def h3_9grid_generate_images(req: H3GenerateImagesRequest):
    """阶段 2.5：基于图片提示词 + 产品图，i2i 生成 N 段 × 9 张关键帧。

    - 跨段 sc1 = 上段 sc9 物理同一张图（不调用 image2，直接复用文件路径）。
    - 每张图均以原始产品图为参考 i2i 生成，保产品外观一致性。
    """
    if not Path(req.product_image_path).exists():
        raise HTTPException(400, f"产品图不存在: {req.product_image_path}")
    if not req.image_prompts_json.get("segments"):
        raise HTTPException(400, "image_prompts_json.segments 为空")

    task_id = uuid.uuid4().hex[:12]
    h3_9grid_image_tasks[task_id] = {
        "status": "running",
        "result": None,
        "error": None,
        "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    async def run_generation():
        try:
            from backend.services.image2_client import Image2Client
            output_dir = H3_9GRID_OUTPUT_DIR / task_id
            output_dir.mkdir(parents=True, exist_ok=True)

            result = await Image2Client().generate_9grid_keyframes(
                image_prompts_json=req.image_prompts_json,
                product_image_path=req.product_image_path,
                output_dir=output_dir,
                size=req.size,
                max_concurrency=req.max_concurrency,
            )

            # 把绝对路径转换为可访问的 /outputs/ URL
            for seg in result.get("segments", []):
                for img in seg.get("images", []):
                    abs_p = img.get("image_path")
                    if abs_p:
                        try:
                            img["image_url"] = (
                                f"/outputs/{Path(abs_p).relative_to(OUTPUT_DIR).as_posix()}"
                            )
                        except ValueError:
                            img["image_url"] = ""

            h3_9grid_image_tasks[task_id].update({
                "status": "completed",
                "result": result,
                "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
        except Exception as e:
            h3_9grid_image_tasks[task_id].update({
                "status": "failed",
                "error": str(e),
                "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })

    asyncio.create_task(run_generation())
    return {"success": True, "task_id": task_id}


@app.get("/api/h3-9grid/images/status/{task_id}")
async def h3_9grid_images_status(task_id: str):
    """查询 9 图 i2i 生成任务状态"""
    if task_id not in h3_9grid_image_tasks:
        raise HTTPException(404, "任务不存在")
    return h3_9grid_image_tasks[task_id]


# ─────────────────────────────────────
# 视频翻译 API
# ─────────────────────────────────────

@app.get("/video-translate", response_class=HTMLResponse)
async def video_translate_page():
    """视频翻译页面"""
    html_path = Path(settings.FRONTEND_DIR) / "video_translate.html"
    if not html_path.exists():
        raise HTTPException(404, "视频翻译页面未找到")
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


# 视频翻译任务状态存储
vt_tasks: dict = {}
VT_OUTPUT_DIR = OUTPUT_DIR / "video_translate"
VT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _recover_vt_tasks_on_startup() -> int:
    recovered = 0
    for task_path in VT_OUTPUT_DIR.glob("*/task.json"):
        try:
            task = json.loads(task_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        task_id = task.get("task_id") or task_path.parent.name
        if task.get("status") == "RUNNING":
            task.update({
                "status": "FAILED",
                "error": "服务重启导致任务中断",
                "message": "任务已中断，可从已成功步骤继续",
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
            task_path.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
            recovered += 1
        vt_tasks[task_id] = task
    return recovered


def _save_vt_task(task_id: str):
    task = vt_tasks.get(task_id)
    if not task:
        return
    rh_path = VT_OUTPUT_DIR / task_id / "runninghub_tasks.json"
    if rh_path.exists():
        try:
            task["runninghub_tasks"] = json.loads(rh_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    task["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    task_dir = VT_OUTPUT_DIR / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    task_path = task_dir / "task.json"
    temp_path = task_dir / "task.json.tmp"
    temp_path.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(task_path)


def _load_vt_task(task_id: str) -> Optional[dict]:
    task_path = VT_OUTPUT_DIR / task_id / "task.json"
    if not task_path.exists():
        return None
    try:
        task = json.loads(task_path.read_text(encoding="utf-8"))
        vt_tasks[task_id] = task
        return task
    except (OSError, json.JSONDecodeError):
        return None


def _get_vt_task(task_id: str) -> Optional[dict]:
    return vt_tasks.get(task_id) or _load_vt_task(task_id)


class CharacterMapping(BaseModel):
    source_face_path: str
    target_face_path: str


class VideoTranslateRequest(BaseModel):
    video_path: str
    target_face_image: str = ""
    source_lang: str = "zh"
    target_lang: str = "en"
    asr_engine: str = "whisper"
    translate_engine: str = "gpt"
    character_mappings: list[CharacterMapping] = []
    remove_subtitle: bool = False  # Step 0: 是否消除原视频硬字幕
    voice_source: str = "original"  # Step 2: original / uploaded / tts
    uploaded_ref_audio: str = ""    # Step 2: 上传的参考音色音频路径


@app.post("/api/video-translate/detect-characters")
async def video_translate_detect_characters(req: dict):
    """识别视频中的角色人脸"""
    from backend.services.character_detector import detect_characters_in_video
    video_path = req.get("video_path", "")
    if not video_path:
        return {"success": False, "error": "video_path 为空"}
    
    # 尝试相对路径和绝对路径
    abs_path = os.path.abspath(video_path)
    if not os.path.exists(video_path) and not os.path.exists(abs_path):
        return {"success": False, "error": f"视频文件不存在: {video_path}"}
    
    # 使用能找到的路径
    actual_path = video_path if os.path.exists(video_path) else abs_path
    
    try:
        characters = await detect_characters_in_video(actual_path)
        return {"success": True, "characters": characters}
    except Exception as e:
        import traceback
        return {"success": False, "error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()}


def _launch_vt_task(task_id: str):
    # 先确保任务在内存中（服务重启后 vt_tasks 会清空，需要从磁盘加载）
    task = _get_vt_task(task_id)
    if not task:
        raise RuntimeError(f"任务 {task_id} 不存在，无法启动")
    vt_tasks[task_id] = task  # _get_vt_task 可能已设置，但显式赋值更安全
    params = task["params"]
    task.update({
        "status": "RUNNING",
        "error": None,
        "message": "正在从已成功步骤继续" if task.get("attempt", 0) else "任务已提交",
        "attempt": task.get("attempt", 0) + 1,
    })
    _save_vt_task(task_id)

    def on_progress(step: int, progress: int, message: str):
        if task_id in vt_tasks:
            vt_tasks[task_id]["current_step"] = step
            vt_tasks[task_id]["progress"] = progress
            vt_tasks[task_id]["message"] = message
            _save_vt_task(task_id)

    async def _run():
        try:
            from backend.services.video_translate_pipeline import run_video_translate
            result = await run_video_translate(
                video_path=params["video_path"],
                source_lang=params.get("source_lang", "zh"),
                target_lang=params.get("target_lang", "en"),
                target_face_image=params.get("target_face_image", ""),
                asr_engine=params.get("asr_engine", "whisper"),
                translate_engine=params.get("translate_engine", "gpt"),
                character_mappings=params.get("character_mappings", []),
                task_id=task_id,
                status_callback=on_progress,
                remove_subtitle=params.get("remove_subtitle", False),
                voice_source=params.get("voice_source", "original"),
                uploaded_ref_audio=params.get("uploaded_ref_audio", ""),
            )
            segs = result.get("segments", [])
            sub_text = "\n".join(
                f"[{s.get('start',0):.1f}s→{s.get('end',0):.1f}s] {s.get('source_text','')} → {s.get('target_text','')}"
                for s in segs
            )
            vt_tasks[task_id].update({
                "status": "SUCCESS",
                "progress": 100,
                "current_step": 5,
                "subtitle_text": sub_text,
                "outputs": [{
                    "type": "video",
                    "url": f"/api/result/file?path={result['result_video_path']}",
                    "path": result["result_video_path"],
                    "role": "heygem_lipsync_master",
                }],
                "message": "视频翻译完成",
                "completed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
        except Exception as e:
            import traceback
            print(f"[VideoTranslate] 任务 {task_id} 失败:\n{traceback.format_exc()}", flush=True)
            vt_tasks[task_id].update({
                "status": "FAILED",
                "error": str(e),
                "message": f"翻译失败: {e}",
            })
        finally:
            _save_vt_task(task_id)

    asyncio.create_task(_run())


@app.post("/api/video-translate/submit")
async def video_translate_submit(req: VideoTranslateRequest):
    """提交视频翻译任务"""
    # 一个视频同时只允许一个 RUNNING/PENDING 任务，避免重复启动浪费资源
    norm_path = os.path.abspath(req.video_path)
    for existing in vt_tasks.values():
        if existing.get("status") not in ("RUNNING", "PENDING"):
            continue
        try:
            existing_path = os.path.abspath(existing.get("params", {}).get("video_path", ""))
        except Exception:
            continue
        if existing_path == norm_path:
            raise HTTPException(
                409,
                f"该视频已有运行中任务 {existing.get('task_id')}，请等待完成或在历史列表中点击查看"
            )
    task_id = uuid.uuid4().hex[:12]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    vt_tasks[task_id] = {
        "task_id": task_id,
        "status": "PENDING",
        "current_step": 0,
        "progress": 0,
        "outputs": [],
        "error": None,
        "subtitle_text": None,
        "message": "等待执行",
        "params": req.model_dump(),
        "created_at": now,
        "updated_at": now,
        "attempt": 0,
    }
    _save_vt_task(task_id)
    _launch_vt_task(task_id)
    return {"success": True, "task_id": task_id}


@app.post("/api/video-translate/resume/{task_id}")
async def video_translate_resume(task_id: str):
    """失败任务从已成功步骤继续，不创建新任务。"""
    task = _get_vt_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    # 严格防并发：只允许 FAILED/PENDING 状态 resume，RUNNING 直接拒绝
    if task.get("status") == "RUNNING":
        raise HTTPException(409, "任务正在运行，请勿重复点击继续")
    if task.get("status") == "SUCCESS":
        return {"success": True, "task_id": task_id, "message": "任务已完成，无需继续"}
    # 清除上次的错误信息，避免前端继续显示旧 FAILED 状态
    task["error"] = None
    task["message"] = "正在从已成功步骤继续"
    _save_vt_task(task_id)
    _launch_vt_task(task_id)
    # 立即返回最新状态，让前端能马上拿到 RUNNING
    return {
        "success": True,
        "task_id": task_id,
        "message": "已从成功步骤继续",
        "status": "RUNNING",
        "current_step": task.get("current_step", 0),
    }


@app.get("/api/video-translate/history")
async def video_translate_history():
    """读取磁盘中的视频翻译历史记录。"""
    history = []
    for task_path in VT_OUTPUT_DIR.glob("*/task.json"):
        try:
            item = json.loads(task_path.read_text(encoding="utf-8"))
            history.append(item)
        except (OSError, json.JSONDecodeError):
            continue
    history.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return {"success": True, "history": history[:100]}


@app.get("/api/video-translate/status")
async def video_translate_status(task_id: str):
    """查询视频翻译任务状态，内存未命中时从磁盘恢复。"""
    task = _get_vt_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return {"success": True, **task}


# ─────────────────────────────────────
# AI 剪辑工作台（Agent 对话 + HyperFrames 预览）
# ─────────────────────────────────────
ai_edit_tasks: dict = {}
AI_EDIT_OUTPUT_DIR = OUTPUT_DIR / "ai_edit"
AI_EDIT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
OPENMONTAGE_DIR = Path(__file__).resolve().parent / "integrations" / "OpenMontage"
OPENMONTAGE_BACKLOT_PORT = 8765


class AiEditCreateRequest(BaseModel):
    main_video: str
    broll_videos: list[str] = []
    engine: str = "gpt"
    workflow_engine: str = "openmontage"


class AiEditChatRequest(BaseModel):
    task_id: str
    message: str
    engine: str = "gpt"


class AiEditTaskRequest(BaseModel):
    task_id: str


def _ai_edit_task_dir(task_id: str) -> Path:
    return AI_EDIT_OUTPUT_DIR / task_id


def _save_ai_edit_task(task_id: str):
    task = ai_edit_tasks[task_id]
    task_dir = _ai_edit_task_dir(task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    tmp = task_dir / "task.json.tmp"
    tmp.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(task_dir / "task.json")


def _load_ai_edit_task(task_id: str) -> Optional[dict]:
    if task_id in ai_edit_tasks:
        return ai_edit_tasks[task_id]
    path = _ai_edit_task_dir(task_id) / "task.json"
    if not path.exists():
        return None
    try:
        task = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    ai_edit_tasks[task_id] = task
    return task


def _init_openmontage_project(task_id: str, media_files: list[str]) -> dict:
    if not (OPENMONTAGE_DIR / "AGENT_GUIDE.md").exists():
        raise RuntimeError("OpenMontage 运行时未安装")
    project_id = f"ai-edit-{task_id}"
    script = (
        "import json,sys; from pathlib import Path; "
        "from lib.checkpoint import init_project; "
        "from lib.source_media_review import review_source_media; "
        "project_id=sys.argv[1]; files=[Path(p) for p in sys.argv[2:]]; "
        "project_dir=init_project(project_id,title='AI Edit '+project_id,pipeline_type='hybrid'); "
        "review=review_source_media(files,{'pipeline_type':'hybrid','project_dir':str(project_dir)}); "
        "artifact=project_dir/'artifacts'/'source_media_review.json'; "
        "artifact.write_text(json.dumps(review,ensure_ascii=False,indent=2),encoding='utf-8'); "
        "print(json.dumps({'project_id':project_id,'project_dir':str(project_dir.resolve()),'artifact':str(artifact.resolve()),'review':review},ensure_ascii=False))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, project_id, *media_files],
        cwd=str(OPENMONTAGE_DIR), capture_output=True, text=True, encoding="utf-8", timeout=180,
    )
    if result.returncode != 0:
        raise RuntimeError("OpenMontage 初始化失败: " + (result.stderr.strip() or result.stdout.strip()))
    return json.loads(result.stdout.strip().splitlines()[-1])


async def _start_openmontage_backlot(project_id: str) -> str:
    preview_url = f"http://127.0.0.1:{OPENMONTAGE_BACKLOT_PORT}/p/{project_id}"
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            response = await client.get(f"http://127.0.0.1:{OPENMONTAGE_BACKLOT_PORT}/api/health")
            if response.status_code == 200:
                return preview_url
    except Exception:
        pass
    env = os.environ.copy()
    env["BACKLOT_PORT"] = str(OPENMONTAGE_BACKLOT_PORT)
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
    subprocess.Popen(
        [sys.executable, "-m", "backlot", "serve", "--port", str(OPENMONTAGE_BACKLOT_PORT)],
        cwd=str(OPENMONTAGE_DIR), env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=creationflags,
    )
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            async with httpx.AsyncClient(timeout=2) as client:
                response = await client.get(f"http://127.0.0.1:{OPENMONTAGE_BACKLOT_PORT}/api/health")
                if response.status_code == 200:
                    return preview_url
        except Exception:
            await asyncio.sleep(0.4)
    raise RuntimeError("OpenMontage Backlot 启动失败")


async def _start_hf_preview(task_id: str) -> str:
    task = _load_ai_edit_task(task_id)
    if not task or not task.get("project_dir"):
        raise HTTPException(400, "剪辑工程尚未生成")
    project_dir = task["project_dir"]
    port = 3100 + (int(task_id[:6], 16) % 700)
    cli = r"d:\video-skill\hyperframes\packages\cli\bin\hyperframes.mjs"
    cmd = ["node", cli, "preview", project_dir, "--background", "--no-open", "--port", str(port)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
    deadline = time.time() + 30
    output = []
    while time.time() < deadline:
        line = await asyncio.to_thread(proc.stdout.readline)
        if line:
            output.append(line.strip())
            if "Studio    http" in line or "Studio http" in line:
                break
        elif proc.poll() is not None:
            break
        else:
            await asyncio.sleep(0.1)
    preview_url = f"http://127.0.0.1:{port}"
    if proc.poll() is not None and proc.returncode:
        raise RuntimeError("HyperFrames 预览启动失败: " + " ".join(output[-5:]))
    task["preview_url"] = preview_url
    task["preview_output"] = output[-10:]
    _save_ai_edit_task(task_id)
    return preview_url


@app.get("/ai-edit", response_class=HTMLResponse)
async def ai_edit_page():
    html_path = Path(settings.FRONTEND_DIR) / "ai_edit.html"
    if not html_path.exists():
        raise HTTPException(404, "AI 剪辑页面未找到")
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.post("/api/ai-edit/create")
async def ai_edit_create(req: AiEditCreateRequest):
    from pipeline.oral_mashup_pipeline import run_asr_with_silence
    from pipeline.oral_mashup_hf_pipeline import generate_edit_plan_for_hf, build_hyperframes_project
    if not os.path.exists(req.main_video):
        raise HTTPException(400, "主视频不存在")
    if any(not os.path.exists(path) for path in req.broll_videos):
        raise HTTPException(400, "B-roll 素材不存在")
    task_id = uuid.uuid4().hex[:12]
    work_dir = _ai_edit_task_dir(task_id)
    work_dir.mkdir(parents=True, exist_ok=True)
    workflow_engine = req.workflow_engine.strip().lower()
    if workflow_engine not in ("openmontage", "hyperframes"):
        raise HTTPException(400, "不支持的剪辑引擎")
    task = {
        "task_id": task_id, "status": "RUNNING", "stage": "source_review",
        "message": "正在用 OpenMontage 审查素材" if workflow_engine == "openmontage" else "正在分析主视频",
        "main_video": req.main_video, "broll_videos": req.broll_videos,
        "engine": req.engine, "workflow_engine": workflow_engine,
        "work_dir": str(work_dir), "messages": [],
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    ai_edit_tasks[task_id] = task
    _save_ai_edit_task(task_id)

    async def build():
        try:
            if workflow_engine == "openmontage":
                om = await asyncio.to_thread(_init_openmontage_project, task_id, [req.main_video, *req.broll_videos])
                task.update({
                    "openmontage_project_id": om["project_id"],
                    "openmontage_project_dir": om["project_dir"],
                    "source_media_review": om["review"],
                    "source_media_artifact": om["artifact"],
                    "message": "OpenMontage 素材审查完成，正在生成剪辑方案",
                    "stage": "asr",
                })
                task["backlot_url"] = await _start_openmontage_backlot(om["project_id"])
                _save_ai_edit_task(task_id)
            asr_result = await run_asr_with_silence(req.main_video, work_dir)
            plan = await generate_edit_plan_for_hf(req.main_video, req.broll_videos, asr_result, work_dir, req.engine)
            project_dir = build_hyperframes_project(plan, req.main_video, req.broll_videos, work_dir)
            task.update({"status": "READY", "stage": "preview", "message": "OpenMontage 剪辑工程已生成" if workflow_engine == "openmontage" else "剪辑工程已生成", "project_dir": str(project_dir), "edit_plan": plan, "asr_result": asr_result})
            if workflow_engine == "openmontage":
                task["preview_url"] = task["backlot_url"]
            else:
                await _start_hf_preview(task_id)
        except Exception as e:
            task.update({"status": "FAILED", "stage": "error", "message": str(e), "error": str(e)})
        finally:
            _save_ai_edit_task(task_id)
    asyncio.create_task(build())
    return {"success": True, "task_id": task_id}


@app.post("/api/ai-edit/chat")
async def ai_edit_chat(req: AiEditChatRequest):
    from pipeline.oral_mashup_hf_pipeline import revise_edit_plan_for_hf, build_hyperframes_project
    task = _load_ai_edit_task(req.task_id)
    if not task:
        raise HTTPException(404, "剪辑任务不存在")
    if task.get("status") not in ("READY", "UPDATED"):
        raise HTTPException(409, "剪辑工程尚未准备好")
    if not req.message.strip():
        raise HTTPException(400, "请输入剪辑指令")
    task["status"] = "RUNNING"
    task["stage"] = "agent"
    task["message"] = "Agent 正在修改剪辑方案"
    _save_ai_edit_task(req.task_id)
    try:
        plan = await revise_edit_plan_for_hf(task["edit_plan"], req.message, task["main_video"], task["broll_videos"], task["asr_result"], Path(task["work_dir"]), req.engine)
        project_dir = build_hyperframes_project(plan, task["main_video"], task["broll_videos"], Path(task["work_dir"]))
        task["edit_plan"] = plan
        task["project_dir"] = str(project_dir)
        task.setdefault("messages", []).append({"role": "user", "content": req.message})
        task["status"] = "UPDATED"
        task["stage"] = "preview"
        task["message"] = "OpenMontage 剪辑方案已更新" if task.get("workflow_engine") == "openmontage" else "剪辑方案已更新"
        if task.get("workflow_engine") == "openmontage":
            instruction_path = Path(task["openmontage_project_dir"]) / "artifacts" / "edit_instruction.json"
            instruction_path.write_text(json.dumps({"instruction": req.message, "edit_plan": plan, "updated_at": datetime.now().isoformat()}, ensure_ascii=False, indent=2), encoding="utf-8")
            task["preview_url"] = task["backlot_url"]
        else:
            await _start_hf_preview(req.task_id)
        _save_ai_edit_task(req.task_id)
        return {"success": True, "task": task}
    except Exception as e:
        task.update({"status": "READY", "stage": "preview", "message": str(e), "error": str(e)})
        _save_ai_edit_task(req.task_id)
        raise HTTPException(400, str(e))


@app.post("/api/ai-edit/preview")
async def ai_edit_preview(req: AiEditTaskRequest):
    task = _load_ai_edit_task(req.task_id)
    if not task:
        raise HTTPException(404, "剪辑任务不存在")
    if task.get("workflow_engine") == "openmontage":
        preview_url = await _start_openmontage_backlot(task["openmontage_project_id"])
        task["preview_url"] = preview_url
        _save_ai_edit_task(req.task_id)
        return {"success": True, "preview_url": preview_url}
    return {"success": True, "preview_url": await _start_hf_preview(req.task_id)}


@app.post("/api/ai-edit/render")
async def ai_edit_render(req: AiEditTaskRequest):
    task = _load_ai_edit_task(req.task_id)
    if not task or not task.get("project_dir"):
        raise HTTPException(404, "剪辑工程不存在")
    from pipeline.oral_mashup_hf_pipeline import run_hyperframes_lint, run_hyperframes_render
    output_path = _ai_edit_task_dir(req.task_id) / "final.mp4"
    if not run_hyperframes_lint(Path(task["project_dir"])):
        raise HTTPException(400, "HyperFrames lint 未通过")
    if not run_hyperframes_render(Path(task["project_dir"]), output_path):
        raise HTTPException(500, "HyperFrames 渲染失败")
    task["final_video"] = str(output_path)
    task["status"] = "EXPORTED"
    _save_ai_edit_task(req.task_id)
    return {"success": True, "url": f"/api/result/file?path={output_path}"}


@app.get("/api/ai-edit/status")
async def ai_edit_status(task_id: str):
    task = _load_ai_edit_task(task_id)
    if not task:
        raise HTTPException(404, "剪辑任务不存在")
    return {"success": True, **task}


@app.get("/api/ai-edit/history")
async def ai_edit_history():
    history = []
    for path in AI_EDIT_OUTPUT_DIR.glob("*/task.json"):
        try:
            history.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    history.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return {"success": True, "history": history[:50]}


# ─────────────────────────────────────
# 一句话生视频（One-Shot Video）
# ─────────────────────────────────────

class OneShotCompileRequest(BaseModel):
    """Prompt 编译请求"""
    user_intent: str                          # 一句话意图（必填）
    product_image_path: Optional[str] = None  # 商品图本地路径（可选，兼容单图）
    product_image_paths: Optional[list] = None  # 多张商品图路径（可选，优先）
    product_info: Optional[str] = None        # 产品补充信息（可选）
    user_supplement: Optional[str] = None     # 客户补充意见（可选，最高优先级）
    variant_count: int = 3                    # 变体数量：1=单条推理（保留原行为），3=三变体（默认，痛点/场景/对比）
    variant_strategy: str = "all"             # 策略选择，覆盖 variant_count："all"(默认,3变体)/"auto"(单条)/"A"/"B"/"C"(指定风格单条)
    engine: str = "gpt"                       # 推理引擎："gpt"（默认，gpt-5.6-sol）/ "grok"（grok-4.6，SR_GROK_* 配置）


class OneShotGenerateRequest(BaseModel):
    """视频生成请求"""
    user_intent: str
    compiled_prompt: str
    metadata: dict = {}
    product_image_path: Optional[str] = None
    product_image_paths: Optional[list] = None
    aspect_ratio: str = "9:16"
    duration: str = "15s"
    resolution: str = "720p"


@app.get("/one-shot-video", response_class=HTMLResponse)
async def one_shot_video_page():
    """一句话生视频前端页面"""
    html_path = Path(settings.FRONTEND_DIR) / "one-shot-video.html"
    if not html_path.exists():
        raise HTTPException(404, "前端页面未找到")
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


def _resolve_image_path(p: str) -> Optional[str]:
    """把 /uploads/... 或 /outputs/... URL 路径解析为绝对路径。"""
    if not p:
        return None
    p = p.strip()
    if p.startswith("/uploads/"):
        return str(UPLOAD_DIR / p[len("/uploads/"):])
    if p.startswith("/outputs/"):
        return str(OUTPUT_DIR / p[len("/outputs/"):])
    return p


@app.post("/api/one-shot/compile")
async def one_shot_compile(req: OneShotCompileRequest):
    """编译一句话意图为 Seedance 2.0 视频生成 prompt。

    返回 metadata JSON + compiledPrompt 字符串供前端预览/编辑。
    支持多张商品图（product_image_paths）。
    """
    if not req.user_intent or not req.user_intent.strip():
        raise HTTPException(400, "user_intent 不能为空")

    # 解析商品图路径（支持多图 + 单图兼容）
    product_image_abs = _resolve_image_path(req.product_image_path) if req.product_image_path else None
    if product_image_abs and not Path(product_image_abs).exists():
        raise HTTPException(400, f"商品图不存在: {product_image_abs}")

    product_image_paths_abs = []
    for p in (req.product_image_paths or []):
        abs_p = _resolve_image_path(p)
        if abs_p and Path(abs_p).exists():
            product_image_paths_abs.append(abs_p)
        elif abs_p:
            raise HTTPException(400, f"商品图不存在: {abs_p}")

    try:
        from backend.services.prompt_compiler import compile_prompt
        result = await compile_prompt(
            user_intent=req.user_intent.strip(),
            product_image_path=product_image_abs,
            product_image_paths=product_image_paths_abs or None,
            product_info=req.product_info,
            user_supplement=req.user_supplement,
            variant_count=req.variant_count,
            engine=req.engine,
            variant_strategy=req.variant_strategy,
        )
        # 兼容两种返回结构
        if "variants" in result:
            return {
                "success": True,
                "variants": result["variants"],
                "specVersion": result["specVersion"],
            }
        else:
            return {
                "success": True,
                "metadata": result["metadata"],
                "compiledPrompt": result["compiledPrompt"],
                "specVersion": result["specVersion"],
            }
    except Exception as e:
        import traceback
        raise HTTPException(500, f"编译失败: {e}\n{traceback.format_exc()}")


class OptimizePromptRequest(BaseModel):
    """Prompt 自查优化请求"""
    compiled_prompt: str
    engine: str = "gpt"                       # 推理引擎："gpt"（默认）/ "grok"


@app.post("/api/one-shot/optimize-prompt")
async def one_shot_optimize_prompt(req: OptimizePromptRequest):
    """对已编译的 Prompt 做分镜逻辑自洽性自查并优化。

    调用 GPT 检查分镜是否有逻辑断层/不自洽，并确保严禁文字生成约束到位。
    返回优化后的完整 Prompt，前端可让用户对比决定是否采用。
    """
    if not req.compiled_prompt or not req.compiled_prompt.strip():
        raise HTTPException(400, "compiled_prompt 不能为空")

    try:
        from backend.services.prompt_compiler import optimize_prompt
        result = await optimize_prompt(req.compiled_prompt.strip(), engine=req.engine)
        return {
            "success": True,
            "optimizedPrompt": result["optimizedPrompt"],
            "originalLength": result["originalLength"],
            "optimizedLength": result["optimizedLength"],
            "changed": result["changed"],
        }
    except Exception as e:
        import traceback
        raise HTTPException(500, f"优化失败: {e}\n{traceback.format_exc()}")


@app.post("/api/one-shot/generate")
async def one_shot_generate(req: OneShotGenerateRequest):
    """启动视频生成任务。

    接收已编译的 compiledPrompt + 商品图，启动子进程调用 step5 RPA。
    支持防重复提交（基于 compiled_prompt + product_image MD5）。
    支持多张商品图（product_image_paths）。
    """
    if not req.compiled_prompt or not req.compiled_prompt.strip():
        raise HTTPException(400, "compiled_prompt 不能为空")

    # 解析商品图路径（支持多图）
    product_image_abs = _resolve_image_path(req.product_image_path) if req.product_image_path else None
    if product_image_abs and not Path(product_image_abs).exists():
        raise HTTPException(400, f"商品图不存在: {product_image_abs}")

    product_image_paths_abs = []
    for p in (req.product_image_paths or []):
        abs_p = _resolve_image_path(p)
        if abs_p and Path(abs_p).exists():
            product_image_paths_abs.append(abs_p)
        elif abs_p:
            raise HTTPException(400, f"商品图不存在: {abs_p}")

    from backend.services import oneshot_video

    # 防重复提交（多图参与指纹）
    dkey = oneshot_video._dedup_key(
        req.compiled_prompt, product_image_abs,
        req.aspect_ratio, req.duration, req.resolution,
        product_image_paths=product_image_paths_abs,
    )
    existing = oneshot_video.find_running_by_dedup(dkey)
    if existing:
        return {
            "success": True,
            "task_id": existing["task_id"],
            "deduplicated": True,
            "message": f"已有相同任务正在运行：{existing['task_id']}",
        }

    # 创建任务
    task = oneshot_video.create_task(
        user_intent=req.user_intent,
        compiled_prompt=req.compiled_prompt,
        metadata=req.metadata,
        product_image_path=product_image_abs,
        product_image_paths=product_image_paths_abs or None,
        aspect_ratio=req.aspect_ratio,
        duration=req.duration,
        resolution=req.resolution,
    )
    task_id = task["task_id"]

    # 异步启动生成
    asyncio.create_task(oneshot_video.start_generation(task_id))

    return {"success": True, "task_id": task_id, "deduplicated": False}


@app.get("/api/one-shot/status")
async def one_shot_status(task_id: str):
    """查询一句话生视频任务状态。"""
    from backend.services import task_store
    task = task_store.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    logs = task_store.get_logs(task_id, limit=50)
    outputs = task.get("outputs", []) or []
    # 处理视频 URL
    video_url = None
    if outputs:
        video_url = outputs[0].get("video_serve_url") or outputs[0].get("local_video")
    return {
        "success": True,
        "task_id": task_id,
        "status": task.get("status", "UNKNOWN"),
        "progress": task.get("progress", 0),
        "stage": task.get("stage", ""),
        "error": task.get("error", ""),
        "video_url": video_url,
        "outputs": outputs,
        "intermediate": task.get("intermediate", {}),
        "logs": logs,
        "params": task.get("params", {}),
    }


@app.get("/api/one-shot/events")
async def one_shot_events():
    """SSE：实时推送 one-shot 任务的运行状态和最新日志。
    每秒一次心跳/状态快照，运行中任务还会推送最近 5 条日志。
    """
    import asyncio, time, json
    from backend.services import task_store, oneshot_video
    last_log_ids = {}  # task_id -> 最后已推送的日志 ts

    async def stream():
        while True:
            try:
                running = task_store.list_running()
                # 只看 one-shot 类任务
                # 🔴 2026-10-08 bug 修复：原来写死字面量 "one_shot"，而写入侧的常量是
                #    backend/services/oneshot_video.py:30 TASK_TYPE = "one_shot_video" ⇒ 永不匹配，
                #    这个 SSE 对 one-shot 是死路径（running_count 恒 0），SKILL.md 里"或听 SSE"因此一直在骗人。
                #    改成引用同一个常量（不再抄字符串），两边再改名也不会脱钩。
                running = [t for t in running if t.get("task_type") == oneshot_video.TASK_TYPE]
                tasks_payload = []
                for t in running:
                    tid = t.get("task_id", "")
                    logs = task_store.get_logs(tid, limit=5) if tid else []
                    # 只推送新日志
                    new_logs = []
                    last_ts = last_log_ids.get(tid, 0)
                    for lg in logs:
                        if lg.get("ts", 0) > last_ts:
                            new_logs.append(lg)
                    if logs:
                        last_log_ids[tid] = max(lg.get("ts", 0) for lg in logs)
                    tasks_payload.append({
                        "task_id": tid,
                        "status": t.get("status", ""),
                        "progress": t.get("progress", 0),
                        "stage": t.get("stage", "") or (t.get("extra", {}) or {}).get("stage", ""),
                        "user_intent": (t.get("params", {}) or {}).get("user_intent", "")[:80],
                        "new_logs": new_logs,
                    })
                payload = {
                    "ts": int(time.time()),
                    "running_count": len(running),
                    "tasks": tasks_payload,
                }
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)[:200]})}\n\n"
            await asyncio.sleep(1.5)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/one-shot/history")
async def one_shot_history(limit: int = 30):
    """获取一句话生视频历史任务列表。"""
    from backend.services import oneshot_video
    items = oneshot_video.list_history(limit=limit)
    # 精简字段
    result = []
    for t in items:
        params = t.get("params", {}) or {}
        outputs = t.get("outputs", []) or []
        video_url = outputs[0].get("video_serve_url") if outputs else None
        result.append({
            "task_id": t["task_id"],
            "status": t.get("status", ""),
            "progress": t.get("progress", 0),
            "stage": t.get("stage", ""),
            "user_intent": params.get("user_intent", ""),
            "video_url": video_url,
            "created_at": t.get("created_at", 0),
            "updated_at": t.get("updated_at", 0),
        })
    return {"success": True, "items": result}


# ─────────────────────────────────────
# 爆款复刻-动作迁移（分阶段审核）
#   POST /api/replica-motion/start         → 启动 Phase1（Step1+2+3）→ after_step3 待审
#   GET  /api/replica-motion/review/{tid}  → 查看待审关键帧
#   POST /api/replica-motion/redo/{tid}    → 重做单 shot 关键帧替换
#   POST /api/replica-motion/approve/{tid} → 通过 → 执行 Phase2（Step4+5）→ 最终视频
#   GET  /api/replica-motion/status/{tid}  → 查询任务状态
#   GET  /api/replica-motion/history       → 历史记录
# ─────────────────────────────────────

# 内存任务存储（与 replica_phased_tasks 同模式）
replica_motion_tasks: dict = {}


class ReplicaMotionStartRequest(BaseModel):
    video_path: str
    product_image_paths: List[str]
    product_name: str = ""
    concurrency: int = 4
    split_mode: str = "scene_cut"  # "scene_cut"=镜头转场 | "action_grained"=细颗粒度动作 | "no_subject"=无人物（主体检测+光流）
    motion_mode: str = "multi"  # "multi"=多参考图V9 | "single"=单图AI App | "wan22"=Wan2.2 | "hand"=Wan21 SCAIL2（仅换工作流，走正常Step1-5）


def _rm_url(p) -> str:
    """把本地路径转成可访问的 /outputs/ URL（不可访问返回 ""）。"""
    try:
        pp = Path(p)
        if not pp.exists():
            return ""
        return "/" + pp.relative_to(Path(settings.OUTPUT_DIR).parent).as_posix()
    except Exception:
        return ""


def _rm_load_state(work_dir: str) -> dict:
    """从 work_dir/state.json 读取任务状态（用于断点恢复和历史记录补全）。"""
    sf = Path(work_dir) / "state.json"
    if not sf.exists():
        return {}
    try:
        return json.loads(sf.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _rm_build_intermediate(work_dir: str, video_path: str = "",
                            product_image_paths: list = None, product_name: str = "",
                            split_mode: str = "", phase: str = "") -> dict:
    """扫描 work_dir 产物，构建 intermediate（每步 input/output）。

    用于：
    - phase 完成时保存到 task_store
    - /history 和 /task/{id} 回显中间产物
    - 断点续传判断已完成步骤

    phase="phase_1" → 填充 input + step1/2/3
    phase="phase_2" → 追加 step4/5
    phase="all"     → 全部填充
    """
    wd = Path(work_dir)
    inter = {}

    # 整体输入
    inter["input"] = {
        "video_path": video_path,
        "video_url": _rm_url(video_path) if video_path else "",
        "product_image_paths": product_image_paths or [],
        "product_image_urls": [_rm_url(p) for p in (product_image_paths or [])],
        "product_name": product_name,
        "split_mode": split_mode,
    }

    # Step1: MiMo 分镜解析 → storyboard.json / timeline.json
    sb_file = wd / "step1" / "storyboard.json"
    if sb_file.exists():
        try:
            sb = json.loads(sb_file.read_text(encoding="utf-8"))
            shots_count = len(sb) if isinstance(sb, list) else 0
        except Exception:
            shots_count = 0
        inter["step1"] = {
            "input": {"video": video_path},
            "output": {
                "storyboard_url": _rm_url(sb_file),
                "timeline_url": _rm_url(wd / "step1" / "timeline.json"),
                "shots_count": shots_count,
            },
        }

    # Step2: 切片 + 抽帧 → keyframes/ + segments/
    kf_dir = wd / "step2" / "keyframes"
    seg_dir = wd / "step2" / "segments"
    if kf_dir.exists():
        kf_urls = sorted([_rm_url(f) for f in kf_dir.glob("shot_*.jpg")])
        seg_urls = sorted([_rm_url(f) for f in seg_dir.glob("shot_*.mp4")]) if seg_dir.exists() else []
        inter["step2"] = {
            "input": {"storyboard": "step1/storyboard.json"},
            "output": {"keyframes_urls": kf_urls, "segments_urls": seg_urls},
        }

    # Step3: 产品替换 → step3/shot_XX.jpg (+ _original.jpg)
    st3 = wd / "step3"
    if st3.exists():
        replaced = sorted([_rm_url(f) for f in st3.glob("shot_*.jpg") if "_original" not in f.name])
        originals = sorted([_rm_url(f) for f in st3.glob("shot_*_original.jpg")])
        inter["step3"] = {
            "input": {"keyframes": "step2/keyframes", "product_images": product_image_paths or []},
            "output": {"replaced_keyframes_urls": replaced, "original_keyframes_urls": originals},
        }

    # 参考图（跨 shot 一致性基准图）
    refs_dir = wd / "refs"
    if refs_dir.exists():
        inter["refs"] = {
            "face_ref_url": _rm_url(refs_dir / "face_ref.jpg"),
            "scene_ref_url": _rm_url(refs_dir / "scene_ref.jpg"),
            "model_full_ref_url": _rm_url(refs_dir / "model_full_ref.jpg"),
        }

    # Phase2 产物：Step4 动作迁移 shot 视频 + Step5 拼接最终视频
    if phase in ("phase_2", "all"):
        # Step4: 动作迁移 → step4/shot_XX.mp4
        st4 = wd / "step4"
        if st4.exists():
            shot_vids = sorted([_rm_url(f) for f in st4.glob("shot_*.mp4")])
            inter["step4"] = {
                "input": {"replaced_keyframes": "step3/", "refs": "refs/"},
                "output": {"shot_videos_urls": shot_vids},
            }
        # Step5: 拼接 → final.mp4
        final = wd / "final.mp4"
        if final.exists():
            inter["step5"] = {
                "input": {"shot_videos": "step4/"},
                "output": {"final_video_url": _rm_url(final)},
            }

    return inter


@app.get("/replica-motion", response_class=HTMLResponse)
async def replica_motion_page():
    """爆款复刻-动作迁移 - 二级页面"""
    html_path = Path(settings.FRONTEND_DIR) / "replica_motion.html"
    if not html_path.exists():
        raise HTTPException(404, "爆款复刻-动作迁移页面未找到")
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.post("/api/replica-motion/start")
async def replica_motion_start(req: ReplicaMotionStartRequest):
    """启动 Phase1：Step1(分镜)+Step2(切片+抽帧)+Step3(产品替换) → after_step3 待审"""
    if not req.video_path or not Path(req.video_path).exists():
        raise HTTPException(400, "视频文件不存在")
    if not req.product_image_paths:
        raise HTTPException(400, "必须提供至少 1 张产品图")
    for p in req.product_image_paths:
        if not Path(p).exists():
            raise HTTPException(400, f"产品图不存在: {p}")

    task_id = uuid.uuid4().hex[:12]
    work_dir = OUTPUT_DIR / "replica_motion" / task_id

    replica_motion_tasks[task_id] = {
        "status": "running",
        "phase": "phase_1",
        "step_name": "Step1+2+3: 分镜解析 + 切片段 + 产品替换",
        "progress": 0,
        "work_dir": str(work_dir),
        "video_path": req.video_path,
        "product_image_paths": req.product_image_paths,
        "product_name": req.product_name,
        "concurrency": req.concurrency,
        "split_mode": req.split_mode,
        "motion_mode": req.motion_mode,
        "review": None,
        "final_video": None,
        "shots": None,
        "error": None,
        "logs": [],
        "created_at": int(time.time()),
    }

    # 同步持久化到 task_store（历史记录/断点续传/中间产物回显依赖此）
    try:
        from backend.services import task_store
        task_store.create(task_id, "replica_motion",
            status="running", progress=0, stage="Step1+2+3: 分镜解析 + 切片段 + 产品替换",
            work_dir=str(work_dir),
            params={
                "video_path": req.video_path,
                "product_image_paths": req.product_image_paths,
                "product_name": req.product_name,
                "concurrency": req.concurrency,
                "split_mode": req.split_mode,
                "motion_mode": req.motion_mode,
                "phase": "phase_1",
            },
            intermediate=_rm_build_intermediate(str(work_dir), req.video_path,
                req.product_image_paths, req.product_name, req.split_mode, "phase_1"),
            extra={"phase": "phase_1", "current_step": "phase_1", "split_mode": req.split_mode,
                   "video_name": Path(req.video_path).name,
                   "product_name": req.product_name})
    except Exception as e:
        print(f"[RM] task_store 持久化失败（不影响运行）: {e}")

    asyncio.create_task(_run_replica_motion_phase(task_id, "phase_1"))
    return {"success": True, "task_id": task_id, "phase": "phase_1"}


@app.post("/api/replica-motion/approve/{task_id}")
async def replica_motion_approve(task_id: str):
    """通过关键帧审核 → 执行 Phase2a（仅 Step4 动作迁移），完成后等待动作迁移片段审核"""
    if task_id not in replica_motion_tasks:
        raise HTTPException(404, "任务不存在")
    task = replica_motion_tasks[task_id]
    if task["status"] != "awaiting_review":
        raise HTTPException(400, f"当前状态 {task['status']}，无法通过")
    if task.get("phase") != "after_step3":
        raise HTTPException(400, f"当前阶段 {task.get('phase')}，无需此操作（动作迁移片段审核请用 /approve-motion）")

    task.update({
        "status": "running",
        "phase": "phase_2a",
        "step_name": "Step4: 动作迁移",
        "progress": 0,
    })
    try:
        from backend.services import task_store
        task_store.update(task_id, status="running", progress=0,
            stage="Step4: 动作迁移",
            extra={"phase": "phase_2a", "current_step": "phase_2a"})
        task_store.add_log(task_id, "✅ 关键帧审核通过，进入 Step4 动作迁移")
    except Exception:
        pass
    asyncio.create_task(_run_replica_motion_phase(task_id, "phase_2a"))
    return {"success": True, "task_id": task_id, "phase": "phase_2a"}


class RedoShotRequest(BaseModel):
    shot_num: Optional[int] = None
    shot_nums: Optional[List[int]] = None  # 批量重做：多 shot 一次性提交
    product_image_paths: Optional[List[str]] = None


@app.post("/api/replica-motion/redo/{task_id}")
async def replica_motion_redo(task_id: str, req: RedoShotRequest):
    """重做关键帧替换（后台异步执行，立即返回，弹窗可关闭）

    支持两种模式：
      - 单 shot：传 shot_num
      - 批量：传 shot_nums（多 shot 一次性提交，并行后台跑，互不阻塞）
    两种模式都复用 redo_shot，自动走 _analyze_keyframe_for_replacement
    （LLM 先分析原关键帧，再精准替换），并共享 redoing_shots 并发追踪。

    请求立即返回，重做任务在后台运行，完成后自动回到 awaiting_review。
    前端通过轮询 status 获取进度，重做完成后刷新 review。
    """
    if task_id not in replica_motion_tasks:
        raise HTTPException(404, "任务不存在")
    task = replica_motion_tasks[task_id]
    # 支持并行重做：允许在 awaiting_review 或 running（重做进行中）状态下触发新重做
    if task["status"] not in ("awaiting_review", "running"):
        raise HTTPException(400, f"当前状态 {task['status']}，无法重做")
    if task.get("phase") != "after_step3":
        raise HTTPException(400, f"当前阶段 {task.get('phase')}，关键帧重做仅适用于 after_step3")

    # 归一化 shot 列表：优先 shot_nums，回退 shot_num
    if req.shot_nums:
        shot_list = list(req.shot_nums)
    elif req.shot_num is not None:
        shot_list = [req.shot_num]
    else:
        raise HTTPException(400, "必须传 shot_num 或 shot_nums")

    work_dir = task["work_dir"]
    product_image_paths = req.product_image_paths or task["product_image_paths"]

    # 并行重做：维护 redoing_shots 列表，避免对同一 shot 重复触发
    redoing = task.setdefault("redoing_shots", [])
    duplicates = [n for n in shot_list if n in redoing]
    if duplicates:
        raise HTTPException(400, f"shot_{[f'{n:02d}' for n in duplicates]} 正在重做中，请等待完成")
    redoing.extend(shot_list)

    # 不切 status，保持 awaiting_review，让其他 shot 可并行重做
    label = f"shot_{shot_list[0]:02d}" if len(shot_list) == 1 else f"{len(shot_list)} 个 shot"
    task["step_name"] = f"重做关键帧 {label}"
    task["error"] = None
    try:
        from backend.services import task_store
        task_store.update(task_id,
            stage=f"重做关键帧 {label}",
            extra={"phase": "after_step3", "current_step": "redo_step3",
                   "redoing_shots": redoing})
        task_store.add_log(task_id, f"🔄 重做 {label} 关键帧替换（后台并行）")
    except Exception:
        pass

    async def _bg_redo_keyframe(shot_num: int):
        """单 shot 重做后台任务（批量场景下每个 shot 独立 await）"""
        try:
            from pipeline.replica_motion import redo_shot
            await asyncio.to_thread(redo_shot, work_dir, shot_num, product_image_paths)
            task["review"] = _load_review_from_disk(work_dir)
            task["updated_at"] = int(time.time())
            task["step_name"] = "等待审核关键帧替换效果"
            try:
                from backend.services import task_store
                inter = _rm_build_intermediate(work_dir, task.get("video_path", ""),
                    task.get("product_image_paths"), task.get("product_name", ""),
                    task.get("split_mode", "scene_cut"), "phase_1")
                task_store.update(task_id, intermediate=inter,
                    stage="等待审核关键帧替换效果",
                    extra={"phase": "after_step3", "current_step": "after_step3",
                           "redoing_shots": redoing})
                task_store.add_log(task_id, f"✅ 重做 shot_{shot_num:02d} 关键帧完成，待审核")
            except Exception:
                pass
        except Exception as e:
            task["step_name"] = "等待审核关键帧替换效果"
            task["error"] = f"重做失败: {e}"
            try:
                from backend.services import task_store
                task_store.update(task_id,
                    stage="等待审核关键帧替换效果", error=f"重做失败: {str(e)[:300]}",
                    extra={"phase": "after_step3", "current_step": "after_step3",
                           "redoing_shots": redoing})
                task_store.add_log(task_id, f"❌ 重做 shot_{shot_num:02d} 失败: {e}")
            except Exception:
                pass
        finally:
            # 无论成功失败，从 redoing 列表移除
            if shot_num in redoing:
                redoing.remove(shot_num)
            try:
                from backend.services import task_store
                task_store.update(task_id,
                    extra={"phase": "after_step3", "current_step": "after_step3",
                           "redoing_shots": redoing})
            except Exception:
                pass

    # 批量场景：为每个 shot 启动独立后台任务，并行跑（共享 redoing_shots 追踪）
    # 限制并发为 2，与 step3_replace_products 一致，避免同时打多个 image2 压垮上游
    batch_sem = asyncio.Semaphore(2)

    async def _bg_redo_with_sem(shot_num: int):
        async with batch_sem:
            await _bg_redo_keyframe(shot_num)

    for sn in shot_list:
        asyncio.create_task(_bg_redo_with_sem(sn))

    return {"success": True, "task_id": task_id,
            "shot_num": shot_list[0] if len(shot_list) == 1 else None,
            "shot_nums": shot_list if len(shot_list) > 1 else None,
            "background": True}


class RedoMotionShotRequest(BaseModel):
    shot_num: int


@app.post("/api/replica-motion/redo-motion/{task_id}")
async def replica_motion_redo_motion(task_id: str, req: RedoMotionShotRequest):
    """重做单个 shot 的动作迁移（后台异步执行，立即返回）

    前提：state.phase = after_step4（动作迁移片段审核中）。
    删除该 shot 的 step4 输出，重新调用 _transfer_one_shot，完成后回到 after_step4。
    支持并行重做：不切 status，每个 shot 独立跑后台，互不阻塞。
    """
    if task_id not in replica_motion_tasks:
        raise HTTPException(404, "任务不存在")
    task = replica_motion_tasks[task_id]
    # 支持并行重做：允许在 awaiting_review 或 running（重做进行中）状态下触发新重做
    if task["status"] not in ("awaiting_review", "running"):
        raise HTTPException(400, f"当前状态 {task['status']}，无法重做")
    if task.get("phase") != "after_step4":
        raise HTTPException(400, f"当前阶段 {task.get('phase')}，动作迁移重做仅适用于 after_step4")

    work_dir = task["work_dir"]
    shot_num = req.shot_num

    # 并行重做：维护 redoing_shots 列表，避免对同一 shot 重复触发
    redoing = task.setdefault("redoing_shots", [])
    if shot_num in redoing:
        raise HTTPException(400, f"shot_{shot_num:02d} 正在重做中，请等待完成")
    redoing.append(shot_num)

    # 不切 status，保持 awaiting_review，让其他 shot 可并行重做
    task["step_name"] = f"重做动作迁移 shot_{shot_num:02d}"
    task["error"] = None
    try:
        from backend.services import task_store
        task_store.update(task_id,
            stage=f"重做动作迁移 shot_{shot_num:02d}",
            extra={"phase": "after_step4", "current_step": "redo_step4",
                   "redoing_shots": redoing})
        task_store.add_log(task_id, f"🔄 重做 shot_{shot_num:02d} 动作迁移（后台）")
    except Exception:
        pass

    async def _bg_redo_motion():
        try:
            from pipeline.replica_motion import redo_motion_shot
            await asyncio.to_thread(redo_motion_shot, work_dir, shot_num)
            task["updated_at"] = int(time.time())
            task["step_name"] = "等待审核动作迁移片段"
            try:
                from backend.services import task_store
                task_store.update(task_id,
                    stage="等待审核动作迁移片段",
                    extra={"phase": "after_step4", "current_step": "after_step4",
                           "redoing_shots": redoing})
                task_store.add_log(task_id, f"✅ 重做 shot_{shot_num:02d} 动作迁移完成，待审核")
            except Exception:
                pass
        except Exception as e:
            task["step_name"] = "等待审核动作迁移片段"
            task["error"] = f"动作迁移重做失败: {e}"
            try:
                from backend.services import task_store
                task_store.update(task_id,
                    stage="等待审核动作迁移片段", error=f"动作迁移重做失败: {str(e)[:300]}",
                    extra={"phase": "after_step4", "current_step": "after_step4",
                           "redoing_shots": redoing})
                task_store.add_log(task_id, f"❌ 重做 shot_{shot_num:02d} 动作迁移失败: {e}")
            except Exception:
                pass
        finally:
            # 无论成功失败，从 redoing 列表移除
            if shot_num in redoing:
                redoing.remove(shot_num)
            try:
                from backend.services import task_store
                task_store.update(task_id,
                    extra={"phase": "after_step4", "current_step": "after_step4",
                           "redoing_shots": redoing})
            except Exception:
                pass

    asyncio.create_task(_bg_redo_motion())
    return {"success": True, "task_id": task_id, "shot_num": shot_num, "background": True}


@app.post("/api/replica-motion/approve-motion/{task_id}")
async def replica_motion_approve_motion(task_id: str):
    """通过动作迁移片段审核 → 执行 Phase2b（Step5 拼接），输出最终视频"""
    if task_id not in replica_motion_tasks:
        raise HTTPException(404, "任务不存在")
    task = replica_motion_tasks[task_id]
    if task["status"] != "awaiting_review":
        raise HTTPException(400, f"当前状态 {task['status']}，无法通过")
    if task.get("phase") != "after_step4":
        raise HTTPException(400, f"当前阶段 {task.get('phase')}，无需此操作")

    task.update({
        "status": "running",
        "phase": "phase_2b",
        "step_name": "Step5: 拼接最终视频",
        "progress": 90,
    })
    try:
        from backend.services import task_store
        task_store.update(task_id, status="running", progress=90,
            stage="Step5: 拼接最终视频",
            extra={"phase": "phase_2b", "current_step": "phase_2b"})
        task_store.add_log(task_id, "✅ 动作迁移审核通过，进入 Step5 拼接最终视频")
    except Exception:
        pass
    asyncio.create_task(_run_replica_motion_phase(task_id, "phase_2b"))
    return {"success": True, "task_id": task_id, "phase": "phase_2b"}


@app.get("/api/replica-motion/review/{task_id}")
async def replica_motion_review(task_id: str):
    """查看待审关键帧（替换后 vs 原帧）"""
    if task_id not in replica_motion_tasks:
        raise HTTPException(404, "任务不存在")
    task = replica_motion_tasks[task_id]
    if task["status"] != "awaiting_review" and not task.get("review"):
        raise HTTPException(400, f"当前状态 {task['status']}，无待审内容")

    review = task.get("review")
    if not review:
        # 从磁盘读取
        review = _load_review_from_disk(task["work_dir"])
        task["review"] = review
    # 附加产品图 URL（中间产物回显）
    product_image_paths = task.get("product_image_paths") or []
    review["product_image_urls"] = [_rm_url(p) for p in product_image_paths]
    return {"success": True, "review": review, "task": _task_summary(task_id)}


@app.get("/api/replica-motion/status/{task_id}")
async def replica_motion_status(task_id: str):
    """查询任务状态（内存字典没有时从 task_store 恢复，支持刷新页面/服务重启后继续）"""
    if task_id not in replica_motion_tasks:
        from backend.services import task_store
        t = task_store.get(task_id)
        if not t or t.get("task_type") != "replica_motion":
            raise HTTPException(404, "任务不存在")
        params = t.get("params", {}) or {}
        work_dir = t.get("work_dir", "")
        if not work_dir or not Path(work_dir).exists():
            raise HTTPException(400, "工作目录不存在，无法恢复")
        _state_motion = None
        try:
            _sd = json.loads((Path(work_dir) / "state.json").read_text(encoding="utf-8"))
            _state_motion = _sd.get("motion_mode")
        except Exception:
            pass
        replica_motion_tasks[task_id] = {
            "status": str(t.get("status", "failed")).lower(),
            "phase": t.get("extra", {}).get("phase", "phase_1"),
            "step_name": t.get("stage", ""),
            "progress": t.get("progress", 0),
            "work_dir": work_dir,
            "video_path": params.get("video_path", ""),
            "product_image_paths": params.get("product_image_paths", []),
            "product_name": params.get("product_name", ""),
            "concurrency": params.get("concurrency", 4),
            "split_mode": params.get("split_mode", "scene_cut"),
            "motion_mode": _state_motion or params.get("motion_mode", "multi"),
            "review": None,
            "final_video": t.get("outputs", [{}])[0].get("fileUrl") if t.get("outputs") else None,
            "shots": None,
            "error": t.get("error", ""),
            "logs": [],
            "created_at": t.get("created_at", int(time.time())),
        }
    return _task_summary(task_id)


@app.post("/api/replica-motion/resume/{task_id}")
async def replica_motion_resume(task_id: str):
    """断点重试：从上次失败的 step 继续（已完成的 step/shot 自动跳过）

    适用场景：
    - Phase1 中 Step1/2/3 任一失败后重试
    - Phase2 中 Step4/5 失败后重试
    - 任务超时或进程异常后恢复
    - 服务重启后恢复（task_store 中标记为 failed 的任务）

    原理：run_phase_1 / run_phase_2 内部检查产物，已存在则跳过。
    若任务不在内存字典（服务重启后），从 task_store + 磁盘重建内存任务后恢复。
    """
    from backend.services import task_store

    # 内存字典没有则从 task_store 恢复（服务重启场景）
    if task_id not in replica_motion_tasks:
        t = task_store.get(task_id)
        if not t or t.get("task_type") != "replica_motion":
            raise HTTPException(404, "任务不存在")
        params = t.get("params", {}) or {}
        work_dir = t.get("work_dir", "")
        if not work_dir or not Path(work_dir).exists():
            raise HTTPException(400, "工作目录不存在，无法恢复")
        # 重建内存任务（status 统一转小写，与 replica_motion 现有约定一致）
        # motion_mode 优先从 state.json 读（用户可能修改过），其次 params，最后默认 multi
        _state_motion = None
        try:
            _sd = json.loads((Path(work_dir) / "state.json").read_text(encoding="utf-8"))
            _state_motion = _sd.get("motion_mode")
        except Exception:
            pass
        replica_motion_tasks[task_id] = {
            "status": str(t.get("status", "failed")).lower(),
            "phase": t.get("extra", {}).get("phase", "phase_1"),
            "step_name": t.get("stage", ""),
            "progress": t.get("progress", 0),
            "work_dir": work_dir,
            "video_path": params.get("video_path", ""),
            "product_image_paths": params.get("product_image_paths", []),
            "product_name": params.get("product_name", ""),
            "concurrency": params.get("concurrency", 4),
            "split_mode": params.get("split_mode", "scene_cut"),
            "motion_mode": _state_motion or params.get("motion_mode", "multi"),
            "review": None,
            "final_video": t.get("outputs", [{}])[0].get("fileUrl") if t.get("outputs") else None,
            "shots": None,
            "error": t.get("error", ""),
            "logs": [],
            "created_at": t.get("created_at", int(time.time())),
        }

    task = replica_motion_tasks[task_id]
    # 只允许 failed 状态重试；running/success/awaiting_review 不需要
    if task["status"] == "running":
        raise HTTPException(400, "任务正在运行中，无需重试")
    if task["status"] in ("success", "awaiting_review"):
        raise HTTPException(400, f"当前状态 {task['status']}，无需重试（success 已完成 / awaiting_review 请用 approve 或 redo）")

    phase = task.get("phase", "phase_1")
    # 判断从哪个阶段恢复（优先用 state.json 的 phase 判断，更准确）
    # - after_step4 → Step4 已完成，只需拼接（phase_2b）
    # - after_step3 → Step4 未跑，恢复 phase_2a（已完成的 shot 自动跳过）
    # - done 但无最终视频 → phase_2b 失败，恢复 phase_2b
    # - 其他 → 恢复 phase_1（Step1/2/3 已完成的会自动跳过）
    state_file = Path(task["work_dir"]) / "state.json"
    _state_phase = None
    if state_file.exists():
        try:
            _state_phase = json.loads(state_file.read_text(encoding="utf-8")).get("phase")
        except Exception:
            pass

    if _state_phase == "after_step4":
        resume_phase = "phase_2b"
    elif _state_phase == "after_step3":
        resume_phase = "phase_2a"
    elif _state_phase == "done" and task.get("final_video") is None:
        resume_phase = "phase_2b"
    elif phase in ("phase_2a", "phase_2b"):
        resume_phase = phase
    elif phase in ("phase_2", "after_step3", "done") and task.get("final_video") is None:
        # 旧任务兼容（approve 走过 phase_2 的历史任务）
        resume_phase = "phase_2a"
    else:
        resume_phase = "phase_1"

    task.update({
        "status": "running",
        "phase": resume_phase,
        "step_name": f"断点重试（{resume_phase}）",
        "progress": 0,
        "error": None,
    })
    # 同步 task_store
    try:
        task_store.update(task_id, status="running", progress=0,
            stage=f"断点重试（{resume_phase}）", error="",
            extra={"phase": resume_phase, "current_step": resume_phase,
                   "split_mode": task.get("split_mode", "scene_cut"),
                   "video_name": Path(task.get("video_path", "")).name,
                   "product_name": task.get("product_name", "")})
        task_store.add_log(task_id, f"↻ 断点重试：从 {resume_phase} 恢复（已完成步骤自动跳过）")
    except Exception:
        pass
    asyncio.create_task(_run_replica_motion_phase(task_id, resume_phase))
    return {"success": True, "task_id": task_id, "phase": resume_phase, "resume": True}


@app.get("/api/replica-motion/history")
async def replica_motion_history(limit: int = 50):
    """历史记录（参考女装带货设计：从 task_store 读取 + 磁盘扫描补全产物 URL）。

    返回每个任务的完整信息：状态、输入输出、中间产物（分镜/关键帧/最终视频），
    供前端历史记录面板回显。不依赖内存字典，服务重启后历史不丢。
    """
    from backend.services import task_store
    items = []

    # 1. 从 task_store 读取持久化任务
    try:
        tasks = task_store.list_by_type("replica_motion", limit=limit)
    except Exception:
        tasks = []

    seen_dirs = set()
    for t in tasks:
        tid = t["task_id"]
        work_dir = t.get("work_dir", "")
        seen_dirs.add(Path(work_dir).name) if work_dir else None
        extra = t.get("extra", {}) or {}
        inter = t.get("intermediate", {}) or {}
        params = t.get("params", {}) or {}

        # 实时扫描磁盘补全 intermediate（防止 task_store 中数据过期）
        if work_dir and Path(work_dir).exists():
            scan_phase = "all" if t.get("status") == "success" else \
                         ("phase_2" if extra.get("phase") in ("phase_2", "done") else "phase_1")
            fresh_inter = _rm_build_intermediate(work_dir, params.get("video_path", ""),
                params.get("product_image_paths", []), params.get("product_name", ""),
                params.get("split_mode", "scene_cut"), scan_phase)
            # 合并：磁盘扫描结果优先，task_store 的作为兜底
            for k in fresh_inter:
                if fresh_inter[k] and fresh_inter[k].get("output"):
                    inter[k] = fresh_inter[k]
            # 输入信息以 task_store 为准（磁盘扫描没有）
            if "input" not in inter:
                inter["input"] = fresh_inter.get("input", {})

        outputs = t.get("outputs", []) or []
        final_video_url = outputs[0].get("fileUrl", "") if outputs else extra.get("final_video_url", "")

        items.append({
            "task_id": tid,
            "status": str(t.get("status", "")).lower(),
            "phase": extra.get("phase", t.get("stage", "")),
            "progress": t.get("progress", 0),
            "stage": t.get("stage", ""),
            "error": t.get("error", ""),
            "failed_step": extra.get("failed_step", ""),
            "split_mode": params.get("split_mode", extra.get("split_mode", "")),
            "motion_mode": params.get("motion_mode", ""),
            "video_name": extra.get("video_name", "") or Path(params.get("video_path", "")).name,
            "product_name": params.get("product_name", "") or extra.get("product_name", ""),
            "final_video_url": final_video_url,
            "intermediate": inter,
            "input": inter.get("input", {}),
            "shots_count": inter.get("step1", {}).get("output", {}).get("shots_count", 0),
            "created_at": t.get("created_at", 0),
            "created_at_str": time.strftime("%Y-%m-%d %H:%M:%S",
                time.localtime(t.get("created_at", 0))) if t.get("created_at") else "",
            "updated_at": t.get("updated_at", 0),
        })

    # 2. 扫描磁盘补全：task_store 没有但有 work_dir 产物的任务（老任务/异常未落盘）
    # 仅恢复有最终视频的成功任务，跳过失败/未完成的目录（磁盘产物保留但不展示）
    rm_base = OUTPUT_DIR / "replica_motion"
    if rm_base.exists():
        for d in sorted(rm_base.iterdir(),
                        key=lambda p: p.stat().st_mtime if p.is_dir() else 0, reverse=True):
            if not d.is_dir() or d.name in seen_dirs:
                continue
            # 只恢复有最终视频的任务（success），跳过失败/未完成的
            inter = _rm_build_intermediate(str(d), "", [], "", "", "all")
            if not inter.get("step1"):
                continue  # 无任何产物，跳过
            final_url = inter.get("step5", {}).get("output", {}).get("final_video_url", "")
            if not final_url:
                continue  # 无最终视频，视为失败/未完成，跳过（磁盘产物保留）
            st = d.stat()
            items.append({
                "task_id": d.name,
                "status": "success",
                "phase": "done",
                "progress": 100,
                "stage": "完成（磁盘恢复）",
                "error": "",
                "failed_step": "",
                "split_mode": "",
                "motion_mode": "",
                "video_name": "",
                "product_name": "",
                "final_video_url": final_url,
                "intermediate": inter,
                "input": inter.get("input", {}),
                "shots_count": inter.get("step1", {}).get("output", {}).get("shots_count", 0),
                "created_at": int(st.st_mtime),
                "created_at_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
                "updated_at": int(st.st_mtime),
            })

    # 按 created_at 降序
    items.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return {"success": True, "items": items[:limit]}


@app.get("/api/replica-motion/task/{task_id}")
async def replica_motion_task_detail(task_id: str):
    """获取单个任务详情（含中间产物、日志），供前端点击历史记录查看完整回显。

    参考女装带货 /api/womenswear-studio/task/{task_id} 设计。
    优先从内存字典取实时状态，task_store 取持久化数据，磁盘扫描补全产物。
    """
    from backend.services import task_store

    # 内存字典有则用内存（实时）
    if task_id in replica_motion_tasks:
        t = replica_motion_tasks[task_id]
        work_dir = t["work_dir"]
        scan_phase = "all" if t.get("final_video") else "phase_1"
        inter = _rm_build_intermediate(work_dir, t.get("video_path", ""),
            t.get("product_image_paths"), t.get("product_name", ""),
            t.get("split_mode", "scene_cut"), scan_phase)
        logs = t.get("logs", [])
    else:
        # 从 task_store 取
        t = task_store.get(task_id)
        if not t or t.get("task_type") != "replica_motion":
            # 磁盘扫描兜底：task_store 无记录但有 work_dir 产物的老任务
            d = OUTPUT_DIR / "replica_motion" / task_id
            if d.exists() and d.is_dir():
                scan_phase = "all" if (d / "final.mp4").exists() else "phase_1"
                inter = _rm_build_intermediate(str(d), "", [], "", "", scan_phase)
                final_url = inter.get("step5", {}).get("output", {}).get("final_video_url", "")
                work_dir = str(d)
                t = {
                    "status": "success" if final_url else "unknown",
                    "phase": "done" if final_url else "",
                    "progress": 100 if final_url else 0,
                    "stage": "完成（磁盘恢复）" if final_url else "未知（磁盘恢复）",
                    "error": "",
                    "extra": {"phase": "done" if final_url else ""},
                }
                logs = []
            else:
                raise HTTPException(404, "任务不存在")
        else:
            work_dir = t.get("work_dir", "")
            params = t.get("params", {}) or {}
            extra = t.get("extra", {}) or {}
            scan_phase = "all" if t.get("status") == "success" else \
                         ("phase_2" if extra.get("phase") in ("phase_2", "done") else "phase_1")
            inter = t.get("intermediate", {}) or {}
            if work_dir and Path(work_dir).exists():
                fresh = _rm_build_intermediate(work_dir, params.get("video_path", ""),
                    params.get("product_image_paths", []), params.get("product_name", ""),
                    params.get("split_mode", "scene_cut"), scan_phase)
                for k in fresh:
                    if fresh[k] and fresh[k].get("output"):
                        inter[k] = fresh[k]
            try:
                logs = task_store.get_logs(task_id, limit=300)
            except Exception:
                logs = []

    review = _load_review_from_disk(work_dir) if work_dir else {"items": []}
    return {
        "success": True,
        "task_id": task_id,
        "status": str(t.get("status", "")).lower(),
        "phase": t.get("phase", t.get("extra", {}).get("phase", "")) if isinstance(t.get("phase"), str) else t.get("extra", {}).get("phase", ""),
        "progress": t.get("progress", 0),
        "stage": t.get("stage", t.get("step_name", "")),
        "error": t.get("error", ""),
        "work_dir": work_dir,
        "intermediate": inter,
        "review": review,
        "logs": logs,
        "updated_at": t.get("updated_at", 0),
    }


@app.get("/api/replica-motion/logs/{task_id}")
async def replica_motion_logs(task_id: str):
    """获取任务日志（内存字典没有时从 task_store 读取持久化日志）"""
    if task_id not in replica_motion_tasks:
        from backend.services import task_store
        try:
            logs = task_store.get_logs(task_id, limit=500)
            return {"success": True, "logs": [l.get("msg", "") for l in logs]}
        except Exception:
            raise HTTPException(404, "任务不存在")
    return {"success": True, "logs": replica_motion_tasks[task_id].get("logs", [])}


def _task_summary(task_id: str) -> dict:
    t = replica_motion_tasks[task_id]
    # 扫描 step4 目录获取逐 shot 动作迁移进度（中间产物回显）
    step4_progress = []
    wd = t.get("work_dir", "")
    if wd:
        from pathlib import Path as P
        s4dir = P(wd) / "step4"
        state_file = P(wd) / "state.json"
        all_shots = []
        no_product = set()
        if state_file.exists():
            try:
                sd = json.loads(state_file.read_text(encoding="utf-8"))
                all_shots = sd.get("all_shots", [])
                no_product = set(sd.get("no_product_shots", []))
            except Exception:
                pass
        for n in all_shots:
            sp = s4dir / f"shot_{n:02d}.mp4"
            step4_progress.append({
                "shot": n,
                "status": "done" if (sp.exists() and sp.stat().st_size > 1024) else "pending",
                "is_no_product": n in no_product,
                "video_url": _rm_url(str(sp)) if sp.exists() else None,
            })
    return {
        "task_id": task_id,
        "status": t["status"],
        "phase": t.get("phase", ""),
        "step_name": t.get("step_name", ""),
        "progress": t.get("progress", 0),
        "work_dir": t.get("work_dir", ""),
        "final_video": t.get("final_video"),
        "shots": t.get("shots"),
        "review": t.get("review"),
        "error": t.get("error"),
        "logs": t.get("logs", [])[-50:],
        "updated_at": t.get("updated_at", 0),
        "step4_progress": step4_progress,
        "redoing_shots": t.get("redoing_shots", []) or [],
        "product_image_urls": [_rm_url(p) for p in (t.get("product_image_paths") or [])],
    }


def _load_review_from_disk(work_dir: str) -> dict:
    """从磁盘读取 review 数据"""
    from pathlib import Path as P
    wd = P(work_dir)
    state_file = wd / "state.json"
    if not state_file.exists():
        return {"items": []}
    state = json.loads(state_file.read_text(encoding="utf-8"))
    all_shots = state.get("all_shots", [])
    no_product = set(state.get("no_product_shots", []))
    kf_replaced = wd / "step3"
    kf_orig = wd / "step2" / "keyframes"

    items = []
    for n in all_shots:
        rp = kf_replaced / f"shot_{n:02d}.jpg"
        op = kf_orig / f"shot_{n:02d}.jpg"
        items.append({
            "shot": n,
            "replaced": str(rp) if rp.exists() else None,
            "original": str(op) if op.exists() else None,
            "is_no_product": n in no_product,
        })
    return {
        "keyframes_dir": str(kf_replaced),
        "original_keyframes_dir": str(kf_orig),
        "items": items,
        "all_shots": all_shots,
        "no_product_shots": sorted(no_product),
    }


async def _refresh_review(task_id: str):
    """刷新任务的 review 数据"""
    t = replica_motion_tasks[task_id]
    t["review"] = _load_review_from_disk(t["work_dir"])
    t["updated_at"] = int(time.time())


def _sync_rm_to_store(task_id: str, task: dict, phase: str, status: str,
                      error: str = "", progress: int = None, stage: str = "",
                      final_video: str = ""):
    """把 replica_motion 内存任务状态同步到 task_store（持久化）。

    同时刷新 intermediate（扫描 work_dir 产物），保证历史记录/详情/断点续传数据准确。
    成功（success）时写入 outputs；失败时记录 failed_step 供 /resume 使用。
    """
    try:
        from backend.services import task_store
        work_dir = task.get("work_dir", "")
        video_path = task.get("video_path", "")
        product_paths = task.get("product_image_paths", [])
        product_name = task.get("product_name", "")
        split_mode = task.get("split_mode", "scene_cut")

        # 扫描产物构建 intermediate（phase_2 会包含 step4/5）
        scan_phase = "all" if status == "success" else ("phase_2" if phase == "phase_2" else "phase_1")
        inter = _rm_build_intermediate(work_dir, video_path, product_paths,
                                        product_name, split_mode, scan_phase)

        extra = {
            "phase": phase if status != "success" else "done",
            "current_step": phase if status == "running" else (phase if status == "failed" else status),
            "split_mode": split_mode,
            "video_name": Path(video_path).name if video_path else "",
            "product_name": product_name,
        }
        if status == "failed":
            extra["failed_step"] = phase
        if final_video:
            extra["final_video_url"] = _rm_url(final_video)

        fields = {
            "status": status,
            "intermediate": inter,
            "extra": extra,
        }
        if progress is not None:
            fields["progress"] = progress
        if stage:
            fields["stage"] = stage
        if error:
            fields["error"] = error[:500]
        if status == "success" and final_video:
            fields["outputs"] = [{"fileUrl": _rm_url(final_video), "fileType": "mp4"}]
        elif status == "success":
            # 没有 final_video 也算成功，outputs 留空
            fields["outputs"] = []

        task_store.update(task_id, **fields)
    except Exception as e:
        print(f"[RM] _sync_rm_to_store 失败（不影响运行）: {e}")


async def _run_replica_motion_phase(task_id: str, phase: str):
    """子进程方式运行阶段任务"""
    from pathlib import Path as P
    task = replica_motion_tasks[task_id]
    work_dir = task["work_dir"]

    if phase == "phase_1":
        params = {
            "video_path": task["video_path"],
            "product_image_paths": task["product_image_paths"],
            "product_name": task["product_name"],
            "work_dir": work_dir,
            "concurrency": task["concurrency"],
            "task_id": task_id,
            "split_mode": task.get("split_mode", "scene_cut"),
            "motion_mode": task.get("motion_mode"),
        }
        phase_func = "run_phase_1"
    elif phase == "phase_2":
        params = {
            "work_dir": work_dir,
            "concurrency": task["concurrency"],
            "motion_mode": task.get("motion_mode"),
        }
        phase_func = "run_phase_2"
    elif phase == "phase_2a":
        params = {
            "work_dir": work_dir,
            "concurrency": task["concurrency"],
            "motion_mode": task.get("motion_mode"),
        }
        phase_func = "run_phase_2a"
    elif phase == "phase_2b":
        params = {
            "work_dir": work_dir,
        }
        phase_func = "run_phase_2b"
    else:
        task.update({"status": "failed", "error": f"未知阶段: {phase}"})
        return

    param_file = OUTPUT_DIR / f"_rm_params_{task_id}.json"
    param_file.write_text(json.dumps(params, ensure_ascii=False), encoding="utf-8")
    result_file = OUTPUT_DIR / f"_rm_result_{task_id}.json"
    runner_script = OUTPUT_DIR / f"_rm_runner_{task_id}.py"

    runner_script.write_text(
        f'''
import json, sys, os, io, logging, builtins
os.environ["PYTHONUTF8"] = "1"
os.environ["PYTHONIOENCODING"] = "utf-8"
sys.path.insert(0, r"{P(__file__).parent}")
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
if hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)

# Windows 管道 flush 可能抛 OSError [Errno 22]，吞掉 print 异常避免中断流程
_orig_print = builtins.print
def _safe_print(*a, **k):
    try:
        _orig_print(*a, **k)
    except Exception:
        pass
builtins.print = _safe_print

params = json.loads(open(r"{param_file}", encoding="utf-8").read())
try:
    from pipeline.replica_motion import {phase_func}
    result = {phase_func}(**params)
    with open(r"{result_file}", "w", encoding="utf-8") as f:
        json.dump({{"success": True, "result": result}}, f, ensure_ascii=False, default=str)
except Exception as e:
    import traceback
    tb = traceback.format_exc()
    with open(r"{result_file}", "w", encoding="utf-8") as f:
        json.dump({{"success": False, "error": str(e), "traceback": tb}}, f, ensure_ascii=False)
''',
        encoding="utf-8",
    )

    def _log(msg: str):
        task["logs"].append(msg)
        task["updated_at"] = int(time.time())
        # 同步日志到 task_store（历史记录/详情回显依赖此）
        try:
            from backend.services import task_store
            task_store.add_log(task_id, msg)
        except Exception:
            pass

    try:
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        process = await asyncio.create_subprocess_exec(
            sys.executable, "-X", "utf8", str(runner_script),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
            cwd=str(P(__file__).parent),
        )

        timeout_sec = 1800  # 30 分钟
        start_time = asyncio.get_event_loop().time()
        while True:
            try:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=10.0)
            except asyncio.TimeoutError:
                line = b""
            if line:
                msg = line.decode("utf-8", errors="replace").rstrip()
                _log(msg)
                echo(f"[RM-{task_id}] {msg}")
            if process.stdout.at_eof():
                break
            if asyncio.get_event_loop().time() - start_time > timeout_sec:
                process.kill()
                task.update({"status": "failed", "error": f"{phase} 超时（{timeout_sec}s）"})
                _sync_rm_to_store(task_id, task, phase, "failed", f"{phase} 超时（{timeout_sec}s）")
                return

        await asyncio.wait_for(process.wait(), timeout=30.0)

        if not result_file.exists():
            task.update({"status": "failed", "error": f"{phase} 未生成结果文件"})
            _sync_rm_to_store(task_id, task, phase, "failed", f"{phase} 未生成结果文件")
            return

        data = json.loads(result_file.read_text(encoding="utf-8"))
        if not data.get("success"):
            task.update({
                "status": "failed",
                "error": data.get("error", "未知错误"),
                "phase": phase,
            })
            _log(f"[FAIL] {data.get('error', '')[:200]}")
            _sync_rm_to_store(task_id, task, phase, "failed", data.get('error', '未知错误')[:300])
            return

        result = data["result"]

        if phase == "phase_1":
            # ── Hand 模式 skipped 分支：未检测到主体 → 整段保留原视频，直接 success ──
            if result.get("hand_mode") and result.get("skipped_no_subject"):
                final_video = result.get("final_video")
                task.update({
                    "status": "success",
                    "phase": "done",
                    "step_name": "完成（手部模式-未检测到主体）",
                    "progress": 100,
                    "final_video": final_video,
                    "shots": result.get("timeline", []),
                    "hand_mode": True,
                    "skipped_no_subject": True,
                })
                _log(f"[Hand模式] 未检测到主体，整段保留原视频: {final_video}")
                _sync_rm_to_store(task_id, task, phase, "success", "",
                                  progress=100, stage="完成（手部模式-未检测到主体）",
                                  final_video=final_video)
                return

            # 常规流程（含 Hand 模式常规路径）：进入待审状态
            review = result.get("review", {})
            task.update({
                "status": "awaiting_review",
                "phase": "after_step3",
                "step_name": "等待审核关键帧替换效果",
                "progress": 50,
                "review": review,
                "storyboard": result.get("storyboard"),
                "all_shots": result.get("all_shots"),
                "product_shots": result.get("product_shots"),
                "no_product_shots": result.get("no_product_shots"),
                "hand_mode": result.get("hand_mode", False),
            })
            _log("[Phase1] 完成，等待审核。可通过 /redo 重做单 shot，或 /approve 进入 Phase2。")
            _sync_rm_to_store(task_id, task, phase, "awaiting_review", "",
                              progress=50, stage="等待审核关键帧替换效果")
        elif phase == "phase_2a":
            # Hand 模式：Phase1 已出最终视频，直接 success
            if result.get("hand_mode") or result.get("phase") == "done":
                final_video = result.get("final_video")
                task.update({
                    "status": "success", "phase": "done",
                    "step_name": "完成", "progress": 100,
                    "final_video": final_video,
                    "shots": result.get("shots"),
                    "hand_mode": True,
                })
                _log(f"[Phase2a] Hand 模式，最终视频: {final_video}")
                _sync_rm_to_store(task_id, task, phase, "success", "",
                                  progress=100, stage="完成", final_video=final_video)
            else:
                task.update({
                    "status": "awaiting_review",
                    "phase": "after_step4",
                    "step_name": "等待审核动作迁移片段",
                    "progress": 75,
                    "shots": result.get("shots"),
                })
                _log("[Phase2a] Step4 动作迁移完成，等待审核。可 /redo-motion 重做单片，或 /approve-motion 合并。")
                _sync_rm_to_store(task_id, task, phase, "awaiting_review", "",
                                  progress=75, stage="等待审核动作迁移片段")
        elif phase == "phase_2b":
            final_video = result.get("final_video")
            task.update({
                "status": "success",
                "phase": "done",
                "step_name": "完成",
                "progress": 100,
                "final_video": final_video,
                "shots": result.get("shots"),
            })
            _log(f"[Phase2b] 合并完成！最终视频: {final_video}")
            _sync_rm_to_store(task_id, task, phase, "success", "",
                              progress=100, stage="完成", final_video=final_video)
        elif phase == "phase_2":
            final_video = result.get("final_video")
            task.update({
                "status": "success",
                "phase": "done",
                "step_name": "完成",
                "progress": 100,
                "final_video": final_video,
                "shots": result.get("shots"),
            })
            _log(f"[Phase2] 完成！最终视频: {final_video}")
            _sync_rm_to_store(task_id, task, phase, "success", "",
                              progress=100, stage="完成", final_video=final_video)

    except Exception as e:
        task.update({"status": "failed", "error": str(e), "phase": phase})
        _log(f"[EXCEPTION] {e}")
        _sync_rm_to_store(task_id, task, phase, "failed", str(e)[:300])
    finally:
        for f in [param_file, runner_script]:
            try:
                f.unlink()
            except Exception:
                pass


# ─────────────────────────────────────
# One-Shot Replica API：爆款视频 → MiMo 解析 → LLM 反推 → Seedance 生成
# 改造原一句话生视频为「上传爆款视频→复刻」流程，保留 /one-shot 页面 URL
# ─────────────────────────────────────

# 任务状态存储（reverse→generate 同一 task_id 贯穿全流程）
oneshot_replica_tasks: dict = {}


class OneShotReplicaSubmitRequest(BaseModel):
    """反推启动请求"""
    video_path: str                     # 上传的爆款视频本地路径
    duration: str = "5"                 # 目标时长（5/8/10/15）
    aspect_ratio: str = "9:16"
    resolution: str = "720p"
    version: str = "2.0"                # Seedance 版本
    engine: str = "grok"                # LLM 引擎（grok 优先，gpt 备用）
    product_image_paths: list = []      # 产品图路径列表（可选）
    extra_hint: str = ""


@app.post("/api/one-shot-replica/submit")
async def oneshot_replica_submit(req: OneShotReplicaSubmitRequest):
    """启动爆款视频反推流程：MiMo 解析 → LLM 反推 → 输出 compiled_prompt

    异步执行，立即返回 task_id，前端轮询 /status 获取结果
    """
    if not Path(req.video_path).exists():
        raise HTTPException(400, f"视频文件不存在: {req.video_path}")

    task_id = f"replica_{int(time.time()*1000)}_{os.urandom(3).hex()}"
    out_dir = OUTPUT_DIR / "one_shot_replica" / task_id
    out_dir.mkdir(parents=True, exist_ok=True)

    oneshot_replica_tasks[task_id] = {
        "task_id": task_id,
        "phase": "reverse",              # reverse → generating → done/failed
        "status": "pending",
        "video_path": req.video_path,
        "duration": req.duration,
        "aspect_ratio": req.aspect_ratio,
        "resolution": req.resolution,
        "version": req.version,
        "engine": req.engine,
        "extra_hint": req.extra_hint,
        "storyboard": None,
        "metadata": None,
        "compiled_prompt": None,
        "video_url": None,
        "video_path_out": None,
        "rh_task_id": None,
        "cost": None,
        "duration_actual": None,
        "engine_used": None,
        "video_duration": None,
        "error": None,
        "created_at": datetime.now().isoformat(),
        "completed_at": None,
        "out_dir": str(out_dir),
    }

    import threading

    def _bg_reverse(tid: str, req_data: dict, out_dir_str: str):
        t = oneshot_replica_tasks[tid]
        try:
            from backend.services.oneshot_replica import reverse_engineer
            t["status"] = "running"
            result = asyncio.run(reverse_engineer(
                video_path=req_data["video_path"],
                output_dir=out_dir_str,
                duration=req_data["duration"],
                aspect_ratio=req_data["aspect_ratio"],
                resolution=req_data["resolution"],
                version=req_data["version"],
                engine=req_data["engine"],
                product_image_paths=req_data.get("product_image_paths") or [],
                extra_hint=req_data.get("extra_hint", ""),
            ))
            t["storyboard"] = result["storyboard"]
            t["metadata"] = result["metadata"]
            t["compiled_prompt"] = result["compiled_prompt"]
            t["engine_used"] = result["engine"]
            t["video_duration"] = result.get("video_duration")
            t["status"] = "completed"
            t["phase"] = "reverse_done"
            t["completed_at"] = datetime.now().isoformat()
        except Exception as e:
            t["status"] = "failed"
            t["error"] = f"反推失败: {e}"
            t["completed_at"] = datetime.now().isoformat()

    th = threading.Thread(target=_bg_reverse, args=(task_id, req.dict(), str(out_dir)), daemon=True)
    th.start()
    return {"task_id": task_id, "phase": "reverse", "status": "pending"}


class OneShotReplicaGenerateRequest(BaseModel):
    """基于反推结果调 Seedance 生成视频"""
    task_id: str                         # 反推任务的 task_id
    compiled_prompt: Optional[str] = None  # 用户可编辑后的 prompt（不传则用反推原值）
    duration: Optional[str] = None        # 可覆盖反推时的时长
    aspect_ratio: Optional[str] = None
    resolution: Optional[str] = None
    version: Optional[str] = None


@app.post("/api/one-shot-replica/generate")
async def oneshot_replica_generate(req: OneShotReplicaGenerateRequest):
    """基于反推结果调 Seedance 生成视频（异步，复用同 task_id）"""
    # 🔴 花钱批准闸：这条腿直连 Seedance（不经 gateway.invoke）⇒ 入口先要条子。
    #    purpose 给 "*"：具体打 seedance-2.0 还是 2.5 我没在 seedance_runner 里核实死，
    #    不猜路由 ⇒ 只认人签的通吃条子，报价按 video.generate 的上限（宁可高报不猜低）。
    _spend_or_423("video.generate", "*", "one-shot-replica 出片")
    t = oneshot_replica_tasks.get(req.task_id)
    if not t:
        raise HTTPException(404, "反推任务不存在")
    if t["status"] != "completed" or t["phase"] != "reverse_done":
        raise HTTPException(400, f"反推未完成，当前状态: {t['phase']}/{t['status']}")

    compiled_prompt = req.compiled_prompt or t["compiled_prompt"]
    if not compiled_prompt:
        raise HTTPException(400, "compiled_prompt 为空")

    version = req.version or t["version"]
    duration = req.duration or t["duration"]
    aspect_ratio = req.aspect_ratio or t["aspect_ratio"]
    resolution = req.resolution or t["resolution"]
    image_paths = [t["video_path"]] if False else []  # 视频不是产品图，留空

    # 调 Seedance
    from backend.services.seedance_runner import submit_seedance_task, start_background_poll
    try:
        submit = submit_seedance_task(
            compiled_prompt=compiled_prompt,
            version=version,
            aspect_ratio=aspect_ratio,
            duration=duration,
            resolution=resolution,
            image_path=None,
        )
    except Exception as e:
        raise HTTPException(500, f"Seedance 提交失败: {e}")

    t["rh_task_id"] = submit["rh_task_id"]
    t["phase"] = "generating"
    t["status"] = "pending"

    start_background_poll(
        task_id=req.task_id,
        rh_task_id=submit["rh_task_id"],
        api_key=submit["api_key"],
        out_dir=t["out_dir"],
        tasks_store=oneshot_replica_tasks,
    )
    return {
        "task_id": req.task_id,
        "rh_task_id": submit["rh_task_id"],
        "endpoint": submit["endpoint"],
        "phase": "generating",
    }


@app.get("/api/one-shot-replica/status/{task_id}")
async def oneshot_replica_status(task_id: str):
    """查询任务状态（反推 + 生成）"""
    t = oneshot_replica_tasks.get(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    return {
        "task_id": t["task_id"],
        "phase": t["phase"],
        "status": t["status"],
        "engine_used": t["engine_used"],
        "video_duration": t["video_duration"],
        "storyboard": t["storyboard"],
        "compiled_prompt": t["compiled_prompt"],
        "metadata": t["metadata"],
        "rh_task_id": t["rh_task_id"],
        "video_url": t["video_url"],
        "video_path_out": t["video_path_out"],
        "cost": t["cost"],
        "duration_actual": t["duration_actual"],
        "error": t["error"],
        "created_at": t["created_at"],
        "completed_at": t["completed_at"],
    }


@app.get("/api/one-shot-replica/tasks")
async def oneshot_replica_list_tasks():
    """列出所有 Replica 任务"""
    items = []
    for t in sorted(oneshot_replica_tasks.values(), key=lambda x: x.get("created_at", ""), reverse=True):
        items.append({
            "task_id": t["task_id"],
            "phase": t["phase"],
            "status": t["status"],
            "engine_used": t["engine_used"],
            "video_duration": t["video_duration"],
            "video_url": t["video_url"],
            "cost": t["cost"],
            "created_at": t["created_at"],
        })
    return {"tasks": items, "total": len(items)}


@app.get("/api/one-shot-replica/storyboard/{task_id}")
async def oneshot_replica_storyboard(task_id: str):
    """下载 storyboard JSON"""
    t = oneshot_replica_tasks.get(task_id)
    if not t or not t["storyboard"]:
        raise HTTPException(404, "storyboard 不存在")
    return {"storyboard": t["storyboard"], "metadata": t["metadata"]}


# ─────────────────────────────────────
# 提示词反推 + wan3.0 爆款复刻 API
# ─────────────────────────────────────

class PromptReverseRequest(BaseModel):
    """提示词反推请求"""
    video_path: str
    extra_hint: str = ""
    output_subdir: Optional[str] = None  # 输出子目录名（不传则用 uuid）
    engine: str = "mimo"  # mimo / grok / both
    trim_start: Optional[float] = None
    trim_end: Optional[float] = None


@app.post("/api/prompt-reverse/submit")
async def prompt_reverse_submit(req: PromptReverseRequest):
    """提交爆款视频，AI 反推 H3 视频生成 prompt（异步执行）"""
    from backend.services.prompt_reverser import reverse_engineer_prompt

    if not Path(req.video_path).exists():
        raise HTTPException(400, f"视频不存在: {req.video_path}")

    # 输出子目录
    sub = req.output_subdir or f"prompt_reverse_{uuid.uuid4().hex[:8]}"
    out_dir = OUTPUT_DIR / sub
    out_dir.mkdir(parents=True, exist_ok=True)

    video_path = req.video_path
    if req.trim_start is not None and req.trim_end is not None:
        if float(req.trim_end) <= float(req.trim_start):
            raise HTTPException(400, f"裁剪区间非法：{req.trim_start} → {req.trim_end}")
        from backend.services.super_clone_wan3 import trim_video
        trimmed = out_dir / "trimmed.mp4"
        await asyncio.to_thread(
            trim_video, req.video_path, float(req.trim_start), float(req.trim_end), str(trimmed),
        )
        video_path = str(trimmed)

    video_size_mb = Path(video_path).stat().st_size / 1024 / 1024
    if video_size_mb > 15:
        raise HTTPException(400, f"视频过大（{video_size_mb:.1f}MB），MiMo-V2.5 限制 15MB 以内，请压缩或截取片段后重试")

    task_id = f"pr_{uuid.uuid4().hex[:12]}"
    tasks[task_id] = {
        "task_id": task_id, "type": "prompt_reverse", "status": "INIT",
        "video_path": video_path, "output_dir": str(out_dir),
        "extra_hint": req.extra_hint, "created_at": datetime.now().isoformat(),
        "result": None, "error": None,
        "trim_start": req.trim_start, "trim_end": req.trim_end,
    }

    async def _runner():
        try:
            tasks[task_id]["status"] = "RUNNING"
            tasks[task_id]["started_at"] = datetime.now().isoformat()
            result = await reverse_engineer_prompt(
                video_path=video_path,
                output_dir=str(out_dir),
                extra_hint=req.extra_hint,
                engine=req.engine,
            )
            tasks[task_id]["result"] = result
            tasks[task_id]["status"] = "SUCCESS"
            tasks[task_id]["finished_at"] = datetime.now().isoformat()
        except Exception as e:
            tasks[task_id]["status"] = "FAILED"
            tasks[task_id]["error"] = str(e)
            tasks[task_id]["finished_at"] = datetime.now().isoformat()
            print(f"[!] prompt_reverse {task_id} 失败: {e}")

    asyncio.create_task(_runner())
    return {"success": True, "task_id": task_id, "output_dir": str(out_dir)}


@app.get("/api/prompt-reverse/status/{task_id}")
async def prompt_reverse_status(task_id: str):
    """查询反推任务状态"""
    t = tasks.get(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    return {
        "task_id": task_id, "type": t["type"],
        "status": t["status"], "error": t.get("error"),
        "created_at": t.get("created_at"),
        "started_at": t.get("started_at"),
        "finished_at": t.get("finished_at"),
        "result": t.get("result"),
    }


class FusionPromptRequest(BaseModel):
    """MiMo 原始提示词与产品图深度融合请求"""
    mimo_prompt: str
    reference_images: list[str] = []
    product_name: str = ""
    extra_hint: str = ""
    target_duration_sec: Optional[int] = None  # 目标视频时长（秒），None=与原视频一致
    source_duration: Optional[float] = None  # 原视频时长（秒），用于告诉 LLM 原视频多长


@app.post("/api/prompt-reverse/fuse-product")
async def prompt_reverse_fuse_product(req: FusionPromptRequest):
    """先生成当前产品专用融合 Prompt，审核后再提交视频生成。"""
    if not req.mimo_prompt.strip():
        raise HTTPException(400, "MiMo 原始提示词不能为空")
    if not req.reference_images:
        raise HTTPException(400, "请至少上传一张商品图")

    import base64
    import mimetypes
    import httpx as _httpx
    from backend.services.prompt_reverser import _gpt_generalize_chat

    system_prompt = f"""你是一名专业的电商短视频裂变编排导演。

你的任务是将 MiMo 原始视频提示词与当前商品参考图、商品资料及客户要求深度融合，生成一份仅适用于当前商品的高质量 H3 视频 Prompt。

你不仅要理解原视频拍了什么，还要理解每个镜头的营销作用，并在保留原爆款有效结构的基础上，完成商品适配、动作调整、卖点重写和时长编排。

质量永远是第一原则。不得为了机械保留原视频、满足固定分镜数量或凑满时长，而牺牲商品真实性、动作合理性、叙事连贯性或视频可生成性。

【一、核心原则】

1. 商品真实优先
当前商品参考图是可见外观与结构的依据；商品资料可补充已确认的功能和卖点。不得继承原视频中目标商品不存在的结构、部件或功能，也不得编造无法确认的商品事实。

2. 营销作用优先于机械复刻
尽量保留原视频的叙事结构、人物表演、镜头语言、节奏和营销作用。具体动作、手部轨迹、展示方式或口播与目标商品不兼容时，应进行最小必要调整，而不是强行复刻。

3. 质量优先于分镜数量
Shot 数量、镜头时长和信息密度应根据目标时长、动作复杂度、商品展示需求及叙事节奏动态决定。不得使用固定数量或机械公式。

4. 事实优先于口播原文
与商品无关且适用的口播尽量保留。涉及商品结构、功能、数量、材质或卖点的内容必须与目标商品一致；发生冲突时，应保留原句的营销作用、语气和节奏，改写为真实且相关的表达。

5. 可生成性优先于指令堆砌
选择清晰、合理、可执行的镜头表达。对于复杂交互，应优先通过动作适配、视角调整或合理拆分降低结构风险，不得依靠大量重复的负向指令掩盖不合理的拍摄设计。

【二、内部编排流程】

在输出最终 Prompt 前，完成以下内部工作，不展示分析过程。

A. 理解原视频

识别原视频的 Shot、时间线、人物、场景、动作、手部交互、景别、运镜、转场、口播及整体节奏。

判断各镜头的营销作用，包括钩子、痛点、商品出现、卖点证明、使用过程、使用结果和转化引导等。

区分必须继承的核心结构与可以调整的具体表现。

B. 建立商品身份

从当前参考图识别商品的关键外观、结构、部件数量、层级关系、材质、颜色、比例及具有辨识度的细节。

结合商品资料确认可使用的功能和卖点。

对无法确认的功能或隐藏结构保持谨慎，不将推测写成确定事实。

确保商品在所有镜头中保持同一身份，不发生结构、数量、材质或关键外观的无故变化。

C. 识别冲突并融合

逐镜头比较原商品与目标商品的结构、功能和交互方式。

兼容的内容尽量继承；不兼容的内容进行最小必要调整。

调整时优先保留原镜头的营销作用、人物表演、景别、运镜和节奏。

如果原动作无法合理适配目标商品，可以重新设计等效的展示动作，但不得虚构商品能力。

同步检查并修正与商品事实冲突的口播、音效和展示内容，避免画面与台词互相矛盾。

【关键规则：产品出现时机必须严格遵守 MiMo storyboard】

MiMo storyboard 的每个 Shot 包含 "商品交互" 字段，其 "交互方式" 和 "营销功能" 字段决定了该 Shot 是否有产品出现。

必须严格遵守：
- 当某 Shot 的 "商品交互.交互方式" 为 "无"，或 "商品交互.营销功能" 为 "无"，或 "画面动作.产品动作" 为 "无" 时，该 Shot 禁止出现目标产品、禁止引用产品图编号、禁止将原商品替换为目标产品。该 Shot 应保持原视频画面内容，即使 storyboard 的 "人物与产品.产品" 字段填了产品名，也不代表该 Shot 中产品可见。
- 当某 Shot 的 "商品交互.交互方式" 不为 "无" 时，才可以在该 Shot 中生成目标产品，并引用对应的产品图编号约束外观。
- 产品首次出现的 Shot 必须与原视频中产品首次实际可见的 Shot 一致，不得提前注入产品。
- 不得为了让产品多曝光，而在原视频无产品的 Shot 中强行添加产品画面或产品引用。

错误示例（禁止）：
原视频 Shot 1 是"男子在沙发上乱画，产品动作=无，商品交互.交互方式=无"，编译器却输出"沙发上铺着绿色沙发毯 图1，男子在沙发毯上乱画"——这是严重错误，产品被提前注入，且动作对象被篡改。

正确示例：
Shot 1 产品动作=无 → 编译器输出应保持"男子在沙发上乱画"，不提沙发毯，不引用产品图。

D. 自适应时间线编排

原视频时长：{f"{req.source_duration:.1f} 秒" if req.source_duration else "未提供"}

目标视频时长：
{req.target_duration_sec or ('与原视频一致（即 ' + f"{req.source_duration:.1f} 秒）" if req.source_duration else "与原视频一致")}

⚠️ 时间轴约束（必须严格遵守）：
- 所有分镜的时间戳必须从 0.00s 开始，最终 Shot 的结束时间必须等于目标时长
- 分镜数量和各 Shot 时长由你根据场景内容自主分配，但总时长必须精确等于目标时长
- ASR 口播段时间戳仅用于对齐口播内容，不代表视频总时长；口播结束后到目标时长之间的时间也要分配分镜

未指定目标时长时，尽量保留原视频的 Shot 数量、顺序、时长和节奏。

指定目标时长时，以目标时长为约束，重新规划分镜数量、镜头时长、动作、口播和叙事节奏。

允许根据需要压缩、合并、拆分、删除或增加镜头，但必须保留最有价值的营销信息，并保证商品展示清楚、动作能够完成、口播自然。

缩短时长时，优先保留核心钩子、商品证明和转化信息，删除冗余内容，不机械截取原视频。

延长时长时，可以增加有价值的展示、动作细节或使用场景，不得通过重复画面或编造卖点填充时长。

分镜数量由内容和质量决定，不预设固定数量。复杂商品镜头应获得足够的展示时间，简单镜头可以采用更快节奏。

最终时间线应连续、合理，总时长符合客户要求。

E. 最终质量检查

检查商品身份是否一致，动作是否合理，口播是否真实，镜头之间是否连贯，时间线是否成立，营销结构是否完整。

发现冲突时，在内部修正后再输出。

【三、H3 Prompt 输出要求】

输出一份完整、自然、可直接用于 H3 视频生成的中文 Prompt。

语言要求（必须遵守）：
- 画面、人物、商品、动作、场景、风格、时间线、音效描述全部使用中文，不得把整段 Prompt 写成英文
- H3 技术标记保持原格式：[Shot N]、<Picture N>、<Subject N>、<t>...</t>、<d>[Language] ...</d>
- 运镜类型可用英文术语（Pan / Tilt / Track / Zoom / Static / Dolly），但整句必须是中文
- 口播原文保持原视频语言，不要翻译；中文口播用 <d>[Chinese] 原文</d>，英文口播用 <d>[English] 原文</d>
- 字段名（如 integrated_multimodal_description）可保持英文，字段内容必须是中文

根据实际内容组织 Prompt，包含必要的：

- 全局商品身份与一致性约束
- 视频整体风格
- 按时间顺序排列的 Shot
- 每个 Shot 的画面、人物动作、商品交互、景别、运镜和口播
- 与目标时长匹配的时间线
- 必要的环境音、商品操作音和音乐要求

保留 MiMo 原始提示词中适用的口播语言及标记格式。

全局约束集中描述，镜头内只补充与该镜头相关的关键限制，避免机械重复。

不要加入与当前商品无关的固定示例、无意义的负向约束或超出输入信息的商品事实。

不要为了显得完整而编造镜头、动作、音效或功能。

不输出分析过程、冲突清单、JSON、Markdown、解释或额外建议。

只输出最终 H3 Prompt。"""

    content = [{
        "type": "text",
        "text": f"""产品名称：{req.product_name or '以参考图为准'}
目标视频时长：{req.target_duration_sec or '与原视频一致'}
客户补充意见（最高优先级，覆盖其他输入）：{req.extra_hint or '无'}

MiMo 原始视频提示词：
{req.mimo_prompt}"""
    }]
    print(f"[fuse-product] 收到 {len(req.reference_images)} 张商品图，开始编码 base64...", flush=True)
    for path in req.reference_images:
        file_path = Path(path)
        if not file_path.exists():
            raise HTTPException(400, f"商品图不存在: {path}")
        mime = mimetypes.guess_type(file_path.name)[0] or "image/jpeg"
        encoded = base64.b64encode(file_path.read_bytes()).decode("ascii")
        content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}})
        print(f"[fuse-product]   - {file_path.name} ({len(encoded)} chars base64)", flush=True)
    print(f"[fuse-product] content 总段数: {len(content)}（1 文本 + {len(req.reference_images)} 图片），调用 {settings.SR_GROK_MODEL}...", flush=True)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": content},
    ]
    # 524 是 Cloudflare 网关超时（默认 100s），多图大 payload 容易触发
    # 策略：Grok-4.6 重试 2 次（每次 180s），全部失败后切 GPT-5.6-sol 备用
    payload_grok = {
        "model": settings.TEXT_MODEL_MIMO_PRO,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": 8000,
    }
    payload_gpt = {
        "model": settings.OPENAI_MODEL,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": 8000,
    }
    headers_grok = {
        "Authorization": f"Bearer {settings.MIMO_API_KEY}",
        "Content-Type": "application/json",
    }
    headers_gpt = {
        "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    last_error = None
    for attempt in range(1, 3):
        try:
            print(f"[fuse-product] Grok 第 {attempt}/2 次尝试（timeout=180s）...", flush=True)
            async with _httpx.AsyncClient(timeout=180.0) as client:
                response = await client.post(
                    f"{settings.MIMO_BASE_URL.rstrip('/')}/chat/completions",
                    headers=headers_grok,
                    json=payload_grok,
                )
                response.raise_for_status()
                resp = response.json()
            prompt = resp.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            if prompt:
                print(f"[fuse-product] Grok 成功，prompt 长度 {len(prompt)}", flush=True)
                return {"success": True, "fusion_prompt": prompt}
            last_error = RuntimeError("Grok 返回空内容")
            print(f"[fuse-product] Grok 第 {attempt}/2 次返回空内容", flush=True)
        except Exception as e:
            last_error = e
            err_str = str(e)
            is_524 = "524" in err_str
            print(f"[fuse-product] Grok 第 {attempt}/2 次失败：{err_str[:200]}", flush=True)
            if is_524 and attempt < 2:
                print(f"[fuse-product] 524 网关超时，5s 后重试...", flush=True)
                import asyncio
                await asyncio.sleep(5)

    # Grok 全失败，切到 GPT-5.6-sol
    try:
        print(f"[fuse-product] 切到 GPT-5.6-sol 备用（timeout=180s）...", flush=True)
        async with _httpx.AsyncClient(timeout=180.0) as client:
            response = await client.post(
                f"{settings.OPENAI_BASE_URL.rstrip('/')}/chat/completions",
                headers=headers_gpt,
                json=payload_gpt,
            )
            response.raise_for_status()
            resp = response.json()
        prompt = resp.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        if prompt:
            print(f"[fuse-product] GPT 备用成功，prompt 长度 {len(prompt)}", flush=True)
            return {"success": True, "fusion_prompt": prompt, "engine": "gpt-fallback"}
        raise HTTPException(502, "融合 Prompt 为空（Grok 2 次失败，GPT 也返回空）")
    except HTTPException:
        raise
    except Exception as e:
        err_msg = str(last_error)
        is_524 = "524" in err_msg
        hint = ""
        if is_524:
            hint = "（hub.nocannobb.com 网关 524 超时，多图大 payload 容易触发，可单图重试或稍后再试）"
        raise HTTPException(500, f"融合 Prompt 生成失败：{last_error}{hint}；GPT 备用也失败：{e}")


class PromptToGenericRequest(BaseModel):
    """H3 提示词转通用版请求

    把含具体产品/品牌/人物描述的 H3 prompt 改写成可复用模板，
    保留镜头/动作/场景结构，将具体描述替换为 {{PRODUCT}} / {{SCENE}} / {{MODEL}} 占位符。
    下次喂新素材直接替换占位即可换产品/换背景/换人物。
    """
    prompt: str


@app.post("/api/prompt-reverse/to-generic")
async def prompt_reverse_to_generic(req: PromptToGenericRequest):
    """把含产品识别的 H3 prompt 转成通用版模板（LLM 推理）"""
    if not req.prompt.strip():
        raise HTTPException(400, "prompt 不能为空")
    from backend.services.gpt_client import gpt_client

    system_prompt = """你是 H3 视频生成提示词通用化改写器。

任务：把一段含具体产品/品牌/人物描述的 H3 prompt 改写成可复用模板。

规则：
1. 保留所有镜头结构、动作描述、镜头语言、节奏控制、画面构图、镜头运动、时长标记。
2. 仅通用化画面中的产品描述：具体品牌、型号、价格、颜色、款式、材质和装饰细节改写为“产品”或“reference_images 中的产品”，不要反复添加占位符。
3. 删除画面中的具体品牌名、商标、型号、价格和规格，但保留产品展示动作。
4. 所有 `<d>...</d>` 口播内容必须逐字保留，包括语言标签、说话人、原文、时间和顺序；不得翻译、总结、补写或删除口播。
5. 只清理画面描述中的 `[PRODUCT]`、`【产品】`、`<Subject 1>` 等中间占位符，不得修改 `<d>...</d>` 内的内容。
6. 将明显错误的分钟/秒钟表达统一为秒数格式，例如“1分80秒”改为“1.80s”，但不改变时间点含义。
7. 不要改写镜头数量、镜头顺序、画面比例等技术参数。
8. 直接输出改写后的 prompt，不加说明，不加 Markdown 代码块。

示例输入（片段）：
Shot 1: 特写镜头，一台银色戴森 V15 无线吸尘器放在大理石厨房台面上，背景是现代北欧风格厨房

示例输出（片段）：
Shot 1: 特写镜头，一台 {{PRODUCT}} 放在 {{SCENE}} 上，背景是 {{SCENE}}"""

    user_msg = f"请把下面的 H3 视频生成 prompt 改写成通用版模板：\n\n{req.prompt}"

    try:
        resp = await gpt_client.chat_completion(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.2,
            max_tokens=4000,
        )
        # OpenAI 兼容响应
        generic = (
            resp.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
        )
        # 去掉可能的 markdown 代码块包裹
        if generic.startswith("```"):
            generic = generic.split("```", 2)[1] if "```" in generic[3:] else generic
            generic = generic.strip("`").strip()
        if not generic:
            raise HTTPException(500, "LLM 返回空内容")
        return {"success": True, "generic_prompt": generic}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"通用化失败：{e}")


class PrepareLinkRequest(BaseModel):
    """从抖音/小红书分享文案预下载视频，供裁剪弹窗使用"""
    text: str


@app.post("/api/prompt-reverse/prepare-link")
async def prompt_reverse_prepare_link(req: PrepareLinkRequest):
    """下载分享链接视频，返回本地路径、预览 URL 和时长，不启动反推。"""
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(400, "链接为空")

    from backend.services.link_fetch import link_fetch_local

    out_dir = UPLOAD_DIR / "prompt_reverse_links" / uuid.uuid4().hex[:12]
    out_dir.mkdir(parents=True, exist_ok=True)
    logs: list[str] = []

    try:
        # 下载这条腿只有 `link_fetch` 一份：两次重试、断头片（ffprobe 量不出时长就重下）、
        # 抖音/小红书分发、浏览器策略全在里面 —— 这里不再自己直接调下载器（少一次抄一份的机会）。
        got = await link_fetch_local(text, cb=logs, out_dir=str(out_dir))
    except RuntimeError as e:
        msg = str(e)
        # 认不出链接 = 输入问题(400)；下载器拿不到片 = 上游问题(502)。两种话不许混成一种。
        raise HTTPException(400 if ("未识别" in msg or "链接为空" in msg) else 502,
                            f"视频下载失败：{msg}")
    except Exception as e:
        raise HTTPException(502, f"视频下载失败：{e}")

    return {
        "success": True,
        "file_path": got["file_path"],
        "preview_url": got["preview_url"],
        "duration": got["duration"],
        "title": got["title"],
        "logs": logs,
        "size_mb": got["size_mb"],
        "seconds": got["seconds"],
        "platform": got["platform"],
    }


class PublishRpaRequest(BaseModel):
    """通用提示词 → weeksir 网站 RPA 发布请求"""
    video_path: str           # 本地视频绝对路径
    prompt_text: str = ""     # 通用提示词文本（与 prompt_file 二选一）
    prompt_file: str = ""     # 已有 txt 路径
    category: str             # weeksir 分类（不存在自动创建）
    title: str = "服装带货"   # 示例标题
    submit: bool = False      # True=真正点保存，False=dry-run
    headless: bool = False    # 无头模式


@app.post("/api/prompt-reverse/publish-rpa")
async def prompt_reverse_publish_rpa(req: PublishRpaRequest):
    """触发 weeksir_example_rpa.py 把「截取视频+通用提示词」发布到 weeksir.com/admin/video-examples（异步）"""
    if not Path(req.video_path).exists():
        raise HTTPException(400, f"视频不存在: {req.video_path}")
    if not req.prompt_text.strip() and not (req.prompt_file and Path(req.prompt_file).exists()):
        raise HTTPException(400, "通用提示词为空")

    rpa_work = OUTPUT_DIR / "weeksir_session"
    rpa_work.mkdir(parents=True, exist_ok=True)

    # 选定 prompt txt 文件
    if req.prompt_file and Path(req.prompt_file).exists():
        prompt_file = Path(req.prompt_file)
    else:
        prompt_file = rpa_work / f"publish_prompt_{uuid.uuid4().hex[:8]}.txt"
        prompt_file.write_text(req.prompt_text.strip(), encoding="utf-8")

    task_id = f"rpa_{uuid.uuid4().hex[:12]}"
    log_path = rpa_work / f"publish_{task_id}.log"
    log_path.touch()

    cmd = [
        sys.executable, "-X", "utf8", "weeksir_example_rpa.py",
        "--video", str(req.video_path),
        "--prompt-file", str(prompt_file),
        "--category", req.category,
        "--title", req.title,
    ]
    if req.submit:
        cmd.append("--submit")
    if req.headless:
        cmd.append("--headless")

    tasks[task_id] = {
        "task_id": task_id, "type": "publish_rpa", "status": "INIT",
        "video_path": str(req.video_path),
        "prompt_file": str(prompt_file),
        "category": req.category, "title": req.title,
        "submit": req.submit, "headless": req.headless,
        "log_path": str(log_path),
        "stdout": [], "return_code": None,
        "created_at": datetime.now().isoformat(),
        "started_at": None, "finished_at": None, "error": None,
    }

    async def _runner():
        t = tasks[task_id]
        t["status"] = "RUNNING"
        t["started_at"] = datetime.now().isoformat()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(Path(__file__).parent),
            )
            t["pid"] = proc.pid
            lines: list[str] = []
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                lines.append(text)
                if len(lines) > 200:
                    lines.pop(0)
                t["stdout"] = lines
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(text + "\n")
            rc = await proc.wait()
            t["return_code"] = rc
            t["status"] = "SUCCESS" if rc == 0 else "FAILED"
            t["error"] = None if rc == 0 else f"RPA 退出码 {rc}"
        except Exception as e:
            t["status"] = "FAILED"
            t["error"] = str(e)
        finally:
            t["finished_at"] = datetime.now().isoformat()

    asyncio.create_task(_runner())
    return {
        "success": True, "task_id": task_id,
        "log_path": str(log_path),
        "prompt_file": str(prompt_file),
        "category": req.category, "title": req.title,
        "submit": req.submit,
    }


@app.get("/api/prompt-reverse/publish-rpa/status/{task_id}")
async def prompt_reverse_publish_rpa_status(task_id: str):
    """查询 RPA 发布任务状态"""
    t = tasks.get(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    return {
        "task_id": task_id, "type": t.get("type"),
        "status": t["status"],
        "submit": t.get("submit"),
        "category": t.get("category"), "title": t.get("title"),
        "stdout": (t.get("stdout") or [])[-50:],
        "return_code": t.get("return_code"),
        "error": t.get("error"),
        "log_path": t.get("log_path"),
        "created_at": t.get("created_at"),
        "started_at": t.get("started_at"),
        "finished_at": t.get("finished_at"),
    }


class BatchPromptRequest(BaseModel):
    """批量提示词反推请求"""
    links: str = ""  # 多行文本，一行一个抖音链接
    video_paths: list[str] = []  # 可选：已上传视频路径，按行对应
    trim_ranges: Optional[list] = None  # 与 video_paths 对齐，[{start, end} | null]


# 批量反推任务存储
batch_prompt_tasks: dict = {}


def _save_prompt_reverse_history(task_id: str, links: list, video_paths: list, status: str = "running"):
    from db import save_prompt_reverse_history
    task = batch_prompt_tasks.get(task_id, {})
    save_prompt_reverse_history(task_id, links, video_paths, task.get("items", []), status)


@app.post("/api/batch-prompt-reverse/start")
async def batch_prompt_reverse_start(req: BatchPromptRequest):
    """启动批量提示词反推任务（串行）"""
    links = [l.strip() for l in req.links.strip().split("\n") if l.strip()]
    video_paths = [p.strip() for p in req.video_paths if p and p.strip()]
    trim_ranges = list(req.trim_ranges or [])
    total = max(len(links), len(video_paths))
    if not total:
        raise HTTPException(400, "请至少输入一个抖音链接或视频文件")
    links.extend([""] * (total - len(links)))
    video_paths.extend([""] * (total - len(video_paths)))
    trim_ranges.extend([None] * (total - len(trim_ranges)))

    task_id = f"bp_{uuid.uuid4().hex[:10]}"
    batch_prompt_tasks[task_id] = {
        "task_id": task_id,
        "status": "running",
        "total": total,
        "items": [],
        "started_at": time.time(),
        "links": links,
        "video_paths": video_paths,
        "trim_ranges": trim_ranges,
    }

    async def _on_item_update(item: dict):
        # 同步到 tasks 存储
        items = batch_prompt_tasks[task_id]["items"]
        if item["index"] >= len(items):
            items.append(dict(item))
        else:
            items[item["index"]] = dict(item)
        _save_prompt_reverse_history(task_id, links, video_paths)

    async def _run():
        from backend.services.batch_prompt_pipeline import run_batch
        try:
            result = await run_batch(
                links, video_paths, _on_item_update, batch_id=task_id, trim_ranges=trim_ranges,
            )
            batch_prompt_tasks[task_id]["status"] = "done"
            batch_prompt_tasks[task_id]["items"] = result["items"]
            _save_prompt_reverse_history(task_id, links, video_paths, "done")
        except Exception as e:
            batch_prompt_tasks[task_id]["status"] = "failed"
            batch_prompt_tasks[task_id]["error"] = str(e)
            _save_prompt_reverse_history(task_id, links, video_paths, "failed")

    asyncio.create_task(_run())
    return {"success": True, "task_id": task_id, "total": len(links)}


@app.get("/api/batch-prompt-reverse/status/{task_id}")
async def batch_prompt_reverse_status(task_id: str):
    """查询批量反推任务状态，服务重启后从历史记录恢复。"""
    task = batch_prompt_tasks.get(task_id)
    if task:
        return task

    from db import get_prompt_reverse_history
    history = get_prompt_reverse_history(task_id)
    if history:
        items = history.get("items", [])
        was_running = history.get("status") == "running"
        return {
            "task_id": task_id,
            "status": "failed" if was_running else history.get("status", "failed"),
            "total": len(items),
            "items": items,
            "links": history.get("links", []),
            "video_paths": history.get("video_paths", []),
            "error": "服务重启导致任务中断" if was_running else "",
        }

    raise HTTPException(404, "任务不存在")


@app.get("/api/batch-prompt-reverse/list")
async def batch_prompt_reverse_list():
    """列出历史批量任务及已保存提示词"""
    from backend.services.batch_prompt_pipeline import list_saved_prompts
    from db import list_prompt_reverse_history, migrate_prompt_reverse_history
    migrate_prompt_reverse_history(str(OUTPUT_DIR / "batch_prompts"))
    return {"success": True, "prompts": list_saved_prompts(), "history": list_prompt_reverse_history()}


@app.get("/api/batch-prompt-reverse/history/{batch_id}")
async def batch_prompt_reverse_history(batch_id: str):
    from db import get_prompt_reverse_history
    history = get_prompt_reverse_history(batch_id)
    if not history:
        raise HTTPException(404, "历史任务不存在")
    return {"success": True, "history": history}


class PromptReverseHistoryItemRequest(BaseModel):
    index: int
    updates: dict


@app.post("/api/batch-prompt-reverse/history/{batch_id}/item")
async def batch_prompt_reverse_history_item(batch_id: str, req: PromptReverseHistoryItemRequest):
    from db import update_prompt_reverse_item
    if not update_prompt_reverse_item(batch_id, req.index, req.updates):
        raise HTTPException(404, "历史条目不存在")
    return {"success": True}


@app.get("/api/batch-prompt-reverse/read/{prompt_file:path}")
async def batch_prompt_reverse_read(prompt_file: str):
    """读取提示词文件内容"""
    from backend.services.batch_prompt_pipeline import read_prompt_file, PROMPTS_DIR
    # 安全检查：确保路径在 PROMPTS_DIR 内
    target = (PROMPTS_DIR / prompt_file).resolve() if not Path(prompt_file).is_absolute() else Path(prompt_file).resolve()
    try:
        target.relative_to(PROMPTS_DIR.resolve())
    except ValueError:
        raise HTTPException(400, "非法路径")
    content = read_prompt_file(str(target))
    return {"success": True, "content": content}


@app.post("/api/batch-prompt-reverse/retry/{task_id}/{item_index}")
async def batch_prompt_reverse_retry(task_id: str, item_index: int):
    """重试批量任务中失败的条目（断点续跑，参考 replica retry 范式）。

    策略：
    - item 有 reverse_result.h3_prompt → 跳过 download+reverse，从 generalize 续跑
    - item 有 source_video_path 但无 reverse_result → 从 reverse 续跑
    - 都没有 → 从头跑
    """
    # 1. 加载任务（内存优先，磁盘回退）
    task = batch_prompt_tasks.get(task_id)
    if not task:
        from db import get_prompt_reverse_history
        history = get_prompt_reverse_history(task_id)
        if not history:
            raise HTTPException(404, "任务不存在")
        # 重建内存状态
        items = history.get("items", [])
        task = {
            "task_id": task_id,
            "status": history.get("status", "failed"),
            "total": len(items),
            "items": items,
            "links": history.get("links", []),
            "video_paths": history.get("video_paths", []),
        }
        batch_prompt_tasks[task_id] = task

    items = task.get("items", [])
    if item_index < 0 or item_index >= len(items):
        raise HTTPException(400, f"item_index 越界：{item_index}（共 {len(items)} 条）")

    item = items[item_index]
    if item["status"] == "running" or item["status"] in ("downloading", "reversing", "generalizing", "saving"):
        raise HTTPException(400, f"该条目正在处理中（{item['status']}），无法重试")

    links = task.get("links", [])
    video_paths = task.get("video_paths", [])
    trim_ranges = task.get("trim_ranges") or []

    # 2. 重置任务状态 + 启动重试协程
    task["status"] = "running"
    task["error"] = None
    item["retrying"] = True

    async def _on_item_update(updated: dict):
        # 同步到 tasks 存储
        items[item_index] = dict(updated)
        _save_prompt_reverse_history(task_id, links, video_paths, "running")

    async def _runner():
        from backend.services.batch_prompt_pipeline import retry_item
        try:
            await retry_item(
                batch_id=task_id,
                item_index=item_index,
                item=item,
                links=links,
                video_paths=video_paths,
                on_item_update=_on_item_update,
                trim_ranges=trim_ranges,
            )
            # 检查是否所有 item 都完成
            all_done = all(it.get("status") in ("done", "failed") for it in items)
            if all_done:
                has_failed = any(it.get("status") == "failed" for it in items)
                task["status"] = "failed" if has_failed else "done"
                _save_prompt_reverse_history(task_id, links, video_paths, task["status"])
        except Exception as e:
            task["status"] = "failed"
            task["error"] = str(e)
            _save_prompt_reverse_history(task_id, links, video_paths, "failed")

    asyncio.create_task(_runner())
    return {
        "success": True,
        "task_id": task_id,
        "item_index": item_index,
        "message": f"开始重试第 {item_index + 1} 条（从失败阶段续跑）",
    }


class ReplicaWan3SubmitRequest(BaseModel):
    """wan3.0 复刻请求"""
    video_path: str            # 爆款视频本地路径
    image_path: str            # 产品图本地路径
    prompt: str                # H3 prompt 文本
    width: Optional[int] = None
    height: Optional[int] = None
    fps: Optional[int] = None
    output_subdir: Optional[str] = None


@app.post("/api/replica-wan3/submit")
async def replica_wan3_submit(req: ReplicaWan3SubmitRequest):
    """提交 wan3.0 复刻任务到 RunningHub（异步执行）"""
    # 🔴 花钱批准闸（RunningHub 按 GPU 秒真扣钱）：purpose 给 "*" —— 网关里的 wan3 路由是 dashscope 那条，
    #    与这条走的 RunningHub 不是同一个上游，**不拿不相干的路由单价糊报价**，所以只认通吃条子、按上限报。
    _spend_or_423("video.generate", "*", "replica-wan3 复刻出片")
    from backend.services.replica_wan3 import submit_replica, poll_until_done, validate_config

    err = validate_config()
    if err:
        raise HTTPException(400, err)
    if not Path(req.video_path).exists():
        raise HTTPException(400, f"视频不存在: {req.video_path}")
    if not Path(req.image_path).exists():
        raise HTTPException(400, f"产品图不存在: {req.image_path}")
    if not req.prompt or not req.prompt.strip():
        raise HTTPException(400, "H3 prompt 不能为空")

    sub = req.output_subdir or f"replica_wan3_{uuid.uuid4().hex[:8]}"
    out_dir = OUTPUT_DIR / sub
    out_dir.mkdir(parents=True, exist_ok=True)

    task_id = f"w3_{uuid.uuid4().hex[:12]}"
    tasks[task_id] = {
        "task_id": task_id, "type": "replica_wan3", "status": "INIT",
        "video_path": req.video_path, "image_path": req.image_path,
        "prompt": req.prompt, "output_dir": str(out_dir),
        "created_at": datetime.now().isoformat(),
        "rh_task_id": None, "result": None, "error": None,
    }

    async def _runner():
        try:
            tasks[task_id]["status"] = "RUNNING"
            tasks[task_id]["started_at"] = datetime.now().isoformat()
            # 1. 提交到 RH
            submit = await submit_replica(
                video_path=req.video_path,
                image_path=req.image_path,
                prompt=req.prompt,
                width=req.width, height=req.height, fps=req.fps,
            )
            rh_task_id = submit["task_id"]
            tasks[task_id]["rh_task_id"] = rh_task_id
            tasks[task_id]["submit_detail"] = submit
            print(f"[wan3.0] {task_id} → RH {rh_task_id}")
            # 2. 轮询完成
            poll = await poll_until_done(rh_task_id, max_wait=1800)
            tasks[task_id]["result"] = poll
            tasks[task_id]["status"] = "SUCCESS"
            tasks[task_id]["finished_at"] = datetime.now().isoformat()
        except Exception as e:
            tasks[task_id]["status"] = "FAILED"
            tasks[task_id]["error"] = str(e)
            tasks[task_id]["finished_at"] = datetime.now().isoformat()
            print(f"[!] replica_wan3 {task_id} 失败: {e}")

    asyncio.create_task(_runner())
    return {"success": True, "task_id": task_id, "output_dir": str(out_dir)}


@app.get("/api/replica-wan3/status/{task_id}")
async def replica_wan3_status(task_id: str):
    """查询 wan3.0 复刻任务状态"""
    t = tasks.get(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    return {
        "task_id": task_id, "type": t["type"],
        "status": t["status"], "error": t.get("error"),
        "rh_task_id": t.get("rh_task_id"),
        "created_at": t.get("created_at"),
        "started_at": t.get("started_at"),
        "finished_at": t.get("finished_at"),
        "result": t.get("result"),
    }


# ─────────────────────────────────────
# Wan3.0 爆款复刻 Pipeline API（3 步工作流：MiMo 一站式解析+反推 → 并发生成 → 合并）
# 严格遵循 wan3-prompt-builder skill v3.1.0
# ─────────────────────────────────────

# 任务存储：replica_wan3_pipeline_tasks[task_id] = {...}
replica_wan3_pipeline_tasks: dict = {}


class ReplicaWan3PipelineSubmitRequest(BaseModel):
    """wan3.0 爆款复刻 Pipeline 请求
    
    替换目标由 LLM 自行推断：
      - 上传产品图 → LLM 视觉识别新产品特征 → 在 prompt 中用 Image N 替换原产品
      - 上传模特图 → LLM 识别新模特 → 在 prompt 中用 Image N 替换原人物
      - 上传场景图 → LLM 识别新背景 → 在 prompt 中用 Image N 替换原背景
      - 客户需求说明 → LLM 综合判断意图
    """
    video_path: str                                  # 爆款视频本地路径
    product_image_paths: List[str] = []              # 产品图（多张，存在即触发换产品）
    model_image_path: Optional[str] = None           # 模特图（1 张，存在即触发换模特）
    scene_image_path: Optional[str] = None           # 场景图（1 张，存在即触发换背景）
    customer_request: str = ""                      # 客户需求说明（LLM 综合判断意图）
    output_subdir: Optional[str] = None              # 输出子目录名


@app.post("/api/replica-wan3-pipeline/submit")
async def replica_wan3_pipeline_submit(req: ReplicaWan3PipelineSubmitRequest):
    """提交 wan3.0 爆款复刻 pipeline 任务（异步执行 3 步工作流）"""
    from backend.services.replica_wan3_pipeline import run_pipeline

    if not Path(req.video_path).exists():
        raise HTTPException(400, f"爆款视频不存在: {req.video_path}")
    for p in req.product_image_paths:
        if not Path(p).exists():
            raise HTTPException(400, f"产品图不存在: {p}")
    if req.model_image_path and not Path(req.model_image_path).exists():
        raise HTTPException(400, f"模特图不存在: {req.model_image_path}")
    if req.scene_image_path and not Path(req.scene_image_path).exists():
        raise HTTPException(400, f"场景图不存在: {req.scene_image_path}")

    sub = req.output_subdir or f"replica_wan3_pipe_{uuid.uuid4().hex[:8]}"
    out_dir = OUTPUT_DIR / sub
    out_dir.mkdir(parents=True, exist_ok=True)

    task_id = f"w3p_{uuid.uuid4().hex[:12]}"
    # 自动推断替换目标（仅用于状态展示，LLM 自行视觉识别）
    auto_replace = {
        "replace_product": bool(req.product_image_paths),
        "replace_model": bool(req.model_image_path),
        "replace_background": bool(req.scene_image_path),
    }
    replica_wan3_pipeline_tasks[task_id] = {
        "task_id": task_id,
        "type": "replica_wan3_pipeline",
        "status": "PENDING",
        "phase": "init",
        "video_path": req.video_path,
        "product_image_paths": req.product_image_paths,
        "model_image_path": req.model_image_path,
        "scene_image_path": req.scene_image_path,
        "auto_replace": auto_replace,
        "customer_request": req.customer_request,
        "output_dir": str(out_dir),
        "logs": [],
        "storyboard": None,
        "segment_plans": None,
        "segment_results": None,
        "merged_path": None,
        "duration": None,
        "error": None,
        "created_at": datetime.now().isoformat(),
        "started_at": None,
        "finished_at": None,
    }

    async def _runner():
        t = replica_wan3_pipeline_tasks[task_id]
        t["status"] = "RUNNING"
        t["started_at"] = datetime.now().isoformat()

        def _log(msg: str) -> None:
            t["logs"].append({"ts": datetime.now().isoformat(), "msg": msg})

        def _phase(phase: str, extra: dict) -> None:
            t["phase"] = phase
            if phase == "cutting":
                _log("▶ Step 1/3: MiMo 一站式解析 + wan3.0 prompt 反推")
            elif phase == "generating":
                _log(f"▶ Step 2/3: 并发提交 wan3.0（{extra.get('segments', 0)} 段）")
            elif phase == "merging":
                _log("▶ Step 3/3: ffmpeg concat 合并")
            elif phase == "completed":
                _log(f"✓ 工作流完成 → {extra.get('merged_path', '')}")
                t["merged_path"] = extra.get("merged_path")
                t["duration"] = extra.get("duration")

        try:
            result = await run_pipeline(
                task_id=task_id,
                video_path=req.video_path,
                product_image_paths=req.product_image_paths,
                model_image_path=req.model_image_path or "",
                scene_image_path=req.scene_image_path or "",
                customer_request=req.customer_request,
                output_dir=str(out_dir),
                on_log=_log,
                on_phase=_phase,
            )
            t["storyboard"] = result.get("storyboard")
            t["segment_plans"] = result.get("segment_plans")
            t["segment_results"] = result.get("segment_results")
            t["merged_path"] = result.get("merged_path")
            t["duration"] = result.get("duration")
            t["status"] = "SUCCESS"
            t["finished_at"] = datetime.now().isoformat()
        except Exception as e:
            t["status"] = "FAILED"
            t["error"] = str(e)
            t["finished_at"] = datetime.now().isoformat()
            _log(f"✗ 失败: {e}")
            print(f"[!] replica_wan3_pipeline {task_id} 失败: {e}")

    asyncio.create_task(_runner())
    return {
        "success": True,
        "task_id": task_id,
        "output_dir": str(out_dir),
        "phase": "init",
        "status": "PENDING",
    }


@app.get("/api/replica-wan3-pipeline/status/{task_id}")
async def replica_wan3_pipeline_status(task_id: str):
    """查询 pipeline 任务状态（含阶段、日志、分镜、段结果）"""
    t = replica_wan3_pipeline_tasks.get(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    return {
        "task_id": task_id,
        "type": t["type"],
        "status": t["status"],          # PENDING / RUNNING / SUCCESS / FAILED
        "phase": t["phase"],           # init / cutting / reversing / generating / merging / completed
        "error": t.get("error"),
        "video_path": t.get("video_path"),
        "output_dir": t.get("output_dir"),
        "merged_path": t.get("merged_path"),
        "duration": t.get("duration"),
        "storyboard": t.get("storyboard"),
        "segment_plans": t.get("segment_plans"),
        "segment_results": t.get("segment_results"),
        "logs": t.get("logs", []),
        "created_at": t.get("created_at"),
        "started_at": t.get("started_at"),
        "finished_at": t.get("finished_at"),
    }


@app.get("/api/replica-wan3-pipeline/result/{task_id}")
async def replica_wan3_pipeline_result(task_id: str):
    """下载 pipeline 成品视频"""
    t = replica_wan3_pipeline_tasks.get(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    if t["status"] != "SUCCESS" or not t.get("merged_path"):
        raise HTTPException(400, f"任务未完成，当前状态: {t['status']}")
    merged = Path(t["merged_path"])
    if not merged.exists():
        raise HTTPException(404, "成品视频文件不存在")
    return FileResponse(
        str(merged),
        media_type="video/mp4",
        filename=f"replica_wan3_{task_id}.mp4",
    )


@app.get("/api/replica-wan3-pipeline/tasks")
async def replica_wan3_pipeline_tasks_list():
    """列出所有 pipeline 任务（按创建时间倒序，最多 50 条）"""
    items = sorted(
        replica_wan3_pipeline_tasks.values(),
        key=lambda t: t.get("created_at", ""),
        reverse=True,
    )[:50]
    return {
        "tasks": [
            {
                "task_id": t["task_id"],
                "status": t["status"],
                "phase": t["phase"],
                "video_path": t.get("video_path"),
                "merged_path": t.get("merged_path"),
                "duration": t.get("duration"),
                "created_at": t.get("created_at"),
                "finished_at": t.get("finished_at"),
                "error": t.get("error"),
            }
            for t in items
        ]
    }


# ─────────────────────────────────────
# 超级复刻 Wan3.0 API（单模式「超级复刻」≤15s 爆款复刻）
# 4 步工作流：MiMo（纯解析）→ GPT 编译器 → GPT Lint → Wan3.0（执行器）
# ─────────────────────────────────────

# 任务存储：super_clone_wan3_tasks[task_id] = {...}
super_clone_wan3_tasks: dict = {}

# ── 断点继续：任务状态落盘 ──
# 为什么要落盘：任务只存在内存里时，后端重启/进程中断 = 任务连同请求参数一起消失，
# 盘上只剩半成品目录却没人能接着跑。现在每个任务在 output_dir 下留一份状态快照，
# 重启后历史记录仍可见，并可「断点继续」从最后成功步骤复用产物接着跑。
SC_STATE_FILE = "_super_clone_state.json"
_SC_PERSIST_KEYS = (
    "task_id", "type", "status", "phase", "error", "request", "output_dir",
    "prompt", "storyboard", "result_url", "local_path", "duration", "video_duration",
    "trimmed_path", "wan3_task_id", "segment_paths", "segment_count", "trim_start",
    "trim_end", "resolution", "aspect_ratio", "audio", "model_image_path",
    "scene_image_path", "created_at", "started_at", "finished_at",
)


def _persist_super_clone_task(t: dict, force: bool = False) -> None:
    """把任务状态快照写入 output_dir/_super_clone_state.json（节流，落盘失败不影响运行）。"""
    out_dir = t.get("output_dir")
    if not out_dir:
        return
    now = time.time()
    if not force and now - float(t.get("_persist_at") or 0) < 0.8:
        return
    t["_persist_at"] = now
    try:
        data = {k: t.get(k) for k in _SC_PERSIST_KEYS}
        data["logs"] = list(t.get("logs") or [])[-1000:]
        data["updated_at"] = datetime.now().isoformat()
        path = Path(out_dir) / SC_STATE_FILE
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001
        print(f"[!] super_clone_wan3 状态落盘失败: {exc}", flush=True)


def _task_from_state(state: dict) -> dict:
    """把磁盘快照还原成任务字典；上次未正常收尾的任务标记为 INTERRUPTED（可断点继续）。"""
    task = {k: state.get(k) for k in _SC_PERSIST_KEYS}
    task["task_id"] = state.get("task_id") or ""
    task["type"] = state.get("type") or "super_clone_wan3"
    task["logs"] = list(state.get("logs") or [])
    task["error"] = state.get("error")
    saved_status = str(state.get("status") or "FAILED")
    # 只有正常收尾的 SUCCESS/FAILED 可信；盘上是 RUNNING/PENDING = 进程已经不在了
    if saved_status in ("RUNNING", "PENDING"):
        task["status"] = "INTERRUPTED"
        task["interrupted_from"] = saved_status
    else:
        task["status"] = saved_status
    task["resumable"] = task["status"] in ("FAILED", "INTERRUPTED") and bool(task.get("request"))
    task["_from_disk"] = True
    return task


def _find_disk_super_clone_task(task_id: str) -> Optional[dict]:
    """按 task_id 在 outputs 目录里找状态快照（后端重启后恢复用）。"""
    if not task_id:
        return None
    for state_file in OUTPUT_DIR.glob(f"*/{SC_STATE_FILE}"):
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if str(state.get("task_id") or "") != task_id:
            continue
        state.setdefault("output_dir", str(state_file.parent))
        return _task_from_state(state)
    return None


def _list_disk_super_clone_tasks() -> List[dict]:
    """列出所有带状态快照的任务（按创建时间倒序）。"""
    items: List[dict] = []
    for state_file in OUTPUT_DIR.glob(f"*/{SC_STATE_FILE}"):
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if not state.get("task_id"):
            continue
        state.setdefault("output_dir", str(state_file.parent))
        items.append(_task_from_state(state))
    items.sort(key=lambda t: t.get("created_at") or "", reverse=True)
    return items


async def _super_clone_runner(task_id: str, resume: bool = False) -> None:
    """执行/续跑超级复刻任务。所有入参都从 task['request'] 读，保证重启后也能重建同一条任务。"""
    from backend.services.super_clone_wan3 import run_pipeline

    t = super_clone_wan3_tasks[task_id]
    r = t.get("request") or {}
    t["status"] = "RUNNING"
    t["started_at"] = t.get("started_at") or datetime.now().isoformat()
    if resume:
        t["logs"].append({"ts": datetime.now().isoformat(),
                          "msg": "▶ 断点继续：已完成步骤自动复用，缺失步骤重新执行"})
    _persist_super_clone_task(t, force=True)

    def _log(msg: str) -> None:
        t["logs"].append({"ts": datetime.now().isoformat(), "msg": msg})
        _persist_super_clone_task(t)

    def _phase(phase: str, extra: dict) -> None:
        t["phase"] = phase
        if phase == "step1_mimo":
            # 这三行 banner 里【不写厂商标】：分镜腿可在 agnes/小米之间切、编译/审查腿可在 magpie/mimo/grok/gpt
            #   之间切（config 开关），写死一家名就是让 UI 照着假话念引擎。真型号由服务层自己打：
            #   `分镜腿=…（model=…）`、`调用编译器（<model>，…）`、`审查腿=…（model=…）`。
            _log("▶ Step 1/4: 分镜腿（纯视频解析器）→ storyboard JSON")
        elif phase == "step1_5_gpt_compile":
            _log(f"▶ Step 1.5/4: 编译器 → storyboard JSON → Wan3.0 prompt（{extra.get('shots', '?')} 镜）")
        elif phase == "step2_gpt_lint":
            _log("▶ Step 2/4: 审查（Lint）→ 规则校验 + LLM 二次审查")
        elif phase in ("step3_wan3", "step3_h3"):
            executor = "满雪 H3 全能参考" if phase == "step3_h3" else "Wan3.0"
            _log(f"▶ Step 3/4: {executor}（执行器）→ 生成成品视频（目标 {extra.get('target_duration', '?')}s）")
        elif phase == "completed":
            _log(f"✓ 工作流完成 → {extra.get('final_path', '')}")
            t["local_path"] = extra.get("final_path")
        _persist_super_clone_task(t, force=True)

    try:
        compile_notes = []
        customer_request = (r.get("customer_request") or "").strip()
        if r.get("swap_person"):
            compile_notes.append(
                "换脸：主体人物严格按模特图，只换脸、五官、发型、肤色；"
                "忽略模特图中的服装与配饰。成片服装只来自产品图。"
            )
        if r.get("swap_scene"):
            bg = (r.get("scene_text") or "").strip()
            compile_notes.append(
                "换背景（仅文字，不提供场景图）："
                f"{bg}\n每个 Shot 的人物站位、动作、运镜继承原视频；"
                "禁止把人贴在一张背景静帧上循环；不要写回原室内除非用户未要求换背景。"
            )
            extra_bg = f"背景要求：{bg}"
            customer_request = f"{customer_request}\n\n{extra_bg}".strip() if customer_request else extra_bg
        result = await run_pipeline(
            task_id=task_id,
            video_path=r.get("video_path"),
            product_image_paths=r.get("product_image_paths") or [],
            customer_request=customer_request,
            output_dir=t.get("output_dir"),
            on_log=_log,
            on_phase=_phase,
            trim_start=r.get("trim_start"),
            trim_end=r.get("trim_end"),
            model_image_path=r.get("model_image_path") if r.get("swap_person") else None,
            scene_image_path=None,
            product_name=r.get("product_name") or "",
            resolution=r.get("resolution"),
            aspect_ratio=r.get("aspect_ratio"),
            audio=True if r.get("audio") is None else r.get("audio"),
            engine=r.get("engine") or "wan3",
            extra_compile_note="\n".join(compile_notes) if compile_notes else None,
            background_override=(r.get("scene_text") or "").strip() if r.get("swap_scene") else None,
            voice_audio_paths=r.get("voice_audio_paths") or [],
            resume=resume,
        )
        t["prompt"] = result.get("prompt")
        t["storyboard"] = result.get("storyboard")
        t["result_url"] = result.get("result_url")
        t["local_path"] = result.get("local_path")
        t["duration"] = result.get("duration")
        t["video_duration"] = result.get("video_duration")
        t["trimmed_path"] = result.get("trimmed_path")
        t["wan3_task_id"] = result.get("task_id")
        t["segment_paths"] = result.get("segment_paths")
        t["segment_count"] = result.get("segment_count")
        t["status"] = "SUCCESS"
        t["phase"] = "completed"
        t["error"] = None
        t["finished_at"] = datetime.now().isoformat()
    except Exception as e:
        t["status"] = "FAILED"
        t["error"] = str(e)
        t["finished_at"] = datetime.now().isoformat()
        t["logs"].append({"ts": datetime.now().isoformat(), "msg": f"✗ 失败: {e}"})
        print(f"[!] super_clone_wan3 {task_id} 失败: {e}", flush=True)
    _persist_super_clone_task(t, force=True)


class SuperCloneWan3SubmitRequest(BaseModel):
    """超级复刻 wan3.0 请求

    单模式「超级复刻」：原视频 + 产品图 → MiMo 生成提示词 → Wan3.0 生成成品。
    超过 15s 时按 15s 切段生成再自动拼接。trim 可选，用于截取原片中要复刻的区间。
    """
    video_path: str                            # 爆款视频本地路径（任意时长；>15s 自动切段拼接）
    product_image_paths: List[str] = []        # 产品图（1-10 张）
    product_name: str                          # 产品名（必填，GPT 编译器以此替换原视频产品）
    customer_request: str = ""                # 客户需求说明（可选，自然语言：场景/风格/特征要求）
    output_subdir: Optional[str] = None        # 输出子目录名
    trim_start: Optional[float] = None         # 裁剪起点（秒，0-based），与 trim_end 同时提供时只复刻该区间
    trim_end: Optional[float] = None           # 裁剪终点（秒，0-based），不限 15s
    model_image_path: Optional[str] = None     # 有值即换脸，进 Wan3 模特槽
    scene_image_path: Optional[str] = None     # 已废弃：换背景改为文字，不再传场景图
    scene_text: str = ""                       # 有文字即换背景，不上传背景图
    voiceover_replacements: List[dict] = []    # 兼容旧字段，口播跟原片
    person_app_key: str = "dreamid_v"
    scene_app_key: str = "bg_replace"
    resolution: Optional[str] = None           # 输出分辨率（可选：480P/720P/1080P），默认 480P
    aspect_ratio: Optional[str] = None         # 画幅比例（可选：adaptive/16:9/9:16/1:1），默认 adaptive
    audio: Optional[bool] = None               # 是否保留原音频（默认 True）
    voice_audio_paths: List[str] = []          # 音色参考音频本地路径（≤5 段，单段 1-15s，总时长 ≤15s）
    engine: str = "wan3"                       # 执行器：wan3 或 h3（临时测试）


@app.get("/api/super-clone-wan3/probe-duration")
async def super_clone_wan3_probe_duration(path: str):
    """探测视频时长（秒），供前端剪辑尺初始化默认区间"""
    from backend.services.super_clone_wan3 import _ffprobe_duration
    if not path or not Path(path).exists():
        raise HTTPException(404, f"文件不存在: {path}")
    dur = _ffprobe_duration(path)
    return {"path": path, "duration": dur or 0.0}


@app.post("/api/super-clone-wan3/submit")
async def super_clone_wan3_submit(req: SuperCloneWan3SubmitRequest):
    """提交超级复刻 wan3.0 任务（异步执行 4 步工作流）

    Step 1:   MiMo（纯视频解析器）→ storyboard JSON
    Step 1.5: Grok 编译器 → storyboard JSON → Wan3.0 prompt
    Step 2:   Grok Lint → 规则校验 + LLM 二次审查
    Step 3:   Wan3.0（执行器）→ 原视频+产品图+提示词 → 成品视频

    时长规则：允许上传任意时长视频。
      - 若提供 trim_start/trim_end：只复刻该区间（最短 1s，不限最长）
      - 区间或原片超过 15s：按 15s 切段生成后自动拼接
    """
    from backend.services.super_clone_wan3 import run_pipeline, validate_input, validate_trim, _ffprobe_duration, _cfg, engine_lock_error
    # 🔴 引擎锁放在【建任务记录之前】：否则要等 _runner 抛出来才标 FAILED，
    #    人看到的是一串已经排队的任务，而不是一句「这条线没有第二个引擎」。
    _lock = engine_lock_error(req.engine)
    if _lock:
        raise HTTPException(400, _lock)

    # 入口校验：视频存在 + 能探测时长
    err = validate_input(req.video_path)
    if err:
        raise HTTPException(400, err)
    if not req.product_image_paths:
        raise HTTPException(400, "至少需要 1 张产品图")
    for p in req.product_image_paths:
        if not Path(p).exists():
            raise HTTPException(400, f"产品图不存在: {p}")
    # 产品名为必填（GPT 编译器依赖此名做产品识别与替换）
    if not req.product_name or not req.product_name.strip():
        raise HTTPException(400, "product_name 为必填字段，请提供产品名")
    swap_person = bool(req.model_image_path)
    # 🔴 非空 ≠ 要换背景（2026-10-22 sc_9c246af70384：客户把整段任务说明写进「换背景」栏，
    #   而那句里明写着「背景…全部保留原片」，旧写法 `bool(scene_text)` 替他打开了两道从未要求的闸）。
    #   判定抽在 super_clone_wan3.scene_swap_decision —— 入口与 Layer1 闸读同一份，不各抄一套。
    from backend.services.super_clone_wan3 import scene_swap_decision
    swap_scene, swap_why = scene_swap_decision(req.scene_text)
    # 他写的自然语言是合同：判成不换背景时，这段文字不能丢，原样并进「客户需求说明」继续进编译器。
    customer_request_final = (req.customer_request or "").strip()
    if not swap_scene and (req.scene_text or "").strip():
        _st = req.scene_text.strip()
        customer_request_final = (customer_request_final + "\n" + _st).strip() if customer_request_final else _st
    if swap_person and not Path(req.model_image_path).exists():
        raise HTTPException(400, f"面部图不存在: {req.model_image_path}")
    vd = _ffprobe_duration(req.video_path) or 0.0
    if req.trim_start is not None or req.trim_end is not None:
        terr = validate_trim(req.trim_start, req.trim_end, vd, enforce_max=False)
        if terr:
            raise HTTPException(400, terr)
    else:
        min_dur = _cfg()["min_trim_duration"]
        if vd < min_dur:
            raise HTTPException(
                400,
                f"原视频时长 {vd:.2f}s < {min_dur:.0f}s（最短 {min_dur:.0f}s）",
            )

    sub = req.output_subdir or f"super_clone_wan3_{uuid.uuid4().hex[:8]}"
    out_dir = OUTPUT_DIR / sub
    out_dir.mkdir(parents=True, exist_ok=True)

    task_id = f"sc_{uuid.uuid4().hex[:12]}"
    # ★完整入参快照：断点继续时靠它重建同一条任务（后端重启后也不丢参数）
    request_snapshot = {
        "video_path": req.video_path,
        "product_image_paths": list(req.product_image_paths or []),
        "product_name": req.product_name,
        "customer_request": customer_request_final,
        "output_subdir": req.output_subdir,
        "trim_start": req.trim_start,
        "trim_end": req.trim_end,
        "model_image_path": req.model_image_path,
        "scene_image_path": req.scene_image_path,
        "scene_text": req.scene_text,
        "voiceover_replacements": req.voiceover_replacements,
        "person_app_key": req.person_app_key,
        "scene_app_key": req.scene_app_key,
        "resolution": req.resolution,
        "aspect_ratio": req.aspect_ratio,
        "audio": req.audio,
        "voice_audio_paths": list(req.voice_audio_paths or []),
        "engine": req.engine,
        "swap_person": swap_person,
        "swap_scene": swap_scene,
    }
    super_clone_wan3_tasks[task_id] = {
        "task_id": task_id,
        "type": "super_clone_wan3",
        "status": "PENDING",
        "phase": "init",
        "request": request_snapshot,
        "video_path": req.video_path,
        "product_image_paths": req.product_image_paths,
        "customer_request": customer_request_final,
        "model_image_path": req.model_image_path,
        "scene_image_path": req.scene_image_path,
        "swap_person": swap_person,
        "swap_scene": swap_scene,
        "voiceover_replacements": req.voiceover_replacements,
        "person_app_key": req.person_app_key,
        "scene_app_key": req.scene_app_key,
        "trim_start": req.trim_start,
        "trim_end": req.trim_end,
        "resolution": req.resolution,
        "aspect_ratio": req.aspect_ratio,
        "audio": req.audio,
        "voice_audio_paths": req.voice_audio_paths,
        "output_dir": str(out_dir),
        "logs": [],
        "prompt": None,           # Step 1 输出的 Wan3.0 提示词
        "storyboard": None,       # MiMo 分镜
        "result_url": None,       # Wan3.0 结果视频 URL
        "local_path": None,       # 本地成品视频路径
        "duration": None,         # 成品时长（秒）
        "video_duration": None,   # 原视频时长
        "trimmed_path": None,     # 裁剪后视频路径
        "wan3_task_id": None,     # Wan3.0 任务 ID
        "error": None,
        "created_at": datetime.now().isoformat(),
        "started_at": None,
        "finished_at": None,
    }
    # 判成什么就说什么：这道闸开没开、为什么开，必须印在任务日志上，别让人去猜（也别让人以为是自己填错了）
    super_clone_wan3_tasks[task_id]["logs"].append({
        "ts": datetime.now().isoformat(),
        "msg": "换背景判定=%s（%s）%s" % (
            "要换" if swap_scene else "不换", swap_why,
            "" if swap_scene else "；「换背景」栏那段文字已并进客户需求说明，合同不丢"),
    })
    _persist_super_clone_task(super_clone_wan3_tasks[task_id], force=True)

    asyncio.create_task(_super_clone_runner(task_id))
    return {
        "success": True,
        "task_id": task_id,
        "output_dir": str(out_dir),
        "phase": "init",
        "status": "PENDING",
    }


def _super_clone_generate_offer(t: dict) -> dict:
    """提示词下面那颗「生成视频」按钮的料（只读、0 消耗、不建任何条子）。

    出现条件只写一处：有编译好的 prompt + 盘上没有成片 + 这一单没在跑。
    分数现读价目表（`model_prices.json` 改了按钮跟着改，前端不许硬编码 60）。
    """
    from backend import gateway as gw

    local = str(t.get("local_path") or "")
    delivered = bool(local) and Path(local).is_file()
    segments = 1
    try:
        plan = Path(str(t.get("output_dir") or "")) / "_submit_plan.json"
        if plan.is_file():
            segments = max(1, int(json.loads(plan.read_text(encoding="utf-8")).get("count") or 1))
    except Exception as e:                                     # noqa: BLE001
        print(f"[!] super_clone 提交计划读不到（按钮按 1 段显示）：{e}", flush=True)
    try:
        # 分数只认网关那一份估法（`meter` 与预扣同源；在这里再乘一遍时长档就是第二套判据）
        m = gw.meter("video.generate", "wan3-prime", {"count": segments})
        unit, total = int(m["unit_price"]), int(m["credits_estimate"])
    except Exception as e:                                     # noqa: BLE001
        print(f"[!] super_clone 报价读不到（按钮显示『价格待查』）：{e}", flush=True)
        unit = total = 0
    return {
        "lane": "video.generate/wan3-prime",
        "delivered": delivered,
        "segments": segments,
        "unit_price": unit,
        "total_credits": total,
        "ready": bool(t.get("prompt")) and not delivered and t.get("status") != "RUNNING",
    }


@app.get("/api/super-clone-wan3/status/{task_id}")
async def super_clone_wan3_status(task_id: str):
    """查询超级复刻 wan3.0 任务状态（内存里没有时从磁盘快照恢复，支持重启后查询）"""
    t = super_clone_wan3_tasks.get(task_id)
    if not t:
        t = _find_disk_super_clone_task(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    return {
        "task_id": task_id,
        "type": t.get("type") or "super_clone_wan3",
        "status": t["status"],
        "phase": t.get("phase"),
        "error": t.get("error"),
        "prompt": t.get("prompt"),
        "storyboard": t.get("storyboard"),
        "result_url": t.get("result_url"),
        "local_path": t.get("local_path"),
        "duration": t.get("duration"),
        "video_duration": t.get("video_duration"),
        "trimmed_path": t.get("trimmed_path"),
        "trim_start": t.get("trim_start"),
        "trim_end": t.get("trim_end"),
        "model_image_path": t.get("model_image_path"),
        "scene_image_path": t.get("scene_image_path"),
        "resolution": t.get("resolution"),
        "aspect_ratio": t.get("aspect_ratio"),
        "audio": t.get("audio"),
        "wan3_task_id": t.get("wan3_task_id"),
        "segment_count": t.get("segment_count"),
        "segment_paths": t.get("segment_paths"),
        "logs": t.get("logs", []),
        "resumable": bool(t.get("resumable")) or t["status"] in ("FAILED", "INTERRUPTED"),
        "generate": _super_clone_generate_offer(t),
        "created_at": t.get("created_at"),
        "started_at": t.get("started_at"),
        "finished_at": t.get("finished_at"),
    }


@app.post("/api/super-clone-wan3/resume/{task_id}")
async def super_clone_wan3_resume(task_id: str):
    """断点继续任务：从最后成功步骤续跑（已落盘的产物自动复用，缺失才重跑）。

    覆盖：任务 FAILED，或后端重启/进程中断导致盘上状态停在 RUNNING/PENDING（显示为 INTERRUPTED）。
    """
    from backend.services.super_clone_wan3 import engine_lock_error, validate_input

    t = super_clone_wan3_tasks.get(task_id)
    if t:
        if t.get("status") == "RUNNING":
            raise HTTPException(400, "任务正在运行，无需断点继续")
        if t.get("status") == "SUCCESS":
            # 🔴 判据是【盘上有没有成片】，不是状态字：2026-10-22 sc_9c246af70384 提交被批准闸拒回
            #    （0 分未扣、上游没建任务）却写着 SUCCESS ⇒ 按状态字拒绝等于逼人重发一整单。
            if _super_clone_generate_offer(t)["delivered"]:
                raise HTTPException(400, "任务已交付成片，无需再生成")
    else:
        t = _find_disk_super_clone_task(task_id)
        if not t:
            raise HTTPException(404, "任务不存在，且磁盘上没有可恢复的状态快照")
        super_clone_wan3_tasks[task_id] = t

    req = t.get("request") or {}
    if not req:
        raise HTTPException(400, "缺少原始请求参数（该任务早于断点功能），无法继续，请重新发起")
    lock_err = engine_lock_error(req.get("engine") or "wan3")
    if lock_err:
        raise HTTPException(400, lock_err)
    in_err = validate_input(req.get("video_path") or "")
    if in_err:
        raise HTTPException(400, f"原始视频不可用，无法继续：{in_err}")
    missing = [p for p in (req.get("product_image_paths") or [])
               if p and not Path(p).exists()]
    if missing:
        raise HTTPException(400, f"产品图已不存在，无法继续：{missing[0]}")
    if req.get("swap_person") and req.get("model_image_path") and not Path(req["model_image_path"]).exists():
        raise HTTPException(400, f"模特图已不存在，无法继续：{req['model_image_path']}")

    t["status"] = "RUNNING"
    t["error"] = None
    t["finished_at"] = None
    t["resumable"] = False
    _persist_super_clone_task(t, force=True)
    asyncio.create_task(_super_clone_runner(task_id, resume=True))
    return {
        "success": True,
        "task_id": task_id,
        "status": "RUNNING",
        "resume_from_phase": t.get("phase"),
        "output_dir": t.get("output_dir"),
    }


@app.get("/api/super-clone-wan3/result/{task_id}")
async def super_clone_wan3_result(task_id: str):
    """下载成品视频（内存里没有时从磁盘快照恢复）"""
    t = super_clone_wan3_tasks.get(task_id) or _find_disk_super_clone_task(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    local = t.get("local_path")
    if not local or not Path(local).exists():
        raise HTTPException(404, "成品视频未就绪")
    return FileResponse(
        str(local),
        media_type="video/mp4",
        filename=f"super_clone_wan3_{task_id}.mp4",
    )


@app.get("/api/super-clone-wan3/tasks")
async def super_clone_wan3_tasks_list():
    """列出所有超级复刻任务（内存 + 磁盘快照合并，按创建时间倒序，最多 50 条）"""
    merged: dict = {}
    for t in _list_disk_super_clone_tasks():
        merged[t["task_id"]] = t
    for t in super_clone_wan3_tasks.values():
        merged[t["task_id"]] = t
    items = sorted(
        merged.values(),
        key=lambda t: t.get("created_at") or "",
        reverse=True,
    )[:50]
    return {
        "tasks": [
            {
                "task_id": t["task_id"],
                "status": t["status"],
                "phase": t.get("phase"),
                "video_path": t.get("video_path") or (t.get("request") or {}).get("video_path"),
                "local_path": t.get("local_path"),
                "duration": t.get("duration"),
                "video_duration": t.get("video_duration"),
                "created_at": t.get("created_at"),
                "finished_at": t.get("finished_at"),
                "error": t.get("error"),
                "resumable": bool(t.get("resumable")) or t["status"] in ("FAILED", "INTERRUPTED"),
            }
            for t in items
        ]
    }


# ─────────────────────────────────────
# 视频换人 / 换背景（RunningHub AI App）
# ─────────────────────────────────────

video_person_scene_tasks: dict = {}


class VideoPersonSceneSubmitRequest(BaseModel):
    video_path: str
    swap_person: bool = False
    swap_scene: bool = False
    person_image_path: Optional[str] = None
    scene_image_path: Optional[str] = None
    person_app_key: str = "dreamid_v"
    scene_app_key: str = "bg_replace"
    prompt: str = ""
    output_subdir: Optional[str] = None


@app.get("/video-person-scene", response_class=HTMLResponse)
async def video_person_scene_page():
    html_path = Path(settings.FRONTEND_DIR) / "video_person_scene.html"
    if not html_path.exists():
        raise HTTPException(404, "视频换人换背景页面未找到")
    return HTMLResponse(
        content=html_path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"},
    )


@app.get("/api/video-person-scene/apps")
async def video_person_scene_apps():
    from backend.services.video_person_scene import list_apps
    return list_apps()


@app.get("/api/video-person-scene/probe-app/{app_id}")
async def video_person_scene_probe_app(app_id: str):
    from backend.services.video_person_scene import probe_ai_app
    return await probe_ai_app(app_id)


@app.post("/api/video-person-scene/submit")
async def video_person_scene_submit(req: VideoPersonSceneSubmitRequest):
    from backend.services.video_person_scene import run_person_scene

    if not req.swap_person and not req.swap_scene:
        raise HTTPException(400, "请至少勾选换人或换背景")
    if not req.video_path or not Path(req.video_path).exists():
        raise HTTPException(400, "视频不存在")
    if req.swap_person and (not req.person_image_path or not Path(req.person_image_path).exists()):
        raise HTTPException(400, "勾选换人后必须上传人物图")
    if req.swap_scene and (not req.scene_image_path or not Path(req.scene_image_path).exists()):
        raise HTTPException(400, "勾选换背景后必须上传背景图")

    sub = req.output_subdir or f"person_scene_{uuid.uuid4().hex[:8]}"
    out_dir = OUTPUT_DIR / sub
    out_dir.mkdir(parents=True, exist_ok=True)
    task_id = f"ps_{uuid.uuid4().hex[:12]}"
    video_person_scene_tasks[task_id] = {
        "task_id": task_id,
        "status": "PENDING",
        "phase": "init",
        "logs": [],
        "local_path": None,
        "error": None,
        "created_at": datetime.now().isoformat(),
    }

    async def _runner():
        t = video_person_scene_tasks[task_id]
        t["status"] = "RUNNING"

        def _log(msg: str) -> None:
            t["logs"].append({"ts": datetime.now().isoformat(), "msg": msg})

        try:
            path = await run_person_scene(
                video_path=req.video_path,
                output_dir=str(out_dir),
                swap_person=req.swap_person,
                swap_scene=req.swap_scene,
                person_image_path=req.person_image_path,
                scene_image_path=req.scene_image_path,
                person_app_key=req.person_app_key or "dreamid_v",
                scene_app_key=req.scene_app_key or "bg_v2",
                prompt=req.prompt or "",
                on_log=_log,
            )
            t["local_path"] = path
            t["status"] = "SUCCESS"
            t["phase"] = "completed"
            t["finished_at"] = datetime.now().isoformat()
        except Exception as e:
            t["status"] = "FAILED"
            t["error"] = str(e)
            t["finished_at"] = datetime.now().isoformat()
            _log(f"✗ 失败: {e}")

    asyncio.create_task(_runner())
    return {"success": True, "task_id": task_id, "output_dir": str(out_dir)}


@app.get("/api/video-person-scene/status/{task_id}")
async def video_person_scene_status(task_id: str):
    t = video_person_scene_tasks.get(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    return t


@app.get("/api/video-person-scene/result/{task_id}")
async def video_person_scene_result(task_id: str):
    t = video_person_scene_tasks.get(task_id)
    if not t or not t.get("local_path"):
        raise HTTPException(404, "结果不存在")
    return FileResponse(t["local_path"], media_type="video/mp4", filename=f"person_scene_{task_id}.mp4")


# ─────────────────────────────────────
# 爆款裂变 wan3.0（素材矩阵替换）
# ─────────────────────────────────────

hot_explosion_wan3_tasks: dict = {}


class MaterialGroupRequest(BaseModel):
    """单套素材组（爆款裂变支持 N 套素材并行裂变）"""
    product_image_paths: List[str] = []
    product_name: str
    model_image_path: Optional[str] = None
    scene_image_path: Optional[str] = None
    customer_request: str = ""
    product_facts: Optional[str] = None


class HotExplosionWan3SubmitRequest(BaseModel):
    """爆款裂变 wan3.0 请求

    模式：整段商品替换裂变（与超级复刻 1:1 模式相同 API 参数，但支持 N 套素材并行生成 N 个变体）。
    - 原视频（≤15s）+ N 套素材 → N 个变体视频
    - reference_video = 完整原视频
    - duration = -1（保持原视频时长）
    - 变化维度：产品/模特/场景/口播替换
    """
    video_path: str
    material_groups: List[MaterialGroupRequest]  # ≥1 套，每套生成 1 个变体
    output_subdir: Optional[str] = None
    trim_start: Optional[float] = None
    trim_end: Optional[float] = None
    max_concurrent: int = 3  # 并发上限（默认 3，避免 Wan3.0 API 限流）
    resolution: Optional[str] = None           # 输出分辨率（可选：480P/720P/1080P）
    aspect_ratio: Optional[str] = None         # 画幅比例（可选：adaptive/16:9/9:16/1:1）
    audio: Optional[bool] = None               # 是否保留原音频（默认 True）


@app.get("/api/hot-explosion-wan3/probe-duration")
async def hot_explosion_wan3_probe_duration(path: str):
    """探测视频时长（秒）"""
    from backend.services.super_clone_wan3 import _ffprobe_duration
    if not path or not Path(path).exists():
        raise HTTPException(404, f"文件不存在: {path}")
    dur = _ffprobe_duration(path)
    return {"path": path, "duration": dur or 0.0}


@app.post("/api/hot-explosion-wan3/submit")
async def hot_explosion_wan3_submit(req: HotExplosionWan3SubmitRequest):
    """旧矩阵裂变通道已退役（M5 收口，只断提交、保留捞回）。

    原「1 原视频 + N 素材组」矩阵工作流已被「账号知识库 + 智能体大脑裂变」取代
    （新入口 POST /api/smart-fission/start + /hot-explosion-wan3 新页）。
    旧生成腿必死：submit_reference_to_video 被 WAN3_RH_ENABLED=False 守卫拒绝
    （2026-10-09 禁令），放行只会白烧 MiMo/GPT 解析费。
    status / result / tasks 端点保留，用于捞回历史任务（只捞不重发）。
    """
    raise HTTPException(
        410,
        "旧矩阵裂变通道已下线，请走 /hot-explosion-wan3 新工作流（POST /api/smart-fission/start）",
    )


@app.get("/api/hot-explosion-wan3/status/{task_id}")
async def hot_explosion_wan3_status(task_id: str):
    """查询爆款裂变任务状态"""
    t = hot_explosion_wan3_tasks.get(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    return {
        "task_id": task_id,
        "type": t["type"],
        "status": t["status"],
        "phase": t.get("phase"),
        "error": t.get("error"),
        "storyboard": t.get("storyboard"),
        "target_duration": t.get("target_duration"),
        "variants": t.get("variants"),
        "success_count": t.get("success_count", 0),
        "failed_count": t.get("failed_count", 0),
        "material_groups": t.get("material_groups"),
        "trim_start": t.get("trim_start"),
        "trim_end": t.get("trim_end"),
        "max_concurrent": t.get("max_concurrent"),
        "resolution": t.get("resolution"),
        "aspect_ratio": t.get("aspect_ratio"),
        "audio": t.get("audio"),
        "logs": t.get("logs", []),
        "created_at": t.get("created_at"),
        "started_at": t.get("started_at"),
        "finished_at": t.get("finished_at"),
    }


@app.get("/api/hot-explosion-wan3/result/{task_id}/{variant_id}")
async def hot_explosion_wan3_result(task_id: str, variant_id: str):
    """下载指定变体的成品视频"""
    t = hot_explosion_wan3_tasks.get(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    variants = t.get("variants") or []
    match = next((v for v in variants if v.get("variant_id") == variant_id), None)
    if not match:
        raise HTTPException(404, f"变体 {variant_id} 不存在")
    local = match.get("local_path")
    if not local or not Path(local).exists():
        raise HTTPException(404, f"变体 {variant_id} 成品未就绪")
    return FileResponse(
        str(local),
        media_type="video/mp4",
        filename=f"hot_explosion_{task_id}_{variant_id}.mp4",
    )


@app.get("/api/hot-explosion-wan3/tasks")
async def hot_explosion_wan3_tasks_list():
    """列出所有爆款裂变任务（按创建时间倒序，最多 50 条）"""
    items = sorted(
        hot_explosion_wan3_tasks.values(),
        key=lambda t: t.get("created_at", ""),
        reverse=True,
    )[:50]
    return {
        "tasks": [
            {
                "task_id": t["task_id"],
                "status": t["status"],
                "phase": t.get("phase"),
                "video_path": t.get("video_path"),
                "success_count": t.get("success_count", 0),
                "failed_count": t.get("failed_count", 0),
                "total_variants": len(t.get("material_groups") or []),
                "created_at": t.get("created_at"),
                "finished_at": t.get("finished_at"),
                "error": t.get("error"),
            }
            for t in items
        ]
    }


@app.get("/hot-explosion-wan3", response_class=HTMLResponse)
async def hot_explosion_wan3_page():
    """爆款裂变：对标分析 → 商品适配 → 裂变审核台"""
    html_path = Path(settings.FRONTEND_DIR) / "hot_explosion_wan3.html"
    if not html_path.exists():
        raise HTTPException(404, "爆款裂变 wan3.0 页面未找到")
    return HTMLResponse(
        content=html_path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"},
    )


# ─────────────────────────────────────
# 钩子延展（基于 zexapi Omni 续接 API）
# ─────────────────────────────────────

# 任务存储：hook_extend_tasks[task_id] = {...}
hook_extend_tasks: dict = {}


class HookExtendSubmitRequest(BaseModel):
    """钩子延展请求

    工作流：
      Step 1: 裁剪原视频前 N 秒作为钩子段（默认 5 秒）
      Step 2: 上传钩子段到 CDN
      Step 3: 提交 omni_flash-10s 任务（video_url=钩子 + prompt=延展描述）
      Step 4: 轮询任务状态
      Step 5: 下载 10 秒延展视频

    输入：原视频（任意时长）+ 延展 prompt（描述钩子之后发生什么）
    输出：10 秒延展视频（前段衔接钩子，后段为延展内容）
    """
    video_path: str                       # 原视频本地路径（任意时长）
    extend_prompt: str                    # 延展描述（描述钩子之后发生什么）
    hook_duration: float = 5.0            # 钩子段时长（秒，默认 5，最大 10）
    hook_start: float = 0.0               # 钩子段起点（秒，默认 0 即原视频开头）
    image_urls: Optional[List[str]] = None  # 参考图片 URL 列表（可选）
    model: Optional[str] = None           # Omni 模型名（默认用配置里的 omni_flash-10s）
    output_subdir: Optional[str] = None   # 输出子目录名


@app.post("/api/hook-extend/submit")
async def hook_extend_submit(req: HookExtendSubmitRequest):
    """提交钩子延展任务"""
    # 🔴 花钱批准闸：hook_extend.run_pipeline 里 Step3 调 `omni_client.submit_omni_task`（omni_flash-10s）⇒ 真花钱，
    #    路由对得上网关的 omni-t2v，所以 purpose 给实路由、单价从价目表读。
    _spend_or_423("video.generate", "omni-t2v", "hook-extend 钩子延展")
    from backend.services import hook_extend

    task_id = f"hook_extend_{uuid.uuid4().hex[:8]}"
    sub = req.output_subdir or task_id
    out_dir = str(Path(settings.OUTPUT_DIR) / sub)
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    # 限制钩子段长度（≤ 10 秒）
    hook_dur = max(1.0, min(req.hook_duration, 10.0))

    task = {
        "task_id": task_id,
        "type": "hook_extend",
        "status": "INIT",
        "video_path": req.video_path,
        "extend_prompt": req.extend_prompt,
        "hook_duration": hook_dur,
        "hook_start": req.hook_start,
        "model": req.model,
        "output_dir": out_dir,
        "result": None,
        "error": None,
        "logs": [],
        "phases": {},
        "created_at": time.time(),
        "started_at": None,
        "completed_at": None,
    }
    hook_extend_tasks[task_id] = task

    def _log(msg: str):
        # 防御：避免后端重启后日志追加到不存在的 task
        t = hook_extend_tasks.get(task_id)
        if t is not None:
            t["logs"].append(msg)

    def _phase(phase: str, data):
        t = hook_extend_tasks.get(task_id)
        if t is not None:
            t["phases"][phase] = data

    async def _runner():
        t = hook_extend_tasks.get(task_id)
        if t is None:
            return
        t["status"] = "RUNNING"
        t["started_at"] = time.time()
        try:
            result = await hook_extend.run_pipeline(
                task_id=task_id,
                video_path=req.video_path,
                extend_prompt=req.extend_prompt,
                output_dir=out_dir,
                hook_duration=hook_dur,
                hook_start=req.hook_start,
                image_urls=req.image_urls,
                model=req.model,
                on_log=_log,
                on_phase=_phase,
            )
            t = hook_extend_tasks.get(task_id)
            if t is None:
                return
            t["result"] = result
            t["status"] = "SUCCESS"
            t["completed_at"] = time.time()
        except Exception as e:
            t = hook_extend_tasks.get(task_id)
            if t is None:
                return
            t["status"] = "FAILED"
            t["error"] = str(e)
            t["completed_at"] = time.time()
            print(f"[!] hook_extend {task_id} 失败: {e}")

    asyncio.create_task(_runner())

    return {"success": True, "task_id": task_id, "output_dir": out_dir}


@app.get("/api/hook-extend/status/{task_id}")
async def hook_extend_status(task_id: str):
    """查询钩子延展任务状态"""
    t = hook_extend_tasks.get(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    return {
        "success": True,
        "task_id": task_id,
        "status": t["status"],
        "phases": t.get("phases", {}),
        "logs": t.get("logs", []),
        "result": t.get("result"),
        "error": t.get("error"),
        "created_at": t.get("created_at"),
        "started_at": t.get("started_at"),
        "completed_at": t.get("completed_at"),
    }


@app.get("/api/hook-extend/result/{task_id}")
async def hook_extend_result(task_id: str):
    """下载钩子延展结果视频"""
    t = hook_extend_tasks.get(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    if t["status"] != "SUCCESS" or not t.get("result"):
        raise HTTPException(400, f"任务未完成（当前状态: {t['status']}）")
    result = t["result"]
    result_path = result.get("result_path")
    if not result_path or not Path(result_path).exists():
        raise HTTPException(404, "结果文件不存在")
    return FileResponse(
        result_path,
        media_type="video/mp4",
        filename=Path(result_path).name,
    )


agnes_image_tasks: dict = {}


class AgnesImageSubmitRequest(BaseModel):
    prompt: str
    size: str = "1K"
    ratio: str = "9:16"
    image_urls: List[str] = []


@app.post("/api/agnes-image/submit")
async def agnes_image_submit(req: AgnesImageSubmitRequest):
    # 🔴 花钱批准闸：这条直接 `agnes_image_client.create_image` 出图（按条计费、绕开网关）。
    #    purpose=default 是有据的：config.AGNES_IMAGE_MODEL = agnes-image-2.5-flash，
    #    与网关 image.generate/default 解析出的模型同名 ⇒ 单价能报实数（4 分/张），不用退到上限。
    _spend_or_423("image.generate", "default", "agnes-image 出图")
    prompt = (req.prompt or "").strip()
    if not prompt:
        raise HTTPException(400, "prompt 不能为空")
    size = (req.size or "1K").upper()
    if size not in {"1K", "2K", "3K", "4K"}:
        raise HTTPException(400, "size 必须是 1K / 2K / 3K / 4K")
    ratio = req.ratio or "9:16"
    if ratio not in {"1:1", "3:4", "4:3", "16:9", "9:16", "2:3", "3:2", "21:9"}:
        raise HTTPException(400, "ratio 不支持")
    images = [u.strip() for u in (req.image_urls or []) if u and u.strip()]
    task_id = f"agnes_image_{uuid.uuid4().hex[:8]}"
    out_dir = str(OUTPUT_DIR / "agnes_image" / task_id)
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    agnes_image_tasks[task_id] = {
        "task_id": task_id,
        "status": "QUEUED",
        "logs": [],
        "prompt": prompt,
        "size": size,
        "ratio": ratio,
        "created_at": time.time(),
    }

    async def _runner():
        from backend.services import agnes_image_client as agnes
        t = agnes_image_tasks.get(task_id)
        if t is None:
            return
        t["status"] = "RUNNING"
        t["started_at"] = time.time()

        def _log(msg: str) -> None:
            tt = agnes_image_tasks.get(task_id)
            if tt is not None:
                tt.setdefault("logs", []).append(msg)

        try:
            _log(f"提交 {settings.AGNES_IMAGE_MODEL} size={size} ratio={ratio}")
            created = await agnes.create_image(
                prompt,
                size=size,
                ratio=ratio,
                images=images or None,
            )
            items = created.get("data") or []
            url = (items[0].get("url") if items else None) or created.get("url")
            local = None
            if url:
                ext = ".png"
                if ".jpg" in url.lower() or ".jpeg" in url.lower():
                    ext = ".jpg"
                local = str(Path(out_dir) / f"result{ext}")
                await agnes.download_file(url, local)
                _log(f"已下载 {local}")
            t["status"] = "SUCCESS"
            t["result"] = {"url": url, "local_path": local, "raw": created}
            t["completed_at"] = time.time()
        except Exception as e:
            t["status"] = "FAILED"
            t["error"] = str(e)
            t["completed_at"] = time.time()
            _log(f"失败: {e}")

    asyncio.create_task(_runner())
    return {"success": True, "task_id": task_id}


@app.get("/api/agnes-image/status/{task_id}")
async def agnes_image_status(task_id: str):
    t = agnes_image_tasks.get(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    return {
        "success": True,
        "task_id": task_id,
        "status": t.get("status"),
        "logs": t.get("logs", []),
        "result": t.get("result"),
        "error": t.get("error"),
    }


agnes_flash_tasks: dict = {}


class AgnesFlashSubmitRequest(BaseModel):
    prompt: str
    seconds: str = "5"
    aspect_ratio: str = "9:16"
    image_urls: List[str] = []
    audio_urls: List[str] = []
    seed: Optional[int] = None


@app.post("/api/agnes-flash/submit")
async def agnes_flash_submit(req: AgnesFlashSubmitRequest):
    prompt = (req.prompt or "").strip()
    if not prompt:
        raise HTTPException(400, "prompt 不能为空")
    sec = str(req.seconds or "5")
    if sec not in {str(i) for i in range(4, 13)}:
        raise HTTPException(400, "seconds 必须是 4–12")
    images = [u.strip() for u in (req.image_urls or []) if u and u.strip()][:5]
    audios = [u.strip() for u in (req.audio_urls or []) if u and u.strip()][:3]
    task_id = f"agnes_flash_{uuid.uuid4().hex[:8]}"
    out_dir = str(OUTPUT_DIR / "agnes_flash" / task_id)
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    agnes_flash_tasks[task_id] = {
        "task_id": task_id,
        "status": "QUEUED",
        "logs": [],
        "prompt": prompt,
        "seconds": sec,
        "aspect_ratio": req.aspect_ratio,
        "created_at": time.time(),
    }

    async def _runner():
        from backend.services import agnes_video_client as agnes
        t = agnes_flash_tasks.get(task_id)
        if t is None:
            return
        t["status"] = "RUNNING"
        t["started_at"] = time.time()

        def _log(msg: str) -> None:
            tt = agnes_flash_tasks.get(task_id)
            if tt is not None:
                tt.setdefault("logs", []).append(msg)

        try:
            created = await agnes.create_flash_task(
                prompt,
                seconds=sec,
                aspect_ratio=req.aspect_ratio or "9:16",
                images=images,
                audios=audios,
                seed=req.seed,
            )
            video_id = created.get("video_id") or created.get("id")
            t["agnes_video_id"] = video_id
            t["create_resp"] = created
            _log(f"已创建 video_id={video_id}")
            done = await agnes.wait_flash_task(str(video_id), on_log=_log)
            url = done.get("url")
            local = None
            if url:
                local = str(Path(out_dir) / "result.mp4")
                await agnes.download_file(url, local)
                _log(f"已下载 {local}")
            t["status"] = "SUCCESS"
            t["result"] = {"url": url, "local_path": local, "raw": done}
            t["completed_at"] = time.time()
        except Exception as e:
            t["status"] = "FAILED"
            t["error"] = str(e)
            t["completed_at"] = time.time()
            _log(f"失败: {e}")

    asyncio.create_task(_runner())
    return {"success": True, "task_id": task_id}


@app.get("/api/agnes-flash/status/{task_id}")
async def agnes_flash_status(task_id: str):
    t = agnes_flash_tasks.get(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    return {
        "success": True,
        "task_id": task_id,
        "status": t.get("status"),
        "logs": t.get("logs", []),
        "result": t.get("result"),
        "error": t.get("error"),
        "agnes_video_id": t.get("agnes_video_id"),
    }


agnes_25_tasks: dict = {}


class Agnes25SubmitRequest(BaseModel):
    prompt: str
    seconds: str = "5"
    aspect_ratio: str = "16:9"
    size: str = "720P"
    image_urls: List[str] = []
    audio_urls: List[str] = []
    video_url: str = ""
    start_seconds: Optional[float] = None
    first_frame: str = ""
    last_frame: str = ""
    seed: Optional[int] = None


@app.post("/api/agnes-25/submit")
async def agnes_25_submit(req: Agnes25SubmitRequest):
    prompt = (req.prompt or "").strip()
    if not prompt:
        raise HTTPException(400, "prompt 不能为空")
    sec = str(req.seconds or "5")
    if sec not in {str(i) for i in range(4, 13)}:
        raise HTTPException(400, "seconds 必须是 4–12")
    size = (req.size or "720P").upper()
    if size not in {"720P", "1080P", "1K", "2K"}:
        raise HTTPException(400, "size 必须是 720P / 1080P / 1K / 2K")
    images = [u.strip() for u in (req.image_urls or []) if u and u.strip()][:8]
    audios = [u.strip() for u in (req.audio_urls or []) if u and u.strip()][:3]
    video_url = (req.video_url or "").strip()
    first_frame = (req.first_frame or "").strip()
    last_frame = (req.last_frame or "").strip()
    if video_url and (first_frame or last_frame):
        raise HTTPException(400, "参考视频与首尾帧不能同时使用")
    videos = []
    if video_url:
        item = {"url": video_url}
        if req.start_seconds is not None:
            item["start_seconds"] = float(req.start_seconds)
        videos.append(item)
    task_id = f"agnes_25_{uuid.uuid4().hex[:8]}"
    out_dir = str(OUTPUT_DIR / "agnes_25" / task_id)
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    agnes_25_tasks[task_id] = {
        "task_id": task_id,
        "status": "QUEUED",
        "logs": [],
        "prompt": prompt,
        "seconds": sec,
        "size": size,
        "aspect_ratio": req.aspect_ratio,
        "created_at": time.time(),
    }

    async def _runner():
        from backend.services import agnes_video_client as agnes
        t = agnes_25_tasks.get(task_id)
        if t is None:
            return
        t["status"] = "RUNNING"
        t["started_at"] = time.time()

        def _log(msg: str) -> None:
            tt = agnes_25_tasks.get(task_id)
            if tt is not None:
                tt.setdefault("logs", []).append(msg)

        try:
            created = await agnes.create_full_task(
                prompt,
                seconds=sec,
                aspect_ratio=req.aspect_ratio or "16:9",
                size=size,
                images=images,
                audios=audios,
                videos=videos or None,
                first_frame=first_frame or None,
                last_frame=last_frame or None,
                seed=req.seed,
            )
            video_id = created.get("video_id") or created.get("id")
            t["agnes_video_id"] = video_id
            t["create_resp"] = created
            _log(f"已创建 video_id={video_id} mode={created.get('mode') or ''}")
            done = await agnes.wait_full_task(str(video_id), on_log=_log)
            url = done.get("url")
            local = None
            if url:
                local = str(Path(out_dir) / "result.mp4")
                await agnes.download_file(url, local)
                _log(f"已下载 {local}")
            t["status"] = "SUCCESS"
            t["result"] = {"url": url, "local_path": local, "raw": done}
            t["completed_at"] = time.time()
        except Exception as e:
            t["status"] = "FAILED"
            t["error"] = str(e)
            t["completed_at"] = time.time()
            _log(f"失败: {e}")

    asyncio.create_task(_runner())
    return {"success": True, "task_id": task_id}


@app.get("/api/agnes-25/status/{task_id}")
async def agnes_25_status(task_id: str):
    t = agnes_25_tasks.get(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    return {
        "success": True,
        "task_id": task_id,
        "status": t.get("status"),
        "logs": t.get("logs", []),
        "result": t.get("result"),
        "error": t.get("error"),
        "agnes_video_id": t.get("agnes_video_id"),
    }


@app.get("/api/hook-extend/tasks")
async def hook_extend_list_tasks():
    """列出所有钩子延展任务（按创建时间倒序，最多 50 条）"""
    items = list(hook_extend_tasks.values())
    items.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    items = items[:50]
    return {
        "success": True,
        "tasks": [
            {
                "task_id": t["task_id"],
                "status": t["status"],
                "created_at": t.get("created_at"),
                "completed_at": t.get("completed_at"),
                "extend_prompt": (t.get("extend_prompt") or "")[:80],
                "hook_duration": t.get("hook_duration"),
                "result_duration": (t.get("result") or {}).get("duration"),
            }
            for t in items
        ],
    }


@app.get("/hook-extend", response_class=HTMLResponse)
async def hook_extend_page():
    """钩子延展页面 - 原视频前 N 秒作钩子 + 延展 prompt → 10s 延展视频"""
    html_path = Path(settings.FRONTEND_DIR) / "hook_extend.html"
    if not html_path.exists():
        raise HTTPException(404, "钩子延展页面未找到")
    return HTMLResponse(
        content=html_path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"},
    )


# ─────────────────────────────────────
# 爆款裂变 v2 工作流（smart_fission）
# ─────────────────────────────────────

@app.get("/smart-fission")
async def smart_fission_page():
    """旧入口收口（M5）：302 → /hot-explosion-wan3，杜绝旧页传老参数（去 video_path 契约同次上线）。"""
    return RedirectResponse(url="/hot-explosion-wan3", status_code=302)


class SmartFissionStartRequest(BaseModel):
    """启动爆款裂变 v2 请求（M5 契约：去 video_path——解析在知识库入库阶段完成）"""
    model_config = ConfigDict(extra="forbid")  # 旧页传老参数（video_path 等）必须 422，不许静默吞掉

    product_image_paths: list[str]
    customer_request: str = ""
    variant_count: int = 3
    target_duration: float = 30.0  # 15 / 30 两档（30s 待小样本定档）
    product_name: str = "目标产品"
    product_facts: str | None = None
    model_image_path: str | None = None
    scene_image_path: str | None = None
    account_id: str = ""
    account_skill_version: int | None = None


@app.post("/api/smart-fission/start")
async def smart_fission_start(req: SmartFissionStartRequest):
    """启动新任务（Step 3-4 一气呵成，到 PENDING_REVIEW 停下）"""
    from backend.services.smart_fission.workflow import start_workflow
    if int(req.target_duration) not in (15, 30):
        raise HTTPException(400, f"target_duration 只放行 15/30 秒（收到 {req.target_duration}）")
    if not req.product_image_paths:
        raise HTTPException(400, "产品图必填")
    task_id = await start_workflow(
        product_image_paths=req.product_image_paths,
        customer_request=req.customer_request,
        variant_count=req.variant_count,
        target_duration=req.target_duration,
        product_name=req.product_name,
        product_facts=req.product_facts,
        model_image_path=req.model_image_path,
        scene_image_path=req.scene_image_path,
        account_id=req.account_id,
        account_skill_version=req.account_skill_version,
    )
    return {"task_id": task_id, "status": "RUNNING", "stage": "INIT"}


@app.get("/api/smart-fission/{task_id}/status")
async def smart_fission_status(task_id: str):
    """查询任务状态"""
    from backend.services.task_store import get as get_task, get_logs
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, f"任务不存在：{task_id}")
    intermediate = task.get("intermediate", {})
    if isinstance(intermediate, str):
        import json as _json
        intermediate = _json.loads(intermediate) if intermediate else {}
    logs = get_logs(task_id, limit=200)
    return {
        "task_id": task_id,
        "status": task.get("status", ""),
        "stage": task.get("stage", ""),
        "progress": task.get("progress", 0),
        "error": task.get("error", ""),
        "intermediate": intermediate,
        "logs": [{"ts": l.get("ts"), "msg": l.get("msg")} for l in logs],
    }


@app.get("/api/smart-fission/{task_id}/variants")
async def smart_fission_variants(task_id: str):
    """获取变体方案（含 shots + h3_prompt + grid_prompt + grid_path）"""
    from backend.services.task_store import get as get_task
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, f"任务不存在：{task_id}")
    intermediate = task.get("intermediate", {})
    if isinstance(intermediate, str):
        import json as _json
        intermediate = _json.loads(intermediate) if intermediate else {}
    variants_plan = intermediate.get("variants_plan", {})
    grids = intermediate.get("grids", {})
    analysis = intermediate.get("analysis", {})
    return {
        "analysis": analysis,
        "variants": variants_plan.get("variants", []),
        "grids": grids.get("grids", []),
        "cross_review": variants_plan.get("cross_review", {}),
    }


class SmartFissionEditRequest(BaseModel):
    """用户编辑变体请求"""
    edited_variants: list[dict]


@app.post("/api/smart-fission/{task_id}/edit")
async def smart_fission_edit(task_id: str, req: SmartFissionEditRequest):
    """用户编辑变体（修改 shots / 删除变体 / 修改 h3_prompt）"""
    from backend.services.smart_fission.workflow import apply_user_edits
    plan = await apply_user_edits(task_id, req.edited_variants)
    return {"status": "ok", "variants": plan.get("variants", [])}


class SmartFissionReorchestrateRequest(BaseModel):
    """单条变体重新编排请求"""
    variant_id: str
    edit_request: str


@app.post("/api/smart-fission/{task_id}/reorchestrate")
async def smart_fission_reorchestrate(task_id: str, req: SmartFissionReorchestrateRequest):
    """针对单条变体重新编排（保留满意部分）"""
    from backend.services.smart_fission.workflow import reorchestrate_variant
    new_variant = await reorchestrate_variant(task_id, req.variant_id, req.edit_request)
    return {"status": "ok", "variant": new_variant}


class SmartFissionGenerateRequest(BaseModel):
    """触发 Wan3.0 生成请求"""
    variant_ids: list[str] | None = None  # None=全部


@app.post("/api/smart-fission/{task_id}/generate")
async def smart_fission_generate(task_id: str, req: SmartFissionGenerateRequest):
    """用户审核通过后触发 Step 6 Wan3.0 完整直出"""
    from backend.services.smart_fission.workflow import trigger_step6
    await trigger_step6(task_id, req.variant_ids)
    return {"status": "ok", "message": "Step 6 已触发"}


class SmartFissionConfirmRequest(BaseModel):
    variant_id: str
    confirmed: bool = True


@app.post("/api/smart-fission/{task_id}/confirm")
async def smart_fission_confirm(task_id: str, req: SmartFissionConfirmRequest):
    """确认 / 取消确认一条变体方案"""
    from backend.services.smart_fission.workflow import set_variant_confirmed
    data = await set_variant_confirmed(task_id, req.variant_id, req.confirmed)
    return {"status": "ok", **data}


class SmartFissionRegenGridRequest(BaseModel):
    variant_id: str


@app.post("/api/smart-fission/{task_id}/regenerate-grid")
async def smart_fission_regen_grid(task_id: str, req: SmartFissionRegenGridRequest):
    """重生成单条变体宫格图"""
    from backend.services.smart_fission.workflow import regenerate_variant_grid
    grid = await regenerate_variant_grid(task_id, req.variant_id)
    return {"status": "ok", "grid": grid}


class SmartFissionIncrementalRequest(BaseModel):
    """增量裂变请求"""
    additional_count: int


@app.post("/api/smart-fission/{task_id}/incremental")
async def smart_fission_incremental(task_id: str, req: SmartFissionIncrementalRequest):
    """增量裂变：在已有变体基础上再加 N 条"""
    from backend.services.smart_fission.workflow import incremental_fission
    new_plan = await incremental_fission(task_id, req.additional_count)
    return {"status": "ok", "new_variants": new_plan.get("variants", [])}


@app.get("/api/smart-fission/list")
async def smart_fission_list(limit: int = 20):
    """列出最近的 smart_fission 任务"""
    from backend.services.task_store import list_by_type
    items = list_by_type("smart_fission", limit=limit)
    return {
        "tasks": [
            {
                "task_id": t.get("task_id"),
                "status": t.get("status"),
                "stage": t.get("stage"),
                "progress": t.get("progress"),
                "created_at": t.get("created_at"),
                "updated_at": t.get("updated_at"),
                "error": t.get("error"),
            }
            for t in items
        ]
    }


# ─────────────────────────────────────
# 账号知识库 API（M1：视频 RAG 入库，MiMo 解析 + GPT 营销结构 + 本地 ASR）
# ─────────────────────────────────────

class KbIngestVideo(BaseModel):
    path: str
    title: str = ""
    play_count: int = 0
    source: str = "upload"  # upload / link（link 通道 M1b 可选，默认关闭）


class KbIngestRequest(BaseModel):
    account_id: str
    videos: List[KbIngestVideo]


@app.post("/api/kb/ingest")
async def kb_ingest(req: KbIngestRequest):
    """批量入库并逐条解析（断点续跑：重跑跳过 status='parsed' 条目）"""
    from backend.services.kb.ingest import start_ingest
    try:
        task_id = await start_ingest(req.account_id, [v.dict() for v in req.videos])
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"task_id": task_id, "status": "RUNNING", "stage": "KB_INGEST"}


@app.get("/api/kb/ingest/{task_id}/status")
async def kb_ingest_status(task_id: str):
    """入库任务进度 + 日志 + 调用账目"""
    from backend.services.task_store import get as get_task, get_logs
    from backend.services.kb import db as kb_db
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, f"任务不存在：{task_id}")
    intermediate = task.get("intermediate", {})
    if isinstance(intermediate, str):
        import json as _json
        intermediate = _json.loads(intermediate) if intermediate else {}
    logs = get_logs(task_id, limit=200)
    return {
        "task_id": task_id,
        "status": task.get("status", ""),
        "stage": task.get("stage", ""),
        "progress": task.get("progress", 0),
        "error": task.get("error", ""),
        "intermediate": intermediate,
        "calls": kb_db.calls_summary(task_id),
        "logs": [{"ts": l.get("ts"), "msg": l.get("msg")} for l in logs],
    }


@app.get("/api/kb/{account_id}/videos")
async def kb_videos(account_id: str):
    """账号视频台账（含解析状态，驱动断点续跑展示）"""
    from backend.services.kb import db as kb_db
    kb_db.init_db()
    items = kb_db.videos_by_account(account_id)
    return {"account_id": account_id, "videos": items, "counts": kb_db.video_counts(account_id)}


@app.get("/api/kb/{account_id}/analysis")
async def kb_analysis(account_id: str):
    """逐条解析结果（shot_analysis / 营销结构 / 案例字段）"""
    from backend.services.kb import db as kb_db
    kb_db.init_db()
    return {"account_id": account_id, "items": kb_db.analyses_by_account(account_id)}


@app.get("/api/kb/{account_id}/status")
async def kb_account_status(account_id: str):
    """账号知识库总览：条目统计 + 解析统计 + 全链路调用账目"""
    from backend.services.kb import db as kb_db
    kb_db.init_db()
    return {
        "account_id": account_id,
        "counts": kb_db.video_counts(account_id),
        "calls": kb_db.calls_summary(),
        "has_profile": bool(kb_db.profile_latest(account_id)),
        "skill_latest": kb_db.skill_latest(account_id),
    }


# ─────────────────────────────────────
# 账号知识库 API（M2/M5：聚合蒸馏 + skill 在线审核）
# ─────────────────────────────────────

@app.get("/api/kb/accounts")
async def kb_accounts():
    """全部账号清单（视频条数 + 最新 skill 版本，驱动前端账号/skill 快照选择）"""
    from backend.services.kb import db as kb_db
    return {"accounts": kb_db.accounts_list()}


class KbTaskRequest(BaseModel):
    """聚合/蒸馏请求（task_id 可选，不传则后端建任务号）"""
    task_id: str = ""


def _kb_task_id(prefix: str, req: KbTaskRequest | None) -> str:
    task_id = (req.task_id if req else "") or ""
    if not task_id:
        task_id = f"{prefix}-{uuid.uuid4().hex[:8]}"
        from backend.services.task_store import create as _create
        _create(task_id=task_id, task_type=prefix, status="RUNNING",
                stage="KB_AGGREGATE", params={})
    return task_id


def _kb_task_failed(task_id: str, err: str) -> None:
    """kb 聚合/蒸馏异常收口：任务置 FAILED，防僵尸 RUNNING。"""
    from backend.services import task_store
    try:
        task_store.update(task_id, status="FAILED", error=str(err)[:500])
    except Exception:
        pass


@app.post("/api/kb/{account_id}/aggregate")
async def kb_aggregate(account_id: str, req: KbTaskRequest | None = None):
    """账号画像聚合（≥3 条已解析结果；画像版本 +1 落库）"""
    from backend.services.kb.aggregate import aggregate_account
    task_id = _kb_task_id("kb_aggregate", req)
    try:
        result = await aggregate_account(task_id, account_id)
    except ValueError as e:
        _kb_task_failed(task_id, str(e))
        raise HTTPException(400, str(e))
    except Exception as e:
        _kb_task_failed(task_id, str(e))
        raise HTTPException(500, f"账号画像聚合失败：{e}")
    return {"task_id": task_id, "status": "DONE",
            "version": result.get("version"), "stats": result.get("stats")}


@app.post("/api/kb/{account_id}/distill")
async def kb_distill(account_id: str, req: KbTaskRequest | None = None):
    """两阶段 Map-Reduce 蒸馏 → SKILL.md（含 contract: 段，过 skill_contract 形状闸）"""
    from backend.services.kb.distill import distill_skill
    task_id = _kb_task_id("kb_distill", req)
    try:
        result = await distill_skill(task_id, account_id)
    except ValueError as e:
        _kb_task_failed(task_id, str(e))
        raise HTTPException(400, str(e))
    except Exception as e:
        _kb_task_failed(task_id, str(e))
        raise HTTPException(500, f"账号蒸馏失败：{e}")
    return {"task_id": task_id, "status": "DONE",
            "version": result.get("version"),
            "skill_md_path": result.get("skill_md_path"),
            "stats": result.get("stats")}


@app.get("/api/kb/skill/{account_id}/{version}")
async def kb_skill_get(account_id: str, version: int = 0):
    """skill 版本详情 + SKILL.md 全文（version=0 取最新）"""
    from backend.services.kb import db as kb_db
    kb_db.init_db()
    row = kb_db.skill_get(account_id, version) if version > 0 else kb_db.skill_latest(account_id)
    if not row:
        raise HTTPException(404, f"skill 不存在：{account_id} v{version or 'latest'}")
    content = ""
    p = Path(row.get("skill_md_path", ""))
    if p.exists():
        content = p.read_text(encoding="utf-8")
    return {**row, "content": content}


class KbSkillSaveRequest(BaseModel):
    """人工审核修改 skill（写回 SKILL.md 并标 manual_edited，不被后续蒸馏覆盖）"""
    account_id: str
    version: int
    content: str


@app.post("/api/kb/skill/save")
async def kb_skill_save(req: KbSkillSaveRequest):
    """skill 在线审核保存：写回文件 + manual_edited；合同形状问题如实回报（人工有最终决定权）"""
    from backend.services.kb import db as kb_db
    kb_db.init_db()
    row = kb_db.skill_get(req.account_id, req.version)
    if not row:
        raise HTTPException(404, f"skill 不存在：{req.account_id} v{req.version}")
    p = Path(row.get("skill_md_path", ""))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(req.content, encoding="utf-8")
    kb_db.skill_save(req.account_id, req.version, str(p),
                     row.get("rule_candidates") or [], status="manual_edited")

    problems: List[str] = []
    try:
        from backend.skill_contract import parse_contract, contract_shape_problems
        contract = parse_contract(req.content)
        problems = contract_shape_problems(contract) if contract else ["未找到 contract: 段（生成出片时合同回执闸可能拒绝）"]
    except Exception as e:
        problems = [f"合同解析失败：{e}"]
    return {"status": "ok", "version": req.version, "skill_md_path": str(p),
            "skill_status": "manual_edited", "contract_problems": problems}


@app.get("/account-kb", response_class=HTMLResponse)
async def account_kb_page():
    """账号知识库工作台（M5）：视频库双通道入库 → 蒸馏分析 → skill 在线审核"""
    html_path = Path(settings.FRONTEND_DIR) / "account_kb.html"
    if not html_path.exists():
        raise HTTPException(404, "账号知识库页面未找到")
    return HTMLResponse(
        content=html_path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"},
    )


# ─────────────────────────────────────
# 启动
# ─────────────────────────────────────

# ─────────────────────────────────────
# 洋子剧本知识库：本地 SQLite + FTS5 检索
# ─────────────────────────────────────

class ScreenplayKBSearchRequest(BaseModel):
    """检索画像：参考洋子 film-knowledge-bridge 的 profile 格式。"""
    stage: str = "02-创作流"
    question: str = ""
    queries: List[str] = []
    lane: str = "reference"
    mode: str = "any"
    path_contains: Optional[str] = None
    path_excludes: Optional[List[str]] = None
    festival: Optional[str] = None
    top_k: int = 10
    selected_documents: Optional[List[str]] = None
    deep_read_selected: bool = False


@app.get("/screenplay-kb", response_class=HTMLResponse)
async def screenplay_kb_page():
    """剧本知识库工作台"""
    html_path = Path(settings.FRONTEND_DIR) / "screenplay_kb.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.get("/api/screenplay-kb/info")
async def screenplay_kb_info():
    """知识库元数据 + 统计"""
    import json
    config_path = Path(__file__).parent / "backend" / "knowledge-base.json"
    if not config_path.exists():
        return {"success": False, "error": "knowledge-base.json 不存在"}
    config = json.loads(config_path.read_text(encoding="utf-8"))
    database = Path(config["database"])
    if not database.exists():
        return {"success": False, "error": f"SQLite 索引不存在：{database}"}

    import sqlite3
    conn = sqlite3.connect(database)
    try:
        documents = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        sample = conn.execute(
            "SELECT name, path FROM documents ORDER BY rowid LIMIT 5"
        ).fetchall()
    finally:
        conn.close()

    return {
        "success": True,
        "data": {
            "name": config.get("name", ""),
            "root": config.get("root", ""),
            "database": config.get("database", ""),
            "indexed_documents": documents,
            "indexed_chunks": chunks,
            "generated_at": config.get("generated_at", ""),
            "sample_documents": [{"name": n, "path": p} for n, p in sample],
        },
    }


@app.post("/api/screenplay-kb/search")
async def screenplay_kb_search(req: ScreenplayKBSearchRequest):
    """调用洋子 retrieve() 检索剧本资料"""
    import json
    import logging
    logger = logging.getLogger("screenplay-kb")

    try:
        config_path = Path(__file__).parent / "backend" / "knowledge-base.json"
        if not config_path.exists():
            return {"success": False, "error": "knowledge-base.json 不存在"}
        config = json.loads(config_path.read_text(encoding="utf-8"))
        database = Path(config["database"])
        if not database.exists():
            return {"success": False, "error": f"SQLite 索引不存在：{database}"}

        queries_text = req.queries if req.queries else ([req.question] if req.question else [])
        if not queries_text:
            return {"success": False, "error": "请提供 question 或 queries"}

        profile = {
            "stage": req.stage,
            "question": req.question,
            "queries": [
                {
                    "lane": req.lane,
                    "text": q,
                    "mode": req.mode,
                    "weight": 1.0,
                    "path_contains": req.path_contains,
                    "path_excludes": req.path_excludes or [],
                    "festival": req.festival or "",
                    "include_journals": True,
                    "limit": req.top_k,
                }
                for q in queries_text
            ],
            "deep_read_selected": req.deep_read_selected,
        }
        if req.selected_documents:
            profile["selected_documents"] = req.selected_documents

        import sys
        bridge_dir = Path(__file__).parent / "backend" / "screenplay_kb_bridge"
        if str(bridge_dir) not in sys.path:
            sys.path.insert(0, str(bridge_dir))
        from retrieve_stage_sources import retrieve

        results = await asyncio.to_thread(retrieve, database, config, profile)

        trimmed = {}
        for lane, items in results.items():
            trimmed[lane] = items[: req.top_k]

        return {
            "success": True,
            "data": {
                "profile": profile,
                "results": trimmed,
                "total_candidates": sum(len(items) for items in results.values()),
            },
        }
    except Exception as e:
        import traceback
        logger.error(f"剧本知识库检索失败：{e}\n{traceback.format_exc()}")
        return {"success": False, "error": str(e)}


# ── 剧本知识库：候选卡导出 ──

_kb_index_tasks: dict = {}  # task_id -> {status, output, error, started_at}


@app.post("/api/screenplay-kb/export-card")
async def screenplay_kb_export_card(req: ScreenplayKBSearchRequest):
    """检索并生成 Markdown 候选卡/证据卡"""
    import json
    import logging
    import tempfile
    logger = logging.getLogger("screenplay-kb")

    try:
        config_path = Path(__file__).parent / "backend" / "knowledge-base.json"
        if not config_path.exists():
            return {"success": False, "error": "knowledge-base.json 不存在"}
        config = json.loads(config_path.read_text(encoding="utf-8"))
        database = Path(config["database"])
        if not database.exists():
            return {"success": False, "error": f"SQLite 索引不存在：{database}"}

        queries_text = req.queries if req.queries else ([req.question] if req.question else [])
        if not queries_text:
            return {"success": False, "error": "请提供 question 或 queries"}

        profile = {
            "stage": req.stage,
            "question": req.question,
            "problem": req.question,
            "queries": [
                {
                    "lane": req.lane,
                    "text": q,
                    "mode": req.mode,
                    "weight": 1.0,
                    "path_contains": req.path_contains,
                    "path_excludes": req.path_excludes or [],
                    "festival": req.festival or "",
                    "include_journals": True,
                    "limit": req.top_k,
                }
                for q in queries_text
            ],
            "deep_read_selected": req.deep_read_selected,
        }
        if req.selected_documents:
            profile["selected_documents"] = req.selected_documents

        import sys
        bridge_dir = Path(__file__).parent / "backend" / "screenplay_kb_bridge"
        if str(bridge_dir) not in sys.path:
            sys.path.insert(0, str(bridge_dir))
        from retrieve_stage_sources import retrieve, write_card

        results = await asyncio.to_thread(retrieve, database, config, profile)

        # 限制每 lane 返回数量
        for lane in results:
            results[lane] = results[lane][: req.top_k]

        # 生成 Markdown 卡
        card_path = Path(tempfile.gettempdir()) / f"screenplay_kb_card_{req.stage}.md"
        write_card(card_path, profile, results)
        card_content = card_path.read_text(encoding="utf-8")

        return {
            "success": True,
            "data": {
                "card": card_content,
                "stage": req.stage,
                "total_candidates": sum(len(items) for items in results.values()),
            },
        }
    except Exception as e:
        import traceback
        logger.error(f"候选卡导出失败：{e}\n{traceback.format_exc()}")
        return {"success": False, "error": str(e)}


# ── 剧本知识库：文档内容查看 ──

def _fetch_document_chunks(config: dict, name: str, path: str, page: int) -> dict:
    """同步函数：从 SQLite 读取文档分块（在线程池中调用）"""
    import sqlite3
    import sys
    bridge_dir = Path(__file__).parent / "backend" / "screenplay_kb_bridge"
    if str(bridge_dir) not in sys.path:
        sys.path.insert(0, str(bridge_dir))
    from retrieve_stage_sources import resolve_source_path

    database = Path(config["database"])
    conn = sqlite3.connect(database)
    try:
        if path:
            row = conn.execute(
                "SELECT id, name, path, pages, status FROM documents WHERE path = ?", (path,)
            ).fetchone()
        elif name:
            row = conn.execute(
                "SELECT id, name, path, pages, status FROM documents WHERE name = ?", (name,)
            ).fetchone()
        else:
            return {"success": False, "error": "请提供 name 或 path 参数"}

        if not row:
            return {"success": False, "error": f"未找到文档：{name or path}"}

        doc_id, doc_name, doc_path, doc_pages, doc_status = row

        # 获取该文档所有分块（chunks_content 是 FTS5 影子表，列名为 c0~c5）
        # c0=document_id, c1=path, c2=name, c3=page, c4=chunk_no, c5=text
        chunks = conn.execute(
            "SELECT c3, c4, c5 FROM chunks_content "
            "WHERE c0 = ? ORDER BY CAST(c3 AS INTEGER), CAST(c4 AS INTEGER)",
            (doc_id,),
        ).fetchall()

        if not chunks:
            return {
                "success": True,
                "data": {
                    "name": doc_name,
                    "path": doc_path,
                    "pages": doc_pages,
                    "status": doc_status,
                    "total_chunks": 0,
                    "chunks": [],
                },
            }

        per_page = 10
        total_chunks = len(chunks)
        start = page * per_page
        end = start + per_page
        page_chunks = chunks[start:end]

        source_path, source_exists = resolve_source_path(str(doc_path), config)

        return {
            "success": True,
            "data": {
                "name": doc_name,
                "path": source_path,
                "source_exists": source_exists,
                "pages": doc_pages,
                "status": doc_status,
                "total_chunks": total_chunks,
                "current_page": page,
                "total_pages": (total_chunks + per_page - 1) // per_page,
                "chunks": [
                    {"page": c[0], "chunk_no": c[1], "text": c[2]}
                    for c in page_chunks
                ],
            },
        }
    finally:
        conn.close()


@app.get("/api/screenplay-kb/document")
async def screenplay_kb_document(name: str = "", path: str = "", page: int = 0):
    """查看指定文档的全文内容（分页返回）"""
    import json

    try:
        config_path = Path(__file__).parent / "backend" / "knowledge-base.json"
        if not config_path.exists():
            return {"success": False, "error": "knowledge-base.json 不存在"}
        config = json.loads(config_path.read_text(encoding="utf-8"))
        database = Path(config["database"])
        if not database.exists():
            return {"success": False, "error": f"SQLite 索引不存在：{database}"}

        return await asyncio.to_thread(_fetch_document_chunks, config, name, path, page)
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── 剧本知识库：索引刷新 ──

@app.post("/api/screenplay-kb/refresh-index")
async def screenplay_kb_refresh_index():
    """触发增量索引更新（异步执行）"""
    import uuid
    import threading
    import subprocess as _sp

    task_id = str(uuid.uuid4())[:8]
    _kb_index_tasks[task_id] = {
        "status": "running",
        "output": "",
        "error": "",
        "started_at": datetime.now().isoformat(),
    }

    def _run_index():
        try:
            bridge_dir = Path(__file__).parent / "backend" / "screenplay_kb_bridge"
            script = bridge_dir / "update_knowledge_index.py"
            result = _sp.run(
                [sys.executable, "-X", "utf8", str(script)],
                capture_output=True,
                text=True,
                timeout=600,
                encoding="utf-8",
                errors="replace",
            )
            _kb_index_tasks[task_id]["output"] = result.stdout or ""
            if result.returncode != 0:
                _kb_index_tasks[task_id]["status"] = "failed"
                _kb_index_tasks[task_id]["error"] = result.stderr or "未知错误"
            else:
                _kb_index_tasks[task_id]["status"] = "done"
        except Exception as e:
            _kb_index_tasks[task_id]["status"] = "failed"
            _kb_index_tasks[task_id]["error"] = str(e)

    thread = threading.Thread(target=_run_index, daemon=True)
    thread.start()

    return {"success": True, "data": {"task_id": task_id, "status": "running"}}


@app.get("/api/screenplay-kb/refresh-status/{task_id}")
async def screenplay_kb_refresh_status(task_id: str):
    """查询索引刷新任务状态"""
    if task_id not in _kb_index_tasks:
        return {"success": False, "error": "任务不存在"}
    task = _kb_index_tasks[task_id]
    return {"success": True, "data": task}


# ═══════════════════════════════════════
# 视频换脸 (RunningHub AI App)
# ═══════════════════════════════════════

face_swap_tasks: dict = {}


@app.get("/face-swap", response_class=HTMLResponse)
async def face_swap_page():
    """视频换脸页面"""
    html_path = Path(settings.FRONTEND_DIR) / "face_swap.html"
    if not html_path.exists():
        raise HTTPException(404, "视频换脸页面未找到")
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


class FaceSwapSubmitRequest(BaseModel):
    video_path: str
    face_image_path: str
    duration: Optional[int] = 8
    width: Optional[int] = 576
    height: Optional[int] = 1024
    instance_type: Optional[str] = "plus"
    output_subdir: Optional[str] = None


@app.post("/api/face-swap/submit")
async def face_swap_submit(req: FaceSwapSubmitRequest):
    """提交视频换脸任务"""
    import uuid
    from backend.services.video_face_swap import run_face_swap

    if not req.video_path or not Path(req.video_path).exists():
        raise HTTPException(400, f"视频不存在: {req.video_path}")
    if not req.face_image_path or not Path(req.face_image_path).exists():
        raise HTTPException(400, f"人脸图不存在: {req.face_image_path}")

    sub = req.output_subdir or f"face_swap_{uuid.uuid4().hex[:8]}"
    out_dir = OUTPUT_DIR / sub
    out_dir.mkdir(parents=True, exist_ok=True)

    task_id = f"fs_{uuid.uuid4().hex[:12]}"
    face_swap_tasks[task_id] = {
        "task_id": task_id,
        "status": "PENDING",
        "phase": "init",
        "created_at": datetime.now().isoformat(),
        "logs": [],
        "video_path": req.video_path,
        "face_image_path": req.face_image_path,
        "output_dir": str(out_dir),
    }

    async def _runner():
        t = face_swap_tasks[task_id]
        t["status"] = "RUNNING"
        t["started_at"] = datetime.now().isoformat()

        def _log(msg: str):
            t["logs"].append({"ts": datetime.now().isoformat(), "msg": msg})

        try:
            _log("▶ 开始换脸...")
            t["phase"] = "uploading"
            result = await run_face_swap(
                video_path=req.video_path,
                face_image_path=req.face_image_path,
                output_dir=str(out_dir),
                duration=req.duration,
                width=req.width,
                height=req.height,
                instance_type=req.instance_type or "plus",
                on_log=_log,
            )
            t["status"] = "SUCCESS"
            t["output_path"] = result.get("output_path")
            t["duration"] = result.get("duration")
            t["finished_at"] = datetime.now().isoformat()
            _log(f"✓ 完成 → {result.get('output_path')}")
        except Exception as e:
            t["status"] = "FAILED"
            t["error"] = str(e)
            t["finished_at"] = datetime.now().isoformat()
            _log(f"✗ 失败: {e}")

    asyncio.create_task(_runner())
    return {"success": True, "task_id": task_id}


@app.get("/api/face-swap/status/{task_id}")
async def face_swap_status(task_id: str):
    """查询换脸任务状态"""
    t = face_swap_tasks.get(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    return {
        "task_id": t["task_id"],
        "status": t["status"],
        "phase": t.get("phase"),
        "error": t.get("error"),
        "output_path": t.get("output_path"),
        "duration": t.get("duration"),
        "logs": t["logs"][-20:],
    }


@app.get("/api/face-swap/result/{task_id}")
async def face_swap_result(task_id: str):
    """下载换脸后的视频"""
    t = face_swap_tasks.get(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    if t["status"] != "SUCCESS":
        raise HTTPException(400, f"任务状态: {t['status']}")
    local = t.get("output_path")
    if not local or not Path(local).exists():
        raise HTTPException(404, "结果文件不存在")
    return FileResponse(
        str(local),
        media_type="video/mp4",
        filename=f"face_swap_{task_id}.mp4",
    )


# ==================== 抖音账号批量下载工作台 (/douyin-batch) ====================
# 零模型消耗的链路：只翻列表 + 拉 mp4，不进花钱那道闸（与 womenswear 同侧）。
BATCH_TASK_TYPE = "douyin_batch"
douyin_batch_tasks: dict = {}


def _batch_touch(task_id: str, **fields):
    t = douyin_batch_tasks.get(task_id)
    if t:
        t.update(fields)


def _batch_log(task_id: str, msg: str, level: str = "info"):
    t = douyin_batch_tasks.get(task_id)
    if t is not None:
        logs = t.setdefault("logs", [])
        logs.append({"ts": time.time(), "msg": str(msg)})
        del logs[:-400]
    try:
        from backend.services import task_store
        task_store.add_log(task_id, str(msg), level=level)
    except Exception:
        pass


def _batch_persist(task_id: str):
    """逐条都写库会把 1300 条的台账变成 IO 瓶颈，按检查点镜像；内存态才是活的那一份"""
    t = douyin_batch_tasks.get(task_id)
    if not t:
        return
    try:
        from backend.services import task_store
        task_store.update(
            task_id,
            status=t.get("status"),
            progress=t.get("progress", 0),
            stage=t.get("stage", ""),
            error=t.get("error") or "",
            intermediate={"feed": t.get("intermediate", {}).get("feed", {}),
                          "items": t.get("items", [])},
            extra=t.get("extra", {}),
            outputs=t.get("outputs", []),
        )
    except Exception:
        pass


def _batch_rehydrate(task_id: str) -> Optional[dict]:
    """服务重启后内存丢了，从 task_store 把任务捞回来（列表/进度都能回看）"""
    if task_id in douyin_batch_tasks:
        return douyin_batch_tasks[task_id]
    try:
        from backend.services import task_store
        t_db = task_store.get(task_id)
    except Exception:
        t_db = None
    if not t_db or t_db.get("task_type") != BATCH_TASK_TYPE:
        return None
    inter = t_db.get("intermediate", {}) or {}
    t = {
        "task_id": task_id,
        "status": t_db.get("status", "FAILED"),
        "progress": t_db.get("progress", 0),
        "stage": t_db.get("stage", ""),
        "error": t_db.get("error"),
        "kind": t_db.get("extra", {}).get("kind", "download"),
        "params": t_db.get("params", {}),
        "items": inter.get("items", []) or [],
        "intermediate": {"feed": inter.get("feed", {}) or {}},
        "extra": t_db.get("extra", {}),
        "outputs": t_db.get("outputs", []),
        "logs": [],
    }
    douyin_batch_tasks[task_id] = t
    return t


def _batch_counts(items: list) -> dict:
    return {
        "total": len(items),
        "done": sum(1 for i in items if i.get("dl_status") == "done"),
        "failed": sum(1 for i in items if i.get("dl_status") == "failed"),
        "skipped": sum(1 for i in items if i.get("dl_status") == "skipped"),
        "pending": sum(1 for i in items if not i.get("dl_status")),
    }


class DouyinBatchCookieRequest(BaseModel):
    cookie: str


class DouyinBatchPreviewRequest(BaseModel):
    account_input: str
    want: int = 0


class DouyinBatchRunRequest(BaseModel):
    """run 只认 preview 出来的那张清单 —— 看到多少条就下多少条，不在背后重翻一次列表"""
    sec_uid: str
    nickname: str = ""
    items: list
    target_dir: str = ""
    gap_sec: float = 2.0


class DouyinBatchDeleteRequest(BaseModel):
    """真删下载目录内的视频文件（勾选即真删，拦住无效视频进蒸馏）"""
    task_id: str = ""
    files: List[str]


async def _douyin_batch_preview(task_id: str):
    from backend.services import douyin_author_feed as F

    t = douyin_batch_tasks[task_id]
    params = t["params"]
    _batch_touch(task_id, status="RUNNING", stage="解析账号链接…",
                 extra={**t.get("extra", {}), "current_step": "preview"})
    try:
        author = await F.resolve_author(params.get("account_input", ""))
        _batch_log(task_id, f"主页解析成功：{author['homepage_url'][:70]}")

        async def _say(msg):
            _batch_touch(task_id, stage=str(msg))

        feed = await F.list_author_videos(
            author["sec_uid"], want=int(params.get("want") or 0), status_callback=_say)
        _batch_touch(
            task_id,
            items=feed["items"],
            intermediate={"feed": {k: v for k, v in feed.items() if k != "items"}},
            status="SUCCESS",
            progress=100,
            stage=(f"列表就绪：{feed.get('nickname') or '未知昵称'}｜取到 {feed['got']} 条本人作品"
                   f"（非本人已扔 {feed['foreign_dropped']} 条）"),
            extra={**t.get("extra", {}), "sec_uid": feed["sec_uid"],
                   "nickname": feed.get("nickname") or "",
                   "declared_count": feed.get("declared_count"),
                   "stopped_reason": feed.get("stopped_reason"),
                   "maybe_anonymous": feed.get("maybe_anonymous"),
                   "foreign_dropped": feed.get("foreign_dropped")},
        )
        _batch_log(task_id, f"翻页尺寸 {feed['page_sizes']}，has_more={feed['has_more']}，"
                            f"停止原因={feed['stopped_reason']}")
    except Exception as e:
        _batch_touch(task_id, status="FAILED", stage=f"取列表失败：{e}", error=str(e)[:600],
                     extra={**t.get("extra", {}), "failed_step": "preview"})
        _batch_log(task_id, f"取列表失败：{e}", level="error")
    _batch_persist(task_id)


async def _douyin_batch_download(task_id: str):
    import shutil
    from backend.services import douyin_author_feed as F
    from backend.services.link_fetch import link_fetch_local

    t = douyin_batch_tasks[task_id]
    items = t["items"]
    params = t["params"]
    extra = t.get("extra", {})
    target = Path(str(params.get("target_dir") or F.DEFAULT_TARGET_DIR))
    # 续跑必须落回同一个目录：优先用首次算出来并存档的那个，别拿昵称现算
    out_dir = Path(extra.get("out_dir") or F.run_dir(target, extra.get("nickname", ""),
                                                    extra.get("sec_uid", ""), task_id))
    out_dir.mkdir(parents=True, exist_ok=True)
    gap = max(0.0, float(params.get("gap_sec", 2.0)))
    total = len(items)
    _batch_touch(task_id, status="RUNNING", progress=0, error=None,
                 stage=f"目标目录：{out_dir}｜依次下载 {total} 条",
                 extra={**extra, "current_step": "download", "out_dir": str(out_dir)})
    _batch_log(task_id, f"开始依次下载 {total} 条 → {out_dir}")
    consecutive_fail = 0

    for pos, it in enumerate(items, start=1):
        t = douyin_batch_tasks[task_id]
        if t.get("abort_requested"):
            _batch_log(task_id, "已收到中止，剩余条目不再下载", level="error")
            break
        idx = it.get("index", pos)
        already = it.get("file") and (out_dir / it["file"]).exists()
        if it.get("dl_status") == "done" and already:
            continue
        if not it.get("downloadable", True):
            it["dl_status"] = "skipped"
            _batch_log(task_id, f"[{idx}/{total}] 跳过：{it.get('skip_reason') or '不可下载'}")
            _batch_touch(task_id, progress=int(pos / max(total, 1) * 100))
            continue

        fname = F.item_filename(idx, total, it["aweme_id"], it.get("title") or "")
        final_path = out_dir / fname
        if final_path.exists() and final_path.stat().st_size > 100_000:
            it.update({"dl_status": "done", "file": fname,
                       "size_mb": round(final_path.stat().st_size / 1024 / 1024, 2),
                       "method": it.get("method") or "已存在（跳过重下）", "error": ""})
            _batch_log(task_id, f"[{idx}/{total}] 已在盘上，跳过：{fname}")
            _batch_persist(task_id)
            continue

        it["dl_status"] = "downloading"
        _batch_touch(task_id, stage=f"[{idx}/{total}] 下载中：{(it.get('title') or '')[:24]}")
        try:
            raw = str(it.get("share_url") or "").strip()
            if not raw and it.get("aweme_id"):
                # 只有 aweme_id 时拼成分享页链接：下载腿认的是 URL（`resolve_douyin_url` 里没有
                # `http` 当场就拒），原来把裸 id 直接传进去必然失败 —— 那是给错了形状，不是下载器的毛病。
                raw = "https://www.iesdouyin.com/share/video/%s/" % it["aweme_id"]
            # 下载这条腿只有 `link_fetch` 一份：两次重试 + 断头片判定（ffprobe 量不出时长就重下）
            # 都在里面；批量存片只要文件 ⇒ analyze=False，与漏斗默认一致（这里写明是为了看得懂）。
            res = await link_fetch_local(raw, cb=_say_batch(task_id), out_dir=str(out_dir),
                                         analyze=False, allow_browser=True)
            src = Path(res["file_path"])
            if src != final_path:
                if final_path.exists():
                    final_path.unlink()
                shutil.move(str(src), str(final_path))
            size_mb = round(final_path.stat().st_size / 1024 / 1024, 2)
            it.update({
                "dl_status": "done", "file": fname, "size_mb": size_mb,
                "method": res.get("method") or "",
                "quality": res.get("quality") or "", "error": "",
            })
            _batch_log(task_id, f"[{idx}/{total}] 完成 {fname}"
                                f"（{res.get('method') or '没记'}｜{res.get('quality') or size_mb}MB）")
            consecutive_fail = 0
        except Exception as e:
            it.update({"dl_status": "failed", "error": str(e)[:300]})
            consecutive_fail += 1
            _batch_log(task_id, f"[{idx}/{total}] 失败：{str(e)[:180]}", level="error")
            if consecutive_fail >= 3:
                _batch_touch(task_id, stage=f"连续 {consecutive_fail} 条失败，先停下（多半被风控或 cookie 过期）")
                _batch_log(task_id, "连续 3 条失败，批量已自动停止，避免继续撞风控", level="error")
                break
        _batch_touch(task_id, progress=int(pos / max(total, 1) * 100))
        if pos % 20 == 0 or pos == total:
            # 3712 条的清单约 1MB，逐条全量写库 = 4GB 写放大
            _batch_persist(task_id)
        if gap:
            await asyncio.sleep(gap)

    cur = douyin_batch_tasks[task_id]
    counts = _batch_counts(cur["items"])
    try:
        _write_batch_ledger(out_dir, cur["items"])
    except Exception as e:
        _batch_log(task_id, f"清单写入失败：{e}", level="error")
    aborted = bool(cur.get("abort_requested"))
    status = "FAILED" if (aborted or counts["failed"]) else "SUCCESS"
    _batch_touch(
        task_id, status=status, progress=100,
        stage=(f"已中止：{counts['done']}/{counts['total']} 条已落盘" if aborted else
               f"完成：成功 {counts['done']}／失败 {counts['failed']}／跳过 {counts['skipped']}"
               f"／未做 {counts['pending']}"),
        error=cur.get("error"),
        outputs=[{"name": i["file"], "path": str(out_dir / i["file"]), "index": i["index"],
                  "size_mb": i.get("size_mb", 0), "method": i.get("method", "")}
                 for i in cur["items"] if i.get("dl_status") == "done" and i.get("file")],
        extra={**cur.get("extra", {}), "out_dir": str(out_dir), "counts": counts},
    )
    _batch_persist(task_id)


def _write_batch_ledger(out_dir: Path, items: list):
    import csv
    from backend.services import douyin_author_feed as F

    rows = F.list_ledger(items)
    with open(out_dir / "清单.csv", "w", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerows(rows)
    (out_dir / "清单.json").write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _say_batch(task_id: str):
    """下载器的每条状态同时进日志和当前 stage（前端只轮询一个任务口）"""
    async def _cb(msg):
        _batch_touch(task_id, stage=str(msg)[:160])
        _batch_log(task_id, f"    {msg}")
    return _cb


@app.get("/douyin-batch", response_class=HTMLResponse)
async def douyin_batch_page():
    """抖音账号批量下载工作台（账号主页链接 → 选数量 → 依次下载到指定文件夹）"""
    html_path = Path(settings.FRONTEND_DIR) / "douyin_batch_studio.html"
    if not html_path.exists():
        raise HTTPException(404, "抖音批量下载工作台页面未找到")
    return HTMLResponse(
        content=html_path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"},
    )


@app.get("/api/douyin-batch-studio/cookie-status")
async def douyin_batch_cookie_status():
    """只读登录态摘要：有没有文件、几条 cookie、关键位齐不齐。绝不回显 cookie 值。"""
    from backend.services import douyin_author_feed as F
    st = F.login_state()
    st["default_target_dir"] = str(F.DEFAULT_TARGET_DIR)
    return {"success": True, **st}


@app.post("/api/douyin-batch-studio/save-cookie")
async def douyin_batch_save_cookie(req: DouyinBatchCookieRequest):
    from backend.services import douyin_author_feed as F
    try:
        summary = F.save_cookie_string(req.cookie)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"success": True, **summary}


@app.post("/api/douyin-batch-studio/preview")
async def douyin_batch_preview(req: DouyinBatchPreviewRequest):
    from backend.services import douyin_author_feed as F
    if not (req.account_input or "").strip():
        raise HTTPException(400, "请输入抖音账号主页链接（或整段分享文案）")
    if not F.login_state()["exists"]:
        raise HTTPException(400, "还没有抖音登录态：请先粘贴 cookie。")

    task_id = uuid.uuid4().hex[:12]
    douyin_batch_tasks[task_id] = {
        "task_id": task_id, "status": "INIT", "progress": 0, "stage": "排队中",
        "kind": "preview", "params": {"account_input": req.account_input, "want": req.want},
        "items": [], "intermediate": {}, "extra": {"kind": "preview", "current_step": "init"},
        "outputs": [], "error": None, "logs": [],
    }
    try:
        from backend.services import task_store
        task_store.create(task_id, BATCH_TASK_TYPE, status="INIT", progress=0, stage="排队中",
                          params={"account_input": req.account_input, "want": req.want,
                                  "sec_uid": ""},
                          intermediate={}, extra={"kind": "preview", "current_step": "init"})
    except Exception:
        pass
    asyncio.create_task(_douyin_batch_preview(task_id))
    return {"success": True, "task_id": task_id}


@app.post("/api/douyin-batch-studio/run")
async def douyin_batch_run(req: DouyinBatchRunRequest):
    from backend.services import douyin_author_feed as F

    if not req.sec_uid:
        raise HTTPException(400, "缺 sec_uid：请先取列表再下载")
    if not req.items:
        raise HTTPException(400, "列表是空的，没东西可下")
    target = Path(str(req.target_dir or F.DEFAULT_TARGET_DIR))
    task_id = uuid.uuid4().hex[:12]
    out_dir = F.run_dir(target, req.nickname, req.sec_uid, task_id)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        raise HTTPException(400, f"目标目录不可写：{out_dir}（{e}）")

    items = [dict(i) for i in req.items]
    for pos, it in enumerate(items, start=1):
        it.setdefault("index", pos)
        it.setdefault("dl_status", "")
    params = {
        "sec_uid": req.sec_uid, "nickname": req.nickname, "target_dir": str(target),
        "gap_sec": req.gap_sec, "item_count": len(items),
        "account_input": "", "want": len(items),
    }
    douyin_batch_tasks[task_id] = {
        "task_id": task_id, "status": "INIT", "progress": 0, "stage": "排队中",
        "kind": "download", "params": params, "items": items, "intermediate": {},
        "extra": {"kind": "download", "current_step": "init", "sec_uid": req.sec_uid,
                  "nickname": req.nickname},
        "outputs": [], "error": None, "logs": [],
    }
    try:
        from backend.services import task_store
        task_store.create(task_id, BATCH_TASK_TYPE, status="INIT", progress=0, stage="排队中",
                          params=params, intermediate={"items": items},
                          extra={"kind": "download", "current_step": "init",
                                 "sec_uid": req.sec_uid, "nickname": req.nickname})
    except Exception:
        pass
    asyncio.create_task(_douyin_batch_download(task_id))
    return {"success": True, "task_id": task_id, "out_dir": str(out_dir)}


@app.get("/api/douyin-batch-studio/status")
async def douyin_batch_status(task_id: str):
    t = _batch_rehydrate(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    extra = t.get("extra", {})
    counts = _batch_counts(t.get("items", []))
    return {
        "success": True,
        "task_id": task_id,
        "kind": extra.get("kind", t.get("kind", "")),
        "status": t.get("status"),
        "progress": t.get("progress", 0),
        "stage": t.get("stage", ""),
        "error": t.get("error"),
        "sec_uid": extra.get("sec_uid", ""),
        "nickname": extra.get("nickname", ""),
        "declared_count": extra.get("declared_count"),
        "stopped_reason": extra.get("stopped_reason", ""),
        "maybe_anonymous": extra.get("maybe_anonymous", False),
        "foreign_dropped": extra.get("foreign_dropped", 0),
        "out_dir": extra.get("out_dir", ""),
        "counts": counts,
        "feed": t.get("intermediate", {}).get("feed", {}),
        "items": t.get("items", []),
        "logs": t.get("logs", [])[-60:],
    }


@app.get("/api/douyin-batch-studio/history")
async def douyin_batch_history(limit: int = 30):
    from backend.services import task_store
    out = []
    try:
        rows = task_store.list_by_type(BATCH_TASK_TYPE, limit=limit)
    except Exception:
        rows = []
    for t in rows:
        extra = t.get("extra", {}) or {}
        counts = extra.get("counts") or {}
        out.append({
            "task_id": t["task_id"],
            "kind": extra.get("kind", ""),
            "status": t.get("status"),
            "progress": t.get("progress", 0),
            "stage": t.get("stage", ""),
            "nickname": extra.get("nickname", ""),
            "sec_uid": extra.get("sec_uid", ""),
            "out_dir": extra.get("out_dir", ""),
            "item_count": (t.get("params", {}) or {}).get("item_count", 0),
            "counts": counts,
            "created_at": t.get("created_at", 0),
            "created_at_str": time.strftime("%Y-%m-%d %H:%M", time.localtime(t.get("created_at", 0)))
            if t.get("created_at") else "",
        })
    return {"success": True, "tasks": out}


@app.post("/api/douyin-batch-studio/retry")
async def douyin_batch_retry(task_id: str):
    t = _batch_rehydrate(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    if t.get("kind") != "download" and t.get("extra", {}).get("kind") != "download":
        raise HTTPException(400, "只有下载任务可以续跑（取列表任务请用「取列表」按钮）")
    if t.get("status") == "RUNNING":
        raise HTTPException(400, "这个任务还在跑，不用续")
    t["abort_requested"] = False
    for it in t["items"]:
        if it.get("dl_status") in ("failed", "downloading"):
            it["dl_status"] = ""
            it["error"] = ""
    _batch_log(task_id, "续跑：失败的条目重下，已落盘的自动跳过")
    asyncio.create_task(_douyin_batch_download(task_id))
    return {"success": True, "task_id": task_id}


@app.post("/api/douyin-batch-studio/abort")
async def douyin_batch_abort(task_id: str):
    t = _batch_rehydrate(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    t["abort_requested"] = True
    _batch_log(task_id, "用户请求中止，当前这条下完就停", level="error")
    return {"success": True, "task_id": task_id}


@app.get("/api/douyin-batch-studio/file")
async def douyin_batch_file(task_id: str, name: str):
    """文件在 C:\\视频下载（仓库外），所以这里给一条受路径校验的直链，不挂 StaticFiles"""
    t = _batch_rehydrate(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    out_dir = Path(t.get("extra", {}).get("out_dir") or "").resolve()
    if not str(out_dir) or not out_dir.exists():
        raise HTTPException(404, "任务目录还不存在")
    candidate = (out_dir / name).resolve()
    if not str(candidate).startswith(str(out_dir) + os.sep):
        raise HTTPException(400, "非法文件名")
    if not candidate.exists():
        raise HTTPException(404, f"文件不存在：{name}")
    suffix = candidate.suffix.lower()
    media_type = {"csv": "text/csv", "json": "application/json"}.get(suffix[1:], "video/mp4")
    return FileResponse(str(candidate), media_type=media_type, filename=candidate.name)


@app.post("/api/douyin-batch-studio/delete")
async def douyin_batch_delete(req: DouyinBatchDeleteRequest):
    """真删下载目录内的视频文件（M6：勾选即真删，拦住无效视频进蒸馏）。

    安全口径：
    - 路径 resolve 后必须落在下载根目录（C:/视频下载）内，防目录逃逸；
    - 只允许删视频后缀（mp4/webm/mov/m4v/flv/ts），清单账本 csv/json 删不到；
    - 不支持删目录。
    """
    from backend.services.douyin_author_feed import DEFAULT_TARGET_DIR
    root = Path(str(DEFAULT_TARGET_DIR)).resolve()
    video_suffixes = {".mp4", ".webm", ".mov", ".m4v", ".flv", ".ts"}
    deleted: list = []
    errors: list = []
    for f in (req.files or [])[:200]:
        raw = str(f or "").strip()
        if not raw:
            errors.append({"file": f, "error": "空路径"})
            continue
        try:
            p = Path(raw)
            if not p.is_absolute():
                p = root / raw
            rp = p.resolve()
            if not str(rp).startswith(str(root) + os.sep):
                errors.append({"file": raw, "error": "路径越界：不在下载目录内，拒绝删除"})
                continue
            if rp.suffix.lower() not in video_suffixes:
                errors.append({"file": raw, "error": f"只允许删除视频文件（{sorted(video_suffixes)}）"})
                continue
            if rp.is_dir():
                errors.append({"file": raw, "error": "不支持删除目录"})
                continue
            if not rp.exists():
                errors.append({"file": raw, "error": "文件不存在"})
                continue
            rp.unlink()
            deleted.append(str(rp))
        except Exception as e:
            errors.append({"file": raw, "error": str(e)[:200]})
    if req.task_id and deleted:
        try:
            _batch_log(req.task_id, f"真删 {len(deleted)} 个视频文件（拦住无效视频进蒸馏）")
        except Exception:
            pass
    return {"success": True, "deleted": deleted, "errors": errors}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host=settings.HOST, port=settings.PORT, reload=False)
