# Claude Code source validation

Tested on November 18, 2026, with native Windows Claude Code **2.1.225**, Python 3.13, and no physical flower. See [usage and compatibility](claude-code.md).

## Software acceptance

The first runtime pass used a real Claude CLI and real Bash execution with a loopback synthetic model fixture. It covered approval and resume before command completion, rejection, questions, Esc, allow rules, concurrent sessions, background shell and process exit.

A second pass used the configured remote gateway and real model. Its native Anthropic entry failed because it routed tool requests through incompatible Chat Completions. The test used an isolated, temporary Anthropic-to-Responses transport against the same gateway/model. This transport only converted wire formats; model text and tool calls came from real upstream responses. It is outside the repository and packages, and was shut down after testing. Daily Claude settings remained unchanged under file-hash checks. The original gateway entry was not repaired or claimed to pass.

Real-model acceptance:

| Scenario | Observed result |
| --- | --- |
| Two independent CLI sessions both request Bash approval | Bridge stops, attention on, two waiting session IDs |
| Approve A while B waits | Bridge rotates and keeps B's attention |
| A command is still executing | 16 observations confirm start marker exists, done marker is absent, A is working and B is waiting |
| A completes | Actual model returns the expected completion text; B's attention remains |
| Approve B | B independently resumes, executes and receives actual model completion; attention clears |
| AskUserQuestion, answer A, request another C/D question, cancel with Esc | Both real questions appear; answer resumes work; cancellation returns the bridge to STOP/attention off |

Each command used an isolated working directory and wrote a start marker, slept eight seconds, then wrote a done marker. File mtimes independently confirm the working observations precede command completion. A/B session IDs were bound to their own cwd and transcripts. Six actual bridge logs and the runtime summaries were checked by assertions and a separate reviewer. The successful group contained ten upstream requests, all HTTP 200 with completed responses. Exploratory failures were excluded from that group.

The temporary transport buffered Responses into Anthropic SSE, so this is not a native streaming-latency test. Encrypted reasoning replay was rejected by the upstream path; successful tests used visible, stateless history. No generic provider/proxy compatibility is part of this feature.

After rebasing onto the Windows integration at `a012c36`, the final bridge also completed a fresh real-model approval cycle in continuous `--dry-run` mode. Its actual status file changed from waiting/attention on to RUN HIGH/attention off before the done marker existed, then STOP after the actual model completion. The observer recorded 40 working observations during execution. The test used isolated flower support paths, no USB, and no installed service.

## Automated coverage

The source and process tests cover approval without PostToolUse, rejection/Esc without Stop, long waits without mtime expiry, all 64 combinations of three independent session states, PID exit/reuse/query errors, partial rewrites, timestamp rollback, parsing limits, effort isolation and transcript replacement. Installer/package tests cover opt-in selection, saved absolute roots, upgrade preservation and runtime allowlists.

Run the commands in [CONTRIBUTING](../CONTRIBUTING.md). Results for the final PR revision are recorded in the PR description; non-Mac baseline failures are listed separately from passing tests. A passing WSL check does not establish macOS runtime compatibility.

Final local checks against base `a012c36`:

| Check | Result |
| --- | --- |
| Windows Python 3.13: all integrated tests | 366 tests, pass, 1 platform skip |
| WSL Python 3.12: all integrated tests | 366 tests, pass, 4 platform skips |
| Windows portable installer / DSH / package / packaged-runtime suites | 43 / 35 / 7 / 3 tests, all pass |
| WSL Node 22 observer suite | 25 tests, all pass |
| WSL full m1 suite | Base 172 → feature 186; same 4 failures, 4 errors and 8 skips |
| Windows full m1 suite | Base 149 → feature 163; same 2 failures, 71 errors and 2 skips |

Full-m1 failed test identities match the base exactly in each environment; no new failures were found. These full suites contain Mac-only shell, user-ID and permission checks. WSL's eight failures/errors are the Mac-only WorkBuddy entry guard and the macOS `base64 -D` one-click installer cases. Native macOS checks remain pending; no passing CI run is inferred from these local checks.

## Required author / device-holder acceptance

These checks remain **unverified**, and are requested before declaring Mac or hardware support accepted:

- Native macOS Claude session files and process creation identity, including approval, rejection, questions, Esc, concurrent sessions and background shell.
- Mac installation/upgrade/uninstall, saved custom Claude config root, login startup and service restart.
- Physical motor speeds and attention light, using a harmless command lasting several seconds to verify resume **before** completion.
- USB disconnect/reconnect, service stop/start, two sources running concurrently, and existing source regression.

Record OS, Claude version, board and firmware. The author has not yet committed to performing these checks. No physical USB, Mac runtime, startup, signed installer, release or broader Claude-version compatibility is claimed by this software acceptance.
