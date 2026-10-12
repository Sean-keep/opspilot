# 应用发布（S4）

一份发布 = 一个**钉死的版本**，发到一个**应用环境**上的一组**服务器**。从提交到线上服务
换版本，中间是一串固定的步骤；哪一步失败、线上会停在哪个版本，是这一页要讲清楚的事。

## 目录布局

每台目标机上：

```
/opt/opspilot/apps/<app>/
  releases/<release_id>/     只追加；切换之后也不删
  shared/config/             环境文件（含密文，只在目标机上）
  shared/logs/
  shared/data/               永不自动清理
  current -> releases/<id>   唯一的软链接
```

`releases` / `shared` / `current` 是布局自己占的名字，所以应用名不能叫这三个
（`RESERVED_APP_NAMES`）。应用名同时是目录段：小写、字母开头、`[a-z0-9-]`，长度 ≤ 64。

## 发布计划（§8.2）

计划在**提交时**就用 `shared/deploy_plan.build_deploy_plan()` 组装并校验一次：组装不出来的
提交不会被记成一条发布。Worker 再用同一份输入重建同一份计划 —— **批下来的，就是跑的**。

| # | 步骤 key | 动作 | 说明 |
|---|---|---|---|
| 1 | `check_os` | `system.check_os` | 只读；发行版不符直接停 |
| 2 | `check_disk` | `system.check_disk` | 只读；余量不足直接停 |
| 3 | `ensure_user` | `system.ensure_user` | 仅当 `run_user` 非空 |
| 4 | `ensure_runtime` | `python.ensure_runtime` | 仅当 `python_version` 非空 |
| 5 | `fetch_release` | `app.fetch_release` | 拉制品，**校验 SHA-256**，不符不落盘 |
| 6 | `install_release` | `app.install_release` | 解压到 `releases/<id>`，不碰 `shared/` |
| 7 | `render_config` | `file.render` | 写 `shared/config/app.env` |
| 8 | `render_unit` | `systemd.render_unit` | 写 unit、`daemon-reload`；**路径写的是 `current`，不是具体 release** |
| 9 | **`switch_current`** | **`app.switch_current`** | **不归零点** |
| 10 | `restart_service` | `systemd.restart` | 重启服务单元 |
| 11 | `check_health` | `http.check` | 按 `health_url` 打一枪，状态码不符即失败 |

`release_id` / `release_version` 在所有地方都拒绝保留字：`latest`、`current`、`shared`、
`releases`、`ops`。§8.2 写的「生产禁用 latest」是这条规则的子集 —— 代码在**每个**环境都拒，
因为一条写着 `latest` 的发布记录明天就答不出「当时线上是哪一版」。

## 不归零点与恢复策略

`switch_current` 是唯一翻转线上状态的一步，也是**不归零点**：

| 失败发生在 | 线上版本 | 行为 | 发布记录状态 |
|---|---|---|---|
| 切换**之前**（步骤 1–8） | 没被动过 | 停批，不恢复 | `FAILED` |
| 切换**这一步或之后**（9–11） | 未知或已是新版 | 跑 `build_restore_plan()` 把旧版本放回去 | `ROLLED_BACK` |
| 切换之后，且**没有上一个版本**（首次发布） | 已是新版 | 不假装没事，记下「链路已翻且无版本可放回」 | `FAILED` |
| 恢复本身也失败 | 未知 | 记录两次失败的原因 | `FAILED` |
| 用户取消 | — | 停后续步骤，**不自动回滚** | `CANCELLED` |

`ROLLED_BACK` 不是 `FAILED` 的委婉说法：它是「这次失败了，**并且**旧版本已经放回去了」。
首次发布失败后没有旧版本可放回，所以只能是 `FAILED` —— 报告里必须看得出差别。

恢复计划只有两步：`restore_current`（再切一次链接）+ `restore_restart`（重启服务）。
切换动作本身是幂等的：`readlink(current)` 已经指向目标就直接跳过（`changed=False`），
不会先 `rm` 再 `ln`。

## `app.switch_current` 的原子切换

第 16 个动作，也是唯一一个不在文档 §7.4 那 15 个里的：

```sh
ln -s -- releases/<id>  <app_root>/.current.opspilot-new
mv -Tf -- <app_root>/.current.opspilot-new  <app_root>/current
```

- **`-T` 是承重的。** 没有它，`mv` 会把新链接移进 `current/` 目录里面，而不是替换 `current`。
- **绝不 `rm` 旧链接再 `ln`。** 那中间有一个窗口，`current` 是不存在的；进程在此刻重启会直接找不到代码。
- 目标 release 不存在就拒绝，**并且不动链接**。
- 已经指向目标 → 跳过（`changed=False`）。

unit 文件写的是 `WorkingDirectory={app_root}/current`、`ExecStart={app_root}/current/venv/bin/python`，
是**版本无关**的，所以在链接翻转之前写它是安全的。

## 密钥怎么走

任务的 `params` 里只有 `config_ref`（放在顶层，与 `deploy` 平级），**没有值**：

```json
{
  "deployment_id": 12,
  "config_ref": "cred:77",
  "deploy": { "...": "不含 secrets、不含 config_ref" }
}
```

Worker 在执行时 `SELECT payload_encrypted FROM ops_credentials WHERE secret_ref = :ref`
→ 解密 → `json.loads` → `DeployInputs.secrets`，和 SSH 私钥走同一条「运行时才解析」的路。
因此 API 进程的内存里、任务行里、步骤参数里、日志里都不出现明文配置值。写进目标机的
`shared/config/app.env` 是唯一落地点。

