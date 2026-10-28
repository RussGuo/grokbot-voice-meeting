# Security

## Reporting

Please do not open a public issue for a vulnerability. Use GitHub private vulnerability reporting on this repository, or email the maintainer listed on the GitHub profile if advisories are not enabled yet.

Include what you can do with the bug and how you reproduced it. Do not include live API keys, join tokens, or other people's meeting audio.

## Secrets

- `.env` is gitignored. `.env.example` has empty values.
- `meet` writes the join URL to `links/<room>.txt` mode `0600`. The URL contains a LiveKit token. Treat it as a credential: it can publish into that room until it expires.
- `bots.yaml` and `contexts/` are gitignored. Briefs can contain private notes.
- `meet doctor` prints whether a variable is set, never its value.
- Do not log webhook URLs that embed a secret. `WebhookPrincipal` does not.

## What the worker exposes

`meet worker` connects out to LiveKit. Its health port binds `127.0.0.1`. The optional token server binds `127.0.0.1` unless `WEB_HOST` is changed. Prefer the static join page (`MEETING_JOIN_PAGE`) so you do not publish a token server.

`MEETING_PRINCIPAL=webhook` POSTs meeting questions to a URL you configure. Point it only at a host you trust.
