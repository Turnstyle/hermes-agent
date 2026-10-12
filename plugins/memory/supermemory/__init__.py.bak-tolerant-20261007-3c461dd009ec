"""Supermemory memory plugin (MemoryProvider): profile recall, semantic search, memory tools, per-turn capture."""

from __future__ import annotations

import hashlib
import contextlib
import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

from agent.memory_provider import MemoryProvider, spawn_context_thread
from agent.secret_scope import get_secret, is_multiplex_active, serves_routed_profile
from tools.registry import tool_error

from . import tunnel_key_helper as _tunnel

logger = logging.getLogger(__name__)

_DEFAULT_CONTAINER_TAG = "hermes"
_VALID_SEARCH_MODES = ("hybrid", "memories", "documents")
_DEFAULT_BASE_URL = "https://api.supermemory.ai"
# With a tunnel the SDK's transport connects only to the tunnel's Unix socket, so this URL supplies just the Host
# header and the path. ".invalid" never resolves (RFC 6761): nothing could reach it over TCP even if tried.
_TUNNEL_BASE_URL = "http://supermemory-tunnel.invalid"
_API_KEY_URL = "http://app.supermemory.ai/integrations?connect=hermes"
# Strips injected <supermemory-context> / <supermemory-containers> blocks before capture.
_INJECTED_BLOCK_RE = re.compile(r"<supermemory-(context|containers)>[\s\S]*?</supermemory-\1>\s*", re.DOTALL)
_DATA_URI_RE = re.compile(r"data:[^;,\s]+;base64,[A-Za-z0-9+/=]+")  # pasted inline images are useless as memory text
_CAPTURE_BUCKET_HOURS = 4  # one capture document per session per 4h window (matches the other Supermemory agent integrations)
_FAILED = object()  # _quietly default for capture writes: an explicit failure marker (a client returning None still counts as success)
_MAX_PENDING_TURNS = 50  # a down service must not accumulate an unbounded retry buffer
_MAX_PENDING_BYTES = 256 * 1024
_DEFAULT_ENTITY_CONTEXT = (
    "User-assistant conversation. Format: [role: user]...[user:end] and [role: assistant]...[assistant:end].\n\n"
    "Only extract things useful in future conversations. Most messages are not worth remembering.\n\n"
    "Remember lasting personal facts, preferences, routines, tools, ongoing projects, working context, "
    "and explicit requests to remember something.\n\n"
    "Do not remember temporary intents, one-time tasks, assistant actions, implementation details, or in-progress status.\n\n"
    "When in doubt, store less."
)
# snake_case tool name -> kebab-case alias exposed alongside it.
_KEBAB_ALIASES = {"supermemory_store": "supermemory-save", "supermemory_search": "supermemory-search",
                  "supermemory_forget": "supermemory-forget", "supermemory_profile": "supermemory-profile"}
_ALIAS_TO_TOOL = {kebab: snake for snake, kebab in _KEBAB_ALIASES.items()}
_BOOL_WORDS = {**dict.fromkeys(("true", "1", "yes", "y", "on"), True), **dict.fromkeys(("false", "0", "no", "n", "off"), False)}
# Set by a key helper (see tunnel_key_helper.py) when require_availability_proof is on:
# "v1:<hermes pid>:<key fingerprint>" on success, "down:<hermes pid>:<reason>" otherwise.
_PROOF_ENV = "SUPERMEMORY_AVAILABILITY_PROOF"
_CONTAINER_OPS = ("read", "write")
# Why _drop_inherited_key removed SUPERMEMORY_API_KEY from this (single-profile) process. Later gate checks, in any
# provider instance, report it instead of the missing key the drop left behind.
_dropped_key_reason = ""


def _quietly(fn: Callable[[], Any], fail_msg: str = "", *args: Any, level: int = logging.DEBUG, default: Any = None) -> Any:
    """Run ``fn()``; on any exception log ``fail_msg`` (if given) with traceback and return ``default``."""
    try:
        return fn()
    except Exception:
        if fail_msg:
            logger.log(level, fail_msg, *args, exc_info=True)
        return default


def _sanitize_tag(raw: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-zA-Z0-9_]", "_", raw or "")).strip("_") or _DEFAULT_CONTAINER_TAG


def _resolve_base_url(config_value: Any = "") -> str:
    """config > SUPERMEMORY_BASE_URL (profile-scoped) > default (self-hosted support)."""
    raw = str(config_value or "").strip() or (get_secret("SUPERMEMORY_BASE_URL", "") or "").strip()
    return (raw or _DEFAULT_BASE_URL).rstrip("/") or _DEFAULT_BASE_URL


def _clamp_entity_context(text: str) -> str:
    return text.strip()[:1500] if text else _DEFAULT_ENTITY_CONTEXT


def _as_bool(value: Any, default: bool) -> bool:
    """bool passthrough; common true/false words parsed; anything else (incl. ints) -> default."""
    return value if isinstance(value, bool) else _BOOL_WORDS.get(value.strip().lower(), default) if isinstance(value, str) else default


def _clamp_number(value: Any, default, lo, hi, cast):
    """Cast ``value`` and clamp it to [lo, hi]; fall back to ``default`` on any conversion error."""
    return _quietly(lambda: max(lo, min(hi, cast(value))), default=default)


def _normalize_permissions(value: Any) -> Optional[Dict[str, Dict[str, bool]]]:
    """``containers`` map -> {raw tag: {"read": bool, "write": bool}}; None = no map (legacy: every op allowed).
    A malformed map or entry grants nothing: once permissions are configured, anything unclear is denied."""
    if value is None:
        return None
    if not isinstance(value, dict):
        return {}
    return {str(tag).strip(): {op: _as_bool(perms.get(op), False) if isinstance(perms, dict) else False for op in _CONTAINER_OPS}
            for tag, perms in value.items() if str(tag).strip()}


def _normalize_tunnel(value: Any) -> Optional[Dict[str, str]]:
    """``tunnel`` block -> {"ssh_host", "forward"}; None = no block; {} = malformed, a TCP forward included (the gate
    then fails closed)."""
    if value is None:
        return None
    if not isinstance(value, dict) or not str(value.get("ssh_host") or "").strip():
        return {}
    forward = str(value.get("forward") or "").strip()
    return {"ssh_host": str(value["ssh_host"]).strip(), "forward": forward} if _quietly(lambda: _tunnel.split_forward(forward)) else {}


