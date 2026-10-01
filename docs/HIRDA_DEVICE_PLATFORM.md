# HIRDA Device Platform

HIRDA Device Platform is the cross-platform device trust layer for Windows, Linux,
macOS, CLI, and headless runtimes. Platform differences are adapters; identity,
pairing, policy, and transport contracts are shared.

## Roadmap

1. **P1 — Device Identity + Pairing Protocol**
2. **P2 — Device Broker / outbound WSS**
3. **P3 — Dynamic trusted devices in Machine Registry**
4. **P4 — Shared Device Runtime**
5. **P5 — Windows adapter**
6. **P6 — Linux adapter**
7. **P7 — macOS adapter**
8. **P8 — migrate legacy inbound machine agents**

The legacy Tailscale discovery and `:8765` agent remain available during the
migration. They are no longer the target onboarding architecture.

## P1 trust model

A device generates an Ed25519 keypair locally. The private key never leaves the
device. HIRDA receives the canonical public key:

```text
ed25519:<base64url-encoded 32-byte public key>
```

The stable HIRDA device ID is derived from SHA-256 of the public key, not from an
IP address, hostname, or operating system.

### Pairing flow

```text
Device runtime
  -> POST /api/device-pairing/sessions
  <- pairing session + QR URL + 8-digit display code

Owner browser
  -> scan QR URL (session ID + one-time nonce)
  -> POST /api/device-pairing/sessions/{id}/verify
       nonce + 8-digit code
  <- pending_owner

Authenticated owner
  -> POST /api/device-pairing/sessions/{id}/approve
  <- trusted device
```

Human verification and owner approval are intentionally separate. Knowing the
8-digit code is not sufficient to create trust.

### QR payload

The QR URL contains a high-entropy pairing-session identifier plus a one-time
nonce. The nonce is not persisted in plaintext. The 8-digit code is displayed
separately on the device and is also not persisted in plaintext.

Example shape:

```text
https://hirda.example/pair/pair_<random>?nonce=<random>
```

### Pairing code security

The server stores an HMAC-SHA256 digest of the 8-digit code using a local
32-byte pairing secret. The secret is stored separately with owner-only file
permissions. Pairing sessions:

- expire after 5 minutes by default;
- permit at most 5 failed verification attempts by default;
- lock after the attempt limit;
- require both the QR nonce and 8-digit code;
- become `pending_owner` after human verification;
- become `trusted` only after owner approval.

### Owner authority

Approve, reject, list-trusted-devices, and revoke operations require HIRDA owner
authorization. P1 reuses the existing HIRDA owner token/session verifier.

The device-facing create/status/verify calls never grant trust by themselves.

## P1 state machine

```text
pairing
  |-- correct nonce + code --> pending_owner
  |                           |-- owner approve --> trusted
  |                           '-- owner reject  --> rejected
  |
  |-- expiry ----------------> expired
  '-- failed attempts -------> locked
```

Trusted identity can later transition to `revoked`. Revocation preserves the
identity record so an old key cannot silently appear as a new machine.

## Capability declaration

Capabilities are device claims, not permissions. A device may declare that it
supports:

```text
filesystem
process
computer_use
shell
browser
gpu
```

The P3/P4 policy layer decides what HIRDA actually authorizes. Capability
availability must never be treated as permission.

## P2 outbound Device Broker

P2 is implemented as an outbound WebSocket trust boundary:

```text
HIRDA Device Runtime
      |
      | outbound WSS
      v
HIRDA Device Broker
```

A trusted device connects to `/api/device-broker/ws?device_id=<device_id>`.
HIRDA sends a one-time challenge containing a random challenge ID, nonce, expiry,
and canonical signing input. The device signs that exact UTF-8 payload with its
paired Ed25519 private key and returns the signature.

The broker:

- accepts only identities whose trust state is `trusted`;
- consumes each challenge before verification so it cannot be retried;
- rejects expired or malformed challenges and signatures;
- keeps authenticated connection state in memory only;
- gives each connection a short-lived connection ID and absolute expiry;
- requires periodic heartbeat/ping traffic;
- re-checks trust on every heartbeat so owner revocation cuts authority quickly;
- permits only one active connection per device, replacing an older generation
  without allowing the old disconnect handler to remove the new one;
- enforces a bounded message size.

No long-lived bearer credential is issued to the device. The authenticated
WebSocket itself is the transport session and the private key never leaves the
device.

