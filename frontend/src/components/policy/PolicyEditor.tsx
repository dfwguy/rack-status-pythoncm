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
 * Monaco, bundled and JSON-only, validating against the schema the server serves.
 *
 * NO `@monaco-editor/react`, AND THAT IS DELIBERATE — measured, not assumed. The wrapper is the
 * obvious choice and it cost two things this product cannot pay:
 *
 *  1. **It defaults to a CDN.** `@monaco-editor/loader` resolves
 *     `https://cdn.jsdelivr.net/npm/monaco-editor@.../min/vs`, so the editor never appears on an
 *     air-gapped host. `loader.config({ monaco })` is supposed to override that, but the built
 *     chunk *still contained the jsdelivr URL* — so whether the CDN path could ever be taken
 *     became a question to reason about rather than a fact. `nvis-rack-status.mdc` already forbids
 *     CDN links for exactly this reason, after the Kaizen CSS paid for it once. Instantiating
 *     Monaco directly removes the code path instead of configuring around it.
 *  2. **It pulls every language.** The wrapper's entry drags in the TypeScript, CSS and HTML
 *     workers: the SPA went from 8.1 MB to 22 MB, of which ~7.6 MB was `ts.worker` and
 *     `css.worker` — for an editor that only ever opens JSON.
 *
 * Importing `editor.api` plus the JSON contribution, and answering `MonacoEnvironment.getWorker`
 * with only the JSON worker, keeps both the language service and the bundle honest.
 *
 * The JSON Schema comes from `GET /api/policies/schema` rather than being written here, so the
 * editor and the server cannot disagree about what a valid document is — including the
 * bare-`*`-in-allow rule, which pydantic expresses as a validator and the schema export bridges as
 * a conditional. Without it the editor would accept an accidental grant of every action the
 * product will ever have, and only the server would object, at submit time.
 */

import * as monaco from 'monaco-editor/editor/editor.api.js';
import { jsonDefaults } from 'monaco-editor/language/json/monaco.contribution.js';
import JsonWorker from 'monaco-editor/language/json/json.worker.js?worker';
import { useEffect, useRef } from 'react';

/**
 * Only the JSON worker, and a hard failure for anything else.
 *
 * Returning a generic worker for an unexpected label would leave a broken language service that
 * looks like a working one; throwing means a future editor for another language is a startup error
 * naming the label, not a silent loss of validation.
 */
declare global {
  interface Window {
    MonacoEnvironment?: monaco.Environment;
  }
}

window.MonacoEnvironment = {
  getWorker(_moduleId: string, label: string) {
    if (label === 'json') return new JsonWorker();
    throw new Error(`policy editor: no Monaco worker bundled for ${label}`);
  },
};

const SCHEMA_URI = 'inmemory://greenlight/policy-schema.json';

export interface PolicyEditorProps {
  value: string;
  onChange: (next: string) => void;
  /** JSON Schema from `GET /api/policies/schema`. Validation stays off until it arrives. */
  schema?: object;
  /** Action names from `GET /api/policies/vocabulary`, for completion. */
  actions?: string[];
  height?: number | string;
  readOnly?: boolean;
}

