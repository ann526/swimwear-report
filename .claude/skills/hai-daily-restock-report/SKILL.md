---
name: hai-daily-restock-report
description: HAI the Label 每日補貨快報。每天(台灣時間早上 9 點前)把 Shopify 前一天的銷售、庫存、預購訂單算成熱銷品指標,寫回 Google 試算表「熱門商品分析」,產生 artifact 報表,滿 60 天的品項列出全部數值明細,符合補貨標準時推播提醒。使用時機:排程觸發「每日熱銷品報表更新」、使用者說「更新報表 / 跑每日快報 / 補貨提醒」,或要把這份快報設定到新帳號。
---

# 每日補貨快報

所有指令都在 repo 根目錄執行(`ann526/swimwear-report`,分支看 `state/config.json` 的 `git_branch`)。
回覆使用者一律用繁體中文。

## 檔案

| 檔案 | 用途 |
|---|---|
| `state/config.json` | 環境設定:試算表 ID、分頁名稱、欄位標題、artifact 連結、要補庫存的商品。**換帳號只改這裡** |
| `state/items.json` | 追蹤品項(品號、PO、開賣日、預購品清單、略過的前綴) |
| `state/state.json` | 已出過 60 天報告的品項、最後一次截止日 |
| `state/inv_override.json` | 手動庫存補值(通常不用,自動補值失敗時才填) |
| `tools/daily.py` | 主程式:`today / queries / check / build / payload / render / finish` |
| `tools/render.py`、`tools/report.css` | 產生報表 HTML |
| `state/in/` | 選用:把查詢結果 JSON 存成檔案放這裡,`daily.py` 也會讀 |

`daily.py` 會自動從 Claude Code 對話紀錄(`~/.claude/projects/`)讀到你剛跑過的工具結果,所以**照順序呼叫工具,再跑指令就好,不用手動存檔**。

## 需要的連接器

- **Shopify**:登入 hai-swimwear.myshopify.com(要用到 `run-analytics-query`、`graphql_query`、`get-inventory-levels`、`search_products`)
- **Google Sheets**:登入的 Google 帳號要有試算表「熱門商品分析」的**編輯**權限
- **GitHub**:能 push 到 `ann526/swimwear-report`

缺任何一個就停下來告訴使用者缺什麼,不要用猜的資料。

## 每日流程

資料截止日 = 台灣時間昨天。`python3 tools/daily.py today` 會印出來,以下簡稱 `<截止日>`。

1. **列出今天的查詢**
   `python3 tools/daily.py queries`
   印出 6 段 ShopifyQL、1 段訂單 GraphQL、要補庫存的商品、要讀的試算表範圍、新品搜尋條件。

2. **呼叫工具**(彼此獨立,可以同一輪一起叫):
   - `get_values`:主表 `'SKU動銷分析(新版)'!A1:AB19`
   - `search_products`:照印出的條件找新品
   - `run-analytics-query`:6 段 ShopifyQL **原文照貼**(查詢字串要一模一樣,程式靠它對應結果;結果太大被存成檔案也沒關係)
   - `graphql_query`:訂單查詢 + variables(`hasNextPage` 為 true 就帶 `after` 再查一次)
   - `get-inventory-levels`:印出的每個 productId

3. **檢查**:`python3 tools/daily.py check`
   - 欄位標題被改過(exit code 1)→ **停下來回報哪一欄不同,不要寫入**。
   - 出現「官網新泳裝品號」→ 在主表資料區最後一列下插入一列,複製上一列 A:AB 的公式與格式,填 A(PO)、B(品號)、C(開賣日),同時加進 `state/items.json` 的 `items`;如果是預購品也加進 `preorder`。改完重新 `get_values` 再 `check`。
   - 配件(Belt、Halo、Accessories)和 41502 不追蹤。

4. **計算**:`python3 tools/daily.py build`
   - `missing_inventory` 不是空的 → 去 Shopify 找那個商品的 product gid,跑 `get-inventory-levels`,把 gid 和 SKU 加進 `state/config.json` 的 `inventory_patch_products`,再 build 一次。

5. **寫回試算表**:`python3 tools/daily.py payload`
   每一行是一段 `{"range","values"}`,逐段用 `update_values`(spreadsheetId = config 的 `spreadsheet_id`)寫入。
   顏色尺寸分頁只寫有變動的列;新環境第一次跑(沒有基準檔)會整張重寫,正常。
   任何一段寫入失敗 → 停下來回報是哪一段失敗。

