"""交付义务台账（delivery ledger）—— 根治"视频没出来就停止运行"的状态地基。

为什么需要它（2026-10-05 实测）：内核回合结束的判据是
`packages/core/agent-loop/src/agent.ts:486-487` —— 本轮没有 tool-call 就 return {kind:'completed'}，
**没有任何东西检查产物在不在盘上**。提示词只能改模型的倾向，改不了这条判据，
所以"还欠几条产物"必须有一个有状态的主人：提交时登记、文件真落盘才销账、超时自动作废。

三条设计决定：
1. 台账落在【工作区】`<工作区>\\.dsh\\delivery.json` —— DSH 自带 agent 跑在文件沙箱里，写 D:\\video-skill 会被拒
   （`sandbox escalation … is not strictly wider`），但它一定写得 own 工作区。和 `.dsh\\intent.json` 同一套路。
2. 期望路径由【提交方】给死，不许拿 mtime 猜目录 —— "任务名→工程目录"没有主人正是 no-project 那个老坑。
3. TTL 到期自动作废 —— 一条几天前的登记不能变成这条会话的永久欠账（presence 四修的同一条教训）。

用法：open_obligation(...) 在提交成功后调用；pending()/sweep() 给 8100 读口和回归闸用。
"""
import json
import os
import time

LEDGER_PATH = os.environ.get("DELIVERY_LEDGER") or os.path.join(
    os.environ.get("DSH_WORKSPACE") or r"C:\dsh", ".dsh", "delivery.json")
DEFAULT_TTL_SEC = float(os.environ.get("DELIVERY_TTL") or 24 * 3600)
KINDS = ("video", "image", "audio", "file")
# 一次写盘撞 WinError 5（杀软/索引器占着）不代表失败：重试 4 次仍失败才明确报错，不静默丢登记
WRITE_RETRIES = 4


def _path(path=None):
    """每次调用现读 env —— 不在 import 时固化。

    为什么：常驻进程（8100）里模块只 import 一次，固化路径会让运行期的覆盖失效，
    测试也只能靠子进程重开才能换台账 —— 上一版就栽在这儿（改了 env 还读到旧路径）。
    """
    return path or os.environ.get("DELIVERY_LEDGER") or os.path.join(_workspace(), ".dsh", "delivery.json")


def _workspace(workspace=None):
    return workspace or os.environ.get("DSH_WORKSPACE") or r"C:\dsh"


def _read(path):
    """文件不存在 = 确实没欠账；存在但读不出 = 【不知道】，必须往上抛。

    🔴 这两件事绝不能混：混了就等于台账坏掉时向拦截器谎报"这条会话不欠东西"（presence 五修的同一条纪律）。
    """
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else []


def _write(path, rows):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp-%d" % os.getpid()
    last = None
    for _ in range(WRITE_RETRIES):
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
            return
        except Exception as e:                       # noqa: BLE001 - 换时间重试，最后一次才报
            last = e
            time.sleep(0.15)
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
    except Exception:
        pass
    raise RuntimeError("delivery ledger 写盘失败（重试 %d 次）：%s -> %s" % (WRITE_RETRIES, path, last))


def decide(entry, now, size_of, job_done=None):
    """纯判据（不碰磁盘、不读时钟）⇒ 能整块抠出来跑真值表。

    size_of(path) 返回字节数或 None（不存在/读不到）；job_done(job_id) 返回 True/False。
    为什么要有 job_done：hypit 的一条腿在【提交时】还不知道产物文件叫什么（名字来自上游回包），
    拿路径表达就是假话 —— 那就用"这条 job 必须跑到 succeeded"来表达，一样可判。
    返回 (status, 更新后的 entry)。status: closed（全部满足）> expired（超 TTL 且没清账）> pending。
    """
    job_done = job_done or (lambda jid: False)
    out = dict(entry)
    items = []
    all_done = True
    for it in out.get("expected") or []:
        it = dict(it)
        if not it.get("delivered_at"):
            jid = it.get("job_id")
            hit = bool(job_done(jid)) if jid else bool(size_of(it.get("path")))
            if hit:
                it["delivered_at"] = now
        if not it.get("delivered_at"):
            all_done = False
        items.append(it)
    out["expected"] = items
    if all_done:
        # 全落盘才算 closed；空 expected 也走这里（没东西欠）—— 但正常路径进不来，open_obligation 会先拒
        out["closed_at"] = out.get("closed_at") or now
        out["expired_at"] = None
        return "closed", out
    if now - float(out.get("created_at") or now) > float(out.get("ttl_sec") or DEFAULT_TTL_SEC):
        out["expired_at"] = now
        return "expired", out
    out["expired_at"] = None
    return "pending", out


def _rel(path, workspace):
    """期望路径统一存成【工作区相对 + 正斜杠】：台账要能被另一台机器/别的工作区读懂。"""
    p = (path or "").replace("\\", "/")
    w = (workspace or "").replace("\\", "/").rstrip("/")
    if w and p.lower().startswith(w.lower() + "/"):
        return p[len(w) + 1:]
    return p


