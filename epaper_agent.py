#!/usr/bin/env python3
"""局域网水墨屏服务：UDP 自动发现 + 三页 800x480/1-bit 位图分发。"""
from __future__ import annotations
import argparse, hashlib, os, socket, threading, json, time, csv
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGES = {"1": "home.bin", "2": "screen.bin", "3": "reading.bin"}

class Agent:
    def __init__(self, args):
        self.args = args
        self.telemetry_lock = threading.Lock()
        self.last_telemetry = 0
    def page_path(self, page: str) -> str:
        name = PAGES[page]
        root = self.args.home if page == "1" else self.args.stock if page == "2" else self.args.reading
        return os.path.join(root, name)
    def record_telemetry(self, headers):
        limits={"X-Battery-Soc":(0,100),"X-Battery-Mv":(2500,4600),"X-Power-Flags":(0,255),"X-Charge-Flags":(0,255)}
        values={}
        for name,(lo,hi) in limits.items():
            try:value=int(headers.get(name,""))
            except (ValueError,TypeError):continue
            if lo<=value<=hi:values[name]=value
        if not values:return
        with self.telemetry_lock:
            if self.last_telemetry and time.monotonic()-self.last_telemetry<60:return
            root=os.path.dirname(os.path.dirname(self.args.home))
            directory=os.path.join(root,".runtime");os.makedirs(directory,exist_ok=True,mode=0o700)
            path=os.path.join(directory,"battery.csv")
            if os.path.exists(path) and os.path.getsize(path)>2_000_000:os.replace(path,os.path.join(directory,"battery.previous.csv"))
            exists=os.path.exists(path)
            with open(path,"a",encoding="utf-8",newline="") as f:
                writer=csv.writer(f)
                if not exists:writer.writerow(["received_at","soc_raw","vbat_mv","power_flags","charge_flags"])
                writer.writerow([datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")]+[values.get(k,"") for k in limits])
            self.last_telemetry=time.monotonic()
    def page_headers(self, page):
        root=os.path.dirname(self.page_path(page))
        name={"1":"home_meta.json","2":"meta.json","3":"reading_meta.json"}[page]
        try:
            with open(os.path.join(root,name),encoding="utf-8") as f:meta=json.load(f)
        except (OSError,ValueError):meta={}
        result={}
        if page=="2":
            status=meta.get("data_status") or {};stamp=meta.get("data_fetched_at")
            try:
                dt=datetime.fromisoformat(str(stamp))
                if dt.tzinfo is None:dt=dt.replace(tzinfo=timezone(timedelta(hours=8)))
                age=time.time()-dt.timestamp()
            except (TypeError,ValueError):age=999999
            result["X-Data-Stale"]="1" if status.get("state")!="fresh" or age>180 or age < -60 else "0"
            rect=meta.get("status_rect")
            if isinstance(rect,list) and len(rect)==4 and all(isinstance(v,int) for v in rect):result["X-Status-Rect"]=",".join(map(str,rect))
        return result
    def serve(self):
        agent = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                parts = self.path.split("?", 1)[0].strip("/").split("/")
                if self.path.split("?", 1)[0] == "/health":
                    self.send_response(200); self.end_headers(); self.wfile.write(b"ok\n"); return
                if self.path.split("?",1)[0] == "/status":
                    path=os.path.join(os.path.dirname(os.path.dirname(agent.args.home)), ".runtime", "status.json")
                    try:
                        with open(path,encoding="utf-8") as f:status=json.load(f)
                    except (OSError,ValueError):status={}
                    # Only timestamps and boolean health flags; no balances, credentials or local paths.
                    safe={k:{field:v[field] for field in ("ok","attempted_at","last_success_at") if field in v} for k,v in status.items() if isinstance(v,dict)}
                    data=json.dumps(safe,ensure_ascii=False).encode("utf-8")
                    self.send_response(200);self.send_header("Content-Type","application/json; charset=utf-8");self.send_header("Content-Length",str(len(data)));self.end_headers();self.wfile.write(data);return
                if len(parts) != 4 or parts[:3] != ["api", "display", "page"] or parts[3] not in PAGES:
                    self.send_error(404); return
                try:agent.record_telemetry(self.headers)
                except OSError:pass # Logging failure must never prevent displaying a valid page.
                path = agent.page_path(parts[3])
                if parts[3] == '2' and os.path.exists(os.path.join(agent.args.stock, '.requires-live-data')):
                    self.send_error(503, 'stock data is not ready'); return
                try:
                    with open(path, "rb") as f: data = f.read()
                except OSError:
                    self.send_error(404, "page image not generated"); return
                if len(data) != 48000:
                    self.send_error(503, "page image must be 48000 bytes"); return
                etag = '"' + hashlib.sha256(data).hexdigest()[:16] + '"'
                data_headers=agent.page_headers(parts[3])
                if self.headers.get("If-None-Match") == etag:
                    self.send_response(304); self.send_header("ETag", etag)
                    for key,value in data_headers.items():self.send_header(key,value)
                    self.end_headers(); return
                self.send_response(200); self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data))); self.send_header("ETag", etag)
                self.send_header("X-Page-Version", etag.strip('"'))
                for key,value in data_headers.items():self.send_header(key,value)
                self.end_headers(); self.wfile.write(data)
        server = ThreadingHTTPServer(("0.0.0.0", self.args.http_port), Handler)
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:self.udp.bind(("", self.args.udp_port))
        except OSError:server.server_close();raise
        threading.Thread(target=self.discovery, daemon=True).start()
        print(f"HTTP :{self.args.http_port}; UDP :{self.args.udp_port}; pages: /api/display/page/1..3")
        server.serve_forever()
    def discovery(self):
        udp = self.udp
        while True:
            data, addr = udp.recvfrom(512)
            if data.strip() == b"EPAPER_DISCOVER":
                udp.sendto(f"EPAPER_AGENT 1 {self.args.http_port}".encode(), addr)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True, help="含 home.bin 的目录")
    ap.add_argument("--stock", required=True, help="含 screen.bin 的目录")
    ap.add_argument("--reading", required=True, help="含 reading.bin 的目录")
    ap.add_argument("--http-port", type=int, default=1234); ap.add_argument("--udp-port", type=int, default=18999)
    Agent(ap.parse_args()).serve()
