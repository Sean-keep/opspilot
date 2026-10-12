# 状态机

任务引擎是这个项目最容易翻车的部分。以下约定在 S2 落地前先固定，迁移表放在
`shared/task_protocol/`，backend 与 worker 共用同一份，任何非法迁移一律抛
`InvalidStateTransition`。

## 任务（`ops_tasks.status`）

```
PENDING ──► QUEUED ──► RUNNING ──┬──► SUCCESS
                                 ├──► PARTIAL_SUCCESS
                                 ├──► FAILED
                                 └──► CANCELLING ──┬──► CANCELLED
                                                   ├──► SUCCESS
                                                   ├──► PARTIAL_SUCCESS
                                                   └──► FAILED
```

| 状态 | 含义 |
|---|---|
| `PENDING` | 已写入，尚未入队（同一事务里也会写 `ops_outbox`） |
| `QUEUED` | 已入队，等待 Worker 认领 |
| `RUNNING` | 至少一个 Run 在执行 |
| `SUCCESS` | 所有目标 Run 成功。**硬终态，永不再动** |
| `PARTIAL_SUCCESS` | 有成功也有失败/取消的终态 |
| `FAILED` | 无成功，且有失败 |
| `CANCELLING` | 用户已请求取消，正在停 |
| `CANCELLED` | 取消完成 |

`PENDING` / `QUEUED` 也能直接进 `FAILED`（例如目标服务器在入队后被删掉，一个 Run 都跑不了）。

**取消是协作式的。** `CANCELLING` 允许继续走到 `SUCCESS` / `PARTIAL_SUCCESS` / `FAILED`：
停的信号发出去了，但已经在跑的步骤可能先一步跑完，任务就该按真实结果收尾，而不是被
`CANCELLED` 盖掉。

**重试边（`PARTIAL_SUCCESS` / `FAILED` / `CANCELLED` → `QUEUED`）只由 `POST /tasks/{id}/retry`
触发**，Worker 不会自己走过去。`SUCCESS` 没有出边 —— 成功了就结束了，要再做一遍就新建任务。

**聚合规则：**

```
全部 SUCCESS              → SUCCESS
有 SUCCESS 也有其它终态    → PARTIAL_SUCCESS
无 SUCCESS 且有 FAILED     → FAILED
用户取消主导               → CANCELLED
还有 CANCELLED / UNKNOWN 混着 → PARTIAL_SUCCESS   （必须有人看一眼）
```

### 折叠可以跳过没人观察到的状态

折叠是从 Run 的终态推任务的终态。任务可能停在 `QUEUED` 而所有 Run 已经走到 `SUCCESS`
（Worker 折 Run，Scheduler 折任务，两个进程）。此时合法路径是 `QUEUED → RUNNING → SUCCESS`，
但 `RUNNING` 没人写过。

于是折叠走的是 `task_route(current, target)`：一张 BFS，只允许**非终态**当跳板，按
`RUNNING → CANCELLING → QUEUED → PENDING` 的顺序试探，找到路就把沿途状态逐个写一遍
（`started_at` 就在这时补上）。

跳板只对非终态开放，所以 `FAILED → … → SUCCESS` 仍然是不可能的。终态到终态只有
`allow_terminal_repivot` 一条路，而它只在人工 resolve 时使用。

## Run（`ops_task_runs.status`）

一个 Run 是「一个任务在一台服务器上的一次执行」。

```
WAITING ──► PRECHECK ──┬──► EXECUTING ──► VERIFYING ──┬──► SUCCESS
                       │                              └──► FAILED
                       ├──► VERIFYING
                       ├──► SUCCESS          （预检本身就是工作内容）
                       ├──► FAILED
                       ├──► CANCELLED        （没跑过，不必表演 CANCELLING）
                       └──► UNKNOWN

PRECHECK / EXECUTING / VERIFYING ──┬──► SUCCESS / FAILED / UNKNOWN / WAITING
                                   └──► CANCELLING ──► CANCELLED

FAILED / CANCELLED / UNKNOWN ──► WAITING   （人工重试 / 人工裁定，attempt+1）
```

| 状态 | 含义 |
|---|---|
| `WAITING` | 已创建，等待认领 |
| `PRECHECK` | 已认领，正在做只读预检（磁盘、OS、依赖） |
| `EXECUTING` | 正在跑步骤 |
| `VERIFYING` | 步骤跑完，正在做健康检查 |
| `SUCCESS` | 终态 |
| `FAILED` | 终态（可人工重试） |
| `CANCELLING` / `CANCELLED` | 取消中 / 已取消 |
| `UNKNOWN` | **远端状态无法判定** —— 见下 |

