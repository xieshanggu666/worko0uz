"""企业间交易订单测试：双方确认、撤销、交割，以及交易占用与履约冻结的冲突。"""

import pytest

from app.models import (
    AllowanceAccount,
    AllowanceTransaction,
    Company,
    ComplianceRecord,
    TradeOrder,
)
from app.services.mrv_service import approve_report, generate_report, submit_report
from app.services.quota_service import allocate_quota
from app.services.trade_order_service import (
    CANCELLED,
    CONFIRMED,
    DELIVERED,
    PENDING,
    cancel_order,
    confirm_order,
    create_order,
    deliver_order,
)


def approx(value, rel=1e-6):
    return pytest.approx(float(value), rel=rel)


@pytest.fixture()
def two_companies(db, seed):
    """两家企业 + 2025 年度账户：甲（卖方）1000 吨，乙（买方）400 吨。"""
    buyer = Company(code="T-002", name="测试企业乙", industry="水泥", region="测试区")
    db.add(buyer)
    db.flush()
    allocate_quota(db, seed["company"].id, 2025, baseline=1000, allocation_amount=1000)
    allocate_quota(db, buyer.id, 2025, baseline=400, allocation_amount=400)
    db.commit()
    seller_acc = (
        db.query(AllowanceAccount).filter_by(company_id=seed["company"].id, year=2025).one()
    )
    buyer_acc = (
        db.query(AllowanceAccount).filter_by(company_id=buyer.id, year=2025).one()
    )
    return {
        "seller": seed["company"],
        "buyer": buyer,
        "seller_acc": seller_acc,
        "buyer_acc": buyer_acc,
    }


def _refresh(ctx, db):
    for obj in (ctx["seller_acc"], ctx["buyer_acc"]):
        db.refresh(obj)


