#!/usr/bin/env python3
"""
plans.py — NPC agency engine + layered intrigue tracker.

Layer 4 of the hierarchical GM tracking system. Provides:

  1. NPC plan registry (plans.json) — persistent multi-session plans with
     steps, deadlines, resources, failure conditions, adaptation triggers.
     Plan depth is tied to intrigue layer: core-layer antagonists get full
     state-machine plans; everyone else gets light plans.

  2. Layered intrigue tracker (intrigues.json) — nested mysteries with
     parent/child links, unlock conditions, reveal thresholds. A campaign
     has 1-3 root intrigues (Layer A) with unbounded children (Layer B,
     C, ...) that unlock as parent layers resolve.

  3. Arc beat ↔ intrigue coupling — arc beats can be gated by intrigue
     state (beat 2a can't advance until Layer B has N revealed clues)
     and can force-reveal clues when players dawdle.

  4. Off-screen advancement — deterministic Python diff that advances
     NPC plan steps between sessions based on ETA, no LLM call required.

  5. Emergence — `promote-foreshadow` turns a foreshadowed element from
     scene_index.json into a new child intrigue, linked to its parent.
     This is what keeps the deep tracking from calcifying into a railroad.

Storage:
  <campaign-dir>/plans.json
  <campaign-dir>/intrigues.json

LLM-agnostic. All advancement and queries are deterministic Python. The
GM drafts plans and intrigues (with model assistance at /gm init intrigue
and /gm save); this script validates, stores, and advances them.

Usage:
  # NPC plans
  python3 plans.py plan-show --campaign <name> --npc velkyn
  python3 plans.py plan-add --campaign <name> --npc velkyn '<json>'
  python3 plans.py plan-advance --campaign <name> --to-session 15
  python3 plans.py plan-adapt --campaign <name> --npc velkyn --trigger "..."
  python3 plans.py plan-complete --campaign <name> --npc velkyn --outcome success --summary "..."
  python3 plans.py plan-history --campaign <name> --npc velkyn

  # Intrigues
  python3 plans.py intrigue-show --campaign <name> --id i001
  python3 plans.py intrigue-list --campaign <name> [--layer A] [--status active]
  python3 plans.py intrigue-reveal --campaign <name> --id i001 --clue "..." --session 15
  python3 plans.py intrigue-resolve --campaign <name> --id i001 --resolution "..."
  python3 plans.py intrigue-unlock --campaign <name> --id i002  # manually unlock

  # Heat / pressure tracking
  python3 plans.py intrigue-heat --campaign <name> --id i001 --heat 73 --stability 41
  python3 plans.py intrigue-heat-show --campaign <name> --id i001
  python3 plans.py intrigue-heat-trigger --campaign <name> --id i001 --event failed_stealth

  # Confidence per clue
  python3 plans.py intrigue-reveal --campaign <name> --id i001 --clue "..." \
      --confidence confirmed    # confirmed|suspected|rumor|false|fabricated

  # Competing intrigues
  python3 plans.py intrigue-compete --campaign <name> --id i001 \
      --weakens i002 --strengthens i003

  # Deadlines / time decay
  python3 plans.py intrigue-deadline --campaign <name> --id i001 \
      --deadline session:20 --outcome "trail goes cold"
  python3 plans.py intrigue-check-deadlines --campaign <name> --to-session 21

  # Richer dependencies
  python3 plans.py intrigue-dependency --campaign <name> --id i001 \
      --requires i002 --blocks i003 --reveals i004 \
      --invalidates i005 --accelerates i006

  # Arc coupling
  python3 plans.py arc-check --campaign <name> --beat 2a
  python3 plans.py arc-advance --campaign <name> --beat 2a
  python3 plans.py arc-force-reveal --campaign <name> --beat 2b

  # Emergence
  python3 plans.py promote-foreshadow --campaign <name> --scene s015 --index 2 \
      --parent i001 --title "The Western Marches Cousin" --layer B

  # Faction summary
  python3 plans.py factions --campaign <name>
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

def _plans_path(campaign: str) -> Path:
    return find_campaign(campaign) / "plans.json"


def _intrigues_path(campaign: str) -> Path:
    return find_campaign(campaign) / "intrigues.json"


def _load_plans(campaign: str) -> dict:
    p = _plans_path(campaign)
    if not p.exists():
        return {"version": 2, "npcs": {}, "factions": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": 2, "npcs": {}, "factions": {}}
    data.setdefault("version", 2)
    data.setdefault("npcs", {})
    data.setdefault("factions", {})
    return data


def _save_plans(campaign: str, data: dict) -> None:
    p = _plans_path(campaign)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ── Entity state integration ────────────────────────────────────────────────

def _entity_state_path(campaign: str) -> Path:
    return find_campaign(campaign) / "entity_state.json"


def _load_entity_state(campaign: str) -> dict:
    p = _entity_state_path(campaign)
    if not p.exists():
        return {"version": 1, "entities": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "entities": {}}
    data.setdefault("version", 1)
    data.setdefault("entities", {})
    return data


def _save_entity_state(campaign: str, data: dict) -> None:
    p = _entity_state_path(campaign)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _get_or_create_entity(es_data: dict, npc_id: str, name: str = "") -> dict:
    entities = es_data.setdefault("entities", {})
    if npc_id not in entities:
        entities[npc_id] = {
            "name": name or npc_id.split(":")[-1],
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


def _get_disposition(campaign: str, npc_id: str) -> str:
    """Read NPC disposition from entity_state.json (canonical source)."""
    es = _load_entity_state(campaign)
    entity = es.get("entities", {}).get(npc_id, {})
    return entity.get("relationship_to_party", "neutral")


def _get_trust(campaign: str, npc_id: str) -> int:
    """Read NPC trust from entity_state.json (canonical source)."""
    es = _load_entity_state(campaign)
    entity = es.get("entities", {}).get(npc_id, {})
    return entity.get("trust", 0)


def _load_intrigues(campaign: str) -> dict:
    p = _intrigues_path(campaign)
    if not p.exists():
        return {"version": 2, "intrigues": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": 2, "intrigues": []}
    data.setdefault("version", 2)
    data.setdefault("intrigues", [])
    return data


def _save_intrigues(campaign: str, data: dict) -> None:
    p = _intrigues_path(campaign)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ── Helpers ─────────────────────────────────────────────────────────────────

LAYER_DEPTH_MAP = {
    "A": "light",      # surface — light plans
    "B": "light",      # mid — light plans
    "C": "full",       # core — full state-machine
    "D": "full",       # deeper — full state-machine
}


def _plan_depth_for_layer(layer: str) -> str:
    """Determine plan depth from intrigue layer. Core-layer antagonists
    get full state-machine plans; everyone else gets light plans."""
    return LAYER_DEPTH_MAP.get(layer.upper(), "light")


def _find_intrigue(data: dict, intrigue_id: str) -> Optional[dict]:
    for i in data["intrigues"]:
        if i["id"] == intrigue_id:
            return i
    return None


def _find_npc_plan(data: dict, npc_id: str) -> Optional[dict]:
    return data["npcs"].get(npc_id)


def _intrigue_unlocked(intrigue: dict, all_intrigues: list, revealed_clues_cache: dict) -> bool:
    """Check if an intrigue is unlocked based on its parent's state."""
    if intrigue.get("status") in ("resolved", "abandoned"):
        return False
    if intrigue.get("status") == "active":
        return True
    # Check unlock_condition
    parent_id = intrigue.get("parent_intrigue")
    if not parent_id:
        return intrigue.get("status") != "hidden"
    parent = _find_intrigue({"intrigues": all_intrigues}, parent_id)
    if not parent:
        return False
    if parent.get("status") != "resolved" and parent.get("status") != "active":
        return False
    # Check reveal_threshold on parent
    threshold = parent.get("reveal_threshold", 0)
    parent_clues = len(parent.get("revealed_clues", []))
    if parent_clues < threshold:
        return False
    return True


