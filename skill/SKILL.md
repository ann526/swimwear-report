---
name: hai-daily-restock-report
description: HAI the Label 每日補貨快報。每天(台灣時間早上 9 點前)把 Shopify 前一天的銷售、庫存、預購訂單算成熱銷品指標,寫回 Google 試算表「熱門商品分析」,產生 artifact 報表,滿 60 天的品項列出全部數值明細,符合補貨標準時推播提醒。使用時機:排程觸發「每日熱銷品報表更新」、使用者說「更新報表 / 跑每日快報 / 補貨提醒」,或要在新帳號設定這份快報。
---

# 每日補貨快報

回覆使用者一律用繁體中文。
以下 `D` 代表這個 skill 的資料夾(載入 skill 時給的 base directory)。指令都用 `python3 "$D/scripts/daily.py" <指令>`。

**不需要 GitHub。** 程式在這個 skill 裡；設定和狀態(追蹤品項、已出過的 60 天報告、artifact 連結)存在試算表的「_快報設定」分頁，每天開始時讀回、結束時寫回。本機暫存資料放在 `~/hai-report/`(可用環境變數 `HAI_WORK` 改位置)。

## 執行環境

- 要在 **Claude Code**(雲端 session 或桌面版)執行。程式會從 Claude Code 的對話紀錄(`~/.claude/projects/`)讀取你剛呼叫過的工具結果，所以照順序呼叫工具、再跑指令就好，不用手動存檔。
- 雲端 session 不用選 repo。
- 需要的連接器:
  - **Shopify**:登入 hai-swimwear.myshopify.com(會用到 `run-analytics-query`、`graphql_query`、`get-inventory-levels`、`search_products`)
  - **Google Sheets**:登入的 Google 帳號要有試算表「熱門商品分析」的**編輯**權限
- 缺任何一個就停下來，告訴使用者缺什麼，不要用猜的資料。

## 檔案

| 位置 | 用途 |
|---|---|
| `D/scripts/daily.py` | 主程式:`today / restore / queries / check / build / payload / render / finish / settings` |
| `D/scripts/render.py`、`report.css` | 產生報表 HTML |
| `D/defaults/*.json` | 第一次執行、試算表還沒有設定分頁時用的預設值 |
| 試算表「_快報設定」分頁 A1:B3 | `config`(試算表 ID、分頁名稱、欄位標題、artifact 連結、補庫存商品)、`items`(追蹤品項、預購品)、`state`(已出過的 60 天報告、last_cutoff) |
| `~/hai-report/state/out/` | 每次執行的中間檔與 `report.html` |

## 每日流程

資料截止日 = 台灣時間昨天(`daily.py today` 會印出來)，以下簡稱 `<截止日>`。
試算表 ID 預設是 `1IecIBUOHrdIcn50b-VPZ8RKGTfY4U_yRDJ5wcHS9Ki4`;還原設定後以 config 的 `spreadsheet_id` 為準。

0. **還原設定**:先 `get_values` `'_快報設定'!A1:B3`,再跑 `daily.py restore`。
   分頁不存在或讀取失敗時，程式會沿用預設值，繼續往下做;第 8 步會把設定寫回去(分頁不存在就先用 `update_spreadsheet` 新增一個叫「_快報設定」的分頁)。

1. **列出今天的查詢**:`daily.py queries`
   會印出 6 段 ShopifyQL、1 段訂單 GraphQL、要補庫存的商品、要讀的主表範圍、新品搜尋條件。

2. **呼叫工具**(彼此獨立，可以同一輪一起叫):
   - `get_values`:主表 `'SKU動銷分析(新版)'!A1:AB19`
   - `search_products`:照印出的條件找新品
   - `run-analytics-query`:6 段 ShopifyQL **原文照貼**。程式靠查詢字串對應結果，一個字都不能改。結果太大被存成檔案也沒關係。
   - `graphql_query`:訂單查詢加上 variables。`hasNextPage` 為 true 就帶 `after` 再查一次。
   - `get-inventory-levels`:印出的每個 productId

3. **檢查**:`daily.py check`
   - 欄位標題被改過(exit code 1):**停下來回報哪一欄不同，不要寫入**。
   - 出現「官網新泳裝品號」:
     1. 在主表資料區最後一列下插入一列。
     2. 複製上一列 A:AB 的公式與格式，填 A(PO)、B(品號)、C(開賣日)。
     3. 在 `~/hai-report/state/items.json` 的 `items` 加上這個品號;預購品也加進 `preorder`。
     4. 重新 `get_values` 主表，再跑一次 `check`。
   - 配件(Belt、Halo、Accessories)和 41502 不追蹤。

4. **計算**:`daily.py build`
   - 如果 `missing_inventory` 不是空的:到 Shopify 找那個商品的 product gid,跑 `get-inventory-levels`,把 gid 和 SKU 加進 config 的 `inventory_patch_products`,再 build 一次。

