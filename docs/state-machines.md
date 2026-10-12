# 状态机

任务引擎是这个项目最容易翻车的部分。以下约定在 S2 落地前先固定，迁移表放在
`shared/task_protocol/`，backend 与 worker 共用同一份，任何非法迁移一律抛
`InvalidStateTransition`。

## 任务（`ops_tasks.status`）

```
PENDING ──► QUEUED ──► RUNNING ──┬──► SUCCESS
                                 ├──► PARTIAL_SUCCESS
                                 ├──► FAILED
                                 └──► CANCELLING ──► CANCELLED
```

| 状态 | 含义 |
|---|---|
| `PENDING` | 已写入，尚未入队（同一事务里也会写 `ops_outbox`） |
| `QUEUED` | 已入队，等待 Worker 认领 |
| `RUNNING` | 至少一个 Run 在执行 |
| `SUCCESS` | 所有目标 Run 成功 |
| `PARTIAL_SUCCESS` | 有成功也有失败/取消的终态 |
| `FAILED` | 无成功，且有失败 |
| `CANCELLING` | 用户已请求取消，正在停 |
| `CANCELLED` | 取消完成 |

**聚合规则：**

```
全部 SUCCESS              → SUCCESS
有 SUCCESS 也有其它终态    → PARTIAL_SUCCESS
无 SUCCESS 且有 FAILED     → FAILED
用户取消主导               → CANCELLED
```

## Run（`ops_task_runs.status`）

一个 Run 是「一个任务在一台服务器上的一次执行」。

```
WAITING ──► PRECHECK ──► EXECUTING ──► VERIFYING ──┬──► SUCCESS
                                                  └──► FAILED
任意执行态 ──► CANCELLING ──► CANCELLED
任意执行态 ──► UNKNOWN   （租约过期且无法判定远端结果）
```

| 状态 | 含义 |
|---|---|
| `WAITING` | 已创建，等待认领 |
| `PRECHECK` | 已认领，正在做只读预检（磁盘、OS、依赖） |
| `EXECUTING` | 正在跑步骤 |
| `VERIFYING` | 步骤跑完，正在做健康检查 |
| `SUCCESS` / `FAILED` | 终态 |
| `CANCELLING` / `CANCELLED` | 取消中 / 已取消 |
| `UNKNOWN` | **远端状态无法判定** —— 见下 |

### `UNKNOWN` 不是失败

租约过期 ≠ 远端失败。恢复器对超时 Run 走三步：

1. **探活远端**：`systemd.is_active`、目标 PID 文件、端口探测（只读动作）。
2. **远端仍在跑** → 续租，保持 `EXECUTING`，继续跟日志。
3. **远端已结束但无证据成功** →
   - 步骤是**幂等**的（`package.install`、`file.render`、`systemd.restart`）→ 允许重跑
   - 步骤是**非幂等**的（`mysql.precheck` 之后的初始化、数据迁移）→ 标 **`UNKNOWN`**，挂人工核查
4. **无法判断远端状态**（SSH 连不上）→ **`UNKNOWN`**。

`UNKNOWN` 是一个**需要人工裁决的分支**，不是可自动重试的失败。它只能由人
`POST /task-runs/{id}/resolve` 明确「确认失败→重试」或「确认成功→改判」。
自动重跑一个非幂等的数据库初始化步骤，会把目标机搞坏 —— 这条规则没有例外。

### 幂等分级

每个 Action 声明自己的 `idempotency_class`：

| 类别 | 租约过期后的处理 |
|---|---|
| `SAFE` | 直接重跑（纯读或天然幂等） |
| `CONDITIONAL` | 先跑预检；预检说已在目标态就跳过，否则 `UNKNOWN` |
| `DESTRUCTIVE` | 一律 `UNKNOWN`，绝不自动重跑 |

## Step（`ops_task_steps`）

唯一键 `(run_id, step_key, attempt)`。`attempt` 只在真正重跑时 +1 —— **不靠重复插行**表达重试，
因为 `ops_task_runs` 上有 `UNIQUE(task_id, server_id)`，Run 本身就是单行的。

步骤记录 `result_json`、`started_at`、`finished_at`、`exit_code`、`duration_ms`。

## 取消 ≠ 回滚

`POST /tasks/{id}/cancel` → 任务 `RUNNING → CANCELLING`。Worker 在步骤边界检查取消标记，对
正在跑的 SSH 通道发停止信号，然后：

- 在 `result_json` 里记录 `cancel_requested_at` / `remote_signal_sent` / `remote_exit_after_signal`
- **把远端退出码或进程消失作为证据落日志**，再置 `CANCELLED`
- 只改 DB 状态、不碰远端 = **不合规**

**取消只停止后续步骤，不撤销已完成的步骤。** 回滚是独立的 rollback 任务，有自己的审计与
确认。把两者混为一谈会让「取消」变成一个悄无声息的半成品状态。

## 认领与租约

认领必须是**单条 SQL、单个事务**：

```sql
SELECT id FROM ops_task_runs
 WHERE status = 'WAITING'
   AND (lease_expires_at IS NULL OR lease_expires_at < UTC_TIMESTAMP(3))
 ORDER BY id
 LIMIT :n
 FOR UPDATE SKIP LOCKED;

UPDATE ops_task_runs
   SET status = 'PRECHECK',
       lease_owner = :me,
       lease_expires_at = UTC_TIMESTAMP(3) + INTERVAL :lease SECOND,
       heartbeat_at = UTC_TIMESTAMP(3)
 WHERE id IN (...);
-- 同一事务提交
```

`SELECT` 之后再 `UPDATE` 会让两个 Worker 同时拿到同一个 Run。`SKIP LOCKED` 保证并发下
互不阻塞也不重复。两并发 Worker 认领测试断言「只有一方拿到」。

心跳：Worker 每 `lease/3` 秒刷 `heartbeat_at` 并续租。`heartbeat_seconds` 必须严格小于
`lease_seconds`（`Settings` 里有校验器强制），否则一个短暂 GC 停顿就会被误判为掉线。

## 创建 + 入队的原子性

```
BEGIN
  INSERT ops_tasks (status='PENDING', idempotency_key=?)
  INSERT ops_task_runs × N (status='WAITING')      -- N 台目标机，冻结 target_snapshot
  INSERT ops_outbox (topic='task.created', ...)
COMMIT
```

绝不允许「DB 提交了但没人入队」。`target_snapshot_json` 在提交时刻冻结服务器组成员，
之后组变了也不影响这个任务的目标集合。
