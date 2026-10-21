#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
eink-home-dashboard · 环节 1a —— Codex 官方额度采集（独立运行）

数据源（非公开接口，OpenAI 可能改动，故带多级降级）：
  GET https://chatgpt.com/backend-api/wham/usage
  鉴权：只读本机 Codex OAuth access_token（Bearer）
  返回：rate_limit.primary_window.used_percent（5小时滚动窗）
        rate_limit.secondary_window.used_percent（7天窗）
  屏上语义：剩余% = 100 - used_percent（与设计稿「5小时剩余/每周剩余」一致）

凭证查找顺序：
  1. $CODEX_HOME/auth.json
  2. ~/.codex/auth.json
  3. ~/.config/codex/auth.json
  4. macOS 钥匙串（security find-generic-password -s "Codex Auth"）

安全边界：
  看板仅读取现有 access_token 请求额度，绝不刷新、缓存或回写 Codex 登录凭证。
  access_token 过期时保留上一次额度，待 Codex 自身登录态更新后自动恢复。

降级链：直接请求 -> 刷新后重试 -> 本机 codex-cli-usage json（若已安装）-> 放弃（不改动 codex.json，
渲染端靠 ts 过期自动显示 --% 降级态）。

输出：<runtime>/data/codex.json
  {"five_hour_remaining": 68, "weekly_remaining": 41, "plan": "plus", "stale": false, "ts": "..."}

用法：python3 fetch_codex.py [--runtime DIR]
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
TIMEOUT = 15

DEFAULT_RUNTIME = os.path.expanduser("~/WorkBuddy/eink-home-data")


def log(msg):
    sys.stderr.write("[codex] %s\n" % msg)


def now_ts():
    return datetime.now().isoformat(timespec="seconds")


def atomic_write(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------- 凭证

def auth_candidates():
    out = []
    ch = os.environ.get("CODEX_HOME")
    if ch:
        out.append(os.path.join(ch, "auth.json"))
    out.append(os.path.expanduser("~/.codex/auth.json"))
    out.append(os.path.expanduser("~/.config/codex/auth.json"))
    return [p for p in out if os.path.isfile(p)]


def load_auth_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f), path
    except Exception:
        return None, None


def load_auth_keychain():
    """macOS 钥匙串兜底：Codex CLI 用 keychain 存储时磁盘上没有 auth.json。"""
    if sys.platform != "darwin" or not shutil.which("security"):
        return None, None
    try:
        raw = subprocess.run(["security", "find-generic-password", "-s", "Codex Auth", "-w"],
                             capture_output=True, text=True, timeout=10)
        if raw.returncode != 0:
            return None, None
        return json.loads(raw.stdout), "keychain"
    except Exception:
        return None, None


def get_auth():
    for p in auth_candidates():
        a, src = load_auth_file(p)
        if a:
            return a, src
    return load_auth_keychain()


def tokens_of(auth):
    t = (auth or {}).get("tokens") or {}
    return t.get("access_token"), t.get("refresh_token"), t.get("account_id")


def http_json(url, headers, data=None, timeout=TIMEOUT):
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_usage(access_token, account_id=None):
    headers = {"Authorization": "Bearer %s" % access_token, "Accept": "application/json"}
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    return http_json(USAGE_URL, headers)


def parse_usage(doc):
    rl = (doc or {}).get("rate_limit") or {}
    prim = rl.get("primary_window") or {}
    sec = rl.get("secondary_window") or {}

    def remaining(win):
        u = win.get("used_percent")
        if u is None:
            return None
        return max(0.0, min(100.0, 100.0 - float(u)))

    return {
        "five_hour_remaining": remaining(prim),
        "weekly_remaining": remaining(sec),
        "plan": doc.get("plan_type"),
    }


def fallback_codex_cli_usage():
    """本机装了 codex-cli-usage 时用它兜底（它自己处理 token 刷新与缓存）。"""
    exe = shutil.which("codex-cli-usage")
    if not exe:
        return None
    try:
        raw = subprocess.run([exe, "json"], capture_output=True, text=True, timeout=30)
        if raw.returncode != 0:
            return None
        doc = json.loads(raw.stdout)
    except Exception:
        return None
    # 输出结构随版本变化，宽松解析：找 5h/weekly 的 used 或 remaining 百分比
    def pick(*keys):
        for k in keys:
            v = doc
            for part in k.split("."):
                if isinstance(v, dict):
                    v = v.get(part)
                else:
                    v = None
                    break
            if isinstance(v, (int, float)):
                return float(v)
        return None

    five_used = pick("primary.used_percent", "primary.usedPercent", "five_hour.used_percent")
    week_used = pick("secondary.used_percent", "secondary.usedPercent", "weekly.used_percent")
    five_rem = pick("primary.remaining_percent", "five_hour.remaining")
    week_rem = pick("secondary.remaining_percent", "weekly.remaining")
    five = five_rem if five_rem is not None else (100 - five_used if five_used is not None else None)
    week = week_rem if week_rem is not None else (100 - week_used if week_used is not None else None)
    if five is None and week is None:
        return None
    return {"five_hour_remaining": five, "weekly_remaining": week, "plan": doc.get("plan_type")}


def main():
    import argparse
    ap = argparse.ArgumentParser(description="采集 Codex 官方额度剩余%")
    ap.add_argument("--runtime", default=DEFAULT_RUNTIME)
    args = ap.parse_args()
    runtime = os.path.abspath(os.path.expanduser(args.runtime))
    data_dir = os.path.join(runtime, "data")
    out_path = os.path.join(data_dir, "codex.json")
    auth, src = get_auth()
    if not auth:
        log("未找到 Codex 凭证（auth.json / 钥匙串均无），尝试 codex-cli-usage 兜底")
    else:
        at, _rt, acct = tokens_of(auth)
        if at:
            try:
                doc = fetch_usage(at, acct)
                parsed = parse_usage(doc)
                if parsed.get("five_hour_remaining") is not None or parsed.get("weekly_remaining") is not None:
                    parsed.update({"stale": False, "ts": now_ts(), "source": "wham/usage"})
                    atomic_write(out_path, parsed)
                    print("OK Codex 额度: 5小时剩 %s%% 每周剩 %s%% (plan=%s)" % (
                        parsed["five_hour_remaining"], parsed["weekly_remaining"], parsed.get("plan")))
                    return 0
            except urllib.error.HTTPError as e:
                # 不刷新、不缓存、更不回写登录态；下次 Codex 自身更新令牌后自动恢复。
                log("HTTP %s；保留上次数据，不修改 Codex 登录凭证" % e.code)
            except Exception as e:
                log("请求失败: %s" % e)

    fb = fallback_codex_cli_usage()
    if fb:
        fb.update({"stale": False, "ts": now_ts(), "source": "codex-cli-usage"})
        atomic_write(out_path, fb)
        print("OK Codex 额度(codex-cli-usage 兜底): 5小时剩 %s%% 每周剩 %s%%" % (
            fb["five_hour_remaining"], fb["weekly_remaining"]))
        return 0

    log("全部途径失败：不改动 codex.json，渲染端将按 ts 过期显示降级态(--%%)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
