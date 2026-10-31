"""Read-only live-source capture; emit a narrow candidate, never install it."""
from pathlib import Path
import difflib
import hashlib
import json

LIVE = Path('/Users/sheldon/.hermes/releases/stable-v0.22.5-carry-d4c5aec662c8/candidate')
OUT = Path(__file__).resolve().parent
HELPER = '/Users/sheldon/.hermes/profiles/shld-keys-router/tools/key_op.py'
ENV_SHA = '755ed19cd94bafff49390e005030880628af66cc753899142d123dd8f8f82aaa'

def sha(data):
    return hashlib.sha256(data).hexdigest()

def replace_once(source, before, after):
    if source.count(before) != 1:
        raise ValueError('overlay anchor is not unique')
    return source.replace(before, after, 1)

def overlay(source):
    source = replace_once(source, '_OP_RUN_TIMEOUT = 30', '''_OP_RUN_TIMEOUT = 30
_ONEPASSWORD_MAX_CACHE_TTL = 1740.0  # plus env-loader's 60-second window
_PINNED_SHARED_CACHE_HELPER = "/Users/sheldon/.hermes/profiles/shld-keys-router/tools/key_op.py"


def _bounded_cache_ttl(value: object) -> float:
    """Bound every public entry path; invalid/nonfinite TTLs disable caching."""
    import math

    try:
        ttl = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return min(ttl, _ONEPASSWORD_MAX_CACHE_TTL) if math.isfinite(ttl) and ttl > 0 else 0.0''')
    source = replace_once(source, '    valid, warnings = _validate_references(references)\n', '''    cache_ttl_seconds = _bounded_cache_ttl(cache_ttl_seconds)
    # The exact opted-in transport owns secret age. Re-timestamping its cached
    # result here would extend the shared TTL by another outer cache window.
    if binary_path == _PINNED_SHARED_CACHE_HELPER or str(binary) == _PINNED_SHARED_CACHE_HELPER:
        use_cache = False
    use_cache = use_cache and cache_ttl_seconds > 0
    valid, warnings = _validate_references(references)
''')
    source = replace_once(source, '            binary=binary, cache_ttl_seconds=cache_ttl_seconds, home_path=home_path)', '            binary=binary, binary_path=binary_path, cache_ttl_seconds=cache_ttl_seconds, home_path=home_path)')
    source = replace_once(source, '                binary=binary, cache_ttl_seconds=coerce_float', '                binary=binary, binary_path=binary_path, cache_ttl_seconds=coerce_float')
    source = replace_once(source, 'Disk+memory cache TTL; 0 disables', 'Disk+memory cache TTL; max 1740 seconds plus 60 outer; 0 disables')
    source = replace_once(source, '        if cached is not None:\n            return dict(cached.secrets), warnings', '        if cached is not None and 0 <= time.time() - cached.fetched_at < cache_ttl_seconds:\n            return dict(cached.secrets), warnings')
    source = replace_once(source, '    secrets: dict[str, str] = {}\n    read_errors = 0', '    fetched_at = time.time()  # before any provider read; never re-age on publication\n    secrets: dict[str, str] = {}\n    read_errors = 0')
    source = replace_once(source, '    if use_cache and not read_errors and secrets:', '    if not 0 <= time.time() - fetched_at < _ONEPASSWORD_MAX_CACHE_TTL:\n        return {}, warnings + ["1Password fetch exceeded freshness window"]\n\n    if use_cache and not read_errors and secrets:')
    source = replace_once(source, '_refs_fingerprint(valid))', '"age-budget-v2:" + _refs_fingerprint(valid))')
    source = replace_once(source, 'CachedFetch(secrets=dict(secrets), fetched_at=time.time())', 'CachedFetch(secrets=dict(secrets), fetched_at=fetched_at)')
    return source

def main():
    paths = ['agent/secret_sources/onepassword.py', 'agent/secret_sources/_cache.py', 'hermes_cli/env_loader.py']
    raw = {p: (LIVE / p).read_bytes() for p in paths}
    hashes = {p: sha(b) for p, b in raw.items()}
    if hashes[paths[2]] != ENV_SHA:
        raise ValueError('env_loader exact-source hash mismatch')
    manifest_path = OUT / 'source_manifest.json'
    if manifest_path.exists():
        expected = json.loads(manifest_path.read_text())['source_sha256']
        if hashes != expected:
            raise ValueError('source changed: refusing to regenerate for a different live snapshot')
    before = raw[paths[0]].decode()
    after = overlay(before)
    compile(after, '<candidate-onepassword>', 'exec')
    patch = ''.join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), fromfile='a/'+paths[0], tofile='b/'+paths[0]))
    (OUT / 'onepassword.patch').write_text(patch)
    (OUT / 'onepassword.candidate.py').write_text(after)
    manifest = {'source_root': str(LIVE), 'source_sha256': hashes, 'candidate_sha256': sha(after.encode()), 'patch_sha256': sha(patch.encode()), 'installed': False}
    manifest_path.write_text(json.dumps(manifest, indent=2)+'\n')
    print(json.dumps(manifest, indent=2))

if __name__ == '__main__':
    main()
