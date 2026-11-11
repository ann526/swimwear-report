#!/usr/bin/env python3
"""每日熱銷品報表更新工具。

用法(cutoff = 資料截止日,通常是台灣時間昨天):
  python3 tools/daily.py queries --cutoff 2026-11-10   # 印出要跑的 Shopify 查詢
  python3 tools/daily.py build   --cutoff 2026-11-10   # 從對話紀錄抓查詢結果,算出要寫進試算表的值
  python3 tools/daily.py render  --cutoff 2026-11-10 --sheet <sheet.json>   # 用試算表讀回的值產生每日報表 HTML

查詢結果的來源:Claude Code 對話紀錄(~/.claude/projects/*/*.jsonl)與大型結果檔(tool-results/*.txt)。
"""
import argparse, collections, datetime as dt, glob, html, json, math, os, re, statistics, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = json.load(open(os.path.join(ROOT, 'state', 'items.json')))
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
        if isinstance(d, dict) and 'query' in d and 'rows' in d:
            ql[d['query']] = d['rows']
        elif isinstance(d, dict) and isinstance(d.get('data'), dict):
            gql.append(d['data'])
        elif isinstance(d, dict) and isinstance(d.get('content'), dict) and 'values' in d['content']:
            sheets.append(d['content'])
        elif isinstance(d, dict) and 'range' in d and 'values' in d:
            sheets.append(d)
    return ql, gql


sheets = []


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


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['queries', 'build', 'render'])
    ap.add_argument('--cutoff', required=True)
    ap.add_argument('--sheet')
    a = ap.parse_args()
    cut = D.fromisoformat(a.cutoff)
    if a.cmd == 'queries':
        for k, q in queries(cut).items(): print(f'[{k}]\n{q}\n')
    elif a.cmd == 'build':
        build(cut)
    else:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import render
        render.main(cut, a.sheet)
