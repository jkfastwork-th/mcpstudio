# UI Theme / Dialog / Workspace Pass

## Navigation

Primary navigation is now:

- Home
- Sessions
- Workspaces
- System

## Themes

The header contains explicit Light and Dark choices. The selection is stored in `localStorage` as `mcp-studio-theme`.

Both themes use Google-inspired semantic colors:
- blue: primary/action
- green: healthy
- yellow: attention
- red: error/destructive

## Dialogs

Browser-native prompt/alert/confirm interactions are removed. Interactive actions use one styled application dialog based on `<dialog>`:
- rename managed session
- stop managed session
- register workspace
- action failure details

## Workspaces

The Workspaces page concentrates project-level administration:
- approved workspace registry
- session usage counts
- worker ownership
- write lease state

This keeps workspace management out of the normal Sessions workflow while preserving advanced transport details under Sessions.
