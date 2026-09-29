# 合作协议义务提醒（完整服务端）

为协议管理员提供的合作协议义务履约提醒后端：把**义务、责任方、前置条件、时间窗、证明要求、核验角色**登记为
**版本化计划**，用**可注入时钟**生成提醒与逾期案件；完成必须由**独立角色确认**；
延期、豁免、责任移交**只追加事件、不抹去旧记录**；重复调度与服务恢复**幂等不重复**；
授权人员可用管理命令**重算指定区间**并查看每项义务**为何到期**。

## 领域模型与不变量

契约见 `domain/contract.json`（协议管理员 / 责任院校 / 核验人员；登记 → 调度 → 提醒 → 核验 → 结清）。
对应实现：

| 契约不变量 | 实现位置 |
| --- | --- |
| 义务依赖（前置条件） | `src/obligation/models.py` `Prerequisite`；`scheduler.py` 前置门控 |
| 可注入时钟 | `src/obligation/clock.py`（`SystemClock` / `FixedClock` / `SettableClock`） |
| 独立核验 | `verification.py`：须为 verifier、持登记核验角色、非责任方、非提交人 |
| 幂等提醒 | `storage.py` 唯一约束 + `scheduler.py` `INSERT ... ON CONFLICT DO NOTHING` + outbox |

### 关键设计

- **版本化计划**：`plan_versions` / `obligation_versions` 只追加，每次登记产生新版本号；
  可随时按版本号回放当时的责任方、时间窗、证明要求。
- **代次 generation**：义务的调度代次，初始为 1；延期或移交时 +1。旧代次的提醒
  标记 `superseded_at` 但**不删除**——能证明"逾期时的提醒曾发给离任负责人"。
- **幂等三层**：
  1. `reminders` 活跃档位部分唯一索引（同代次同档位仅一条）+ 含 `due_date` 的表级唯一键；
  2. `deliveries` 对同一提醒至多一条有效投递，且带确定性 `idempotency_key`；
  3. 日历网关按键去重（`InMemoryCalendarGateway` 模拟；生产替换实现即可）。
  故重复执行 `recalc`、服务崩溃后 `dispatch` 重放都不会产生多条提醒/日历邀请。
- **投递时机**：计划行可预生成，但只有 `fire_date <= 今天` 才进入投递队列（不会提前数周发出）。
- **证明与结清**：提交≠合格；合格证明数达到登记的 `minimum_count` 才置 `completed`，
  结清不可逆，此后不再产生提醒/逾期。
- **审计事件流**：`events` 追加记录登记、移交（含新旧负责人/联系方式）、延期（含旧→新截止日）、
  豁免、提交、核验通过/驳回、提醒投递、重算。

## 目录

- `domain/contract.json`：领域角色、状态、约束与样例。
- `src/domain_contract/`：契约读取与校验（既有）。
- `src/obligation/`：服务端实现
  - `models.py` 值对象（`ObligationSpec/TimeWindow/ProofRequirement/Prerequisite/ResponsibleParty`）
  - `clock.py` 可注入时钟
  - `storage.py` SQLite schema 与事务
  - `repository.py` 仓储
  - `planner.py` 版本化登记、移交、延期、豁免
  - `scheduler.py` 到期推导、幂等调度、逾期案件、解释引擎、区间重算
  - `verification.py` 证明提交与独立核验
  - `calendar.py` 日历投递网关（含失败恢复演练网关）
  - `server.py` 零依赖 HTTP 服务（标准库）
- `tools/admin.py`：协议管理员命令行与演示场景播种。
- `tests/`：契约回归 + 服务端单测（固定时钟）+ HTTP 端到端测试。

## 快速演练

```bash
# 1) 播种场景（今天固定为 2026-09-29；设备交付截止 2026-08-29，逾期约一个月）
python3 tools/admin.py seed

# 2) 看逾期案件（新提醒只发新任负责人吴欣；旧提醒记录仍指向离任的老周）
python3 tools/admin.py overdue A-09303-012

# 3) 看"为何到期"的完整推导链（截止日、延期、前置、证明、核验角色、档位）
python3 tools/admin.py explain A-09303-012 --code EQ-01

# 4) 管理员重算指定区间（可重复执行，created 第二次必为 0）
python3 tools/admin.py recalc A-09303-012 --start 2026-09-01 --end 2026-10-15

# 5) 独立核验：责任方成员/错角色会被拒；独立学术核验人通过后前置解除
python3 tools/admin.py verify A-09303-012 RPT-01 1 --user u-review --conforming

# 6) 查不可变历史（移交新旧负责人、延期旧→新截止日、投递幂等键都在）
python3 tools/admin.py history A-09303-012 --code EQ-01
python3 tools/admin.py reminders A-09303-012 --code EQ-01 --all

# 7) 服务恢复后安全重放
python3 tools/admin.py dispatch

# 启动 HTTP 服务
python3 tools/admin.py serve --port 8080
```

### HTTP 摘要

`Authorization: Bearer <token>`（播种输出中给出各角色令牌）。

| 方法 | 路径 | 角色 |
| --- | --- | --- |
| POST | `/agreements`、`/agreements/{id}/plans` | admin |
| POST | `.../obligations/{code}/transfer|extension|waiver` | admin |
| POST | `/admin/recalc/{id}`（body：`range_start/range_end/dispatch`） | admin |
| POST | `/admin/dispatch` | admin |
| GET | `/agreements/{id}/explain[?code=]`、`/overdue`、`/events` | 已认证 |
| POST | `.../submissions` | 责任院校 |
| POST | `.../submissions/{sid}/verify` | 持登记角色的独立核验人 |

## 验证

```bash
python3 -m compileall -q src tools tests
python3 -m unittest discover -s tests -v   # 28 项：契约 + 领域 + HTTP
python3 tools/check_contract.py domain/contract.json
```

## 生产化提示

默认 SQLite + 内存日历网关用于自包含交付。接入生产时：
- 替换 `CalendarGateway` 为企业日历/邮件实现（保持 `idempotency_key` 语义）；
- 把 `auth_tokens` 的演示鉴权换成统一身份中间件；
- 定时以 `SystemClock` 调用 `recalculate(今天, 今天)` + `dispatch_pending()` 即可，
  由于全链路幂等，多实例/重复触发安全。
