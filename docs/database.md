# 数据库

## 约定

所有表都遵守同一套约定，由 `backend/app/db/base.py` 与迁移文件强制：

| 约定 | 值 | 原因 |
|---|---|---|
| 存储引擎 | `InnoDB` | 事务、行锁、外键 |
| 字符集 / 排序 | `utf8mb4` / `utf8mb4_0900_ai_ci` | 完整 Unicode，含 emoji 与中文 |
| 主键 | `BIGINT UNSIGNED` | 单调、够用到天荒地老，且不会混入负数 |
| 时间戳 | `DATETIME(3)`，UTC | 毫秒精度；会话时区钉在 `+00:00` |
| 状态列 | `VARCHAR(32)` 显式字符串枚举 | 可索引、可读、不用 MySQL ENUM 的迁移地狱 |
| 软删除 | `status` + 保留行 | 审计历史永不因删服务器而消失 |

时间戳在应用层统一用 `UTCDateTime` 类型装饰器：写入的 naive datetime 被当作 UTC，
读出的一律 tz-aware UTC。引擎层还有一个 `connect` 事件强制 `SET time_zone = '+00:00'`，
URL 里也带了 `init_command` 做同样的事 —— 三层兜底，因为时间戳错一次就很难查。

## 表清单

### S0 已落地（迁移 `0001`）

| 表 | 用途 |
|---|---|
| `ops_users` | 平台账号。`password_hash` 为 Argon2id；`status` ∈ active/disabled/locked |
| `ops_roles` | 角色 |
| `ops_permissions` | 权限码目录（`code` 唯一） |
| `ops_user_roles` | 用户↔角色，`UNIQUE(user_id, role_id)` |
| `ops_role_permissions` | 角色↔权限，`UNIQUE(role_id, permission_id)` |
| `ops_audit_logs` | 审计。`snapshot` 为脱敏后的 JSON |

### S1 已落地（迁移 `0002`）

| 表 | 用途 |
|---|---|
| `ops_credentials` | 加密凭据。`payload_encrypted` 为 Fernet 密文，**任何 API 响应都不含该列**；`secret_ref` 是模板/任务用的稳定句柄（`cred:<id>`） |
| `ops_servers` | 受管主机。含主机密钥三态（`unverified`/`pending_confirm`/`verified`/`mismatch`）与最后一次检查结果 |
| `ops_server_groups` | 服务器分组 |
| `ops_server_group_members` | 主机↔分组，`UNIQUE(server_id, group_id)`，无代理主键 |
| `ops_server_facts` | 系统事实快照，**只追加**。每次检查插一行，不改旧行 |

`ops_servers.host_key_fingerprint` 是信任锚。地址或端口变更会把该列清空并回到
`unverified` —— 新端点就是新主机，旧 pin 不再适用。

### S2 已落地（迁移 `0003`）

| 表 | 用途 |
|---|---|
| `ops_tasks` | 任务。`idempotency_key` 唯一；`target_snapshot_json` 在提交时刻冻结目标服务器集合 |
| `ops_task_runs` | 任务 × 服务器 = 一个 Run。持有 `lease_owner` / `lease_expires_at` / `heartbeat_at` |
| `ops_task_steps` | 步骤执行记录。唯一 `(run_id, step_key, attempt)`；`result_json` 含取消证据 |
| `ops_task_logs` | 追加式日志。`(run_id, seq)` 是游标；API 出口再脱敏一次 |
| `ops_outbox` | 与任务同事务写入的「已入队」凭据，杜绝「DB 提交了但没人入队」 |
| `ops_api_idempotency` | 请求层幂等回放。`(scope, idempotency_key)` + `request_hash` |
| `ops_lease_recoveries` | 租约过期恢复的判定留痕（探活结果、最终 decision） |

`ops_lease_recoveries` 记录恢复器每一次裁决，因为「为什么这个 Run 变成了 `UNKNOWN`」
事后必须能答得出来，而不是靠猜。

### S3

`ops_templates`、`ops_template_versions`

### S4

`ops_applications`、`ops_app_environments`、`ops_deployments`

## 关键唯一约束

这些约束是并发正确性的地基，不能软化：

| 约束 | 保护什么 |
|---|---|
| `ops_tasks.idempotency_key` UNIQUE | 同一次提交不会产生两个任务 |
| `ops_task_runs` UNIQUE `(task_id, server_id)` | 同任务同机器只有一个 Run；重跑是 `attempt+1`，不是插新行 |
| `ops_task_steps` UNIQUE `(run_id, step_key, attempt)` | 同一步骤同一次尝试只有一条记录 |
| `ops_api_idempotency` UNIQUE `(scope, idempotency_key)` + `request_hash` | 同 key 同 body 回放原响应；同 key 不同 body → 409 |
| `ops_permissions.code` UNIQUE | 权限码是稳定词汇表 |

S2 的三条任务约束各有集成测试直接撞库断言 `IntegrityError`，不是「应该有」的注释。

## 迁移策略

- **Alembic 独立于应用启动。** `alembic upgrade head` 由运维显式执行；lifespan 里绝不跑 DDL。
- **迁移可重复。** `upgrade head` 跑两次，第二次是空操作（不输出 `Running upgrade`）。集成
  测试 `test_migrations.py` 断言这一点。
- **可回退。** 每个迁移都写 `downgrade()`。测试断言 `downgrade base` 后 `ops_%` 表全部消失，
  再 `upgrade head` 能重建。
- **手写 DDL，不用 `create_all()`。** 迁移文件里显式写出 `mysql_engine="InnoDB"`、
  `mysql_charset="utf8mb4"`、`BIGINT UNSIGNED`、`DATETIME(3)`，不依赖 ORM 推断 —— 推断出来的
  结构在不同 SQLAlchemy 版本间会漂移。

### 已踩过的坑

MySQL 的 DDL **非事务化**，而 SQLAlchemy 2.x 的连接是 commit-as-you-go。第一版
`migrations/env.py` 跑完 `run_migrations()` 就结束，结果 `alembic_version` 的插入留在一个
未提交的事务里：表建出来了，版本号没落库，于是 `downgrade base` 变成静默空操作。修复是在
`run_migrations()` 之后显式 `connection.commit()`。这个提交点是刻意的，不要去掉。

## 测试库

集成测试打真 MySQL 的 `opspilot_test` 库，每个用例前清空表。**不用 SQLite 冒充**：
`FOR UPDATE SKIP LOCKED`、utf8mb4 排序规则、`DATETIME(3)`、唯一约束行为在 SQLite 上都不等价。
MySQL 不可达时测试 **skip**，而不是 mock 出绿色。

测试引擎的隔离级别设成 `READ COMMITTED`：默认的 `REPEATABLE READ` 会让测试会话看不到
其它连接刚提交的行（例如「失败登录也要有审计」这条就因此假失败过）。

## 元数据库与业务数据库分离

`ops_*` 表所在的库是**控制面元数据库**，由 OpsPilot 自己管理。被管理的业务 MySQL 是**目标**，
OpsPilot 只通过 `mysql.precheck` 等只读/受限动作与它交互，绝不在自己的元数据库里存业务数据，
也绝不自动初始化或重置业务库的 datadir。
