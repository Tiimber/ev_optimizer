# Copilot / AI Agent Instructions for EV Optimizer

Purpose: help an AI coding agent become productive quickly in this Home Assistant custom integration.

- **Big picture:** `custom_components/ev_optimizer` implements a Home Assistant integration that calculates charging schedules and load-balancing and controls chargers/vehicles. The core runtime object is the `EVSmartChargerCoordinator` (data/logic hub) in `coordinator.py`; planners and image generation are separated into `planner.py` and `image_generator.py`.

- **Key files to read first:**
  - [manifest.json](custom_components/ev_optimizer/manifest.json) — domain, dependencies, and requirements (Pillow is optional, requirements empty).
  - [__init__.py](custom_components/ev_optimizer/__init__.py) — sets up the Coordinator and forwards platform setups.
  - [coordinator.py](custom_components/ev_optimizer/coordinator.py) — central DataUpdateCoordinator, persistence, state machine, calls `planner` and `image_generator`.
  - [planner.py](custom_components/ev_optimizer/planner.py) — algorithmic logic: charging plan, price analysis, load balancing (read before modifying charging logic).
  - [sensor.py](custom_components/ev_optimizer/sensor.py) — shows how entities are created from coordinator data (model for other platforms).
  - [config_flow.py](custom_components/ev_optimizer/config_flow.py) — options/selector patterns and how configuration keys map to `entry.data` / `entry.options`.
  - [image_generator.py](custom_components/ev_optimizer/image_generator.py) — optional PIL usage; guard for missing Pillow and font-fallback logic.
  - [services.yaml](custom_components/ev_optimizer/services.yaml) — custom services exposed (`generate_report_image`, `generate_plan_image`).
  - [README.md](README.md) — user-facing installation and usage details; shows expected Home Assistant entities and examples.

- **Architecture & dataflow notes (specific):**
  - The `EVSmartChargerCoordinator` subclasses Home Assistant's `DataUpdateCoordinator`. It owns the refresh cycle (30s), stores runtime state in `hass.data[DOMAIN][entry.entry_id]`, persists UI settings via `Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")`, and logs actions into `action_log`.
  - Charging decisions are produced by `planner.generate_charging_plan(...)` and helpers like `calculate_load_balancing()`. The coordinator merges planner output into `self.data` and then updates entities.
  - Image generation is CPU-bound and uses `hass.async_add_executor_job` to call blocking code in `image_generator.py`. Pillow is optional — code gracefully degrades when not installed.
  - Config values are accessed with a `get_conf()` pattern that prefers `entry.options` over `entry.data` (see `coordinator.__init__`). Follow this pattern when reading config.
  - Services and platform forwarding: `__init__.py` forwards platforms listed in `PLATFORMS` and pre-imports `logbook` using `importlib` to avoid blocking I/O errors.

- **Conventions & patterns to follow when editing:**
  - Non-blocking: use `async_add_executor_job` for blocking I/O (image save, heavy CPU work). Avoid synchronous I/O in async methods.
  - Persistence: use Home Assistant `Store` with `async_load` and `async_delay_save` exactly as implemented; keep saved-time formats compatible with the loading code (times saved as `HH:MM` strings).
  - Config merge: use `entry.options.get(key, entry.data.get(key, default))` to read options.
  - Coordinator API: prefer adding methods on `EVSmartChargerCoordinator` (e.g., `set_user_input`, `clear_manual_override`, `async_trigger_report_generation`) rather than manipulating its internals directly.
  - Logging/events: use `_LOGGER` and `hass.bus.async_fire(f"{DOMAIN}_log_event", {...})` when emitting user-visible events.

- **Integration & dependency specifics:**
  - No required pip packages listed in `manifest.json`; Pillow (PIL) is optional and guarded. If adding third-party dependencies, add them to `manifest.json` `requirements`.
  - `manifest.json` lists platform dependencies (`sensor`, `number`, `switch`, `button`, `time`, `camera`); ensure any new platform additions are mirrored in `PLATFORMS` in `__init__.py`.

- **Developer workflows and manual test tips (repo-specific):**
  - Install locally by copying `custom_components/ev_optimizer` to `config/custom_components/` in a Home Assistant instance or use HACS as described in `README.md`.
  - Generate images locally: install `Pillow` in the Home Assistant Python environment; otherwise image services will be no-ops.
  - Trigger services from Developer Tools → Services: `ev_optimizer.generate_report_image` and `ev_optimizer.generate_plan_image` to exercise image paths.
  - Inspect runtime coordinator state via developer tools or by logging: `hass.data['ev_optimizer'][<entry_id>]` contains `action_log`, `user_settings`, `last_session_data`.

- **What to look for when changing charging logic:**
  - Update planner functions in `planner.py` and ensure coordinator still expects the same dict keys (e.g., `should_charge_now`, `charging_schedule`, `planned_target_soc`, `session_end_time`).
  - The coordinator includes buffer/edge-case handling (startup grace, short charging bursts). Avoid removing those without running longer tests.

- **Exact references/examples from codebase:**
  - Coordinator stored on setup: `hass.data.setdefault(DOMAIN, {}); hass.data[DOMAIN][entry.entry_id] = coordinator` ([__init__.py](custom_components/ev_optimizer/__init__.py)).
  - Save/load settings: `self.store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")` and `store.async_delay_save(...)` ([coordinator.py](custom_components/ev_optimizer/coordinator.py)).
  - Service names: `generate_report_image` / `generate_plan_image` (see [services.yaml](custom_components/ev_optimizer/services.yaml)).

If any part of this is unclear or you want the AI to expand an area (tests, more examples, or stricter style rules), tell me which sections to iterate on.
