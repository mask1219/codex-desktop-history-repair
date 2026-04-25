# Codex Desktop History Repair

一个用于修复和保障 Codex 桌面端历史对话不丢失的项目骨架，当前主路径已经收敛为对真实 `Codex.app` 的最小补丁。

核心目标已收敛为：
- 使用 Codex 桌面端原生 UI，不切到额外控制台
- 用 `ccswitch` 切换第三方 API 后，App 内仍能看到全部历史对话
- 更换 Codex 登录账号后，App 内仍能看到全部历史对话
- 打开旧对话后可以继续发送消息

## 当前范围

本项目先聚焦以下问题：
- 切换第三方 API 后历史线程仍可见、可继续对话
- 切换官方账号后历史线程仍可见、可继续对话
- 升级 Codex 桌面端后可自动重打补丁，维持历史列表读取逻辑
- 流式中断、崩溃、断网后最后一轮不会消失

## 核心原则

1. 本地历史是唯一真相。
2. provider、账号、`previous_response_id` 只是路由元数据。
3. 任何失败只能产生 `partial` 或 `failed` 记录，不能导致历史整段消失。
4. 用户必须知道哪些内容只保存在本地，哪些内容会发送给当前 provider。

## 当前可用能力

当前仓库保留底层 engine 和桌面端 adapter 雏形，但主路径不是重建一套历史系统。主路径是给真实 Codex.app 打开内置的 `persistExtendedHistory` 开关，并让最近线程列表优先读本地 state db，让历史继续留在 App 自己的体验里。

- Codex.app 打包补丁：普通新线程和恢复线程默认 `persistExtendedHistory: true`
- Codex.app 打包补丁：最近线程列表、项目入口最近线程列表、全部线程列表都优先走 state db
- Codex.app 打包补丁：对需要展示跨 provider 历史的 `thread/list`，显式传 `modelProviders: []`
- 已验证当前 Codex Desktop / app-server 语义下，`modelProviders: null` 只返回当前 provider，不会返回全部 provider
- 2026-04-25 已在真实 Codex.app 验证：当前项目不再只显示 3 条，`custom / sub2api / openai` 历史可同时显示
- 只读检查：确认当前 App 是否已经打开补丁
- 自动重打补丁：通过 LaunchAgent 在 Codex.app 更新后重新应用补丁
- SQLite schema 初始化与迁移
- 线程/消息/路由/摘要的本地持久化
- `remote_chain / local_rebuild / summary_rebuild` 续聊策略
- 流式增量写盘与中断恢复收敛
- JSONL 导出 / 导入恢复（冲突导入为线程副本）
- CLI：`init / recover / export / import / list / show / send`
- `Responses API` 兼容 provider 客户端（SSE / JSON 响应）
- 宿主接线入口：`DesktopSessionHost.startup()` 自动执行 migration + recover

`DesktopSessionHost.list_threads()` 额外返回面向 UI 的字段：
- `latest_route`（provider/account/model）
- `send_available` / `send_unavailable_reason`
- `last_continuation_error`
- `last_message_role` / `last_message_status` / `last_message_preview`

## App 内最小方案

先检查当前 Codex.app 是否已经打开扩展历史：

```bash
node scripts/patch_codex_app_extended_history.js /Applications/Codex.app --check
```

如果输出 `needsPatch: true`，执行补丁：

```bash
node scripts/patch_codex_app_extended_history.js /Applications/Codex.app
```

为了避免 Codex.app 自动更新后补丁被覆盖，安装自动重打补丁的 LaunchAgent：

```bash
sh scripts/install_codex_app_patch_launch_agent.sh /Applications/Codex.app
```

这条路径的用户体验目标是：
- 不新增额外界面
- 不要求用户理解 provider / route / SQLite
- 切换 `ccswitch`、第三方 API 或登录账号后，Codex.app 原生历史列表仍显示历史
- 从历史列表打开旧对话后，可以继续发送消息

当前补丁脚本已经内置一个关键兼容处理：
- 在当前 Codex Desktop / app-server 版本里，`thread/list` 只有在 `modelProviders: []` 时才会返回全部 provider 历史；传 `null` 只会拿到当前 provider 的线程

## Engine / CLI 备用方案

CLI 只作为底层 engine 的调试、导出导入能力保留。它不是当前核心需求的默认体验，也不提供本地网页控制台。

桌面端也可以直接创建 adapter 并执行 `startup()`，后续线程列表、详情和发送都通过同一个 adapter 访问本地历史库：

```python
from history_repair import DesktopHistoryAdapter, DesktopProviderConfig

adapter = DesktopHistoryAdapter("/path/to/history.db")
startup = adapter.startup()
threads = adapter.list_threads()
detail = adapter.get_thread("thread-id")

send_result = adapter.send_text_message(
    thread_id="thread-id",
    message="继续",
    provider_config=DesktopProviderConfig(
        provider="provider-a",
        account_id="acc-1",
        model="gpt-5.4",
        base_url="https://api.example.com/v1",
        api_key="sk-xxx",
    ),
)
```

底层 adapter 接入要求：
- 应用启动早期必须调用 `startup()`，它会执行 migration 和流式中断恢复。
- 线程列表和详情页使用 `list_threads()` / `get_thread()` 的返回值，不再依赖远端历史查询决定是否展示。
- provider、账号或模型切换时保持同一个 `thread_id`，只替换 `DesktopProviderConfig`。
- 当返回 `send_available=false` 或 `read_only_mode=true` 时，UI 需要禁用发送并展示 `send_unavailable_reason` 或错误信息。
- 外部桌面端更新时不得删除或重建 `$APP_DATA/history-repair/history.db`，也不要把数据库放进应用安装目录。

## CLI 快速使用

先在项目根目录安装本地包：

```bash
python3 -m pip install -e .
```

```bash
python3 -m history_repair init --db /tmp/history.db
python3 -m history_repair recover --db /tmp/history.db --pending-timeout-ms 60000
python3 -m history_repair export --db /tmp/history.db --output /tmp/history.jsonl
python3 -m history_repair import --db /tmp/history.db --input /tmp/history.jsonl
python3 -m history_repair list --db /tmp/history.db
python3 -m history_repair show --db /tmp/history.db --thread-id t1
python3 -m history_repair send --db /tmp/history.db --thread-id t1 --message "继续" --provider provider-a --account-id acc-1 --model gpt-5.4 --base-url https://api.example.com/v1 --api-key sk-xxx
# 或者通过环境变量提供 API Key（默认读取 HISTORY_REPAIR_API_KEY）
HISTORY_REPAIR_API_KEY=sk-xxx python3 -m history_repair send --db /tmp/history.db --thread-id t1 --message "继续" --provider provider-a --account-id acc-1 --model gpt-5.4 --base-url https://api.example.com/v1
```

也可以使用安装后的命令：

```bash
history-repair list --db /tmp/history.db
```
