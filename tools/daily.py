#!/usr/bin/env python3
"""每日熱銷品報表更新工具。

用法(cutoff = 資料截止日,通常是台灣時間昨天;不給 --cutoff 就自動用台灣時間昨天):
  python3 tools/daily.py today                 # 印出今天要用的資料截止日
  python3 tools/daily.py queries               # 印出要跑的 Shopify 查詢(ShopifyQL x6、GraphQL 訂單、補庫存商品)
  python3 tools/daily.py check                 # 檢查試算表欄位標題、官網有沒有新泳裝品號
  python3 tools/daily.py build                 # 從查詢結果算出要寫進試算表的值
  python3 tools/daily.py payload               # 印出要寫進試算表的每一段 range + values
  python3 tools/daily.py render                # 用試算表讀回的值產生每日報表 HTML
  python3 tools/daily.py finish                # 記錄已出過的 60 天報告、更新 last_cutoff

查詢結果的來源(依序都會讀):
  1. state/in/*.json:手動或其他工具存下來的查詢結果(每個檔一個 JSON 結果)
  2. Claude Code 對話紀錄(~/.claude/projects/*/*.jsonl)與大型結果檔(tool-results/*.txt)
環境設定(試算表 ID、artifact 連結等)在 state/config.json。
"""
import argparse, collections, datetime as dt, glob, html, json, math, os, re, statistics, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = json.load(open(os.path.join(ROOT, 'state', 'items.json')))
CONF = json.load(open(os.path.join(ROOT, 'state', 'config.json')))
OUT = os.path.join(ROOT, 'state', 'out')
PREFIXES = "product_variant_sku STARTS WITH '415' OR product_variant_sku STARTS WITH '41307' OR product_variant_sku STARTS WITH '61115' OR product_variant_sku STARTS WITH '71'"
D = dt.date


def days(a, b):
    out, d = [], a
    while d <= b:
        out.append(d.isoformat()); d += dt.timedelta(1)
    return out


def windows(cut):
    return {
        'w30': (cut - dt.timedelta(29), cut), 'w7': (cut - dt.timedelta(6), cut),
        'p30': (cut - dt.timedelta(59), cut - dt.timedelta(30)), 'w60': (cut - dt.timedelta(59), cut),
    }


def queries(cut):
    w = windows(cut); s = lambda d: d.isoformat()
    return {
        'daily': f"FROM sales SHOW net_items_sold, gross_sales, discounts, net_sales WHERE {PREFIXES} GROUP BY product_variant_sku, day SINCE {s(w['w60'][0])} UNTIL {s(cut)} LIMIT 10000",
        'cum': f"FROM sales SHOW net_items_sold WHERE {PREFIXES} GROUP BY product_variant_sku SINCE 2023-01-01 UNTIL {s(cut)} LIMIT 10000",
        'inv': f"FROM inventory_by_location SHOW ending_inventory_units_at_location WHERE {PREFIXES} GROUP BY product_variant_sku, inventory_location_name SINCE {s(cut)} UNTIL {s(cut)} LIMIT 10000",
        'stock30': f"FROM inventory SHOW days_in_stock, days_out_of_stock WHERE {PREFIXES} GROUP BY product_variant_sku SINCE {s(w['w30'][0])} UNTIL {s(cut)} LIMIT 10000",
        'stockp30': f"FROM inventory SHOW days_in_stock, days_out_of_stock WHERE {PREFIXES} GROUP BY product_variant_sku SINCE {s(w['p30'][0])} UNTIL {s(w['p30'][1])} LIMIT 10000",
        'store': f"FROM sales SHOW net_items_sold TIMESERIES day SINCE {s(w['w60'][0])} UNTIL {s(cut)}",
    }


