/* SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
 * SPDX-License-Identifier: LicenseRef-NvidiaProprietary
 *
 * NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
 * property and proprietary rights in and to this material, related
 * documentation and any modifications thereto. Any use, reproduction,
 * disclosure or distribution of this material and related documentation
 * without an express license agreement from NVIDIA CORPORATION or
 * its affiliates is strictly prohibited.
 * Author: Rafael M. Koike & Cursor Agent ("Ada")
 */

/**
 * User administration — FR-13, FR-13a, FR-17.
 *
 * Structure follows the NVIDIA Workshop Studio portal's users page, which had already solved the
 * shape: a search-and-filter row, one row per user, row actions behind an ellipsis menu, and a
 * confirmation dialog that names the exact consequence before anything happens.
 *
 * **Every destructive action states what cannot be undone.** Three of them are irreversible or
 * near-irreversible and the dialog says which: deleting retires the login forever (FR-2b), resetting
 * seizes the account and signs the holder out (FR-13a), disabling ends their sessions immediately.
 * A generic "Are you sure?" would make the cheap and the permanent look identical.
 *
 * The server is the authority on all of it. Actions are disabled here where the server would refuse
 * — your own row, the last enabled account — because a button that always fails is worse than one
 * that is visibly unavailable, but the refusals are enforced there and surfaced verbatim when they
 * arrive.
 */

