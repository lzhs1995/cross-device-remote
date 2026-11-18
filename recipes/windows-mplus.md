# 配方：直接執行 Windows Mplus

[English overview](../README.md#execution-routes) · [RStudio 路線](windows-rstudio-clauder.md)

只在任務允許直接引擎路線時使用。本配方不經 RStudio 或 MplusAutomation，不能替代它們的驗收。Windows 需已有合法 Mplus 安裝。

```sh
xdev health
xdev find-tools --write
xdev mplus model.inp --input synthetic.dat --out ./results/mplus-run --max 3600
```

輸入中 `DATA: FILE=`、`SAVEDATA` 與其他輸出路徑使用工作目錄內檔名；避免非 ASCII 模型路徑。`xdev` 使用私有設定的 `windows.mplus`，核對實際執行路徑，不假設 PATH 中的 `Mplus` 就是指定版本。

工具檢查 `.out` 是否包含正常終止訊息及 `*** ERROR`。Mplus 有時在模型失敗時仍回傳程序退出碼 0，因此不能只看 `rc`。正常終止也不足以證明變異數、標準誤、收斂、資訊矩陣或研究對比全部有效。

`--max` 只限制 Mac 等待，不會終止 Windows 引擎。需要嚴格有限估計時，使用具備已驗時間監督的領域 worker。超時沿原工作名 `tail`／`wait`，不重新提交。

工具取回整個工作目錄，包含輸入副本；限制外流資料請改為 Windows 本機資料與僅結果匯出流程。

## 獨立成員並行

上面的 `xdev mplus` 是提交、等待、取回的便利入口。逐一呼叫它會使呼叫方等待，不能據此認定 Windows 或 Tailscale 只能跑一個模型。任務明確允許直接引擎路線時，候選 [具名生命週期](../docs/concurrency.md#named-task-lifecycle) 可先用 `task-submit` 啟動多個已准入成員，再沿各原名 `task-status`，終態後 `task-collect`。它不替模型實作有限時間監督，driver 必須保留原引擎預算。

各成員獨占 cwd、輸出及 `TMP`／`TEMP`。先經共用 `HostReservations` 分別核可用 RAM bytes、commit 餘量、全部程序／執行緒及各卷未來成長；保留原 cap、`apim_v2` cap-1／cap-2 及僅前景契約。Stata 單工作階段保序不構成 Mplus 全機鎖。UNKNOWN 沿原名字與程序出生身分接回，不重投或清 claim。

候選介面未代表安裝或實機驗收。並行證據須有真實 Mplus PID／出生時間的重疊區間；短程序退出後讀不到 `MainModule`，不等於程序仍在執行。原始 `.out`、退出回執與科學驗收分開保存，速度提升另需同規格串行／並行牆鐘對照及數值核對。

## 多個模型同時跑

```sh
xdev mplus m1.inp m2.inp m3.inp m4.inp --input shared.dat --out ./results/batch --max 3600
xdev mplus --resume ./results/batch      # Mac 端等待中斷後接著看同一批，不重新提交
```

整批是一個[具名任務](../docs/concurrency.md#named-task-lifecycle)：本機先凍結意圖（每個 `.inp` 與共用檔的 SHA、池正文、原樣 RUNNER），原子預約名字、逐檔上傳各留回執、只啟動一次。Windows 本機程序池依准入同時起跑，每輪一個唯讀狀態作業看全部模型，先跑完的先用 `pull-dir` 取回到 `OUT/<檔名>/`；遠端 zip 只在本機全部核完後才刪，結果 UNKNOWN 時保留 zip 與 claim、不重取。最多 100 個模型，模型加共用檔最多 128 個。

- 隔離：每個模型獨占工作目錄與 `TEMP`／`TMP`。共用資料只傳一份，以唯讀硬連結放進各模型目錄，結束時核原檔 SHA；`SAVEDATA`、`MONTECARLO`／`DATA IMPUTATION` 的 `SAVE`／`RESULTS` 寫到共用檔名的模型，上傳前整批拒絕。
- 成功：退出碼 0、`.out` 無 `*** ERROR`／`n ERROR(S) FOUND`／`DID NOT TERMINATE NORMALLY`、正常終止（Monte Carlo 要完成數＝要求數）、取回完成，四項都要。命令退出碼 0＝全部模型通過且共用檔未變；1＝有模型失敗；7＝有結果 UNKNOWN；124＝`--max` 到時，Windows 照跑，用 `--resume OUT` 接著看。
- 准入只管本批（`capacity_scope: BATCH_ONLY`）：同時運行數 ≤ `--jobs`（預設全部）；`PROCESSORS` 之和 ≤ `--cpu-budget`（預設 Windows 邏輯 CPU − 2，首個模型也算，單個超預算就 `REFUSED`）；可用實體記憶體與可用 commit 各扣掉在跑模型尚未用到的預約後，仍不低於 `--mem-reserve-gib`／`--commit-reserve-gib` 加新模型的 `--model-mem-gib`；工作卷剩餘 ≥ `--disk-reserve-gib`。停住時印出原因與數字；沒有模型在跑仍停住超過 `--admission-wait` 秒就記 `NOT_ADMITTED`。
- 邊界：本池不讀寫共用 `HostReservations`，看不見其他佇列與 R／Stata 預約，也從不呼叫 W1 的 `GeneralBatchAdmission`；靜態 `BATCH_ONLY` 聲明不能寫成嚴格准入已完成。主機級准入走下方候選 `--native` 路線。`--model-timeout`（預設不設）只結束池自己起的直接子程序，沒有 Job 物件，不收子孫程序。
- 優先級：`--priority`（預設 Normal 並關閉 EcoQoS）凍結在本批池正文，只作用於本批 Mplus 子程序；RUNNER 與其他 Stata／R／detach 任務不變。

批次清單在 `OUT/_xdev_mplus_batch.json`，含任務回執、各模型 PID、Windows 核心起訖時間、Stopwatch 墻鐘、CPU 秒與取回狀態；`max_alive_observed` 是同一次掃描時仍在跑的子程序數最大值，`kernel_interval_peak` 是由起訖時間算出的區間重疊峰值，兩者分開記。Windows 端工作目錄是 `%LOCALAPPDATA%\xdev\work\<批次名>\`，各模型在其下 `models\<檔名>\`，池心跳是同目錄 `xdev_pool.json`；具名任務本身在 `jobs\<批次名>`。

歷史實測（i7-1260P 16 執行緒，均為已發布的 d9f2d02 前版本，非本後繼版）：2026-11-16 同一合成 Monte Carlo 模型 4 份同時跑，Windows 跨度 32.0 s；`--jobs 1` 一個一個跑 70.9 s。2026-11-17 在具名任務版基線上 5 個模型（4 個 Monte Carlo＋1 個讀共用資料）同時跑 35.9 s，5 個 PID 的起訖區間互相重疊（由各程序記錄的起訖時間算出，當時沒有同一時刻存活數的觀測）；`--jobs 1` 91.8 s。兩次的 MODEL RESULTS 在並行與串行之間逐字相同，差異只有 `.out` 裡的起訖時間。本後繼版只做過 stub 程式的功能對照，未做 Mplus 速度基準。

## Host-admitted batch (candidate)

```sh
xdev mplus m1.inp m2.inp --input shared.dat --native ~/private/xdev-native-binding.json --deadline 120 --jobs 2 --out ./results/native
xdev mplus --resume ./results/native     # 同一入口；按清單 schema 走 native 收尾，不重提、不重調 driver
```

`--native` 把起程序核心整個交給 W1 的長壽命 Python owner（`native_batch_driver` + `GeneralBatchAdmission`）。整批仍是一個具名任務（凍結意圖、逐檔上傳、只啟動一次），多傳兩個控制檔 `_xdev_native_plan.json`、`_xdev_native_admission.json`。任務正文不起 Mplus、不讀寫預約：先核 cwd 等於 `work_root\<批次名>`、任務名等於 batch_id、driver／adapter／backend／plan／config 五個 SHA、Python 存在、journal 與 `models\` 都還不存在，任一不符寫 `xdev_native_wrapper.json`（REFUSED）並退出 2；全部通過才用 `Start-Process` 同步呼叫 driver 一次（只有 `--plan`、`--admission-config`、`--adapter-module`、`--backend-module` 四個參數），把 driver 退出碼原樣交給 runner。

- 每個模型的 `working_directory`／`temp_directory` 是 `work_root\<批次名>\models\<檔名>\cwd` 與同層 `tmp`（driver 自建，父層不得預先存在）；輸入從任務目錄複製並核 SHA。request 欄位、admission config 欄位與 W1 adapter 的欄位集完全相同。
- 收件：Mac 等 runner 回執與 W1 `task_status` 核到 DONE，再一次讀出 driver journal（`RESULT.json`、各 attempt 的 `terminal.json`／`result.json`），逐個 `pull-dir` 取回 `working_directory`，並把 journal 取回到 `OUT/_native_journal/`。模型成功要 driver 記 `EXITED`、exit_code 0、無 validation_errors、`processes_ok`，attempt 檔與 RESULT 一致，取回完成，本機算的 `.out` SHA 等於 driver 記錄，`.out` 判定通過。批次成功另要原任務 rc 0、`admission_state` 為 `RELEASED`、沒有未決成員。
- 退出碼：0 全過；1 有失敗或 driver 沒建 journal（`NOT_STARTED`，原因在橋接回執）；7 有 UNKNOWN（RESULT 缺／半截／外批、未決成員、SHA 或 attempt 檔不符、pull 124），不重取、不重跑；124 是 `--max` 到時。`processes_ok` 與 `scientific_validation: NOT_PERFORMED` 只說明程序與檔案檢查，不是科學驗收。
- 現有限制直接拒絕、不靜默映射：`--deadline` 必填且 0 < S ≤ 180（driver 上限），較長的模型仍走上面的池；池參數（`--cpu-budget`、`--mem-reserve-gib`、`--commit-reserve-gib`、`--model-mem-gib`、`--disk-reserve-gib`、`--model-timeout`、`--admission-wait`、`--priority`）一律拒絕；沒有優先級／EcoQoS 選項；同批一個引擎；`--jobs`（K）≤ 模型數；檔名須為 ASCII `[A-Za-z0-9][A-Za-z0-9_.-]*`。

BINDING 是私有部署 JSON（Windows 路徑、預算、協調窗口），由批准的部署提供，不進倉庫。結構（值僅示意）：

```json
{"schema": "xdev-mplus-native-binding-v1",
 "work_root": "C:\\path\\to\\xdev\\work",
 "python": {"path": "C:\\path\\to\\python.exe"},
 "driver":  {"path": "C:\\path\\to\\native_batch_driver.py",   "sha256": "7412f8de…（必須等於 W1 審過的配對）"},
 "adapter": {"path": "C:\\path\\to\\general_batch_admission.py", "sha256": "27886689…"},
 "backend": {"path": "C:\\path\\to\\win_process.py",            "sha256": "aed6e241…"},
 "engine":  {"exe_path": "C:\\path\\to\\Mplus.exe", "exe_sha256": "<64 hex>"},
 "admission": {"helper_module": "…", "provider_directory": "…", "registry": "…", "original_controller": "…",
               "policy_id": "…", "policy": {"volume_reserve_bytes": {"C:\\": 20000000000}, "…": "…"},
               "external_evidence": {"path": "…", "sha256": "<64 hex>"}, "overhead": {"…": "…"}},
 "model_budget": {"physical_bytes": 1, "commit_bytes": 1, "output_budget_bytes": 1, "temp_budget_bytes": 1}}
```

本機只核結構、三個 SHA 配對、本地絕對路徑（無 `%VAR%`、UNC、`..`、流名）、引擎檔名 `Mplus.exe`、C: 保留量 ≥ 20,000,000,000 B 與正整數預算；策略、協調窗口與 provider 由 Windows 上的 adapter 原樣核驗。候選實作、Windows 依賴部署、安裝載入與實機 native 運行是四件分開的證據，本 checkout 不代表任何一件已完成。

## 合成冒煙

只有在獲准使用 Mplus 的窗口才執行：

```sh
xdev mplus examples/smoke_mplus.inp --input examples/smoke_mplus.dat --out ./results/mplus-smoke
```

範例含合成資料，不代表複雜模型、整夜長測或新機完整驗收已通過。
