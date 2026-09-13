"""The options flow must be constructible.

A stray pair of constants was once pasted into the middle of a
``vol.Optional(...)`` call in the meter step. Voluptuous takes
``(schema, msg, default, description)`` positionally, so the extra names
collided with the ``default=`` keyword and every attempt to open that step
raised ``TypeError`` — which is why the charger energy sensor could never be
configured from the UI. Nothing imported config_flow in the test suite, so the
whole options flow was dead while every other test stayed green.

These checks are static: config_flow pulls in far more of Home Assistant than
the stubs in conftest provide, and the defect is visible in the source anyway.
"""

import ast
from pathlib import Path

import pytest

MARKER_CALLS = {"Optional", "Required", "Exclusive", "Inclusive"}

SOURCE = Path(__file__).resolve().parents[1] / "custom_components" / "ev_optimizer" / "config_flow.py"


def _marker_calls(tree):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # vol.Optional(...) and a bare Optional(...) alike
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name in MARKER_CALLS:
            yield name, node


def test_voluptuous_markers_take_exactly_one_positional_key():
    """One positional argument — the key. Anything more shadows msg/default."""
    tree = ast.parse(SOURCE.read_text())
    offenders = [
        (name, node.lineno, len(node.args))
        for name, node in _marker_calls(tree)
        if len(node.args) != 1
    ]
    assert not offenders, (
        "voluptuous markers must be called as vol.X(key, default=...); "
        f"these pass extra positional arguments: {offenders}"
    )


def test_every_schema_builder_constructs():
    """Build each _get_*_schema with voluptuous, with the selectors stubbed out.

    This is the check that would have failed on the real bug: it calls the
    builders rather than reading them.
    """
    vol = pytest.importorskip("voluptuous")

    tree = ast.parse(SOURCE.read_text())
    builders = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name.startswith("_get_") and node.name.endswith("_schema")
    ]
    assert builders, "no schema builders found — has config_flow been restructured?"

    # Everything the builders reference that is not voluptuous: constants
    # resolve to their own name, selectors to a callable returning None.
    class _Any:
        """Stands in for any selector, enum or config class the builders use."""

        def __init__(self, *args, **kwargs):
            pass

        def __call__(self, *args, **kwargs):
            return self

        def __getattr__(self, item):  # NumberSelectorMode.BOX and friends
            return self

    for builder in builders:
        source = ast.get_source_segment(SOURCE.read_text(), builder)
        env = {"vol": vol}
        namespace = {}

        class _Names(dict):
            def __missing__(self, key):
                if key.isupper():
                    return key
                return _Any()

        env = _Names(env)
        exec(compile(ast.Module(body=[builder], type_ignores=[]), "<schema>", "exec"), env, namespace)
        fn = namespace[builder.name]
        schema = fn()
        assert isinstance(schema, vol.Schema), f"{builder.name} did not return a Schema"
