#!/usr/bin/env python3
"""A simple TFTP server."""

from __future__ import annotations

import argparse
import os
import socket
import struct
import sys
import threading
from pathlib import Path

# --- types --- #
_Addr = tuple[str, int]

# --- consts ---- #
_NATIVE_LINESEP = os.linesep.encode("ascii")

# --- packet opcodes --- #
OP_RRQ = 1
OP_WRQ = 2
OP_DATA = 3
OP_ACK = 4
OP_ERROR = 5
OP_OACK = 6

# --- error codes --- #
ERR_UNDEFINED = 0
ERR_FILE_NOT_FOUND = 1
ERR_ACCESS_VIOLATION = 2
ERR_DISK_FULL = 3
ERR_ILLEGAL_OP = 4
ERR_UNKNOWN_TID = 5
ERR_FILE_EXISTS = 6

# --- defaults --- #
DEFAULT_BLKSIZE: int = 512
DEFAULT_TIMEOUT: int = 5
MAX_RETRIES: int = 5


def _parse_request(data: bytes) -> tuple[int, str | None, str | None, dict[str, str]]:
    # RRQ/WRQ packet (RFC 1350 + RFC 2347 options):
    #   opcode    uint16              operation code
    #   filename  NUL-terminated str  requested file path
    #   mode      NUL-terminated str  transfer mode (e.g. "octet")
    #   opt key   NUL-terminated str  option name      ┐
    #   opt value NUL-terminated str  option value     ┘ repeated
    opcode = struct.unpack("!H", data[:2])[0]
    fields = data[2:].split(b"\x00")
    fields = [f for f in fields if f]
    if len(fields) < 2:
        return opcode, None, None, {}
    filename = fields[0].decode("ascii")
    mode = fields[1].decode("ascii").lower()
    options: dict[str, str] = {}
    i = 2
    while i + 1 < len(fields):
        options[fields[i].decode("ascii").lower()] = fields[i + 1].decode("ascii")
        i += 2
    return opcode, filename, mode, options


def _pack_data(block: int, payload: bytes) -> bytes:
    # DATA packet (RFC 1350):
    #   opcode  uint16        operation code
    #   block   uint16        block number
    #   data    0–blksize B   file data
    return struct.pack("!HH", OP_DATA, block) + payload


def _pack_ack(block: int) -> bytes:
    # ACK packet (RFC 1350):
    #   opcode  uint16  operation code
    #   block   uint16  acknowledged block number
    return struct.pack("!HH", OP_ACK, block)


def _pack_error(code: int, msg: str) -> bytes:
    # ERROR packet (RFC 1350):
    #   opcode   uint16              operation code
    #   errcode  uint16              error code
    #   errmsg   NUL-terminated str  human-readable error message
    return struct.pack("!HH", OP_ERROR, code) + msg.encode("ascii") + b"\x00"


def _pack_oack(opts: dict[str, int]) -> bytes:
    # OACK packet (RFC 2347):
    #   opcode    uint16              operation code
    #   opt key   NUL-terminated str  option name   ┐
    #   opt value NUL-terminated str  option value  ┘ repeated
    pkt = struct.pack("!H", OP_OACK)
    for k, v in opts.items():
        pkt += k.encode("ascii") + b"\x00" + str(v).encode("ascii") + b"\x00"
    return pkt


def _to_netascii(data: bytes) -> bytes:
    data = data.replace(b"\r", b"\r\0")
    if _NATIVE_LINESEP != b"\r\n":
        data = data.replace(_NATIVE_LINESEP, b"\r\n")
    return data


def _from_netascii(data: bytes) -> bytes:
    if _NATIVE_LINESEP != b"\r\n":
        data = data.replace(b"\r\n", _NATIVE_LINESEP)
    data = data.replace(b"\r\0", b"\r")
    return data


def _resolve(base: str, filename: str) -> Path | None:
    resolved_base = Path(base).resolve()
    target = Path(os.path.join(resolved_base, filename)).resolve()
    if not target.is_relative_to(resolved_base):
        return None
    return target


