# Lifeline: evidence for the judges

This file is generated: `python -m lifeline.judge` writes it from chain data. Do not edit it by hand. The data file of the same run, `data.json`, has each figure of this file. Each amount is in wei of the chain that its row names. A block is a block of the chain that its row or its column names.

## Claim limit

Lifeline restores future delivery, not zero loss. Work that arrives while a target is in debt is lost, and nothing replays it. Each gap of this file gives the blocks in which a target was dead, so that its owner knows what to retry.

## Heights

| Chain | Chain ID | From | To |
|---|---|---|---|
| lasna | 5318007 | 9039165 | 9143104 |
| base-sepolia | 84532 | 62488753 | 63040152 |
| unichain-sepolia | 1301 | 68567187 | 68677469 |

Each log query of this run is inside `from..to` of its chain. `from` is the deployment block of the Lifeline contract of that chain: the hub on lasna, the vault on a destination chain. Each state value is read at `to`.

## Destination rescues

Each row is one relay request of the hub: a `Relayed` log on lasna. The join finds the `Result` log of the vault of the destination chain with the same request ID and the same target. The debt event is the last `BlacklistContract` log of the callback proxy for the target before the result. For a rescue, the `WhitelistContract` log of the proxy is in the transaction of the result.

### Joined

| Request ID | Chain | Target | Debt event | `Relayed`, lasna | `Result` | Outcome | Paid | `WhitelistContract` |
|---|---|---|---|---|---|---|---|---|
| 340282366920938463463374607431768211457 | base-sepolia | `0xa462B906917C0372872AD35C0321eB5Daa60692E` | 62489494<br>`0x9bb5e827d134906e1ec5f184cd2fecd4a0370974fb8711d2baaabedcb0c15727` | 9039315<br>`0x33cc6420f62ea2653c5fe28ae6b41d572a06405d10fdbc43fa2c9244fc35061e` | 62489499<br>`0xd19743a00790ed97d97e0a4d830759dbf1293d2257fd61eb0f56f47b25091adf` | Rescued | 50559145400000 | 62489499<br>`0xd19743a00790ed97d97e0a4d830759dbf1293d2257fd61eb0f56f47b25091adf` |
| 340282366920938463463374607431768211458 | base-sepolia | `0x81de741ACEd87B5b59770391AE6426333E444dc7` | 62955820<br>`0xaa6db5420b9d050467c71662846fc45680d6b0e43a0fa10740b7b316d1835704` | 9127221<br>`0x4a366f63e7a56405351221696b89776cd4def9e4f1e62e09d9d901a4ae73084c` | 62955825<br>`0x181ec8d0b285d86128797508317365df1eacdfe1d229851e355d461370ed2614` | Rescued | 50560930090454 | 62955825<br>`0x181ec8d0b285d86128797508317365df1eacdfe1d229851e355d461370ed2614` |
| 340282366920938463463374607431768211459 | base-sepolia | `0x313496642Ce898F7dD723B6AB6CF4A5891A456ed` | 62980279<br>`0x667f3903afddb62c14cfd0e0ba6ba8440cb59febb309895038bb99c74647968e` | 9131793<br>`0x4d17fe6d568a59199b014c27583a3a073faece3dcc7465d0acf0b071ef902672` | 62980284<br>`0xca270503967fa43799aa53cddbb9548f6e93331d5b082ce6a6fd5b0a12eb537b` | Rescued | 3129436200000 | 62980284<br>`0xca270503967fa43799aa53cddbb9548f6e93331d5b082ce6a6fd5b0a12eb537b` |
| 340282366920938463463374607431768211460 | base-sepolia | `0x313496642Ce898F7dD723B6AB6CF4A5891A456ed` | 62990196<br>`0x28b5763a5a59adea895ce20903799bced1ae2839b4aa74c4e1cf7c9e26d6913b` | 9133654<br>`0xfce18edfdd39e729b8a62e59a46913b4d2ee9c0ca912309cc08853503ee1d89e` | 62990201<br>`0xb90ce349675071e8172cf7f2ca89e21e2df1425200bb1a80d565c7d84341c422` | Rescued | 3354872400000 | 62990201<br>`0xb90ce349675071e8172cf7f2ca89e21e2df1425200bb1a80d565c7d84341c422` |
| 340282366920938463463374607431768211461 | base-sepolia | `0x313496642Ce898F7dD723B6AB6CF4A5891A456ed` | 63000135<br>`0xa87bdab3acd6b05a9085f7d7575b6d973041b7b96c58c3c0c37cb2e4a3e7025a` | 9135544<br>`0xb84178c59a1797a9185891a567d389545ad6c338ba02b613bb8410f836b1d44c` | 63000140<br>`0x06974537ab68e52c24234f03fb7669280a6606bc6ee5826f01fe12e178a22fab` | Rescued | 3354872400000 | 63000140<br>`0x06974537ab68e52c24234f03fb7669280a6606bc6ee5826f01fe12e178a22fab` |
| 340282366920938463463374607431768211462 | base-sepolia | `0x313496642Ce898F7dD723B6AB6CF4A5891A456ed` | 63010052<br>`0x528a7a0b51884af383cf9afe4d059df72e2a9d637a1aca7e0f6e7680e40039f9` | 9137418<br>`0xbefa2508186478c660a2c7beff67f9b51c6cbce7075b6f9b42c299eef1099607` | 63010057<br>`0xd32920a1ca3031ba2b5d57ab61b3a3b14f92a63007bf8aad3133ff86e2faf2b8` | Rescued | 3354872400000 | 63010057<br>`0xd32920a1ca3031ba2b5d57ab61b3a3b14f92a63007bf8aad3133ff86e2faf2b8` |

