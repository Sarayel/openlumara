#!/usr/bin/env python3
"""
npc_drives.py — NPC utility AI: state-driven goals that mutate the world.

The closed-loop simulation engine. NPCs don't just have plans (which track
steps) — they have DRIVES: goals with trigger conditions tied to game state.
When the players stall, NPC drives fire, mutate the state, and force the
Director's hand.

The loop:
  1. Players do nothing → state is static
  2. At /gm load or /gm end, drives are checked against current state
  3. Vantree's trigger fires: "players dawdling, mystery unsolved"
  4. State mutates: violence +30, hope -20, a world event is injected
  5. Director reads the mutated state → picks a crisis beat
  6. LLM renders Vantree's coup → players must react

This is what makes the world move without the players. NPCs are the engine
of entropy. If you sit on your hands, the NPCs will trigger their goals,
mutate the state, and force the Director's hand.

Storage: <campaign-dir>/npc_drives.json

Usage:
  python3 npc_drives.py add --campaign <name> '<json>'
  python3 npc_drives.py list --campaign <name> [--npc velkyn]
  python3 npc_drives.py check --campaign <name> --session 15
  python3 npc_drives.py show --campaign <name> --id d001
  python3 npc_drives.py fire --campaign <name> --id d001 --session 15
  python3 npc_drives.py reset-cooldown --campaign <name> --id d001
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from paths import find_campaign


# ── IO ──────────────────────────────────────────────────────────────────────

def _drives_path(campaign: str) -> Path:
    return find_campaign(campaign) / "npc_drives.json"


def _load(campaign: str) -> dict:
    p = _drives_path(campaign)
    if not p.exists():
        return {"version": 1, "drives": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "drives": []}
    data.setdefault("version", 1)
    data.setdefault("drives", [])
    return data


def _save(campaign: str, data: dict) -> None:
    p = _drives_path(campaign)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _find(data: dict, drive_id: str) -> Optional[dict]:
    for d in data["drives"]:
        if d["id"] == drive_id:
            return d
    return None


def _norm_id(name: str) -> str:
    if ":" in name:
        return name
    return f"npc:{name}"


# ── State readers (for trigger evaluation) ──────────────────────────────────

def _load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _read_pressure(campaign: str) -> dict:
    p = find_campaign(campaign) / "pressure.json"
    return _load_json(p, {"axes": {}}).get("axes", {})


def _read_economy(campaign: str) -> dict:
    p = find_campaign(campaign) / "economy.json"
    return _load_json(p, {"resources": {}}).get("resources", {})


def _read_campaign_state(campaign: str) -> dict:
    p = find_campaign(campaign) / "campaign_state.json"
    return _load_json(p, {"phase": "stability", "round": 0})


def _read_intrigues(campaign: str) -> list:
    p = find_campaign(campaign) / "intrigues.json"
    return _load_json(p, {"intrigues": []}).get("intrigues", [])


# ── Trigger condition evaluation ────────────────────────────────────────────

def _eval_condition(condition: str, context: dict) -> bool:
    """Evaluate a trigger condition expression.

    Supported formats:
      "economy.hope < 30"           — economy resource below threshold
      "pressure.mystery > 70"       — pressure axis above threshold
      "phase == tension"            — campaign phase matches
      "session_since_last_action > 2" — players haven't acted in N sessions
      "intrigue.i001.clues < 2"     — intrigue has fewer than N revealed clues
      "intrigue.i001.status == active"
      "economy.hope < 30 AND pressure.violence < 20"  — combined conditions
      "true"                        — always fires (for manual triggers)
    """
    condition = condition.strip()
    if not condition or condition == "true":
        return True

    # Handle AND
    if " AND " in condition:
        parts = condition.split(" AND ")
        return all(_eval_condition(p.strip(), context) for p in parts)

    # Handle OR
    if " OR " in condition:
        parts = condition.split(" OR ")
        return any(_eval_condition(p.strip(), context) for p in parts)

    # Parse "field operator value"
    # Supported: <, >, <=, >=, ==, !=
    for op in ("<=", ">=", "!=", "==", "<", ">"):
        if op in condition:
            parts = condition.split(op, 1)
            if len(parts) != 2:
                continue
            field = parts[0].strip()
            value_str = parts[1].strip()
            actual = _resolve_field(field, context)
            return _compare(actual, op, value_str)

    return False


def _resolve_field(field: str, context: dict):
    """Resolve a dot-path field reference to its value."""
    parts = field.split(".")
    current = context

    for i, part in enumerate(parts):
        if isinstance(current, dict):
            current = current.get(part, None)
        elif isinstance(current, list):
            # Try to find by id (e.g. intrigue.i001)
            for item in current:
                if isinstance(item, dict) and item.get("id") == part:
                    current = item
                    break
            else:
                return None
        else:
            return None

    return current


def _compare(actual, op: str, value_str: str) -> bool:
    """Compare actual to value_str using operator."""
    # Try numeric comparison first
    try:
        value = float(value_str)
        actual_num = float(actual) if actual is not None else 0
        if op == "<":  return actual_num < value
        if op == ">":  return actual_num > value
        if op == "<=": return actual_num <= value
        if op == ">=": return actual_num >= value
        if op == "==": return actual_num == value
        if op == "!=": return actual_num != value
    except (ValueError, TypeError):
        pass

    # String comparison
    actual_str = str(actual) if actual is not None else ""
    if op == "==": return actual_str == value_str
    if op == "!=": return actual_str != value_str
    return False


def _build_context(campaign: str, current_session: int) -> dict:
    """Build the evaluation context from all state files."""
    pressure = _read_pressure(campaign)
    economy = _read_economy(campaign)
    campaign_state = _read_campaign_state(campaign)
    intrigues = _read_intrigues(campaign)

    # Compute session_since_last_action from beat_history
    beat_history = campaign_state.get("beat_history", [])
    last_session = max((h.get("session", 0) for h in beat_history), default=0)
    session_since_last = max(0, current_session - last_session) if last_session else current_session

    return {
        "economy": economy,
        "pressure": pressure,
        "phase": campaign_state.get("phase", "stability"),
        "round": campaign_state.get("round", 0),
        "session": current_session,
        "session_since_last_action": session_since_last,
        "intrigue": intrigues,  # list — _resolve_field handles id lookup
    }


# ── State mutation ──────────────────────────────────────────────────────────

def _apply_mutations(campaign: str, mutations: dict) -> list:
    """Apply state mutations to pressure.json and economy.json.

    Returns a list of human-readable change descriptions.
    """
    changes = []

    # Pressure mutations
    pressure_path = find_campaign(campaign) / "pressure.json"
    if "pressure" in mutations and pressure_path.exists():
        pdata = _load_json(pressure_path, {"axes": {}})
        axes = pdata.setdefault("axes", {})
        for axis, delta in mutations["pressure"].items():
            old = axes.get(axis, 0)
            new = max(0, min(100, old + delta))
            axes[axis] = new
            changes.append(f"pressure.{axis}: {old} → {new} ({delta:+d})")
        pdata["axes"] = axes
        pressure_path.write_text(json.dumps(pdata, indent=2, ensure_ascii=False), encoding="utf-8")

    # Economy mutations
    economy_path = find_campaign(campaign) / "economy.json"
    if "economy" in mutations and economy_path.exists():
        edata = _load_json(economy_path, {"resources": {}})
        resources = edata.setdefault("resources", {})
        for res, delta in mutations["economy"].items():
            old = resources.get(res, 0)
            new = max(0, min(100, old + delta))
            resources[res] = new
            changes.append(f"economy.{res}: {old} → {new} ({delta:+d})")
        edata["resources"] = resources
        economy_path.write_text(json.dumps(edata, indent=2, ensure_ascii=False), encoding="utf-8")

    # Campaign state mutations (phase change, etc.)
    state_path = find_campaign(campaign) / "campaign_state.json"
    if "campaign_state" in mutations and state_path.exists():
        sdata = _load_json(state_path, {})
        for key, value in mutations["campaign_state"].items():
            old = sdata.get(key)
            sdata[key] = value
            changes.append(f"campaign_state.{key}: {old} → {value}")
        state_path.write_text(json.dumps(sdata, indent=2, ensure_ascii=False), encoding="utf-8")

    return changes


# ── Commands ────────────────────────────────────────────────────────────────

def cmd_add(args) -> int:
    """Register an NPC drive."""
    data = _load(args.campaign)
    try:
        drive = json.loads(args.json)
    except json.JSONDecodeError as e:
        print(f"error: invalid JSON: {e}", file=sys.stderr)
        return 1

    if "id" not in drive or "npc_id" not in drive or "goal" not in drive:
        print("error: drive must have 'id', 'npc_id', and 'goal'", file=sys.stderr)
        return 1

    drive.setdefault("trigger_conditions", [])  # list of condition strings (AND)
    drive.setdefault("mutations", {})           # {pressure: {axis: delta}, economy: {res: delta}}
    drive.setdefault("narrative_prompt", "")    # what the GM/LLM should narrate
    drive.setdefault("world_event", "")         # short label for the event
    drive.setdefault("cooldown", 3)             # sessions before this can fire again
    drive.setdefault("last_fired", None)        # session of last firing
    drive.setdefault("one_shot", False)         # fires only once
    drive.setdefault("fired_count", 0)          # how many times it has fired
    drive.setdefault("priority", 5)             # higher = checked first

    drive["npc_id"] = _norm_id(drive["npc_id"])

    existing_ids = {d["id"] for d in data["drives"]}
    if drive["id"] in existing_ids and not args.force:
        print(f"error: drive '{drive['id']}' already exists. Use --force.", file=sys.stderr)
        return 1

    if drive["id"] in existing_ids:
        data["drives"] = [d if d["id"] != drive["id"] else drive for d in data["drives"]]
    else:
        data["drives"].append(drive)

    _save(args.campaign, data)
    print(f"OK — drive '{drive['id']}' registered for {drive['npc_id']}")
    print(f"  goal: {drive['goal']}")
    print(f"  conditions: {len(drive['trigger_conditions'])}")
    print(f"  cooldown: {drive['cooldown']} sessions")
    print(f"  one_shot: {drive['one_shot']}")
    return 0


def cmd_list(args) -> int:
    """List all NPC drives."""
    data = _load(args.campaign)
    if not data["drives"]:
        print(f"# no NPC drives registered")
        return 0

    drives = data["drives"]
    if args.npc:
        npc_id = _norm_id(args.npc)
        drives = [d for d in drives if d.get("npc_id") == npc_id]

    drives.sort(key=lambda d: d.get("priority", 5), reverse=True)

    print(f"# {len(drives)} NPC drive(s)\n")
    print(f"{'ID':<8} {'NPC':<18} {'Goal':<35} {'Ready':>5} {'Fired':>5}")
    print("-" * 75)
    for d in drives:
        goal = d.get("goal", "?")[:35]
        ready = "✓" if d.get("last_fired") is None else "?"
        fired = d.get("fired_count", 0)
        print(f"{d['id']:<8} {d.get('npc_id', '?'):<18} {goal:<35} {ready:>5} {fired:>5}")
    return 0


def cmd_show(args) -> int:
    """Show full detail for a drive."""
    data = _load(args.campaign)
    drive = _find(data, args.id)
    if not drive:
        print(f"# drive '{args.id}' not found", file=sys.stderr)
        return 1
    print(json.dumps(drive, indent=2, ensure_ascii=False))
    return 0


def cmd_check(args) -> int:
    """Check all NPC drives against current state. Fire any whose conditions
    are met and cooldown has expired.

    This is the core of the closed-loop simulation. Called at /gm load or
    /gm end. Returns a diff of what happened off-screen.
    """
    data = _load(args.campaign)
    context = _build_context(args.campaign, args.session)

    fired = []
    skipped = []

    # Sort by priority (highest first)
    drives = sorted(data["drives"], key=lambda d: d.get("priority", 5), reverse=True)

    for drive in drives:
        # Skip if one-shot and already fired
        if drive.get("one_shot") and drive.get("fired_count", 0) > 0:
            continue

        # Check cooldown
        last_fired = drive.get("last_fired")
        cooldown = drive.get("cooldown", 3)
        if last_fired is not None:
            sessions_since = args.session - last_fired
            if sessions_since < cooldown:
                skipped.append({
                    "drive": drive["id"],
                    "npc": drive.get("npc_id", "?"),
                    "goal": drive.get("goal", "?"),
                    "reason": f"cooldown ({sessions_since}/{cooldown} sessions)",
                })
                continue

        # Evaluate trigger conditions
        conditions = drive.get("trigger_conditions", [])
        if not conditions:
            # No conditions = manual trigger only, skip auto-check
            continue

        all_met = all(_eval_condition(cond, context) for cond in conditions)

        if all_met:
            # Fire the drive
            result = _fire_drive(args.campaign, drive, args.session)
            fired.append(result)
        else:
            skipped.append({
                "drive": drive["id"],
                "npc": drive.get("npc_id", "?"),
                "goal": drive.get("goal", "?"),
                "reason": "conditions not met",
            })

    # Save updated drive states (last_fired, fired_count)
    _save(args.campaign, data)

    # Output
    if not fired:
        print(f"# no NPC drives fired at session {args.session}")
        if skipped:
            print(f"# {len(skipped)} drive(s) checked but not fired:")
            for s in skipped[:5]:
                print(f"   {s['drive']} ({s['npc']}): {s['reason']}")
        return 0

    print(f"# {len(fired)} NPC drive(s) fired at session {args.session}\n")

    for i, result in enumerate(fired, 1):
        print(f"## Drive {i}: {result['drive_id']} — {result['npc']}")
        print(f"   goal: {result['goal']}")
        print(f"   world event: {result['world_event']}")
        print(f"\n   state mutations:")
        for change in result["changes"]:
            print(f"     • {change}")
        print(f"\n   narrative prompt:")
        print(f"     {result['narrative_prompt']}")
        print()

    # Diff for state.md → Faction Moves
    print(f"## Diff for state.md → Faction Moves:")
    for result in fired:
        print(f"  - {result['npc']}: {result['world_event']}")
        for change in result["changes"]:
            print(f"    → {change}")

    print(f"\n## Director guidance:")
    print(f"   The state has been mutated. Run the director to see the new recommendation:")
    print(f"   python3 scripts/director.py --campaign {args.campaign} recommend --session {args.session}")
    return 0


def _fire_drive(campaign: str, drive: dict, session: int) -> dict:
    """Fire a single drive — apply mutations, update drive state, return result."""
    # Apply state mutations
    mutations = drive.get("mutations", {})
    changes = _apply_mutations(campaign, mutations)

    # Update drive state
    drive["last_fired"] = session
    drive["fired_count"] = drive.get("fired_count", 0) + 1

    return {
        "drive_id": drive["id"],
        "npc": drive.get("npc_id", "?"),
        "goal": drive.get("goal", "?"),
        "world_event": drive.get("world_event", drive.get("goal", "?")),
        "narrative_prompt": drive.get("narrative_prompt", ""),
        "changes": changes,
    }


def cmd_fire(args) -> int:
    """Manually fire a drive (GM override)."""
    data = _load(args.campaign)
    drive = _find(data, args.id)
    if not drive:
        print(f"# drive '{args.id}' not found", file=sys.stderr)
        return 1

    result = _fire_drive(args.campaign, drive, args.session)
    _save(args.campaign, data)

    print(f"# MANUALLY FIRED: {result['drive_id']} — {result['npc']}")
    print(f"   goal: {result['goal']}")
    print(f"   world event: {result['world_event']}")
    print(f"\n   state mutations:")
    for change in result["changes"]:
        print(f"     • {change}")
    print(f"\n   narrative prompt:")
    print(f"     {result['narrative_prompt']}")
    return 0


def cmd_reset_cooldown(args) -> int:
    """Reset a drive's cooldown so it can fire again immediately."""
    data = _load(args.campaign)
    drive = _find(data, args.id)
    if not drive:
        print(f"# drive '{args.id}' not found", file=sys.stderr)
        return 1

    drive["last_fired"] = None
    _save(args.campaign, data)
    print(f"OK — cooldown reset for {args.id}")
    return 0


# ── CLI ─────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--campaign", required=True, help="Campaign name")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("add", help="Register an NPC drive")
    s.add_argument("json", help="Drive JSON")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("list", help="List all drives")
    s.add_argument("--npc", help="Filter by NPC")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("show", help="Show full drive detail")
    s.add_argument("--id", required=True)
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("check", help="Check all drives and fire any whose conditions are met")
    s.add_argument("--session", type=int, required=True)
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("fire", help="Manually fire a drive (GM override)")
    s.add_argument("--id", required=True)
    s.add_argument("--session", type=int, default=0)
    s.set_defaults(func=cmd_fire)

    s = sub.add_parser("reset-cooldown", help="Reset a drive's cooldown")
    s.add_argument("--id", required=True)
    s.set_defaults(func=cmd_reset_cooldown)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
