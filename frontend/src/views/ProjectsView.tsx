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
import { useCallback, useEffect, useState } from "react";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import { Plus, Pencil, Trash2, CheckCircle2, Server, FolderKanban } from "lucide-react";
import {
  Banner,
  Button as KuiButton,
  Card,
  Flex,
  FormField,
  Modal,
  ModalCloseButton,
  Select,
  StatusMessage,
  Tag,
  Text,
  TextArea,
  TextInput,
} from "@nvidia/foundations-react-core";
import { PageHeader } from "../components/layout/PageHeader";
import { CONTENT_MAX_WIDTH } from "../components/layout/contentWidth";
import {
  deleteProject,
  getVocab,
  listPlatforms,
  saveProject,
} from "../services/catalogService";
import { useActiveCluster } from "../context/ActiveClusterContext";
import type { CatalogVocab, Cluster, Platform, Project } from "../types/catalog";
import { intOr } from "../lib/catalogForm";

function emptyCluster(): Cluster {
  return {
    id: "",
    name: "",
    platform_id: "",
    cluster_manager: "bcm",
    site_prefix: "",
    naming: { rack_id_template: "", ct_id_template: "", nvsw_id_template: "", ct_joiner: "-ct", slurm_hostlist: "" },
    geometry: { pods: 1, rows_per_pod: 0, racks_per_row: 0, trays_per_rack: 0 },
    connection: { exec_mode: "", ssh_host: "", ssh_user: "", ssh_key: "", remote_base_path: "" },
  };
}

function slugify(s: string): string {
  return s.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 63) || "item";
}

