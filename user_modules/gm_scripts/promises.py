#!/usr/bin/env python3
"""
promises.py — narrative promises tracking.

Stories are made of promises, not quests. A promise is an expectation
planted in the player's mind — a question the GM has implicitly committed
to answering. Players unconsciously remember promises; when they're
fulfilled, narrative tension releases. When they're broken, players feel
cheated.

Example promise chain:
  Player meets mysterious woman.
    → Promise: "Who is she?"
  Reveal clue.
    → Promise: "Why is she lying?"
  Reveal.
    → Promise: "Who controls her?"

Each promise has a strength (0-100) that increases each time it's
referenced. High-strength promises demand fulfillment; ignoring them
creates player frustration. The Scene Director tracks unfulfilled
promises and surfaces them when planning scenes.

This is different from story questions (dramatic questions the GM tracks
internally) and intrigues (nested mysteries with clue trails). Promises
are about the SOCIAL CONTRACT between GM and players — what the GM has
committed to delivering.

Storage: <campaign-dir>/promises.json

Usage:
  python3 promises.py add --campaign <name> '<json>'
  python3 promises.py list --campaign <name> [--status open]
  python3 promises.py strengthen --campaign <name> --id p001 --amount 10
  python3 promises.py fulfill --campaign <name> --id p001 --session 15
  python3 promises.py break --campaign <name> --id p001 --reason "..."
  python3 promises.py check-expiration --campaign <name> --to-session 20
  python3 promises.py pressing --campaign <name>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from paths import find_campaign


# ── IO ──────────────────────────────────────────────────────────────────────

def _promises_path(campaign: str) -> Path:
    return find_campaign(campaign) / "promises.json"


def _load(campaign: str) -> dict:
    p = _promises_path(campaign)
    if not p.exists():
        return {"version": 1, "promises": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "promises": []}
    data.setdefault("version", 1)
    data.setdefault("promises", [])
    return data


def _save(campaign: str, data: dict) -> None:
    p = _promises_path(campaign)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _find(data: dict, pid: str) -> Optional[dict]:
    for p in data["promises"]:
        if p["id"] == pid:
            return p
    return None


# ── States ──────────────────────────────────────────────────────────────────

PROMISE_STATES = ("open", "strengthening", "fulfilled", "broken", "abandoned")


# ── Commands ────────────────────────────────────────────────────────────────

def cmd_add(args) -> int:
    """Plant a narrative promise."""
    data = _load(args.campaign)
    try:
        promise = json.loads(args.json)
    except json.JSONDecodeError as e:
        print(f"error: invalid JSON: {e}", file=sys.stderr)
        return 1

    if "id" not in promise or "promise" not in promise:
        print("error: promise must have 'id' and 'promise'", file=sys.stderr)
        return 1

    promise.setdefault("strength", 20)        # starting strength
    promise.setdefault("status", "open")
    promise.setdefault("planted_session", args.session or 0)
    promise.setdefault("planted_scene", None)
    promise.setdefault("fulfillment_condition", "")
    promise.setdefault("fulfilled_session", None)
    promise.setdefault("expiration", None)    # session:N after which it's "broken"
    promise.setdefault("linked_facts", [])    # epistemology fact IDs
    promise.setdefault("linked_intrigues", [])
    promise.setdefault("references", [])      # sessions where it was strengthened
    promise.setdefault("break_reason", None)

    existing_ids = {p["id"] for p in data["promises"]}
    if promise["id"] in existing_ids and not args.force:
        print(f"error: promise '{promise['id']}' already exists. Use --force.", file=sys.stderr)
        return 1

    if promise["id"] in existing_ids:
        data["promises"] = [p if p["id"] != promise["id"] else promise for p in data["promises"]]
    else:
        data["promises"].append(promise)

    _save(args.campaign, data)
    print(f"OK — promise '{promise['id']}' planted")
    print(f"  promise: {promise['promise']}")
    print(f"  strength: {promise['strength']}")
    print(f"  planted: session {promise['planted_session']}")
    if promise.get("expiration"):
        print(f"  expires: {promise['expiration']}")
    return 0


def cmd_list(args) -> int:
    """List promises, optionally filtered by status."""
    data = _load(args.campaign)
    if not data["promises"]:
        print(f"# no promises for campaign '{args.campaign}'")
        return 0

    promises = data["promises"]
    if args.status:
        promises = [p for p in promises if p.get("status") == args.status]

    # Sort by strength descending (most pressing first)
    promises.sort(key=lambda p: p.get("strength", 0), reverse=True)

    print(f"# {len(promises)} promise(s)\n")
    print(f"{'ID':<8} {'Status':<14} {'Str':>3} {'Planted':>7} {'Promise'}")
    print("-" * 80)
    for p in promises:
        print(f"{p['id']:<8} {p.get('status', '?'):<14} "
              f"{p.get('strength', 0):>3} "
              f"s{p.get('planted_session', 0):>5}  {p['promise'][:50]}")
    return 0


def cmd_strengthen(args) -> int:
    """Increase a promise's strength — call when the promise is referenced."""
    data = _load(args.campaign)
    promise = _find(data, args.id)
    if not promise:
        print(f"# promise '{args.id}' not found", file=sys.stderr)
        return 1

    old = promise.get("strength", 0)
    promise["strength"] = min(100, old + args.amount)

    # Record the reference
    promise.setdefault("references", []).append({
        "session": args.session,
        "amount": args.amount,
        "context": args.context or "",
    })

    # Auto-transition to "strengthening" if strength crosses 40
    if promise["strength"] >= 40 and promise.get("status") == "open":
        promise["status"] = "strengthening"

    _save(args.campaign, data)
    print(f"OK — promise strengthened: {args.id}")
    print(f"  {old} → {promise['strength']} (+{args.amount})")
    print(f"  status: {promise['status']}")
    if promise["strength"] >= 70:
        print(f"  ⚠ HIGH STRENGTH — players are strongly expecting fulfillment")
    return 0


