# OpsPilot

自托管的 Web 运维控制台：通过浏览器管理多台 Ubuntu / Debian Linux 服务器，做软件环境安装、
Python 业务应用发布、服务管理、任务编排、健康检查、发布记录、失败恢复与审计。

**当前阶段：S3 —— 模板协议 / Action Registry / 模板校验与示例模板。**
S0（工程初始化）→ S1（服务器资产）→ S2（任务引擎）已完成；S4（应用发布）待做。
按阶段交付，每个阶段收口时主分支始终可运行、可测试。

---

## 硬约束

这些是设计文档的明令禁止项，代码、依赖、文档、部署流程都不允许违反：

1. **不使用容器**。不引入 Docker、Docker Compose、Kubernetes，也不依赖任何容器化的开发或
   部署流程。进程由 systemd 托管，依赖由 apt / pip / npm 安装。
   交付自查：`find . -iname '*docker*' -o -iname 'Dockerfile*' -o -iname 'docker-compose*'` 必须为空。
2. **不以 root 执行任意 Shell**。远端操作只走 Action Registry 白名单；命令与参数分离（argv
   列表），从不 `shell=True`、从不字符串拼接。默认非 root SSH。
3. **不覆盖已存在的数据目录**。MySQL / Redis 等的 datadir 一旦存在就拒绝初始化；不清空、
   不重置 root 密码、不自动跑不可逆升级。
4. **不提交真实密钥**。只提交 `.env.example`；主加密密钥与密文分离存放；前端永远拿不到
   原始私钥或明文密码。
5. **不伪造测试结果**。每阶段报告真正跑过的命令与真实输出。
6. **危险操作可审计、有二次确认**。后端权限校验是安全边界；前端隐藏按钮不算访问控制。

---

## 技术栈

| 层 | 选择 |
|---|---|
| 后端 | Python 3.12 · FastAPI · Pydantic v2 · SQLAlchemy 2.0 · Alembic · PyMySQL |
| 认证 | PyJWT (HS256) · Argon2id (`pwdlib`) |
| 日志 | structlog，JSON 输出，递归脱敏，带 `request_id` |
| 远程执行 | Paramiko（argv 分离，主机密钥校验开启，禁止 `AutoAddPolicy`） |
| 任务执行 | 独立 Python Worker 进程 + MySQL 租约（`FOR UPDATE SKIP LOCKED`） |
| 前端 | Vue 3 · TypeScript · Vite · Pinia · Vue Router · Element Plus |
| 进度推送 | SSE（`GET /api/v1/tasks/{id}/events`，S2） |
| 数据库 | MySQL，InnoDB，utf8mb4 |
| 进程托管 | systemd（`opspilot-api` / `opspilot-worker`） |

---

## 仓库结构

```
backend/app/
  api/           FastAPI 路由
  core/          settings、logging、security、errors
  db/            Base、session、模型聚合
  auth/          用户 / 角色 / 权限 / RBAC
  audit/         审计写入与查询
  inventory/     S1 服务器资产
  credentials/   S1 凭据
  catalog/       S3 模板
  applications/  S4 应用
  deployments/   S4 发布
  orchestrator/  S2 任务编排
  executor/      S2/S3 SSH + Action Registry
  artifacts/     S4 制品
  notifications/ Telegram 等
worker/          独立 Worker 进程（与 API 同库、独立部署）
shared/          跨进程共享包：redaction、schemas、task_protocol、actions
frontend/        Vue 3 控制台
migrations/      Alembic 版本化迁移
templates/       模板目录（runtime / web / application / database / middleware）
scripts/         安装、预检、引导脚本（不是模板执行器）
docs/            架构、数据库、状态机、模板协议、安全模型、安装
systemd/         单元文件
```

`shared/` 是可 import 的 Python 包，backend 与 worker 都依赖它，**反向绝不 import backend** ——
API 与 Worker 因此可以独立进程部署，又不拆微服务、不产生循环 import。

---

## MySQL 版本偏差（重要）

