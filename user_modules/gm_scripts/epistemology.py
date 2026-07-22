#!/usr/bin/env python3
"""
epistemology.py — the three-layer truth/belief/knowledge system.

The most important architectural separation in the engine. Most RPGs and
AI GMs collapse three distinct things into one:

  World     — what is objectively true (the GM's secret)
  Belief    — what each NPC believes is true (per-NPC, may be wrong)
  Knowledge — what the players have discovered (tracked separately)

Example:
  World:     Prince has been replaced by a Lasombra double agent
  Belief:    Sheriff believes Prince is the real Prince
             Velkyn knows the truth
             Marquise Dore suspects
  Knowledge: Players believe Prince is real (they haven't discovered it yet)

If the GM conflates these, the system either reveals secrets too early
or narrates NPCs acting on knowledge they shouldn't have.

This script tracks facts at all three layers. The Scene Director and
the LLM renderer both query this system before narrating — so NPCs act
on their BELIEFS (which may be wrong), and players experience the world
through their KNOWLEDGE (which may be incomplete).

Storage: <campaign-dir>/epistemology.json

Usage:
  python3 epistemology.py fact-add --campaign <name> '<json>'
  python3 epistemology.py fact-show --campaign <name> --id f001
  python3 epistemology.py belief-set --campaign <name> --id f001 --npc sheriff --belief "Prince is real"
  python3 epistemology.py belief-check --campaign <name> --id f001 --npc sheriff
  python3 epistemology.py knowledge-set --campaign <name> --id f001 --state suspected
  python3 epistemology.py fact-reveal --campaign <name> --id f001 --session 15
  python3 epistemology.py epistemic-gaps --campaign <name>
  python3 epistemology.py player-misconceptions --campaign <name>
  python3 epistemology.py who-knows --campaign <name> --id f001
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from paths import find_campaign


# ── IO ──────────────────────────────────────────────────────────────────────

def _epist_path(campaign: str) -> Path:
    return find_campaign(campaign) / "epistemology.json"


def _load(campaign: str) -> dict:
    p = _epist_path(campaign)
    if not p.exists():
        return {"version": 1, "facts": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "facts": []}
    data.setdefault("version", 1)
    data.setdefault("facts", [])
    return data


def _save(campaign: str, data: dict) -> None:
    p = _epist_path(campaign)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _norm_id(name: str) -> str:
    if ":" in name:
        return name
    return f"npc:{name}"


def _find_fact(data: dict, fid: str) -> Optional[dict]:
    for f in data["facts"]:
        if f["id"] == fid:
            return f
    return None


# ── Knowledge states ────────────────────────────────────────────────────────

KNOWLEDGE_STATES = {
    "unknown":      "players have no idea this fact exists",
    "rumored":      "players heard a rumor but don't know if it's true",
    "suspected":    "players suspect something but lack proof",
    "known":        "players know the truth with certainty",
    "disproven":    "players believe something FALSE (the truth is different)",
}

BELIEF_ACCURACY = {
    "correct":      "NPC believes the truth",
    "mistaken":     "NPC believes something false",
    "suspects":     "NPC suspects but isn't sure",
    "ignorant":     "NPC has no knowledge of this fact",
    "knows_partial":"NPC knows part of the truth",
}


# ── Commands ────────────────────────────────────────────────────────────────

def cmd_fact_add(args) -> int:
    """Register a fact with its truth and surface appearance."""
    data = _load(args.campaign)
    try:
        fact = json.loads(args.json)
    except json.JSONDecodeError as e:
        print(f"error: invalid JSON: {e}", file=sys.stderr)
        return 1

    if "id" not in fact or "truth" not in fact:
        print("error: fact must have 'id' and 'truth'", file=sys.stderr)
        return 1

    fact.setdefault("appearance", fact["truth"])  # what seems to be true
    fact.setdefault("npc_beliefs", {})            # {npc_id: {belief, accuracy}}
    fact.setdefault("player_knowledge", "unknown")
    fact.setdefault("evidence_supporting", [])    # evidence for the truth
    fact.setdefault("evidence_contradicting", []) # evidence against the truth
    fact.setdefault("reveal_conditions", [])      # conditions that trigger discovery
    fact.setdefault("consequences_on_reveal", "") # what happens when players learn
    fact.setdefault("created_session", args.session or 0)
    fact.setdefault("revealed_session", None)

    existing_ids = {f["id"] for f in data["facts"]}
    if fact["id"] in existing_ids and not args.force:
        print(f"error: fact '{fact['id']}' already exists. Use --force.", file=sys.stderr)
        return 1

    if fact["id"] in existing_ids:
        data["facts"] = [f if f["id"] != fact["id"] else fact for f in data["facts"]]
    else:
        data["facts"].append(fact)

    _save(args.campaign, data)
    print(f"OK — fact '{fact['id']}' registered")
    print(f"  truth: {fact['truth'][:80]}")
    print(f"  appearance: {fact['appearance'][:80]}")
    print(f"  player knowledge: {fact['player_knowledge']}")
    return 0


def cmd_fact_show(args) -> int:
    """Show the full epistemic state of a fact."""
    data = _load(args.campaign)
    fact = _find_fact(data, args.id)
    if not fact:
        print(f"# fact '{args.id}' not found", file=sys.stderr)
        return 1

    print(f"# fact: {fact['id']}\n")

    print(f"  WORLD TRUTH (GM only):")
    print(f"    {fact.get('truth', '?')}")
    print()

    print(f"  SURFACE APPEARANCE (what observers perceive):")
    print(f"    {fact.get('appearance', '?')}")
    print()

    print(f"  PLAYER KNOWLEDGE: {fact.get('player_knowledge', 'unknown')}")
    print(f"    {KNOWLEDGE_STATES.get(fact.get('player_knowledge', 'unknown'), '?')}")
    if fact.get("revealed_session"):
        print(f"    revealed session: {fact['revealed_session']}")
    print()

    beliefs = fact.get("npc_beliefs", {})
    if beliefs:
        print(f"  NPC BELIEFS ({len(beliefs)} NPCs):")
        for npc_id, belief in beliefs.items():
            accuracy = belief.get("accuracy", "ignorant")
            npc_belief = belief.get("belief", "?")
            print(f"    {npc_id}: [{accuracy}] {npc_belief}")
    else:
        print(f"  NPC BELIEFS: (none registered)")
    print()

    if fact.get("evidence_supporting"):
        print(f"  EVIDENCE (supporting truth):")
        for e in fact["evidence_supporting"]:
            print(f"    + {e}")
    if fact.get("evidence_contradicting"):
        print(f"  EVIDENCE (contradicting truth):")
        for e in fact["evidence_contradicting"]:
            print(f"    - {e}")

    if fact.get("consequences_on_reveal"):
        print(f"\n  CONSEQUENCES ON REVEAL: {fact['consequences_on_reveal']}")
    return 0


def cmd_belief_set(args) -> int:
    """Set what an NPC believes about a fact."""
    data = _load(args.campaign)
    fact = _find_fact(data, args.id)
    if not fact:
        print(f"# fact '{args.id}' not found", file=sys.stderr)
        return 1

    npc_id = _norm_id(args.npc)
    accuracy = args.accuracy or "mistaken"

    if accuracy not in BELIEF_ACCURACY:
        print(f"error: invalid accuracy '{accuracy}'. Valid: {sorted(BELIEF_ACCURACY.keys())}",
              file=sys.stderr)
        return 1

    fact.setdefault("npc_beliefs", {})[npc_id] = {
        "belief": args.belief,
        "accuracy": accuracy,
        "set_session": args.session,
    }

    _save(args.campaign, data)
    print(f"OK — belief set for {npc_id} on {args.id}")
    print(f"  belief: {args.belief}")
    print(f"  accuracy: {accuracy} ({BELIEF_ACCURACY[accuracy]})")
    return 0


def cmd_belief_check(args) -> int:
    """Check what an NPC believes about a fact — used before narrating NPC actions."""
    data = _load(args.campaign)
    fact = _find_fact(data, args.id)
    if not fact:
        print(f"# fact '{args.id}' not found", file=sys.stderr)
        return 1

    npc_id = _norm_id(args.npc)
    belief = fact.get("npc_beliefs", {}).get(npc_id)

    if not belief:
        print(f"# {npc_id} has no registered belief about {args.id}")
        print(f"# assume: ignorant — NPC does not know this fact exists")
        print(f"# truth: {fact.get('truth', '?')}")
        return 0

    print(f"# belief check: {npc_id} → {args.id}")
    print(f"  believes: {belief.get('belief', '?')}")
    print(f"  accuracy: {belief.get('accuracy', 'ignorant')}")
    print(f"  truth:    {fact.get('truth', '?')}")
    print()

    # Guidance for the GM
    accuracy = belief.get("accuracy", "ignorant")
    if accuracy == "correct":
        print(f"  ✓ NPC knows the truth — can act on it")
    elif accuracy == "mistaken":
        print(f"  ⚠ NPC believes something FALSE — will act on wrong info")
        print(f"    narrate NPC acting on their belief, not the truth")
    elif accuracy == "suspects":
        print(f"  ⊙ NPC suspects but isn't sure — may investigate or test")
    elif accuracy == "ignorant":
        print(f"  · NPC doesn't know — cannot reference this fact in dialogue")
    return 0


def cmd_knowledge_set(args) -> int:
    """Set the players' knowledge state about a fact."""
    data = _load(args.campaign)
    fact = _find_fact(data, args.id)
    if not fact:
        print(f"# fact '{args.id}' not found", file=sys.stderr)
        return 1

    if args.state not in KNOWLEDGE_STATES:
        print(f"error: invalid state '{args.state}'. Valid: {sorted(KNOWLEDGE_STATES.keys())}",
              file=sys.stderr)
        return 1

    old = fact.get("player_knowledge", "unknown")
    fact["player_knowledge"] = args.state

    if args.state == "known" and not fact.get("revealed_session"):
        fact["revealed_session"] = args.session

    _save(args.campaign, data)
    print(f"OK — player knowledge updated: {args.id}")
    print(f"  {old} → {args.state}")
    print(f"  {KNOWLEDGE_STATES[args.state]}")
    return 0


