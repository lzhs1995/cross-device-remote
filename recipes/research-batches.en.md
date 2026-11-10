# Recipe: research batch continuation and efficiency

[繁體中文](pp-lgcm-mplus-continuation.md) · [RStudio route](windows-rstudio-clauder.en.md)

These are general lessons from research use. Private results, machine paths, progress ledgers and receipts are not distributed. Model design belongs to `mplusautomation-guide`; xdev does not introduce another scientific scheduler.

## Keep one contract

Read a short current handoff, the queue and original job receipt. Pin the scientific script, input hashes, budgets, completed members and unresolved outcomes. Use version control for changes instead of copying a runner directory at every stage.

Mac-remote and Windows-local agents use the same RStudio/ClaudeR → R → MplusAutomation → Mplus route. Any process supervisor must remain inside the approved domain invocation path. A Python coordinator directly starting the engine does not prove that route was used.

## Start promptly after a grant

Prepare a READY record binding the approved plan, script hashes, RStudio identity, remaining budget and original submission. A domain-specific lightweight observer can wait for a shared grant, persist a claim, then submit the original request once. Check expiry again when Windows actually enters the R code.

Grant, claim and engine start are different events. Keep expired-before-execution, uncertain acceptance and model failure separate. Reconcile UNKNOWN with the original request/job; do not erase the claim or use a new ID to replay it. This watcher is not a universal built-in xdev service.

## Keep the batch on Windows

Transfer code once, reuse authorized Windows-local data, and process the approved batch inside one worker. A single low-frequency Mac observer reads short progress. Avoid restarting R, packaging and collecting everything for each tiny model.

Measure grant-to-submit, engine runtime, export, collection, and engineering preparation separately. Roundtrip overhead includes queues, startup, polling and packaging, not just network latency. One successful interval's engine fraction is not a controlled comparison with Windows-local development.

## Recover without resetting budgets

A Mac disconnection or expired wait does not prove the Windows worker stopped. Inspect original engine and orphan-output evidence before restarting unfinished members. A member may have completed before its receipt was written.

Check original runtime caps before accepting success and again before adoption/export. An exit code of zero after the cap is still TIMEOUT under the original contract. Do not infer unknown runtime from modification time or count it as zero. Unsettled cumulative budgets remain HOLD rather than being silently increased.

STOP semantics belong to the domain worker, such as finishing the current member under its original cap and starting no more. Do not kill another owner's engine. Resource waiting can have a reasonable bounded duration without consuming scientific engine seconds, where the original contract permits it.

## Collect deliberately

Perform independent Windows output/statistical checks, then create a result-only export. `pull-dir` collects every file in the chosen directory. Check the manifest before transfer and the exact received file set afterward, so an input copied in after preflight is detected.

Engine completion, independent acceptance and Mac collection are separate states. A small receipt and SHA summary are not complete collection. Do not rerun finished models because a disk floor prevents transfer.

Measure Windows RAM, commit and disk separately from Mac collection space. Recheck Mac space immediately before transfer. Preserve the project's margin and original precision; do not delete another task's data or lower a threshold to manufacture a pass.

## Large historical sessions

With an existing snapshot, transfer a bounded increment after validating the original prefix hash and the source identity, size and modification time. Recompute the full hash from snapshot plus increment on receipt. Preserve failed attempts if the prefix or source changes.

Keep private original sessions separate from public lessons. Do not run naive redaction over opaque encrypted session blocks. Publish curated plaintext documentation instead of the original session.

Once execution evidence is sound, slow convergence, negative variance and information-matrix anomalies need separate finite numerical plans. They are not reasons to keep rebuilding the remote transport.
