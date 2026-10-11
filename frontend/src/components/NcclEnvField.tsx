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
/**
 * One of a cluster's NCCL environment lists, edited as `KEY=VALUE` lines.
 *
 * The same syntax and the same name rule as the launch dialog's "Extra environment variables"
 * (`parseEnvVars`), because both end in the same job environment. The text is held here rather than
 * re-rendered from the parsed map: a line half-typed (`NCCL_` with no `=` yet) is not a variable, and
 * re-rendering from the map would delete it under the operator's cursor.
 */
import { useState } from "react";
import { FormField, TextArea } from "@nvidia/foundations-react-core";
import { mapToText } from "../lib/catalogForm";
import { parseEnvVars } from "../lib/rackCommandBodies";

export interface NcclEnvFieldProps {
  name: string;
  label: string;
  /** What this span is, for the operator: which launches receive the list. */
  help: string;
  value: Record<string, string>;
  onChange: (env: Record<string, string>) => void;
}

/** Lines that hold something but are not a `KEY=VALUE` the job will receive. */
function ignoredLines(text: string, parsed: Record<string, string>): number {
  const meaningful = text.split("\n").filter((l) => l.trim() && !l.trim().startsWith("#")).length;
  return Math.max(0, meaningful - Object.keys(parsed).length);
}

export function NcclEnvField({ name, label, help, value, onChange }: NcclEnvFieldProps) {
  const [text, setText] = useState(() => mapToText(value));
  const parsed = parseEnvVars(text);
  const count = Object.keys(parsed).length;
  const ignored = ignoredLines(text, parsed);
  const summary =
    (count ? `${count} variable${count === 1 ? "" : "s"}.` : "None: the scripts' defaults.") +
    (ignored ? ` ${ignored} line${ignored === 1 ? "" : "s"} ignored (not KEY=VALUE).` : "");
  return (
    <FormField name={name} slotLabel={label} slotHelp={`${help} ${summary}`}>
      <TextArea
        value={text}
        onValueChange={(v: string) => {
          setText(v);
          onChange(parseEnvVars(v));
        }}
        resizeable="auto"
        placeholder={"NCCL_NET_PLUGIN=/path/libnccl-net-spcx.so\nNCCL_IB_DYNAMIC_FLOW=1"}
        style={{ fontFamily: "monospace", fontSize: 13 }}
      />
    </FormField>
  );
}
