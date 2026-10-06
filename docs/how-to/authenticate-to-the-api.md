---
diataxis: how-to
description: Log in over SRP, get an access token, and call the API with it from a script.
---

# How to authenticate to the API

Every endpoint except the documentation, the login routes and the PWA manifest requires a token.
This is how a script obtains one and uses it.

The flow below was run end-to-end against the app; the request and response shapes are what the
server actually sends.

## There is no `curl -u user:password`

Greenlight authenticates with **SRP**, so the password never leaves the client — not even inside
TLS. The client and server prove to each other that they know the same secret by exchanging
public values and a pair of proofs, and the server stores a *verifier* rather than anything a
password can be recovered from.

The consequence is practical: a login cannot be expressed as a single `curl` call, because it
requires computing `A`, deriving the session key, and sending a proof. A client needs an SRP
library. Once the token is in hand, `curl` does everything else.

## Get a token

Two POSTs. The Python below is the whole handshake:

```python
import requests
import srp  # pip install srp

BASE = "https://greenlight.example:5050"
LOGIN, PASSWORD = "apidemo", "..."

srp.rfc5054_enable(True)  # REQUIRED — see the client settings below
user = srp.User(LOGIN, PASSWORD, hash_alg=srp.SHA256, ng_type=srp.NG_4096)
_, A = user.start_authentication()

s = requests.Session()  # keeps the cookies the server sets

begin = s.post(f"{BASE}/api/auth/srp/begin",
               json={"login": LOGIN, "A": A.hex()}).json()
# -> {"ok": true, "token": "<challenge>", "salt": "<hex>", "B": "<hex>"}

M = user.process_challenge(bytes.fromhex(begin["salt"]),
                           bytes.fromhex(begin["B"]))

finish = s.post(f"{BASE}/api/auth/srp/finish",
                json={"token": begin["token"], "M": M.hex()}).json()
# -> {"ok": true, "HAMK": "<hex>", "user": {...},
#     "must_change_password": false, "id_token": "<jwt>"}

# Verify the SERVER's proof. Skipping this authenticates you to something that never
# proved it knows your verifier.
user.verify_session(bytes.fromhex(finish["HAMK"]))
assert user.authenticated()

access = s.cookies["nvis_access"]
```

`must_change_password` in that response is not advisory: a first-boot administrator must change
the one-time password before anything else will work.

## Use it

The gate reads the `nvis_access` cookie first, then an `Authorization: Bearer` header. Both work,
so pick whichever suits the client:

```python
r = s.get(f"{BASE}/api/status")                                  # cookie, via the session
r = requests.get(f"{BASE}/api/status",
                 headers={"Authorization": f"Bearer {access}"})  # explicit bearer
```

And from the shell, once you have the token:

```bash
curl -sS -H "Authorization: Bearer $TOKEN" https://greenlight.example:5050/api/status
curl -sS -H "Authorization: Bearer $TOKEN" https://greenlight.example:5050/api/auth-check
```

`GET /api/auth-check` is the cheapest way to confirm a token works. It answers
`{"authenticated": true, ...}` and needs no permission beyond being authenticated.

## Renew before it expires

```python
s.post(f"{BASE}/api/auth/refresh")
# -> {"ok": true, "principal": "user:apidemo", "access_expires_at": 1760000000.0}
```

`access_expires_at` (epoch seconds, server clock) is **metadata, not a credential** — the token
itself stays in the `HttpOnly` cookie. It is also returned by `POST /api/auth/srp/finish` and
`GET /api/auth-check` (which additionally reports `access_ttl_seconds`). A client holds it as a
small control plane: check it before a call and renew when the deadline has passed, turning an
expired-token request from three round trips (request → 401, refresh, retry) into two. Apply a few
seconds of skew margin, since the deadline is on the server's clock.

Defaults and ceilings:

| Token | Default lifetime | Maximum | Setting |
|---|---|---|---|
| access | 1 hour | 7 days | `RACK_WEB_ACCESS_TTL_SECONDS` |
| refresh | 7 days | 30 days | `RACK_WEB_REFRESH_TTL_SECONDS` |

