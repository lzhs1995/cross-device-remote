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

## 合成冒煙

只有在獲准使用 Mplus 的窗口才執行：

```sh
xdev mplus examples/smoke_mplus.inp --input examples/smoke_mplus.dat --out ./results/mplus-smoke
```

範例含合成資料，不代表複雜模型、整夜長測或新機完整驗收已通過。
