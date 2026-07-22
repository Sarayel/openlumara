#!/usr/bin/env python3
"""
entity_state.py — single canonical NPC state table.

Consolidates NPC disposition, trust, favors, known secrets, and last
interaction into ONE file. scene_index.py and plans.py should read from
and write to this instead of maintaining parallel disposition fields.

This solves the "three sources of truth" problem:
  - plans.json had disposition_toward_party + trust
  - scene_index.json had per-scene deltas with disposition changes
  - The proposed entity_state.json would have duplicated all of that

Now entity_state.json IS the live NPC state table. Other scripts write
deltas to it; they don't maintain their own copies.

Storage: <campaign-dir>/entity_state.json

Usage:
  python3 entity_state.py show --campaign <name> --npc velkyn
  python3 entity_state.py update --campaign <name> --npc velkyn --field trust --value -2
  python3 entity_state.py add-favor --campaign <name> --npc dore --owed-by party
  python3 entity_state.py resolve-favor --campaign <name> --npc dore
  python3 entity_state.py add-secret --campaign <name> --npc velkyn --secret "pale_court_leylines"
  python3 entity_state.py card --campaign <name> --npc dore  # compact card for LLM
  python3 entity_state.py list --campaign <name>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from paths import find_campaign


def _path(campaign: str) -> Path:
    return find_campaign(campaign) / "entity_state.json"


def _load(campaign: str) -> dict:
    p = _path(campaign)
    if not p.exists():
        return {"version": 1, "entities": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "entities": {}}
    data.setdefault("version", 1)
    data.setdefault("entities", {})
    return data


def _save(campaign: str, data: dict) -> None:
    p = _path(campaign)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _norm_id(name: str) -> str:
    if ":" in name:
        return name
    return f"npc:{name}"


def _get_or_create(data: dict, npc_id: str) -> dict:
    entities = data.setdefault("entities", {})
    if npc_id not in entities:
        entities[npc_id] = {
            "name": npc_id.split(":")[-1],
            "relationship_to_party": "neutral",
            "trust": 0,
            "favors_owed": [],
            "favors_owed_to": [],
            "known_secrets": [],
            "known_facts": [],
            "last_interaction": None,
            "last_interaction_session": 0,
            "disposition_history": [],
        }
    return entities[npc_id]


# ── Commands ────────────────────────────────────────────────────────────────

def cmd_show(args) -> int:
    data = _load(args.campaign)
    npc_id = _norm_id(args.npc)
    entity = data.get("entities", {}).get(npc_id)
    if not entity:
        print(f"# no entity state for '{args.npc}'")
        return 1
    print(json.dumps(entity, indent=2, ensure_ascii=False))
    return 0


def cmd_update(args) -> int:
    """Update a single field on an entity."""
    data = _load(args.campaign)
    npc_id = _norm_id(args.npc)
    entity = _get_or_create(data, npc_id)

    field = args.field
    # Try to parse value as int, float, or string
    try:
        value = int(args.value)
    except ValueError:
        try:
            value = float(args.value)
        except ValueError:
            value = args.value

    old = entity.get(field)
    entity[field] = value

    # Track disposition changes
    if field in ("relationship_to_party", "trust"):
        entity.setdefault("disposition_history", []).append({
            "field": field,
            "old": old,
            "new": value,
            "session": args.session,
        })

    _save(args.campaign, data)
    print(f"OK — {npc_id}.{field}: {old} → {value}")
    return 0


def cmd_add_favor(args) -> int:
    """Record that a favor is owed."""
    data = _load(args.campaign)
    npc_id = _norm_id(args.npc)
    entity = _get_or_create(data, npc_id)

    if args.owed_by == "party":
        # Party owes NPC a favor
        entity.setdefault("favors_owed_to", []).append({
            "description": args.description or "unspecified favor",
            "session": args.session,
            "resolved": False,
        })
    else:
        # NPC owes party a favor
        entity.setdefault("favors_owed", []).append({
            "description": args.description or "unspecified favor",
            "session": args.session,
            "resolved": False,
        })

    _save(args.campaign, data)
    direction = "party owes NPC" if args.owed_by == "party" else "NPC owes party"
    print(f"OK — favor recorded: {direction}")
    print(f"  description: {args.description or 'unspecified'}")
    return 0


def cmd_resolve_favor(args) -> int:
    """Mark a favor as resolved."""
    data = _load(args.campaign)
    npc_id = _norm_id(args.npc)
    entity = data.get("entities", {}).get(npc_id)
    if not entity:
        print(f"# no entity state for '{args.npc}'", file=sys.stderr)
        return 1

    resolved = False
    for favor in entity.get("favors_owed", []):
        if not favor.get("resolved"):
            favor["resolved"] = True
            favor["resolved_session"] = args.session
            resolved = True
            break
    if not resolved:
        for favor in entity.get("favors_owed_to", []):
            if not favor.get("resolved"):
                favor["resolved"] = True
                favor["resolved_session"] = args.session
                resolved = True
                break

    if resolved:
        _save(args.campaign, data)
        print(f"OK — favor resolved for {npc_id}")
    else:
        print(f"# no unresolved favors for {npc_id}")
    return 0


def cmd_add_secret(args) -> int:
    """Record that an NPC knows a secret."""
    data = _load(args.campaign)
    npc_id = _norm_id(args.npc)
    entity = _get_or_create(data, npc_id)

    if args.secret not in entity.get("known_secrets", []):
        entity.setdefault("known_secrets", []).append(args.secret)

    _save(args.campaign, data)
    print(f"OK — {npc_id} now knows secret: {args.secret}")
    return 0


def cmd_add_fact(args) -> int:
    """Record that an NPC knows a fact (epistemic linkage)."""
    data = _load(args.campaign)
    npc_id = _norm_id(args.npc)
    entity = _get_or_create(data, npc_id)

    fact_entry = {"fact_id": args.fact_id}
    if args.confidence:
        fact_entry["confidence"] = args.confidence

    if fact_entry not in entity.get("known_facts", []):
        entity.setdefault("known_facts", []).append(fact_entry)

    _save(args.campaign, data)
    print(f"OK — {npc_id} now knows fact {args.fact_id}" + 
          f" ({args.confidence})" if args.confidence else "")
    return 0


def cmd_card(args) -> int:
    """Output a compact entity card for LLM context — condenses full state
    into ~200 tokens."""
    data = _load(args.campaign)
    npc_id = _norm_id(args.npc)
    entity = data.get("entities", {}).get(npc_id)
    if not entity:
        print(f"# no entity state for '{args.npc}'")
        return 1

    lines = [f"**{entity.get('name', npc_id)}**"]
    lines.append(f"  relationship: {entity.get('relationship_to_party', 'neutral')}")
    lines.append(f"  trust: {entity.get('trust', 0)}")

    favors_owed = [f for f in entity.get("favors_owed", []) if not f.get("resolved")]
    favors_to = [f for f in entity.get("favors_owed_to", []) if not f.get("resolved")]
    if favors_owed:
        lines.append(f"  owes party: {len(favors_owed)} favor(s)")
    if favors_to:
        lines.append(f"  party owes: {len(favors_to)} favor(s)")

    secrets = entity.get("known_secrets", [])
    if secrets:
        lines.append(f"  knows secrets: {', '.join(secrets[:3])}")

    facts = entity.get("known_facts", [])
    if facts:
        fact_ids = [f["fact_id"] if isinstance(f, dict) else str(f) for f in facts[:5]]
        lines.append(f"  knows facts: {', '.join(fact_ids)}")

    if entity.get("last_interaction"):
        lines.append(f"  last seen: {entity['last_interaction']} (s{entity.get('last_interaction_session', 0)})")

    print("\n".join(lines))
    return 0


def cmd_list(args) -> int:
    """List all tracked entities."""
    data = _load(args.campaign)
    entities = data.get("entities", {})
    if not entities:
        print(f"# no entities tracked")
        return 0

    print(f"# {len(entities)} entit(y/ies)\n")
    print(f"{'NPC':<25} {'Relationship':<15} {'Trust':>5} {'Favors':>6} {'Secrets':>7}")
    print("-" * 65)
    for npc_id, entity in sorted(entities.items()):
        name = entity.get("name", npc_id)
        rel = entity.get("relationship_to_party", "?")[:15]
        trust = entity.get("trust", 0)
        favors = len([f for f in entity.get("favors_owed", []) if not f.get("resolved")])
        secrets = len(entity.get("known_secrets", []))
        print(f"{name:<25} {rel:<15} {trust:>5} {favors:>6} {secrets:>7}")
    return 0


def cmd_sync_from_plans(args) -> int:
    """One-time sync: pull disposition and trust from plans.json into
    entity_state.json. Run this once when adopting entity_state for an
    existing campaign."""
    plans_path = find_campaign(args.campaign) / "plans.json"
    if not plans_path.exists():
        print(f"# no plans.json found")
        return 1

    plans_data = json.loads(plans_path.read_text(encoding="utf-8"))
    data = _load(args.campaign)

    synced = 0
    for npc_id, npc in plans_data.get("npcs", {}).items():
        entity = _get_or_create(data, npc_id)
        entity["name"] = npc.get("name", npc_id.split(":")[-1])
        entity["relationship_to_party"] = npc.get("disposition_toward_party", "neutral")
        entity["trust"] = npc.get("trust", 0)
        synced += 1

    _save(args.campaign, data)
    print(f"OK — synced {synced} NPC(s) from plans.json to entity_state.json")
    return 0


# ── CLI ─────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--campaign", required=True)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("show")
    s.add_argument("--npc", required=True)
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("update")
    s.add_argument("--npc", required=True)
    s.add_argument("--field", required=True)
    s.add_argument("--value", required=True)
    s.add_argument("--session", type=int, default=0)
    s.set_defaults(func=cmd_update)

    s = sub.add_parser("add-favor")
    s.add_argument("--npc", required=True)
    s.add_argument("--owed-by", required=True, choices=["party", "npc"])
    s.add_argument("--description", default="")
    s.add_argument("--session", type=int, default=0)
    s.set_defaults(func=cmd_add_favor)

    s = sub.add_parser("resolve-favor")
    s.add_argument("--npc", required=True)
    s.add_argument("--session", type=int, default=0)
    s.set_defaults(func=cmd_resolve_favor)

    s = sub.add_parser("add-secret")
    s.add_argument("--npc", required=True)
    s.add_argument("--secret", required=True)
    s.set_defaults(func=cmd_add_secret)

    s = sub.add_parser("add-fact")
    s.add_argument("--npc", required=True)
    s.add_argument("--fact-id", required=True)
    s.add_argument("--confidence", choices=["confirmed", "suspected", "rumor"])
    s.set_defaults(func=cmd_add_fact)

    s = sub.add_parser("card")
    s.add_argument("--npc", required=True)
    s.set_defaults(func=cmd_card)

    s = sub.add_parser("list")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("sync-from-plans")
    s.set_defaults(func=cmd_sync_from_plans)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
