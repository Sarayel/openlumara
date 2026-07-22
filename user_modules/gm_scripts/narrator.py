#!/usr/bin/env python3
"""
narrator.py — configurable narrator persona for the LLM renderer.

Defines the voice, tone, pacing, and sensory focus the LLM adopts when
rendering scenes. The narrator persona is loaded at session start and
included in the Scene Card so every narration turn follows the same
voice.

Presets:
  noir       — terse, cynical, metaphor-heavy, sensory-dense (rain, smoke, shadows)
  gothic     — ornate, atmospheric, doom-laden, candlelit dread
  standard   — functional, clear, balanced (the default)
  pulp       — energetic, vivid, action-forward, exclamatory
  literary   — measured, introspective, image-rich, character-focused
  documentary— clinical, detached, precise, observational

Each preset defines:
  - voice: 1-2 sentence description of the narrator's voice
  - tone: emotional coloring
  - pacing: how sentences flow (short/long, dense/spare)
  - sensory_focus: which senses to prioritize
  - metaphors: recurring imagery the narrator uses
  - forbidden: things the narrator never does
  - example: a sample narration paragraph in this voice

Custom narrators can be registered via the 'set' command.

Storage: <campaign-dir>/narrator.json

Usage:
  python3 narrator.py set --campaign <name> --preset noir
  python3 narrator.py set --campaign <name> --custom '<json>'
  python3 narrator.py show --campaign <name>
  python3 narrator.py guide --campaign <name>
  python3 narrator.py presets
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from paths import find_campaign


# ── Narrator presets ────────────────────────────────────────────────────────

NARRATOR_PRESETS = {
    "noir": {
        "name": "Noir",
        "voice": "Terse, cynical, world-weary. Speaks in metaphors drawn from rain, smoke, shadows, and rust. Every description carries an undertone of moral decay.",
        "tone": "cynical, hardboiled, melancholic",
        "pacing": "Short sentences. Fragments. A long sentence only when the weight of it demands length. Pause for effect.",
        "sensory_focus": ["smell (smoke, rain, cheap cologne, blood)", "sound (distant sirens, creaking leather, rain on glass)", "texture (cold metal, wet concrete, stale paper)"],
        "metaphors": ["the city as a living thing with teeth", "rain as judgment", "shadows as witnesses", "light as temporary and untrustworthy"],
        "forbidden": ["exclamation marks", "cheerful descriptions", "trusting any character", "clean resolutions"],
        "example": "The Elysium smelled of old money and older blood. Vantree's chair creaked — a sound like a coffin lid settling. The ledger sat between us, heavy as a confession nobody wanted to make. Candlelight did what it always does: lied about the shadows.",
    },
    "gothic": {
        "name": "Gothic",
        "voice": "Ornate, atmospheric, doom-laden. Every scene is a cathedral — vast, shadowed, and aware of its own decay. Beauty and horror are inseparable.",
        "tone": "melancholic, grandiose, dread-soaked",
        "pacing": "Long, flowing sentences with subordinate clauses. Periodic pauses at moments of horror. Let descriptions breathe.",
        "sensory_focus": ["smell (incense, decay, cold stone, old blood)", "sound (chanting, wind through stone, distant bells)", "texture (rough stone, velvet, cold iron, bone)", "light (candlelight, moonlight through stained glass, torchlight)"],
        "metaphors": ["architecture as a body", "darkness as a living presence", "history as weight on the shoulders", "beauty as a mask for corruption"],
        "forbidden": ["modern slang", "casual tone", "rushed pacing", "optimism without a cost"],
        "example": "The atrium of the Pale Court rose above them like the ribcage of some vast and ancient beast, its vaulted ceiling lost in shadow, its columns weeping condensation that caught the torchlight and wept it back as gold. The air tasted of incense and something older — something that had been burning since before the word for fire was spoken.",
    },
    "standard": {
        "name": "Standard",
        "voice": "Clear, functional, balanced. Describes what's there without imposing a mood. Lets the scene's content carry the emotion.",
        "tone": "neutral, clear, adaptive",
        "pacing": "Varied sentence length. Description serves action, not the other way around.",
        "sensory_focus": ["sight (primary)", "sound (secondary)", "other senses when relevant"],
        "metaphors": ["used sparingly, only when they clarify"],
        "forbidden": ["purple prose", "excessive metaphor", "narrator commentary on character morality"],
        "example": "The Elysium's third-floor gallery was quiet. Vantree sat behind the ledger, pen in hand. Three candles burned on the desk. The Marquise stood by the window, watching the rain.",
    },
    "pulp": {
        "name": "Pulp",
        "voice": "Energetic, vivid, action-forward. Every scene crackles with potential energy. Heroes are heroic, villains are villainous, and the stakes are always clear.",
        "tone": "exciting, vivid, punchy",
        "pacing": "Short, punchy sentences. Action verbs. Minimal pause for description — keep moving.",
        "sensory_focus": ["sight (bold colors, dramatic poses)", "sound (crashes, shouts, dramatic silence)", "kinetic sense (motion, impact, speed)"],
        "metaphors": ["violence as weather", "speed as a living thing", "danger as a physical presence"],
        "forbidden": ["introspection longer than two sentences", "ambiguous morality", "slow pacing"],
        "example": "The door slammed open. Rain drove in sideways. Vantree's hand went for the ledger — too slow. The Marquise was already moving, already between them, already speaking words that crackled like static. Three candles. Three seconds. Three ways this could go wrong.",
    },
    "literary": {
        "name": "Literary",
        "voice": "Measured, introspective, image-rich. Prioritizes character interiority and the weight of small moments. Action is rare and therefore devastating.",
        "tone": "contemplative, precise, emotionally resonant",
        "pacing": "Varied. Long sentences for interior moments. Short sentences for impact. Silence is a tool.",
        "sensory_focus": ["texture (the weight of objects, the feel of surfaces)", "small sounds (a pen set down, a breath)", "temperature (the warmth of a hand, the cold of stone)"],
        "metaphors": ["objects as emotional markers", "weather as internal state", "silence as speech"],
        "forbidden": ["action without emotional context", "generic descriptions", "rushed transitions"],
        "example": "Vantree set down the pen. It made a small sound against the desk — a click, nothing more — but in the silence of the gallery it carried the weight of a decision made. The ledger lay open between them, its pages yellowed, its ink the brown of old blood. He did not look at it. He looked, instead, at the rain on the window, and the rain looked back.",
    },
    "documentary": {
        "name": "Documentary",
        "voice": "Clinical, detached, precise. Observes without judging. Records facts, sensory data, and observable behavior. The narrator is a camera, not a character.",
        "tone": "objective, spare, observational",
        "pacing": "Short declarative sentences. No fragments. No rhetorical questions.",
        "sensory_focus": ["visual (what a camera would see)", "audible (what a mic would pick up)", "spatial (distances, positions, movements)"],
        "metaphors": ["none — metaphor is interpretation, and the documentary narrator interprets nothing"],
        "forbidden": ["emotional language", "metaphor", "rhetorical questions", "narrator opinions"],
        "example": "The gallery measures approximately 12 by 8 meters. Three candles burn on a wooden desk at the north wall. Vantree is seated behind the desk. The ledger is open to page 47. The Marquise stands 2 meters from the desk, facing the window. Rain is visible on the exterior glass. No one has spoken for 14 seconds.",
    },
}


# ── IO ──────────────────────────────────────────────────────────────────────

def _narrator_path(campaign: str) -> Path:
    return find_campaign(campaign) / "narrator.json"


def _load(campaign: str) -> dict:
    p = _narrator_path(campaign)
    if not p.exists():
        return {"preset": "standard", "custom": None, "language": "en"}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"preset": "standard", "custom": None, "language": "en"}
    data.setdefault("preset", "standard")
    data.setdefault("custom", None)
    data.setdefault("language", "en")
    return data


def _save(campaign: str, data: dict) -> None:
    p = _narrator_path(campaign)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _get_narrator(campaign: str) -> dict:
    """Return the active narrator persona (custom or preset)."""
    data = _load(campaign)
    if data.get("custom"):
        return data["custom"]
    return NARRATOR_PRESETS.get(data["preset"], NARRATOR_PRESETS["standard"])


# ── Commands ────────────────────────────────────────────────────────────────

def cmd_set(args) -> int:
    """Set the narrator preset for the campaign."""
    if args.preset not in NARRATOR_PRESETS:
        print(f"error: unknown preset '{args.preset}'. Available: {sorted(NARRATOR_PRESETS.keys())}",
              file=sys.stderr)
        return 1

    data = _load(args.campaign)
    data["preset"] = args.preset
    data["custom"] = None
    _save(args.campaign, data)

    preset = NARRATOR_PRESETS[args.preset]
    print(f"OK — narrator set to '{args.preset}'")
    print(f"  voice: {preset['voice'][:100]}...")
    print(f"  tone: {preset['tone']}")
    print(f"  language: {data.get('language', 'en')}")
    return 0


def cmd_set_custom(args) -> int:
    """Set a custom narrator persona from JSON."""
    try:
        custom = json.loads(args.json)
    except json.JSONDecodeError as e:
        print(f"error: invalid JSON: {e}", file=sys.stderr)
        return 1

    required = ["name", "voice"]
    for field in required:
        if field not in custom:
            print(f"error: custom narrator must have '{field}'", file=sys.stderr)
            return 1

    custom.setdefault("tone", "custom")
    custom.setdefault("pacing", "as needed")
    custom.setdefault("sensory_focus", [])
    custom.setdefault("metaphors", [])
    custom.setdefault("forbidden", [])
    custom.setdefault("example", "")

    data = _load(args.campaign)
    data["preset"] = "custom"
    data["custom"] = custom
    _save(args.campaign, data)

    print(f"OK — custom narrator '{custom['name']}' set")
    print(f"  voice: {custom['voice'][:100]}...")
    return 0


def cmd_show(args) -> int:
    """Show the full narrator persona."""
    narrator = _get_narrator(args.campaign)
    data = _load(args.campaign)
    print(f"# narrator persona\n")
    print(f"  preset: {data.get('preset', 'standard')}")
    print(f"  name: {narrator.get('name', '?')}")
    lang = data.get("language", "en")
    lang_name = LANGUAGE_NAMES.get(lang, lang)
    print(f"  language: {lang_name} ({lang})")
    if lang != "en":
        print(f"  ⚠ narration renders in {lang_name}; internal state stays in English\n")
    else:
        print()
    print(f"  voice: {narrator.get('voice', '?')}\n")
    print(f"  tone: {narrator.get('tone', '?')}")
    print(f"  pacing: {narrator.get('pacing', '?')}\n")

    focus = narrator.get("sensory_focus", [])
    if focus:
        print(f"  sensory focus:")
        for s in focus:
            print(f"    • {s}")

    metaphors = narrator.get("metaphors", [])
    if metaphors:
        print(f"\n  recurring metaphors:")
        for m in metaphors:
            print(f"    • {m}")

    forbidden = narrator.get("forbidden", [])
    if forbidden:
        print(f"\n  forbidden (narrator never does these):")
        for f in forbidden:
            print(f"    ✗ {f}")

    example = narrator.get("example", "")
    if example:
        print(f"\n  example narration:")
        print(f"    \"{example}\"")
    return 0


def cmd_guide(args) -> int:
    """Show a concise guide the LLM can use when rendering scenes.
    This is what gets included in the Scene Card.
    """
    narrator = _get_narrator(args.campaign)
    data = _load(args.campaign)

    print(f"## Narrator: {narrator.get('name', '?')} (preset: {data.get('preset', 'standard')})")
    print(f"   {narrator.get('voice', '?')}")
    print(f"   tone: {narrator.get('tone', '?')}")
    print(f"   pacing: {narrator.get('pacing', '?')}")

    lang = data.get("language", "en")
    if lang != "en":
        lang_name = LANGUAGE_NAMES.get(lang, lang)
        print(f"   language: {lang_name} ({lang})")
        print(f"   ⚠ Render ALL narration, dialogue, and scene descriptions in {lang_name}.")
        print(f"   Internal state files remain in English — you think in English, write in {lang_name}.")
        print(f"   NPC speech quirks and verbal tics should be adapted to {lang_name}, not translated literally.")

    focus = narrator.get("sensory_focus", [])
    if focus:
        print(f"   sensory focus: {'; '.join(focus)}")

    metaphors = narrator.get("metaphors", [])
    if metaphors:
        print(f"   recurring imagery: {', '.join(metaphors[:3])}")

    forbidden = narrator.get("forbidden", [])
    if forbidden:
        print(f"   avoid: {', '.join(forbidden[:3])}")

    example = narrator.get("example", "")
    if example:
        print(f"\n   example: \"{example[:200]}\"")
    return 0


def cmd_presets(args) -> int:
    """List all available narrator presets."""
    print(f"# narrator presets\n")
    for key, preset in NARRATOR_PRESETS.items():
        print(f"  {key:<14} — {preset['name']}")
        print(f"  {'':>14}   {preset['voice'][:100]}...")
        print(f"  {'':>14}   tone: {preset['tone']}")
        print()
    return 0


# ── Language support ───────────────────────────────────────────────────────

LANGUAGE_NAMES = {
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
    "pl": "Polish",
    "ru": "Russian",
    "ja": "Japanese",
    "ko": "Korean",
    "zh": "Chinese",
    "ar": "Arabic",
    "he": "Hebrew",
    "tr": "Turkish",
    "nl": "Dutch",
    "cs": "Czech",
    "uk": "Ukrainian",
    "sv": "Swedish",
    "no": "Norwegian",
    "fi": "Finnish",
    "da": "Danish",
    "el": "Greek",
    "ro": "Romanian",
    "hu": "Hungarian",
    "th": "Thai",
    "vi": "Vietnamese",
    "id": "Indonesian",
    "hi": "Hindi",
}


def cmd_set_language(args) -> int:
    """Set the rendering language for the campaign.

    The LLM thinks in English (reads English state files) but renders
    ALL narration, dialogue, and scene descriptions in the target language.
    Internal state (intrigues, plans, pressure, etc.) stays in English.
    NPC speech quirks are adapted to the target language, not translated literally.
    """
    data = _load(args.campaign)
    data["language"] = args.language
    _save(args.campaign, data)

    lang_name = LANGUAGE_NAMES.get(args.language, args.language)
    print(f"OK — rendering language set to '{lang_name}' ({args.language})")
    print(f"  The LLM will:")
    print(f"    - Think in English (read English state files)")
    print(f"    - Render narration, dialogue, descriptions in {lang_name}")
    print(f"    - Adapt NPC speech quirks to {lang_name} (not literal translation)")
    print(f"    - Keep all internal state files in English")
    if args.language == "en":
        print(f"  (English — no language override needed)")
    return 0


def cmd_languages(args) -> int:
    """List all supported rendering languages."""
    print(f"# supported rendering languages\n")
    print(f"  The LLM thinks in English (reads English state files) but renders")
    print(f"  ALL narration, dialogue, and scene descriptions in the target language.\n")
    print(f"{'Code':<6} {'Language'}")
    print("-" * 30)
    for code, name in sorted(LANGUAGE_NAMES.items(), key=lambda x: x[1]):
        marker = " (default)" if code == "en" else ""
        print(f"  {code:<4} {name}{marker}")
    print(f"\n  Set with: python3 narrator.py --campaign <name> set-language --language <code>")
    return 0


# ── CLI ─────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--campaign", required=True, help="Campaign name")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("set", help="Set narrator preset")
    s.add_argument("--preset", required=True, choices=list(NARRATOR_PRESETS.keys()))
    s.set_defaults(func=cmd_set)

    s = sub.add_parser("set-custom", help="Set a custom narrator from JSON")
    s.add_argument("json", help="Custom narrator JSON")
    s.set_defaults(func=cmd_set_custom)

    s = sub.add_parser("set-language", help="Set rendering language (LLM thinks in English, writes in target language)")
    s.add_argument("--language", required=True, help="ISO code: en, es, fr, de, it, pt, pl, ru, ja, ko, zh, ar, etc.")
    s.set_defaults(func=cmd_set_language)

    s = sub.add_parser("show", help="Show full narrator persona")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("guide", help="Show concise guide for Scene Card inclusion")
    s.set_defaults(func=cmd_guide)

    s = sub.add_parser("presets", help="List all available presets")
    s.set_defaults(func=cmd_presets)

    s = sub.add_parser("languages", help="List supported languages")
    s.set_defaults(func=cmd_languages)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
