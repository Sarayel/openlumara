import core

class Modules(core.module.Module):
    """Helps your AI manage your modules"""

    settings = {
        "allow_ai_to_toggle": {
            "description": "Allow your AI to enable/disable modules for you. WARNING: very insecure and unsafe when used in public instances! Only use for private instances, and even then, keep the dangers of prompt injection in mind.",
            "default": False,
            "unsafe": True
        },
        "insert_system_prompt": {
            "description": "Whether to insert the list of enabled/disabled modules into the system prompt. Will make the AI aware of what modules are enabled/disabled at all times.",
            "default": True
        }
    }

    async def on_system_prompt(self):
        module_list = {
            "enabled": ", ".join(core.config.get("modules", "enabled", default=[])),
            "disabled": ", ".join(core.config.get("modules", "disabled", default=[]))
        }
        return str(module_list)

    async def toggle(self, name: str):
        module_name = name.lower().strip()
        if name == "modules":
            return "module manager can only be manually turned off by using the settings dialog, the `/module` command, or editing the config file. user needs to know what you're doing!"

        if self.manager.toggle_module(name):
            return self.result("Module has been toggled. Remind user to use `/restart` to apply changes")
        else:
            return self.result("That module does not exist!", success=false)

    async def get_settings_docs(self, module_name: str):
        """Returns the settings structure for any given module. Use this if user wants to know how to set up or use a specific module."""

        structure = core.config.get_module_structure()

        if module_name not in structure:
            return self.result("That module does not exist", success=False)

        return structure.get(module_name)
