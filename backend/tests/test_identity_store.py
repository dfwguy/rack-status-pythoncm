# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Identity persistence: uniqueness, retirement, and concurrent writers (FR-1, FR-2b, NFR-5).

Author: Rafael M. Koike & Cursor Agent ("Ada")

The two things worth testing hard are the two that are easy to get subtly wrong. **Retirement**,
because a recycled login makes the audit trail confidently wrong rather than merely incomplete. And
**concurrency**, because a uniqueness check outside the lock lets two racing creates both succeed,
and that failure only appears under load — which is to say, on a customer cluster.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from identity import store as store_mod
from identity.models import Group, IdentityError, User


@pytest.fixture
def st(tmp_path, monkeypatch):
    """A store per test, in its own file. Repointing the env var and resetting the cached handle
    together, because the module caches by path and a stale handle would write to the wrong file."""
    monkeypatch.setenv("NVIS_IDENTITY_FILE", str(tmp_path / "identity.json"))
    store_mod.reset_for_tests()
    yield store_mod.store()
    store_mod.reset_for_tests()


class TestUsers:
    def test_a_created_user_reads_back(self, st):
        st.create_user(User(login="rkoike", display_name="Rafael"))
        got = st.get_user("rkoike")
        assert got is not None
        assert got.display_name == "Rafael"

    def test_lookup_is_case_insensitive(self, st):
        """The canonical form is what is stored, so any case finds it (FR-2a)."""
        st.create_user(User(login="RKoike"))
        assert st.get_user("rkoike") is not None
        assert st.get_user("RKOIKE") is not None

    def test_a_duplicate_login_is_refused(self, st):
        st.create_user(User(login="rkoike"))
        with pytest.raises(IdentityError, match="already exists"):
            st.create_user(User(login="RKoike"))

    def test_an_update_cannot_rename(self, st):
        """The login is the key and `edited()` cannot change it, so this asserts the store agrees:
        updating an unknown login fails rather than quietly inserting a second record."""
        st.create_user(User(login="rkoike"))
        with pytest.raises(IdentityError, match="no such user"):
            st.update_user(User(login="someone-else"))
        assert [u.login for u in st.list_users()] == ["rkoike"]

    def test_an_update_changes_only_mutable_fields(self, st):
        user = st.create_user(User(login="rkoike", display_name="R"))
        st.update_user(user.edited(display_name="Rafael M. Koike", disabled=True))
        got = st.get_user("rkoike")
        assert got.display_name == "Rafael M. Koike"
        assert got.disabled is True
        assert got.login == "rkoike"

    def test_persistence_survives_a_fresh_handle(self, st, tmp_path):
        """AC-1: the record is on disk, not in memory."""
        st.create_user(User(login="rkoike"))
        store_mod.reset_for_tests()
        assert store_mod.store().get_user("rkoike") is not None


class TestRetirementProtectsTheTrail:
    """FR-2b. The requirement that stops the audit trail being wrong rather than incomplete."""

    def test_a_deleted_login_cannot_be_recreated(self, st):
        """AC-2a. Without this, a new `jdoe` inherits the departed `jdoe`'s history and grants."""
        st.create_user(User(login="jdoe"))
        st.delete_user("jdoe")
        with pytest.raises(IdentityError, match="cannot be reused"):
            st.create_user(User(login="jdoe"))

    def test_reuse_is_refused_across_case(self, st):
        st.create_user(User(login="jdoe"))
        st.delete_user("jdoe")
        with pytest.raises(IdentityError, match="cannot be reused"):
            st.create_user(User(login="JDoe"))

    def test_a_retired_login_stays_resolvable(self, st):
        """AC-2b. Retirement is not erasure: an old audit event naming `jdoe` must stay
        attributable, so the store still knows the login existed."""
        st.create_user(User(login="jdoe"))
        st.delete_user("jdoe")
        assert st.is_login_retired("jdoe") is True
        assert "jdoe" in st.retired_logins()

    def test_retirement_survives_a_restart(self, st):
        """The ledger is worthless if it is in memory — a restart would re-open reuse."""
        st.create_user(User(login="jdoe"))
        st.delete_user("jdoe")
        store_mod.reset_for_tests()
        with pytest.raises(IdentityError, match="cannot be reused"):
            store_mod.store().create_user(User(login="jdoe"))

    def test_deleting_a_user_removes_its_memberships(self, st):
        """A dangling edge is a grant pointing at nothing, and would be a grant pointing at the
        wrong person if reuse were ever permitted."""
        st.create_user(User(login="jdoe"))
        st.create_group(Group(name="operators"))
        st.add_member("operators", "jdoe")
        st.delete_user("jdoe")
        assert st.members_of("operators") == []

    def test_a_group_name_is_also_retired(self, st):
        """Same reasoning: a recycled group name silently re-grants what the old group had."""
        st.create_group(Group(name="operators"))
        st.delete_group("operators")
        with pytest.raises(IdentityError, match="cannot be reused"):
            st.create_group(Group(name="Operators"))


