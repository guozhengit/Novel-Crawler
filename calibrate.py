# -*- coding: utf-8 -*-
"""用 golden_187（浏览器渲染的正确文本）校正字体映射表。
原始HTML正文与正确文本逐字对齐：凡原始字符属于混淆码位者，
正确文本同位置字符即为真值。"""
import io, json, requests, html as ihtml
from bs4 import BeautifulSoup
from fontTools.ttLib import TTFont

HEADERS={"User-Agent":"Mozilla/5.0","Referer":"https://www.twbook.cc/100786922/"}
raw_html=requests.get("https://www.twbook.cc/100786922/187.html",headers=HEADERS,timeout=20).content.decode("utf-8")

soup=BeautifulSoup(raw_html,"html.parser")
content=soup.select_one("div.content")
for t in content.select(".adBlock,script,style,ins"): t.decompose()
raw_paras=[]
for p in content.find_all("p"):
    txt=ihtml.unescape(p.get_text()).strip()
    if not txt: continue
    if "網站即將改版" in txt: continue
    raw_paras.append(txt)
raw_text="\n".join(raw_paras)

golden=io.open("golden_187.txt",encoding="utf-8").read().strip()
golden_paras=[l for l in golden.splitlines() if l.strip()]

# 混淆码位集合
tt=TTFont("f24092.ttf")
obf=set(tt.getBestCmap().keys())

print("raw paras=%d golden paras=%d"%(len(raw_paras),len(golden_paras)))

# 逐段对齐，仅处理长度相同的段落
mapping={}
used=0; skipped=0
n=min(len(raw_paras),len(golden_paras))
for rp,gp in zip(raw_paras,golden_paras):
    if len(rp)!=len(gp):
        skipped+=1
        continue
    used+=1
    for rc,gc in zip(rp,gp):
        if ord(rc) in obf:
            if rc in mapping and mapping[rc]!=gc:
                print("CONFLICT",hex(ord(rc)),mapping[rc],gc)
            mapping[rc]=gc
print("paras used=%d skipped(len diff)=%d, obf chars mapped=%d"%(used,skipped,len(mapping)))

# 与旧映射合并（旧的作为其余未覆盖码位的近似值）
old=json.load(io.open("font_decode_map.json",encoding="utf-8"))
merged=dict(old); merged.update(mapping)
json.dump(merged,io.open("font_decode_map.json","w",encoding="utf-8"),ensure_ascii=False)
print("corrected chars from 187:",len(mapping),"total map:",len(merged))
# 显示本次校正覆盖了哪些、修正了哪些
fixed=[(k,old.get(k),mapping[k]) for k in mapping if old.get(k)!=mapping[k]]
for k,o,n in fixed:
    print("FIX U+%04X: %s -> %s"%(ord(k),o,n))
