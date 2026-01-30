import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


# Minimal Home Assistant stubs required by coordinator imports
def _make_ha_stubs():
    # homeassistant.const
    const = ModuleType("homeassistant.const")
    const.STATE_UNAVAILABLE = "unavailable"
    const.STATE_UNKNOWN = "unknown"
    const.SERVICE_TURN_ON = "turn_on"
    const.SERVICE_TURN_OFF = "turn_off"

    # homeassistant.helpers.update_coordinator
    uh = ModuleType("homeassistant.helpers.update_coordinator")

    class DataUpdateCoordinator:
        def __init__(self, hass, logger, name=None, update_interval=None):
            self.hass = hass
            self.logger = logger
            self.name = name
            self.update_interval = update_interval
            self.data = {}

    def UpdateFailed(msg):
        return Exception(msg)

    uh.DataUpdateCoordinator = DataUpdateCoordinator
    uh.UpdateFailed = Exception

    # homeassistant.helpers.storage
    storage = ModuleType("homeassistant.helpers.storage")

    class Store:
        def __init__(self, hass, version, key):
            self._data = None

        async def async_load(self):
            return None

        def async_delay_save(self, func, delay):
            return None

    storage.Store = Store
    
    # homeassistant.helpers.event
    event = ModuleType("homeassistant.helpers.event")
    event.async_track_state_change_event = lambda hass, entities, action: None
    
    # homeassistant.config_entries and core placeholders

    # homeassistant.config_entries and core placeholders
    ce = ModuleType("homeassistant.config_entries")
    class ConfigEntry: pass
    ce.ConfigEntry = ConfigEntry

    core = ModuleType("homeassistant.core")
    class HomeAssistant: pass
    def callback(func): return func
    core.HomeAssistant = HomeAssistant
    core.callback = callback

    # Insert into sys.modules
    sys.modules["homeassistant.const"] = const
    sys.modules["homeassistant.helpers.update_coordinator"] = uh
    sys.modules["homeassistant.helpers.storage"] = storage
    sys.modules["homeassistant.helpers.event"] = event
    sys.modules["homeassistant.config_entries"] = ce
    sys.modules["homeassistant.core"] = core


def _load_pkg_module(full_name, rel_path):
    path = Path(__file__).resolve().parents[1] / rel_path
    spec = importlib.util.spec_from_file_location(full_name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def ha_stubs():
    _make_ha_stubs()
    yield


@pytest.fixture
def pkg_loader():
    def _loader(name):
        base = Path(__file__).resolve().parents[1] / "custom_components" / "ev_optimizer"
        # Ensure package entries exist so relative imports inside modules work
        pkg_root = Path(__file__).resolve().parents[1] / "custom_components"
        if "custom_components" not in sys.modules:
            pkg_mod = ModuleType("custom_components")
            pkg_mod.__path__ = [str(pkg_root)]
            sys.modules["custom_components"] = pkg_mod

        pkg_ev = "custom_components.ev_optimizer"
        if pkg_ev not in sys.modules:
            ev_mod = ModuleType(pkg_ev)
            ev_mod.__path__ = [str(base)]
            sys.modules[pkg_ev] = ev_mod
            # Fix: Attach to parent
            setattr(sys.modules["custom_components"], "ev_optimizer", ev_mod)

        # Load the requested module
        mod = _load_pkg_module(f"custom_components.ev_optimizer.{name}", base / f"{name}.py")
        
        # Fix: Attach this module to the package
        setattr(sys.modules[pkg_ev], name, mod)
        
        return mod

    return _loader


class HassStates:
    def __init__(self, states_dict):
        self._states = states_dict

    def get(self, entity_id):
        return self._states.get(entity_id)


class HassServices:
    def __init__(self):
        self.calls = []

    async def async_call(self, domain, service, data, blocking=False, return_response=False):
        self.calls.append((domain, service, data))
        return None


class HassMock:
    def __init__(self, states=None):
        self.states = HassStates(states or {})
        self.services = HassServices()


@pytest.fixture
def hass_mock():
    return HassMock()