def cmd_fact_reveal(args) -> int:
    """Players discover the truth — transitions knowledge to 'known'."""
    data = _load(args.campaign)
    fact = _find_fact(data, args.id)
    if not fact:
        print(f"# fact '{args.id}' not found", file=sys.stderr)
        return 1

    old = fact.get("player_knowledge", "unknown")
    fact["player_knowledge"] = "known"
    fact["revealed_session"] = args.session

    _save(args.campaign, data)
    print(f"OK — fact REVEALED to players (session {args.session})")
    print(f"  truth: {fact.get('truth', '?')}")
    print(f"  was: {old}")
    if fact.get("consequences_on_reveal"):
        print(f"  consequences: {fact['consequences_on_reveal']}")

    # Check for NPCs whose beliefs are now contradicted
    beliefs = fact.get("npc_beliefs", {})
    contradicted = [nid for nid, b in beliefs.items()
                    if b.get("accuracy") == "correct" and old != "known"]
    if contradicted:
        print(f"\n  NPCs who knew the truth (may need to react to player discovery):")
        for nid in contradicted:
            print(f"    - {nid}")
    return 0


def cmd_epistemic_gaps(args) -> int:
    """Find facts where NPC beliefs diverge from truth — secrets waiting to surface."""
    data = _load(args.campaign)
    gaps = []

    for fact in data["facts"]:
        truth = fact.get("truth", "")
        for npc_id, belief in fact.get("npc_beliefs", {}).items():
            accuracy = belief.get("accuracy", "ignorant")
            if accuracy in ("mistaken", "suspects"):
                gaps.append({
                    "fact_id": fact["id"],
                    "npc_id": npc_id,
                    "truth": truth[:60],
                    "belief": belief.get("belief", "?")[:60],
                    "accuracy": accuracy,
                    "player_knowledge": fact.get("player_knowledge", "unknown"),
                })

    if not gaps:
        print(f"# no epistemic gaps — all NPC beliefs match truth")
        return 0

    print(f"# {len(gaps)} epistemic gap(s) — NPCs with wrong or incomplete beliefs\n")
    for g in gaps:
        print(f"  {g['fact_id']} / {g['npc_id']}: [{g['accuracy']}]")
        print(f"    truth:  {g['truth']}")
        print(f"    belief: {g['belief']}")
        print(f"    players: {g['player_knowledge']}")
        print()
    return 0