import Box from "@mui/material/Box";
import FormControlLabel from "@mui/material/FormControlLabel";
import FormLabel from "@mui/material/FormLabel";
import Radio from "@mui/material/Radio";
import RadioGroup from "@mui/material/RadioGroup";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import IconButton from "@mui/material/IconButton";
import InputAdornment from "@mui/material/InputAdornment";
import ListItemIcon from "@mui/material/ListItemIcon";
import ListItemText from "@mui/material/ListItemText";
import Menu from "@mui/material/Menu";
import MenuItem from "@mui/material/MenuItem";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import TextField from "@mui/material/TextField";
import { Banner, Button, Card, Flex, Skeleton, Tag, Text } from "@nvidia/foundations-react-core";
import {
  Ellipsis,
  KeyRound,
  MailCheck,
  Pencil,
  Search,
  Trash2,
  UserCheck,
  UserPlus,
  UserX,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { IssuedCredentialDialog } from "../components/identity/IssuedCredentialDialog";
import { PageHeader } from "../components/layout/PageHeader";
import { useAuth } from "../context/AuthContext";
import * as identity from "../services/identityService";
import type { IdentityUser, IssuedCredential } from "../types/identity";

type Pending =
  | { kind: "disable" | "enable" | "reset" | "delete"; user: IdentityUser }
  | null;

export function UsersView() {
  const { user: signedInAs } = useAuth();

  const [users, setUsers] = useState<IdentityUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [search, setSearch] = useState("");

  const [menuAnchor, setMenuAnchor] = useState<HTMLElement | null>(null);
  const [menuUser, setMenuUser] = useState<IdentityUser | null>(null);
  const [pending, setPending] = useState<Pending>(null);
  const [busyLogin, setBusyLogin] = useState<string | null>(null);
  const [issued, setIssued] = useState<IssuedCredential | null>(null);

  const [creating, setCreating] = useState(false);
  const [newLogin, setNewLogin] = useState("");
  const [newDisplayName, setNewDisplayName] = useState("");
  const [newEmail, setNewEmail] = useState("");
  const [newPhoneNumber, setNewPhoneNumber] = useState("");
  const [newGivenName, setNewGivenName] = useState("");
  const [newFamilyName, setNewFamilyName] = useState("");

  // Editing an existing user's profile. Held separately from the create form so opening one does
  // not inherit half-typed values from the other.
  const [editing, setEditing] = useState<IdentityUser | null>(null);
  const [editDisplayName, setEditDisplayName] = useState("");
  const [editEmail, setEditEmail] = useState("");
  const [editPhoneNumber, setEditPhoneNumber] = useState("");
  const [editGivenName, setEditGivenName] = useState("");
  const [editFamilyName, setEditFamilyName] = useState("");
  const [editZoneinfo, setEditZoneinfo] = useState("");
  const [editLocale, setEditLocale] = useState("");
  // Generate by default. It is the safer path — a generated password is high-entropy and nobody
  // has typed it into a second place — so the operator has to opt into choosing one.
  const [credentialMode, setCredentialMode] = useState<"generate" | "choose">("generate");
  const [newPassword, setNewPassword] = useState("");
  const [newPasswordConfirm, setNewPasswordConfirm] = useState("");

  const reload = useCallback(async () => {
    try {
      setUsers(await identity.listUsers());
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load users");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  /** The signed-in principal is `user:<login>`; rows are keyed by bare login. */
  const myLogin = (signedInAs ?? "").replace(/^user:/, "");
  const enabledCount = users.filter((u) => !u.disabled).length;

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return users;
    return users.filter(
      (u) =>
        u.login.toLowerCase().includes(q) ||
        u.display_name.toLowerCase().includes(q) ||
        (u.email ?? "").toLowerCase().includes(q) ||
        u.groups.some((g) => g.toLowerCase().includes(q)),
    );
  }, [users, search]);

  const closeMenu = () => {
    setMenuAnchor(null);
    setMenuUser(null);
  };

  const run = async (action: () => Promise<void>, login: string, success: string) => {
    setBusyLogin(login);
    setError("");
    try {
      await action();
      setNotice(success);
      await reload();
    } catch (err) {
      // The server's wording explains which rule was hit; replacing it with something generic
      // would leave the operator guessing.
      setError(err instanceof Error ? err.message : "The operation failed");
    } finally {
      setBusyLogin(null);
    }
  };

  const openEdit = (user: IdentityUser) => {
    setEditing(user);
    setEditDisplayName(user.display_name);
    setEditEmail(user.email);
    setEditPhoneNumber(user.phone_number);
    setEditGivenName(user.given_name);
    setEditFamilyName(user.family_name);
    setEditZoneinfo(user.zoneinfo);
    setEditLocale(user.locale);
  };

  const saveEdit = async () => {
    if (editing === null) return;
    const user = editing;
    // Send only what moved. A no-op field would still be a write, and the server clears the
    // verified flag whenever an address is *set*, so resending an unchanged address would drop a
    // standing assertion for no reason.
    const changes: Parameters<typeof identity.updateProfile>[1] = {};
    if (editDisplayName.trim() !== user.display_name) changes.display_name = editDisplayName.trim();
    if (editEmail.trim() !== user.email) changes.email = editEmail.trim();
    if (editPhoneNumber.trim() !== user.phone_number) changes.phone_number = editPhoneNumber.trim();
    if (editGivenName.trim() !== user.given_name) changes.given_name = editGivenName.trim();
    if (editFamilyName.trim() !== user.family_name) changes.family_name = editFamilyName.trim();
    if (editZoneinfo.trim() !== user.zoneinfo) changes.zoneinfo = editZoneinfo.trim();
    if (editLocale.trim() !== user.locale) changes.locale = editLocale.trim();

    setEditing(null);
    if (Object.keys(changes).length === 0) return;
    const addressMoved = changes.email !== undefined;
    await run(
      () => identity.updateProfile(user.login, changes).then(() => undefined),
      user.login,
      addressMoved && user.email_verified
        ? `${user.login} was updated. The new address is no longer marked verified.`
        : `${user.login} was updated.`,
    );
  };

  const editEmailLooksWellFormed =
    editEmail.trim().length === 0 || /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(editEmail.trim());

  const confirm = async () => {
    if (pending === null) return;
    const { kind, user } = pending;
    setPending(null);
    if (kind === "disable" || kind === "enable") {
      await run(
        () => identity.setDisabled(user.login, kind === "disable").then(() => undefined),
        user.login,
        `${user.login} is now ${kind === "disable" ? "disabled" : "enabled"}.`,
      );
    } else if (kind === "reset") {
      setBusyLogin(user.login);
      try {
        setIssued(await identity.resetCredential(user.login));
        await reload();
      } catch (err) {
        setError(err instanceof Error ? err.message : "The reset failed");
      } finally {
        setBusyLogin(null);
      }
    } else {
      await run(
        () => identity.deleteUser(user.login),
        user.login,
        `${user.login} was deleted and the login retired.`,
      );
    }
  };

  const resetCreateForm = () => {
    setCreating(false);
    setNewLogin("");
    setNewDisplayName("");
    setCredentialMode("generate");
    setNewPassword("");
    setNewPasswordConfirm("");
    setNewEmail("");
    setNewPhoneNumber("");
    setNewGivenName("");
    setNewFamilyName("");
  };

  const createProfile = (): identity.NewUserProfile => ({
    display_name: newDisplayName.trim(),
    email: newEmail.trim(),
    phone_number: newPhoneNumber.trim(),
    given_name: newGivenName.trim(),
    family_name: newFamilyName.trim(),
  });

  const create = async () => {
    const login = newLogin.trim();
    if (!login) return;
    setBusyLogin(login);
    setError("");
    try {
      if (credentialMode === "choose") {
        // The password is turned into a salt and verifier in this browser; only those are sent.
        await identity.createUserWithPassword(login, newPassword, createProfile());
        resetCreateForm();
        setNotice(`${login} was created. Give them the password you chose.`);
      } else {
        const credential = await identity.createUser(login, createProfile());
        resetCreateForm();
        // Shown before the reload, because this password is the only unrecoverable thing here.
        setIssued(credential);
      }
      await reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not create the user");
    } finally {
      setBusyLogin(null);
    }
  };

  const chosenTooShort = newPassword.length > 0 && newPassword.length < 12;
  const chosenMismatch = newPasswordConfirm.length > 0 && newPassword !== newPasswordConfirm;
  // Mirrors the server's shape check rather than trying to out-guess it: the button stays
  // disabled for an obviously incomplete address, and anything subtler is the server's call, whose
  // refusal surfaces in the error banner. Duplicating the full rule here would let the two drift.
  const emailLooksWellFormed = /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(newEmail.trim());
  const canCreate =
    newLogin.trim().length > 0 &&
    emailLooksWellFormed &&
    (credentialMode === "generate" ||
      (newPassword.length >= 12 && newPassword === newPasswordConfirm));

  if (loading) {
    return (
      <Box sx={{ p: 3 }}>
        <PageHeader title="Users" description="Manage who can sign in to NVIS Greenlight." />
        <Skeleton style={{ height: 320, marginTop: 16 }} />
      </Box>
    );
  }

  return (
    <Box sx={{ p: 3 }}>
      <PageHeader
        title="Users"
        description="Manage who can sign in to NVIS Greenlight. New accounts receive a one-time password and must choose their own at first sign-in."
        actions={
          <Button kind="primary" size="small" onClick={() => setCreating(true)}>
            <UserPlus size={16} />
            <span style={{ marginLeft: 6 }}>Add user</span>
          </Button>
        }
      />

      {error && (
        <div style={{ marginBottom: 16 }}>
          <Banner status="error">{error}</Banner>
        </div>
      )}
      {notice && (
        <div style={{ marginBottom: 16 }}>
          <Banner status="success">{notice}</Banner>
        </div>
      )}

      <Box sx={{ display: "flex", gap: 2, mb: 2, alignItems: "center", flexWrap: "wrap" }}>
        <TextField
          size="small"
          placeholder="Search by login, name, email or group…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          slotProps={{
            input: {
              startAdornment: (
                <InputAdornment position="start">
                  <Search size={18} />
                </InputAdornment>
              ),
            },
          }}
          sx={{ minWidth: 300 }}
        />
        <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
          {filtered.length} of {users.length} · {enabledCount} can sign in
        </Text>
      </Box>

      <Card style={{ padding: 0, overflow: "hidden" }}>
        <TableContainer>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>Login</TableCell>
                <TableCell>Display name</TableCell>
                <TableCell>Email</TableCell>
                <TableCell>Groups</TableCell>
                <TableCell>Status</TableCell>
                <TableCell align="right" />
              </TableRow>
            </TableHead>
            <TableBody>
              {filtered.length === 0 && (
                <TableRow>
                  <TableCell colSpan={6}>
                    <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                      {users.length === 0
                        ? "No users yet."
                        : "No users match that search."}
                    </Text>
                  </TableCell>
                </TableRow>
              )}
              {filtered.map((u) => {
                const isSelf = u.login === myLogin;
                // Mirrors the server's guards so an action that would be refused is visibly
                // unavailable rather than failing on click.
                const isLastEnabled = !u.disabled && enabledCount <= 1;
                const locked = isSelf || isLastEnabled;
                return (
                  <TableRow key={u.login} hover>
                    <TableCell>
                      <Text kind="mono/sm">{u.login}</Text>
                      {isSelf && (
                        <Tag color="blue" kind="outline" density="compact" readOnly
                             style={{ marginLeft: 8 }}>
                          you
                        </Tag>
                      )}
                    </TableCell>
                    <TableCell>{u.display_name || "—"}</TableCell>
                    <TableCell>
                      {u.email ? (
                        <Flex align="center" gap="1">
                          <Text kind="body/regular/sm">{u.email}</Text>
                          {u.email_verified && (
                            <Tag color="green" kind="outline" readOnly density="compact">
                              verified
                            </Tag>
                          )}
                        </Flex>
                      ) : (
                        // Flagged rather than blank: accounts predating this field have no address
                        // and so cannot be notified, and that gap should be visible.
                        <Tag color="yellow" kind="outline" readOnly density="compact">
                          none
                        </Tag>
                      )}
                    </TableCell>
                    <TableCell>
                      {u.groups.length === 0
                        ? "—"
                        : u.groups.map((g) => (
                            <Tag key={g} color="gray" kind="outline" density="compact" readOnly
                                 style={{ marginRight: 4 }}>
                              {g}
                            </Tag>
                          ))}
                    </TableCell>
                    <TableCell>
                      {u.disabled ? (
                        <Tag color="red" kind="solid" density="compact" readOnly>disabled</Tag>
                      ) : u.must_change_password ? (
                        <Tag color="yellow" kind="outline" density="compact" readOnly>
                          must change password
                        </Tag>
                      ) : (
                        <Tag color="green" kind="outline" density="compact" readOnly>active</Tag>
                      )}
                    </TableCell>
                    <TableCell align="right">
                      <IconButton
                        size="small"
                        disabled={busyLogin === u.login}
                        onClick={(e) => {
                          setMenuAnchor(e.currentTarget);
                          setMenuUser({ ...u, ...(locked ? { disabled: u.disabled } : {}) });
                        }}
                        aria-label={`Actions for ${u.login}`}
                      >
                        <Ellipsis size={18} />
                      </IconButton>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </TableContainer>
      </Card>

      <Menu anchorEl={menuAnchor} open={menuAnchor !== null} onClose={closeMenu}>
        <MenuItem
          onClick={() => {
            if (menuUser) openEdit(menuUser);
            closeMenu();
          }}
        >
          <ListItemIcon><Pencil size={18} /></ListItemIcon>
          <ListItemText>Edit profile…</ListItemText>
        </MenuItem>
        <MenuItem
          // Its own item, not a checkbox in the edit dialog, because it is a different kind of act:
          // relying parties trust this claim for account linking, and it is audited separately.
          disabled={menuUser !== null && !menuUser.email}
          onClick={() => {
            const target = menuUser;
            closeMenu();
            if (!target) return;
            void run(
              () =>
                identity
                  .setEmailVerified(target.login, !target.email_verified)
                  .then(() => undefined),
              target.login,
              target.email_verified
                ? `${target.login}'s address is no longer marked verified.`
                : `${target.login}'s address is now marked verified.`,
            );
          }}
        >
          <ListItemIcon><MailCheck size={18} /></ListItemIcon>
          <ListItemText>
            {menuUser?.email_verified ? "Unmark email verified" : "Mark email verified"}
          </ListItemText>
        </MenuItem>
        <MenuItem
          onClick={() => {
            if (menuUser) setPending({ kind: "reset", user: menuUser });
            closeMenu();
          }}
        >
          <ListItemIcon><KeyRound size={18} /></ListItemIcon>
          <ListItemText>Reset password</ListItemText>
        </MenuItem>
        <MenuItem
          disabled={
            menuUser !== null &&
            !menuUser.disabled &&
            (menuUser.login === myLogin || enabledCount <= 1)
          }
          onClick={() => {
            if (menuUser) {
              setPending({ kind: menuUser.disabled ? "enable" : "disable", user: menuUser });
            }
            closeMenu();
          }}
        >
          <ListItemIcon>
            {menuUser?.disabled ? <UserCheck size={18} /> : <UserX size={18} />}
          </ListItemIcon>
          <ListItemText>{menuUser?.disabled ? "Enable" : "Disable"}</ListItemText>
        </MenuItem>
        <MenuItem
          disabled={menuUser !== null && menuUser.login === myLogin}
          onClick={() => {
            if (menuUser) setPending({ kind: "delete", user: menuUser });
            closeMenu();
          }}
        >
          <ListItemIcon><Trash2 size={18} /></ListItemIcon>
          <ListItemText>Delete…</ListItemText>
        </MenuItem>
      </Menu>

      {/* Edit profile */}
      <Dialog open={editing !== null} onClose={() => setEditing(null)} maxWidth="sm" fullWidth>
        <DialogTitle>Edit {editing?.login}</DialogTitle>
        <DialogContent>
          <TextField
            fullWidth
            autoFocus
            label="Display name"
            helperText="For presentation only. The login itself can never change."
            value={editDisplayName}
            onChange={(e) => setEditDisplayName(e.target.value)}
            sx={{ mb: 2, mt: 1 }}
          />
          <TextField
            fullWidth
            type="email"
            label="Email"
            helperText={
              editing?.email_verified && editEmail.trim() !== editing?.email
                ? "Changing the address clears its verified mark."
                : "Unique across users — where notifications will be sent."
            }
            value={editEmail}
            onChange={(e) => setEditEmail(e.target.value)}
            error={!editEmailLooksWellFormed}
            sx={{ mb: 2 }}
          />
          <TextField
            fullWidth
            label="Phone number (optional)"
            helperText="Stored in international form, e.g. +1 555 010 4477."
            value={editPhoneNumber}
            onChange={(e) => setEditPhoneNumber(e.target.value)}
            sx={{ mb: 2 }}
          />
          <Flex gap="2" style={{ marginBottom: 16 }}>
            <TextField
              fullWidth
              label="First name"
              value={editGivenName}
              onChange={(e) => setEditGivenName(e.target.value)}
            />
            <TextField
              fullWidth
              label="Last name"
              value={editFamilyName}
              onChange={(e) => setEditFamilyName(e.target.value)}
            />
          </Flex>
          <Flex gap="2">
            <TextField
              fullWidth
              label="Time zone"
              helperText="IANA name, e.g. America/New_York. Used to render times for this operator."
              value={editZoneinfo}
              onChange={(e) => setEditZoneinfo(e.target.value)}
            />
            <TextField
              fullWidth
              label="Locale"
              helperText="Language tag, e.g. en-US."
              value={editLocale}
              onChange={(e) => setEditLocale(e.target.value)}
            />
          </Flex>
        </DialogContent>
        <DialogActions>
          <Button kind="tertiary" onClick={() => setEditing(null)}>Cancel</Button>
          <Button
            kind="primary"
            disabled={!editEmailLooksWellFormed}
            onClick={() => void saveEdit()}
          >
            Save
          </Button>
        </DialogActions>
      </Dialog>

      {/* Add user */}
      <Dialog open={creating} onClose={resetCreateForm} maxWidth="sm" fullWidth>
        <DialogTitle>Add user</DialogTitle>
        <DialogContent>
          <TextField
            fullWidth
            autoFocus
            label="Login"
            helperText="Permanent. It identifies the user in every audit record and can never be changed or reused."
            value={newLogin}
            onChange={(e) => setNewLogin(e.target.value)}
            sx={{ mb: 2, mt: 1 }}
          />
          <TextField
            fullWidth
            label="Display name (optional)"
            helperText="For presentation only. Freely editable later."
            value={newDisplayName}
            onChange={(e) => setNewDisplayName(e.target.value)}
            sx={{ mb: 2 }}
          />
          <Flex gap="2" style={{ marginBottom: 16 }}>
            <TextField
              fullWidth
              label="First name (optional)"
              value={newGivenName}
              onChange={(e) => setNewGivenName(e.target.value)}
            />
            <TextField
              fullWidth
              label="Last name (optional)"
              value={newFamilyName}
              onChange={(e) => setNewFamilyName(e.target.value)}
            />
          </Flex>
          <TextField
            fullWidth
            required
            type="email"
            label="Email"
            helperText="Required, and unique across users — where notifications will be sent."
            value={newEmail}
            onChange={(e) => setNewEmail(e.target.value)}
            error={newEmail.trim().length > 0 && !emailLooksWellFormed}
            sx={{ mb: 2 }}
          />
          <TextField
            fullWidth
            label="Phone number (optional)"
            helperText="Stored in international form, e.g. +1 555 010 4477."
            value={newPhoneNumber}
            onChange={(e) => setNewPhoneNumber(e.target.value)}
            sx={{ mb: 3 }}
          />

          <FormLabel>
            <Text kind="body/bold/sm">Initial password</Text>
          </FormLabel>
          <RadioGroup
            value={credentialMode}
            onChange={(e) => setCredentialMode(e.target.value as "generate" | "choose")}
          >
            <FormControlLabel
              value="generate"
              control={<Radio size="small" />}
              label="Generate one and show it once (recommended)"
            />
            <FormControlLabel
              value="choose"
              control={<Radio size="small" />}
              label="Set a password now"
            />
          </RadioGroup>

          {credentialMode === "choose" && (
            <Box sx={{ mt: 1 }}>
              <TextField
                fullWidth
                type="password"
                label="Password"
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
                autoComplete="new-password"
                error={chosenTooShort}
                helperText={chosenTooShort ? "At least 12 characters" : " "}
                sx={{ mb: 1 }}
              />
              <TextField
                fullWidth
                type="password"
                label="Confirm password"
                value={newPasswordConfirm}
                onChange={(e) => setNewPasswordConfirm(e.target.value)}
                autoComplete="new-password"
                error={chosenMismatch}
                helperText={chosenMismatch ? "The two entries do not match" : " "}
              />
              <Text
                kind="body/regular/sm"
                style={{ display: "block", color: "var(--text-color-secondary)" }}
              >
                The password is turned into a verifier in your browser — it is not sent to the
                server and cannot be recovered from what is stored. {newLogin.trim() || "The user"}{" "}
                will still be required to choose their own at first sign-in, since you know this one.
              </Text>
            </Box>
          )}
        </DialogContent>
        <DialogActions>
          <Button kind="tertiary" onClick={resetCreateForm}>Cancel</Button>
          <Button kind="primary" disabled={!canCreate} onClick={() => void create()}>
            {credentialMode === "choose" ? "Create user" : "Create and show password"}
          </Button>
        </DialogActions>
      </Dialog>

      {/* Confirmation — names the exact consequence rather than asking "are you sure?" */}
      <Dialog open={pending !== null} onClose={() => setPending(null)} maxWidth="sm" fullWidth>
        <DialogTitle>
          {pending?.kind === "delete" && `Delete ${pending.user.login}?`}
          {pending?.kind === "reset" && `Reset the password for ${pending.user.login}?`}
          {pending?.kind === "disable" && `Disable ${pending.user.login}?`}
          {pending?.kind === "enable" && `Enable ${pending.user.login}?`}
        </DialogTitle>
        <DialogContent>
          {pending?.kind === "delete" && (
            <Banner status="warning">
              The login <strong>{pending.user.login}</strong> is retired permanently and can never
              be created again — audit records identify people by login, so reusing one would
              reattribute this person's history to somebody else. Disable the account instead if
              you may need it back.
            </Banner>
          )}
          {pending?.kind === "reset" && (
            <Banner status="warning">
              A new one-time password is issued and shown once. The current password stops working
              immediately and <strong>{pending.user.login}</strong> is signed out everywhere.
            </Banner>
          )}
          {pending?.kind === "disable" && (
            <Banner status="warning">
              <strong>{pending.user.login}</strong> is signed out immediately and cannot sign in
              again until re-enabled. Their group memberships are kept.
            </Banner>
          )}
          {pending?.kind === "enable" && (
            <Text kind="body/regular/sm">
              {pending.user.login} will be able to sign in again with their existing password.
            </Text>
          )}
        </DialogContent>
        <DialogActions>
          <Button kind="tertiary" onClick={() => setPending(null)}>Cancel</Button>
          <Button
            kind="primary"
            color={pending?.kind === "enable" ? "brand" : "danger"}
            onClick={() => void confirm()}
          >
            {pending?.kind === "delete" && "Delete and retire the login"}
            {pending?.kind === "reset" && "Reset and show the new password"}
            {pending?.kind === "disable" && "Disable and sign out"}
            {pending?.kind === "enable" && "Enable"}
          </Button>
        </DialogActions>
      </Dialog>

      <IssuedCredentialDialog credential={issued} onClose={() => setIssued(null)} />
    </Box>
  );
}
