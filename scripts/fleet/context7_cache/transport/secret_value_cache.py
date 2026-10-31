#!/usr/bin/env python3
"""Private, same-UID shared cache. No environment overrides or provider imports.

Deployment is proposed only. Install beside key_op.py through shld-deploy-deputy.
"""
import base64
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import time

CACHE_DIR = Path('/Users/sheldon/.hermes/profiles/shld-keys-router/cache/secret-values-v1')
MAX_TTL = 1740  # Reserve 60 seconds for the env-loader publication cache.
DEFAULT_TTL = 1740
_VAULTS = ('op://keys-router/', 'op://2uadhfojbtwx2zlvpz4tw5375q/')
_IDS = ('z75ikclocxoipbuaijaokcyaci', 'td6qii4vwjxhydnrkosz2sci6m', '6sluosa3usmgdkrzdduoq257ba')
LEGACY_REFS = {'op://keys-router/jtp-bots-suite Jev API Key from TypeSafe AI/credential':
               'op://2uadhfojbtwx2zlvpz4tw5375q/z75ikclocxoipbuaijaokcyaci/credential'}
ALLOWED_REFS = frozenset(v + i + '/credential' for v in _VAULTS for i in _IDS) | frozenset({
    'op://keys-router/jtp-bots-suite Context7 API Key/credential', *LEGACY_REFS})
FIELDS = {'v', 'ref', 'created', 'monotonic', 'expires', 'ttl', 'value'}
MAX_RECORD = 90000


class CacheSecurityError(RuntimeError):
    """Unsafe cache path. Never bypass this failure to call the provider."""


def canonical(reference):
    if not isinstance(reference, str) or reference not in ALLOWED_REFS:
        raise CacheSecurityError('reference not allowed')
    return LEGACY_REFS.get(reference, reference)


def identity(reference):
    return hashlib.sha256(canonical(reference).encode('utf-8')).hexdigest()


def validate_value(value):
    # Current allowed consumers use printable API keys, never op auth/session tokens.
    if (type(value) is not bytes or not 1 <= len(value) <= 65536
            or not re.fullmatch(rb'[!-~]+', value)
            or value.startswith((b'ops_', b'OP_SESSION_', b'op://', b'Bearer '))):
        raise ValueError('invalid secret value')
    return value


def _dir(path, create=False):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise CacheSecurityError('absolute private path required')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for n, part in enumerate(path.parts[1:]):
            last = n == len(path.parts[1:]) - 1
            if last and create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
            s = os.fstat(fd)
            # Root-owned system ancestors are necessary; all user ancestors must
            # be current-UID, never writable by other users. Leaf is exactly0700.
            if (s.st_uid not in (0, os.getuid()) or s.st_mode & 0o022
                    or (last and (s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) != 0o700))):
                raise CacheSecurityError('unsafe directory')
        return fd
    except BaseException:
        os.close(fd)
        raise


def _regular(fd):
    s = os.fstat(fd)
    if (not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid()
            or stat.S_IMODE(s.st_mode) != 0o600 or s.st_nlink != 1):
        raise CacheSecurityError('unsafe cache file')
    return s


