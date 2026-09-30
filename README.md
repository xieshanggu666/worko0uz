# 碳排放核算与交易管理系统

面向控排企业的碳管理平台：活动数据采集、排放核算、配额分配、配额交易台账、履约清缴与年度 MRV 报告。

## 技术栈

- **后端**：Python 3.10+ / FastAPI / SQLAlchemy ORM / SQLite / JWT（Cookie 认证）
- **前端**：React 18（本地 UMD 运行时 + htm 模板引擎，无需构建工具，完全离线可用）
- **测试**：pytest（80 项全部通过，含 18 项多线程并发一致性测试）

## 快速开始

```bash
pip install -r requirements.txt
python scripts/init_db.py      # 初始化数据库与演示数据（已有旧库先执行迁移脚本）
uvicorn app.main:app --reload  # 启动服务
```

> 升级旧库（幂等键列与唯一约束）：`python scripts/migrate_concurrency.py`，可重复执行。
> 新增企业间交易订单（账户交易占用列 + `trade_orders` 表）：`python scripts/migrate_trade_orders.py`，可重复执行。

访问 http://127.0.0.1:8000

### 演示账号（密码均为 `123456`）

| 用户名    | 角色       | 说明                       |
|-----------|------------|----------------------------|
| `admin`   | 监管管理员 | 企业/因子/配额/清缴全权限   |
| `verifier`| 核查员     | 核验活动数据、批准 MRV 报告 |
| `elec`    | 控排企业   | 绿能电力集团（边界受限）    |
| `cement`  | 控排企业   | 恒固水泥股份（边界受限）    |

## 功能模块

1. **核算边界管理**：企业注册行业/地区/核算边界说明，范围一/二/三边界配置
2. **排放因子库**：因子编号、有效期、数据来源；修订自动记录版本历史
3. **活动数据台账**：企业按年度/周期录入活动量，核查员核验标记
4. **排放核算引擎**：
   - `activity_factor`：排放量 = 活动量 × 因子值
   - `fuel_combustion`：排放量 = 燃料量 × 综合系数 × 碳氧化率 × 44/12
   - 因子按年度生效区间取值，重复核算幂等（先清后算）
5. **配额管理**：免费配额分配（基准 + 分配量 + 调整量）、配额账户持仓、可用余额与履约冻结额
6. **配额交易台账**：买入/卖出/划转，实时校验**可用余额（持仓 - 冻结 - 交易占用）**，逐笔记录持仓、冻结与占用快照
7. **企业间交易订单**：买卖双方协议下单，**待双方确认 → 已确认 → 已交割**（或撤销）；卖方确认即形成**交易占用**，撤销即释放，交割时卖方扣减（核销占用）、买方入账，双方配额账户与流水同步
8. **履约闭环**：MRV 报告批准后按批准排放快照冻结配额；清缴优先核销冻结配额，不足部分扣减可用配额；缺口年度买入或补充分配后可补缴；报告冲正会解冻并退还已清缴配额、归档旧履约记录
9. **MRV 报告**：年度范围一二三汇总生成，草稿 → 提交 → 批准状态流转；已批准报告不得直接覆盖，须由监管/核查角色通过冲正接口异常回滚

### 交易占用与履约冻结的冲突处理

账户上的两笔预留相互独立，数据库原子 UPDATE 强制不变量 **`持仓(current) ≥ 履约冻结(frozen) + 交易占用(held)`**：

- **卖方确认占用**时校验“持仓 − 履约冻结 − 已有交易占用”充足：已被履约冻结的配额不能挂单；
- **报告批准冻结 / 清缴**时同样扣除交易占用部分：已挂单的配额不会被履约重复预留或清缴挪用；
- **交割**时卖方“释放占用 + 扣减持仓”在同一条原子 UPDATE 中完成，扣减后持仓仍须覆盖其履约冻结；
- 双方**账户键 + 企业年度清缴键 + 订单键**统一按锁名排序获取，与清缴/普通交易路径共享锁命名空间，杜绝多账户死锁；
- 订单状态、占用、双方余额与四笔流水（占用/释放 + 双方成交）在同一事务提交，任一失败整体回滚。

### 企业间订单状态机

| 状态 | 含义 | 配额影响 |
|------|------|----------|
| `pending_confirmation` | 待双方确认（发起方创建即确认本方） | 若发起方为卖方则已交易占用；买方发起则待卖方确认时占用 |
| `confirmed` | 买卖双方均确认，待交割 | 卖方交易占用中 |
| `delivered` | 已交割 | 卖方释放占用并扣减持仓（`trade_sell`），买方入账（`trade_buy`） |
| `cancelled` | 交割前任一方撤销 | 卖方占用释放（`trade_release`），无持仓变化 |

