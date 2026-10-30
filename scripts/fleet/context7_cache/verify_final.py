#!/usr/bin/env python3
"""Integrated fake-provider author checks; no deployment or credential bootstrap."""
from pathlib import Path
import json
import os
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
SCRATCH = Path('/Users/sheldon/.hermes/profiles/shld-mm-hermes-agent/cache/scratch/context7-cache-correction-20261030')
COMMANDS = [
 ['python3','-B',str(HERE/'loader/build_overlay.py')],
 ['python3','-B',str(HERE/'loader/build_env_overlay.py')],
 ['python3','-B',str(HERE/'loader/test_overlay.py')],
 ['python3','-B',str(HERE/'loader/test_stacked_age.py')],
 ['python3','-B',str(HERE/'loader/test_env_overlay.py')],
 ['python3','-B',str(HERE/'config/test_config.py')],
 ['python3','-B',str(HERE/'transport/build_overlay.py')],
 ['python3','-B',str(HERE/'transport/verify_offline.py')],
 ['python3','-B',str(HERE/'restart/verify.py')],
 ['git','diff','--check'],
 ['gitleaks','dir','scripts/fleet/context7_cache','--config','.gitleaks.toml','--redact','--no-banner','--report-format','json','--report-path',str(SCRATCH/'final-gitleaks.json')],
]
def main():
 SCRATCH.mkdir(parents=True,exist_ok=True)
 results=[]
 for i,cmd in enumerate(COMMANDS):
  if cmd[0] == 'python3': cmd = [sys.executable, *cmd[1:]]
  log=SCRATCH/f'final-check-{i}.txt'
  with log.open('w') as out:
   proc=subprocess.run(cmd,cwd=ROOT,stdout=out,stderr=subprocess.STDOUT,timeout=75,env={**os.environ,'TMPDIR':str(SCRATCH)})
  results.append({'command':cmd,'exit_code':proc.returncode,'log':str(log)})
  print(i,proc.returncode,log,flush=True)
  if proc.returncode: break
 receipt={'kind':'author_fake_provider_checks_not_independent_review','ok':len(results)==len(COMMANDS) and all(x['exit_code']==0 for x in results),'source_head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),'results':results}
 (SCRATCH/'final-integrated-result.json').write_text(json.dumps(receipt,indent=2)+'\n')
 return 0 if receipt['ok'] else 1
if __name__=='__main__': sys.exit(main())
