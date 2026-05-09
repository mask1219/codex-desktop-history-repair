# Codex Desktop History Repair

一个用于修复 Codex 桌面端历史线程可见性与可续聊性的本地工具集。

当前主路径不再依赖修改真实 `Codex.app` 包体。仓库已移除补丁脚本，主方案改为参考 `codex-provider-sync` 的思路，直接同步 `~/.codex` 下的 provider 元数据、rollout 文件、项目缓存和 `state_5.sqlite`，让切换 provider 或账号后历史列表重新对齐。

## 现在解决什么问题

- 切换第三方 API 后，历史线程因为 `model_provider` 不一致而不显示
- `sessions/`、`archived_sessions/`、`state_5.sqlite` 之间元数据漂移
- `.codex-global-state.json` 项目缓存缺失导致历史所在项目不出现在桌面端列表
- 历史存在但排在最近 50 条之外，需要诊断首屏可见性
- 配置文件当前 provider 已切换，但旧线程仍绑在旧 provider 上
- 线程包含 `encrypted_content` 时，跨 provider / 账号后可能只能显示，不能可靠续聊
- 流式中断、远端链路失效后，仍需要本地历史引擎兜底继续对话

## 当前主能力

- `provider-status`
  检查 `config.toml`、rollout 文件、项目缓存、`state_5.sqlite` 之间的 provider / `cwd` / 归档状态是否一致，并输出项目是否落在最近 50 条内、是否存在 `encrypted_content`
- `provider-sync`
  批量把 rollout 文件、项目缓存和 `state_5.sqlite` 同步到目标 provider，可选同步 `cwd`，遇到锁住的 rollout 会跳过并报告
- `provider-switch`
  校验目标 provider 已在 `config.toml` 声明，先改当前 provider，再同步历史元数据
- `provider-autosync`
  持续观察当前 `config.toml` provider，发现外部工具切换 API 后自动同步历史元数据
- `provider-autosync-install`
  安装 macOS LaunchAgent，让开机 / 登录后后台常驻自动同步，无需人工运行命令
- `provider-restore`
  从自动备份恢复 `config.toml`、`.codex-global-state.json`、`state_5.sqlite` 和被改动的 rollout 文件，可按类别跳过
- `provider-prune-backups`
  清理旧备份

同时保留原有本地历史引擎能力：

- SQLite schema 初始化与迁移
- 线程 / 消息 / 路由 / 摘要持久化
- `remote_chain / local_rebuild / summary_rebuild` 续聊策略
- 流式增量写盘与中断恢复
- JSONL 导出 / 导入恢复
- CLI：`init / recover / export / import / list / show / send`

## 设计原则

1. 本地历史是唯一真相。
2. provider、账号、`previous_response_id` 只是路由元数据。
3. 展示问题优先通过同步本地 metadata 解决，而不是修改 App 包体。
4. 同步 provider / `cwd` 不改 `updated_at`，避免人为改变桌面端最近列表排序。
5. 任何失败只能产生 `partial` 或 `failed` 记录，不能导致历史整段消失。

## 快速使用

先安装本地包：

```bash
python3 -m pip install -e .
```

查看当前 provider 与历史元数据偏差：

```bash
python3 -m history_repair provider-status
```

只预览把历史同步到当前 `config.toml` provider 会改什么：

```bash
python3 -m history_repair provider-sync --dry-run
```

实际同步到当前 provider：

```bash
python3 -m history_repair provider-sync
```

常驻自动同步。适合你用 Codex App 或其他工具切 API，工具会按当前 `config.toml` provider 自动追平历史：

```bash
python3 -m history_repair provider-autosync --quiet
```

安装后台自动同步。之后每次登录 macOS 后会常驻检测当前 `config.toml` provider，并自动把历史同步到当前 API：

```bash
python3 -m history_repair provider-autosync-install
```

查看后台同步状态：

```bash
python3 -m history_repair provider-autosync-status
```

卸载后台同步：

```bash
python3 -m history_repair provider-autosync-uninstall
```

如果你希望它根据最新会话实际写入的 provider，主动反写 `config.toml`，再同步历史：

```bash
python3 -m history_repair provider-autosync --switch-provider --once
```

也可以把这种模式装成后台服务，但一般只建议你明确需要“根据最新历史反向改 `config.toml`”时使用：

```bash
python3 -m history_repair provider-autosync-install --switch-provider
```

也可以显式指定目标 provider：

```bash
python3 -m history_repair provider-autosync --provider sub2api --switch-provider --once
```

只检查一次，适合放到切换 API 后的脚本里：

```bash
python3 -m history_repair provider-autosync --once
```

切换到一个新 provider，并同步历史：

```bash
python3 -m history_repair provider-switch --provider sub2api
```

如果还要同时切模型：

```bash
python3 -m history_repair provider-switch --provider sub2api --model gpt-5.5
```

只同步某几个线程：

```bash
python3 -m history_repair provider-sync --provider sub2api --thread-id thread-1 --thread-id thread-2
```

从备份恢复：

```bash
python3 -m history_repair provider-restore --backup-dir ~/.codex/backups_provider_sync/20260508231031
```

只恢复数据库和 rollout，不恢复配置与项目缓存：

```bash
python3 -m history_repair provider-restore --backup-dir ~/.codex/backups_provider_sync/20260508231031 --no-config --no-global-state
```

清理旧备份，只保留最近 10 份：

```bash
python3 -m history_repair provider-prune-backups --keep 10
```

## 备份策略

执行 `provider-sync` 或 `provider-switch` 时，只要发生写入，都会自动备份：

- `~/.codex/config.toml`
- `~/.codex/state_5.sqlite`
- `~/.codex/state_5.sqlite-wal`
- `~/.codex/state_5.sqlite-shm`
- `~/.codex/.codex-global-state.json`
- 本次要修改的 rollout 文件

备份目录默认在：

```text
~/.codex/backups_provider_sync/<timestamp>/
```

## 本地历史引擎用法

如果你还需要调试底层续聊引擎，原有 CLI 仍然可用：

```bash
python3 -m history_repair init --db /tmp/history.db
python3 -m history_repair recover --db /tmp/history.db --pending-timeout-ms 60000
python3 -m history_repair export --db /tmp/history.db --output /tmp/history.jsonl
python3 -m history_repair import --db /tmp/history.db --input /tmp/history.jsonl
python3 -m history_repair list --db /tmp/history.db
python3 -m history_repair show --db /tmp/history.db --thread-id t1
python3 -m history_repair send --db /tmp/history.db --thread-id t1 --message "继续" --provider provider-a --account-id acc-1 --model gpt-5.4 --base-url https://api.example.com/v1 --api-key sk-xxx
```

也可以使用安装后的命令：

```bash
history-repair provider-status
history-repair provider-switch --provider sub2api
history-repair provider-autosync --quiet
```
