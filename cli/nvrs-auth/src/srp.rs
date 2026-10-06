// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: LicenseRef-NvidiaProprietary
//! SRP-6a client, mirroring `frontend/src/lib/srpClient.ts` and `backend/identity/srp.py` (pysrp).
//!
//! **Every constant and every step here has an exact counterpart on the server, and a mismatch does
//! not error — it produces a proof the server rejects, indistinguishable from a wrong password.**
//! That is why this is written out rather than pulled from a crate: the RustCrypto `srp` crate
//! computes a different `M1`, so it cannot interoperate at all, and any library's padding must be
//! audited against ours regardless. The correctness gate is `tests/srp_vector.rs`, which replays a
//! vector generated from the server's own implementation — if the two ever disagree, that test fails
//! rather than every operator's login.
//!
//! The parameters (`identity/srp.py`): SHA-256, the RFC 5054 Appendix A **4096-bit** group with
//! generator **g = 5**, a 32-byte salt, and RFC 5054 §2.6 padding **on**. The padding is *not*
//! uniform: `u = H(PAD(A), PAD(B))` and `k = H(N, PAD(g))` pad, but in the client proof
//! `M = H(H(N) xor H(PAD(g)), H(I), s, A, B, K)` the values `A` and `B` are **unpadded**. Getting
//! that asymmetry wrong is the single most likely way to break interoperability.

use num_bigint::BigUint;
use num_traits::Zero;
use sha2::{Digest, Sha256};

/// RFC 5054 Appendix A, 4096-bit group modulus. Byte-identical to `srp.NG_4096` on the server and
/// `N_HEX` in `srpClient.ts`.
const N_HEX: &str = concat!(
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
);

/// Errors from the client side of the exchange. Deliberately few: the server answers every failure
/// uniformly, so most "failures" the caller sees are just a rejected proof (a wrong password).
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum SrpError {
    /// The server sent `B ≡ 0 (mod N)`, which would force the session key to a constant. RFC 5054
    /// says refuse.
    #[error("server sent an invalid B")]
    InvalidB,
    /// `u` came out zero — the exchange cannot continue safely.
    #[error("SRP scrambling parameter u is zero")]
    UZero,
    /// The server's salt or B was not valid hex.
    #[error("malformed value from server: {0}")]
    MalformedServerValue(String),
}

fn n() -> BigUint {
    BigUint::parse_bytes(N_HEX.as_bytes(), 16).expect("N_HEX is valid hex")
}

fn g() -> BigUint {
    BigUint::from(5u8)
}

/// Byte width of N — what every padded value is left-padded to (512 for the 4096-bit group).
fn n_bytes_len() -> usize {
    N_HEX.len() / 2
}

fn sha256(parts: &[&[u8]]) -> [u8; 32] {
    let mut h = Sha256::new();
    for p in parts {
        h.update(p);
    }
    h.finalize().into()
}

/// Minimal big-endian encoding matching Python's `long_to_bytes` and the TS `toBytes`: no leading
/// zero bytes, and a single `0` byte for zero. Any extra leading zero would change every hash it
/// feeds.
fn to_bytes(v: &BigUint) -> Vec<u8> {
    if v.is_zero() {
        return vec![0];
    }
    v.to_bytes_be()
}

/// Left-pad to `width` with zero bytes. This is RFC 5054 §2.6 PAD().
fn pad(bytes: &[u8], width: usize) -> Vec<u8> {
    if bytes.len() >= width {
        return bytes.to_vec();
    }
    let mut out = vec![0u8; width];
    out[width - bytes.len()..].copy_from_slice(bytes);
    out
}

fn xor(a: &[u8], b: &[u8]) -> Vec<u8> {
    a.iter().zip(b).map(|(x, y)| x ^ y).collect()
}

/// The private scalar `x = H(salt, H(login:password))` — **no padding here**, matching
/// `identity/srp.py`'s `gen_x` and `srpClient.ts`. Both the login proof and the enrolment verifier
/// derive from it, so it lives in one place: a drift here is a drift in both at once, and the vector
/// test pins it.
fn x_value(login: &str, salt: &[u8], password: &str) -> BigUint {
    let inner = sha256(&[format!("{login}:{password}").as_bytes()]);
    BigUint::from_bytes_be(&sha256(&[salt, &inner]))
}

