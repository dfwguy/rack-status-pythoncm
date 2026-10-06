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
 * SRP-6a client — the browser half of the login exchange.
 *
 * **The password never leaves this function.** That is the entire point: the server receives a
 * proof of knowledge, so the password cannot appear in a proxy log, a server memory dump, or a
 * crash report. TLS protects the wire; this protects everything the wire terminates into.
 *
 * ## Why this is written out rather than pulled from npm
 *
 * The parameters have to match `backend/identity/srp.py` exactly — SHA-256, the RFC 5054 4096-bit
 * group, and RFC 5054 §2.6 padding **on**. A mismatch in any of them does not raise: it produces a
 * proof that fails to verify, so every login looks precisely like a wrong password. That failure
 * has already cost this project time once (see `identity-auth-traps.mdc`), and auditing a
 * third-party library's padding semantics against ours is most of the work of writing it.
 *
 * No primitive is invented here. SHA-256 comes from Web Crypto and the only algorithm is modular
 * exponentiation. Correctness is pinned by `srpClient.test.ts`, which replays a vector generated
 * from the server's own implementation — if the two ever disagree, that test fails rather than
 * every operator's login.
 *
 * ## The detail most likely to be got wrong
 *
 * Padding is **not** uniform across the computation, and the asymmetry is easy to miss:
 *
 * - `u = H(PAD(A), PAD(B))` — both padded to the byte length of N;
 * - `k = H(N, PAD(g))` — g padded to the byte length of N;
 * - `M = H(H(N) xor H(PAD(g)), H(I), s, A, B, K)` — A and B **unpadded** here.
 *
 * Using padded A and B in `M` yields a proof the server rejects, and the symptom is a wrong
 * password. The vector test is what makes this safe to touch.
 */

const encoder = new TextEncoder();

/** RFC 5054 Appendix A, 4096-bit group. Must equal `srp.NG_4096` on the server. */
const N_HEX = [
  "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74",
  "020BBEA63B139B22514A08798E3404DDEF9519B3CD3A431B302B0A6DF25F1437",
  "4FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED",
  "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3DC2007CB8A163BF05",
  "98DA48361C55D39A69163FA8FD24CF5F83655D23DCA3AD961C62F356208552BB",
  "9ED529077096966D670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B",
  "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF695581718",
  "3995497CEA956AE515D2261898FA051015728E5A8AAAC42DAD33170D04507A33",
  "A85521ABDF1CBA64ECFB850458DBEF0A8AEA71575D060C7DB3970F85A6E1E4C7",
  "ABF5AE8CDB0933D71E8C94E04A25619DCEE3D2261AD2EE6BF12FFA06D98A0864",
  "D87602733EC86A64521F2B18177B200CBBE117577A615D6C770988C0BAD946E2",
  "08E24FA074E5AB3143DB5BFCE0FD108E4B82D120A92108011A723C12A787E6D7",
  "88719A10BDBA5B2699C327186AF4E23C1A946834B6150BDA2583E9CA2AD44CE8",
  "DBBBC2DB04DE8EF92E8EFC141FBECAA6287C59474E6BC05D99B2964FA090C3A2",
  "233BA186515BE7ED1F612970CEE2D7AFB81BDD762170481CD0069127D5B05AA9",
  "93B4EA988D8FDDC186FFB7DC90A6C08F4DF435C934063199FFFFFFFFFFFFFFFF",
].join("");

const N = BigInt("0x" + N_HEX);
const g = 5n;

/** Byte length of N — the width every padded value is left-padded to. */
const N_BYTES = N_HEX.length / 2;

async function sha256(...parts: Uint8Array[]): Promise<Uint8Array> {
  let total = 0;
  for (const p of parts) total += p.length;
  const joined = new Uint8Array(total);
  let at = 0;
  for (const p of parts) {
    joined.set(p, at);
    at += p.length;
  }
  const digest = await crypto.subtle.digest("SHA-256", joined);
  return new Uint8Array(digest);
}

/**
 * Minimal big-endian encoding, matching Python's `long_to_bytes`: no leading zero bytes, and a
 * single zero byte for zero. Any extra leading zero changes every hash it feeds.
 */
function toBytes(value: bigint): Uint8Array {
  if (value === 0n) return new Uint8Array([0]);
  let hex = value.toString(16);
  if (hex.length % 2) hex = "0" + hex;
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  return out;
}

function toBigInt(bytes: Uint8Array): bigint {
  let hex = "";
  for (const b of bytes) hex += b.toString(16).padStart(2, "0");
  return hex ? BigInt("0x" + hex) : 0n;
}

/** Left-pad to `width` with zero bytes. This is RFC 5054 §2.6 PAD(). */
function pad(bytes: Uint8Array, width: number = N_BYTES): Uint8Array {
  if (bytes.length >= width) return bytes;
  const out = new Uint8Array(width);
  out.set(bytes, width - bytes.length);
  return out;
}

