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

REAL_STATE = ROOT / "outputs" / "super_clone_wan3_b51de56b" / "_super_clone_state.json"


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
    Path(td, "_submit_plan.json").write_text(json.dumps({"count": 3}), encoding="utf-8")
    g5 = offer({"prompt": "p", "local_path": None, "status": "FAILED", "output_dir": td})
    cases.append(("3 段 ⇒ 报价按段乘", g5["segments"] == 3 and g5["total_credits"] == g5["unit_price"] * 3, g5))

if REAL_STATE.is_file():
    real = json.loads(REAL_STATE.read_text(encoding="utf-8"))
    g = offer(real)
    # 现网 sc_9c246af70384：提交被批准闸拒回（0 分未扣、没建任务）却写着 SUCCESS ⇒ 按钮必须出现，
    # 否则人只能重新发起一整单（分镜 5 分钟 + 编译 16 分钟重烧）。
    cases.append(("真档：SUCCESS 但没成片 ⇒ ready", g["ready"] is True, g))
    cases.append(("真档：段数取自 _submit_plan.json", g["segments"] == 1, g))
    cases.append(("真档：分数与网关 meter 同源（不硬编码价）",
                  0 < g["unit_price"] == gw.meter("video.generate", "wan3-prime", {"count": 1})["unit_price"]
                  and g["total_credits"] == g["unit_price"], g))
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
