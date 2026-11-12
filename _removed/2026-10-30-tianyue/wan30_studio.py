"""wan3.0 直出工作台（2026-10-21 新建 · 挂 8090）。

形状：传【参考视频 / 参考音频 / 参考图】+ 提示词 ⇒ 出一条 wan3.0 成片。
本页【不造任何第二份判据】，一切问网关：
  · 参数校验（花钱前、0 消耗）  = gateway._tianyue_payload(params, lane="wan30-studio")
  · 单价 / 要不要人批           = gateway.unit_price / gateway.needs_spend_approval
  · 素材换公网直链（0 分记台账） = gateway.upload_media(key_hint="wan3")
  · 花钱那一刀                  = gateway.invoke("video.generate", "wan30-studio", …)
    ⇒ 预扣 → 提交 → 轮询 → 落盘 → 结算/回滚，全在网关账本里（对不到人的账就是没账）。

这一条【免批不免账】（2026-10-21 用户口径："video-skill 的工作流生视频都不需要审批，和 dsh 是独立的"）：
人就在浏览器上，点一次「提交生成」就是点头，不必再弹一张窗问第二遍。免的是批准窗，不是记账 ——
预扣→结算/回滚照走 `invoke()`。免批登记在**能力级**白名单 `gateway.SPEND_APPROVAL_EXEMPT` 里的
`video.generate`（13 条视频生成腿一起松开，含超级复刻 `wan3-prime`）；`video.edit`/`lipsync`/`image.edit`
不在这一格里，照旧要批。判据只有 `needs_spend_approval()` 一处，所以 8090 那四个
`_spend_or_423("video.generate", …)` 提交口自动变成空操作，不用一个个去改。
范围闸（免到哪条能力为止）与反向对照见 scripts/check-wan30-studio.py。

lane 身份：`video.generate/wan30-studio` 与超级复刻的 `video.generate/wan3-prime` 是同一条渠道、
同一个适配器，但【两条身份两条登记】—— wan3-prime 已被 super-clone 的合同档声明，直连提交会被
"没有合同回执"判死（0 消耗拒绝），借它等于逼工作台去伪造别人的回执。
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse

from config import settings

router = APIRouter()

CAP = "video.generate"
PURPOSE = "wan30-studio"
LANE = "%s/%s" % (CAP, PURPOSE)
PAGE_PATH = Path(settings.FRONTEND_DIR) / "wan30_studio.html"
STUDIO_ROOT = Path(settings.OUTPUT_DIR) / "wan30_studio"
UPLOAD_ROOT = Path(settings.UPLOAD_DIR)

# 素材口：浏览器传上来的文件先落 uploads/，再经 gateway.upload_media 换成公网直链
MEDIA_KINDS = {
    "video": (".mp4", ".mov", ".webm", ".mkv", ".avi"),
    "audio": (".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"),
    "image": (".jpg", ".jpeg", ".png", ".webp", ".bmp"),
}
MEDIA_NOTES = {
    "image": "渠道文档把 image_urls 标成必填，但 2026-10-20 实测不传图照样建单（task_rIoIXogLG5Pc2JLfUByayAAE5hTw7e99 "
             "completed）⇒ 本页按『建议给、不给也不拦』处理，不假装硬必填。",
    "video": "上游会真去下载这条外链；坏链回『参考素材下载失败』（PIX 实测）。是否真被读进【时间轴】有条件，见 known_limits。",
    "audio": "参考音频起什么作用【未实测】：参数表只列 audio_urls，没写是『照这条念』还是『只取音色』⇒ 出片后要用耳朵或 "
             "ASR 复验，不许凭参数在交付里写『口播已改』。",
}

_TASKS: Dict[str, dict] = {}
_TASKS_LOCK = threading.Lock()

# ── 自动回传（2026-10-21 用户定："捞回这一格要自动回传"）────────────────────────
# 上游任务号在拿到号的那一刻就被网关写进成片旁边的 `wan3_job.json`，所以"进程死了/页面关了/轮询超时"
# 都不需要人来贴号：扫一遍盘，凡是【有号、盘上没片】的就自己 GET 回来。
# 🔴 这一条腿只发 GET，一个 POST 都不发 ⇒ 永远不会双花（这家渠道没有对账口）。
# 只扫本工作台自己的目录 `outputs/wan30_studio/`：超级复刻那些任务目录不归我改。
RECONCILE_INTERVAL = 30.0
_WATCH_FRESH = 120.0            # 本进程正在盯的任务：心跳没老就不抢它的活
# ── 本页给的两条模型（2026-10-21 用户指定，两条都是 prime 档；其余一律从页面摘掉）────────────
#   1080P → `ME-WAN3-prime-1080p-1055`（替换掉当天被渠道回「上游创建任务失败 / 余额不足」的 ME-wan30-1080p-1055）
#   720P  → `S-Wan30-prime-720p-1050-se`（当天 17:06 真出过片的那条：2 参考图 + 1 参考视频 + 11.89 秒 → 10.98MB）
# 分辨率【不单独选】：选模型就等于选档，页面上拼不出"要 1080P 却跑 720p 模型"这种组合
# （那正是 2026-10-03 mini→h3 马甲的形状）。档位唯一源在 `gateway._WAN30_RES_MODEL`，本页只做校验不做映射。
# 上游【没有】媒体能力卡（实测 GET /v1/models 每条只回 supported_endpoint_types）⇒ "支持到什么程度"
# 只能靠已建任务的终态与成片反推，没跑过的照实写未实测。取证：scripts/probe-wan30-media.py（全只读 GET）。
MODEL_RULES = {
    "ME-WAN3-prime-1080p-1055": {
        "resolution": "1080P",
        "role": "1080P 档",
        "duration_min": 1,
        "duration_max_with_ref_video": None,
        "media": {
            "image": "【未实测】这条在我们链路上是第一次接，没有任何一发已建任务的终态可读",
            "video": "【未实测】同上；同族的 ME-wan30-1080p 实测过『参考视频体积太大就不上时间轴』，别照抄到这条",
            "audio": "【未实测】",
        },
        "loose": "🔴 今天之前这条【一条都没跑过】⇒ 时长/媒体上限/排队时长全部未知，先拿小样本试，"
                 "不许照旧 ME-wan30 那档的宽松度推断",
        "proven": False,
        "fallback": "本页【显式点名】这条 = 锁死它，不会自动换 PIX。PIX 的实测口径留着当参考："
                    "duration<5 被拒、带参考视频必须 5-15 秒；图/视频 18:31 实测可用（2 图 + 1 段参考视频 + 15 秒 "
                    "9:16 → 16.84MB，task_l4zt4LCRkT4zdxarrM086e8rPYq7C3pM），但参考音频至今没送过 ⇒ 音频仍未证实",
    },
    "S-Wan30-prime-720p-1050-se": {
        "resolution": "720P",
        "role": "720P 档",
        "duration_min": 1,
        "duration_max_with_ref_video": None,
        "media": {
            "image": "实测可用：17:06 那条真出片带了 2 张参考图（产品图 + 模特图，RH CDN 公网外链）",
            "video": "实测可用：同一条带 1 段参考视频（原片裁的 11.89 秒），POST→落盘 2 分 16 秒，成片 10.98MB",
            "audio": "【没送过】那条 `reference_audio_sent=False` ⇒ 这条的参考音频到今天没有证据",
        },
        "loose": "实测口径只有『720P + 3:4 + 11.89 秒』这一发；⚠️ 3:4 的『720P』实际尺寸 = 832×1108"
                 "（不是 1280×720 那个横版口径），验收别拿 1280 去对。排队时长看运气：同机另一条 15 秒 9:16 排了 43 分钟",
        "price_seen": "2026-10-21 18:4x 实测：这条 12 秒 3:4 上游要求预扣 ¥4.20/次（403 insufficient_user_quota 原话"
                      "『用户剩余额度: ¥0.260000, 需要预扣费额度: ¥4.200000』⇒ 账号已见底；没建单、我方 0 分）",
        "proven": True,
        "proven_shape": {"duration": 11.89, "duration_max": 12,
                         "shape": "720P · 3:4 · 2 张参考图 + 1 段参考视频 · 17:06 落盘 10.98MB · 2 分 16 秒"},
        "fallback": "选这条 = 锁死 720P 档；PIX 在渠道目录里【没有】720p 变体 ⇒ 网关不会拿 1080p 的模型来兜"
                    "（那等于悄悄换分辨率），本档只会同分辨率换线",
    },
    # 🔴 2026-10-23 07:5x 用户定「换 ME-WAN3-prime-720p 渠道」⇒ 这条成为 720P 档首选
    #    （`gateway._WAN30_RES_MODEL["720P"]`）。上面那条 10-21 实测过的 `-se` **不摘**：
    #    它是本档目前唯一有出片证据的一条，删了页面就只剩一张没跑过的卡（那正是"没数据的腿进池"要防的）。
    #    ⚠️ 两条口径不同：这条名字无 `-se` 尾巴 ⇒ **按次**（`duration` 固定传 1，真实秒数只留在 `video_duration`）；
    #    上面那条 `-se` ⇒ 按秒。所以页面必须分开列、各标各的，不能合并成"720P 一档一句话"。
    "ME-WAN3-prime-720p-1055": {
        "resolution": "720P",
        "role": "720P 档（2026-10-23 起的首选）",
        "duration_min": 1,
        "duration_max_with_ref_video": None,
        "media": {
            "image": "【未实测】这条我们自己一条都没建上过单，能力只能照同族 1080p prime 推断、不当证据",
            "video": "【未实测】同上；同族 1080p 今天连续 12+ 发被渠道回 504（上游创建任务失败），换到这条就是为躲那条线",
            "audio": "【没送过】",
        },
        "loose": "🔴 未实测 ⇒ 时长/媒体上限/排队时长全部未知，不许照 `-se` 那条的 832×1108 或 12 秒口径推断",
        "proven": False,
        "fallback": "本页【显式点名】这条 = 锁死它。同档候选链（只列同分辨率）："
                    "S-Wan30-prime-720p-1050-se（10-21 实测出过片）→ S-Wan30-720p-1050-se → ME-wan30-720p-1055（干涸那条，排末尾）",
    },
}
_ABANDON_AFTER = 12             # 连续 12 轮 GET 都失败（约 6 分钟）⇒ 标 abandoned 停手，写下原因。
                                # 为什么必须有：号可能是编的/已过期，一条永远取不回来的死号不该被追到天荒地老。
_pulling: set = set()
_pull_lock = threading.Lock()
_reconcile_stats: Dict[str, Any] = {"scanned": 0, "pulled": 0, "waiting": 0, "failed": 0, "ts": 0.0}
_reconciler_on = [False]


def _gw():
    from backend import gateway
    return gateway


def _now() -> float:
    return time.time()


def _stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _task_dir(task_id: str) -> Path:
    return STUDIO_ROOT / task_id


def _out_path(task_id: str) -> str:
    return str(_task_dir(task_id) / "wan30.mp4")


def _public_base() -> str:
    """本机直链前缀（成片走 8090 已有的 /outputs 静态挂载，不再造第二套交付口）。"""
    return "http://127.0.0.1:%s/outputs/wan30_studio" % getattr(settings, "PORT", 8090)


def _params_from_body(body: dict) -> dict:
    """把页面表单拼成网关入参，并归一出一个稳定 task_id（素材只认已换成公网直链的那批）。"""
    def urls(k):
        return [str(u or "").strip() for u in (body.get(k) or []) if str(u or "").strip()]
    task_id = str(body.get("task_id") or "").strip() or "w30-" + uuid.uuid4().hex[:8]
    # 选模型 = 选分辨率档（唯一映射在 MODEL_RULES[*]["resolution"]，那边又对齐网关档位表）。
    # 页面传来的 resolution 只用来做【冲突检查】（见 submit），不参与决定跑哪条。
    model = str(body.get("model") or "").strip() or next(iter(MODEL_RULES), "")
    res = str((MODEL_RULES.get(model) or {}).get("resolution") or body.get("resolution") or "1080P")
    return {
        "task_id": task_id,
        "model": model,
        "body_resolution": str(body.get("resolution") or "").strip().upper(),
        "params": {
            "prompt": str(body.get("prompt") or "").strip(),
            "duration": body.get("duration"),
            "resolution": res,
            "ratio": str(body.get("ratio") or "adaptive"),
            "image_urls": urls("image_urls"),
            "video_urls": urls("video_urls"),
            "audio_urls": urls("audio_urls"),
            "output_path": _out_path(task_id),
            # 🔴 永远显式点名：本页不给"不点名 = 主 + 自动兜底"这条路（他要的就是只留这两条）
            "model": model,
        },
    }


def model_warnings(params: dict) -> List[str]:
    """不拦、但必须出声：这次给的参数【超出这条模型实测过的口径】多少。

    为什么不做成硬拦：除了 PIX 那两条（时长下限 5、带参考视频 5-15）是上游原话，其余上限/媒体数量
    上游既不写在参数表也不回显 ⇒ 拦了就是我替渠道编规则。所以：编不出规则的一律【放行 + 出声】，
    编得出规则的在 `check_model_rules` 里硬拦。
    """
    ru = MODEL_RULES.get(str(params.get("model") or "")) or {}
    out: List[str] = []
    if ru and not ru.get("proven"):
        out.append("%s 在我们链路上还没跑过 ⇒ 时长/媒体上限/排队全是未实测，这一发当小样本看，"
                   "出片后核对实际尺寸与是否照参考走。" % params.get("model"))
    try:
        dur = int(float(params.get("duration") or 0))
    except (TypeError, ValueError):
        return out
    ok = ru.get("proven_shape") or {}
    if ok and dur and dur > int(ok.get("duration_max") or 0):
        out.append("%s 实测只跑过 %s 秒这一发（%s），现在给 %d 秒 ⇒ 超出实测口径，可能排更久或被上游拒。"
                   % (params.get("model"), ok.get("duration"), ok.get("shape"), dur))
    return out


def _job_note(task_id: str) -> dict:
    """读网关在成片旁边落的 wan3_job.json —— 上游任务号一拿到就有，进程死了也还在（重投=双花）。"""
    p = _task_dir(task_id) / "wan3_job.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}


def _job_note_path(task_id: str) -> Path:
    return _task_dir(task_id) / "wan3_job.json"


def mark_note(task_id: str, status: str, extra: dict | None = None) -> None:
    """把回传结果写回 `wan3_job.json` 的 status（只补自己这几格，不动网关写的字段）。

    ⚠️ 写失败【不许】打断回传：号还在盘上，下一轮自愈还会再来一次。
    """
    p = _job_note_path(task_id)
    if not p.is_file():
        return
    try:
        rec = json.loads(p.read_text(encoding="utf-8")) or {}
        rec["status"] = status
        rec["reconcile_at"] = _stamp()
        for k, v in (extra or {}).items():
            rec[k] = v
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
        os_replace(tmp, p)
    except (OSError, ValueError):
        pass


def os_replace(tmp: Path, dst: Path) -> None:
    """`os.replace` 的薄壳（Windows 上目标存在也能换）；单独一层是为了让自检能换掉它。"""
    import os
    os.replace(str(tmp), str(dst))


def scan_jobs() -> List[dict]:
    """扫本工作台目录：每个有 `wan3_job.json` 的任务回一行盘上事实。"""
    rows: List[dict] = []
    if not STUDIO_ROOT.is_dir():
        return rows
    for d in sorted(STUDIO_ROOT.iterdir()):
        p = d / "wan3_job.json"
        if not p.is_file():
            continue
        try:
            rec = json.loads(p.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            rec = {"_read_error": "wan3_job.json 读不出来"}
        mp4 = d / "wan30.mp4"
        have = mp4.is_file()
        rows.append({
            "task_id": d.name,
            "job_id": str(rec.get("task_id") or ""),
            "status": str(rec.get("status") or ""),
            "lane": str(rec.get("lane") or ""),
            "model": str(rec.get("model") or ""),
            "submitted_at": str(rec.get("submitted_at") or ""),
            "reconcile_at": str(rec.get("reconcile_at") or ""),
            "error": str(rec.get("last_error") or "")[:300],
            "attempts": int(rec.get("reconcile_attempts") or 0),
            "have_file": have,
            "bytes": mp4.stat().st_size if have else 0,
            "local_path": str(mp4) if have else "",
            "out_url": "%s/%s/wan30.mp4" % (_public_base(), d.name),
            "note_read_error": rec.get("_read_error") or "",
        })
    return rows


def pull(task_id: str, job_id: str) -> dict:
    """只 GET 不重投：把这条上游任务取回盘上。成功=落盘，未完成=留在待捞（下一轮再来）。

    走 `gw.call` 而不是 `gw.invoke`：捞回不是"花钱那一刀"（原提交已经预扣过、失败已回滚），
    这条不建预扣、不动账本、不碰批准。
    """
    gw = _gw()
    out = _out_path(task_id)
    _task_dir(task_id).mkdir(parents=True, exist_ok=True)
    try:
        res = gw.call(CAP, PURPOSE, job_id=job_id, output_path=out) or {}
    except Exception as e:                                        # noqa: BLE001
        res = {"success": False, "error": "%s: %s" % (type(e).__name__, str(e)[:300])}
    ok = bool(res.get("success"))
    stage = "recovered" if ok else "recover_wait"
    err = "" if ok else str(res.get("error") or res)[:500]
    with _TASKS_LOCK:
        t = _TASKS.get(task_id) or {"task_id": task_id, "lane": LANE, "params": {}}
        t.update({"stage": stage, "heartbeat": _now(), "ok": ok, "job_id": job_id,
                  "out_url": "%s/%s/wan30.mp4" % (_public_base(), task_id),
                  "local_path": res.get("path") or "", "bytes": res.get("bytes"),
                  "model_used": res.get("model") or t.get("model_used") or "",
                  "error": err, "credits": t.get("credits") or {},
                  "note": "自动回传：只 GET 上游，一个 POST 都不发"})
        _TASKS[task_id] = t
    prev = _job_note(task_id)
    tries = int(prev.get("reconcile_attempts") or 0)
    if ok:
        mark_note(task_id, "delivered", {"delivered_at": _stamp(), "bytes": res.get("bytes"),
                                         "reconcile_attempts": tries})
    else:
        tries += 1
        mark_note(task_id, "abandoned" if tries >= _ABANDON_AFTER else "pending",
                  {"last_error": err, "reconcile_attempts": tries, "delivered_at": ""})
    return res


def reconcile_once() -> dict:
    """一轮自愈：有号、盘上没片、本进程没在盯 ⇒ 起一条 GET。"""
    stats = {"scanned": 0, "pulled": 0, "waiting": 0, "failed": 0, "ts": _now()}
    for r in scan_jobs():
        stats["scanned"] += 1
        if r["have_file"] or not r["job_id"] or r["status"] in ("delivered", "abandoned"):
            continue                       # 已有片 / 没号 / 已经追到放弃 ⇒ 不起腿
        with _TASKS_LOCK:
            t = _TASKS.get(r["task_id"]) or {}
        if t.get("stage") in ("queued", "submitting", "recovered") and \
                (_now() - float(t.get("heartbeat") or t.get("created_at") or 0)) < _WATCH_FRESH:
            continue                       # 本进程正在盯这一条，别抢它
        with _pull_lock:
            if r["job_id"] in _pulling:
                continue
            _pulling.add(r["job_id"])
        stats["pulled"] += 1

        def _job(tid=r["task_id"], jid=r["job_id"]):
            try:
                pull(tid, jid)
            finally:
                with _pull_lock:
                    _pulling.discard(jid)

        threading.Thread(target=_job, daemon=True).start()
    _reconcile_stats.clear()
    _reconcile_stats.update(stats)
    return stats


def reconciler_loop() -> None:
    while True:
        try:
            reconcile_once()
        except Exception as e:                                    # noqa: BLE001
            print("[wan30-studio] 自愈这一轮炸了（下一轮照跑）：%s: %s"
                  % (type(e).__name__, str(e)[:200]), flush=True)
        time.sleep(RECONCILE_INTERVAL)


def start_reconciler() -> dict:
    """开机自愈 + 常驻轮询。由 app.py 的 lifespan 调（幂等，重复调不会多起线程）。"""
    if _reconciler_on[0]:
        return {"already": True, **_reconcile_stats}
    _reconciler_on[0] = True
    first = reconcile_once()
    threading.Thread(target=reconciler_loop, daemon=True).start()
    print("[wan30-studio] 自动回传已启动：扫到 %d 条任务，本轮起腿 %d 条（只 GET）"
          % (first["scanned"], first["pulled"]), flush=True)
    return {"started": True, **first}


def _page_models() -> List[dict]:
    """本页给哪几条模型 + 每条在网关档位里【还在不在】。

    🔴 档位唯一源是 `gateway.wan30_models_for(分辨率)`：本页只列、不映射。哪天网关把那档换掉了，
       这里就报 `in_lane=False`，提交被大声拒（0 分 0 请求），而不是悄悄按另一条跑。
    """
    gw = _gw()
    out: List[dict] = []
    for mid, ru in MODEL_RULES.items():
        res = str(ru.get("resolution") or "1080P")
        chain = list(gw.wan30_models_for(res))
        out.append(dict(ru, id=mid, resolution=res, in_lane=mid in chain, lane_chain=chain))
    return out


def _route_models() -> List[str]:
    return [m["id"] for m in _page_models() if m["in_lane"]]


def check_model_rules(params: dict, model: str) -> str:
    """模型相关的【花钱前】判据：返回拒因空串=放行。

    为什么要在工作台做这一层：ME 宽松到 duration=0 都建单、PIX 实测 duration<5 直接拒
    ⇒ 同一份表单在两条模型上合法域不一样。不拦的代价不是 0 分，是"发出去被上游拒 + 一条失败任务"。
    """
    rules = MODEL_RULES.get(model) or {}
    if not rules:
        return ""
    try:
        dur = int(float(params.get("duration") or 0))
    except (TypeError, ValueError):
        return "duration 要正整数秒（收到 %r）" % (params.get("duration"),)
    dmin = int(rules.get("duration_min") or 1)
    if dur < dmin:
        return ("%s 实测时长下限 %d 秒（上游原话『必须是>=%d秒』），现在给的是 %d ⇒ 先改参数，"
                "别发出去让上游拒" % (model, dmin, dmin, dur))
    dmax = rules.get("duration_max_with_ref_video")
    if dmax and (params.get("video_urls") or []) and dur > int(dmax):
        return ("%s 有参考视频时实测只能 5-%d 秒，现在给 %d 秒 ⇒ 这一条会直接被上游拒；"
                "要更长就切段，或换另一档模型（各自口径见上面那张模型表）" % (model, int(dmax), dur))
    return ""


# ─────────────────────────────────────────────────────────
# 页面 / 选项
# ─────────────────────────────────────────────────────────

@router.get("/wan30-studio", response_class=HTMLResponse)
async def studio_page():
    if not PAGE_PATH.is_file():
        raise HTTPException(404, "wan3.0 直出工作台页面未找到：%s" % PAGE_PATH)
    return HTMLResponse(PAGE_PATH.read_text(encoding="utf-8"))


@router.get("/api/wan30-studio/options")
async def studio_options():
    """页面能选的一切来自网关唯一源，不在前端写死档位（写死=第二份真相）。"""
    gw = _gw()
    try:
        r = gw.resolve(CAP, PURPOSE)
    except gw.RouteError:
        # 2026-10-30『tianyue lane 移除』：腿下架后页面要拿到【诚实拒因】，不能 500
        # （500 会被前端误标成"后端还是旧版"）。
        return {"lane": LANE,
                "disabled": "这条腿已下架（2026-10-30『tianyue lane 移除』：wan3.0 固定走 youzan666 的 "
                            "video.generate/wan3-prime，tianyue 余额不足且 503/504 连发）。"
                            "恢复=删 model_prices.json 里 video.generate/wan30-studio 那行 disabled。"}
    models = []
    for m in _page_models():
        models.append({
            "id": m["id"], "role": m.get("role") or "", "resolution": m["resolution"],
            "duration_min": int(m.get("duration_min") or 1),
            "duration_max_with_ref_video": m.get("duration_max_with_ref_video"),
            "media": m.get("media") or {}, "loose": m.get("loose") or "",
            "fallback": m.get("fallback") or "", "proven": bool(m.get("proven")),
            "proven_shape": m.get("proven_shape") or {},
            "price_seen": m.get("price_seen") or "",
            "in_lane": bool(m["in_lane"]), "lane_chain": m["lane_chain"],
        })
    stale = [m["id"] for m in models if not m["in_lane"]]
    return {
        "lane": LANE,
        "provider": r["provider"],
        "model": r["model"],
        "models": models,
        "media_capability": "上游没有媒体能力卡：实测 GET /v1/models 每条只回 supported_endpoint_types（=全 "
                            "openai），媒体形状只有渠道文档那三个数组 + 已建任务的终态原话 ⇒ "
                            "下表逐条标注来源，未证实的一律写未证实，不猜上限。",
        "fallback_model": str(gw._get("TIANYUE_WAN30_FALLBACK_MODEL") or ""),
        "key_ready": bool(r["api_key"]),
        "base_url": r["base_url"],
        "proxy": str(gw._get("TIANYUE_PROXY_URL") or ""),
        "ratios": list(gw._WAN30_RATIOS),
        # 分辨率【不是一格可选控件】：选模型就等于选档（2026-10-21 用户指定只留这两条 prime 模型）。
        # 这里仍把 lane 的档位表原样发出去，页面文案一律现读，不许自己编一份。
        "resolution_chosen_by": "model",
        "stale_models": stale,
        "resolutions_in_lane": list(getattr(gw, "_WAN30_RESOLUTIONS", ())),
        "resolution_model": dict(getattr(gw, "_WAN30_RES_MODEL", {}) or {}),
        # 🔴 计费口径不再是一个全局开关（2026-10-22）：跟着模型名尾巴走 —— `-se` = 按秒传真实秒数，
        #    没有尾巴 = 按次、duration 固定传 1。判据唯一源 = gateway.wan30_is_per_second()。
        "duration_mode": "按模型名判（-se=按秒 / 无尾巴=按次），本页不另设开关",
        "model_billing": {m["id"]: ("按秒" if gw.wan30_is_per_second(m["id"]) else "按次") for m in models},
        "unit_price": gw.unit_price(CAP, PURPOSE),
        "needs_approval": gw.needs_spend_approval(CAP, PURPOSE),
        "ledger": True,          # 免批不等于免账：预扣/结算/回滚照走
        "media_kinds": {k: list(v) for k, v in MEDIA_KINDS.items()},
        "media_notes": MEDIA_NOTES,
        # 说清楚这条腿【没有】什么，别让人在页面上找一个不存在的开关
        "not_supported": {
            "first_frame/last_frame": "tianyue 参数表里没有首尾帧（点名给就本地拒，0 分不发请求）",
            "audio on/off": "没有『原声/静音』开关；要动声音只能给参考音频",
            "分辨率单独选": "没有这一格：分辨率跟着模型档走（1080P 档 = %s，720P 档 = %s），"
                            "拼不出「要 1080P 却跑 720p 模型」那种组合"
                            % (str(getattr(gw, "_WAN30_RES_MODEL", {}).get("1080P")),
                               str(getattr(gw, "_WAN30_RES_MODEL", {}).get("720P"))),
        },
        "known_limits": [
            "素材只吃公网 http/https 外链：本页上传时自动换直链（gateway.upload_media，0 分、记台账）",
            "两条模型的合法域【不一样】（时长/媒体），别按一条的规矩填另一条 ⇒ 见上面那一排模型档",
            "参考视频是否真被读进时间轴【有条件】：实测 1.38MB 的参考段跟上了（同刻相关 0.574），"
            "19.77MB 的那条没跟上 ⇒ 可疑是上游对体积/码率有上限、超了悄悄不用；判据 scripts/prove-reference-video.py",
            "单价：我方价目 60 分/段仍是【占位】；上游第一次报出了数——2026-10-21 18:4x 一条 12 秒 3:4 要"
            "预扣 ¥4.20（403 原文带的），但这家没有对账口回读历史实扣 ⇒ 换算成积分前先去渠道后台核",
            "一条实测 10–17 分钟/段，轮询上限 65 分钟 ⇒ 提交后别重复点；号一拿到就落盘，成片由「自动回传」自己 GET",
        ],
    }


# ─────────────────────────────────────────────────────────
# 素材上传：本地文件 → 公网直链
# ─────────────────────────────────────────────────────────

@router.post("/api/wan30-studio/media")
async def studio_media(file: UploadFile = File(...), kind: str = Form("image")):
    kind = str(kind or "").strip().lower()
    if kind not in MEDIA_KINDS:
        raise HTTPException(400, "kind 只认 %s（收到 %r）" % ("/".join(MEDIA_KINDS), kind))
    name = str(file.filename or "").strip()
    ext = (Path(name).suffix or "").lower()
    if ext not in MEDIA_KINDS[kind]:
        raise HTTPException(400, "%s 不收 %s（这一口只认：%s）"
                            % (kind, ext or "无扩展名", " ".join(MEDIA_KINDS[kind])))
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "空文件不传（上游会回 200 + code!=0，白占一次台账）")
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    save = UPLOAD_ROOT / ("w30s_%s_%s%s" % (kind, uuid.uuid4().hex[:10], ext))
    save.write_bytes(raw)

    gw = _gw()
    env = gw.upload_media(str(save), provider="runninghub", key_hint="wan3", account="local")
    res = env.get("result") or {}
    url = str(res.get("download_url") or "").strip()
    if not env.get("ok") or not url:
        # 传不上就是传不上：拒因原样带出，不许回一句"上传失败"让人去猜是 key 坏还是网络坏
        save.unlink(missing_ok=True)
        raise HTTPException(502, "素材没换成公网直链：%s" % str(env.get("error") or env)[:400])
    return {
        "kind": kind,
        "filename": name,
        "local_path": str(save),
        "size": len(raw),
        "public_url": url,
        "ref_name": res.get("file_name") or "",
        "host": res.get("host") or "",
        "key_src": res.get("key_src") or "",      # 用的哪把上传 key（不回显值）
        "ledger_error": env.get("ledger_error") or "",
        "ts": _stamp(),
    }


# ─────────────────────────────────────────────────────────
# 提交（花钱那一刀在网关里：预扣 → 提交 → 轮询 → 落盘 → 结算/回滚）
# ─────────────────────────────────────────────────────────

def _run_job(task_id: str, params: dict) -> None:
    gw = _gw()
    with _TASKS_LOCK:
        t = _TASKS.get(task_id) or {}
        t.update({"stage": "submitting", "started_at": _now(), "heartbeat": _now()})
        _TASKS[task_id] = t
    try:
        env = gw.invoke(CAP, PURPOSE, account="local", params=params)
    except Exception as e:                                       # noqa: BLE001
        env = {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:400])}
    res = env.get("result") or {}
    with _TASKS_LOCK:
        _TASKS[task_id] = {
            "task_id": task_id,
            "stage": "done" if env.get("ok") else "failed",
            "heartbeat": _now(),
            "finished_at": _now(),
            "ok": bool(env.get("ok")),
            "lane": LANE,
            "params": params,
            "out_url": "%s/%s/wan30.mp4" % (_public_base(), task_id),
            "job_id": str(env.get("job_id") or res.get("job_id") or ""),
            "error": str(env.get("error") or "")[:800],
            "credits": env.get("credits") or {},
            "model_used": res.get("model") or "",
            "fallback_from": res.get("fallback_from") or "",
            "fallback_why": res.get("fallback_why") or "",
            "local_path": res.get("path") or "",
            "upstream_url": res.get("url") or "",
            "bytes": res.get("bytes"),
            "note": res.get("note") or "",
        }
    try:
        (_task_dir(task_id) / "studio_envelope.json").write_text(
            json.dumps({"task_id": task_id, "lane": LANE, "params": params, "envelope": env},
                       ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


@router.post("/api/wan30-studio/submit")
async def studio_submit(body: dict):
    gw = _gw()
    # 2026-10-30 腿下架 ⇒ 收单口先拒（0 分 0 请求、不建任务目录），别让单子进了后台线程才死。
    if gw.is_disabled(CAP, PURPOSE):
        raise HTTPException(503, "wan30-studio 已下架（2026-10-30『tianyue lane 移除』，wan3.0 固定走 "
                                 "video.generate/wan3-prime=youzan666）⇒ 不收单。恢复=删 model_prices.json "
                                 "disabled 里那一行。")
    packed = _params_from_body(body or {})
    params, task_id = packed["params"], packed["task_id"]
    allowed = _route_models()
    # 🔴 本页只给这两条模型（2026-10-21 用户指定，其余从页面摘掉）。点名字之外的、或网关把档位换掉了
    #    导致这条不在档里 ⇒ 一律【本地拒】，0 分 0 请求，绝不悄悄按另一条计费口径跑。
    if not allowed:
        raise HTTPException(503, "wan30-studio 没有一条模型还在网关档位里（stale）⇒ 停手，先去档位表核对，"
                                 "别拿一条对不上号的模型去花钱")
    if params.get("model") not in allowed:
        raise HTTPException(400, "wan30-studio 这一页只给 %s（收到 model=%r）⇒ 拒，不悄悄换家"
                            % (" / ".join(allowed), params["model"]))
    # 分辨率不单独选：请求体给了 resolution 就必须等于所选模型的档，否则就是"页面一套、机器另一套"。
    want_res = str((MODEL_RULES.get(params.get("model") or "") or {}).get("resolution") or "")
    if packed.get("body_resolution") and packed["body_resolution"] != want_res:
        raise HTTPException(400, "分辨率不单独选：%s 属于 %s 档（请求体给的是 %r）⇒ 拒。"
                            "lane 的档位表（唯一源）= %s"
                            % (params.get("model"), want_res, packed["body_resolution"],
                               json.dumps(getattr(gw, "_WAN30_RES_MODEL", {}), ensure_ascii=False)))
    vals, err = gw._tianyue_payload(params, lane=PURPOSE)        # 花钱前的本地判据，0 分 0 请求
    if err:
        raise HTTPException(400, err)
    merr = check_model_rules(params, params.get("model") or (allowed[0] if allowed else ""))
    if merr:
        raise HTTPException(400, merr)
    est = int(gw.meter(CAP, PURPOSE, {"count": 1})["credits_estimate"])
    _task_dir(task_id).mkdir(parents=True, exist_ok=True)
    with _TASKS_LOCK:
        _TASKS[task_id] = {"task_id": task_id, "stage": "queued", "lane": LANE, "params": params,
                           "created_at": _now(), "out_url": "%s/%s/wan30.mp4" % (_public_base(), task_id)}
    threading.Thread(target=_run_job, args=(task_id, params), daemon=True).start()
    return {"task_id": task_id, "lane": LANE, "stage": "queued", "credits_estimate": est,
            "model_will_use": params.get("model") or allowed[0],
            "model_pinned": bool(params.get("model")),
            "warnings": model_warnings(params),
            "needs_approval": gw.needs_spend_approval(CAP, PURPOSE),
            "out_url": "%s/%s/wan30.mp4" % (_public_base(), task_id),
            "out_dir": _task_dir(task_id).as_posix()}


@router.get("/api/wan30-studio/status/{task_id}")
async def studio_status(task_id: str):
    with _TASKS_LOCK:
        t = dict(_TASKS.get(task_id) or {})
    if not t:
        note = _job_note(task_id)
        raise HTTPException(404, "本进程没有任务 %s（8090 重启过？）⇒ 不用你贴号：上游任务号 %s 已经落在盘上，"
                            "自动回传每 %.0f 秒自己 GET 一次（只 GET，绝不重投）"
                            % (task_id, note.get("task_id") or "盘上还没号", RECONCILE_INTERVAL))
    if t.get("stage") in ("queued", "submitting"):
        t["job_id"] = _job_note(task_id).get("task_id") or t.get("job_id") or ""
        t["seconds"] = round(_now() - float(t.get("started_at") or t.get("created_at") or _now()), 1)
    return t


@router.post("/api/wan30-studio/recover")
async def studio_recover(body: dict):
    """手动补一次回传（正常情况用不到：`/api/wan30-studio/pending` + 常驻自愈每 30 秒自己 GET）。

    留这一格是给"号是从别处带来的"那种情况（例如另一会话/别台机器提交的任务）。
    🔴 仍然只走 `gw.call`：只 GET 上游，绝不 POST、不建预扣、不动账本。
    """
    gw = _gw()
    job = str((body or {}).get("job_id") or "").strip()
    if not job:
        raise HTTPException(400, "recover 要给 job_id（上游任务号，形如 task_XXXX）")
    task_id = str((body or {}).get("task_id") or "").strip() or "w30r-" + uuid.uuid4().hex[:8]
    res = pull(task_id, job)
    ok = bool(isinstance(res, dict) and res.get("success"))
    return {"task_id": task_id, "ok": ok, "lane": LANE, "job_id": job,
            "local_path": (res or {}).get("path") or "",
            "out_url": "%s/%s/wan30.mp4" % (_public_base(), task_id),
            "error": "" if ok else str((res or {}).get("error") or res)[:500],
            "credits": "本次没花钱（只 GET）"}


@router.get("/api/wan30-studio/pending")
async def studio_pending():
    """盘上事实 + 自愈状态：谁是【有号没片】的、谁已经回传落地。页面每 10 秒读这一格。"""
    rows = scan_jobs()
    for r in rows:
        with _TASKS_LOCK:
            t = _TASKS.get(r["task_id"]) or {}
        r["stage"] = t.get("stage") or ("delivered" if r["have_file"] else "待自动回传")
        r["watching"] = bool(r["job_id"] and r["job_id"] in _pulling)
        r["seconds_ago"] = (round(_now() - float(t.get("heartbeat") or 0), 1)
                            if t.get("heartbeat") else None)
    return {"lane": LANE, "interval_seconds": RECONCILE_INTERVAL,
            "reconcile": dict(_reconcile_stats), "tasks": rows[::-1],
            "rule": "只 GET 上游，一个 POST 都不发；有号没片的每 30 秒自动再捞一次"}


@router.get("/api/wan30-studio/tasks")
async def studio_tasks():
    with _TASKS_LOCK:
        rows = [{"task_id": k, "stage": v.get("stage"), "ok": v.get("ok"),
                 "job_id": v.get("job_id") or "", "error": str(v.get("error") or "")[:200],
                 "out_url": v.get("out_url") or "",
                 "created": time.strftime("%Y-%m-%d %H:%M:%S",
                                          time.localtime(v.get("created_at") or _now()))}
                for k, v in _TASKS.items()]
    return {"tasks": rows[::-1], "lane": LANE}
