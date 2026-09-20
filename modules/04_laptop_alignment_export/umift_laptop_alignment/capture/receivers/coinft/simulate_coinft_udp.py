#!/usr/bin/env python3
"""Send synthetic CoinFT LAN packets to the iPhone observe page."""

from __future__ import annotations

import argparse
import json
import math
import socket
import time


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Synthetic CoinFT UDP sender")
    parser.add_argument("--udp-host", default="255.255.255.255", help="iPhone LAN IP or broadcast address")
    parser.add_argument("--udp-port", type=int, default=43001)
    parser.add_argument("--hz", type=float, default=120.0)
    parser.add_argument("--source-id", default="coinft-sim-lan")
    parser.add_argument("--broadcast", dest="broadcast", action="store_true", default=True)
    parser.add_argument("--no-broadcast", dest="broadcast", action="store_false")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if args.broadcast:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    destination = (args.udp_host, args.udp_port)
    period = 1.0 / args.hz
    seq = 0
    start = time.monotonic()
    print(f"sending synthetic CoinFT packets to {destination[0]}:{destination[1]}")

    while True:
        now = time.monotonic()
        t = now - start
        left_force = 1.5 + 1.2 * max(0.0, math.sin(2.0 * math.pi * 0.7 * t))
        right_force = 1.2 + 1.0 * max(0.0, math.sin(2.0 * math.pi * 0.7 * t + 0.9))
        payload = {
            "schema": "umift.coinft.lan.v1",
            "seq": seq,
            "source_id": args.source_id,
            "bridge_time_ns": time.time_ns(),
            "bridge_monotonic_ns": time.monotonic_ns(),
            "sample_period_hint_s": period,
            "left": {
                "raw": [1000 + i + (seq % 7) for i in range(12)],
                "wrench": [
                    left_force,
                    0.15 * math.sin(t),
                    0.20 * math.cos(t * 0.5),
                    0.01 * math.sin(t * 0.7),
                    0.01 * math.cos(t * 0.8),
                    0.0,
                ],
            },
            "right": {
                "raw": [1020 + i + (seq % 5) for i in range(12)],
                "wrench": [
                    right_force,
                    0.12 * math.sin(t + 0.4),
                    0.18 * math.cos(t * 0.5 + 0.2),
                    0.01 * math.sin(t * 0.7 + 0.3),
                    0.01 * math.cos(t * 0.8 + 0.2),
                    0.0,
                ],
            },
        }
        sock.sendto(json.dumps(payload, separators=(",", ":")).encode("utf-8"), destination)
        seq += 1
        target = start + seq * period
        time.sleep(max(0.0, target - time.monotonic()))


if __name__ == "__main__":
    raise SystemExit(main())
