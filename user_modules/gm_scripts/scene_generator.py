#!/usr/bin/env python3
"""
scene_generator.py — MANDATORY generative scene outcomes.

Every beat type produces specific content. No template fallbacks.
The generator always pulls:
  - WHO is present (from location default_present + on-scene NPCs)
  - WHAT they want (from NPC plans, arcs, drives)
  - WHAT changed from last session (from scene_index, pressure deltas)
  - WHAT the beat demands they do (beat-type-specific action)

If any source is missing, the generator says so explicitly rather than
outputting a placeholder. "No clue available — the reveal must come from
an NPC confession instead" is more useful than "a truth surfaces."

Usage:
  python3 scene_generator.py --campaign <name> generate \
      --beat reveal --session 15 --location elysium
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from paths import find_campaign


# ── State readers ───────────────────────────────────────────────────────────

def _load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _read(campaign: str, filename: str, default: dict) -> dict | list:
    p = find_campaign(campaign) / filename
    data = _load_json(p, default)
    # Return the inner collection if there's a wrapper key
    for key in ("intrigues", "arcs", "promises", "facts", "npcs", "axes",
                "resources", "questions", "secrets", "locations", "memories", "scenes"):
        if isinstance(data, dict) and key in data and isinstance(data[key], (list, dict)):
            return data[key]
    return data


def _read_intrigues(c): return _read(c, "intrigues.json", {"intrigues": []})
def _read_arcs(c): return _read(c, "character_arcs.json", {"arcs": {}})
def _read_promises(c): return _read(c, "promises.json", {"promises": []})
def _read_facts(c): return _read(c, "epistemology.json", {"facts": []})
def _read_plans(c): return _read(c, "plans.json", {"npcs": {}})
def _read_pressure(c): return _read(c, "pressure.json", {"axes": {}})
def _read_economy(c): return _read(c, "economy.json", {"resources": {}})
def _read_questions(c): return _read(c, "story_questions.json", {"questions": []})
def _read_secrets(c): return _read(c, "secrets.json", {"secrets": []})
def _read_locations(c): return _read(c, "locations.json", {"locations": []})
def _read_scene_index(c): return _read(c, "scene_index.json", {"scenes": []})


def _get_location(campaign: str, loc_id: str) -> dict:
    for loc in _read_locations(campaign):
        if loc.get("id") == loc_id:
            return loc
    return {"id": loc_id, "name": loc_id, "sensory": {}, "mood": "unknown", "default_present": []}


def _get_present_npcs(campaign: str, loc_id: str) -> list[str]:
    """Who is present: location defaults + any NPC with an active plan."""
    loc = _get_location(campaign, loc_id)
    present = list(loc.get("default_present", []))
    # Also include NPCs whose plans are active (they're doing things)
    plans = _read_plans(campaign)
    for npc_id, npc in plans.items():
        if npc.get("current_plan") and npc_id not in present:
            present.append(npc_id)
    return present[:5]  # cap at 5


def _get_npc_wants(campaign: str, npc_id: str) -> str:
    """What does this NPC want right now? From plan goal + arc goal."""
    plans = _read_plans(campaign)
    npc = plans.get(npc_id, {})
    plan = npc.get("current_plan", {})
    goal = plan.get("goal", "")
    if goal:
        return goal

    arcs = _read_arcs(campaign)
    arc = arcs.get(npc_id, {})
    return arc.get("current_goal", "unknown — no active goal")


def _get_npc_name(campaign: str, npc_id: str) -> str:
    plans = _read_plans(campaign)
    npc = plans.get(npc_id, {})
    if npc.get("name"):
        return npc["name"]
    arcs = _read_arcs(campaign)
    arc = arcs.get(npc_id, {})
    return arc.get("name", npc_id)


def _get_last_scene(campaign: str) -> Optional[dict]:
    """Get the most recent scene from the index."""
    scenes = _read_scene_index(campaign)
    if not scenes:
        return None
    return scenes[-1]


def _get_pressure_delta(campaign: str) -> dict:
    """What changed in pressure since last session? Approximated from beat history."""
    # We can't easily diff pressure across sessions without storing history,
    # but we can read current state and identify extremes
    pressure = _read_pressure(campaign)
    high = {k: v for k, v in pressure.items() if v >= 70}
    low = {k: v for k, v in pressure.items() if v <= 15}
    return {"high": high, "low": low, "all": pressure}


def _get_economy_state(campaign: str) -> dict:
    """Read economy and identify scarcities that should gate content."""
    economy = _read_economy(campaign)
    return {
        "all": economy,
        "trust": economy.get("trust", 50),
        "hope": economy.get("hope", 50),
        "fear": economy.get("fear", 50),
        "information": economy.get("information", 50),
        "chaos": economy.get("chaos", 50),
        "trust_broken": economy.get("trust", 50) <= 10,
        "hope_crushed": economy.get("hope", 50) <= 10,
        "fear_dominant": economy.get("fear", 50) >= 80,
    }


# ── Hook finders ────────────────────────────────────────────────────────────

def _find_clue(campaign: str) -> Optional[dict]:
    for intr in _read_intrigues(campaign):
        if intr.get("status") == "active" and intr.get("unrevealed_clues"):
            return {"intrigue_id": intr["id"], "title": intr.get("title", "?"),
                    "clue": intr["unrevealed_clues"][0],
                    "question": intr.get("central_question", "?")}
    return None


def _find_stale_arc(campaign: str, beat_history_len: int) -> Optional[dict]:
    if beat_history_len < 3:
        return None
    for npc_id, arc in _read_arcs(campaign).items():
        current = arc.get("current_stage", 0)
        stages = arc.get("arc_stages", [])
        for t in arc.get("stage_transitions", []):
            if t.get("triggered_session") is not None:
                continue
            if current < len(stages) and t.get("from") == stages[current]:
                return {"npc_id": npc_id, "name": arc.get("name", npc_id),
                        "from": t.get("from"), "to": t.get("to"),
                        "trigger": t.get("trigger"),
                        "goal": arc.get("current_goal", "?"),
                        "weakness": arc.get("weakness", "?"),
                        "fear": arc.get("fear", "?")}
    return None


def _find_critical_promise(campaign: str) -> Optional[dict]:
    for p in _read_promises(campaign):
        if p.get("status") in ("open", "strengthening") and p.get("strength", 0) >= 60:
            return p
    return None


def _find_epistemic_gap(campaign: str) -> Optional[dict]:
    for fact in _read_facts(campaign):
        for npc_id, belief in fact.get("npc_beliefs", {}).items():
            if belief.get("accuracy") in ("mistaken", "suspects"):
                return {"fact_id": fact["id"], "truth": fact.get("truth", "?"),
                        "npc_id": npc_id, "belief": belief.get("belief", "?"),
                        "accuracy": belief.get("accuracy"),
                        "player_knowledge": fact.get("player_knowledge", "unknown")}
    return None


def _find_completed_plan_step(campaign: str, session: int) -> Optional[dict]:
    for npc_id, npc in _read_plans(campaign).items():
        plan = npc.get("current_plan")
        if not plan:
            continue
        steps = plan.get("steps", [])
        current = plan.get("current_step", 1)
        if current > 1 and current - 2 < len(steps):
            prev = steps[current - 2]
            if prev.get("completed_session") in (session, session - 1):
                return {"npc_id": npc_id, "name": npc.get("name", npc_id),
                        "action": prev.get("action", "?"),
                        "step": current - 1,
                        "goal": plan.get("goal", "?")}
    return None


def _find_active_npc_plan(campaign: str, npc_id: str) -> Optional[dict]:
    plans = _read_plans(campaign)
    npc = plans.get(npc_id, {})
    plan = npc.get("current_plan")
    if not plan:
        return None
    steps = plan.get("steps", [])
    current = plan.get("current_step", 1)
    if current <= len(steps):
        return {"action": steps[current - 1].get("action", "?"),
                "step": current, "goal": plan.get("goal", "?")}
    return None


def _find_pressing_question(campaign: str) -> Optional[dict]:
    qs = [q for q in _read_questions(campaign) if q.get("status") in ("open", "escalating")]
    if not qs:
        return None
    qs.sort(key=lambda q: q.get("current_pressure", 0), reverse=True)
    return qs[0]


def _find_secret(campaign: str) -> Optional[dict]:
    for s in _read_secrets(campaign):
        if s.get("status") in ("hidden", "suspected"):
            return s
    return None


# ── MANDATORY generative beat generators ────────────────────────────────────
# Every generator MUST produce specific content. If a hook is missing,
# the generator says what's missing and constructs a scene from available
# materials (who's present, what they want, what the beat demands).

def _base_context(campaign: str, session: int, location: str) -> dict:
    """Build the shared context every generator uses."""
    loc = _get_location(campaign, location)
    present = _get_present_npcs(campaign, location)
    economy = _get_economy_state(campaign)
    pressure = _get_pressure_delta(campaign)
    last_scene = _get_last_scene(campaign)

    npc_info = []
    for npc_id in present[:3]:
        npc_info.append({
            "id": npc_id,
            "name": _get_npc_name(campaign, npc_id),
            "wants": _get_npc_wants(campaign, npc_id),
            "plan": _find_active_npc_plan(campaign, npc_id),
        })

    return {
        "location": loc,
        "present_npcs": npc_info,
        "economy": economy,
        "pressure": pressure,
        "last_scene": last_scene,
        "session": session,
    }


def _format_present(ctx: dict) -> str:
    """Format who's present and what they want — always available."""
    lines = []
    for npc in ctx["present_npcs"]:
        wants = npc["wants"]
        plan = npc.get("plan", {})
        plan_str = f" (current step: {plan.get('action', '?')})" if plan else ""
        lines.append(f"{npc['name']} wants: {wants}{plan_str}")
    return "; ".join(lines) if lines else "no named NPCs present"


