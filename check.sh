#!/bin/bash
# 改完程式後跑這個。用 macOS 內建的 JavaScriptCore 檢查 JS，Python 用 ast。
cd "$(dirname "$0")"
ok=0
echo "=== Python ==="
for f in webui/*.py gui.py rvc-client/*.py; do
  if .venv/bin/python -c "import ast,sys; ast.parse(open('$f').read())" 2>/dev/null; then
    echo "  ✓ $f"
  else
    echo "  ✗ $f"; .venv/bin/python -c "import ast; ast.parse(open('$f').read())" 2>&1 | tail -2; ok=1
  fi
done
echo "=== JavaScript ==="
for f in webui/static/*.js; do
  msg=$(osascript -l JavaScript -e "
    var src = \$.NSString.stringWithContentsOfFileEncodingError('$PWD/$f', 4, \$()).js;
    try { new Function(src); 'ok' } catch(e) { 'ERR ' + e.message }" 2>&1 | tail -1)
  if [ "$msg" = "ok" ]; then echo "  ✓ $f"; else echo "  ✗ $f  $msg"; ok=1; fi
done
echo "=== UDP 協定兩份定義是否一致 ==="
.venv/bin/python - <<'PY'
import re
a=open('webui/udp_audio.py').read(); b=open('rvc-client/udp_protocol.py').read()
keys=['HEADER = struct.Struct','MAGIC_IN','MAGIC_OUT','VERSION =','FLAG_INT16',
      'FLAG_PARITY','CHUNK_SAMPLES =']
bad=[]
for k in keys:
    ra=[l.strip() for l in a.splitlines() if l.startswith(k)]
    rb=[l.strip() for l in b.splitlines() if l.startswith(k)]
    if ra!=rb: bad.append(f"{k}: server={ra} client={rb}")
print("  ✓ 一致" if not bad else "  ✗ 不一致\n    "+"\n    ".join(bad))
PY

echo "=== i18n 鍵與 DOM id ==="
.venv/bin/python - <<'PY'
import re
html=open('webui/static/index.html').read(); js=open('webui/static/app.js').read(); i=open('webui/static/i18n.js').read()
need=(set(re.findall(r'data-i18n="([^"]+)"',html))|set(re.findall(r'\bT\.([a-zA-Z]\w*)',js)))-{'err'}
bad=False
for L in ['zh','en','ja']:
    m=re.search(rf"^  {L}: \{{$", i, re.M); st=m.end(); d=1; k=st
    while d>0: d += (i[k]=='{') - (i[k]=='}'); k+=1
    miss=need-set(re.findall(r'^    (\w+):', i[st:k], re.M))
    print(f"  {'✓' if not miss else '✗'} {L}" + (f"  缺 {sorted(miss)}" if miss else ""))
    bad |= bool(miss)
ids=set(re.findall(r"\$\('([^']+)'\)",js))-set(re.findall(r'id="([^"]+)"',html))
print(f"  {'✓' if not ids else '✗'} DOM id" + (f"  缺 {sorted(ids)}" if ids else ""))
PY
exit $ok