def _check_unlock_condition(condition: str, context: dict) -> bool:
    """Evaluate a simple unlock condition expression.

    Supported formats:
      "intrigue:i001:clues>=3"  — parent intrigue has >= 3 revealed clues
      "intrigue:i001:resolved"  — parent intrigue is resolved
      "arc:2a:complete"         — arc beat 2a is complete
      "session>=15"             — current session >= 15
    """
    if not condition:
        return True
    condition = condition.strip()

    if condition.startswith("intrigue:"):
        parts = condition.split(":")
        if len(parts) < 3:
            return False
        intr_id = parts[1]
        check = parts[2]
        intr = _find_intrigue({"intrigues": context.get("intrigues", [])}, intr_id)
        if not intr:
            return False
        if check == "resolved":
            return intr.get("status") == "resolved"
        if check == "active":
            return intr.get("status") == "active"
        if ">=" in check:
            field, val = check.split(">=")
            val = int(val.strip())
            if field.strip() == "clues":
                return len(intr.get("revealed_clues", [])) >= val
        return False

    if condition.startswith("arc:"):
        parts = condition.split(":")
        if len(parts) < 3:
            return False
        beat_id = parts[1]
        check = parts[2]
        completed_beats = context.get("completed_beats", set())
        if check == "complete":
            return beat_id in completed_beats
        return False

    if condition.startswith("session>="):
        val = int(condition.split(">=")[1].strip())
        return context.get("current_session", 0) >= val

    return False


# ── NPC Plan commands ───────────────────────────────────────────────────────

def cmd_plan_show(args) -> int:
    """Show an NPC's current plan."""
    data = _load_plans(args.campaign)
    npc_id = args.npc if ":" in args.npc else f"npc:{args.npc}"
    npc = data["npcs"].get(npc_id)
    if not npc:
        print(f"# no plan registered for '{args.npc}'", file=sys.stderr)
        return 1
    print(json.dumps(npc, indent=2, ensure_ascii=False))
    return 0


def cmd_plan_add(args) -> int:
    """Add or replace an NPC's plan."""
    data = _load_plans(args.campaign)
    npc_id = args.npc if ":" in args.npc else f"npc:{args.npc}"

    try:
        plan = json.loads(args.json)
    except json.JSONDecodeError as e:
        print(f"error: invalid JSON: {e}", file=sys.stderr)
        return 1

    # Determine plan depth from intrigue layer if specified
    layer = plan.get("intrigue_layer") or args.layer
    if layer:
        plan["intrigue_layer"] = layer
        plan["plan_depth"] = _plan_depth_for_layer(layer)
    else:
        plan.setdefault("plan_depth", "light")

    plan["last_advanced"] = plan.get("last_advanced", 0)
    plan.setdefault("history", [])

    # Preserve existing NPC metadata if updating
    existing = data["npcs"].get(npc_id, {})
    for key in ("name", "faction", "archetype"):
        if key in existing and key not in plan:
            plan[key] = existing[key]

    # ── Entity state consolidation ──
    # disposition_toward_party and trust are now stored in entity_state.json
    # as the canonical source. plans.json still carries them as a convenience
    # copy (set by sync-from-plans), but the write path goes to entity_state.
    # If the plan JSON includes disposition/trust, write them to entity_state.
    disposition = plan.pop("disposition_toward_party", None)
    trust = plan.pop("trust", None)

    # Read current values from entity_state (or defaults)
    es_data = _load_entity_state(args.campaign)
    es_entity = _get_or_create_entity(es_data, npc_id, plan.get("name", npc_id.split(":")[-1]))

    if disposition is not None:
        es_entity["relationship_to_party"] = disposition
    if trust is not None:
        es_entity["trust"] = trust

    # Keep a read-only copy in plans.json for backwards compatibility
    plan["_disposition_ref"] = "entity_state.json"  # marker: canonical source is entity_state
    _save_entity_state(args.campaign, es_data)

    data["npcs"][npc_id] = plan
    _save_plans(args.campaign, data)
    print(f"OK — plan registered for {npc_id} "
          f"(depth: {plan['plan_depth']}, layer: {plan.get('intrigue_layer', 'none')})")
    print(f"  disposition: {es_entity['relationship_to_party']} (entity_state.json)")
    print(f"  trust: {es_entity['trust']} (entity_state.json)")
    return 0


def cmd_plan_advance(args) -> int:
    """Advance all NPC plans to the target session.

    This is the deterministic off-screen advancement engine. For each NPC:
      - If current_step has an ETA <= target session and its requires
        condition is met, mark it complete and advance current_step.
      - Check adaptation_triggers — not auto-fired, but reported.
      - Check failure_condition — if met, mark plan as failed.

    Returns a diff suitable for writing to state.md → ## Faction Moves.
    """
    data = _load_plans(args.campaign)
    intrigues = _load_intrigues(args.campaign)
    target = args.to_session

    advancements = []
    failures = []

    for npc_id, npc in data["npcs"].items():
        plan = npc.get("current_plan") or npc.get("plan")
        if not plan:
            continue
        if npc.get("last_advanced", 0) >= target:
            continue

        steps = plan.get("steps", [])
        current_step = plan.get("current_step", 1)
        changed = False

        while current_step <= len(steps):
            step = steps[current_step - 1]
            if step.get("status") in ("complete", "skipped"):
                current_step += 1
                continue

            # Check ETA
            eta = step.get("eta", "")
            eta_session = _parse_eta(eta)
            if eta_session is None or eta_session > target:
                break

            # Check requires condition
            requires = step.get("requires", "")
            if requires and not _check_step_requires(requires, intrigues, data):
                # Can't advance — requirement not met
                break

            # Advance
            step["status"] = "complete"
            step["completed_session"] = target
            advancements.append({
                "npc": npc_id,
                "name": npc.get("name", npc_id),
                "plan_id": plan.get("id", "?"),
                "step": current_step,
                "action": step.get("action", "?"),
                "completed_at": target,
            })
            current_step += 1
            changed = True

        if changed:
            plan["current_step"] = current_step
            npc["last_advanced"] = target

            # Check if plan is complete
            if current_step > len(steps):
                # Plan succeeded
                npc["history"].append({
                    "session": target,
                    "plan_id": plan.get("id", "?"),
                    "summary": plan.get("goal", "?"),
                    "outcome": "succeeded",
                })
                npc["current_plan"] = None

            # Check failure condition
            fail_cond = plan.get("failure_condition", "")
            if fail_cond and _check_failure(fail_cond, intrigues, data):
                failures.append({
                    "npc": npc_id,
                    "name": npc.get("name", npc_id),
                    "plan_id": plan.get("id", "?"),
                    "reason": fail_cond,
                })
                npc["history"].append({
                    "session": target,
                    "plan_id": plan.get("id", "?"),
                    "summary": plan.get("goal", "?"),
                    "outcome": f"failed: {fail_cond}",
                })
                npc["current_plan"] = None

    _save_plans(args.campaign, data)

    # Emit the diff
    if not advancements and not failures:
        print(f"# no plan advancements for session {target}")
        return 0

    print(f"# plan advancements to session {target}\n")
    if advancements:
        print("## Advanced:")
        for a in advancements:
            print(f"  - {a['name']} ({a['plan_id']} step {a['step']}): {a['action']}")
    if failures:
        print("\n## Failed:")
        for f in failures:
            print(f"  - {f['name']} ({f['plan_id']}): {f['reason']}")

    print("\n## Diff for state.md → Faction Moves:")
    for a in advancements:
        print(f"  - {a['name']}: {a['action']} (completed off-screen)")
    for f in failures:
        print(f"  - {f['name']}: plan failed — {f['reason']}")
    return 0


def _parse_eta(eta: str) -> Optional[int]:
    """Parse 'session:15' or '15' into session number."""
    if not eta:
        return None
    eta = eta.strip()
    if eta.startswith("session:"):
        try:
            return int(eta.split(":")[1])
        except (ValueError, IndexError):
            return None
    try:
        return int(eta)
    except ValueError:
        return None


def _check_step_requires(requires: str, intrigues: dict, plans: dict) -> bool:
    """Check if a step's requires condition is met.

    Formats:
      "step 2 complete" — earlier step in same plan is done
      "marquise_dore on-screen" — soft requirement, assume met
      "intrigue:i001:clues>=2" — delegate to _check_unlock_condition
    """
    requires = requires.strip()
    if not requires:
        return True

    if requires.startswith("intrigue:"):
        return _check_unlock_condition(requires, {
            "intrigues": intrigues.get("intrigues", []),
        })

    # "step N complete" is checked by the caller (step ordering)
    if requires.startswith("step ") and "complete" in requires:
        return True  # already enforced by sequential advancement

    # Soft requirements (NPC on-screen, party trust) — assume met for off-screen
    return True


def _check_failure(condition: str, intrigues: dict, plans: dict) -> bool:
    """Check if a plan's failure condition has been met."""
    if not condition:
        return False
    condition = condition.strip().lower()

    if condition.startswith("intrigue:"):
        return _check_unlock_condition(condition, {
            "intrigues": intrigues.get("intrigues", []),
        })

    # Free-text failure conditions need GM judgment — don't auto-fire
    return False


