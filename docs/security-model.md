# 安全模型

## 威胁模型摘要

OpsPilot 是一个**能远程改动服务器的控制台**。它一旦被攻破，攻击者就拿到了所有被管机器的
执行权。所以它的安全边界比一般内部工具要硬。

主要威胁：

| 威胁 | 对策 |
|---|---|
| 凭据泄漏（DB / 日志 / API 响应） | 主密钥与密文分离；递归脱敏；API 从不返回密钥材料 |
| 命令注入 | Action Registry 白名单 + argv 分离 + 严格参数渲染，无 `shell=True` |
| 路径穿越 | 所有远端路径必须绝对路径且前缀在白名单 |
| 越权操作 | RBAC 后端强制；前端隐藏按钮**不是**控制 |
| 重复 / 并发执行 | 三层幂等 + `SKIP LOCKED` 原子认领 |
| 非幂等步骤被自动重跑 | `UNKNOWN` 状态，强制人工裁决 |
| 中间人 / 主机冒充 | SSH 主机密钥校验开启，首次接入显式指纹确认 |
| 提权 | 非 root SSH；受限 sudo 白名单；systemd 加固 |

## 认证

- 用户名 + 密码 → JWT（HS256，`typ=access`，含 `jti`）。
- 密码哈希 **Argon2id**（`pwdlib` 的 `PasswordHash.recommended()`），最短 12 位，拒绝常见弱口令。
- 登录失败与成功都写审计。
- 「用户不存在」与「密码错误」**响应完全一致**（同样的 `unauthenticated`，同样的耗时 ——
  未知用户走一条 dummy hash 的等时路径），避免用户名枚举。
- 换签名密钥、过期 token、缺 token、伪造 token 一律 401。

### 已知限制：登录限速在进程内存

`backend/app/auth/router.py` 里用一个进程内滑动窗口记失败次数
（`OPSPILOT_LOGIN_MAX_FAILURES` / `OPSPILOT_LOGIN_WINDOW_SECONDS`）。

**为什么这样：** V1 的目标部署是 systemd 下的单 API 进程，进程内存足够，也避免为此引入
Redis 或额外表。

**限制是什么：** 多实例部署时每个进程各算各的，限速会被稀释。若将来水平扩展 API，必须把
计数挪到 MySQL（一张 `ops_login_attempts` 表 + 时间窗口查询）或共享存储。这条是**已知的、
有意的简化**，不是疏漏。

测试钩子 `reset_rate_limiter()` 只在测试里用。

## 授权（RBAC）

五张表：`ops_users` / `ops_roles` / `ops_permissions` / `ops_user_roles` / `ops_role_permissions`。
用户的最终权限是**所有角色权限的并集**。

每个特权端点都挂 `require_permission("xxx")` 依赖，**每次请求都查库**（不做进程内缓存），
所以改权限立刻生效。

**前端隐藏按钮不是安全控制。** `PermissionGate.vue` 与侧栏的权限过滤只是 UX，让操作者
不被提供用不了的入口。后端必须独立拒绝同一个操作。测试 `test_rbac.py` 专门验证
viewer 调 `POST /auth/users` 会拿到 403 且消息里含缺失的权限码。

默认角色：

| 角色 | 权限 |
|---|---|
| `admin` | 全部 |
| `operator` | 跑安装与发布，但不能管账号 |
| `viewer` | 只读：服务器、凭据元数据、模板、任务、应用、发布 |

权限码是**存储中的稳定词汇**（`user.create`、`task.resolve`、`deployment.rollback` …），
改名等于破坏存储的审计与角色配置。`test_rbac.py::test_permission_codes_are_stable` 锁死这一点。

## 凭据

- 存储形态：`secret_ref`（引用）+ Fernet 加密载荷。主密钥来自 `OPSPILOT_MASTER_KEY_FILE`
  （推荐）或 `OPSPILOT_MASTER_KEY`。
- 主密钥文件若 mode 含 group/world 位，进程**拒绝读取**并报错。
- 前端**永远拿不到**原始私钥或明文密码。读接口只返回元数据（名称、类型、指纹、最后使用时间）。
- 解密只发生在 Worker 真正要用的那一刻，用完即弃，不进日志。

