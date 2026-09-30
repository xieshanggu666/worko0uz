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
    tx_type = Column(String(24), nullable=False)   # allocation/buy/sell/transfer_in/transfer_out/freeze/clear/frozen_clear/offset/reversal
    amount = Column(Numeric(18, 4), nullable=False, default=0)
    counterparty = Column(String(128), nullable=False, default="")
    price = Column(Numeric(18, 2), nullable=True)
    tx_date = Column(String(10), nullable=False, default="")
    balance_after = Column(Numeric(18, 4), nullable=False, default=0)
    frozen_after = Column(Numeric(18, 4), nullable=False, default=0)
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
