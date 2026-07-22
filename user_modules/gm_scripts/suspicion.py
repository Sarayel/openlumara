#!/usr/bin/env python3
"""
suspicion.py — NPC suspicion graph for hierarchical GM tracking.

Tracks who suspects whom, and at what level. Suspicion is per-NPC,
per-target, and changes in response to revelations, observed actions,
and rumor spread.

The suspicion graph is what produces political stories automatically.
When a revelation happens, suspicion shifts. When NPCs talk to each
other, suspicion spreads. When the party does something conspicuous,
multiple NPCs' suspicion of the party rises in parallel.

Storage: <campaign-dir>/suspicion.json
  {
    "version": 1,
    "entries": [
      {
        "npc_id": "npc:velkyn",
        "name": "Velkyn",
        "suspects": {
          "npc:prince": 45,
          "npc:sheriff": 90,
          "pc:aldric": 20
        },
        "last_updated_session": 15
      }
    ],
    "revelation_effects": [
      {
        "intrigue_id": "i001",
        "clue": "the Pale Court knows the party's faces",
        "suspicion_changes": [
          {"npc_id": "npc:velkyn", "target": "pc:aldric", "delta": 30},
          {"npc_id": "npc:sheriff", "target": "pc:aldric", "delta": 15}
        ]
      }
    ]
  }

Suspicion is a 0-100 scale:
   0-20: no suspicion (trusts the target)
  21-40: mild suspicion (watchful)
  41-60: moderate suspicion (investigating)
  61-80: high suspicion (actively hostile)
  81-100: certain (will act on the suspicion)

LLM-agnostic. All queries and updates are deterministic Python.

Usage:
  python3 suspicion.py show --campaign <name> --npc velkyn
  python3 suspicion.py adjust --campaign <name> --npc velkyn --target prince --delta 15
  python3 suspicion.py check --campaign <name> --target aldric       # who suspects this target?
  python3 suspicion.py spread --campaign <name> --from velkyn --to sheriff --target prince --amount 20
  python3 suspicion.py trigger --campaign <name> --intrigue i001 --clue "..." --session 15
  python3 suspicion.py matrix --campaign <name>                      # full suspicion matrix
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from paths import find_campaign


# ── IO ──────────────────────────────────────────────────────────────────────

def _suspicion_path(campaign: str) -> Path:
    return find_campaign(campaign) / "suspicion.json"


def _load(campaign: str) -> dict:
    p = _suspicion_path(campaign)
    if not p.exists():
        return {"version": 1, "entries": [], "revelation_effects": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "entries": [], "revelation_effects": []}
    data.setdefault("version", 1)
    data.setdefault("entries", [])
    data.setdefault("revelation_effects", [])
    return data


def _save(campaign: str, data: dict) -> None:
    p = _suspicion_path(campaign)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ── Helpers ─────────────────────────────────────────────────────────────────

def _normalize_id(name: str) -> str:
    """Normalize a name to npc:/pc:/faction: prefix format."""
    if not name:
        return name
    if ":" in name:
        return name
    # Default to npc: prefix
    return f"npc:{name}"


def _find_entry(data: dict, npc_id: str) -> Optional[dict]:
    npc_id = _normalize_id(npc_id)
    for entry in data["entries"]:
        if entry["npc_id"] == npc_id:
            return entry
    return None


def _get_or_create_entry(data: dict, npc_id: str, name: str = "") -> dict:
    npc_id = _normalize_id(npc_id)
    entry = _find_entry(data, npc_id)
    if entry is None:
        entry = {
            "npc_id": npc_id,
            "name": name or npc_id.split(":", 1)[-1],
            "suspects": {},
            "last_updated_session": 0,
        }
        data["entries"].append(entry)
    return entry


def _suspicion_label(score: int) -> str:
    """Human-readable label for a suspicion score."""
    if score <= 20:
        return "trusting"
    elif score <= 40:
        return "watchful"
    elif score <= 60:
        return "investigating"
    elif score <= 80:
        return "hostile"
    else:
        return "certain"


def _clamp(score: int) -> int:
    return max(0, min(100, score))


# ── Commands ────────────────────────────────────────────────────────────────

def cmd_show(args) -> int:
    """Show who an NPC suspects and at what level."""
    data = _load(args.campaign)
    npc_id = _normalize_id(args.npc)
    entry = _find_entry(data, npc_id)
    if not entry:
        print(f"# no suspicion data for '{args.npc}'")
        print(f"# use: python3 suspicion.py adjust --campaign {args.campaign} "
              f"--npc {args.npc} --target <name> --delta <N>")
        return 1

    suspects = entry.get("suspects", {})
    if not suspects:
        print(f"# {entry.get('name', npc_id)} suspects no one")
        return 0

    print(f"# suspicion report for {entry.get('name', npc_id)}\n")
    # Sort by suspicion level descending
    sorted_suspects = sorted(suspects.items(), key=lambda x: x[1], reverse=True)
    for target_id, score in sorted_suspects:
        label = _suspicion_label(score)
        bar = "█" * (score // 5) + "░" * (20 - score // 5)
        print(f"  {target_id:<25} {score:>3} {bar} ({label})")
    return 0


def cmd_adjust(args) -> int:
    """Adjust an NPC's suspicion of a target by a delta."""
    data = _load(args.campaign)
    entry = _get_or_create_entry(data, args.npc, args.npc_name or "")
    target_id = _normalize_id(args.target)

    suspects = entry.setdefault("suspects", {})
    old_score = suspects.get(target_id, 0)
    new_score = _clamp(old_score + args.delta)
    suspects[target_id] = new_score
    entry["last_updated_session"] = args.session

    _save(args.campaign, data)
    print(f"OK — suspicion adjusted")
    print(f"  {entry['name']} → {target_id}: {old_score} → {new_score} ({args.delta:+d})")
    print(f"  label: {_suspicion_label(new_score)}")

    # Check for threshold crossings
    if old_score < 60 <= new_score:
        print(f"  ⚠ crossed into 'investigating' — {entry['name']} may act on this")
    elif old_score < 80 <= new_score:
        print(f"  ⚠ crossed into 'hostile' — {entry['name']} will act on this")
    return 0