class TestOrderLifecycle:
    def test_full_workflow_seller_creates(self, db, two_companies):
        """卖方发起（创建即确认+占用）→ 买方确认 → 交割：双方余额与流水正确。"""
        ctx = two_companies
        order = create_order(
            db,
            ctx["seller"].id,
            ctx["buyer"].id,
            2025,
            300,
            unit_price=85.5,
            creator_company_id=ctx["seller"].id,
        )
        assert order.status == PENDING
        assert bool(order.seller_confirmed) and not bool(order.buyer_confirmed)
        _refresh(ctx, db)
        assert float(ctx["seller_acc"].trade_held_balance) == approx(300)
        # 可用余额 = 持仓 - 冻结 - 交易占用
        assert float(ctx["seller_acc"].current_balance) - float(
            ctx["seller_acc"].trade_held_balance
        ) == approx(700)

        order = confirm_order(db, order.id, ctx["buyer"].id)
        assert order.status == CONFIRMED
        assert order.confirmed_at is not None

        order = deliver_order(db, order.id, ctx["buyer"].id)
        assert order.status == DELIVERED
        assert order.delivered_at is not None
        _refresh(ctx, db)
        # 卖方持仓 1000-300、占用清零；买方持仓 400+300
        assert float(ctx["seller_acc"].current_balance) == approx(700)
        assert float(ctx["seller_acc"].trade_held_balance) == approx(0)
        assert float(ctx["buyer_acc"].current_balance) == approx(700)

        sell_tx = (
            db.query(AllowanceTransaction)
            .filter(AllowanceTransaction.tx_type == "trade_sell")
            .one()
        )
        buy_tx = (
            db.query(AllowanceTransaction)
            .filter(AllowanceTransaction.tx_type == "trade_buy")
            .one()
        )
        assert float(sell_tx.amount) == approx(300)
        assert float(sell_tx.balance_after) == approx(700)
        assert float(sell_tx.trade_held_after) == approx(0)
        assert sell_tx.trade_order_id == order.id
        assert float(buy_tx.balance_after) == approx(700)
        assert buy_tx.trade_order_id == order.id
        assert float(sell_tx.price) == approx(85.5)
        # 占用与释放不应在该流程产生释放流水（交割直接核销占用）
        assert (
            db.query(AllowanceTransaction)
            .filter(AllowanceTransaction.tx_type == "trade_release")
            .count()
            == 0
        )

    def test_buyer_creates_then_seller_confirms_holds(self, db, two_companies):
        """买方发起：创建时不占用；卖方确认时才占用，双方确认后可交割。"""
        ctx = two_companies
        order = create_order(
            db,
            ctx["seller"].id,
            ctx["buyer"].id,
            2025,
            250,
            creator_company_id=ctx["buyer"].id,
        )
        assert not bool(order.seller_confirmed) and bool(order.buyer_confirmed)
        _refresh(ctx, db)
        assert float(ctx["seller_acc"].trade_held_balance) == approx(0)

        order = confirm_order(db, order.id, ctx["seller"].id)
        assert order.status == CONFIRMED
        _refresh(ctx, db)
        assert float(ctx["seller_acc"].trade_held_balance) == approx(250)

        deliver_order(db, order.id, ctx["seller"].id)
        _refresh(ctx, db)
        assert float(ctx["seller_acc"].current_balance) == approx(750)
        assert float(ctx["buyer_acc"].current_balance) == approx(650)

    def test_cancel_releases_hold(self, db, two_companies):
        """卖方发起后买方撤销（或任一方撤销）：占用释放，订单不可再交割。"""
        ctx = two_companies
        order = create_order(
            db,
            ctx["seller"].id,
            ctx["buyer"].id,
            2025,
            300,
            creator_company_id=ctx["seller"].id,
        )
        order = cancel_order(db, order.id, ctx["buyer"].id, reason="价格未谈拢")
        assert order.status == CANCELLED
        assert order.cancelled_by_company_id == ctx["buyer"].id
        assert order.cancel_reason == "价格未谈拢"
        _refresh(ctx, db)
        assert float(ctx["seller_acc"].trade_held_balance) == approx(0)
        release_tx = (
            db.query(AllowanceTransaction)
            .filter(AllowanceTransaction.tx_type == "trade_release")
            .one()
        )
        assert float(release_tx.amount) == approx(300)
        assert float(release_tx.trade_held_after) == approx(0)

        with pytest.raises(ValueError, match="已交割或已撤销"):
            confirm_order(db, order.id, ctx["buyer"].id)
        with pytest.raises(ValueError, match="已撤销"):
            deliver_order(db, order.id, ctx["buyer"].id)

    def test_cancel_by_seller_before_confirmation_no_hold(self, db, two_companies):
        """买方发起、卖方尚未确认时撤销：从未占用，无释放流水。"""
        ctx = two_companies
        order = create_order(
            db,
            ctx["seller"].id,
            ctx["buyer"].id,
            2025,
            200,
            creator_company_id=ctx["buyer"].id,
        )
        cancel_order(db, order.id, ctx["seller"].id)
        _refresh(ctx, db)
        assert float(ctx["seller_acc"].trade_held_balance) == approx(0)
        assert (
            db.query(AllowanceTransaction)
            .filter(AllowanceTransaction.tx_type == "trade_release")
            .count()
            == 0
        )

    def test_idempotent_confirmation_no_double_hold(self, db, two_companies):
        """卖方重复确认幂等：只占用一次。"""
        ctx = two_companies
        order = create_order(
            db,
            ctx["seller"].id,
            ctx["buyer"].id,
            2025,
            300,
            creator_company_id=ctx["buyer"].id,
        )
        confirm_order(db, order.id, ctx["seller"].id)
        again = confirm_order(db, order.id, ctx["seller"].id)
        assert again.id == order.id
        _refresh(ctx, db)
        assert float(ctx["seller_acc"].trade_held_balance) == approx(300)
        assert (
            db.query(AllowanceTransaction)
            .filter(AllowanceTransaction.tx_type == "trade_hold")
            .count()
            == 1
        )

    def test_deliver_is_idempotent(self, db, two_companies):
        """重复交割只划转一次。"""
        ctx = two_companies
        order = create_order(
            db,
            ctx["seller"].id,
            ctx["buyer"].id,
            2025,
            300,
            creator_company_id=ctx["seller"].id,
        )
        confirm_order(db, order.id, ctx["buyer"].id)
        first = deliver_order(db, order.id, ctx["seller"].id)
        second = deliver_order(db, order.id, ctx["seller"].id)
        assert first.id == second.id
        _refresh(ctx, db)
        assert float(ctx["seller_acc"].current_balance) == approx(700)
        assert float(ctx["buyer_acc"].current_balance) == approx(700)
        assert (
            db.query(AllowanceTransaction).filter(AllowanceTransaction.tx_type == "trade_buy").count()
            == 1
        )

    def test_cannot_deliver_before_both_confirmed(self, db, two_companies):
        """仅一方确认时交割被拒绝。"""
        ctx = two_companies
        order = create_order(
            db,
            ctx["seller"].id,
            ctx["buyer"].id,
            2025,
            300,
            creator_company_id=ctx["seller"].id,
        )
        with pytest.raises(ValueError, match="双方均已确认"):
            deliver_order(db, order.id, ctx["seller"].id)

    def test_create_order_idempotent_key(self, db, two_companies):
        """相同幂等键重复下单只生成一张订单。"""
        ctx = two_companies
        o1 = create_order(
            db, ctx["seller"].id, ctx["buyer"].id, 2025, 100,
            creator_company_id=ctx["seller"].id, idempotency_key="order-key-1",
        )
        o2 = create_order(
            db, ctx["seller"].id, ctx["buyer"].id, 2025, 100,
            creator_company_id=ctx["seller"].id, idempotency_key="order-key-1",
        )
        assert o1.id == o2.id
        assert db.query(TradeOrder).count() == 1
        _refresh(ctx, db)
        assert float(ctx["seller_acc"].trade_held_balance) == approx(100)

    def test_invalid_amount_and_same_party_rejected(self, db, two_companies):
        ctx = two_companies
        with pytest.raises(ValueError, match="正数"):
            create_order(db, ctx["seller"].id, ctx["buyer"].id, 2025, 0)
        with pytest.raises(ValueError, match="同一企业"):
            create_order(db, ctx["seller"].id, ctx["seller"].id, 2025, 100)

    def test_stranger_cannot_operate_order(self, db, two_companies):
        """非买卖双方企业不能确认/撤销/交割。"""
        ctx = two_companies
        outsider = Company(code="T-003", name="路人企业", industry="化工", region="测试区")
        db.add(outsider)
        db.flush()
        order = create_order(
            db,
            ctx["seller"].id,
            ctx["buyer"].id,
            2025,
            300,
            creator_company_id=ctx["seller"].id,
        )
        with pytest.raises(ValueError, match="仅订单的买卖双方"):
            confirm_order(db, order.id, outsider.id)
        with pytest.raises(ValueError, match="仅订单的买卖双方"):
            cancel_order(db, order.id, outsider.id)
        with pytest.raises(ValueError, match="仅订单的买卖双方"):
            deliver_order(db, order.id, outsider.id)