def _format_atmosphere(ctx: dict) -> str:
    loc = ctx["location"]
    sensory = loc.get("sensory", {})
    parts = []
    if sensory.get("smell"): parts.append(f"smell of {sensory['smell']}")
    if sensory.get("sound"): parts.append(f"sound of {sensory['sound']}")
    return ", ".join(parts) if parts else "the ambient atmosphere"


def _format_what_changed(ctx: dict) -> str:
    """What changed since last session — from pressure extremes + last scene."""
    pressure = ctx["pressure"]
    last = ctx.get("last_scene")
    parts = []
    if last:
        parts.append(f"last session was a {last.get('outcome', '?')} at {last.get('location', '?')}")
    if pressure["high"]:
        parts.append(f"pressure spiking: {', '.join(f'{k}={v}' for k,v in pressure['high'].items())}")
    if pressure["low"]:
        parts.append(f"pressure flat: {', '.join(f'{k}={v}' for k,v in pressure['low'].items())}")
    eco = ctx["economy"]
    if eco["trust_broken"]:
        parts.append("trust is shattered — no one believes anyone")
    if eco["hope_crushed"]:
        parts.append("hope is gone — despair dominates")
    if eco["fear_dominant"]:
        parts.append("fear is pervasive — everyone is afraid")
    return "; ".join(parts) if parts else "the situation is stable"