def cmd_plan_adapt(args) -> int:
    """Apply an adaptation trigger to an NPC's plan.

    The GM specifies which trigger fired; this script applies the
    pre-committed adaptation logic (advancing, skipping, or escalating).
    """
    data = _load_plans(args.campaign)
    npc_id = args.npc if ":" in args.npc else f"npc:{args.npc}"
    npc = data["npcs"].get(npc_id)
    if not npc:
        print(f"# no plan for {npc_id}", file=sys.stderr)
        return 1

    plan = npc.get("current_plan")
    if not plan:
        print(f"# {npc_id} has no active plan to adapt", file=sys.stderr)
        return 1

    triggers = plan.get("adaptation_triggers", [])
    matched = None
    for t in triggers:
        if args.trigger.lower() in t.lower():
            matched = t
            break

    if not matched:
        print(f"# no matching trigger for '{args.trigger}'", file=sys.stderr)
        print(f"# available triggers:")
        for t in triggers:
            print(f"  - {t}")
        return 1

    # Apply adaptation — the trigger text describes the action
    # (e.g. "skip to step 4", "escalate deadline to session:18")
    print(f"# matched trigger: {matched}")
    print(f"# NPC: {npc.get('name', npc_id)}")
    print(f"# Plan: {plan.get('id', '?')} — {plan.get('goal', '?')}")
    print(f"# Current step: {plan.get('current_step', 1)}")
    print()
    print("# To apply this adaptation, edit plans.json manually or use:")
    print(f"#   python3 plans.py plan-add --campaign {args.campaign} --npc {args.npc} '<updated JSON>'")
    print()
    print("# The trigger text describes what to change. The GM (with model")
    print("# assistance) interprets and applies it. This is NOT auto-applied")
    print("# because adaptation logic is too nuanced for deterministic execution.")

    if args.apply:
        # Mark the trigger as fired
        plan.setdefault("fired_triggers", []).append({
            "trigger": matched,
            "session": args.session or 0,
            "applied": date.today().isoformat(),
        })
        _save_plans(args.campaign, data)
        print(f"# trigger marked as fired in plans.json")

    return 0


def cmd_plan_complete(args) -> int:
    """Mark an NPC's current plan as complete (success or failure)."""
    data = _load_plans(args.campaign)
    npc_id = args.npc if ":" in args.npc else f"npc:{args.npc}"
    npc = data["npcs"].get(npc_id)
    if not npc:
        print(f"# no plan for {npc_id}", file=sys.stderr)
        return 1

    plan = npc.get("current_plan")
    if not plan:
        print(f"# {npc_id} has no active plan", file=sys.stderr)
        return 1

    npc.setdefault("history", []).append({
        "session": args.session or 0,
        "plan_id": plan.get("id", "?"),
        "summary": plan.get("goal", "?"),
        "outcome": args.outcome,
        "note": args.summary,
    })
    npc["current_plan"] = None
    npc["last_advanced"] = args.session or npc.get("last_advanced", 0)

    _save_plans(args.campaign, data)
    print(f"OK — plan {plan.get('id', '?')} for {npc_id} marked as {args.outcome}")
    return 0


def cmd_plan_history(args) -> int:
    """Show an NPC's plan history."""
    data = _load_plans(args.campaign)
    npc_id = args.npc if ":" in args.npc else f"npc:{args.npc}"
    npc = data["npcs"].get(npc_id)
    if not npc:
        print(f"# no plan for {npc_id}", file=sys.stderr)
        return 1

    print(f"# plan history for {npc.get('name', npc_id)}\n")
    for entry in npc.get("history", []):
        print(f"  [s{entry.get('session', '?'):>3}] {entry.get('plan_id', '?')}: "
              f"{entry.get('outcome', '?')} — {entry.get('summary', '')}")
        if entry.get("note"):
            print(f"         note: {entry['note']}")

    current = npc.get("current_plan")
    if current:
        print(f"\n# current plan: {current.get('id', '?')} — {current.get('goal', '?')}")
        print(f"  step {current.get('current_step', 1)} of {len(current.get('steps', []))}")
    return 0


# ── Intrigue commands ───────────────────────────────────────────────────────

def cmd_intrigue_show(args) -> int:
    """Show full detail for an intrigue."""
    data = _load_intrigues(args.campaign)
    intr = _find_intrigue(data, args.id)
    if not intr:
        print(f"# intrigue '{args.id}' not found", file=sys.stderr)
        return 1
    print(json.dumps(intr, indent=2, ensure_ascii=False))
    return 0


def cmd_intrigue_list(args) -> int:
    """List intrigues, optionally filtered by layer or status."""
    data = _load_intrigues(args.campaign)
    if not data["intrigues"]:
        print(f"# intrigues.json not initialized for '{args.campaign}'")
        print(f"# run: python3 scripts/intrigue_init.py --campaign {args.campaign}")
        return 0

    intrs = data["intrigues"]
    if args.layer:
        intrs = [i for i in intrs if i.get("layer", "A") == args.layer.upper()]
    if args.status:
        intrs = [i for i in intrs if i.get("status") == args.status]
    if args.root:
        intrs = [i for i in intrs if not i.get("parent_intrigue")]

    # Sort by layer then id
    intrs.sort(key=lambda i: (i.get("layer", "A"), i["id"]))

    print(f"# {len(intrs)} intrigue(s)\n")
    print(f"{'ID':<8} {'Layer':>5} {'Status':<10} {'Parent':<8} {'Title'}")
    print("-" * 70)
    for i in intrs:
        parent = i.get("parent_intrigue") or "—"
        print(f"{i['id']:<8} {i.get('layer', 'A'):>5} {i.get('status', '?'):<10} "
              f"{parent:<8} {i.get('title', '?')}")

    # Show hierarchy tree
    if args.tree:
        print("\n# Hierarchy:")
        roots = [i for i in intrs if not i.get("parent_intrigue")]
        for root in roots:
            _print_intrigue_tree(root, data["intrigues"], indent=0)
    return 0


def _print_intrigue_tree(intrigue: dict, all_intrigues: list, indent: int) -> None:
    prefix = "  " * indent
    status_icon = {"active": "●", "hidden": "○", "locked": "🔒",
                   "resolved": "✓", "abandoned": "✗"}.get(intrigue.get("status", ""), "?")
    print(f"{prefix}{status_icon} [{intrigue.get('layer', 'A')}] {intrigue['id']}: {intrigue.get('title', '?')}")
    children = [i for i in all_intrigues if i.get("parent_intrigue") == intrigue["id"]]
    for child in children:
        _print_intrigue_tree(child, all_intrigues, indent + 1)


# Confidence levels for revealed clues
CONFIDENCE_LEVELS = {
    "confirmed": 1.0,    # counts fully toward thresholds
    "suspected": 0.5,   # counts at half weight
    "rumor": 0.25,      # counts at quarter weight
    "false": 0.0,       # doesn't count (player theory is wrong)
    "fabricated": 0.0,  # doesn't count (planted misinformation)
}
DEFAULT_CONFIDENCE = "confirmed"


def _count_confirmed_clues(intrigue: dict, min_weight: float = 0.5) -> float:
    """Count revealed clues weighted by confidence.

    By default counts confirmed (1.0) + suspected (0.5) toward thresholds.
    Rumors (0.25) and below don't count unless min_weight is lowered.
    """
    total = 0.0
    for clue in intrigue.get("revealed_clues", []):
        conf = clue.get("confidence", DEFAULT_CONFIDENCE)
        weight = CONFIDENCE_LEVELS.get(conf, 0.0)
        if weight >= min_weight:
            total += weight
    return total


def _accelerate_deadline(deadline: str, pull_forward_sessions: int) -> str:
    """Pull a session:N deadline forward by N sessions."""
    if not deadline or not deadline.startswith("session:"):
        return deadline
    try:
        current = int(deadline.split(":")[1])
        new = max(1, current - pull_forward_sessions)
        return f"session:{new}"
    except (ValueError, IndexError):
        return deadline


def _apply_dependency_effects(campaign: str, intr: dict, data: dict, trigger: str) -> None:
    """Apply dependency effects when an intrigue state changes.

    trigger is "reveal" or "resolve". Effects:
      - reveals: unlock the target intrigue (on reveal or resolve)
      - accelerates: pull target's deadline forward (on resolve)
      - invalidates: abandon the target (on resolve)
      - blocks: prevent target from advancing (checked at target's arc-check)
    """
    effects = []

    if trigger == "reveal":
        # Reveal effects: unlock any intrigue this one "reveals"
        for other_id in intr.get("reveals", []):
            other = _find_intrigue(data, other_id)
            if other and other.get("status") in ("hidden", "locked"):
                other["status"] = "active"
                effects.append(f"revealed {other_id}")

    if effects:
        _save_intrigues(campaign, data)
        print(f"\n# dependency effects ({trigger}):")
        for e in effects:
            print(f"  - {e}")


