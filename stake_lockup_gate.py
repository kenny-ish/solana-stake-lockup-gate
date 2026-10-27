#!/usr/bin/env python3
"""Read-only Solana RPC checks. Python 3.10+, standard library only."""

import argparse
import base64
import binascii
import json
import math
import struct
import sys
import urllib.error
import urllib.parse
import urllib.request

ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
SYSVAR_OWNER = "Sysvar1111111111111111111111111111111111111"


def b58encode(raw):
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, digit = divmod(n, 58)
        out = ALPHABET[digit] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + out


def pubkey(text):
    if not isinstance(text, str) or not 32 <= len(text) <= 44:
        raise ValueError("public key must be a base58-encoded 32-byte value")
    n = 0
    for char in text:
        if char not in ALPHABET:
            raise ValueError("invalid base58 character")
        n = n * 58 + ALPHABET.index(char)
    raw = b"\0" * (len(text) - len(text.lstrip("1")))
    raw += n.to_bytes((n.bit_length() + 7) // 8, "big")
    if len(raw) != 32:
        raise ValueError("public key must contain 32 bytes")
    return raw


def rpc_call(url, method, params, timeout=15):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.netloc:
        raise ValueError("RPC URL must use http or https")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be positive and finite")
    body = json.dumps({"jsonrpc": "2.0", "id": 1,
                       "method": method, "params": params}).encode()
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read(1_048_577)
    except (OSError, urllib.error.URLError) as exc:
        raise ValueError("RPC transport failed: " + type(exc).__name__) from None
    if len(payload) > 1_048_576:
        raise ValueError("RPC response exceeds 1 MiB")
    try:
        envelope = json.loads(payload)
    except (ValueError, UnicodeError):
        raise ValueError("RPC returned invalid JSON") from None
    if not isinstance(envelope, dict) or envelope.get("jsonrpc") != "2.0":
        raise ValueError("invalid JSON-RPC envelope")
    if type(envelope.get("id")) is not int or envelope["id"] != 1:
        raise ValueError("JSON-RPC response ID mismatch")
    if "error" in envelope:
        raise ValueError("RPC returned an error; check endpoint access and inputs")
    if "result" not in envelope:
        raise ValueError("JSON-RPC result is missing")
    return envelope["result"]


def account_data(account, owner):
    if not isinstance(account, dict):
        raise ValueError("account does not exist")
    if account.get("owner") != owner or account.get("executable") is not False:
        raise ValueError("unexpected account owner or executable flag")
    data = account.get("data")
    if (not isinstance(data, list) or len(data) != 2 or data[1] != "base64"
            or not isinstance(data[0], str)):
        raise ValueError("expected base64 account data")
    try:
        raw = base64.b64decode(data[0], validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("invalid base64 account data") from None
    if "space" in account and account["space"] != len(raw):
        raise ValueError("account space does not match data length")
    return raw


def snapshot(result):
    if not isinstance(result, dict) or not isinstance(result.get("context"), dict):
        raise ValueError("missing RPC context")
    slot = result["context"].get("slot")
    values = result.get("value")
    if type(slot) is not int or not 0 <= slot < 2**64:
        raise ValueError("invalid context slot")
    if not isinstance(values, list) or len(values) != 2:
        raise ValueError("expected two accounts in one RPC snapshot")
    return slot, values


def parser_base(description):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--rpc", required=True, help="user-supplied JSON-RPC URL")
    parser.add_argument("--commitment", choices=("finalized", "confirmed"),
                        default="finalized")
    parser.add_argument("--timeout", type=float, default=15, help="seconds, default 15")
    return parser


STAKE_OWNER = "Stake11111111111111111111111111111111111111"
CLOCK = "SysvarC1ock11111111111111111111111111111111"


def decode_stake(raw):
    if len(raw) != 200:
        raise ValueError("expected a 200-byte stake account")
    state = struct.unpack_from("<I", raw)[0]
    if state not in (1, 2):
        raise ValueError("only Initialized and Stake states are supported")
    timestamp, epoch = struct.unpack_from("<qQ", raw, 76)
    return {"state": "initialized" if state == 1 else "delegated",
            "withdrawer": b58encode(raw[44:76]),
            "lockup_timestamp": timestamp, "lockup_epoch": epoch,
            "custodian": b58encode(raw[92:124])}


def decode_clock(raw):
    if len(raw) != 40:
        raise ValueError("expected a 40-byte Clock sysvar")
    slot, _, epoch, _, timestamp = struct.unpack("<QqQQq", raw)
    return {"slot": slot, "epoch": epoch, "unix_timestamp": timestamp}


def evaluate(stake, clock, withdrawer, custodian=None):
    withdrawer_raw = pubkey(withdrawer)
    if custodian is not None:
        pubkey(custodian)
    seconds = max(0, stake["lockup_timestamp"] - clock["unix_timestamp"])
    epochs = max(0, stake["lockup_epoch"] - clock["epoch"])
    locked = bool(seconds or epochs)
    authority_ok = withdrawer == stake["withdrawer"] and any(withdrawer_raw)
    custodian_ok = (custodian is not None and custodian == stake["custodian"]
                    and any(pubkey(custodian)))
    blockers = []
    if not authority_ok:
        blockers.append("withdrawer-mismatch-or-zero-key")
    if locked and not custodian_ok:
        blockers.append("lockup-needs-custodian")
    return {"stake_state": stake["state"], "epoch": clock["epoch"],
            "unix_timestamp": clock["unix_timestamp"],
            "onchain_withdrawer": stake["withdrawer"],
            "onchain_custodian": stake["custodian"],
            "lockup_timestamp": stake["lockup_timestamp"],
            "lockup_epoch": stake["lockup_epoch"],
            "lockup_in_force": locked, "seconds_remaining": seconds,
            "epochs_remaining": epochs, "withdrawer_matches": bool(authority_ok),
            "custodian_matches": bool(custodian_ok), "blockers": blockers,
            "authority_lockup_gate": "blocked" if blockers else "pass"}


def inspect_stake(rpc, address, withdrawer, custodian=None, commitment="finalized"):
    for key in (address, withdrawer):
        pubkey(key)
    if custodian is not None:
        pubkey(custodian)
    result = rpc("getMultipleAccounts", [[address, CLOCK],
                 {"encoding": "base64", "commitment": commitment}])
    slot, values = snapshot(result)
    stake = decode_stake(account_data(values[0], STAKE_OWNER))
    clock = decode_clock(account_data(values[1], SYSVAR_OWNER))
    if clock["slot"] != slot:
        raise ValueError("Clock slot does not match RPC context")
    return {"stake_account": address, "context_slot": slot,
            **evaluate(stake, clock, withdrawer, custodian)}


def main(argv=None, rpc=None):
    parser = parser_base("Check stake withdrawal authority and lockup gates only.")
    parser.add_argument("stake_account", help="initialized or delegated stake account")
    parser.add_argument("--withdrawer", required=True, help="assumed withdrawal signer")
    parser.add_argument("--custodian", help="optional assumed custodian signer")
    args = parser.parse_args(argv)
    try:
        call = rpc or (lambda method, params: rpc_call(
            args.rpc, method, params, args.timeout))
        report = inspect_stake(call, args.stake_account, args.withdrawer,
                               args.custodian, args.commitment)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report["authority_lockup_gate"] == "pass" else 1
    except ValueError as exc:
        print("error: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
