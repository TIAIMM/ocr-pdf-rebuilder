"""Compatibility bridge for incrementally extracted pipeline components.

Focused components own their implementations and constants.  Before an
exported function runs, the bridge refreshes only its external dependencies
from the stable CLI namespace.  Export implementations themselves are never
overwritten, which avoids recursion while preserving existing monkeypatch and
diagnostic behavior during the modular migration.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from types import ModuleType
from typing import Any


class ComponentRuntime:
    def __init__(self, module_globals: dict[str, Any], export_names: tuple[str, ...]) -> None:
        self.module_globals = module_globals
        self.export_names = export_names
        self.implementations = {
            name: module_globals[name]
            for name in export_names
            if callable(module_globals.get(name))
        }
        self.state_names = tuple(
            name for name in export_names if not callable(module_globals.get(name))
        )

    def exports(self) -> dict[str, Any]:
        return {name: self.module_globals[name] for name in self.export_names}

    def bind(self, namespace: Mapping[str, Any]) -> None:
        for name, value in namespace.items():
            if name.startswith("__"):
                continue
            if name in self.export_names:
                implementation = self.implementations.get(name)
                if implementation is not None:
                    self.module_globals[name] = (
                        implementation
                        if getattr(value, "__component_wrapper__", False)
                        else value
                    )
                else:
                    self.module_globals[name] = value
                continue
            self.module_globals[name] = value

    def invoke(
        self,
        name: str,
        namespace: Mapping[str, Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        self.bind(namespace)
        try:
            return self.implementations[name](*args, **kwargs)
        finally:
            # Keep mutable/scalar component state visible through the legacy
            # entry namespace, including deliberate diagnostic resets.
            if isinstance(namespace, MutableMapping):
                for state_name in self.state_names:
                    namespace[state_name] = self.module_globals[state_name]


def install_component(module: ModuleType, namespace: dict[str, Any]) -> None:
    """Install constants and lazy compatibility wrappers into a CLI namespace."""
    exports = module.component_exports()
    for name, value in exports.items():
        if not callable(value) or isinstance(value, type):
            namespace[name] = value
            continue

        def compatibility_wrapper(*args: Any, __name: str = name, **kwargs: Any) -> Any:
            return module.invoke_component(__name, namespace, *args, **kwargs)

        compatibility_wrapper.__name__ = name
        compatibility_wrapper.__qualname__ = name
        compatibility_wrapper.__doc__ = getattr(value, "__doc__", None)
        compatibility_wrapper.__module__ = namespace.get("__name__", "")
        compatibility_wrapper.__component_wrapper__ = True
        namespace[name] = compatibility_wrapper
