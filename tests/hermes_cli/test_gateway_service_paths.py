from unittest.mock import patch


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


def test_service_path_prefers_source_launcher_over_venv(tmp_path):
    """A ``.hermes/bin/hermes`` source-tree launcher outranks the venv/sys.prefix entries so a
    ``gateway run`` spawned from source resolves the same ``hermes`` the operator installed with."""
    from hermes_cli import gateway

    root = tmp_path / "repo"
    launcher_dir = root / ".hermes" / "bin"
    launcher_dir.mkdir(parents=True)
    (launcher_dir / "hermes").touch()
    venv_bin = root / "venv" / "bin"
    venv_bin.mkdir(parents=True)

    with patch("hermes_cli.gateway.get_hermes_home", return_value=tmp_path / ".hermes"):
        dirs = gateway._build_service_path_dirs(project_root=root)

    assert dirs[0] == str(launcher_dir)
    assert str(venv_bin) in dirs


def test_service_path_omits_source_launcher_when_absent(tmp_path):
    """No ``.hermes/bin/hermes`` in the project root: PATH building (and so the Linux systemd
    unit output) is unchanged from before the source-launcher preference existed."""
    from hermes_cli import gateway

    root = tmp_path / "repo"
    venv_bin = root / "venv" / "bin"
    venv_bin.mkdir(parents=True)

    with patch("hermes_cli.gateway.get_hermes_home", return_value=tmp_path / ".hermes"):
        dirs = gateway._build_service_path_dirs(project_root=root)

    assert str(root / ".hermes" / "bin") not in dirs
    assert dirs[0] == str(venv_bin)
