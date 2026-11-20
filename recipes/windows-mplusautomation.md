# 配方：Windows R 與 MplusAutomation

[English overview](../README.md#execution-routes) · [完整 RStudio 中英文配方](windows-rstudio-clauder.md)

科學 helper 與驗收規則由 [clauder-rstudio-workbench](https://github.com/lzhs1995/clauder-rstudio-workbench) 中的 `mplusautomation-guide` 管理。Windows 本機 agent 與 Mac 遠端 agent source 同一份科學腳本，不維護兩套模型邏輯。

## 準備與版本

在實際執行的 R 工作階段中核對：

```r
cat(R.version.string, "\n")
print(packageVersion("MplusAutomation"))
print(.libPaths())
mplus_command <- Sys.getenv("MPLUS_COMMAND")
stopifnot(nzchar(mplus_command), file.exists(mplus_command))
```

使用私有環境設定提供 `MPLUS_COMMAND` 與 `MPLUS_GUIDE_ROOT`，不要在公開腳本硬編碼個人安裝路徑。按安裝版 guide 的 helper 簽名，明確傳入 Mplus 執行檔；舊 helper 未必讀取環境預設值。套件啟動訊息或版本建置警告不等於模型錯誤，應核實際終態及 validation。

## 選擇正確入口

- 指定 RStudio／ClaudeR：按 [RStudio 配方](windows-rstudio-clauder.md) 綁定身分，先提交已准入的獨立非同步 R 成員，再聚合觀察原 job。
- 明確允許獨立 Rscript：以 `xdev rscript driver.R --out ./results/run` 執行。它會取回整個工作目錄，先確認沒有受限輸入副本。
- Windows 本機 agent：經本機 ClaudeR 執行相同 driver，不必使用 xdev。

每個新模型使用已批准的唯一 model ID；恢復未知或已嘗試模型時沿原 ID 與累計預算核帳，不能僅換新 ID 重跑。

## 驗收與結果

保留原輸入、輸出、參數、協方差、軟體版本、驗收判定與 SHA manifest。`PASS`、`REVIEW`、`FAIL` 的意義由該領域 helper 定義；不把普通程序退出碼代替它。

限制外流資料留在 Windows。另建 export，只納入允許的摘要、日誌、表格或圖形，取回前後核精確檔案集合及 SHA。`pull-dir` 本身不會篩掉 `data.dat`。

獨立模型可由多個非同步 R worker 並行執行；每個 worker 明確載入 MplusAutomation，使用獨占 cwd、可寫輸出及暫存目錄，共享輸入保持唯讀。先提交准入成員，再聚合觀察，不逐一等待全程完成才提交下一個。callr 建立 R 程序後才改 `TMP`／`TEMP` 不會搬移既有 `tempdir()`。

候選 [fanout 命令](../docs/concurrency.md#rstudio-fanout) 只管理 `BATCH_ONLY` 與原 job 證據；外部 grant 回執不等於主機原子准入。跨佇列共用既有 `HostReservations`，分別計算實體 RAM bytes、commit 餘量、所有程序／執行緒與各卷成長預算。保留原 cap、`apim_v2` cap-1／cap-2、僅前景契約與 UNKNOWN，不把 Stata 單工作階段租約當成 Mplus 全機鎖。

候選實作、安裝、同時存在的真實 Mplus PID 與速度／數值對照分開驗證；本配方不宣稱四路實測已通過。`xdev` 不定義 MI、APIM、LGCM 或其他模型的數值規格。