def _normalize_quarantined_ids(value: Any) -> Dict[str, List[str]]:
    """Container -> exact memory ids excluded from every recall path.

    Malformed entries grant no recall: non-list values become empty lists, and
    ids are stripped, deduplicated, and bounded to avoid untrusted config growth.
    Container templates are resolved later alongside the permission map.
    """
    if not isinstance(value, dict):
        return {}
    normalized: Dict[str, List[str]] = {}
    for tag, ids in value.items():
        tag = str(tag).strip()
        if not tag:
            continue
        # A configured but malformed quarantine must not silently become "no quarantine".
        normalized[tag] = ["*"] if not isinstance(ids, list) else list(dict.fromkeys(
            str(memory_id).strip()[:256] for memory_id in ids
            if str(memory_id).strip()
        ))[:256]
    return normalized


# config key -> (default, normalizer applied to the raw/merged value). Order = supermemory.json layout.
# container_tag is kept raw here: {identity} templates are resolved in initialize(), and
# _sanitize_tag runs AFTER that resolution. custom_containers, by contrast, are sanitized on load.
_CONFIG_SPEC: Dict[str, tuple] = {
    "container_tag": (_DEFAULT_CONTAINER_TAG, lambda v: str(v).strip() or _DEFAULT_CONTAINER_TAG),
    "auto_recall": (True, lambda v: _as_bool(v, True)),
    "auto_capture": (True, lambda v: _as_bool(v, True)),
    "max_recall_results": (10, lambda v: _clamp_number(v, 10, 1, 20, int)),
    "profile_frequency": (50, lambda v: _clamp_number(v, 50, 1, 500, int)),
    "capture_mode": ("all", lambda v: "everything" if v == "everything" else "all"),
    "search_mode": ("hybrid", lambda v: v if (v := str(v).strip().lower()) in _VALID_SEARCH_MODES else "hybrid"),
    "entity_context": (_DEFAULT_ENTITY_CONTEXT, lambda v: _clamp_entity_context(str(v))),
    "api_timeout": (5.0, lambda v: _clamp_number(v, 5.0, 0.5, 15.0, float)),
    "base_url": ("", lambda v: str(v or "").strip()),
    "enable_custom_container_tags": (False, lambda v: _as_bool(v, False)),
    "custom_containers": ([], lambda v: [_sanitize_tag(str(t)) for t in v if t] if isinstance(v, list) else []),
    "custom_container_instructions": ("", lambda v: str(v).strip()),
    # Container-scoped exact memory ids that must never be returned. When a container has quarantined ids, profile
    # recall is disabled for that container because the aggregate profile API does not identify source documents.
    "quarantined_memory_ids": ({}, _normalize_quarantined_ids),
    # Per-container operation permissions, e.g. {"hermes_fleet_pilot": {"read": true, "write": false}}; keys accept
    # {identity}. Absent = every op allowed; present = default-deny (an unlisted container gets neither op).
    "containers": (None, _normalize_permissions),
    # Require a fresh per-process proof from the key helper (_PROOF_ENV) instead of mere key presence.
    "require_availability_proof": (False, lambda v: _as_bool(v, False)),
    # The SSH forward to a local Unix socket that every request goes through, e.g. {"ssh_host": "rosie", "forward":
    # "/Users/me/.hermes/supermemory-tunnel/rosie.sock:127.0.0.1:6768"}; base_url must then be unset. The socket and
    # its listener are verified before every client use, the socket file before every request (_TunnelTransport).
    "tunnel": (None, _normalize_tunnel),
}


def _read_json_dict(path: Path) -> dict:
    raw = _quietly(lambda: json.loads(path.read_text(encoding="utf-8-sig")), "Failed to parse %s", path) if path.exists() else None
    return raw if isinstance(raw, dict) else {}


def _load_supermemory_config(hermes_home: Optional[str] = None) -> dict:
    """Defaults overlaid with $hermes_home/supermemory.json (None = defaults only), every key normalized."""
    config = {k: (list(d) if isinstance(d, list) else d) for k, (d, _) in _CONFIG_SPEC.items()}
    if hermes_home is not None:
        config.update({k: v for k, v in _read_json_dict(Path(hermes_home) / "supermemory.json").items() if v is not None})
    for key, (_, normalize) in _CONFIG_SPEC.items():
        config[key] = normalize(config[key])
    return config


def _save_supermemory_config(values: dict, hermes_home: str) -> None:
    from utils import atomic_json_write
    config_path = Path(hermes_home) / "supermemory.json"
    atomic_json_write(config_path, {**_read_json_dict(config_path), **values}, mode=0o600, sort_keys=True)


def _detect_category(text: str) -> str:
    lowered = text.lower()  # first matching pattern wins
    return next((cat for cat, pat in (("preference", r"prefer|like|love|hate|want"), ("decision", r"decided|will use|going with"),
                                      ("fact", r"\bis\b|\bare\b|\bhas\b|\bhave\b")) if re.search(pat, lowered)), "other")


def _format_relative_time(iso_timestamp: str) -> str:
    """'just now' / '5m ago' / '3h ago' / '2d ago' / '%d %b[ %Y]'; '' when unparseable."""
    def _fmt():
        dt, now = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00")), datetime.now(timezone.utc)
        seconds = (now - dt).total_seconds()
        for limit, unit, label in ((1800, 0, "just now"), (3600, 60, "m ago"), (86400, 3600, "h ago"), (604800, 86400, "d ago")):
            if seconds < limit:
                return f"{int(seconds / unit)}{label}" if unit else label
        return dt.strftime("%d %b" if dt.year == now.year else "%d %b %Y")
    return _quietly(_fmt, default="")


def _similarity_pct(value: Any) -> Optional[int]:
    """0..1 similarity -> whole percent; None when absent or unparseable."""
    return _quietly(lambda: None if value is None else round(float(value) * 100))


def _profile_sections(static_facts: list, dynamic_facts: list) -> list[str]:
    return [f"## {title}\n" + "\n".join(f"- {item}" for item in items)
            for title, items in (("User Profile (Persistent)", static_facts), ("Recent Context", dynamic_facts)) if items]


def _format_prefetch_context(static_facts: list, dynamic_facts: list, search_results: list, max_results: int) -> str:
    """Dedupe across the three lists (earlier lists win: profile facts beat search hits), cap each, render."""
    seen: set = set()

    def _unique(items, key=lambda x: x):  # set.add() returns None, so `not seen.add(k)` records k and keeps the item
        return [i for i in items or [] if (k := key(i)) and k not in seen and not seen.add(k)][:max_results]
    sections = _profile_sections(_unique(static_facts), _unique(dynamic_facts))
    lines = []
    for item in _unique(search_results, key=lambda i: i.get("memory", "")):
        rel = _format_relative_time(item.get("updated_at") or item.get("updatedAt") or "")
        pct = _similarity_pct(item.get("similarity"))
        lines.append(f"- {' '.join(([f'[{rel}]'] if rel else []) + ([f'[{pct}%]'] if pct is not None else []))} {item['memory']}".strip())
    sections += ["## Relevant Memories\n" + "\n".join(lines)] if lines else []
    intro = "The following is background context from long-term memory. Use it silently when relevant. Do not force memories into the conversation."
    return f"<supermemory-context>\n{intro}\n\n" + "\n\n".join(sections) + "\n</supermemory-context>" if sections else ""


