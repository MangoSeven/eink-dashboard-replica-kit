#!/usr/bin/env python3
"""单实例局域网看板服务。只重启本程序的子进程，不终止占用端口的其他程序。"""
import argparse, os, signal, socket, subprocess, sys, time
from pathlib import Path

class InstanceLock:
    def __init__(self,path):self.path=Path(path);self.handle=None
    def acquire(self):
        self.path.parent.mkdir(parents=True,exist_ok=True);self.handle=open(self.path,'a+')
        try:
            if os.name=='nt':
                import msvcrt
                self.handle.seek(0);self.handle.write(' ');self.handle.flush();self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            return True
        except (OSError,IOError):self.handle.close();self.handle=None;return False
    def close(self):
        if self.handle:self.handle.close();self.handle=None

def ensure_python(runtime):
    venv=runtime/'.venv';py=venv/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
    if not py.exists():subprocess.run([sys.executable,'-m','venv',str(venv)],check=True)
    check=subprocess.run([str(py),'-c','import PIL'],capture_output=True)
    if check.returncode:subprocess.run([str(py),'-m','pip','install','-r',str(runtime/'requirements.txt')],check=True)
    return py

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--root',default=str(Path(__file__).resolve().parent));ap.add_argument('--http-port',type=int,default=1234);ap.add_argument('--udp-port',type=int,default=18999);a=ap.parse_args()
    root=Path(a.root).resolve();runtime=root/'dashboard-runtime';state=runtime/'.runtime'
    lock=InstanceLock(state/'service.lock')
    if not lock.acquire():print('看板服务已在运行，无需重复启动。');return 0
    processes={};stop=False;log=None
    def stopping(*_):
        nonlocal stop;stop=True
    signal.signal(signal.SIGTERM,stopping);signal.signal(signal.SIGINT,stopping)
    try:
        # Detect conflicts before starting an updater or installing dependencies.
        probe=socket.socket()
        try:probe.bind(('0.0.0.0',a.http_port))
        except OSError:print(f'端口 {a.http_port} 已被占用，请先确认原有服务；未启动重复采集器。');return 1
        finally:probe.close()
        py=ensure_python(runtime)
        state.mkdir(exist_ok=True,mode=0o700)
        os.chmod(state,0o700)
        log_path=state/'service.log'
        if log_path.exists() and log_path.stat().st_size>2_000_000:log_path.replace(state/'service.previous.log')
        log=open(log_path,'ab',buffering=0)
        commands={'http':[py,root/'epaper_agent.py','--home',runtime/'home/out','--stock',runtime/'stock/out','--reading',runtime/'reading/out','--http-port',a.http_port,'--udp-port',a.udp_port],
                  'updater':[py,runtime/'dashboard_updater.py']}
        due={name:0 for name in commands};failures={name:0 for name in commands};started={}
        print(f'看板服务启动：局域网 HTTP {a.http_port} / UDP {a.udp_port}；日志位于 dashboard-runtime/.runtime/service.log',flush=True)
        while not stop:
            for name,cmd in commands.items():
                child=processes.get(name)
                if child and child.poll() is not None:
                    if time.monotonic()-started[name]>300:failures[name]=0
                    failures[name]+=1;due[name]=time.monotonic()+min(60,2**min(failures[name],6));del processes[name]
                    print(name+' 子进程退出，将退避重启。',flush=True)
                elif not child and time.monotonic()>=due[name]:
                    processes[name]=subprocess.Popen([str(x) for x in cmd],stdout=log,stderr=log,start_new_session=os.name!='nt')
                    started[name]=time.monotonic()
            # Bound active logs as well, without rotating away an open descriptor.
            if log_path.stat().st_size>4_000_000:
                log.seek(0);log.truncate(0)
            time.sleep(.5)
        return 0
    finally:
        for child in processes.values():
            if child.poll() is None:
                if os.name!='nt':os.killpg(child.pid,signal.SIGTERM)
                else:child.terminate()
        for child in processes.values():
            try:child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if os.name!='nt':os.killpg(child.pid,signal.SIGKILL)
                else:child.kill()
        if log:log.close()
        lock.close()
if __name__=='__main__':raise SystemExit(main())
