# Concurrency contracts

[Project](../README.md) · [繁體中文入口](../README.zh-TW.md) · [RStudio recipe](../recipes/windows-rstudio-clauder.en.md)

These are **source candidate interfaces**. Check the exact checkout and its `--help` before use; an installed older `xdev` may not expose them. Source checks, installation, active-client loading, simultaneous Windows execution and a speed benchmark are separate acceptance steps. This document claims neither a live four-Mplus-PID test nor a speedup.

## Choose the concurrency layer

| Layer | What can overlap | Required boundary |
| --- | --- | --- |
| Legacy control queue | Short launch/status requests are still ordered | A command queue is not the Windows compute capacity |
| Named tasks | Independent child processes after launch | Unique name, output directory and temporary directory |
| RStudio fanout | Approved independent asynchronous R workers | One original job per member; external host admission |
| One mutable Stata session | Commands execute in order | Lease the bound session/port and its shared execution state |
| Independent Stata sessions | Work allowed by the original engine grant | Preserve the existing Stata engine cap; it is not a Mplus cap |
| Candidate control channel | Explicit independent jobs with disjoint resource keys | Separate server/state; serial jobs remain barriers |

Stata supports frames and can hold several datasets. Frames do not make a shared interpreter's commands, macros, returned results, log handles or working directory independent. A session lease must cover those mutations together. It must not become a lock on unrelated RStudio workers or every Mplus process on the host.

Existing accepted caps, finite budgets, `apim_v2` cap-1/cap-2 contracts and foreground-only execution contracts remain in force for their original runs. A new interface does not enlarge those grants or turn foreground work into background work. UNKNOWN attempts retain their original identity and spent budget.

## Host admission is separate

Reuse the host owner's existing **`HostReservations`** mechanism across queues. Do not create a second ledger per agent or per fanout run. This repository does not provide that host-wide admission implementation.

Before admitting a new member, account independently for:

- Physical available RAM in bytes, with a safety margin.
- System commit headroom in bytes (`commit limit - committed bytes`), with its own margin.
- All relevant host processes and their thread/CPU budgets, including other queues and R/Stata helpers.
- Free bytes and expected peak output/scratch growth on each affected volume.
- Full reservations for members not yet started and remaining growth reservations for running members, without charging an observed allocation twice.

Admission and reservation must be one atomic operation in that shared host mechanism. Reconcile a reservation with exact process birth/ownership and durable terminal evidence before releasing it; a missing observation is not a zero-cost job. A memory percentage alone proves neither a one-model limit nor four-model capacity. One process's present memory use is not its remaining peak budget.

## Named task lifecycle

From this checkout:

```sh
./scripts/xdev task-submit member-a.ps1 --name example-member-a --input model-a.inp
./scripts/xdev task-submit member-b.ps1 --name example-member-b --input model-b.inp
./scripts/xdev task-status example-member-a --bytes 2000 --wait 20
./scripts/xdev task-status example-member-b --bytes 2000 --wait 20
./scripts/xdev task-collect example-member-a --out ./results/member-a
```

Submit both approved independent members before waiting for either to finish. `task-submit` atomically reserves a new name, uploads inputs and launches once. It does not wait for completion, reserve host capacity or enforce an engine timeout. The scientific driver still owns its finite runtime budget. Do not use this route to substitute for required RStudio/ClaudeR execution.

New tasks use `task-claims/<name>.json`, `jobs/<name>/` and `work/<name>/` beneath the configured Windows root. Each child has its own cwd and `TMP`/`TEMP`. Case-insensitive duplicate input basenames and existing names are refused. `task-status` joins the original name; `task-collect` requires a durable `DONE` exit receipt and collects the entire work directory, including inputs. Explicit collection can retain a failed task's nonzero exit code. UNKNOWN/LOST is not permission to submit a replacement or remove a claim. Legacy named tasks are not migrated.

New submissions also retain a local task journal containing the original owner, frozen body/runner, input hashes, reservation/start requests and each input's transfer receipt. The file layer calls `receipt_callback` after writing its intent and before staging or enqueueing; a callback failure submits nothing. `task-status` reconciles those original signed results through `recover_push`, without uploading inputs or starting execution.

After an interrupted upload has a verified original terminal success, explicitly continue the original task:

