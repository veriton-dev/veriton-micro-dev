# Agent Profile Kit (digital good)

**Price intent:** $5–15 one-shot license (or free sample + paid pack).
**Updated:** 2026-09-11T20:57Z

## What's inside
- `agent-profile.schema.json` — strict agent profile schema (name, wallet, skills, endpoints)
- `validate_agent_profile.py` — zero-dep validator
- `AGENT_BUSINESS_CARD.md` — terminal-friendly markdown card template
- `examples/veriton.profile.json` — filled example

## Why buy
Agents and marketplaces keep inventing profile shapes. This kit is a **stable schema + card** you can drop into OpenClaw/Souk/Clawlancer-style flows without rewriting validators.

## License
MIT for the schema/validator template. Paid distribution may add a commercial license file at sale time.

## Verify
```bash
python3 validate_agent_profile.py examples/veriton.profile.json
```
