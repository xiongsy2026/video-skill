# -*- coding: utf-8 -*-
"""skill 执行合同与回执 —— 根治"agent 驱动 skill 时按自己理解跑"。

设计见 C:\\dsh\\docs\\ADR-skill-contract.md。一句话：**绕开合同必须失败，且失败不了就必须现形。**

三层共用这一份判据（谁都不许抄第二份）：
  · 入口开工先调 open_receipt() 落一份全 ⬜ 骨架，之后【每做完一步当场 tick() 一个钩】
    ⇒ productions/<task>/.skill_receipt.json 是从头长到尾的，中途崩了也看得见"打到第几格"
  · 花钱那一刀调 verify_or_raise()：无回执 / 版本不符 / 输入指纹对不上 ⇒ 抛，不提交
  · 交付（deliver_pack.py）调 summarize()：把缺步与偏离原样印进交付消息

🔴 上下文怎么传：入口在**同进程**里设 SKILL_CONTRACT_CTX={"skill","task_dir"}。
   不经入口直接调管线 ⇒ 读不到这个变量 ⇒ 按"没有回执"拒。这正是我们要的性质：
   绕过入口 = 提交不出去，而不是"没人拦"。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time

BS = chr(92)
RECEIPT_NAME = ".skill_receipt.json"
CTX_ENV = "SKILL_CONTRACT_CTX"
WORKSPACE = os.environ.get("DSH_WORKSPACE") or r"C:\dsh"
SKILLS_DIR = os.path.join(WORKSPACE, ".dsh", "skills")

# 意图档（会花钱、有流程可跑偏）⇒ 必须有合同。权威源是 hypit_bridge.KNOWN_SKILLS，
# 这里只登记"档名 → skill 目录名"，由 scripts/check-skill-contract.py 钉两份对齐。
LANE_DIRS = {
    "omni": "omni",
    "hypit": "hypit-pipeline",
    "super-clone": "super-clone",
    "product-video": "product-video-pipeline",
    "motion-transfer": "motion-transfer",
    "prompt-reverse": "prompt-reverse",
    "film": "ai-film-router",
}


class SkillContractError(RuntimeError):
    """合同不满足 ⇒ 花钱那一刀不许落。文案点名"回去跑哪个入口"，不许只说"不行"。"""


def sha8(path: str) -> str:
    try:
        h = hashlib.sha1()
        with open(path, "rb") as f:
            for blk in iter(lambda: f.read(1 << 20), b""):
                h.update(blk)
        return h.hexdigest()[:8]
    except Exception:                                        # noqa: BLE001
        return "unreadable"


def _split_row(line: str) -> dict:
    out = {}
    for cell in line.split("|"):
        c = cell.strip()
        if not c:
            continue
        if "=" in c:
            k, v = c.split("=", 1)
            out[k.strip()] = v.strip()
        elif "required" in c.lower() or c in ("必需", "必填"):
            out["required"] = "1"
    return out


def _scalar(line: str) -> str:
    """取 `k: v` 的 v，并剥掉行尾注释（`v   # 说明`）。
    为什么要剥：合同值是要拿去和网关 ROUTES 比对的字符串，注释混进值里 ⇒ 比对永远对不上
    （2026-10-10 实测：spend 后面写了一句解释，plan_table 和三方闸一起被带沟里）。"""
    v = line.split(":", 1)[1]
    v = re.split(r"\s+#", v, maxsplit=1)[0]
    return v.strip()


def parse_contract(text: str) -> dict:
    """从 SKILL.md 里抠 `contract:` 段。行格式与 form: 同族：`k=v | k=v`。"""
    m = re.search(r"(?m)^contract:\s*$", text)
    if not m:
        return {}
    body = text[m.end():]
    stop = re.search(r"(?m)^[A-Za-z_][A-Za-z0-9_]*:\s*$|^---\s*$|^#{1,2} ", body)
    if stop:
        body = body[:stop.start()]
    c = {"version": "", "entry": "", "spend": "", "steps": []}
    for line in body.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        low = s.lower()
        if low.startswith("version:"):
            c["version"] = _scalar(s)
        elif low.startswith("entry:"):
            c["entry"] = _scalar(s)
        elif low.startswith("spend:"):
            c["spend"] = _scalar(s)
        elif s.startswith("-"):
            row = _split_row(s[1:])
            if row.get("id"):
                c["steps"].append({"id": row["id"], "proof": row.get("证明") or row.get("proof") or "",
                                   "pred": row.get("判据") or row.get("pred") or "",
                                   "impl": row.get("手段") or row.get("impl") or "",
                                   "required": row.get("required", "1") != "0"})
    return c


def contract_shape_problems(contract: dict) -> list:
    """🔴 条款形状闸：一条 step 必须【要么给得出物证，要么限定手段】，两者都没有 = 把手段交给 agent。

    为什么单独钉这一条（2026-10-09 事故）：合同里写了"删除不希望继承的烧录字幕/水印"这种
    **纯结果条款** —— 结果已知、手段未知，agent 就必然拿"我想到一个能干活的办法"去填空隙
    （那次是手搓 ffmpeg 去字幕 + 差点编出"内置去字幕模型"）。
    所以"合同没写手段"不是文档不够细，是**合同把决定权让渡出去了**，必须让它红。
    """
    bad = []
    for st in (contract or {}).get("steps", []):
        sid = str(st.get("id") or "?")
        has_proof = bool(str(st.get("proof") or "").strip())
        has_impl = bool(str(st.get("impl") or "").strip())
        if not has_proof and not has_impl:
            bad.append("步 %s 既无『证明=』也无『手段=』⇒ 手段由执行者自由发挥（合同在此处让渡了决定权）" % sid)
    return bad


def impl_problems(contract: dict, used_impl: dict) -> list:
    """白名单核验：`used_impl = {step_id: 实际用的手段}`。
    合同给某步声明了 `手段=`（逗号分隔白名单）⇒ 不在名单里的实现就是**自造手段**，报出来。
    没声明 `手段=` 的步不判（那种靠物证判；两样都没有的由 contract_shape_problems 拦在合同层）。
    """
    bad = []
    for st in (contract or {}).get("steps", []):
        allow = [a.strip() for a in str(st.get("impl") or "").split(",") if a.strip()]
        if not allow:
            continue
        got = str((used_impl or {}).get(st["id"]) or "").strip()
        if not got:
            bad.append("步 %s 限定了手段 %s，但没申报用了哪个" % (st["id"], allow))
        elif got not in allow:
            bad.append("步 %s 的手段 %r 不在合同白名单 %s 内 ⇒ 自造手段，不许执行" % (st["id"], got, allow))
    return bad


def check_proof(pred: str, evidence: str, other_evidence: dict | None = None) -> str:
    """🔴 "证明"不能只是"文件存在" —— 那是自证：管线自己写一行 prompt.txt 就算过了 Step1.5，
    而真实事故形状正是【跳过 Step1 分镜、直接切整片当 ref、喂简化 user 段】照样交出去。
    所以这里加"内容判据"：只有真跑了那一步才会成立的形状。逗号分隔可叠多条。

      json-nonempty                  文件非空（>0 字节）
      json-list-min:<key>=<n>        JSON 里该 key 是 list 且长度 ≥ n
      json-key:<key>=<value>         JSON 某字段等于给定值
      bind-sha8:<step_id>:<key>      本 JSON 的 <key> 必须等于【另一条步证据文件】的 sha8
                                     （把"编译产物"钉死在"这一步的输入"上：跳步 ⇒ 绑不上 ⇒ 拒）
    返回 "" = 通过；否则返回人话原因。判据写错/文件读不出 ⇒ 一律算不通过（宁可拒也不放行）。
    """
    if not pred:
        return ""
    if not evidence or not os.path.isfile(evidence):
        return "证据文件不在：" + str(evidence or "(空)")
    for one in [p.strip() for p in str(pred).split(",") if p.strip()]:
        try:
            if one == "json-nonempty":
                if os.path.getsize(evidence) <= 0:
                    return "证据文件是空的"
                continue
            with open(evidence, "r", encoding="utf-8", errors="replace") as f:
                raw = f.read()
            if one.startswith("text-has="):
                # 原文里必须出现这些片段（分号分隔）—— 用来钉"合同说的禁令真的进了产物"。
                # 例：负向锁定（字幕/水印不生成）是写在 prompt 里的，那 prompt.txt 里就必须找得到；
                #     这条比"文件存在"硬，又不用假设产物结构，且【真跑过的样本实测命中】才敢用。
                frags = [x for x in one.split("=", 1)[1].split(";") if x.strip()]
                for fr in frags:
                    if fr.strip() not in raw:
                        return "产物里找不到片段 %r ⇒ 该约束没真进产物" % fr.strip()
                continue
            if one.startswith("json-array-min="):
                # 根就是数组的产物（storyboard.json 就是 shot 列表）：只判"文件在"等于没判。
                # 🔴 这里原来私货加了一条"每个 shot 必须有顶层 start/end"，2026-10-10 删掉，三条理由：
                #    ① 合同那行只声明 `判据=json-array-min=1`（.dsh/skills/super-clone/SKILL.md:373）——
                #       实现不许给判据加合同没写的条件（与"合同没写手段的地方也不许自己发明手段"同一条）；
                #    ② 流水线产出的 canonical 形状【从来没有】顶层 start/end：时间住在 时间区间 里
                #       （normalize_storyboard_time 只 snap 它，全仓唯一读法是 _shot_time_range）⇒ 真跑必拒；
                #    ③ 闸当时全绿是因为 fixture 由闸自己造了 {"start":…,"end":…}（自证自，见 T17/T34 的订正）。
                #    "这份就是编译器吃进去的那一份"由下一步 bind-sha8 钉（SKILL.md:375），不靠字段名存在与否。
                n = int(one.split("=", 1)[1])
                v = json.loads(raw or "[]")
                if not isinstance(v, list) or len(v) < n:
                    return "判据 %s 不成立（实际 %s）" % (one, "不是数组" if not isinstance(v, list) else "%d 条" % len(v))
                continue
            if one.startswith("json-list-min:"):
                key, _, n = one.split(":", 1)[1].partition("=")
                v = json.loads(raw or "{}")
                lst = v.get(key.strip()) if isinstance(v, dict) else None
                if not isinstance(lst, list) or len(lst) < int(n):
                    return "判据 %s 不成立（实际 %s）" % (one, "无" if lst is None else "%d 条" % len(lst))
                continue
            if one.startswith("json-key:"):
                key, _, want = one.split(":", 1)[1].partition("=")
                v = json.loads(raw or "{}")
                if str((v.get(key.strip()) if isinstance(v, dict) else None)) != want.strip():
                    return "判据 %s 不成立（实际 %r）" % (one, v.get(key.strip()) if isinstance(v, dict) else None)
                continue
            if one.startswith("bind-sha8:"):
                step_id, _, key = one.split(":", 1)[1].partition(":")
                other = (other_evidence or {}).get(step_id.strip())
                if not other or not os.path.isfile(other):
                    return "绑定的那一步(%s)没有证据文件 ⇒ 这一步可能根本没跑" % step_id.strip()
                v = json.loads(raw or "{}")
                got = str(v.get(key.strip()) or "") if isinstance(v, dict) else ""
                if not got or got != sha8(other):
                    return ("编译产物的 %s(%r) 与 %s 的当前指纹(%s)不符 ⇒ "
                            "prompt 不是由这份 storyboard 编出来的（跳步或事后换料）"
                            % (key.strip(), got, step_id.strip(), sha8(other)))
                continue
            return "不认识的判据 %r ⇒ 按不通过算（判据写错不许变成放行）" % one
        except Exception as exc:                               # noqa: BLE001
            return "判据 %s 读不出：%s" % (one, str(exc)[:80])
    return ""


def contract_for(skill: str) -> dict:
    d = LANE_DIRS.get(skill)
    if not d:
        return {}
    p = os.path.join(SKILLS_DIR, d, "SKILL.md")
    if not os.path.isfile(p):
        return {}
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        return parse_contract(f.read())


def skill_for_spend(capability: str, purpose: str) -> str:
    """由【花钱那一刀】反查这条 spend 属于哪条合同 ⇒ 报价消息能自带"要走哪几步、每步授权什么手段"。

    🔴 为什么要挂在网关上而不是靠 agent 记得跑 `contract_tables.py`（2026-10-10 现网）：
       三条会话的报价都没带计划表 —— 可跳过的步骤等于不存在的步骤。approve 自己把表打出来，
       agent 复制那条报价就一定带上，人不至于只看价格就点头。
    匹配 `contract.spend`（形如 `video.generate/wan3-prime`）：精确命中优先，只写能力名的当兜底；
    命中 0 条或多条 ⇒ 回空串（调用方据此不提表，绝不编一张别人的表）。
    """
    cap = str(capability or "").strip()
    want = "%s/%s" % (cap, str(purpose or "").strip())
    exact, loose = [], []
    for lane in sorted(LANE_DIRS):
        sp = str((contract_for(lane) or {}).get("spend") or "").strip()
        if not sp:
            continue
        if sp == want:
            exact.append(lane)
        elif sp == cap:
            loose.append(lane)
    hit = exact or loose
    return hit[0] if len(hit) == 1 else ""


def ctx() -> dict:
    raw = os.environ.get(CTX_ENV) or ""
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except Exception:                                        # noqa: BLE001
        return {}


def set_ctx(skill: str, task_dir: str, session: str = "") -> None:
    """常驻服务（8090）里没有 DSH_SESSION_ID ⇒ 由入口把会话显式带进来；不带就照实记"未知"。"""
    os.environ[CTX_ENV] = json.dumps({"skill": skill, "task_dir": os.path.abspath(task_dir),
                                      "session": session or ""}, ensure_ascii=False)


NO_MODEL = "不用模型"          # 确实不经模型的步（本地 ffmpeg / 后端常量 / 纯对账）要明写这一句，不许留空


def _me() -> str:
    return os.environ.get("DSH_SESSION_ID") or str(ctx().get("session") or "")


def _dump_receipt(rec: dict, task_dir: str) -> str:
    """原子落盘 + 撞句柄重试 4 次。返回 ""=写成了，否则原因。

    🔴 回执写失败【不许抛】：它是留痕，不是新故障点（同网关那条素材台账）。
       但也不许静默 —— 原因原样回给调用方打印，人看得见"这一格没记上"。"""
    p = os.path.join(task_dir, RECEIPT_NAME)
    try:
        os.makedirs(task_dir, exist_ok=True)
    except Exception as exc:                                     # noqa: BLE001
        return "目录建不出：%s: %s" % (type(exc).__name__, str(exc)[:100])
    tmp = "%s.%d.tmp" % (p, os.getpid())                         # pid 进临时名：多段并发不许互相踩
    last = ""
    for _ in range(4):
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, indent=1))
            os.replace(tmp, p)
            return ""
        except Exception as exc:                                 # noqa: BLE001
            last = "%s: %s" % (type(exc).__name__, str(exc)[:120])
            time.sleep(0.25)
    try:
        os.remove(tmp)
    except OSError:
        pass
    return last


def _skeleton(skill: str, task_dir: str, inputs=(), contract: dict | None = None) -> dict:
    """内存里的【全 ⬜】骨架：合同声明的每一步都先在表上占一行，做一件打一件。"""
    c = contract or contract_for(skill)
    return {"kind": "skill_receipt", "skill": skill, "contract_version": c.get("version", ""),
            "entry": c.get("entry", ""), "task_dir": os.path.abspath(task_dir),
            "session": _me(), "ts": round(time.time(), 3),
            "inputs": [{"path": os.path.abspath(x), "sha8": sha8(x)} for x in inputs if os.path.isfile(x)],
            "steps": [{"id": st["id"], "ok": False, "required": st["required"], "impl": "", "model": "",
                       "pred": st.get("pred") or "", "evidence": "", "evidence_sha8": "",
                       "session": "", "ts": None} for st in c.get("steps", [])],
            "deviations": []}


def open_receipt(skill: str, task_dir: str, inputs=(), contract: dict | None = None) -> dict:
    """开工第一行动它：落一份还没打钩的骨架 ⇒ 回执从【跑完才有】变成【一直在长】。

    🔴 为什么要开工就落盘（2026-10-12 用户定"回执的写法是每一次进行打钩"）：原来只在提交前一次性写，
       中途崩了盘上要么没文件、要么是上一次那份旧的 ⇒ "跑到第几步"没人答得出。
    · 同一档同一合同版本已经开过 ⇒ 只补输入指纹，**不清钩**（重跑同一个段目录不该抹掉历史打钩）；
    · 合同改过版 ⇒ 旧回执本来就不算数（check_receipt 按版本拒），这里重建骨架。
    """
    c = contract or contract_for(skill)
    rec = load_receipt(task_dir)
    if rec and rec.get("skill") == skill and rec.get("contract_version") == c.get("version"):
        fresh = [{"path": os.path.abspath(x), "sha8": sha8(x)} for x in inputs if os.path.isfile(x)]
        if fresh:
            rec["inputs"] = fresh
            _dump_receipt(rec, task_dir)
        return rec
    rec = _skeleton(skill, task_dir, inputs=inputs, contract=c)
    _dump_receipt(rec, task_dir)
    return rec


def tick(skill: str, task_dir: str, step_id: str, evidence=None, model: str = "",
         impl: str = "", contract: dict | None = None) -> dict:
    """🔴 做完一步当场打一个钩。这一步做了 = 打钩；做不到 = 这一格还是 ⬜，交付时人看得见。

    三条硬规矩（都是"缺一样就不算这一步"，不是提示）：
      ① `step_id` 必须在合同 steps 里 ⇒ 合同外的一步打了钩等于伪造一张合格表；
      ② `model` 必须给 ⇒ 用户要的正是"每一格都有模型名"。只许现读：网关腿 `gateway.lane_model()`，
         账本外直连腿 `_llm_stamp()`，确实不经模型写 `NO_MODEL`；
      ③ 物证要么是真存在的文件，要么显式 `True`（内联证据）⇒ 空字符串/不存在的路径不算。
    返回 {"ok","row","error","written"}；**不抛**（留痕不许变成故障点），失败由调用方打印。
    署名沿用：同一格物证与模型都没变 ⇒ 保留第一次的会话与时间（台账记事实，不是记最后碰文件的人）。"""
    c = contract or contract_for(skill)
    st = next((x for x in c.get("steps", []) if x.get("id") == step_id), None)
    if st is None:
        return {"ok": False, "error": "步 %r 不在合同（%s）的 steps 里 ⇒ 没打钩：合同外的一步打上钩，"
                                      "等于替这条流程伪造一格合格状态" % (step_id, c.get("version") or "?")}
    if (not evidence) or (isinstance(evidence, str) and not evidence.strip()):
        why_model = "步 %s 没给物证 ⇒ 没打钩（合同要求：%s）" % (step_id, st.get("proof") or "无说明")
    elif isinstance(evidence, str) and not os.path.isfile(evidence):
        why_model = "步 %s 的物证文件不在（%s）⇒ 没打钩" % (step_id, evidence)
    elif not str(model or "").strip():
        why_model = ("步 %s 没给模型名 ⇒ 没打钩。这一格只许现读：网关腿用 gateway.lane_model"
                     "(capability, purpose)，账本外直连腿用 _llm_stamp(client, model)；"
                     "确实不经模型就写 %r" % (step_id, NO_MODEL))
    else:
        why_model = ""
    rec = load_receipt(task_dir)
    if not rec or rec.get("skill") != skill or rec.get("contract_version") != c.get("version"):
        rec = open_receipt(skill, task_dir, contract=c)

    def _clear(why: str) -> dict:
        """🔴 打不上钩 ⇒ 这一格必须当场抹成 ⬜（连同上一次的物证/指纹/模型名）。

        不清就是假钩：重跑同一段时编译器没报模型名，这一格却还挂着【上一次】的 ok=True
        ⇒ 交付表说"这步做了"，而这一次它根本没被核验过。抹掉之后必需步缺失 ⇒ 花钱那一刀拒。"""
        rr = load_receipt(task_dir) or _skeleton(skill, task_dir, contract=c)
        for x in rr.get("steps", []):
            if x.get("id") == step_id:
                x.update({"ok": False, "evidence": "", "evidence_sha8": "", "model": "",
                          "impl": "", "session": "", "ts": None, "why": why})
        rr["session"] = _me()
        rr["ts"] = round(time.time(), 3)
        _dump_receipt(rr, task_dir)
        return {"ok": False, "error": why, "cleared": True}

    if why_model:
        return _clear(why_model)
    if isinstance(evidence, str) and evidence.strip():
        cell, path = os.path.abspath(evidence), os.path.abspath(evidence)
    else:
        cell, path = "inline", None
    row = next((r for r in rec.get("steps", []) if r.get("id") == step_id), None)
    if row is None:                                    # 合同新加的一步在旧骨架里没有 ⇒ 补一行再打钩
        row = {"id": step_id, "ok": False, "required": st["required"], "impl": "", "model": "",
               "pred": st.get("pred") or "", "evidence": "", "evidence_sha8": "", "session": "", "ts": None}
        rec["steps"].append(row)
    sha = sha8(path) if path else ""
    same = (bool(row.get("ok")) and row.get("evidence") == cell and row.get("evidence_sha8") == sha
            and row.get("model") == str(model) and str(row.get("impl") or "") == str(impl or ""))
    old_session, old_ts = str(row.get("session") or ""), row.get("ts")
    row.pop("why", None)                               # 上一次打不上钩的原因，打上钩就作废
    row.update({"ok": True, "required": st["required"], "pred": st.get("pred") or "",
                "evidence": cell, "evidence_sha8": sha, "model": str(model),
                "impl": str(impl or ""), "session": _me(), "ts": round(time.time(), 3)})
    if same and old_session:
        row["session"], row["ts"] = old_session, old_ts
    rec["session"] = _me()          # 拿不到就留空 ⇒ 表里那一列明写"未知"，绝不拿上一份回执的头替这次署名
    rec["ts"] = round(time.time(), 3)
    why = _dump_receipt(rec, task_dir)
    return {"ok": True, "row": row, "receipt": rec, "error": why, "written": not why}


def note_deviation(task_dir: str, step_id: str, reason: str) -> dict:
    """跳过某步要当场写明原因 —— 豁免不许静默（check_receipt 对没原因的偏离直接拒）。"""
    rec = load_receipt(task_dir)
    if not rec:
        return {"ok": False, "error": "还没开工（没有回执）⇒ 偏离没处记：先调 open_receipt()"}
    rs = str(reason or "").strip()
    if not rs:
        return {"ok": False, "error": "偏离 %s 没写原因 ⇒ 不许记（豁免必须看得见）" % step_id}
    rec["deviations"] = [d for d in rec.get("deviations", [])
                         if isinstance(d, dict) and d.get("step") != step_id]
    rec["deviations"].append({"step": step_id, "reason": rs, "ts": round(time.time(), 3),
                              "session": _me()})
    why = _dump_receipt(rec, task_dir)
    return {"ok": not why, "receipt": rec, "error": why}


def write_receipt(skill: str, task_dir: str, steps: dict, inputs=(), deviations=(),
                  impl: dict | None = None, contract: dict | None = None,
                  models: dict | None = None) -> dict:
    """批量形状：open_receipt + 逐格 tick，**没有第二套打钩规则**。
    steps = {step_id: 证据路径或 True}；models = {step_id: "模型名@provider · 账本内外"}。

    🔴 缺模型名的那一格现在【打不上钩】而不是"少填一列"：回执说了"这步做了"却答不出哪个模型跑的，
       交付时人问"哪个模型跑的"仍然只能靠猜（2026-10-10 就是这一问）。
    没打的格不是错误 —— 但必需步没打 ⇒ check_receipt 报"缺少步" ⇒ verify_or_raise 在花钱前拒。"""
    c = contract or contract_for(skill)
    open_receipt(skill, task_dir, inputs=inputs, contract=c)
    req = {st["id"]: bool(st["required"]) for st in c.get("steps", [])}
    errs = []
    for sid, ev in (steps or {}).items():
        # 连"没给物证"也要走一遍 tick ⇒ 那一格被抹成 ⬜（不许留着上一次的旧钩当这次的合格状态）
        r = tick(skill, task_dir, sid, evidence=ev, model=str((models or {}).get(sid) or ""),
                 impl=str((impl or {}).get(sid) or ""), contract=c)
        why = r.get("error") or ("" if r.get("ok") else "没打上钩")
        if why and (not r.get("ok") or req.get(sid, True)):
            errs.append("%s：%s" % (sid, why))
    if deviations:
        rec = load_receipt(task_dir)
        rec["deviations"] = [d for d in deviations if isinstance(d, dict)]
        _dump_receipt(rec, task_dir)
    rec = load_receipt(task_dir)
    if errs:
        rec["tick_errors"] = errs
    return rec


def load_receipt(task_dir: str) -> dict:
    p = os.path.join(task_dir or "", RECEIPT_NAME)
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, dict) and v.get("kind") == "skill_receipt" else {}
    except Exception:                                        # noqa: BLE001
        return {}


def check_receipt(skill: str, receipt: dict, inputs=(), contract: dict | None = None) -> list:
    """返回【问题清单】，空表 = 合格。判据与 verify_or_raise 同源，交付打印也用它。"""
    c = contract or contract_for(skill)
    bad = []
    if not c.get("steps"):
        return ["skill %r 没有流程合同（contract: 段缺失或 steps 为空）⇒ 这一档还没被保护" % skill]
    if not receipt:
        return ["没有回执 ⇒ 这条产物不是从声明的入口出来的。要出片请跑：%s" % (c.get("entry") or "(入口未声明)")]
    if receipt.get("skill") != skill:
        bad.append("回执属于 %r，不是 %r" % (receipt.get("skill"), skill))
    if receipt.get("contract_version") != c.get("version"):
        bad.append("合同版本不符：回执=%r 当前=%r（合同改过 ⇒ 旧回执作废，重跑入口）"
                   % (receipt.get("contract_version"), c.get("version")))
    have = {s.get("id"): s for s in receipt.get("steps", [])}
    # 给 bind-sha8 用：步 id -> 证据文件路径（只有 ok 且真是文件的才算）
    ev_of = {sid: r.get("evidence") for sid, r in have.items()
             if isinstance(r, dict) and r.get("ok") and isinstance(r.get("evidence"), str)
             and os.path.isfile(r.get("evidence"))}
    waived = {str(d.get("step")): str(d.get("reason") or "").strip()
              for d in receipt.get("deviations", []) if isinstance(d, dict)}
    for st in c["steps"]:
        if not st["required"]:
            continue
        got = have.get(st["id"])
        if not got or not got.get("ok"):
            reason = waived.get(st["id"])
            if reason:
                continue                                     # 人写明了原因 ⇒ 允许，但仍会被打印
            bad.append("缺少步 %s（合同要求：%s）" % (st["id"], st.get("proof") or "无说明"))
        elif got.get("evidence_sha8") and got.get("evidence") and os.path.isfile(got["evidence"]):
            if sha8(got["evidence"]) != got.get("evidence_sha8"):
                bad.append("步 %s 的证据文件在回执之后被改过（sha8 不符）⇒ 手写替换过的嫌疑" % st["id"])
        # 🔴 打了钩就必须有模型名（2026-10-12 用户口径："回执的写法是每一次进行打钩，有模型名"）。
        #    tick() 已经把这条守在【打钩那一刻】；这里再钉一次，是因为回执是一个 JSON 文件 ——
        #    手改出来的 ok=True + 空模型，和手写台账行伪造"我看过图了"是同一种形状（2026-10-12 现网真发生过）。
        if got and got.get("ok") and not str(got.get("model") or "").strip():
            bad.append("步 %s 打了钩却没记模型名 ⇒ 这一格不算数（现读 gateway.lane_model / "
                       "_llm_stamp；确实不经模型就写 %r）" % (st["id"], NO_MODEL))
        # 🔴 内容判据：文件在、sha 没动，还不够 —— 还得是"真跑了那一步才会有的形状"
        _pred = (got or {}).get("pred") or st.get("pred") or ""
        if _pred and got and got.get("ok"):
            why = check_proof(_pred, ev_of.get(st["id"], ""), ev_of)
            if why:
                bad.append("步 %s 的物证不合格：%s（合同判据：%s）" % (st["id"], why, _pred))
    for d in receipt.get("deviations", []):
        if isinstance(d, dict) and not str(d.get("reason") or "").strip():
            bad.append("偏离 %s 没写原因 ⇒ 豁免不许静默" % d.get("step"))
    # 🔴 手段白名单在这里自动判（回执里带 impl 申报）：合同限定过手段的步，申报缺失或不在名单 ⇒ 拒。
    #    放在 check_receipt 而不是让调用方自己记得调 impl_problems —— 靠自觉的判据等于没有判据。
    used_impl = {s.get("id"): s.get("impl") for s in receipt.get("steps", []) if isinstance(s, dict)}
    bad += impl_problems(c, used_impl)
    want = {os.path.abspath(x): sha8(x) for x in inputs if os.path.isfile(x)}
    got_in = {str(i.get("path")): str(i.get("sha8")) for i in receipt.get("inputs", [])}
    for path, h in want.items():
        if path not in got_in:
            bad.append("输入 %s 不在回执里（提交的文件与编译时用的不是同一批）" % os.path.basename(path))
        elif got_in[path] != h:
            bad.append("输入 %s 内容变了（回执 %s ≠ 现在 %s）⇒ 拿旧 prompt 配新素材"
                       % (os.path.basename(path), got_in[path], h))
    return bad


def verify_or_raise(skill: str | None = None, task_dir: str | None = None, inputs=()) -> dict:
    """花钱那一刀之前调它。上下文缺省时从入口设的 SKILL_CONTRACT_CTX 读。"""
    c0 = ctx()
    skill = skill or c0.get("skill") or ""
    task_dir = task_dir or c0.get("task_dir") or ""
    if not skill:
        raise SkillContractError(
            "没有合同上下文（%s 未设）⇒ 这条腿不是从 skill 声明的入口进来的。"
            "要出片请跑对应 skill 的 runner，而不是直接调管线。" % CTX_ENV)
    c = contract_for(skill)
    if not c.get("steps"):
        raise SkillContractError("skill %r 没有 contract: 段 ⇒ 无法核验流程，拒绝提交"
                                 "（先给它补合同，见 docs/ADR-skill-contract.md）" % skill)
    # 🔴 合同自己有洞（某步既无物证又无手段白名单）⇒ 也拒：那种空洞就是留给执行者自由发挥的地方。
    #    宁可挡住一次花钱，也不要在"合同没说清"的空隙里跑一条没人授权的流程。
    shape = contract_shape_problems(c)
    if shape:
        raise SkillContractError("skill %r 的合同不合格（%s）：\n  - %s\n"
                                 "先把合同补成【可执行形状】（每条要么给得出物证，要么限定手段）再提交。"
                                 % (skill, c.get("version") or "?", "\n  - ".join(shape)))
    problems = check_receipt(skill, load_receipt(task_dir), inputs=inputs, contract=c)
    if problems:
        raise SkillContractError("skill 执行合同未满足（%s）：\n  - %s\n"
                                 "入口：%s" % (skill, "\n  - ".join(problems), c.get("entry") or "?"))
    rec = load_receipt(task_dir)
    return {"ok": True, "skill": skill, "contract_version": c.get("version"),
            "waived": [d for d in rec.get("deviations", [])], "table": who_table(rec)}


def _fmt_ts(ts) -> str:
    try:
        return time.strftime("%m-%d %H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return "?"


def plan_table(skill: str) -> str:
    """🔴 提交【之前】给人看的计划表：这条流程要走哪几步、每步由谁授权、允许什么手段。
    报价那条消息必须带上它 —— 让人在花钱前就能看出"要走的步对不对"，而不是事后追认。"""
    c = contract_for(skill)
    if not c.get("steps"):
        return "（%s 没有 contract: 段 ⇒ 无法出计划表，这一档还没被保护）" % skill
    rows = ["**流程计划**（%s · 合同 %s · 花钱口 `%s`）" % (skill, c.get("version") or "?", c.get("spend") or "?"),
            "| 步 | 必需 | 授权手段（白名单） | 物证要求 |", "|---|---|---|---|"]
    for st in c["steps"]:
        rows.append("| `%s` | %s | %s | %s |" % (
            st["id"], "✅" if st["required"] else "—",
            st.get("impl") or "（未限定，靠物证判）", st.get("proof") or "—"))
    rows.append("")
    rows.append("入口：`%s`" % (c.get("entry") or "?"))
    return "\n".join(rows)


def who_table(receipt: dict) -> str:
    """🔴 这张表就是【打钩单】：合同里每一步一行，做到哪打到哪，最后一列一眼看清 已打钩 / 没打钩。
    用户 2026-10-12 定的写法："每一次进行打钩，有模型名" ⇒
      · 每一行都带【模型/路由】：现读的模型名（`gateway.lane_model` / `_llm_stamp`），不经模型写「不用模型」；
      · 没打钩的格子【照样在表上】并标 ⬜ —— 骨架是开工时落的，所以"跑到第几步"看得见，
        而不是像以前那样：中途崩了盘上要么没文件、要么是上一次那份旧的。
    署名按【行】取（不是整表取回执头部那一个）—— 多会话接力时只有按行才看得出是谁改的哪一步。
    会话 id 取不到就明写"未知"，不许留空糊过去（留空 = 事后查不到责任人）。"""
    if not receipt:
        return "（没有回执 ⇒ 这次不是从声明的入口进来的，无表可出）"
    head = receipt.get("session") or "未知（DSH_SESSION_ID 没传进这个进程）"
    steps = [s for s in receipt.get("steps", []) if isinstance(s, dict)]
    done = sum(1 for s in steps if s.get("ok"))
    need = sum(1 for s in steps if s.get("required"))
    done_need = sum(1 for s in steps if s.get("required") and s.get("ok"))
    rows = ["**流程执行表**（%s · 合同 %s · 打钩 必需 %d/%d · 全部 %d/%d · 本次写入会话 `%s` · %s）" % (
        receipt.get("skill") or "?", receipt.get("contract_version") or "?",
        done_need, need, done, len(steps), head, _fmt_ts(receipt.get("ts"))),
        "| 步 | 必需 | 谁做的（会话） | 时间 | 手段 | 模型/路由 | 物证 | 指纹 | 状态 |",
        "|---|---|---|---|---|---|---|---|---|"]
    fb_sid, fb_ts = (head.split("（")[0] or "未知"), _fmt_ts(receipt.get("ts"))
    for s in steps:
        ev = str(s.get("evidence") or "")
        rows.append("| `%s` | %s | %s | %s | %s | %s | `%s` | `%s` | %s |" % (
            s.get("id"), "✅" if s.get("required") else "—",
            (str(s.get("session") or "") or "未知（这一格的写入者没传会话）") if s.get("ok") else "—（这步没做）",
            ((_fmt_ts(s.get("ts")) if s.get("ts") else fb_ts) if s.get("ok") else "—"),
            s.get("impl") or "—",
            # 🔴 打钩了却没模型名 = 这一格不算数（check_receipt 直接拒）；没打钩的行不是"漏记模型"，
            #    它压根还没发生 ⇒ 写"—（还没做）"，绝不拿 source 函数标签那种"看起来像模型名"的东西冒充。
            ("`" + str(s.get("model")) + "`") if s.get("model") else
            ("没记" if s.get("ok") else "—（还没做）"),
            os.path.basename(ev) if ev and ev != "inline" else ("内联" if ev == "inline" else "—"),
            s.get("evidence_sha8") or "—",
            ("✅ 已打钩" if s.get("ok") else
             "⬜ 没打钩（%s…）" % str(s.get("why"))[:56] if s.get("why") else "⬜ 没打钩")))
    for d in receipt.get("deviations", []):
        if isinstance(d, dict):
            rows.append("| `%s` | ? | %s | %s | 跳过 | —（这步被跳过了） | — | — | ⚠️ %s |" % (
                d.get("step"), fb_sid, fb_ts, d.get("reason") or "（没写原因）"))
    return "\n".join(rows)


def latest_receipt(root: str, max_dirs: int = 4000) -> dict:
    """交付时手里只有 productions/<task>，回执却落在段目录里 ⇒ 往下找最新的一份。

    🔴 两道界，缺一都会出事（2026-10-10 实测：调用方把 root 算成 C:\\ ⇒ 递归整块盘，看着就是卡死）：
       ① 文件系统根（盘根/共享根）直接不扫；② 最多走 max_dirs 个目录就走人（宁可少给一张表）。"""
    best: dict = {}
    if not root or not os.path.isdir(root):
        return {}
    try:
        here = os.path.abspath(root).rstrip(BS)
        if not here or os.path.dirname(here) == here:
            return {}                                     # 盘根 ⇒ 不扫
    except Exception:                                     # noqa: BLE001
        return {}
    seen = 0
    for dp, dirs, files in os.walk(root):
        seen += 1
        if seen > max_dirs:
            break
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "__pycache__")]
        if RECEIPT_NAME in files:
            r = load_receipt(dp)
            if r and r.get("ts", 0) >= best.get("ts", 0):
                best = r
    return best


def summarize(task_dir: str) -> str:
    """给交付用：执行表（逐行署名）+ 未满足清单。没有合同上下文的产物返回空串。"""
    rec = latest_receipt(task_dir)
    if not rec:
        return ""
    skill = rec.get("skill", "")
    problems = check_receipt(skill, rec, inputs=[i.get("path", "") for i in rec.get("inputs", [])])
    tail = ("🔴 未满足：" + "；".join(problems)) if problems else "全部必需步都有证据，无未说明的偏离。"
    return who_table(rec) + "\n" + tail