设计文档写的是 **MySQL 8.4**。Ubuntu 24.04 (noble) 的官方 apt 仓里只有
**`mysql-server-8.0 8.0.46-0ubuntu0.24.04.4`**。

**本项目按 8.0 兼容编写，并在 8.0.46 上验证**，不为此引入第三方 apt 源、不使用容器。
所有 SQL 特性都取 8.0 / 8.4 交集：

- InnoDB、`utf8mb4`、`utf8mb4_0900_ai_ci`
- `BIGINT UNSIGNED` 主键
- `DATETIME(3)` 时间戳，会话时区钉在 UTC
- 普通 DDL / 索引 / 外键
- `SELECT … FOR UPDATE SKIP LOCKED`（任务认领）

8.4 上无需改动即可运行。若目标环境已经是 8.4，直接用即可。

---

## 本地开发

### 前置

- Ubuntu 22.04 / 24.04 或 Debian 12（本文在 Ubuntu 24.04.4 LTS on WSL2 上验证）
- Python 3.12 + `python3.12-venv`
- Node.js 22 + npm
- MySQL 8.0+ （见下）

### 1. 安装并初始化 MySQL

```bash
sudo bash scripts/install-mysql.sh
```

脚本会 `apt install mysql-server`、`systemctl enable --now mysql`，然后**增量**创建
`opspilot` / `opspilot_test` 两个库和一个 `opspilot` 应用用户。

它**不会**：清空已有的 datadir、重置 root 密码、删除已有数据库或用户、执行任何不可逆升级。
若 datadir 已存在，脚本直接拒绝继续。

### 2. 建虚拟环境并安装

```bash
cd ~/Projects/opspilot
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
```

### 3. 配置

```bash
cp .env.example .env
chmod 600 .env
```

至少填：

```bash
OPSPILOT_DB_PASSWORD=<install-mysql.sh 生成的密码>
OPSPILOT_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
OPSPILOT_MASTER_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
```

生产环境请改用 `OPSPILOT_MASTER_KEY_FILE=/etc/opspilot/master.key`（`chmod 600`），让密钥与
它保护的密文分开放置。进程会拒绝读取 group/world 可读的密钥文件。

### 4. 跑迁移

```bash
alembic upgrade head     # 可重复执行：第二次是空操作
```

### 5. 引导账号与权限

```bash
python scripts/bootstrap.py --admin-password '<一个足够长的密码>'
```

只在管理员不存在时创建；**绝不重置已存在账号的密码**。密码必须 ≥ 12 位，不在常见弱口令表里。

### 6. 启动（三条命令，三个独立进程）

```bash
# 一键（后台起 api + worker + 前端 dev，Ctrl-C 收尸）
bash scripts/dev-up.sh

# 或分别手动启动
. .venv/bin/activate && uvicorn backend.app.main:app --host 127.0.0.1 --port 8080
. .venv/bin/activate && python -m worker.main
cd frontend && npm install && npm run dev
```

- API：<http://127.0.0.1:8080>
- 前端：<http://127.0.0.1:5173>（dev server 代理 `/api`、`/healthz`、`/readyz` 到 API）
- 健康检查：`curl -s http://127.0.0.1:8080/healthz`

---

## 测试

```bash
# 后端 + shared + 模板（真 MySQL 集成测试，不用 SQLite 冒充）
. .venv/bin/activate
export PYTHONPATH=$(pwd)
pytest backend/tests worker/tests shared/tests templates

# 静态检查
ruff check .
mypy backend worker shared templates

# 前端
cd frontend
npm run lint
npm run typecheck
npm run test
npm run build
```

集成测试用 `opspilot_test` 库（每用例前清空表）。MySQL 连不上时相关用例 **skip**，而不是
mock 出绿色。`SKIP LOCKED`、utf8mb4 排序规则、`DATETIME(3)`、唯一约束行为在 SQLite 上都不等价，
所以测试库必须是真 MySQL。

**当前 S3 实测结果**（见下方「S3 收口记录」）：`pytest` 549 passed、`ruff` 通过、`mypy` 120 files
通过、前端 lint/typecheck/build 全部通过。

---

## Ubuntu systemd 部署

