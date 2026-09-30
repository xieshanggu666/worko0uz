"""为已存在的数据库补齐并发、幂等及报告履约闭环所需的列与约束。

用法：python scripts/migrate_concurrency.py（可重复执行）

- allowance_transactions：idempotency_key、frozen_after
- compliance_records：idempotency_key、frozen_amount、report_id、is_active
- mrv_reports：reversed_by/reversed_at/reversal_reason
- 活跃履约记录保持 (company_id, year) 唯一；冲正归档记录可重新批准
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text  # noqa: E402

from app.core.database import engine  # noqa: E402


def _has_column(inspector, table: str, column: str) -> bool:
    return any(c["name"] == column for c in inspector.get_columns(table))


def _has_index(inspector, name: str) -> bool:
    return any(
        ix["name"] == name
        for table in inspector.get_table_names()
        for ix in inspector.get_indexes(table)
    )


def _add_column(statements, inspector, table: str, column: str, ddl: str) -> None:
    if table in inspector.get_table_names() and not _has_column(inspector, table, column):
        statements.append(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def main():
    inspector = inspect(engine)
    statements: list[str] = []
    tables = set(inspector.get_table_names())

    if "allowance_transactions" in tables:
        _add_column(
            statements,
            inspector,
            "allowance_transactions",
            "idempotency_key",
            "idempotency_key VARCHAR(64)",
        )
        _add_column(
            statements,
            inspector,
            "allowance_transactions",
            "frozen_after",
            "frozen_after NUMERIC(18, 4) NOT NULL DEFAULT 0",
        )
        if not _has_index(inspector, "uq_tx_account_idem"):
            # SQLite 中 NULL 不参与唯一索引，未携带幂等键的历史/新请求不受影响
            statements.append(
                "CREATE UNIQUE INDEX uq_tx_account_idem "
                "ON allowance_transactions (account_id, idempotency_key)"
            )

    if "compliance_records" in tables:
        _add_column(
            statements, inspector, "compliance_records", "idempotency_key",
            "idempotency_key VARCHAR(64)",
        )
        _add_column(
            statements, inspector, "compliance_records", "frozen_amount",
            "frozen_amount NUMERIC(18, 4) NOT NULL DEFAULT 0",
        )
        _add_column(
            statements, inspector, "compliance_records", "report_id",
            "report_id INTEGER",
        )
        _add_column(
            statements, inspector, "compliance_records", "is_active",
            "is_active INTEGER NOT NULL DEFAULT 1",
        )
        # 旧版本的全量唯一约束会阻止“冲正后重新批准”保留历史记录；
        # 新模型改为仅活跃记录唯一。
        if _has_index(inspector, "uq_compliance_company_year"):
            statements.append("DROP INDEX uq_compliance_company_year")

        if not _has_index(inspector, "uq_compliance_active_company_year"):
            if engine.dialect.name in {"sqlite", "postgresql"}:
                statements.append(
                    "CREATE UNIQUE INDEX uq_compliance_active_company_year "
                    "ON compliance_records (company_id, year) WHERE is_active = 1"
                )
            else:
                # MySQL 旧版本不支持部分索引；由企业年度键锁保证活跃记录唯一。
                statements.append(
                    "CREATE INDEX ix_compliance_active_company_year "
                    "ON compliance_records (company_id, year, is_active)"
                )

    if "quotas" in tables and not _has_index(inspector, "uq_quota_company_year"):
        statements.append(
            "CREATE UNIQUE INDEX uq_quota_company_year ON quotas (company_id, year)"
        )

    if "mrv_reports" in tables:
        _add_column(statements, inspector, "mrv_reports", "reversed_by", "reversed_by INTEGER")
        _add_column(statements, inspector, "mrv_reports", "reversed_at", "reversed_at TIMESTAMP")
        _add_column(
            statements,
            inspector,
            "mrv_reports",
            "reversal_reason",
            "reversal_reason TEXT NOT NULL DEFAULT ''",
        )

    if not statements:
        print("无需迁移：所有列与约束均已存在")
        return

    with engine.begin() as conn:
        for stmt in statements:
            print(f"执行：{stmt}")
            conn.execute(text(stmt))
    print(f"迁移完成：{len(statements)} 项变更")


if __name__ == "__main__":
    main()
