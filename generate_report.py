#!/usr/bin/env python3
"""
HAI Swimwear 週銷量報表產生器
每週一早上 9 點 (Taiwan Time, UTC+8) 自動執行
"""

import os
import sys
import json
import re
import smtplib
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from collections import defaultdict

# ─── 設定 ───────────────────────────────────────────────────────────────────

SHOPIFY_STORE    = os.environ.get("SHOPIFY_STORE", "")          # e.g. hai-the-label.myshopify.com
SHOPIFY_TOKEN    = os.environ.get("SHOPIFY_ACCESS_TOKEN", "")
GMAIL_USER       = os.environ.get("GMAIL_USER", "ann@hai-swimwear.com")
GMAIL_APP_PASS   = os.environ.get("GMAIL_APP_PASSWORD", "")
REPORT_RECIPIENT = os.environ.get("REPORT_RECIPIENT", "ann@hai-swimwear.com")

TWO_PIECE_LINES  = {"regular_two_piece"}  # 正線兩件式：T+B 算一套
JUNIOR_LINES     = {"junior"}             # Junior：每件分開算

# 尺寸代碼對應
SIZE_MAP = {"0": "XS", "1": "S", "2": "M", "3": "L", "4": "XL", "5": "XXL", "F": "F"}

# 產品類別顯示名稱
CATEGORY_NAMES = {
    "regular_one_piece":  "泳裝正線・一件式",
    "regular_two_piece":  "泳裝正線・兩件式",
    "junior":             "泳裝 Junior 線",
    "dress":              "洋裝 / Cover-up",
    "accessory":          "泳裝配件",
}

# 報表類別顯示順序
CATEGORY_ORDER = ["regular_one_piece", "regular_two_piece", "junior", "dress", "accessory"]

# ─── SKU 解析 ────────────────────────────────────────────────────────────────

def parse_sku(sku: str) -> dict | None:
    """解析 SKU，回傳 dict 或 None（無法解析時）"""
    if not sku or len(sku) < 5:
        return None
    product_code = sku[:5]
    if not sku[0].isdigit():
        return None

    second = product_code[1]
    third  = product_code[2]
    is_junior = product_code.startswith("415")

    # 分類
    if second == "1":
        if is_junior:
            category = "junior"
        elif third == "1":
            category = "regular_one_piece"
        elif third == "2":
            category = "regular_two_piece"
        else:
            category = "accessory"
    elif second == "3":
        category = "dress"
    else:
        category = "accessory"

    remainder = sku[5:]

    # 解析顏色、尺寸、上下身
    # 格式: COLOR(2字母) [CUP(1字母)] SIZE(1碼|F) [CUP_AFTER_SIZE] [T|B]
    m = re.match(
        r"^([A-Z]{2})([A-Z]?)([0-9F])(C?)([TB]?)(.*)$",
        remainder,
    )
    if m:
        color   = m.group(1)
        cup     = m.group(2) or (m.group(4) or None)
        size_ch = m.group(3)
        piece   = m.group(5) or None
        size    = SIZE_MAP.get(size_ch, size_ch)
    else:
        # fallback：至少取顏色
        color = remainder[:2] if len(remainder) >= 2 else remainder
        cup   = None
        size  = None
        piece = None

    return {
        "product_code": product_code,
        "color":        color,
        "cup":          cup,
        "size":         size,
        "piece":        piece,   # "T" / "B" / None
        "category":     category,
        "is_junior":    is_junior,
    }

# ─── 商品名稱快取 ────────────────────────────────────────────────────────────

_product_name_cache: dict[str, str] = {}

def product_display_name(product_code: str, title: str) -> str:
    """從訂單 title 萃取商品名（去除尺寸／顏色後綴）"""
    if product_code in _product_name_cache:
        return _product_name_cache[product_code]
    # title 格式通常: "(Pre-Order X) Brand | Product Name - Color"
    name = re.sub(r"\(.*?\)\s*", "", title).strip()
    name = re.sub(r"\s*-\s*[^-]+$", "", name).strip()
    _product_name_cache[product_code] = name
    return name

# ─── Shopify GraphQL API ──────────────────────────────────────────────────────

def shopify_gql(query: str, variables: dict = None) -> dict:
    url = f"https://{SHOPIFY_STORE}/admin/api/2024-10/graphql.json"
    payload = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": SHOPIFY_TOKEN,
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())

