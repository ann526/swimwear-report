"""產生每日補貨快報 HTML(由 daily.py render 呼叫)。

輸入:對話中最近一次讀回的「SKU動銷分析(新版)」A1:AB19、state/out/build.json(顏色尺寸、預購)、state/state.json(已出過 60 天報告的品項)。
輸出:state/out/report.html 與 state/out/report_meta.json(今天的提醒與 60 天品項清單)。
"""
import html, json, os, re, datetime as dt
import daily

ROOT = daily.ROOT
OUT = daily.OUT
HI, MID = 75, 50          # 補貨門檻:75 分以上整款補、50~74 只補斷碼熱賣色
WARN_DAYS_HI, WARN_DAYS_MID = 14, 7   # 距離 ROP 幾天內預警


def esc(x):
    return html.escape('' if x is None else str(x))


def num(s):
    if s is None: return None
    s = str(s).strip().replace(',', '')
    m = re.fullmatch(r'(-?\d+(?:\.\d+)?)(%|x)?', s)
    if not m: return None
    v = float(m.group(1))
    return v / 100 if m.group(2) == '%' else v


def sheet_rows():
    daily.load_results()
    for d in reversed(daily.sheets):
        v = d.get('values') or []
        if v and v[0][:3] == ['PO單號', '品項數字', '預購上架日期'] and any(r and r[0] == '資料截止日' for r in v):
            return v
    raise SystemExit('找不到讀回的試算表(SKU動銷分析 A1:AB19),請先 get_values')


