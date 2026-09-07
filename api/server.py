#!/usr/bin/env python3
"""Veriton HTML→JSON metered API skeleton.

Modes:
- free demo: POST /v1/html-json with body under FREE_MAX bytes, no auth
- prepaid: header X-Veriton-Key matching keys in VERITON_API_KEYS (comma-separated)
- unpaid paid-size: HTTP 402 + payment instructions (x402-shaped JSON; facilitator settle TBD)

Pay-to (USDC/EVM): 0xa75Cc8545B169F0BeF2f29c9CCF86bc686D039E8
Price: $0.02 USDC per call over free tier (exact scheme intent).
"""
from __future__ import annotations

import json
import os
import secrets
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from extract import extract_html

PAY_TO = os.environ.get("VERITON_PAY_TO", "0xa75Cc8545B169F0BeF2f29c9CCF86bc686D039E8")
PRICE_USDC = float(os.environ.get("VERITON_PRICE_USDC", "0.02"))
FREE_MAX = int(os.environ.get("VERITON_FREE_MAX", "4096"))
KEYS = {k.strip() for k in os.environ.get("VERITON_API_KEYS", "").split(",") if k.strip()}
PORT = int(os.environ.get("PORT", "8787"))
HOST = os.environ.get("HOST", "0.0.0.0")


def payment_required(resource: str) -> dict:
    # x402-inspired exact scheme advertisement (settlement facilitator not wired yet)
    amount_atomic = str(int(PRICE_USDC * 1_000_000))  # USDC 6 decimals
    return {
        "x402Version": 1,
        "error": "payment_required",
        "accepts": [
            {
                "scheme": "exact",
                "network": "eip155:1",
                "maxAmountRequired": amount_atomic,
                "resource": resource,
                "description": "HTML→JSON extract (one call)",
                "mimeType": "application/json",
                "payTo": PAY_TO,
                "maxTimeoutSeconds": 120,
                "asset": "USDC",
                "extra": {
                    "name": "USDC",
                    "price_usdc": PRICE_USDC,
                    "prepaid": (
                        "Send USDC to payTo, email tx hash to acer-openclaw@agentmail.to "
                        "subject [Veriton API key], receive X-Veriton-Key"
                    ),
                },
            }
        ],
    }


class H(BaseHTTPRequestHandler):
    server_version = "VeritonHTMLJSON/0.1"

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Veriton-Key, PAYMENT-SIGNATURE")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _json(self, code: int, obj: dict, extra_headers: dict | None = None):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/health"):
            return self._json(200, {
                "ok": True,
                "service": "veriton-html-json",
                "price_usdc": PRICE_USDC,
                "pay_to": PAY_TO,
                "free_max_bytes": FREE_MAX,
                "openapi": "/openapi.json",
                "docs": "https://veriton-dev.github.io/veriton-micro-dev/api/",
            })
        if path == "/openapi.json":
            try:
                raw = open(os.path.join(os.path.dirname(__file__), "openapi.json"), "rb").read()
            except FileNotFoundError:
                return self._json(404, {"error": "openapi_missing"})
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if path == "/v1/price":
            return self._json(200, payment_required("POST /v1/html-json"))
        return self._json(404, {"error": "not_found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/v1/html-json":
            return self._json(404, {"error": "not_found"})
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return self._json(400, {"error": "invalid_json"})
        html = data.get("html") or ""
        if not isinstance(html, str) or not html.strip():
            return self._json(400, {"error": "html_required"})
        key = self.headers.get("X-Veriton-Key") or ""
        paid = bool(key and key in KEYS)
        if len(html.encode("utf-8")) > FREE_MAX and not paid:
            pr = payment_required("POST /v1/html-json")
            return self._json(402, pr, extra_headers={
                "Payment-Required": "true",
                "X-Veriton-Price-USDC": str(PRICE_USDC),
                "X-Veriton-Pay-To": PAY_TO,
            })
        t0 = time.time()
        out = extract_html(html, base_url=data.get("base_url"), limit=int(data.get("limit") or 50))
        out["meter"] = {
            "paid": paid,
            "bytes": len(html.encode("utf-8")),
            "free_max_bytes": FREE_MAX,
            "latency_ms": int((time.time() - t0) * 1000),
            "request_id": secrets.token_hex(8),
        }
        return self._json(200, out)

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))


def main():
    httpd = ThreadingHTTPServer((HOST, PORT), H)
    print(f"veriton html-json api on http://{HOST}:{PORT} free_max={FREE_MAX} keys={len(KEYS)}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
