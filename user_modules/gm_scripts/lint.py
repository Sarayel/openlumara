#!/usr/bin/env python3
"""
lint.py — session-end sanity checker for all JSON state stores.

Cross-references every campaign JSON file for inconsistencies that compound
over sessions if left unchecked. No LLM calls — pure deterministic checks.

Run at /gm save or /gm end. Outputs a list of warnings and errors, each
with a suggested fix. Exit 0 = clean, exit 1 = warnings, exit 2 = errors.

Checks:
  1.  NPC disposition consistency (plans vs suspicion vs relationships)
  2.  Promise fulfillment vs linked story question status
  3.  Intrigue reveal threshold met but children still hidden
  4.  Epistemic fact "known" but no info_inventory entry
  5.  NPC plan completed but no scene_index entry for intersection
  6.  Story question pressure at 100 but no recent beats addressing it
  7.  Campaign phase doesn't match pressure/economy state
  8.  Beat history contains beats not in BEAT_TEMPLATES
  9.  Character arc "complete" but current_plan still active
  10. Secret status "revealed" but owner still has leverage in relationships
  11. Pressure/economy values outside 0-100 range
  12. Location marked as visited but no scene_index entry for that session
  13. Suspicion entry references NPC not in plans or arcs
  14. Promise strength > 100 or < 0
  15. Duplicate scene IDs in scene_index

Usage:
  python3 lint.py --campaign <name>
  python3 lint.py --campaign <name> --fix    # auto-fix safe issues
  python3 lint.py --campaign <name> --json   # machine-readable output
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from paths import find_campaign


# ── State loading ───────────────────────────────────────────────────────────

def _load(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _load_all(campaign: str) -> dict:
    """Load every JSON state file for the campaign."""
    camp = find_campaign(campaign)
    files = {}
    for name in ("plans.json", "intrigues.json", "scene_index.json",
                 "suspicion.json", "story_questions.json", "promises.json",
                 "epistemology.json", "secrets.json", "campaign_state.json",
                 "pressure.json", "economy.json", "themes.json",
                 "character_arcs.json", "emotional_memory.json",
                 "relationships.json", "locations.json",
                 "info_inventory.json", "npc_drives.json",
                 "character_traits.json", "narrator.json",
                 "director_config.json"):
        data = _load(camp / name)
        if data is not None:
            files[name] = data
    return files


# ── Lint checks ────────────────────────────────────────────────────────────

class LintResult:
    def __init__(self):
        self.errors = []   # must fix before next session
        self.warnings = [] # should fix, won't break
        self.fixed = []    # auto-fixed

    def error(self, check, message, fix=""):
        self.errors.append({"check": check, "message": message, "fix": fix})

    def warn(self, check, message, fix=""):
        self.warnings.append({"check": check, "message": message, "fix": fix})

    @property
    def clean(self):
        return not self.errors and not self.warnings


def _norm_id(name: str) -> str:
    if ":" in name:
        return name
    return f"npc:{name}"


def lint_all(state: dict) -> LintResult:
    """Run all lint checks against the loaded state."""
    result = LintResult()

    _check_npc_disposition(state, result)
    _check_promise_vs_questions(state, result)
    _check_intrigue_thresholds(state, result)
    _check_epistemic_vs_inventory(state, result)
    _check_plan_vs_scene_index(state, result)
    _check_question_pressure(state, result)
    _check_phase_vs_state(state, result)
    _check_beat_history(state, result)
    _check_arc_vs_plan(state, result)
    _check_secret_vs_relationships(state, result)
    _check_value_ranges(state, result)
    _check_location_visits(state, result)
    _check_suspicion_orphans(state, result)
    _check_promise_ranges(state, result)
    _check_duplicate_scenes(state, result)

    return result


# ── Individual checks ──────────────────────────────────────────────────────

def _check_npc_disposition(state: dict, r: LintResult) -> None:
    """1. NPC disposition consistency across stores."""
    plans = state.get("plans.json", {}).get("npcs", {})
    suspicion = state.get("suspicion.json", {}).get("entries", [])

    for npc_id, npc in plans.items():
        disposition = npc.get("disposition_toward_party", "neutral")

        # Check suspicion entries for this NPC
        for entry in suspicion:
            if entry.get("npc_id") == npc_id:
                suspects = entry.get("suspects", {})
                for target, score in suspects.items():
                    if target.startswith("pc:") and score >= 70 and disposition == "friendly":
                        r.warn("npc-disposition",
                               f"{npc_id} is 'friendly' in plans but suspects {target} at {score} in suspicion",
                               f"Update disposition in plans.json or reduce suspicion score")


def _check_promise_vs_questions(state: dict, r: LintResult) -> None:
    """2. Promise fulfilled but linked question still escalating."""
    promises = state.get("promises.json", {}).get("promises", [])
    questions = state.get("story_questions.json", {}).get("questions", [])

    for p in promises:
        if p.get("status") != "fulfilled":
            continue
        # Check if any question references this promise's topic
        p_text = p.get("promise", "").lower()
        for q in questions:
            if q.get("status") in ("escalating", "open"):
                q_text = q.get("question", "").lower()
                # Simple keyword overlap check
                p_words = set(w for w in p_text.split() if len(w) > 4)
                q_words = set(w for w in q_text.split() if len(w) > 4)
                if len(p_words & q_words) >= 2:
                    r.warn("promise-question",
                           f"Promise '{p['promise'][:50]}' is fulfilled but question '{q['question'][:50]}' is still {q['status']}",
                           f"Consider advancing or resolving question {q['id']}")


def _check_intrigue_thresholds(state: dict, r: LintResult) -> None:
    """3. Intrigue reveal threshold met but children still hidden."""
    intrigues = state.get("intrigues.json", {}).get("intrigues", [])

    for intr in intrigues:
        if intr.get("status") != "active":
            continue
        revealed = len(intr.get("revealed_clues", []))
        threshold = intr.get("reveal_threshold", 0)
        if threshold and revealed >= threshold:
            children = [c for c in intrigues
                       if c.get("parent_intrigue") == intr["id"]
                       and c.get("status") in ("hidden", "locked")]
            for child in children:
                # Check if unlock_condition is met
                unlock = child.get("unlock_condition", "")
                if not unlock or "resolved" not in unlock:
                    r.warn("intrigue-threshold",
                           f"Intrigue {intr['id']} met reveal_threshold ({revealed}/{threshold}) but child {child['id']} ({child.get('title', '?')}) is still {child['status']}",
                           f"python3 plans.py --campaign <name> intrigue-unlock --id {child['id']}")


def _check_epistemic_vs_inventory(state: dict, r: LintResult) -> None:
    """4. Epistemic fact 'known' but no info_inventory entry."""
    facts = state.get("epistemology.json", {}).get("facts", [])
    inventory = state.get("info_inventory.json", {}).get("items", [])

    known_facts = {f["id"] for f in facts if f.get("player_knowledge") == "known"}
    inventoried_facts = {item.get("fact_id") for item in inventory}

    for fact_id in known_facts - inventoried_facts:
        r.warn("epistemic-inventory",
               f"Fact {fact_id} is 'known' to players but has no info_inventory entry",
               f"python3 info_inventory.py --campaign <name> add '{{\"fact_id\": \"{fact_id}\", \"known_by\": [\"pc:party\"]}}'")


def _check_plan_vs_scene_index(state: dict, r: LintResult) -> None:
    """5. NPC plan completed but no scene_index entry for intersection."""
    plans = state.get("plans.json", {}).get("npcs", {})
    scenes = state.get("scene_index.json", {}).get("scenes", [])

    for npc_id, npc in plans.items():
        plan = npc.get("current_plan") or npc.get("plan")
        if not plan:
            # Check history for completed plans
            for entry in npc.get("history", []):
                # If plan was completed recently, check if it intersected play
                pass
            continue
        steps = plan.get("steps", [])
        for step in steps:
            if step.get("status") == "complete":
                completed = step.get("completed_session")
                if completed is None:
                    continue
                # Check if any scene_index entry for that session mentions this NPC
                npc_short = npc_id.replace("npc:", "")
                found = any(
                    s.get("session") == completed and
                    (npc_short in str(s.get("participants", "")) or
                     npc_short in str(s.get("outcome_summary", "")))
                    for s in scenes
                )
                if not found and completed > 0:
                    r.warn("plan-intersection",
                           f"{npc_id} plan step '{step.get('action', '?')[:50]}' completed at session {completed} but no scene_index entry mentions it",
                           "The off-screen action may not have intersected play — consider adding a scene_index entry")


def _check_question_pressure(state: dict, r: LintResult) -> None:
    """6. Story question pressure at 100 but no recent beats addressing it."""
    questions = state.get("story_questions.json", {}).get("questions", [])
    beat_history = state.get("campaign_state.json", {}).get("beat_history", [])

    if not beat_history:
        return

    last_5_sessions = set(h.get("session", 0) for h in beat_history[-5:])
    relevant_beats = [h for h in beat_history[-5:] if h.get("beat") in ("reveal", "resolution", "twist")]

    for q in questions:
        if q.get("current_pressure", 0) >= 90 and q.get("status") in ("open", "escalating"):
            if len(relevant_beats) == 0:
                r.warn("question-pressure",
                       f"Story question '{q.get('question', '?')[:50]}' is at pressure {q.get('current_pressure')} but no reveal/resolution/twist beats in last 5 sessions",
                       "The director should prioritize this question or it will break as a promise")


def _check_phase_vs_state(state: dict, r: LintResult) -> None:
    """7. Campaign phase doesn't match pressure/economy state."""
    phase = state.get("campaign_state.json", {}).get("phase", "stability")
    pressure = state.get("pressure.json", {}).get("axes", {})
    economy = state.get("economy.json", {}).get("resources", {})

    high_count = sum(1 for v in pressure.values() if v >= 70)
    critical_count = sum(1 for v in pressure.values() if v >= 80)
    hope = economy.get("hope", 50)

    if phase == "stability" and (high_count >= 2 or hope < 20):
        r.warn("phase-state",
               f"Phase is 'stability' but {high_count} axes ≥70 and hope is {hope} — should be tension or crisis",
               "python3 drama.py --campaign <name> state-advance")
    elif phase == "tension" and (critical_count >= 3 or hope < 10):
        r.warn("phase-state",
               f"Phase is 'tension' but {critical_count} axes ≥80 and hope is {hope} — should be crisis or collapse",
               "python3 drama.py --campaign <name> state-advance")
    elif phase == "collapse" and hope > 50:
        r.warn("phase-state",
               f"Phase is 'collapse' but hope is {hope} — should be reconstruction",
               "python3 drama.py --campaign <name> state-advance")


