# Recipe: Mac agent → Windows RStudio/ClaudeR → R/MplusAutomation

[繁體中文](windows-rstudio-clauder.md) · [Architecture](../docs/architecture.md)

This route sends work into a verified Windows RStudio session. A Windows-local agent can invoke the same R script through local ClaudeR without routing through a Mac.

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

Prepare the original scientific driver on Windows, using `xdev push` and a SHA check. `submit.R` can source that approved driver from a project path already configured in the R session:

```r
project_root <- Sys.getenv("XDEV_PROJECT_ROOT")
stopifnot(nzchar(project_root))
source(file.path(project_root, "run_batch.R"), local = TRUE)
```

```sh
xdev rstudio submit --state ./rstudio-state --request-id batch-01 --job-id batch-01 --code-file submit.R
xdev rstudio poll --state ./rstudio-state --job-id batch-01
xdev rstudio result --state ./rstudio-state --request-id batch-01
```

Submit one approved batch. Load MplusAutomation once inside the asynchronous Windows R worker and process members there. Keep one low-frequency Mac observer of stage, message, updated_at and the original job ID. Avoid transferring a full work directory for every short model.

## 3. Preserve the request through unknown outcomes

- On 124 or UNKNOWN, use `result` with the original request ID. Do not submit again. Reconcile a late binding with `--request-id bind`.
- Reuse a saved terminal result. Some older interfaces consume an in-memory terminal job; `not_found` does not prove it never ran.
- A crash can occur between HTTP execution and durable receipt writing. The interface is not an exactly-once guarantee across every failure.
- The scientific worker needs its own durable terminal state, original model caps and cumulative budget. Do not turn unknown orphan runtime into zero.
- Read a discovery token locally on Windows and send `X-Clauder-Token`; do not copy it to the Mac.

New `xdev rstudio` wire receipts are bounded to 64 KiB on Windows. Larger originals remain there, with an explicit omitted-output summary and the original path, size, SHA and line count. This is not a result-file transfer limit.

## 4. Results and visibility

Run domain checks on the original output and create a separate permitted result-only export. `pull-dir` copies the whole selected directory; it does not exclude `data.dat`. Verify the export manifest before collection, then compare the exact received file set and hashes.

RStudio can show the received code and main-session output; not every asynchronous worker line must appear in its Console. A short Mplus process can finish before Task Manager is opened. Prove the route through R invocation, Mplus start/end records and saved output, not a Console screenshot alone.

Successful HTTP fallback is not successful native MCP. Obtain native MCP evidence with the workbench procedure when required. A Python process directly launching Mplus is also not equivalent to the requested RStudio → MplusAutomation route.

## 5. Parsing and Windows I/O lessons

- Split `XDEV_JSON` on byte LF. Python `str.splitlines()` can split legal Unicode separators inside JSON strings. Preserve raw stdout/stderr before parsing.
- Parse a complete multi-line JSON body, not its last line. Prefer a named durable receipt when progress text is mixed in.
- PowerShell 5.1 can misdecode JSON HTTP responses without a charset. Preserve the original bytes and decode as UTF-8 instead of guessing repairs to old evidence.
- Use shared read handles permitting both ReadWrite and Delete, and close them promptly, to avoid obstructing atomic replacement.
- Use `[IO.File]::ReadAllText` or an explicit `[string]` rather than serializing a `Get-Content -Raw` PSObject with provider properties into a huge receipt.
- Through a wrapper, `$PSCommandPath` may identify the wrapper rather than the scientific script. Record source-script, submitted-body and transport-payload hashes separately.
- Keep identity probes short. Do not put a full process inventory inside the R session's synchronous HTTP window. Inspect original artifacts after a parser failure; do not rerun the model.
