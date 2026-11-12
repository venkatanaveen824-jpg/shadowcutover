# Security Policy

ShadowCutover sits in front of systems of record, so safety issues matter more than features.

## Reporting a vulnerability

Please do not open a public issue. Use GitHub's **Report a vulnerability** button on the Security tab of this repository. I will reply within 5 business days.

## Design rules this project holds to

- In shadow mode the agent never writes to the legacy system.
- Canary and live modes stay locked until the cutover gate passes.
- An agent result that fails validation is never written, in any mode.
- Failed live writes are queued to an outbox, never dropped.
- No secrets are stored in the repository; API keys come from environment variables.