@contextmanager
def _locked(directory_fd, key):
    # Separate exclusive creation from opening an existing lock. Darwin can
    # return ENOENT during concurrent O_CREAT|O_NOFOLLOW opens; bounded retry
    # never relaxes no-follow or validation and never removes a lock inode.
    for attempt in range(8):
        try:
            fd = os.open(key + '.lock', os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK,
                         0o600, dir_fd=directory_fd)
            break
        except FileExistsError:
            try:
                fd = os.open(key + '.lock', os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory_fd)
                break
            except FileNotFoundError:
                if attempt == 7:
                    raise
        except FileNotFoundError:
            if attempt == 7:
                raise
    try:
        _regular(fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        _regular(fd)
        yield
    finally:
        os.close(fd)


def _unique(pairs):
    result = {}
    for k, v in pairs:
        if k in result:
            raise ValueError('duplicate field')
        result[k] = v
    return result


def _now():
    # Older Apple Python monotonic() has a process-local epoch. Persisted
    # freshness requires the shared system-wide clock across independent readers.
    return time.time(), time.clock_gettime(time.CLOCK_MONOTONIC)


def _read(directory_fd, key, reference, wall, mono):
    try:
        fd = os.open(key + '.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    except FileNotFoundError:
        return None
    try:
        s = _regular(fd)
        if s.st_size > MAX_RECORD:
            return None
        with os.fdopen(os.dup(fd), 'rb') as stream:
            raw = stream.read(MAX_RECORD + 1)
        if len(raw) > MAX_RECORD:
            return None
        try:
            r = json.loads(raw, object_pairs_hook=_unique)
            if type(r) is not dict or set(r) != FIELDS or type(r['v']) is not int or r['v'] != 2 or r['ref'] != reference:
                return None
            for name in ('created', 'monotonic', 'expires', 'ttl'):
                if type(r[name]) not in (int, float) or not math.isfinite(r[name]) or r[name] < 0:
                    return None
            if not 0 < r['ttl'] <= MAX_TTL or r['expires'] != r['created'] + r['ttl']:
                return None
            dw, dm = wall - r['created'], mono - r['monotonic']
            if not (0 <= dw < r['ttl'] and 0 <= dm < r['ttl']) or abs(dw - dm) > 5:
                return None
            if type(r['value']) is not str:
                return None
            value = base64.b64decode(r['value'], validate=True)
            if base64.b64encode(value).decode('ascii') != r['value']:
                return None
            return validate_value(value)
        except (ValueError, TypeError, KeyError, UnicodeError, OverflowError):
            return None
    finally:
        os.close(fd)


def _atomic(directory_fd, key, record):
    name = key + '.' + secrets.token_hex(12) + '.tmp'
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=directory_fd)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(json.dumps(record, sort_keys=True, allow_nan=False).encode('ascii'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, key + '.json', src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        try:
            os.unlink(name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _event(directory_fd, callback, event, key):
    # Bounded per-reference counters, not an unbounded log. Called under flock.
    # No reference text, value, value hash, error, auth token or environment.
    fields = {'v', 'ref_id', 'hit', 'miss', 'provider_read', 'event'}
    record = dict(v=1, ref_id=key, hit=0, miss=0, provider_read=0, event=event)
    try:
        fd = os.open(key + '.metadata.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory_fd)
    except FileNotFoundError:
        pass
    else:
        try:
            if _regular(fd).st_size > 4096:
                raise CacheSecurityError('invalid metadata')
            with os.fdopen(os.dup(fd), 'rb') as stream:
                previous = json.loads(stream.read(4097), object_pairs_hook=_unique)
            if (type(previous) is not dict or set(previous) != fields
                    or type(previous['v']) is not int or previous['v'] != 1
                    or previous['ref_id'] != key
                    or previous['event'] not in ('hit', 'miss', 'provider_read')
                    or any(type(previous[n]) is not int or not 0 <= previous[n] < 2**63
                           for n in ('hit', 'miss', 'provider_read'))):
                raise CacheSecurityError('invalid metadata')
            record = previous
        finally:
            os.close(fd)
    record[event] = min(record[event] + 1, 2**63 - 1)
    record['event'] = event
    _atomic(directory_fd, key + '.metadata', record)
    if callback is not None:
        callback({'event': event, 'ref_id': key})


def get(reference, provider, *, ttl=DEFAULT_TTL, evidence=None, _directory=None, _clock=None):
    """Canonicalize first. Provider runs once per successful fill under per-ref flock.

    Underscore injection points exist only for offline tests, never environment/config.
    """
    reference = canonical(reference)
    if type(ttl) not in (int, float) or not math.isfinite(ttl) or not 0 < ttl <= MAX_TTL:
        raise ValueError('invalid TTL')
    clock = _clock or _now
    key = identity(reference)
    directory_fd = _dir(CACHE_DIR if _directory is None else _directory, create=True)
    try:
        with _locked(directory_fd, key):
            wall, mono = clock()
            cached = _read(directory_fd, key, reference, wall, mono)
            if cached is not None:
                _event(directory_fd, evidence, 'hit', key)
                return cached
            # Remove expired/corrupt entries BEFORE provider invocation. Never stale.
            try:
                os.unlink(key + '.json', dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            _event(directory_fd, evidence, 'miss', key)
            _event(directory_fd, evidence, 'provider_read', key)
            value = validate_value(provider(reference))
            # TTL starts before the read: slow providers cannot extend freshness.
            end_wall, end_mono = clock()
            if not (0 <= end_wall-wall < ttl and 0 <= end_mono-mono < ttl) or abs((end_wall-wall)-(end_mono-mono)) > 5:
                raise ValueError('clock change or provider exceeded TTL')
            _atomic(directory_fd, key, dict(v=2, ref=reference, created=wall,
                    monotonic=mono, expires=wall+ttl, ttl=ttl,
                    value=base64.b64encode(value).decode('ascii')))
            return value
    finally:
        os.close(directory_fd)


def cleanup(*, purge=False, _directory=None, _clock=None):
    """Remove invalid/expired records under the same locks; never remove lock files.

    Locks persist to prevent inode-split races with waiting readers. Unknown files
    are not touched. Cleanup emits no secret data and never calls a provider.
    """
    directory_fd = _dir(CACHE_DIR if _directory is None else _directory, create=True)
    count = 0
    try:
        clock = _clock or _now
        refs = {identity(r): canonical(r) for r in ALLOWED_REFS}
        names = os.listdir(directory_fd)
        for key, reference in refs.items():
            with _locked(directory_fd, key):
                wall, mono = clock()
                # A writer holds this same lock through publication. Any matching
                # temp surviving acquisition is abandoned, not an active write.
                for name in names:
                    if re.fullmatch(re.escape(key) + r'(?:\.metadata)?\.[0-9a-f]{24}\.tmp', name):
                        try:
                            temp = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
                        except FileNotFoundError:
                            continue
                        try:
                            _regular(temp)
                            os.unlink(name, dir_fd=directory_fd)
                            count += 1
                        finally:
                            os.close(temp)
                # Validate file safety even for explicit purge; no provider calls.
                cached = _read(directory_fd, key, reference, wall, mono)
                if purge or cached is None:
                    try:
                        os.unlink(key + '.json', dir_fd=directory_fd)
                        count += 1
                    except FileNotFoundError:
                        pass
        os.fsync(directory_fd)
        return count
    finally:
        os.close(directory_fd)


if __name__ == '__main__':
    import sys
    if sys.argv[1:] not in (['--cleanup'], ['--purge']):
        raise SystemExit('only --cleanup or --purge is supported')
    try:
        cleanup(purge=sys.argv[1:] == ['--purge'])
    except Exception:
        raise SystemExit('cache cleanup unavailable')
