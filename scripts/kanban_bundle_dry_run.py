"""Read-only live inventory; never calls cleanup or opens a writer."""
import argparse
import json
from pathlib import Path
import sqlite3
from hermes_cli.kanban_bundle_preserve import plan_bundle

p = argparse.ArgumentParser()
p.add_argument('--db', required=True)
p.add_argument('--root', required=True)
p.add_argument('--output', required=True)
a = p.parse_args()
c = sqlite3.connect(Path(a.db).resolve().as_uri() + '?mode=ro', uri=True)
c.row_factory = sqlite3.Row
c.execute('PRAGMA query_only=ON')
items = []
for row in c.execute("SELECT id, workspace_kind, workspace_path FROM tasks WHERE status='done' ORDER BY id"):
    root = Path(row['workspace_path'] or Path(a.root) / row['id'])
    if not root.is_dir():
        continue
    item = dict(row)
    item['workspace_path'] = str(root)
    try:
        plan = plan_bundle(c, row['id'], root)
        item.update(plan)
        dependents = c.execute("SELECT COUNT(*) FROM task_links JOIN tasks ON tasks.id=task_links.child_id WHERE task_links.parent_id=? AND tasks.status NOT IN ('done','archived')", (row['id'],)).fetchone()[0]
        item['action'] = 'KEEP: dependent' if dependents else ('KEEP: dir workspace' if row['workspace_kind']=='dir' else 'WOULD preserve then cleanup; existing worktree/containment checks still apply')
    except Exception as exc:
        item['action'] = 'KEEP: preservation failure ' + type(exc).__name__
    items.append(item)
c.close()
Path(a.output).write_text(json.dumps({'mode':'dry-run; no copies or deletion', 'items':items}, indent=2) + '\n')
print(json.dumps({'inventory':a.output, 'count':len(items)}))