def _generate_reveal(c, s, loc, ctx) -> dict:
    clue = _find_clue(c)
    gap = _find_epistemic_gap(c)
    secret = _find_secret(c)
    present = _format_present(ctx)
    atmosphere = _format_atmosphere(ctx)

    if clue:
        return {
            "what_changes": f"The party discovers: {clue['clue']}. This advances '{clue['title']}' — {clue['question']}",
            "whose_position_shifts": f"Whoever is implicated by this clue is now exposed. Present: {present}",
            "which_door_closes": "The party can no longer claim ignorance. They must act on what they know.",
            "what_opens": f"A direct path toward answering: {clue['question']}",
            "scene_prompt": f"At {ctx['location']['name']}, amid the {atmosphere}, the party finds: {clue['clue']}. {present}. The discovery implicates someone — the question is who.",
            "mechanical_changes": [f"intrigue {clue['intrigue_id']}: reveal clue"],
        }
    elif gap:
        return {
            "what_changes": f"{gap['npc_id']}'s belief is wrong. Truth: {gap['truth']}. They believe: {gap['belief']}",
            "whose_position_shifts": f"{_get_npc_name(c, gap['npc_id'])} has been acting on false information. Their strategy collapses when this surfaces.",
            "which_door_closes": "The NPC's current plan is invalidated — it was built on a false assumption.",
            "what_opens": "The party can exploit the wrong belief or correct it for leverage.",
            "scene_prompt": f"At {ctx['location']['name']}, evidence surfaces proving {_get_npc_name(c, gap['npc_id'])} is wrong about: {gap['truth']}. They thought: {gap['belief']}. {present}. The confrontation is inevitable.",
            "mechanical_changes": [f"epistemic fact {gap['fact_id']}: player knowledge increases"],
        }
    elif secret:
        return {
            "what_changes": f"The secret '{secret.get('secret', '?')[:80]}' begins to surface. Owner: {secret.get('owner', '?')}",
            "whose_position_shifts": f"{secret.get('owner', 'The owner')} loses their hidden advantage.",
            "which_door_closes": "The secret can no longer be kept perfectly — someone knows.",
            "what_opens": "The party gains leverage over the secret's owner.",
            "scene_prompt": f"At {ctx['location']['name']}, a thread surfaces connecting to: {secret.get('secret', 'a hidden truth')[:80]}. {present}. The evidence is partial but damning.",
            "mechanical_changes": [f"secret {secret.get('id', '?')}: suspicion increases"],
        }
    else:
        # NO HOOK — be explicit about it and build from available materials
        return {
            "what_changes": f"No unrevealed clue or epistemic gap available. The reveal must come from an NPC confession or a discovered document. {present}.",
            "whose_position_shifts": f"Someone present knows something they haven't shared. The scene should pressure them.",
            "which_door_closes": "The party's current theory is either confirmed or disproven by what emerges.",
            "what_opens": "A new investigative thread based on what the NPC reveals under pressure.",
            "scene_prompt": f"At {ctx['location']['name']}, amid the {atmosphere}, the party confronts someone who knows more than they've said. {_format_what_changed(ctx)}. {present}. The pressure of the scene forces a partial truth into the open.",
            "mechanical_changes": [],
        }


