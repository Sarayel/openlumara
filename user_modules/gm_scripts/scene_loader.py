#!/usr/bin/env python3
"""
scene_loader.py — token-budgeted Scene Card generator.

Layer 2 of the hierarchical GM tracking system. Produces a Tier 0 Scene
Card (≈500-1500 tokens) from Tier 1 + Tier 2 inputs, capped at --budget
tokens. The Scene Card is the minimum context to run one scene.

Pulls from:
  - state.md → Current Situation, Live State Flags, Active Quests
  - world.md → Adventure Nodes (place cards) or Settlement locations
  - npcs.md → index table (one-line NPC summaries)
  - scene_index.json → latest outcomes for on-scene NPCs and threads
  - plans.json → current NPC plan steps (for agency visibility)
  - intrigues.json → active intrigues surfacing in this scene
  - graph.json → relational subgraph (via gm_graph.py scene-context)
  - characters/*.md → PC resource snapshots
  - tracker.json → active conditions/effects on on-scene characters

Output: a single markdown block written to state.md → ## Scene Card,
ready to drop into the GM's context at scene start.

Usage:
  python3 scene_loader.py --campaign <name> \
      --place "<location>" \
      --present "<npc1,npc2>" \
      --threads "<thread1,thread2>" \
      --budget 1200 \
      [--focus <npc>] \
      [--at-session N]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

from paths import find_campaign, python_executable


# ── Token estimation ────────────────────────────────────────────────────────

def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token for English text."""
    return len(text) // 4


# ── Campaign file readers ───────────────────────────────────────────────────

def _read_state(campaign: str) -> dict:
    """Read targeted sections from state.md."""
    state_path = find_campaign(campaign) / "state.md"
    if not state_path.exists():
        return {}
    text = state_path.read_text(errors="replace")

    sections = {}
    # Extract ## headers and their content
    for match in re.finditer(r"^## (.+?)\n(.*?)(?=\n## |\Z)", text, re.MULTILINE | re.DOTALL):
        sections[match.group(1).strip()] = match.group(2).strip()

    return {
        "raw": text,
        "sections": sections,
        "current_situation": sections.get("Current Situation", ""),
        "live_state_flags": sections.get("Live State Flags", ""),
        "active_quests": sections.get("Active Quests", ""),
        "open_threads": sections.get("Open Threads & Rumours", ""),
        "recent_events": sections.get("Recent Events", ""),
        "faction_moves": sections.get("Faction Moves", ""),
        "scene_card": sections.get("Scene Card", ""),
        "active_intrigues": sections.get("Active Intrigues", ""),
        "campaign_arc": sections.get("Campaign Arc", ""),
    }


def _read_world_place(campaign: str, place_name: str) -> str:
    """Find a place card from world.md."""
    world_path = find_campaign(campaign) / "world.md"
    if not world_path.exists():
        return f"(world.md not found — place '{place_name}' unknown)"

    text = world_path.read_text(errors="replace")
    place_lower = place_name.lower()

    # Look for ### <Place Name> heading
    for match in re.finditer(r"^### (.+?)\n(.*?)(?=\n### |\n## |\Z)", text, re.MULTILINE | re.DOTALL):
        heading = match.group(1).strip()
        if place_lower in heading.lower() or heading.lower() in place_lower:
            return f"**{heading}**\n{match.group(2).strip()[:800]}"

    # Look in Notable Districts & Locations
    for match in re.finditer(r"^\s+-\s+(.+?):\s*(.+)$", text, re.MULTILINE):
        name = match.group(1).strip()
        if place_lower in name.lower():
            return f"**{name}**: {match.group(2).strip()}"

    return f"(place '{place_name}' not found in world.md — describe from context)"