ORDER_QUERY = """
query ($after: String, $query: String!) {
  orders(first: 250, after: $after, query: $query) {
    edges {
      node {
        name
        createdAt
        displayFinancialStatus
        lineItems(first: 50) {
          edges {
            node {
              sku
              quantity
              title
              variant {
                inventoryItem { id }
              }
            }
          }
        }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

PRODUCT_CATALOG_QUERY = """
query ($after: String, $query: String!) {
  products(first: 250, after: $after, query: $query) {
    edges {
      node {
        title
        variants(first: 100) {
          edges {
            node {
              sku
              inventoryQuantity
              inventoryItem { id }
            }
          }
        }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

INVENTORY_QUERY = """
query ($ids: [ID!]!) {
  nodes(ids: $ids) {
    ... on InventoryItem {
      id
      inventoryLevels(first: 20) {
        edges {
          node {
            location { name }
            quantities(names: ["available"]) {
              name
              quantity
            }
          }
        }
      }
    }
  }
}
"""

# 預購倉關鍵字（不分大小寫）：含這些字的倉庫不計入庫存
PREORDER_LOCATION_KEYWORDS = ["預購倉", "pre-order", "preorder", "pre order"]

def is_preorder_location(name: str) -> bool:
    n = name.lower()
    return any(kw in n for kw in PREORDER_LOCATION_KEYWORDS)

def fetch_orders(date_from: datetime, date_to: datetime) -> list[dict]:
    """抓取指定日期區間所有已付款訂單（含退款單，後續扣除）"""
    q = f"created_at:>={date_from.strftime('%Y-%m-%dT%H:%M:%SZ')} created_at:<={date_to.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    orders, cursor = [], None
    while True:
        data = shopify_gql(ORDER_QUERY, {"after": cursor, "query": q})
        edges = data["data"]["orders"]["edges"]
        for e in edges:
            orders.append(e["node"])
        pi = data["data"]["orders"]["pageInfo"]
        if not pi["hasNextPage"]:
            break
        cursor = pi["endCursor"]
    return orders

def fetch_inventory_batch(inv_item_ids: list[str]) -> dict[str, int]:
    """一批取得庫存，回傳 {inventoryItemId: available_qty}（排除預購倉）"""
    result = {}
    batch_size = 50
    for i in range(0, len(inv_item_ids), batch_size):
        batch = inv_item_ids[i:i + batch_size]
        data = shopify_gql(INVENTORY_QUERY, {"ids": batch})
        for node in data.get("data", {}).get("nodes", []):
            if not node:
                continue
            total = 0
            for le in node.get("inventoryLevels", {}).get("edges", []):
                loc_name = le["node"].get("location", {}).get("name", "")
                if is_preorder_location(loc_name):
                    continue  # 跳過預購倉
                for q in le["node"].get("quantities", []):
                    if q["name"] == "available":
                        total += q["quantity"]
            result[node["id"]] = total
    return result

def fetch_product_catalog() -> dict[str, dict]:
    """
    抓取所有 4/5/6 開頭 SKU 的 ACTIVE 商品，回傳
    {product_code: {"name": str, "category": str, "inv_ids": set, "inventory": int}}
    用於確保這些品項即使本週銷量為 0 也會顯示在報表中。
    """
    catalog: dict[str, dict] = {}
    for prefix in ("4", "5", "6"):
        cursor = None
        while True:
            data = shopify_gql(
                PRODUCT_CATALOG_QUERY,
                {"after": cursor, "query": f"status:active sku:{prefix}*"},
            )
            for pe in data["data"]["products"]["edges"]:
                product = pe["node"]
                title = product["title"]
                for ve in product["variants"]["edges"]:
                    v = ve["node"]
                    sku = v.get("sku") or ""
                    parsed = parse_sku(sku)
                    if not parsed:
                        continue
                    pc = parsed["product_code"]
                    if not pc[0].isdigit() or pc[0] not in ("4", "5", "6"):
                        continue
                    inv_id = v["inventoryItem"]["id"] if v.get("inventoryItem") else None
                    inv_qty = v.get("inventoryQuantity") or 0
                    if pc not in catalog:
                        catalog[pc] = {
                            "name":     product_display_name(pc, title),
                            "category": parsed["category"],
                            "inv_ids":  set(),
                            "inventory": 0,
                        }
                    if inv_id:
                        catalog[pc]["inv_ids"].add(inv_id)
                    catalog[pc]["inventory"] += inv_qty
            pi = data["data"]["products"]["pageInfo"]
            if not pi["hasNextPage"]:
                break
            cursor = pi["endCursor"]
    return catalog


# ─── 計算銷售統計 ─────────────────────────────────────────────────────────────

def compute_stats(orders: list[dict], inventory: dict[str, int]):
    """
    回傳:
      category_totals: {category: 銷售數(套/件)}
      item_totals:     {category: {product_code: {"name":str, "count":float, "daily_avg":float, "inventory":int}}}
      color_totals:    {category: {product_code: {color: count}}}
      week_days:       int（本週天數，通常 7）
    """
    # {category: {product_code: {"pieces": {T: n, B: n, "solo": n}, "inv_ids": set, "name": str}}}
    raw = defaultdict(lambda: defaultdict(lambda: {
        "T": 0, "B": 0, "solo": 0, "inv_ids": set(), "name": ""
    }))
    # color 統計 {category: {product_code: {color: {T,B,solo}}}}
    raw_color = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: {
        "T": 0, "B": 0, "solo": 0
    })))
    # size 統計 {category: {product_code: {size: count}}}
    raw_size: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))

    inv_item_map: dict[str, str] = {}  # inventoryItemId → sku

    for order in orders:
        # 跳過退款訂單（整筆排除）
        if order.get("displayFinancialStatus") in ("REFUNDED", "VOIDED"):
            continue
        for edge in order["lineItems"]["edges"]:
            li = edge["node"]
            sku = li.get("sku") or ""
            qty = li.get("quantity", 1)
            title = li.get("title", "")
            parsed = parse_sku(sku)
            if not parsed:
                continue
            cat = parsed["category"]
            pc  = parsed["product_code"]
            col = parsed["color"] or "??"
            piece = parsed["piece"]  # T / B / None

            # 商品名
            if not raw[cat][pc]["name"]:
                raw[cat][pc]["name"] = product_display_name(pc, title)

            # 庫存 item id
            if li.get("variant") and li["variant"].get("inventoryItem"):
                inv_id = li["variant"]["inventoryItem"]["id"]
                raw[cat][pc]["inv_ids"].add(inv_id)
                inv_item_map[inv_id] = pc

            # 分片計數
            if piece == "T":
                raw[cat][pc]["T"] += qty
                raw_color[cat][pc][col]["T"] += qty
            elif piece == "B":
                raw[cat][pc]["B"] += qty
                raw_color[cat][pc][col]["B"] += qty
            else:
                raw[cat][pc]["solo"] += qty
                raw_color[cat][pc][col]["solo"] += qty

            # 尺寸統計（直接加件數，不換算套數）
            sz = parsed["size"] or "?"
            raw_size[cat][pc][sz] += qty

    week_days = 7

    # 將庫存數加總到 product level（多個 SKU 同 product_code 加總）
    # 先取出所有 inv_ids
    all_inv_ids = []
    for cat_data in raw.values():
        for pc_data in cat_data.values():
            all_inv_ids.extend(pc_data["inv_ids"])
    # inventory dict 從外部傳入，已含全部 SKU 庫存

    def to_sets(T: int, B: int, solo: int, category: str) -> float:
        """
        正線兩件式: (T+B)/2 套
        正線一件式/洋裝/Junior(件數): solo
        Junior: T+B 分開算
        """
        if category == "regular_two_piece":
            return (T + B) / 2
        elif category == "junior":
            return T + B + solo
        else:
            return solo + T + B  # one_piece / dress: 不會有T/B，但保險

    # ── item_totals ──
    item_totals: dict[str, dict] = defaultdict(dict)
    for cat, pc_dict in raw.items():
        for pc, d in pc_dict.items():
            count = to_sets(d["T"], d["B"], d["solo"], cat)
            # 庫存：該 product_code 下所有 inv_ids 加總
            inv_qty = sum(inventory.get(iid, 0) for iid in d["inv_ids"])
            item_totals[cat][pc] = {
                "name":       d["name"],
                "count":      count,
                "daily_avg":  round(count / week_days, 2),
                "inventory":  inv_qty,
                "days_left":  round(inv_qty / (count / week_days), 1) if count > 0 else "∞",
            }

    # ── color_totals ──
    color_totals: dict[str, dict] = defaultdict(dict)
    for cat, pc_dict in raw_color.items():
        for pc, col_dict in pc_dict.items():
            color_totals[cat][pc] = {}
            for col, d in col_dict.items():
                count = to_sets(d["T"], d["B"], d["solo"], cat)
                color_totals[cat][pc][col] = count

    # ── category totals ──
    category_totals = {
        cat: sum(v["count"] for v in pc_dict.values())
        for cat, pc_dict in item_totals.items()
    }

    # ── size_totals: {category: {product_code: {size: qty}}} ──
    size_totals: dict = {cat: dict(pc_dict) for cat, pc_dict in raw_size.items()}

    # ── sku_details: {product_code: set of sizes} ──
    sku_details: dict[str, set] = defaultdict(set)
    for cat_dict in raw_size.values():
        for pc, sz_dict in cat_dict.items():
            sku_details[pc].update(sz_dict.keys())

    return category_totals, item_totals, color_totals, size_totals, week_days, sku_details

