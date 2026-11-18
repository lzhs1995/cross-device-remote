---
name: cross-device-remote
description: Run authorized Windows work from a Mac agent through xdev and a private network such as Tailscale. Supports PowerShell, durable background jobs, SHA-checked transfers, and verified Windows RStudio/ClaudeR routes. 從 Mac agent 遠端執行 Windows 工作，包含 PowerShell、背景任務、檔案校驗與 RStudio／ClaudeR；保留原請求及科學流程。
---

# cross-device-remote · xdev

[English project guide](README.md) · [繁體中文專案指南](README.zh-TW.md)

## Entry / 入口

```sh
"$HOME/.agents/skills/cross-device-remote/scripts/xdev" health
```

Use the installed absolute entry, or add its `scripts` directory to PATH and use `xdev`. Configuration is private at `~/.config/cross-device-remote/config.json`; `XDEV_CONFIG` can select another file. Never commit private configuration, state, actual device addresses, tokens or research data.

使用已安裝的絕對入口，或將 `scripts` 加入 PATH 後呼叫 `xdev`。設定及密鑰留在私有位置，不把真實位址、工作階段或研究資料放進 skill。安裝詳見 [English](docs/installation.md)／[繁體中文](docs/installation.zh-TW.md)。

## Choose the route / 選對路線

| Route / 路線 | Command / 命令 |
| --- | --- |
| Short PowerShell / 短命令 | `xdev run job.ps1` |
| Original late result / 原遲到結果 | `xdev result ID` |
| Background job / 背景工作 | `xdev detach NAME job.ps1`; `xdev tail NAME`; `xdev wait NAME --max 3600` |
| Candidate named lifecycle / 候選具名生命週期 | `xdev task-submit`, `task-status`, `task-resume`, `task-collect` |
| RStudio / ClaudeR | `xdev rstudio bind/execute/submit/poll/result` |
| Candidate R fanout / 候選 R 並行成員 | `xdev rstudio fanout-submit/fanout-poll/fanout-merge` |
| Standalone R / 獨立 R | `xdev rscript analysis.R --out ./results/run` |
| Direct Mplus / 直接引擎 | `xdev mplus model.inp --input synthetic.dat --out ./results/run` |
| Many Mplus at once / 多個 Mplus 同時跑 | `xdev mplus a.inp b.inp c.inp --input data.dat --out ./results/batch`; later `xdev mplus --resume ./results/batch` |
| Candidate host-admitted batch / 候選主機級准入批次 | `xdev mplus a.inp b.inp --input data.dat --native BINDING.json --deadline 120 --out ./results/batch` |
| Files / 檔案 | `xdev push`, `xdev pull`, `xdev pull-dir` |

If the user requires RStudio/ClaudeR, use that route. Rscript and direct Mplus are separate routes and cannot substitute for its acceptance. A Windows-local agent uses local ClaudeR and the same approved scientific R script; it need not route through a Mac.

使用者指定 RStudio／ClaudeR 時，必須沿該路線；獨立 Rscript 或直接 Mplus 成功不能代替。Windows 本機 agent 透過本機 ClaudeR 使用同一科學腳本，不因所在機器不同而改模型。

Candidate commands refer to this source checkout, not a claim about the installed entry. Check that entry's `--help`. Candidate implementation, installation, active-client loading, simultaneous real Mplus PIDs and benchmark results are separate evidence. The independent control candidate is invoked through `python3 scripts/xdev_control.py`; it uses its own server/state and never migrates a legacy cursor.

候選命令只代表本 checkout，不代表已安裝入口可用；先核該入口的 `--help`。候選實作、安裝、客戶端載入、真實 Mplus PID 同時運行與速度對照分別驗證。新 control 候選由 `python3 scripts/xdev_control.py` 呼叫，使用獨立 server／state，不遷移舊 cursor。

