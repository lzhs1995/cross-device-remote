# Recipe: Mac agent → Windows RStudio/ClaudeR → R/MplusAutomation

[繁體中文](windows-rstudio-clauder.md) · [Architecture](../docs/architecture.md)

This route sends work into a verified Windows RStudio session. A Windows-local agent can invoke the same R script through local ClaudeR without routing through a Mac.

The `fanout-*` commands below are source candidates. Verify the selected checkout's help; this document does not establish installation, native MCP loading, four simultaneous Mplus PIDs or a speed benchmark.

## Prerequisites

- Windows remains logged in and awake, with R, RStudio and a compatible ClaudeR installed. Estimation also requires a licensed Mplus installation and the intended MplusAutomation version.
- Start a dedicated session using the installed version's [clauder-rstudio-workbench](https://github.com/lzhs1995/clauder-rstudio-workbench) procedure. Do not submit to a session you are not authorized to use.
- Check `xdev health` for the service and a fresh poll, not just old `running` IDs.
- Prepare the scientific script, inputs, finite budget, export rules and any shared Mplus execution window before submission.

Do not transplant old startup scripts that extract fixed numbered statements from `claudeAddin()`. Separate starting RStudio from waiting for initialization; a desktop process must not be accidentally tied to a short command's timeout tree.

## 1. Inspect the actual session identity

Read Windows discovery and process identity. Do not dump full discovery files, which may contain tokens.

```sh
xdev run -c 'Get-Process rstudio,rsession* -ErrorAction SilentlyContinue | Select-Object ProcessName,Id,StartTime | ConvertTo-Json -Compress'
xdev run -c 'Get-ChildItem (Join-Path $env:USERPROFILE ".claude_r_sessions") -Filter *.json | ForEach-Object { $s = [IO.File]::ReadAllText($_.FullName) | ConvertFrom-Json; [pscustomobject]@{session_name=$s.session_name; port=$s.port; pid=$s.pid; started_at=$s.started_at} } | ConvertTo-Json -Compress'
```

A Mac-local MCP `list_sessions` response is not a Windows session inventory. Set these variables from observed values; no sample PID or address is supplied. `bind` checks discovery, listener, R process birth and RStudio parent identity.

```sh
# Set SESSION_NAME, CLAUDER_PORT, R_PID and RSTUDIO_PID from the observed session.
xdev rstudio bind --state ./rstudio-state --session-name "$SESSION_NAME" --port "$CLAUDER_PORT" --r-pid "$R_PID" --rstudio-pid "$RSTUDIO_PID"
```

Preserve `rstudio-state`. If the R PID changes, reconcile the old job, outputs and budget before verifying the new identity and creating new state.

## 2. Inspect briefly, then submit once

A short `inspect.R` can contain:

```r
cat("R:", R.version.string, "\n")
cat("R PID:", Sys.getpid(), "\n")
cat("MplusAutomation:", as.character(packageVersion("MplusAutomation")), "\n")
```

```sh
xdev rstudio execute --state ./rstudio-state --request-id inspect-01 --code-file inspect.R
```

Keep existing serial/foreground-only contracts on their approved route and cap; the asynchronous examples below do not change them. For one member approved for async execution, prepare its scientific driver on Windows, using `xdev push` and a SHA check. Its `submit.R` can source the approved driver from a project path already configured for that worker:

```r
project_root <- Sys.getenv("XDEV_PROJECT_ROOT")
stopifnot(nzchar(project_root))
source(file.path(project_root, "run_member.R"), local = TRUE)
```

```sh
xdev rstudio submit --state ./rstudio-state --request-id member-01 --job-id member-01 --code-file submit.R
xdev rstudio poll --state ./rstudio-state --request-id member-01-poll-01 --job-id member-01
```

If the Mac's local wait expires before the original queue result arrives, recover that exact poll request:

```sh
xdev rstudio result --state ./rstudio-state --request-id member-01-poll-01
```

`result` reads a recorded request receipt; it does not query the async job again. Using the submit request `member-01` retrieves its submission receipt, which may only acknowledge startup. If the poll has a saved `UNKNOWN_HTTP_OUTCOME` receipt, repeated `result` calls return that same saved UNKNOWN. Preserve it without treating it as completion; you may issue a new read-only poll for the same original job under the same binding identity checks:

```sh
xdev rstudio poll --state ./rstudio-state --request-id member-01-poll-02 --job-id member-01
```

A poll that reports the job is still running can be followed in the same way. Each new poll uses a new request ID; omitting `--request-id` generates one, which appears in its response receipt. Never resubmit the model or overwrite the original UNKNOWN receipt. A poll that returns an already-saved terminal result creates no new request receipt and needs no `result` recovery.