export default function PolicyEditor({
  value,
  onChange,
  schema,
  actions = [],
  height = 420,
  readOnly = false,
}: PolicyEditorProps) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const editorRef = useRef<monaco.editor.IStandaloneCodeEditor | null>(null);
  const completionRef = useRef<monaco.IDisposable | null>(null);
  const onChangeRef = useRef(onChange);
  // Updated in an effect, not during render: a ref write during render is what
  // `react-hooks/set-state-in-effect`'s sibling rule "Cannot access refs during render" refuses,
  // and it is the one hard eslint error in this project's config. Safe here because the ref is only
  // ever *read* from the editor's change callback, which cannot fire before the first commit, and
  // `useRef(onChange)` already seeds it with the initial handler.
  useEffect(() => {
    onChangeRef.current = onChange;
  }, [onChange]);

  useEffect(() => {
    if (!hostRef.current) return;

    const editor = monaco.editor.create(hostRef.current, {
      value,
      language: 'json',
      readOnly,
      minimap: { enabled: false },
      scrollBeyondLastLine: false,
      tabSize: 2,
      automaticLayout: true,
      fontSize: 13,
    });
    editorRef.current = editor;

    // Through a ref, so a new `onChange` identity each render does not tear down the editor and
    // lose the cursor position mid-typing.
    const subscription = editor.onDidChangeModelContent(() => {
      onChangeRef.current(editor.getValue());
    });

    return () => {
      subscription.dispose();
      editor.getModel()?.dispose();
      editor.dispose();
      editorRef.current = null;
    };
    // Mount once. `value` is applied on mount and reconciled by the effect below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Reconcile external value changes without fighting the user's cursor.
  useEffect(() => {
    const editor = editorRef.current;
    if (editor && value !== editor.getValue()) editor.setValue(value);
  }, [value]);

  // Schema registration is global to the JSON language service, not per-editor, so it is applied
  // whenever the served schema arrives rather than passed as a prop.
  useEffect(() => {
    if (!schema) return;
    jsonDefaults.setDiagnosticsOptions({
      validate: true,
      // Never resolve a schema over the network: a `$ref` fetch would silently stop validating on
      // an air-gapped host, which is the failure this whole file is arranged to avoid.
      enableSchemaRequest: false,
      schemas: [{ uri: SCHEMA_URI, fileMatch: ['*'], schema }],
    });
  }, [schema]);

  // Completion over the real vocabulary. Worth the wiring: it is 253 entries and nobody can be
  // expected to remember `fabric.nvswitch:reset_sdn`, so without it the editor validates typos it
  // could have prevented.
  useEffect(() => {
    if (!actions.length) return;
    completionRef.current?.dispose();
    completionRef.current = monaco.languages.registerCompletionItemProvider('json', {
      provideCompletionItems: (model, position) => {
        const preceding = model.getValueInRange({
          startLineNumber: Math.max(1, position.lineNumber - 8),
          startColumn: 1,
          endLineNumber: position.lineNumber,
          endColumn: position.column,
        });
        // Only inside an `actions` array. Offering action names for a resource would teach the
        // wrong shape: a resource is `<type>:<id>`, an action is `<type>:<verb>`.
        if (preceding.lastIndexOf('"actions"') <= preceding.lastIndexOf('"resources"')) {
          return { suggestions: [] };
        }
        const word = model.getWordUntilPosition(position);
        return {
          suggestions: actions.map((name) => ({
            label: name,
            kind: monaco.languages.CompletionItemKind.Value,
            insertText: name,
            range: {
              startLineNumber: position.lineNumber,
              endLineNumber: position.lineNumber,
              startColumn: word.startColumn,
              endColumn: word.endColumn,
            },
          })),
        };
      },
    });
    return () => completionRef.current?.dispose();
  }, [actions]);

  // Semantic markers for the two things JSON Schema cannot express, and both are real gaps rather
  // than polish:
  //
  //  * an **unknown action** validates structurally and grants nothing, so a typo in
  //    `rack.power:of` produces a policy that looks correct, saves cleanly, and quietly denies.
  //    The vocabulary was already loaded for completion — and completion is not validation: it
  //    helps someone who types a prefix and does nothing for someone who pastes (AC-15g2).
  //  * a **duplicate `sid`** is refused by the server and accepted by the schema, because JSON
  //    Schema has `uniqueItems` for whole items and no keyword for uniqueness by property. Without
  //    a check here the operator only finds out on save.
  useEffect(() => {
    const editor = editorRef.current;
    const model = editor?.getModel();
    if (!model) return;

    const known = new Set(actions);

    const mark = () => {
      const text = model.getValue();
      const markers: monaco.editor.IMarkerData[] = [];

      // Parsed, not regex-scanned: a string or comment containing `"actions"` would fool a scan,
      // and a marker in the wrong place is worse than no marker. Malformed JSON is already
      // reported by the schema validator, so this simply stays quiet for it.
      type PolicyShape = { statements?: { sid?: string; actions?: string[] }[] };
      const parse = (): PolicyShape | null => {
        try {
          return JSON.parse(text) as PolicyShape;
        } catch {
          return null;
        }
      };
      const doc = parse();

      const rangeOf = (needle: string) => {
        const at = text.indexOf(`"${needle}"`);
        if (at < 0) return null;
        const start = model.getPositionAt(at);
        const end = model.getPositionAt(at + needle.length + 2);
        return {
          startLineNumber: start.lineNumber,
          startColumn: start.column,
          endLineNumber: end.lineNumber,
          endColumn: end.column,
        };
      };

      const statements = doc?.statements ?? [];
      const seen = new Set<string>();

      for (const statement of statements) {
        const sid = statement?.sid;
        if (typeof sid === 'string' && sid) {
          if (seen.has(sid)) {
            const range = rangeOf(sid);
            if (range) {
              markers.push({
                ...range,
                severity: monaco.MarkerSeverity.Error,
                message: `Duplicate statement id "${sid}". The server refuses this on save.`,
              });
            }
          }
          seen.add(sid);
        }

        for (const action of statement?.actions ?? []) {
          if (typeof action !== 'string' || !action) continue;
          // A wildcard cannot be checked against the vocabulary; that is what it is for.
          if (action.includes('*')) continue;
          if (known.size > 0 && !known.has(action)) {
            const range = rangeOf(action);
            if (range) {
              markers.push({
                ...range,
                severity: monaco.MarkerSeverity.Warning,
                message: `Unknown action "${action}". It matches nothing, so this statement grants nothing.`,
              });
            }
          }
        }
      }

      monaco.editor.setModelMarkers(model, 'greenlight-policy', markers);
    };

    mark();
    const subscription = model.onDidChangeContent(mark);
    return () => {
      subscription.dispose();
      monaco.editor.setModelMarkers(model, 'greenlight-policy', []);
    };
  }, [actions]);

  return (
    <div
      ref={hostRef}
      data-testid="policy-editor"
      style={{ height, border: '1px solid var(--kui-color-border-subtle, #444)' }}
    />
  );
}
