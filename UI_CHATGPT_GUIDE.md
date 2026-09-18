# ChatGPT-first Guide pass

UI-only addition on top of the Kanit + Tailwind-inspired v0.9.9 overlay.

## Main change

The Guide now teaches the real daily workflow first:

```text
ChatGPT
  ↓
MCP Studio
  ↓
Managed Session
  ↓
Project Pin
  ↓
Dedicated Serena
  ↓
Workspace
```

Normal use starts by telling ChatGPT which workspace to use. Workspace registration and manual session setup are moved into a collapsed **Admin setup** section because they are one-time operations rather than the primary daily workflow.

## Added

- ChatGPT-first Quick Start at the top of Guide.
- Localized English/Thai example commands.
- Copy buttons for the common ChatGPT command sequence.
- Workspace-switch example.
- Clear `ChatGPT vs MCP Studio UI` mental model.
- `Copy ChatGPT command` action on every registered Workspace row.
- Clipboard fallback for non-secure/local browser contexts.
- Existing hover help, Light/Dark theme, Kanit typography and i18n retained.

No backend, session manager, project pin, ingress, lease, OAuth or Serena isolation behavior is changed.