/// Derive a fresh `(salt_hex, verifier_hex)` for enrolling or changing a password, byte-compatible
/// with the server's `identity.srp.verifier_for_salt`: a 32-byte random salt, `x = H(salt,
/// H(login:password))`, and `v = g^x mod N`. The password stays in this module — only the salt and
/// verifier leave, exactly as enrolment and a password change require (the server never receives a
/// password to hash). A 32-byte salt matches the server; its default 4-byte salt is the unsafe path
/// `srp.py` deliberately overrides.
pub fn enroll(login: &str, password: &str) -> (String, String) {
    let mut salt = [0u8; 32];
    rand::RngCore::fill_bytes(&mut rand::thread_rng(), &mut salt);
    enroll_with_salt(&canonical_login(login), password, &salt)
}

/// The deterministic core of [`enroll`] with a caller-supplied salt — reproducible, so the vector
/// test can pin the verifier against one generated by the server's own `pysrp`. `login` is assumed
/// already canonical (as [`enroll`] passes it).
fn enroll_with_salt(login: &str, password: &str, salt: &[u8]) -> (String, String) {
    let v = g().modpow(&x_value(login, salt, password), &n());
    (hex::encode(salt), hex::encode(to_bytes(&v)))
}

/// Canonicalise a login exactly as `identity.models.canonical_login` does for the bytes that feed
/// the hash: trim, then case-fold. Rust's `to_lowercase` is Unicode lowercasing rather than full
/// `casefold`; for the ASCII operator logins this deployment uses they are identical, and only the
/// hashed bytes matter here (the server re-validates the rest).
pub fn canonical_login(raw: &str) -> String {
    raw.trim().to_lowercase()
}

/// A started exchange. Holds the ephemeral private value `a`, so it must be used for exactly one
/// login and then dropped.
pub struct Handshake {
    a: BigUint,
    big_a: BigUint,
    login: String,
    password: String,
}

/// The client proof `M` plus the server proof we expect back, so the caller can complete round trip
/// two and then verify the server authenticated itself.
#[derive(Debug)]
pub struct Proof {
    pub m_hex: String,
    expected_hamk: [u8; 32],
}

impl Proof {
    /// Check the server's own proof `HAMK`. **Always call this.** Skipping it means authenticating to
    /// a server that never authenticated back — the half of mutual authentication an impostor would
    /// love you to skip. Constant-time compare, out of habit; the value is public once sent.
    pub fn verify_server(&self, hamk_hex: &str) -> bool {
        let Ok(given) = hex::decode(hamk_hex) else {
            return false;
        };
        if given.len() != self.expected_hamk.len() {
            return false;
        }
        let mut diff = 0u8;
        for (x, y) in given.iter().zip(self.expected_hamk.iter()) {
            diff |= x ^ y;
        }
        diff == 0
    }
}

/// Begin an exchange with a fresh random private exponent.
pub fn start(login: &str, password: &str) -> Handshake {
    let mut raw = [0u8; 32];
    rand::RngCore::fill_bytes(&mut rand::thread_rng(), &mut raw);
    start_with_private(login, password, BigUint::from_bytes_be(&raw))
}

/// Begin an exchange with a supplied private exponent. For the vector test, which pins `a` so the
/// whole exchange is reproducible.
pub fn start_with_private(login: &str, password: &str, a: BigUint) -> Handshake {
    let big_a = g().modpow(&a, &n());
    Handshake {
        a,
        big_a,
        login: canonical_login(login),
        password: password.to_string(),
    }
}

impl Handshake {
    /// `A` as hex, sent to `/api/auth/srp/begin`.
    pub fn a_hex(&self) -> String {
        hex::encode(to_bytes(&self.big_a))
    }

