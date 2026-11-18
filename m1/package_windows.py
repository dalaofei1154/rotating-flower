#!/usr/bin/env python3
"""Build the Windows software-preview source ZIP; no runtime is bundled."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import zipfile

from windows_setup import APP_FILES, BRIDGE_FILES, SOURCES, VERSION

NAME = 'RotatingFlower-Windows-v' + VERSION
SCRIPTS = ('install.cmd', 'start.cmd', 'stop.cmd', 'check.cmd', 'diagnose.cmd', 'uninstall.cmd',
           'launch.cmd', 'find-python.ps1')
GUIDE = """旋转花 · Windows 软件预览版

Windows 软件预览版 preview.7 包含后台状态文件占用修复（PR #11）。
已安装旧预览版：把本 ZIP 完整解压到新文件夹，直接双击新版 install.cmd，无需先卸载。
安装器会沿用原有来源、路径、串口和模式，安装完成后启动后台。如果原来启用了硬件，小花可能恢复转动。
安装成功后运行同目录的 check.cmd，确认 running 和 status_current 均为 true。使用 DSH 的用户请重启 DSH。
需要 Windows 桌面环境、Python 3.11 或更新版本；安装时联网从 PyPI 下载
经过固定版本与 SHA256 校验的 pyserial 3.5，并建立独立 Python 虚拟环境。
Python 下载：https://www.python.org/downloads/windows/
虚拟环境仍依赖安装时的基础 Python，请勿在使用期间卸载该 Python。

1. 将整个 ZIP 解压到本地文件夹。不要直接在压缩文件内运行。
2. 双击 install.cmd。首次默认启用 Codex + WorkBuddy，并使用软件检查模式：
   读取应用状态、写入日志，但不打开 USB、不转动、不亮灯。
3. 双击 check.cmd 查看后台和各应用状态；diagnose.cmd 做一次只读诊断。
   未安装/未初始化的应用可能显示等待；接口不兼容会显示错误，不冒充已连接。
4. start.cmd 启动已安装后台；stop.cmd 暂停（直到再次 start，期间登录也保持停止），
   uninstall.cmd 停止并移除登录自启。

命令行选项（在解压文件夹打开终端）：
  py -3 -X utf8 app/windows_setup.py install --source workbuddy
  py -3 -X utf8 app/windows_setup.py install --source dsh --dry-run
  py -3 -X utf8 app/windows_setup.py install --source workbuddy,dsh --dry-run
  py -3 -X utf8 app/windows_setup.py install --source all --dry-run
  py -3 -X utf8 app/windows_setup.py install --source qodercn --dry-run
  py -3 -X utf8 app/windows_setup.py install --source claude --dry-run
  py -3 -X utf8 app/windows_setup.py install --source workbuddy,claude --claude-root "D:\\Claude config" --dry-run
  py -3 -X utf8 app/windows_setup.py install --source codex,workbuddy,dsh,qodercn --dry-run
  py -3 -X utf8 app/windows_setup.py install --source codex --sessions-root "D:\\my codex\\sessions"
  py -3 -X utf8 app/windows_setup.py install --codex-endpoint "Windows 上已确认的实际 IPC 端点"
  py -3 -X utf8 app/windows_setup.py install --enable-hardware --port COM10
  py -3 -X utf8 app/windows_setup.py install --enable-hardware --port auto
  py -3 -X utf8 app/windows_setup.py install --dry-run

enable-hardware 明确开启 USB 控制；请准备好硬件后再使用。
重新安装会保留已有来源、路径、端口和软件检查/硬件模式，只有显式选项会覆盖。
首次默认 both；只有一个应用时请指定 --source codex 或 --source workbuddy。
Qoder CN 来自 Y1X1n 的 PR #8（b06b3da），适配器保持原样；贡献来源详见 docs/windows-contributions.md。
all 保持 Codex + WorkBuddy + DSH；加入 Qoder 请显式选择 qodercn 或上面的四来源组合。
Qoder 默认读取 ~/.qoder-cn，可用 --qoder-root 与 --qoder-app-data 指定路径；重装保留配置。
Qoder 问答等待支持提醒灯，权限授权等待目前不支持；无更新的会话超过十分钟会过期。
Claude Code 交互式 CLI 须显式启用；both 和 all 不包含 Claude。
首次安装的 Claude 目录按 --claude-root、CLAUDE_CONFIG_DIR、~/.claude 依次选择，并保存绝对路径。
重装保留已保存目录；需要更改时显式传入 --claude-root。不会安装 Claude 或改动其设置。
Claude 支持范围和验证边界见 docs/claude-code.md 与 docs/claude-code-validation.md。
应用内部状态格式可能随版本变化；支持范围和历史验证详见 docs/windows.md。
DSH 首轮仅支持 Windows 原生官方桌面版，默认使用 ~/.dsh/profiles/desktop。
启用 DSH 会安装受管观察插件；安装后重启 DSH 并创建新任务做软件检查。
切换到不含 DSH 的来源或卸载会移除受管插件配置，保留其他用户配置。
WSL 和自定义 profile 不属于本轮；不会修改应用数据库，也不烧录固件。

