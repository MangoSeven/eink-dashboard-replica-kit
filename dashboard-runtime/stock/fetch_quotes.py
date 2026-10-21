#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
环节 1/3 —— 数据采集（独立运行，不依赖渲染环节）

职责单一：读 watchlist.json -> 调 westock 批量拉行情 -> 写 latest.json + 追加 history.sqlite

契约（与下游唯一的耦合点）：
  输入  <config_dir>/watchlist.json
  输出  <runtime>/latest.json      最新一轮快照（原子覆盖写）
        <runtime>/history.sqlite   分时采样累积（供分时曲线用）
        <runtime>/fetch.log        运行日志（追加）

用法：
  python3 fetch_quotes.py [--config DIR] [--runtime DIR] [--force]
    --force   忽略交易时段判断，强制采集一次（用于调试 / 收盘后补数）

退出码：0 成功 / 2 限流（已跳过本轮，保留上一次数据）/ 3 采集失败
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, date, time as dtime

# ---------------------------------------------------------------- 常量

DEFAULT_CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config")
DEFAULT_RUNTIME = os.path.expanduser("~/WorkBuddy/eink-dashboard-data")
WESTOCK_CANDIDATES = [
    os.path.expanduser("~/.local/bin/westock"),
    "/usr/local/bin/westock",
    "westock",
]
RATE_LIMIT_MARKERS = ("请求过于频繁", "请稍后再试", "rate limit", "too many requests")

# 需要从 westock 输出里取的字段（其余丢弃，减小 latest.json 体积）
KEEP_FIELDS = (
    "code", "name", "price", "prev_close", "open", "high", "low",
    "volume", "amount", "change", "change_percent", "turnover_rate",
    "volume_ratio", "pe_ratio", "pb_ratio", "total_market_cap",
    "chg_5d", "chg_10d", "chg_20d", "chg_60d", "chg_ytd",
    "high_52week", "low_52week", "avg_price",
)
NUMERIC_FIELDS = set(KEEP_FIELDS) - {"code", "name"}


# ---------------------------------------------------------------- 工具