### Open

A relay request with no `Result` log in the heights of its destination chain.

None.

## Lasna rescues and gaps

Each row is one `Rescued` log of the hub with the `Gap` log of the same transaction. A dead-from block of zero means that no debt log started the rescue.

| Target | `Rescued` | Debt | Paid | Dead from block | Alive at block |
|---|---|---|---|---|---|
| `0xF6152129A1Cbef7c04F7c92D47B6AA0Ac80FA5F9` | 9051444<br>`0xeb5f7b5f092550dc46bb9eee726ac6d0c9bcf0999510209cd32dafb48cb6b955` | 11980000000000000 | 12980000000000000 | 9051443 | 9051444 |
| `0xF6152129A1Cbef7c04F7c92D47B6AA0Ac80FA5F9` | 9051466<br>`0x0513ec1482d1df1aa9fa7a6376efd7656a3c320cfd7245a62cb37aed1a0acf2d` | 10980000000000000 | 11980000000000000 | 9051465 | 9051466 |
| `0x541FCD5EeA3Af2d8fd2A8447299128ad476c7948` | 9133032<br>`0x9dd8a1350b7a3216b5e9fbf561373c6e0a1fe4255f65fea54c3c4a3ca13dcf9c` | 6380000000000000 | 70380000000000000 | 9133031 | 9133032 |
| `0x541FCD5EeA3Af2d8fd2A8447299128ad476c7948` | 9134914<br>`0x50b463025ecadd7c7bfc51231c13746b2c06cf089cb55d431708b3ad4ef1673c` | 10040000000000000 | 74040000000000000 | 9134913 | 9134914 |
| `0x541FCD5EeA3Af2d8fd2A8447299128ad476c7948` | 9136794<br>`0x81937deca291aea975b223dd2a82ce5acd7e2930efe9442176008612bb9135c3` | 10040000000000000 | 74040000000000000 | 9136793 | 9136794 |

## Rescue latency

Each row is one rescue that a debt log started: the time from the `BlacklistContract` log of the target to the payment that ended the debt, on the chain of the target. The seconds are the difference of the two block timestamps, in whole seconds. The median is the lower middle value. `debt event`: the debt log started the rescue, by a reaction of the hub on lasna or by a relay request of the hub. `sweep`: the target waited for a sweep, for example while its vault was down.

| Started by | Chain | Rescues | Median seconds | Largest seconds | Median blocks | Largest blocks |
|---|---|---|---|---|---|---|
| debt event | lasna | 5 | 1 | 1 | 1 | 1 |
| debt event | base-sepolia | 6 | 1 | 1 | 5 | 5 |
| sweep | base-sepolia | 1 | 25 | 25 | 127 | 127 |

