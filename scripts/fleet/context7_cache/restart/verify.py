"""Read-only source verification plus synthetic tests; persist genuine test output."""
import ast
import contextlib
import difflib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import unittest
ROOT = Path(__file__).resolve().parent

def sha(data):
    return hashlib.sha256(data).hexdigest()

def main():
    manifest = json.loads((ROOT / 'manifest.json').read_text())
    raw = Path(manifest['source']).read_bytes()  # Source only, never gateway state.
    assert sha(raw) == manifest['source_sha256']
    text = raw.decode()
    old, new = manifest['old_line'] + '\n', manifest['new_line'] + '\n'
    assert text.count(old) == 1
    overlay = text.replace(old, new, 1)
    assert sha(overlay.encode()) == manifest['overlay_sha256']
    expected_patch = ''.join(difflib.unified_diff(text.splitlines(True), overlay.splitlines(True), fromfile='a/gateway_coordinated_restart.py', tofile='b/gateway_coordinated_restart.py'))
    assert expected_patch.encode() == (ROOT / 'gateway_coordinated_restart.patch').read_bytes()
    before, after = ast.parse(text), ast.parse(overlay)
    for tree in [before, after]:
        tree.body = [n for n in tree.body if not isinstance(n, ast.FunctionDef) or n.name != 'run_restart']
    assert ast.dump(before) == ast.dump(after)
    spec = importlib.util.spec_from_file_location('synthetic_overlay_tests', ROOT / 'test_overlay.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        result = unittest.TextTestRunner(stream=output, verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(module))
    # Check source again after executing the tests; no live action took place.
    assert sha(Path(manifest['source']).read_bytes()) == manifest['source_sha256']
    receipt = {'ok': result.wasSuccessful(), 'tests_run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors), 'skipped': len(result.skipped), 'source_unchanged': True, 'one_line_patch_verified': True, 'outside_function_ast_unchanged': True, 'subprocess': 'Mock only; no gateway or service-manager process invoked', 'stdout_stderr': output.getvalue()}
    (ROOT / 'test-result.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(output.getvalue())
    assert receipt['ok']
    inventory = {str(p.relative_to(ROOT)): sha(p.read_bytes()) for p in sorted(ROOT.iterdir()) if p.is_file() and p.name != 'artifact-hashes.json'}
    (ROOT / 'artifact-hashes.json').write_text(json.dumps(inventory, indent=2) + '\n')
    print(json.dumps({k: v for k, v in receipt.items() if k != 'stdout_stderr'}))

if __name__ == '__main__':
    main()