The refresh cookie is **path-scoped to `/api/auth`**, so it is only sent to the refresh endpoint
and never rides along with an ordinary API call. A client that stores cookies by path — any
normal HTTP library — gets this right without doing anything.

A long-running script should refresh on a timer rather than waiting for a 401, because the
access token's hour is measured from issue and not from last use.

## Client settings that must match

SRP has parameters, and a mismatch does not produce a useful error — it produces a proof that
fails to verify, which is **indistinguishable from a wrong password**. Four things have to agree:

| Setting | Value |
|---|---|
| Group | 4096-bit (`NG_4096`) |
| Hash | SHA-256 |
| Salt length | 32 bytes |
| RFC 5054 padding | **enabled** |

The padding one is the trap. It is off by default in the `srp` Python library, and the resulting
variant interoperates only with itself. Call `srp.rfc5054_enable(True)` at the *package* level, not
on a backend module — the flag is per-backend, and the package picks between an OpenSSL backend and
a pure-Python one at import time, so setting it on the wrong one is a silent no-op.

## For a third-party application: OIDC

Greenlight is an OpenID Connect provider, so an application that already speaks OIDC does not need
to implement SRP at all — the user logs in to Greenlight and the app receives tokens.

| Endpoint | Purpose |
|---|---|
| `GET /.well-known/openid-configuration` | discovery; point the client library here |
| `GET /.well-known/jwks.json` | public keys for verifying our tokens |
| `GET /oidc/authorize` | start the authorization-code flow |
| `POST /oidc/token` | exchange a code, or a refresh token |
| `GET /oidc/userinfo` | claims about the signed-in user |
| `POST /oidc/introspect` | check whether a token is still live |
| `POST /oidc/revoke` | withdraw a token |

Two limits to design around:

- **`/oidc/token` supports `authorization_code` and `refresh_token` only.** There is no
  `client_credentials` grant, so **there are no service accounts yet**: every token belongs to a
  person who authenticated. An unattended job needs a real account and the SRP flow above.
- **`iss` is this deployment's URL**, as OIDC Discovery requires, and conforming clients compare it
  by exact string. It is configured (`RACK_WEB_OIDC_ISSUER`), never derived from the request's
  `Host` header — otherwise a caller could choose the issuer of our tokens.

## What a token does not carry

A token says **who you are and how you authenticated**. It carries no permissions.

Authorization is evaluated from policy on every request, which is what makes a one-hour access
token safe: withdrawing a grant takes effect on the *next request* rather than at token expiry.
So a token that works for `/api/status` and is refused for a rack action is behaving correctly —
see [Actions and resources](../reference/identity/actions-and-resources.md).

## If it does not work

| Response | Meaning |
|---|---|
| `401 {"error": "authentication failed"}` | Wrong password, unknown login, expired challenge and replayed token are **deliberately indistinguishable** — telling them apart would make the endpoint a user-enumeration oracle. Check the login and the SRP parameters above. |
| `403 {"reason": "policy"}` | Authenticated, not permitted. The action needs a grant you do not hold. |
| `403` on **refresh specifically** | Renewal is itself an action (`auth:refresh`). A principal without it can log in and then fail to renew, which presents as a session that dies after an hour. |
| `429` | Rate limited: 10/minute on `srp/begin` and 20/minute on `srp/finish`, because each call costs a 4096-bit exponentiation and one worker saturates a core at roughly 19 per second. |
| Login succeeds, every later call is anonymous | The cookie was dropped. On a plain-HTTP deployment this is expected for a browser; use the bearer header instead. |
| Login works about half the time | More than one worker. SRP challenge state is in-process, so the deployment must run a single worker; the startup gate refuses more. |

## Coming later

Per-endpoint request and response examples are not in this documentation yet. The API reference
lists every route, and this page is the authentication half that all of them need.

## Related

- [Actions and resources](../reference/identity/actions-and-resources.md) — what a grant looks like.
- [Action vocabulary](../reference/identity/action-vocabulary.md) — every action, generated.
- [How to diagnose a refused request](diagnose-a-refused-request.md) — for the 403s.
