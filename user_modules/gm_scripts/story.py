#!/usr/bin/env python3
"""
story.py — Story Questions + Secrets subsystem.

Layer above the intrigue engine. Tracks dramatic questions (not quests)
and first-class secret objects that can be stolen, sold, destroyed, or
fabricated.

Story Questions are the dramatic engine's unit of narrative tension.
Instead of "the party needs to find the ledger" (a quest), a story
question asks "Will the Pale Court's finances be exposed?" (a dramatic
question). Every scene advances one or more story questions.

Secrets are first-class objects with owners, evidence, false evidence,
reveal thresholds, and consequences. They move — stolen, sold, destroyed,
fabricated. This is where political games thrive.

Storage:
  <campaign-dir>/story_questions.json
  <campaign-dir>/secrets.json

Usage:
  # Story Questions
  python3 story.py question-add --campaign <name> '<json>'
  python3 story.py question-list --campaign <name> [--status open]
  python3 story.py question-advance --campaign <name> --id q001
  python3 story.py question-answer --campaign <name> --id q001 --answer "..."
  python3 story.py question-pressure --campaign <name> --id q001 --delta 15

  # Secrets
  python3 story.py secret-add --campaign <name> '<json>'
  python3 story.py secret-list --campaign <name> [--owner <npc>] [--status hidden]
  python3 story.py secret-suspect --campaign <name> --id sec001 --npc velkyn --delta 20
  python3 story.py secret-steal --campaign <name> --id sec001 --thief velkyn --session 15
  python3 story.py secret-reveal --campaign <name> --id sec001 --session 15
  python3 story.py secret-destroy --campaign <name> --id sec001 --session 15
  python3 story.py secret-fabricate --campaign <name> '<json>'
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from paths import find_campaign


# ── IO ──────────────────────────────────────────────────────────────────────

def _questions_path(campaign: str) -> Path:
    return find_campaign(campaign) / "story_questions.json"


def _secrets_path(campaign: str) -> Path:
    return find_campaign(campaign) / "secrets.json"


def _load_questions(campaign: str) -> dict:
    p = _questions_path(campaign)
    if not p.exists():
        return {"version": 1, "questions": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "questions": []}
    data.setdefault("version", 1)
    data.setdefault("questions", [])
    return data


def _save_questions(campaign: str, data: dict) -> None:
    p = _questions_path(campaign)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _load_secrets(campaign: str) -> dict:
    p = _secrets_path(campaign)
    if not p.exists():
        return {"version": 1, "secrets": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "secrets": []}
    data.setdefault("version", 1)
    data.setdefault("secrets", [])
    return data


def _save_secrets(campaign: str, data: dict) -> None:
    p = _secrets_path(campaign)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _norm_id(name: str) -> str:
    if ":" in name:
        return name
    return f"npc:{name}"


def _find_question(data: dict, qid: str) -> Optional[dict]:
    for q in data["questions"]:
        if q["id"] == qid:
            return q
    return None


def _find_secret(data: dict, sid: str) -> Optional[dict]:
    for s in data["secrets"]:
        if s["id"] == sid:
            return s
    return None


# ── Story Question commands ─────────────────────────────────────────────────

QUESTION_STATUSES = ("open", "escalating", "answered", "failed")


def cmd_question_add(args) -> int:
    """Add a dramatic question to the campaign."""
    data = _load_questions(args.campaign)
    try:
        q = json.loads(args.json)
    except json.JSONDecodeError as e:
        print(f"error: invalid JSON: {e}", file=sys.stderr)
        return 1

    if "id" not in q or "question" not in q:
        print("error: question must have 'id' and 'question'", file=sys.stderr)
        return 1

    q.setdefault("status", "open")
    q.setdefault("importance", 5)
    q.setdefault("linked_intrigues", [])
    q.setdefault("participants", [])
    q.setdefault("current_pressure", 0)
    q.setdefault("introduced_session", args.session or 0)
    q.setdefault("answer", None)
    q.setdefault("answered_session", None)
    q.setdefault("deadline", None)

    existing_ids = {x["id"] for x in data["questions"]}
    if q["id"] in existing_ids and not args.force:
        print(f"error: question '{q['id']}' already exists. Use --force.", file=sys.stderr)
        return 1

    if q["id"] in existing_ids:
        data["questions"] = [x if x["id"] != q["id"] else q for x in data["questions"]]
    else:
        data["questions"].append(q)

    _save_questions(args.campaign, data)
    print(f"OK — story question '{q['id']}' added")
    print(f"  question: {q['question']}")
    print(f"  importance: {q['importance']}/10")
    print(f"  status: {q['status']}")
    return 0


def cmd_question_list(args) -> int:
    """List dramatic questions."""
    data = _load_questions(args.campaign)
    if not data["questions"]:
        print(f"# no story questions for campaign '{args.campaign}'")
        return 0

    questions = data["questions"]
    if args.status:
        questions = [q for q in questions if q.get("status") == args.status]

    questions.sort(key=lambda q: q.get("importance", 5), reverse=True)

    print(f"# {len(questions)} story question(s)\n")
    print(f"{'ID':<8} {'Status':<12} {'Imp':>3} {'Press':>5} {'Question'}")
    print("-" * 80)
    for q in questions:
        pressure = q.get("current_pressure", 0)
        print(f"{q['id']:<8} {q.get('status', '?'):<12} "
              f"{q.get('importance', 5):>3} {pressure:>5} {q['question']}")
    return 0


def cmd_question_advance(args) -> int:
    """Advance a story question's pressure and potentially its status."""
    data = _load_questions(args.campaign)
    q = _find_question(data, args.id)
    if not q:
        print(f"# question '{args.id}' not found", file=sys.stderr)
        return 1

    old_pressure = q.get("current_pressure", 0)
    old_status = q.get("status", "open")

    # Increase pressure
    q["current_pressure"] = min(100, old_pressure + 15)

    # Auto-escalate if pressure crosses 60
    if q["current_pressure"] >= 60 and q["status"] == "open":
        q["status"] = "escalating"

    _save_questions(args.campaign, data)

    print(f"OK — story question {args.id} advanced")
    print(f"  pressure: {old_pressure} → {q['current_pressure']}")
    if old_status != q["status"]:
        print(f"  status: {old_status} → {q['status']} (auto-escalated)")
    print(f"  question: {q['question']}")

    if q["current_pressure"] >= 80:
        print(f"  ⚠ CRITICAL PRESSURE — this question demands resolution soon")
    return 0