三条刻意不那么直观的边：

- **`WAITING → CANCELLED`**：还没开始的 Run 直接取消。此时没有在飞的工作要收，
  走一遍 `CANCELLING` 只是表演。
- **`PRECHECK → SUCCESS`**：有些 Run 的预检本身就是全部工作（`server.check` 就是）。
- **活动状态 `→ WAITING`**：租约过期但**还没有结论**的 Run 退回队列是安全的 ——
  它没有宣称过任何结果。有结论的状态（`FAILED` / `CANCELLED` / `UNKNOWN`）回到
  `WAITING` 只能由人推动。

### `UNKNOWN` 不是失败

租约过期 ≠ 远端失败。恢复器对超时 Run 走三步：

1. **探活远端**：`systemd.is_active`、目标 PID 文件、端口探测（只读动作）。
2. **远端仍在跑** → 续租，保持 `EXECUTING`，继续跟日志。
3. **远端已结束但无证据成功** →
   - 步骤是**幂等**的（`package.install`、`file.render`、`systemd.restart`）→ 允许重跑
   - 步骤是**非幂等**的（`mysql.precheck` 之后的初始化、数据迁移）→ 标 **`UNKNOWN`**，挂人工核查
4. **无法判断远端状态**（SSH 连不上）→ **`UNKNOWN`**。

`UNKNOWN` 是一个**需要人工裁决的分支**，不是可自动重试的失败。`POST /tasks/{id}/retry`
会**跳过**它。离开 `UNKNOWN` 的唯一入口是 `POST /task-runs/{id}/resolve`，一次只裁一个
Run，必须给出理由，三种裁定：

| 裁定 | 含义 | 去向 |
|---|---|---|
| `success` | 实际成功了 | `SUCCESS` |
| `retry` | 可以安全重试 | `WAITING`，`attempt+1` |
| `failure` | 按失败处理 | `FAILED` |

自动重跑一个非幂等的数据库初始化步骤，会把目标机搞坏 —— 这条规则没有例外。

### 幂等分级

每个 Action 声明自己的 `idempotency_class`：

| 类别 | 租约过期后的处理 |
|---|---|
| `SAFE` | 直接重跑（纯读或天然幂等） |
| `CONDITIONAL` | 先跑预检；预检说已在目标态就跳过，否则 `UNKNOWN` |
| `DESTRUCTIVE` | 一律 `UNKNOWN`，绝不自动重跑 |

## Step（`ops_task_steps`）

```
PENDING ──► RUNNING ──┬──► SUCCESS
                     ├──► FAILED
                     ├──► CANCELLED
                     └──► UNKNOWN
PENDING ──► CANCELLED / SKIPPED
UNKNOWN ──► SUCCESS / FAILED / CANCELLED
```

`SUCCESS` / `FAILED` / `SKIPPED` / `CANCELLED` 都是硬终态，没有出边；`UNKNOWN` 的出边
同样只有人工裁定能走。步骤断电后重跑靠**新 attempt 行**，不改旧行。

唯一键 `(run_id, step_key, attempt)`。`attempt` 只在真正重跑时 +1 —— **不靠重复插行**表达重试，
因为 `ops_task_runs` 上有 `UNIQUE(task_id, server_id)`，Run 本身就是单行的。

步骤记录 `result_json`、`started_at`、`finished_at`、`exit_code`、`duration_ms`。

### 恢复必须收尾步骤行

步骤说 `RUNNING`，意思就是**有东西正在执行它**。租约恢复里只有 `renew` 仍成立（探针发现远端
还活着），所以只有 `renew` 允许留下 `RUNNING` 的步骤行。其余判定一律把未终结的步骤收尾：

| 判定 | Run | 未终结的步骤 |
|---|---|---|
| `renewed` | 留在 `EXECUTING` | 留在 `RUNNING`（远端还在跑） |
| `skipped` | → `SUCCESS` | → `SUCCESS`（run 级证据说它成了） |
| `requeued` | → `WAITING` | → `UNKNOWN`（这一 attempt 被放弃） |
| `marked_unknown` | → `UNKNOWN` | → `UNKNOWN`（没看到它的下文） |

不收尾的后果在真机演练里见过：部署在 `ensure_user` 中途被 `SIGKILL`，Run 恢复成 `UNKNOWN`
了，步骤还写着 `RUNNING` —— 什么都没在跑。任何数「在跑步骤」的查询都会被它骗。

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
