"""wan3.0 直出工作台的 0 消耗自检（免审批那一改的范围闸）。

跑法：C:\\...\\Python312\\python.exe -X utf8 scripts/check-wan30-studio.py
🔴 两条不许碰的：真提交（花钱出片）、真弹批准窗。所以本文件只调【纯判据 + 会被本地拒的调用】，
   批准账本指向临时文件（GATEWAY_SPEND_APPROVALS），生产账本一行都不动。
"""
import asyncio
import json
import os
import re
import sys
import tempfile
from pathlib import Path

os.environ["GATEWAY_APPROVAL_POPUP"] = "0"
os.environ["GATEWAY_SPEND_APPROVALS"] = os.path.join(tempfile.gettempdir(), "w30s_appr.json")
os.environ["GATEWAY_MEDIA_UPLOADS"] = os.path.join(tempfile.gettempdir(), "w30s_uploads.json")
for _f in (os.environ["GATEWAY_SPEND_APPROVALS"], os.environ["GATEWAY_MEDIA_UPLOADS"]):
    if os.path.isfile(_f):
        os.unlink(_f)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend import gateway as gw                                    # noqa: E402
from backend.services import wan30_studio as ws                      # noqa: E402

# 2026-10-30『tianyue lane 移除』：腿已在 model_prices.json.disabled 软下架。
# 本闸 8 步守的是【活 lane】（页面档位/免批范围/假提交），lane 不在 ⇒ 每一步都会撞 RouteError 崩掉，
# 崩≠红。短路成【有意红·待办】：恢复 lane（删 disabled 那行）后本闸自动回到守活闸，无需改闸。
if gw.is_disabled("video.generate", ws.PURPOSE):
    print("有意红(待办)：video.generate/%s 已按 2026-10-30『tianyue lane 移除』指令下架"
          "（model_prices.json.disabled）。本闸守的是活 lane，恢复后自动重新生效。" % ws.PURPOSE)
    sys.exit(3)

# 🔴 第一件事就是把最外层 socket 换成假口，并把产物目录指到临时盘 —— 在这之前【一行 studio_submit 都不许跑】。
#    为什么排在最前：本脚本会直接调 `studio_submit`，那条路在判据放行后就会起线程提交真任务。
#    2026-10-21 现网教训：网关把 720P 变成真档后，"非法分辨率"那一条用例判据当场反转成"合法 ⇒ 真提交"，
#    假口没先装就等于拿真端点试闸（花钱那一刀）。FAKE 记录一切 method，跑完统一断言没碰真主机。
net_calls: list = []
FAKE_MP4 = b"W" * 4096
ORIG_HTTP = gw._wan30_http          # 留个参照：跑完必须还是假口在场，证明整组没碰真 socket


def gw_stub(method, url, headers=None, body=None, timeout=60.0):
    net_calls.append((method, url, (body or b"").decode("utf-8", "replace")))
    if method == "POST" and url.endswith("/v1/videos"):
        return 200, {}, json.dumps({"id": "task_fake", "task_id": "task_fake", "status": "queued"}).encode()
    if "/v1/videos/" in url:
        return 200, {}, json.dumps({"id": "task_fake", "status": "completed",
                                    "url": "https://dw.tianyue.xyz/proxy/down_video?token=fake"}).encode()
    return 200, {"content-type": "video/mp4"}, FAKE_MP4


gw._wan30_http = gw_stub
import shutil                                                        # noqa: E402
ws.STUDIO_ROOT = Path(tempfile.gettempdir()) / "w30s_checkroot"      # 自检不落真产物目录
shutil.rmtree(ws.STUDIO_ROOT, ignore_errors=True)
GOOD = {"prompt": "一只橘猫在厨房台面上打哈欠，写实", "duration": 12, "resolution": "1080P",
        "ratio": "9:16", "image_urls": ["https://cdn.example.com/a.png"],
        "video_urls": ["https://cdn.example.com/b.mp4"], "audio_urls": ["https://cdn.example.com/c.wav"]}
FAILS = []


def ck(name, cond, got=""):
    line = ("  OK   " if cond else "  FAIL ") + name + ("" if cond else "  <- " + str(got)[:220])
    # 🔴 按【输出流自己的编码】转、转不了的字符换 ?：这条断言文案里全是 ⇒/≠ 这类字符，
    #    在 cp936 控制台上裸 print 会 UnicodeEncodeError ⇒ 整条闸崩在第 245 行（闸崩≠闸红，
    #    崩了的闸一条都不守，而且看起来像"跑过了"）。不要用 reconfigure 修（那会造零输出+exit0 的静默假绿）。
    import sys as _s
    enc = getattr(_s.stdout, "encoding", None) or "utf-8"
    try:
        _s.stdout.buffer.write((line + "\n").encode(enc, "replace"))
        _s.stdout.buffer.flush()
    except (AttributeError, ValueError):
        print(line.encode(enc, "replace").decode(enc, "replace"))
    if not cond:
        FAILS.append(name)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


