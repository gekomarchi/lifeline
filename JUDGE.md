# Lifeline: evidence for the judges

This file is generated: `python -m lifeline.judge` writes it from chain data. Do not edit it by hand. The data file of the same run, `data.json`, has each figure of this file. Each amount is in wei of the chain that its row names. A block is a block of the chain that its row or its column names.

## Claim limit

Lifeline restores future delivery, not zero loss. Work that arrives while a target is in debt is lost, and nothing replays it. Each gap of this file gives the blocks in which a target was dead, so that its owner knows what to retry.

## Heights

| Chain | Chain ID | From | To |
|---|---|---|---|
| lasna | 5318007 | 9039165 | 9071008 |
| base-sepolia | 84532 | 62488753 | 62657559 |
| unichain-sepolia | 1301 | 68567187 | 68600951 |

Each log query of this run is inside `from..to` of its chain. `from` is the deployment block of the Lifeline contract of that chain: the hub on lasna, the vault on a destination chain. Each state value is read at `to`.

## Destination rescues

Each row is one relay request of the hub: a `Relayed` log on lasna. The join finds the `Result` log of the vault of the destination chain with the same request ID and the same target. The debt event is the last `BlacklistContract` log of the callback proxy for the target before the result. For a rescue, the `WhitelistContract` log of the proxy is in the transaction of the result.

### Joined

| Request ID | Chain | Target | Debt event | `Relayed`, lasna | `Result` | Outcome | Paid | `WhitelistContract` |
|---|---|---|---|---|---|---|---|---|
| 340282366920938463463374607431768211457 | base-sepolia | `0xa462B906917C0372872AD35C0321eB5Daa60692E` | 62489494<br>`0x9bb5e827d134906e1ec5f184cd2fecd4a0370974fb8711d2baaabedcb0c15727` | 9039315<br>`0x33cc6420f62ea2653c5fe28ae6b41d572a06405d10fdbc43fa2c9244fc35061e` | 62489499<br>`0xd19743a00790ed97d97e0a4d830759dbf1293d2257fd61eb0f56f47b25091adf` | Rescued | 50559145400000 | 62489499<br>`0xd19743a00790ed97d97e0a4d830759dbf1293d2257fd61eb0f56f47b25091adf` |

### Open

A relay request with no `Result` log in the heights of its destination chain.

None.

## Lasna rescues and gaps

Each row is one `Rescued` log of the hub with the `Gap` log of the same transaction. A dead-from block of zero means that no debt log started the rescue.

| Target | `Rescued` | Debt | Paid | Dead from block | Alive at block |
|---|---|---|---|---|---|
| `0xF6152129A1Cbef7c04F7c92D47B6AA0Ac80FA5F9` | 9051444<br>`0xeb5f7b5f092550dc46bb9eee726ac6d0c9bcf0999510209cd32dafb48cb6b955` | 11980000000000000 | 12980000000000000 | 9051443 | 9051444 |
| `0xF6152129A1Cbef7c04F7c92D47B6AA0Ac80FA5F9` | 9051466<br>`0x0513ec1482d1df1aa9fa7a6376efd7656a3c320cfd7245a62cb37aed1a0acf2d` | 10980000000000000 | 11980000000000000 | 9051465 | 9051466 |

## Fleet report

The fleet is each enrollment of the manifest that is not a guest and not a fixture of the experiment. Each figure counts from the enrollment block of the target on its own chain to `to` of that chain. The enrollment block is the block of `hub.enroll` for a lasna target, and the block of `vault.setPolicy` for a target of a destination chain.

### FastfillReactive

| Figure | Value | Chain, block or hash |
|---|---|---|
| Chain | lasna | chain ID 5318007 |
| Target | `0x05a566959544A55c16012aeeF0a09067da1f204f` |  |
| Enrollment block | 9039243 | `0x77b119294494b9d86b3b24f19e00787054aa13344203aeee5835ab6524a21cf8` |
| Removed | no |  |
| Fills | 0 | unichain-sepolia, from block 68567263, logs of `0x6999526e507Cc3b03b180BbE05E1Ff938259A874` |
| Rescues | 0 | from block 9039243 |
| Guardian top-ups | 0 | from block 9039243 |
| Principal paid | 0 | from block 9039243 |
| Low mark of the policy | 50000000000000000 | the enrollment transaction |
| High mark of the policy | 250000000000000000 | the enrollment transaction |
| Reserves kept | 60373758200000000000 | at block 9071008 |

Gaps: none.

### FastfillExecutor

| Figure | Value | Chain, block or hash |
|---|---|---|
| Chain | unichain-sepolia | chain ID 1301 |
| Target | `0x916D4d5f9E363CbdFA8C34fB41185a75A6a2E705` |  |
| Enrollment block | 68567271 | `0xeddc1f5a5d9f9ace1caa51759c57ae0d13f9675e474b5a9057306b191aa7a3c0` |
| Removed | no |  |
| Fills | 0 | unichain-sepolia, from block 68567271, logs of `0x6999526e507Cc3b03b180BbE05E1Ff938259A874` |
| Rescues | 0 | from block 68567271 |
| Guardian top-ups | 0 | from block 68567271 |
| Principal paid | 0 | from block 68567271 |
| Low mark of the policy | 500000000000000 | the enrollment transaction |
| High mark of the policy | 2000000000000000 | the enrollment transaction |
| Reserves kept | 1985547990287874 | at block 68600951 |