def _check_beat_history(state: dict, r: LintResult) -> None:
    """8. Beat history contains beats not in BEAT_TEMPLATES."""
    VALID_BEATS = {"reveal", "reversal", "complication", "escalation",
                   "calm", "false_victory", "loss", "choice", "twist", "resolution"}
    beat_history = state.get("campaign_state.json", {}).get("beat_history", [])

    for entry in beat_history:
        beat = entry.get("beat", "")
        if beat and beat not in VALID_BEATS:
            r.error("beat-history",
                    f"Beat history contains unknown beat type '{beat}' at session {entry.get('session', '?')}",
                    f"Edit campaign_state.json to remove or correct this entry")


def _check_arc_vs_plan(state: dict, r: LintResult) -> None:
    """9. Character arc 'complete' but current_plan still active."""
    arcs = state.get("character_arcs.json", {}).get("arcs", {})
    plans = state.get("plans.json", {}).get("npcs", {})

    for npc_id, arc in arcs.items():
        stages = arc.get("arc_stages", [])
        current = arc.get("current_stage", 0)
        if current >= len(stages):  # arc is complete
            npc_plan = plans.get(npc_id, {})
            if npc_plan.get("current_plan"):
                r.warn("arc-plan",
                       f"{npc_id}'s arc is complete (stage {current}) but still has an active plan: {npc_plan['current_plan'].get('goal', '?')[:50]}",
                       f"python3 plans.py --campaign <name> plan-complete --npc {npc_id} --outcome success --summary 'arc complete'")


