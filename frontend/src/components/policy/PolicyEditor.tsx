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
  onChangeRef.current = onChange;

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

  return (
    <div
      ref={hostRef}
      data-testid="policy-editor"
      style={{ height, border: '1px solid var(--kui-color-border-subtle, #444)' }}
    />
  );
}