目标机：Ubuntu 22.04 / 24.04 或 Debian 12，已装 MySQL 8.0+、Python 3.12、Node 仅前端构建时需要。

```bash
# 1. 放置代码
sudo mkdir -p /opt/opspilot
sudo rsync -a --exclude .venv --exclude node_modules --exclude frontend/dist \
    ./  /opt/opspilot/
cd /opt/opspilot

# 2. 虚拟环境（不要用 root 跑服务）
sudo python3.12 -m venv .venv
sudo .venv/bin/pip install -e .

# 3. 构建前端静态文件（在开发机或构建机上做，拷贝 dist/ 过去即可）
cd frontend && npm ci && npm run build && cd ..

# 4. 专用系统用户与日志目录
sudo useradd --system --home /opt/opspilot --shell /usr/sbin/nologin opspilot
sudo mkdir -p /var/log/opspilot /etc/opspilot
sudo chown -R opspilot:opspilot /opt/opspilot /var/log/opspilot
sudo chmod 700 /etc/opspilot

# 5. 配置
sudo install -m 600 -o opspilot -g opspilot .env.example /etc/opspilot/opspilot.env
sudo install -m 600 -o opspilot -g opspilot /dev/null /etc/opspilot/master.key
# ... 写入真实值到 /etc/opspilot/opspilot.env，主密钥写到 master.key
sudo chmod 600 /etc/opspilot/master.key

# 6. 迁移（手动执行，服务启动时不跑 DDL）
sudo -u opspilot .venv/bin/alembic upgrade head
sudo -u opspilot .venv/bin/python scripts/bootstrap.py --admin-password '<密码>'

# 7. 安装单元
sudo install -m 644 systemd/opspilot-api.service    /etc/systemd/system/
sudo install -m 644 systemd/opspilot-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now opspilot-api opspilot-worker

# 8. 验证
systemctl status opspilot-api opspilot-worker --no-pager
curl -sS http://127.0.0.1:8080/healthz
journalctl -u opspilot-api -n 50 --no-pager
```

生产环境把 `OPSPILOT_API_HOST` 留在 `127.0.0.1`，由 Nginx / Caddy 反代并终止 TLS。
`/healthz` 故意不鉴权（供探活），只返回 liveness + 一个粗粒度的 `database: ok|error`，
不含任何配置值或密钥。`/readyz` 在数据库不可达时返回 503，供反代摘流用。

---

## 日志与观测

- 全部 JSON，写 stdout / journal。
- 每条日志自动经过 `shared/redaction.py` 递归脱敏（`password` / `token` / `secret` /
  `private_key` / `authorization` 等键名，以及 PEM、`Bearer `、`AKIA` 等值形态）。
- 每条日志带 `request_id`，与响应头 `X-Request-Id` 一致，错误体里的 `request_id` 也是它。
- 审计写入走**独立事务**，请求失败（401 / 403 / 500）时审计记录仍然落库。

---

## 文档

| 文档 | 内容 |
|---|---|
| [docs/architecture.md](docs/architecture.md) | 进程边界、模块划分、依赖方向 |
| [docs/database.md](docs/database.md) | 表设计、迁移策略、约定 |
| [docs/state-machines.md](docs/state-machines.md) | 任务 / Run / Step 状态机、取消与恢复 |
| [docs/template-protocol.md](docs/template-protocol.md) | 模板格式、Action Registry、参数渲染 |
| [docs/security-model.md](docs/security-model.md) | 威胁模型、认证、RBAC、脱敏、已知限制 |
| [docs/local-install.md](docs/local-install.md) | 本机从零到可用的完整步骤 |

---

## 已知限制（S2）

- 登录失败限速计数在**进程内存**里。API 以单进程跑在 systemd 下时够用；若将来水平扩展，
  需要把计数挪到 MySQL。见 `docs/security-model.md`。
- 凭据目前只支持 `ssh_key` 做连通性检查；`ssh_password` / `sudo_password` 已能存取，
  使用它们的通道在 S3 的 Action Registry 里接。
