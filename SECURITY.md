# Security policy

## Scope

This project is policy-bounded autonomy on a **simulated** fleet. It does not connect to real
vehicles, drivers, carriers or customer data, and it is not deployed anywhere. Reports are
welcome for the engine's own controls: the policy gate, approval binding, the audit chain,
authentication and role checks in `api/`, the per-agent database grants in `db/roles.sql`, and
prompt-injection handling. The threat model is in [`docs/threat-model.md`](docs/threat-model.md).

## Reporting a vulnerability

Please report privately through GitHub security advisories: on the repository page choose
**Security**, then **Report a vulnerability**. Do not open a public issue for a vulnerability.

Include the version or commit, the steps to reproduce, and the effect you observed. This is a
personal project, so there is no response-time guarantee, but reports will be read and
acknowledged.

## Secrets

The repository holds no secrets. `.env.example` has placeholders only. If you find a credential
in the history, report it the same way.