5. **寫回試算表**:`daily.py payload`
   - 每一行是一段 `{"range","values"}`,逐段用 `update_values` 寫入(spreadsheetId 用 config 的 `spreadsheet_id`)。
   - 新 session 沒有前一次的基準檔，所以「顏色尺寸斷碼分析」會整張重寫，這是正常的。
   - 任何一段寫入失敗:停下來回報是哪一段失敗。

6. **讀回**:再 `get_values` 主表一次，拿公式算好的綜合評估、ROP、評估階段。

7. **產生報表**:`daily.py render`,產出 `~/hai-report/state/out/report.html`。用 Artifact 發佈這個檔:
   - config 的 `artifact_url` 有值:publish 時帶 `url` = 這個連結(更新同一份，不要開新的),`label` = `<截止日>`。
   - `artifact_url` 是空的(新帳號第一次):不帶 url 發佈,icon 用 `bell`,label 用 `<截止日>`。發佈後把拿到的連結寫進 `~/hai-report/state/config.json` 的 `artifact_url`。

8. **收尾**:`daily.py finish`
   - 程式會更新本機的 state,並印出:
     - 一段寫回「_快報設定」的 payload:用 `update_values` 寫入。**這一步一定要做**,否則明天會重複出 60 天報告。
     - `alert`、`push`、`reply`
   - `alert: true`:
     1. 用 PushNotification 發 `push` 那一行。
     2. 回覆第一行用 `reply`(⚠ 開頭)。
     3. 接著列出每個提醒品項的分數、庫存與 ROP 的比較、熱賣斷碼的顏色尺寸。有滿 60 天品項時，用表格整理重點數值。
   - `alert: false`:只回 `reply` 那一行。

## 判斷規則

這些規則寫在試算表公式和 render.py 裡，這裡只是說明。

- **補貨**:
  - 綜合評估 ≥75 且庫存 ≤ ROP:整款補貨。
  - 綜合評估 50–74 且庫存 ≤ ROP:只補斷碼熱賣色。
- **預警**:
  - ≥75 分，且 14 天內會到 ROP。
  - 50–74 分，且 7 天內會到 ROP。
- **滿 60 天**:上架天數(V 欄，從 C 欄預購上架日期起算)達 60 天，而且這次上架還沒出過報告 → 列出該品項全部數值與顏色尺寸明細。
- C 欄「預購上架日期」是開賣日的唯一依據,`build` 會自動同步到 items。

## 第一次在新帳號設定

1. **安裝 skill**:在 claude.ai「設定 → 功能 → 技能」上傳 `hai-daily-restock-report.zip`。
   上傳後開一個 Claude Code 雲端 session,確認輸入 `/` 時看得到 `hai-daily-restock-report`。
2. **權限**:
   - 試算表「熱門商品分析」分享給新帳號的 Google 帳號，權限設「編輯者」。
   - 在 claude.ai 的連接器設定中連上 Shopify(hai-swimwear 商店)和 Google Sheets。
3. **手動跑一次**:在雲端 session 說「跑每日補貨快報」。
   - 第 0 步會從「_快報設定」讀回舊帳號留下的設定與狀態。
   - 新帳號要有自己的報表頁面:還原後把 `~/hai-report/state/config.json` 的 `artifact_url` 改成 `""`,第 7 步就會建立新的 artifact。
   - 第 8 步會把新連結存回試算表。
4. **設定每日排程**:在同一個 session 建立排程，綁在這個 session,連接器才會跟著走。
   - 時間:`CRON_TZ=Asia/Taipei 50 8 * * *`(每天 08:50,9 點前完成)
   - 提示文字:
     > 每日熱銷品報表自動更新。使用 hai-daily-restock-report skill,照「每日流程」0–8 做，資料截止日 = 台灣時間昨天。符合補貨標準或有新滿 60 天品項時發推播，並在回覆第一行用 ⚠ 標出;都沒有時只回一行「<截止日> 報表已更新，今天沒有補貨提醒」+ 連結。試算表欄位標題被改過、或查詢/寫入失敗時，不要硬寫，回報哪一步失敗。
   - 如果改用「每次開新 session」的排程，建立時要把 `Shopify`、`Google Sheets` 加進排程的 connectors,否則新 session 拿不到資料。
5. **停掉舊帳號的排程**,避免兩邊同時寫試算表。

## 常見狀況

- **ShopifyQL 結果太大被存成檔案**:正常，程式會讀 `tool-results/*.txt`。
- **出現「缺少查詢結果」**:某段查詢字串和 `queries` 印的不一樣(多了空白、日期不同),照原文重跑那段。
- **61115 Navy NV1/NV2/NV4 不在庫存報表裡**:已知問題,`get-inventory-levels` 會自動補，補值會印在 build 的輸出。
- **倉庫出現官網沒有的品號(例如 71204)**:草稿或未上架商品，先不追蹤，等官網上架、`check` 抓到再加。
- **出現「找不到讀回的試算表」**:第 6 步的 `get_values` 沒跑，或範圍不對。