def _generate_resolution(c, s, loc, ctx) -> dict:
    promise = _find_critical_promise(c)
    question = _find_pressing_question(c)
    present = _format_present(ctx)
    atmosphere = _format_atmosphere(ctx)

    if promise:
        return {
            "what_changes": f"The promise '{promise['promise']}' is FULFILLED. The answer arrives definitively.",
            "whose_position_shifts": "The party's trust in the narrative is restored. The GM delivered on a commitment.",
            "which_door_closes": f"This thread is closed. '{promise['promise']}' can no longer be an open question.",
            "what_opens": "The answer's implications create a new question.",
            "scene_prompt": f"At {ctx['location']['name']}, the party receives the answer to: {promise['promise']}. {present}. The fulfillment is specific — not a hint, but a definitive revelation.",
            "mechanical_changes": [f"promise {promise['id']}: fulfill"],
        }
    elif question:
        return {
            "what_changes": f"'{question['question']}' advances. Pressure was {question.get('current_pressure', 0)}. A partial answer arrives.",
            "whose_position_shifts": f"The answer implicates or exonerates someone. {present}",
            "which_door_closes": "One possible answer is eliminated. The field narrows.",
            "what_opens": "The partial resolution points toward the next layer.",
            "scene_prompt": f"At {ctx['location']['name']}, the party makes concrete progress on: {question['question']}. {_format_what_changed(ctx)}. {present}. This narrows the possibilities — not a vague clue, but a step that eliminates an option.",
            "mechanical_changes": [f"story question {question['id']}: pressure -20"],
        }
    else:
        return {
            "what_changes": f"No critical promise or pressing question to resolve. The resolution must come from a confrontation that settles a dispute. {present}.",
            "whose_position_shifts": "A conflict between present NPCs reaches a breaking point and is decided.",
            "which_door_closes": "The losing side's position is no longer tenable.",
            "what_opens": "The winner's agenda advances, creating new complications.",
            "scene_prompt": f"At {ctx['location']['name']}, a dispute reaches its climax. {_format_what_changed(ctx)}. {present}. Someone wins and someone loses — the outcome is final.",
            "mechanical_changes": [],
        }


