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

## Operations

The operator commands deploy the hub and the vaults, verify them on Sourcify, and onboard, remove and recover targets. One configuration file is the input. One manifest file is the output, and each later command reads it.

```
python -m lifeline.ops deploy     [--config <file>] [--manifest <file>]
python -m lifeline.ops verify     [--manifest <file>]
python -m lifeline.ops enroll     --chain <name> --target <address> --label <text>
                                  --low <amount> --high <amount> --max-debt <amount> --daily-cap <amount>
                                  --max-rescues <n> --window <seconds> [--guest --consent <reference>]
python -m lifeline.ops fleet      [--config <file>]
python -m lifeline.ops remove     --chain <name> --target <address>
python -m lifeline.ops recover    --chain <name>
python -m lifeline.ops status
python -m lifeline.ops loop-check --chain <name> [--timeout <seconds>]
python -m lifeline.forkrelay      --rpc <chain>=<url> ... [--manifest <file>] [--cron-tick]
```

### Setup

```
pip install -e ".[ops]"
git submodule update --init
forge build
export LIFELINE_OWNER_KEY=<private key of the owner account>
```

- The commands read each ABI and each bytecode from the Foundry build output in `out/`. Run `forge build` before the first command and after each change of a contract.
- `LIFELINE_OWNER_KEY` is the private key of the owner: `0x` and 64 hexadecimal characters. The account deploys the contracts and becomes the `owner` of the hub and of each vault. No command prints or stores the key. `verify` does not need it.
- The extra `ops` installs `eth-account`, which signs the transactions and encodes ABI data.

### Arguments of each command

| Argument | Meaning |
|---|---|
| `--manifest <file>` | The manifest. Default: `deployments/manifest.json`. Each command accepts it. |
| `--rpc <chain>=<url>` | RPC URL for one chain. Repeat the flag for more chains. More than one URL for a chain makes an ordered list: a request that gets no answer goes to the next URL. The URLs replace the `rpc` list of the configuration (`deploy`) or of the manifest (all other commands) for that chain. Each command accepts it. |
| `--config <file>` | The configuration. Default: `deployments/config.json`. For `deploy` and `fleet`. |

Chain names: `lasna`, `unichain-sepolia`, `base-sepolia`. An amount on the command line is a decimal number in the native unit of the chain (lREACT on Lasna, ETH on a destination chain). The command converts it to wei and prints both values.

Each command exits with status 0 after success and with status 1 after a refusal or a failure; the reason is on standard error. Two exceptions: an argument that the command line does not accept gives status 2, and `verify` prints the result of each contract on standard output and exits with status 1 if one contract is not verified.

Each URL of a chain must answer `eth_chainId` with the chain ID of the configuration or of the manifest. A command sends no other request to a URL that answers for another chain, and it signs each transaction with the chain ID of the file.

| Command | Arguments | What it does |
|---|---|---|
| `deploy` | | Runs the checks, then deploys the hub on Lasna and one vault for each entry of `vaults`, registers each vault at the hub (`hub.setVault`), and subscribes the cron tick (`hub.setCronTopic` if the topic is not `Cron10000`, then `hub.resumeCron`). It writes the manifest after each step. A second run does not deploy a contract that the manifest holds, and sends no transaction for a step that is done. |
| `verify` | `--sourcify <url>` (default `https://sourcify.dev/server`) | Verifies the hub and each vault on Sourcify with the metadata and the sources of the build, and prints the constructor arguments and the result for each contract. It reports a chain that Sourcify does not support as `BLOCKED`. |
| `enroll` | `--chain`, `--target`, `--label`; the policy: `--low`, `--high`, `--max-debt`, `--daily-cap` (amounts), `--max-rescues` (1 to 4), `--window` (seconds); `--guest --consent <reference>` | Onboards one target. See "Onboarding order". `--guest` marks a contract of another participant; it needs `--consent`, a reference to the written consent of the target owner. Without the reference the command refuses before any request. A guest uses the same path as each other target. |
| `fleet` | | Onboards each entry of `fleet` of the configuration through the path of `enroll`, and prints the Lasna block of each `hub.enroll`. An entry that the hub has already is skipped. The first run starts the 48-hour window. |
| `remove` | `--chain`, `--target` | Removes one target. See "Removal order". |
| `recover` | `--chain` | Restores a vault that is in debt. See "Recovery". |
| `status` | | Reads the funds and the state of the hub, of each vault and of each enrolled target, and prints a `WARN` line for each item that needs an action of the owner. It needs no key and sends nothing. Exit status 1 if there is a warning. See "Funds and status". |
| `loop-check` | `--chain`, `--timeout <seconds>` (default 600), `--new`, `--canary-reserve <amount>`, `--high <amount>`, `--max-debt <amount>` | Shows the destination loop with a canary pair. See "Loop check". |

