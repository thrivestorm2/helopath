# Security policy

Please **do not open a public issue** for a security problem.

Report it privately through GitHub instead: on this repository, open the **Security** tab and choose
**Report a vulnerability** (GitHub's private vulnerability reporting). Only the maintainers can see the report.

Useful details: what you found, how to reproduce it, and what an attacker could do with it. You will get a reply
through the same report.

## Scope

- The application code in this repository (Django app, background worker, templates).
- The deployment files (`Dockerfile`, `.do/app.yaml`, `.github/workflows/`).

The running instance is private and login-only; please do not test against it or attempt to access data you are
not authorised to see. Test against a local copy instead (see the README).

## What this repository does not contain

No credentials, API keys, database contents, or patient information. Deployment secrets are held in the hosting
provider's encrypted environment settings. If you believe you have found a secret in the code or its history,
report it as above.
