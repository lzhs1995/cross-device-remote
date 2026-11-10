# 安裝與私有配對

[English](installation.md) · [專案首頁](../README.zh-TW.md)

## 前置條件

- Mac：Python 3.10 以上、Git，以及能執行本機命令並使用私有網路的 CLI agent。CLI 本身不依賴特定 agent SDK。
- Windows：Windows PowerShell 5.1、已登入使用者及任務所需軟體。桌面／RStudio 長工作需保持喚醒。
- 兩端：可信私有路由。跨網路時可在兩端安裝 Tailscale，並設定 tailnet 存取政策，允許指定 Windows 連到 Mac 服務。
- RStudio／ClaudeR、R 套件、Mplus 及授權皆為另外的依賴；本安裝器不安裝或授權這些軟體。

## 1. 在 Mac 安裝 skill

```sh
git clone https://github.com/lzhs1995/cross-device-remote.git
cd cross-device-remote
python3 scripts/check.py
python3 scripts/install.py --agents codex claude gemini antigravity
python3 scripts/install.py --agents codex claude gemini antigravity --check
export PATH="$HOME/.agents/skills/cross-device-remote/scripts:$PATH"
```

共用 skill 指向此 checkout。請保留 checkout 位置；更新它會影響已安裝連結。為了可重現，執行前記錄 commit，只在安全邊界更新。

| 入口 | 安裝位置 |
| --- | --- |
| 共用 | `~/.agents/skills/cross-device-remote` |
| Codex | `~/.codex/skills/cross-device-remote` |
| Claude Code | `~/.claude/skills/cross-device-remote` |
| Gemini | `~/.gemini/skills/cross-device-remote` |
| Antigravity | `~/.gemini/antigravity/skills/cross-device-remote` |

預設安裝四個 agent 入口。`--prefix PATH` 可使用另一個 home 做隔離安裝。遇到不同既有內容時預設保留並拒絕；明確加 `--replace` 才會先建立附日期備份再替換連結。已有私有設定保持原樣。`--check` 只核連結，不核 agent 載入或網路健康。

其他 agent 可明確讀取共用 `SKILL.md`，以絕對路徑呼叫 `scripts/xdev`。自動探索規則會隨產品與版本而異，需由實際 agent 核驗載入行為。

## 2. 初始化 Mac 私有狀態

```sh
WBAGENT_STATE="$HOME/.local/state/cross-device-remote" python3 "$HOME/.agents/skills/cross-device-remote/scripts/wbagent.py" init
open -e "$HOME/.config/cross-device-remote/config.json"
```

安裝器只在設定不存在時建立檔案。範例僅監聽 loopback，刻意不接受遠端 Windows 連線。請在**私有**設定中把 `serve_args` 改為實際可信綁定位址與允許清單。以下只有佔位符，不能原樣啟動：

```json
"serve_args": [
  "--host", "<MAC_TAILSCALE_IP>",
  "--port", "18092",
  "--allow-ip", "<WINDOWS_TAILSCALE_IP>/32",
  "--require-signed-poll",
  "--ttl", "86400"
]
```

各機執行 `tailscale ip -4` 可取得該機位址。tailnet 政策與服務允許清單都只放行指定對端。TTL 應覆蓋預計批次，或預先安排維護邊界。本套件的服務未提供 HTTPS 終止：私有 Tailscale 路徑提供傳輸加密，HMAC 提供訊息驗證；檔案下載也依賴網路允許清單。

設定檔、`state_dir`、配對密鑰、真實位址與選用位址發現服務資訊均留在 Git 之外；不要把私有狀態複製到 checkout。

## 3. 配對全新 Windows agent

把倉庫取得到 Windows，例如使用 Git。經已批准的私有通道，把 Mac state 的 `secret` 檔案傳到 Windows 私有檔案。不要放進對話記錄、公用 gist 或倉庫；安裝器直接讀檔，不列印內容。

