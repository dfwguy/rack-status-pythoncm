import Box from "@mui/material/Box";
import { Skeleton } from "@nvidia/foundations-react-core";

export function LoadingSkeleton() {
  return (
    <Box sx={{ p: 4, maxWidth: 1400, mx: "auto" }}>
      <Skeleton style={{ width: 300, height: 40, marginBottom: 8 }} />
      <Skeleton style={{ width: 500, height: 24, marginBottom: 24 }} />
      <Skeleton style={{ height: 80, marginBottom: 16 }} />
      <Skeleton style={{ height: 400 }} />
    </Box>
  );
}
