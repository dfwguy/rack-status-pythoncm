# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Immutable, non-secret execution identity; remote staging is explicitly unsupported."""
import copy
import os
import pwd
import socket
from pathlib import Path

from platforms import get_platform
from transport.base import Connection
from transport.local import LocalExecutor

COMMANDS = ('sbatch', 'sinfo', 'squeue', 'sacct', 'scontrol', 'scancel')
MARKER = '__RACK_CLUSTERKIT_ARGS__'
SETTINGS = ('RACK_PLATFORM', 'RACK_EXEC_MODE', 'RACK_CLUSTER_NAME',
            'RACK_SLURM_MODULE', 'RACK_SLURM_SKIP_MODULE_LOAD', 'RACK_SLURM_CONF',
            *(f'RACK_SLURM_{name.upper()}' for name in COMMANDS))


def capture(scripts_dir, connection=None, platform=None):
    from config_env import _CATALOG_AVAILABLE, catalog_context, read_only_values
    values = read_only_values(SETTINGS)
    if connection is not None:
        if connection.kind != 'local':
            raise ValueError('ClusterKit backend currently supports local execution only; remote staging is not implemented')
        executor = connection.executor
        from transport.compose import EnvForwardingExecutor
        while isinstance(executor, EnvForwardingExecutor):
            executor = executor.inner
        if not isinstance(executor, LocalExecutor):
            raise ValueError('ClusterKit requires an unprivileged local execution context')
    if connection is None and values.get('RACK_EXEC_MODE') not in (None, '', 'local'):
        raise ValueError('Configured remote execution is unsupported for modular ClusterKit')
    selection = {'project': '', 'cluster': ''}
    if _CATALOG_AVAILABLE:
        from catalog.store import read_only_repo
        selection = catalog_context.get_active(repo=read_only_repo())
    provider = copy.copy(platform or get_platform())
    expected = (values.get('RACK_PLATFORM') or '').strip().lower()
    if expected and type(provider).__module__.rsplit('.',1)[-1] != expected:
        raise ValueError('Active platform differs from the resolved cluster configuration')
    provider._slurm_settings = {key: value or '' for key, value in values.items()}
    return {'schema_version': 1, 'kind': 'local', 'hostname': socket.gethostname(),
            'uid': os.geteuid(), 'user': pwd.getpwuid(os.geteuid()).pw_name,
            'selection': selection, 'cluster_name': values.get('RACK_CLUSTER_NAME') or '',
            'platform': type(provider).__module__ + '.' + type(provider).__qualname__,
            'scripts_directory': str(Path(scripts_dir).resolve()),
            'slurm_environment': {key: os.environ.get(key, '') for key in ('SLURM_CONF', 'SLURM_CLUSTERS')},
            'commands': {name: getattr(provider, name + '_cmd')(MARKER) for name in COMMANDS}}


def require_same(expected, current):
    if not isinstance(expected, dict) or expected != current:
        raise ValueError('Execution context changed; select the original cluster/user/configuration before accessing this run')


class PinnedPlatform:
    def __init__(self, context):
        self.context = context

    def __getattr__(self, name):
        if name.endswith('_cmd') and name[:-4] in COMMANDS:
            template = self.context['commands'][name[:-4]]
            if template.count(MARKER) != 1:
                raise ValueError('Invalid pinned Slurm command')
            return lambda args='': template.replace(MARKER, args)
        raise AttributeError(name)


class PinnedLocalConnection(Connection):
    """Use the existing local transport with frozen Slurm selection and no SBATCH overrides."""
    def __init__(self, context):
        super().__init__(LocalExecutor())
        self.context = context

    def run(self, cmd, **kwargs):
        environment = {k: v for k, v in os.environ.items()
                       if not k.startswith('SBATCH_') and k not in {'SLURM_CONF', 'SLURM_CLUSTERS'}}
        environment.update({k: v for k, v in self.context['slurm_environment'].items() if v})
        kwargs['env'] = environment
        kwargs['login_shell'] = False
        return super().run(cmd, **kwargs)
