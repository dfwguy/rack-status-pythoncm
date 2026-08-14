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
import { describe, expect, it } from "vitest";
import { parseSqueueStdout } from "./parseSqueueStdout";

describe("parseSqueueStdout", () => {
  it("keeps a parenthesized multi-word REASON in the last column", () => {
    const out = [
      "             JOBID PARTITION     NAME     USER ST       TIME  NODES NODELIST(REASON)",
      "                 7      defq MNUBERGEMM    cmods PD       0:00     18 (launch failed requeued held)",
    ].join("\n");
    const { headers, rows, useRaw } = parseSqueueStdout(out);
    expect(useRaw).toBe(false);
    expect(headers).toHaveLength(8);
    expect(rows).toHaveLength(1);
    const row = rows[0]!;
    expect(row).toHaveLength(8);
    expect(row[0]).toBe("7");
    expect(row[1]).toBe("defq");
    expect(row[2]).toBe("MNUBERGEMM");
    expect(row[3]).toBe("cmods");
    expect(row[4]).toBe("PD");
    expect(row[6]).toBe("18");
    expect(row[7]).toBe("(launch failed requeued held)");
  });

  it("handles a running job whose NODELIST is a host list (single token)", () => {
    const out = [
      "             JOBID PARTITION     NAME     USER ST       TIME  NODES NODELIST(REASON)",
      "                 7      defq MNUBERGEMM    cmods  R       0:02     18 bos01-b05-11-c01,bos01-b05-12-c02",
    ].join("\n");
    const { rows } = parseSqueueStdout(out);
    expect(rows[0]).toHaveLength(8);
    expect(rows[0]![4]).toBe("R");
    expect(rows[0]![7]).toBe("bos01-b05-11-c01,bos01-b05-12-c02");
  });

  it("still folds a multi-word NAME when there is no parenthesized reason", () => {
    const out = [
      "JOBID PARTITION     NAME     USER ST       TIME  NODES NODELIST(REASON)",
      "    9      defq my job name cmods  R       1:00      2 node01",
    ].join("\n");
    const { rows } = parseSqueueStdout(out);
    expect(rows[0]).toHaveLength(8);
    expect(rows[0]![2]).toBe("my job name");
    expect(rows[0]![7]).toBe("node01");
  });
});