安装目录：%LOCALAPPDATA%\\RotatingFlower
日志：安装目录\\logs\\bridge.log（启动失败另见 runner-error.log）
配置：安装目录\\config.json；软件状态：status.json
当前用户登录自启：HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run 的 RotatingFlower 项。
不需要管理员权限。卸载保留运行文件、备份和日志，以便排查或恢复。
安装、切换模式和启动请在用户自行打开的终端或文件资源管理器中执行；
AI 工具启动的后台可能受任务清理影响。安装步骤记录见 logs/install-events.jsonl；
diagnose 会同时报告 Python 路径和实际读取的登录启动项。旧 WinError 5 根因尚未确定。
从 WorkBuddy 内置 Python 迁移时，请用新独立 Python 的完整路径运行 app/windows_setup.py install，
不附来源/模式选项可保留旧设置；仅安装 Python 不会自动迁移，原启动器仍会优先复用旧解释器。
软件来源和许可见 LICENSE；包内无账号、聊天记录、缓存或本地用户配置。
"""


def source_revision(root, exported_commit=None):
    if exported_commit is not None:
        if not re.fullmatch(r'[0-9a-f]{40}', exported_commit):
            raise ValueError('Exported source commit must be a full lowercase Git SHA-1')
        if (root / '.git').exists():
            raise ValueError('Use --source-commit only for a verified source export without .git')
        return {'commit': exported_commit, 'dirty': False}
    if not (root / '.git').exists():
        return {'commit': None, 'dirty': None}
    try:
        commit = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root,
                                capture_output=True, text=True, check=True, timeout=5).stdout.strip()
        changed = subprocess.run(['git', 'status', '--porcelain', '--untracked-files=all', '--',
                                  'm1', 'docs/windows.md', 'docs/windows-contributions.md', 'docs/qodercn.md',
                                  'docs/claude-code.md', 'docs/claude-code-validation.md'],
                                 cwd=root, capture_output=True, text=True, check=True, timeout=5).stdout
        return {'commit': commit, 'dirty': bool(changed.strip())}
    except (OSError, subprocess.SubprocessError):
        return {'commit': None, 'dirty': None}


def build(output: Path, source_commit=None):
    source = Path(__file__).resolve().parent
    revision = source_revision(source.parent, source_commit)
    files = {f'app/integrated/{name}': source / 'integrated' / name for name in BRIDGE_FILES}
    files.update({name: source / 'windows' / name for name in SCRIPTS})
    files.update({f'app/{name}': source / name for name in APP_FILES})
    files['LICENSE'] = source.parent / 'LICENSE'
    for name in ('windows.md', 'windows-contributions.md', 'qodercn.md',
                 'claude-code.md', 'claude-code-validation.md'):
        files['docs/' + name] = source.parent / 'docs' / name
    missing = [str(path) for path in files.values() if not path.is_file()]
    if missing:
        raise ValueError('Missing package files: ' + ', '.join(missing))
    output.mkdir(parents=True, exist_ok=True)
    contents = {name: path.read_bytes() for name, path in files.items()}
    # CRLF .cmd files work in cmd.exe regardless of the authoring host.
    for name in SCRIPTS:
        contents[name] = contents[name].replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')
    contents['先看这里.txt'] = GUIDE.encode('utf-8-sig')
    checksums = {name: hashlib.sha256(data).hexdigest() for name, data in sorted(contents.items())}
    manifest = {'version': VERSION, 'runtime_bundled': False, 'requires_python': '>=3.11',
                'installation_network_downloads': True, 'hardware_verified': False,
                'windows_runtime_verified': False, 'default_dry_run': True,
                'supported_sources': ['codex', 'workbuddy', 'dsh', 'qodercn', 'claude'],
                'explicit_opt_in_sources': ['claude'], 'source_choices': list(SOURCES),
                'dsh_windows_integrated': True, 'dsh_profile': 'desktop',
                'dsh_windows_runtime_verified': False,
                'qodercn_windows_runtime_verified': False,
                'qodercn_upstream_commit': 'b06b3da016caeb04b00214f3c2745207aa8c0aea',
                'source_revision': revision,
                'prior_hardware_baseline_sha256': '2d382cccd3bfd6780fa6882a5d593667bb2b5a5302b25003555c100ae49266df',
                'validation_note': 'PR #11 verified status contention and recovery on Windows in dry-run mode. Hardware results for prior previews are recorded separately.',
                'files': checksums}
    contents['manifest.json'] = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    target = output / (NAME + '.zip')
    with tempfile.NamedTemporaryFile(prefix='.windows-package-', suffix='.zip', dir=output, delete=False) as tmp:
        temporary = Path(tmp.name)
    try:
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name, data in sorted(contents.items()):
                info = zipfile.ZipInfo(NAME + '/' + name, (2026, 11, 10, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, data)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix('.zip.sha256').write_text(f'{digest}  {target.name}\n', encoding='ascii')
    return target


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--source-commit', help='Verified export provenance; never overrides a Git checkout')
    args = parser.parse_args()
    print(build(args.output, args.source_commit))