- **API 进程不碰凭据、不开命令通道。** `POST /servers/{id}/check` 只做前置检查并入队，
  解密与 SSH 全在 Worker。唯一的例外是 `discover_host_key`：未认证的公钥握手，不取凭据、
  不执行命令，必须留在 API 才能让操作员当场确认指纹。
- SSE 的 token 走查询参数（`EventSource` 设不了请求头）。该端点只读，且后端单独校验
  `task.read`。见 `docs/security-model.md`。
- 取消只停后续步骤，**不回滚**。回滚是独立任务，S4 才有 `POST /deployments/{id}/rollback`。
- 模板在 V1 里**不能出现字面 `$`**。严格渲染器拒绝 `${...}` / `$(...)` / 反引号，而 nginx
  之类的配置又要写 `$host`。V1 选择安全优先：需要字面 `$` 的配置文件暂不能用模板渲染，
  改用 `file.render` 的 `content` 原文（里面 `$host` 是允许的，因为被拒绝的只是 `${`、`$(`、
  `` ` `` 三种形态）。见 `docs/template-protocol.md`。
- 模板的 `release_id` 保留字 `latest` / `current` / `shared` / `releases` 一律拒绝，强制钉版本。
- 应用发布链路尚未实现（S4）。
- 前端有登录、外壳、健康状态、服务器、凭据、任务（列表 + 详情）六个页面；模板 / 应用 /
  发布记录 / 审计日志仍标注「即将开放」。

---

## SSH 测试目标（S1/S4 用）

本仓库的远程执行测试只打一台**可销毁**的本地目标，绝不对生产机做破坏性测试。

```bash
# 装 openssh-server、建非 root 的 opstest 用户、生成专用密钥、收紧 sudoers
sudo bash scripts/setup-ssh-test-target.sh
```

脚本会创建 `/opt/opspilot/apps` 与 `/opt/opspilot-test`（属主 `opstest`，写入不需要 sudo），
把私钥放在 `/etc/opspilot/ssh-test/opstest_ed25519`（mode 600），并在
`/etc/sudoers.d/opspilot-opstest` 里只放行 `systemctl` 查询/重启、unit 安装到
`/etc/systemd/system/`、以及 `/opt/opspilot/apps` 下的软链切换。**不放行** `chmod` /
`chown` / `cp` / `mv` / `rm` / `dd` / `sh` / `bash` / `sudo -s`。

然后在 `.env` 里填：

```dotenv
OPSPILOT_TEST_SSH_HOST=127.0.0.1
OPSPILOT_TEST_SSH_PORT=22
OPSPILOT_TEST_SSH_USER=opstest
OPSPILOT_TEST_SSH_KEY_FILE=/etc/opspilot/ssh-test/opstest_ed25519
```

四项要么全填、要么全空。全空时 `backend/tests/test_ssh_live.py` **跳过**而不是假装通过。
应用本身不读这几个键，它们只被测试夹具读取。

---

## S0 收口记录

真实命令与结果见交付说明。门禁命令：

```bash
. .venv/bin/activate && export PYTHONPATH=$(pwd)
alembic upgrade head && alembic upgrade head   # 第二次必须无 "Running upgrade"
pytest backend/tests worker/tests shared/tests
ruff check .
mypy backend worker shared
cd frontend && npm run lint && npm run typecheck && npm run test && npm run build
find . -iname '*docker*' -o -iname 'Dockerfile*' -o -iname 'docker-compose*'
```

---

## S1 收口记录

在 S0 之上新增：服务器资产、加密凭据、SSH 主机密钥信任链、只读系统检查与事实快照、
对应的前端页面。门禁命令与 S0 相同，结果：

- `pytest backend/tests worker/tests shared/tests` → **159 passed**（S0 时 84）
- `ruff check .` → All checks passed
- `mypy backend worker shared` → Success: no issues found in 70 source files
- `npm run lint` / `typecheck` / `test`（15 passed）/ `build` → 全绿
- `find . -iname '*docker*' …` → 空
- 端到端冒烟（真 MySQL + 真 SSH 到本地 `opstest`）：未信任时 check 被 409 拒绝；
  discover 得到的指纹与 `ssh-keygen -lf` 一致；填错指纹 accept 被 409 拒绝；
  正确确认后 check 采到 `ubuntu-24.04 / x86_64 / 12 核 / 15940 MB / 1006.85 GB`；
  所有响应体与审计行均无密钥材料；注入 payload `echo 'hello; touch /tmp/opspilot-pwned'`
  保持单个 argv token，文件未生成。

---

## S2 收口记录

在 S1 之上新增：任务 / Run / Step / 日志四张表与 Outbox、独立 Worker（原子认领 + 租约
心跳 + 过期恢复）、取消证据、`UNKNOWN` 人工裁定、SSE 事件流、任务列表与详情页。

**S1 → S2 接缝已经完成。** API 进程不再解密凭据、不再开命令通道：`POST /servers/{id}/check`
只做前置检查并入队，返回 `202` + `Location: /api/v1/tasks/{id}`；解密、私钥暂存、
Paramiko 连接、命令执行全部在 Worker。唯一的例外是 `discover_host_key`（未认证公钥握手，
不取凭据、不执行命令），见 `docs/security-model.md`。

门禁命令与 S0 相同，真实结果：

- `alembic upgrade head` 连跑两次 → 第二次无 `Running upgrade`
- `pytest backend/tests worker/tests shared/tests` → **262 passed**（S1 时 159）
- `ruff check .` → All checks passed
- `mypy backend worker shared` → Success: no issues found in 94 source files
- `npm run lint` / `typecheck` / `test`（25 passed）/ `build` → 全绿
- `find . -iname '*docker*' …` → 空

S2 新增的并发与恢复测试（都在真 MySQL 上跑）：

| 测试 | 锁死了什么 |
|---|---|
| `test_two_workers_claim_disjoint_runs` | 两并发 Worker 认领，只有一方拿到 |
| `test_task_idempotency_key_is_unique` 等三条 | 三个唯一约束真的会抛 `IntegrityError` |
| `test_safe_step_is_requeued_when_the_host_is_reachable` | 幂等步骤租约过期后可重排，且 `attempt` 不加 |
| `test_destructive_step_is_parked_as_unknown` | 非幂等步骤一律 `UNKNOWN` |
| `test_unreachable_host_is_unknown_even_for_safe_steps` | 连不上就 `UNKNOWN`，不猜 |
| `test_unknown_is_not_reclaimed_as_work` | `UNKNOWN` 不会被下一轮再认领 |
| `test_cancel_mid_step_records_remote_evidence` | 取消留远端证据，不是只改状态列 |
| `test_the_api_never_builds_a_transport` | API 侧只要碰 socket 测试就红 |

**已知取舍（S2）：**

- SSE 事件流用 `?token=` 传访问令牌（`EventSource` 设不了请求头）。端点只读，且单独校验
  `task.read`。若反代能透传 `Authorization`，应改成走头。
- `server.check` 目前是唯一的任务类型。模板与发布（S3/S4）接入后才会出现多步骤计划。
- 取消只停后续步骤，不回滚；回滚任务在 S4。

---

## S3 收口记录

在 S2 之上新增：模板目录与加载器、15 个 V1 动作的 Action Registry、严格模式参数渲染、
模板四层拒绝、版本生命周期与不可变发布、模板目录 API、5 个示例模板（各带校验与幂等测试）。

**S3 的两条安全收口：**

1. **载荷只走 stdin。** `ExecPort.run` 增加 `stdin`，文件内容、systemd unit、curl 配置里的
   token 一律从 stdin 进，argv 里不出现。传输层只记 `stdin_bytes` 长度，绝不记内容。
   `file.render` 用 `dd of=… status=none` 而不是 `tee`（`tee` 会把内容打进 stdout，
   而 `ActionResult.from_exec` 会把 stdout 写进摘要）。Telegram token 走 `curl --config -`，
   同时 `shared/redaction.py` 加了 `/bot<digits>:<token>/` 的模式脱敏兜底。
2. **提权是白名单，不是约定。** `ensure_sudo_allowed` 只放行 `apt-get` / `useradd` / `usermod` /
   `systemctl` / `supervisorctl` / `nginx` / `install`；`chmod` / `chown` / `cp` / `mv` / `rm` /
   `dd` / `sh` / `bash` 刻意不在里面。

**模板校验有四层拒绝**（`shared/templates.py`），任一层不过就整份模板拒绝，绝不「好的步骤先跑」：

| 层 | 拒绝什么 |
|---|---|
| 1 结构 | YAML 不合 `TemplateSpec`、步骤 key 重复、`action` 未注册、name 与目录不一致 |
| 2 绑定 | 参数名不在 action 的 `model_fields`、`{{ inputs.x }}` 未在 schema 声明、`{{ server.y }}` 不在 `SERVER_VALUE_KEYS` |
| 3 渲染 | `{{ a \| b }}`、`{{ f(x) }}`、`{% %}`、`${}`、`$(())`、`` ` ``、不配对的 `{{` |
| 4 干跑 | 用 schema 默认值渲染后交给 action 的 Pydantic 模型再验一次（服务白名单、路径白名单都在这层炸） |

