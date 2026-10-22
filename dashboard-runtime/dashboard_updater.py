#!/usr/bin/env python3
"""独立调度三页任务：失败保留数据、重渲染缓存状态并退避，读书跨日重试。"""
import argparse, concurrent.futures, os, signal, subprocess, sys, threading, time
from pathlib import Path
from datetime import timedelta
from data_status import atomic_json, load, now
ROOT=Path(__file__).resolve().parent
STOP=threading.Event(); STATE_LOCK=threading.Lock()

def run(args, timeout=90):
    try:
        # 仅在本机日志保留子程序输出；日志与采集数据不进入复刻分发。
        result=subprocess.run([sys.executable]+[str(x) for x in args],timeout=timeout)
        return result.returncode==0
    except (OSError,subprocess.TimeoutExpired):
        print('WARN 子任务执行失败或超时',flush=True);return False

def record(name,ok):
    with STATE_LOCK:
        path=ROOT/'.runtime'/'status.json';doc=load(path)
        item=doc.get(name,{})
        item.update(ok=ok,attempted_at=now().isoformat(timespec='seconds'))
        if ok:item['last_success_at']=item['attempted_at']
        doc[name]=item;atomic_json(path,doc)
    return ok

def home_job():
    results=[]
    for name,script in [('codex','fetch_codex.py'),('deepseek','fetch_deepseek_balance.py')]:
        ok=run([ROOT/'home'/script,'--runtime',ROOT/'home'],timeout=65)
        record(name,ok);results.append(ok)
    rendered=run([ROOT/'home/render_home.py','--runtime',ROOT/'home','--config',ROOT/'home','--device-base'],timeout=30)
    record('home',rendered)
    return rendered and all(results)

def stock_job():
    ok=run([ROOT/'stock/fetch_tencent_quotes.py','--config',ROOT/'stock/watchlist.json','--runtime',ROOT/'stock'],timeout=120)
    record('stock',ok)
    # Failed fetching must still mark the last valid page as cached; never fabricate prices.
    rendered=run([ROOT/'stock/render_screen.py','--config',ROOT/'stock','--runtime',ROOT/'stock'],timeout=30) if (ROOT/'stock/latest.json').exists() else False
    record('stock_render',rendered)
    return ok and rendered

def reading_job():
    ok=run([ROOT/'reading/render_reading.py','--runtime',ROOT/'reading'],timeout=30)
    return record('reading',ok)

def retry_delay(interval, failures):return min(1800, interval*(2**min(failures,5)))
def until_next_day(current):
    tomorrow=(current+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)
    return max(1,(tomorrow-current).total_seconds())

def main():
    jobs={'home':(home_job,300),'stock':(stock_job,60),'reading':(reading_job,60)}
    due={name:0.0 for name in jobs};failures={name:0 for name in jobs};running={}
    signal.signal(signal.SIGTERM,lambda *_:STOP.set());signal.signal(signal.SIGINT,lambda *_:STOP.set())
    pool=concurrent.futures.ThreadPoolExecutor(max_workers=3)
    try:
        while not STOP.is_set():
            tick=time.monotonic()
            for name,(fn,interval) in jobs.items():
                future=running.get(name)
                if future and future.done():
                    try:ok=future.result()
                    except Exception:ok=False;record(name,False)
                    failures[name]=0 if ok else failures[name]+1
                    delay=until_next_day(now()) if name=='reading' and ok else (interval if ok else retry_delay(interval,failures[name]))
                    due[name]=tick+delay;del running[name]
                elif not future and tick>=due[name]:running[name]=pool.submit(fn)
            STOP.wait(1)
    finally:
        pool.shutdown(wait=True)
if __name__=='__main__':main()