print("== 1. 页面档位全部来自网关唯一源，且这一条【免批、账照记】 ==")
o = run(ws.studio_options())
ck("ratios=网关 6 档", o["ratios"] == list(gw._WAN30_RATIOS), o["ratios"])
ck("分辨率不单独选：页面把档位绑在模型上（2026-10-21 用户指定只留两条 prime 模型）",
   o.get("resolution_chosen_by") == "model" and "resolution_fixed" not in o, o.get("resolution_chosen_by"))
ck("页面每条标的档都在网关档位表里，且每档的【首选】都出现在页面上（本页不自己映射）",
   all(m["resolution"] in gw._WAN30_RES_MODEL for m in o["models"])
   and all(any(m["id"] == v and m["resolution"] == k for m in o["models"])
           for k, v in gw._WAN30_RES_MODEL.items()),
   [(m["id"], m["resolution"]) for m in o["models"]])
ck("两条都在网关的候选链里（哪天网关换档，页面要立刻知道而不是照跑）",
   all(m["in_lane"] for m in o["models"]), [(m["id"], m["lane_chain"]) for m in o["models"]])
ck("needs_approval=True（2026-10-22 方案1：video.generate 从免批名单摘回去了 —— 超级复刻的收口是"
   "『人点一次审批链接 ⇒ 服务端直接投』，这一格免批就等于那条链接是装饰）",
   o["needs_approval"] is True, o["needs_approval"])
ck("ledger=True（免批 ≠ 免账）", o["ledger"] is True)
ck("素材三口都列出来了", set(o["media_kinds"]) == {"video", "audio", "image"})

print("== 2. 花钱之前就得拒的几种参数（0 分 0 请求，拒因署真实身份） ==")
for name, bad, want in (("本地路径当素材", dict(GOOD, image_urls=["D:/x.png"]), "公网"),
                        ("非法画幅", dict(GOOD, ratio="7:9"), "只认"),
                        ("空提示词", dict(GOOD, prompt=""), "prompt"),
                        ("请求体塞的分辨率与所选模型的档不一致（默认 1080P 档，硬给 720P）",
                         dict(GOOD, resolution="720P"), "分辨率不单独选"),
                        ("点名已被本页摘掉的模型（PIX 不再给）",
                         dict(GOOD, model="PIX-Wan30-pro-1080p-1055-se"), "只给"),
                        ("点名网关档位里根本没有的模型",
                         dict(GOOD, model="ME-wan30-720p-1055"), "只给"),
                        ("渠道名单外的分辨率（480P 不在 ME-wan30/S-Wan30 这两档里）",
                         dict(GOOD, resolution="480P"), "分辨率不单独选")):
    try:
        run(ws.studio_submit(bad))
        ck(name + " 该被拒", False, "居然放行了（假口已先装 ⇒ 没花钱，但判据瞎了）")
    except Exception as e:
        msg = str(getattr(e, "detail", str(e)))
        ck(name + " → 400 且拒因点名『%s』" % want, want in msg, msg[:170])

print("== 3. 要批的范围：video.generate 全 %d 条腿都得要批（少一条 = 那条腿悄悄绕过人）==" % len(
      sorted(p for (c, p) in gw.ROUTES if c == "video.generate")))
# 🔴 2026-10-22 用户定案「方案1：把 video.generate 从免批名单摘回去」，推翻 10-21 那句
#    "video-skill 的工作流生视频都不需要审批"。这一组因此整体反向：原来钉"每条都免批"，
#    现在钉"每条都要批"。为什么逐条枚举而不是只钉一条：capability 级免批会一次松开 13 条腿，
#    逐条枚举才看得见"哪一条被顺手放出去了"。
vid = sorted(p for (c, p) in gw.ROUTES if c == "video.generate")
_missing = [p for p in vid if not gw.needs_spend_approval("video.generate", p)]
ck("video.generate 每一条 purpose 都要批（枚举 %d 条）" % len(vid), not _missing, _missing)
ck("超级复刻那条 wan3-prime 要批（审批链接管的就是它）",
   gw.needs_spend_approval("video.generate", "wan3-prime") is True)
ck("8090 提交口的写法 purpose='*' 也要批（那四个 _spend_or_423 靠这个才不是空转）",
   gw.needs_spend_approval("video.generate", "*") is True)
