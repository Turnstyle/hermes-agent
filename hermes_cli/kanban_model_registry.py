"""Read Mission Control metadata through its existing exact-get CLI.

Registry descriptions are evidence for advice, never credential or task-quality
proof. Each selection has a two-second lookup budget and makes no retries.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
import os
import signal
from pathlib import Path
import subprocess
import sys
import time


DEFAULT_FLEET_OPS = Path.home() / '.hermes' / 'fleet-node-state' / 'fleet_ops.py'
LOOKUP_TIMEOUT_SECONDS = 2


def _score(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and 0 <= value <= 1)


def read_model_record(document_id, policy, *, now, timeout=LOOKUP_TIMEOUT_SECONDS):
    """Return a metadata-only snapshot record and a fixed, non-secret reason."""
    path = Path(os.environ.get('HERMES_FLEET_OPS') or DEFAULT_FLEET_OPS)
    if not path.is_file():
        return None, 'registry_reader_missing'
    try:
        with subprocess.Popen(
            [sys.executable, str(path), 'model-routes', 'get', '--model', document_id, '--json'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=os.name == 'posix',
        ) as process:
            try:
                stdout, _ = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                # The fleet reader spawns gcloud. Reap only the group created by
                # this lookup so an inherited pipe cannot extend the deadline.
                if os.name == 'posix':
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass  # The group exited between the timeout and signal.
                else:
                    process.kill()
                process.communicate()
                return None, 'registry_lookup_timeout'
            returncode = process.returncode
    except OSError:
        return None, 'registry_reader_unavailable'
    except UnicodeError:
        return None, 'registry_invalid_record'
    if returncode:
        return None, 'registry_lookup_failed'
    try:
        record = json.loads(stdout)
        model_id = record['model_id']
        provider, model = model_id.split('/', 1)
        if not provider or not model or document_id != model_id.replace('/', '_'):
            raise ValueError('model identity mismatch')
        caps = []
        for field in ('good_at', 'best_fleet_uses'):
            values = record.get(field, [])
            if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
                raise ValueError('invalid capabilities')
            caps.extend(v for v in values if v not in caps)
        quality = {}
        verdicts = record.get('job_type_verdicts', {})
        if not isinstance(verdicts, dict):
            raise ValueError('invalid verdicts')
        for task_class, score in verdicts.items():
            if _score(score):
                quality[task_class] = score
        # An unscaled index (e.g. 34 points) has no [0,1] task-quality meaning.
        for evaluation in record.get('public_evals', []):
            if (isinstance(evaluation, dict) and evaluation.get('scale') == [0, 1]
                    and isinstance(evaluation.get('task_class'), str)
                    and _score(evaluation.get('score'))):
                key = evaluation['task_class']
                quality[key] = min(quality.get(key, 1), evaluation['score'])
    except (ValueError, TypeError, KeyError, AttributeError):
        return None, 'registry_invalid_record'
    meta = {'provider': provider, 'model': model,
            'capabilities': caps}
    if quality:
        meta['quality'] = quality
    return meta, 'registry_lookup_ok'


def resolve_registry_policy(policy, *, now):
    """Resolve without mutating the owner policy or persisting remote raw data."""
    if 'registry' not in policy:
        return policy, None
    resolved = deepcopy(policy)
    records = []
    reasons = []
    deadline = time.monotonic() + LOOKUP_TIMEOUT_SECONDS
    for document_id in policy['registry']['model_ids']:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            reasons.append('registry_lookup_timeout')
            break
        record, reason = read_model_record(document_id, policy, now=now, timeout=remaining)
        reasons.append(reason)
        if record is not None:
            records.append(record)
    # Only owner records establish route identity and proof. Metadata can
    # enrich a matching owner record, never introduce a new eligible route.
    for owner in resolved['snapshot']['model_capabilities']:
        if (owner.get('node'), owner.get('profile')) != (policy['node'], policy['profile']):
            continue
        for record in records:
            if (owner.get('provider'), owner.get('model')) != (record['provider'], record['model']):
                continue
            caps = owner.get('capabilities', [])
            if isinstance(caps, list):
                owner['capabilities'] = caps + [c for c in record['capabilities'] if c not in caps]
            quality = owner.get('quality', {})
            if isinstance(quality, dict):
                for task_class, score in record.get('quality', {}).items():
                    if task_class not in quality:
                        quality[task_class] = score
                    elif _score(quality[task_class]):
                        # Conflicting evidence cannot upgrade a known low score.
                        quality[task_class] = min(quality[task_class], score)
                if quality:
                    owner['quality'] = quality
    reason = next((r for r in reasons if r != 'registry_lookup_ok'), 'registry_lookup_ok')
    return resolved, reason