def _clean_text_for_capture(text: str) -> str:
    return _DATA_URI_RE.sub("[image]", _INJECTED_BLOCK_RE.sub("", text or "")).strip()


def _memory_fields(item: Any, *keys: str) -> dict:
    """Pick SDK result attrs into a plain dict; ``updated_at`` also accepts camelCase ``updatedAt``."""
    defaults = {"id": "", "memory": "", "similarity": None, "metadata": None}
    return {k: getattr(item, "updated_at", None) or getattr(item, "updatedAt", None) if k == "updated_at" else getattr(item, k, defaults.get(k))
            for k in keys}


class _TunnelTransport(httpx.BaseTransport):
    """The SDK's only transport when supermemory.json has a ``tunnel``: every request goes to the tunnel's Unix socket
    on a new connection, and only after the socket file passes ``check_socket`` against ``pinned``: the socket the
    start-up gate verified, or else one verified here (a replaced, stale-path or loosened socket is refused before a
    byte is sent). There is no TCP route: no TCP URL, no environment proxies, nothing to fall back to."""

    def __init__(self, tunnel: Dict[str, str], pinned: Optional[Tuple[int, int]] = None):
        if not tunnel:
            raise ValueError("supermemory.json tunnel settings are malformed")
        self._socket_path = _tunnel.split_forward(tunnel["forward"])[0]
        if pinned is None:
            reason, pinned = _tunnel.verify_tunnel(tunnel["forward"], tunnel["ssh_host"])
            if reason:
                raise ConnectionError(f"Supermemory tunnel check failed ({reason})")
        self._pinned = pinned
        self._inner = httpx.HTTPTransport(uds=self._socket_path, retries=0,
                                          limits=httpx.Limits(max_keepalive_connections=0))

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        reason, _ = _tunnel.check_socket(self._socket_path, _tunnel.current_uid(), self._pinned)
        if reason:
            raise httpx.ConnectError(f"Supermemory tunnel socket refused ({reason})", request=request)
        return self._inner.handle_request(request)

    def close(self) -> None:
        self._inner.close()


class _SupermemoryClient:
    def __init__(self, api_key: str, timeout: float, container_tag: str,
                 search_mode: str = "hybrid", base_url: str = "", tunnel: Optional[Dict[str, str]] = None,
                 socket_pin: Optional[Tuple[int, int]] = None):
        # Make the pinned extra importable; on failure fall through so the raw
        # import below produces the canonical ImportError message.
        with contextlib.suppress(Exception):
            from pm import ensure_import as _lazy_ensure
            _lazy_ensure("supermemory")
        from supermemory import Supermemory
        self._api_key, self._container_tag, self._timeout = api_key, container_tag, timeout
        self._search_mode = search_mode if search_mode in _VALID_SEARCH_MODES else "hybrid"
        self._base_url = _resolve_base_url(base_url) if tunnel is None else _TUNNEL_BASE_URL
        http_client = None if tunnel is None else httpx.Client(transport=_TunnelTransport(tunnel, socket_pin),
                                                               trust_env=False, timeout=timeout, follow_redirects=False)
        self._client = Supermemory(api_key=api_key, base_url=self._base_url, timeout=timeout, max_retries=0,
                                   default_headers={"x-sm-source": "hermes"},
                                   **({"http_client": http_client} if http_client else {}))

    def _merge_metadata(self, metadata: Optional[dict]) -> dict:
        # sm_source routes Hermes writes into the "Hermes" Space in the Supermemory app so the user
        # can filter / bulk-manage them per source agent (a routing key for the user, not telemetry).
        merged = {"sm_source": "hermes", **(metadata or {})}
        if (legacy_source := merged.pop("source", None)) and "type" not in merged:
            merged["type"] = str(legacy_source)
        return merged

    def add_memory(self, content: str, metadata: Optional[dict] = None, *, entity_context: str = "",
                   container_tag: Optional[str] = None, custom_id: Optional[str] = None) -> dict:
        kwargs: dict[str, Any] = {"content": content.strip(), "container_tags": [container_tag or self._container_tag],
                                  **({"metadata": self._merge_metadata(metadata)} if metadata else {}),
                                  **({"entity_context": _clamp_entity_context(entity_context)} if entity_context else {}),
                                  **({"custom_id": custom_id} if custom_id else {})}
        return {"id": getattr(self._client.documents.add(**kwargs), "id", "")}

    def search_memories(self, query: str, *, limit: int = 5, container_tag: Optional[str] = None,
                        search_mode: Optional[str] = None) -> list[dict]:
        mode = search_mode or self._search_mode
        kwargs: dict[str, Any] = {"q": query, "container_tag": container_tag or self._container_tag, "limit": limit,
                                  # Parent documents let the quarantine see through chunk ids (document mode).
                                  "include": {"documents": True},
                                  **({"search_mode": mode} if mode in _VALID_SEARCH_MODES else {})}
        response = self._client.search.memories(**kwargs)
        return [{**_memory_fields(item, "id", "memory", "similarity", "updated_at", "metadata", "documents"), "memory": getattr(item, "memory", "") or ""}
                for item in (getattr(response, "results", None) or [])]

    def get_profile(self, query: Optional[str] = None, *, container_tag: Optional[str] = None) -> dict:
        response = self._client.profile(container_tag=container_tag or self._container_tag, **({"q": query} if query else {}))
        profile_data = getattr(response, "profile", None)
        search_data = getattr(response, "search_results", None) or getattr(response, "searchResults", None)
        raw_results = getattr(search_data, "results", None) or search_data or []
        return {
            **{k: (getattr(profile_data, k, []) or []) if profile_data else [] for k in ("static", "dynamic")},
            "search_results": [item if isinstance(item, dict) else _memory_fields(item, "id", "memory", "updated_at", "similarity")
                               for item in raw_results] if isinstance(raw_results, list) else [],
        }

    def forget_memory(self, memory_id: str, *, container_tag: Optional[str] = None) -> None:
        self._client.memories.forget(container_tag=container_tag or self._container_tag, id=memory_id)

    def forget_by_query(self, query: str, *, container_tag: Optional[str] = None) -> dict:
        results = self.search_memories(query, limit=5, container_tag=container_tag)
        memory_id = results[0].get("id", "") if results else ""
        if not memory_id:
            return {"success": False, "message": "Best matching memory has no id." if results else "No matching memory found to forget."}
        self.forget_memory(memory_id, container_tag=container_tag)
        return {"success": True, "message": f'Forgot: "{(results[0].get("memory") or "")[:100]}"', "id": memory_id}


