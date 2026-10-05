# 每月訂單整理流程

使用者每月會給一份 Shopify 訂單匯出的 Google Sheet(例如「9月訂單」),請依下列步驟處理。所有修改都直接寫在使用者給的那份試算表裡,連結不變。回覆一律用繁體中文。

## 需要的檔案

- **當月訂單**:Shopify 匯出的 Google Sheet,分頁名稱像「9月訂單」。
- **營業日誌**(Excel 檔,存在 Drive 上,Sheets 連接器讀不了,要用 Drive `download_file_content` 下載後用 openpyxl 解析):
  - 台中大遠百專櫃-營業日誌.xlsx(ID `14Isusm9LYDnzVf7u2wSvkqaXn6HSLHRU`),月份分頁名稱如 `202609`,備註在 I 欄,資料從第 7 列開始。
  - 信義大遠百-營業 日誌.xlsx(ID `1lgfQIGqhB46Q2HBYfyMp_N3oA9K2Br04`),月份分頁名稱如 `September-2026`,備註在 H 欄,資料從第 7 列開始。
  - 分頁結構可能會變,動手前先確認標題列與欄位。
- **8月訂單**(參考做法):`15pUNoZRwfvqTgnLaeZSlMTdrU4kX0oAODLjshP-412g`

## 主要欄位(Shopify 匯出)

A Name(訂單編號)、C Financial Status、M Discount Code、P Created at、Q Lineitem quantity、R Lineitem name、U Lineitem sku、AS Notes、AX Refunded Amount、BA Employee、BB Location。步驟 2 插入輔助欄位後,BB 之後的欄位會右移一欄。

## 步驟 1:補齊同一張訂單的 Location 與 Discount Code

Shopify 一單多品項時只有第一列有資料。依訂單編號(A 欄),把同一張訂單其他空白列的 Location(BB)和 Discount Code(M)填成同一個值。原本有值的不要動;先檢查同一訂單有沒有出現兩個不同的值,有的話回報使用者。

## 步驟 2:專櫃網單(官網下單、算專櫃業績)

1. 從台中、信義營業日誌當月分頁的「備註」欄抓出所有訂單編號(`#` 或全形 `＃` 加 5 碼)。
   - 只看備註欄;「交接事項」欄裡的編號是取貨、調貨交接,不算。
   - 每個編號都要回當月訂單比對。對不到的(例如打錯成上個月的編號)不要自己猜,列出所在日期,回報使用者確認。
2. 新增分頁「網單公式」:A 欄放每天的原始備註,B 欄每列一個訂單編號,C 欄放「信義網單」或「台中網單」。
3. 在 Location 右邊插入一欄「輔助欄位」,第 2 列放:
   `=ARRAYFORMULA(IF(A2:A="", "", IFERROR(VLOOKUP(A2:A, '網單公式'!B:C, 2, FALSE), "找不到")))`
4. 網單的 Location 改成「信義大遠百」或「台中大遠百」(寫成文字值)。
5. 其餘 Location 空白的列填「官網」(使用者已確認要這樣做)。
6. 技巧:要大量改值時,先在空欄寫暫時公式算出結果,讀回來和自己算的比對,再用 `copyPaste` + `PASTE_VALUES` 貼成值,不要手打上千格。

## 步驟 3:海港城員工銷售統計

1. 只看 Location = `Harbour City` 的訂單。
2. Employee 欄是 POS 共用帳號(hello hello),不能用。員工名字從 **Discount Code** 判斷(例如「Kaley六週年」算 Kaley);折扣碼沒有名字再看 **Notes**。
   - 9 月出現的名字:Kaley、Chloe、Kristine、Aris、Hana、Yan、Phoenix、Tunng、亞、Kk、Amy、Vicky。名字大小寫不一(chloe、phoenix、KK),要統一。
   - 海港城員工有些和台灣專櫃同名,只要地點是海港城就照名字算。
   - **沒有寫名字的訂單不算。**
3. 不算進件數:沒有 SKU 的列(Tote bag 環保袋、「補差」之類)。有些自訂商品的 SKU 寫在品名欄(例如品名是 `33001BY3`),要算。
4. **退款要扣掉**:Financial Status = `refunded` 的訂單整張不算;`partially_refunded` 的要查是哪幾件被退(可用 Shopify 連接器查訂單),回報使用者再決定。
5. 新增兩個分頁:
   - **「海港城N月員工明細」**:每列一件商品。欄位:員工、日期(Created at 前 10 碼)、訂單編號、完整SKU、品項(SKU前5碼)、商品名稱(去掉最後的「 - 顏色 - 尺寸」與「(Pre-Order …)」前綴)、件數。依員工(件數多到少)再依日期排序。
   - **「海港城N月員工銷售」**:A–B 欄是每人合計(`=SUMIF` 加總右邊明細,最後一列「合計」),D–F 欄是每人每個品項的件數,F 欄用 `=SUMIFS('海港城N月員工明細'!$G$2:$G, '海港城N月員工明細'!$A$2:$A, D2, '海港城N月員工明細'!$E$2:$E, E2)` 從明細分頁加總。
6. SKU 前 5 碼要寫成文字(值前面加 `'`),否則像 `53E01` 會被當成科學記號。

## 每一步都要做的檢查

- 寫入後讀回來,和自己算的結果逐格比對,再跟使用者說完成。
- 不確定的資料(對不到的編號、沒名字的訂單、部分退款)列出來問使用者,不要自己猜。
- 讀大範圍資料時,工具結果太大會存成檔案;用 Python 從檔案解析,不要手抄。
