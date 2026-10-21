#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
环节 2/3 —— 位图渲染（独立运行，不依赖采集环节）

职责单一：读 latest.json + history.sqlite -> 画 800x480 纯黑白位图 -> 写 out/screen.bin

契约（与上游唯一的耦合点）：
  输入  <runtime>/latest.json      由 fetch_quotes.py 产出
        <runtime>/history.sqlite   分时采样（缺失则画平线，不报错）
        <config>/layout.json       可调参数（字号/行高/字节序…），缺失项用内置默认值
  输出  <runtime>/out/screen.bin   1bpp 裸位图，固定文件名，每轮覆盖上一张
        <runtime>/out/screen.png   同内容 PNG，供人眼预览
        <runtime>/out/meta.json    生成时间/尺寸/字节序，供 HTTP ETag 与 ESP32 校验

设计要点（针对 Waveshare ESP32-S3-ePaper-3.97，800x480，86.40x51.84mm ≈ 235DPI）：
  * 2x 超采样后 BOX 降采样 + 阈值二值化 —— 14px 中文在 1bpp 下才不发虚
  * 最小字号 14px（物理 1.51mm），现价 30px（3.24mm）
  * 只用纯黑白：无灰阶、无 1px 线、无大面积 45° 斜线
  * 涨跌双编码：实心▲+黑底白字 / 空心▽+白底黑框（无颜色也能一眼区分）
  * 卡片位置永久固定（严格按 watchlist 顺序），绝不按涨跌幅重排 —— 重排等于全刷，残影不可接受

用法：
  python3 render_screen.py [--config DIR] [--runtime DIR] [--clock HH:MM]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, time as dtime

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.stderr.write("ERR 缺少 Pillow。安装：pip install pillow\n")
    sys.exit(4)

DEFAULT_CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config")
DEFAULT_RUNTIME = os.path.expanduser("~/WorkBuddy/eink-dashboard-data")

# 中文字体候选（按优先级；本机 PingFang.ttc 实测缺失，故 Hiragino/STHeiti 优先）
CJK_CANDIDATES = [
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", 2),   # W6 中黑
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", 0),   # W3 常规
    ("/System/Library/Fonts/STHeiti Medium.ttc", 1),     # Heiti SC Medium
    ("/System/Library/Fonts/STHeiti Medium.ttc", 0),
    ("/System/Library/Fonts/PingFang.ttc", 0),
    ("/System/Library/Fonts/Supplemental/Songti.ttc", 1),
    ("/Library/Fonts/Arial Unicode.ttf", 0),
    ("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", 0),       # Linux 兜底
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 0),
]
MONO_CANDIDATES = [
    ("/System/Library/Fonts/Menlo.ttc", 0),
    ("/System/Library/Fonts/SFNSMono.ttf", 0),
    ("/System/Library/Fonts/Supplemental/Menlo.ttc", 0),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 0),
]

# ---------------------------------------------------------------- 默认布局（可被 layout.json 部分覆盖）

DEFAULT_LAYOUT = {
    "width": 800, "height": 480,
    "render_scale": 2, "threshold": 128,

    "header_height": 56,
    "grid_cols": 4, "grid_rows": 2,
    "card_pad_x": 10, "card_pad_y": 8,
    "divider": 2,

    "row_name": 30, "row_price": 38, "row_code": 18,
    "row_chart": 90, "row_footer": 20,

    "fs_title": 20, "fs_bench_name": 16, "fs_bench_price": 28,
    "fs_badge": 16, "fs_state": 16, "fs_clock": 26, "fs_clock_label": 14,
    "fs_header_note": 14, "fs_stock_name": 20, "fs_price": 30,
    "fs_code": 14, "fs_footer": 14, "fs_currency": 14,

    "chart_line_width": 2.5, "chart_dash": [4, 4],
    "chart_end_dot": 4, "chart_show_lunch_marker": True,

    "bit_order": "msb", "black_is_one": False, "orientation": "landscape",

    "output_bin": "screen.bin", "output_png": "screen.png", "output_meta": "meta.json",
    "preview_scale": 2, "output_png_2x": "screen@2x.png",
}

# ---------------------------------------------------------------- 字体与测量

_PROBE_DRAW = ImageDraw.Draw(Image.new("1", (8, 8)))


