"""「生成视频」按钮那一格的料：把 `_super_clone_generate_offer` 从 app.py 整块抠出来跑真值表。

0 消耗：不提交、不建预扣、不碰批准闸，只验【什么时候该出现、报价从哪来】。
跑法：python scripts/check-super-clone-generate-offer.py
"""
import ast
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import gateway as gw                      # noqa: E402

# 真档 = 已编译、未交付的任务（b51de56b 那单 10-23 已真出片，local_path 落盘 ⇒ 不再是"没成片"样本）。
REAL_STATE = ROOT / "outputs" / "super_clone_wan3_28ba351f" / "_super_clone_state.json"


def _offer_fn():
    import io
    tree = ast.parse(io.open(str(ROOT / "app.py"), encoding="utf-8").read())
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "_super_clone_generate_offer")
    ns = {"json": json, "Path": Path}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "app.py", "exec"), ns)  # noqa: S102
    return ns["_super_clone_generate_offer"]


offer = _offer_fn()
cases = []

with tempfile.TemporaryDirectory() as td:
    # 🔴 2026-10-30 换芯 yz30 后单价是【分/秒】、实扣预扣每段带真实秒数 ⇒
    #    有 target_duration：报价 = 单价 × ceil(Σ秒)，与段数无关（Σ秒 已含全部段）；
    #    没有秒数：退回 meter 的"每段 30 秒上限估"（宁可高报）。
    Path(td, "_submit_plan.json").write_text(json.dumps({"count": 3, "target_duration": 40}), encoding="utf-8")
    g5 = offer({"prompt": "p", "local_path": None, "status": "FAILED", "output_dir": td})
    cases.append(("40 秒 3 段 ⇒ 报价=单价×40（Σ秒口径，不按段重复乘）",
                  g5["segments"] == 3 and g5["total_credits"] == g5["unit_price"] * 40, g5))
    Path(td, "_submit_plan.json").write_text(json.dumps({"count": 3}), encoding="utf-8")
    g5b = offer({"prompt": "p", "local_path": None, "status": "FAILED", "output_dir": td})
    cases.append(("3 段·秒数未知 ⇒ 按 meter 每段 30 秒上限估",
                  g5b["segments"] == 3
                  and g5b["total_credits"] == gw.meter("video.generate", "wan3-prime", {"count": 3})["credits_estimate"], g5b))

if REAL_STATE.is_file():
    real = json.loads(REAL_STATE.read_text(encoding="utf-8"))
    g = offer(real)
    # 真档 sc_086c1910135f：编译完停住（COMPILED）、盘上没成片 ⇒ 按钮必须出现，
    # 人点「生成视频」才花钱。
    cases.append(("真档：SUCCESS 但没成片 ⇒ ready", g["ready"] is True, g))
    cases.append(("真档：段数取自 _submit_plan.json", g["segments"] == 1, g))
    # 真档期望值从它自己的 _submit_plan.json 现算（不硬编码秒数——不同素材时长不同）：
    # 报价 = 单价 × ceil(target_duration)（实扣口径 = Σ秒×分/秒）。
    _pj = json.loads((REAL_STATE.parent / "_submit_plan.json").read_text(encoding="utf-8"))
    _s = float(_pj.get("target_duration") or 0.0) or float(real.get("duration") or 0.0)
    _sec = int(_s) + (1 if _s > int(_s) else 0)
    _exp = gw.meter("video.generate", "wan3-prime", {"count": 1, "duration": _sec})["credits_estimate"]
    cases.append(("真档：分数与网关 meter 同源（不硬编码价）",
                  0 < g["unit_price"] == gw.meter("video.generate", "wan3-prime", {"count": 1})["unit_price"]
                  and g["total_credits"] == _exp, g))
    delivered = dict(real)
    delivered["local_path"] = real["trimmed_path"]
    g2 = offer(delivered)
    cases.append(("反向对照：有成片 ⇒ 不 ready", g2["ready"] is False and g2["delivered"] is True, g2))
else:
    print("SKIP 真档不在（%s）⇒ 只跑合成档" % REAL_STATE)

cases += [
    ("反向对照：没有提示词 ⇒ 不 ready",
     offer({"prompt": "", "local_path": None, "status": "FAILED", "output_dir": "x"})["ready"] is False, {}),
    ("反向对照：RUNNING ⇒ 不 ready（点第二下就是双花）",
     offer({"prompt": "p", "local_path": None, "status": "RUNNING", "output_dir": "x"})["ready"] is False, {}),
    ("反向对照：提交计划缺失 ⇒ 按 1 段显示且不崩",
     offer({"prompt": "p", "local_path": None, "status": "FAILED",
            "output_dir": str(ROOT / "不存在的目录")})["segments"] == 1, {}),
]

bad = [c for c in cases if not c[1]]
for name, ok, payload in cases:
    print(("OK   " if ok else "FAIL ") + name + ("  " + json.dumps(payload, ensure_ascii=False) if payload else ""))
print("OFFER-PASS" if not bad else "OFFER-FAIL %d 条不过" % len(bad))
sys.exit(1 if bad else 0)