For a new named task interrupted during upload, `task-status NAME` reconciles its saved original transfer receipts without starting execution. `task-resume NAME` continues only never-attempted steps under the original owner and frozen input/body hashes. An unresolved original upload/start is not resubmitted. Keep the same configuration and local task journal; see [recovery details](docs/concurrency.md#named-task-lifecycle).

新具名任務上傳中斷時，`task-status NAME` 只核原傳輸回執；`task-resume NAME` 沿原 owner 與固定輸入／程式 SHA 接續未曾嘗試的步驟。未決上傳或啟動不重提；保留原設定與本機任務紀錄。

## Run Mplus models concurrently / 多個 Mplus 同時跑

Windows runs many Mplus processes at once; xdev does not serialize them. Pass every independent `.inp` to one `xdev mplus` call (up to 100 models; models plus shared inputs at most 128 files). The batch is one [named task](docs/concurrency.md#named-task-lifecycle): intent and SHAs are frozen locally, each file is uploaded with its own transfer receipt, and the pool starts once. One read-only status job per round watches all models, and each finished model is pulled with `pull-dir` into `OUT/<inp stem>/`. Each model has its own working directory and its own `TEMP`/`TMP`. Shared `--input` files are uploaded once, hard-linked read-only into every model directory and SHA-checked at the end; a model whose `SAVEDATA`/`SAVE` names a shared file is refused before upload.

A model passes only if its exit code is 0, its `.out` has no `*** ERROR`, `n ERROR(S) FOUND` or `DID NOT TERMINATE NORMALLY`, it terminated normally (Monte Carlo: completed = requested replications) and its collection completed. Exit 0 = every model passed and shared inputs are unchanged; 1 = some model failed; 7 = some result is UNKNOWN (never re-pulled or resubmitted); 124 = `--max` ended the local wait while Windows keeps running. Continue with `xdev mplus --resume OUT`; it never resubmits.

Admission covers this batch only (`capacity_scope: BATCH_ONLY`) and uses numbers: running models ≤ `--jobs` (default: all); the sum of `PROCESSORS` ≤ `--cpu-budget` (default Windows logical CPUs − 2, first model included; a model above the budget is refused); Windows available physical memory and available commit, each minus what running models reserved but have not used yet, must stay ≥ `--mem-reserve-gib` / `--commit-reserve-gib` plus `--model-mem-gib` for the new model; the work volume keeps ≥ `--disk-reserve-gib` free. A hold prints its reason and numbers; a model still held with nothing running after `--admission-wait` seconds is `NOT_ADMITTED`. The pool does not read shared `HostReservations` and cannot see other queues or R/Stata jobs. Strict host admission is W1's `GeneralBatchAdmission`, a long-lived Python owner that holds the process and Job handles. The pool never calls it; never report the static `BATCH_ONLY` declaration as strict host admission. The candidate `--native` route below hands the whole launch core to that owner. `--model-timeout` kills only the pool's own direct child; without a Job object, descendants are not closed.

`--priority` (default Normal, EcoQoS off) applies only to this batch's Mplus children; the frozen runner and other Stata/R/detach tasks are unchanged. Do not wait for "Mplus count = 0"; do not loop single `xdev mplus` calls.

**Candidate `--native BINDING --deadline S` (`capacity_scope: GENERAL_BATCH_ADMISSION`).** The batch is still one named task, but its body starts no Mplus. It checks the work directory, the SHA of driver, adapter, backend and the uploaded plan/config, and the SHA of `python.exe`; the same interpreter then reports its version, architecture and the psutil it actually imports, which must equal BINDING. Only then does it run W1's `native_batch_driver` once and pass its exit code to the runner. Reservation, suspended creation, Job, terminal proof and release all stay in that Python process. Each attempt id is `<batch>.<model>` because the adapter keeps (controller, attempt_id) forever. After the task is DONE the Mac pulls the plan's whole journal once and verifies it against the frozen plan: `PLAN.json`, the file set, canonical record bytes, `RESULT` equal to the polled original with each plan attempt exactly once, the event sequence and each member's spawn/identity/terminal events, and the bridge and task exit codes. Only then does it pull each plan `working_directory` and require the `.out` SHA to equal the driver's record before the `.out` check. Journal missing = `NOT_STARTED` (1); `RESULT` missing, partial or foreign, a journal pull that failed or cannot be verified (`EVIDENCE_UNVERIFIED`, no model pulled), an unresolved attempt, a SHA mismatch, or a pull 124 = UNKNOWN (7, nothing re-pulled or rerun). `processes_ok` is not scientific validation. Current limits, refused instead of mapped: `--deadline` is required with 0 < S ≤ 180 (driver limit), so longer models stay on the pool; pool flags (`--cpu-budget`, `--priority`, `--model-timeout`, …) are refused; one engine per batch; K = `--jobs` ≤ batch size. BINDING is private (Windows paths, budgets, coordinated window, Python/psutil identity) and never enters the repo; its schema is in [the Mplus recipe](recipes/windows-mplus.md#host-admitted-batch-candidate). Installation, Windows dependency deployment and a native run are separate evidence; none is implied by this checkout.

Windows 可以同時跑多個 Mplus，xdev 不把它們排成一個一個跑。把所有獨立 `.inp` 一次交給 `xdev mplus`（最多 100 個模型，模型加共用檔最多 128 個）：整批是一個具名任務，先凍結意圖與 SHA、逐檔上傳留回執、只啟動一次；每輪一個唯讀狀態作業看全部模型，跑完的用 `pull-dir` 取回到 `OUT/<檔名>/`。各模型獨占工作目錄與 `TEMP`／`TMP`，共用資料唯讀硬連結、結束時核 SHA；寫共用檔名的模型上傳前就拒絕。成功要退出碼 0、`.out` 無錯誤且正常終止、取回完成；退出碼 0／1／7／124 分別是全過／有失敗／有 UNKNOWN／本機等待到時，用 `--resume OUT` 接著看，不重提。

准入只管本批（`BATCH_ONLY`），全用數字：`--jobs`、`PROCESSORS` 之和 `--cpu-budget`（首個模型也算）、可用實體記憶體與 commit 各扣在跑模型未用的預約後仍不低於保留量加新模型 `--model-mem-gib`、工作卷剩餘 `--disk-reserve-gib`。它不讀共用 `HostReservations`，看不見其他佇列與 R／Stata。主機級嚴格准入是 W1 的 `GeneralBatchAdmission`（長壽命 Python 原 owner 持有程序與 Job 句柄）；本池從不呼叫它，靜態 `BATCH_ONLY` 不能寫成嚴格准入已完成。候選 `--native BINDING --deadline S`：仍是一個具名任務，正文不起 Mplus，只核工作目錄、driver／adapter／backend／plan／config 與 `python.exe` 的 SHA，並用同一解釋器實測版本、架構與實際導入的 psutil 等於 BINDING，之後同步呼叫 driver 一次；預約、掛起建立、Job、終態與釋放都在該程序內。attempt_id 為 `<批次名>.<檔名>`（adapter 永久保留 (controller, attempt_id)）。Mac 等任務 DONE 後先把 plan 的整個 journal 取回一次，對照凍結 plan 核 PLAN、檔案集、規範位元組、RESULT 與輪詢原文相同且 plan 每個 attempt 恰好一次、事件序列與各成員的 SPAWN／身份／終態事件、橋接與任務退出碼；核過才逐個取回 plan 的 `working_directory`，`.out` SHA 須等於 driver 記錄再做 `.out` 判定。journal 缺＝NOT_STARTED（1）；RESULT 缺／半截／外批、journal 取回失敗或核不過（`EVIDENCE_UNVERIFIED`，一個模型都不取）、未決成員、SHA 不符或 pull 124＝UNKNOWN（7，不重取不重跑）。`processes_ok` 不是科學驗收。現有限制：`--deadline` 必填且 0 < S ≤ 180，較長模型仍走池；池參數一律拒絕；同批一個引擎。BINDING 私有（含 Python／psutil 身份），不進倉庫；安裝、Windows 依賴部署與實機運行分別驗證。`--model-timeout` 只結束池自己起的直接子程序。`--priority` 只作用於本批 Mplus。不要等「Mplus 數量為 0」，也不要一個一個呼叫 `xdev mplus`。

## Execution contract / 執行契約

1. **Verify each layer.** A fresh poll and command receipt establish command transport. RStudio needs real discovery/PID/birth/parent/listener identity; a model needs its own terminal evidence. Old `running` IDs and `ping` do not establish current execution.
2. **Submit once, reconcile by ID.** On timeout or UNKNOWN, use the original request/job. Do not reset cursors, delete claims, replay completed members, or treat unknown runtime as zero. Crash-proof exactly-once behavior is not guaranteed.
3. **Keep the shared queue short.** Long work uses a named detached task or approved asynchronous RStudio worker. `--max` is a wait limit, not an engine cap. The domain worker owns finite model budgets and shared-window rules.
4. **Submit independent members before waiting.** Prepare code before the Windows grant. Launch approved independent R async workers, then observe them together; do not wait for one model to finish before submitting the next. Fanout capacity is `BATCH_ONLY`, not atomic host admission. Reuse the host owner's `HostReservations` for RAM bytes, commit headroom, all process/thread budgets and per-volume growth. Preserve original caps, `apim_v2` cap-1/cap-2 and foreground-only contracts.
5. **Collect deliberately.** `pull-dir`, `rscript` and `mplus` collect the whole chosen directory, including input copies. For restricted data, create a result-only export and check its exact file set and hashes. Check receiver disk margin first.
6. **Preserve ownership and evidence.** Do not stop other sessions or modify a shared runtime merely because the channel is stale. Keep original failures, budgets and output. Use existing authority for maintenance; HMAC is not encryption, so use a trusted private path.

1. **分層核驗：** 新鮮輪詢與命令回執只證明傳輸；RStudio 需真實身分，模型需原終態。舊 `running` 與 ping 不代表正在執行。
2. **一次提交、原 ID 接回：** 超時或 UNKNOWN 沿原 request／job；不清 cursor、不刪 claim、不重跑已完成成員、不把未知耗時計零；不宣稱所有故障都恰好一次。
3. **共用佇列保持短小：** 長工作用具名背景工作或已批准非同步 RStudio；`--max` 不是引擎 cap，領域 worker 管理預算與窗口。
4. **先提交獨立成員再等待：** 獲窗前準備好，依准入啟動獨立非同步 R worker，再聚合觀察。fanout 只管 `BATCH_ONLY`，主機 RAM bytes、commit 餘量、所有程序／執行緒及各卷成長預算由共用 `HostReservations` 准入。保留原 cap、`apim_v2` cap-1／cap-2 及僅前景契約。
5. **明確取回範圍：** 整目錄命令包含輸入副本；受限資料另建僅結果匯出，核精確集合及 SHA，先查接收端空間。
6. **保留所有權與證據：** 不因通道舊就停止他方工作；保留失敗、預算與結果，維護沿既有授權。HMAC 不加密，需可信私有通道。

## Read only what the task needs / 按需閱讀

- Concurrency / 並行契約與候選介面: [Guide](docs/concurrency.md). One mutable Stata session leases its commands/macros/logs/cwd; frames do not remove that shared state. The lease does not cap unrelated Mplus/R workers. 同一 Stata 工作階段保序，不把其租約擴大為整機計算鎖。
- Authorized window close / 已授權窗口友好關閉: [Recipe](recipes/windows-owned-window-close.md). Exact PID/birth/path, responding window, one close, ten-second wait; preserve dialogs and do not kill/retry.
- RStudio/ClaudeR: [English](recipes/windows-rstudio-clauder.en.md) · [繁體中文](recipes/windows-rstudio-clauder.md).
- Research batches / 研究批次: [English](recipes/research-batches.en.md) · [繁體中文](recipes/pp-lgcm-mplus-continuation.md).
- Recovery / 恢復: [English](docs/recovery.md) · [繁體中文](docs/recovery.zh-TW.md).
- Acceptance / 驗收: [English](docs/validation.md) · [繁體中文](docs/validation.zh-TW.md).
- Other recipes / 其他配方: [R](recipes/windows-r.md), [Mplus](recipes/windows-mplus.md), [MplusAutomation](recipes/windows-mplusautomation.md), [PowerShell/WSL](recipes/windows-wsl-ops-lessons.md), [migration/disk](recipes/mac-restore-and-remote-acceptance-lessons.md).

Transport belongs here; session semantics belong to [clauder-rstudio-workbench](https://github.com/lzhs1995/clauder-rstudio-workbench); scientific design and validation belong to its `mplusautomation-guide`. The originating route has run research; this public package still needs fresh-machine verification. Installed links are not proof that every agent automatically loads the skill.

本 skill 管傳輸，workbench 管工作階段，`mplusautomation-guide` 管科學設計及驗收。來源路線有研究實績，但公開套件仍需新機驗證；安裝連結不等於所有 agent 自動載入通過。
