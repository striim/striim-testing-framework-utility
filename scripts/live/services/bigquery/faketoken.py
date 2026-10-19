"""Local-only OAuth test endpoint; never validates or logs a supplied JWT."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BODY = json.dumps({"access_token": "local-bigquery-token", "token_type": "Bearer",
                   "expires_in": 3600}).encode()


class Handler(BaseHTTPRequestHandler):
    def respond(self):
        if self.path != "/token":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length > 65536:
            self.send_error(413)
            return
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()
        self.wfile.write(BODY)

    do_GET = respond
    do_POST = respond

    def log_message(self, *_args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 9051), Handler).serve_forever()