Gaps: none.

### loop-check canary (base-sepolia)

| Figure | Value | Chain, block or hash |
|---|---|---|
| Chain | base-sepolia | chain ID 84532 |
| Target | `0xa462B906917C0372872AD35C0321eB5Daa60692E` |  |
| Enrollment block | 62489400 | `0xd4d84e16adb7312d39b564934fa9f9673c2bc825ab5c0973f6adfc6889b5ce71` |
| Removed | yes | hubDisarm `0x8452aa6825b4eabe87a6c5b5a4a74b2b364e36745afdea8934b7b1ad3537e307`<br>vaultDisarm `0xc3313c96bea2d9c5c3b512309bb857337fd415157953fda47d79a4af79e40df1` |
| Fills | 0 | unichain-sepolia, from block 68567320, logs of `0x6999526e507Cc3b03b180BbE05E1Ff938259A874` |
| Rescues | 1 | from block 62489400 |
| Guardian top-ups | 0 | from block 62489400 |
| Principal paid | 50559145400000 | from block 62489400 |
| Low mark of the policy | 0 | the enrollment transaction |
| High mark of the policy | 50000000000000 | the enrollment transaction |
| Reserves kept | 50000000000000 | at block 62657559 |

| Gap: dead from block | Alive at block | Blocks | Transaction that ended the gap |
|---|---|---|---|
| 62489494 | 62489499 | 5 | `0xd19743a00790ed97d97e0a4d830759dbf1293d2257fd61eb0f56f47b25091adf` |

The specification does not define a fill. This report reads it as a fill event of the fleet itself, as `subjects.fastfill.fill` of `config/network.json` defines it: the count is the number of these logs from the first block of the fill chain at or after the enrollment, by the block timestamps, to `to`. A guardian top-up is a separate figure. For a lasna target a gap is a `Gap` log of the hub. For a target of a destination chain a gap is a debt interval at the callback proxy: from a `BlacklistContract` log to the next `WhitelistContract` log, or to `to`. The block count of a gap starts at the enrollment block if the debt started before it.

## Guardian cost

Each figure counts from the deployment block of the contract to `to`.

| Guardian | Chain | Address | Deployment block | Principal paid | `SelfCare` added | Fee float consumed | Balance | Reserves | Debt | State block |
|---|---|---|---|---|---|---|---|---|---|---|
| hub | lasna | `0xbba61E507e7CC334e57A62c39A776272DA6b07Dd` | 9039165 | 24960000000000000 | 355028400000000000 | 179581000000000000 | 4420011600000000000 | 175447400000000000 | 0 | 9071008 |
| vault of base-sepolia | base-sepolia | `0x1aA62Eaf6746Ffc7a19330dD255211680B4952B2` | 62488753 | 50559145400000 | 201909492200000 | 2778540600000 | 1947531362400000 | 199130951600000 | 0 | 62657559 |
| vault of unichain-sepolia | unichain-sepolia | `0x89dbc693c78C8844b5A3294DE8f7c0f0adf1DC26` | 68567187 | 0 | 200185524350000 | 402786450000 | 4999814475650000 | 199782737900000 | 0 | 68600951 |

The principal paid is the sum of the payment events: `Rescued` and `ToppedUp` of the hub, `Result` of a vault. The fee float consumed is the sum of `SelfCare.added`, minus the reserves at `to`, plus the debt at `to`. A deposit to the float by another account is not visible in these events: it makes the figure too low by its amount.

## Three-arm table

The text between the two markers is `evidence/experiment/table.md`, unchanged. Its heights are the heights of the experiment.

<!-- begin: evidence/experiment/table.md, unchanged -->
# Three-arm experiment

| Chain | Chain ID | From | To |
|---|---|---|---|
| lasna | 5318007 | 9051258 | 9051573 |
| base-sepolia | 84532 | 62552591 | 62554317 |

Amounts are in wei of the chain that the row names.

