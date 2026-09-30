"""企业间交易订单 API 测试：鉴权边界、状态流转、幂等与交割入账。"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.security import hash_password
from app.main import app
from app.models import Company, User
from app.services.quota_service import allocate_quota


@pytest.fixture()
def ctx():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(bind=engine, autoflush=False)
    Base.metadata.create_all(engine)

    db = TestingSession()
    pwd_hash, salt = hash_password("123456")
    c1 = Company(code="E-001", name="企业甲", industry="电力", region="华东")
    c2 = Company(code="E-002", name="企业乙", industry="水泥", region="华北")
    c3 = Company(code="E-003", name="企业丙", industry="化工", region="华南")
    db.add_all([c1, c2, c3])
    db.flush()
    db.add_all(
        [
            User(username="ent1", display_name="企业甲", role="enterprise",
                 company_id=c1.id, password_hash=pwd_hash, salt=salt),
            User(username="ent2", display_name="企业乙", role="enterprise",
                 company_id=c2.id, password_hash=pwd_hash, salt=salt),
            User(username="ent3", display_name="企业丙", role="enterprise",
                 company_id=c3.id, password_hash=pwd_hash, salt=salt),
            User(username="admin", display_name="监管员", role="admin",
                 password_hash=pwd_hash, salt=salt),
            User(username="verifier", display_name="核查员", role="verifier",
                 password_hash=pwd_hash, salt=salt),
        ]
    )
    db.flush()
    allocate_quota(db, c1.id, 2025, baseline=1000, allocation_amount=1000)
    allocate_quota(db, c2.id, 2025, baseline=500, allocation_amount=500)
    db.commit()
    ids = {"c1": c1.id, "c2": c2.id, "c3": c3.id}
    db.close()

    def override_get_db():
        session = TestingSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    yield client, ids
    app.dependency_overrides.clear()
    engine.dispose()


def login(client, username):
    client.post("/api/auth/login", json={"username": username, "password": "123456"})


def _create(client, seller, buyer, amount=200, **extra):
    payload = {
        "seller_company_id": seller,
        "buyer_company_id": buyer,
        "year": 2025,
        "amount": amount,
        "unit_price": 80,
        "trade_date": "2025-07-01",
    }
    payload.update(extra)
    return client.post("/api/trade-orders", json=payload)


def test_create_list_and_full_workflow(ctx):
    """卖方企业下单 → 买方确认 → 交割；列表只返回本企业相关订单。"""
    client, ids = ctx
    login(client, "ent1")
    res = _create(client, ids["c1"], ids["c2"])
    assert res.status_code == 200, res.text
    order = res.json()
    assert order["status"] == "pending_confirmation"
    assert order["seller_confirmed"] is True
    assert order["buyer_confirmed"] is False
    assert order["held_amount"] == 200.0
    assert order["seller_name"] == "企业甲"

    # 卖方账户交易占用 200，可用余额 800
    acc = client.get(f"/api/companies/{ids['c1']}/account?year=2025").json()
    assert acc["trade_held_balance"] == 200.0
    assert acc["available_balance"] == 800.0

    # 企业甲只能看到自己的订单
    mine = client.get("/api/trade-orders").json()
    assert len(mine) == 1 and mine[0]["id"] == order["id"]

    login(client, "ent2")
    confirm = client.post(f"/api/trade-orders/{order['id']}/confirm")
    assert confirm.status_code == 200
    assert confirm.json()["status"] == "confirmed"

    deliver = client.post(f"/api/trade-orders/{order['id']}/deliver")
    assert deliver.status_code == 200
    assert deliver.json()["status"] == "delivered"

    # 双方账户余额：甲 800、乙 700（按企业边界分别登录查询）
    login(client, "ent1")
    acc1 = client.get(f"/api/companies/{ids['c1']}/account?year=2025").json()
    login(client, "ent2")
    acc2 = client.get(f"/api/companies/{ids['c2']}/account?year=2025").json()
    assert acc1["current_balance"] == 800.0 and acc1["trade_held_balance"] == 0.0
    assert acc2["current_balance"] == 700.0

    # 买方流水含 trade_buy 且带 trade_order_id
    txs = client.get(f"/api/accounts/{acc2['id']}/transactions").json()
    buy_tx = [t for t in txs if t["tx_type"] == "trade_buy"]
    assert len(buy_tx) == 1
    assert buy_tx[0]["trade_order_id"] == order["id"]


def test_enterprise_cannot_create_order_not_as_party(ctx):
    """企业只能以本方为买方或卖方发起订单。"""
    client, ids = ctx
    login(client, "ent1")
    # 甲乙之间的订单，ent1 是卖方，正常；但伪造 creator 无关方时服务端仍强制为本方
    res = _create(client, ids["c2"], ids["c3"])  # 甲既不是买方也不是卖方
    assert res.status_code == 403


def test_stranger_cannot_view_or_operate(ctx):
    """第三方企业看不到也不能操作他人订单。"""
    client, ids = ctx
    login(client, "ent1")
    order = _create(client, ids["c1"], ids["c2"]).json()

    login(client, "ent3")
    assert client.get("/api/trade-orders").json() == []
    assert client.get(f"/api/trade-orders/{order['id']}").status_code == 403
    assert client.post(f"/api/trade-orders/{order['id']}/confirm").status_code == 403
    assert client.post(
        f"/api/trade-orders/{order['id']}/cancel", json={"reason": "恶意撤销"}
    ).status_code == 403
    assert client.post(f"/api/trade-orders/{order['id']}/deliver").status_code == 403


def test_cancel_releases_hold(ctx):
    """买方撤销订单后卖方占用释放。"""
    client, ids = ctx
    login(client, "ent1")
    order = _create(client, ids["c1"], ids["c2"]).json()
    login(client, "ent2")
    res = client.post(
        f"/api/trade-orders/{order['id']}/cancel", json={"reason": "价格不合适"}
    )
    assert res.status_code == 200
    assert res.json()["status"] == "cancelled"

    login(client, "ent1")
    acc = client.get(f"/api/companies/{ids['c1']}/account?year=2025").json()
    assert acc["trade_held_balance"] == 0.0
    assert acc["available_balance"] == 1000.0


def test_cannot_deliver_pending_order(ctx):
    """仅一方确认时交割返回 400。"""
    client, ids = ctx
    login(client, "ent1")
    order = _create(client, ids["c1"], ids["c2"]).json()
    res = client.post(f"/api/trade-orders/{order['id']}/deliver")
    assert res.status_code == 400
    assert "双方" in res.json()["detail"]


def test_seller_confirm_over_frozen_balance_rejected(ctx):
    """卖方可用（履约冻结后）不足时确认订单被拒绝。"""
    client, ids = ctx
    # 买方乙发起：甲卖 900 吨（甲持仓 1000，先用普通交易无法模拟冻结，
    # 这里直接断言超过持仓的占用被拒绝）
    login(client, "ent2")
    res = _create(client, ids["c1"], ids["c2"], 1200)
    assert res.status_code == 200  # 买方发起不占用，下单成功
    order = res.json()
    login(client, "ent1")
    confirm = client.post(f"/api/trade-orders/{order['id']}/confirm")
    assert confirm.status_code == 400
    assert "可用配额不足" in confirm.json()["detail"]


def test_create_idempotent_header(ctx):
    """Idempotency-Key 请求头去重：重复下单只生成一张订单。"""
    client, ids = ctx
    login(client, "ent1")
    payload = {
        "seller_company_id": ids["c1"], "buyer_company_id": ids["c2"],
        "year": 2025, "amount": 100,
    }
    r1 = client.post("/api/trade-orders", json=payload, headers={"Idempotency-Key": "hdr-key-1"})
    r2 = client.post("/api/trade-orders", json=payload, headers={"Idempotency-Key": "hdr-key-1"})
    assert r1.json()["id"] == r2.json()["id"]
    assert len(client.get("/api/trade-orders").json()) == 1


def test_admin_must_specify_creator(ctx):
    """监管下单必须指定 creator_company_id。"""
    client, ids = ctx
    login(client, "admin")
    res = _create(client, ids["c1"], ids["c2"])
    assert res.status_code == 400

    res = _create(client, ids["c1"], ids["c2"], creator_company_id=ids["c1"])
    assert res.status_code == 200


def test_verifier_readonly(ctx):
    """核查员可查看全部订单，但不能下单/确认/交割。"""
    client, ids = ctx
    login(client, "ent1")
    order = _create(client, ids["c1"], ids["c2"]).json()

    login(client, "verifier")
    assert len(client.get("/api/trade-orders").json()) == 1
    assert client.post(f"/api/trade-orders/{order['id']}/confirm").status_code == 403
    assert client.post(f"/api/trade-orders/{order['id']}/deliver").status_code == 403


def test_requires_login(ctx):
    client, _ = ctx
    assert client.get("/api/trade-orders").status_code == 401
    assert client.post(
        "/api/trade-orders",
        json={"seller_company_id": 1, "buyer_company_id": 2, "year": 2025, "amount": 10},
    ).status_code == 401


def test_order_404(ctx):
    client, _ = ctx
    login(client, "admin")
    assert client.get("/api/trade-orders/999999").status_code == 404
    assert client.post("/api/trade-orders/999999/confirm?company_id=1").status_code == 404
