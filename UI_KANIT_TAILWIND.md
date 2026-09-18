# UI Kanit + Tailwind-inspired

This pass intentionally does not migrate the application to a Tailwind build pipeline. The current dashboard is server-rendered/static and already has stable class contracts with app.js. Replacing those contracts would add build/deploy risk with no backend benefit. Instead, the production CSS uses Tailwind-inspired design tokens and component treatment while keeping the existing DOM and JavaScript stable.

Kanit is loaded from Google Fonts in `templates/index.html`; if unavailable, Noto/system fallbacks are used. No font binaries are bundled.
