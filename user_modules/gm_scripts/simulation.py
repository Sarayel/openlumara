#!/usr/bin/env python3
"""
simulation.py — Character Arcs + Emotional Memory + Multi-dimensional Relationships.

The Simulation layer. NPCs evolve along internal arcs (trajectory, not
personality), remember emotional significance (not transcripts), and have
multi-dimensional relationships (trust, fear, respect, love, hatred,
dependency, knowledge, leverage — each numeric).

The Scene Director (director.py) reads this layer to find character
development opportunities and to understand NPC motivation.

Storage:
  <campaign-dir>/character_arcs.json
  <campaign-dir>/emotional_memory.json
  <campaign-dir>/relationships.json

Usage:
  # Character Arcs
  python3 simulation.py arc-show --campaign <name> --npc sheriff
  python3 simulation.py arc-add --campaign <name> --npc sheriff '<json>'
  python3 simulation.py arc-advance --campaign <name> --npc sheriff --session 15
  python3 simulation.py arc-check-triggers --campaign <name> --session 15

  # Emotional Memory
  python3 simulation.py memory-add --campaign <name> '<json>'
  python3 simulation.py memory-show --campaign <name> --npc aldric
  python3 simulation.py memory-search --campaign <name> --tag betrayal
  python3 simulation.py memory-emotional-state --campaign <name> --npc aldric

  # Relationships (multi-dimensional)
  python3 simulation.py rel-show --campaign <name> --from sheriff --to prince
  python3 simulation.py rel-adjust --campaign <name> --from sheriff --to prince --axis trust --delta -10
  python3 simulation.py rel-matrix --campaign <name> --axis trust
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from paths import find_campaign


# ── IO helpers ──────────────────────────────────────────────────────────────

def _arcs_path(campaign: str) -> Path:
    return find_campaign(campaign) / "character_arcs.json"


def _memory_path(campaign: str) -> Path:
    return find_campaign(campaign) / "emotional_memory.json"


def _rel_path(campaign: str) -> Path:
    return find_campaign(campaign) / "relationships.json"


def _norm_id(name: str) -> str:
    if ":" in name:
        return name
    return f"npc:{name}"


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


def _clamp(val: int) -> int:
    return max(0, min(100, val))


# ── Relationship axes ───────────────────────────────────────────────────────

RELATIONSHIP_AXES = [
    "trust",       # does A believe B has their best interests at heart?
    "fear",        # does A fear what B can do to them?
    "respect",     # does A esteem B's capabilities or position?
    "love",        # does A feel affection for B?
    "hatred",      # does A actively want B harmed?
    "dependency",  # does A need something only B can provide?
    "knowledge",   # how much does A know about B?
    "leverage",    # does A hold something over B?
]

# VAD (Valence/Arousal/Dominance) — emotional dimension per directed edge.
# Different from the 8 relationship axes (which track social/political
# standing). VAD tracks the *emotional* state A feels toward B right now.
# Valence:   -1 (negative/hostile) to +1 (positive/warm)
# Arousal:   -1 (calm/indifferent) to +1 (intense/activated)
# Dominance: -1 (submissive/controlled) to +1 (dominant/in control)
VAD_AXES = ["valence", "arousal", "dominance"]


# ── Character Arc commands ──────────────────────────────────────────────────

def cmd_arc_show(args) -> int:
    """Show an NPC's character arc."""
    data = _load_json(_arcs_path(args.campaign), {"arcs": {}})
    npc_id = _norm_id(args.npc)
    arc = data.get("arcs", {}).get(npc_id)

    if not arc:
        print(f"# no arc registered for '{args.npc}'", file=sys.stderr)
        print(f"# use: python3 simulation.py arc-add --campaign {args.campaign} --npc {args.npc} '<json>'",
              file=sys.stderr)
        return 1

    print(f"# character arc: {arc.get('name', npc_id)}\n")
    print(f"  current goal:  {arc.get('current_goal', '?')}")
    print(f"  fear:          {arc.get('fear', '?')}")
    print(f"  need:          {arc.get('need', '?')}")
    print(f"  weakness:      {arc.get('weakness', '?')}")

    stages = arc.get("arc_stages", [])
    current = arc.get("current_stage", 0)

    print(f"\n  arc trajectory:")
    for i, stage in enumerate(stages):
        marker = "▶" if i == current else " " if i < current else "·"
        print(f"    {marker} [{i}] {stage}")

    # Show transitions
    transitions = arc.get("stage_transitions", [])
    if transitions:
        print(f"\n  stage transitions:")
        for t in transitions:
            triggered = t.get("triggered_session") is not None
            status = "✓" if triggered else "○"
            print(f"    {status} {t.get('from', '?')} → {t.get('to', '?')}: {t.get('trigger', '?')}")
            if triggered:
                print(f"        triggered session {t.get('triggered_session')}")
    return 0


