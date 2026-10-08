#!/usr/bin/env python3
"""Minimal fake OAuth2 token endpoint for the GCS-emulator tests.

Striim's GCSReader/GCSWriter always resolve real credentials (a service-account key
-> JWT-bearer grant POST to the key's token_uri), even against the emulator. The
emulator (fake-gcs-server) serves storage with NO auth but has no token endpoint, so
the client's token fetch fails on a throwaway key ("Invalid JWT Signature" from real
Google). This server stands in for the token endpoint: it returns a static bearer
token for any request (it never verifies the JWT), and fake-gcs-server then ignores
that token. Point the throwaway key's token_uri here.

Not a security component — it hands out a fixed dummy token to anyone. Test-only.
"""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

_BODY = json.dumps({"access_token": "fake-emulator-token",
                    "token_type": "Bearer", "expires_in": 3600}).encode()

class Handler(BaseHTTPRequestHandler):
    def _respond(self):
        try:
            self.rfile.read(int(self.headers.get("content-length", 0) or 0))
        except Exception:
            pass
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(_BODY)))
        self.end_headers()
        self.wfile.write(_BODY)

    do_POST = _respond
    do_GET = _respond

    def log_message(self, *a):
        pass

if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 4444), Handler).serve_forever()
