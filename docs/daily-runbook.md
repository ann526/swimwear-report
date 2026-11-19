# 每日補貨快報:執行流程

完整步驟與換帳號說明在 skill:[`skill/SKILL.md`](../skill/SKILL.md)。
要搬到其他帳號時，跑 `python3 tools/make_bundle.py` 產生 `dist/hai-daily-restock-report.zip`,上傳到新帳號的技能(不需要 GitHub)。
設定和狀態同步存在試算表「_快報設定」分頁。

快速版(資料截止日 = 台灣時間昨天，不帶 --cutoff 會自動算):

```
                                 # 0. get_values '_快報設定'!A1:B3(本 repo 版可略過 restore)
python3 tools/daily.py queries   # 1. 列出要跑的查詢 → 呼叫 Shopify / Sheets 工具
python3 tools/daily.py check     # 3. 檢查欄位標題與官網新品
python3 tools/daily.py build     # 4. 計算
python3 tools/daily.py payload   # 5. 寫回試算表的每一段 → update_values
                                 # 6. 重新 get_values 主表
python3 tools/daily.py render    # 7. 產生 state/out/report.html → Artifact 發佈
python3 tools/daily.py finish    # 8. 更新 state,印出「_快報設定」寫回內容(update_values)與推播/回覆文字
                                 # 9. git commit + push state/
```
