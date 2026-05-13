import core
import os
import modules.sandboxed_files

class ModuleMaker(modules.sandboxed_files.SandboxedFiles):
    """Lets your AI create OpenLumara modules for you"""

    settings = {}
    unsafe = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sandbox_path = os.path.abspath(core.config.get("user_modules", "path"))

    def _get_module_path(self, name):
        return self._get_sandbox_path(f"{name}.py")

    async def create(self, name: str, python_code: str):
        """Creates a custom module for openlumara. This can grant you, the AI, new tools to use! Before using this, ALWAYS call docs_read(topic=openlumara_dev_docs, subject=core/module)"""
        with open(self._get_module_path(name), "w") as f:
            f.write(python_code)
        return self.result("Code written! Remind user to enable the module and restart the server (using `/restart` or the restart button in the webUI settings panel)")

    async def read(self, name: str):
        """read an already-created module"""

        if not os.path.exists(self._get_module_path(name)):
            return self.result("Module does not exist!", success=False)

        content = None
        with open(self._get_module_path(name), "r") as f:
            content = f.read()
        return self.result(content)

    async def edit(self, name: str, python_code: str):
        """edits an existing module. ALWAYS call read_module() first before editing a module!"""
        if not os.path.exists(self._get_module_path(name)):
            return self.result("Module does not exist!", success=False)

        with open(self._get_module_path(name), "w") as f:
            f.write(python_code)

        return self.result("Code written! Remind user to enable the module and restart the server (using `/restart` or the restart button in the webUI settings panel)")
