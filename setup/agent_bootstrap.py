#!/usr/bin/env python3
"""为 Waveshare ESP32-S3-ePaper-3.97 复刻包执行环境检测、依赖安装、编译与可选上传。"""
from __future__ import annotations
import argparse, os, platform, shutil, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKETCH = ROOT / 'eink_stock_static'
FQBN = 'esp32:esp32:esp32s3:FlashMode=dio,FlashSize=16M,PSRAM=opi,PartitionScheme=fatflash'


def run(cmd, check=True):
    print('+', ' '.join(map(str, cmd)))
    return subprocess.run(list(map(str, cmd)), check=check)


def find_cli():
    candidates = [shutil.which('arduino-cli')]
    if platform.system() == 'Darwin':
        candidates.append('/Applications/Arduino IDE.app/Contents/Resources/app/lib/backend/resources/arduino-cli')
    for value in candidates:
        if value and Path(value).is_file(): return value
    return None


def candidates_for_ports():
    if platform.system() == 'Darwin': return sorted(Path('/dev').glob('cu.usb*'))
    if platform.system() == 'Linux': return sorted(list(Path('/dev').glob('ttyACM*')) + list(Path('/dev').glob('ttyUSB*')))
    return []


def ensure_arduino(cli, install):
    if not cli and install and platform.system() == 'Darwin' and shutil.which('brew'):
        # Homebrew 可用时，AI 应自行下载 Arduino IDE；安装后再探测内置 CLI。
        run(['brew', 'install', '--cask', 'arduino-ide'])
        cli = find_cli()
    if not cli:
        raise RuntimeError('未找到 Arduino CLI。AI 应先尝试通过当前系统的软件包管理器安装 Arduino IDE 2 或 Arduino CLI；若环境禁止下载或安装，再请求用户授予该项权限。')
    run([cli, 'version'])
    if install:
        run([cli, 'core', 'update-index'])
        run([cli, 'core', 'install', 'esp32:esp32@2.0.17'])
        run([cli, 'lib', 'install', 'GxEPD2'])
    return cli


def check_secret_files():
    forbidden = [ROOT/'dashboard-runtime/home/.deepseek_api_key']
    found = [str(p.relative_to(ROOT)) for p in forbidden if p.exists() and p.read_text(errors='ignore').strip()]
    if found:
        raise RuntimeError('复刻包中不应携带私密 Key：' + ', '.join(found))


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--install', action='store_true', help='安装 ESP32 2.0.17 和 GxEPD2')
    ap.add_argument('--compile', action='store_true', help='编译固件')
    ap.add_argument('--upload', action='store_true', help='编译并上传，需配合 --port')
    ap.add_argument('--port', help='串口，例如 /dev/cu.usbmodem1101')
    ap.add_argument('--check', action='store_true', help='检查工程结构和隐私文件')
    args=ap.parse_args()
    if args.check:
        check_secret_files()
        required=['AI_AGENT_README.md','eink_stock_static/eink_stock_static.ino','dashboard-runtime/home/render_home.py','dashboard-runtime/stock/fetch_tencent_quotes.py']
        missing=[x for x in required if not (ROOT/x).exists()]
        if missing: raise RuntimeError('缺少文件：'+', '.join(missing))
        print('OK 复刻包结构与隐私文件检查通过')
    if args.install or args.compile or args.upload:
        cli=ensure_arduino(find_cli(), args.install)
        if args.compile or args.upload:
            build=ROOT/'.build'
            run([cli, 'compile', '--fqbn', FQBN, '--build-path', build, SKETCH])
            print('OK 固件编译通过')
        if args.upload:
            if not args.port:
                ports=', '.join(map(str,candidates_for_ports())) or '未检测到'
                raise RuntimeError('未指定串口。检测到：'+ports)
            run([cli, 'upload', '-p', args.port, '--fqbn', FQBN, '--input-dir', ROOT/'.build', SKETCH])
            print('OK 固件已上传')
    if not (args.check or args.install or args.compile or args.upload): ap.print_help()

if __name__ == '__main__':
    try: main()
    except Exception as e:
        print('ERR', e, file=sys.stderr); sys.exit(1)
