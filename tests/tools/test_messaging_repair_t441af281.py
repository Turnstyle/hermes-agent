"""t_441af281: sender-close delivery survival and interactive managed surfaces."""
import json
import os
import shlex
import sqlite3
import sys
import subprocess
import threading
import time
from pathlib import Path

import pytest
import run_agent

from tools import bot_mode_dm, bot_mode_probe


class _DB:
    def __init__(self, home):
        self.db_path = str(home / 'state.db')
        self.rows = []
    def get_session_title(self, sid):
        return 'Interactive chat'
    def append_message(self, *args, **kwargs):
        self.rows.append((args, kwargs))


class _Agent:
    def __init__(self, home, platform='cli'):
        self._session_db = _DB(home)
        self.session_id = 'sender-test'
        self.platform = platform
        self._session_source = 'interactive'
        self._bot_mode_protocol = True
        self._process_owner_task_ids = {'sender-test'}
        self.tools = []
        self.valid_tool_names = set()

@pytest.fixture(autouse=True)
def clean_probe(monkeypatch):
    monkeypatch.delenv('HERMES_SESSION_SOURCE', raising=False)
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    bot_mode_probe._reset_cache_for_tests()
    yield
    bot_mode_probe._reset_cache_for_tests()


def _home(tmp_path, managed=True):
    home = tmp_path / 'install' / 'profiles' / 'sender'
    home.mkdir(parents=True)
    if managed:
        (home / 'profile.yaml').write_text('ui_meta:\n  hermes-bots:\n    shape: cloud\n')
    return home


def _until(predicate, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.02)
    assert predicate(), 'timed out waiting for test subprocess'


def _gone(pid):
    try:
        os.kill(pid, 0)
        return False
    except ProcessLookupError:
        return True


