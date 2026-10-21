#!/usr/bin/env python3
"""每日读书推荐：按确认的 v3 版式输出 ESP32 使用的 800×480/1-bit 位图。"""
from __future__ import annotations
import argparse, json, os, tempfile
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont
W,H=800,480
CJK='/System/Library/Fonts/Hiragino Sans GB.ttc'; MONO='/System/Library/Fonts/Menlo.ttc'
def font(size,bold=False,mono=False):
    path=MONO if mono else CJK; idx=1 if mono and bold else (2 if bold else 0)
    return ImageFont.truetype(path,size,index=idx)
def text(draw,xy,s,size,bold=False,mono=False,anchor='la'):
    draw.text(xy,s,font=font(size,bold,mono),fill=0,anchor=anchor)
def width(draw,s,size,bold=False,mono=False): return draw.textlength(s,font=font(size,bold,mono))
def wrap(draw,s,max_width,size,bold=False):
    out=[]; line=''; f=font(size,bold)
    for ch in s:
        trial=line+ch
        if line and draw.textlength(trial,font=f)>max_width:
            # 中文标点不单独换行；必要时允许它跟在上一行末尾。
            if ch in '，。！？；：、': line=trial
            else: out.append(line); line=ch
        else: line=trial
    if line: out.append(line)
    return out
def fit_title(draw,title,author,max_width):
    # 作者保持在书名右侧；长书名自动缩小，但不低于 24px。
    aw=width(draw,author,19,True)+18
    for size in range(39,23,-1):
        if width(draw,title,size,True)+aw <= max_width: return size
    return 24
def pack(im):
    pix=im.load(); raw=bytearray(W*H//8)
    for y in range(H):
      for x in range(W):
        if pix[x,y] != 0: raw[y*(W//8)+(x>>3)] |= 0x80>>(x&7)
    return bytes(raw)
def main():
 ap=argparse.ArgumentParser(); ap.add_argument('--runtime',default='.'); ap.add_argument('--date'); a=ap.parse_args()
 root=os.path.abspath(a.runtime); books=json.load(open(os.path.join(root,'reading_library.json'),encoding='utf-8'))
 now=datetime.strptime(a.date,'%Y-%m-%d') if a.date else datetime.now(); book=books[now.toordinal()%len(books)]
 im=Image.new('1',(W,H),1); d=ImageDraw.Draw(im)
 # v3 fixed grid: 44px outside margin, 238px quote column, 46px central gap.
 text(d,(44,34),'READING PICK',18,True,True); text(d,(756,34),'每日推荐',18,True,anchor='ra')
 d.rectangle((44,62,756,64),fill=0)
 # Left: one quote only.
 d.rectangle((44,94,50,432),fill=0); text(d,(72,123),'“',68,True)
 quote=book['quote']; qsize=26
 while qsize>18 and len(wrap(d,quote,205,qsize,True))>4: qsize-=1
 y=185
 for line in wrap(d,quote,205,qsize,True)[:4]: text(d,(72,y),line,qsize,True); y+=int(qsize*1.62)
 text(d,(72,410),'金句',18,True)
 # Right: title+author, then two distinct reading layers.
 x=328; maxw=428; title=book['title']; author=book['author']; ts=fit_title(d,title,author,maxw)
 title_y=120; text(d,(x,title_y),title,ts,True)
 text(d,(x+width(d,title,ts,True)+18,title_y+max(0,(ts-19)//2)),author,19,True)
 divider_y=120+ts+31; d.rectangle((x,divider_y,756,divider_y+2),fill=0)
 text(d,(x,divider_y+27),'本书在谈什么',18,True)
 y=divider_y+55
 for line in wrap(d,book.get('overview') or book['reason'],428,20)[:2]: text(d,(x,y),line,20); y+=32
 core_y=y+17
 text(d,(x,core_y),'核心命题',18,True)
 y=core_y+28
 for line in wrap(d,book.get('core_claim') or book['reason'],428,20)[:2]: text(d,(x,y),line,20); y+=32
 out=os.path.join(root,'out'); os.makedirs(out,exist_ok=True); raw=pack(im)
 fd,tmp=tempfile.mkstemp(dir=out); os.close(fd)
 try:
  with open(tmp,'wb') as f: f.write(raw)
  os.replace(tmp,os.path.join(out,'reading.bin'))
 finally:
  if os.path.exists(tmp): os.unlink(tmp)
 im.save(os.path.join(out,'reading.png'))
 fd,tmp=tempfile.mkstemp(dir=out); os.close(fd)
 try:
  with open(tmp,'w',encoding='utf-8') as f: json.dump({'date':now.strftime('%Y-%m-%d'),'book':book['title'],'bin_bytes':len(raw),'layout':'v3'},f,ensure_ascii=False,indent=2)
  os.replace(tmp,os.path.join(out,'reading_meta.json'))
 finally:
  if os.path.exists(tmp): os.unlink(tmp)
 print('OK reading v3',book['title'],len(raw))
if __name__=='__main__': main()
