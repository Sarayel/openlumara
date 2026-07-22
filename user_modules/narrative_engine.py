# ALWAYS import core at the start. very important.
import core

import asyncio
import json
import os
import pathlib
import re
import shlex
import sys


# Confidence weights match plans.py's own CONFIDENCE_LEVELS vocabulary in the
# original engine (confirmed/suspected/rumor/false/fabricated) — reused here
# rather than inventing a new scale, since it's already the engine's authored
# notion of "how much does this fact count."
CONFIDENCE_WEIGHT = {"confirmed": 1.0, "suspected": 0.6, "rumor": 0.3, "false": 0.05, "fabricated": 0.05}

_WHAT_KNOWS_LINE = re.compile(
    r"^\s*\[s\s*(-?\d+)\]\s+(\S+)\s+\[(\w+)\]\s+via\s+(\S+)\s+from\s+(.+?)\s*$"
)


def _dt_now() -> str:
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class NarrativeEngine(core.module.Module):
    """
    Narrative Engine orchestrator — a thin wrapper around the original
    open-tabletop-gm scripts (drama.py, plans.py, npc_drives.py, story.py,
    info_inventory.py, characters.py, etc). It does not reimplement any game
    logic itself: it shells out to the real scripts as subprocesses, so the
    scripts stay the single source of truth and can be updated independently
    by just dropping new files into the scripts folder.

    Drop the original open-tabletop-gm/scripts/*.py files into the folder set
    by the scripts_dir setting (default: "gm_scripts", next to this module).
    Use gm_list_scripts() to see what's available, and call any script with
    args="--help" first if you're unsure of its exact command syntax.

    The NPC-drive simulation advances with the conversation, not a wall clock:
    every rounds_per_check chat messages, it runs npc_drives.py check once —
    so nothing runs while the chat is idle and nothing piles up between turns.
    """

    # -------------------------
    #   CONFIGURATION
    # -------------------------

    settings = {
        "scripts_dir": {
            "type": "string",
            "description": "Folder containing the original open-tabletop-gm scripts. Relative paths resolve next to this module file.",
            "default": "gm_scripts",
        },
        "campaign": {
            "type": "string",
            "description": "Active campaign name, passed as --campaign to every script call. Change at runtime with campaign_switch or /gm campaign <name>.",
            "default": "default",
        },
        "campaign_root": {
            "type": "string",
            "description": "If set, overrides GM_CAMPAIGN_ROOT so campaign data is stored here instead of the scripts' own default (~/open-tabletop-gm).",
            "default": "",
        },
        "auto_check_drives": {
            "description": "Whether npc_drives.py check runs automatically as the chat progresses — this is the original engine's closed-loop simulation entry point.",
            "default": True,
        },
        "rounds_per_check": {
            "type": "number",
            "description": "How many chat rounds (user messages) between automatic npc_drives.py check calls. 1 = every message. Higher = the sim advances more slowly relative to the conversation.",
            "default": 1,
        },
        "auto_direct": {
            "description": "Whether micro_director.py tick (in-scene pacing) and director.py recommend (scene-level beat picking) run automatically alongside the drive check, if those scripts are present.",
            "default": True,
        },
        "director_check_every_rounds": {
            "type": "number",
            "description": "How many chat rounds between automatic director.py recommend calls. This is a macro, scene-level call, so it runs far less often than the per-round drive/micro-director checks — only a one-line 'new beat suggested' note is surfaced into context, never the full report.",
            "default": 6,
        },
        "inject_context": {
            "description": "Whether to inject a compact live snapshot (raw output of drama.py state-show / pressure-show, if present) into context each turn.",
            "default": True,
        },
        "timeout": {
            "type": "number",
            "description": "Seconds to wait for a script to finish before timing out.",
            "default": 30,
        },
        "salience_decay_rate": {
            "type": "number",
            "description": "How fast a fact's recall ranking fades with age, per session. Lower = slower fade. Default 0.15 is deliberately gentle — a clever early deduction shouldn't quietly vanish just because time passed. Use pin_fact() for anything that must never fade regardless of this setting.",
            "default": 0.15,
        },
        "salience_decay_floor": {
            "type": "number",
            "description": "The minimum recency weight a fact can decay to, 0-1. Confirmed facts never drop below this floor no matter how old, so nothing fully disappears from ranking purely due to age.",
            "default": 0.35,
        },
        "console_enabled": {
            "description": "Whether to maintain a separate, non-webUI debug console: a live log file, a live-rewritten text dashboard, and a simple command-drop file — all plain files on disk, meant to be watched from your own terminal (tail -f / watch cat), not the chat window.",
            "default": True,
        },
        "console_dir": {
            "type": "string",
            "description": "Folder for the console's files (console.log, dashboard.txt, commands.in, commands.out). Relative paths resolve next to this module file, same as scripts_dir.",
            "default": "console",
        },
        "console_poll_seconds": {
            "type": "number",
            "description": "How often the console refreshes dashboard.txt and checks commands.in for new commands. This is infrastructure polling for the debug console only — it is NOT the narrative simulation, which stays chat-round-driven (see rounds_per_check); this timer never touches game state.",
            "default": 5,
        },
        "require_active_session": {
            "description": "OpenLumara has no concept of 'this conversation is the RPG session' — on_user_message/on_end_prompt/on_system_prompt fire for every message in every conversation where this module is enabled, with no way to tell a game turn apart from an unrelated chat. When this is on (default), the per-round tick, context injection, and the full system prompt are all skipped entirely unless engine_start() has been called for the active campaign — so asking for a cookie recipe in an unrelated chat doesn't advance the world or cost any tokens. Turn this off only if you want the old always-on behavior (e.g. a dedicated OpenLumara instance/channel used for nothing but this campaign).",
            "default": True,
        },
    }

    dependencies = []

    # -------------------------
    #   SETUP
    # -------------------------

    async def on_ready(self):
        self._campaign = self.config.get("campaign") or "default"
        self._meta = core.storage.StorageDict("narrative_engine_orchestrator_meta", type="json")
        self._meta.setdefault("session", {})   # campaign name -> session counter (manual, GM-paced)
        self._meta.setdefault("round", {})     # campaign name -> chat-round counter (automatic, per message)
        self._meta.setdefault("scene", {})     # campaign name -> {"place": str, "present": [str]}
        self._meta.setdefault("pins", {})      # campaign name -> {fact_id: note} — never decays out of recall
        self._meta.setdefault("active", {})    # campaign name -> bool — gates ticking/injection, off by default
        self._meta.save()
        self._pending = []      # short strings from this round's ticks, surfaced once in on_end_prompt then cleared
        self._recall_log = []   # last few recall() calls this session, for /gm recalls
        self._check_log = []    # last few resolve_check() calls this session, for /gm checks

        if self.config.get("console_enabled", True):
            self._console_dir().mkdir(parents=True, exist_ok=True)
            for f in ("console.log", "dashboard.txt", "commands.in", "commands.out"):
                p = self._console_dir() / f
                if not p.exists():
                    p.write_text("", encoding="utf-8")
            self._log(f"=== narrative_engine module started, campaign '{self._campaign}' ===")

    async def on_shutdown(self):
        self._meta.save()

    def _is_active(self) -> bool:
        """Whether the per-round tick / context injection / full system prompt should
        run at all right now. If require_active_session is off, everything always
        runs (the old always-on behavior). If it's on (default), nothing runs unless
        engine_start() has been called for the active campaign — since OpenLumara
        gives modules no way to tell 'this is the game conversation' apart from any
        other chat, an explicit toggle is the only honest fix for that."""
        if not self.config.get("require_active_session", True):
            return True
        return bool(self._meta["active"].get(self._campaign, False))

    @staticmethod
    def _norm_id(name: str) -> str:
        if not name or ":" in name:
            return name
        return f"npc:{name}"

    def _scene(self) -> dict:
        return self._meta["scene"].get(self._campaign, {"place": "", "present": []})

    def _pins(self) -> dict:
        return self._meta["pins"].get(self._campaign, {})

    # -------------------------
    #   DEBUG CONSOLE (separate from the webUI — plain files on disk)
    # -------------------------
    # Meant to be watched from your own terminal, not the chat window:
    #   tail -f console/console.log        (live event log)
    #   watch -n 2 cat console/dashboard.txt   (live-refreshing text dashboard)
    #   echo "dashboard" >> console/commands.in   (drop a command, read the reply
    #                                               appended to console/commands.out)
    # None of this touches game state — it's read-only observability plus a tiny
    # command-relay, kept deliberately as plain files so it works the same whether
    # OpenLumara is running on your laptop, in Docker, or on a headless remote box.

    def _console_dir(self) -> pathlib.Path:
        raw = self.config.get("console_dir") or "console"
        p = pathlib.Path(raw)
        if not p.is_absolute():
            p = pathlib.Path(__file__).resolve().parent / raw
        return p

    def _log(self, msg: str):
        """Unified logging: always goes to OpenLumara's own module log, and — if the
        console is enabled — also appended with a timestamp to console/console.log
        for tailing from a separate terminal."""
        self.channel.log(self.name, msg)
        if not self.config.get("console_enabled", True):
            return
        try:
            import datetime
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            with open(self._console_dir() / "console.log", "a", encoding="utf-8") as f:
                f.write(f"[{ts}] {msg}\n")
        except OSError:
            pass  # console is observability-only — never let a file error break the module

    async def _count_from_list(self, script: str, args: str):
        """Runs a script's list/-list command and pulls the count out of its own
        '# N thing(s)' header line — every wired script prints this consistently.
        Returns None (not 0) if the script is missing or the format doesn't match,
        so the dashboard can honestly show 'n/a' instead of a wrong number."""
        if not self._resolve_script(script).exists():
            return None
        rc, out = await self._run(script, args)
        m = re.search(r"^#\s+(\d+)\s+\S", out, re.MULTILINE)
        return int(m.group(1)) if m else None

    async def _build_dashboard(self) -> str:
        campaign_q = shlex.quote(self._campaign)
        counts = {}
        for label, script, args in (
            ("NPCs (with traits)", "characters.py", f"--campaign {campaign_q} list"),
            ("Locations", "locations.py", f"--campaign {campaign_q} list"),
            ("Intrigues", "plans.py", f"--campaign {campaign_q} intrigue-list"),
            ("NPC drives", "npc_drives.py", f"--campaign {campaign_q} list"),
            ("Info items", "info_inventory.py", f"--campaign {campaign_q} list"),
        ):
            counts[label] = await self._count_from_list(script, args)

        if self._resolve_script("gm_graph.py").exists():
            rc, out = await self._run("gm_graph.py", f"list --campaign {campaign_q}")
            m = re.search(r"(\d+)\s+nodes,\s+(\d+)\s+edges", out)
            counts["Graph nodes / edges"] = f"{m.group(1)} / {m.group(2)}" if m else None
        else:
            counts["Graph nodes / edges"] = None

        scene = self._scene()
        pins = self._pins()
        lines = [
            f"NARRATIVE ENGINE — campaign '{self._campaign}'",
            f"session {self._session()} | round {self._round()} | {_dt_now()}",
            "-" * 50,
        ]
        for label, value in counts.items():
            lines.append(f"{label:<22} {value if value is not None else 'n/a'}")
        lines.append("-" * 50)
        lines.append(f"scene            {scene.get('place') or '(not set)'}")
        lines.append(f"present          {', '.join(scene.get('present', [])) or '-'}")
        lines.append(f"pinned facts     {len(pins)}")
        lines.append("-" * 50)
        lines.append("recent log:")
        try:
            log_lines = (self._console_dir() / "console.log").read_text(encoding="utf-8").splitlines()
            lines.extend(f"  {l}" for l in log_lines[-8:])
        except OSError:
            lines.append("  (console.log not readable)")
        return "\n".join(lines)

    async def _write_dashboard(self):
        try:
            (self._console_dir() / "dashboard.txt").write_text(await self._build_dashboard(), encoding="utf-8")
        except OSError:
            pass

    async def _poll_console_commands(self):
        """Reads any lines dropped into console/commands.in, runs each the same way
        /gm does (a script call, or one of dashboard/recalls/checks/pins/scene), and
        appends replies to console/commands.out. The file is cleared after each read
        so commands aren't re-run on the next poll."""
        path = self._console_dir() / "commands.in"
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if not lines:
            return
        path.write_text("", encoding="utf-8")

        replies = []
        for line in lines:
            parts = line.split()
            cmd, rest = parts[0], parts[1:]
            if cmd == "dashboard":
                reply = await self._build_dashboard()
            elif cmd == "recalls":
                reply = ", ".join(self._recall_log) or "none yet"
            elif cmd == "checks":
                reply = "\n".join(self._check_log) or "none yet"
            elif cmd == "pins":
                reply = "\n".join(f"{k}: {v}" for k, v in self._pins().items()) or "none"
            elif cmd == "scene":
                s = self._scene()
                reply = f"place: {s.get('place') or '?'} | present: {', '.join(s.get('present', [])) or 'no one'}"
            else:
                rc, reply = await self._run(cmd, " ".join(rest))
            replies.append(f"$ {line}\n{reply}\n")

        try:
            with open(self._console_dir() / "commands.out", "a", encoding="utf-8") as f:
                f.write("\n".join(replies) + "\n")
        except OSError:
            pass

    async def on_background(self):
        """Refreshes the debug console only (dashboard.txt + polling commands.in) on
        a wall-clock timer. This is deliberately separate from the narrative
        simulation, which stays chat-round-driven — this loop never touches game
        state, it only re-renders a read-only snapshot and relays admin commands."""
        while True:
            try:
                interval = self.config.get("console_poll_seconds") or 5
                await asyncio.sleep(interval)
                if not self.config.get("console_enabled", True):
                    continue
                await self._write_dashboard()
                await self._poll_console_commands()
            except asyncio.CancelledError:
                break
            except Exception:
                pass  # console is observability-only — never let it crash the module

    def _rank_by_salience(self, what_knows_output: str, limit: int) -> str:
        """Parses info_inventory.py's 'what-knows' lines and re-sorts them by a simple,
        transparent salience score instead of raw chronological order — the aim is that
        recall() surfaces what's actually worth remembering right now, not everything
        ever logged. Falls back to the untouched original text if the format doesn't
        match (e.g. a future script version), so this never hard-fails.

        Two things keep this from quietly burying something a player worked to notice:
        pinned facts (via pin_fact) always show in full, never counted against `limit`;
        and the age-based decay for everything else is intentionally gentle and floored
        (see salience_decay_rate/salience_decay_floor) rather than fading toward zero."""
        lines = what_knows_output.splitlines()
        parsed = []
        for line in lines:
            m = _WHAT_KNOWS_LINE.match(line)
            if m:
                session, fact_id, conf, source, from_who = m.groups()
                parsed.append({
                    "session": int(session), "fact_id": fact_id, "confidence": conf,
                    "source": source, "from_who": from_who, "raw": line,
                })
        if not parsed:
            return what_knows_output  # unrecognized format — pass through untouched

        current_session = self._session()
        present_ids = {self._norm_id(p) for p in self._scene().get("present", [])}
        pins = self._pins()
        decay_rate = float(self.config.get("salience_decay_rate") or 0.15)
        decay_floor = max(0.0, min(1.0, float(self.config.get("salience_decay_floor") or 0.35)))

        def score(item):
            age = max(0, current_session - item["session"])
            recency = max(decay_floor, 1.0 / (1 + age * decay_rate))
            confidence = CONFIDENCE_WEIGHT.get(item["confidence"], 0.3)
            present_boost = 0.25 if self._norm_id(item["from_who"]) in present_ids else 0.0
            return confidence * recency + present_boost

        pinned = [i for i in parsed if i["fact_id"] in pins]
        rest = [i for i in parsed if i["fact_id"] not in pins]
        rest.sort(key=score, reverse=True)

        header = next((l for l in lines if l.startswith("#")), "")
        shown = rest[:limit]
        omitted = len(rest) - len(shown)
        out_lines = [header, ""] if header else []
        out_lines += [f"  \U0001F4CC {item['raw'].strip()}  — pinned: {pins[item['fact_id']]}" for item in pinned]
        out_lines += [f"  {item['raw'].strip()}" for item in shown]
        if omitted > 0:
            out_lines.append(f"\n  ...and {omitted} older/less-salient fact(s) not shown "
                              f"(raise recall's limit, or use gm_exec info_inventory.py what-knows for the full list).")
        return "\n".join(out_lines)

    def _scripts_dir(self) -> pathlib.Path:
        raw = self.config.get("scripts_dir") or "gm_scripts"
        p = pathlib.Path(raw)
        if not p.is_absolute():
            p = pathlib.Path(__file__).resolve().parent / raw
        return p

    def _resolve_script(self, script: str) -> pathlib.Path:
        if not script.endswith(".py"):
            script += ".py"
        return self._scripts_dir() / script

    def _session(self) -> int:
        return self._meta["session"].get(self._campaign, 0)

    def _advance_session(self) -> int:
        n = self._session() + 1
        self._meta["session"][self._campaign] = n
        self._meta.save()
        return n

    def _round(self) -> int:
        return self._meta["round"].get(self._campaign, 0)

    def _advance_round(self) -> int:
        n = self._round() + 1
        self._meta["round"][self._campaign] = n
        self._meta.save()
        return n

    # -------------------------
    #   SUBPROCESS ORCHESTRATION
    # -------------------------

    async def _run(self, script: str, args: str = ""):
        """Runs `python <script> <args>` in the scripts folder. Returns (returncode, combined_output)."""
        path = self._resolve_script(script)
        if not path.exists():
            return 1, f"script not found: {path.name} (looked in {self._scripts_dir()})"

        python_bin = sys.executable or "python3"
        try:
            argv = shlex.split(args)
        except ValueError as e:
            return 1, f"couldn't parse args: {e}"

        env = dict(os.environ)
        root = self.config.get("campaign_root")
        if root:
            env["GM_CAMPAIGN_ROOT"] = root

        timeout = self.config.get("timeout") or 30
        try:
            proc = await asyncio.create_subprocess_exec(
                python_bin, str(path), *argv,
                cwd=str(self._scripts_dir()), env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            return 1, f"script timed out after {timeout}s"
        except Exception as e:
            return 1, f"failed to run script: {e}"

        out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")
        combined = out + (("\n" + err) if err else "")
        return proc.returncode, combined.strip()

    def _script_description(self, path: pathlib.Path) -> str:
        """Pulls the first non-empty line of a script's module docstring, for discovery."""
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            return ""
        m = re.search(r'"""(.*?)"""', text, re.DOTALL)
        if not m:
            m = re.search(r"'''(.*?)'''", text, re.DOTALL)
        if not m:
            return ""
        for line in m.group(1).splitlines():
            line = line.strip().lstrip("#").strip()
            if line:
                return line
        return ""

    def _extract_after(self, text: str, marker: str, max_lines: int = 2) -> str:
        """Grabs up to max_lines of non-empty text right after a marker line. Returns ''
        if the marker isn't found — callers treat that as 'nothing to surface', so if a
        script's output format changes later this degrades silently instead of crashing."""
        idx = text.find(marker)
        if idx == -1:
            return ""
        rest = text[idx + len(marker):].splitlines()
        picked = [l.strip() for l in rest if l.strip()][:max_lines]
        return " ".join(picked)

    def _guess_action_type(self, content: str) -> str:
        """Cheap keyword heuristic mapping a chat message to micro_director.py's
        action_type vocabulary (dialogue|combat|investigation|social|exploration|movement).
        Deliberately not an LLM call — this only needs to be roughly right to feed the
        scene-tension valve, not perfectly classified."""
        text = (content or "").lower()
        if any(w in text for w in ("attack", "strike", "swing", "shoot", "stab", "fight", "punch")):
            return "combat"
        if any(w in text for w in ("search", "examine", "investigate", "inspect", "look for", "check the")):
            return "investigation"
        if any(w in text for w in ("persuade", "convince", "lie", "negotiate", "threaten", "bargain", "intimidate")):
            return "social"
        if any(w in text for w in ("walk", "run", "travel", "head to", "go to", "leave", "move to")):
            return "movement"
        return "dialogue"

    async def _advance_narrative(self, content: str):
        """The per-round tick: advances the round counter, then runs whichever of
        npc_drives.py / micro_director.py / director.py are present. Only ever appends
        to self._pending when a script actually reports something happened — silent
        rounds cost nothing extra in context."""
        round_n = self._advance_round()
        campaign_q = shlex.quote(self._campaign)
        session = self._session()

        if self.config.get("auto_check_drives", True):
            every = max(1, int(self.config.get("rounds_per_check") or 1))
            if round_n % every == 0 and self._resolve_script("npc_drives.py").exists():
                try:
                    rc, out = await self._run("npc_drives.py", f"--campaign {campaign_q} check --session {session}")
                    self._log(f"[{self._campaign}] round {round_n} drive check: {out[:500]}")
                    if "no NPC drives fired" not in out:
                        events = self._extract_after(out, "world event:", max_lines=1)
                        if events:
                            self._pending.append(f"[npc drive fired] {events}")
                except Exception as e:
                    self._log(f"drive check error: {e}")

        if self.config.get("auto_direct", True):
            if self._resolve_script("micro_director.py").exists():
                action_type = self._guess_action_type(content)
                try:
                    rc, out = await self._run(
                        "micro_director.py",
                        f"--campaign {campaign_q} tick --action-type {action_type} --session {session}",
                    )
                    directive = (self._extract_after(out, "INTERRUPT INJECTION:", max_lines=1)
                                 or self._extract_after(out, "AMBIENT BEAT:", max_lines=1))
                    if directive:
                        self._pending.append(directive)
                except Exception as e:
                    self._log(f"micro_director tick error: {e}")

            director_every = max(1, int(self.config.get("director_check_every_rounds") or 6))
            if round_n % director_every == 0 and self._resolve_script("director.py").exists():
                try:
                    rc, out = await self._run("director.py", f"--campaign {campaign_q} recommend --session {session}")
                    beat = self._extract_after(out, "## Recommended beat:", max_lines=2)
                    if beat:
                        self._pending.append(f"[director suggests] {beat}")
                except Exception as e:
                    self._log(f"director recommend error: {e}")

    async def on_user_message(self, content: str):
        """Runs the per-round tick on every user message IF the active campaign has
        been started with engine_start() (or require_active_session is off) — see
        _is_active(). Ties the sim's pace to the conversation instead of a real-time
        timer, so nothing runs while the chat is idle and nothing piles up between
        messages, and — critically — nothing runs at all in conversations that have
        nothing to do with this campaign."""
        if not self._is_active():
            return True
        try:
            await self._advance_narrative(content)
        except Exception as e:
            self._log(f"round tick error: {e}")
        return True

    # -------------------------
    #   CONTEXT INJECTION
    # -------------------------

    async def on_system_prompt(self):
        if not self._is_active():
            return (
                "Narrative Engine module is installed but inactive for this conversation "
                "(require_active_session is on, and engine_start() hasn't been called for "
                "campaign '" + self._campaign + "'). Its tools exist but there's no reason to "
                "use them unless this conversation turns out to actually be that tabletop RPG "
                "session — if it does, call engine_start() (or tell the user to run /gm start) "
                "first. Otherwise, ignore this module entirely."
            )
        return (
            "Narrative Engine orchestrator is active: it runs the original open-tabletop-gm "
            "scripts (drama.py, plans.py, npc_drives.py, info_inventory.py, characters.py, "
            "scene_loader.py, epistemic_filter.py, director.py, micro_director.py, narrator.py, "
            "dice.py, combat.py, gm_graph.py, name_registry.py, locations.py, etc) as "
            "subprocesses rather than reimplementing them — the database is the source of "
            "truth, not this model's judgment.\n\n"
            "HARD RULES, not suggestions:\n"
            "1. Never guess or invent something a tool could answer instead — what an NPC "
            "knows, their personality, current pressure/phase, a fact's history. Call recall "
            "(or gm_exec) and use the real answer, even if you're confident you already know it.\n"
            "2. Never decide the outcome of a contested action (persuasion, combat, a check with "
            "real stakes) by what's more dramatic. Call resolve_check and narrate the result you "
            "actually got.\n"
            "3. When you introduce a NAMED character or place that might matter again — has a "
            "name, could recur, is a suspect, is somewhere the party might return to — create it "
            "with npc_new / location_new so it's actually saved (registered, given traits, added "
            "to the graph) instead of only existing in that one message. This does NOT apply to "
            "throwaway color with no name and no future — a passing barmaid, an unnamed guard, a "
            "nondescript alley. If you wouldn't be annoyed to be asked about it again, save it; "
            "if you would be, don't bother.\n\n"
            "Context is kept deliberately thin: only the current scene (place + who's present) "
            "and this round's world events are shown automatically below. Everything else is "
            "retrieved on demand with recall, the same way you'd recall something from memory "
            "rather than having it all sitting in front of you — once per NPC you're about to "
            "voice, recall('world') before narrating a shift in the political or supernatural "
            "situation, or recall(subject, mode='graph') to follow who's connected to whom a hop "
            "or two out. Pin anything a player worked to notice with pin_fact so it can't quietly "
            "fade out of recall later just because time passed — unpinned facts still decay "
            "gently, but pinned ones always show in full. Set the scene with scene_set whenever "
            "the location or cast changes. Call dashboard() for a quick front-facing summary "
            "(counts of NPCs, locations, intrigues, drives, info items) if useful context for "
            "the conversation. Use gm_list_scripts() for the full script index and "
            "gm_exec(script, '--help') for exact syntax before using something unfamiliar."
        )

    async def on_end_prompt(self):
        if not self.config.get("inject_context", True) or not self._is_active():
            return None
        lines = [f"[narrative engine — campaign \"{self._campaign}\", "
                 f"session {self._session()}, round {self._round()}]"]

        scene = self._scene()
        if scene.get("place") or scene.get("present"):
            present = ", ".join(scene.get("present", [])) or "no one tracked"
            lines.append(f"[scene] place: {scene.get('place') or '?'} | present: {present}")

        if self._pending:
            lines.append("[this round]")
            lines.extend(f"  {e}" for e in self._pending)
            self._pending = []

        return "\n".join(lines)

    # =========================================================================
    #   AI TOOLS
    # =========================================================================

    async def gm_list_scripts(self):
        """
        Lists every script found in the scripts folder, with a one-line description
        pulled from each script's own docstring. Call this first if you don't know
        what's available yet.
        """
        d = self._scripts_dir()
        if not d.exists():
            return self.result(f"scripts folder not found: {d}. Add the original open-tabletop-gm scripts there.", success=False)
        found = {}
        for path in sorted(d.glob("*.py")):
            found[path.name] = self._script_description(path)
        if not found:
            return self.result(f"no .py scripts found in {d}.", success=True)
        return self.result(json.dumps(found, indent=2), success=True)

    async def dashboard(self):
        """
        Front-facing summary: counts of NPCs, locations, intrigues, NPC drives, info
        items, and graph nodes/edges for the active campaign, plus current scene,
        session/round, and pinned-fact count. The same content is also continuously
        written to console/dashboard.txt for viewing outside the chat (see the
        console_enabled setting) — this tool exists so the AI (and you, in chat) can
        also just ask for it directly without needing a separate terminal.
        """
        return self.result(await self._build_dashboard(), success=True)

    async def gm_exec(self, script: str, args: str = "--help"):
        """
        Runs one of the original open-tabletop-gm scripts directly, exactly as you would
        on the command line, and returns its output. If you don't know a script's exact
        syntax, call it with args="--help" (the default) first — every script documents
        its own subcommands and flags this way. Note --campaign is a top-level flag on
        these scripts, so it goes BEFORE the subcommand, not after (the active campaign
        name is shown in context).

        Args:
            script: Script filename, e.g. "drama.py" or "plans" (".py" is optional).
            args: The exact command-line arguments, e.g. "--campaign myworld pressure-show"
                or "--campaign myworld pressure-adjust --axis mystery --delta 10".
        """
        rc, out = await self._run(script, args)
        return self.result(out[:6000], success=(rc == 0))

    async def scene_set(self, place: str, present: list = []):
        """
        Declares the current scene — where it's happening and who's present. This is
        the only thing shown automatically in context every turn (see the [scene] line);
        everything else is fetched on demand with recall(). Call this whenever the
        location or the cast of present characters changes.

        Args:
            place: Short label for the location, e.g. "the Rusty Anchor tavern".
            present: Names of NPCs/PCs present, e.g. ["Bob", "pc:aldric"]. Leave empty
                for a place with no one notable present yet.
        """
        present = present or []
        self._meta["scene"][self._campaign] = {"place": place, "present": present}
        self._meta.save()
        return self.result(f"scene set: {place} — present: {', '.join(present) or 'no one'}", success=True)

    async def recall(self, subject: str, mode: str = "knowledge", hops: int = 2, limit: int = 8):
        """
        On-demand memory search — the equivalent of an NPC (or the world) recalling
        something, instead of it all being preloaded into context. Call this as many
        times as you need before responding: once per character you're about to voice,
        once for the world if a scene needs to reflect current pressure/politics, once
        in graph mode if you want to follow a character's connections, etc.

        Args:
            subject: An NPC/PC name or id, e.g. "bob" or "pc:aldric" — or the literal
                string "world" for overall campaign state (pressure + phase), which
                ignores mode/hops/limit.
            mode: "knowledge" (default) — what this character personally knows plus
                their profile/traits, if generated. Facts are ranked by salience
                (confidence x recency x relevance to who's currently in the scene),
                not dumped in raw chronological order, and capped at `limit`.
                "gap" — facts the party knows that this character doesn't (their blind
                spot). "edge" — facts this character knows that the party doesn't
                (their information advantage). "graph" — traverses the relationship
                graph outward from this character (needs gm_graph.py and a graph
                already built for this campaign; use hops to control how far).
            hops: Only used in graph mode. How many relationship-steps to traverse
                outward from subject — 1 is direct connections only, 2 (default)
                includes connections-of-connections. Clamped to 1-3 to keep the
                returned subgraph readable rather than pulling in half the campaign.
            limit: Only used in knowledge mode. Max number of facts to show after
                ranking by salience. Clamped to 3-20.
        """
        self._recall_log.append(f"{subject}/{mode}")
        self._recall_log = self._recall_log[-20:]
        self._log(f"[{self._campaign}] recall({subject}, {mode})")

        if subject.strip().lower() == "world":
            parts = []
            for script, args in (
                ("drama.py", f"--campaign {shlex.quote(self._campaign)} state-show"),
                ("drama.py", f"--campaign {shlex.quote(self._campaign)} pressure-show"),
            ):
                if self._resolve_script(script).exists():
                    rc, out = await self._run(script, args)
                    if out:
                        parts.append(out)
            return self.result("\n\n".join(parts) or "drama.py not found in scripts folder.", success=bool(parts))

        campaign_q = shlex.quote(self._campaign)

        if mode == "graph":
            if not self._resolve_script("gm_graph.py").exists():
                return self.result("gm_graph.py not found in scripts folder.", success=False)
            hops = max(1, min(3, int(hops or 2)))
            rc, out = await self._run(
                "gm_graph.py", f"subgraph --campaign {campaign_q} --seed {shlex.quote(subject)} --hops {hops}"
            )
            return self.result(out, success=(rc == 0))

        npc_id = self._norm_id(subject)

        if mode == "gap":
            if not self._resolve_script("info_inventory.py").exists():
                return self.result("info_inventory.py not found in scripts folder.", success=False)
            rc, out = await self._run("info_inventory.py", f"--campaign {campaign_q} info-gap --npc {shlex.quote(npc_id)}")
            return self.result(out, success=(rc == 0))

        if mode == "edge":
            if not self._resolve_script("info_inventory.py").exists():
                return self.result("info_inventory.py not found in scripts folder.", success=False)
            rc, out = await self._run("info_inventory.py", f"--campaign {campaign_q} info-gap --npc {shlex.quote(npc_id)} --party")
            return self.result(out, success=(rc == 0))

        # mode == "knowledge": combine what they know (salience-ranked) + their profile
        limit = max(3, min(20, int(limit or 8)))
        parts = []
        if self._resolve_script("info_inventory.py").exists():
            rc, out = await self._run("info_inventory.py", f"--campaign {campaign_q} what-knows --npc {shlex.quote(npc_id)}")
            if out:
                parts.append(self._rank_by_salience(out, limit))
        if self._resolve_script("characters.py").exists():
            rc, out = await self._run("characters.py", f"--campaign {campaign_q} show --npc {shlex.quote(npc_id)}")
            if out:
                parts.append(out)
        if not parts:
            return self.result(f"nothing found for '{npc_id}' — check info_inventory.py/characters.py are present and the id is right.", success=False)
        return self.result("\n\n".join(parts), success=True)

    async def pin_fact(self, fact_id: str, note: str = ""):
        """
        Marks a fact as important enough that it should never fade out of recall()
        regardless of age — use this the moment a player does real work to notice
        something (a clever deduction, a subtle clue), so it doesn't quietly get
        buried under newer, less-relevant facts a few sessions later. Pinned facts
        always show in full in recall's knowledge mode, never counted against limit.

        Args:
            fact_id: The fact's id, e.g. "f003" (matches info_inventory.py's ids).
            note: A short reminder of why this was pinned, e.g. "player deduced the
                ledger connection from the wax seal — protect this".
        """
        self._meta["pins"].setdefault(self._campaign, {})[fact_id] = note or "(no note)"
        self._meta.save()
        return self.result(f"pinned {fact_id} for campaign '{self._campaign}'.", success=True)

    async def unpin_fact(self, fact_id: str):
        """
        Removes a fact's pin, letting it fade normally with the rest of recall's
        salience ranking again.

        Args:
            fact_id: The fact's id to unpin.
        """
        removed = self._meta["pins"].get(self._campaign, {}).pop(fact_id, None)
        self._meta.save()
        return self.result(f"unpinned {fact_id}." if removed is not None else f"{fact_id} wasn't pinned.", success=True)

    async def npc_new(self, name: str, archetype: str = ""):
        """
        Properly creates a new NAMED NPC instead of just inventing one in prose — use
        this whenever you introduce a character who might matter again (has a name,
        will recur, is a suspect, etc), not for throwaway background color like "a
        barmaid" or "a passing guard". Registers the name (so it can't collide with
        another NPC later), generates deterministic personality traits, and adds it
        to the relationship graph if one exists — so the character is something you
        can recall() and connect to others later instead of a detail that only ever
        existed in one message.

        Args:
            name: The NPC's display name, e.g. "Bob Ferro".
            archetype: One of the engine's archetypes (schemer, warrior, diplomat,
                fanatic, opportunist, mystic, survivor, noble...) if you know it.
                Leave empty to let characters.py pick one.
        """
        slug = "_".join(name.lower().replace("-", " ").split())
        campaign_q = shlex.quote(self._campaign)
        steps = []

        if self._resolve_script("name_registry.py").exists():
            rc, out = await self._run("name_registry.py", f"check {shlex.quote(name)} --json")
            steps.append(f"name check: {out.strip()[:200]}")
            try:
                if json.loads(out).get("is_duplicate"):
                    return self.result(json.dumps({
                        "npc_id": slug, "steps": steps,
                        "note": f"'{name}' already exists — not overwriting. Use recall('{slug}') to see "
                                 "the existing NPC, or pick a different name.",
                    }, indent=2), success=False)
            except (ValueError, KeyError):
                pass  # couldn't parse — proceed best-effort rather than blocking on a format change
            rc, out = await self._run(
                "name_registry.py",
                f"add --name {shlex.quote(name)} --type npc --campaign {campaign_q} --session {self._session()}",
            )
            steps.append(f"name registered: {out.strip()[:200]}" if rc == 0 else f"name registry error: {out.strip()[:200]}")

        if self._resolve_script("characters.py").exists():
            arch_flag = f" --archetype {shlex.quote(archetype)}" if archetype else ""
            rc, out = await self._run("characters.py", f"--campaign {campaign_q} generate --npc {shlex.quote(slug)}{arch_flag}")
            steps.append(out.strip()[:400] if rc == 0 else f"characters.py error: {out.strip()[:200]}")
        else:
            steps.append("characters.py not found — no traits generated.")

        if self._resolve_script("gm_graph.py").exists():
            rc, out = await self._run(
                "gm_graph.py", f"add-node --campaign {campaign_q} --type npc --id {shlex.quote(slug)} --name {shlex.quote(name)}"
            )
            steps.append(f"graph node added" if rc == 0 else f"graph error: {out.strip()[:200]}")

        self._log(f"[{self._campaign}] npc_new({name}): {steps}")
        return self.result(json.dumps({"npc_id": slug, "steps": steps}, indent=2), success=True)

    async def location_new(self, place: str, description: str = ""):
        """
        Properly creates a new NAMED location instead of just inventing one in prose —
        use this for anywhere the party might return to or that matters to the plot,
        not for a location mentioned once in passing. Registers it with
        locations.py (sensory/atmosphere fields get sane defaults) and adds it to the
        relationship graph if one exists, so it can be recalled and connected to NPCs
        or events later.

        Args:
            place: The location's name, e.g. "the Rusty Anchor tavern".
            description: A short description, if you have one yet.
        """
        slug = "_".join(place.lower().replace("-", " ").split())
        campaign_q = shlex.quote(self._campaign)
        steps = []

        if self._resolve_script("locations.py").exists():
            payload = json.dumps({"id": slug, "name": place, "description": description})
            rc, out = await self._run("locations.py", f"--campaign {campaign_q} add {shlex.quote(payload)}")
            steps.append(out.strip()[:300] if rc == 0 else f"locations.py error: {out.strip()[:200]}")
        else:
            steps.append("locations.py not found — nothing persisted for this location.")

        if self._resolve_script("gm_graph.py").exists():
            rc, out = await self._run(
                "gm_graph.py", f"add-node --campaign {campaign_q} --type location --id {shlex.quote(slug)} --name {shlex.quote(place)}"
            )
            steps.append("graph node added" if rc == 0 else f"graph error: {out.strip()[:200]}")

        self._log(f"[{self._campaign}] location_new({place}): {steps}")
        return self.result(json.dumps({"location_id": slug, "steps": steps}, indent=2), success=True)

    async def _roll(self, notation: str, modifier: int = 0, advantage: bool = False, disadvantage: bool = False):
        """Internal helper: shells out to dice.py for the actual random roll — this
        module never generates its own randomness, so a roll here is exactly as real
        as one made from the command line. Returns (total_or_None, raw_output)."""
        if not self._resolve_script("dice.py").exists():
            return None, "dice.py not found in scripts folder."
        expr = notation.strip()
        if modifier:
            expr += f"{modifier:+d}"
        if advantage:
            expr += " adv"
        elif disadvantage:
            expr += " dis"
        rc, out = await self._run("dice.py", f"{expr} --silent")
        if rc != 0:
            return None, out
        try:
            return int(out.strip().splitlines()[-1]), out
        except (ValueError, IndexError):
            return None, out

    async def resolve_check(self, actor_mod: int = 0, target: str = "", opposed_mod: str = "",
                             advantage: bool = False, disadvantage: bool = False, notation: str = "1d20"):
        """
        Generic deterministic contested-action resolver. Rolls real dice via dice.py
        and compares the total against a target, instead of narration alone deciding
        success or failure — this is the actual mechanical resolve step for ANY
        contested action, not just combat: a persuasion attempt against an NPC's
        evidence/suspicion, a stealth check against a guard's perception, an ability
        check against a stated DC. Call this before narrating the outcome of anything
        with real stakes, then narrate the result you got rather than the result that
        would be more dramatic. Every call is logged (see /gm checks) so there's an
        audit trail of what was actually rolled versus narrated freely.

        Args:
            actor_mod: The acting character's relevant modifier (skill bonus, stat,
                or a value you've derived from their sheet/traits).
            target: A flat DC/target number to beat, as a string (e.g. "15"), if this
                is a stated difficulty rather than an opposed roll. Provide this OR
                opposed_mod, not both — leave the other one as an empty string.
            opposed_mod: The opposing side's modifier, as a string (e.g. "5"), if this
                is opposed rather than a flat DC (e.g. an NPC's Insight bonus, or a
                value derived from their current suspicion/evidence score). The module
                rolls a matching die for the opposition too, rather than treating it
                as a static number.
            advantage / disadvantage: Whether the actor's roll has advantage or
                disadvantage. Never both.
            notation: The base die, default "1d20" for a standard d20 check. Change
                for other systems, e.g. "2d6".
        """
        target_int = int(target) if str(target).strip() else None
        opposed_int = int(opposed_mod) if str(opposed_mod).strip() else None
        if target_int is None and opposed_int is None:
            return self.result("need either target (a flat DC) or opposed_mod (an opposing roll) to resolve against.", success=False)

        actor_total, actor_raw = await self._roll(notation, actor_mod, advantage, disadvantage)
        if actor_total is None:
            return self.result(f"couldn't roll: {actor_raw}", success=False)

        if opposed_int is not None:
            target_total, target_raw = await self._roll(notation, opposed_int)
            if target_total is None:
                return self.result(f"couldn't roll opposition: {target_raw}", success=False)
        else:
            target_total, target_raw = target_int, f"flat DC {target_int}"

        success = actor_total >= target_total
        margin = actor_total - target_total
        entry = f"{notation}{actor_mod:+d} ({actor_total}) vs {target_total} -> {'success' if success else 'fail'} (margin {margin:+d})"
        self._check_log.append(entry)
        self._check_log = self._check_log[-20:]
        self._log(f"[{self._campaign}] resolve_check: {entry}")

        return self.result(json.dumps({
            "actor_total": actor_total, "actor_roll_detail": actor_raw.strip(),
            "target_total": target_total, "target_detail": str(target_raw).strip(),
            "success": success, "margin": margin,
        }, indent=2), success=True)

    async def campaign_switch(self, name: str):
        """
        Switches the active campaign that gm_exec/context injection default to.

        Args:
            name: The campaign name to make active.
        """
        self._campaign = name
        return self.result(f"active campaign set to '{name}'.", success=True)

    async def engine_start(self):
        """
        Marks this conversation as an active game session for the current campaign.
        Only matters if require_active_session is on (the default) — until this is
        called, the per-round tick, context injection, and the full system prompt
        all stay off, so unrelated conversations aren't affected. Call this once,
        near the start of an actual play session; it persists until engine_stop().
        """
        self._meta["active"][self._campaign] = True
        self._meta.save()
        self._log(f"[{self._campaign}] engine started")
        return self.result(f"engine started for campaign '{self._campaign}' — ticking and context injection are now on.", success=True)

    async def engine_stop(self):
        """
        Turns off the per-round tick and context injection for the current campaign
        again (the opposite of engine_start). Use this when a play session ends.
        """
        self._meta["active"][self._campaign] = False
        self._meta.save()
        self._log(f"[{self._campaign}] engine stopped")
        return self.result(f"engine stopped for campaign '{self._campaign}'.", success=True)

    async def session_advance(self):
        """
        Advances the active campaign's session counter by one. Several scripts
        (npc_drives, info_inventory, plans) take a --session number; call this at
        the end of a game session so subsequent calls default to the right value.
        """
        n = self._advance_session()
        return self.result(f"session advanced to {n} for campaign '{self._campaign}'.", success=True)

    # -------------------------
    #   USER-FACING COMMANDS
    # -------------------------

    @core.module.command("gm", help={
        "list": "Lists available scripts in the scripts folder",
        "start": "Marks this conversation as an active game session (required before anything ticks, if require_active_session is on)",
        "stop": "Turns the active session off again for this campaign",
        "status": "Shows whether the engine is active, and for which campaign",
        "campaign <name>": "Switches the active campaign",
        "session": "Shows the current session number",
        "session advance": "Advances the session counter by one",
        "scene": "Shows the current place/present scene",
        "recalls": "Shows the last few recall() calls this session",
        "checks": "Shows the last few resolve_check() calls this session",
        "pins": "Lists pinned facts for the active campaign",
        "dashboard": "Shows the live text dashboard (also continuously written to console/dashboard.txt)",
        "console": "Shows where the separate debug console files live, for tailing from your own terminal",
        "<script> [args...]": "Runs a script directly, e.g. /gm drama.py --campaign myworld pressure-show",
    })
    async def gm_command(self, args: list):
        if not args or args[0] == "list":
            d = self._scripts_dir()
            if not d.exists():
                return f"scripts folder not found: {d}"
            names = sorted(p.name for p in d.glob("*.py"))
            return "scripts: " + ", ".join(names) if names else f"no scripts found in {d}"

        if args[0] == "start":
            self._meta["active"][self._campaign] = True
            self._meta.save()
            return f"engine started for campaign '{self._campaign}' — ticking and context injection are now on."

        if args[0] == "stop":
            self._meta["active"][self._campaign] = False
            self._meta.save()
            return f"engine stopped for campaign '{self._campaign}'."

        if args[0] == "status":
            gate = self.config.get("require_active_session", True)
            active = self._is_active()
            return (f"campaign: {self._campaign} | active: {active} "
                    f"(require_active_session: {gate}) | session: {self._session()} | round: {self._round()}")

        if args[0] == "campaign" and len(args) > 1:
            self._campaign = args[1]
            return f"active campaign set to '{args[1]}'"

        if args[0] == "session":
            if len(args) > 1 and args[1] == "advance":
                return f"session advanced to {self._advance_session()}"
            return (f"current session: {self._session()} (campaign '{self._campaign}'), "
                    f"chat round: {self._round()}")

        if args[0] == "scene":
            scene = self._scene()
            if not scene.get("place") and not scene.get("present"):
                return "no scene set yet — call scene_set or /gm won't show a [scene] line in context."
            return f"place: {scene.get('place') or '?'} | present: {', '.join(scene.get('present', [])) or 'no one'}"

        if args[0] == "recalls":
            return "recent recalls: " + (", ".join(self._recall_log) or "none yet")

        if args[0] == "checks":
            return "recent checks:\n" + ("\n".join(self._check_log) if self._check_log else "none yet")

        if args[0] == "pins":
            pins = self._pins()
            if not pins:
                return f"no pinned facts for campaign '{self._campaign}'."
            return "\n".join(f"{fid}: {note}" for fid, note in pins.items())

        if args[0] == "dashboard":
            return await self._build_dashboard()

        if args[0] == "console":
            if not self.config.get("console_enabled", True):
                return "console_enabled is off — turn it on in settings to get a separate live log/dashboard."
            d = self._console_dir()
            return (f"Console files live at: {d}\n"
                    f"  tail -f {d / 'console.log'}          — live event log\n"
                    f"  watch -n 2 cat {d / 'dashboard.txt'}  — live-refreshing dashboard\n"
                    f"  echo 'dashboard' >> {d / 'commands.in'}   — drop a command\n"
                    f"  tail -f {d / 'commands.out'}          — see command replies\n"
                    f"All separate from this chat — none of this needs the webUI open.")

        script, rest = args[0], args[1:]
        rc, out = await self._run(script, " ".join(rest))
        return out[:3000] if out else f"(no output, exit code {rc})"
