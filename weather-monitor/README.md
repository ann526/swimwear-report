# 香港天氣警告監控記錄

供 HAI 泳裝(ann@hai-swimwear.com)計算惡劣天氣期間工時薪資扣減依據使用。

- `weather-log.csv`:每小時監控routine持續追加的天氣警告變化記錄(best-effort,資料來源為WebSearch,非天文台官方即時API)
- 每月1日會自動產生當月報表(xlsx)並透過 SendUserFile 傳送給使用者,不會提交到這個repo

## 重要限制
1. 資料來源非天文台官方逐分鐘記錄,正式扣薪計算前請至天文台官網核實:
   https://www.hko.gov.hk/tc/wxinfo/climat/warndb/warndb1.shtml
2. 本記錄檔案改存於此git repo(取代原本 /tmp scratchpad 路徑),原因是 scratchpad 屬於暫存空間,
   工作環境閒置一段時間後會被回收清空,曾導致2026年8月的監控記錄完全遺失。
   每次「每小時監控」routine偵測到變化時,除了寫入本檔案,也應執行 git add/commit/push 確保真正持久化。