## 脱敏

`shared/redaction.py` 是**唯一**出口。所有日志、所有审计 snapshot、所有错误 details 都过它。

键名匹配（大小写不敏感，忽略 `-`/`_`）：
`password`、`passwd`、`secret`、`token`、`private_key`、`privatekey`、`apikey`、`api_key`、
`passphrase`、`credential`、`authorization`。

值形态匹配：`-----BEGIN`、`ssh-rsa `、`AKIA` 前缀，以及文本里的 `password=`、`token=`、
`Authorization:`、`Bearer `。

`redact()` 递归走 dict / list / tuple / set / dataclass，有深度上限。`test_redaction.py`（14 个
用例）和 `test_settings_and_logging.py::test_safe_summary_presence_flags_survive_redaction` 锁死
契约。

**一个反直觉的约束：** `safe_summary()` 的键名必须避开上面的标记词，否则「有没有配数据库密码」
这种布尔标记会被脱敏成 `[REDACTED]`。所以键叫 `db_login_present` 而不是 `db_password_set`。
这个命名约束有测试守着。

## 审计

`write_audit()` **用自己的事务、立刻提交、永不抛异常**。

原因：FastAPI 会把端点异常抛进 `get_db()` 的 `yield`，于是 `commit()` 被跳过、`rollback()`
执行 —— 如果审计行写在请求会话里，一次失败的登录（401）会把「有人试过」的记录一起回滚掉，
审计就失去了意义。所以审计必须独立于请求事务。测试
`test_audit.py::test_failed_login_is_audited` 守着这条。

审计 `snapshot` 在写入前就脱敏，所以即使调用方不小心传了密钥，落库的也是 `[REDACTED]`。

## SSH

- **主机密钥校验开启。** 禁止 `AutoAddPolicy`。首次接入必须显式确认指纹
  （`ops_servers.host_key_fingerprint` + 一次 accept 操作）。
- 默认**非 root** 用户（`OPSPILOT_SSH_USER`）。
- 需要提权的步骤走**受限 sudo 白名单**，不是 `sudo -s`。
- 命令一律 argv 列表，超时可控（`OPSPILOT_SSH_CONNECT_TIMEOUT` / `OPSPILOT_SSH_COMMAND_TIMEOUT`）。

## 远端执行边界

- **不允许任意 Shell。** 只有 Action Registry 里注册的动作可执行。
- **不覆盖已有数据目录。** MySQL / Redis / ES / ClickHouse 的 datadir 一旦存在就拒绝初始化；
  不清空、不重置 root 密码、不自动跑不可逆升级。
- 危险操作（`service.manage`、`middleware.install`、`deployment.rollback`）需要额外权限
  **加二次确认**，并在审计里留下确认人与确认时间。

## HTTP

- 统一错误体 `{code, message, request_id[, details]}`。堆栈只进日志。
- 请求校验失败只回字段**位置**，不回显值（避免把密码原样退回）。
- `request_id` 来自 `X-Request-Id`（仅当 ≤64 字符且 ASCII，否则重新生成），响应头回写，
  错误体里也带，方便对账。
- CORS 仅在 dev 且显式列出 origin 时开启。

## 前端

- Token 存 `localStorage`，只作 Bearer 头发送，不进 cookie —— 避免 CSRF。
- **注意：** SSE 的 `EventSource` 无法自定义请求头，所以任务事件流把 token 放在查询参数里。
  该端点在后端单独校验，且只允许读事件流。这是文档化的取舍，不是疏漏；若反代能保留
  Authorization 头，应改成走头。
- 前端从不渲染 `details` 字段里的任意内容，只渲染 `code` / `message` / `request_id`。

## 不做的事

- 不用容器（硬约束）。
- 不在日志里打印解析后的密钥。
- 不自动重跑非幂等步骤。
- 不把取消当成回滚。
- 不用真 Jinja / eval 做参数渲染。
- 不用 SQLite 充当测试库。
