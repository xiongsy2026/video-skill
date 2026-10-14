# -*- coding: utf-8 -*-
"""花钱批准的【置顶弹窗】：人证仍然是"点一下"，只是那一下自己弹到脸上。

为什么不是系统 MessageBox（2026-10-08 实测，别再走回头路）：
    无人参与时 MessageBoxW 会【自己返回 IDYES】并真签出条子，闸 G 组抓到过两次；
    MB_TIMEOUT 也不生效。系统弹窗不能当人证，已从代码里整个删掉。
这个窗口只有三样东西：
    ① 只有真实鼠标点击才发 POST（Enter 被显式吃掉，键盘不误签）；
    ② 额度仍由服务端 consume_by_click 签出，核销码从不出现在窗口、stdout 或命令行；
    ③ 没人点 ⇒ pending 15 分钟过期 ⇒ 回到"拒绝"态（fail-closed），不会有人替谁点头。

用法（由 `gateway.py approve` 建完待批后自动拉起，人不用敲）：
    pythonw approval_popup.py <pending_id> [--port 8100]
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from tkinter import Tk, Label, Button, Frame

BRAND = "WEEKSIR 电商AI专家 · 专注电商AI落地"
POLL_MS = 2000


def center_xy(sw: int, sh: int, w: int, h: int) -> tuple:
    """纯判据：屏幕 sw×sh、窗口 w×h ⇒ 左上角该放哪（居中，且不许放到屏幕外）。

    🔴 为什么这条必须是函数而不是两行内联算术：这张窗是【唯一的人证入口】，
       它落在屏幕角落里 = 客户没看见 = 15 分钟过期 = 这条方案白报价（2026-10-12 用户点名"弹窗要居中"）。
    夹到 0 是为了另一种形状：窗口比屏幕还高（内容长、或客户是 1366×768 的小屏）⇒
    居中算出来是负数，那会把标题栏推到屏幕外，连【批准】那颗按钮都够不着。
    """
    return max(0, int((sw - w) // 2)), max(0, int((sh - h) // 2))


def place_centered(root) -> tuple:
    """按【当前实际尺寸】把窗口挪到屏幕正中；返回 (x, y) 方便出声报数。

    🔴 为什么写全 `宽x高+X+Y` 而不是只写 `+X+Y`（2026-10-12 实测）：只给位置不带尺寸时
       Tk 不设 USPosition 标志，窗口一 map 就被 Windows **级联放置**接管 —— 现网两次弹出来
       落在 78,78 和 130,130（每次往下偏 52px），居中那一句等于没执行。带上尺寸两个标志都置上，
       WM 才认这个位置。
    """
    root.update_idletasks()                       # 没有这句，winfo_req* 还是打包前的旧尺寸
    w, h = root.winfo_reqwidth(), root.winfo_reqheight()
    x, y = center_xy(root.winfo_screenwidth(), root.winfo_screenheight(), w, h)
    root.geometry("%dx%d+%d+%d" % (w, h, x, y))
    root.update_idletasks()
    return x, y


def _rpc(port: int, path: str, body=None):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), method="POST" if body else "GET")
    if body:
        raw = json.dumps(body).encode("utf-8")
        req.data = raw
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            return json.loads(r.read().decode("utf-8", "replace") or "{}")
    except Exception as e:                                    # noqa: BLE001
        return {"_err": "%s: %s" % (type(e).__name__, str(e)[:90])}


def fetch_pending(port: int, pid: str) -> dict:
    j = _rpc(port, "/api/spend/pending")
    for row in (j.get("pending") or []):
        if row.get("id") == pid:
            return row
    return {}


def main(argv):
    pid = argv[0] if argv else ""
    port = 8100
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])
    if not pid:
        print("用法: pythonw approval_popup.py <pending_id> [--port 8100]")
        return 2

    root = Tk()
    root.title("批准这次生成 · Weeksir")
    root.attributes("-topmost", True)
    root.resizable(False, False)
    root.configure(bg="#0f1115")
    fg, sub = "#e8eaed", "#9aa4b2"
    Label(root, text=BRAND, fg="#7fd1ae", bg="#0f1115", font=("Microsoft YaHei UI", 9)).pack(pady=(14, 2))
    Label(root, text="这次要跑的是", fg=sub, bg="#0f1115", font=("Microsoft YaHei UI", 9)).pack()
    title = Label(root, text="读取中…", fg=fg, bg="#0f1115", font=("Microsoft YaHei UI", 13, "bold"),
                  wraplength=420, justify="left")
    title.pack(pady=(2, 6), padx=18, anchor="w")
    detail = Label(root, text="", fg=sub, bg="#0f1115", font=("Microsoft YaHei UI", 10),
                   wraplength=420, justify="left")
    detail.pack(padx=18, anchor="w")
    left = Label(root, text="", fg="#e0a35c", bg="#0f1115", font=("Microsoft YaHei UI", 9))
    left.pack(padx=18, anchor="w", pady=(6, 0))
    # 后果说明区：内容整段来自服务端那一份 notice（谁都不许在这里写第二份"不能撤回"）
    nhead = Label(root, text="", fg="#e0a35c", bg="#0f1115", font=("Microsoft YaHei UI", 9, "bold"))
    nhead.pack(padx=18, anchor="w", pady=(10, 2))
    nbody = Label(root, text="", fg=fg, bg="#0f1115", font=("Microsoft YaHei UI", 9),
                  wraplength=430, justify="left")
    nbody.pack(padx=18, anchor="w")
    box = Frame(root, bg="#0f1115")
    box.pack(pady=16)
    state = {"done": False}

    def act(kind):
        # 🔴 只有真实点击走到这里；没有任何定时器会调用它 ⇒ 不会自签
        r = _rpc(port, "/api/spend/%s" % kind, {"pending_id": pid})
        if r.get("ok"):
            title.config(text=("已批准，开始跑" if kind == "approve" else "已拒绝，这条不会跑"))
            detail.config(text="窗口 3 秒后自动关闭。额度状态以 gateway.py approvals 为准。")
        else:
            detail.config(text="没签成：%s（条子可能已过期或已被别处点过）"
                          % (r.get("error") or r.get("reason") or r.get("_err") or "未知"))
        state["done"] = True
        root.after(3000, root.destroy)

    ok_btn = Button(box, text="批准，开始跑", command=lambda: act("approve"), bg="#1f8a5f", fg="#fff",
                    activebackground="#26a874", relief="flat", padx=18, pady=8,
                    font=("Microsoft YaHei UI", 11, "bold"))
    ok_btn.pack(side="left", padx=(0, 10))
    Button(box, text="先不跑", command=lambda: act("deny"), bg="#2a2f3a", fg=fg,
           activebackground="#353b47", relief="flat", padx=14, pady=8,
           font=("Microsoft YaHei UI", 11)).pack(side="left")
    # 🔴 Enter/Space 一律不触发按钮：键盘路过不该等于点头
    root.bind_all("<Return>", lambda e: "break")
    root.bind_all("<KP_Enter>", lambda e: "break")
    root.bind_all("<space>", lambda e: "break")

    def tick():
        row = fetch_pending(port, pid)
        if not row:
            if not state["done"]:
                title.config(text="这条待批已经不在了")
                detail.config(text="过期或被别处处理过 ⇒ 0 额度、不会提交（fail-closed）。")
                root.after(4000, root.destroy)
                state["done"] = True
            return
        cap = "%s/%s" % (row.get("capability"), row.get("purpose"))
        title.config(text="%s · %s 段 · 上限 %s 分" % (cap, row.get("count"), row.get("credits")))
        detail.config(text="要做的：%s\n账户：%s" % (row.get("scope") or "（没写范围）", row.get("account")))
        # 🔴「点下去之后」这段后果说明只有一份（gateway.approval_notice，随 /api/spend/pending 下发）
        #    ⇒ 窗口里不许自己写一句"不能撤回"，也不许在批准页 HTML 里再写一遍：两处迟早漂。
        notice = row.get("notice") or {}
        bullets = notice.get("bullets") or []
        if bullets:
            nhead.config(text=notice.get("headline") or "点下去之后")
            nbody.config(text="\n".join("· " + b.replace("🔴 ", "") for b in bullets))
            if notice.get("button"):
                ok_btn.config(text=notice["button"])
        sec = int(row.get("left_seconds") or 0)
        left.config(text="剩余 %d 分 %d 秒后自动作废（作废 = 拒绝，不是默认同意）" % (sec // 60, sec % 60))
        root.after(POLL_MS, tick)

    tick()
    place_centered(root)                  # 🔴 居中：这张窗就是人证本身，落在角落里等于没弹
    root.lift()
    root.focus_force()
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
