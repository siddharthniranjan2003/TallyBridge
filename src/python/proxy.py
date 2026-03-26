from http.server import HTTPServer, BaseHTTPRequestHandler
import requests

TALLY_URL = "http://localhost:9000"
PROXY_PORT = 9001  # BizAnalyst will point here temporarily

class ProxyHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        print("\n" + "="*60)
        print("REQUEST FROM BIZANALYST:")
        print("="*60)
        print(body.decode("utf-8", errors="ignore"))

        # Forward to real Tally
        resp = requests.post(
            TALLY_URL,
            data=body,
            headers={"Content-Type": self.headers.get("Content-Type", "text/xml")},
            timeout=10,
        )

        print("\nRESPONSE FROM TALLY:")
        print("="*60)
        print(resp.text)

        # Send back to BizAnalyst
        self.send_response(resp.status_code)
        self.send_header("Content-Type", "text/xml")
        self.end_headers()
        self.wfile.write(resp.content)

    def log_message(self, format, *args):
        pass  # suppress default logs

print(f"Proxy running on port {PROXY_PORT} → forwarding to {TALLY_URL}")
HTTPServer(("localhost", PROXY_PORT), ProxyHandler).serve_forever()