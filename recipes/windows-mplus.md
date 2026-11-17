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
- 邊界：本池不讀寫共用 `HostReservations`，看不見其他佇列與 R／Stata 預約。主機級嚴格准入是 W1 的 `GeneralBatchAdmission`（長壽命 Python 原 owner 持有程序與 Job 句柄，Windows 端只啟動它並讀狀態），尚未接到本池。接線要把池正文的起程序核心（`BatchAdmit` 判定、`Start-Process`、終態）移進該 owner 內的 driver；只把 `BatchAdmit` 換成一次短命 acquire 不夠。靜態 `BATCH_ONLY` 聲明不能寫成嚴格准入已完成。`--model-timeout`（預設不設）只結束池自己起的直接子程序，沒有 Job 物件，不收子孫程序。
- 優先級：`--priority`（預設 Normal 並關閉 EcoQoS）凍結在本批池正文，只作用於本批 Mplus 子程序；RUNNER 與其他 Stata／R／detach 任務不變。

批次清單在 `OUT/_xdev_mplus_batch.json`，含任務回執、各模型 PID、Windows 核心起訖時間、Stopwatch 墻鐘、CPU 秒與取回狀態；`max_alive_observed` 是同一次掃描時仍在跑的子程序數最大值，`kernel_interval_peak` 是由起訖時間算出的區間重疊峰值，兩者分開記。Windows 端工作目錄是 `%LOCALAPPDATA%\xdev\work\<批次名>\`，各模型在其下 `models\<檔名>\`，池心跳是同目錄 `xdev_pool.json`；具名任務本身在 `jobs\<批次名>`。

歷史實測（i7-1260P 16 執行緒，均為已發布的 d9f2d02 前版本，非本後繼版）：2026-11-16 同一合成 Monte Carlo 模型 4 份同時跑，Windows 跨度 32.0 s；`--jobs 1` 一個一個跑 70.9 s。2026-11-17 在具名任務版基線上 5 個模型（4 個 Monte Carlo＋1 個讀共用資料）同時跑 35.9 s，5 個 PID 的起訖區間互相重疊（由各程序記錄的起訖時間算出，當時沒有同一時刻存活數的觀測）；`--jobs 1` 91.8 s。兩次的 MODEL RESULTS 在並行與串行之間逐字相同，差異只有 `.out` 裡的起訖時間。本後繼版只做過 stub 程式的功能對照，未做 Mplus 速度基準。

## 合成冒煙

只有在獲准使用 Mplus 的窗口才執行：

```sh
xdev mplus examples/smoke_mplus.inp --input examples/smoke_mplus.dat --out ./results/mplus-smoke
```

範例含合成資料，不代表複雜模型、整夜長測或新機完整驗收已通過。
