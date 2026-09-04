# Security

This project sits in front of fnOS Music (`trim.music`). Treat it as production infrastructure.

## Secrets

- Never commit `.env`, API keys, tokens, or password hashes.
- Daily recommend credentials live only in `.env` (`chmod 600`) and are loaded via systemd `EnvironmentFile`. They are not logged.
- Report leaked keys by rotating them at the provider; do not paste keys into issues.

## What this project must not do

- Do not modify fnOS nginx configs (the system rewrites them).
- Do not patch `trim-music` binaries or write to the official `music.db`.
- Do not disable fail-safe: if the proxy dies, official music must still work after `restore.sh` or automatic socket reclaim.

## Reporting

Open a GitHub issue describing the impact and reproduction **without** secrets or personal library paths.