### 并发一致性保障（清缴 / 交易）

- **账户锁定**：进程内按键（账户 / 企业+年度履约 / 交易订单）串行化余额变更，多键按序加锁防死锁；PostgreSQL/MySQL 额外加 `SELECT … FOR UPDATE` 行锁，SQLite 设置 `busy_timeout` 等待写锁
- **原子扣减/冻结/占用**：扣减使用“可用余额（current - frozen - held）充足”的条件单条 UPDATE；冻结、交易占用、清缴由数据库保证 `frozen ≥ 0`、`held ≥ 0`、`current ≥ frozen + held`，余额、冻结、占用与流水在同一事务提交，异常统一回滚
- **订单状态流转**：创建/确认、撤销、交割均在双方账户键 + 企业年度清缴键 + 订单键保护下进行，锁内强制使 ORM 缓存失效后重读，避免读到取锁前的旧快照
- **报告批准与冲正**：批准报告、创建履约记录、冻结配额同事务完成；冲正报告、归档履约、解冻/退还配额、更新配额状态同事务完成
- **重复提交**：流水、履约记录与交易订单支持幂等键（请求体 `idempotency_key` 或 `Idempotency-Key` 请求头），双击 / 超时重试只入账一次；前端提交期间禁用按钮并自动生成幂等键
- **数据库兜底约束**：`quotas` 的 (企业, 年度) 唯一约束防止并发分配重复；活跃 `compliance_records` 的 (企业, 年度) 部分唯一索引允许冲正归档后重新批准；`trade_orders.idempotency_key` 全局唯一约束防止并发重复下单

## 数据表（14 张）

`users` `companies` `emission_scopes` `activity_data` `emission_factors` `factor_versions` `calculation_methods` `emission_results` `quotas` `allowance_accounts` `allowance_transactions` `compliance_records` `mrv_reports` `trade_orders`

## API 摘要

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/auth/login` | 登录（Cookie 会话） |
| GET | `/api/dashboard/stats` | 平台统计 |
| GET/POST | `/api/companies` | 企业列表/创建（admin） |
| POST | `/api/companies/{id}/scopes` | 添加核算边界（admin） |
| GET | `/api/companies/{id}/totals?year=` | 年度范围汇总 |
| GET/POST | `/api/activity` | 活动数据列表/录入 |
| POST | `/api/activity/{id}/verify` | 核验（verifier/admin） |
| GET/POST | `/api/factors` | 因子列表/创建（admin） |
| PUT | `/api/factors/{id}` | 修订因子并记版本（admin） |
| POST | `/api/companies/{id}/calculate?year=` | 触发核算 |
| GET | `/api/companies/{id}/results?year=` | 核算明细 |
| GET/POST | `/api/quotas` | 配额列表/分配（admin） |
| GET | `/api/companies/{id}/account?year=` | 配额账户（含履约冻结/交易占用/可用余额） |
| POST | `/api/accounts/{id}/transfer` | 配额交易 |
| GET | `/api/accounts/{id}/transactions` | 账户流水（含占用快照与订单关联） |
| POST | `/api/companies/{id}/clear` | 履约清缴/缺口补缴（admin） |
| GET | `/api/compliance` | 履约记录 |
| GET/POST | `/api/trade-orders` | 企业间订单列表/下单（买卖双方；admin 需 `creator_company_id`） |
| GET | `/api/trade-orders/{id}` | 订单详情（仅买卖双方/监管可见） |
| POST | `/api/trade-orders/{id}/confirm?company_id=` | 买方/卖方确认（卖方确认即占用；监管用 company_id 代确认） |
| POST | `/api/trade-orders/{id}/cancel?company_id=` | 撤销订单并释放占用（需原因，可空） |
| POST | `/api/trade-orders/{id}/deliver` | 双方确认后交割，同步双方账户与流水 |
| POST | `/api/companies/{id}/reports/generate` | 生成 MRV 报告 |
| POST | `/api/reports/{id}/submit` / `/approve` | 提交/批准报告（批准即冻结配额） |
| POST | `/api/reports/{id}/reverse` | 冲正已批准报告并回滚冻结/清缴（verifier/admin，需原因） |

## 测试

```bash
python -m pytest tests/ -v   # 80 passed
```

覆盖：核算引擎两种公式、因子按年取值、核算幂等、配额分配幂等、清缴达标/缺口与补缴、交易余额校验、MRV 状态机、API 冒烟、越权防护；企业间订单的双方确认/撤销/交割全流程、下单幂等、第三方越权；以及多线程并发交易/清缴/确认/交割（无超额扣减、持仓+冻结+占用三列流水快照链一致、幂等键去重、失败整体回滚、**交易占用与履约冻结并发互不侵占**）。