def _check_secret_vs_relationships(state: dict, r: LintResult) -> None:
    """10. Secret 'revealed' but owner still has leverage in relationships."""
    secrets = state.get("secrets.json", {}).get("secrets", [])
    relationships = state.get("relationships.json", {}).get("edges", {})

    for secret in secrets:
        if secret.get("status") != "revealed":
            continue
        owner = secret.get("owner", "")
        if not owner:
            continue
        # Check if owner has leverage edges
        for key, edge in relationships.items():
            if key.startswith(f"{owner}->") and edge.get("leverage", 0) >= 50:
                r.warn("secret-leverage",
                       f"Secret '{secret.get('id', '?')}' is revealed but {owner} still has leverage ({edge['leverage']}) in relationship {key}",
                       f"python3 simulation.py --campaign <name> rel-adjust --from-npc {owner.replace('npc:', '')} --to-npc {key.split('->')[1].replace('npc:', '')} --axis leverage --delta -50")


def _check_value_ranges(state: dict, r: LintResult) -> None:
    """11. Pressure/economy values outside 0-100 range."""
    pressure = state.get("pressure.json", {}).get("axes", {})
    economy = state.get("economy.json", {}).get("resources", {})

    for name, val in pressure.items():
        if not isinstance(val, (int, float)):
            r.error("value-range", f"pressure.{name} is not a number: {val}", "Fix in pressure.json")
        elif val < 0 or val > 100:
            r.error("value-range", f"pressure.{name} = {val} (outside 0-100)", f"Clamp to {max(0, min(100, val))}")

    for name, val in economy.items():
        if not isinstance(val, (int, float)):
            r.error("value-range", f"economy.{name} is not a number: {val}", "Fix in economy.json")
        elif val < 0 or val > 100:
            r.error("value-range", f"economy.{name} = {val} (outside 0-100)", f"Clamp to {max(0, min(100, val))}")


