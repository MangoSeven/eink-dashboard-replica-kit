#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""读取本机保存的 DeepSeek API Key，查询账户当前可用余额。"""
from __future__ import annotations

import argparse
import json
import os
import sys
import math
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

API_URL = "https://api.deepseek.com/user/balance"
TIMEOUT = 15


def now_ts():
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def atomic_write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_key(runtime):
    # 环境变量便于高级用户自动化；本地隐藏文件便于普通用户配置。
    value = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if value:
        return value
    path = os.path.join(runtime, ".deepseek_api_key")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def fetch_balance(api_key):
    req = urllib.request.Request(API_URL, headers={
        "Authorization": "Bearer " + api_key,
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser(description="采集 DeepSeek 当前 API 余额")
    ap.add_argument("--runtime", required=True)
    args = ap.parse_args()
    runtime = os.path.abspath(os.path.expanduser(args.runtime))
    out_path = os.path.join(runtime, "data", "deepseek.json")
    api_key = load_key(runtime)

    if not api_key:
        atomic_write(out_path, {"configured": False, "ts": now_ts()})
        print("INFO DeepSeek API Key 未配置")
        return 0

    try:
        doc = fetch_balance(api_key)
        infos = doc.get("balance_infos") or []
        info = next((item for item in infos if item.get("currency") == "CNY"), infos[0] if infos else {})
        value = float(info["total_balance"])
        if not math.isfinite(value) or value < 0:raise ValueError("Invalid balance")
        result = {
            "configured": True,
            "is_available": bool(doc.get("is_available")),
            "currency": info.get("currency", "CNY"),
            "balance": value,
            "granted_balance": float(info.get("granted_balance", 0)),
            "topped_up_balance": float(info.get("topped_up_balance", 0)),
            "ts": now_ts(),
            "source": "DeepSeek /user/balance",
        }
        atomic_write(out_path, result)
        print("OK DeepSeek 余额: %s %.2f" % (result["currency"], result["balance"]))
        return 0
    except urllib.error.HTTPError as e:
        print("WARN DeepSeek 余额查询失败: HTTP %s；保留上次有效余额" % e.code, file=sys.stderr)
    except Exception as e:
        print("WARN DeepSeek 响应无效或请求失败；保留上次有效余额", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