_ck_exempt_backup = gw.SPEND_APPROVAL_EXEMPT
try:
    gw.SPEND_APPROVAL_EXEMPT = tuple(_ck_exempt_backup) + ("video.generate",)
    ck("反向对照：把 video.generate 塞回免批名单 ⇒ 上面三条必须同时抓到（不然它们是空跑的绿）",
       [not [p for p in vid if not gw.needs_spend_approval("video.generate", p)],
        gw.needs_spend_approval("video.generate", "wan3-prime") is True,
        gw.needs_spend_approval("video.generate", "*") is True],
       [False, False, False])
finally:
    gw.SPEND_APPROVAL_EXEMPT = _ck_exempt_backup

print("== 4. 反向对照：只免「生图片 / 推理 / 看图」三条，别的能力不许被顺手免掉 ==")
ck("免批能力恰好是这 3 个（video.generate 已按 2026-10-22 方案1 摘回）",
   set(gw.SPEND_APPROVAL_EXEMPT) == {"image.generate", "text.complete", "video.analyze"},
   gw.SPEND_APPROVAL_EXEMPT)
ck("video.edit 仍要批", gw.needs_spend_approval("video.edit", "dewatermark") is True)
ck("lipsync 仍要批", gw.needs_spend_approval("lipsync", "default") is True)
ck("image.edit 仍要批", gw.needs_spend_approval("image.edit", "faceswap") is True)
ck("purpose 级白名单没有留下没人认领的条目", gw.SPEND_APPROVAL_EXEMPT_PURPOSES == (),
   gw.SPEND_APPROVAL_EXEMPT_PURPOSES)
try:
    gw.spend_guard("lipsync", "default", "local", 1, "闸还活着吗")
    ck("spend_guard 对要批的能力仍然拦（闸没被改成空函数）", False, "没拦")
except gw.SpendApprovalRequired:
    ck("spend_guard 对要批的能力仍然拦（闸没被改成空函数）", True)
# 🔴 2026-10-22 方案1 之后这一格【反过来】了：video.generate 不再免批 ⇒ spend_guard 会抛，
#    而那四个 8090 提交口就不再是空操作。原来这里是一句裸调用 + "直接放行" 的断言，
#    现在它会当场把闸炸掉（闸崩≠闸红），所以改成"必须抛、且抛的是 approval_required"。
try:
    gw.spend_guard("video.generate", "*", "local", 1, "8090 四个提交口")
    ck("video.generate 现在要批：spend_guard 必须拦（放行 = 免批名单又被塞回来了）", False, "居然放行了")
except gw.SpendApprovalRequired as _g42:
    ck("video.generate 现在要批：spend_guard 抛 approval_required ⇒ 8090 那四个 _spend_or_423 不再是空操作",
       "approval_required" in str(_g42), True)

print("== 5. 这条路上物理上不存在弹窗/建待批/消费批准 ==")
src = open(os.path.join(ROOT, "backend", "services", "wan30_studio.py"), encoding="utf-8").read()
hits = re.findall(r"pop_approval_window|create_spend_pending|check_spend_approval|peek_spend_approval", src)
ck("工作台源码里不含批准三件套", not hits, set(hits))
ck("页面里也没有「算价并弹批准窗」这一格", "算价并弹批准窗" not in
   open(os.path.join(ROOT, "frontend", "wan30_studio.html"), encoding="utf-8").read())

print("== 6. 捞回口径：不给 job_id 就拒，别让人以为捞过了 ==")
try:
    run(ws.studio_recover({}))
    ck("无 job_id 该拒", False, "居然进去了")
except Exception as e:
    ck("无 job_id → 400", "job_id" in str(getattr(e, "detail", e)), e)

print("== 7. 自动回传：只 GET、拿到片就落盘、失败会计数并放弃（传输层换成假口，判据跑真链路） ==")
FAKE_MP4 = b"W" * 4096
calls = []


def fake_http(method, url, headers=None, body=None, timeout=60.0):
    """只替换最外面那层 socket：`pull → gw.call → _tianyue_video_run → _tianyue_video_once(poll)
       → _tianyue_deliver → _wan30_download` 全是真代码在跑。"""
    calls.append((method, url))
    if "/v1/videos/" in url:
        st = getattr(fake_http, "status", "completed")
        obj = {"id": "task_selftest", "status": st}
        if st == "completed":
            obj["url"] = "https://dw.tianyue.xyz/proxy/down_video?token=selftest"
        if st == "failed":
            obj["error"] = {"message": "自检：上游说这条失败了"}
        return 200, {"content-type": "application/json"}, json.dumps(obj).encode()
    return 200, {"content-type": "video/mp4"}, FAKE_MP4


gw._wan30_http = fake_http
ws.STUDIO_ROOT = Path(tempfile.gettempdir()) / "w30s_reconcile"      # 别污染真产物目录/别让常驻自愈看见
import shutil                                                        # noqa: E402
shutil.rmtree(ws.STUDIO_ROOT, ignore_errors=True)