def _read_location_atmosphere(campaign: str, place_name: str, session: int) -> Optional[str]:
    """Read sensory atmosphere from locations.json. Returns None if not registered."""
    loc_path = find_campaign(campaign) / "locations.json"
    if not loc_path.exists():
        return None

    try:
        data = json.loads(loc_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    place_lower = place_name.lower()
    for loc in data.get("locations", []):
        # Match by id, name, or partial match
        if (place_lower == loc.get("id", "").lower() or
            place_lower == loc.get("name", "").lower() or
            place_lower in loc.get("name", "").lower() or
            loc.get("id", "").lower() in place_lower):
            return _format_location_atmosphere(loc, session)
    return None


def _format_location_atmosphere(loc: dict, session: int) -> str:
    """Format a location's atmosphere for the Scene Card."""
    name = loc.get("name", loc.get("id", "?"))
    sensory = loc.get("sensory", {})
    visits = loc.get("visits", [])
    visit_count = len(visits)
    pending_changes = loc.get("_pending_changes", [])

    lines = [f"**{name}**"]

    if loc.get("mood"):
        lines.append(f"mood: {loc['mood']}")

    if visit_count == 0:
        lines.append("(first visit — full sensory description)")
        for sense in ("smell", "sound", "texture", "light", "temperature", "weight"):
            val = sensory.get(sense, "")
            if val:
                lines.append(f"  {sense}: {val}")
        # Include visual cues on first visit
        for cue in loc.get("visual_cues", [])[:3]:
            lines.append(f"  • {cue.get('cue', '')}: {cue.get('detail', '')}")
    elif pending_changes:
        lines.append(f"(returning — CHANGED since session {visits[-1].get('session', '?')})")
        lines.append("  what's different:")
        for change in pending_changes:
            lines.append(f"    • {change}")
        # Brief sensory recap
        for sense in ("smell", "sound"):
            val = sensory.get(sense, "")
            if val:
                lines.append(f"  {sense}: {val}")
    else:
        lines.append("(returning — unchanged)")
        anchor = sensory.get("smell", "") or sensory.get("sound", "")
        if anchor:
            lines.append(f"  anchor: {anchor}")

    return "\n".join(lines)


def _read_narrator_guide(campaign: str) -> Optional[str]:
    """Read the narrator persona guide for Scene Card inclusion."""
    narrator_path = find_campaign(campaign) / "narrator.json"
    if not narrator_path.exists():
        return None

    try:
        data = json.loads(narrator_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    # Use custom narrator if set, otherwise preset
    if data.get("custom"):
        narrator = data["custom"]
    else:
        # Import presets from narrator.py
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent))
        try:
            from narrator import NARRATOR_PRESETS
            narrator = NARRATOR_PRESETS.get(data.get("preset", "standard"), NARRATOR_PRESETS["standard"])
        except ImportError:
            return None

    lines = [f"voice: {narrator.get('voice', '?')}"]
    if narrator.get("tone"):
        lines.append(f"tone: {narrator['tone']}")
    if narrator.get("pacing"):
        lines.append(f"pacing: {narrator['pacing']}")
    focus = narrator.get("sensory_focus", [])
    if focus:
        lines.append(f"sensory focus: {'; '.join(focus[:3])}")
    metaphors = narrator.get("metaphors", [])
    if metaphors:
        lines.append(f"imagery: {', '.join(metaphors[:3])}")
    forbidden = narrator.get("forbidden", [])
    if forbidden:
        lines.append(f"avoid: {', '.join(forbidden[:3])}")

    # Language directive — the LLM thinks in English but renders in target language
    lang = data.get("language", "en")
    if lang and lang != "en":
        # Import language names from narrator.py
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent))
        try:
            from narrator import LANGUAGE_NAMES
            lang_name = LANGUAGE_NAMES.get(lang, lang)
        except ImportError:
            lang_name = lang
        lines.append(f"")
        lines.append(f"⚠ RENDERING LANGUAGE: {lang_name} ({lang})")
        lines.append(f"   Think in English. Write ALL narration, dialogue, and descriptions in {lang_name}.")
        lines.append(f"   Adapt NPC speech quirks to {lang_name} — don't translate literally.")
        lines.append(f"   Internal state files stay in English.")

    return "\n".join(lines)


