# Core: The Module Loader (`core.modules`)

The `core.modules` module provides the engine for OpenLumara's extensibility. It is responsible for dynamically discovering, importing, and identifying the various modules and channels that make up the system.

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

The `modules.nonagentic` tuple contains a list of module names that are considered "non-agentic." These modules (such as `characters` or `time`) are special because their prompts are injected into the context window even when the AI's "tool use" capability is turned off. This ensures that essential framework awareness is always present.
