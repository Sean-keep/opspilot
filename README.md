# OpsPilot

自托管的 Web 运维控制台：通过浏览器管理多台 Ubuntu / Debian Linux 服务器，做软件环境安装、
Python 业务应用发布、服务管理、任务编排、健康检查、发布记录、失败恢复与审计。

**当前阶段：S0 —— 工程初始化 / 迁移 / 认证 RBAC / 健康检查 / 前端壳。**
S1（服务器资产）→ S2（任务引擎）→ S3（模板协议）→ S4（应用发布）按阶段交付，每个阶段收口时
主分支始终可运行、可测试。

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
# 后端 + shared（真 MySQL 集成测试，不用 SQLite 冒充）
. .venv/bin/activate
export PYTHONPATH=$(pwd)
pytest -q backend/tests shared/tests

# 静态检查
ruff check .
mypy backend worker shared

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

**当前 S0 实测结果**（见下方「S0 收口记录」）：`pytest` 84 passed、`ruff` 通过、`mypy` 54 files
通过、前端 lint/typecheck/7 tests/build 全部通过。

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

## 已知限制（S0）

- 登录失败限速计数在**进程内存**里。API 以单进程跑在 systemd 下时够用；若将来水平扩展，
  需要把计数挪到 MySQL。见 `docs/security-model.md`。
- 任务引擎、SSE、模板协议、应用发布尚未实现（S2–S4）。
- Worker 目前是可启动 / 可优雅退出的空转循环，认领逻辑在 S2 填入。
- 前端只有登录、外壳与健康状态页；侧栏里尚未接入的条目标注「即将开放」。

---

## S0 收口记录

真实命令与结果见交付说明。门禁命令：

```bash
. .venv/bin/activate && export PYTHONPATH=$(pwd)
alembic upgrade head && alembic upgrade head   # 第二次必须无 "Running upgrade"
pytest -q backend/tests shared/tests
ruff check .
mypy backend worker shared
cd frontend && npm run lint && npm run typecheck && npm run test && npm run build
find . -iname '*docker*' -o -iname 'Dockerfile*' -o -iname 'docker-compose*'
```
