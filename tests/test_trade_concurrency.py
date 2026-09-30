"""企业间交易订单并发一致性测试。

覆盖：
- 并发生成/确认/交割同一订单：只占用一次、只交割一次，双方流水快照链严格一致；
- 相同幂等键并发下单只生成一张订单；
- 卖方确认占用与报告批准履约冻结并发：frozen + held 永不超过持仓，互不侵占；
- 挂单占用与普通卖出并发：可用余额（持仓 - 冻结 - 占用）不被超额扣减；
- 撤销与确认并发：要么占用后存在、要么未占用，不出现占用泄漏/重复释放。
"""

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import Base
from app.models import (
    ActivityData,
    AllowanceAccount,
    AllowanceTransaction,
    Company,
)
from app.services.mrv_service import approve_report, generate_report, submit_report
from app.services.calculation_service import recalc_company_year
from app.services.quota_service import allocate_quota
from app.services.trade_order_service import (
    CANCELLED,
    CONFIRMED,
    DELIVERED,
    cancel_order,
    confirm_order,
    create_order,
    deliver_order,
)
from app.services.trading_service import transfer


@pytest.fixture()
def db(tmp_path):
    """文件型临时库：多线程各自独立连接但共享同一份数据。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'trade_concurrency.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _busy_timeout(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture()
def market(db):
    """甲（卖方，持仓1000）、乙（买方，持仓500）两家企业 2025 年度账户。"""
    seller = Company(code="M-001", name="卖方企业", industry="电力", region="华东")
    buyer = Company(code="M-002", name="买方企业", industry="水泥", region="华北")
    db.add_all([seller, buyer])
    db.flush()
    scope2 = None
    from app.models import EmissionScope

    scope2 = EmissionScope(
        company_id=seller.id, scope="2", category="外购电力", name="厂区用电"
    )
    db.add(scope2)
    allocate_quota(db, seller.id, 2025, 1000, 1000, 0)
    allocate_quota(db, buyer.id, 2025, 500, 500, 0)
    db.commit()
    return {
        "seller": seller,
        "buyer": buyer,
        "scope2": scope2,
        "seller_acc": db.query(AllowanceAccount).filter_by(company_id=seller.id, year=2025).one(),
        "buyer_acc": db.query(AllowanceAccount).filter_by(company_id=buyer.id, year=2025).one(),
    }


def approx(value, rel=1e-6):
    return pytest.approx(float(value), rel=rel)


def _fresh_session(db):
    return Session(bind=db.bind)


_CURRENT_SIGNS = {
    "allocation": 1, "buy": 1, "transfer_in": 1, "reversal": 1, "trade_buy": 1,
    "sell": -1, "transfer_out": -1, "offset": -1, "clear": -1,
    "frozen_clear": -1, "trade_sell": -1,
    "freeze": 0, "trade_hold": 0, "trade_release": 0,
}
_FROZEN_SIGNS = {"freeze": 1, "frozen_clear": -1, "reversal": -1}
_HELD_SIGNS = {"trade_hold": 1, "trade_release": -1, "trade_sell": -1}


def _snapshot_consistent(db, account_id):
    """流水不变量：持仓/冻结/占用三个快照逐笔可推算，末笔等于账户当前值。"""
    txs = (
        db.query(AllowanceTransaction)
        .filter(AllowanceTransaction.account_id == account_id)
        .order_by(AllowanceTransaction.id.asc())
        .all()
    )
    exp_current = exp_frozen = exp_held = 0.0
    for tx in txs:
        exp_current = round(exp_current + _CURRENT_SIGNS.get(tx.tx_type, 0) * float(tx.amount), 4)
        exp_frozen = round(exp_frozen + _FROZEN_SIGNS.get(tx.tx_type, 0) * float(tx.amount), 4)
        exp_held = round(exp_held + _HELD_SIGNS.get(tx.tx_type, 0) * float(tx.amount), 4)
        assert float(tx.balance_after) == approx(exp_current), (
            f"流水 #{tx.id}({tx.tx_type}) 持仓快照 {tx.balance_after} != 推算 {exp_current}"
        )
        assert float(tx.frozen_after or 0) == approx(exp_frozen), (
            f"流水 #{tx.id}({tx.tx_type}) 冻结快照 {tx.frozen_after} != 推算 {exp_frozen}"
        )
        assert float(tx.trade_held_after or 0) == approx(exp_held), (
            f"流水 #{tx.id}({tx.tx_type}) 占用快照 {tx.trade_held_after} != 推算 {exp_held}"
        )
    account = db.get(AllowanceAccount, account_id)
    assert float(account.current_balance) == approx(exp_current)
    assert float(account.frozen_balance) == approx(exp_frozen)
    assert float(account.trade_held_balance) == approx(exp_held)
    # 核心冲突不变量：履约冻结 + 交易占用 <= 持仓
    assert float(account.frozen_balance) + float(account.trade_held_balance) <= float(
        account.current_balance
    ) + 1e-9
    return exp_current, exp_frozen, exp_held, txs


class TestOrderConcurrency:
    def test_parallel_deliver_settles_once(self, db, market):
        """5 个线程同时交割同一张已确认订单：只划转一次，双方各一笔成交流水。"""
        order = create_order(
            db, market["seller"].id, market["buyer"].id, 2025, 300,
            creator_company_id=market["seller"].id,
        )
        confirm_order(db, order.id, market["buyer"].id)
        order_id = order.id

        def worker():
            session = _fresh_session(db)
            try:
                rec = deliver_order(session, order_id, None)
                return rec.status
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=5) as pool:
            statuses = list(pool.map(lambda _: worker(), range(5)))

        db.expire_all()
        assert set(statuses) == {DELIVERED}
        assert (
            db.query(AllowanceTransaction).filter(AllowanceTransaction.tx_type == "trade_buy").count()
            == 1
        )
        assert (
            db.query(AllowanceTransaction).filter(AllowanceTransaction.tx_type == "trade_sell").count()
            == 1
        )
        _snapshot_consistent(db, market["seller_acc"].id)
        _snapshot_consistent(db, market["buyer_acc"].id)
        s = db.get(AllowanceAccount, market["seller_acc"].id)
        b = db.get(AllowanceAccount, market["buyer_acc"].id)
        assert float(s.current_balance) == approx(700)
        assert float(s.trade_held_balance) == approx(0)
        assert float(b.current_balance) == approx(800)

    def test_parallel_confirm_holds_once(self, db, market):
        """卖方多线程同时确认：只占用一次，订单进入已确认。"""
        order = create_order(
            db, market["seller"].id, market["buyer"].id, 2025, 200,
            creator_company_id=market["buyer"].id,
        )
        order_id = order.id

        def worker():
            session = _fresh_session(db)
            try:
                rec = confirm_order(session, order_id, market["seller"].id)
                return rec.status, bool(rec.seller_confirmed), float(rec.held_amount)
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(lambda _: worker(), range(5)))

        db.expire_all()
        assert {status for status, _, _ in results} == {CONFIRMED}
        assert all(held == 200 for _, _, held in results)
        assert (
            db.query(AllowanceTransaction).filter(AllowanceTransaction.tx_type == "trade_hold").count()
            == 1
        )
        _snapshot_consistent(db, market["seller_acc"].id)

    def test_parallel_create_same_idempotency_key(self, db, market):
        """相同幂等键并发下单：只生成一张订单、只占用一次。"""
        def worker():
            session = _fresh_session(db)
            try:
                rec = create_order(
                    session,
                    market["seller"].id,
                    market["buyer"].id,
                    2025,
                    150,
                    creator_company_id=market["seller"].id,
                    idempotency_key="parallel-order-key",
                )
                return rec.id
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=5) as pool:
            ids = list(pool.map(lambda _: worker(), range(5)))

        db.expire_all()
        assert set(ids) == {ids[0]}
        from app.models import TradeOrder

        assert db.query(TradeOrder).count() == 1
        _snapshot_consistent(db, market["seller_acc"].id)
        s = db.get(AllowanceAccount, market["seller_acc"].id)
        assert float(s.trade_held_balance) == approx(150)

    def test_parallel_cancel_and_confirm_no_leak(self, db, market):
        """买方已发起、卖方确认与卖方撤销并发：终态唯一，占用不泄漏也不会为负。"""
        order = create_order(
            db, market["seller"].id, market["buyer"].id, 2025, 250,
            creator_company_id=market["buyer"].id,
        )
        order_id = order.id

        def do_confirm():
            session = _fresh_session(db)
            try:
                return ("confirm", confirm_order(session, order_id, market["seller"].id).status)
            except ValueError:
                return ("confirm", "rejected")
            finally:
                session.close()

        def do_cancel():
            session = _fresh_session(db)
            try:
                return ("cancel", cancel_order(session, order_id, market["seller"].id).status)
            except ValueError:
                return ("cancel", "rejected")
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=4) as pool:
            futs = [pool.submit(do_confirm) for _ in range(2)] + [
                pool.submit(do_cancel) for _ in range(2)
            ]
            outcomes = [f.result() for f in futs]

        db.expire_all()
        from app.models import TradeOrder

        final = db.get(TradeOrder, order_id)
        assert final.status in (CONFIRMED, CANCELLED)
        s = db.get(AllowanceAccount, market["seller_acc"].id)
        if final.status == CONFIRMED:
            assert float(s.trade_held_balance) == approx(250)
        else:
            # 撤销先提交：占用必须已释放归零，且没有残留流水破坏快照链
            assert float(s.trade_held_balance) == approx(0)
        _snapshot_consistent(db, market["seller_acc"].id)


class TestHoldVsFreezeConflict:
    def test_parallel_order_confirm_and_report_freeze_never_overlap(self, db, market, seed=None):
        """卖方确认占用与报告批准履约冻结并发：frozen + held <= 持仓恒成立。

        10 个订单（各占用 150）的卖方确认与报告批准（试图冻结 700）并发执行。
        无论提交顺序如何，两类预留之和不得超过持仓 1000，账本三方快照一致。
        """
        # 预置约 700 吨排放活动并核算（批准由线程内完成）
        db.add(
            ActivityData(
                company_id=market["seller"].id, scope_id=market["scope2"].id, year=2025,
                period="monthly", activity_type="外购电力", unit="MWh",
                quantity=int(700 / 0.5703), data_source="台账", verified=1,
            )
        )
        db.commit()
        recalc_company_year(db, market["seller"].id, 2025)

        # 由买方先发起 10 张待确认订单（此时不占用）
        order_ids = []
        for _ in range(10):
            o = create_order(
                db,
                market["seller"].id,
                market["buyer"].id,
                2025,
                150,
                creator_company_id=market["buyer"].id,
            )
            order_ids.append(o.id)

        def do_confirm(order_id):
            session = _fresh_session(db)
            try:
                confirm_order(session, order_id, market["seller"].id)
                return "held"
            except ValueError:
                return "rejected"
            finally:
                session.close()

        def do_approve():
            session = _fresh_session(db)
            try:
                report = generate_report(session, market["seller"].id, 2025)
                submit_report(session, report)
                approve_report(session, report, verifier_id=1)
                return "approved"
            except Exception:  # noqa: BLE001 - 与并发重建报告冲突允许失败，不得脏写
                return "approve_rejected"
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = [pool.submit(do_confirm, oid) for oid in order_ids] + [
                pool.submit(do_approve) for _ in range(3)
            ]
            results = [f.result() for f in futs]

        db.expire_all()
        s = db.get(AllowanceAccount, market["seller_acc"].id)
        # 不可变核心断言：两类预留互不侵占
        assert float(s.frozen_balance) + float(s.trade_held_balance) <= 1000 + 1e-9
        held_count = sum(1 for r in results if r == "held")
        assert float(s.trade_held_balance) == approx(150 * held_count)
        # 最多占用 6 张（900），为履约冻结留足或互不重叠；快照链一致
        _snapshot_consistent(db, market["seller_acc"].id)
        _snapshot_consistent(db, market["buyer_acc"].id)

    def test_held_order_blocks_parallel_sell(self, db, market):
        """挂单占用后，并发普通卖出不能动用挂单配额，总额不超过可用部分。"""
        # 卖方先挂单占用 700（买方发起、卖方确认）
        order = create_order(
            db, market["seller"].id, market["buyer"].id, 2025, 700,
            creator_company_id=market["buyer"].id,
        )
        confirm_order(db, order.id, market["seller"].id)
        account_id = market["seller_acc"].id

        def worker():
            session = _fresh_session(db)
            try:
                transfer(session, session.get(AllowanceAccount, account_id), 100, "sell",
                         counterparty="场外买方", tx_date="2025-06-01")
                return True
            except ValueError:
                return False
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=6) as pool:
            outcomes = list(pool.map(lambda _: worker(), range(6)))

        db.expire_all()
        # 可用仅 300：恰好 3 笔成功、3 笔拒绝
        assert outcomes.count(True) == 3
        assert outcomes.count(False) == 3
        current, _, held, txs = _snapshot_consistent(db, account_id)
        assert held == approx(700)
        assert current == approx(700)  # 1000 - 300 卖出，占用不动
        sold = sum(float(t.amount) for t in txs if t.tx_type == "sell")
        assert sold == approx(300)