# ---------- reading tool results ----------
def _texts():
    for f in sorted(glob.glob(os.path.join(ROOT, 'state', 'in', '*.json')), key=os.path.getmtime):
        yield open(f, errors='ignore').read()
    for f in sorted(glob.glob(os.path.expanduser('~/.claude/projects/*/*.jsonl')), key=os.path.getmtime):
        for line in open(f, errors='ignore'):
            try: e = json.loads(line)
            except Exception: continue
            c = e.get('message', {}).get('content')
            if not isinstance(c, list): continue
            for b in c:
                if b.get('type') != 'tool_result': continue
                cc = b.get('content')
                yield cc if isinstance(cc, str) else ''.join(x.get('text', '') for x in cc if isinstance(x, dict))
    for f in sorted(glob.glob(os.path.expanduser('~/.claude/projects/*/*/tool-results/*.txt')), key=os.path.getmtime):
        yield open(f, errors='ignore').read()


def load_results():
    ql, gql = {}, []
    for t in _texts():
        try: d = json.loads(t)
        except Exception: continue
        if isinstance(d, dict) and 'inventoryItems' in d:
            invlevels.append(d)
        elif isinstance(d, dict) and 'query' in d and 'rows' in d:
            ql[d['query']] = d['rows']
        elif isinstance(d, dict) and isinstance(d.get('data'), dict):
            gql.append(d['data'])
        elif isinstance(d, dict) and isinstance(d.get('content'), dict) and 'values' in d['content']:
            sheets.append(d['content'])
        elif isinstance(d, dict) and 'range' in d and 'values' in d:
            sheets.append(d)
    return ql, gql


sheets = []
invlevels = []  # get-inventory-levels 結果(補 inventory_by_location 漏掉的 SKU)


def sheet_launch_dates():
    """從最近一次讀回的 SKU動銷分析 A1 範圍,取 C 欄(預購上架日期)當開賣日。試算表為準。"""
    for d in reversed(sheets):
        if 'SKU' in d.get('range', '') and d['values'] and d['values'][0][:3] == ['PO單號', '品項數字', '預購上架日期']:
            out = {}
            for r in d['values'][2:]:
                if len(r) > 2 and r[1] and re.fullmatch(r'\d{4}/\d{1,2}/\d{1,2}', r[2] or ''):
                    y, m, dd = map(int, r[2].split('/')); out[r[1]] = D(y, m, dd).isoformat()
            if out: return out
    return {}


# ---------- SKU parsing ----------
SZ = {'1': 'S', '2': 'M', '3': 'L', '4': '4', '0': 'XS'}


def code_of(sku):
    return sku[:5]


def parse(sku):
    code, rest = sku[:5], sku[5:]
    col, t = rest[:2], rest[2:]
    if code == '71108' and re.fullmatch(r'[12]\d\d[A-D]', t):
        return code, col, '上身', SZ[t[0]] + '/' + t[1:]
    part = '—'
    if t.endswith('T') and code != '41307': part, t = '上身', t[:-1]
    elif t.endswith('B'): part, t = '下身', t[:-1]
    if t == 'F': size = 'F'
    elif re.fullmatch(r'3\d[A-D]', t): size = t
    else: size = SZ.get(t, t)
    return code, col, part, size


def tracked():
    return [i for i in CFG['items'] if not i.get('placeholder')]


def skip(sku):
    return any(sku.startswith(p) for p in CFG['ignore_prefixes']) or re.fullmatch(r'71108(OB|GW)[123]', sku)


