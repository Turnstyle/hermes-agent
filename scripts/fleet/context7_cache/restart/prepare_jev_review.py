"""Save a bounded Jev request and an honest non-invocation receipt. No CLI import/call."""
import hashlib
import json
from pathlib import Path
ROOT = Path(__file__).resolve().parent

def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()

def main():
    evidence = json.loads((ROOT / 'source-evidence.json').read_text())
    manifest = json.loads((ROOT / 'manifest.json').read_text())
    # Select whole relevant sections, not isolated affirmative quotes. Include coordination,
    # failure fallback, notice side effects, provenance, and startup counterevidence.
    state = {'run_tag': 'context7-cache-restart-overlay-review-20261030', 'authority': 'Review only. No restart, messages, board writes, secrets, live edits, config changes or deployment. Deputy alone may later deploy after authorization.',
             'proposal': manifest, 'evidence': evidence,
             'limits': ['Synthetic tests cannot prove health or actual interrupted-session recovery.', 'An existing coordinator-force service-manager fallback is preserved, not newly authorized.', 'No blanket notice suppression was found; wake-cmd none and omitted notify-cmd are only partial controls.', 'HERMES_BIN and PATH can choose a different executable; their live values were not read.'],
             'reported_incident_unverified': 'Parent reports PID5719 from Oct29 and two failed reloads, latest r-20261030-152121-60b0; no live state inspected.'}
    topics = [
        'Does --yes express interrupt consent rather than separate-gateway force?',
        'Is default profile targeting preserved without --all?',
        'Is the one-line delta confined to the existing coordinated restart window?',
        'Are drain markers, pause ownership and board dispatch gates unchanged?',
        'Can the unmodified bare argv explain the reported non-TTY live-work refusal?',
        'Is a broader claim of successful session recovery unsupported by offline mocks?',
        'Does omitting notify-cmd still leave steering and board comments possible?',
        'Does wake-cmd none disable only the wake route rather than all notices?',
        'Is exact source hash matching required before deputy deployment?',
        'Are timeout and nonzero-exit behavior preserved rather than auto-retried?',
        'Is the existing coordinator-force budget fallback a residual risk, not a new change?',
        'Does parser --force have different semantics from coordinator --force?',
        'Can the retained flow still call the same run_restart after its own gates?',
        'Does selected binary provenance need checking without reading environment secrets?',
        'Does the launcher old directory name alone establish an old runtime?',
        'Do launcher sys.path and main imports support the evidenced live release chain?',
        'Would running the requested Jev CLI conflict with the no-dotenv startup rule?',
        'Is declining Jev under that rule missing advice rather than a passing review?',
        'Is actual restart verification outside this task authority?',
        'Are inherited child environment flags preserved while real environment stays unread?',
        'Would sending a coordinator request violate the no-board/no-message restriction?',
        'Can we claim a universal suppression flag exists from these source excerpts?',
        'Does adding --yes itself expand the drained window or create a new retry?',
        'Is the reported historical PID a task input rather than independently verified state?']
    questions = {}
    for i, topic in enumerate(topics):
        qid = f'R{i+1:02}'
        if i % 3 == 0:
            questions[qid] = {'type': 'choice', 'instructions': topic + ' Judge only the supplied exact source and distinguish validated mechanism from deployment authority.', 'criteria': {'supported': 'The supplied evidence supports the proposition.', 'contradicted': 'The supplied evidence contradicts the proposition.', 'insufficient': 'Evidence is incomplete or the scope differs.'}}
        elif i % 3 == 1:
            questions[qid] = {'type': 'score', 'instructions': topic + ' Score evidentiary support, not operational approval.', 'criteria': ['Unsupported', 'Weak', 'Mixed', 'Mostly supported', 'Directly supported']}
        else:
            questions[qid] = {'type': 'noul', 'instructions': topic + ' Explain the decisive evidence and material exception in a concise source-grounded answer.'}
    request = {'state': state, 'questions': questions, 'long_context': False}
    sizes = {'state_bytes': len(canonical(state)), 'questions_bytes': len(canonical(questions)), 'request_bytes': len(canonical(request)), 'question_count': len(questions), 'focused_adapter_bytes': len((ROOT / 'run_restart.before.txt').read_bytes())}
    focused_evidence = []
    for item in evidence:
        if item['path'] == manifest['source']:
            focused_evidence.append(dict(item, excerpts=[item['excerpts'][1]]))
        elif item['path'].endswith(('hermes_cli/subcommands/gateway.py', 'hermes_cli/gateway_lifecycle_guards.py', 'hermes_cli/gateway.py')):
            focused_evidence.append(item)
    focused_state = {'proposal': manifest, 'evidence': focused_evidence}
    sizes['focused_state_bytes'] = len(canonical(focused_state))
    sizes['state_breadth_ratio'] = sizes['state_bytes'] / sizes['focused_state_bytes']
    assert 2 <= sizes['state_breadth_ratio'] <= 3, sizes
    assert sizes['state_bytes'] <= 32768 and sizes['question_count'] <= 100 and sizes['question_count'] >= 20
    (ROOT / 'jev-request.json').write_text(json.dumps(request, indent=2) + '\n')
    # This is explicitly a local blocker receipt, NOT a fabricated Jev/provider response.
    receipt = {'ok': False, 'status': 'not_invoked', 'advice': 'missing', 'reason': 'Task forbids commands loading secrets. Exact launcher imports hermes_cli.main, whose startup calls load_hermes_dotenv before dispatch. Only update skips external secret sources; jev does not. No credential/provider lookup or repair attempted.', 'requested_command_not_run': 'hermes -p shld-mm-hermes-agent jev evaluate --file jev-request.json', 'provider': 'not inspected', 'credential': 'not inspected', 'answers': {}, 'sizes': sizes, 'request_sha256': hashlib.sha256((ROOT / 'jev-request.json').read_bytes()).hexdigest(), 'parse_rule_if_later_authorized': "result=json.JSONDecoder().raw_decode(out[out.index('{'):])[0]; require result.ok and exact returned/requested IDs before using advice; confidence < 0.50 is no signal; absent confidence unknown", 'state_breadth': 'Broader context includes complete relevant adapter function, both coordinated flows, pause/drain, notice and callback boundaries, CLI parser/guard, launcher and dotenv source; no narrower affirmative-only state.'}
    (ROOT / 'jev-result.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps(sizes))

if __name__ == '__main__':
    main()
