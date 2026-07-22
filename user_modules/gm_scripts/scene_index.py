#!/usr/bin/env python3
"""
scene_index.py — outcome partitioner for hierarchical GM tracking.

Write-time indexing of scene outcomes, keyed for future retrieval by
scene_loader.py and plans.py. Each resolved scene gets a JSON entry with:

  - deltas: structured state changes (NPC disposition, faction stance,
    thread status, item holders)
  - revealed: facts that are now canon and can be referenced in future scenes
  - foreshadowed: open threads the GM planted but hasn't developed —
    promotable to full child intrigues via `plans.py promote-foreshadow`
  - retrieval_keys: the magic field — future scenes query by key in O(1)
  - log_anchor: deep-read path back to session-log.md if atmosphere is needed

Storage: <campaign-dir>/scene_index.json

LLM-agnostic. All queries are deterministic Python. The GM (with model
assistance) drafts entries at /gm save; this script validates and stores them.

Usage:
  python3 scene_index.py add --campaign <name> '<json>'
  python3 scene_index.py query --campaign <name> --keys velkyn,ledger
  python3 scene_index.py query --campaign <name> --npc velkyn --since 10
  python3 scene_index.py latest --campaign <name> --npc velkyn
  python3 scene_index.py unrevealed --campaign <name>
  python3 scene_index.py foreshadowed --campaign <name>
  python3 scene_index.py show --campaign <name> --id s014
  python3 scene_index.py list --campaign <name> [--session N]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Optional

from paths import find_campaign


# ── IO ──────────────────────────────────────────────────────────────────────

def _index_path(campaign: str) -> Path:
    return find_campaign(campaign) / "scene_index.json"


def _load(campaign: str) -> dict:
    p = _index_path(campaign)
    if not p.exists():
        return {"version": 1, "scenes": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "scenes": []}
    data.setdefault("version", 1)
    data.setdefault("scenes", [])
    return data


def _save(campaign: str, data: dict) -> None:
    p = _index_path(campaign)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ── Schema validation ───────────────────────────────────────────────────────

REQUIRED_FIELDS = {"id", "session", "location", "outcome", "outcome_summary"}
OPTIONAL_FIELDS = {
    "date", "participants", "stake", "resolution", "deltas",
    "revealed", "foreshadowed", "retrieval_keys", "log_anchor",
    "graph_edges_added", "graph_edges_closed",
}


def _validate_scene(scene: dict) -> list[str]:
    """Return list of validation errors (empty = valid)."""
    errors = []
    for field in REQUIRED_FIELDS:
        if field not in scene:
            errors.append(f"missing required field: {field}")
    if not errors:
        if not isinstance(scene["id"], str) or not scene["id"]:
            errors.append("id must be a non-empty string")
        if not isinstance(scene["session"], int) or scene["session"] < 1:
            errors.append("session must be a positive integer")
        if not isinstance(scene["outcome"], str) or not scene["outcome"]:
            errors.append("outcome must be a non-empty string")
    # Check for unknown fields (warn, don't error — forward-compat)
    unknown = set(scene.keys()) - REQUIRED_FIELDS - OPTIONAL_FIELDS
    if unknown:
        errors.append(f"warning: unknown fields (kept but not validated): {sorted(unknown)}")
    return errors


# ── Commands ────────────────────────────────────────────────────────────────

def cmd_add(args) -> int:
    """Add a scene entry to the index."""
    try:
        scene = json.loads(args.json)
    except json.JSONDecodeError as e:
        print(f"error: invalid JSON: {e}", file=sys.stderr)
        return 1

    errors = _validate_scene(scene)
    hard_errors = [e for e in errors if not e.startswith("warning")]
    if hard_errors:
        print("validation errors:", file=sys.stderr)
        for e in hard_errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    for w in errors:
        if w.startswith("warning"):
            print(w, file=sys.stderr)

    # Auto-populate retrieval_keys if not provided
    if "retrieval_keys" not in scene:
        scene["retrieval_keys"] = _auto_keys(scene)

    # Auto-populate date if not provided
    if "date" not in scene:
        scene["date"] = date.today().isoformat()

    # Auto-populate empty optionals
    for field in ("revealed", "foreshadowed", "deltas"):
        scene.setdefault(field, [] if field != "deltas" else {})

    data = _load(args.campaign)

    # Check for duplicate ID
    existing_ids = {s["id"] for s in data["scenes"]}
    if scene["id"] in existing_ids and not args.force:
        print(f"error: scene id '{scene['id']}' already exists. "
              f"Use --force to overwrite.", file=sys.stderr)
        return 1

    # Replace if --force, else append
    if scene["id"] in existing_ids:
        data["scenes"] = [s if s["id"] != scene["id"] else scene for s in data["scenes"]]
    else:
        data["scenes"].append(scene)

    _save(args.campaign, data)

    # ── Entity state side-effect ──
    # When a scene is indexed with NPC deltas (disposition/trust changes),
    # apply those deltas to entity_state.json as the canonical NPC state.
    _apply_deltas_to_entity_state(args.campaign, scene)

    print(f"OK — scene '{scene['id']}' indexed "
          f"({len(scene.get('retrieval_keys', []))} retrieval keys, "
          f"{len(scene.get('revealed', []))} revealed, "
          f"{len(scene.get('foreshadowed', []))} foreshadowed)")
    return 0


def _apply_deltas_to_entity_state(campaign: str, scene: dict) -> None:
    """Apply NPC deltas from a scene_index entry to entity_state.json.

    This is the write-side of the entity state consolidation: when a scene
    records that an NPC's disposition changed, that change is applied to
    the canonical entity_state.json as a side effect.
    """
    deltas = scene.get("deltas", {})
    if not deltas:
        return

    es_path = find_campaign(campaign) / "entity_state.json"
    if not es_path.exists():
        es_data = {"version": 1, "entities": {}}
    else:
        try:
            es_data = json.loads(es_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            es_data = {"version": 1, "entities": {}}
    es_data.setdefault("version", 1)
    es_data.setdefault("entities", {})

    changed = False
    session = scene.get("session", 0)

    for key, delta in deltas.items():
        if not key.startswith("npc:"):
            continue
        npc_id = key
        entity = es_data["entities"].get(npc_id)
        if entity is None:
            entity = {
                "name": npc_id.split(":")[-1],
                "relationship_to_party": "neutral",
                "trust": 0,
                "favors_owed": [], "favors_owed_to": [],
                "known_secrets": [], "known_facts": [],
                "last_interaction": None, "last_interaction_session": 0,
                "disposition_history": [],
            }
            es_data["entities"][npc_id] = entity

        # Apply disposition change
        if "disposition" in delta:
            old = entity.get("relationship_to_party", "neutral")
            new = delta["disposition"]
            entity["relationship_to_party"] = new
            entity.setdefault("disposition_history", []).append({
                "field": "relationship_to_party",
                "old": old, "new": new,
                "session": session,
                "source": f"scene_index:{scene.get('id', '?')}",
            })
            changed = True

        # Apply trust change (delta value, not absolute)
        if "trust" in delta:
            old = entity.get("trust", 0)
            entity["trust"] = old + delta["trust"]
            entity.setdefault("disposition_history", []).append({
                "field": "trust",
                "old": old, "new": entity["trust"],
                "session": session,
                "source": f"scene_index:{scene.get('id', '?')}",
            })
            changed = True

        # Update last interaction
        entity["last_interaction"] = scene.get("id", "")
        entity["last_interaction_session"] = session
        changed = True

    if changed:
        es_path.parent.mkdir(parents=True, exist_ok=True)
        es_path.write_text(json.dumps(es_data, indent=2, ensure_ascii=False), encoding="utf-8")


def _auto_keys(scene: dict) -> list[str]:
    """Auto-generate retrieval keys from scene fields."""
    keys = set()
    keys.add(scene["location"])
    keys.add(f"session:{scene['session']}")
    for p in scene.get("participants", []):
        # Extract name from "pc:aldric" or "npc:velkyn" format
        if ":" in p:
            keys.add(p.split(":", 1)[1])
        else:
            keys.add(p)
    # Add outcome as a key
    keys.add(scene["outcome"])
    return sorted(k for k in keys if k)


def cmd_query(args) -> int:
    """Query scenes by retrieval keys, NPC, or session range."""
    data = _load(args.campaign)
    if not data["scenes"]:
        print(f"# scene_index not initialized for campaign '{args.campaign}'")
        return 0

    results = []

    for scene in data["scenes"]:
        # Session range filter
        if args.since and scene.get("session", 0) < args.since:
            continue
        if args.before and scene.get("session", 0) >= args.before:
            continue

        # Key filter (AND within keys, OR across scenes)
        if args.keys:
            scene_keys = set(scene.get("retrieval_keys", []))
            query_keys = set(k.strip() for k in args.keys.split(",") if k.strip())
            if not query_keys & scene_keys:
                continue

        # NPC filter
        if args.npc:
            participants = scene.get("participants", [])
            npc_match = f"npc:{args.npc}" in participants or args.npc in participants
            if not npc_match:
                # Also check deltas
                deltas = scene.get("deltas", {})
                if f"npc:{args.npc}" not in deltas:
                    continue

        # Thread filter
        if args.thread:
            deltas = scene.get("deltas", {})
            if f"thread:{args.thread}" not in deltas:
                continue

        results.append(scene)

    # Sort by session descending (most recent first)
    results.sort(key=lambda s: s.get("session", 0), reverse=True)

    if args.limit:
        results = results[:args.limit]

    if not results:
        print("# no matching scenes found")
        return 0

    print(f"# {len(results)} scene(s) matching query\n")
    for scene in results:
        _print_scene_summary(scene)
        if args.full:
            _print_scene_detail(scene)
        print()

    return 0


def cmd_latest(args) -> int:
    """Show the latest scene involving a given NPC or key."""
    data = _load(args.campaign)
    if not data["scenes"]:
        print(f"# scene_index not initialized for campaign '{args.campaign}'")
        return 0

    target = args.npc or args.key
    if not target:
        print("error: --npc or --key required", file=sys.stderr)
        return 1

    # Reuse query logic
    results = []
    for scene in data["scenes"]:
        scene_keys = set(scene.get("retrieval_keys", []))
        participants = scene.get("participants", [])
        if target in scene_keys or f"npc:{target}" in participants or target in participants:
            results.append(scene)

    if not results:
        print(f"# no scenes found involving '{target}'")
        return 0

    results.sort(key=lambda s: s.get("session", 0), reverse=True)
    latest = results[0]
    print(f"# latest scene involving '{target}'\n")
    _print_scene_summary(latest)
    if args.full:
        _print_scene_detail(latest)
    return 0


def cmd_unrevealed(args) -> int:
    """List all unrevealed clues across all scenes (for GM planning)."""
    data = _load(args.campaign)
    if not data["scenes"]:
        print(f"# scene_index not initialized for campaign '{args.campaign}'")
        return 0

    print("# unrevealed clues (foreshadowed but not yet developed)\n")
    count = 0
    for scene in sorted(data["scenes"], key=lambda s: s.get("session", 0)):
        for clue in scene.get("foreshadowed", []):
            print(f"  [s{scene['session']:02d}] {clue}")
            count += 1
    print(f"\n# {count} foreshadowed element(s)")
    return 0


def cmd_foreshadowed(args) -> int:
    """Alias for unrevealed — list foreshadowed elements."""
    return cmd_unrevealed(args)


def cmd_show(args) -> int:
    """Show full detail for a specific scene by ID."""
    data = _load(args.campaign)
    for scene in data["scenes"]:
        if scene["id"] == args.id:
            _print_scene_detail(scene)
            return 0
    print(f"# scene '{args.id}' not found", file=sys.stderr)
    return 1


def cmd_list(args) -> int:
    """List all scenes, optionally filtered by session."""
    data = _load(args.campaign)
    if not data["scenes"]:
        print(f"# scene_index not initialized for campaign '{args.campaign}'")
        return 0

    scenes = data["scenes"]
    if args.session:
        scenes = [s for s in scenes if s.get("session") == args.session]

    scenes.sort(key=lambda s: s.get("session", 0))

    print(f"# {len(scenes)} scene(s) indexed\n")
    print(f"{'ID':<8} {'Sess':>4} {'Location':<25} {'Outcome':<15} Summary")
    print("-" * 90)
    for s in scenes:
        loc = s.get("location", "?")[:25]
        outcome = s.get("outcome", "?")[:15]
        summary = s.get("outcome_summary", "")[:40]
        print(f"{s['id']:<8} {s.get('session', 0):>4} {loc:<25} {outcome:<15} {summary}")
    return 0


# ── Printing helpers ────────────────────────────────────────────────────────

def _print_scene_summary(scene: dict) -> None:
    print(f"## {scene['id']} (session {scene.get('session', '?')}) — "
          f"{scene.get('location', '?')}")
    print(f"**Outcome:** {scene['outcome']} — {scene.get('outcome_summary', '')}")
    if scene.get("stake"):
        print(f"**Stake:** {scene['stake']}")
    if scene.get("participants"):
        print(f"**Present:** {', '.join(scene['participants'])}")
    if scene.get("retrieval_keys"):
        print(f"**Keys:** {', '.join(scene['retrieval_keys'])}")


def _print_scene_detail(scene: dict) -> None:
    print(f"\n--- Full Entry ---")
    print(json.dumps(scene, indent=2, ensure_ascii=False))


# ── CLI ─────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--campaign", required=True, help="Campaign name")
    sub = p.add_subparsers(dest="cmd", required=True)

    s_add = sub.add_parser("add", help="Add a scene entry")
    s_add.add_argument("json", help="Scene entry as JSON string")
    s_add.add_argument("--force", action="store_true", help="Overwrite if ID exists")
    s_add.set_defaults(func=cmd_add)

    s_query = sub.add_parser("query", help="Query scenes by keys/NPC/session")
    s_query.add_argument("--keys", help="Comma-separated retrieval keys (OR match)")
    s_query.add_argument("--npc", help="Filter by NPC name")
    s_query.add_argument("--thread", help="Filter by thread name")
    s_query.add_argument("--since", type=int, help="From session N onward")
    s_query.add_argument("--before", type=int, help="Before session N")
    s_query.add_argument("--limit", type=int, default=10, help="Max results (default 10)")
    s_query.add_argument("--full", action="store_true", help="Show full JSON detail")
    s_query.set_defaults(func=cmd_query)

    s_latest = sub.add_parser("latest", help="Latest scene for an NPC or key")
    s_latest.add_argument("--npc", help="NPC name")
    s_latest.add_argument("--key", help="Retrieval key")
    s_latest.add_argument("--full", action="store_true")
    s_latest.set_defaults(func=cmd_latest)

    s_unrev = sub.add_parser("unrevealed", help="List foreshadowed elements")
    s_unrev.set_defaults(func=cmd_unrevealed)

    s_foreshadow = sub.add_parser("foreshadowed", help="Alias for unrevealed")
    s_foreshadow.set_defaults(func=cmd_foreshadowed)

    s_show = sub.add_parser("show", help="Show full detail for a scene ID")
    s_show.add_argument("id", help="Scene ID (e.g. s014)")
    s_show.set_defaults(func=cmd_show)

    s_list = sub.add_parser("list", help="List all scenes")
    s_list.add_argument("--session", type=int, help="Filter by session number")
    s_list.set_defaults(func=cmd_list)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