# ─── HTML 報表生成 ────────────────────────────────────────────────────────────

def collect_sizes_colors(color_totals: dict, item_totals: dict) -> tuple[list, list]:
    """收集報表中所有出現的尺寸和顏色，供篩選器使用"""
    all_colors: set[str] = set()
    # 從 color_totals 取顏色（已含顏色代碼）
    for cat_dict in color_totals.values():
        for col_dict in cat_dict.values():
            all_colors.update(col_dict.keys())
    return sorted(all_colors)


def render_html(
    week_label: str,
    category_totals: dict,
    item_totals: dict,
    color_totals: dict,
    size_totals: dict,
    week_days: int,
    sku_details: dict = None,  # {product_code: set of sizes} for filter
) -> str:
    CAT_COLORS = {
        "regular_one_piece": "#0ea5e9",
        "regular_two_piece": "#8b5cf6",
        "junior":            "#ec4899",
        "dress":             "#f59e0b",
        "accessory":         "#94a3b8",
    }

    # 收集所有顏色供篩選器
    all_colors = collect_sizes_colors(color_totals, item_totals)

    def badge(text, color="#6366f1", bg=None):
        bg = bg or color + "1a"
        return f'<span style="display:inline-block;padding:2px 10px;border-radius:20px;font-size:12px;font-weight:600;color:{color};background:{bg};">{text}</span>'

    def inventory_bar(days_left):
        if days_left == "∞":
            return '<span style="color:#94a3b8;">—</span>'
        days = float(days_left)
        color = "#22c55e" if days > 14 else ("#f59e0b" if days > 7 else "#ef4444")
        return f'<span style="color:{color};font-weight:700;">{days} 天</span>'

    SIZE_ORDER = ["XS", "S", "M", "L", "XL", "XXL", "F", "?"]

    def color_breakdown(cat, pc):
        col_data = color_totals.get(cat, {}).get(pc, {})
        if not col_data:
            return ""
        unit = "套" if cat == "regular_two_piece" else "件"
        sorted_cols = sorted(col_data.items(), key=lambda x: -x[1])
        chips = " ".join(
            f'<span class="color-chip" style="display:inline-block;margin:2px;padding:2px 8px;border-radius:12px;font-size:11px;background:#f1f5f9;color:#475569;">'
            f'{col} {v:.1f}{unit}</span>'
            for col, v in sorted_cols
        )
        return f'<div style="margin-top:3px;"><span style="font-size:10px;color:#94a3b8;font-weight:600;margin-right:4px;">顏色</span>{chips}</div>'

    def size_breakdown(cat, pc):
        sz_data = size_totals.get(cat, {}).get(pc, {})
        if not sz_data:
            return ""
        sorted_sizes = sorted(sz_data.items(), key=lambda x: (SIZE_ORDER.index(x[0]) if x[0] in SIZE_ORDER else 99))
        chips = " ".join(
            f'<span style="display:inline-block;margin:2px;padding:2px 8px;border-radius:12px;font-size:11px;background:#eff6ff;color:#2563eb;">'
            f'{sz} {qty}件</span>'
            for sz, qty in sorted_sizes
        )
        return f'<div style="margin-top:3px;"><span style="font-size:10px;color:#94a3b8;font-weight:600;margin-right:4px;">尺寸</span>{chips}</div>'

    # ── 建立所有品項行（含 data 屬性供 JS 篩選）
    all_rows_html = ""
    for cat in CATEGORY_ORDER:
        pc_dict = item_totals.get(cat)
        if not pc_dict:
            continue
        unit = "套" if cat == "regular_two_piece" else "件"
        cat_label = CATEGORY_NAMES.get(cat, cat)
        cat_color = CAT_COLORS.get(cat, "#6366f1")
        cat_total = category_totals.get(cat, 0)

        # 分類標題行
        all_rows_html += f"""
        <tr class="cat-header" data-cat="{cat}">
          <td colspan="5" style="padding:20px 0 6px;">
            <div style="display:flex;align-items:center;gap:8px;">
              <div style="width:4px;height:20px;background:{cat_color};border-radius:2px;"></div>
              <span style="font-size:15px;font-weight:700;color:#0f172a;">{cat_label}</span>
              <span class="cat-badge" data-cat="{cat}" style="display:inline-block;padding:2px 10px;border-radius:20px;font-size:12px;font-weight:600;color:{cat_color};background:{cat_color}1a;">本週 {cat_total:.1f} {unit}</span>
            </div>
          </td>
        </tr>
        <tr class="cat-header" data-cat="{cat}" style="background:#f1f5f9;">
          <th style="padding:8px 12px;text-align:left;font-size:12px;color:#64748b;font-weight:600;">品項</th>
          <th style="padding:8px 12px;text-align:center;font-size:12px;color:#64748b;font-weight:600;">週銷量</th>
          <th style="padding:8px 12px;text-align:center;font-size:12px;color:#64748b;font-weight:600;">日均銷量</th>
          <th style="padding:8px 12px;text-align:center;font-size:12px;color:#64748b;font-weight:600;">現有庫存</th>
          <th style="padding:8px 12px;text-align:center;font-size:12px;color:#64748b;font-weight:600;">預估可售天數</th>
        </tr>
        """

        sorted_items = sorted(pc_dict.items(), key=lambda x: -x[1]["count"])
        for i, (pc, d) in enumerate(sorted_items):
            bg = "#ffffff" if i % 2 == 0 else "#fafafa"
            col_data = color_totals.get(cat, {}).get(pc, {})
            colors_str = " ".join(col_data.keys())
            sizes_str = " ".join((sku_details or {}).get(pc, set()))
            all_rows_html += f"""
            <tr class="item-row" data-cat="{cat}" data-pc="{pc}" data-colors="{colors_str}" data-sizes="{sizes_str}"
                style="background:{bg};border-bottom:1px solid #f1f5f9;">
              <td style="padding:10px 12px;">
                <div style="font-weight:600;color:#0f172a;">{pc}</div>
                <div style="font-size:12px;color:#64748b;">{d['name']}</div>
                {color_breakdown(cat, pc)}
                {size_breakdown(cat, pc)}
              </td>
              <td style="padding:10px 12px;text-align:center;font-size:15px;font-weight:700;color:#0f172a;">{d['count']:.1f} {unit}</td>
              <td style="padding:10px 12px;text-align:center;color:#475569;">{d['daily_avg']} {unit}/天</td>
              <td style="padding:10px 12px;text-align:center;font-weight:600;color:#0f172a;">{d['inventory']}</td>
              <td style="padding:10px 12px;text-align:center;">{inventory_bar(d['days_left'])}</td>
            </tr>
            """

    # 額外分析：熱銷排行、庫存警示（只算有銷量品項）
    all_items = []
    for cat, pd in item_totals.items():
        for pc, d in pd.items():
            all_items.append({**d, "pc": pc, "cat": cat})

    top5 = sorted([x for x in all_items if x["count"] > 0], key=lambda x: -x["count"])[:5]
    low_stock = [x for x in all_items if x["count"] > 0 and isinstance(x["days_left"], float) and x["days_left"] < 7]

    top5_rows = "".join(
        f'<tr style="border-bottom:1px solid #f1f5f9;">'
        f'<td style="padding:8px 12px;">{i+1}. <b>{x["pc"]}</b> <span style="color:#64748b;font-size:12px;">{x["name"][:30]}</span></td>'
        f'<td style="padding:8px 12px;text-align:center;font-weight:700;">{x["count"]:.1f}</td>'
        f'<td style="padding:8px 12px;text-align:center;color:#64748b;">{CATEGORY_NAMES.get(x["cat"],"")}</td>'
        f'</tr>'
        for i, x in enumerate(top5)
    ) if top5 else '<tr><td colspan="3" style="padding:12px;color:#94a3b8;text-align:center;">本週尚無銷量資料</td></tr>'

    if low_stock:
        low_rows = "".join(
            f'<tr style="border-bottom:1px solid #f1f5f9;">'
            f'<td style="padding:8px 12px;"><b>{x["pc"]}</b> {x["name"][:28]}</td>'
            f'<td style="padding:8px 12px;text-align:center;">{x["inventory"]}</td>'
            f'<td style="padding:8px 12px;text-align:center;color:#ef4444;font-weight:700;">{x["days_left"]} 天</td>'
            f'</tr>'
            for x in sorted(low_stock, key=lambda x: x["days_left"])
        )
        low_section = f"""
        <h3 style="margin:24px 0 10px;color:#ef4444;font-size:14px;">⚠️ 庫存警示（不足 7 天）</h3>
        <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;background:#fff5f5;border-radius:8px;overflow:hidden;">
          <tr style="background:#fee2e2;">
            <th style="padding:8px 12px;text-align:left;font-size:12px;color:#991b1b;">品項</th>
            <th style="padding:8px 12px;text-align:center;font-size:12px;color:#991b1b;">庫存</th>
            <th style="padding:8px 12px;text-align:center;font-size:12px;color:#991b1b;">預估可售天數</th>
          </tr>
          {low_rows}
        </table>
        """
    else:
        low_section = '<p style="color:#22c55e;font-size:13px;">✅ 所有品項庫存充足（均可售超過 7 天）</p>'

    total_orders_sold = sum(v for v in category_totals.values())

    # 篩選器選項
    color_options = "".join(f'<option value="{c}">{c}</option>' for c in all_colors)
    size_options = "".join(
        f'<option value="{s}">{s}</option>'
        for s in ["XS", "S", "M", "L", "XL", "XXL", "F"]
    )
    cat_options = "".join(
        f'<option value="{k}">{v}</option>'
        for k, v in CATEGORY_NAMES.items()
    )

    filter_js = """
<script>
function applyFilters() {
  var cat   = document.getElementById('f-cat').value;
  var color = document.getElementById('f-color').value.toUpperCase().trim();
  var size  = document.getElementById('f-size').value;
  var pc    = document.getElementById('f-pc').value.toUpperCase().trim();

  // 先顯示所有行
  document.querySelectorAll('.item-row').forEach(function(row) {
    var rCat    = row.dataset.cat;
    var rPc     = row.dataset.pc;
    var rColors = (row.dataset.colors || '').toUpperCase();
    var rSizes  = (row.dataset.sizes  || '').toUpperCase();

    var show = true;
    if (cat   && rCat !== cat) show = false;
    if (color && rColors.indexOf(color) === -1) show = false;
    if (size  && rSizes.indexOf(size)   === -1) show = false;
    if (pc    && rPc.indexOf(pc)        === -1) show = false;
    row.style.display = show ? '' : 'none';
  });

  // 分類標題：若該分類下沒有任何可見 item-row 就隱藏
  document.querySelectorAll('.cat-header').forEach(function(header) {
    var hCat = header.dataset.cat;
    if (cat && hCat !== cat) { header.style.display = 'none'; return; }
    var hasVisible = Array.from(
      document.querySelectorAll('.item-row[data-cat="' + hCat + '"]')
    ).some(function(r){ return r.style.display !== 'none'; });
    header.style.display = hasVisible ? '' : 'none';
  });

  // 無結果提示
  var noRes = document.getElementById('no-results');
  var anyVisible = Array.from(document.querySelectorAll('.item-row'))
    .some(function(r){ return r.style.display !== 'none'; });
  noRes.style.display = anyVisible ? 'none' : '';
}

function clearFilters() {
  ['f-cat','f-color','f-size','f-pc'].forEach(function(id){
    document.getElementById(id).value = '';
  });
  applyFilters();
}
</script>
"""

    html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>HAI Swimwear 週銷量報表</title>
{filter_js}
</head>
<body style="margin:0;padding:0;background:#f8f9fb;font-family:'Helvetica Neue',Arial,sans-serif;">
<div style="max-width:760px;margin:0 auto;padding:24px 16px;">

  <!-- Header -->
  <div style="background:#0f172a;border-radius:12px 12px 0 0;padding:24px 28px;">
    <div style="color:#f8fafc;font-size:22px;font-weight:800;letter-spacing:-0.5px;">HAI Swimwear</div>
    <div style="color:#94a3b8;font-size:13px;margin-top:4px;">週銷量報表・{week_label}</div>
  </div>

  <!-- Summary Cards -->
  <div style="background:#1e293b;padding:16px 28px 20px;">
    <div style="display:flex;gap:12px;">
      <div style="flex:1;background:#334155;border-radius:8px;padding:14px;">
        <div style="color:#94a3b8;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;">本週總銷量</div>
        <div style="color:#f8fafc;font-size:28px;font-weight:800;margin-top:4px;">{total_orders_sold:.1f}</div>
        <div style="color:#64748b;font-size:12px;">件 / 套</div>
      </div>
      <div style="flex:1;background:#334155;border-radius:8px;padding:14px;">
        <div style="color:#94a3b8;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;">日均銷量</div>
        <div style="color:#f8fafc;font-size:28px;font-weight:800;margin-top:4px;">{total_orders_sold/week_days:.1f}</div>
        <div style="color:#64748b;font-size:12px;">件·套 / 天（共 {week_days} 天）</div>
      </div>
    </div>
  </div>

  <!-- 篩選器 -->
  <div style="background:#ffffff;border-top:1px solid #e2e8f0;padding:16px 28px;">
    <div style="font-size:13px;font-weight:600;color:#0f172a;margin-bottom:10px;">🔍 篩選</div>
    <div style="display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end;">
      <div>
        <div style="font-size:11px;color:#64748b;margin-bottom:4px;">分類</div>
        <select id="f-cat" onchange="applyFilters()"
          style="padding:7px 10px;border:1px solid #e2e8f0;border-radius:6px;font-size:13px;color:#0f172a;background:#f8f9fb;min-width:140px;">
          <option value="">全部分類</option>
          {cat_options}
        </select>
      </div>
      <div>
        <div style="font-size:11px;color:#64748b;margin-bottom:4px;">型號</div>
        <input id="f-pc" oninput="applyFilters()" placeholder="例: 41102"
          style="padding:7px 10px;border:1px solid #e2e8f0;border-radius:6px;font-size:13px;color:#0f172a;background:#f8f9fb;width:100px;">
      </div>
      <div>
        <div style="font-size:11px;color:#64748b;margin-bottom:4px;">顏色代碼</div>
        <select id="f-color" onchange="applyFilters()"
          style="padding:7px 10px;border:1px solid #e2e8f0;border-radius:6px;font-size:13px;color:#0f172a;background:#f8f9fb;min-width:100px;">
          <option value="">全部顏色</option>
          {color_options}
        </select>
      </div>
      <div>
        <div style="font-size:11px;color:#64748b;margin-bottom:4px;">尺寸</div>
        <select id="f-size" onchange="applyFilters()"
          style="padding:7px 10px;border:1px solid #e2e8f0;border-radius:6px;font-size:13px;color:#0f172a;background:#f8f9fb;min-width:80px;">
          <option value="">全部尺寸</option>
          {size_options}
        </select>
      </div>
      <button onclick="clearFilters()"
        style="padding:7px 16px;background:#f1f5f9;border:1px solid #e2e8f0;border-radius:6px;font-size:13px;color:#475569;cursor:pointer;">
        清除篩選
      </button>
    </div>
  </div>

  <!-- Main Table -->
  <div style="background:#ffffff;padding:0 28px 24px;">
    <table id="main-table" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
      {all_rows_html}
      <tr id="no-results" style="display:none;">
        <td colspan="5" style="padding:32px;text-align:center;color:#94a3b8;font-size:14px;">
          找不到符合篩選條件的品項
        </td>
      </tr>
    </table>
  </div>

  <!-- Additional Analysis -->
  <div style="background:#ffffff;padding:0 28px 28px;">
    <div style="border-top:1px solid #e2e8f0;padding-top:20px;">
      <h3 style="margin:0 0 10px;color:#0f172a;font-size:14px;">🔥 本週熱銷 Top 5</h3>
      <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;border-radius:8px;overflow:hidden;border:1px solid #e2e8f0;">
        <tr style="background:#f8f9fb;">
          <th style="padding:8px 12px;text-align:left;font-size:12px;color:#64748b;">品項</th>
          <th style="padding:8px 12px;text-align:center;font-size:12px;color:#64748b;">銷量</th>
          <th style="padding:8px 12px;text-align:center;font-size:12px;color:#64748b;">類別</th>
        </tr>
        {top5_rows}
      </table>

      <div style="margin-top:20px;">
        {low_section}
      </div>

      <div style="margin-top:20px;padding:14px;background:#f0fdf4;border-radius:8px;border-left:3px solid #22c55e;">
        <div style="font-size:13px;font-weight:600;color:#166534;margin-bottom:6px;">📊 補充分析說明</div>
        <ul style="margin:0;padding-left:18px;font-size:12px;color:#166534;line-height:1.8;">
          <li>正線兩件式以「套」計算（上下身各一算一套）</li>
          <li>Junior 線以「件」計算（上下身各算一件）</li>
          <li>「預估可售天數」= 現有庫存 ÷ 日均銷量，供備貨參考</li>
          <li>庫存低於 7 天者標示紅色警示；∞ 表示本週銷量為 0</li>
          <li>庫存數量已排除預購倉</li>
        </ul>
      </div>
    </div>
  </div>

  <!-- Footer -->
  <div style="background:#f1f5f9;border-radius:0 0 12px 12px;padding:16px 28px;text-align:center;">
    <div style="color:#94a3b8;font-size:11px;">HAI Swimwear 自動報表系統・每週一 09:00 Taiwan Time 發送</div>
    <div style="color:#cbd5e1;font-size:11px;margin-top:2px;">報表產生時間：{datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')} TWN</div>
  </div>

</div>
</body>
</html>"""
    return html

# ─── 發送郵件 ─────────────────────────────────────────────────────────────────

def send_email(subject: str, html_body: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = REPORT_RECIPIENT
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASS)
        server.sendmail(GMAIL_USER, REPORT_RECIPIENT, msg.as_string())
    print(f"✅ 報表已寄出至 {REPORT_RECIPIENT}")

# ─── 多週導航 ─────────────────────────────────────────────────────────────────

def get_weekly_reports_index(base_dir: str) -> list[dict]:
    """
    掃描 reports/ 目錄，回傳所有週報表清單（由新到舊）
    格式: [{"date": "2026-06-22", "label": "2026/06/22（一）~ 2026/06/28（日）", "file": "reports/2026-06-22.html"}]
    """
    reports_dir = os.path.join(base_dir, "reports")
    if not os.path.isdir(reports_dir):
        return []
    entries = []
    for fn in os.listdir(reports_dir):
        m = re.match(r"^(\d{4}-\d{2}-\d{2})\.html$", fn)
        if not m:
            continue
        date_str = m.group(1)
        try:
            monday = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            continue
        sunday = monday + timedelta(days=6)
        weekdays = ["一", "二", "三", "四", "五", "六", "日"]
        label = f"{monday.strftime('%Y/%m/%d')}（{weekdays[monday.weekday()]}）~ {sunday.strftime('%Y/%m/%d')}（{weekdays[sunday.weekday()]}）"
        entries.append({
            "date":  date_str,
            "label": label,
            "file":  f"reports/{fn}",
        })
    entries.sort(key=lambda x: x["date"], reverse=True)
    return entries


def render_index_html(reports: list[dict]) -> str:
    """產生多週導航頁面 index.html"""
    tz_tw = timezone(timedelta(hours=8))
    now_str = datetime.now(tz_tw).strftime("%Y-%m-%d %H:%M")

    if reports:
        latest = reports[0]
        latest_link = f'<a href="{latest["file"]}" style="color:#0ea5e9;text-decoration:none;font-weight:700;">→ 查看最新報表：{latest["label"]}</a>'
    else:
        latest_link = '<span style="color:#94a3b8;">尚無報表</span>'

    rows = ""
    for i, r in enumerate(reports):
        bg = "#ffffff" if i % 2 == 0 else "#f8f9fb"
        tag = ' <span style="font-size:10px;padding:2px 8px;background:#0ea5e9;color:#fff;border-radius:20px;font-weight:600;">最新</span>' if i == 0 else ""
        rows += f"""
        <tr style="background:{bg};border-bottom:1px solid #f1f5f9;">
          <td style="padding:12px 16px;font-size:13px;color:#0f172a;">{r["label"]}{tag}</td>
          <td style="padding:12px 16px;text-align:right;">
            <a href="{r["file"]}" style="color:#0ea5e9;font-size:13px;text-decoration:none;font-weight:600;">查看報表 →</a>
          </td>
        </tr>"""

    if not rows:
        rows = '<tr><td colspan="2" style="padding:24px;text-align:center;color:#94a3b8;">尚無歷史報表</td></tr>'

    return f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>HAI Swimwear 週銷量報表・歷史紀錄</title>
</head>
<body style="margin:0;padding:0;background:#f8f9fb;font-family:'Helvetica Neue',Arial,sans-serif;">
<div style="max-width:680px;margin:0 auto;padding:24px 16px;">

  <div style="background:#0f172a;border-radius:12px 12px 0 0;padding:24px 28px;">
    <div style="color:#f8fafc;font-size:22px;font-weight:800;letter-spacing:-0.5px;">HAI Swimwear</div>
    <div style="color:#94a3b8;font-size:13px;margin-top:4px;">週銷量報表・歷史紀錄</div>
  </div>

  <div style="background:#1e293b;padding:16px 28px;">
    <div style="color:#cbd5e1;font-size:13px;">{latest_link}</div>
  </div>

  <div style="background:#ffffff;padding:0 0 8px;">
    <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
      <tr style="background:#f1f5f9;">
        <th style="padding:10px 16px;text-align:left;font-size:12px;color:#64748b;font-weight:600;">週次</th>
        <th style="padding:10px 16px;text-align:right;font-size:12px;color:#64748b;font-weight:600;"></th>
      </tr>
      {rows}
    </table>
  </div>

  <div style="background:#f1f5f9;border-radius:0 0 12px 12px;padding:14px 28px;text-align:center;">
    <div style="color:#94a3b8;font-size:11px;">HAI Swimwear 自動報表系統・每週一 09:00 Taiwan Time 發送</div>
    <div style="color:#cbd5e1;font-size:11px;margin-top:2px;">更新時間：{now_str} TWN</div>
  </div>

</div>
</body>
</html>"""


# ─── 主程式 ───────────────────────────────────────────────────────────────────

def get_week_range(ref_date: datetime = None):
    """
    取得「上一週」(週一~週日) 的日期區間（UTC+8 判斷）
    若 ref_date 為週一，回傳上週 Mon 00:00 ~ Sun 23:59:59 (UTC)
    """
    tz_tw = timezone(timedelta(hours=8))
    now_tw = ref_date or datetime.now(tz_tw)
    # 找到本週一（今天如果是週一，就是今天）
    days_since_monday = now_tw.weekday()  # 0=Mon
    this_monday = now_tw.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_since_monday)
    last_monday = this_monday - timedelta(weeks=1)
    last_sunday  = this_monday - timedelta(seconds=1)
    # 轉回 UTC for API query
    last_monday_utc = last_monday.astimezone(timezone.utc)
    last_sunday_utc = last_sunday.astimezone(timezone.utc)
    week_label = f"{last_monday.strftime('%Y/%m/%d')}（一）~ {last_sunday.strftime('%Y/%m/%d')}（日）"
    monday_date_str = last_monday.strftime("%Y-%m-%d")
    return last_monday_utc, last_sunday_utc, week_label, monday_date_str

