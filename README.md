# Solana Stake Lockup Gate

Check whether an assumed withdrawal signer passes a Solana stake account's authority and lockup gates. It explains whether time, epoch, or a missing custodian signature blocks these gates without sending a transaction.

## Example

Actual mainnet-beta output collected on 2026-10-10 UTC. Later runs will differ.

    python stake_lockup_gate.py --rpc https://api.mainnet-beta.solana.com FtCjZ5ig6VVZY4F8AurERQB1JcG3jYj5QAeVCS6B8L5S --withdrawer 5Aivjf4A7pmHwgFnZKMtTqf65FXyUovyuCzNDZj4pC98

    {
      "authority_lockup_gate": "pass",
      "blockers": [],
      "context_slot": 455201068,
      "custodian_matches": false,
      "epoch": 1053,
      "epochs_remaining": 0,
      "lockup_epoch": 0,
      "lockup_in_force": false,
      "lockup_timestamp": 0,
      "onchain_custodian": "11111111111111111111111111111111",
      "onchain_withdrawer": "5Aivjf4A7pmHwgFnZKMtTqf65FXyUovyuCzNDZj4pC98",
      "seconds_remaining": 0,
      "stake_account": "FtCjZ5ig6VVZY4F8AurERQB1JcG3jYj5QAeVCS6B8L5S",
      "stake_state": "delegated",
      "unix_timestamp": 1791623447,
      "withdrawer_matches": true
    }

## Usage

`python stake_lockup_gate.py --rpc URL STAKE_ACCOUNT --withdrawer PUBKEY [--custodian PUBKEY] [--commitment finalized|confirmed] [--timeout SECONDS]`

The public keys describe signers you assume are available. No signatures or private keys are requested or verified. The default commitment is finalized and the timeout is 15 seconds. Exit codes: 0 means these gates pass, 1 means blocked, and 2 means invalid input or unavailable data.

## How it works

One `getMultipleAccounts` call reads the stake account and Clock at the same bank. Only 200-byte Initialized and Stake accounts are supported. The owner, binary layout, and Clock context slot are checked.

Lockup remains in force if either its timestamp or its epoch is ahead of Clock. Equality releases that condition. A matching nonzero custodian can override lockup, but cannot replace the withdrawal authority. Both remaining counters are independent; epochs are not converted to seconds.

A pass is not a withdrawal simulation. This tool does not check stake activation, delegated funds, available lamports, rent reserves, transaction signatures, or instruction construction. It never estimates a withdrawable balance.

Offline tests contain a real mainnet stake account and Clock captured together on 2026-10-10 UTC. Account fields were checked with RPC `jsonParsed`; the Clock timestamp was checked against `getBlock` at its slot.

Layout and lockup reference: [Solana stake state](https://raw.githubusercontent.com/solana-program/stake/main/interface/src/state.rs). Python 3.10 or newer is required. No dependencies are needed. MIT licensed.

## Tests

`python -m unittest -v`
