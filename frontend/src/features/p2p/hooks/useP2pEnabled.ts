/*
 * SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
 * SPDX-License-Identifier: LicenseRef-NvidiaProprietary
 *
 * NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
 * property and proprietary rights in and to this material, related
 * documentation and any modifications thereto. Any use, reproduction,
 * disclosure or distribution of this material and related documentation
 * without an express license agreement from NVIDIA CORPORATION or
 * its affiliates is strictly prohibited.
 */
// Author: Rafael M. Koike
//
// Whether the P2P (artifact distribution) capability is enabled for the active
// cluster. P2P is single-tenant -- one Dragonfly instance per cluster -- so it
// is a per-project/per-cluster feature, not a global one, and the nav group is
// scoped to the active cluster the same way scheduler surfaces are.
//
// The authoritative flag is `Cluster.p2p_enabled`, set per cluster from the Edit
// Cluster dialog (Projects & Clusters). A local dev override remains as a fallback
// only when no cluster is active, so the group can still be exercised without a
// catalog entry:
//
//   localStorage.setItem("nvis-p2p-enabled", "1")   // then reload
import { useActiveCluster } from "../../../context/ActiveClusterContext";

const DEV_OVERRIDE_KEY = "nvis-p2p-enabled";

/** True when the active cluster has the P2P capability turned on. */
export function useP2pEnabled(): boolean {
  const { activeCluster } = useActiveCluster();
  if (activeCluster) return Boolean(activeCluster.p2p_enabled);
  // No active cluster yet: honour the dev override so the group can be revealed for testing.
  try {
    return localStorage.getItem(DEV_OVERRIDE_KEY) === "1";
  } catch {
    return false;
  }
}