def main():
    if not SHOPIFY_STORE or not SHOPIFY_TOKEN:
        print("❌ 請設定 SHOPIFY_STORE 和 SHOPIFY_ACCESS_TOKEN 環境變數")
        sys.exit(1)
    if not GMAIL_APP_PASS:
        print("❌ 請設定 GMAIL_APP_PASSWORD 環境變數")
        sys.exit(1)

    date_from, date_to, week_label, monday_date_str = get_week_range()
    print(f"📅 抓取訂單區間：{week_label}")

    print("🔄 正在從 Shopify 抓取訂單...")
    orders = fetch_orders(date_from, date_to)
    print(f"   → 共取得 {len(orders)} 筆訂單")

    # 收集所有 inventory item IDs
    inv_ids = set()
    for order in orders:
        for edge in order["lineItems"]["edges"]:
            li = edge["node"]
            if li.get("variant") and li["variant"].get("inventoryItem"):
                inv_ids.add(li["variant"]["inventoryItem"]["id"])

    print(f"🔄 正在查詢庫存（{len(inv_ids)} 個 SKU）...")
    inventory = fetch_inventory_batch(list(inv_ids))

    print("🔄 正在抓取商品目錄（4/5/6 開頭品項）...")
    product_catalog = fetch_product_catalog()
    print(f"   → 共取得 {len(product_catalog)} 個商品代碼")

    print("📊 計算銷售統計...")
    category_totals, item_totals, color_totals, size_totals, week_days, sku_details = compute_stats(orders, inventory)

    # 將目錄中的品項合入 item_totals（4/5/6 開頭，銷量=0 也要顯示）
    for pc, cat_info in product_catalog.items():
        cat = cat_info["category"]
        if pc not in item_totals.get(cat, {}):
            item_totals.setdefault(cat, {})[pc] = {
                "name":      cat_info["name"],
                "count":     0,
                "daily_avg": 0,
                "inventory": cat_info["inventory"],
                "days_left": "∞",
            }

    # 重新計算 category_totals（含補入的 0 銷量品項不影響加總）
    category_totals = {
        cat: sum(v["count"] for v in pc_dict.values())
        for cat, pc_dict in item_totals.items()
    }

    print("🎨 生成 HTML 報表...")
    html = render_html(week_label, category_totals, item_totals, color_totals, size_totals, week_days, sku_details)

    base_dir = os.path.dirname(os.path.abspath(__file__))

    # 儲存週報表至 reports/YYYY-MM-DD.html
    reports_dir = os.path.join(base_dir, "reports")
    os.makedirs(reports_dir, exist_ok=True)
    week_report_path = os.path.join(reports_dir, f"{monday_date_str}.html")
    with open(week_report_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"💾 週報表已存至 {week_report_path}")

    # 更新導航 index.html
    reports_list = get_weekly_reports_index(base_dir)
    index_html = render_index_html(reports_list)
    index_path = os.path.join(base_dir, "index.html")
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(index_html)
    print(f"💾 導航頁面已更新至 {index_path}")

    subject = f"HAI Swimwear 週銷量報表・{week_label}"
    print(f"📧 寄送報表至 {REPORT_RECIPIENT}...")
    send_email(subject, html)

if __name__ == "__main__":
    main()
