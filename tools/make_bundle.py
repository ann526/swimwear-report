#!/usr/bin/env python3
"""打包可以搬到其他帳號的 skill:dist/hai-daily-restock-report/ 與 .zip(不需要 GitHub)。

  python3 tools/make_bundle.py
"""
import json, os, shutil, zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME = 'hai-daily-restock-report'
DST = os.path.join(ROOT, 'dist', NAME)
shutil.rmtree(DST, ignore_errors=True)
os.makedirs(os.path.join(DST, 'scripts')); os.makedirs(os.path.join(DST, 'defaults'))
shutil.copy(os.path.join(ROOT, 'skill', 'SKILL.md'), DST)
for f in ('daily.py', 'render.py', 'report.css'):
    shutil.copy(os.path.join(ROOT, 'tools', f), os.path.join(DST, 'scripts', f))
conf = json.load(open(os.path.join(ROOT, 'state', 'config.json')))
conf['artifact_url'] = ''          # 新帳號第一次執行時建立自己的 artifact
json.dump(conf, open(os.path.join(DST, 'defaults', 'config.json'), 'w'), ensure_ascii=False, indent=1)
shutil.copy(os.path.join(ROOT, 'state', 'items.json'), os.path.join(DST, 'defaults', 'items.json'))
st = json.load(open(os.path.join(ROOT, 'state', 'state.json')))
st = {k: st[k] for k in ('reported_60d', 'last_cutoff') if k in st}
json.dump(st, open(os.path.join(DST, 'defaults', 'state.json'), 'w'), ensure_ascii=False, indent=2)
zp = os.path.join(ROOT, 'dist', NAME + '.zip')
with zipfile.ZipFile(zp, 'w', zipfile.ZIP_DEFLATED) as z:
    for base, _, files in os.walk(DST):
        for f in files:
            full = os.path.join(base, f)
            z.write(full, os.path.join(NAME, os.path.relpath(full, DST)))
print(zp)