tid = "zz-selftest-1"
(ws._task_dir(tid)).mkdir(parents=True, exist_ok=True)
(ws._job_note_path(tid)).write_text(json.dumps(
    {"kind": "wan3_job", "task_id": "task_selftest", "lane": ws.LANE, "model": "ME-wan30-1080p-1055",
     "status": "queued"}, ensure_ascii=False), encoding="utf-8")

fake_http.status = "completed"
run_res = ws.pull(tid, "task_selftest")
mp4 = ws._out_path(tid)
ck("成片自动落盘且字节对", os.path.isfile(mp4) and os.path.getsize(mp4) == len(FAKE_MP4), mp4)
ck("全程一个 POST 都没发（不可能双花）", set(m for m, _u in calls) == {"GET"}, set(calls))
ck("GET 打的确实是这条号", any("task_selftest" in u for _m, u in calls), calls[:3])
note = json.loads((ws._job_note_path(tid)).read_text(encoding="utf-8"))
ck("wan3_job.json 标成 delivered", note.get("status") == "delivered", note.get("status"))
rows = {r["task_id"]: r for r in ws.scan_jobs()}
ck("盘上事实：这一条=已回传", rows[tid]["have_file"] and rows[tid]["bytes"] == len(FAKE_MP4), rows.get(tid))
ck("已回传的不再起腿（不会反复重下）", ws.reconcile_once()["pulled"] == 0)

os.unlink(mp4)
fake_http.status = "failed"
ws.pull(tid, "task_selftest")
note = json.loads((ws._job_note_path(tid)).read_text(encoding="utf-8"))
ck("失败会计数并留在待捞（不是假装成功）",
   note.get("status") == "pending" and note.get("reconcile_attempts") == 1, note)
ck("拒因写在盘上给人看，且不带着上一次的 delivered_at 撒谎",
   "status=failed" in str(note.get("last_error") or "") and not note.get("delivered_at"), note)

for _ in range(ws._ABANDON_AFTER - 1):
    ws.pull(tid, "task_selftest")
note = json.loads((ws._job_note_path(tid)).read_text(encoding="utf-8"))
ck("追满 %d 次就放弃并写明（死号不该被追到天荒地老）" % ws._ABANDON_AFTER,
   note.get("status") == "abandoned" and note.get("reconcile_attempts") == ws._ABANDON_AFTER, note)
ck("放弃后不再起腿", ws.reconcile_once()["pulled"] == 0)
row = {r["task_id"]: r for r in ws.scan_jobs()}[tid]
ck("放弃这件事在页面读得到（attempts+status）",
   row["attempts"] == ws._ABANDON_AFTER and row["status"] == "abandoned", row)

v, e = gw._tianyue_payload({"job_id": "task_selftest", "output_path": "x.mp4"}, lane="wan30-studio")
ck("带 job_id 的形状=poll，body 空（这一条物理上不可能是提交）",
   (v or {}).get("mode") == "poll" and (v or {}).get("body") == {}, v)

shutil.rmtree(ws.STUDIO_ROOT, ignore_errors=True)
ck("自检产物已清干净", not ws.STUDIO_ROOT.exists())

print("== 8. 分辨率→模型档（唯一源 gateway._WAN30_RES_MODEL）：720P 是真档，且不许悄悄换模型 ==")
# 传输层还是第 7 组那个假口（一个真请求都不发），但【提交这条链路是真代码】：
#   _tianyue_video_run → _tianyue_video_once → _tianyue_payload → POST → 轮询 → 落盘
# 为什么这一组必须跑真链路而不是读源码：2026-10-21 之前"只出 1080P"这条判据抄在三处
#   （网关、wan3_client 两条 prime 腿、超级复刻的 log 一行），只比对字符串的闸会全绿而档位其实送不出去。
submit_calls = []


def submit_http(method, url, headers=None, body=None, timeout=60.0):
    submit_calls.append((method, url, (body or b"").decode("utf-8", "replace")))
    if method == "POST" and url.endswith("/v1/videos"):
        return 200, {}, json.dumps({"id": "task_g8", "task_id": "task_g8", "status": "queued"}).encode()
    if "/v1/videos/" in url:
        return 200, {}, json.dumps({"id": "task_g8", "status": "completed",
                                    "url": "https://dw.tianyue.xyz/proxy/down_video?token=g8"}).encode()
    return 200, {"content-type": "video/mp4"}, FAKE_MP4


gw._wan30_http = submit_http


