# Deno/Fresh Security and Privacy Review

Review date: 2026-09-04\
Release target: 1.0.0\
Disposition: approved with no unresolved release blockers

## Scope

This review covers the Deno/Fresh runtime boundary, authentication and request
integrity, tenant authorization, provider credentials, logs and command output,
database backup subprocesses, retention and legal holds, deployment permissions,
and the blue/green write fence. Live provider validation and production rollout
evidence remain independently gated by `DF5-G2`, `DF5-G3`, and the other DF6
exit gates.

## Findings

| ID       | Severity | Finding                                                                                   | Disposition                                                                                                         |
| -------- | -------- | ----------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| `SPR-01` | High     | Structured logs could emit secrets supplied in context or exception text.                 | Resolved centrally by recursive sensitive-key and credential-pattern redaction.                                     |
| `SPR-02` | Medium   | Direct Fresh responses lacked the browser security headers supplied by the Python server. | Resolved by a global response wrapper with CSP, clickjacking, MIME, referrer, permissions, and HTTPS HSTS controls. |
| `SPR-03` | High     | Python configuration and readiness output exposed PostgreSQL URL credentials.             | Resolved with a shared URL sanitizer used by both output paths.                                                     |

No unresolved release-blocking finding remains in the reviewed scope.

## Verified Controls

- Session and OAuth state HMACs reject tampering and expiry; EventSub validates
  raw bytes, freshness, and signatures with constant-time comparisons.
- Browser mutations enforce origin and CSRF policy. Session and OAuth cookies
  are `HttpOnly`, `SameSite=Lax`, and `Secure` under HTTPS.
- Tenant roles, explicit overrides, surface policies, and provider capabilities
  deny unauthorized and cross-tenant access before mutation.
- Installation credentials are encrypted at rest, tenant-scoped, rotatable, and
  audited. Revocation removes stored credentials.
- Configuration summaries, migration output, backup commands, restore commands,
  structured context, and exception text do not expose database or provider
  credentials.
- Retention removes expired tenant and operational records while active legal
  holds preserve covered messages and observations.
- Read-only green deployments reject mutations and OAuth callbacks before
  controller execution.
- Environment files are restricted to `0600` or `0640`; systemd enables
  `NoNewPrivileges`, private temporary storage, read-only home/application
  paths, explicit writable paths, and `UMask=0077`.
- Runtime tasks grant role-specific environment, filesystem, subprocess, and
  network permissions.

## Verification

Run the repeatable review gate:

```bash
deno task test:security-review
```

The gate covers cryptography, authentication integrity, permissions, web write
fencing, browser headers, retention/legal holds, backup credential isolation,
configuration and log redaction, and deployment hardening. Any regression is a
release blocker until the gate passes again.