def cmd_intrigue_reveal(args) -> int:
    """Reveal a clue for an intrigue.

    The --confidence flag sets the epistemic status of the clue:
      confirmed   — the party has verified proof (default)
      suspected   — the party has a strong theory but no proof
      rumor       — the party heard it second-hand
      false       — the party believes this but it's wrong
      fabricated  — someone planted this to mislead the party

    Only confirmed and suspected clues count toward reveal_threshold and
    arc_beat_gate min_clues (suspected at half weight).
    """
    data = _load_intrigues(args.campaign)
    intr = _find_intrigue(data, args.id)
    if not intr:
        print(f"# intrigue '{args.id}' not found", file=sys.stderr)
        return 1

    confidence = (args.confidence or DEFAULT_CONFIDENCE).lower()
    if confidence not in CONFIDENCE_LEVELS:
        print(f"error: invalid confidence '{confidence}'. "
              f"Valid: {sorted(CONFIDENCE_LEVELS.keys())}", file=sys.stderr)
        return 1

    # Move from unrevealed to revealed
    unrevealed = intr.get("unrevealed_clues", [])
    revealed = intr.setdefault("revealed_clues", [])

    # Check if this clue was in the unrevealed list
    found = False
    if args.clue in unrevealed:
        unrevealed.remove(args.clue)
        found = True
    elif args.remove_from_unrevealed:
        # Clue wasn't pre-listed; just add to revealed
        found = True

    revealed.append({
        "session": args.session,
        "clue": args.clue,
        "scene": args.scene or "",
        "confidence": confidence,
    })

    # Increase player_attention when a clue is revealed
    intr["player_attention"] = min(100, intr.get("player_attention", 0) + 10)

    intr["unrevealed_clues"] = unrevealed
    _save_intrigues(args.campaign, data)

    print(f"OK — clue revealed for {args.id} (session {args.session})")
    print(f"  clue: {args.clue}")
    print(f"  confidence: {confidence} (weight: {CONFIDENCE_LEVELS[confidence]})")
    confirmed_count = _count_confirmed_clues(intr)
    print(f"  confirmed clues: {confirmed_count:.1f}/{intr.get('reveal_threshold', '?')} threshold")
    print(f"  player_attention: {intr.get('player_attention', 0)}")

    # Check if this unlocks children
    threshold = intr.get("reveal_threshold", 0)
    if threshold and confirmed_count >= threshold:
        children = [i for i in data["intrigues"]
                    if i.get("parent_intrigue") == args.id]
        if children:
            print(f"\n# reveal threshold met — children may unlock:")
            for c in children:
                unlock = c.get("unlock_condition", "")
                if not unlock or _check_unlock_condition(unlock, {"intrigues": data["intrigues"]}):
                    if c.get("status") in ("hidden", "locked"):
                        print(f"  → {c['id']}: {c.get('title', '?')} (can unlock)")
                else:
                    print(f"  ⊙ {c['id']}: {c.get('title', '?')} (condition: {unlock})")

    # Check if this triggers any dependency effects (reveals/accelerates)
    _apply_dependency_effects(args.campaign, intr, data, "reveal")
    return 0


def cmd_intrigue_resolve(args) -> int:
    """Mark an intrigue as resolved.

    Resolution triggers several cascade effects:
      1. Auto-unlock children whose unlock conditions are met
      2. Apply competing intrigue effects (weakens_on_resolve / strengthens_on_resolve)
      3. Apply dependency effects (reveals / invalidates / accelerates / blocks)
    """
    data = _load_intrigues(args.campaign)
    intr = _find_intrigue(data, args.id)
    if not intr:
        print(f"# intrigue '{args.id}' not found", file=sys.stderr)
        return 1

    intr["status"] = "resolved"
    intr["resolution"] = args.resolution
    intr["resolved_session"] = args.session

    effects = []

    # 1. Auto-unlock children
    children = [i for i in data["intrigues"] if i.get("parent_intrigue") == args.id]
    unlocked = []
    for child in children:
        if child.get("status") in ("hidden", "locked"):
            child["status"] = "active"
            child["activated_session"] = args.session
            unlocked.append(child["id"])
    if unlocked:
        effects.append(f"unlocked children: {', '.join(unlocked)}")

    # 2. Apply competing intrigue effects
    weakens = intr.get("weakens_on_resolve", [])
    strengthens = intr.get("strengthens_on_resolve", [])
    for other_id in weakens:
        other = _find_intrigue(data, other_id)
        if other and other.get("status") == "active":
            other["stability"] = max(0, other.get("stability", 50) - 20)
            other["heat"] = min(100, other.get("heat", 50) + 15)
            effects.append(f"weakened {other_id} (stability -20, heat +15)")
    for other_id in strengthens:
        other = _find_intrigue(data, other_id)
        if other and other.get("status") == "active":
            other["stability"] = min(100, other.get("stability", 50) + 15)
            other["player_attention"] = min(100, other.get("player_attention", 0) + 20)
            effects.append(f"strengthened {other_id} (stability +15, attention +20)")

    # 3. Apply dependency effects
    for other_id in intr.get("invalidates", []):
        other = _find_intrigue(data, other_id)
        if other and other.get("status") == "active":
            other["status"] = "abandoned"
            other["abandoned_reason"] = f"invalidated by resolution of {args.id}"
            effects.append(f"invalidated {other_id} (abandoned)")
    for other_id in intr.get("accelerates", []):
        other = _find_intrigue(data, other_id)
        if other and other.get("status") == "active":
            # Pull deadline forward by 3 sessions
            old_deadline = other.get("deadline", "")
            new_deadline = _accelerate_deadline(old_deadline, 3)
            if new_deadline != old_deadline:
                other["deadline"] = new_deadline
                effects.append(f"accelerated {other_id} (deadline: {old_deadline} → {new_deadline})")
    for other_id in intr.get("reveals", []):
        other = _find_intrigue(data, other_id)
        if other and other.get("status") in ("hidden", "locked"):
            other["status"] = "active"
            other["activated_session"] = args.session
            effects.append(f"revealed {other_id} (now active)")

    _save_intrigues(args.campaign, data)
    print(f"OK — intrigue {args.id} resolved (session {args.session})")
    if effects:
        print(f"\n# cascade effects:")
        for e in effects:
            print(f"  - {e}")
    return 0


def cmd_intrigue_unlock(args) -> int:
    """Manually unlock an intrigue."""
    data = _load_intrigues(args.campaign)
    intr = _find_intrigue(data, args.id)
    if not intr:
        print(f"# intrigue '{args.id}' not found", file=sys.stderr)
        return 1

    intr["status"] = "active"
    intr["activated_session"] = args.session
    _save_intrigues(args.campaign, data)
    print(f"OK — intrigue {args.id} unlocked (session {args.session})")
    return 0


def cmd_intrigue_add(args) -> int:
    """Add a new intrigue to the registry."""
    data = _load_intrigues(args.campaign)
    try:
        intr = json.loads(args.json)
    except json.JSONDecodeError as e:
        print(f"error: invalid JSON: {e}", file=sys.stderr)
        return 1

    # Validate
    if "id" not in intr or "title" not in intr:
        print("error: intrigue must have 'id' and 'title'", file=sys.stderr)
        return 1

    intr.setdefault("layer", args.layer or "A")
    intr.setdefault("status", "active" if not intr.get("parent_intrigue") else "hidden")
    intr.setdefault("parent_intrigue", None)
    intr.setdefault("unlocks", [])
    intr.setdefault("unlock_condition", None)
    intr.setdefault("reveal_condition", None)
    intr.setdefault("reveal_threshold", 0)
    intr.setdefault("revealed_clues", [])
    intr.setdefault("unrevealed_clues", [])
    intr.setdefault("arc_beat_gate", None)
    intr.setdefault("force_reveal_on_beat", None)
    intr.setdefault("introduced_session", args.session or 0)

    # Check for duplicate
    existing_ids = {i["id"] for i in data["intrigues"]}
    if intr["id"] in existing_ids and not args.force:
        print(f"error: intrigue '{intr['id']}' already exists. Use --force.",
              file=sys.stderr)
        return 1

    if intr["id"] in existing_ids:
        data["intrigues"] = [i if i["id"] != intr["id"] else intr for i in data["intrigues"]]
    else:
        data["intrigues"].append(intr)

    # Update parent's "unlocks" list
    if intr.get("parent_intrigue"):
        parent = _find_intrigue(data, intr["parent_intrigue"])
        if parent:
            if intr["id"] not in parent.get("unlocks", []):
                parent.setdefault("unlocks", []).append(intr["id"])

    _save_intrigues(args.campaign, data)
    print(f"OK — intrigue '{intr['id']}' added "
          f"(layer {intr['layer']}, parent: {intr.get('parent_intrigue') or 'root'})")
    return 0