def log(runtime: str, msg: str) -> None:
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        os.makedirs(runtime, exist_ok=True)
        with open(os.path.join(runtime, "fetch.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def find_westock(explicit: str | None = None) -> str | None:
    for cand in ([explicit] if explicit else []) + WESTOCK_CANDIDATES:
        if not cand:
            continue
        p = shutil.which(cand) or (cand if os.path.isfile(cand) and os.access(cand, os.X_OK) else None)
        if p:
            return p
    return None


def atomic_write(path: str, text: str) -> None:
    """原子写：先写临时文件再 rename，避免渲染环节读到半截 JSON。"""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def to_num(raw: str):
    """westock 输出里空值/占位符很多（''、'-'、'0'），统一转 float 或 None。"""
    if raw is None:
        return None
    s = str(raw).strip()
    if s in ("", "-", "--", "None", "null"):
        return None
    try:
        return float(s)
    except ValueError:
        return s  # 非数字字段（如 name）原样保留


# ---------------------------------------------------------------- westock Markdown 表解析

def parse_markdown_table(text: str) -> list[dict]:
    """把 westock 的 Markdown 表格解析成 list[dict]。容错：跳过分隔行、忽略空行。"""
    rows: list[dict] = []
    header: list[str] | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        # 分隔行 | --- | --- |
        if all(re.fullmatch(r":?-{2,}:?", c or "-") for c in cells if c != ""):
            continue
        if header is None:
            header = cells
            continue
        if len(cells) != len(header):
            continue
        rows.append(dict(zip(header, cells)))
    return rows


def run_westock(binary: str, codes: list[str], timeout: int = 30) -> tuple[str, str]:
    """
    调用 westock quote 批量拉行情。
    用极简环境（env -i）调用，证明并保证：不携带任何会话凭证、不经过任何 LLM，
    纯本地二进制直连腾讯行情服务器。
    返回 (stdout+stderr, 状态) 状态 ∈ {"ok", "rate_limited", "error"}
    """
    cmd = [binary, "quote", ",".join(codes)]
    env = {"PATH": os.path.dirname(binary) + ":/usr/bin:/bin", "HOME": os.path.expanduser("~")}
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=env,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return "", "error"
    except Exception:
        return "", "error"

    out = (proc.stdout or "") + (proc.stderr or "")
    if any(m in out for m in RATE_LIMIT_MARKERS):
        return out, "rate_limited"
    if proc.returncode != 0 or "|" not in out:
        return out, "error"
    return out, "ok"


# ---------------------------------------------------------------- 交易时段判定

def load_holidays(config_dir: str) -> set[str]:
    p = os.path.join(config_dir, "holidays.json")
    if not os.path.isfile(p):
        return set()
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("holidays", []))
    except Exception:
        return set()


def market_state(now: datetime, holidays: set[str], tz_label: str = "北京时间") -> tuple[str, bool]:
    """
    返回 (状态文字, 是否交易时段)。
    A 股：09:15-09:25 集合竞价 / 09:30-11:30 / 13:00-15:00
    港股：09:30-12:00 / 13:00-16:00（取并集，保证小米在港股时段也能更新）
    """
    if now.weekday() >= 5:
        return "休市", False
    if now.strftime("%Y-%m-%d") in holidays:
        return "休市", False

    t = now.time()
    a_share = (dtime(9, 15) <= t <= dtime(11, 30)) or (dtime(13, 0) <= t <= dtime(15, 0))
    hk = (dtime(9, 30) <= t <= dtime(12, 0)) or (dtime(13, 0) <= t <= dtime(16, 0))

    if a_share and hk:
        return "盘中", True
    if a_share:
        return "盘中", True
    if hk:
        return "港股盘", True
    if dtime(11, 30) < t < dtime(13, 0):
        return "午休", False
    if t < dtime(9, 15):
        return "未开盘", False
    return "已收盘", False


# ---------------------------------------------------------------- SQLite 分时存储

def init_db(path: str) -> sqlite3.Connection:
    """建表。若库文件损坏（disk I/O error / NOTADB）则隔离旧文件重建，保证无人值守自愈。"""
    last_err = None
    for attempt in range(2):
        try:
            conn = sqlite3.connect(path, timeout=10)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ticks (
                    ts       TEXT NOT NULL,
                    day      TEXT NOT NULL,
                    code     TEXT NOT NULL,
                    price    REAL,
                    chg_pct  REAL,
                    volume   REAL,
                    PRIMARY KEY (ts, code)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ticks_day_code ON ticks(day, code)")
            conn.commit()
            return conn
        except sqlite3.DatabaseError as e:
            last_err = e
            try:
                conn.close()
            except Exception:
                pass
            # 隔离损坏库与残留 journal/wal，下一轮循环重建
            for suffix in ("", "-journal", "-wal", "-shm"):
                p = path + suffix
                if os.path.exists(p):
                    try:
                        os.replace(p, p + ".corrupt")
                    except Exception:
                        pass
    raise last_err


def store_ticks(conn: sqlite3.Connection, ts: str, day: str, quotes: list[dict]) -> int:
    n = 0
    for q in quotes:
        price = to_num(q.get("price"))
        if price is None:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO ticks(ts, day, code, price, chg_pct, volume) VALUES(?,?,?,?,?,?)",
            (ts, day, q["code"], price, to_num(q.get("change_percent")), to_num(q.get("volume"))),
        )
        n += 1
    conn.commit()
    return n


def prune_old_days(conn: sqlite3.Connection, keep_days: int = 10) -> None:
    """只保留最近 N 个交易日的分时数据，防止 sqlite 无限膨胀。"""
    try:
        cutoff = date.fromordinal(date.today().toordinal() - keep_days).strftime("%Y-%m-%d")
        conn.execute("DELETE FROM ticks WHERE day < ?", (cutoff,))
        conn.commit()
    except Exception:
        pass


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser(description="环节1：采集行情（独立运行）")
    ap.add_argument("--config", default=DEFAULT_CONFIG_DIR, help="配置目录（含 watchlist.json）")
    ap.add_argument("--runtime", default=DEFAULT_RUNTIME, help="运行时数据目录")
    ap.add_argument("--westock", default=None, help="westock 可执行文件路径（默认自动查找）")
    ap.add_argument("--force", action="store_true", help="忽略交易时段判断，强制采集")
    ap.add_argument("--keep-days", type=int, default=10, help="分时数据保留天数")
    args = ap.parse_args()

    config_dir = os.path.abspath(args.config)
    runtime = os.path.abspath(os.path.expanduser(args.runtime))
    os.makedirs(runtime, exist_ok=True)

    # ---- 读 watchlist（用户可自由增删标的，本脚本不做任何假设）
    wl_path = os.path.join(config_dir, "watchlist.json")
    if not os.path.isfile(wl_path):
        log(runtime, "ERR 找不到 watchlist.json: %s" % wl_path)
        return 3
    try:
        with open(wl_path, encoding="utf-8") as f:
            wl = json.load(f)
    except Exception as e:
        log(runtime, "ERR watchlist.json 解析失败: %s" % e)
        return 3

    items = wl.get("items") or []
    if not items:
        log(runtime, "ERR watchlist.json 里 items 为空")
        return 3
    codes = [it["code"] for it in items if it.get("code")]
    benchmark = wl.get("benchmark") or {}
    bench_code = benchmark.get("code")
    if bench_code and bench_code not in codes:
        codes = [bench_code] + codes

    # ---- 交易时段判定
    now = datetime.now()
    state, trading = market_state(now, load_holidays(config_dir))
    if not trading and not args.force:
        log(runtime, "SKIP 当前非交易时段（%s），本轮不采集" % state)
        # 仍然刷新 latest.json 的 market_state，让屏幕能显示"午休/已收盘"
        prev = os.path.join(runtime, "latest.json")
        if os.path.isfile(prev):
            try:
                with open(prev, encoding="utf-8") as f:
                    data = json.load(f)
                data["market_state"] = state
                data["trading"] = False
                data["checked_at"] = now.isoformat(timespec="seconds")
                atomic_write(prev, json.dumps(data, ensure_ascii=False, indent=2))
            except Exception:
                pass
        return 0

    # ---- 采集（限流退避：最多 2 次，间隔 30s）
    binary = find_westock(args.westock)
    if not binary:
        log(runtime, "ERR 找不到 westock 可执行文件，请先安装（见 SKILL.md 安装章节）")
        return 3

    quotes, status = [], "error"
    for attempt in range(2):
        out, status = run_westock(binary, codes)
        if status == "ok":
            quotes = parse_markdown_table(out)
            break
        if status == "rate_limited":
            log(runtime, "WARN 触发数据源限流（第 %d 次），30 秒后退避重试" % (attempt + 1))
            time.sleep(30)
            continue
        log(runtime, "ERR westock 调用失败（第 %d 次）: %s" % (attempt + 1, out[:200]))
        time.sleep(5)

    if status != "ok" or not quotes:
        log(runtime, "FAIL 本轮采集失败，保留上一帧数据不动（水墨屏会维持旧画面）")
        return 2 if status == "rate_limited" else 3

    by_code = {}
    for row in quotes:
        clean = {}
        for k in KEEP_FIELDS:
            if k in row:
                clean[k] = to_num(row[k]) if k in NUMERIC_FIELDS else row[k]
        if clean.get("code"):
            by_code[clean["code"]] = clean

    # ---- 写 latest.json（含渲染环节需要的全部上下文）
    ts = now.strftime("%Y-%m-%d %H:%M:%S")
    payload = {
        "fetched_at": ts,
        "fetched_date": now.strftime("%Y-%m-%d"),
        "market_state": state,
        "trading": trading,
        "source": "westock (腾讯自选股接口)",
        "watchlist": wl,          # 原样带上，渲染环节按此顺序与别名绘制
        "quotes": by_code,
        "benchmark_code": bench_code,
    }
    atomic_write(os.path.join(runtime, "latest.json"),
                 json.dumps(payload, ensure_ascii=False, indent=2))

    # ---- 追加 history.sqlite
    try:
        conn = init_db(os.path.join(runtime, "history.sqlite"))
        n = store_ticks(conn, ts, now.strftime("%Y-%m-%d"), list(by_code.values()))
        prune_old_days(conn, args.keep_days)
        conn.close()
        log(runtime, "OK %s 状态=%s 采集 %d/%d 标的，入库 %d 条" % (ts, state, len(by_code), len(codes), n))
    except Exception as e:
        log(runtime, "WARN latest.json 已写，但入库失败: %s" % e)

    return 0


if __name__ == "__main__":
    sys.exit(main())