def cmd_player_misconceptions(args) -> int:
    """Find facts where players believe something false (disproven state)."""
    data = _load(args.campaign)

    misconceptions = [f for f in data["facts"]
                      if f.get("player_knowledge") == "disproven"]
    rumors = [f for f in data["facts"]
              if f.get("player_knowledge") == "rumored"]

    if not misconceptions and not rumors:
        print(f"# no player misconceptions — players' beliefs align with available info")
        return 0

    if misconceptions:
        print(f"# {len(misconceptions)} player MISCONCEPTION(S) — players believe something false\n")
        for f in misconceptions:
            print(f"  {f['id']}:")
            print(f"    players believe: (false)")
            print(f"    truth: {f.get('truth', '?')[:80]}")
            print(f"    appearance: {f.get('appearance', '?')[:80]}")
            print()

    if rumors:
        print(f"# {len(rumors)} RUMOR(S) — players heard but haven't verified\n")
        for f in rumors:
            print(f"  {f['id']}: {f.get('truth', '?')[:60]}")
            print(f"    players are unsure if this is true")
    return 0


def cmd_who_knows(args) -> int:
    """Show who knows the truth about a fact — for planning revelations."""
    data = _load(args.campaign)
    fact = _find_fact(data, args.id)
    if not fact:
        print(f"# fact '{args.id}' not found", file=sys.stderr)
        return 1

    print(f"# who knows the truth: {args.id}")
    print(f"  truth: {fact.get('truth', '?')[:80]}\n")

    correct = []
    mistaken = []
    suspects = []
    ignorant = []

    for npc_id, belief in fact.get("npc_beliefs", {}).items():
        acc = belief.get("accuracy", "ignorant")
        if acc == "correct":
            correct.append(npc_id)
        elif acc == "mistaken":
            mistaken.append(npc_id)
        elif acc == "suspects":
            suspects.append(npc_id)
        else:
            ignorant.append(npc_id)

    print(f"  knows the truth ({len(correct)}):")
    for n in correct:
        print(f"    ✓ {n}")
    print(f"  suspects ({len(suspects)}):")
    for n in suspects:
        print(f"    ⊙ {n}")
    print(f"  mistaken ({len(mistaken)}):")
    for n in mistaken:
        print(f"    ✗ {n}")
    print(f"  ignorant ({len(ignorant)}):")
    for n in ignorant:
        print(f"    · {n}")

    print(f"\n  player knowledge: {fact.get('player_knowledge', 'unknown')}")

    # Suggest revelation paths
    if correct and fact.get("player_knowledge") != "known":
        print(f"\n  revelation paths:")
        print(f"    - players can learn from: {', '.join(correct)}")
        print(f"    - or discover evidence: {fact.get('evidence_supporting', [])[:2]}")
    return 0


