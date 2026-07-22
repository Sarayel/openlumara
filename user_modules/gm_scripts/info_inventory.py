#!/usr/bin/env python3
"""
info_inventory.py — track information as an inventory.

Who has learned what, when, and from whom. Different from the epistemology
layer (which tracks truth/belief/knowledge) — this tracks the *flow* of
information through the campaign. The GM can query "what does the party
know?" or "who told Velkyn about the ledger?" or "what does the Sheriff
know that the party doesn't?"

Information is tracked as items in an inventory:
  - fact_id (links to epistemology.json)
  - known_by (list of who knows this fact)
  - learned_from (who told them)
  - learned_session (when)
  - source (how: witness, document, interrogation, rumor, inference)
  - confidence (confirmed/suspected/rumor — mirrors epistemology)
  - context (brief note on how they learned)

Storage: <campaign-dir>/info_inventory.json

Usage:
  python3 info_inventory.py add --campaign <name> '<json>'
  python3 info_inventory.py who-knows --campaign <name> --fact f001
  python3 info_inventory.py what-knows --campaign <name> --npc velkyn
  python3 info_inventory.py party-knows --campaign <name>
  python3 info_inventory.py info-gap --campaign <name> --npc sheriff --party
  python3 info_inventory.py trace --campaign <name> --fact f001
  python3 info_inventory.py list --campaign <name>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from paths import find_campaign


# ── IO ──────────────────────────────────────────────────────────────────────

def _inv_path(campaign: str) -> Path:
    return find_campaign(campaign) / "info_inventory.json"


def _load(campaign: str) -> dict:
    p = _inv_path(campaign)
    if not p.exists():
        return {"version": 1, "items": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "items": []}
    data.setdefault("version", 1)
    data.setdefault("items", [])
    return data


def _save(campaign: str, data: dict) -> None:
    p = _inv_path(campaign)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _norm_id(name: str) -> str:
    if ":" in name:
        return name
    return f"npc:{name}"


def _find_items_for_fact(data: dict, fact_id: str) -> list:
    return [item for item in data["items"] if item.get("fact_id") == fact_id]


# ── Commands ────────────────────────────────────────────────────────────────

INFO_SOURCES = ("witness", "document", "interrogation", "rumor",
                "inference", "intercept", "confession", "discovery")


def cmd_add(args) -> int:
    """Add an information item to the inventory."""
    data = _load(args.campaign)
    try:
        item = json.loads(args.json)
    except json.JSONDecodeError as e:
        print(f"error: invalid JSON: {e}", file=sys.stderr)
        return 1

    if "fact_id" not in item or "known_by" not in item:
        print("error: item must have 'fact_id' and 'known_by'", file=sys.stderr)
        return 1

    item.setdefault("id", f"info{len(data['items']) + 1:03d}")
    item.setdefault("learned_from", None)
    item.setdefault("learned_session", args.session or 0)
    item.setdefault("source", "discovery")
    item.setdefault("confidence", "confirmed")
    item.setdefault("context", "")

    # Normalize known_by IDs
    item["known_by"] = [_norm_id(n) for n in item["known_by"]]
    if item.get("learned_from"):
        item["learned_from"] = _norm_id(item["learned_from"])

    data["items"].append(item)
    _save(args.campaign, data)

    print(f"OK — info item '{item['id']}' added")
    print(f"  fact: {item['fact_id']}")
    print(f"  known by: {', '.join(item['known_by'])}")
    print(f"  source: {item['source']}")
    print(f"  confidence: {item['confidence']}")
    return 0


def cmd_who_knows(args) -> int:
    """Show who knows a given fact."""
    data = _load(args.campaign)
    items = _find_items_for_fact(data, args.fact)

    if not items:
        print(f"# no one knows fact '{args.fact}' (or it hasn't been tracked)")
        return 0

    print(f"# who knows: {args.fact}\n")
    all_knowers = set()
    for item in items:
        for npc in item["known_by"]:
            all_knowers.add((npc, item.get("confidence", "?"), item.get("source", "?"),
                             item.get("learned_session", "?")))

    for npc, conf, source, session in sorted(all_knowers):
        print(f"  {npc:<25} [{conf}] via {source} (session {session})")
    return 0


def cmd_what_knows(args) -> int:
    """Show what a given NPC knows."""
    data = _load(args.campaign)
    npc_id = _norm_id(args.npc)

    items = [item for item in data["items"] if npc_id in item.get("known_by", [])]

    if not items:
        print(f"# '{args.npc}' doesn't know any tracked facts")
        return 0

    items.sort(key=lambda x: x.get("learned_session", 0))
    print(f"# what {args.npc} knows ({len(items)} facts)\n")
    for item in items:
        conf = item.get("confidence", "?")
        source = item.get("source", "?")
        session = item.get("learned_session", "?")
        from_who = item.get("learned_from", "—")
        print(f"  [s{session:>3}] {item['fact_id']:<10} [{conf}] via {source} from {from_who}")
    return 0


def cmd_party_knows(args) -> int:
    """Show everything the party knows (any pc: prefix)."""
    data = _load(args.campaign)

    party_items = []
    for item in data["items"]:
        party_knowers = [n for n in item.get("known_by", []) if n.startswith("pc:")]
        if party_knowers:
            party_items.append((item, party_knowers))

    if not party_items:
        print(f"# party doesn't know any tracked facts")
        return 0

    party_items.sort(key=lambda x: x[0].get("learned_session", 0))
    print(f"# party knowledge ({len(party_items)} facts)\n")
    for item, knowers in party_items:
        conf = item.get("confidence", "?")
        session = item.get("learned_session", "?")
        print(f"  [s{session:>3}] {item['fact_id']:<10} [{conf}] known by: {', '.join(knowers)}")
    return 0


def cmd_info_gap(args) -> int:
    """Find facts that one party knows but another doesn't — information asymmetry."""
    data = _load(args.campaign)
    npc_id = _norm_id(args.npc)

    if args.party:
        # Find what the NPC knows that the party doesn't
        npc_items = [item for item in data["items"] if npc_id in item.get("known_by", [])]
        party_known_facts = set()
        for item in data["items"]:
            if any(n.startswith("pc:") for n in item.get("known_by", [])):
                party_known_facts.add(item["fact_id"])

        gaps = [item for item in npc_items if item["fact_id"] not in party_known_facts]

        if not gaps:
            print(f"# no information gap — {args.npc} and the party know the same facts")
            return 0

        print(f"# {args.npc} knows {len(gaps)} fact(s) the party doesn't:\n")
        for item in gaps:
            print(f"  {item['fact_id']}: [{item.get('confidence', '?')}] via {item.get('source', '?')}")
            if item.get("context"):
                print(f"    context: {item['context']}")
        return 0

    # Default: what the party knows that the NPC doesn't
    party_items = [item for item in data["items"]
                   if any(n.startswith("pc:") for n in item.get("known_by", []))]
    npc_known_facts = set()
    for item in data["items"]:
        if npc_id in item.get("known_by", []):
            npc_known_facts.add(item["fact_id"])

    gaps = [item for item in party_items if item["fact_id"] not in npc_known_facts]

    if not gaps:
        print(f"# no information gap — the party and {args.npc} know the same facts")
        return 0

    print(f"# the party knows {len(gaps)} fact(s) that {args.npc} doesn't:\n")
    for item in gaps:
        print(f"  {item['fact_id']}: [{item.get('confidence', '?')}]")
    return 0


