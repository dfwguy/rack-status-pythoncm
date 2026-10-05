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
// P2P shim over the host Rack Status `apiFetch`.
//
// The standalone console had its own `apiFetch` that spoke to Dragonfly Manager
// with a `jwt` cookie and its own dedupe/renewal. Inside Rack Status that job
// belongs to one citizen: `src/lib/apiFetch` already injects the operator
// headers, carries the same-origin session cookie, dedupes in-flight GETs, and
// transparently renews a 401. So this module keeps only the P2P-flavoured JSON
// envelope the feature's services and hooks expect -- `apiJson<T>()` and the
// `ApiError` with `isUnauthenticated` -- and delegates every actual request to
// the host wrapper. There is no second cookie and no second renewal path.
import { apiFetch as hostApiFetch } from "../../../lib/apiFetch";

/** Thrown for any non-2xx response, carrying the status so callers can branch. */
export class ApiError extends Error {
  readonly status: number;
  readonly body: unknown;

  constructor(status: number, message: string, body: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }

  /** The session is gone (or the P2P role is missing); the operator must re-auth. */
  get isUnauthenticated(): boolean {
    return this.status === 401 || this.status === 403;
  }
}

/** Fetch an API path through the host wrapper. Resolves the `Response`. */
export function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  return hostApiFetch(path, init);
}

/** Fetch and parse JSON, raising `ApiError` on a non-2xx response. */
export async function apiJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await hostApiFetch(path, {
    cache: "no-store",
    ...init,
    headers: {
      Accept: "application/json",
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
  });
  const text = await response.text();
  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }
  if (!response.ok) {
    const message =
      (body && typeof body === "object" && "message" in body
        ? String((body as { message: unknown }).message)
        : null) ?? response.statusText;
    throw new ApiError(response.status, message, body);
  }
  return body as T;
}