def cmd_question_answer(args) -> int:
    """Answer a story question — resolving it."""
    data = _load_questions(args.campaign)
    q = _find_question(data, args.id)
    if not q:
        print(f"# question '{args.id}' not found", file=sys.stderr)
        return 1

    old_status = q.get("status", "open")
    q["status"] = args.outcome  # "answered" or "failed"
    q["answer"] = args.answer
    q["answered_session"] = args.session

    _save_questions(args.campaign, data)
    print(f"OK — story question {args.id} {args.outcome}")
    print(f"  question: {q['question']}")
    print(f"  answer: {args.answer}")
    print(f"  was: {old_status}")
    return 0


def cmd_question_pressure(args) -> int:
    """Adjust a story question's pressure by a delta."""
    data = _load_questions(args.campaign)
    q = _find_question(data, args.id)
    if not q:
        print(f"# question '{args.id}' not found", file=sys.stderr)
        return 1

    old = q.get("current_pressure", 0)
    q["current_pressure"] = max(0, min(100, old + args.delta))

    if q["current_pressure"] >= 60 and q["status"] == "open":
        q["status"] = "escalating"
    elif q["current_pressure"] < 40 and q["status"] == "escalating":
        q["status"] = "open"

    _save_questions(args.campaign, data)
    print(f"OK — pressure adjusted: {old} → {q['current_pressure']} ({args.delta:+d})")
    print(f"  status: {q['status']}")
    return 0


# ── Secrets commands ────────────────────────────────────────────────────────

SECRET_STATUSES = ("hidden", "suspected", "partially_revealed", "revealed",
                   "destroyed", "fabricated")


def cmd_secret_add(args) -> int:
    """Add a secret to the campaign."""
    data = _load_secrets(args.campaign)
    try:
        s = json.loads(args.json)
    except json.JSONDecodeError as e:
        print(f"error: invalid JSON: {e}", file=sys.stderr)
        return 1

    if "id" not in s or "secret" not in s:
        print("error: secret must have 'id' and 'secret'", file=sys.stderr)
        return 1

    s.setdefault("owner", None)
    s.setdefault("who_suspects", {})
    s.setdefault("evidence", [])
    s.setdefault("false_evidence", [])
    s.setdefault("reveal_threshold", 3)
    s.setdefault("consequences", "")
    s.setdefault("status", "hidden")
    s.setdefault("location", None)
    s.setdefault("created_session", args.session or 0)

    existing_ids = {x["id"] for x in data["secrets"]}
    if s["id"] in existing_ids and not args.force:
        print(f"error: secret '{s['id']}' already exists. Use --force.", file=sys.stderr)
        return 1

    if s["id"] in existing_ids:
        data["secrets"] = [x if x["id"] != s["id"] else s for x in data["secrets"]]
    else:
        data["secrets"].append(s)

    _save_secrets(args.campaign, data)
    print(f"OK — secret '{s['id']}' added")
    print(f"  secret: {s['secret'][:80]}...")
    print(f"  owner: {s.get('owner', 'unknown')}")
    print(f"  status: {s['status']}")
    print(f"  evidence pieces: {len(s.get('evidence', []))}")
    return 0


