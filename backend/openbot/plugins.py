"""Load only configured plugins; no discovery, installation, or background dispatch."""

import importlib
import re
from contextlib import AsyncExitStack

from openbot.plugin_api import API_VERSION, Plugin, PluginHost
from openbot.runtime.plugin_settings import SettingsStore


def load_plugins(modules):
    modules = ("openbot.builtin_plugins.claude", "openbot.builtin_plugins.telegram", *modules)
    found = {}
    for module in modules:
        plugin = importlib.import_module(module).plugin
        if (not isinstance(plugin, Plugin) or not isinstance(plugin.api_version, tuple)
                or len(plugin.api_version) != 2 or plugin.api_version[0] != API_VERSION[0]
                or not 0 <= plugin.api_version[1] <= API_VERSION[1]):
            raise ValueError(f"{module}: incompatible plugin API (expected 1)")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", plugin.id):
            raise ValueError(f"invalid plugin ID: {plugin.id}")
        if plugin.id in found:
            raise ValueError(f"duplicate plugin ID: {plugin.id}")
        if plugin.router and not (plugin.router.prefix == "/" + plugin.id
                                  or plugin.router.prefix.startswith("/" + plugin.id + "/")):
            raise ValueError(f"plugin {plugin.id}: routes must use /{plugin.id} prefix")
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


def configured_plugins(services):
    if services.plugins is None:
        services.plugins = load_plugins(services.settings.plugin_modules)
    from openbot.runtime.cli_agents import backend_registry
    services.settings._coding_backends = backend_registry(services.plugins)
    return services.plugins


async def start_plugins(services):
    if services.plugin_stack is not None:
        return
    stack = AsyncExitStack()
    try:
        from openbot.runtime.commands import commands
        commands(services)  # Validate before starting any plugin.
        for plugin in configured_plugins(services):
            path = (services.settings.plugin_state_dir / plugin.id).resolve()
            path.mkdir(parents=True, exist_ok=True)
            host = PluginHost(
                id=plugin.id,
                engine=services.session_factory.kw["bind"],
                session_factory=services.session_factory,
                state_dir=path,
                config=services.settings.plugin_config.get(plugin.id, {}),
                _publish=services.bus.publish,
                settings=SettingsStore(services, plugin),
            )
            services.plugin_hosts[plugin.id] = host
            from openbot.runtime.channels import ChannelHost, ChannelService
            if services.channels is None:
                services.channels = ChannelService(services)
            host.channels = ChannelHost(services.channels, plugin.id)
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


async def start_plugin_background(services):
    stack = AsyncExitStack()
    try:
        for plugin in configured_plugins(services):
            if plugin.background:
                await stack.enter_async_context(plugin.background(services.plugin_hosts[plugin.id]))
        if services.channels:
            await services.channels.start()
    except BaseException:
        await stack.aclose()
        raise
    services.plugin_background = stack


async def stop_plugin_background(services):
    if services.channels:
        await services.channels.stop()
    if services.plugin_background:
        await services.plugin_background.aclose()
        services.plugin_background = None
