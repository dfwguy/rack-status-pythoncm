import Box from "@mui/material/Box";
import Paper from "@mui/material/Paper";
import Typography from "@mui/material/Typography";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import { parseSqueueStdout } from "../lib/parseSqueueStdout";
import type { ClusterSqueueBlock } from "../types/api";

const NVIDIA_GREEN = "#76B900";

export function ClusterSqueueOverview({ squeue }: { squeue: ClusterSqueueBlock | null | undefined }) {
  if (!squeue) return null;

  const raw = squeue.stdout || "";
  const parsed = parseSqueueStdout(raw);

  return (
    <Paper
      variant="outlined"
      sx={{
        borderLeft: `4px solid ${NVIDIA_GREEN}`,
        overflow: "hidden",
      }}
    >
      <Box sx={{ px: 2, py: 1.5, borderBottom: 1, borderColor: "divider", bgcolor: "action.hover" }}>
        <Typography variant="subtitle1" sx={{ fontWeight: 700, color: NVIDIA_GREEN }}>
          Slurm queue (squeue)
        </Typography>
        {squeue.command_display && (
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", fontFamily: "monospace", mt: 0.5, wordBreak: "break-all" }}>
            {squeue.command_display}
          </Typography>
        )}
        {!squeue.ok && squeue.error && (
          <Typography variant="body2" color="error" sx={{ mt: 1 }}>
            {squeue.error}
          </Typography>
        )}
      </Box>
      <Box sx={{ p: 0 }}>
        {parsed.useRaw || !parsed.headers.length ? (
          <Box
            component="pre"
            sx={{
              m: 0,
              p: 2,
              maxHeight: 360,
              overflow: "auto",
              fontSize: 12,
              fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
            }}
          >
            {raw || "(empty)"}
          </Box>
        ) : (
          <TableContainer sx={{ maxHeight: 360 }}>
            <Table size="small" stickyHeader>
              <TableHead>
                <TableRow>
                  {parsed.headers.map((h) => (
                    <TableCell
                      key={h}
                      sx={{
                        fontWeight: 700,
                        bgcolor: "action.hover",
                        color: NVIDIA_GREEN,
                        whiteSpace: "nowrap",
                      }}
                    >
                      {h}
                    </TableCell>
                  ))}
                </TableRow>
              </TableHead>
              <TableBody>
                {parsed.rows.map((cells, ri) => (
                  <TableRow key={ri} hover sx={{ "&:nth-of-type(even)": { bgcolor: "action.hover" } }}>
                    {parsed.headers.map((_, ci) => (
                      <TableCell key={ci} sx={{ fontFamily: "monospace", fontSize: 12, whiteSpace: "nowrap" }}>
                        {cells[ci] ?? ""}
                      </TableCell>
                    ))}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableContainer>
        )}
      </Box>
    </Paper>
  );
}