| Started by | Chain | Target | Debt log block | Rescue block | Blocks | Seconds | Rescue transaction |
|---|---|---|---|---|---|---|---|
| debt event | lasna | `0xF6152129A1Cbef7c04F7c92D47B6AA0Ac80FA5F9` | 9051443 | 9051444 | 1 | 1 | `0xeb5f7b5f092550dc46bb9eee726ac6d0c9bcf0999510209cd32dafb48cb6b955` |
| debt event | lasna | `0xF6152129A1Cbef7c04F7c92D47B6AA0Ac80FA5F9` | 9051465 | 9051466 | 1 | 1 | `0x0513ec1482d1df1aa9fa7a6376efd7656a3c320cfd7245a62cb37aed1a0acf2d` |
| debt event | lasna | `0x541FCD5EeA3Af2d8fd2A8447299128ad476c7948` | 9133031 | 9133032 | 1 | 1 | `0x9dd8a1350b7a3216b5e9fbf561373c6e0a1fe4255f65fea54c3c4a3ca13dcf9c` |
| debt event | lasna | `0x541FCD5EeA3Af2d8fd2A8447299128ad476c7948` | 9134913 | 9134914 | 1 | 1 | `0x50b463025ecadd7c7bfc51231c13746b2c06cf089cb55d431708b3ad4ef1673c` |
| debt event | lasna | `0x541FCD5EeA3Af2d8fd2A8447299128ad476c7948` | 9136793 | 9136794 | 1 | 1 | `0x81937deca291aea975b223dd2a82ce5acd7e2930efe9442176008612bb9135c3` |
| debt event | base-sepolia | `0xa462B906917C0372872AD35C0321eB5Daa60692E` | 62489494 | 62489499 | 5 | 1 | `0xd19743a00790ed97d97e0a4d830759dbf1293d2257fd61eb0f56f47b25091adf` |
| debt event | base-sepolia | `0x81de741ACEd87B5b59770391AE6426333E444dc7` | 62955820 | 62955825 | 5 | 1 | `0x181ec8d0b285d86128797508317365df1eacdfe1d229851e355d461370ed2614` |
| debt event | base-sepolia | `0x313496642Ce898F7dD723B6AB6CF4A5891A456ed` | 62980279 | 62980284 | 5 | 1 | `0xca270503967fa43799aa53cddbb9548f6e93331d5b082ce6a6fd5b0a12eb537b` |
| debt event | base-sepolia | `0x313496642Ce898F7dD723B6AB6CF4A5891A456ed` | 62990196 | 62990201 | 5 | 1 | `0xb90ce349675071e8172cf7f2ca89e21e2df1425200bb1a80d565c7d84341c422` |
| debt event | base-sepolia | `0x313496642Ce898F7dD723B6AB6CF4A5891A456ed` | 63000135 | 63000140 | 5 | 1 | `0x06974537ab68e52c24234f03fb7669280a6606bc6ee5826f01fe12e178a22fab` |
| debt event | base-sepolia | `0x313496642Ce898F7dD723B6AB6CF4A5891A456ed` | 63010052 | 63010057 | 5 | 1 | `0xd32920a1ca3031ba2b5d57ab61b3a3b14f92a63007bf8aad3133ff86e2faf2b8` |
| sweep | base-sepolia | `0xC67C2BDE920E75378eB71c9011abd3F4C1775289` | 63038646 | 63038773 | 127 | 25 | `0x77633be6a468669993383edddfe263c025fbcd20e083bcffcfb5964e71453f50` |

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
| Reserves kept | 60373758200000000000 | at block 9143104 |

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
| Reserves kept | 1985547990287874 | at block 68677469 |

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
| Reserves kept | 50000000000000 | at block 63040152 |

| Gap: dead from block | Alive at block | Blocks | Transaction that ended the gap |
|---|---|---|---|
| 62489494 | 62489499 | 5 | `0xd19743a00790ed97d97e0a4d830759dbf1293d2257fd61eb0f56f47b25091adf` |

