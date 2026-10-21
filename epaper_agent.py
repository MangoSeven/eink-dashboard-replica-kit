#!/usr/bin/env python3
"""局域网水墨屏服务：UDP 自动发现 + 三页 800x480/1-bit 位图分发。"""
from __future__ import annotations
import argparse, hashlib, os, socket, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGES = {"1": "home.bin", "2": "screen.bin", "3": "reading.bin"}

class Agent:
    def __init__(self, args):
        self.args = args
    def page_path(self, page: str) -> str:
        name = PAGES[page]
        root = self.args.home if page == "1" else self.args.stock if page == "2" else self.args.reading
        return os.path.join(root, name)
    def serve(self):
        agent = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                parts = self.path.split("?", 1)[0].strip("/").split("/")
                if self.path.split("?", 1)[0] == "/health":
                    self.send_response(200); self.end_headers(); self.wfile.write(b"ok\n"); return
                if len(parts) != 4 or parts[:3] != ["api", "display", "page"] or parts[3] not in PAGES:
                    self.send_error(404); return
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
                if self.headers.get("If-None-Match") == etag:
                    self.send_response(304); self.send_header("ETag", etag); self.end_headers(); return
                self.send_response(200); self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data))); self.send_header("ETag", etag)
                self.send_header("X-Page-Version", etag.strip('"')); self.end_headers(); self.wfile.write(data)
        server = ThreadingHTTPServer(("0.0.0.0", self.args.http_port), Handler)
        threading.Thread(target=self.discovery, daemon=True).start()
        print(f"HTTP :{self.args.http_port}; UDP :{self.args.udp_port}; pages: /api/display/page/1..3")
        server.serve_forever()
    def discovery(self):
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        udp.bind(("", self.args.udp_port))
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
