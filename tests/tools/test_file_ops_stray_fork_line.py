"""Fork-child stderr must not become file content or command output."""

import hashlib
import os
import subprocess

import pytest

from tools.environments import local
from tools.file_operations import ExecuteResult, ShellFileOperations
from tools.terminal_tool_sudo import _rewrite_real_sudo_invocations


STRAY = (
    "I0928 22:32:11.197294 59121032 ev_poll_posix.cc:593] "
    "FD from fork parent still in poll list: fd(13, generation: 1)\n"
)


class StrayBeforeShellEnv:
    """Run the actual file commands, then model stderr emitted before bash starts."""

    def __init__(self, cwd):
        self.cwd = str(cwd)

    def execute(self, command, cwd=None, stdin_data=None, **kwargs):
        if cwd and not os.path.isdir(cwd):
            return {"output": STRAY + f"bash: line 1: cd: {cwd}: No such file or directory\n",
                    "returncode": 126}
        result = subprocess.run(
            [local._find_bash(), "-c", command], cwd=cwd or self.cwd,
            input=stdin_data, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, check=False,
        )
        return {"output": STRAY + result.stdout, "returncode": result.returncode}


@pytest.fixture
def stray_ops(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_NATIVE_FILE_READ", "0")
    return ShellFileOperations(StrayBeforeShellEnv(tmp_path))


def test_patch_replace_excludes_pre_shell_stderr(stray_ops, tmp_path):
    path = tmp_path / "note.txt"
    path.write_text("before\n", encoding="utf-8")
    result = stray_ops.patch_replace(str(path), "before", "after")
    assert result.success, result.error
    assert path.read_text(encoding="utf-8") == "after\n"


def test_write_file_verifies_despite_pre_shell_stderr(stray_ops, tmp_path):
    path = tmp_path / "note.txt"
    result = stray_ops.write_file(str(path), "written\n")
    assert result.error is None
    assert result.verified is True
    assert path.read_text(encoding="utf-8") == "written\n"


def test_read_file_excludes_pre_shell_stderr(stray_ops, tmp_path):
    path = tmp_path / "note.txt"
    path.write_text("actual\n", encoding="utf-8")
    result = stray_ops.read_file(str(path))
    assert result.error is None
    assert "actual" in result.content
    assert STRAY.strip() not in result.content


@pytest.mark.platforms("posix")
def test_local_child_preexec_stderr_is_dropped_but_shell_stderr_is_merged(tmp_path, monkeypatch):
    real_popen = subprocess.Popen

    def child_stderr_popen(*args, **kwargs):
        assert "preexec_fn" not in kwargs
        return real_popen(*args, preexec_fn=lambda: os.write(2, STRAY.encode()), **kwargs)

    env = local.LocalEnvironment(cwd=str(tmp_path))
    monkeypatch.setattr(local.subprocess, "Popen", child_stderr_popen)
    result = env.execute("echo real; echo err >&2")
    assert result["returncode"] == 0
    assert "real" in result["output"]
    assert "err" in result["output"]
    assert STRAY.strip() not in result["output"]


def test_hash_verifier_scans_for_digest_and_degrades_without_one(stray_ops, tmp_path, monkeypatch):
    path = tmp_path / "note.txt"
    content = b"written\n"
    path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    monkeypatch.setattr(stray_ops, "_exec", lambda *a, **k: ExecuteResult(
        stdout=STRAY + f"{digest}  {path}\n", exit_code=0))
    assert stray_ops._verify_written_hash(str(path), content) == (True, None)
    monkeypatch.setattr(stray_ops, "_exec", lambda *a, **k: ExecuteResult(
        stdout=STRAY + "hash unavailable\n", exit_code=0))
    assert stray_ops._verify_written_hash(str(path), content) == (None, None)


def test_start_marker_preserves_stdin_and_cwd_failure(stray_ops, tmp_path):
    result = stray_ops._exec("cat", stdin_data="from stdin\n")
    assert result.stdout == "from stdin\n"
    assert result.exit_code == 0

    missing = tmp_path / "missing"
    result = stray_ops._exec("pwd", cwd=str(missing))
    assert result.exit_code == 126
    assert "working directory" in result.cwd_error
    assert STRAY.strip() in result.cwd_error


def test_start_marker_keeps_sudo_in_command_position(tmp_path):
    class CaptureEnv:
        cwd = str(tmp_path)

        def execute(self, command, **kwargs):
            rewritten, count = _rewrite_real_sudo_invocations(command)
            assert count == 1
            assert "\nsudo -S -p '' true" in rewritten
            marker = command.splitlines()[0].split()[1]
            return {"output": marker + "\n", "returncode": 0}

    assert ShellFileOperations(CaptureEnv())._exec("sudo true").exit_code == 0
