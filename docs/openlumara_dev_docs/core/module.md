# Core: The Module System (`core.Module`)

OpenLumara is built on a highly extensible plugin architecture. The `Module` class is the base for all additional functionality, allowing developers to easily inject new capabilities into the AI agent.

## Module Architecture

Every module is a Python class that inherits from `core.Module`. Modules are loaded dynamically by the `Manager` and can interact with the rest of the system through the `Manager` and the active `Channel`.

## Module Class Attributes

| Attribute | Type | Description |
| :--- | :--- | :--- |
| `settings` | `dict` | Default settings that can be changed by the user. |
| `unsafe` | `bool` | If `True`, marks the module as risky to enable in supported settings UIs. |
| `dependencies` | `list` | List of Python dependencies that need to be installed for the module to work. |

## Module Instance Attributes

| Attribute | Type | Description |
| :--- | :--- | :--- |
| `manager` | `Manager` | Reference to the OpenLumara manager instance. |
| `channel` | `Channel` | The active channel (set by the channel base class via `_set_as_active_channel()`). |
| `name` | `str` | Shorthand alias for the module's snake_case name (from `core.modules.get_name()`). |
| `disabled_tools` | `list` | List of tool names to disable. Alter this in `__init__()` to selectively disable tools. |
| `config` | `ModuleConfig` | Configuration wrapper for accessing/setting module settings. |

## Module Configuration (`ModuleConfig`)

Each module can define its own `settings` dictionary. These settings are:
1.  Defined in the module class as a `settings` dict.
2.  Persisted in the `config.yml` file under `modules/settings/<module_name>` (or `user_modules/settings/<module_name>` for user modules).
3.  Accessible via `self.config.get("key")` or `self.config.get("nested", "key")` for nested keys.

The `ModuleConfig` class provides two main methods:
- **`get(*keys, default=None)`**: Retrieves a config value by key(s). Supports nested traversal. Returns `default` if the key doesn't exist.
- **`set(key, value)`**: Sets a config value. Returns `None` if the key doesn't exist in the config.

## Key Capabilities

### 1. Prompt Injection
Modules can influence the AI's behavior by injecting text into the context window at specific points:
- **`on_system_prompt()`**: Adds content to the very beginning of the system prompt. This is ideal for defining identity, rules, or framework awareness.
- **`on_end_prompt()`**: Adds content to the end of the conversation history (just before the next user message). This is perfect for dynamic information like the current time or date, as it doesn't require reprocessing the entire history.
- **`on_message_inject()`**: Injects content directly into the user's message. Useful for adding data that should persist in history, such as timestamps, giving the AI a sense of when every message was sent.

### 2. Tool Provisioning
Modules can expose Python functions as "tools" that the AI can call.
- Any method in a module can be converted into a tool.
- The `Manager` uses inspection to automatically generate the JSON schema required for OpenAI-compatible function calling.
- Docstrings are used to provide instructions to the AI about what the tool does and what its arguments are.
- Use `self.result(data, success=True)` to return a unified response format: `{"status": "success"|"error", "content": data}`.

### 3. Event Hooks
Modules can react to events happening within the system:
- **`on_ready()`**: Triggered once when the module is successfully loaded. Use this instead of `__init__()` for async initialization.
- **`on_shutdown()`**: Triggered when the module is shut down or reloaded (e.g., when config settings are changed).
- **`on_background()`**: Runs a continuous background task (e.g., a scheduler or monitor). The framework checks if the method is an empty coroutine (only `pass`, `...`, or docstrings) — if so, it won't be started as a background task.
- **`on_user_message(content)`**: Triggered whenever the user sends a message.
- **`on_assistant_message(content)`**: Triggered whenever the AI sends a response.
- **`on_install()`**: Triggered when the auto-installer installs the module's dependencies.
- **`on_uninstall()`**: Triggered when the auto-installer uninstalls the module's dependencies.

### 4. Logging
- **`log(category, message)`**: Alias for `self.manager.log(category, message)`.

