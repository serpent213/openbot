"""Load only configured plugins; no discovery, installation, or background dispatch."""

import importlib
import re
from contextlib import AsyncExitStack

from openbot.plugin_api import Plugin, PluginHost


def load_plugins(modules):
    found = {}
    for module in modules:
        plugin = importlib.import_module(module).plugin
        if not isinstance(plugin, Plugin) or plugin.api_version != 1:
            raise ValueError(f"{module}: incompatible plugin API (expected 1)")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", plugin.id):
            raise ValueError(f"invalid plugin ID: {plugin.id}")
        if plugin.id in found:
            raise ValueError(f"duplicate plugin ID: {plugin.id}")
        found[plugin.id] = plugin
    ordered = []
    while found:
        ready = [p for p in found.values() if all(d in {x.id for x in ordered} for d in p.requires)]
        if not ready:
            raise ValueError(f"missing or cyclic plugin dependencies: {list(found)}")
        for plugin in ready:
            ordered.append(plugin)
            del found[plugin.id]
    return ordered


async def start_plugins(services):
    if services.plugin_stack is not None:
        return
    stack = AsyncExitStack()
    try:
        for plugin in load_plugins(services.settings.plugin_modules):
            path = (services.settings.plugin_state_dir / plugin.id).resolve()
            path.mkdir(parents=True, exist_ok=True)
            host = PluginHost(
                plugin.id,
                services.settings.database_url,
                services.session_factory,
                path,
                services.settings.plugin_config.get(plugin.id, {}),
                services.bus.publish,
            )
            services.plugin_hosts[plugin.id] = host
            if plugin.migrations:
                await host.migrate(plugin.migrations)
            if plugin.lifespan:
                await stack.enter_async_context(plugin.lifespan(host))
    except BaseException:
        await stack.aclose()
        services.plugin_hosts.clear()
        raise
    services.plugin_stack = stack


async def stop_plugins(services):
    if services.plugin_stack is not None:
        await services.plugin_stack.aclose()
        services.plugin_stack = None
        services.plugin_hosts.clear()
