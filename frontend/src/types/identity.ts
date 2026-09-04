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

/** Shapes returned by `/api/identity`. Mirrors `identity.models.User.public_dict()`. */

export interface IdentityUser {
  /** The principal. Immutable, unique case-insensitively, and never reused (FR-2, FR-2b). */
  login: string;
  /** Presentation only — not an identifier, not unique, freely editable (FR-2c). */
  display_name: string;
  disabled: boolean;
  must_change_password: boolean;
  auth_source: string;
  /**
   * Contact attributes, named for the OpenID Connect Core §5.1 standard claims. Required when
   * creating a user; empty on the bootstrap admin and on any account predating the field, which is
   * why the table flags a missing address rather than assuming there is one.
   */
  email: string;
  phone_number: string;
  /**
   * Whether this deployment asserts the address was verified. Set only by an explicit admin action,
   * and cleared automatically when the address changes — a stale assertion is what lets a relying
   * party link the wrong accounts.
   */
  email_verified: boolean;
  phone_number_verified: boolean;
  /**
   * The rest of the OIDC Core §5.1 profile set. `zoneinfo` is an IANA zone name and `locale` an
   * RFC 5646 tag stored in conventional casing (`en-US`), both normalised server-side.
   */
  given_name: string;
  family_name: string;
  zoneinfo: string;
  locale: string;
  created_at?: string;
  updated_at?: string;
  /** Group names this user belongs to. Server-side join, so the list is one request. */
  groups: string[];
}

export interface IdentityGroup {
  name: string;
  description: string;
  members: string[];
}

/**
 * A one-time password, returned by creation and by reset.
 *
 * **Present in exactly one response and never retrievable.** The server stores only its SRP
 * verifier, so a UI that discards this value without showing it has silently created an account
 * nobody can use.
 */
export interface IssuedCredential {
  login: string;
  password: string;
}
