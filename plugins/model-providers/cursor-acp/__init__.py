"""Official Cursor ACP route using the CLI's existing subscription login."""
from typing import Any

from providers import register_provider
from providers.base import ProviderProfile


def _classify_error(error, **kwargs):
    if getattr(error, "code", None) == "native_tool_safety_unavailable":
        return {"reason": "format_error", "retryable": False, "should_fallback": False}
    return None


class CursorACPProfile(ProviderProfile):
    def create_client(self, **client_kwargs: Any) -> Any:
        from agent.cursor_acp_client import CursorACPClient

        return CursorACPClient(**client_kwargs)

    def fetch_models(self, *, api_key=None, base_url=None, timeout=15.0):
        from hermes_cli.auth import resolve_external_process_provider_credentials

        try:
            credentials = resolve_external_process_provider_credentials(self.name)
            client = self.create_client(command=credentials["command"], args=credentials["args"])
            try:
                return client.list_models(timeout_seconds=timeout) or None
            finally:
                client.close()
        except (OSError, RuntimeError, ValueError):
            return None


register_provider(CursorACPProfile(
    name="cursor-acp", api_mode="chat_completions", env_vars=(), base_url="acp://cursor",
    auth_type="external_process", process_command="cursor-agent", process_args=("acp",),
    classify_api_error=_classify_error,
))
