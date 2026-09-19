from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch
import zlib

from quant.contracts import ContractError
from quant.data.acquisition import read_cached, save_response
from quant.data.baostock import BaoStockClient, ProviderError, SEP, TERMINATOR, decode_table


def frame(parts, kind="96"):
    body = SEP.join(parts).encode()
    if kind in ("96", "99"):
        body = zlib.compress(body)
    header = ("00.9.30" + SEP + kind + SEP + str(len(body)).zfill(10)).encode()
    return header + body + SEP.encode() + b"123" + TERMINATOR


def history_frame(records=None):
    records = records if records is not None else [["2020-01-02", "SYN001.SH", "10.0"]]
    return frame(["0", "success", "query_history_k_data_plus", "anonymous", "1", "2000",
                  json.dumps({"record": records}), "daily", "date, code, close"])


class AcquisitionTests(unittest.TestCase):
    def test_decode_compressed_history_and_new_daily_layout(self):
        history = decode_table(history_frame(), method="query_history_k_data_plus")
        self.assertEqual(("date", "code", "close"), history.fields)
        self.assertEqual("SYN001.SH", history.rows[0][1])
        wire = frame(["0", "success", "query_daily_history_k_AStock", "anonymous",
                      json.dumps({"record": [["2020-01-02", "SYN002.SZ"]]}), "date,code"], "99")
        table = decode_table(wire, method="query_daily_history_k_AStock", row_index=4, field_index=5)
        self.assertEqual((("2020-01-02", "SYN002.SZ"),), table.rows)

    def test_status_method_size_and_table_failures_are_not_empty_success(self):
        with self.assertRaisesRegex(ProviderError, "permission"):
            decode_table(frame(["400", "permission denied", "x"]), method="x")
        with self.assertRaisesRegex(ProviderError, "METHOD_MISMATCH"):
            decode_table(history_frame(), method="wrong_method")
        with self.assertRaisesRegex(ProviderError, "RESPONSE_TOO_LARGE"):
            decode_table(history_frame(), method="query_history_k_data_plus", max_decoded_bytes=20)
        with self.assertRaisesRegex(ProviderError, "TABLE_INVALID"):
            decode_table(history_frame([["two", "fields"]]), method="query_history_k_data_plus")
        with self.assertRaisesRegex(ProviderError, "FRAME_INVALID"):
            decode_table(history_frame()[:-1], method="query_history_k_data_plus")

    def test_socket_fragmentation_and_wire_budget(self):
        class FakeSocket:
            def __init__(self, chunks):
                self.chunks = iter(chunks)
            def sendall(self, _):
                pass
            def settimeout(self, _):
                pass
            def recv(self, _):
                return next(self.chunks, b"")
        wire = history_frame()
        client = BaoStockClient(pause_seconds=0)
        client.socket = FakeSocket([wire[:10], wire[10:50], wire[50:]])
        table = client.request("95", ("query_history_k_data_plus", "anonymous"))
        self.assertEqual(1, len(table.rows))
        client.socket = FakeSocket([wire])
        client.max_wire_bytes = 10
        with self.assertRaisesRegex(ProviderError, "RESPONSE_TOO_LARGE"):
            client.request("95", ("query_history_k_data_plus", "anonymous"))
        client.socket = FakeSocket([wire[:10]])
        client.max_wire_bytes = 10_000
        with self.assertRaisesRegex(ProviderError, "CONNECTION_CLOSED"):
            client.request("95", ("query_history_k_data_plus", "anonymous"))

    def test_timeout_retains_partial_wire_and_does_not_discard_complete_frame(self):
        class Clock:
            now = 0.0
        class SlowSocket:
            def __init__(self, content):
                self.content = content
            def sendall(self, _):
                pass
            def settimeout(self, _):
                pass
            def recv(self, _):
                Clock.now = 31.0
                return self.content
        wire = history_frame()
        for content, complete in ((wire[:40], False), (wire, True)):
            Clock.now = 0.0
            client = BaoStockClient(timeout=30, pause_seconds=0)
            client.socket = SlowSocket(content)
            with patch("quant.data.baostock.time.monotonic", side_effect=lambda: Clock.now):
                if complete:
                    self.assertEqual(1, len(client.request("95", ("query_history_k_data_plus", "anonymous")).rows))
                else:
                    with self.assertRaisesRegex(ProviderError, "RESPONSE_TIMEOUT") as caught:
                        client.request("95", ("query_history_k_data_plus", "anonymous"))
                    self.assertEqual(content, caught.exception.wire)

    def test_failed_login_closes_connection(self):
        class FakeSocket:
            closed = False
            def settimeout(self, _):
                pass
            def close(self):
                self.closed = True
        sock = FakeSocket()
        with patch("quant.data.baostock.socket.create_connection", return_value=sock), \
             patch.object(BaoStockClient, "request", side_effect=ProviderError("403", "no")):
            with self.assertRaises(ProviderError):
                with BaoStockClient():
                    self.fail("login must fail")
        self.assertTrue(sock.closed)

    def test_raw_cache_is_append_once_and_hash_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            table = decode_table(history_frame(), method="query_history_k_data_plus")
            save_response(root, "sample", table, query={"method": "synthetic"}, seconds=0)
            restored = read_cached(root, "sample", method="query_history_k_data_plus")
            self.assertEqual(table, restored)
            with self.assertRaisesRegex(ContractError, "ALREADY_EXISTS"):
                save_response(root, "sample", table, query={}, seconds=0)
            (root / "sample.bin").write_bytes(b"tampered")
            with self.assertRaisesRegex(ContractError, "HASH_MISMATCH"):
                read_cached(root, "sample", method="query_history_k_data_plus")


if __name__ == "__main__":
    unittest.main()
