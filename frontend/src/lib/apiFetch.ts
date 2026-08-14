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
 * Thin wrapper around the browser ``fetch`` that injects the rack-operator headers
 * (display name + stable client id) on every request. Use this in place of raw ``fetch``
 * anywhere the SPA talks to the Flask backend so server-side rack-lock tracking can
 * attribute operations to the right operator.
 */
import { getOperatorName } from "./getOperatorName";

const CLIENT_ID_KEY = "nvis-rack-client-id";

let _clientId: string | null = null;

/** Stable per-browser client id (matches the existing one used by useRackStatus). */
export function getClientId(): string {
  if (_clientId) return _clientId;
  try {
    let cached = localStorage.getItem(CLIENT_ID_KEY);
    if (!cached) {
      cached = `spa-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
      localStorage.setItem(CLIENT_ID_KEY, cached);
    }
    _clientId = cached;
  } catch {
    _clientId = `spa-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
  }
  return _clientId;
}

/** ``fetch`` replacement that adds X-Rack-Operator + X-Rack-Client-Id headers. */
export function apiFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const op = getOperatorName();
  const cid = getClientId();
  const headers = new Headers(init?.headers || {});
  if (op && !headers.has("X-Rack-Operator")) {
    headers.set("X-Rack-Operator", op);
  }
  if (cid && !headers.has("X-Rack-Client-Id")) {
    headers.set("X-Rack-Client-Id", cid);
  }
  return fetch(input, {
    credentials: "same-origin",
    ...init,
    headers,
  });
}
