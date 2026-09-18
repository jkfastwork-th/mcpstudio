# HIRDA M1 — Identity & Product Boundary

Status: LOCKED  
Date: 2026-09-18

## Product identity

**Product name:** HIRDA  
**Category:** Agent Runtime & Control Plane  
**Primary tagline:** **One control plane. Many agents.**

HIRDA is the product identity that succeeds the working name **MCP Studio**.

The name is intentionally independent of MCP. MCP remains an important protocol and ingress surface, but it is no longer the product boundary.

HIRDA's visual metaphor is a many-headed system sharing one controlled body: multiple agents, workspaces, sessions and computer runtimes governed through one control plane. The metaphor may inform brand/IP work, but the product should not depend on fantasy styling.

## Product definition

HIRDA owns the boundary between a user-facing agent client and the resources an agent may operate.

The current architectural invariant is:

> **One managed session = one project boundary + one computer runtime + one permission boundary.**

HIRDA currently provides:

- durable managed sessions
- workspace/project pinning
- isolated Serena runtimes
- per-session Computer Use runtimes
- VNC/browser/CDP isolation
- READ / WRITE / EXECUTE / DESTRUCTIVE policy
- workspace-scoped execution
- fail-closed handling for unclassified tools
- ingress routing and session binding
- MCP and OAuth ingress
- lifecycle, health and operational control surfaces

## Product family vocabulary

| Name | Responsibility |
| --- | --- |
| **HIRDA Core** | Control-plane authority and lifecycle coordination |
| **HIRDA Gateway** | Ingress, MCP, OAuth, routing and client binding |
| **HIRDA Runtime** | Managed agent/runtime execution |
| **HIRDA Sessions** | Durable project-pinned managed sessions |
| **HIRDA Workspaces** | Approved project boundaries |
| **HIRDA Policy** | READ / WRITE / EXECUTE / DESTRUCTIVE authorization and scope |
| **HIRDA Computer** | Per-session VNC, browser and CDP runtime |
| **HIRDA Studio** | Human-facing web control panel |
| **HIRDA Node** | A machine hosting HIRDA runtimes; reserved for multi-node work |
| **HIRDA Mesh** | Multi-node coordination; reserved for future distributed operation |
| **HIRDA Watch** | Observability/monitoring identity; reserved for future work |

These names describe product surfaces. They do not require immediate code/package renames.

## M1 migration boundary

### Rename now

User-visible product language may use **HIRDA** immediately.

During the transition, documentation may use:

> **HIRDA — formerly MCP Studio**

### Do not rename in M1

The following are infrastructure identities and remain stable until a dedicated migration milestone:

- repository path /home/alfred/mcp-studio
- Python package mcp_studio
- mcp-studio.service
- existing API routes
- existing database identifiers/schema
- existing mcpstudio_* MCP control-tool names
- environment/config keys using the existing namespace
- durable session/workspace IDs
- existing production deployment paths

This prevents a branding change from destabilizing a newly certified runtime/security boundary.

## Naming rules

1. **HIRDA** is always uppercase in product naming.
2. Use **HIRDA Studio** for the web UI, not for the entire platform.
3. Use **MCP** only when referring to the protocol, MCP ingress, MCP tools or compatibility.
4. Do not describe HIRDA as an "MCP manager"; MCP is one interface into HIRDA.
5. Prefer **agent runtime**, **control plane**, **managed session**, **workspace boundary**, **computer runtime**, and **policy boundary** in architecture language.
6. Internal legacy identifiers may keep the mcp-studio / mcpstudio_ namespace until their migration is explicitly certified.

## M1 acceptance criteria

HIRDA M1 is complete when:

- the product name and tagline are recorded as source of truth;
- the product boundary is documented independently of MCP;
- component vocabulary is defined;
- user-visible branding starts transitioning to HIRDA;
- infrastructure identifiers remain unchanged;
- all existing tests continue to pass.

## Next identity milestone

A future **HIRDA M2 — Brand & Infrastructure Migration** may cover:

- logo and geometric multi-head symbol
- color/type identity
- repository/package namespace decisions
- service/unit naming
- API/control-tool compatibility aliases
- migration/deprecation policy for mcpstudio_*
- domain and public documentation
- trademark/domain/GitHub availability review

M2 must preserve backward compatibility and be independently certified.
