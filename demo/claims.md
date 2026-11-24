# Demo video: claims table

This file is written by `python demo/claims.py`. Do not edit it by hand.

Each row is one number, hash, address, block or factual statement that the video shows or says. The command builds the text of the video from the value at the source and then looks for that text in the narration, in the terminal recording, or in the text of the framed element as the page showed it when its picture was made. "Equal" is the result of that check.

Rows: 127. Not equal: 0. Not checked: 0. Length of the cut: 176.5 s.

| Time | Shown or said | Text in the video | Source | Value there | Equal |
|---|---|---|---|---|---|
| 0:00.3 | shown | `0xacF7a5E9B0e986863579f4E1bA0bd2c54FF7a427` | deployments/manifest.json: fixtures.armA.reactive.address | 0xacF7a5E9B0e986863579f4E1bA0bd2c54FF7a427 | yes |
| 0:00.3 | said | the first sentence is the line about the narration | demo/narration.json: clips[0] | n01 | yes |
| 0:03.7 | said | "generated with ElevenLabs" | demo/narration.json: voice.engine | ElevenLabs | yes |
| 0:06.5 | shown | `Contract reserves : 0,0000 REACT` | evidence/judge/data.json: experiment.arms.A.capitalLocked[reactive.reserves] | 0 | yes |
| 0:06.5 | shown | `Contract debt : 0,0063 REACT` | chain: debt(armA.reactive) at the system contract of Lasna, now; Reactscan cuts the value to 4 decimals | 6380000000000000 | yes |
| 0:07.2 | said | "ran out of gas credit" | evidence/judge/data.json: experiment.arms.A.capitalLocked[reactive.reserves], and the debt of the row above | reserves 0, debt 6380000000000000 | yes |
| 0:09.0 | said | "Each transaction to it still shows success" | the Reactscan page of armA.reactive: the status column of the picture | 24 rows with Success, 0 other | yes |
| 0:12.3 | shown | `// Transactions [24]` | evidence/judge/data.json: experiment.arms.A.pokes | 24 | yes |
| 0:12.3 | shown | `// Reactive Executions [7]` | evidence/judge/data.json: experiment.arms.A.ticks | 7 | yes |
| 0:12.4 | said | "the network gives it no more work" | evidence/judge/data.json: experiment.arms.A.eventsMissed | 17 | yes |
| 0:15.4 | shown | `22 of 75` | evidence/network/data.json: queries.buildathon_debt.result.inDebt, .submissions | 22 of 75 | yes |
| 0:15.4 | shown | `Lasna block 9140577` | evidence/network/data.json: queries.buildathon_debt.result.block | 9140577 | yes |
| 0:15.4 | shown | `424 debt events on Lasna` | evidence/network/data.json: queries.lasna_debt_events.result.count | 424 debt events on Lasna | yes |
| 0:15.4 | shown | `66 on the destination chains` | evidence/network/data.json: queries.destination_debt_events.result.total | 66 on the destination chains | yes |
| 0:15.4 | shown | `231 of 306` | evidence/network/data.json: queries.stopped_contracts.result.byChain.lasna | 231 of 306 | yes |
| 0:15.4 | shown | `19 ran out 3 times or more` | evidence/network/data.json: queries.repeat_debtors.result.byChain.lasna.contracts | 19 ran out 3 times or more | yes |
| 0:15.4 | shown | `About 159 callbacks were dropped` | evidence/network/data.json: queries.dropped_callbacks.result.dropped | About 159 callbacks were dropped | yes |
| 0:16.8 | said | "more than a quarter of the submissions" | evidence/network/data.json: queries.buildathon_debt.result | 22 of 75 = 29.3% | yes |
| 0:20.9 | shown | `60.37 lREACT` | evidence/network/data.json: queries.fastfill_reactive_reserves.result.reservesWei | 60373758200000000000 | yes |
| 0:22.6 | said | "about sixty lREACT" | evidence/network/data.json: queries.fastfill_reactive_reserves.result.reservesWei | 60373758200000000000 wei = 60.3737582 lREACT | yes |
| 0:24.3 | said | "cannot be withdrawn" | README.md, section Problem (the sentence is on screen) | in reserves that cannot be withdrawn, because over-funding was the only protection | yes |
| 0:26.1 | said | "Over-funding was the only protection" | README.md, section Problem | in reserves that cannot be withdrawn, because over-funding was the only protection | yes |
| 0:26.1 | shown | `60.3737 lREACT` | evidence/judge/data.json: fleet[FastfillReactive].reserves.value; the board cuts the value to 4 decimals | 60373758200000000000 | yes |
| 0:26.1 | shown | `low 0.05 lREACT · high 0.25 lREACT` | deployments/manifest.json: enrollments[FastfillReactive].policy.low, .high | 50000000000000000, 250000000000000000 | yes |
| 0:30.3 | said | "a gas guardian that I set once" | README.md, first paragraph and section How it works | Lifeline is the guardian I set once | yes |
| 0:33.1 | shown | the sequence diagram: AIRBAG and GAUGE | README.md, section How it works: the Mermaid block, drawn by github.com | sequenceDiagram | yes |
| 0:33.7 | said | "listens to the network's own debt event" | README.md, first paragraph and section How it works | The hub subscribes to the network's own debt event | yes |
| 0:36.2 | said | "pays the debt, and refills the contract" | README.md, first paragraph and section How it works | The payment is `debt + (high - reserves)` | yes |
| 0:39.9 | said | "refills a contract on a cron tick, before it runs out" | README.md, first paragraph and section How it works | each target with reserves below `low` is refilled to `high` | yes |
| 0:44.2 | shown | `--low 0.0005 --high 0.002 --max-debt 0.001 --daily-cap 0.004 --max-rescues 2 --window 3600` | deployments/manifest.json: enrollments[FastfillExecutor].policy (wei as a decimal amount) | {"low": "500000000000000", "high": "2000000000000000", "maxDebt": "1000000000000000", "dailyCap": "4000000000000000", "maxRescues": 2, "window": 3600} | yes |
| 0:44.2 | said | "Enrollment is one command" | README.md, section Operator guide | python -m lifeline.ops enroll | yes |
| 0:48.8 | said | "two refill marks, a daily budget, and a limit of rescues" | deployments/manifest.json: the fields of enrollments[FastfillExecutor].policy | low, high, maxDebt, dailyCap, maxRescues, window | yes |
| 0:53.7 | shown | `low 0.05 lREACT · high 0.25 lREACT` | deployments/manifest.json: enrollments[FastfillReactive].policy.low, .high | 50000000000000000, 250000000000000000 | yes |
| 0:53.7 | shown | `low 0.0005 ETH · high 0.002 ETH` | deployments/manifest.json: enrollments[FastfillExecutor].policy.low, .high | 500000000000000, 2000000000000000 | yes |
| 0:53.7 | said | "Two of my automations are enrolled" | deployments/config.json: fleet; the board shows each one with the hub state Active | FastfillReactive, FastfillExecutor | yes |
| 0:56.3 | shown | `0x29df5c19147b1a94d5bbf843b2d3b3bd4a602da52db7afa9aa36452091ce2ce9` | deployments/manifest.json: enrollments[FastfillExecutor].txs.enroll | 0x29df5c19147b1a94d5bbf843b2d3b3bd4a602da52db7afa9aa36452091ce2ce9 | yes |
| 0:56.3 | shown | `Block number : 9,039,259` | deployments/manifest.json: enrollments[FastfillExecutor].txs.enrollBlock | 9039259 | yes |
| 0:56.7 | shown | `chainId : 1301 , target : 0x916D4d5f9E363CbdFA8C34fB41185a75A6a2E705 , policy : {low: 500000000000000, high: 2000000000000000, maxDebt: 1000000000000000, dailyCap: 4000000000000000, maxRescues: 2, window: 3600}` | deployments/manifest.json: enrollments[FastfillExecutor].chainId, .target, .policy | chainId : 1301 , target : 0x916D4d5f9E363CbdFA8C34fB41185a75A6a2E705 , policy : {low: 500000000000000, high: 2000000000000000, maxDebt: 1000000000000000, dailyCap: 4000000000000000, maxRescues: 2, window: 3600} | yes |
| 0:59.1 | shown | `python -m lifeline.demo capture` | the command of lifeline/demo.py | python -m lifeline.demo capture | yes |
| 0:59.5 | said | "a live rescue on testnet" | evidence/demo/capture.json: chain, chainId; the terminal recording | base-sepolia (84532) | yes |
| 1:02.0 | shown | `armC.reactive 0x541FCD5EeA3Af2d8fd2A8447299128ad476c7948` | deployments/manifest.json: fixtures.armC.reactive.address | armC.reactive 0x541FCD5EeA3Af2d8fd2A8447299128ad476c7948 | yes |
| 1:02.0 | shown | `armC.callback 0x313496642Ce898F7dD723B6AB6CF4A5891A456ed` | deployments/manifest.json: fixtures.armC.callback.address | armC.callback 0x313496642Ce898F7dD723B6AB6CF4A5891A456ed | yes |
| 1:04.9 | said | "its callback contract on Base Sepolia" | evidence/demo/capture.json: target, chain | 0x313496642Ce898F7dD723B6AB6CF4A5891A456ed on base-sepolia | yes |
| 1:09.8 | said | "at real speed, with no cut" | demo/build/timeline.json: terminal.pieces; the last piece of the recording starts before the last poke() and has no cut to the end | one piece from 140.22 s of the recording; debt() 0 at 144.49 s | yes |
| 1:13.3 | shown | `1 debt event block 63098499 on base-sepolia` | evidence/demo/capture.json: blocks.debtEvent; evidence/judge/data.json: rescues[requestId].debtEvent.block | 63098499; 63098499 | yes |
| 1:13.3 | shown | `tx 0x622ab7e70837931092c02e94741b66631a4ae78489d4622a8216ec21a3ab58b3` | evidence/judge/data.json: rescues[requestId].debtEvent.tx | tx 0x622ab7e70837931092c02e94741b66631a4ae78489d4622a8216ec21a3ab58b3 | yes |
| 1:13.3 | shown | `tx 0xd79e28c32f6fd3803faff2f1793acc096b873e4b4cd7e21738a7dca329dd7e26` | evidence/judge/data.json: rescues[requestId].relayed.tx | tx 0xd79e28c32f6fd3803faff2f1793acc096b873e4b4cd7e21738a7dca329dd7e26 | yes |
| 1:13.3 | shown | `tx 0x428b5698f2e9ae4d1ea0fe8a1f72f03c3365c2c7b2f2b4676608a7b333ccf502` | evidence/judge/data.json: rescues[requestId].result.tx | tx 0x428b5698f2e9ae4d1ea0fe8a1f72f03c3365c2c7b2f2b4676608a7b333ccf502 | yes |
| 1:14.4 | shown | `2 hub reaction block 9154121 on lasna` | evidence/demo/capture.json: blocks.relayed; evidence/judge/data.json: rescues[requestId].relayed.block | 9154121; 9154121 | yes |
| 1:14.4 | shown | `requestId 340282366920938463463374607431768211465` | evidence/demo/capture.json: requestId; evidence/judge/data.json: rescues[].requestId | requestId 340282366920938463463374607431768211465 | yes |
| 1:16.5 | shown | `3 vault callback block 63098504 on base-sepolia` | evidence/demo/capture.json: blocks.result; evidence/judge/data.json: rescues[requestId].result.block | 63098504; 63098504 | yes |
| 1:16.5 | shown | `outcome Rescued` | evidence/judge/data.json: rescues[requestId].result.outcome | outcome Rescued | yes |
| 1:16.5 | shown | `paid 0.0000033548724 (3354872400000 wei)` | evidence/judge/data.json: rescues[requestId].result.paid | 3354872400000 | yes |
| 1:18.8 | shown | `4 debt() is 0 block 63098504 on base-sepolia` | evidence/demo/capture.json: blocks.debtZero; evidence/judge/data.json: rescues[requestId].whitelist.block | 63098504; 63098504 | yes |
| 1:18.8 | said | "The debt is zero" | evidence/judge/data.json: rescues[requestId].whitelist (the proxy emits it in the transaction of the result) | 0x428b5698f2e9ae4d1ea0fe8a1f72f03c3365c2c7b2f2b4676608a7b333ccf502 | yes |
| 1:20.3 | said | "Nobody sent a transaction after the first step" | README.md, section Proof; chain: the senders of the Relayed and the Result transaction are accounts of the node, not the account of the fixture key | senders 0x038e06667e42782e571eab20432b9237f9bd6b82, 0xf9f61c1d3325e2d4abb55dd9e0db84f7a0d914b0; fixture account 0xE52ee8943C6e7a982268CE93a78de570c3b68D45 | yes |
| 1:23.7 | shown | `0xd79e28c32f6fd3803faff2f1793acc096b873e4b4cd7e21738a7dca329dd7e26` | evidence/judge/data.json: rescues[requestId].relayed.tx | 0xd79e28c32f6fd3803faff2f1793acc096b873e4b4cd7e21738a7dca329dd7e26 | yes |
| 1:23.9 | shown | `Block number : 9,154,121` | evidence/judge/data.json: rescues[requestId].relayed.block | 9154121 | yes |
| 1:26.3 | shown | `Base Sepolia ( 84532 )` | evidence/demo/capture.json: chainId | 84532 | yes |
| 1:26.3 | shown | `Destination : 0x1aa62eaf6746ffc7a19330dd255211680b4952b2` | deployments/manifest.json: contracts.vaults.base-sepolia.address | 0x1aA62Eaf6746Ffc7a19330dD255211680B4952B2 | yes |
| 1:26.3 | shown | `Requested by : 0xbba61E507e7CC334e57A62c39A776272DA6b07Dd` | deployments/manifest.json: contracts.hub.address | Requested by : 0xbba61E507e7CC334e57A62c39A776272DA6b07Dd | yes |
| 1:26.8 | shown | `Destination tx : 0x428b5698f2e9ae4d1ea0fe8a1f72f03c3365c2c7b2f2b4676608a7b333ccf502` | evidence/judge/data.json: rescues[requestId].result.tx | Destination tx : 0x428b5698f2e9ae4d1ea0fe8a1f72f03c3365c2c7b2f2b4676608a7b333ccf502 | yes |
| 1:29.5 | shown | `0x622ab7e70837931092c02e94741b66631a4ae78489d4622a8216ec21a3ab58b3` | evidence/judge/data.json: rescues[requestId].debtEvent.tx | 0x622ab7e70837931092c02e94741b66631a4ae78489d4622a8216ec21a3ab58b3 | yes |
| 1:29.5 | shown | `Block 63098499` | evidence/judge/data.json: rescues[requestId].debtEvent.block | 63098499 | yes |
| 1:29.5 | shown | `Nov 24 2026 17:32:48 (+00:00 UTC)` | chain: the timestamp of block 63098499 of base-sepolia | 1795541568 | yes |
| 1:32.3 | shown | `0x428b5698f2e9ae4d1ea0fe8a1f72f03c3365c2c7b2f2b4676608a7b333ccf502` | evidence/judge/data.json: rescues[requestId].result.tx | 0x428b5698f2e9ae4d1ea0fe8a1f72f03c3365c2c7b2f2b4676608a7b333ccf502 | yes |
| 1:32.3 | shown | `Block 63098504` | evidence/judge/data.json: rescues[requestId].result.block | 63098504 | yes |
| 1:32.3 | shown | `Nov 24 2026 17:32:49 (+00:00 UTC)` | chain: the timestamp of block 63098504 of base-sepolia | 1795541569 | yes |
| 1:33.0 | said | "one second later" | evidence/judge/data.json: latency.rescues[tx of the result].seconds (.blocks 5) | 1 | yes |
| 1:35.2 | said | "Three identical automations got the same load" | evidence/judge/data.json: experiment.arms.A/B/C.pokes; README.md, section Lifeline against the alternatives | 24, 24, 24 | yes |
| 1:35.6 | shown | `Trigger events sent 24 24 24` | evidence/judge/data.json: experiment.arms.A/B/C.pokes | Trigger events sent 24 24 24 | yes |
| 1:35.6 | shown | `Reactions that ran on Lasna 7 7 24` | evidence/judge/data.json: experiment.arms.A/B/C.ticks | Reactions that ran on Lasna 7 7 24 | yes |
| 1:35.6 | shown | `Callbacks delivered on Base Sepolia 3 3 24` | evidence/judge/data.json: experiment.arms.A/B/C.pongs | Callbacks delivered on Base Sepolia 3 3 24 | yes |
| 1:35.6 | shown | `Blocks of downtime on Lasna 9891 6978 3` | evidence/judge/data.json: experiment.arms.A/B/C.downtimeBlocks.lasna | Blocks of downtime on Lasna 9891 6978 3 | yes |
| 1:35.6 | shown | `Blocks of downtime on Base Sepolia 58884 58841 20` | evidence/judge/data.json: experiment.arms.A/B/C.downtimeBlocks.base-sepolia | Blocks of downtime on Base Sepolia 58884 58841 20 | yes |
| 1:39.1 | said | "With no guardian, three callbacks arrived" | evidence/judge/data.json: experiment.arms.A.pongs (guardian: none) | 3 | yes |
| 1:42.3 | said | "With a cron poller, also three" | evidence/judge/data.json: experiment.arms.B.pongs (guardian: cron poller) | 3 | yes |
| 1:45.0 | said | "With Lifeline, all twenty-four" | evidence/judge/data.json: experiment.arms.C.pongs (guardian: Lifeline) | 24 | yes |
| 1:48.6 | shown | `B was dead on Lasna for 7369 s` | evidence/network/data.json: queries.cron_poller_ward_dead_time.result.longestSeconds | 7369 | yes |
| 1:48.6 | shown | `C delivered each of its 24 callbacks` | evidence/judge/data.json: experiment.arms.C.pongs | 24 | yes |
| 1:48.6 | shown | `Each debt ended 1 s later` | evidence/judge/data.json: latency.byChain.*.largestSeconds | [1, 1] | yes |
| 1:48.6 | shown | `The 169 blocks of vault downtime` | evidence/judge/data.json: experiment.arms.C.guardianDowntimeBlocks.base-sepolia | 169 | yes |
| 1:50.0 | said | "dead for about two hours" | evidence/network/data.json: queries.cron_poller_ward_dead_time.result.longestSeconds | 7369 s = 2.05 h | yes |
| 1:52.7 | said | "Lifeline is not a wallet refiller" | README.md, section Lifeline against the alternatives (the sentence is on screen) | Lifeline is not a wallet refiller. A wallet refiller sends native currency to an account. Lifeline pays the debt of a contract at the gas ledger of Reactive Network: a stopped contract starts again only when that debt is paid. | yes |
| 1:53.2 | shown | the comparison table: 11 rows | README.md, section Lifeline against the alternatives: each row of the table, cell by cell | 11 of 11 rows are on the page | yes |
| 1:55.9 | said | "A refiller sends currency to an account" | README.md, section Lifeline against the alternatives (the sentence is on screen) | Lifeline is not a wallet refiller. A wallet refiller sends native currency to an account. Lifeline pays the debt of a contract at the gas ledger of Reactive Network: a stopped contract starts again only when that debt is paid. | yes |
| 1:58.9 | said | "Lifeline pays a contract's debt at the gas ledger" | README.md, section Lifeline against the alternatives (the sentence is on screen) | Lifeline is not a wallet refiller. A wallet refiller sends native currency to an account. Lifeline pays the debt of a contract at the gas ledger of Reactive Network: a stopped contract starts again only when that debt is paid. | yes |
| 1:58.9 | shown | `Restarts a stopped reactive contract Yes - No: its source has no depositTo and no debt event` | README.md, the comparison table: the row is shown as the README has it | \| Restarts a stopped reactive contract \| Yes \| - \| No: its source has no `depositTo` and no debt event \| | yes |
| 2:02.6 | said | "only that restarts a stopped contract" | README.md, section Lifeline against the alternatives (the sentence is on screen) | Lifeline is not a wallet refiller. A wallet refiller sends native currency to an account. Lifeline pays the debt of a contract at the gas ledger of Reactive Network: a stopped contract starts again only when that debt is paid. | yes |
| 2:07.0 | shown | `Quarantined 1` | evidence/judge/data.json: quarantine.recorded; `python -m lifeline.ops status`: one target in the state Quarantined | True | yes |
| 2:07.0 | shown | `experiment looping` | deployments/manifest.json: enrollments[experiment looping].label; evidence/judge/data.json: quarantine.target | 0xF6152129A1Cbef7c04F7c92D47B6AA0Ac80FA5F9 | yes |
| 2:07.0 | shown | `0.01098 lREACT` | chain: debt(looping canary) at the system contract of Lasna, now | 10980000000000000 | yes |
| 2:09.0 | said | "After two rescues" | evidence/judge/data.json: quarantine.rescues (count), quarantine.maxRescues | 2, 2 | yes |
| 2:09.9 | shown | `2 rescues in 3,600 s` | deployments/manifest.json: enrollments[experiment looping].policy.maxRescues, .window; evidence/judge/data.json: quarantine.maxRescues | 2, 3600; 2 | yes |
| 2:10.5 | said | "the hub quarantined this looping test contract" | evidence/judge/data.json: quarantine.quarantined | {"block": 9051488, "tx": "0xe5958954b40c20d3001134a31a5d3bd515f4c7b89f208940f2717e50cf95d0bd"} | yes |
| 2:13.6 | shown | `Quarantined` | evidence/judge/data.json: quarantine.quarantined.block | 9051488 | yes |
| 2:13.8 | said | "paid nothing more" | evidence/judge/data.json: quarantine.paidAfterQuarantine | 0 | yes |
| 2:15.8 | said | "A flood of junk callbacks pushed one vault offline" | evidence/experiment/data.json: junk.callbackFailures, .debtEnd; evidence/judge/data.json: junkRecovery.steps[BlacklistContract(vault)] | 556 failed callbacks, debt 11676388108 at the end | yes |
| 2:15.8 | shown | `Balance of the vault at the start block 1873018247944628` | evidence/experiment/data.json: junk.balanceStart | Balance of the vault at the start block 1873018247944628 | yes |
| 2:15.8 | shown | `Balance of the vault at the end block 1751053458275459` | evidence/experiment/data.json: junk.balanceEnd | Balance of the vault at the end block 1751053458275459 | yes |
| 2:16.1 | shown | `CallbackFailure logs of the vault 556` | evidence/experiment/data.json: junk.callbackFailures | 556 | yes |
| 2:19.8 | shown | `Reserves of the vault at the end block 0` | evidence/experiment/data.json: junk.reservesEnd (start: junk.reservesStart 198831813200000) | 0 | yes |
| 2:19.8 | said | "It lost its small fee float" | evidence/experiment/data.json: junk.reservesStart, .reservesEnd | 198831813200000 to 0 wei (0.0001988318132 ETH) | yes |
| 2:21.8 | shown | `Sum of SelfCare.added 121964789669169` | evidence/experiment/data.json: junk.selfCareAdded | 121964789669169 | yes |
| 2:22.0 | said | "a little self-care" | evidence/experiment/data.json: junk.selfCareAdded; README.md, section Proof: 0.000122 ETH | 121964789669169 wei = 0.000121964789669169 ETH | yes |
| 2:23.7 | shown | `Balance change that the payments do not explain 0` | evidence/experiment/data.json: junk.unexplainedBalanceChange; evidence/judge/data.json: junkRecovery.unexplainedChange | 0 | yes |
| 2:23.7 | said | "Its other funds were not touched" | evidence/experiment/data.json: junk.unexplainedBalanceChange, .resultPaid | 0, 0 | yes |
| 2:26.3 | said | "Recovery needed two transactions from the owner" | deployments/manifest.json: contracts.vaults.base-sepolia.recoveries[-1].deposit, .vaultUp[] | 0x36224a76be7bdcfd0960e296317016046f9bb4dae2637d35bb7054515133b298, 0x916427a8591de24459f4e4639528a95d6e12fe8cdb6819c958435e2bb397f89e | yes |
| 2:26.7 | shown | `BlacklistContract(vault) base-sepolia 63038541 0x0d376cab18c7c5517f4ad9d30818feb65571a5b5a605be246889f613d414180a` | evidence/judge/data.json: junkRecovery.steps[BlacklistContract(vault)] | BlacklistContract(vault) base-sepolia 63038541 0x0d376cab18c7c5517f4ad9d30818feb65571a5b5a605be246889f613d414180a | yes |
| 2:26.7 | shown | `VaultDown lasna 9142807 0x61abedad9eab8ee3ceedd0ebf00220644f1d468a4112150f4b17c942f0fcd6a0` | evidence/judge/data.json: junkRecovery.steps[VaultDown] | VaultDown lasna 9142807 0x61abedad9eab8ee3ceedd0ebf00220644f1d468a4112150f4b17c942f0fcd6a0 | yes |
| 2:26.7 | shown | `Pending lasna 9142827 0xe9938f65b4d2989ede98cb3ec5e33cdadeeb50c1caf70cff3a72684b801637b0` | evidence/judge/data.json: junkRecovery.steps[Pending] | Pending lasna 9142827 0xe9938f65b4d2989ede98cb3ec5e33cdadeeb50c1caf70cff3a72684b801637b0 | yes |
| 2:26.7 | shown | `WhitelistContract(vault) base-sepolia 63038710 0x36224a76be7bdcfd0960e296317016046f9bb4dae2637d35bb7054515133b298` | evidence/judge/data.json: junkRecovery.steps[WhitelistContract(vault)] | WhitelistContract(vault) base-sepolia 63038710 0x36224a76be7bdcfd0960e296317016046f9bb4dae2637d35bb7054515133b298 | yes |
| 2:26.7 | shown | `VaultUp lasna 9142849 0x916427a8591de24459f4e4639528a95d6e12fe8cdb6819c958435e2bb397f89e` | evidence/judge/data.json: junkRecovery.steps[VaultUp] | VaultUp lasna 9142849 0x916427a8591de24459f4e4639528a95d6e12fe8cdb6819c958435e2bb397f89e | yes |
| 2:26.7 | shown | `Result(waiting.callback) base-sepolia 63038773 0x77633be6a468669993383edddfe263c025fbcd20e083bcffcfb5964e71453f50` | evidence/judge/data.json: junkRecovery.steps[Result(waiting.callback)] | Result(waiting.callback) base-sepolia 63038773 0x77633be6a468669993383edddfe263c025fbcd20e083bcffcfb5964e71453f50 | yes |
| 2:29.8 | said | "The hub then repeated the waiting rescue" | evidence/judge/data.json: junkRecovery.waitingRescue | {"outcome": "Rescued", "paid": "50559145400000", "requestId": "340282366920938463463374607431768211463", "target": "0xC67C2BDE920E75378eB71c9011abd3F4C1775289"} | yes |
| 2:32.3 | said | "twenty-five seconds after its debt event" | evidence/judge/data.json: latency.rescues[trigger sweep].seconds; latency.sweepByChain.base-sepolia.largestSeconds | 25 | yes |
| 2:32.4 | shown | `sweep base-sepolia 0xC67C2BDE920E75378eB71c9011abd3F4C1775289 63038646 63038773 127 25` | evidence/judge/data.json: latency.rescues[trigger sweep] | sweep base-sepolia 0xC67C2BDE920E75378eB71c9011abd3F4C1775289 63038646 63038773 127 25 | yes |
| 2:32.4 | shown | the 6 rows above the mark: a rescue that a debt event started, 5 blocks, 1 s | evidence/judge/data.json: latency.rescues[trigger debt event, base-sepolia] (6 of 8 are on the page: see the note below) | [(5, 1)] | yes |
| 2:35.7 | shown | `No guest enrolled.` | evidence/judge/data.json: guests | [] | yes |
| 2:35.7 | said | "No guest is enrolled yet" | evidence/judge/data.json: guests | [] | yes |
| 2:38.3 | said | "Lifeline restores future delivery" | README.md, section Limits (on screen) | Lifeline restores future delivery. Work that arrives while a target is in debt is lost. | yes |
| 2:41.4 | said | "Work that arrives during a debt is lost" | README.md, section Limits (on screen) | Lifeline restores future delivery. Work that arrives while a target is in debt is lost. | yes |
| 2:44.3 | said | "Testnet only" | README.md, section Demo versus production (on screen) | Demo: testnet, one owner | yes |
| 2:46.0 | said | "A public pool and automatic vault recovery are not built" | README.md, section Not built (on screen) | Public rescue pool; ...; automatic recovery of a vault that is in debt | yes |
| 2:51.7 | said | "the repository, the live board, and JUDGE.md" | README.md, line 1: the links of the title | https://gekomarchi.github.io/lifeline/board/, JUDGE.md | yes |
| 2:56.5 | said | the whole narration and each subtitle | the claim limits: no "zero loss", no "nobody else", no name of another team, no "faster" or "better" | words found: none | yes |
| 2:56.5 | said | each subtitle is a part of the narration text | demo/narration.json | 73 subtitles | yes |