# ── Arc coupling commands ───────────────────────────────────────────────────

def cmd_arc_check(args) -> int:
    """Check if an arc beat can advance based on intrigue gates.

    Reads the arc from state.md → ## Campaign Arc, finds the beat,
    checks its intrigue_gate conditions against intrigues.json.
    """
    campaign_dir = find_campaign(args.campaign)
    state_path = campaign_dir / "state.md"
    if not state_path.exists():
        print(f"# state.md not found for campaign '{args.campaign}'", file=sys.stderr)
        return 1

    state_text = state_path.read_text(errors="replace")

    # Find the beat in the arc YAML
    import re
    # Look for the beat by id in the arc block
    beat_pattern = rf'- id: "{args.beat}"\n(.*?)(?=- id:|\n {2,}[a-z]|\n {0,1}[a-z]|\Z)'
    match = re.search(beat_pattern, state_text, re.DOTALL)
    if not match:
        print(f"# beat '{args.beat}' not found in state.md arc", file=sys.stderr)
        return 1

    beat_block = match.group(1)

    # Extract intrigue_gate if present
    gate_pattern = r'intrigue_gate:\s*\{([^}]+)\}'
    gate_match = re.search(gate_pattern, beat_block)

    if not gate_match:
        print(f"# beat '{args.beat}' has no intrigue_gate — can advance")
        return 0

    gate_str = gate_match.group(1)
    # Parse: intrigue: "i001", min_clues: 3
    intr_id = None
    min_clues = 0
    id_match = re.search(r'intrigue:\s*"?([^",}]+)"?', gate_str)
    if id_match:
        intr_id = id_match.group(1).strip()
    clues_match = re.search(r'min_clues:\s*(\d+)', gate_str)
    if clues_match:
        min_clues = int(clues_match.group(1))

    if not intr_id:
        print(f"# beat '{args.beat}' intrigue_gate malformed — can advance (no gate)")
        return 0

    intrigues = _load_intrigues(args.campaign)
    intr = _find_intrigue(intrigues, intr_id)
    if not intr:
        print(f"# gate intrigue '{intr_id}' not found — CANNOT advance", file=sys.stderr)
        return 1

    # Count clues weighted by confidence — only confirmed (1.0) and
    # suspected (0.5) count toward arc gates. Rumors and below don't.
    actual_clues = _count_confirmed_clues(intr, min_weight=0.5)
    can_advance = actual_clues >= min_clues

    print(f"# arc beat '{args.beat}' gate check:")
    print(f"   intrigue: {intr_id} ({intr.get('title', '?')})")
    print(f"   confirmed clues: {actual_clues:.1f} / {min_clues} required")
    print(f"   (confirmed=1.0, suspected=0.5; rumors and below don't count)")
    print(f"   intrigue status: {intr.get('status', '?')}")
    if can_advance:
        print(f"   ✓ CAN ADVANCE")
        return 0
    else:
        print(f"   ✗ CANNOT ADVANCE — need {min_clues - actual_clues:.1f} more confirmed clue(s)")
        if intr.get("unrevealed_clues"):
            print(f"   available unrevealed clues:")
            for c in intr["unrevealed_clues"][:3]:
                print(f"     - {c}")
        return 1


def cmd_arc_advance(args) -> int:
    """Advance an arc beat: check gates, fire force-reveals.

    This is a two-phase operation:
      1. Check intrigue_gate — refuse if not met (unless --force)
      2. If beat has force_reveal_on_beat, reveal that clue
    """
    # Phase 1: check gate
    if not args.force:
        check_args = argparse.Namespace(campaign=args.campaign, beat=args.beat)
        result = cmd_arc_check(check_args)
        if result != 0:
            print(f"\n# use --force to override the gate check", file=sys.stderr)
            return 1

    # Phase 2: fire force-reveal
    campaign_dir = find_campaign(args.campaign)
    state_path = campaign_dir / "state.md"
    state_text = state_path.read_text(errors="replace")

    import re
    beat_pattern = rf'- id: "{args.beat}"\n(.*?)(?=- id:|\n {2,}[a-z]|\n {0,1}[a-z]|\Z)'
    match = re.search(beat_pattern, state_text, re.DOTALL)
    if not match:
        return 1

    beat_block = match.group(1)
    force_pattern = r'force_reveal_on_beat:\s*\{([^}]+)\}'
    force_match = re.search(force_pattern, beat_block)

    if force_match:
        force_str = force_match.group(1)
        intr_id = None
        clue = None
        id_match = re.search(r'intrigue:\s*"?([^",}]+)"?', force_str)
        if id_match:
            intr_id = id_match.group(1).strip()
        clue_match = re.search(r'clue:\s*"([^"]+)"', force_str)
        if clue_match:
            clue = clue_match.group(1)

        if intr_id and clue:
            reveal_args = argparse.Namespace(
                campaign=args.campaign,
                id=intr_id,
                clue=clue,
                session=args.session or 0,
                scene=f"arc:{args.beat}",
                remove_from_unrevealed=True,
            )
            print(f"# firing force_reveal_on_beat for beat '{args.beat}'")
            cmd_intrigue_reveal(reveal_args)
        else:
            print(f"# force_reveal_on_beat malformed in beat '{args.beat}'", file=sys.stderr)
    else:
        print(f"# beat '{args.beat}' has no force_reveal_on_beat — nothing to fire")

    print(f"\n# beat '{args.beat}' arc-advance complete")
    print(f"# remember to mark the beat complete in state.md → outstanding_beats")
    return 0


def cmd_arc_force_reveal(args) -> int:
    """Force-reveal a clue for an intrigue (GM override when players dawdle)."""
    data = _load_intrigues(args.campaign)
    intr = _find_intrigue(data, args.id)
    if not intr:
        print(f"# intrigue '{args.id}' not found", file=sys.stderr)
        return 1

    reveal_args = argparse.Namespace(
        campaign=args.campaign,
        id=args.id,
        clue=args.clue,
        session=args.session or 0,
        scene=f"gm-override",
        remove_from_unrevealed=True,
    )
    return cmd_intrigue_reveal(reveal_args)


# ── Emergence: promote-foreshadow ───────────────────────────────────────────

