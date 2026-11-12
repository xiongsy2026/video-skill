"""wan3.0 两条模型的【媒体入参】取证：全部只读 GET，0 消耗、一个 POST 都不发。

跑法：python -X utf8 scripts/probe-wan30-media.py
拿什么：
  1) /v1/models 里 ME 与 PIX 两条的 supported_endpoint_types（有卡才算它有那个入参形状）
  2) 把 2026-10-20 那 8 条探针任务【逐条 GET 回来】读它们自己的终态与原因 —— 尤其是 PIX 那 5 条
     failed 的原话（图片/视频/音频上限就是从那儿来的），以及成片对象里有没有回写 media 字段。
🔴 不许在这里加任何 POST：那 8 条已经是账本外的真单，取证只能靠 GET。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend import gateway as gw                                    # noqa: E402

BASE = str(gw._get("TIANYUE_BASE_URL") or "").rstrip("/")
KEY = str(gw._get("TIANYUE_API_KEY") or "").strip()
HDR = {"Authorization": "Bearer " + KEY, "User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}

# 2026-10-20 的 8 条探针（全部在网关账本外，见 C:\dsh\docs\tianyue-api.md 事故留档）
PROBES = [
    ("task_rIoIXogLG5Pc2JLfUByayAAE5hTw7e99", "ME", "不传图会不会拒"),
    ("task_OCuuSmc5Rc81g7WKENEhGYmIJzCGbNkt", "ME", "720p 收不收"),
    ("task_YYaKpiSrKxnu2u8gGpAZzs4jj3xBHbAi", "ME", "duration=0 该被拒"),
    ("task_HtWSW5JqbyNzwlGD1hD0ZffqUz4FvmF7", "PIX", "图片上限"),
    ("task_eUlMo6dHmixMS1z6NrNIu7HcHPZZ3kgE", "PIX", "视频 3 个"),
    ("task_vc9DOed5EruQXPoNWOe0FIpWTiFOmeN2", "PIX", "音频 1"),
    ("task_usT2CiF1zTv3OOCafCdnHZGxiED85lUK", "PIX", "音频 3"),
    ("task_BRDNyJI7IPGdGJnQt3OKvWCgw5HOiWwW", "PIX", "图+视频+音频齐"),
]
WANT = ("ME-wan30-1080p-1055", "PIX-Wan30-pro-1080p-1055-se", "ME-wan30-720p-1055",
        "S-Wan30-prime-720p-1050-se", "S-Wan30-720p-1050-se", "ME-WAN3-prime-1080p-1055")

st, _, raw = gw._wan30_http("GET", BASE + "/v1/models", headers=HDR, timeout=60.0)
print("GET /v1/models ->", st)
if st == 200:
    data = json.loads((raw or b"").decode("utf-8", "replace") or "{}")
    rows = data.get("data") if isinstance(data.get("data"), list) else data.get("models") or []
    print("模型条数:", len(rows))
    for m in rows:
        if str(m.get("id")) in WANT:
            print("  %-32s endpoint_types=%s" % (m.get("id"), json.dumps(m.get("supported_endpoint_types"), ensure_ascii=False)))
else:
    print("  拿不到:", (raw or b"")[:200])

print("\n== 逐条 GET 那 8 发探针的终态（只读） ==")
for tid, model, intent in PROBES:
    st, _, raw = gw._wan30_http("GET", BASE + "/v1/videos/" + tid, headers=HDR, timeout=60.0)
    try:
        o = json.loads((raw or b"").decode("utf-8", "replace") or "{}")
    except ValueError:
        o = {}
    keys = sorted(o.keys()) if isinstance(o, dict) else []
    print("%-6s %-34s 意图=%-14s HTTP=%s status=%s progress=%s seconds=%s"
          % (model, tid[:26], intent, st, o.get("status"), o.get("progress"), o.get("seconds")))
    for k in ("error", "message", "failed_reason", "fail_reason", "reason", "detail"):
        if isinstance(o.get(k), (dict, str)) and o.get(k):
            print("      %s = %s" % (k, json.dumps(o[k], ensure_ascii=False)[:300] if isinstance(o[k], dict) else str(o[k])[:300]))
    media_echo = {k: o.get(k) for k in ("image_urls", "video_urls", "audio_urls", "media", "inputs", "params", "prompt")
                  if isinstance(o.get(k), (list, str, dict)) and o.get(k)}
    if media_echo:
        brief = {}
        for k, v in media_echo.items():
            if isinstance(v, list):
                brief[k] = "%d 条" % len(v)
            elif k == "prompt":
                brief[k] = "%d 字" % len(str(v))
            else:
                brief[k] = str(v)[:80]
        print("      回显入参 =", json.dumps(brief, ensure_ascii=False))
    else:
        print("      回显入参 = 无（顶层 keys:", ",".join(keys)[:180], ")")
    meta = o.get("metadata") if isinstance(o.get("metadata"), dict) else {}
    if meta:
        print("      metadata keys =", ",".join(sorted(meta))[:200])
