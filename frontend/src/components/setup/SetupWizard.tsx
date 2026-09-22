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
/**
 * SetupWizard — six steps from a fresh install to a configured cluster.
 * Author: Rafael M. Koike & Cursor Agent ("Ada")
 *
 * A first boot creates an administrator, a policy and a certificate, then lands the operator on
 * a dashboard describing a cluster nobody configured. This fills that gap.
 *
 * Two choices worth knowing before editing:
 *
 * **It creates the project through the ordinary catalog endpoints.** The finish sequence is
 * `saveProject` → `setActiveContext` → bootstrap scripts → `completeSetup`, so there is one
 * code path that writes a Project document and one place its schema is understood. A dedicated
 * "create from wizard" endpoint would be a second one, used once per deployment and therefore
 * never noticed when it drifts.
 *
 * **The naming step proposes and proves; it never asserts.** One hostname cannot say whether
 * `-s1-dgx-` is fixed text or data, so the operator is shown each candidate's *parsed* rack and
 * tray — against the hostname they typed — and picks the reading they recognise. Every field
 * stays editable, because a site that matches none of the proposals must still be able to
 * finish.
 */
import { useCallback, useEffect, useState } from "react";
import Box from "@mui/material/Box";
import {
  Banner,
  Button,
  Card,
  Checkbox,
  Divider,
  Flex,
  FormField,
  RadioGroup,
  Select,
  StepperItem,
  StepperRoot,
  Text,
  TextArea,
  TextInput,
} from "@nvidia/foundations-react-core";
import { ArrowLeft, ArrowRight, CheckCircle2, Wand2 } from "lucide-react";
import { listPlatforms, saveProject, setActiveContext } from "../../services/catalogService";
import {
  bootstrapProjectScripts,
  completeSetup,
  deployScripts,
  previewScriptDeploy,
  proposeNaming,
  storeSshKey,
  type NamingCandidate,
  type ScriptDeployResult,
} from "../../services/setupService";
import type { Platform } from "../../types/catalog";
import {
  STEP_DESCRIPTIONS,
  STEP_TITLES,
  clusterId,
  emptyDraft,
  projectId,
  stepProblems,
  toProject,
  type WizardDraft,
} from "./wizardDraft";

const LAST_STEP = STEP_TITLES.length - 1;

const subtle = { color: "var(--text-color-secondary)", display: "block" } as const;

export interface SetupWizardProps {
  /** Shown when the wizard is reached from the Projects screen rather than at first login. */
  onCancel?: () => void;
  /** Called once the project exists and is active. */
  onFinished?: (projectId: string) => void;
}

