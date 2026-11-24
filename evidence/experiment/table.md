# Three-arm experiment

| Chain | Chain ID | From | To |
|---|---|---|---|
| lasna | 5318007 | 9051258 | 9142906 |
| base-sepolia | 84532 | 62552591 | 63039082 |

Amounts are in wei of the chain that the row names.

| Metric | A: no guardian | B: cron poller | C: Lifeline |
|---|---|---|---|
| Pokes, ticks, pongs | 24, 7, 3 | 24, 7, 3 | 24, 24, 24 |
| Blocks of downtime, lasna | 9891 | 6978 | 3 |
| Blocks of downtime, base-sepolia | 58884 | 58841 | 20 |
| Events missed | 17 | 17 | 0 |
| Callbacks dropped | 4 | 4 | 0 |
| Guardian cost: principal, lasna | 0 | 70380000000000000 | 280160000000000000 |
| Guardian cost: principal, base-sepolia | 0 | 0 | 13194053400000 |
| Guardian cost: fees (lower bound), lasna | 0 | 109867000000000000 | 482644300000000000 |
| Guardian cost: fees (lower bound), base-sepolia | 0 | 0 | 333778670822195 |
| Guardian cost: fee figures that are not exact | none | none | base-sepolia |
| Guardian cost: fee scope | no guardian | the poller serves the reactive canary of this arm only; the figure is from its deployment to `to` | the hub and the vault serve each enrolled target of the deployment and each scenario; the figures are their whole fee consumption in `from..to`, and no share is estimated |
| Blocks of guardian downtime | none | lasna 0 | lasna 0<br>base-sepolia 169 |
| Capital locked | reactive.reserves 0<br>callback.reserves 0 | reactive.reserves 64000000000000000<br>callback.reserves 0<br>poller.balance 121620000000000000<br>poller.reserves 210133000000000000 | reactive.reserves 64000000000000000<br>callback.reserves 1226563800000<br>hub.balance 3770589000000000000<br>hub.reserves 178207400000000000<br>vault.balance 1700494312875459<br>vault.reserves 198955926200000 |
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

| Value | Number |
|---|---|
| Chain | base-sepolia |
| Vault | 0x1aA62Eaf6746Ffc7a19330dD255211680B4952B2 |
| Start block | 63015499 |
| End block | 63038566 |
| Balance of the vault at the start block | 1873018247944628 |
| Balance of the vault at the end block | 1751053458275459 |
| Sum of `Result.paid` | 0 |
| Sum of `SelfCare.added` | 121964789669169 |
| Balance change that the payments do not explain | 0 |
| Reserves of the vault at the start block | 198831813200000 |
| Reserves of the vault at the end block | 0 |
| Debt of the vault at the end block | 11676388108 |
| `CallbackFailure` logs of the vault | 556 |
