# Installation and private pairing

[繁體中文](installation.zh-TW.md) · [Project](../README.md)

## Requirements

- Mac: Python 3.10 or newer, Git, and a CLI agent that can run local commands and use the private network. The CLI itself does not depend on an agent SDK.
- Windows: Windows PowerShell 5.1, a logged-in user, and the software needed by the task. Keep Windows awake for long desktop/RStudio work.
- Both devices: a trusted private route. For cross-network use, install Tailscale on both and configure the tailnet's access policy to permit the intended Windows-to-Mac server connection.
- RStudio/ClaudeR, R packages, Mplus and their licenses are separate dependencies. This installer does not install or license them.

## 1. Install the skill on Mac

```sh
git clone https://github.com/lzhs1995/cross-device-remote.git
cd cross-device-remote
python3 scripts/check.py
python3 scripts/install.py --agents codex claude gemini antigravity
python3 scripts/install.py --agents codex claude gemini antigravity --check
export PATH="$HOME/.agents/skills/cross-device-remote/scripts:$PATH"
```

The shared skill points to this checkout. Keep the checkout in place; updates to it affect installed links. For reproducible use, record the commit before running work and update only at a safe boundary.

| Entry | Installed location |
| --- | --- |
| Shared | `~/.agents/skills/cross-device-remote` |
| Codex | `~/.codex/skills/cross-device-remote` |
| Claude Code | `~/.claude/skills/cross-device-remote` |
| Gemini | `~/.gemini/skills/cross-device-remote` |
| Antigravity | `~/.gemini/antigravity/skills/cross-device-remote` |

The default agent list is all four. `--prefix PATH` uses another home directory for an isolated installation. Conflicting existing entries are preserved and installation is refused; an explicit `--replace` makes dated backups before replacing links. Existing private configuration is preserved. `--check` only checks the links, not agent discovery or network health.

For another agent, ask it to read the shared `SKILL.md` and invoke the absolute `scripts/xdev` entry. Skill auto-discovery conventions vary across products and versions; the actual agent must verify its own loading behavior.

## 2. Initialize private Mac state

```sh
WBAGENT_STATE="$HOME/.local/state/cross-device-remote" python3 "$HOME/.agents/skills/cross-device-remote/scripts/wbagent.py" init
open -e "$HOME/.config/cross-device-remote/config.json"
```

The installer creates configuration only if absent. The sample listens on loopback, which deliberately does not accept a remote Windows connection. In your **private** configuration, replace `serve_args` with your actual trusted binding and allowlist. This illustrative fragment contains placeholders, not runnable addresses:

```json
"serve_args": [
  "--host", "<MAC_TAILSCALE_IP>",
  "--port", "18092",
  "--allow-ip", "<WINDOWS_TAILSCALE_IP>/32",
  "--require-signed-poll",
  "--ttl", "86400"
]
```

Use `tailscale ip -4` on each device to obtain that device's address. Permit only the intended peer in the tailnet policy and the server allowlist. Configure a TTL that covers the planned batch or a coordinated maintenance boundary. The server has no HTTPS termination in this package: the private Tailscale route provides transport encryption, while HMAC provides message authentication. File-download access also depends on the network allowlist.

Keep configuration, `state_dir`, the pairing secret, real addresses, and optional discovery service details outside Git. Do not copy private state into this checkout.

## 3. Pair a fresh Windows agent

Copy the repository to Windows, for example using Git. Transfer the Mac state's `secret` file through an approved private channel to a private Windows file. Do not put it in a chat transcript, public gist or repository. The installer reads the file without printing its content.

