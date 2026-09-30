"""配额账本并发控制原语：账户锁定、原子余额更新与事务回滚。

设计目标（并发清缴 / 交易时保证余额与流水一致）：
1. 账户锁定：同一账户（或同一企业清缴键）的余额变更在进程内串行化，
   消除“读余额 → 判断 → 写余额”的竞态；跨账户操作按锁名排序获取，避免死锁；
   对支持行锁的数据库额外使用 SELECT ... FOR UPDATE。
2. 原子更新：余额扣减使用带 ``current_balance >= amount`` 条件的单条 UPDATE，
   数据库自身保证不会超额扣减；影响行数为 0 即判定为余额不足并整体回滚。
3. 事务边界：所有余额变更在单一事务中完成，异常统一 rollback，
   绝不出现“余额已扣、流水缺失”或“流水已写、余额未变”的半成品状态。

SQLite 不支持 SELECT ... FOR UPDATE，且默认写锁为库级锁，
因此进程内键锁是其主要的并发防线，原子条件 UPDATE 作为兜底；
切换到 PostgreSQL/MySQL 时键锁与行锁会同时生效。
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator, Sequence

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.allowance import AllowanceAccount


class InsufficientBalanceError(ValueError):
    """可用配额不足，扣减被拒绝（事务已回滚，余额与流水均不变）。"""


class DuplicateSubmitError(Exception):
    """幂等键冲突：同一业务请求已成功处理过（调用方应返回首次结果）。"""


_locks_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}


def account_lock_key(account_id: int) -> str:
    return f"account:{account_id}"


def company_clear_key(company_id: int, year: int) -> str:
    """清缴键：企业 + 年度（覆盖该企业当年可能存在的全部账户变更）。"""
    return f"clear:{company_id}:{year}"


def trade_order_key(order_id: int) -> str:
    """企业间订单键：同一张订单的确认/撤销/交割串行化。"""
    return f"trade-order:{order_id}"


def _get_lock(key: str) -> threading.RLock:
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _locks[key] = lock
        return lock


@contextmanager
def locked_accounts(keys: Sequence[str]) -> Iterator[None]:
    """按锁名排序后依次获取键锁，避免多账户操作时交叉等待形成死锁。

    使用可重入锁，清缴（先取企业键再取账户键）与嵌套调用安全。
    """
    acquired: list[threading.RLock] = []
    for key in sorted(set(keys)):
        lock = _get_lock(key)
        lock.acquire()
        acquired.append(lock)
    try:
        yield
    finally:
        for lock in reversed(acquired):
            lock.release()


@contextmanager
def transactional(db: Session) -> Iterator[None]:
    """统一事务边界：正常退出提交，任何异常回滚后原样抛出。

    业务层（分配/交易/清缴）统一通过此上下文提交，不再各自 commit，
    保证“余额 + 流水 + 业务记录”要么全部落库、要么全部撤销。
    """
    try:
        yield
        db.commit()
    except Exception:
        db.rollback()
        raise


def lock_rows_for_update(db: Session, account_id: int) -> None:
    """对支持行锁的数据库加 SELECT ... FOR UPDATE，并使本地缓存失效。

    加锁后使会话中该账户的属性过期，后续读取拿到的是锁内最新数据
    （避免 PostgreSQL 等数据库下 FOR UPDATE 与 ORM 快照不一致）。
    SQLite 不支持 FOR UPDATE，下为 no-op。
    """
    if db.bind is not None and db.bind.dialect.name != "sqlite":
        db.query(AllowanceAccount.id).filter(AllowanceAccount.id == account_id).with_for_update().first()
        db.expire_all()


def apply_ledger_delta(
    db: Session,
    account_id: int,
    current_delta: float = 0,
    frozen_delta: float = 0,
    held_delta: float = 0,
) -> tuple[float, float, float]:
    """原子更新账户当前余额、履约冻结额与交易占用额。

    返回更新后的 ``(当前余额, 履约冻结额, 交易占用额)``。

    数据库条件同时保证：
    - 当前余额非负、履约冻结额非负、交易占用额非负；
    - 履约冻结 + 交易占用不超过当前余额。

    因此交易/占用校验的是“可用余额 current - frozen - held”，报告批准后的
    履约冻结与卖方订单的交易占用互不侵占：已被任一方预留的配额不能再被
    另一方占用，从数据库层面杜绝“同一批配额既被交易挂单又被履约冻结”。
    """
    current_amount = round(current_delta, 4)
    frozen_amount = round(frozen_delta, 4)
    held_amount = round(held_delta, 4)
    current_expr = AllowanceAccount.current_balance + current_amount
    frozen_expr = AllowanceAccount.frozen_balance + frozen_amount
    held_expr = AllowanceAccount.trade_held_balance + held_amount

    stmt = (
        update(AllowanceAccount)
        .where(AllowanceAccount.id == account_id)
        .where(current_expr >= 0)
        .where(frozen_expr >= 0)
        .where(held_expr >= 0)
        .where(current_expr >= frozen_expr + held_expr)
        .values(
            current_balance=current_expr,
            frozen_balance=frozen_expr,
            trade_held_balance=held_expr,
        )
    )
    result = db.execute(stmt.execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise InsufficientBalanceError("可用配额余额不足")
    # UPDATE 绕开 ORM 状态同步，必须使身份映射中的旧对象失效后再读
    db.expire_all()
    db.flush()
    refreshed = db.get(AllowanceAccount, account_id)
    return (
        round(float(refreshed.current_balance), 4),
        round(float(refreshed.frozen_balance), 4),
        round(float(refreshed.trade_held_balance), 4),
    )


def apply_balance_delta(
    db: Session,
    account_id: int,
    delta: float,
) -> float:
    """对账户当前余额执行单条原子条件 UPDATE，返回更新后的余额。"""
    balance, _, _ = apply_ledger_delta(db, account_id, current_delta=delta)
    return balance


def is_duplicate_submit(exc: IntegrityError, column: str = "idempotency_key") -> bool:
    """判断 IntegrityError 是否为幂等键唯一约束冲突（兼容各数据库报错文案）。"""
    message = str(exc.orig) if getattr(exc, "orig", None) is not None else str(exc)
    return column in message or "UNIQUE constraint failed" in message or "duplicate" in message.lower()
