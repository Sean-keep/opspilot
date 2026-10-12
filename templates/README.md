# 模板目录

这里存放 OpsPilot 的**声明式模板**：一串已注册 Action 的调用，参数经校验，路径经白名单。
它不是自由脚本语言。协议见 [../docs/template-protocol.md](../docs/template-protocol.md)。

## 分类

| 目录 | 放什么 |
|---|---|
| `runtime/` | Python 等语言运行时 |
| `web/` | Nginx 等 Web 服务器 |
| `application/` | 业务应用部署 |
| `database/` | MySQL / Redis 等 |
| `middleware/` | Supervisor 等 |

## 单个模板的布局

```
<category>/<name>/
├── template.yaml              # 步骤与 action 调用
├── schemas/
│   └── inputs.schema.json     # 入参 JSON Schema（同时用 Pydantic 校验）
└── tests/
    ├── test_validate.py       # 语法 + Schema + Action 注册表
    └── test_idempotency.py    # 重跑语义
```

## 状态

S0 只建立目录骨架。模板加载器、Action Registry 与示例模板在 **S3** 落地。

在此之前，这五个子目录为空是预期状态；`.gitkeep` 只是为了让 git 保留布局。
