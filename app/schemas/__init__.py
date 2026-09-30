from pydantic import BaseModel, Field


class LoginIn(BaseModel):
    username: str
    password: str


class CompanyIn(BaseModel):
    code: str
    name: str
    industry: str = ""
    region: str = ""
    boundary_desc: str = ""


class ScopeIn(BaseModel):
    scope: str = Field(pattern="^[123]$")
    category: str = ""
    name: str = ""
    description: str = ""


class ActivityIn(BaseModel):
    scope_id: int
    year: int
    period: str = "monthly"
    activity_type: str
    unit: str = ""
    quantity: float
    data_source: str = ""


class FactorIn(BaseModel):
    factor_code: str
    name: str
    scope: str = Field(default="1", pattern="^[123]$")
    unit: str = "tCO2/单位"
    value: float
    source: str = ""
    valid_from: str = ""
    valid_to: str | None = None


class QuotaIn(BaseModel):
    company_id: int
    year: int
    baseline: float = 0
    allocation_amount: float
    adjustment: float = 0


class TransferIn(BaseModel):
    amount: float
    tx_type: str = "sell"
    counterparty: str = ""
    price: float | None = None
    tx_date: str = ""
    remark: str = ""
    # 客户端幂等键：同账户相同键的重复提交只入账一次（也可用 Idempotency-Key 请求头）
    idempotency_key: str | None = None


class TradeOrderIn(BaseModel):
    seller_company_id: int
    buyer_company_id: int
    year: int
    amount: float = Field(gt=0)
    unit_price: float | None = Field(default=None, ge=0)
    trade_date: str = ""
    remark: str = ""
    # 监管代发起时指定发起方企业（创建即确认该方）；企业发起时由服务端强制为本企业
    creator_company_id: int | None = None
    # 下单幂等键（也可用 Idempotency-Key 请求头）：重复提交只生成一张订单
    idempotency_key: str | None = None


class TradeOrderCancelIn(BaseModel):
    reason: str = Field(default="", max_length=256)


class ReportReversalIn(BaseModel):
    reason: str = Field(min_length=2, max_length=500)