**读路径与写路径是两种规则。** `system.check_disk.path` 是读路径（`ReadPath`）：绝对、无
`..`、无控制字符，但**不必**落在写前缀里 —— 探 `/var/lib/mysql` 是合法的。`file.render.path`
是写路径（`RemotePath`），必须落在 `/opt/opspilot/apps/` 等白名单前缀里。模板校验按 action
声明的字段类型分流，不按参数名猜。

**版本生命周期：** `DRAFT → VALIDATED → TESTED → PUBLISHED → DEPRECATED`。一旦离开 `DRAFT`
正文即冻结（`spec_json` / `inputs_schema_json` / `checksum` 不可改）；`PUBLISHED` 只能变
`DEPRECATED`，没有 unpublish。有集成测试证明「已发布正文被拒绝改写后逐字节不变」。

门禁命令与 S0 相同（`pytest` 路径加了 `templates`，`mypy` 加了 `templates`），真实结果：

- `alembic upgrade head` 连跑两次 → 第二次无 `Running upgrade`
- `pytest backend/tests worker/tests shared/tests templates` → **549 passed**（S2 时 262）
- `ruff check .` → All checks passed
- `mypy backend worker shared templates` → Success: no issues found in 120 source files
- `find . -iname '*docker*' …` → 空

S3 新增测试的分工：