def cmd_fulfill(args) -> int:
    """Fulfill a promise — deliver the answer/payoff."""
    data = _load(args.campaign)
    promise = _find(data, args.id)
    if not promise:
        print(f"# promise '{args.id}' not found", file=sys.stderr)
        return 1

    old = promise.get("status", "open")
    promise["status"] = "fulfilled"
    promise["fulfilled_session"] = args.session
    promise["final_strength"] = promise.get("strength", 0)

    _save(args.campaign, data)
    print(f"OK — promise FULFILLED: {args.id}")
    print(f"  promise: {promise['promise']}")
    print(f"  was: {old}")
    print(f"  strength at fulfillment: {promise['final_strength']}")
    if promise.get("fulfillment_condition"):
        print(f"  condition: {promise['fulfillment_condition']}")
    print(f"\n  → narrative tension released")
    return 0


def cmd_break(args) -> int:
    """Break a promise — the GM failed to deliver. Creates player frustration."""
    data = _load(args.campaign)
    promise = _find(data, args.id)
    if not promise:
        print(f"# promise '{args.id}' not found", file=sys.stderr)
        return 1

    old = promise.get("status", "open")
    promise["status"] = "broken"
    promise["break_reason"] = args.reason
    promise["broken_session"] = args.session
    promise["final_strength"] = promise.get("strength", 0)

    _save(args.campaign, data)
    print(f"OK — promise BROKEN: {args.id}")
    print(f"  promise: {promise['promise']}")
    print(f"  reason: {args.reason}")
    print(f"  strength at break: {promise['final_strength']}")
    print(f"\n  ⚠ player frustration created — consider compensating with a different payoff")
    return 0


