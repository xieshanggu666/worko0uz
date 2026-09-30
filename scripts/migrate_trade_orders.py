"""为已存在的数据库补齐企业间交易订单所需的表、列与约束。

用法：python scripts/migrate_trade_orders.py（可重复执行）

- allowance_accounts：trade_held_balance（交易占用，独立于履约冻结）
- allowance_transactions：trade_held_after、trade_order_id
- trade_orders：新建企业间交易订单表（双方确认/撤销/交割状态机）
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text  # noqa: E402

# 导入模型即完成 metadata 注册；engine 在模型导入后取用
from app.core.database import Base, engine  # noqa: E402
from app.models import TradeOrder  # noqa: E402,F401


def _has_column(inspector, table: str, column: str) -> bool:
    return any(c["name"] == column for c in inspector.get_columns(table))


def _has_index(inspector, name: str) -> bool:
    return any(
        ix["name"] == name
        for table in inspector.get_table_names()
        for ix in inspector.get_indexes(table)
    )


def _has_unique_constraint(inspector, table: str, name: str) -> bool:
    return any(
        uq["name"] == name for uq in inspector.get_unique_constraints(table)
    )


def _add_column(statements, inspector, table: str, column: str, ddl: str) -> None:
    if table in inspector.get_table_names() and not _has_column(inspector, table, column):
        statements.append(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def main():
    inspector = inspect(engine)
    statements: list[str] = []
    tables = set(inspector.get_table_names())

    if "allowance_accounts" in tables:
        _add_column(
            statements,
            inspector,
            "allowance_accounts",
            "trade_held_balance",
            "trade_held_balance NUMERIC(18, 4) NOT NULL DEFAULT 0",
        )

    if "allowance_transactions" in tables:
        _add_column(
            statements,
            inspector,
            "allowance_transactions",
            "trade_held_after",
            "trade_held_after NUMERIC(18, 4) NOT NULL DEFAULT 0",
        )
        _add_column(
            statements,
            inspector,
            "allowance_transactions",
            "trade_order_id",
            "trade_order_id INTEGER",
        )

    # 先执行 ALTER，再创建新表（新表外键引用已存在的 companies/allowance_accounts）
    if statements:
        with engine.begin() as conn:
            for stmt in statements:
                print(f"执行：{stmt}")
                conn.execute(text(stmt))

    if "trade_orders" not in tables:
        TradeOrder.__table__.create(engine)
        print("执行：CREATE TABLE trade_orders")
        print("迁移完成：已新增企业间交易订单表及账户/流水占用列")
    elif statements:
        print(f"迁移完成：{len(statements)} 项列变更（trade_orders 表已存在）")
    else:
        print("无需迁移：企业间交易订单相关表与列均已存在")

    # 兜底确认订单幂等唯一约束存在（create_all 在新库建为表级 UNIQUE 约束，
    # 旧库手工补同名唯一索引；SQLite 中 NULL 不参与唯一，无幂等键的请求不受影响）。
    inspector = inspect(engine)
    if "trade_orders" in inspector.get_table_names() and not _has_unique_constraint(
        inspector, "trade_orders", "uq_trade_order_idem"
    ) and not _has_index(inspector, "uq_trade_order_idem"):
        with engine.begin() as conn:
            conn.execute(
                text("CREATE UNIQUE INDEX uq_trade_order_idem ON trade_orders (idempotency_key)")
            )
        print("执行：CREATE UNIQUE INDEX uq_trade_order_idem")


if __name__ == "__main__":
    main()
