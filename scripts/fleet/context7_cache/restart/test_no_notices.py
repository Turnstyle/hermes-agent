"""AST-only exact-live overlay tests. No adapter import or real IO/process/network."""
import __future__
import argparse
import ast
import contextlib
import json
import shlex
import types
import unittest
from unittest.mock import Mock
import build_overlay


class Fail(Exception):
    pass


class Deferred(Fail):
    pass


def load(*names):
    raw = build_overlay.ADAPTER.read_bytes()
    assert build_overlay.digest(raw) == 'df56ebff6a48c22b1a698d491da4195e2fe54d2d19f8df0133b31654eac3d3a8'
    tree = ast.parse(build_overlay.overlay(raw.decode()))
    ns = {'__doc__': 'synthetic adapter', 'argparse': argparse, 'json': json,
          'shlex': shlex, 'Fail': Fail, 'SwitchDeferred': Deferred,
          'SwitchDeferredUnreadable': type('Unreadable', (Deferred,), {}),
          'SwitchDeferredUnknown': type('Unknown', (Deferred,), {}),
          'DEFAULT_DEADLINE_MINUTES': 10, 'MIN_GAP_MINUTES': 30,
          'POLL_SECONDS': 1, 'IDLE_CONFIRM_POLLS': 1, 'MARKER_REFRESH_SECONDS': 60,
          'NOTICE_FILE': 'synthetic-notice', 'OUTCOME_FILE': 'synthetic-outcome',
          'STATE_FILE': 'synthetic-state', 'STATE_LOCK': 'synthetic-lock',
          'DRAIN_MARKER': 'synthetic-drain', 'HERMES_HOME': 'synthetic-home',
          'now': Mock(return_value=100), 'iso': Mock(return_value='synthetic-time'),
          'time': types.SimpleNamespace(sleep=Mock()),
          'subprocess': types.SimpleNamespace(run=Mock(side_effect=AssertionError('unexpected subprocess'))),
          'atomic_write': Mock(), 'update_request': Mock(),
          'kanban_comment': Mock(side_effect=AssertionError('board mutation')),
          'steer': Mock(side_effect=AssertionError('steer mutation')),
          'api_key': Mock(side_effect=AssertionError('secret load')),
          'api_base': Mock(return_value='synthetic-api'),
          'notice_text': Mock(return_value='synthetic-notice-text'),
          'back_online_text': Mock(return_value='synthetic-back-text')}
    selected = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in selected} == set(names)
    ns['SCRIPT'] = 'synthetic-script'
    ns['mins'] = Mock(return_value='synthetic-minutes')
    ns['cleanup_allowed'] = Mock(return_value=True)
    exec(compile(ast.Module(body=selected, type_ignores=[]), '<selected exact-live functions>', 'exec', flags=__future__.annotations.compiler_flag), ns)
    return ns


def work():
    return {'by_profile': {'bot': [{'kind': 'kanban worker', 'task_id': 'card'},
                                  {'kind': 'api', 'run_id': 'run'}]}, 'notes': [], 'unnamed': 0}