def _format_turn(user: str, assistant: str) -> str:
    """Render one turn in the [role: x]...[x:end] layout the entity context describes."""
    return "\n".join(f"[role: {role}]\n{text}\n[{role}:end]" for role, text in (("user", user), ("assistant", assistant)) if text)


def _capture_custom_id(session_id: str, now: Optional[datetime] = None) -> str:
    """<session>_<YYYY-MM-DD>_b<0..5>: same id within a 4h window, so the API appends turns to one document."""
    now = now or datetime.now(timezone.utc)
    return f"{_sanitize_tag(session_id)}_{now:%Y-%m-%d}_b{now.hour // _CAPTURE_BUCKET_HOURS}"


def _build_client(api_key: str, config: dict, container_tag: str,
                  socket_pin: Optional[Tuple[int, int]] = None) -> _SupermemoryClient:
    """``socket_pin``: the tunnel socket a gate just verified, so the client enforces that very socket."""
    tunnel = config["tunnel"]
    return _SupermemoryClient(api_key=api_key, timeout=config["api_timeout"], container_tag=container_tag,
                              search_mode=config["search_mode"], base_url=_resolve_base_url(config["base_url"]),
                              **({"tunnel": tunnel, "socket_pin": socket_pin} if tunnel is not None else {}))


def _resolve_container_tag(config_tag: str, identity: str) -> str:
    """SUPERMEMORY_CONTAINER_TAG (profile-scoped) > config > default; {identity} expands to the agent
    identity, then sanitize. The container is the data partition, so it must never be borrowed from
    the default profile's environ under multiplexing."""
    raw_tag = (get_secret("SUPERMEMORY_CONTAINER_TAG", "") or "").strip() or config_tag
    return _sanitize_tag(raw_tag.replace("{identity}", identity))