# ---------- build ----------
def build(cut):
    ql, gql = load_results()
    Q = queries(cut)
    miss = [k for k, q in Q.items() if q not in ql]
    if miss:
        sys.exit('缺少查詢結果: ' + ', '.join(miss) + '\n請先跑: ' + '\n'.join(Q[k] for k in miss))
    codes = {i['code'] for i in tracked()}
    sl = sheet_launch_dates()
    for it in tracked():
        if it['code'] in sl and sl[it['code']] != it['launch']:
            print(f"開賣日以試算表 C 欄為準: {it['code']} {it['launch']} -> {sl[it['code']]}")
            it['launch'] = sl[it['code']]
    json.dump(CFG, open(os.path.join(ROOT, 'state', 'items.json'), 'w'), ensure_ascii=False, indent=1)
    w = windows(cut)
    d30, d7, dp = set(days(*w['w30'])), set(days(*w['w7'])), set(days(*w['p30']))
    daily = collections.defaultdict(collections.Counter)
    money = collections.defaultdict(lambda: [0.0, 0.0, 0.0])  # gross, disc, net in w30
    sku_seen = set()
    for sku, day, n, g, di, ns in ql[Q['daily']]:
        if skip(sku) or code_of(sku) not in codes: continue
        sku_seen.add(sku)
        daily[code_of(sku)][day] += int(n)
        if day in d30:
            m = money[code_of(sku)]; m[0] += float(g); m[1] += float(di); m[2] += float(ns)
    cum = collections.Counter(); cum_sku = collections.Counter()
    for sku, n in ql[Q['cum']]:
        if skip(sku) or code_of(sku) not in codes: continue
        cum[code_of(sku)] += int(n); cum_sku[sku] += int(n); sku_seen.add(sku)
    stock = collections.Counter(); stock_sku = collections.Counter(); inv_skus = set()
    locs = set(CFG['stores']) | {CFG['warehouse']}
    for sku, loc, u in ql[Q['inv']]:
        if skip(sku) or code_of(sku) not in codes: continue
        inv_skus.add(sku)
        if loc in locs:
            stock[code_of(sku)] += int(u); stock_sku[sku] += int(u)
    missing_inv = sorted(s for s in sku_seen if s not in inv_skus and cum_sku.get(s, 0) > 0)
    # manual inventory overrides (state/inv_override.json: {sku: units}) for SKUs the location report omits
    ov_path = os.path.join(ROOT, 'state', 'inv_override.json')
    ov = json.load(open(ov_path)) if os.path.exists(ov_path) else {}
    for sku, u in ov.get(cut.isoformat(), {}).items():
        stock[code_of(sku)] += int(u) - stock_sku.get(sku, 0); stock_sku[sku] = int(u); inv_skus.add(sku)
    # 自動補:用最近一次 get-inventory-levels 的 onHand(只算 5 倉)
    lv = {}
    for d in invlevels:
        for it in d['inventoryItems']:
            lv.setdefault(it['sku'], {})[it['locationName']] = int(it.get('onHand', it.get('available', 0)) or 0)
    for sku in list(missing_inv):
        if sku in lv and sku not in ov.get(cut.isoformat(), {}):
            u = sum(n for loc, n in lv[sku].items() if loc in locs)
            stock[code_of(sku)] += u - stock_sku.get(sku, 0); stock_sku[sku] = u; inv_skus.add(sku)
            print(f'庫存補值(get-inventory-levels):{sku} = {u}')
    missing_inv = [s for s in missing_inv if s not in inv_skus]

    def instock(rows, extra=()):
        per_sku = {}
        for sku, a, o in rows:
            if skip(sku) or code_of(sku) not in codes: continue
            a, o = int(a), int(o)
            per_sku[sku] = a / (a + o) if a + o else None
        for s in extra: per_sku.setdefault(s, 1.0)
        grp = collections.defaultdict(list)
        for s, v in per_sku.items():
            if v is None: continue
            c, col, part, _ = parse(s); grp[(c, col, part)].append(v)
        item = collections.defaultdict(list)
        for (c, _, _), v in grp.items():
            if all(x == 0 for x in v): continue  # 整色停售不計
            item[c] += v
        return per_sku, {c: sum(v) / len(v) for c, v in item.items()}
    # SKUs missing from the inventory report but with stock override are treated as in stock
    inst_sku, inst = instock(ql[Q['stock30']], extra=[s for s in inv_skus if s not in {r[0] for r in ql[Q['stock30']]} and stock_sku.get(s, 0) > 0])
    _, inst_p = instock(ql[Q['stockp30']])
    store = {r[0]: int(r[1]) for r in ql[Q['store']]}

    rows = {}
    for it in tracked():
        c = it['code']; L = D.fromisoformat(it['launch']); V = (cut - L).days + 1
        nd = max(1, min(30, V))
        series = [daily[c][x] for x in days(cut - dt.timedelta(nd - 1), cut)]
        F = sum(daily[c][x] for x in d30); G = sum(daily[c][x] for x in d7)
        mean = F / nd
        cv = statistics.stdev(series) / mean if len(series) > 1 and mean > 0 else ''
        g, di, ns = money[c]
        disc = -di / g if g else 0
        # 自身成長率
        growth = ''
        if V > 60:
            pr = sum(daily[c][x] for x in dp)
            sr = sum(store.get(x, 0) for x in d30) / max(1, sum(store.get(x, 0) for x in dp))
            ir, ip = inst.get(c, 1) or 1, inst_p.get(c, 1) or 1
            if pr > 0 and sr > 0: growth = round(((F / ir) / (pr / ip)) / sr, 2)
        else:
            alld = days(L, cut)
            first = next((x for x in alld if daily[c][x] != 0), None)
            if first:
                span = days(D.fromisoformat(first), cut); n = len(span)
                if n >= 14:
                    h = n // 2; a, b = span[:n - h], span[n - h:]
                    ia, ib = sum(daily[c][x] for x in a), sum(daily[c][x] for x in b)
                    sa, sb = sum(store.get(x, 0) for x in a) / len(a), sum(store.get(x, 0) for x in b) / len(b)
                    if ia > 0 and sa > 0: growth = round(((ib / len(b)) / (ia / len(a))) / (sb / sa), 2)
        rows[c] = dict(E=stock[c], F=F, G=G, J=round(ns), O=round(inst.get(c, 1), 3), P=round(disc, 3),
                       Q=growth, Y=round(cv, 3) if cv != '' else '', AB=cum[c], V=V)
    # tab 2
    allsk = {s for s in (set(stock_sku) | set(cum_sku)) if not skip(s) and code_of(s) in codes}
    groups = collections.defaultdict(list)
    for s in allsk:
        c, col, part, size = parse(s); groups[(c, col, part)].append((size, s))
    order_sz = ['XS', 'S', 'M', 'L', '4', 'F']
    corder = [i['code'] for i in tracked()]
    t2 = [['品號', '顏色', '部位', '尺寸', '現有庫存', '累積銷量', '售罄率', '狀態', '近30天有貨率']]
    for key in sorted(groups, key=lambda g: (corder.index(g[0]), g[1], {'上身': 0, '下身': 1}.get(g[2], 2))):
        items = sorted(groups[key], key=lambda x: (order_sz.index(x[0].split('/')[0]) if x[0].split('/')[0] in order_sz else 9, x[0]))
        st = [stock_sku.get(s, 0) for _, s in items]
        outs = [sz for sz, s in items if stock_sku.get(s, 0) <= 0]
        if all(x <= 0 for x in st): status = '該色售罄' + ('(疑似停產)' if all(cum_sku.get(s, 0) == 0 for _, s in items) else '')
        elif outs: status = '斷碼-缺' + '/'.join(outs)
        else: status = None
        for sz, s in items:
            sv, n = stock_sku.get(s, 0), cum_sku.get(s, 0); tot = sv + n; rate = n / tot if tot > 0 else 0
            stt = status or ('健康(即將斷碼-%s僅剩%d件)' % (sz, sv) if sv <= 3 and rate >= .75 else '健康(熱賣)' if rate >= .5 else '健康')
            if sv < 0: stt += '(異常:負庫存)'
            iv = inst_sku.get(s)
            t2.append([int(key[0]), key[1], key[2], "'" + sz if sz.isdigit() else sz, sv, n, round(rate, 4), stt, round(iv, 3) if iv is not None else ''])
    # tab 3: 預購訂單(最近一次 orders 查詢結果)
    pre_items = CFG.get('preorder', [])
    orders = next((g['orders']['nodes'] for g in reversed(gql) if 'orders' in g and g['orders'].get('nodes') and 'lineItems' in g['orders']['nodes'][0]), [])
    t3 = [['品項', '上市日', '預購訂單數', '取消訂單數', '取消率(訂單)', '訂購件數', '取消件數', '取消率(件數)']]
    for p in pre_items:
        o = oc = u = uc = 0
        for n in orders:
            if n['createdAt'][:10] > cut.isoformat(): continue
            q = sum(li['quantity'] for li in n['lineItems']['nodes'] if any((li['sku'] or '').startswith(x) for x in p['skus']))
            if q:
                o += 1; u += q
                if n['cancelledAt']: oc += 1; uc += q
        t3.append([p['label'], p['launch'].replace('-', '/'), o, oc, round(oc / o, 4) if o else '尚無訂單', u, uc, round(uc / u, 4) if u else '尚無訂單'])
    os.makedirs(OUT, exist_ok=True)
    order = [i['code'] for i in CFG['items']]
    sheet = []
    for c in order:
        r = rows.get(c)
        sheet.append({'code': c, **(r or {})})
    json.dump({'cutoff': cut.isoformat(), 'rows': sheet, 'tab2': t2, 'tab3': t3, 'orders_seen': len(orders), 'missing_inventory': missing_inv}, open(os.path.join(OUT, 'build.json'), 'w'), ensure_ascii=False)
    # compact payloads for the sheet (row 3 onward, in items.json order)
    blocks = {'E:G': [], 'J': [], 'O:Q': [], 'Y': [], 'AB': []}
    for r in sheet:
        if 'E' not in r:
            for k in blocks: blocks[k].append([''] * (3 if ':' in k else 1))
            continue
        blocks['E:G'].append([r['E'], r['F'], r['G']]); blocks['J'].append([r['J']])
        blocks['O:Q'].append([r['O'], r['P'], r['Q']]); blocks['Y'].append([r['Y']]); blocks['AB'].append([r['AB']])
    json.dump(blocks, open(os.path.join(OUT, 'sheet_blocks.json'), 'w'), ensure_ascii=False)
    print(json.dumps({'cutoff': cut.isoformat(), 'rows_first': 3, 'rows_last': 2 + len(sheet), 'tab2_rows': len(t2), 'missing_inventory': missing_inv}, ensure_ascii=False))
    for c in order:
        if c in rows: print(c, rows[c])


