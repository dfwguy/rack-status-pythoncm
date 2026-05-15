import Dialog from "@mui/material/Dialog";
import DialogTitle from "@mui/material/DialogTitle";
import DialogContent from "@mui/material/DialogContent";
import DialogActions from "@mui/material/DialogActions";
import { Button } from "@nvidia/foundations-react-core";
import { SinfoPanel } from "./SinfoPanel";

interface SinfoDialogProps {
  open: boolean;
  onClose: () => void;
}

export function SinfoDialog({ open, onClose }: SinfoDialogProps) {
  return (
    <Dialog open={open} onClose={onClose} maxWidth="lg" fullWidth scroll="paper">
      <DialogTitle sx={{ fontWeight: 600 }}>sinfo -R --long</DialogTitle>
      <DialogContent dividers sx={{ pt: 1 }}>
        <SinfoPanel active={open} tableMaxHeight={360} />
      </DialogContent>
      <DialogActions>
        <Button kind="tertiary" onClick={onClose}>
          Close
        </Button>
      </DialogActions>
    </Dialog>
  );
}
