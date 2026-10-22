# ESP32-S3 墨水屏三页看板复刻包

适用于 **Waveshare ESP32-S3-ePaper-3.97**：主页（时间、温湿度、Codex 用量、DeepSeek 余额）、股票看板、每日读书推荐。显示方向按原项目旋转 180°。

## 交给 AI 复刻

将整个文件夹或本项目地址交给能读写文件、运行程序的 Codex、WorkBuddy、OpenClaw 等 AI agent，复制：

> 请完整阅读 AI_AGENT_README.md，帮我安装并复刻这套看板。能自行完成的下载、配置、编译和检查请直接执行；需要我的权限或实体操作时给出明确步骤。接口失效请自行核对替代方案，不用模拟数据冒充真实数据。

项目地址：https://github.com/MangoSeven/eink-dashboard-replica-kit

## 首次安装与上传

在本文件夹打开终端，由 AI 优先执行：

```bash
python3 setup/agent_bootstrap.py --check
python3 setup/agent_bootstrap.py --install --compile
python3 setup/agent_bootstrap.py --upload --port <检测到的串口>
./run-epaper-agent.sh
```

环境：Python 3、Arduino CLI 或 Arduino IDE 2、ESP32 Core 2.0.17、GxEPD2。Arduino IDE 打开 `eink_stock_static/eink_stock_static.ino`，所有头文件必须保留在同目录。连接必须使用支持数据传输的 USB 线。

上传后长按中键约 3 秒并松开进入配网，手机连接屏幕热点，访问 `http://192.168.4.1`，选择 2.4GHz Wi-Fi 并保存。板子与电脑在同一局域网，通过 UDP 18999 自动发现 HTTP 1234 服务；发现失败可在配网页面填写电脑局域网地址。不要配置公网访问。

## 日常运行

使用 `./run-epaper-agent.sh` 启动单实例管理程序，自动创建本目录虚拟环境及安装 Pillow，启动并守护网页服务、三页更新器。保持终端运行，Ctrl+C 停止整个服务。电脑休眠或服务关闭后，屏幕保留缓存，联网数据不会继续更新。

重复运行不会启动第二份本目录服务。若 1234 已被旧服务或其他程序占用，先确认并停止旧服务；本程序不会自动杀掉占用者。状态查看 `http://电脑局域网地址:1234/status`，仅提供任务结果和时间。

## 刷新与操作

| 内容 | 正常周期 |
| --- | --- |
| 主页用量与余额 | 电脑及板端每 5 分钟检查；分开标出各自采集时间，失败保留数值并标缓存 |
| 时间、温湿度、电池及 Wi-Fi | 主页每分钟检查，内容变化才局部绘制 |
| 股票 | 每 1 分钟采集/检查；失败或过期标缓存，不伪造行情 |
| 读书 | 电脑每天重新生成；板端读书页每 24 小时检查，切页或中键也可获取 |

失败后采用退避重试，成功恢复正常周期。上下键切页先显示缓存，中键短按请求刷新；中键长按进入/退出配网。页面显示和 HTTP 短等待仍有耗时，按键会排队。PWR 保持硬件电源用途。

## 当前版本与升级

当前固件串口标识：**`[BUILD] 2026-10-23 reliability-v3`**。

已加入离线按键、分步关联、电池模式 Light-sleep、数据采集时间/缓存标记、独立 Wi-Fi 与电脑服务状态、原始电池记录、服务单实例与子进程恢复。升级需要重新上传固件，并停止旧电脑服务、运行新启动脚本；只更新其中一端无法获得全部新功能。

详见 [可靠性优化与验收.md](可靠性优化与验收.md)。已完成编译、主机模拟、本地服务联调和 PNG 检查；未完成本版板上验证、完整电量校准或长期续航测试，不承诺达到 10–15 天续航。

## 可选：macOS 登录自启

先手动运行服务确认正常，再自行执行：

```bash
python3 setup/manage_autostart.py --install
# 取消：
python3 setup/manage_autostart.py --uninstall
```

仅在当前用户登录后运行；电脑休眠期间不能采集。本次代码交付未替用户启用系统启动项。其他平台由接手 AI 按该系统配置用户级启动方式。

## 数据与隐私

DeepSeek Key 可选，由用户保存在本机 `dashboard-runtime/home/.deepseek_api_key`；不要贴到聊天或提交 Git。Codex 用量依赖复刻者本机登录态。股票使用公开真实行情，接口变化时接手 AI 应验证替代源。

日志和原始电池记录位于 `dashboard-runtime/.runtime/`（电池记录为 battery.csv）；它们不是电流实测结果，不应公开分发。复刻包不包含原作者 Key、Wi-Fi 密码、账号余额、登录信息、历史行情、日志和虚拟环境，保留无账户数据的离线兜底图。

## 许可证

原创代码与文档采用 [MIT License](LICENSE)。第三方库、角色形象和图片素材权利归各自权利人所有，不因本项目许可证获得额外授权。`dashboard-runtime/assets/art/default_art.png` 为角色示例图，使用或分发前请确认授权；可通过 `home_config.json` 替换。
