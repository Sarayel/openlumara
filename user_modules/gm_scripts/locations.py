#!/usr/bin/env python3
"""
locations.py — location atmosphere registry with visit tracking.

Each location stores sensory details (smell, sound, texture, light,
weight/temperature) that the Scene Card surfaces to the LLM renderer.
Visit state is tracked so the GM knows whether to give a full first-time
description or a brief return recap.

Three visit states:
  first_visit   — full sensory description, all details
  returning     — brief recap ("you've been here before") + what's changed
  unchanged     — minimal ("the same as you left it")

If a location has changed since last visit (fire damage, rearranged
furniture, new occupant), the change is tracked explicitly so the GM
can narrate the difference.

Storage: <campaign-dir>/locations.json

Usage:
  python3 locations.py add --campaign <name> '<json>'
  python3 locations.py show --campaign <name> --id elysium
  python3 locations.py visit --campaign <name> --id elysium --session 15
  python3 locations.py set-changed --campaign <name> --id elysium --change "scorch marks on the east wall"
  python3 locations.py atmosphere --campaign <name> --id elysium
  python3 locations.py list --campaign <name>
  python3 locations.py reset-visits --campaign <name>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from paths import find_campaign


# ── IO ──────────────────────────────────────────────────────────────────────

def _loc_path(campaign: str) -> Path:
    return find_campaign(campaign) / "locations.json"


def _load(campaign: str) -> dict:
    p = _loc_path(campaign)
    if not p.exists():
        return {"version": 1, "locations": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "locations": []}
    data.setdefault("version", 1)
    data.setdefault("locations", [])
    return data


def _save(campaign: str, data: dict) -> None:
    p = _loc_path(campaign)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _find(data: dict, loc_id: str) -> Optional[dict]:
    for loc in data["locations"]:
        if loc["id"] == loc_id:
            return loc
    return None


# ── Commands ────────────────────────────────────────────────────────────────

def cmd_add(args) -> int:
    """Register a location with sensory atmosphere."""
    data = _load(args.campaign)
    try:
        loc = json.loads(args.json)
    except json.JSONDecodeError as e:
        print(f"error: invalid JSON: {e}", file=sys.stderr)
        return 1

    if "id" not in loc or "name" not in loc:
        print("error: location must have 'id' and 'name'", file=sys.stderr)
        return 1

    # Sensory details — the core of atmosphere
    loc.setdefault("sensory", {})
    sensory = loc["sensory"]
    sensory.setdefault("smell", "")        # "old paper, beeswax, copper"
    sensory.setdefault("sound", "")        # "creaking floorboards, distant chanting"
    sensory.setdefault("texture", "")      # "rough stone, velvet curtains, cold iron"
    sensory.setdefault("light", "")        # "candlelight flickering, harsh fluorescent, dim amber"
    sensory.setdefault("temperature", "")  # "stuffy and warm, cold draft, damp chill"
    sensory.setdefault("weight", "")       # "oppressive silence, bustling energy, hollow emptiness"

    # Visual cues — trackable details the GM can reference
    loc.setdefault("visual_cues", [])
    # Each: {"cue": "the stained glass window", "detail": "depicts a scene of judgment", "first_seen_session": N}

    # Visit tracking
    loc.setdefault("visits", [])
    # Each: {"session": N, "changes": ["scorch marks on wall", "furniture rearranged"]}

    loc.setdefault("description", "")
    loc.setdefault("type", "interior")  # interior / exterior / supernatural / transit
    loc.setdefault("mood", "")          # "oppressive", "sanctuary", "tense", "decaying"
    loc.setdefault("default_present", [])  # NPCs typically found here

    existing_ids = {l["id"] for l in data["locations"]}
    if loc["id"] in existing_ids and not args.force:
        print(f"error: location '{loc['id']}' already exists. Use --force.", file=sys.stderr)
        return 1

    if loc["id"] in existing_ids:
        data["locations"] = [l if l["id"] != loc["id"] else loc for l in data["locations"]]
    else:
        data["locations"].append(loc)

    _save(args.campaign, data)
    print(f"OK — location '{loc['id']}' registered")
    print(f"  name: {loc['name']}")
    print(f"  mood: {loc.get('mood', '?')}")
    filled = sum(1 for v in sensory.values() if v)
    print(f"  sensory details: {filled}/6 filled")
    return 0


def cmd_show(args) -> int:
    """Show full location detail including visit history."""
    data = _load(args.campaign)
    loc = _find(data, args.id)
    if not loc:
        print(f"# location '{args.id}' not found", file=sys.stderr)
        return 1

    print(f"# location: {loc.get('name', loc['id'])}\n")
    print(f"  type: {loc.get('type', '?')}  mood: {loc.get('mood', '?')}")

    sensory = loc.get("sensory", {})
    if sensory:
        print(f"\n  sensory atmosphere:")
        for sense, detail in sensory.items():
            if detail:
                print(f"    {sense}: {detail}")

    if loc.get("visual_cues"):
        print(f"\n  visual cues ({len(loc['visual_cues'])}):")
        for cue in loc["visual_cues"]:
            print(f"    • {cue.get('cue', '?')}: {cue.get('detail', '?')}")

    visits = loc.get("visits", [])
    if visits:
        print(f"\n  visit history ({len(visits)} visits):")
        for v in visits:
            changes = v.get("changes", [])
            if changes:
                print(f"    session {v['session']}: changes — {', '.join(changes)}")
            else:
                print(f"    session {v['session']}: no changes")
    else:
        print(f"\n  visit history: (not yet visited)")

    if loc.get("default_present"):
        print(f"\n  typically present: {', '.join(loc['default_present'])}")
    return 0


def cmd_visit(args) -> int:
    """Mark a location as visited this session. Returns atmosphere guidance
    based on visit state (first visit / returning / unchanged).
    """
    data = _load(args.campaign)
    loc = _find(data, args.id)
    if not loc:
        print(f"# location '{args.id}' not found", file=sys.stderr)
        return 1

    visits = loc.setdefault("visits", [])
    visit_count = len(visits)

    if visit_count == 0:
        # First visit — full atmosphere dump
        visit_entry = {"session": args.session, "changes": []}
        visits.append(visit_entry)
        _save(args.campaign, data)

        print(f"# FIRST VISIT: {loc.get('name', args.id)}\n")
        print(f"## Full atmosphere (use all sensory details):")
        sensory = loc.get("sensory", {})
        for sense, detail in sensory.items():
            if detail:
                print(f"  {sense}: {detail}")
        if loc.get("visual_cues"):
            print(f"\n## Visual cues to establish:")
            for cue in loc["visual_cues"]:
                print(f"  • {cue.get('cue', '?')}: {cue.get('detail', '?')}")
        print(f"\n## GM guidance:")
        print(f"  This is the party's first time here. Paint the full picture —")
        print(f"  every sensory detail, every visual cue. Establish the mood:")
        print(f"  {loc.get('mood', '(unspecified)')}.")
    else:
        # Returning visit — check for changes
        last_visit = visits[-1]
        pending_changes = loc.get("_pending_changes", [])

        if pending_changes:
            # Location has changed since last visit
            visit_entry = {"session": args.session, "changes": pending_changes.copy()}
            visits.append(visit_entry)
            loc["_pending_changes"] = []
            _save(args.campaign, data)

            print(f"# RETURNING — CHANGED: {loc.get('name', args.id)}\n")
            print(f"## What's different since last visit (session {last_visit['session']}):")
            for change in pending_changes:
                print(f"  • {change}")
            print(f"\n## Recap (brief — they've been here before):")
            sensory = loc.get("sensory", {})
            # Only show 2-3 key sensory details for return visits
            key_senses = ["smell", "sound", "light"]
            for sense in key_senses:
                if sensory.get(sense):
                    print(f"  {sense}: {sensory[sense]}")
            print(f"\n## GM guidance:")
            print(f"  They've been here before. Don't re-describe everything —")
            print(f"  focus on what's CHANGED. The familiarity should contrast")
            print(f"  with the changes to create unease or curiosity.")
        else:
            # Unchanged return
            visits.append({"session": args.session, "changes": []})
            _save(args.campaign, data)

            print(f"# RETURNING — UNCHANGED: {loc.get('name', args.id)}\n")
            print(f"## GM guidance:")
            print(f"  They've been here before and nothing has changed.")
            print(f"  Minimal description — one sensory detail at most.")
            print(f"  Reference familiarity: 'the same smell of old paper'")
            print(f"  or 'the candles in their usual places.'")
            sensory = loc.get("sensory", {})
            if sensory.get("smell"):
                print(f"  anchor detail: {sensory['smell']}")
    return 0


def cmd_set_changed(args) -> int:
    """Mark that a location has changed since last visit. The change will
    be surfaced when the party next visits.
    """
    data = _load(args.campaign)
    loc = _find(data, args.id)
    if not loc:
        print(f"# location '{args.id}' not found", file=sys.stderr)
        return 1

    loc.setdefault("_pending_changes", []).append(args.change)
    _save(args.campaign, data)

    print(f"OK — change recorded for {args.id}")
    print(f"  change: {args.change}")
    print(f"  will surface on next visit")
    return 0


def cmd_atmosphere(args) -> int:
    """Show just the atmosphere block for a location — for Scene Card inclusion."""
    data = _load(args.campaign)
    loc = _find(data, args.id)
    if not loc:
        print(f"# location '{args.id}' not found", file=sys.stderr)
        return 1

    sensory = loc.get("sensory", {})
    visits = loc.get("visits", [])
    visit_state = "first_visit" if not visits else "returning"

    print(f"**{loc.get('name', args.id)}** ({visit_state})")
    if loc.get("mood"):
        print(f"mood: {loc['mood']}")

    details = []
    for sense in ("smell", "sound", "texture", "light", "temperature", "weight"):
        val = sensory.get(sense, "")
        if val:
            details.append(f"{sense}: {val}")

    if visit_state == "first_visit":
        for d in details:
            print(f"  {d}")
    else:
        # Return visit — only 2 key details
        for d in details[:2]:
            print(f"  {d}")
        pending = loc.get("_pending_changes", [])
        if pending:
            print(f"  CHANGED: {', '.join(pending)}")

    if loc.get("visual_cues") and visit_state == "first_visit":
        for cue in loc["visual_cues"][:3]:
            print(f"  • {cue.get('cue', '')}: {cue.get('detail', '')}")
    return 0


def cmd_list(args) -> int:
    """List all registered locations."""
    data = _load(args.campaign)
    if not data["locations"]:
        print(f"# no locations registered")
        return 0

    print(f"# {len(data['locations'])} location(s)\n")
    print(f"{'ID':<15} {'Name':<25} {'Visits':>6} {'Mood':<15} {'Sensory':>7}")
    print("-" * 70)
    for loc in data["locations"]:
        visits = len(loc.get("visits", []))
        mood = loc.get("mood", "?")[:15]
        sensory = loc.get("sensory", {})
        filled = sum(1 for v in sensory.values() if v)
        print(f"{loc['id']:<15} {loc.get('name', '?'):<25} {visits:>6} {mood:<15} {filled}/6")
    return 0


def cmd_reset_visits(args) -> int:
    """Reset all visit history (useful for testing or campaign restart)."""
    data = _load(args.campaign)
    for loc in data["locations"]:
        loc["visits"] = []
        loc.pop("_pending_changes", None)
    _save(args.campaign, data)
    print(f"OK — visit history reset for {len(data['locations'])} location(s)")
    return 0


# ── CLI ─────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--campaign", required=True, help="Campaign name")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("add", help="Register a location")
    s.add_argument("json", help="Location JSON")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("show", help="Show full location detail")
    s.add_argument("--id", required=True)
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("visit", help="Mark as visited — returns atmosphere guidance")
    s.add_argument("--id", required=True)
    s.add_argument("--session", type=int, default=0)
    s.set_defaults(func=cmd_visit)

    s = sub.add_parser("set-changed", help="Record a change for next visit")
    s.add_argument("--id", required=True)
    s.add_argument("--change", required=True, help="Description of what changed")
    s.set_defaults(func=cmd_set_changed)

    s = sub.add_parser("atmosphere", help="Show atmosphere block for Scene Card")
    s.add_argument("--id", required=True)
    s.set_defaults(func=cmd_atmosphere)

    s = sub.add_parser("list", help="List all locations")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("reset-visits", help="Reset all visit history")
    s.set_defaults(func=cmd_reset_visits)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