### `deployments/config.json`

The one input of `deploy` and `fleet`. Amounts are decimal strings in wei.

| Entry | Meaning |
|---|---|
| `chains.<name>.chainId` | Chain ID. `deploy` compares it with `eth_chainId` of the RPC. |
| `chains.<name>.ledger` | The system contract on Lasna, the callback proxy on a destination chain. |
| `chains.<name>.rpc` | RPC URLs, in order. |
| `chains.<name>.sweepBase` | Destination chains only. The base gas of a sweep callback: `90,000 + _extraGas + _maxChargeGas` of that proxy. `deploy` gives it to `hub.setVault`. |
| `chains.<name>.measuredSweepGas` | Destination chains only. The gas of one full sweep of 8 targets (`SWEEP_MAX`): the line `SWEEP_GAS n=8 base=<sweepBase> used=<gas>` of `forge test --match-test test_sweepFitsItsGasLimit_eachCountOnEachConfiguration -vv`. |
| `chains.<name>.extraGas`, `gasPriceCoeffPer1000`, `ledgerNote` | Optional. Used only if the ledger has no getter `_extraGas()` or `_gasPriceCoeffPer1000()`. `ledgerNote` says how the value was read; without the note the value is not used. |
| `hub`, `vaults.<name>` | `principal`, `feeFloat`, `selfReserveTarget`, `selfCareDailyCap`, `principalFloor`. The deployment sends `principal + feeFloat`; `feeFloat` goes to the reserves of the contract at its ledger. |
| `callbackSender` | `"hub"` (the address of the deployed hub) or an address. It is the immutable `callbackSender` of each vault: the address that the vault expects in the first argument of a callback. The node writes the address of the contract that made the request into that argument. So a vault accepts the callbacks of the hub only if the value is the hub address. Another address does not change what the node writes. |
| `cronTopic` | `Cron10000` or `Cron1000`. |
| `fleet` | The targets of `fleet`: `label`, `chain`, `address`, `policy` (`low`, `high`, `maxDebt`, `dailyCap` in wei, `maxRescues`, `window` in seconds). |

Values of the file and their sources:

- Ledgers: Lasna `0x8888888888888888888888888888888888888888`, Base Sepolia `0x476F44b11F5C942c58f4c8E3d0300E5710743585`, Unichain Sepolia `0x775c9B990ADe401060B78D0e98684eEf8d2466C1` (chain ID 1301). Source: the Reactive Network documentation, "Origins & Destinations"; they are the ledgers of `config/network.json`.
- `sweepBase` 220,000: both proxies answer `_extraGas()` 80,000 and `_maxChargeGas()` 50,000.
- `measuredSweepGas` 619,221: the `SWEEP_GAS n=8 base=220000` line of the contract test, on the Base Sepolia fork. It is the gas of the delivery transaction, which is more than the gas that the proxy charges. The contract test does not run on a Unichain Sepolia fork, so the fork dry run measures both chains: a full sweep of 8 rescued targets through the real proxy was charged 524,784 gas on a Unichain Sepolia fork and 524,774 gas on a Base Sepolia fork, each plus `_extraGas`. The fee that `deploy` computes from 619,221 is about 16 % above the fee that the proxy charged. The test `test_measured_sweep_gas_of_the_configuration_holds_on_each_proxy` repeats the measurement.
- The amounts of `hub` and `vaults` and the policies of `fleet` are operator inputs. The source gives only the marks of FastfillReactive: `low` 0.05 and `high` 0.25 lREACT.
- The fleet addresses are the Fastfill contracts of the builder.

