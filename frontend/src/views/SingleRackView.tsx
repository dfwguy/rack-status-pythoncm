import { useState, useCallback } from "react";
import Box from "@mui/material/Box";
import { PageHeader } from "../components/layout/PageHeader";
import { StatusSummary, type ValidationSummaryCategory } from "../components/StatusSummary";
import { RackTable } from "../components/RackTable";
import { WorkerStatusPanel } from "../components/WorkerStatus";
import { FooterNotes } from "../components/FooterNotes";
import { LoadingSkeleton } from "../components/LoadingSkeleton";
import { ErrorBanner } from "../components/ErrorBanner";
import type { RackStatusResponse } from "../types/api";

interface SingleRackViewProps {
  data: RackStatusResponse | null;
  loading: boolean;
  error: string | null;
  onRackSelect?: (rack: string) => void;
  onCommentChange?: (rack: string, comment: string | null) => void;
}

/** Per-rack validation table, workers, and filters (formerly the main dashboard). */
export function SingleRackView({ data, loading, error, onRackSelect, onCommentChange }: SingleRackViewProps) {
  const [validationFilters, setValidationFilters] = useState<string[]>([]);

  const toggleValidationCategory = useCallback((category: ValidationSummaryCategory) => {
    setValidationFilters((prev) =>
      prev.includes(category) ? prev.filter((c) => c !== category) : [...prev, category]
    );
  }, []);

  if (loading && !data) return <LoadingSkeleton />;

  return (
    <Box sx={{ maxWidth: 1400, mx: "auto", p: 4 }}>
      <PageHeader title="Rack Validation Status" description="Overview of single-rack NEMO/NCCL validation progress." />

      {error && <ErrorBanner message={error} />}

      {data && (
        <Box sx={{ display: "flex", flexDirection: "column", gap: 2 }}>
          <StatusSummary
            summary={data.summary}
            updatedAt={data.updated_at}
            legend={data.legend}
            validationFilters={validationFilters}
            onToggleValidationCategory={toggleValidationCategory}
          />

          {data.native_workers && (
            <WorkerStatusPanel workers={data.native_workers} />
          )}

          <RackTable
            rows={data.rows}
            comments={data.comments}
            logDir={data.log_dir}
            onRackSelect={onRackSelect}
            onCommentChange={onCommentChange}
            validationFilters={validationFilters}
            onValidationFiltersChange={setValidationFilters}
          />

          <FooterNotes footnotes={data.footnotes} />
        </Box>
      )}
    </Box>
  );
}
