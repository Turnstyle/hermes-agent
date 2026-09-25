"""The direct-run DM delivery runner selects the committed dependency environment.

``tools/bot_mode_dm.py --run-delivery`` (and the relay ``--wait-reply`` waiter) runs as a plain
script under the backend's ``sys.executable``. Once PM owns the install that is the bare store
Python, and the script never passes through ``hermes_bootstrap``, so nothing put the committed
environment on ``sys.path``: its lazy ``hermes_cli.config`` -> ``hermes_yaml`` import died with
"No module named 'ruamel'" and every Bot Chat DM came back ``ambiguous``. A developer venv keeps
the packages it carries.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pm.environments import install_key, site_packages

REPO = Path(__file__).resolve().parents[2]
RUNNER = REPO / "tools" / "bot_mode_dm.py"

# Run the runner file exactly as a background launch would (its own ``__main__`` block), then report
# whether the process it left behind can import Hermes' config module. Malformed argv: exits 2, sends nothing.
_PROBE = """
import json, runpy, sys
runner = sys.argv[1]
sys.argv = [runner, "--run-delivery"]
try:
    runpy.run_path(runner, run_name="__main__")
    code = None
except SystemExit as exc:
    code = exc.code
try:
    import hermes_cli.config
    config = "ok"
except Exception as exc:
    config = f"{type(exc).__name__}: {exc}"
print(json.dumps({"exit": code, "config": config}))
"""


def _commit_environment(home: Path, *, package_dirs: list[str]) -> None:
    """A committed PM generation for this checkout whose site-packages exposes ``package_dirs``."""
    environment = home / "installs" / install_key(REPO) / "environments" / "g1" / "venv"
    (environment.parent).mkdir(parents=True)
    environment.mkdir()
    (environment / "pyvenv.cfg").write_text(
        "version = {}.{}.{}\n".format(*sys.version_info[:3]), encoding="utf-8")
    packages = site_packages(environment)
    packages.mkdir(parents=True)
    (packages / "deps.pth").write_text("".join(f"{d}\n" for d in package_dirs), encoding="utf-8")
    (home / "installs" / install_key(REPO) / "facts.json").write_text(
        json.dumps({"schema": 1, "packages": {"venv": {"environment": str(environment)}}}), encoding="utf-8")


def _run_probe(python: str, home: Path, *flags: str) -> dict:
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home.parent), "HERMES_HOME": str(home)}
    if sys.platform == "win32":
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
    done = subprocess.run([python, *flags, "-c", _PROBE, str(RUNNER)], env=env, capture_output=True,
                          text=True, timeout=120, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_bare_interpreter_runner_imports_config_from_the_committed_environment(tmp_path):
    """The production shape: a bare interpreter with no site-packages of its own (``-I -S``)."""
    home = tmp_path / "hermes"
    home.mkdir()
    _commit_environment(home, package_dirs=[p for p in sys.path if Path(p).name == "site-packages"])

    result = _run_probe(sys._base_executable, home, "-I", "-S")

    assert result == {"exit": 2, "config": "ok"}


def test_venv_runner_keeps_its_own_packages(tmp_path):
    """A developer venv is never switched: an empty committed environment must not strip its packages."""
    if sys.prefix == sys.base_prefix:
        pytest.skip("the test interpreter is not a venv")
    home = tmp_path / "hermes"
    home.mkdir()
    _commit_environment(home, package_dirs=[])

    result = _run_probe(sys.executable, home, "-I")

    assert result == {"exit": 2, "config": "ok"}
