"""Execute only the extracted adapter function with fully mocked subprocess/environment."""
import ast
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import types
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parent

class Fail(Exception):
    pass

def load_adapter(patched=True, platform='darwin', forced=False, result=None):
    manifest = json.loads((ROOT / 'manifest.json').read_text())
    fixture = (ROOT / 'run_restart.before.txt').read_text()
    assert hashlib.sha256(fixture.encode()).hexdigest() == manifest['fixture_sha256']
    if patched:
        fixture = fixture.replace(manifest['old_line'], manifest['new_line'], 1)
    node = ast.parse(fixture).body[0]
    assert isinstance(node, ast.FunctionDef) and node.name == 'run_restart'
    runner = Mock(return_value=result or types.SimpleNamespace(returncode=0, stdout='healthy\n', stderr=''))
    env = {'SYNTHETIC_CONTEXT': 'preserved', 'HERMES_DELEGATED_CHILD_CONTEXT': 'synthetic-child'}  # Never access the real environment.
    namespace = {'hermes_bin': lambda: '/synthetic/hermes', 'shlex': shlex,
                 'subprocess': types.SimpleNamespace(run=runner, TimeoutExpired=subprocess.TimeoutExpired),
                 'os': types.SimpleNamespace(environ=env, getuid=lambda: 999),
                 'sys': types.SimpleNamespace(platform=platform), 'RESTART_TIMEOUT_SECONDS': 120,
                 'SCRIPT': '/synthetic/coordinator.py', 'Fail': Fail,
                 'load_state': Mock(return_value={'request': {'force': forced}})}
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<extracted run_restart>', 'exec'), namespace)
    return namespace['run_restart'], runner, namespace

