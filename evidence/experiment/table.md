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
| Guardian cost: fees, lasna | 0 | 0 | 67036500000000000 |
| Guardian cost: fees, base-sepolia | 0 | 0 | 0 |
| Guardian cost: fee scope | no guardian | the poller serves the reactive canary of this arm only; the figure is from its deployment to `to` | the hub and the vault serve each enrolled target of the deployment and each scenario; the figures are their whole fee consumption in `from..to`, and no share is estimated |
| Blocks of guardian downtime | none | lasna 0 | lasna 0<br>base-sepolia 0 |
| Capital locked | reactive.reserves 80000000000000000<br>callback.reserves 1452000000000 | reactive.reserves 80000000000000000<br>callback.reserves 1452000000000<br>poller.balance 192000000000000000<br>poller.reserves 320000000000000000 | reactive.reserves 80000000000000000<br>callback.reserves 1452000000000<br>hub.balance 4465672600000000000<br>hub.reserves 178891600000000000<br>vault.balance 1948400410800000<br>vault.reserves 199130951600000 |
| Capital shared with the fleet | none | none | hub.balance, hub.reserves, vault.balance, vault.reserves |

## Definitions

- **Blocks of downtime**: Blocks inside the debt intervals of the reactive canary on Lasna and of the callback canary on the destination chain, for each chain: from a `BlacklistContract` log of the ledger to the next `WhitelistContract` log, or to `to`; the `Gap` event of the hub is not used, because its start block is 0 for a rescue that no debt log started.
- **Events missed**: `Poke(n)` logs of the reactive canary in `from..to` with no `Tick(n)` log of that canary.
- **Callbacks dropped**: `Tick(n)` logs of the reactive canary in `from..to` with no `Pong(n)` log of its callback canary.
- **Guardian cost**: Principal is the sum that the guardian paid to the two contracts of the arm (arm B: `Paid` of the poller; arm C: `Rescued` and `ToppedUp` of the hub and `Result.paid` of the vault); fees are what the ledger took from the guardian: its reserves minus its debt at block `from - 1`, minus the same value at `to`, plus its `SelfCare` deposits, plus each deposit that cleared a debt of it (the ledger state at the block of its `WhitelistContract` log against the block before), and for the poller plus its fee float of the deployment; a fee that the ledger took in the block of such a deposit is not in the figure, so `feeDetail.exact` is false for a figure that holds such a deposit.
- **Capital locked**: At `to`: the reserves of the two contracts of the arm, and the funds of the guardian (arm B: the balance and the reserves of the poller; arm C: the balance and the reserves of the hub and of the vault, which are shared with each other enrolled target).
- **Blocks of guardian downtime**: Blocks inside the debt intervals of the guardian itself (arm B: the poller; arm C: the hub on Lasna and the vault on the destination chain), from the ledger logs: a hub at its principal floor goes into debt with no `HubLow` event, so that event is not the source.
- **Junk flood**: The balance of the vault at the start block and at the end block of the junk flood, the sums of `Result.paid` and `SelfCare.added` of the vault after the start block up to the end block, and the balance change that these payments do not explain; the expected value is 0.

## Junk flood

Status: not recorded.
