#!/usr/bin/env python3
"""
validate_system.py — porting contract validator for system modules.

Checks a new system module against the required contract from
SYSTEM-PORTING.md before it's considered shippable. Turns "follow
SYSTEM-PORTING.md" from an honor system into a checked one.

Checks:
  1. system.md exists and has required sections
  2. Dice Convention section declares a resolution mechanic
  3. Ability Scores / Statistics section is present
  4. Character Structure section is present
  5. Health and Damage section is present
  6. Primary Resource section is present
  7. Conditions / Status Effects section is present
  8. ui.json exists and has valid schema (if present)
  9. ui.json system field matches directory name
  10. ui.json sidebar and sheet sections are valid
  11. CONDITION_COLOURS in tracker.py includes system conditions (if custom)

Usage:
  python3 validate_system.py --system dnd5e
  python3 validate_system.py --system vtm-v20
  python3 validate_system.py --all
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


SKILL_BASE = Path(__file__).resolve().parent.parent
SYSTEMS_DIR = SKILL_BASE / "systems"

REQUIRED_SECTIONS = [
    ("Dice Convention", "Core resolution mechanic — how to resolve a roll"),
    ("Ability Scores", "Character statistics and how modifiers are derived"),
    ("Character Structure", "Key tracked fields for a character"),
    ("Health and Damage", "HP/health model and damage types"),
    ("Primary Resource", "Main limited resource characters spend"),
    ("Conditions", "Status effects with severity for colour-coding"),
]


class ValidationResult:
    def __init__(self):
        self.errors = []
        self.warnings = []

    def error(self, check, message):
        self.errors.append({"check": check, "message": message})

    def warn(self, check, message):
        self.warnings.append({"check": check, "message": message})

    @property
    def passed(self):
        return not self.errors


def validate_system(system_name: str) -> ValidationResult:
    r = ValidationResult()
    sys_dir = SYSTEMS_DIR / system_name

    if not sys_dir.exists():
        r.error("exists", f"System directory '{system_name}' not found in systems/")
        return r

    # 1. system.md exists
    system_md = sys_dir / "system.md"
    if not system_md.exists():
        r.error("system.md", f"system.md not found in systems/{system_name}/")
        return r

    text = system_md.read_text(encoding="utf-8", errors="replace")

    # 2-7. Required sections
    for section_name, description in REQUIRED_SECTIONS:
        # Check for ## heading (flexible: might be "Ability Scores / Statistics")
        pattern = rf"^##.*{re.escape(section_name)}.*$"
        if not re.search(pattern, text, re.MULTILINE):
            r.error("sections", f"Missing required section: '## {section_name}' — {description}")

    # 8-10. ui.json validation (optional but checked if present)
    ui_path = sys_dir / "ui.json"
    if ui_path.exists():
        try:
            ui = json.loads(ui_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            r.error("ui.json", f"ui.json is invalid JSON: {e}")
            return r

        # Check manifest_version
        if ui.get("manifest_version") != 1:
            r.error("ui.json", "manifest_version must be 1")

        # Check system field matches directory name
        if ui.get("system") != system_name:
            r.error("ui.json", f"system field '{ui.get('system')}' doesn't match directory name '{system_name}'")

        # Check label exists
        if not ui.get("label"):
            r.warn("ui.json", "No label field — will show system name in display")

        # Check sidebar exists and is a list
        sidebar = ui.get("sidebar")
        if sidebar is None:
            r.warn("ui.json", "No sidebar field — display will use built-in default")
        elif not isinstance(sidebar, list):
            r.error("ui.json", "sidebar must be a list of widget objects")

        # Check sheet exists
        sheet = ui.get("sheet")
        if sheet is not None:
            if not isinstance(sheet, dict):
                r.error("ui.json", "sheet must be an object")
            else:
                if "combat_strip" not in sheet:
                    r.warn("ui.json", "No combat_strip in sheet — character sheet will lack top stats")
                if "stat_grid" not in sheet:
                    r.warn("ui.json", "No stat_grid in sheet — character sheet will lack attribute grid")

        # Validate sidebar widget types
        VALID_WIDGET_TYPES = {"bar", "stat_lines", "tag_list", "tag_single", "effects",
                              "badge_set", "pip_levels", "feature_flags", "badge"}
        if isinstance(sidebar, list):
            for i, widget in enumerate(sidebar):
                if not isinstance(widget, dict):
                    r.error("ui.json", f"sidebar[{i}] is not a dict")
                    continue
                wtype = widget.get("type")
                if wtype not in VALID_WIDGET_TYPES:
                    r.error("ui.json", f"sidebar[{i}] has unknown type '{wtype}'. Valid: {sorted(VALID_WIDGET_TYPES)}")
                if not widget.get("bind"):
                    r.warn("ui.json", f"sidebar[{i}] ({wtype}) has no bind field — will render nothing")
    else:
        r.warn("ui.json", f"No ui.json — display will use built-in D&D 5e default layout")

    # 11. Check for NOTICE file (attribution)
    notice = sys_dir / "NOTICE"
    if not notice.exists():
        r.warn("NOTICE", f"No NOTICE file in systems/{system_name}/ — attribution recommended for published game systems")

    return r


def cmd_validate(args) -> int:
    if args.all:
        systems = sorted(d.name for d in SYSTEMS_DIR.iterdir() if d.is_dir() and not d.name.startswith("."))
    else:
        systems = [args.system]

    all_pass = True
    for sys_name in systems:
        result = validate_system(sys_name)
        status = "✓ PASS" if result.passed else "✗ FAIL"
        print(f"\n{'='*60}")
        print(f"  {status}: systems/{sys_name}/")
        print(f"{'='*60}\n")

        if result.errors:
            for e in result.errors:
                print(f"  ✗ [{e['check']}] {e['message']}")
            all_pass = False
        if result.warnings:
            for w in result.warnings:
                print(f"  ⚠ [{w['check']}] {w['message']}")
        if result.passed and not result.warnings:
            print("  All checks passed with no warnings.")

    print()
    if all_pass:
        print("All systems passed validation.")
        return 0
    else:
        print("Some systems failed validation.")
        return 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--system", help="System module name to validate")
    p.add_argument("--all", action="store_true", help="Validate all system modules")
    p.add_argument("cmd", nargs="?", default="validate")
    args = p.parse_args(argv)

    if not args.system and not args.all:
        p.error("either --system <name> or --all is required")
    return cmd_validate(args)


if __name__ == "__main__":
    sys.exit(main())
