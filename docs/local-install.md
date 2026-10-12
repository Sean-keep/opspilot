# 本机安装

从零到「能登录、能看到壳」的完整步骤。本文在 **Ubuntu 24.04.4 LTS on WSL2**（systemd 已启用）
上实际跑通过。

## 前置清单

| 依赖 | 版本 | 检查 |
|---|---|---|
| OS | Ubuntu 22.04 / 24.04，Debian 12 | `lsb_release -ds` |
| systemd | 运行中 | `ps -p 1 -o comm=` |
| Python | 3.12 + venv | `python3.12 --version` |
| Node | 22 + npm | `node -v && npm -v` |
| MySQL | 8.0+ | `mysql --version` |
| git | 任意近期版本 | `git --version` |

WSL2 需要 systemd 启用（`/etc/wsl.conf` 里 `[boot] systemd=true`），否则
`systemctl enable --now mysql` 会失败。

## 1. MySQL

```bash
sudo bash scripts/install-mysql.sh
```

脚本做的事：

1. 预检（能否用 apt、systemctl 是否可用）
2. `apt-get install -y mysql-server`
3. `systemctl enable --now mysql`
4. **增量**创建 `opspilot`、`opspilot_test` 两个库和 `opspilot` 应用用户
   （已存在则跳过，不覆盖、不改密码）

它**拒绝**做的事（有显式检查）：

- 清空或重建已有的 datadir
- 重置 root 密码
- 删除已有数据库或用户
- 执行任何不可逆升级

密码由脚本生成，写在终端输出里；请立刻记下来。也可以自己指定。

## 2. 代码与虚拟环境

```bash
cd ~/Projects/opspilot
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
```

## 3. 配置

```bash
cp .env.example .env
chmod 600 .env
```

必填三项：

```bash
OPSPILOT_DB_PASSWORD=<上一步的密码>
OPSPILOT_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
OPSPILOT_MASTER_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
```

开发环境用 `OPSPILOT_MASTER_KEY` 内联即可。**生产环境必须**用文件形式：

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" \
  | sudo tee /etc/opspilot/master.key > /dev/null
sudo chmod 600 /etc/opspilot/master.key
# .env 里: OPSPILOT_MASTER_KEY_FILE=/etc/opspilot/master.key
```

进程会拒绝读取 mode 含 group/world 位的密钥文件 —— 这是硬要求，不是建议。

## 4. 迁移

```bash
. .venv/bin/activate
alembic upgrade head
alembic upgrade head    # 第二次必须是空操作（无 "Running upgrade"）
```

## 5. 引导账号

```bash
python scripts/bootstrap.py --admin-password '<至少 12 位，不在弱口令表里>'
```

- 权限与默认角色（admin / operator / viewer）是幂等种子，跑多次安全。
- 管理员只在不存在时创建。**脚本绝不重置已存在账号的密码。**
- 不给 `--admin-password` 也不给 `OPSPILOT_BOOTSTRAP_PASSWORD` 时，脚本拒绝自造默认密码。

## 6. 启动

```bash
bash scripts/dev-up.sh
```

一条命令后台起三个进程（uvicorn API、Worker、Vite dev），Ctrl-C 时一起收尸。也可以分开起：

```bash
. .venv/bin/activate
uvicorn backend.app.main:app --host 127.0.0.1 --port 8080   # 终端 1
python -m worker.main                                        # 终端 2
cd frontend && npm install && npm run dev                    # 终端 3
```

## 7. 验证

```bash
curl -sS http://127.0.0.1:8080/healthz
# {"status":"ok","env":"dev","time":"...","database":"ok"}

curl -sS http://127.0.0.1:8080/readyz
# {"status":"ready","database":"ok"}
```

浏览器打开 <http://127.0.0.1:5173>，用上一步的管理员账号登录，应看到运行状态页。

## 8. 测试

```bash
. .venv/bin/activate
export PYTHONPATH=$(pwd)
pytest -q backend/tests shared/tests
ruff check .
mypy backend worker shared

cd frontend
npm run lint && npm run typecheck && npm run test && npm run build
```

集成测试会**清空** `opspilot_test` 库的表。绝不要把 `OPSPILOT_TEST_DB_NAME` 指向生产库。

## 常见问题

**`systemctl` 报 not booted with systemd**
WSL 里没启用 systemd。改 `/etc/wsl.conf` 加 `[boot] systemd=true`，然后 `wsl --shutdown` 重启发行版。

**`alembic upgrade head` 说找不到 `OPSPILOT_SECRET_KEY`**
`.env` 没在当前工作目录，或者没激活 venv。迁移进程会读同一份 settings。

**登录一直 401**
先确认 `scripts/bootstrap.py` 真的建了账号（它不会重置已存在账号的密码 —— 若你之前用别的
密码建过，需要手动改）。再看 API 日志里的 `auth.login` 审计行。

**测试大量 skip**
MySQL 连不上。`mysqladmin ping` 看看服务在不在，`OPSPILOT_DB_PASSWORD` 对不对。测试 skip 而
不是 mock 绿，这是刻意的。

**前端 5173 打开但登录失败**
Vite 的代理把 `/api` 转发到 `http://127.0.0.1:8080`。若 API 不在 8080，设
`OPSPILOT_API_ORIGIN=http://127.0.0.1:<port>` 再起 dev server。
