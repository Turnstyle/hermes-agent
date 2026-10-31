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

# Exact, unique source anchors. Never copy an older full adapter over the live source.
EDITS = [
    (OLD, NEW),
    ('            "retain_pause": bool(getattr(args, "retain_pause", False)),\n',
     '            "no_notices": bool(getattr(args, "no_notices", False)),\n            "retain_pause": bool(getattr(args, "retain_pause", False)),\n'),
    ('def refuse_if_underway(r: dict, args) -> None:\n',
     'def refuse_if_underway(r: dict, args) -> None:\n    if bool(r.get("no_notices")) != bool(getattr(args, "no_notices", False)):\n        raise Fail("Cannot join a request with a different --no-notices policy; nothing changed.")\n'),
    ('    text = notice_text(req)\n',
     '    if req.get("no_notices"):\n        log("notice", "outbound notices suppressed (--no-notices)")\n        return\n    text = notice_text(req)\n'),
    ('    text = back_online_text(req)\n    results: dict[str, str] = {}\n',
     '    if req.get("no_notices"):\n        log("wake", "outbound wakes suppressed (--no-notices)")\n        return {}\n    text = back_online_text(req)\n    results: dict[str, str] = {}\n'),
    ("        atomic_write(NOTICE_FILE, json.dumps({'phase': 'coming', 'request': req['id'], 'text': text, 'deadline_at': req['deadline_at']}))\n",
     "        if req.get('no_notices'):\n            update_request(req['id'], deadline_checkpointed_at=iso(), deadline_checkpoint_cards=cards,\n                           deadline_checkpoint_results={}, deadline_checkpoint_posted_cards=[],\n                           deadline_checkpoint_notices_suppressed=True)\n            log('checkpoint', 'checkpoint census retained; outbound notices suppressed (--no-notices)')\n            return\n        atomic_write(NOTICE_FILE, json.dumps({'phase': 'coming', 'request': req['id'], 'text': text, 'deadline_at': req['deadline_at']}))\n"),
    ('def coordinate(req: dict, args, log, dry: bool) -> None:\n',
     'def coordinate(req: dict, args, log, dry: bool) -> None:\n    req["no_notices"] = bool(req.get("no_notices") or getattr(args, "no_notices", False))\n'),
    ('        key = None if dry else api_key()\n', '        key = None if dry or req.get("no_notices") else api_key()\n'),
    ('        atomic_write(NOTICE_FILE, json.dumps({"phase": "back", "request": rid, "text": text}))\n        for p, tid in cards:\n',
     '        if not req.get("no_notices"):\n            atomic_write(NOTICE_FILE, json.dumps({"phase": "back", "request": rid, "text": text}))\n        for p, tid in ([] if req.get("no_notices") else cards):\n'),
    ('def request_plan_text(rec: dict, args) -> str:\n',
     'def request_plan_text(rec: dict, args) -> str:\n    if rec.get("no_notices"):\n        return (f"Request {rec[\'id\']}: --no-notices; outbound notice/steer/comment/wake/notify routes suppressed. "\n                f"Drain, census, checkpoint/deadline policy, permissions, locks and health checks remain. "\n                f"Status: python3 {SCRIPT} status")\n'),
    ('        if req.get("notify_cmd"):\n', '        if req.get("notify_cmd") and not req.get("no_notices"):\n'),
    ('        r["coordinator"] = {"pid": os.getpid(),',
     '        r["no_notices"] = bool(r.get("no_notices") or getattr(args, "no_notices", False))\n        r["coordinator"] = {"pid": os.getpid(),'),
    ('        sp.add_argument("--notify-cmd", help=',
     '        sp.add_argument("--no-notices", action="store_true", help="suppress outbound notices, steers, checkpoint/card comments, wakes and notify-cmd; keep census, drain, checkpoint policy and health gates")\n        sp.add_argument("--notify-cmd", help='),
]
CHANGED_FUNCTIONS = {'run_restart', 'new_request_record', 'refuse_if_underway', 'send_notices',
                     'wake_bots', 'deadline_checkpoint', 'coordinate', '_coordinate', 'cmd_run', 'build_parser', 'request_plan_text'}

def overlay(text):
    for old, new in EDITS:
        assert text.count(old) == 1, f'Exact unique anchor missing: {old!r}'
        text = text.replace(old, new, 1)
    return text

def main():
    raw = ADAPTER.read_bytes()
    assert digest(raw) == 'df56ebff6a48c22b1a698d491da4195e2fe54d2d19f8df0133b31654eac3d3a8', 'Exact source changed: stop; do not regenerate against drift.'
    text = raw.decode()
    assert text.count(OLD) == 1
    updated = overlay(text)
    before_ast, after_ast = ast.parse(text), ast.parse(updated)
    function = next(n for n in before_ast.body if isinstance(n, ast.FunctionDef) and n.name == 'run_restart')
    lines = text.splitlines(keepends=True)
    fixture = ''.join(lines[function.lineno - 1:function.end_lineno]) + '\n'
    (ROOT / 'run_restart.before.txt').write_text(fixture)
    patch = ''.join(difflib.unified_diff(text.splitlines(True), updated.splitlines(True), fromfile='a/gateway_coordinated_restart.py', tofile='b/gateway_coordinated_restart.py', n=0))
    (ROOT / 'gateway_coordinated_restart.patch').write_text(patch)
    evidence = []
    projections = [(ADAPTER, [(593, 602), (1490, 1525), (1409, 1430), (1780, 1798), (1815, 1830), (1900, 1925), (1940, 1962), (2482, 2499), (2613, 2635), (440, 475), (1171, 1183), (1204, 1212), (1334, 1338), (1569, 1664), (1749, 1757), (1830, 1885), (1923, 1965), (2015, 2037), (2458, 2511)]),
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
    # All bytes outside the explicitly listed functions remain exact-live.
    def outside(source, tree):
        result = source
        for node in reversed(tree.body):
            if isinstance(node, ast.FunctionDef) and node.name in CHANGED_FUNCTIONS:
                segment = ast.get_source_segment(source, node)
                result = result.replace(segment, '', 1)
        return result
    assert outside(text, before_ast) == outside(updated, after_ast)
    write_json('manifest.json', {'schema': 2, 'deploy_authorized': False, 'source': str(ADAPTER), 'source_sha256': digest(raw), 'source_bytes': len(raw), 'overlay_sha256': digest(updated.encode()), 'overlay_bytes': len(updated.encode()), 'old_line': OLD.rstrip('\n'), 'new_line': NEW.rstrip('\n'), 'function_lines': [function.lineno, function.end_lineno], 'patch_sha256': digest(patch.encode()), 'fixture_sha256': digest(fixture.encode()), 'evidence_sha256': digest((ROOT / 'source-evidence.json').read_bytes()), 'changed_functions': sorted(CHANGED_FUNCTIONS), 'scope': 'Explicit opt-in no-notices policy plus supported --yes argv; no drain, census, deadline permission, lock, health or native-child guard change.', 'deployment_gate': 'Review only. Deputy must revalidate exact source and selected executable; no deployment authority.', 'identity_evidence': {'source': 'inherited runtime context; no provider or credential query', 'model': 'gpt-6.1-sol', 'provider': 'openai-codex', 'platform': 'subagent', 'subscription': 'inherited subscription only; billing not independently queried'}})
    print(json.dumps({'source_sha256': digest(raw), 'overlay_sha256': digest(updated.encode()), 'edit_anchors': len(EDITS)}))

if __name__ == '__main__':
    main()
