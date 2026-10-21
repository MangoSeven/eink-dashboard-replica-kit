#!/bin/zsh
# 立即更新三张页面。单一数据源失败时保留随安装包提供的上一张有效页面。
set -u
ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV="$ROOT/.venv"; PY="$VENV/bin/python"
if [[ ! -x "$PY" ]]; then
  /usr/bin/python3 -m venv "$VENV"
  "$PY" -m pip install --upgrade pip
  "$PY" -m pip install -r "$ROOT/requirements.txt"
fi
"$PY" "$ROOT/home/fetch_codex.py" --runtime "$ROOT/home" || echo "WARN Codex 用量更新失败，保留上次数据" >&2
"$PY" "$ROOT/home/fetch_deepseek_balance.py" --runtime "$ROOT/home" || echo "WARN DeepSeek 余额更新失败，保留上次数据" >&2
"$PY" "$ROOT/home/render_home.py" --runtime "$ROOT/home" --config "$ROOT/home" --device-base || echo "WARN 主页渲染失败，保留上一张页面" >&2
if "$PY" "$ROOT/stock/fetch_tencent_quotes.py" --config "$ROOT/stock/watchlist.json" --runtime "$ROOT/stock"; then
  "$PY" "$ROOT/stock/render_screen.py" --config "$ROOT/stock" --runtime "$ROOT/stock" || echo "WARN 股票页渲染失败，保留上一张页面" >&2
else
  echo "WARN 股票行情更新失败，保留上一张股票页" >&2
fi
"$PY" "$ROOT/reading/render_reading.py" --runtime "$ROOT/reading" || echo "WARN 读书页渲染失败，保留上一张页面" >&2
for page in "$ROOT/home/out/home.bin" "$ROOT/stock/out/screen.bin" "$ROOT/reading/out/reading.bin"; do
  [[ $(wc -c < "$page" | tr -d ' ') == 48000 ]] || { echo "ERR 位图尺寸异常: $page" >&2; exit 1; }
done
echo "OK 三页位图已就绪"
