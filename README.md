# DWS Auto Reply

轻量的 macOS 钉钉个人消息自动回复守护进程。它使用 DWS 个人事件长连接监听私聊和群 `@`，通过受限的 Codex 子进程生成回复，并提供只绑定 localhost 的管理页面。

## 特性

- 私聊支持全部、仅白名单、关闭三种模式；黑白名单、延迟和每日上限可热更新。
- 群聊支持全部、仅白名单、关闭三种模式；只处理明确 `@`，可配置延迟和表情回应。
- Codex 模型、推理强度、身份、自我介绍、Personality Prompt、自定义 System Prompt 和 AI 后缀可热更新。
- localhost 控制台支持暂停/恢复、真实发送门禁、优雅重启、状态、最近任务和日志。
- 可配置一个本地 Git 仓库及目录白名单。默认只读当前工作树和已有 refs；消息明确指定分支时可按需 fetch，再用 `git show` / `git grep` 读取，不执行 checkout。
- SQLite 只保存任务、计数、幂等和短期防重复元数据，不保存完整聊天正文。
- 单进程、标准库 HTTP 页面、无前端框架和额外常驻服务。

## 安全边界

- 默认配置关闭真实发送；从控制台开启或扩大范围必须输入确认词。
- 可配置 Prompt 不能覆盖内置安全约束。
- 代码仓库不会被修改，不执行 checkout，不连接数据库。
- 凭据、密钥、本地路径、源码片段和受限实现细节不会向普通联系人输出。
- 外部 URL 拦截本机和内网地址；钉钉文档只通过 DWS 读取。
- 控制台只绑定 `127.0.0.1`。

## 运行前提

- macOS 与 zsh。
- `uv`，用于创建 Python 虚拟环境和安装依赖。
- `codex` CLI，已完成本机认证。
- `dws` CLI 1.0.15 或更高版本，且当前分发已开通本项目需要的个人事件流、聊天消息和文档读取能力。

本仓库不分发 DWS。请通过你有权使用的正式渠道安装，不要从不可信来源下载，也不要把 DWS token、profile、事件键或用户 ID 提交到 Git。

## 准备 DWS

先确认 CLI 可用并完成钉钉 OAuth 登录：

```bash
dws version --format json
dws auth login --format json
dws auth status --format json
dws doctor --json --format json
```

本机登录会打开浏览器完成授权；SSH、容器或无浏览器环境使用 `dws auth login --device --format json`。认证过期或返回 `AUTH_TOKEN_EXPIRED`、`USER_TOKEN_ILLEGAL` 时，重新执行登录命令。

本项目依赖的 DWS 能力并非所有企业或分发都默认开放。至少检查以下命令的帮助是否存在：

```bash
dws event consume --help --format json
dws chat message reply --help --format json
dws chat message search-advanced --help --format json
dws doc read --help --format json
```

如果 `dws event consume --help` 报 `unknown command`，或者只回到 DWS 根帮助，说明当前 DWS 没有个人事件流能力，本项目不能启动监听。请从你的 DWS 管理方取得已开通该能力的合法分发或配置；不要猜测事件键。

当前用户信息可通过下列只读命令查询，用于填写 `identity.user_id` 和 `identity.open_dingtalk_id`：

```bash
dws contact user get-self --format json
```

`dws.profile` 与三个事件键由实际 DWS 部署或管理员提供，不等同于钉钉昵称，也无法由本仓库推导。`dws.config_dir` 默认可使用 `~/.config/dws`；如果你的 DWS 使用其他配置目录，请填写实际路径。

## 初始化项目

```bash
./scripts/init.sh
```

脚本会从 `config.example.yaml` 创建权限为 `0600` 的本地 `config.yaml`，并执行 `uv sync`。然后编辑 `config.yaml`，至少替换这些占位内容：

```yaml
identity:
  user_id: "YOUR_DINGTALK_USER_ID"
  open_dingtalk_id: "YOUR_OPEN_DINGTALK_ID"

codex:
  model: "YOUR_CODEX_MODEL"

dws:
  config_dir: "~/.config/dws"
  profile: "YOUR_DWS_PROFILE"
  event_keys:
    private: "YOUR_DWS_PRIVATE_EVENT_KEY"
    at: "YOUR_DWS_AT_EVENT_KEY"
    group: "YOUR_DWS_GROUP_EVENT_KEY"
```

还需要按使用场景配置身份与 Prompt、本地代码仓库路径、允许读取的目录，以及私聊/群聊白名单。真实配置只应保存在已被 `.gitignore` 覆盖的 `config.yaml`。

## 启动前验证

先验证 YAML 与安全约束，再验证 DWS consumer 能否登录并进入 ready 状态。smoke test 不读取消息正文：

```bash
.venv/bin/python -m app.main --check
.venv/bin/python scripts/listener_smoke.py --duration 8
```

两项均通过后前台启动：

```bash
.venv/bin/python -m app.main
```

控制台：<http://127.0.0.1:8765>。默认配置关闭真实发送；需要发送时，必须在 localhost 控制台中显式确认并开启发送门禁。

## 配置热更新

下列配置保存后通常在两秒内生效：

- 私聊/群聊模式、黑白名单、延迟、每日上限
- 发送门禁与暂停状态
- Codex 模型、推理强度、身份、Personality、自定义 System Prompt 和后缀
- 仓库路径、目录白名单和按需 fetch 开关

监听目标变化会自动重连对应 DWS consumer。程序代码或 Python 环境变化需要优雅重启。

## macOS launchd

管理脚本会复用项目根目录已有的机器专用 plist；开源安装时则从 `launchd.plist.example` 生成本机 plist：

```bash
./scripts/manage.sh install
./scripts/manage.sh status
./scripts/manage.sh logs 100
./scripts/manage.sh restart
```

登录 macOS 后自动启动；异常退出和控制台发起的重启会由 launchd 拉起。DWS consumer 通过关闭 stdin 和 SIGTERM 优雅停止，不使用 SIGKILL。

消息断连补偿使用周期性只读历史扫描，不读取 DWS 的本地日志目录。真实事件键只保存在被 Git 忽略的 `config.yaml` 中；仓库示例仅提供占位符。

## 测试

```bash
.venv/bin/python -m unittest discover -s tests -v
uvx ruff check app tests
```

## 常见启动问题

- `dws: command not found`：DWS 未安装或不在 `PATH`；通过正式渠道安装后重新运行 `dws version --format json`。
- DWS 认证失败：运行 `dws auth status --format json` 和 `dws doctor --json --format json`；token 失效时重新登录。
- `event consume` 不存在：当前 DWS 分发未开放个人事件流能力，需要联系 DWS 管理方，不能通过修改本项目绕过。
- `--check` 失败：检查 YAML 缩进、占位符、本地路径和发送门禁组合。
- smoke test 未 ready：核对 `dws.config_dir`、`dws.profile`、三个事件键及账号权限；必要时给 smoke test 加 `--verbose` 查看诊断。
- 管理页能打开但不回复：先确认 listener ready，再检查暂停状态、私聊/群聊模式、黑白名单、每日上限和发送门禁。
- 提交 issue 或日志前请脱敏；不要粘贴 token、profile、事件键、用户/群 ID、聊天正文或本地绝对路径。

## 本地文件

以下文件已加入 `.gitignore`，发布前不应提交：

- `config.yaml` 与备份
- `.dws/`
- `state.sqlite3*`
- `logs/`、`runtime/`
- 机器专用 `com.*.plist`

仓库中的 `config.example.yaml` 和 `launchd.plist.example` 不含个人 ID、群 ID、profile 或绝对用户路径。
