# Repository Guidelines

## Scope

This repository hosts Red-DiscordBot cogs.

## MMIdleAlpha Operational Notes

- Cog package: `mmidlealpha/`
- Primary commands:
  - Player: `/redeem`, `/alphalink`, `/alphastatus`
  - Staff: `/alphadiag`
  - Admin config group: `[p]mmalpha ...`
- API contract targets MMIdle endpoints:
  - Redeem: `/api/integrations/discord/redeem`
  - Status: `/api/integrations/discord/status`
- Shared secret is required and must be set via:
  - `[p]mmalpha setsecret <IDLEMMO_DISCORD_REDEEM_SECRET>`

## Safety

- Never commit real secrets/tokens in code, docs, or examples.
- Keep user-facing command responses concise and actionable.
- Preserve backward-compatible command names unless explicitly requested.

## Validation

- Run syntax validation before commit:
  - `python3 -m py_compile mmidlealpha/mmidlealpha.py mmidlealpha/__init__.py`