def _generate_reversal(c, s, loc, ctx) -> dict:
    gap = _find_epistemic_gap(c)
    plan_step = _find_completed_plan_step(c, s)
    present = _format_present(ctx)
    atmosphere = _format_atmosphere(ctx)
    changed = _format_what_changed(ctx)

    if plan_step:
        return {
            "what_changes": f"{plan_step['name']} has completed: '{plan_step['action']}'. The party discovers the consequences.",
            "whose_position_shifts": f"{plan_step['name']} gains ground toward: {plan_step['goal']}. The party loses initiative.",
            "which_door_closes": f"The party can no longer prevent: {plan_step['action']}. It's done.",
            "what_opens": "The completed action creates new evidence or danger the party must now address.",
            "scene_prompt": f"At {ctx['location']['name']}, the party discovers that {plan_step['name']} has executed: {plan_step['action']}. {changed}. {present}. The consequences are visible and immediate — this is not a rumor.",
            "mechanical_changes": [f"plan step {plan_step['step']} for {plan_step['npc_id']} intersects play"],
        }
    elif gap:
        return {
            "what_changes": f"What the party assumed was true is false. {_get_npc_name(c, gap['npc_id'])} was wrong about: {gap['truth']}",
            "whose_position_shifts": f"{_get_npc_name(c, gap['npc_id'])}'s entire strategy was built on: {gap['belief']}. It collapses.",
            "which_door_closes": "The NPC's current plan is dead. They must adapt or be destroyed.",
            "what_opens": "The party can now exploit the wrong assumption — or correct it and gain an ally.",
            "scene_prompt": f"At {ctx['location']['name']}, proof surfaces that {_get_npc_name(c, gap['npc_id'])} is wrong. Truth: {gap['truth']}. Their belief: {gap['belief']}. {present}. The reversal reframes everything they've done.",
            "mechanical_changes": [f"epistemic fact {gap['fact_id']}: player knowledge → suspected"],
        }
    else:
        # Build from NPC conflicts — who present wants what, and how does it clash?
        npcs = ctx["present_npcs"]
        if len(npcs) >= 2:
            n1, n2 = npcs[0], npcs[1]
            return {
                "what_changes": f"What {n1['name']} told the party contradicts what {n2['name']} said. One of them is lying.",
                "whose_position_shifts": f"{n1['name']} wants: {n1['wants']}. {n2['name']} wants: {n2['wants']}. Their interests are incompatible — one must lose.",
                "which_door_closes": "The party can no longer trust both. Choosing one means betraying the other.",
                "what_opens": "The exposed lie creates a leverage opportunity — whoever was lying is now vulnerable.",
                "scene_prompt": f"At {ctx['location']['name']}, amid the {atmosphere}, {n1['name']} and {n2['name']} give contradictory accounts. {changed}. The party must determine who is telling the truth — and both are present.",
                "mechanical_changes": [],
            }
        return {
            "what_changes": f"A reversal occurs — but from NPC conflict, not state hooks. {present}. The party's working assumption is proven wrong by someone present.",
            "whose_position_shifts": "The NPC who was lying or mistaken loses credibility.",
            "which_door_closes": "The party's current plan, based on the false assumption, is now void.",
            "what_opens": "The truth creates a new path, but also a new enemy.",
            "scene_prompt": f"At {ctx['location']['name']}, the party's key assumption is proven wrong. {changed}. {present}. Someone present has been deceiving them — the reversal is personal, not abstract.",
            "mechanical_changes": [],
        }


