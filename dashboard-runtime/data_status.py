"""真实采集时点与缓存状态；不把渲染时间当作采集时间。"""
import json, math
from datetime import datetime, timezone, timedelta
from pathlib import Path
CHINA = timezone(timedelta(hours=8))
def now(): return datetime.now(CHINA)
def timestamp(value):
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return dt.replace(tzinfo=CHINA) if dt.tzinfo is None else dt
    except (ValueError, TypeError): return None

def load(path):
    try: return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError): return {}

def atomic_json(path, value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    import tempfile, os
    fd,tmp=tempfile.mkstemp(dir=str(path.parent),prefix='.'+path.name)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:json.dump(value,f,ensure_ascii=False,indent=2)
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

def finite_number(value):
    try: return value is not None and not isinstance(value,bool) and math.isfinite(float(value))
    except (ValueError,TypeError): return False

def source_status(data, attempt=None, current=None, max_age=600, configured=True, value_ok=True):
    current=current or now();attempt=attempt or {};dt=timestamp(data.get('ts') or data.get('fetched_at'))
    if current.tzinfo is None:current=current.replace(tzinfo=CHINA)
    if not configured:return {'state':'unconfigured','label':'未配置','fetched_at':None}
    if not dt or not value_ok:return {'state':'unavailable','label':'未获取','fetched_at':None}
    age=(current-dt).total_seconds()
    # A later failed attempt marks old values as cache even before age expiry.
    attempted=timestamp(attempt.get('attempted_at'))
    failed=attempt.get('ok') is False and attempted is not None and attempted>=dt
    stale=age < -60 or age>max_age or failed or bool(data.get('stale'))
    stamp=dt.astimezone(CHINA).strftime('%H:%M' if dt.astimezone(CHINA).date()==current.astimezone(CHINA).date() else '%m/%d')
    return {'state':'cached' if stale else 'fresh','label':('缓存 ' if stale else '更新 ')+stamp,'fetched_at':dt.isoformat()}