def _read_npc_index(campaign: str, npc_names: list) -> dict:
    """Read one-line summaries for specified NPCs from npcs.md index."""
    npcs_path = find_campaign(campaign) / "npcs.md"
    if not npcs_path.exists():
        return {}

    text = npcs_path.read_text(errors="replace")
    result = {}

    # Parse the index table
    for name in npc_names:
        name_lower = name.lower()
        # Look in table rows
        for match in re.finditer(r"^\| (.+?) \| (.+?) \| (.+?) \| (.+?) \| (.+?) \| (.+?) \|", text, re.MULTILINE):
            row_name = match.group(1).strip()
            if name_lower in row_name.lower() or row_name.lower() in name_lower:
                result[name] = {
                    "role": match.group(2).strip(),
                    "faction": match.group(3).strip(),
                    "location": match.group(4).strip(),
                    "attitude": match.group(5).strip(),
                    "notes": match.group(6).strip(),
                }
                break

    # For NPCs not in the index, try npcs-full.md headers
    if len(result) < len(npc_names):
        npcs_full = find_campaign(campaign) / "npcs-full.md"
        if npcs_full.exists():
            full_text = npcs_full.read_text(errors="replace")
            for name in npc_names:
                if name in result:
                    continue
                name_lower = name.lower()
                for match in re.finditer(r"^### (.+?)\n(.*?)(?=\n### |\n## |\Z)", full_text, re.MULTILINE | re.DOTALL):
                    heading = match.group(1).strip()
                    if name_lower in heading.lower():
                        # Extract first few lines
                        lines = match.group(2).strip().split("\n")[:5]
                        result[name] = {"summary": " | ".join(l.strip() for l in lines if l.strip())}
                        break

    return result


def _read_npc_full(campaign: str, npc_name: str) -> str:
    """Read a full NPC profile from npcs-full.md."""
    npcs_full = find_campaign(campaign) / "npcs-full.md"
    if not npcs_full.exists():
        return f"(npcs-full.md not found)"

    text = npcs_full.read_text(errors="replace")
    name_lower = npc_name.lower()

    for match in re.finditer(r"^### (.+?)\n(.*?)(?=\n### |\n## |\Z)", text, re.MULTILINE | re.DOTALL):
        heading = match.group(1).strip()
        if name_lower in heading.lower():
            return f"### {heading}\n{match.group(2).strip()[:2000]}"

    return f"(NPC '{npc_name}' not found in npcs-full.md)"


def _read_pc_snapshots(campaign: str) -> list:
    """Read compact PC snapshots from characters/*.md."""
    chars_dir = find_campaign(campaign) / "characters"
    if not chars_dir.exists():
        return []

    snapshots = []
    for char_file in sorted(chars_dir.glob("*.md")):
        text = char_file.read_text(errors="replace")
        name = char_file.stem

        # Extract key fields
        hp_match = re.search(r"HP:\s*(\d+)\s*/\s*(\d+)", text)
        ac_match = re.search(r"AC:\s*(\d+)", text)
        level_match = re.search(r"Level:\s*(\d+)", text)
        class_match = re.search(r"Class:\s*(\S+)", text)

        snap = {
            "name": name,
            "hp": f"{hp_match.group(1)}/{hp_match.group(2)}" if hp_match else "?",
            "ac": ac_match.group(1) if ac_match else "?",
            "level": level_match.group(1) if level_match else "?",
            "class": class_match.group(1) if class_match else "?",
        }
        snapshots.append(snap)

    return snapshots


def _query_scene_index(campaign: str, keys: list, npc: str = None) -> list:
    """Query scene_index.py for matching scenes."""
    if not keys:
        return []

    script_path = Path(__file__).parent / "scene_index.py"
    cmd = [python_executable(), str(script_path), "--campaign", campaign, "query",
           "--keys", ",".join(keys), "--limit", "3"]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0 and result.stdout:
            return result.stdout
    except Exception:
        pass
    return []


def _query_plans(campaign: str, npc_names: list) -> dict:
    """Read plan summaries for on-scene NPCs from plans.json."""
    plans_path = find_campaign(campaign) / "plans.json"
    if not plans_path.exists():
        return {}

    try:
        data = json.loads(plans_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}

    result = {}
    for name in npc_names:
        npc_id = f"npc:{name}" if not name.startswith("npc:") else name
        npc = data.get("npcs", {}).get(npc_id)
        if npc:
            plan = npc.get("current_plan", {})
            result[name] = {
                "goal": plan.get("goal", "?"),
                "current_step": plan.get("current_step", 1),
                "total_steps": len(plan.get("steps", [])),
                "disposition": npc.get("disposition_toward_party", "?"),
                "intrigue_layer": npc.get("intrigue_layer", "?"),
                "plan_depth": npc.get("plan_depth", "light"),
            }
    return result


