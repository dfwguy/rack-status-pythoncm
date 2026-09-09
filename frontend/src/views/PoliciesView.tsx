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
 * Authoring policies and attaching them to users and groups (FR-19, FR-20, FR-21).
 *
 * Until this existed the policy engine could only be driven by editing `var/store/policies.json`
 * on the host, which meant the whole of Phase 3 was unusable through the product.
 *
 * Two things the UI is deliberately explicit about, because both are ways an operator can remove
 * their own access without noticing:
 *
 *  - **Deleting an attached policy** shows exactly who has it and makes the operator confirm a
 *    second time. The server refuses the first attempt and names the subjects; this surfaces that
 *    refusal rather than retrying with `force` behind the scenes.
 *  - **A deny statement** is called out in the table, because deny beats every allow at any
 *    specificity and a policy that looks narrow can shut off a whole verb.
 */

import Box from '@mui/material/Box';
import Dialog from '@mui/material/Dialog';
import DialogActions from '@mui/material/DialogActions';
import DialogContent from '@mui/material/DialogContent';
import DialogTitle from '@mui/material/DialogTitle';
import MenuItem from '@mui/material/MenuItem';
import Table from '@mui/material/Table';
import TableBody from '@mui/material/TableBody';
import TableCell from '@mui/material/TableCell';
import TableHead from '@mui/material/TableHead';
import TableRow from '@mui/material/TableRow';
import TextField from '@mui/material/TextField';
import { Banner, Button, Card, Flex, Skeleton, Tag, Text } from '@nvidia/foundations-react-core';
import { Plus, ShieldAlert, Trash2 } from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';

import PolicyEditor from '../components/policy/PolicyEditor';
import { listGroups, listUsers } from '../services/identityService';
import {
  attachPolicy,
  createPolicy,
  deletePolicy,
  detachPolicy,
  fetchPolicySchema,
  fetchVocabulary,
  listPolicies,
  updatePolicy,
} from '../services/policyService';
import type { PolicyDocument, PolicyRow, PolicyVocabulary } from '../types/policy';

/** A worked example rather than an empty object: the shape is not guessable from the schema. */
const TEMPLATE = `{
  "id": "rack-operators",
  "description": "Power-cycle any rack, except the critical row",
  "statements": [
    {
      "sid": "power",
      "effect": "allow",
      "actions": ["rack.power:*"],
      "resources": ["rack.power:*"]
    },
    {
      "sid": "protect-critical",
      "effect": "deny",
      "actions": ["rack.power:*"],
      "resources": ["rack.power:critical-*"]
    }
  ]
}`;

