# HIRDA ChatGPT plugin bundle

`packaging/chatgpt/hirda` is the portable Agent Plugins 1.0 package for connecting ChatGPT to HIRDA. Build it with:

```sh
./scripts/build-chatgpt-plugin.sh
```

The ZIP at `dist/hirda-chatgpt-1.0.0.zip` is the primary artifact for manual upload in ChatGPT. `dist/hirda-chatgpt-1.0.0.tar.gz` is an additional portable archive; `dist/SHA256SUMS` contains checksums for both.

The package contains the public HIRDA MCP endpoint and usage guidance, but no private authentication values. Each ChatGPT account authorizes through HIRDA OAuth when connecting; authorization is account-specific.