| Metric | A: no guardian | B: cron poller | C: Lifeline |
|---|---|---|---|
| Pokes, ticks, pongs | 0, 0, 0 | 0, 0, 0 | 0, 0, 0 |
| Blocks of downtime, lasna | 0 | 0 | 0 |
| Blocks of downtime, base-sepolia | 0 | 0 | 0 |
| Events missed | 0 | 0 | 0 |
| Callbacks dropped | 0 | 0 | 0 |
| Guardian cost: principal, lasna | 0 | 0 | 0 |
| Guardian cost: principal, base-sepolia | 0 | 0 | 0 |
| Guardian cost: fees (lower bound), lasna | 0 | 0 | 67036500000000000 |
| Guardian cost: fees (lower bound), base-sepolia | 0 | 0 | 0 |
| Guardian cost: fee figures that are not exact | none | none | none |
| Guardian cost: fee scope | no guardian | the poller serves the reactive canary of this arm only; the figure is from its deployment to `to` | the hub and the vault serve each enrolled target of the deployment and each scenario; the figures are their whole fee consumption in `from..to`, and no share is estimated |
| Blocks of guardian downtime | none | lasna 0 | lasna 0<br>base-sepolia 0 |
| Capital locked | reactive.reserves 80000000000000000<br>callback.reserves 1452000000000 | reactive.reserves 80000000000000000<br>callback.reserves 1452000000000<br>poller.balance 192000000000000000<br>poller.reserves 320000000000000000 | reactive.reserves 80000000000000000<br>callback.reserves 1452000000000<br>hub.balance 4465672600000000000<br>hub.reserves 178891600000000000<br>vault.balance 1948400410800000<br>vault.reserves 199130951600000 |
| Capital shared with the fleet | none | none | hub.balance, hub.reserves, vault.balance, vault.reserves |

## Definitions

- **Blocks of downtime**: Blocks inside the debt intervals of the reactive canary on Lasna and of the callback canary on the destination chain, for each chain: from a `BlacklistContract` log of the ledger to the next `WhitelistContract` log, or to `to`; the `Gap` event of the hub is not used, because its start block is 0 for a rescue that no debt log started.
- **Events missed**: `Poke(n)` logs of the reactive canary in `from..to` with no `Tick(n)` log of that canary.
- **Callbacks dropped**: `Tick(n)` logs of the reactive canary in `from..to` with no `Pong(n)` log of its callback canary.
- **Guardian cost**: Principal is the sum that the guardian paid to the two contracts of the arm (arm B: `Paid` of the poller; arm C: `Rescued` and `ToppedUp` of the hub and `Result.paid` of the vault); fees are what the ledger took from the guardian: its reserves minus its debt at block `from - 1`, minus the same value at `to`, plus its `SelfCare` deposits, plus each deposit that cleared a debt of it (the ledger state at the block of its `WhitelistContract` log against the block before), and for the poller plus its fee float of the deployment; each fee figure is a lower bound, because a deposit of another account to a guardian with no debt has no log and is not in the figure, and a fee that the ledger took in the block of a deposit that cleared a debt is not in it; `feeDetail.exact` is false for a figure that holds a deposit that cleared a debt, and for a figure of 0 with `feeDetail.unloggedDeposits` above 0, which is the least sum of deposits with no log that the ledger state proves.
- **Capital locked**: At `to`: the reserves of the two contracts of the arm, and the funds of the guardian (arm B: the balance and the reserves of the poller; arm C: the balance and the reserves of the hub and of the vault, which are shared with each other enrolled target).
- **Blocks of guardian downtime**: Blocks inside the debt intervals of the guardian itself (arm B: the poller; arm C: the hub on Lasna and the vault on the destination chain), from the ledger logs: a hub at its principal floor goes into debt with no `HubLow` event, so that event is not the source.
- **Junk flood**: The balance of the vault at the start block and at the end block of the junk flood, the sums of `Result.paid` and `SelfCare.added` of the vault after the start block up to the end block, and the balance change that these payments do not explain; the expected value is 0.

## Junk flood

Status: not recorded.
<!-- end: evidence/experiment/table.md -->

## Quarantine

| Figure | Value | Chain, block and hash |
|---|---|---|
| Looping canary | `0xF6152129A1Cbef7c04F7c92D47B6AA0Ac80FA5F9` | lasna |
| `Rescued` |  | 9051444<br>`0xeb5f7b5f092550dc46bb9eee726ac6d0c9bcf0999510209cd32dafb48cb6b955` |
| `Rescued` |  | 9051466<br>`0x0513ec1482d1df1aa9fa7a6376efd7656a3c320cfd7245a62cb37aed1a0acf2d` |
| `Quarantined` |  | 9051488<br>`0xe5958954b40c20d3001134a31a5d3bd515f4c7b89f208940f2717e50cf95d0bd` |
| Rescues from the enrollment to `Quarantined` | 2 | the `Rescued` rows |
| `maxRescues` of the policy | 2 | `hub.enroll` `0xa4538529f12a96807cb7a780b40433fae769f615af0dc3e489151210b94b4f65` |
| Principal paid from the enrollment to `Quarantined` | 24960000000000000 | before the `Quarantined` log of block 9051488 |
| `dailyCap` of the policy | 260000000000000000 | `hub.enroll` `0xa4538529f12a96807cb7a780b40433fae769f615af0dc3e489151210b94b4f65` |
| Principal paid after `Quarantined` | 0 | after the `Quarantined` log of block 9051488, to block 9071008 |

## Junk recovery

Status: not recorded.

## Guests

No guest enrolled.
