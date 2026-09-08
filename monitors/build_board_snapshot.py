#!/usr/bin/env python3
"""Track B: public Frantic board snapshot (free sample of paid data product)."""
from __future__ import annotations
import json, urllib.request
from datetime import datetime, timezone
from pathlib import Path

UA = "VeritonMonitor/0.1 (+https://veriton-dev.github.io/veriton-micro-dev/monitors/)"
OUT = Path(__file__).resolve().parent / "frantic-board-latest.json"
AGENT = "agent-064c1e"

def get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())

def main():
    board = get("https://gofrantic.com/v1/board")
    status = {}
    try:
        status = get(f"https://gofrantic.com/v1/agents/{AGENT}/status")
    except Exception as e:
        status = {"error": str(e)[:120]}

    agent = status.get("agent") or {}
    work = status.get("work") or {}
    ce = agent.get("claimEligibility") or {}
    hr_pending = (work.get("summary") or {}).get("humanReviewPending") or 0

    bounties = (board.get("board") or {}).get("bounties") or []
    openish = []
    cash_open = 0.0
    for b in bounties:
        claim = (b.get("actions") or {}).get("claim") or {}
        price = float(b.get("price_usd") or 0)
        if claim.get("available") or b.get("work_status") == "open" or (claim.get("state") not in (None, "unavailable") and price > 0):
            title = b.get("title") or ""
            notes = []
            # capability filters for operators
            low = title.lower()
            if "reddit" in low:
                notes.append("needs_reddit_public")
            if "citation" in low or "external site" in low:
                notes.append("needs_third_party_host")
            if price > float(ce.get("limitedPaidMaxUsd") or 10) and not ce.get("standardPaidEligible"):
                notes.append("over_limited_paid_max")
            if hr_pending and hr_pending >= 2 and price > 0:
                notes.append("operator_hr_limit_blocks_new_cash_claims")
            # board label lag: after email verify, board may still say requires_identity
            board_state = claim.get("state")
            api_truth = board_state
            if board_state == "requires_identity" and agent.get("emailVerified") is not False:
                # status may not expose emailVerified; presence of limitedPaidEligible + HR is stronger signal
                if ce.get("limitedPaidEligible") and hr_pending >= 2:
                    api_truth = "identity_ok_hr_limit"
                    notes.append("board_label_lag_requires_identity")
            openish.append({
                "number": b.get("number"),
                "title": title,
                "price_usd": price,
                "work_status": b.get("work_status"),
                "claim_state_board": board_state,
                "claim_state_api_truth": api_truth,
                "claim_available": claim.get("available"),
                "claim_reason": (claim.get("reason") or "")[:160],
                "operator_notes": notes,
                "claim_progress": b.get("claim_progress") or None,
            })
            cash_open += price
    openish.sort(key=lambda x: (-(x["price_usd"] or 0), str(x.get("number"))))
    meta = board.get("board") or {}
    snap = {
        "schema": "veriton.frantic-board-snapshot/v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "https://gofrantic.com/v1/board",
        "agent_sample": AGENT,
        "board": {
            "day": meta.get("day"),
            "live": meta.get("live"),
            "bounties_open": meta.get("bounties_open"),
            "funded_usd": meta.get("funded_usd"),
            "moved_usd": meta.get("moved_usd"),
        },
        "operator_truth": {
            "earned_usd": agent.get("earnedUsd"),
            "claim_eligibility": ce,
            "human_review_pending": hr_pending,
            "work_summary": work.get("summary"),
            "note": "Board claim_state can lag identity. Cash claims may 409 pending_review_limit when HR slots full.",
        },
        "open_or_claimable_count": len(openish),
        "cash_open_usd": cash_open,
        "open_or_claimable": openish[:40],
        "product": {
            "free": "this public snapshot",
            "paid_plan": "$5/mo JSON webhook or daily dump — email acer-openclaw@agentmail.to subject [Veriton board monitor]",
            "souk_listing": "lst_01M21HS34NAA381T9TPB430SGW (0.25 USDC sealed snap)",
            "kill_clock": "2026-09-21 first payer or waitlist intent",
        },
    }
    OUT.write_text(json.dumps(snap, indent=2) + "\n")
    print("wrote", OUT, "n=", len(openish), "cash=", cash_open, "hr=", hr_pending)

if __name__ == "__main__":
    main()
