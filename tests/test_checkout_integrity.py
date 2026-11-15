"""Tests of the root tests/checkout_integrity helper; all canaries are tmp-owned."""
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest


def guard_for(root, request, allowed=()):
    try:
        from tests.checkout_integrity import CheckoutGuard
    except ImportError:
        raise AssertionError("checkout write guard is missing")
    guard = CheckoutGuard(root, allowed).start()
    request.addfinalizer(guard.stop)
    return guard


@pytest.mark.parametrize("operation", [
    "text", "bytes", "open", "os_open", "mkdir", "unlink", "rename_src",
    "rename_dst", "rmtree", "ancestor_rmtree", "truncate", "chmod", "sqlite",
    "sqlite_uri", "sqlite_literal", "sqlite_uppercase",
])
def test_checkout_mutations_refused_before_touching_tmp_canary(tmp_path, request, monkeypatch, operation):
    root = tmp_path / "checkout"
    root.mkdir()
    victim = root / "tracked.txt"
    victim.write_text("keep")
    outside = tmp_path / "outside"
    outside.write_text("outside")
    guard_for(root, request)
    monkeypatch.chdir(root)
    mutations = {
        "text": lambda: victim.write_text("damage"),
        "bytes": lambda: victim.write_bytes(b"damage"),
        "open": lambda: open(victim, "w"),
        "os_open": lambda: os.open(victim, os.O_WRONLY | os.O_TRUNC),
        "mkdir": lambda: (root / "new").mkdir(),
        "unlink": victim.unlink,
        "rename_src": lambda: victim.rename(outside),
        "rename_dst": lambda: outside.replace(victim),
        "rmtree": lambda: shutil.rmtree(root),
        "ancestor_rmtree": lambda: shutil.rmtree(tmp_path),
        "truncate": lambda: os.truncate(victim, 0),
        "chmod": lambda: victim.chmod(0o600),
        "sqlite": lambda: sqlite3.connect(root / "state.db"),
        "sqlite_uri": lambda: sqlite3.connect((root / "state.db").as_uri(), uri=True),
        "sqlite_literal": lambda: sqlite3.connect("file:state.db?mode=ro", uri=False),
        "sqlite_uppercase": lambda: sqlite3.connect("FILE:state.db?mode=ro", uri=True),
    }
    with pytest.raises(pytest.fail.Exception, match="repository checkout"):
        mutations[operation]()
    assert victim.read_text() == "keep"
    assert outside.read_text() == "outside"
    assert not (root / "state.db").exists()


@pytest.mark.require_symlinks
@pytest.mark.parametrize("operation", ["alias", "symlink", "link"])
def test_aliases_and_link_destinations_are_guarded(tmp_path, request, operation):
    root = tmp_path / "checkout"
    root.mkdir()
    victim = root / "tracked.txt"
    victim.write_text("keep")
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    guard_for(root, request)
    calls = {"alias": lambda: (alias / "tracked.txt").write_text("damage"),
             "symlink": lambda: os.symlink(tmp_path, root / "new-link"),
             "link": lambda: os.link(victim, root / "new-hardlink")}
    with pytest.raises(pytest.fail.Exception, match="repository checkout"):
        calls[operation]()
    assert victim.read_text() == "keep"


