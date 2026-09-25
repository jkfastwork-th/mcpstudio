from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from .device_runtime import DeviceRuntime, DeviceRuntimeConfig, DeviceRuntimeError


def _default_config_path() -> str:
    return os.environ.get(
        "HIRDA_DEVICE_CONFIG",
        str(Path.home() / ".hirda" / "device.yaml"),
    )


def _print_qr(value: str) -> None:
    try:
        import qrcode
    except Exception:
        print(f"Pairing URL: {value}")
        return
    qr = qrcode.QRCode(border=1)
    qr.add_data(value)
    qr.make(fit=True)
    try:
        qr.print_ascii(out=sys.stdout, tty=False, invert=True)
    except Exception:
        print(f"Pairing URL: {value}")


async def _pair(config: DeviceRuntimeConfig) -> int:
    runtime = DeviceRuntime(config)
    client = runtime.pairing_client()
    started = await client.start()
    code = str(started.get("display_code") or "")
    url = str(started.get("pairing_url") or "")
    session_id = str(started.get("session_id") or "")
    if not code or not url or not session_id:
        raise DeviceRuntimeError("pairing_start_response_incomplete")

    print()
    print("Scan this QR with a browser that can approve HIRDA devices:")
    print()
    _print_qr(url)
    print()
    grouped = f"{code[:4]} {code[4:]}" if len(code) == 8 else code
    print(f"Pairing code: {grouped}")
    print(f"Device ID: {runtime.identity.device_id}")
    print("Waiting for owner approval...")

    def on_status(status: dict[str, Any]) -> None:
        state = str(status.get("state") or "unknown")
        print(f"Pairing state: {state}")

    await client.wait_for_trust(session_id, on_status=on_status)
    print("Device trusted. You can now run: hirda-device run")
    await runtime.stop()
    return 0


async def _run(config: DeviceRuntimeConfig) -> int:
    runtime = DeviceRuntime(config)
    print(
        f"HIRDA device runtime connecting as {runtime.identity.device_id} "
        f"({config.name})"
    )

    def on_error(exc: Exception) -> None:
        print(
            f"device connection error: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )

    await runtime.run_forever(on_error=on_error)
    return 0


def _identity(config: DeviceRuntimeConfig) -> int:
    runtime = DeviceRuntime(config)
    print(f"device_id={runtime.identity.device_id}")
    print(f"public_key={runtime.identity.public_key}")
    print(f"broker_url={runtime.broker_url}")
    print(f"workspaces={','.join(sorted(config.workspaces))}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hirda-device",
        description="Cross-platform HIRDA Device Runtime",
    )
    parser.add_argument(
        "--config",
        default=_default_config_path(),
        help="Path to HIRDA device YAML config",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("pair", help="Pair this device using QR + 8-digit code")
    sub.add_parser("run", help="Run the outbound HIRDA device runtime")
    sub.add_parser("identity", help="Show the local cryptographic device identity")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        config = DeviceRuntimeConfig.load(args.config)
        if args.command == "pair":
            return asyncio.run(_pair(config))
        if args.command == "run":
            return asyncio.run(_run(config))
        if args.command == "identity":
            return _identity(config)
        raise DeviceRuntimeError(f"unknown_command:{args.command}")
    except KeyboardInterrupt:
        return 130
    except DeviceRuntimeError as exc:
        print(f"HIRDA device error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
