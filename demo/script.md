# Demo video: shot list and narration

This file is written by `python demo/compose.py` from `demo/shots.py`, `demo/narration.json` and the footage. Do not edit it by hand: change the sources and build the cut again.

Length of this cut: 175.2 s (the limit is less than 180 s).

## Voice

- Engine: ElevenLabs, text to speech with the time of each character.
- Voice: Daniel (voice ID `onwK4e9ZLuTAKqWW03F9`), model `eleven_v4`, seed 11.
- Settings, the same for each clip: `stability` 0.6, `similarity_boost` 0.8, `style` 0.0, `use_speaker_boost` True; output `mp3_44100_192`.
- Each clip gets the text of the clip before it and after it as context (`previous_text`, `next_text`).

## Spoken forms

A number is written in words in the narration text. A term has one spoken form:

| Term | As sent to the voice engine |
|---|---|
| ElevenLabs | Eleven Labs |
| Lasna | Lasna |
| lREACT | L-React |
| Reactive Network | Reactive Network |
| Reactscan | React-scan |
| Unichain Sepolia | Unichain Sepolia |
| Base Sepolia | Base Sepolia |
| depositTo | deposit |
| vaultUp | vault-up |
| callback proxy | callback proxy |
| JUDGE.md | judge dot M D |

`depositTo`, `vaultUp` and `callback proxy` are on screen and are not spoken in this cut.

## Shots

