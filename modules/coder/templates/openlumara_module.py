import core

class ExampleModule(core.module.Module):
    """
    A sample module demonstrating core features.
    This module docstring shows up as the module description all over the framework!
    """

    # settings defined here will show up in all channels that support it (such as the webUI)
    # for the user to change as they see fit
    settings = {
        "enable_system_prompt": {
            "description": "Whether to inject a custom system prompt defined by this module",
            "default": False
        },
        "sysprompt_style": {
            "type": "select",
            "description": "What system prompt to inject",
            "default": "standard",
            "options": {
                "standard": "Just your run-of-the-mill system prompt",
                "uwu": "Makes your AI say uwu all the time!",
                "nag": "Makes your AI nag you a lot"
            }
        },
        "allow_ping": {
            "description": "Whether to allow the AI to use the ping tool",
            "default": True
        }
    }

    # list of dependencies that the module needs in order to work.
    # this is an example, leave empty if no dependencies are needed
    dependencies = ["pytest"]

    async def on_ready(self):
        """ALWAYS use this instead of the class constructor (__init__) as it runs at the right time during the framework's startup sequence."""

        await self.channel.push("Example Module is online!")

        self.user_msg_counter = 0

        if not self.config.get("allow_ping"):
            # disabled_tools is a special list that tells the framework to disable that tool
            self.disabled_tools.append("ping")

    async def on_shutdown(self):
        """Runs when the framework shuts down or restarts, or the module is reloaded. Use to clean up anything the module may have set up"""
        self.disabled_tools = []

    async def on_background(self):
        """If this is present, the framework will auto-start this function as an asyncio task to run in the background. Use for contineous background monitoring, background tasks, scheduled reminders, event loops, etc"""
        pass

    async def on_system_prompt(self):
        match self.config.get("sysprompt_style"):
            case "standard":
                return "You are an expert in everything related to Example Module."
            case "uwu":
                return "You MUST say uwu a lot"
            case "nag":
                return "Nag the user about their taxes"
            case _:
                return None

    async def on_end_prompt(self):
        """Will insert its return value into the end of the context (after the conversation history) if something is returned (defaults to None). Useful for things that change frequently, such as displaying what channel the user is currently in. Using the prompt at the end of conversation history means history does not have to be reprocessed if the prompt changes."""
        return f"current style: {self.config.get('sysprompt_style')}"

    async def on_user_message(self, content: str):
        """Runs on every message the user sends. Can be used to do whatever you want with the content of a user's sent message."""
        self.log("user message intercepted", f"message: {content}")
        self.user_msg_counter += 1

    async def on_assistant_message(self, content: str):
        """Runs on every message received from the AI assistant. Can be used to do whatever you want with the content of a message received from the AI."""
        self.log("AI message intercepted", f"message: {content}")

    async def on_message_inject(self):
        """Will inject whatever string you return here into the user's message. Very useful for adding extra data that should persist in history. For example, when injecting timestamps, instead of using the end prompt for it (which would only show the AI what time it currently is), it can now give the AI a sense of when every message was sent."""
        return f"this is user message {self.user_msg_counter}"

    async def on_install(self):
        """This runs after the module's dependencies are installed by the framework's auto-installer. Use it for post-installation hooks"""
        pass

    async def on_uninstall(self):
        """This runs after the module's dependencies are uninstalled by the framework's auto-installer. Use it for post-uninstallation hooks."""
        pass

    # tools are simply class methods. the framework will read the definition
    # and translate the name, arguments and docstring to a tool usable by the AI
    # tools don't need a special decorator
    async def ping(self, latency: int):
        """
        Simulates a ping to the user.

        Args:
            latency: The latency to set for the simulated ping
        """
        if not self.config.get("allow_ping"):
            return self.result("Ping is disabled for security", success=False)

        # always return results using self.result, which standardizes the json output emitted by toolcalls
        return self.result(f"Pong! latency: {latency}", success=True)

    # commands are usable by the user only,
    # this one for example gives the user a `/ping` command,
    # with an optional "cookie" argument
    @core.module.command("ping", help={
        "": "Checks if the module is responsive",
        "cookie": "gives you a cookie"
    })
    async def ping_command(self, args: list):
        if not args:
            return "Pong!"
        elif len(args) >= 1 and args[1] == "cookie":
            return "heres a cookie! :3"