def _check_location_visits(state: dict, r: LintResult) -> None:
    """12. Location marked as visited but no scene_index entry for that session."""
    locations = state.get("locations.json", {}).get("locations", [])
    scenes = state.get("scene_index.json", {}).get("scenes", [])

    for loc in locations:
        for visit in loc.get("visits", []):
            session = visit.get("session")
            if session is None:
                continue
            found = any(s.get("session") == session and s.get("location") == loc.get("id") for s in scenes)
            if not found:
                r.warn("location-visit",
                       f"Location '{loc.get('id', '?')}' marked visited at session {session} but no scene_index entry exists for that session/location",
                       "The visit may have been recorded without indexing the scene")


def _check_suspicion_orphans(state: dict, r: LintResult) -> None:
    """13. Suspicion entry references NPC not in plans or arcs."""
    suspicion = state.get("suspicion.json", {}).get("entries", [])
    plans = state.get("plans.json", {}).get("npcs", {})
    arcs = state.get("character_arcs.json", {}).get("arcs", {})

    known_npcs = set(plans.keys()) | set(arcs.keys())

    for entry in suspicion:
        npc_id = entry.get("npc_id", "")
        if npc_id not in known_npcs:
            r.warn("suspicion-orphan",
                   f"Suspicion entry for '{npc_id}' but NPC not in plans.json or character_arcs.json",
                   f"Add {npc_id} to plans or remove from suspicion.json")


def _check_promise_ranges(state: dict, r: LintResult) -> None:
    """14. Promise strength > 100 or < 0."""
    promises = state.get("promises.json", {}).get("promises", [])

    for p in promises:
        strength = p.get("strength", 0)
        if strength > 100:
            r.error("promise-range",
                    f"Promise '{p.get('id', '?')}' has strength {strength} (max 100)",
                    f"Set strength to 100 in promises.json")
        elif strength < 0:
            r.error("promise-range",
                    f"Promise '{p.get('id', '?')}' has strength {strength} (min 0)",
                    f"Set strength to 0 in promises.json")


def _check_duplicate_scenes(state: dict, r: LintResult) -> None:
    """15. Duplicate scene IDs in scene_index."""
    scenes = state.get("scene_index.json", {}).get("scenes", [])
    seen = set()

    for scene in scenes:
        sid = scene.get("id", "")
        if sid in seen:
            r.error("duplicate-scene",
                    f"Duplicate scene ID '{sid}' in scene_index.json",
                    "Remove or rename the duplicate entry")
        seen.add(sid)


