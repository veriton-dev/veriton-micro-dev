#!/usr/bin/env python3
"""Veriton metered agent API — prepaid credits + x402-shaped 402 responses.

v0 rails:
  - Free: GET /health, GET /, GET /v1/openapi.json, GET /v1/demo/html-to-json (tiny cap)
  - Paid: POST /v1/html-to-json  ($0.02 / call) via prepaid credit token
  - Buy credits: POST /v1/credits/quote → pay USDC → POST /v1/credits/claim with tx hash

No CDP facilitator required for v0. Settlement = on-chain USDC transfer to PAY_TO
verified via public Base RPC, then credit token issued. Agents can poll claim.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

HOST = os.environ.get("VERITON_API_HOST", "0.0.0.0")
PORT = int(os.environ.get("VERITON_API_PORT", "8402"))
PAY_TO = os.environ.get(
    "VERITON_PAY_TO", "0xa75Cc8545B169F0BeF2f29c9CCF86bc686D039E8"
).lower()
# Base mainnet USDC
USDC = os.environ.get(
    "VERITON_USDC", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
).lower()
RPC = os.environ.get("VERITON_RPC", "https://mainnet.base.org")
PRICE_HTML_USD = float(os.environ.get("VERITON_PRICE_HTML", "0.02"))
PRICE_FETCH_USD = float(os.environ.get("VERITON_PRICE_FETCH", "0.05"))
MIN_PACK_USD = float(os.environ.get("VERITON_MIN_PACK", "0.50"))
# credit unit = $0.01
UNIT_CENTS = 1
DATA_DIR = Path(os.environ.get("VERITON_API_DATA", str(Path(__file__).resolve().parent / "data")))
SECRET = os.environ.get("VERITON_API_SECRET", "")
CONTACT = "acer-openclaw@agentmail.to"
SERVICE_NAME = "veriton-html-json-api"
NETWORK = "eip155:8453"  # Base

_lock = threading.RLock()
_state: dict[str, Any] = {
    "credits": {},  # token -> {"units": int, "created": ts, "tx": str|None}
    "spent_tx": set(),  # claimed payment tx hashes
    "demo_hits": {},  # ip -> [ts,...]
    "calls": 0,
    "paid_calls": 0,
}


def _ensure_secret() -> str:
    global SECRET
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    sec_path = DATA_DIR / "secret"
    if SECRET:
        return SECRET
    if sec_path.exists():
        SECRET = sec_path.read_text().strip()
        return SECRET
    SECRET = secrets.token_hex(32)
    sec_path.write_text(SECRET)
    sec_path.chmod(0o600)
    return SECRET


def _state_path() -> Path:
    return DATA_DIR / "state.json"


def _load() -> None:
    p = _state_path()
    if not p.exists():
        return
    try:
        raw = json.loads(p.read_text())
        with _lock:
            _state["credits"] = raw.get("credits", {})
            _state["spent_tx"] = set(raw.get("spent_tx", []))
            _state["calls"] = int(raw.get("calls", 0))
            _state["paid_calls"] = int(raw.get("paid_calls", 0))
    except Exception:
        pass


def _save() -> None:
    with _lock:
        payload = {
            "credits": _state["credits"],
            "spent_tx": sorted(_state["spent_tx"]),
            "calls": _state["calls"],
            "paid_calls": _state["paid_calls"],
            "updated": time.time(),
        }
    tmp = _state_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(_state_path())


def usd_to_units(usd: float) -> int:
    return max(1, int(round(usd * 100)))


def units_to_usdc_base_units(units: int) -> int:
    # USDC 6 decimals; 1 unit = $0.01 = 10_000 base units
    return units * 10_000


class Extractor(HTMLParser):
    def __init__(self, selector: str | None = None, limit: int = 50):
        super().__init__(convert_charrefs=True)
        self.limit = limit
        self.title_parts: list[str] = []
        self.in_title = False
        self.meta: list[dict[str, str]] = []
        self.canonical: str | None = None
        self.headings: list[dict[str, str]] = []
        self.links: list[dict[str, str]] = []
        self.images: list[dict[str, str]] = []
        self.texts: list[str] = []
        self._sel = self._parse_selector(selector) if selector else None
        self._capture_text = False
        self._text_buf: list[str] = []
        self._tag_stack: list[str] = []

    @staticmethod
    def _parse_selector(sel: str) -> dict[str, Any]:
        # tiny subset: tag, .class, #id, comma lists handled outside
        sel = sel.strip()
        out: dict[str, Any] = {"tag": None, "id": None, "cls": None}
        if sel.startswith("#"):
            out["id"] = sel[1:]
        elif sel.startswith("."):
            out["cls"] = sel[1:]
        else:
            m = re.match(r"^([a-zA-Z0-9]+)(?:#([^.]+))?(?:\.([^\s]+))?$", sel)
            if m:
                out["tag"], out["id"], out["cls"] = m.group(1).lower(), m.group(2), m.group(3)
            else:
                out["tag"] = sel.lower()
        return out

    def _match(self, tag: str, attrs: dict[str, str]) -> bool:
        if not self._sel:
            return False
        if self._sel["tag"] and tag != self._sel["tag"]:
            return False
        if self._sel["id"] and attrs.get("id") != self._sel["id"]:
            return False
        if self._sel["cls"]:
            classes = attrs.get("class", "").split()
            if self._sel["cls"] not in classes:
                return False
        return True

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]):
        attrs = {k: (v or "") for k, v in attrs_list}
        self._tag_stack.append(tag)
        # capture flags: bit0 heading, bit1 link, bit2 selector
        flags = 0
        if tag == "title":
            self.in_title = True
        if tag == "meta":
            item = {k: v for k, v in attrs.items() if k in ("name", "property", "content", "charset")}
            if item:
                self.meta.append(item)
        if tag == "link" and attrs.get("rel", "").lower() == "canonical":
            self.canonical = attrs.get("href") or None
        if tag in ("h1", "h2", "h3", "h4") and len(self.headings) < self.limit:
            self.headings.append({"level": tag, "text": ""})
            flags |= 1
        if tag == "a" and len(self.links) < self.limit:
            href = attrs.get("href")
            if href:
                self.links.append({"href": href, "text": ""})
                flags |= 2
        if tag == "img" and len(self.images) < self.limit:
            src = attrs.get("src")
            if src:
                self.images.append({"src": src, "alt": attrs.get("alt", "")})
        if self._sel and self._match(tag, attrs):
            flags |= 4
        if not hasattr(self, "_cap_stack"):
            self._cap_stack = []
        self._cap_stack.append(flags)
        if flags:
            self._text_buf = []

    def handle_endtag(self, tag: str):
        if tag == "title":
            self.in_title = False
        if not hasattr(self, "_cap_stack"):
            self._cap_stack = []
        flags = self._cap_stack.pop() if self._cap_stack else 0
        if flags:
            text = re.sub(r"\s+", " ", "".join(self._text_buf)).strip()
            if flags & 1 and self.headings and self.headings[-1]["text"] == "":
                self.headings[-1]["text"] = text
            if flags & 2 and self.links and self.links[-1].get("text") == "":
                self.links[-1]["text"] = text
            if flags & 4 and text and len(self.texts) < self.limit:
                self.texts.append(text)
            self._text_buf = []
        if self._tag_stack and self._tag_stack[-1] == tag:
            self._tag_stack.pop()
        elif tag in self._tag_stack:
            while self._tag_stack and self._tag_stack[-1] != tag:
                self._tag_stack.pop()
            if self._tag_stack:
                self._tag_stack.pop()

    def handle_data(self, data: str):
        if self.in_title:
            self.title_parts.append(data)
        caps = getattr(self, "_cap_stack", [])
        if caps and caps[-1]:
            self._text_buf.append(data)

    def result(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "title": re.sub(r"\s+", " ", "".join(self.title_parts)).strip() or None,
            "canonical": self.canonical,
            "meta": self.meta[: self.limit],
            "headings": [h for h in self.headings if h.get("text")][: self.limit],
            "links": self.links[: self.limit],
            "images": self.images[: self.limit],
        }
        if self._sel is not None:
            out["selected"] = self.texts[: self.limit]
        return out


def extract_html(raw: str, selector: str | None = None, limit: int = 50) -> dict[str, Any]:
    if len(raw) > 1_500_000:
        raise ValueError("html too large (max ~1.5MB)")
    limit = max(1, min(int(limit), 200))
    if selector and "," in selector:
        # multi-selector: run each
        parts = [s.strip() for s in selector.split(",") if s.strip()]
        base = Extractor(None, limit)
        base.feed(raw)
        base.close()
        merged = base.result()
        selected: dict[str, list[str]] = {}
        for p in parts[:10]:
            ex = Extractor(p, limit)
            ex.feed(raw)
            ex.close()
            selected[p] = ex.texts[:limit]
        merged["selected"] = selected
        return merged
    ex = Extractor(selector, limit)
    ex.feed(raw)
    ex.close()
    return ex.result()


def rpc(method: str, params: list[Any]) -> Any:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(
        RPC, data=body, headers={"content-type": "application/json", "user-agent": "VeritonAPI/0.1"}
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode())
    if "error" in data:
        raise RuntimeError(data["error"])
    return data.get("result")


def verify_usdc_transfer(tx_hash: str, min_units: int) -> dict[str, Any]:
    """Verify tx is USDC transfer to PAY_TO for >= min_units ($0.01 units)."""
    if not re.fullmatch(r"0x[0-9a-fA-F]{64}", tx_hash or ""):
        raise ValueError("tx must be 0x + 64 hex")
    tx = rpc("eth_getTransactionReceipt", [tx_hash])
    if not tx:
        raise ValueError("tx not found on Base yet — wait for confirmation and retry")
    if int(tx.get("status", "0x0"), 16) != 1:
        raise ValueError("tx failed on-chain")
    # ERC-20 Transfer(address,address,uint256)
    topic_transfer = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    pay_to_topic = "0x" + PAY_TO[2:].rjust(64, "0")
    usdc = USDC
    matched = []
    for log in tx.get("logs") or []:
        if (log.get("address") or "").lower() != usdc:
            continue
        topics = log.get("topics") or []
        if len(topics) < 3 or topics[0].lower() != topic_transfer:
            continue
        if topics[2].lower() != pay_to_topic:
            continue
        data = log.get("data") or "0x0"
        amount = int(data, 16)
        matched.append(amount)
    if not matched:
        raise ValueError("no USDC Transfer to payTo found in tx (Base USDC only)")
    total = sum(matched)
    need = units_to_usdc_base_units(min_units)
    if total < need:
        raise ValueError(f"transfer too small: got {total} base units, need >= {need}")
    # convert base units to $0.01 credit units
    credit_units = total // 10_000
    return {
        "tx": tx_hash.lower(),
        "usdc_base_units": total,
        "credit_units": credit_units,
        "block": tx.get("blockNumber"),
    }


def mint_token(units: int, tx: str | None = None) -> str:
    token = "vk_" + secrets.token_urlsafe(24)
    with _lock:
        _state["credits"][token] = {
            "units": int(units),
            "created": time.time(),
            "tx": tx,
        }
        if tx:
            _state["spent_tx"].add(tx.lower())
    _save()
    return token


def debit(token: str, cost_units: int) -> dict[str, Any]:
    with _lock:
        row = _state["credits"].get(token)
        if not row:
            raise PermissionError("invalid credit token")
        if row["units"] < cost_units:
            raise PermissionError(
                f"insufficient credits: have {row['units']} units, need {cost_units} ($0.01 each)"
            )
        row["units"] -= cost_units
        _state["calls"] += 1
        _state["paid_calls"] += 1
        left = row["units"]
    _save()
    return {"remaining_units": left, "charged_units": cost_units}


def _public_base_static() -> str:
    env = (os.environ.get("VERITON_PUBLIC_BASE") or "").rstrip("/")
    if env:
        return env
    pb = Path(__file__).resolve().parent / "PUBLIC_BASE.txt"
    if pb.exists():
        for line in pb.read_text().splitlines():
            line = line.strip()
            if line.startswith("http"):
                return line.rstrip("/")
    return "https://veriton-html-json-api.netlify.app"


def _resource_meta(resource_path: str, price_usd: float) -> dict[str, Any]:
    """Bazaar discovery metadata + human description per paid route."""
    path = resource_path if resource_path.startswith("/") else f"/{resource_path}"
    if path.endswith("fetch-to-json"):
        return {
            "description": (
                "Fetch a public http(s) URL server-side and extract structured JSON "
                f"(title, meta, headings, links, images, optional CSS selector). ${price_usd:.2f} USDC/call on Base."
            ),
            "input_example": {"url": "https://example.com", "selector": "h1", "limit": 20},
            "input_schema_props": {
                "url": {"type": "string", "description": "Public http(s) URL to fetch"},
                "selector": {"type": "string", "description": "Optional CSS selector"},
                "limit": {"type": "integer", "description": "Max matches", "default": 40},
            },
            "input_required": ["url"],
            "output_example": {
                "title": "Example Domain",
                "meta": {"description": "Example"},
                "headings": [{"tag": "h1", "text": "Example Domain"}],
                "links": [{"href": "https://iana.org", "text": "More information..."}],
                "images": [],
                "matches": [],
            },
        }
    # default html-to-json
    return {
        "description": (
            "Extract structured JSON from an HTML string for agents "
            f"(title, meta, headings, links, images, optional CSS selector). ${price_usd:.2f} USDC/call on Base."
        ),
        "input_example": {"html": "<html><head><title>Hi</title></head><body><h1>Hello</h1></body></html>", "selector": "h1", "limit": 20},
        "input_schema_props": {
            "html": {"type": "string", "description": "Raw HTML document or fragment"},
            "selector": {"type": "string", "description": "Optional CSS selector"},
            "limit": {"type": "integer", "description": "Max matches", "default": 40},
        },
        "input_required": ["html"],
        "output_example": {
            "title": "Hi",
            "meta": {},
            "headings": [{"tag": "h1", "text": "Hello"}],
            "links": [],
            "images": [],
            "matches": [{"text": "Hello"}],
        },
    }


def payment_required_body(
    resource: str,
    price_usd: float,
    *,
    public_base: str | None = None,
    method: str = "POST",
) -> dict[str, Any]:
    """x402 v2 PaymentRequired with Bazaar discovery extension (CDP-indexable shape).

    Prepaid credit rail remains primary for v0 settlement (no facilitator keys yet).
    Bazaar listing still needs one successful CDP facilitator settle after keys exist.
    """
    import base64  # noqa: F401 — callers may still encode; keep import local-free here

    units = usd_to_units(price_usd)
    amount = str(units_to_usdc_base_units(units))
    base = (public_base or _public_base_static()).rstrip("/")
    path = resource if resource.startswith("/") else f"/{resource}"
    abs_url = f"{base}{path}"
    meta = _resource_meta(path, price_usd)
    # Prefer checksummed USDC address as seen on live Bazaar entries
    asset = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    pay_to = "0xa75Cc8545B169F0BeF2f29c9CCF86bc686D039E8"
    bazaar_info = {
        "input": {
            "type": "http",
            "method": method.upper(),
            "bodyType": "json",
            "body": meta["input_example"],
        },
        "output": {
            "type": "json",
            "example": meta["output_example"],
        },
    }
    bazaar_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["input"],
        "properties": {
            "input": {
                "type": "object",
                "required": ["type", "method"],
                "additionalProperties": False,
                "properties": {
                    "type": {"const": "http", "type": "string"},
                    "method": {
                        "type": "string",
                        "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"],
                    },
                    "bodyType": {"type": "string", "enum": ["json", "form", "text"]},
                    "body": {
                        "type": "object",
                        "properties": meta["input_schema_props"],
                        "required": meta["input_required"],
                        "additionalProperties": True,
                    },
                },
            },
            "output": {
                "type": "object",
                "required": ["type"],
                "properties": {
                    "type": {"type": "string"},
                    "example": {"type": "object"},
                },
            },
        },
    }
    return {
        "x402Version": 2,
        "error": "Payment Required",
        "resource": {
            "url": abs_url,
            "description": meta["description"][:500],
            "mimeType": "application/json",
            "serviceName": "VeritonHTMLJSON",
            "tags": ["html", "json", "scrape", "agents", "extract"],
        },
        "accepts": [
            {
                "scheme": "exact",
                "network": NETWORK,
                "amount": amount,
                "maxAmountRequired": amount,  # v1 clients / dual-read
                "asset": asset,
                "currency": asset,
                "payTo": pay_to,
                "recipient": pay_to,
                "maxTimeoutSeconds": 300,
                "extra": {
                    "name": "USDC",
                    "version": "2",
                    "veriton": {
                        "v0": "prepaid-credits",
                        "unit": "$0.01",
                        "buy": "POST /v1/credits/quote then pay USDC on Base, then POST /v1/credits/claim",
                        "use": "Authorization: Bearer vk_... or X-Api-Key: vk_...",
                        "contact": CONTACT,
                        "priceUsd": price_usd,
                    },
                },
            }
        ],
        "extensions": {
            "bazaar": {
                "info": bazaar_info,
                "schema": bazaar_schema,
            }
        },
        # dual-read helpers for our own clients / docs
        "payTo": pay_to,
        "network": NETWORK,
        "asset": "USDC",
        "priceUsd": price_usd,
    }


def payment_required_headers(pr: dict[str, Any]) -> dict[str, str]:
    import base64

    # CDP validate requires standard base64-encoded JSON in Payment-Required.
    # Keep ASCII-only + single header name (duplicates confuse some proxies/validators).
    raw = json.dumps(pr, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    b64 = base64.b64encode(raw).decode("ascii")
    return {"Payment-Required": b64}


def openapi() -> dict[str, Any]:
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "Veriton HTML→JSON Agent API",
            "version": "0.1.0",
            "description": (
                "Metered HTML→structured JSON for agents. "
                f"Pay USDC on Base to {PAY_TO}, claim credits, call endpoints. "
                f"${PRICE_HTML_USD}/call html body; ${PRICE_FETCH_USD}/call fetch+extract."
            ),
            "contact": {"email": CONTACT, "name": "Veriton"},
        },
        "servers": [{"url": "/", "description": "this host"}],
        "paths": {
            "/health": {"get": {"summary": "Health", "responses": {"200": {"description": "ok"}}}},
            "/v1/html-to-json": {
                "post": {
                    "summary": f"Extract JSON from HTML body (${PRICE_HTML_USD})",
                    "security": [{"bearer": []}],
                    "responses": {
                        "200": {"description": "extracted"},
                        "402": {"description": "payment required / buy credits"},
                    },
                }
            },
            "/v1/fetch-to-json": {
                "post": {
                    "summary": f"Fetch public URL + extract (${PRICE_FETCH_USD})",
                    "security": [{"bearer": []}],
                    "responses": {"200": {"description": "extracted"}, "402": {"description": "pay"}},
                }
            },
            "/v1/credits/quote": {"post": {"summary": "Quote a credit pack"}},
            "/v1/credits/claim": {"post": {"summary": "Claim credits after Base USDC tx"}},
            "/v1/credits/balance": {"get": {"summary": "Credit balance for token"}},
            "/v1/demo/html-to-json": {
                "post": {"summary": "Free tiny demo (rate-limited, max 8KB html)"}
            },
        },
        "components": {
            "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}},
        },
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "VeritonAPI/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys_stderr = __import__("sys").stderr
        print("[%s] %s" % (self.log_date_time_string(), fmt % args), file=sys_stderr)

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Api-Key, PAYMENT-SIGNATURE")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, HEAD, OPTIONS")

    def _send(self, code: int, obj: Any, extra_headers: dict[str, str] | None = None, head_only: bool = False) -> None:
        body = json.dumps(obj, indent=2, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _quote_payload(self, usd: float | None = None) -> dict[str, Any]:
        if usd is None:
            usd = MIN_PACK_USD
        usd = float(usd)
        if usd < MIN_PACK_USD:
            raise ValueError(f"min pack ${MIN_PACK_USD}")
        if usd > 50:
            raise ValueError("max pack $50 in v0")
        units = usd_to_units(usd)
        base_units = units_to_usdc_base_units(units)
        return {
            "usd": round(units / 100, 2),
            "credit_units": units,
            "usdc_base_units": base_units,
            "usdc_amount": f"{base_units / 1_000_000:.6f}",
            "payTo": PAY_TO,
            "token": USDC,
            "network": NETWORK,
            "chain": "base",
            "method": "POST /v1/credits/quote with JSON {\"usd\": <amount>} also accepted",
            "note": "Send USDC on Base mainnet, then POST /v1/credits/claim with tx hash",
            "claim": "POST /v1/credits/claim {\"tx\": \"0x...\"}",
            "html_to_json_calls": units // usd_to_units(PRICE_HTML_USD),
            "fetch_to_json_calls": units // usd_to_units(PRICE_FETCH_USD),
        }

    def _read_json(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 2_000_000:
            raise ValueError("body too large")
        raw = self.rfile.read(n) if n else b"{}"
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _token(self) -> str | None:
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            return auth.split(" ", 1)[1].strip()
        return self.headers.get("X-Api-Key") or None

    def _client_ip(self) -> str:
        return self.headers.get("CF-Connecting-IP") or self.headers.get("X-Forwarded-For", "").split(",")[0].strip() or self.client_address[0]


    def do_HEAD(self) -> None:  # noqa: N802
        # Discovery crawlers probe with HEAD; answer without body.
        path = urlparse(self.path).path.rstrip("/") or "/"
        self._head_only = True
        try:
            # Reuse GET routing but suppress body via flag on _send
            orig = self._send
            def _head_send(code, obj, extra_headers=None, head_only=False):
                return orig(code, obj, extra_headers=extra_headers, head_only=True)
            self._send = _head_send  # type: ignore[method-assign]
            self.do_GET()
        finally:
            self._send = orig  # type: ignore[method-assign]
            self._head_only = False

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _public_base(self) -> str:
        # Prefer proxy-facing host so Netlify/Cloudflare frontends advertise themselves.
        env = (os.environ.get("VERITON_PUBLIC_BASE") or "").rstrip("/")
        if not env:
            pb = Path(__file__).resolve().parent / "PUBLIC_BASE.txt"
            if pb.exists():
                for line in pb.read_text().splitlines():
                    line = line.strip()
                    if line.startswith("http"):
                        env = line.rstrip("/")
                        break
        if env:
            return env
        xf_host = (self.headers.get("X-Forwarded-Host") or "").split(",")[0].strip()
        if xf_host:
            proto = (self.headers.get("X-Forwarded-Proto") or "https").split(",")[0].strip() or "https"
            return f"{proto}://{xf_host}".rstrip("/")
        host_file = Path(__file__).resolve().parent / "HOST.txt"
        if host_file.exists():
            for line in host_file.read_text().splitlines():
                line = line.strip()
                if line.startswith("http"):
                    return line.rstrip("/")
        proto = self.headers.get("X-Forwarded-Proto") or "https"
        host = self.headers.get("Host") or f"127.0.0.1:{PORT}"
        return f"{proto}://{host}".rstrip("/")

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path in ("/.well-known/x402", "/.well-known/x402.json"):
            base = self._public_base()
            return self._send(
                200,
                {
                    "version": 2,
                    "x402Version": 2,
                    "spec": "x402-discovery/1",
                    "name": SERVICE_NAME,
                    "description": "Metered HTML→structured JSON for agents. Base USDC prepaid credits + HTTP 402 x402 v2 accepts[] + Bazaar extensions.",
                    "homepage": "https://veriton-dev.github.io/veriton-micro-dev/api/",
                    "docs": f"{base}/v1/openapi.json",
                    "contact": CONTACT,
                    "payment": {
                        "protocol": "x402",
                        "scheme": "exact",
                        "network": NETWORK,
                        "asset": USDC,
                        "currency": "USDC",
                        "payTo": PAY_TO,
                        "v0": "prepaid-credits via /v1/credits/quote + /v1/credits/claim",
                    },
                    "resources": [
                        f"{base}/v1/html-to-json",
                        f"{base}/v1/fetch-to-json",
                    ],
                    "resourceDetails": [
                        {
                            "url": f"{base}/v1/html-to-json",
                            "method": "POST",
                            "name": "html-to-json",
                            "description": "Extract title, meta, headings, links, images, optional CSS selector matches from an HTML string. Body: {html, selector?}.",
                            "price": PRICE_HTML_USD,
                            "network": NETWORK,
                            "asset": USDC,
                            "payTo": PAY_TO,
                            "currency": "USDC",
                        },
                        {
                            "url": f"{base}/v1/fetch-to-json",
                            "method": "POST",
                            "name": "fetch-to-json",
                            "description": "Fetch a public http(s) URL server-side and extract structured JSON. Body: {url, selector?}.",
                            "price": PRICE_FETCH_USD,
                            "network": NETWORK,
                            "asset": USDC,
                            "payTo": PAY_TO,
                            "currency": "USDC",
                        },
                    ],
                    "instructions": {
                        "demo": f"POST {base}/v1/demo/html-to-json",
                        "method": "POST on paid routes; unpaid calls return HTTP 402 x402 accepts[]",
                        "buy": [
                            f"POST {base}/v1/credits/quote",
                            "Transfer USDC on Base to payTo",
                            f"POST {base}/v1/credits/claim",
                            f"POST {base}/v1/html-to-json with Authorization: Bearer vk_...",
                        ],
                    },
                },
            )
        if path == "/health":
            with _lock:
                return self._send(
                    200,
                    {
                        "ok": True,
                        "service": SERVICE_NAME,
                        "payTo": PAY_TO,
                        "network": NETWORK,
                        "asset": "USDC",
                        "prices": {
                            "html_to_json_usd": PRICE_HTML_USD,
                            "fetch_to_json_usd": PRICE_FETCH_USD,
                            "min_pack_usd": MIN_PACK_USD,
                        },
                        "calls": _state["calls"],
                        "paid_calls": _state["paid_calls"],
                        "mcp": f"{self._public_base()}/mcp",
                    },
                )
        if path in ("/mcp", "/v1/mcp"):
            base = self._public_base()
            return self._send(
                200,
                {
                    "protocol": "mcp",
                    "transport": "http-jsonrpc",
                    "endpoint": f"{base}/mcp",
                    "name": SERVICE_NAME,
                    "payment": "x402 + prepaid Base USDC credits",
                    "tools": [
                        {"name": "html_to_json", "price_usd": PRICE_HTML_USD},
                        {"name": "fetch_to_json", "price_usd": PRICE_FETCH_USD},
                        {"name": "demo_html_to_json", "price_usd": 0},
                    ],
                    "docs": f"{base}/v1/openapi.json",
                    "buy": f"{base}/v1/credits/quote",
                },
            )
        if path in ("/", "/v1"):
            return self._send(
                200,
                {
                    "name": SERVICE_NAME,
                    "docs": "/v1/openapi.json",
                    "mcp": "/mcp",
                    "payTo": PAY_TO,
                    "network": "Base (eip155:8453)",
                    "asset": USDC,
                    "flow": [
                        "POST /v1/credits/quote {\"usd\": 1.0}",
                        "Transfer USDC on Base to payTo for exact amount",
                        "POST /v1/credits/claim {\"tx\": \"0x...\"}",
                        "POST /v1/html-to-json with Authorization: Bearer <token>",
                    ],
                    "endpoints": {
                        "POST /v1/html-to-json": f"${PRICE_HTML_USD}",
                        "POST /v1/fetch-to-json": f"${PRICE_FETCH_USD}",
                        "POST /v1/demo/html-to-json": "free rate-limited",
                        "POST /mcp": "MCP JSON-RPC (tools/list + tools/call)",
                    },
                    "contact": CONTACT,
                    "wallet_mppx": PAY_TO,
                },
            )
        if path in ("/v1/openapi.json", "/openapi.json", "/openapi"):
            return self._send(200, openapi())
        if path in ("/v1/health", "/demo", "/v1/demo"):
            # aliases for crawlers / humans following short docs links
            if path.endswith("health"):
                return self._send(
                    200,
                    {
                        "ok": True,
                        "service": "veriton-html-json-api",
                        "payTo": PAY_TO,
                        "network": NETWORK,
                        "docs": f"{self._public_base()}/v1/openapi.json",
                    },
                )
            return self._send(
                200,
                {
                    "demo": True,
                    "usage": "GET or POST /v1/demo/html-to-json with html= (max 8KB). Optional selector, limit.",
                    "endpoint": f"{self._public_base()}/v1/demo/html-to-json",
                    "example": f"{self._public_base()}/v1/demo/html-to-json?html=%3Ch1%3EHi%3C%2Fh1%3E",
                    "upgrade": "/v1/credits/quote",
                },
            )

        if path == "/v1/credits/quote":
            qs = parse_qs(urlparse(self.path).query)
            try:
                usd = float((qs.get("usd") or [MIN_PACK_USD])[0])
                payload = self._quote_payload(usd)
            except Exception as e:
                return self._send(400, {"error": str(e)})
            return self._send(200, payload)
        if path == "/v1/demo/html-to-json":
            # GET demo for crawlers: ?html=...&selector=...
            qs = parse_qs(urlparse(self.path).query)
            html_in = (qs.get("html") or [""])[0]
            selector = (qs.get("selector") or [None])[0]
            try:
                limit = int((qs.get("limit") or ["20"])[0])
            except Exception:
                limit = 20
            if not html_in:
                return self._send(
                    200,
                    {
                        "demo": True,
                        "usage": "GET or POST /v1/demo/html-to-json with html= (max 8KB). Optional selector, limit.",
                        "example": f"{self._public_base()}/v1/demo/html-to-json?html=%3Ch1%3EHi%3C%2Fh1%3E",
                        "upgrade": "/v1/credits/quote",
                    },
                )
            ip = self._client_ip()
            now = time.time()
            with _lock:
                hits = [t for t in _state["demo_hits"].get(ip, []) if now - t < 3600]
                if len(hits) >= 10:
                    return self._send(429, {"error": "demo rate limit: 10/hour/ip"})
                hits.append(now)
                _state["demo_hits"][ip] = hits
            if len(html_in) > 8_000:
                return self._send(400, {"error": "demo max 8KB html — buy credits for full"})
            try:
                result = extract_html(html_in, selector, limit)
            except Exception as e:
                return self._send(400, {"error": str(e)})
            return self._send(200, {"demo": True, "result": result, "upgrade": "/v1/credits/quote"})
        if path == "/v1/credits/balance":
            tok = self._token()
            if not tok:
                return self._send(401, {"error": "missing bearer token"})
            with _lock:
                row = _state["credits"].get(tok)
            if not row:
                return self._send(404, {"error": "unknown token"})
            return self._send(
                200,
                {
                    "units": row["units"],
                    "usd_remaining": round(row["units"] / 100, 2),
                    "tx": row.get("tx"),
                },
            )
        # Crawlers often GET paid routes; answer 402 so discovery learns chain/payTo/price.
        if path in ("/v1/html-to-json", "/v1/fetch-to-json"):
            price = PRICE_HTML_USD if path.endswith("html-to-json") else PRICE_FETCH_USD
            pr = payment_required_body(path, price, public_base=self._public_base(), method="POST")
            return self._send(402, pr, payment_required_headers(pr))
        return self._send(404, {"error": "not found", "path": path})


    def _mcp_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "html_to_json",
                "description": f"Extract structured JSON from an HTML string. Paid ${PRICE_HTML_USD} USDC/call (prepaid credits or x402).",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "html": {"type": "string", "description": "Raw HTML"},
                        "selector": {"type": "string", "description": "Optional CSS selector"},
                        "limit": {"type": "integer", "default": 40},
                    },
                    "required": ["html"],
                },
            },
            {
                "name": "fetch_to_json",
                "description": f"Fetch a public URL and extract structured JSON. Paid ${PRICE_FETCH_USD} USDC/call.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "selector": {"type": "string"},
                        "limit": {"type": "integer", "default": 40},
                    },
                    "required": ["url"],
                },
            },
            {
                "name": "demo_html_to_json",
                "description": "Free rate-limited demo extract (max 8KB HTML).",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "html": {"type": "string"},
                        "selector": {"type": "string"},
                        "limit": {"type": "integer", "default": 20},
                    },
                    "required": ["html"],
                },
            },
        ]

    def _mcp_rpc(self, body: dict[str, Any]) -> None:
        req_id = body.get("id")
        method = body.get("method") or ""
        params = body.get("params") or {}

        def ok(result: Any) -> None:
            self._send(200, {"jsonrpc": "2.0", "id": req_id, "result": result})

        def err(code: int, message: str, data: Any = None, http: int = 200) -> None:
            payload: dict[str, Any] = {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": code, "message": message},
            }
            if data is not None:
                payload["error"]["data"] = data
            self._send(http, payload)

        if method in ("initialize", "notifications/initialized"):
            if method == "notifications/initialized":
                self.send_response(204)
                self._cors()
                self.end_headers()
                return
            return ok(
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVICE_NAME, "version": "0.1.0"},
                }
            )
        if method in ("tools/list", "tools.list"):
            return ok({"tools": self._mcp_tools()})
        if method in ("tools/call", "tools.call"):
            name = (params.get("name") if isinstance(params, dict) else None) or ""
            arguments = (params.get("arguments") if isinstance(params, dict) else None) or {}
            if not isinstance(arguments, dict):
                arguments = {}
            if name == "demo_html_to_json":
                html_in = str(arguments.get("html") or "")
                selector = arguments.get("selector")
                try:
                    limit = int(arguments.get("limit") or 20)
                except Exception:
                    limit = 20
                if not html_in:
                    return err(-32602, "html required")
                if len(html_in) > 8_000:
                    return err(-32602, "demo max 8KB html — buy credits for full")
                ip = self._client_ip()
                now = time.time()
                with _lock:
                    hits = [t for t in _state["demo_hits"].get(ip, []) if now - t < 3600]
                    if len(hits) >= 10:
                        return err(-32000, "demo rate limit: 10/hour/ip", http=429)
                    hits.append(now)
                    _state["demo_hits"][ip] = hits
                try:
                    result = extract_html(html_in, selector, limit)
                except Exception as e:
                    return err(-32000, str(e))
                return ok({"content": [{"type": "text", "text": json.dumps({"demo": True, "result": result}, ensure_ascii=False)}]})
            if name in ("html_to_json", "fetch_to_json"):
                # Prefer prepaid bearer; otherwise return HTTP 402 x402 challenge.
                price = PRICE_HTML_USD if name == "html_to_json" else PRICE_FETCH_USD
                resource = f"/v1/{name.replace('_', '-')}"
                tok = self._token()
                units_need = usd_to_units(price)
                if tok:
                    with _lock:
                        row = _state["credits"].get(tok)
                        if row and int(row.get("units") or 0) >= units_need:
                            row["units"] = int(row["units"]) - units_need
                            _state["calls"] += 1
                            _state["paid_calls"] += 1
                            _save()
                            try:
                                if name == "html_to_json":
                                    result = extract_html(
                                        str(arguments.get("html") or ""),
                                        arguments.get("selector"),
                                        int(arguments.get("limit") or 40),
                                    )
                                else:
                                    # reuse fetch path via existing helper if present
                                    url = str(arguments.get("url") or "")
                                    if not url:
                                        return err(-32602, "url required")
                                    # fall back: fetch then extract
                                    req = urllib.request.Request(
                                        url,
                                        headers={"User-Agent": "VeritonMCP/0.1", "Accept": "text/html,application/xhtml+xml"},
                                    )
                                    with urllib.request.urlopen(req, timeout=20) as resp:
                                        html_in = resp.read()[:500_000].decode("utf-8", "replace")
                                    result = extract_html(
                                        html_in,
                                        arguments.get("selector"),
                                        int(arguments.get("limit") or 40),
                                    )
                            except Exception as e:
                                # refund on failure
                                with _lock:
                                    row2 = _state["credits"].get(tok)
                                    if row2 is not None:
                                        row2["units"] = int(row2.get("units") or 0) + units_need
                                        _state["paid_calls"] = max(0, int(_state["paid_calls"]) - 1)
                                        _state["calls"] = max(0, int(_state["calls"]) - 1)
                                        _save()
                                return err(-32000, str(e))
                            return ok(
                                {
                                    "content": [
                                        {
                                            "type": "text",
                                            "text": json.dumps(
                                                {
                                                    "paid": True,
                                                    "tool": name,
                                                    "units_spent": units_need,
                                                    "result": result,
                                                },
                                                ensure_ascii=False,
                                            ),
                                        }
                                    ]
                                }
                            )
                pr = payment_required_body(resource, price, public_base=self._public_base(), method="POST")
                # HTTP 402 so MCP/x402 clients can settle; also embed challenge in JSON-RPC error.
                return self._send(
                    402,
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {
                            "code": 402,
                            "message": "Payment Required",
                            "data": pr,
                        },
                        "x402": pr,
                    },
                    payment_required_headers(pr),
                )
            return err(-32601, f"unknown tool: {name}")
        if method in ("ping",):
            return ok({})
        return err(-32601, f"method not found: {method}")

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            body = self._read_json()
        except Exception as e:
            return self._send(400, {"error": f"bad json: {e}"})

        if path in ("/mcp", "/v1/mcp"):
            return self._mcp_rpc(body)

        if path == "/v1/credits/quote":
            try:
                payload = self._quote_payload(float(body.get("usd") or MIN_PACK_USD))
            except Exception as e:
                return self._send(400, {"error": str(e)})
            return self._send(200, payload)

        if path == "/v1/credits/claim":
            tx = (body.get("tx") or body.get("tx_hash") or "").strip()
            min_usd = float(body.get("min_usd") or MIN_PACK_USD)
            min_units = usd_to_units(min_usd)
            with _lock:
                if tx.lower() in _state["spent_tx"]:
                    return self._send(409, {"error": "tx already claimed"})
            try:
                info = verify_usdc_transfer(tx, min_units)
            except Exception as e:
                return self._send(400, {"error": str(e)})
            token = mint_token(info["credit_units"], info["tx"])
            return self._send(
                200,
                {
                    "ok": True,
                    "token": token,
                    "credit_units": info["credit_units"],
                    "usd": round(info["credit_units"] / 100, 2),
                    "tx": info["tx"],
                    "authorization": f"Bearer {token}",
                },
            )

        if path == "/v1/demo/html-to-json":
            ip = self._client_ip()
            now = time.time()
            with _lock:
                hits = [t for t in _state["demo_hits"].get(ip, []) if now - t < 3600]
                if len(hits) >= 10:
                    return self._send(429, {"error": "demo rate limit: 10/hour/ip"})
                hits.append(now)
                _state["demo_hits"][ip] = hits
            html_in = body.get("html") or ""
            if len(html_in) > 8_000:
                return self._send(400, {"error": "demo max 8KB html — buy credits for full"})
            try:
                result = extract_html(html_in, body.get("selector"), int(body.get("limit") or 20))
            except Exception as e:
                return self._send(400, {"error": str(e)})
            return self._send(200, {"demo": True, "result": result, "upgrade": "/v1/credits/quote"})

        if path in ("/v1/html-to-json", "/v1/fetch-to-json"):
            price = PRICE_HTML_USD if path.endswith("html-to-json") else PRICE_FETCH_USD
            cost = usd_to_units(price)
            tok = self._token()
            if not tok:
                pr = payment_required_body(path, price, public_base=self._public_base(), method="POST")
                return self._send(402, pr, payment_required_headers(pr))
            try:
                bill = debit(tok, cost)
            except PermissionError as e:
                pr = payment_required_body(path, price, public_base=self._public_base(), method="POST")
                pr["detail"] = str(e)
                return self._send(402, pr, payment_required_headers(pr))

            try:
                if path.endswith("fetch-to-json"):
                    url = (body.get("url") or "").strip()
                    if not url.startswith("http://") and not url.startswith("https://"):
                        raise ValueError("url must be http(s)")
                    req = urllib.request.Request(
                        url,
                        headers={
                            "User-Agent": "VeritonFetch/0.1 (+https://veriton-dev.github.io/veriton-micro-dev/)",
                            "Accept": "text/html,application/xhtml+xml",
                        },
                    )
                    with urllib.request.urlopen(req, timeout=20) as resp:
                        raw = resp.read(1_500_000).decode("utf-8", "replace")
                        final_url = resp.geturl()
                    result = extract_html(raw, body.get("selector"), int(body.get("limit") or 50))
                    result["source_url"] = final_url
                else:
                    html_in = body.get("html")
                    if not isinstance(html_in, str) or not html_in:
                        raise ValueError("html string required")
                    result = extract_html(html_in, body.get("selector"), int(body.get("limit") or 50))
            except Exception as e:
                # refund on failure
                with _lock:
                    row = _state["credits"].get(tok)
                    if row:
                        row["units"] += cost
                        _state["paid_calls"] = max(0, _state["paid_calls"] - 1)
                        _state["calls"] = max(0, _state["calls"] - 1)
                _save()
                return self._send(400, {"error": str(e), "refunded_units": cost})

            return self._send(
                200,
                {
                    "ok": True,
                    "charged_usd": price,
                    "billing": bill,
                    "result": result,
                },
            )

        return self._send(404, {"error": "not found", "path": path})


def main() -> None:
    _ensure_secret()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _load()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(
        json.dumps(
            {
                "listening": f"{HOST}:{PORT}",
                "payTo": PAY_TO,
                "network": NETWORK,
                "usdc": USDC,
                "data": str(DATA_DIR),
            }
        ),
        flush=True,
    )
    httpd.serve_forever()


if __name__ == "__main__":
    main()
