"""Mock TallyPrime gateway for SAFE live testing.

Reproduces the abnormal port-9000 behaviours the prod-readiness fixes target —
WITHOUT touching a real client's TallyPrime. Point the engine/app at it with
TALLY_URL=http://127.0.0.1:9009 (a non-9000 port, so it never clashes with a
real Tally and there is zero risk of hitting real data).

Standalone (for app-level tests, e.g. the push-worker watchdog):
    py -3 tests/mock_tally.py hang 9009      # accepts the connection, never replies
    py -3 tests/mock_tally.py html 9009      # returns a license HTML page
    py -3 tests/mock_tally.py normal 9009     # returns valid-ish XML / CREATED on push

Importable (for in-process live tests): start_mock(mode) -> (httpd, port).

Modes:
  normal    valid envelope on read; CREATED=1/ERRORS=0 on an Import (push)
  html      an HTML page (degraded Tally: license/activation dialog)
  empty     empty body
  status0   <STATUS>0</STATUS> envelope (company not found)
  hang      accept the socket, sleep ~600s (never reply within a test window)
  pusherror on an Import, reply with ERRORS/LINEERROR (push rejected)
"""

import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VALID_READ = (
    "<ENVELOPE><BODY><DATA><COLLECTION>"
    "<LEDGER NAME='Cash'><PARENT>Cash-in-Hand</PARENT></LEDGER>"
    "</COLLECTION></DATA></BODY></ENVELOPE>"
)
HTML_PAGE = "<!DOCTYPE html><html><body>TallyPrime license has expired</body></html>"
STATUS0 = "<ENVELOPE><STATUS>0</STATUS><DATA>Could not find company</DATA></ENVELOPE>"
PUSH_OK = (
    "<ENVELOPE><HEADER><STATUS>1</STATUS></HEADER><BODY><DATA>"
    "<CREATED>1</CREATED><ALTERED>0</ALTERED><ERRORS>0</ERRORS><EXCEPTIONS>0</EXCEPTIONS>"
    "</DATA></BODY></ENVELOPE>"
)
PUSH_ERR = (
    "<ENVELOPE><BODY><DATA><CREATED>0</CREATED><ALTERED>0</ALTERED>"
    "<ERRORS>1</ERRORS><EXCEPTIONS>0</EXCEPTIONS>"
    "<LINEERROR>Ledger 'Foo' does not exist</LINEERROR>"
    "</DATA></BODY></ENVELOPE>"
)


def _make_handler(mode: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):  # silence per-request logging
            pass

        def _reply(self, body: str):
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/xml; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length).decode("utf-8", "ignore") if length else ""
            is_push = "Import" in body  # <TALLYREQUEST>Import</TALLYREQUEST>

            if mode == "hang":
                time.sleep(600)
                return
            if mode == "html":
                return self._reply(HTML_PAGE)
            if mode == "empty":
                return self._reply("")
            if mode == "status0":
                return self._reply(STATUS0)
            if is_push:
                return self._reply(PUSH_ERR if mode == "pusherror" else PUSH_OK)
            return self._reply(VALID_READ)

        def do_GET(self):
            self._reply(VALID_READ)

    return Handler


def start_mock(mode: str = "normal", port: int = 0):
    """Start the mock in a background thread. port=0 picks a free port."""
    import threading

    httpd = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(mode))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "normal"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 9009
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(mode))
    print(f"[mock-tally] mode={mode} listening on http://127.0.0.1:{port}  (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[mock-tally] stopped")