def _generate_twist(c, s, loc, ctx) -> dict:
    return _generate_reversal(c, s, loc, ctx)  # twist and reversal share hooks


def _generate_loss(c, s, loc, ctx) -> dict:
    plan_step = _find_completed_plan_step(c, s)
    present = _format_present(ctx)
    atmosphere = _format_atmosphere(ctx)
    changed = _format_what_changed(ctx)
    eco = ctx["economy"]

    if plan_step and any(w in plan_step["action"].lower() for w in ("plant", "frame", "turn", "assassin", "destroy", "seize")):
        return {
            "what_changes": f"{plan_step['name']} executes: {plan_step['action']}. The party loses something concrete.",
            "whose_position_shifts": f"{plan_step['name']} gains. The party loses standing, a resource, or a safe haven.",
            "which_door_closes": "What was lost cannot be recovered through the same path.",
            "what_opens": "The loss creates urgency — the party must act before the next loss.",
            "scene_prompt": f"At {ctx['location']['name']}, the party suffers: {plan_step['action']}. {changed}. {present}. The consequences are specific — a contact is gone, a haven is compromised, or evidence points at them.",
            "mechanical_changes": [f"NPC {plan_step['npc_id']} plan step intersects play"],
        }
    elif eco["hope_crushed"]:
        return {
            "what_changes": f"Hope is already at {eco['hope']}. The loss deepens the despair — someone the party relied on gives up or turns away.",
            "whose_position_shifts": "An ally withdraws support. The party is more alone.",
            "which_door_closes": "The ally's resources (information, safe haven, political cover) are no longer available.",
            "what_opens": "The party must find a new path without the ally's help.",
            "scene_prompt": f"At {ctx['location']['name']}, hope at {eco['hope']}, the party loses an ally's support. {changed}. {present}. The withdrawal is not a misunderstanding — it's a deliberate choice born of fear or self-preservation.",
            "mechanical_changes": ["economy.hope -10"],
        }
    else:
        return {
            "what_changes": f"A loss occurs — built from current scarcities. {changed}. {present}.",
            "whose_position_shifts": "The party loses something they were counting on: a contact, a safe location, or a piece of evidence.",
            "which_door_closes": "The lost resource cannot be replaced through the same channel.",
            "what_opens": "The loss forces the party to seek new allies or new approaches.",
            "scene_prompt": f"At {ctx['location']['name']}, amid the {atmosphere}, the party loses something concrete. {changed}. {present}. The loss is specific — name what is taken and who takes it.",
            "mechanical_changes": [],
        }


