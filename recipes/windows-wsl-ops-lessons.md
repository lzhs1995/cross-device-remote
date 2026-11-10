# 配方：共用佇列、PowerShell、WSL 與磁碟

[English recovery guide](../docs/recovery.md) · [恢復指南](../docs/recovery.zh-TW.md)

## 短命令不占長佇列

所有 agent 可能共用一條序列命令佇列。`run` 只放短查詢並設期限；長工作使用具名 `detach` 或已批准的非同步 RStudio 路線。避免在使用者目錄做無深度限制的遞迴掃描。巡檢讀已知小回執、Windows 可用 RAM、commit 與磁碟，不反覆掃全樹。

`run --timeout` 可終止該命令的程序樹；`wait --max` 只限制等待。兩者不能混稱模型 cap。`tail` 只讀具名 detached 工作；短 `run` 的遲到結果用原數字 ID 的 `result`。

## 睡眠與舊未知結果

服務有 DISPATCH 而無 RESULT，不代表工作未執行。背景工作可能在喚醒後留下終態；先讀原具名工作。不要重派、不清 cursor。舊派發 ID 已落在輪詢 cursor 之前時，保留 UNKNOWN；以新鮮輪詢和當前回執判斷通道，不因歷史記錄無限等待。

服務 TTL、agent 重連、Tailscale 路由與 Windows 喚醒要分別核對。watchdog 不是完整自癒驗收；用原記錄判定它是否實際恢復。

## PowerShell 5.1 與 UTF-8

- 多層引號中的 R／Python／shell 程式改成檔案傳送，再以明確 interpreter 執行。
- xdev 嵌入包裝器的正文用 UTF-8 無 BOM；由 PowerShell 5.1 直接讀取的獨立 `.ps1` 依其需要使用 UTF-8 BOM。
- 原生程式寫 stderr 不必然失敗；按 `$LASTEXITCODE`、持久回執及科學判定核對。
- `foreach` 語句不能直接接管道，可先收成陣列，再交給 `Sort-Object` 等命令。
- JSON 多行讀完整字串；哈希表陣列按明確鍵取值，不依賴 property adaptation。
- `run` 提供 `Expand`、`$XRoot`、`Emit`；`detach` 不提供 `Emit`，背景正文自行寫短 JSON 或具名記錄。
- `ZipArchiveMode` 相關程式需載入正確壓縮組件；路徑、原腳本與包裝器 SHA 各自記錄。

## WSL 檔案與資源

小型 WSL 產物先複製到 Windows 本機暫存，再以 `pull` 核 SHA。狀態、鎖與持久工作目錄放 Windows 本機磁碟；UNC 上的硬連結與 Windows 鎖語義不可假設等價。

WSL 的 VHDX、Docker 虛擬磁碟與 pagefile 可能占用 C:，但不能僅憑單次大小斷言是最近下降原因。先記基線、再比對增量。pagefile 已分配大小和使用量是不同數字；釋放 WSL 頁快取不等於釋放磁碟。

必要時由已授權的維護流程設定適合該主機的 WSL 記憶體上限；保留既有 `.wslconfig`，先協調所有受影響工作。`wsl --shutdown` 會中止 WSL 程序；同步監督器可能立即將它重啟，需只處理已獲准的那一組程序並恢復原入口。不要把某台主機的固定 GB 值當通用設定。

## 大檔與備份

GB 級初次遷移使用專門、可續傳且可獨立回下載驗真的傳輸工具。xdev 適合控制訊息與有界結果，不為一次性大歸檔塞滿共用佇列。輸出只回小摘要，完整記錄留原端。

回下載副本仍占磁碟。釋放之前按既有授權核本地 SHA、歸檔回執與精確遠端物件，保留刪除清單和證據；不把「快取」當作刪除他方資料的理由。

## Windows 非估計測試

可在 Mac 預先固定 commit 並打包領域 skill，由 Windows 在隔離測試目錄執行非估計測試。確認測試真的不呼叫引擎；它仍會消耗 RAM、磁碟與佇列，應和其他 owner 協調。實際 R 路線的 `packageVersion()` 才是版本證據，不能拿另一個 R 安裝的結果代替。
