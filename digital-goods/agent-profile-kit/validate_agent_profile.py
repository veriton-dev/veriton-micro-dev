#!/usr/bin/env python3
"""Validate agent profile JSON against agent-profile.schema.json (stdlib only)."""
from __future__ import annotations
import json, re, sys
from pathlib import Path

SCHEMA = json.loads((Path(__file__).with_name("agent-profile.schema.json")).read_text())

def err(msg):
    print(f"INVALID: {msg}", file=sys.stderr)
    return 1

def main(argv=None):
    argv = argv or sys.argv[1:]
    if not argv:
        print("usage: validate_agent_profile.py profile.json", file=sys.stderr)
        return 2
    data = json.loads(Path(argv[0]).read_text())
    if not isinstance(data, dict):
        return err("root must be object")
    for k in SCHEMA.get("required", []):
        if k not in data:
            return err(f"missing required {k}")
    name = data.get("name")
    if not isinstance(name, str) or not (1 <= len(name) <= 100):
        return err("name")
    wallet = data.get("wallet_address")
    if not isinstance(wallet, str) or not re.fullmatch(r"0x[a-fA-F0-9]{40}", wallet):
        return err("wallet_address")
    if "handle" in data:
        h = data["handle"]
        if not isinstance(h, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", h):
            return err("handle")
    if "bio" in data and (not isinstance(data["bio"], str) or len(data["bio"]) > 280):
        return err("bio")
    if "skills" in data:
        sk = data["skills"]
        if not isinstance(sk, list) or len(sk) > 32 or len(set(sk)) != len(sk):
            return err("skills")
        for s in sk:
            if not isinstance(s, str) or not (1 <= len(s) <= 64):
                return err("skills item")
    print("OK")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
