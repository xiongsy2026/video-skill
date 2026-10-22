"""超级复刻 Wan3.0 - 爆款复刻（不限时长，单段 ≤15s）工作流

模式：单模式「超级复刻」，1:1 复刻爆款视频。
原片超过 15s 时按 15s 切段，每段走同一条 MiMo → 编译 → Wan3，再自动拼接。

流程：
  Step 1: MiMo = 导演 + 广告改写器
          原视频 + 产品图 → 输出一条给 Wan3.0 用的「精准视频提示词」
  Step 2: Wan3.0 = 执行器
          原视频 + 产品图 + MiMo 提示词 → 输出成品视频

严格遵循 wan3-prompt-builder skill v3.1.0 规范：
  - 参考视频单段 1-15 秒（Wan3 API 上限）；超长片切段后逐段送入
  - 单段输入时长 + 输出时长 ≤ 30 秒
  - 提示词含 Shot 分镜 + 图N 参考绑定 + 后置约束
"""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import math
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from config import settings
from backend.services.prompt_reverser import _extract_voiceover
from backend.services.runninghub_client import rh_video_guard


# ── 配置（懒读，便于覆盖）──
# ── 引擎白名单（2026-10-10 用户裁定：现在唯一的 wan3.0 只有一个）─────────────────
# 🔴 判据只在这一份。runner / run_pipeline / 8090 提交口三处都问 engine_lock_error()，
#    谁在别处再抄一份名单，scripts/check-engine-single-source.py 的 I 组会红。
# 为什么必须落到码上：此前 run_pipeline(engine=...) 没有白名单、runner 的 --engine 没有 choices
#    ⇒ engine=h3 能一路走到 step2_h3_execute（RunningHub 导演台），只被 rh_video_guard 挡着，
#      合同 / 表单 / 引擎单一源闸全都看不见这条分叉。
LANE_ENGINES = ("wan3",)
LANE_ENGINE_SPEND = "video.generate/wan3-prime"   # 与合同 spend、表单 engine_lock 同源（I 组核三方）


def engine_lock_error(engine) -> str:
    """纯判据：返回拒因；空串 = 放行。抽成函数是为了让闸能整块抠出来跑值表。"""
    e = str(engine or "").strip()
    if e in LANE_ENGINES:
        return ""
    return ("engine=%r 不在本线白名单 %s 内 ⇒ 拒（0 消耗、不动盘）。"
            "wan3.0 的唯一实现是网关 lane %s（provider yz-prime）；"
            "要换引擎 = 换 skill、重新报价，不是在这里传参切换。" % (e, "/".join(LANE_ENGINES), LANE_ENGINE_SPEND))

def _cfg() -> Dict[str, Any]:
    _sb = _storyboard_lane()          # 分镜腿三元组的唯一源（config 的开关 + 网关路由），调用点与回执都读它
    return {
        "mimo_fps": float(getattr(settings, "REPLICA_WAN3_MIMO_FPS", 4.0)),
        "mimo_resolution": getattr(settings, "REPLICA_WAN3_MIMO_MEDIA_RESOLUTION", "max"),
        "mimo_split_mode": getattr(settings, "REPLICA_WAN3_MIMO_SPLIT_MODE", "scene_cut"),
        # 分镜腿（step1_mimo_parse）实际用的模型名 —— 🔴 唯一源 = _storyboard_lane()，
        # 不再写死 TEXT_MODEL_MIMO_PRO（2026-10-20：小米那把 429 死了，切到网关 video.analyze 那条 lane；
        # 想切回把 .env 的 REPLICA_WAN3_STORYBOARD_ENGINE 写成 mimo）。
        "mimo_model": str(_sb.get("model") or "").strip(),
        "storyboard_lane": _sb,
        "llm_engine": getattr(settings, "REPLICA_WAN3_LLM_ENGINE", "gpt"),
        "resolution": getattr(settings, "REPLICA_WAN3_RESOLUTION", "480P"),
        "aspect_ratio": getattr(settings, "REPLICA_WAN3_ASPECT_RATIO", "adaptive"),
        "audio": bool(getattr(settings, "REPLICA_WAN3_AUDIO", True)),
        "min_trim_duration": 1.0,
        "max_input_duration": 15.0,   # Wan3 单段参考视频上限；超长由 plan_15s_windows 切段拼接
        "concurrency": int(getattr(settings, "REPLICA_WAN3_CONCURRENCY", 3) or 3),
    }


class _GatewayCompletions:
    """`client.chat.completions` 那一层的替身：真请求经 gateway.invoke 发出去。

    🔴 为什么是壳而不是改调用点：调用点用的是 openai SDK 的形状（`completion.choices[0].message.content`），
    换成壳 = 两处真请求点一行都不动；直接改写 = 要把编译器里所有取值重写一遍（那才是引入 bug 的地方）。
    走网关 ⇒ 预扣/结算/台账齐全，`_llm_stamp` 那格因此能如实写 ledger="on"。
    """

    def __init__(self, outer: "_GatewayChatClient"):
        self._o = outer

    def create(self, model: str = "", messages: list | None = None,
               max_tokens: int = 4000, temperature: float = 0.7, **_kw):
        from backend import gateway as _gw
        env = _gw.invoke("text.complete", self._o.purpose, "local",
                         {"messages": messages or [], "max_tokens": int(max_tokens or 4000),
                          "temperature": float(temperature or 0)}) or {}
        res = env.get("result") or {}
        lane = res.get("lane") or {}
        self._o._gw_lane = lane                                  # 哪条腿真的答了，交给 _llm_stamp 落物证
        self._o._gw_credits = env.get("credits") or {}
        text = res.get("text")
        if not (env.get("ok") and text):
            raise RuntimeError("网关文本腿失败（%s/%s）：%s" % (
                "text.complete", self._o.purpose, str(env.get("error") or res.get("error") or "无正文")[:200]))
        msg = type("_M", (), {"content": text})()
        ch = type("_C", (), {"message": msg, "finish_reason": "stop"})()
        return type("_R", (), {"choices": [ch], "usage": res.get("usage") or {},
                               "model": lane.get("model") or model})()


class _GatewayChatClient:
    """openai client 的替身（只实现编译/Lint 用到的那一条链：`.chat.completions.create`）。

    `create(model=...)` 里那个 model 被**忽略** —— 用哪家由 purpose 决定 ⇒ 调用方点不动型号，
    引擎唯一源留在 config/网关路由那一份（这条线历史上被"表单锁 yz-prime、agent 念 RunningHub"咬过）。
    """

    def __init__(self, purpose: str, base_url: str = ""):
        self.purpose = purpose
        self.base_url = base_url
        self._gw_lane: dict = {}
        self._gw_credits: dict = {}
        self.chat = type("_Chat", (), {"completions": None})()
        self.chat.completions = _GatewayCompletions(self)


def _compiler_llm(engine: str = ""):
    """Step 1.5 编译 / Step 2 审查：按 REPLICA_WAN3_LLM_ENGINE 走 magpie / MiMo / Grok / GPT。

    engine 传值 ⇒ 只顶掉这一条腿的全局选择（③审查腿独立选家就走这里），
      取值仍是那四个引擎名之一；🔴 不接受网关 purpose 名 —— `text.complete/mimo` 是经网关扣账的腿，
      `engine=mimo` 是直连厂商不扣账的腿，一个字段两种方言就会造出假保险。

    🔴 2026-10-20：默认档换 magpie（用户裁定"按 8099 的模型接法，但这些模型要放到网关上调用"），
       起因是小米那把 token-plan-cn 整把 429 `quota exhausted`（实测 text.complete/mimo_pro 当场拒）。
       magpie 这一档经 gateway.invoke ⇒ **进账本**；mimo/grok/gpt 三档仍自己拼 OpenAI client 直连厂商
       （ledger="off"，如实标）。模型名/base/key 的唯一源：magpie = 网关路由，其余 = config 字段。
    """
    engine = (str(engine or "").strip().lower()
              or str(_cfg().get("llm_engine") or "gpt").strip().lower())
    if engine == "magpie":
        from backend import gateway as _gw
        try:
            r = _gw.resolve("text.complete", "magpie")
        except Exception as e:                                    # noqa: BLE001
            raise RuntimeError("magpie 路由解不出来（config 的 MAGPIE_* 或 .env 没配？）：%s" % str(e)[:160])
        return _GatewayChatClient("magpie", r.get("base_url") or ""), str(r.get("model") or "")
    if engine == "mimo":
        api_key = (
            os.environ.get("MIMO_API_KEY", "") or str(getattr(settings, "MIMO_API_KEY", ""))
        ).strip()
        base_url = (
            os.environ.get("MIMO_BASE_URL", "") or str(getattr(settings, "MIMO_BASE_URL", ""))
        ).strip()
        model = (
            os.environ.get("MIMO_MODEL", "") or str(getattr(settings, "TEXT_MODEL_MIMO_PRO", ""))
        ).strip()
        if not api_key:
            raise RuntimeError("MIMO_API_KEY 未配置，MiMo 编译器无法运行")
        if not model:
            raise RuntimeError("TEXT_MODEL_MIMO_PRO 未配置（MiMo 编译器模型名的唯一源）")
    elif engine == "grok":
        api_key = (
            os.environ.get("SR_GROK_API_KEY", "") or str(getattr(settings, "SR_GROK_API_KEY", ""))
        ).strip()
        base_url = (
            os.environ.get("SR_GROK_BASE_URL", "") or str(getattr(settings, "SR_GROK_BASE_URL", ""))
        ).strip()
        model = (
            os.environ.get("SR_GROK_MODEL", "") or str(getattr(settings, "SR_GROK_MODEL", "")) or "grok-4.6"
        ).strip()
        if not api_key or api_key.startswith("xai-"):
            raise RuntimeError("SR_GROK_API_KEY 未配置，Grok 编译器无法运行")
    else:
        api_key = (
            os.environ.get("OPENAI_API_KEY", "") or str(getattr(settings, "OPENAI_API_KEY", ""))
        ).strip()
        base_url = (
            os.environ.get("OPENAI_BASE_URL", "") or str(getattr(settings, "OPENAI_BASE_URL", ""))
        ).strip()
        model = (
            os.environ.get("OPENAI_MODEL", "") or str(getattr(settings, "OPENAI_MODEL", "")) or "gpt-5.6-sol"
        ).strip()
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY 未配置，GPT 编译器无法运行")
        engine = "gpt"
    from openai import OpenAI
    return OpenAI(
        api_key=api_key,
        base_url=base_url or "https://hub.nocannobb.com/v1",
        timeout=180.0,
    ), model


def _llm_stamp(client, model: str) -> dict:
    """把"这段 prompt 到底是哪个模型跑的"写成物证。

    🔴 为什么必须有（2026-10-10）：返回信封原本只有 `{prompt, source}` —— `source` 只证明
       "走了 step1_5_compile_prompt 这个函数"，不证明用的哪个模型、哪一家、有没有进账本。
       于是交付时问"是不是对应模型跑的"，谁都要重新猜一遍。
    ⚠️ `ledger` 按【这条腿到底走没走网关】写：magpie 档 = "on"（gateway.invoke，有预扣/结算行）；
       mimo/grok/gpt 三档 = "off"（自己拼 OpenAI/xAI client 直连厂商，不经网关 ⇒ 写成 on 就是假账）。
       只记 base_url 的 host，绝不带 key。
    """
    import urllib.parse as _up
    lane = getattr(client, "_gw_lane", None) or {}
    if lane:
        # 这条腿经 gateway.invoke ⇒ 台账里真有这一行，所以 ledger 写 on；model 用**真答了的那条腿**的模型名
        #（腿表会 fallback，入口那条不一定就是干活的那条 —— 写入口名就是假账）。
        cr = getattr(client, "_gw_credits", None) or {}
        return {"model": str(lane.get("model") or model or "")[:60],
                "provider": "magpie@%s" % str(lane.get("purpose") or "?")[:24],
                "ledger": "on", "lane_attempt": lane.get("attempt"),
                "tokens": cr.get("tokens"), "charged": cr.get("charged")}
    raw = str(getattr(client, "base_url", "") or "")
    host = (_up.urlsplit(raw).netloc or raw).replace("https://", "").replace("http://", "")[:60]
    return {"model": str(model or "")[:60], "provider": host or "未知", "ledger": "off"}


def _lane_cell(capability: str, purpose: str = "") -> str:
    """现读这条【网关账本内】的腿会用哪个模型，拼成回执「模型/路由」那一格。

    🔴 只取 resolve() 的 model / provider 两个键 —— 那一份里带 api_key，整份绝不外传、绝不打印。
    取不到 ⇒ 回空串 ⇒ skill_contract.tick() 拒绝打钩 ⇒ 花钱那一刀之前拒。
    为什么宁可拒也不兜一个默认型号：这一格是"哪个模型跑的"的唯一出处，编一个比空着更坏。
    """
    from backend import gateway as _gw
    try:
        r = _gw.resolve(capability, purpose or None)
    except Exception:                                          # noqa: BLE001
        return ""
    return "%s@%s · 网关账本内" % (str(r.get("model") or "(默认)")[:60],
                                   str(r.get("provider") or "?"))


def _storyboard_lane() -> dict:
    """Step 1 分镜腿（唯一一条【必须吃视频】的腿）用哪家 —— 唯一源 = config.REPLICA_WAN3_STORYBOARD_ENGINE。

    · "gateway"（默认，2026-10-20）⇒ 现读网关 `video.analyze` 的默认路由（agnes-3.0-flash）。
      依据是实测不是猜：同一种 video_url part 打过去，`video_tokens=240`、答对主体颜色（2026-10-20）。
    · "mimo" ⇒ 小米 token-plan 那把直连（2026-10-15 定的那款；额度恢复后 .env 一行切回，不动码）。
    🔴 这一腿不许指 magpie 那三条 free：桥的 chat part 白名单只有 text/image_url/file，
      带 video_url 时三条腿 prompt_tokens 一字不涨、都答"没收到"（模型卡写着 in=…video 也不算数）。
      ⇒ 下面那道 provider 守卫是这条禁令唯一的机器形状，别删。
    ⚠️ api_key 只在调用点用 ⇒ 绝不进物证/回执/日志（这里只带 host）。
    解不出来 ⇒ 回 `{"error": …}` ⇒ 调用点在发任何请求之前拒，**不静默回落到 step1 签名里的 flash 默认值**。
    """
    import urllib.parse as _up
    eng = str(getattr(settings, "REPLICA_WAN3_STORYBOARD_ENGINE", "gateway") or "gateway").strip().lower()
    if eng in ("mimo", "xiaomimimo"):
        base = str(getattr(settings, "MIMO_BASE_URL", "") or "").strip()
        key = str(getattr(settings, "MIMO_API_KEY", "") or "").strip()
        # 模型名：config 的 REPLICA_WAN3_STORYBOARD_MODEL 优先（2026-10-22 用户点名 flash），
        #   没写才回落 TEXT_MODEL_MIMO_PRO —— 保持"这一腿用哪个模型"只有一个出处。
        mdl = (str(getattr(settings, "REPLICA_WAN3_STORYBOARD_MODEL", "") or "").strip()
               or str(getattr(settings, "TEXT_MODEL_MIMO_PRO", "") or "").strip())
        if not (base and key and mdl):
            return {"error": "分镜腿=mimo，但 config/.env 缺 MIMO_BASE_URL / MIMO_API_KEY / "
                             "REPLICA_WAN3_STORYBOARD_MODEL(或 TEXT_MODEL_MIMO_PRO)"}
        host = (_up.urlsplit(base).netloc or base)[:60]
        return {"base_url": base, "api_key": key, "model": mdl,
                "provider": "mimo·%s" % host, "ledger": "off"}
    from backend import gateway as _gw
    try:
        r = _gw.resolve("video.analyze", _gw.default_purpose("video.analyze"))
    except Exception as e:                                       # noqa: BLE001
        return {"error": "分镜腿=gateway，但网关 video.analyze 路由解不出来：%s" % str(e)[:140]}
    prov = str(r.get("provider") or "")
    if prov == "magpie":
        return {"error": "网关的 video.analyze 默认路由现在是 magpie —— 这条腿吃视频，"
                         "magpie 桥会丢 video part ⇒ 拒（默认路由请指 mimo；agnes 已于 2026-10-22 下架，"
                         "别拿丢视频的腿做分镜）"}
    host = (_up.urlsplit(str(r.get("base_url") or "")).netloc or str(r.get("base_url") or ""))[:60]
    return {"base_url": r.get("base_url"), "api_key": r.get("api_key"), "model": str(r.get("model") or ""),
            "provider": "%s·%s" % (prov or "?", host),
            # 🔴 ledger 一律 "off"，即使这条腿的 base/key/model 是从网关路由读来的：
            #    真发请求的是 step1_mimo_parse（BOUNDARY 整文件保护，只调不改）⇒ 没有预扣/结算行，
            #    写 on 就是假账。"值取自哪条 lane"记在下面的 lane 字段里，两件事分开。
            "ledger": "off", "lane": "video.analyze/%s" % (r.get("purpose") or "")}


def _analyze_lanes() -> list:
    """看图腿的【有序链】—— 唯一源 = config.REPLICA_WAN3_ANALYZE_PURPOSE（串形 `purpose[:次数],…`）。

    写一条裸名字（老写法 `mimo_pro`）= 只有一条腿的链，行为与从前一字不差。
    🔴 名字认错（网关 `video.analyze` 下没有这条路由）⇒ 整条链作废、一条都不发：
       半条链比空链危险 —— 会悄悄用一条没人指定过的腿跑出来，而回执还把它署成主腿的名字。
    """
    from backend import gateway as _gw
    out = []
    for piece in str(getattr(settings, "REPLICA_WAN3_ANALYZE_PURPOSE", "") or "").split(","):
        piece = piece.strip()
        if not piece:
            continue
        pur, _, n = piece.partition(":")
        pur, n = pur.strip(), (n.strip() or "1")
        if ("video.analyze", pur) not in _gw.ROUTES:
            return []
        try:
            out.append({"purpose": pur, "tries": max(1, int(n))})
        except ValueError:
            return []
    return out


def _analyze_purpose() -> str:
    """链的【第一条】（主腿）—— 回执格、面板、报价读它；真发请求走 `_analyze_lanes()`。

    🔴 回执那一格与真 invoke 必须读同一个源：以前两处都写死 "mimo_pro"，那把 key 429 死了之后
       形状是"每条素材都分析失败 ⇒ 补跑也失败 ⇒ 120 分熔断空转"，而回执只显示一个查不出来的模型名。
    """
    _ls = _analyze_lanes()
    return str(_ls[0]["purpose"]) if _ls else ""


def _analyze_lane_model(purpose: str) -> str:
    """这条腿现在解出来是哪个模型 —— 现读路由，不写第二份型号字面量。"""
    from backend import gateway as _gw
    try:
        return str(_gw.resolve("video.analyze", purpose).get("model") or "")
    except Exception:                                          # noqa: BLE001
        return ""


def _mimo_cell() -> str:
    """Step1 分镜那条腿的模型名 —— 与调用点【同一份来源】：都读 `_storyboard_lane()`。

    🔴 不抄散文、不写第二份型号字面量：这一格是"分镜到底是哪个模型跑的"的唯一出处。
    解不出来 ⇒ 回空串 ⇒ `skill_contract.tick()` 拒绝打钩 ⇒ 花钱那一刀之前拒（编一个名字比空着更坏）。
    ledger 按这条腿真走没走网关写：mimo 档那一份文件（BOUNDARY 整文件保护、只调不改）自己拼 HTTP ⇒ off；
    gateway 档取的是网关路由的三元组，但**发请求的仍是那份受保护文件** ⇒ 仍如实写"账本外·直连厂商"，
    换成"网关账本内"就是假账（预扣/结算确实没发生）。
    """
    lane = _storyboard_lane()
    if lane.get("error"):
        return ""
    mdl = str(lane.get("model") or "").strip()
    if not mdl:
        return ""
    return "%s@%s · 账本外·直连厂商（值取自 %s）" % (mdl[:60], str(lane.get("provider") or "未知"),
                                                    str(lane.get("lane") or "config 的 MIMO_* 三键"))


# ── ffprobe 工具（本地副本，避免跨模块依赖）──
def _ffprobe_duration(video_path: str) -> Optional[float]:
    """用 ffprobe 探测视频时长（秒）。"""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                video_path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            capture_output=False,
        )
        return float(result.stdout.decode("utf-8", errors="ignore").strip())
    except Exception:
        return None


def validate_input(video_path: str) -> Optional[str]:
    """入口校验：视频存在 + 能探测时长。超长视频由管线按 15s 切段拼接。"""
    if not video_path or not Path(video_path).exists():
        return f"爆款视频不存在: {video_path}"
    dur = _ffprobe_duration(video_path) or 0.0
    if dur <= 0:
        return f"无法探测视频时长，请检查文件是否损坏: {video_path}"
    return None


