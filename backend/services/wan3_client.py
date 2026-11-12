"""RunningHub Wan3.0 标准模型 API 客户端。

链路：RunningHub CDN 上传媒体 → RunningHub Wan3.0 标准模型提交 →
RunningHub v2 查询 → 下载结果视频。

兼容旧版函数签名（upload_media / submit_reference_to_video / query_task /
poll_until_done / download_result / run_one_segment），上层调用无需修改。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from config import settings

# ===== RunningHub Wan3.0 标准模型配置 =====
RH_WAN3_BASE_URL = (getattr(settings, "RUNNINGHUB_WAN3_BASE_URL", "") or "https://www.runninghub.ai").strip()
RH_WAN3_ENDPOINT = "/openapi/v2/alibaba/wan-3.0/reference-to-video"
RH_QUERY_ENDPOINT = "/openapi/v2/query"

# ===== 异常类型（用于 run_one_segment 的 seed 重试决策）=====
class WanError(Exception):
    """Wan3.0 调用异常基类"""
    def __init__(self, message: str, *, status_code: Optional[int] = None, reason: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason


class WanAuthError(WanError):
    """鉴权失败（401/403）—— 不重试"""


class WanParamError(WanError):
    """参数非法（400）—— 不重试"""


class WanApprovalRequired(WanError):
    """网关批准闸的拒绝（0 分未扣、任务未发起）—— 不重试，也不许被叫成「参数非法（400）」
    （2026-10-22 sc_9c246af70384 就是把这句拒因印成了 400，人据此以为提交坏了）"""


class WanUpstreamUnavailable(WanError):
    """上游分销商没有可用渠道 / 上游 5xx —— 不是我方参数错。
    2026-10-22 23:51 现网：`No available channel for model w3-p-c1`（HTTP 500 包着上游 503）
    被归成「API 参数非法（400）」⇒ 人会去改 prompt，而真正要决定的是等上游还是换一条线。"""


class WanContentRejectError(WanError):
    """内容/安全审核拒绝 —— 不通过换 seed 重试"""
    # 触发关键词（按 failed_reason / message 文本匹配）
    CONTENT_REJECT_KEYWORDS = (
        "内容审核", "安全审核", "违规", "敏感内容", "content_filter",
        "content policy", "safety", "violation",
    )


class WanGenerateError(WanError):
    """生成失败 / 模型内部失败 —— seed + 1 重试"""


class WanTimeoutError(WanError):
    """任务轮询超时（任务可能仍 RUNNING）—— 恢复流程，不重新提交"""


class WanTransientError(WanError):
    """可重试的瞬时错误（HTTP 429/5xx/网络超时）—— 原 seed 重试请求"""


# ===== RunningHub CDN 上传配置（仅用于上传本地文件拿公网 URL）=====
RH_BASE_URL = (getattr(settings, "RUNNINGHUB_WAN3_BASE_URL", "") or "https://www.runninghub.ai").strip()
RH_UPLOAD_ENDPOINT = "/openapi/v2/media/upload/binary"


def _resolve_wan3_key(override: Optional[str] = None) -> str:
    """解析 RunningHub Wan3.0 API Key：override > RUNNINGHUB_WAN3_API_KEY。"""
    src = "override"
    if override and override.strip():
        key = override.strip()
    else:
        key = (getattr(settings, "RUNNINGHUB_WAN3_API_KEY", "") or "").strip()
        src = "settings.RUNNINGHUB_WAN3_API_KEY"
    if not key:
        raise RuntimeError("未配置 RUNNINGHUB_WAN3_API_KEY，无法调用 RunningHub Wan3.0 API")
    masked = key[:6] + "***" + key[-4:] if len(key) >= 10 else "***"
    print(f"[wan3_client] RH_BASE_URL={RH_WAN3_BASE_URL} key_src={src} key={masked}", flush=True)
    return key


def _resolve_upload_key(override: Optional[str] = None) -> str:
    """解析 RunningHub 上传专用 key（上传 CDN 仍走 RH 企业 key）"""
    src = "override"
    if override and override.strip():
        key = override.strip()
    else:
        key = (getattr(settings, "RUNNINGHUB_WAN3_API_KEY", "") or "").strip()
        src = "settings.RUNNINGHUB_WAN3_API_KEY"
        if not key:
            key = (getattr(settings, "RUNNINGHUB_ENTERPRISE_API_KEY", "") or "").strip()
            src = "settings.RUNNINGHUB_ENTERPRISE_API_KEY"
        if not key:
            key = (getattr(settings, "RUNNINGHUB_API_KEY", "") or "").strip()
            src = "settings.RUNNINGHUB_API_KEY"
    if not key:
        raise RuntimeError("未配置 RUNNINGHUB_API_KEY，无法上传文件到 RunningHub CDN")
    return key


def upload_media(file_path: str, api_key: Optional[str] = None) -> str:
    """上传本地文件到 RunningHub CDN，返回公网 download_url。

    DashScope 官方不提供媒体上传接口，只能用公网 URL 作为 media。
    此处借用 RunningHub CDN 做文件托管。

    Args:
        file_path: 本地文件绝对路径（mp4/jpg/png 等）
        api_key: 可选 RunningHub API Key，默认读配置

    Returns:
        download_url 字符串（公网可访问）

    Raises:
        FileNotFoundError: 文件不存在
        RuntimeError: 上传失败
    """
    p = Path(file_path)
    if not p.exists():
        raise FileNotFoundError(f"文件不存在: {file_path}")

    key = _resolve_upload_key(api_key)
    url = f"{RH_BASE_URL}{RH_UPLOAD_ENDPOINT}"

    with open(p, "rb") as f:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {key}"},
            files={"file": (p.name, f)},
            timeout=300,
        )

    if resp.status_code != 200:
        raise RuntimeError(f"上传失败 HTTP {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(
            f"上传失败 code={data.get('code')} msg={data.get('message')}"
        )

    download_url = (data.get("data") or {}).get("download_url")
    if not download_url:
        raise RuntimeError(f"上传响应无 download_url: {data}")
    return download_url


# ==== 🔴 RunningHub wan3.0 的闸，钉在【花钱这一刀】上（2026-10-09 用户两条指令）==================
# 指令一"把 runninghub 的 wan3.0 断了" ⇒ 默认【拒绝提交】。
# 指令二"生视频必须我同意（铁律）" ⇒ 就算有人把开关打开，也必须先有人在 8100/approval 点过一次。
# 为什么钉在这里、不钉在网关前门：8090 的 /api/super-clone-wan3/*（app.py:10230+）、
#   replica_wan3_pipeline、smart_fission/step6_wan3_direct、super_clone_wan3、还有一堆 _e2e_*.py
#   都【直连本模块】，从不经 gateway.invoke() ⇒ 前门的闸对它们根本不存在。
#   实测（2026-10-09）：11:01:13 提交、11:17:39 出片，而网关账本 10:32 之后一行都没有。
#   本模块的 submit_reference_to_video 是所有 wan3 腿的唯一咽喉（run_one_segment 也走它），
#   钉在这里 = 一处落码全覆盖，含以后新写的直连脚本。
# 🔴 只断【提交】：query_task / poll_until_done / download_result 一律照常 ——
#   断之前已提交的任务还要用 taskId 捞回结果（重投=双花，那条禁令不变）。
def _wan3_submit_guard(count: int = 1, account: str = "local") -> None:
    """不满足就抛 WanParamError。任何自检异常一律按【拒】处理：闸自己坏了不许被当成"闸没拦"。"""
    if not bool(getattr(settings, "WAN3_RH_ENABLED", False)):
        raise WanParamError(
            "RunningHub wan3.0 已按 2026-10-09 指令断开：不再提交新任务。"
            "已提交的任务仍可用 query_task/poll_until_done/download_result 捞回（别重投）。"
            "要恢复：.env 或 config 里设 WAN3_RH_ENABLED=1；恢复后仍然【必须有人的批准条子】才提交。")
    try:
        from backend import gateway as gw
        # 🔴 会话只许一处读法（gateway._session_of）。原来这里自己 os.environ 读一遍、批准闸那处
        #    又不回落 env ⇒ 两边各走各的：条子按会话绑，匹配时递上去的是空串 = 永远匹配不上。
        sid = gw._session_of({})
        est = int((gw.meter("video.generate", "clone-r2v", {}) or {}).get("credits_estimate") or 0)
        ticket = gw.check_spend_approval("video.generate", "clone-r2v", account,
                                         count=max(int(count or 1), 1),
                                         credits=max(est, 1) * max(int(count or 1), 1), session=sid)
    except WanParamError:
        raise
    except Exception as e:
        raise WanParamError("wan3 批准闸自检失败（按拒处理）：%s: %s" % (type(e).__name__, str(e)[:160]))
    if not (isinstance(ticket, dict) and ticket.get("ok")):
        raise WanParamError("没批准条子，拒绝提交 wan3.0（reason=%s）：先报价并让人在 "
                            "http://127.0.0.1:8100/approval 点一次"
                            % str((ticket or {}).get("reason") if isinstance(ticket, dict) else ticket))


def _prime_resolution(resolution) -> str:
    """wan3-prime 的分辨率档：判据【不抄第二份】，现读网关那条 lane 自己的名单
    `gateway.lane_resolution_choices("video.generate","wan3-prime")`。

    以前这里是硬编码 `!= "1080P"` 就拒 ⇒ 网关那侧其实有 720p 的模型档，但没人能把它送进来；
    而网关白名单一旦加档，这里不改就变成第二份真相、永远比网关窄。
    🔴 2026-10-30 换芯后再补一层：原来直接读 `gateway._WAN30_RESOLUTIONS` —— 那是 **tianyue 那一家**的名单，
       而 wan3-prime 现在指 yz30。名字里带 prime、判据守别家 = 换芯那天就没人能送 720P、
       而且以后 tianyue 加档会悄悄放过 yz30 不认的参数 ⇒ 改成按【路由的 provider】取那一份。
       取不到网关那份（导入顺序/网关没起来）按【拒】处理，不退回一份自造的字面量名单。
    """
    from backend import gateway as gw
    res = str(resolution or "1080P").strip().upper().replace(" ", "")
    allowed = tuple(getattr(gw, "lane_resolution_choices", lambda *a: ())("video.generate", "wan3-prime") or ())
    if not allowed:
        raise WanParamError("读不到 gateway.lane_resolution_choices('video.generate','wan3-prime') ⇒ 按拒处理"
                            "（自检失败不许被当成『闸没拦』）")
    if res not in allowed:
        raise WanParamError("wan3-prime 分辨率只认 %s（收到 %r）⇒ 档位唯一源在网关那条 lane 自己的名单，"
                            "要别的分辨率是换档名单，不是改这条校验" % ("/".join(allowed), resolution))
    return res


def submit_reference_to_video(
    prompt: str,
    video_urls: List[str],
    image_urls: Optional[List[str]] = None,
    audio_urls: Optional[List[str]] = None,
    duration: int = 5,
    resolution: str = "480P",
    aspect_ratio: str = "adaptive",
    audio: bool = True,
    enable_thinking: bool = False,
    prompt_extend: bool = False,
    seed: Optional[int] = None,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """提交 RunningHub Wan3.0 参考生视频任务。

    严格遵循官方参数约束：
    - reference_video: 最多 5 段，单段 1-15 秒，总时长 ≤15 秒
    - reference_image: 最多 10 张
    - reference_audio: 最多 5 段，单段 1-15 秒，总时长 ≤15 秒
    - duration: 2-30 整数（或 -1 智能时长模式）。有参考视频时，输入视频时长 + 输出时长 ≤30 秒

    返回结构兼容旧版 RunningHub 格式：
    - 旧 RH: {taskId, code, msg}
    - 新 DS: {output: {task_id, task_status}, request_id}
    统一转换为 {taskId, raw: <原始响应>}
    """
    _wan3_submit_guard()   # 🔴 花钱这一刀上的闸：wan3 已断 / 没批准条子 ⇒ 到不了 requests.post
    key = _resolve_wan3_key(api_key)
    url = f"{RH_WAN3_BASE_URL}{RH_WAN3_ENDPOINT}"
    payload = {
        "prompt": prompt,
        "videoUrls": video_urls or [],
        "imageUrls": image_urls or [],
        "audioUrls": audio_urls or [],
        "resolution": resolution,
        "duration": str(duration),
        "fileUrl": None,
        "linkUrl": None,
        "aspectRatio": aspect_ratio,
        "audio": bool(audio),
        "enableThinking": bool(enable_thinking),
    }
    if seed is not None:
        payload["seed"] = seed

    # #region debug-point structural-cart-replacement
    try:
        import urllib.request as _debug_request
        import json as _debug_json
        _debug_request.urlopen(_debug_request.Request(
            "http://127.0.0.1:7777/event",
            data=_debug_json.dumps({
                "sessionId": "structural-cart-replacement",
                "runId": "pre-fix",
                "hypothesisId": "B",
                "location": "backend/services/wan3_client.py:submit_reference_to_video",
                "msg": "[DEBUG] structural replacement submit inputs",
                "data": {
                    "video_count": len(video_urls or []),
                    "image_count": len(image_urls or []),
                    "duration": duration,
                    "resolution": resolution,
                    "aspect_ratio": aspect_ratio,
                    "audio": bool(audio),
                    "prompt_has_three_tiers": "三层" in prompt or "3-Tier" in prompt,
                    "prompt_has_four_tiers": "四层" in prompt or "4-Tier" in prompt,
                },
            }, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json"},
        ), timeout=3).read()
    except Exception:
        pass
    # #endregion

    # #region debug-point B:wan-submit-payload
    import json as _debug_json, os as _debug_os, re, urllib.request as _debug_request
    try:
        _debug_env = ".dbg/cup-product-migration.env"
        _debug_cfg = open(_debug_env, encoding="utf-8").read()
        _debug_url = next((line.split("=", 1)[1].strip() for line in _debug_cfg.splitlines() if line.startswith("DEBUG_SERVER_URL=")), "http://127.0.0.1:7777/event")
        _debug_session = next((line.split("=", 1)[1].strip() for line in _debug_cfg.splitlines() if line.startswith("DEBUG_SESSION_ID=")), "cup-product-migration")
        _debug_data = {"sessionId": _debug_session, "runId": "runninghub-wan3", "hypothesisId": "B", "location": "backend/services/wan3_client.py:197", "msg": "[DEBUG] RunningHub Wan3 submit payload", "data": {"video_count": len(video_urls or []), "image_count": len(image_urls or []), "image_urls_present": [bool(url) for url in (image_urls or [])], "shot_image_refs": re.findall(r"Shot\\s+\\d+[^\\n]*?(?:Image\\s+\\d+[^\\n]*)?", prompt)}, "ts": int(time.time() * 1000)}
        _debug_request.urlopen(_debug_request.Request(_debug_url, data=_debug_json.dumps(_debug_data, ensure_ascii=False).encode(), headers={"Content-Type": "application/json"}), timeout=2).read()
    except Exception:
        pass
    # #endregion

    try:
        resp = requests.post(
            url,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
                "User-Agent": "video-skill-wan3/1.0",
            },
            json=payload,
            timeout=60,
        )
    except requests.exceptions.Timeout as e:
        # 网络超时 → 瞬时错误，原 seed 重试
        raise WanTransientError(f"提交 wan3.0 网络超时: {e}") from e
    except requests.exceptions.ConnectionError as e:
        # 网络连接错误 → 瞬时错误，原 seed 重试
        raise WanTransientError(f"提交 wan3.0 网络连接失败: {e}") from e

    if resp.status_code != 200:
        body = resp.text[:300]
        sc = resp.status_code
        # 鉴权失败 → 不重试
        if sc in (401, 403):
            raise WanAuthError(f"鉴权失败 HTTP {sc}: {body}", status_code=sc)
        # 参数非法 → 不重试
        if sc == 400:
            raise WanParamError(f"参数非法 HTTP {sc}: {body}", status_code=sc)
        # 限流 / 服务端 5xx / 网关错误 → 原 seed 重试
        if sc == 429 or 500 <= sc < 600:
            raise WanTransientError(f"瞬时错误 HTTP {sc}: {body}", status_code=sc)
        # 其他 4xx（404/405/409 等）→ 当作参数类错误，不重试
        raise WanParamError(f"非预期 HTTP {sc}: {body}", status_code=sc)

    data = resp.json()
    task_id = data.get("taskId")
    if not task_id:
        raise RuntimeError(f"提交响应无 taskId: {data}")
    return {"taskId": task_id, "task_id": task_id, "raw": data}


def query_task(task_id: str, api_key: Optional[str] = None) -> Dict[str, Any]:
    """查询 RunningHub Wan3.0 任务状态。

    返回结构兼容旧版 RunningHub 字段：
    - status: 旧 RH 用 SUCCESS/FAILED/QUEUED/RUNNING
      新 DS 用 SUCCEEDED/FAILED/PENDING/RUNNING
      统一映射：SUCCEEDED → SUCCESS, PENDING → QUEUED
    - results: [{url}]（旧版字段，从 DS 的 output.video_url 转换）
    - errorMessage / failedReason
    """
    key = _resolve_wan3_key(api_key)
    url = f"{RH_WAN3_BASE_URL}{RH_QUERY_ENDPOINT}"

    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"taskId": task_id},
        timeout=60,
    )

    if resp.status_code != 200:
        raise RuntimeError(f"查询失败 HTTP {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    rh_status = str(data.get("status") or "").upper()
    status_map = {"SUCCESS": "SUCCESS", "RUNNING": "RUNNING", "QUEUED": "QUEUED", "FAILED": "FAILED", "CANCELED": "FAILED", "CANCELLED": "FAILED"}
    mapped_status = status_map.get(rh_status, rh_status or "UNKNOWN")
    results = data.get("results") or []
    return {
        "status": mapped_status,
        "results": results,
        "task_id": task_id,
        "raw_status": rh_status,
        "errorMessage": data.get("errorMessage") or "",
        "failedReason": data.get("failedReason") or {},
        "raw": data,
    }


def poll_until_done(
    task_id: str,
    max_wait: int = 1800,
    interval: int = 10,
    api_key: Optional[str] = None,
    on_progress: Optional[callable] = None,
) -> Dict[str, Any]:
    """轮询任务直到完成（SUCCESS / FAILED）。

    Args:
        task_id: DashScope 任务 ID
        max_wait: 最大等待秒数（默认 30 分钟）
        interval: 轮询间隔秒数
        on_progress: 可选回调 fn(status, elapsed, msg)，用于实时上报

    Returns:
        SUCCESS 时的任务详情（含 results[0].url）

    Raises:
        WanContentRejectError: 内容/安全审核拒绝
        WanGenerateError: 生成失败 / 模型内部失败
        WanTimeoutError: 轮询超时（任务可能仍 RUNNING）
    """
    start = time.time()
    last_status = None
    while True:
        elapsed = int(time.time() - start)
        if elapsed > max_wait:
            # 超时但任务可能仍 RUNNING —— 抛 WanTimeoutError，由上层决定恢复或放弃
            raise WanTimeoutError(
                f"任务 {task_id} 轮询超时（{max_wait}s），最后状态: {last_status}",
                reason=str(last_status),
            )

        try:
            data = query_task(task_id, api_key=api_key)
        except Exception as e:
            if on_progress:
                on_progress("QUERY_ERROR", elapsed, str(e))
            time.sleep(interval)
            continue

        status = data.get("status", "")
        if status != last_status:
            last_status = status
        if on_progress:
            on_progress(status, elapsed, data.get("errorMessage", ""))

        if status == "SUCCESS":
            return data
        if status == "FAILED":
            reason = data.get("failedReason") or data.get("errorMessage") or "未知原因"
            # 区分内容拒绝 vs 生成失败
            reason_lower = reason.lower()
            is_content_reject = any(
                kw.lower() in reason_lower
                for kw in WanContentRejectError.CONTENT_REJECT_KEYWORDS
            )
            if is_content_reject:
                raise WanContentRejectError(
                    f"任务 {task_id} 内容/安全审核拒绝: {reason}",
                    reason=reason,
                )
            raise WanGenerateError(
                f"任务 {task_id} 生成失败: {reason}",
                reason=reason,
            )

        time.sleep(interval)


def download_result(
    result_url: str,
    output_path: str,
    api_key: Optional[str] = None,
) -> str:
    """下载结果视频到本地。

    DashScope 返回的视频 URL 是阿里云 OSS 公开链接，无需鉴权。
    """
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    resp = requests.get(result_url, stream=True, timeout=300)
    if resp.status_code != 200:
        raise RuntimeError(
            f"下载失败 HTTP {resp.status_code}: {resp.text[:200]}"
        )

    with open(out, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)

    return str(out)


# 上游「没渠道 / 抖动」的原话特征：命中就不许再报成「参数非法（400）」。
# 只收【上游自己说的】字样，不收 prompt/ratio/duration 那类我方本地已拦过的形状。
_UPSTREAM_UNAVAILABLE_MARKERS = ("no available channel", "model_not_found", "upstream",
                                 "上游创建任务失败", "状态码:503", "http 500", "http 502",
                                 "http 503", "http 504")


def _raise_prime_env_failure(env: dict, jid: str) -> None:
    """网关信封 ok=false ⇒ 按【网关自己给的理由】分类，两条 prime 腿共用同一个判据。

    🔴 不许一律 raise WanParamError：批准闸的拒绝是「0 分未扣、任务根本没发起」，
       和「上游回 400 说参数写错」是两件不同的事，印错会让人去改参数。
    """
    msg = str(env.get("error") or env)[:700]
    if str((env.get("approval") or {}).get("reason") or "") == "approval_required":
        raise WanApprovalRequired("wan3-prime 被批准闸拒绝（0 分未扣、任务未发起）：%s" % msg)
    if jid or "结果未知" in msg or "不要重发" in msg:
        raise WanTimeoutError("wan3-prime 结果未知：%s（job_id=%s ⇒ 用 job_id 捞回，别再提交）"
                              % (msg, jid or "无"))
    low = msg.lower()
    if any(m in low or m in msg for m in _UPSTREAM_UNAVAILABLE_MARKERS):
        raise WanUpstreamUnavailable("wan3-prime 上游没有可用渠道（0 分未扣、任务未发起，"
                                     "重投=再花一次上游 calls）：%s" % msg)
    raise WanParamError("wan3-prime 失败：%s" % msg)


def run_one_segment_prime(
    prompt: str,
    reference_video_path: str,
    image_urls: List[str],
    duration: int,
    output_dir: str,
    segment_index: int,
    resolution: str = "1080P",
    aspect_ratio: str = "adaptive",
    audio: bool = True,
    audio_urls: Optional[List[str]] = None,
    account: str = "local",
    on_progress: Optional[callable] = None,
) -> Dict[str, Any]:
    """超级复刻 wan3.0 的提交腿：走【网关 lane video.generate/wan3-prime】(provider 现读 ROUTES，2026-10-30 起=yz30/youzan666)。

    为什么换这条（2026-10-10 排查定案）：
      - 2026-10-09 用户禁令"视频严格杜绝走 RunningHub" ⇒ config.WAN3_RH_ENABLED=False，走入口提交不出去；
      - 而表单锁定行写 yz-prime、合同 spend 写 clone-r2v（价目里查不到）、网关价目里是 wan3-prime
        ⇒ 三处各说各话，agent 才"路由到 RunningHub wan3.0"。现在三处统一指向这条 lane；
      - 只有经 gateway.invoke 才有 预扣→结算→审计（直连厂商那条在账本上对不到人）。

    🔴 本函数只 call 网关一次；**重试环在网关适配器里**（`gateway._yz_prime_run`），一次批准内部重投
    ⇒ 上限 4 次尝试、退避 45/120/240s，而且每次决定前都先读平台对账口 `/v1/balance`（0 分）：
    「任务失败但计费 0」或「报未知但平台没建任务」才允许重投；已计费/已出片一律只捞不重发（守双花）。
    为什么重试不放上层：上层每 call 一次就吃一段批准额度 ⇒ 上游 23% 的成功率会变成"用户点四次批准"。
    不换 seed（这条 lane 没有 seed 入参）。
    ⚠️ 能力差异（不假装支持）：上游参数卡（GET /v1/models，2026-10-14 现读）写的是
       `audio: boolean`、`media_types` 含 `reference_audio` ⇒ **这条 lane 支持传音频、也支持开关声音**，
       两个参数现在都真送上去（以前 `audio` 被我们丢掉、只在转储里写一句"没送"）。
       但【送上去会起什么作用没有实测】：reference_audio 到底是"照这条音频念"还是"只取音色"，
       参数卡没写 ⇒ 出片后必须本地 ASR 复验听到的就是新词，不许凭参数在交付里写"口播已改"。
       要动声音却不给参考音频，这里仍直接拒（0 消耗），不悄悄降级。
    """
    from backend import gateway as gw

    if not str(prompt or "").strip():
        raise WanParamError("wan3-prime 要给 prompt（编译器写出的那一段）")
    _res = _prime_resolution(resolution)
    # 🔴 画幅名单【只从网关那条 lane 自己取】，取不到就按拒处理：
    #    2026-10-20 那次留的"退回同一组字面量"兜底，在 2026-10-30 换芯后变成了第二份真相
    #    （yz30 认七档含 21:9、tianyue 认六档）—— 一份自造名单要么比网关宽（放行上游会拒的参数），
    #    要么比网关窄（客户点名的画幅永远进不来），两种都是"回执写 A、实跑 B"那一类。
    #    历史：更早这里还写死 `gw._PRIME_YZ_RATIOS`（那个常量 10-16 连适配器一起删了）
    #    ⇒ 每次提交都在本地抛 AttributeError（0 分、一个请求都不发）。名字要有主人。
    _ratios = tuple(getattr(gw, "lane_ratio_choices", lambda *a: ())("video.generate", "wan3-prime") or ())
    if not _ratios:
        raise WanParamError("读不到 gateway.lane_ratio_choices('video.generate','wan3-prime') ⇒ 按拒处理"
                            "（自检失败不许被当成『闸没拦』，更不许退回一份自造的字面量名单）")
    ratio = str(aspect_ratio or "adaptive").strip()
    if ratio not in _ratios:
        raise WanParamError("wan3-prime ratio 只认 %s（收到 %r）" % ("/".join(_ratios), ratio))
    try:
        dur = int(float(duration))
    except (TypeError, ValueError):
        raise WanParamError("wan3-prime duration 要正整数秒（收到 %r）" % (duration,))
    if dur <= 0:
        raise WanParamError("wan3-prime duration 要正整数秒（收到 %r）" % (duration,))
    refs = [str(u or "").strip() for u in (audio_urls or []) if str(u or "").strip()]
    if audio is False and not refs:
        raise WanParamError("这条 lane 没有『原声/静音』开关：要动声音必须给 reference_audio（现在一条都没有）"
                            "⇒ 属于参数被悄悄忽略，先拒（0 消耗）")

    if on_progress:
        on_progress("prime_upload", 0, "托管参考视频（拿 HTTPS 外链）")
    ref_url = upload_media(reference_video_path)      # CDN 托管不是视频生成 ⇒ 不碰 RH 视频禁令

    out = os.path.join(output_dir, "seg%02d-prime.mp4" % int(segment_index))
    params = {"prompt": prompt, "duration": dur, "resolution": _res, "ratio": ratio,
              "video_urls": [ref_url], "image_urls": [u for u in (image_urls or []) if u],
              "audio_urls": refs, "audio": bool(audio), "output_path": out}
    if on_progress:
        on_progress("prime_submit", 0, "提交网关 video.generate/wan3-prime（预扣→提交→轮询→落盘）")
    env = gw.invoke("video.generate", "wan3-prime", account=account, params=params)
    res = env.get("result") or {}
    jid = str(env.get("job_id") or res.get("job_id") or "").strip()
    # 🔴 上游 job_id 一拿到就落盘，【成功失败都写】：账本 holds 只有 hold_id、没有上游任务号，
    #    而"回滚"只退我们这边的记账 —— RunningHub 按 GPU 秒真扣钱，那边可能已经开了任务。
    #    没有这一行，进程一死就没人捞得回来（2026-10-10 现网：30 秒内三次预扣、两次 rolled_back，
    #    盘上 grep 不到任何 job_id ⇒ 只能去控制台人工认，或者重投=双花）。
    try:
        with open(os.path.join(output_dir, "_wan3_job.json"), "w", encoding="utf-8") as _jh:
            json.dump({"job_id": jid, "ok": bool(env.get("ok")), "account": account,
                       "route": "video.generate/wan3-prime", "ts": round(time.time(), 3),
                       "error": str(env.get("error") or "")[:300],
                       # 🔴 试了几次、每次怎么判的、平台对账读数 —— 全落盘。
                       #    没有这三格，"提交失败"就只剩一句猜（2026-10-10：连着四次 unknown，
                       #    其中两次平台其实建了任务，而我们盘上只有一句 job_id=''）。
                       "attempts": res.get("prime_attempts") or 1,
                       "tries": res.get("prime_tries") or [],
                       "reconcile": res.get("prime_reconcile"),
                       "output_path": out}, _jh, ensure_ascii=False, indent=1)
    except OSError:
        pass                      # 落盘失败不许改变提交结果，但下一行的错误信息里会带上 jid
    if not env.get("ok"):
        # 结果未知（超时/断线/已出片没落盘）⇒ 抛超时类，让上层【只捞不重投】；真参数错 ⇒ 抛参数错
        _raise_prime_env_failure(env, jid)
    local = res.get("path") or out
    if not os.path.isfile(local):
        raise WanTimeoutError("wan3-prime 信封说成功但盘上没有 %s（job_id=%s ⇒ 先捞回再判）" % (local, jid))
    # 🔴 provider 现读 ROUTES（2026-10-30 换芯 yz30 后这里写死 "tianyue" 就是回执撒谎）；
    #    模型名只许现读信封（`_yz30_video_run` 带的就是真发出去那条）；以前这里写死一个
    #    "wan3.0-video-prime" 兜底 ⇒ 上游没回 model 字段时台账会印一个【不存在的模型名】冒充出处。
    #    拿不到就留空，由回执那一格写"没记"，不猜。
    from backend import gateway as _gwprov
    _prov = str(((_gwprov.ROUTES.get(("video.generate", "wan3-prime")) or {}).get("provider")) or "没记")
    return {"segment_index": segment_index, "task_id": jid, "provider": _prov,
            "lane": "video.generate/wan3-prime",
            "model": str(res.get("model") or res.get("model_used") or ""),
            "reference_video_url": ref_url, "result_url": res.get("url") or "",
            "local_path": str(local), "duration": dur, "seed_used": None, "attempts": 1,
            "bytes": res.get("bytes"), "calls": res.get("calls"), "credits": env.get("credits") or {},
            "note": res.get("note") or ""}


def run_prompt_only_prime(
    prompt: str,
    image_urls: List[str],
    duration: int,
    output_dir: str,
    output_name: str = "wan3-prime.mp4",
    resolution: str = "1080P",
    aspect_ratio: str = "adaptive",
    account: str = "local",
    on_progress: Optional[callable] = None,
) -> Dict[str, Any]:
    """账号知识库裂变的出片腿：wan3-prime **prompt 生视频**（无参考视频，仅参考图）。

    与 run_one_segment_prime 同一条网关 lane（video.generate/wan3-prime），三点差别：
      - 不带 reference_video：成片全靠 Wan3.0 语法 prompt 驱动 + 参考图定产品/人/景；
      - 时长由调用方给（本工作台缺省 30s，上游 durations.max=null 不设上限）；
      - 批准闸 2026-10-12 用户口径【免人批】（purpose 级免批登记），但免批 ≠ 免账：
        预扣→提交→轮询→结算/回滚照旧走 gateway.invoke。
    🔴 重试环在网关适配器里（对账防双花，最多 4 次尝试），本函数【绝不重复提交】：
       结果未知（有 job_id / 超时）只抛 WanTimeoutError，由上层拿 job_id 捞回。
    """
    from backend import gateway as gw

    if not str(prompt or "").strip():
        raise WanParamError("wan3-prime 要给 prompt（编译器拼出的 Wan3.0 文本）")
    _res = _prime_resolution(resolution)
    # 🔴 画幅名单【只从网关那条 lane 自己取】，取不到就按拒处理：
    #    2026-10-20 那次留的"退回同一组字面量"兜底，在 2026-10-30 换芯后变成了第二份真相
    #    （yz30 认七档含 21:9、tianyue 认六档）—— 一份自造名单要么比网关宽（放行上游会拒的参数），
    #    要么比网关窄（客户点名的画幅永远进不来），两种都是"回执写 A、实跑 B"那一类。
    #    历史：更早这里还写死 `gw._PRIME_YZ_RATIOS`（那个常量 10-16 连适配器一起删了）
    #    ⇒ 每次提交都在本地抛 AttributeError（0 分、一个请求都不发）。名字要有主人。
    _ratios = tuple(getattr(gw, "lane_ratio_choices", lambda *a: ())("video.generate", "wan3-prime") or ())
    if not _ratios:
        raise WanParamError("读不到 gateway.lane_ratio_choices('video.generate','wan3-prime') ⇒ 按拒处理"
                            "（自检失败不许被当成『闸没拦』，更不许退回一份自造的字面量名单）")
    ratio = str(aspect_ratio or "adaptive").strip()
    if ratio not in _ratios:
        raise WanParamError("wan3-prime ratio 只认 %s（收到 %r）" % ("/".join(_ratios), ratio))
    try:
        dur = int(float(duration))
    except (TypeError, ValueError):
        raise WanParamError("wan3-prime duration 要正整数秒（收到 %r）" % (duration,))
    if dur <= 0:
        raise WanParamError("wan3-prime duration 要正整数秒（收到 %r）" % (duration,))
    imgs = [str(u or "").strip() for u in (image_urls or []) if str(u or "").strip()]

    os.makedirs(output_dir, exist_ok=True)
    out = os.path.join(output_dir, output_name)
    params = {"prompt": prompt, "duration": dur, "resolution": _res, "ratio": ratio,
              "image_urls": imgs, "output_path": out}
    if on_progress:
        on_progress("prime_submit", 0, "提交网关 video.generate/wan3-prime（免人批不免账：预扣→提交→轮询→落盘）")
    env = gw.invoke("video.generate", "wan3-prime", account=account, params=params)
    res = env.get("result") or {}
    jid = str(env.get("job_id") or res.get("job_id") or "").strip()
    # 🔴 job_id 一拿到就落盘（成功失败都写）：进程一死靠它捞回，重投=双花
    try:
        with open(os.path.join(output_dir, "_wan3_job.json"), "w", encoding="utf-8") as _jh:
            json.dump({"job_id": jid, "ok": bool(env.get("ok")), "account": account,
                       "route": "video.generate/wan3-prime", "ts": round(time.time(), 3),
                       "error": str(env.get("error") or "")[:300],
                       "attempts": res.get("prime_attempts") or 1,
                       "tries": res.get("prime_tries") or [],
                       "reconcile": res.get("prime_reconcile"),
                       "output_path": out}, _jh, ensure_ascii=False, indent=1)
    except OSError:
        pass
    if not env.get("ok"):
        _raise_prime_env_failure(env, jid)
    local = res.get("path") or out
    if not os.path.isfile(local):
        raise WanTimeoutError("wan3-prime 信封说成功但盘上没有 %s（job_id=%s ⇒ 先捞回再判）" % (local, jid))
    from backend import gateway as _gwprov
    _prov = str(((_gwprov.ROUTES.get(("video.generate", "wan3-prime")) or {}).get("provider")) or "没记")
    return {"task_id": jid, "provider": _prov, "lane": "video.generate/wan3-prime",
            "model": str(res.get("model") or res.get("model_used") or ""),
            "result_url": res.get("url") or "", "local_path": str(local), "duration": dur,
            "attempts": res.get("prime_attempts") or 1,
            "bytes": res.get("bytes"), "calls": res.get("calls"), "credits": env.get("credits") or {},
            "note": res.get("note") or ""}


def run_one_segment(
    prompt: str,
    reference_video_path: str,
    image_urls: List[str],
    duration: int,
    output_dir: str,
    segment_index: int,
    resolution: str = "480P",
    aspect_ratio: str = "adaptive",
    audio: bool = True,
    audio_urls: Optional[List[str]] = None,  # 音色参考音频 CDN URL（≤5 段，单段 1-15s，总时长 ≤15s）
    seed: Optional[int] = 42,  # 基础 seed（提高复现性，非绝对确定性）
    api_key: Optional[str] = None,
    on_progress: Optional[callable] = None,
    max_generation_attempts: int = 3,  # 含首次，最大生成尝试次数（seed 42/43/44）
    max_transient_retries: int = 3,  # 瞬时错误（HTTP 429/5xx/网络）单独重试上限
    recovery_on_timeout: bool = False,  # 超时但任务仍 RUNNING：True=继续查询，False=放弃
) -> Dict[str, Any]:
    """执行单段完整链路：上传参考视频 → 提交任务 → 轮询 → 下载。

    分错误类型重试策略：
      ┌──────────────────────────────┬──────────────────┬─────────────────┐
      │ 错误类型                      │ 重试动作          │ seed 处理        │
      ├──────────────────────────────┼──────────────────┼─────────────────┤
      │ 生成失败 / 模型内部失败        │ 重新提交+轮询+下载 │ seed + 1        │
      │ (WanGenerateError)            │                  │                 │
      ├──────────────────────────────┼──────────────────┼─────────────────┤
      │ 任务轮询超时但任务仍 RUNNING   │ 继续查询原任务     │ seed 不变       │
      │ (WanTimeoutError)             │ （不重新提交）     │                 │
      ├──────────────────────────────┼──────────────────┼─────────────────┤
      │ HTTP 429 / 5xx / 网络超时      │ 原请求重试        │ seed 不变       │
      │ (WanTransientError)           │                  │                 │
      ├──────────────────────────────┼──────────────────┼─────────────────┤
      │ HTTP 400 / 参数非法            │ 不重试            │ ——              │
      │ (WanParamError)               │                  │                 │
      ├──────────────────────────────┼──────────────────┼─────────────────┤
      │ 鉴权失败（401/403）            │ 不重试            │ ——              │
      │ (WanAuthError)                │                  │                 │
      ├──────────────────────────────┼──────────────────┼─────────────────┤
      │ 内容/安全审核拒绝              │ 不重试            │ ——              │
      │ (WanContentRejectError)       │                  │                 │
      └──────────────────────────────┴──────────────────┴─────────────────┘

    返回：
        {
            "segment_index": int,
            "rh_task_id": str,
            "reference_video_url": str,
            "result_url": str,
            "local_path": str,
            "duration": str,
            "seed_used": int,
            "attempts": int,
        }
    """
    # 1. 上传参考视频段（走 RunningHub CDN）—— 只做一次
    if on_progress:
        on_progress("UPLOADING", 0, f"上传参考视频段 {segment_index}")
    ref_url = upload_media(reference_video_path, api_key=api_key)

    base_seed = seed if seed is not None else 42
    current_seed = base_seed  # 当前使用的 seed（生成失败时 +1）
    attempt = 0  # 生成失败重试计数
    transient_attempt = 0  # 瞬时错误重试计数（独立计数，不耗尽生成重试配额）

    while True:
        try:
            # 2. 提交 wan3.0 任务
            if on_progress:
                on_progress("SUBMITTING", 0, f"提交段 {segment_index} wan3.0 任务（seed={current_seed}, 生成尝试 {attempt + 1}/{max_generation_attempts}）")
            submit_resp = submit_reference_to_video(
                prompt=prompt,
                video_urls=[ref_url],
                image_urls=image_urls,
                audio_urls=audio_urls,
                duration=duration,
                resolution=resolution,
                aspect_ratio=aspect_ratio,
                audio=audio,
                prompt_extend=False,
                seed=current_seed,
                api_key=api_key,
            )
            task_id = submit_resp["taskId"]

            # 3. 轮询
            if on_progress:
                on_progress("POLLING", 0, f"轮询段 {segment_index} 任务 {task_id}")
            result_data = poll_until_done(
                task_id,
                on_progress=lambda st, el, msg: on_progress("POLLING", el, f"段 {segment_index} 状态 {st}: {msg}") if on_progress else None,
                api_key=api_key,
            )

            results = result_data.get("results") or []
            if not results:
                # SUCCESS 但无结果 —— 当作生成失败，seed + 1 重试
                raise WanGenerateError(f"任务 {task_id} 成功但无结果")
            result_url = results[0].get("url")
            if not result_url:
                raise WanGenerateError(f"任务 {task_id} 结果无 url")

            # 4. 下载
            local_path = Path(output_dir) / f"segment_{segment_index:02d}.mp4"
            if on_progress:
                on_progress("DOWNLOADING", 0, f"下载段 {segment_index} 结果视频")
            download_result(result_url, str(local_path))

            return {
                "segment_index": segment_index,
                "rh_task_id": task_id,
                "task_id": task_id,
                "reference_video_url": ref_url,
                "result_url": result_url,
                "local_path": str(local_path),
                "duration": duration,
                "seed_used": current_seed,
                "attempts": attempt + 1,
            }

        except WanTimeoutError as e:
            # 任务轮询超时但任务可能仍 RUNNING —— 恢复流程：继续查询原任务
            # 不重新提交（避免重复扣费/双任务并行）
            if recovery_on_timeout:
                if on_progress:
                    on_progress("RECOVER", 0, f"任务超时但可能仍 RUNNING，继续查询原任务 {task_id}（不重新提交，seed 不变）")
                # 继续轮询原任务（更长 max_wait）
                result_data = poll_until_done(
                    task_id,
                    max_wait=3600,  # 恢复期再等 1 小时
                    on_progress=lambda st, el, msg: on_progress("RECOVER", el, f"恢复轮询 {task_id} 状态 {st}: {msg}") if on_progress else None,
                    api_key=api_key,
                )
                results = result_data.get("results") or []
                if not results:
                    raise WanGenerateError(f"恢复后任务 {task_id} 无结果")
                result_url = results[0].get("url")
                if not result_url:
                    raise WanGenerateError(f"恢复后任务 {task_id} 无 url")
                local_path = Path(output_dir) / f"segment_{segment_index:02d}.mp4"
                if on_progress:
                    on_progress("DOWNLOADING", 0, f"下载段 {segment_index} 结果视频")
                download_result(result_url, str(local_path))
                return {
                    "segment_index": segment_index,
                    "rh_task_id": task_id,
                    "task_id": task_id,
                    "reference_video_url": ref_url,
                    "result_url": result_url,
                    "local_path": str(local_path),
                    "duration": duration,
                    "seed_used": current_seed,
                    "attempts": attempt + 1,
                }
            # 不恢复 → 直接抛给上层
            raise

        except WanGenerateError as e:
            # 生成失败 → seed + 1 重试
            attempt += 1
            if attempt >= max_generation_attempts:
                if on_progress:
                    on_progress("FAILED", 0, f"生成尝试已达上限（{max_generation_attempts}）: {e}")
                raise
            current_seed = base_seed + attempt
            if on_progress:
                on_progress("RETRY", 0, f"生成失败（seed={base_seed + attempt - 1}）→ seed={current_seed} 重试（{attempt + 1}/{max_generation_attempts}）: {str(e)[:120]}")
            continue

        except WanTransientError as e:
            # HTTP 429/5xx/网络 → 原 seed 重试
            transient_attempt += 1
            if transient_attempt > max_transient_retries:
                if on_progress:
                    on_progress("FAILED", 0, f"瞬时错误重试已达上限（{max_transient_retries}）: {e}")
                raise
            if on_progress:
                on_progress("RETRY", 0, f"瞬时错误（seed={current_seed} 不变）重试（{transient_attempt}/{max_transient_retries}）: {str(e)[:120]}")
            # 短退避后重试
            time.sleep(min(2 ** transient_attempt, 30))
            continue

        # WanAuthError / WanParamError / WanContentRejectError → 直接抛出，不重试
