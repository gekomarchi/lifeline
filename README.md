# lifeline | [![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE) ![Solidity](https://img.shields.io/badge/solidity-0.8.30-363636) ![Python](https://img.shields.io/badge/python-3.10%2B-3776AB) ![Network](https://img.shields.io/badge/network-testnet%20only-orange)

**Lifeline: the gas guardian for my reactive automations.** Reactive automations stop without a warning when their gas credit runs out. Lifeline is the guardian I set once: it refuels them across chains inside my budget, and it stops feeding one that breaks.

Lifeline runs on Reactive Network testnet only. Entry track: "Set It and Forget It".

| Contract | Chain (chain ID) | Address | Sourcify | Ledger of the chain |
|---|---|---|---|---|
| `LifelineHub` | Lasna (5318007) | `0xbba61E507e7CC334e57A62c39A776272DA6b07Dd` | [verified](https://repo.sourcify.dev/5318007/0xbba61E507e7CC334e57A62c39A776272DA6b07Dd) | system contract `0x8888888888888888888888888888888888888888` |
| `LifelineVault` | Unichain Sepolia (1301) | `0x89dbc693c78C8844b5A3294DE8f7c0f0adf1DC26` | [verified](https://repo.sourcify.dev/1301/0x89dbc693c78C8844b5A3294DE8f7c0f0adf1DC26) | callback proxy `0x775c9B990ADe401060B78D0e98684eEf8d2466C1` |
| `LifelineVault` | Base Sepolia (84532) | `0x1aA62Eaf6746Ffc7a19330dD255211680B4952B2` | [verified](https://repo.sourcify.dev/84532/0x1aA62Eaf6746Ffc7a19330dD255211680B4952B2) | callback proxy `0x476F44b11F5C942c58f4c8E3d0300E5710743585` |

The addresses come from [`deployments/manifest.json`](deployments/manifest.json).

## Problem

A reactive contract pays each reaction from its reserves at the system contract. A callback contract pays each callback from its reserves at the callback proxy. When the reserves are empty, the ledger records a debt and the contract gets no more work. There is no error: the automation only stops.

The first user is the builder. The builder's own automation `FastfillReactive` holds [60.37 lREACT](evidence/network/table.md) in reserves (query `fastfill_reactive_reserves`), and reserves cannot be withdrawn. The builder over-funded it, because a contract that runs out stops with no error.

Network numbers, from 2026-11-05 00:00 EET to the heights in [`evidence/network/heights.json`](evidence/network/heights.json):

| Number | Value | Query in [`data.json`](evidence/network/data.json) |
|---|---|---|
| Debt events on Lasna | [349](evidence/network/table.md) | `lasna_debt_events` |
| Debt events on the destination chains | [47](evidence/network/table.md): Base Sepolia 36, Unichain Sepolia 11 | `destination_debt_events` |
| Contracts with a debt event that are in debt at the end | Lasna [199 of 256](evidence/network/table.md), Base Sepolia [24 of 36](evidence/network/table.md), Unichain Sepolia [8 of 11](evidence/network/table.md) | `stopped_contracts` |
| Contracts on Lasna with 3 or more debt events | [14](evidence/network/table.md) | `repeat_debtors` |
| Dropped callbacks, derived from debt intervals (not a count of observed reverted transactions) | [146](evidence/network/table.md) | `dropped_callbacks` |

What Lifeline does for an enrolled automation:

- An automation that runs out works again. Measured on testnet: a Lasna target was alive [1 block](JUDGE.md#lasna-rescues-and-gaps) after its debt event, in each of 2 rescues. A Base Sepolia target was alive [5 blocks](JUDGE.md#loop-check-canary-base-sepolia) after its debt event, in 1 rescue. The time in seconds is not measured yet.
- An automation normally does not run out, because a slow gauge refills it first.
- A broken automation is cut off after a set number of rescues. Measured on testnet: a canary that reverts in a loop was [quarantined after 2 rescues](JUDGE.md#quarantine), and the hub paid 0 after that.

## How it works

```
Lasna target in debt ───────► system contract emits BlacklistContract(target)
                                 └► LifelineHub.react() ── depositTo(target)
destination target in debt ─► callback proxy emits BlacklistContract(target)
                                 └► LifelineHub.react() ── callback ──► LifelineVault.reconcile(target)
                                                                          └ proxy.depositTo(target)
cron tick ──────────────────► LifelineHub.react() ── refill Lasna targets below low
                                                   └ callback ──► LifelineVault.sweep(targets[])
```

| Part | What it does |
|---|---|
| Hub and vaults | One `LifelineHub` on Lasna guards reactive contracts. One `LifelineVault` on each destination chain guards callback contracts, on request of the hub. There is no server and no bridge. |
| Funds | Each contract has a principal (its balance) and a small fee float (its reserves at the ledger). The principal pays the targets, and the owner can withdraw it. The ledger takes fees from the float only: neither contract has `pay()`. Self-care moves principal into the float up to `selfReserveTarget`, at most `selfCareDailyCap` for each day, and never below `principalFloor`. |
| Airbag | The hub subscribes to the network's own debt event of each enrolled target, with the exact target address. For a Lasna target it pays `debt + (high - reserves)` in that reaction. For a destination target it requests one `reconcile` callback, and the vault pays from its own principal. |
| Gauge | On each cron tick (`Cron10000` by default) the hub refills each Lasna target that is below `low`, and requests one `sweep` on each vault that has a target. One sweep has at most 8 targets; a cursor continues on the next tick. |
| Quarantine | After `maxRescues` rescues inside `window` seconds, the next debt quarantines the target: no more payment. The hub decides for a Lasna target, the vault for a destination target. For a destination target the hub counts relay requests only: at `2 x maxRescues + 2` requests inside `window` it parks the target (`RelayLimited`). |

## Lifeline and Reactivate

Reactivate is an earlier project for the same problem.

| Part | Lifeline | Against Reactivate |
|---|---|---|
| Trigger | The network's own debt event. The guardian works when the application is dead. | New |
| Code in the target | None: `depositTo` needs no code in the target. | New |
| Infrastructure | No server and no deployment for each application. | New |
| Destination chains | Debt at a callback proxy is handled by the vault of that chain. | New |
| Budget and quarantine | `maxDebt`, `dailyCap`, `maxRescues` in `window`. | New |
| Principal isolation | The ledger can take only the fee float. | New |
| Threshold top-up | The gauge refills a target below `low`. | Not new |

The strongest alternative is a cron poller that calls `depositTo`, or large pre-funding. The experiment runs three identical canary automations side by side: A with no guardian, B with a cron poller, C with Lifeline. Result: [the three-arm table](JUDGE.md#three-arm-table). At the heights of that table no load is recorded (0 pokes for each arm), so it shows no difference between the arms yet.

## Quick start

You need `git`, Python 3.10 or later, and [Foundry](https://getfoundry.sh) (`forge`, `anvil`, `cast`).

```
git clone --recurse-submodules https://github.com/gekomarchi/lifeline
cd lifeline
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[ops]"
forge build
```

### Fork dry run

The dry run needs no funds. Start three local forks, each in its own terminal:

```
anvil --fork-url https://lasna-rpc.rnk.dev/ --port 8545 --disable-min-priority-fee
anvil --fork-url https://unichain-sepolia.drpc.org --fork-block-number 68574000 --port 8546 --disable-min-priority-fee
anvil --fork-url https://base-sepolia.gateway.tenderly.co --fork-block-number 62527000 --port 8547 --disable-min-priority-fee
```

In a fourth terminal, in the repository with the virtual environment active:

```
cast rpc anvil_mine 0x1 --rpc-url http://127.0.0.1:8545
cast wallet new        # a throwaway key: it prints an address and a private key
export LIFELINE_OWNER_KEY=<the private key>
for port in 8545 8546 8547; do cast rpc anvil_setBalance <the address> 0x56BC75E2D63100000 --rpc-url http://127.0.0.1:$port; done

DRY=(--manifest .cache/dry/manifest.json --rpc lasna=http://127.0.0.1:8545 --rpc unichain-sepolia=http://127.0.0.1:8546 --rpc base-sepolia=http://127.0.0.1:8547)
python -m lifeline.ops deploy "${DRY[@]}"
python -m lifeline.forkrelay "${DRY[@]}" --state .cache/dry/relay.json --watch &
python -m lifeline.ops loop-check --chain base-sepolia --canary-reserve 0.05 "${DRY[@]}"
python -m lifeline.ops status "${DRY[@]}"
```

`loop-check` deploys a test target (a canary pair), enrolls it, puts it into debt and waits for the rescue. The target is protected when the output ends like this:

```
  1 proxy debt event BlacklistContract(canary) on base-sepolia: tx 0x… block …
  2 Lasna reaction with Relayed: tx 0x… block …, requestId …
  3 vault Result on base-sepolia: tx 0x… block …, requestId …, outcome Rescued, paid …
  4 debt(canary) at the proxy of base-sepolia: 0 at block …
loop-check passed
```

`status` then shows the canary with `hub state Active, debt 0`.

- The forks run the real system contract and the real callback proxies. `forkrelay` emulates only the node. A dry run does not prove real delivery or latency.
- The RPC of a destination fork must serve the state of the fork block. At the two blocks above, the proxies authorize the delivery account that `forkrelay` uses. For another block, give `--operator <chain>=<address>`.
- For a new dry run, start new forks and delete `.cache/dry`.

### Testnet

Use a funded key. `deploy` prints its checks first, and it sends nothing if the balance of a chain is not above `principal + feeFloat` of the contracts of that chain in [`deployments/config.json`](deployments/config.json).

```
export LIFELINE_OWNER_KEY=<a funded key>
python -m lifeline.ops deploy --manifest deployments/my-manifest.json
python -m lifeline.ops loop-check --chain base-sepolia --manifest deployments/my-manifest.json
python -m lifeline.ops status --manifest deployments/my-manifest.json
```

`deployments/manifest.json` is the deployment of the builder, and only its owner key can use it. With that key, do not run `deploy`, and omit `--manifest`.

The end is the same as in the dry run. The canary stays enrolled: remove it with `remove`, or continue with your own contract in "Enroll".

## Operator guide

```
python -m lifeline.ops deploy     [--config <file>]
python -m lifeline.ops verify     [--sourcify <url>]
python -m lifeline.ops enroll     --chain <name> --target <address> --label <text>
                                  --low <amount> --high <amount> --max-debt <amount> --daily-cap <amount>
                                  --max-rescues <n> --window <seconds> [--guest --consent <reference>]
python -m lifeline.ops fleet      [--config <file>]
python -m lifeline.ops remove     --chain <name> --target <address>
python -m lifeline.ops recover    --chain <name>
python -m lifeline.ops status
python -m lifeline.ops loop-check --chain <name> [--timeout <seconds>] [--new] [--canary-reserve <amount>]
python -m lifeline.forkrelay      --rpc <chain>=<url> ... [--cron-tick] [--watch] [--state <file>]
```

- Each command accepts `--manifest <file>` (default `deployments/manifest.json`) and `--rpc <chain>=<url>`. Chain names: `lasna`, `unichain-sepolia`, `base-sepolia`.
- An amount is a decimal number in the native unit of the chain: lREACT on Lasna, ETH on a destination chain.
- The commands read each ABI from `out/`: run `forge build` first. `LIFELINE_OWNER_KEY` is the key of the owner of the hub and of each vault. `verify` and `status` need no key.
- Exit status 0 is success. Status 1 is a refusal or a failure, with the reason on standard error.
- `deploy` reads [`deployments/config.json`](deployments/config.json) and writes the manifest. Each later command reads the manifest and adds its records.
- A command writes each transaction to the manifest before it sends it. After a failure, run the same command again: it continues and sends no transaction a second time. Do not run two commands with one key at the same time.

### Configuration and manifest

| Entry of `deployments/config.json` | Meaning |
|---|---|
| `chains.<name>` | `chainId`, `ledger` (the system contract on Lasna, the callback proxy on a destination chain) and the `rpc` list. A destination chain also has `sweepBase` and `measuredSweepGas`: the base gas and the measured gas of one full sweep. |
| `hub`, `vaults.<name>` | `principal`, `feeFloat`, `selfReserveTarget`, `selfCareDailyCap`, `principalFloor`, in wei. The deployment sends `principal + feeFloat`. |
| `callbackSender` | `"hub"`: each vault accepts only the callbacks that the hub requested. |
| `cronTopic` | `Cron10000` or `Cron1000`. |
| `fleet` | The targets of the `fleet` command: `label`, `chain`, `address`, `policy` (amounts in wei). |

- Before its first transaction, `deploy` checks each chain ID and each ledger, the policies of `fleet`, and the owner balances. It also checks that the float of the hub covers one full trigger, and that `selfReserveTarget` of each vault covers 4 full sweeps. A second run deploys nothing that the manifest has.
- The manifest holds `contracts` (the hub and each vault with `address`, `deployBlock` and `deployTx`), `enrollments[]` (with `policy`, `guest`, `consent`, the transaction hashes and `removed`), `loopCheck` and `fixtures`. It holds no key.

### Enroll

Example values, not measurements:

```
# a reactive contract on Lasna
python -m lifeline.ops enroll --chain lasna --target <address> --label "my automation" \
  --low 0.05 --high 0.25 --max-debt 0.25 --daily-cap 0.5 --max-rescues 2 --window 3600

# a callback contract on a destination chain
python -m lifeline.ops enroll --chain unichain-sepolia --target <address> --label "my callback contract" \
  --low 0.0005 --high 0.002 --max-debt 0.001 --daily-cap 0.004 --max-rescues 2 --window 3600
```

- Lasna target. One transaction: `hub.enroll(5318007, target, policy)`. The hub reconciles the target at once.
- Destination target. Three steps in this order: `vault.setPolicy(target, policy)` on the destination chain, which reconciles the target at once; a read-back of `vault.policyOf(target)`; then `hub.enroll(chainId, target, policy)` on Lasna. If the read-back differs, the command stops and sends nothing to Lasna.
- The `setPolicy` line shows the result of the first reconcile. With `DebtAboveCap`, `DailyCap` or `Floor` the vault paid nothing and an old debt stays: clear it or give another policy.
- `fleet` enrolls each entry of `fleet` of the configuration through the same path.

### Policy

| Flag | Meaning |
|---|---|
| `--low` | The refill mark. The gauge refills a target with reserves below it. |
| `--high` | The reserves after a payment. A payment is `debt + (high - reserves)`. |
| `--max-debt` | A debt above this is not paid. There is never a partial payment. |
| `--daily-cap` | The most principal for this target in one calendar day (UTC). A payment that does not fit is not made. |
| `--max-rescues`, `--window` | A rescue is a payment that cleared a debt. After `--max-rescues` rescues (1 to 4) inside `--window` seconds, the next debt quarantines the target. A top-up is not a rescue. |

The contracts accept marks in native units only. The presets are advice, not measurements. One run is the fee of one reaction or one callback of your target: read it from the transactions of the target.

| Preset | For a target that runs | `low` | `high` |
|---|---|---|---|
| light | a few times each day | 2 runs | 10 runs |
| standard | each hour | 4 runs | 20 runs |
| heavy | each minute | 10 runs | 50 runs |

Set `maxDebt` above the fee of the largest run, and `dailyCap` to the refills that you accept for each day. The fleet policy of `FastfillReactive` has [`low` 0.05 lREACT and `high` 0.25 lREACT](JUDGE.md#fleet-report).

### Remove

```
python -m lifeline.ops remove --chain <name> --target <address>
```

The command sends `hub.disarm(chainId, target)` on Lasna first, then `vault.disarm(target)` on the destination chain. A Lasna target stops with `hub.disarm`. For a destination target, `vault.disarm` is the authoritative stop, and `hub.disarm` stops the relay cost.

### Recover a vault

A vault in debt runs no callback. The hub emits `VaultDown(chainId)`, sends no request to that vault, and keeps each later debt event of that chain as pending.

```
python -m lifeline.ops recover --chain <name>
```

1. It reads `debt(vault)` at the callback proxy.
2. If the debt is above 0, it sends `proxy.depositTo(vault)` with the value `debt + selfReserveTarget`.
3. While the hub reports the vault as down or reports pending targets, it checks that `debt(vault)` is 0 and sends `hub.vaultUp(chainId)`. Each call sends one catch-up sweep for at most 8 pending targets.

### Manual recovery transactions

| Case | Transaction |
|---|---|
| The hub is in debt, or `status` warns about its float | `depositTo(hub)` at the system contract on Lasna |
| A vault is in debt (`VaultDown`) | `proxy.depositTo(vault)` on the destination chain, then `hub.vaultUp(chainId)` on Lasna when `debt(vault)` is 0. `recover` does both. |
| A destination target is parked by the relay limit (`RelayLimited`) | `hub.rearm(chainId, target)` on Lasna |
| A target is quarantined | Lasna target: `hub.release(target)`, which reconciles it at once. Destination target: `vault.release(target)` on the destination chain; the next sweep reconciles it. |

Any account can send `depositTo`. The other functions accept only the owner.

Add `--rpc-url <RPC of the chain> --private-key $LIFELINE_OWNER_KEY` to each line:

```
cast send 0x8888888888888888888888888888888888888888 "depositTo(address)" <hub> --value <amount>ether   # on Lasna
cast send <proxy> "depositTo(address)" <vault> --value <amount>ether                                      # on the destination chain
cast send <hub> "vaultUp(uint256)" <chainId>                                                              # on Lasna
cast send <hub> "rearm(uint256,address)" <chainId> <target>                                               # on Lasna
cast send <hub> "release(address)" <target>                                                               # on Lasna
cast send <vault> "release(address)" <target>                                                             # on the destination chain
```

### Funds, status and board

| Item | How |
|---|---|
| Add principal | Send the native currency to the hub or to the vault. The owner takes it back with `withdraw(uint256)`. |
| Add to a fee float | `depositTo(contract)` at the ledger. A float cannot be withdrawn. |
| Status | `python -m lifeline.ops status` prints the funds of the hub and of each vault, and the state, the debt and the reserves of each target. It prints a `WARN` line for each item that needs the owner, and exits with status 1 if there is one. |
| Board | `python -m lifeline.board` builds [`board/index.html`](board/index.html) from the manifest. Open the file in a browser. The page is read-only and sends requests only to the RPC URLs of the manifest. Build it again after each command that changes the manifest. |

## Limits

- **Future delivery, not zero loss.** Lifeline restores future delivery. Work that arrives while a target is in debt is lost, and nothing replays it. The `Gap(target, deadFromBlock, aliveAtBlock)` event of the hub tells the owner what to retry.
- **Calendar-day caps.** A cap counts for one calendar day (UTC), so two caps can be paid across midnight.
- **`PaymentFailure`.** `PaymentFailure` does not always accompany debt. Lifeline listens to `BlacklistContract` only.
- **Sample values.** [0.0125 lREACT and 7e11 wei](evidence/network/table.md) are samples (the `sampleFeeWei` parameters of the network queries), not protocol constants.
- **A quarantined target in the sweep.** A target that is quarantined on a vault stays in the sweep of the hub until `hub.disarm`. The cost is bounded: one sweep for each vault for each tick while at least one target is enrolled on that chain, and a quarantined target adds one storage read and one `Result` event. `hub.disarm` ends the cost.
- **`vaultUp` after the vault debt is 0.** The hub cannot read the destination ledger. A sweep request to a vault in debt is not delivered, and its targets are not pending any more. `recover` makes the check before each call.
- **Junk callbacks.** If someone floods a Lifeline vault with junk callbacks, the rescue funds are not spent on them: the proxy can take only the small fee float, and at most `selfCareDailyCap` of principal per day refills that float. The vault can be pushed offline. One transaction, `proxy.depositTo(vault)`, brings it back, and the hub then repeats the rescues that were waiting. The largest loss for each day is the float plus `selfCareDailyCap`. Full prevention needs a platform change.
- **Low funds.** `HubLow` and `VaultLow` come only when the principal is below `principalFloor`. A hub at its floor keeps its cron subscription, and it goes into debt with no event when its float is empty. `status` warns before that state.

## Not built

- Public rescue pool
- Mutual-guard contract
- Adaptive cron speed
- Learned burn rate as a safety rule
- Quarantine from failure events
- Replay of work lost in the gap
- Movement of funds between vaults
- Automatic recovery of a vault that is in debt

## Demo versus production

| Demo | Production |
|---|---|
| Testnet, one owner, fixed marks, no upgradeability, two destination chains at most. | Audit; a rolling budget window; a price for destination gas; a platform answer to junk callbacks (admission control before the recipient is billed). |

## Guests

The owner enrolls a contract of another participant on request, with the written consent of the target owner: `enroll --guest --consent <reference>`. Without the reference the command refuses. A guest uses the same path as each other target. There is no public pool.

Status: [no guest enrolled](JUDGE.md#guests). The candidates are in the `guest_candidates` query of [`evidence/network/data.json`](evidence/network/data.json).

## Evidence and reproduction

Each number in this file is a link to [`evidence/network/table.md`](evidence/network/table.md), where its row names the query, the chain and the heights, or to a section of [`JUDGE.md`](JUDGE.md).

| Evidence | Command | Output |
|---|---|---|
| Network statistics | `python -m lifeline.stats [--rpc <chain>=<url> ...] [--heights <file>]` | `evidence/network/`: `data.json`, `table.md`, `heights.json` |
| Experiment | `python -m lifeline.experiment measure [--rpc <chain>=<url> ...] [--heights <file>]` | `evidence/experiment/`: `data.json`, `table.md`, `heights.json` |
| Judge document | `python -m lifeline.judge [--rpc <chain>=<url> ...] [--heights <file>]` | `JUDGE.md`, `evidence/judge/data.json`, `evidence/judge/heights.json` |

- The three commands need no key and send no transaction. `judge` needs `pip install -e ".[ops]"` and `forge build`.
- A first run pins `to` of each chain at the head and writes `heights.json`. A run with `--heights <file>` gives the same files again. No file has a wall-clock time or an RPC URL.
- `JUDGE.md` is generated. Do not edit it by hand. Run `measure` first, then `judge`.
- Network queries: `lasna_debt_events`, `destination_debt_events`, `stopped_contracts`, `cron_stopped_share`, `repeat_debtors`, `dropped_callbacks`, `fastfill_fill_times`, `cron_poller_ward_dead_time`, `fastfill_reactive_reserves`, `guest_candidates`. `cron_poller_ward_dead_time` is not configured (`subjects.cronPoller` of `config/network.json`), so this file has no number for it.
- Not recorded at the committed heights: the load of the three-arm experiment and the junk recovery. `JUDGE.md` says so in their sections.

The experiment drivers send transactions on the deployment of the manifest:

```
python -m lifeline.experiment fixtures   [--dest-chain <name>]
python -m lifeline.experiment load       --pokes <n> --interval <seconds>
python -m lifeline.experiment quarantine
python -m lifeline.experiment junk       --chain <name>
```

- `LIFELINE_FIXTURE_KEY` deploys and drives the fixtures. `fixtures` and `junk` also need `LIFELINE_OWNER_KEY`.
- `fixtures` deploys three identical canary automations (arms A, B, C), the cron poller of arm B, a looping canary, a waiting canary and a junk requester. It enrolls arm C and the looping canary with `enroll`.
- `junk` accepts only a vault of the manifest whose owner is the address of `LIFELINE_OWNER_KEY`. It floods that vault, waits for `VaultDown`, then runs `recover`.
- Order: `fixtures`, `load`, then `quarantine` and `junk`, then `measure`, then `judge`. The drivers write `evidence/experiment/scenarios.json`.

## Development

| Item | Value |
|---|---|
| Foundry | 1.10.0 |
| Solidity compiler | 0.8.30, EVM version `cancun`, optimizer with 200 runs |
| `lib/reactive-lib-omni` | commit `3ade0dc` |
| `lib/forge-std` | v1.17.0 |

```
export LASNA_RPC_URL=<url> BASE_SEPOLIA_RPC_URL=<url>
forge test

pip install -e ".[test]"
python -m pytest
```

- `forge test` runs the contracts against the real ledgers on forks of Lasna and Base Sepolia. Each node must serve the state of the fork block of the test. `forge test --match-contract LifelineTypesTest` needs no fork.
- `python -m pytest` runs the real commands against a local JSON-RPC server.
- The fork modules (`test_ops_forks.py`, `test_experiment_forks.py`, `test_judge_forks.py`, `test_board_forks.py`) repeat the dry run with the real commands. They also need `anvil`, `UNICHAIN_SEPOLIA_RPC_URL`, `BASE_SEPOLIA_FORK_BLOCK=62527000` and `UNICHAIN_SEPOLIA_FORK_BLOCK=68574000`. Without them the modules are skipped. The board modules need Chrome or Chromium.

## License

Apache License 2.0. See [`LICENSE`](LICENSE).