# ---------- helpers for the daily run ----------
def yesterday_tw():
    try:
        from zoneinfo import ZoneInfo
        now = dt.datetime.now(ZoneInfo(CONF.get('timezone', 'Asia/Taipei')))
    except Exception:
        now = dt.datetime.utcnow() + dt.timedelta(hours=8)
    return now.date() - dt.timedelta(1)


def orders_query(cut):
    terms = []
    for p in CFG.get('preorder', []):
        for k in p['skus']:
            terms.append(f'sku:{k}' if len(k) >= 8 else f'sku:{k}*')
    q = ('query($q:String!,$after:String){ orders(first:100, after:$after, query:$q){ pageInfo{hasNextPage endCursor} '
         'nodes{ name createdAt cancelledAt lineItems(first:30){ nodes{ sku quantity } } } } }')
    v = {'q': f"created_at:>={CONF.get('preorder_since', '2026-09-01')} created_at:<={cut.isoformat()}T23:59:59+08:00 ({' OR '.join(terms)})"}
    return q, v


def latest_main_sheet():
    head = CONF['main_headers'][:3]
    for d in reversed(sheets):
        v = d.get('values') or []
        if v and v[0][:3] == head:
            return v
    return None


def check():
    """步驟 1–2 的自動檢查:欄位標題、官網新品。回傳 0 = 可以繼續寫入。"""
    _, gql = load_results()
    ok = True
    v = latest_main_sheet()
    if v is None:
        print('✗ 找不到讀回的主表,請先 get_values ' + f"'{CONF['sheets']['main']}'!{CONF['main_read_range']}"); return 1
    if v[0] != CONF['main_headers']:
        ok = False
        exp, got = CONF['main_headers'], v[0]
        for i in range(max(len(exp), len(got))):
            a = exp[i] if i < len(exp) else '(無)'; b = got[i] if i < len(got) else '(無)'
            if a != b: print(f'✗ 欄位標題不同:第 {i + 1} 欄 應為「{a}」實際「{b}」')
    else:
        print('✓ 欄位標題正確')
    known = {i['code'] for i in CFG['items']} | {p for p in CFG['ignore_prefixes']}
    prods = next((g['products']['edges'] for g in reversed(gql) if isinstance(g.get('products'), dict) and 'edges' in g['products']), None)
    if prods is None:
        print('… 沒看到 search_products 結果,略過新品檢查')
    else:
        new = {}
        for e in prods:
            n = e.get('node', {})
            if (n.get('status') or 'ACTIVE') != 'ACTIVE': continue
            if (n.get('productType') or '') in ('Accessories',) or 'Halo' in (n.get('title') or ''): continue
            for ve in (n.get('variants') or {}).get('edges', []):
                sku = (ve.get('node') or {}).get('sku') or ''
                c = sku[:5]
                if re.fullmatch(r'\d{5}', c) and c not in known and not any(sku.startswith(p) for p in CFG['ignore_prefixes']):
                    new.setdefault(c, n.get('title'))
        if new:
            for c, t in new.items(): print(f'＋ 官網新泳裝品號:{c}({t})→ 依 SKILL 步驟 2 加進試算表與 state/items.json')
        else:
            print('✓ 官網沒有新泳裝品號')
    return 0 if ok else 1


