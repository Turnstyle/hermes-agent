#!/usr/bin/env python3
"""Offline exact-source overlay builder/verifier; never imports router or credentials."""
import ast
import difflib
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = Path('/Users/sheldon/.hermes/profiles/shld-keys-router/tools/key_op.py')
ROUTER = SOURCE.with_name('keys_router_cli.py')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def main():
    original = SOURCE.read_bytes()
    text = original.decode()
    old = "        value = subprocess.run([module.OP, 'read', '--no-newline', '--', reference],\n                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,\n                               stderr=subprocess.DEVNULL, env=custody_env(module),\n                               timeout=5, start_new_session=True)\n        if value.returncode or not value.stdout or len(value.stdout)>65536 or b'\\n' in value.stdout or b'\\r' in value.stdout:\n            raise RuntimeError()\n        return value.stdout"
    new = "        from secret_value_cache import get\n        return get(reference, lambda canonical_ref: _current_provider_read(canonical_ref, module))"
    assert text.count(old) == 1, 'source drift: exact current provider block absent'
    helper = '''def _current_provider_read(reference, module):
    # Unchanged custody and desktop guards, now invoked only on a cache miss.
    value = subprocess.run([module.OP, 'read', '--no-newline', '--', reference],
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, env=custody_env(module),
                           timeout=5, start_new_session=True)
    if value.returncode or not value.stdout or len(value.stdout)>65536 or b'\\n' in value.stdout or b'\\r' in value.stdout:
        raise RuntimeError()
    return value.stdout


'''
    candidate = text.replace(old, new).replace('def resolve(reference, module):', helper + 'def resolve(reference, module):')
    src, dst = ast.parse(text), ast.parse(candidate)
    funcs = lambda tree: {n.name: ast.dump(n, include_attributes=False) for n in tree.body if isinstance(n, ast.FunctionDef)}
    a, b = funcs(src), funcs(dst)
    assert all(a[k] == b[k] for k in a if k != 'resolve'), 'unrelated source changed'
    # Preserve legacy/non-current fallback byte-for-byte, including env scrubbing.
    assert text[text.index('    # The entire read'):text.index('\n\ndef main')] == candidate[candidate.index('    # The entire read'):candidate.index('\n\ndef main')]
    (HERE / 'key_op.candidate.py').write_text(candidate)
    (HERE / 'key_op.exact-source.patch').write_text(''.join(difflib.unified_diff(text.splitlines(True), candidate.splitlines(True), fromfile='a/tools/key_op.py', tofile='b/tools/key_op.py')))
    manifest = {
        'status': 'PROPOSED_PENDING_SHLD_DEPLOY_DEPUTY',
        'source_path': str(SOURCE), 'source_sha256': sha(original),
        'router_read_only_path': str(ROUTER), 'router_sha256': sha(ROUTER.read_bytes()),
        'target_cache_directory': '/Users/sheldon/.hermes/profiles/shld-keys-router/cache/secret-values-v1',
        'files': {name: sha((HERE / name).read_bytes()) for name in (
            'key_op.candidate.py', 'key_op.exact-source.patch', 'secret_value_cache.py',
            'cleanup.launchd.plist', 'test_transport.py', 'build_overlay.py', 'verify_offline.py', 'README.md')},
        'unchanged_function_ast_checks': sorted(k for k in a if k != 'resolve'),
        'no_deployment_performed': True,
    }
    (HERE / 'source-overlay-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()