def cmd_promote_foreshadow(args) -> int:
    """Promote a foreshadowed element from scene_index.json into a new
    child intrigue in intrigues.json.

    This is the emergence mechanism — when players pull on a foreshadowed
    thread, the GM can promote it from a one-line note into a full
    intrigue with its own clues, reveal threshold, and plan tracking.

    The new intrigue is linked as a child of --parent, inheriting the
    campaign's layering (parent layer A → child layer B, etc.).
    """
    # 1. Load the foreshadowed element from scene_index.json
    scene_index_path = find_campaign(args.campaign) / "scene_index.json"
    if not scene_index_path.exists():
        print(f"# scene_index.json not found for '{args.campaign}'", file=sys.stderr)
        return 1

    scene_data = json.loads(scene_index_path.read_text(encoding="utf-8"))
    scene = None
    for s in scene_data.get("scenes", []):
        if s["id"] == args.scene:
            scene = s
            break
    if not scene:
        print(f"# scene '{args.scene}' not found in scene_index.json", file=sys.stderr)
        return 1

    foreshadowed = scene.get("foreshadowed", [])
    if args.index >= len(foreshadowed):
        print(f"# index {args.index} out of range (scene has {len(foreshadowed)} foreshadowed)",
              file=sys.stderr)
        return 1

    clue_text = foreshadowed[args.index]
    print(f"# promoting foreshadowed element:")
    print(f"   scene: {args.scene}")
    print(f"   index: {args.index}")
    print(f"   text:  {clue_text}")
    print()

    # 2. Determine the new intrigue's layer from parent
    intrigues = _load_intrigues(args.campaign)
    parent = None
    parent_layer = "A"
    if args.parent:
        parent = _find_intrigue(intrigues, args.parent)
        if not parent:
            print(f"# parent intrigue '{args.parent}' not found", file=sys.stderr)
            return 1
        parent_layer = parent.get("layer", "A")

    # Child layer = next letter after parent
    child_layer = chr(ord(parent_layer) + 1) if parent_layer else args.layer or "B"
    if args.layer:
        child_layer = args.layer

    # 3. Generate the new intrigue ID
    existing_ids = {i["id"] for i in intrigues["intrigues"]}
    if args.id:
        new_id = args.id
    else:
        # Auto-generate: i001, i002, ...
        n = 1
        while f"i{n:03d}" in existing_ids:
            n += 1
        new_id = f"i{n:03d}"

    # 4. Create the intrigue
    new_intrigue = {
        "id": new_id,
        "title": args.title,
        "type": args.type or "mystery",
        "layer": child_layer,
        "parent_intrigue": args.parent,
        "unlocks": [],
        "unlock_condition": args.unlock_condition or f"intrigue:{args.parent}:clues>=2" if args.parent else None,
        "reveal_condition": args.reveal_condition,
        "status": "active" if not args.parent else "active",  # promoting = activating
        "introduced_session": scene.get("session", 0),
        "deadline": args.deadline,
        "central_question": args.central_question or args.title,
        "key_actors": args.key_actors.split(",") if args.key_actors else [],
        "red_herrings": [],
        "revealed_clues": [{
            "session": scene.get("session", 0),
            "clue": clue_text,
            "scene": args.scene,
        }],
        "unrevealed_clues": [],  # GM fills in later
        "reveal_threshold": args.reveal_threshold or 2,
        "answer": "",  # GM fills in
        "resolution_condition": "",
        "impact_on_resolution": args.impact or "",
        "promoted_from": {
            "scene": args.scene,
            "foreshadow_index": args.index,
            "original_text": clue_text,
            "promoted_session": args.session or scene.get("session", 0),
        },
    }

    # 5. Add to intrigues.json
    intrigues["intrigues"].append(new_intrigue)

    # Update parent's unlocks list
    if parent:
        if new_id not in parent.get("unlocks", []):
            parent.setdefault("unlocks", []).append(new_id)

    _save_intrigues(args.campaign, intrigues)

    # 6. Remove the foreshadowed element from scene_index.json (it's now a real intrigue)
    scene["foreshadowed"].pop(args.index)
    scene.setdefault("promoted_to_intrigues", []).append(new_id)
    scene_index_path.write_text(json.dumps(scene_data, indent=2, ensure_ascii=False),
                                encoding="utf-8")

    print(f"# OK — promoted to intrigue {new_id}")
    print(f"   layer: {child_layer}")
    print(f"   parent: {args.parent or '(root)'}")
    print(f"   title: {args.title}")
    print(f"   reveal threshold: {new_intrigue['reveal_threshold']}")
    print()
    print(f"# next steps:")
    print(f"#   1. Fill in 'unrevealed_clues' and 'answer' in intrigues.json")
    print(f"#   2. Optionally add a plan for the key antagonist via plans.py plan-add")
    print(f"#   3. The foreshadowed element has been removed from scene_index.json")
    return 0


# ── Faction summary ─────────────────────────────────────────────────────────

def cmd_factions(args) -> int:
    """Show a summary of all factions and their current state."""
    data = _load_plans(args.campaign)
    intrigues = _load_intrigues(args.campaign)

    factions = data.get("factions", {})
    if not factions:
        print(f"# no factions registered in plans.json")
        print(f"# factions are auto-derived from NPC plans — add NPCs with --faction to populate")
        return 0

    print(f"# {len(factions)} faction(s)\n")
    for fid, fac in sorted(factions.items()):
        print(f"## {fac.get('name', fid)}")
        print(f"   goal: {fac.get('aggregate_goal', '?')}")
        print(f"   stance toward party: {fac.get('stance_toward_party', '?')}")
        print(f"   intensity: {fac.get('intensity', '?')}")
        members = [nid for nid, npc in data["npcs"].items()
                   if npc.get("faction") == fid.replace("faction:", "")]
        if members:
            print(f"   members ({len(members)}):")
            for m in members:
                npc = data["npcs"][m]
                plan = npc.get("current_plan", {})
                step = plan.get("current_step", "—")
                # Read disposition from entity_state.json (canonical source)
                disposition = _get_disposition(args.campaign, m)
                print(f"     - {npc.get('name', m)}: step {step} "
                      f"(disposition: {disposition})")
        print()
    return 0


# ── Heat / pressure tracking ────────────────────────────────────────────────

DEFAULT_HEAT_TRIGGERS = {
    "failed_stealth":      {"heat": 15, "stability": -10, "attention": 10},
    "witness":             {"heat": 10, "stability": -5,  "attention": 5},
    "media_coverage":      {"heat": 20, "stability": -15, "attention": 15},
    "police_attention":    {"heat": 25, "stability": -20, "attention": 10},
    "camarilla_attention": {"heat": 20, "stability": -10, "attention": 5},
    "sabbat_attention":    {"heat": 20, "stability": -15, "attention": 5},
    "masquerade_breach":   {"heat": 30, "stability": -25, "attention": 20},
    "violence":            {"heat": 10, "stability": -10, "attention": 10},
    "diplomacy_success":   {"heat": -5, "stability": 10,  "attention": 0},
    "time_passes":         {"heat": -2, "stability": 2,   "attention": -5},
}


def cmd_intrigue_heat(args) -> int:
    """Set or adjust heat/stability/attention for an intrigue."""
    data = _load_intrigues(args.campaign)
    intr = _find_intrigue(data, args.id)
    if not intr:
        print(f"# intrigue '{args.id}' not found", file=sys.stderr)
        return 1

    # Initialize heat fields if not present
    intr.setdefault("heat", 0)
    intr.setdefault("stability", 50)
    intr.setdefault("player_attention", 0)

    if args.heat is not None:
        intr["heat"] = max(0, min(100, args.heat))
    if args.stability is not None:
        intr["stability"] = max(0, min(100, args.stability))
    if args.attention is not None:
        intr["player_attention"] = max(0, min(100, args.attention))

    _save_intrigues(args.campaign, data)
    print(f"OK — heat updated for {args.id}")
    print(f"  heat:              {intr['heat']}")
    print(f"  stability:         {intr['stability']}")
    print(f"  player_attention:  {intr['player_attention']}")
    return 0


def cmd_intrigue_heat_show(args) -> int:
    """Show current heat/stability/attention for an intrigue."""
    data = _load_intrigues(args.campaign)
    intr = _find_intrigue(data, args.id)
    if not intr:
        print(f"# intrigue '{args.id}' not found", file=sys.stderr)
        return 1

    heat = intr.get("heat", 0)
    stability = intr.get("stability", 50)
    attention = intr.get("player_attention", 0)

    # Interpret the values
    heat_label = "low" if heat < 30 else "moderate" if heat < 60 else "high" if heat < 80 else "critical"
    stab_label = "stable" if stability > 70 else "strained" if stability > 40 else "volatile" if stability > 20 else "collapsing"
    att_label = "ignored" if attention < 20 else "noticed" if attention < 50 else "investigating" if attention < 80 else "focused"

    print(f"# heat report for {args.id} — {intr.get('title', '?')}")
    print(f"  heat:              {heat:>3} ({heat_label})")
    print(f"  stability:         {stability:>3} ({stab_label})")
    print(f"  player_attention:  {attention:>3} ({att_label})")

    # Show registered heat triggers
    triggers = intr.get("heat_triggers", [])
    if triggers:
        print(f"\n  registered heat triggers:")
        for t in triggers:
            print(f"    - {t.get('event', '?')}: heat {t.get('heat_delta', 0):+d}, "
                  f"stability {t.get('stability_delta', 0):+d}")

    # Check for high-heat warnings
    if heat >= 80:
        print(f"\n  ⚠ CRITICAL HEAT — factions are actively responding")
    if stability <= 20:
        print(f"\n  ⚠ STABILITY COLLAPSING — situation may spiral out of control")
    return 0


