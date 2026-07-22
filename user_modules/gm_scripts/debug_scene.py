#!/usr/bin/env python3
"""
debug_scene.py — dump all reasoning for the current turn in one place.

Surfaces what the Director, Micro-Director, epistemic filter, and
retrieval layer are doing — so a bad session is diagnosable instead
of mysterious.

Usage:
  python3 debug_scene.py --campaign <name> --session 15 --present "velkyn,sheriff"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from paths import find_campaign, python_executable


def _load(path: Path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def cmd_debug(args) -> int:
    camp = find_campaign(args.campaign)

    print(f"# ═══ DEBUG: SCENE STATE for session {args.session} ═══\n")

    # ── 1. Director reasoning ──
    print("## 1. DIRECTOR RECOMMENDATION")
    import subprocess
    r = subprocess.run(
        [python_executable(), str(Path(__file__).parent / "director.py"),
         "--campaign", args.campaign, "recommend", "--session", str(args.session)],
        capture_output=True, text=True
    )
    if r.returncode == 0 and r.stdout:
        print(r.stdout)
    else:
        print("  (director not available or errored)")
    print()

    # ── 2. Micro-Director state ──
    print("## 2. MICRO-DIRECTOR STATE")
    r = subprocess.run(
        [python_executable(), str(Path(__file__).parent / "micro_director.py"),
         "--campaign", args.campaign, "check"],
        capture_output=True, text=True
    )
    if r.returncode == 0 and r.stdout:
        print(r.stdout)
    else:
        print("  (micro-director not initialized)")
    print()

    # ── 3. Epistemic filter ──
    if args.present:
        present = [n.strip() for n in args.present.split(",") if n.strip()]
        print(f"## 3. EPISTEMIC FILTER (present: {', '.join(present)})")
        r = subprocess.run(
            [python_executable(), str(Path(__file__).parent / "epistemic_filter.py"),
             "--campaign", args.campaign, "filter", "--present", args.present],
            capture_output=True, text=True
        )
        if r.returncode == 0 and r.stdout:
            print(r.stdout)
        else:
            print("  (epistemic filter not available)")
        print()

        # ── 4. What each NPC can say ──
        for npc in present:
            print(f"## 4. WHAT {npc} CAN SAY")
            r = subprocess.run(
                [python_executable(), str(Path(__file__).parent / "epistemic_filter.py"),
                 "--campaign", args.campaign, "what-can-say",
                 "--npc", npc, "--present", args.present],
                capture_output=True, text=True
            )
            if r.returncode == 0 and r.stdout:
                print(r.stdout)
            else:
                print(f"  (no data for {npc})")
            print()
    else:
        print("## 3. EPISTEMIC FILTER (skipped — no --present specified)\n")

    # ── 5. Retrieval paths ──
    print("## 5. RETRIEVAL STATE")
    scene_index = _load(camp / "scene_index.json", {"scenes": []})
    scenes = scene_index.get("scenes", [])
    print(f"  scene_index entries: {len(scenes)}")

    emb_path = camp / "scene_embeddings.json"
    if emb_path.exists():
        emb_data = _load(emb_path, {"embeddings": []})
        print(f"  semantic embeddings: {len(emb_data.get('embeddings', []))}")
        print(f"  retrieval path: tag-first → semantic fallback")
    else:
        print(f"  semantic embeddings: 0 (not indexed)")
        print(f"  retrieval path: tag-only (deterministic)")
    print()

    # ── 6. Entity state summary ──
    print("## 6. ENTITY STATE SUMMARY")
    es = _load(camp / "entity_state.json", {"entities": {}})
    entities = es.get("entities", {})
    if entities:
        print(f"  {'NPC':<20} {'Relationship':<15} {'Trust':>5} {'Favors':>6} {'Secrets':>7}")
        print(f"  {'-'*20} {'-'*15} {'-'*5} {'-'*6} {'-'*7}")
        for nid, e in sorted(entities.items()):
            name = e.get("name", nid)
            rel = e.get("relationship_to_party", "?")[:15]
            trust = e.get("trust", 0)
            favors = len([f for f in e.get("favors_owed", []) if not f.get("resolved")])
            secrets = len(e.get("known_secrets", []))
            print(f"  {name:<20} {rel:<15} {trust:>5} {favors:>6} {secrets:>7}")
    else:
        print("  (no entities tracked)")
    print()

    # ── 7. Pressure & Economy ──
    print("## 7. PRESSURE & ECONOMY")
    pressure = _load(camp / "pressure.json", {"axes": {}}).get("axes", {})
    economy = _load(camp / "economy.json", {"resources": {}}).get("resources", {})
    if pressure:
        print(f"  Pressure: {', '.join(f'{k}={v}' for k, v in sorted(pressure.items()))}")
    if economy:
        print(f"  Economy:  {', '.join(f'{k}={v}' for k, v in sorted(economy.items()))}")
    print()

    # ── 8. Active intrigues ──
    print("## 8. ACTIVE INTRIGUES")
    intrigues = _load(camp / "intrigues.json", {"intrigues": []}).get("intrigues", [])
    active = [i for i in intrigues if i.get("status") == "active"]
    if active:
        for i in active:
            clues = len(i.get("revealed_clues", []))
            threshold = i.get("reveal_threshold", 0)
            heat = i.get("heat", 0)
            print(f"  {i['id']} [{i.get('layer', '?')}]: {i.get('title', '?')}")
            print(f"    clues: {clues}/{threshold}, heat: {heat}")
    else:
        print("  (no active intrigues)")
    print()

    # ── 9. Pending promises ──
    print("## 9. PROMISES")
    promises = _load(camp / "promises.json", {"promises": []}).get("promises", [])
    open_p = [p for p in promises if p.get("status") in ("open", "strengthening")]
    if open_p:
        for p in open_p:
            print(f"  {p['id']}: [{p.get('status', '?')}] strength={p.get('strength', 0)} — {p.get('promise', '?')[:50]}")
    else:
        print("  (no open promises)")
    print()

    # ── 10. Campaign phase & beat history ──
    print("## 10. CAMPAIGN STATE")
    cs = _load(camp / "campaign_state.json", {})
    print(f"  phase: {cs.get('phase', 'stability')}")
    bh = cs.get("beat_history", [])
    if bh:
        print(f"  beat history ({len(bh)}):")
        for h in bh[-5:]:
            print(f"    s{h.get('session', '?'):>2} {h.get('beat', '?'):<16} [{h.get('texture', '?')}]")
    print()

    print("## END DEBUG DUMP")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--campaign", required=True)
    p.add_argument("--session", type=int, default=0)
    p.add_argument("--present", help="Comma-separated present NPCs for epistemic filter")
    p.add_argument("cmd", nargs="?", default="dump")
    args = p.parse_args(argv)
    return cmd_debug(args)


if __name__ == "__main__":
    sys.exit(main())
