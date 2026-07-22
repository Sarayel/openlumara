#!/usr/bin/env python3
"""
intrigue_init.py — Phase 0: generate layered intrigues + seed NPC plans.

Takes a campaign's pitch, system, arc skeleton, NPCs, and factions, and
produces a layered intrigues.json (3 layers: A/B/C with parent/child links)
plus seed plans.json entries for 3-5 antagonists tied to Layer C.

This is the ONE place in the hierarchical tracking system where an LLM call
is justified — the initial creative generation of the quest structure.
Everything downstream (advancement, scene loading, indexing, arc coupling)
stays deterministic Python, consistent with the project's LLM-agnostic
principle.

Backend resolution (mirrors dm_help.py's portable approach):
  1. OTGM_INTRIGUE_CMD env var → custom command (prompt on stdin, JSON on stdout)
  2. Auto-detect: claude, opencode, gemini, llm on PATH
  3. OTGM_INTRIGUE_MODEL → pin a model for auto-detected backend
  4. If no backend available → fall back to deterministic template generator

Usage:
  python3 intrigue_init.py --campaign <name> [--dry-run] [--backend auto]
  python3 intrigue_init.py --campaign <name> --template   # force template fallback
  python3 intrigue_init.py --campaign <name> --status     # check backend availability

Output:
  - <campaign-dir>/intrigues.json (layered, with 3 root intrigues)
  - <campaign-dir>/plans.json (seed entries for 3-5 antagonists)
  - Both files cross-referenced with shared retrieval_keys
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from paths import find_campaign


# ── Backend resolution ──────────────────────────────────────────────────────

INTRIGUE_TIMEOUT = int(os.environ.get("OTGM_INTRIGUE_TIMEOUT", "120"))


def _detect_backend() -> Optional[tuple[str, list[str]]]:
    """Auto-detect an LLM CLI on PATH. Returns (name, command_prefix)."""
    for name, cmd in [
        ("claude", ["claude"]),
        ("opencode", ["opencode"]),
        ("gemini", ["gemini"]),
        ("llm", ["llm"]),
    ]:
        if shutil.which(cmd[0]):
            return name, cmd
    return None


def _backend_command() -> Optional[tuple[str, list[str]]]:
    """Resolve the intrigue generation backend.

    Priority:
      1. OTGM_INTRIGUE_CMD env var (split on spaces)
      2. Auto-detected CLI on PATH
    """
    custom = os.environ.get("OTGM_INTRIGUE_CMD", "").strip()
    if custom:
        parts = custom.split()
        return parts[0], parts

    detected = _detect_backend()
    if detected:
        return detected

    return None


def _backend_status() -> str:
    """Return human description of backend readiness."""
    custom = os.environ.get("OTGM_INTRIGUE_CMD", "").strip()
    if custom:
        return f"env:OTGM_INTRIGUE_CMD ({custom.split()[0]})"

    detected = _detect_backend()
    if detected:
        name, _ = detected
        model = os.environ.get("OTGM_INTRIGUE_MODEL", "")
        if model:
            return f"auto:{name} (model: {model})"
        return f"auto:{name}"

    return "none (will use template fallback)"


# ── Campaign context gathering ──────────────────────────────────────────────

def _gather_campaign_context(campaign: str) -> dict:
    """Read campaign files and assemble the context for intrigue generation."""
    camp_dir = find_campaign(campaign)
    context = {"campaign": campaign, "campaign_dir": str(camp_dir)}

    # state.md — premise, system, arc
    state_path = camp_dir / "state.md"
    if state_path.exists():
        state_text = state_path.read_text(errors="replace")
        context["state_excerpt"] = state_text[:4000]
        # Extract system module
        import re
        sys_match = re.search(r"\*\*System Module:\*\*\s*(\S+)", state_text)
        if sys_match:
            context["system"] = sys_match.group(1)
        # Extract arc
        arc_match = re.search(r"## Campaign Arc\n(.*?)(?=\n## |\Z)", state_text, re.DOTALL)
        if arc_match:
            context["arc_excerpt"] = arc_match.group(1)[:3000]
    else:
        context["state_excerpt"] = "(state.md not found)"

    # world.md — premise, factions, threat, mystery
    world_path = camp_dir / "world.md"
    if world_path.exists():
        world_text = world_path.read_text(errors="replace")
        context["world_excerpt"] = world_text[:6000]
    else:
        context["world_excerpt"] = "(world.md not found)"

    # npcs.md — NPC index
    npcs_path = camp_dir / "npcs.md"
    if npcs_path.exists():
        context["npcs_excerpt"] = npcs_path.read_text(errors="replace")[:4000]
    else:
        context["npcs_excerpt"] = "(npcs.md not found)"

    # npcs-full.md if it exists
    npcs_full_path = camp_dir / "npcs-full.md"
    if npcs_full_path.exists():
        context["npcs_full_excerpt"] = npcs_full_path.read_text(errors="replace")[:8000]

    return context


# ── LLM prompt ──────────────────────────────────────────────────────────────

def _build_prompt(context: dict) -> str:
    """Build the prompt for the LLM backend."""
    # Pre-compute the NPC profiles block (can't use nested f-strings with
    # backslashes inside an outer f-string expression on Python <3.12)
    npcs_full = context.get('npcs_full_excerpt', '')
    npcs_block = f"NPC profiles:\n{npcs_full}" if npcs_full else ""

    return f"""You are generating the initial quest structure for a tabletop RPG campaign.

