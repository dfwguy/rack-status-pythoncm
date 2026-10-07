// Promise-based confirm/alert backed by the themed ConfirmDialog -- a drop-in for the browser's
// blocking window.confirm / window.alert.
//
//   const { confirm, showAlert, confirmDialog } = useConfirm();
//   // ... in JSX: {confirmDialog}
//   if (!(await confirm({ message: "Delete this?", danger: true }))) return;
//   await showAlert({ title: "Failed", message: err.message });
//
// Kept in its own file (not ConfirmDialog.tsx) so each module exports a single kind -- the
// react-refresh/only-export-components rule is a hard CI error here.
import { useCallback, useRef, useState } from "react";

import { ConfirmDialog, type ConfirmOptions } from "./ConfirmDialog";

export function useConfirm() {
  // Open and options are separate on purpose. Closing only flips `open`: the dialog stays mounted
  // through its exit fade, and clearing the options with it repainted that fade with the defaults
  // ("Confirm" / "Cancel" / "Confirm", an empty body, a danger button turning brand) -- the ask
  // visibly changed into a different dialog as it left. The options persist until the next ask.
  const [open, setOpen] = useState(false);
  const [options, setOptions] = useState<ConfirmOptions | null>(null);
  const resolver = useRef<((v: boolean) => void) | null>(null);

  const ask = useCallback(
    (opts: ConfirmOptions) =>
      new Promise<boolean>((resolve) => {
        // A second ask before the first resolves cancels the first — one dialog at a time.
        resolver.current?.(false);
        resolver.current = resolve;
        setOptions(opts);
        setOpen(true);
      }),
    [],
  );

  const confirm = useCallback((opts: ConfirmOptions) => ask(opts), [ask]);
  const showAlert = useCallback(
    (opts: ConfirmOptions) => ask({ ...opts, alert: true }).then(() => undefined),
    [ask],
  );

  const settle = useCallback((result: boolean) => {
    resolver.current?.(result);
    resolver.current = null;
    setOpen(false);
  }, []);

  const confirmDialog = (
    <ConfirmDialog
      open={open}
      message={options?.message ?? ""}
      title={options?.title}
      confirmLabel={options?.confirmLabel}
      cancelLabel={options?.cancelLabel}
      danger={options?.danger}
      alert={options?.alert}
      onConfirm={() => settle(true)}
      onCancel={() => settle(false)}
    />
  );

  return { confirm, showAlert, confirmDialog };
}
