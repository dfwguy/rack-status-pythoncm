# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Exclusive, fsynced submission evidence stored in each existing attempt directory."""
import fcntl
import hashlib
import json
import os
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path

from clusterkit_artifacts import directory, read_json, relative_parts


def write_record(root, name, value):
    parts = relative_parts(name)
    if len(parts) != 1:
        raise ValueError('Submission records must be direct attempt children')
    data = (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()
    with directory(root) as parent:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o640, dir_fd=parent)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.fsync(parent)


def load_intent(root, activity_id=None):
    intent = read_json(root, 'submission-intent.json')
    plan = read_json(root, 'plan.json')
    if not isinstance(intent, dict) or intent.get('schema_version') != 1 or not isinstance(plan, dict):
        raise ValueError('Invalid durable submission intent')
    claimed_hash = plan.get('plan_hash')
    body = {k: v for k, v in plan.items() if k != 'plan_hash'}
    if hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest() != claimed_hash:
        raise ValueError('Corrupt stored plan')
    if (intent.get('plan_hash') != claimed_hash or intent.get('attempt_id') != plan.get('attempt_id')
            or str(Path(root)) != plan.get('results_directory')
            or intent.get('activity_id') != str(uuid.UUID(plan['attempt_id']))
            or (activity_id and intent['activity_id'] != activity_id)):
        raise ValueError('Submission intent/plan/activity identity mismatch')
    return intent, plan


def receipt(root, intent):
    try:
        value = read_json(root, 'submission-receipt.json')
    except FileNotFoundError:
        return None
    if (not isinstance(value, dict) or value.get('attempt_id') != intent['attempt_id']
            or value.get('context') != intent['context']
            or not isinstance(value.get('job_id'), str) or not value['job_id'].isdigit()
            or int(value['job_id']) <= 0):
        raise ValueError('Invalid submission receipt')
    return value


class SubmissionBusyError(RuntimeError):
    """Another submission decision is in progress; no scheduler request was made."""


@contextmanager
def submission_lock(root):
    """Serialize claim/submission decisions across processes, never during preview."""
    with directory(root) as parent:
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            fd = os.open('.clusterkit-submission.lock', flags | os.O_CREAT | os.O_EXCL,
                         0o640, dir_fd=parent)
        except FileExistsError:
            fd = os.open('.clusterkit-submission.lock', flags, dir_fd=parent)
        locked = False
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError('Submission lock must be a regular file')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SubmissionBusyError("Another submission is in progress; inspect its status before trying again") from exc
            locked = True
            yield
        finally:
            if locked:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def reject_unresolved_overlap(plan, context):
    """A new token must not duplicate work whose prior scheduler acceptance is unknown."""
    root = Path(plan['effective_profile']['results_root'])
    with directory(root) as fd, os.scandir(fd) as entries:
        for index, item in enumerate(entries):
            if index >= 512:
                raise ValueError('Submission safety scan exceeds 512 entries; operator review required')
            if not item.is_dir(follow_symlinks=False):
                continue
            candidate = root / item.name
            try:
                intent = read_json(candidate, 'submission-intent.json')
            except FileNotFoundError:
                continue
            if not isinstance(intent, dict) or intent.get('context') != context:
                continue
            try:
                read_json(candidate, 'submission-started.json')
            except FileNotFoundError:
                continue
            previous_intent, previous = load_intent(candidate)
            # Corrupt/partial receipts also fail closed, without replacing evidence.
            if set(previous['nodes']) & set(plan['nodes']) and receipt(candidate, previous_intent) is None:
                raise ValueError('An overlapping attempt has SUBMISSION_UNKNOWN; reconcile it before requesting another run')
