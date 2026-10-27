import base64
import contextlib
import io
import json
import struct
import unittest
from unittest import mock
import stake_lockup_gate as m

# Mainnet RPC capture on 2026-10-10 UTC; account fields checked with jsonParsed.
REAL_ADDRESS = 'FtCjZ5ig6VVZY4F8AurERQB1JcG3jYj5QAeVCS6B8L5S'
REAL_STAKE = 'AgAAAIDVIgAAAAAAPexho4hIVFakx2D0RGW+vAT1LVRrkP4mMpI6SGURRCs97GGjiEhUVqTHYPREZb68BPUtVGuQ/iYykjpIZRFEKwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABBZSFn6O+y3QerYPgZNPVnBoKeXB/07AwCFmeSpPMSR4rUDIAMAAAD7AwAAAAAAAB0EAAAAAAAAAAAAAAAA0D/bpUmZAAAAAAAAAAA='
REAL_CLOCK = 'odIhGwAAAAD4/MhqAAAAAB0EAAAAAAAAHgQAAAAAAABrAcpqAAAAAA=='
EXPECTED_STAKE = {'state': 'delegated', 'withdrawer': '5Aivjf4A7pmHwgFnZKMtTqf65FXyUovyuCzNDZj4pC98', 'lockup_timestamp': 0, 'lockup_epoch': 0, 'custodian': '11111111111111111111111111111111'}
EXPECTED_CLOCK = {'slot': 455201441, 'epoch': 1053, 'unix_timestamp': 1791623531}

WITHDRAWER = m.b58encode(bytes([7]) * 32)
CUSTODIAN = m.b58encode(bytes([8]) * 32)
OTHER = m.b58encode(bytes([9]) * 32)


def stake_data(state=2, timestamp=1000, epoch=10, custodian=CUSTODIAN):
    raw = bytearray(200)
    struct.pack_into("<I", raw, 0, state)
    raw[44:76] = m.pubkey(WITHDRAWER)
    struct.pack_into("<qQ", raw, 76, timestamp, epoch)
    raw[92:124] = m.pubkey(custodian)
    return bytes(raw)


def clock_data(slot=100, epoch=10, timestamp=1000):
    return struct.pack("<QqQQq", slot, 0, epoch, epoch + 1, timestamp)


def account(raw, owner):
    return {"owner": owner, "executable": False,
            "data": [base64.b64encode(raw).decode(), "base64"], "space": len(raw)}


def fake_rpc(timestamp=1000):
    return mock.Mock(return_value={"context": {"slot": 100}, "value": [
        account(stake_data(timestamp=timestamp), m.STAKE_OWNER),
        account(clock_data(), m.SYSVAR_OWNER)]})


class StakeTests(unittest.TestCase):
    def check(self, timestamp=1000, epoch=10, withdrawer=WITHDRAWER,
              custodian=None, onchain_custodian=CUSTODIAN):
        stake = m.decode_stake(stake_data(timestamp=timestamp, epoch=epoch,
                                         custodian=onchain_custodian))
        return m.evaluate(stake, m.decode_clock(clock_data()), withdrawer, custodian)

    def test_mainnet_account_and_clock(self):
        stake = m.decode_stake(base64.b64decode(REAL_STAKE))
        clock = m.decode_clock(base64.b64decode(REAL_CLOCK))
        self.assertEqual(clock, EXPECTED_CLOCK)
        self.assertEqual(stake, EXPECTED_STAKE)
        result = m.evaluate(stake, clock, EXPECTED_STAKE["withdrawer"])
        self.assertFalse(result["lockup_in_force"])
        self.assertEqual(result["seconds_remaining"], 0)
        self.assertEqual(result["epochs_remaining"], 0)
        self.assertEqual(result["authority_lockup_gate"], "pass")

    def test_exact_boundaries_release(self):
        self.assertEqual(self.check()["authority_lockup_gate"], "pass")

    def test_time_alone_locks(self):
        result = self.check(timestamp=1001)
        self.assertEqual(result["seconds_remaining"], 1)
        self.assertEqual(result["epochs_remaining"], 0)
        self.assertEqual(result["authority_lockup_gate"], "blocked")

    def test_epoch_alone_locks(self):
        result = self.check(epoch=11)
        self.assertTrue(result["lockup_in_force"])
        self.assertEqual(result["epochs_remaining"], 1)
        self.assertEqual(result["seconds_remaining"], 0)

    def test_both_conditions_must_expire(self):
        result = self.check(timestamp=1100, epoch=12)
        self.assertEqual((result["seconds_remaining"], result["epochs_remaining"]), (100, 2))
        self.assertEqual(result["blockers"], ["lockup-needs-custodian"])

    def test_custodian_override(self):
        result = self.check(timestamp=1100, epoch=12, custodian=CUSTODIAN)
        self.assertTrue(result["lockup_in_force"])
        self.assertTrue(result["custodian_matches"])
        self.assertEqual(result["authority_lockup_gate"], "pass")

    def test_wrong_custodian(self):
        self.assertEqual(self.check(epoch=11, custodian=OTHER)["authority_lockup_gate"], "blocked")

    def test_custodian_does_not_replace_withdrawer(self):
        result = self.check(epoch=11, withdrawer=OTHER, custodian=CUSTODIAN)
        self.assertEqual(result["blockers"], ["withdrawer-mismatch-or-zero-key"])

    def test_zero_custodian_not_assumed_signable(self):
        zero = "1" * 32
        result = self.check(epoch=11, custodian=zero, onchain_custodian=zero)
        self.assertFalse(result["custodian_matches"])
        self.assertEqual(result["authority_lockup_gate"], "blocked")

    def test_signed_timestamp_and_initialized_state(self):
        self.assertFalse(self.check(timestamp=-1, epoch=0)["lockup_in_force"])
        self.assertEqual(m.decode_stake(stake_data(state=1))["state"], "initialized")

    def test_invalid_state_and_size(self):
        for raw in (stake_data(state=0), stake_data(state=3), stake_data(state=9),
                    stake_data()[:-1], stake_data() + b"\0"):
            with self.assertRaises(ValueError):
                m.decode_stake(raw)
        with self.assertRaises(ValueError):
            m.decode_clock(clock_data()[:-1])

    def test_one_snapshot_request(self):
        rpc = fake_rpc()
        self.assertEqual(m.inspect_stake(rpc, REAL_ADDRESS, WITHDRAWER)["authority_lockup_gate"], "pass")
        rpc.assert_called_once_with("getMultipleAccounts", [[REAL_ADDRESS, m.CLOCK],
                                    {"encoding": "base64", "commitment": "finalized"}])

    def test_clock_context_mismatch(self):
        rpc = fake_rpc()
        rpc.return_value["context"]["slot"] = 101
        with self.assertRaises(ValueError):
            m.inspect_stake(rpc, REAL_ADDRESS, WITHDRAWER)

    def test_cli_exit_codes(self):
        args = ["--rpc", "https://rpc.invalid", REAL_ADDRESS, "--withdrawer", WITHDRAWER]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m.main(args, rpc=fake_rpc()), 0)
            self.assertEqual(m.main(args, rpc=fake_rpc(timestamp=1001)), 1)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(m.main(args, rpc=mock.Mock(return_value={})), 2)


