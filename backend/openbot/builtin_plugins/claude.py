from openbot.plugin_api import Plugin
from openbot.runtime.cli_agents.claude_code import ClaudeCode

plugin = Plugin(id="claude", backends=(ClaudeCode(),))
