"""Build review-only artifacts from pinned source; never import or execute Hermes."""
import ast
import difflib
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ADAPTER = Path('/Users/sheldon/.hermes/fleet-node-state/u3g/g3f/gateway_coordinated_restart.py')
RELEASE = Path('/Users/sheldon/.hermes/releases/stable-v0.22.5-carry-d4c5aec662c8/candidate')
OLD = '    cmd = [hermes_bin(), "-p", "default", "gateway", "restart"]\n'
NEW = '    cmd = [hermes_bin(), "-p", "default", "gateway", "restart", "--yes"]\n'

def digest(data):
    return hashlib.sha256(data).hexdigest()

def write_json(name, value):
    (ROOT / name).write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')

def main():
    raw = ADAPTER.read_bytes()
    assert digest(raw) == 'df56ebff6a48c22b1a698d491da4195e2fe54d2d19f8df0133b31654eac3d3a8', 'Exact source changed: stop; do not regenerate against drift.'
    text = raw.decode()
    assert text.count(OLD) == 1
    updated = text.replace(OLD, NEW, 1)
    before_ast, after_ast = ast.parse(text), ast.parse(updated)
    function = next(n for n in before_ast.body if isinstance(n, ast.FunctionDef) and n.name == 'run_restart')
    lines = text.splitlines(keepends=True)
    fixture = ''.join(lines[function.lineno - 1:function.end_lineno]) + '\n'
    (ROOT / 'run_restart.before.txt').write_text(fixture)
    patch = ''.join(difflib.unified_diff(text.splitlines(True), updated.splitlines(True), fromfile='a/gateway_coordinated_restart.py', tofile='b/gateway_coordinated_restart.py'))
    (ROOT / 'gateway_coordinated_restart.patch').write_text(patch)
    evidence = []
    projections = [(ADAPTER, [(593, 602), (1490, 1525), (1409, 1430), (1780, 1798), (1815, 1830), (1900, 1925), (1940, 1962), (2482, 2499), (2613, 2635)]),
                   (RELEASE / 'hermes_cli/subcommands/gateway.py', [(11, 21), (141, 155)]),
                   (RELEASE / 'hermes_cli/gateway_lifecycle_guards.py', [(97, 129)]),
                   (RELEASE / 'hermes_cli/gateway.py', [(5454, 5471)]),
                   (RELEASE / 'hermes_cli/main.py', [(708, 716)]),
                   (RELEASE / 'hermes_cli/_early_recovery.py', [(70, 81)]),
                   (Path('/Users/sheldon/.local/bin/hermes'), [(1, 2)]),
                   (Path('/Users/sheldon/.hermes/releases/stable-v0.22.0-carry-6a0df67dd320/candidate/.hermes/bin/hermes'), [(1, 6), (120, 133)])]
    for path, ranges in projections:
        data = path.read_bytes()
        source_lines = data.decode().splitlines()
        evidence.append({'path': str(path), 'sha256': digest(data), 'bytes': len(data), 'excerpts': [{'start': start, 'end': end, 'text': '\n'.join(source_lines[start-1:end])} for start, end in ranges]})
    write_json('source-evidence.json', evidence)
    # Outside run_restart, source bytes and AST are unchanged, including both call sites.
    original_function = ast.get_source_segment(text, function)
    after_function = next(n for n in after_ast.body if isinstance(n, ast.FunctionDef) and n.name == 'run_restart')
    assert text.replace(original_function, '') == updated.replace(ast.get_source_segment(updated, after_function), '')
    write_json('manifest.json', {'schema': 1, 'deploy_authorized': False, 'source': str(ADAPTER), 'source_sha256': digest(raw), 'source_bytes': len(raw), 'overlay_sha256': digest(updated.encode()), 'overlay_bytes': len(updated.encode()), 'old_line': OLD.rstrip('\n'), 'new_line': NEW.rstrip('\n'), 'function_lines': [function.lineno, function.end_lineno], 'patch_sha256': digest(patch.encode()), 'fixture_sha256': digest(fixture.encode()), 'evidence_sha256': digest((ROOT / 'source-evidence.json').read_bytes()), 'scope': 'One argv literal inside existing run_restart; no call-site, coordination, child context, board safety, timeout, environment, error or fallback changes.', 'deployment_gate': 'Deputy only: exact source hash must match; verify selected hermes_bin target implements the evidenced --yes semantics. No standalone adapter invocation.'})
    print(json.dumps({'source_sha256': digest(raw), 'overlay_sha256': digest(updated.encode()), 'changed_lines': 1}))

if __name__ == '__main__':
    main()
