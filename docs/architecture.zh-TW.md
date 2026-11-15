# 架構與責任

[English](architecture.md) · [專案首頁](../README.zh-TW.md)

![Mac 到 Windows 的執行與證據流程](../assets/architecture.svg)

## 各層負責什麼

| 層次 | 責任 | 不能據此證明的事 |
| --- | --- | --- |
| Mac 上的 agent | 編輯、審查、提交已準備好的請求、讀取持久結果 | 已排隊的請求真的執行過 |
| `xdev` 與 Mac 服務 | 簽章控制訊息、請求 ID、檔案傳輸、雜湊校驗 | 科學結論正確，或所有故障下都恰好執行一次 |
| Windows agent | 輪詢已設定的服務、執行已授權 PowerShell、回傳結果 | 背景模型已經結束 |
| Windows RStudio + ClaudeR | 在已核身分的 R 工作階段中執行，啟動及觀察非同步 R 工作 | 整批模型全部通過 |
| R + MplusAutomation + Mplus | 執行原科學腳本並保存模型證據 | 已獨立核驗，或結果已取回 |
| 領域工作流 | 預算、模型設計、收斂檢查、僅結果匯出、獨立核驗 | 傳輸通道健康 |
| 候選 fanout 編排 | 固定獨立成員、提交非同步 job、聚合原回執 | 主機原子准入、原生 MCP 載入或科學合併 |
| 主機擁有者的 `HostReservations` | 跨佇列共用 RAM、commit、執行緒及各卷成長准入 | 科學驗收 |

Windows agent 主動連線到 Mac 領取命令。Mac 不直接暴露 Windows RStudio：`xdev rstudio` 派發短命令，由 Windows 本機呼叫 loopback 上的 ClaudeR。若 discovery 提供 token，就在 Windows 讀取並使用，不帶回 Mac。

控制器與 Windows agent 使用共用密鑰驗證訊息。HMAC 驗證訊息，並不加密內容；請使用 Tailscale 私有路徑或其他可信私有傳輸。範例設定預設僅監聽本機，需由擁有者設定可信介面與允許清單。不要把服務暴露到公網。

## 同一科學流程，兩種 agent 所在位置

**Windows 本機 agent：** Windows agent → 本機 ClaudeR／RStudio → R → MplusAutomation → Mplus。

**Mac agent：** Mac agent → xdev → Windows agent → Windows loopback ClaudeR／RStudio → 同一份 R 腳本 → MplusAutomation → Mplus。

科學腳本、模型規格、套件要求、時間預算與結果檢查都不應改變。ClaudeR 安裝與工作階段語義由 [clauder-rstudio-workbench](https://github.com/lzhs1995/clauder-rstudio-workbench) 管理；科學執行與驗收由其中的 `mplusautomation-guide` skill 管理。本專案負責傳輸及操作層恢復。

`xdev rscript` 與 `xdev mplus` 是另兩條執行路線。任務允許時可以使用，但不能拿它們的成功證明指定的 RStudio／ClaudeR 路線已通過。

## 狀態與恢復

必須區分三種身分：

1. **傳輸請求 ID：** 短 Windows 命令及其回傳結果。
2. **具名背景工作或 ClaudeR job ID：** 較長時間執行的工作。
3. **科學成員 ID：** 單一模型、輸入 SHA、輸出、預算及終態。

傳輸回應遺失時，模型可能已經完成。先讀原證據，再判斷是否需要新工作。本機請求狀態可避免一般重複提交，但不能據此宣稱崩潰、睡眠或斷電後一律恰好執行一次。

`run` 會占用共用命令佇列；`detach` 和 ClaudeR 非同步工作在啟動後釋放佇列。佇列有空位不代表 Mplus 空閒；反過來，舊派發 ID 沒有回執，也不足以證明程序仍在執行。

候選具名任務新增 `task-submit`、`task-status`、`task-collect`：先提交獨立子程序，再沿原名觀察。每個子程序具有獨占 cwd 與 `TMP`／`TEMP`。RStudio fanout 則保留指定的 RStudio → 非同步 R → MplusAutomation 路線，state 位於 `<binding-state>/fanout/<run-id>/`。wrapper 設定 worker 的 cwd 與暫存環境，但不會搬移已建立 callr R 程序的 `tempdir()`。

獨立 control 候選目前使用 `xdev-control/1` wire version、專用 server／SQLite state／worker，由 `python3 scripts/xdev_control.py` 呼叫，不把既有 worker 改指新服務。它協商能力、先保存 claim 再啟動，保留 serial barrier，允許有明確且互不衝突 resource key 的獨立任務並行。UNKNOWN 保留預約；舊 queue cursor 不遷移。[命令與精確契約](concurrency.md) 將候選、安裝與實機驗收分開。

## 工作階段保序與主機容量

同一可變 Stata 工作階段的命令、巨集、返回結果、日誌與 cwd 需要保序。Stata frames 支援多份資料，不等於獨立解譯器。租約只覆蓋綁定 session／port，不擴大為整台主機。獨立 Stata 工作階段仍遵守原 Stata 引擎上限，該上限不延伸到 Mplus。

跨佇列重用同一主機 `HostReservations`。實體 RAM 可用 bytes 與系統 commit 餘量分開准入，再核全部程序／執行緒預算及各卷輸出／暫存成長；包含未啟動預約及已運行成員後續成長。記憶體百分比或單程序當下用量都不能證明四路容量。原 cap、`apim_v2` cap-1／cap-2 與僅前景契約沿既有批次保留。

fanout 固定外部 grant、檢查欄位及有效期，但明示 `capacity_scope: BATCH_ONLY`、`external_host_admission_enforced_by_xdev: false`，未實作主機原子預約。新 control 的 resource-key 排他也不是主機記憶體准入，單一 run 鎖更不能代替這兩層。

## 提高效率

先在 Mac 準備並核驗腳本，再領取 Windows 執行窗口。程式碼傳一次，共用輸入依唯讀授權複用，先提交已批准獨立 R worker，再等待完成。沿原 job ID 保留一個聚合觀察器；領域核驗後才取回小型僅結果匯出。`fanout-merge` 只合併生命週期回執，科學驗收仍為 `NOT_EVALUATED`。

共用窗口獲准後，應立即提交已備好的工作。grant 綁定已批准的計畫、腳本 SHA、擁有者及預算；先持久保存 claim，再提交，Windows 進入 R 正文時再次核對是否過期。等待 grant 的任務專用觀察器屬領域整合，並非 `xdev` 內建調度器。

候選碼與離線檢查不證明真實 Mplus 並行；需要真實 PID／出生時間的重疊區間及原模型終態。短程序退出後可能讀不到 `MainModule`，不能據此推定仍活著。耗時採各主機 monotonic clock；跨主機牆鐘差需有時鐘偏差界線，不能保證延遲小於兩秒。速度提升另需同規格串行／並行對照與數值一致。
