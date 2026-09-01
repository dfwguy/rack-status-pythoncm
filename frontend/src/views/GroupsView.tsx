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
 * Group and membership administration — FR-8, FR-17.
 *
 * **Membership editing is the reason this view exists.** The scope document puts it plainly: without
 * it, groups are a schema field. It is also the highest-privilege operation on this surface — a
 * group is how grants will be conferred once Phase 3 lands, so whoever can add a member can grant
 * themselves whatever that group has.
 *
 * Which is why the view is honest about what groups do *today*: nothing. There is no policy engine
 * yet, so membership confers no access, and a UI that implied otherwise would invite an operator to
 * believe they had restricted something. The banner says so, and it should be deleted in the same
 * change that wires a real engine.
 *
 * Groups are flat (FR-8) — a group holds users and never another group — so the member picker offers
 * only users and there is no nesting to reason about.
 */

import Box from "@mui/material/Box";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import IconButton from "@mui/material/IconButton";
import MenuItem from "@mui/material/MenuItem";
import TextField from "@mui/material/TextField";
import { Banner, Button, Card, Flex, Skeleton, Tag, Text } from "@nvidia/foundations-react-core";
import { Trash2, UserMinus, UserPlus, Users } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { PageHeader } from "../components/layout/PageHeader";
import * as identity from "../services/identityService";
import type { IdentityGroup, IdentityUser } from "../types/identity";