def cmd_intrigue_heat_trigger(args) -> int:
    """Fire a heat trigger event on an intrigue.

    Uses the intrigue's registered heat_triggers if the event matches,
    otherwise falls back to DEFAULT_HEAT_TRIGGERS.
    """
    data = _load_intrigues(args.campaign)
    intr = _find_intrigue(data, args.id)
    if not intr:
        print(f"# intrigue '{args.id}' not found", file=sys.stderr)
        return 1

    intr.setdefault("heat", 0)
    intr.setdefault("stability", 50)
    intr.setdefault("player_attention", 0)

    # Find the trigger config
    trigger_config = None
    for t in intr.get("heat_triggers", []):
        if t.get("event", "").lower() == args.event.lower():
            trigger_config = t
            break
    if not trigger_config:
        trigger_config = DEFAULT_HEAT_TRIGGERS.get(args.event.lower())

    if not trigger_config:
        print(f"# unknown heat trigger '{args.event}'", file=sys.stderr)
        print(f"# available defaults: {sorted(DEFAULT_HEAT_TRIGGERS.keys())}")
        return 1

    old_heat = intr["heat"]
    old_stab = intr["stability"]
    old_att = intr["player_attention"]

    intr["heat"] = max(0, min(100, old_heat + trigger_config.get("heat", 0)))
    intr["stability"] = max(0, min(100, old_stab + trigger_config.get("stability", 0)))
    intr["player_attention"] = max(0, min(100, old_att + trigger_config.get("attention", 0)))

    _save_intrigues(args.campaign, data)
    print(f"OK — heat trigger '{args.event}' fired on {args.id}")
    print(f"  heat:              {old_heat} → {intr['heat']} ({trigger_config.get('heat', 0):+d})")
    print(f"  stability:         {old_stab} → {intr['stability']} ({trigger_config.get('stability', 0):+d})")
    print(f"  player_attention:  {old_att} → {intr['player_attention']} ({trigger_config.get('attention', 0):+d})")

    # Check for NPC plan heat-based adaptations
    plans_data = _load_plans(args.campaign)
    heat_adaptations = []
    for npc_id, npc in plans_data.get("npcs", {}).items():
        plan = npc.get("current_plan", {})
        for trigger in plan.get("adaptation_triggers", []):
            if "heat" in trigger.lower() and intr["heat"] >= 70:
                heat_adaptations.append((npc.get("name", npc_id), trigger))
    if heat_adaptations:
        print(f"\n# heat-based plan adaptations to review:")
        for name, trigger in heat_adaptations:
            print(f"  - {name}: {trigger}")
    return 0


# ── Competing intrigues ─────────────────────────────────────────────────────

def cmd_intrigue_compete(args) -> int:
    """Register competing intrigue relationships.

    When this intrigue resolves:
      --weakens <other_id>    → other loses stability, gains heat
      --strengthens <other_id> → other gains stability, gains player attention
    """
    data = _load_intrigues(args.campaign)
    intr = _find_intrigue(data, args.id)
    if not intr:
        print(f"# intrigue '{args.id}' not found", file=sys.stderr)
        return 1

    intr.setdefault("competes_with", [])
    intr.setdefault("weakens_on_resolve", [])
    intr.setdefault("strengthens_on_resolve", [])

    changes = []
    if args.weakens:
        for w in args.weakens.split(","):
            w = w.strip()
            if w and w not in intr["weakens_on_resolve"]:
                intr["weakens_on_resolve"].append(w)
                changes.append(f"weakens: {w}")
            if w not in intr["competes_with"]:
                intr["competes_with"].append(w)
    if args.strengthens:
        for s in args.strengthens.split(","):
            s = s.strip()
            if s and s not in intr["strengthens_on_resolve"]:
                intr["strengthens_on_resolve"].append(s)
                changes.append(f"strengthens: {s}")
            if s not in intr["competes_with"]:
                intr["competes_with"].append(s)

    _save_intrigues(args.campaign, data)
    print(f"OK — competing relationships updated for {args.id}")
    for c in changes:
        print(f"  {c}")
    if intr.get("weakens_on_resolve") or intr.get("strengthens_on_resolve"):
        print(f"\n  full compete config:")
        print(f"    weakens_on_resolve:    {intr.get('weakens_on_resolve', [])}")
        print(f"    strengthens_on_resolve: {intr.get('strengthens_on_resolve', [])}")
    return 0


# ── Deadlines / time decay ─────────────────────────────────────────────────

def cmd_intrigue_deadline(args) -> int:
    """Set a deadline and expiry outcome for an intrigue."""
    data = _load_intrigues(args.campaign)
    intr = _find_intrigue(data, args.id)
    if not intr:
        print(f"# intrigue '{args.id}' not found", file=sys.stderr)
        return 1

    intr["deadline"] = args.deadline
    if args.outcome:
        intr["expiry_outcome"] = {
            "description": args.outcome,
            "world_changes": args.world_changes.split("|") if args.world_changes else [],
            "status_on_expiry": args.status_on_expiry or "expired",
        }

    _save_intrigues(args.campaign, data)
    print(f"OK — deadline set for {args.id}")
    print(f"  deadline: {intr['deadline']}")
    if intr.get("expiry_outcome"):
        print(f"  expiry outcome: {intr['expiry_outcome']['description']}")
        print(f"  status on expiry: {intr['expiry_outcome']['status_on_expiry']}")
        if intr['expiry_outcome'].get("world_changes"):
            print(f"  world changes:")
            for wc in intr['expiry_outcome']['world_changes']:
                print(f"    - {wc}")
    return 0


def cmd_intrigue_check_deadlines(args) -> int:
    """Check all active intrigues for expired deadlines and apply expiry outcomes."""
    data = _load_intrigues(args.campaign)
    expired = []

    for intr in data["intrigues"]:
        if intr.get("status") not in ("active",):
            continue
        deadline = intr.get("deadline", "")
        if not deadline:
            continue

        deadline_session = _parse_eta(deadline)
        if deadline_session is None:
            continue

        if deadline_session <= args.to_session:
            # Deadline has passed
            expiry = intr.get("expiry_outcome", {})
            old_status = intr["status"]
            intr["status"] = expiry.get("status_on_expiry", "expired")
            intr["expired_session"] = args.to_session

            expired.append({
                "id": intr["id"],
                "title": intr.get("title", "?"),
                "deadline": deadline,
                "old_status": old_status,
                "new_status": intr["status"],
                "outcome": expiry.get("description", "(no outcome specified)"),
                "world_changes": expiry.get("world_changes", []),
            })

    if expired:
        _save_intrigues(args.campaign, data)
        print(f"# {len(expired)} intrigue(s) expired by session {args.to_session}\n")
        for e in expired:
            print(f"## {e['id']} — {e['title']}")
            print(f"  deadline: {e['deadline']}")
            print(f"  status: {e['old_status']} → {e['new_status']}")
            print(f"  outcome: {e['outcome']}")
            if e["world_changes"]:
                print(f"  world changes:")
                for wc in e["world_changes"]:
                    print(f"    - {wc}")
            print()

        print(f"## Diff for state.md → Faction Moves / World State:")
        for e in expired:
            print(f"  - {e['title']}: deadline expired — {e['outcome']}")
            for wc in e["world_changes"]:
                print(f"    → {wc}")
    else:
        print(f"# no expired deadlines by session {args.to_session}")
    return 0


# ── Richer dependencies ────────────────────────────────────────────────────

def cmd_intrigue_dependency(args) -> int:
    """Register richer dependency relationships between intrigues.

    Dependency types:
      --requires <ids>      : this intrigue can't resolve until targets are resolved
      --blocks <ids>        : this intrigue prevents targets from resolving
      --reveals <ids>       : revealing a clue here unlocks target intrigues
      --invalidates <ids>   : resolving this abandons the targets
      --accelerates <ids>   : resolving this pulls target deadlines forward
    """
    data = _load_intrigues(args.campaign)
    intr = _find_intrigue(data, args.id)
    if not intr:
        print(f"# intrigue '{args.id}' not found", file=sys.stderr)
        return 1

    changes = []
    for dep_type in ("requires", "blocks", "reveals", "invalidates", "accelerates"):
        val = getattr(args, dep_type, None)
        if val:
            ids = [i.strip() for i in val.split(",") if i.strip()]
            intr.setdefault(dep_type, [])
            for target_id in ids:
                if target_id not in intr[dep_type]:
                    intr[dep_type].append(target_id)
                    changes.append(f"{dep_type}: {target_id}")

            # Validate target exists
            for target_id in ids:
                if not _find_intrigue(data, target_id):
                    print(f"  warning: target intrigue '{target_id}' not found", file=sys.stderr)

    _save_intrigues(args.campaign, data)
    print(f"OK — dependencies updated for {args.id}")
    for c in changes:
        print(f"  {c}")
    return 0