class TestTradeHoldVsComplianceFreeze:
    def test_seller_confirm_blocked_when_compliance_frozen(self, db, seed, two_companies):
        """卖方配额已被履约冻结时：买方发起订单，卖方确认占用被拒绝，订单保持待确认。"""
        from app.models import ActivityData
        from app.services.calculation_service import recalc_company_year

        ctx = two_companies
        # 卖方持仓 1000，新增 800 吨排放并批准报告 → 履约冻结 800，可用仅 200
        db.add(
            ActivityData(
                company_id=ctx["seller"].id, scope_id=seed["scope2"].id, year=2025,
                period="monthly", activity_type="外购电力", unit="MWh",
                quantity=int(800 / 0.5703), data_source="台账", verified=1,
            )
        )
        db.commit()
        recalc_company_year(db, ctx["seller"].id, 2025)
        report = generate_report(db, ctx["seller"].id, 2025)
        submit_report(db, report)
        approve_report(db, report, verifier_id=1)
        _refresh(ctx, db)
        frozen = float(ctx["seller_acc"].frozen_balance)
        assert frozen > 0
        available = (
            float(ctx["seller_acc"].current_balance)
            - float(ctx["seller_acc"].frozen_balance)
            - float(ctx["seller_acc"].trade_held_balance)
        )

        order = create_order(
            db,
            ctx["seller"].id,
            ctx["buyer"].id,
            2025,
            round(available + 1, 4),
            creator_company_id=ctx["buyer"].id,
        )
        with pytest.raises(ValueError, match="可用配额不足"):
            confirm_order(db, order.id, ctx["seller"].id)
        db.refresh(order)
        db.refresh(ctx["seller_acc"])
        assert order.status == PENDING
        assert float(ctx["seller_acc"].trade_held_balance) == approx(0)

        # 可用额度内的订单可以确认并交割
        ok_order = create_order(
            db,
            ctx["seller"].id,
            ctx["buyer"].id,
            2025,
            round(available, 4),
            creator_company_id=ctx["buyer"].id,
        )
        confirm_order(db, ok_order.id, ctx["seller"].id)
        deliver_order(db, ok_order.id, ctx["seller"].id)
        db.refresh(ctx["seller_acc"])
        # 履约冻结额不受交易影响
        assert float(ctx["seller_acc"].frozen_balance) == approx(frozen)

    def test_compliance_freeze_skips_trade_held_quota(self, db, two_companies, seed):
        """卖方挂单占用后，报告批准冻结只能冻结剩余可用部分，挂单配额不被重复预留。"""
        from app.models import ActivityData
        from app.services.calculation_service import annual_total, recalc_company_year

        ctx = two_companies
        # 卖方先挂单占用 600（持仓 1000，剩可用 400）
        order = create_order(
            db,
            ctx["seller"].id,
            ctx["buyer"].id,
            2025,
            600,
            creator_company_id=ctx["seller"].id,
        )
        _refresh(ctx, db)
        assert float(ctx["seller_acc"].trade_held_balance) == approx(600)

        # 随后新增排放 700 吨并批准报告：仅可冻结 400，缺口 300
        db.add(
            ActivityData(
                company_id=ctx["seller"].id, scope_id=seed["scope2"].id, year=2025,
                period="monthly", activity_type="外购电力", unit="MWh",
                quantity=int(700 / 0.5703), data_source="台账", verified=1,
            )
        )
        db.commit()
        recalc_company_year(db, ctx["seller"].id, 2025)
        emission = annual_total(db, ctx["seller"].id, 2025)
        report = generate_report(db, ctx["seller"].id, 2025)
        submit_report(db, report)
        approve_report(db, report, verifier_id=1)

        db.refresh(ctx["seller_acc"])
        record = (
            db.query(ComplianceRecord)
            .filter_by(company_id=ctx["seller"].id, year=2025, is_active=1)
            .one()
        )
        assert float(ctx["seller_acc"].trade_held_balance) == approx(600)
        assert float(ctx["seller_acc"].frozen_balance) == approx(400)
        assert float(record.frozen_amount) == approx(400)
        assert float(record.deficit) == approx(emission - 400)

        # 买方确认后交割：卖出的 600 吨中含此前冻结之外的部分，冻结额保持不变
        confirm_order(db, order.id, ctx["buyer"].id)
        deliver_order(db, order.id, ctx["buyer"].id)
        db.refresh(ctx["seller_acc"])
        assert float(ctx["seller_acc"].current_balance) == approx(400)
        assert float(ctx["seller_acc"].frozen_balance) == approx(400)
        assert float(ctx["seller_acc"].trade_held_balance) == approx(0)

    def test_clear_does_not_consume_trade_held_quota(self, db, two_companies):
        """无报告手动清缴时，挂单占用的配额不能被清缴扣减。"""
        ctx = two_companies
        create_order(
            db,
            ctx["seller"].id,
            ctx["buyer"].id,
            2025,
            700,
            creator_company_id=ctx["seller"].id,
        )
        # 持仓 1000、交易占用 700，清缴义务（无核算结果为 0，场景不适用）——
        # 直接校验可用口径：普通卖出也不能动用占用
        from app.services.trading_service import transfer

        _refresh(ctx, db)
        with pytest.raises(ValueError, match="可用配额"):
            transfer(db, ctx["seller_acc"], 400, "sell", tx_date="2025-06-01")
        db.refresh(ctx["seller_acc"])
        assert float(ctx["seller_acc"].current_balance) == approx(1000)
        assert float(ctx["seller_acc"].trade_held_balance) == approx(700)

    def test_deliver_coexists_with_compliance_freeze(self, db, two_companies, seed):
        """挂单占用 + 履约冻结并存且恰好瓜分全部持仓时，交割后持仓仍覆盖冻结。

        场景：持仓 1000，挂单占用 600；批准报告冻结剩余可用约 400。
        交割时卖方释放 600 占用并扣减 600 持仓，原子 UPDATE 校验
        ``current(≈400) >= frozen(≈400) + held(0)`` 通过；买方入账 600。
        """
        from app.models import ActivityData
        from app.services.calculation_service import annual_total, recalc_company_year

        ctx = two_companies
        # 持仓 1000：挂单 600；新增排放约 400 → 冻结剩余可用
        order = create_order(
            db,
            ctx["seller"].id,
            ctx["buyer"].id,
            2025,
            600,
            creator_company_id=ctx["seller"].id,
        )
        db.add(
            ActivityData(
                company_id=ctx["seller"].id, scope_id=seed["scope2"].id, year=2025,
                period="monthly", activity_type="外购电力", unit="MWh",
                quantity=int(400 / 0.5703), data_source="台账", verified=1,
            )
        )
        db.commit()
        recalc_company_year(db, ctx["seller"].id, 2025)
        emission = annual_total(db, ctx["seller"].id, 2025)
        report = generate_report(db, ctx["seller"].id, 2025)
        submit_report(db, report)
        approve_report(db, report, verifier_id=1)
        confirm_order(db, order.id, ctx["buyer"].id)

        # 交割 600：卖方持仓 1000-600=400，恰好仍覆盖冻结额，成功
        deliver_order(db, order.id, ctx["buyer"].id)
        _refresh(ctx, db)
        assert float(ctx["seller_acc"].current_balance) == approx(400)
        assert float(ctx["seller_acc"].frozen_balance) == approx(emission)
        assert float(ctx["buyer_acc"].current_balance) == approx(1000)
