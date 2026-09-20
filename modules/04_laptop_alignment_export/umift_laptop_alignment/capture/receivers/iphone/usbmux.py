#!/usr/bin/env python3
"""Small usbmuxd helper used by the laptop-side iPhone receiver."""

from __future__ import annotations

import plistlib
import socket
import struct
from dataclasses import dataclass
from typing import Any


USBMUXD_SOCKET = "/var/run/usbmuxd"
USBMUX_VERSION = 1
USBMUX_MESSAGE_PLIST = 8
DEFAULT_DEVICE_SERIAL = "00008130-000E2DA10141001C"


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
    header = struct.pack("<IIII", 16 + len(body), USBMUX_VERSION, USBMUX_MESSAGE_PLIST, tag)
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
            "ClientVersionString": "umift-laptop-alignment",
            "ProgName": "umift-laptop-alignment",
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
        normalized_serial = serial.replace("-", "")
        for device in devices:
            if device.serial == serial or device.serial.replace("-", "") == normalized_serial:
                return device
        visible = ", ".join(device.serial for device in devices)
        raise UsbmuxError(f"device {serial} not found. visible devices: {visible}")

    usb_devices = [device for device in devices if device.connection_type.upper() == "USB"]
    if len(usb_devices) == 1:
        return usb_devices[0]
    if len(devices) == 1:
        return devices[0]
    visible = ", ".join(device.serial for device in devices)
    raise UsbmuxError(f"multiple devices visible; pass --device. visible devices: {visible}")


def connect_device_port(device: UsbmuxDevice, port: int) -> socket.socket:
    sock = usbmux_socket()
    send_plist_request(
        sock,
        {
            "MessageType": "Connect",
            "ClientVersionString": "umift-laptop-alignment",
            "ProgName": "umift-laptop-alignment",
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