def _g8(resolution, **over):
    p = {"prompt": "自检：一只橘猫在台面打哈欠", "duration": 12, "resolution": resolution,
         "ratio": "3:4", "image_urls": ["https://cdn.example.com/a.png"],
         "video_urls": ["https://cdn.example.com/b.mp4"],
         "output_path": str(Path(tempfile.gettempdir()) / ("w30s_g8_%s.mp4" % resolution)),
         "poll_interval": 0.05, "poll_timeout": 20}
    p.update(over)
    del submit_calls[:]
    return gw._tianyue_video_run(p, {"capability": "video.generate", "purpose": "wan30-studio"})


r7 = _g8("720P")
posts7 = [c for c in submit_calls if c[0] == "POST"]
body7 = json.loads(posts7[0][2]) if posts7 else {}
ck("720P 走得通（假口提交成功，不是被本地判死）", bool(r7.get("success")), r7.get("error"))
ck("720P → model=唯一源那一档（2026-10-21 换成 S-Wan30-prime-720p-1050-se；不是拿 1080p 冒充）",
   body7.get("model") == gw._WAN30_RES_MODEL["720P"], body7.get("model"))
ck("720P → body.resolution=720p", body7.get("resolution") == "720p", body7.get("resolution"))
ck("720P 只发一次 POST（没有 720p 兜底模型 ⇒ 不兜=不双花）", len(posts7) == 1, len(posts7))
ck("信封署的模型=真发出去那条", r7.get("model_used") == gw._WAN30_RES_MODEL["720P"], r7.get("model_used"))
r10 = _g8("1080P")
posts10 = [c for c in submit_calls if c[0] == "POST"]
body10 = json.loads(posts10[0][2]) if posts10 else {}
# 🔴 2026-10-22 用户改了口径（推翻 10-21 那版"按段长二选一"）：超级复刻一条片子里【全部走
#    ME-WAN3-prime-1080p-1055】，短段不许掉进按秒计费的 PIX —— 同一条片两段两个模型、两种计费口径
#    = 画面与账都漂（用户原话"不然会乱"）。实现 = .env 的 FALLBACK 键留空 ⇒ 候选表只剩一条。
#    这条断言现在钉的是"12s 这种短段也落 prime、且绝不落 PIX"（反向对照：把 PIX 填回 .env 它就红）。
ck("1080P + 12s（短段）⇒ 仍落 prime（2026-10-22 起不分段长，PIX 不接），resolution 仍 1080p",
   [body10.get("model"), body10.get("resolution")]
   == [str(gw._WAN30_RES_MODEL["1080P"]), "1080p"], [body10.get("model"), body10.get("resolution")])
ck("1080P + 12s ⇒ 只发 1 次 POST（不换档=不双花）", len(posts10), 1)
_r15 = _g8("1080P", duration=15)
_b15 = json.loads([c for c in submit_calls if c[0] == "POST"][0][2])
ck("1080P + 满 15s ⇒ 落 prime 档",
   _b15.get("model") == gw._WAN30_RES_MODEL["1080P"], _b15.get("model"))
_g8("1080P", duration=None)
_pn = [c for c in submit_calls if c[0] == "POST"]
ck("反向对照：段长取不到 ⇒ 落主档（prime），且绝不从别处捞第三条模型",
   [bool(_pn), json.loads(_pn[0][2]).get("model")] if _pn else [False, None],
   [True, gw._WAN30_RES_MODEL["1080P"]])
r4 = _g8("480P")
posts4 = [c for c in submit_calls if c[0] == "POST"]
ck("480P 在【本地】就拒、一个 POST 都没发", not r4.get("success") and not posts4, (r4.get("error"), posts4))
ck("拒因点名合法档位（让人知道能给什么）", "720P" in str(r4.get("error") or ""), r4.get("error"))
r5 = _g8("720P", model=gw._WAN30_RES_MODEL["1080P"])
ck("点名要 1080p 模型又给 720P ⇒ 大声拒，不悄悄换模型",
   not r5.get("success") and "model" in str(r5.get("error") or ""), r5.get("error"))
ck("档位表恰好两档（多一档=渠道目录里真有了才许加）", set(gw._WAN30_RES_MODEL) == {"1080P", "720P"},
   list(gw._WAN30_RES_MODEL))

