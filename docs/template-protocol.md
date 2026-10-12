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
            ├── test_validate.py
            └── test_idempotency.py
```

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
