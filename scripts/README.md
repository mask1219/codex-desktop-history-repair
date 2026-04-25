# Scripts

## `patch_codex_app_extended_history.js`

用途：给已安装的 `/Applications/Codex.app` 打包产物打补丁，让普通新线程和恢复线程默认启用 `persistExtendedHistory: true`，并让最近线程列表优先从本地 state db 读取。

背景：当前 Codex.app 是 Electron 打包应用，核心代码在 `Contents/Resources/app.asar`，不是源码仓库。直接做源码级接入不可行时，用这个脚本对打包后的几个明确调用点做最小补丁。

执行：

```bash
node scripts/patch_codex_app_extended_history.js /Applications/Codex.app
```

只读检查当前 App 是否需要补丁：

```bash
node scripts/patch_codex_app_extended_history.js /Applications/Codex.app --check
```

实验单个补丁点：

```bash
node scripts/patch_codex_app_extended_history.js /Applications/Codex.app --patch vscode-api
node scripts/patch_codex_app_extended_history.js /Applications/Codex.app --patch app-server
node scripts/patch_codex_app_extended_history.js /Applications/Codex.app --patch thread-list-state-db
node scripts/patch_codex_app_extended_history.js /Applications/Codex.app --patch workspace-root-drop-state-db
node scripts/patch_codex_app_extended_history.js /Applications/Codex.app --patch thread-list-all-state-db
```

脚本会做：

- 备份 `Contents/Resources/app.asar`
- 备份 `Contents/Info.plist`
- 修改 `webview/assets/vscode-api-*.js` 中的新线程参数
- 修改 `webview/assets/app-server-manager-signals-*.js` 中的恢复线程参数
- 修改 `webview/assets/app-server-manager-signals-*.js` 中的最近线程列表参数，强制 `useStateDbOnly: true`
- 修改 `webview/assets/app-server-manager-signals-*.js` 中的“全部线程”列表参数，移除 `sourceKinds` 限制并强制 `useStateDbOnly: true`
- 修改 `.vite/build/workspace-root-drop-handler-*.js` 中的项目入口最近线程参数，强制 `useStateDbOnly: true`
- 对需要展示跨 provider 历史的 `thread/list` 请求，显式传 `modelProviders: []`，因为 `modelProviders: null` 在当前 Codex Desktop / app-server 语义下不会返回全部 provider
- 若 Codex.app 更新导致 hash 文件名变化，脚本会按资源文件名前缀定位；必要时再按未补丁的目标代码片段兜底定位
- 更新 asar header 里的目标文件 integrity
- 更新 `Info.plist` 里的 `ElectronAsarIntegrity` header SHA256
- 对修改后的 `.app` 做本地 ad-hoc 重新签名

备份文件命名：

```text
app-backups/app.asar.history-repair-backup-YYYYMMDDHHMMSS
app-backups/Info.plist.history-repair-backup-YYYYMMDDHHMMSS
```

已执行记录：

- 2026-04-24：已对 `/Applications/Codex.app` 执行一次补丁。
- 备份：
  - `/Applications/Codex.app/Contents/Resources/app.asar.history-repair-backup-20260424081442`
  - `/Applications/Codex.app/Contents/Info.plist.history-repair-backup-20260424081442`
- 旧脚本记录的补丁后普通文件 SHA256：
  - `4a54c04c2be4332a563cc87ed1e1e8bdd8c1b417b6a3974fd240eb2f8389dba9`

何时需要重跑：

- Codex.app 自动更新后
- 手动替换或重装 `/Applications/Codex.app` 后
- `GET /health` 或用户侧行为显示历史持久化补丁不再生效时

验证：

```bash
shasum -a 256 /Applications/Codex.app/Contents/Resources/app.asar
plutil -p /Applications/Codex.app/Contents/Info.plist | rg -A3 ElectronAsarIntegrity
```

注意：

- 修改打包应用会使原始 OpenAI Developer ID 签名失效；脚本会重新做本地 ad-hoc 签名，让应用可启动。
- 不要把备份文件留在 `.app` 包内；额外文件会破坏 macOS sealed resources 校验。
- 脚本保持 asar 内目标文件长度不变，降低打包结构损坏风险。
- `ElectronAsarIntegrity` 不是整个 `app.asar` 的普通文件 SHA256，而是 asar header JSON 的 SHA256。
- 脚本是幂等的：如果目标已经是 `persistExtendedHistory: true` 或 `useStateDbOnly: true`，会标记为 `already-patched`。
- 2026-04-25 已验证 Codex.app `26.422.21637` 在修正 asar header integrity 后可以完整打补丁并启动。
- 2026-04-25 已新增 `--check` 只读检查，并对 `/Applications/Codex.app` 再次执行补丁；检查结果为两个目标点均 `already-patched`。
- 2026-04-25 已通过独立 `codex app-server` 调试确认：`thread/list` 的 `modelProviders: null` 只返回当前 provider，`modelProviders: []` 才返回全部 provider；脚本已按这个语义修正列表补丁。
- 2026-04-25 已在真实 Codex.app 中验证：当前项目历史列表可同时显示 `custom / sub2api / openai`，不再只显示 3 条。
- 若未来 Codex.app 改了打包文件名或目标代码片段，脚本会失败并提示 `Patch target not found`，需要重新定位调用点。

关联文档：

- `docs/桌面端外部接入与升级策略.md`

## `restore_codex_app_from_backup.js`

用途：从 `app-backups` 中恢复最近一组成对的 `app.asar` 和 `Info.plist` 备份，并对 `.app` 做本地 ad-hoc 重签名。

执行：

```bash
node scripts/restore_codex_app_from_backup.js /Applications/Codex.app
```

## `install_codex_app_patch_launch_agent.sh`

用途：安装用户级 LaunchAgent，让 `patch_codex_app_extended_history.js` 自动执行。

执行：

```bash
sh scripts/install_codex_app_patch_launch_agent.sh /Applications/Codex.app
```

安装后会创建：

```text
~/Library/LaunchAgents/com.am700.codex-history-repair.patch.plist
```

触发时机：

- 用户登录时自动执行一次
- `/Applications/Codex.app/Contents/Resources/app.asar` 发生变化时自动执行

这意味着 Codex.app 自动更新覆盖补丁后，LaunchAgent 会重新运行补丁脚本。补丁脚本本身是幂等的，因此已经打过补丁时不会重复写入或重复生成备份。

日志：

```text
~/Library/Logs/codex-history-repair-patch.log
~/Library/Logs/codex-history-repair-patch.err.log
```

检查是否已加载：

```bash
launchctl print gui/$(id -u)/com.am700.codex-history-repair.patch
```

手动触发：

```bash
launchctl kickstart -k gui/$(id -u)/com.am700.codex-history-repair.patch
```

卸载：

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.am700.codex-history-repair.patch.plist
rm ~/Library/LaunchAgents/com.am700.codex-history-repair.patch.plist
```
