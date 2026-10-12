# 模板协议

模板是 OpsPilot 里表达「要在目标机上做什么」的唯一方式。它不是一个自由脚本语言，
而是一个**受约束的声明式描述**：一串已注册 Action 的调用，参数经过校验，路径经过白名单。

这是整个项目最关键的安全边界之一。放开模板的表达能力 = 放开 RCE。

## 目录布局

```
templates/
├── runtime/          Python 运行时
├── web/              Nginx 等
├── application/      业务应用部署
├── database/         MySQL / Redis 等
└── middleware/       Supervisor 等
    └── <name>/
        ├── template.yaml
        ├── schemas/
        │   └── inputs.schema.json
        └── tests/
            ├── test_<name>_validate.py
            └── test_<name>_idempotency.py
```

目录名 = 模板的 `name`（kebab-case）。测试文件名带模板名：`nginx-site` 这种 kebab 名
永远组不成 Python 包路径，五个 `test_validate.py` 会让 pytest 撞模块名。

## template.yaml

```yaml
api_version: v1
name: python-app-runtime
version: 1.2.0
description: Provision a Python 3.12 virtualenv for a managed application.
inputs_schema: schemas/inputs.schema.json

steps:
  - key: check_os
    action: system.check_os
    params:
      expect: ["ubuntu-22.04", "ubuntu-24.04", "debian-12"]

  - key: ensure_user
    action: system.ensure_user
    params:
      username: "{{ inputs.app_user }}"
      shell: /usr/sbin/nologin

  - key: install_python
    action: python.ensure_runtime
    params:
      version: "{{ inputs.python_version }}"

  - key: create_venv
    action: file.render
    params:
      path: "{{ inputs.app_root }}/venv"
      kind: directory
      mode: "0750"
      owner: "{{ inputs.app_user }}"
```

`action` 字段**只能**是 Action Registry 里存在的名字。加载模板时校验，未注册 → 拒绝。

## 参数渲染：不是 Jinja

`{{ inputs.app_user }}` **不是**模板引擎表达式。实现方式是：找出 `{{ inputs.<name> }}`
和 `{{ server.<field> }}` 这两种点分标识符模式，**逐个替换**为已经过 Pydantic 校验的值。

以下任何形态一律**拒绝**（不是「原样保留」，是拒绝整个模板）：

| 形态 | 例子 |
|---|---|
| 管道 / 过滤器 | `{{ a \| b }}` |
| 函数调用 | `{{ f(x) }}` |
| 语句块 | `{% if %}`、`{# comment #}` |
| Shell 展开 | `${VAR}`、`$(cmd)`、`` `cmd` `` |

被替换的**值**本身还要按目标用途再过一层约束：

- 用户名走 `^[a-z_][a-z0-9_-]{0,31}$`
- 路径必须绝对路径，且前缀在白名单内（`/opt/opspilot/…`、`/etc/systemd/…` 等）
- 版本号、端口、包名各有独立模式

执行侧一律 `exec_command(argv=[...])` 列表形式，**从不** `shell=True`，**从不**把参数拼进
一个字符串。参数即使包含 `; rm -rf /` 也只是 argv 里的一个普通 token。

## 输入 Schema

`schemas/inputs.schema.json` 是标准 JSON Schema，同时用 Pydantic 模型校验。它既约束
API 端 `POST /tasks` 的入参，也在 Worker 侧重新校验一次（不信任 API 传来的快照）。

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["app_user", "app_root", "python_version"],
  "properties": {
    "app_user":    { "type": "string", "pattern": "^[a-z_][a-z0-9_-]{0,31}$" },
    "app_root":    { "type": "string", "pattern": "^/opt/opspilot/apps/[a-z0-9_-]+$" },
    "python_version": { "type": "string", "enum": ["3.11", "3.12"] }
  },
  "additionalProperties": false
}
```

## 版本生命周期

```
DRAFT ──► VALIDATED ──► TESTED ──► PUBLISHED ──► DEPRECATED
```

| 状态 | 含义 |
|---|---|
| `DRAFT` | 可编辑 |
| `VALIDATED` | 语法 + Schema + Action 注册表校验通过 |
| `TESTED` | 跑过 `tests/test_validate.py` 与 `test_idempotency.py` |
| `PUBLISHED` | **不可变**。有 `checksum`（内容 SHA-256），任务必须钉住具体版本 |
| `DEPRECATED` | 不再允许新建任务选用，历史任务保留 |

**已发布版本不可修改。** 要改就发新版本。`checksum` 在任务创建时钉进 `params_snapshot_json`，
执行前重新计算并比对；不一致 → 拒绝执行。**绝不自动跟随 `latest`** —— 生产禁用 `latest`，
必须指定明确版本号或 commit / tag。

## Action Registry

注册表是**进程内字典**，启动时由显式 `register_all()` 装载（不做隐式 import 副作用，避免
循环 import，也便于测试隔离）。

```python
@register("file.render", idempotent=True, idempotency_class="CONDITIONAL")
class FileRender(Action):
    params = FileRenderParams   # Pydantic，负责校验

    def run(self, ctx: ExecContext, p: FileRenderParams) -> ActionResult: ...