def _key_fingerprint(api_key: str) -> str:
    """First 16 hex chars of sha256(key): binds a proof to one key without carrying the key. Must match
    tunnel_key_helper.key_fingerprint."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def _availability_error(api_key: str, config: dict) -> str:
    """"" when the provider may use ``api_key``; else a short reason that never contains the key.

    With ``require_availability_proof`` the key helper must have proven, in THIS process, that the endpoint
    answered for THIS key. A proof inherited from a parent process (other pid) or minted for another key
    (other fingerprint) is stale, so an inherited SUPERMEMORY_API_KEY alone can no longer make the provider live."""
    if config.get("require_availability_proof"):
        state, _, rest = (get_secret(_PROOF_ENV, "") or "").strip().partition(":")
        pid, _, detail = rest.partition(":")
        if not state:
            return f"{_PROOF_ENV} is not set (the key helper has not proven the endpoint reachable in this process)"
        if state == "down":
            return f"key helper reported the endpoint down ({detail or 'no reason given'})"
        if state != "v1" or pid != str(os.getpid()):
            return f"{_PROOF_ENV} is stale or malformed (not issued to this process)"
        if not api_key or detail != _key_fingerprint(api_key):
            return f"SUPERMEMORY_API_KEY is not the key the helper proved ({_PROOF_ENV} fingerprint mismatch)"
    return "" if api_key else "SUPERMEMORY_API_KEY not set"


def _guarded(config: dict) -> bool:
    """An availability guard is configured, so availability is more than key presence and is re-checked on use."""
    return bool(config["require_availability_proof"] or config["tunnel"] is not None)


def _tunnel_check(config: dict, pinned: Optional[Tuple[int, int]] = None) -> Tuple[str, Optional[Tuple[int, int]]]:
    """("", socket id) when there is no ``tunnel`` block or its socket is still this user's ssh forward (and the socket
    ``pinned`` by an earlier check, when given); else (reason, None). Live and credential-free (lstat, an empty
    connection, the listener's peer credentials and exact argv), so it runs before every client use."""
    tunnel = config["tunnel"]
    if tunnel is None:
        return "", None
    if not tunnel:
        return ("supermemory.json tunnel settings are malformed (need ssh_host and forward "
                "<absolute socket path>:<remote host>:<remote port>)"), None
    if str(config["base_url"] or "").strip() or (get_secret("SUPERMEMORY_BASE_URL", "") or "").strip():
        return "base_url (supermemory.json or SUPERMEMORY_BASE_URL) must be unset with a tunnel: requests go only to its socket", None
    reason, socket_id = _tunnel.verify_tunnel(tunnel["forward"], tunnel["ssh_host"])
    if not reason and pinned is not None and socket_id != pinned:
        reason = "socket_replaced"
    return (f"tunnel check failed ({reason})", None) if reason else ("", socket_id)


def _drop_inherited_key(reason: str) -> None:
    """Remove an unproven SUPERMEMORY_API_KEY from this process's environ, so children that copy os.environ don't
    inherit it. Never under multiplexing or for a routed profile: os.environ is shared there and belongs to no one
    profile. A copy held in a bound secret scope (TUI/Desktop bodies bind one even single-profile) is not touched and
    stays until the process exits; the provider refuses it either way, and a restart removes it."""
    global _dropped_key_reason
    if is_multiplex_active() or serves_routed_profile():
        return
    if os.environ.pop("SUPERMEMORY_API_KEY", None) is not None:
        _dropped_key_reason = reason
        logger.warning("Supermemory: removed SUPERMEMORY_API_KEY from the process environment (%s)", reason)


def _gate(config: Optional[dict] = None) -> Tuple[str, Optional[Tuple[int, int]]]:
    """(availability reason, "" = usable; the tunnel socket it verified) for the active profile. A guarded failure
    drops an unproven inherited key (_drop_inherited_key)."""
    if config is None:
        from hermes_constants import get_hermes_home
        config = _load_supermemory_config(str(get_hermes_home()))
    key = get_secret("SUPERMEMORY_API_KEY", "") or ""
    if not key and _dropped_key_reason and _guarded(config):
        return _dropped_key_reason, None
    error, socket_id = _availability_error(key, config), None
    if not error:
        error, socket_id = _tunnel_check(config)
    if error and _guarded(config):
        _drop_inherited_key(error)
    return error, socket_id


def _probe_supermemory_connection(api_key: str, hermes_home: str, *, identity: str = "default", gate_error: str = "",
                                  socket_pin: Optional[Tuple[int, int]] = None) -> dict:
    config = _load_supermemory_config(hermes_home)
    status = {"ok": False, "error": "", "profile_facts": 0, "container_tag": _resolve_container_tag(config["container_tag"], identity),
              "auto_recall": bool(config["auto_recall"]), "auto_capture": bool(config["auto_capture"])}
    if gate_error:
        return {**status, "error": gate_error}
    if not (api_key or "").strip():
        return {**status, "error": "SUPERMEMORY_API_KEY not set"}
    try:
        __import__("supermemory")
    except ImportError:
        return {**status, "error": "supermemory package not installed"}
    try:
        profile = _build_client(api_key.strip(), config, status["container_tag"], socket_pin).get_profile()
    except Exception as exc:
        return {**status, "error": str(exc).strip()[:160] or "connection failed"}
    facts = sum(1 for f in (profile.get("static") or []) + (profile.get("dynamic") or []) if f and str(f).strip())
    return {**status, "ok": True, "profile_facts": facts}


def _format_connection_summary(status: dict) -> str:
    container = status.get("container_tag") or _DEFAULT_CONTAINER_TAG
    flags = f"auto_recall {'on' if status.get('auto_recall') else 'off'} · auto_capture {'on' if status.get('auto_capture') else 'off'}"
    if status.get("ok"):
        facts = int(status.get("profile_facts") or 0)
        return f"✓ Connected · container: {container} · {facts} profile {'fact' if facts == 1 else 'facts'} · {flags}"
    return f"✗ {status.get('error') or 'connection failed'} · container: {container} · {flags}"


# (name, description, ((prop, type, description), ...), required) -> tool schema; kebab aliases are added in get_tool_schemas().
_BASE_SCHEMAS = [
    {"name": name, "description": description,
     "parameters": {"type": "object", "properties": {p: {"type": t, "description": d} for p, t, d in props}, **({"required": req} if req else {})}}
    for name, description, props, req in (
        ("supermemory_store", "Store an explicit memory for future recall.",
         (("content", "string", "The memory content to store."), ("metadata", "object", "Optional metadata attached to the memory.")), ["content"]),
        ("supermemory_search", "Search long-term memory by semantic similarity.",
         (("query", "string", "What to search for."), ("limit", "integer", "Maximum results to return, 1 to 20.")), ["query"]),
        ("supermemory_forget", "Forget a memory by exact id or by best-match query.",
         (("id", "string", "Exact memory id to delete."), ("query", "string", "Query used to find the memory to forget.")), None),
        ("supermemory_profile", "Retrieve persistent profile facts and recent memory context.",
         (("query", "string", "Optional query to focus the profile response."),), None),
    )
]


class _TagError(Exception):
    """Tool call named a container_tag outside the whitelist, or one without the operation's permission."""


def _tagged(resp: dict, tag: Optional[str]) -> dict:
    return {**resp, "container_tag": tag} if tag else resp


class SupermemoryMemoryProvider(MemoryProvider):
    def __init__(self):
        self._api_key = self._session_id = self._hermes_home = ""
        self._identity = "default"  # expands {identity} in container_tag and in `containers` permission keys
        self._client: Optional[_SupermemoryClient] = None
        self._container_tag, self._turn_count, self._write_enabled, self._active = _DEFAULT_CONTAINER_TAG, 0, True, False
        self._prefetch_thread = self._sync_thread = self._write_thread = None  # only _write_thread is ever started
        self._pending_turns: List[Dict[str, str]] = []  # failed writes, each tagged with its session_id; retried on next write/end/switch/shutdown
        self._capture_lock = threading.Lock()  # sync_turn (worker) vs on_session_switch/shutdown (caller thread) both touch _pending_turns
        self._socket_pin: Optional[Tuple[int, int]] = None  # the tunnel socket the start-up gate verified
        self._disabled = ""  # why a re-check dropped the client
        self._apply_config(_load_supermemory_config())
        self._base_url, self._allowed_containers = _DEFAULT_BASE_URL, []  # env var is only consulted in initialize()

    def _apply_config(self, config: dict) -> None:
        self._config = config  # kept for the availability re-check before each client use
        for key in ("auto_recall", "auto_capture", "max_recall_results", "profile_frequency", "capture_mode",
                    "search_mode", "entity_context", "api_timeout", "custom_containers", "custom_container_instructions"):
            setattr(self, f"_{key}", config[key])
        self._base_url = _resolve_base_url(config["base_url"]) if config["tunnel"] is None else _TUNNEL_BASE_URL
        self._enable_custom_containers = config["enable_custom_container_tags"]
        self._allowed_containers: List[str] = [self._container_tag] + list(self._custom_containers)
        self._quarantined_memory_ids: Dict[str, set[str]] = {
            _sanitize_tag(tag.replace("{identity}", self._identity)): set(memory_ids)
            for tag, memory_ids in config["quarantined_memory_ids"].items()
        }
        perms = config["containers"]
        self._container_permissions: Optional[Dict[str, Dict[str, bool]]] = None if perms is None else {
            _sanitize_tag(tag.replace("{identity}", self._identity)): ops for tag, ops in perms.items()}

    def _permits(self, tag: str, op: str) -> bool:
        """Operation permission for a resolved container tag; no ``containers`` map = allowed (legacy)."""
        if self._container_permissions is None:
            return True
        return self._container_permissions.get(tag, {}).get(op, False)

    def _quarantined_ids(self, tag: Optional[str] = None) -> set[str]:
        return self._quarantined_memory_ids.get(tag or self._container_tag, set())

    def _filter_quarantined(self, results: List[dict], tag: Optional[str] = None) -> List[dict]:
        """Drop results tied to a quarantined id; fail closed when provenance is missing.

        Document-mode results are chunks whose top-level id is a chunk id, so a quarantined *document* can only be
        recognized through the parent ``documents`` list (requested with ``include.documents``). In a container that
        has a quarantine, a result carrying no parent document id cannot be proven clean and is dropped.
        """
        quarantined = self._quarantined_ids(tag)
        if not quarantined:
            return results
        if "*" in quarantined:
            return []

        def _field(obj: Any, key: str) -> Any:
            return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)

        def _custom_ids(obj: Any) -> set[str]:
            ids = {str(_field(obj, key) or "").strip() for key in ("custom_id", "customId")}
            metadata = _field(obj, "metadata")
            if isinstance(metadata, dict):
                ids.update(str(metadata.get(key) or "").strip() for key in ("custom_id", "customId"))
            return ids

        def provenance(item: dict) -> tuple[set[str], set[str]]:
            """(every identifier seen, parent document ids)."""
            ids = {str(item.get("id") or "").strip()} | _custom_ids(item)
            parents: set[str] = set()
            for document in item.get("documents") or []:
                doc_id = str(_field(document, "id") or "").strip()
                parents.add(doc_id)
                ids.add(doc_id)
                ids |= _custom_ids(document)
            return ids - {""}, parents - {""}

        kept = []
        for item in results:
            ids, parents = provenance(item)
            if parents and ids.isdisjoint(quarantined):
                kept.append(item)
        return kept

    def _live_client(self) -> Optional[_SupermemoryClient]:
        """The client when it may be used right now, else None. Every client use goes through here.

        With a guard configured, each use re-checks the proof and key in the current scope and the live tunnel: the
        socket the start-up gate verified must still be there, safe, and served by this user's ssh forward. Nothing is
        cached. A failed re-check drops the client and the key for the rest of this provider's life (_disable). A new
        session re-runs is_available()/initialize() and comes back only if the gate passes again. Independently, the
        client's transport re-checks the socket file before every request."""
        if not (self._active and self._client):
            return None
        if not _guarded(self._config):
            return self._client
        key = get_secret("SUPERMEMORY_API_KEY", "") or ""
        error = _availability_error(key, self._config)
        if not error and key != self._api_key:
            error = "SUPERMEMORY_API_KEY in scope is not the key this session was started with"
        if not error:
            error, _ = _tunnel_check(self._config, self._socket_pin)
        if error:
            self._disable(error)
            return None
        return self._client

    def _disable(self, reason: str) -> None:
        logger.warning("Supermemory disabled for this session: %s. Dropped its client and API key; "
                       "a new session re-checks availability.", reason)
        self._client, self._api_key, self._active, self._disabled = None, "", False, reason
        _drop_inherited_key(reason)

    @property
    def name(self) -> str:
        return "supermemory"

    def is_available(self) -> bool:
        # No SDK import check: the SDK is lazy-installed in initialize(), so gating on importability here is a
        # chicken-and-egg trap on sealed venvs. Key presence, plus the helper's proof and the live tunnel check when
        # configured (a failure there also drops an unproven inherited key).
        return not _gate()[0]

    def unavailable_reason(self) -> str:
        return _gate()[0]

    def get_config_schema(self):
        # Only the API key is prompted during `hermes memory setup`; other options live in supermemory.json / env.
        return [{"key": "api_key", "description": "Supermemory API key", "secret": True, "required": True, "env_var": "SUPERMEMORY_API_KEY", "url": _API_KEY_URL}]

    def save_config(self, values, hermes_home):
        sanitized = dict(values or {})
        for key, fix in (("container_tag", _sanitize_tag), ("entity_context", _clamp_entity_context)):
            if key in sanitized:
                sanitized[key] = fix(str(sanitized[key]))
        _save_supermemory_config(sanitized, hermes_home)

    def get_status_config(self, provider_config: dict) -> dict:
        from hermes_constants import get_hermes_home
        hermes_home = str(get_hermes_home())
        gate_error, socket_pin = _gate(_load_supermemory_config(hermes_home))
        return {"summary": _format_connection_summary(_probe_supermemory_connection(
            get_secret("SUPERMEMORY_API_KEY", "") or "", hermes_home, gate_error=gate_error, socket_pin=socket_pin))}

    def post_setup(self, hermes_home: str, config: dict) -> None:
        from hermes_cli.config import save_config
        from hermes_cli.memory_setup import _prompt, _write_env_vars
        print(f"\n  Configuring supermemory:\n\n  Get your API key at {_API_KEY_URL}\n")
        existing = os.environ.get("SUPERMEMORY_API_KEY", "")
        masked = f"...{existing[-4:]}" if len(existing) > 4 else "set"
        val = _prompt(f"Supermemory API key (current: {masked}, blank to keep)" if existing else "Supermemory API key", secret=True)
        memory = config["memory"] = config["memory"] if isinstance(config.get("memory"), dict) else {}
        memory["provider"] = self.name
        save_config(config)
        if val:
            _write_env_vars({"SUPERMEMORY_API_KEY": val}, hermes_home=hermes_home)
        api_key = val or existing
        # Make the freshly-entered key visible to the probe below. Single-profile only: under a multiplexed
        # gateway, writing to the process-global environ would leak the key to sibling profiles and their subprocesses.
        if api_key and not is_multiplex_active() and os.environ.get("SUPERMEMORY_API_KEY") != api_key:
            os.environ["SUPERMEMORY_API_KEY"] = api_key
        # A guarded config gets the same gate as a session start, so setup never reports "Connected" for a config the
        # next session refuses. Unguarded configs keep the plain key probe.
        setup_config = _load_supermemory_config(hermes_home)
        gate_error, socket_pin = _gate(setup_config) if _guarded(setup_config) else ("", None)
        status = _probe_supermemory_connection(api_key, hermes_home, gate_error=gate_error, socket_pin=socket_pin)
        print(f"\n  {_format_connection_summary(status)}\n\n  Memory provider: supermemory\n  Activation saved to config.yaml")
        if val:
            print("  API keys saved to .env")
        print("\n  Start a new session to activate.\n")

    def initialize(self, session_id: str, **kwargs) -> None:
        from hermes_constants import get_hermes_home
        self._hermes_home = kwargs.get("hermes_home") or str(get_hermes_home())
        self._session_id, self._turn_count, self._pending_turns = session_id, 0, []
        config = _load_supermemory_config(self._hermes_home)
        # Re-checked here: a host may initialize without consulting is_available().
        self._disabled = ""
        gate_error, self._socket_pin = _gate(config)
        if gate_error and _guarded(config):
            logger.info("Supermemory inactive: %s", gate_error)
        self._api_key = "" if gate_error else (get_secret("SUPERMEMORY_API_KEY", "") or "")
        self._identity = kwargs.get("agent_identity", "default")
        self._container_tag = _resolve_container_tag(config["container_tag"], self._identity)
        self._apply_config(config)
        self._write_enabled = kwargs.get("agent_context", "") not in {"cron", "flush", "subagent"}
        self._client = _quietly(lambda: _build_client(self._api_key, config, self._container_tag, self._socket_pin),
                                "Supermemory initialization failed", level=logging.WARNING) if self._api_key else None
        self._active = self._client is not None

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        self._turn_count = max(turn_number, 0)

    def system_prompt_block(self) -> str:
        lines = ["# Supermemory", f"Active. Container: {self._container_tag}.",
                 "Use supermemory-search, supermemory-save, supermemory-forget, and supermemory-profile (aliases: supermemory_search, supermemory_store, supermemory_forget, supermemory_profile)."]
        if self._enable_custom_containers and self._custom_containers:
            lines += [f"\nMulti-container mode enabled. Available containers: {', '.join(self._allowed_containers)}.",
                      "Pass an optional container_tag to supermemory_search, supermemory_store, supermemory_forget, and supermemory_profile to target a specific container."]
            lines += [f"\n{self._custom_container_instructions}"] if self._custom_container_instructions else []
        if self._container_permissions is not None:
            access = [f"{tag}: {'/'.join(op for op in _CONTAINER_OPS if self._permits(tag, op)) or 'none'}"
                      for tag in self._allowed_containers]
            lines += [f"\nEnforced container permissions: {'; '.join(access)}. Calls outside these are refused."]
        return "\n".join(lines) if self._active else ""

    def _can_write(self) -> bool:
        return bool(self._active and self._write_enabled and self._client)

    def _may_capture(self) -> bool:
        """Automatic writes (turn capture, built-in memory mirroring): need auto_capture and primary write permission."""
        return self._can_write() and self._auto_capture and self._permits(self._container_tag, "write")

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not self._active or not self._auto_recall or not self._client or not query.strip() \
                or not self._permits(self._container_tag, "read") or (client := self._live_client()) is None:
            return ""
        def _recall():
            include_profile = self._turn_count <= 1 or (self._turn_count % self._profile_frequency == 0)
            if self._quarantined_ids():
                # The profile API aggregates facts without source ids, so it cannot enforce an exact-document
                # quarantine. Fall back to id-bearing search results for this container and filter them locally.
                results = self._filter_quarantined(
                    client.search_memories(query[:200], limit=self._max_recall_results)
                )
                return _format_prefetch_context([], [], results, self._max_recall_results)
            profile = client.get_profile(query=query[:200])
            return _format_prefetch_context(profile["static"] if include_profile else [], profile["dynamic"] if include_profile else [],
                                            profile["search_results"], self._max_recall_results)
        return _quietly(_recall, "Supermemory prefetch failed", default="")

    def _write_turns(self, mode: str, new_turn: Optional[Dict[str, str]] = None) -> None:
        """Write pending turns (+ ``new_turn``) as one documents.add per session id; custom_id = session + 4h bucket, so the
        API appends deltas. Failed batches stay pending under their own session id, so a switch never re-homes them.
        Retries are at-least-once: a write the API accepted but whose response was lost is re-sent and appended again.
        The lock serializes the snapshot/write/replace sequence across the worker and caller threads."""
        with self._capture_lock:
            turns = self._pending_turns + ([new_turn] if new_turn else [])
            if not turns or (client := self._live_client()) is None:
                return
            failed: List[Dict[str, str]] = []
            for sid in dict.fromkeys(t["session_id"] for t in turns):
                batch = [t for t in turns if t["session_id"] == sid]
                now = datetime.now(timezone.utc)
                content = "\n\n".join(_format_turn(t["user"], t["assistant"]) for t in batch)
                metadata = {"type": "conversation", "session_id": sid, "timestamp": now.isoformat()}  # no sm_capture_mode: Hermes policy
                result = _quietly(lambda: client.add_memory(content, metadata=metadata, entity_context=self._entity_context,
                                                             custom_id=_capture_custom_id(sid, now)),
                                  "Supermemory capture failed (%s, session=%s, %d turns pending)", mode, sid, len(batch),
                                  level=logging.WARNING if mode != "turn" else logging.DEBUG, default=_FAILED)
                if result is _FAILED:  # only a raised exception re-queues the batch
                    failed += batch
            self._pending_turns = failed

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        # Host runs this on a worker thread, so the blocking write is fine here.
        if not self._may_capture():
            return
        turn = {"user": _clean_text_for_capture(user_content), "assistant": _clean_text_for_capture(assistant_content),
                "session_id": session_id or self._session_id}
        if turn["user"] or turn["assistant"]:
            self._write_turns("turn", turn)
            self._bound_pending_turns()

    def _flush_pending(self, mode: str) -> None:
        if self._may_capture():  # retries are capture writes too: same switch, same approval
            self._write_turns(mode)
            self._bound_pending_turns()

    def _bound_pending_turns(self) -> None:
        """Keep the retry buffer bounded: drop OLDEST entries past the turn/byte caps.

        Without this, a persistently failing service accumulates one entry per turn for the
        process lifetime (gateway runs never re-initialize) and every retry re-sends the
        whole accumulated payload."""
        with self._capture_lock:
            if len(self._pending_turns) <= _MAX_PENDING_TURNS and \
                    sum(len(t["user"]) + len(t["assistant"]) for t in self._pending_turns) <= _MAX_PENDING_BYTES:
                return
            kept: List[Dict[str, str]] = list(self._pending_turns)
            total = sum(len(t["user"]) + len(t["assistant"]) for t in kept)
            while len(kept) > _MAX_PENDING_TURNS or total > _MAX_PENDING_BYTES:
                if not kept:
                    break
                dropped = kept.pop(0)
                total -= len(dropped["user"]) + len(dropped["assistant"])
            if len(kept) != len(self._pending_turns):
                logger.warning("Supermemory: dropped %d oldest pending turn(s) to keep the retry buffer bounded",
                               len(self._pending_turns) - len(kept))
            self._pending_turns = kept

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        # Turns were already written as they completed; only retry what failed.
        self._flush_pending("session_end")

    def on_session_switch(self, new_session_id: str, *, parent_session_id: str = "", reset: bool = False, **kwargs) -> None:
        # Pending turns survive the switch: they carry their own session_id, so a later retry still lands on the old session.
        self._flush_pending("session_switch")
        if self._can_write():
            self._turn_count = 0
        self._session_id = str(new_session_id or "").strip() or self._session_id

    def on_memory_write(self, action: str, target: str, content: str) -> None:
        # Mirroring a built-in memory write exports local memory, so it is capture: same switch, same approval.
        if not self._may_capture() or action != "add" or not (content or "").strip() or (client := self._live_client()) is None:
            return
        if self._write_thread and self._write_thread.is_alive():
            self._write_thread.join(timeout=2.0)
        self._write_thread = spawn_context_thread(
            _quietly, daemon=False, name="supermemory-memory-write",
            args=(lambda: client.add_memory(content.strip(), metadata={"target": target, "type": "explicit_memory"},
                                            entity_context=self._entity_context), "Supermemory on_memory_write failed"))
        self._write_thread.start()

    def shutdown(self) -> None:
        self._flush_pending("shutdown")
        if self._write_thread and self._write_thread.is_alive():
            self._write_thread.join(timeout=5.0)
        self._prefetch_thread = self._sync_thread = self._write_thread = None

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        schemas = [json.loads(json.dumps(base)) for base in _BASE_SCHEMAS]  # deep copies
        for schema in schemas if self._enable_custom_containers else ():  # multi-container mode: every tool takes container_tag
            schema["parameters"]["properties"]["container_tag"] = {
                "type": "string", "description": f"Optional container tag. Allowed: {', '.join(self._allowed_containers)}. Defaults to primary ({self._container_tag})."}
        # Kebab-case aliases are appended after all snake_case schemas (deep-copied, name swapped).
        return schemas + [{**json.loads(json.dumps(s)), "name": _KEBAB_ALIASES[s["name"]]} for s in schemas]

    def _tool_container_tag(self, args: dict) -> Optional[str]:
        """Validated container_tag from args; None = primary. Raises _TagError when not whitelisted."""
        raw = str(args.get("container_tag") or "").strip() if self._enable_custom_containers else ""
        tag = _sanitize_tag(raw) if raw else None
        if tag and tag not in self._allowed_containers:
            raise _TagError(f"Container tag '{tag}' is not allowed. Allowed: {', '.join(self._allowed_containers)}")
        return tag

    def _permitted_tag(self, args: dict, op: str) -> Optional[str]:
        """``_tool_container_tag`` plus the ``containers`` permission for ``op``; refusals are logged, not silent."""
        tag = self._tool_container_tag(args)
        target = tag or self._container_tag
        if not self._permits(target, op):
            logger.warning("Supermemory: refused %s on container %s (not permitted by supermemory.json containers)", op, target)
            raise _TagError(f"Refused: container '{target}' does not permit {op} (supermemory.json containers permissions).")
        return tag

    def _tool_store(self, args: dict) -> dict | str:
        content = str(args.get("content") or "").strip()
        if not content:
            return tool_error("content is required")
        metadata = args.get("metadata") if isinstance(args.get("metadata"), dict) else {}
        metadata.setdefault("type", _detect_category(content))
        metadata.pop("source", None)
        tag = self._permitted_tag(args, "write")
        result = self._client.add_memory(content, metadata=metadata, entity_context=self._entity_context, container_tag=tag)
        return _tagged({"saved": True, "id": result.get("id", ""), "preview": content[:80] + ("..." if len(content) > 80 else "")}, tag)

    def _tool_search(self, args: dict) -> dict | str:
        query = str(args.get("query") or "").strip()
        if not query:
            return tool_error("query is required")
        limit = _clamp_number(args.get("limit", 5) or 5, 5, 1, 20, int)
        tag = self._permitted_tag(args, "read")
        raw_results = self._client.search_memories(query, limit=limit, container_tag=tag)
        results = [{"id": i.get("id", ""), "content": i.get("memory", ""), **({"similarity": pct} if (pct := _similarity_pct(i.get("similarity"))) is not None else {})}
                   for i in self._filter_quarantined(raw_results, tag)]
        return _tagged({"results": results, "count": len(results)}, tag)

    def _tool_forget(self, args: dict) -> dict | str:
        memory_id, query = str(args.get("id") or "").strip(), str(args.get("query") or "").strip()
        if not memory_id and not query:
            return tool_error("Provide either id or query")
        tag = self._permitted_tag(args, "write")  # not echoed in the response
        if not memory_id:
            results = self._filter_quarantined(
                self._client.search_memories(query, limit=5, container_tag=tag), tag
            )
            memory_id = results[0].get("id", "") if results else ""
            if not memory_id:
                return {"success": False, "message": "No non-quarantined matching memory found to forget."}
            self._client.forget_memory(memory_id, container_tag=tag)
            return {"success": True, "message": "Forgot the best non-quarantined match.", "id": memory_id}
        self._client.forget_memory(memory_id, container_tag=tag)
        return {"forgotten": True, "id": memory_id}

    def _tool_profile(self, args: dict) -> dict:
        tag = self._permitted_tag(args, "read")
        if self._quarantined_ids(tag):
            raise _TagError(
                f"Refused: container '{tag or self._container_tag}' has quarantined memory ids; "
                "use supermemory-search so exact-document quarantine can be enforced."
            )
        profile = self._client.get_profile(query=str(args.get("query") or "").strip() or None, container_tag=tag)
        return _tagged({"profile": "\n\n".join(_profile_sections(profile["static"], profile["dynamic"])),
                       "static_count": len(profile["static"]), "dynamic_count": len(profile["dynamic"])}, tag)

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        """Handlers return a tool_error() string for bad args or a dict to JSON-encode; client failures get ``fail_prefix``."""
        if self._live_client() is None:
            return tool_error(f"Supermemory is disabled for this session: {self._disabled}" if self._disabled
                              else "Supermemory is not configured")
        tool_name = _ALIAS_TO_TOOL.get(tool_name, tool_name)
        if tool_name not in self._TOOL_HANDLERS:
            return tool_error(f"Unknown tool: {tool_name}")
        handler, fail_prefix = self._TOOL_HANDLERS[tool_name]
        try:
            resp = handler(self, args)
        except Exception as exc:
            return tool_error(str(exc) if isinstance(exc, _TagError) else f"{fail_prefix}: {exc}")
        return resp if isinstance(resp, str) else json.dumps(resp)

    # snake_case tool name -> (handler, error prefix); kebab aliases are folded in via _ALIAS_TO_TOOL first.
    _TOOL_HANDLERS = {"supermemory_store": (_tool_store, "Failed to store memory"), "supermemory_search": (_tool_search, "Search failed"),
                      "supermemory_forget": (_tool_forget, "Forget failed"), "supermemory_profile": (_tool_profile, "Profile failed")}


def register(ctx):
    ctx.register_memory_provider(SupermemoryMemoryProvider())


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.

FORGET_SCHEMA = {
    "name": "supermemory_forget",
    "description": "Forget a memory by exact id or by best-match query.",
    "parameters": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Exact memory id to delete."},
            "query": {"type": "string", "description": "Query used to find the memory to forget."},
        },
    },
}

PROFILE_SCHEMA = {
    "name": "supermemory_profile",
    "description": "Retrieve persistent profile facts and recent memory context.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Optional query to focus the profile response."},
        },
    },
}

SEARCH_SCHEMA = {
    "name": "supermemory_search",
    "description": "Search long-term memory by semantic similarity.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for."},
            "limit": {"type": "integer", "description": "Maximum results to return, 1 to 20."},
        },
        "required": ["query"],
    },
}

STORE_SCHEMA = {
    "name": "supermemory_store",
    "description": "Store an explicit memory for future recall.",
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The memory content to store."},
            "metadata": {"type": "object", "description": "Optional metadata attached to the memory."},
        },
        "required": ["content"],
    },
}
# ---- END PLUGIN-COMPAT ----
