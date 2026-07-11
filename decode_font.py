# -*- coding: utf-8 -*-
"""破解混淆字体映射（改进版）：
- 候选仅限 CJK 统一表意文字（排除假名/符号）
- 同时用 微软正黑(繁) + 微软雅黑(简) 两种参考字体，取相似度最高者
- 裁剪归一化后余弦相似度匹配
"""
import io, json, numpy as np
from fontTools.ttLib import TTFont
from PIL import Image, ImageFont, ImageDraw

OBF_TTF="f24092.ttf"
REF_FONTS=[r"C:\Windows\Fonts\msjh.ttc", r"C:\Windows\Fonts\msyh.ttc"]
SIZE=64

def render_crop(font, ch, size=SIZE):
    big=96
    img=Image.new("L",(big,big),0); d=ImageDraw.Draw(img)
    d.text((big//2,big//2),ch,fill=255,font=font,anchor="mm")
    a=np.asarray(img); ys,xs=np.where(a>40)
    if len(xs)==0: return None
    crop=img.crop((xs.min(),ys.min(),xs.max()+1,ys.max()+1)).resize((size,size))
    return np.asarray(crop,dtype=np.float32).ravel()

obf_font=ImageFont.truetype(OBF_TTF,72)
tt=TTFont(OBF_TTF); cps=sorted(tt.getBestCmap().keys())

# 候选：GB2312 + Big5，过滤到 CJK 统一表意文字区
cand=set()
for i in range(0xB0,0xF8):
    for j in range(0xA1,0xFF):
        try: cand.add(bytes([i,j]).decode("gb2312"))
        except: pass
for hi in range(0xA4,0xC7):
    for lo in list(range(0x40,0x7F))+list(range(0xA1,0xFF)):
        try: cand.add(bytes([hi,lo]).decode("big5"))
        except: pass
cand=[c for c in cand if len(c)==1 and 0x4E00<=ord(c)<=0x9FFF]
print("CJK candidates:",len(cand))

# 预渲染候选（每种字体一份矩阵）
mats=[]
for fp in REF_FONTS:
    f=ImageFont.truetype(fp,72)
    vs=[]; ok=[]
    for ch in cand:
        v=render_crop(f,ch)
        if v is None: continue
        vs.append(v); ok.append(ch)
    M=np.stack(vs); M=M/(np.linalg.norm(M,axis=1,keepdims=True)+1e-8)
    mats.append((M,ok))

mapping={}; info={}
for cp in cps:
    v=render_crop(obf_font,chr(cp))
    if v is None: continue
    vn=v/(np.linalg.norm(v)+1e-8)
    best=(-1,None)
    for M,ok in mats:
        sims=M@vn; k=int(np.argmax(sims)); s=float(sims[k])
        if s>best[0]: best=(s,ok[k])
    mapping[chr(cp)]=best[1]; info[chr(cp)]=round(best[0],3)

# 保存基于字形相似度的映射，随后由 calibrate.py 用 golden 精确校准覆盖
json.dump(mapping,io.open("font_decode_map.json","w",encoding="utf-8"),ensure_ascii=False)

truth={"\uB18C":"和","\uB562":"同","\uB186":"是","\uB204":"到"}
print("verify:", {t:mapping[k] for k,t in truth.items()})
print("mapping size:",len(mapping))
