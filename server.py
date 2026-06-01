"""
本地開發伺服器
  python server.py        # 預設 port 8080
  python server.py 3000   # 指定 port
  GET /api/refresh        → 執行 fetch_courts.py 重新抓取資料
"""

import sys, os, subprocess, threading, webbrowser
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
ROOT = Path(__file__).parent


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self):
        if self.path == "/api/refresh":
            self._handle_refresh()
        else:
            super().do_GET()

    def _handle_refresh(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write("[server] 開始抓取資料...\n".encode("utf-8"))
        self.wfile.flush()
        try:
            proc = subprocess.Popen(
                [sys.executable, str(ROOT / "fetch_courts.py")],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=str(ROOT),
            )
            for line in proc.stdout:
                self.wfile.write(line)
                self.wfile.flush()
            proc.wait()
            status = "完成" if proc.returncode == 0 else f"失敗（exit {proc.returncode}）"
            self.wfile.write(f"[server] {status}\n".encode("utf-8"))
        except Exception as e:
            self.wfile.write(f"[server] 錯誤：{e}\n".encode("utf-8"))

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        if args and (str(args[1]).startswith(("4", "5")) or "/api/" in str(args[0])):
            super().log_message(fmt, *args)


def open_browser():
    import time; time.sleep(0.8)
    webbrowser.open(f"http://localhost:{PORT}")


if __name__ == "__main__":
    os.chdir(ROOT)
    print(f"  http://localhost:{PORT}")
    print(f"  http://localhost:{PORT}/api/refresh\n")
    threading.Thread(target=open_browser, daemon=True).start()
    with HTTPServer(("", PORT), Handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