def _contig(changed):
    """把變動列號合併成連續區段(間隔 ≤ 3 列就併在一起,減少寫入次數)。"""
    out = []
    for r in changed:
        if out and r - out[-1][1] <= 4: out[-1][1] = r
        else: out.append([r, r])
    return out


def payload(full=False):
    b = json.load(open(os.path.join(OUT, 'build.json')))
    blocks = json.load(open(os.path.join(OUT, 'sheet_blocks.json')))
    cut = D.fromisoformat(b['cutoff'])
    n = len(b['rows']); last = 2 + n
    S = CONF['sheets']
    w = []
    cols = {'E:G': ('E', 'G'), 'J': ('J', 'J'), 'O:Q': ('O', 'Q'), 'Y': ('Y', 'Y'), 'AB': ('AB', 'AB')}
    for k, (a, z) in cols.items():
        w.append({'range': f"'{S['main']}'!{a}3:{z}{last}", 'values': blocks[k]})
    w.append({'range': f"'{S['main']}'!B{last + 2}", 'values': [[f'{cut.year}/{cut.month}/{cut.day}']]})
    t3 = b['tab3']
    w.append({'range': f"'{S['preorder']}'!A2:H{len(t3)}", 'values': t3[1:]})
    t2 = b['tab2']
    base_p = os.path.join(OUT, 'tab2_written.json')
    base = None if full or not os.path.exists(base_p) else json.load(open(base_p))
    if base is None or len(base) != len(t2):
        w.append({'range': f"'{S['colorsize']}'!A2:I{len(t2)}", 'values': t2[1:]})
        if base is not None and len(base) > len(t2):
            w.append({'range': f"'{S['colorsize']}'!A{len(t2) + 1}:I{len(base)}", 'values': [[''] * 9 for _ in range(len(base) - len(t2))]})
    else:
        changed = [i + 1 for i in range(1, len(t2)) if t2[i] != base[i]]
        for a, z in _contig(changed):
            w.append({'range': f"'{S['colorsize']}'!A{a}:I{z}", 'values': t2[a - 1:z]})
    json.dump(w, open(os.path.join(OUT, 'writes.json'), 'w'), ensure_ascii=False)
    print(f"# 試算表 {CONF['spreadsheet_id']}:依序用 update_values 寫入下列 {len(w)} 段(state/out/writes.json)")
    for x in w:
        print(json.dumps(x, ensure_ascii=False, separators=(',', ':')))