def main(cut, _sheet=None):
    v = sheet_rows()
    head = v[0]; weights = v[1]
    asof = next(r[1] for r in v if r and r[0] == '資料截止日')
    if asof.replace('/', '-') != f'{cut.year}-{cut.month}-{cut.day}' and asof != f'{cut.year}/{cut.month}/{cut.day}':
        print(f'注意:試算表截止日 {asof} 與 --cutoff {cut} 不同')
    H = {h: i for i, h in enumerate(head)}
    def col(prefix):
        return next(i for h, i in H.items() if h.startswith(prefix))
    C = {k: col(p) for k, p in dict(code='品項數字', pre='預購上架日期', stock='現有庫存', u30='近30天銷量', u7='近7天銷量', avg30='近30天日均', sales='近30天銷售額',
                                     abc='ABC分級', cum_st='累積售罄率', st30='近30天售罄率', instock='近30天有貨率', disc='折扣率', growth='自身成長率', doc='庫存周轉天數',
                                     quad='動銷矩陣象限', mom='動能', action='建議動作', days='上架天數', stage='評估階段', score='綜合評估', rop='ROP', cum='累積銷售量').items()}
    build = json.load(open(os.path.join(OUT, 'build.json')))
    t2 = build['tab2']
    state = json.load(open(os.path.join(ROOT, 'state', 'state.json')))
    reported = set(state.get('reported_60d', []))
    items = []
    for r in v[2:]:
        if not r or r[0] == '資料截止日': break
        if len(r) <= C['score'] or not r[C['stock']]: continue
        g = lambda k: r[C[k]] if C[k] < len(r) else ''
        it = dict(row=r, code=g('code'), pre=g('pre'), score=num(g('score')), score_txt=g('score'), stock=num(g('stock')), rop=num(g('rop')),
                  u30=num(g('u30')), days=num(g('days')), instock=num(g('instock')) or 1, stage=g('stage'), doc=num(g('doc')))
        nd = max(1, min(30, it['days'] or 30))
        it['demand'] = (it['u30'] or 0) / nd / (it['instock'] or 1)
        it['gap_days'] = (it['stock'] - it['rop']) / it['demand'] if it['demand'] > 0 and it['rop'] is not None else None
        s = it['score']
        it['level'] = None if s is None else 'hi' if s >= HI else 'mid' if s >= MID else 'lo'
        it['below_rop'] = it['rop'] is not None and it['stock'] <= it['rop']
        items.append(it)
    def colors_of(code, hot_only=True):
        out = []
        for r in t2[1:]:
            if str(r[0]) != code: continue
            st, rate = r[7], r[6]
            if st.startswith('斷碼') or '即將斷碼' in st:
                if not hot_only or rate >= .5: out.append(r)
        return out
    restock_full = [i for i in items if i['level'] == 'hi' and i['below_rop']]
    restock_cut = [i for i in items if i['level'] == 'mid' and i['below_rop']]
    warn = [i for i in items if not i['below_rop'] and i['gap_days'] is not None and
            ((i['level'] == 'hi' and i['gap_days'] <= WARN_DAYS_HI) or (i['level'] == 'mid' and i['gap_days'] <= WARN_DAYS_MID))]
    hot_cuts = [(i, colors_of(i['code'])) for i in items if i['level'] in ('hi', 'mid')]
    hot_cuts = [(i, c) for i, c in hot_cuts if c]
    key = lambda i: f"{i['code']}@{i['pre']}"
    sixty = [i for i in items if i['days'] is not None and 60 <= i['days'] <= 120 and key(i) not in reported]
    upcoming = sorted([i for i in items if i['days'] is not None and 50 <= i['days'] < 60], key=lambda i: -i['days'])

    def verdict(i):
        s = i['score']
        if s is None: return ('觀察中', '開賣未滿 14 天,先不評分。', 'muted')
        if s >= HI: return ('整款追單', f'分數 {s:.0f} ≥ {HI}:庫存降到 ROP({i["rop"]:.0f} 件)就整款下單。' + (' 目前已低於 ROP,建議立即下單。' if i['below_rop'] else f' 目前庫存 {i["stock"]:.0f} 件,約 {i["gap_days"]:.0f} 天後到 ROP。' if i['gap_days'] else ''), 'hi')
        if s >= MID: return ('只追斷碼熱賣色', f'分數 {s:.0f} 在 {MID}–{HI-1}:到 ROP 時只補斷碼的熱賣顏色尺寸,不整款追單。', 'mid')
        extra = ' 庫存周轉超過 180 天,建議促銷或調貨出清。' if (i['doc'] or 0) > 180 else ''
        return ('不追單', f'分數 {s:.0f} < {MID}:不補貨,賣完為止。' + extra, 'lo')

    css = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'report.css')).read()
    P = []
    a = P.append
    a(f'<title>每日補貨快報</title>\n<meta name="claude:viewport-fit-sides" content="cover">\n<style>{css}</style>')
    a('<main class="wrap">')
    a(f'<header class="top"><p class="eyebrow">HAI the Label · 熱銷品分析</p><h1>每日補貨快報</h1>'
      f'<p class="sub">資料截止 <b>{esc(asof)}</b>(Shopify 官網+門市,5倉庫存)· 每天早上自動更新</p></header>')
    # ---- 提醒 ----
    n_alert = len(restock_full) + len(restock_cut)
    a('<section class="block"><h2>補貨提醒</h2>')
    if n_alert:
        a(f'<div class="banner bad"><b>⚠ 今天有 {n_alert} 個品項達到補貨標準</b>,請確認下單。</div>')
    else:
        a('<div class="banner ok">今天沒有品項達到補貨標準(分數 ≥ 50 且庫存降到 ROP)。</div>')
    def card(i, kind, body):
        return (f'<article class="alert {kind}"><div class="alert-h"><span class="code">{esc(i["code"])}</span>'
                f'<span class="score">{esc(i["score_txt"])} 分</span></div><p>{body}</p>'
                f'<p class="facts">庫存 {i["stock"]:.0f} · ROP {i["rop"]:.0f} · {esc(i["stage"])}</p></article>')
    for i in restock_full:
        a(card(i, 'bad', f'<b>立即整款補貨</b>:庫存已降到 ROP 以下。'))
    for i in restock_cut:
        cs = colors_of(i['code'])
        lst = '、'.join(f'{c[1]}{"" if c[2]=="—" else c[2]} {str(c[3]).lstrip(chr(39))}({c[7]})' for c in cs) or '目前沒有斷碼的熱賣色'
        a(card(i, 'warn', f'<b>補斷碼熱賣色</b>:{esc(lst)}'))
    if warn:
        a('<h3>即將到 ROP</h3>')
        for i in warn:
            a(card(i, 'note', f'約 <b>{i["gap_days"]:.0f} 天</b>後庫存會降到 ROP,先準備下單。'))
    if hot_cuts:
        a('<h3>分數 ≥ 50 品項的斷碼熱賣色</h3><p class="hint">整款庫存還高於 ROP,但這些顏色尺寸已經斷碼或快斷碼,而且售罄率超過五成。</p>')
        a('<div class="tw"><table><thead><tr><th>品項</th><th>分數</th><th>顏色/部位</th><th>尺寸</th><th class="n">庫存</th><th class="n">售罄率</th><th>狀態</th></tr></thead><tbody>')
        for i, cs in hot_cuts:
            for c in cs:
                a(f'<tr><td>{esc(c[0])}</td><td>{esc(i["score_txt"])}</td><td>{esc(c[1])} {esc("" if c[2]=="—" else c[2])}</td><td>{esc(str(c[3]).lstrip(chr(39)))}</td>'
                  f'<td class="n">{esc(c[4])}</td><td class="n">{c[6]*100:.0f}%</td><td>{esc(c[7])}</td></tr>')
        a('</tbody></table></div>')
    a('</section>')
    # ---- 60 天 ----
    a('<section class="block"><h2>滿 60 天評估</h2>')
    if not sixty:
        a('<p class="hint">今天沒有新滿 60 天的品項。</p>')
    for i in sixty:
        t, why, lv = verdict(i)
        a(f'<article class="sixty"><div class="sixty-h"><h3>{esc(i["code"])}</h3><span class="pill {lv}">{esc(t)}</span></div><p>{esc(why)}</p>')
        a('<dl class="kv">')
        for h, idx in H.items():
            if idx >= len(i['row']) or h in ('PO單號',): continue
            val = i['row'][idx]
            if val == '': continue
            short = re.sub(r'\(.*?\)', '', h)
            a(f'<div><dt>{esc(short)}</dt><dd>{esc(val)}</dd></div>')
        a('</dl>')
        rows = [r for r in t2[1:] if str(r[0]) == i['code']]
        if rows:
            a('<details><summary>顏色尺寸明細</summary><div class="tw"><table><thead><tr><th>顏色</th><th>部位</th><th>尺寸</th><th class="n">庫存</th><th class="n">累積銷量</th><th class="n">售罄率</th><th class="n">有貨率</th><th>狀態</th></tr></thead><tbody>')
            for r in rows:
                a(f'<tr><td>{esc(r[1])}</td><td>{esc(r[2])}</td><td>{esc(str(r[3]).lstrip(chr(39)))}</td><td class="n">{esc(r[4])}</td><td class="n">{esc(r[5])}</td>'
                  f'<td class="n">{r[6]*100:.0f}%</td><td class="n">{"" if r[8]=="" else f"{r[8]*100:.0f}%"}</td><td>{esc(r[7])}</td></tr>')
            a('</tbody></table></div></details>')
        a('</article>')
    if upcoming:
        a('<p class="hint">快滿 60 天:' + '、'.join(f'{esc(i["code"])}(第 {i["days"]:.0f} 天)' for i in upcoming) + '</p>')
    a('</section>')
    # ---- 全品項 ----
    a('<section class="block"><h2>全品項分數</h2><p class="hint">依綜合評估分數排序。距 ROP 天數 = (庫存 − ROP) ÷ 有貨調整後日均。</p>')
    a('<div class="tw"><table><thead><tr><th>品項</th><th class="n">分數</th><th>判斷</th><th>階段</th><th class="n">庫存</th><th class="n">ROP</th><th class="n">距 ROP</th><th class="n">30天銷量</th><th class="n">有貨率</th></tr></thead><tbody>')
    for i in sorted(items, key=lambda i: -(i['score'] if i['score'] is not None else -1)):
        t, _, lv = verdict(i)
        gd = '已低於' if i['below_rop'] else ('' if i['gap_days'] is None else f'{i["gap_days"]:.0f} 天')
        a(f'<tr><td>{esc(i["code"])}</td><td class="n"><b>{esc(i["score_txt"])}</b></td><td><span class="pill {lv}">{esc(t)}</span></td><td>{esc(i["stage"])}</td>'
          f'<td class="n">{i["stock"]:.0f}</td><td class="n">{"" if i["rop"] is None else f"{i["rop"]:.0f}"}</td><td class="n">{gd}</td><td class="n">{i["u30"]:.0f}</td><td class="n">{i["instock"]*100:.0f}%</td></tr>')
    a('</tbody></table></div></section>')
    # ---- 預購 ----
    t3 = build.get('tab3') or []
    if len(t3) > 1:
        a('<section class="block"><h2>預購訂單</h2><div class="tw"><table><thead><tr><th>品項</th><th class="n">訂單</th><th class="n">取消</th><th class="n">取消率</th><th class="n">件數</th></tr></thead><tbody>')
        for r in t3[1:]:
            cr = r[4] if isinstance(r[4], str) else f'{r[4]*100:.0f}%'
            a(f'<tr><td>{esc(r[0])}</td><td class="n">{r[2]}</td><td class="n">{r[3]}</td><td class="n">{esc(cr)}</td><td class="n">{r[5]}</td></tr>')
        a('</tbody></table></div></section>')
    a('<footer class="foot">門檻:分數 ≥ 75 到 ROP 整款補貨;50–74 到 ROP 只補斷碼熱賣色;< 50 不補。分數、ROP 取自 Google 試算表「熱門商品分析」。</footer></main>')
    os.makedirs(OUT, exist_ok=True)
    open(os.path.join(OUT, 'report.html'), 'w').write('\n'.join(P))
    meta = dict(asof=asof, restock_full=[i['code'] for i in restock_full], restock_cut=[i['code'] for i in restock_cut],
                warn=[(i['code'], round(i['gap_days'])) for i in warn], sixty=[key(i) for i in sixty],
                sixty_verdicts={i['code']: verdict(i)[0] for i in sixty}, hot_cut_items=[i['code'] for i, _ in hot_cuts])
    json.dump(meta, open(os.path.join(OUT, 'report_meta.json'), 'w'), ensure_ascii=False)
    print(json.dumps(meta, ensure_ascii=False))
