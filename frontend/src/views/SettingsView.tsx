import { useCallback, useEffect, useRef, useState } from "react";
import Box from "@mui/material/Box";
import TextField from "@mui/material/TextField";
import Button from "@mui/material/Button";
import Paper from "@mui/material/Paper";
import Typography from "@mui/material/Typography";
import Alert from "@mui/material/Alert";
import CircularProgress from "@mui/material/CircularProgress";
import Chip from "@mui/material/Chip";
import Tab from "@mui/material/Tab";
import Tabs from "@mui/material/Tabs";
import Switch from "@mui/material/Switch";
import FormControlLabel from "@mui/material/FormControlLabel";
import { Card, Text } from "@nvidia/foundations-react-core";
import { PageHeader } from "../components/layout/PageHeader";
import { getAISettings, updateAISettings, testAIConnection, type AISettings } from "../services/aiService";

/** Use monospace input for long regex / technical values. */
const MONOSPACE_JOB_ENV_KEYS = new Set([
  "RACK_CMSH_DS_NODE_PATTERN",
  "RACK_CMSH_DS_NODE_TOKEN_PATTERN",
  "RACK_CMSH_DS_NODE_FIND_PATTERN",
  "RACK_CMSH_DS_NODE_PARSE_PATTERN",
  "NVLMAPPER_RC",
]);

const PASSWORD_JOB_ENV_KEYS = new Set(["RACK_PROMETHEUS_BASIC_AUTH_PASSWORD", "NVLMAPPER_PASSWORD"]);

interface SettingsViewProps {
  logDir: string;
  onLogDirChange: (val: string) => void;
  /** After a successful save: refetch dashboard APIs (status, cluster overview) so UI matches overrides. */
  onDashboardEnvSaved?: () => void | Promise<void>;
}

type JobEnvRow = {
  key: string;
  label: string;
  description: string;
  value: string;
  has_override?: boolean;
  section?: string;
};

type JobEnvSection = {
  id: string;
  title: string;
  variables: JobEnvRow[];
};