class NoNoticesTests(unittest.TestCase):
    def test_parser_request_and_run_default_and_opt_in(self):
        ns = load('build_parser')
        parser = ns['build_parser']()
        for command in ('request', 'run'):
            args = [command, '--reason', 'synthetic']
            self.assertFalse(parser.parse_args(args).no_notices)
            self.assertTrue(parser.parse_args(args + ['--no-notices']).no_notices)

    def test_request_persistence_and_join_policy(self):
        ns = load('new_request_record', 'refuse_if_underway')
        ns.update(time=types.SimpleNamespace(strftime=Mock(return_value='r-'), localtime=Mock()),
                  uuid=types.SimpleNamespace(uuid4=Mock(return_value=types.SimpleNamespace(hex='1234'))),
                  socket=types.SimpleNamespace(gethostname=Mock(return_value='synthetic-host')))
        args = argparse.Namespace(reason='synthetic', force=False, deadline_minutes=10, no_notices=True)
        req = ns['new_request_record'](args, 'synthetic', 0)
        self.assertTrue(req['no_notices'])
        ns['refuse_if_underway'](req, args)
        args.no_notices = False
        with self.assertRaisesRegex(Fail, 'different --no-notices'):
            ns['refuse_if_underway'](req, args)
        self.assertEqual(req['joiners'], [])

    def test_notice_and_wake_helpers_do_not_reach_mutators(self):
        ns = load('send_notices', 'wake_bots')
        req = {'id': 'r', 'no_notices': True}
        ns['send_notices'](work(), req, {}, 'fake-key', Mock(), False)
        self.assertEqual(ns['wake_bots'](['bot'], req, 'synthetic-route', Mock(), False), {})
        for name in ('atomic_write', 'kanban_comment', 'steer'):
            ns[name].assert_not_called()
        ns['subprocess'].run.assert_not_called()

    def test_checkpoint_keeps_census_and_policy_without_comments(self):
        ns = load('deadline_checkpoint', 'confirm_idle_or_deadline')
        ns['who_is_working'] = Mock(return_value=work())
        ns['collect_snapshot'] = Mock(return_value={'synthetic': True})
        ns['confirm_idle_for_switch'] = Mock(side_effect=Deferred('busy'))
        req = {'id': 'r', 'deadline_at': 99, 'no_notices': True}
        ns['confirm_idle_or_deadline'](req, Mock())
        self.assertTrue(req['deadline_checkpointed'])
        ns['collect_snapshot'].assert_called_once()
        ns['who_is_working'].assert_called_once()
        receipt = ns['update_request'].call_args.kwargs
        self.assertEqual(receipt['deadline_checkpoint_cards'], ['card'])
        self.assertEqual(receipt['deadline_checkpoint_posted_cards'], [])
        self.assertTrue(receipt['deadline_checkpoint_notices_suppressed'])
        ns['atomic_write'].assert_not_called()
        ns['subprocess'].run.assert_not_called()
        req['deadline_at'] = 101
        with self.assertRaises(Deferred):
            ns['confirm_idle_or_deadline'](req, Mock())

    def test_checkpoint_default_positive_control_uses_only_fake_process(self):
        ns = load('deadline_checkpoint')
        ns.update(collect_snapshot=Mock(return_value={}), who_is_working=Mock(return_value=work()),
                  hermes_bin=Mock(return_value='/synthetic/hermes'))
        ns['subprocess'].run = Mock(return_value=types.SimpleNamespace(returncode=0))
        req = {'id': 'r', 'deadline_at': 99}
        ns['deadline_checkpoint'](req, Mock())
        self.assertEqual(ns['subprocess'].run.call_args.args[0][1:4], ['kanban', 'comment', 'card'])
        self.assertEqual(ns['update_request'].call_args.kwargs['deadline_checkpoint_posted_cards'], ['card'])
        ns['atomic_write'].assert_called_once()
        ns['deadline_checkpoint'](req, Mock())
        ns['subprocess'].run.assert_called_once()

    def test_current_failclosed_census_is_preserved(self):
        ns = load('confirm_idle_for_switch', 'gateway_activity_known')
        ns.update(collect_snapshot=Mock(return_value={'kanban_running': None}),
                  restart_wait_guidance=Mock(return_value='wait'))
        with self.assertRaises(ns['SwitchDeferredUnreadable']):
            ns['confirm_idle_for_switch'](Mock())
        ns['collect_snapshot'].return_value = {'kanban_running': [], 'gateway_state': None}
        ns.update(who_is_working=Mock(return_value={}), is_idle=Mock(return_value=True),
                  gateway_stopped_evidence=Mock(return_value=(False, 'unknown')))
        with self.assertRaises(ns['SwitchDeferredUnknown']):
            ns['confirm_idle_for_switch'](Mock())

    def test_normal_coordinator_keeps_drain_locks_restart_health_and_outcome(self):
        ns = load('_coordinate', 'send_notices', 'wake_bots', 'deadline_checkpoint')
        req = {'id': 'r', 'force': False, 'deadline_minutes': 10, 'no_notices': True,
               'notify_cmd': 'synthetic-notify', 'wake_cmd': 'synthetic-wake'}
        state = {'request': req}
        gs = {'pid': 1, 'served_profiles': ['bot'], 'platforms': {}}
        ns.update(read_gateway_state=Mock(return_value=gs), read_marker=Mock(return_value=None),
                  publish_initial_deadline=Mock(return_value=(10, 99)),
                  write_marker=Mock(), clear_own_marker=Mock(return_value=True),
                  collect_snapshot=Mock(return_value={'gateway_state': gs}),
                  who_is_working=Mock(return_value=work()), describe_work=Mock(return_value=['busy']),
                  is_idle=Mock(return_value=False), notified_profiles=Mock(return_value=['bot']),
                  restart_wait_guidance=Mock(return_value='wait'), check_cancel=Mock(),
                  earlier_stored_deadline=Mock(return_value=99),
                  confirm_idle_or_deadline=Mock(),
                  locked=Mock(side_effect=lambda *_: contextlib.nullcontext()),
                  load_state=Mock(return_value=state), save_state=Mock(),
                  gateway_pid=Mock(return_value=1), cut_sets=Mock(return_value=({'bot': {}}, [('bot', 'card')])),
                  run_restart=Mock(), verify=Mock(return_value=(True, [])),
                  resume_report=Mock(return_value=['synthetic resume']),
                  wake_profiles=Mock(return_value=['bot']))
        pause = Mock()
        ns['_coordinate'](req, argparse.Namespace(), Mock(), False, pause)
        pause.acquire.assert_called_once()
        self.assertGreaterEqual(pause.barrier.call_count, 2)
        self.assertGreaterEqual(pause.check.call_count, 2)
        ns['write_marker'].assert_called_once()
        ns['clear_own_marker'].assert_called_once()
        self.assertGreaterEqual(ns['collect_snapshot'].call_count, 4)
        self.assertTrue(req['deadline_checkpointed'])
        self.assertGreaterEqual(ns['confirm_idle_or_deadline'].call_count, 2)
        ns['run_restart'].assert_called_once()
        ns['verify'].assert_called_once_with({'pid': 1, 'served': ['bot'], 'platforms': []}, unittest.mock.ANY)
        pause.release.assert_called_once()
        ns['kanban_comment'].assert_not_called()
        ns['steer'].assert_not_called()
        ns['subprocess'].run.assert_not_called()
        ns['api_key'].assert_not_called()
        self.assertEqual([c.args[0] for c in ns['atomic_write'].call_args_list], ['synthetic-outcome'])

    def test_retained_flow_keeps_callback_manifest_health_release(self):
        ns = load('coordinate_retained')
        pause = Mock()
        ns['DispatchPause'] = Mock(return_value=pause)
        for name in ('publish_retained_markers', 'refresh_retained', 'confirm_idle_or_deadline',
                     'acquire_auxiliary', 'check_manifest', 'run_retained_callback',
                     'acquire_auxiliary_best_effort', 'run_restart', 'cmd_release'):
            ns[name] = Mock()
        ns.update(publish_initial_deadline=Mock(return_value=(10, 200)),
                  retained_record=Mock(return_value={'id': 'r'}),
                  read_gateway_state=Mock(return_value={}), gateway_pid=Mock(return_value=1),
                  verify=Mock(return_value=(True, [])), contextlib=contextlib)
        req = {'id': 'r', 'deadline_minutes': 10, 'no_notices': True}
        ns['coordinate_retained'](req, Mock())
        for name in ('publish_retained_markers', 'refresh_retained', 'run_retained_callback',
                     'run_restart', 'verify', 'cmd_release'):
            ns[name].assert_called_once()
        self.assertEqual(ns['confirm_idle_or_deadline'].call_count, 2)
        self.assertGreaterEqual(ns['check_manifest'].call_count, 3)
        ns['subprocess'].run.assert_not_called()

    def test_health_still_requires_new_pid_and_answering_health(self):
        from datetime import datetime, timezone
        ns = load('verify')
        ns.update(VERIFY_TIMEOUT_SECONDS=0, HEARTBEAT_FRESH_SECONDS=30,
                  datetime=datetime, pid_alive=Mock(return_value=True),
                  gateway_pid=Mock(side_effect=lambda gs: gs['pid']),
                  read_gateway_state=Mock(return_value={
                      'pid': 1, 'gateway_state': 'running', 'platforms': {},
                      'updated_at': datetime.fromtimestamp(100, timezone.utc).isoformat()}),
                  http=Mock(), urllib=__import__('urllib.error', fromlist=['error']))
        # verify catches urllib.error.URLError; bind the actual namespace but never call network.
        import urllib.error
        ns['urllib'] = urllib
        ok, problems = ns['verify']({'pid': 1}, Mock())
        self.assertFalse(ok)
        self.assertTrue(any('not new/alive' in p for p in problems))
        ns['http'].assert_called_once_with('synthetic-api/health')
        ns['read_gateway_state'].return_value['pid'] = 2
        self.assertTrue(ns['verify']({'pid': 1}, Mock())[0])
        ns['http'].side_effect = OSError('synthetic health failure')
        self.assertFalse(ns['verify']({'pid': 1}, Mock())[0])

    def test_coordinate_run_option_reaches_retained_and_cannot_unsuppress(self):
        ns = load('coordinate')
        ns['coordinate_retained'] = Mock()
        for stored, requested in ((False, True), (True, False)):
            req = {'id': 'r', 'retain_pause': True, 'no_notices': stored}
            ns['coordinate'](req, argparse.Namespace(no_notices=requested), Mock(), False)
            self.assertTrue(req['no_notices'])
        self.assertEqual(ns['coordinate_retained'].call_count, 2)

    def test_default_notice_path_not_silently_removed(self):
        ns = load('send_notices')
        ns['kanban_comment'] = Mock(return_value='commented')
        ns['steer'] = Mock(return_value='steered')
        w = work()
        w['by_profile']['bot'][0]['detail'] = 'worker'
        w['by_profile']['bot'][1]['session'] = 'session'
        ns['send_notices'](w, {'id': 'r', 'deadline_at': 1}, {}, 'fake-key', Mock(), False)
        ns['kanban_comment'].assert_called_once()
        ns['steer'].assert_called_once()
        ns['atomic_write'].assert_called_once()