### `deployments/manifest.json`

The output of `deploy`. The commands after `deploy` read it and add their records.

| Entry | Meaning |
|---|---|
| `version` | 1. |
| `cronTopic` | The cron topic of the hub. |
| `chains.<name>` | `chainId`, `ledger` and the `rpc` list of the configuration. |
| `contracts.hub` | `chain`, `chainId`, `address`, `deployBlock`, `deployTx`, and `cron` (`topic`, `setCronTopicTx`, `resumeCronTx`). |
| `contracts.vaults.<name>` | `chainId`, `address`, `deployBlock`, `deployTx`, `setVaultTx`, and after a recovery `recoveries[]`: for each run of `recover` that sent a transaction, `deposit`, `depositBlock` and `vaultUp[]` (`tx`, `block`, `requestId`, `sent`, `remaining`). |
| `enrollments[]` | One record for each enrollment: `label`, `chain`, `chainId`, `target`, `policy`, `guest`, `consent`, `txs` (`setPolicy`, `readBackBlock`, `enroll`, `enrollBlock`) and `removed`. For a Lasna target `setPolicy` and `readBackBlock` are null. A record with `enrollBlock` null is an enrollment that did not finish. `removed` is null, or `{ "hubDisarm", "vaultDisarm" }` after `remove`; a hash is null if that step needed no transaction. |
| `loopCheck` | The newest loop check: `chain`, `chainId`, `status` (`setup`, `waiting`, `timeout`, `failed` or `passed`), `missing`, the two canaries (`canaryCallback`, `canaryReactive`, each with `chain`, `chainId`, `address`, `deployBlock`, `deployTx`), `reserve`, `fundTx`, `policy`, `pokeTx`, `pokeBlock`, and `steps` with the hash and the block of each step. |
| `fixtures` | An empty object. The experiment fixtures fill it. |

A command changes only its own entries and keeps each other entry of the file. The file holds no key. An `--rpc` URL is not written to it.

### A run that stops

A command writes the manifest before each transaction goes to the node, with the nonce, the hash and the fee fields of that transaction (a journal). The hash moves to its place in the file when the transaction has a receipt. So the same command, run again, continues after a failure or a lost answer of a node. The rules:

- New transaction. A command sends a transaction with a new nonce only when each earlier transaction of the owner account on that chain has a receipt. It waits for that state for a limited time, then it stops. So two commands with the same key do not run well at the same time on one chain.
- Journal, and the chain has the transaction, in a block or not: the command waits for it and sends nothing.
- Journal, and the node does not know the transaction, and the nonce is free: the command sends the transaction again with the same nonce and with fee fields that are 12.5 % higher, so a node accepts it as a replacement. One of the two at most can get into a block.
- Journal, and the node does not know the transaction, and the nonce was used: the result is not known. The command stops and sends nothing. It does not use a new nonce. Run the command again later. If the result is the same, find the transaction of that nonce in a block explorer and correct the manifest.
- Journal with other inputs than the inputs of this run (for example another amount in the configuration): the command stops. Give the inputs of the earlier run again.
- A step that the state of the chain shows as done needs no transaction. The command first closes the journal of that step: it waits for a recorded transaction that the chain has and writes its hash to its place.
- `remove` and `recover` wait until each earlier transaction of the owner has a receipt before they read the state that decides their steps.

Where the journal is:

- `deploy` and `loop-check`: `pendingDeploy` of the entry of a contract. No contract is deployed two times. A contract entry with `address` null is a deployment that did not finish. The float checks of `deploy` apply again to a deployment whose recorded transaction the node does not know.
- `enroll`, `fleet` and each other transaction: `pending` next to the hash. `enroll` writes the enrollment record before `setPolicy`. The next run with the same policy sends only the steps that are missing.
- `remove` writes its hashes to `removing` of the record, and moves them to `removed` at the end. It does not start while a transaction of the enrollment has no receipt: finish the enrollment with `enroll` first.
- `recover` writes the deposit to the newest entry of `recoveries` of the vault. A deposit that waits for its receipt is not sent a second time. If the vault has no debt and the node does not know the recorded deposit, the journal stays while the nonce of that deposit is free, so a later deposit uses the same nonce. `hub.vaultUp` has no journal: the command reads `vaultInfo` and the vault debt before each call, and a call with no pending target changes nothing.

