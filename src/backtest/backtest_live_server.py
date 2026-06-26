from __future__ import annotations

import argparse
import json
import mimetypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve live backtest charts over local HTML.")
    parser.add_argument("--output-root", required=True, help="Backtest output root directory.")
    parser.add_argument("--prefix", default="live", help="Live checkpoint filename prefix.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--refresh-seconds", type=float, default=4.0)
    return parser


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _html(prefix: str, refresh_seconds: float) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Backtest Live Dashboard</title>
  <style>
    body {{ font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; margin: 20px; background: #f8fafc; color: #0f172a; }}
    .row {{ display: flex; gap: 20px; flex-wrap: wrap; }}
    .card {{ background: white; border: 1px solid #e2e8f0; border-radius: 10px; padding: 14px 16px; min-width: 240px; }}
    .k {{ color: #475569; font-size: 13px; }}
    .v {{ font-size: 20px; font-weight: 600; margin-top: 2px; }}
    .img-wrap {{ margin-top: 16px; background: white; border: 1px solid #e2e8f0; border-radius: 10px; padding: 8px; }}
    img {{ width: 100%; height: auto; display: block; border-radius: 8px; }}
    .muted {{ color: #64748b; font-size: 13px; }}
  </style>
</head>
<body>
  <h2>Backtest Live Dashboard</h2>
  <div class="muted">prefix={prefix} | auto refresh {refresh_seconds:.1f}s</div>

  <div class="row" style="margin-top:12px">
    <div class="card"><div class="k">Progress</div><div class="v" id="progress">--</div></div>
    <div class="card"><div class="k">Session Date</div><div class="v" id="session_date">--</div></div>
    <div class="card"><div class="k">Equity</div><div class="v" id="equity">--</div></div>
    <div class="card"><div class="k">Drawdown</div><div class="v" id="drawdown">--</div></div>
    <div class="card"><div class="k">Updated</div><div class="v" id="updated_at">--</div></div>
  </div>

  <div class="img-wrap">
    <div class="muted">Equity Curve</div>
    <img id="equity_img" src="/file/{prefix}_equity_curve_compare.png" alt="equity curve" />
  </div>
  <div class="img-wrap">
    <div class="muted">Drawdown Curve</div>
    <img id="drawdown_img" src="/file/{prefix}_drawdown_curve_compare.png" alt="drawdown curve" />
  </div>

  <script>
    const refreshMs = {max(1, int(refresh_seconds * 1000))};
    const eq = document.getElementById('equity_img');
    const dd = document.getElementById('drawdown_img');
    const setText = (id, txt) => document.getElementById(id).textContent = txt;

    function fmtNum(v) {{
      if (v === null || v === undefined || Number.isNaN(v)) return '--';
      return Number(v).toLocaleString(undefined, {{ maximumFractionDigits: 2 }});
    }}
    function fmtPct(v) {{
      if (v === null || v === undefined || Number.isNaN(v)) return '--';
      return (100 * Number(v)).toFixed(2) + '%';
    }}

    async function tick() {{
      const ts = Date.now();
      eq.src = '/file/{prefix}_equity_curve_compare.png?t=' + ts;
      dd.src = '/file/{prefix}_drawdown_curve_compare.png?t=' + ts;
      try {{
        const res = await fetch('/status?t=' + ts, {{ cache: 'no-store' }});
        if (!res.ok) return;
        const s = await res.json();
        setText('progress', `${{s.sessions_done ?? '--'}}/${{s.sessions_total ?? '--'}} (${{(s.progress_pct ?? 0).toFixed ? s.progress_pct.toFixed(2) : '--'}}%)`);
        setText('session_date', s.session_date || '--');
        setText('equity', fmtNum(s.equity));
        setText('drawdown', fmtPct(s.drawdown));
        setText('updated_at', s.updated_at || '--');
      }} catch (_e) {{
      }}
    }}
    tick();
    setInterval(tick, refreshMs);
  </script>
</body>
</html>
"""


def main() -> int:
    args = build_parser().parse_args()
    output_root = Path(args.output_root).resolve()
    prefix = str(args.prefix).strip() or "live"
    refresh_seconds = max(1.0, float(args.refresh_seconds))

    if not output_root.exists():
        raise FileNotFoundError(f"Output root not found: {output_root.as_posix()}")

    status_path = output_root / f"{prefix}_status.json"

    class Handler(BaseHTTPRequestHandler):
        def _send_bytes(self, code: int, payload: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            if path in ("/", "/index.html"):
                body = _html(prefix=prefix, refresh_seconds=refresh_seconds).encode("utf-8")
                self._send_bytes(HTTPStatus.OK, body, "text/html; charset=utf-8")
                return
            if path == "/status":
                payload = json.dumps(_read_json(status_path), ensure_ascii=False, indent=2).encode("utf-8")
                self._send_bytes(HTTPStatus.OK, payload, "application/json; charset=utf-8")
                return
            if path.startswith("/file/"):
                rel_name = unquote(path[len("/file/") :]).lstrip("/")
                if not rel_name or ".." in rel_name.replace("\\", "/").split("/"):
                    self._send_bytes(HTTPStatus.BAD_REQUEST, b"invalid path", "text/plain; charset=utf-8")
                    return
                target = (output_root / rel_name).resolve()
                if not str(target).startswith(str(output_root)):
                    self._send_bytes(HTTPStatus.FORBIDDEN, b"forbidden", "text/plain; charset=utf-8")
                    return
                if not target.exists() or not target.is_file():
                    self._send_bytes(HTTPStatus.NOT_FOUND, b"not found", "text/plain; charset=utf-8")
                    return
                mime, _ = mimetypes.guess_type(target.as_posix())
                self._send_bytes(HTTPStatus.OK, target.read_bytes(), mime or "application/octet-stream")
                return
            self._send_bytes(HTTPStatus.NOT_FOUND, b"not found", "text/plain; charset=utf-8")

        def log_message(self, format: str, *args) -> None:  # noqa: A003
            return

    server = ThreadingHTTPServer((str(args.host), int(args.port)), Handler)
    print(
        f"[LiveServer] http://{args.host}:{args.port}  output_root={output_root.as_posix()} prefix={prefix}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
