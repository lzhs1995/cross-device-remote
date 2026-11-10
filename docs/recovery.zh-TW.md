# 恢復原工作，不重放已完成作業

[English](recovery.md) · [專案首頁](../README.zh-TW.md)

## 依序讀取證據

先執行 `xdev health`，再讀原請求／job 狀態。分開診斷：網路可達、Windows 新鮮輪詢、命令執行、ClaudeR 身分、科學進度。`ping` 成功不能證明後續各層。

| 觀察 | 下一步 |
| --- | --- |
| `SERVER_DOWN` | 查 Mac 服務記錄與 TTL；以 `xdev serve` 啟動已設定的服務，確認 Windows 出現新輪詢後才提交。 |
| `AGENT_STALE` | 查 Windows 登入／睡眠、agent 記錄、目前位址、Tailscale 路由及服務允許清單；保留既有排隊請求。 |
| 輪詢已舊，卻顯示 `OK (agent busy...)` | 這只代表佇列紀錄；查 cursor 與原 job 證據，不當作目前可達。 |
| `run` 回傳 124 | 用 `xdev result ORIGINAL_ID`，不重跑 `run`。 |
| 背景工作仍在執行 | 用 `xdev tail ORIGINAL_NAME` 或 `xdev wait ORIGINAL_NAME --max 3600`，不重跑 `detach`。 |
| ClaudeR 提交結果未知 | 用 `xdev rstudio result --state ORIGINAL_STATE --request-id ORIGINAL_REQUEST`，沿原非同步 job 輪詢。 |
| RStudio PID 已變 | 先核舊 job、輸出與預算；核新程序身分後才建立新的 binding state。 |
| 有完整輸出卻缺退出回執 | 依領域工作流檢查孤兒輸出與預算；未知耗時不能算零。 |

## 服務 TTL 與守護

服務 TTL 應覆蓋預計批次，或預先安排協調過的維護邊界。重啟 Mac 服務不代表 Windows agent 已重連；位址發現與備援路由可能在 Tailscale 正常時單獨失敗。

Windows 安裝器提供的選用自啟與 watchdog 是恢復輔助，並不保證服務不中斷。請在目標機器核對實際任務定義及記錄。不要為了讓健康畫面變乾淨而重設 cursor、刪除未知任務或重啟他方正在使用的程序。

真實 Tailscale 位址及位址發現設定放在私有設定檔。VPN／代理干擾時，核兩端實際路由與 TUN 排除規則，只套用該部署已授權的變更，不整份覆寫使用者代理設定。

## Windows 與 RStudio 邊界

桌面版 RStudio 需要 Windows 保持登入與喚醒。背景 R worker 可能在 Mac 斷線後繼續執行；RStudio 關閉後是否存活，取決於 worker 實作。未測該實際環境前，不能宣稱關閉 RStudio 沒影響。

使用安裝版本支援的 ClaudeR 啟動程序。不要把舊版按固定語句編號擷取 `claudeAddin()` 的腳本直接用在新版。核對 discovery、監聽程序、RStudio 父 PID 與程序出生時間；僅有監聽埠不足以確認身分。

WSL 占用記憶體時，分別檢查 Windows 實體記憶體、commit、磁碟餘量及模型實測需求。資源等待不必計入引擎秒數。WSL 上限與 shutdown 會影響其他工作：按既有授權協調、保留 `.wslconfig`，只恢復本次刻意停止的元件；不要預設定時全域丟棄快取。

## 縮小恢復成本

只讀一份簡短現況交接、佇列及最新原回執，不為每次觀察另開版本目錄。修正最小的傳輸或解析問題，隨即返回已批准的科學工作。通道與執行證據正確後，模型不收斂應當作數值問題處理。

保留失敗嘗試與累計預算。後來的探針成功，不會把先前未知結果改成成功。大型記錄留在 Windows，小型回執應指出原檔路徑、大小、SHA 及內容省略狀態。
