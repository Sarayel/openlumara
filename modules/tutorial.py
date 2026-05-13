import core

class Tutorial(core.module.Module):
    """Guides you through openlumara if you're new!"""

    async def on_system_prompt(self):
        output = []

        disable_instr = "by typing `/module tutorial`"
        webui_instr = ""
        if "webui" in self.manager.channels:
            network_mode = self.manager.channels["webui"].config.get("network_mode")
            match network_mode:
                case "local":
                    webui_host = "127.0.0.1"
                case "internet":
                    webui_host = "0.0.0.0"
                case "custom":
                    webui_host = self.manager.channels["webui"].config.get("custom_host")
                case _:
                    webui_host = "127.0.0.1"

            webui_port = self.manager.channels["webui"].config.get("port")
            webui_url = f"http://{webui_host}:{webui_port}"

            webui_instr = f"""
Ask the user if they need any help with setting up openlumara. If user says yes, follow these instructions: To set up openlumara, the user needs to open the webui's settings dialog (gear icon at the top of the chat window in the webUI). If the user is not currently in the webUI, tell them to open the webUI at `{webui_url}`. Then inside the settings dialog, the most important thing is that the user can toggle channels and modules, and set up settings to their liking per channel and per module.
            """.strip()
            disable_instr += " or through the WebUI's settings dialog, in the Modules section"

        output.append(f"""
ALWAYS upon the first message of a conversation, tell the user how to use openlumara by referencing the channel instructions within your system prompt. Present the information in a user-friendly, easy to understand way.

The `docs` module is active by default and gives you access to documentation to answer any questions the user might have about openlumara! If user asks any questions about openlumara that are not covered by this prompt, use the docs module to get documentation about it. For module-specific questions, use modules_get_settings_docs instead.

Tell the user with extra emphasis and attention-grabbing emojis that this tutorial message can be turned off by disabling the tutorial module, which the user can do {disable_instr}.

Further, tell the user:
- Modules can insert system prompts like this tutorial, add new commands for the user to use, do things when the user or the AI sends a message, run anything in the background, and other cool stuff.
- A module called `module_maker` is available which can automatically code modules for the user, replacing the `SKILL.md` system used in many other popular AI agents.

{webui_instr}
""")

        return "\n".join(output)