@pytest.mark.platforms("posix")
def test_descriptor_and_directory_relative_mutations(tmp_path, request):
    root = tmp_path / "checkout"
    root.mkdir()
    victim = root / "tracked.txt"
    victim.write_text("keep")
    guard_for(root, request)
    fd, directory = os.open(victim, os.O_RDONLY), os.open(root, os.O_RDONLY)
    try:
        for mutation in (lambda: os.ftruncate(fd, 0), lambda: os.truncate(fd, 0),
                         lambda: os.unlink("tracked.txt", dir_fd=directory),
                         lambda: os.open("tracked.txt", os.O_WRONLY | os.O_TRUNC, dir_fd=directory)):
            with pytest.raises(pytest.fail.Exception, match="repository checkout"):
                mutation()
    finally:
        os.close(fd)
        os.close(directory)
    assert victim.read_text() == "keep"


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("target", ["outside", "checkout"])
def test_dir_fd_create_uses_directory_not_cwd(tmp_path, request, monkeypatch, target):
    root = tmp_path / "checkout"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    guard_for(root, request)
    directory = os.open(outside if target == "outside" else root, os.O_RDONLY)
    monkeypatch.chdir(root if target == "outside" else outside)
    try:
        if target == "outside":
            fd = os.open("down", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=directory)
            os.close(fd)
            assert (outside / "down").is_file()
        else:
            with pytest.raises(pytest.fail.Exception, match="repository checkout"):
                os.open("down", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=directory)
        assert not (root / "down").exists()
        # Neither a successful nor refused dir_fd call may exempt later opens.
        monkeypatch.chdir(root)
        with pytest.raises(pytest.fail.Exception, match="repository checkout"):
            os.open("unscoped", os.O_WRONLY | os.O_CREAT, 0o600)
        assert not (root / "unscoped").exists()
    finally:
        os.close(directory)


def test_reads_ancestor_mkdir_and_explicit_temp_artifacts_allowed(tmp_path, request):
    root = tmp_path / "checkout"
    root.mkdir()
    victim = root / "tracked.txt"
    victim.write_text("keep")
    database = root / "state.db"
    with sqlite3.connect(database) as db:
        db.execute("create table canary (value text)")
    temp = root / "basetemp"
    guard_for(root, request, allowed=(temp,))
    root.parent.mkdir(exist_ok=True)
    os.makedirs(root.parent, exist_ok=True)
    temp.mkdir()
    (temp / "allowed").write_text("ok")
    cache = root / "__pycache__"
    cache.mkdir()
    (cache / "module.pyc").write_bytes(b"cache")
    assert victim.read_text() == "keep"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        assert db.execute("select count(*) from canary").fetchone() == (0,)
    with sqlite3.connect(":memory:") as db:
        db.execute("select 1")


