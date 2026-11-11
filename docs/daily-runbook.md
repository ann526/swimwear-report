# 每日熱銷品報表:自動更新流程(台灣時間每天早上 9 點前完成)

資料截止日 = 台灣時間「昨天」。所有指令在 repo 根目錄執行。試算表:熱門商品分析(1IecIBUOHrdIcn50b-VPZ8RKGTfY4U_yRDJ5wcHS9Ki4),分頁「SKU動銷分析(新版)」sheetId 595354439。

1. **讀試算表**:`get_values` `'SKU動銷分析(新版)'!A1:AB19`。確認欄位標題沒被改(標題被改或多/少欄時,停下來回報,不要寫入)。C 欄「預購上架日期」是開賣日的唯一來源,腳本會自動同步。
2. **新品同步**:Shopify `search_products`(status:active,依 CREATED_AT 新到舊 50 筆)。泳裝新品(SKU 前 5 碼不在 B 欄;略過 Belt/Halo/Accessories、41502、草稿)→ 在資料區最後一列下插入一列、複製上一列 A:AB 的公式與格式、填 A/B/C,並加進 `state/items.json`(launch=C 欄日期)。預購新品也加進 items.json 的 `preorder`。
3. **跑查詢**:`python3 tools/daily.py queries --cutoff <昨天>`,把印出的 6 個 ShopifyQL 原樣跑一次(大結果會自動存檔,不用讀)。另跑 graphql orders 查詢:`created_at:>=2026-09-01 created_at:<=<昨天>T23:59:59+08:00` 加 items.json `preorder` 各 SKU 前綴(first:100,有下一頁就翻頁)。
4. **計算**:`python3 tools/daily.py build --cutoff <昨天>`。若 `missing_inventory` 不是空的(例如 61115NV1/NV2/NV4),用 `get-inventory-levels` 查該商品各倉 onHand,5 倉加總寫進 `state/inv_override.json` 的 `{"<昨天>": {sku: 件數}}`,再跑一次 build。
5. **寫回試算表**(`state/out/sheet_blocks.json`,列 3 起依 items.json 順序):E:G、J、O:Q、Y、AB;B19 = 截止日(yyyy/m/d)。「顏色尺寸斷碼分析」先清 A2:I400 再寫 build.json 的 tab2(超過格線先加列);「預購訂單追蹤」A2:H 寫 tab3。
6. **讀回**:再 `get_values` A1:AB19(拿到公式算好的分數、ROP、評估階段)。
7. **報表**:`python3 tools/daily.py render --cutoff <昨天>`,用 Artifact 發佈 `state/out/report.html`(同一路徑=同一連結 https://claude.ai/artifact/USWkeXXvHYxrtTtkaQH1rm,label 寫截止日)。
8. **提醒**:看 `state/out/report_meta.json`。`restock_full`/`restock_cut` 有品項、或 `sixty` 有新滿 60 天品項時,發手機推播並在回覆最上面用 ⚠ 標出;都沒有就只回一行摘要。把 `sixty` 加進 `state/state.json` 的 `reported_60d`,更新 `last_cutoff`。
9. **commit + push** `state/` 與文件變更到 `claude/dreamy-sagan-w5rtdd`。

補貨標準:綜合評估 ≥75 且庫存 ≤ ROP → 立即整款補貨;50–74 且庫存 ≤ ROP → 補斷碼熱賣色;≥75 且 14 天內到 ROP、50–74 且 7 天內到 ROP → 預警。滿 60 天:上架天數(V 欄,自 C 欄起算)達 60 天且這次上架還沒出過報告,列出全部數值與判斷。
