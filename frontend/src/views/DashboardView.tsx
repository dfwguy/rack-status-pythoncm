import Box from "@mui/material/Box";
import { Button } from "@nvidia/foundations-react-core";
import { PageHeader } from "../components/layout/PageHeader";
import { StatusSummary } from "../components/StatusSummary";
import { RackTable } from "../components/RackTable";
import { WorkerStatusPanel } from "../components/WorkerStatus";
import { FooterNotes } from "../components/FooterNotes";
import { LoadingSkeleton } from "../components/LoadingSkeleton";
import { ErrorBanner } from "../components/ErrorBanner";
import type { RackStatusResponse } from "../types/api";

interface DashboardViewProps {
  data: RackStatusResponse | null;
  loading: boolean;
  error: string | null;
  onRackSelect?: (rack: string) => void;
  onCommentChange?: (rack: string, comment: string | null) => void;
}

export function DashboardView({ data, loading, error, onRackSelect, onCommentChange }: DashboardViewProps) {
  if (loading && !data) return <LoadingSkeleton />;

  return (
    <Box sx={{ maxWidth: 1400, mx: "auto", p: 4 }}>
      <PageHeader
        title="Rack Validation Status"
        description="Overview of single-rack NEMO/NCCL validation progress."
        actions={
          <Button kind="secondary" size="small">
            Export to Excel
          </Button>
        }
      />

      {error && <ErrorBanner message={error} />}

      {data && (
        <Box sx={{ display: "flex", flexDirection: "column", gap: 2 }}>
          <StatusSummary
            summary={data.summary}
            updatedAt={data.updated_at}
            legend={data.legend}
          />

          {data.native_workers && (
            <WorkerStatusPanel workers={data.native_workers} />
          )}

          <RackTable rows={data.rows} comments={data.comments} logDir={data.log_dir} onRackSelect={onRackSelect} onCommentChange={onCommentChange} />

          <FooterNotes footnotes={data.footnotes} />
        </Box>
      )}
    </Box>
  );
}