def _negotiate(
    options: dict[str, str],
    filesize: int | None = None,
) -> tuple[dict[str, int], int, int]:
    accepted: dict[str, int] = {}
    blksize = DEFAULT_BLKSIZE
    timeout = DEFAULT_TIMEOUT
    if "blksize" in options:
        blksize = max(8, min(65464, int(options["blksize"])))
        accepted["blksize"] = blksize
    if "tsize" in options:
        accepted["tsize"] = filesize or 0
    if "timeout" in options:
        timeout = max(1, min(255, int(options["timeout"])))
        accepted["timeout"] = timeout
    return accepted, blksize, timeout


class _Transfer:
    client: _Addr
    sock: socket.socket

    def __init__(self, client_addr: _Addr, server_bind: str) -> None:
        self.client = client_addr
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((server_bind, 0))

    def close(self) -> None:
        self.sock.close()

    def settimeout(self, t: float) -> None:
        self.sock.settimeout(t)

    def send(self, pkt: bytes) -> None:
        self.sock.sendto(pkt, self.client)

    def recv(self, bufsize: int = 65536) -> bytes:
        while True:
            data, addr = self.sock.recvfrom(bufsize)
            if addr == self.client:
                return data
            self.sock.sendto(_pack_error(ERR_UNKNOWN_TID, "Unknown transfer ID"), addr)

    def send_error(self, code: int, msg: str) -> None:
        self.send(_pack_error(code, msg))

    def send_reliable(self, pkt: bytes, expect_opcode: int, expect_block: int) -> bytes:
        for _ in range(MAX_RETRIES):
            self.send(pkt)
            try:
                data = self.recv()
            except socket.timeout:
                continue
            op = struct.unpack("!H", data[:2])[0]
            if op == OP_ERROR:
                raise ConnectionError(data[4:-1].decode("ascii", errors="replace"))
            if op == expect_opcode and len(data) >= 4:
                blk = struct.unpack("!H", data[2:4])[0]
                if blk == expect_block:
                    return data
        raise TimeoutError("transfer timed out")


def _handle_rrq(
    directory: str,
    filename: str,
    mode: str,
    options: dict[str, str],
    client_addr: _Addr,
    server_bind: str,
) -> None:
    path = _resolve(directory, filename)
    xfer = _Transfer(client_addr, server_bind)
    try:
        if mode not in ("octet", "netascii"):
            xfer.send_error(ERR_ILLEGAL_OP, f"Unsupported mode: {mode}")
            print(f"  Unsupported mode {mode!r}: {filename!r}")
            return

        if path is None or not path.is_file():
            code = ERR_ACCESS_VIOLATION if path is None else ERR_FILE_NOT_FOUND
            msg = "Access violation" if path is None else "File not found"
            xfer.send_error(code, msg)
            print(f"  {msg}: {filename!r}")
            return

        try:
            file_data = path.read_bytes()
        except PermissionError:
            xfer.send_error(ERR_ACCESS_VIOLATION, "Permission denied")
            return

        if mode == "netascii":
            file_data = _to_netascii(file_data)

        accepted, blksize, timeout = _negotiate(options, len(file_data))
        xfer.settimeout(timeout)

        print(
            f"RRQ  {filename!r} from {client_addr[0]}:{client_addr[1]}"
            f" ({len(file_data)} bytes, blksize={blksize}, mode={mode})"
        )

        if accepted:
            xfer.send_reliable(_pack_oack(accepted), OP_ACK, 0)

        block = 1
        offset = 0
        while True:
            chunk = file_data[offset : offset + blksize]
            xfer.send_reliable(_pack_data(block, chunk), OP_ACK, block)
            offset += blksize
            block = (block + 1) & 0xFFFF
            if len(chunk) < blksize:
                break

        print(f"  -> sent {filename!r} ({len(file_data)} bytes)")

    except (ConnectionError, TimeoutError) as e:
        print(f"  !! {filename!r}: {e}")
    finally:
        xfer.close()