For a new batch of approved independent models, use one asynchronous R worker per admitted member and submit the available members before observing completion. Each worker loads its own packages. Prepare the exact [fanout contract and external host grant](../docs/concurrency.md#rstudio-fanout), then use the source candidate:

```sh
./scripts/xdev rstudio fanout-submit --state ./rstudio-state --run-id example-run --contract ./fanout.json
./scripts/xdev rstudio fanout-poll --state ./rstudio-state --run-id example-run --contract ./fanout.json --max-polls 8
./scripts/xdev rstudio fanout-merge --state ./rstudio-state --run-id example-run --contract ./fanout.json
```

`fanout-poll` observes original jobs; it does not start pending members. Refill free slots with `fanout-submit` on the same run/contract while its grant is valid. UNKNOWN or FAILED blocks new submissions. `fanout-merge` only records verified lifecycle success; scientific aggregation and acceptance are separate. Keep one aggregate observer of stage, message, updated_at and original job IDs.

Fanout is `BATCH_ONLY`: reuse host-wide `HostReservations` for physical RAM bytes, commit headroom, all process/thread reservations and per-volume scratch/output growth. A Stata session lease does not cap Mplus on the host. Preserve existing caps, `apim_v2` cap-1/cap-2 and foreground-only contracts.

Each member needs unique writable output and scratch directories. The wrapper changes the async worker's cwd and `TMP`/`TEMP`/`TMPDIR`, but callr has already created the R process: its existing `tempdir()` is not relocated. Shared inputs remain read-only; avoid transferring the full work directory per short model.

## 3. Preserve the request through unknown outcomes

- When the original queue result is still pending, use `result` with that original request ID. A saved `UNKNOWN_HTTP_OUTCOME` permits a new read-only poll of the same job with a new request ID as above; retain the unknown original receipt and never submit the model again. Reconcile a late binding with `--request-id bind`.
- Reuse a saved terminal result. Some older interfaces consume an in-memory terminal job; `not_found` does not prove it never ran.
- A crash can occur between HTTP execution and durable receipt writing. The interface is not an exactly-once guarantee across every failure.
- The scientific worker needs its own durable terminal state, original model caps and cumulative budget. Do not turn unknown orphan runtime into zero.
- Read a discovery token locally on Windows and send `X-Clauder-Token`; do not copy it to the Mac.

New `xdev rstudio` wire receipts are bounded to 64 KiB on Windows. Larger originals remain there, with an explicit omitted-output summary and the original path, size, SHA and line count. This is not a result-file transfer limit.

## 4. Results and visibility

Run domain checks on the original output and create a separate permitted result-only export. `pull-dir` copies the whole selected directory; it does not exclude `data.dat`. Verify the export manifest before collection, then compare the exact received file set and hashes.

RStudio can show the received code and main-session output; not every asynchronous worker line must appear in its Console. A short Mplus process can finish before Task Manager is opened. Prove the route through R invocation, Mplus start/end records and saved output, not a Console screenshot alone.

`MainModule` may be unavailable after a short child exits; that does not establish a live process. Preserve terminal evidence separately. Prove concurrency with overlapping real Mplus PID/birth intervals, and measure local durations with a monotonic clock. Cross-host UTC differences need clock-skew uncertainty; they do not guarantee sub-two-second latency.

Successful HTTP fallback is not successful native MCP. Obtain native MCP evidence with the workbench procedure when required. A Python process directly launching Mplus is also not equivalent to the requested RStudio → MplusAutomation route.

## 5. Parsing and Windows I/O lessons

- Split `XDEV_JSON` on byte LF. Python `str.splitlines()` can split legal Unicode separators inside JSON strings. Preserve raw stdout/stderr before parsing.
- Parse a complete multi-line JSON body, not its last line. Prefer a named durable receipt when progress text is mixed in.
- PowerShell 5.1 can misdecode JSON HTTP responses without a charset. Preserve the original bytes and decode as UTF-8 instead of guessing repairs to old evidence.
- Use shared read handles permitting both ReadWrite and Delete, and close them promptly, to avoid obstructing atomic replacement.
- Use `[IO.File]::ReadAllText` or an explicit `[string]` rather than serializing a `Get-Content -Raw` PSObject with provider properties into a huge receipt.
- Through a wrapper, `$PSCommandPath` may identify the wrapper rather than the scientific script. Record source-script, submitted-body and transport-payload hashes separately.
- Keep identity probes short. Do not put a full process inventory inside the R session's synchronous HTTP window. Inspect original artifacts after a parser failure; do not rerun the model.
