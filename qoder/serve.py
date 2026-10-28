"""StudyFlow 本地服务：静态站点 + seekdb 卡片接口。

用法：

    STUDYFLOW_DB_PORT=3892 python serve.py            # 默认监听 8765
    STUDYFLOW_DB_PORT=3892 python serve.py 9000

浏览器打开 http://127.0.0.1:8765/ 即可；页面右上角会显示 seekdb 连接状态。
只用标准库的 http.server，唯一第三方依赖是 pymysql。
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import studyflow_seekdb as store

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "dist" if (ROOT / "dist").is_dir() else ROOT


class Handler(BaseHTTPRequestHandler):
    server_version = "StudyFlow/1.0"

    def _send(self, status: int, payload: dict | str, content_type: str = "application/json") -> None:
        if isinstance(payload, str):
            body = payload.encode("utf-8")
        else:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        path = self.path.split("?", 1)[0]
        if path == "/api/health":
            self._send(200, store.health())
            return
        if path == "/api/cards":
            try:
                self._send(200, {"ok": True, "cards": store.list_cards()})
            except Exception as error:  # noqa: BLE001
                self._send(500, {"ok": False, "error": f"{type(error).__name__}: {error}"})
            return
        if path == "/api/focus":
            try:
                self._send(200, {"ok": True, "minutes": store.focus_total_minutes()})
            except Exception as error:  # noqa: BLE001
                self._send(500, {"ok": False, "error": f"{type(error).__name__}: {error}"})
            return
        target = STATIC / ("index.html" if path in ("/", "") else path.lstrip("/"))
        if target.is_file():
            suffix = target.suffix.lower()
            mime = {
                ".html": "text/html",
                ".js": "text/javascript",
                ".css": "text/css",
                ".json": "application/json",
                ".svg": "image/svg+xml",
            }.get(suffix, "application/octet-stream")
            self._send(200, target.read_text(encoding="utf-8"), mime)
            return
        self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._send(400, {"ok": False, "error": "invalid json"})
            return
        try:
            if path == "/api/cards":
                card_id = store.add_card(payload)
                self._send(200, {"ok": True, "id": card_id})
                return
            if path == "/api/focus":
                session_id = store.add_focus_session(
                    int(payload.get("minutes", 25)), str(payload.get("label", ""))
                )
                self._send(200, {"ok": True, "id": session_id})
                return
        except Exception as error:  # noqa: BLE001
            self._send(500, {"ok": False, "error": f"{type(error).__name__}: {error}"})
            return
        self._send(404, {"ok": False, "error": "not found"})

    def log_message(self, fmt: str, *args) -> None:  # silence per-request noise
        return


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("STUDYFLOW_PORT", "8765"))
    store.init_schema()
    status = store.health()
    print(f"seekdb: {status}")
    print(f"StudyFlow serving {STATIC} on http://127.0.0.1:{port}/")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
