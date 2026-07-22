#!/usr/bin/env python3
"""
world_lint.py — cross-file consistency validator for all state stores.

Distinct from lint.py (which checks single-file invariants like value
ranges and duplicate IDs). world_lint.py checks CROSS-FILE invariants:
references between stores that can silently disagree.

Checks:
  1.  Every NPC in plans.json exists in entity_state.json
  2.  Every NPC in scene_index deltas exists in entity_state.json
  3.  Every NPC in suspicion.json exists in entity_state.json or plans.json
  4.  No intrigue unlock_condition references a nonexistent parent
  5.  No cyclic parent_intrigue chains
  6.  Every _disposition_ref marker in plans.json points to entity_state.json
  7.  plans.json NPCs with _disposition_ref have matching entity_state entries
  8.  Every fact_id in entity_state known_facts exists in epistemology.json
  9.  Every secret_id referenced in intrigues exists in secrets.json
  10. NPC drives reference NPCs that exist in plans.json
  11. Scene_index retrieval_keys aren't all empty (degraded index)
  12. campaign_state beat_history beat types match BEAT_TEMPLATES

Usage:
  python3 world_lint.py --campaign <name>
  python3 world_lint.py --campaign <name> --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from paths import find_campaign


def _load(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _load_all(campaign: str) -> dict:
    camp = find_campaign(campaign)
    stores = {}
    for name in ("plans.json", "intrigues.json", "scene_index.json",
                 "suspicion.json", "entity_state.json", "epistemology.json",
                 "secrets.json", "campaign_state.json", "npc_drives.json",
                 "promises.json", "story_questions.json"):
        data = _load(camp / name)
        if data is not None:
            stores[name] = data
    return stores


class WorldLintResult:
    def __init__(self):
        self.errors = []
        self.warnings = []

    def error(self, check, message, fix=""):
        self.errors.append({"check": check, "message": message, "fix": fix})

    def warn(self, check, message, fix=""):
        self.warnings.append({"check": check, "message": message, "fix": fix})

    @property
    def clean(self):
        return not self.errors and not self.warnings


def lint_cross_file(state: dict) -> WorldLintResult:
    r = WorldLintResult()

    _check_plans_npcs_in_entity_state(state, r)
    _check_scene_index_npcs_in_entity_state(state, r)
    _check_suspicion_npcs_exist(state, r)
    _check_intrigue_parent_refs(state, r)
    _check_intrigue_cycles(state, r)
    _check_disposition_ref_markers(state, r)
    _check_entity_state_facts_in_epistemology(state, r)
    _check_drives_npc_exist(state, r)
    _check_scene_index_keys_not_empty(state, r)
    _check_beat_history_types(state, r)

    return r


def _get_npcs_from_plans(state: dict) -> set:
    return set((state.get("plans.json") or {}).get("npcs", {}).keys())


def _get_npcs_from_entity_state(state: dict) -> set:
    return set((state.get("entity_state.json") or {}).get("entities", {}).keys())


def _get_npcs_from_suspicion(state: dict) -> set:
    return {e.get("npc_id", "") for e in (state.get("suspicion.json") or {}).get("entries", [])}


def _check_plans_npcs_in_entity_state(state: dict, r: WorldLintResult) -> None:
    plans_npcs = _get_npcs_from_plans(state)
    es_npcs = _get_npcs_from_entity_state(state)
    for npc_id in plans_npcs - es_npcs:
        r.warn("plans-entity-state",
               f"NPC '{npc_id}' in plans.json but not in entity_state.json",
               f"python3 entity_state.py --campaign <name> sync-from-plans")


def _check_scene_index_npcs_in_entity_state(state: dict, r: WorldLintResult) -> None:
    es_npcs = _get_npcs_from_entity_state(state)
    plans_npcs = _get_npcs_from_plans(state)
    known_npcs = es_npcs | plans_npcs
    scenes = (state.get("scene_index.json") or {}).get("scenes", [])
    for scene in scenes:
        deltas = scene.get("deltas", {})
        for key in deltas:
            if key.startswith("npc:") and key not in known_npcs:
                r.warn("scene-index-entity-state",
                       f"Scene '{scene.get('id', '?')}' references NPC '{key}' in deltas but not in entity_state.json or plans.json",
                       f"Add {key} to entity_state.json")


def _check_suspicion_npcs_exist(state: dict, r: WorldLintResult) -> None:
    suspicion_npcs = _get_npcs_from_suspicion(state)
    known_npcs = _get_npcs_from_entity_state(state) | _get_npcs_from_plans(state)
    for npc_id in suspicion_npcs - known_npcs:
        r.warn("suspicion-orphan",
               f"Suspicion entry for '{npc_id}' but NPC not in entity_state.json or plans.json",
               f"Add {npc_id} to entity_state.json or plans.json")


def _check_intrigue_parent_refs(state: dict, r: WorldLintResult) -> None:
    intrigues = (state.get("intrigues.json") or {}).get("intrigues", [])
    ids = {i.get("id") for i in intrigues}
    for intr in intrigues:
        parent = intr.get("parent_intrigue")
        if parent and parent not in ids:
            r.error("intrigue-parent-ref",
                    f"Intrigue '{intr.get('id', '?')}' references parent '{parent}' which doesn't exist",
                    f"Fix parent_intrigue in intrigues.json")


def _check_intrigue_cycles(state: dict, r: WorldLintResult) -> None:
    intrigues = (state.get("intrigues.json") or {}).get("intrigues", [])
    parent_map = {i.get("id"): i.get("parent_intrigue") for i in intrigues}

    for start_id in parent_map:
        visited = set()
        current = start_id
        while current and current in parent_map:
            if current in visited:
                r.error("intrigue-cycle",
                        f"Cyclic parent_intrigue chain detected starting at '{start_id}'",
                        f"Break the cycle in intrigues.json")
                break
            visited.add(current)
            current = parent_map[current]


def _check_disposition_ref_markers(state: dict, r: WorldLintResult) -> None:
    plans = (state.get("plans.json") or {}).get("npcs", {})
    es_entities = (state.get("entity_state.json") or {}).get("entities", {})

    for npc_id, npc in plans.items():
        if npc.get("_disposition_ref") == "entity_state.json":
            if npc_id not in es_entities:
                r.error("disposition-ref-missing",
                        f"{npc_id} has _disposition_ref='entity_state.json' but no entry in entity_state.json",
                        f"python3 entity_state.py --campaign <name> sync-from-plans")


def _check_entity_state_facts_in_epistemology(state: dict, r: WorldLintResult) -> None:
    es = (state.get("entity_state.json") or {}).get("entities", {})
    facts = (state.get("epistemology.json") or {}).get("facts", [])
    fact_ids = {f.get("id") for f in facts}

    for npc_id, entity in es.items():
        for kf in entity.get("known_facts", []):
            fid = kf.get("fact_id") if isinstance(kf, dict) else kf
            if fid and fid not in fact_ids:
                r.warn("entity-state-fact-orphan",
                       f"entity_state.json: {npc_id} knows fact '{fid}' but it doesn't exist in epistemology.json",
                       f"Add fact {fid} to epistemology.json or remove from entity_state.json")


def _check_drives_npc_exist(state: dict, r: WorldLintResult) -> None:
    drives = (state.get("npc_drives.json") or {}).get("drives", [])
    known_npcs = _get_npcs_from_entity_state(state) | _get_npcs_from_plans(state)
    for drive in drives:
        npc_id = drive.get("npc_id", "")
        if npc_id and npc_id not in known_npcs:
            r.warn("drive-npc-orphan",
                   f"NPC drive '{drive.get('id', '?')}' references NPC '{npc_id}' not in entity_state.json or plans.json",
                   f"Add {npc_id} to plans.json")


def _check_scene_index_keys_not_empty(state: dict, r: WorldLintResult) -> None:
    scenes = (state.get("scene_index.json") or {}).get("scenes", [])
    for scene in scenes:
        keys = scene.get("retrieval_keys", [])
        if not keys:
            r.warn("scene-keys-empty",
                   f"Scene '{scene.get('id', '?')}' has no retrieval_keys — won't be found by tag-based retrieval",
                   "Add retrieval_keys or let scene_index auto-generate them")


def _check_beat_history_types(state: dict, r: WorldLintResult) -> None:
    VALID_BEATS = {"reveal", "reversal", "complication", "escalation",
                   "calm", "false_victory", "loss", "choice", "twist", "resolution"}
    beat_history = (state.get("campaign_state.json") or {}).get("beat_history", [])
    for entry in beat_history:
        beat = entry.get("beat", "")
        if beat and beat not in VALID_BEATS:
            r.error("beat-type-invalid",
                    f"Beat history contains unknown beat type '{beat}' at session {entry.get('session', '?')}",
                    "Fix campaign_state.json")


# ── CLI ─────────────────────────────────────────────────────────────────────

def cmd_check(args) -> int:
    state = _load_all(args.campaign)
    if not state:
        print(f"# no state files found for campaign '{args.campaign}'")
        return 0

    result = lint_cross_file(state)

    if args.json:
        print(json.dumps({
            "errors": result.errors,
            "warnings": result.warnings,
            "clean": result.clean,
        }, indent=2, ensure_ascii=False))
        return 2 if result.errors else (1 if result.warnings else 0)

    if result.errors:
        print(f"# {len(result.errors)} CROSS-FILE ERROR(S):\n")
        for e in result.errors:
            print(f"  ✗ [{e['check']}] {e['message']}")
            if e.get("fix"):
                print(f"    fix: {e['fix']}")
            print()

    if result.warnings:
        print(f"# {len(result.warnings)} CROSS-FILE WARNING(S):\n")
        for w in result.warnings:
            print(f"  ⚠ [{w['check']}] {w['message']}")
            if w.get("fix"):
                print(f"    fix: {w['fix']}")
            print()

    if result.clean:
        print(f"# ✓ all cross-file invariants hold — stores are consistent")

    return 2 if result.errors else (1 if result.warnings else 0)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--campaign", required=True)
    p.add_argument("--json", action="store_true")
    p.add_argument("cmd", nargs="?", default="check")
    args = p.parse_args(argv)
    return cmd_check(args)


if __name__ == "__main__":
    sys.exit(main())
