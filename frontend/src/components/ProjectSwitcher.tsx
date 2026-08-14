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
import { useMemo } from "react";
import { Combobox } from "@nvidia/foundations-react-core";
import { useActiveCluster } from "../context/ActiveClusterContext";

const SEP = "::";

interface ProjectSwitcherProps {
  /** Called after the active cluster is switched, so the app can refresh data. */
  onSwitched?: () => void;
}

/**
 * Top-bar selector for the active project/cluster. Switching is server-authoritative
 * (PUT /api/catalog/active) so all downstream APIs re-scope to the chosen cluster.
 */
export function ProjectSwitcher({ onSwitched }: ProjectSwitcherProps) {
  const { projects, active, setActive, loading } = useActiveCluster();

  const items = useMemo(
    () =>
      projects.flatMap((p) =>
        (p.clusters || []).map((c) => ({
          value: `${p.id}${SEP}${c.id}`,
          children: projects.length > 1 ? `${p.name} / ${c.name}` : c.name,
        })),
      ),
    [projects],
  );

  const value = active.project && active.cluster ? `${active.project}${SEP}${active.cluster}` : "";

  if (loading && items.length === 0) return null;
  if (items.length === 0) return null; // no clusters to show at all

  return (
    <Combobox
      items={items}
      value={value}
      size="small"
      aria-label="Active project and cluster"
      style={{ width: "100%" }}
      onValueChange={(v: string | string[]) => {
        const next = Array.isArray(v) ? v[0] : v;
        if (!next || next === value) return;
        const [projectId, clusterId] = next.split(SEP);
        void setActive(projectId, clusterId).then(() => onSwitched?.());
      }}
    />
  );
}