export default function PoliciesView() {
  const [rows, setRows] = useState<PolicyRow[] | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const [schema, setSchema] = useState<object | undefined>();
  const [vocabulary, setVocabulary] = useState<PolicyVocabulary | undefined>();

  const [editing, setEditing] = useState<{ id: string | null; text: string } | null>(null);
  const [saving, setSaving] = useState(false);

  const [attaching, setAttaching] = useState<PolicyRow | null>(null);
  const [subject, setSubject] = useState('');
  const [subjects, setSubjects] = useState<string[]>([]);

  const [deleting, setDeleting] = useState<PolicyRow | null>(null);

  const refresh = useCallback(async () => {
    try {
      setRows(await listPolicies());
      setError('');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    void refresh();
    // Schema and vocabulary come from the server so the editor cannot disagree with it. Failing
    // to load them is not fatal: the editor still works, it just validates less, and saying so
    // beats blocking authoring on a diagnostic endpoint.
    void fetchPolicySchema().then(setSchema).catch(() => undefined);
    void fetchVocabulary().then(setVocabulary).catch(() => undefined);
  }, [refresh]);

  useEffect(() => {
    if (!attaching) return;
    void Promise.all([listUsers(), listGroups()])
      .then(([users, groups]) => {
        setSubjects([
          ...users.map((u) => `user:${u.login}`),
          ...groups.map((g) => `group:${g.name}`),
        ]);
      })
      .catch(() => setSubjects([]));
  }, [attaching]);

  const parsed = useMemo(() => {
    if (!editing) return null;
    try {
      return JSON.parse(editing.text) as PolicyDocument;
    } catch {
      return null;
    }
  }, [editing]);

  async function save() {
    if (!editing || !parsed) return;
    setSaving(true);
    try {
      if (editing.id) {
        await updatePolicy(editing.id, parsed);
        setNotice(`Saved ${editing.id}.`);
      } else {
        await createPolicy(parsed);
        setNotice(`Created ${parsed.id}.`);
      }
      setEditing(null);
      await refresh();
    } catch (err) {
      // The server's message, verbatim: it names the field and the rule that refused it.
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  }

  async function confirmDelete(force: boolean) {
    if (!deleting) return;
    try {
      const detached = await deletePolicy(deleting.id, force);
      setNotice(
        detached.length
          ? `Deleted ${deleting.id} and detached it from ${detached.length} subject(s).`
          : `Deleted ${deleting.id}.`,
      );
      setDeleting(null);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <Box sx={{ p: 3 }}>
      <Flex style={{ justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
        <Text kind="title/lg">Policies</Text>
        <Button
          kind="primary"
          size="small"
          onClick={() => setEditing({ id: null, text: TEMPLATE })}
        >
          <Plus size={14} style={{ marginRight: 6 }} />
          New policy
        </Button>
      </Flex>

      <Banner status="info" kind="inline">
        Evaluation starts at an implicit deny. Any matching deny wins outright, and an allow only
        applies where it matches — so a policy grants nothing until it is attached to a user or a
        group.
      </Banner>

      {error && (
        <Box sx={{ mt: 2 }}>
          <Banner status="error">{error}</Banner>
        </Box>
      )}
      {notice && (
        <Box sx={{ mt: 2 }}>
          <Banner status="success">{notice}</Banner>
        </Box>
      )}

      <Card style={{ marginTop: 16, padding: 0 }}>
        {rows === null ? (
          <Box sx={{ p: 2 }}>
            <Skeleton style={{ height: 120 }} />
          </Box>
        ) : rows.length === 0 ? (
          <Box sx={{ p: 3 }}>
            <Text kind="body/regular/sm" style={{ opacity: 0.75 }}>
              No policies yet. With none, every authenticated request is permitted and the audit
              trail records the engine as permissive — write one to start enforcing.
            </Text>
          </Box>
        ) : (
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>Policy</TableCell>
                <TableCell>Statements</TableCell>
                <TableCell>Attached to</TableCell>
                <TableCell align="right">Actions</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {rows.map((row) => {
                const denies = row.statements.filter((s) => s.effect === 'deny').length;
                return (
                  <TableRow key={row.id} hover>
                    <TableCell>
                      <Text kind="mono/sm">{row.id}</Text>
                      {row.description && (
                        <Text kind="body/regular/sm" style={{ opacity: 0.7, display: 'block' }}>
                          {row.description}
                        </Text>
                      )}
                    </TableCell>
                    <TableCell>
                      <Flex style={{ gap: 6, alignItems: "center" }}>
                        <Tag color="gray" kind="outline" readOnly density="compact">
                          {row.statements.length}
                        </Tag>
                        {denies > 0 && (
                          // Called out because a deny cannot be outvoted by any number of allows,
                          // so a policy that reads as narrow can shut off a whole verb.
                          <Tag color="red" kind="solid" readOnly density="compact">
                            <ShieldAlert size={11} style={{ marginRight: 4 }} />
                            {denies} deny
                          </Tag>
                        )}
                      </Flex>
                    </TableCell>
                    <TableCell>
                      {row.attachment_count === 0 ? (
                        <Text kind="body/regular/sm" style={{ opacity: 0.6 }}>
                          not attached — grants nothing
                        </Text>
                      ) : (
                        <Flex style={{ gap: 4, flexWrap: "wrap" }}>
                          {row.attached_to.map((s) => (
                            <Tag key={s} color="blue" kind="outline" readOnly density="compact">
                              {s}
                            </Tag>
                          ))}
                        </Flex>
                      )}
                    </TableCell>
                    <TableCell align="right">
                      <Flex style={{ gap: 6, justifyContent: "flex-end" }}>
                        <Button
                          kind="tertiary"
                          size="tiny"
                          onClick={() =>
                            setEditing({
                              id: row.id,
                              text: JSON.stringify(
                                { id: row.id, description: row.description, statements: row.statements },
                                null,
                                2,
                              ),
                            })
                          }
                        >
                          Edit
                        </Button>
                        <Button kind="tertiary" size="tiny" onClick={() => setAttaching(row)}>
                          Attach
                        </Button>
                        {/* `aria-label` + `title`: the only icon-only control on this row, and
                            it is the destructive one. Without a label a screen reader announces
                            it as "button" and a hover reveals nothing, so the single action that
                            can revoke somebody's access is the single action with no name. */}
                        <Button
                          kind="tertiary"
                          size="tiny"
                          color="danger"
                          aria-label={`Delete policy ${row.id}`}
                          title={`Delete policy ${row.id}`}
                          onClick={() => setDeleting(row)}
                        >
                          <Trash2 size={12} />
                        </Button>
                      </Flex>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        )}
      </Card>

      {/* Editor */}
      <Dialog open={editing !== null} onClose={() => setEditing(null)} maxWidth="md" fullWidth>
        <DialogTitle>{editing?.id ? `Edit ${editing.id}` : 'New policy'}</DialogTitle>
        <DialogContent>
          {!schema && (
            <Box sx={{ mb: 1 }}>
              <Banner status="warning" kind="inline">
                The policy schema could not be loaded, so the editor cannot validate as you type.
                The server still validates on save.
              </Banner>
            </Box>
          )}
          <PolicyEditor
            value={editing?.text ?? ''}
            onChange={(text) => setEditing((prev) => (prev ? { ...prev, text } : prev))}
            schema={schema}
            actions={vocabulary?.actions}
          />
          {editing && !parsed && (
            <Box sx={{ mt: 1 }}>
              <Banner status="error" kind="inline">
                Not valid JSON yet.
              </Banner>
            </Box>
          )}
        </DialogContent>
        <DialogActions>
          <Button kind="tertiary" size="small" onClick={() => setEditing(null)}>
            Cancel
          </Button>
          <Button
            kind="primary"
            size="small"
            disabled={!parsed || saving}
            onClick={() => void save()}
          >
            {saving ? 'Saving…' : 'Save'}
          </Button>
        </DialogActions>
      </Dialog>

      {/* Attach */}
      <Dialog open={attaching !== null} onClose={() => setAttaching(null)} maxWidth="sm" fullWidth>
        <DialogTitle>Attach {attaching?.id}</DialogTitle>
        <DialogContent>
          <Text kind="body/regular/sm" style={{ opacity: 0.75 }}>
            A policy grants nothing until it is attached. Attaching to a group reaches every member.
          </Text>
          <TextField
            select
            fullWidth
            size="small"
            label="User or group"
            value={subject}
            onChange={(e) => setSubject(e.target.value)}
            sx={{ mt: 2 }}
          >
            {subjects.map((s) => (
              <MenuItem key={s} value={s}>
                {s}
              </MenuItem>
            ))}
          </TextField>
          {attaching && attaching.attached_to.length > 0 && (
            <Box sx={{ mt: 2 }}>
              <Text kind="body/regular/sm" style={{ opacity: 0.7 }}>
                Already attached to: {attaching.attached_to.join(', ')}
              </Text>
            </Box>
          )}
        </DialogContent>
        <DialogActions>
          <Button kind="tertiary" size="small" onClick={() => setAttaching(null)}>
            Cancel
          </Button>
          {attaching && attaching.attached_to.includes(subject) ? (
            <Button
              kind="secondary"
              size="small"
              onClick={async () => {
                try {
                  await detachPolicy(attaching.id, subject);
                  setNotice(`Detached ${attaching.id} from ${subject}.`);
                  setAttaching(null);
                  await refresh();
                } catch (err) {
                  setError(err instanceof Error ? err.message : String(err));
                }
              }}
            >
              Detach
            </Button>
          ) : (
            <Button
              kind="primary"
              size="small"
              disabled={!subject}
              onClick={async () => {
                if (!attaching) return;
                try {
                  await attachPolicy(attaching.id, subject);
                  setNotice(`Attached ${attaching.id} to ${subject}.`);
                  setAttaching(null);
                  setSubject('');
                  await refresh();
                } catch (err) {
                  setError(err instanceof Error ? err.message : String(err));
                }
              }}
            >
              Attach
            </Button>
          )}
        </DialogActions>
      </Dialog>

      {/* Delete */}
      <Dialog open={deleting !== null} onClose={() => setDeleting(null)} maxWidth="sm" fullWidth>
        <DialogTitle>Delete {deleting?.id}?</DialogTitle>
        <DialogContent>
          {deleting && deleting.attachment_count > 0 ? (
            <Banner status="warning">
              This policy is attached to {deleting.attachment_count} subject(s):{' '}
              {deleting.attached_to.join(', ')}. Deleting it changes what they can do.
            </Banner>
          ) : (
            <Text kind="body/regular/sm">
              This policy is not attached to anyone, so deleting it changes no one&apos;s access.
            </Text>
          )}
        </DialogContent>
        <DialogActions>
          <Button kind="tertiary" size="small" onClick={() => setDeleting(null)}>
            Cancel
          </Button>
          <Button
            kind="primary"
            size="small"
            color="danger"
            onClick={() => void confirmDelete((deleting?.attachment_count ?? 0) > 0)}
          >
            {deleting && deleting.attachment_count > 0
              ? `Delete and detach from ${deleting.attachment_count}`
              : 'Delete'}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