@pytest.mark.parametrize('launch_path,close_during_launch', [
    ('pipe', False), ('pipe', True), ('adopt', True), ('pty', True), ('pty-fallback', True),
])
def test_delivery_survives_sender_close_and_unrelated_process_killed(
        tmp_path, monkeypatch, launch_path, close_during_launch):
    import tools.process_registry as pr
    import tools.terminal_tool as terminal

    monkeypatch.setattr(pr, 'get_hermes_home', lambda: tmp_path / 'registry')
    registry = pr.ProcessRegistry()
    monkeypatch.setattr(pr, 'process_registry', registry)
    kill_calls = []
    real_kill = registry.kill_process
    def record_kill(sid, **kwargs):
        kill_calls.append((sid, kwargs.get('source')))
        return real_kill(sid, **kwargs)
    monkeypatch.setattr(registry, 'kill_process', record_kill)
    monkeypatch.setattr('gateway.session_context.async_delivery_supported', lambda: False)
    # Isolate non-process resource disposal; process cleanup is the real lifecycle method.
    monkeypatch.setattr(run_agent, 'cleanup_vm', lambda *a, **k: None)
    monkeypatch.setattr(run_agent, 'cleanup_browser', lambda *a, **k: None)
    real_terminal = terminal.terminal_tool
    sender = run_agent.AIAgent(api_key='test', base_url='http://127.0.0.1:9/v1',
        provider='openai-compat', model='test', enabled_toolsets=[], quiet_mode=True,
        skip_context_files=True, skip_memory=True)
    from hermes_state import SessionDB
    sender._session_db = SessionDB(_home(tmp_path) / 'state.db')
    sender._session_db.create_session('sender-session', 'api_server')
    sender.session_id = 'sender-session'
    sender.platform = 'api_server'
    sender._process_owner_task_ids = {'sender-test'}
    ready, release, child_pid = [tmp_path / n for n in ('ready', 'release', 'child-pid')]
    storage = tmp_path / 'recipient.db'
    with sqlite3.connect(storage) as db:
        db.execute('CREATE TABLE messages (body TEXT)')
    child = tmp_path / 'recipient.py'
    child.write_text('''import os, sqlite3, sys, time
from pathlib import Path
storage, ready, release, pidfile = map(Path, sys.argv[1:])
pidfile.write_text(str(os.getpid()))
with sqlite3.connect(storage) as db:
    db.execute("INSERT INTO messages VALUES (?)", ("unique-delivery",))
ready.touch()
end = time.monotonic() + 30
while not release.exists():
    if time.monotonic() > end: raise RuntimeError("sender never released recipient")
    time.sleep(.02)
print("recipient reply finished", flush=True)
''')
    command = shlex.join([sys.executable, str(child), str(storage), str(ready), str(release), str(child_pid)])
    sessions = []
    pids = []
    registered, launch_release = threading.Event(), threading.Event()
    real_track = registry._track_started
    def track_with_barrier(session, *args, **kwargs):
        real_track(session, *args, **kwargs)
        if session.command == command:
            sessions.append(session.id)
            if close_during_launch:
                registered.set()
                assert launch_release.wait(10), 'launch barrier was not released'
    monkeypatch.setattr(registry, '_track_started', track_with_barrier)
    if launch_path == 'pty-fallback':
        def no_pty(*args, **kwargs):
            raise ImportError('test forces pipe fallback')
        monkeypatch.setattr(registry, '_spawn_local_pty', no_pty)
    def launch(command_arg, **kwargs):
        if command_arg == command and launch_path == 'adopt':
            child_proc = subprocess.Popen(shlex.split(command_arg), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, start_new_session=True)
            session = registry.adopt_local(child_proc, command=command_arg, cwd=str(tmp_path),
                task_id='sender-test', owner_task_id='sender-test', notify_on_complete=False)
            return json.dumps({'session_id': session.id, 'notify_on_complete': False})
        return real_terminal(command_arg, **dict(kwargs, force=True,
            pty=command_arg == command and launch_path in ('pty', 'pty-fallback')))
    monkeypatch.setattr(terminal, 'terminal_tool', launch)
    ack_box = []
    def deliver():
        ack_box.append(json.loads(bot_mode_dm._spawn_delivery(
            command, '@recipient', task_id='sender-test', agent=sender)))
    launch_thread = threading.Thread(target=deliver, name='test-dm-launch')
    def durable_rows():
        with sqlite3.connect(sender._session_db.db_path) as db:
            return db.execute("SELECT content FROM messages WHERE session_id = ? AND display_kind = ?",
                ('sender-session', 'process_complete')).fetchall()
    try:
        # Both request completion notifications. Only the DM launch should be retained.
        unrelated_pid = tmp_path / 'unrelated-child-pid'
        unrelated = json.loads(terminal.terminal_tool(shlex.join([sys.executable, '-c',
            'import os,sys,time; from pathlib import Path; Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)',
            str(unrelated_pid)]),
            background=True, notify_on_complete=True, task_id='sender-test', _host_local=True))
        assert 'session_id' in unrelated, unrelated
        sessions.append(unrelated['session_id'])
        _until(unrelated_pid.exists)
        pids.append(int(unrelated_pid.read_text()))
        launch_thread.start()
        if close_during_launch:
            assert registered.wait(10), 'real registry did not reach launch barrier'
            _until(ready.exists)
            assert not ack_box, 'launch returned before sender close'
            sender.close()
            sender._session_db.close()
            assert not registry.get(sessions[-1]).exited, 'DM killed in registration/return window'
            launch_release.set()
        launch_thread.join(10)
        assert not launch_thread.is_alive()
        assert len(ack_box) == 1
        ack = ack_box[0]
        assert ack['status'] == 'queued', ack
        assert ack['reply_delivery'] == 'transcript', ack
        pids.extend(registry.get(sid).pid for sid in sessions if registry.get(sid).pid not in pids)
        _until(ready.exists)
        if int(child_pid.read_text()) not in pids:
            pids.append(int(child_pid.read_text()))
        print('SPAWNED_TEST_PIDS', json.dumps(pids), flush=True)
        assert not registry.get(ack['process_id']).exited
        if not close_during_launch:
            sender.close()
            sender._session_db.close()
        unrelated_proc = registry.get(unrelated['session_id'])
        assert unrelated_proc._completion_event.wait(10)
        assert unrelated_proc.exited
        assert (unrelated['session_id'], 'agent_close') in kill_calls
        assert (ack['process_id'], 'agent_close') not in kill_calls
        assert not registry.get(ack['process_id']).exited, 'DM killed by sender close during delivery'
        release.touch()
        proc = registry.get(ack['process_id'])
        assert proc._completion_event.wait(10)
        _until(lambda: len(durable_rows()) == 1)
        reply_thread = next((t for t in threading.enumerate()
            if t.name == f"message-agent-reply-{ack['process_id']}"), None)
        if reply_thread:
            reply_thread.join(10)
            assert not reply_thread.is_alive()
        assert proc.message_agent_delivery is True
        if launch_path == 'pty':
            assert proc._pty is not None, 'PTY coverage silently fell back to pipes'
        assert proc.exit_code == 0
        assert proc.completion_reason == 'exited'
        assert 'recipient reply finished' in proc.output_buffer
        with sqlite3.connect(storage) as db:
            assert db.execute('SELECT body FROM messages').fetchall() == [('unique-delivery',)]
        assert len(durable_rows()) == 1
        assert 'recipient reply finished' in durable_rows()[0][0]
        assert registry.is_completion_consumed(ack['process_id']) is False
        # Repeated status reads must not append a second stored reply.
        registry.poll(ack['process_id']); registry.poll(ack['process_id'])
        sender.close()
        sender._session_db.close()
        assert len(durable_rows()) == 1
    finally:
        launch_release.set()
        release.touch()
        if launch_thread.ident is not None:
            launch_thread.join(10)
            assert not launch_thread.is_alive()
        for sid in sessions:
            session = registry.get(sid)
            if session and not session.exited:
                registry.kill_process(sid, source='test_cleanup')
            if session and session.pid not in pids:
                pids.append(session.pid)
            if session and session.process:
                session.process.wait(timeout=10)
        if child_pid.exists() and int(child_pid.read_text()) not in pids:
            pids.append(int(child_pid.read_text()))
        for pid in pids:
            _until(lambda pid=pid: _gone(pid))
        sender._session_db.close()
        print('VERIFIED_TEST_PIDS_GONE', json.dumps(pids), flush=True)