function fromHex(hex: string): Uint8Array {
  const clean = hex.length % 2 ? "0" + hex : hex;
  const out = new Uint8Array(clean.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(clean.slice(i * 2, i * 2 + 2), 16);
  return out;
}

function toHex(bytes: Uint8Array): string {
  let out = "";
  for (const b of bytes) out += b.toString(16).padStart(2, "0");
  return out;
}

/** `a ^ b`, byte-wise. Both operands are digests, so lengths always match. */
function xorBytes(a: Uint8Array, b: Uint8Array): Uint8Array {
  const out = new Uint8Array(a.length);
  for (let i = 0; i < a.length; i++) out[i] = a[i] ^ b[i];
  return out;
}

/** `base^exp mod mod`, square-and-multiply. BigInt has no modpow of its own. */
function modPow(base: bigint, exp: bigint, mod: bigint): bigint {
  let result = 1n;
  let b = base % mod;
  let e = exp;
  while (e > 0n) {
    if (e & 1n) result = (result * b) % mod;
    b = (b * b) % mod;
    e >>= 1n;
  }
  return result;
}

/** A cryptographically random private exponent. 32 bytes, as the reference implementations use. */
function randomPrivate(): bigint {
  const raw = new Uint8Array(32);
  crypto.getRandomValues(raw);
  return toBigInt(raw);
}

export interface SrpHandshake {
  /** `A`, hex — sent to `/api/auth/srp/begin`. */
  readonly A: string;
  /** Compute the proof for the server's challenge. Returns `M` as hex. */
  proof(saltHex: string, bHex: string): Promise<string>;
  /**
   * Verify the server's own proof. **Call this.** Skipping it means the client authenticated
   * itself to a server it never authenticated back — half of what mutual authentication is for,
   * and the half an attacker who can answer the request would love you to skip.
   */
  verifyServer(hamkHex: string): boolean;
}

/**
 * Begin an exchange. The returned object holds the ephemeral private value, so it must be used for
 * exactly one login and then dropped.
 */
export function startSrp(login: string, password: string, privateValue?: bigint): SrpHandshake {
  const a = privateValue ?? randomPrivate();
  const A = modPow(g, a, N);

  let expectedHamk: Uint8Array | null = null;

  return {
    A: toHex(toBytes(A)),

    async proof(saltHex: string, bHex: string): Promise<string> {
      const salt = fromHex(saltHex);
      const B = BigInt("0x" + bHex);

      // SRP-6a safety check: B ≡ 0 (mod N) means the server sent a value that would force the
      // session key to a constant. Refusing is the specified behaviour.
      if (B % N === 0n) throw new Error("SRP: server sent an invalid B");

      const u = toBigInt(await sha256(pad(toBytes(A)), pad(toBytes(B))));
      if (u === 0n) throw new Error("SRP: u is zero");

      const k = toBigInt(await sha256(toBytes(N), pad(toBytes(g))));

      // x = H(salt, H(login:password)) — no padding on this one.
      const inner = await sha256(encoder.encode(`${login}:${password}`));
      const x = toBigInt(await sha256(salt, inner));

      const v = modPow(g, x, N);
      // S = (B - k*v)^(a + u*x) mod N. The subtraction can go negative before reduction, and a
      // negative BigInt would break modPow, so it is normalised into [0, N).
      let base = (B - ((k * v) % N)) % N;
      if (base < 0n) base += N;
      const S = modPow(base, a + u * x, N);

      const K = await sha256(toBytes(S));

      // M = H( H(N) xor H(PAD(g)), H(login), salt, A, B, K ).
      // A and B are UNPADDED here even though `u` above padded them — matching the server's
      // `calculate_M`. This asymmetry is the single most likely place to break interoperability.
      const hN = await sha256(toBytes(N));
      const hg = await sha256(pad(toBytes(g)));
      const hI = await sha256(encoder.encode(login));
      const M = await sha256(xorBytes(hN, hg), hI, salt, toBytes(A), toBytes(B), K);

      expectedHamk = await sha256(toBytes(A), M, K);
      return toHex(M);
    },

    verifyServer(hamkHex: string): boolean {
      if (expectedHamk === null) return false;
      const given = fromHex(hamkHex);
      if (given.length !== expectedHamk.length) return false;
      // Constant-time-ish compare. The value is public once sent, but a short-circuit here is a
      // habit worth not forming.
      let diff = 0;
      for (let i = 0; i < given.length; i++) diff |= given[i] ^ expectedHamk[i];
      return diff === 0;
    },
  };
}

/**
 * Derive `(salt, verifier)` for a new password — the enrolment half of SRP, done in the browser.
 *
 * `v = g^x mod N` where `x = H(salt, H(login:password))`. The verifier is not reversible, so the
 * server can check a password it has never seen. This is why a password change can send only these
 * two values: the new password stays here, exactly as the old one did at login.
 */
export async function deriveVerifier(
  login: string,
  password: string,
): Promise<{ saltHex: string; verifierHex: string }> {
  // 16 bytes, matching `identity.srp.SALT_LEN`. The library's own default is 4 bytes — a 32-bit
  // salt — which is one of the unsafe defaults the server pins away from, and the client has to
  // pin away from it too or enrolment quietly weakens on this side instead.
  const salt = new Uint8Array(16);
  crypto.getRandomValues(salt);

  const inner = await sha256(encoder.encode(`${login}:${password}`));
  const x = toBigInt(await sha256(salt, inner));
  const v = modPow(g, x, N);

  return { saltHex: toHex(salt), verifierHex: toHex(toBytes(v)) };
}

export interface PasswordChangeResult {
  ok: boolean;
  error?: string;
}

/**
 * Change the signed-in user's password.
 *
 * Two legs like a login, and for the same reason: the first proves knowledge of the **current**
 * password. Being signed in is not sufficient — a stolen session would otherwise be enough to lock
 * the real owner out of their own account.
 *
 * On success the server revokes every session including this one, so the caller must sign in again.
 * That is deliberate rather than an oversight: a password change that left existing tokens working
 * would be cosmetic against the compromise it is usually a response to.
 */
export async function changePassword(
  login: string,
  currentPassword: string,
  newPassword: string,
): Promise<PasswordChangeResult> {
  const handshake = startSrp(login, currentPassword);

  const begin = await fetch("/api/auth/password/begin", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ A: handshake.A }),
  });
  if (!begin.ok) return { ok: false, error: "Current password is incorrect" };

  const challenge = await begin.json();
  if (!challenge?.salt || !challenge?.B || !challenge?.token) {
    return { ok: false, error: "Current password is incorrect" };
  }

  let proof: string;
  try {
    proof = await handshake.proof(challenge.salt, challenge.B);
  } catch {
    return { ok: false, error: "Current password is incorrect" };
  }

  const { saltHex, verifierHex } = await deriveVerifier(login, newPassword);

  const finish = await fetch("/api/auth/password/finish", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      token: challenge.token,
      M: proof,
      new_salt: saltHex,
      new_verifier: verifierHex,
    }),
  });
  if (!finish.ok) {
    // 401 means the current-password proof failed; anything else is the write failing, and the two
    // need different words because only one of them is the user's mistake.
    return {
      ok: false,
      error:
        finish.status === 401
          ? "Current password is incorrect"
          : "Could not change the password. Try again.",
    };
  }
  return { ok: true };
}