The specification does not define a fill. This report reads it as a fill event of the fleet itself, as `subjects.fastfill.fill` of `config/network.json` defines it: the count is the number of these logs from the first block of the fill chain at or after the enrollment, by the block timestamps, to `to`. A guardian top-up is a separate figure. For a lasna target a gap is a `Gap` log of the hub. For a target of a destination chain a gap is a debt interval at the callback proxy: from a `BlacklistContract` log to the next `WhitelistContract` log, or to `to`. The block count of a gap starts at the enrollment block if the debt started before it.

## Guardian cost

Each figure counts from the deployment block of the contract to `to`.

| Guardian | Chain | Address | Deployment block | Principal paid | `SelfCare` added | Fee float consumed | Balance | Reserves | Debt | State block |
|---|---|---|---|---|---|---|---|---|---|---|
| hub | lasna | `0xbba61E507e7CC334e57A62c39A776272DA6b07Dd` | 9039165 | 305120000000000000 | 724291000000000000 | 546083600000000000 | 3770589000000000000 | 178207400000000000 | 0 | 9143104 |
| vault of base-sepolia | base-sepolia | `0x1aA62Eaf6746Ffc7a19330dD255211680B4952B2` | 62488753 | 164873274290454 | 334632412834087 | 135676486634087 | 1700494312875459 | 198955926200000 | 0 | 63040152 |
| vault of unichain-sepolia | unichain-sepolia | `0x89dbc693c78C8844b5A3294DE8f7c0f0adf1DC26` | 68567187 | 0 | 201706152800000 | 1923414900000 | 4998293847200000 | 199782737900000 | 0 | 68677469 |

The principal paid is the sum of the payment events: `Rescued` and `ToppedUp` of the hub, `Result` of a vault. The fee float consumed is the sum of `SelfCare.added`, minus the reserves at `to`, plus the debt at `to`. A deposit to the float by another account is not visible in these events: it makes the figure too low by its amount.

## Three-arm table

The text between the two markers is `evidence/experiment/table.md`, unchanged. Its heights are the heights of the experiment.

<!-- begin: evidence/experiment/table.md, unchanged -->
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
| Principal paid after `Quarantined` | 0 | after the `Quarantined` log of block 9051488, to block 9143104 |

## Junk recovery

Vault `0x1aA62Eaf6746Ffc7a19330dD255211680B4952B2` on base-sepolia.

| Step | Chain | Block | Transaction |
|---|---|---|---|
| `BlacklistContract(vault)` | base-sepolia | 63038541 | `0x0d376cab18c7c5517f4ad9d30818feb65571a5b5a605be246889f613d414180a` |
| `VaultDown` | lasna | 9142807 | `0x61abedad9eab8ee3ceedd0ebf00220644f1d468a4112150f4b17c942f0fcd6a0` |
| `Pending` | lasna | 9142827 | `0xe9938f65b4d2989ede98cb3ec5e33cdadeeb50c1caf70cff3a72684b801637b0` |
| `WhitelistContract(vault)` | base-sepolia | 63038710 | `0x36224a76be7bdcfd0960e296317016046f9bb4dae2637d35bb7054515133b298` |
| `VaultUp` | lasna | 9142849 | `0x916427a8591de24459f4e4639528a95d6e12fe8cdb6819c958435e2bb397f89e` |
| `Result(waiting.callback)` | base-sepolia | 63038773 | `0x77633be6a468669993383edddfe263c025fbcd20e083bcffcfb5964e71453f50` |

| Target that waited | Request ID of the sweep | Outcome | Paid | `Result` |
|---|---|---|---|---|
| `0xC67C2BDE920E75378eB71c9011abd3F4C1775289` | 340282366920938463463374607431768211463 | Rescued | 50559145400000 | 63038773<br>`0x77633be6a468669993383edddfe263c025fbcd20e083bcffcfb5964e71453f50` |

| Figure of the data file of the experiment | Value | Block |
|---|---|---|
| Balance of the vault at the start block of the flood | 1873018247944628 | 63015499 |
| Balance of the vault at the end block of the flood | 1751053458275459 | 63038566 |
| Balance change that the payments do not explain | 0 | from block 63015499 to block 63038566 |

## Guests

No guest enrolled.