class FontCache:
    """字体加载与缓存。中文/等宽各自探测系统可用字体，失败退回 PIL 默认。"""

    def __init__(self):
        self._cjk = self._pick(CJK_CANDIDATES)
        self._mono = self._pick(MONO_CANDIDATES)
        self._cache: dict = {}

    @staticmethod
    def _pick(cands):
        for path, idx in cands:
            if os.path.isfile(path):
                try:
                    ImageFont.truetype(path, 20, index=idx)
                    return (path, idx)
                except Exception:
                    continue
        return None

    def get(self, size: int, mono: bool = False):
        size = int(size)
        key = (size, bool(mono))
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        spec = self._mono if (mono and self._mono) else self._cjk
        font = None
        if spec:
            try:
                font = ImageFont.truetype(spec[0], size, index=spec[1])
            except Exception:
                font = None
        if font is None:
            font = ImageFont.load_default()
        self._cache[key] = font
        return font

    @property
    def info(self) -> str:
        c = os.path.basename(self._cjk[0]) if self._cjk else "PIL默认(无中文!)"
        m = os.path.basename(self._mono[0]) if self._mono else c
        return "中文=%s 等宽=%s" % (c, m)


def text_w(s, font) -> int:
    """测量文字宽度（1x 逻辑像素）。始终用独立探针，兼容超采样包装层。"""
    try:
        return int(_PROBE_DRAW.textlength(str(s), font=font))
    except Exception:
        size = getattr(font, "size", 14)
        return int(sum(size if ord(ch) > 0x2E80 else size * 0.55 for ch in str(s)))


def fit_font(fonts: FontCache, s: str, max_w: int, size: int, mono: bool = False):
    """超宽时逐级降字号，保证绝不溢出卡片。"""
    while size > 9:
        f = fonts.get(size, mono)
        if text_w(s, f) <= max_w:
            return f
        size -= 1
    return fonts.get(9, mono)


# ---------------------------------------------------------------- 超采样绘制包装

class ScaledDraw:
    """
    绘图代码按 1x 逻辑坐标书写，这里统一乘 scale；文字测量仍用 1x 字体，
    因此布局计算与最终绘制坐标口径完全一致。
    """
    _fonts = FontCache()
    _big_cache: dict = {}

    def __init__(self, draw, scale: int):
        self._d = draw
        self._s = max(1, int(scale))

    def _pt(self, xy):
        return [(x * self._s, y * self._s) for x, y in xy]

    def _box(self, b):
        return [b[0] * self._s, b[1] * self._s, b[2] * self._s, b[3] * self._s]

    def _lw(self, kw):
        if "width" in kw:
            kw["width"] = max(1, int(round(kw["width"] * self._s)))
        return kw

    def _big_font(self, font):
        path = getattr(font, "path", None)
        size = getattr(font, "size", None)
        if not path or not size:
            return font
        idx = getattr(font, "index", 0)
        key = (path, idx, size * self._s)
        hit = ScaledDraw._big_cache.get(key)
        if hit is not None:
            return hit
        try:
            big = ImageFont.truetype(path, size * self._s, index=idx)
        except Exception:
            big = font
        ScaledDraw._big_cache[key] = big
        return big

    def rectangle(self, box, **kw):
        return self._d.rectangle(self._box(box), **self._lw(kw))

    def line(self, xy, **kw):
        pts = self._pt(xy) if xy and isinstance(xy[0], (list, tuple)) else xy
        return self._d.line(pts, **self._lw(kw))

    def polygon(self, xy, **kw):
        return self._d.polygon(self._pt(xy), **self._lw(kw))

    def ellipse(self, box, **kw):
        return self._d.ellipse(self._box(box), **self._lw(kw))

    def text(self, xy, s, font=None, **kw):
        return self._d.text((xy[0] * self._s, xy[1] * self._s), s,
                            font=self._big_font(font), **kw)


# ---------------------------------------------------------------- 数值格式化

def fmt_price(v) -> str:
    """按量级自适应小数位，保证不溢出卡片宽度。"""
    if v is None:
        return "--"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    a = abs(v)
    if a >= 10000:
        return "%.1f" % v
    if a >= 10:
        return "%.2f" % v
    return "%.3f" % v


def fmt_pct(v, digits: int = 2) -> str:
    if v is None:
        return "--"
    try:
        return "%.*f%%" % (digits, float(v))
    except (TypeError, ValueError):
        return "--"


