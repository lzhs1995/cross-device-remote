---
name: cross-device-remote
description: Run authorized Windows work from a Mac agent through xdev and a private network such as Tailscale. Supports PowerShell, durable background jobs, SHA-checked transfers, and verified Windows RStudio/ClaudeR routes. 從 Mac agent 遠端執行 Windows 工作，包含 PowerShell、背景任務、檔案校驗與 RStudio／ClaudeR；保留原請求及科學流程。
---

# cross-device-remote · xdev

[English project guide](README.md) · [繁體中文專案指南](README.zh-TW.md)

## Entry / 入口

```sh
"$HOME/.agents/skills/cross-device-remote/scripts/xdev" health
```

Use the installed absolute entry, or add its `scripts` directory to PATH and use `xdev`. Configuration is private at `~/.config/cross-device-remote/config.json`; `XDEV_CONFIG` can select another file. Never commit private configuration, state, actual device addresses, tokens or research data.

使用已安裝的絕對入口，或將 `scripts` 加入 PATH 後呼叫 `xdev`。設定及密鑰留在私有位置，不把真實位址、工作階段或研究資料放進 skill。安裝詳見 [English](docs/installation.md)／[繁體中文](docs/installation.zh-TW.md)。

## Choose the route / 選對路線

| Route / 路線 | Command / 命令 |
| --- | --- |
| Short PowerShell / 短命令 | `xdev run job.ps1` |
| Original late result / 原遲到結果 | `xdev result ID` |
| Background job / 背景工作 | `xdev detach NAME job.ps1`; `xdev tail NAME`; `xdev wait NAME --max 3600` |
| RStudio / ClaudeR | `xdev rstudio bind/execute/submit/poll/result` |
| Standalone R / 獨立 R | `xdev rscript analysis.R --out ./results/run` |
| Direct Mplus / 直接引擎 | `xdev mplus model.inp --input synthetic.dat --out ./results/run` |
| Files / 檔案 | `xdev push`, `xdev pull`, `xdev pull-dir` |

If the user requires RStudio/ClaudeR, use that route. Rscript and direct Mplus are separate routes and cannot substitute for its acceptance. A Windows-local agent uses local ClaudeR and the same approved scientific R script; it need not route through a Mac.

使用者指定 RStudio／ClaudeR 時，必須沿該路線；獨立 Rscript 或直接 Mplus 成功不能代替。Windows 本機 agent 透過本機 ClaudeR 使用同一科學腳本，不因所在機器不同而改模型。

## Execution contract / 執行契約

1. **Verify each layer.** A fresh poll and command receipt establish command transport. RStudio needs real discovery/PID/birth/parent/listener identity; a model needs its own terminal evidence. Old `running` IDs and `ping` do not establish current execution.
2. **Submit once, reconcile by ID.** On timeout or UNKNOWN, use the original request/job. Do not reset cursors, delete claims, replay completed members, or treat unknown runtime as zero. Crash-proof exactly-once behavior is not guaranteed.
3. **Keep the shared queue short.** Long work uses a named detached task or approved asynchronous RStudio worker. `--max` is a wait limit, not an engine cap. The domain worker owns finite model budgets and shared-window rules.
4. **Batch efficiently.** Prepare code before the Windows grant; start promptly after approval. Let Windows process the approved members; keep one low-frequency observer and one concise current handoff.
5. **Collect deliberately.** `pull-dir`, `rscript` and `mplus` collect the whole chosen directory, including input copies. For restricted data, create a result-only export and check its exact file set and hashes. Check receiver disk margin first.
6. **Preserve ownership and evidence.** Do not stop other sessions or modify a shared runtime merely because the channel is stale. Keep original failures, budgets and output. Use existing authority for maintenance; HMAC is not encryption, so use a trusted private path.

1. **分層核驗：** 新鮮輪詢與命令回執只證明傳輸；RStudio 需真實身分，模型需原終態。舊 `running` 與 ping 不代表正在執行。
2. **一次提交、原 ID 接回：** 超時或 UNKNOWN 沿原 request／job；不清 cursor、不刪 claim、不重跑已完成成員、不把未知耗時計零；不宣稱所有故障都恰好一次。
3. **共用佇列保持短小：** 長工作用具名背景工作或已批准非同步 RStudio；`--max` 不是引擎 cap，領域 worker 管理預算與窗口。
4. **批次提高效率：** 獲窗前準備好、獲准後及時提交；Windows 執行整批，Mac 一個低頻觀察器與一份簡短現況交接。
5. **明確取回範圍：** 整目錄命令包含輸入副本；受限資料另建僅結果匯出，核精確集合及 SHA，先查接收端空間。
6. **保留所有權與證據：** 不因通道舊就停止他方工作；保留失敗、預算與結果，維護沿既有授權。HMAC 不加密，需可信私有通道。

## Read only what the task needs / 按需閱讀

- RStudio/ClaudeR: [English](recipes/windows-rstudio-clauder.en.md) · [繁體中文](recipes/windows-rstudio-clauder.md).
- Research batches / 研究批次: [English](recipes/research-batches.en.md) · [繁體中文](recipes/pp-lgcm-mplus-continuation.md).
- Recovery / 恢復: [English](docs/recovery.md) · [繁體中文](docs/recovery.zh-TW.md).
- Acceptance / 驗收: [English](docs/validation.md) · [繁體中文](docs/validation.zh-TW.md).
- Other recipes / 其他配方: [R](recipes/windows-r.md), [Mplus](recipes/windows-mplus.md), [MplusAutomation](recipes/windows-mplusautomation.md), [PowerShell/WSL](recipes/windows-wsl-ops-lessons.md), [migration/disk](recipes/mac-restore-and-remote-acceptance-lessons.md).

Transport belongs here; session semantics belong to [clauder-rstudio-workbench](https://github.com/lzhs1995/clauder-rstudio-workbench); scientific design and validation belong to its `mplusautomation-guide`. The originating route has run research; this public package still needs fresh-machine verification. Installed links are not proof that every agent automatically loads the skill.

本 skill 管傳輸，workbench 管工作階段，`mplusautomation-guide` 管科學設計及驗收。來源路線有研究實績，但公開套件仍需新機驗證；安裝連結不等於所有 agent 自動載入通過。