def cmd_secret_list(args) -> int:
    """List secrets, optionally filtered."""
    data = _load_secrets(args.campaign)
    if not data["secrets"]:
        print(f"# no secrets for campaign '{args.campaign}'")
        return 0

    secrets = data["secrets"]
    if args.owner:
        owner_id = _norm_id(args.owner)
        secrets = [s for s in secrets if s.get("owner") == owner_id]
    if args.status:
        secrets = [s for s in secrets if s.get("status") == args.status]

    print(f"# {len(secrets)} secret(s)\n")
    for s in secrets:
        suspects = s.get("who_suspects", {})
        max_susp = max(suspects.values()) if suspects else 0
        print(f"  {s['id']}: [{s.get('status', '?')}] {s['secret'][:60]}...")
        print(f"    owner: {s.get('owner', '?')}  max suspicion: {max_susp}  "
              f"evidence: {len(s.get('evidence', []))}")
    return 0


def cmd_secret_suspect(args) -> int:
    """Adjust an NPC's suspicion of a secret."""
    data = _load_secrets(args.campaign)
    s = _find_secret(data, args.id)
    if not s:
        print(f"# secret '{args.id}' not found", file=sys.stderr)
        return 1

    npc_id = _norm_id(args.npc)
    suspects = s.setdefault("who_suspects", {})
    old = suspects.get(npc_id, 0)
    suspects[npc_id] = max(0, min(100, old + args.delta))

    # Auto-status transition
    max_susp = max(suspects.values()) if suspects else 0
    if max_susp >= 60 and s["status"] == "hidden":
        s["status"] = "suspected"
    elif max_susp >= 80 and s["status"] == "suspected":
        s["status"] = "partially_revealed"

    _save_secrets(args.campaign, data)
    print(f"OK — suspicion adjusted for {args.id}")
    print(f"  {npc_id}: {old} → {suspects[npc_id]} ({args.delta:+d})")
    print(f"  status: {s['status']}")
    if suspects[npc_id] >= 80:
        print(f"  ⚠ {npc_id} is certain — secret may be extracted or traded")
    return 0


def cmd_secret_steal(args) -> int:
    """A secret is stolen — ownership transfers to the thief."""
    data = _load_secrets(args.campaign)
    s = _find_secret(data, args.id)
    if not s:
        print(f"# secret '{args.id}' not found", file=sys.stderr)
        return 1

    old_owner = s.get("owner", "unknown")
    thief_id = _norm_id(args.thief)
    s["owner"] = thief_id
    s["stolen_session"] = args.session
    s.setdefault("ownership_history", []).append({
        "from": old_owner,
        "to": thief_id,
        "session": args.session,
        "method": "stolen",
    })

    _save_secrets(args.campaign, data)
    print(f"OK — secret {args.id} stolen")
    print(f"  {old_owner} → {thief_id}")
    print(f"  session: {args.session}")
    print(f"  consequence: {thief_id} now has leverage over {old_owner}")
    return 0


def cmd_secret_reveal(args) -> int:
    """A secret is revealed to the public — consequences trigger."""
    data = _load_secrets(args.campaign)
    s = _find_secret(data, args.id)
    if not s:
        print(f"# secret '{args.id}' not found", file=sys.stderr)
        return 1

    s["status"] = "revealed"
    s["revealed_session"] = args.session

    _save_secrets(args.campaign, data)
    print(f"OK — secret {args.id} REVEALED (session {args.session})")
    print(f"  secret: {s['secret']}")
    print(f"  owner: {s.get('owner', '?')}")
    print(f"  consequences: {s.get('consequences', '(unspecified)')}")

    # Check for linked story questions that this resolves
    questions = _load_questions(args.campaign)
    linked = [q for q in questions["questions"]
              if args.id in str(q.get("linked_intrigues", [])) + str(q.get("linked_secrets", []))]
    if linked:
        print(f"\n  linked story questions to consider answering:")
        for q in linked:
            print(f"    - {q['id']}: {q['question']}")
    return 0


def cmd_secret_destroy(args) -> int:
    """A secret is destroyed — evidence eliminated."""
    data = _load_secrets(args.campaign)
    s = _find_secret(data, args.id)
    if not s:
        print(f"# secret '{args.id}' not found", file=sys.stderr)
        return 1

    s["status"] = "destroyed"
    s["destroyed_session"] = args.session
    s["evidence"] = []

    _save_secrets(args.campaign, data)
    print(f"OK — secret {args.id} DESTROYED (session {args.session})")
    print(f"  the secret still exists in the world but can no longer be proven")
    print(f"  owner: {s.get('owner', '?')} is safe unless someone remembers")
    return 0


