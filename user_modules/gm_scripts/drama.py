#!/usr/bin/env python3
"""
drama.py — Dramatic Pressure + Themes + Dramatic Economy + Beat Templates + Campaign State Machine.

The Drama Engine layer. Tracks multi-axis pressure meters, campaign themes,
abstract dramatic resources, reusable beat templates, and the campaign-level
state machine (Stability → Tension → Crisis → Collapse → Reconstruction).

The Scene Director (director.py) reads this layer to decide what kind of
scene should happen next.

Storage:
  <campaign-dir>/pressure.json
  <campaign-dir>/themes.json
  <campaign-dir>/economy.json
  <campaign-dir>/campaign_state.json
  <skill-base>/data/narrative/beat_templates.yaml  (shipped defaults)

Usage:
  # Pressure
  python3 drama.py pressure-show --campaign <name>
  python3 drama.py pressure-adjust --campaign <name> --axis mystery --delta 15
  python3 drama.py pressure-trigger --campaign <name> --event clue_revealed

  # Themes
  python3 drama.py theme-show --campaign <name>
  python3 drama.py theme-boost --campaign <name> --theme identity --delta 5
  python3 drama.py theme-dominant --campaign <name>

  # Economy
  python3 drama.py economy-show --campaign <name>
  python3 drama.py economy-exchange --campaign <name> --give information:15 --take fear:10 --reason "clue revealed"

  # Beats
  python3 drama.py beat-list --campaign <name>
  python3 drama.py beat-apply --campaign <name> --type reveal --session 15

  # Campaign State Machine
  python3 drama.py state-show --campaign <name>
  python3 drama.py state-advance --campaign <name>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from paths import find_campaign


# ── Default configurations ──────────────────────────────────────────────────

DEFAULT_PRESSURE_AXES = {
    "political": 20,
    "personal": 15,
    "violence": 10,
    "mystery": 20,
    "masquerade": 15,
    "supernatural": 10,
}

DEFAULT_THEMES = {
    "power": 20,
    "humanity": 50,
    "faith": 15,
    "control": 25,
    "family": 20,
    "decay": 30,
    "identity": 25,
}

DEFAULT_ECONOMY = {
    "hope": 50,
    "fear": 30,
    "trust": 35,
    "information": 20,
    "power": 25,
    "influence": 20,
    "chaos": 15,
}

# Beat templates — reusable scene templates with pressure/theme effects
BEAT_TEMPLATES = {
    "reveal": {
        "description": "A hidden truth surfaces",
        "pressure_effects": {"mystery": -15, "information": 10},
        "economy_effects": {"information": 15, "fear": -5, "chaos": 5},
        "theme_boost": "identity",
        "best_when": {"mystery": "high", "information": "low"},
    },
    "reversal": {
        "description": "What seemed true was false",
        "pressure_effects": {"political": 10, "chaos": 15},
        "economy_effects": {"trust": -15, "chaos": 20, "fear": 10},
        "theme_boost": "power",
        "best_when": {"trust": "high", "chaos": "low"},
    },
    "complication": {
        "description": "The problem deepens",
        "pressure_effects": {"mystery": 10, "violence": 5},
        "economy_effects": {"fear": 10, "chaos": 5},
        "theme_boost": "decay",
        "best_when": {"mystery": "medium"},
    },
    "escalation": {
        "description": "Stakes rise",
        "pressure_effects": {"violence": 20, "fear": 15, "political": 10},
        "economy_effects": {"fear": 20, "hope": -10, "power": 10},
        "theme_boost": "power",
        "best_when": {"violence": "low", "fear": "low"},
    },
    "calm": {
        "description": "A moment of peace before the storm",
        "pressure_effects": {"violence": -10, "fear": -5, "personal": 5},
        "economy_effects": {"hope": 15, "fear": -10, "trust": 10},
        "theme_boost": "humanity",
        "best_when": {"violence": "high", "fear": "high"},
    },
    "false_victory": {
        "description": "Success that isn't what it seems",
        "pressure_effects": {"hope": 15, "chaos": 10, "mystery": 5},
        "economy_effects": {"hope": 20, "trust": -10, "chaos": 10},
        "theme_boost": "control",
        "best_when": {"hope": "low", "chaos": "low"},
    },
    "loss": {
        "description": "Something is taken or destroyed",
        "pressure_effects": {"personal": 15, "violence": 10, "political": 10},
        "economy_effects": {"hope": -20, "fear": 15, "power": -10},
        "theme_boost": "decay",
        "best_when": {"hope": "high"},
    },
    "choice": {
        "description": "A decision with lasting consequences",
        "pressure_effects": {"personal": 10, "political": 5},
        "economy_effects": {"chaos": 5, "power": 5},
        "theme_boost": "identity",
        "best_when": {"chaos": "low"},
    },
    "consequence": {
        "description": "A prior choice lands",
        "pressure_effects": {"varies": True},
        "economy_effects": {"varies": True},
        "theme_boost": "varies",
        "best_when": {},
    },
    "twist": {
        "description": "Everything changes",
        "pressure_effects": {"chaos": 20, "mystery": 10, "political": 15},
        "economy_effects": {"chaos": 25, "trust": -10, "fear": 15},
        "theme_boost": "identity",
        "best_when": {"chaos": "low", "mystery": "medium"},
    },
    "resolution": {
        "description": "A question is answered, a thread resolves",
        "pressure_effects": {"mystery": -20, "violence": -10, "personal": -10},
        "economy_effects": {"hope": 15, "fear": -10, "chaos": -15, "trust": 10},
        "theme_boost": "humanity",
        "best_when": {"mystery": "high", "chaos": "high"},
    },
}

# Campaign state machine phases
CAMPAIGN_PHASES = [
    "stability",      # the calm before; factions maneuver quietly
    "tension",        # pressure builds; multiple threads strain
    "crisis",         # something breaks; a point of no return
    "collapse",       # the old order fails; power vacuum
    "reconstruction", # new order emerges from the ashes
]

# Pressure trigger events — pre-configured pressure adjustments
PRESSURE_TRIGGERS = {
    "clue_revealed":        {"mystery": -10, "information": 5, "chaos": 5},
    "violence_erupts":      {"violence": 20, "fear": 10, "masquerade": 5},
    "political_summit":     {"political": 10, "personal": -5},
    "masquerade_breach":    {"masquerade": 25, "political": 10, "fear": 10},
    "supernatural_event":   {"supernatural": 20, "fear": 10, "mystery": 5},
    "personal_loss":        {"personal": 15, "fear": 5, "hope": -10},
    "alliance_formed":      {"political": -10, "trust": 10, "personal": -5},
    "betrayal_revealed":    {"political": 15, "chaos": 10, "trust": -15},
    "time_passes":          {"mystery": -2, "political": 2, "decay": 1},
    "investigation_success": {"mystery": -15, "information": 10},
    "investigation_failure": {"mystery": 5, "fear": 5, "frustration": 10},
}


# ── IO helpers ──────────────────────────────────────────────────────────────

def _pressure_path(campaign: str) -> Path:
    return find_campaign(campaign) / "pressure.json"


def _themes_path(campaign: str) -> Path:
    return find_campaign(campaign) / "themes.json"


def _economy_path(campaign: str) -> Path:
    return find_campaign(campaign) / "economy.json"


def _campaign_state_path(campaign: str) -> Path:
    return find_campaign(campaign) / "campaign_state.json"


def _load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return {"version": 1, **default}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": 1, **default}
    return data


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _load_pressure(campaign: str) -> dict:
    data = _load_json(_pressure_path(campaign), {"axes": dict(DEFAULT_PRESSURE_AXES), "history": []})
    data.setdefault("axes", dict(DEFAULT_PRESSURE_AXES))
    data.setdefault("history", [])
    return data


def _load_themes(campaign: str) -> dict:
    data = _load_json(_themes_path(campaign), {"themes": dict(DEFAULT_THEMES), "dominant": None})
    data.setdefault("themes", dict(DEFAULT_THEMES))
    data.setdefault("dominant", None)
    return data


def _load_economy(campaign: str) -> dict:
    data = _load_json(_economy_path(campaign), {"resources": dict(DEFAULT_ECONOMY), "transactions": []})
    data.setdefault("resources", dict(DEFAULT_ECONOMY))
    data.setdefault("transactions", [])
    return data


def _load_campaign_state(campaign: str) -> dict:
    data = _load_json(_campaign_state_path(campaign), {"phase": "stability", "phase_history": []})
    data.setdefault("phase", "stability")
    data.setdefault("phase_history", [])
    data.setdefault("phase_entered_session", 0)
    return data


def _clamp(val: int) -> int:
    return max(0, min(100, val))


def _level_label(val: int) -> str:
    if val < 20: return "low"
    if val < 40: return "moderate"
    if val < 60: return "medium"
    if val < 80: return "high"
    return "critical"


# ── Pressure commands ───────────────────────────────────────────────────────

def cmd_pressure_show(args) -> int:
    """Show all pressure axes with labels."""
    data = _load_pressure(args.campaign)
    axes = data.get("axes", {})

    print(f"# dramatic pressure report\n")
    print(f"{'Axis':<15} {'Score':>5} {'Level':<12} Bar")
    print("-" * 50)
    for axis in sorted(axes.keys()):
        val = axes[axis]
        label = _level_label(val)
        bar = "█" * (val // 5) + "░" * (20 - val // 5)
        print(f"{axis:<15} {val:>5} {label:<12} {bar}")

    # Identify imbalances
    high = {k: v for k, v in axes.items() if v >= 70}
    low = {k: v for k, v in axes.items() if v <= 15}
    if high:
        print(f"\n# high-pressure axes (need release):")
        for k, v in sorted(high.items(), key=lambda x: x[1], reverse=True):
            print(f"  ⚠ {k}: {v}")
    if low:
        print(f"\n# low-pressure axes (can escalate):")
        for k, v in sorted(low.items()):
            print(f"  · {k}: {v}")
    return 0


def cmd_pressure_adjust(args) -> int:
    """Adjust a single pressure axis."""
    data = _load_pressure(args.campaign)
    axes = data.setdefault("axes", dict(DEFAULT_PRESSURE_AXES))
    old = axes.get(args.axis, 0)
    axes[args.axis] = _clamp(old + args.delta)

    # Record history
    data.setdefault("history", []).append({
        "session": args.session,
        "axis": args.axis,
        "old": old,
        "new": axes[args.axis],
        "delta": args.delta,
        "reason": args.reason or "",
    })

    _save_json(_pressure_path(args.campaign), data)
    print(f"OK — pressure adjusted: {args.axis} {old} → {axes[args.axis]} ({args.delta:+d})")
    print(f"  level: {_level_label(axes[args.axis])}")
    return 0


def cmd_pressure_trigger(args) -> int:
    """Fire a pre-configured pressure trigger event."""
    trigger = PRESSURE_TRIGGERS.get(args.event.lower())
    if not trigger:
        print(f"# unknown trigger '{args.event}'", file=sys.stderr)
        print(f"# available: {sorted(PRESSURE_TRIGGERS.keys())}", file=sys.stderr)
        return 1

    data = _load_pressure(args.campaign)
    axes = data.setdefault("axes", dict(DEFAULT_PRESSURE_AXES))

    print(f"# firing pressure trigger: {args.event}\n")
    for axis, delta in trigger.items():
        if axis not in axes:
            axes[axis] = 50  # default for new axes
        old = axes[axis]
        axes[axis] = _clamp(old + delta)
        print(f"  {axis}: {old} → {axes[axis]} ({delta:+d})")

    data.setdefault("history", []).append({
        "session": args.session,
        "trigger": args.event,
        "effects": trigger,
    })

    _save_json(_pressure_path(args.campaign), data)
    return 0


# ── Theme commands ──────────────────────────────────────────────────────────

def cmd_theme_show(args) -> int:
    """Show all campaign themes with progression."""
    data = _load_themes(args.campaign)
    themes = data.get("themes", {})

    print(f"# campaign themes\n")
    print(f"{'Theme':<15} {'Score':>5} {'Level':<12} Bar")
    print("-" * 50)
    for theme in sorted(themes.keys()):
        val = themes[theme]
        label = _level_label(val)
        bar = "█" * (val // 5) + "░" * (20 - val // 5)
        print(f"{theme:<15} {val:>5} {label:<12} {bar}")

    # Show dominant
    dominant = max(themes, key=themes.get) if themes else None
    if dominant:
        print(f"\n# dominant theme: {dominant} ({themes[dominant]})")
        print(f"  the director should prefer scenes that reinforce {dominant}")
    return 0


def cmd_theme_boost(args) -> int:
    """Increase a theme's score."""
    data = _load_themes(args.campaign)
    themes = data.setdefault("themes", dict(DEFAULT_THEMES))
    old = themes.get(args.theme, 0)
    themes[args.theme] = _clamp(old + args.delta)

    # Update dominant
    data["dominant"] = max(themes, key=themes.get)

    _save_json(_themes_path(args.campaign), data)
    print(f"OK — theme boosted: {args.theme} {old} → {themes[args.theme]} ({args.delta:+d})")
    print(f"  dominant theme: {data['dominant']}")
    return 0