export function SettingsView({ logDir, onLogDirChange, onDashboardEnvSaved }: SettingsViewProps) {
  const [jobEnvSections, setJobEnvSections] = useState<JobEnvSection[]>([]);
  const [jobEnvPath, setJobEnvPath] = useState("");
  const [jobEnvValues, setJobEnvValues] = useState<Record<string, string>>({});
  const [jobEnvLoading, setJobEnvLoading] = useState(true);
  const [jobEnvSaving, setJobEnvSaving] = useState(false);
  const [jobEnvError, setJobEnvError] = useState("");
  const [jobEnvOk, setJobEnvOk] = useState("");
  /** Index into ``jobEnvSections`` for the tabbed environment editor. */
  const [jobEnvSectionTab, setJobEnvSectionTab] = useState(0);
  /** Last effective values from the server (GET / successful PUT). Save only sends keys that differ — empty strings here would otherwise clear unrelated overrides on the server. */
  const jobEnvBaselineRef = useRef<Record<string, string>>({});

  const loadJobEnv = useCallback(async () => {
    setJobEnvError("");
    setJobEnvLoading(true);
    try {
      const res = await fetch("/api/settings/job-environment", {
        credentials: "same-origin",
        cache: "no-store",
      });
      if (res.status === 401) {
        window.location.href = `/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
        return;
      }
      const json = (await res.json()) as {
        ok?: boolean;
        error?: string;
        sections?: JobEnvSection[];
        variables?: JobEnvRow[];
        path?: string;
      };
      if (!res.ok || !json.ok) {
        setJobEnvError(json.error || `HTTP ${res.status}`);
        setJobEnvSections([]);
        jobEnvBaselineRef.current = {};
        return;
      }
      setJobEnvPath(json.path || "");
      const m: Record<string, string> = {};
      if (json.sections && json.sections.length > 0) {
        setJobEnvSections(json.sections);
        for (const sec of json.sections) {
          for (const v of sec.variables) {
            m[v.key] = v.value ?? "";
          }
        }
      } else if (json.variables && json.variables.length > 0) {
        setJobEnvSections([{ id: "all", title: "Environment variables", variables: json.variables }]);
        for (const v of json.variables) {
          m[v.key] = v.value ?? "";
        }
      } else {
        setJobEnvError("No environment variables returned");
        setJobEnvSections([]);
        jobEnvBaselineRef.current = {};
        return;
      }
      jobEnvBaselineRef.current = { ...m };
      setJobEnvValues(m);
    } catch (e) {
      setJobEnvError(e instanceof Error ? e.message : String(e));
      setJobEnvSections([]);
      jobEnvBaselineRef.current = {};
    } finally {
      setJobEnvLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadJobEnv();
  }, [loadJobEnv]);

  useEffect(() => {
    setJobEnvSectionTab((i) => {
      if (jobEnvSections.length === 0) {
        return 0;
      }
      return Math.min(i, jobEnvSections.length - 1);
    });
  }, [jobEnvSections]);

  const reloadJobEnvAndDashboard = useCallback(async () => {
    setJobEnvError("");
    setJobEnvOk("");
    await loadJobEnv();
    if (onDashboardEnvSaved) {
      await Promise.resolve(onDashboardEnvSaved());
    }
  }, [loadJobEnv, onDashboardEnvSaved]);

  const saveJobEnv = useCallback(async () => {
    setJobEnvError("");
    setJobEnvOk("");
    setJobEnvSaving(true);
    try {
      const baseline = jobEnvBaselineRef.current;
      const overrides: Record<string, string> = {};
      for (const sec of jobEnvSections) {
        for (const row of sec.variables) {
          const key = row.key;
          const cur = jobEnvValues[key] ?? "";
          const base = baseline[key] ?? "";
          if (cur !== base) {
            overrides[key] = cur;
          }
        }
      }
      const res = await fetch("/api/settings/job-environment", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        cache: "no-store",
        body: JSON.stringify({ overrides }),
      });
      if (res.status === 401) {
        window.location.href = `/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
        return;
      }
      const json = (await res.json()) as {
        ok?: boolean;
        error?: string;
        sections?: JobEnvSection[];
        variables?: JobEnvRow[];
        path?: string;
      };
      if (!res.ok || !json.ok) {
        setJobEnvError(json.error || `HTTP ${res.status}`);
        return;
      }
      setJobEnvOk("Saved. Dashboard data was refreshed to match the new values.");
      // Apply the PUT response (same shape as GET) so we do not rely on a follow-up GET that may
      // hit another Gunicorn worker with stale in-memory cache.
      if (json.sections && json.sections.length > 0) {
        setJobEnvPath(json.path || "");
        setJobEnvSections(json.sections);
        const m: Record<string, string> = {};
        for (const sec of json.sections) {
          for (const v of sec.variables) {
            m[v.key] = v.value ?? "";
          }
        }
        jobEnvBaselineRef.current = { ...m };
        setJobEnvValues(m);
      } else {
        await loadJobEnv();
      }
      if (onDashboardEnvSaved) {
        await Promise.resolve(onDashboardEnvSaved());
      }
    } catch (e) {
      setJobEnvError(e instanceof Error ? e.message : String(e));
    } finally {
      setJobEnvSaving(false);
    }
  }, [jobEnvSections, jobEnvValues, loadJobEnv, onDashboardEnvSaved]);

  const hasJobEnvRows = jobEnvSections.some((s) => s.variables.length > 0);

  const renderRow = (row: JobEnvRow, monospace: boolean) => {
    const passwordField = PASSWORD_JOB_ENV_KEYS.has(row.key);
    return (
      <Box key={row.key} sx={{ display: "flex", flexDirection: "column", gap: 0.5 }}>
        <Box sx={{ display: "flex", alignItems: "center", gap: 1, flexWrap: "wrap" }}>
          <Typography variant="caption" color="text.secondary" sx={{ fontFamily: "monospace" }}>
            {row.key}
          </Typography>
          {row.has_override ? <Chip size="small" label="override" color="primary" variant="outlined" /> : null}
        </Box>
        <TextField
          label={row.label}
          size="small"
          fullWidth
          type={passwordField ? "password" : "text"}
          autoComplete={passwordField ? "new-password" : undefined}
          value={jobEnvValues[row.key] ?? ""}
          onChange={(e) =>
            setJobEnvValues((prev) => ({
              ...prev,
              [row.key]: e.target.value,
            }))
          }
          helperText={row.description}
          slotProps={
            monospace
              ? {
                  input: {
                    sx: { fontFamily: "ui-monospace, monospace", fontSize: "0.8125rem" },
                  },
                }
              : undefined
          }
        />
      </Box>
    );
  };

  const [topTab, setTopTab] = useState(0);

  return (
    <Box sx={{ maxWidth: 900, mx: "auto", p: 4 }}>
      <PageHeader
        title="Settings"
        description="Configure log directory, environment overrides, and AI assistant endpoint."
      />

      <Tabs
        value={topTab}
        onChange={(_, v) => setTopTab(v)}
        sx={{ borderBottom: 1, borderColor: "divider", mb: 3 }}
      >
        <Tab label="General" />
        <Tab label="AI Configuration" />
      </Tabs>

      {topTab === 1 && <AIConfigurationPanel />}

      {topTab === 0 && (
      <Box sx={{ display: "flex", flexDirection: "column", gap: 3 }}>
        <Card style={{ padding: 24 }}>
          <Text kind="body/bold/md" style={{ marginBottom: 8, display: "block" }}>
            Log Directory
          </Text>
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", marginBottom: 16, display: "block" }}>
            Root directory for NEMO validation logs. This is used as the working directory for rack_status.sh and log
            analysis (sent with API requests as <code>log_dir</code>).
          </Text>
          <TextField
            size="small"
            fullWidth
            value={logDir}
            onChange={(e) => onLogDirChange(e.target.value)}
            placeholder="leave empty for server default (RACK_STATUS_LOG_DIR)"
          />
        </Card>

        <Paper variant="outlined" sx={{ p: 2 }}>
          <Typography variant="subtitle1" sx={{ fontWeight: 600, mb: 0.5 }}>
            Job &amp; environment overrides
          </Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            Values are merged over the Flask process environment for the dashboard (status refresh, page title), external
            services (Grafana embed URLs, Prometheus basic auth for bundled scripts, log/chart timezones), validation, parser,
            native status (including <Box component="code" sx={{ fontSize: "0.85em" }}>cmsh -c ds</Box>
            ), Slurm helpers, and bundled scripts. Saving writes to{" "}
            <Box component="code" sx={{ fontSize: "0.85em" }}>
              {jobEnvPath || "var/cache/job_env_overrides.json"}
            </Box>
            . Leave a field empty and save to clear an override (inherit from the shell again).
          </Typography>

          {jobEnvLoading ? (
            <Box sx={{ display: "flex", alignItems: "center", gap: 1, py: 2 }}>
              <CircularProgress size={22} />
              <Typography variant="body2" color="text.secondary">
                Loading…
              </Typography>
            </Box>
          ) : null}

          {jobEnvError ? (
            <Alert severity="error" sx={{ mb: 2 }}>
              {jobEnvError}
            </Alert>
          ) : null}
          {jobEnvOk ? (
            <Alert severity="success" sx={{ mb: 2 }} onClose={() => setJobEnvOk("")}>
              {jobEnvOk}
            </Alert>
          ) : null}

          {!jobEnvLoading && hasJobEnvRows ? (
            <Box sx={{ display: "flex", flexDirection: "column", gap: 2 }}>
              {jobEnvSections.length > 1 ? (
                <Tabs
                  value={jobEnvSectionTab}
                  onChange={(_, next) => setJobEnvSectionTab(next)}
                  variant="scrollable"
                  scrollButtons="auto"
                  sx={{ borderBottom: 1, borderColor: "divider", mb: 0 }}
                >
                  {jobEnvSections.map((sec) => (
                    <Tab
                      key={sec.id}
                      label={sec.title}
                      id={`settings-env-tab-${sec.id}`}
                      aria-controls={`settings-env-panel-${sec.id}`}
                    />
                  ))}
                </Tabs>
              ) : (
                <Typography variant="subtitle2" sx={{ fontWeight: 600 }}>
                  {jobEnvSections[0]?.title}
                </Typography>
              )}
              {jobEnvSections.map((sec, idx) => (
                <Box
                  key={sec.id}
                  role="tabpanel"
                  hidden={jobEnvSections.length > 1 ? jobEnvSectionTab !== idx : false}
                  id={`settings-env-panel-${sec.id}`}
                  aria-labelledby={`settings-env-tab-${sec.id}`}
                  sx={{
                    display: "flex",
                    flexDirection: "column",
                    gap: 2,
                    pt: jobEnvSections.length > 1 ? 1 : 0,
                  }}
                >
                  {sec.variables.map((row) => renderRow(row, MONOSPACE_JOB_ENV_KEYS.has(row.key)))}
                </Box>
              ))}
              <Box sx={{ display: "flex", gap: 2, alignItems: "center", pt: 1 }}>
                <Button variant="contained" disabled={jobEnvSaving} onClick={() => void saveJobEnv()}>
                  {jobEnvSaving ? "Saving…" : "Save"}
                </Button>
                <Button
                  variant="outlined"
                  disabled={jobEnvSaving || jobEnvLoading}
                  onClick={() => void reloadJobEnvAndDashboard()}
                >
                  Reload
                </Button>
              </Box>
            </Box>
          ) : null}
        </Paper>
      </Box>
      )}
    </Box>
  );
}

