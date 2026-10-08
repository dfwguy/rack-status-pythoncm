# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# shellcheck shell=bash
# lib/node_config.inc.sh — settings Rack Status publishes for the launch host and compute nodes.
#
# Rack Status projects facts that live only in its store (first: the P2P image catalog) as files
# in the config/ directory of the scripts a project runs (backend/node_config.py,
# bin/scripts/config/README.md): project-<id>/config once a project is bootstrapped, otherwise the
# platform layer's or multi-platform's. That is always the directory beside this lib/, on the
# shared /home, so the head and every node read the same files and nothing is configured twice.
# NVIS_NODE_CONFIG_DIR overrides it (tests, or relocated scripts).

_NVIS_NODE_CONFIG_SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Print the config directory of this scripts copy.
nvis_node_config_dir() {
  printf '%s\n' "${NVIS_NODE_CONFIG_DIR:-${_NVIS_NODE_CONFIG_SCRIPTS_DIR}/config}"
}

# nvis_node_config_file <name> — print the path of one published file; non-zero if it is absent.
nvis_node_config_file() {
  local path
  path="$(nvis_node_config_dir)/$1"
  printf '%s\n' "$path"
  [[ -r "$path" ]]
}
