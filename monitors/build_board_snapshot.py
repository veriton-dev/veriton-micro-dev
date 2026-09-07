#!/usr/bin/env python3
"""Track B: public Frantic board snapshot (free sample of paid data product)."""
from __future__ import annotations
import json, urllib.request
from datetime import datetime, timezone
from pathlib import Path

UA = "VeritonMonitor/0.1 (+https://veriton-dev.github.io/veriton-micro-dev/monitors/)"
OUT = Path(__file__).resolve().parent / "frantic-board-latest.json"

def get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())

def main():
    board = get("https://gofrantic.com/v1/board")
    bounties = (board.get("board") or {}).get("bounties") or []
    openish = []
    for b in bounties:
        claim = (b.get("actions") or {}).get("claim") or {}
        price = float(b.get("price_usd") or 0)
        if claim.get("available") or b.get("work_status") == "open" or (claim.get("state") not in (None, "unavailable") and price > 0):
            openish.append({
                "number": b.get("number"),
                "title": b.get("title"),
                "price_usd": price,
                "work_status": b.get("work_status"),
                "claim_state": claim.get("state"),
                "claim_available": claim.get("available"),
                "claim_reason": (claim.get("reason") or "")[:160],
                "claim_progress": b.get("claim_progress") or None,
            })
    openish.sort(key=lambda x: (-(x["price_usd"] or 0), str(x.get("number"))))
    meta = board.get("board") or {}
    snap = {
        "schema": "veriton.frantic-board-snapshot/v0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "https://gofrantic.com/v1/board",
        "board": {
            "day": meta.get("day"),
            "live": meta.get("live"),
            "bounties_open": meta.get("bounties_open"),
            "funded_usd": meta.get("funded_usd"),
            "moved_usd": meta.get("moved_usd"),
        },
        "open_or_claimable_count": len(openish),
        "open_or_claimable": openish[:40],
        "product": {
            "free": "this public snapshot",
            "paid_plan": "$5/mo JSON webhook or daily dump — email acer-openclaw@agentmail.to subject [Veriton board monitor]",
            "kill_clock": "2026-09-21 first payer or waitlist intent",
        },
    }
    OUT.write_text(json.dumps(snap, indent=2) + "\n")
    print("wrote", OUT, "n=", len(openish))

if __name__ == "__main__":
    main()