### 5. Command System
Modules can register custom commands that bypass the AI entirely.
- Using the `@core.module.command(name="my_cmd", help="...", send_to_ai=False)` decorator, a module can define a command.
- Commands are triggered by the user via the configured command prefix (e.g., `/my_cmd`).
- **`name`**: The command name (auto-converted to lowercase).
- **`help`**: A string description or dict for subcommand help. Falls back to the first line of the function's docstring if not provided.
- **`send_to_ai`**: If `False` (default), the command is marked as "temporary" and won't be sent to the AI. If `True`, the command is persistent.
- Command handlers are automatically discovered via `__init_subclass__()` and registered in `_command_registry`.

Helper functions:
- **`command_is_temporary(command_name)`**: Checks if a command is marked as temporary.
- **`get_command_description(command_name)`**: Gets the help description for a command.
- **`is_empty_coroutine(func)`**: Checks if a coroutine function body is effectively empty (only `pass`, `...`, or docstrings).

## Implementation Example

```python
# You must ALWAYS import core at the very top of the file
import core

class MyAwesomeModule(core.module.Module):
    """
    A sample module demonstrating core features.
    This module docstring shows up in the WebUI!
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
        },
        "allow_ping": {
            "description": "Whether to allow the AI to use the ping tool",
            "default": True
        }
    }

    async def on_ready(self):
        await self.manager.channel.push("Awesome Module is online!")
        
        if not self.config.get("allow_ping"):
            # disabled_tools is defined in core.module.Module and tells the framework to disable that tool
            self.disabled_tools.append("ping")

    async def on_system_prompt(self):
        match self.config.get("sysprompt_style"):
            case "standard":
                return "You are an expert in everything related to Awesome Module."
            case "uwu":
                return "You MUST say uwu a lot"
            case "nag":
                return "Nag the user about their taxes"
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
            
    async def ping(self, latency: int):
        """
        This is a tool the AI can use.
        Simulates a ping to the user.
        
        Args:
            latency: The latency to set for the simulated ping
        """
        if not self.config.get("allow_ping"):
            return self.result("Ping is disabled for security", success=False)
        
        return self.result(f"Pong! latency: {latency}", success=True)
```

## Module Lifecycle

1. **Loading**: `core.modules.load()` discovers and imports the module, finding classes that inherit from `core.Module`.
2. **Instantiation**: The `Module.__init__()` method sets up `manager`, `channel`, `name`, `disabled_tools`, and `config`.
3. **Command Registration**: `Module.__init_subclass__()` scans for `@command`-decorated methods and registers them.
4. **Startup**: `Manager` calls `_check()` (if defined), then `_start()` which invokes `on_ready()` and optionally `on_background()`.
5. **Runtime**: Event hooks (`on_user_message`, `on_assistant_message`, etc.) fire as events occur.
6. **Shutdown/Reload**: `on_shutdown()` is called to clean up.

---

# Core: How modules are loaded (`core.Modules`)
The `core.modules` file provides the engine for OpenLumara's extensibility. It is responsible for dynamically discovering, importing, and identifying the various modules and channels that make up the system.

## Dynamic Discovery

Instead of hardcoding every possible module or channel, OpenLumara uses filesystem scanning to find them. This allows users to simply drop a new `.py` file into the `modules/`, `user_modules/`, or `channels/` directory, and the system will automatically pick it up on the next restart. Modules created by Lumara or by the user must be placed in the `user_modules/` directory.

The `load()` function performs the following steps:
1.  **Package Scanning**: Uses `pkgutil` to iterate through all sub-modules within a given package (like `modules/` or `channels/`).
2.  **Conditional Import**: Only imports modules that are present in the `filter` list (e.g., only the modules enabled in `config.yml`).
3.  **Class Inspection**: Once a module is imported, it scans the module for any classes that inherit from a specified `base_class` (like `core.module.Module` or `core.channel.Channel`).
4.  **Filtering**: Ensures that only valid, relevant classes are returned to the `Manager`.

## Naming Convention

To ensure consistency across the framework, OpenLumara automatically converts Pythonic `CamelCase` class names into `snake_case` names. This is used for:
- Identifying modules in the configuration file.
- Mapping module names to tool names.
- Creating a unified internal registry.

**Example**:
- Class: `LifeOrganizer` $\rightarrow$ Module Name: `life_organizer`
- Class: `TelegramChannel` $\rightarrow$ Channel Name: `telegram_channel`

## Key Functions