print("== 8a. 2026-10-23 用户指定：720P 档【首选】换成 ME-WAN3-prime-720p-1055 ==")
# 起因不是审美：1080P 那条 prime 从 00:28 起连续 12+ 发被渠道回 `fail_to_fetch_task / 上游创建任务失败 504`，
# 一条都没建上单（同 key 的 /v1/models 200、/v1/videos/<乱号> 秒回 400 ⇒ 站与账户都好，死的是建单那层）。
# 这一组钉的是【顺序 + 不删证据】：首选跟着用户点名走，但被换下来的那条（本档唯一有出片证据的）
# 必须留在候选链里 —— 删掉它 = 页面只剩一张没跑过的卡，那正是"没数据的腿进池"要防的形状。
S720 = "ME-WAN3-prime-720p-1055"
PREV720 = "S-Wan30-prime-720p-1050-se"
DRY720 = "ME-wan30-720p-1055"
gw._wan30_http = submit_http                    # 8b 留的是"建上单后失败"那支假口，本组要会成功的
chain720 = list(gw.wan30_models_for("720P"))
ck("720P 主模型 = 用户点名的这条", gw._WAN30_RES_MODEL["720P"] == S720, gw._WAN30_RES_MODEL["720P"])
ck("候选链第一条就是它（不再先撞干涸那条）", chain720[0] == S720, chain720)
ck("10-21 实测出过片的那条还在链里（换首选不许顺手摘别人的证据）", PREV720 in chain720, chain720)
ck("干涸那条还在链里但排在后面（回血还能用）", DRY720 in chain720 and chain720.index(DRY720) > 0, chain720)
ck("替换没顺手换分辨率：链里每条名字都含 720p", all("720p" in str(m) for m in chain720), chain720)
ck("反向对照：1080P 档 = .env 的主模型，且两档不串（换 720P 没把 1080P 带跑）",
   gw._WAN30_RES_MODEL["1080P"] == str(gw._get("TIANYUE_WAN30_MODEL"))
   and gw._WAN30_RES_MODEL["1080P"] != gw._WAN30_RES_MODEL["720P"],
   gw._WAN30_RES_MODEL)
rr = _g8("720P")
bb = [json.loads(c[2]) for c in submit_calls if c[0] == "POST"]
ck("真发出去的 body.model = 新主（不是只在表里改个名）",
   bool(bb) and bb[0].get("model") == S720 and rr.get("success"), (bb[:1], rr.get("error")))
ck("只发一次 POST（新主一次就成 ⇒ 没白撞）", len(bb) == 1, len(bb))


print("== 8b. 720P 同分辨率候选链：只有【确定没建上单】才换下一条，拿到过号绝不重发 ==")
# 2026-10-21 16:57 现网：ME-wan30-720p-1055 被渠道回 500「上游余额不足了」（无任务号）⇒ 单档=一条线走死。
# 这一组钉的是【换线的边界】：换的只能是同分辨率的另一条线，且已建上单就绝不再 POST。
def dry_http(method, url, headers=None, body=None, timeout=60.0):
    submit_calls.append((method, url, (body or b"").decode("utf-8", "replace")))
    if method == "POST" and url.endswith("/v1/videos"):
        # 形状照 16:57 现网那条真响应：tianyue 把上游整包塞进 message 字符串里回 500
        _up = json.dumps({"object": "video", "status": "failed",
                          "error": {"message": "上游创建任务失败，状态码:403", "code": "task_failed"},
                          "detail": "上游余额不足了"}, ensure_ascii=False)
        return 500, {}, json.dumps({"message": "fail_to_fetch_task：" + _up}, ensure_ascii=False).encode()
    return 200, {}, b"{}"


gw._wan30_http = dry_http
rd = _g8("720P")
chain_posted = [json.loads(c[2]).get("model") for c in submit_calls if c[0] == "POST"]
ck("整条 720P 候选链按顺序试完（%d 条）" % len(chain_posted),
   chain_posted == list(gw.wan30_models_for("720P")), chain_posted)
ck("链里没有一条非 720p 的模型（换线不许顺手换分辨率）",
   all("720p" in str(m) for m in chain_posted), chain_posted)
ck("1080P 的链仍来自 .env（没被 720P 那格污染）",
   gw.wan30_models_for("1080P", ["ME-from-env", "PIX-from-env"]) == ["ME-from-env", "PIX-from-env"],
   gw.wan30_models_for("1080P", ["ME-from-env", "PIX-from-env"]))
ck("全链都干 ⇒ 信封是失败，且把渠道原话带出来（含『余额不足』那句）",
   not rd.get("success") and "余额不足" in str(rd.get("error")), rd.get("error"))


def built_then_failed(method, url, headers=None, body=None, timeout=60.0):
    """第一条线【建上了任务号】、随后任务失败 —— 这正是双花陷阱：绝不能换第二条再 POST。"""
    submit_calls.append((method, url, (body or b"").decode("utf-8", "replace")))
    if method == "POST" and url.endswith("/v1/videos"):
        return 200, {}, json.dumps({"id": "task_g8b", "task_id": "task_g8b", "status": "queued"}).encode()
    return 200, {}, json.dumps({"id": "task_g8b", "status": "failed",
                                "error": {"message": "自检：已建上单之后任务失败"}}).encode()