def test_subprocess_pipe_descriptors_are_not_checkout_files(tmp_path, request):
    guard_for(tmp_path / "checkout", request)
    result = subprocess.run([sys.executable, "-c", "import sys; print(sys.stdin.read())"],
                            input="pipe canary", capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "pipe canary"


@pytest.mark.allow_real_home_io
def test_home_opt_out_does_not_disable_checkout_guard(request):
    state = getattr(request.config, "_checkout_integrity", None)
    assert state is not None, "checkout write guard is missing"
    with pytest.raises(pytest.fail.Exception, match="repository checkout"):
        state[2].check(Path(__file__).resolve())


def git(root, *args):
    executable = shutil.which("git")
    assert executable, "git is required for the checkout canary"
    return subprocess.check_output([executable, *args], cwd=root, stderr=subprocess.DEVNULL)


@pytest.fixture
def tracked_repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init")
    (root / "tracked.txt").write_text("keep")
    git(root, "add", ".")
    git(root, "-c", "user.name=Canary", "-c", "user.email=canary@invalid", "commit", "-m", "base")
    return root


def test_boundary_git_capture_survives_product_mocks(tracked_repo, monkeypatch):
    try:
        from tests.checkout_integrity import snapshot_tracked_paths
    except ImportError:
        raise AssertionError("checkout boundary check is missing")
    assert snapshot_tracked_paths(tracked_repo) == set()
    (tracked_repo / "tracked.txt").unlink()
    def mocked(*args, **kwargs):
        raise AssertionError("must not intercept checkout git")
    for name in ("run", "check_output", "Popen"):
        monkeypatch.setattr(subprocess, name, mocked)
    monkeypatch.setenv("GIT_DIR", str(tracked_repo / "wrong"))
    assert snapshot_tracked_paths(tracked_repo) == {"tracked.txt"}


def test_double_start_does_not_leak_guard(tracked_repo):
    from types import SimpleNamespace
    try:
        from tests.checkout_integrity import start_checkout_guard, pytest_unconfigure
    except ImportError:
        raise AssertionError("checkout write guard is missing")
    config = SimpleNamespace(option=SimpleNamespace(basetemp=None), rootpath=tracked_repo)
    session = SimpleNamespace(config=config)
    start_checkout_guard(session, tracked_repo)
    state = config._checkout_integrity
    try:
        start_checkout_guard(session, tracked_repo)
        assert config._checkout_integrity is state
    finally:
        pytest_unconfigure(config)
    (tracked_repo / "allowed-after-stop").write_text("ok")


@pytest.mark.platforms("posix")
def test_boundary_git_detects_mode_change(tracked_repo):
    try:
        from tests.checkout_integrity import snapshot_tracked_paths
    except ImportError:
        raise AssertionError("checkout boundary check is missing")
    victim = tracked_repo / "tracked.txt"
    victim.chmod(victim.stat().st_mode | 0o111)
    assert snapshot_tracked_paths(tracked_repo) == {"tracked.txt"}


@pytest.mark.parametrize("phase", ["call", "finalizer", "collection", "benign", "python", "collection_python", "sessionfinish"])
def test_nested_cacheprovider_on_session(tracked_repo, tmp_path, phase):
    root = tracked_repo
    damage = "import subprocess, sys; subprocess.run([sys.executable, '-c', \"from pathlib import Path; Path('tracked.txt').write_text('changed')\"], check=True)\n"
    if phase == "call":
        body = "def test_offender():\n    " + damage
    elif phase == "finalizer":
        body = "import pytest\n@pytest.fixture\ndef late():\n    yield\n    " + damage + "def test_offender(late):\n    pass\n"
    elif phase == "collection":
        body = damage + "def test_offender():\n    pass\n"
    elif phase in ("python", "collection_python"):
        write = "from pathlib import Path; Path('tracked.txt').write_text('damage')\n"
        body = ("def test_offender():\n    " + write) if phase == "python" else write
    elif phase == "sessionfinish":
        (root / "conftest.py").write_text("def pytest_sessionfinish():\n    " + damage)
        body = "def test_offender():\n    pass\n"
    else:
        body = "def test_benign(tmp_path):\n    (tmp_path / 'allowed.txt').write_text('ok')\n"
    (root / "test_canary.py").write_text(body)
    env = os.environ.copy()
    for key in ("HOME", "HERMES_HOME", "TMPDIR"):
        target = tmp_path / "child" / key
        target.mkdir(parents=True)
        env[key] = str(target)
    env.update(PYTHONPATH=str(Path(__file__).resolve().parents[1]), PYTHONDONTWRITEBYTECODE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    # Benign run exercises BOTH TMPDIR/basetemp in checkout, cacheprovider ON,
    # and junitxml inside checkout. Only the tiny canary repository is written.
    extra = []
    if phase == "benign":
        env["TMPDIR"] = str(root / "temp")
        Path(env["TMPDIR"]).mkdir()
        extra = ["--basetemp=" + str(root / "basetemp"), "--junitxml=" + str(root / "results.xml")]
    result = subprocess.run([sys.executable, "-m", "pytest", "-p", "tests.checkout_integrity", "-q", "test_canary.py", *extra],
                            cwd=root, env=env, capture_output=True, text=True, timeout=120)
    output = result.stdout + result.stderr
    if phase == "benign":
        assert result.returncode == 0, output
        assert (root / "results.xml").exists()
        assert (root / ".pytest_cache").is_dir()
    elif phase in ("python", "collection_python"):
        assert result.returncode == (2 if phase == "collection_python" else 1), output
        assert "repository checkout" in output
        assert (root / "tracked.txt").read_text() == "keep"
    else:
        assert result.returncode == 1, output
        assert "tracked checkout integrity FAILED" in output
        assert "tracked.txt" in output and "test_canary.py::test_offender" in output