def cmd_arc_add(args) -> int:
    """Add or replace an NPC's character arc."""
    data = _load_json(_arcs_path(args.campaign), {"arcs": {}})
    npc_id = _norm_id(args.npc)

    try:
        arc = json.loads(args.json)
    except json.JSONDecodeError as e:
        print(f"error: invalid JSON: {e}", file=sys.stderr)
        return 1

    arc.setdefault("current_stage", 0)
    arc.setdefault("stage_transitions", [])
    arc.setdefault("name", args.npc)

    data.setdefault("arcs", {})[npc_id] = arc
    _save_json(_arcs_path(args.campaign), data)
    print(f"OK — arc registered for {npc_id}")
    print(f"  stages: {len(arc.get('arc_stages', []))}")
    print(f"  current: stage {arc['current_stage']} — {arc.get('arc_stages', ['?'])[arc['current_stage']] if arc.get('arc_stages') else '?'}")
    return 0


def cmd_arc_advance(args) -> int:
    """Advance an NPC to the next arc stage."""
    data = _load_json(_arcs_path(args.campaign), {"arcs": {}})
    npc_id = _norm_id(args.npc)
    arc = data.get("arcs", {}).get(npc_id)

    if not arc:
        print(f"# no arc for '{args.npc}'", file=sys.stderr)
        return 1

    stages = arc.get("arc_stages", [])
    current = arc.get("current_stage", 0)

    if current >= len(stages) - 1:
        print(f"# {arc.get('name', npc_id)} is already at final stage ({stages[current]})")
        return 0

    old_stage = stages[current]
    arc["current_stage"] = current + 1
    new_stage = stages[arc["current_stage"]]

    # Mark the transition as triggered
    for t in arc.get("stage_transitions", []):
        if t.get("to") == new_stage and t.get("triggered_session") is None:
            t["triggered_session"] = args.session
            break

    _save_json(_arcs_path(args.campaign), data)
    print(f"OK — arc advanced for {arc.get('name', npc_id)}")
    print(f"  {old_stage} → {new_stage}")
    print(f"  stage {arc['current_stage']} of {len(stages) - 1}")
    if arc["current_stage"] == len(stages) - 1:
        print(f"  ⚠ FINAL STAGE — arc is complete")
    return 0


def cmd_arc_check_triggers(args) -> int:
    """Check all NPC arcs for transitions whose trigger conditions might be met.

    This is a heuristic check — the GM (with model assistance) interprets
    whether the trigger text matches recent events. The script surfaces
    candidates; the GM decides.
    """
    data = _load_json(_arcs_path(args.campaign), {"arcs": {}})

    candidates = []
    for npc_id, arc in data.get("arcs", {}).items():
        current = arc.get("current_stage", 0)
        stages = arc.get("arc_stages", [])
        for t in arc.get("stage_transitions", []):
            if t.get("triggered_session") is not None:
                continue
            if t.get("from") != stages[current]:
                continue
            candidates.append({
                "npc": arc.get("name", npc_id),
                "npc_id": npc_id,
                "from": t.get("from"),
                "to": t.get("to"),
                "trigger": t.get("trigger"),
            })

    if not candidates:
        print(f"# no pending arc transitions for session {args.session}")
        return 0

    print(f"# {len(candidates)} pending arc transition(s) to review:\n")
    for c in candidates:
        print(f"  {c['npc']}: {c['from']} → {c['to']}")
        print(f"    trigger: {c['trigger']}")
        print(f"    to advance: python3 simulation.py arc-advance --campaign {args.campaign} --npc {c['npc_id']} --session {args.session}")
        print()
    return 0


