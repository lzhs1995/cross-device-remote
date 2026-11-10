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

## 合成冒煙

只有在獲准使用 Mplus 的窗口才執行：

```sh
xdev mplus examples/smoke_mplus.inp --input examples/smoke_mplus.dat --out ./results/mplus-smoke
```

範例含合成資料，不代表複雜模型、整夜長測或新機完整驗收已通過。
