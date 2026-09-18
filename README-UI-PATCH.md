# MCP Studio UI Final Polish Patch v0.9.9

Apply this patch on top of the certified MCP Studio v0.9.9 baseline.

```bash
cd /home/alfred/mcp-studio
sudo cp -a /home/alfred/mcp-studio /home/alfred/mcp-studio.backup-before-ui-final

tar -xzf /home/alfred/mcp-studio-ui-final-polish-v0.9.9.tar.gz \
  -C /home/alfred/mcp-studio \
  --strip-components=1

sudo systemctl restart mcp-studio.service
```

Then hard-refresh the browser (`Ctrl+F5`).

The patch is UI-only. See `UI_FINAL_POLISH.md` for verification details.