# ── CLI ─────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--campaign", required=True, help="Campaign name")
    sub = p.add_subparsers(dest="cmd", required=True)

    # Plan commands
    s = sub.add_parser("plan-show", help="Show an NPC's current plan")
    s.add_argument("--npc", required=True)
    s.set_defaults(func=cmd_plan_show)

    s = sub.add_parser("plan-add", help="Add or replace an NPC's plan")
    s.add_argument("--npc", required=True)
    s.add_argument("json", help="Plan JSON")
    s.add_argument("--layer", help="Intrigue layer (A/B/C/D) — sets plan depth")
    s.set_defaults(func=cmd_plan_add)

    s = sub.add_parser("plan-advance", help="Advance all NPC plans to target session")
    s.add_argument("--to-session", type=int, required=True)
    s.set_defaults(func=cmd_plan_advance)

    s = sub.add_parser("plan-adapt", help="Apply an adaptation trigger")
    s.add_argument("--npc", required=True)
    s.add_argument("--trigger", required=True, help="Trigger text (matched against adaptation_triggers)")
    s.add_argument("--session", type=int, default=0)
    s.add_argument("--apply", action="store_true", help="Mark trigger as fired")
    s.set_defaults(func=cmd_plan_adapt)

    s = sub.add_parser("plan-complete", help="Mark an NPC's plan as complete")
    s.add_argument("--npc", required=True)
    s.add_argument("--outcome", required=True, choices=["success", "failure", "abandoned"])
    s.add_argument("--summary", default="")
    s.add_argument("--session", type=int, default=0)
    s.set_defaults(func=cmd_plan_complete)

    s = sub.add_parser("plan-history", help="Show an NPC's plan history")
    s.add_argument("--npc", required=True)
    s.set_defaults(func=cmd_plan_history)

    # Intrigue commands
    s = sub.add_parser("intrigue-show", help="Show full detail for an intrigue")
    s.add_argument("--id", required=True)
    s.set_defaults(func=cmd_intrigue_show)

    s = sub.add_parser("intrigue-list", help="List intrigues")
    s.add_argument("--layer", help="Filter by layer (A/B/C/D)")
    s.add_argument("--status", help="Filter by status")
    s.add_argument("--root", action="store_true", help="Only root intrigues")
    s.add_argument("--tree", action="store_true", help="Show hierarchy tree")
    s.set_defaults(func=cmd_intrigue_list)

    s = sub.add_parser("intrigue-add", help="Add a new intrigue")
    s.add_argument("json", help="Intrigue JSON")
    s.add_argument("--layer", default="A")
    s.add_argument("--session", type=int, default=0)
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_intrigue_add)

    s = sub.add_parser("intrigue-reveal", help="Reveal a clue")
    s.add_argument("--id", required=True)
    s.add_argument("--clue", required=True)
    s.add_argument("--session", type=int, default=0)
    s.add_argument("--scene", help="Scene ID where revealed")
    s.add_argument("--confidence", default="confirmed",
                   choices=list(CONFIDENCE_LEVELS.keys()),
                   help="Epistemic status: confirmed|suspected|rumor|false|fabricated")
    s.add_argument("--remove-from-unrevealed", action="store_true",
                   help="Remove from unrevealed_clues if present")
    s.set_defaults(func=cmd_intrigue_reveal)

    s = sub.add_parser("intrigue-resolve", help="Mark an intrigue as resolved")
    s.add_argument("--id", required=True)
    s.add_argument("--resolution", required=True)
    s.add_argument("--session", type=int, default=0)
    s.set_defaults(func=cmd_intrigue_resolve)

    s = sub.add_parser("intrigue-unlock", help="Manually unlock an intrigue")
    s.add_argument("--id", required=True)
    s.add_argument("--session", type=int, default=0)
    s.set_defaults(func=cmd_intrigue_unlock)

    # Arc coupling
    s = sub.add_parser("arc-check", help="Check if an arc beat can advance")
    s.add_argument("--beat", required=True, help="Beat ID (e.g. 2a)")
    s.set_defaults(func=cmd_arc_check)

    s = sub.add_parser("arc-advance", help="Advance an arc beat (check gates + fire reveals)")
    s.add_argument("--beat", required=True)
    s.add_argument("--session", type=int, default=0)
    s.add_argument("--force", action="store_true", help="Skip gate check")
    s.set_defaults(func=cmd_arc_advance)

    s = sub.add_parser("arc-force-reveal", help="Force-reveal a clue (GM override)")
    s.add_argument("--id", required=True, help="Intrigue ID")
    s.add_argument("--clue", required=True)
    s.add_argument("--session", type=int, default=0)
    s.set_defaults(func=cmd_arc_force_reveal)

    # Emergence
    s = sub.add_parser("promote-foreshadow", help="Promote a foreshadowed element to a child intrigue")
    s.add_argument("--scene", required=True, help="Scene ID in scene_index.json")
    s.add_argument("--index", type=int, required=True, help="Index into foreshadowed[] array")
    s.add_argument("--parent", help="Parent intrigue ID")
    s.add_argument("--title", required=True)
    s.add_argument("--type", default="mystery")
    s.add_argument("--layer", help="Override child layer (default: parent+1)")
    s.add_argument("--id", help="Override intrigue ID (default: auto i00N)")
    s.add_argument("--central-question")
    s.add_argument("--key-actors", help="Comma-separated NPC IDs")
    s.add_argument("--deadline")
    s.add_argument("--reveal-threshold", type=int, default=2)
    s.add_argument("--unlock-condition")
    s.add_argument("--reveal-condition")
    s.add_argument("--impact")
    s.add_argument("--session", type=int, default=0)
    s.set_defaults(func=cmd_promote_foreshadow)

    # Faction summary
    s = sub.add_parser("factions", help="Show faction summary")
    s.set_defaults(func=cmd_factions)

    # Heat / pressure tracking
    s = sub.add_parser("intrigue-heat", help="Set or adjust heat/stability/attention")
    s.add_argument("--id", required=True)
    s.add_argument("--heat", type=int, help="Heat 0-100 (how much pressure/attention)")
    s.add_argument("--stability", type=int, help="Stability 0-100 (how volatile)")
    s.add_argument("--attention", type=int, help="Player attention 0-100")
    s.set_defaults(func=cmd_intrigue_heat)

    s = sub.add_parser("intrigue-heat-show", help="Show heat report for an intrigue")
    s.add_argument("--id", required=True)
    s.set_defaults(func=cmd_intrigue_heat_show)

    s = sub.add_parser("intrigue-heat-trigger", help="Fire a heat trigger event")
    s.add_argument("--id", required=True)
    s.add_argument("--event", required=True,
                   help=f"Event name. Defaults: {sorted(DEFAULT_HEAT_TRIGGERS.keys())}")
    s.set_defaults(func=cmd_intrigue_heat_trigger)

    # Competing intrigues
    s = sub.add_parser("intrigue-compete", help="Register competing intrigue relationships")
    s.add_argument("--id", required=True)
    s.add_argument("--weakens", help="Comma-separated intrigue IDs this one weakens on resolve")
    s.add_argument("--strengthens", help="Comma-separated intrigue IDs this one strengthens on resolve")
    s.set_defaults(func=cmd_intrigue_compete)

    # Deadlines / time decay
    s = sub.add_parser("intrigue-deadline", help="Set deadline and expiry outcome")
    s.add_argument("--id", required=True)
    s.add_argument("--deadline", required=True, help="Deadline (e.g. session:20)")
    s.add_argument("--outcome", help="Description of what happens on expiry")
    s.add_argument("--world-changes", help="Pipe-separated world changes on expiry")
    s.add_argument("--status-on-expiry", default="expired",
                   help="Status to set on expiry (default: expired)")
    s.set_defaults(func=cmd_intrigue_deadline)

    s = sub.add_parser("intrigue-check-deadlines", help="Check and apply expired deadlines")
    s.add_argument("--to-session", type=int, required=True, help="Check deadlines up to this session")
    s.set_defaults(func=cmd_intrigue_check_deadlines)

    # Richer dependencies
    s = sub.add_parser("intrigue-dependency", help="Register richer dependency relationships")
    s.add_argument("--id", required=True)
    s.add_argument("--requires", help="Comma-separated IDs this intrigue requires")
    s.add_argument("--blocks", help="Comma-separated IDs this intrigue blocks")
    s.add_argument("--reveals", help="Comma-separated IDs this intrigue reveals")
    s.add_argument("--invalidates", help="Comma-separated IDs this intrigue invalidates")
    s.add_argument("--accelerates", help="Comma-separated IDs this intrigue accelerates")
    s.set_defaults(func=cmd_intrigue_dependency)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
