# Core: The Module System (`core.Module`)

OpenLumara is built on a highly extensible plugin architecture. The `Module` class is the base for all additional functionality, allowing developers to easily inject new capabilities into the AI agent.

## Module Architecture

Every module is a Python class that inherits from `core.Module`. Modules are loaded dynamically by the `Manager` and can interact with the rest of the system through the `Manager` and the active `Channel`.

## Key Capabilities

### 1. Prompt Injection
Modules can influence the AI's behavior by injecting text into the context window at specific points:
- **`on_system_prompt()`**: Adds content to the very beginning of the system prompt. This is ideal for defining identity, rules, or framework awareness.
- **`on_end_prompt()`**: Adds content to the end of the conversation history (just before the next user message). This is perfect for dynamic information like the current time or date, as it doesn't require reprocessing the entire history.

### 2. Tool Provisioning
Modules can expose Python functions as "tools" that the AI can call.
- Any method in a module can be converted into a tool.
- The `Manager` uses inspection to automatically generate the JSON schema required for OpenAI-compatible function calling.
- Docstrings are used to provide instructions to the AI about what the tool does and what its arguments are.

### 3. Event Hooks
Modules can react to events happening within the system:
- **`on_ready()`**: Triggered once when the module is successfully loaded.
- **`on_background()`**: Runs a continuous background task (e.g., a scheduler or a monitor).
- **`on_user_message(content)`**: Triggered whenever the user sends a message.
- **`on_assistant_message(content)`**: Triggered whenever the AI sends a response.

### 4. Command System
Modules can register custom commands that bypass the AI entirely.
- Using the `@core.module.command(name="my_cmd")` decorator, a module can define a command.
- Commands are triggered by the user via the configured command prefix (e.g., `/my_cmd`).

## Implementation Example

```python
import core

class MyAwesomeModule(core.module.Module):
    """
    A sample module demonstrating core features.
    """
    settings = {
        "enable_system_prompt": {
            "description": "Whether to enable the awesome injection into the system prompt!",
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
        }
    }

    async def on_ready(self):
        await self.manager.channel.push("Awesome Module is online!")

    async def on_system_prompt(self):
        match self.config.get("sysprompt_style"):
            case "standard":
                return "You are an expert in everything related to Awesome Module."
            case "uwu":
                return "You MUST say uwu a lot"
            case "nag":
                return "nag the user about their taxes"
            case _:
                return None

    @core.module.command("ping", help={
        "": "Checks if the module is responsive",
        "cookie": "gives you a cookie"
    })
    async def ping_command(self, args: list):
        if not args:
            return "Pong!"
        elif len(args) >= 1 and args[1] == "cookie":
            return "heres a cookie! :3"

    async def my_useful_tool(self, input_text: str):
        """
        This is a tool the AI can use.
        
        Args:
            input_text: The text to process.
        """
        return self.result(f"Processed: {input_text}", success=True)
```

## Module Configuration

Each module can define its own `settings` dictionary. These settings are:
1.  Defined in the module class.
2.  Persisted in the `config.yml` file.
3.  Accessible via `self.config.get("key")`.
