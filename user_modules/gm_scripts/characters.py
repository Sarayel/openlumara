#!/usr/bin/env python3
"""
characters.py — semi-random character traits, speech patterns, and stress
responses for LLM roleplay.

Generates procedural character profiles that the LLM can use to roleplay
NPCs consistently. Each NPC gets:
  - Speech pattern (vocabulary, sentence length, verbal tics)
  - Stress response (how they behave under pressure)
  - Emotional baseline (VAD: valence/arousal/dominance)
  - Conversational tactics (what they do when they want something)
  - Tell signs (how the GM signals the NPC is lying/nervous/angry)

Seeded by NPC name + campaign name, so the same NPC in the same campaign
always gets the same traits — reproducible and debuggable.

Storage: <campaign-dir>/character_traits.json

Usage:
  python3 characters.py generate --campaign <name> --npc velkyn
  python3 characters.py generate --campaign <name> --npc velkyn --archetype schemer
  python3 characters.py show --campaign <name> --npc velkyn
  python3 characters.py speech-guide --campaign <name> --npc velkyn
  python3 characters.py stress-response --campaign <name> --npc velkyn --stress high
  python3 characters.py list --campaign <name>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path
from typing import Optional

from paths import find_campaign


# ── Trait catalogs ──────────────────────────────────────────────────────────

SPEECH_PATTERNS = {
    "terse": {
        "description": "Short, clipped sentences. Doesn't waste words.",
        "avg_sentence_length": (3, 8),
        "vocabulary": "plain",
        "verbal_tics": ["...", "Tch.", "Hm."],
        "example": "No. Wrong move. Try again.",
    },
    "elaborate": {
        "description": "Long, flowing sentences. Loves adjectives.",
        "avg_sentence_length": (15, 30),
        "vocabulary": "ornate",
        "verbal_tics": ["indeed", "furthermore", "one might say"],
        "example": "It is, one might say, a rather delicate situation that requires our most careful consideration.",
    },
    "clipped_military": {
        "description": "Precise, commanding. Expects obedience.",
        "avg_sentence_length": (4, 10),
        "vocabulary": "tactical",
        "verbal_tics": ["Understood.", "Move.", "Report."],
        "example": "Sitrep. Now. Then we move.",
    },
    "flowery": {
        "description": "Poetic, metaphor-heavy. Speaks in images.",
        "avg_sentence_length": (12, 25),
        "vocabulary": "literary",
        "verbal_tics": ["like a...", "as the saying goes", "in the manner of"],
        "example": "The night unfolds like a velvet cloak, and we are but moths drawn to its flame.",
    },
    "street": {
        "description": "Slang-heavy, informal. Drops articles.",
        "avg_sentence_length": (3, 10),
        "vocabulary": "slang",
        "verbal_tics": ["yeah", "nah", "whatcha", "ain't"],
        "example": "Yeah, nah, that ain't how it works down here. Whatcha gonna do about it?",
    },
    "academic": {
        "description": "Precise, qualified. Hedges and footnotes verbally.",
        "avg_sentence_length": (10, 20),
        "vocabulary": "technical",
        "verbal_tics": ["arguably", "in principle", "one could observe", "strictly speaking"],
        "example": "Strictly speaking, the evidence is circumstantial, though one could argue it points toward a conclusion.",
    },
    "whispered": {
        "description": "Quiet, hesitant. Trails off. Avoids commitment.",
        "avg_sentence_length": (5, 12),
        "vocabulary": "simple",
        "verbal_tics": ["I think...", "maybe...", "perhaps...", "or not"],
        "example": "I think... maybe we should... perhaps not. Or... I don't know.",
    },
    "booming": {
        "description": "Loud, emphatic. Declares rather than discusses.",
        "avg_sentence_length": (8, 15),
        "vocabulary": "forceful",
        "verbal_tics": ["By God!", "Listen!", "I tell you!"],
        "example": "I tell you, this is madness! By God, we'll see who's standing at the end!",
    },
}

STRESS_RESPONSES = {
    "controlled": {
        "description": "Becomes colder, more precise under pressure.",
        "behavior": "slows down, speaks quieter, narrows eyes",
        "tells": ["voice drops half an octave", "hands stay perfectly still", "sentences get shorter"],
        "risk": "may seem calm when actually furious — players misread",
    },
    "explosive": {
        "description": "Erupts. Volume and aggression spike.",
        "behavior": "shouts, gestures wildly, invades space",
        "tells": ["veins in neck pulse", "voice cracks upward", "knocks things over"],
        "risk": "may attack or say something unforgivable",
    },
    "retreating": {
        "description": "Withdraws. Becomes small and quiet.",
        "behavior": "backs away, folds arms, avoids eye contact",
        "tells": ["voice becomes barely audible", "fidgets with cuffs/jewelry", "eyes dart to exits"],
        "risk": "may flee or shut down entirely",
    },
    "deflecting": {
        "description": "Changes subject, makes jokes, deflects.",
        "behavior": "laughs nervously, redirects, offers drinks",
        "tells": ["laughter doesn't reach eyes", "pours drinks nobody asked for", "brings up old stories"],
        "risk": "players may not realize they're stressed",
    },
    "calculating": {
        "description": "Goes silent, then acts decisively.",
        "behavior": "long pause, then a single devastating move",
        "tells": ["goes completely still", "eyes unfocus briefly", "then acts without hesitation"],
        "risk": "may betray or sacrifice someone in the next 30 seconds",
    },
    "pleading": {
        "description": "Begs, bargains, offers anything.",
        "behavior": "drops to knees, grabs at clothing, offers deals",
        "tells": ["voice becomes high and thin", "tears (real or performed)", "promises escalate wildly"],
        "risk": "may reveal secrets to save themselves",
    },
}

ARCHETYPES = {
    "schemer": {
        "speech": ["elaborate", "academic", "clipped_military"],
        "stress": ["controlled", "calculating"],
        "vad": {"valence": -0.2, "arousal": 0.3, "dominance": 0.7},
        "tactics": ["implies consequences", "offers 'help' with strings", "names drops"],
    },
    "warrior": {
        "speech": ["terse", "clipped_military", "booming"],
        "stress": ["explosive", "controlled"],
        "vad": {"valence": 0.1, "arousal": 0.6, "dominance": 0.8},
        "tactics": ["intimidates", "challenges directly", "escalates to violence"],
    },
    "diplomat": {
        "speech": ["elaborate", "flowery", "academic"],
        "stress": ["deflecting", "controlled"],
        "vad": {"valence": 0.3, "arousal": 0.2, "dominance": 0.5},
        "tactics": ["finds common ground", "offers trade", "appeals to mutual interest"],
    },
    "fanatic": {
        "speech": ["booming", "flowery", "terse"],
        "stress": ["explosive", "pleading"],
        "vad": {"valence": -0.3, "arousal": 0.8, "dominance": 0.4},
        "tactics": ["cites doctrine", "demands conversion", "threatens divine punishment"],
    },
    "opportunist": {
        "speech": ["street", "flowery", "whispered"],
        "stress": ["deflecting", "pleading"],
        "vad": {"valence": 0.0, "arousal": 0.5, "dominance": 0.3},
        "tactics": ["offers deals", "plays both sides", "cut losses fast"],
    },
    "mystic": {
        "speech": ["flowery", "whispered", "academic"],
        "stress": ["retreating", "calculating"],
        "vad": {"valence": -0.1, "arousal": 0.3, "dominance": 0.6},
        "tactics": ["speaks in riddles", "cites visions", "withholds until 'the time is right'"],
    },
    "survivor": {
        "speech": ["terse", "street", "whispered"],
        "stress": ["retreating", "explosive"],
        "vad": {"valence": -0.4, "arousal": 0.7, "dominance": 0.2},
        "tactics": ["appeals to sympathy", "offers information", "bargains desperately"],
    },
    "noble": {
        "speech": ["elaborate", "booming", "flowery"],
        "stress": ["controlled", "explosive"],
        "vad": {"valence": 0.2, "arousal": 0.3, "dominance": 0.9},
        "tactics": ["invokes rank", "commands", "offers patronage"],
    },
}

# Physical/behavioral tells for lying, nervousness, anger
TELL_SIGNS = {
    "lying": [
        "touches face frequently",
        "maintains too much eye contact (overcompensating)",
        "voice rises slightly at end of statements",
        "grooms clothing unnecessarily",
        "smiles that don't reach the eyes",
    ],
    "nervous": [
        "fidgets with ring/cuff",
        "swallows before speaking",
        "voice drops volume",
        "shifts weight foot to foot",
        "eyes dart to exits",
    ],
    "angry": [
        "jaw clenches",
        "nostrils flare",
        "hands curl into fists at sides",
        "breathing slows deliberately",
        "voice becomes dangerously quiet",
    ],
    "afraid": [
        "pales visibly",
        "hands tremble slightly",
        "takes half-step back",
        "eyes widen, then narrow",
        "voice cracks",
    ],
    "calculating": [
        "eyes unfocus briefly",
        "goes completely still",
        "fingers tap once on surface",
        "head tilts 5 degrees",
        "lips press into thin line",
    ],
}


# ── IO ──────────────────────────────────────────────────────────────────────

def _traits_path(campaign: str) -> Path:
    return find_campaign(campaign) / "character_traits.json"


def _load(campaign: str) -> dict:
    p = _traits_path(campaign)
    if not p.exists():
        return {"version": 1, "characters": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "characters": {}}
    data.setdefault("version", 1)
    data.setdefault("characters", {})
    return data


def _save(campaign: str, data: dict) -> None:
    p = _traits_path(campaign)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _norm_id(name: str) -> str:
    if ":" in name:
        return name
    return f"npc:{name}"


def _seeded_rng(campaign: str, npc_id: str) -> random.Random:
    """Create a seeded RNG from campaign + npc_id. Reproducible."""
    seed_str = f"{campaign}:{npc_id}"
    seed = int(hashlib.md5(seed_str.encode()).hexdigest(), 16) % (2**32)
    return random.Random(seed)


# ── Generation ──────────────────────────────────────────────────────────────

def _generate_traits(campaign: str, npc_id: str, archetype: str = None) -> dict:
    """Generate semi-random traits for an NPC using a seeded RNG."""
    rng = _seeded_rng(campaign, npc_id)

    # Pick archetype if not specified
    if not archetype or archetype not in ARCHETYPES:
        archetype = rng.choice(list(ARCHETYPES.keys()))
    arch = ARCHETYPES[archetype]

    # Pick speech pattern from archetype's preferred list
    speech_key = rng.choice(arch["speech"])
    speech = SPEECH_PATTERNS[speech_key].copy()
    speech["key"] = speech_key

    # Pick stress response from archetype's preferred list
    stress_key = rng.choice(arch["stress"])
    stress = STRESS_RESPONSES[stress_key].copy()
    stress["key"] = stress_key

    # Pick tell signs (2-3 per emotion)
    tells = {}
    for emotion, signs in TELL_SIGNS.items():
        tells[emotion] = rng.sample(signs, min(2, len(signs)))

    # VAD baseline from archetype, with small random variation
    vad = {}
    for axis, base in arch["vad"].items():
        vad[axis] = max(-1.0, min(1.0, base + rng.uniform(-0.1, 0.1)))

    # Conversational tactics (pick 2-3 from archetype)
    tactics = rng.sample(arch["tactics"], min(2, len(arch["tactics"])))

    return {
        "archetype": archetype,
        "speech": speech,
        "stress_response": stress,
        "vad_baseline": vad,
        "tactics": tactics,
        "tells": tells,
        "generated": True,
    }


def cmd_generate(args) -> int:
    """Generate (or regenerate) traits for an NPC."""
    data = _load(args.campaign)
    npc_id = _norm_id(args.npc)

    traits = _generate_traits(args.campaign, npc_id, args.archetype)

    # Preserve any existing name/role metadata
    existing = data["characters"].get(npc_id, {})
    traits["name"] = existing.get("name", args.npc)
    if args.archetype:
        traits["archetype"] = args.archetype

    data["characters"][npc_id] = traits
    _save(args.campaign, data)

    print(f"OK — traits generated for {npc_id}")
    print(f"  archetype: {traits['archetype']}")
    print(f"  speech: {traits['speech']['key']} — {traits['speech']['description']}")
    print(f"  stress: {traits['stress_response']['key']} — {traits['stress_response']['description']}")
    print(f"  VAD: v={traits['vad_baseline']['valence']:.1f} "
          f"a={traits['vad_baseline']['arousal']:.1f} "
          f"d={traits['vad_baseline']['dominance']:.1f}")
    print(f"  tactics: {', '.join(traits['tactics'])}")
    return 0


def cmd_show(args) -> int:
    """Show the full trait profile for an NPC."""
    data = _load(args.campaign)
    npc_id = _norm_id(args.npc)
    traits = data["characters"].get(npc_id)

    if not traits:
        print(f"# no traits for '{args.npc}'. Generate with:")
        print(f"   python3 characters.py generate --campaign {args.campaign} --npc {args.npc}")
        return 1

    print(json.dumps(traits, indent=2, ensure_ascii=False))
    return 0


def cmd_speech_guide(args) -> int:
    """Show a speech guide the LLM can use to roleplay this NPC."""
    data = _load(args.campaign)
    npc_id = _norm_id(args.npc)
    traits = data["characters"].get(npc_id)

    if not traits:
        print(f"# no traits for '{args.npc}'", file=sys.stderr)
        return 1

    speech = traits.get("speech", {})
    stress = traits.get("stress_response", {})
    vad = traits.get("vad_baseline", {})

    print(f"# SPEECH GUIDE: {traits.get('name', npc_id)}\n")

    print(f"## Speech pattern: {speech.get('key', '?')}")
    print(f"   {speech.get('description', '?')}")
    print(f"   avg sentence length: {speech.get('avg_sentence_length', '?')} words")
    print(f"   vocabulary: {speech.get('vocabulary', '?')}")
    print(f"   verbal tics: {', '.join(speech.get('verbal_tics', []))}")
    print(f"   example: \"{speech.get('example', '?')}\"")
    print()

    print(f"## Emotional baseline (VAD):")
    v = vad.get("valence", 0)
    a = vad.get("arousal", 0)
    d = vad.get("dominance", 0)
    print(f"   valence:   {v:+.1f} ({'positive' if v > 0.2 else 'negative' if v < -0.2 else 'neutral'})")
    print(f"   arousal:   {a:+.1f} ({'high energy' if a > 0.4 else 'low energy' if a < 0.2 else 'medium'})")
    print(f"   dominance: {d:+.1f} ({'dominant' if d > 0.4 else 'submissive' if d < 0.2 else 'balanced'})")
    print()

    print(f"## Conversational tactics (when they want something):")
    for t in traits.get("tactics", []):
        print(f"   • {t}")
    print()

    print(f"## Tell signs (GM uses these to signal emotional state):")
    for emotion, signs in traits.get("tells", {}).items():
        print(f"   {emotion}:")
        for s in signs:
            print(f"     • {s}")
    return 0


def cmd_stress_response(args) -> int:
    """Show how this NPC behaves under the specified stress level."""
    data = _load(args.campaign)
    npc_id = _norm_id(args.npc)
    traits = data["characters"].get(npc_id)

    if not traits:
        print(f"# no traits for '{args.npc}'", file=sys.stderr)
        return 1

    stress = traits.get("stress_response", {})
    level = args.stress

    print(f"# STRESS RESPONSE: {traits.get('name', npc_id)} under {level} stress\n")

    print(f"## Pattern: {stress.get('key', '?')}")
    print(f"   {stress.get('description', '?')}")
    print(f"   behavior: {stress.get('behavior', '?')}")
    print()

    print(f"## Observable tells:")
    for t in stress.get("tells", []):
        print(f"   • {t}")
    print()

    print(f"## Risk:")
    print(f"   {stress.get('risk', '?')}")
    print()

    if level == "high":
        print(f"## GM guidance for high stress:")
        print(f"   - Apply the tells liberally in narration")
        print(f"   - The NPC's speech pattern should shift: {stress.get('key')} stress modifies their normal {traits.get('speech', {}).get('key', '?')} speech")
        print(f"   - The risk ({stress.get('risk', '?')}) should be live — players can trigger it")
    elif level == "moderate":
        print(f"## GM guidance for moderate stress:")
        print(f"   - Show 1-2 tells, not all of them")
        print(f"   - Speech pattern is mostly normal but strained")
    else:
        print(f"## GM guidance for low stress:")
        print(f"   - NPC behaves normally per their speech pattern")
        print(f"   - No stress tells visible")
    return 0


def cmd_list(args) -> int:
    """List all NPCs with generated traits."""
    data = _load(args.campaign)
    if not data["characters"]:
        print(f"# no character traits generated for '{args.campaign}'")
        return 0

    print(f"# {len(data['characters'])} character(s) with traits\n")
    print(f"{'NPC':<20} {'Archetype':<12} {'Speech':<18} {'Stress':<14}")
    print("-" * 65)
    for npc_id, traits in sorted(data["characters"].items()):
        name = traits.get("name", npc_id)
        arch = traits.get("archetype", "?")
        speech = traits.get("speech", {}).get("key", "?")
        stress = traits.get("stress_response", {}).get("key", "?")
        print(f"{name:<20} {arch:<12} {speech:<18} {stress:<14}")
    return 0


# ── CLI ─────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--campaign", required=True, help="Campaign name")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("generate", help="Generate traits for an NPC")
    s.add_argument("--npc", required=True)
    s.add_argument("--archetype", choices=list(ARCHETYPES.keys()),
                   help="Force a specific archetype (default: seeded random)")
    s.set_defaults(func=cmd_generate)

    s = sub.add_parser("show", help="Show full trait profile")
    s.add_argument("--npc", required=True)
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("speech-guide", help="Show speech guide for LLM roleplay")
    s.add_argument("--npc", required=True)
    s.set_defaults(func=cmd_speech_guide)

    s = sub.add_parser("stress-response", help="Show stress response for an NPC")
    s.add_argument("--npc", required=True)
    s.add_argument("--stress", required=True, choices=["low", "moderate", "high"])
    s.set_defaults(func=cmd_stress_response)

    s = sub.add_parser("list", help="List all NPCs with traits")
    s.set_defaults(func=cmd_list)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
