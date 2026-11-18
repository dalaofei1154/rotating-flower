# Claude Code interactive CLI source

[简体中文](#中文说明)

This source follows local Claude Code interactive terminal sessions. It reads native session status and bounded transcript effort metadata. It does not install hooks, change Claude settings, answer questions or approve commands.

## Availability and validation

Source integration only; not included in the published v0.7.0 installer. Windows native Claude Code **2.1.225** was tested with real terminal dialogs and Bash execution driven by a loopback synthetic model fixture. Command approval, resume before command completion, rejection, questions, Esc interruption, allow rules without prompts, concurrent sessions, a background shell and process exit were exercised. Real configured-model command approval, resume, concurrent sessions and question/answer workflows also passed through an isolated Responses transport; see the [acceptance record](claude-code-validation.md). The original gateway's Anthropic entry remains incompatible; that temporary transport is not shipped with the flower and daily Claude settings were not changed.

macOS live Claude, USB hardware and login startup have not been validated for this source. The Mac installer and package wiring have automated tests. Desktop, VS Code extension hosts, SDK / `claude -p`, remote sessions and cross-OS WSL observation are outside the supported scope; using the CLI inside an IDE terminal is the interactive CLI case.

The native session files are an internal format. Only the version above is verified. Other versions with the required fields are marked `unverified_version`; incompatible records are excluded and reported. Recheck after Claude upgrades.

## Try without hardware

From the repository root, using your development Python:

```sh
python m1/integrated/integrated_bridge.py --source claude --once
python m1/integrated/integrated_bridge.py --source claude --claude-root /absolute/path/to/claude-config --once
```

`--once` observes for about five seconds and prints state without controlling USB. Root precedence is `--claude-root`, then `CLAUDE_CONFIG_DIR`, then `~/.claude`. Start an interactive Claude terminal using the same config root. No state directory is created by the source.

Use `--source workbuddy,claude` to combine sources. `both` and `all` retain their historical meanings and do not enable Claude. Source ordering in CLI choices follows `codex,workbuddy,dsh,qodercn,claude`.

For a source-built Mac installation, explicitly enable the CLI source:

```sh
python3 m1/integrated_setup.py install --source claude --claude-root "$HOME/.claude"
```

For the Windows source installer, start with software-only validation:

```powershell
py -3 -X utf8 m1/windows_setup.py install --source claude --claude-root "$HOME/.claude" --dry-run
```

The source installer configures the flower's background runner; follow the [Windows guide](windows.md) for startup, status, uninstall and the separate USB-enabled mode. The packaged preview uses `app/windows_setup.py` instead of `m1/windows_setup.py`. The Claude source and process identity have native Windows software acceptance; physical USB acceptance for this source is still pending.

Both installers save an absolute root and preserve it on upgrades. Pass `--claude-root` explicitly to change a saved root. Claude is opt-in and does not participate in desktop app detection. It may be enabled before the CLI is first started. The Mac command configures the existing Mac flower service and requires the project's normal Mac installation prerequisites.

## What the flower reports

| Native interactive session | Output |
| --- | --- |
| Working (`busy`) | Spin at the observed effort, defaulting to MEDIUM |
| Waiting for a command approval or answer (`waiting`) | Stop and light attention |
| Approval granted / answer submitted | Resume on native `busy`, before any tool completion event |
| Rejected, interrupted or completed (`idle`) | Stop and clear that session's attention |
| User opens a local settings dialog (`dialog open`) | Stop without attention |
| Background shell still running (`shell`) | Continue spinning until the shell finishes |
| Process exits, creation identity differs or cannot be verified | Ignore its stored state |

Multiple sessions contribute independently: one session working while another waits produces rotation and attention together. Waiting does not expire by file age. Metadata is polled every 0.3 seconds by the bridge; short transitions entirely between polls may not be visible.

Effort maps `low / medium / high / xhigh / max` to `LOW / MEDIUM / HIGH / XHIGH / ULTRA`. Before this turn's effort becomes visible, `default_effort` denotes MEDIUM. Starting the observer during an existing approval or command can also default to MEDIUM because earlier assistant metadata predates the observed activity boundary. Activity and approval correctness do not depend on effort availability.

## Diagnostics and bounds

`not_initialized` means no native session data yet, or registration is still initializing. `not_running` means no verified live interactive session. `error` means no usable session and incompatible/unreadable metadata. A malformed neighbor does not suppress a healthy session; `diagnostics` records partial coverage and unavailable optional effort data.

The reader caps session scans at 4096 directory entries and 512 candidate sessions, each 64 KiB. Transcript discovery examines up to 2048 project directories and reads only selected live sessions, at most 4 MiB per changed transcript poll and 1 MiB per record. Limits are reported, not presented as complete coverage. An incomplete native JSON rewrite may retain the same verified process's last state for at most one second; an exited process gets no grace. Unfinished transcript bodies are re-read by offset and are not retained between polls.

Only session IDs, activity, effort, version, timestamps and bounded reason categories are retained. Prompts, command arguments, answers, outputs and credentials are not logged. Status still contains session IDs; review it before sharing.

On Windows the source uses read-only process handles and exact FILETIME creation identity. POSIX uses a bounded C/UTC `ps` query; its identity has one-second resolution, so same-second PID reuse cannot be excluded by that format. macOS native Claude identity compatibility still requires real testing.

## Acceptance before release

Run the repository checks in [CONTRIBUTING](../CONTRIBUTING.md). Record software tests separately from physical USB tests. Hardware acceptance must include approval of a harmless command lasting several seconds: verify that the flower resumes while the command is still running, plus rejection, Esc, concurrent sources, USB reconnection and startup. Do not infer physical acceptance from a passing simulated suite.

## 中文说明

Claude Code 来源读取本机交互终端的原生会话状态，完整覆盖命令审批等待、批准后恢复、拒绝、提问作答和 Esc 中断。多个会话独立贡献状态：一个等待、另一个工作时，可以同时旋转和亮提醒灯。不会注入 hooks、修改 Claude 设置或代替用户批准命令。

这是源码集成，尚未随公开 v0.7.0 安装包发布。已验证 Windows 原生 Claude Code 2.1.225 的审批、恢复、拒绝、提问、Esc、允许规则、多会话和后台 shell；首轮模型响应由本地合成夹具提供。后续使用原真实网关和模型，通过隔离 Responses 转换路径完成了命令审批、恢复、多会话和提问验收，见[验收记录](claude-code-validation.md)。原网关的 Anthropic 入口仍待修复，日常配置未改，临时转换器不属于小花发行内容。macOS 真实会话、USB 与开机启动仍待验收。

在仓库根目录运行上面的 `--source claude --once` 命令即可无硬件检查。自定义目录使用 `--claude-root`；否则依次读取 `CLAUDE_CONFIG_DIR`、`~/.claude`。安装器会保存绝对路径并在升级时保留；Claude 必须显式启用，旧 `all` / `both` 不包含它。

原生状态决定等待、恢复和结束，transcript 只补充 effort。首次观察到本轮 effort 之前使用 MEDIUM，诊断为 `default_effort`；中途启动观察器也可能暂用默认档。长时间审批不会因文件旧而过期。进程退出、PID 创建标识不同或无法核验时，旧记录不再贡献输出。

内部文件格式可能随 Claude 更新变化。未验证版本会标记 `unverified_version`，缺少必需字段会报告不兼容。日志仅保留状态元数据，不记录提示词、工具输入输出或认证配置。macOS 的 POSIX 创建时间只有秒级精度，其真实兼容性及同秒 PID 复用限制应在发布前复核。
