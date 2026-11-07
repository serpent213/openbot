"""Coding backends registered by explicitly enabled plugins."""

from openbot.runtime.cli_agents.base import CliAdapter, CliAgentError


def backend_registry(plugins):
    import re

    from openbot.runtime.providers import STATUS_PROVIDER_ORDER
    found = {}
    for plugin in plugins:
        for backend in plugin.backends:
            if backend.provider in STATUS_PROVIDER_ORDER or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", backend.provider):
                raise ValueError(f"invalid or reserved coding backend: {backend.provider}")
            if backend.provider in found:
                raise ValueError(f"duplicate coding backend: {backend.provider}")
            found[backend.provider] = backend
    return found


def adapters(settings=None):
    if settings is not None and settings._coding_backends is not None:
        return settings._coding_backends
    from openbot.plugins import load_plugins
    return backend_registry(load_plugins(tuple(settings.plugin_modules) if settings else ()))


def adapter_for(provider, settings=None):
    return adapters(settings).get(provider or "")


__all__ = ["CliAdapter", "CliAgentError", "adapter_for", "adapters"]