def cmd_secret_fabricate(args) -> int:
    """Fabricate a false secret — planted misinformation."""
    data = _load_secrets(args.campaign)
    try:
        s = json.loads(args.json)
    except json.JSONDecodeError as e:
        print(f"error: invalid JSON: {e}", file=sys.stderr)
        return 1

    s.setdefault("id", f"sec{len(data['secrets']) + 1:03d}")
    s.setdefault("status", "fabricated")
    s.setdefault("is_false", True)
    s.setdefault("fabricated_by", None)
    s.setdefault("fabricated_session", args.session or 0)
    s.setdefault("who_suspects", {})
    s.setdefault("evidence", [])
    s.setdefault("false_evidence", s.get("evidence", []))
    s.setdefault("reveal_threshold", 3)
    s.setdefault("consequences", "target is framed; truth comes out later")
    s.setdefault("owner", s.get("target"))
    s.setdefault("location", None)
    s.setdefault("created_session", args.session or 0)

    data["secrets"].append(s)
    _save_secrets(args.campaign, data)
    print(f"OK — fabricated secret '{s['id']}' planted")
    print(f"  false secret: {s.get('secret', '?')[:80]}...")
    print(f"  target: {s.get('owner', '?')}")
    print(f"  fabricated by: {s.get('fabricated_by', '?')}")
    print(f"  if revealed, this is MISINFORMATION — the truth is the opposite")
    return 0


# ── CLI ─────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--campaign", required=True, help="Campaign name")
    sub = p.add_subparsers(dest="cmd", required=True)

    # Story Questions
    s = sub.add_parser("question-add", help="Add a dramatic question")
    s.add_argument("json", help="Question JSON")
    s.add_argument("--session", type=int, default=0)
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_question_add)

    s = sub.add_parser("question-list", help="List story questions")
    s.add_argument("--status", choices=QUESTION_STATUSES)
    s.set_defaults(func=cmd_question_list)

    s = sub.add_parser("question-advance", help="Advance a question's pressure")
    s.add_argument("--id", required=True)
    s.set_defaults(func=cmd_question_advance)

    s = sub.add_parser("question-answer", help="Answer (or fail) a story question")
    s.add_argument("--id", required=True)
    s.add_argument("--answer", required=True, help="The answer/outcome")
    s.add_argument("--outcome", required=True, choices=["answered", "failed"])
    s.add_argument("--session", type=int, default=0)
    s.set_defaults(func=cmd_question_answer)

    s = sub.add_parser("question-pressure", help="Adjust a question's pressure")
    s.add_argument("--id", required=True)
    s.add_argument("--delta", type=int, required=True)
    s.set_defaults(func=cmd_question_pressure)

    # Secrets
    s = sub.add_parser("secret-add", help="Add a secret")
    s.add_argument("json", help="Secret JSON")
    s.add_argument("--session", type=int, default=0)
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_secret_add)

    s = sub.add_parser("secret-list", help="List secrets")
    s.add_argument("--owner", help="Filter by owner NPC")
    s.add_argument("--status", choices=SECRET_STATUSES)
    s.set_defaults(func=cmd_secret_list)

    s = sub.add_parser("secret-suspect", help="Adjust NPC suspicion of a secret")
    s.add_argument("--id", required=True)
    s.add_argument("--npc", required=True)
    s.add_argument("--delta", type=int, required=True)
    s.set_defaults(func=cmd_secret_suspect)

    s = sub.add_parser("secret-steal", help="A secret is stolen")
    s.add_argument("--id", required=True)
    s.add_argument("--thief", required=True)
    s.add_argument("--session", type=int, default=0)
    s.set_defaults(func=cmd_secret_steal)

    s = sub.add_parser("secret-reveal", help="A secret is revealed")
    s.add_argument("--id", required=True)
    s.add_argument("--session", type=int, default=0)
    s.set_defaults(func=cmd_secret_reveal)

    s = sub.add_parser("secret-destroy", help="A secret is destroyed")
    s.add_argument("--id", required=True)
    s.add_argument("--session", type=int, default=0)
    s.set_defaults(func=cmd_secret_destroy)

    s = sub.add_parser("secret-fabricate", help="Fabricate a false secret")
    s.add_argument("json", help="Fabricated secret JSON")
    s.add_argument("--session", type=int, default=0)
    s.set_defaults(func=cmd_secret_fabricate)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
