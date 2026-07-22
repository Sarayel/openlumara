#!/usr/bin/env python3
"""
party_turn.py — party turn management for local multiplayer.

Tracks whose turn it is outside combat, rotates turns, handles skips,
and provides concurrent action resolution guidance.

The display companion already stages multiple players' actions via the
Party Input panel. This script adds the GM-facing layer: turn order,
"who's next", "who hasn't spoken", and concurrent action resolution.

Works alongside the existing single-player flow — if there's only one
PC, party mode is a no-op.

Storage: <campaign-dir>/party_turn.json

Usage:
  python3 party_turn.py init --campaign <name> --members "Aldric,Piper,Kael"
  python3 party_turn.py status --campaign <name>
  python3 party_turn.py next --campaign <name>
  python3 party_turn.py skip --campaign <name> --member Aldric
  python3 party_turn.py set --campaign <name> --member Piper
  python3 party_turn.py add --campaign <name> --member NewChar
  python3 party_turn.py remove --campaign <name> --member LeavingChar
  python3 party_turn.py rotate --campaign <name>   # advance to next who hasn't acted
  python3 party_turn.py reset-round --campaign <name>  # new scene/round, clear acted flags
  python3 party_turn.py waiting --campaign <name>  # who hasn't acted this round?
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from paths import find_campaign


# ── IO ──────────────────────────────────────────────────────────────────────

def _turn_path(campaign: str) -> Path:
    return find_campaign(campaign) / "party_turn.json"


def _load(campaign: str) -> dict:
    p = _turn_path(campaign)
    if not p.exists():
        return {"version": 1, "members": [], "current": None, "acted": {}, "round": 0}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "members": [], "current": None, "acted": {}, "round": 0}
    data.setdefault("version", 1)
    data.setdefault("members", [])
    data.setdefault("current", None)
    data.setdefault("acted", {})
    data.setdefault("round", 0)
    return data


def _save(campaign: str, data: dict) -> None:
    p = _turn_path(campaign)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ── Commands ────────────────────────────────────────────────────────────────

def cmd_init(args) -> int:
    """Initialize the party turn tracker with member names."""
    data = _load(args.campaign)
    members = [m.strip() for m in args.members.split(",") if m.strip()]

    data["members"] = members
    data["current"] = members[0] if members else None
    data["acted"] = {m: False for m in members}
    data["round"] = 1
    data["initiative_order"] = None  # set during combat via combat.py

    _save(args.campaign, data)
    print(f"OK — party initialized with {len(members)} member(s)")
    print(f"  members: {', '.join(members)}")
    print(f"  current: {data['current']}")
    print(f"  round: {data['round']}")
    return 0


def cmd_status(args) -> int:
    """Show current party turn status."""
    data = _load(args.campaign)
    if not data["members"]:
        print(f"# no party initialized — use 'init --members \"Name1,Name2,...\"'")
        return 1

    print(f"# party turn status\n")
    print(f"  round: {data['round']}")
    print(f"  current: {data['current']}\n")

    print(f"  {'Member':<20} {'Acted':>6} {'Status'}")
    print(f"  {'-'*20} {'-'*6} {'-'*20}")
    for m in data["members"]:
        acted = data["acted"].get(m, False)
        acted_str = "✓" if acted else "·"
        status = "current" if m == data["current"] else "acted" if acted else "waiting"
        print(f"  {m:<20} {acted_str:>6} {status}")

    waiting = [m for m in data["members"] if not data["acted"].get(m, False)]
    print(f"\n  waiting: {', '.join(waiting) if waiting else '(all have acted)'}")
    return 0


def cmd_next(args) -> int:
    """Advance to the next party member who hasn't acted this round."""
    data = _load(args.campaign)
    if not data["members"]:
        print(f"# no party initialized", file=sys.stderr)
        return 1

    # Mark current as acted
    if data["current"]:
        data["acted"][data["current"]] = True

    # Find next member who hasn't acted
    current_idx = data["members"].index(data["current"]) if data["current"] in data["members"] else -1
    next_member = None
    for i in range(1, len(data["members"]) + 1):
        idx = (current_idx + i) % len(data["members"])
        candidate = data["members"][idx]
        if not data["acted"].get(candidate, False):
            next_member = candidate
            break

    if next_member is None:
        # Everyone has acted — advance round
        data["round"] += 1
        data["acted"] = {m: False for m in data["members"]}
        next_member = data["members"][0] if data["members"] else None
        print(f"# round {data['round'] - 1} complete — starting round {data['round']}")
    else:
        print(f"# turn advanced")

    data["current"] = next_member
    _save(args.campaign, data)

    print(f"  next: {next_member}")
    waiting = [m for m in data["members"] if not data["acted"].get(m, False) and m != next_member]
    if waiting:
        print(f"  still waiting: {', '.join(waiting)}")
    return 0