@pytest.mark.parametrize('platform', ['cli', 'tui', 'desktop'])
def test_interactive_surfaces_allowed(tmp_path, monkeypatch, platform):
    sender = _Agent(_home(tmp_path), platform)
    assert not bot_mode_dm.is_canonical_bot_chat(sender)
    assert bot_mode_dm.message_agent_authorized(sender)
    assert bot_mode_dm.ensure_message_agent_tool(sender)
    assert bot_mode_dm.ensure_message_agent_tool(sender)
    assert len(sender.tools) == 1
    assert bot_mode_dm.MESSAGE_AGENT_TOOL_NAME in sender.valid_tool_names
    import tools.terminal_tool as terminal
    calls = []
    def capture(command, **kwargs):
        calls.append((command, kwargs))
        return json.dumps({"session_id": "proc_surface_test", "notify_on_complete": True})
    monkeypatch.setattr(terminal, "terminal_tool", capture)
    monkeypatch.setattr(bot_mode_dm, "_dm_dir", lambda: tmp_path)
    recipient = Path(sender._session_db.db_path).parent.parent / "recipient"
    recipient.mkdir()
    (recipient / "profile.yaml").write_text("ui_meta:\n  hermes-bots:\n    shape: cloud\n")
    result = json.loads(bot_mode_dm.message_agent_tool("recipient", "hello from interactive", agent=sender))
    assert result.get("status") == "queued", result
    assert len(calls) == 1


@pytest.mark.parametrize('platform', ['cron', 'subagent', 'batch', '', 'slack'])
def test_noninteractive_excluded_without_opt_in(tmp_path, platform):
    assert not bot_mode_dm.message_agent_authorized(_Agent(_home(tmp_path), platform))


@pytest.mark.parametrize('platform', ['cli', 'tui', 'desktop'])
def test_protocol_off_and_unmanaged_excluded(tmp_path, platform):
    sender = _Agent(_home(tmp_path), platform)
    sender._bot_mode_protocol = False
    assert not bot_mode_dm.message_agent_authorized(sender)
    sender = _Agent(_home(tmp_path / 'plain', managed=False), platform)
    assert not bot_mode_dm.message_agent_authorized(sender)


@pytest.mark.parametrize('source', ['cron', 'subagent', 'batch'])
@pytest.mark.parametrize('title', ['ordinary interactive session', 'Bot Chat'])
def test_runner_source_cannot_use_interactive_platform(tmp_path, source, title):
    sender = _Agent(_home(tmp_path), 'cli')
    sender._session_source = source
    sender._session_title_hint = title
    assert not bot_mode_dm.message_agent_authorized(sender)
    assert not bot_mode_dm.ensure_message_agent_tool(sender)
    result = json.loads(bot_mode_dm.message_agent_tool('recipient', 'must not send', agent=sender))
    assert 'error' in result


def test_delivery_scope_is_internal_command_bound_and_reset(tmp_path, monkeypatch):
    import tools.process_registry as pr
    from tools.terminal_tool import TERMINAL_SCHEMA
    assert not any('delivery' in name for name in TERMINAL_SCHEMA['parameters']['properties'])
    monkeypatch.setattr(pr, 'get_hermes_home', lambda: tmp_path)
    make = lambda command, **extra: pr.ProcessRegistry._new_session(
        command, 'task', 'owner', '', str(tmp_path), **extra)
    assert not make('delivery').message_agent_delivery
    with pytest.raises(RuntimeError):
        with pr._message_agent_delivery_scope('delivery'):
            assert make('delivery').message_agent_delivery
            assert not make('unrelated').message_agent_delivery
            assert not make('delivery', pid_scope='sandbox').message_agent_delivery
            other_thread = []
            t = threading.Thread(target=lambda: other_thread.append(make('delivery').message_agent_delivery))
            t.start(); t.join(5)
            assert not t.is_alive()
            assert other_thread == [False]
            with pr._message_agent_delivery_scope('nested'):
                assert make('nested').message_agent_delivery
                assert not make('delivery').message_agent_delivery
            assert make('delivery').message_agent_delivery
            raise RuntimeError('launch failed')
    assert not make('delivery').message_agent_delivery
