# Recovery without replaying completed work

[繁體中文](recovery.zh-TW.md) · [Project](../README.md)

## Read evidence in order

Run `xdev health`, then read the original request/job state. Diagnose these separately: network reachability, fresh Windows polling, command execution, ClaudeR identity, and scientific progress. `ping` alone proves none of the later stages.

| Observation | Next action |
| --- | --- |
| `SERVER_DOWN` | Inspect the Mac service log and TTL. Start the configured service with `xdev serve`; verify a fresh Windows poll before submitting. |
| `AGENT_STALE` | Check Windows login/sleep state, its agent log, configured address, Tailscale route, and server allowlist. Preserve queued requests. |
| `OK (agent busy...)` with old polling | Treat it as queue evidence, not proof of current reachability. Inspect the cursor and original job evidence. |
| `run` returns 124 | Use `xdev result ORIGINAL_ID`. Do not repeat `run`. |
| Detached work is still running | Use `xdev tail ORIGINAL_NAME` or `xdev wait ORIGINAL_NAME --max 3600`. Do not repeat `detach`. |
| ClaudeR submission is uncertain | Use `xdev rstudio result --state ORIGINAL_STATE --request-id ORIGINAL_REQUEST`. Poll the original async job. |
| RStudio PID changed | Inspect old jobs, outputs and budgets first. Verify the new process identity before creating new binding state. |
| Valid output exists without an exit receipt | Apply the domain workflow's orphan-output and budget checks. Unknown runtime is not zero. |

## Service TTL and watchdogs

A server TTL must cover the planned batch or have a coordinated maintenance boundary. Restarting the Mac service does not by itself prove the Windows agent reconnected. Address discovery and fallback routes can fail independently of Tailscale.

The optional Windows installer autostart/watchdog is recovery support, not a guarantee of uninterrupted service. Check its logs and installed task definitions on the target machine. Do not reset a request cursor, delete unknown tasks, or restart another owner's active process to make health output look clean.

Keep the actual Tailscale addresses and any discovery configuration in private configuration. When a VPN or proxy interferes, inspect the effective route and TUN exclusions on both ends. Apply only the deployment's approved changes; do not overwrite the user's entire proxy configuration.

## Windows and RStudio boundaries

Windows must remain logged in and awake for desktop RStudio. A background R worker may outlive a lost Mac connection; its lifetime across RStudio closure depends on the worker implementation. Do not claim that closing RStudio is harmless without testing that exact setup.

Use version-supported ClaudeR startup procedures. Do not copy an old script that extracts fixed numbered statements from `claudeAddin()` into a different version. Verify discovery, the listening process, RStudio parent PID and process birth time. A listening port is not sufficient identity.

If WSL consumes memory, check Windows physical memory, committed memory, disk space, and measured model demand separately. Resource waits need not consume engine time. Memory limits and WSL shutdown affect other work: coordinate them under existing authority, preserve `.wslconfig`, and restore only the components intentionally stopped. Do not schedule global cache dropping as a default fix.

## Keep recovery small

Read one short current handoff, the queue, and the latest original receipt. Do not create another versioned runner directory for each observation. Fix the narrow transport or parser failure, then return to the approved scientific work. Model nonconvergence is a numerical problem once transport and execution evidence are sound.

Preserve failed attempts and cumulative budgets. A successful later probe does not turn an earlier unknown result into success. Large logs stay on Windows; small receipts should identify their path, size, hash, and omission status.
