"""Stage 3 focused-run receipt plugin. No system-wide process enumeration."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

import psutil

ROOT = Path(__file__).resolve().parents[1]
RECEIPT = None

def pytest_addoption(parser):
    parser.addoption('--retention-receipt', type=Path, default=None,
                     help='Explicit writable JSONL destination for known-process receipts')

def _record(event, **fields):
    if RECEIPT is None:
        return
    process = psutil.Process(os.getpid())
    record = {'event': event, 'pid': process.pid, 'start': process.create_time(),
              'time': datetime.now(timezone.utc).isoformat(), **fields}
    with RECEIPT.open('a') as output:
        output.write(json.dumps(record) + '\n')

def pytest_sessionstart(session):
    global RECEIPT
    RECEIPT = session.config.getoption('--retention-receipt')
    _record('start', args=[str(a) for a in session.config.args])

def pytest_sessionfinish(session, exitstatus):
    escaped = {}
    local_prefixes = ('hermes_cli', 'tools', 'agent', 'cron', 'gateway', 'pm', 'hermes_platform')
    for name, mod in list(sys.modules.items()):
        location = getattr(mod, '__file__', None)
        if not location:
            continue
        path = Path(location).resolve()
        if ('/candidate/' in str(path) or
                (name.split('.')[0] in local_prefixes and not path.is_relative_to(ROOT))):
            escaped[name] = str(path)
    _record('finish', exit_code=int(exitstatus), escaped_modules=escaped)

# Record children at their creation boundary. Listing every host PID is both
# unnecessary and denied in the desktop sandbox. Never inspect environments.
import subprocess
_original_init = subprocess.Popen.__init__
_original_wait = subprocess.Popen.wait

def _child_init(self, *args, **kwargs):
    _original_init(self, *args, **kwargs)
    try:
        self._stage3_start = psutil.Process(self.pid).create_time()
    except psutil.NoSuchProcess:
        self._stage3_start = None
    _record('child_start', child_pid=self.pid, child_start=self._stage3_start)

def _child_wait(self, *args, **kwargs):
    code = _original_wait(self, *args, **kwargs)
    _record('child_exit', child_pid=self.pid, child_start=getattr(self, '_stage3_start', None), child_exit=code)
    return code

subprocess.Popen.__init__ = _child_init
subprocess.Popen.wait = _child_wait
