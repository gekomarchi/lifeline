# Lifeline

Lifeline is a gas guardian for reactive automations on Reactive Network. It runs on testnets only.

## Network statistics

One read-only command scans chain data on Reactive Network testnet (Lasna) and on the destination chains. It writes each network number and the guest-candidate list with the query name, the chain and the block heights. It needs no key and sends no transaction.

```
python -m lifeline.stats --rpc <chain>=<url> [--rpc <chain>=<url> ...] [--heights <file>] [--out <dir>]
```

Python 3.10 or later. The command has no dependency outside the standard library.

### Flags

| Flag | Meaning |
|---|---|
| `--rpc <chain>=<url>` | RPC URL for one chain. Repeat the flag for more chains. More than one URL for a chain makes an ordered list: a retry uses the next URL. A chain with no `--rpc` uses the `rpc` list of `config/network.json`. |
| `--heights <file>` | Use the heights, parameters and saved state of a `heights.json` file. The run reads no chain head and writes an identical `data.json`. |
| `--out <dir>` | Output directory. Default: `evidence/network/`. |
| `--config <file>`, `--cache <dir>` | Configuration file (default `config/network.json`) and cache directory (default `.cache/chain`). |
| `--retries <n>`, `--backoff <seconds>` | Retries of one request (default 8) and the first retry delay (default 1 s, doubled each time, 60 s at most). |

Chain names: `lasna`, `base-sepolia`, `unichain-sepolia`.

### Heights

A first run has no `--heights`. For each chain, `to` is the head block at the start of the run, and `from` is the first block with a timestamp at or after 2026-11-05 00:00 EET. Event scans cover `from..to`. State reads (`debt`, `reserves`) are made at `to`.

A run with `--heights` asks the node for each state value again. If the node has no state at the old block, the run uses the value saved in the file and names that value on standard output. If the node gives a different value, the run keeps the saved value and reports the difference on standard output.

### Output files

| File | Content |
|---|---|
| `data.json` | Heights, parameters, and for each query its chains, its definition, its status (`ok` or `not_configured`) and its result. Amounts are decimal strings in wei. |
| `table.md` | One row for each number, with query name, chain, `from` and `to`, and the definition of each query. |
| `heights.json` | The heights snapshot: `from`, `to` and the block timestamp of `to` (`toTimestamp`) for each chain, the parameters, and each state value read at `to`. |

No file contains a run time or an RPC URL. Standard output shows each number with its query name, chain and heights, and the RPC methods that each query sent.

Queries: `lasna_debt_events`, `destination_debt_events`, `stopped_contracts`, `cron_stopped_share`, `repeat_debtors`, `dropped_callbacks`, `fastfill_fill_times`, `cron_poller_ward_dead_time`, `fastfill_reactive_reserves`, `guest_candidates`.

### Dropped callbacks

The `dropped_callbacks` number is derived from debt intervals. It is not a count of observed reverted transactions. The query joins `CallbackRequest` logs on Lasna with debt intervals on each destination proxy: from `BlacklistContract(recipient)` to the next `WhitelistContract(recipient)`, or to `to`. A request is counted as dropped if its recipient has an open interval at the block timestamp of the request, because the proxy rejects a callback to a contract in debt with `InDebt`. A request within the latency margin (5 s) of an interval edge is counted separately as uncertain. So is a request to a recipient whose interval is open at `to`, if the request comes within the margin of the end of the destination window or after it. A request to a chain that is not configured is counted separately as not scanned. An interval that opened before `from` and did not close in the range is not visible. The query sends log queries and block-header reads only.

### Failures and the cache

The reader scans logs in block chunks and uses a smaller chunk after a block-range or response-size error. It retries rate-limit, timeout and server errors with exponential backoff. If the retries of one request are used, the run exits with a non-zero status and writes no file. The three files are written together: a run that cannot write all of them leaves the output directory as it was. Finished chunks stay in `.cache/chain`, so the next run continues from there.

### `config/network.json`

| Entry | Meaning |
|---|---|
| `chains.<name>.chainId` | Chain ID. The reader refuses an RPC URL that answers for a different chain. |
| `chains.<name>.role` | `reactive` for Lasna, `destination` for a chain with a callback proxy. |
| `chains.<name>.ledger` | The contract that holds `debt` and `reserves`: the system contract on Lasna, the callback proxy on a destination chain. |
| `chains.<name>.rpc` | Default RPC URLs, in order. |
| `subjects.fastfillReactive` | `chain` and `address` of the contract for `fastfill_reactive_reserves`. |
| `subjects.fastfill` | `deposit` and `fill`, each with `chain`, `address` and `event` (an event declaration with parameter names), and `joinField` (an indexed parameter that both events have). The optional `where` gives fixed values for indexed parameters. Used by `fastfill_fill_times`. |
| `subjects.cronPoller` | `chain`, `address` and `ward` of the existing cron poller. Used by `cron_poller_ward_dead_time`. |

A query whose subject is absent does not run. It has `"status": "not_configured"` in `data.json`, and standard output names the missing key. `subjects.cronPoller` is not configured at this time.

Add a destination chain as one more entry in `chains` with the role `destination`.

### Tests

```
pip install -e ".[test]"
python -m pytest
```

The tests run the real command and the real reader against a local JSON-RPC server.

## Development

The contracts are one Foundry project at the repository root: `src/`, `test/`, `lib/`.

| Item | Value |
|---|---|
| Foundry | 1.10.0 (`forge` commit `083e229`) |
| Solidity compiler | 0.8.30, EVM version `cancun`, optimizer with 200 runs |
| `lib/reactive-lib-omni` | commit `3ade0dc` |
| `lib/forge-std` | v1.17.0 (commit `f3dae6e`) |
| `LASNA_RPC_URL` | RPC URL of Reactive Network testnet (Lasna, chain ID 5318007). The node must serve the state of the fork block. |
| Fork block | 8,918,212 on Lasna |

```
git submodule update --init
export LASNA_RPC_URL=<url>
forge test
```

The fork tests in `test/LifelineHubLasna.t.sol` run against the system contract code of the fork block. They fail if `LASNA_RPC_URL` is not set. `forge test --match-contract LifelineTypesTest` runs the tests that need no fork.

## License

Apache License 2.0. See `LICENSE`.
