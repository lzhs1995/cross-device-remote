# 配方：Mac agent → Windows RStudio／ClaudeR → R／MplusAutomation

[English](windows-rstudio-clauder.en.md) · [架構](../docs/architecture.zh-TW.md)

本配方把工作送進已核身分的 Windows RStudio。Windows 本機 agent 可以透過本機 ClaudeR 執行同一份 R 腳本；不必為了沿用科學經驗繞經 Mac。

## 前置條件

- Windows 保持登入與喚醒，已安裝 R、RStudio、相容 ClaudeR；需要估計時另有合法 Mplus 與指定 MplusAutomation 版本。
- 按 [clauder-rstudio-workbench](https://github.com/lzhs1995/clauder-rstudio-workbench) 的安裝版本啟動專屬工作階段。不要對未獲准的使用者工作階段提交。
- `xdev health` 顯示服務及新鮮輪詢；不僅看舊 `running` ID。
- 科學腳本、輸入、有限預算、結果匯出規則與共享 Mplus 窗口已準備好。

不要跨版本複製按固定語句編號擷取 `claudeAddin()` 的歷史啟動方式。啟動 RStudio 與等待初始化分開處理，避免把桌面程序綁在短 `run` 的超時程序樹中。

## 1. 只讀取得真實工作階段身分

先讀 Windows discovery、監聽與程序身分。不要直接輸出可能含 token 的 discovery 原文。

```sh
xdev run -c 'Get-Process rstudio,rsession* -ErrorAction SilentlyContinue | Select-Object ProcessName,Id,StartTime | ConvertTo-Json -Compress'
xdev run -c 'Get-ChildItem (Join-Path $env:USERPROFILE ".claude_r_sessions") -Filter *.json | ForEach-Object { $s = [IO.File]::ReadAllText($_.FullName) | ConvertFrom-Json; [pscustomobject]@{session_name=$s.session_name; port=$s.port; pid=$s.pid; started_at=$s.started_at} } | ConvertTo-Json -Compress'
```

Mac 上本機 MCP 的 `list_sessions` 不等於 Windows 工作階段清單。依現場值設定下列變數；示例不提供假 PID 或假位址。`bind` 會核 discovery、監聽、R 程序出生時間與 RStudio 父程序。

```sh
# SESSION_NAME、CLAUDER_PORT、R_PID、RSTUDIO_PID 必須來自上述現場核對。
xdev rstudio bind --state ./rstudio-state --session-name "$SESSION_NAME" --port "$CLAUDER_PORT" --r-pid "$R_PID" --rstudio-pid "$RSTUDIO_PID"
```

保留 `rstudio-state`。R PID 改變時，先核原 job、輸出及預算，再核新身分並建立新 state，不覆寫舊 state。

## 2. 短檢查與一次非同步提交

短檢查腳本 `inspect.R` 可包含：

```r
cat("R:", R.version.string, "\n")
cat("R PID:", Sys.getpid(), "\n")
cat("MplusAutomation:", as.character(packageVersion("MplusAutomation")), "\n")
```

```sh
xdev rstudio execute --state ./rstudio-state --request-id inspect-01 --code-file inspect.R
```

在 Windows 準備原科學 driver，經 `xdev push` 後核 SHA。`submit.R` 只需 source 已批准的 Windows 腳本；例如使用事先於該 R 工作階段設定的專案路徑：

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

一次提交整個已准入批次；在 Windows 非同步 R worker 內載入 MplusAutomation 並串行處理成員。Mac 保持一個低頻觀察器，讀 stage、message、updated_at 與同一 job ID。勿為每個短模型傳輸整份工作目錄。

## 3. 保留原請求，處理未知結果

- 124 或 UNKNOWN：沿原 request ID 用 `result`，不能重新 `submit`。遲到的 bind 回執用 `--request-id bind` 接回。
- 載入已保存終態，不反覆讀可能會消耗記憶體 job 記錄的舊版介面；`not_found` 不代表從未執行。
- HTTP 成功到回執落盤之間仍可能崩潰。這個介面不保證每種故障都恰好執行一次。
- 科學 worker 必須有獨立持久終態、原模型時間上限與累計預算；未知孤兒耗時不能填零。
- token 由 Windows discovery 在本機讀取，使用 `X-Clauder-Token`；不在 Mac 上保存副本。

`xdev rstudio` 的新回執在 Windows 端限制為 64 KiB；超限正文留在 Windows，回傳明確省略內容的狀態及原檔路徑、大小、SHA、行數。這不是結果檔傳輸上限。

## 4. 結果與可見性

在原輸出上做領域核驗，另建立僅含允許結果的 export。`pull-dir` 會取回整個指定目錄，不會排除 `data.dat`。取回前核 manifest，取回後核精確檔案集合與 SHA。

RStudio 能顯示接收的程式碼及主工作階段輸出；非同步 worker 的每行輸出不一定都在 Console。短 Mplus 程序可能在打開工作管理員前已結束；以 R 呼叫、Mplus 起止與保存輸出證明執行位置，不以 Console 截圖代替驗收。

HTTP fallback 已通不能寫成 agent 原生 MCP 已通；原生 MCP 證據應依 workbench 的檢查取得。Python 直接啟動 Mplus 也不能冒充指定的 RStudio → MplusAutomation 路線。

## 5. 解析與 Windows I/O 經驗

- `XDEV_JSON` 按位元組 LF 分行；Python `str.splitlines()` 會切開合法 JSON 字串內的 Unicode 分隔字元。先保存原 stdout／stderr，再解析。
- 多行 JSON 應解析完整正文，不能用最後一行代替完整物件；帶進度時優先讀具名持久回執。
- PowerShell 5.1 對無 charset 的 HTTP JSON 可能解碼錯誤。保留原 bytes，依 UTF-8 解碼，不猜測改寫舊回執。
- 讀進度檔時使用允許 ReadWrite 與 Delete 的共享模式並立即關閉，減少阻礙原子替換。
- `[IO.File]::ReadAllText` 或明確 `[string]` 比直接把 `Get-Content -Raw` 的 PSObject 交給 `ConvertTo-Json` 更合適，避免連 provider 屬性一起展開成巨型回執。
- 經包裝器執行時，`$PSCommandPath` 不一定是原科學腳本。分別記錄原腳本 SHA、提交正文 SHA 與傳輸載荷 SHA。
- 純身分檢查保持短小，不把全程序列舉塞進 R 的同步 HTTP 窗口；解析失敗先核原產物，不重跑模型。
