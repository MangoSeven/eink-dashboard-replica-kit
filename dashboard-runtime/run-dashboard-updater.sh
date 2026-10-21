#!/bin/zsh
# 长驻更新器：主页 5 分钟、股票 1 分钟（验证用）、读书页每天一次。
set -u
ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV="$ROOT/.venv"
PY="$VENV/bin/python"
if [[ ! -x "$PY" ]]; then
  /usr/bin/python3 -m venv "$VENV"
  "$PY" -m pip install --upgrade pip
  "$PY" -m pip install -r "$ROOT/requirements.txt"
fi
last_home=0; last_stock=0; last_day=""
while true; do
  now=$(date +%s); day=$(date +%F)
  if (( now - last_home >= 300 )); then
    "$PY" "$ROOT/home/fetch_codex.py" --runtime "$ROOT/home" || echo "WARN Codex 用量更新失败，保留上次数据" >&2
    "$PY" "$ROOT/home/fetch_deepseek_balance.py" --runtime "$ROOT/home" || echo "WARN DeepSeek 余额更新失败，保留上次数据" >&2
    "$PY" "$ROOT/home/render_home.py" --runtime "$ROOT/home" --config "$ROOT/home" --device-base || echo "WARN 主页渲染失败" >&2
    last_home=$now
  fi
  if (( now - last_stock >= 60 )); then
    if "$PY" "$ROOT/stock/fetch_tencent_quotes.py" --config "$ROOT/stock/watchlist.json" --runtime "$ROOT/stock"; then
      "$PY" "$ROOT/stock/render_screen.py" --config "$ROOT/stock" --runtime "$ROOT/stock" || echo "WARN 股票页渲染失败" >&2
    else
      echo "WARN 股票行情更新失败，保留上一张真实股票页" >&2
    fi
    last_stock=$now
  fi
  if [[ "$last_day" != "$day" ]]; then
    "$PY" "$ROOT/reading/render_reading.py" --runtime "$ROOT/reading" || echo "WARN 读书页渲染失败" >&2
    last_day=$day
  fi
  sleep 5
done