If the hub has a target and the manifest has no record of that enrollment, `enroll` and `fleet` refuse the target. `remove` removes it; that removal has no record in the manifest.

The timeouts come from the environment: `LIFELINE_RECEIPT_TIMEOUT` (seconds to wait for a receipt, default 180) and `LIFELINE_POLL_SECONDS` (default 2).

### Checks before deployment

`deploy` sends no transaction and exits with status 1 if one of these checks fails. It prints each check and the numbers that it used.

- Each `chainId` equals `eth_chainId` of the RPC, and each ledger has code.
- For each vault: `selfReserveTarget >= 4 x` the fee of one full sweep. The fee is `(_extraGas + measuredSweepGas) x gas price x _gasPriceCoeffPer1000 / 1000`. `_extraGas` and the coefficient come from the deployed proxy.
- For the hub: `feeFloat` and `selfReserveTarget` each cover one full trigger of 900,000 gas: `900,000 x gas price x _gasPriceCoeffPer1000 / 1000`, about 0.09 lREACT at 100 gwei.
- `measuredSweepGas` and `sweepBase` are above 0, and each policy of `fleet` is valid: `high > 0`, `low <= high`, `maxDebt > 0`, `dailyCap > 0`, `1 <= maxRescues <= 4`, `window > 0`.
- The owner balance on each chain is above the value of the deployments on that chain.

The gas price is the largest of `eth_gasPrice` and the base fees of the newest blocks. The float checks apply to a contract that is not deployed yet.

### Onboarding order

For a target on a destination chain, `enroll` does three steps and prints them in this order:

1. `vault.setPolicy(target, policy)` on the destination chain. The output has the transaction hash.
2. Read-back: `vault.policyOf(target)` over RPC. The output has the block of the read. If one field differs from the input, the command stops with status 1 and sends nothing to Lasna.
3. `hub.enroll(chainId, target, policy)` on Lasna, with the destination chain ID and the same policy value. The output has the transaction hash.

The vault policy comes first for two reasons. `vault.setPolicy` reconciles the target at once, inside the limits of the policy. A subscription of the hub does not replay an old debt event, and `hub.enroll` sends no callback, so this reconcile is the only one for a debt that the target has already. And when the hub starts to relay debt events of the target, the vault has the policy already, so no relay ends with `NoPolicy`.

The `setPolicy` line of the output has the outcome of that reconcile. `Rescued` and `ToppedUp` are payments. With `DebtAboveCap`, `DailyCap` or `Floor` the vault paid nothing and an old debt stays: the enrollment continues, and the operator must clear that debt or give another policy.

For a target on Lasna, `enroll` sends one transaction: `hub.enroll(5318007, target, policy)`. The hub holds the policy and reconciles the target at once.

Before the first transaction, `enroll` checks the policy rule, the consent of a guest, the code of the target, the owner of the hub and of the vault, and that the hub does not have the target already.

### Removal order

`remove` uses the reverse order:

1. `hub.disarm(chainId, target)` on Lasna. The command confirms that the receipt holds the `Unsubscribe` log of the debt tuple of the target, or that the tuple was unsubscribed before, and that `hub.targetInfo` reports the state `Disarmed`.
2. For a destination target: `vault.disarm(target)` on the destination chain.

The hub stops to listen first, so it sends no new request for the target to a vault that has no policy for it. A request that the hub sent before `hub.disarm` can still arrive at the vault after `vault.disarm`; its result is `NoPolicy`. The command writes the two hashes to `removed` of the enrollment record. A second run sends nothing.

For a target with an enrollment that did not finish (the vault has the policy and the hub does not have the target), `remove` sends `vault.disarm` only and closes the record.

### Recovery

A vault in debt runs no callback. The hub then marks the vault as down and keeps the debt events of its targets as pending. `recover` restores the vault:

1. Read `debt(vault)` at the callback proxy.
2. If the debt is above 0: one transaction on the destination chain, `proxy.depositTo{value: debt + float}(vault)`. The float is `selfReserveTarget()` of the vault.
3. Read `debt(vault)` again.
4. While `hub.vaultInfo` reports the vault as down or reports pending targets: read `debt(vault)`, then `hub.vaultUp(chainId)` on Lasna. Each call sends one catch-up sweep for at most 8 pending targets. The command prints each hash with `sent` and `remaining`, and stops when 0 remain.

Rule: `hub.vaultUp` runs only when the vault debt is 0. The hub cannot read the destination ledger, so the command makes the check before each call: if the debt is above 0, or if the deposit cannot be sent, the command exits with status 1 and sends nothing to Lasna. A sweep request to a vault in debt is not delivered, and its targets are not pending any more.

If the vault has no debt, is up and has no pending target, `recover` sends nothing. A deposit goes to the reserves of the vault, and reserves cannot be withdrawn: see "A run that stops" for the rule that prevents a second deposit.

### Funds and status

Each contract has two kinds of funds. Principal is the balance of the contract: it pays the targets, and the owner can withdraw it. The fee float is the reserves of the contract at its ledger: the ledger takes each fee from it, and nobody can withdraw it. Self-care moves principal into the float up to `selfReserveTarget`, at most `selfCareDailyCap` for each day, and never below `principalFloor`.

- Add principal: send the native currency to the address of the hub or of the vault (a plain transfer).
- Add to a float: `depositTo(contract)` at the ledger, with a value. `recover` does this for a vault in debt.
- `python -m lifeline.ops status` shows both for each contract, the number of full triggers that the funds of the hub cover, the state of each vault at the hub, and the debt and the reserves of each enrolled target.

Limits of the deployed contracts that the owner must know:

- The hub emits `HubLow` and stops its cron subscription only when its balance is below `principalFloor`. Self-care stops at the floor and does not take the balance below it. So a hub at its floor keeps its cron subscription, pays each tick from the float, and goes into debt when the float is empty, with no event. A hub in debt gets no reaction. `status` warns before that state: when the principal above the floor does not cover one full trigger, and when the float does not cover one.
- The same holds for a vault and `VaultLow`. The hub reports a vault in debt with `VaultDown`.
- One gauge tick of the live hub cost 0.0259 lREACT (tick 9,040,000: one Lasna target, two sweep requests, with self-care). With `Cron10000` and blocks of about 1 second that is about 0.22 lREACT for each day, with no rescue.

### Loop check

`loop-check` deploys a `CanaryCallback` on the destination chain and a `CanaryReactive` on Lasna that calls it, deposits reserves for the Lasna canary, and enrolls the callback canary through the path of `enroll` with `low` 0. So `setPolicy` pays nothing, and the first callback puts the canary into debt. Then the command calls `poke()` and waits for four steps. It prints the hash and the block of each step and writes them to `loopCheck`:

1. `BlacklistContract(canary)` of the callback proxy: the debt event.
2. `Relayed(requestId, chainId, canary)` of the hub: the reaction on Lasna.
3. `Result(requestId, canary, Rescued, paid)` of the vault, with the same `requestId`.
4. `debt(canary) == 0` at the callback proxy.

At the timeout the command exits with status 1 and names the step that did not occur. The next run continues a check that did not pass: it sends only what is missing and waits again. After a check that passed, the next run starts a new check with two new canaries. `--new` starts a new check in each case. The callback canary stays enrolled after the check; `remove` removes it. The defaults of the canary policy are `high = selfReserveTarget / 4` and `maxDebt = selfReserveTarget` of the vault. The default reserves of the Lasna canary are the fee of 400,000 gas at the current gas price.

### Fork dry run

Run the commands on local forks before the live run. The system contract and the callback proxies of a fork are the real deployed code. `lifeline.forkrelay` emulates only the node: it sends each log to its subscribers through the real `trigger` from the injected address of the node, with 900,000 gas, and each callback request to the real `deliverCallback` from the authorized delivery account of the proxy. The emulation does not prove real delivery or latency.

