from unittest.mock import patch
import os
import plistlib


def test_service_path_skips_nonexistent_node_modules(tmp_path):
    """Service PATH should not include node_modules/.bin if it doesn't exist."""
    from hermes_cli.gateway import _build_service_path_dirs
    with patch("hermes_cli.gateway.get_hermes_home", return_value=tmp_path / ".hermes"):
        dirs = _build_service_path_dirs(project_root=tmp_path)
    node_modules_bin = str(tmp_path / "node_modules" / ".bin")
    assert node_modules_bin not in dirs


def test_service_path_includes_node_modules_when_present(tmp_path):
    """Service PATH should include node_modules/.bin when it exists."""
    nm_bin = tmp_path / "node_modules" / ".bin"
    nm_bin.mkdir(parents=True)
    from hermes_cli.gateway import _build_service_path_dirs
    with patch("hermes_cli.gateway.get_hermes_home", return_value=tmp_path / ".hermes"):
        dirs = _build_service_path_dirs(project_root=tmp_path)
    assert str(nm_bin) in dirs


def test_service_paths_prefer_source_launcher_and_omit_installs_bins(tmp_path, monkeypatch):
    from hermes_cli import gateway
    from pm.environments import installs_root

    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    root = tmp_path / "repo"
    launcher_dir = root / ".hermes" / "bin"
    launcher_dir.mkdir(parents=True)
    (launcher_dir / "hermes").touch()
    stale_bin = installs_root() / "old" / "environments" / "old" / "venv" / "bin"
    stale_bin.mkdir(parents=True)
    stale_launcher = installs_root() / "old" / "environments" / "old" / "workspace" / ".hermes" / "bin"
    stale_launcher.mkdir(parents=True)
    (stale_launcher / "hermes").touch()
    monkeypatch.setattr(gateway, "PROJECT_ROOT", root)
    monkeypatch.setenv("PATH", os.pathsep.join((str(stale_bin), str(stale_launcher), "/usr/bin")))

    assert gateway._build_service_path_dirs(project_root=root)[0] == str(launcher_dir)
    plist = plistlib.loads(gateway.generate_launchd_plist().encode("utf-8"))
    launchd_path = plist["EnvironmentVariables"]["PATH"].split(os.pathsep)
    monkeypatch.setattr(gateway, "_build_user_local_paths", lambda home, entries: [str(stale_bin)])
    unit = gateway.generate_systemd_unit(system=False)
    systemd_path = next(line.removeprefix('Environment="PATH=').removesuffix('"')
                        for line in unit.splitlines() if line.startswith('Environment="PATH='))
    for entries in (launchd_path, systemd_path.split(os.pathsep)):
        assert entries[0] == str(launcher_dir)
        assert str(stale_bin) not in entries
        assert str(stale_launcher) not in entries

    monkeypatch.setattr(gateway, "PROJECT_ROOT", installs_root() / "old" / "environments" / "old" / "workspace")
    installed_dirs = gateway._build_service_path_dirs()
    assert installed_dirs[0] == str(stale_launcher)
    assert str(stale_launcher) not in gateway._persisted_service_path_entries(installed_dirs)
