"""Small, read-only BaoStock transport using its published 0.9.3 wire protocol.

No SDK is installed/imported. The public anonymous account is the SDK default.
Official protocol source: https://pypi.org/project/baostock/0.9.3/
Raw responses belong in the private data store, never in this repository.
"""

from dataclasses import dataclass
import json
import socket
import time
import zlib

SEP = "\x01"
TERMINATOR = b"<![CDATA[]]>\n"
COMPRESSED = {"96", "99", "9B", "9D"}


class ProviderError(RuntimeError):
    def __init__(self, code, message, *, wire=b""):
        self.code = str(code)
        self.wire = wire
        super().__init__(self.code + ": " + str(message)[:300])


@dataclass(frozen=True)
class Table:
    fields: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    wire: bytes


def decode_table(wire, *, method, row_index=6, field_index=8, max_decoded_bytes=64_000_000):
    if len(wire) < 21 or not wire.endswith(TERMINATOR):
        raise ProviderError("FRAME_INVALID", "Incomplete provider response")
    header = wire[:21].decode("ascii").split(SEP)
    if len(header) != 3:
        raise ProviderError("HEADER_INVALID", "Invalid provider header")
    if header[1] in COMPRESSED:
        size = int(header[2])
        inflater = zlib.decompressobj()
        body = inflater.decompress(wire[21:21 + size], max_decoded_bytes + 1)
        if len(body) > max_decoded_bytes or not inflater.eof or inflater.unconsumed_tail:
            raise ProviderError("RESPONSE_TOO_LARGE", "Decoded response exceeds the limit")
    else:
        body = wire[21:]
        if len(body) > max_decoded_bytes:
            raise ProviderError("RESPONSE_TOO_LARGE", "Provider response exceeds the limit")
    parts = body.decode("utf-8").split(SEP)
    if len(parts) < 2:
        raise ProviderError("BODY_INVALID", "Missing response status")
    if parts[0] != "0":
        raise ProviderError(parts[0], parts[1])
    if len(parts) < 3 or parts[2] != method:
        raise ProviderError("METHOD_MISMATCH", "Response does not match the request")
    if row_index is None:
        return Table((), (), wire)
    if len(parts) <= max(row_index, field_index):
        raise ProviderError("TABLE_INVALID", "Missing data or columns")
    fields = tuple(name.strip() for name in parts[field_index].split(","))
    rows = json.loads(parts[row_index])["record"] if parts[row_index].strip() else []
    if not fields or len(set(fields)) != len(fields):
        raise ProviderError("COLUMNS_INVALID", "Missing or duplicate provider columns")
    if not isinstance(rows, list) or any(not isinstance(row, list) or len(row) != len(fields)
                                        or any(not isinstance(x, str) for x in row) for row in rows):
        raise ProviderError("TABLE_INVALID", "Unexpected provider row shape or value types")
    return Table(fields, tuple(tuple(row) for row in rows), wire)


class BaoStockClient:
    """One connection, sequential requests, bounded responses and deliberate pacing."""

    def __init__(self, *, timeout=30, pause_seconds=1.0, max_wire_bytes=16_000_000):
        self.timeout = timeout
        self.pause_seconds = pause_seconds
        self.max_wire_bytes = max_wire_bytes
        self.socket = None
        self.last_request_finished = 0.0

    def __enter__(self):
        self.socket = socket.create_connection(("public-api.baostock.com", 10030), timeout=min(30, self.timeout))
        self.socket.settimeout(min(30, self.timeout))
        try:
            self.request("00", ("login", "anonymous", "123456", "0"), row_index=None)
        except Exception:
            self.socket.close()
            self.socket = None
            raise
        return self

    def __exit__(self, *_):
        if self.socket is not None:
            self.socket.close()
            self.socket = None

    def request(self, kind, parts, *, row_index=6, field_index=8):
        if self.socket is None:
            raise ProviderError("NOT_CONNECTED", "Use the client as a context manager")
        delay = self.pause_seconds - (time.monotonic() - self.last_request_finished)
        if delay > 0:
            time.sleep(delay)
        body = SEP.join(str(value) for value in parts)
        message = "00.9.30" + SEP + kind + SEP + str(len(body)).zfill(10) + body
        wire = (message + SEP + str(zlib.crc32(message.encode())) + "\n").encode()
        self.socket.sendall(wire)
        received = bytearray()
        started = time.monotonic()
        while not received.endswith(TERMINATOR):
            remaining = self.timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise ProviderError("RESPONSE_TIMEOUT", "Provider response timed out", wire=bytes(received))
            self.socket.settimeout(min(30, remaining))
            try:
                chunk = self.socket.recv(65536)
            except TimeoutError:
                continue
            except OSError as error:
                raise ProviderError("CONNECTION_ERROR", str(error), wire=bytes(received)) from error
            if not chunk:
                raise ProviderError("CONNECTION_CLOSED", "Provider closed an unfinished response", wire=bytes(received))
            received.extend(chunk)
            if len(received) > self.max_wire_bytes:
                raise ProviderError("RESPONSE_TOO_LARGE", "Wire response exceeds the limit", wire=bytes(received))
        self.last_request_finished = time.monotonic()
        try:
            return decode_table(bytes(received), method=parts[0], row_index=row_index, field_index=field_index)
        except ProviderError as error:
            error.wire = bytes(received)
            raise

    def daily(self, day):
        return self.request("98", ("query_daily_history_k_AStock", "anonymous", day),
                            row_index=4, field_index=5)

    def daily_adjustments(self, day):
        return self.request("9C", ("query_daily_adjust_factor", "anonymous", day),
                            row_index=4, field_index=5)

    def calendar(self, start, end, page=1):
        return self.request("33", ("query_trade_dates", "anonymous", page, 2000, start, end), field_index=9)

    def universe(self, day, page=1):
        return self.request("35", ("query_all_stock", "anonymous", page, 2000, day), field_index=8)

    def securities(self, code="", page=1):
        return self.request("45", ("query_stock_basic", "anonymous", page, 2000, code, ""), field_index=9)

    def history(self, symbol, start, end, *, adjustflag="3", page=1):
        fields = "date,code,open,high,low,close,preclose,volume,amount,adjustflag,tradestatus,isST"
        return self.request("95", ("query_history_k_data_plus", "anonymous", page, 2000,
                                  symbol, fields, start, end, "d", adjustflag), field_index=8)

    def adjustments(self, symbol, start, end, page=1):
        return self.request("15", ("query_adjust_factor", "anonymous", page, 2000, symbol, start, end),
                            field_index=10)