# ── CLI ─────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--campaign", required=True, help="Campaign name")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("fact-add", help="Register a fact")
    s.add_argument("json", help="Fact JSON")
    s.add_argument("--session", type=int, default=0)
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_fact_add)

    s = sub.add_parser("fact-show", help="Show full epistemic state of a fact")
    s.add_argument("--id", required=True)
    s.set_defaults(func=cmd_fact_show)

    s = sub.add_parser("belief-set", help="Set what an NPC believes")
    s.add_argument("--id", required=True)
    s.add_argument("--npc", required=True)
    s.add_argument("--belief", required=True, help="What the NPC believes")
    s.add_argument("--accuracy", choices=list(BELIEF_ACCURACY.keys()),
                   help="How accurate the belief is")
    s.add_argument("--session", type=int, default=0)
    s.set_defaults(func=cmd_belief_set)

    s = sub.add_parser("belief-check", help="Check what an NPC believes (before narrating)")
    s.add_argument("--id", required=True)
    s.add_argument("--npc", required=True)
    s.set_defaults(func=cmd_belief_check)

    s = sub.add_parser("knowledge-set", help="Set player knowledge state")
    s.add_argument("--id", required=True)
    s.add_argument("--state", required=True, choices=list(KNOWLEDGE_STATES.keys()))
    s.add_argument("--session", type=int, default=0)
    s.set_defaults(func=cmd_knowledge_set)

    s = sub.add_parser("fact-reveal", help="Players discover the truth")
    s.add_argument("--id", required=True)
    s.add_argument("--session", type=int, default=0)
    s.set_defaults(func=cmd_fact_reveal)

    s = sub.add_parser("epistemic-gaps", help="Find NPCs with wrong/incomplete beliefs")
    s.set_defaults(func=cmd_epistemic_gaps)

    s = sub.add_parser("player-misconceptions", help="Find facts players believe falsely")
    s.set_defaults(func=cmd_player_misconceptions)

    s = sub.add_parser("who-knows", help="Show who knows the truth about a fact")
    s.add_argument("--id", required=True)
    s.set_defaults(func=cmd_who_knows)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
