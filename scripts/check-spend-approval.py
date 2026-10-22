#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""花钱批准闸的回归（0 消耗、纯判据）。

为什么要有这一份：`backend/gateway.py` 里那行注释早就写着"判据与回归：scripts/check-spend-approval.py"，
但这份文件此前【不存在】——文档承诺了一道没写的闸（最坏的那类：看着有人守着，其实没有人）。
钉的六件事，一条都不能少：
  G1 全局免批白名单里不许混进任何"会产出东西"的能力（默认要批是反向写的）
  G2 DSH 入口（没有来源标记）⇒ video.generate 每条腿都要批
  G3 工作台入口（WEEKSIR_ENTRY=workbench）⇒ 只有 video.generate 免批
  G4 免批不许外溢：video.edit / lipsync / image.edit 在两个入口都要批
  G5 反向对照：把工作台免批表清空 ⇒ 工作台也要批（证明真在读那张表，不是恒真）
  G6 来源身份只能由启动脚本给（start_8090.cmd 里那行必须在），且不认自报/模糊值
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("WEEKSIR_ENTRY", None)          # 判活不依赖跑它的人的环境
from backend import gateway as gw              # noqa: E402

PRODUCING = ("video.generate", "video.edit", "lipsync", "image.edit", "audio.generate", "transcribe")
fails = []


def ck(name, ok, detail=""):
    print("%-4s %s%s" % ("OK" if ok else "FAIL", name, ("  ← " + detail) if detail and not ok else ""))
    if not ok:
        fails.append(name)


# G1 —— 全局白名单只准推理类
mixed = [c for c in PRODUCING if c in gw.SPEND_APPROVAL_EXEMPT]
ck("G1 全局免批白名单没混进产出类", not mixed, str(mixed))

vid = [p for (c, p) in gw.ROUTES if c == "video.generate"]

# G2 —— DSH 入口：全都要批
ck("G2 无来源标记时 video.generate 每条腿都要批",
   all(gw.needs_spend_approval("video.generate", p) for p in vid) and gw.needs_spend_approval("video.generate", "*"),
   "漏在: %s" % [p for p in vid if not gw.needs_spend_approval("video.generate", p)])

# G3 + G4 —— 工作台入口：只免这一格
os.environ["WEEKSIR_ENTRY"] = "workbench"
try:
    ck("G3 工作台入口 video.generate 免批",
       not any(gw.needs_spend_approval("video.generate", p) for p in vid))
    bled = [c for c in ("video.edit", "lipsync", "image.edit") if not gw.needs_spend_approval(c, "*")]
    ck("G4 免批没外溢到别的产出类", not bled, str(bled))
    # G5 —— 反向对照：清掉那张表 ⇒ 同一入口必须回到"要批"
    backup = gw.SPEND_APPROVAL_EXEMPT_WORKBENCH
    try:
        gw.SPEND_APPROVAL_EXEMPT_WORKBENCH = ()
        rev = gw.needs_spend_approval("video.generate", "wan3-prime")
    finally:
        gw.SPEND_APPROVAL_EXEMPT_WORKBENCH = backup
    ck("G5 反向对照：清空表后工作台也要批（判据真在读表）", rev is True)
    # G6 —— 自报/模糊值不算来源
    os.environ["WEEKSIR_ENTRY"] = "workbench-by-agent"
    ck("G6 模糊值不认（自报身份不是安全判据）",
       gw.needs_spend_approval("video.generate", "wan3-prime") is True)
finally:
    os.environ.pop("WEEKSIR_ENTRY", None)

launcher = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "start_8090.cmd")
raw = open(launcher, "rb").read().decode("utf-8", "replace") if os.path.exists(launcher) else ""
ck("G6b 工作台启动脚本确实写下来源（否则免批物理上不可能生效）",
   'set "WEEKSIR_ENTRY=workbench"' in raw)

print("\nCHECK-%s  (%d 条不过)" % ("PASS" if not fails else "FAIL", len(fails)))
sys.exit(1 if fails else 0)
