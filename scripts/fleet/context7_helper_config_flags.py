"""Dry-run by default. Add only the approved helper assertion, with cold backups.

Use the release venv Python (PyYAML). No secret values or environment files are read.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import yaml

HELPER = "/Users/sheldon/.hermes/profiles/shld-keys-router/tools/key_op.py"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def child(mapping, key):
    if not isinstance(mapping, yaml.MappingNode):
        raise ValueError("expected YAML mapping")
    matches = [v for k, v in mapping.value if k.value == key]
    if len(matches) != 1:
        raise ValueError("missing or duplicate YAML field: " + key)
    return matches[0]


def plan(path):
    raw = path.read_bytes()
    config = yaml.safe_load(raw)
    source = (config or {}).get("secrets", {}).get("onepassword", {})
    if source.get("binary_path") != HELPER:
        return None
    if source.get("allow_unattended_helper") is True and source.get("cache_ttl_seconds") == 86400:
        return {"path": path, "raw": raw, "new": raw, "status": "already_set"}
    text = raw.decode("utf-8")
    root = yaml.compose(text)
    node = child(child(root, "secrets"), "onepassword")
    # Use source marks to preserve every unrelated byte, comment and setting.
    existing = [(k, v) for k, v in node.value if k.value == "allow_unattended_helper"]
    if existing:
        if len(existing) != 1 or not isinstance(existing[0][1], yaml.ScalarNode):
            raise ValueError("ambiguous flag")
        value = existing[0][1]
        new = text[:value.start_mark.index] + "true" + text[value.end_mark.index:]
    else:
        key = next(k for k, v in node.value if k.value == "binary_path")
        if node.flow_style:
            raise ValueError("flow mapping requires explicit operator review")
        lines = text.splitlines(keepends=True)
        lines.insert(key.start_mark.line, " " * key.start_mark.column + "allow_unattended_helper: true\n")
        new = "".join(lines)
    # The owner requires a daily cache to protect the shared 1Password read cap.
    ttl_root = yaml.compose(new)
    ttl_node = child(child(ttl_root, "secrets"), "onepassword")
    ttl_value = child(ttl_node, "cache_ttl_seconds")
    if not isinstance(ttl_value, yaml.ScalarNode):
        raise ValueError("ambiguous cache TTL")
    new = new[:ttl_value.start_mark.index] + "86400" + new[ttl_value.end_mark.index:]
    expected = copy.deepcopy(config)
    expected["secrets"]["onepassword"]["allow_unattended_helper"] = True
    expected["secrets"]["onepassword"]["cache_ttl_seconds"] = 86400
    if yaml.safe_load(new) != expected:
        raise ValueError("semantic delta exceeds approved flag")
    return {"path": path, "raw": raw, "new": new.encode(), "status": "change"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profiles-root", type=Path, default=Path("/Users/sheldon/.hermes/profiles"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    args = parser.parse_args()
    if args.apply and not args.backup_dir:
        parser.error("--apply requires --backup-dir in cold storage")
    plans = [item for path in sorted(args.profiles_root.glob("*/config.yaml"))
             if (item := plan(path)) is not None]
    manifest = {"mode": "apply" if args.apply else "dry_run", "matched_count": len(plans),
                "changed_count": sum(x["status"] == "change" for x in plans), "profiles": []}
    for item in plans:
        path, raw, new = item["path"], item["raw"], item["new"]
        row = {"profile": path.parent.name, "path": str(path), "status": item["status"],
               "before_sha256": sha(raw), "after_sha256": sha(new)}
        if args.apply and item["status"] == "change":
            args.backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            backup = args.backup_dir / (path.parent.name + "-config.yaml")
            with backup.open("xb") as out:
                os.chmod(backup, 0o600)
                out.write(raw)
            if sha(backup.read_bytes()) != sha(raw):
                raise ValueError("backup verification failed")
            row["backup"] = str(backup)
            fd, temp = tempfile.mkstemp(prefix=".context7-", dir=path.parent)
            try:
                with os.fdopen(fd, "wb") as out:
                    out.write(new)
                    out.flush()
                    os.fsync(out.fileno())
                shutil.copystat(path, temp)
                if sha(Path(temp).read_bytes()) != sha(new) or path.read_bytes() != raw:
                    raise ValueError("hash verification or concurrent write conflict")
                os.replace(temp, path)
                if path.read_bytes() != new:
                    raise ValueError("post-write verification failed")
            finally:
                Path(temp).unlink(missing_ok=True)
        manifest["profiles"].append(row)
        if args.apply:
            (args.backup_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