def _query_active_intrigues(campaign: str, threads: list) -> list:
    """Read active intrigues that surface in this scene."""
    intrigues_path = find_campaign(campaign) / "intrigues.json"
    if not intrigues_path.exists():
        return []

    try:
        data = json.loads(intrigues_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []

    active = []
    for intr in data.get("intrigues", []):
        if intr.get("status") not in ("active",):
            continue
        # Check if this intrigue is relevant to the threads
        relevant = False
        for thread in threads:
            if thread.lower() in intr.get("title", "").lower():
                relevant = True
                break
            if thread.lower() in intr.get("central_question", "").lower():
                relevant = True
                break
            for actor in intr.get("key_actors", []):
                if thread.lower() in actor.lower():
                    relevant = True
                    break

        # If no threads specified, include all active
        if not threads:
            relevant = True

        if relevant:
            # Include heat/deadline info for active intrigues
            entry = {
                "id": intr["id"],
                "title": intr.get("title", "?"),
                "layer": intr.get("layer", "A"),
                "central_question": intr.get("central_question", "?"),
                "revealed_clues_count": len(intr.get("revealed_clues", [])),
                "reveal_threshold": intr.get("reveal_threshold", 0),
                "arc_beat_gate": intr.get("arc_beat_gate"),
            }
            # Add heat metrics if present
            if "heat" in intr or "stability" in intr or "player_attention" in intr:
                entry["heat"] = intr.get("heat", 0)
                entry["stability"] = intr.get("stability", 50)
                entry["player_attention"] = intr.get("player_attention", 0)
            # Add deadline if present
            if intr.get("deadline"):
                entry["deadline"] = intr["deadline"]
            active.append(entry)

    return active


def _query_suspicion(campaign: str, npc_names: list) -> dict:
    """Read suspicion data for on-scene NPCs from suspicion.json."""
    suspicion_path = find_campaign(campaign) / "suspicion.json"
    if not suspicion_path.exists():
        return {}

    try:
        data = json.loads(suspicion_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}

    result = {}
    for name in npc_names:
        npc_id = f"npc:{name}" if not name.startswith("npc:") else name
        for entry in data.get("entries", []):
            if entry.get("npc_id") == npc_id:
                suspects = entry.get("suspects", {})
                if suspects:
                    # Get top 3 suspects above 40 (moderate+)
                    significant = {k: v for k, v in suspects.items() if v >= 40}
                    if significant:
                        top = sorted(significant.items(), key=lambda x: x[1], reverse=True)[:3]
                        result[name] = top
                break
    return result


def _query_graph(campaign: str, place: str, present: list, session: int) -> str:
    """Call gm_graph.py scene-context for the relational subgraph."""
    graph_script = Path(__file__).parent / "gm_graph.py"
    if not graph_script.exists():
        return "(gm_graph.py not found)"

    cmd = [
        python_executable(), str(graph_script), "scene-context",
        "--campaign", campaign,
        "--place", place,
        "--present", ",".join(present),
        "--hops", "2",
    ]
    if session:
        cmd.extend(["--at-session", str(session)])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            # Trim to first 1500 chars to stay within budget
            output = result.stdout.strip()
            if len(output) > 1500:
                output = output[:1500] + "\n  ...(truncated)"
            return output
    except Exception:
        pass
    return "(graph query failed)"


# ── Scene Card assembly ─────────────────────────────────────────────────────

def _assemble_scene_card(
    campaign: str,
    place: str,
    present: list,
    threads: list,
    focus_npc: Optional[str],
    budget: int,
    at_session: int,
) -> str:
    """Assemble the Scene Card from all sources, respecting token budget."""

    sections = []  # list of (priority, title, content) tuples

    # 0. Narrator persona (priority 1 — sets the voice for the whole card)
    narrator_guide = _read_narrator_guide(campaign)
    if narrator_guide:
        sections.append((1, "Narrator", narrator_guide))

    # 1. Place card (priority 1 — highest) — now with sensory atmosphere
    place_atmosphere = _read_location_atmosphere(campaign, place, at_session)
    if place_atmosphere:
        sections.append((1, "Location", place_atmosphere))
    else:
        place_card = _read_world_place(campaign, place)
        sections.append((1, "Location", place_card))

    # 2. PC snapshots (priority 1)
    pc_snaps = _read_pc_snapshots(campaign)
    if pc_snaps:
        pc_lines = []
        for snap in pc_snaps:
            pc_lines.append(
                f"- **{snap['name']}** ({snap['class']} {snap['level']}): "
                f"HP {snap['hp']}, AC {snap['ac']}"
            )
        sections.append((1, "Party", "\n".join(pc_lines)))
    else:
        sections.append((1, "Party", "(no character files found)"))

    # 3. NPC dossiers (priority 2 — present NPCs)
    npc_summaries = _read_npc_index(campaign, present)
    npc_plans = _query_plans(campaign, present)
    npc_suspicion = _query_suspicion(campaign, present)
    npc_lines = []
    for name in present:
        summary = npc_summaries.get(name, {})
        plan = npc_plans.get(name, {})
        line = f"- **{name}**"
        if summary.get("role"):
            line += f" ({summary['role']})"
        if summary.get("faction"):
            line += f" [{summary['faction']}]"
        if summary.get("attitude"):
            line += f" — {summary['attitude']}"
        if plan:
            line += f"\n  Plan: {plan.get('goal', '?')} (step {plan.get('current_step', 1)}/{plan.get('total_steps', '?')}, layer {plan.get('intrigue_layer', '?')})"
        # Add suspicion summary if this NPC has notable suspicions
        if name in npc_suspicion:
            susp = npc_suspicion[name]
            susp_str = ", ".join(f"{t} ({s})" for t, s in susp[:2])
            line += f"\n  Suspects: {susp_str}"
        npc_lines.append(line)
    sections.append((2, "On-scene NPCs", "\n".join(npc_lines) if npc_lines else "(none specified)"))

    # 4. Active threads + intrigues (priority 2)
    state = _read_state(campaign)
    active_intrigues = _query_active_intrigues(campaign, threads)

    thread_lines = []
    if threads:
        # Query scene_index for recent outcomes on these threads
        for thread in threads:
            recent = _query_scene_index(campaign, [thread])
            thread_lines.append(f"**{thread}**:")
            if recent:
                # Take first 2 lines of output
                lines = recent.strip().split("\n")[:4]
                thread_lines.append("  " + "\n  ".join(lines))
            else:
                thread_lines.append("  (no prior scenes indexed)")
    else:
        # Show open threads from state.md
        if state.get("open_threads"):
            thread_lines.append(state["open_threads"][:500])

    if active_intrigues:
        thread_lines.append("\n**Active intrigues surfacing:**")
        for intr in active_intrigues:
            gate = f" [gates beat {intr['arc_beat_gate']}]" if intr.get("arc_beat_gate") else ""
            clue_str = f" ({intr['revealed_clues_count']}/{intr['reveal_threshold']} clues){gate}"
            # Add heat/deadline info if present
            heat_str = ""
            if "heat" in intr and intr["heat"] >= 60:
                heat_str = f" ⚠heat:{intr['heat']}"
            elif "heat" in intr and intr["heat"] >= 30:
                heat_str = f" (heat:{intr['heat']})"
            deadline_str = ""
            if intr.get("deadline"):
                deadline_str = f" ⏰{intr['deadline']}"
            thread_lines.append(
                f"  - [{intr['layer']}] {intr['title']}: {intr['central_question']}"
                f"{clue_str}{heat_str}{deadline_str}"
            )

    sections.append((2, "Threads & Intrigues", "\n".join(thread_lines)))

    # 5. Live State Flags (priority 3)
    if state.get("live_state_flags"):
        flags = state["live_state_flags"][:800]
        sections.append((3, "Live State Flags", flags))

    # 6. Relational subgraph (priority 3)
    graph_output = _query_graph(campaign, place, present, at_session)
    sections.append((3, "Relational Subgraph", graph_output))

    # 7. Focus NPC full profile (priority 4 — only if focus specified)
    if focus_npc:
        focus_profile = _read_npc_full(campaign, focus_npc)
        sections.append((4, f"Focus NPC: {focus_npc}", focus_profile))

    # 8. Recent events (priority 5 — lowest, dropped first if over budget)
    if state.get("recent_events"):
        sections.append((5, "Recent Events", state["recent_events"][:600]))

    # Assemble in priority order, respecting budget
    sections.sort(key=lambda x: x[0])

    output_lines = [f"## Scene Card — {place}", ""]
    total_tokens = _estimate_tokens("\n".join(output_lines))
    dropped = []

    for priority, title, content in sections:
        section_text = f"### {title}\n{content}\n"
        section_tokens = _estimate_tokens(section_text)

        if total_tokens + section_tokens > budget and priority > 2:
            dropped.append(title)
            continue

        output_lines.append(section_text)
        total_tokens += section_tokens

    if dropped:
        output_lines.append(f"### (Truncated — dropped: {', '.join(dropped)})")
        output_lines.append(f"### Token budget: {total_tokens}/{budget} tokens")

    return "\n".join(output_lines)


def _write_scene_card(campaign: str, card: str) -> bool:
    """Write the Scene Card to state.md → ## Scene Card section."""
    state_path = find_campaign(campaign) / "state.md"
    if not state_path.exists():
        return False

    text = state_path.read_text(encoding="utf-8")

    # Replace or insert ## Scene Card section
    pattern = r"## Scene Card\n.*?(?=\n## |\Z)"
    replacement = card + "\n"

    if re.search(pattern, text, re.DOTALL):
        new_text = re.sub(pattern, replacement, text, count=1, flags=re.DOTALL)
    else:
        # Insert after the header line
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if line.startswith("## Current Situation"):
                lines.insert(i, card + "\n")
                break
        else:
            # Append at end
            lines.append("\n" + card)
        new_text = "\n".join(lines)

    state_path.write_text(new_text, encoding="utf-8")
    return True


# ── Main ────────────────────────────────────────────────────────────────────

def cmd_load(args) -> int:
    """Generate and optionally write a Scene Card."""
    place = args.place
    present = [n.strip() for n in args.present.split(",") if n.strip()] if args.present else []
    threads = [t.strip() for t in args.threads.split(",") if t.strip()] if args.threads else []

    # If place not provided, try to read it from state.md → ## Current Situation
    if not place:
        try:
            camp_path = find_campaign(args.campaign)
            state_path = camp_path / "state.md"
            if state_path.exists():
                state_text = state_path.read_text(encoding="utf-8", errors="replace")
                # Look for "- **Location:** <name>" in Current Situation
                import re as _re
                m = _re.search(r"\*\*Location:\*\*\s*(.+?)(?:\n|$)", state_text)
                if m:
                    place = m.group(1).strip()
                    # Also try to extract present NPCs if not provided
                    if not present:
                        m2 = _re.search(r"\*\*Present NPCs?\*\*:\s*(.+?)(?:\n|$)", state_text)
                        if m2:
                            present = [n.strip() for n in m2.group(1).split(",") if n.strip()]
        except Exception:
            pass
    # If still no place, use a placeholder so the card still generates
    if not place:
        place = "(unknown location — set ## Current Situation → Location in state.md)"

    card = _assemble_scene_card(
        campaign=args.campaign,
        place=place,
        present=present,
        threads=threads,
        focus_npc=args.focus,
        budget=args.budget,
        at_session=args.at_session or 0,
    )

    if args.write:
        if _write_scene_card(args.campaign, card):
            print(f"# Scene Card written to state.md ({_estimate_tokens(card)} tokens)")
            print()
        else:
            print(f"# could not write to state.md — printing to stdout", file=sys.stderr)

    print(card)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--campaign", required=True, help="Campaign name")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("load", help="Generate a Scene Card")
    s.add_argument("--place", help="Location name (if omitted, read from state.md ## Current Situation)")
    s.add_argument("--present", help="Comma-separated NPC names on-scene")
    s.add_argument("--threads", help="Comma-separated thread names to surface")
    s.add_argument("--focus", help="NPC name to load full profile for (primary interlocutor)")
    s.add_argument("--budget", type=int, default=1200, help="Token budget (default 1200)")
    s.add_argument("--at-session", type=int, default=0, help="Current session number")
    s.add_argument("--write", action="store_true", help="Write to state.md → ## Scene Card")
    s.set_defaults(func=cmd_load)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
