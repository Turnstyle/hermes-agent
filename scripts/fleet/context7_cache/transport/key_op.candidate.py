#!/opt/homebrew/bin/python3
"""Private 1Password transport for configured consumers; never invoke as a chat tool.

Native `op read` stdout is a secret pipe to Hermes, not a diagnostic. Commands
are launched only explicitly; importing/sourcing a shell file runs nothing.
"""
import importlib.util
import os
from pathlib import Path
import re
import subprocess
import sys
import time

REF = re.compile(r'op://(?:jtp-bots-suite|keys-router|2uadhfojbtwx2zlvpz4tw5375q)/[A-Za-z0-9][A-Za-z0-9_. -]{0,199}/[A-Za-z0-9][A-Za-z0-9_. -]{0,95}\Z')
CURRENT = ('TYPESAFE_API_KEY', 'CONTEXT7_API_KEY')
CURRENT_REFS = frozenset('op://keys-router/' + title + '/credential' for title in
    ('jtp-bots-suite Jev API Key from TypeSafe AI', 'jtp-bots-suite Context7 API Key'))
# Lane K, 29 Oct 2026: owner renamed the fleet Jev item (same id/value) and made per-profile
# Jev items. Exact item-id refs only (fleet item + shld-cndr + shld-king); same vault and token.
KR_VAULTS = ('op://keys-router/', 'op://2uadhfojbtwx2zlvpz4tw5375q/')
JEV_ITEM_IDS = ('z75ikclocxoipbuaijaokcyaci', 'td6qii4vwjxhydnrkosz2sci6m', '6sluosa3usmgdkrzdduoq257ba')
CURRENT_REFS = CURRENT_REFS | frozenset(v + i + '/credential' for v in KR_VAULTS for i in JEV_ITEM_IDS)
LEGACY_REFS = {'op://keys-router/jtp-bots-suite Jev API Key from TypeSafe AI/credential': 'op://2uadhfojbtwx2zlvpz4tw5375q/z75ikclocxoipbuaijaokcyaci/credential'}


def custody_env(module):
    """Only this node's own router token; no ambient/desktop/fleet fallback."""
    path = Path(__file__).resolve().parent.parent / '.env'
    if any(p.is_symlink() for p in [path, *path.parents]) or not path.is_file():
        raise RuntimeError()
    if path.stat().st_size > 1024 * 1024:
        raise RuntimeError()
    matches = re.findall(r'^\s*(?:export\s+)?OP_SERVICE_ACCOUNT_TOKEN\s*=\s*([^\r\n]+)', path.read_text(), re.M)
    if len(matches) != 1:
        raise RuntimeError()
    token = matches[0].strip()
    if len(token) >= 2 and token[0] in "\"'" and token[-1] == token[0]:
        token = token[1:-1]
    if not re.fullmatch(r'ops_[A-Za-z0-9_.-]+', token):
        raise RuntimeError()
    env = {k:v for k,v in module.base_env().items() if k in ('HOME','PATH','TMPDIR','USER','LANG')}
    env.update(OP_SERVICE_ACCOUNT_TOKEN=token, OP_LOAD_DESKTOP_APP_SETTINGS='false',
               OP_BIOMETRIC_UNLOCK_ENABLED='false')
    return env


def router():
    path = Path(__file__).with_name('keys_router_cli.py')
    spec = importlib.util.spec_from_file_location('_custody_router', path)
    if spec is None or spec.loader is None:
        raise RuntimeError()
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _current_provider_read(reference, module):
    # Unchanged custody and desktop guards, now invoked only on a cache miss.
    value = subprocess.run([module.OP, 'read', '--no-newline', '--', reference],
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, env=custody_env(module),
                           timeout=5, start_new_session=True)
    if value.returncode or not value.stdout or len(value.stdout)>65536 or b'\n' in value.stdout or b'\r' in value.stdout:
        raise RuntimeError()
    return value.stdout


def resolve(reference, module):
    if not isinstance(reference, str) or not REF.fullmatch(reference):
        raise RuntimeError()
    reference = LEGACY_REFS.get(reference, reference)
    if reference.startswith(KR_VAULTS):
        if reference not in CURRENT_REFS:
            raise RuntimeError()
        from secret_value_cache import get
        return get(reference, lambda canonical_ref: _current_provider_read(canonical_ref, module))
    # The entire read, including bootstrap, is capped at five seconds.
    deadline = time.monotonic() + 5
    env = module.base_env()
    for name in tuple(env):
        if name.startswith(('OP_SESSION_', 'OP_CONNECT_')) or name in ('OP_SERVICE_ACCOUNT_TOKEN', 'OP_ACCOUNT'):
            env.pop(name, None)
    env['OP_LOAD_DESKTOP_APP_SETTINGS'] = 'false'
    env['OP_BIOMETRIC_UNLOCK_ENABLED'] = 'false'
    token = subprocess.run([module.GCLOUD, 'secrets', 'versions', 'access', 'latest',
                            '--secret=' + module.BOOTSTRAP_SECRET, *module.gcloud_args()],
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, env=env, timeout=min(2, deadline-time.monotonic()),
                           start_new_session=True)
    if token.returncode or not token.stdout.strip():
        raise RuntimeError()
    env['OP_SERVICE_ACCOUNT_TOKEN'] = token.stdout.decode().strip()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError()
    value = subprocess.run([module.OP, 'read', '--no-newline', '--', reference],
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, env=env, timeout=remaining,
                           start_new_session=True)
    if value.returncode or not value.stdout or len(value.stdout)>65536 or b'\n' in value.stdout or b'\r' in value.stdout:
        raise RuntimeError()
    return value.stdout


def main(args):
    try:
        module = router()
        if args == ['--version']:
            print('key-op custody transport t_cf1c649f')
            return 0
        if args[:1] == ['read']:
            refs = [a for a in args[1:] if a not in ('--no-newline', '--')]
            if len(refs) != 1 or any(a.startswith('-') and a not in ('--no-newline', '--') for a in args[1:]):
                raise RuntimeError()
            value = resolve(refs[0], module)
            sys.stdout.buffer.write(value + (b'' if '--no-newline' in args else b'\n'))
            return 0
        if len(args) >= 3 and args[:2] == ['run', '--']:
            env = dict(os.environ)
            refs = {k:v for k,v in env.items() if REF.fullmatch(v)}
            if not refs:
                raise RuntimeError()
            # Never let an inherited old key provide false launch proof.
            for key in CURRENT:
                env.pop(key, None)
            for name, reference in refs.items():
                env.pop(name, None)
                env[name] = resolve(reference, module).decode()
            os.execvpe(args[2], args[2:], env)
        raise RuntimeError()
    except Exception:
        for key in CURRENT:
            os.environ.pop(key, None)
        print('key-op: credential unset; custody source unavailable', file=sys.stderr)
        return 3


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
