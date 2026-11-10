# 配方：Windows Rscript 批次

[English overview](../README.md#execution-routes) · [RStudio 路線](windows-rstudio-clauder.md)

適用於任務允許獨立 `Rscript` 的情況；若指定 RStudio／ClaudeR，請改用上方配方。套件與授權軟體先在 Windows 安裝，不在每個批次臨時下載。

```sh
xdev health
xdev find-tools --write
xdev rscript analysis.R --input synthetic.csv --out ./results/r-run --max 3600
```

`find-tools --write` 只補未設定的軟體路徑；如需固定 R 版本，核對私有設定中的 `windows.rscript`。腳本用工作目錄相對路徑，明確載入套件，並保存具名結果與成功標記。實際執行時記錄 R 版本、套件版本與 `.libPaths()`，不要把 RStudio 套件庫當成 Rscript 套件庫。

`rscript` 會建立新的 Windows 工作目錄、推送輸入、背景執行，結束後取回**整個目錄**並核 SHA；所以本配方範例只使用合成資料。限制外流的資料應保留在 Windows，以專案 worker 執行後另做僅結果匯出。

`--max` 是 Mac 等待上限，不是 Windows 引擎終止預算。若顯示 `XDEV_STILL_RUNNING`，保留原工作名，用 `xdev tail NAME`／`xdev wait NAME --max 3600` 接續；不再執行同一 `rscript` 命令。之後依原工作目錄明確取回結果。

退出碼、腳本終態、具名產物與領域檢查分開核對。最後一行 `XDEV_TASK` 的 `ok` 只描述工具層結果，不代替科學驗收。

## 合成冒煙

在已批准的測試窗口，從倉庫根目錄執行：

```sh
xdev rscript examples/smoke.R --out ./results/r-smoke
```

這會真的啟動 Windows R，但不呼叫 Mplus。回傳目錄必須全新或為空。