```
anvil --fork-url <Lasna RPC>            --port 8545 --disable-min-priority-fee
anvil --fork-url <Unichain Sepolia RPC> --port 8546 --disable-min-priority-fee
anvil --fork-url <Base Sepolia RPC>     --port 8547 --disable-min-priority-fee
cast rpc anvil_mine 0x1 --rpc-url http://127.0.0.1:8545

export LIFELINE_OWNER_KEY=<a throwaway key>
cast rpc anvil_setBalance <its address> 0x56BC75E2D63100000 --rpc-url http://127.0.0.1:8545   # and ports 8546, 8547

FORKS="--rpc lasna=http://127.0.0.1:8545 --rpc unichain-sepolia=http://127.0.0.1:8546 --rpc base-sepolia=http://127.0.0.1:8547"
python -m lifeline.ops deploy --config <a copy of the configuration with amounts> --manifest /var/tmp/dry/manifest.json $FORKS
python -m lifeline.forkrelay --manifest /var/tmp/dry/manifest.json $FORKS --watch &
python -m lifeline.ops loop-check --chain base-sepolia --canary-reserve 0.05 --manifest /var/tmp/dry/manifest.json $FORKS
```

- Use a throwaway key and a scratch manifest. A command refuses to write `deployments/manifest.json` when an RPC is a local development node.
- Give `--rpc` for each chain to each command. The manifest holds the RPC list of the configuration, not the fork URLs.
- Mine one block on the Lasna fork first: anvil refuses a call at the fork block of Lasna.
- `forkrelay` takes one URL for each chain and refuses an RPC that is not a local development node. One run relays until no new log remains, and prints the number of triggers and deliveries. `--watch` keeps it running, for `loop-check`. `--cron-tick` first sends one cron log record of the newest block to each cron subscriber. `--state <file>` holds the blocks that earlier runs relayed (default `.cache/forkrelay/state.json`): use a new file for new forks. The command refuses a state file that it cannot read. `--operator <chain>=<address>` and `--gas-price <chain>=<wei>` replace the delivery account and the gas price of the node transactions.
- Order of two chains. A Lasna log matches the subscriptions that were active at its place in the Lasna log order. A destination log matches the subscriptions that are active when the relay reads it; on the network, a subscription matches a log from the time of the log. The two rules agree if the relay reads between a destination log and a change of a subscription that matches it. So keep the relay running with `--watch`, or run it one time before an enrollment or a removal of a target that has a new debt. If both came between two reads, the relay prints `note: order not known` with the choice that it made, and a last line `order not known: <n>`.
- On an anvil fork the base fee goes down with each empty block. The node transactions of `forkrelay` use one gas price for each chain: the largest of `eth_gasPrice` at the start of the command, the base fee of the fork block and the base fee of the block before it. The float checks of `deploy` and the default canary reserves of `loop-check` use the gas price of the fork, which is lower than the live price: give `--canary-reserve`.
- `verify` needs the live chains.
- The measured gas of a full sweep on each proxy comes from this dry run: see `measuredSweepGas` above.
- A fork does not show each rule of a live node. Lasna answers 0 for `eth_maxPriorityFeePerGas` and refuses a transaction with a tip below 1 wei; anvil accepts a tip of 0. The commands send a tip of 1 wei or more.

The fork dry run is also a test module. It starts the three forks itself and runs the real commands:

```
pip install -e ".[test]"
forge build
export LASNA_RPC_URL=<url> UNICHAIN_SEPOLIA_RPC_URL=<url> BASE_SEPOLIA_RPC_URL=<url>
python -m pytest lifeline/tests/test_ops_forks.py
```

Without `anvil`, the build output or one of the three variables, the module is skipped.

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
| `BASE_SEPOLIA_RPC_URL` | RPC URL of Base Sepolia (chain ID 84532). The node must serve the state of the fork block. |
| Base Sepolia fork block | 61,846,945 |

```
git submodule update --init
export LASNA_RPC_URL=<url>
export BASE_SEPOLIA_RPC_URL=<url>
forge test
```

The fork tests in `test/LifelineHubLasna.t.sol` run against the system contract code of the fork block. They fail if `LASNA_RPC_URL` is not set. `forge test --match-contract LifelineTypesTest` runs the tests that need no fork.

## License

Apache License 2.0. See `LICENSE`.
