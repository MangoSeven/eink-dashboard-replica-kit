#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
eink-home-dashboard · 环节 2/3 —— 主页位图渲染（独立运行，不依赖采集环节）

职责单一：读 data/*.json + 本地时间 -> 画 800x480 纯黑白主页位图 -> 写 out/home.bin

契约（与上游唯一的耦合点是文件，进程间零依赖）：
  输入  <runtime>/data/sensor.json     温湿度+WiFi+同步状态（ESP32 经 POST /sensor 落地）
        <runtime>/data/codex.json      Codex 官方额度剩余%（fetch_codex.py 产出）
        <runtime>/data/deepseek.json   DeepSeek 当前 API 余额（fetch_deepseek_balance.py 产出）
        <runtime>/data/fortune.json    每日运势（模型每天生成一次写入）
        <config>/home_config.json      刷新周期/图片路径/运势兜底文案等
  输出  <runtime>/out/home.bin         1bpp 裸位图 48000 字节，固定名，每轮覆盖
        <runtime>/out/home.png         同内容 1x PNG
        <runtime>/out/home@2x.png      2x NEAREST 预览图（Retina 点对点，不发虚）
        <runtime>/out/home_meta.json   生成时间/尺寸/字节序，供 ETag 与 ESP32 校验

设计基准：design-home-page-v6.html（1:1 800x480，坐标逐行对齐，防遮挡）
工程铁律：1bpp 纯黑白 / 2x 超采样 BOX 降采样阈值二值化 / 最小线宽 2px /
          无透明度（次要信息用小字号，不用 opacity）/ 双编码状态（实心=正常 空心=异常）

用法：
  python3 render_home.py [--config DIR] [--runtime DIR] [--clock HH:MM] [--date YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data_status import now as china_now, source_status, load as status_load, finite_number


try:
    from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageEnhance
except ImportError:
    sys.stderr.write("ERR 缺少 Pillow。安装：pip install pillow\n")
    sys.exit(4)

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG_DIR = os.path.join(SKILL_DIR, "config")
DEFAULT_RUNTIME = os.path.expanduser("~/WorkBuddy/eink-home-data")

# ---------------------------------------------------------------- 字体
# 探测结论（本机实测）：Menlo.ttc idx0=Regular idx1=Bold；
# Hiragino Sans GB.ttc idx0=W3(常规) idx2=W6(中黑/粗)；Menlo 覆盖 ¥ ° · 等 Latin-1 符号。
FONT_SPECS = {
    "mono":  [("/System/Library/Fonts/Menlo.ttc", 0),
              ("/System/Library/Fonts/SFNSMono.ttf", 0),
              ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 0)],
    "monob": [("/System/Library/Fonts/Menlo.ttc", 1),
              ("/System/Library/Fonts/SFNSMono.ttf", 0),
              ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", 0)],
    "cjk":   [("/System/Library/Fonts/Hiragino Sans GB.ttc", 0),
              ("/System/Library/Fonts/STHeiti Medium.ttc", 1),
              ("/System/Library/Fonts/PingFang.ttc", 0),
              ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 0)],
    "cjkb":  [("/System/Library/Fonts/Hiragino Sans GB.ttc", 2),
              ("/System/Library/Fonts/STHeiti Medium.ttc", 1),
              ("/System/Library/Fonts/PingFang.ttc", 0),
              ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 0)],
}


class Fonts:
    def __init__(self, scale: int):
        self.scale = max(1, int(scale))
        self._picked = {k: self._pick(v) for k, v in FONT_SPECS.items()}
        self._cache: dict = {}
        self._probe: dict = {}

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

    def _get(self, kind: str, size: int, scaled: bool):
        px = int(size) * (self.scale if scaled else 1)
        key = (kind, px)
        if key in self._cache:
            return self._cache[key]
        spec = self._picked.get(kind)
        font = None
        if spec:
            try:
                font = ImageFont.truetype(spec[0], px, index=spec[1])
            except Exception:
                font = None
        if font is None:
            font = ImageFont.load_default()
        self._cache[key] = font
        return font

    def draw_font(self, kind, size):
        """按 scale 放大后的字体，用于实际绘制。"""
        return self._get(kind, size, True)

    def width(self, kind, size, s) -> int:
        """1x 逻辑像素宽度测量（用未放大的探针字体）。"""
        f = self._get(kind, size, False)
        try:
            return int(f.getlength(str(s)))
        except Exception:
            return int(sum(size if ord(c) > 0x2E80 else size * 0.55 for c in str(s)))

    @property
    def info(self):
        def nm(k):
            spec = self._picked.get(k)
            return os.path.basename(spec[0]) if spec else "PIL默认(无中文!)"
        return "中文=%s 粗=%s 等宽=%s" % (nm("cjk"), nm("cjkb"), nm("mono"))


# ---------------------------------------------------------------- 绘图包装（1x 逻辑坐标 -> scale 物理像素）

class D:
    """所有坐标按 1x 逻辑像素书写，内部统一乘 scale。文字用 baseline 锚点对齐 SVG。"""

    def __init__(self, img: Image.Image, fonts: Fonts):
        self.d = ImageDraw.Draw(img)
        self.s = fonts.scale
        self.f = fonts

    def _b(self, box):
        return [box[0] * self.s, box[1] * self.s, box[2] * self.s, box[3] * self.s]

    def _w(self, width):
        return max(1, int(round(width * self.s)))

    def rect(self, box, fill=None, outline=None, width=1):
        kw = {}
        if fill is not None:
            kw["fill"] = fill
        if outline is not None:
            kw["outline"] = outline
            kw["width"] = self._w(width)
        self.d.rectangle(self._b(box), **kw)

    def line(self, xy, fill=0, width=2):
        pts = [(x * self.s, y * self.s) for x, y in xy]
        self.d.line(pts, fill=fill, width=self._w(width))

    def circle(self, cx, cy, r, fill=None, outline=None, width=2):
        box = [(cx - r) * self.s, (cy - r) * self.s, (cx + r) * self.s, (cy + r) * self.s]
        kw = {}
        if fill is not None:
            kw["fill"] = fill
        if outline is not None:
            kw["outline"] = outline
            kw["width"] = self._w(width)
        self.d.ellipse(box, **kw)

    def arc(self, cx, cy, r, start, end, fill=0, width=2):
        box = [(cx - r) * self.s, (cy - r) * self.s, (cx + r) * self.s, (cy + r) * self.s]
        self.d.arc(box, start, end, fill=fill, width=self._w(width))

    def text(self, x, y, s, size, kind="cjk", fill=0, anchor="ls", ls=0):
        """x,y 为 baseline 起点（anchor 末位 s=baseline）。ls=字距(逻辑px)。"""
        font = self.f.draw_font(kind, size)
        if ls:
            # 逐字绘制以模拟 SVG letter-spacing
            cx = x
            for ch in str(s):
                self.d.text((cx * self.s, y * self.s), ch, font=font, fill=fill, anchor="ls")
                cx += self.f.width(kind, size, ch) + ls
        else:
            self.d.text((x * self.s, y * self.s), str(s), font=font, fill=fill, anchor=anchor)

    def fit_size(self, s, size, kind, max_w, floor=9):
        """超宽逐级降字号，绝不溢出。返回可用字号。"""
        while size > floor and self.f.width(kind, size, s) > max_w:
            size -= 1
        return size


# ---------------------------------------------------------------- 数据加载（缺文件/损坏都降级，不报错）

def load_json(path, default=None):
    if not os.path.isfile(path):
        return default
    try:
        with open(path, encoding="utf-8") as fp:
            return json.load(fp)
    except Exception:
        return default


def is_fresh(ts, now, stale_min):
    """数据时间戳是否在 stale_min 分钟内。无法解析视为不新鲜。"""
    if not ts:
        return False
    try:
        t = datetime.fromisoformat(str(ts))
        return (now - t).total_seconds() <= stale_min * 60
    except Exception:
        return False


# ---------------------------------------------------------------- 图像区（任意图片 -> 272x272 抖动二值）

def prepare_art(path, size, dither=True, contrast=1.3):
    """把用户提供的任意图片裁成 size×size 纯黑白抖动图，直接可贴。缺图返回 None。"""
    if not path or not os.path.isfile(path):
        return None
    try:
        im = Image.open(path).convert("L")
        im = ImageOps.fit(im, (size, size), Image.Resampling.LANCZOS)
        if contrast and contrast != 1.0:
            im = ImageEnhance.Contrast(im).enhance(contrast)
        if dither:
            im = im.convert("1")  # 默认 Floyd-Steinberg
        else:
            im = im.point(lambda p: 0 if p < 128 else 255, "1")
        return im
    except Exception as e:
        sys.stderr.write("WARN 图像处理失败(%s): %s\n" % (path, e))
        return None


# ---------------------------------------------------------------- 各区块绘制

def draw_wifi(dc, cx, cy, connected):
    """WiFi 扇形图标。connected=实心弧+实心点；断网=细弧+空心点（双编码）。"""
    if connected:
        for r in (7, 12, 17):
            dc.arc(cx, cy, r, 215, 325, fill=0, width=2.5)
    else:
        for r in (7, 12, 17):
            dc.arc(cx, cy, r, 215, 325, fill=0, width=1)


def draw_status_band(dc, f, sensor, now, stale_min):
    """大时间右侧窄栏的两行温湿度；图标保留最右上。"""
    temp = humidity = None
    if sensor and is_fresh(sensor.get("ts"), now, stale_min) and sensor.get("sensor_ok", True):
        temp, humidity = sensor.get("temp_c"), sensor.get("humidity")
    temp_s = "--.- C" if temp is None else "%.1f C" % float(temp)
    hum_s = "-- %%" if humidity is None else "%d %%" % round(float(humidity))
    # x=470 是大时间右缘之外、分割线 x=480 之前的安全窄栏。
    dc.text(470, 82, temp_s, 20, "monob", anchor="rs")
    dc.text(470, 112, hum_s, 20, "monob", anchor="rs")
    draw_wifi(dc, 686, 40, True)
    dc.rect([722, 31, 751, 49], outline=0, width=2)
    dc.rect([752, 36, 756, 44], fill=0)


def draw_clock(dc, now):
    """仅绘制大时间；板端版本会改由 ESP32 局部重绘。"""
    dc.text(24, 150, now.strftime("%H:%M"), 112, "monob")


def draw_date(dc, now):
    """日期始终属于电脑端基础位图，切页后立即可见。"""
    wk = "一二三四五六日"[now.weekday()]
    date_s = "%d / %02d / %02d · 星期%s" % (now.year, now.month, now.day, wk)
    dc.text(28, 194, date_s, 22, "cjkb", ls=1)


def draw_clock_date(dc, f, now, time_ok):
    """预览模式：完整绘制大时间和日期。"""
    draw_clock(dc, now)
    draw_date(dc, now)


def draw_progress(dc, f, label, pct, y_text, y_box, warn_low=True):
    """一行进度条：标签 + 300px 框 + 填充 + 右对齐百分比。pct=None 走降级(空心+--%)。"""
    dc.text(40, y_text, label, 18, "cjkb")
    x0, bw = 100, 245
    dc.rect([x0, y_box, x0 + bw, y_box + 16], outline=0, width=2)
    if pct is None:
        val = "--%"
    else:
        # 进度条与数字始终表达同一件事：可用额度剩余比例。
        # 单色屏不使用“低额度整条反色”这种会被误读为 100% 的视觉编码。
        p = max(0.0, min(100.0, float(pct)))
        inner = int(round(p / 100.0 * (bw - 6)))
        if inner > 0:
            dc.rect([x0 + 3, y_box + 3, x0 + 3 + inner, y_box + 13], fill=0)
        val = "%d%%" % round(p)
    dc.text(430, y_text, val, 18, "monob", anchor="rs")


def draw_codex_card(dc, f, codex, now, stale_min):
    """Codex 额度卡，下移以拉开与日期、DeepSeek 的间距。"""
    codex = codex or {}
    dc.rect([24, 238, 28, 314], fill=0)
    dc.text(40, 254, "Codex 用量", 19, "cjkb", ls=2)

    state = source_status(codex, codex.get("_attempt"), now, max_age=600,
                          value_ok=finite_number(codex.get("five_hour_remaining")) or finite_number(codex.get("weekly_remaining")))
    dc.text(464, 254, state["label"], 18, "cjk", anchor="rs")
    five_remaining = codex.get("five_hour_remaining") if finite_number(codex.get("five_hour_remaining")) and state["state"] in ("fresh", "cached") else None
    week_remaining = codex.get("weekly_remaining") if finite_number(codex.get("weekly_remaining")) and state["state"] in ("fresh", "cached") else None
    # 进度条统一表达「已用」：剩余 100% 时为空，用量越高填充越多。
    five_used = None if five_remaining is None else 100.0 - float(five_remaining)
    week_used = None if week_remaining is None else 100.0 - float(week_remaining)
    draw_progress(dc, f, "5小时", five_used, 283, 270)
    draw_progress(dc, f, "每周", week_used, 309, 296)


def draw_deepseek_card(dc, f, ds, now, stale_min):
    """DeepSeek 当前 API 余额：仅显示官方余额接口可验证的数据。"""
    ds = ds or {}
    dc.rect([24, 352, 28, 416], fill=0)
    dc.text(40, 368, "DeepSeek 余额", 19, "cjkb", ls=2)

    configured = bool(ds.get("configured"))
    state = source_status(ds, ds.get("_attempt"), now, max_age=600, configured=configured, value_ok=finite_number(ds.get("balance")))
    dc.text(464, 368, state["label"], 18, "cjk", anchor="rs")
    balance = ds.get("balance") if configured and finite_number(ds.get("balance")) and state["state"] in ("fresh", "cached") else None
    if balance is None:
        dc.text(40, 404, "未配置" if not configured else "暂不可用", 22, "cjkb")
        dc.text(144, 403, "设置 API Key 后自动更新" if not configured else "尚无有效查询结果", 18, "cjk")
        return

    currency = str(ds.get("currency") or "CNY").upper()
    prefix = "¥" if currency == "CNY" else ("$" if currency == "USD" else currency + " ")
    dc.text(40, 403, "可用余额", 18, "cjkb")
    dc.text(146, 405, prefix + ("%.2f" % float(balance)), 27, "monob")
    dc.text(378, 403, "" if state["state"] == "cached" else ("可调用" if ds.get("is_available") else "不可用"), 18, "cjkb")


def draw_fortune(dc, f, fortune, cfg):
    """底部运势带 y398-434：分隔线 + 今日运势 + 文案 + 5格方块评分。"""
    dc.rect([24, 398, 776, 400], fill=0)
    dc.text(24, 430, "今日运势", 16, "cjkb", ls=2)

    fortune = fortune or {}
    text = fortune.get("text") or cfg.get("fortune_fallback") or "运势待生成"
    lucky = fortune.get("lucky_number")
    body = text if lucky is None else "%s · 幸运数字 %s" % (text, lucky)
    # 文案区可用宽度 x128..616（→「运势」标签前），超宽降字号；降到 13px 及以下改粗体防断笔
    size = 20
    while size > 13 and dc.f.width("cjk", size, body) > 616 - 128:
        size -= 1
    kind = "cjk"
    if size <= 13:
        kind = "cjkb"
        while size > 11 and dc.f.width(kind, size, body) > 616 - 128:
            size -= 1
    dc.text(128, 430, body, size, kind)

    dc.text(624, 430, "运势", 13, "cjkb")
    score = fortune.get("score")
    score = int(score) if isinstance(score, (int, float)) else 0
    score = max(0, min(5, score))
    for i in range(5):
        x = 668 + i * 22
        if i < score:
            dc.rect([x, 416, x + 14, 430], fill=0)
        else:
            dc.rect([x, 416, x + 14, 430], outline=0, width=2)


# ---------------------------------------------------------------- 位图打包（与股票页同一约定）

def pack_bitmap(im, cfg):
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
            is_black = (px[x, y] == 0)
            if (1 if is_black else 0) == (1 if black_is_one else 0):
                if msb:
                    buf[base + (x >> 3)] |= 0x80 >> (x & 7)
                else:
                    buf[base + (x >> 3)] |= 0x01 << (x & 7)
    return bytes(buf)


def atomic_write(path, data):
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    if isinstance(data, (bytes, bytearray)):
        with open(tmp, "wb") as fp:
            fp.write(bytes(data))
    else:
        with open(tmp, "w", encoding="utf-8") as fp:
            fp.write(data)
    os.replace(tmp, path)


# ---------------------------------------------------------------- 主流程

DEFAULT_CFG = {
    # render_scale=4 是实测结论：12-15px 常规字重中文细笔画在 2x+BOX 降采样后
    # 覆盖率不足被阈值吃掉（断笔/鬼影），4x 才完整清晰。5 分钟一轮，耗时可忽略。
    "width": 800, "height": 480, "render_scale": 4, "threshold": 128,
    "refresh_minutes": 5,
    "art_image": os.path.join(SKILL_DIR, "assets", "art", "default_art.png"),
    "art_size": 272, "art_dither": True, "art_contrast": 1.3,
    "stale_minutes": 90,
    "fortune_fallback": "运势待生成",
    "bit_order": "msb", "black_is_one": False, "orientation": "landscape",
    "output_bin": "home.bin", "output_png": "home.png",
    "output_png_2x": "home@2x.png", "output_meta": "home_meta.json",
    "preview_scale": 2,
}


def main():
    ap = argparse.ArgumentParser(description="主页渲染：data/*.json -> out/home.bin")
    ap.add_argument("--config", default=DEFAULT_CONFIG_DIR)
    ap.add_argument("--runtime", default=DEFAULT_RUNTIME)
    ap.add_argument("--clock", default=None, help="覆盖时钟 HH:MM（调试）")
    ap.add_argument("--date", default=None, help="覆盖日期 YYYY-MM-DD（调试）")
    ap.add_argument("--device-base", action="store_true", help="给板子输出动态区域留白，由板端局部绘制")
    args = ap.parse_args()

    config_dir = os.path.abspath(os.path.expanduser(args.config))
    runtime = os.path.abspath(os.path.expanduser(args.runtime))
    data_dir = os.path.join(runtime, "data")
    out_dir = os.path.join(runtime, "out")
    os.makedirs(out_dir, exist_ok=True)

    cfg = dict(DEFAULT_CFG)
    user_cfg = load_json(os.path.join(config_dir, "home_config.json"), {}) or {}
    cfg.update({k: v for k, v in user_cfg.items() if not k.startswith("_")})

    # art_image 支持相对技能目录的路径
    art = cfg.get("art_image") or ""
    if art and not os.path.isabs(art):
        cfg["art_image"] = os.path.join(SKILL_DIR, art)

    W, H = int(cfg["width"]), int(cfg["height"])
    scale = max(1, int(cfg["render_scale"]))
    stale_min = int(cfg.get("stale_minutes", 90))

    sensor = load_json(os.path.join(data_dir, "sensor.json"))
    codex = load_json(os.path.join(data_dir, "codex.json"))
    deepseek = load_json(os.path.join(data_dir, "deepseek.json"))
    fortune = load_json(os.path.join(data_dir, "fortune.json"))

    # 本地时间（可被 --clock/--date 覆盖用于调试）
    attempts = status_load(os.path.join(os.path.dirname(runtime), ".runtime", "status.json"))
    codex = dict(codex or {}); codex["_attempt"] = attempts.get("codex", {})
    deepseek = dict(deepseek or {}); deepseek["_attempt"] = attempts.get("deepseek", {})
    now = china_now()
    if args.date:
        try:
            base = datetime.strptime(args.date, "%Y-%m-%d")
            now = now.replace(year=base.year, month=base.month, day=base.day)
        except Exception:
            pass
    if args.clock:
        try:
            now = now.replace(hour=int(args.clock[:2]), minute=int(args.clock[3:5]))
        except Exception:
            pass

    fonts = Fonts(scale)
    img = Image.new("1", (W * scale, H * scale), 1)  # 白底
    dc = D(img, fonts)

    if not args.device_base:
        draw_status_band(dc, fonts, sensor, now, stale_min)
        draw_clock(dc, now)
    draw_date(dc, now)
    draw_codex_card(dc, fonts, codex, now, stale_min)
    draw_deepseek_card(dc, fonts, deepseek, now, stale_min)

    art = prepare_art(cfg.get("art_image"), int(cfg.get("art_size", 272)),
                      bool(cfg.get("art_dither", True)), float(cfg.get("art_contrast", 1.3)))
    dc.rect([480, 92, 482, 416], fill=0)
    if art is not None:
        big = art.resize((int(cfg["art_size"]) * scale, int(cfg["art_size"]) * scale),
                         Image.Resampling.NEAREST)
        img.paste(big, (504 * scale, 105 * scale))
    dc.text(776, 405, "页面生成 " + now.strftime("%H:%M"), 13, "cjkb", anchor="rs")

    # 2x 超采样 -> BOX 降采样 -> 阈值二值化（"1"模式 point(LUT) 陷阱：必须先转 L）
    if scale != 1:
        thr = int(cfg.get("threshold", 128))
        img = img.convert("L").resize((W, H), Image.Resampling.BOX)
        img = img.point(lambda p: 255 if p >= thr else 0, "1")

    bin_path = os.path.join(out_dir, cfg["output_bin"])
    raw = pack_bitmap(img, cfg)
    atomic_write(bin_path, raw)
    img.save(os.path.join(out_dir, cfg["output_png"]))
    ps = max(1, int(cfg.get("preview_scale", 2)))
    if ps > 1:
        img.resize((W * ps, H * ps), Image.Resampling.NEAREST).save(
            os.path.join(out_dir, cfg["output_png_2x"]))
    atomic_write(os.path.join(out_dir, cfg["output_meta"]), json.dumps({
        "generated_at": china_now().isoformat(timespec="seconds"),
        "page": "home",
        "width": W, "height": H,
        "orientation": cfg.get("orientation"),
        "bit_order": cfg.get("bit_order"),
        "black_is_one": cfg.get("black_is_one"),
        "bin_bytes": len(raw),
        "bin_file": os.path.basename(bin_path),
        "refresh_minutes": cfg.get("refresh_minutes"),
        "source_details": {
            "codex": source_status(codex, attempts.get("codex"), now, max_age=600, value_ok=finite_number(codex.get("five_hour_remaining")) or finite_number(codex.get("weekly_remaining"))),
            "deepseek": source_status(deepseek, attempts.get("deepseek"), now, max_age=600, configured=bool(deepseek.get("configured")), value_ok=finite_number(deepseek.get("balance"))),
        },
        "sources": {
            "sensor": "fresh" if is_fresh((sensor or {}).get("ts"), now, stale_min) else "stale/none",
            "codex": "fresh" if is_fresh((codex or {}).get("ts"), now, stale_min) else "stale/none",
            "deepseek": "fresh" if is_fresh((deepseek or {}).get("ts"), now, stale_min) else "stale/none",
            "fortune": "ok" if (fortune or {}).get("text") else "fallback",
        },
        "font": fonts.info,
    }, ensure_ascii=False, indent=2))

    print("OK 主页渲染 %dx%d -> %s (%d 字节) | 时间=%s | sensor=%s codex=%s deepseek=%s fortune=%s | %s" % (
        W, H, bin_path, len(raw), now.strftime("%Y-%m-%d %H:%M"),
        "有" if sensor else "缺", "有" if codex else "缺",
        "有" if deepseek else "缺", "有" if (fortune or {}).get("text") else "兜底",
        fonts.info))
    return 0


if __name__ == "__main__":
    sys.exit(main())
