# Result publication under concurrent clients

The Mac result server keeps the first complete receipt for an original numeric
request ID. It writes to a unique temporary file, flushes it, then publishes it
with a no-replace hard link. Concurrent readers see the complete new receipt;
concurrent senders cannot replace it. The original signed queue request must
exist and validate before a new receipt is accepted.

| HTTP result | Meaning | Next action |
| --- | --- | --- |
| `200` | First publication or identical full-record retry | Keep the original ID and receipt |
| `400` | Invalid record or original request unavailable/invalid | Inspect the original state; do not resubmit the command |
| `401` | Result signature mismatch | Inspect pairing and the original sender; do not rotate keys to hide the failure |
| `409 RESULT_CONFLICT` | The ID already has a different or malformed receipt | Preserve both; reconcile the original attempt |
| `503` or disconnected response | Storage/publication outcome is uncertain | Retry only the same result bytes/ID; never rerun the command |

Duplicate comparison covers the complete JSON record, including `ms` and unknown
metadata; object key order and formatting do not matter. Conflicting candidates
are saved under `STATE/conflicts/ID/PAYLOAD_SHA256.json`. Existing receipt bytes are never
repaired or overwritten, including partial legacy receipts. Repeated identical
conflicts reuse one evidence file; different candidates retain separate hashes.
Conflicts still require reconciliation instead of an unbounded retry loop.
The candidate Windows workers now quarantine a deterministic result-publication
`409` without claiming delivery or replaying execution:

| Worker | Preserved evidence and quarantine | Later delivery behavior |
| --- | --- | --- |
| Legacy `wbagent-agent2.ps1` | Keep `result-ID.json`; add `result-ID.json.conflict` with `DELIVERY_CONFLICT`, original ID and payload hash; do not write `.sent` | Skip that conflict in later outbox scans; rotate through other pending results and continue fetching |
| Separate `xdev-control/1` worker | Keep original SQLite result; record `publication_conflicts`; set delivery marker `sent=-1`, not `sent=1` | Exclude that result from the normal `sent=0` outbox; continue other results and dispatch |

Quarantine isolates publication failure; it does not resolve the original
execution outcome or release an UNKNOWN reservation. Preserve the original
server receipt and conflicting candidate for reconciliation. Do not delete the
marker, reset the cursor, replace the request ID or rerun the command to make a
conflict disappear. A transient network error still retries only original
result publication, with bounded/fair outbox processing. Neither worker turns
an HTTP failure into permission to execute again.

`wbagent.read_result(ID)` is the shared validation entry for an original result.
It returns `None` for a missing, partial, malformed, incorrectly signed or
mismatched-ID receipt. `wbagent result ID --wait SECONDS` waits only within its
monotonic local budget and returns `124` if no valid receipt is available. It
does not create another request or change the engine lifetime. Permission and
other filesystem errors remain errors. Client integrations should use the same
reader rather than accepting a file merely because it exists. A missing receipt
returns `None` only when its storage directory is valid. An absent, inaccessible
or non-directory `results` path is a storage error, not a reason to keep polling
or submit another command. Initialize private state before use as described in
[installation](installation.md).

The v1 signature still covers only `id`, `rc`, `out` and `err`. Comparing metadata
does not authenticate it. The wire protocol contains no queue UUID, complete
request digest or execution-attempt identity. Keep the original queue, key,
cursor and worker binding; this change does not make numeric IDs safe to reuse
across queues or establish crash-proof exactly-once execution. It also cannot
protect against a legacy server still overwriting the same results directory.
Install at the coordinated maintenance boundary with one result-server owner;
preserve all original requests, receipts and UNKNOWN attempts.

Offline HTTP/thread/process fixtures verify publication and conflict behavior.
They do not establish installed-client adoption, Windows model concurrency,
resource admission, scientific completion or any latency bound.

These descriptions apply to the candidate source. A static integration audit
is evidence of wiring, not a fresh test execution or a deployed worker. Record
the exact source revision, installation, active-client behavior and real Windows
acceptance separately. See [concurrency contracts](concurrency.md).

## 繁體中文操作要點

Mac 回執服務保留原 ID 的第一份完整結果；相同完整 JSON 重傳回覆 `200`，
不同結果回覆 `409` 並另存 `STATE/conflicts/ID/PAYLOAD_SHA256.json`；相同衝突重傳
不會重複新增證據檔。原件即使殘缺也不覆寫。
讀取逾時仍沿原 ID 核對，不再次提交模型。`503` 或回覆中斷只允許重傳原回執，
不代表原命令未執行。候選 legacy Windows worker 保留原 `result-ID.json`，
另寫 `.conflict`，不寫 `.sent`；後續 outbox 跳過該衝突並繼續其他回執與取件。
獨立 `xdev-control/1` worker 則保留原 SQLite result、記錄 `publication_conflicts`，
設 `sent=-1` 隔離，不能冒充 `sent=1` 已交付。兩者都不重跑原命令；衝突需核對原次，
不能清標記、換 ID 或無限重傳。隔離投遞衝突不代表 UNKNOWN 預約已解除。
只有有效儲存目錄內缺少回執才回傳 `None`；`results` 路徑不存在、無法存取或
不是目錄時回報儲存錯誤，不轉成等待或重新提交。使用前須先初始化私有狀態。

此變更保留 v1 簽名：`ms` 等額外欄位會比較但沒有簽名保護，也未新增 queue UUID、
request 摘要或 execution 身分。更新須由原服務負責者在維護邊界套用，保留原 queue、
key、cursor 和所有回執。離線競爭測試通過不等於 Windows 多 Mplus 或所有 agent 已載入。