export function ProjectsView() {
  const { projects, active, setActive, refresh: refreshActive } = useActiveCluster();
  const [platforms, setPlatforms] = useState<Platform[]>([]);
  const [vocab, setVocab] = useState<CatalogVocab | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [projectDraft, setProjectDraft] = useState<Partial<Project> | null>(null);
  const [clusterDraft, setClusterDraft] = useState<{ projectId: string; cluster: Cluster; isNew: boolean } | null>(null);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      const [plats, voc] = await Promise.all([listPlatforms(), getVocab()]);
      setPlatforms(plats);
      setVocab(voc);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load reference data");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const platformName = (id: string) => platforms.find((p) => p.id === id)?.name || id || "—";

  const onSaveProject = async () => {
    if (!projectDraft) return;
    try {
      setSaving(true);
      const id = projectDraft.id || slugify(projectDraft.name || "");
      const existing = projects.find((p) => p.id === id);
      await saveProject({
        id,
        name: projectDraft.name,
        customer: projectDraft.customer,
        description: projectDraft.description,
        clusters: existing?.clusters || [],
      });
      setProjectDraft(null);
      await refreshActive();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to save project");
    } finally {
      setSaving(false);
    }
  };

  const onDeleteProject = async (p: Project) => {
    if (!window.confirm(`Delete project "${p.name}" and all its clusters?`)) return;
    try {
      await deleteProject(p.id);
      await refreshActive();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to delete project");
    }
  };

  const onSaveCluster = async () => {
    if (!clusterDraft) return;
    const project = projects.find((p) => p.id === clusterDraft.projectId);
    if (!project) return;
    try {
      setSaving(true);
      const cluster = { ...clusterDraft.cluster, id: clusterDraft.cluster.id || slugify(clusterDraft.cluster.name) };
      const clusters = clusterDraft.isNew
        ? [...project.clusters, cluster]
        : project.clusters.map((c) => (c.id === cluster.id ? cluster : c));
      await saveProject({ ...project, clusters });
      setClusterDraft(null);
      await refreshActive();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to save cluster");
    } finally {
      setSaving(false);
    }
  };

  const onDeleteCluster = async (project: Project, cluster: Cluster) => {
    if (!window.confirm(`Remove cluster "${cluster.name}" from "${project.name}"?`)) return;
    try {
      await saveProject({ ...project, clusters: project.clusters.filter((c) => c.id !== cluster.id) });
      await refreshActive();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to remove cluster");
    }
  };

  const setProj = (patch: Partial<Project>) => setProjectDraft((d) => (d ? { ...d, ...patch } : d));
  const setCl = (patch: Partial<Cluster>) =>
    setClusterDraft((d) => (d ? { ...d, cluster: { ...d.cluster, ...patch } } : d));
  const setNaming = (patch: Partial<Cluster["naming"]>) =>
    setClusterDraft((d) => (d ? { ...d, cluster: { ...d.cluster, naming: { ...d.cluster.naming, ...patch } } } : d));
  const setConn = (patch: Partial<Cluster["connection"]>) =>
    setClusterDraft((d) => (d ? { ...d, cluster: { ...d.cluster, connection: { ...d.cluster.connection, ...patch } } } : d));

  return (
    <Box sx={{ maxWidth: CONTENT_MAX_WIDTH.comfortable, mx: "auto", p: 4 }}>
      <PageHeader
        title="Projects & Clusters"
        description="Customer projects and their clusters: naming convention, geometry, platform, and connection. Set the active cluster to re-scope the whole app."
        contentId="projects"
        actions={
          <Button variant="contained" size="small" startIcon={<Plus size={16} />} onClick={() => setProjectDraft({ name: "", customer: "", description: "" })}>
            New project
          </Button>
        }
      />

      {error && (
        <Banner status="error" kind="inline" style={{ marginBottom: 16 }}>
          {error}
        </Banner>
      )}

      {projects.length === 0 ? (
        <Card style={{ padding: 32 }}>
          <StatusMessage
            size="small"
            slotMedia={<FolderKanban />}
            slotHeading="No projects yet"
            slotSubheading="Create a project, then add the clusters for that customer engagement."
          />
        </Card>
      ) : (
        <Flex direction="col" gap="4">
          {projects.map((p) => (
            <Card key={p.id} style={{ padding: 16 }}>
              <Flex justify="between" align="start" gap="2">
                <Box>
                  <Text kind="body/bold/md">{p.name}</Text>
                  {p.customer && (
                    <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", display: "block" }}>
                      Customer: {p.customer}
                    </Text>
                  )}
                  {p.description && (
                    <Text kind="body/regular/sm" style={{ display: "block", marginTop: 4 }}>{p.description}</Text>
                  )}
                </Box>
                <Flex gap="2">
                  <Button variant="outlined" size="small" startIcon={<Pencil size={14} />} onClick={() => setProjectDraft({ id: p.id, name: p.name, customer: p.customer, description: p.description })}>
                    Edit
                  </Button>
                  <Button variant="outlined" size="small" startIcon={<Plus size={14} />} onClick={() => setClusterDraft({ projectId: p.id, cluster: emptyCluster(), isNew: true })}>
                    Cluster
                  </Button>
                  <Button variant="outlined" color="error" size="small" startIcon={<Trash2 size={14} />} onClick={() => void onDeleteProject(p)}>
                    Delete
                  </Button>
                </Flex>
              </Flex>

              {p.clusters.length === 0 ? (
                <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", display: "block", marginTop: 12 }}>
                  No clusters yet — add one to make it selectable.
                </Text>
              ) : (
                <Box sx={{ mt: 2, display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))", gap: 1.5 }}>
                  {p.clusters.map((c) => {
                    const isActive = active.project === p.id && active.cluster === c.id;
                    return (
                      <Card key={c.id} style={{ padding: 12, border: isActive ? "1px solid var(--color-green-600, #4caf50)" : undefined }}>
                        <Flex align="center" gap="2">
                          <Server size={16} />
                          <Text kind="body/bold/sm">{c.name}</Text>
                          {isActive && (
                            <Tag color="green" kind="solid" readOnly density="compact">active</Tag>
                          )}
                        </Flex>
                        <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", display: "block", marginTop: 4 }}>
                          {platformName(c.platform_id)} · {c.cluster_manager}
                        </Text>
                        <Flex gap="2" style={{ marginTop: 10 }} wrap="wrap">
                          {!isActive && (
                            <Button variant="contained" size="small" startIcon={<CheckCircle2 size={14} />} onClick={() => void setActive(p.id, c.id)}>
                              Set active
                            </Button>
                          )}
                          <Button variant="outlined" size="small" startIcon={<Pencil size={14} />} onClick={() => setClusterDraft({ projectId: p.id, cluster: c, isNew: false })}>
                            Edit
                          </Button>
                          <Button variant="outlined" color="error" size="small" startIcon={<Trash2 size={14} />} onClick={() => void onDeleteCluster(p, c)}>
                            Remove
                          </Button>
                        </Flex>
                      </Card>
                    );
                  })}
                </Box>
              )}
            </Card>
          ))}
        </Flex>
      )}

      {/* Project editor */}
      <Modal
        open={projectDraft !== null}
        onOpenChange={(o) => {
          if (!o) setProjectDraft(null);
        }}
        modal
        style={{ width: "520px", maxWidth: "92vw" }}
        slotHeading={projectDraft?.id ? "Edit project" : "New project"}
        slotFooter={
          <>
            <ModalCloseButton kind="secondary">Cancel</ModalCloseButton>
            <KuiButton kind="primary" color="brand" disabled={saving} onClick={() => void onSaveProject()}>
              {saving ? "Saving…" : "Save"}
            </KuiButton>
          </>
        }
      >
        <Flex direction="col" gap="3">
          <FormField name="proj-name" slotLabel="Name">
            <TextInput value={projectDraft?.name || ""} onValueChange={(v) => setProj({ name: v })} placeholder="e.g. NSCALE USA" />
          </FormField>
          <FormField name="proj-customer" slotLabel="Customer">
            <TextInput value={projectDraft?.customer || ""} onValueChange={(v) => setProj({ customer: v })} placeholder="e.g. NSCALE" />
          </FormField>
          <FormField name="proj-description" slotLabel="Description">
            <TextArea value={projectDraft?.description || ""} onValueChange={(v) => setProj({ description: v })} resizeable="auto" placeholder="Optional notes about this engagement" />
          </FormField>
        </Flex>
      </Modal>

      {/* Cluster editor */}
      <Modal
        open={clusterDraft !== null}
        onOpenChange={(o) => {
          if (!o) setClusterDraft(null);
        }}
        modal
        style={{ width: "820px", maxWidth: "94vw" }}
        slotHeading={clusterDraft?.isNew ? "New cluster" : "Edit cluster"}
        slotFooter={
          <>
            <ModalCloseButton kind="secondary">Cancel</ModalCloseButton>
            <KuiButton kind="primary" color="brand" disabled={saving} onClick={() => void onSaveCluster()}>
              {saving ? "Saving…" : "Save"}
            </KuiButton>
          </>
        }
      >
        <Flex direction="col" gap="3">
          <Box sx={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 2 }}>
            <FormField name="cl-name" slotLabel="Name">
              <TextInput value={clusterDraft?.cluster.name || ""} onValueChange={(v) => setCl({ name: v })} placeholder="e.g. Pod A" />
            </FormField>
            <FormField name="cl-platform" slotLabel="Platform">
              <Select
                items={platforms.map((p) => ({ value: p.id, children: p.name }))}
                value={clusterDraft?.cluster.platform_id || ""}
                onValueChange={(v) => setCl({ platform_id: v })}
                placeholder="Select"
              />
            </FormField>
            <FormField name="cl-manager" slotLabel="Cluster manager">
              <Select items={vocab?.cluster_managers || ["bcm", "maas"]} value={clusterDraft?.cluster.cluster_manager || "bcm"} onValueChange={(v) => setCl({ cluster_manager: v })} placeholder="Select" />
            </FormField>
            <FormField name="cl-siteprefix" slotLabel="Site prefix" slotHelp="Used for multirack hostname inference">
              <TextInput value={clusterDraft?.cluster.site_prefix || ""} onValueChange={(v) => setCl({ site_prefix: v })} placeholder="optional" />
            </FormField>
          </Box>

          <Text kind="body/bold/sm" style={{ display: "block", marginTop: 4 }}>Naming convention</Text>
          <Box sx={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 2 }}>
            <FormField name="cl-rack-tpl" slotLabel="Rack id template" slotHelp="NVIS_RACK_ID">
              <TextInput value={clusterDraft?.cluster.naming.rack_id_template || ""} onValueChange={(v) => setNaming({ rack_id_template: v })} />
            </FormField>
            <FormField name="cl-ct-tpl" slotLabel="Node/CT id template" slotHelp="NVIS_CT_ID">
              <TextInput value={clusterDraft?.cluster.naming.ct_id_template || ""} onValueChange={(v) => setNaming({ ct_id_template: v })} />
            </FormField>
            <FormField name="cl-nvsw-tpl" slotLabel="NVSwitch id template" slotHelp="NVIS_NVSW_ID">
              <TextInput value={clusterDraft?.cluster.naming.nvsw_id_template || ""} onValueChange={(v) => setNaming({ nvsw_id_template: v })} />
            </FormField>
            <FormField name="cl-joiner" slotLabel="Tray joiner">
              <TextInput value={clusterDraft?.cluster.naming.ct_joiner || "-ct"} onValueChange={(v) => setNaming({ ct_joiner: v })} />
            </FormField>
          </Box>

          <Text kind="body/bold/sm" style={{ display: "block", marginTop: 4 }}>Connection</Text>
          <Box sx={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 2 }}>
            <FormField name="cl-execmode" slotLabel="Exec mode">
              <Select
                items={[
                  { value: "", children: "(default)" },
                  { value: "local", children: "local" },
                  { value: "ssh", children: "ssh" },
                ]}
                value={clusterDraft?.cluster.connection.exec_mode || ""}
                onValueChange={(v) => setConn({ exec_mode: v })}
                placeholder="(default)"
              />
            </FormField>
            <FormField name="cl-sshhost" slotLabel="SSH host">
              <TextInput value={clusterDraft?.cluster.connection.ssh_host || ""} onValueChange={(v) => setConn({ ssh_host: v })} />
            </FormField>
            <FormField name="cl-sshuser" slotLabel="SSH user">
              <TextInput value={clusterDraft?.cluster.connection.ssh_user || ""} onValueChange={(v) => setConn({ ssh_user: v })} />
            </FormField>
            <FormField name="cl-sshkey" slotLabel="SSH key path">
              <TextInput value={clusterDraft?.cluster.connection.ssh_key || ""} onValueChange={(v) => setConn({ ssh_key: v })} />
            </FormField>
            <Box sx={{ gridColumn: "span 2" }}>
              <FormField name="cl-remotebase" slotLabel="Remote base path">
                <TextInput value={clusterDraft?.cluster.connection.remote_base_path || ""} onValueChange={(v) => setConn({ remote_base_path: v })} />
              </FormField>
            </Box>
          </Box>

          <Text kind="body/bold/sm" style={{ display: "block", marginTop: 4 }}>Site geometry</Text>
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", display: "block", marginTop: -4 }}>
            0 = inherit from platform.
          </Text>
          <Box sx={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 1.5 }}>
            {([
              ["pods", "Pods"],
              ["rows_per_pod", "Rows/pod"],
              ["racks_per_row", "Racks/row"],
              ["trays_per_rack", "Trays/rack"],
            ] as const).map(([key, label]) => (
              <FormField key={key} name={`cl-geo-${key}`} slotLabel={label}>
                <TextInput
                  inputMode="numeric"
                  value={(clusterDraft?.cluster.geometry[key] ?? 0).toString()}
                  onValueChange={(v) => setCl({ geometry: { ...(clusterDraft as { cluster: Cluster }).cluster.geometry, [key]: intOr(v, 0) } })}
                />
              </FormField>
            ))}
          </Box>
        </Flex>
      </Modal>
    </Box>
  );
}