```sh
./scripts/xdev task-status example-member-a
./scripts/xdev task-resume example-member-a
```

`task-resume` may upload only remaining inputs that have never been attempted, then start the original owner/body once after local and remote input hash checks. Existing upload, reservation and start attempts are reconciled by their original IDs; FAILED or unresolved attempts are preserved. A process crash before the transport returned its queue ID can leave insufficient evidence: the command refuses to guess an ID or re-enqueue. Keep the original local state and configuration. Legacy tasks without this journal remain observable; they cannot be silently converted to resumable tasks.

## RStudio fanout

Bind the real session using the [RStudio recipe](../recipes/windows-rstudio-clauder.en.md). Pre-provision nonoverlapping Windows work/scratch directories and prepare approved independent R member scripts. Shared inputs may be reused only under a read-only contract. Obtain an external host grant before submission; do not manufacture a receipt to satisfy the parser.

The external JSON receipt must contain:

```json
{
  "schema_version": 1,
  "status": "GRANTED",
  "grant_id": "external-grant-id",
  "run_id": "example-run",
  "binding_id": "exact-original-binding-id",
  "max_concurrency": 2,
  "expires_at_utc": "<actual future ISO8601 timestamp with timezone>"
}
```

`fanout.json` pins its exact bytes and each member's UTF-8 source bytes by lowercase SHA-256. Replace all digest placeholders with actual hashes:

```json
{
  "schema_version": 1,
  "batch_concurrency": 2,
  "external_host_admission": {
    "receipt_file": "host-grant.json",
    "receipt_sha256": "<64 lowercase hex characters>"
  },
  "members": [
    {
      "member_id": "member-a",
      "code_file": "member-a.R",
      "code_sha256": "<64 lowercase hex characters>",
      "independent": true,
      "remote_work_dir": "C:\\xdev-runs\\example-run\\member-a-work",
      "remote_temp_dir": "C:\\xdev-runs\\example-run\\member-a-temp"
    },
    {
      "member_id": "member-b",
      "code_file": "member-b.R",
      "code_sha256": "<64 lowercase hex characters>",
      "independent": true,
      "remote_work_dir": "C:\\xdev-runs\\example-run\\member-b-work",
      "remote_temp_dir": "C:\\xdev-runs\\example-run\\member-b-temp"
    }
  ]
}
```

The schema allows 1–128 members and batch concurrency 1–32; those parser limits are not permission or host capacity. Ordinary absolute drive-letter paths are required. UNC/device paths and overlapping member directories are refused.

```sh
./scripts/xdev rstudio fanout-submit --state ./rstudio-state --run-id example-run --contract ./fanout.json
./scripts/xdev rstudio fanout-poll --state ./rstudio-state --run-id example-run --contract ./fanout.json --max-polls 8
./scripts/xdev rstudio fanout-merge --state ./rstudio-state --run-id example-run --contract ./fanout.json
```

`fanout-submit` fills currently available batch slots without waiting for models to finish. `fanout-poll` fairly rotates at most `--max-polls` original-job queries and does not start pending members. After terminals free slots, call `fanout-submit` again with the **same** contract/run to start only never-submitted members, while the original grant remains valid. A FAILED or UNKNOWN member blocks new submissions; original members can still be observed. Keep one aggregate observer rather than waiting to completion after each submission.

State lives at `<rstudio-state>/fanout/<run-id>/`; run IDs must be lowercase. Original code snapshots, request/job IDs, grant bytes, directory identities and binding are pinned before transport. Per-run locking is not a host lease. Do not edit the contract, replace a member ID or delete original intent to recover.

The wrapper sets each asynchronous worker's cwd and `TMP`/`TEMP`/`TMPDIR`, then runs the original member code. It does not alter the main RStudio session. **The callr R process already exists when this wrapper runs:** changing those variables does not relocate its existing `tempdir()`. Verify the installed callr/ClaudeR startup configuration if R's own temporary directory must be pinned before process creation.

Receipts explicitly report `capacity_scope: BATCH_ONLY`, `external_host_admission_enforced_by_xdev: false` and `native_mcp_verified: false`. xdev validates the pinned receipt fields and expiry before new submissions; it neither independently proves the grant nor atomically reserves host resources at Windows execution time. The host integration must enforce that part.