def _generate_choice(c, s, loc, ctx) -> dict:
    arc = _find_stale_arc(c, len(ctx.get("last_scene", {}) and [1] or []))
    present = _format_present(ctx)
    atmosphere = _format_atmosphere(ctx)
    changed = _format_what_changed(ctx)

    # Try to find any pending arc, not just stale ones
    if not arc:
        for npc_id, a in _read_arcs(c).items():
            current = a.get("current_stage", 0)
            stages = a.get("arc_stages", [])
            for t in a.get("stage_transitions", []):
                if t.get("triggered_session") is not None:
                    continue
                if current < len(stages) and t.get("from") == stages[current]:
                    arc = {"npc_id": npc_id, "name": a.get("name", npc_id),
                           "from": t.get("from"), "to": t.get("to"),
                           "trigger": t.get("trigger"),
                           "goal": a.get("current_goal", "?"),
                           "weakness": a.get("weakness", "?")}
                    break
            if arc:
                break

    if arc:
        return {
            "what_changes": f"{arc['name']} confronts a turning point: {arc['from']} → {arc['to']}. Trigger: {arc['trigger']}",
            "whose_position_shifts": f"{arc['name']}'s arc advances. Current goal ({arc['goal']}) is now in question. Weakness ({arc['weakness']}) is exposed.",
            "which_door_closes": f"The '{arc['from']}' phase is over. {arc['name']} cannot go back.",
            "what_opens": f"The '{arc['to']}' phase begins. New behaviors, new betrayals become possible.",
            "scene_prompt": f"At {ctx['location']['name']}, {arc['name']} reaches a breaking point. Trigger: {arc['trigger']}. {changed}. {present}. The party's actions determine whether the transition is clean or violent.",
            "mechanical_changes": [f"arc {arc['npc_id']}: advance {arc['from']} → {arc['to']}"],
        }
    else:
        # No arc pending — build from NPC conflict
        npcs = ctx["present_npcs"]
        if len(npcs) >= 2:
            n1, n2 = npcs[0], npcs[1]
            return {
                "what_changes": f"The party must choose between {n1['name']} and {n2['name']}. {n1['name']} wants: {n1['wants']}. {n2['name']} wants: {n2['wants']}.",
                "whose_position_shifts": f"Choosing {n1['name']} means betraying {n2['name']}, and vice versa.",
                "which_door_closes": "The unchosen NPC becomes an enemy or a lost resource.",
                "what_opens": "The chosen NPC owes the party — but the debt has strings.",
                "scene_prompt": f"At {ctx['location']['name']}, the party faces a choice between {n1['name']} ({n1['wants']}) and {n2['name']} ({n2['wants']}). {changed}. Both are present. The decision must be made now — delay means losing both.",
                "mechanical_changes": [],
            }
        return {
            "what_changes": f"A choice must be made. {present}. The decision closes one door and opens another.",
            "whose_position_shifts": "Whoever the party chooses gains; the other loses.",
            "which_door_closes": "The unchosen option is gone.",
            "what_opens": "The chosen path creates new obligations.",
            "scene_prompt": f"At {ctx['location']['name']}, the party faces a decision. {changed}. {present}. Both options have costs — the choice is not between good and bad, but between two kinds of bad.",
            "mechanical_changes": [],
        }


def _generate_escalation(c, s, loc, ctx) -> dict:
    pressure = ctx["pressure"]
    present = _format_present(ctx)
    atmosphere = _format_atmosphere(ctx)
    changed = _format_what_changed(ctx)

    if pressure["high"].get("violence", 0) >= 70:
        return {
            "what_changes": f"Violence is already at {pressure['high']['violence']}. It escalates to a new level — someone dies, or a faction makes a military move.",
            "whose_position_shifts": "The faction that initiates gains territory or eliminates a rival.",
            "which_door_closes": "The violence cannot be de-escalated through diplomacy. Blood demands blood.",
            "what_opens": "The escalation creates a power vacuum or a refugee crisis.",
            "scene_prompt": f"At {ctx['location']['name']}, violence explodes. {changed}. {present}. Someone dies — not a nameless NPC, but someone the party knows. The death is specific and consequential.",
            "mechanical_changes": ["pressure.violence +15", "economy.hope -10"],
        }
    else:
        return {
            "what_changes": f"Violence erupts where there was none. {changed}. {present}.",
            "whose_position_shifts": "The party loses the luxury of operating in shadows. The Camarilla notices.",
            "which_door_closes": "Quiet investigation is no longer an option. The situation is public.",
            "what_opens": "The violence draws new attention — allies and enemies alike.",
            "scene_prompt": f"At {ctx['location']['name']}, violence breaks out. {changed}. {present}. Blood is spilled and the Masquerade strains. The party is caught in the crossfire or must pick a side.",
            "mechanical_changes": ["pressure.violence +20", "pressure.masquerade +10"],
        }


def _generate_complication(c, s, loc, ctx) -> dict:
    present = _format_present(ctx)
    atmosphere = _format_atmosphere(ctx)
    changed = _format_what_changed(ctx)
    question = _find_pressing_question(c)

    if question:
        return {
            "what_changes": f"The problem deepens: {question['question']}. A new complication emerges — a faction enters, an old enemy resurfaces, or evidence is destroyed.",
            "whose_position_shifts": f"Someone present benefits from the complication. {present}",
            "which_door_closes": "The party's current approach is no longer sufficient.",
            "what_opens": "The complication demands a new strategy.",
            "scene_prompt": f"At {ctx['location']['name']}, the situation around '{question['question']}' deepens. {changed}. {present}. A new obstacle blocks the party's current path.",
            "mechanical_changes": [],
        }
    return {
        "what_changes": f"A complication arises. {changed}. {present}. The problem is bigger than it first appeared.",
        "whose_position_shifts": "The party's position weakens — a new variable they didn't account for.",
        "which_door_closes": "The simple solution is off the table.",
        "what_opens": "A new approach is needed.",
        "scene_prompt": f"At {ctx['location']['name']}, the situation complicates. {changed}. {present}. The party's plan hits an unforeseen obstacle.",
        "mechanical_changes": [],
    }