gw._wan30_http = built_then_failed
rb = _g8("720P")
posts_b = [c for c in submit_calls if c[0] == "POST"]
ck("反向对照：已建上单后失败 ⇒ 只发了一次 POST（换第二条=双花）", len(posts_b) == 1, len(posts_b))
ck("不换的原因写在信封里（人要知道是不该换，不是换不动）",
   "任务号" in str(rb.get("fallback_why") or rb.get("error") or ""), rb.get("fallback_why") or rb.get("error"))

for _f in ("1080P", "720P", "480P"):
    _p = Path(tempfile.gettempdir()) / ("w30s_g8_%s.mp4" % _f)
    if _p.exists():
        _p.unlink()
ck("第 8 组产物已清干净", not any((Path(tempfile.gettempdir()) / ("w30s_g8_%s.mp4" % _f)).exists()
                              for _f in ("1080P", "720P", "480P")))

print("== 9. 页面选模型 = 选档，且发出去的 body 真是那条（10-21 只留两条 prime；10-23 起 720P 有两条口径不同的候选） ==")
P1080 = "ME-WAN3-prime-1080p-1055"
P720 = "ME-WAN3-prime-720p-1055"                             # 10-23 首选（按次，名字无 `-se`）
P720SE = "S-Wan30-prime-720p-1050-se"                        # 10-21 实测过的那条（按秒），留在页面当对照
G9DIR = Path(tempfile.gettempdir()) / "w30s_g9"
G9DIR.mkdir(exist_ok=True)
G9 = str(G9DIR / "wan30.mp4")
BASE_BODY = {"prompt": "自检：一只橘猫在台面打哈欠", "duration": 8, "ratio": "3:4",
             "image_urls": ["https://cdn.example.com/a.png"]}

o9 = run(ws.studio_options())
ck("页面点名的模型 = 本地档位表里那些（1080p prime + 720P 两条口径各一）",
   [m["id"] for m in o9["models"]] == [P1080, P720SE, P720], [m["id"] for m in o9["models"]])
ck("PIX 与旧 ME-wan30-1080p 已从页面摘掉（他说只留这两个）",
   not any("PIX" in m["id"] or m["id"] == "ME-wan30-1080p-1055" for m in o9["models"]),
   [m["id"] for m in o9["models"]])
ck("每条都各自标了档：1080P/720P（同档两条不许合并成一句话）",
   sorted((m["id"], m["resolution"]) for m in o9["models"])
   == sorted([(P1080, "1080P"), (P720SE, "720P"), (P720, "720P")]),
   [(m["id"], m["resolution"]) for m in o9["models"]])
ck("720P 那条带着实测口径（832×1108 那句在，验收不拿 1280 去对）",
   "832×1108" in [m for m in o9["models"] if m["id"] == P720SE][0]["loose"], "")
ck("10-23 新首选这条老实写着【未实测】（没跑过就不许把 -se 那发的数挂它头上）",
   [m for m in o9["models"] if m["id"] == P720][0]["proven"] is False, "")
ck("1080P 那条老实写着【没跑过】（没跑过就不许编上限）",
   [m for m in o9["models"] if m["id"] == P1080][0]["proven"] is False, "")
ck("价格证据挂在页面上（上游 403 第一次报出 ¥4.20/次，别退回『单价看不见』那句 —— 那句是 -se 那条的实测）",
   "4.20" in [m for m in o9["models"] if m["id"] == P720SE][0].get("price_seen", ""), "")

pk = ws._params_from_body(dict(BASE_BODY, model=P720))
ck("选 720P 那条 ⇒ 送网关的 resolution=720P、model 锁死",
   pk["params"]["resolution"] == "720P" and pk["params"]["model"] == P720, pk["params"])
pk2 = ws._params_from_body(BASE_BODY)
ck("页面没点名字面 ⇒ 默认第一条，且永远显式点名（不留「不点名=自动兜底」那条路）",
   pk2["params"]["model"] == P1080 and pk2["params"]["resolution"] == "1080P", pk2["params"])


def _wire(**over):
    """走【页面拼出来的参数】打到假口上：看真发出去的 body 到底是哪条模型、哪一档分辨率。"""
    p = dict(ws._params_from_body(dict(BASE_BODY, **over))["params"])
    p["output_path"] = G9
    gw._wan30_http = gw_stub                      # 第 7/8 组各自换过假口 ⇒ 每次显式装回本段这支
    del net_calls[:]
    res = gw.call(ws.CAP, ws.PURPOSE, **p)         # call 不进账本 ⇒ 不会留假预扣
    return res, [json.loads(b) for m, _u, b in net_calls if m == "POST"]


res_a, body_a = _wire(model=P720)
ck("选 720P ⇒ POST body.model 就是它、resolution=720p",
   bool(body_a) and body_a[0].get("model") == P720 and body_a[0].get("resolution") == "720p", body_a[:1])
