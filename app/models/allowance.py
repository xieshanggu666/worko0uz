from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)

from app.core.database import Base


class Quota(Base):
    """年度配额分配：免费配额与调整。"""

    __tablename__ = "quotas"
    __table_args__ = (
        # 同一企业同一年度只能有一条配额记录，并发分配由数据库兜底幂等
        UniqueConstraint("company_id", "year", name="uq_quota_company_year"),
    )

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    year = Column(Integer, nullable=False, index=True)
    baseline = Column(Numeric(18, 4), nullable=False, default=0)      # 历史基准排放
    allocation_amount = Column(Numeric(18, 4), nullable=False, default=0)  # 免费配额（tCO2）
    adjustment = Column(Numeric(18, 4), nullable=False, default=0)    # 调整量（可为负）
    total = Column(Numeric(18, 4), nullable=False, default=0)         # 最终配额
    status = Column(String(16), nullable=False, default="pending")    # pending/allocated/frozen/cleared
    allocated_at = Column(DateTime, nullable=True)


class AllowanceAccount(Base):
    """配额账户：企业年度配额持仓。"""

    __tablename__ = "allowance_accounts"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    year = Column(Integer, nullable=False, index=True)
    opening_balance = Column(Numeric(18, 4), nullable=False, default=0)
    current_balance = Column(Numeric(18, 4), nullable=False, default=0)
    frozen_balance = Column(Numeric(18, 4), nullable=False, default=0)   # 履约冻结
    trade_held_balance = Column(Numeric(18, 4), nullable=False, default=0)  # 企业间交易占用（卖方已确认未交割）
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class AllowanceTransaction(Base):
    """配额划转与交易台账。"""

    __tablename__ = "allowance_transactions"
    __table_args__ = (
        # 客户端幂等键：同一账户重复提交（双击/重试/超时重发）只入账一次。
        # NULL 不参与唯一约束，未携带幂等键的请求不受影响。
        UniqueConstraint("account_id", "idempotency_key", name="uq_tx_account_idem"),
    )

    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("allowance_accounts.id"), nullable=False, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    # allocation/buy/sell/transfer_in/transfer_out/freeze/clear/frozen_clear/offset/reversal
    # trade_hold（交易占用）/trade_release（占用释放）/trade_sell（企业间卖出）/trade_buy（企业间买入）
    tx_type = Column(String(24), nullable=False)
    amount = Column(Numeric(18, 4), nullable=False, default=0)
    counterparty = Column(String(128), nullable=False, default="")
    price = Column(Numeric(18, 2), nullable=True)
    tx_date = Column(String(10), nullable=False, default="")
    balance_after = Column(Numeric(18, 4), nullable=False, default=0)
    frozen_after = Column(Numeric(18, 4), nullable=False, default=0)
    trade_held_after = Column(Numeric(18, 4), nullable=False, default=0)  # 交易占用快照
    trade_order_id = Column(Integer, ForeignKey("trade_orders.id"), nullable=True, index=True)  # 关联企业间订单
    remark = Column(String(256), nullable=False, default="")
    idempotency_key = Column(String(64), nullable=True)  # 客户端去重键（UUID），同账户唯一
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class ComplianceRecord(Base):
    """年度履约记录：清缴配额抵扣实际排放。"""

    __tablename__ = "compliance_records"
    __table_args__ = (
        # 仅活跃记录保持企业+年度唯一；报告冲正归档后允许重新批准生成新记录。
        # SQLite/PostgreSQL 使用部分索引，其他数据库降级为普通索引并由应用锁兜底。
        Index(
            "uq_compliance_active_company_year",
            "company_id",
            "year",
            unique=True,
            sqlite_where=text("is_active = 1"),
            postgresql_where=text("is_active = 1"),
        ),
    )

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    year = Column(Integer, nullable=False, index=True)
    verified_emission = Column(Numeric(18, 4), nullable=False, default=0)  # 批准报告确认的排放量
    cleared_amount = Column(Numeric(18, 4), nullable=False, default=0)     # 已清缴配额
    frozen_amount = Column(Numeric(18, 4), nullable=False, default=0)      # 批准后冻结、尚未清缴的配额
    deficit = Column(Numeric(18, 4), nullable=False, default=0)            # 缺口
    status = Column(String(16), nullable=False, default="pending")         # pending/compliant/deficit/reversed
    deadline = Column(String(10), nullable=False, default="")
    report_id = Column(Integer, ForeignKey("mrv_reports.id"), nullable=True, index=True)
    is_active = Column(Integer, nullable=False, default=1)                 # 0=报告冲正后归档，重新批准可建新记录
    idempotency_key = Column(String(64), nullable=True)  # 清缴请求去重键（全局唯一）
    cleared_at = Column(DateTime, nullable=True)


class TradeOrder(Base):
    """企业间配额交易订单：双方确认 → 交割，或撤销。

    状态机：
    - pending_confirmation（待双方确认，发起方创建即确认本方）
    - confirmed（双方均确认，等待交割）
    - delivered（已交割：卖方扣减、买方入账，占用同步释放）
    - cancelled（交割前任一方撤销，卖方占用已释放）

    卖方在其确认时即冻结“交易占用”（trade_held_balance），与履约冻结
    （frozen_balance）相互独立；可用余额 = 持仓 - 履约冻结 - 交易占用。
    """

    __tablename__ = "trade_orders"
    __table_args__ = (
        # 下单幂等：同一创建请求（双击/重试）只生成一张订单。NULL 不参与唯一约束。
        UniqueConstraint("idempotency_key", name="uq_trade_order_idem"),
    )

    id = Column(Integer, primary_key=True)
    order_no = Column(String(40), nullable=False, unique=True, index=True)
    year = Column(Integer, nullable=False, index=True)
    seller_company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    buyer_company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    seller_account_id = Column(Integer, ForeignKey("allowance_accounts.id"), nullable=False)
    buyer_account_id = Column(Integer, ForeignKey("allowance_accounts.id"), nullable=False)
    amount = Column(Numeric(18, 4), nullable=False)           # 成交数量（tCO2）
    unit_price = Column(Numeric(18, 2), nullable=True)        # 单价（元/吨）
    status = Column(String(24), nullable=False, default="pending_confirmation", index=True)
    seller_confirmed = Column(Integer, nullable=False, default=0)
    buyer_confirmed = Column(Integer, nullable=False, default=0)
    held_amount = Column(Numeric(18, 4), nullable=False, default=0)  # 卖方已交易占用量
    creator_company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    confirmed_at = Column(DateTime, nullable=True)
    delivered_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)
    cancelled_by_company_id = Column(Integer, ForeignKey("companies.id"), nullable=True)
    cancel_reason = Column(String(256), nullable=False, default="")
    trade_date = Column(String(10), nullable=False, default="")       # 成交/交割日期
    remark = Column(String(256), nullable=False, default="")
    idempotency_key = Column(String(64), nullable=True)  # 下单去重键（全局唯一）
