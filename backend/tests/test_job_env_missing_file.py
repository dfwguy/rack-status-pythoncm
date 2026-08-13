# Author: Rafael M. Koike & Cursor Agent ("Ada")
"""Loading job-env overrides with no overrides file must not recurse.

Found by running the app on a clean checkout: startup emitted a wall of FileNotFoundError
tracebacks and then died with ``OSError: [Errno 24] Too many open files``.

The cycle is ``_job_env_overrides_load`` -> ``_materialize_job_env_overrides_into_os_environ``
-> ``_apply_catalog_active_to_environ`` -> ``_job_env_overrides_load``. When the file exists the
mtime cache short-circuits the second entry and it stops at depth two. When it does **not** exist
there is no mtime to cache, so nothing terminates it -- and every level is holding the flock
handle opened on the way in, so the descriptor table empties out.

Invisible on any deployment, because there the file exists. Immediate on a fresh install, which
is exactly where a first-run failure is most expensive.
"""
from __future__ import annotations

import pytest

import app as A


@pytest.fixture
def no_overrides_file(tmp_path, monkeypatch):
    """Point the override store at a directory that has no overrides file in it."""
    # The path is hardcoded to REPO_ROOT/var/cache, not read from the environment -- the env var
    # of that name is an OUTPUT of the loader, for forked workers. So patch the resolver.
    monkeypatch.setattr(A, "_job_env_overrides_path", lambda: tmp_path / "job_env_overrides.json")
    monkeypatch.setattr(A, "_job_env_flock_path", lambda: tmp_path / "job_env_overrides.flock")
    monkeypatch.setattr(A, "_JOB_ENV_OVERRIDES_CACHE", None)
    monkeypatch.setattr(A, "_JOB_ENV_OVERRIDES_MTIME", None)
    monkeypatch.delenv("RACK_STATUS_DEMO", raising=False)  # demo mode skips the flock entirely
    return tmp_path


def test_a_missing_overrides_file_returns_empty(no_overrides_file):
    assert A._job_env_overrides_load() == {}


def test_it_does_not_re_enter_itself(no_overrides_file, monkeypatch):
    """Count re-entry, not descriptors.

    Measuring open descriptors before and after does NOT catch this: every level releases its
    flock handle as the stack unwinds, so the count is back to normal by the time the call
    returns. The leak only exists at peak depth. Re-entry count is the honest signal -- with the
    bug it climbs until the descriptor table is empty; fixed, the second entry finds the cache
    already marked absent and returns.
    """
    real = A._job_env_overrides_load
    calls = {"n": 0}

    def counting():
        calls["n"] += 1
        if calls["n"] > 25:
            raise RecursionError("unbounded re-entry into _job_env_overrides_load")
        return real()

    # The recursion goes out through _apply_catalog_active_to_environ and back in by global name,
    # so patching the module attribute is what the recursive call resolves to.
    monkeypatch.setattr(A, "_job_env_overrides_load", counting)

    assert A._job_env_overrides_load() == {}
    assert calls["n"] <= 3, f"re-entered {calls['n']} times"


def test_repeated_loads_stay_consistent(no_overrides_file):
    """Nothing accumulates in the cache either."""
    for _ in range(3):
        assert A._job_env_overrides_load() == {}
    assert A._JOB_ENV_OVERRIDES_CACHE == {}
    assert A._JOB_ENV_OVERRIDES_MTIME is None


def test_a_file_appearing_later_is_picked_up(no_overrides_file):
    """The empty-state guard must not latch: writing the file must still take effect."""
    assert A._job_env_overrides_load() == {}

    key = next(iter(A._JOB_ENV_ALLOWED_KEYS))
    path = no_overrides_file / "job_env_overrides.json"
    path.write_text(f'{{"{key}": "from-the-file"}}', encoding="utf-8")

    assert A._job_env_overrides_load().get(key) == "from-the-file"
