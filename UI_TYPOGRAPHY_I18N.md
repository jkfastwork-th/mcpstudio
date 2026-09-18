# UI Typography + Multilingual Pass

UI-only patch for MCP Studio v0.9.9.

## Typography

- Default text scale is now **112%** (one-time migration from the previous 100% default).
- Available scale steps: **100%, 112%, 124%, 136%**.
- Reset returns to **112%**.
- Modern multilingual system font stack:
  - Inter
  - Noto Sans Thai
  - Noto Sans
  - Leelawadee UI
  - Segoe UI Variable / Segoe UI
  - Roboto
  - system sans-serif fallback
- No font binaries are bundled and there is no external web-font dependency.

## Languages

- English (`en`)
- Thai (`th`)

The selected language is stored as `mcp-studio-language` in `localStorage`.
If there is no saved preference, the UI follows the browser language (`th*` -> Thai, otherwise English).

The i18n layer translates static UI text, contextual hover help, aria labels, placeholders, dialogs, common statuses, and common dynamic time/count phrases. Unknown/technical values intentionally fall back to their original value.

The language dictionary is intentionally extensible: add another locale to `SUPPORTED_LANGUAGES`, add its dictionary, and expose it in the language selector.

## Scope

No backend, ingress, OAuth, managed-session, project-pin, lease, Serena instance-pool, or production-cutover behavior is changed.
