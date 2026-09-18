# M6.2.5 — ChatGPT Control Tool Exposure

## Problem

An unbound ChatGPT MCP transport can receive `NO_MANAGED_SESSION_BOUND` for Serena coding tools, yet the Studio controls required to recover/select a project may not be visible in the ChatGPT tool catalog.

The three critical controls are:

- `mcpstudio_use_workspace`
- `mcpstudio_create_session`
- `mcpstudio_use_session`

## Fix

MCP Studio 0.9.10 makes these controls discovery-critical:

1. Studio management tools are prepended to `tools/list`, with the three project/session controls first.
2. SSE tool discovery scans all data events and augments the actual `result.tools` event, even if progress/notification events appear first.
3. If Serena tool discovery is temporarily unreachable or returns an HTTP error, Studio returns a valid local `tools/list` containing Studio controls instead of stranding the client.
4. Studio control `tools/call` remains local and callable before a managed session is bound.
5. Existing fail-closed behavior for Serena coding tools remains unchanged until a project/session is selected.

This does not weaken Project Pin, managed-session isolation, or the production cutover guard.

## Expected ChatGPT flow

1. ChatGPT initializes the MCP connection.
2. `tools/list` contains the three critical Studio controls at the start of the catalog.
3. ChatGPT can call `mcpstudio_use_workspace`, `mcpstudio_create_session`, or `mcpstudio_use_session` while unbound.
4. Studio binds the gateway transport to the dedicated Serena instance for that workspace/session.
5. Subsequent Serena calls execute only through the pinned managed session.

## After deployment

Restart `mcp-studio.service`. Then start a new ChatGPT MCP session or reconnect/refresh the connector so ChatGPT performs fresh tool discovery. Existing chats may retain a previously cached tool catalog.
