"""Atomic recovery of one adopted local receipt pair, with a retained rollback preimage."""

import json
from pathlib import Path

from persistence.authority.bindings import ConfigSecretProvenanceRecovery
from persistence.core.errors import AuthorityRefusal

from ..conversation._store.local import POLICY_PREFIX
from ..conversation._store.local_lineage import validate_local_lineage
from ..conversation._store.local_migration import LEGACY_PREFIX
from ._runtime import _epoch, _json, _session
from ._runtime_guards import require_idle

PREIMAGE_PREFIX = 'gateway.config_secret_provenance.v1:'
_CONFIG_PREFIX = 'profile-config-v1:'


def _object(text):
    try:
        value = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise AuthorityRefusal('storage_unavailable') from exc
    if not isinstance(value, dict):
        raise AuthorityRefusal('storage_unavailable')
    return value


def _homes(old_home, home):
    old, current = Path(old_home), Path(home)
    # Lexical sibling paths only: never open or resolve the old profile or follow its symlinks.
    if (not old.is_absolute() or str(old) != old_home or '..' in old.parts
            or old.parent != current.parent or current.parent.name != 'profiles'
            or old == current):
        raise AuthorityRefusal('invalid_params', field='old_home', detail='expected an old sibling profile home')
    return str(current)


def _pair(conn, sid, route, origin, home, old_home):
    keys = (POLICY_PREFIX + sid, LEGACY_PREFIX + sid)
    raw = []
    for key in keys:
        row = conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()
        if row is None:
            raise AuthorityRefusal('storage_unavailable')
        raw.append(row[0])
    receipt, legacy = map(_object, raw)
    fields = ('session_id', 'profile_id', 'principal_id', 'legacy_session_id')
    if (any(not isinstance(receipt.get(k), str) or not receipt[k] for k in fields)
            or legacy != {k: receipt[k] for k in fields}
            or receipt['session_id'] != sid or receipt['legacy_session_id'] != sid
            or receipt['profile_id'] not in (old_home, home)
            or receipt.get('route') != route):
        raise AuthorityRefusal('storage_unavailable')
    target = validate_local_lineage(conn, receipt)
    entry = receipt['entry']
    stored_origin = dict(entry['origin'])
    if 'profile' in origin:
        stored_origin.setdefault('profile', origin['profile'])
    if (entry.get('session_key') != route or stored_origin != dict(origin)
            or origin.get('user_id') != receipt['principal_id'] or origin.get('chat_id') != sid):
        raise AuthorityRefusal('storage_unavailable')
    for segment in receipt.get('lineage', [sid]):
        row = _session(conn, segment)
        if (row['session_key'] != route or row['chat_id'] != sid
                or row['user_id'] != receipt['principal_id']):
            raise AuthorityRefusal('storage_unavailable')
    routed = conn.execute("SELECT entry_json FROM gateway_routing WHERE scope='' AND session_key=?", (route,)).fetchone()
    if routed is None or _object(routed[0]).get('session_id') != target:
        raise AuthorityRefusal('storage_unavailable')
    return keys, raw, receipt, legacy, target


def _candidate(receipt, legacy, sid, old_home, home):
    policy = receipt.get('policy')
    ref = policy.get('config_secret_ref') if isinstance(policy, dict) else None
    if not isinstance(ref, str) or not ref.startswith(_CONFIG_PREFIX):
        raise AuthorityRefusal('launch_credentials_unavailable', field='config_secret_ref')
    source = _object(ref[len(_CONFIG_PREFIX):])
    identity = (source.get('home'), source.get('profile'))
    if source.get('session') != sid or identity not in ((old_home, old_home), (home, home)):
        raise AuthorityRefusal('launch_credentials_unavailable', field='config_secret_ref',
                               detail='reference does not name this session and paired profile home')
    if identity == (home, home) and receipt['profile_id'] != home:
        raise AuthorityRefusal('storage_unavailable')
    if identity == (home, home):
        return [receipt, legacy], False
    source.update(home=home, profile=home)
    candidate = dict(receipt, profile_id=home, policy=dict(policy, config_secret_ref=_CONFIG_PREFIX + _json(source)))
    return [candidate, dict(legacy, profile_id=home)], True


def _restore(conn, record, raw, keys, generation, preimage_key):
    expected = record['before'] if record['rolled_back'] else record['after']
    if raw != expected or generation != record['generation']:
        raise AuthorityRefusal('revision_conflict')
    if record['rolled_back']:
        return 'unchanged', generation
    for key, value in zip(keys, record['before']):
        conn.execute('UPDATE state_meta SET value=? WHERE key=?', (value, key))
    generation += 1
    record.update(rolled_back=True, generation=generation)
    conn.execute('UPDATE state_meta SET value=? WHERE key=?', (_json(record), preimage_key))
    return 'rolled_back', generation


def recover(holder, *, epoch, session_id, old_home, home, route, origin, action, validate_policy):
    home = _homes(old_home, home)
    if action not in ('preview', 'apply', 'rollback') or not isinstance(session_id, str) or not session_id:
        raise AuthorityRefusal('invalid_params')
    preimage_key = PREIMAGE_PREFIX + session_id

    def write(conn):
        _epoch(conn, epoch)
        keys, raw, receipt, legacy, target = _pair(conn, session_id, route, origin, home, old_home)
        require_idle(holder, conn, list({session_id, target}))
        generation = _session(conn, session_id)['runtime_generation']
        saved = conn.execute('SELECT value FROM state_meta WHERE key=?', (preimage_key,)).fetchone()
        record = _object(saved[0]) if saved else None
        if record is not None and record['old_home'] != old_home:
            raise AuthorityRefusal('admission_conflict')
        if action == 'rollback':
            if record is None:
                raise AuthorityRefusal('not_found')
            candidate, changed = _candidate(*map(_object, record['before']), session_id, old_home, home)
            if not changed or candidate != list(map(_object, record['after'])):
                raise AuthorityRefusal('storage_unavailable')
            if record['target_revision'] != _session(conn, target)['runtime_revision']:
                raise AuthorityRefusal('revision_conflict')
            status, new_generation = _restore(conn, record, raw, keys, generation, preimage_key)
        else:
            pair, changed = _candidate(receipt, legacy, session_id, old_home, home)
            validate_policy(pair[0]['policy'])
            if not changed:
                return ConfigSecretProvenanceRecovery('unchanged', preimage_key if record else None, generation)
            if record is not None:
                raise AuthorityRefusal('admission_conflict')
            if action == 'preview':
                return ConfigSecretProvenanceRecovery('preview', None, generation)
            after = [_json(item) for item in pair]
            new_generation = generation + 1
            record = {'old_home': old_home, 'before': raw, 'after': after,
                      'generation': new_generation, 'rolled_back': False,
                      'target_revision': _session(conn, target)['runtime_revision']}
            # The preimage and both receipts share the holder's one transaction.
            conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)', (preimage_key, _json(record)))
            for key, value in zip(keys, after):
                conn.execute('UPDATE state_meta SET value=? WHERE key=?', (value, key))
            status = 'applied'
        conn.execute('UPDATE sessions SET runtime_generation=? WHERE id=?', (new_generation, session_id))
        return ConfigSecretProvenanceRecovery(status, preimage_key, new_generation)

    try:
        return holder._execute_write(write)
    except AuthorityRefusal:
        raise
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise AuthorityRefusal('storage_unavailable') from exc