export function SetupWizard({ onCancel, onFinished }: SetupWizardProps) {
  const [step, setStep] = useState(0);
  const [draft, setDraft] = useState<WizardDraft>(emptyDraft);
  const [platforms, setPlatforms] = useState<Platform[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");

  // Naming helper state.
  const [candidates, setCandidates] = useState<NamingCandidate[]>([]);
  const [caveat, setCaveat] = useState("");
  const [chosen, setChosen] = useState(-1);

  // Key material lives here and nowhere else: it is posted, exchanged for a path, and dropped.
  const [keyMaterial, setKeyMaterial] = useState("");
  const [keyFingerprint, setKeyFingerprint] = useState("");

  const [scriptResult, setScriptResult] = useState("");
  // How much a deploy would send. Fetched once, never blocking: it is a detail on a
  // checkbox's help line, so a deployment that cannot answer just shows one line less.
  const [deployPreview, setDeployPreview] = useState<ScriptDeployResult | null>(null);

  const set = useCallback(
    (patch: Partial<WizardDraft>) => setDraft((d) => ({ ...d, ...patch })),
    [],
  );

  useEffect(() => {
    listPlatforms()
      .then(setPlatforms)
      .catch(() => setPlatforms([]));
    previewScriptDeploy()
      .then(setDeployPreview)
      .catch(() => setDeployPreview(null));
  }, []);

  const problems = stepProblems(step, draft);
  const pid = projectId(draft);

  /**
   * Choosing a platform prefills the geometry that platform already knows.
   *
   * Done in the event rather than in an effect watching `platformId`: the choice is what causes
   * the prefill, so syncing it afterwards is a second render that can also fight the operator\'s
   * own typing. Prefilled and not imposed — a site runs a partly populated rack often enough
   * that a fixed value would be wrong more often than a blank one — so an existing answer wins.
   */
  const onPlatformChosen = (id: string) => {
    const picked = platforms.find((p) => p.id === id);
    setDraft((d) => ({
      ...d,
      platformId: id,
      traysPerRack: d.traysPerRack || String(picked?.geometry.trays_per_rack || ""),
    }));
  };

  const applyCandidate = (candidate: NamingCandidate, index: number) => {
    setChosen(index);
    setDraft((d) => ({
      ...d,
      rackIdTemplate: candidate.rack_id_template,
      ctIdTemplate: candidate.ct_id_template,
      nvswIdTemplate: candidate.nvsw_id_template,
      ctJoiner: candidate.ct_joiner || d.ctJoiner,
    }));
  };

  const onPropose = async () => {
    setError("");
    setBusy("naming");
    try {
      const result = await proposeNaming(draft.exampleHostname);
      setCandidates(result.candidates || []);
      setCaveat(result.caveat || "");
      setChosen(-1);
      if (result.candidates?.length) applyCandidate(result.candidates[0], 0);
    } catch (e) {
      setCandidates([]);
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("");
    }
  };

  const onStoreKey = async () => {
    setError("");
    setBusy("key");
    try {
      const { path, fingerprint } = await storeSshKey(pid, clusterId(draft), keyMaterial);
      set({ sshKeyPath: path });
      setKeyFingerprint(fingerprint);
      // Dropped as soon as it has a home. Holding it would keep a private key in a React state
      // tree for the rest of the session, where every error boundary and dev tool can read it.
      setKeyMaterial("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("");
    }
  };

  const onKeyFile = async (file: File | null | undefined) => {
    if (!file) return;
    setKeyMaterial(await file.text());
  };

  const onFinish = async () => {
    setError("");
    setBusy("finish");
    try {
      const project = toProject(draft);
      await saveProject(project);
      await setActiveContext({ project: project.id, cluster: project.clusters[0].id });

      // Deploy before bootstrap, always: the project's copy is composed from the shared
      // layers this sends. Both are reported together so a partial result names which half
      // worked -- "scripts failed" with two mechanisms behind it is not a usable answer.
      const done: string[] = [];
      try {
        if (draft.deployScripts) {
          setBusy("deploy");
          const sent = await deployScripts();
          done.push(`sent ${sent.files} shipped file(s) to ${sent.dest}`);
        }
        if (draft.bootstrapScripts) {
          setBusy("finish");
          const result = await bootstrapProjectScripts(project.id, false, project.clusters[0].id);
          done.push(`copied ${result.copied} file(s) into ${result.dest}`);
        }
        setScriptResult(done.join("; "));
      } catch (e) {
        // The project exists and is active; only the scripts failed, and both halves are
        // repeatable from the Projects screen. Failing the whole wizard here would throw
        // away everything the operator just entered for a step that can be retried.
        setScriptResult(done.join("; "));
        setError(
          `The project was created, but its scripts are not fully in place: ${
            e instanceof Error ? e.message : String(e)
          }.${done.length ? ` Completed first: ${done.join("; ")}.` : ""} Use "Deploy scripts to head"` +
            ` and "Update scripts" on the Projects screen once the connection works.`,
        );
      }

      await completeSetup(project.id);
      onFinished?.(project.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("");
    }
  };

  const text = (
    name: string,
    label: string,
    key: keyof WizardDraft,
    placeholder = "",
    help = "",
  ) => (
    <FormField name={name} slotLabel={label} slotHelp={help || undefined}>
      <TextInput
        value={String(draft[key] ?? "")}
        placeholder={placeholder}
        onValueChange={(v: string) => set({ [key]: v } as Partial<WizardDraft>)}
      />
    </FormField>
  );

  const stepBody = () => {
    switch (step) {
      case 0:
        return (
          <Flex direction="col" gap="3">
            {text("w-name", "Project name", "projectName", "Nscale TX", "Shown in the cluster switcher.")}
            {text("w-customer", "Customer or site", "customer", "Nscale")}
            <FormField name="w-desc" slotLabel="Description" slotHelp="Optional.">
              <TextArea
                value={draft.description}
                resizeable="auto"
                onValueChange={(v: string) => set({ description: v })}
              />
            </FormField>
            {pid ? (
              <Text kind="mono/sm" style={subtle}>
                id: {pid}
              </Text>
            ) : null}
          </Flex>
        );

      case 1:
        return (
          <Flex direction="col" gap="3">
            <FormField
              name="w-platform"
              slotLabel="Hardware platform"
              slotHelp="Supplies rack geometry and per-tool defaults."
            >
              <Select
                items={platforms.map((p) => ({ value: p.id, children: `${p.name} (${p.gpu_model})` }))}
                value={draft.platformId}
                placeholder="Select a platform"
                onValueChange={(v: string) => onPlatformChosen(v)}
              />
            </FormField>
            {/* Two options, so both are on screen. KUI's guidance is RadioGroup below five:
                hiding a binary choice behind a dropdown makes the operator open it to find out
                what the alternative even is. */}
            <FormField
              name="w-manager"
              slotLabel="Cluster manager"
              slotHelp="What answers node inventory: BCM's cmsh, or MAAS."
            >
              <RadioGroup
                name="w-manager"
                value={draft.clusterManager}
                onValueChange={(v: string) => set({ clusterManager: v })}
                items={[
                  { value: "bcm", children: "BCM (Base Command Manager)" },
                  { value: "maas", children: "MAAS" },
                ]}
              />
            </FormField>
            {text("w-cluster", "Cluster name", "clusterName", draft.projectName || "Primary")}
            {text(
              "w-prefix",
              "Site prefix",
              "sitePrefix",
              "ink50",
              "Optional. The leading token in this site's rack ids; used to group multirack launches.",
            )}
          </Flex>
        );

      case 2:
        return (
          <Flex direction="col" gap="3">
            <Text kind="body/regular/sm" style={subtle}>
              Nodes are discovered from cluster inventory without requiring rack assignments or
              naming templates. Leave both templates blank to keep existing naming settings, or
              provide both to configure rack grouping. To derive templates, enter a known compute
              node hostname and check the proposed rack and tray below.
            </Text>
            <Flex gap="2" align="end">
              <Box sx={{ flex: 1 }}>
                <FormField name="w-example" slotLabel="Example compute node hostname">
                  <TextInput
                    value={draft.exampleHostname}
                    placeholder="ink50-dh01-h15-15"
                    onValueChange={(v: string) => set({ exampleHostname: v })}
                  />
                </FormField>
              </Box>
              <Button
                kind="secondary"
                onClick={() => void onPropose()}
                disabled={!draft.exampleHostname.trim() || busy === "naming"}
              >
                <Wand2 size={14} style={{ marginRight: 6 }} />
                Derive
              </Button>
            </Flex>

            {caveat ? <Banner status="info">{caveat}</Banner> : null}

            {candidates.map((c, i) => (
              <Card
                key={`${c.rack_id_template}|${c.ct_id_template}`}
                style={{
                  padding: 12,
                  cursor: "pointer",
                  border:
                    chosen === i
                      ? "2px solid var(--kui-color-border-selected, #76b900)"
                      : "1px solid var(--kui-color-border-subtle, #444)",
                }}
                onClick={() => applyCandidate(c, i)}
              >
                <Flex justify="between" align="center" gap="2">
                  <Flex direction="col" gap="1">
                    {/* The parse first: it is the thing an operator can actually check. */}
                    <Text kind="body/bold/sm">
                      rack {c.parsed.rack_id} · tray {c.parsed.tray}
                    </Text>
                    <Text kind="mono/sm" style={subtle}>
                      {c.ct_id_template}
                    </Text>
                  </Flex>
                  {chosen === i ? <CheckCircle2 size={16} /> : null}
                </Flex>
              </Card>
            ))}

            <Divider />
            {text("w-rack-t", "Rack id template", "rackIdTemplate", "ink[01-99]-dh[01-99]-[a-z][01-99]", "Optional; provide both rack and compute node templates to configure grouping.")}
            {text("w-ct-t", "Compute node template", "ctIdTemplate", "", "Optional; required only when a rack template is provided.")}
            {text("w-nvsw-t", "NVSwitch template", "nvswIdTemplate")}
            <Flex gap="2">
              {text("w-pods", "Pods", "pods")}
              {text("w-rows", "Rows per pod", "rowsPerPod")}
              {text("w-racks", "Racks per row", "racksPerRow")}
              {text("w-trays", "Trays per rack", "traysPerRack", "", "Required with naming templates; otherwise optional. Leave blank to use the platform default.")}
            </Flex>
          </Flex>
        );

      case 3:
        return (
          <Flex direction="col" gap="3">
            <FormField
              name="w-exec"
              slotLabel="Execution mode"
              slotHelp="Where scripts run: this container, or the cluster's head node over SSH."
            >
              {/* This choice decides whether the next four fields apply at all, so it is worth
                  the space: a dropdown would hide the reason the form changes shape. */}
              <RadioGroup
                name="w-exec"
                value={draft.execMode}
                onValueChange={(v: string) => set({ execMode: v })}
                items={[
                  { value: "ssh", children: "SSH to the head node" },
                  { value: "local", children: "Local (this container runs the scripts)" },
                ]}
              />
            </FormField>
            {draft.execMode === "ssh" ? (
              <>
                {text("w-host", "SSH host", "sshHost", "10.29.136.8")}
                {text("w-user", "SSH user", "sshUser", "root")}
                <FormField
                  name="w-key"
                  slotLabel="SSH private key"
                  slotHelp={
                    draft.sshKeyPath
                      ? `Stored at ${draft.sshKeyPath}${keyFingerprint ? ` (${keyFingerprint})` : ""}`
                      : "Paste the private key or choose the key file. It is written owner-only on the server; only its path is stored with the cluster."
                  }
                >
                  <Flex direction="col" gap="2">
                    <TextArea
                      value={keyMaterial}
                      resizeable="auto"
                      placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"
                      onValueChange={(v: string) => setKeyMaterial(v)}
                    />
                    <Flex gap="2" align="center">
                      <input
                        type="file"
                        aria-label="SSH key file"
                        onChange={(e) => void onKeyFile(e.target.files?.[0])}
                      />
                      <Button
                        kind="secondary"
                        size="small"
                        disabled={!keyMaterial.trim() || busy === "key"}
                        onClick={() => void onStoreKey()}
                      >
                        Store key
                      </Button>
                    </Flex>
                  </Flex>
                </FormField>
              </>
            ) : null}
            {text(
              "w-base",
              "Remote base path",
              "remoteBasePath",
              "/home/cmsupport/nvis-rack-status",
              "Where the scripts and logs live on the head node.",
            )}
            {text("w-logs", "Log directory", "logDir", "", "Optional; defaults to <base>/var/logs.")}
          </Flex>
        );

      case 4:
        return (
          <Flex direction="col" gap="3">
            <Text kind="body/regular/sm" style={subtle}>
              Greenlight runs its work from scripts on the head node, in two layers. The shared
              set ships with this release; each project then gets its own copy of it, so you can
              fix a script for this cluster without changing it for every other project here.
            </Text>

            {/* Checkboxes rather than a Switch: these are values submitted when the wizard
                finishes, not settings that take effect the moment they are clicked. */}
            <Checkbox
              slotLabel="Send the shipped scripts to the head node"
              checked={draft.deployScripts}
              onCheckedChange={(checked) => set({ deployScripts: checked === true })}
            />
            <Text kind="mono/sm" style={subtle}>
              {draft.remoteBasePath || "<base>"}/scripts/multi-platform
              {deployPreview ? `  ·  ${deployPreview.files} file(s)` : ""}
            </Text>
            <Text kind="body/regular/sm" style={subtle}>
              Needed on a head node that has not had a Greenlight release deployed to it. Your
              own scripts under <code>custom/</code> are never sent, so nothing you added there
              can be overwritten.
            </Text>

            <Divider />

            <Checkbox
              slotLabel="Copy the scripts into this project's directory"
              checked={draft.bootstrapScripts}
              onCheckedChange={(checked) => set({ bootstrapScripts: checked === true })}
            />
            <Text kind="mono/sm" style={subtle}>
              {draft.remoteBasePath || "<base>"}/scripts/project-{pid || "<project>"}
            </Text>

            {/* The order is not a preference. A project's copy is composed *from* the shared
                layers, so on a head that has never had a deploy there is nothing to compose
                from -- which is the failure this step was added to prevent. */}
            {draft.bootstrapScripts && !draft.deployScripts ? (
              <Banner status="warning">
                This project&apos;s copy is built from the shared scripts. If the head node has
                never had them, copying will fail with nothing to copy — leave the step above
                on unless you know they are already there.
              </Banner>
            ) : (
              <Banner status="info">
                Both steps are safe to repeat: existing files are left as they are, so running
                this again after you edit a script keeps your change.
              </Banner>
            )}
          </Flex>
        );

      default: {
        const project = toProject(draft);
        const cluster = project.clusters[0];
        const row = (label: string, value: string) => (
          <Flex justify="between" gap="3" key={label}>
            <Text kind="body/regular/sm" style={subtle}>
              {label}
            </Text>
            <Text kind="body/regular/sm">{value || "—"}</Text>
          </Flex>
        );
        return (
          <Flex direction="col" gap="2">
            {row("Project", `${project.name} (${project.id})`)}
            {row("Customer", project.customer)}
            {row("Cluster", `${cluster.name} (${cluster.id})`)}
            {row("Platform", cluster.platform_id)}
            {row("Cluster manager", cluster.cluster_manager)}
            {row("Rack template", cluster.naming.rack_id_template || "Use existing settings")}
            {row("Node template", cluster.naming.ct_id_template || "Use existing settings")}
            {row("Trays per rack", cluster.geometry.trays_per_rack > 0 ? String(cluster.geometry.trays_per_rack) : "Platform default")}
            {row(
              "Connection",
              cluster.connection.exec_mode === "ssh"
                ? `${cluster.connection.ssh_user}@${cluster.connection.ssh_host}`
                : "local",
            )}
            {row("SSH key", cluster.connection.ssh_key)}
            {row("Remote base", cluster.connection.remote_base_path)}
            {row(
              "Scripts",
              [
                draft.deployScripts ? "send to head" : "",
                draft.bootstrapScripts ? "copy into project" : "",
              ]
                .filter(Boolean)
                .join(", ") || "skip",
            )}
            {scriptResult ? row("Scripts done", scriptResult) : null}
          </Flex>
        );
      }
    }
  };

  return (
    <Flex direction="col" gap="4" style={{ maxWidth: 820, margin: "0 auto", padding: 24 }}>
      <Flex direction="col" gap="1">
        <Text kind="display/sm">Set up Greenlight</Text>
        <Text kind="body/regular/md" style={subtle}>
          Describe the cluster once. Everything after this reads from it.
        </Text>
      </Flex>

      <StepperRoot activeStep={step} layout="horizontal" aria-label="Setup progress">
        {STEP_TITLES.map((title, i) => (
          <StepperItem key={title} value={i} slotHeading={title} />
        ))}
      </StepperRoot>

      <Card style={{ padding: 20 }}>
        <Flex direction="col" gap="3">
          <Flex direction="col" gap="1">
            <Text kind="title/lg">{STEP_TITLES[step]}</Text>
            <Text kind="body/regular/sm" style={subtle}>
              {STEP_DESCRIPTIONS[step]}
            </Text>
          </Flex>
          <Divider />
          {stepBody()}
        </Flex>
      </Card>

      {error ? <Banner status="error">{error}</Banner> : null}
      {problems.length > 0 && step !== LAST_STEP ? (
        <Banner status="warning">{problems.join(" ")}</Banner>
      ) : null}

      <Flex justify="between" align="center">
        <Flex gap="2">
          <Button
            kind="tertiary"
            onClick={() => setStep((s) => Math.max(0, s - 1))}
            disabled={step === 0 || busy !== ""}
          >
            <ArrowLeft size={14} style={{ marginRight: 6 }} />
            Back
          </Button>
          {onCancel ? (
            <Button kind="tertiary" onClick={onCancel} disabled={busy !== ""}>
              Cancel
            </Button>
          ) : null}
        </Flex>
        {step === LAST_STEP ? (
          <Button onClick={() => void onFinish()} disabled={busy !== ""}>
            <CheckCircle2 size={14} style={{ marginRight: 6 }} />
            {busy === "finish" ? "Creating…" : "Create project"}
          </Button>
        ) : (
          <Button
            onClick={() => setStep((s) => Math.min(LAST_STEP, s + 1))}
            disabled={problems.length > 0 || busy !== ""}
          >
            Next
            <ArrowRight size={14} style={{ marginLeft: 6 }} />
          </Button>
        )}
      </Flex>
    </Flex>
  );
}