def _generate_calm(c, s, loc, ctx) -> dict:
    present = _format_present(ctx)
    atmosphere = _format_atmosphere(ctx)
    eco = ctx["economy"]

    return {
        "what_changes": f"A moment of peace. {present}. Someone says something important — not a clue, but a personal truth.",
        "whose_position_shifts": "A relationship deepens. Trust increases through vulnerability, not transaction.",
        "which_door_closes": "The calm is temporary — the storm is still coming.",
        "what_opens": f"The personal connection creates future leverage. Hope rises from {eco['hope']}.",
        "scene_prompt": f"At {ctx['location']['name']}, amid the {atmosphere}, a quiet conversation happens. {present}. No one is fighting. Someone shares something personal — a fear, a memory, a doubt. The scene is about character, not plot.",
        "mechanical_changes": ["economy.hope +15", "economy.fear -10"],
    }


def _generate_false_victory(c, s, loc, ctx) -> dict:
    present = _format_present(ctx)
    changed = _format_what_changed(ctx)

    return {
        "what_changes": f"The party achieves a goal — but something is wrong. {present}. The victory feels hollow.",
        "whose_position_shifts": "The party gains ground, but the gain is a trap.",
        "which_door_closes": "The party commits to the victory, not realizing it's compromised.",
        "what_opens": "The false victory will be revealed later — the trap springs when it hurts most.",
        "scene_prompt": f"At {ctx['location']['name']}, the party wins — but the win is too easy. {changed}. {present}. Something is off: the enemy gave up too quickly, the evidence was too convenient, or the ally is too eager.",
        "mechanical_changes": ["economy.hope +20", "economy.trust -10"],
    }


# ── Generator registry ──────────────────────────────────────────────────────

BEAT_GENERATORS = {
    "reveal": _generate_reveal,
    "resolution": _generate_resolution,
    "reversal": _generate_reversal,
    "twist": _generate_twist,
    "loss": _generate_loss,
    "choice": _generate_choice,
    "escalation": _generate_escalation,
    "complication": _generate_complication,
    "calm": _generate_calm,
    "false_victory": _generate_false_victory,
}


def cmd_generate(args) -> int:
    ctx = _base_context(args.campaign, args.session, args.location)
    generator = BEAT_GENERATORS.get(args.beat, _generate_complication)
    result = generator(args.campaign, args.session, args.location, ctx)

    print(f"# SCENE GENERATION: {args.beat} at {args.location} (session {args.session})\n")
    print(f"## What changes:")
    print(f"   {result.get('what_changes', '?')}")
    print()
    if result.get("whose_position_shifts"):
        print(f"## Whose position shifts:")
        print(f"   {result['whose_position_shifts']}")
        print()
    if result.get("which_door_closes"):
        print(f"## Which door closes:")
        print(f"   {result['which_door_closes']}")
        print()
    if result.get("what_opens"):
        print(f"## What opens:")
        print(f"   {result['what_opens']}")
        print()
    print(f"## Scene prompt (for LLM renderer):")
    print(f"   {result.get('scene_prompt', '?')}")
    print()
    if result.get("mechanical_changes"):
        print(f"## Mechanical changes:")
        for change in result["mechanical_changes"]:
            print(f"   • {change}")
    print(f"\n## JSON:")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--campaign", required=True)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("generate")
    s.add_argument("--beat", required=True)
    s.add_argument("--session", type=int, default=0)
    s.add_argument("--location", default="unknown")
    s.set_defaults(func=cmd_generate)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