def cmd_check(args) -> int:
    """Check who suspects a given target, and at what level."""
    data = _load(args.campaign)
    target_id = _normalize_id(args.target)
    # Also check for pc: and faction: variants
    target_variants = {target_id}
    if not args.target.startswith(("npc:", "pc:", "faction:")):
        target_variants.add(f"pc:{args.target}")
        target_variants.add(f"faction:{args.target}")
        target_variants.add(args.target)

    suspects = []
    for entry in data["entries"]:
        for t_id in target_variants:
            score = entry.get("suspects", {}).get(t_id, 0)
            if score > 0:
                suspects.append((entry.get("name", entry["npc_id"]), entry["npc_id"], score))
                break

    if not suspects:
        print(f"# no one suspects '{args.target}'")
        return 0

    suspects.sort(key=lambda x: x[2], reverse=True)
    print(f"# who suspects {target_id}\n")
    for name, npc_id, score in suspects:
        label = _suspicion_label(score)
        bar = "█" * (score // 5) + "░" * (20 - score // 5)
        print(f"  {name:<25} {score:>3} {bar} ({label})")
    return 0


def cmd_spread(args) -> int:
    """Spread suspicion from one NPC to another.

    When NPC A talks to NPC B, B's suspicion of a target moves toward A's
    level by the specified amount. This models gossip, briefings, and
    intelligence sharing.
    """
    data = _load(args.campaign)
    from_entry = _find_entry(data, args.from_npc)
    if not from_entry:
        print(f"# no suspicion data for source '{args.from_npc}'", file=sys.stderr)
        return 1

    target_id = _normalize_id(args.target)
    from_score = from_entry.get("suspects", {}).get(target_id, 0)
    if from_score == 0:
        print(f"# {from_entry.get('name', args.from_npc)} doesn't suspect {target_id} — nothing to spread",
              file=sys.stderr)
        return 1

    to_entry = _get_or_create_entry(data, args.to_npc, args.to_name or "")
    to_suspects = to_entry.setdefault("suspects", {})
    old_score = to_suspects.get(target_id, 0)

    # Spread: move toward from_score by args.amount (capped at from_score)
    if from_score > old_score:
        new_score = min(from_score, old_score + args.amount)
    else:
        new_score = max(from_score, old_score - args.amount)

    to_suspects[target_id] = new_score
    to_entry["last_updated_session"] = args.session

    _save(args.campaign, data)
    print(f"OK — suspicion spread")
    print(f"  source: {from_entry.get('name', args.from_npc)} ({from_score})")
    print(f"  target: {to_entry.get('name', args.to_npc)}")
    print(f"  suspect: {target_id}")
    print(f"  {old_score} → {new_score} (moved {new_score - old_score:+d} toward {from_score})")
    return 0


def cmd_trigger(args) -> int:
    """Apply suspicion changes tied to a revelation.

    When a clue is revealed in an intrigue, it can shift suspicion. This
    command looks up the pre-registered revelation_effect for the
    intrigue+clue and applies the suspicion deltas.
    """
    data = _load(args.campaign)

    # Find matching revelation effect
    effect = None
    for re in data.get("revelation_effects", []):
        if re.get("intrigue_id") == args.intrigue and args.clue in re.get("clue", ""):
            effect = re
            break

    if not effect:
        # No pre-registered effect — offer to create one
        print(f"# no revelation effect registered for intrigue {args.intrigue}, clue containing '{args.clue}'")
        print(f"# to register one, add to suspicion.json → revelation_effects:")
        print(f'''#   {{
#     "intrigue_id": "{args.intrigue}",
#     "clue": "{args.clue}",
#     "suspicion_changes": [
#       {{"npc_id": "npc:velkyn", "target": "pc:aldric", "delta": 30}}
#     ]
#   }}''')
        return 1

    changes = effect.get("suspicion_changes", [])
    if not changes:
        print(f"# revelation effect found but has no suspicion_changes")
        return 1

    print(f"# applying revelation effect for {args.intrigue} (session {args.session})")
    print(f"   clue: {args.clue}\n")

    for change in changes:
        npc_id = change.get("npc_id", "")
        target = change.get("target", "")
        delta = change.get("delta", 0)

        entry = _get_or_create_entry(data, npc_id)
        suspects = entry.setdefault("suspects", {})
        target_id = _normalize_id(target)
        old_score = suspects.get(target_id, 0)
        new_score = _clamp(old_score + delta)
        suspects[target_id] = new_score
        entry["last_updated_session"] = args.session

        print(f"  {entry['name']} → {target_id}: {old_score} → {new_score} ({delta:+d})")

    _save(args.campaign, data)
    print(f"\n# {len(changes)} suspicion change(s) applied")
    return 0


def cmd_matrix(args) -> int:
    """Show the full suspicion matrix — every NPC's suspicion of every target."""
    data = _load(args.campaign)
    if not data["entries"]:
        print(f"# no suspicion data for campaign '{args.campaign}'")
        return 0

    # Collect all unique targets
    all_targets = set()
    for entry in data["entries"]:
        all_targets.update(entry.get("suspects", {}).keys())
    all_targets = sorted(all_targets)

    if not all_targets:
        print(f"# no suspicion entries to display")
        return 0

    # Print matrix
    header = f"{'NPC':<25}" + "".join(f"{t[-12:]:>14}" for t in all_targets)
    print(f"# suspicion matrix\n")
    print(header)
    print("-" * len(header))

    for entry in sorted(data["entries"], key=lambda e: e.get("name", e["npc_id"])):
        name = entry.get("name", entry["npc_id"])
        row = f"{name:<25}"
        for target in all_targets:
            score = entry.get("suspects", {}).get(target, 0)
            if score > 0:
                row += f"{score:>14}"
            else:
                row += f"{'·':>14}"
        print(row)

    print(f"\n# {len(data['entries'])} NPC(s), {len(all_targets)} target(s)")
    return 0


def cmd_register_effect(args) -> int:
    """Register a revelation effect — what suspicion changes when a clue is revealed."""
    data = _load(args.campaign)

    # Parse the changes JSON
    try:
        changes = json.loads(args.changes)
    except json.JSONDecodeError as e:
        print(f"error: invalid JSON for changes: {e}", file=sys.stderr)
        return 1

    effect = {
        "intrigue_id": args.intrigue,
        "clue": args.clue,
        "suspicion_changes": changes,
    }

    # Check for existing effect with same intrigue+clue
    existing = None
    for i, re in enumerate(data.get("revelation_effects", [])):
        if re.get("intrigue_id") == args.intrigue and re.get("clue") == args.clue:
            existing = i
            break

    if existing is not None:
        if args.force:
            data["revelation_effects"][existing] = effect
            print(f"OK — replaced existing revelation effect for {args.intrigue}")
        else:
            print(f"# revelation effect already exists for {args.intrigue} / '{args.clue}'")
            print(f"# use --force to replace")
            return 1
    else:
        data.setdefault("revelation_effects", []).append(effect)
        print(f"OK — registered revelation effect for {args.intrigue}")

    _save(args.campaign, data)
    print(f"   clue: {args.clue}")
    print(f"   changes: {len(changes)}")
    for c in changes:
        print(f"     - {c.get('npc_id', '?')} → {c.get('target', '?')}: {c.get('delta', 0):+d}")
    return 0


# ── CLI ─────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--campaign", required=True, help="Campaign name")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("show", help="Show who an NPC suspects")
    s.add_argument("--npc", required=True, help="NPC name or ID")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("adjust", help="Adjust an NPC's suspicion of a target")
    s.add_argument("--npc", required=True)
    s.add_argument("--target", required=True, help="Who they suspect")
    s.add_argument("--delta", type=int, required=True, help="Change in suspicion (-100 to +100)")
    s.add_argument("--npc-name", help="Display name for the NPC (if new)")
    s.add_argument("--session", type=int, default=0)
    s.set_defaults(func=cmd_adjust)

    s = sub.add_parser("check", help="Check who suspects a target")
    s.add_argument("--target", required=True)
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("spread", help="Spread suspicion from one NPC to another")
    s.add_argument("--from-npc", required=True, dest="from_npc")
    s.add_argument("--to-npc", required=True, dest="to_npc")
    s.add_argument("--target", required=True, help="The suspected target")
    s.add_argument("--amount", type=int, default=10, help="How much to move toward source level")
    s.add_argument("--to-name", help="Display name for the target NPC (if new)")
    s.add_argument("--session", type=int, default=0)
    s.set_defaults(func=cmd_spread)

    s = sub.add_parser("trigger", help="Apply suspicion changes from a revelation")
    s.add_argument("--intrigue", required=True, help="Intrigue ID")
    s.add_argument("--clue", required=True, help="Clue text (or substring)")
    s.add_argument("--session", type=int, default=0)
    s.set_defaults(func=cmd_trigger)

    s = sub.add_parser("register-effect", help="Register a revelation effect")
    s.add_argument("--intrigue", required=True)
    s.add_argument("--clue", required=True)
    s.add_argument("changes", help="JSON array of suspicion changes")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_register_effect)

    s = sub.add_parser("matrix", help="Show full suspicion matrix")
    s.set_defaults(func=cmd_matrix)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
