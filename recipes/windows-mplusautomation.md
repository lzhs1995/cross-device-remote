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

- 指定 RStudio／ClaudeR：按 [RStudio 配方](windows-rstudio-clauder.md) 綁定身分、提交一次非同步批次。
- 明確允許獨立 Rscript：以 `xdev rscript driver.R --out ./results/run` 執行。它會取回整個工作目錄，先確認沒有受限輸入副本。
- Windows 本機 agent：經本機 ClaudeR 執行相同 driver，不必使用 xdev。

每個新模型使用已批准的唯一 model ID；恢復未知或已嘗試模型時沿原 ID 與累計預算核帳，不能僅換新 ID 重跑。

## 驗收與結果

保留原輸入、輸出、參數、協方差、軟體版本、驗收判定與 SHA manifest。`PASS`、`REVIEW`、`FAIL` 的意義由該領域 helper 定義；不把普通程序退出碼代替它。

限制外流資料留在 Windows。另建 export，只納入允許的摘要、日誌、表格或圖形，取回前後核精確檔案集合及 SHA。`pull-dir` 本身不會篩掉 `data.dat`。

長批次一次載入套件，由 Windows 串行處理已准入成員。共用 Mplus 時按已有窗口協調，不以短命令佇列為空判定引擎空閒。`xdev` 不定義 MI、APIM、LGCM 或其他模型的數值規格。
