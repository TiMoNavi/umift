#!/usr/bin/env python3
"""Minimal usbmux TCP port forwarder for UMIFT iPhone capture streams.

This is a small, dependency-free replacement for the common `iproxy` workflow:

    python3 usbmux_forward.py --local-port 17381 --device-port 17381

It talks to macOS usbmuxd through /var/run/usbmuxd and forwards each local TCP
client to the selected iPhone port over the USB cable.
"""

from __future__ import annotations

import argparse
import plistlib
import select
import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any


USBMUXD_SOCKET = "/var/run/usbmuxd"
USBMUX_VERSION = 1
USBMUX_MESSAGE_PLIST = 8
DEFAULT_DEVICE_SERIAL = "00008130-000E2DA10141001C"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 17381


@dataclass(frozen=True)
class UsbmuxDevice:
    device_id: int
    serial: str
    connection_type: str


class UsbmuxError(RuntimeError):
    pass


def recv_exact(sock: socket.socket, byte_count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = byte_count
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise UsbmuxError("usbmuxd closed the connection")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def usbmux_socket() -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(USBMUXD_SOCKET)
    return sock


def send_plist_request(sock: socket.socket, payload: dict[str, Any], tag: int) -> None:
    body = plistlib.dumps(payload, fmt=plistlib.FMT_XML)
    header = struct.pack(
        "<IIII",
        16 + len(body),
        USBMUX_VERSION,
        USBMUX_MESSAGE_PLIST,
        tag,
    )
    sock.sendall(header + body)


def recv_plist_response(sock: socket.socket) -> dict[str, Any]:
    header = recv_exact(sock, 16)
    length, _version, message_type, _tag = struct.unpack("<IIII", header)
    if length < 16:
        raise UsbmuxError(f"invalid usbmuxd frame length {length}")
    body = recv_exact(sock, length - 16)
    if message_type != USBMUX_MESSAGE_PLIST:
        raise UsbmuxError(f"unexpected usbmuxd message type {message_type}")
    return plistlib.loads(body)


def request(payload: dict[str, Any], tag: int = 1) -> dict[str, Any]:
    with usbmux_socket() as sock:
        send_plist_request(sock, payload, tag)
        return recv_plist_response(sock)


def list_devices() -> list[UsbmuxDevice]:
    response = request(
        {
            "MessageType": "ListDevices",
            "ClientVersionString": "umift-usbmux-forward",
            "ProgName": "umift-usbmux-forward",
            "kLibUSBMuxVersion": 3,
        }
    )
    devices: list[UsbmuxDevice] = []
    for item in response.get("DeviceList", []):
        properties = item.get("Properties", {})
        serial = properties.get("SerialNumber") or properties.get("USBSerialNumber") or ""
        connection_type = properties.get("ConnectionType") or "unknown"
        device_id = item.get("DeviceID") or properties.get("DeviceID")
        if isinstance(device_id, int) and serial:
            devices.append(
                UsbmuxDevice(
                    device_id=device_id,
                    serial=str(serial),
                    connection_type=str(connection_type),
                )
            )
    return devices


def choose_device(serial: str | None) -> UsbmuxDevice:
    devices = list_devices()
    if not devices:
        raise UsbmuxError("no iPhone is visible through usbmuxd")

    if serial:
        for device in devices:
            if device.serial == serial or device.serial.replace("-", "") == serial.replace("-", ""):
                return device
        known = ", ".join(device.serial for device in devices)
        raise UsbmuxError(f"device {serial} not found. visible devices: {known}")

    usb_devices = [device for device in devices if device.connection_type.upper() == "USB"]
    if len(usb_devices) == 1:
        return usb_devices[0]
    if len(devices) == 1:
        return devices[0]
    known = ", ".join(device.serial for device in devices)
    raise UsbmuxError(f"multiple devices visible; pass --device. visible devices: {known}")


def connect_device_port(device: UsbmuxDevice, port: int) -> socket.socket:
    sock = usbmux_socket()
    send_plist_request(
        sock,
        {
            "MessageType": "Connect",
            "ClientVersionString": "umift-usbmux-forward",
            "ProgName": "umift-usbmux-forward",
            "DeviceID": device.device_id,
            # usbmuxd expects the TCP port in network byte order.
            "PortNumber": socket.htons(port),
            "kLibUSBMuxVersion": 3,
        },
        tag=2,
    )
    response = recv_plist_response(sock)
    result = response.get("Number")
    if result != 0:
        sock.close()
        raise UsbmuxError(f"connect to device port {port} failed: {response}")
    return sock


def close_socket(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def pipe_socket_pair(client: socket.socket, remote: socket.socket) -> None:
    sockets = [client, remote]
    while True:
        readable, _, errored = select.select(sockets, [], sockets, 1.0)
        if errored:
            return
        for sock in readable:
            try:
                data = sock.recv(65536)
            except OSError:
                return
            if not data:
                return
            target = remote if sock is client else client
            try:
                target.sendall(data)
            except OSError:
                return


def handle_client(client: socket.socket, address: tuple[str, int], device: UsbmuxDevice, device_port: int) -> None:
    started = time.monotonic()
    remote: socket.socket | None = None
    try:
        print(f"client connected {address[0]}:{address[1]}", flush=True)
        remote = connect_device_port(device, device_port)
        pipe_socket_pair(client, remote)
    except Exception as exc:  # noqa: BLE001 - this is a diagnostic CLI.
        print(f"client error {address[0]}:{address[1]}: {exc}", file=sys.stderr)
    finally:
        close_socket(client)
        if remote is not None:
            close_socket(remote)
        elapsed = time.monotonic() - started
        print(f"client closed {address[0]}:{address[1]} elapsed={elapsed:.1f}s", flush=True)


def serve(args: argparse.Namespace) -> int:
    device = choose_device(args.device)
    print(
        "forwarding "
        f"{args.local_host}:{args.local_port} -> "
        f"{device.serial} usbmux_device_id={device.device_id} port={args.device_port}",
        flush=True,
    )

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((args.local_host, args.local_port))
    server.listen(args.backlog)

    served_count = 0
    try:
        while True:
            client, address = server.accept()
            served_count += 1
            if args.once:
                handle_client(client, address, device, args.device_port)
                break
            thread = threading.Thread(
                target=handle_client,
                args=(client, address, device, args.device_port),
                daemon=True,
            )
            thread.start()
    except KeyboardInterrupt:
        print()
    finally:
        close_socket(server)
    return 0


def print_devices() -> int:
    for device in list_devices():
        print(f"{device.serial}\tdevice_id={device.device_id}\t{device.connection_type}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Forward a local TCP port to an iPhone app port over usbmuxd.")
    parser.add_argument("--list", action="store_true", help="List devices visible through usbmuxd and exit.")
    parser.add_argument("--device", default=DEFAULT_DEVICE_SERIAL, help="iPhone serial/UDID visible to usbmuxd.")
    parser.add_argument("--local-host", default=DEFAULT_HOST)
    parser.add_argument("--local-port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--device-port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--backlog", type=int, default=8)
    parser.add_argument("--once", action="store_true", help="Accept one client then stop listening.")
    args = parser.parse_args()

    try:
        if args.list:
            return print_devices()
        return serve(args)
    except UsbmuxError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    raise SystemExit(main())