# ── Auto-fix ────────────────────────────────────────────────────────────────

def auto_fix(state: dict, result: LintResult, campaign: str) -> None:
    """Auto-fix safe issues: value ranges and promise ranges."""
    camp = find_campaign(campaign)

    # Fix value ranges (clamp pressure/economy)
    if "pressure.json" in state:
        pressure_data = state["pressure.json"]
        axes = pressure_data.get("axes", {})
        changed = False
        for name, val in list(axes.items()):
            if isinstance(val, (int, float)) and (val < 0 or val > 100):
                axes[name] = max(0, min(100, val))
                result.fixed.append(f"Clamped pressure.{name} from {val} to {axes[name]}")
                changed = True
        if changed:
            (camp / "pressure.json").write_text(
                json.dumps(pressure_data, indent=2, ensure_ascii=False), encoding="utf-8")

    if "economy.json" in state:
        economy_data = state["economy.json"]
        resources = economy_data.get("resources", {})
        changed = False
        for name, val in list(resources.items()):
            if isinstance(val, (int, float)) and (val < 0 or val > 100):
                resources[name] = max(0, min(100, val))
                result.fixed.append(f"Clamped economy.{name} from {val} to {resources[name]}")
                changed = True
        if changed:
            (camp / "economy.json").write_text(
                json.dumps(economy_data, indent=2, ensure_ascii=False), encoding="utf-8")

    # Fix promise ranges
    if "promises.json" in state:
        promises_data = state["promises.json"]
        promises = promises_data.get("promises", [])
        changed = False
        for p in promises:
            if p.get("strength", 0) > 100:
                p["strength"] = 100
                result.fixed.append(f"Clamped promise {p.get('id', '?')} strength to 100")
                changed = True
            elif p.get("strength", 0) < 0:
                p["strength"] = 0
                result.fixed.append(f"Clamped promise {p.get('id', '?')} strength to 0")
                changed = True
        if changed:
            (camp / "promises.json").write_text(
                json.dumps(promises_data, indent=2, ensure_ascii=False), encoding="utf-8")


# ── CLI ─────────────────────────────────────────────────────────────────────

def cmd_lint(args) -> int:
    """Run all lint checks."""
    state = _load_all(args.campaign)

    if not state:
        print(f"# no JSON state files found for campaign '{args.campaign}'")
        return 0

    result = lint_all(state)

    # Auto-fix if requested
    if args.fix:
        auto_fix(state, result, args.campaign)

    # JSON output
    if args.json:
        output = {
            "errors": result.errors,
            "warnings": result.warnings,
            "fixed": result.fixed,
            "clean": result.clean,
        }
        print(json.dumps(output, indent=2, ensure_ascii=False))
        return 2 if result.errors else (1 if result.warnings else 0)

    # Human-readable output
    if result.fixed:
        print(f"# {len(result.fixed)} issue(s) auto-fixed:\n")
        for fix in result.fixed:
            print(f"  ✓ {fix}")
        print()

    if result.errors:
        print(f"# {len(result.errors)} ERROR(S) — must fix before next session:\n")
        for e in result.errors:
            print(f"  ✗ [{e['check']}] {e['message']}")
            if e.get("fix"):
                print(f"    fix: {e['fix']}")
            print()

    if result.warnings:
        print(f"# {len(result.warnings)} WARNING(S) — should fix:\n")
        for w in result.warnings:
            print(f"  ⚠ [{w['check']}] {w['message']}")
            if w.get("fix"):
                print(f"    fix: {w['fix']}")
            print()

    if result.clean and not result.fixed:
        print(f"# ✓ all checks passed — campaign state is consistent")
    elif result.clean and result.fixed:
        print(f"# ✓ all checks passed after auto-fix")

    return 2 if result.errors else (1 if result.warnings else 0)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--campaign", required=True, help="Campaign name")
    p.add_argument("--fix", action="store_true", help="Auto-fix safe issues (value clamping)")
    p.add_argument("--json", action="store_true", help="Machine-readable JSON output")
    p.add_argument("cmd", nargs="?", default="check", help="Command (default: check)")

    args = p.parse_args(argv)
    return cmd_lint(args)


if __name__ == "__main__":
    sys.exit(main())