    /// Compute the client proof `M` for the server's challenge `(salt, B)`, and remember the `HAMK`
    /// we expect in return.
    pub fn proof(&self, salt_hex: &str, b_hex: &str) -> Result<Proof, SrpError> {
        let modulus = n();
        let width = n_bytes_len();

        let salt = hex::decode(salt_hex)
            .map_err(|e| SrpError::MalformedServerValue(format!("salt: {e}")))?;
        let b = BigUint::parse_bytes(b_hex.as_bytes(), 16)
            .ok_or_else(|| SrpError::MalformedServerValue("B is not hex".into()))?;

        // B ≡ 0 (mod N) is the specified refusal.
        if (&b % &modulus).is_zero() {
            return Err(SrpError::InvalidB);
        }

        let a_bytes = to_bytes(&self.big_a);
        let b_bytes = to_bytes(&b);

        // u = H(PAD(A), PAD(B))
        let u = BigUint::from_bytes_be(&sha256(&[&pad(&a_bytes, width), &pad(&b_bytes, width)]));
        if u.is_zero() {
            return Err(SrpError::UZero);
        }

        // k = H(N, PAD(g))
        let n_bytes = to_bytes(&modulus);
        let g_padded = pad(&to_bytes(&g()), width);
        let k = BigUint::from_bytes_be(&sha256(&[&n_bytes, &g_padded]));

        // x = H(salt, H(login:password)) — no padding here. Shared with `enroll` so the proof and
        // the verifier can never disagree about how `x` is formed.
        let x = x_value(&self.login, &salt, &self.password);

        // v = g^x mod N
        let v = g().modpow(&x, &modulus);

        // S = (B - k*v)^(a + u*x) mod N. Do the subtraction in the modular domain so it never goes
        // negative (BigUint cannot represent a negative), matching the TS normalisation into [0, N).
        let kv = (&k * &v) % &modulus;
        let base = (&b + &modulus - kv) % &modulus;
        let exp = &self.a + &u * &x;
        let s = base.modpow(&exp, &modulus);

        // K = H(S)
        let big_k = sha256(&[&to_bytes(&s)]);

        // M = H( H(N) xor H(PAD(g)), H(login), salt, A, B, K ) — A and B UNPADDED here.
        let h_n = sha256(&[&n_bytes]);
        let h_g = sha256(&[&g_padded]);
        let h_i = sha256(&[self.login.as_bytes()]);
        let m = sha256(&[&xor(&h_n, &h_g), &h_i, &salt, &a_bytes, &b_bytes, &big_k]);

        // HAMK = H(A, M, K)
        let expected_hamk = sha256(&[&a_bytes, &m, &big_k]);

        Ok(Proof {
            m_hex: hex::encode(m),
            expected_hamk,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn canonical_login_folds_and_trims() {
        assert_eq!(canonical_login("  Rafael "), "rafael");
        assert_eq!(canonical_login("ADMIN"), "admin");
        assert_eq!(canonical_login("op.1_test@x"), "op.1_test@x");
    }

    #[test]
    fn to_bytes_matches_long_to_bytes_conventions() {
        assert_eq!(to_bytes(&BigUint::zero()), vec![0]);
        assert_eq!(to_bytes(&BigUint::from(255u8)), vec![0xff]);
        // 256 = 0x0100 — no leading zero, exactly two bytes.
        assert_eq!(to_bytes(&BigUint::from(256u16)), vec![0x01, 0x00]);
    }

    #[test]
    fn pad_left_pads_to_width_and_leaves_wide_values() {
        assert_eq!(pad(&[0xaa], 4), vec![0, 0, 0, 0xaa]);
        assert_eq!(pad(&[1, 2, 3], 3), vec![1, 2, 3]);
        assert_eq!(pad(&[1, 2, 3, 4], 3), vec![1, 2, 3, 4]);
    }

    #[test]
    fn n_is_the_4096_bit_group() {
        assert_eq!(n_bytes_len(), 512);
        assert_eq!(n().bits(), 4096);
    }

    #[test]
    fn a_is_nonzero_and_reproducible_from_private() {
        // Same private exponent must give the same A — the property the vector test relies on.
        let a = BigUint::from(123456789u64);
        let h1 = start_with_private("op", "pw", a.clone());
        let h2 = start_with_private("op", "pw", a);
        assert_eq!(h1.a_hex(), h2.a_hex());
        assert!(!h1.a_hex().is_empty());
    }

    #[test]
    fn rejects_zero_b() {
        let hs = start_with_private("op", "pw", BigUint::from(42u8));
        // B = N ≡ 0 (mod N).
        let err = hs.proof(&hex::encode([0u8; 32]), N_HEX).unwrap_err();
        assert_eq!(err, SrpError::InvalidB);
    }

    #[test]
    fn wrong_hamk_is_rejected() {
        let hs = start_with_private("op", "pw", BigUint::from(7u8));
        // A small but valid B so proof() completes; we only care that verify_server rejects a bad
        // HAMK here, not the exchange's cryptographic validity (the vector test covers that).
        let proof = hs.proof(&hex::encode([1u8; 32]), "07").unwrap();
        assert!(!proof.verify_server(&hex::encode([0u8; 32])));
        assert!(!proof.verify_server("not-hex"));
    }

    /// **The correctness gate for the whole crate.** These values were generated by
    /// `pysrp` — the exact library `backend/identity/srp.py` uses — with RFC 5054 padding on and the
    /// ephemeral private exponents fixed, and the library's own `verify_session` accepted them. So if
    /// this client reproduces `A`, `M` and the expected `HAMK` from the same inputs, it is
    /// interoperable with the server by construction. If a parameter or the padding asymmetry ever
    /// drifts, this fails here — not as every operator's login looking like a wrong password.
    ///
    /// Regenerate with the script in the Phase 1 notes if the server's SRP parameters ever change
    /// (which would invalidate every enrolled verifier anyway).
    mod vector {
        use super::*;

        const LOGIN: &str = "operator";
        const PASSWORD: &str = "correct-horse-battery-staple";
        const A_PRIV_HEX: &str = "8011111111111111111111111111111111111111111111111111111111111111";
        const SALT_HEX: &str = "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f";
        const A_HEX: &str = "2d6b023e680367be18534d93ffceaa85df3b0098c31750c07c1121b958b635221ccaf57ec01938a81a4a64d522e742faa6d75cc8ab2c026e63e35bc1d27ac5000dd0a20a36cf2c2641e50e81f0aaf5cd373e57cd02f736d47d4dd7d63045099924786458f7753abc300cdd9839996f13b51e6279ebf9743b1e0b4e16c6533805a80efae65f5fb30e54529e864c691f1f7dd83a1792a9170a724502de6eaaf9b3c881cf3e3ac3587e53120f33132b55e9535a44c153eaff6a9d0e6ea8977b6989ee2419aa459660286327a66dfd81e7904d9236f6583a8e3d597ae6ad467e50c33d8bf9583d1fc6367127cd3e5bf0eee6a4a27441f7505508be155db9430b6f6b1e895bf27fed7869ed2716dd76bdd51ea7b9e507c421cf289a2042b6315d1991e1c690d9944ed195ce44be42ee7bc73c70c322762b353c6f15283cc2a9762aa4ab01ea93f81a1fa9fe0f059d17e951ddf547cc097086ba8c782484b84a2d1d310c333b29c9737c256570be8fd24bdaed5310aa7a1871f7350dba7523143f60f8bda94634cd4dd8fa9ecabec9b88b1d9a66d2d9bd51bc7475b359df5f793bf6b490a20e233a44f115b56cc3ebe631e5b98933e70c9341612c3d88260122cb9a2bfaf9540487ffcd887e4488749b57a739c01f9ba2c5bfb6096b5cf87c21adf1f90ec4c6b754714aa2912ca11ff4b64372f55754d626a56dbc7738e8f17120c7fb";
        const B_HEX: &str = "8e52264aaba32b9f67477fd27a43ba0cc4f136750adb2e9cd544d1d9ba891e0238325efe641e2e68e6c4d5c9904312856830d1691c281f340487cff82be1e3d5678c9a6f63e7fff33f347ad6c6d6e9dc8d0bd9fecaf1a397b9716c3e1dc8e22a8b467f1de002d58f703b491bac08c4bf307bc3d85087f195c874ad48835986837478bc17f324e3d4b036e44c3977724f2789fe61cf69bdd833f06df9fa7542f3d5b318b1a4bef89262f9221bd63eef9e16caf9f8b5958fc5b58abd43b8a8375a787ba6356c47eb06875610a9b56a7d2f9d25a51e39fd2006c34f34cdf14a3e2ab157e6907298017d3a1bb89ada1330609e319fde233ee92d77ea6b35acd2589488caad7fe2aedab78dd529d164453b18073ac5455a035d8ba55a5ea18affa0547091ad641b624577a31d2406b929ece32bc8989b99960d1ead01ed92313cb435c7e679eaccfd1e3fd030b32205ee6cd1f21fd9009dae5e125fee63ab5ea6c380be8522c8e49730c3c07de2f00f6ceebdfec8791be33bafa3723f05f107cbd2501fc5704117e819ce397d1c1782a0fad6d054228289d0ffde2e20d7e2677ff47356a246527a36e7378ea017e67cb73d2b684eb7be582a841d2221a472c25bd2f89b60292b0c64ffe251b54778fd9dace375e847e2bc0a0d23ba37be73b364420af574b2a628d4dc46a611843b9ef36ac4465cd1c023266fd114b166eb4dac2fc5";
        const M_HEX: &str = "4c87ad533071a69a9aac362f193da0fe1d8ffab0cd7017dad025aa46bb5df404";
        const HAMK_HEX: &str = "34a6f22f5f9723665999a15c1a7e98dea567c635a583e33a7ccf6993c372945b";

        #[test]
        fn client_reproduces_the_servers_exchange() {
            let a = BigUint::parse_bytes(A_PRIV_HEX.as_bytes(), 16).unwrap();
            let hs = start_with_private(LOGIN, PASSWORD, a);

            // A is derived from the private exponent alone.
            assert_eq!(hs.a_hex(), A_HEX, "A does not match the server's");

            // M is the whole exchange folded together — salt, B, x, k, u, S, K, and the padding
            // asymmetry. Matching it is matching every parameter at once.
            let proof = hs.proof(SALT_HEX, B_HEX).expect("proof computes");
            assert_eq!(
                proof.m_hex, M_HEX,
                "client proof M does not match the server's"
            );

            // And we recognise the server's own proof.
            assert!(
                proof.verify_server(HAMK_HEX),
                "expected HAMK not reproduced — mutual auth would fail"
            );
        }

        #[test]
        fn a_wrong_password_produces_a_different_proof() {
            // The negative of the gate: the same everything but a wrong password must NOT yield M.
            // (This is what a real wrong password does — the server then rejects the proof.)
            let a = BigUint::parse_bytes(A_PRIV_HEX.as_bytes(), 16).unwrap();
            let hs = start_with_private(LOGIN, "not-the-password", a);
            let proof = hs.proof(SALT_HEX, B_HEX).expect("proof computes");
            assert_ne!(proof.m_hex, M_HEX);
        }

        /// The enrolment counterpart of the gate: the verifier `enroll` derives for this vector's
        /// `(login, password, salt)` must equal the one the server stores. This value was computed
        /// offline with the same maths (`v = g^x mod N`, `x = H(salt, H(login:password))`) and is
        /// byte-identical to what `identity.srp.verifier_for_salt` produces via `pysrp`. A password
        /// change installs exactly such a verifier, so if this drifts, every changed password would
        /// become a lockout the way a drifted proof would become a wrong-password.
        const VERIFIER_HEX: &str = "54b81b73901e3495b96f62ff47b3dbc04839cc81b984ab86bc797589b6fa5dd2eea6741e8fa9000209e657325847157b251bc45c62fd478008f9e1e40d998319a2bfe1b63451c192e6c13d5bd7ebc5d08abc14c78102548a76687a9337411b6b744b470f9021990d89e563af84bc742f773d5922e9596e2ce78e68677bbe1b61a6f8c8337e53cbd8abad3ec8b80cdaba2558afbc8f031ec50ae8f79f0396ceb5d55bd3fb01ff77566801615d4f9f40f6f882bc5cef0185c61e4bc4ad4e46274e186d54e820ac7644b9fd265d6b0b8ef3b72d925c8611e5933550ff83d9d45e5b61e8eba5309b5403fdd14e2dfe2bc3708fd38f24fcced44a8c93aeede19d3d767e1ec399a3aaeff81a82b0b353b58ce634504ab16ec0a8734c5ed03d8b1bad96170d3dc653d35c3b308c481dc9f49d84a9e2103f9f6574c7c9e0a5f2ecc0ca53450272b290da947a342f7c31505bbdfeaf35adac87c9559a56521f42e333405d5f8e619a16377d41c0b7c1a46b65beec864d5501b57f527fe96751a1e568cfa4c21c988861a4e8f066ff027826c3e078442e1240f4d08a7665e295e4c10e8d4c687703b83cf6302d7bf459672adf61b708e96e4c80400ce8a8d92dbc38697e532dd386840d9bfbb4a5cb2dc85592fdc3fdb5b5f4dbb8e6e9a327d993628a32346a1f19dcf730816e4056de9928a455fec10a242dd27564ec18f1881333bb9c72";

        #[test]
        fn enroll_reproduces_the_servers_verifier() {
            let salt = hex::decode(SALT_HEX).unwrap();
            let (salt_out, verifier) = enroll_with_salt(LOGIN, PASSWORD, &salt);
            assert_eq!(salt_out, SALT_HEX, "salt is echoed back unchanged");
            assert_eq!(
                verifier, VERIFIER_HEX,
                "enrolled verifier must match the server's — a mismatch is a lockout"
            );
        }

        #[test]
        fn enroll_uses_a_random_salt_each_time() {
            // The public entry point must not reuse a salt; two enrolments of the same password
            // differ in both salt and verifier.
            let (s1, v1) = enroll(LOGIN, PASSWORD);
            let (s2, v2) = enroll(LOGIN, PASSWORD);
            assert_ne!(s1, s2);
            assert_ne!(v1, v2);
            assert_eq!(s1.len(), 64, "32-byte salt, hex-encoded");
        }
    }
}
