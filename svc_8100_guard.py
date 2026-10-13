# -*- coding: utf-8 -*-
"""8100 存活守卫 —— 判的是【这个端口上的服务还答不答话】，不是【端口在不在】。

为什么要有它（2026-10-11 现网）：8100 的 keep-alive 没设超时，右栏插件 5s 一轮 + 表单页 8s 一轮
把连接攒到 21 条，accept 积压打满 ⇒ 端口仍然 LISTENING、进程仍然活着，但新连接一律【连接被拒】。
老守卫是 `curl … && exit /b 0`：curl 失败它就往下走、直接再起一份 —— 于是又留下一个
"活着但一个端口都没占到"的僵尸实例（当天盘上真的同时躺着两份 model_admin）。
而任务计划那边 MultipleInstancesPolicy=IgnoreNew + 实例记录卡死（LastTaskResult=0x800710E0），
⇒ "端口活着、服务已死"这种死法当时没有任何东西能发现，也没有东西能把它换掉。

三种状态的判据在 classify()（唯一一份，闸钉着）；动作分四个子命令：
  status  —— 只读：报 HEALTHY / WEDGED / DOWN，退出码 0/1/2。
  ensure  —— 【看门狗每一跳走这条】.cmd 只调这一行：先掐掉"端口还占着却不答话"的那一份
             （只杀命令行里确实是 model_admin 的进程），需要时拉起一份，最后复验。
             ⚠️ 绝不在这条路里碰 schtasks —— .cmd 就是任务的动作，守卫再 /run 是自我递归：
                2026-10-11 实测第二层 guard 会把刚起的那份 /end 掉，表现是"报 RESTARTED-OK 然后端口空着"。
  sweep   —— 只做"掐掉卡死的那一份"，不起服务（给排错和手动一步步走时用）。
  restart —— 人工重启：schtasks /end → 确认包装用的 wscript 退净 → /run → 复验。
             （任务的实例记录会卡成 LastTaskResult=0x800710E0="还在跑"，不先确认 wscript 退净，/run 就是白敲。）

用法：  python svc_8100_guard.py status | sweep | ensure | restart
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

PORT = int(os.environ.get("MODEL_ADMIN_PORT", "8100"))
BASE = "http://127.0.0.1:%d" % PORT
# 🔴 探针地址：8100 上【没有 /api/health】（那条是别的口的）。这里用 /api/model/caps ——
#    它是老 .cmd 一直在用的同一个口，且只读、0 消耗。判"活"的标准是【有没有 HTTP 应答】，
#    不是"是不是 200"：404/500 都说明进程在答话，而 WEDGED 那种死法是根本不应答（超时/拒）。
#    拿 200 当"活"会造出一个更坏的形状：路由一改名，看门狗就每分钟杀一次健康服务。
HEALTH_URL = BASE + "/api/model/caps"
TRIES = 3                  # 连续几次不应答才判死（一次超时可能是正在处理长请求）
TIMEOUT = 3.0
GAP = 1.0
TASK = "WeeksirModelAdmin"
VBS = "run_hidden.vbs"
HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "_svc_8100.err.log")
OWNER_MARK = "model_admin"   # 只有命令行里带这个词的进程才许被杀


def classify(answers: list, listener_pid) -> str:
    """纯函数：探针结果 + 端口占用者 → 三态。判据只这一份（闸也读它）。"""
    if any(answers):
        return "HEALTHY"
    return "WEDGED" if listener_pid else "DOWN"


def should_kill(cmdline: str) -> bool:
    """白名单而不是黑名单：只有命令行里明确带着 model_admin 的才许杀。
    万一 8100 被别的服务占了，守卫只能报状态，不能替人杀掉不相干的进程。"""
    return OWNER_MARK in str(cmdline or "").lower()


def probe(tries: int = TRIES, timeout: float = TIMEOUT, gap: float = GAP) -> list:
    """连打 tries 次。有一次【拿到 HTTP 应答】就停（健康时不必白等三轮）。"""
    out = []
    for i in range(max(1, tries)):
        out.append(answered(timeout))
        if out[-1]:
            break
        if i < tries - 1:
            time.sleep(gap)
    return out


def answered(timeout: float = TIMEOUT) -> bool:
    """这个口有没有回话：2xx/4xx/5xx 都算回了（进程活着）；超时、拒连、异常才算没回。"""
    try:
        urllib.request.urlopen(HEALTH_URL, timeout=timeout).read(1)
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:                                       # noqa: BLE001
        return False


def listener_pid(port: int = PORT):
    """谁在 LISTENING 这个端口（拿不到就回 None，不猜）。"""
    try:
        r = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=15)
    except Exception:                                       # noqa: BLE001
        return None
    for ln in (r.stdout or "").splitlines():
        m = re.search(r"TCP\s+\S*:%d\s+\S+\s+LISTENING\s+(\d+)" % port, ln)
        if m:
            return int(m.group(1))
    return None


def cmdline_of(pid: int) -> str:
    ps = 'Get-CimInstance Win32_Process -Filter "ProcessId=%d" | ForEach-Object { $_.CommandLine }' % pid
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=20)
    except Exception:                                       # noqa: BLE001
        return ""
    return (r.stdout or "").strip()


def wrapper_pids():
    """还活着的 `wscript run_hidden.vbs start_model_admin.cmd` 包装进程。
    /end 之后必须确认它们退净，否则 IgnoreNew 会认为"实例还在跑"，/run 直接白敲。"""
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'wscript.exe' -and "
          "$_.CommandLine -like '*%s*' } | ForEach-Object { $_.ProcessId }" % VBS)
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=20)
    except Exception:                                       # noqa: BLE001
        return []
    return [int(x) for x in re.findall(r"\d+", r.stdout or "")]


def status() -> dict:
    answers = probe()
    pid = None if any(answers) else listener_pid()
    return {"state": classify(answers, pid), "answers": answers, "pid": pid,
            "cmdline": (cmdline_of(pid) if pid else "")}


def _log(line: str) -> None:
    try:
        with open(LOG, "a", encoding="utf-8", errors="replace") as f:
            f.write(time.strftime("[%Y-%m-%d %H:%M:%S] guard: ") + line + "\n")
    except OSError:
        pass


def _wait_healthy(seconds: float = 25.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if probe(tries=1):
            return True
        time.sleep(1.0)
    return False


SERVICE_LOG = os.path.join(HERE, "_svc_8100.service.log")


def _log_sink():
    """被看着的那个进程，stderr 该落哪儿。
    🔴 结构要求：**看门狗自己的日志绝不被它看着的进程握着**。
       现网形状（2026-10-11 21:31→23:53 这次宕机）：`_spawn_direct()` 把 model_admin 的 stdout/stderr
       指到守卫自己那份 `_svc_8100.err.log`（任务那一跳又是 `2>>` 同一个文件），于是长期活着的
       服务进程继承了那份句柄；下一次守卫要重启时 `open(LOG,"a")` 撞 PermissionError ⇒
       `ensure` 每分钟崩在同一行裸 open 上，8100 一直没人起（首页四张技能封面全成碎图标）。
       "要重启才走的那条路"恰好是唯一会碰这份日志的路 ⇒ 平时看不出来，一出事就是死锁。
    退路顺序：服务专用日志 → 带时间戳的旁路 → DEVNULL（宁可无声，不可不起）。"""
    for p in (SERVICE_LOG, "%s.%s" % (SERVICE_LOG, time.strftime("%Y%m%d-%H%M%S"))):
        try:
            return open(p, "a", encoding="utf-8", errors="replace"), os.path.basename(p)
        except OSError:
            continue
    return subprocess.DEVNULL, "devnull"


def _spawn_direct() -> str:
    """最后兜底：任务那条路起不来时，自己拉一份（stderr 走服务专用那份，绝不碰守卫自己的日志）。"""
    try:
        err, where = _log_sink()
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000008 | 0x00000200   # DETACHED_PROCESS | NEW_PROCESS_GROUP
        subprocess.Popen([sys.executable, os.path.join(HERE, "backend", "model_admin.py")],
                         cwd=HERE, stdout=err, stderr=err, stdin=subprocess.DEVNULL, **kwargs)
    except Exception as e:                                  # noqa: BLE001 —— 起服务这一刀不许把守卫带走
        _log("direct-spawn 失败：%s: %s" % (type(e).__name__, str(e)[:120]))
        return "direct-spawn-failed:%s" % type(e).__name__
    _log("direct-spawn 已拉起 model_admin（stderr→%s）" % where)
    return "direct-spawn"


def task_restart(wait: float = 30.0) -> str:
    """【人工/agent 用】把任务那一跳真正重启一次：/end → 确认包装 wscript 退净 → /run → 等健康。
    ⚠️ 看门狗自己【绝不】走这里 —— .cmd 就是任务的动作，ensure 里再 /run 等于自我递归：
       2026-10-11 实测第二层 guard 会把第一层刚起的那份 /end 掉，表现是"报 RESTARTED-OK 然后端口空着"。
       这条留给人，是因为任务的实例记录会卡（LastTaskResult=0x800710E0="还在跑"），
       不先把 wscript 确认退净，/run 就是白敲。
    🔴 但"端口答话"【不能】当重启成功的判据（2026-10-13 实测）：端口可能被【上一代的孤儿 python】
       占着 —— 19:11 起的那份 model_admin 父进程已死，`schtasks /end` 根本碰不到它。于是
       /end → /run 都"成功"，答话的还是那份旧代码，守卫打印 RESTARTED-OK 而新代码一行都没生效。
       所以这里改判据：**换人**才算重启（结束前记下监听 pid，重启后必须是另一个 pid）。"""
    old = listener_pid()
    subprocess.run(["schtasks", "/end", "/tn", TASK], capture_output=True, timeout=30)
    for _ in range(8):          # 等包装进程退净，IgnoreNew 才不会再挡下 /run
        left = wrapper_pids()
        if not left:
            break
        for w in left:
            subprocess.run(["taskkill", "/PID", str(w), "/F"], capture_output=True, timeout=20)
        time.sleep(1.0)
    if old:                     # 端口还被【/end 管不到的旧进程】占着 ⇒ 指名结束它（只杀命令行确实是
        for _ in range(4):      #    model_admin 的那一份；命令行对不上就什么都不做，见 kill_wedged）
            if not cmdline_of(int(old)):
                break
            if not kill_wedged(int(old)):
                break
            time.sleep(1.0)
    subprocess.run(["schtasks", "/run", "/tn", TASK], capture_output=True, timeout=30)
    if not _wait_healthy(wait):
        return "schtasks-no-answer"
    # 🔴 答话之后还要读得到"是谁在答话"：刚 /run 的那一两秒里监听表可能还没刷出来（实测
    #    49252->None），此时若直接判"换了人"就是又一次自报成功。读不到就等，等不到就明说。
    new = None
    for _ in range(12):
        new = listener_pid()
        if new:
            break
        time.sleep(0.5)
    if not new:
        return ("schtasks-no-listener-pid ⇒ 端口在答话但读不到监听者 pid，"
                "不能判「换人」，请手动核一次再当生效")
    if old and new == old:
        return ("schtasks-SAME-PID-%s ⇒ 端口还是【重启前那一份】在答话 = 跑的是旧代码，"
                "别当重启成功" % old)
    return "schtasks %s->%s" % (old, new)


def kill_wedged(pid: int) -> bool:
    """只杀【命令行里确实是 model_admin】的占用者；杀完等端口真的空出来。"""
    if not should_kill(cmdline_of(pid)):
        _log("端口被 pid=%s 占着，但命令行里没有 %s ⇒ 不杀（只报状态）" % (pid, OWNER_MARK))
        return False
    subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=30)
    for _ in range(12):
        if not listener_pid():
            return True
        time.sleep(1.0)
    return False


def sweep_wedged() -> str:
    """只做一个动作：端口有人监听、却连续 TRIES 次不答话 ⇒ 掐掉那一份（且它必须是 model_admin）。
    健康/没人监听 都不动，绝不在这儿起服务 —— 起服务的是 .cmd，两边各管一半才没有递归。"""
    if any(probe(tries=TRIES)):
        return "HEALTHY"
    pid = listener_pid()
    if not pid:
        return "DOWN"
    if not kill_wedged(int(pid)):
        return "WEDGED-REFUSED-KILL"
    _log("WEDGED：连续 %d 次探针不应答 ⇒ 已掐掉 pid=%s，等 .cmd 起新的一份" % (TRIES, pid))
    return "WEDGED-KILLED"


def ensure() -> int:
    """看门狗每一跳走这条（.cmd 只调它一行）：先掐掉卡死的那一份，需要时拉起新的，最后复验。
    🔴 绝不在这条路里碰 schtasks —— .cmd 就是任务的动作，守卫再 /run 是自我递归。"""
    st = sweep_wedged()
    if st == "HEALTHY":
        return 0
    if st == "WEDGED-REFUSED-KILL":
        print(st)
        return 1
    if listener_pid():
        print("BUSY-TRYING-LATER")
        return 1
    _sp = _spawn_direct()
    ok = _wait_healthy()
    _log("ensure（%s）：%s" % (st, "起好了" if ok else "起了但仍不应答（下一跳再来）"))
    if _sp.startswith("direct-spawn-failed"):
        print("SPAWN-FAILED %s" % _sp)          # 🔴 起都没起来 ≠ 起了没应答，两件事别混成一条
        return 1
    print("STARTED-OK" if ok else "STARTED-STILL-DEAD")
    return 0 if ok else 1


if __name__ == "__main__":
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "status")
    if cmd == "status":
        s = status()
        print(json.dumps({"state": s["state"], "answers": s["answers"], "pid": s["pid"],
                          "port": PORT}, ensure_ascii=False))
        sys.exit({"HEALTHY": 0, "WEDGED": 1}.get(s["state"], 2))
    elif cmd == "sweep":
        print(sweep_wedged())          # .cmd 每一跳走这条：只掐死的，不起服务
        sys.exit(0)
    elif cmd == "ensure":
        sys.exit(ensure())
    elif cmd == "restart":
        how = task_restart()
        ok = probe(tries=1) and not how.startswith("schtasks-SAME-PID")
        # 🔴 "答话"不等于"换人"：旧进程还在答话时这里必须红着报，否则调用方（和我）会把
        #    "RESTARTED-OK" 当成新代码已生效 —— 2026-10-13 就是这么差点收工的。
        print(("RESTARTED-OK " if ok else "RESTARTED-NOT-EFFECTIVE ") + how)
        sys.exit(0 if ok else 1)
    else:
        print("用法: python svc_8100_guard.py [status|sweep|ensure|restart]")
        sys.exit(2)