回滚会重写 env 文件（`APP_RELEASE` 变了），所以它从被撤销那条发布自己的任务参数里继承
`config` / `module_name` / `app_port` / `python_version` / `requirements` —— 否则一次回滚
会把操作员填过的非密文配置一起抹掉。密文仍然只从 `config_ref` 解析，不继承。

### env 文件里每个键只写一次

`app.env` 里有四个键来自**类型化的发布输入**，也就是计划和健康检查各自使用的那一份值：

| 键 | 来源 |
|---|---|
| `APP_ROOT` | `DeployInputs.app_root` |
| `APP_RELEASE` | `DeployInputs.release_id` |
| `APP_PORT` | `DeployInputs.app_port` |
| `APP_USER` | `DeployInputs.run_user` |

`config` / `secrets` 里再出现同名键**直接拒绝**，不写第二行，也不静默二选一。
`EnvironmentFile` 是后写覆盖前写，所以重复行等于悄悄改端口：`app_port: 8888` +
`config: {"APP_PORT": "9999"}` 会部署出一个听 9999 的进程，而健康检查还在探 8888。
两头都是「操作员填的」，谁赢都不对 —— 只有拒绝才不会让计划和事实分家。

## 环境互斥（§10）

同一个应用环境同时只允许一条未终结的发布：`PENDING` / `RUNNING` 期间再来一条会被 **409**
拒绝，而不是排队。发布结束（`SUCCEEDED` / `FAILED` / `ROLLED_BACK` / `CANCELLED`）后锁自然放开。

这条检查在并发下是**建议性**的：两个请求同时通过检查就会都插进去。Worker 侧的逐步执行
仍然是最后一道防线，不要把它当成唯一的一道。

## `system.check_os` 的平台词汇表

`expect` 匹配的**不是** `/etc/os-release` 的原文，而是从里面解析出的平台标识：

| 形态 | 例子 | 来源 |
|---|---|---|
| `<ID>-<VERSION_ID>` | `ubuntu-24.04`、`debian-12` | `ID=ubuntu` + `VERSION_ID=24.04` |
| `<ID>` | `ubuntu`、`debian` | 只有 `ID` 时 |

示例模板与发布计划都用 `["ubuntu-22.04", "ubuntu-24.04", "debian-12"]`。早期实现拿
`any(token in stdout for token in expect)` 去匹配原文，而 `ubuntu-24.04` **不是**
`/etc/os-release` 任何一行的子串，于是每次真实检查都会失败。现在解析后比对标识，
模板与发布共用同一条规则。

## API

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| `POST` | `/api/v1/applications` | `app.create` | 建应用 |
| `PATCH` | `/api/v1/applications/{id}` | `app.update` | 改定义（**不能改名**） |
| `POST` | `/api/v1/applications/{id}/environments` | `app.update` | 加环境 |
| `PATCH` | `/api/v1/applications/{id}/environments/{env_id}` | `app.update` | 改环境（**不能改名**） |
| `POST` | `/api/v1/applications/{id}/deploy` | `deployment.create` | 提交发布 → **202** + `Location` |
| `GET` | `/api/v1/applications/{id}/deployments` | `deployment.read` | 该应用的发布历史 |
| `GET` | `/api/v1/deployments[?application_id=]` | `deployment.read` | 发布历史 |
| `GET` | `/api/v1/deployments/{id}` | `deployment.read` | 单条记录 |
| `POST` | `/api/v1/deployments/{id}/rollback` | `deployment.rollback` | 回滚 → **202** + `Location` |

提交与回滚都接受 `Idempotency-Key`：重复提交返回**原**发布记录，不排第二条。
回滚必须带 `confirm: true` —— 这是后端的拒绝条件，不是前端的按钮显隐。

## 前端

- **应用**（`/applications`）：应用定义、环境（服务单元 / 健康检查 / 配置引用 / 保留版本）。
  只描述「跑什么、放哪」，不下发任何命令。
- **发布记录**（`/deployments`）：历史、新建发布、回滚。目标服务器多选；非密文环境变量是
  key/value 行；**没有密文输入框**，因为密文从环境的 `config_ref` 解析。
- **任务详情**（`/tasks/{id}`，S2）：每台服务器、每个步骤的状态 + 历史日志。发布提交后
  自动跳到这里。

## 相关测试

| 位置 | 锁死了什么 |
|---|---|
| `shared/tests/test_deploy_plan.py` | 计划顺序、`switch_index`、恢复计划的形状、`inputs_from_mapping` 拒绝未知字段 |
| `shared/tests/test_actions.py` | `app.switch_current` 的 argv 形状（`ln` + `mv -Tf`，永不 `rm` live link）、幂等跳过、`check_os` 平台词汇表 |
| `backend/tests/test_applications_api.py` | 名字规范化与保留名、unit 路径、服务白名单、SSRF、不能改名、无 DELETE |
| `backend/tests/test_deployments_api.py` | 钉版本、校验和、SSRF、环境互斥、`previous_release_id` 的推导、密钥不进任务行、回滚二次确认、幂等回放 |
| `backend/tests/test_worker_deploy.py` | 切换前失败停批、切换后失败放回旧版本、首次发布失败无处可放、密文只进 env 文件、切换幂等 |
