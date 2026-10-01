"""WeekSir 统一模型网关 · HTTP 路由（挂到 8090 上，不新起端口）。

把 backend/gateway.py 的能力（invoke/resolve/meter/test_models）暴露成 HTTP，
供 skill(HTTP)、hypit provider、外部工具用同一个模型入口。CLI 仍是 `python gateway.py call`。

端点（全部 127.0.0.1 本机·运维自用；给客户前须并入 Gate 0：只读化/鉴权）：
  GET  /api/model/caps                     → 能力 + purposes
  GET  /api/model/resolve?capability=&purpose= → 脱敏摘要 + 积分预估（批准前给人看成本）
  POST /api/model/call {capability,purpose,account,params} → 走 gateway.invoke（预扣/回滚/信封）
  GET  /api/model/health                   → gateway.test_models（各 route 真探活）

用同步 def（FastAPI 丢线程池跑，内部 urllib/subprocess 不阻塞事件循环）。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from backend import gateway

router = APIRouter(prefix="/api/model", tags=["model-gateway"])


class ModelCall(BaseModel):
    capability: str
    purpose: str | None = None
    account: str = "local"
    params: dict | None = None


@router.get("/caps")
def caps():
    return {"capabilities": gateway.CAPABILITIES,
            "routes": {f"{c}/{p}": gateway.ROUTES[(c, p)]["provider"] for (c, p) in gateway.ROUTES}}


@router.get("/resolve")
def resolve(capability: str = Query(...), purpose: str | None = None):
    try:
        r = gateway.resolve(capability, purpose)
    except gateway.RouteError as e:
        raise HTTPException(400, str(e))
    est = gateway.meter(capability, r["purpose"], None)["credits_estimate"]
    return {"route": gateway.safe_summary(r), "credits_estimate": est}


@router.post("/call")
def call(req: ModelCall):
    # invoke 内部已做 预扣/回滚/信封，不抛；异常也归到 ok:false 信封里
    return gateway.invoke(req.capability, req.purpose, req.account, req.params or {})


@router.get("/health")
def health():
    return gateway.test_models()