# ── Emotional Memory commands ───────────────────────────────────────────────

def cmd_memory_add(args) -> int:
    """Add an emotional memory."""
    data = _load_json(_memory_path(args.campaign), {"memories": []})
    try:
        mem = json.loads(args.json)
    except json.JSONDecodeError as e:
        print(f"error: invalid JSON: {e}", file=sys.stderr)
        return 1

    if "event" not in mem:
        print("error: memory must have 'event'", file=sys.stderr)
        return 1

    mem.setdefault("id", f"em{len(data.get('memories', [])) + 1:03d}")
    mem.setdefault("session", args.session or 0)
    mem.setdefault("importance", 5)
    mem.setdefault("participants", [])
    mem.setdefault("emotions", {})
    mem.setdefault("trauma", 0)
    mem.setdefault("resentment", 0)
    mem.setdefault("pride", 0)
    mem.setdefault("fear", 0)
    mem.setdefault("tags", [])

    data.setdefault("memories", []).append(mem)
    _save_json(_memory_path(args.campaign), data)
    print(f"OK — emotional memory '{mem['id']}' added")
    print(f"  event: {mem['event'][:80]}")
    print(f"  importance: {mem['importance']}/10")
    print(f"  participants: {len(mem['participants'])}")
    print(f"  tags: {mem['tags']}")
    return 0


def cmd_memory_show(args) -> int:
    """Show emotional memories involving an NPC."""
    data = _load_json(_memory_path(args.campaign), {"memories": []})
    npc_id = _norm_id(args.npc)

    memories = [m for m in data.get("memories", [])
                if npc_id in m.get("participants", []) or npc_id in m.get("emotions", {})]

    if not memories:
        print(f"# no emotional memories for '{args.npc}'")
        return 0

    memories.sort(key=lambda m: m.get("importance", 5), reverse=True)

    print(f"# emotional memories for {args.npc} ({len(memories)} total)\n")
    for m in memories[:20]:  # cap at 20 most important
        print(f"  [s{m.get('session', '?'):>3}] {m.get('event', '?')[:60]}")
        print(f"         importance: {m.get('importance', '?')}/10  tags: {m.get('tags', [])}")
        emotions = m.get("emotions", {}).get(npc_id, {})
        if emotions:
            emo_str = ", ".join(f"{k}:{v}" for k, v in emotions.items())
            print(f"         emotions: {emo_str}")
    return 0


def cmd_memory_search(args) -> int:
    """Search emotional memories by tag."""
    data = _load_json(_memory_path(args.campaign), {"memories": []})

    memories = [m for m in data.get("memories", []) if args.tag in m.get("tags", [])]

    if not memories:
        print(f"# no memories tagged '{args.tag}'")
        return 0

    memories.sort(key=lambda m: m.get("importance", 5), reverse=True)
    print(f"# {len(memories)} memory(ies) tagged '{args.tag}'\n")
    for m in memories:
        print(f"  [s{m.get('session', '?'):>3}] {m.get('event', '?')[:70]}")
        print(f"         importance: {m.get('importance', '?')}/10")
    return 0


