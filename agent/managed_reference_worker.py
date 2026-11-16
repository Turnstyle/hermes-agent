"""Fixed reference availability probe. No config, provider, model or credential RPC.

Only approved NAMEs and an owner challenge cross the pipe. Values are never emitted.
The owner launches this interpreter at the maker's finalized Popen seam with the
same environment, before that mapping is released.
"""
import json
import os
import re
import sys

PROVIDERS = ('fleet-relay-claude', 'fleet-relay-openai')
LIMIT = 4096


def read(stream):
    line = stream.readline(LIMIT + 1)
    if len(line) > LIMIT or not line.endswith(b'\n'):
        raise ValueError('invalid_reference_frame')
    frame = json.loads(line)
    if type(frame) is not dict:
        raise ValueError('invalid_reference_frame')
    return frame


def send(stream, frame):
    data = json.dumps(frame, separators=(',', ':'), allow_nan=False).encode() + b'\n'
    if len(data) > LIMIT:
        raise ValueError('invalid_reference_frame')
    stream.write(data)
    stream.flush()


def execute(frame, environment):
    if (set(frame) != {'type', 'challenge', 'names'} or frame['type'] != 'reference.run'
            or type(frame['challenge']) is not str or not re.fullmatch('[0-9a-f]{64}', frame['challenge'])
            or type(frame['names']) is not dict or set(frame['names']) != set(PROVIDERS)
            or any(type(n) is not str or not re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', n)
                   for n in frame['names'].values())):
        raise ValueError('invalid_reference_frame')
    # Presence is scoped reference availability, never an authentication result.
    available = {p: bool(environment.get(frame['names'][p])) for p in PROVIDERS}
    return {'type': 'reference.complete', 'challenge': frame['challenge'], 'available': available}


def main():
    from agent.managed_worker import hello
    send(sys.stdout.buffer, {'type': 'hello', **hello()})
    send(sys.stdout.buffer, execute(read(sys.stdin.buffer), os.environ))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