The owner-only `GET /api/device-broker` endpoint exposes low-risk connection
metadata such as device ID, connection ID, last-seen time, expiry, and remote
address. Raw WebSocket objects, private keys, signatures, and pairing secrets are
never exposed.

P2 removes the architectural requirement for inbound device listeners and
firewall rules. Production clients must use WSS; Tailscale can remain as an
optional transport optimization rather than an enrollment dependency.

## P3 dynamic Machine Registry

P3 materializes trusted device identities into the Machine Registry without
requiring static YAML entries or IP addresses. A paired device becomes a dynamic
machine whose stable machine ID is its cryptographic device ID.

Dynamic machines:

- use the paired device name/platform/capabilities as registry metadata;
- have no identity dependency on an IP address;
- derive online/offline state from the authenticated Device Broker connection;
- default to read-only, fail-closed machine policy unless the owner explicitly
  grants broader permissions;
- remain logically bindable to managed sessions while offline;
- are removed from the dynamic registry immediately after trust is revoked.

For broker-backed devices, managed-session binding no longer requires a
control-plane `workspace_map`. HIRDA sends only the logical `workspace_key`
over broker RPC. The cross-platform Device Runtime resolves that key to its own
local path and must fail closed when the workspace is unknown. Host absolute
paths are not leaked across machines.

New runtimes advertise native support when pairing. Previously paired runtimes
without that capability marker continue to use the legacy Desktop Commander RPC.

Native filesystem/process calls use the authenticated broker channel:

```text
Managed Session
   -> Machine Capability Router
   -> Dynamic Machine Registry
   -> Device Broker RPC: hirda_native.call (desktop_commander.call remains accepted)
   -> HIRDA Device Runtime
   -> HIRDA Native Computer (optional Desktop Commander fallback)
```

Broker RPC requests carry a unique request ID and are matched to the exact
device connection generation. Pending calls fail if that connection closes,
responses from an old/replaced connection are rejected, and RPC messages share
the connection send lock with heartbeat/control traffic.

Legacy static machine entries and the inbound `:8765` agent remain supported
during migration. Static/operator registry entries always win on ID collision,
so dynamic pairing cannot replace an explicitly configured machine.

## P4 shared Device Runtime

P4 implements the cross-platform runtime core used by Windows, Linux, macOS,
CLI, and headless installations.

The runtime owns the complete device-side lifecycle:

```text
Persistent Ed25519 identity
        |
        v
QR + 8-digit pairing client
        |
        v
Outbound WSS Device Broker session
        |
        v
Heartbeat / reconnect
        |
        v
Broker RPC dispatch
        |
        v
Local workspace resolver
        |
        v
Shared Desktop Commander relay
```

The portable P4 identity store writes the Ed25519 private key to an owner-only
file and never sends it to HIRDA. P5/P6/P7 may replace only this storage adapter
with DPAPI/TPM, Secret Service, or Keychain while keeping the same public-key
identity contract.

The runtime loads one YAML configuration on every supported platform. It maps
logical HIRDA workspace keys to local paths, launches Desktop Commander as a
local stdio child process, and never exposes Desktop Commander on a network
listener.

The shared relay now uses a background response reader and bounded request
timeouts. A stuck Desktop Commander child therefore fails fast instead of
blocking an HTTP health endpoint or device RPC indefinitely. The legacy
`hirda_machine_agent.py` also imports this relay during migration.

The `hirda-device` CLI exposes:

```text
hirda-device --config <path> pair
hirda-device --config <path> run
hirda-device --config <path> identity
```

`pair` renders the server-provided QR payload and 8-digit code, then waits for
the control plane to report the cryptographic identity as trusted. The QR URL
lands on HIRDA's `/pair/{session_id}` page where the human code is verified
before the separate owner-approval step.

`run` keeps an outbound broker connection alive, signs each fresh server
challenge, reconnects with bounded exponential backoff, sends heartbeat traffic,
and handles native filesystem/process calls without blocking the network event
loop. It continues to accept `desktop_commander.call` for older clients.

An example device config lives at `examples/hirda-device.yaml`.

## P5 boundary

P5 packages this shared runtime for Windows and replaces the portable pieces
that benefit from native integration: secure private-key storage, autostart,
installer/update lifecycle, Desktop UI, and local workspace selection. The
pairing/broker/RPC contracts remain unchanged.