# ── 视频裁剪（用户选定区间，或 15s 切段）──
def trim_video(
    video_path: str,
    start: float,
    end: float,
    output_path: str,
) -> str:
    """用 ffmpeg 精准裁剪视频区间 [start, end]（单位：秒）。

    使用 -ss + -to 精确到关键帧后再重编码（避免丢帧），输出 H.264 + AAC。
    返回 output_path。
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    duration = max(0.1, float(end) - float(start))
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{start:.3f}",
        "-i", video_path,
        "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        output_path,
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
    return output_path


def validate_trim(
    start: Optional[float],
    end: Optional[float],
    video_duration: float,
    enforce_max: bool = True,
) -> Optional[str]:
    """裁剪区间校验：0 ≤ start < end ≤ video_duration，最短 1s。

    enforce_max=True 时区间不得超过 Wan3 单段上限（15s），供爆款裂变等仍走整段一次的入口。
    超级复刻传 enforce_max=False：超长区间由管线按 15s 切段拼接。
    start/end 都为 None 视为不裁剪。
    """
    if start is None and end is None:
        return None
    if start is None or end is None:
        return "裁剪区间必须同时提供 start 和 end"
    try:
        s, e = float(start), float(end)
    except (TypeError, ValueError):
        return "裁剪区间必须为数字"
    if s < 0 or e <= s:
        return f"裁剪区间非法：start={s} end={e}"
    dur = e - s
    cfg = _cfg()
    min_duration = cfg["min_trim_duration"]
    max_duration = cfg["max_input_duration"]
    if dur < min_duration - 0.05:
        return f"裁剪区间过短：{dur:.1f}s < {min_duration:.0f}s（最短 {min_duration:.0f}s）"
    if enforce_max and dur > max_duration + 0.05:
        return f"裁剪区间过长：{dur:.1f}s > {max_duration:.0f}s（最长 {max_duration:.0f}s）"
    if e > video_duration + 0.05:
        return f"裁剪终点 {e:.1f}s 超过视频时长 {video_duration:.1f}s"
    return None


def plan_15s_windows(
    total: float,
    chunk: float = 15.0,
    ref_min: float = 5.0,
    slack: float = 0.05,
) -> List[Dict[str, float]]:
    """把总时长切成【输出连续、参考可重叠】的窗口（每段两个窗口，各管各的事）：

    · `start`/`end`       = 这一段在**成片时间轴上负责**的那一段 ⇒ 连续、不重叠、每段 ≤15s，
                            拼起来就是整条片子。批准条子的段数与窗口指纹认的是这一份。
    · `ref_start`/`ref_end` = 这一段**喂给模型看的参考窗口** ⇒ 默认等于本段；只有尾段太短才向前借。

    为什么要借（2026-10-22 用户定：**一条片子只走 `ME-WAN3-prime-1080p-1055` 一条模型**，原话"不然会乱"
    —— 两段用两个模型、两种计费口径，画面与账都会漂）：尾巴这一段在时间轴上常常只多出 0.13 秒这种量级，
    而参考窗口只给 0.13 秒 = 模型看不见任何动作事实，只能猜。所以**只借参考窗口**（`10.13-15.13`），
    生成 5 秒、**只留多出来的那 0.13 秒** ⇒ 重复的那 4.87 秒不会进成片（拼接按 start/end 裁）。
    旧写法是"尾段短于 2s 就向前一段借时"（改的是 start/end，整条重排）：它把 15.13s 切成 13.13+2.00，
    于是①批准条子上写明的窗口（0-15s）在盘上不成立了，②本该满 15 秒的主段被压成 13.13 秒 = 白丢复刻质量。
    """
    total = round(float(total or 0.0), 3)
    if total <= 0:
        return []

    def _w(a: float, b: float) -> Dict[str, float]:
        return {"start": round(a, 3), "end": round(b, 3), "ref_start": round(a, 3), "ref_end": round(b, 3)}

    if total <= chunk + slack:
        return [_w(0.0, total)]
    windows: List[Dict[str, float]] = []
    t = 0.0
    while t < total - 1e-6:
        end = min(round(t + chunk, 3), total)
        if end <= t:
            break
        windows.append(_w(t, end))
        t = end
    # 只有最后一段可能短于 ref_min：借参考窗口（不动 start/end ⇒ 时间轴仍然连续、不重复）
    last = windows[-1]
    if (last["end"] - last["start"]) < ref_min - slack:
        last["ref_start"] = round(max(0.0, last["end"] - ref_min), 3)
    return windows


SUBMIT_PLAN_NAME = "_submit_plan.json"


def write_submit_plan(output_dir: str, *, task_id: str, windows, target_duration: float,
                      trim_range, source_video: str, as_duration_chunk: bool,
                      log=None) -> dict:
    """把"这一次到底提交几段、每段是哪条时间窗口"落在盘上（Step 0 那一跳，0 分、不调模型）。

    🔴 为什么必须有主人（2026-10-13 现网两条事故同一个根因）：
       ① 段数没有主人 —— 批准条子的 count 此前只认 `TIMELINE.md` 和 `outputs/seg*`，
          而这条线不产 TIMELINE、段目录嵌在 `outputs/step2_wan3/chunk_01…03`，网关数不到就
          回落"按单段" ⇒ 人批了 1 段的量，机器花了 3 段的钱。
       ② 时间窗口没有主人 —— 客户写「0-15s」，agent 自己挑了一个 `chunk_03`（6.3s 尾段）提交，
          盘上没有任何一份"这次要做的窗口"能被机器读，所以也没有任何一道闸能对得上它。
       现在由【算窗口的那一手】直接写下来：批准读它算段数，提交读它核窗口，两边同一份事实。
    """
    wins = []
    for w in (windows or []):
        a, b = round(float(w["start"]), 3), round(float(w["end"]), 3)
        item = {"start": a, "end": b}
        # 参考窗口只有和"这一段负责的时间"不一样时才写（一样就不制造第二种读法）
        rs, re_ = w.get("ref_start"), w.get("ref_end")
        if rs is not None and re_ is not None and (round(float(rs), 3), round(float(re_), 3)) != (a, b):
            item["ref_start"], item["ref_end"] = round(float(rs), 3), round(float(re_), 3)
        wins.append(item)
    if not wins:
        wins = [{"start": 0.0, "end": round(float(target_duration or 0.0), 3)}]
    import time as _t
    from backend import skill_contract as _skc
    plan = {
        "count": len(wins),
        "windows": wins,
        "target_duration": round(float(target_duration or 0.0), 3),
        "trim": dict(trim_range) if isinstance(trim_range, dict) else None,
        "as_duration_chunk": bool(as_duration_chunk),
        "task_id": str(task_id or ""),
        "source_video": str(source_video or ""),
        "source_sha8": (_skc.sha8(str(source_video)) if source_video else ""),
        "written_at": _t.strftime("%Y-%m-%d %H:%M:%S"),
    }
    d = Path(output_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / SUBMIT_PLAN_NAME
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    if log:
        def _desc(w: dict) -> str:
            tw = "%.2f-%.2fs" % (w["start"], w["end"])
            if "ref_start" in w:
                tw += "（参考 %.2f-%.2fs）" % (w["ref_start"], w["ref_end"])
            return tw
        log("★ 本次提交计划落盘 → %s（%d 段：%s）"
            % (path, plan["count"], "、".join(_desc(w) for w in wins)))
    plan["plan_path"] = str(path)
    return plan


def fit_clip_duration(video_path: str, duration: float, output_path: str, offset: float = 0.0) -> str:
    """把成片裁到目标时长，便于按窗口精准拼接。

    `offset` > 0 用于【参考窗口比本段长】的那种段（尾段把参考窗口借满 5 秒）：
    模型生成的是整条参考窗口，这一段的贡献只是其中靠后的一截 ⇒ 先跳到 offset 再取 duration。
    `-ss` 放在 `-i` **之后**（解码后定位）：放前面是关键帧取整，0.13s 这种短尾会被切歪。
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    dur = max(0.1, float(duration))
    off = max(0.0, float(offset or 0.0))
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", video_path,
    ]
    if off > 0.0:
        cmd += ["-ss", f"{off:.3f}"]
    cmd += [
        "-t", f"{dur:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        output_path,
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
    return output_path


# ── Step 0: media_manifest 编号归属（后端固定，GPT 不得改）──
def _build_media_manifest(
    product_image_paths: List[str],
    model_image_path: Optional[str],
    scene_image_path: Optional[str],
    extra_model_image_paths: Optional[List[str]] = None,
) -> List[Dict[str, str]]:
    """在 Step 0 阶段固定 图N 编号、路径和素材用途。

    清单顺序就是 Wan3.0 reference_image 的唯一上传顺序。
    ★image_id 使用"图N"格式，与 Wan3.0 官方 prompt 引用规范对齐
      （官方文档：prompt 中用"图1""图2"指代 media 数组中对应顺序的素材）。
    """
    manifest: List[Dict[str, str]] = []
    next_image_id = 1
    for i, path in enumerate(product_image_paths or [], start=1):
        manifest.append({"image_id": f"图{next_image_id}", "path": path, "label": f"产品图 {i}", "type": "product"})
        next_image_id += 1
    model_paths = []
    if model_image_path:
        model_paths.append(model_image_path)
    for p in extra_model_image_paths or []:
        if p and p not in model_paths:
            model_paths.append(p)
    for i, path in enumerate(model_paths, start=1):
        label = "模特图" if len(model_paths) == 1 else f"模特图{i}"
        manifest.append({"image_id": f"图{next_image_id}", "path": path, "label": label, "type": "model"})
        next_image_id += 1
    if scene_image_path:
        manifest.append({"image_id": f"图{next_image_id}", "path": scene_image_path, "label": "场景图", "type": "scene"})
        next_image_id += 1
    return manifest


# Step 1.5 固定后置约束：Compiler 输出与 Validator 必须 exact match。
STEP1_5_GLOBAL_CONSTRAINT = (
    "仅在对应 Shot 原商品实际可见时生成目标产品，并将该 Shot 中所有原商品实例替换为目标产品，禁止保留原商品外观；"
    "原商品不可见的 Shot 不新增目标产品。"
    "目标产品实际出现时，产品图片中真实存在的包装文字、品牌标识或产品印刷元素按产品图片保留；"
    "不得额外生成、改写或杜撰产品外观、结构或标识。"
    "当目标产品图片显示的结构数量、层数、部件数量或几何轮廓与原视频产品不一致时，目标产品图片是唯一的产品几何结构来源；"
    "原视频仅用于继承人物动作、手部运动方向、人物与产品的空间关系、镜头运动和时间节奏，不得从原视频恢复或复制原产品的层数、部件数量、尺寸轮廓或结构布局。"
    "画面中不得出现字幕、花字、贴纸、账号水印或原片烧录文字；成片默认无字幕、无水印。产品包装上真实印刷文字按产品图保留。"
)

# ─────────────────────────────────────────────────────────────────
# Step 1.5: Prompt 编译器 = JSON 分镜 → Wan3.0 视觉 Prompt
#   用独立 LLM 调用，把 Step1 的 JSON 分镜编译成符合 10 条规则的纯视觉 Prompt
# ─────────────────────────────────────────────────────────────────
STEP1_5_SYSTEM_PROMPT = """你是 Wan3.0 视频生成提示词**迁移编译器**（Migration-Compiler）。
你做 6 件事，顺序固定：①商品迁移 ②商品相关口播/动作迁移 ③模特迁移 ④场景替换 ⑤图N 引用绑定 ⑥格式编译。
禁止解释、分析、总结或输出推理过程。

【Step 1.5 输入 Contract】
★共 9 个字段；所有字段 Key 必须完整传入。
★Key 存在不等于值有效；必须同时满足下列输入有效性要求。

1. storyboard          【必填】Step 1 MiMo 原始解析结果经过后端 Checkpoint A 验证并 canonicalize 后得到的 canonical storyboard。
                           ★这是本 Compiler 唯一允许读取的 storyboard。
                           ★不得直接传入未经 Checkpoint A 处理的 Step 1 raw storyboard。
2. product_image_paths 【必填】B 产品图路径列表，数量 K≥1。
                           产品图固定对应 media_manifest 中的 图1..K。
                           产品图 + 模特图 + 场景图的总参考图片数量必须为 1~10 张；
                           该数量约束由 Step 0 后端保证。
3. model_image_path    【可选】模特图路径；有值 = 客户要求换脸，对应 media_manifest 中紧随产品图之后的连续 图N。只取脸/五官/发型/肤色。
4. scene_image_path    【可选】背景参考图：客户给的"目标背景"图（静态图，如客户客厅照）。
                            ★背景定调以该图为准：编译出场景描述必须与图中家具/墙面/地板/光线一致，
                              各 Shot 只是机位角度不同（俯拍/平视/特写），背景元素全程不变。
                            ★该图只描述"背景长什么样"，不控制人物、商品、口播。
                            ★背景替换仍走文字：场景环境/背景细节字段按该图重写，原片背景（品牌抱枕、
                              绿植、展示柜、透明椅等参考像素）一律不复现。
                            ★原视频画面中本就存在的真实文字（产品包装印刷、品牌标识、店内真实标牌）
                              不在清除范围；烧录字幕/花字仍按合同清除。
                            ★同时有文字背景要求和背景参考图时：参考图为背景事实源，文字只补充图中没有的细节。
5. product_name        【必填】客户指定的 B 产品名（仅替换商品身份字段，不承担性能/卖点事实源）
6. product_facts       【可选】客户明确陈述的商品事实（如"材质：纯棉"/"功能：防水"）
                           ★仅当客户明确陈述"该商品确实具有 X 属性"时才填入
                           ★客户需求/期望（如"帮我突出防水"）不算商品事实
7. customer_request    【可选】客户需求说明
                           ★不得修改非商品人物动作、原运镜、原转场、非商品口播原文、Shot 结构或任何 Shot 时间
                           ★只允许修改客户明确指定的人物视觉属性、场景属性、整体视觉风格，以及不触碰上述保护字段的其他非商品视觉属性
                           ★不得要求额外新增字幕、标题、水印、贴纸或与目标产品无关的 Logo；这些内容由后期层处理
                           ★目标产品图片中真实存在的品牌标识、包装印刷和产品自身文字，属于目标产品可见外观，不因本规则被主动删除、改写或杜撰
                           ★需要修改动作、运镜或脚本时，另走“重创作”模式，不属于本复刻 Compiler
                           ★不得作为商品事实源、改变 target_duration，或改变镜头结构
                            ★原片烧录的字幕/花字/贴字样式的口播文案只进 speech 字段（由原声音轨承载），严禁写入任何可渲染画面描述字段；画面默认无字幕、无花字、无烧录文字。但原视频画面中本就存在的真实文字（产品包装印刷、品牌标识、店内真实标牌、挂画文字等）属于画面真实内容，按原样保留，不属于烧录字幕，不在清除范围
8. media_manifest       【必填】Step 0 后端固定的 图N 编号归属
9. target_duration     【必填】Step 0 后端用 ffprobe 精确确定的输出时长（浮点秒）
                           ★全局硬参数，本编译器不得改变；Shot 时间轴以此为边界
                           ★不得重新计算、snap、四舍五入或调整任何 Shot 时间；口播时长不够只能压缩商品迁移口播，不得延长视频

【输入有效性】
以下字段不仅 Key 必须存在，而且值必须有效：
- storyboard：非空 Shot 数组
- product_image_paths：非空列表，至少 1 张
- product_name：非空字符串
- media_manifest：非空有效清单
- target_duration：有效正数

★当前版本仅支持单一主要人物 / 单一说话人。
不支持：
- 两个及以上独立主体人物；
- 两个及以上说话人。
允许：
- 不承担主体动作、不参与主要口播的背景或偶然人物。

以下字段 Key 必须存在，但值允许为 null / 空：
- model_image_path
- scene_image_path
- product_facts
- customer_request

【图片可用判定】
“有产品图 / 有模特图 / 有场景图”必须同时满足：
1. 对应 path 非空；
2. media_manifest 中存在对应 图N；
3. 对应实际多模态图片内容已传入模型。
仅 path 非空不算“有图”。
- 产品图片实际内容缺失：输入无效，后端不得调用本编译器。
- model/scene path 非空但实际图片或 manifest 映射缺失：输入不一致，后端不得调用本编译器。
- optional path 为 null/空：正常按无图分支降级。

【路径与视觉事实边界】
product_image_paths、model_image_path、scene_image_path 仅用于后端素材定位。
对应图片的实际视觉内容由后端以多模态图片输入同时提供。
只有实际传入的图片内容可以作为视觉事实来源。
不得：
- 从文件名推断颜色、类别、材质、功能、人物属性或场景；
- 因为 path 非空就假定图片视觉内容已经可用。

【时间字段边界】
target_duration = Step 0 唯一权威时间事实。
Shot 时间只能直接取自 canonical storyboard。
输出时间字符串必须直接复制 canonical storyboard 的开始时间和结束时间值，不得要求固定小数位，不得做任何时间格式转换。
Compiler 不得修改 target_duration，不得 snap，不得重新计算任何 Shot 时间。

【最高原则】
1. storyboard 是**原视频（A产品）**的内容，目标视频按客户明确要求迁移为 B 产品；原视频结构仍是唯一来源。
2. **必须保留** storyboard 中与商品迁移无关的内容：非商品相关口播原文、声音语气、转场、镜头数、时间区间、景别、非商品动作。
3. **必须迁移**所有与原商品直接相关的内容，包括产品身份、画面内容中的商品描述、产品动作、商品交互、商品相关口播和场景描述中的商品位置或状态。
4. 只迁移因商品更换而必须改变的内容；人物动作、镜头、场景及其他非商品内容不得因此被重写。
5. 本编译器追求格式稳定：降低随机性，提高输出稳定性。不脑补、不扩写、不无故省略。只有商品迁移、事实纠错、物理兼容和时长适配明确要求时，才允许做最小必要删减或压缩。

【目标产品事实权威规则】
按商品属性维度分别确定唯一权威来源：
A. 商品身份/类别：product_name 是唯一权威来源。
B. 可见外观：实际传入的产品图片是唯一权威来源，包括颜色、形状、结构、图案、部件数量、包装外观和版型；product_facts 不得覆盖与图片直接可见事实冲突的视觉属性。
C. 非视觉事实：仅 product_facts 可以提供材质成分、功能、性能和功效；产品图片和 product_name 不得推导这些事实。
D. customer_request 不是商品事实来源，不得覆盖 A/B/C 的商品事实。

【目标商品输入一致性】
- 调用方保证 product_name 与 product_image_paths 表示同一个目标商品/SKU。
- Compiler 默认信任该 Contract。
- Compiler 不负责在 product_name 与产品图片明显不一致时自行纠正商品身份、改名或选择其中一方。
- 此类输入冲突属于上游输入错误，后端不得进入正常编译流程。

【事实冲突处理】
- 对可直接观察的视觉属性，以实际产品图片为准。
- 若 product_facts 中出现与产品图片直接冲突的视觉描述，不得写入该冲突描述，不得折中、调和或同时保留两种描述；Prompt 只使用产品图片中直接可确认的视觉事实。
- 对非视觉属性，产品图片不得推翻或补充 product_facts。

- product_facts 是允许使用的非视觉事实，不是必须全部塞入 Prompt 的卖点列表。
- customer_request 不是事实源，只能触发使用已经由对应权威来源确认的事实：
  - 可见视觉事实 → 必须由实际产品图片确认；
  - 非视觉事实 → 必须由 product_facts 确认；
  - 商品身份/类别 → 必须由 product_name 确认。
- customer_request 请求 X：
  - 若 X 已由对应权威来源确认 → 可以在不改变 Shot 结构的前提下最小使用；
  - 若 X 未被对应权威来源确认 → 不使用、不推导、不生成 X。
- 只有当前 Shot 的以下内容实际需要某项事实时，才使用该事实：
  - 商品外观描述；
  - 商品相关动作；
  - 商品交互；
  - 营销功能槽位；
  - 商品相关口播；
  - customer_request 明确要求。
- 不得因为某项事实存在，就机械写入所有 Shot。

【商品可见性继承】
判定某 Shot 中原商品是否可见，以后端【商品可见性时间轴】的 product_visible 为准，与下列字段一致：
- 不可见：人物与产品.产品 = "无" 且 画面动作.产品动作 = "无" 且 商品交互.交互方式 = "无"
- 可见：其余情况

★后端会在 user 消息中给出【后端锁定的商品可见性时间轴】。该表优先级高于自行从 storyboard 推断：
- product_visible=false：本镜头禁止目标产品入画、禁止产品名、禁止产品 图N。
- 原商品不可见时，画面保持该镜原视频内容，不得把无关物体改写成目标产品。
- 原商品不可见的 Shot 不提目标产品、不引用产品图；目标产品从首次 product_visible=true 的 Shot 才入画。
- 产品首次入画时间之前的所有画面，都不得提前注入目标产品。

- 若 product_visible=false：
  - 不得在画面中新增目标产品；
  - 不得新增目标产品动作或商品交互；
  - 不得引用任何产品 图N；
  - 不得修改"画面动作.画面内容"中与原商品无关的部分。
- 若该 Shot 只有商品相关口播但画面中商品不可见：口播仍按任务2迁移，但不得因此让目标商品出现在画面。
- 若 product_visible=true，方可按任务1替换为目标产品并引用对应 图N。

【原商品依赖内容迁移】
当某 Shot 原商品可见时，必须扫描并迁移所有与原商品直接相关的内容：
- “人物与产品.产品”中的原商品身份；
- “画面动作.画面内容”中的商品外观、位置和状态；
- “画面动作.人物动作”中与原商品直接交互的子动作；
- “画面动作.产品动作”；
- “商品交互”；
- 商品相关口播；
- 场景描述中的商品位置、状态或商品专属背景描述。
原商品专属外观、结构、包装和使用方式不得残留；只迁移商品依赖内容，人物动作、镜头、场景及其他非商品内容不得因此被重写。
- 当原商品在当前 Shot 可见时，必须替换该 Shot 中所有可见原商品实例；不得仅替换被手持或画面中心的一个实例，也不得保留其他原商品实例作为背景、对比物或残留物。

【customer_request 权限】
customer_request 只允许影响客户明确指定的：人物属性、场景属性、整体视觉风格，以及其他被明确点名允许修改的非商品属性。
不得改变 target_duration，不得增删、合并、拆分或重排 Shot，不得修改 Shot 时间，不得把愿望当作商品事实，不得引入未经 product_facts 确认的商品性能或功效。
customer_request 永远不得修改非商品人物动作、原运镜、原转场或非商品口播原文；这些内容如需修改，必须另走“重创作”模式，不属于本复刻 Compiler。

【输入字段约定】
storyboard JSON 每个 shot 包含：
- shot: 镜头编号（整数）
- 时间区间: {开始时间, 结束时间, 镜头时长}
- 场景环境: {场景, 背景细节, 光影设定}  ← 原视频场景，按任务4规则继承或迁移
- 人物与产品: {人物, 产品}  ← 原视频产品；产品字段为“无”时必须保持“无”，否则按任务1替换为 product_name
- 画面动作: {画面内容, 人物动作, 产品动作}  ← 保留非商品动作；人物动作中的商品交互子动作和产品动作按 B 产品调整
- 镜头语言: {景别, 构图, 运镜, 转场衔接}
- 音效: 文本
- 口播: 文本（可能为"无"）  ← 保留非商品口播；商品相关口播按 B 产品迁移
- 声音语气: {语速, 语调, 重音, 停顿, 情绪, 呼吸感}  ← 语速/语调/情绪/呼吸感稳定继承；重音/停顿随商品口播合法删改同步处理
- 面部微表情: {眼神方向, 眉眼变化, 嘴角状态, 表情说明}
- 身体微动作: {头部微晃, 重心转移, 手部律动, 肩颈律动}  ← 真人自然律动
- 商品交互: {交互方式, 交互部位, 营销功能}  ← 原视频人物如何与商品互动
- 营销功能: 仅允许以下客观枚举值：外观展示 / 结构展示 / 操作演示 / 使用过程 / 状态变化 / 尺寸/数量展示 / 前后结果对比 / 包装展示 / 无；仅用于判断展示功能槽位，不代表卖点事实，也不是判断商品相关性的唯一依据。不得使用同义词或其他枚举值

【media_manifest（后端确定，GPT 不得改）】
后端在 Step 0 已固定 图N 编号归属：
- 图1..K = 产品图（K=产品图数量）
- 有模特图时：它是产品图之后的下一个连续 图N（只用于换脸）
- 有场景图时：它是模特图之后的下一个连续 图N（只用于背景定调，不控制人物/商品）
GPT 只决定每个 Shot 引用哪个已知 图N，不得重新定义编号归属。

【图片与 图N 对齐规则】
- 实际多模态图片与 图N 的身份只能依据 media_manifest 确定。
- 不得根据图片在上下文中的出现顺序自行重新编号。
- Prompt 使用 图N 前，必须确认该 N 在 media_manifest 中属于对应素材类型。

【任务1：商品迁移（身份字段替换）】
1. ★product_name 仅替换原商品实际可见且“人物与产品.产品”不是“无”的 Shot 中的**商品身份字段**（包括占位符和原产品名）。
   - 若“人物与产品.产品”="无"，不得替换为 product_name，必须继续保持“无”。
   - 例：原产品="顺风顺水航空杯" → 替换为 product_name
   - 例：占位符="（待识别产品）" → 替换为 product_name
2. 同一 prompt 内产品名跨 Shot 一致。
3. ★商品的动作、结构描述、卖点不在此步处理，统一进入任务②语义迁移：
   - 商品相关动作 → 任务②按 B 产品可确认事实调整
   - 商品结构/卖点 → 任务②按可用事实源改写或删除
4. 从产品图识别 B 产品可见特征（图案/形态/颜色），仅用于描述产品外观，不改产品名。

【任务2：商品相关口播/动作迁移】★核心新增
1. 商品相关性必须综合判断：人物与产品.产品、画面动作中的商品描述/产品动作、商品交互、口播中的商品指代或商品事实、营销功能。
   “营销功能”只是辅助字段，不是商品相关性的唯一判断依据。
2. **可用事实源（只能使用以下三类中"实际存在且明确支持"的信息）**：
   ★某一事实源缺失时仅忽略该来源，不影响使用其他有效来源。
   a. 产品图中直接可确认的视觉事实（图案/形态/颜色/版型/结构）
   b. product_name：仅用于商品身份/类别命名（如"冲锋衣"/"连衣裙"），不承担性能/卖点事实源
      ★即使产品名含形容词（如"超防水冲锋衣"），也不得直接把"超防水"当作硬事实写入卖点口播
   c. product_facts：负责非视觉商品事实（如"材质：纯棉"/"功能：防水"）
      ★仅当客户在此明确陈述"该商品确实具有 X 属性"时才可作为事实源
      ★customer_request 不作为商品事实源（客户需求/期望不等于商品确实具有该属性）
3. **禁止从外观推导的不可见属性**：弹力、舒适、透气、防水、保暖、显瘦、功效、材质成分、手感、味道、使用寿命等。这些不能仅凭图片确定，一律不得写入迁移后的口播/动作。
   ★例外：若 product_facts 中明确陈述了某属性（如"防水"），则可写入；仅凭图片或产品名推导则不可。
4. **商品强相关口播**：先区分商品相关部分和非商品部分。若原口播包含 A 产品的具体商品事实，而该事实无法由 B 产品对应的目标权威事实源确认：
   - 优先仅删除或替换无法确认的词或短语；
   - 保留其余已确认内容和原句结构；
   - 若整句完全由无法确认的商品断言组成，允许删除整句商品相关口播；
   - 不得因为“目标事实源没有否定”就继续保留该断言；
   - 不得把“未知”当成“已确认”；
   - 商品相关口播只能使用目标事实源已确认的内容；非商品口播必须逐字保留。
   - 不得脑补产品图上看不见的特征
5. **口播声音属性迁移**：
   - 稳定声音属性：语速、语调、情绪（表达强度）、呼吸感，必须保持 storyboard 原值。
   - 文本依赖属性：重音、停顿，默认保持 storyboard 原值。
   - 若重音或停顿对应的商品词语在任务2中被合法删除或替换，只允许删除已经失去对应文本的重音或停顿；不得新增重音词，不得重新设计停顿结构。
6. **商品强相关动作**：
   - 保留原 Shot 的人物姿态、动作顺序、手部运动方向和展示功能，能保留多少保留多少。
   - 若原商品动作与目标商品物理结构不兼容，仅删除无法成立的商品专属子动作。
   - 只有产品图片直接可见结构或 product_facts 明确支持时，才能替换为新的商品子动作。
   - 若不存在唯一且有事实依据的替代动作，不得创造新的使用方式；仅在输入事实支持时，降级为静态持有或展示状态。
   - 不得为了让广告更合理而新增主动作，不得推导弹力、韧性等不可见属性。
   - 人物动作中与商品无关的部分原样保留。
   - 人物动作中与商品直接交互的子动作按本任务的商品强相关动作规则处理。
   - 不得因处理商品相关子动作而重写整个人物动作。
7. **商品相关音效迁移**：
   - 非商品音效（环境声、BGM、与商品无关的声音）原样保留。
   - 商品动作依赖音效：若对应原商品动作仍保留，则保留；若对应商品动作已因迁移被删除，则同步删除。
   - 新的目标商品专属音效属于非视觉事实，只有 product_facts 明确提供对应声音事实时才能新增或替换。
   - 产品图片、product_name、customer_request 均不得推导商品专属声音；不得自行创造咔哒声、撕裂声、按压声、清脆声等新商品音效。
8. **非商品相关内容**：与商品无关的口播（如"姐妹们今天挖到宝了"）/动作（如"走向镜头"）一律保留原样。
9. **迁移边界**：只迁移"因为换产品而必须改变"的语义，不得自由创作。
10. ★**输出闭环**：任务②迁移后的新口播/动作必须用于最终 prompt 输出；非商品相关口播逐字保留原文。
   - 商品相关口播：用②迁移后的新口播（禁止又改回原口播）
   - 非商品相关口播：逐字保留原文（禁止以"统一风格"为由改写）
   - 时长不够时：只允许压缩②生成的新口播，非商品原口播不得改写/删减

【任务3：模特迁移】有模特图 = 客户要求换脸
若【图片可用判定】确认 model_image 可用（按 media_manifest 中产品图之后的连续 图N）：
1. ★**只换脸**：用模特图替换主体人物的脸型、五官、发型、肤色。忽略模特图中的服装与配饰。成片服装只来自产品图。不得用模特图的体型、身材覆盖原视频人物体型。
   - 从模特图提取脸型、眉眼、鼻型、嘴型、发型、发色、肤色，在 Shot prompt 中**显式描述**，并标注「严格按 图N」。
   - 不得仅用「图N 中的同一位女性」弱引用；弱引用会导致 Wan3.0 保留参考视频原人物面部。
   - 仅在当前 Shot 脸部实际可见时描述五官；仅手部入镜的 Shot 不描述面部。
2. **同一人物跨 Shot 重复出现时，面部身份保持一致**（同一张脸）；当前 MVP 仅支持单一主要人物。
3. ★覆盖优先级：有模特图时，模特图只控制面部身份（脸/五官/发型/肤色）；姿态、主动作、手势、运动轨迹、体型轮廓继承原视频。customer_request 仅覆盖明确指定的人物属性。无模特图时完整继承原人物。
4. **穿戴类商品**：服装/鞋/包/眼镜/首饰由产品图控制对应穿戴区域，不得从模特图继承衣服。
5. 若无模特图：完整保留原人物（含脸），不引用模特 图N。
6. model_image 只控制主体人物的脸/发型/肤色，不控制衣服、体型和未入镜部位。
7. 背景中偶然出现、且不是主体人物的其他人物不得被替换成目标模特。
8. 不得因为上传模特图而新增、删除或统一背景人物。
9. 原 Shot 仅手部入镜时，只保持手部动作，不新增脸或发型；手部肤色可按模特图调整但不描述面部。
10. 原 Shot 未出现的人体部位不得因为模特图而新增。
11. ★有模特图时，每个主体人物**脸部实际出现**的 Shot 必须出现模特 图N，写「严格按 图N」，并显式描述五官/发型/肤色。漏写模特 图N 或只写「图N 中的人」视为编译失败。

【任务4：场景迁移】换背景支持两种输入，可单独用也可叠加：
A. 背景文字（customer_request 中的「背景要求」或等价场景描述）
B. 场景参考图（media_manifest 中的场景 图N，客户给的目标背景图）
1. **有场景参考图**：背景定调以该图为唯一事实源。各 Shot 的场景环境/背景细节字段按该图重写（家具/墙面/地板/光线/挂饰与图一致），各 Shot 只是机位角度不同（俯拍/平视/特写），背景元素全程不变。禁止把人贴在一张背景静帧上循环；不得引用该图里的人物/商品。
2. **有背景文字、无场景图**：按文字改写各 Shot 的场景身份；人物站位、动作、运镜、构图、人物与产品空间关系继承原视频。
3. **场景图 + 背景文字同时有**：场景图为背景事实源，文字只补充图中没有的细节；冲突时以图为准。
4. **两者都无**：完整继承 storyboard 原场景及其真实场景变化，不做任何修改。
5. 光影默认保留原 Shot；只有目标背景与原光影物理不兼容时才做最小必要适配。
6. 背景中的参考像素（原片品牌抱枕、绿植、展示柜、透明椅等）一律不复现，按目标背景重写。
7. 不得仅因未给背景输入，就擅自删除 storyboard 中真实存在的场景变化。

【图片职责边界】
- 产品图只控制目标产品（含成片服装/穿戴）。
- 模特图只控制目标人物的脸、五官、发型、肤色。
- 场景图只控制背景（家具/墙面/地板/光线）；图中的人物、商品、文字一律不得继承。
- 产品图中的背景或人物不得继承；模特图中的商品、衣服或背景不得继承。

【任务5：图N 引用绑定】
1. media_manifest 编号由后端固定，不得修改。
2. 调用方保证 product_image_paths 中全部产品图片属于同一个目标 SKU；本编译器默认信任该 Contract。
3. 同一 SKU 的产品本体、外包装盒、内包装或其他明确的物理形态可以分别由不同产品图表示；若输入或 customer_request 明确给出形态与 图N 的对应关系，必须按该关系绑定，不得把不同形态当作普通角度图，也不得互相替换。
4. 除非输入或 customer_request 明确给出形态映射，图1..K 才按同一商品的不同角度、状态或细节参考处理，不表示多个 SKU 或多个商品实例。
5. 本编译器不负责判断图片是否同 SKU，不根据视觉差异自行拆分 SKU，也不因角度、颜色、曝光或包装状态不同而推断为不同商品；不同 SKU 输入由上游处理。
6. 图1 是 media_manifest 中第一个产品图，也是主产品图；不表示所有 Shot 必须只引用 图1。
7. **每个 Shot 根据展示角度和细节选择最相关的 1~2 张商品参考图**，不要求始终引用同一 图N：
   - 正面镜头 → 优先匹配正面图
   - 侧面/背面/细节镜头 → 优先匹配对应参考图
   - 若当前 Shot 无法可靠判断应匹配哪个产品角度或细节，使用 图1；不得随机选择其他 图N，不得为了增加参考数量而额外加入第二张图片。
   - 只有当前 Shot 的视角或部位能够明确匹配某个其他产品图时，才优先使用对应 图N。
   - 同一商品跨 Shot 必须保持同一 product identity（同一件商品），但可引用不同 图N。
7. **引用多个商品图表示"共同定义同一个商品外观"**，不得解释为画面中出现多个商品实例。
8. 画面中目标商品实例数量完全由 storyboard 决定；图N 只定义目标商品外观，不定义商品实例数量。
9. 即使画面并列展示多个同款商品，也仍只选择当前视角最相关的 1~2 张产品参考图。
10. 对比衬托 Shot 中，如果目标商品实际可见，仍必须引用当前视角相关的有效产品 图N；只有目标商品完全不可见时才不引用产品图。
11. 每个目标商品可见 Shot：根据当前角度选择最相关的 1~2 个产品 图N；需要视觉约束时必须在该 Shot 内明确引用；不要求所有 Shot 使用同一个 图N，也不要求所有上传产品图都被使用。
12. 每个 Shot 只描述该 Shot 实际需要且与当前视角相关的目标产品外观。
13. 最终 Prompt 中出现的目标产品外观必须与产品图片一致。
14. 不得机械重复全部商品视觉特征，不得描述当前角度不可见或与该 Shot 无关的商品细节。

【任务6：格式编译】
把迁移+替换后的 storyboard 编译成强制语法字符串。

【强制输出语法】
**每个分镜必须在一行内输出**（禁止跨行换行），格式如下：

Shot N [start-ends]: 景别, <画面内容>[, <非口播声音>][; "口播文案" [character1, 语速=..., 语调=..., 重音=..., 停顿=..., 情绪=..., 呼吸感=...]][; <转场指令>];

其中：
- N = shot 编号
- start-ends = 直接复制 canonical storyboard 的时间区间.开始时间 / 结束时间值；不得要求固定小数位或重新计算时间。若 Wan Prompt 需要特定字符串格式，格式化必须由后端在调用 Compiler 前确定性完成，GPT 只复制传入值
- 景别 = 镜头语言.景别 字段值（保留原视频景别）
- <画面内容> 必须整合：
  1. 任务4迁移后的场景（有背景文字则按文字写场景；无则继承原场景）。不得引用场景 图N。
  2. ★任务3迁移后的主体人物；模特图可用且脸部实际出现时：必须包含对应模特 图N，并**显式描述**脸型/眉眼/鼻型/嘴型/发型/发色/肤色，标注「严格按 图N」；不得仅用「图N 中的女性」弱引用，不得用模特图体型覆盖原片。仅手部入镜 Shot 不描述面部。
  3. 任务1/2迁移后的目标产品；原商品可见时，包含当前视角匹配的产品 图N；原商品不可见时不得出现目标产品或产品 图N。
  4. 迁移后的商品相关动作 + 原样保留的非商品动作。
  5. storyboard 原构图、运镜、面部微表情和已记录身体微动作。
  6. customer_request 明确允许修改的整体视觉风格或其他非商品视觉属性。
  不得遗漏任务3、任务4已经确定的迁移结果，不得新增未被输入事实支持的内容。
- "口播文案" = 必须双引号包裹迁移后的口播；若口播不存在有效可保留内容，则省略该段
- 声音属性 = 口播存在时，character1 永远保留；按固定顺序只输出非“无”的字段：语速 → 语调 → 重音 → 停顿 → 情绪 → 呼吸感；稳定声音属性保持原值，重音/停顿按任务2规则同步删除，不得总结为心理词
- 若至少一个声音属性非“无”：输出 `[character1, 语速=..., 语调=...]`，仅列出非“无”项
- 若六项声音属性全部为“无”：输出 `[character1]`
- 音效 = 按任务2的商品相关音效迁移规则处理；非商品音效原样保留，商品动作依赖音效随对应动作同步保留或删除；无音效时同时省略其前置分隔符
- <转场指令> = 见“转场规则”；无转场时不输出转场文本

【身体微动作编译】
- 只使用 storyboard 已明确记录的可见微动作。
- 可以压缩措辞，但不得新增 storyboard 中不存在的微动作。
- “无”“未入镜”“看不清”不得转换成新的动作描述。
- 不得为了让人物更自然而自行添加手指、头部、肩颈或重心动作。
- 不得改变原动作意图。

【可选段标点规则】
- 无音效：同时省略“<非口播声音>”及其前置分隔符。
- 无有效口播：同时省略整个“口播”及其前置分隔符。
- 无转场指令：不输出转场文本及其前置分隔符。
- 无论可选段是否存在，每个 Shot 最终只以一个“;”结束。
- 禁止产生连续空分隔符“,,”“;;”或空占位。

【转场规则】
“转场衔接”描述前一个 Shot → 当前 Shot 的实际连接方式。
- Shot 1 不输出转场指令。
- Shot N（N≥2）读取当前 Shot 的“转场衔接”字段。
- “硬切” → 输出 “Hard cut transition”。
- “无”或“看不清”或为空或缺失 → 不输出转场指令。
- 其他明确转场 → 输出对应转场术语。
- 最后一个 Shot 与普通 Shot 相同；若它从前一 Shot 硬切进入，仍然输出 “Hard cut transition”。
- 不得把“无”默认转换为硬切，不得预测当前 Shot 之后的转场。

【示例】
输入 storyboard:
{"shot":1, "时间区间":{"开始时间":"00:00.00","结束时间":"00:02.00", "镜头时长":"2.0s"}, "镜头语言":{"景别":"中景","转场衔接":"无"}, "场景环境":{"场景":"室内木纹桌面"}, "人物与产品":{"人物":"手部入镜","产品":"（待识别产品）"}, "画面动作":{"画面内容":"手从叠放杯子中取出一个杯子", "产品动作":"被单手持握展示"}, "口播":"家里招待客人，就用这种航空杯", "声音语气":{"语调":"平稳"}, "商品交互":{"交互方式":"手从顶部取杯","营销功能":"外观展示"}}

附产品图：图1 = 杯身印"顺风顺水"字样的航空杯
product_name = 顺风顺水航空杯
customer_request: 户外露营场景背景

输出（单行）:
Shot 1 [00:00.00-00:02.00]: 中景, 户外露营木质桌面上右手从叠放杯子中取出一个顺风顺水航空杯 图1; "家里招待客人，就用这种航空杯" [character1, 语调=平稳];

【时间轴前置条件】
进入本 Compiler 的 storyboard 已由后端 Checkpoint A 验证并 canonicalize。
Compiler 不负责判断是否回源、不负责修复时间、不负责 snap。
若后端发现时间 Contract 不满足，不得调用 Compiler。
Compiler 只复制 canonical storyboard 的 shot、start、end；时间值直接复制，不要求固定小数位。
Shot 数量和每个 Shot 时长以原视频实际镜头结构为准，不人为设置上下限。

【严禁事项】
❌ 严禁修改非商品相关口播原文（如"姐妹们今天挖到宝了"）
❌ 严禁修改 storyboard 中的稳定声音属性：语速、语调、情绪（表达强度）、呼吸感
❌ 重音、停顿仅可因对应商品词语被任务2合法删除或替换而同步删除，不得新增或重新设计
❌ 严禁修改非商品动作描述
❌ 严禁脑补产品图上看不见的产品特征
❌ 严禁省略 storyboard 已明确记录的身体微动作（头部微晃/重心转移/手部律动/肩颈律动）
❌ 严禁新增 storyboard 中不存在的身体微动作或主动作
❌ 严禁把“无”“未入镜”“看不清”转换成新的动作描述
❌ 严禁自行新增/删除/合并/重排分镜
❌ 严禁重新定义 media_manifest 编号归属（后端已固定）
❌ 严禁输出"参考图/替换/保留原视频"等元指令
❌ 严禁输出字幕、标题、水印、花字或贴纸；严禁复现原视频烧录的字幕、花字、账号水印。成片默认无字幕、无水印。产品图片中真实存在的包装文字、品牌标识除外，必须按产品图片保留；原视频画面中本就存在的真实文字（包装印刷/品牌标识/店内真实标牌/挂画文字）按原样保留，不属于烧录字幕，不在清除范围。
❌ 严禁输出推理、分析、检查过程
❌ 严禁输出 markdown 代码块（```）或 JSON 包装
❌ 严禁输出标题（如"修正版："、"编译结果："等）
❌ 严禁跨行输出单个分镜（每个 Shot 必须一行）
❌ 严禁遗漏 Shot（输入几个分镜就输出几行 Shot）
❌ **口播段输出规则**：只要迁移后的口播仍有有效可保留内容，就必须输出 `"口播文案" [character1, ...]` 段；character1 永远保留，六项声音属性全部为“无”时输出 `[character1]`。
❌ 非商品口播必须逐字保留；不得因商品口播被合法删除而遗漏非商品口播内容。
❌ 若任务2已合法删除全部商品相关口播，则允许省略口播段；非商品口播绝不允许因此遗漏。
❌ 严禁无依据地随机选择 图N（必须按展示角度/细节语义匹配）
❌ 严禁把"引用多张商品图共同定义同一商品外观"解释为画面中出现多个商品实例
❌ 严禁把 图N 当作商品实例数量；画面中目标商品实例数量完全由 storyboard 决定
❌ 严禁在目标商品完全不可见时引用产品图；目标商品实际可见时必须引用当前视角相关的有效产品 图N

【后置约束段】
所有分镜行之后，必须追加以下固定单行字符串，内容必须与该字符串 exact match：
仅在对应 Shot 原商品实际可见时生成目标产品，并将该 Shot 中所有原商品实例替换为目标产品，禁止保留原商品外观；原商品不可见的 Shot 不新增目标产品。目标产品实际出现时，产品图片中真实存在的包装文字、品牌标识或产品印刷元素按产品图片保留；不得额外生成、改写或杜撰产品外观、结构或标识。画面中不得出现字幕、花字、贴纸、账号水印或原片烧录文字；成片默认无字幕、无水印。产品包装上真实印刷文字按产品图保留。

【输出严格约束（违反任一条直接判失败）】
1. 只输出编译后的 prompt 正文，不要任何前言、后记、解释、分析
2. **每个分镜必须在一行内完成**（行内不得有换行符）
3. 行末分号 `;`（最后一个 Shot 也是分号，因为后面还有后置约束段）
4. 最后一行必须是【后置约束段】中定义的固定单行字符串，且必须与该字符串 exact match（无分号结尾）
5. 第一字节必须是 S（Shot 1 的 S），禁止添加任何前置字符
6. 输出字数应与 storyboard 信息量匹配，不得大幅扩写或缩写
7. 禁止输出"以下是""编译结果""修正版"等引导词
8. Shot 数量必须与输入 storyboard 的 shot 数量一致
9. **目标商品可见 Shot 必须引用当前视角相关的有效产品 图N**；不要求所有上传产品图全部出现在 Prompt
10. ★**模特图必须被引用且必须显式描述面部身份**：若图片可用判定确认模特图可用，所有「主体人物脸部实际出现」的 Shot 必须引用对应模特 图N，并显式描述脸型/眉眼/发型/肤色，标注「严格按 图N」；不得仅用「图N 中的女性」弱引用。仅手部入镜 Shot 不描述面部。模特图不控制衣服和体型。
11. **换背景只走文字**：有背景文字时按文字写场景，不得引用场景 图N；无背景文字时继承原场景。
12. 最终完整 Prompt 长度不得超过 20000 字符，包括全部 Shot 行、换行符和后置约束段。
    若信息较多接近上限：优先压缩重复视觉描述；不得删除 Shot、受保护的非商品口播、必要 图N、原视频核心动作、运镜、转场或声音信息，不得修改时间。

【口播】
Wan3 成片跟原片声音，口播不得改写。
每个 Shot 引号内必须逐字复制 storyboard 该镜「口播」字段，一个字都不能改。
禁止商品迁移改写口播、禁止跨镜合并、禁止补全后文、禁止换成产品卖点新词。
任务②只迁移商品相关动作、交互和画面描述，不改引号内口播。

**只输出 prompt 正文本身。第一字节必须是 S。每个 Shot 必须单行。**
"""


async def step1_5_compile_prompt(
    storyboard: List[Dict[str, Any]],
    target_duration: float,
    media_manifest: List[Dict[str, str]],
    customer_request: Optional[str],
    product_image_paths: List[str],
    model_image_path: Optional[str],
    scene_image_path: Optional[str],
    product_name: str = "",
    product_facts: Optional[str] = None,
    extra_compile_note: Optional[str] = None,
    extra_model_image_paths: Optional[List[str]] = None,
    background_override: Optional[str] = None,
    on_log: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Step 1.5: GPT Prompt 编译器
    用 GPT 把 Step1 MiMo 解析的 storyboard JSON + 素材图
    编译成符合 Wan3.0 强制语法的 prompt（含口播+语气+转场）。

    与 MiMo 直拼相比：
    - MiMo 直拼：经常丢口播、语气、转场标记（不稳定）
    - GPT 编译：按强制语法确定性输出（格式稳定）

    ★Input Contract（视觉多模态输入，非纯路径）:
      product_images : 实际多模态图片内容（Base64 编码，附在 messages.content 中）
                       path 参数（product_image_paths）只是后端文件定位信息，
                       调用 GPT 前会自动转 Base64 给模型查看
      model_image    : 实际多模态图片内容 | null
                       path = model_image_path
      scene_image    : 实际多模态图片内容 | null
                       path = scene_image_path
      media_manifest : 图N ↔ 素材身份映射（不是图片内容，是编号归属）
                       如：[{"image_id": "图1", "label": "产品正面", "path": "/tmp/..."}]

      本地 path 不能代替多模态输入：
        - 若 product_image_paths / model_image_path / scene_image_path 中某个文件不存在
          → 该图片不会出现在 GPT 输入中（既不报错，也无视觉内容）
          → 编译器只能基于文本描述工作，迁移质量会下降
        - 调用方应在 Step 0 已确保所有 path 文件存在

    Returns:
        {
            "prompt": str,            # 编译后的 Wan3.0 完整 prompt
            "source": "step1_5_gpt_compiler",
            "model": str,             # 实际用的模型名（_compiler_llm 解析出来的那一个）
            "provider": str,          # base_url 的 host（不含 key）
            "ledger": "off",          # 🔴 这条腿不在网关账本上，如实标
        }
        ★主产品图固定为 media_manifest 中的 图1；本函数不接收或输出主图编号变量
    """
    def log(msg: str) -> None:
        if on_log:
            on_log(msg)
        print(f"  [Step1.5·编译器] {msg}", flush=True)   # 不写厂商标：这条腿现在是 magpie/deepseek，
        #   前缀里挂"Grok"会让 agent 和客户照着一句假话念引擎（同"报价不许凭记忆写厂商标"一条规矩）。

    product_paths = [path for path in (product_image_paths or []) if path]
    if not product_paths or any(not os.path.isfile(path) for path in product_paths):
        raise RuntimeError("产品图片实际内容缺失，输入无效，不得调用 Step 1.5 Compiler")

    manifest_by_path = {item.get("path"): item for item in (media_manifest or [])}
    expected_optional = (
        ("model_image_path", model_image_path, "model"),
        ("scene_image_path", scene_image_path, "scene"),
    )
    for field_name, path, expected_type in expected_optional:
        if path and (
            not os.path.isfile(path)
            or path not in manifest_by_path
            or manifest_by_path[path].get("type") != expected_type
        ):
            raise RuntimeError(f"{field_name} 与实际图片或 media_manifest 映射不一致，不得调用 Step 1.5 Compiler")
    for extra in extra_model_image_paths or []:
        if extra and (
            not os.path.isfile(extra)
            or extra not in manifest_by_path
            or manifest_by_path[extra].get("type") != "model"
        ):
            raise RuntimeError("extra_model_image_paths 与实际图片或 media_manifest 映射不一致，不得调用 Step 1.5 Compiler")
    if any(path not in manifest_by_path for path in product_paths):
        raise RuntimeError("产品图片与 media_manifest 映射不一致，不得调用 Step 1.5 Compiler")

    client, model = _compiler_llm()

    # media_manifest 文本——明确告诉 GPT 附图对应哪个 图N
    # 时间字段必须原样复制 storyboard，不得重新计算或重新分配。
    manifest_lines = []
    img_idx = 0
    for item in media_manifest:
        image_id = item["image_id"]
        label = item["label"]
        # 显式标注 media_manifest 对应的 图N 身份
        img_idx += 1
        manifest_lines.append(f"- {image_id} = {label}（对应已传入的多模态图片）")
    manifest_text = "\n".join(manifest_lines) if manifest_lines else "(空，无参考图)"

    # 完整传 storyboard JSON（保留所有字段：口播/声音语气/转场衔接/面部微表情/身体微动作等）
    storyboard_json = json.dumps(storyboard, ensure_ascii=False, indent=2)

    # 构造 user 消息（文本 + 图片）
    content_list: List[Dict[str, Any]] = []

    text_parts = [
        f"target_duration（Step 0 全局硬参数）: {target_duration!r}s",
        "  ★此值由 Step 0 后端用 ffprobe 精确确定并锁死，本编译器不得改变。",
        "  ★进入本 Compiler 的 storyboard 已由后端 Checkpoint A 验证并 canonicalize。",
        "  ★时间轴前置条件：Compiler 不负责判断是否回源、不负责修复时间、不负责 snap；若后端发现时间 Contract 不满足，不得调用 Compiler。",
        "  ★Shot 1.start、每个 Shot 的 start/end、最后 Shot.end 必须逐字段复制 canonical storyboard，不得重新计算或重新分配时间。",
        "  ★不得要求固定小数位；若 Wan Prompt 需要特定字符串格式，必须由后端在调用 Compiler 前确定性完成，GPT 只复制传入值。",
        "  ★只能按原有 Shot 时间编译内容，不得重新分配时间或改变 target_duration。",
        "  ★口播必须逐字复制 storyboard，不得缩短、改写或换成新词。", 
        f"\nmedia_manifest:\n{manifest_text}",
    ]
    if customer_request and customer_request.strip():
        text_parts.append(
            f"\n客户需求说明（仅用于场景/风格/特殊要求，不作商品事实源）:\n{customer_request.strip()}"
        )
    # ★Plan B：背景强制要求通道（指那打那）
    # - 单独字段，独立于 customer_request，权重更高
    # - 触发 Compiler 任务4 强化策略：前置描述 + 禁令清单 + 末尾闭环
    if background_override and background_override.strip():
        original_bg_keywords: List[str] = []
        for shot in storyboard:
            env = shot.get("场景环境") or {}
            if isinstance(env, dict):
                details = env.get("背景细节") or env.get("场景") or ""
                if isinstance(details, str) and details.strip():
                    original_bg_keywords.append(details.strip())
        original_bg_text = "；".join(original_bg_keywords) if original_bg_keywords else "原视频背景"
        text_parts.append(
            "\n【background_override 客户硬要求 · 必须执行】\n"
            f"客户明确要求替换背景为：{background_override.strip()}\n"
            "★任务4强化策略（必须执行）：\n"
            "1. 各 Shot 行最前面写新背景描述（不允许放在末尾或省略）；\n"
            f"2. 显式禁令：严禁复刻参考视频原背景，原背景元素不得出现：{original_bg_text}；\n"
            "3. 末尾闭环：再次声明'背景必须为上述新场景，严禁复刻参考视频原背景'；\n"
            "4. 有场景图时背景定调按 任务4 的场景图分支执行（场景图为背景事实源，本字段只补图中没有的细节）；无场景图时按本字段文字执行；\n"
            "5. 人物动作/运镜/口播不变，仅场景环境按本字段执行。\n"
            "★本字段优先级高于 customer_request 中的背景描述，按本字段执行。"
        )
    # 场景参考图提示：有场景图时显式告知编译器背景定调以该图为事实源
    scene_ref = next((m for m in media_manifest if m.get("type") == "scene"), None)
    if scene_ref:
        text_parts.append(
            "\n【场景参考图】media_manifest 中的场景 图N（"
            f"{scene_ref.get('image_id')}）= 客户给的目标背景图。"
            "★按 任务4 的场景图分支执行：各 Shot 的场景环境/背景细节以该图家具/墙面/地板/光线为事实源重写，"
            "各 Shot 只是机位角度不同，背景元素全程不变；该图中的人物/商品/文字一律不得继承。"
        )
    if product_facts and product_facts.strip():
        text_parts.append(
            f"\n【product_facts 客户明确陈述的商品事实（可作为商品事实源）】\n{product_facts.strip()}"
            "\n★仅当客户在此明确陈述'该商品确实具有 X 属性'时才可作为事实源使用。"
        )
    # 产品名为必填字段：客户已指定产品名，GPT 编译器直接使用此名替换商品身份字段
    text_parts.append(
        f"\n【客户指定产品名（最高优先级）】\n{product_name}"
        "\n★仅替换原商品实际可见且'人物与产品.产品'不是'无'的 Shot 中的商品身份字段（含占位符和原产品名）；字段为'无'时必须保持'无'。"
        "\n★商品的动作/结构/卖点不在此步处理，统一进入任务②语义迁移。"
        "\n产品图仅用于描述产品外观特征与 图N 分配，产品名统一使用上述客户指定名称。"
        "\n调用方必须保证 product_name 与 product_image_paths 表示同一个目标商品/SKU；若明显不一致，属于上游输入错误，不得进入正常编译。"
        "\n同一 prompt 内产品名必须跨 Shot 一致。"
    )
    text_parts.append(f"\n待编译的 storyboard JSON:\n{storyboard_json}")
    text_parts.append("\n" + _visibility_timeline_text(storyboard))
    if extra_compile_note and extra_compile_note.strip():
        text_parts.append("\n" + extra_compile_note.strip())
    text_parts.append(
        "\n请按系统提示的强制输出语法，把 storyboard JSON 编译为单条 Wan3.0 prompt。"
        "\n★口播：每个 Shot 引号内逐字复制 storyboard 该镜「口播」，不得改写、合并或换成卖点新词。"
        "\n  - 声音语气、转场衔接：默认继承原 storyboard 字段值；有效口播永远保留 character1，六项声音属性全为'无'时输出 [character1]；否则按语速→语调→重音→停顿→情绪→呼吸感顺序只输出非'无'项"
        "\n每个分镜必须单行输出，禁止跨行。"
    )

    content_list.append({"type": "text", "text": "\n".join(text_parts)})

    # 附带素材图（产品图 → 模特图 → 场景图，与 Image 编号顺序一致）
    def _file_to_b64(path: str) -> str:
        import base64
        from io import BytesIO
        try:
            from PIL import Image
            img = Image.open(path)
            img = img.convert("RGB")
            img.thumbnail((768, 768))
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=75)
            data = base64.b64encode(buf.getvalue()).decode()
            return f"data:image/jpeg;base64,{data}"
        except Exception:
            ext = Path(path).suffix.lower().lstrip(".")
            mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext or 'png'}"
            with open(path, "rb") as f:
                data = base64.b64encode(f.read()).decode()
            return f"data:{mime};base64,{data}"

    visual_products = [p for p in (product_image_paths or []) if p and os.path.exists(p)]
    extra_products = visual_products[_COMPILER_MAX_VISUAL_PRODUCTS:]
    visual_products = visual_products[:_COMPILER_MAX_VISUAL_PRODUCTS]
    if extra_products:
        log(
            f"编译器只看前 {_COMPILER_MAX_VISUAL_PRODUCTS} 张产品图，"
            f"其余 {len(extra_products)} 张仅按 media_manifest 文案引用"
        )
    for p in visual_products:
        content_list.append({"type": "image_url", "image_url": {"url": _file_to_b64(p)}})
    if model_image_path and os.path.exists(model_image_path):
        content_list.append({"type": "image_url", "image_url": {"url": _file_to_b64(model_image_path)}})
    for extra_face in extra_model_image_paths or []:
        if extra_face and os.path.exists(extra_face):
            content_list.append({"type": "image_url", "image_url": {"url": _file_to_b64(extra_face)}})
    if scene_image_path and os.path.exists(scene_image_path):
        content_list.append({"type": "image_url", "image_url": {"url": _file_to_b64(scene_image_path)}})

    log(f"调用编译器（{model}，{len(storyboard)} 个分镜，{len(media_manifest)} 张素材图）")

    completion = await asyncio.to_thread(
        client.chat.completions.create,
        model=model,
        messages=[
            {
                "role": "system",
                "content": STEP1_5_SYSTEM_PROMPT,
            },
            {"role": "user", "content": content_list},  # type: ignore
        ],
        max_tokens=8192,
        temperature=0,
    )
    raw = (completion.choices[0].message.content or "").strip()

    # 去除可能的 markdown 包裹
    if raw.startswith("```"):
        raw = re.sub(r'^```[a-zA-Z]*\n?', '', raw)
        raw = re.sub(r'\n?```$', '', raw)
        raw = raw.strip()

    if not raw:
        raise RuntimeError("编译器输出为空（这条腿的模型=%s）" % model)

    # 强制语法已要求单段 prompt，不再拆分视觉/口播
    full_prompt = raw
    log(f"✓ 编译完成: prompt {len(full_prompt)} 字")

    # 本函数只输出 prompt；素材编号由 Step 0 的 media_manifest 固定。
    return {
        "prompt": full_prompt,
        "source": "step1_5_gpt_compiler",
        **_llm_stamp(client, model),
    }


# ─────────────────────────────────────────────────────────────────
# Step 1.6: Prompt 自检 = 规则校验 + LLM 二次审查
#   MiMo 输出 → 校正 → 更优版本
# ─────────────────────────────────────────────────────────────────
def _parse_storyboard_time(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().lower().replace("秒", "").replace("s", "")
        if ":" in text:
            minutes, seconds = text.split(":", 1)
            return float(minutes) * 60.0 + float(seconds)
        return float(text)
    return None


_ABSENT_FIELD_VALUES = {
    "", "无", "none", "null", "n/a", "看不清", "-", "—",
}
_MIN_STATE_SPLIT_SECONDS = 2.0
_SEGMENT_TARGET_MAX = 2.0
_WAN3_DURATION_MIN = 2
_WAN3_DURATION_MAX = 15


def wan3_api_duration(target_duration: float) -> int:
    """Wan3 duration 只接受整数秒。向上取整，避免成片短于目标、无法 trim 补回。"""
    seconds = float(target_duration or 0)
    return max(_WAN3_DURATION_MIN, min(_WAN3_DURATION_MAX, int(math.ceil(seconds - 1e-9))))
_SEGMENT_HARD_MAX = 15.0
_COMPILER_MAX_VISUAL_PRODUCTS = 1
_SHOT_LINE_RE = re.compile(
    r"^(Shot\s+(\d+)\s+\[)([^\]\n]+)-([^\]\n]+)\]:(.*)$"
)


def _is_absent_field(value: Any) -> bool:
    if value is None:
        return True
    return str(value).strip().lower() in _ABSENT_FIELD_VALUES


def shot_original_product_visible(shot: Dict[str, Any]) -> bool:
    """原商品在该 Shot 是否实际可见。

    不可见：产品、产品动作、交互方式均为「无」。
    可见：其余（含产品已标明、或有动作/交互）。
    """
    product = (shot.get("人物与产品") or {}).get("产品")
    action = (shot.get("画面动作") or {}).get("产品动作")
    interact = (shot.get("商品交互") or {}).get("交互方式")
    return not (
        _is_absent_field(product)
        and _is_absent_field(action)
        and _is_absent_field(interact)
    )


def _shot_time_range(shot: Dict[str, Any]) -> tuple:
    interval = shot.get("时间区间") or shot.get("time_range") or {}
    start = _parse_storyboard_time(interval.get("开始时间", interval.get("start")))
    end = _parse_storyboard_time(interval.get("结束时间", interval.get("end")))
    return (0.0 if start is None else float(start), 0.0 if end is None else float(end))


def invisible_shots_citing_product(
    prompt: str,
    storyboard: List[Dict[str, Any]],
    media_manifest: Optional[List[Dict[str, str]]] = None,
) -> List[int]:
    """不可见 Shot 的成品行若引用了产品 图N，返回违规 Shot 编号。不改写 prompt。"""
    product_nums = set()
    for item in media_manifest or []:
        if item.get("type") != "product":
            continue
        match = re.search(r"(\d+)", str(item.get("image_id") or ""))
        if match:
            product_nums.add(int(match.group(1)))
    visible_by_shot = {}
    for index, shot in enumerate(storyboard or []):
        num = int(shot.get("shot") or index + 1)
        visible_by_shot[num] = shot_original_product_visible(shot)
    bad: List[int] = []
    for line in (prompt or "").splitlines():
        match = re.match(r"^Shot\s+(\d+)\s+", line)
        if not match:
            continue
        num = int(match.group(1))
        if visible_by_shot.get(num, True):
            continue
        cited = {
            int(a or b)
            for a, b in re.findall(r"图(\d+)|Image\s+(\d+)", line)
            if (a or b)
        }
        if cited & product_nums:
            bad.append(num)
    return bad


def first_product_appearance_time(storyboard: Optional[List[Dict[str, Any]]]) -> Optional[float]:
    """原商品首次实际可见的开始时间；全片不可见时返回 None。"""
    for shot in storyboard or []:
        if shot_original_product_visible(shot):
            start, _ = _shot_time_range(shot)
            return start
    return None


def should_split_generation(
    storyboard: Optional[List[Dict[str, Any]]],
    target_duration: float,
    model_image_path: Optional[str] = None,
) -> bool:
    """是否按产品入画时间拆成两次生成。

    有模特图时不拆：换人和换产品必须同一次生成（同一段参考视频 + 同一组参考图）。
    仅换产品、且原商品中途入画、前后都够长时才拆。
    """
    if model_image_path:
        return False
    first_t = first_product_appearance_time(storyboard)
    if first_t is None:
        return False
    return (
        first_t >= _MIN_STATE_SPLIT_SECONDS
        and (float(target_duration) - first_t) >= _MIN_STATE_SPLIT_SECONDS
    )


def _visibility_timeline_text(storyboard: List[Dict[str, Any]]) -> str:
    lines = ["【后端锁定的商品可见性时间轴】GPT 不得改写此表，必须按此编译。"]
    for index, shot in enumerate(storyboard or []):
        num = shot.get("shot", index + 1)
        start, end = _shot_time_range(shot)
        visible = shot_original_product_visible(shot)
        lines.append(
            f"- Shot {num} [{start:.2f}-{end:.2f}]: product_visible="
            f"{'true' if visible else 'false'}"
        )
    first_t = first_product_appearance_time(storyboard)
    if first_t is None:
        lines.append("产品首次入画时间: 无（全片原商品不可见）")
    else:
        lines.append(f"产品首次入画时间: {first_t:.2f}s")
    return "\n".join(lines)


def _fmt_prompt_time(value: float) -> str:
    return f"{max(0.0, float(value)):.2f}"


def slice_prompt_for_shots(
    prompt: str,
    shot_nums: set,
    time_offset: float = 0.0,
) -> str:
    """按 Shot 编号切出子 Prompt，并把时间轴平移到该段参考视频的 0 点。"""
    out_lines = []
    for line in (prompt or "").splitlines():
        match = _SHOT_LINE_RE.match(line.strip())
        if not match:
            continue
        num = int(match.group(2))
        if num not in shot_nums:
            continue
        start = _parse_storyboard_time(match.group(3))
        end = _parse_storyboard_time(match.group(4))
        if start is None or end is None:
            out_lines.append(line.strip())
            continue
        new_start = max(0.0, float(start) - float(time_offset))
        new_end = max(new_start, float(end) - float(time_offset))
        out_lines.append(
            f"Shot {num} [{_fmt_prompt_time(new_start)}-{_fmt_prompt_time(new_end)}]:"
            f"{match.group(5)}"
        )
    out_lines.append(STEP1_5_GLOBAL_CONSTRAINT)
    return "\n".join(out_lines)


_WAN3_PROMPT_MAX = 5000


def _is_constraint_line(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if t == STEP1_5_GLOBAL_CONSTRAINT:
        return True
    return t.startswith("仅在对应 Shot 原商品")


def merge_compiled_shot_prompts(prompts: List[str]) -> str:
    """把逐镜编译的 prompt 拼成一条，只保留一份后置约束。"""
    lines: List[str] = []
    for raw in prompts:
        body = (raw or "").strip()
        if STEP1_5_GLOBAL_CONSTRAINT in body:
            body = body.replace(STEP1_5_GLOBAL_CONSTRAINT, "").strip()
        for line in body.splitlines():
            text = line.strip()
            if text and not _is_constraint_line(text):
                lines.append(text)
    lines.append(STEP1_5_GLOBAL_CONSTRAINT)
    return "\n".join(lines)


def fit_prompt_to_wan3_limit(prompt: str, max_chars: int = _WAN3_PROMPT_MAX) -> str:
    """RunningHub Wan3 prompt 上限 5000；去掉重复后置约束后再截到上限。"""
    text = merge_compiled_shot_prompts([prompt or ""])
    if len(text) <= max_chars:
        return text
    constraint = "\n" + STEP1_5_GLOBAL_CONSTRAINT
    budget = max_chars - len(constraint)
    body = text
    if body.endswith(STEP1_5_GLOBAL_CONSTRAINT):
        body = body[: -len(STEP1_5_GLOBAL_CONSTRAINT)].rstrip()
    if budget < 200:
        return text[:max_chars]
    return body[:budget].rstrip() + constraint


def canonical_prompt_tail(prompt: str) -> tuple:
    """把稿子的【最后一行】钉成 STEP1_5_GLOBAL_CONSTRAINT 的精确版本。返回 `(稿子, 改过吗, 说明)`。

    🔴 为什么由系统钉、不让模型保证：`_validate_prompt_deterministic` 要求尾行**逐字等于**这句常量
       （合同行不许少），而编译/审查腿是语言模型 —— 2026-10-22 实测 agnes/mimo/千问/deepseek 四家
       都会把它复述成 206 字的近似版。旧形状下后果是连锁的：Layer2 输出被"尾行不一致"判失败并【整份丢弃】
       ⇒ 审查等于没跑，但流程照绿、照打钩、照花钱（静默降级）。
    安全边界：只替换"以常量前 12 字开头"的行（那是在复述它）；其余内容一个字不删，缺就补。
    """
    text = (prompt or "").rstrip()
    if not text:
        return text, False, "空稿"
    lines = text.split("\n")
    probe = STEP1_5_GLOBAL_CONSTRAINT[:12]
    if lines[-1].strip() == STEP1_5_GLOBAL_CONSTRAINT:
        return text, False, "尾行已是精确合同行"
    if lines[-1].strip().startswith(probe):
        old_len = len(lines[-1].strip())
        lines[-1] = STEP1_5_GLOBAL_CONSTRAINT
        return "\n".join(lines), True, "尾行是模型的复述版（%d 字）⇒ 钉回精确合同行" % old_len
    kept = [l for l in lines if not l.strip().startswith(probe)]
    removed = len(lines) - len(kept)
    new = "\n".join(kept).rstrip() + "\n" + STEP1_5_GLOBAL_CONSTRAINT
    return new, True, ("补上精确合同行（另移除 %d 行复述）" % removed) if removed else "补上精确合同行"


def shot_lines_needing(prompt: str, needles: List[str]) -> List[int]:
    """稿子里哪些 `Shot N` 行命中了给定片段 —— 背景判红时只重发这几镜，不再整段重编译。"""
    hits: List[int] = []
    for line in (prompt or "").splitlines():
        m = re.match(r"^Shot\s+(\d+)", line.strip())
        if m and any(n and n in line for n in needles):
            hits.append(int(m.group(1)))
    return hits


def replace_shot_line(prompt: str, shot_no: int, new_line: str) -> str:
    """按 Shot 号整行替换（只替换第一条同号行；Shot 号在稿内本应唯一）。"""
    out, done = [], False
    for line in (prompt or "").splitlines():
        m = re.match(r"^Shot\s+(\d+)", line.strip())
        if (not done) and m and int(m.group(1)) == int(shot_no):
            out.append((new_line or "").strip())
            done = True
        else:
            out.append(line)
    return "\n".join(out)


def split_shots_by_product_appearance(
    storyboard: List[Dict[str, Any]],
    first_product_time: float,
) -> tuple:
    """按原商品首次入画时间把 Shot 分成产品入画前 / 产品入画后。"""
    before_nums = set()
    after_nums = set()
    for index, shot in enumerate(storyboard or []):
        num = int(shot.get("shot") or index + 1)
        start, _ = _shot_time_range(shot)
        if start + 0.05 < float(first_product_time):
            before_nums.add(num)
        else:
            after_nums.add(num)
    return before_nums, after_nums


def group_shots_into_segments(
    storyboard: Optional[List[Dict[str, Any]]],
    min_dur: float = _MIN_STATE_SPLIT_SECONDS,
    target_max: float = _SEGMENT_TARGET_MAX,
    hard_max: float = _SEGMENT_HARD_MAX,
) -> List[Dict[str, Any]]:
    """一镜一段小段替换：默认约 2 秒，短于 2 秒才和相邻镜合并，单段不超过 15 秒。"""
    items: List[Dict[str, Any]] = []
    for index, shot in enumerate(storyboard or []):
        start, end = _shot_time_range(shot)
        if end <= start:
            continue
        items.append({
            "num": int(shot.get("shot") or index + 1),
            "start": float(start),
            "end": float(end),
            "shot": shot,
        })
    if not items:
        return []

    def _pack(pack_items: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "shot_nums": {x["num"] for x in pack_items},
            "start": float(pack_items[0]["start"]),
            "end": float(pack_items[-1]["end"]),
            "shots": [x["shot"] for x in pack_items],
        }

    groups: List[Dict[str, Any]] = []
    current: List[Dict[str, Any]] = []

    def flush() -> None:
        if current:
            groups.append(_pack(current))
            current.clear()

    for item in items:
        dur = item["end"] - item["start"]
        if dur > hard_max + 0.05:
            flush()
            t = item["start"]
            while t < item["end"] - 0.05:
                chunk_end = min(item["end"], t + hard_max)
                groups.append({
                    "shot_nums": {item["num"]},
                    "start": t,
                    "end": chunk_end,
                    "shots": [item["shot"]],
                })
                t = chunk_end
            continue
        if not current:
            current.append(item)
            continue
        new_dur = item["end"] - current[0]["start"]
        cur_dur = current[-1]["end"] - current[0]["start"]
        if new_dur <= target_max + 0.05:
            current.append(item)
        elif cur_dur < min_dur - 0.05 and new_dur <= hard_max + 0.05:
            current.append(item)
        else:
            flush()
            current.append(item)
    flush()

    if (
        len(groups) >= 2
        and (groups[-1]["end"] - groups[-1]["start"]) < min_dur - 0.05
        and (groups[-1]["end"] - groups[-2]["start"]) <= hard_max + 0.05
    ):
        prev, last = groups[-2], groups[-1]
        prev["end"] = last["end"]
        prev["shot_nums"] |= last["shot_nums"]
        prev["shots"].extend(last["shots"])
        groups.pop()
    return groups


def _rebase_shots_for_segment(
    shots: List[Dict[str, Any]],
    offset: float,
    seg_end: float,
) -> List[Dict[str, Any]]:
    """把一组 Shot 的时间轴平移到该段参考视频的 0 点。"""
    import copy
    out: List[Dict[str, Any]] = []
    for shot in shots:
        cloned = copy.deepcopy(shot)
        start, end = _shot_time_range(cloned)
        new_start = max(0.0, float(start) - float(offset))
        new_end = max(new_start, min(float(end), float(seg_end)) - float(offset))
        interval = dict(cloned.get("时间区间") or {})
        interval["开始时间"] = round(new_start, 2)
        interval["结束时间"] = round(new_end, 2)
        interval["镜头时长"] = f"{new_end - new_start:.2f}s"
        cloned["时间区间"] = interval
        out.append(cloned)
    return out


# 🔴 名字不许再叫 _SHOT_LINE_RE：上面 1322 行那份钉的是 [开始-结束] 两个时间组，
#    这一份钉的是「前缀 + 正文」。同名两个写者 ⇒ 后者盖掉前者，slice_prompt_for_shots
#    拿 group(3) 当时间解析就变成 ValueError（HEAD 里就是坏的，测试里现形）。
_SHOT_HEAD_RE = re.compile(r"^(Shot\s+(\d+)\s+\[[^\]]+\]:\s*)(.*)$")
_QUOTED_SPEECH_RE = re.compile(r';\s*"[^"]*"\s*(?:\[[^\]]*\])?')
_QUOTED_ONLY_RE = re.compile(r'"[^"]*"\s*(?:\[[^\]]*\])?')


def _voice_block_from_shot(shot: Dict[str, Any]) -> str:
    voice = shot.get("声音语气") or {}
    attrs = []
    for key in ("语速", "语调", "重音", "停顿", "情绪", "呼吸感"):
        value = str(voice.get(key) or "").strip()
        if value and value not in {"无", "none", "None"}:
            attrs.append(f"{key}={value}")
    if attrs:
        return "[character1, " + ", ".join(attrs) + "]"
    return "[character1]"


def apply_voiceover_script(
    storyboard: List[Dict[str, Any]], full_text: str
) -> List[Dict[str, Any]]:
    """把客户改稿对齐回各 Shot：优先按原文片段做 diff，避免把词从中间切开。"""
    from difflib import SequenceMatcher

    text = (full_text or "").strip()
    if not text or not storyboard:
        return storyboard
    slots: List[int] = []
    orig_parts: List[str] = []
    for i, shot in enumerate(storyboard):
        orig = str(shot.get("口播") or "").strip()
        if orig in ("", "无", "none", "None"):
            continue
        slots.append(i)
        orig_parts.append(orig)
    if not slots:
        storyboard[0]["口播"] = text
        return storyboard
    orig_s = "".join(orig_parts)
    new_s = text
    if len(orig_s) == len(new_s):
        pos = 0
        for idx, part in zip(slots, orig_parts):
            storyboard[idx]["口播"] = new_s[pos : pos + len(part)]
            pos += len(part)
        return storyboard
    matcher = SequenceMatcher(a=orig_s, b=new_s, autojunk=False)
    pos = 0
    for idx, part in zip(slots, orig_parts):
        a0, a1 = pos, pos + len(part)
        chunks: List[str] = []
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            lo, hi = max(i1, a0), min(i2, a1)
            if lo >= hi and not (tag == "insert" and i1 == a0):
                if tag == "insert" and i1 == a1 and idx == slots[-1]:
                    chunks.append(new_s[j1:j2])
                continue
            if tag == "equal":
                chunks.append(new_s[j1 + (lo - i1) : j1 + (hi - i1)])
            elif tag == "replace":
                span = max(1, i2 - i1)
                ja = j1 + int(round((j2 - j1) * (lo - i1) / span))
                jb = j1 + int(round((j2 - j1) * (hi - i1) / span))
                chunks.append(new_s[ja:jb])
            elif tag == "insert" and a0 <= i1 < a1:
                chunks.append(new_s[j1:j2])
        storyboard[idx]["口播"] = "".join(chunks).strip() or part
        pos = a1
    return storyboard


def _norm_speech(s: Any) -> str:
    """口播比对用的归一形：去掉所有空白（客户一行一句，换行/空格不算差异）。"""
    return re.sub(r"\s+", "", str(s or ""))


def narration_coverage(want: str, got: str) -> float:
    """新稿实际落位比例 0–1（按字符顺序对齐，不看空格）。"""
    a, b = _norm_speech(want), _norm_speech(got)
    if not a:
        return 1.0
    m = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return round(sum(blk.size for blk in m.get_matching_blocks()) / float(len(a)), 4)


_PUNCH_BOUNDARY = "，。！？；、,.!?;:\n"


def split_script_by_durations(text: str, durations) -> List[str]:
    """按每镜【时长占比】把整篇新稿切成连续片段：拼回去正好等于原稿（一字不多一字不少）。
    切点优先吸附到最近的标点/换行（±14 字窗口），避免把词从中间切开。

    🔴 为什么不用 `apply_voiceover_script` 那种字符 diff 比例（2026-10-14 现网抓到）：
       它在"原片 ASR 全集 vs 新稿全集"上取比例区间，一旦**段数与行数不等**（那次是 ASR 7 段、
       客户稿 6 行）尾部那镜就分不到字 ⇒ 盘上出现"最后一镜还在说原片旧词"，残留 19.3%。
       而客户约定就是"一行一句、句序对齐成片口播"⇒ 顺序必须保持行序，只有"哪一镜说多少"
       由镜头时长决定（时间轴是原片锁死的，SKILL 硬规则 9）。
    """
    raw = str(text or "")
    durs = [max(0.0, float(d or 0.0)) for d in (durations or [])]
    n = len(durs)
    if n == 0 or not raw:
        return [""] * n
    total = sum(durs) or 1.0
    L = len(raw)
    out: List[str] = []
    pos = 0
    for i, d in enumerate(durs):
        if i == n - 1:
            out.append(raw[pos:])
            break
        cut = min(L, pos + max(0, int(round(L * d / total))))
        if pos >= cut >= L:
            out.append("")
            continue
        best = None
        for j in range(cut, min(L, cut + 14)):
            if raw[j] in _PUNCH_BOUNDARY:
                best = j + 1
                break
        if best is None:
            for j in range(cut - 1, max(pos, cut - 14), -1):
                if raw[j] in _PUNCH_BOUNDARY:
                    best = j + 1
                    break
        cut = int(best) if best is not None else cut
        out.append(raw[pos:cut])
        pos = max(pos, cut)
    return out


def apply_customer_narration(
    storyboard: List[Dict[str, Any]],
    voice_script: Optional[str],
    output_dir: str,
    on_log: Optional[Callable[[str], None]] = None,
) -> Optional[Dict[str, Any]]:
    """把客户改稿灌进各镜「口播」位 ⇒ 编译器与 `force_verbatim_speech` 会把新词【逐字】送进 prompt。

    🔴 这一步就是那根没接的线（2026-10-14 定案"wan3.0 能不能直出改口播"）：
       这条 lane 的请求体只有 prompt/duration/resolution/ratio/media，台词位 = prompt 里那句引号，
       而 `apply_voiceover_script`（按原文 diff 把新稿对齐回各镜）在全仓【零调用点】⇒ 进 prompt 的
       永远是 FunASR 抄下来的原片旧词，于是"改口播"只能靠后处理换轨。接上之后 wan3 一枪出片就念新词。
    🔴 必须覆盖在 FunASR 写回【之后】：ASR 那份是"原片到底说了什么"，正是 diff 对齐的底子；
       在它之前改会被整段盖掉（那才是静默冒充"改好了"）。
    ⚠️ 这条校验只保证"新词逐字进了分镜、也就进了 prompt"，【不保证】模型念得准、嘴形对得上 ——
       念词与口型是审帧那一刀 + lip-sync 那条腿的事，别把它当已经验收过。
    对不齐 ⇒ 抛（在编译与提交之前，0 分），不许悄悄拿旧词出片。
    """
    def log(msg: str) -> None:
        if on_log:
            on_log(msg)

    text = str(voice_script or "").strip()
    if not text:
        return None                       # 不改口播 ⇒ 什么都不做（空稿不等于"已按新稿改好"）
    if not storyboard:
        raise RuntimeError("改口播直出：storyboard 是空的 ⇒ 没有可落位的镜，不提交")
    orig = [str(s.get("口播") or "").strip() for s in storyboard]
    slots = [i for i, o in enumerate(orig) if o and o != "无"]
    if not slots:
        raise RuntimeError(
            "改口播直出：各镜口播位全空 ⇒ 没有原句可以做对齐底子（先查 FunASR 有没有识别到口播），"
            "不许把新稿整段塞进第一镜蒙过去")
    spans = []
    for i in slots:
        a, b = _shot_time_range(storyboard[i])
        spans.append(max(0.0, float(b) - float(a)))
    pieces = split_script_by_durations(text, spans)
    for i, piece in zip(slots, pieces):
        storyboard[i]["口播"] = str(piece).strip() or "无"
    after = [str(s.get("口播") or "").strip() for s in storyboard]
    joined = "".join(o for o in after if o and o != "无")
    cover = narration_coverage(text, joined)
    stale = narration_coverage(joined, text)      # 反方向：落进去的词有多少真属于新稿（残留检测）
    problems = []
    if cover < 0.999:
        problems.append("新稿只落位 %.1f%%" % (cover * 100))
    if stale < 0.999:
        problems.append("台词位里还残留 %.1f%% 不属于新稿的字（漏改比没改更坏）" % ((1 - stale) * 100))
    rec = {
        "voice_script": text,
        "chars_script": len(_norm_speech(text)),
        "chars_landed": len(_norm_speech(joined)),
        "coverage": cover,
        "stale": stale,
        "shots": [{"shot": s.get("shot"), "时间区间": s.get("时间区间"), "原口播": o, "新口播": n,
                  "分配字数": len(_norm_speech(n))}
                 for s, o, n in zip(storyboard, orig, after)],
        "method": "按镜时长占比切新稿（split_script_by_durations），切点吸附标点；行序=客户稿行序",
        "slot_spans": [round(x, 2) for x in spans],
        "problems": problems,
        "executor": "pipeline:apply_customer_narration",
        "note": "覆盖在 FunASR 写回之后；只证明新词逐字进了 prompt 的台词位，不证明念得准/嘴形对",
    }
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "voice_script_binding.json").write_text(
        json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    if problems:
        raise RuntimeError(
            "改口播直出校验没过：%s ⇒ 不提交、0 分未扣。逐镜对齐见 %s"
            % ("；".join(problems), out / "voice_script_binding.json"))
    log("✓ 改口播已灌进各镜口播位：%d 镜 / %d 字，覆盖 %.3f、残留 %.3f（逐字进 prompt 由 force_verbatim_speech 钉）"
        % (len(after), rec["chars_script"], cover, stale))
    return rec


def force_verbatim_speech(prompt: str, shots: List[Dict[str, Any]]) -> str:
    """把编译后每镜引号口播钉回 storyboard 原文，避免商品类口播被改写。"""
    by_num: Dict[int, Dict[str, Any]] = {}
    for shot in shots:
        try:
            by_num[int(shot.get("shot") or 0)] = shot
        except (TypeError, ValueError):
            continue
    unused = list(shots)
    out_lines: List[str] = []
    for line in (prompt or "").splitlines():
        match = _SHOT_HEAD_RE.match(line)
        if not match:
            out_lines.append(line)
            continue
        prefix, shot_num_s, rest = match.group(1), match.group(2), match.group(3)
        shot = by_num.get(int(shot_num_s))
        if shot is None and unused:
            shot = unused.pop(0)
        elif shot is not None and unused and unused[0] is shot:
            unused.pop(0)
        speech = str((shot or {}).get("口播") or "").strip()
        rest2 = _QUOTED_SPEECH_RE.sub("", rest)
        rest2 = _QUOTED_ONLY_RE.sub("", rest2).rstrip(" ;")
        if speech and speech not in {"无", "none"}:
            out_lines.append(
                f'{prefix}{rest2}; "{speech}" {_voice_block_from_shot(shot or {})};'
            )
        else:
            out_lines.append(f"{prefix}{rest2};")
    return "\n".join(out_lines)


def concat_videos(paths: List[str], output_path: str) -> str:
    """ffmpeg concat 重编码拼接，保证前后段封装一致。"""
    if not paths:
        raise RuntimeError("concat_videos 没有输入视频")
    if len(paths) == 1:
        return paths[0]
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    list_path = out.with_name(out.stem + "_concat.txt")
    lines = []
    for path in paths:
        abs_p = Path(path).resolve().as_posix().replace("'", r"'\''")
        lines.append(f"file '{abs_p}'")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(list_path),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(out),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=900)
    return str(out)


def extract_still_frame(video_path: str, timestamp: float, output_path: str) -> str:
    """从视频抽取一帧静图，用作产品入画前的产品槽占位，避免打乱 图N 顺序。"""
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{max(0.0, float(timestamp)):.3f}",
        "-i", video_path,
        "-frames:v", "1", "-q:v", "2",
        str(out),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    if not out.exists() or out.stat().st_size <= 0:
        raise RuntimeError(f"抽帧失败: {output_path}")
    return str(out)


def manifest_for_segment(
    media_manifest: Optional[List[Dict[str, str]]],
    include_product: bool,
    product_placeholder_path: Optional[str] = None,
) -> List[Dict[str, str]]:
    """保持 图N 顺序。产品入画前把产品槽换成该段静帧，模特图/场景图原样保留。"""
    out: List[Dict[str, str]] = []
    for item in media_manifest or []:
        cloned = dict(item)
        if cloned.get("type") == "product" and not include_product:
            if not product_placeholder_path:
                raise RuntimeError("产品入画前隐藏产品图时必须提供该段静帧占位，否则 图N 会错位")
            cloned["path"] = product_placeholder_path
            cloned["placeholder"] = "product_absent"
        out.append(cloned)
    return out


def normalize_storyboard_time(storyboard: List[Dict[str, Any]], target_duration: float) -> List[Dict[str, Any]]:
    """在 Checkpoint A 一次性规范化时间边界，返回 canonical storyboard。

    ★先校验后 snap 原则：
      - 结构合法性硬失败（不参与 snap，统一边界规则）:
          start < -0.05             → 非法负值，硬失败
          end > target_duration + 0.05  → 超出视频时长，硬失败
          start >= end              → 非法镜头，硬失败
          Shot 顺序倒退             → 硬失败
      - 不再使用"end > 2×target_duration"等额外概念
      - 仅以下三类 anchor 允许 snap（≤ 0.05s）:
          first.start ≈ 0
          next.start  ≈ previous.end
          last.end    ≈ target_duration
      - 误差 > 0.05s 直接抛异常回源重跑 Step 1 MiMo
      - 严禁"先修成正确，再验证"
    """
    if not storyboard:
        raise RuntimeError("Step 1 storyboard 为空")
    tolerance = 0.05
    normalized = storyboard
    times: List[tuple] = []
    for index, shot in enumerate(normalized, start=1):
        interval = shot.get("时间区间") or shot.get("time_range") or {}
        start = _parse_storyboard_time(interval.get("开始时间", interval.get("start")))
        end = _parse_storyboard_time(interval.get("结束时间", interval.get("end")))
        if start is None or end is None:
            raise RuntimeError(f"Step 1 storyboard Shot {index} 缺少可解析的开始/结束时间")

        # === 结构合法性硬失败（统一边界规则，不参与 snap）===
        # 1. start < -0.05（不允许负时间，超出舍入误差）
        if start < -tolerance:
            raise RuntimeError(
                f"Step 1 storyboard Shot {index} 结构非法：start={start:.3f}s < 0（超出舍入容差），"
                f"非舍入误差，请回源重跑 Step 1 MiMo"
            )
        # 2. end > target_duration + 0.05（不允许超出视频时长）
        if end > target_duration + tolerance:
            raise RuntimeError(
                f"Step 1 storyboard Shot {index} 结构非法：end={end:.3f}s "
                f"> target_duration + 0.05={target_duration + tolerance:.3f}s，"
                f"非舍入误差，请回源重跑 Step 1 MiMo"
            )
        # 3. start >= end（非法镜头，即使差 0.01s 也是结构错误）
        if start >= end:
            raise RuntimeError(
                f"Step 1 storyboard Shot {index} 结构非法：start={start:.3f}s >= end={end:.3f}s，"
                f"非舍入误差，请回源重跑 Step 1 MiMo"
            )
        # 4. Shot 顺序倒退（前一镜 end 超出当前 start 0.05s 以上）
        if times and start < times[-1][1] - tolerance:
            raise RuntimeError(
                f"Step 1 storyboard Shot {index} 顺序倒退：start={start:.3f}s "
                f"< 前一镜 end={times[-1][1]:.3f}s，"
                f"非舍入误差，请回源重跑 Step 1 MiMo"
            )

        # === anchor 校验（仅这三类允许 snap）===
        # 第一镜 start 必须 ≈ 0
        if index == 1 and abs(start) > tolerance:
            raise RuntimeError(
                f"Step 1 storyboard 时间轴异常：第一镜 start={start:.3f}s，"
                f"与 0.000s 误差 {abs(start):.3f}s > {tolerance}s 容差，"
                f"非舍入误差，请回源重跑 Step 1 MiMo"
            )
        # 相邻 Shot 必须连续（与前一镜 end 的误差 ≤ tolerance）
        if times and abs(start - times[-1][1]) > tolerance:
            raise RuntimeError(
                f"Step 1 storyboard 时间轴异常：Shot {index} start={start:.3f}s "
                f"与前一镜 end={times[-1][1]:.3f}s 不连续，"
                f"误差 {abs(start - times[-1][1]):.3f}s > {tolerance}s 容差，"
                f"非舍入误差，请回源重跑 Step 1 MiMo"
            )
        # 最后一镜 end 必须 ≈ target_duration
        is_last = (index == len(normalized))
        if is_last and abs(end - target_duration) > tolerance:
            raise RuntimeError(
                f"Step 1 storyboard 时间轴异常：最后 Shot.end={end:.3f}s，"
                f"与 target_duration={target_duration:.3f}s 误差 {abs(end - target_duration):.3f}s > {tolerance}s 容差，"
                f"非舍入误差（MiMo 可能解析错误或漏镜），请回源重跑 Step 1 MiMo"
            )

        # ★校验通过后才允许 snap（仅修 ≤ 0.05s 的舍入误差，且仅对 anchor）
        if index == 1 and abs(start) <= tolerance:
            interval["开始时间"] = 0.0
            if "start" in interval:
                interval["start"] = 0.0
            start = 0.0
        if times and abs(start - times[-1][1]) <= tolerance:
            prev_end = times[-1][1]
            interval["开始时间"] = prev_end
            if "start" in interval:
                interval["start"] = prev_end
            start = prev_end
        if is_last and abs(end - target_duration) <= tolerance:
            interval["结束时间"] = float(target_duration)
            if "end" in interval:
                interval["end"] = float(target_duration)
            end = float(target_duration)
        if "镜头时长" in interval:
            interval["镜头时长"] = round(end - start, 3)
        if "duration" in interval:
            interval["duration"] = round(end - start, 3)
        times.append((start, end))
    validate_storyboard_time(normalized, target_duration)
    return normalized


def validate_storyboard_time(storyboard: List[Dict[str, Any]], target_duration: float) -> None:
    """校验 canonical storyboard 时间轴；失败只能回源重跑 Step 1。"""
    if not storyboard:
        raise RuntimeError("Step 1 storyboard 为空")
    times: List[tuple] = []
    for index, shot in enumerate(storyboard, start=1):
        interval = shot.get("时间区间") or shot.get("time_range") or {}
        start = _parse_storyboard_time(interval.get("开始时间", interval.get("start")))
        end = _parse_storyboard_time(interval.get("结束时间", interval.get("end")))
        if start is None or end is None:
            raise RuntimeError(f"Step 1 storyboard Shot {index} 缺少可解析的开始/结束时间")
        times.append((start, end))
    tolerance = 0.05
    if abs(times[0][0]) > tolerance:
        raise RuntimeError(f"Step 1 storyboard 时间轴异常：第一镜 start={times[0][0]:.3f}s，不是 0")
    for index, (start, end) in enumerate(times):
        if not start < end:
            raise RuntimeError(f"Step 1 storyboard 时间轴异常：Shot {index + 1} start >= end")
        if end > target_duration + tolerance:
            raise RuntimeError(f"Step 1 storyboard 时间轴异常：Shot {index + 1}.end 超出 target_duration")
        if index and abs(start - times[index - 1][1]) > tolerance:
            raise RuntimeError(f"Step 1 storyboard 时间轴异常：Shot {index + 1} 与前一镜不连续")
    if abs(times[-1][1] - target_duration) > tolerance:
        raise RuntimeError(f"Step 1 storyboard 时间轴异常：最后一镜 end={times[-1][1]:.3f}s，不等于 target_duration={target_duration:.3f}s")


def _extract_shot_lines(prompt: str) -> List[str]:
    return [line.strip() for line in prompt.splitlines() if re.match(r"^Shot\s+\d+\s*\[", line.strip())]


def _protected_prompt_signature(prompt: str) -> tuple:
    """提取 Layer 2 严禁修改的字段，用于前后版本确定性比较。

    ★Image 引用不在 protected signature 内：
      - Layer 2 被授权修"可唯一确定的 Image 引用错误"
      - 若把 Image 放入 signature，会与 Image 修复授权直接冲突
        （GPT 正确把 图8 → 图2，复验会发现 Image 变了，丢弃正确结果）
    - Image 引用的合法性由 _validate_prompt_deterministic() 单独校验：
        所有 图N 必须 1 ≤ N ≤ len(media_manifest)
    - Image 引用的"非授权位置不变"由 Layer2 fixes 记录的修复位置约束
    """
    shot_fields = tuple(_extract_shot_lines(prompt))
    dialogue = tuple(re.findall(r'"([^"\n]*)"', prompt))
    character_tags = tuple(re.findall(r"\[character[^\]\n]*\]", prompt, flags=re.IGNORECASE))
    transitions = tuple(re.findall(r"(?:Hard cut transition|平滑切换|蒙太奇|硬切|转场)[^;\n]*", prompt, flags=re.IGNORECASE))
    camera_terms = tuple(
        term.lower()
        for term in re.findall(
            r"\b(?:close-up|medium shot|wide shot|pan|tilt|zoom|dolly|tracking|handheld|static|特写|近景|中景|远景|全景|推镜|拉镜|摇镜|移镜|跟拍|固定机位|手持)\b",
            prompt,
            flags=re.IGNORECASE,
        )
    )
    sfx_terms = tuple(
        term.lower()
        for term in re.findall(
            r"\b(?:SFX|BGM|音效|背景音|配乐|环境音|人声|旁白|VO|sound|audio)[^\n;]*",
            prompt,
            flags=re.IGNORECASE,
        )
    )
    return (shot_fields, dialogue, character_tags, transitions, camera_terms, sfx_terms)


def _validate_prompt_deterministic(
    prompt: str,
    storyboard: List[Dict[str, Any]],
    media_manifest: List[Dict[str, str]],
    product_name: str,
    baseline_prompt: Optional[str] = None,
    layer2_fixes: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """Layer 2 后的完整确定性复验；失败时丢弃 LLM 结果。

    ★Image 引用修改授权范围检查（防止 LLM 把 10 处 图1 全部改成合法 图2）:
      - 对比 baseline_prompt 与 Layer2 输出 prompt 的每个 Image 引用位置
      - 若某位置 图N 发生变化，必须满足全部条件：
          1. 该位置出现在 layer2_fixes 中（fix.old_image → fix.new_image）
          2. new_image 必须存在于 media_manifest（1 ≤ N ≤ len(media_manifest)）
          3. 除 layer2_fixes 声明位置外，其余 Image 引用必须完全不变
      - 否则：丢弃 Layer2，回退使用 Layer1 prompt
    """
    if len(prompt) > 20_000:
        raise RuntimeError("Prompt 超过 20,000 字符")
    prompt_lines = prompt.splitlines()
    if not prompt_lines or prompt_lines[-1] != STEP1_5_GLOBAL_CONSTRAINT:
        raise RuntimeError("Prompt 后置约束与 Step 1.5 固定字符串不一致")
    validate_prompt_time_against_storyboard(prompt, storyboard)
    image_count = len(media_manifest or [])
    # ★兼容"图N"（Wan3.0 官方）和"Image N"（旧版）两种引用格式
    _refs = re.findall(r"(?:Image\s+(\d+)|图(\d+))", prompt)
    image_refs = {int(n[0] or n[1]) for n in _refs}
    if any(value < 1 or value > image_count for value in image_refs):
        raise RuntimeError("Prompt 含超出 media_manifest 的 Image 引用")
    normalized = prompt.replace("（待识别产品）", product_name).replace("<PRODUCT>", product_name)
    if re.search(r"<[^<>\n]+>", normalized):
        raise RuntimeError("Prompt 含未知占位符")
    if baseline_prompt is not None and _protected_prompt_signature(prompt) != _protected_prompt_signature(baseline_prompt):
        raise RuntimeError("Layer2 修改了受保护字段")

    # ★Image 引用修改授权范围检查（仅在传入 baseline_prompt 时执行）
    #   防止 LLM 把 10 处 图1 全改成合法 图2 → 全部通过 manifest 编号校验
    if baseline_prompt is not None:
        repairs = layer2_fixes or []
        _validate_image_ref_changes_in_scope(
            baseline_prompt=baseline_prompt,
            new_prompt=prompt,
            media_manifest=media_manifest,
            layer2_fixes=repairs,
            storyboard=storyboard,
        )
        _validate_physical_repairs(
            baseline_prompt=baseline_prompt,
            new_prompt=prompt,
            storyboard=storyboard,
            repairs=repairs,
        )


def _validate_physical_repairs(
    baseline_prompt: str,
    new_prompt: str,
    storyboard: List[Dict[str, Any]],
    repairs: List[Dict[str, Any]],
) -> None:
    physical_repairs = [repair for repair in repairs if repair.get("type") == "physical_text"]
    if not physical_repairs:
        baseline_without_images = re.sub(r"(?:Image\s+\d+|图\d+)", "", baseline_prompt)
        new_without_images = re.sub(r"(?:Image\s+\d+|图\d+)", "", new_prompt)
        if baseline_without_images != new_without_images:
            raise RuntimeError("Layer2 存在未声明的非 Image 文本修改")
        return

    shot_ids = {shot.get("shot") for shot in storyboard}
    shot_lines = {
        int(match.group(1)): match.group(0)
        for match in re.finditer(r"(?m)^.*?Shot\s+(\d+).*?(?=\n|$)", baseline_prompt)
    }
    expected = baseline_prompt
    for repair in physical_repairs:
        shot_id = repair["shot"]
        if shot_id not in shot_ids:
            raise RuntimeError(f"physical_text repair 的 shot 不存在：{shot_id}")
        old = repair["old"]
        if shot_lines.get(shot_id, "").count(old) != 1:
            raise RuntimeError(f"physical_text repair 的 old 未在指定 Shot 中唯一出现：{shot_id}")
        if expected.count(old) != 1:
            raise RuntimeError("physical_text repair 的 old 在完整输入中不唯一，无法安全替换")
        expected = expected.replace(old, repair["new"], 1)

    expected_without_images = re.sub(r"(?:Image\s+\d+|图\d+)", "", expected)
    new_without_images = re.sub(r"(?:Image\s+\d+|图\d+)", "", new_prompt)
    if expected_without_images != new_without_images:
        raise RuntimeError("Layer2 存在未被 repairs 声明的非 Image 文本修改")


def _validate_image_ref_changes_in_scope(
    baseline_prompt: str,
    new_prompt: str,
    media_manifest: List[Dict[str, str]],
    layer2_fixes: List[Dict[str, Any]],
    storyboard: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """对 baseline 与 new prompt 中每个 图N 出现位置做精确对比，
    仅允许 layer2_fixes 声明的修改；其他位置必须完全不变。

    实现思路：
      - 用 re.finditer 找出 baseline / new 中所有 "图N" 出现位置（按字符 offset）
      - 对齐两个列表（要求出现次数相同；不同 → 直接失败，因为 protected signature 已校验过结构）
      - 逐位置比较 old_image vs new_image：
          · 相同 → OK
          · 不同 → 必须在 layer2_fixes 中找到匹配的 (old, new) 记录
          · 找不到 → 失败
      - new_image 必须在 media_manifest 编号范围内（已在主校验中保证）
    """
    # ★兼容"图N"（Wan3.0 官方）和"Image N"（旧版）两种引用格式
    pattern = re.compile(r"(?:Image\s+(\d+)|图(\d+))")
    baseline_matches = list(pattern.finditer(baseline_prompt))
    new_matches = list(pattern.finditer(new_prompt))

    def _norm_ref(match) -> str:
        n = match.group(1) or match.group(2)
        return f"图{n}"

    baseline_refs = [_norm_ref(match) for match in baseline_matches]
    new_refs = [_norm_ref(match) for match in new_matches]

    declared_changes: List[tuple] = []
    for fix in layer2_fixes:
        if not isinstance(fix, dict) or fix.get("type") != "image_reference":
            raise RuntimeError("Layer2 repairs 含非法 repair 类型")
        old_val = fix.get("old", "")
        new_val = fix.get("new", "")
        # ★兼容"图N"（Wan3.0 官方格式）和"Image N"（旧版兼容）
        _img_ref_pattern = r"(?:Image\s+\d+|图\d+)"
        if not isinstance(old_val, str) or not re.fullmatch(_img_ref_pattern, old_val):
            raise RuntimeError("Layer2 image_reference repair 的 old 必须是 图N，不允许为空")
        if not isinstance(new_val, str) or not re.fullmatch(_img_ref_pattern, new_val):
            raise RuntimeError("Layer2 image_reference repair 的 new 必须为 图N")
        declared_changes.append((old_val, new_val, fix["shot"]))

    image_count = len(media_manifest or [])
    image_types = {}
    for item in media_manifest or []:
        image_id = item.get("image_id", "")
        label = item.get("label", "")
        if "产品" in label:
            image_types[image_id] = "product"
        elif "模特" in label or "人物" in label:
            image_types[image_id] = "model"
        elif "场景" in label:
            image_types[image_id] = "scene"

    def infer_role(text: str) -> Optional[str]:
        roles = set()
        if re.search(r"产品|商品|服装|杯|瓶|包装|穿戴", text):
            roles.add("product")
        if re.search(r"人物|模特|主体人物|脸|发型|身体|手部", text):
            roles.add("model")
        if re.search(r"场景|背景|环境|桌面|室内|户外", text):
            roles.add("scene")
        return next(iter(roles)) if len(roles) == 1 else None

    shot_lines = {
        int(match.group(1)): match.group(0)
        for match in re.finditer(r"(?m)^.*?Shot\s+(\d+).*?(?=\n|$)", baseline_prompt)
    }
    for repair in layer2_fixes:
        if repair.get("type") != "image_reference":
            continue
        shot_id = repair["shot"]
        shot_text = shot_lines.get(shot_id, "")
        if shot_text.count(repair["old"]) != 1:
            raise RuntimeError(f"Layer2 Image repair 无法唯一定位原始引用：Shot {shot_id}")
        role = infer_role(shot_lines.get(shot_id, ""))
        if role is None:
            raise RuntimeError(f"Layer2 Image repair 无法唯一确认素材角色：Shot {shot_id}")
        new_ref = repair["new"]
        if image_types.get(new_ref) != role:
            raise RuntimeError(f"Layer2 Image repair 素材类型不匹配：Shot {shot_id} {new_ref} != {role}")
        candidates = [image_id for image_id, image_type in image_types.items() if image_type == role]
        if len(candidates) != 1:
            raise RuntimeError(f"Layer2 Image repair 的同类型候选不唯一：Shot {shot_id} {role}")
        old_ref = repair["old"]
        if old_ref and image_types.get(old_ref) == role:
            raise RuntimeError(f"Layer2 Image repair 的 old 并非类型错绑或越界：{shot_id} {old_ref}")

    actual_changes: List[tuple] = []
    matcher = difflib.SequenceMatcher(a=baseline_refs, b=new_refs, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "delete" or i2 - i1 > 1 or j2 - j1 > 1:
            raise RuntimeError("Layer2 Image 引用发生未授权删除或批量重排")
        old_ref = baseline_refs[i1] if i1 < i2 else ""
        new_ref = new_refs[j1] if j1 < j2 else ""
        if not new_ref:
            raise RuntimeError("Layer2 不得删除 Image 引用")
        # ★兼容"图N"和"Image N"两种格式
        _nums = re.findall(r"\d+", new_ref)
        new_n = int(_nums[-1]) if _nums else -1
        if new_n < 1 or new_n > image_count:
            raise RuntimeError(f"Layer2 Image 引用越界：{new_ref}")
        actual_changes.append((old_ref, new_ref))

    declared_pairs = [(old_ref, new_ref) for old_ref, new_ref, _ in declared_changes]
    if sorted(actual_changes) != sorted(declared_pairs):
        raise RuntimeError(
            f"Layer2 Image repair log 与实际 diff 不一致：声明={declared_changes}，实际={actual_changes}"
        )


def validate_prompt_time_against_storyboard(prompt: str, storyboard: List[Dict[str, Any]], tolerance: float = 0.05) -> None:
    """校验 GPT 是否逐字段复制 storyboard 时间；失败只能重跑 Step 1.5。"""
    matches = list(re.finditer(r"Shot\s+(\d+)\s*\[\s*([^\]\-]+?)\s*-\s*([^\]]+?)\s*\]", prompt))
    if len(matches) != len(storyboard):
        raise RuntimeError(f"Step 1.5 Prompt 时间轴异常：Prompt 有 {len(matches)} 个 Shot，storyboard 有 {len(storyboard)} 个")
    for index, match in enumerate(matches):
        shot_number = int(match.group(1))
        interval = storyboard[index].get("时间区间") or storyboard[index].get("time_range") or {}
        expected_start = _parse_storyboard_time(interval.get("开始时间", interval.get("start")))
        expected_end = _parse_storyboard_time(interval.get("结束时间", interval.get("end")))
        actual_start = _parse_storyboard_time(match.group(2).strip())
        actual_end = _parse_storyboard_time(match.group(3).strip())
        expected_number = storyboard[index].get("shot", index + 1)
        if shot_number != int(expected_number) or expected_start is None or expected_end is None:
            raise RuntimeError(f"Step 1.5 Prompt 时间轴异常：Shot {index + 1} 字段不匹配 storyboard")
        if abs(actual_start - expected_start) > tolerance or abs(actual_end - expected_end) > tolerance:
            raise RuntimeError(
                f"Step 1.5 Prompt 时间轴异常：Shot {shot_number} 时间 {actual_start:.2f}-{actual_end:.2f}s "
                f"与 storyboard {expected_start:.2f}-{expected_end:.2f}s 不一致"
            )


# ─────────────────────────────────────────────────────────────────────────
# 「换背景」这件事的判定 —— 全部抽成纯函数，入口(8090)与 Layer1 闸读同一份，别抄两遍。
# 事故（2026-10-22 sc_9c246af70384）：客户把整段任务说明写进「换背景」栏，里面明写着
# 「背景、动作、口播全部保留原片」，而旧写法 `swap_scene = bool(scene_text)` 只看非空，
# 于是替他打开两道他从未要求的闸（新背景落实 + 原背景残留），且两道在这种输入下必红，
# 每红一次就整段重编译一次（9 镜实测 173-226s，换到 Cloudflare 那家就是 524）⇒ 循环失败。
# ─────────────────────────────────────────────────────────────────────────
_KEEP_BG_PHRASES = ("保留原片", "保留原背景", "背景不变", "不要换背景", "不换背景", "背景全部保留")
# 背景判定的虚词表（唯一源；Layer1 里不许再抄一份）
_BG_STOP = {"背景", "必须", "严禁", "复刻", "参考", "视频", "原背景", "环境", "场景",
             "室内", "居家", "不得", "出现", "画面", "可见", "无其他物体"}
# 否定/空值型开头：这些不是"元素"，换成任何背景都还会存在，禁它等于禁如实描述
_BG_NEGATIVE_HEADS = ("无", "没有", "不含", "未见", "仅有", "只有")


def scene_swap_decision(scene_text: str) -> tuple:
    """非空 ≠ 要换背景。返回 `(要不要换, 原因)`——原因要能印到日志上，别让人猜。"""
    t = str(scene_text or "").strip()
    if not t:
        return False, "换背景栏为空"
    hit = [p for p in _KEEP_BG_PHRASES if p in t]
    if hit:
        return False, "换背景栏里写着「%s」，按【不换】处理" % "、".join(hit)
    return True, "文本是背景描述，未见保留原片类否定"


def _bg_fragments(text: str) -> List[str]:
    """按标点切片段（≥4 字），用来判"新背景原话有没有进稿"——比数碎词出现次数可靠。"""
    parts = [p.strip() for p in re.split(r"[，、；;。\n,]", str(text or ""))]
    return [p for p in parts if len(p) >= 4]


def background_landed(bg_text: str, prompt: str) -> tuple:
    """新背景是否落实：最长片段整段进稿，或 ≥2 个不同片段进稿。返回 `(落实吗, 拒因)`。"""
    frags = _bg_fragments(bg_text)
    if not frags:
        return True, "无 ≥4 字的背景片段可判"
    longest = max(frags, key=len)
    hits = [f for f in frags if f in prompt]
    if longest in prompt or len(hits) >= 2:
        return True, "命中 %d/%d 段" % (len(hits), len(frags))
    return False, "新背景原话没进稿（仅命中 %d 段，最长片段=%r）" % (len(hits), longest[:40])


def original_bg_residue(bg_text: str, storyboard: Optional[List[Dict[str, Any]]],
                        prompt: str, new_bg_words: Optional[List[str]] = None) -> List[str]:
    """原背景残留：只数 ≥6 字、非否定/空值型、且不在新背景描述里的片段。"""
    leaked: List[str] = []
    bg = str(bg_text or "")
    allow_skip = set(new_bg_words or [])
    for shot in (storyboard or []):
        env = shot.get("场景环境") or {}
        det = str((env.get("背景细节") or env.get("场景") or "")).strip()
        if not det:
            continue
        for w in re.findall(r"[一-鿿]{6,}", det):
            if w in _BG_STOP or w in allow_skip or w.startswith(_BG_NEGATIVE_HEADS):
                continue
            if w in bg or w not in prompt or w in leaked:
                continue
            leaked.append(w)
    return leaked


async def validate_and_fix_prompt(
    prompt: str,
    target_duration: float,
    media_manifest: List[Dict[str, str]],
    product_name: str = "",
    storyboard: Optional[List[Dict[str, Any]]] = None,
    background_override: Optional[str] = None,
    on_log: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    r"""对编译后的 prompt 做两层校正：

    Layer 1 规则校验 + 自动修复（代码层）：
      - prompt 非空、长度 ≤ 20,000 字符
      - target_duration 使用 Step 0 的全局硬参数（原样回传，Lint 不得改变）
      - 图N 编号越界检测（基于 media_manifest 编号归属）
      - 已注册占位符代码替换；未知占位符直接失败
      - 删除残留顶层 API 参数行（行首独立参数行 resolution:/ratio:/duration:）
        ★正则只匹配行首 `^\s*(resolution|ratio|duration)\s*:\s*.+$`
        ★不删除引号内口播、画面文字、普通描述句中出现的 "duration:"/"ratio:" 等字样
    Layer 2 LLM 二次审查（MiMo 审查员）：
      - **时间错误不在此层处理**（Layer 1 已全权负责时间轴校验，失败即抛异常回源）
      - 仅允许修：物理文本错误 / Image 引用错误
      - 触发后输出修正版 prompt（若无需修改则原样返回）

    ★Layer 2 后的确定性复验（保险层，必跑）：
      Layer 2 输出 → _validate_prompt_deterministic() 复验：
        - Shot 数量/编号未变
        - start/end 与 Checkpoint A canonical storyboard 一致（仅后端确定性复验，不由 Layer 2 修复）
        - 图N 均合法（1 ≤ N ≤ len(media_manifest））
        - 无未知占位符
        - protected signature 未变（口播/台词/character/Image 引用/转场/运镜/音效 BGM & SFX）
        - Prompt ≤ 20,000 字符
      失败 → 【同一份稿重问一次】→ 再失败就抛异常大声停。
        🔴 旧形状是"丢弃 Layer2 输出、改用 Layer1 结果"——那是假绿：审查没跑，流程却全绿、照打钩、照花钱
        （2026-10-22 实测三家模型都有概率栽在 repair-JSON 契约上，尾行复述也曾整份丢弃审查结果）。

    Args:
        target_duration: Step 0 锁死的全局时间事实源（float 秒），全流程唯一变量名。
            Lint 仅原样回传，不做任何 clamp / 修改。
        media_manifest: Step 0 后端固定的 图N 编号归属清单（含 image_id/label/path）。
            image_count 不再作为独立权威输入，由 len(media_manifest) 在内部计算。

    Returns:
        {
            "prompt": str,           # 校正后的 prompt
            "target_duration": float, # 原样回传（= 输入 target_duration）
            "fixes": list[str],       # 修复项列表（用于日志）
            "llm_reviewed": bool,    # 是否触发了 LLM 审查
        }
    """
    def log(msg: str) -> None:
        if on_log:
            on_log(msg)
        print(f"  [Step1.5·自检] {msg}", flush=True)

    fixes: List[str] = []
    out_prompt = prompt or ""
    # 🔴 第一件事就是钉合同行（尾行逐字等于 STEP1_5_GLOBAL_CONSTRAINT）：
    #    否则后面 Layer2 的确定性复验会因"模型复述了这句"而【整份丢弃审查结果】——静默没审查。
    out_prompt, tailed, tail_note = canonical_prompt_tail(out_prompt)
    if tailed:
        fixes.append("尾行钉回精确合同行：%s" % tail_note)
        log("  ⓘ %s" % tail_note)

    # ★target_duration：Step 0 后端锁死的全局硬参数，Lint 不得改变此值
    # （仅校验 prompt 时间轴是否以此为边界，不做 clamp 修改）
    try:
        target_duration_f = float(target_duration)
    except (TypeError, ValueError):
        raise RuntimeError("target_duration 无效：必须由 Step 0 提供有效正数")
    out_target_duration = target_duration_f  # 原样回传，不改

    # image_count 由 media_manifest 派生，不再作为权威输入
    image_count = len(media_manifest or [])

    # === Layer 1: 规则校验 + 自动修复 ===
    # 1. prompt 非空与 Wan3.0 长度上限
    if not out_prompt.strip():
        raise RuntimeError("MiMo 输出的 prompt 为空")
    if len(out_prompt) > 20_000:
        raise RuntimeError(f"Wan3.0 Prompt 超过 20,000 字符上限：{len(out_prompt)} 字符")
    if len(out_prompt) < 30:
        fixes.append(f"prompt 过短（{len(out_prompt)} 字），可能信息不足")

    # 2. 占位符只允许代码处理，未知占位符直接失败，不交 Layer 2 猜测
    if product_name:
        out_prompt = out_prompt.replace("（待识别产品）", product_name).replace("<PRODUCT>", product_name)
    unknown_placeholders = re.findall(r"<[^<>\n]+>", out_prompt)
    if unknown_placeholders:
        raise RuntimeError(
            "Prompt 含未知占位符，无法由代码唯一映射："
            + ", ".join(sorted(set(unknown_placeholders)))
            + "；应回源重跑 Step 1.5"
        )

    # 3. target_duration 由 Step 0 锁死；此处只原样传递，不做区间判断或修复。

    # 3. ★Shot 时间轴 Contract 只校验，不再修改时间；规范化已在 Checkpoint A 完成
    #    提取所有形如 "Shot N [X.X-Y.Ys]" 的时间对
    shot_time_pattern = re.compile(r'Shot\s+\d+\s*\[\s*([^\]\-]+?)\s*-\s*([^\]]+?)\s*\]')
    shot_times: List[tuple] = []  # [(start, end, match_obj), ...]
    for m in shot_time_pattern.finditer(out_prompt):
        try:
            s = _parse_storyboard_time(m.group(1).strip())
            e = _parse_storyboard_time(m.group(2).strip())
            if s is None or e is None:
                continue
            shot_times.append((s, e, m))
        except (TypeError, ValueError):
            continue

    SNAP_TOLERANCE = 0.05  # 仅用于校验 canonical storyboard，不在此处修改时间
    # 3a0. ★第一镜必须从 0 开始：first.start = 0（±SNAP_TOLERANCE）
    #      超出容差 → 结构错误，抛异常回源重跑
    if shot_times:
        sorted_for_first = sorted(shot_times, key=lambda x: x[0])
        first_start = sorted_for_first[0][0]
        if first_start > SNAP_TOLERANCE:
            msg = (
                f"storyboard 时间轴异常：第一镜 start={first_start:.2f}s ≠ 0.00"
                f"（前 {first_start:.2f}s 未覆盖，> {SNAP_TOLERANCE}s 容差）。\n"
                f"→ 结构错误，不交 LLM 猜修，请回源重跑 Step 1 MiMo。"
            )
            log(f"  ✗ {msg}")
            raise RuntimeError(msg)
        elif 0 < first_start <= SNAP_TOLERANCE:
            log(f"  ✓ 第一镜 start={first_start:.2f}s 在 canonical 容差内，保持 Prompt 原值，不再 snap")

    # 3a1. ★每镜内部时序合法：start < end
    #      违反 → 结构错误，抛异常回源重跑
    invalid_order: List[str] = []
    for s, e, m in shot_times:
        if not (e > s):
            invalid_order.append(f"Shot [{s:.2f}-{e:.2f}s] start >= end（时序倒置或为零时长）")
    if invalid_order:
        msg = (
            f"storyboard 时间轴异常：Shot 内 start >= end（共 {len(invalid_order)} 处）。\n"
            + "\n".join(invalid_order[:5])
            + f"\n→ 结构错误，不交 LLM 猜修，请回源重跑 Step 1 MiMo。"
        )
        log(f"  ✗ {msg}")
        raise RuntimeError(msg)

    # 3a. 越界检查：所有 Shot.end ≤ target_duration + SNAP_TOLERANCE
    #     超出 SNAP_TOLERANCE 的越界 → 结构错误，抛异常回源重跑（不交 LLM 猜）
    severe_overrun: List[str] = []
    for s, e, m in shot_times:
        if e > target_duration_f + SNAP_TOLERANCE:
            severe_overrun.append(f"Shot [{s:.2f}-{e:.2f}s] end 超过 target_duration {target_duration_f:.2f}s（超出 {e - target_duration_f:.3f}s > {SNAP_TOLERANCE}s）")
    if severe_overrun:
        msg = (
            f"storyboard 时间轴异常：Shot.end 严重越界 target_duration（共 {len(severe_overrun)} 处）。\n"
            + "\n".join(severe_overrun[:5])
            + "\n→ 结构错误，不交 LLM 猜修，请回源重跑 Step 1 MiMo。"
        )
        log(f"  ✗ {msg}")
        raise RuntimeError(msg)

    # 3b. 未覆盖检查：最后 Shot.end 必须 = target_duration（±SNAP_TOLERANCE）
    #     ≤SNAP_TOLERANCE → 自动 snap 到 target_duration
    #     >SNAP_TOLERANCE → 结构错误，抛异常回源重跑
    if shot_times:
        max_end = max(e for _, e, _ in shot_times)
        gap = target_duration_f - max_end
        if gap > SNAP_TOLERANCE:
            # 明显未覆盖（如 7.20 vs 8.73，差 1.53s）→ 不让 LLM 猜，回源重跑
            msg = (
                f"storyboard 时间轴异常：最后 Shot.end={max_end:.2f}s 远小于 target_duration={target_duration_f:.2f}s"
                f"（未覆盖 {gap:.2f}s > {SNAP_TOLERANCE}s 容差）。\n"
                f"→ 结构错误，不交 LLM 猜修，请回源重跑 Step 1 MiMo。"
            )
            log(f"  ✗ {msg}")
            raise RuntimeError(msg)
        if 0 < gap <= SNAP_TOLERANCE:
            log(f"  ✓ 最后一镜 end 与 canonical target_duration 差 {gap:.3f}s，在容差内，保持 Prompt 原值，不再 snap")

    # 3c. 相邻 Shot 连续性检查：previous.end = next.start（±SNAP_TOLERANCE）
    #     超出容差 → 结构错误，抛异常回源重跑
    if len(shot_times) >= 2:
        sorted_shots = sorted(shot_times, key=lambda x: x[0])
        severe_gap: List[str] = []
        for i in range(1, len(sorted_shots)):
            prev_e = sorted_shots[i - 1][1]
            curr_s = sorted_shots[i][0]
            if abs(curr_s - prev_e) > SNAP_TOLERANCE:
                severe_gap.append(
                    f"Shot{i} start={curr_s:.2f}s ≠ 前一 Shot end={prev_e:.2f}s（差 {abs(curr_s - prev_e):.3f}s > {SNAP_TOLERANCE}s）"
                )
        if severe_gap:
            msg = (
                f"storyboard 时间轴异常：相邻 Shot 不连续（共 {len(severe_gap)} 处）。\n"
                + "\n".join(severe_gap[:5])
                + f"\n→ 结构错误，不交 LLM 猜修，请回源重跑 Step 1 MiMo。"
            )
            log(f"  ✗ {msg}")
            raise RuntimeError(msg)

    # 4. 图N 编号越界检测 —— 代码层只检测不自动修复，交由 Layer 2 LLM 处理
    # （Layer 1 直接替换为"产品图"会破坏语义，如"图7 中的模特" → "产品图中的模特"）
    # ★兼容"图N"（Wan3.0 官方）和"Image N"（旧版）两种引用格式
    image_refs = re.findall(r'(?:Image\s+(\d+)|图(\d+))', out_prompt)
    # findall 返回 tuple，需要 flatten 并取非空值
    image_refs = [n[0] or n[1] for n in image_refs]
    seen_refs = set()
    out_of_range_refs: List[str] = []
    for ref in image_refs:
        if ref in seen_refs:
            continue
        seen_refs.add(ref)
        n = int(ref)
        if n > image_count:
            out_of_range_refs.append(f"图{n}")
    if out_of_range_refs:
        # 不在 Layer 1 修复，仅记录到日志，由 Layer 2 LLM 基于 media_manifest 决策
        log(f"Layer1 检测到 Image 引用越界: {', '.join(out_of_range_refs)}（交 Layer 2 LLM 判断，能唯一确定才修，否则保留）")

    # 4. 仅删除顶层 API 参数行，不触碰引号内口播或画面文字。
    api_line_pattern = re.compile(r"(?im)^\s*(resolution|ratio|duration)\s*:\s*.+$\n?")
    out_prompt, n_subs = api_line_pattern.subn("", out_prompt)
    if n_subs > 0:
        fixes.append(f"删除顶层 API 参数行（{n_subs} 行）")

    # 注：原"多余空行清理"已删除 —— Linter 核心原则是"尽可能不碰正确 Prompt"，格式美化不属于校验

    # ★Plan C：背景一致性校验 —— 只有这道闸【真的该开】时才跑，且判据全走上面的纯函数。
    #   校验1 新背景原话是否进了稿（不再数"前 6 个碎词出现 ≥3 次"）
    #   校验2 是否有"严禁复刻参考视频原背景"或等价禁令
    #   校验3 原背景元素是否残留（≥6 字、非否定/空值型；"无其他物体"这类不算元素）
    # 失败 ⇒ 抛异常让流程【大声停】。不再默认整段重编译：2026-10-22 那单每次红就重发 9 镜一发
    # （实测 173-226s；换到 Cloudflare 那家直接 524），输入一字不变 ⇒ 同一个红必然再来。
    if background_override and background_override.strip():
        bg_text = background_override.strip()
        swap, why = scene_swap_decision(bg_text)
        if not swap:
            log("  ⓘ 背景闸本不该开（%s）⇒ 本次按【保留原片背景】处理，不要求新背景原话、也不禁原背景元素" % why)
        else:
            landed, landed_detail = background_landed(bg_text, out_prompt)
            if not landed:
                raise RuntimeError(
                    "background_override 校验失败：%s。Compiler 未落实背景描述，请重跑 Step 1.5。" % landed_detail)
            forbid_patterns = [
                r"严禁复刻参考视频原背景",
                r"严禁复刻.*?原背景",
                r"不得.*?原背景",
                r"禁止.*?原背景",
                r"不复刻.*?原背景",
            ]
            if not any(re.search(p, out_prompt) for p in forbid_patterns):
                raise RuntimeError(
                    "background_override 校验失败：prompt 缺少'严禁复刻参考视频原背景'或等价禁令。"
                    "请重跑 Step 1.5 Compiler。"
                )
            leaked = original_bg_residue(bg_text, storyboard, out_prompt, _bg_fragments(bg_text))
            if leaked:
                raise RuntimeError(
                    f"background_override 校验失败：prompt 仍含原背景元素 {leaked}，"
                    "未落实禁令。请重跑 Step 1.5 Compiler。"
                )
            log("Layer1 背景一致性校验通过：新背景原话已落实（%s）、禁令存在、原背景元素无残留" % landed_detail)

    if fixes:
        log(f"Layer1 规则校验修复 {len(fixes)} 项: {', '.join(fixes)}")
    else:
        log("Layer1 规则校验通过，无需修复")

    # === Layer 2: LLM 二次审查 ===
    llm_ran = False        # 审查到底跑没跑，要能在返回里被看见（别让人对着一条绿钩子猜）
    try:
        review_result = await _llm_review_prompt(out_prompt, out_target_duration, media_manifest, storyboard, on_log)
        reviewed_prompt = review_result.get("repaired_prompt", "").strip()
        layer2_repairs = review_result.get("repairs", [])
        if reviewed_prompt:
            if len(reviewed_prompt) > 20_000:
                raise RuntimeError(f"Layer2 输出超过 Wan3.0 Prompt 20,000 字符上限：{len(reviewed_prompt)} 字符")
            if storyboard is not None:
                _validate_prompt_deterministic(
                    reviewed_prompt,
                    storyboard,
                    media_manifest,
                    product_name,
                    baseline_prompt=out_prompt,
                    layer2_fixes=layer2_repairs,
                )
            llm_ran = True   # 走到这里 = 审查真跑了且复验通过
            if reviewed_prompt != out_prompt.strip():
                out_prompt, t2, note2 = canonical_prompt_tail(reviewed_prompt)
                if t2:
                    log("  ⓘ Layer2 动过尾行 ⇒ 重新钉回精确合同行（%s）" % note2)
                fixes.append("Layer2 LLM 审查修正分镜逻辑/物理规律问题")
                log("Layer2 LLM 审查已输出修正版")
            else:
                log("Layer2 LLM 审查通过，分镜逻辑合理")
    except Exception as e:
        # 🔴 不再静默降级成「用 Layer1 结果」——那形状是"审查没跑却照样打钩、照样花钱"（假绿）。
        #    同一份稿重问一次（只是一发审查请求，不是整段重编译），再不行就大声停。
        first_err = str(e)[:200]
        log(f"⚠ Layer2 审查第一次没跑成（{first_err}），同一份稿重问一次...")
        try:
            retry = await _llm_review_prompt(out_prompt, out_target_duration, media_manifest,
                                             storyboard, on_log)
            rp = (retry.get("repaired_prompt") or "").strip()
            if rp and rp != out_prompt.strip():
                out_prompt, t3, note3 = canonical_prompt_tail(rp)
                if t3:
                    log("  ⓘ 重问那发的尾行也钉回精确合同行（%s）" % note3)
                fixes.append("Layer2 LLM 审查修正分镜逻辑/物理规律问题（重问一次后）")
            llm_ran = True
        except Exception as e2:                              # noqa: BLE001
            raise RuntimeError(
                "审查腿（Layer2）两次都没跑成，已停止 —— 不再用 Layer1 结果冒充「审查通过」。\n"
                "第一次：%s\n第二次：%s\n"
                "自救：给 config 的 REPLICA_WAN3_LINT_ENGINE 换一条活腿（magpie/mimo/grok/gpt），"
                "或修审查的输出契约。" % (first_err, str(e2)[:200])) from e2

    return {
        "prompt": out_prompt,
        "target_duration": out_target_duration,
        "fixes": fixes,
        "llm_reviewed": "Layer2 LLM 审查修正分镜逻辑/物理规律问题" in fixes,
        "lint_ran": llm_ran,     # 审查真跑过没有（False = 这稿没经过 Layer2，别再往下花钱）
    }


async def _llm_review_prompt(
    prompt: str,
    target_duration: float,
    media_manifest: List[Dict[str, str]],
    storyboard: Optional[List[Dict[str, Any]]] = None,
    on_log: Optional[Callable[[str, None]]] = None,
) -> Dict[str, Any]:
    """调用 GPT 二次审查 prompt 的分镜逻辑、物理规律、脑补/渲染禁令等 9 项。

    Args:
        target_duration: Step 0 锁死的全局时间事实源（float 秒），全流程唯一变量名。
            仅作为 LLM 上下文展示，不得修改。
        media_manifest: Step 0 后端固定的 图N 编号归属清单（含 image_id/label/path）。
            LLM 基于此清单做"可唯一确定的 Image 引用修复"，不再凭编号顺序猜测。

    返回结构化结果：{"repaired_prompt": str, "repairs": list}。
    若无需修改或审查失败，repaired_prompt 原样返回，repairs 为空。
    """
    # ③审查腿可以独立选家（config.REPLICA_WAN3_LINT_ENGINE，取值 magpie/mimo/grok/gpt）；空=跟②同一条。
    lint_engine = str(getattr(settings, "REPLICA_WAN3_LINT_ENGINE", "") or "").strip()
    try:
        client, model = _compiler_llm(lint_engine)
    except RuntimeError as exc:
        # 不 swallow：起不来就是起不来，让上层重问一次或大声停（见 validate_and_fix_prompt 的 Layer2 分支）
        raise RuntimeError("审查腿起不来：%s" % exc) from exc
    if on_log:
        on_log("审查腿=%s（model=%s，与编译腿%s）" % (
            lint_engine or "同编译腿", model or "未知", "分开" if lint_engine else "共用"))

    system = """你是 Wan3.0 视频生成提示词审查员（Linter / Sanitizer）。
你不是第二个创作者，不优化、不润色、不重写。你只做"客观错误的最小修复"。

【最高原则】
如果没有发现"能确认存在错误 + 能确定唯一正确答案"的客观错误，必须使 `repaired_prompt` 与输入 Prompt 逐字一致。
注意"逐字一致"是指 JSON decode 后的 `repaired_prompt` 字符串与输入 Prompt 的字符序列完全一致，包括空格、标点、换行和格式。
本 Linter 追求 Precision 极高、Recall 可以低：漏掉 5 个问题可以接受，把 1 个正确 Prompt 改坏更危险。

【决策树】每一处修改都必须通过此决策树，任一否即不改：
发现异常
  → 是不是明确客观错误？
      否 → 不改
      是 → 有没有唯一正确修复方案？
              否 → 不改
              是 → 是否会触碰口播/卖点/音效/运镜/台词？
                      是 → 不改
                      否 → 只做最小字符级修改

【只修这 2 类客观错误】

1. **明确且可唯一修复的物理文本错误**（只在文本明确写明违反物理、且存在唯一合理修复时才改）
   - 只有文本自身形成明确的物理或语义矛盾，且不依赖对未提供画面的推测时，才进入修复候选
   - 只要存在合理物理情境可以使该描述成立，就不得修改
   - "杯子落下弹起""头发飘动"是正常物理，不得修改
   - AI 视频生成时的穿模/结构突变是 Wan 执行问题，不是 Prompt 文本问题，不在此层处理
   - 无唯一合理修复方案时不处理，原样保留

2. **Image 引用错误**（基于 user 消息中的 media_manifest）
   - **若 user 消息未提供 media_manifest，或 manifest 为空，则不校验、不补全、不修改任何 Image 引用**
   - **若 manifest 存在但未定义某类素材对应关系**（如未声明模特图），不得自行猜测编号对应关系
   - **越界**：引用了 manifest 不存在的编号；只有先客观确认当前引用承担的素材角色，再在同类型候选中唯一确定正确 图N 时才修正
   - **类型错绑**：引用的 图N 在 manifest 中存在，但其素材类型与当前文本明确承担的角色不一致；只有角色唯一确定、manifest 存在该类型、且该类型只有一个合法 图N 时才修正
   - **Image 素材类型匹配**：当前 Shot 文本必须客观确认 Image 的素材角色：目标产品 → product；主体人物/模特 → model；目标场景 → scene。角色无法唯一判断时不修改；manifest 不存在对应类型时不修改，不得用其他类型替代
   - **完全漏写 Image**：Layer 2 不补写任何缺失的 Image 引用。若 canonical storyboard 判定某 Shot 应存在某类 Image 引用，但 Prompt 中完全缺失，Layer 2 不修改、不生成 repair，由后端 Validator 判失败并回退或重跑 Step 1.5 Compiler。
   - **不增不减**：Layer 2 只修复已经存在的 Image 引用：越界或类型错绑；不得新增、删除或重排 Image 引用

【目标商品可见性判定】
是否属于“目标商品应可见 Shot”，只依据输入的 canonical storyboard 时间轴摘要：
- product_visible=false → 原商品不可见，不得新增产品 图N
- product_visible=true → 目标商品应可见 Shot
- 不得根据 Compiler Prompt 是否已经写出目标产品，反向判断商品是否应出现
- 即使判定为目标商品应可见 Shot，补哪个 图N 仍必须满足上面的唯一性条件

★已删除"产品视觉特征矛盾"可修类别：
   - Layer 2 输入仅 prompt + target_duration + media_manifest + product_name + storyboard
   - 没有产品图片实际视觉内容，无法可靠判断产品颜色/形状/材质矛盾
   - 商品视觉准确性由 Step 1.5 Compiler 负责（Compiler 接收产品图 Base64）
   - Layer 2 不再创造第二个视觉判断器

【repair log Contract】
【JSON 字符串一致性定义】
“repaired_prompt 与输入 Prompt 逐字一致”是指：对 Layer 2 输出执行标准 JSON decode 后，得到的 repaired_prompt 字符串必须与输入 Prompt 的字符序列完全一致。
- 双引号、反斜杠和换行必须按 JSON 规则转义；这些序列化转义不视为修改 Prompt。
- 禁止因为 JSON 包装而修改 repaired_prompt 解码后的空格、换行、标点或正文字符。

【顶层 JSON Schema】
输出 JSON object 必须且只能包含两个 key：{"repaired_prompt":"<string>","repairs":[]}
- repaired_prompt 类型必须为 string；repairs 类型必须为 array。
- 禁止任何其他顶层 key、重复 key、null，以及 JSON object 之外的任何字符。

【repairs Schema】
repairs 必须是 JSON array。数组中每个元素必须且只能是以下两种 object 之一，禁止 reason、explanation、confidence、analysis 或任何额外 key：
1. Image 引用修复：{"type":"image_reference","shot":<integer>,"old":"图N","new":"图N"}
   - 越界或类型错绑替换使用 old="图N"；不允许 old=""；new 必须为 图N；shot 必须是实际修改的 Shot 编号。
2. 物理文本修复：{"type":"physical_text","shot":<integer>,"old":"<被替换的原始精确文本>","new":"<唯一正确的替换文本>"}
   - shot 必须是实际修改的 Shot 编号。
【repair 唯一定位规则】
任何 repair 必须同时满足：
- shot 唯一定位目标 Shot；
- old 必须在该 Shot 的原始输入文本中精确出现且仅出现 1 次；
- old 出现 0 次或 2 次及以上时，一律不修改、不生成 repair；
- 不使用 occurrence index、offset，也不得猜测重复项中的哪一个需要修改。
该规则同时适用于 image_reference 和 physical_text。

- 每一条 repair 必须对应 repaired_prompt 中真实发生的一次修改；repaired_prompt 中每一次允许修改也必须存在对应 repair。
- 无修改时 repaired_prompt 必须与输入 Prompt 逐字一致，repairs 必须为 []。

【严禁触碰的内容（违反即判失败）】

❌ **严禁修改任何时间字段**（时间轴由 Layer 1 代码层全权负责，LLM 永远不碰）
   - 不修改任何 Shot 编号
   - 不修改 start / end / duration
   - 不修改 target_duration
   - 不新增、删除、重排 Shot
   - 时间错误回退到 Step 1（storyboard 错）或 Step 1.5（Compiler 抄错），Layer 2 LLM 永远不修时间

❌ **严禁修改口播/台词/文案**
   - 引号内的所有口播内容、台词、广告语一律保留
   - 比喻、夸张、对比等广告修辞手法一律保留
   - 功效宣传词、材质描述、卖点描述一律保留
   - 你不判断文案是否"脑补""合规""夸张"，只判断客观事实错误

❌ **严禁修改音效和运镜术语**
   - 音效描述（XX声、环境白噪音等）保留
   - 运镜术语（Hard cut / 平滑切换 / 蒙太奇 等）保留
   - 语音风格标记（[characterN, 平稳/强调/上扬] 等）保留

❌ **严禁判镜头衔接问题**
   - 你看不到画面，无法判断"突兀跳转""视角冲突"
   - 所有剪辑手法（Hard cut / 平滑切换 / 蒙太奇）都是合法的，不得修改

【冲突优先级】
当可修项与禁止修改项发生冲突，或不存在唯一客观正确答案时，**一律不修改**。
例：Shot 1 写蓝色杯子，Shot 2 写红色杯子，口播说"这个红色真亮眼"——无权威源，不改。

【最小修改原则】
每一次修改必须是解决已确认错误所需的最小字符级修改。
- 禁止顺手润色、补充细节、重写句子、统一文风、优化 Prompt
- 例：确认 图4 应为 图2，只改数字 4→2，不得变成"严格参考 图2 中优雅的白色连衣裙"

【反啰嗦硬约束（最高优先级，违反任一即判失败）】
A. 禁止输出任何推理过程、分析、理由、思路、审查步骤
B. 禁止输出任何元描述（解释你做了什么、改了什么、为什么改）
C. 禁止输出任何前后缀寒暄（"好的""以下是""请参考"等）
D. repaired_prompt 的字符数应接近输入 Prompt；外层 JSON 和 repairs 的字符数不参与此限制

【输出严格约束（违反任一条直接判失败）】
1. 只输出一个 JSON object，格式必须为 {"repaired_prompt":"<完整 prompt>","repairs":[]}；不得输出任何前言、后记、解释、分析、审查过程或 Markdown
2. `repaired_prompt` 的第一字节必须与输入 Prompt 第一字节一致；外层 JSON 的 `{` 不属于 Prompt 内容，不参与此比较
3. 禁止自行添加审查结果类标题或前缀（如"修正版："、"审查结果："、"修正后："等）；输入 Prompt 原本存在的标题、标签或段落名必须原样保留，除非其自身属于允许修复的客观错误
4. repaired_prompt 必须保留输入 Prompt 的原有 Markdown 符号、列表、标签、标点和排版（包括 [Shot N: X-Ys]、[characterN, ...] 等）；外层 JSON 仅用于承载 repaired_prompt 和 repairs
5. 禁止输出"无问题"、"通过"等结论性文字
6. 如果无问题：仍然只输出规定的 JSON object；repaired_prompt 必须与输入 Prompt 逐字一致；repairs 必须为 []；不得直接裸输出 Prompt
7. 保持原 Prompt 的 Shot 编号、分镜数量、时间区间、Image 引用及其他内容不变；仅允许对本规则明确列出的 2 类可修错误、且通过决策树验证的部分执行最小字符级修改；除此之外全部逐字保留
8. 严禁新增、删除、重排分镜；严禁修改任何分镜时间（start/end/duration）；时间轴由 Layer 1 代码层全权负责
9. repaired_prompt 的字符数应接近输入 Prompt；外层 JSON 和 repairs 的字符数不参与此限制，不得大幅扩写或缩写
10. 严禁在 repaired_prompt 内添加"以下是""修正版""审查后"等引导词；repaired_prompt 第一字节起必须直接是原 Prompt 内容

**只输出规定的 JSON object，不要任何其他内容。**"""

    # 构造 media_manifest 文本：基于 Step 0 固定的真实编号归属清单
    #   - 图1 的主产品图语义由固定 manifest 顺序确定
    #   - LLM 仅基于清单做可唯一确定的 Image 引用修复（支持 图N 和旧版 Image N 格式）
    visibility_summary = json.dumps([
        {
            "shot": shot.get("shot", index + 1),
            "product_visible": shot_original_product_visible(shot),
        }
        for index, shot in enumerate(storyboard or [])
    ], ensure_ascii=False)
    if media_manifest:
        manifest_lines = ["media_manifest:"]
        for item in media_manifest:
            image_id = item.get("image_id", "")
            label = item.get("label", "")
            manifest_lines.append(f"- {image_id} = {label}")
        manifest_block = "\n".join(manifest_lines)
        user = f"""target_duration（Step 0 全局硬参数，不得修改）: {target_duration}s
★进入 Layer 2 的 Prompt 时间轴必须对应已由后端 Checkpoint A 验证并 canonicalize 的 storyboard：
  - Layer 2 只复验 Shot 的 start/end 是否与 canonical storyboard 一致
  - 不负责判断是否回源，不负责修复时间，不负责 snap
★因此 Layer 2 不执行任何时间轴修复：
  - 不修改任何 Shot 时间（包括 start/end/时长）
  - 不修改 target_duration 本身
  - 不新增/删除 Shot 来"填满"时间轴
  - 不猜测应该如何分配未覆盖的时间

{manifest_block}

canonical storyboard 商品可见性摘要（唯一权威来源）：
{visibility_summary}

待审查 prompt:
{prompt}

只返回一个合法 JSON object，不要任何其他内容。

有修改时：
{{"repaired_prompt":"<完整 repaired_prompt>","repairs":[{{"type":"<image_reference 或 physical_text>","shot":<integer>,"old":"<原始精确文本>","new":"<唯一修复后的文本>"}}]}}

无修改时：
{{"repaired_prompt":"<输入 Prompt，JSON decode 后逐字一致>","repairs":[]}}"""
    else:
        user = f"""target_duration（Step 0 全局硬参数，不得修改）: {target_duration}s
★进入 Layer 2 的 Prompt 时间轴必须对应已由后端 Checkpoint A 验证并 canonicalize 的 storyboard：
  - Layer 2 只复验 Shot 的 start/end 是否与 canonical storyboard 一致
  - 不负责判断是否回源，不负责修复时间，不负责 snap
★因此 Layer 2 不执行任何时间轴修复：
  - 不修改任何 Shot 时间（包括 start/end/时长）
  - 不修改 target_duration 本身
  - 不新增/删除 Shot 来"填满"时间轴
  - 不猜测应该如何分配未覆盖的时间

media_manifest: (空，无参考图)

canonical storyboard 商品可见性摘要（唯一权威来源）：
{visibility_summary}

待审查 prompt:
{prompt}

只返回一个合法 JSON object，不要任何其他内容。

有修改时：
{{"repaired_prompt":"<完整 repaired_prompt>","repairs":[{{"type":"<image_reference 或 physical_text>","shot":<integer>,"old":"<原始精确文本>","new":"<唯一修复后的文本>"}}]}}

无修改时：
{{"repaired_prompt":"<输入 Prompt，JSON decode 后逐字一致>","repairs":[]}}"""

    try:
        completion = await asyncio.to_thread(
            client.chat.completions.create,
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=8192,
            temperature=0.1,
        )
        content = (completion.choices[0].message.content or "").strip()
    except Exception as e:                              # noqa: BLE001
        raise RuntimeError("审查腿这一发没跑成：%s" % str(e)[:200]) from e

    # Layer 2 必须返回结构化 JSON，不接受纯文本或任何前后缀。
    def reject_duplicate_keys(pairs: List[tuple]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"JSON 存在重复 key：{key}")
            result[key] = value
        return result

    try:
        result = json.loads(content, object_pairs_hook=reject_duplicate_keys)
        if not isinstance(result, dict) or set(result) != {"repaired_prompt", "repairs"}:
            raise ValueError("根节点必须且只能包含 repaired_prompt 和 repairs")
        content = result.get("repaired_prompt")
        repairs = result.get("repairs")
        if not isinstance(content, str) or not isinstance(repairs, list):
            raise ValueError("必须包含 repaired_prompt 字符串和 repairs 数组")
        for repair in repairs:
            if not isinstance(repair, dict):
                raise ValueError("repair 必须是 object")
            repair_type = repair.get("type")
            if repair_type == "image_reference":
                if set(repair) != {"type", "shot", "old", "new"}:
                    raise ValueError("image_reference repair key 不符合 Schema")
                if not isinstance(repair.get("shot"), int) or isinstance(repair.get("shot"), bool):
                    raise ValueError("image_reference repair 的 shot 必须是 integer")
                if not isinstance(repair.get("old"), str) or not isinstance(repair.get("new"), str) or not repair.get("new"):
                    raise ValueError("image_reference repair 缺少有效 old/new")
                # ★兼容"图N"（Wan3.0 官方）和"Image N"（旧版）两种引用格式
                _ref_pat = r"(?:Image\s+\d+|图\d+)"
                if repair["old"] and not re.fullmatch(_ref_pat, repair["old"]):
                    raise ValueError("image_reference repair 的 old 格式非法")
                if not re.fullmatch(_ref_pat, repair["new"]):
                    raise ValueError("image_reference repair 的 new 格式非法")
            elif repair_type == "physical_text":
                if set(repair) != {"type", "shot", "old", "new"}:
                    raise ValueError("physical_text repair key 不符合 Schema")
                if not isinstance(repair.get("shot"), int) or isinstance(repair.get("shot"), bool):
                    raise ValueError("physical_text repair 的 shot 必须是 integer")
                if not isinstance(repair.get("old"), str) or not repair.get("old") or not isinstance(repair.get("new"), str):
                    raise ValueError("physical_text repair 缺少有效 old/new")
            else:
                raise ValueError("repair type 必须是 image_reference 或 physical_text")
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        # 🔴 2026-10-22 实测：千问 3 发里 2 发、deepseek 1 发都栽在这条 repair-JSON 契约上
        #    （old 不是整串 图N / 输出里带裸控制字符）。旧形状是"整份丢弃、保留原版"⇒ 审查静默没跑。
        #    现在如实抛，让上层重问一次或大声停；宁可不改，不许假绿。
        raise RuntimeError("审查输出不是合法 repair JSON：%s" % str(e)[:200]) from e
    content = content.strip()

    # 反啰嗦兜底：检测推理引导词，命中直接保留原版
    VERBOSE_MARKERS = [
        '经过分析', '该 prompt 存在', '该prompt存在', '存在问题',
        '修正如下', '修改如下', '调整如下',
        '已将', '已修改', '已删除', '已替换',
        '保留原结构', '保持原结构',
        '审查结果', '审查后', '检查发现',
        '主要问题', '问题如下', '问题点',
    ]
    verbose_hits = [m for m in VERBOSE_MARKERS if m in content[:200]]  # 只检查开头 200 字
    if verbose_hits:
        raise RuntimeError("审查输出含推理引导词 %s ⇒ 判不可用（宁可不改，不拿一份没审的稿冒充审过）"
                           % "、".join(verbose_hits))

    # ═══════════ 2 道结构性保险栓（只防结构改坏，不干预内容修正）═══════════
    fallback_reasons = []

    # 保险栓 1: 分镜数量必须一致（Shot N 或 镜头 N）—— 防止 GPT 新增/删除分镜
    orig_shots = re.findall(r'(?:Shot|镜头)\s*(\d+)', prompt)
    new_shots = re.findall(r'(?:Shot|镜头)\s*(\d+)', content)
    if len(orig_shots) != len(new_shots) and orig_shots:
        fallback_reasons.append(f"分镜数变了（{len(orig_shots)}→{len(new_shots)}）")

    # 保险栓 2: Image 引用只允许 repairs 声明的已有引用替换。
    # 缺失引用不由 Layer 2 补写，由后端 Validator 触发回退或重跑。

    if fallback_reasons:
        raise RuntimeError("审查触发结构性保险栓：%s ⇒ 判不可用" % "，".join(fallback_reasons))

    if on_log and content != prompt:
        delta = len(content) - len(prompt)
        sign = "+" if delta >= 0 else ""
        on_log(f"✓ 审查通过，采用修正版（{len(prompt)} → {len(content)} 字，{sign}{delta}）")

    return {"repaired_prompt": content or prompt, "repairs": repairs}


def _storyboard_system_prompt(target_duration: float, split_mode: str) -> str:
    """分镜腿的 system prompt = 受保护那份原文 + 一段调用侧补强。

    🔴 pipeline/step1_mimo_parse.py 是 BOUNDARY.md 整文件保护（一个字不动），补强只能经
       `system_prompt=` 这个它自己开着的参数传进来；正文用 import 引用、不复制，
       复制一份 = 第二个事实源，必然和原文漂移。
    🔴 补的是什么：原文【固定空值规则】要求「无主体商品时产品/产品动作/商品交互三格全填无」，
       校验器（_validate_result）也照这条判红，但从没告诉模型「判无之后，这一格里对道具的真实动作
       写去哪」。模型只剩两条路：谎报无动作，或把道具改写成产品 —— 于是按下 Shot 4 就冒出 Shot 9。
       实测 2026-10-22 sc_5a859fb5c1f1：第一轮 Shot 4「产品=无 / 产品动作=从收纳状态变为展开状态」，
       管线自带的那一次纠错重试把它修好了，第二轮换成 Shot 9「产品=无 / 交互=涂抹·肩背部·使用过程」，
       而第二轮的原文不落盘（mimo_raw.json 只写第一轮）⇒ 看着像随机死亡。
    ⚠️ 传自定义 prompt 会让 run() 走 _is_custom_prompt 分支，user 消息里「当前视频目标时长为 X 秒」和
       「输出一个 source_storyboard JSON 数组」两句一起消失 ⇒ 这两件必须在这里补回来，
       否则时间轴覆盖不到 target_duration，Checkpoint A 判红。
    """
    from pipeline.step1_mimo_parse import SYSTEM_PROMPT
    return SYSTEM_PROMPT + f"""

====================
【超级复刻·调用侧补强】与上文冲突时以本节为准。
====================
1. 分镜切分模式 = 「{split_mode}」（scene_cut 即上文模式A，action_grained 即上文模式B）。上文提到的「分镜切分模式」由这一句给出，不要猜。
2. target_duration = {target_duration:.2f} 秒，是后端 ffprobe 实测的唯一权威时长。第一个 Shot 从 00:00.00 开始，最后一个 Shot 的结束时间必须等于 {target_duration:.2f} 秒，Shot 之间不留空洞、不重叠。
3. 主体原商品在整段视频里只有一件。先按【主体原商品识别规则】定下这一件，此后每个 Shot 的「产品」字段都用同一个称呼指同一件东西；不要因为某镜头里被操作的是别的道具（毛巾、器皿、包装盒、家具），就把那件道具写成产品。
4. 某 Shot 里这件主体原商品确实不可见时，「产品」「产品动作」「交互方式」「交互部位」「营销功能」五格全填「无」；该镜头里对道具的真实操作改写进「画面动作.画面内容」和「画面动作.人物动作」两格。真实动作不得因为判了无就删掉，也不得留在「产品动作」里。
5. 只输出一个顶层 JSON 数组，逐 Shot 只含【唯一输出 Schema】列出的字段：不要 gen_mode，不要时间轴数组，不要解析依据和合并段表，不要 Markdown、代码块或任何解释文字。
"""


# ─────────────────────────────────────────────────────────────────
# Step 1: MiMo = 导演 + 广告改写器
#   原视频 + 产品图 → 一条 Wan3.0 精准提示词
# ─────────────────────────────────────────────────────────────────
async def step1_mimo_director(
    video_path: str,
    output_dir: str,
    target_duration: float,
    on_log: Optional[Callable[[str, None]]] = None,
) -> Dict[str, Any]:
    """MiMo 视频解析器：只解析原视频，输出 storyboard JSON。

    ★职责极其干净：MiMo 只看原视频（A），回答"原片到底怎么拍的"。
    - 不输入产品图/模特图/场景图（避免污染原视频解析）
    - 不输入 customer_request/product_name/product_facts（这些是 Step 1.5 GPT 的输入）
    - 不拼装 prompt（prompt 编译交给 Step 1.5 GPT 编译器）

    target_duration：Step 0 后端用 ffprobe 确定的输出时长硬参数，MiMo 不得改变，
                     storyboard 时间轴必须以此为边界（完整覆盖 0 → target_duration）。

    Returns:
        {
            "storyboard": list,   # MiMo 分镜（原视频结构，纯解析）
        }

    ★不再输出 mimo_detected_duration：ffprobe 得到的就是 target_duration（Step 0 已锁死），
      MiMo 自身没有"原视频时长"独立事实源。全流程唯一时间事实源 = target_duration。
    """
    def log(msg: str) -> None:
        if on_log:
            on_log(msg)
        print(f"  [Step1·分镜腿] {msg}", flush=True)

    cfg = _cfg()
    # 同步 settings.MIMO_API_KEY 到环境变量（pipeline.step1_mimo_parse 用 os.environ.get）
    os.environ["MIMO_API_KEY"] = settings.MIMO_API_KEY
    from pipeline.step1_mimo_parse import run as mimo_run

    # 🔴 分镜腿用哪家 = config 的开关（唯一源 _storyboard_lane），解不出来就在这里拒：
    #    不拒就会静默落回那份受保护文件签名里的 `mimo-v2.6-flash` 默认值 —— 那是"回执写 A、实跑 B"的形状。
    _sb = cfg.get("storyboard_lane") or {}
    if _sb.get("error"):
        raise RuntimeError("Step1 分镜腿起不来：%s" % _sb["error"])
    if not (_sb.get("base_url") and _sb.get("api_key") and _sb.get("model")):
        raise RuntimeError("Step1 分镜腿三元组不全（base_url/api_key/model）⇒ 不发请求；当前 provider=%s"
                           % _sb.get("provider"))
    log("分镜腿=%s（model=%s，ledger=%s）" % (_sb.get("provider"), _sb.get("model"), _sb.get("ledger")))

    mimo_out = str(Path(output_dir) / "step1_mimo")
    Path(mimo_out).mkdir(parents=True, exist_ok=True)

    # ★Step 1 只解析原视频，不输入任何新素材（产品图/模特图/场景图/customer_request/product_name）
    #   - 避免新素材污染原视频解析（例如新场景是咖啡店 → MiMo 可能误把原视频白色卧室解析成咖啡店）
    #   - 新素材的迁移统一由 Step 1.5 GPT 编译器处理
    log("启动分镜腿（纯视频解析器）解析原视频 → storyboard JSON")
    log("分镜腿 prompt = 受保护原文 + 调用侧补强（无主体商品时把道具动作改写进画面内容/人物动作；"
        "重申 target_duration=%s 与只出一个数组、不出 gen_mode）" % ("%.2f" % target_duration))
    # #region debug-point A:mimo-asr-path
    try:
        import urllib.request
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name,sample_rate,channels", "-of", "json", video_path],
            capture_output=True, text=True, encoding="utf-8", timeout=20,
        )
        urllib.request.urlopen(urllib.request.Request(
            "http://127.0.0.1:7777/event",
            data=json.dumps({
                "sessionId": "nasal-speech-asr", "runId": "pre-fix", "hypothesisId": "A",
                "location": "backend/services/super_clone_wan3.py:step1_mimo_director",
                "msg": "[DEBUG] MiMo input audio probe; no independent FunASR call in super clone step",
                "data": {"video": Path(video_path).name, "audio_probe": probe.stdout[:1000], "returncode": probe.returncode},
            }).encode(), headers={"Content-Type": "application/json"}), timeout=3).read()
    except Exception:
        pass
    # #endregion
    result = await asyncio.to_thread(
        mimo_run,
        video_path=video_path,
        product_name="",  # 不传产品名：MiMo 不做产品替换，只解析原视频结构
        output_dir=mimo_out,
        fps=cfg["mimo_fps"],
        media_resolution=cfg["mimo_resolution"],
        split_mode=cfg["mimo_split_mode"],
        base_url=_sb["base_url"],   # 🔴 显式传：step1 的签名默认值是小米那个域名，
        api_key=_sb["api_key"],     #    不传就只能跑默认那一家（config 的 MIMO_BASE_URL 也救不回来 —— 它在 api_key 非空时不参与）
        model=cfg["mimo_model"],   # 唯一源 = _storyboard_lane()（2026-10-20 起按开关指 agnes 或小米），
                                   #    不用 step1_mimo_parse 签名里的 flash 默认值（那份 BOUNDARY 只调不改）
        target_duration=target_duration,  # Step 0 唯一权威时长，供局部时间轴校验
        product_image_paths=None,   # ★不传产品图
        model_image_path=None,      # ★不传模特图
        scene_image_path=None,      # ★不传场景图
        customer_request=None,      # ★不传客户需求
        # 🔴 受保护文件里唯一开着的注入口；正文 import 自那份 SYSTEM_PROMPT，只在尾部补一段（见 _storyboard_system_prompt）
        system_prompt=_storyboard_system_prompt(target_duration, cfg["mimo_split_mode"]),
    )

    storyboard = result.get("storyboard") or []
    # #region debug-point D:storyboard-voiceover
    try:
        import urllib.request
        speech = [
            {"shot": s.get("shot"), "voiceover": s.get("口播"), "time": s.get("时间区间")}
            for s in storyboard if isinstance(s, dict) and s.get("口播") not in (None, "", "无")
        ]
        urllib.request.urlopen(urllib.request.Request(
            "http://127.0.0.1:7777/event",
            data=json.dumps({
                "sessionId": "nasal-speech-asr", "runId": "pre-fix", "hypothesisId": "D",
                "location": "backend/services/super_clone_wan3.py:step1_mimo_director",
                "msg": "[DEBUG] MiMo storyboard voiceover fields captured",
                "data": {"storyboard_count": len(storyboard), "voiceover_count": len(speech), "voiceover": speech[:20]},
            }).encode(), headers={"Content-Type": "application/json"}), timeout=3).read()
    except Exception:
        pass
    # #endregion
    if not storyboard:
        raise RuntimeError("MiMo 未输出 storyboard")

    # FunASR 负责口播，MiMo 只负责画面和镜头，按时间区间把 ASR 结果写回 storyboard。
    asr_dir = Path(output_dir) / "step1_mimo"
    voiceover_segments = await _extract_voiceover(video_path, asr_dir, target_duration)
    if voiceover_segments:
        def _time_value(value: Any) -> float:
            if isinstance(value, (int, float)):
                return float(value)
            text = str(value or "0").strip().replace("秒", "").replace("s", "")
            if ":" in text:
                minute, second = text.split(":", 1)
                return float(minute) * 60.0 + float(second)
            return float(text or 0)

        matched = 0
        for shot in storyboard:
            interval = shot.get("时间区间") or shot.get("time_range") or {}
            shot_start = _time_value(interval.get("开始时间", interval.get("start", 0)))
            shot_end = _time_value(interval.get("结束时间", interval.get("end", shot_start)))
            texts = [
                str(seg.get("text", "")).strip()
                for seg in voiceover_segments
                if float(seg.get("start", 0)) < shot_end
                and float(seg.get("end", 0)) > shot_start
                and str(seg.get("text", "")).strip()
            ]
            shot["口播"] = " ".join(texts) if texts else "无"
            if texts:
                matched += 1
        log(f"✓ FunASR 口播已覆盖 storyboard：{len(voiceover_segments)} 段 ASR，匹配 {matched}/{len(storyboard)} 镜")
    else:
        log("⚠ FunASR 未识别到口播，保留画面解析并清空 MiMo 臆测口播")
        for shot in storyboard:
            shot["口播"] = "无"

    log(f"✓ MiMo 纯解析完成：{len(storyboard)} 镜 storyboard（画面由 MiMo，口播由 FunASR）")

    # ★Step 1 不再输出 mimo_detected_duration
    #   - ffprobe 得到的就是 target_duration（Step 0 已锁死，唯一时间事实源）
    #   - MiMo 自身没有"原视频时长"独立事实源（它只输出 storyboard 的时间区间）
    #   - 不再让 ffprobe(video_path) 在 Step 1 重复跑一次（与 Step 0 重复且语义混乱）
    return {
        "storyboard": storyboard,
    }


# ─────────────────────────────────────────────────────────────────
# Step 2: Wan3.0 / H3 = 执行器
#   原视频 + 产品图 + MiMo 提示词 → 成品视频
# ─────────────────────────────────────────────────────────────────
async def step2_h3_execute(
    prompt: str,
    video_path: str,
    product_image_paths: List[str],
    target_duration: float,
    output_dir: str,
    on_log: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[str, int, str], None]] = None,
    aspect_ratio: Optional[str] = None,
    media_manifest: Optional[List[Dict[str, str]]] = None,
    model_image_path: Optional[str] = None,
    scene_image_path: Optional[str] = None,
) -> Dict[str, Any]:
    """RunningHub H3 全能参考：视频 + 产品/模特/场景参考图 + Prompt。"""
    import httpx

    def log(msg: str) -> None:
        if on_log:
            on_log(msg)
        print(f"  [Step3·H3-RH] {msg}", flush=True)

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / "h3_output.mp4"
    duration = max(2, min(15, int(round(float(target_duration) or 5))))
    # 🔴 2026-10-09 用户指令"视频严格杜绝走 RunningHub"。这条 H3 腿自己拼 HTTP（下面打
    #    /openapi/v2/minimax/hailuo-h3/multimodal-to-video），【不经 submit_task 那个咽喉】，
    #    所以调同一份判据（判据只有 rh_video_guard 一份，这里不复制规则）。
    #    放在取密钥/上传素材之前 ⇒ 被拒时一个请求都不发，也不会留下"素材已上传、任务没提交"的半截状态。
    rh_video_guard()
    base_url = "https://www.runninghub.ai"
    api_key = (
        os.environ.get("RUNNINGHUB_H3_API_KEY", "").strip()
        or str(getattr(settings, "RUNNINGHUB_H3_API_KEY", "")).strip()
        or os.environ.get("RUNNINGHUB_API_KEY", "").strip()
        or str(getattr(settings, "RUNNINGHUB_ENTERPRISE_API_KEY", "")).strip()
        or str(getattr(settings, "RUNNINGHUB_API_KEY", "")).strip()
    )
    if not api_key:
        raise RuntimeError("未配置 RunningHub API Key")
    headers = {"Authorization": f"Bearer {api_key}"}

    if on_progress:
        on_progress("h3_uploading", 10, "上传原视频和参考图到 RunningHub")
    async with httpx.AsyncClient(timeout=300.0, follow_redirects=True) as client:
        async def upload(path: str) -> str:
            p = Path(path)
            if not p.exists():
                raise FileNotFoundError(f"文件不存在: {path}")
            with p.open("rb") as handle:
                response = await client.post(
                    f"{base_url}/openapi/v2/media/upload/binary",
                    headers=headers,
                    files={"file": (p.name, handle)},
                )
            response.raise_for_status()
            data = response.json()
            url = (data.get("data") or {}).get("download_url")
            if not url:
                raise RuntimeError(f"RunningHub 上传失败，响应无 download_url: {data}")
            return url

        video_url = await upload(video_path)
        manifest = media_manifest or _build_media_manifest(
            product_image_paths, model_image_path, scene_image_path
        )
        image_items = [
            item for item in manifest
            if item.get("path") and Path(str(item["path"])).exists()
        ]
        image_urls = []
        image_labels = []
        for item in image_items:
            image_urls.append(await upload(str(item["path"])))
            image_labels.append(f'{item.get("image_id")} {item.get("label")}')
        payload = {
            "prompt": prompt,
            "imageUrls": image_urls,
            "videoUrls": [video_url],
            "audioUrls": [],
            "resolution": "768P",
            "duration": str(duration),
            "ratio": aspect_ratio or "adaptive",
            "aigc_watermark": False,
        }
        (out_dir / "h3_runninghub_request.json").write_text(
            json.dumps(
                {
                    "endpoint": f"{base_url}/openapi/v2/minimax/hailuo-h3/multimodal-to-video",
                    "image_labels": image_labels,
                    "payload": payload,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        (out_dir / "h3_prompt_utf8.txt").write_bytes(prompt.encode("utf-8"))
        log(
            f"提交 RunningHub H3：videoUrls=1，imageUrls={len(image_urls)}"
            f"（{' / '.join(image_labels) or '无参考图'}），"
            f"768P/{duration}s，prompt_utf8={len(prompt.encode('utf-8'))} bytes"
        )
        if on_progress:
            on_progress("h3_submitting", 20, "RunningHub H3 接收视频、参考图和最终 Prompt")
        request_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        response = await client.post(
            f"{base_url}/openapi/v2/minimax/hailuo-h3/multimodal-to-video",
            headers={**headers, "Content-Type": "application/json; charset=utf-8"},
            content=request_body,
        )
        response.raise_for_status()
        submitted = response.json()
        task_id = submitted.get("taskId")
        if not task_id:
            raise RuntimeError(f"RunningHub H3 提交失败: {submitted}")
        log(f"RunningHub H3 已提交：{task_id}")

        started = asyncio.get_running_loop().time()
        result_url = None
        while asyncio.get_running_loop().time() - started < 1800:
            await asyncio.sleep(10)
            query_response = await client.post(
                f"{base_url}/openapi/v2/query",
                headers={**headers, "Content-Type": "application/json"},
                json={"taskId": task_id},
            )
            query_response.raise_for_status()
            status_data = query_response.json()
            status = str(status_data.get("status", "")).upper()
            log(f"RunningHub H3 状态：{status}")
            if status == "SUCCESS":
                results = status_data.get("results") or []
                result_url = results[0].get("url") if results and isinstance(results[0], dict) else None
                if not result_url:
                    raise RuntimeError(f"RunningHub H3 成功但没有输出 URL: {status_data}")
                break
            if status in ("FAILED", "CANCELLED", "ERROR"):
                raise RuntimeError(f"RunningHub H3 失败：{status_data.get('errorMessage') or status_data}")
        if not result_url:
            raise RuntimeError(f"RunningHub H3 轮询超时：{task_id}")
        download = await client.get(result_url, timeout=300.0)
        download.raise_for_status()
        output_path.write_bytes(download.content)

    result_path = str(output_path)
    result_duration = _ffprobe_duration(result_path)
    log(f"RunningHub H3 生成完成：{result_path}，实际时长={result_duration or 0:.3f}s")
    return {
        "raw_result_url": result_url,
        "raw_output_duration": result_duration,
        "raw_task_id": task_id,
        "last_task_id": task_id,
        "task_id": task_id,
        "final_result_url": result_url,
        "final_path": result_path,
        "final_output_duration": result_duration,
        "result_url": result_url,
        "local_path": result_path,
        "target_duration": target_duration,
        "output_duration": result_duration,
        "wan_duration_param": duration,
        "duration_status": "pass",
        "attempt_failure_reason": None,
        "seed_used": 0,
        "total_attempts": 1,
    }


async def step2_wan3_execute(
    prompt: str,
    video_path: str,
    product_image_paths: List[str],
    target_duration: float,
    output_dir: str,
    media_manifest: Optional[List[Dict[str, str]]] = None,
    model_image_path: Optional[str] = None,
    scene_image_path: Optional[str] = None,
    primary_image_id: int = 1,
    on_log: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[str, int, str], None]] = None,
    max_generation_attempts: int = 3,
    resolution: Optional[str] = None,
    aspect_ratio: Optional[str] = None,
    audio: Optional[bool] = None,
    voice_audio_paths: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Wan3.0 执行器：原视频 + 产品图 + 模特图 + 场景图 + MiMo prompt → 成品视频。

    严格遵循 wan3-prompt-builder skill v3.1.0：
      - 原视频（≤15s）作为 reference_video（单段，1-15s ✓）
      - 产品图 + 模特图 + 场景图作为 reference_image（总数 1-10 张，Step 0 已硬校验）
        Image 编号顺序与 Step1 MiMo 输出一致：产品图 → 模特图 → 场景图
      - ★本次 RunningHub Wan3.0 测试明确传入目标时长 duration（5-15 秒）
      - ★生成 + 时长验收 统一循环，所有 attempt 共享 max_generation_attempts 配额：
          · Wan 模型生成失败
          · 成片明显偏长（diff > 0.5s）
          · 成片偏短（diff < -0.05s）
        都消耗一次 attempt，seed+1 重试
      - HTTP 429/5xx/网络瞬时错误由网关 invoke 内部重试，不消耗 attempt 配额
      - 时长 PASS 容差 0.05s（30fps 下 1 frame ≈ 0.033s，0.05s ≈ 1.5 帧；不再写"≤1 帧"）
      - ★0.05 < diff ≤ TRIM_LIMIT 的偏长 trim 后必须 ffprobe 验证：
          |final_duration - target_duration| ≤ DURATION_TOLERANCE → trimmed
          否则 → attempt_failure_reason=trim_failed（attempt 内部状态），
                duration_status 保持 generation_failed，不交付，继续下一次 attempt

    Args:
        target_duration: Step 0 锁死的全局时间事实源（float 秒）
        max_generation_attempts: 总生成尝试上限（默认 3），含首次 + 重试

    Returns:
        {
            "raw_result_url": Optional[str],          # 最后一次实际生成成功的 Wan URL（诊断用）
            "raw_output_duration": Optional[float],   # 对应时长（诊断用，与 raw_result_url/raw_task_id 一组）
            "raw_task_id": Optional[str],             # ★与 raw_result_url 一组的 task ID
            "last_task_id": Optional[str],            # ★最后一次实际提交 Wan API 的 task ID（可能与 raw_task_id 不同）
            "final_result_url": Optional[str],        # 最终交付 URL（PASS/trimmed 时有值，失败为 None）
            "final_path": Optional[str],              # 最终交付本地路径
            "final_output_duration": Optional[float], # 最终交付时长
            "result_url": Optional[str],               # = final_result_url（兼容）
            "local_path": Optional[str],               # = final_path（兼容）
            "task_id": Optional[str],                  # 成功时 = raw_task_id，失败时 = last_task_id（兼容诊断）
            "reference_video_url": str,
            "image_urls": list,
            "target_duration": float,                 # Step 0 锁死的全局硬参数
            "output_duration": Optional[float],       # 实际成品时长（float，配额耗尽为 None）
            "wan_duration_param": int,                # Wan API duration 参数（5-15 秒）
            "duration_status": str,                   # pass / trimmed / generation_failed（trim_failed 仅作 attempt_failure_reason）
            "attempt_failure_reason": Optional[str],  # attempt 内部失败原因（trim_failed / regenerate / ffprobe_failed 等）
            "seed_used": int,                         # 最终交付版本使用的 seed
            "total_attempts": int,                    # 总消耗的 attempt 数
        }
    """
    def log(msg: str) -> None:
        if on_log:
            on_log(msg)
        print(f"  [Step2·Wan3.0] {msg}", flush=True)

    cfg = _cfg()
    # ★前端可覆盖 resolution/aspect_ratio/audio（如未传则用 _cfg() 默认）
    eff_resolution = resolution or cfg["resolution"]
    eff_aspect_ratio = aspect_ratio or cfg["aspect_ratio"]
    eff_audio = cfg["audio"] if audio is None else bool(audio)
    from backend.services import wan3_client

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    fitted = fit_prompt_to_wan3_limit(prompt)
    if fitted != prompt:
        log(f"⚠ prompt {len(prompt)} 字超过 Wan3 5000 上限，压缩到 {len(fitted)} 字后再提交")
        prompt = fitted

    # 1. 上传所有素材图：严格按 Step 0 media_manifest 顺序构造 reference_image
    #    图1 = reference_image[0]，图2 = reference_image[1]，不得重新拼接顺序
    manifest = media_manifest or _build_media_manifest(product_image_paths, model_image_path, scene_image_path)
    all_image_paths = [item["path"] for item in manifest]
    image_labels = [f'{item["image_id"]} {item["label"]}' for item in manifest]

    # ★primary_image_id 是 1-based 的 图N 编号（与 Prompt 中"图1"语义一致）
    #   内部访问数组时才转 0-based：primary_idx = primary_image_id - 1（如未来需要）
    log(f"上传 {len(all_image_paths)} 张素材图到 RunningHub CDN（主推图: 图{primary_image_id}, 顺序: {' / '.join(image_labels)}）")
    image_urls: List[str] = []
    if all_image_paths:
        upload_tasks = [
            asyncio.to_thread(wan3_client.upload_media, p)
            for p in all_image_paths
        ]
        upload_results = await asyncio.gather(*upload_tasks, return_exceptions=True)
        for i, r in enumerate(upload_results):
            if isinstance(r, Exception):
                log(f"⚠ {image_labels[i]} 上传失败: {r}")
                image_urls.append("")
            else:
                image_urls.append(r)
                log(f"✓ {image_labels[i]} → {r[:60]}...")

    # ★音色参考音频（可选）：Wan3.0 reference_audio（≤5 段，单段 1-15s，总时长 ≤15s）
    voice_audio_paths = [p for p in (voice_audio_paths or []) if p]
    audio_urls: List[str] = []
    if voice_audio_paths:
        log(f"上传 {len(voice_audio_paths)} 段音色参考音频到 RunningHub CDN")
        audio_upload_tasks = [
            asyncio.to_thread(wan3_client.upload_media, p)
            for p in voice_audio_paths
        ]
        audio_upload_results = await asyncio.gather(*audio_upload_tasks, return_exceptions=True)
        for i, r in enumerate(audio_upload_results):
            if isinstance(r, Exception):
                raise RuntimeError(f"音色参考音频 {voice_audio_paths[i]} 上传失败: {r}")
            audio_urls.append(r)
            log(f"✓ 音色参考音频{i + 1} → {r[:60]}...")

    # 2. 调用 wan3_client.run_one_segment_prime 完成完整链路（经 gateway.invoke，唯一一条 wan3 腿）
    #    （上传参考视频 → 提交任务 → 轮询 → 下载）
    log(f"提交 Wan3.0 任务（参考视频 + {len(image_urls)} 张素材图（产品/模特/场景）+ prompt）")
    wan_duration = wan3_api_duration(target_duration)
    log(f"target_duration={target_duration:.3f}s, duration={wan_duration}s, resolution={eff_resolution}, aspect={eff_aspect_ratio}, audio={eff_audio}")

    # ★Dump 完整请求参数到文件，便于排查"实际传了什么给 Wan3.0"
    _wan3_request_dump = {
        "prompt": prompt,
        "reference_video_path": video_path,
        "image_urls": image_urls,          # 实际传给 API 的 CDN URL 列表
        "image_labels": image_labels,      # 每张图的编号和标签
        "image_count": len(image_urls),
        "model_image_included": any("模特图" in lbl for lbl in image_labels),
        "duration": wan_duration,
        "resolution": eff_resolution,
        "aspect_ratio": eff_aspect_ratio,
        "audio": eff_audio,
        # 2026-10-14 更正：上游参数卡（GET /v1/models 现读）明写 `audio: boolean`、
        # `media_types` 含 `reference_audio` ⇒ 这两个都【真的送上去了】，不再是丢掉的参数。
        # 留这两格是为了看得见"送没送"与"验没验"——送了不等于生效，语义仍未实测。
        "audio_param_sent_to_lane": True,
        "reference_audio_sent": bool(audio_urls),
        "audio_note": ("audio 与 reference_audio 均已上送；但上游拿 reference_audio 做什么"
                       "（照它念 / 只取音色）未实测 ⇒ 出片必须本地 ASR 复验听到的就是新词，"
                       "复验不过不许在交付里写『口播已改』"),
        "audio_urls": audio_urls,
        "voice_audio_count": len(audio_urls),
        "target_duration": target_duration,
        "primary_image_id": primary_image_id,
    }
    _dump_path = Path(output_dir) / "_wan3_request.json"
    _dump_path.write_text(json.dumps(_wan3_request_dump, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"✓ Wan3.0 请求参数已 dump → {_dump_path}")
    # 🔴 提交腿自己打钩（2026-10-12 用户口径"每一次进行打钩，有模型名"）：
    #    模型名现读网关这条 lane（表单锁定 ↔ 合同 spend ↔ ROUTES 三方同源，由单一源闸钉）。
    #    打不上钩不影响这次提交 —— 它只让交付表少一格，绝不是"钱已经花了却查不到哪个模型跑的"。
    try:
        from backend import skill_contract as _skc2
        _r2 = _skc2.tick("super-clone", str(output_dir), "submit-package",
                         evidence=str(_dump_path), model=_lane_cell("video.generate", "wan3-prime"),
                         impl="gateway:invoke:video.generate/wan3-prime")
        if not _r2.get("ok"):
            log("⚠️ 回执 submit-package 没打上钩：%s" % _r2.get("error"))
    except Exception as _te:                                  # noqa: BLE001
        log("⚠️ 回执 submit-package 打钩异常（不影响这次提交，交付表会缺一格）：%s" % _te)

    def _progress(phase: str, elapsed: int, msg: str) -> None:
        if on_progress:
            on_progress(phase, elapsed, msg)
        log(f"[{phase}] {msg}")

    # ── 生成 + 时长验收 统一循环 ──
    # ★所有以下情况都消耗一次 generation attempt（共享 max_generation_attempts 配额）：
    #   - Wan 模型生成失败（WanGenerateError）
    #   - 成片明显偏长（diff > TRIM_LIMIT）
    #   - 成片偏短（diff < -DURATION_TOLERANCE，trim 无法修回）
    # ★HTTP 429/5xx/网络瞬时错误由 run_one_segment_prime 内部处理，不消耗配额
    #
    # ★配额耗尽后不再保留"最后一次输出"作为交付版本（不合格结果不能交付）：
    #   - final_result_url / final_path / final_output_duration / output_duration = None
    #   - duration_status = "generation_failed"
    base_seed = 42
    DURATION_TOLERANCE = 0.05   # PASS 容差：30fps 下 1 frame ≈ 0.0333s，0.05s ≈ 1.5 帧（工程合理）
    # 偏长则 ffmpeg 裁到 target。ceil 多出来的整数秒也必须能 trim，不能按 1s 硬顶掉。
    TRIM_LIMIT = 1.0 + max(0.0, float(wan_duration) - float(target_duration))
    # ★瞬时错误（HTTP 429/5xx/网络超时）原 seed 重试上限，避免无限循环
    MAX_TRANSIENT_RETRIES = 3

    total_attempts = 0
    transient_retry_count = 0  # 瞬时错误重试计数（不消耗 generation attempt 配额）
    seed_used: Optional[int] = None
    last_result: Dict[str, Any] = {}
    # ★raw 三元组（与最后一次实际成功生成的 Wan 视频一一对应）
    #   - 任何 attempt 只要成功生成视频，raw_* 就更新为该次的数据
    #   - 即使后续 attempt 失败，raw_* 仍保留最后一次成功生成的诊断信息
    raw_result_url: Optional[str] = None
    raw_output_duration: Optional[float] = None
    raw_task_id: Optional[str] = None
    # ★last_task_id：最后一次实际提交 Wan API 的 task_id（可能与 raw_task_id 不同）
    #   - 用于诊断"最后一次尝试是哪个 task 失败的"
    last_task_id: Optional[str] = None
    # ★最终交付字段：默认 None（配额耗尽且无合格结果时保持 None，不交付不合格结果）
    final_result_url: Optional[str] = None
    final_path: Optional[str] = None
    final_output_duration: Optional[float] = None
    duration_status = "generation_failed"  # 默认失败，只有 PASS/trim 成功才会改写
    attempt_failure_reason: Optional[str] = None  # attempt 内部失败原因（trim_failed / regenerate / ffprobe_failed 等）
    last_submit_error: Optional[str] = None       # 提交这一步【网关/上游自己说的原话】，交付层拿它写拒因

    while total_attempts < max_generation_attempts:
        current_seed = base_seed + total_attempts
        log(f"=== Wan3.0 生成 attempt {total_attempts + 1}/{max_generation_attempts}（wan3-prime 无 seed 概念：每次重试都是上游一次新计费 calls=1）===)")

        try:
            # 🔴 路由唯一源（2026-10-10 定）：wan3.0 = 网关 lane video.generate/wan3-prime（provider tianyue）。
            #    不再走 RunningHub（2026-10-09 禁令 + WAN3_RH_ENABLED=False），也不再靠"agent 直调网关"绕开入口。
            #    分辨率在这一层【原样递下去】：以前这里印一句"只出 1080P"然后把调用方给的档位丢掉、
            #    硬写 1080P ⇒ 客户点名 720P 时既没拒也没换档，只是悄悄出了 1080P（悄悄改参数）。
            #    现在交给 lane 自己的档位校验（gateway._WAN30_RES_MODEL）：名单外 0 分本地拒。
            log(f"=== 提交走网关 video.generate/wan3-prime（account=local · resolution={eff_resolution} · 按次计费 calls=1）===")
            result = await asyncio.to_thread(
                wan3_client.run_one_segment_prime,
                prompt=prompt,
                reference_video_path=video_path,
                image_urls=image_urls,
                # 本次裁剪后的目标时长（与旧 RH 腿同一口径）
                duration=wan_duration,
                output_dir=output_dir,
                segment_index=total_attempts + 1,
                resolution=eff_resolution,
                aspect_ratio=eff_aspect_ratio,
                audio=eff_audio,
                audio_urls=audio_urls,
                account="local",
                on_progress=_progress,
            )
        except Exception as e:
            # ★Step 3 错误分类（避免参数错误/鉴权失败浪费 attempt 配额）
            #   1. 瞬时错误（HTTP 429/5xx/网络超时）→ 原 seed 重试，不消耗 attempt
            #   2. 模型内部生成失败 → 消耗 1 次 attempt，seed+1 重试
            #   3. 永久错误（HTTP 400 参数错误 / 401/403 鉴权 / 内容审核拒绝 / 素材格式非法）
            #      → 立即终止，不换 seed 重试（参数写错换 seed 也会失败）
            err_type_name = type(e).__name__
            err_module = type(e).__module__
            is_wan_error = "Wan" in err_type_name or "wan3" in err_module.lower()
            last_submit_error = "%s：%s" % (err_type_name, str(e)[:700])

            # 判断是否为永久错误（不可重试）
            is_permanent = False
            permanent_reason = ""
            if is_wan_error:
                # WanAuthError（401/403）、WanParamError（400）、WanContentRejectError（内容审核）
                # 这些类在 wan3_client.py 中已定义，这里按类名匹配避免循环依赖
                if err_type_name in ("WanAuthError", "WanParamError", "WanContentRejectError",
                                     "WanApprovalRequired", "WanUpstreamUnavailable"):
                    is_permanent = True
                    permanent_reason = {
                        "WanAuthError": "鉴权/权限失败（401/403）",
                        "WanParamError": "API 参数非法（400）",
                        "WanContentRejectError": "内容/安全审核拒绝",
                        "WanApprovalRequired": "网关批准闸拒绝（0 分未扣、任务未发起）",
                        "WanUpstreamUnavailable": "上游没有可用渠道（渠道池空/上游抖动，不是参数问题）",
                    }[err_type_name]

            if is_permanent:
                # 永久错误 → 立即终止，不换 seed 重试
                duration_status = "generation_failed"
                log(
                    f"✗ 永久错误（{permanent_reason}）attempt {total_attempts + 1}/{max_generation_attempts}, "
                    f"seed={current_seed}: {e} → 立即终止，不重试"
                )
                # 更新 last_task_id（即使失败也要保留诊断信息）
                # 注：永久错误时通常 result 未返回，last_task_id 保持上一次的值
                break

            # 判断是否为瞬时错误（原 seed 重试，不消耗 attempt）
            is_transient = False
            if is_wan_error and err_type_name in ("WanTransientError", "WanTimeoutError"):
                is_transient = True
            elif "timeout" in str(e).lower() or "timed out" in str(e).lower():
                is_transient = True

            if is_transient:
                # 瞬时错误 → 不消耗 attempt，原 seed 重试（受外层循环限制避免无限重试）
                transient_retry_count += 1
                if transient_retry_count > MAX_TRANSIENT_RETRIES:
                    log(
                        f"✗ 瞬时错误重试超限（{transient_retry_count}/{MAX_TRANSIENT_RETRIES}）"
                        f"attempt {total_attempts + 1}/{max_generation_attempts}, seed={current_seed}: {e}"
                    )
                    duration_status = "generation_failed"
                    break
                log(
                    f"• 瞬时错误（{err_type_name}）attempt {total_attempts + 1}/{max_generation_attempts}, "
                    f"seed={current_seed}: {e} → 原 seed 重试（不消耗 attempt，第 {transient_retry_count}/{MAX_TRANSIENT_RETRIES} 次）"
                )
                continue

            # 模型内部生成失败 → 消耗 1 次 attempt，seed+1 重试
            total_attempts += 1
            log(f"✗ 生成失败 (attempt {total_attempts}/{max_generation_attempts}, seed={current_seed}): {e}")
            if total_attempts >= max_generation_attempts:
                # ★配额耗尽且无合格结果：不抛异常，返回 None 让上层决策
                log(f"  ⚠ 已达 max_generation_attempts={max_generation_attempts}，全部生成失败")
                duration_status = "generation_failed"
                break
            log(f"  → seed={base_seed + total_attempts} 继续")
            continue

        # 生成成功 → 消耗一次 attempt
        total_attempts += 1
        seed_used = result.get("seed_used", current_seed)
        last_result = result
        # ★raw 三元组同步更新（最后一次实际成功生成 Wan 视频的诊断信息）
        raw_result_url = result.get("result_url", "")
        current_task_id = result.get("task_id") or result.get("rh_task_id", "")
        raw_task_id = current_task_id
        last_task_id = current_task_id
        local_out = result.get("local_path", "")
        log(f"✓ Wan3.0 生成完成 (seed={seed_used}, attempt {total_attempts}/{max_generation_attempts}) → {local_out}")

        # 🔴 回执这一格拿到结果后【按现读重打一次】：提交前那一钩只能写路由配置上的主模型，
        #    而这条 lane 现在会在【同分辨率候选链】里换线（2026-10-21 现网 17:03：客户要 720P，
        #    ME-wan30-720p-1055 与其下一条候选都被渠道回"上游余额不足"⇒ 真出片的是 S-Wan30-prime-720p-1050-se，
        #    而表上印的是 ME-wan30-1080p-1055）。交付表写一条没跑过的模型 = 台账作废，所以以信封为准。
        try:
            from backend import skill_contract as _skc3
            _m_real = str(result.get("model") or "").strip() or "没记"
            _r3 = _skc3.tick("super-clone", str(output_dir), "submit-package",
                             evidence=str(Path(output_dir) / "wan3_job.json"),
                             model="%s@tianyue · 网关账本内" % _m_real,
                             impl="gateway:invoke:video.generate/wan3-prime")
            if not _r3.get("ok"):
                log("⚠️ 回执 submit-package 没改成现读模型名：%s" % _r3.get("error"))
            else:
                log(f"  ✓ 回执 submit-package 的模型名已按信封改口 → {_m_real}")
        except Exception as _te3:                            # noqa: BLE001
            log("⚠️ 回执 submit-package 重打异常（不影响这次交付）：%s" % _te3)

        # ── Step 3.5: 时长验收（每次 Wan 成功后立即 validate_generated_video）──
        # ★基准 = target_duration（Step 0 ffprobe 锁死的全流程唯一时间事实源）
        #   diff = output_duration - target_duration
        #
        #   分档处理：
        #     |diff| ≤ DURATION_TOLERANCE       → PASS → 交付
        #     0 < diff ≤ TRIM_LIMIT            → 偏长 → ffmpeg trim → 交付
        #     diff > TRIM_LIMIT                → 明显偏长 → 消耗下一次 attempt
        #     diff < -DURATION_TOLERANCE       → 偏短 → 消耗下一次 attempt
        output_duration: Optional[float] = None
        if local_out and Path(local_out).exists():
            try:
                output_duration = _ffprobe_duration(local_out)
                raw_output_duration = output_duration
            except Exception as e:
                log(f"  ⚠ ffprobe 成品时长失败：{e}")

        if output_duration is None or output_duration <= 0:
            # ffprobe 失败 → 不交付，继续下一次 attempt
            duration_status = "generation_failed"
            log("  ⚠ 无法探测成品时长，视为本次 attempt 失败")
            if total_attempts >= max_generation_attempts:
                log(f"  ⚠ 已达 max_generation_attempts={max_generation_attempts}，无合格成品")
                break
            log(f"  → attempt {total_attempts + 1}/{max_generation_attempts}, seed={base_seed + total_attempts} 重新生成")
            continue

        ref_dur = float(target_duration)
        diff = output_duration - ref_dur

        if abs(diff) <= DURATION_TOLERANCE:
            # PASS → 交付
            duration_status = "pass"
            final_result_url = raw_result_url
            final_path = local_out
            final_output_duration = output_duration
            log(f"  ✓ 成品时长 {output_duration:.3f}s ≈ target {ref_dur:.3f}s（diff={diff:+.3f}s ≤ {DURATION_TOLERANCE}s）→ PASS")
            break

        if 0 < diff <= TRIM_LIMIT:
            # 偏长 → ffmpeg 精准 trim 至 target_duration → 必须再 ffprobe 验证才宣告 trimmed
            # ★trim_failed 仅作 attempt 内部 failure_reason，不影响最终 duration_status 枚举
            #   最终状态只保留 pass / trimmed / generation_failed
            trimmed_path = str(Path(output_dir) / f"final_trimmed_attempt{total_attempts}.mp4")
            log(f"  • 成品偏长 {output_duration:.3f}s vs target {ref_dur:.3f}s（diff={diff:+.3f}s）→ ffmpeg 精准 trim")
            try:
                trim_video(local_out, 0.0, ref_dur, trimmed_path)
                trimmed_duration = _ffprobe_duration(trimmed_path)
                if trimmed_duration is None or abs(trimmed_duration - ref_dur) > DURATION_TOLERANCE:
                    # ★trim 后再次 ffprobe 验证失败 → 不交付，继续下一次 attempt
                    #   attempt_failure_reason = "trim_failed"（仅作日志/诊断，不进 duration_status 枚举）
                    attempt_failure_reason = "trim_failed"
                    actual_diff = (trimmed_duration - ref_dur) if trimmed_duration else None
                    log(
                        f"  ⚠ ffmpeg trim 后验证失败：final_duration={trimmed_duration}s, "
                        f"target={ref_dur:.3f}s, diff={actual_diff}（超出容差 {DURATION_TOLERANCE}s）"
                        f"→ attempt_failure_reason=trim_failed，不交付，继续 regenerate"
                    )
                    # ★duration_status 保持 "generation_failed"（不引入 trim_failed 终态）
                    if total_attempts >= max_generation_attempts:
                        log(f"  ⚠ 已达 max_generation_attempts={max_generation_attempts}，无合格成品（最后失败原因: {attempt_failure_reason}）")
                        break
                    log(f"  → attempt {total_attempts + 1}/{max_generation_attempts}, seed={base_seed + total_attempts} 重新生成")
                    continue
                # ★trim 验证通过 → 重新上传 trimmed 文件，final_result_url 指向新上传 URL
                #   - 不复用 raw_result_url（那是未裁剪的原始 Wan 输出 URL）
                #   - final_result_url / final_path / final_output_duration 必须三者一组指向同一文件
                final_path = trimmed_path
                final_output_duration = trimmed_duration
                final_result_url = wan3_client.upload_media(trimmed_path)
                duration_status = "trimmed"
                log(
                    f"  ✓ ffmpeg trim 完成 → {trimmed_path}（final_duration={trimmed_duration:.3f}s）"
                    f"已重新上传，final_result_url={final_result_url}"
                )
                break
            except Exception as e:
                # trim 失败 → 不交付，继续下一次 attempt
                #   attempt_failure_reason = "trim_failed"（仅作日志/诊断）
                attempt_failure_reason = "trim_failed"
                log(f"  ⚠ ffmpeg trim 失败：{e}（attempt_failure_reason=trim_failed，不交付不合格结果，继续 regenerate）")
                if total_attempts >= max_generation_attempts:
                    log(f"  ⚠ 已达 max_generation_attempts={max_generation_attempts}，无合格成品（最后失败原因: {attempt_failure_reason}）")
                    break
                log(f"  → attempt {total_attempts + 1}/{max_generation_attempts}, seed={base_seed + total_attempts} 重新生成")
                continue

        # regenerate（明显偏长 / 偏短）→ 不交付，继续下一次 attempt
        # ★duration_status 保持 "generation_failed"（regenerate 是中间动作，不作为最终状态）
        if diff > 0:
            reason = f"成品明显偏长（diff={diff:+.3f}s > {TRIM_LIMIT}s）"
        else:
            reason = f"成品偏短（diff={diff:+.3f}s < -{DURATION_TOLERANCE}s，trim 无法修回）"
        attempt_failure_reason = "regenerate"
        log(f"  ✗ 成品时长 {output_duration:.3f}s vs target {ref_dur:.3f}s：{reason} → regenerate（不交付不合格结果）")

        if total_attempts >= max_generation_attempts:
            log(f"  ⚠ 已达 max_generation_attempts={max_generation_attempts}，无合格成品（最后失败原因: {attempt_failure_reason}）")
            # ★final_result_url / final_path / final_output_duration 保持 None
            # ★duration_status 保持 "generation_failed"（regenerate 不是最终状态）
            break

        log(f"  → attempt {total_attempts + 1}/{max_generation_attempts}, seed={base_seed + total_attempts} 重新生成")
        continue

    return {
        # 原始 Wan 结果（诊断用，配额耗尽且失败时可能为 None）
        "raw_result_url": raw_result_url,
        "raw_output_duration": raw_output_duration,
        "raw_task_id": raw_task_id,                      # ★与 raw_result_url/raw_output_duration 一组的 task ID
        "last_task_id": last_task_id,                    # ★最后一次实际提交 Wan API 的 task ID（可能与 raw_task_id 不同）
        # ★最终交付版本：PASS / trimmed 时有值；配额耗尽且无合格结果时为 None
        "final_result_url": final_result_url,
        "final_path": final_path,
        "final_output_duration": final_output_duration,
        # 兼容字段：local_path/result_url 指向最终版本（可能为 None）
        "result_url": final_result_url,
        "local_path": final_path,
        # ★task_id 语义：成功交付时 = final 对应 task_id；失败时 = last_task_id（兼容诊断）
        "task_id": raw_task_id if duration_status in ("pass", "trimmed") else last_task_id,
        "reference_video_url": last_result.get("reference_video_url", ""),
        "image_urls": image_urls,
        # ★明确字段（替代原 "duration": int 的歧义）
        "target_duration": float(target_duration),       # Step 0 锁死的全局硬参数
        "output_duration": final_output_duration,        # 实际成品时长（float，配额耗尽时为 None）
        "wan_duration_param": wan_duration,              # Wan API duration 参数（5-15 秒）
        # ★最终状态枚举（仅三种）：pass / trimmed / generation_failed
        #   trim_failed / regenerate / ffprobe_failed 等仅作为 attempt_failure_reason 出现
        "duration_status": duration_status,
        "attempt_failure_reason": attempt_failure_reason,  # attempt 内部失败原因（仅诊断用）
        "submit_error": last_submit_error,  # 提交这一步的原话拒因（交付层用它写"为什么没有成片"）
        "seed_used": seed_used,
        "total_attempts": total_attempts,
    }


def delivery_problems(step2: dict) -> list:
    """单段路径的验收判据（纯函数）：「跑完了」必须等于【盘上有一支能播的片子】。

    2026-10-22 sc_9c246af70384：提交被批准闸拒了（0 分未扣、上游任务根本没建），执行器回
    final_path=None / duration_status=generation_failed，而这里原来什么都不查就
    on_phase("completed") ⇒ 状态写 SUCCESS、日志打「工作流完成 → None」。
    「该发生却没发生」必须能红，所以判据抽成函数、由交付层调用。
    """
    if not isinstance(step2, dict):
        return ["执行器没返回信封，无从判定交付"]
    problems: list = []
    status = str(step2.get("duration_status") or "")
    if status not in ("pass", "trimmed"):
        problems.append("最终状态=%s（不是 pass/trimmed）" % (status or "空"))
    final = str(step2.get("final_path") or "")
    if not final:
        problems.append("没有成片路径")
    elif not Path(final).is_file():
        problems.append("成片文件不在盘上：%s" % final)
    return problems


# ─────────────────────────────────────────────────────────────────
# 顶层编排：Step1 → Step2
# ─────────────────────────────────────────────────────────────────
# ── 素材分析：执行人 = 管线，不是 agent（2026-10-12 用户裁定"skill 要锁死交给谁干"）────────────
# 以前合同这一步只在【报错文案】里教 agent 自己去跑 CLI ⇒ 它真的自己干：PowerShell 吃引号 → 猜参数键名
# → 写 _analyze_run.py 手搓 → 走了不记账的近路 → 台账 0 条却汇报"齐全"（现网 session-1e7a5162 08:45–08:48）。
# 现在这段由管线自己经 gateway.invoke 跑（会预扣、会结算、会记台账），agent 在这条路上不碰任何模型。
MATERIAL_ANALYZE_QUESTION = (
    "这是本次要替换上去的产品图。请逐项说清：品类、颜色（尽量具体色名）、材质与工艺、版型或尺寸、"
    "可见细节（扣子/拉链/图案/边饰/配件）、画面里是否有文字或水印。"
    "只描述画面中真实存在的东西，不要推测品牌、价格、功效。")
# 300 是 analyze_media 的默认正文预算：实测会把 15s 视频的逐秒时间轴截断在前几秒（现网就是这么截的），
# 所以这条腿自己给到 900；要改给环境变量，不改代码。
MATERIAL_ANALYZE_TOKENS = int(os.environ.get("SUPER_CLONE_ANALYZE_TOKENS") or "900")
# 熔断：看图免批不免账，一条单挂 10 张产品图就可能失控 ⇒ 自动补跑累计超过这个分数就先停下报数，不许死循环烧分。
MATERIAL_ANALYZE_CREDIT_CAP = int(os.environ.get("SUPER_CLONE_ANALYZE_CAP") or "120")


# ── 编译产物缓存（2026-10-14 需求单：把"编译一次、多次复用"焊死）───────────────────
# 为什么要有这一份：`--stage submit` 的语义是"从头跑到提交"，run_pipeline 里原来没有任何复用开关
# ⇒ 同一条 14s 输入今晚被完整编译了 4 遍，其中两遍纯白烧（一遍提交被身份问题拒、一遍 MiMo 截断后整条重跑）。
# 指纹覆盖【所有会影响 storyboard / prompt 的输入】：命中 = 输入一字没变 = 重编译也是同样结果 ⇒ 零语义损失。
_COMPILE_FP_KEYS = ("trim_start", "trim_end", "target_duration", "resolution", "aspect_ratio",
                    "voice_mode", "voice_script", "extra_note", "customer_request", "product_name",
                    "media_manifest", "source_sha8")


def compile_fingerprint(facts: Dict[str, Any]) -> str:
    """纯本地 sha8 —— 0 模型、0 分、不发请求。
    🔴 少列一个会影响 prompt 的输入 = 会把旧提示词当成新的复用掉，所以加字段必须同时过闸 CP 组。"""
    parts = []
    for k in _COMPILE_FP_KEYS:
        v = (facts or {}).get(k)
        parts.append("%s=%s" % (k, json.dumps(v, ensure_ascii=False, sort_keys=True)
                                if isinstance(v, (list, dict)) else v))
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:8]


def compile_cache_state(seg_dir, facts: Dict[str, Any]) -> tuple:
    """(命中?, 说得出名字的原因)。三件齐才算命中 —— 只比指纹会被"盘上躺着旧 prompt"骗
    （2026-10-10 深夜：v3 那轮把已交付成片的 prompt.txt 原地盖掉，就是这个形状）。
    ① 指纹相同 ② 两份物证的 sha8 就是指纹里记的那两份 ③ 回执必需步全打钩。
    🔴 任何一条不满足一律判【未命中】⇒ 宁可多烧一次编译，也不能拿错的那份提示词去花 60 分。
    ⚠️ 只有【单段路径】会写 compile_fp；多段（按分镜切 chunk）那条不写 ⇒ 永远不命中、照旧全量重编译。"""
    d = Path(seg_dir)
    pm, pt, sb, rc = (d / "prompt_meta.json", d / "prompt.txt",
                      d / "storyboard.json", d / ".skill_receipt.json")
    from backend import skill_contract as _skc            # 模块层没有这个名字，见上方注释
    if not (pm.is_file() and pt.is_file() and sb.is_file()):
        return False, "缺编译产物（prompt.txt / storyboard.json / prompt_meta.json 有一样不在）"
    if not pt.read_text(encoding="utf-8", errors="replace").strip():
        return False, "prompt.txt 是空的"
    try:
        meta = json.loads(pm.read_text(encoding="utf-8"))
    except Exception:                                          # noqa: BLE001
        return False, "prompt_meta.json 读不出来/坏 JSON"
    fp = compile_fingerprint(facts)
    if str(meta.get("compile_fp") or "") != fp:
        return False, "指纹不符（输入改过，或这一份是旧版本产物、根本没写指纹）"
    if _skc.sha8(str(sb)) != str(meta.get("storyboard_sha8") or ""):
        return False, "storyboard.json 与指纹里记的那份不一致（被改过）"
    if _skc.sha8(str(pt)) != str(meta.get("prompt_sha8") or ""):
        return False, "prompt.txt 与指纹里记的那份不一致（被改过）"
    try:
        rec = json.loads(rc.read_text(encoding="utf-8"))
        steps = rec.get("steps") or []
        if not isinstance(steps, list):
            raise ValueError("steps 不是列表")
    except Exception:                                          # noqa: BLE001
        return False, "回执读不出来/形状不对（缺回执 = 那次没走完，不许复用）"
    miss = [str(s.get("id")) for s in steps if isinstance(s, dict)
            and s.get("required") and not s.get("ok")]
    if miss:
        return False, "回执必需步没齐：%s" % "、".join(miss)
    return True, "命中（compile_fp=%s）" % fp


def _contract_evidence(seg_dir, *, seg_board, seg_prompt, compiled_seg, seg_duration,
                       ref_video, seg_product_paths, source_fit=None,
                       extra_note: Optional[str] = None,
                       narration_binding: Optional[str] = None,
                       compile_facts: Optional[Dict[str, Any]] = None,
                       log: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """物证三件（prompt.txt / storyboard.json / prompt_meta.json）+ 回执 + 花钱前的合同核验。

    🔴 为什么抽出来：这段原来只写在【按分镜切多段】的循环里 ⇒ 一条 ≤15s 的单段复刻
       既不写 prompt.txt、也不写回执、更不核验，直奔提交。用户问"回执呢、提示词呢"，
       真相是那个运行形状物理上就没有这两样（2026-10-10 实测），不是"没展示"。
       两条路现在共用同一个写者，一处定义。

    🔴 回执是【打钩单】不是结尾总结（2026-10-12 用户定："回执的写法是每一次进行打钩，有模型名"）：
       开工先 open_receipt() 落一张全 ⬜ 的表，之后每落一件物证当场 tick() 一格，
       每一格都带【现读的模型名】（网关腿 _lane_cell / MiMo 腿 _mimo_cell / 编译器 _llm_stamp；
       不经模型的本地步写 skill_contract.NO_MODEL）。
       ⇒ 中途崩了看得见打到第几格；打不上钩的那一格要么补上、要么必需步缺失 ⇒ 花钱那一刀拒。
    """
    from backend import skill_contract as _skc
    seg_dir = Path(seg_dir)
    seg_dir.mkdir(parents=True, exist_ok=True)

    def _l(msg: str) -> None:
        if log:
            log(msg)

    def _tk(step_id: str, evidence, model: str, impl: str = "") -> None:
        """做完一步就打一个钩；打不上必须出声（原样把 tick 的原因打出来，不许吞）。"""
        r = _skc.tick("super-clone", str(seg_dir), step_id, evidence=evidence, model=model, impl=impl)
        if not r.get("ok"):
            _l("⚠️ 回执这一格【没打上钩】：%s" % r.get("error"))
        elif r.get("error"):
            _l("⚠️ 这一格打钩了但没写进盘：%s" % r.get("error"))

    # 🔴 模型名现读，三家一次读齐（不写死、不凭记忆）：看图腿 / Step1 分镜腿 / 编译器腿
    _cell_analyze = _lane_cell("video.analyze", _analyze_purpose())   # 2026-10-20：purpose 唯一源在 config（默认 magpie 带图腿；mimo_pro 那把 key 现在 429 死着）
    _cell_mimo = _mimo_cell()
    # 开工即落骨架（含输入指纹）。素材没看过而 raise 时，盘上留下的就是这张【全 ⬜ 的打钩单】——
    # 它不是"脏盘"，正是用户要的东西：一眼看出这次跑到第几步、卡在哪一格。
    _skc.set_ctx("super-clone", str(seg_dir))
    _skc.open_receipt("super-clone", str(seg_dir),
                      inputs=[str(ref_video)] + [str(x) for x in (seg_product_paths or [])])
    # 🔴 素材必须【真看过】（2026-10-10 用户裁定：提交前 agent 要分析所有素材做意图匹配）。
    #    判据不认 agent 自述、只认网关台账：每条 video.analyze 跑完由网关自己记账，
    #    这里按内容 sha8 对账 ⇒ 没看过就提交不出去。放在写任何【物证】之前 ⇒ 拒的时候盘上只剩
    #    那张全 ⬜ 的打钩单（open_receipt 落的骨架），一格假钩都没有。
    from backend import gateway as _gw
    _sid = _gw._session_of({})
    _mats = [str(x) for x in (seg_product_paths or []) if str(x or "").strip()]
    # 🔴 缺哪几张由【管线】补跑，不甩给 agent：只补还缺的、有熔断、跑完再对账（判据只有一份 =
    #    gateway.analysis_missing_paths，这里不解析错误字符串）。范围只含产品图 —— 原片的动作与时间轴
    #    事实由 Step1 MiMo 那条腿承担，再花一次看图分是重复消耗（用户 2026-10-12 定"最可靠"）。
    _spent, _auto = 0, []
    _lanes = _analyze_lanes()
    if not _lanes:
        raise RuntimeError("看图腿的链读不出来：config/.env 的 REPLICA_WAN3_ANALYZE_PURPOSE 串形不对，"
                           "或里面点名了网关 `video.analyze` 没有的 purpose ⇒ 0 分、一条都不发"
                           "（半条链比空链危险：会拿一条没人指定的腿跑，再把回执署成主腿的名字）")
    for _round in (1, 2):
        _miss = _gw.analysis_missing_paths(_mats, _gw.media_analysis_rows(), _sid)
        if not _miss or _round == 2 or _spent >= MATERIAL_ANALYZE_CREDIT_CAP:
            break
        for one in _miss:
            # 链：主腿挂了才往下走一条，但【挂在哪条、花了多少】必须留在物证里（看不见=查不着）
            row = {"name": os.path.basename(one), "ok": False, "charged": 0, "error": "",
                   "lane": "", "model": "", "tried": []}
            for ln in _lanes:
                for _t in range(int(ln["tries"])):
                    env = _gw.invoke("video.analyze", ln["purpose"], "local",
                                     {"path": one, "question": MATERIAL_ANALYZE_QUESTION,
                                      "max_tokens": MATERIAL_ANALYZE_TOKENS}) or {}
                    _cr = int(((env.get("credits") or {}).get("charged")) or 0)
                    _spent += _cr
                    row["charged"] += _cr
                    _txt = str(((env.get("result") or {}).get("text")) or "").strip()
                    _good = bool(env.get("ok")) and bool(_txt)
                    row["tried"].append({"purpose": ln["purpose"], "ok": _good, "charged": _cr,
                                         "error": str(env.get("error") or ("正文为空" if not _txt else ""))[:160]})
                    if _good:
                        row.update({"ok": True, "lane": ln["purpose"],
                                    "model": _analyze_lane_model(ln["purpose"])})
                        break
                    row["error"] = str(env.get("error") or "正文为空")[:160]
                    if _spent >= MATERIAL_ANALYZE_CREDIT_CAP:
                        break
                if row["ok"] or _spent >= MATERIAL_ANALYZE_CREDIT_CAP:
                    break
            _auto.append(row)
            if _spent >= MATERIAL_ANALYZE_CREDIT_CAP:
                break
    # 回执那一格的模型名 = 【真的答了这些素材的那条腿】；混着答或一条没答就保留主腿（不许猜）
    _used = sorted({str(a.get("lane") or "") for a in _auto if a.get("ok")})
    if len(_used) == 1:
        _cell_analyze = _lane_cell("video.analyze", _used[0])
    if _auto and log:
        log("素材分析由管线自动补跑 %d 条、实扣 %d 分（免批不免账，已计入这次消耗）"
            % (len(_auto), _spent))
    _missing = _gw.analysis_coverage_problems(_mats, _gw.media_analysis_rows(), _sid)
    _ma = seg_dir / "material_analysis.json"
    _ma.write_text(json.dumps({
        "session": _sid or "未知",
        "materials": [_gw.file_identity(p) for p in _mats],
        "missing": _missing,
        "ledger_rows": len(_gw.media_analysis_rows()),
        # 🔴 谁跑的、花了多少：交付表与报价都读这一份，不靠 agent 自述
        "executor": "pipeline:invoke:video.analyze",   # 与合同 手段= 同一个 token：交付表与合同对得上
        "auto_ran": _auto, "auto_spent": _spent,
        # 这次按的是哪条链（顺序=试的先后）+ 每条现在解出来的模型名 —— 没有这份，"为什么答的是这家"只能猜
        "analyze_chain": [{"purpose": ln["purpose"], "tries": ln["tries"],
                           "model": _analyze_lane_model(ln["purpose"])} for ln in _lanes],

        # 这一格的模型名从【这条腿现在的路由】现读；台账行只记 sha8 不记型号（要改网关那份才行）
        "model_cell": _cell_analyze,
        "credit_cap": MATERIAL_ANALYZE_CREDIT_CAP,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    if _missing:
        # 🔴 这份物证必须在 raise 【之前】落盘：拒因原话让人"去看 material_analysis.json 的 auto_ran"，
        #    而它写在 raise 之后 ⇒ 真被拒时那文件物理上不存在，等于让人去查一个没写出来的东西
        #    （2026-10-22 sc_9c246af70384 就是这么让我抓不到补跑失败原因的）。
        raise RuntimeError(
            "素材没看过 ⇒ 不提交（本轮管线自动补跑 %d 次、实扣 %d 分）。缺 %d 项：%s"
            % (len(_auto), _spent, len(_missing), "；".join(_missing)[:400])
            + ("｜熔断在 %d 分（SUPER_CLONE_ANALYZE_CAP 可调），不是闸坏了" % MATERIAL_ANALYZE_CREDIT_CAP
               if _spent >= MATERIAL_ANALYZE_CREDIT_CAP else
               "｜多半是这条腿本身跑不通（看 %s 的 auto_ran 里的 error）" % _ma.name)
            + "｜注意：会话=%s，台账里【没有 hold_id 的行不算证据】" % (_sid or "未知"))
    _tk("material-analysis", str(_ma), _cell_analyze, impl="pipeline:invoke:video.analyze")

    cs = compiled_seg or {}
    mdl, prov, led = str(cs.get("model") or ""), str(cs.get("provider") or ""), str(cs.get("ledger") or "off")
    cell = ("%s@%s · %s" % (mdl or "?", prov or "?",
                           "网关账本内" if led == "on" else "账本外·直连厂商")) if mdl else ""

    _pt = seg_dir / "prompt.txt"
    _pt.write_text(seg_prompt, encoding="utf-8")
    _tk("compile-prompt", str(_pt), cell)
    _tk("negative-lock", str(_pt), cell, impl="compiler-negative-lock")

    sb = seg_dir / "storyboard.json"
    sb.write_text(json.dumps(seg_board, ensure_ascii=False, indent=2), encoding="utf-8")
    _tk("storyboard", str(sb), _cell_mimo)

    # 改口播直出那一格：这次真带了客户改稿才有物证（合同 required=0）。
    # 🔴 【没有物证也要走一遍 tick】—— tick 打不上时会把那格抹成 ⬜（连旧物证/指纹一起清）。
    #    跳过不调 = 留着上一次那条真片子的 ✅ 冒充这次做过（2026-10-14 闸 N12 当场抓到这个形状）。
    # 模型名写 NO_MODEL —— 这一步是纯字符串对齐，真念词的是 wan3 那条腿。
    _tk("narration-direct", str(narration_binding or ""), _skc.NO_MODEL,
        impl="pipeline:apply_customer_narration")

    _pm = seg_dir / "prompt_meta.json"
    _pm.write_text(json.dumps({
        "source": str(cs.get("source") or ""),
        # 🔴 模型身份单独记：source 只证明"走了编译器这个函数"，答不出"哪个模型跑的"
        "model": mdl, "provider": prov, "ledger": led,
        "storyboard_sha8": _skc.sha8(str(sb)),
        "prompt_sha8": _skc.sha8(str(_pt)),
        # 复用缓存的那把钥匙：只有【单段路径】带 facts 进来才写（多段不写 ⇒ 永不命中，宁可重编）
        "compile_fp": (compile_fingerprint(compile_facts) if compile_facts else ""),
        "prompt_chars": len(seg_prompt),
        "target_duration": float(seg_duration)}, ensure_ascii=False, indent=1), encoding="utf-8")
    _tk("compile-bound", str(_pm), cell)

    # 🔴 storyboard-fact-check（2026-10-11 补的合同步）：MiMo 的分镜表是【上游解析结果】，不是事实本身
    #    （现网：5.1s 单镜头被解析成 2 镜，还标了"硬切 + 换装"）。SKILL.md 早就要求"标签不符就按秒写死
    #    时间轴压过它"，但那一步既没有物证、也没登记授权手段 ⇒ agent 每改一次都要自己论证一遍合法性，
    #    论证不出来就去绕开入口手搓 driver（2026-10-11 现网 21:23 就是这个形状）。
    #    现在把两样都登记上：改了什么（extra_note.txt 全文 + 指纹）+ 改之前是什么（镜数/时长/分镜指纹），
    #    由管线自己落盘，不是 agent 手写。
    #    ⚠️ 强度边界照实说：这份物证证明"对账发生过、改的是哪一版"，【不证明改得对】——
    #       对不对由人在审帧那一刀看（frames/ 与成片），不由这个文件担保。
    _note = str(extra_note or "").strip()
    npath = seg_dir / "extra_note.txt"
    if _note:
        npath.write_text(_note + "\n", encoding="utf-8")
    sc = seg_dir / "storyboard_check.json"
    sc.write_text(json.dumps({
        "source": "step1_mimo+runner-extra-note",
        "shot_count": len(seg_board or []),
        "duration": float(seg_duration),
        "note_present": bool(_note),
        "note_chars": len(_note),
        "note_sha8": (_skc.sha8(str(npath)) if _note else ""),
        "storyboard_sha8": _skc.sha8(str(sb)),
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    _tk("storyboard-fact-check", str(sc), _skc.NO_MODEL, impl="runner:--extra-note")

    fit = Path(source_fit) if source_fit else None
    if fit is None or not fit.exists():
        for cand in ("trimmed_source.mp4", "source.mp4"):
            if (seg_dir / cand).exists():
                fit = seg_dir / cand
                break
    if fit is not None and fit.exists():
        _tk("source-fit", str(fit), _skc.NO_MODEL)
    _pa = seg_dir / "product_absent.jpg"
    if _pa.exists():
        _tk("reference-clean", str(_pa), _skc.NO_MODEL)

    # 🔴 花钱那一刀之前：无回执 / 合同版本不符 / 输入指纹变了 ⇒ 抛，不提交、0 分。
    #    只断提交，不断查询与捞回 —— 已提交的任务照旧能用 taskId 取回（重投=双花）。
    #    ⚠️ 这里原来还多传了一个 `impl=` —— `verify_or_raise` 没有这个参数 ⇒ 【每次多段复刻都在这行
    #    TypeError 崩掉】，而合同那条"手段申报"真正生效的地方是上面 write_receipt（手段进回执行，
    #    check_receipt 再从行里读名单）。2026-10-10 实测才发现，不是理论问题。
    v = _skc.verify_or_raise("super-clone", str(seg_dir),
                             inputs=[str(ref_video)] + [str(x) for x in seg_product_paths])
    for _ln in str(v.get("table") or "").splitlines():
        _l("合同核验 %s" % _ln)
    return v


async def run_pipeline(
    task_id: str,
    video_path: str,
    product_image_paths: List[str],
    customer_request: str,
    output_dir: str,
    on_log: Optional[Callable[[str, None]]] = None,
    on_phase: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    trim_start: Optional[float] = None,
    trim_end: Optional[float] = None,
    model_image_path: Optional[str] = None,
    scene_image_path: Optional[str] = None,
    product_name: str = "",
    product_facts: Optional[str] = None,
    resolution: Optional[str] = None,
    aspect_ratio: Optional[str] = None,
    audio: Optional[bool] = None,
    engine: str = "wan3",
    split_by_shots: bool = False,
    extra_compile_note: Optional[str] = None,
    extra_model_image_paths: Optional[List[str]] = None,
    as_duration_chunk: bool = False,
    background_override: Optional[str] = None,
    voice_audio_paths: Optional[List[str]] = None,
    voice_script: Optional[str] = None,
    stop_after: Optional[str] = None,
    reuse_compile: bool = False,
    reuse_compile_strict: bool = True,
    resume: bool = False,
) -> Dict[str, Any]:
    """超级复刻 Wan3.0/H3 - 4 步工作流顶层编排。

    Step 1:   MiMo（视频解析器）→ 原视频解析为 storyboard JSON
              （只解析，不拼装 prompt）
    Step 1.5: GPT 编译器 → storyboard JSON + 素材图 → Wan3.0 prompt
              （强制语法：保留口播/语气/转场，确定性编译）
    Step 2:   GPT Lint（Linter/Sanitizer）→ prompt + media_manifest
              → 规则校验 + LLM 二次审查，修复客观错误
    Step 3:   Wan3.0（执行器）→ 原视频+产品图+模特图+场景图+GPT 编译后提示词
              → 输出替换了产品/模特/场景/口播的成品视频

    Args:
        task_id: 任务 ID
        video_path: 爆款视频本地路径（任意时长；>15s 按 15s 切段后拼接）
        product_image_paths: 产品图本地路径列表（与模特图/场景图合计 1-10 张）
        customer_request: 客户需求说明文本（可选，可包含人物/场景描述）
        output_dir: 输出根目录
        on_log: 实时日志回调
        on_phase: 阶段变更回调 fn(phase, extra)
        trim_start: 裁剪起点（秒，0-based），与 trim_end 同时提供时启用精准复刻（5-15s 区间）
        trim_end: 裁剪终点（秒，0-based）
        model_image_path: 模特参考图本地路径（可选，用于人物替换）
        scene_image_path: 场景参考图本地路径（可选，用于背景替换）
        stop_after: None=照常提交；"compile"=只走到"物证+回执+合同核验"就返回（不提交、不建 hold、0 分）
        resume: True=断点继续模式：已落盘且校验通过的步骤产物自动复用（裁剪视频/分镜/编译/Lint/
                已完成的分段），缺失或校验不过才重跑

    Returns:
        {
            "prompt": str,                # MiMo/GPT Lint 最终 Wan3.0 prompt
            "storyboard": list,           # MiMo 分镜
            "result_url": Optional[str],  # Wan3.0 结果视频 URL（= final_result_url），配额耗尽为 None
            "local_path": Optional[str],  # 本地成品视频路径（= final_path），配额耗尽为 None
            # ★明确语义字段（全流程统一使用 target_duration，不再有歧义的 duration:int）
            "target_duration": float,      # Step 0 锁死的全局硬参数（最终输出应等于此值）
            "output_duration": Optional[float],  # 实际成品时长（float，配额耗尽为 None）
            "wan_duration_param": int,      # Wan API duration 参数（5-15 秒）
            "duration_status": str,        # pass / trimmed / generation_failed（trim_failed 仅作 attempt_failure_reason）
            "attempt_failure_reason": Optional[str],  # attempt 内部失败原因（诊断用）
            "duration": Optional[float],   # 兼容旧调用方，= output_duration（配额耗尽为 None）
            "task_id": Optional[str],      # ★Wan3.0 任务 ID（首次提交前彻底失败则可能为 None）
            "trimmed_path": str,           # 裁剪后视频路径（未裁剪则等于 video_path）
            "trim_range": dict,            # {"start": float, "end": float} 或 None
        }
    """
    # 🔴 第一道：引擎锁。放在建目录/读素材/调任何模型之前 ⇒ 拒的时候 0 消耗、盘上不留半成品。
    _lock = engine_lock_error(engine)
    if _lock:
        raise RuntimeError(_lock)

    # 🔴 第二道：档位锁（2026-10-11）。stop_after="compile" = 只走到"物证 + 回执 + 合同核验"就返回，
    #    不提交、不建 hold、0 分。为什么必须有这一档：SKILL.md 要求"分镜/动作标签不符 ⇒ 补 extra-note
    #    重编译（0 分，不烧 Wan）"，而入口以前【只有整条跑到提交】一种走法 ⇒ 想遵守这句散文就只能绕开
    #    入口手搓 driver 直调 run_pipeline（2026-10-11 现网 21:23 就是这么走的，绕道又撞上"不许发明手段"）。
    #    拼错的档位一律拒 —— 不许把 "compil" 静默当成"照常提交"（那等于拿拼写错误花钱）。
    if stop_after not in (None, "", "compile"):
        raise RuntimeError('stop_after 只认 None 或 "compile"（收到 %r）' % (stop_after,))

    def log(msg: str) -> None:
        if on_log:
            on_log(msg)
        print(f"[super-clone {task_id}] {msg}", flush=True)

    Path(output_dir).mkdir(parents=True, exist_ok=True)

    # 产品名为必填字段（GPT 编译器依赖此名做产品识别与替换）
    if not product_name or not product_name.strip():
        raise RuntimeError("product_name 为必填字段，请提供产品名")

    # ── Step 0: 素材预处理（裁剪 + target_duration 全局硬参数 + media_manifest 编号归属固定）──
    trimmed_path = video_path
    trim_range: Optional[Dict[str, float]] = None
    requested_duration: Optional[float] = None  # 客户要求的理论时长（仅记录，不作事实源）
    orig_dur = _ffprobe_duration(video_path) or 0.0
    trim_err = validate_trim(trim_start, trim_end, orig_dur, enforce_max=False)
    if trim_err:
        raise RuntimeError(f"裁剪校验失败: {trim_err}")
    if trim_start is not None and trim_end is not None:
        trimmed_path = str(Path(output_dir) / "trimmed_source.mp4")
        requested_duration = round(float(trim_end) - float(trim_start), 3)
        if resume and Path(trimmed_path).is_file():
            log(f"=== Step 0: ↻ 断点继续，复用已裁剪视频 → {trimmed_path} ===")
        else:
            log(f"=== Step 0: 精准裁剪原视频 [{trim_start:.2f}s → {trim_end:.2f}s] (requested={requested_duration:.3f}s) ===")
            trim_video(video_path, float(trim_start), float(trim_end), trimmed_path)
            log(f"✓ 裁剪完成 → {trimmed_path}")
        trim_range = {"start": float(trim_start), "end": float(trim_end)}
    else:
        # 无裁剪时也明确指定实际送入 MiMo/Wan 的参考视频路径。
        trimmed_path = video_path
        requested_duration = None
        log(f"=== Step 0: 不裁剪，trimmed_path = video_path → {trimmed_path} ===")

    if trim_start is None or trim_end is None:
        min_dur = _cfg()["min_trim_duration"]
        if orig_dur > 0 and orig_dur < min_dur:
            raise RuntimeError(
                f"原视频时长 {orig_dur:.2f}s < {min_dur:.0f}s（最短 {min_dur:.0f}s），"
                f"请提供更长的视频或指定裁剪区间"
            )

    # ★target_duration：统一由实际送给 MiMo/Wan 的视频文件 ffprobe 得到（全流程唯一时间事实源）
    #   - 无论是否裁剪，都从 trimmed_path 实际文件取时长
    #   - 原因：视频按帧存在，ffmpeg 实际文件可能与 requested_duration 差 1 帧（如 8.7333s vs 8.73s）
    #   - 这样所有模型看到的参考视频与 target_duration 绝对一致
    target_duration = _ffprobe_duration(trimmed_path) or 0.0
    if target_duration <= 0:
        # ffprobe 兜底失败时用裁剪区间差值
        if trim_range is not None:
            target_duration = trim_range["end"] - trim_range["start"]
        else:
            raise RuntimeError("Step 0 无法确定 target_duration：ffprobe 失败且无裁剪区间")
    target_duration = round(target_duration, 2)

    if trim_range is not None and requested_duration is not None:
        drift = abs(target_duration - requested_duration)
        if drift > 0.05:
            raise RuntimeError(
                f"Step 0 trim_result_invalid：ffmpeg 裁剪后 target_duration={target_duration:.3f}s "
                f"与 requested_duration={requested_duration:.3f}s 误差 {drift:.3f}s > 0.05s 容差，"
                f"裁剪结果不可信，请检查 ffmpeg 版本或原视频编码"
            )
    min_input_dur = _cfg()["min_trim_duration"]
    max_input_dur = _cfg()["max_input_duration"]
    if target_duration < min_input_dur:
        raise RuntimeError(
            f"Step 0 时长下限校验失败：target_duration={target_duration:.3f}s < {min_input_dur:.0f}s "
            f"（最短 {min_input_dur:.0f}s）→ 禁止进入 Step 1"
        )
    windows = [] if as_duration_chunk else plan_15s_windows(target_duration)
    if as_duration_chunk and target_duration > max_input_dur + 0.05:
        cap_path = str(Path(output_dir) / "source_cap15.mp4")
        log(
            f"⚠ 本段 ffprobe={target_duration:.3f}s，略超 15s，再裁到 15.00s → {cap_path}"
        )
        trim_video(trimmed_path, 0.0, float(max_input_dur), cap_path)
        trimmed_path = cap_path
        target_duration = round(_ffprobe_duration(cap_path) or float(max_input_dur), 2)
    write_submit_plan(
        output_dir, task_id=task_id, windows=windows, target_duration=target_duration,
        trim_range=trim_range, source_video=trimmed_path, as_duration_chunk=as_duration_chunk,
        log=log,
    )
    if voice_script and len(windows) > 1:
        raise RuntimeError(
            "改口播直出这条只接【一段一条稿】：这次会自动切成 %d 段，而客户给的那份稿是对整条的，"
            "按段各灌一遍 = 每段都念整稿（重复与漏词），比不改更坏。"
            "两条合法走法：① 按难单逐段跑（每段一次 run.py + 那一段自己的稿，0 分先 --stage compile 看对齐）；"
            "② 整条一次出片、改口播走后处理换轨（narration-swap，每句一次克隆、不在网关账本上）。"
            "⇒ 现在拒：0 分未扣、任务未发起" % len(windows))
    if len(windows) > 1:
        log(
            f"=== Step 0: 原片 {target_duration:.2f}s，按 15s 切成 {len(windows)} 段自动拼接 ==="
        )
        for i, win in enumerate(windows, start=1):
            _ref = ""
            if (win.get("ref_start"), win.get("ref_end")) != (win["start"], win["end"]):
                _ref = "｜参考 %.2f-%.2fs（尾巴太短看不出动作者，参考窗口借满 5s；重复的那截不进成片）" % (
                    float(win["ref_start"]), float(win["ref_end"]))
            log(
                f"  段 {i}/{len(windows)}: {win['start']:.2f}-{win['end']:.2f}s "
                f"({win['end'] - win['start']:.2f}s){_ref}"
            )
        split_root = Path(output_dir) / ("step2_h3" if engine == "h3" else "step2_wan3")
        split_root.mkdir(parents=True, exist_ok=True)
        import copy as _copy

        async def _run_one_chunk(i: int, win: Dict[str, float]) -> Dict[str, Any]:
            seg_dir = split_root / f"chunk_{i:02d}"
            seg_dir.mkdir(parents=True, exist_ok=True)
            ref_video = str(seg_dir / "source.mp4")
            # 🔴 断点继续：整段已生成过（fitted.mp4 + chunk_result.json 齐全）⇒ 直接复用，不重投 Wan
            _cres_path = seg_dir / "chunk_result.json"
            if resume and _cres_path.is_file():
                try:
                    _saved = json.loads(_cres_path.read_text(encoding="utf-8"))
                    _saved_fitted = str(_saved.get("fitted") or "")
                    if _saved_fitted and Path(_saved_fitted).is_file():
                        log(f"↻ 第 {i}/{len(windows)} 段已完成，复用 {_saved_fitted}（不重新提交）")
                        return _saved
                except Exception as _re:                     # noqa: BLE001
                    log(f"⚠ 第 {i}/{len(windows)} 段复用失败（{_re}），改走重新生成")
            # 🔴 两个窗口各管各的：喂模型的是 ref_*（尾段会把参考窗口借满 5 秒，见 plan_15s_windows），
            #    这一段在成片里负责的只有 start/end ⇒ 拼接时按 offset 丢掉重复的那一截。
            ref_a = float(win.get("ref_start", win["start"]))
            ref_b = float(win.get("ref_end", win["end"]))
            out_dur = round(float(win["end"]) - float(win["start"]), 3)
            if resume and Path(ref_video).is_file():
                log(f"↻ 第 {i}/{len(windows)} 段复用已裁剪参考视频 → {ref_video}")
            else:
                trim_video(trimmed_path, ref_a, ref_b, ref_video)
            want = round(ref_b - ref_a, 3)
            log(f"▶ 第 {i}/{len(windows)} 段 [出 {win['start']:.2f}-{win['end']:.2f}s"
                f"｜参考 {ref_a:.2f}-{ref_b:.2f}s（生成 {want:.2f}s，取其中 {out_dur:.2f}s）] 开始")
            if on_phase:
                on_phase(
                    "step3_h3" if engine == "h3" else "step3_wan3",
                    {
                        "target_duration": target_duration,
                        "segment": i,
                        "segments": len(windows),
                    },
                )

            def _chunk_log(msg: str, _i: int = i, _n: int = len(windows), _w: Dict[str, float] = win) -> None:
                log(f"[段 {_i}/{_n} {_w['start']:.1f}-{_w['end']:.1f}s] {msg}")

            chunk_result = await run_pipeline(
                task_id=f"{task_id}_c{i:02d}",
                video_path=ref_video,
                product_image_paths=product_image_paths,
                customer_request=customer_request,
                output_dir=str(seg_dir),
                on_log=_chunk_log,
                on_phase=None,
                trim_start=None,
                trim_end=None,
                model_image_path=model_image_path,
                scene_image_path=scene_image_path,
                product_name=product_name,
                product_facts=product_facts,
                resolution=resolution,
                aspect_ratio=aspect_ratio,
                audio=audio,
                engine=engine,
                extra_compile_note=extra_compile_note,
                extra_model_image_paths=extra_model_image_paths,
                as_duration_chunk=True,
                stop_after=stop_after,
                background_override=background_override,
                voice_audio_paths=voice_audio_paths,
                resume=resume,
            )
            if chunk_result.get("compiled_only"):
                # 🔴 档位必须一路传到底（2026-10-12 现网 super-clone-20261012-1824）：按长度切段的递归
                #    以前不带 stop_after ⇒ `--stage compile` 的每一块照样走到提交那一刀，
                #    只是当时没有批准条子才被网关拦回 approval_required —— 有条子就是真花 60 分/段。
                #    这里早退，是因为下面那半句把"没有成片"判成"生成失败"。
                return {"index": i, "win": win, "compiled_only": True,
                        "fitted": None, "chunk_result": chunk_result}
            part = chunk_result.get("final_path") or chunk_result.get("local_path")
            if not part or not Path(str(part)).exists():
                raise RuntimeError(f"第 {i}/{len(windows)} 段生成失败，无法拼接")
            fitted = str(seg_dir / "fitted.mp4")
            fit_clip_duration(str(part), out_dur, fitted,
                              offset=round(float(win["start"]) - ref_a, 3))
            _outcome = {
                "index": i,
                "win": win,
                "fitted": fitted,
                "chunk_result": chunk_result,
            }
            # 🔴 断点继续的落盘凭据：这一段落盘后才算「完成」，下次 resume 直接复用
            try:
                _cres_path.write_text(
                    json.dumps(_outcome, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except Exception as _we:                         # noqa: BLE001
                log(f"⚠ 第 {i}/{len(windows)} 段完成凭据落盘失败（不影响本次拼接）：{_we}")
            return _outcome

        # 多段并发（受 REPLICA_WAN3_CONCURRENCY 限流），不再逐段串行等待
        concurrency = max(1, int(_cfg().get("concurrency") or 1))
        log(f"多段并发生成：{len(windows)} 段，concurrency={concurrency}")
        sem = asyncio.Semaphore(concurrency)

        async def _guarded(i: int, win: Dict[str, float]) -> Dict[str, Any]:
            async with sem:
                return await _run_one_chunk(i, win)

        chunk_outcomes = await asyncio.gather(
            *[_guarded(i, win) for i, win in enumerate(windows, start=1)],
            return_exceptions=True,
        )
        errors = [
            f"段 {windows[idx]['start']:.2f}-{windows[idx]['end']:.2f}s: {exc}"
            for idx, exc in enumerate(chunk_outcomes)
            if isinstance(exc, BaseException)
        ]
        if errors:
            raise RuntimeError("多段生成失败，无法拼接：" + "；".join(errors))

        ordered = sorted(chunk_outcomes, key=lambda item: item["index"])
        if stop_after == "compile":
            # 只编译档位的收口：没有 fitted.mp4、没有拼接、一条都没提交 ⇒ 0 分
            _segs: List[Dict[str, Any]] = []
            for item in ordered:
                for s in (item["chunk_result"].get("segments") or []):
                    _d = dict(s)
                    _d["seg"] = item["index"]
                    _segs.append(_d)
            log("档位=只编译 ⇒ %d 段全部只走到【物证 + 回执 + 合同核验】，一条都没提交、0 分" % len(ordered))
            # 多段路径下 media_manifest 局部变量尚未赋值（它在后面的 Step 0 块才建），
            # 这里就地用入参重建同一份清单，避免 UnboundLocalError
            _mm = _build_media_manifest(
                product_image_paths, model_image_path, scene_image_path, extra_model_image_paths
            )
            return {
                "compiled_only": True, "stop_after": "compile", "success": True,
                "duration_status": "not_submitted", "segments": _segs,
                "prompt": None, "storyboard": None,
                "media_manifest": _mm,
                "task_id": None, "result_url": None, "local_path": None,
                "final_path": None, "output_duration": None, "duration": None,
            }
        part_paths: List[str] = []
        prompt_parts: List[str] = []
        merged_board: List[Dict[str, Any]] = []
        last_chunk: Dict[str, Any] = {}
        next_shot = 1
        for item in ordered:
            i = item["index"]
            win = item["win"]
            chunk_result = item["chunk_result"]
            part_paths.append(item["fitted"])
            last_chunk = chunk_result
            chunk_prompt = (chunk_result.get("prompt") or "").strip()
            if chunk_prompt:
                prompt_parts.append(
                    f"=== 段 {i} [{win['start']:.2f}-{win['end']:.2f}] ===\n{chunk_prompt}"
                )
            for shot in chunk_result.get("storyboard") or []:
                cloned = _copy.deepcopy(shot)
                start, end = _shot_time_range(cloned)
                interval = dict(cloned.get("时间区间") or {})
                interval["开始时间"] = round(start + float(win["start"]), 2)
                interval["结束时间"] = round(end + float(win["start"]), 2)
                cloned["时间区间"] = interval
                cloned["shot"] = next_shot
                next_shot += 1
                merged_board.append(cloned)
        concat_path = str(split_root / "final_concat.mp4")
        log(f"拼接 {len(part_paths)} 段 → {concat_path}")
        concat_videos(part_paths, concat_path)
        concat_dur = _ffprobe_duration(concat_path)
        if on_phase:
            on_phase("completed", {"final_path": concat_path})
        log(f"=== 工作流完成（{len(windows)} 段拼接）→ {concat_path} ===")
        prompt = "\n\n".join(prompt_parts)
        return {
            "prompt": prompt,
            "storyboard": merged_board,
            "media_manifest": last_chunk.get("media_manifest"),
            "raw_result_url": last_chunk.get("raw_result_url"),
            "raw_output_duration": concat_dur,
            "final_result_url": last_chunk.get("final_result_url"),
            "final_path": concat_path,
            "final_output_duration": concat_dur,
            "result_url": last_chunk.get("final_result_url"),
            "local_path": concat_path,
            "target_duration": target_duration,
            "output_duration": concat_dur,
            "wan_duration_param": last_chunk.get("wan_duration_param"),
            "duration_status": last_chunk.get("duration_status") or "pass",
            "attempt_failure_reason": last_chunk.get("attempt_failure_reason"),
            "duration": concat_dur,
            "task_id": last_chunk.get("task_id"),
            "trimmed_path": trimmed_path,
            "trim_range": trim_range,
            "video_duration": orig_dur,
            "segment_paths": part_paths,
            "segment_count": len(windows),
            "segment_windows": windows,
        }
    if target_duration > max_input_dur + 0.05:
        raise RuntimeError(
            f"Step 0 API 上限校验失败：target_duration={target_duration:.3f}s > {max_input_dur:.0f}s "
            f"（Wan3.0 API 要求 reference_video ≤ {max_input_dur:.0f}s）"
            f"→ 禁止进入 Step 1"
        )
    log(f"=== Step 0: target_duration 全局硬参数 = {target_duration:.2f}s（ffprobe 实际文件时长，全流程锁死） ===")

    # Step 0 固定 media_manifest 编号归属（后端确定，贯穿后续所有步骤，GPT/MiMo 不得改）
    # 产品图是必填业务输入；同一 SKU 由调用方保证，不由本层猜测。
    product_image_paths = [p for p in (product_image_paths or []) if p]
    if not product_image_paths:
        raise RuntimeError("product_image_paths 至少需要 1 张同一目标商品/SKU 的产品图")
    # 可选路径的空字符串按未提供处理，避免计数与实际上传清单不一致。
    model_image_path = model_image_path or None
    scene_image_path = scene_image_path or None
    extra_model_image_paths = [p for p in (extra_model_image_paths or []) if p]
    media_manifest = _build_media_manifest(
        product_image_paths, model_image_path, scene_image_path, extra_model_image_paths
    )

    # ★primary_image_id 是 Step 0 metadata（产品图必填，固定为 图1）
    #   - 不由 GPT 决定
    #   - Step 1.5 / Step 2 / Step 3 仅作为输入接收，不再输出
    #   - 1-based 图N 编号（与 Prompt 中"图1"语义一致，避免 off-by-one）
    primary_image_id = 1
    # ★独立硬校验 product_images >= 1：
    #   - 不能由 total_images 校验替代（否则产品图=0、模特图=1、场景图=1 也会通过）
    #   - primary_image_id = 1 必须指向产品图，否则后续生成会引用错误素材
    #   - assert media_manifest[0].type == "product" 保证 图1 语义对齐
    if not product_image_paths:
        raise RuntimeError("product_image_paths 至少需要 1 张产品图（primary_image_id=1 必须指向产品图）")
    if not media_manifest or media_manifest[0].get("type") != "product":
        raise RuntimeError(
            f"primary_image_id=1 必须指向产品图，但 media_manifest[0]={media_manifest[0] if media_manifest else '空'}"
            f"不是产品图类型 → 编号归属错误，无法进入 Step 1"
        )
    extra_model_count = len(extra_model_image_paths or [])
    total_reference_images = (
        len(product_image_paths or [])
        + int(model_image_path is not None)
        + extra_model_count
        + int(scene_image_path is not None)
    )
    if total_reference_images != len(media_manifest):
        raise RuntimeError("media_manifest 与参考图片数量不一致")
    if not 1 <= total_reference_images <= 10:
        raise RuntimeError(
            "Wan3.0 reference_image 数量非法："
            f"{total_reference_images} 张（产品图 {len(product_image_paths or [])} 张"
            f" + 模特图 {1 if model_image_path else 0} 张"
            f" + 场景图 {1 if scene_image_path else 0} 张），必须为 1-10 张"
        )
    log(f"=== Step 0: media_manifest 编号归属固定（共 {total_reference_images} 张）→ {media_manifest} ===")

    # ★音色参考音频（可选）：Wan3.0 reference_audio 约束校验（≤5 段，单段 1-15s，总时长 ≤15s）
    voice_audio_paths = [p for p in (voice_audio_paths or []) if p]
    if voice_audio_paths:
        if engine == "h3":
            raise RuntimeError("H3 执行器暂不支持音色参考音频，请用 engine='wan3' 或去掉 voice_audio_paths")
        if len(voice_audio_paths) > 5:
            raise RuntimeError(f"音色参考音频最多 5 段，当前 {len(voice_audio_paths)} 段")
        total_voice_dur = 0.0
        for ap in voice_audio_paths:
            if not Path(ap).exists():
                raise RuntimeError(f"音色参考音频不存在: {ap}")
            ad = _ffprobe_duration(ap) or 0.0
            if not 1.0 <= ad <= 15.0:
                raise RuntimeError(f"音色参考音频单段时长须 1-15s: {ap} 为 {ad:.2f}s")
            total_voice_dur += ad
        if total_voice_dur > 15.0:
            raise RuntimeError(f"音色参考音频总时长 {total_voice_dur:.2f}s 超过 15s 上限")
        log(f"=== Step 0: 音色参考音频 {len(voice_audio_paths)} 段（总时长 {total_voice_dur:.2f}s）校验通过 ===")
        voice_note = (
            "【voice_audio 音色参考 · 必须执行】\n"
            "客户提供了音色参考音频（作为 reference_audio 随本次生成提交）。\n"
            "1. 成片所有口播/旁白的音色必须迁移到参考音频的说话人音色（性别、音高、声线质感以参考音频为准）；\n"
            "2. 严禁沿用或模仿参考视频原说话人的音色；\n"
            "3. 语速/语调/重音/停顿/情绪/呼吸感仍按 storyboard 原值执行，仅音色替换；\n"
            "4. 口播文案不变（商品迁移规则照旧），只换声音不换词。"
        )
        if model_image_path:
            voice_note += (
                "\n5. ★换人换声联动：本次同时给了模特图（换脸）和音色参考音频（换声），"
                "说话人身份已整体更换。口播音色必须严格按参考音频执行，"
                "严禁继承参考视频原说话人任何音色特征。"
            )
        extra_compile_note = (
            f"{extra_compile_note}\n\n{voice_note}" if extra_compile_note else voice_note
        )

    # ── Step 1: MiMo 纯视频解析 → storyboard JSON（不输入新素材，不拼装 prompt）──
    if on_phase:
        on_phase("step1_mimo", {})
    # 🔴 编译产物缓存（2026-10-14 需求单 1+2）：先算一次指纹（0 模型 0 分），命中就整段跳过
    #    MiMo + FunASR + Grok，直接读盘上的 storyboard/prompt 进提交。
    #    为什么必须在这儿：`--stage submit` 原来一律从头跑一遍，同一条输入今晚被编译了 4 遍，
    #    其中两遍纯白烧（提交被身份问题拒一次、MiMo 截断后整条重跑一次）。
    _cc_dir = Path(output_dir) / "step2_wan3"
    # 🔴 `_skc` 在这个模块里【没有模块层导入】（都是各函数内延迟 import）。这一段是我 10-14 深夜加的
    #    指纹缓存，当时漏了这句 import ⇒ `NameError: name '_skc' is not defined` 直接打死每一段，
    #    10-15 09:05 那次【批准即点火】起的提交腿就是这么死的（submit.log 尾部三行同名错），
    #    后果是"人点了批准、系统没投，agent 醒来自己重跑" —— 同一个文件为这件事崩过三次（os、json、这次）。
    from backend import skill_contract as _skc
    _cc_facts = {
        "trim_start": float(trim_start or 0.0), "trim_end": float(trim_end or 0.0),
        "target_duration": float(target_duration), "resolution": resolution,
        "aspect_ratio": aspect_ratio, "voice_mode": None,
        "voice_script": str(voice_script or ""), "extra_note": str(extra_compile_note or ""),
        "customer_request": str(customer_request or ""), "product_name": str(product_name or ""),
        "media_manifest": [{"image_id": m.get("image_id"), "path": m.get("path"),
                            "sha8": _skc.sha8(str(m.get("path")))} for m in media_manifest],
        "source_sha8": _skc.sha8(str(trimmed_path)),
    }
    _cc_hit, _cc_why = compile_cache_state(_cc_dir, _cc_facts)
    if reuse_compile and not _cc_hit:
        if reuse_compile_strict:
            raise RuntimeError(
                "--reuse-compile 但编译缓存不命中（%s）⇒ 去掉这个开关全量重编译。"
                "这一档不许「半复用」：拿一份对不上号的旧 prompt 去花 60 分，比多烧一次编译坏得多。"
                % _cc_why)
        # 软档（服务端【批准即点火】用的那条）：不命中不是错误，是"这次没有可复用的产物"。
        # 🔴 为什么不照硬档拒：条子批的是【提交】，缓存恰好失效（批完之后又改过 extra_note / 物证被重写）
        #    时拒掉 = 一张已生效的批准花不出去、也没人重新报价 = 卡死。日志必须出声，别让人以为复用了。
        log("⚠ 编译缓存不命中（%s）⇒ 不复用，照常从 Step1 全量重编译后再投（软复用档，服务端点火用的就是这条）"
            % _cc_why)
    if _cc_hit:
        log("=== Step 1/4: 【复用编译产物】%s ⇒ 跳过 MiMo 视频解析与 FunASR 写回（0 token、0 分）===" % _cc_why)
        storyboard = json.loads((_cc_dir / "storyboard.json").read_text(encoding="utf-8"))
    else:
        # 🔴 断点继续：MiMo 已落盘的分镜先复验 Checkpoint A，通过就整段复用（0 token），
        #    不通过（截断/时间轴不合规）才重新解析 —— 复用只认校验过的产物。
        _sb_path = Path(output_dir) / "step1_mimo" / "storyboard.json"
        storyboard = None
        if resume and _sb_path.is_file():
            try:
                _cand = json.loads(_sb_path.read_text(encoding="utf-8"))
                storyboard = normalize_storyboard_time(_cand, target_duration)
                log("=== Step 1/4: 【断点继续】复用已解析分镜（Checkpoint A 复验通过，0 token）===")
            except Exception as _sb_err:                     # noqa: BLE001
                log(f"⚠ 盘上分镜未通过 Checkpoint A（{_sb_err}），重新调用 MiMo 解析")
                storyboard = None
        if storyboard is None:
            log("=== Step 1/4: 分镜腿（纯视频解析）→ storyboard JSON ===")
            log("  ★只看原视频，不输入产品图/模特图/场景图/customer_request/product_name（避免污染解析）")
            # ★MiMo 解析有随机性，有时会漏掉最后一段（end < target_duration）；
            #   Checkpoint A 校验失败时自动重试，最多 3 次
            max_mimo_retries = 3
            storyboard = None
            for attempt in range(1, max_mimo_retries + 1):
                step1 = await step1_mimo_director(
                    video_path=trimmed_path,
                    output_dir=output_dir,
                    target_duration=target_duration,
                    on_log=log,
                )
                storyboard = step1["storyboard"]
                try:
                    storyboard = normalize_storyboard_time(storyboard, target_duration)
                    break  # 校验通过
                except RuntimeError as exc:
                    if attempt < max_mimo_retries:
                        log(f"⚠ 第 {attempt}/{max_mimo_retries} 次解析校验失败：{exc}，重试中…")
                    else:
                        raise RuntimeError(
                            f"Step 1 MiMo 解析 {max_mimo_retries} 次均未通过 Checkpoint A：{exc}"
                        ) from exc
            log("✓ Checkpoint A 通过：已生成 canonical storyboard，时间只在此处规范化一次")
    # media_manifest 已在 Step 0 固定，此处不再从 Step 1 重新取（防止被覆盖）
    image_count = len(media_manifest)
    # ★Step 1 不再输出 mimo_detected_duration：ffprobe 得到的就是 target_duration
    #   全流程唯一时间事实源 = target_duration（Step 0 锁死）
    log(f"✓ Step 1 完成：解析出 {len(storyboard)} 个分镜，target_duration={target_duration:.2f}s（Step 0 锁死）")

    # 🔴 改口播直出（2026-10-14 接的那根线）：客户改稿必须在【编译之前】进 storyboard 的口播位，
    #    这样 Step 1.5 编译器把它当台词写进 prompt，末尾 force_verbatim_speech 再逐字钉回去 ⇒
    #    wan3 一次出片就念新词。判据只证"新词进了台词位"，念得准不准、嘴形对不对仍归审帧 + lip-sync。
    #    命中复用时【不再灌第二遍】：盘上那份 storyboard.json 已经是灌过之后的（指纹里含 voice_script，
    #    所以命中就意味着是同一条稿），再灌一次等于拿新词去比新词、把物证算成满分。
    if _cc_hit:
        log("  · 改口播：复用命中，跳过 apply_customer_narration（盘上那份已灌过，指纹含 voice_script）")
    else:
        apply_customer_narration(storyboard, voice_script, output_dir, on_log=log)

    if split_by_shots:
        log("⚠ 超级复刻不允许组段生成，已忽略 split_by_shots=True")
        split_by_shots = False
    if float(target_duration) > 15.05:
        raise RuntimeError(
            f"单段参考视频 {target_duration:.2f}s 超过 Wan3 15s 上限"
        )
    segments: List[Dict[str, Any]] = []
    skip_global_compile = False
    prompt = ""
    check: Dict[str, Any] = {"prompt": "", "fixes": [], "target_duration": target_duration}

    # ── Step 1.5: 小段模式跳过整片编译，改为每段单独编译（避免一次塞太多图 524）──
    if skip_global_compile:
        log(
            f"=== Step 1.5/4: 小段替换，跳过整片编译，稍后逐段编译 "
            f"（{len(segments)} 段）==="
        )
        if on_phase:
            on_phase("step1_5_gpt_compile", {"shots": len(storyboard), "segments": len(segments)})
    else:
        if on_phase:
            on_phase("step1_5_gpt_compile", {"shots": len(storyboard)})
        log("=== Step 1.5/4: 编译器 → storyboard JSON → Wan3.0 prompt ===")
        compile_kwargs = dict(
            storyboard=storyboard,
            target_duration=target_duration,
            media_manifest=media_manifest,
            customer_request=customer_request,
            product_image_paths=product_image_paths,
            model_image_path=model_image_path,
            scene_image_path=scene_image_path,
            product_name=product_name,
            product_facts=product_facts,
            extra_compile_note=extra_compile_note,
            extra_model_image_paths=extra_model_image_paths,
            background_override=background_override,
            on_log=log,
        )
        if _cc_hit:
            prompt = (_cc_dir / "prompt.txt").read_text(encoding="utf-8")
            _meta = json.loads((_cc_dir / "prompt_meta.json").read_text(encoding="utf-8"))
            compiled = {"prompt": prompt,
                        "source": str(_meta.get("source") or "step1_5_gpt_compiler"),
                        "model": str(_meta.get("model") or "没记"),
                        "provider": str(_meta.get("provider") or "没记"),
                        "ledger": str(_meta.get("ledger") or "off")}
            log("=== Step 1.5/4: 【复用编译产物】跳过逐镜编译与合并（0 token）；"
                "下面时间轴校验与不可见镜引用校验照跑，不因复用而免 ===")
        elif len(storyboard) > 1:
            log(f"整段传：逐镜编译再合并（{len(storyboard)} 镜，一次送 Wan3）")
            shot_prompts: List[str] = []
            shot_stamps: List[Dict[str, Any]] = []
            for shot in storyboard:
                compiled_one = await step1_5_compile_prompt(
                    **{**compile_kwargs, "storyboard": [shot]}
                )
                shot_prompts.append(compiled_one["prompt"])
                shot_stamps.append(compiled_one)
            prompt = merge_compiled_shot_prompts(shot_prompts)
            # 🔴 合并这条路的信封原来被整个丢掉、只重新盖一个 source ⇒ 逐镜用的哪个模型查不回来。
            #    现在把各镜的 stamp 归并（同名就一个值，不同名就都列出来，不做"看起来一致"的美化）。
            def _merge_key(k: str) -> str:
                vals = sorted({str(s.get(k) or "").strip() for s in shot_stamps if s.get(k)})
                return "+".join(vals) if vals else "没记"
            compiled = {"prompt": prompt, "source": "step1_5_gpt_compiler",
                        "model": _merge_key("model"), "provider": _merge_key("provider"),
                        "ledger": _merge_key("ledger")}
        else:
            compiled = await step1_5_compile_prompt(**compile_kwargs)
            prompt = compiled["prompt"]
        prompt = force_verbatim_speech(prompt, storyboard)
        compiled["prompt"] = prompt
        try:
            validate_prompt_time_against_storyboard(prompt, storyboard)
        except RuntimeError as exc:
            raise RuntimeError(f"Step 1.5 Prompt 校验失败，应仅重跑 Step 1.5：{exc}") from exc
        bad_shots = invisible_shots_citing_product(prompt, storyboard, media_manifest)
        if bad_shots:
            note = (
                f"上次输出违规：Shot {bad_shots} 的 product_visible=false，"
                f"不得引用产品图N。请整段重编译，不要在这些 Shot 行出现产品 图N。"
            )
            log(f"⚠ Step 1.5 不可见镜引用了产品图：{bad_shots}，重跑 Compiler 一次")
            compiled = await step1_5_compile_prompt(**compile_kwargs, extra_compile_note=note)
            prompt = compiled["prompt"]
            prompt = force_verbatim_speech(prompt, storyboard)
            compiled["prompt"] = prompt
            try:
                validate_prompt_time_against_storyboard(prompt, storyboard)
            except RuntimeError as exc:
                raise RuntimeError(f"Step 1.5 Prompt 校验失败，应仅重跑 Step 1.5：{exc}") from exc
            bad_shots = invisible_shots_citing_product(prompt, storyboard, media_manifest)
            if bad_shots:
                raise RuntimeError(
                    f"Step 1.5 Compiler 两次均在不可见 Shot {bad_shots} 引用产品图，停止交付"
                )
        log(f"✓ Step 1.5 完成：Grok 编译出 prompt {len(prompt)} 字（来源: {compiled['source']}），时间字段与 storyboard 一致")

        if on_phase:
            on_phase("step2_gpt_lint", {"target_duration": target_duration})
        log("=== Step 2/4: 审查（Lint）→ 规则校验 + LLM 二次审查 ===")
        # 🔴 断点继续：Lint 产物按【输入 prompt 指纹】复用；prompt 变过（重编译过）就不复用
        _lint_path = _cc_dir / "lint_result.json"
        _lint_key = _skc.sha8(prompt)
        check = None
        if resume and _lint_path.is_file():
            try:
                _lint_saved = json.loads(_lint_path.read_text(encoding="utf-8"))
                if (_lint_saved.get("input_prompt_sha8") == _lint_key
                        and isinstance(_lint_saved.get("check"), dict)):
                    check = _lint_saved["check"]
                    log("=== Step 2/4: 【断点继续】复用上次 Lint 结果（输入 prompt 指纹一致，0 token）===")
            except Exception as _lint_err:                    # noqa: BLE001
                log(f"⚠ 盘上 Lint 结果不可用（{_lint_err}），重新执行 Lint")
                check = None
        # ★Plan C 失败重试：背景一致性校验失败 → 重跑 Compiler 一次（强化 note）→ 重跑 Lint
        if check is None:
            try:
                check = await validate_and_fix_prompt(
                    prompt=prompt,
                    target_duration=target_duration,
                    media_manifest=media_manifest,
                    product_name=product_name,
                    storyboard=storyboard,
                    background_override=background_override,
                    on_log=log,
                )
            except RuntimeError as lint_exc:
                if not (background_override and "background_override 校验失败" in str(lint_exc)):
                    raise
                # 🔴 只重发【判红的那几镜】，不再整段重编译：2026-10-22 sc_9c246af70384 实测 9 镜整段一发
                #   要 173-226s（经 Cloudflare 那家直接 524），而输入一字不变 ⇒ 同一个红必然再来。
                _bg = (background_override or "").strip()
                _frags = _bg_fragments(_bg)
                leak = original_bg_residue(_bg, storyboard, prompt, _frags)
                landed_ok, landed_detail = background_landed(_bg, prompt)
                bad_shots = shot_lines_needing(prompt, leak) if leak else []
                if not bad_shots:
                    raise RuntimeError(
                        "背景一致性校验没过，且【定位不到该重发哪几镜】⇒ 不猜、直接停。\n"
                        "拒因：%s\n残留元素=%s 新背景落实=%s" % (lint_exc, leak, landed_detail)) from lint_exc
                log(f"⚠ 背景校验失败：残留={leak} ⇒ 只重发这几镜 {bad_shots}（整段不重发，省一发 9 镜）")
                for _n in bad_shots:
                    shot = next((s for s in storyboard if int(s.get("shot") or 0) == _n), None)
                    if shot is None:
                        continue
                    one = await step1_5_compile_prompt(
                        **{**compile_kwargs, "storyboard": [shot],
                           "extra_compile_note": "本镜严禁保留这些原背景元素：%s；新场景按 %s 描述。"
                                                 % ("、".join(leak), (_frags[0] if _frags else _bg))})
                    new_line = next((l for l in (one.get("prompt") or "").splitlines()
                                     if l.strip().startswith("Shot 1")), "")
                    old_line = next((l for l in prompt.splitlines() if l.strip().startswith("Shot %d " % _n)), "")
                    m_old = re.match(r"^(Shot\s+\d+\s+\[[^\]]+\])", old_line.strip())
                    body = re.sub(r"^Shot\s+\d+(\s*\[[^\]]*\])?\s*[:：]?\s*", "", new_line.strip(), count=1)
                    if m_old and body:
                        # 时间括号一律沿用原行（canonical 时间轴由 Checkpoint A 定，编译器无权改）
                        prompt = replace_shot_line(prompt, _n, "%s: %s" % (m_old.group(1), body))
                prompt = force_verbatim_speech(prompt, storyboard)
                compiled["prompt"] = prompt
                check = await validate_and_fix_prompt(
                    prompt=prompt,
                    target_duration=target_duration,
                    media_manifest=media_manifest,
                    product_name=product_name,
                    storyboard=storyboard,
                    background_override=background_override,
                    on_log=log,
                )
            # 🔴 Lint 通过后落盘，供下次断点继续直接复用（键 = 送入 Lint 的 prompt 指纹）
            try:
                _lint_path.parent.mkdir(parents=True, exist_ok=True)   # step2_wan3 目录这时可能还没建
                _lint_path.write_text(json.dumps(
                    {"input_prompt_sha8": _lint_key, "check": check},
                    ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception as _lw:                          # noqa: BLE001
                log(f"⚠ Lint 结果落盘失败（不影响本次运行）：{_lw}")
        prompt = check["prompt"]
        target_duration = check["target_duration"]
        if check["fixes"]:
            log(f"✓ Step 2 完成：共修复 {len(check['fixes'])} 项:")
            for f in check["fixes"]:
                log(f"   - {f}")
        else:
            log("✓ Step 2 完成：自检通过，无需修复")

    async def _run_executor(
        *,
        exec_prompt: str,
        exec_video: str,
        exec_duration: float,
        exec_product_paths: List[str],
        exec_model_path: Optional[str],
        exec_scene_path: Optional[str],
        exec_manifest: List[Dict[str, str]],
        exec_dir: str,
    ) -> Dict[str, Any]:
        if engine == "h3":
            return await step2_h3_execute(
                prompt=exec_prompt,
                video_path=exec_video,
                product_image_paths=exec_product_paths,
                target_duration=exec_duration,
                output_dir=exec_dir,
                on_log=log,
                on_progress=lambda ph, el, msg: log(f"[{ph}] {msg}"),
                aspect_ratio=aspect_ratio or "9:16",
                media_manifest=exec_manifest,
                model_image_path=exec_model_path,
                scene_image_path=exec_scene_path,
            )
        return await step2_wan3_execute(
            prompt=exec_prompt,
            video_path=exec_video,
            product_image_paths=exec_product_paths,
            target_duration=exec_duration,
            output_dir=exec_dir,
            media_manifest=exec_manifest,
            model_image_path=exec_model_path,
            scene_image_path=exec_scene_path,
            primary_image_id=primary_image_id if exec_product_paths else 1,
            on_log=log,
            on_progress=lambda ph, el, msg: log(f"[{ph}] {msg}"),
            resolution=resolution,
            aspect_ratio=aspect_ratio,
            audio=audio,
            voice_audio_paths=voice_audio_paths,
        )

    # ── Step 3: Wan3.0 / H3 执行 ──
    # 禁止组段：整段参考视频 + 全部分镜 Prompt 一次送执行器。
    if on_phase:
        on_phase("step3_h3" if engine == "h3" else "step3_wan3", {"target_duration": target_duration})
    split_root = Path(output_dir) / ("step2_h3" if engine == "h3" else "step2_wan3")
    if len(segments) > 1:
        log(
            f"=== Step 3/4: 按分镜切 {len(segments)} 段生成 "
            + " / ".join(
                f"[{g['start']:.2f}-{g['end']:.2f}] Shot {sorted(g['shot_nums'])}"
                for g in segments
            )
            + " ==="
        )
        split_root.mkdir(parents=True, exist_ok=True)
        part_paths: List[str] = []
        compiled_only: List[Dict[str, Any]] = []      # 只编译档位收这一段（不进执行器 ⇒ 0 消耗）
        last_result: Dict[str, Any] = {}
        for index, group in enumerate(segments, start=1):
            seg_dir = split_root / f"seg_{index:02d}"
            seg_dir.mkdir(parents=True, exist_ok=True)
            ref_video = str(seg_dir / "ref.mp4")
            trim_video(trimmed_path, float(group["start"]), float(group["end"]), ref_video)
            seg_duration = float(group["end"]) - float(group["start"])
            seg_board = _rebase_shots_for_segment(
                group["shots"], float(group["start"]), float(group["end"])
            )
            log(f"段 {index}/{len(segments)} 编译 Grok prompt（{len(seg_board)} 镜，{seg_duration:.2f}s）")
            compiled_seg = await step1_5_compile_prompt(
                storyboard=seg_board,
                target_duration=seg_duration,
                media_manifest=media_manifest,
                customer_request=customer_request,
                product_image_paths=product_image_paths,
                model_image_path=model_image_path,
                scene_image_path=scene_image_path,
                product_name=product_name,
                product_facts=product_facts,
                extra_compile_note=extra_compile_note,
                extra_model_image_paths=extra_model_image_paths,
                background_override=background_override,
                on_log=log,
            )
            seg_prompt = compiled_seg["prompt"]
            seg_prompt = force_verbatim_speech(seg_prompt, seg_board)
            prompt = (prompt + "\n" + seg_prompt).strip() if prompt else seg_prompt
            include_product = any(
                shot_original_product_visible(shot) for shot in group["shots"]
            )
            if include_product:
                seg_manifest = media_manifest
                seg_product_paths = product_image_paths
            else:
                still = extract_still_frame(
                    ref_video, 0.05, str(seg_dir / "product_absent.jpg")
                )
                seg_manifest = manifest_for_segment(
                    media_manifest,
                    include_product=False,
                    product_placeholder_path=still,
                )
                seg_product_paths = [
                    item["path"] for item in seg_manifest if item.get("type") == "product"
                ]
            # ── 🔴 物证 + 回执 + 花钱前核验：单段与多段共用同一个写者（见 _contract_evidence）──
            _contract_evidence(
                seg_dir, seg_board=seg_board, seg_prompt=seg_prompt, compiled_seg=compiled_seg,
                seg_duration=seg_duration, ref_video=ref_video,
                seg_product_paths=seg_product_paths, extra_note=extra_compile_note,
                narration_binding=str(Path(output_dir) / "voice_script_binding.json"),
                log=log,
            )
            log(
                f"段 {index}/{len(segments)} "
                f"{group['start']:.2f}-{group['end']:.2f}s "
                f"Shot {sorted(group['shot_nums'])} "
                f"产品={'绑定' if include_product else '占位'}"
            )
            if stop_after == "compile":
                # 只编译档位：物证与回执已经写完（上面那一刀），这里【不进执行器】⇒ 0 消耗。
                compiled_only.append({"seg": index, "dir": str(seg_dir),
                                      "prompt_path": str(Path(seg_dir) / "prompt.txt"),
                                      "window": [float(group["start"]), float(group["end"])]})
                continue
            last_result = await _run_executor(
                exec_prompt=seg_prompt,
                exec_video=ref_video,
                exec_duration=float(group["end"]) - float(group["start"]),
                exec_product_paths=seg_product_paths,
                exec_model_path=model_image_path,
                exec_scene_path=scene_image_path,
                exec_manifest=seg_manifest,
                exec_dir=str(seg_dir),
            )
            part = last_result.get("final_path") or last_result.get("local_path")
            if not part or not Path(str(part)).exists():
                raise RuntimeError(
                    f"第 {index}/{len(segments)} 段生成失败，无法拼接"
                )
            part_paths.append(str(part))
        if stop_after == "compile":
            log("档位=只编译 ⇒ 不提交、不建 hold、0 分（%d 段提示词已落盘并过合同核验）"
                % len(compiled_only))
            return {
                "compiled_only": True, "stop_after": "compile",
                "prompt": prompt, "storyboard": storyboard, "segments": compiled_only,
                "target_duration": target_duration, "trimmed_path": trimmed_path,
                "task_id": None, "result_url": None, "local_path": None,
                "final_path": None, "output_duration": None, "duration": None,
                "duration_status": "not_submitted",
            }
        concat_path = str(split_root / "final_concat.mp4")
        log(f"拼接 {len(part_paths)} 段 → {concat_path}")
        concat_videos(part_paths, concat_path)
        concat_dur = _ffprobe_duration(concat_path)
        step2 = {
            **last_result,
            "raw_result_url": last_result.get("raw_result_url"),
            "raw_output_duration": concat_dur,
            "final_result_url": last_result.get("final_result_url"),
            "final_path": concat_path,
            "final_output_duration": concat_dur,
            "result_url": last_result.get("final_result_url"),
            "local_path": concat_path,
            "output_duration": concat_dur,
            "target_duration": target_duration,
            "duration_status": last_result.get("duration_status") or "pass",
            "split_at": segments[0]["end"] if segments else None,
            "segment_paths": part_paths,
        }
    else:
        # 🔴 单段路径（一条 ≤15s 的段、或没按分镜切）以前【什么都不写就直奔提交】：prompt.txt、
        #    storyboard.json、prompt_meta.json、.skill_receipt.json 四样在那个运行形状里物理上不存在，
        #    合同核验也就从没跑过 ⇒ "回执呢？提示词呢？"当时没有答案可给（2026-10-10 实测）。
        #    现在与多段共用同一个写者：交出去的那条 prompt 先落盘，再核验，再执行。
        _whole_dir = str(Path(output_dir) / ("step2_h3" if engine == "h3" else "step2_wan3"))
        # 🔴 断点继续：Step 3 已交付过（结果文件 + 成片都在，且 prompt 指纹一致）⇒ 直接复用，不重投
        _step2_path = Path(_whole_dir) / "step2_result.json"
        step2 = None
        if resume and _step2_path.is_file():
            try:
                _saved_step2 = json.loads(_step2_path.read_text(encoding="utf-8"))
                _saved_result = _saved_step2.get("result") or {}
                _saved_path = str(_saved_result.get("final_path") or "")
                if (_saved_step2.get("prompt_sha8") == _skc.sha8(prompt)
                        and _saved_path and Path(_saved_path).is_file()):
                    step2 = _saved_result
                    log(f"=== Step 3/4: 【断点继续】复用已完成成品 → {_saved_path}（不重新提交）===")
                else:
                    log("⚠ 盘上成片与本次 prompt 指纹不一致或文件缺失，重新生成")
            except Exception as _s2e:                         # noqa: BLE001
                log(f"⚠ 盘上成片结果不可用（{_s2e}），重新生成")
                step2 = None
        if step2 is None:
            _contract_evidence(_whole_dir, seg_board=storyboard, seg_prompt=prompt,
                               compiled_seg=compiled, seg_duration=float(target_duration),
                               ref_video=trimmed_path, seg_product_paths=list(product_image_paths),
                               source_fit=trimmed_path, extra_note=extra_compile_note,
                               narration_binding=str(Path(output_dir) / "voice_script_binding.json"),
                               compile_facts=_cc_facts,
                               log=log)
            if stop_after == "compile":
                log("档位=只编译 ⇒ 不提交、不建 hold、0 分（提示词/分镜/回执已落在 %s）" % _whole_dir)
                return {
                    "compiled_only": True, "stop_after": "compile",
                    "prompt": prompt, "storyboard": storyboard, "media_manifest": media_manifest,
                    "target_duration": target_duration, "trimmed_path": trimmed_path,
                    "task_id": None, "result_url": None, "local_path": None,
                    "final_path": None, "output_duration": None, "duration": None,
                    "duration_status": "not_submitted",
                    "segments": [{"seg": 1, "dir": _whole_dir,
                                  "prompt_path": str(Path(_whole_dir) / "prompt.txt"),
                                  "window": [0.0, float(target_duration)]}],
                }
            if engine == "h3":
                log("=== Step 3/4: 满雪 H3 全能参考（执行器）→ 原视频 + 产品图 + 原提示词 ===")
                step2 = await _run_executor(
                    exec_prompt=prompt,
                    exec_video=trimmed_path,
                    exec_duration=float(target_duration),
                    exec_product_paths=product_image_paths,
                    exec_model_path=model_image_path,
                    exec_scene_path=scene_image_path,
                    exec_manifest=media_manifest,
                    exec_dir=_whole_dir,
                )
            else:
                log("=== Step 3/4: Wan3.0（执行器）→ 生成成品视频 ===")
                step2 = await _run_executor(
                    exec_prompt=prompt,
                    exec_video=trimmed_path,
                    exec_duration=float(target_duration),
                    exec_product_paths=product_image_paths,
                    exec_model_path=model_image_path,
                    exec_scene_path=scene_image_path,
                    exec_manifest=media_manifest,
                    exec_dir=_whole_dir,
                )
            # 🔴 Step 3 交付凭据：prompt 指纹 + 成片结果，下次断点继续直接复用
            try:
                _step2_path.write_text(json.dumps(
                    {"prompt_sha8": _skc.sha8(prompt),
                     "target_duration": float(target_duration),
                     "result": step2}, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception as _s2w:                         # noqa: BLE001
                log(f"⚠ Step 3 结果落盘失败（不影响本次交付）：{_s2w}")

    _bad = delivery_problems(step2)
    if _bad:
        _why = str(step2.get("submit_error") or "").strip()
        raise RuntimeError(
            "Step 3 没有交付物 ⇒ 不算跑完（%s）。拒因：%s｜"
            "这一单 0 分未扣、上游任务没建，把拒因原话贴回来再决定下一步。"
            % ("；".join(_bad), _why or "没记")
        )
    if on_phase:
        on_phase("completed", {"final_path": step2.get("final_path", "")})
    log(f"=== 工作流完成 → {step2.get('final_path', '')} ===")

    return {
        "prompt": prompt,
        "storyboard": storyboard,
        "media_manifest": media_manifest,
        "raw_result_url": step2["raw_result_url"],
        "raw_output_duration": step2["raw_output_duration"],
        "final_result_url": step2["final_result_url"],
        "final_path": step2["final_path"],
        "final_output_duration": step2["final_output_duration"],
        # 兼容内部调用：result_url/local_path 指向最终交付版本
        "result_url": step2["final_result_url"],
        "local_path": step2["final_path"],
        # ★明确语义字段（替代原 "duration": int 的歧义）
        "target_duration": target_duration,          # Step 0 锁死的全局硬参数（最终输出应等于此值）
        "output_duration": step2["output_duration"], # 实际成品时长（float，与 target_duration 对比）
        "wan_duration_param": step2["wan_duration_param"],  # Wan API duration 参数（整数 -1）
        # ★最终状态枚举（仅三种）：pass / trimmed / generation_failed
        #   trim_failed / regenerate / ffprobe_failed 等仅作为 attempt_failure_reason 出现
        "duration_status": step2["duration_status"],
        "attempt_failure_reason": step2.get("attempt_failure_reason"),  # attempt 内部失败原因（诊断用）
        # 兼容旧调用方：duration 字段保留，值 = output_duration（float，不再为 int）
        "duration": step2["output_duration"],
        # ★已删除 mimo_detected_duration：ffprobe 得到的就是 target_duration
        "task_id": step2["task_id"],
        "trimmed_path": trimmed_path,
        "trim_range": trim_range,
        "prompt_check": check,
        "step1_5_compiled": True,
        "primary_image_id": primary_image_id,
    }
