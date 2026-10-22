#!/usr/bin/env python3
"""macOS 用户级登录自启；只有用户明确执行 --install 才会修改系统设置。"""
import argparse, os, plistlib, subprocess, sys
from pathlib import Path
LABEL='local.eink.dashboard'
def document(root):
    return {'Label':LABEL,'ProgramArguments':[sys.executable,str(root/'service_supervisor.py'),'--root',str(root)],
            'WorkingDirectory':str(root),'RunAtLoad':True,'KeepAlive':{'SuccessfulExit':False},'ThrottleInterval':30,
            'StandardOutPath':str(root/'dashboard-runtime/.runtime/autostart.log'),
            'StandardErrorPath':str(root/'dashboard-runtime/.runtime/autostart-error.log')}
def main():
    ap=argparse.ArgumentParser();g=ap.add_mutually_exclusive_group(required=True);g.add_argument('--print',action='store_true');g.add_argument('--install',action='store_true');g.add_argument('--uninstall',action='store_true');a=ap.parse_args()
    if sys.platform!='darwin':raise SystemExit('当前脚本仅支持 macOS；其他平台请由接手 AI 配置本用户的启动项，不使用管理员权限。')
    root=Path(__file__).resolve().parents[1];path=Path.home()/'Library/LaunchAgents'/f'{LABEL}.plist';domain=f'gui/{os.getuid()}'
    if a.print:print(plistlib.dumps(document(root)).decode());return
    if a.uninstall:
        if path.exists():
            subprocess.run(['launchctl','bootout',domain,str(path)],check=False,capture_output=True);path.unlink()
        print('已移除本看板的用户级自启。');return
    # Bootstrapping dependencies and testing manual start should happen before installation.
    if not (root/'dashboard-runtime/.venv/bin/python').is_file():raise SystemExit('请先手动运行看板服务，确认依赖安装和服务正常，再安装自启。')
    (root/'dashboard-runtime/.runtime').mkdir(exist_ok=True)
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():subprocess.run(['launchctl','bootout',domain,str(path)],check=False,capture_output=True)
    path.write_bytes(plistlib.dumps(document(root)))
    subprocess.run(['launchctl','bootstrap',domain,str(path)],check=True)
    print('已设置本用户登录后启动；电脑睡眠期间不能继续采集。')
if __name__=='__main__':main()