class AdapterTests(unittest.TestCase):
    def test_yes_default_scope_and_context(self):
        fn, runner, ns = load_adapter()
        fn(Mock())
        self.assertEqual(runner.call_count, 1)
        argv = runner.call_args.args[0]
        self.assertEqual(argv, ['/synthetic/hermes', '-p', 'default', 'gateway', 'restart', '--yes'])
        self.assertNotIn('--force', argv)
        self.assertNotIn('--all', argv)
        self.assertEqual(runner.call_args.kwargs, {'capture_output': True, 'text': True, 'timeout': 120, 'env': {'SYNTHETIC_CONTEXT': 'preserved', 'HERMES_DELEGATED_CHILD_CONTEXT': 'synthetic-child', 'GATEWAY_COORDINATED_RESTART': '1', 'HERMES_RESTART_AFTER_TURN_TIMEOUT': '1', 'HERMES_RESTART_DRAIN_TIMEOUT': '1'}})
        self.assertEqual(ns['os'].environ, {'SYNTHETIC_CONTEXT': 'preserved', 'HERMES_DELEGATED_CHILD_CONTEXT': 'synthetic-child'})

    def test_regression_old_bare_command_refused_patched_accepted(self):
        def simulate(argv, **kwargs):
            return types.SimpleNamespace(returncode=0 if '--yes' in argv else 1, stdout='', stderr='' if '--yes' in argv else 'Refusing: live sessions')
        for patched in [False, True]:
            with self.subTest(patched=patched):
                fn, runner, _ = load_adapter(patched)
                runner.side_effect = simulate
                if patched:
                    fn(Mock())
                else:
                    with self.assertRaises(Fail):
                        fn(Mock())
                self.assertEqual(runner.call_count, 1)

    def test_timeout_no_retry_or_fallback(self):
        fn, runner, _ = load_adapter()
        runner.side_effect = subprocess.TimeoutExpired('synthetic', 120)
        with self.assertRaisesRegex(Fail, 'longer than 120s'):
            fn(Mock())
        self.assertEqual(runner.call_count, 1)

    def test_failure_no_bare_retry_or_force(self):
        for forced in [False, True]:
            fn, runner, _ = load_adapter(forced=forced, result=types.SimpleNamespace(returncode=1, stdout='', stderr='live work refusal'))
            with self.assertRaises(Fail):
                fn(Mock())
            self.assertEqual(runner.call_count, 1)
            self.assertEqual(runner.call_args.args[0][-1], '--yes')
            self.assertNotIn('--force', runner.call_args.args[0])

    def test_existing_budget_fallback_unchanged_mock_only(self):
        for platform, expected in [('darwin', [['launchctl', 'kickstart', '-k', 'gui/999/ai.hermes.gateway']]), ('linux', [['systemctl', '--user', 'reset-failed', 'hermes-gateway.service'], ['systemctl', '--user', 'restart', 'hermes-gateway.service']])]:
            fn, runner, _ = load_adapter(platform=platform, forced=True)
            runner.side_effect = [types.SimpleNamespace(returncode=1, stdout='', stderr='Restart budget blocked')] + [types.SimpleNamespace(returncode=0, stdout='', stderr='') for _ in expected]
            fn(Mock())
            self.assertEqual([c.args[0] for c in runner.call_args_list][1:], expected)
            self.assertEqual(runner.call_args_list[0].args[0][-1], '--yes')
            self.assertNotIn('--force', runner.call_args_list[0].args[0])

    def test_budget_refusal_without_coordinator_force_no_fallback(self):
        fn, runner, _ = load_adapter(result=types.SimpleNamespace(returncode=1, stdout='Restart budget blocked', stderr=''))
        with self.assertRaises(Fail):
            fn(Mock())
        self.assertEqual(runner.call_count, 1)

    def test_patch_is_one_argv_change(self):
        manifest = json.loads((ROOT / 'manifest.json').read_text())
        patch = (ROOT / 'gateway_coordinated_restart.patch').read_bytes()
        self.assertEqual(hashlib.sha256(patch).hexdigest(), manifest['patch_sha256'])
        lines = patch.decode().splitlines()
        removed = [s[1:] for s in lines if s.startswith('-') and not s.startswith('---')]
        added = [s[1:] for s in lines if s.startswith('+') and not s.startswith('+++')]
        self.assertIn(manifest['old_line'], removed)
        self.assertIn(manifest['new_line'], added)
        fixture = (ROOT / 'run_restart.before.txt').read_text()
        before = ast.parse(fixture)
        after = ast.parse(fixture.replace(manifest['old_line'], manifest['new_line'], 1))
        # Within restart itself only the supported consent argv changes.
        after.body[0].body[0] = before.body[0].body[0]
        self.assertEqual(ast.dump(before), ast.dump(after))

    def test_actual_cli_parser_flag_and_guard_function(self):
        evidence = json.loads((ROOT / 'source-evidence.json').read_text())
        parser = next(x for x in evidence if x['path'].endswith('subcommands/gateway.py'))
        self.assertIn('"--yes", "--interrupt", dest="interrupt"', parser['excerpts'][1]['text'])
        import argparse
        import textwrap
        flag_expr = next(n for n in ast.parse(textwrap.dedent(parser['excerpts'][1]['text'])).body if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Name) and n.value.func.id == '_flag' and isinstance(n.value.args[0], ast.Name) and n.value.args[0].id == 'gateway_restart' and any(isinstance(a, ast.Constant) and a.value == '--yes' for a in n.value.args))
        cli_parser = argparse.ArgumentParser()
        flag_ns = {'_flag': lambda p, *names, help, **kw: p.add_argument(*names, action='store_true', help=help, **kw), 'gateway_restart': cli_parser, '_INTERRUPT_HELP': 'interrupt {verb}'}
        exec(compile(ast.Module(body=[flag_expr], type_ignores=[]), '<actual CLI flag registration>', 'exec'), flag_ns)
        self.assertFalse(cli_parser.parse_args([]).interrupt)
        for flag in ['--yes', '-y', '--interrupt']:
            self.assertTrue(cli_parser.parse_args([flag]).interrupt)
        guard = next(x for x in evidence if x['path'].endswith('gateway_lifecycle_guards.py'))['excerpts'][0]['text']
        for interrupt in [False, True]:
            ns = {'Path': Path, 'Optional': __import__('typing').Optional, 'refusal_lines': lambda *a, **kw: ['Refusing to restart the gateway: live', 'detail', 'hint', 'end'], '_candidate_homes': lambda *a: [Path('/synthetic')], 'sys': types.SimpleNamespace(stdin=types.SimpleNamespace(isatty=lambda: False), stdout=types.SimpleNamespace(isatty=lambda: False), exit=lambda code: (_ for _ in ()).throw(SystemExit(code)))}
            # Strip only the gateway print import; no runtime module imports allowed.
            guard_node = ast.parse(guard).body[0]
            guard_node.body = [n for n in guard_node.body if not isinstance(n, ast.ImportFrom)]
            ns.update(print_warning=Mock(), print_error=Mock(), INTERRUPT_FLAG='--yes')
            exec(compile(ast.Module(body=[guard_node], type_ignores=[]), '<extracted CLI guard>', 'exec'), ns)
            if interrupt:
                ns['refuse_while_sessions_live']('restart', types.SimpleNamespace(interrupt=True))
            else:
                with self.assertRaises(SystemExit):
                    ns['refuse_while_sessions_live']('restart', types.SimpleNamespace(interrupt=False))

if __name__ == '__main__':
    unittest.main(verbosity=2)