在 Windows checkout 執行，替換位址佔位符及私有檔案路徑：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\windows\install.ps1 -Server 'http://<MAC_TAILSCALE_IP>:18092' -SecretFile 'C:\private\xdev-secret'
```

預設狀態位於 `%LOCALAPPDATA%\XdevAgent`，權限僅限目前使用者與 SYSTEM。`-State` 可指定另一個全新目錄。安裝器遇到既有 state 或舊 worker 設定會拒絕，避免覆寫或為同一佇列建立第二個消費者。

選項必須明確指定：

- `-Start`：立即啟動 worker。
- `-AutoStart`：安裝使用者層登入及每五分鐘 watchdog 排程；單獨使用不會立即啟動。
- 兩者同時：安裝排程並立即啟動。

安裝後手動啟動：

```powershell
wscript.exe "$env:LOCALAPPDATA\XdevAgent\start.vbs"
```

既有部署沿原恢復程序處理；不要用本全新安裝器覆寫執行中的舊 agent、重設 cursor，或把私有 state 複製到倉庫。

## 4. 啟動 Mac 服務並核通道

```sh
xdev serve
xdev health
xdev run -c '[Environment]::MachineName; [DateTime]::UtcNow.ToString("o")' --timeout 20
```

新鮮簽章輪詢與新命令回執可證明命令通道可用，仍不能證明 RStudio 或 Mplus 成功。保留請求 ID；等待到期時用 `xdev result ID` 接回，不重複提交。

科學工作前依 [RStudio 配方](../recipes/windows-rstudio-clauder.md) 或任務另行允許的 Rscript／Mplus 路線執行。`find-tools --write` 會搜尋軟體，且只補尚未設定的路徑；需要時執行一次，不每次巡檢重掃。

## 離線與連線檢查

`python3 scripts/check.py` 是本機檢查入口。`scripts/selftest.sh` 為連線測試，可能寫合成檔案及建立遠端工作；先閱讀內容並取得測試窗口。詳見 [驗收邊界](validation.zh-TW.md)。

## 卸載但保留恢復狀態

1. 停止新增提交，並到達安全邊界：確認本安裝沒有在途命令、背景工作或 RStudio／引擎作業。保留全部請求 ID、預算與結果。若 Mac 服務為共用，先和其他擁有者協調再停止。
2. 在 Windows 核對 `XdevAgentLogon` 與 `XdevAgentWatchdog` 排程的 action 確實指向**本安裝**的 `start.vbs`。到達安全邊界後，在自身 state 目錄建立 `STOP`（自訂安裝需使用原 `-State` 路徑），等待記錄新增 `WBAGENT_V2 stop` 且對應 worker 程序退出。watchdog 會遵守此檔案；不要強停其他 worker，也不要把舊記錄當成本次已停止。

   ```powershell
   $xdevState = Join-Path $env:LOCALAPPDATA 'XdevAgent'
   Get-ScheduledTask -TaskName XdevAgentLogon,XdevAgentWatchdog -ErrorAction SilentlyContinue | Format-List TaskName,Actions
   # 核對所有權，且確認已無工作後：
   New-Item -ItemType File -Path (Join-Path $xdevState 'STOP') -Force | Out-Null
   ```

3. worker 停止後，只取消已核對且由本安裝建立的兩個排程。未使用 `-AutoStart` 時可能不存在；現役舊 `StataWorkbenchAgent`／`WbAgentWatchdog` 與他方排程一律保留。

   ```powershell
   # 僅針對確實存在、且 action 已於上一步核對的任務執行。
   Unregister-ScheduledTask -TaskName XdevAgentLogon -Confirm:$false
   Unregister-ScheduledTask -TaskName XdevAgentWatchdog -Confirm:$false
   ```

4. 在 Mac 逐一核對上列 skill 入口：只有在它是符號連結、且解析後仍指向本 checkout 時才移除。先移除已核對的 agent 連結，再移除共用連結，確保核對時仍能正確解析。一般目錄與指向別處的連結不動。若也停用本安裝的 Mac 服務，沿同一無工作安全邊界，在它自身 state 目錄建立 `STOP`，再核對退出。

保留私有 `config.json`、兩端 state、配對密鑰、原嘗試、預算及結果檔。卸載連結或排程不需要刪除這些資料；保留或刪除是另一項使用者決定。需要恢復時，沿保留狀態的原恢復程序接續，不初始化新佇列覆蓋它。

## Tailscale 官方參考

- [安裝 Tailscale](https://tailscale.com/kb/1017/install)
- [存取控制政策](https://tailscale.com/kb/1018/acls)
- [直連與中繼連線](https://tailscale.com/kb/1257/connection-types)

中繼連線仍可加密；直連影響路由及效能。ping 或連線類型都不能取代新鮮 xdev 命令回執。
