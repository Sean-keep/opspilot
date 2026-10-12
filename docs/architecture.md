# 架构

## 进程模型

OpsPilot 由**两个独立进程**加一个静态前端组成。它们共享一个 MySQL 元数据库，但不共享
进程、不共享内存、不互相 import 对方的应用代码。

```
        ┌──────────────┐   HTTP /api/v1    ┌──────────────────┐
Browser │  Vue 3 SPA   │ ────────────────► │  opspilot-api    │
        │  (static)    │ ◄──────────────── │  FastAPI + SSE   │
        └──────────────┘                   └────────┬─────────┘
                                                    │ SQLAlchemy
                                                    ▼
                                           ┌──────────────────┐
                                           │  MySQL 元数据库   │
                                           │  (control plane) │
                                           └────────┬─────────┘
                                                    │ SQLAlchemy
        ┌──────────────┐  SSH (argv)       ┌────────▼─────────┐
目标机  │  opstest@…   │ ◄──────────────── │  opspilot-worker │
        │  Ubuntu/Deb  │                   │  租约 + 执行器    │
        └──────────────┘                   └──────────────────┘
```

**为什么是两个进程而不是一个：** 远程操作动辄几十秒到几十分钟。如果 HTTP 请求要等它，
连接池、反代超时、重试策略全都会打架。API 只负责写入任务意图并立刻返回 202，Worker
异步认领执行，进度通过 SSE / 轮询回流。

**为什么不是微服务：** V1 的规模不需要服务发现、消息队列、分布式事务。两个进程共享一个
库，靠数据库的行锁与唯一约束做协调，部署面仍然只有 systemd 两个单元。

## 依赖方向

```
shared  ◄────  backend
  ▲             ▲
  │             │
  └── worker ───┘      (worker imports shared; worker MUST NOT import backend.app)
```

`shared/` 是普通 Python 包（`shared.redaction`、`shared.schemas`、`shared.task_protocol`、
`shared.actions`），backend 与 worker 都依赖它。**worker 绝不 import backend**，否则两个进程
就无法独立部署、独立升级，也会在 `backend.app.executor` 与 worker 之间产生循环 import。

执行器放在 `backend/app/executor/`，通过一个 `ExecPort` 协议与具体传输解耦：

- worker 侧提供 `SshExecPort`（Paramiko）
- backend 侧提供 `DryRunExecPort`（只校验、不连接，供模板校验与 dry-run）

于是 Action Registry 可以同时被 API（校验模板、计算预检）和 Worker（真正执行）复用，
而不需要把 SSH 栈拖进 API。

## backend/app 模块划分

| 模块 | 职责 | 阶段 |
|---|---|---|
| `core/` | settings、结构化日志、密码/JWT、统一错误 | S0 |
| `db/` | DeclarativeBase、引擎/会话、模型聚合 | S0 |
| `auth/` | 用户、角色、权限、RBAC 依赖 | S0 |
| `audit/` | 审计写入（独立事务）与查询 | S0 |
| `api/` | 路由聚合、健康检查 | S0 |
| `inventory/` | 服务器、分组、系统事实 | S1 |
| `credentials/` | 凭据引用与加密载荷 | S1 |
| `orchestrator/` | 任务 / Run / Step / 日志、状态机 | S2 |
| `executor/` | SSH 通道、Action Registry、参数渲染 | S2/S3 |
| `catalog/` | 模板目录、版本、校验 | S3 |
| `applications/` | 应用与环境定义 | S4 |
| `deployments/` | 发布记录、软链接切换、回滚任务 | S4 |
| `artifacts/` | 制品下载与校验和 | S4 |
| `notifications/` | Telegram、Outbox | S2+ |

模块之间只通过服务层（`service.py`）和 ORM 模型交互，路由层不直接拼 SQL。

## HTTP 边界

- 所有业务接口前缀 `/api/v1`。
- 健康检查 `/healthz`、`/readyz` 无前缀、无鉴权，只暴露 liveness。
- 统一错误体 `{code, message, request_id[, details]}`。堆栈、密钥、命令参数永不进响应。
- 创建任务的接口接受 `Idempotency-Key` 头；同 key 同 body 返回原对象，同 key 不同 body → 409。
- 长任务一律异步：接口返回任务 id，进度走 `GET /api/v1/tasks/{id}/events`（SSE）与
  分页日志 `GET /api/v1/task-runs/{id}/logs`。

## 配置

全部外部配置来自环境变量或权限受限文件，前缀 `OPSPILOT_`，见 `.env.example`。

两条约定：

1. `*_FILE` 变体让密钥从进程可读、但不与被它保护的数据同处的文件注入。生产环境的主加密
   密钥**必须**这样给（`OPSPILOT_MASTER_KEY_FILE`）。进程拒绝读取 mode 含 group/world 位的
   密钥文件。
2. 没有任何解析后的密钥会被日志打印。启动时只输出 `Settings.safe_summary()`，它把每个秘密
   折叠成「有没有」的布尔标记。

## 部署

systemd 两个单元：`opspilot-api`、`opspilot-worker`。都是 `Type=exec`、非 root、
`ProtectSystem=strict`、`NoNewPrivileges`、`MemoryDenyWriteExecute`。

**应用启动不跑 DDL。** 迁移是 `alembic upgrade head`，由运维手动或由部署脚本显式执行。
这是刻意的：启动时自动改表结构在回滚和多实例场景下会变成事故源。
