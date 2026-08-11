"""Scan for the WHOOP band and (optionally) save its peripheral identifier.

Run once per host before `collector.py` — macOS assigns a peripheral UUID
that's stable on this Mac but meaningless on any other host (see
ARCHITECTURE.md → macOS BLE quirks).

WHOOP bands can advertise either the factory pattern ("WHOOP <serial>") or a
custom nickname (for example, the fictional fixture "SYNTHETIC-BAND"). Matching
uses a case-insensitive substring, and --name can disambiguate multiple nearby
devices.

Usage:
    python -m whoop_local.capture.scan                      # list matches (default filter: "whoop")
    python -m whoop_local.capture.scan --name "synthetic-band"  # match one nickname
    python -m whoop_local.capture.scan --name "synthetic-band" --save
"""

import argparse
import asyncio

from bleak import BleakScanner

from whoop_local.config import DEFAULT_DB_PATH, WHOOP_ADVERTISED_NAME, current_host
from whoop_local.db.device_store import save_device
from whoop_local.db.schema import init_db


async def scan(timeout: float, name_filter: str) -> list:
    devices = await BleakScanner.discover(timeout=timeout)
    needle = name_filter.lower()
    return [d for d in devices if d.name and needle in d.name.lower()]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=10.0, help="scan duration in seconds")
    parser.add_argument(
        "--save", action="store_true", help="save the first match to the device table"
    )
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="path to the SQLite database")
    parser.add_argument(
        "--host",
        default=current_host(),
        help="validated per-host source label (defaults to mac, linux, or pi)",
    )
    parser.add_argument(
        "--name",
        default=WHOOP_ADVERTISED_NAME,
        help="case-insensitive substring to match against the advertised name "
        "(default 'WHOOP'; override when multiple devices are nearby, "
        'e.g. --name "synthetic-band")',
    )
    args = parser.parse_args()

    matches = asyncio.run(scan(args.timeout, args.name))

    if not matches:
        print(
            f"No devices with '{args.name}' in their advertised name found in {args.timeout}s. "
            f"Check Bluetooth permission (System Settings → Privacy & Security → Bluetooth) "
            f"and that the band is charged/awake."
        )
        raise SystemExit(1)

    if len(matches) > 1 and not args.save:
        print(
            f"NOTE: {len(matches)} matches found — narrow with --name if this isn't the band you want."
        )

    for d in matches:
        print(f"  {d.name!r}  identifier={d.address}")

    if args.save:
        if len(matches) > 1:
            print(
                f"Multiple matches for '{args.name}' — refusing to guess. "
                f"Narrow with a more specific --name and re-run."
            )
            raise SystemExit(1)
        chosen = matches[0]
        conn = init_db(args.db)
        save_device(conn, args.host, chosen.address, chosen.name)
        print(
            f"Saved identifier {chosen.address!r} ({chosen.name!r}) for host={args.host!r} in {args.db}"
        )


if __name__ == "__main__":
    main()