def fmt_signed(v, digits: int = 2) -> str:
    if v is None:
        return "--"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "--"
    return ("+" if f > 0 else "") + "%.*f" % (digits, f)


def direction(v) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "flat"
    return "up" if f > 0.0001 else ("down" if f < -0.0001 else "flat")


# ---------------------------------------------------------------- 绘图基元（纯黑白）

def draw_triangle(d, cx, cy, r, filled, color=0):
    """实心▲ / 空心▽ —— 手绘多边形，不依赖字体是否含该字形。"""
    if filled:
        d.polygon([(cx, cy - r), (cx - r, cy + r), (cx + r, cy + r)], fill=color)
    else:
        d.polygon([(cx, cy + r), (cx - r, cy - r), (cx + r, cy - r)], outline=color, width=2)


def draw_badge(d, fonts, y, h, pct, right_x):
    """涨跌块，右对齐到 right_x。涨=黑底白字实心▲；跌=白底黑框空心▽；平=无框短横。"""
    dirn = direction(pct)
    label = fmt_pct(pct)
    font = fonts.get(max(12, int(h * 0.52)))
    tw = text_w(label, font)
    tri = max(6, int(h * 0.30))
    gap = int(h * 0.30)
    bw = tw + tri + gap * 2
    bx = right_x - bw

    if dirn == "up":
        d.rectangle([bx, y, bx + bw, y + h], fill=0)
        draw_triangle(d, bx + gap + tri // 2, y + h // 2, tri // 2, True, color=1)
        d.text((bx + gap + tri + gap // 2, y + (h - font.size) // 2 - 1), label, font=font, fill=1)
    elif dirn == "down":
        d.rectangle([bx, y, bx + bw, y + h], outline=0, width=2)
        draw_triangle(d, bx + gap + tri // 2, y + h // 2, tri // 2, False, color=0)
        d.text((bx + gap + tri + gap // 2, y + (h - font.size) // 2 - 1), label, font=font, fill=0)
    else:
        flat = "— " + label
        ff = fonts.get(max(12, int(h * 0.48)))
        d.text((right_x - text_w(flat, ff), y + (h - ff.size) // 2 - 1), flat, font=ff, fill=0)
    return bx


def dashed_hline(d, x0, x1, y, dash, w=2, fill=0):
    step = max(2, dash[0] + dash[1])
    x = x0
    while x < x1:
        d.line([(x, y), (min(x + dash[0], x1), y)], fill=fill, width=w)
        x += step


def dashed_vline(d, y0, y1, x, dash, w=2, fill=0):
    step = max(2, dash[0] + dash[1])
    y = y0
    while y < y1:
        d.line([(x, y), (x, min(y + dash[0], y1))], fill=fill, width=w)
        y += step


# ---------------------------------------------------------------- 分时图

TOTAL_MIN = 240   # 09:30-11:30 (120) + 13:00-15:00 (120)


def minute_offset(ts: str):
    """'YYYY-MM-DD HH:MM:SS' -> 0..240 分钟轴位置。午休段压到 120，盘外 clamp。"""
    try:
        t = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").time()
    except Exception:
        return None
    m = t.hour * 60 + t.minute
    s0, e0, s1, e1 = 9 * 60 + 30, 11 * 60 + 30, 13 * 60, 15 * 60
    if m <= s0:
        return 0.0
    if m <= e0:
        return float(m - s0)
    if m <= s1:
        return 120.0
    if m <= e1:
        return 120.0 + float(m - s1)
    return float(TOTAL_MIN)


def load_intraday(runtime: str, day: str, code: str, limit: int = 400):
    """读当日分时序列 -> [(minute_offset, price), ...]。库不存在时返回空，不报错。"""
    db = os.path.join(runtime, "history.sqlite")
    if not os.path.isfile(db):
        return []
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=5)
        rows = conn.execute(
            "SELECT ts, price FROM ticks WHERE day=? AND code=? AND price IS NOT NULL "
            "ORDER BY ts ASC LIMIT ?", (day, code, limit)).fetchall()
        conn.close()
    except Exception:
        return []
    out = []
    for ts, price in rows:
        off = minute_offset(ts)
        if off is not None:
            out.append((off, float(price)))
    return out


def draw_chart(d, fonts, cfg, box, series, prev_close, cur_price):
    """y 轴按 prev_close ± 当日最大偏离自适应，保证小波动也看得清。"""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    if w <= 6 or h <= 6:
        return

    anchors = [p for _, p in series]
    if prev_close:
        anchors.append(float(prev_close))
    if cur_price:
        anchors.append(float(cur_price))
    if not anchors:
        f = fonts.get(14)
        tip = "等待首次采样…"
        d.text((x0 + (w - text_w(tip, f)) // 2, y0 + (h - 14) // 2), tip, font=f, fill=0)
        return

    lo, hi = min(anchors), max(anchors)
    span = hi - lo
    if span <= 0:
        span = max(abs(hi) * 0.002, 0.001)
    pad = span * 0.18
    lo, hi, span = lo - pad, hi + pad, span + 2 * pad

    def Y(p):
        return y1 - 1 - (float(p) - lo) / span * (h - 2)

    def X(off):
        return x0 + 1 + off / TOTAL_MIN * (w - 2)

    if cfg.get("chart_show_lunch_marker", True):
        dashed_vline(d, y0 + 1, y1 - 1, int(X(120)), cfg["chart_dash"], w=2, fill=0)

    if prev_close:
        by = int(Y(float(prev_close)))
        if y0 < by < y1:
            dashed_hline(d, x0 + 1, x1 - 1, by, cfg["chart_dash"], w=2, fill=0)

    lw = max(2, int(round(cfg["chart_line_width"])))
    if len(series) >= 2:
        pts = [(X(off), Y(p)) for off, p in series]
        d.line(pts, fill=0, width=lw, joint="curve")
        ex, ey = pts[-1]
    elif len(series) == 1:
        ex, ey = X(series[0][0]), Y(series[0][1])
    elif cur_price:
        ey = Y(float(cur_price))
        d.line([(x0 + 1, ey), (x1 - 1, ey)], fill=0, width=lw)
        ex = x1 - 1
    else:
        return

    r = cfg["chart_end_dot"]
    d.ellipse([ex - r, ey - r, ex + r, ey + r], fill=0)


# ---------------------------------------------------------------- 卡片

def draw_card(d, fonts, cfg, box, item, quote, bench_pct):
    x0, y0, x1, y1 = box
    ix0, ix1 = x0 + cfg["card_pad_x"], x1 - cfg["card_pad_x"]
    inner_w = ix1 - ix0
    cy = y0 + cfg["card_pad_y"]
    q = quote or {}

    code = item.get("code", "")
    name = item.get("alias") or q.get("name") or code
    currency = item.get("currency") or ""
    price = q.get("price")
    pct = q.get("change_percent")

    # 行1：名称 + 涨跌块（涨跌放首行，扫视优先级最高）
    h = cfg["row_name"]
    badge_h = max(20, int(h * 0.78))
    nf = fit_font(fonts, name, inner_w - badge_h - 34, cfg["fs_stock_name"])
    d.text((ix0, cy + (h - nf.size) // 2 - 1), name, font=nf, fill=0)
    draw_badge(d, fonts, cy + (h - badge_h) // 2, badge_h, pct, ix1)
    cy += h

    # 行2：现价（主信息，等宽字体）
    h = cfg["row_price"]
    ptxt = fmt_price(price)
    reserve = text_w(currency, fonts.get(cfg["fs_currency"])) + 8 if currency else 0
    pf = fit_font(fonts, ptxt, inner_w - reserve, cfg["fs_price"], mono=True)
    d.text((ix0, cy + (h - pf.size) // 2 - 2), ptxt, font=pf, fill=0)
    if currency:
        cf = fonts.get(cfg["fs_currency"])
        d.text((ix1 - text_w(currency, cf), cy + h - cf.size - 2), currency, font=cf, fill=0)
    cy += h

    # 行3：代码 + 相对基准超额
    h = cfg["row_code"]
    cf = fonts.get(cfg["fs_code"])
    d.text((ix0, cy + (h - cf.size) // 2 - 1), code, font=cf, fill=0)
    if bench_pct is not None and pct is not None:
        try:
            rtxt = "相对 %s" % fmt_signed(float(pct) - float(bench_pct))
            rf = fit_font(fonts, rtxt, inner_w - text_w(code, cf) - 8, cfg["fs_code"])
            d.text((ix1 - text_w(rtxt, rf), cy + (h - rf.size) // 2 - 1), rtxt, font=rf, fill=0)
        except (TypeError, ValueError):
            pass
    cy += h

    # 行4：分时图
    h = cfg["row_chart"]
    draw_chart(d, fonts, cfg, (ix0, cy + 2, ix1, cy + h - 2),
               item.get("_series", []), q.get("prev_close"), price)
    cy += h

    # 行5：5日涨跌 + 量比
    h = cfg["row_footer"]
    ff = fonts.get(cfg["fs_footer"])
    ty = cy + (h - ff.size) // 2 - 1
    c5 = q.get("chg_5d")
    left = "5日 %s%%" % fmt_signed(c5) if c5 is not None else "5日 --"
    d.text((ix0, ty), left, font=ff, fill=0)
    vr = q.get("volume_ratio")
    if vr is not None:
        right = "量比 %.2f" % float(vr)
        d.text((ix1 - text_w(right, ff), ty), right, font=ff, fill=0)


# ---------------------------------------------------------------- 顶栏

def draw_header(d, fonts, cfg, W, payload, bench_quote, now):
    H = cfg["header_height"]
    mid = H // 2

    tf = fonts.get(cfg["fs_title"])
    title = "自选看板"
    d.text((14, mid - tf.size // 2 - 1), title, font=tf, fill=0)
    x = 14 + text_w(title, tf) + 14
    d.rectangle([x, mid - 15, x + 3, mid + 15], fill=0)
    x += 3 + 14

    bench = (payload.get("watchlist") or {}).get("benchmark") or {}
    bname = bench.get("alias") or bench.get("code") or "基准"
    bf = fonts.get(cfg["fs_bench_name"])
    d.text((x, mid - bf.size // 2), bname, font=bf, fill=0)
    x += text_w(bname, bf) + 9

    bprice = fmt_price((bench_quote or {}).get("price"))
    bpf = fonts.get(cfg["fs_bench_price"])
    d.text((x, mid - bpf.size // 2 - 2), bprice, font=bpf, fill=0)
    x += text_w(bprice, bpf) + 9

    bpct = (bench_quote or {}).get("change_percent")
    probe = Image.new("1", (8, 8))
    pd = ImageDraw.Draw(probe)
    badge_w = (text_w(fmt_pct(bpct), fonts.get(max(12, int(34 * 0.52))))
               + max(6, int(34 * 0.30)) + int(34 * 0.30) * 2)
    _ = pd
    draw_badge(d, fonts, mid - 17, 34, bpct, x + badge_w)
    x += badge_w + 12

    # 右侧区块先算宽度，再决定辅助信息能留多少（绝不挤压时钟）
    state = payload.get("market_state") or ("交易中" if payload.get("trading") else "已收盘")
    trading = bool(payload.get("trading"))
    sf = fonts.get(cfg["fs_state"])
    state_w = text_w(state, sf) + 20
    state_h = 30

    clock = now.strftime("%H:%M")
    clf = fonts.get(cfg["fs_clock"])
    lblf = fonts.get(cfg["fs_clock_label"])
    lbl = "更新于"
    clock_w = max(text_w(clock, clf), text_w(lbl, lblf))
    clock_h = clf.size + lblf.size + 2

    nf = fonts.get(cfg["fs_header_note"])
    bits = []
    bq = bench_quote or {}
    if bq.get("chg_5d") is not None:
        bits.append("5日 %s%%" % fmt_signed(bq.get("chg_5d")))
    if bq.get("chg_ytd") is not None:
        bits.append("年内 %s%%" % fmt_signed(bq.get("chg_ytd")))

    def note_w(n):
        return text_w("｜".join(n), nf) if n else 0

    for cut in (0, 1, 2):
        cand = bits[:len(bits) - cut] if cut else bits
        if x + note_w(cand) + 12 + state_w + 12 + clock_w + 14 <= W or cut == 2:
            bits = cand
            break

    nw = note_w(bits)
    clock_x = W - 14 - clock_w
    state_x = clock_x - 12 - state_w
    note_x = state_x - 12 - nw

    if bits:
        d.text((note_x, mid - nf.size // 2), "｜".join(bits), font=nf, fill=0)

    # 状态块：交易中 = 实心（数据在动）；非交易 = 空心框（数据已冻结）
    sy = mid - state_h // 2
    if trading:
        d.rectangle([state_x, sy, state_x + state_w, sy + state_h], fill=0)
        d.text((state_x + 10, sy + (state_h - sf.size) // 2 - 1), state, font=sf, fill=1)
    else:
        d.rectangle([state_x, sy, state_x + state_w, sy + state_h], outline=0, width=2)
        d.text((state_x + 10, sy + (state_h - sf.size) // 2 - 1), state, font=sf, fill=0)

    # 时钟 + "更新于"
    total = clock_h
    ty = mid - total // 2
    d.text((clock_x + (clock_w - text_w(clock, clf)) // 2, ty), clock, font=clf, fill=0)
    d.text((clock_x + (clock_w - text_w(lbl, lblf)) // 2, ty + clf.size + 1), lbl, font=lblf, fill=0)

    dv = cfg["divider"] + 1
    d.rectangle([0, H - dv, W, H], fill=0)


# ---------------------------------------------------------------- 位图打包

def pack_bitmap(im: Image.Image, cfg: dict) -> bytes:
    """1bpp 裸字节流。字节序/极性/方向全部由 layout.json 配置，不改代码即可适配固件。"""
    if cfg.get("orientation", "landscape") == "portrait":
        im = im.transpose(Image.Transpose.ROTATE_90)

    W, H = im.size
    px = im.load()
    msb = cfg.get("bit_order", "msb") == "msb"
    black_is_one = bool(cfg.get("black_is_one", False))

    row_bytes = (W + 7) // 8
    buf = bytearray(row_bytes * H)
    for y in range(H):
        base = y * row_bytes
        for x in range(W):
            is_black = (px[x, y] == 0)          # PIL '1' 模式：0=黑, 255=白
            if (1 if is_black else 0) == (1 if black_is_one else 0):
                if msb:
                    buf[base + (x >> 3)] |= 0x80 >> (x & 7)
                else:
                    buf[base + (x >> 3)] |= 0x01 << (x & 7)
    return bytes(buf)


def atomic_write(path: str, data) -> None:
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    if isinstance(data, (bytes, bytearray)):
        with open(tmp, "wb") as f:
            f.write(bytes(data))
    else:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
    os.replace(tmp, path)


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser(description="环节2：渲染水墨屏位图（独立运行）")
    ap.add_argument("--config", default=DEFAULT_CONFIG_DIR, help="配置目录（含 layout.json）")
    ap.add_argument("--runtime", default=DEFAULT_RUNTIME, help="运行时数据目录")
    ap.add_argument("--clock", default=None, help="覆盖时钟显示 HH:MM（调试用）")
    args = ap.parse_args()

    config_dir = os.path.abspath(args.config)
    runtime = os.path.abspath(os.path.expanduser(args.runtime))
    out_dir = os.path.join(runtime, "out")
    os.makedirs(out_dir, exist_ok=True)

    cfg = dict(DEFAULT_LAYOUT)
    lp = os.path.join(config_dir, "layout.json")
    if os.path.isfile(lp):
        try:
            with open(lp, encoding="utf-8") as f:
                user = json.load(f)
            cfg.update({k: v for k, v in user.items() if not k.startswith("_")})
        except Exception as e:
            sys.stderr.write("WARN layout.json 解析失败，改用内置默认布局: %s\n" % e)
    else:
        sys.stderr.write("WARN 未找到 layout.json，使用内置默认布局\n")

    lj = os.path.join(runtime, "latest.json")
    if not os.path.isfile(lj):
        sys.stderr.write("ERR 找不到 %s —— 请先运行 fetch_quotes.py\n" % lj)
        return 3
    try:
        with open(lj, encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as e:
        sys.stderr.write("ERR latest.json 解析失败: %s\n" % e)
        return 3

    quotes = payload.get("quotes") or {}
    wl = payload.get("watchlist") or {}
    items = [dict(it) for it in (wl.get("items") or []) if it.get("code")]
    if not items:
        sys.stderr.write("ERR watchlist.items 为空，无可绘制标的\n")
        return 3

    bench_code = payload.get("benchmark_code") or (wl.get("benchmark") or {}).get("code")
    bench_quote = quotes.get(bench_code) if bench_code else None
    bench_pct = (bench_quote or {}).get("change_percent")
    day = payload.get("intraday_date") or payload.get("fetched_date") or datetime.now().strftime("%Y-%m-%d")

    W, H = int(cfg["width"]), int(cfg["height"])
    scale = max(1, int(cfg.get("render_scale", 2)))

    # 预取分时序列（避免绘图函数里做 IO）
    for it in items:
        it["_series"] = load_intraday(runtime, day, it["code"])

    img = Image.new("1", (W * scale, H * scale), 1)
    d = ScaledDraw(ImageDraw.Draw(img), scale) if scale != 1 else ImageDraw.Draw(img)
    fonts = FontCache()

    now = datetime.now()
    if args.clock:
        try:
            now = now.replace(hour=int(args.clock[:2]), minute=int(args.clock[3:5]))
        except Exception:
            pass

    draw_header(d, fonts, cfg, W, payload, bench_quote, now)

    hh = int(cfg["header_height"])
    cols, rows = int(cfg["grid_cols"]), int(cfg["grid_rows"])
    slots = cols * rows
    if len(items) > slots:
        sys.stderr.write("WARN watchlist 有 %d 项，超过网格容量 %d（%dx%d），多余项不显示。"
                         "如需全部显示请调大 layout.json 的 grid_cols/grid_rows\n"
                         % (len(items), slots, cols, rows))
        items = items[:slots]

    cw = W // cols
    ch = (H - hh) // rows
    dv = int(cfg["divider"])

    # 卡片位置永久固定：严格按 watchlist.items 顺序，绝不按涨跌幅重排
    for i, item in enumerate(items):
        r, c = divmod(i, cols)
        x0, y0 = c * cw, hh + r * ch
        draw_card(d, fonts, cfg, (x0, y0, x0 + cw, y0 + ch),
                  item, quotes.get(item["code"]), bench_pct)
        if c < cols - 1:
            d.rectangle([x0 + cw - dv, y0, x0 + cw, y0 + ch], fill=0)
        if r < rows - 1:
            d.rectangle([x0, y0 + ch - dv, x0 + cw, y0 + ch], fill=0)

    for i in range(len(items), slots):
        r, c = divmod(i, cols)
        x0, y0 = c * cw, hh + r * ch
        f = fonts.get(20)
        tip = "空槽位"
        d.text((x0 + (cw - text_w(tip, f)) // 2, y0 + ch // 2 - 10), tip, font=f, fill=0)

    if scale != 1:
        # 注意：必须先转 L 再降采样。"1" 模式下 point() 的 LUT 作用于存储位值(0/1)，
        # 白像素=1 会被 LUT[1] 映射成 0 导致整屏全黑（历史 bug）。
        thr = int(cfg.get("threshold", 128))
        img = img.convert("L").resize((W, H), Image.Resampling.BOX)
        img = img.point(lambda p: 255 if p >= thr else 0, "1")

    bin_path = os.path.join(out_dir, cfg.get("output_bin", "screen.bin"))
    png_path = os.path.join(out_dir, cfg.get("output_png", "screen.png"))
    meta_path = os.path.join(out_dir, cfg.get("output_meta", "meta.json"))

    raw = pack_bitmap(img, cfg)
    atomic_write(bin_path, raw)          # 固定文件名，每轮覆盖上一张
    img.save(png_path)
    # 人眼预览专用 2x 图：最近邻放大，纯黑白硬边不插值。
    # 原因：Retina 屏看 1x 的 1bpp PNG 会被平滑插值出一圈灰，显得发虚；
    # 2x 图在 Retina 上 1 逻辑像素 = 2 设备像素，点对点显示，预览即真机观感。
    ps = max(1, int(cfg.get("preview_scale", 2)))
    if ps > 1:
        img.resize((W * ps, H * ps), Image.Resampling.NEAREST).save(
            os.path.join(out_dir, cfg.get("output_png_2x", "screen@2x.png")))
    atomic_write(meta_path, json.dumps({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "data_fetched_at": payload.get("fetched_at"),
        "market_state": payload.get("market_state"),
        "width": W, "height": H,
        "orientation": cfg.get("orientation"),
        "bit_order": cfg.get("bit_order"),
        "black_is_one": cfg.get("black_is_one"),
        "bin_bytes": len(raw),
        "bin_file": os.path.basename(bin_path),
        "items": [it.get("alias") or it.get("code") for it in items],
        "font": fonts.info,
    }, ensure_ascii=False, indent=2))

    print("OK 渲染完成 %dx%d -> %s (%d 字节) | 数据时点=%s | 状态=%s | %s" % (
        W, H, bin_path, len(raw), payload.get("fetched_at", "?"),
        payload.get("market_state", "?"), fonts.info))
    return 0


if __name__ == "__main__":
    sys.exit(main())
