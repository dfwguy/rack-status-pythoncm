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

/** Shapes returned by `/api/policies`. Mirrors `authz.schema.PolicyDocument`. */

export type PolicyEffect = 'allow' | 'deny';

export interface PolicyStatement {
  /** Optional name, so a refusal can say `policy/statement` rather than just the policy. */
  sid?: string;
  effect: PolicyEffect;
  /** Action patterns, e.g. `rack.power:*`. A bare `*` is refused in an allow. */
  actions: string[];
  /** Resource patterns, e.g. `rack.power:a01` or `rack.power:_all` for the collection. */
  resources: string[];
}

export interface PolicyDocument {
  id: string;
  description?: string;
  statements: PolicyStatement[];
}

/** A policy as the list endpoint returns it — the document plus who has it. */
export interface PolicyRow extends PolicyDocument {
  /** `user:<login>` and `group:<name>` subjects this policy is attached to. */
  attached_to: string[];
  attachment_count: number;
}

/**
 * What a policy may name, from `GET /api/policies/vocabulary`.
 *
 * Served rather than hard-coded here: a frontend list is wrong the first time somebody adds an
 * endpoint, and the failure is an editor marking a valid action unknown, which teaches operators
 * to ignore the validation.
 */
export interface PolicyVocabulary {
  actions: string[];
  services: string[];
  resource_types: string[];
  /** The id a `:list` verb resolves to, e.g. `_all` in `rack:_all`. */
  collection_id: string;
  /** The id a one-per-deployment resource resolves to, e.g. `default` in `settings:default`. */
  singleton_id: string;
}
