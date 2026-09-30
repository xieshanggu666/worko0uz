"""企业间配额交易订单：下单、双方确认、撤销与交割。

业务流程与状态机
----------------
- ``create_order``：卖方/买方/监管发起订单，发起方创建即确认本方；
  卖方若在创建时即确认，则立即占用其可用配额（交易占用 trade_held_balance）。
- ``confirm_order``：对手方确认。买方确认只改变确认标志；卖方确认时占用配额，
  占用后订单进入 ``confirmed``（双方已确认，待交割）。
- ``cancel_order``：交割前任一交易方可撤销；已产生的卖方占用同步释放。
- ``deliver_order``：双方确认后任一交易方（或监管）触发交割：卖方持仓扣减并
  释放占用，买方持仓增加，双方各写一笔成交流水。

与履约冻结的冲突处理
--------------------
交易占用（``trade_held_balance``）与履约冻结（``frozen_balance``）是两笔相互
独立的预留，数据库原子 UPDATE 强制不变量 ``current >= frozen + held``：
- 卖方确认占用时校验“持仓 - 履约冻结 - 已有交易占用”充足，履约冻结的配额不能挂单；
- 报告批准冻结/清缴时同样扣除已交易占用的部分，挂单配额不能被履约挪用；
- 双方账户键 + 企业年度清缴键 + 订单键统一按锁名排序获取，杜绝多账户死锁；
- 订单状态、占用、双方余额与四方流水在同一事务提交，任一步失败整体回滚。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.ledger import (
    InsufficientBalanceError,
    account_lock_key,
    apply_ledger_delta,
    company_clear_key,
    is_duplicate_submit,
    lock_rows_for_update,
    locked_accounts,
    trade_order_key,
    transactional,
)
from app.models.allowance import AllowanceAccount, AllowanceTransaction, TradeOrder
from app.models.company import Company

# 订单状态
PENDING = "pending_confirmation"
CONFIRMED = "confirmed"
DELIVERED = "delivered"
CANCELLED = "cancelled"
_ACTIVE_STATUSES = (PENDING, CONFIRMED)


def _today() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


def _get_account(db: Session, company_id: int, year: int) -> AllowanceAccount:
    account = (
        db.query(AllowanceAccount)
        .filter(AllowanceAccount.company_id == company_id, AllowanceAccount.year == year)
        .first()
    )
    if account is None:
        raise ValueError(f"企业 {company_id} 的 {year} 年度配额账户不存在，请先分配配额")
    return account


def _get_company_name(db: Session, company_id: int) -> str:
    company = db.get(Company, company_id)
    return company.name if company else str(company_id)


def _get_order(db: Session, order_id: int) -> TradeOrder:
    order = db.get(TradeOrder, order_id)
    if order is None:
        raise ValueError("交易订单不存在")
    return order


def _order_lock_keys(order: TradeOrder) -> list[str]:
    """订单涉及的全部锁：订单键 + 双方账户键 + 双方企业年度清缴键。

    锁名全局排序后获取，与清缴/普通交易路径使用同一命名空间，杜绝循环等待。
    """
    return [
        trade_order_key(order.id),
        account_lock_key(order.seller_account_id),
        account_lock_key(order.buyer_account_id),
        company_clear_key(order.seller_company_id, order.year),
        company_clear_key(order.buyer_company_id, order.year),
    ]


def _ensure_party(order: TradeOrder, actor_company_id: int) -> None:
    if actor_company_id not in (order.seller_company_id, order.buyer_company_id):
        raise ValueError("仅订单的买卖双方企业可执行该操作")


def _is_seller(order: TradeOrder, actor_company_id: int) -> bool:
    return actor_company_id == order.seller_company_id


def _add_order_tx(
    db: Session,
    account: AllowanceAccount,
    tx_type: str,
    amount: float,
    balance_after: float,
    frozen_after: float,
    held_after: float,
    counterparty: str,
    remark: str,
    order: TradeOrder,
    price: float | None = None,
) -> AllowanceTransaction:
    tx = AllowanceTransaction(
        account_id=account.id,
        company_id=account.company_id,
        tx_type=tx_type,
        amount=round(amount, 4),
        counterparty=counterparty,
        price=price,
        tx_date=order.trade_date or _today(),
        balance_after=round(balance_after, 4),
        frozen_after=round(frozen_after, 4),
        trade_held_after=round(held_after, 4),
        trade_order_id=order.id,
        remark=remark,
    )
    db.add(tx)
    return tx


def _place_hold(db: Session, order: TradeOrder, amount: float) -> None:
    """卖方确认时占用其可用配额（交易占用），并写占用流水。"""
    account = db.get(AllowanceAccount, order.seller_account_id)
    lock_rows_for_update(db, account.id)
    account = db.get(AllowanceAccount, account.id)
    available = round(
        float(account.current_balance)
        - float(account.frozen_balance)
        - float(account.trade_held_balance),
        4,
    )
    if available < amount:
        raise InsufficientBalanceError("卖方可用配额不足，无法确认占用")
    buyer_name = _get_company_name(db, order.buyer_company_id)
    balance_after, frozen_after, held_after = apply_ledger_delta(
        db, account.id, 0, 0, amount
    )
    _add_order_tx(
        db,
        account,
        "trade_hold",
        amount,
        balance_after,
        frozen_after,
        held_after,
        buyer_name,
        f"订单 {order.order_no} 卖方确认，交易占用 {amount} 吨",
        order,
        price=order.unit_price,
    )


def _release_hold(db: Session, order: TradeOrder, amount: float, remark: str) -> None:
    """释放卖方交易占用并写释放流水（撤销时使用）。"""
    if amount <= 0:
        return
    account = db.get(AllowanceAccount, order.seller_account_id)
    buyer_name = _get_company_name(db, order.buyer_company_id)
    balance_after, frozen_after, held_after = apply_ledger_delta(
        db, account.id, 0, 0, -amount
    )
    _add_order_tx(
        db,
        account,
        "trade_release",
        amount,
        balance_after,
        frozen_after,
        held_after,
        buyer_name,
        remark,
        order,
        price=order.unit_price,
    )


def create_order(
    db: Session,
    seller_company_id: int,
    buyer_company_id: int,
    year: int,
    amount: float,
    unit_price: float | None = None,
    trade_date: str = "",
    remark: str = "",
    creator_company_id: int | None = None,
    idempotency_key: str | None = None,
) -> TradeOrder:
    """创建企业间交易订单。发起方（creator_company_id）创建即确认本方。

    卖方为发起方时，创建即占用其配额；买方为发起方时，待卖方确认后才占用。
    同一 ``idempotency_key`` 的重复提交返回首张订单，不重复占用。
    """
    if amount <= 0:
        raise ValueError("交易数量必须为正数")
    if unit_price is not None and float(unit_price) < 0:
        raise ValueError("单价不能为负数")
    if seller_company_id == buyer_company_id:
        raise ValueError("买卖双方不能为同一企业")

    creator_company_id = creator_company_id or seller_company_id
    if creator_company_id not in (seller_company_id, buyer_company_id):
        raise ValueError("仅买卖双方企业可发起该订单")

    if idempotency_key:
        existing = (
            db.query(TradeOrder)
            .filter(TradeOrder.idempotency_key == idempotency_key)
            .first()
        )
        if existing:
            return existing

    seller_account = _get_account(db, seller_company_id, year)
    buyer_account = _get_account(db, buyer_company_id, year)

    # 创建时订单行尚不存在，先以双方账户键 + 企业年度清缴键互斥；
    # 订单落库后的确认/撤销/交割再额外持有订单键。
    creation_keys = sorted(
        {
            account_lock_key(seller_account.id),
            account_lock_key(buyer_account.id),
            company_clear_key(seller_company_id, year),
            company_clear_key(buyer_company_id, year),
        }
    )

    with locked_accounts(creation_keys):
        try:
            with transactional(db):
                # 锁内重读账户，避免使用取锁前其他线程可能已改过的余额/占用快照
                db.expire_all()
                # 锁内复查：并发首单可能已提交
                if idempotency_key:
                    existing = (
                        db.query(TradeOrder)
                        .filter(TradeOrder.idempotency_key == idempotency_key)
                        .first()
                    )
                    if existing:
                        return existing

                seller_account = db.get(AllowanceAccount, seller_account.id)
                buyer_account = db.get(AllowanceAccount, buyer_account.id)

                order = TradeOrder(
                    order_no=f"TO{year}{datetime.utcnow().strftime('%m%d%H%M%S%f')}",
                    year=year,
                    seller_company_id=seller_company_id,
                    buyer_company_id=buyer_company_id,
                    seller_account_id=seller_account.id,
                    buyer_account_id=buyer_account.id,
                    amount=round(amount, 4),
                    unit_price=round(unit_price, 2) if unit_price is not None else None,
                    status=PENDING,
                    creator_company_id=creator_company_id,
                    trade_date=trade_date or _today(),
                    remark=remark,
                    idempotency_key=idempotency_key,
                )
                db.add(order)
                db.flush()  # 取得订单 ID（流水外键依赖），后续失败随事务回滚

                lock_rows_for_update(db, seller_account.id)
                lock_rows_for_update(db, buyer_account.id)

                seller_creates = creator_company_id == seller_company_id
                # 注意：_place_hold 内部的原子 UPDATE 会 expire_all()，
                # 因此先完成占用，再写订单确认标志，避免未 flush 的标志赋值被过期丢弃。
                if seller_creates:
                    # 卖方发起：创建即占用；余额不足整体回滚，订单不留痕
                    _place_hold(db, order, round(amount, 4))
                    order.held_amount = round(amount, 4)
                order.seller_confirmed = 1 if seller_creates else 0
                order.buyer_confirmed = 0 if seller_creates else 1

                db.flush()
                db.refresh(order)
        except IntegrityError as exc:
            # 并发重复下单撞全局幂等键：回滚后返回首张订单
            if idempotency_key and is_duplicate_submit(exc, "uq_trade_order_idem"):
                db.rollback()
                existing = (
                    db.query(TradeOrder)
                    .filter(TradeOrder.idempotency_key == idempotency_key)
                    .first()
                )
                if existing:
                    return existing
            raise
        return order


def confirm_order(db: Session, order_id: int, actor_company_id: int) -> TradeOrder:
    """交易方确认订单。卖方确认时占用配额；双方确认后订单待交割。

    本方已确认时为幂等操作：不重复占用，直接返回当前订单。
    """
    order = _get_order(db, order_id)
    _ensure_party(order, actor_company_id)
    if order.status == CONFIRMED:
        return order
    if order.status not in _ACTIVE_STATUSES:
        raise ValueError("订单已交割或已撤销，不能再确认")

    with locked_accounts(_order_lock_keys(order)):
        try:
            with transactional(db):
                # 锁前读取的订单对象可能是其他线程提交前的旧快照，
                # 必须使身份映射失效后在锁内重读最新状态/占用量/确认标志。
                db.expire_all()
                order = db.get(TradeOrder, order_id)
                seller = _is_seller(order, actor_company_id)

                if seller and not order.seller_confirmed:
                    # 卖方确认即占用：履约冻结或已有挂单占用导致可用不足时拒绝确认
                    _place_hold(db, order, float(order.amount))
                    order.seller_confirmed = 1
                    order.held_amount = float(order.amount)
                elif not seller and not order.buyer_confirmed:
                    order.buyer_confirmed = 1
                else:
                    # 本方此前已确认：幂等返回，不重复占用/入账
                    db.refresh(order)
                    return order

                if order.seller_confirmed and order.buyer_confirmed:
                    order.status = CONFIRMED
                    order.confirmed_at = datetime.utcnow()

                db.flush()
                db.refresh(order)
        except InsufficientBalanceError:
            raise ValueError("卖方可用配额不足（可能已被履约冻结或其他挂单占用），确认失败")
        return order


def cancel_order(
    db: Session,
    order_id: int,
    actor_company_id: int,
    reason: str = "",
) -> TradeOrder:
    """撤销未交割订单。仅买卖双方可撤销；已占用的卖方配额同步释放。"""
    order = _get_order(db, order_id)
    _ensure_party(order, actor_company_id)
    if order.status == CANCELLED:
        return order
    if order.status == DELIVERED:
        raise ValueError("订单已交割，不能撤销")

    with locked_accounts(_order_lock_keys(order)):
        try:
            with transactional(db):
                db.expire_all()
                order = db.get(TradeOrder, order_id)
                held = round(float(order.held_amount), 4)
                if held > 0:
                    _release_hold(
                        db,
                        order,
                        held,
                        f"订单 {order.order_no} 撤销，释放交易占用 {held} 吨",
                    )
                order.held_amount = 0
                order.status = CANCELLED
                order.cancelled_at = datetime.utcnow()
                order.cancelled_by_company_id = actor_company_id
                order.cancel_reason = (reason or "").strip()[:256]
                db.flush()
                db.refresh(order)
        except InsufficientBalanceError:
            raise ValueError("配额账本状态异常，撤销失败并已回滚")
        return order


def deliver_order(db: Session, order_id: int, actor_company_id: int | None = None) -> TradeOrder:
    """交割双方已确认的订单。

    卖方：释放交易占用并扣减持仓（trade_sell）；买方：增加持仓（trade_buy）。
    双方余额、占用与两笔成交流水在同一事务提交。重复交割为幂等返回。
    """
    order = _get_order(db, order_id)
    if actor_company_id is not None:
        _ensure_party(order, actor_company_id)
    if order.status == DELIVERED:
        return order
    if order.status == CANCELLED:
        raise ValueError("订单已撤销，不能交割")
    if order.status != CONFIRMED:
        raise ValueError("仅双方均已确认的订单可交割")

    with locked_accounts(_order_lock_keys(order)):
        try:
            with transactional(db):
                db.expire_all()
                order = db.get(TradeOrder, order_id)
                if order.status != CONFIRMED:
                    # 锁内复查：并发交割/撤销竞态下以后提交者看到的状态为准
                    if order.status == DELIVERED:
                        return order
                    raise ValueError("订单状态已变化，交割已中止")
                if not order.seller_confirmed or not order.buyer_confirmed:
                    raise ValueError("订单尚未经双方确认，不能交割")

                amount = round(float(order.amount), 4)
                seller = db.get(AllowanceAccount, order.seller_account_id)
                buyer = db.get(AllowanceAccount, order.buyer_account_id)
                lock_rows_for_update(db, seller.id)
                lock_rows_for_update(db, buyer.id)
                seller = db.get(AllowanceAccount, seller.id)
                buyer = db.get(AllowanceAccount, buyer.id)

                held = round(float(order.held_amount), 4)
                if held < amount:
                    # 卖方未走占用直接到交割的异常保护（正常流程不会发生）
                    raise ValueError("卖方交易占用与订单数量不一致，交割已中止")

                seller_name = _get_company_name(db, order.seller_company_id)
                buyer_name = _get_company_name(db, order.buyer_company_id)

                # 卖方：释放占用的同时扣减持仓。current -amount、held -amount，
                # 履约冻结不变；原子 UPDATE 保证扣减后持仓仍覆盖履约冻结。
                s_balance, s_frozen, s_held = apply_ledger_delta(
                    db, seller.id, -amount, 0, -amount
                )
                _add_order_tx(
                    db,
                    seller,
                    "trade_sell",
                    amount,
                    s_balance,
                    s_frozen,
                    s_held,
                    buyer_name,
                    f"订单 {order.order_no} 交割：向{buyer_name}卖出 {amount} 吨",
                    order,
                    price=order.unit_price,
                )

                # 买方：持仓增加，冻结/占用不变
                b_balance, b_frozen, b_held = apply_ledger_delta(db, buyer.id, amount, 0, 0)
                _add_order_tx(
                    db,
                    buyer,
                    "trade_buy",
                    amount,
                    b_balance,
                    b_frozen,
                    b_held,
                    seller_name,
                    f"订单 {order.order_no} 交割：从{seller_name}买入 {amount} 吨",
                    order,
                    price=order.unit_price,
                )

                order.held_amount = 0
                order.status = DELIVERED
                order.delivered_at = datetime.utcnow()
                db.flush()
                db.refresh(order)
        except InsufficientBalanceError:
            # 与并发履约清缴等路径冲突时由数据库条件 UPDATE 兜底拒绝，整体回滚
            raise ValueError("卖方可用配额不足（可能已被履约清缴），交割失败并已回滚")
        return order
