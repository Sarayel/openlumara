#!/usr/bin/env python3
"""
micro_director.py — scene-level pacing valve that runs during the LLM loop.

The macro Director (director.py) picks beats between scenes. The Micro-Director
runs DURING scenes, tracking scene tension and injecting interrupts when the
players are dawdling while the world is burning.

Key design decisions (from review feedback):
  - Interrupt cooldown prevents spam (minimum 3 turns between interrupts)
  - Ambient beats are gated against intrigue layers (won't leak Layer C secrets)
  - Scene tension is a 0-100 valve that rises with action, drops with dialogue

Storage: <campaign-dir>/micro_director_state.json (per-session, reset each scene)

Usage:
  python3 micro_director.py --campaign <name> init --session 15
  python3 micro_director.py --campaign <name> tick --action-type dialogue --session 15
  python3 micro_director.py --campaign <name> check --session 15
  python3 micro_director.py --campaign <name> reset
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from paths import find_campaign


# ── Config ──────────────────────────────────────────────────────────────────

TENSION_START = 30          # starting tension for a new scene
TENSION_DIALOGUE_DECAY = 3  # tension drops per dialogue turn
TENSION_COMBAT_BOOST = 15   # tension rises per combat action
TENSION_INVESTIGATION_BOOST = 5  # slight rise for investigation
TENSION_SOCIAL_BOOST = 2    # minimal rise for social
INTERRUPT_THRESHOLD = 20    # tension below this + high campaign pressure = interrupt
INTERRUPT_COOLDOWN = 3      # minimum turns between interrupts
AMBIENT_COOLDOWN = 5        # minimum turns between ambient beats


def _path(campaign: str) -> Path:
    return find_campaign(campaign) / "micro_director_state.json"


def _load(campaign: str) -> dict:
    p = _path(campaign)
    if not p.exists():
        return _default_state()
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _default_state()


def _default_state() -> dict:
    return {
        "version": 1,
        "scene_tension": TENSION_START,
        "turn_count": 0,
        "turns_since_interrupt": 99,
        "turns_since_ambient": 99,
        "last_interrupt_type": None,
        "history": [],
    }


def _save(campaign: str, data: dict) -> None:
    p = _path(campaign)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ── Pressure reader (for interrupt gating) ──────────────────────────────────

def _read_pressure(campaign: str) -> dict:
    p = find_campaign(campaign) / "pressure.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("axes", {})
    except (json.JSONDecodeError, OSError):
        return {}


def _read_economy(campaign: str) -> dict:
    p = find_campaign(campaign) / "economy.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("resources", {})
    except (json.JSONDecodeError, OSError):
        return {}


def _read_intrigues(campaign: str) -> list:
    p = find_campaign(campaign) / "intrigues.json"
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("intrigues", [])
    except (json.JSONDecodeError, OSError):
        return []


def _campaign_pressure_is_high(pressure: dict) -> bool:
    """Check if campaign-level pressure is high enough to justify an interrupt."""
    high_axes = sum(1 for v in pressure.values() if v >= 60)
    return high_axes >= 2


# ── Commands ────────────────────────────────────────────────────────────────

def cmd_init(args) -> int:
    """Initialize/reset the micro-director for a new scene."""
    state = _default_state()
    state["session"] = args.session
    _save(args.campaign, state)
    print(f"OK — micro-director initialized for session {args.session}")
    print(f"  scene_tension: {state['scene_tension']}")
    print(f"  turn_count: 0")
    return 0


def cmd_tick(args) -> int:
    """Process one player action — update tension and check for interrupts.

    action_type: dialogue | combat | investigation | social | exploration | movement
    """
    state = _load(args.campaign)

    # Update tension based on action type
    tension_delta = {
        "dialogue": -TENSION_DIALOGUE_DECAY,
        "combat": TENSION_COMBAT_BOOST,
        "investigation": TENSION_INVESTIGATION_BOOST,
        "social": TENSION_SOCIAL_BOOST,
        "exploration": 0,
        "movement": 1,
    }.get(args.action_type, 0)

    old_tension = state["scene_tension"]
    state["scene_tension"] = max(0, min(100, old_tension + tension_delta))
    state["turn_count"] += 1
    state["turns_since_interrupt"] += 1
    state["turns_since_ambient"] += 1

    # Record in history
    state.setdefault("history", []).append({
        "turn": state["turn_count"],
        "action": args.action_type,
        "tension_before": old_tension,
        "tension_after": state["scene_tension"],
    })

    # Check for interrupt
    interrupt = _check_interrupt(args.campaign, state)

    # Check for ambient beat
    ambient = _check_ambient(args.campaign, state)

    _save(args.campaign, state)

    print(f"turn {state['turn_count']}: {args.action_type}")
    print(f"  tension: {old_tension} → {state['scene_tension']} ({tension_delta:+d})")

    if interrupt:
        print(f"\n⚠ INTERRUPT INJECTION:")
        print(f"  {interrupt['directive']}")
        print(f"  reason: {interrupt['reason']}")
        state["turns_since_interrupt"] = 0
        state["last_interrupt_type"] = interrupt["type"]
        _save(args.campaign, state)

    if ambient:
        print(f"\n📡 AMBIENT BEAT:")
        print(f"  {ambient['directive']}")
        print(f"  reason: {ambient['reason']}")
        state["turns_since_ambient"] = 0
        _save(args.campaign, state)

    if not interrupt and not ambient:
        print(f"  (no interrupt needed)")

    return 0


def _check_interrupt(campaign: str, state: dict) -> Optional[dict]:
    """Check if an interrupt should fire.

    Conditions:
      1. Scene tension is below INTERRUPT_THRESHOLD (20)
      2. Campaign pressure is high (2+ axes at 60+)
      3. Interrupt cooldown has passed (3+ turns since last)
      4. At least 3 turns of dialogue/exploration have occurred
    """
    if state["scene_tension"] > INTERRUPT_THRESHOLD:
        return None
    if state["turns_since_interrupt"] < INTERRUPT_COOLDOWN:
        return None
    if state["turn_count"] < 3:
        return None

    pressure = _read_pressure(campaign)
    if not _campaign_pressure_is_high(pressure):
        return None

    # Determine interrupt type based on what's high
    high_axes = {k: v for k, v in pressure.items() if v >= 60}

    if "violence" in high_axes:
        return {
            "type": "physical",
            "directive": "[SYSTEM DIRECTIVE: A sudden physical interruption occurs — a door bursts open, a shot rings out, or someone lunges. Force the players to react immediately.]",
            "reason": f"violence pressure at {high_axes['violence']} while scene tension is {state['scene_tension']}",
        }
    elif "political" in high_axes:
        return {
            "type": "social",
            "directive": "[SYSTEM DIRECTIVE: A social interruption occurs — an NPC arrives with urgent news, a messenger demands attention, or a confrontation escalates. Force the players to engage.]",
            "reason": f"political pressure at {high_axes['political']} while scene tension is {state['scene_tension']}",
        }
    elif "mystery" in high_axes:
        return {
            "type": "discovery",
            "directive": "[SYSTEM DIRECTIVE: A clue surfaces unexpectedly — a document falls from a pocket, a sound reveals a hidden passage, or an NPC says something they shouldn't. Force the players to investigate.]",
            "reason": f"mystery pressure at {high_axes['mystery']} while scene tension is {state['scene_tension']}",
        }
    else:
        return {
            "type": "generic",
            "directive": "[SYSTEM DIRECTIVE: Something happens that forces the players to act. The world is not waiting for them.]",
            "reason": f"campaign pressure high while scene tension is {state['scene_tension']}",
        }


def _check_ambient(campaign: str, state: dict) -> Optional[dict]:
    """Check if an ambient beat should fire — hinting at off-screen NPC plans.

    Conditions:
      1. Ambient cooldown has passed (5+ turns)
      2. There's a completed NPC plan step that hasn't been surfaced
      3. The hint doesn't leak a hidden intrigue layer

    GATING: Ambient beats check intrigue reveal_condition before hinting.
    A hint about Velkyn's plan is only injected if the intrigue layer
    connected to Velkyn is already active (not hidden).
    """
    if state["turns_since_ambient"] < AMBIENT_COOLDOWN:
        return None
    if state["turn_count"] < 5:
        return None

    # Check for completed plan steps
    plans_path = find_campaign(campaign) / "plans.json"
    if not plans_path.exists():
        return None

    try:
        plans = json.loads(plans_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    intrigues = _read_intrigues(campaign)

    for npc_id, npc in plans.get("npcs", {}).items():
        plan = npc.get("current_plan")
        if not plan:
            continue
        steps = plan.get("steps", [])
        current = plan.get("current_step", 1)
        if current > 1 and current - 2 < len(steps):
            prev = steps[current - 2]
            if prev.get("status") == "complete":
                # Check if this NPC is linked to a hidden intrigue
                # If so, DON'T hint — it would leak the layer
                npc_short = npc_id.replace("npc:", "")
                for intr in intrigues:
                    if (intr.get("status") in ("hidden", "locked") and
                        npc_id in intr.get("key_actors", [])):
                        return None  # GATED — would leak hidden intrigue

                # Safe to hint
                action = prev.get("action", "?")
                return {
                    "directive": f"[AMBIENT BEAT: Describe a subtle environmental detail that hints at {npc.get('name', npc_id)}'s recent action: '{action}'. This should be background — not a direct confrontation, but a sensory cue the players might notice.]",
                    "reason": f"{npc_id} completed '{action}' off-screen; ambient hint is safe (no hidden intrigue leaked)",
                }

    return None


def cmd_check(args) -> int:
    """Check current micro-director state without ticking."""
    state = _load(args.campaign)
    print(f"# micro-director state\n")
    print(f"  session: {state.get('session', '?')}")
    print(f"  scene_tension: {state['scene_tension']}")
    print(f"  turn_count: {state['turn_count']}")
    print(f"  turns_since_interrupt: {state['turns_since_interrupt']}")
    print(f"  turns_since_ambient: {state['turns_since_ambient']}")
    print(f"  last_interrupt_type: {state.get('last_interrupt_type', 'none')}")

    # Show recent history
    history = state.get("history", [])
    if history:
        print(f"\n  recent turns:")
        for h in history[-5:]:
            print(f"    turn {h['turn']}: {h['action']} (tension {h['tension_before']}→{h['tension_after']})")
    return 0


def cmd_reset(args) -> int:
    """Reset the micro-director (new scene)."""
    state = _default_state()
    _save(args.campaign, state)
    print(f"OK — micro-director reset")
    print(f"  scene_tension: {state['scene_tension']}")
    return 0


# ── CLI ─────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--campaign", required=True)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="Initialize for a new scene")
    s.add_argument("--session", type=int, default=0)
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("tick", help="Process one player action")
    s.add_argument("--action-type", required=True,
                   choices=["dialogue", "combat", "investigation", "social", "exploration", "movement"])
    s.add_argument("--session", type=int, default=0)
    s.set_defaults(func=cmd_tick)

    s = sub.add_parser("check", help="Check current state")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("reset", help="Reset for a new scene")
    s.set_defaults(func=cmd_reset)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
