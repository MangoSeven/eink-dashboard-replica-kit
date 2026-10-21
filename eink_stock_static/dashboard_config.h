// 这些是首次启动时的可选默认值。通常保持为空：
// 长按板子中间键约 2 秒，手机连接屏幕显示的热点，在 192.168.4.1 配网。
// ESP32-S3 只支持 2.4GHz Wi-Fi。
#pragma once

#define DASHBOARD_WIFI_SSID ""
#define DASHBOARD_WIFI_PASSWORD ""

// 电脑上的位图服务地址，例如 "http://192.168.1.25:1234"。
// 网页配网时也可以填写；留空时，板子仍会显示离线股票页。
#define DASHBOARD_HOST ""

// 各页面向局域网服务检查新位图的间隔。股票暂设为 1 分钟，便于验证。
#define DASHBOARD_HOME_REFRESH_MINUTES 5
#define DASHBOARD_STOCK_REFRESH_MINUTES 1
#define DASHBOARD_READING_REFRESH_HOURS 24