class TransportTests(unittest.TestCase):
    def response(self, payload):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = payload
        return response

    def test_transport_success_and_request(self):
        payload = b'{"jsonrpc":"2.0","id":1,"result":{"ok":true}}'
        with mock.patch.object(m.urllib.request, "urlopen", return_value=self.response(payload)) as opened:
            self.assertEqual(m.rpc_call("https://rpc.invalid", "getMultipleAccounts", []), {"ok": True})
        request = opened.call_args.args[0]
        self.assertEqual(json.loads(request.data)["method"], "getMultipleAccounts")

    def test_transport_bad_responses(self):
        cases = [b'{}', b'[]', b'bad', b'\xff',
                 b'{"jsonrpc":"2.0","id":2,"result":0}',
                 b'{"jsonrpc":"2.0","id":1,"error":{"code":-32602}}',
                 b'{"jsonrpc":"2.0","id":1}', b' ' * 1_048_577]
        for payload in cases:
            with self.subTest(payload=payload[:60]):
                with mock.patch.object(m.urllib.request, "urlopen", return_value=self.response(payload)):
                    with self.assertRaises(ValueError):
                        m.rpc_call("https://rpc.invalid", "getMultipleAccounts", [])

    def test_transport_failure_and_validation(self):
        with mock.patch.object(m.urllib.request, "urlopen", side_effect=TimeoutError):
            with self.assertRaises(ValueError):
                m.rpc_call("https://rpc.invalid", "getMultipleAccounts", [])
        for url, timeout in [("file:///tmp/a", 1), ("https://rpc.invalid", 0),
                             ("https://rpc.invalid", float("nan"))]:
            with self.assertRaises(ValueError):
                m.rpc_call(url, "getMultipleAccounts", [], timeout)

    def test_pubkeys(self):
        for raw in (bytes(32), bytes(range(32)), b"\xff" * 32):
            self.assertEqual(m.pubkey(m.b58encode(raw)), raw)
        for value in ("O" * 32, "1" * 31, "1" * 33, "z" * 44):
            with self.assertRaises(ValueError):
                m.pubkey(value)

    def test_account_validation(self):
        valid = {"owner": "owner", "executable": False, "data": ["AA==", "base64"]}
        self.assertEqual(m.account_data(valid, "owner"), b"\0")
        for change in ({"owner": "other"}, {"executable": True},
                       {"data": ["!", "base64"]}, {"data": ["AA==", "jsonParsed"]},
                       {"space": 2}):
            with self.assertRaises(ValueError):
                m.account_data({**valid, **change}, "owner")
        with self.assertRaises(ValueError):
            m.account_data(None, "owner")

    def test_snapshot_validation(self):
        for result in (None, {}, {"context": {"slot": True}, "value": [None, None]},
                       {"context": {"slot": 1}, "value": []}):
            with self.assertRaises(ValueError):
                m.snapshot(result)


if __name__ == "__main__":
    unittest.main()
