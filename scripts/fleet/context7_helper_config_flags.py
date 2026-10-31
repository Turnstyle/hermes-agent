"""Scoped Context7 correction. Dry-run by default; never invokes a secret provider.

Helper opt-in remains explicit and pinned. Ordinary onepassword TTLs above 1800
are capped; Context7 is explicitly enabled only in the two approved profiles.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import yaml

HELPER = "/Users/sheldon/.hermes/profiles/shld-keys-router/tools/key_op.py"
CONTEXT7_PROFILES = frozenset({"shld-coordinator", "shld-host-diagnostics"})
TTL = 1800


def sha(data):
    return hashlib.sha256(data).hexdigest()


def no_symlinks(path):
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError("symlink path refused")


def snapshot(path):
    no_symlinks(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("non-regular or hard-linked config refused")
        raw = stream.read()
        if identity(info) != identity(os.fstat(stream.fileno())):
            raise ValueError("concurrent config read")
    return raw, info


def identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_mode, info.st_uid, info.st_gid)


def parse(raw, strict=True):
    text = raw.decode("utf-8")
    # Unscoped configs may contain anchors; never edit an anchored config.
    if strict and any(isinstance(token, (yaml.AliasToken, yaml.AnchorToken))
                      for token in yaml.scan(text)):
        raise ValueError("YAML anchors/aliases refused")
    root = yaml.compose(text)
    visited = set()
    def validate(node):
        if id(node) in visited:
            return
        visited.add(id(node))
        if isinstance(node, yaml.MappingNode):
            seen = set()
            for key, value in node.value:
                if not isinstance(key, yaml.ScalarNode) or key.tag != "tag:yaml.org,2002:str":
                    raise ValueError("non-string YAML key refused")
                if key.value in seen or key.value == "<<":
                    raise ValueError("duplicate or merge YAML key refused")
                seen.add(key.value)
                validate(value)
        elif isinstance(node, yaml.SequenceNode):
            for value in node.value:
                validate(value)
    validate(root)
    config = yaml.safe_load(text)
    if not isinstance(config, dict):
        raise ValueError("config must be a mapping")
    return text, root, config


def field(node, key):
    if not isinstance(node, yaml.MappingNode):
        raise ValueError("expected YAML mapping")
    return next((v for k, v in node.value if k.value == key), None)


def literal_at(root, text, keys, tag, value):
    """Prove an explicit scalar at its original path, not an alias/coercion."""
    node = root
    for key in keys:
        if not isinstance(node, yaml.MappingNode):
            return False
        pair = next(((k, v) for k, v in node.value if k.value == key), None)
        if pair is None or pair[1].start_mark.index < pair[0].end_mark.index:
            return False
        node = pair[1]
    return (isinstance(node, yaml.ScalarNode)
            and node.tag == 'tag:yaml.org,2002:' + tag
            and text[node.start_mark.index:node.end_mark.index] == value)


def plan(path):
    path = Path(path)
    raw, info = snapshot(path)
    text, root, config = parse(raw, strict=False)
    expected = copy.deepcopy(config)
    updates = []
    secrets = config.get("secrets", {})
    if not isinstance(secrets, dict):
        raise ValueError("invalid secrets mapping")
    source = secrets.get("onepassword", {})
    if not isinstance(source, dict):
        raise ValueError("invalid onepassword mapping")
    helper = source.get("binary_path") == HELPER
    ttl = source.get("cache_ttl_seconds")
    if helper:
        updates += [(('secrets', 'onepassword', 'allow_unattended_helper'), True),
                    (('secrets', 'onepassword', 'cache_ttl_seconds'), TTL)]
    elif type(ttl) in (int, float) and ttl > TTL:
        updates.append((('secrets', 'onepassword', 'cache_ttl_seconds'), TTL))
    if path.parent.name in CONTEXT7_PROFILES:
        updates.append((('mcp_servers', 'context7', 'enabled'), True))
    if not updates:
        return None
    # Read-only recognition follows path and duplicate-key validation, but must
    # precede the anchor-refusing mutation parser. Never infer missing defaults.
    if (helper and source.get('allow_unattended_helper') is True
            and type(ttl) is int and ttl <= TTL
            and literal_at(root, text,
                           ('secrets', 'onepassword', 'allow_unattended_helper'),
                           'bool', 'true')
            and literal_at(root, text,
                           ('secrets', 'onepassword', 'cache_ttl_seconds'),
                           'int', str(ttl))
            and (path.parent.name not in CONTEXT7_PROFILES
                 or literal_at(root, text, ('mcp_servers', 'context7', 'enabled'),
                               'bool', 'true'))):
        return {"path": path, "raw": raw, "new": raw, "info": info,
                "status": "already_set", "settings": []}
    parse(raw)  # Anchors/aliases remain forbidden for every mutation.
    changes = []
    for keys, value in updates:
        current = expected
        for key in keys[:-1]:
            current = current.setdefault(key, {})
            if not isinstance(current, dict):
                raise ValueError("target parent must be a mapping")
        if type(current.get(keys[-1])) is type(value) and current.get(keys[-1]) == value:
            _, probe, _ = parse(text.encode())
            for key in keys:
                probe = field(probe, key)
            canonical = 'true' if value is True else str(value)
            if isinstance(probe, yaml.ScalarNode) and text[probe.start_mark.index:probe.end_mark.index] == canonical:
                continue
        current[keys[-1]] = value
        changes.append('.'.join(keys))
        # Recompose after each local edit so marks always refer to current text.
        _, node, _ = parse(text.encode())
        for index, key in enumerate(keys):
            target = field(node, key)
            if target is None:
                if node.flow_style or not node.value:
                    raise ValueError("empty/flow mapping insertion requires review")
                first_key = node.value[0][0]
                offset = text.rfind('\n', 0, first_key.start_mark.index) + 1
                indent = first_key.start_mark.column
                newline = '\r\n' if '\r\n' in text else '\n'
                addition = ''
                for depth, missing in enumerate(keys[index:]):
                    final = depth == len(keys[index:]) - 1
                    literal = ('true' if value is True else str(value)) if final else ''
                    addition += ' ' * (indent + 2 * depth) + missing + ':' + (' ' + literal if final else '') + newline
                text = text[:offset] + addition + text[offset:]
                break
            if index == len(keys) - 1:
                if not isinstance(target, yaml.ScalarNode) or target.style in ('|', '>'):
                    raise ValueError("non-scalar target refused")
                literal = 'true' if value is True else str(value)
                text = text[:target.start_mark.index] + literal + text[target.end_mark.index:]
            else:
                node = target
    if parse(text.encode())[2] != expected:
        raise ValueError("semantic delta exceeds approved settings")
    return {"path": path, "raw": raw, "new": text.encode(), "info": info,
            "status": "change" if changes else "already_set", "settings": changes}


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def exclusive_file(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
    if path.read_bytes() != data:
        raise ValueError("backup verification failed")


def unchanged(item):
    raw, info = snapshot(item['path'])
    if raw != item['raw'] or identity(info) != identity(item['info']):
        raise ValueError("concurrent config write conflict")


def apply(plans, backup_dir, manifest):
    changed = [item for item in plans if item['status'] == 'change']
    if not changed:
        return
    no_symlinks(backup_dir)
    # Fresh exclusive directory: neither backups nor manifest can be clobbered.
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    temps = []
    try:
        # Complete ALL durable backups and replacement precreation before any write.
        for item in changed:
            unchanged(item)
            backup = backup_dir / (item['path'].parent.name + '-config.yaml')
            exclusive_file(backup, item['raw'])
            fd, name = tempfile.mkstemp(prefix='.context7-', dir=item['path'].parent)
            temp = Path(name)
            temps.append((item, temp))
            with os.fdopen(fd, 'wb') as out:
                os.fchmod(out.fileno(), stat.S_IMODE(item['info'].st_mode))
                if (os.fstat(out.fileno()).st_uid, os.fstat(out.fileno()).st_gid) != (item['info'].st_uid, item['info'].st_gid):
                    os.fchown(out.fileno(), item['info'].st_uid, item['info'].st_gid)
                out.write(item['new'])
                out.flush()
                os.fsync(out.fileno())
            if temp.read_bytes() != item['new']:
                raise ValueError("replacement verification failed")
        exclusive_file(backup_dir / 'manifest.json', (json.dumps(manifest, indent=2) + '\n').encode())
        sync_dir(backup_dir)
        for item in changed:
            unchanged(item)
        for item, temp in temps:
            unchanged(item)
            os.replace(temp, item['path'])
            sync_dir(item['path'].parent)
            if snapshot(item['path'])[0] != item['new']:
                raise ValueError("post-write verification failed")
    finally:
        for _, temp in temps:
            temp.unlink(missing_ok=True)


def run(profiles_root, do_apply=False, backup_dir=None):
    profiles_root = Path(profiles_root).absolute()
    no_symlinks(profiles_root)
    lock = profiles_root / '.context7-config.lock'
    owned = False
    try:
        if do_apply:
            if backup_dir is None:
                raise ValueError("apply requires a fresh cold backup directory")
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            owned = True
        paths = sorted(profiles_root.glob('*/config.yaml'))
        plans = [item for path in paths if (item := plan(path)) is not None]
        manifest = {'mode': 'apply' if do_apply else 'dry_run',
                    'scanned_count': len(paths), 'matched_count': len(plans),
                    'changed_count': sum(x['status'] == 'change' for x in plans),
                    'profiles': [{'profile': x['path'].parent.name, 'path': str(x['path']),
                                  'status': x['status'], 'settings': x['settings'],
                                  'before_sha256': sha(x['raw']), 'after_sha256': sha(x['new'])}
                                 for x in plans]}
        if do_apply:
            apply(plans, Path(backup_dir).absolute(), manifest)
        return manifest
    finally:
        if owned:
            lock.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profiles-root', type=Path, default=Path('/Users/sheldon/.hermes/profiles'))
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup-dir', type=Path)
    args = parser.parse_args()
    if args.apply and args.backup_dir is None:
        parser.error('--apply requires --backup-dir in cold storage')
    try:
        result = run(args.profiles_root, args.apply, args.backup_dir)
    except (OSError, ValueError, yaml.YAMLError, UnicodeError):
        # Parser exception text may contain source lines. Never emit it.
        parser.exit(1, 'Config correction refused: unsafe YAML/path, backup, or writer conflict.\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