// ---------------------------------------------------------------------------
// AI Configuration Panel
// ---------------------------------------------------------------------------

function AIConfigurationPanel() {
  const [settings, setSettings] = useState<AISettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [endpoint, setEndpoint] = useState("");
  const [model, setModel] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [systemRules, setSystemRules] = useState("");

  useEffect(() => {
    getAISettings()
      .then((s) => {
        setSettings(s);
        setEndpoint(s.endpoint || "");
        setModel(s.model || "");
        setEnabled(s.enabled);
        setSystemRules(s.system_rules || "");
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  const handleSave = async () => {
    setError("");
    setSuccess("");
    setSaving(true);
    try {
      const payload: Record<string, unknown> = { enabled, endpoint, model, system_rules: systemRules };
      if (apiKey.trim()) payload.api_key = apiKey.trim();
      const updated = await updateAISettings(payload as Parameters<typeof updateAISettings>[0]);
      setSettings(updated);
      setApiKey("");
      setSuccess("AI settings saved.");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const handleTest = async () => {
    setError("");
    setSuccess("");
    setTesting(true);
    try {
      const result = await testAIConnection();
      if (result.ok) {
        setSuccess(`Connection successful. Model: ${result.model}`);
      } else {
        setError(`Connection failed: ${result.error}`);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setTesting(false);
    }
  };

  if (loading) {
    return (
      <Box sx={{ display: "flex", alignItems: "center", gap: 1, py: 2 }}>
        <CircularProgress size={22} />
        <Typography variant="body2" color="text.secondary">Loading AI settings…</Typography>
      </Box>
    );
  }

  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 3 }}>
      {error && <Alert severity="error" onClose={() => setError("")}>{error}</Alert>}
      {success && <Alert severity="success" onClose={() => setSuccess("")}>{success}</Alert>}

      <Card style={{ padding: 24 }}>
        <Text kind="body/bold/md" style={{ marginBottom: 16, display: "block" }}>
          AI Assistant Configuration
        </Text>
        <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", marginBottom: 24, display: "block" }}>
          Configure the LLM endpoint for the AI assistant. Supports NVIDIA NIM, OpenAI-compatible APIs (vLLM, Ollama), or a local Llama instance.
        </Text>

        <Box sx={{ display: "flex", flexDirection: "column", gap: 2.5 }}>
          <FormControlLabel
            control={<Switch checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />}
            label="Enable AI Assistant"
          />

          <TextField
            label="Endpoint URL"
            size="small"
            fullWidth
            value={endpoint}
            onChange={(e) => setEndpoint(e.target.value)}
            placeholder="https://integrate.api.nvidia.com/v1/chat/completions"
            helperText="OpenAI-compatible chat completions endpoint"
          />

          <TextField
            label="Model"
            size="small"
            fullWidth
            value={model}
            onChange={(e) => setModel(e.target.value)}
            placeholder="nvidia/llama-3.1-nemotron-ultra-253b-v1"
            helperText="Model identifier sent in the API request"
          />

          <TextField
            label="API Key"
            size="small"
            fullWidth
            type="password"
            autoComplete="new-password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder={settings?.api_key_configured ? "••• key configured (enter new to replace)" : "Enter API key"}
            helperText={settings?.api_key_masked ? `Current: ${settings.api_key_masked}` : "Required for NVIDIA NIM and cloud endpoints"}
          />

          <TextField
            label="Custom System Rules"
            size="small"
            fullWidth
            multiline
            minRows={3}
            maxRows={8}
            value={systemRules}
            onChange={(e) => setSystemRules(e.target.value)}
            placeholder="Additional instructions for the AI assistant (optional)"
            helperText="Appended to the system prompt for all conversations"
          />

          <Box sx={{ display: "flex", gap: 2, pt: 1 }}>
            <Button variant="contained" disabled={saving} onClick={() => void handleSave()}>
              {saving ? "Saving…" : "Save"}
            </Button>
            <Button variant="outlined" disabled={testing || !settings?.api_key_configured} onClick={() => void handleTest()}>
              {testing ? "Testing…" : "Test Connection"}
            </Button>
          </Box>
        </Box>
      </Card>
    </Box>
  );
}