| Start | Duration | Part | Surface (URL or command) | On-screen action | Narration | Text as sent to the voice engine, if it differs |
|---|---|---|---|---|---|---|
| 0.0 s | 15.1 s | 2. The problem | https://lasna.reactscan.net/address/0xacF7a5E9B0e986863579f4E1bA0bd2c54FF7a427 | The page of the arm A canary on Reactscan, from its top. Zoom in to the reserves and the debt of the contract. Zoom out to the list of its transactions; a mark on the status column, then on the counts of transactions and of reactive executions. | I do not speak English, so this narration was generated with ElevenLabs. This reactive contract ran out of gas credit. Each transaction to it still shows success. But the network gives it no more work. | I do not speak English, so this narration was generated with Eleven Labs. |
| 15.1 s | 10.7 s | 2. The problem | https://github.com/gekomarchi/lifeline | README on github.com, section Problem. A mark on the line about the buildathon, then on the line about the builder's own automation. | In this buildathon, more than a quarter of the submissions have a contract in debt. My own automation holds about sixty lREACT that cannot be withdrawn. | My own automation holds about sixty L-React that cannot be withdrawn. |
| 25.8 s | 3.2 s | 2. The problem | https://gekomarchi.github.io/lifeline/board/#theme=dark | The board, panel Targets. A mark on the reserves and the marks of FastfillReactive. | Over-funding was the only protection. | - |
| 29.0 s | 3.7 s | 3. What Lifeline is | https://github.com/gekomarchi/lifeline | README on github.com, the title and the first paragraph. | Lifeline is a gas guardian that I set once. | - |
| 32.7 s | 11.2 s | 3. What Lifeline is | https://github.com/gekomarchi/lifeline | README, section How it works: the sequence diagram. First the airbag half; the picture then moves down to the gauge half. | Its airbag listens to the network's own debt event, pays the debt, and refills the contract. Its gauge refills a contract on a cron tick, before it runs out. | - |
| 43.9 s | 9.5 s | 4. Set it once | https://github.com/gekomarchi/lifeline | README, section Operator guide: the `enroll` command with its policy flags. | Enrollment is one command. The policy is plain numbers: two refill marks, a daily budget, and a limit of rescues. | - |
| 53.4 s | 2.6 s | 4. Set it once | https://gekomarchi.github.io/lifeline/board/#theme=dark | The board, panel Targets. A mark on the two fleet targets, FastfillReactive and FastfillExecutor, with their state and their marks. | Two of my automations are enrolled, | - |
| 56.0 s | 2.8 s | 4. Set it once | https://lasna.reactscan.net/tx/0x29df5c19147b1a94d5bbf843b2d3b3bd4a602da52db7afa9aa36452091ce2ce9 | The `hub.enroll` transaction of FastfillExecutor on Reactscan. A mark on its input: the chain, the target and the policy. | each with a transaction you can check. | - |
| 58.8 s | 24.6 s | 5. A live rescue | Terminal: `python -m lifeline.demo capture` | The terminal recording of `python -m lifeline.demo capture` on testnet. The command is typed. The waits between the `poke()` calls are cut (label: waits cut). From before the last `poke()` to `debt()` 0 the recording runs at real speed with no cut (label: real speed, no cut): the four steps appear with their block numbers. The screen then stays, and a mark goes to each step when the narration names it. | Now a live rescue on testnet. This command drives a test automation until its callback contract on Base Sepolia runs out. Now watch, at real speed, with no cut. The debt event. The hub's reaction on Lasna. The vault's payment in a callback. The debt is zero. Nobody sent a transaction after the first step. | - |
| 83.4 s | 5.5 s | 5. A live rescue | https://lasna.reactscan.net/tx/0xd79e28c32f6fd3803faff2f1793acc096b873e4b4cd7e21738a7dca329dd7e26 | The `Relayed` transaction of the captured rescue on Reactscan: a mark on its block number. The picture moves down to its callback; a mark on the destination transaction. | Here is that reaction on Reactscan, with its callback to Base Sepolia. | Here is that reaction on React-scan, with its callback to Base Sepolia. |
| 88.9 s | 2.9 s | 5. A live rescue | https://base-sepolia.blockscout.com/tx/0x622ab7e70837931092c02e94741b66631a4ae78489d4622a8216ec21a3ab58b3 | The transaction of the debt event on Blockscout (Base Sepolia). A mark on its timestamp. | The debt event has this timestamp. | - |
| 91.8 s | 3.1 s | 5. A live rescue | https://base-sepolia.blockscout.com/tx/0x428b5698f2e9ae4d1ea0fe8a1f72f03c3365c2c7b2f2b4676608a7b333ccf502 | The transaction of the vault `Result` on Blockscout, in the same framing. A mark on its timestamp. | The payment came one second later. | - |
| 94.8 s | 30.9 s | 6. The difference | https://github.com/gekomarchi/lifeline | README, section Lifeline against the alternatives: the three-arm table, with a mark on the row of the delivered callbacks. The picture moves down to the notes (a mark on arm B), then to the sentence about the wallet refiller and the comparison table (a mark on the row "Restarts a stopped reactive contract"). | Three identical automations got the same load. With no guardian, three callbacks arrived. With a cron poller, also three. With Lifeline, all twenty-four. The poller left its contract dead for about two hours. Lifeline is not a wallet refiller. A refiller sends currency to an account. Lifeline pays a contract's debt at the gas ledger, and only that restarts a stopped contract. | - |
| 125.8 s | 9.7 s | 7. When things break | https://gekomarchi.github.io/lifeline/board/#theme=dark | The board, panel Targets. The cursor clicks the state filter Quarantined: one target stays, the looping canary. The cursor clicks Details: a mark on the quarantine rule, then on the state of the target. | A target that breaks is cut off. After two rescues, the hub quarantined this looping test contract, and paid nothing more. | - |
| 135.5 s | 9.2 s | 7. When things break | https://github.com/gekomarchi/lifeline/blob/main/JUDGE.md | JUDGE.md on github.com, section Junk flood. Marks: the count of failed callbacks, the reserves of the vault at the end, the self-care sum, the balance change that the payments do not explain. | A flood of junk callbacks pushed one vault offline. It lost its small fee float and a little self-care, and nothing else. | - |
| 144.6 s | 6.0 s | 7. When things break | https://github.com/gekomarchi/lifeline/blob/main/JUDGE.md | JUDGE.md, section Junk recovery: the steps. Marks: the deposit and `VaultUp` of the owner, then the `Result` of the target that waited. | Recovery needed two transactions from the owner. The hub then repeated the waiting rescue, | - |
| 150.6 s | 3.4 s | 7. When things break | https://github.com/gekomarchi/lifeline/blob/main/JUDGE.md | JUDGE.md, section Rescue latency: the last rows of the table of each rescue. A mark on the rescue that waited for the sweep: 127 blocks, 25 seconds. Each row above it is a rescue that a debt event started: 5 blocks, 1 second. | twenty-five seconds after its debt event. | - |
| 154.0 s | 2.6 s | 8. Guests | https://github.com/gekomarchi/lifeline/blob/main/JUDGE.md | JUDGE.md, section Guests. | No guest is enrolled yet. | - |
| 156.7 s | 12.0 s | 9. Limits | https://github.com/gekomarchi/lifeline | README, section Limits: the first limit. The picture moves down to Not built and Demo versus production. | Lifeline restores future delivery. Work that arrives during a debt is lost. Testnet only. A public pool and automatic vault recovery are not built. | - |
| 168.7 s | 6.5 s | 10. Where to check | https://github.com/gekomarchi/lifeline | README on github.com, the title with the links to the live board and to JUDGE.md. | Check it yourself: the repository, the live board, and JUDGE.md. | Check it yourself: the repository, the live board, and judge dot M D. |

The first sentence (part 1) is spoken over the first shot of part 2.