```

每个 Action 声明：

- `params`：Pydantic 模型，唯一的参数校验入口
- `safe`：是否只读（`system.check_*` 为 true）
- `idempotent` / `idempotency_class`：`SAFE` / `CONDITIONAL` / `DESTRUCTIVE`，
  决定租约过期后能否自动重跑（见 [state-machines.md](state-machines.md)）
- `requires_sudo`：是否需要受限 sudo 白名单

### V1 的 15 个动作

| 动作 | 用途 | 幂等 |
|---|---|---|
| `system.check_os` | 只读：发行版 / 版本核对 | SAFE |
| `system.check_disk` | 只读：磁盘余量核对 | SAFE |
| `system.ensure_user` | 确保系统用户存在 | CONDITIONAL |
| `package.install` | apt 安装软件包 | CONDITIONAL |
| `python.ensure_runtime` | 确保 Python 运行时 | CONDITIONAL |
| `file.render` | 原子写文件 / 建目录 | CONDITIONAL |
| `app.fetch_release` | 拉取制品并校验和 | CONDITIONAL |
| `app.install_release` | 解压到 `releases/<id>` | CONDITIONAL |
| `systemd.render_unit` | 生成 unit 文件 | CONDITIONAL |
| `systemd.restart` | 重启服务 | DESTRUCTIVE |
| `supervisor.restart` | 重启 supervisor 程序 | DESTRUCTIVE |
| `http.check` | 健康检查 HTTP | SAFE |
| `mysql.precheck` | 只读：MySQL 预检 | SAFE |
| `redis.precheck` | 只读：Redis 预检 | SAFE |
| `notification.telegram` | 发通知 | SAFE |

未注册的 action 一律拒绝。**V1 不允许 AI 或用户直接提交任意 Shell 命令并以 root 执行。**

## `file.render` 的原子性

1. 路径前缀白名单校验（防穿越）
2. 远端已存在 → 先备份到 `.bak.<ts>`
3. 写临时文件 + `fsync` + `rename`（原子替换）
4. `chown` / `chmod`，把 mode/owner/checksum 记进 `result_json`
5. 内容里的 `secret_ref` 只写值本身，**绝不落日志**

## secret_ref

模板参数里可以出现 `secret_ref: "cred:deploy-key"`，表示「这里要填某个凭据的值」。
渲染时把值填进去；**`params_snapshot_json` 里只存 `secret_ref` 本身**，不存解析出来的值。
日志经过 `shared/redaction.py` 递归脱敏。

## 四层拒绝

`shared/templates.py` 的校验分四层，**任一层不过就整份模板拒绝**，绝不「好的步骤先跑」。
四层分开是为了每层都能单独测，也是为了让操作员一次看到全部问题而不是第一个。

| 层 | 拒绝什么 | 实现 |
|---|---|---|
| 1 结构 | YAML 不合 `TemplateSpec`（`extra="forbid"`）、步骤 key 重复、`action` 未注册、`name` 与目录不一致 | `TemplateSpec.model_validate` + `spec_of()` |
| 2 绑定 | 参数名不在 action 的 `model_fields`；`{{ inputs.x }}` 的 `x` 未在 schema 声明；`{{ server.y }}` 的 `y` 不在 `SERVER_VALUE_KEYS` | `validate_template()` |
| 3 渲染 | `{{ a \| b }}`、`{{ f(x) }}`、`{{ a['b'] }}`、`{% %}`、`{# #}`、`${}`、`$(())`、反引号、不配对的 `{{` / `}}` | `validate_template_text()` |
| 4 干跑 | 用 schema 的 `default`/`const`/`examples`/`enum` 造探针值渲染，再交给 action 的 Pydantic 模型验一次 | `dry_run_params()` |

第 4 层是「模板能解析，但 action 会拒收这个值」的唯一防线：服务名白名单（`sshd` 不在
`opspilot-*` 里）、路径白名单、`release_id` 保留字都在这层炸。

## 读路径 vs 写路径

同一个参数名 `path`，规则取决于 action **声明的字段类型**，不是参数名本身：

| 类型 | 规则 | 例子 |
|---|---|---|
| `ReadPath` | 绝对、无 `..`、无控制字符 | `system.check_disk.path` 可以是 `/var/lib/mysql` |
| `RemotePath` | 上面全部 **+** 必须落在 `ALLOWED_PATH_PREFIXES` 里 | `file.render.path` 只能是 `/opt/opspilot/apps/…` 等 |

写白名单：

```
/opt/opspilot/apps/       /opt/opspilot/test/       /etc/systemd/system/
/etc/supervisor/conf.d/   /etc/nginx/
```

区分两者是因为探一个已存在的数据目录是合法需求，而把写规则套在读路径上会让
`mysql-precheck` 这类模板完全写不出来。

## 载荷只走 stdin

**argv 会被记录并持久化到 `result_json`，stdin 不会。** 所以文件内容、unit 正文、
curl 配置里带 token 这类东西**必须**从 stdin 进：

- `file.render` 用 `dd of=<tmp> status=none`，内容走 stdin。不用 `tee`：`tee` 会把内容
  打进 stdout，而 `ActionResult.from_exec` 会把 stdout 写进摘要。
- `systemd.render_unit` 先把正文 stage 到 `/opt/opspilot/test/`，再用受限的
  `sudo install` 装进 `/etc/systemd/system/`。
- `notification.telegram` 的 token 塞进 `curl --config -` 的 stdin，argv 里真的没有 token。
  `shared/redaction.py` 另有 `/bot<digits>:<token>/` 的模式脱敏兜底。

传输层只记 `stdin_bytes`（长度），不记内容。

## 提权白名单

`requires_sudo=True` 的动作一律走 `ensure_sudo_allowed(program)`，只放行：

```
/usr/bin/apt-get    /usr/sbin/useradd   /usr/sbin/usermod
/usr/bin/systemctl  /usr/bin/supervisorctl   /usr/sbin/nginx
/usr/bin/install
```

`chmod` / `chown` / `cp` / `mv` / `rm` / `dd` / `sh` / `bash` **刻意不在里面**。
`install` 是唯一的 root 写原语，而且只用来把 SSH 用户已经 stage 好的文件搬过去。

## `{{ server.* }}` 可读字段

`SERVER_VALUE_KEYS`（18 个，来自 `ServerOut` / `FactsOut`）：

```
id  name  hostname  port  ssh_user  description  status
os_name  os_version  kernel  arch
cpu_count  cpu_model  memory_total_mb  memory_available_mb
disk_total_gb  disk_available_gb
```

凭据字段（`ssh_password`、`credential`、`master_key`、`host_key_fingerprint` 等）**不在**里面，
引用它们会直接拒绝整份模板。不是「渲染成空」，是拒绝。

## 字面 `$` 的限制

严格渲染器拒绝 `${...}`、`$(...)`、`` `...` `` 三种形态，所以 **V1 模板里写不出 shell 展开**。
但 nginx 的 `$host` 是**允许**的：被拒绝的只是那三种形态，裸 `$name` 原样保留。

代价是：需要 `${VAR}` 这种 shell 展开的配置文件，V1 模板渲染不出来。这类文件请直接把
原文写进 `file.render` 的 `content`（不带 `${` 就行）。这是安全优先的取舍，不是遗漏。

## `release_id` 保留字

`app.fetch_release` / `app.install_release` 拒绝这些 `release_id`：

```
latest   current   shared   releases   ops
```

生产必须钉明确版本。「绝不自动跟随 `latest`」是硬要求，所以它在 action 里被拒，
而不是只靠 inputs schema 的 `pattern` 约束 —— schema 是给 API 入参用的，
action 才是最后一道闸。

## 模板自带的测试

每个模板的 `tests/` 不打任何远端主机，只证明两件事：

- `test_<name>_validate.py` —— 合法：能加载、四层零错误、干跑干净、checksum 只覆盖
  `template.yaml` + `schemas/*.json`（**不含** `tests/`）。
- `test_<name>_idempotency.py` —— 可重跑：声明了哪些幂等类别、重跑产生同样的 argv、
  配置是替换不是追加、载荷走 stdin。

共用夹具在 `templates/harness.py`（`RecordingPort` / `run_steps`）。
