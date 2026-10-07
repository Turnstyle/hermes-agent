# Search files test fixture audit

Checked at 2026-10-06 22:23:22 CDT.

Candidate and rc1 used the same test source hash: `19a12538f79a84b8ac5bbf1fdb3d51777f89ea90966f757fa0431c966f62eb8d`. Sources: `evidence/full/results.jsonl`, line 4648; `evidence/full-rc1/results.jsonl`, line 204.

Both runs passed the first 11 cases. Both then exited without a pytest summary. The traceback shows that the test's broad `os.path.abspath` monkeypatch also intercepted `pathlib` and `tests/home_io_guard.py`. Sources: `evidence/full/92366786af53.events.jsonl`, lines 1-11; `evidence/full/92366786af53.log`, lines 1-51; corresponding rc1 files under `evidence/full-rc1/`.

Jev025 classified the cause as `global_monkeypatch_breaks_harness`. It chose `localize_fixture_and_replay`. Source: `jev-025.json`.

The candidate fixture now replaces only the `os` global in `tools.file_operations_search`. Any controller-side `os` access still raises. The shared process-wide `os.path` module remains unchanged. Both original remote normalization assertions remain.

Green replay is pending the existing `finish-tests.py` driver. The file is in `final-focused-manifest.txt`. No separate pytest process was started.
