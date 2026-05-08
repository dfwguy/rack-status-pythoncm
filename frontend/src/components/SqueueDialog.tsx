import Dialog from "@mui/material/Dialog";
import DialogTitle from "@mui/material/DialogTitle";
import DialogContent from "@mui/material/DialogContent";
import DialogActions from "@mui/material/DialogActions";
import { Button } from "@nvidia/foundations-react-core";
import { SqueuePanel } from "./SqueuePanel";

interface SqueueDialogProps {
  open: boolean;
  onClose: () => void;
  logDir?: string;
}

export function SqueueDialog({ open, onClose, logDir }: SqueueDialogProps) {
  return (
    <Dialog open={open} onClose={onClose} maxWidth="lg" fullWidth scroll="paper">
      <DialogTitle sx={{ fontWeight: 600 }}>squeue</DialogTitle>
      <DialogContent dividers sx={{ pt: 1 }}>
        <SqueuePanel logDir={logDir} active={open} tableMaxHeight={360} />
      </DialogContent>
      <DialogActions>
        <Button kind="tertiary" onClick={onClose}>
          Close
        </Button>
      </DialogActions>
    </Dialog>
  );
}