| Function | Description |
| :--- | :--- |
| `load(package, base_class, filter, reload)` | The core engine for discovering and importing classes from a package. |
| `get_name(obj)` | Converts a class name into its `snake_case` identifier. |

## Non-Agentic Modules

The `modules.nonagentic` tuple contains a list of module names that are considered "non-agentic." These modules (`characters`, `writing_style`, `time`) are special because their prompts are injected into the context window even when the AI's "tool use" capability is turned off. This ensures that essential framework awareness is always present.

---

# Core: How modules are loaded (`core.modules`)
The `core.modules` file provides the engine for OpenLumara's extensibility. It is responsible for dynamically discovering, importing, and identifying the various modules and channels that make up the system.

## Dynamic Discovery

Instead of hardcoding every possible module or channel, OpenLumara uses filesystem scanning to find them. This allows users to simply drop a new `.py` file into the `modules/`, `user_modules/`, or `channels/` directory, and the system will automatically pick it up on the next restart. Modules created by Lumara or by the user must be placed in the `user_modules/` directory.

The `load()` function performs the following steps:
1.  **Package Scanning**: Uses `pkgutil` to iterate through all sub-modules within a given package (like `modules/` or `channels/`).
2.  **Dependency Check**: Before importing, parses the module file with `ast` to extract the `dependencies` list and checks if they're installed (via `importlib.metadata.version`). Skips modules with missing deps (logs a warning unless `loading_config=True`).
3.  **Conditional Import**: Only imports modules that are present in the `filter` list (e.g., only the modules enabled in `config.yml`).
4.  **Reload Support**: If `reload=True`, forces a reload of the module code via `importlib.reload()`.
5.  **Class Inspection**: Once a module is imported, it scans the module for any classes that inherit from a specified `base_class` (like `core.module.Module` or `core.channel.Channel`).
6.  **Filtering**: Ensures that only valid, relevant classes are returned to the `Manager`. Skips the base class itself.
7.  **Error Handling**: Catches all exceptions during import. If a module fails to load, logs the error and adds the module name to `reported_broken` to skip it on future loads.

## Dependency Auto-Installer

OpenLumara can automatically install and uninstall Python dependencies for modules and channels:

| Function | Description |
| :--- | :--- |
| `install_module_deps(package, module_name, manager)` | Checks if a module's dependencies are installed. If not, runs `pip install --quiet`. Returns `True` if something was installed. |
| `uninstall_module_deps(package, module_name, manager, exclude)` | Uninstalls a module's dependencies only if they're not still required by enabled modules. Checks all enabled modules' deps for exclusion. Calls `on_uninstall()` hook on a temporary instance before uninstalling. Returns `True` if something was uninstalled. |

Dependencies are extracted from the module file using AST parsing (no import needed), looking for a `dependencies = [...]` class attribute.

## Naming Convention

To ensure consistency across the framework, OpenLumara automatically converts Pythonic `CamelCase` class names into `snake_case` names using a regex-based conversion. This is used for:
- Identifying modules in the configuration file.
- Mapping module names to tool names.
- Creating a unified internal registry.

**Example**:
- Class: `LifeOrganizer` $\rightarrow$ Module Name: `life_organizer`
- Class: `TelegramChannel` $\rightarrow$ Channel Name: `telegram_channel`

## Key Functions

| Function | Description |
| :--- | :--- |
| `load(package, base_class=None, filter=None, reload=False, loading_config=False)` | The core engine for discovering and importing classes from a package. |
| `get_name(obj)` | Converts a class name or instance into its `snake_case` identifier. |

## Logging

During early initialization (before the `Manager` is ready), log messages are buffered in `core.modules.log_buffer` as tuples of `(category, message)`. Once the manager is available, `Manager._drain_log_buffers()` processes and displays these buffered messages.

## Module Globals

| Variable | Description |
| :--- | :--- |
| `nonagentic` | Tuple of module names whose prompts are injected even when tools are off: `("characters", "writing_style", "time")`. |
| `reported_missing` | List of module names that were skipped due to missing dependencies. |
| `reported_broken` | List of module names that failed to load (to avoid repeated error logging). |
| `log_buffer` | List of `(category, message)` tuples buffered during early initialization. |

