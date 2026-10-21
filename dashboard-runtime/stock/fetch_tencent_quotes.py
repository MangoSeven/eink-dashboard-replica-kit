#!/usr/bin/env python3
"""无需 westock 的腾讯行情采集器，输出 render_screen.py 所需 latest.json。"""
import argparse, json, os, re, sys, tempfile, sqlite3
from datetime import datetime
from urllib.request import Request, urlopen

def market_session(now):
    """以中国标准交易时段标注看板状态。"""
    if now.weekday() >= 5:
        return "休市", False
    minute = now.hour * 60 + now.minute
    if minute < 9 * 60 + 30:
        return "盘前", False
    if minute < 11 * 60 + 30:
        return "交易中", True
    if minute < 13 * 60:
        return "午间休市", False
    if minute < 15 * 60:
        return "交易中", True
    return "已收盘", False


def fetch_intraday(code):
    """读取腾讯当日真实分时序列；失败时返回空，绝不补造曲线。"""
    url = 'https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=' + code
    raw = urlopen(Request(url, headers={'Referer':'https://gu.qq.com/','User-Agent':'Mozilla/5.0'}), timeout=12).read()
    payload = json.loads(raw.decode('utf-8', 'replace'))
    node = (payload.get('data') or {}).get(code) or {}
    outer = node.get('data') or node
    day = str(outer.get('date') or '')
    minute = outer.get('minute') or outer.get('data') or []
    if len(day) == 8 and day.isdigit():
        day = f'{day[:4]}-{day[4:6]}-{day[6:]}'
    if not day or not isinstance(minute, list):
        return None, []
    points = []
    for row in minute:
        parts = str(row).split()
        if len(parts) < 2:
            continue
        clock = parts[0]
        if len(clock) == 4 and clock.isdigit():
            clock = clock[:2] + ':' + clock[2:]
        if ':' not in clock:
            continue
        try:
            price = float(parts[1])
        except ValueError:
            continue
        points.append((f'{day} {clock}:00', price))
    return day, points


def save_intraday(runtime, codes):
    """一次获取每个关注标的的真实日内分时，并原子替换当天该标的序列。"""
    db = os.path.join(runtime, 'history.sqlite')
    con = sqlite3.connect(db)
    con.execute('CREATE TABLE IF NOT EXISTS ticks (day TEXT, code TEXT, ts TEXT, price REAL)')
    saved = 0
    latest_day = None
    for code in codes:
        try:
            day, points = fetch_intraday(code)
        except Exception:
            continue
        if not day or not points:
            continue
        con.execute('DELETE FROM ticks WHERE day=? AND code=?', (day, code))
        con.executemany('INSERT INTO ticks(day, code, ts, price) VALUES (?, ?, ?, ?)',
                        [(day, code, ts, price) for ts, price in points])
        latest_day = day
        saved += 1
    con.commit()
    con.close()
    return saved, latest_day


def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--config',default='watchlist.json'); ap.add_argument('--runtime',default='.')
    a=ap.parse_args()
    with open(a.config,encoding='utf-8') as f: watch=json.load(f)
    items=watch['items']; bench=watch.get('benchmark') or {}
    codes=[x['code'] for x in items]
    if bench.get('code'): codes.insert(0,bench['code'])
    url='https://qt.gtimg.cn/q='+','.join(codes)
    raw=urlopen(Request(url,headers={'Referer':'https://gu.qq.com/','User-Agent':'Mozilla/5.0'}),timeout=12).read().decode('gb18030','replace')
    got={}
    for code, body in re.findall(r'v_([^=]+)="([^"]*)";',raw):
        f=body.split('~')
        if len(f)<33: continue
        def number(i):
            try:return float(f[i])
            except:return None
        price,prev=number(3),number(4)
        got[code]={'code':code,'name':f[1] or code,'price':price,'prev_close':prev,
          'change_percent':number(32),'volume_ratio':number(49) if len(f)>49 else None}
    quotes={}
    for item in items:
        q=got.get(item['code'])
        if q:
            q['name']=item.get('alias') or q['name']; q['currency']=item.get('currency',''); quotes[item['code']]=q
    if bench.get('code') in got: quotes[bench['code']]=got[bench['code']]
    now = datetime.now()
    market_state, trading = market_session(now)
    out={'fetched_at':now.isoformat(timespec='seconds'),'fetched_date':now.strftime('%Y-%m-%d'),
         'market_state':market_state,'trading':trading,'benchmark_code':bench.get('code'),'watchlist':watch,'quotes':quotes}
    expected = {x['code'] for x in items}
    missing = expected - set(quotes)
    # 禁止半份数据或模拟数据覆盖已生成的真实股票页。
    if missing:
        raise RuntimeError('行情不完整，未覆盖现有页面：' + ', '.join(sorted(missing)))
    os.makedirs(a.runtime,exist_ok=True); path=os.path.join(a.runtime,'latest.json')
    # 分时图使用同一数据源的当日真实 minute 序列；失败只是不更新曲线，绝不生成模拟走势。
    intraday_codes = [x['code'] for x in items]
    intraday_count, intraday_day = save_intraday(a.runtime, intraday_codes)
    if intraday_day:
        out['intraday_date'] = intraday_day
    fd,tmp=tempfile.mkstemp(dir=a.runtime); os.close(fd)
    with open(tmp,'w',encoding='utf-8') as f: json.dump(out,f,ensure_ascii=False)
    os.replace(tmp,path)
    unavailable = os.path.join(a.runtime, 'out', '.requires-live-data')
    try: os.unlink(unavailable)
    except FileNotFoundError: pass
    print('OK',path,len(quotes),'intraday',intraday_count)
if __name__=='__main__':
    try:
        main()
    except Exception as e:
        print('ERR', e, file=sys.stderr)
        sys.exit(1)