## Notes

- **Pages.** A picture of a page is the public page at its URL, not changed. A value in a picture that has no row above (for example the gas price of a transaction on an explorer, or a board row of another target) is the value of that page at the time below.
- **`JUDGE.md` on github.com.** The video shows `JUDGE.md` of the branch `main`. The capture of this unit added rows to `JUDGE.md` (the captured rescue, and two more rescues). Until that file is on `main`, the page does not have these rows. Each row that the video shows is equal to `evidence/judge/data.json`; the video shows no count of rescues from that page.
- **Spoken amounts.** The narration says a rounded form ("about sixty", "about two hours", "more than a quarter"); the exact value is on screen in the same shot.

| Page | Picture made (UTC) |
|---|---|
| https://base-sepolia.blockscout.com/tx/0x428b5698f2e9ae4d1ea0fe8a1f72f03c3365c2c7b2f2b4676608a7b333ccf502 | 2026-11-24T18:25:13Z |
| https://base-sepolia.blockscout.com/tx/0x622ab7e70837931092c02e94741b66631a4ae78489d4622a8216ec21a3ab58b3 | 2026-11-24T18:25:03Z |
| https://gekomarchi.github.io/lifeline/board/#theme=dark | 2026-11-24T18:24:15Z |
| https://github.com/gekomarchi/lifeline | 2026-11-24T18:26:38Z |
| https://github.com/gekomarchi/lifeline/blob/main/JUDGE.md | 2026-11-24T18:27:23Z |
| https://lasna.reactscan.net/address/0xacF7a5E9B0e986863579f4E1bA0bd2c54FF7a427 | 2026-11-24T18:22:46Z |
| https://lasna.reactscan.net/tx/0x29df5c19147b1a94d5bbf843b2d3b3bd4a602da52db7afa9aa36452091ce2ce9 | 2026-11-24T18:24:38Z |
| https://lasna.reactscan.net/tx/0xd79e28c32f6fd3803faff2f1793acc096b873e4b4cd7e21738a7dca329dd7e26 | 2026-11-24T18:24:52Z |