| 位置 | 数量 | 锁死了什么 |
|---|---|---|
| `shared/tests/test_actions.py` | 155 | 15 个动作的注入面、sudo 白名单、路径白名单、stdin 载荷边界、SSRF、token 不进 argv |
| `shared/tests/test_templates.py` | 43 | 四层拒绝各自独立可测、checksum 确定性、生命周期表 |
| `backend/tests/test_catalog_service.py` | 13 | 已发布正文不可变、非法状态迁移拒绝、`(template_id, version)` 唯一 |
| `backend/tests/test_catalog_api.py` | 11 | 权限分离（viewer 读得到但发不了）、404/409 语义、发布被审计 |
| `templates/*/tests/` | 65 | 每个示例模板的合法性 + 重跑收敛（各模板自己断言自己含哪些幂等类别） |

**已知取舍（S3）：**

- 每个模板的测试文件名是 `test_<name>_validate.py` / `test_<name>_idempotency.py`，不是
  一律叫 `test_validate.py`。原因是模板名是 kebab-case（`nginx-site`），永远不可能组成
  Python 包路径，pytest 收集五个同名 `test_validate.py` 会撞模块名。文件名带模板名后，
  失败输出里一眼能看出是哪个模板。
- `TESTED` 状态目前靠服务层 `transition(...)` 置位，HTTP 只暴露 `publish` / `deprecate`。
  「跑过模板自带测试」在 V1 是操作员的判断，不是 CI 回写。
- 模板里的 `secret_ref` 语法已在协议文档里定义，但 V1 的 5 个示例模板都没用到它 ——
  真正接凭据解析在 S4 的发布链路里。