Run from the Windows checkout, replacing the placeholder address and private file path:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\windows\install.ps1 -Server 'http://<MAC_TAILSCALE_IP>:18092' -SecretFile 'C:\private\xdev-secret'
```

The default state is `%LOCALAPPDATA%\XdevAgent`, restricted to the current user and SYSTEM. `-State` selects another fresh directory. The installer refuses an existing state or legacy worker configuration instead of replacing it or creating a second consumer of one queue.

Options are explicit:

- `-Start`: start the worker immediately.
- `-AutoStart`: install user-level logon and five-minute watchdog tasks. This alone does not start the worker immediately.
- Both switches: install the tasks and start now.

To start after installation:

```powershell
wscript.exe "$env:LOCALAPPDATA\XdevAgent\start.vbs"
```

Existing deployments should use their existing recovery procedure. Do not install this fresh-worker package over a running legacy agent, overwrite its cursor, or copy its private state into the repository.

## 4. Start Mac service and verify the channel

```sh
xdev serve
xdev health
xdev run -c '[Environment]::MachineName; [DateTime]::UtcNow.ToString("o")' --timeout 20
```

A fresh signed poll and the new command's result establish a working command channel. They do not establish RStudio or Mplus success. Keep the returned request ID; if waiting expires, use `xdev result ID` instead of repeating the command.

Before scientific work, follow the [RStudio recipe](../recipes/windows-rstudio-clauder.en.md) or the separately permitted Rscript/Mplus route. `find-tools --write` searches software and fills only unset path keys; run it once when needed, not on every status check.

## Offline and connected checks

`python3 scripts/check.py` is the local check entry. `scripts/selftest.sh` is a connected test that can write synthetic files and create remote jobs; read it and reserve a test window first. See [validation boundaries](validation.md).

## Uninstall without discarding recovery state

1. Stop new submissions and reach a safe boundary: confirm this installation has no in-flight commands, detached work, or RStudio/engine jobs. Preserve all request IDs, budgets and results. If the Mac service is shared, coordinate with its other owners before stopping it.
2. On Windows, verify that the installed `XdevAgentLogon` and `XdevAgentWatchdog` task actions point to **this installation's** `start.vbs`. At the safe boundary, create a `STOP` file in its own state directory (use the original `-State` path if customized), then wait for a newly appended `WBAGENT_V2 stop` log entry and the matching worker process to exit. The watchdog honors this file. Do not force-stop another worker or treat an old log entry as a fresh stop.

   ```powershell
   $xdevState = Join-Path $env:LOCALAPPDATA 'XdevAgent'
   Get-ScheduledTask -TaskName XdevAgentLogon,XdevAgentWatchdog -ErrorAction SilentlyContinue | Format-List TaskName,Actions
   # After verifying ownership and that no work remains:
   New-Item -ItemType File -Path (Join-Path $xdevState 'STOP') -Force | Out-Null
   ```

3. After the worker has stopped, unregister only the verified tasks that this installation created. If `-AutoStart` was not used, they may not exist. Leave legacy `StataWorkbenchAgent` / `WbAgentWatchdog` tasks and other owners' tasks untouched.

   ```powershell
   # Run only for each existing task whose action was verified above.
   Unregister-ScheduledTask -TaskName XdevAgentLogon -Confirm:$false
   Unregister-ScheduledTask -TaskName XdevAgentWatchdog -Confirm:$false
   ```

4. On the Mac, inspect each skill entry listed above. Remove a link only if it is a symlink and its resolved target is still this checkout; remove the verified agent links before the shared link so resolution remains meaningful. Leave ordinary directories and links pointing elsewhere untouched. If retiring this installation's Mac service too, use the same no-work boundary and its own state-directory `STOP` file, then verify its exit.

Keep private `config.json`, both devices' state directories, pairing secrets, original attempts, budgets and result files. Uninstalling links or tasks does not require deleting them. Retention or deletion is a separate user decision. Resume from the retained state through its original recovery procedure rather than initializing a new queue over it.

## Official Tailscale references

- [Install Tailscale](https://tailscale.com/kb/1017/install)
- [Access control policies](https://tailscale.com/kb/1018/acls)
- [Direct and relay connection types](https://tailscale.com/kb/1257/connection-types)

A relay connection can still be encrypted; direct connectivity affects routing and performance. Neither a ping nor a route type substitutes for a fresh xdev command result.
