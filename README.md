# 合作协议义务提醒

本项目维护合作协议义务提醒的领域约定、角色边界、样例数据与**完整服务端实现**，供接口、自动化与管理命令统一使用。

覆盖的核心问题：

- 设备交付等义务逾期后，提醒不能继续发给**离任负责人**；
- 报告、师资义务不能只凭“说交了”就结清——必须按登记的证明要求提交、由**独立核验角色**确认；
- 延期、豁免、责任移交**不能抹去旧记录**；
- 重复调度、定时器重入、服务崩溃恢复**不得产生多条提醒或多个逾期案件**；
- 授权人员可重算指定时间区间，并查看每项义务**为何到期**。

## 领域角色与不变量（来自 `domain/contract.json`）

| 角色 | 职责 |
| --- | --- |
| 协议管理员 `admin` | 登记协议/计划，延期、豁免、责任移交，区间重算 |
| 责任院校 `owner` | 按证明要求提交合格证明（仅当前责任方） |
| 核验人员 `verifier` | 独立裁决合格/不合格；所属方不得是责任方或提交方 |

四个不变量的落地方式：

1. **义务依赖**：计划登记即校验前置义务存在且无环；调度立案时计算阻塞项。
2. **可注入时钟**：`Clock` 协议 + `SystemClock`/`FixedClock`；全部到期判断与 CLI 的 `--as-of` 都走时钟，可回放任意历史区间。
3. **独立核验**：核验必须由核验人员角色作出，且所属方不同于责任方与全部提交方。
4. **幂等提醒**：提醒表对 `(义务, 版本, 类型, 期次, 计划日)` 有唯一约束；写事务使用进程锁 + `BEGIN IMMEDIATE`，案件表对 `(义务, 期次)` 唯一。

## 版本化与不可变历史

- 计划首登时生成 `plan_versions` v1 与每条义务的修订 v1（`register`）。
- 延期 `extend`、豁免 `exempt`、责任移交 `transfer`、一般修订 `amend` 都**追加一条义务修订版本**，旧版本永不更新或删除；任一历史日期可通过生效版本求值重放。
- 逾期案件维护追加式事件流：`opened → deadline_moved → transferred → resolved/reopened …`。
- 所有写操作同时进入只追加的 `audit_log`。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/obligation_service/`：服务端实现
  - `models.py`：角色、义务修订、时间窗、证明要求、前置条件；
  - `services.py`：协议/计划登记与延期、豁免、移交；
  - `verification.py`：证明提交、符合性检查、独立核验裁决；
  - `scheduler.py`：幂等调度、逾期案件、区间重算、到期解释；
  - `store.py`：SQLite 架构与事务（无第三方依赖）；
  - `app.py`：HTTP 服务（标准库）；
  - `cli.py` / `seed_data.py`：管理命令与演示数据。
- `tools/check_contract.py`：命令行契约摘要检查。
- `tests/`：契约回归与服务端行为/并发/HTTP 端到端测试。

## 快速开始

```bash
# 1. 登记演示协议与三项义务（设备交付/师资培训/年度报告）
PYTHONPATH=src python3 -m obligation_service.cli seed --db data/app.db --as-of 2026-09-29

# 2. 执行一次调度（可安全重复；服务恢复后重放同样幂等）
PYTHONPATH=src python3 -m obligation_service.cli dispatch --db data/app.db --as-of 2026-09-29

# 3. 管理员重算区间并查看每项义务为何到期
PYTHONPATH=src python3 -m obligation_service.cli recalculate --db data/app.db \
    --from 2026-08-01 --to 2026-09-29

# 4. 查看逾期案件、提醒、义务版本链、提交/核验留痕
PYTHONPATH=src python3 -m obligation_service.cli cases --db data/app.db
PYTHONPATH=src python3 -m obligation_service.cli reminders --db data/app.db
PYTHONPATH=src python3 -m obligation_service.cli duty --db data/app.db --duty <duty_id>
PYTHONPATH=src python3 -m obligation_service.cli trail --db data/app.db --duty <duty_id>
PYTHONPATH=src python3 -m obligation_service.cli explain --db data/app.db --duty <duty_id>

# 5. HTTP 服务
PYTHONPATH=src python3 -m obligation_service.cli serve --db data/app.db --port 8080
```

## HTTP API 摘要

身份经请求头传递：`X-Actor-Id`、`X-Actor-Name`、`X-Actor-Role`（`admin`/`owner`/`verifier`）、`X-Actor-Party`；POST 可带 `Idempotency-Key` 实现请求级重放。

| 方法与路径 | 角色 | 说明 |
| --- | --- | --- |
| `POST /agreements` | admin | 登记协议 |
| `POST /agreements/{id}/plans` | admin | 登记版本化计划（义务/责任方/前置条件/时间窗/证明要求） |
| `GET /plans/{id}` `/plans/{id}/duties` | 任意 | 计划版本与义务清单 |
| `POST /duties/{id}/extend` `/exempt` `/transfer` `/amend` | admin | 追加修订版本，旧记录保留 |
| `POST /duties/{id}/submissions` | owner（当前责任方） | 提交证明，返回与证明要求的符合性结论 |
| `POST /duties/{id}/confirmations` | verifier（独立方） | `approved`/`rejected` 裁决 |
| `GET /duties/{id}/trail` `/explain` | 任意 | 留痕链；到期原因解释 |
| `POST /scheduler/dispatch` | 任意（可系统调用） | 按当前时钟幂等调度 |
| `POST /scheduler/recalculate` | admin | 重算闭区间并返回逐项解释 |
| `GET /cases` `/cases/{id}` `/reminders` `/runs/{id}` | 任意 | 案件（含事件流）、提醒、运行结果 |

## 验证

```bash
python3 -m unittest discover -s tests -v     # 契约回归 + 服务端行为 + 并发 + HTTP 端到端
python3 -m compileall -q src tools tests
python3 tools/check_contract.py domain/contract.json
```
