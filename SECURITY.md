# Security

GovernFlow is a starter implementation. The current release has automated mock, concurrency, authorization, and browser checks; live provider and external notification acceptance remains pending. The local Compose setup exposes development services and requires deployment configuration before use with real client data.

## Reporting a vulnerability

Use GitHub's **Report a vulnerability** option on this repository's Security tab when private reporting is enabled. If that option is unavailable, open an issue requesting a private contact channel without including exploit details, credentials, client content, or database dumps.

Include affected versions or commits, reproduction steps using synthetic data, expected versus observed behavior, and the likely impact. Do not post working provider keys or production approval tokens.

## Handling exposed credentials

Revoke or rotate exposed credentials first. Removing a file or changing `.gitignore` does not erase secrets from Git history, existing clones, or logs. Coordinate history cleanup with the maintainers after revocation.

Store local credentials in ignored environment files and production credentials in a secret manager. Inference and organization reporting keys have different purposes; keep reporting keys on the server. Use dedicated provider accounts for tests and review the [live setup](docs/live-setup.md) and [implementation decisions](docs/decisions.md) before enabling real traffic.