class TestMembership:
    def test_membership_requires_both_ends_to_exist(self, st):
        """An edge naming something that does not exist is a no-op grant waiting to become real
        when the name is created."""
        st.create_user(User(login="rkoike"))
        with pytest.raises(IdentityError, match="no such group"):
            st.add_member("nope", "rkoike")
        st.create_group(Group(name="operators"))
        with pytest.raises(IdentityError, match="no such user"):
            st.add_member("operators", "ghost")

    def test_adding_twice_is_idempotent(self, st):
        st.create_user(User(login="rkoike"))
        st.create_group(Group(name="operators"))
        st.add_member("operators", "rkoike")
        st.add_member("operators", "rkoike")
        assert st.members_of("operators") == ["rkoike"]

    def test_both_directions_of_the_edge_are_queryable(self, st):
        st.create_user(User(login="a"))
        st.create_user(User(login="b"))
        st.create_group(Group(name="operators"))
        st.add_member("operators", "a")
        st.add_member("operators", "b")
        assert st.members_of("operators") == ["a", "b"]
        assert st.groups_of("a") == ["operators"]

    def test_removing_a_member_leaves_the_user(self, st):
        st.create_user(User(login="a"))
        st.create_group(Group(name="operators"))
        st.add_member("operators", "a")
        st.remove_member("operators", "a")
        assert st.members_of("operators") == []
        assert st.get_user("a") is not None

    def test_deleting_a_group_removes_its_memberships_not_its_users(self, st):
        st.create_user(User(login="a"))
        st.create_group(Group(name="operators"))
        st.add_member("operators", "a")
        st.delete_group("operators")
        assert st.groups_of("a") == []
        assert st.get_user("a") is not None


class TestConcurrency:
    """NFR-5. The deployment runs one worker with several threads, so this is the real case."""

    def test_racing_creates_of_one_login_produce_exactly_one_user(self, st):
        """The failure this catches: a uniqueness check outside the lock lets both writers win, and
        it only shows up under load."""
        errors: list[Exception] = []
        start = threading.Barrier(8)

        def attempt():
            start.wait()
            try:
                st.create_user(User(login="contested"))
            except IdentityError as exc:
                errors.append(exc)

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert [u.login for u in st.list_users()] == ["contested"]
        assert len(errors) == 7, f"expected 7 refusals, got {len(errors)}"

    def test_concurrent_distinct_writes_all_land(self, st):
        """The other half: serialising must not *lose* writes. A last-writer-wins bug on the whole
        file would silently drop most of these."""
        start = threading.Barrier(12)

        def create(n: int):
            start.wait()
            st.create_user(User(login=f"user{n:02d}"))

        threads = [threading.Thread(target=create, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(st.list_users()) == 12

    def test_the_generation_counter_moves_on_write_and_not_on_read(self, st):
        """`identity.revocation` will cache on this, and Phase 1 showed why: a per-request store
        read cost 4.74 ms and breached the audit latency budget."""
        before = store_mod.current_generation()
        st.create_user(User(login="rkoike"))
        after_write = store_mod.current_generation()
        assert after_write > before
        st.get_user("rkoike")
        st.list_users()
        assert store_mod.current_generation() == after_write


class TestBootstrapPreconditions:
    def test_is_empty_is_about_users_not_the_file(self, st):
        """FR-10b: the upgrade case. A store file that exists with no users still needs the
        bootstrap, or upgrading from shared-token auth is a lockout."""
        assert st.is_empty() is True
        st.create_group(Group(name="operators"))
        assert st.is_empty() is True, "a group is not a user; the bootstrap is still owed"
        st.create_user(User(login="admin"))
        assert st.is_empty() is False

    def test_the_store_stays_owner_only_across_many_writes(self, st, tmp_path):
        """NFR-4, and the reason this asserts *repeatedly* rather than once.

        The storage layer writes to a `.tmp` and `os.replace`s it, so every write installs a new
        inode carrying the umask — typically `0o644`. A `chmod` at construction is therefore undone
        by the first save. This test caught exactly that: the file holding SRP verifiers was
        world-readable after one create. Checking after several writes is what makes it a guard
        against the real mechanism rather than against the symptom.
        """
        path = tmp_path / "identity.json"
        assert (path.stat().st_mode & 0o777) == 0o600, "wrong before any write"
        for i in range(5):
            st.create_user(User(login=f"user{i}"))
            assert (path.stat().st_mode & 0o777) == 0o600, f"world-readable after write {i + 1}"
        st.delete_user("user0")
        assert (path.stat().st_mode & 0o777) == 0o600, "world-readable after a delete"

    def test_the_parent_directory_is_owner_only_too(self, st, tmp_path):
        """A `0o600` file in a `0o755` directory still leaks its existence and its size."""
        st.create_user(User(login="admin"))
        assert (tmp_path.stat().st_mode & 0o777) == 0o700


class TestCredentialsAreNotExposed:
    def test_raw_contains_the_verifier_but_public_dict_does_not(self, st):
        """`raw()` feeds the encrypted export (FR-14), so it legitimately holds the material; the
        API shape must not."""
        st.create_user(User(login="rkoike", srp_salt="aabb", srp_verifier="ccdd"))
        assert "ccdd" in str(st.raw())
        assert "ccdd" not in str(st.get_user("rkoike").public_dict())