def finish():
    meta = json.load(open(os.path.join(OUT, 'report_meta.json')))
    b = json.load(open(os.path.join(OUT, 'build.json')))
    sp = os.path.join(ROOT, 'state', 'state.json')
    st = json.load(open(sp)) if os.path.exists(sp) else {}
    rep = st.setdefault('reported_60d', [])
    for k in meta.get('sixty', []):
        if k not in rep: rep.append(k)
    st['last_cutoff'] = b['cutoff']
    if CONF.get('artifact_url'): st['artifact_url'] = CONF['artifact_url']
    json.dump(st, open(sp, 'w'), ensure_ascii=False, indent=2)
    json.dump(b['tab2'], open(os.path.join(OUT, 'tab2_written.json'), 'w'), ensure_ascii=False)
    asof = meta['asof']; url = CONF.get('artifact_url') or '(artifact 連結)'
    alerts = []
    if meta['restock_full']: alerts.append('整款補貨:' + '、'.join(meta['restock_full']))
    if meta['restock_cut']: alerts.append('補斷碼熱賣色:' + '、'.join(meta['restock_cut']))
    if meta['sixty']: alerts.append('滿60天:' + '、'.join(f"{k.split('@')[0]}({meta['sixty_verdicts'].get(k.split('@')[0], '')})" for k in meta['sixty']))
    print(json.dumps({'alert': bool(alerts), 'push': ('⚠ ' + '|'.join(alerts)) if alerts else None,
                      'reply': (('⚠ ' + '|'.join(alerts) + '\n') if alerts else f'{asof} 報表已更新,今天沒有補貨提醒:') + url},
                     ensure_ascii=False, indent=1))


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['today', 'queries', 'check', 'build', 'payload', 'render', 'finish'])
    ap.add_argument('--cutoff', help='資料截止日 YYYY-MM-DD,預設 = 台灣時間昨天')
    ap.add_argument('--full', action='store_true', help='payload:顏色尺寸分頁整張重寫')
    ap.add_argument('--sheet')
    a = ap.parse_args()
    cut = D.fromisoformat(a.cutoff) if a.cutoff else yesterday_tw()
    if a.cmd == 'today':
        print(cut.isoformat())
    elif a.cmd == 'queries':
        print(f'# 資料截止日 {cut}\n')
        for k, q in queries(cut).items(): print(f'[{k}]  run-analytics-query\n{q}\n')
        q, v = orders_query(cut)
        print('[orders]  graphql_query(first:100,有下一頁就帶 after 翻頁)\n' + q + '\nvariables: ' + json.dumps(v, ensure_ascii=False) + '\n')
        for p in CONF.get('inventory_patch_products', []):
            print(f"[inventory]  get-inventory-levels productId={p['product_gid']}  ({', '.join(p['skus'])})\n")
        print(f"[sheet]  get_values spreadsheetId={CONF['spreadsheet_id']} range='{CONF['sheets']['main']}'!{CONF['main_read_range']}")
        print("[products]  search_products search_query=\"status:active created_at:>'" + (cut - dt.timedelta(3)).isoformat() + "'\" sort_key=CREATED_AT reverse=true first=50")
    elif a.cmd == 'check':
        sys.exit(check())
    elif a.cmd == 'build':
        build(cut)
    elif a.cmd == 'payload':
        payload(a.full)
    elif a.cmd == 'finish':
        finish()
    else:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import render
        render.main(cut, a.sheet)
