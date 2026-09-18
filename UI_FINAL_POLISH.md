# MCP Studio UI Final Polish — v0.9.9

Final UI-only cleanup pass for the v0.9.9 production baseline.

## Fixed

- Removed duplicate Home panels for ingress/alerts; Home now uses the health banner + four KPIs + active project sessions.
- Reduced repeated Guide content and moved setup/status reference into collapsible sections.
- Added responsive bottom navigation for mobile.
- Removed duplicate visible page titles by breakpoint: top-bar title on desktop, page heading on mobile.
- Reworked mobile header controls so language, theme, font size, and refresh fit at 320–390 px widths.
- Added responsive rules for sessions, workspace registry, ownership, write leases, system metrics, advanced tables, dialogs, and Guide cards.
- Fixed concatenated workspace metadata on mobile/Thai by forcing secondary metadata onto separate lines.
- Added earlier compact breakpoint for advanced worker/work tables when the sidebar narrows the content area.
- Preserved Light/Dark, Kanit, Thai/English, text scaling, custom dialogs, hover/focus help, Workspaces, and ChatGPT-first Guide.

## Verification

- Backend regression suite: 100 passed.
- JavaScript syntax: PASS.
- Duplicate HTML IDs: none.
- Missing static element IDs referenced by JS: none.
- Native prompt/alert/confirm: none.
- Responsive layout QA: 0 horizontal-overflow failures across widths 320, 360, 390, 768, 820, 1024, and 1440 px; all five views tested.
- Font-scale QA: 112% and 136% at critical widths.
- Thai + Dark + 136% mobile spot check: PASS.

This patch changes only UI assets under `static/` and `templates/` plus documentation. It does not change MCP routing, OAuth, session isolation, Project Pin, lease logic, or Serena process management.