Your output MUST be valid JSON with this exact schema:

{{
  "intrigues": [
    {{
      "id": "i001",
      "title": "<short title>",
      "type": "mystery|political|personal|threat",
      "layer": "A",
      "parent_intrigue": null,
      "unlocks": ["i002"],
      "unlock_condition": null,
      "reveal_threshold": 3,
      "status": "active",
      "central_question": "<the dramatic question this intrigue asks>",
      "key_actors": ["npc:name1", "npc:name2"],
      "red_herrings": ["<plausible false lead>"],
      "revealed_clues": [],
      "unrevealed_clues": ["<clue 1>", "<clue 2>", "<clue 3>"],
      "answer": "<the truth — hidden from players, visible to GM>",
      "resolution_condition": "<what solving looks like>",
      "impact_on_resolution": "<how this affects the campaign ending>",
      "arc_beat_gate": "<beat id this gates, or null>",
      "force_reveal_on_beat": {{"intrigue": "<id>", "clue": "<clue text>"}}
    }}
  ],
  "plans": [
    {{
      "npc_id": "npc:name",
      "name": "<display name>",
      "faction": "<faction name>",
      "archetype": "schemer|warrior|diplomat|fanatic|opportunist",
      "intrigue_layer": "C",
      "disposition_toward_party": "hostile|neutral|friendly",
      "trust": 0,
      "current_plan": {{
        "id": "p001",
        "goal": "<what they want>",
        "deadline": "session:20",
        "current_step": 1,
        "steps": [
          {{"id": 1, "action": "<step 1>", "status": "in_progress", "eta": "session:5"}},
          {{"id": 2, "action": "<step 2>", "status": "pending", "eta": "session:10", "requires": "step 1 complete"}},
          {{"id": 3, "action": "<step 3>", "status": "pending", "requires": "step 2 complete"}}
        ],
        "resources": ["<resource 1>", "<resource 2>"],
        "failure_condition": "<what would make this plan fail>",
        "adaptation_triggers": ["if <event> then <response>"]
      }}
    }}
  ]
}}

RULES:
1. Generate exactly 3 root intrigues (Layer A), each with 1-2 children (Layer B),
   and one Layer C "core" intrigue that is the campaign's true central mystery.
2. Layer A intrigues are solvable in 3-5 sessions. Layer B in 5-10. Layer C is
   the campaign's endpoint.
3. Layer B unlock_conditions reference their parent's clue count:
   "intrigue:<parent_id>:clues>=<threshold>"
4. Layer C is initially "hidden" status — it only becomes active after its
   parent (Layer B) resolves.
5. Generate seed plans for 3-5 antagonists tied to Layer C (the core intrigue).
   These get full state-machine plans (intrigue_layer: "C").
6. Cross-reference retrieval_keys: each intrigue's key_actors should appear in
   at least one plan, and vice versa.
7. Arc beat gates: at least one Layer A intrigue should gate beat "1b"
   (Complication), and the Layer C intrigue should gate beat "3a"
   (Final Confrontation).
8. Keep unrevealed_clues to 3-5 per intrigue — enough for a trail, not so
   many that the GM can't track them.
9. The "answer" field is the GM's secret. Make it surprising but consistent
   with the clues.

CAMPAIGN CONTEXT:

System: {context.get('system', 'unknown')}

State excerpt:
{context.get('state_excerpt', '(none)')}

World excerpt (premise, factions, threat, mystery):
{context.get('world_excerpt', '(none)')}

NPC index:
{context.get('npcs_excerpt', '(none)')}

{npcs_block}

Arc skeleton:
{context.get('arc_excerpt', '(no arc found in state.md)')}

Generate the JSON now. Output ONLY the JSON — no preamble, no markdown fences.
"""


def _call_llm_backend(prompt: str) -> Optional[str]:
    """Call the LLM backend and return the raw JSON string output."""
    backend = _backend_command()
    if not backend:
        return None

    name, cmd_prefix = backend
    model = os.environ.get("OTGM_INTRIGUE_MODEL", "")

    # Build the full command
    if name == "claude":
        cmd = cmd_prefix + ["-p", "--output-format", "text"]
        if model:
            cmd.extend(["--model", model])
    elif name == "llm":
        cmd = cmd_prefix + []
        if model:
            cmd.extend(["-m", model])
    else:
        # Generic: pass model as first arg if specified
        cmd = list(cmd_prefix)
        if model:
            cmd.append(model)

    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=INTRIGUE_TIMEOUT,
        )
        if result.returncode != 0:
            print(f"# backend '{name}' returned exit {result.returncode}",
                  file=sys.stderr)
            if result.stderr:
                print(f"# stderr: {result.stderr[:500]}", file=sys.stderr)
            return None
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        print(f"# backend '{name}' timed out after {INTRIGUE_TIMEOUT}s",
              file=sys.stderr)
        return None
    except FileNotFoundError:
        print(f"# backend '{name}' not found on PATH", file=sys.stderr)
        return None
    except Exception as e:
        print(f"# backend '{name}' error: {e}", file=sys.stderr)
        return None


# ── Deterministic template fallback ─────────────────────────────────────────

def _template_generate(context: dict) -> dict:
    """Generate a generic 3-layer intrigue structure from campaign context.

    This is the fallback when no LLM backend is available. It produces a
    valid but generic structure that the GM should refine manually.
    """
    system = context.get("system", "dnd5e")
    campaign = context.get("campaign", "campaign")

    # Try to extract faction names from world excerpt
    import re
    world_text = context.get("world_excerpt", "")
    faction_names = []
    for m in re.finditer(r"### (.+?) —", world_text):
        name = m.group(1).strip()
        if name and len(name) < 50:
            faction_names.append(name)

    # Try to extract NPC names
    npcs_text = context.get("npcs_excerpt", "")
    npc_names = []
    for m in re.finditer(r"^\| (.+?) \|", npcs_text, re.MULTILINE):
        name = m.group(1).strip()
        if name and len(name) < 30 and name not in ("Name", ""):
            npc_names.append(name)
    npc_names = npc_names[:5]  # cap at 5

    # Try to extract premise
    premise = ""
    prem_match = re.search(r"\*\*Premise:\*\*\s*(.+)", world_text)
    if prem_match:
        premise = prem_match.group(1).strip()

    # Build template intrigues
    template = {
        "version": 2,
        "intrigues": [
            {
                "id": "i001",
                "title": "The Surface Mystery",
                "type": "mystery",
                "layer": "A",
                "parent_intrigue": None,
                "unlocks": ["i004"],
                "unlock_condition": None,
                "reveal_threshold": 3,
                "status": "active",
                "introduced_session": 1,
                "central_question": f"What is really happening in {campaign}?",
                "key_actors": npc_names[:2] if npc_names else [],
                "red_herrings": ["a plausible false lead"],
                "revealed_clues": [],
                "unrevealed_clues": [
                    "clue 1 — findable without any roll",
                    "clue 2 — requires investigation",
                    "clue 3 — only accessible after significant progress",
                ],
                "answer": "GM should fill this in — the truth behind the surface mystery",
                "resolution_condition": "party has at least 3 clues and confronts the responsible party",
                "impact_on_resolution": "Determines which Layer B intrigue unlocks",
                "arc_beat_gate": "1b",
                "force_reveal_on_beat": None,
            },
            {
                "id": "i002",
                "title": "The Faction Conflict",
                "type": "political",
                "layer": "A",
                "parent_intrigue": None,
                "unlocks": ["i004"],
                "unlock_condition": None,
                "reveal_threshold": 3,
                "status": "active",
                "introduced_session": 1,
                "central_question": "Which faction will dominate, and at what cost?",
                "key_actors": [f"faction:{f}" for f in faction_names[:2]] if faction_names else [],
                "red_herrings": ["an apparent alliance that's actually a trap"],
                "revealed_clues": [],
                "unrevealed_clues": [
                    "faction A's secret resource",
                    "faction B's hidden weakness",
                    "the third party benefiting from the conflict",
                ],
                "answer": "GM should fill this in",
                "resolution_condition": "one faction is decisively weakened or empowered",
                "impact_on_resolution": "Shapes the political landscape for Layer B",
                "arc_beat_gate": "2a",
                "force_reveal_on_beat": None,
            },
            {
                "id": "i003",
                "title": "The Personal Stake",
                "type": "personal",
                "layer": "A",
                "parent_intrigue": None,
                "unlocks": ["i004"],
                "unlock_condition": None,
                "reveal_threshold": 2,
                "status": "active",
                "introduced_session": 1,
                "central_question": "What is the party willing to risk?",
                "key_actors": npc_names[2:4] if len(npc_names) >= 4 else npc_names,
                "red_herrings": [],
                "revealed_clues": [],
                "unrevealed_clues": [
                    "a personal connection to the main threat",
                    "the cost of refusing the call",
                ],
                "answer": "GM should fill this in — tied to PC backstories",
                "resolution_condition": "party commits to the campaign's central conflict",
                "impact_on_resolution": "Determines emotional stakes for Layer C",
                "arc_beat_gate": "2b",
                "force_reveal_on_beat": None,
            },
            {
                "id": "i004",
                "title": "The Hidden Truth",
                "type": "mystery",
                "layer": "B",
                "parent_intrigue": "i001",
                "unlocks": ["i005"],
                "unlock_condition": "intrigue:i001:clues>=3",
                "reveal_threshold": 3,
                "status": "hidden",
                "introduced_session": 0,
                "central_question": "What is the real motive behind the surface events?",
                "key_actors": npc_names[:3] if npc_names else [],
                "red_herrings": [],
                "revealed_clues": [],
                "unrevealed_clues": [
                    "the connection between the surface mystery and the faction conflict",
                    "who is manipulating whom",
                    "the true beneficiary of the chaos",
                ],
                "answer": "GM should fill this in — the Layer B revelation",
                "resolution_condition": "party understands the true shape of the conflict",
                "impact_on_resolution": "Reveals the Layer C core intrigue",
                "arc_beat_gate": "2b",
                "force_reveal_on_beat": None,
            },
            {
                "id": "i005",
                "title": "The Core Conflict",
                "type": "threat",
                "layer": "C",
                "parent_intrigue": "i004",
                "unlocks": [],
                "unlock_condition": "intrigue:i004:resolved",
                "reveal_threshold": 3,
                "status": "hidden",
                "introduced_session": 0,
                "central_question": "What is the campaign ultimately about?",
                "key_actors": npc_names[:5] if npc_names else [],
                "red_herrings": [],
                "revealed_clues": [],
                "unrevealed_clues": [
                    "the true antagonist's identity",
                    "the antagonist's real goal",
                    "what must be sacrificed to stop them",
                ],
                "answer": "GM should fill this in — the campaign's central truth",
                "resolution_condition": "party confronts the true antagonist",
                "impact_on_resolution": "Determines the campaign ending",
                "arc_beat_gate": "3a",
                "force_reveal_on_beat": {"intrigue": "i005", "clue": "the antagonist reveals themselves"},
            },
        ],
        "plans": _template_plans(npc_names, faction_names),
    }
    return template


def _template_plans(npc_names: list, faction_names: list) -> list:
    """Generate seed plan entries for 3-5 antagonists."""
    plans = []
    antagonists = npc_names[:5] if len(npc_names) >= 3 else ["antagonist_1", "antagonist_2", "antagonist_3"]

    for i, name in enumerate(antagonists[:5], 1):
        npc_id = f"npc:{name}" if not name.startswith("npc:") else name
        plans.append({
            "npc_id": npc_id,
            "name": name.replace("npc:", ""),
            "faction": faction_names[i % len(faction_names)] if faction_names else "independent",
            "archetype": ["schemer", "warrior", "diplomat", "fanatic", "opportunist"][i % 5],
            "intrigue_layer": "C",
            "plan_depth": "full",
            "disposition_toward_party": "hostile" if i <= 2 else "neutral",
            "trust": -1 if i <= 2 else 0,
            "last_advanced": 0,
            "history": [],
            "current_plan": {
                "id": f"p{i:03d}",
                "goal": f"<GM: define {name}'s goal tied to Layer C intrigue>",
                "deadline": "session:20",
                "current_step": 1,
                "steps": [
                    {"id": 1, "action": "establish position and resources",
                     "status": "in_progress", "eta": "session:5"},
                    {"id": 2, "action": "make first move against party interests",
                     "status": "pending", "eta": "session:10", "requires": "step 1 complete"},
                    {"id": 3, "action": "escalate to direct confrontation",
                     "status": "pending", "requires": "step 2 complete"},
                    {"id": 4, "action": "final move — success or failure",
                     "status": "pending", "requires": "step 3 complete"},
                ],
                "resources": ["<resource 1>", "<resource 2>"],
                "failure_condition": "<what would make this plan fail>",
                "adaptation_triggers": [
                    "if party discovers the plan early → escalate timeline",
                    "if party allies with a rival → adapt strategy",
                ],
            },
        })
    return plans


# ── Validation ──────────────────────────────────────────────────────────────

def _validate_generated(data: dict) -> list[str]:
    """Validate the generated structure. Returns list of errors."""
    errors = []
    if "intrigues" not in data:
        errors.append("missing 'intrigues' array")
        return errors
    if "plans" not in data:
        errors.append("missing 'plans' array")

    # Check intrigue layering
    layers = {}
    for intr in data.get("intrigues", []):
        layer = intr.get("layer", "A")
        layers.setdefault(layer, []).append(intr["id"])
        if "id" not in intr or "title" not in intr:
            errors.append(f"intrigue missing id/title: {intr}")
        if intr.get("parent_intrigue"):
            parent_exists = any(i["id"] == intr["parent_intrigue"]
                               for i in data["intrigues"])
            if not parent_exists:
                errors.append(f"intrigue {intr['id']} references missing parent {intr['parent_intrigue']}")

    # Check we have at least layers A and C
    if "A" not in layers:
        errors.append("no Layer A (surface) intrigues generated")
    if "C" not in layers:
        errors.append("no Layer C (core) intrigue generated")

    # Check plans reference valid NPCs
    for plan in data.get("plans", []):
        if "npc_id" not in plan:
            errors.append(f"plan missing npc_id: {plan}")
        if not plan.get("current_plan"):
            errors.append(f"plan for {plan.get('npc_id', '?')} missing current_plan")

    return errors


# ── Output writing ──────────────────────────────────────────────────────────

def _write_output(campaign: str, generated: dict, dry_run: bool) -> None:
    """Write the generated intrigues and plans to campaign files."""
    camp_dir = find_campaign(campaign)

    intrigues_data = {
        "version": 2,
        "intrigues": generated.get("intrigues", []),
    }
    plans_data = {
        "version": 2,
        "npcs": {},
        "factions": {},
    }

    # Convert plans array to npc dict
    for plan in generated.get("plans", []):
        npc_id = plan.get("npc_id", "")
        if not npc_id.startswith("npc:"):
            npc_id = f"npc:{npc_id}"
        plans_data["npcs"][npc_id] = {
            k: v for k, v in plan.items() if k != "npc_id"
        }

    if dry_run:
        print("\n# DRY RUN — would write:")
        print(f"#   {camp_dir}/intrigues.json ({len(intrigues_data['intrigues'])} intrigues)")
        print(f"#   {camp_dir}/plans.json ({len(plans_data['npcs'])} NPC plans)")
        print("\n# Generated structure preview:")
        print(json.dumps(generated, indent=2, ensure_ascii=False)[:3000])
        return

    # Backup existing files if present
    for filename in ("intrigues.json", "plans.json"):
        existing = camp_dir / filename
        if existing.exists():
            backup = camp_dir / f"{filename}.backup-{int(time.time())}"
            shutil.copy2(existing, backup)
            print(f"# backed up existing {filename} → {backup.name}")

    # Write
    intrigues_path = camp_dir / "intrigues.json"
    plans_path = camp_dir / "plans.json"

    intrigues_path.write_text(
        json.dumps(intrigues_data, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )
    plans_path.write_text(
        json.dumps(plans_data, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )

    print(f"\n# OK — wrote:")
    print(f"   {intrigues_path} ({len(intrigues_data['intrigues'])} intrigues)")
    print(f"   {plans_path} ({len(plans_data['npcs'])} NPC plans)")

    # Print layer summary
    layers = {}
    for intr in intrigues_data["intrigues"]:
        layers.setdefault(intr.get("layer", "A"), []).append(intr["id"])
    print(f"\n# Layer summary:")
    for layer in sorted(layers):
        print(f"   Layer {layer}: {len(layers[layer])} intrigue(s) — {', '.join(layers[layer])}")


# ── Commands ────────────────────────────────────────────────────────────────

def cmd_init(args) -> int:
    """Generate intrigues + plans for a campaign."""
    print(f"# intrigue_init — Phase 0 generation")
    print(f"# campaign: {args.campaign}")
    print(f"# backend:  {_backend_status()}")
    print()

    # Gather context
    context = _gather_campaign_context(args.campaign)
    print(f"# context gathered:")
    print(f"   system: {context.get('system', 'unknown')}")
    print(f"   state:  {len(context.get('state_excerpt', ''))} chars")
    print(f"   world:  {len(context.get('world_excerpt', ''))} chars")
    print(f"   npcs:   {len(context.get('npcs_excerpt', ''))} chars")
    if context.get("npcs_full_excerpt"):
        print(f"   npcs-full: {len(context['npcs_full_excerpt'])} chars")
    print()

    # Generate
    if args.template:
        print("# using template fallback (--template flag)")
        generated = _template_generate(context)
    else:
        prompt = _build_prompt(context)
        print(f"# prompt built ({len(prompt)} chars)")
        print(f"# calling LLM backend (timeout: {INTRIGUE_TIMEOUT}s)...")
        raw_output = _call_llm_backend(prompt)

        if raw_output:
            # Strip markdown fences if present
            raw_output = raw_output.strip()
            if raw_output.startswith("```"):
                lines = raw_output.split("\n")
                raw_output = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])

            try:
                generated = json.loads(raw_output)
                print(f"# LLM returned valid JSON ({len(raw_output)} chars)")
            except json.JSONDecodeError as e:
                print(f"# LLM returned invalid JSON: {e}", file=sys.stderr)
                print(f"# falling back to template generator", file=sys.stderr)
                generated = _template_generate(context)
        else:
            print("# no LLM backend available — using template generator")
            generated = _template_generate(context)

    # Validate
    errors = _validate_generated(generated)
    hard_errors = [e for e in errors if not e.startswith("warning")]
    if hard_errors:
        print("\n# VALIDATION ERRORS:", file=sys.stderr)
        for e in hard_errors:
            print(f"  - {e}", file=sys.stderr)
        print("\n# output saved for manual review:", file=sys.stderr)
        print(json.dumps(generated, indent=2, ensure_ascii=False)[:2000], file=sys.stderr)
        return 1

    # Write
    _write_output(args.campaign, generated, args.dry_run)

    if not args.dry_run:
        print("\n# next steps:")
        print(f"#   1. Review {find_campaign(args.campaign)}/intrigues.json")
        print(f"#   2. Fill in 'answer' fields for each intrigue (GM secret)")
        print(f"#   3. Review {find_campaign(args.campaign)}/plans.json")
        print(f"#   4. Fill in 'goal', 'resources', 'failure_condition' for each plan")
        print(f"#   5. Run: python3 scripts/plans.py intrigue-list --campaign {args.campaign} --tree")
        print(f"#   6. At /gm load, the scene_loader will surface active intrigues automatically")
    return 0


def cmd_status(args) -> int:
    """Print backend status without generating."""
    print(f"# intrigue_init backend status")
    print(f"   backend: {_backend_status()}")
    print(f"   timeout: {INTRIGUE_TIMEOUT}s")
    print(f"   model:   {os.environ.get('OTGM_INTRIGUE_MODEL', '(auto)')}")
    return 0


# ── CLI ─────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--campaign", required=True, help="Campaign name")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="Generate intrigues + plans")
    s.add_argument("--dry-run", action="store_true", help="Preview without writing")
    s.add_argument("--template", action="store_true", help="Force template fallback")
    s.add_argument("--backend", default="auto", help="Backend (auto/template)")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("status", help="Check backend availability")
    s.set_defaults(func=cmd_status)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