def cmd_check_expiration(args) -> int:
    """Find promises that have expired (unfulfilled past their expiration)."""
    data = _load(args.campaign)
    expired = []

    for promise in data["promises"]:
        if promise.get("status") not in ("open", "strengthening"):
            continue
        exp = promise.get("expiration", "")
        if not exp:
            continue

        # Parse session:N
        if exp.startswith("session:"):
            try:
                exp_session = int(exp.split(":")[1])
            except (ValueError, IndexError):
                continue
            if exp_session <= args.to_session:
                expired.append(promise)

    if not expired:
        print(f"# no expired promises by session {args.to_session}")
        return 0

    print(f"# {len(expired)} expired promise(s) — auto-breaking\n")
    for promise in expired:
        promise["status"] = "broken"
        promise["break_reason"] = f"expired ({promise.get('expiration')}) — never fulfilled"
        promise["broken_session"] = args.to_session
        promise["final_strength"] = promise.get("strength", 0)

        print(f"  {promise['id']}: {promise['promise'][:60]}")
        print(f"    strength: {promise['final_strength']}")
        print(f"    planted: session {promise.get('planted_session', '?')}")
        print(f"    expired: {promise.get('expiration')}")
        print()

    _save(args.campaign, data)
    print(f"## Diff for state.md:")
    for p in expired:
        print(f"  - broken promise: {p['promise'][:50]} (strength was {p['final_strength']})")
    return 0


def cmd_pressing(args) -> int:
    """Show promises that are pressing — high strength + open/strengthening."""
    data = _load(args.campaign)
    pressing = [p for p in data["promises"]
                if p.get("status") in ("open", "strengthening")
                and p.get("strength", 0) >= 50]

    if not pressing:
        print(f"# no pressing promises — narrative tension is manageable")
        return 0

    pressing.sort(key=lambda p: p.get("strength", 0), reverse=True)

    print(f"# {len(pressing)} pressing promise(s) — players are expecting fulfillment\n")
    for p in pressing:
        strength = p.get("strength", 0)
        bar = "█" * (strength // 5) + "░" * (20 - strength // 5)
        print(f"  {p['id']}: {p['promise'][:60]}")
        print(f"    strength: {strength:>3} {bar}")
        print(f"    planted: session {p.get('planted_session', '?')}")
        if p.get("expiration"):
            print(f"    expires: {p['expiration']}")
        if p.get("references"):
            print(f"    referenced: {len(p['references'])} time(s)")
        print()

    print(f"## Director guidance:")
    high = [p for p in pressing if p.get("strength", 0) >= 70]
    if high:
        print(f"  {len(high)} promise(s) at strength ≥70 — fulfill soon or risk frustration")
    return 0


# ── CLI ─────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--campaign", required=True, help="Campaign name")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("add", help="Plant a narrative promise")
    s.add_argument("json", help="Promise JSON")
    s.add_argument("--session", type=int, default=0)
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("list", help="List promises")
    s.add_argument("--status", choices=PROMISE_STATES)
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("strengthen", help="Increase a promise's strength")
    s.add_argument("--id", required=True)
    s.add_argument("--amount", type=int, default=10)
    s.add_argument("--session", type=int, default=0)
    s.add_argument("--context", help="Why it was strengthened")
    s.set_defaults(func=cmd_strengthen)

    s = sub.add_parser("fulfill", help="Fulfill a promise")
    s.add_argument("--id", required=True)
    s.add_argument("--session", type=int, default=0)
    s.set_defaults(func=cmd_fulfill)

    s = sub.add_parser("break", help="Break a promise (creates frustration)")
    s.add_argument("--id", required=True)
    s.add_argument("--reason", required=True)
    s.add_argument("--session", type=int, default=0)
    s.set_defaults(func=cmd_break)

    s = sub.add_parser("check-expiration", help="Find and auto-break expired promises")
    s.add_argument("--to-session", type=int, required=True)
    s.set_defaults(func=cmd_check_expiration)

    s = sub.add_parser("pressing", help="Show high-strength unfulfilled promises")
    s.set_defaults(func=cmd_pressing)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
