"""Incremental exact-live env_loader TTL candidate. Never imports or installs Hermes."""
import difflib
import json
from build_overlay import LIVE, OUT, ENV_SHA, sha, replace_once


def overlay(source):
    source = replace_once(source, 'import threading\n', 'import threading\nimport time\n')
    source = replace_once(source, '_APPLIED_HOMES: set[str] = set()', '''_APPLIED_HOMES: set[str] = set()
# Start age BEFORE resolving: publishing a slow fetch must not restart its lifetime.
_SECRET_SOURCE_MAX_AGE = 1800.0
_SECRET_SOURCE_FETCHED_AT: dict[str, float] = {}
_SECRET_SOURCE_CLOCK = time.monotonic''')
    source = replace_once(source, 'def get_secret_source_values(hermes_home: str | os.PathLike) -> dict[str, str]:\n    """Return the external-secret value snapshot for ``hermes_home``."""\n    return dict(_SECRET_SOURCE_VALUES_BY_HOME.get(str(Path(hermes_home).resolve()), {}))', '''def _secret_source_cache_fresh(home_key: str) -> bool:
    """Locked freshness gate for guards, snapshots and dotenv restore; never fetches."""
    started = _SECRET_SOURCE_FETCHED_AT.get(home_key)
    if started is not None and 0 <= _SECRET_SOURCE_CLOCK() - started < _SECRET_SOURCE_MAX_AGE:
        return True
    _APPLIED_HOMES.discard(home_key)
    _SECRET_SOURCE_FETCHED_AT.pop(home_key, None)
    _SECRET_SOURCE_VALUES_BY_HOME.pop(home_key, None)
    _SECRET_SOURCE_RESTORE_BY_HOME.pop(home_key, None)
    # Do not let our expired process write become a non-overriding provider's
    # "existing" value, or survive as an ambient fallback after a failed refresh.
    # Unlike config revocation, expiry must also peel our own write over a
    # dotenv placeholder: merely owning that NAME does not make the old secret fresh.
    for name, (_source, value, prior) in _SECRET_SOURCE_WRITES_BY_HOME.get(home_key, {}).items():
        if os.environ.get(name) == value:
            if prior is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = prior
    _revoke_secret_source_writes(Path(home_key), keep=lambda _name, _source: False)
    return False


def get_secret_source_values(hermes_home: str | os.PathLike) -> dict[str, str]:
    """Return a copy of this home's unexpired snapshot; never fetch or use ambient env."""
    home_key = str(Path(hermes_home).resolve())
    with _SECRET_SOURCE_CACHE_LOCK:
        if not _secret_source_cache_fresh(home_key):
            return {}
        return dict(_SECRET_SOURCE_VALUES_BY_HOME.get(home_key, {}))''')
    source = source.replace('    if home_key in _APPLIED_HOMES:\n', '    if _secret_source_cache_fresh(home_key) and home_key in _APPLIED_HOMES:\n')
    # Both private hydration and process-global paths use a single captured start.
    source = replace_once(source, '    # A retry must not keep serving a partial result', '    fetched_at = _SECRET_SOURCE_CLOCK()\n\n    # A retry must not keep serving a partial result')
    source = replace_once(source, '    _SECRET_SOURCE_VALUES_BY_HOME[home_key] = values\n    return dict(values)', '    _SECRET_SOURCE_VALUES_BY_HOME[home_key] = values\n    _SECRET_SOURCE_FETCHED_AT[home_key] = fetched_at\n    return get_secret_source_values(home)')
    # Reset must serialize with publication; keep the established reset semantics.
    source = replace_once(source, 'def reset_secret_source_cache(hermes_home: str | os.PathLike | None = None) -> None:', '''def reset_secret_source_cache(hermes_home: str | os.PathLike | None = None) -> None:
    """Serialize per-home or whole-process invalidation with source publication."""
    with _SECRET_SOURCE_CACHE_LOCK:
        _reset_secret_source_cache(hermes_home)


def _reset_secret_source_cache(hermes_home: str | os.PathLike | None = None) -> None:''')
    source = replace_once(source, '        _APPLIED_HOMES.clear()\n', '        _APPLIED_HOMES.clear()\n        _SECRET_SOURCE_FETCHED_AT.clear()\n')
    source = replace_once(source, '    _APPLIED_HOMES.discard(home_key)\n    _SECRET_SOURCE_VALUES_BY_HOME.pop(home_key, None)', '    _APPLIED_HOMES.discard(home_key)\n    _SECRET_SOURCE_FETCHED_AT.pop(home_key, None)\n    _SECRET_SOURCE_VALUES_BY_HOME.pop(home_key, None)')
    source = replace_once(source, '''    if _SECRET_SOURCE_RESTORE_BY_HOME:
        for name, value in _SECRET_SOURCE_RESTORE_BY_HOME.get(str(home_path.resolve()), {}).items():
            if os.environ.get(name) != value:
                os.environ[name] = value''', '''    with _SECRET_SOURCE_CACHE_LOCK:
        home_key = str(home_path.resolve())
        if _secret_source_cache_fresh(home_key):
            for name, value in _SECRET_SOURCE_RESTORE_BY_HOME.get(home_key, {}).items():
                if os.environ.get(name) != value:
                    os.environ[name] = value''')
    source = replace_once(source, 'def _apply_external_secret_sources(home_path: Path) -> None:', '''def _apply_external_secret_sources(home_path: Path) -> None:
    """Serialize freshness checks and source publication with hydration/reset."""
    with _SECRET_SOURCE_CACHE_LOCK:
        _apply_external_secret_sources_locked(home_path)


def _apply_external_secret_sources_locked(home_path: Path) -> None:''')
    source = replace_once(source, '    # Neither early return marks the home applied:', '    fetched_at = _SECRET_SOURCE_CLOCK()\n\n    # Neither early return marks the home applied:')
    source = replace_once(source, '    _APPLIED_HOMES.add(home_key)\n\n    if report.applied_any:', '    _APPLIED_HOMES.add(home_key)\n    _SECRET_SOURCE_FETCHED_AT[home_key] = fetched_at\n\n    if report.applied_any:')
    source = replace_once(source, '    for src in report.sources:\n        if src.applied:', '    _secret_source_cache_fresh(home_key)  # discard even a fetch that took the entire window\n\n    for src in report.sources:\n        if src.applied:')
    return source


def main():
    relative = 'hermes_cli/env_loader.py'
    raw = (LIVE / relative).read_bytes()
    if sha(raw) != ENV_SHA:
        raise ValueError('env_loader exact-live source drift')
    before = raw.decode()
    after = overlay(before)
    compile(after, '<candidate-env-loader>', 'exec')
    patch = ''.join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), fromfile='a/'+relative, tofile='b/'+relative))
    (OUT / 'env_loader.candidate.py').write_text(after)
    (OUT / 'env_loader.patch').write_text(patch)
    manifest = dict(source_root=str(LIVE), source_path=relative, source_sha256=sha(raw), candidate_sha256=sha(after.encode()), patch_sha256=sha(patch.encode()), installed=False)
    (OUT / 'env_loader_manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()
