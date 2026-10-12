# 模板目录

这里存放 OpsPilot 的**声明式模板**：一串已注册 Action 的调用，参数经校验，路径经白名单。
它不是自由脚本语言。协议见 [../docs/template-protocol.md](../docs/template-protocol.md)。

## 分类

| 目录 | 放什么 | 示例 |
|---|---|---|
| `runtime/` | Python 等语言运行时 | `python312` |
| `web/` | Nginx 等 Web 服务器 | `nginx-site` |
| `application/` | 业务应用部署 | `python-app` |
| `database/` | MySQL / Redis 等 | `mysql-precheck` |
| `middleware/` | Supervisor 等 | `supervisor-program` |

## 单个模板的布局

```
<category>/<name>/
├── template.yaml                       # 步骤与 action 调用
├── schemas/
│   └── inputs.schema.json              # 入参 JSON Schema（同时用 Pydantic 校验）
└── tests/
    ├── test_<name>_validate.py         # 语法 + Schema + Action 注册表
    └── test_<name>_idempotency.py      # 重跑语义
```

目录名就是模板的 `name`（kebab-case，与 `template.yaml` 里的 `name:` 必须一致）。

**测试文件名必须带模板名。** 模板名是 kebab-case（`nginx-site`），永远不可能组成 Python
包路径，所以五个都叫 `test_validate.py` 会让 pytest 撞模块名。文件名带模板名之后，失败
输出里一眼能看出是哪个模板。

## 每个模板自带的两类测试

`tests/` 里的测试不打任何远端主机。它们证明的是「这份模板**合法**、**可重跑**」，
不是「它在真机上做对了事」——后者是 `shared/tests/test_actions.py` 与 SSH 测试目标的职责。

- `test_<name>_validate.py` —— 能加载、四层校验零错误、干跑干净、checksum 只覆盖
  `template.yaml` + `schemas/*.json`、写路径都在白名单里。
- `test_<name>_idempotency.py` —— 声明了哪些幂等类别、重跑产生同样的 argv、
  配置是替换不是追加、载荷走 stdin 不进 argv。

共用夹具在 [harness.py](harness.py)：`RecordingPort` 记录 argv 与 stdin 长度，
`run_steps()` 按 schema 默认值渲染并执行每一步。

## 状态

```
DRAFT ──► VALIDATED ──► TESTED ──► PUBLISHED ──► DEPRECATED
```

只有 `DRAFT` 可改。离开 `DRAFT` 正文即冻结；`PUBLISHED` 只能变 `DEPRECATED`。
导入目录用 `CatalogService.import_tree()`，已冻结版本遇到改过的文件会**拒绝**而不是
静默替换 —— checksum 就是用来抓这个的。

## 已交付的示例

| 模板 | 步骤 | 幂等类别 |
|---|---|---|
| `runtime/python312` | 检查 OS/磁盘、建用户、装解释器、建 releases/shared 目录 | 全 SAFE / CONDITIONAL |
| `web/nginx-site` | 建用户、建 docroot、渲染 server block、健康检查 | 全 SAFE / CONDITIONAL |
| `application/python-app` | 拉制品并校验、解压、写 env、写 unit、重启、健康检查 | 含 1 个 DESTRUCTIVE（重启） |
| `database/mysql-precheck` | 只读探 MySQL / Redis 与 datadir 余量 | 全 SAFE，无 sudo |
| `middleware/supervisor-program` | 建日志目录、渲染 program 配置、重启 | 含 1 个 DESTRUCTIVE（重启） |

`mysql-precheck` 是「绝不初始化已有数据目录」这条硬约束的可执行表达：它只探不写，
且 `expect_existing_datadir` 是模板里写死的，调用方**不能**通过入参打开初始化。

## `system.check_os` 的 `expect` 取什么值

模板里的 `expect: ["ubuntu-22.04", "ubuntu-24.04", "debian-12"]` 匹配的**不是**
`/etc/os-release` 的原文，而是从里面解析出的平台标识 `<ID>-<VERSION_ID>`（或只有 `<ID>`）。
拿原文做子串匹配是匹配不上的 —— `ubuntu-24.04` 不是 os-release 任何一行的子串，
那条路在真实机器上每次都会失败。词汇表见 [../docs/deployment.md](../docs/deployment.md)。

## 这里没有「发布计划」

S4 的应用发布链路（§8.2 那 11 步、软链接原子切换、失败恢复）**不是**一份模板，而是
`shared/deploy_plan.py` 里写死的计划 —— 它的顺序、不归零点和恢复策略是安全边界，
不能靠一份可编辑的 YAML 表达。`application/python-app` 是把同类步骤当**模板**用的示例，
两条路都调同一批 Action，但只有前者会被发布记录驱动。见
[../docs/deployment.md](../docs/deployment.md)。