def _handle_wrq(
    directory: str,
    filename: str,
    mode: str,
    options: dict[str, str],
    client_addr: _Addr,
    server_bind: str,
) -> None:
    path = _resolve(directory, filename)
    xfer = _Transfer(client_addr, server_bind)
    try:
        if mode not in ("octet", "netascii"):
            xfer.send_error(ERR_ILLEGAL_OP, f"Unsupported mode: {mode}")
            print(f"  Unsupported mode {mode!r}: {filename!r}")
            return

        if path is None:
            xfer.send_error(ERR_ACCESS_VIOLATION, "Access violation")
            print(f"  Access violation: {filename!r}")
            return

        accepted, blksize, timeout = _negotiate(options)
        xfer.settimeout(timeout)

        print(
            f"WRQ  {filename!r} from {client_addr[0]}:{client_addr[1]}"
            f" (blksize={blksize}, mode={mode})"
        )

        if accepted:
            xfer.send(_pack_oack(accepted))
        else:
            xfer.send(_pack_ack(0))

        buf = bytearray()
        expected = 1
        while True:
            try:
                data = xfer.recv(blksize + 4)
            except socket.timeout:
                print(f"  !! {filename!r}: timed out waiting for data")
                return

            op = struct.unpack("!H", data[:2])[0]
            if op == OP_ERROR:
                print(f"  !! {filename!r}: client error")
                return
            if op != OP_DATA:
                xfer.send_error(ERR_ILLEGAL_OP, "Expected DATA")
                return

            blk = struct.unpack("!H", data[2:4])[0]
            payload = data[4:]

            if blk == expected:
                buf.extend(payload)
                xfer.send(_pack_ack(blk))
                # block numbers wrap around after 65535
                expected = (expected + 1) & 0xFFFF
                if len(payload) < blksize:
                    break
            elif blk < expected:
                xfer.send(_pack_ack(blk))

        if mode == "netascii":
            buf = bytearray(_from_netascii(bytes(buf)))

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(buf)
        print(f"  <- received {filename!r} ({len(buf)} bytes)")

    except (ConnectionError, TimeoutError) as e:
        print(f"  !! {filename!r}: {e}")
    finally:
        xfer.close()


def serve(bind: str, port: int, directory: str, allow_write: bool) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((bind, port))
    except PermissionError:
        print(
            f"Error: Permission denied for port {port}. "
            f"Try a higher port or run with elevated privileges.",
            file=sys.stderr,
        )
        sys.exit(1)
    except OSError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    display = bind or "0.0.0.0"
    mode = "read/write" if allow_write else "read-only"
    print(f"Serving TFTP on {display} port {port} ({directory}) [{mode}] ...")

    try:
        while True:
            data, client_addr = sock.recvfrom(65536)
            if len(data) < 4:
                continue

            opcode, filename, mode, options = _parse_request(data)
            if filename is None:
                continue

            server_bind = sock.getsockname()[0]

            if opcode == OP_RRQ:
                t = threading.Thread(
                    target=_handle_rrq,
                    args=(directory, filename, mode, options, client_addr, server_bind),
                    daemon=True,
                )
                t.start()
            elif opcode == OP_WRQ:
                if not allow_write:
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    s.bind((server_bind, 0))
                    s.sendto(
                        _pack_error(ERR_ACCESS_VIOLATION, "Write not permitted"),
                        client_addr,
                    )
                    s.close()
                    print(
                        f"WRQ  {filename!r} from {client_addr[0]}:{client_addr[1]}"
                        f" -> denied (read-only)"
                    )
                    continue
                t = threading.Thread(
                    target=_handle_wrq,
                    args=(directory, filename, mode, options, client_addr, server_bind),
                    daemon=True,
                )
                t.start()
            else:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.bind((server_bind, 0))
                s.sendto(_pack_error(ERR_ILLEGAL_OP, "Illegal operation"), client_addr)
                s.close()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        sock.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="A simple TFTP server.",
        prog="tftp",
    )
    parser.add_argument(
        "port",
        nargs="?",
        type=int,
        default=69,
        help="port number (default: 69)",
    )
    parser.add_argument(
        "-b",
        "--bind",
        default="",
        help="bind address (default: all interfaces)",
    )
    parser.add_argument(
        "-d",
        "--directory",
        default=os.getcwd(),
        help="serve this directory (default: current directory)",
    )
    parser.add_argument(
        "-w",
        "--write",
        action="store_true",
        help="allow upload (WRQ) requests",
    )
    args = parser.parse_args()

    directory = os.path.realpath(args.directory)
    if not os.path.isdir(directory):
        print(f"Error: {directory} is not a directory", file=sys.stderr)
        sys.exit(1)

    serve(args.bind, args.port, directory, args.write)


if __name__ == "__main__":
    main()