def cmd_trace(args) -> int:
    """Trace how a fact propagated through the campaign."""
    data = _load(args.campaign)
    items = _find_items_for_fact(data, args.fact)

    if not items:
        print(f"# no tracking data for fact '{args.fact}'")
        return 0

    items.sort(key=lambda x: x.get("learned_session", 0))
    print(f"# information flow: {args.fact}\n")

    for item in items:
        knowers = ", ".join(item["known_by"])
        from_who = item.get("learned_from", "—")
        source = item.get("source", "?")
        session = item.get("learned_session", "?")
        conf = item.get("confidence", "?")

        print(f"  [s{session:>3}] {from_who} → {knowers}")
        print(f"         via {source} [{conf}]")
        if item.get("context"):
            print(f"         {item['context']}")
        print()
    return 0


def cmd_list(args) -> int:
    """List all info items."""
    data = _load(args.campaign)
    if not data["items"]:
        print(f"# no info items tracked")
        return 0

    print(f"# {len(data['items'])} info item(s)\n")
    print(f"{'ID':<10} {'Fact':<10} {'Known By':<30} {'Source':<14} {'Sess':>4}")
    print("-" * 75)
    for item in data["items"]:
        knowers = ", ".join(item["known_by"][:3])
        if len(item["known_by"]) > 3:
            knowers += f" +{len(item['known_by']) - 3}"
        print(f"{item['id']:<10} {item['fact_id']:<10} {knowers:<30} "
              f"{item.get('source', '?'):<14} "
              f"{item.get('learned_session', 0):>4}")
    return 0


# ── CLI ─────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--campaign", required=True, help="Campaign name")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("add", help="Add an info item")
    s.add_argument("json", help="Item JSON")
    s.add_argument("--session", type=int, default=0)
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("who-knows", help="Who knows a given fact")
    s.add_argument("--fact", required=True)
    s.set_defaults(func=cmd_who_knows)

    s = sub.add_parser("what-knows", help="What does an NPC know")
    s.add_argument("--npc", required=True)
    s.set_defaults(func=cmd_what_knows)

    s = sub.add_parser("party-knows", help="What does the party know")
    s.set_defaults(func=cmd_party_knows)

    s = sub.add_parser("info-gap", help="Find information asymmetry")
    s.add_argument("--npc", required=True)
    s.add_argument("--party", action="store_true",
                   help="What does the NPC know that the party doesn't?")
    s.set_defaults(func=cmd_info_gap)

    s = sub.add_parser("trace", help="Trace how a fact propagated")
    s.add_argument("--fact", required=True)
    s.set_defaults(func=cmd_trace)

    s = sub.add_parser("list", help="List all info items")
    s.set_defaults(func=cmd_list)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
