/*
 * SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
 * SPDX-License-Identifier: LicenseRef-NvidiaProprietary
 *
 * NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
 * property and proprietary rights in and to this material, related
 * documentation and any modifications thereto. Any use, reproduction,
 * disclosure or distribution of this material and related documentation
 * without an express license agreement from NVIDIA CORPORATION or
 * its affiliates is strictly prohibited.
 */
// Author: Rafael M. Koike
//
// The stage ladder of a rack-staged rollout, edited as text ("1, 50%, 100%") but held as a list.
// The typed text is kept locally so a half-typed rung ("1, ") is not reparsed away under the cursor;
// when the list changes from outside (a reload, a save), the input shows the new list instead.
import { useState } from "react";
import { FormField, TextInput } from "@nvidia/foundations-react-core";

import { formatStages, parseStages, stagesProblem } from "../lib/rolloutStages";

const same = (a: readonly string[], b: readonly string[]) => a.length === b.length && a.every((x, i) => x === b[i]);

export function StageLadderInput({
  label, value, onChange, help, inherits = false, placeholder,
}: {
  label: string;
  value: string[];
  onChange: (stages: string[]) => void;
  help?: string;
  /** Empty means "use the cluster's ladder" (a campaign override), so it is not an error. */
  inherits?: boolean;
  placeholder?: string;
}) {
  const [text, setText] = useState(formatStages(value));
  const shown = same(parseStages(text), value) ? text : formatStages(value);
  const problem = stagesProblem(parseStages(shown), { allowEmpty: inherits });
  return (
    <FormField
      slotLabel={label}
      slotHelp={problem ? undefined : help}
      status={problem ? "error" : undefined}
      slotError={problem ?? undefined}
    >
      <TextInput
        aria-label={label}
        value={shown}
        placeholder={placeholder}
        onValueChange={(v) => {
          setText(v);
          onChange(parseStages(v));
        }}
      />
    </FormField>
  );
}