def cmd_skip(args) -> int:
    """Mark a member as having acted (skip their turn)."""
    data = _load(args.campaign)
    if args.member not in data["members"]:
        print(f"# '{args.member}' is not a party member", file=sys.stderr)
        return 1

    data["acted"][args.member] = True
    if data["current"] == args.member:
        # Auto-advance
        return cmd_next(args)

    _save(args.campaign, data)
    print(f"OK — {args.member} skipped (marked as acted)")
    return 0


def cmd_set(args) -> int:
    """Set the current turn to a specific member."""
    data = _load(args.campaign)
    if args.member not in data["members"]:
        print(f"# '{args.member}' is not a party member", file=sys.stderr)
        return 1

    data["current"] = args.member
    _save(args.campaign, data)
    print(f"OK — current turn set to {args.member}")
    return 0


def cmd_add(args) -> int:
    """Add a new party member."""
    data = _load(args.campaign)
    if args.member in data["members"]:
        print(f"# '{args.member}' is already a party member")
        return 0

    data["members"].append(args.member)
    data["acted"][args.member] = False
    if not data["current"]:
        data["current"] = args.member

    _save(args.campaign, data)
    print(f"OK — added {args.member} to the party")
    print(f"  members: {', '.join(data['members'])}")
    return 0


def cmd_remove(args) -> int:
    """Remove a party member."""
    data = _load(args.campaign)
    if args.member not in data["members"]:
        print(f"# '{args.member}' is not a party member", file=sys.stderr)
        return 1

    data["members"].remove(args.member)
    data["acted"].pop(args.member, None)
    if data["current"] == args.member:
        data["current"] = data["members"][0] if data["members"] else None

    _save(args.campaign, data)
    print(f"OK — removed {args.member} from the party")
    print(f"  members: {', '.join(data['members'])}")
    return 0


def cmd_rotate(args) -> int:
    """Advance to the next member who hasn't acted. Alias for 'next'."""
    return cmd_next(args)


def cmd_reset_round(args) -> int:
    """Reset acted flags — start a new round/scene."""
    data = _load(args.campaign)
    data["acted"] = {m: False for m in data["members"]}
    data["round"] += 1
    if data["members"]:
        data["current"] = data["members"][0]
    _save(args.campaign, data)
    print(f"OK — round {data['round']} started")
    print(f"  all members can act again")
    if data["current"]:
        print(f"  first up: {data['current']}")
    return 0


def cmd_waiting(args) -> int:
    """Show who hasn't acted this round — for GM 'waiting for' prompts."""
    data = _load(args.campaign)
    waiting = [m for m in data["members"] if not data["acted"].get(m, False)]

    if not waiting:
        print(f"# all party members have acted this round")
        return 0

    print(f"# waiting for: {', '.join(waiting)}")
    return 0


def cmd_concurrent(args) -> int:
    """Show guidance for resolving concurrent actions from multiple players.

    When multiple players stage actions simultaneously, the GM needs to
    decide order. This command provides deterministic resolution guidance:
      1. Actions that affect the environment go first (opening doors, etc.)
      2. Reactive actions (dodge, counter) go last
      3. Among same-type actions, use the party turn order
    """
    data = _load(args.campaign)
    print(f"# concurrent action resolution guide\n")
    print(f"  When multiple players stage actions at once:")
    print(f"  1. Environmental actions first (open door, pick up item, move)")
    print(f"  2. Social actions next (speak, persuade, intimidate)")
    print(f"  3. Combat/physical actions (attack, grapple, flee)")
    print(f"  4. Reactive actions last (dodge, block, counter)")
    print(f"\n  Among same-type actions, use party turn order:")
    for i, m in enumerate(data["members"]):
        marker = "▶" if m == data["current"] else " "
        print(f"    {marker} {i+1}. {m}")
    print(f"\n  Alternative: let players declare intentions, then resolve")
    print(f"  in the order that creates the most interesting scene.")
    return 0


# ── CLI ─────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--campaign", required=True, help="Campaign name")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="Initialize party with member names")
    s.add_argument("--members", required=True, help="Comma-separated member names")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("status", help="Show current party turn status")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("next", help="Advance to next member who hasn't acted")
    s.set_defaults(func=cmd_next)

    s = sub.add_parser("skip", help="Mark a member as having acted")
    s.add_argument("--member", required=True)
    s.set_defaults(func=cmd_skip)

    s = sub.add_parser("set", help="Set current turn to a specific member")
    s.add_argument("--member", required=True)
    s.set_defaults(func=cmd_set)

    s = sub.add_parser("add", help="Add a new party member")
    s.add_argument("--member", required=True)
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("remove", help="Remove a party member")
    s.add_argument("--member", required=True)
    s.set_defaults(func=cmd_remove)

    s = sub.add_parser("rotate", help="Advance to next (alias for next)")
    s.set_defaults(func=cmd_rotate)

    s = sub.add_parser("reset-round", help="Start a new round/scene")
    s.set_defaults(func=cmd_reset_round)

    s = sub.add_parser("waiting", help="Show who hasn't acted this round")
    s.set_defaults(func=cmd_waiting)

    s = sub.add_parser("concurrent", help="Show concurrent action resolution guide")
    s.set_defaults(func=cmd_concurrent)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
