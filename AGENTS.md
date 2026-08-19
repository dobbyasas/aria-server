# Aria release versioning

Aria uses one shared semantic version across the iPhone/iPad app, macOS app,
and song server. The current baseline is stored in `VERSION`.

Before pushing any completed Aria change:

- For a working fix, refinement, maintenance change, or other non-feature edit,
  increment the patch number: `1.1.3` becomes `1.1.4`.
- When adding or removing a user-visible feature, increment the minor number and
  reset the patch number to zero: `1.1.3` becomes `1.2.0`.
- Change the major number only when the user explicitly requests it.
- Bump once per completed user request, immediately before the related Git push.
- Prepend a `CHANGELOG.txt` entry for the new version with `Created: YYYY-MM-DD`
  and concise `Added`, `Changed`, `Fixed`, or `Removed` notes as applicable.
- Preserve older changelog entries except when correcting a factual mistake.
- Keep `VERSION`, Xcode `MARKETING_VERSION`, visible version labels, and the
  server's `ARIA_VERSION` synchronized across the sibling Aria repositories.
