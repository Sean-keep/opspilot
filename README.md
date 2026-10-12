# OpsPilot

自托管的 Web 运维控制台：通过浏览器管理多台 Ubuntu / Debian Linux 服务器，做软件环境安装、
Python 业务应用发布、服务管理、任务编排、健康检查、发布记录、失败恢复与审计。

**S0 → S4 全部交付完毕。**
S0（工程初始化）→ S1（服务器资产）→ S2（任务引擎）→ S3（模板协议）→ S4（Python + systemd
应用发布链路 / 健康检查 / 版本目录）已逐阶段收口，每个阶段收口时主分支始终可运行、可测试。
S5（多机批次回滚、制品仓库、`keep_releases` 清理）不在本次范围内。

| 我想… | 去哪 |
|---|---|
| 5 分钟先跑起来看看 | [5 分钟跑起来](#5-分钟跑起来compose-快速体验可选) |
| 知道哪些事绝对不能做 | [硬约束](#硬约束) |
| 在开发机上从零装一遍 | [本地开发](#本地开发) |
| 部署到生产（systemd） | [Ubuntu systemd 部署](#ubuntu-systemd-部署) |
| 跑测试 / lint / 类型检查 | [测试](#测试) |
| 看架构与状态机 | [文档](#文档) |
| 看每个阶段真实跑出来的结果 | [收口记录](#s0-收口记录) |

---

## 硬约束

这些是设计文档的明令禁止项，代码、依赖、文档、**产品的远程执行与生产部署流程**都不允许违反：

1. **产品的运维动作与生产部署不用容器**。远端操作、依赖安装、服务托管一律走
   apt / pip / npm + systemd，**绝不引入 Kubernetes / Helm / Operator**，也绝不把容器
   写进 Action Registry 的任何一条执行路径。

   仓库里的 `Dockerfile` / `docker-compose.yml` / `.dockerignore` **只是本地快速体验的入口**
   （见下节），不承载生产流量、不参与任何一次远程执行。设计文档原文要求「不引入 Docker、
   Docker Compose、Kubernetes 或依赖容器的开发流程」；本次按你确认的口径收窄为
   **生产与产品的运维动作不用容器**，快速体验文件是唯一的例外，且被上面几条限制锁死。

   交付自查：

   ```bash
   # 只允许这三个快速体验文件，多一个就改错了
   find . \( -path ./node_modules -o -path ./.venv -o -path ./frontend/node_modules \) -prune \
       -o \( -iname '*docker*' -o -iname 'Dockerfile*' -o -iname 'docker-compose*' \) -print
   # → ./.dockerignore  ./Dockerfile  ./docker-compose.yml

   # K8s / Helm 一律为空
   find . \( -iname '*k8s*' -o -iname '*kubernetes*' -o -name 'Chart.yaml' -o -name 'values.yaml' \) -print
   ```
2. **不以 root 执行任意 Shell**。远端操作只走 Action Registry 白名单；命令与参数分离（argv
   列表），从不 `shell=True`、从不字符串拼接。默认非 root SSH。
3. **不覆盖已存在的数据目录**。MySQL / Redis 等的 datadir 一旦存在就拒绝初始化；不清空、
   不重置 root 密码、不自动跑不可逆升级。
4. **不提交真实密钥**。只提交 `.env.example`；主加密密钥与密文分离存放；前端永远拿不到
   原始私钥或明文密码。
5. **不伪造测试结果**。每阶段报告真正跑过的命令与真实输出。
6. **危险操作可审计、有二次确认**。后端权限校验是安全边界；前端隐藏按钮不算访问控制。

---

## 5 分钟跑起来（Compose 快速体验，可选）

> **这是体验入口，不是生产部署。** 产品的运行方式是 systemd + 裸机，见
> [Ubuntu systemd 部署](#ubuntu-systemd-部署)。Compose 只是让人不用先装 MySQL 和 Node
> 就能把界面点一遍。硬约束第 1 条对它同样有效：不许把它扩成 Kubernetes。

前置：Docker 29+ 与 Docker Compose 插件（`docker compose version`）。

### 1. 配置

```bash
cp .env.example .env
chmod 600 .env
```

填四个值（其余都有默认）：

```bash
OPSPILOT_DB_PASSWORD=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")
MYSQL_ROOT_PASSWORD=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")
OPSPILOT_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
OPSPILOT_MASTER_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
```

把这四行的输出写进 `.env`。**`.env` 已被 gitignore，绝不提交。**

> 配置只走一个旋钮：`--env-file`。默认读 `.env`，想用别的文件就
> `docker compose --env-file .env.quickstart up --build`，栈读的一定是你指定的那一份。

### 2. 起来

```bash
docker compose up --build -d
```

编排会按顺序做：`db` 就绪 → `init` 跑 `alembic upgrade head`（已在 head 时是空操作）→
`api` / `worker` 起 → `web` 暴露到 **<http://127.0.0.1:8088>**。

> 8088 被别的服务占了就换一个：`OPSPILOT_QUICKSTART_PORT=8099` 写进 env 文件再 `up`。
> 端口冲突的症状是 `web` 停在 `Created`，`docker inspect` 里写着
> `Bind for 0.0.0.0:8088 failed: port is already allocated`。

> **第一次 build 要下载整棵依赖树**（Python 侧 20 来个包，Node 侧 305 个包）。
> 网络正常时几分钟；链接被限速时会长很多 —— 这条链路的耗时全在拉包上，不在本项目的代码上。
>
> 仓库默认走**官方源**（`pypi.org` / `registry.npmjs.org`），不内置任何镜像。确实拉不动时，
> 在 env 文件里改这两个**只影响 build** 的键：
>
> ```dotenv
> PIP_INDEX_URL=https://your.pypi.mirror/simple
> NPM_CONFIG_REGISTRY=https://your.npm.mirror/
> ```
>
> 一个真实的坑，记在这里免得下次再撞：`npm ci` 对**下载失败的 optional 依赖是静默跳过**的。
> Vite 8 的 rolldown 是按平台拆的 optional 原生绑定，掉一个就变成
> `Cannot find module '@rolldown/binding-linux-x64-gnu'` —— 报错在打包器深处，离原因很远。
> 所以 `Dockerfile` 在 `npm ci` 之后先 `import('rolldown')` 验一遍，缺了就当场说清楚。

### 3. 建管理员

```bash
docker compose exec api python scripts/bootstrap.py --admin-password '<一个足够长的密码>'
```

只在管理员不存在时创建，**绝不重置已存在账号的密码**。密码 ≥ 12 位且不在弱口令表里。

### 4. 用

浏览器开 <http://127.0.0.1:8088>，用刚才的账号登录。

- 想跑真远程任务（服务器检查 / 发布），还得接入一台 SSH 目标，见
  [SSH 测试目标](#ssh-测试目标s1s4-用)。
- 想跑测试，**不要在容器里跑** —— 集成测试要连真 MySQL 并清空 `opspilot_test`，
  见 [测试](#测试)。

### 收摊

```bash
docker compose down          # 停容器，保留数据卷
docker compose down -v       # 连数据卷一起删（会丢库，确认后再用）
```

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
shared/          跨进程共享包：redaction、schemas、task_protocol、actions、config
frontend/        Vue 3 控制台
migrations/      Alembic 版本化迁移
templates/       模板目录（runtime / web / application / database / middleware）
scripts/         安装、预检、引导脚本（不是模板执行器）
docs/            架构、数据库、状态机、模板协议、安全模型、安装
systemd/         生产用单元文件
deploy/          快速体验用的 nginx 反代配置（非生产）
Dockerfile       快速体验镜像（可选，见硬约束 1）
docker-compose.yml  快速体验编排（可选，见硬约束 1）
```

`shared/` 是可 import 的 Python 包，backend 与 worker 都依赖它，**反向绝不 import backend** ——
API 与 Worker 因此可以独立进程部署，又不拆微服务、不产生循环 import。

---

## MySQL 版本偏差（重要）

设计文档写的是 **MySQL 8.4**。Ubuntu 24.04 (noble) 的官方 apt 仓里只有
**`mysql-server-8.0 8.0.46-0ubuntu0.24.04.4`**。

**本项目按 8.0 兼容编写，并在 8.0.46 上验证**，不为此引入第三方 apt 源去追 8.4。
Compose 快速体验里用的也是 `mysql:8.0`，与开发/生产同版本。所有 SQL 特性都取 8.0 / 8.4 交集：

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

**当前 S4 实测结果**（见下方「S4 收口记录」）：`pytest` 673 passed、`ruff` 通过、`mypy` 135 files
通过、前端 lint/typecheck/test/build 全部通过。

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
| [docs/deployment.md](docs/deployment.md) | 发布计划、原子切换、恢复策略、密钥走向 |
| [docs/security-model.md](docs/security-model.md) | 威胁模型、认证、RBAC、脱敏、已知限制 |
| [docs/local-install.md](docs/local-install.md) | 本机从零到可用的完整步骤 |

---

## 已知限制（S4）

- 登录失败限速计数在**进程内存**里。API 以单进程跑在 systemd 下时够用；若将来水平扩展，
  需要把计数挪到 MySQL。见 `docs/security-model.md`。
- 凭据目前只支持 `ssh_key` 做连通性检查；`ssh_password` / `sudo_password` 已能存取，
  使用它们的通道在 Action Registry 里接。
- **API 进程不碰凭据、不开命令通道。** `POST /servers/{id}/check` 只做前置检查并入队，
  解密与 SSH 全在 Worker。唯一的例外是 `discover_host_key`：未认证的公钥握手，不取凭据、
  不执行命令，必须留在 API 才能让操作员当场确认指纹。
- SSE 的 token 走查询参数（`EventSource` 设不了请求头）。该端点只读，且后端单独校验
  `task.read`。见 `docs/security-model.md`。
- 取消只停后续步骤，**不自动回滚**。回滚是独立的发布记录（`POST /deployments/{id}/rollback`），
  由操作员显式发起并二次确认。
- **回滚只做到「能创建回滚任务」。** 单条记录对应一次任务、每台目标机一个 Run；多机批次
  的联动回滚、以及「整批失败就整批回滚」的策略是 S5。
- 环境互斥（同一应用环境同时只允许一条未终结发布）在并发下是**建议性**的，不是数据库
  级排它锁。两个同时通过检查的请求都能插进去。见 `docs/deployment.md`。
- 发布制品是**拉取 URL + SHA-256**，仓库本身不在 V1 里（`backend/app/artifacts/` 还是空模块）。
  校验和不符就拒绝解压。
- `keep_releases` 只是保留条数的提示，**还没有清理动作**。`releases/<id>` 只追加；
  `shared/data` 永不自动清理 —— 这两条都是刻意的。
- 模板在 V1 里**不能出现字面 `$`**。严格渲染器拒绝 `${...}` / `$(...)` / 反引号，而 nginx
  之类的配置又要写 `$host`。V1 选择安全优先：需要字面 `$` 的配置文件暂不能用模板渲染，
  改用 `file.render` 的 `content` 原文（里面 `$host` 是允许的，因为被拒绝的只是 `${`、`$(`、
  `` ` `` 三种形态）。见 `docs/template-protocol.md`。
- `release_id` / `release_version` 的保留字 `latest` / `current` / `shared` / `releases` / `ops`
  一律拒绝，强制钉版本 —— 不只在生产。
- 前端有登录、外壳、健康状态、服务器、凭据、任务（列表 + 详情）、应用、发布记录八个页面；
  模板 / 审计日志仍标注「即将开放」。

---

## SSH 测试目标（S1/S4 用）

本仓库的远程执行测试只打一台**可销毁**的本地目标，绝不对生产机做破坏性测试。

```bash
# 装 openssh-server、建非 root 的 opstest 用户、生成专用密钥、收紧 sudoers
sudo bash scripts/setup-ssh-test-target.sh
```

脚本会创建 `/opt/opspilot/apps` 与 `/opt/opspilot/test`（属主 `opstest`，写入不需要 sudo；
后者是 `systemd.render_unit` 暂存 unit 正文的 scratch 目录，与 `ALLOWED_PATH_PREFIXES`
里的 `/opt/opspilot/test/` 一致），
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

## 收口记录

以下四节是各阶段收口时**真实跑过**的命令与输出，逐字留档，不回填、不美化。
其中 `find . -iname '*docker*' … → 空` 是当时的事实；S4 交付之后按确认口径加了
Compose 快速体验文件，**这条自查的现状以硬约束第 1 条为准**（只允许三个快速体验文件）。
其余门禁命令与结果不受影响。

### S0 收口记录

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

### S1 收口记录

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

### S2 收口记录

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

### S3 收口记录

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

---

### S4 收口记录

在 S3 之上新增：应用 / 环境定义、发布与回滚记录、`shared/deploy_plan.py` 的 §8.2 计划、
第 16 个动作 `app.switch_current`、Worker 侧的发布执行与失败恢复、应用与发布记录两个前端页面。

**S4 的三条收口：**

1. **计划在提交时就组一遍。** `build_deploy_plan()` 在 API 侧跑一次：组装不出来的提交
   （可变别名、缺校验和、SSRF 形状的制品地址）不会被记成一条发布，而不是跑三步之后才炸。
   Worker 用同一份输入重建同一份计划 —— 批下来的，就是跑的。
2. **密钥从不进任务行。** 任务的 `params` 里只有 `config_ref`（与 `deploy` 平级），值由 Worker
   在执行时从 `ops_credentials` 解析。API 内存里、任务行里、步骤参数里、日志里都不出现明文配置。
   回滚重写 env 文件，所以它从被撤销那条发布的任务参数继承**非密文**的 `config` / `module_name` /
   `app_port` / `python_version` / `requirements`，否则一次回滚会把操作员填过的配置抹掉。
3. **不归零点是 `switch_current`。** 切换前失败 → 停批，线上不动，记录 `FAILED`；
   切换这一步或之后失败 → 跑恢复计划把旧版本放回去，记录 `ROLLED_BACK`；
   首次发布失败且无处可放回 → `FAILED` 并写明「链路已翻且无版本可放回」，不假装没事。
   切换本身是 `ln -s` + `mv -Tf`，**绝不 `rm` 旧链接再 `ln`**（那个窗口里 `current` 不存在）。

**两个真实的 bug，在 S4 测试里抓到并修掉：**

- **`ops_tasks.idempotency_key` 的 `''` 唯一值陷阱。** 列原本是 `NOT NULL DEFAULT ''` + UNIQUE。
  InnoDB 允许多个 `NULL`、只禁重复的非 `NULL` 值，而 `''` 是非 `NULL` —— 于是**整张表只能有一条
  不带 `Idempotency-Key` 的任务**，第二次无 key 提交直接 `IntegrityError` → 500。迁移
  **`0006_task_idempotency_nullable`** 把列改成 `NULL DEFAULT NULL` 并把已有 `''` 归一成 `NULL`；
  UNIQUE 索引不动（NULL 彼此不冲突，正是要的语义）。`downgrade()` 刻意不把 `NULL` 写回 `''`
  —— 那会让它们全撞到同一个值上。
- **`system.check_os` 永远匹配不上。** 原来是 `any(token in stdout for token in expect)`，
  而 `ubuntu-24.04` **不是** `/etc/os-release` 任何一行的子串，真实机器上每次检查都失败。
  现在解析出 `ID` / `VERSION_ID` 拼成 `ubuntu-24.04` / `debian-12` 再比对（`<ID>` 也接受）。
  这同时救回了 `templates/runtime/python312` 等用同一份 expect 的模板。

**另一个修掉的接缝：** Worker 原来从 `deploy` 映射里找 `config_ref`，而 API 把它写在任务参数
顶层 —— 密钥永远到不了目标机。现在两边约定一致：顶层 `config_ref`，`deploy` 里没有它。

门禁命令与 S0 相同（`pytest` 含 `templates`，`mypy` 含 `templates`），真实结果：

- `alembic upgrade head` 连跑两次 → 第二次无 `Running upgrade`
- `pytest backend/tests worker/tests shared/tests templates` → **673 passed, 1 warning in 412.64s**（S3 时 549）
- `ruff check .` → All checks passed!
- `mypy backend worker shared templates` → Success: no issues found in 135 source files（S3 时 120）
- `cd frontend && npm run lint` / `typecheck` / `test`（**38 passed**，6 个测试文件）/ `build` → 全绿
- `find . -iname '*docker*' …` → 空

S4 新增测试的分工（文件内总数，含 S3 累积）：

| 位置 | 数量 | 锁死了什么 |
|---|---|---|
| `shared/tests/test_actions.py` | 183 | 16 个动作的注入面、sudo / 路径白名单、stdin 载荷边界、`app.switch_current` 的 argv 形状与幂等跳过、`test` 探针真打 `/usr/bin/test` |
| `shared/tests/test_deploy_plan.py` | 21 | 计划顺序与 `switch_index`、恢复计划的形状、`inputs_from_mapping` 拒绝未知字段、env 文件每键只写一次、与部署输入同名的 config 键拒绝 |
| `backend/tests/test_task_engine.py` | 35 | `SKIP LOCKED` 并发认领、三个唯一约束、租约过期恢复、`UNKNOWN` 不被再认领、恢复后步骤行不再假装 `RUNNING` |
| `backend/tests/test_applications_api.py` | 23 | 名字规范化与保留名、unit 路径、服务白名单、SSRF、不能改名、无 DELETE、viewer 只读 |
| `backend/tests/test_deployments_api.py` | 25 | 钉版本、校验和、SSRF、环境互斥、`previous_release_id` 的推导、密钥不进任务行、回滚二次确认、幂等回放 |
| `backend/tests/test_worker_deploy.py` | 18 | 切换前失败停批、切换后失败放回旧版本、首次发布失败无处可放、密文只进 env 文件、切换幂等、记录生命周期 |

#### S4 端到端演练与抓到的 bug

一次真实发布（本地 `opstest` SSH 目标 + 真 systemd + 真 MySQL）跑通了 §8.2 的 11 步：

```
check_os / check_disk / ensure_user / ensure_runtime / fetch_release /
install_release / render_config / render_unit / switch_current /
restart_service / check_health        →  11/11 SUCCESS
current -> releases/1.4.2
curl http://127.0.0.1:8888/healthz  →  {"status":"ok"}
```

演练**故意**是找 bug 的，不是演示。抓到并修掉九个，都是测试替身报喜、真机才炸的那类：

| # | bug | 症状（真机） | 为什么测试没拦住 |
|---|---|---|---|
| 1 | `app.fetch_release` 不建 `releases/` | `curl: (23) Failure writing output to destination` | 替身不检查父目录是否存在 |
| 2 | `file.render` 不建父目录 | `dd: failed to open '…/.new'` | 同上 |
| 3 | `systemd.render_unit` 的 scratch 目录写错 | `dd: failed to open '/opt/opspilot/test/…'`，脚本建的是 `/opt/opspilot-test` | 脚本与代码的路径各自「正确」，没对齐 |
| 4 | `mkdir` 失败被丢掉 | 真实错误（`Permission denied`）被下一条 `dd` 的报错盖住 | 看起来只是 `dd` 失败 |
| 5 | `_exists` 写成 `test -e -- path` | **每个存在性探针都返回「不存在」**：不备份、release 永远重解压、锁文件永远像缺失、`app.switch_current` 拒绝切换 | 见下 |
| 6 | `systemd-analyze verify` 拒绝合法 unit | `ExecStart` 指向还没翻的 `current/...`，切换前必然不存在 | 只测过已切换的路径 |
| 7 | `Scheduler._renew` 每次成功都告警 | `worker.lease_lost` 打在每条成功 run 上 | 断言只写了「丢了要报」，没写「没丢别报」 |
| 8 | `apply_recovery` 不收尾步骤行 | run 已 `UNKNOWN`，步骤仍显示 `RUNNING`（谎称在跑） | 恢复测试只断言 run 状态 |
| 9 | `render_env_file` 把 `APP_PORT` 写两遍 | `config: {"APP_PORT": "9999"}` + `app_port: 8888` 时，进程听 9999、健康检查探 8888 | 单测只用 `in body`，查不出重复 |

**第 5 条是这批里最危险的。** POSIX 的 `test` 不把 `--` 当选项终结符：

```
$ /usr/bin/test -e -- /some/path
test: '--': binary operator expected      # exit 2
$ /usr/bin/test -e /some/path             # exit 0
```

三个操作数时 `test` 把 `$2` 当二元运算符 —— 报错退出 2，和「路径不存在」看起来一模一样。
`readlink --` / `mkdir -p --` / `ln -s --` / `mv -Tf --` / `rm -f --` 都认 `--`（GNU coreutils），
唯独 `test` 不认。它静默关掉了**所有**存在性探针：`file.render` 的备份、`install_release`
的已装判断、`systemd.render_unit` 的备份、`mysql.precheck` 的 datadir 判断、
`redis.precheck` 的配置判断、`app.switch_current` 的 release 判断，全是假的。

它能活到真机，是因为测试替身对 `test` 一律回成功/失败的**罐头答案**，而罐头答案分不出
「路径不在」和「argv 写错了」。回归测试换成 `RealTestPort`：真正 `exec` `/usr/bin/test`，
断言 `present` 为真、`gone` 为假、argv 里没有 `--`。把修复改回去，该测试立刻红。

**第 6 条的判定口径**是「语法检查」（文档原词）：`systemd-analyze verify` 同时做语法检查和
「`ExecStart` 能不能解析」，后者在切换前**必然**失败，是合法的推迟，不是错误。现在
`classify_verify_output` 把输出分成 `(problems, deferred)`：只有 `Command … is not executable`
且路径含 `/current/` 才算 deferred；含 `ignoring` 的 systemd 警告丢弃；其余都是 problems。
**空 stderr + 非零退出不算通过** —— 那是没解释的坏退出，不是好结果。

**杀掉 worker 重启，任务状态可恢复**（计划里的收口项）在真机上验了两种形状，结论不同、都是设计如此：

| 场景 | 杀在哪 | 租约过期后的判定 | 结果 |
|---|---|---|---|
| `server.check`（全是 `SAFE` 步骤） | `collect_facts` 正在跑 | `probe=finished` → **`requeued`** | 重排队，`attempt` 1→2，最终 `SUCCESS` |
| `app.deploy`（含 `CONDITIONAL` / `DESTRUCTIVE`） | `ensure_user` 正在跑 | `probe=finished` → **`marked_unknown`** | run 停在 `UNKNOWN`，**不自动重跑**；`current` 仍指旧版本，线上没动 |

worker 日志里的两条恢复事件：

```json
{"run_id": 7, "probe": "finished", "decision": "requeued",       "idempotency_class": "SAFE",        "event": "worker.lease_recovery"}
{"run_id": 9, "probe": "finished", "decision": "marked_unknown", "idempotency_class": "CONDITIONAL", "event": "worker.lease_recovery"}
```

部署那条停在 `UNKNOWN` 是**对的**：`restart_service` 是 `DESTRUCTIVE`、`ensure_user` 是
`CONDITIONAL`，而 `_precheck_target_state()` 在 V1 故意返回 `False` —— 宁可停下来叫人，
也不猜「大概装完了吧」然后重跑一遍不可逆的步骤。单 run 的 `UNKNOWN` 按聚合规则折成任务
`PARTIAL_SUCCESS`（跑完了，但要人看一眼），`UNKNOWN` 永远不折成 `SUCCESS`。

**已知取舍（S4）：**

- 回滚只做到「能创建回滚任务」。单条记录对应一次任务、每台目标机一个 Run；多机批次的
  联动回滚、以及「整批失败就整批回滚」的策略是 S5。
- 环境互斥（§10）在并发下是建议性的，不是数据库级排它锁。两个同时通过检查的请求都能插进去。
  Worker 侧的逐步执行仍是最后一道防线。
- `keep_releases` 只是保留条数的提示，还没有清理动作。`releases/<id>` 只追加；
  `shared/data` 永不自动清理 —— 这两条都是刻意的。
- 制品是拉取 URL + SHA-256，仓库本身不在 V1（`backend/app/artifacts/` 还是空模块）。
- `system.check_os` 的平台词汇表写进了 `docs/deployment.md`；设计文档里没有这一节，
  但 expect 的取值必须和 `/etc/os-release` 的解析结果对齐，否则整条发布链路起不来。

---

### Compose 快速体验收口（本次）

在 S4 之后按确认口径加的：**Compose 只做本地快速体验入口，生产与产品的运维动作仍不用容器**
（硬约束第 1 条已改写，见上）。新增四个文件：`Dockerfile` / `docker-compose.yml` /
`.dockerignore` / `deploy/quickstart-nginx.conf`。**没有 Kubernetes / Helm / Operator，也不许有。**

#### 门禁（在改完 Compose 与 README 后重跑，真实结果）

- `alembic upgrade head` 连跑两次 → 第二次无 `Running upgrade`
- `pytest backend/tests worker/tests shared/tests templates` → **673 passed, 1 warning in 506.92s**
- `ruff check .` → All checks passed!
- `mypy backend worker shared templates` → Success: no issues found in 135 source files
- `cd frontend && npm run lint` / `typecheck` / `test`（**38 passed**，6 个测试文件）→ 全绿

#### Compose 栈真跑起来（`docker compose --env-file … up --build -d`）

```
opspilot-db-1      mysql:8.0        Up (healthy)
opspilot-init-1    alembic upgrade  Exited (0)          ← 第二次跑时无 "Running upgrade"
opspilot-api-1     uvicorn          Up (healthy)
opspilot-worker-1  worker.main      Up                   ← worker.starting / worker.idle
opspilot-web-1     nginx            Up  0.0.0.0:8099->80/tcp
```

打的是 nginx 的 8099（本机 8088 被别的服务占了），链路是 浏览器 → nginx → `api:8080`：

```
GET  /healthz                    → 200  {"status":"ok","database":"ok"}
GET  /readyz                     → 200  {"status":"ready","database":"ok"}
GET  /                           → 200  <title>OpsPilot</title>
GET  /servers                    → 200  （SPA fallback）
POST /api/v1/auth/login          → 200  access_token + 31 个权限码
GET  /api/v1/auth/me             → 200  （带 Bearer）
GET  /api/v1/servers             → 200  []
GET  /api/v1/servers   （无 token）→ 401  {"code":"unauthenticated","message":"missing bearer token","request_id":…}
```

`scripts/bootstrap.py` 跑了两遍，第二遍必须是空操作：

```
第 1 遍：{"permissions_created": 31, "roles_created": 3, …, "event": "bootstrap.admin_created"}
第 2 遍：{"permissions_created": 0,  "roles_created": 0, …, "event": "bootstrap.admin_exists",
          "admin_created": false}
```

第二遍给的另一个密码**没有生效**：原密码仍 200，新密码 401 —— 「只在管理员不存在时创建，
绝不重置已存在账号的密码」是实测的，不是文档措辞。

api / worker 日志里 `grep -icE 'BEGIN [A-Z ]*PRIVATE|access_token|eyJhbGciOi|<刚才那个密码>'` → **0**。

#### 冒烟里抓到的两个坑（都修了，记在这里免得下次再撞）

1. **`npm ci` 对下载失败的 optional 依赖是静默跳过的。** Vite 8 的 rolldown 是按平台拆的
   optional 原生绑定，掉一个就变成 `Cannot find module '@rolldown/binding-linux-x64-gnu'` ——
   报错在打包器深处，离原因很远，看起来像 Dockerfile 写错了。现在 `npm ci` 带
   `--fetch-retries=5`，装完先 `import('rolldown')` 验一遍，缺了就当场说清楚。
2. **端口被占的症状是 `web` 停在 `Created`。** `docker inspect` 里才有
   `Bind for 0.0.0.0:8088 failed: port is already allocated`，`compose ps` 只写 `Created`。
   换 `OPSPILOT_QUICKSTART_PORT` 即可。

**另外，registry 是两个 build-arg，默认官方源。** 仓库不内置任何镜像：

```dotenv
#PIP_INDEX_URL=https://pypi.org/simple
#NPM_CONFIG_REGISTRY=https://registry.npmjs.org/
```

本次冒烟是在拉不动官方源的网络上做的，所以把这两个键指向了可达的镜像才把 build 跑完。
**默认值仍是官方源**，这一点没有因为「本机跑不动」而改。

#### 红线自查（交付前逐字跑过）

```bash
find . \( -path ./node_modules -o -path ./.venv -o -path ./frontend/node_modules \) -prune \
    -o \( -iname '*docker*' -o -iname 'Dockerfile*' -o -iname 'docker-compose*' \) -print
# → ./.dockerignore
#   ./Dockerfile
#   ./docker-compose.yml

find . \( -path ./node_modules -o -path ./.venv -o -path ./frontend/node_modules \) -prune \
    -o \( -iname '*k8s*' -o -iname '*kubernetes*' -o -name 'Chart.yaml' -o -name 'values.yaml' \) -print
# → （空）
```
