#!/usr/bin/env python3
"""
epistemic_filter.py — per-fact knowledge filtering for scene assembly.

When assembling the LLM prompt, this module filters what information is
available based on who is present. If Sheriff Brianna is in the room,
the LLM should only see facts Brianna knows — not facts Velkyn knows.

This prevents the most common immersion-breaking failure in AI TTRPGs:
NPCs leaking omniscient knowledge during dialogue.

How it works:
  1. The scene loader calls filter_facts_for_scene() with the list of
     present NPCs and the campaign name.
  2. The module reads epistemology.json and entity_state.json.
  3. For each fact, it checks which present NPCs "know" it (via their
     belief accuracy in epistemology.json and known_facts in entity_state).
  4. It returns two lists:
     - revealed_to_party: facts the party knows (player_knowledge != "unknown")
     - npc_accessible: facts at least one present NPC knows (for NPC dialogue)
     - hidden_from_all: facts no one present knows (excluded from prompt)

Usage:
  python3 epistemic_filter.py --campaign <name> filter --present "velkyn,sheriff"
  python3 epistemic_filter.py --campaign <name> what-can-say --npc sheriff --present "sheriff,velkyn"
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from paths import find_campaign


def _load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _norm_id(name: str) -> str:
    if ":" in name:
        return name
    return f"npc:{name}"


def _load_epistemology(campaign: str) -> list:
    p = find_campaign(campaign) / "epistemology.json"
    return _load_json(p, {"facts": []}).get("facts", [])


def _load_entity_state(campaign: str) -> dict:
    p = find_campaign(campaign) / "entity_state.json"
    return _load_json(p, {"entities": {}}).get("entities", {})


def _npc_knows_fact(npc_id: str, fact: dict, entities: dict) -> bool:
    """Check if an NPC knows a fact, based on:
    1. epistemology.json: npc_beliefs[npc_id].accuracy in ("correct", "knows_partial")
    2. entity_state.json: known_facts contains fact_id
    3. epistemology.json: player_knowledge == "known" AND npc was present at reveal
    """
    # Check epistemology beliefs
    beliefs = fact.get("npc_beliefs", {})
    belief = beliefs.get(npc_id, {})
    accuracy = belief.get("accuracy", "")
    if accuracy in ("correct", "knows_partial"):
        return True

    # Check entity_state known_facts
    entity = entities.get(npc_id, {})
    known_facts = entity.get("known_facts", [])
    for kf in known_facts:
        if isinstance(kf, dict) and kf.get("fact_id") == fact.get("id"):
            return True
        if isinstance(kf, str) and kf == fact.get("id"):
            return True

    return False


def filter_facts_for_scene(campaign: str, present_npcs: list[str]) -> dict:
    """Filter all epistemic facts based on who is present.

    Returns:
      {
        "party_knows": [facts the party knows — can be narrated],
        "npc_accessible": [facts at least one present NPC knows — NPCs can reference],
        "hidden_from_all": [facts no one present knows — excluded from prompt],
        "conflict_facts": [facts where present NPCs disagree — dramatic potential],
      }
    """
    facts = _load_epistemology(campaign)
    entities = _load_entity_state(campaign)

    present_ids = [_norm_id(n) for n in present_npcs]

    party_knows = []
    npc_accessible = []
    hidden_from_all = []
    conflict_facts = []

    for fact in facts:
        fact_id = fact.get("id", "?")
        truth = fact.get("truth", "?")

        # Does the party know this fact?
        pk = fact.get("player_knowledge", "unknown")
        if pk in ("known", "suspected", "rumored"):
            party_knows.append({"id": fact_id, "truth": truth,
                                "player_knowledge": pk})

        # Which present NPCs know this fact?
        knowers = []
        for npc_id in present_ids:
            if _npc_knows_fact(npc_id, fact, entities):
                knowers.append(npc_id)

        if knowers:
            npc_accessible.append({"id": fact_id, "truth": truth,
                                   "known_by": knowers})
        elif pk == "unknown":
            hidden_from_all.append({"id": fact_id, "truth": truth})

        # Check for conflicts — present NPCs with different beliefs
        beliefs = fact.get("npc_beliefs", {})
        present_beliefs = {}
        for npc_id in present_ids:
            if npc_id in beliefs:
                present_beliefs[npc_id] = beliefs[npc_id].get("accuracy", "ignorant")

        accuracies = set(present_beliefs.values())
        if len(accuracies) > 1 and "ignorant" not in accuracies:
            conflict_facts.append({"id": fact_id, "truth": truth,
                                   "beliefs": present_beliefs})

    return {
        "party_knows": party_knows,
        "npc_accessible": npc_accessible,
        "hidden_from_all": hidden_from_all,
        "conflict_facts": conflict_facts,
    }


def what_npc_can_say(campaign: str, npc_id: str, present_npcs: list[str]) -> dict:
    """What can a specific NPC reference in dialogue?

    Returns facts the NPC knows, filtered to exclude things only other
    present NPCs know (to prevent the NPC from "learning" from someone
    else's knowledge during the same scene).
    """
    npc_id = _norm_id(npc_id)
    facts = _load_epistemology(campaign)
    entities = _load_entity_state(campaign)

    can_say = []
    must_hide = []

    for fact in facts:
        if _npc_knows_fact(npc_id, fact, entities):
            can_say.append({
                "id": fact.get("id", "?"),
                "truth": fact.get("truth", "?"),
                "player_knowledge": fact.get("player_knowledge", "unknown"),
            })
        else:
            must_hide.append({
                "id": fact.get("id", "?"),
                "truth": fact.get("truth", "?"),
                "reason": "NPC does not know this fact",
            })

    return {
        "npc": npc_id,
        "can_reference": can_say,
        "must_not_reference": must_hide,
        "prompt_directive": (
            f"{npc_id} can only reference facts they know. "
            f"They must NOT reference: {', '.join(f['id'] for f in must_hide[:3])}. "
            f"If asked about something they don't know, they should deflect, "
            f"lie, or admit ignorance — not reveal meta-knowledge."
        ),
    }


# ── CLI ─────────────────────────────────────────────────────────────────────

def cmd_filter(args) -> int:
    present = [n.strip() for n in args.present.split(",") if n.strip()]
    result = filter_facts_for_scene(args.campaign, present)

    print(f"# epistemic filter for scene\n")
    print(f"  present: {', '.join(present)}")
    print(f"  party knows: {len(result['party_knows'])} fact(s)")
    print(f"  NPC accessible: {len(result['npc_accessible'])} fact(s)")
    print(f"  hidden from all: {len(result['hidden_from_all'])} fact(s)")
    print(f"  conflict facts: {len(result['conflict_facts'])} fact(s)")

    if result["party_knows"]:
        print(f"\n## Party can reference:")
        for f in result["party_knows"]:
            print(f"  {f['id']}: [{f['player_knowledge']}] {f['truth'][:60]}")

    if result["npc_accessible"]:
        print(f"\n## NPCs can reference:")
        for f in result["npc_accessible"]:
            print(f"  {f['id']}: known by {', '.join(f['known_by'])} — {f['truth'][:60]}")

    if result["conflict_facts"]:
        print(f"\n## ⚠ Dramatic conflicts (NPCs disagree):")
        for f in result["conflict_facts"]:
            print(f"  {f['id']}: {f['truth'][:50]}")
            for npc, acc in f["beliefs"].items():
                print(f"    {npc}: {acc}")

    if result["hidden_from_all"]:
        print(f"\n## Hidden from everyone present ({len(result['hidden_from_all'])} fact(s)):")
        for f in result["hidden_from_all"]:
            print(f"  {f['id']}: {f['truth'][:60]}")

    return 0


def cmd_what_can_say(args) -> int:
    present = [n.strip() for n in args.present.split(",") if n.strip()]
    result = what_npc_can_say(args.campaign, args.npc, present)

    print(f"# what {args.npc} can say\n")
    print(f"  can reference: {len(result['can_reference'])} fact(s)")
    print(f"  must hide: {len(result['must_not_reference'])} fact(s)")

    if result["can_reference"]:
        print(f"\n## Can reference:")
        for f in result["can_reference"]:
            print(f"  {f['id']}: [{f['player_knowledge']}] {f['truth'][:60]}")

    if result["must_not_reference"]:
        print(f"\n## ⚠ Must NOT reference:")
        for f in result["must_not_reference"]:
            print(f"  {f['id']}: {f['truth'][:60]}")

    print(f"\n## Prompt directive:")
    print(f"  {result['prompt_directive']}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--campaign", required=True)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("filter", help="Filter all facts by who's present")
    s.add_argument("--present", required=True, help="Comma-separated NPC names")
    s.set_defaults(func=cmd_filter)

    s = sub.add_parser("what-can-say", help="What a specific NPC can reference")
    s.add_argument("--npc", required=True)
    s.add_argument("--present", required=True, help="All present NPCs (comma-separated)")
    s.set_defaults(func=cmd_what_can_say)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
