# 三页水墨屏运行包

本目录是板子使用的正式页面运行包，包含主页、股票页和图片占位页。

## 运行顺序

1. 运行 `./dashboard-runtime/run-real-pages.sh` 生成页面。首次会创建本目录内的 Python 环境并安装 Pillow。
2. 运行上层的 `./run-epaper-agent.sh` 启动局域网服务。
3. 板子连入同一 Wi-Fi 后，通过 UDP 自动发现服务并下载当前页面。

股票采集使用腾讯行情接口。只有八项自选数据全部返回时，才会替换 `stock/latest.json` 与股票位图；请求失败、数据不完整或断网时，原有的真实位图会保留，绝不写入模拟数据。

## 页面文件约定

| 页面 | 服务接口 | 位图 |
|---|---|---|
| 主页 | `/api/display/page/1` | `home/out/home.bin` |
| 股票 | `/api/display/page/2` | `stock/out/screen.bin` |
| 图片占位 | `/api/display/page/3` | `photo/photo.bin` |

三个位图均为 800×480、1-bit、48,000 字节、MSB、黑=0、白=1。

## DeepSeek 余额

在 `dashboard-runtime/home/` 新建隐藏文件 `.deepseek_api_key`，文件内容只放一行 DeepSeek API Key。启动服务后，主页会每 5 分钟查询一次当前 API 余额。Key 不会发送给墨水屏或写入位图。