def cmd_theme_dominant(args) -> int:
    """Show just the dominant theme."""
    data = _load_themes(args.campaign)
    themes = data.get("themes", {})
    if not themes:
        print("# no themes defined")
        return 1
    dominant = max(themes, key=themes.get)
    print(f"{dominant} ({themes[dominant]})")
    return 0


# ── Economy commands ────────────────────────────────────────────────────────

def cmd_economy_show(args) -> int:
    """Show all dramatic resources."""
    data = _load_economy(args.campaign)
    resources = data.get("resources", {})

    print(f"# dramatic economy\n")
    print(f"{'Resource':<15} {'Score':>5} {'Level':<12} Bar")
    print("-" * 50)
    for res in sorted(resources.keys()):
        val = resources[res]
        label = _level_label(val)
        bar = "█" * (val // 5) + "░" * (20 - val // 5)
        print(f"{res:<15} {val:>5} {label:<12} {bar}")

    # Identify imbalances
    scarce = {k: v for k, v in resources.items() if v <= 15}
    abundant = {k: v for k, v in resources.items() if v >= 80}
    if scarce:
        print(f"\n# scarce resources (party lacks these):")
        for k, v in sorted(scarce.items()):
            print(f"  ⚠ {k}: {v}")
    if abundant:
        print(f"\n# abundant resources (party has surplus):")
        for k, v in sorted(abundant.items(), key=lambda x: x[1], reverse=True):
            print(f"  ★ {k}: {v}")
    return 0


def cmd_economy_exchange(args) -> int:
    """Exchange dramatic resources — give some, take others."""
    data = _load_economy(args.campaign)
    resources = data.setdefault("resources", dict(DEFAULT_ECONOMY))

    give = {}
    take = {}

    # Parse --give resource:amount
    if args.give:
        for pair in args.give.split(","):
            name, val = pair.split(":")
            give[name.strip()] = int(val.strip())
            resources[name.strip()] = _clamp(resources.get(name.strip(), 50) + int(val.strip()))

    # Parse --take resource:amount
    if args.take:
        for pair in args.take.split(","):
            name, val = pair.split(":")
            take[name.strip()] = int(val.strip())
            resources[name.strip()] = _clamp(resources.get(name.strip(), 50) - int(val.strip()))

    # Record transaction
    data.setdefault("transactions", []).append({
        "session": args.session,
        "give": give,
        "take": take,
        "reason": args.reason or "",
    })

    _save_json(_economy_path(args.campaign), data)
    print(f"OK — economy exchange")
    if give:
        print(f"  gained: {give}")
    if take:
        print(f"  spent:  {take}")
    if args.reason:
        print(f"  reason: {args.reason}")
    return 0


# ── Beat commands ───────────────────────────────────────────────────────────

def cmd_beat_list(args) -> int:
    """List all available beat templates."""
    print(f"# narrative beat templates\n")
    print(f"{'Type':<16} {'Description'}")
    print("-" * 60)
    for beat_type, template in sorted(BEAT_TEMPLATES.items()):
        print(f"{beat_type:<16} {template['description']}")
        if template.get("best_when"):
            conditions = ", ".join(f"{k}={v}" for k, v in template["best_when"].items())
            print(f"{'':>16} best when: {conditions}")
    return 0


def cmd_beat_apply(args) -> int:
    """Apply a beat's pressure/economy/theme effects to the campaign."""
    template = BEAT_TEMPLATES.get(args.beat_type)
    if not template:
        print(f"# unknown beat type '{args.beat_type}'", file=sys.stderr)
        print(f"# available: {sorted(BEAT_TEMPLATES.keys())}", file=sys.stderr)
        return 1

    print(f"# applying beat: {args.beat_type}")
    print(f"  description: {template['description']}\n")

    # Apply pressure effects
    pressure_data = _load_pressure(args.campaign)
    pressure_axes = pressure_data.setdefault("axes", dict(DEFAULT_PRESSURE_AXES))
    for axis, delta in template.get("pressure_effects", {}).items():
        if axis == "varies":
            continue
        old = pressure_axes.get(axis, 50)
        pressure_axes[axis] = _clamp(old + delta)
        print(f"  pressure: {axis} {old} → {pressure_axes[axis]} ({delta:+d})")
    _save_json(_pressure_path(args.campaign), pressure_data)

    # Apply economy effects
    economy_data = _load_economy(args.campaign)
    resources = economy_data.setdefault("resources", dict(DEFAULT_ECONOMY))
    for res, delta in template.get("economy_effects", {}).items():
        if res == "varies":
            continue
        old = resources.get(res, 50)
        resources[res] = _clamp(old + delta)
        print(f"  economy: {res} {old} → {resources[res]} ({delta:+d})")
    _save_json(_economy_path(args.campaign), economy_data)

    # Apply theme boost
    theme_boost = template.get("theme_boost", "")
    if theme_boost and theme_boost != "varies":
        theme_data = _load_themes(args.campaign)
        themes = theme_data.setdefault("themes", dict(DEFAULT_THEMES))
        old = themes.get(theme_boost, 0)
        themes[theme_boost] = _clamp(old + 5)
        theme_data["dominant"] = max(themes, key=themes.get)
        _save_json(_themes_path(args.campaign), theme_data)
        print(f"  theme: {theme_boost} {old} → {themes[theme_boost]} (+5)")

    print(f"\n  session: {args.session}")
    return 0


# ── Campaign State Machine ──────────────────────────────────────────────────

def cmd_state_show(args) -> int:
    """Show the current campaign phase."""
    data = _load_campaign_state(args.campaign)
    phase = data.get("phase", "stability")
    phase_idx = CAMPAIGN_PHASES.index(phase) if phase in CAMPAIGN_PHASES else 0

    print(f"# campaign state machine\n")
    print(f"  current phase: {phase}")
    print(f"  entered session: {data.get('phase_entered_session', 0)}")

    print(f"\n  phase progression:")
    for i, p in enumerate(CAMPAIGN_PHASES):
        marker = " → " if i == phase_idx else "   "
        print(f"{marker}{p}")

    # Show phase history
    history = data.get("phase_history", [])
    if history:
        print(f"\n  phase history:")
        for entry in history:
            print(f"    session {entry.get('session', '?')}: {entry.get('from', '?')} → {entry.get('to', '?')} ({entry.get('reason', '')})")
    return 0


def cmd_state_advance(args) -> int:
    """Advance the campaign to the next phase."""
    data = _load_campaign_state(args.campaign)
    old_phase = data.get("phase", "stability")
    old_idx = CAMPAIGN_PHASES.index(old_phase) if old_phase in CAMPAIGN_PHASES else 0

    if old_idx >= len(CAMPAIGN_PHASES) - 1:
        print(f"# already at final phase ({old_phase})")
        print(f"# the campaign has completed its arc; start a new one or remain in reconstruction")
        return 0

    new_phase = CAMPAIGN_PHASES[old_idx + 1]
    data["phase"] = new_phase
    data["phase_entered_session"] = args.session
    data.setdefault("phase_history", []).append({
        "session": args.session,
        "from": old_phase,
        "to": new_phase,
        "reason": args.reason or "",
    })

    _save_json(_campaign_state_path(args.campaign), data)
    print(f"OK — campaign phase advanced")
    print(f"  {old_phase} → {new_phase}")
    print(f"  session: {args.session}")

    phase_descriptions = {
        "stability": "the calm before; factions maneuver quietly",
        "tension": "pressure builds; multiple threads strain",
        "crisis": "something breaks; a point of no return",
        "collapse": "the old order fails; power vacuum",
        "reconstruction": "new order emerges from the ashes",
    }
    print(f"  meaning: {phase_descriptions.get(new_phase, '?')}")
    return 0


# ── CLI ─────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--campaign", required=True, help="Campaign name")
    sub = p.add_subparsers(dest="cmd", required=True)

    # Pressure
    s = sub.add_parser("pressure-show", help="Show all pressure axes")
    s.set_defaults(func=cmd_pressure_show)

    s = sub.add_parser("pressure-adjust", help="Adjust a pressure axis")
    s.add_argument("--axis", required=True)
    s.add_argument("--delta", type=int, required=True)
    s.add_argument("--session", type=int, default=0)
    s.add_argument("--reason", help="Why this adjustment")
    s.set_defaults(func=cmd_pressure_adjust)

    s = sub.add_parser("pressure-trigger", help="Fire a pre-configured pressure trigger")
    s.add_argument("--event", required=True)
    s.add_argument("--session", type=int, default=0)
    s.set_defaults(func=cmd_pressure_trigger)

    # Themes
    s = sub.add_parser("theme-show", help="Show all campaign themes")
    s.set_defaults(func=cmd_theme_show)

    s = sub.add_parser("theme-boost", help="Boost a theme's score")
    s.add_argument("--theme", required=True)
    s.add_argument("--delta", type=int, default=5)
    s.set_defaults(func=cmd_theme_boost)

    s = sub.add_parser("theme-dominant", help="Show the dominant theme")
    s.set_defaults(func=cmd_theme_dominant)

    # Economy
    s = sub.add_parser("economy-show", help="Show all dramatic resources")
    s.set_defaults(func=cmd_economy_show)

    s = sub.add_parser("economy-exchange", help="Exchange dramatic resources")
    s.add_argument("--give", help="Comma-separated resource:amount pairs to gain")
    s.add_argument("--take", help="Comma-separated resource:amount pairs to spend")
    s.add_argument("--session", type=int, default=0)
    s.add_argument("--reason", help="Why this exchange")
    s.set_defaults(func=cmd_economy_exchange)

    # Beats
    s = sub.add_parser("beat-list", help="List all beat templates")
    s.set_defaults(func=cmd_beat_list)

    s = sub.add_parser("beat-apply", help="Apply a beat's effects")
    s.add_argument("--type", required=True, dest="beat_type", choices=list(BEAT_TEMPLATES.keys()))
    s.add_argument("--session", type=int, default=0)
    s.set_defaults(func=cmd_beat_apply)

    # Campaign State Machine
    s = sub.add_parser("state-show", help="Show current campaign phase")
    s.set_defaults(func=cmd_state_show)

    s = sub.add_parser("state-advance", help="Advance to next campaign phase")
    s.add_argument("--session", type=int, default=0)
    s.add_argument("--reason", help="Why advancing")
    s.set_defaults(func=cmd_state_advance)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