export interface LoginResult {
  ok: boolean;
  error?: string;
  user?: { login: string; display_name: string; principal: string };
  mustChangePassword?: boolean;
  /**
   * When the freshly minted access token expires, in epoch **seconds** (server clock). Control-plane
   * metadata, not a credential — the token itself stays in an `HttpOnly` cookie. The SPA holds this
   * so it can renew ahead of a 401 from its very first request.
   */
  accessExpiresAt?: number;
}

/**
 * Drive both legs against the server.
 *
 * Every failure returns the same message. The server deliberately answers an unknown login exactly
 * as it answers a wrong password (FR-4b), and echoing a more specific error here would undo that
 * from the client side — the UI would become the enumeration oracle the API refuses to be.
 */
export async function srpLogin(login: string, password: string): Promise<LoginResult> {
  const failure: LoginResult = { ok: false, error: "Invalid username or password" };
  const handshake = startSrp(login, password);

  let begin: Response;
  try {
    begin = await fetch("/api/auth/srp/begin", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ login, A: handshake.A }),
    });
  } catch {
    return { ok: false, error: "Could not reach the server" };
  }
  if (begin.status === 429) {
    const retry = begin.headers.get("Retry-After");
    return {
      ok: false,
      error: retry
        ? `Too many attempts. Try again in ${retry} seconds.`
        : "Too many attempts. Try again shortly.",
    };
  }
  if (!begin.ok) return failure;

  const challenge = await begin.json();
  if (!challenge?.salt || !challenge?.B || !challenge?.token) return failure;

  let proof: string;
  try {
    proof = await handshake.proof(challenge.salt, challenge.B);
  } catch {
    return failure;
  }

  const finish = await fetch("/api/auth/srp/finish", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token: challenge.token, M: proof }),
  });
  if (!finish.ok) return failure;

  const body = await finish.json();
  if (!handshake.verifyServer(body?.HAMK ?? "")) {
    // The server could not prove it holds the verifier. Something is answering that should not be,
    // and a session established here would be a session with an impostor.
    return { ok: false, error: "The server failed to authenticate itself" };
  }

  return {
    ok: true,
    user: body.user,
    mustChangePassword: Boolean(body.must_change_password),
    accessExpiresAt:
      typeof body.access_expires_at === "number" ? body.access_expires_at : undefined,
  };
}