def open_obligation(session, task, skill, expected, workspace=None, budget_credits=0,
                    ttl_sec=None, now=None, path=None):
    """登记/覆盖一条义务。同 (session, task) 覆盖旧的（换段数=重新登记，不叠加）。

    expected: ["a.mp4", {"path":..., "kind":...}, ...] —— 顺序即 seq。
    🔴 调用方必须包 try/except：登记炸了绝不能让提交本身失败（先出片，再谈记账）。
    """
    path = _path(path)
    workspace = _workspace(workspace)
    now = now if now is not None else time.time()
    items = []
    for i, e in enumerate(expected or [], 1):
        if isinstance(e, dict):
            p, kind = e.get("path") or "", e.get("kind") or "file"
            jid = e.get("job_id")
        else:
            p, kind, jid = ("" if e is None else str(e)), "file", None
        p = _rel(p, workspace)
        if not p.strip() and not jid:
            continue                      # 🔴 None/空串不许变成字面量 "None" 混进台账
        item = {"seq": i, "kind": kind if kind in KINDS else "file", "delivered_at": None}
        if p.strip():
            item["path"] = p
        if jid:
            item["job_id"] = str(jid)[:40]
        items.append(item)
    if not items:
        raise ValueError("delivery obligation 没有一条有效 expected（路径与 job_id 全空）")
    entry = {"session": session or "", "task": task or "", "skill": skill or "",
             "expected": items, "budget_credits": int(budget_credits or 0),
             "created_at": now, "ttl_sec": float(ttl_sec if ttl_sec is not None else DEFAULT_TTL_SEC),
             "closed_at": None, "expired_at": None}
    rows = _read(path)
    rows = [r for r in rows if not (r.get("session") == entry["session"] and r.get("task") == entry["task"])]
    rows.append(entry)
    _write(path, rows)
    return entry


def append_obligation(session, task, skill, expected, workspace=None, path=None, now=None):
    """往同一条 (session, task) 的义务上【追加】期望项（hypit 每提交一条腿就加一条）。

    和 open_obligation 的区别是语义：open = 重新规划这次任务（覆盖），append = 又欠了一条。
    没有对应条目时等价于 open。已 closed/expired 的条目会被重新激活（又欠账了就是又欠账了）。
    """
    path = _path(path)
    now = now if now is not None else time.time()
    # 🔴 必须在 open_obligation 之前读：它会按 (session,task) 覆盖，之后就读不到旧期望项了
    #    （第一版就栽在这儿：追加第二条腿把第一条冲掉，台账显示"只欠一条"）
    mine = [r for r in _read(path) if r.get("session") == session and r.get("task") == task]
    probe = open_obligation(session, task, skill, expected, workspace=workspace, path=path, now=now)
    if not mine:
        return probe
    base = sorted(mine, key=lambda r: r.get("created_at") or 0)[0]
    seen = {str(i.get("path") or "") + "|" + str(i.get("job_id") or "") for i in base.get("expected") or []}
    items = list(base.get("expected") or [])
    for it in probe["expected"]:
        key = str(it.get("path") or "") + "|" + str(it.get("job_id") or "")
        if key in seen:
            continue
        it = dict(it)
        it["seq"] = len(items) + 1
        items.append(it)
        seen.add(key)
    base["expected"] = items
    base["closed_at"] = None               # 又欠账了就是又欠账了，别拿旧的 closed 挡着
    base["expired_at"] = None
    others = [r for r in _read(path) if not (r.get("session") == session and r.get("task") == task)]
    _write(path, others + [base])
    return base


def refresh(path=None, workspace=None, now=None, job_done=None):
    """扫一遍台账：落盘的销账、全销的关闭、超时的作废。返回 (rows, 未清账条数)。"""
    path = _path(path)
    workspace = _workspace(workspace)
    now = now if now is not None else time.time()

    def size_of(rel):
        try:
            n = os.path.getsize(os.path.join(workspace, rel.replace("/", os.sep)))
            return n if n > 0 else None
        except Exception:
            return None

    rows = _read(path)
    changed, out = False, []
    for r in rows:
        status, new = decide(r, now, size_of, job_done)
        changed = changed or new != r
        out.append(new)
    if changed:
        _write(path, out)
    pending = [r for r in out if decide(r, now, size_of, job_done)[0] == "pending"]
    return out, len(pending)


def pending(session=None, path=None, workspace=None, now=None, job_done=None):
    """给 8100 读口/右栏/回归闸共用的唯一口径：现在【还欠】哪些。"""
    rows, _ = refresh(path=path, workspace=workspace, now=now, job_done=job_done)
    out = []
    for r in rows:
        if r.get("closed_at") or r.get("expired_at"):
            continue
        if session is not None and r.get("session") != session:
            continue
        out.append({"session": r.get("session"), "task": r.get("task"), "skill": r.get("skill"),
                    "budget_credits": r.get("budget_credits"), "created_at": r.get("created_at"),
                    "ttl_sec": r.get("ttl_sec"),
                    "outstanding": [i for i in r.get("expected") or [] if not i.get("delivered_at")],
                    "delivered": [i for i in r.get("expected") or [] if i.get("delivered_at")]})
    return out


def sweep(path=None, workspace=None, now=None, job_done=None):
    """把过期/已关闭的清掉（台账不能只增不减 —— 那是 presence 三修的病）。返回清掉几条。"""
    path = _path(path)
    rows, _ = refresh(path=path, workspace=workspace, now=now, job_done=job_done)
    now = now if now is not None else time.time()
    keep = []
    for r in rows:
        if r.get("closed_at") or r.get("expired_at"):
            continue
        if now - float(r.get("created_at") or now) > float(r.get("ttl_sec") or DEFAULT_TTL_SEC) * 2:
            continue
        keep.append(r)
    if len(keep) != len(rows):
        _write(path, keep)
    return len(rows) - len(keep)


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "pending"
    if cmd == "pending":
        print(json.dumps(pending(sys.argv[2] if len(sys.argv) > 2 else None), ensure_ascii=False, indent=1))
    elif cmd == "sweep":
        print("cleared", sweep())
    else:
        print("usage: delivery_ledger.py [pending [session] | sweep]")
