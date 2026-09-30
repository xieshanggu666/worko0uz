"""企业间配额交易订单 API：下单、双方确认、撤销、交割与查询。"""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user, require_roles
from app.models import Company, TradeOrder, User
from app.schemas import TradeOrderCancelIn, TradeOrderIn
from app.services.trade_order_service import (
    cancel_order,
    confirm_order,
    create_order,
    deliver_order,
)

router = APIRouter(prefix="/api/trade-orders", tags=["trade-orders"])

_STATUS_LABELS = {
    "pending_confirmation": "待双方确认",
    "confirmed": "双方已确认",
    "delivered": "已交割",
    "cancelled": "已撤销",
}


def _company_name(db: Session, company_id: int) -> str:
    company = db.get(Company, company_id)
    return company.name if company else f"企业{company_id}"


def _serialize(db: Session, order: TradeOrder) -> dict:
    return {
        "id": order.id,
        "order_no": order.order_no,
        "year": order.year,
        "seller_company_id": order.seller_company_id,
        "buyer_company_id": order.buyer_company_id,
        "seller_name": _company_name(db, order.seller_company_id),
        "buyer_name": _company_name(db, order.buyer_company_id),
        "amount": float(order.amount),
        "unit_price": float(order.unit_price) if order.unit_price is not None else None,
        "status": order.status,
        "status_label": _STATUS_LABELS.get(order.status, order.status),
        "seller_confirmed": bool(order.seller_confirmed),
        "buyer_confirmed": bool(order.buyer_confirmed),
        "held_amount": float(order.held_amount or 0),
        "creator_company_id": order.creator_company_id,
        "trade_date": order.trade_date,
        "remark": order.remark,
        "created_at": order.created_at,
        "confirmed_at": order.confirmed_at,
        "delivered_at": order.delivered_at,
        "cancelled_at": order.cancelled_at,
        "cancelled_by_company_id": order.cancelled_by_company_id,
        "cancel_reason": order.cancel_reason,
    }


def _resolve_actor(db: Session, user: User, data: TradeOrderIn | None = None) -> int:
    """确定操作所属企业：企业角色只能代表本企业；监管角色须显式指定一方企业。"""
    if user.role == "enterprise":
        # 企业发起时强制为本企业，忽略请求体中可能伪造的 creator_company_id
        return user.company_id
    if data is not None and data.creator_company_id:
        if not db.get(Company, data.creator_company_id):
            raise HTTPException(status_code=404, detail="发起方企业不存在")
        return data.creator_company_id
    raise HTTPException(status_code=400, detail="监管操作须指定发起方企业（creator_company_id）")


@router.get("")
def list_orders(
    status: str | None = None,
    year: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = db.query(TradeOrder)
    # 企业只能看到本企业作为买方或卖方的订单，监管侧可查看全部
    if user.role == "enterprise":
        q = q.filter(
            (TradeOrder.seller_company_id == user.company_id)
            | (TradeOrder.buyer_company_id == user.company_id)
        )
    if status:
        q = q.filter(TradeOrder.status == status)
    if year is not None:
        q = q.filter(TradeOrder.year == year)
    orders = q.order_by(TradeOrder.id.desc()).all()
    return [_serialize(db, o) for o in orders]


@router.get("/{order_id}")
def get_order(order_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    order = db.get(TradeOrder, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="交易订单不存在")
    if user.role == "enterprise" and user.company_id not in (
        order.seller_company_id,
        order.buyer_company_id,
    ):
        raise HTTPException(status_code=403, detail="无权查看该交易订单")
    return _serialize(db, order)


@router.post("")
def create_trade_order(
    request: Request,
    data: TradeOrderIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "enterprise")),
):
    actor_id = _resolve_actor(db, user, data)
    if actor_id not in (data.seller_company_id, data.buyer_company_id):
        raise HTTPException(status_code=403, detail="只能由买卖双方企业发起订单")
    idem_key = data.idempotency_key or request.headers.get("idempotency-key")
    try:
        order = create_order(
            db,
            data.seller_company_id,
            data.buyer_company_id,
            data.year,
            data.amount,
            data.unit_price,
            data.trade_date,
            data.remark,
            creator_company_id=actor_id,
            idempotency_key=idem_key,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _serialize(db, order)


@router.post("/{order_id}/confirm")
def confirm_trade_order(
    order_id: int,
    company_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "enterprise")),
):
    order = db.get(TradeOrder, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="交易订单不存在")
    actor_id = _actor_company(user, db, order, company_id)
    try:
        order = confirm_order(db, order_id, actor_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _serialize(db, order)


@router.post("/{order_id}/cancel")
def cancel_trade_order(
    order_id: int,
    data: TradeOrderCancelIn,
    company_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "enterprise")),
):
    order = db.get(TradeOrder, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="交易订单不存在")
    actor_id = _actor_company(user, db, order, company_id)
    try:
        order = cancel_order(db, order_id, actor_id, data.reason)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _serialize(db, order)


@router.post("/{order_id}/deliver")
def deliver_trade_order(
    order_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "enterprise")),
):
    order = db.get(TradeOrder, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="交易订单不存在")
    # 企业仅能代表本方触发交割；监管可代为触发（actor=None 走服务端双方校验路径）
    if user.role == "enterprise":
        if user.company_id not in (order.seller_company_id, order.buyer_company_id):
            raise HTTPException(status_code=403, detail="仅订单的买卖双方企业可执行该操作")
        actor_id = user.company_id
    else:
        actor_id = None
    try:
        order = deliver_order(db, order_id, actor_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _serialize(db, order)


def _actor_company(user: User, db: Session, order: TradeOrder, company_id: int | None) -> int:
    """解析确认/撤销的操作方企业。

    企业角色强制为本企业且必须是订单买卖双方之一；监管角色须用 ``company_id``
    查询参数指定买卖双方之一。
    """
    if user.role == "enterprise":
        if user.company_id not in (order.seller_company_id, order.buyer_company_id):
            raise HTTPException(status_code=403, detail="仅订单的买卖双方企业可执行该操作")
        return user.company_id
    if not company_id:
        raise HTTPException(status_code=400, detail="监管操作须用 company_id 指定买卖双方之一")
    if company_id not in (order.seller_company_id, order.buyer_company_id):
        raise HTTPException(status_code=403, detail="company_id 必须是订单的买方或卖方")
    if not db.get(Company, company_id):
        raise HTTPException(status_code=404, detail="企业不存在")
    return company_id