ck("选 720P 只发一次 POST（PIX 没有 720p 变体 ⇒ 不兜=不悄悄换分辨率）", len(body_a) == 1, len(body_a))
res_b, body_b = _wire(model=P1080)
ck("选 1080P ⇒ body.model = ME-WAN3-prime（旧那条已被替换，不在任何链上）",
   body_b[0].get("model") == P1080 and body_b[0].get("resolution") == "1080p", body_b[:1])
ck("信封署的模型 = 真发出去那条（台账不许印一条没跑过的）", res_b.get("model_used") == P1080,
   res_b.get("model_used"))
# 🔴 2026-10-22 撤掉全局 TIANYUE_DURATION_MODE 这一键：一个键管两条不同档本身就是 bug（它为按秒那条
#    设成 seconds 之后，按次那条也被按秒发）。口径跟着【模型名尾巴】走：-se=按秒传真实秒数，无尾巴=按次传 1。
#    判据唯一源 = gateway.wan30_is_per_second()，两条断言各钉一个方向（少一条就是另一条没人守）。
res_c, body_c = _wire(model=P720SE)               # 10-21 那条按秒的，仍在候选链里，口径要单独钉
ck("duration 口径按模型名判：prime（无 -se）⇒ duration=1、video_duration 仍是真实秒数 8",
   [body_b[0].get("duration"), body_b[0].get("video_duration")] == [1, 8], body_b[:1])
ck("duration 口径按模型名判：720P 新首选（无 -se，按次）⇒ 同样 duration=1、真实秒数留在 video_duration",
   [body_a[0].get("duration"), body_a[0].get("video_duration")] == [1, 8], body_a[:1])
ck("duration 口径按模型名判：720P 那条（-se）⇒ duration 与 video_duration 同值（都是真实秒数）",
   [body_c[0].get("duration"), body_c[0].get("video_duration")] == [8, 8], body_c[:1])
ck("反向对照：判据本身两向都对（-se 真=按秒、prime 假=按次），不然上面三条是空跑的绿",
   [gw.wan30_is_per_second("PIX-Wan30-pro-1080p-1055-se"),
    gw.wan30_is_per_second("ME-WAN3-prime-1080p-1055"),
    gw.wan30_is_per_second("ME-WAN3-prime-720p-1055"), gw.wan30_is_per_second(None)],
   [True, False, False, False])
ck("信封上那句计费口径与判据同源（不许写死成按次）",
   bool(res_c.get("note") and "按【秒】" in res_c["note"])
   and bool(res_a.get("note") and "按【次】" in res_a["note"])
   and bool(res_b.get("note") and "按【次】" in res_b["note"]),
   [res_c.get("note"), res_a.get("note"), res_b.get("note")])
ck("反向对照：1080P 的链里再没有 ME-wan30-1080p-1055",
   "ME-wan30-1080p-1055" not in gw.wan30_models_for("1080P", [P1080, "PIX-x"]),
   gw.wan30_models_for("1080P", [P1080, "PIX-x"]))

ws.MODEL_RULES[P720]["resolution"] = "480P"        # 假装网关把这一档改朝换了
try:
    run(ws.studio_submit(dict(BASE_BODY, model=P720)))
    ck("档位漂移时该拒而不是照跑", False, "居然放行")
except Exception as e:
    ck("网关档位表一改、页面立刻拒（不照旧映射偷偷花钱）",
       "只给" in str(getattr(e, "detail", e)) or "stale" in str(getattr(e, "detail", e)),
       str(getattr(e, "detail", e))[:140])
ws.MODEL_RULES[P720]["resolution"] = "720P"
ck("漂移用例已复位（链上三条都在：1080p prime + 720P 首选 + 720P 那条按秒候选）",
   [m["id"] for m in ws._page_models() if m["in_lane"]] == [P1080, P720SE, P720],
   ws._route_models())

shutil.rmtree(G9DIR, ignore_errors=True)
ck("第 9 组产物已清干净", not G9DIR.exists())


def _safe(s):
    # 🔴 汇总行必须编码安全：这条闸一旦在打印 FAILS 时崩（`¥`/`✓`/`⇒` 都不在 cp936 里），
    #    退出码就不是"红"而是"崩"—— 而崩掉的闸看起来和通过一样安静（2026-10-23 现网就是这样把
    #    一组真失败藏在了 UnicodeEncodeError 后面）。
    enc = (getattr(sys.stdout, "encoding", None) or "ascii")
    try:
        return s.encode(enc, "replace").decode(enc, "replace")
    except Exception:                                       # noqa: BLE001
        return s.encode("ascii", "replace").decode("ascii")


print(_safe("\n%s  (%d 条不过)" % ("CHECK-PASS" if not FAILS else "CHECK-RED " + json.dumps(FAILS, ensure_ascii=False), len(FAILS))))
sys.exit(1 if FAILS else 0)