export function GroupsView() {
  const [groups, setGroups] = useState<IdentityGroup[]>([]);
  const [users, setUsers] = useState<IdentityUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [newDescription, setNewDescription] = useState("");

  const [addingTo, setAddingTo] = useState<IdentityGroup | null>(null);
  const [memberToAdd, setMemberToAdd] = useState("");
  const [deleting, setDeleting] = useState<IdentityGroup | null>(null);

  const reload = useCallback(async () => {
    try {
      // Both, because the member picker needs the user list and the rows need membership.
      const [g, u] = await Promise.all([identity.listGroups(), identity.listUsers()]);
      setGroups(g);
      setUsers(u);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load groups");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const act = async (action: () => Promise<void>, success: string) => {
    setError("");
    try {
      await action();
      setNotice(success);
      await reload();
    } catch (err) {
      // Surfaced verbatim: the server's refusals name the constraint, e.g. a group cannot be a
      // member of a group (FR-8).
      setError(err instanceof Error ? err.message : "The operation failed");
    }
  };

  if (loading) {
    return (
      <Box sx={{ p: 3 }}>
        <PageHeader title="Groups" description="Organise users into groups." />
        <Skeleton style={{ height: 280, marginTop: 16 }} />
      </Box>
    );
  }

  return (
    <Box sx={{ p: 3 }}>
      <PageHeader
        title="Groups"
        description="Organise users into flat groups. A group holds users and never another group."
        actions={
          <Button kind="primary" size="small" onClick={() => setCreating(true)}>
            <UserPlus size={16} />
            <span style={{ marginLeft: 6 }}>Add group</span>
          </Button>
        }
      />

      {/* Delete this banner in the same change that registers a real policy engine. */}
      <div style={{ marginBottom: 16 }}>
        <Banner status="info">
          Groups do not grant anything yet. Authorization is permissive for every signed-in user
          until the policy engine lands, so membership recorded now takes effect then — it is not
          restricting access today.
        </Banner>
      </div>

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

      {groups.length === 0 && (
        <Card style={{ padding: 32, textAlign: "center" }}>
          <Users size={28} style={{ opacity: 0.5 }} />
          <Text kind="body/regular/md" style={{ display: "block", marginTop: 12 }}>
            No groups yet.
          </Text>
          <Text
            kind="body/regular/sm"
            style={{ display: "block", marginTop: 4, color: "var(--text-color-secondary)" }}
          >
            Create one to start organising operators.
          </Text>
        </Card>
      )}

      <Flex style={{ flexDirection: "column", gap: 12 }}>
        {groups.map((group) => (
          <Card key={group.name} style={{ padding: 20 }}>
            <Flex style={{ justifyContent: "space-between", alignItems: "flex-start", gap: 16 }}>
              <div style={{ flex: 1 }}>
                <Text kind="title/sm" style={{ display: "block" }}>{group.name}</Text>
                {group.description && (
                  <Text
                    kind="body/regular/sm"
                    style={{ display: "block", marginTop: 4, color: "var(--text-color-secondary)" }}
                  >
                    {group.description}
                  </Text>
                )}

                <div style={{ marginTop: 12 }}>
                  {group.members.length === 0 ? (
                    <Text
                      kind="body/regular/sm"
                      style={{ color: "var(--text-color-secondary)" }}
                    >
                      No members
                    </Text>
                  ) : (
                    group.members.map((login) => (
                      <Tag
                        key={login}
                        color="gray"
                        kind="outline"
                        density="compact"
                        readOnly
                        style={{ marginRight: 6, marginBottom: 6 }}
                      >
                        {login}
                        <IconButton
                          size="small"
                          aria-label={`Remove ${login} from ${group.name}`}
                          onClick={() =>
                            void act(
                              () => identity.removeMember(group.name, login),
                              `${login} was removed from ${group.name}.`,
                            )
                          }
                          sx={{ ml: 0.5, p: 0.2 }}
                        >
                          <UserMinus size={13} />
                        </IconButton>
                      </Tag>
                    ))
                  )}
                </div>
              </div>

              <Flex style={{ gap: 8, flexShrink: 0 }}>
                <Button
                  kind="secondary"
                  size="small"
                  onClick={() => {
                    setAddingTo(group);
                    setMemberToAdd("");
                  }}
                >
                  <UserPlus size={15} />
                  <span style={{ marginLeft: 6 }}>Add member</span>
                </Button>
                <IconButton
                  size="small"
                  aria-label={`Delete group ${group.name}`}
                  onClick={() => setDeleting(group)}
                >
                  <Trash2 size={17} />
                </IconButton>
              </Flex>
            </Flex>
          </Card>
        ))}
      </Flex>

      {/* Create group */}
      <Dialog open={creating} onClose={() => setCreating(false)} maxWidth="sm" fullWidth>
        <DialogTitle>Add group</DialogTitle>
        <DialogContent>
          <TextField
            fullWidth
            autoFocus
            label="Name"
            helperText="Permanent — a group cannot be renamed, because its name is its identifier."
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            sx={{ mb: 2, mt: 1 }}
          />
          <TextField
            fullWidth
            label="Description (optional)"
            value={newDescription}
            onChange={(e) => setNewDescription(e.target.value)}
          />
        </DialogContent>
        <DialogActions>
          <Button kind="tertiary" onClick={() => setCreating(false)}>Cancel</Button>
          <Button
            kind="primary"
            disabled={!newName.trim()}
            onClick={() => {
              const name = newName.trim();
              setCreating(false);
              setNewName("");
              setNewDescription("");
              void act(
                () => identity.createGroup(name, newDescription.trim()),
                `Group ${name} was created.`,
              );
            }}
          >
            Create
          </Button>
        </DialogActions>
      </Dialog>

      {/* Add member — users only, since groups are flat (FR-8) */}
      <Dialog open={addingTo !== null} onClose={() => setAddingTo(null)} maxWidth="sm" fullWidth>
        <DialogTitle>Add a member to {addingTo?.name}</DialogTitle>
        <DialogContent>
          <TextField
            select
            fullWidth
            label="User"
            value={memberToAdd}
            onChange={(e) => setMemberToAdd(e.target.value)}
            sx={{ mt: 1 }}
            helperText="Only users can be members. A group never contains another group."
          >
            {users
              .filter((u) => !(addingTo?.members ?? []).includes(u.login))
              .map((u) => (
                <MenuItem key={u.login} value={u.login}>
                  {u.login}
                  {u.display_name && u.display_name !== u.login ? ` — ${u.display_name}` : ""}
                  {u.disabled ? " (disabled)" : ""}
                </MenuItem>
              ))}
          </TextField>
        </DialogContent>
        <DialogActions>
          <Button kind="tertiary" onClick={() => setAddingTo(null)}>Cancel</Button>
          <Button
            kind="primary"
            disabled={!memberToAdd}
            onClick={() => {
              const group = addingTo?.name ?? "";
              const login = memberToAdd;
              setAddingTo(null);
              void act(
                () => identity.addMember(group, login),
                `${login} was added to ${group}.`,
              );
            }}
          >
            Add member
          </Button>
        </DialogActions>
      </Dialog>

      {/* Delete group */}
      <Dialog open={deleting !== null} onClose={() => setDeleting(null)} maxWidth="sm" fullWidth>
        <DialogTitle>Delete {deleting?.name}?</DialogTitle>
        <DialogContent>
          <Banner status="warning">
            The group and its {deleting?.members.length ?? 0} membership
            {(deleting?.members.length ?? 0) === 1 ? "" : "s"} are removed. The users themselves are
            not affected, and the name becomes available again.
          </Banner>
        </DialogContent>
        <DialogActions>
          <Button kind="tertiary" onClick={() => setDeleting(null)}>Cancel</Button>
          <Button
            kind="primary"
            color="danger"
            onClick={() => {
              const name = deleting?.name ?? "";
              setDeleting(null);
              void act(() => identity.deleteGroup(name), `Group ${name} was deleted.`);
            }}
          >
            Delete group
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
