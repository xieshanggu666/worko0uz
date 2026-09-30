# 碳排放核算与交易管理系统

面向控排企业的碳管理平台：活动数据采集、排放核算、配额分配、配额交易台账、履约清缴与年度 MRV 报告。

## 技术栈

- **后端**：Python 3.10+ / FastAPI / SQLAlchemy ORM / SQLite / JWT（Cookie 认证）
- **前端**：React 18（本地 UMD 运行时 + htm 模板引擎，无需构建工具，完全离线可用）
- **测试**：pytest（40 项全部通过，含 12 项多线程并发一致性测试）

## 快速开始

```bash
pip install -r requirements.txt
python scripts/init_db.py      # 初始化数据库与演示数据（已有旧库先执行迁移脚本）
uvicorn app.main:app --reload  # 启动服务
```

> 升级旧库（新增幂等键列与唯一约束）：`python scripts/migrate_concurrency.py`，可重复执行。

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
6. **配额交易台账**：买入/卖出/划转，实时校验**可用余额（持仓 - 冻结）**，逐笔记录持仓与冻结快照
7. **履约闭环**：MRV 报告批准后按批准排放快照冻结配额；清缴优先核销冻结配额，不足部分扣减可用配额；缺口年度买入或补充分配后可补缴；报告冲正会解冻并退还已清缴配额、归档旧履约记录
8. **MRV 报告**：年度范围一二三汇总生成，草稿 → 提交 → 批准状态流转；已批准报告不得直接覆盖，须由监管/核查角色通过冲正接口异常回滚

### 并发一致性保障（清缴 / 交易）

- **账户锁定**：进程内按键（账户 / 企业+年度履约）串行化余额变更，多键按序加锁防死锁；PostgreSQL/MySQL 额外加 `SELECT … FOR UPDATE` 行锁，SQLite 设置 `busy_timeout` 等待写锁
- **原子扣减/冻结**：扣减使用“可用余额（current_balance - frozen_balance）充足”的条件单条 UPDATE；冻结/解冻/清缴由数据库保证 `frozen >= 0`、`current >= frozen`，余额、冻结额与流水在同一事务提交，异常统一回滚
- **报告批准与冲正**：批准报告、创建履约记录、冻结配额同事务完成；冲正报告、归档履约、解冻/退还配额、更新配额状态同事务完成
- **重复提交**：流水与履约记录支持幂等键（请求体 `idempotency_key` 或 `Idempotency-Key` 请求头），双击 / 超时重试只入账一次；前端提交期间禁用按钮并自动生成幂等键
- **数据库兜底约束**：`quotas` 的 (企业, 年度) 唯一约束防止并发分配重复；活跃 `compliance_records` 的 (企业, 年度) 部分唯一索引允许冲正归档后重新批准

## 数据表（12 张）

`users` `companies` `emission_scopes` `activity_data` `emission_factors` `factor_versions` `calculation_methods` `emission_results` `quotas` `allowance_accounts` `allowance_transactions` `compliance_records` `mrv_reports`

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
| POST | `/api/accounts/{id}/transfer` | 配额交易 |
| POST | `/api/companies/{id}/clear` | 履约清缴/缺口补缴（admin） |
| GET | `/api/compliance` | 履约记录 |
| POST | `/api/companies/{id}/reports/generate` | 生成 MRV 报告 |
| POST | `/api/reports/{id}/submit` / `/approve` | 提交/批准报告（批准即冻结配额） |
| POST | `/api/reports/{id}/reverse` | 冲正已批准报告并回滚冻结/清缴（verifier/admin，需原因） |

## 测试

```bash
python -m pytest tests/ -v   # 40 passed
```

覆盖：核算引擎两种公式、因子按年取值、核算幂等、配额分配幂等、清缴达标/缺口与补缴、交易余额校验、MRV 状态机、API 冒烟、越权防护，以及多线程并发交易/清缴（无超额扣减、流水快照链一致、幂等键去重、失败整体回滚、清缴与交易并发三方一致）。
