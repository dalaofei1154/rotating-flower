# Windows installation preview

Windows preview.7 includes the [background status fix](https://github.com/dalaofei1154/rotating-flower/pull/11) reported by Bella-rab. If another program holds the status file open, the bridge keeps running and publishes a fresh snapshot after the file is released. The packaged bridge passed this check in dry-run mode on Windows with Python 3.11 and 3.13.

**Upgrading an existing preview:** Extract the entire preview.7 ZIP into a new directory, then run its `install.cmd` without arguments. The installer keeps your sources, paths, port and software/hardware mode and starts the upgraded service. Run `check.cmd` and look for `running: true` and `status_current: true`. Restart DSH if it is one of your sources. See below if you are also changing Python installations.

## Requirements

- Native Windows desktop applications and a data-capable USB cable with a previously flashed flower. This installer does not install AI applications or flash firmware.
- A separate, maintained Python 3.11+ installation. The preview does not bundle Python. Installation downloads the pinned, SHA256-checked pyserial 3.5 wheel and creates a virtual environment.
- The virtual environment and login entry still depend on the base Python. Do not uninstall or move it. WorkBuddy's bundled interpreter worked in earlier testing, but is not the recommended long-term dependency.

For ordinary consumers the intended delivery is a flower-managed runtime; that packaging is separate work and is not implemented here. Windows 11, multiple flowers and all application versions are not claimed hardware-verified.

## Build and install

Build from this checkout with `python m1/package_windows.py --output dist/windows-preview`. Extract the whole resulting ZIP; do not run entries inside an archive. A Python-equipped developer may also run `python m1/windows_setup.py install` from the checkout on Windows.

Run installation/start operations from Explorer or a terminal you opened independently. Some AI/IDE task hosts reclaim their child processes after a task; a hidden window alone does not establish an independent background lifetime. AI tools can read diagnostics and logs.

1. Install/open the AI application you intend to use. For DSH, open it once to create its desktop profile.
2. In a terminal in the extracted folder, select a source explicitly, for example:

```powershell
py -3 -X utf8 .\app\windows_setup.py install --source codex --dry-run
py -3 -X utf8 .\app\windows_setup.py install --source workbuddy --dry-run
py -3 -X utf8 .\app\windows_setup.py install --source dsh --dry-run
py -3 -X utf8 .\app\windows_setup.py install --source qodercn --dry-run
py -3 -X utf8 .\app\windows_setup.py install --source claude --dry-run
py -3 -X utf8 .\app\windows_setup.py install --source workbuddy,claude --claude-root "D:\Claude config" --dry-run
py -3 -X utf8 .\app\windows_setup.py install --source codex,workbuddy,dsh,qodercn --dry-run
```

3. Restart DSH after installing/updating/removing its plugin. `check.cmd` reports background/source state; an unopened application may be `not_initialized` rather than broken.
4. To connect real hardware, use the same independent Python:

```powershell
py -3 -X utf8 .\app\windows_setup.py install --enable-hardware --port auto
```

`install.cmd` defaults on a first install to Codex + WorkBuddy and dry-run (no USB movement/light). Reinstallation preserves saved source, paths, port and mode unless explicitly overridden. `all` retains the three-source meaning Codex + WorkBuddy + DSH; add Qoder explicitly. `--qoder-root` and `--qoder-app-data` persist custom Qoder locations.

Claude Code's interactive CLI is an explicit opt-in source; neither `both` nor `all` enables it. On first installation, `--claude-root` takes priority over `CLAUDE_CONFIG_DIR`, then `~/.claude`. The installer saves an absolute path and preserves it on reinstallation unless `--claude-root` overrides it. See [Claude Code support](claude-code.md) and its [validation record](claude-code-validation.md).

`start.cmd` resumes; `stop.cmd` pauses until start (including across login); `uninstall.cmd` stops the service, removes its login entry and managed DSH registration. Logs, backups and runtime directories are retained for recovery. Installation is per-user under `%LOCALAPPDATA%\RotatingFlower`, with an HKCU Run value named `RotatingFlower`; it starts after that user logs in, not before login as a system service.

## Existing preview users and Python migration

The launcher deliberately reuses an existing flower interpreter when present, to avoid breaking an installation. Installing a separate Python alone does not migrate an old service. To migrate from a WorkBuddy-provided runtime, explicitly run the new Python executable against this package's installer, without changing source/mode settings:

```powershell
& 'C:\path-to-independent-Python\python.exe' -X utf8 .\app\windows_setup.py install
```

Replace the example with the verified actual executable path. This rebuilds the environment and login entry. Verify diagnostics, one real task and login recovery before removing the old interpreter. Migration to an independent Python 3.13.17 and subsequent login startup passed on the recorded Windows 10 setup; a clean-user first installation remains untested.

DSH remains limited in this Windows installer to the default `~/.dsh` desktop profile. Upgrading the existing managed block uses a file URI; configuration receipts preserve original bytes and user edits. The observer keeps main's event retry and temporary-failure recovery, with Windows status paths. Removal preserves a shared observer still referenced by another profile.

## Diagnostics and known limits

- `diagnose.cmd` reports the installed interpreter, the precise HKCU startup key/value it queried, and source state; it does not drive USB. Registry access denial is reported as a query error, not as proof the value is absent.
- Installation steps are recorded in `logs/install-events.jsonl`, including the failing stage, exception type and OS error/path when available. If even the log directory is denied, the original failure still goes to stderr. Keep the terminal output.
- Background logs are in `logs/bridge.log`; windowless startup failures may appear in `logs/startup-error.log` or `runner-error.log`.
- Earlier preview.5 had an unresolved initial WinError 5, followed by a successful unchanged-package installation in an independent terminal. It did not recur during the recorded preview.6 installs; its original cause remains unknown.
- Run diagnostics as the same Windows user who installed the bridge. During migration testing, an AI tool used a separate sandbox account: its HKCU query did not see the interactive user's startup entry, and its installer-lock access was denied. The user's ordinary terminal successfully read the entry and ran status/diagnose. Check the execution identity before treating these errors as a missing startup entry or an installation failure; this finding does not establish the cause of the earlier WinError 5.
- Qoder structured question waits are supported; command-approval waits are not. Sessions with no updates expire after the existing 600-second retention. See [Qoder details](qodercn.md).

## Validation and attribution

This records a tested candidate, not a new Release or a guarantee for every Windows/application version. Original packages and their build-time verification flags remain unchanged.

### Archived preview.6 candidate

- Base main: `a3e6adbf235f4e0eec06e5512daa638047da0f9e` (includes Qoder CN PR #8).
- Tested source: `c8296c54be84e459223a6bfcc9bf98e9242a4255`.
- Package: `RotatingFlower-Windows-v0.7.0-windows-preview.6-main-integration.zip`.
- Package SHA256: `31dd8253bfa49bd4fb7e8050abd831665227903ad041c9854fe619530d3a7d0c`.
- Cloud source snapshot SHA256: `f3bad0f48843b783c182864f919609e6382c9c1de9c4f4dc77158af0dea31edc`.

### Software checks

The [final validation run](https://github.com/dalaofei1154/rotating-flower/actions/runs/44058293084) passed Windows and Ubuntu Python 3.11/3.13 and macOS Python 3.9/3.13. Each job checked the source snapshot, ran bridge/installation/DSH/package regressions appropriate to that platform, ran the observer tests, and compared every rebuilt package entry against the supplied candidate. The dedicated CI branch commit is `64d62a35eeb694291569f1d0820baba6d34f7d51`; it executes the verified source snapshot rather than testing the older files at that branch's root.

The first run exposed two test-fixture issues: a Windows short-path alias was compared to its canonical spelling, and an unchanged Linux registration correctly skipped the write where the test expected to inject failure. The final source corrects the assertion and supplies a registration that actually requires a write. No runtime change was made to bypass these failures.

### Windows hardware checks

Returned evidence from Windows 10 Home China 22H2, build 19045, with one flower was cross-checked against the original package. Evidence consists of application/device logs, interpreter/configuration/process records, file hashes and explicit user observations. The maintainer's review did not remotely re-read the Windows filesystem.

- DSH 0.2.2-rc.3: upgrade from preview.5, user-configuration preservation, a single managed file-URI registration, repeated installation and plugin reload passed. A real task ran, stopped with the question light on, resumed with the light off after an answer, then stopped at completion. Other sources were idle in the observed window.
- One preference differed between the first-upgrade snapshot and the later reinstall. The returned immediate pre-install backup comparison records that the preference had changed before the second install and was preserved by it. The intervening preference change was not investigated. The original configuration used LF without a BOM, so BOM/CRLF hardware checks were not applicable; software cases cover those formats.
- Migration from WorkBuddy's Python to an independent Python 3.13.17 AMD64 passed using the unmodified installer in the user's ordinary PowerShell. The new venv base and running interpreter paths matched the independent installation; saved source/path/port/mode settings were preserved. Old interpreters and runtimes were retained.
- After reboot and login, the user startup entry, process start times, a new recorded startup timestamp, refreshed state, USB connection logs and the no-manual-start operation record support successful login startup. The short-lived bootstrap process's complete command line was not captured. This is current-user login startup, not a pre-login system service.
- After login, a real DSH task produced rotation and stopping, confirmed by the user and device log. Other sources were idle. Previously passed question-light and four-source scenarios were not repeated in this migration round.
- Both field-test rounds report the same original ZIP hash; all 30 package-file hashes and 17 installed app-file hashes matched the local original during review. No source patch, extra startup helper, manual registry repair or scheduled-task workaround was used.

The saved configuration retained Codex, WorkBuddy, DSH and Qoder CN. This does not constitute a fresh four-application acceptance run on preview.6. The earlier preview.5 has separate records for four-source regression, Qoder/WorkBuddy overlap, USB recovery and stop/start.

### Remaining limits

- A clean-user first installation, Windows 11, multiple devices and arbitrary application versions are not covered by this evidence.
- The preview still needs a separate Python installation; a bundled consumer runtime is not implemented.
- A separate sandbox account could not see the interactive user's HKCU startup entry and could not access the installer lock. The interactive user's terminal successfully queried both registry views and ran diagnostics. This explains that observed context discrepancy, not the historical WinError 5.
- The earlier WinError 5 did not recur in these installations; its original cause remains unresolved. Some terminal text had encoding artifacts; structured evidence was readable.

Later documentation or provenance-comment edits should be distinguished from the tested source. A rebuilt ZIP has its own identity and must not inherit this package's hash. Review source/payload differences before deciding whether further software or hardware checks are needed.

Contribution provenance is recorded in [windows-contributions.md](windows-contributions.md). The earlier preview.5 [Windows CI run](https://github.com/dalaofei1154/rotating-flower/actions/runs/44022917649) and its hardware records remain a separate baseline. Only share sanitized evidence, not application databases, conversations or private machine paths.
