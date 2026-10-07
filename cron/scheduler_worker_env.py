"""Cron: import path of the restart-safe external worker.

The worker is spawned as ``sys.executable -m cron.scheduler`` (a bare store interpreter goes through
the installation-bound runtime command instead: see ``external_worker_argv``). Its entry module is
``cron.scheduler``, not ``hermes_cli.main``, so nothing bootstraps the gateway's checkout
onto its ``sys.path``; historically it imported ``cron`` only through the implicit ``-m``
cwd entry. That entry is gone under ``PYTHONSAFEPATH`` and useless when the venv's
editable install maps a moved/deleted checkout -- the worker then dies with
"No module named 'cron'" before its ownership ack (#112729, hypothesised cause).

The shared subprocess sanitizer strips Hermes-owned PYTHONPATH entries because user
children must not see our tree. This child IS Hermes, so the pin is applied *after* the
env is built, on the sanitized env -- the sanitizer's other decisions (dropped runtime
site-packages, dropped venv markers) stand.
"""

from __future__ import annotations

import os
import sys
import sysconfig
from pathlib import Path


def _installed_purelib() -> Path | None:
    try:
        return Path(sysconfig.get_paths()["purelib"]).resolve()
    except (KeyError, OSError):
        return None


def pin_hermes_tree_on_pythonpath(worker_env: dict, repo_root: Path) -> dict:
    """Prepend ``repo_root`` to the worker env's own PYTHONPATH (never ``os.environ``'s).

    Skipped when ``repo_root`` is the interpreter's ``purelib``: under a wheel / pipx /
    uv-tool install ``cron/`` lives in site-packages itself, which is already importable,
    and pinning it would move site-packages ahead of the stdlib on ``sys.path``.
    """
    root = str(repo_root)
    if _installed_purelib() == Path(root).resolve():
        return worker_env
    existing = [e for e in worker_env.get("PYTHONPATH", "").split(os.pathsep) if e]
    worker_env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys([root, *existing]))
    return worker_env


def external_worker_argv(repo_root: Path, worker_args: list[str]) -> list[str]:
    """Interpreter-bound ``cron.scheduler`` worker command.

    A bare interpreter (PM's store Python, no venv) gets its dependencies only from
    ``hermes_bootstrap``, and ``python -m cron.scheduler`` imports the ``cron`` package (which needs
    them) before any entry module could run it; the worker env also carries no dependency path, because
    the shared sanitizer strips the Hermes-owned PYTHONPATH. The worker then dies with "No module named
    'ruamel'" before its ownership acknowledgement and every job on a systemd gateway fails. Use the
    installation-bound runtime command, as the Kanban dispatcher does
    (``kanban_db_dispatch._module_hermes_argv``): it pins the repo root and runs the bootstrap in the
    child. A venv interpreter carries its own packages and keeps the plain module form.
    """
    if sys.prefix == sys.base_prefix:
        try:
            from hermes_cli._launchers import runtime_command

            return runtime_command(repo_root, worker_args, module="cron.scheduler", python=sys.executable)
        except Exception:
            pass
    return [sys.executable, "-m", "cron.scheduler", *worker_args]