6. **讀回**:再 `get_values` 主表一次(拿公式算好的綜合評估、ROP、評估階段)。

7. **產生報表**:`python3 tools/daily.py render`,產出 `state/out/report.html`。
   用 Artifact 發佈這個檔:
   - config 的 `artifact_url` 有值 → publish 時帶 `url` = 這個連結(更新同一份,不要開新的),`label` = `<截止日>`。
   - `artifact_url` 是空的(新帳號第一次)→ 不帶 url 發佈,icon 用 `bell`,把拿到的連結寫回 `state/config.json` 的 `artifact_url`。

8. **收尾**:`python3 tools/daily.py finish`
   會更新 `state/state.json`(已出 60 天報告的品項、last_cutoff),並印出:
   - `alert: true` → 用 PushNotification 發 `push` 那一行,回覆第一行用 `reply`(⚠ 開頭),接著列出每個提醒品項的分數、庫存 vs ROP、熱賣斷碼的顏色尺寸。有滿 60 天品項時,用表格整理重點數值。
   - `alert: false` → 只回 `reply` 那一行。

9. **commit + push**:
   `git add state/ && git commit -m "Daily report update: cutoff <截止日>" && git push -u origin <git_branch>`

## 判斷規則(寫在試算表公式與 render.py 裡,這裡只是說明)

- 補貨:綜合評估 ≥75 且庫存 ≤ ROP → 整款補貨;50–74 且庫存 ≤ ROP → 只補斷碼熱賣色。
- 預警:≥75 且 14 天內會到 ROP;50–74 且 7 天內會到 ROP。
- 滿 60 天:上架天數(V 欄,從 C 欄預購上架日期起算)達 60 天、而且這次上架還沒出過報告 → 列出該品項全部數值與顏色尺寸明細。
- C 欄「預購上架日期」是開賣日的唯一依據,`build` 會自動同步到 `items.json`。

## 第一次在新帳號設定

1. **權限**
   - 新帳號連接 GitHub,並取得 `ann526/swimwear-report` 的存取權(把新帳號加為 collaborator)。
   - 試算表「熱門商品分析」分享給新帳號的 Google 帳號,權限要「編輯者」。
   - 在 claude.ai 的連接器設定中連上 Shopify(hai-swimwear 商店)和 Google Sheets。
2. **開一個新的 Claude Code 雲端 session**,選這個 repo,切到 `git_branch`。
3. **改設定**:把 `state/config.json` 的 `artifact_url` 改成 `""`(舊帳號的 artifact 新帳號不能更新)。其他欄位不用動。
4. **手動跑一次**:說「跑每日補貨快報」,照上面流程走完。第 7 步會建立新的 artifact,連結寫回 config 後 commit + push。
5. **設定每日排程**:在同一個 session 建立排程(綁在這個 session,連接器才會跟著走):
   - 時間:`CRON_TZ=Asia/Taipei 50 8 * * *`(每天 08:50,9 點前完成)
   - 提示文字:
     > 每日熱銷品報表自動更新。照 repo 的 .claude/skills/hai-daily-restock-report/SKILL.md「每日流程」1–9 做,資料截止日 = 台灣時間昨天。符合補貨標準或有新滿 60 天品項時發推播並在回覆第一行用 ⚠ 標出;都沒有時只回一行「<截止日> 報表已更新,今天沒有補貨提醒」+ 連結。試算表欄位標題被改過、或查詢/寫入失敗時,不要硬寫,回報哪一步失敗。最後 commit + push state/ 的變更。
   - 如果改用「每次開新 session」的排程,建立時要把 `Shopify`、`Google Sheets` 加進排程的 connectors,否則新 session 拿不到資料。
6. **停掉舊帳號的排程**,避免兩邊同時寫試算表。

## 常見狀況

- **ShopifyQL 結果太大被存成檔案**:正常,`daily.py` 會讀 `tool-results/*.txt`。
- **`缺少查詢結果`**:某段查詢字串和 `queries` 印的不一樣(多了空白、日期不同),照原文重跑那段。
- **61115 Navy NV1/NV2/NV4 不在庫存報表裡**:已知問題,`get-inventory-levels` 會自動補;補值會印在 build 輸出。
- **倉庫出現官網沒有的品號(例如 71204)**:草稿或未上架商品,先不追蹤,等官網上架、`check` 抓到再加。
- **找不到讀回的試算表**:第 6 步的 `get_values` 沒跑或範圍不對。
