#!/usr/bin/env python3
"""
director.py — the Scene Director.

The Narrative Director layer. This is the "brain" that reads all lower
layers (intrigue, drama, simulation) and recommends what kind of scene
should happen next.

The director evaluates 8 dimensions and selects a beat template that
best addresses current imbalances:

  1. Mystery pressure — too high? → reveal beat; too low? → complication
  2. Violence pressure — too low? → escalation; too high? → calm
  3. Political pressure — stagnant? → complication or twist
  4. Theme alignment — does the dominant theme need reinforcement?
  5. Story question urgency — any escalating? → advance them
  6. Character arc readiness — any NPC at a transition point?
  7. Emotional memory — unaddressed trauma? → calm or choice beat
  8. Campaign phase — what phase are we in? (stability/tension/crisis/...)

Output: a structured recommendation with beat type, reasoning, suggested
participants, and pressure/theme changes to apply.

Usage:
  python3 director.py --campaign <name> recommend
  python3 director.py --campaign <name> recommend --explain
  python3 director.py --campaign <name> status
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from paths import find_campaign


# ── Layer readers ───────────────────────────────────────────────────────────

def _load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _read_pressure(campaign: str) -> dict:
    p = find_campaign(campaign) / "pressure.json"
    data = _load_json(p, {"axes": {}})
    return data.get("axes", {})


def _read_themes(campaign: str) -> dict:
    p = find_campaign(campaign) / "themes.json"
    data = _load_json(p, {"themes": {}})
    return data.get("themes", {})


def _read_economy(campaign: str) -> dict:
    p = find_campaign(campaign) / "economy.json"
    data = _load_json(p, {"resources": {}})
    return data.get("resources", {})


def _read_story_questions(campaign: str) -> list:
    p = find_campaign(campaign) / "story_questions.json"
    data = _load_json(p, {"questions": []})
    return data.get("questions", [])


def _read_arcs(campaign: str) -> dict:
    p = find_campaign(campaign) / "character_arcs.json"
    data = _load_json(p, {"arcs": {}})
    return data.get("arcs", {})


def _read_emotional_memory(campaign: str) -> list:
    p = find_campaign(campaign) / "emotional_memory.json"
    data = _load_json(p, {"memories": []})
    return data.get("memories", [])


def _read_campaign_state(campaign: str) -> dict:
    p = find_campaign(campaign) / "campaign_state.json"
    return _load_json(p, {"phase": "stability"})


def _read_promises(campaign: str) -> list:
    p = find_campaign(campaign) / "promises.json"
    data = _load_json(p, {"promises": []})
    return data.get("promises", [])


def _read_epistemology(campaign: str) -> list:
    p = find_campaign(campaign) / "epistemology.json"
    data = _load_json(p, {"facts": []})
    return data.get("facts", [])


def _read_intrigues(campaign: str) -> list:
    p = find_campaign(campaign) / "intrigues.json"
    data = _load_json(p, {"intrigues": []})
    return data.get("intrigues", [])


# ── Tuning constants (per-campaign configurable via director_config.json) ───
# These control the anti-repetition machinery. Each campaign can override
# any subset by placing a director_config.json in the campaign directory:
#   {
#     "recency_penalty_base": 25,
#     "texture_penalty_base": 15,
#     "source_penalty_base": 10,
#     "hysteresis_margin": 15,
#     "tiebreak_threshold": 2,
#     "phase_age_threshold": 5,
#     "phase_age_boost_cap": 20,
#     "beat_history_limit": 20
#   }

DEFAULT_CONFIG = {
    "recency_penalty_base": 25,      # max penalty for just-used beat type
    "texture_penalty_base": 15,      # max penalty for just-used texture
    "source_penalty_base": 10,       # per-overlapping-source penalty
    "hysteresis_margin": 15,         # min lead to keep a streaking beat
    "tiebreak_threshold": 2,         # near-tie range for seeded RNG
    "phase_age_threshold": 5,        # sessions before phase-age boost kicks in
    "phase_age_boost_cap": 20,       # max phase-age boost
    "beat_history_limit": 20,        # max beats kept in history
}


def _load_config(campaign: str) -> dict:
    """Load director config, merging per-campaign overrides over defaults."""
    p = find_campaign(campaign) / "director_config.json"
    config = dict(DEFAULT_CONFIG)
    if p.exists():
        try:
            overrides = json.loads(p.read_text(encoding="utf-8"))
            config.update(overrides)
        except (json.JSONDecodeError, OSError):
            pass
    return config


# ── Beat templates (mirrors drama.py) ───────────────────────────────────────

# Texture tags — independent of beat type. A reveal delivered through a
# tense interrogation and a reveal delivered through a quiet found letter
# are mechanically the same beat type but feel completely different.
# The director rotates on texture the same way as beat type.
BEAT_TEXTURES = {
    "violent":   "blood, blades, physical danger",
    "quiet":     "stillness, intimacy, low intensity",
    "social":    "dialogue, negotiation, persuasion",
    "physical":  "movement, environment, sensory detail",
    "procedural": "investigation, research, evidence-gathering",
    "ritual":    "ceremony, tradition, the supernatural",
}

# Source categories — what DROVE the recommendation. Tracking per-source
# usage prevents the same source from driving beats repeatedly even when
# the beat label varies. A returning player notices the *shape* of how a
# reveal happens, not just the label.
BEAT_SOURCES = [
    "intrigue",      # clue from an intrigue
    "promise",       # fulfilling a narrative promise
    "epistemic",     # closing an epistemic gap
    "arc",           # NPC character arc transition
    "pressure",      # addressing a pressure imbalance
    "economy",       # restoring a scarce resource
    "theme",         # reinforcing the dominant theme
    "emotional",     # addressing unaddressed trauma
    "phase",         # campaign phase progression
]

BEAT_TEMPLATES = {
    "reveal": {
        "description": "A hidden truth surfaces",
        "pressure_effects": {"mystery": -15, "information": 10},
        "economy_effects": {"information": 15, "fear": -5, "chaos": 5},
        "theme_boost": "identity",
        "best_when": {"mystery": "high"},
        "textures": ["quiet", "procedural", "social"],
    },
    "reversal": {
        "description": "What seemed true was false",
        "pressure_effects": {"political": 10, "chaos": 15},
        "economy_effects": {"trust": -15, "chaos": 20, "fear": 10},
        "theme_boost": "power",
        "best_when": {"trust": "high"},
        "textures": ["social", "violent", "ritual"],
    },
    "complication": {
        "description": "The problem deepens",
        "pressure_effects": {"mystery": 10, "violence": 5},
        "economy_effects": {"fear": 10, "chaos": 5},
        "theme_boost": "decay",
        "best_when": {"mystery": "medium"},
        "textures": ["physical", "social", "violent"],
    },
    "escalation": {
        "description": "Stakes rise",
        "pressure_effects": {"violence": 20, "fear": 15, "political": 10},
        "economy_effects": {"fear": 20, "hope": -10, "power": 10},
        "theme_boost": "power",
        "best_when": {"violence": "low"},
        "textures": ["violent", "physical", "social"],
    },
    "calm": {
        "description": "A moment of peace before the storm",
        "pressure_effects": {"violence": -10, "fear": -5, "personal": 5},
        "economy_effects": {"hope": 15, "fear": -10, "trust": 10},
        "theme_boost": "humanity",
        "best_when": {"violence": "high", "fear": "high"},
        "textures": ["quiet", "social", "physical"],
    },
    "false_victory": {
        "description": "Success that isn't what it seems",
        "pressure_effects": {"hope": 15, "chaos": 10},
        "economy_effects": {"hope": 20, "trust": -10, "chaos": 10},
        "theme_boost": "control",
        "best_when": {"hope": "low"},
        "textures": ["social", "quiet", "ritual"],
    },
    "loss": {
        "description": "Something is taken or destroyed",
        "pressure_effects": {"personal": 15, "violence": 10},
        "economy_effects": {"hope": -20, "fear": 15, "power": -10},
        "theme_boost": "decay",
        "best_when": {"hope": "high"},
        "textures": ["violent", "quiet", "physical"],
    },
    "choice": {
        "description": "A decision with lasting consequences",
        "pressure_effects": {"personal": 10, "political": 5},
        "economy_effects": {"chaos": 5, "power": 5},
        "theme_boost": "identity",
        "best_when": {},
        "textures": ["social", "quiet", "ritual"],
    },
    "twist": {
        "description": "Everything changes",
        "pressure_effects": {"chaos": 20, "mystery": 10, "political": 15},
        "economy_effects": {"chaos": 25, "trust": -10, "fear": 15},
        "theme_boost": "identity",
        "best_when": {"chaos": "low"},
        "textures": ["violent", "ritual", "social"],
    },
    "resolution": {
        "description": "A question is answered, a thread resolves",
        "pressure_effects": {"mystery": -20, "violence": -10, "personal": -10},
        "economy_effects": {"hope": 15, "fear": -10, "chaos": -15, "trust": 10},
        "theme_boost": "humanity",
        "best_when": {"mystery": "high", "chaos": "high"},
        "textures": ["quiet", "social", "ritual"],
    },
}


# ── Analysis ────────────────────────────────────────────────────────────────

def _level(val: int) -> str:
    if val < 20: return "low"
    if val < 40: return "moderate"
    if val < 60: return "medium"
    if val < 80: return "high"
    return "critical"


def _analyze_pressure(pressure: dict) -> dict:
    """Identify pressure imbalances."""
    high = {k: v for k, v in pressure.items() if v >= 70}
    low = {k: v for k, v in pressure.items() if v <= 15}
    return {"high": high, "low": low, "all": pressure}


def _analyze_themes(themes: dict) -> dict:
    """Identify dominant and weak themes."""
    if not themes:
        return {"dominant": None, "weakest": None, "all": {}}
    dominant = max(themes, key=themes.get)
    weakest = min(themes, key=themes.get)
    return {"dominant": dominant, "dominant_score": themes[dominant],
            "weakest": weakest, "weakest_score": themes[weakest], "all": themes}


def _analyze_story_questions(questions: list) -> dict:
    """Identify urgent story questions."""
    escalating = [q for q in questions if q.get("status") == "escalating"]
    high_pressure = [q for q in questions if q.get("current_pressure", 0) >= 70]
    open_questions = [q for q in questions if q.get("status") == "open"]
    return {
        "escalating": escalating,
        "high_pressure": high_pressure,
        "open": open_questions,
        "total": len(questions),
    }


def _analyze_arcs(arcs: dict) -> dict:
    """Find NPCs at arc transition points."""
    transitions_pending = []
    for npc_id, arc in arcs.items():
        current = arc.get("current_stage", 0)
        stages = arc.get("arc_stages", [])
        for t in arc.get("stage_transitions", []):
            if t.get("triggered_session") is not None:
                continue
            if t.get("from") == stages[current] if current < len(stages) else False:
                transitions_pending.append({
                    "npc": arc.get("name", npc_id),
                    "npc_id": npc_id,
                    "from": t.get("from"),
                    "to": t.get("to"),
                    "trigger": t.get("trigger"),
                })
    return {"pending_transitions": transitions_pending}


def _analyze_emotional_state(memories: list) -> dict:
    """Find unaddressed emotional pressure."""
    if not memories:
        return {"unaddressed_trauma": [], "recent_intense": []}

    # Find high-importance memories with high trauma
    unaddressed = [m for m in memories
                   if m.get("trauma", 0) >= 7 or m.get("resentment", 0) >= 7]
    recent_intense = sorted(memories, key=lambda m: m.get("importance", 0), reverse=True)[:5]
    return {"unaddressed_trauma": unaddressed, "recent_intense": recent_intense}


def _analyze_economy(economy: dict) -> dict:
    """Identify resource imbalances."""
    scarce = {k: v for k, v in economy.items() if v <= 15}
    abundant = {k: v for k, v in economy.items() if v >= 80}
    return {"scarce": scarce, "abundant": abundant, "all": economy}


def _analyze_promises(promises: list) -> dict:
    """Identify pressing and at-risk promises."""
    pressing = [p for p in promises
                if p.get("status") in ("open", "strengthening")
                and p.get("strength", 0) >= 50]
    critical = [p for p in pressing if p.get("strength", 0) >= 70]
    broken = [p for p in promises if p.get("status") == "broken"]
    return {
        "pressing": pressing,
        "critical": critical,
        "broken": broken,
        "total": len(promises),
    }


def _analyze_epistemology(facts: list) -> dict:
    """Identify epistemic gaps and player misconceptions."""
    gaps = []
    misconceptions = []

    for fact in facts:
        truth = fact.get("truth", "")
        for npc_id, belief in fact.get("npc_beliefs", {}).items():
            accuracy = belief.get("accuracy", "ignorant")
            if accuracy in ("mistaken", "suspects"):
                gaps.append({
                    "fact_id": fact["id"],
                    "npc_id": npc_id,
                    "truth": truth[:60],
                    "accuracy": accuracy,
                })
        if fact.get("player_knowledge") == "disproven":
            misconceptions.append({
                "fact_id": fact["id"],
                "truth": truth[:60],
            })

    return {
        "gaps": gaps,
        "misconceptions": misconceptions,
        "total_facts": len(facts),
    }


# ── Beat scoring ────────────────────────────────────────────────────────────

def _score_beat(beat_type: str, template: dict, analysis: dict, campaign_phase: str,
                 beat_history: list = None, current_session: int = 0,
                 campaign_name: str = "", config: dict = None) -> tuple:
    """Score how well a beat fits the current state. Higher = better fit.

    Anti-repetition features (applied after base scoring):
      13. Recency penalty — decaying penalty for beat types used recently
      14. Texture rotation — penalty for textures used recently
      15. Source diversity — penalty for sources that drove recent beats
      16. Phase-age boost — transition beats gain score as phase ages

    All tuning constants are configurable via director_config.json.
    """
    if config is None:
        config = DEFAULT_CONFIG

    score = 0
    reasons = []
    sources = []  # which analysis dimensions drove this beat's score

    pressure = analysis["pressure"]
    economy = analysis["economy"]
    themes = analysis["themes"]
    questions = analysis["questions"]
    arcs = analysis["arcs"]
    emotions = analysis["emotions"]

    # 1. Pressure fit — does this beat address a pressure imbalance?
    for axis, effect in template.get("pressure_effects", {}).items():
        if axis == "varies":
            continue
        current = pressure.get(axis, 0)
        if effect < 0 and current >= 70:
            # Beat reduces a high-pressure axis
            score += 20
            reasons.append(f"reduces high {axis} ({current})")
            if "pressure" not in sources:
                sources.append("pressure")
        elif effect > 0 and current <= 15:
            # Beat increases a low-pressure axis
            score += 15
            reasons.append(f"raises low {axis} ({current})")
            if "pressure" not in sources:
                sources.append("pressure")

    # 2. Economy fit
    for res, effect in template.get("economy_effects", {}).items():
        if res == "varies":
            continue
        current = economy.get(res, 50)
        if effect > 0 and current <= 15:
            score += 15
            reasons.append(f"restores scarce {res} ({current})")
            if "economy" not in sources:
                sources.append("economy")
        elif effect < 0 and current >= 80:
            score += 10
            reasons.append(f"drains abundant {res} ({current})")
            if "economy" not in sources:
                sources.append("economy")

    # 3. Theme alignment
    theme_boost = template.get("theme_boost", "")
    if theme_boost and theme_boost != "varies":
        if theme_boost == themes.get("dominant"):
            score += 10
            reasons.append(f"reinforces dominant theme '{theme_boost}'")
            if "theme" not in sources:
                sources.append("theme")

    # 4. Story question urgency
    if questions["escalating"]:
        if beat_type in ("reveal", "resolution", "twist"):
            score += 15
            reasons.append(f"addresses escalating story question")
            if "intrigue" not in sources:
                sources.append("intrigue")

    # 5. Character arc readiness — auto-fire after 3+ sessions pending
    if arcs["pending_transitions"]:
        # Check how long each transition has been pending
        stale_transitions = []
        for t in arcs["pending_transitions"]:
            # If we have beat_history, estimate staleness from session count
            # Arcs don't track when they became pending, so we use a heuristic:
            # if there are 3+ beats in history and the transition hasn't fired,
            # it's stale
            if len(beat_history or []) >= 3:
                stale_transitions.append(t)

        if beat_type in ("choice", "consequence", "reversal"):
            if stale_transitions:
                # STALE: 3+ sessions pending — force confrontation
                score += 35
                reasons.append(f"⚠ {len(stale_transitions)} arc transition(s) pending 3+ sessions — MUST confront")
                if "arc" not in sources:
                    sources.append("arc")
            else:
                score += 12
                reasons.append(f"NPC arc transition pending")
                if "arc" not in sources:
                    sources.append("arc")
        elif stale_transitions and beat_type in ("escalation", "loss", "twist"):
            # Even non-choice beats get a boost when arcs are stale — the
            # situation should force the confrontation
            score += 15
            reasons.append(f"arc pressure forces {beat_type}")
            if "arc" not in sources:
                sources.append("arc")

    # 6. Emotional pressure
    if emotions["unaddressed_trauma"]:
        if beat_type == "calm":
            score += 15
            reasons.append(f"addresses unaddressed trauma")
            if "emotional" not in sources:
                sources.append("emotional")
        elif beat_type == "choice":
            score += 8
            reasons.append(f"emotional choice for traumatized character")
            if "emotional" not in sources:
                sources.append("emotional")

    # 7. Campaign phase alignment
    phase_preferences = {
        "stability": ["complication", "choice", "escalation"],
        "tension": ["escalation", "complication", "twist"],
        "crisis": ["loss", "twist", "resolution"],
        "collapse": ["loss", "resolution", "calm"],
        "reconstruction": ["calm", "resolution", "false_victory"],
    }
    if beat_type in phase_preferences.get(campaign_phase, []):
        score += 8
        reasons.append(f"fits campaign phase '{campaign_phase}'")
        if "phase" not in sources:
            sources.append("phase")

    # 8. Hope management — if hope is critically low, prefer beats that restore it
    hope = economy.get("hope", 50)
    if hope <= 15:
        if beat_type in ("calm", "false_victory", "resolution"):
            score += 15
            reasons.append(f"hope critically low ({hope}) — need relief")
            if "economy" not in sources:
                sources.append("economy")

    # 9. Chaos management — if chaos is too high, prefer resolution
    chaos = economy.get("chaos", 50)
    if chaos >= 80:
        if beat_type in ("resolution", "calm"):
            score += 15
            reasons.append(f"chaos critical ({chaos}) — need order")
            if "economy" not in sources:
                sources.append("economy")

    # 9b. Complication special scoring — "the problem deepens"
    #     complication structurally never wins because it has no special
    #     bonuses (no story question, promise, or epistemic hooks). Give it
    #     scoring paths that reflect its narrative function: introducing new
    #     obstacles when the party is making progress.
    if beat_type == "complication":
        # Complications are valuable when the party has information but hasn't
        # acted on it — they know something but haven't done anything about it
        information = economy.get("information", 50)
        if information >= 40 and chaos <= 50:
            score += 20
            reasons.append(f"party has information ({information}) but hasn't acted — complication forces response")
            if "economy" not in sources:
                sources.append("economy")
        # Complications introduce new factions or obstacles — valuable in
        # stability and tension phases when the situation should be getting worse
        if campaign_phase in ("stability", "tension"):
            mystery = pressure.get("mystery", 0)
            if 20 <= mystery <= 70:
                score += 15
                reasons.append(f"mystery at moderate level ({mystery}) — complication deepens it")
                if "pressure" not in sources:
                    sources.append("pressure")

    # 9c. False victory special scoring — "success that isn't what it seems"
    #     false_victory structurally never wins because it only appears in
    #     the 'reconstruction' phase (which the sim rarely reaches) and has
    #     no special bonuses. Give it scoring paths that reflect its function:
    #     the party achieves a goal but the victory is compromised.
    if beat_type == "false_victory":
        # False victory is valuable when hope is low — the party NEEDS a win,
        # but the win should come with strings attached
        hope = economy.get("hope", 50)
        if hope <= 30:
            score += 25
            reasons.append(f"hope low ({hope}) — party needs a win, but it should be compromised")
            if "economy" not in sources:
                sources.append("economy")
        # False victory is also valuable after a series of losses — the
        # narrative needs a break before the next blow
        if beat_history:
            recent_losses = sum(1 for h in beat_history[-3:] if h.get("beat") == "loss")
            if recent_losses >= 1:
                score += 15
                reasons.append(f"recent loss(es) — false victory provides relief before next blow")
        # Add to more phase preferences (not just reconstruction)
        if campaign_phase in ("tension", "crisis"):
            score += 10
            reasons.append(f"fits phase '{campaign_phase}' — victory with strings")

    # 10. Pressing promises — high-strength promises need fulfillment
    #     HARD CAP: if any promise has strength ≥80, fulfillment beats get
    #     +40 and non-fulfillment beats get -30. This prevents the engine
    #     from selecting twist/resolution forever without actually fulfilling.
    promises = analysis.get("promises", {})
    pressing = promises.get("pressing", [])
    critical = promises.get("critical", [])  # strength ≥70
    if pressing:
        if beat_type in ("reveal", "resolution"):
            score += 18
            reasons.append(f"{len(pressing)} pressing promise(s) need fulfillment")
            if "promise" not in sources:
                sources.append("promise")
        elif beat_type == "twist":
            # Twist can fulfill but only if it actually delivers — smaller boost
            score += 8
            if "promise" not in sources:
                sources.append("promise")

    # HARD CAP: critical promises (strength ≥70) MUST be fulfilled
    if critical:
        if beat_type in ("reveal", "resolution"):
            score += 40
            reasons.append(f"⚠ {len(critical)} CRITICAL promise(s) ≥70 strength — MUST fulfill this session")
            if "promise" not in sources:
                sources.append("promise")
        elif beat_type not in ("twist",):
            # Non-fulfillment beats are penalized when promises are critical
            score -= 30
            reasons.append(f"-30 critical promise unfulfilled — this beat wastes player trust")

    # 11. Epistemic gaps — NPCs with wrong beliefs are drama waiting to happen
    epistemic = analysis.get("epistemic", {})
    gaps = epistemic.get("gaps", [])
    if gaps:
        if beat_type in ("reveal", "reversal", "twist"):
            score += 12
            reasons.append(f"{len(gaps)} epistemic gap(s) — NPCs acting on wrong beliefs")
            if "epistemic" not in sources:
                sources.append("epistemic")

    # 12. Player misconceptions — players believe something false
    misconceptions = epistemic.get("misconceptions", [])
    if misconceptions:
        if beat_type in ("reversal", "twist", "reveal"):
            score += 15
            reasons.append(f"{len(misconceptions)} player misconception(s) — truth will land hard")
            if "epistemic" not in sources:
                sources.append("epistemic")

    # ── Anti-repetition penalties (applied after base scoring) ──────────────

    # 13. Recency penalty — decaying penalty for beat types used recently.
    #     PLUS: hard escalation after 2 uses — the 3rd use of the same beat
    #     type within 4 sessions gets an additional -40 penalty, ensuring
    #     it scores below alternatives regardless of other factors.
    if beat_history:
        recent_uses = 0
        for entry in beat_history:
            if entry.get("beat") == beat_type:
                sessions_since = current_session - entry.get("session", 0)
                if sessions_since >= 0:
                    penalty = int(config["recency_penalty_base"] * (1 / (sessions_since + 1)))
                    if penalty > 0:
                        score -= penalty
                        reasons.append(f"-{penalty} recency (used {sessions_since} session(s) ago)")
                    # Count uses within last 4 sessions
                    if sessions_since <= 4:
                        recent_uses += 1

        # HARD PENALTY: 3+ uses within 4 sessions = -40 additional
        if recent_uses >= 2:
            score -= 40
            reasons.append(f"-40 BEAT SATURATION ({recent_uses} uses in last 4 sessions — must rotate)")

    # 14. Texture rotation — penalty for textures used recently.
    #     A reveal through interrogation and a reveal through a found letter
    #     are mechanically the same beat but feel different. Rotate texture.
    beat_textures = template.get("textures", [])
    if beat_history and beat_textures:
        for entry in beat_history:
            entry_texture = entry.get("texture")
            if entry_texture in beat_textures:
                sessions_since = current_session - entry.get("session", 0)
                if sessions_since >= 0:
                    penalty = int(config["texture_penalty_base"] * (1 / (sessions_since + 1)))
                    if penalty > 0:
                        score -= penalty
                        reasons.append(f"-{penalty} texture '{entry_texture}' used recently")
                        break

    # 15. Source diversity — penalty for sources that drove recent beats.
    #     Prevents the same source (e.g. "intrigue") from driving every beat
    #     even when the beat label varies. A returning player notices the
    #     *shape* of how a reveal happens, not just the label.
    if beat_history and sources:
        for entry in beat_history[-3:]:  # last 3 beats
            entry_sources = entry.get("sources", [])
            overlap = set(sources) & set(entry_sources)
            if overlap:
                sessions_since = current_session - entry.get("session", 0)
                if sessions_since >= 0:
                    penalty = int(config["source_penalty_base"] * len(overlap) * (1 / (sessions_since + 1)))
                    if penalty > 0:
                        score -= penalty
                        reasons.append(f"-{penalty} source overlap ({', '.join(overlap)})")

    # 16. Phase-age boost — transition beats gain score as phase ages.
    #     Prevents the campaign from getting stuck in a locally-optimal but
    #     flat state. If we've been in "tension" for 8 sessions, loss/resolution/
    #     false_victory get boosted to push the campaign forward.
    phase_age = analysis.get("phase_age", 0)
    if phase_age >= config["phase_age_threshold"] and beat_type in ("loss", "resolution", "false_victory", "twist"):
        boost = min(config["phase_age_boost_cap"], phase_age - (config["phase_age_threshold"] - 1))
        score += boost
        reasons.append(f"+{boost} phase-age boost (in '{campaign_phase}' for {phase_age} sessions)")

    # 17. Lifetime-frequency penalty — pulls overused beats toward fair share.
    #     The recency penalty (13) only looks at recent sessions. The saturation
    #     penalty only fires after 2 uses in 4 sessions. But the batch data shows
    #     resolution/twist/reveal consuming 67% of all sessions across 2000 trials.
    #     This penalty looks at the ENTIRE beat history and penalizes beats that
    #     have been used more than their fair share (1/N where N=10 beat types).
    if beat_history:
        total_beats = len(beat_history)
        if total_beats >= 5:  # only after enough history to matter
            beat_count = sum(1 for h in beat_history if h.get("beat") == beat_type)
            fair_share = total_beats / 10.0  # 10 beat types
            if beat_count > fair_share * 1.5:  # 50% over fair share
                overuse = beat_count - fair_share
                penalty = int(min(30, overuse * 3))  # +3 per beat over fair share, cap 30
                score -= penalty
                reasons.append(f"-{penalty} lifetime frequency ({beat_count}/{total_beats} = {beat_count/total_beats*100:.0f}%, fair share 10%)")

    return score, reasons, sources


# ── Recommendation ──────────────────────────────────────────────────────────

def cmd_recommend(args) -> int:
    """Recommend the next scene type based on all layers."""
    import hashlib

    # Gather all state
    pressure = _read_pressure(args.campaign)
    themes = _read_themes(args.campaign)
    economy = _read_economy(args.campaign)
    questions = _read_story_questions(args.campaign)
    arcs = _read_arcs(args.campaign)
    memories = _read_emotional_memory(args.campaign)
    campaign_state = _read_campaign_state(args.campaign)
    promises = _read_promises(args.campaign)
    epistemic_facts = _read_epistemology(args.campaign)

    # Read beat history for anti-repetition penalties
    beat_history = campaign_state.get("beat_history", [])

    # Compute phase age (sessions in current phase)
    phase_entered = campaign_state.get("phase_entered_session", 0)
    current_session = args.session if hasattr(args, "session") and args.session else 0
    phase_age = max(0, current_session - phase_entered) if phase_entered else 0

    # Analyze
    pressure_analysis = _analyze_pressure(pressure)
    theme_analysis = _analyze_themes(themes)
    economy_analysis = _analyze_economy(economy)
    questions_analysis = _analyze_story_questions(questions)
    arcs_analysis = _analyze_arcs(arcs)
    emotions_analysis = _analyze_emotional_state(memories)
    promises_analysis = _analyze_promises(promises)
    epistemic_analysis = _analyze_epistemology(epistemic_facts)

    analysis = {
        "pressure": pressure,
        "themes": theme_analysis,
        "economy": economy,
        "questions": questions_analysis,
        "arcs": arcs_analysis,
        "emotions": emotions_analysis,
        "promises": promises_analysis,
        "epistemic": epistemic_analysis,
        "phase_age": phase_age,
    }

    campaign_phase = campaign_state.get("phase", "stability")

    # Load per-campaign tuning config
    config = _load_config(args.campaign)

    # Score all beats — now returns (beat_type, score, reasons, sources, template)
    beat_scores = []
    for beat_type, template in BEAT_TEMPLATES.items():
        score, reasons, sources = _score_beat(
            beat_type, template, analysis, campaign_phase,
            beat_history=beat_history, current_session=current_session,
            campaign_name=args.campaign, config=config,
        )
        beat_scores.append((beat_type, score, reasons, sources, template))

    # Sort by score descending
    beat_scores.sort(key=lambda x: x[1], reverse=True)

    # Hysteresis: if top beat matches N-1 or N-2 in history, only take it
    # if it beats the next alternative by the configured margin. Prevents streaks.
    HYSTERESIS_MARGIN = config["hysteresis_margin"]
    recent_beats = [h.get("beat") for h in beat_history[-2:]] if beat_history else []

    top_beat, top_score, top_reasons, top_sources, top_template = beat_scores[0]

    if top_beat in recent_beats and len(beat_scores) > 1:
        second_beat, second_score = beat_scores[1][0], beat_scores[1][1]
        if top_score - second_score < HYSTERESIS_MARGIN:
            # Fall through to the alternative — avoid streak
            top_beat, top_score, top_reasons, top_sources, top_template = beat_scores[1]

    # Seeded tiebreaking: if there's a tie (or near-tie within threshold),
    # use a seeded RNG (campaign name + session) to pick deterministically.
    tiebreak_threshold = config["tiebreak_threshold"]
    if len(beat_scores) > 1:
        top_n = [b for b in beat_scores if b[1] >= top_score - tiebreak_threshold]
        if len(top_n) > 1:
            seed_str = f"{args.campaign}:{current_session}"
            seed = int(hashlib.md5(seed_str.encode()).hexdigest(), 16)
            chosen_idx = seed % len(top_n)
            top_beat, top_score, top_reasons, top_sources, top_template = top_n[chosen_idx]

    # Select texture — pick the least-recently-used texture for this beat
    beat_textures = top_template.get("textures", ["social"])
    recent_textures = [h.get("texture") for h in beat_history[-3:]] if beat_history else []
    chosen_texture = None
    for t in beat_textures:
        if t not in recent_textures:
            chosen_texture = t
            break
    if not chosen_texture:
        chosen_texture = beat_textures[0]

    # Build output
    print(f"# ═══ SCENE DIRECTOR RECOMMENDATION ═══\n")

    print(f"## Recommended beat: {top_beat}")
    print(f"   {top_template['description']}")
    print(f"   texture: {chosen_texture} ({BEAT_TEXTURES.get(chosen_texture, '?')})")
    print(f"   confidence: {top_score} points\n")

    if top_reasons:
        print(f"## Why this beat:")
        for r in top_reasons:
            print(f"   • {r}")
        print()

    if top_sources:
        print(f"## Driven by sources: {', '.join(top_sources)}")
        print()

    # Show state summary
    print(f"## Current state summary:")
    print(f"   campaign phase: {campaign_phase}")

    # Pressure
    if pressure:
        high_p = pressure_analysis["high"]
        low_p = pressure_analysis["low"]
        if high_p:
            print(f"   high pressure: {', '.join(f'{k}={v}' for k, v in sorted(high_p.items(), key=lambda x: x[1], reverse=True))}")
        if low_p:
            print(f"   low pressure: {', '.join(f'{k}={v}' for k, v in sorted(low_p.items()))}")

    # Economy
    if economy:
        scarce = economy_analysis["scarce"]
        abundant = economy_analysis["abundant"]
        if scarce:
            print(f"   scarce resources: {', '.join(f'{k}={v}' for k, v in sorted(scarce.items()))}")
        if abundant:
            print(f"   abundant resources: {', '.join(f'{k}={v}' for k, v in sorted(abundant.items(), key=lambda x: x[1], reverse=True))}")

    # Themes
    if theme_analysis.get("dominant"):
        print(f"   dominant theme: {theme_analysis['dominant']} ({theme_analysis['dominant_score']})")

    # Story questions
    if questions:
        esc = questions_analysis["escalating"]
        if esc:
            print(f"   escalating questions: {len(esc)}")
            for q in esc[:3]:
                print(f"     • {q['id']}: {q['question']} (pressure: {q.get('current_pressure', 0)})")

    # Character arcs
    pending = arcs_analysis["pending_transitions"]
    if pending:
        print(f"   arc transitions pending: {len(pending)}")
        for p in pending[:3]:
            print(f"     • {p['npc']}: {p['from']} → {p['to']} (trigger: {p['trigger']})")

    # Emotional state
    trauma = emotions_analysis["unaddressed_trauma"]
    if trauma:
        print(f"   unaddressed trauma: {len(trauma)} memories")

    # Promises
    pressing_promises = promises_analysis["pressing"]
    if pressing_promises:
        print(f"   pressing promises: {len(pressing_promises)} (strength ≥50)")
        for p in pressing_promises[:3]:
            print(f"     • {p['id']}: {p['promise'][:50]} (strength: {p.get('strength', 0)})")

    # Epistemic gaps
    epistemic_gaps = epistemic_analysis["gaps"]
    if epistemic_gaps:
        print(f"   epistemic gaps: {len(epistemic_gaps)} NPCs with wrong/incomplete beliefs")
    misconceptions = epistemic_analysis["misconceptions"]
    if misconceptions:
        print(f"   player misconceptions: {len(misconceptions)} false beliefs awaiting correction")

    # Apply effects guidance
    print(f"\n## Apply this beat:")
    print(f"   python3 drama.py beat-apply --campaign {args.campaign} --type {top_beat} --session {current_session or '<N>'}")
    print(f"   python3 director.py --campaign {args.campaign} beat-record --beat {top_beat} --texture {chosen_texture} --session {current_session or '<N>'}")

    # Show alternative beats
    print(f"\n## Alternative beats (top 5):")
    for beat_type, score, reasons, sources, _ in beat_scores[:5]:
        marker = "▶" if beat_type == top_beat else " "
        source_str = f" [{','.join(sources[:2])}]" if sources else ""
        print(f"   {marker} {beat_type:<16} ({score:>3} pts){source_str} {BEAT_TEMPLATES[beat_type]['description']}")

    # Show recent beat history
    if beat_history:
        print(f"\n## Recent beat history (anti-repetition context):")
        for h in beat_history[-5:]:
            print(f"   s{h.get('session', '?'):>3} {h.get('beat', '?'):<16} [{h.get('texture', '?')}] ({', '.join(h.get('sources', []))})")

    # Suggest participants based on analysis
    print(f"\n## Suggested participants:")
    suggested = set()

    # NPCs with pending arc transitions
    for p in pending[:2]:
        suggested.add(p["npc_id"])

    # NPCs from escalating story questions
    for q in questions_analysis["escalating"][:2]:
        for participant in q.get("participants", []):
            suggested.add(participant)

    # NPCs from recent intense memories
    for m in emotions_analysis["recent_intense"][:2]:
        for p in m.get("participants", []):
            suggested.add(p)

    if suggested:
        for s in sorted(suggested)[:5]:
            print(f"   • {s}")
    else:
        print(f"   (no specific NPCs suggested — use current scene participants)")

    if args.explain:
        print(f"\n## Full analysis (debug):")
        print(json.dumps(analysis, indent=2, default=str, ensure_ascii=False)[:3000])

    return 0


def cmd_status(args) -> int:
    """Show a quick status summary of all narrative layers."""
    pressure = _read_pressure(args.campaign)
    themes = _read_themes(args.campaign)
    economy = _read_economy(args.campaign)
    questions = _read_story_questions(args.campaign)
    arcs = _read_arcs(args.campaign)
    memories = _read_emotional_memory(args.campaign)
    campaign_state = _read_campaign_state(args.campaign)

    print(f"# narrative engine status\n")

    print(f"  campaign phase: {campaign_state.get('phase', 'stability')}")

    if pressure:
        high = {k: v for k, v in pressure.items() if v >= 70}
        print(f"  pressure: {len(pressure)} axes, {len(high)} high")

    if themes:
        dominant = max(themes, key=themes.get) if themes else None
        print(f"  themes: {len(themes)} tracked, dominant: {dominant}")

    if economy:
        scarce = {k: v for k, v in economy.items() if v <= 15}
        print(f"  economy: {len(economy)} resources, {len(scarce)} scarce")

    if questions:
        esc = [q for q in questions if q.get("status") == "escalating"]
        print(f"  story questions: {len(questions)} total, {len(esc)} escalating")

    if arcs:
        pending = 0
        for arc in arcs.values():
            current = arc.get("current_stage", 0)
            stages = arc.get("arc_stages", [])
            for t in arc.get("stage_transitions", []):
                if t.get("triggered_session") is None and t.get("from") == stages[current] if current < len(stages) else False:
                    pending += 1
        print(f"  character arcs: {len(arcs)} NPCs, {pending} transitions pending")

    if memories:
        intense = [m for m in memories if m.get("importance", 0) >= 7]
        print(f"  emotional memories: {len(memories)} total, {len(intense)} high-importance")

    # Check which files exist
    camp_dir = find_campaign(args.campaign)
    files = ["pressure.json", "themes.json", "economy.json", "story_questions.json",
             "character_arcs.json", "emotional_memory.json", "campaign_state.json",
             "intrigues.json", "plans.json", "scene_index.json", "suspicion.json", "secrets.json"]
    print(f"\n  narrative files present:")
    for f in files:
        exists = (camp_dir / f).exists()
        marker = "✓" if exists else "·"
        print(f"    {marker} {f}")
    return 0


# ── CLI ─────────────────────────────────────────────────────────────────────

def cmd_beat_record(args) -> int:
    """Record an applied beat to beat_history for anti-repetition tracking."""
    state_path = find_campaign(args.campaign) / "campaign_state.json"
    data = _load_json(state_path, {"phase": "stability", "beat_history": []})
    data.setdefault("beat_history", [])

    entry = {
        "session": args.session,
        "beat": args.beat,
        "texture": args.texture or "social",
        "sources": args.sources.split(",") if args.sources else [],
    }
    data["beat_history"].append(entry)

    # Keep only last N beats to prevent unbounded growth
    limit = _load_config(args.campaign)["beat_history_limit"]
    if len(data["beat_history"]) > limit:
        data["beat_history"] = data["beat_history"][-limit:]

    _save_json(state_path, data)
    print(f"OK — recorded beat: {args.beat} [{entry['texture']}] session {args.session}")
    print(f"  history length: {len(data['beat_history'])}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--campaign", required=True, help="Campaign name")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("recommend", help="Recommend the next scene type")
    s.add_argument("--session", type=int, default=0, help="Current session number")
    s.add_argument("--explain", action="store_true", help="Show full analysis (debug)")
    s.set_defaults(func=cmd_recommend)

    s = sub.add_parser("status", help="Show quick status of all narrative layers")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("beat-record", help="Record an applied beat for anti-repetition tracking")
    s.add_argument("--beat", required=True, choices=list(BEAT_TEMPLATES.keys()))
    s.add_argument("--texture", choices=list(BEAT_TEXTURES.keys()))
    s.add_argument("--sources", help="Comma-separated source categories that drove this beat")
    s.add_argument("--session", type=int, default=0)
    s.set_defaults(func=cmd_beat_record)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
