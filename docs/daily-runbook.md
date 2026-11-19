# 每日補貨快報:執行流程

完整步驟、設定方式、換帳號說明都在 skill:
[`.claude/skills/hai-daily-restock-report/SKILL.md`](../.claude/skills/hai-daily-restock-report/SKILL.md)

快速版(資料截止日 = 台灣時間昨天,不帶 --cutoff 會自動算):

```
python3 tools/daily.py queries   # 1. 列出要跑的查詢 → 呼叫 Shopify / Sheets 工具
python3 tools/daily.py check     # 3. 檢查欄位標題與官網新品
python3 tools/daily.py build     # 4. 計算
python3 tools/daily.py payload   # 5. 寫回試算表的每一段 → update_values
                                 # 6. 重新 get_values 主表
python3 tools/daily.py render    # 7. 產生 state/out/report.html → Artifact 發佈
python3 tools/daily.py finish    # 8. 更新 state.json,印出推播/回覆文字
                                 # 9. git commit + push state/
```

環境設定(試算表 ID、artifact 連結、要補庫存的商品)在 `state/config.json`。