`fanout-merge` writes `MERGED.json` only when every original asynchronous terminal success matches its archived request, raw transport and binding. Its status is `LIFECYCLE_MERGED`; `scientific_acceptance` remains `NOT_EVALUATED`. It neither executes scientific merge code nor transfers result files. Collect permitted outputs and perform the domain's numerical/convergence checks separately.

## Independent control channel candidate

The new channel is a separate opt-in server and worker, currently using wire version **`xdev-control/1`**. It is not an upgrade in place of the legacy queue. It does not read or migrate legacy cursors/configuration; choose new state directories and preserve old requests under their original controller.

Use `python3 scripts/xdev_control.py --help` on the Mac. On Windows use the approved Python executable in `$PythonExe`, with the same source available. Set `$CONTROL_URL`, `$CONTROL_SECRET`, `$CHANNEL_ID`, `$MAC_BIND_ADDRESS` and `$WINDOWS_ALLOWED_IP` from private pairing configuration; on Windows the equivalent private values are `$ControlUrl`, `$ControlSecret`, `$ChannelId` and `$ControlWorkerState`.

```sh
python3 scripts/xdev_control.py serve --state ./private-control-controller \
  --secret-file "$CONTROL_SECRET" --channel-id "$CHANNEL_ID" \
  --host "$MAC_BIND_ADDRESS" --port 8771 --allow-ip "$WINDOWS_ALLOWED_IP"
```

```powershell
& $PythonExe .\scripts\xdev_control.py worker --state $ControlWorkerState `
  --secret-file $ControlSecret --channel-id $ChannelId --url $ControlUrl `
  --max-workers 2 --poll 0.5 --seconds 3600
```

```sh
python3 scripts/xdev_control.py capabilities --url "$CONTROL_URL" --secret-file "$CONTROL_SECRET"
python3 scripts/xdev_control.py run --url "$CONTROL_URL" --secret-file "$CONTROL_SECRET" \
  --id example-independent-a --independent --resource output:example-a \
  --timeout 30 --wait 0 -- powershell.exe -NoProfile -Command 'Write-Output "example-a"'
python3 scripts/xdev_control.py status --url "$CONTROL_URL" --secret-file "$CONTROL_SECRET" --id example-independent-a
```

The exact job spec fields are `version`, `op: exec`, `key`, `lane`, `resource_keys`, `body: {argv: [...]}`, `timeout` and `output_limit`. Default `lane: serial` is a barrier within this channel. `--independent` requires at least one `--resource`; shared keys conflict, disjoint keys may overlap. Use a real shared key such as `stata:session-a:port-a` for commands touching the same session, not one key per command. This lease is channel-scoped and is not host RAM/commit admission or protection against work outside the channel.

The worker accepts 1–16 dispatch slots; the controller caps active claims at 16. Each job receives a unique cwd and scratch environment. On Windows, supervised commands and descendants belong to that invocation's Job Object. `--timeout` is the command execution deadline (1–3600 seconds); `--wait` is only the observer budget. This is not a detached desktop launch route. Restart uncertainty becomes UNKNOWN and retains resource reservations; result posting is idempotent. Reuse the exact original ID/spec, inspect `status`, and never reset a cursor or kill an unrelated process to recover.

## Evidence and timing

For actual parallel acceptance, save overlapping start/end intervals for multiple real Mplus PIDs, each bound to birth time, executable, member, input hash and terminal output. Four accepted asynchronous submissions alone do not prove four simultaneous engines. A short process may exit before a later `MainModule` lookup; that failed lookup proves neither a live process nor failed model execution. Keep process observation and durable terminal evidence separate.

Measure local durations with a monotonic clock on the host that owns each interval. Preserve UTC timestamps for correlation and record cross-host clock skew/uncertainty; subtracting Mac and Windows wall-clock timestamps does not prove sub-two-second latency. A speedup claim needs the same model/input/thread settings, serial and parallel wall-clock runs, resource observations and numerical agreement. Local protocol tests and a proposed benchmark plan do not establish that result.

Fanout progress compares poll timestamps only within the same process monotonic-clock domain. Old records and separate CLI invocations normally report `observed_poll_interval_seconds: null`; UTC remains a locator, not a substitute duration. The full local `PROGRESS.json` is separate from the bounded 64 KiB wire summary, which reports omitted progress and the full report's path/hash.
