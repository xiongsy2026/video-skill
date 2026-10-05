"""hypit Provider 适配面：把统一模型网关包成 hypit「自写 Provider」期望的作业协议。

协议出处 = 分发内样板 `examples/provider-package`（README「Follow a video request」），
我们两端都是自己写 ⇒ 满足上游那条硬约束「OpenAI 兼容标签不代表协议匹配」：

  POST /api/hypit/uploads   请求体=媒体字节，头 content-type（可选 x-person-reference） → {"url": "..."}
  POST /api/hypit/videos    {"model","prompt","duration","resolution","ratio","references":[{url,person}],
                             "firstFrame","lastFrame","account"}                          → {"id"}
  GET  /api/hypit/videos/<id>                                                              → {"status","output","error"}
  GET  /api/hypit/rates?model=<m>            → {"description", ...}   （hypit 的 pricing 只读检查用）
  GET  /api/hypit/assets/<name>              结果文件，collect 步骤用（样板规定：签名 URL 不带账号密钥）

生成动作全部转成 `gateway.invoke(...)` 在后台线程里跑 ⇒ 积分预扣/回滚、路由、审计、屏蔽位
与 CLI/`/api/model/call` **完全同一套**，不另开第二套账。
"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import threading
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPLOAD_DIR = os.path.join(ROOT, "outputs", "_hypit_uploads")
OUT_DIR = os.path.join(ROOT, "outputs", "_hypit_jobs")
DB_PATH = os.environ.get("HYPIT_JOBS_DB") or os.path.join(ROOT, ".gateway_jobs.sqlite")
# hypit 的工作区根（工程源文件在这里）。provider 自报的工程路径必须先归一到这个根 inside 才算数。
HYPIT_WORKSPACE = os.environ.get("HYPIT_WORKSPACE") or r"C:\dsh\hypit_runs"
# 必须用 localhost 而不是 127.0.0.1：hypit 的 address()/pricing source 校验只放 https 或 hostname=localhost。
PUBLIC_BASE = os.environ.get("MODEL_GATEWAY_PUBLIC", "http://localhost:8100")

# hypit 送来的 wire model → 我们的 (capability, purpose)。只登记真能出片的，别的直接拒（不悄悄换模型）。
HYPIT_MODELS = {
    "seedance-2-mini": ("video.generate", "seedance-2.0"),
    "seedance-2.0": ("video.generate", "seedance-2.0"),
    "seedance-2.5": ("video.generate", "seedance-2.5"),
    "omni-flash-10s": ("video.generate", "omni-t2v"),
    # RunningHub H3 导演台（ComfyUI 工作流 v2v/i2v）；hypit provider.js model="mini" 映射到此。
    # 参考图走 RunningHub 自家上传（本地文件 → fileName），吃 image paths，不吃公网 URL。
    "h3": ("video.generate", "h3"),
}
# 本适配面真实能带的输入：只有图片参考（vinted/zexapi 都只吃 image_urls，且要公网可取 URL）。
# 视频/音频参考带不过去 ⇒ 按 hypit 的规矩在 supports 里拒绝，绝不静默丢弃。
MAX_SECONDS = 10
# 本路由实测可出片的时长（账本里 succeeded 的段全是 5s；4/6/8s 三条都被上游 422 打回，
# 而那句 422 的文案还是误导性的"请填写提示词"）。别拿未验证的时长去烧钱：先本地拦，
# 要扩就在 0 分的 plan/pricing 上验过再改这里或设 HYPIT_ALLOWED_SECONDS（逗号分隔）。
ALLOWED_SECONDS = tuple(float(x) for x in
                        (os.environ.get("HYPIT_ALLOWED_SECONDS") or "5").split(","))
RESOLUTIONS = ("720p",)
# 🔴 档位是【每条路由各自的事实】，不是一套全局值套所有模型（2026-10-03 定）：
# provider.js 的 LANES 必须与这里逐条一致（scripts/check-runtime-bindings.py 钉着）——两边都在，
# 是因为改了 provider 但还跑着的老 runtime 进程仍会发旧字段，服务端这一道才是花钱前的最后一闸。
# aspects/last_frame 空 = 本路由兑现不了那个输入，宁可本地拒（0 分）也别提交后让上游乱扣。
LANE_SPECS = {
    "seedance-2-mini": {"seconds": None, "resolutions": RESOLUTIONS,
                        "aspects": None, "last_frame": True, "refs": ("image",)},
    "seedance-2.5": {"seconds": None, "resolutions": RESOLUTIONS,
                     "aspects": None, "last_frame": True, "refs": ("image",)},
    # h3 导演台有 v2v/rv2v 节点（client 的 referenceVideoPath/refImagePaths），所以【收视频参考】：
    # 视频出动作、图片换主体。音频参考仍不收（这条路由没实测过带音频的形态）。
    "h3": {"seconds": None, "resolutions": (), "aspects": ("9:16",), "last_frame": False,
           "refs": ("image", "video")},
    "omni-flash-10s": {"seconds": None, "resolutions": RESOLUTIONS,
                       "aspects": None, "last_frame": True, "refs": ("image",)},
}
DEFAULT_SPEC = LANE_SPECS["seedance-2-mini"]


def _lane_spec(model: str) -> dict:
    """seconds=None ⇒ 跟随 ALLOWED_SECONDS（HYPIT_ALLOWED_SECONDS 可扩，验过才扩）。
    某条路由要单独放时长就把它的 seconds 写成具体元组。"""
    spec = dict(LANE_SPECS.get(str(model), DEFAULT_SPEC))
    if spec["seconds"] is None:
        spec["seconds"] = ALLOWED_SECONDS
    return spec
# hypit studio 的工作台端口（agent 起 `hypit studio --run X --port 5179`）。端口不给 HTTP 参数控制：
# 这个探测口是 ACAO:* 的，能指定端口就等于给本机任意网页做端口扫描。
STUDIO_PORT = int(os.environ.get("HYPIT_STUDIO_PORT") or 5179)
# 上游 vinted 的瞬时错误（实测：503 参考图下载繁忙、400 参考图解析失败、生成超时且明确退积分）⇒ 原话让重试。
# ⚠️ 不含"轮询超时"：那是我们自己等够了，上游可能还在出片，盲目重试会重复花费。
RETRYABLE = ("502", "503", "504", "service_unavailable", "参考图", "生成超时")
# 策略/身份类拒绝：重试也不会变，只会白等 15~20 分钟（实测 "参考图未通过肖像保护" 含"参考图"三字，
# 会被上面那档误判成可重试 ⇒ 必须先排除）。
NON_RETRYABLE = ("肖像", "本人肖像", "内容不合规", "违规", "敏感")


def _retryable(msg: str) -> bool:
    return not any(k in msg for k in NON_RETRYABLE) and any(k in msg for k in RETRYABLE)
MAX_ATTEMPTS = 3
RETRY_WAITS = (30, 60)
# 归属登记（契约见 C:\dsh\docs\ADR-workbench-presence.md）：
#   {"runs": {"<工程目录>": {"session":…,"run":…,"port":…,"ts":…}}, "ts":…}
# ⚠️ 登记的是【会话 ↔ 资源(run)】，不是【会话 ↔ 服务】——studio 是单实例、没有会话概念，
#    拿"谁起过服务"当归属就会永久产权（2026-10-01/10-02 三次复发的真根因）。
# 旧格式 {sessions:[…], session, run, port} 一律判无主：不猜，宁可少弹一次不可错弹一次。
STUDIO_MARK = os.environ.get("HYPIT_STUDIO_MARK") or os.path.join(ROOT, ".gateway_studio.json")
STUDIO_CLAIM_TTL = 30 * 60.0    # 只用于"心跳多久没来"的展示/运维判断，不再用来撤销意图
STUDIO_INTENT_KEEP = 20         # 意图记录是持久的（一条会话属于哪条流水线不该 30 分钟就忘），只保留最近 20 个 run
# omni 那侧的意图判定也收进 8100（video-ui 没有 CORS 头，插件直连读不到响应）
OMNI_UI = os.environ.get("VIDEO_UI_URL") or "http://127.0.0.1:8765/"
VIDEO_RUNS = os.environ.get("VIDEO_UI_RUNS") or r"C:\dsh\video_runs"
OMNI_RUNS_TTL = 5.0
_OMNI_CACHE = {"ts": 0.0, "alive": False}
_RUNS_CACHE = {"ts": 0.0, "rows": []}
# 谁起的 studio 登记在这。归属 = (run, 会话) 对的集合：
#   修的是"会话级全局只增不减 ⇒ 跑过一次就永久拥有页签"；不是"多人共看同一台 studio"。
# 诊断用：记下右栏探测实际带来的会话 id（运维据此知道该给哪个会话 claim；上限 30 条）
_SEEN_SESSIONS = {}
STUDIO_CACHE_TTL = 5.0
_STUDIO_CACHE = {"ts": 0.0, "port": None, "res": {"up": False, "error": "cold"}}
RUN_CACHE_TTL = 15.0            # studio 服务哪个 run 变得很慢，单独用长一点的缓存
_RUN_CACHE = {"ts": 0.0, "port": None, "path": None}


RUN_EXTS = (".svrun", ".svml", ".svs")


def _strip_ext(v: str) -> str:
    for e in RUN_EXTS:
        if v.endswith(e):
            return v[: -len(e)]
    return v


def _norm_run(value: str) -> str:
    """把 run 归一成【工程目录】token：claims 传 `x.svrun`、studio 自报 `x.svml`，两边要能对上。
    裸名（不含 /）保留小写名，走 basename 兜底匹配。"""
    v = str(value or "").strip().replace("\\", "/").strip("/")
    if not v:
        return ""
    if v.endswith(RUN_EXTS):
        v = v.rsplit("/", 1)[0] if "/" in v else ""
    return v.lower()


def _run_matches(claimed: str, served: str) -> bool:
    """claimed 是登记时给的原值，served 是 studio 自报的源文件路径。
    先按【工程目录】精确比；登记时给的是裸工程名（不含 /）才退到 basename 比。"""
    a, b = _norm_run(claimed), _norm_run(served)
    if a and a == b:
        return True
    raw = str(claimed or "").strip().replace("\\", "/").strip("/")
    bare = _strip_ext(raw.lower())
    return bool(bare) and "/" not in bare and bool(b) and bare == b.rsplit("/", 1)[-1]


def _studio_record(refresh_session: str = "") -> dict:
    """读归属登记。旧格式/坏文件 → 空表（=无意图）。

    ⚠️ 语义（2026-10-02 定）：**归属 = 意图，是持久的**——"这条会话属于哪条流水线"不该
    30 分钟就忘；台弹不弹只看意图，引擎活没活只决定台里显示什么。所以这里**不再按 TTL 撤销**，
    只保留最近 STUDIO_INTENT_KEEP 个 run（防文件无限长）。
    带 refresh_session 时把该会话名下的 run 的 ts 顶到现在（右栏每 4 秒的探测就是心跳），
    ts 只用于运维看"多久没人看了"。
    """
    rec = _studio_owner()
    runs = rec.get("runs")
    if not isinstance(runs, dict):
        return {"runs": {}}                     # 旧格式（有 sessions 无 runs）→ 判无主
    now = time.time()
    live = {}
    for key, item in runs.items():
        if not isinstance(item, dict) or not _owners_of(item):
            continue
        try:
            ts = float(item.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        live[str(key)] = {**item, "ts": ts, "run_key": str(key)}
    bumped = False
    if refresh_session:
        for item in live.values():
            if str(refresh_session) in _owners_of(item) and now - item["ts"] > 30.0:
                item["ts"] = now                # 顶心跳（30 秒内不重复落盘）
                bumped = True
    if len(live) > STUDIO_INTENT_KEEP:          # 只裁最旧的，不裁"过期的"
        live = _trim_runs(live)
        bumped = True
    if bumped:
        try:
            with open(STUDIO_MARK, "w", encoding="utf-8") as f:
                json.dump({"runs": live, "ts": now}, f, ensure_ascii=False)
        except OSError:
            pass
    return {"runs": live}


def _owners_of(item: dict) -> list:
    """一个 run 的主人列表：{sessions:[…]} 为主，兼容单值 session（多会话共看同一台 studio）。"""
    out = [str(s) for s in (item.get("sessions") or []) if s]
    if item.get("session") and str(item["session"]) not in out:
        out.append(str(item["session"]))
    return out


def _trim_runs(runs: dict) -> dict:
    """登记封顶：只丢最旧的（意图本身不会"过期"，所以不能按 TTL 丢）。读写两侧都裁，
    否则 claim 写回时又能多出一个，不变量守不住。"""
    if len(runs) <= STUDIO_INTENT_KEEP:
        return runs
    for old in sorted(runs, key=lambda k: runs[k].get("ts") or 0)[:len(runs) - STUDIO_INTENT_KEEP]:
        runs.pop(old, None)
    return runs


def studio_claim(session: str, run: str = "", port: int = STUDIO_PORT) -> dict:
    """登记"这个 run 属于这些会话"。同一个 run 再 claim = 加一个观看者；
    换 run claim 只影响那个 run 的名单（不会像旧版那样全局只增不减）。"""
    session, run = str(session or "").strip(), str(run or "").strip()
    if not session or not run:
        return {"error": "session 和 run 都不能为空", "runs": {}}
    rec = _studio_record()
    key = _norm_run(run) or run.lower()
    item = rec["runs"].get(key) or {}
    owners = _owners_of(item)
    if session not in owners:
        owners.append(session)
    rec["runs"][key] = {"sessions": owners, "session": owners[-1], "run": run,
                        "port": int(port), "ts": time.time()}
    rec["runs"] = _trim_runs(rec["runs"])
    with open(STUDIO_MARK, "w", encoding="utf-8") as f:
        json.dump({"runs": rec["runs"], "ts": time.time()}, f, ensure_ascii=False)
    set_intent(session, "hypit", run)     # 起 studio 本身就是"在做 hypit"，顺手把意图钉死（排他）
    return {"runs": rec["runs"], "session": session, "run": run, "port": int(port), "owners": owners}


def studio_release(session: str = "", run: str = "", all_sessions: bool = False) -> dict:
    """带 session=把这些会话从各 run 名单里摘掉（名单空了才删那个 run）；带 run=只动那个 run。

    🔴 整条作废必须显式 `all_sessions=True`（CLI 上写 `--all`）。之前"都不带=整条作废"是脚枪：
    漏传会话 id 裸跑一次 studio-release，就把所有会话的归属登记一次抹光（2026-10-02 实际发生过，
    .gateway_studio.json 整份消失）。释放是窄操作，默认不许扩到全局。
    """
    rec = _studio_record()
    if not session and not run:
        if not all_sessions:
            return {"error": "要说释放谁：studio-release <会话id> [run]；确实要清空全部才加 --all",
                    "owners": {k: _owners_of(v) for k, v in rec["runs"].items()}}
        studio_mark_clear_all()
        return {"released": True, "scope": "all"}
    only = _norm_run(run) if run else ""
    left = {}
    for key, item in rec["runs"].items():
        if only and key != only and not _run_matches(run, key):
            left[key] = item
            continue
        owners = [o for o in _owners_of(item) if o != str(session)] if session else _owners_of(item)
        if owners:
            left[key] = {**item, "sessions": owners, "session": owners[-1]}
    if left:
        with open(STUDIO_MARK, "w", encoding="utf-8") as f:
            json.dump({"runs": left, "ts": time.time()}, f, ensure_ascii=False)
        return {"remaining": {k: _owners_of(v) for k, v in left.items()}}
    studio_mark_clear_all()
    return {"released": True}


def studio_mark_clear_all() -> None:
    try:
        os.remove(STUDIO_MARK)
    except OSError:
        pass


def _studio_owner() -> dict:
    try:
        with open(STUDIO_MARK, encoding="utf-8") as f:
            rec = json.load(f)
        return rec if isinstance(rec, dict) else {}
    except (OSError, ValueError):
        return {}


def _studio_served_path(port: int) -> str:
    """问 studio 自己"你在服务哪个源文件"。⚠️ 只取 source.path，绝不外传 source.text（那是完整源码）。"""
    import urllib.error
    import urllib.request
    now = time.time()
    if _RUN_CACHE["port"] == port and now - _RUN_CACHE["ts"] < RUN_CACHE_TTL:
        return _RUN_CACHE["path"] or ""
    path = ""
    try:
        with urllib.request.urlopen("http://localhost:%d/__studio/session" % port, timeout=3) as r:
            data = json.loads(r.read().decode("utf-8", "replace") or "{}")
        path = str((data.get("source") or {}).get("path") or "")
    except (urllib.error.URLError, OSError, ValueError):
        path = ""          # 生成工程实测会回 500（异步腿进不了 transient 执行）⇒ 拿不到就不核对
    _RUN_CACHE.update(port=port, ts=now, path=path)
    return path


# ══ 选了就给界面：把这条会话的工程用 studio 查看器打开（不让人敲命令行）════════════
def _viewable(p: str) -> str:
    """同一个工程，能渲染的那份优先：有 `assemble.svrun` 就用它替掉 `build.svrun`。

    为什么必须替：生成工程里挂着异步腿，而 studio 的编辑期投影【按设计拒绝异步 capability】
    （@hypit/hypit/packages/runtime-host-node/src/index.ts:308）⇒ `/__studio/session` 必然 500、
    时间轴/产物/预览三页全空。结果就是用户看到的"视频都出来了导演台还不同步"
    （2026-10-03 实测：claim 钉在 productions/replica-final/build.svrun:58637，同目录已有 assemble.svrun）。
    claim 的语义是【这条会话 owns 哪个工程】，不是"必须打开哪个文件"，所以在这里换成可看形态是正解。
    """
    if not p:
        return ""
    ws = os.path.realpath(HYPIT_WORKSPACE)
    cand = p if os.path.isabs(p) else os.path.join(ws, str(p).replace("\\", "/"))
    d = cand if os.path.isdir(cand) else os.path.dirname(cand)
    a = os.path.join(d, "assemble.svrun")
    try:
        if os.path.isfile(a) and _inside(ws, a):
            # 返回形态跟着输入：claim 存的是相对路径就别突然变成绝对路径（presence 的 run 字段要能对比）
            return a if os.path.isabs(p) else os.path.relpath(a, ws).replace("\\", "/")
    except OSError:
        pass
    return p


def _session_project(sid: str) -> str:
    """这条会话该看哪个工程：本会话 claim → 意图任务里的路径 → 本会话最近生成腿所属工程目录。"""
    item = _mine(_studio_record()["runs"], sid)
    if item and item.get("run"):
        return _viewable(str(item["run"]))
    task = str((_intent_map().get(sid) or {}).get("task") or "")
    if task.lower().endswith((".svrun", ".svml")):
        return task
    # 传工程【目录】也要认（2026-10-03 实测坑：只认后缀 ⇒ 文档里写"可以传目录"是假的，
    # agent 老老实实给目录就被判 no-project）。_pick_svrun 自己会做工作区越界检查。
    if task and os.path.isdir(task):
        return task
    con = _db()
    try:
        row = con.execute("select run from jobs where session=? and run is not null and run<>''"
                          " order by created desc limit 1", (sid,)).fetchone()
    finally:
        con.close()
    return _viewable(str(row[0])) if row and row[0] else ""


def _pick_svrun(target: str) -> str:
    """把"工程目录 / .svml / .svrun"统一解析成一个真实存在的 .svrun 绝对路径；越界或找不到回 ""。"""
    import glob
    ws = os.path.realpath(HYPIT_WORKSPACE)
    cand = target if os.path.isabs(target) else os.path.join(ws, str(target).replace("\\", "/"))
    if not _inside(ws, cand):
        return ""
    real = os.path.realpath(cand)
    if real.endswith(".svrun"):
        return real if os.path.isfile(real) else ""
    if real.endswith(".svml"):
        swp = real[:-5] + ".svrun"
        if os.path.isfile(swp):
            return swp
        real = os.path.dirname(real)          # 同名 .svrun 不存在（实测 clone.svml 配 build.svrun）⇒ 退回同目录找
    if os.path.isdir(real):
        hits = sorted(glob.glob(os.path.join(real, "*.svrun")))
        if hits:
            return hits[0]
    return ""


def _free_studio_port(span: int = 12) -> int:
    """studio 实测绑 [::1]，所以按 IPv6 环回找空闲口；5179 被别的会话的服务占着就往上走，绝不抢。

    2026-10-03 实测：一台机器上跑了 11 个历史查看器 ⇒ 5179..5190 这 12 个口全占，本函数返回 0，
    结果是【新会话点了 skill 也起不了查看器】。所以窗口占满时改为向系统要一个空闲口
    （右栏 iframe 的地址由 presence 带 port，端口不固定不影响挂载）。
    """
    import socket
    for port in range(STUDIO_PORT, STUDIO_PORT + span):
        sk = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        try:
            sk.bind(("::1", port))
            return port
        except OSError:
            continue
        finally:
            sk.close()
    sk = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    try:
        sk.bind(("::1", 0))
        return int(sk.getsockname()[1])
    except OSError:
        return 0
    finally:
        sk.close()


def studio_up(session: str, run: str = "") -> dict:
    """给这条会话起（或复用）一个 studio 查看器，并把归属 claim 给它。

    为什么要它：用户点了 /hypit-pipeline，右栏就该出官方界面，而不是"引擎没在跑 + 一行命令行"。
    触发者 = agent 跑 `intent` 那一步（CLI 进程内），所以**不新增 HTTP 执行端点**——
    那是要鉴权的面（ADR-pipeline Gate0），能不加就不加。
    """
    sid = str(session or "").strip()
    if not sid:
        return {"started": False, "reason": "no-session"}
    target = run or _session_project(sid)
    svrun = _pick_svrun(target) if target else ""
    if not svrun:
        return {"started": False, "reason": "no-project", "tried": target,
                "detail": "这条会话名下还没找到 .svrun 工程（先按 skill 把工程写出来再起查看器）"}
    ws = os.path.realpath(HYPIT_WORKSPACE)
    rel = os.path.relpath(svrun, ws).replace("\\", "/")
    for key, item in _studio_record()["runs"].items():          # 已有查看器在服务这个 run ⇒ 复用
        port = int(item["port"])
        if not _run_matches(rel, key):
            continue
        if not _probe_studio(port).get("up"):
            continue
        # 🔴 "端口活着"不够，必须验它服务的就是这个工程：studio-claim 不带端口时会默认记 5179，
        #    而 5179 可能挂着另一台查看器（2026-10-03 实测：装配工程被复用到旧 e2e 工程的台上）。
        #    比的是【同名工程】不是同一路径：studio 自报 .svml，claim 记的是 .svrun，
        #    直接比字符串永远不等 ⇒ 每过冷却就再起一台新的（进程泄漏）。
        served = str(_studio_served_path(port) or "")
        want = os.path.realpath(svrun)
        if served:
            got = os.path.realpath(_pick_svrun(served) or served)
            if got != want:
                continue
        elif os.path.realpath(os.path.join(ws, str(item.get("run") or "").replace("\\", "/"))) != want:
            # 🔴 自报不出来（生成工程的 /__studio/session 会 500）时【不能】直接复用：
            #    2026-10-03 就是这个洞让装配目标复用到了一台正服务 build.svrun 的旧台上。
            #    拿不到自报就只能比登记表，对不上宁肯新起一台。
            continue
        studio_claim(sid, rel, port)
        return {"started": False, "reused": True, "port": port, "run": rel,
                "url": "http://localhost:%d/" % port}
    port = _free_studio_port()
    if not port:
        return {"started": False, "reason": "no-port", "detail": "%d..%d 全被占" % (STUDIO_PORT, STUDIO_PORT + 11)}
    if not os.path.isfile(HYPIT_ENTRY):
        return {"started": False, "reason": "no-hypit-entry", "detail": HYPIT_ENTRY}
    import shutil
    import subprocess
    node = shutil.which("node") or "node"
    log = open(os.path.join(ROOT, "_studio_%d.log" % port), "ab")
    try:
        subprocess.Popen([node, HYPIT_ENTRY, "studio", "--workspace", ws, "--run", svrun, "--port", str(port)],
                         cwd=ws, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as e:
        return {"started": False, "reason": "spawn-failed", "detail": "%s: %s" % (type(e).__name__, e)}
    finally:
        log.close()
    for _ in range(30):                                         # 最多等 15 秒
        time.sleep(0.5)
        _STUDIO_CACHE.update(port=0, ts=0.0)
        if _probe_studio(port).get("up"):
            studio_claim(sid, rel, port)
            return {"started": True, "port": port, "run": rel, "url": "http://localhost:%d/" % port}
    return {"started": False, "reason": "not-listening", "port": port,
            "detail": "起了但 15 秒没在 [::1]:%d 应答，看 %s_studio_%d.log" % (port, ROOT, port)}


def _probe_studio(port: int) -> dict:
    """探一次 studio 的只读口；结果缓存 5 秒——多个右栏窗口共用一份，免得各自每 4 秒去撞死端口。"""
    import urllib.error
    import urllib.request
    now = time.time()
    if _STUDIO_CACHE["port"] == port and now - _STUDIO_CACHE["ts"] < STUDIO_CACHE_TTL:
        return _STUDIO_CACHE["res"]
    url = "http://localhost:%d/__studio/library?section=tasks" % port
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            data = json.loads(r.read().decode("utf-8", "replace") or "{}")
        tasks = data.get("tasks") or []
        running = [t for t in tasks
                   if str(t.get("status", "")).lower() not in ("", "finished", "failed", "cancelled", "saved")]
        res = {"up": True, "tasks": len(tasks), "running": len(running),
               "artifacts": len(data.get("artifacts") or [])}
    except (urllib.error.URLError, OSError, ValueError) as e:
        res = {"up": False, "error": type(e).__name__}
    _STUDIO_CACHE.update(port=port, ts=now, res=res)
    return res


def _mine(rec_runs: dict, session: str) -> dict:
    """这个会话名下的登记项，取心跳最新的那个（一条会话可能先后看几个 run；一个 run 也可多人共看）。"""
    mine = [v for v in rec_runs.values() if str(session) in _owners_of(v)]
    return max(mine, key=lambda v: v["ts"]) if mine else {}


_viewer_tick: dict = {}
_viewer_busy: set = set()
VIEWER_TICK_COOLDOWN = 120.0


def viewer_tick(session: str) -> dict | None:
    """服务端替这条会话把 studio 查看器起起来：登记了 hypit + 名下有真工程 + 还没有查看器 ⇒ 起。

    为什么搬到 8100 这一侧：过去"选了就要看到界面"绑在 agent 跑 `intent` 那条 CLI 上，而 DSH 的
    文件沙箱让它写不了 D:\video-skill（2026-10-03 实测被拒）⇒ 触发动作压根发生不了。
    右栏每 5 秒轮询一次 presence，这本身才是"必然发生的动作"，绑在这儿 agent 一条命令都不用敲。
    带冷却：同一 sid 两分钟内只试一次，轮询不会把进程起爆。
    """
    sid = str(session or "").strip()
    if not sid or os.environ.get("HYPIT_NO_AUTOSTART") or intent_of(sid) != "hypit":
        return None
    item = _mine(_studio_record()["runs"], sid)
    # 🔴 "已经有 claim" 不等于 "台已经对了、还活着"。两个真实坑：
    #    ① claim 钉在 build.svrun 而同工程已有可渲染的 assemble.svrun ⇒ 该换台；
    #    ② 那台已经被杀/自己退了（端口空着）⇒ 该重起。过去两条都会被这个守卫挡掉，
    #    用户看到的就是"右栏说查看器没起，但没人去起"。
    if item and _viewable(str(item.get("run") or "")) == str(item.get("run") or "") \
            and _probe_studio(int(item.get("port") or 0)).get("up"):
        return None
    if not _session_project(sid):
        return None
    now = time.time()
    if now - float(_viewer_tick.get(sid) or 0) < VIEWER_TICK_COOLDOWN or sid in _viewer_busy:
        return None
    _viewer_tick[sid] = now
    _viewer_busy.add(sid)

    def _go():
        # 🔴 必须后台起：studio_up 里要等查看器应答（最长 15 秒），而判定口是 5 秒一轮的轮询，
        #    同步等会把右栏整条对账堵住（2026-10-03 实测第一次调用 6 秒超时）。
        #    失败也吃掉这次冷却：下一次冷却到期再试，不做"每 5 秒重试"的起爆器。
        try:
            studio_up(sid)
        except Exception:
            pass
        finally:
            _viewer_busy.discard(sid)

    threading.Thread(target=_go, daemon=True).start()
    return {"started": False, "reason": "spawning"}


def studio_status(session: str | None = None) -> dict:
    """右栏 presence 信号：DSH 插件在 dsh-app:// 源里 fetch，Vite 的 CORS 只放行白名单 origin，
    浏览器直连 5179 会被拦 ⇒ 由 8100 代探一次，回 ACAO:* 的紧凑状态（不泄露原文，只给计数）。

    🔴 触发器只有一个 = 显式意图登记（intent），2026-10-02 三次定稿：
      intent = 这条会话【显式登记过】hypit（`intent` 动词，或 studio-claim 自登记）⇒ 决定页签弹不弹
      up     = 引擎现在活着且服务的就是你那个 run                ⇒ 决定台里显示什么
    盘上的旧 claim 只是【显示用的痕迹】，不再当触发器——否则"跑过一次"就永久拥有页签，
    而且等于替用户猜意图（用户定：路由肯定会理解错，所以不猜）。
    所以 studio 没起时回 `{intent:true, up:false, reason:"studio-down"}`：页签照样在，
    里面告诉你引擎没跑、怎么起。不带 session 的请求只给运维视图，永不回 up。
    """
    rec = _studio_record(refresh_session=str(session) if session else "")
    runs = rec["runs"]
    if session is None:
        return {"up": False, "reason": "ops-view",
                "runs": [{"run": v["run"], "owners": _owners_of(v), "port": v["port"],
                          "age": round(time.time() - v["ts"])} for v in runs.values()],
                "seen": [{"session": k, "age": round(time.time() - v)}
                         for k, v in sorted(_SEEN_SESSIONS.items(), key=lambda kv: -kv[1])]}

    _SEEN_SESSIONS[str(session)] = time.time()
    if len(_SEEN_SESSIONS) > 30:
        for old in sorted(_SEEN_SESSIONS, key=_SEEN_SESSIONS.get)[:len(_SEEN_SESSIONS) - 30]:
            _SEEN_SESSIONS.pop(old, None)

    item = _mine(runs, str(session))
    ex = explicit_only(str(session), "hypit")
    if ex is False:                      # 明确登记了别的流水线 ⇒ 这个台不挂（哪怕以前 claim 过）
        return {"intent": False, "up": False, "reason": "intent-is-" + intent_of(str(session))}
    if ex is None:                       # 没显式登记 ⇒ 零 UI。旧 claim 只回给人看，不再弹台
        out = {"intent": False, "up": False, "reason": "no-intent",
               "owners": sorted({o for v in runs.values() for o in _owners_of(v)})}
        if item:
            out["stale_claim"] = item["run"]      # 历史遗留：想恢复就重新 intent 一次
        return out
    if not item:                         # 登记了 hypit 但没起 studio 查看器
        # 🔴 2026-10-03 更正：studio 只是【查看器】，真正出片的是 hypit runtime worker。
        #    账本里有在途腿就是"正在生成"，绝不能显示"引擎没在跑"（实测就在谎报）。
        sm = (jobs_progress(str(session)) or {}).get("summary") or {}
        act = int(sm.get("running") or 0) + int(sm.get("queued") or 0)
        if act:
            return {"intent": True, "up": False, "alive": False, "state": "generating",
                    "reason": "viewer-down-but-generating", "active_legs": act, "port": STUDIO_PORT}
        if not _session_project(str(session)):
            # 🔴 和"起了但连不上"分开说：这条会话还没有工程，查看器根本无从打开（2026-10-03 实测：
            #    过去两种情况都印成"studio 查看器没起"，人只能猜是哪一种）
            return {"intent": True, "up": False, "alive": False, "state": "no-project",
                    "reason": "no-project", "port": STUDIO_PORT,
                    "detail": "这条会话名下还没有 .svrun 工程，查看器无从打开（先按 skill 把工程写出来）"}
        return {"intent": True, "up": False, "alive": False, "state": "not-claimed",
                "reason": "not-claimed", "port": STUDIO_PORT,
                "how_to_start": "hypit studio --run <工程>.svrun --port %d" % STUDIO_PORT}

    # 🔴 同工程下有装配工程 ⇒ 该打开的是它，不是 claim 里那个 build.svrun（2026-10-03 实测：
    #    段都出来了导演台还钉在生成工程上 ⇒ 编辑期投影 500、时间轴永远不动，用户看到的就是"不同步"）。
    #    归属（run_key）不变，只换打开目标。这里【不能】直接报 up=true：旧端口服务的是另一个文件，
    #    报上去等于把 500 的台挂进 iframe。回 up=false + 明说原因，presence 的 viewer_tick 会按
    #    _session_project（已换成装配工程）起/复用对的台，并把 claim 挪过去。
    want = _viewable(str(item["run"]))
    if want != str(item["run"]):
        return {"intent": True, "up": False, "alive": True, "state": "not-claimed",
                "reason": "assemble-preferred", "port": int(item["port"]),
                "run": want, "stale_claim": item["run"], "session": item["session"],
                "detail": "同工程下有 assemble.svrun（可渲染的装配形态），查看器正在换过去；"
                          "生成工程的编辑期投影不出画面，所以台不会停在 build.svrun 上"}

    port = int(item["port"])
    base = {"intent": True, "port": port, "run": item["run"], "session": item["session"],
            "idle_age": round(time.time() - item["ts"])}
    probe = dict(_probe_studio(port))
    if not probe.get("up"):
        # 查看器死了 ≠ 引擎没工作：先看账本里有没有在途腿
        sm = (jobs_progress(str(session)) or {}).get("summary") or {}
        act = int(sm.get("running") or 0) + int(sm.get("queued") or 0)
        base.update(up=False, alive=False, reason="studio-down",
                    state="generating" if act else "viewer-down", active_legs=act,
                    how_to_start="hypit studio --run %s --port %d" % (item["run"], port))
        return base

    # 交叉核对：studio 自报它在服务哪个 run。拿到但不匹配 ⇒ 台里提示；拿不到（生成工程 session 会 500）⇒ 只认登记。
    served = _studio_served_path(port)
    if served and not _run_matches(item["run"], served):
        base.update(up=False, alive=True, reason="run-mismatch", served=served,
                    state="viewer-on-another-run")
        return base

    probe.update(base)
    probe.update(up=True, alive=True, state="viewer-up", run_verified=bool(served))
    return probe


# ══ 会话意图登记（铁律：新会话零 UI，明确意图之后才挂台）═══════════════════════════
# 之前只靠"碰巧有 claim / 碰巧有带标的 run"来推断意图 ⇒ 该弹的不弹（agent 忘了 claim）、
# 不该弹的乱弹（一条会话同时挂着两条流水线的痕迹）。现在把意图变成一个【显式动作】：
#   确认走哪条流水线之后，第一件事就是 set_intent(session, skill)。
# 规则（排他·intent-only，2026-10-02 三次定稿）：
#   有显式登记 ⇒ 只挂 skill 匹配的那个台，其余一律不挂
#   无显式登记 ⇒ 零 UI。盘上的旧 claim / 带标的 run 只是显示用的历史，永不触发页签
#                （它们不过期 ⇒ 会把一次旧任务变成这条会话的永久页签，那就是替用户猜意图）
# 纯提示词、知识库类 skill（seedance-* prompt 库、film-* 写作类）【不登记意图】：没有引擎，挂了也是空壳。
INTENT_MARK = os.environ.get("HYPIT_INTENT_MARK") or os.path.join(ROOT, ".gateway_intent.json")
# 🔴 工作区侧的意图登记（2026-10-03 实测）：DSH 自带 agent 跑在文件沙箱里，写 D:\video-skill 会被拒
#   （`sandbox escalation … is not strictly wider`），但它【一定】写得 own 工作区。
#   过去把"选了就要看到界面"绑在一条沙箱里跑不动的命令上 ⇒ 点了 skill 却什么都不弹。
#   所以意图允许落在 <工作区>/.dsh/intent.json，presence 两个源合并读（同 sid 取 ts 更新的那个）。
WORKSPACE_INTENT = os.environ.get("HYPIT_WORKSPACE_INTENT") or os.path.join(
    os.environ.get("DSH_WORKSPACE") or r"C:\dsh", ".dsh", "intent.json")
KNOWN_SKILLS = ("omni", "hypit", "super-clone", "product-video", "film")
# 🔴 意图有效期（2026-10-03）：过期 ⇒ 不再弹台，也不再被 viewer_tick 自动起查看器。
#    为什么必须有：判定口现在会顺手起进程，一条几天前的登记 = 只要那个会话被点开一次就再起一个 studio。
#    注意这不是"被别的台挤掉"（那条禁令仍然成立：意图只能被显式覆盖/清除），是自己到期。
INTENT_TTL_SEC = float(os.environ.get("HYPIT_INTENT_TTL") or 24 * 3600)
_intent_cache = {"sig": None, "data": {}}


def _live_only(d: dict) -> dict:
    """滤掉过期条目。坏记录（没 ts / ts 不是数）当过期，宁可少弹不误起进程。"""
    now = time.time()
    out = {}
    for sid, rec in (d or {}).items():
        try:
            if now - float(rec.get("ts") or 0) <= INTENT_TTL_SEC:
                out[sid] = rec
        except (AttributeError, TypeError, ValueError):
            continue
    return out


def _read_mark(path: str) -> tuple:
    """读一份登记文件 → (mtime_ns 或 None, {sid: {skill,task,ts}})。坏文件/没文件都当空，不抛。"""
    try:
        sig = os.stat(path).st_mtime_ns
    except OSError:
        return None, {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return sig, {}
    return sig, (data if isinstance(data, dict) else {})


def _intent_map() -> dict:
    def _mt(p: str):
        try:
            return os.stat(p).st_mtime_ns
        except OSError:
            return None

    sig = (_mt(INTENT_MARK), _mt(WORKSPACE_INTENT))     # 🔴 必须是 mtime：常驻进程靠它感知"agent 刚写了登记"
    if _intent_cache["sig"] != sig:
        _, main = _read_mark(INTENT_MARK)
        _, side = _read_mark(WORKSPACE_INTENT)
        data = dict(main)
        for sid, rec in side.items():          # 工作区侧同 sid 覆盖：agent 刚点的那一下必须立刻生效
            if not isinstance(rec, dict) or not str(rec.get("skill") or ""):
                continue
            old = data.get(sid)
            if isinstance(old, dict) and float(old.get("ts") or 0) > float(rec.get("ts") or 0):
                continue
            data[sid] = rec
        _intent_cache.update(sig=sig, data=data)
    # 过期过滤每次现算（不能缓存过滤结果：文件没动，但时间在走，到点就该不弹）
    return _live_only(_intent_cache["data"])


def _replace_retry(src: str, dst: str, tries: int = 4) -> bool:
    """`os.replace` 在 Windows 上会被杀软/索引器临时占用目标文件撞到 WinError 5（实测偶发）。
    这份登记决定右栏台弹不弹 ⇒ 撞锁不能静默丢登记，重试几次；最终失败让调用方看见 error。
    """
    for i in range(tries):
        try:
            os.replace(src, dst)
            return True
        except OSError:
            if i == tries - 1:
                return False
            time.sleep(0.05 * (i + 1))
    return False


def set_intent(session: str, skill: str, task: str = "") -> dict:
    """登记"这条会话现在做哪条流水线"。同一会话再登记 = 覆盖（换产品线就靠它）。"""
    session, skill = str(session or "").strip(), str(skill or "").strip()
    if not session or not skill:
        return {"error": "session 和 skill 都不能为空"}
    data = dict(_intent_map())
    data[session] = {"skill": skill, "task": str(task or ""), "ts": time.time()}
    tmp = INTENT_MARK + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    if not _replace_retry(tmp, INTENT_MARK):
        try:
            os.remove(tmp)
        except OSError:
            pass
        return {"error": "意图登记写入失败（目标文件被占用，已重试）：右栏台可能不弹，重试一次即可",
                "session": session, "skill": skill}
    _intent_cache.update(sig=None, data=data)
    return {"session": session, "skill": skill, "task": task, "known": skill in KNOWN_SKILLS}


def intent_of(session: str) -> str:
    return str((( _intent_map().get(str(session or "")) or {}).get("skill")) or "")


def clear_intent(session: str) -> dict:
    data = dict(_intent_map())
    gone = data.pop(str(session or ""), None)
    tmp = INTENT_MARK + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, INTENT_MARK)
    _intent_cache.update(sig=None, data=data)
    return {"cleared": bool(gone), "remaining": len(data)}


def intent_view() -> list:
    """运维视图：现在有哪些会话挂着哪条流水线。"""
    now = time.time()
    return [{"session": k, "skill": str(v.get("skill") or ""), "task": str(v.get("task") or ""),
             "age": round(now - float(v.get("ts") or 0))}
            for k, v in sorted(_intent_map().items(), key=lambda kv: -(kv[1].get("ts") or 0))]


def explicit_only(session: str, skill: str) -> bool | None:
    """True/False = 有显式登记、按它判；None = 没有登记 ⇒ 一律不挂台（不再退到佐证）。"""
    cur = intent_of(session)
    return None if not cur else (cur == skill)


# ══ 生成腿进度（进度台的数据面：账本永久、0 分、不依赖 studio 起没起）════════════════
_PROGRESS_CACHE = {"sid": None, "ts": 0.0, "res": {}}


def jobs_progress(session: str, limit: int = 40) -> dict:
    """按会话把 hypit 生成腿从网关账本里聚出来。

    归属是【数据面盖章】：provider 提交时带 `need`（内含工程源文件绝对路径）与 `$HYPIT_SESSION_ID`。
    🔴 这两个字段只用于显示分组；鉴权与扣费依据仍是 token + 服务端路由，自报的一概不信（见 _project_key）。
    """
    sid = str(session or "")
    if not sid:
        return {"known": False, "reason": "no-session"}
    now = time.time()
    if _PROGRESS_CACHE["sid"] == sid and now - _PROGRESS_CACHE["ts"] < 5:   # 5 秒：状态要够新鲜
        return _PROGRESS_CACHE["res"]
    # 归属回落链（都不新增"必须有人做"的动作）：① 腿自己带的 session（agent 起 build 时导出了 HYPIT_SESSION_ID）
    # ② 该会话 claim 过的工程（studio-claim 会带会话）③ 该会话登记的工程名。都没有才算"没腿"。
    keys = []
    mine = _mine(_studio_record()["runs"], sid)          # 只认【本会话】claim 过的工程
    if mine:
        keys.append(_project_key({"project": mine.get("run") or ""}))
    intent_task = str((_intent_map().get(sid) or {}).get("task") or "")
    k = _project_key({"project": intent_task})           # 登记意图时把工程目录写在任务名里也能对上
    if k and k not in keys:
        keys.append(k)
    con = _db()
    try:
        cols = "id,model,state,run,label,credits,error,created,updated,output,clip"
        rows = con.execute(f"select {cols} from jobs where session=? order by created desc limit ?",
                           (sid, limit)).fetchall()
        how = "session"
        if not rows and keys:
            marks = ",".join("?" * len(keys))
            rows = con.execute(f"select {cols} from jobs"
                               f" where run in ({marks}) order by created desc limit ?", (*keys, limit)).fetchall()
            how = "project" if rows else "session"
    finally:
        con.close()
    if not rows:
        res = {"known": False, "reason": "no-jobs"}
    else:
        seg, tally, spent = [], {"ok": 0, "failed": 0, "running": 0, "queued": 0}, 0
        for jid, model, state, run, label, credits, err, created, updated, out, clip in rows:
            msg = up = ""
            if err:
                try:
                    msg = str(json.loads(err).get("message") or "")[:220]
                except ValueError:
                    msg = str(err)[:220]
                i = msg.find("上游任务")
                if i >= 0:
                    up = msg[i:].split("，")[0].split(" ")[-1][:64]
            tally["ok" if state == "succeeded" else
                 "failed" if state == "failed" else
                 "running" if state == "running" else "queued"] += 1
            spent += credits or 0
            seg.append({"id": jid, "model": model, "state": state, "project": run or "",
                        "label": label or "", "credits": credits or 0,
                        # 🔴 成功的腿必须带成片地址：右栏"看片"、agent 回传贴图都靠它，
                        #    否则进度台只有计数，人得自己去翻账本（用户 2026-10-03 点名过）
                        "output": (out or "") if state == "succeeded" else "",
                        # 回传进工程的相对路径（装配工程就靠它拼时间轴；没回传成功就是空串）
                        "clip": (clip or "") if state == "succeeded" else "",
                        "seconds": int(max(0.0, (updated or created) - (created or 0))),
                        "ago": int(time.time() - (created or 0)), "error": msg, "upstream": up})
        res = {"known": True, "segments": seg, "matched_by": how,
               "summary": {"total": len(seg), "credits": spent, **tally},
               "projects": sorted({x["project"] for x in seg if x["project"]}),
               "source": "gateway-jobs"}
    _PROGRESS_CACHE.update(sid=sid, ts=now, res=res)
    return res


def workbench_presence(session: str | None = None) -> dict:
    """右栏唯一的判定口：这条会话该挂哪个台、那个台活着没。

    规则（2026-10-02 定稿·intent-only）：
      intent（显式登记）决定**页签弹不弹**；up（引擎活着）决定**台里显示什么**。
      没登记 ⇒ 什么都不弹，不看盘上痕迹。
    omni 的意图也走这里，不再让插件直连 8765：video-ui 没有 CORS 头，跨源 fetch 读不到响应。
    """
    if session:
        # 🔴 起查看器绑在轮询上（右栏每 5 秒一次，本身必然发生），而不是绑在"agent 跑一条 CLI"上——
        #    那条命令在 DSH 沙箱里写不了 D 盘，实测发生不了。viewer_tick 自带 120s 冷却。
        viewer_tick(str(session))
    out = {"omni": omni_status(session), "hypit": studio_status(session),
           "film": film_status(session)}
    if session:
        out["hypit"]["progress"] = jobs_progress(str(session))   # 账本里有腿就出进度，与 studio 无关
    return out


def film_status(session: str | None = None) -> dict:
    """film 全流程看板台（8100 同域静态页）的意图与存活。

    短剧带货/影视类 skill（film 档）登记意图后，右栏弹这个台——它不是"引擎活不活"，
    而是"这条会话的 productions/<task>/ 全流程看板活不活"。8100 自己托管看板页，
    所以 up = 8100 自己活着（恒 True，只要判定口回话了就在）；intent 决定弹不弹。
    看板读的是项目文件（01-08 各阶段产物 + 分镜图 + 视频 + 成片），输入→中间→输出全串起来。
    """
    if session is None:
        return {"intent": False, "up": True, "alive": True, "reason": "ops-view",
                "url": "http://127.0.0.1:8100/api/film/board"}
    sid = str(session)
    ex = explicit_only(sid, "film")
    if ex is False:
        return {"intent": False, "up": True, "alive": True, "reason": "intent-is-" + intent_of(sid)}
    if ex is not True:
        return {"intent": False, "up": True, "alive": True, "reason": "no-intent"}
    # intent 登记了 film ⇒ 挂台；url 指 8100 看板页（task 由看板页自己扫最近项目，带 ?task= 可钉死）
    task = str(((_intent_map().get(sid) or {}).get("task")) or "")
    url = "http://127.0.0.1:8100/api/film/board" + ("?task=" + task if task else "")
    return {"intent": True, "up": True, "alive": True, "url": url, "task": task,
            "source": "explicit"}


def omni_status(session: str | None = None) -> dict:
    """omni 流水线台（video-ui :8765）的意图与存活。

    🔴 触发器只有一个 = 显式登记成 omni。盘上"带本会话标的 run"从此**只用于台里显示**，
    不再当触发器——它永不过期，会把 67 分钟前跑过的一条 run 变成这条会话的永久页签，
    等于替用户猜意图（2026-10-02 用户定：不猜，以显式调用为准）。
    """
    alive = _probe_omni()
    if session is None:
        return {"intent": False, "up": False, "alive": alive, "reason": "ops-view",
                "intents": intent_view()}
    sid = str(session)
    ex = explicit_only(sid, "omni")
    if ex is False:                      # 明确登记了别的流水线 ⇒ 这个台不挂
        return {"intent": False, "up": False, "alive": alive, "reason": "intent-is-" + intent_of(sid)}
    if ex is not True:                   # 没登记 ⇒ 零 UI，不看痕迹
        return {"intent": False, "up": False, "alive": alive, "reason": "no-intent"}
    found = _runs_of_session(sid)        # 只用来告诉人"这个会话最近那条 run 是哪个"
    newest = max(found, key=lambda r: r["ts"]) if found else {}
    return {"intent": True, "up": alive, "alive": alive,
            "url": OMNI_UI + "?session=" + sid, "run": newest.get("run", ""),
            "ready": bool(newest.get("script_approved")),
            "idle_age": round(time.time() - newest["ts"]) if newest else None,
            "source": "explicit"}


def _probe_omni() -> bool:
    """video-ui 活没活（缓存 5 秒，别每个窗口每 4 秒撞一次）。"""
    now = time.time()
    if now - _OMNI_CACHE["ts"] < STUDIO_CACHE_TTL:
        return _OMNI_CACHE["alive"]
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(OMNI_UI + "api/runs", timeout=2) as r:
            r.read(64)
            alive = r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        alive = False
    _OMNI_CACHE.update(ts=now, alive=alive)
    return alive


def _runs_of_session(sid: str) -> list:
    """扫 video_runs 找属于这条会话的 run（意图来自盘上的资源，不靠谁登记）。"""
    import glob
    now = time.time()
    if now - _RUNS_CACHE["ts"] < OMNI_RUNS_TTL:
        return [r for r in _RUNS_CACHE["rows"] if r["session"] == sid]
    rows = []
    try:
        files = glob.glob(os.path.join(VIDEO_RUNS, "*", "run.json"))
    except OSError:
        files = []
    for f in files:
        try:
            with open(f, encoding="utf-8") as fh:
                st = json.load(fh)
            sess = str(st.get("session") or "")
            if not sess:
                continue
            review = st.get("review") or {}
            rows.append({"session": sess,
                         "run": os.path.basename(os.path.dirname(f)),
                         "ts": os.path.getmtime(f),
                         "script_approved": bool(review.get("script_approved"))})
        except (OSError, ValueError):
            continue
    _RUNS_CACHE.update(ts=now, rows=rows)
    return [r for r in rows if r["session"] == sid]


def token_required():
    """鉴权开关：.env 的 MODEL_GATEWAY_TOKEN 非空即要求 Bearer 匹配（空=保持现状，仅本机）。"""
    from config import settings
    return str(getattr(settings, "MODEL_GATEWAY_TOKEN", "") or "")


def authorized(header_value: str | None) -> bool:
    need = token_required()
    if not need:
        return True
    return bool(header_value) and header_value.strip().removeprefix("Bearer ").strip() == need


_DB_MIGRATED = {"ok": False}


def _db():
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, model TEXT, state TEXT,"
                " route TEXT, credits INTEGER DEFAULT 0, output TEXT, error TEXT, request TEXT,"
                " created REAL, updated REAL)")
    if not _DB_MIGRATED["ok"]:
        have = {r[1] for r in con.execute("PRAGMA table_info(jobs)")}
        for col in ("run", "label", "session", "clip"):
            if col not in have:
                con.execute(f"ALTER TABLE jobs ADD COLUMN {col} TEXT")
        _DB_MIGRATED["ok"] = True
    con.commit()
    return con


def _project_key(body: dict) -> str:
    """把 hypit provider 自报的工程标识归一成 `productions/<工程>` 这种工作区相对目录。

    🔴 只用于**显示归属**（进度台按工程分组），绝不用作鉴权/扣费依据——依据只有 token 和服务端路由。
    自报路径不可信：归一后不在工作区里面的，一律当"没报"，不入库、不展示。
    两种来源都接：`project`（源文件路径，可能绝对）和 `need`（`need:author:<路径>::component::final:请求名`，
    路径可能被 %5C 转义）。
    """
    import urllib.parse
    ws = os.path.abspath(HYPIT_WORKSPACE)
    raw = str(body.get("project") or "")
    if not raw:
        # need id 整串是百分号编码的（`::` 编码成 %3A%3A），必须先解码再切分
        need = urllib.parse.unquote(str(body.get("need") or ""))
        if need.startswith("need:"):
            head = need[len("need:"):].split("::", 1)[0]
            raw = head[len("author:"):] if head.startswith("author:") else head
    if not raw:
        return ""
    raw = urllib.parse.unquote(raw.replace("\\", "/"))        # 反斜杠 → 斜杠
    cand = raw if os.path.isabs(raw) else os.path.join(ws, raw)
    try:
        real = os.path.realpath(cand)
        rel = os.path.relpath(real if os.path.isdir(real) else os.path.dirname(real), ws)
    except (ValueError, OSError):
        return ""
    rel = rel.replace("\\", "/").strip("/")
    if rel.startswith("..") or not rel.startswith("productions/"):
        return ""
    return "/".join(rel.split("/")[:2])          # productions/<工程目录>


def _need_label(body: dict) -> str:
    """need id 尾巴当段名（`…:request-audio-render` → `request-audio-render`），给进度台一行一个标签。"""
    need = str(body.get("need") or "")
    return need.rsplit(":", 1)[-1][:60] if need else ""


def _touch(jid, **fields):
    con = _db()
    cols = ", ".join(f"{k}=?" for k in fields)
    con.execute(f"UPDATE jobs SET {cols}, updated=? WHERE id=?", (*fields.values(), time.time(), jid))
    con.commit()
    con.close()


def _job(jid):
    con = _db()
    row = con.execute("SELECT id,model,state,route,credits,output,error FROM jobs WHERE id=?", (jid,)).fetchone()
    con.close()
    if not row:
        return None
    return {"id": row[0], "model": row[1], "status": row[2], "route": row[3],
            "credits": row[4] or 0, "output": row[5], "error": row[6]}


def ext_for(media_type: str) -> str:
    return {"image/png": ".png", "image/webp": ".webp", "image/jpeg": ".jpg",
            "video/mp4": ".mp4", "audio/mpeg": ".mp3", "audio/wav": ".wav"}.get(media_type, ".bin")


def _imgbb_url(path: str) -> str | None:
    """传图床拿公网 URL：vinted/zexapi 这类上游要能自己抓取，loopback 地址它们取不到。"""
    from config import settings
    key = str(getattr(settings, "IMGBB_API_KEY", "") or "")
    if not key:
        return None
    import urllib.error
    import urllib.parse
    import urllib.request
    b64 = base64.b64encode(open(path, "rb").read()).decode()
    data = urllib.parse.urlencode({"key": key, "image": b64, "name": os.path.basename(path)}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request("https://api.imgbb.com/1/upload", data=data), timeout=60) as r:
            return str(json.loads(r.read().decode())["data"]["url"])
    except Exception:
        return None


def handle_upload(body: bytes, media_type: str) -> dict:
    """POST /uploads → {url}。落本地 + 尽量换公网 URL（图），换不到就退回本机地址并让 supports 去拒。"""
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    name = f"{uuid.uuid4().hex}{ext_for(media_type)}"
    path = os.path.join(UPLOAD_DIR, name)
    with open(path, "wb") as f:
        f.write(body)
    url = _imgbb_url(path) if media_type.startswith("image/") else None
    return {"url": url or f"{PUBLIC_BASE}/api/hypit/uploads/files/{name}", "local": path,
            "public": bool(url)}


def serve_upload(name: str):
    path = os.path.normpath(os.path.join(UPLOAD_DIR, os.path.basename(name)))
    if not path.startswith(os.path.normpath(UPLOAD_DIR)) or not os.path.isfile(path):
        return None, None
    data = open(path, "rb").read()
    return data, ("image/png" if path.endswith(".png") else
                  "image/jpeg" if path.endswith(".jpg") else
                  "image/webp" if path.endswith(".webp") else "application/octet-stream")


def check_support(body: dict) -> tuple:
    """能带就收、不能带就明说（hypit 的 plan 会把这句话原样给用户看）。"""
    model = str(body.get("model") or "")
    if model not in HYPIT_MODELS:
        return False, f"本服务不提供模型 {model or '(空)'}；可选: {', '.join(sorted(HYPIT_MODELS))}"
    try:
        dur = float(body.get("duration") or 5)
    except (TypeError, ValueError):
        return False, "duration 必须是数字"
    spec = _lane_spec(model)
    if dur > MAX_SECONDS:
        return False, f"本服务最长渲染 {MAX_SECONDS} 秒，不是 {dur:g} 秒"
    if dur not in spec["seconds"]:
        return False, ("%s 实测只有 %s 秒能出片（%g 秒没数据，不拿去烧钱%s）：要扩时长先在 plan/pricing 上验过再改 LANE_SPECS" % (
            model, "/".join(("%g" % x) for x in spec["seconds"]), dur,
            "；seedance 路的 4/6/8 秒都被上游 422 打回过，报的还是误导性的「请填写提示词」"
            if model.startswith("seedance") else ""))
    res = body.get("resolution")
    if res is None or str(res) == "":
        res = spec["resolutions"][0] if spec["resolutions"] else ""
    if spec["resolutions"]:
        if str(res) not in spec["resolutions"]:
            return False, "%s 只出 %s，不出 %s" % (model, "/".join(spec["resolutions"]), res)
    elif res:
        return False, ("%s 这条路由兑现不了 \"%s\" 档位标注（RunningHub 导演台实测只出 576×1024、5 秒）："
                       "别写 resolution 就能跑，要接 768P/2K 先把工作流出幅跑实测") % (model, res)
    ratio = body.get("ratio")
    if spec["aspects"] is not None and ratio not in (None, "") and str(ratio) not in spec["aspects"]:
        return False, "%s 的出幅固定 %s，aspect-ratio 要么不写要么写它（不是 %s）" % (
            model, "/".join(spec["aspects"]), ratio)
    if not spec["last_frame"] and body.get("lastFrame"):
        return False, "%s 不接尾帧（这条工作流没实测过首/尾帧形态）" % model
    for field, kind in (("videoReferences", "video"), ("audioReferences", "audio")):
        if body.get(field) and kind not in spec["refs"]:
            return False, ("%s 只带得动 %s 参考，不接 %s 参考（不静默丢弃；要接就把 %s 参考接进这条路由的网关适配器）"
                           % (model, "/".join(spec["refs"]), kind, kind))
    refs = body.get("references") or []
    if not isinstance(refs, list):
        return False, "references 必须是数组"
    for item in refs:
        u = item.get("url") if isinstance(item, dict) else item
        if not str(u or "").startswith("http"):
            return False, "references 里每项都要带可抓取 url"
    return True, ""


def reconcile_orphans() -> int:
    """服务启动时调用：本服务单进程，队列里还挂着 queued/running 的行必然是上个进程留下的孤儿
    （线程已随进程消失，hypit 会一直 waiting 等不到结果）。2026-10-01 重启 8100 时真的打断了一条在跑的 build。"""
    con = _db()
    cur = con.execute(
        "UPDATE jobs SET state='failed', updated=?, error=? WHERE state IN ('queued','running')",
        (time.time(), json.dumps({"code": "GATEWAY_RESTARTED",
                                  "message": "网关重启时这条还在生成，执行线程已丢失、无法续查上游任务；请重跑这次 build"},
                                 ensure_ascii=False)))
    con.commit()
    n = cur.rowcount
    con.close()
    return n


def holds_pending(member: str = "") -> list:
    """列出还没结算的预扣（重启打断的腿会留这种行：线程死了没人 commit/rollback ⇒ 那笔分永久冻结）。

    🔴 故意**不在启动时自动回滚**：8100 的重启杀不掉 8765/别的进程在途的调用，
    按启发式回滚会把别人正在花的分退回去、账上却已经生成成功。所以这里只列，回收要指名道姓。
    """
    try:
        from backend import gateway
    except ImportError:                                    # pragma: no cover - 脚本式运行
        import gateway
    led = gateway.ledger()
    con = led._con()
    try:
        sql = "SELECT hold_id,member,amount,route,est FROM holds WHERE status='pending'"
        rows = con.execute(sql + (" AND member=?" if member else ""), ((member,) if member else ())).fetchall()
    finally:
        con.close()
    return [{"hold_id": r[0], "member": r[1], "amount": r[2], "route": r[3], "est": r[4]} for r in rows]


def holds_rollback(hold_id: str) -> dict:
    """显式回滚一笔孤儿预扣（走 Ledger.rollback，退款流水一起写，不裸改库）。"""
    try:
        from backend import gateway
    except ImportError:                                    # pragma: no cover
        import gateway
    return gateway.ledger().rollback(str(hold_id or "").strip())


def _normalize(body: dict) -> dict:
    """hypit 的 POST /videos 见过两种形状：字段平铺，以及 {"model":..,"input":{字段},"account":..}。
    只认平铺过一种 ⇒ 另一种 prompt 为空、上游回 422「请填写提示词」（2026-10-01 实跑踩过）。这里统一摊平。"""
    out = dict(body or {})
    for key in ("input", "params"):
        inner = out.pop(key, None)
        if isinstance(inner, dict):
            for k, v in inner.items():
                out.setdefault(str(k), v)
    return out


def _local_upload(url: str) -> str:
    """我们 /uploads 发出去的回环地址（/api/hypit/uploads/files/<name>）⇒ 直接指回磁盘上那份。
    视频动辄几 MB，绕本机一圈再下载回来既慢又会把扩展名猜错（content-type 回的是 octet-stream）。"""
    if "/api/hypit/uploads/files/" not in url:
        return ""
    path = os.path.normpath(os.path.join(UPLOAD_DIR, os.path.basename(url.split("?")[0])))
    return path if path.startswith(os.path.normpath(UPLOAD_DIR)) and os.path.isfile(path) else ""


def _media_urls(body: dict, fields: tuple) -> list:
    out = []
    for f in fields:
        v = body.get(f)
        if isinstance(v, list):
            out += [str((x.get("url") if isinstance(x, dict) else x)) for x in v if x]
        elif v:
            out.append(str(v))
    return out


def _leg_name(body: dict) -> str:
    """这条腿的组件名：need id 形如 `…clone.svml::component::seg1-video:generate` ⇒ 取回 `seg1`。

    段必须按【leg】命名，不能按完成先后：腿会乱序完成、也会单独重跑（重跑要覆盖同一段，
    而不是多出一个 seg9）。取不到 leg 名就不回传（宁缺勿错，绝不瞎起名）。
    """
    import urllib.parse
    need = urllib.parse.unquote(str(body.get("need") or ""))
    parts = [p for p in need.split("::") if p]
    if len(parts) < 2:
        return ""
    comp = parts[-1].split(":")[0].strip()
    for suffix in ("-video", "-audio", "-image", "-clip"):
        if comp.endswith(suffix):
            comp = comp[:-len(suffix)]
            break
    ok = comp and all(c.isalnum() or c in "_.-" for c in comp)
    return comp[:40] if ok else ""


def _land_clip(body: dict, src: str) -> str:
    """把成功的段回传进工程的 clips/<leg>.mp4。

    为什么必须回传进工程：hypit 编辑期投影【不执行异步腿】（runtime-host-node/index.ts:308），
    所以生成工程里的导演台永远看不到段；只有把段落成素材、由装配工程引用，时间轴才会出现片段。
    🔴 回传失败绝不能把已花积分的腿判死：一律吞掉异常，只回 ""。
    """
    import shutil
    try:
        proj = str(_project_key(body) or "")
        leg = _leg_name(body)
        if not proj or not leg or not src or not os.path.isfile(src):
            return ""
        ws = os.path.realpath(HYPIT_WORKSPACE)
        dst_dir = os.path.realpath(os.path.join(ws, proj.replace("\\", "/"), "clips"))
        if not dst_dir.startswith(ws + os.sep):
            return ""
        os.makedirs(dst_dir, exist_ok=True)
        dst = os.path.join(dst_dir, f"{leg}.mp4")
        shutil.copyfile(src, dst)
        return f"{proj}/clips/{leg}.mp4"
    except OSError:
        return ""


def _materialize_refs(body: dict, jid: str,
                      fields: tuple = ("references", "firstFrame", "lastFrame")) -> list:
    """把参考媒体 url 换成本地文件路径：自家回环地址→直接指回磁盘那份；真公网 url→下载到
    OUT_DIR/<jid>_refs/；本来就是本地路径→原样保留。
    H3 导演台只吃本地文件（提交时上传拿 fileName），所以这一步是它与 seedance 路的分水岭。"""
    import urllib.request
    urls = _media_urls(body, fields)
    paths = []
    for u in urls:
        # 已经是本地文件路径（罕见，比如 future 本地直传）⇒ 直接用
        if not u.startswith("http"):
            paths.append(u)
            continue
        mine = _local_upload(u)
        if mine:
            paths.append(mine)
            continue
        refs_dir = os.path.join(OUT_DIR, f"{jid}_refs")
        os.makedirs(refs_dir, exist_ok=True)
        try:
            resp = urllib.request.urlopen(u, timeout=120)
            raw = resp.read()
            ct = (resp.headers.get("content-type") or "").lower()
            ext = (".png" if "png" in ct else ".webp" if "webp" in ct else
                   ".jpg" if "jpeg" in ct or "jpg" in ct else
                   ".mp4" if "mp4" in ct or "video" in ct else
                   ".wav" if "wav" in ct else ".mp3" if "mpeg" in ct else ".bin")
            p = os.path.join(refs_dir, f"ref{len(paths)+1}{ext}")
            with open(p, "wb") as f:
                f.write(raw)
            paths.append(p)
        except Exception as e:
            # 下载失败就记原 url，让 H3 上游报 400（不静默丢）
            paths.append(u)
    return paths


def _run(jid: str, body: dict):
    cap, purpose = HYPIT_MODELS[str(body.get("model"))]
    params = {"prompt": str(body.get("prompt") or ""),
              "duration": int(float(body.get("duration") or 5)),
              "aspect_ratio": str(body.get("ratio") or "9:16"),
              "resolution": str(body.get("resolution") or RESOLUTIONS[0])}
    urls = [str((r.get("url") if isinstance(r, dict) else r)) for r in (body.get("references") or [])]
    for extra in ("firstFrame", "lastFrame"):
        if body.get(extra):
            urls.append(str(body[extra]))
    if urls:
        params["image_urls"] = urls
    os.makedirs(OUT_DIR, exist_ok=True)
    params["output_dir"] = params["output_path"] = os.path.join(OUT_DIR, f"{jid}")
    # H3 腿走本地文件路径（RunningHub 自家上传拿 fileName），不走公网 URL：图片=换主体、视频=出动作
    if purpose == "h3":
        local_paths = _materialize_refs(body, jid)
        vids = _materialize_refs(body, jid, ("videoReferences",))
        if local_paths:
            params["image_paths"] = local_paths
        if vids:
            params["video_paths"] = vids
        params.pop("image_urls", None)
    _touch(jid, state="running")
    from backend import gateway
    env, attempts = None, 0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        attempts = attempt
        try:
            env = gateway.invoke(cap, purpose, str(body.get("account") or "hypit"), params)
        except Exception as e:      # invoke 内部已兜异常；这里只防 resolve 段抛 RouteError
            env = {"ok": False, "result": None, "error": "%s: %s" % (type(e).__name__, e), "_exc": True}
        if env.get("ok"):
            break
        # 上游 503 原文就是"请稍后使用同样的请求重试"（参考图下载繁忙）；生成超时它会退积分 ⇒ 重试不重复收钱
        why = str(env.get("error") or "")
        if attempt == MAX_ATTEMPTS or not _retryable(why):
            break
        time.sleep(RETRY_WAITS[min(attempt - 1, len(RETRY_WAITS) - 1)])
    res = env.get("result") or {}
    credits = (env.get("credits") or {}).get("charged") or 0
    if not env.get("ok"):
        msg = str(env.get("error") or res.get("error") or "生成失败")[:340]
        if isinstance(res, dict) and res.get("job_id"):
            msg += "（上游任务 %s，可据此查它到底出没出片）" % res["job_id"]
        if attempts > 1:
            msg += "（已重试 %d 次）" % (attempts - 1)
        elif any(k in msg for k in NON_RETRYABLE):
            msg += "（内容/身份策略拒绝：不重试，要换参考图或换路由）"
        _touch(jid, state="failed", credits=credits,
               error=json.dumps({"code": "GATEWAY_ERROR" if env.get("_exc") else "GATEWAY_FAILED",
                                 "message": msg}, ensure_ascii=False))
        return
    path = res.get("path") or ""
    name = ""
    if path and os.path.isfile(path):
        name = f"{jid}{os.path.splitext(path)[1] or '.mp4'}"
        try:
            os.replace(path, os.path.join(OUT_DIR, name))
        except OSError:
            name = ""
    if not name and not res.get("url"):
        _touch(jid, state="failed", credits=credits,
               error=json.dumps({"code": "NO_OUTPUT", "message": "上游报成功但既无文件也无 URL"}, ensure_ascii=False))
        return
    output = f"{PUBLIC_BASE}/api/hypit/assets/{name}" if name else str(res.get("url"))
    clip = _land_clip(body, os.path.join(OUT_DIR, name)) if name else ""
    _touch(jid, state="succeeded", credits=credits, output=output, clip=clip)


def handle_submit(body: dict) -> tuple:
    body = _normalize(body if isinstance(body, dict) else {})   # 两种形状都接（见 _normalize）
    ok, reason = check_support(body)
    if not ok:
        return 400, {"error": {"code": "UNSUPPORTED_INPUT", "message": reason}}
    jid = uuid.uuid4().hex[:16]
    model = str(body.get("model"))
    con = _db()
    con.execute("INSERT INTO jobs(id,model,state,route,created,updated,request,run,label,session)"
                " VALUES(?,?,'queued',?,?,?,?,?,?,?)",
                (jid, model, "/".join(HYPIT_MODELS[model]), time.time(), time.time(),
                 json.dumps(body, ensure_ascii=False)[:4000],
                 _project_key(body), _need_label(body), str(body.get("session") or "")[:80]))
    con.commit()
    con.close()
    threading.Thread(target=_run, args=(jid, body), daemon=True).start()
    _register_delivery_leg(str(body.get("session") or ""), _project_key(body), jid)
    return 200, {"id": jid}


def _register_delivery_leg(session, task, jid):
    """提交一条生成腿 = 这条会话当场欠一条产物 ⇒ 追加进交付义务台账。

    为什么用 job_id 而不是文件路径：腿的产物名来自上游回包，提交那一刻还不存在
    （`handle_submit` 里只有 jid），拿路径表达就是假话。台账的 decide() 因此支持
    `{"job_id": ...}` 型期望，由 delivery_job_done() 判它是否已 succeeded。
    🔴 记账炸了绝不能影响提交（和 omni 的 store._register_delivery 同一条纪律）。
    """
    if not session:
        return
    try:
        import delivery_ledger
        delivery_ledger.append_obligation(session[:80], task or "hypit", "hypit",
                                          [{"job_id": jid, "kind": "video"}])
    except Exception:
        pass


def delivery_job_done(jid):
    """台账问"这条腿交付了吗"：jobs 表里 succeeded 才算，failed/queued/running 都不算。"""
    try:
        con = _db()
        try:
            row = con.execute("SELECT state FROM jobs WHERE id=?", (str(jid or "")[:40],)).fetchone()
        finally:
            con.close()
        return bool(row and row[0] == "succeeded")
    except Exception:
        return False


def handle_get(jid: str) -> dict:
    j = _job(jid)
    if not j:
        return {"error": {"code": "NOT_FOUND", "message": f"没有这个任务 {jid}"}}
    out = {"id": j["id"], "status": j["status"], "model": j["model"],
           "credits": j["credits"], "route": j["route"]}
    if j["output"]:
        out["output"] = j["output"]
    if j["error"]:
        out["error"] = json.loads(j["error"])
    return out


def handle_rates(query: dict) -> dict:
    from backend import gateway
    model = (query.get("model") or [""])[0]
    if model not in HYPIT_MODELS:
        return {"error": {"code": "UNKNOWN_MODEL", "message": f"未登记 {model}"}}
    cap, purpose = HYPIT_MODELS[model]
    unit = gateway.unit_price(cap, purpose)
    # 🔴 报价文案必须由 LANE_SPECS 生成，不许再写死全局常量：上一版这里印的是"720p、最长 10 秒、只收图片参考"，
    # 而真实闸已经是"每条路由各自档位"（h3 是 5 秒、不写 resolution、图片+视频参考）⇒ 页面上说的是假话。
    spec = _lane_spec(model)
    secs = "/".join(("%g" % x) for x in spec["seconds"])
    res = "/".join(spec["resolutions"]) if spec["resolutions"] else "不写 resolution（本路由只实测一种出幅）"
    refs = "+".join(spec["refs"])
    frames = ("，可带首/尾帧" if spec["last_frame"] else "，不接尾帧")
    return {"model": model, "route": f"{cap}/{purpose}", "credits_per_10s": unit,
            "lane": {"seconds": list(spec["seconds"]), "resolutions": list(spec["resolutions"]),
                     "aspects": list(spec["aspects"]) if spec["aspects"] is not None else None,
                     "refs": list(spec["refs"]), "last_frame": bool(spec["last_frame"])},
            "description": (f"本服务（Weeksir 网关 {cap}/{purpose}）：{res}、实测时长 {secs} 秒、"
                            f"只收 {refs} 参考{frames}；扣费 {unit} 分/条"
                            f"（文本/看图才按 token 结算），上游实付单价未录入 ⇒ "
                            f"这只是内部记账单位，不是对客报价")}


# ══ 导出口（#29）：一键 build + get，只放"零生成腿"的免费装配工程 ════════════════
# 四道闸（对齐 AGENTS.md 铁律"花钱前必须报价并等到同意"）：
#   ① 免费闸：hypit plan --json 的 providerRequestCount 必须 == 0；有生成腿一律拒，让它回 agent 报价流程
#   ② 路径白名单：run 必须是 HYPIT_WS 下的相对 .svrun；task 必须是 PROD_ROOT 下已存在的目录名
#   ③ 不走 shell：argv 列表 + 固定可执行文件（实测无 shell 直调 npm 的 `hypit` 垫片会 WinError 2 ⇒ 用 node + hypit.mjs）
#   ④ 单飞互斥：同时只跑一条，避免本机渲染互踩、同名成片互相覆盖
HYPIT_WS = os.environ.get("HYPIT_WORKSPACE") or r"C:\dsh\hypit_runs"
PROD_ROOT = os.environ.get("WEEKSIR_PRODUCTIONS") or r"C:\dsh\productions"
HYPIT_ENTRY = os.environ.get("HYPIT_ENTRY") or os.path.join(
    os.environ.get("APPDATA", ""), "npm", "node_modules", "@hypit", "hypit", "bin", "hypit.mjs")
EXPORT_BUILD_TIMEOUT = float(os.environ.get("HYPIT_EXPORT_TIMEOUT") or 1800)
# 没带 Bearer 时只认【本机 origin】：按 host 放行、不按端口（DSH 渲染层端口每次可能变）。
# 外站网页（http://evil.com）一律拒——它带自己的 Origin，藏不住。
LOCAL_ORIGIN_HOSTS = ("127.0.0.1", "localhost", "[::1]", "dsh-app", "file")


def _origin_host(value: str | None) -> str:
    v = str(value or "").strip().lower()
    if not v or v in ("null", "python", "curl"):
        return ""                       # 无 Origin = 本机命令行/脚本，走 Bearer 或下面的本机判定
    if "://" in v:
        scheme, rest = v.split("://", 1)
        # 非 http(s) 的自定义源（如 dsh-app://…）：scheme 才是身份，rest 是路径
        return scheme if scheme not in ("http", "https") else rest.split("/", 1)[0].split(":", 1)[0]
    return v.split("/", 1)[0].split(":", 1)[0]


def export_authorized(header_value: str | None, origin_value: str | None) -> bool:
    """Bearer 对 ⇒ 放行；否则只放行本机 origin（插件里不能嵌 token）。"""
    if authorized(header_value) and token_required():
        return True
    host = _origin_host(origin_value)
    return host in LOCAL_ORIGIN_HOSTS


def _hypit(argv: list, timeout: float = 180.0) -> tuple:
    """跑 hypit CLI：无 shell、cwd 固定在 workspace、强制 utf-8（默认 cp936 会把 UTF-8 输出解码崩）。"""
    import shutil
    import subprocess
    node = shutil.which("node") or "node"
    if not os.path.isfile(HYPIT_ENTRY):
        return 127, "", f"找不到 hypit 入口 {HYPIT_ENTRY}"
    try:
        r = subprocess.run([node, HYPIT_ENTRY, *argv], cwd=HYPIT_WS, capture_output=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
        return r.returncode, (r.stdout or ""), (r.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "", f"hypit {' '.join(argv[:1])} 超过 {timeout:.0f}s"
    except OSError as e:
        return 126, "", f"起不了 hypit：{type(e).__name__} {e}"


def _inside(root: str, candidate: str) -> bool:
    r, p = os.path.realpath(root), os.path.realpath(candidate)
    return p == r or p.startswith(r + os.sep)


def resolve_run(run_rel: str) -> tuple:
    """run 只接受 HYPIT_WS 下的相对 .svrun 路径。"""
    v = str(run_rel or "").strip().replace("\\", "/")
    if not v or ".." in v.split("/") or v.startswith("/") or ":" in v:
        return None, "run 必须是 hypit_runs 下的相对路径（不许 .. / 绝对路径 / 盘符）"
    p = os.path.join(HYPIT_WS, v)
    if not p.lower().endswith(".svrun"):
        return None, "run 必须是 .svrun 工程文件"
    if not os.path.isfile(p) or not _inside(HYPIT_WS, p):
        return None, "hypit_runs 下找不到这个 run"
    return p, ""


def resolve_task(task: str) -> tuple:
    """可选：交付到 productions/<task>/outputs/。task 只能是已存在的一层目录名。"""
    v = str(task or "").strip()
    if not v:
        return "", ""
    if "/" in v or "\\" in v or ".." in v:
        return None, "task 只能是 productions 下的目录名"
    p = os.path.join(PROD_ROOT, v)
    if not os.path.isdir(p) or not _inside(PROD_ROOT, p):
        return None, f"productions 下没有这个任务目录：{v}"
    return p, ""


def _safe_name(name: str, fallback: str) -> str:
    """只收"裸 ASCII 文件名 + .mp4"；含分隔符/穿越/非 ASCII 一律回落默认名。"""
    raw = str(name or "").strip()
    keep = "".join(c for c in raw if c.isascii() and (c.isalnum() or c in "._-"))
    if keep != raw or not keep.endswith(".mp4") or len(keep) <= 4 or ".." in keep or "/" in keep or "\\" in keep:
        return fallback + ".mp4"
    return keep[:48]


def _exp_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS exp(id TEXT PRIMARY KEY, run TEXT, state TEXT,"
                " build_id TEXT, name TEXT, path TEXT, url TEXT, bytes INTEGER, error TEXT,"
                " created REAL, updated REAL)")
    return con


def _exp_touch(eid: str, **fields) -> None:
    if not fields:
        return
    con = _exp_db()
    cols = ", ".join(f"{k}=?" for k in fields)
    con.execute(f"UPDATE exp SET {cols}, updated=? WHERE id=?", (*fields.values(), time.time(), eid))
    con.commit()
    con.close()


def _exp_row(eid: str) -> dict | None:
    con = _exp_db()
    cur = con.execute("SELECT id,run,state,build_id,name,path,url,bytes,error,created,updated"
                      " FROM exp WHERE id=?", (eid,))
    cols = [d[0] for d in cur.description]
    row = cur.fetchone()
    con.close()
    return dict(zip(cols, row)) if row else None


def export_reconcile() -> int:
    """进程重启会打断在跑的导出：把遗留 queued/running 判死（别让人以为还在导）。"""
    con = _exp_db()
    n = con.execute("SELECT COUNT(*) FROM exp WHERE state IN ('queued','planning','building','fetching')").fetchone()[0]
    if n:
        con.execute("UPDATE exp SET state='failed', error=?, updated=? WHERE state IN ('queued','planning','building','fetching')",
                    ("网关进程重启，本次导出被打断；请重新点一次", time.time()))
        con.commit()
    con.close()
    return n


def _build_ids(run_rel: str) -> list:
    rc, out, _ = _hypit(["builds", "--json"], timeout=180)
    if rc != 0:
        return []
    try:
        data = json.loads(out)
    except ValueError:
        return []
    want = run_rel.replace("/", "\\").lower()
    return [b["id"] for b in (data.get("builds") or [])
            if str(b.get("run", "")).replace("/", "\\").lower() == want]


def _export_run(eid: str, run_abs: str, task_dir: str, name: str) -> None:
    run_rel = os.path.relpath(run_abs, HYPIT_WS).replace("\\", "/")
    _exp_touch(eid, state="planning")
    rc, out, err = _hypit(["plan", run_rel, "--json"], timeout=300)
    if rc != 0:
        _exp_touch(eid, state="failed", error=(err or out)[:400]); return
    try:
        plan = json.loads(out)
    except ValueError:
        _exp_touch(eid, state="failed", error="plan 输出解析不了"); return
    paid = plan.get("providerRequestCount")
    if paid != 0:                       # 二次核验：入口那次可能被绕过/工程刚被改过
        _exp_touch(eid, state="failed",
                   error=f"这个工程有 {paid} 段付费生成，导出闸只放免费装配工程（要走生成请让 agent 报价）"); return
    targets = plan.get("targets") or []
    if not targets:
        _exp_touch(eid, state="failed", error="plan 没给出 target"); return

    before = set(_build_ids(run_rel))
    _exp_touch(eid, state="building")
    rc, out, err = _hypit(["build", run_rel, "--follow", "--json"], timeout=EXPORT_BUILD_TIMEOUT)
    new = [b for b in _build_ids(run_rel) if b not in before]
    build_id = new[0] if new else ""
    if rc != 0 and not build_id:
        _exp_touch(eid, state="failed", build_id=build_id, error=((err or out) or "build 失败")[:400]); return
    _exp_touch(eid, build_id=build_id)

    _exp_touch(eid, state="fetching")
    dest = os.path.join(task_dir or OUT_DIR, name)
    if task_dir:
        os.makedirs(os.path.join(task_dir, "outputs"), exist_ok=True)
        dest = os.path.join(task_dir, "outputs", name)
    rc, out, err = _hypit(["get", build_id, "--output", targets[0], "--to", dest, "--json"], timeout=300)
    if rc != 0 or not os.path.isfile(dest):
        _exp_touch(eid, state="failed", error=((err or out) or "取不回成片文件")[:400]); return

    os.makedirs(OUT_DIR, exist_ok=True)
    url_name = name
    if os.path.realpath(dest) != os.path.realpath(os.path.join(OUT_DIR, url_name)):
        import shutil
        shutil.copyfile(dest, os.path.join(OUT_DIR, url_name))
    _exp_touch(eid, state="done", path=dest, url=f"{PUBLIC_BASE}/api/hypit/assets/{url_name}",
               bytes=os.path.getsize(dest))


def handle_export(body: dict) -> tuple:
    """POST /api/hypit/export {run, task?, name?} → {id}；花钱的腿一律拒。"""
    run_abs, why = resolve_run(body.get("run") or "")
    if why:
        return 400, {"error": {"code": "BAD_RUN", "message": why}}
    task_dir, why = resolve_task(body.get("task") or "")
    if why:
        return 400, {"error": {"code": "BAD_TASK", "message": why}}
    con = _exp_db()
    live = con.execute("SELECT id,state FROM exp WHERE state IN ('queued','planning','building','fetching')").fetchall()
    con.close()
    if live:
        return 409, {"error": {"code": "BUSY", "message": f"已有导出在跑（{live[0][0]} · {live[0][1]}），等它结束再点"}}
    eid = uuid.uuid4().hex[:12]
    name = _safe_name(body.get("name") or "", f"export-{eid}")
    now = time.time()
    con = _exp_db()
    con.execute("INSERT INTO exp(id,run,state,name,created,updated) VALUES(?,?,?,?,?,?)",
                (eid, os.path.relpath(run_abs, HYPIT_WS).replace("\\", "/"), "queued", name, now, now))
    con.commit()
    con.close()
    threading.Thread(target=_export_run, args=(eid, run_abs, task_dir, name), daemon=True).start()
    return 200, {"id": eid, "name": name}


def export_status(eid: str) -> dict:
    row = _exp_row(eid)
    if not row:
        return {"error": {"code": "NOT_FOUND", "message": f"没有这条导出 {eid}"}}
    row["elapsed"] = round(time.time() - (row.pop("created") or time.time()))
    return row


def serve_asset(name: str):
    path = os.path.normpath(os.path.join(OUT_DIR, os.path.basename(name)))
    if not path.startswith(os.path.normpath(OUT_DIR)) or not os.path.isfile(path):
        return None, None
    ext = os.path.splitext(path)[1].lower()
    return open(path, "rb").read(), {".mp4": "video/mp4", ".mov": "video/quicktime",
                                     ".webm": "video/webm", ".jpg": "image/jpeg", ".png": "image/png"}.get(ext,
                                                                                                            "application/octet-stream")


if __name__ == "__main__":
    # studio 归属登记（契约见 C:\dsh\docs\ADR-workbench-presence.md）：起 studio 前 claim 这个 run。
    #   python backend/hypit_bridge.py studio-claim <DSH会话id> <run源路径或工程名> [端口]
    #   python backend/hypit_bridge.py studio-release <会话id> [run]   清空全部要显式加 --all
    #   python backend/hypit_bridge.py studio-status [会话id]          不带=运维视图（不会回 up）
    # 过期项不用手动清：claim TTL 30 分钟，读的时候自动回收。
    import sys
    args = sys.argv[1:]
    cmd = args[0] if args else ""
    if cmd == "studio-claim" and len(args) >= 3:
        print(json.dumps(studio_claim(args[1], args[2],
                                      int(args[3]) if len(args) > 3 else STUDIO_PORT), ensure_ascii=False))
    elif cmd == "studio-release":
        rest = [a for a in args[1:] if not a.startswith("--")]      # --all 不算位置参数
        print(json.dumps(studio_release(rest[0] if rest else "", rest[1] if len(rest) > 1 else "",
                                        "--all" in args), ensure_ascii=False))
    elif cmd == "preflight":     # 重启 8100 前必查：在途腿/在途导出/未结算预扣，任一非零都别重启
        con = _db()
        act = [r[0] for r in con.execute("select state from jobs where state in ('queued','running')")]
        try:
            exp = [r[0] for r in con.execute("select state from exp where state in ('queued','planning','building','fetching')")]
        except sqlite3.OperationalError:
            exp = []
        con.close()
        held = holds_pending()
        print(json.dumps({"active_jobs": len(act), "active_exports": len(exp),
                          "pending_holds": len(held), "pending_credit": sum(h["amount"] or 0 for h in held),
                          "safe_to_restart": not act and not exp}, ensure_ascii=False))
    elif cmd == "holds-pending":
        rows = holds_pending(args[1] if len(args) > 1 else "")
        print(json.dumps({"pending": rows, "total": sum(r["amount"] or 0 for r in rows)}, ensure_ascii=False))
    elif cmd == "holds-rollback" and len(args) >= 2:
        print(json.dumps(holds_rollback(args[1]), ensure_ascii=False))
    elif cmd == "studio-status":
        print(json.dumps(studio_status(args[1] if len(args) > 1 else None), ensure_ascii=False))
    elif cmd == "intent" and len(args) >= 3:
        # 铁律动作：确认走哪条流水线之后，第一件事就是登记意图（右栏只按它挂台）
        out = set_intent(args[1], args[2], args[3] if len(args) > 3 else "")
        if args[2] == "hypit":                      # 🔴 选了就给界面：登记意图这一步顺手把 studio 起起来
            out["viewer"] = studio_up(args[1], args[3] if len(args) > 3 else "")
        print(json.dumps(out, ensure_ascii=False))
    elif cmd == "studio-up" and len(args) >= 2:
        print(json.dumps(studio_up(args[1], args[2] if len(args) > 2 else ""), ensure_ascii=False))
    elif cmd == "intent-clear" and len(args) >= 2:
        print(json.dumps(clear_intent(args[1]), ensure_ascii=False))
    elif cmd == "intent-show":
        print(json.dumps(intent_view(), ensure_ascii=False))
    elif cmd == "export" and len(args) >= 2:
        # 走和右栏按钮同一条代码路径（同一套闸）：python backend/hypit_bridge.py export <run相对路径> [task目录名] [文件名]
        code, payload = handle_export({"run": args[1], "task": args[2] if len(args) > 2 else "",
                                       "name": args[3] if len(args) > 3 else ""})
        print(json.dumps({"http": code, **payload}, ensure_ascii=False), flush=True)
        if code != 200 or not payload.get("id"):
            sys.exit(1)
        # 这条命令是前台跑的：不轮询到底，进程一退 daemon 线程就跟着死，导出会被自己掐断
        eid = payload["id"]
        while True:
            row = export_status(eid)
            state = row.get("state")
            if state in ("done", "failed"):
                print(json.dumps(row, ensure_ascii=False), flush=True)
                sys.exit(0 if state == "done" else 1)
            time.sleep(5)
    elif cmd == "export-status" and len(args) >= 2:
        print(json.dumps(export_status(args[1]), ensure_ascii=False))
    else:
        print(__doc__ or "", "用法见 studio-claim / studio-release / studio-status / export / export-status",
              file=sys.stderr)
        sys.exit(2)