def cmd_memory_emotional_state(args) -> int:
    """Summarize an NPC's current emotional state from their memories."""
    data = _load_json(_memory_path(args.campaign), {"memories": []})
    npc_id = _norm_id(args.npc)

    # Aggregate emotional weights, with recent memories counting more
    state = {"trauma": 0, "resentment": 0, "pride": 0, "fear": 0, "joy": 0, "anger": 0}
    total_weight = 0

    for m in data.get("memories", []):
        if npc_id not in m.get("participants", []) and npc_id not in m.get("emotions", {}):
            continue
        # Weight by importance and recency (more important + more recent = higher weight)
        weight = m.get("importance", 5)
        for emotion, value in m.get("emotions", {}).get(npc_id, {}).items():
            if emotion in state:
                state[emotion] += value * weight
            else:
                state[emotion] = value * weight
        total_weight += weight

    if total_weight == 0:
        print(f"# no emotional data for '{args.npc}'")
        return 0

    # Normalize to 0-100
    for k in state:
        state[k] = min(100, state[k] * 10 // max(1, total_weight))

    print(f"# emotional state: {args.npc}\n")
    for emotion, score in sorted(state.items(), key=lambda x: x[1], reverse=True):
        if score > 0:
            bar = "█" * (score // 5) + "░" * (20 - score // 5)
            print(f"  {emotion:<12} {score:>3} {bar}")

    # Identify dominant emotion
    dominant = max(state, key=state.get) if state else None
    if dominant and state[dominant] > 0:
        print(f"\n  dominant emotion: {dominant}")
        if state[dominant] >= 60:
            print(f"  ⚠ this emotion is pressing — NPC behavior should reflect it")
    return 0


# ── Relationship commands ───────────────────────────────────────────────────

def _load_relationships(campaign: str) -> dict:
    data = _load_json(_rel_path(campaign), {"edges": {}})
    data.setdefault("edges", {})
    return data


def _get_edge(data: dict, from_id: str, to_id: str) -> dict:
    key = f"{from_id}->{to_id}"
    edges = data.setdefault("edges", {})
    if key not in edges:
        edges[key] = {axis: 0 for axis in RELATIONSHIP_AXES}
    return edges[key]


def cmd_rel_show(args) -> int:
    """Show the multi-dimensional relationship from one NPC to another."""
    data = _load_relationships(args.campaign)
    from_id = _norm_id(args.from_npc)
    to_id = _norm_id(args.to_npc)
    edge = _get_edge(data, from_id, to_id)

    print(f"# relationship: {args.from_npc} → {args.to_npc}\n")
    for axis in RELATIONSHIP_AXES:
        val = edge.get(axis, 0)
        bar = "█" * (val // 5) + "░" * (20 - val // 5)
        label = "low" if val < 20 else "moderate" if val < 40 else "medium" if val < 60 else "high" if val < 80 else "extreme"
        print(f"  {axis:<12} {val:>3} {bar} ({label})")

    # Interpret the relationship
    high_axes = [(a, edge[a]) for a in RELATIONSHIP_AXES if edge.get(a, 0) >= 60]
    if high_axes:
        high_axes.sort(key=lambda x: x[1], reverse=True)
        print(f"\n  dominant dimensions:")
        for axis, val in high_axes[:3]:
            print(f"    {axis}: {val}")
    return 0


def cmd_rel_adjust(args) -> int:
    """Adjust a single relationship axis."""
    data = _load_relationships(args.campaign)
    from_id = _norm_id(args.from_npc)
    to_id = _norm_id(args.to_npc)
    edge = _get_edge(data, from_id, to_id)

    if args.axis not in RELATIONSHIP_AXES:
        print(f"error: unknown axis '{args.axis}'. Valid: {RELATIONSHIP_AXES}", file=sys.stderr)
        return 1

    old = edge.get(args.axis, 0)
    edge[args.axis] = _clamp(old + args.delta)

    _save_json(_rel_path(args.campaign), data)
    print(f"OK — relationship adjusted")
    print(f"  {args.from_npc} → {args.to_npc}: {args.axis} {old} → {edge[args.axis]} ({args.delta:+d})")

    # Check for threshold crossings
    if old < 60 <= edge[args.axis]:
        print(f"  ⚠ {args.axis} crossed into 'high' — behavior should shift")
    elif old < 80 <= edge[args.axis]:
        print(f"  ⚠ {args.axis} crossed into 'extreme' — this defines the relationship now")
    return 0


def cmd_rel_matrix(args) -> int:
    """Show a matrix of all relationships along one axis."""
    data = _load_relationships(args.campaign)

    if args.axis not in RELATIONSHIP_AXES:
        print(f"error: unknown axis '{args.axis}'. Valid: {RELATIONSHIP_AXES}", file=sys.stderr)
        return 1

    # Collect all NPCs
    all_npcs = set()
    for key in data.get("edges", {}):
        parts = key.split("->")
        if len(parts) == 2:
            all_npcs.add(parts[0])
            all_npcs.add(parts[1])
    all_npcs = sorted(all_npcs)

    if not all_npcs:
        print(f"# no relationship data")
        return 0

    # Print matrix
    print(f"# relationship matrix: {args.axis}\n")
    from_to_label = "From\\To"
    header = f"{from_to_label:<20}" + "".join(f"{n[-12:]:>14}" for n in all_npcs)
    print(header)
    print("-" * len(header))

    for from_npc in all_npcs:
        row = f"{from_npc:<20}"
        for to_npc in all_npcs:
            if from_npc == to_npc:
                row += f"{'—':>14}"
            else:
                edge = _get_edge(data, from_npc, to_npc)
                val = edge.get(args.axis, 0)
                row += f"{val:>14}" if val > 0 else f"{'·':>14}"
        print(row)

    print(f"\n# {len(all_npcs)} NPCs, axis: {args.axis}")
    return 0


# ── VAD (Valence/Arousal/Dominance) emotional network ──────────────────────

def cmd_vad_show(args) -> int:
    """Show the VAD emotional state of one NPC toward another."""
    data = _load_relationships(args.campaign)
    from_id = _norm_id(args.from_npc)
    to_id = _norm_id(args.to_npc)
    edge = _get_edge(data, from_id, to_id)

    # VAD is stored in edge["vad"] = {valence, arousal, dominance}
    vad = edge.get("vad", {"valence": 0.0, "arousal": 0.0, "dominance": 0.0})

    print(f"# VAD: {args.from_npc} → {args.to_npc}\n")
    for axis in VAD_AXES:
        val = vad.get(axis, 0.0)
        bar_len = int(abs(val) * 10)
        if val >= 0:
            bar = "░" * 10 + "█" * bar_len
        else:
            bar = "█" * bar_len + "░" * 10
        print(f"  {axis:<10} {val:+.2f} [{bar}]")

    # Interpret
    v, a, d = vad.get("valence", 0), vad.get("arousal", 0), vad.get("dominance", 0)
    print(f"\n  interpretation:")
    if v > 0.3 and a > 0.3:
        print(f"    warm and energized — excited to see them")
    elif v > 0.3 and a < -0.2:
        print(f"    warm and calm — comfortable, trusting")
    elif v < -0.3 and a > 0.5:
        print(f"    hostile and activated — angry, ready to fight")
    elif v < -0.3 and a < -0.2:
        print(f"    hostile and cold — resentful, withdrawn")
    elif abs(v) < 0.2 and a < -0.3:
        print(f"    indifferent and calm — doesn't care")
    elif abs(v) < 0.2 and a > 0.5:
        print(f"    neutral but activated — anxious, uncertain")

    if d > 0.4:
        print(f"    feels in control of this relationship")
    elif d < -0.4:
        print(f"    feels controlled by the other")
    return 0


def cmd_vad_adjust(args) -> int:
    """Adjust a VAD axis for a directed edge."""
    data = _load_relationships(args.campaign)
    from_id = _norm_id(args.from_npc)
    to_id = _norm_id(args.to_npc)
    edge = _get_edge(data, from_id, to_id)

    vad = edge.setdefault("vad", {"valence": 0.0, "arousal": 0.0, "dominance": 0.0})
    if args.axis not in VAD_AXES:
        print(f"error: invalid VAD axis '{args.axis}'. Valid: {VAD_AXES}", file=sys.stderr)
        return 1

    old = vad.get(args.axis, 0.0)
    vad[args.axis] = max(-1.0, min(1.0, old + args.delta))

    _save_json(_rel_path(args.campaign), data)
    print(f"OK — VAD adjusted: {args.from_npc} → {args.to_npc}")
    print(f"  {args.axis}: {old:+.2f} → {vad[args.axis]:+.2f} ({args.delta:+.2f})")
    return 0


def cmd_vad_network(args) -> int:
    """Show the full VAD network — who feels what toward whom."""
    data = _load_relationships(args.campaign)
    edges = data.get("edges", {})

    # Collect all NPCs
    all_npcs = set()
    for key in edges:
        parts = key.split("->")
        if len(parts) == 2:
            all_npcs.add(parts[0])
            all_npcs.add(parts[1])
    all_npcs = sorted(all_npcs)

    if not all_npcs:
        print(f"# no VAD data — use vad-adjust to set emotional states")
        return 0

    print(f"# VAD network ({len(all_npcs)} NPCs)\n")
    for from_npc in all_npcs:
        for to_npc in all_npcs:
            if from_npc == to_npc:
                continue
            edge = _get_edge(data, from_npc, to_npc)
            vad = edge.get("vad")
            if not vad:
                continue
            v, a, d = vad.get("valence", 0), vad.get("arousal", 0), vad.get("dominance", 0)
            if abs(v) < 0.1 and abs(a) < 0.1 and abs(d) < 0.1:
                continue
            print(f"  {from_npc} → {to_npc}: V={v:+.1f} A={a:+.1f} D={d:+.1f}")
    return 0


# ── CLI ─────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--campaign", required=True, help="Campaign name")
    sub = p.add_subparsers(dest="cmd", required=True)

    # Character Arcs
    s = sub.add_parser("arc-show", help="Show an NPC's character arc")
    s.add_argument("--npc", required=True)
    s.set_defaults(func=cmd_arc_show)

    s = sub.add_parser("arc-add", help="Add or replace an NPC's arc")
    s.add_argument("--npc", required=True)
    s.add_argument("json", help="Arc JSON")
    s.set_defaults(func=cmd_arc_add)

    s = sub.add_parser("arc-advance", help="Advance an NPC to the next arc stage")
    s.add_argument("--npc", required=True)
    s.add_argument("--session", type=int, default=0)
    s.set_defaults(func=cmd_arc_advance)

    s = sub.add_parser("arc-check-triggers", help="Check for pending arc transitions")
    s.add_argument("--session", type=int, default=0)
    s.set_defaults(func=cmd_arc_check_triggers)

    # Emotional Memory
    s = sub.add_parser("memory-add", help="Add an emotional memory")
    s.add_argument("json", help="Memory JSON")
    s.add_argument("--session", type=int, default=0)
    s.set_defaults(func=cmd_memory_add)

    s = sub.add_parser("memory-show", help="Show memories involving an NPC")
    s.add_argument("--npc", required=True)
    s.set_defaults(func=cmd_memory_show)

    s = sub.add_parser("memory-search", help="Search memories by tag")
    s.add_argument("--tag", required=True)
    s.set_defaults(func=cmd_memory_search)

    s = sub.add_parser("memory-emotional-state", help="Summarize an NPC's emotional state")
    s.add_argument("--npc", required=True)
    s.set_defaults(func=cmd_memory_emotional_state)

    # Relationships
    s = sub.add_parser("rel-show", help="Show multi-dimensional relationship")
    s.add_argument("--from-npc", required=True, dest="from_npc")
    s.add_argument("--to-npc", required=True, dest="to_npc")
    s.set_defaults(func=cmd_rel_show)

    s = sub.add_parser("rel-adjust", help="Adjust a relationship axis")
    s.add_argument("--from-npc", required=True, dest="from_npc")
    s.add_argument("--to-npc", required=True, dest="to_npc")
    s.add_argument("--axis", required=True, choices=RELATIONSHIP_AXES)
    s.add_argument("--delta", type=int, required=True)
    s.set_defaults(func=cmd_rel_adjust)

    s = sub.add_parser("rel-matrix", help="Show relationship matrix for one axis")
    s.add_argument("--axis", required=True, choices=RELATIONSHIP_AXES)
    s.set_defaults(func=cmd_rel_matrix)

    # VAD (Valence/Arousal/Dominance) emotional network
    s = sub.add_parser("vad-show", help="Show VAD emotional state between two NPCs")
    s.add_argument("--from-npc", required=True, dest="from_npc")
    s.add_argument("--to-npc", required=True, dest="to_npc")
    s.set_defaults(func=cmd_vad_show)

    s = sub.add_parser("vad-adjust", help="Adjust a VAD axis")
    s.add_argument("--from-npc", required=True, dest="from_npc")
    s.add_argument("--to-npc", required=True, dest="to_npc")
    s.add_argument("--axis", required=True, choices=VAD_AXES)
    s.add_argument("--delta", type=float, required=True)
    s.set_defaults(func=cmd_vad_adjust)

    s = sub.add_parser("vad-network", help="Show the full VAD emotional network")
    s.set_defaults(func=cmd_vad_network)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
