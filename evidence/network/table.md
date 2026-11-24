# Network statistics

| Chain | Chain ID | From | To |
|---|---|---|---|
| lasna | 5318007 | 7546598 | 9140577 |
| base-sepolia | 84532 | 54538656 | 63026754 |
| unichain-sepolia | 1301 | 66977172 | 68674790 |

Parameters: `{"guestFewRuns": 3, "latencyMarginSeconds": 5, "sampleFeeWei": {"destination": "700000000000", "reactive": "12500000000000000"}, "windowStart": "2026-11-05T00:00:00+02:00", "windowStartTimestamp": 1793829600}`

| Query | Number | Value | Chain | From | To |
|---|---|---|---|---|---|
| `lasna_debt_events` | debt events | 424 | lasna | 7546598 | 9140577 |
| `destination_debt_events` | debt events | 52 | base-sepolia | 54538656 | 63026754 |
| `destination_debt_events` | debt events | 14 | unichain-sepolia | 66977172 | 68674790 |
| `destination_debt_events` | debt events, all destination chains | 66 | base-sepolia<br>unichain-sepolia | 54538656<br>66977172 | 63026754<br>68674790 |
| `stopped_contracts` | stopped contracts | 231 | lasna | 7546598 | 9140577 |
| `stopped_contracts` | cohort: contracts with a debt event | 306 | lasna | 7546598 | 9140577 |
| `stopped_contracts` | stopped contracts | 28 | base-sepolia | 54538656 | 63026754 |
| `stopped_contracts` | cohort: contracts with a debt event | 49 | base-sepolia | 54538656 | 63026754 |
| `stopped_contracts` | stopped contracts | 11 | unichain-sepolia | 66977172 | 68674790 |
| `stopped_contracts` | cohort: contracts with a debt event | 14 | unichain-sepolia | 66977172 | 68674790 |
| `cron_stopped_share` | cron-subscribed contracts | 278 | lasna | 7546598 | 9140577 |
| `cron_stopped_share` | cron-subscribed contracts, stopped | 115 | lasna | 7546598 | 9140577 |
| `cron_stopped_share` | cron-subscribed contracts, stopped share | 0.4137 | lasna | 7546598 | 9140577 |
| `cron_stopped_share` | other subscribed contracts | 647 | lasna | 7546598 | 9140577 |
| `cron_stopped_share` | other subscribed contracts, stopped | 78 | lasna | 7546598 | 9140577 |
| `cron_stopped_share` | other subscribed contracts, stopped share | 0.1206 | lasna | 7546598 | 9140577 |
| `repeat_debtors` | contracts with 3 or more debt events | 19 | lasna | 7546598 | 9140577 |
| `repeat_debtors` | largest number of debt events of one contract | 15 | lasna | 7546598 | 9140577 |
| `repeat_debtors` | contracts with 3 or more debt events | 1 | base-sepolia | 54538656 | 63026754 |
| `repeat_debtors` | largest number of debt events of one contract | 4 | base-sepolia | 54538656 | 63026754 |
| `repeat_debtors` | contracts with 3 or more debt events | 0 | unichain-sepolia | 66977172 | 68674790 |
| `repeat_debtors` | largest number of debt events of one contract | 1 | unichain-sepolia | 66977172 | 68674790 |
| `dropped_callbacks` | dropped callbacks (derived from debt intervals and InDebt) | 159 | lasna<br>base-sepolia<br>unichain-sepolia | 7546598<br>54538656<br>66977172 | 9140577<br>63026754<br>68674790 |
| `dropped_callbacks` | dropped callbacks | 82 | lasna<br>base-sepolia | 7546598<br>54538656 | 9140577<br>63026754 |
| `dropped_callbacks` | dropped callbacks | 77 | lasna<br>unichain-sepolia | 7546598<br>66977172 | 9140577<br>68674790 |
| `dropped_callbacks` | smallest delay from debt event to request, seconds | 6 | lasna<br>base-sepolia<br>unichain-sepolia | 7546598<br>54538656<br>66977172 | 9140577<br>63026754<br>68674790 |
| `dropped_callbacks` | requests within 60 s of the debt event | 21 | lasna<br>base-sepolia<br>unichain-sepolia | 7546598<br>54538656<br>66977172 | 9140577<br>63026754<br>68674790 |
| `dropped_callbacks` | median delay, seconds | 5057 | lasna<br>base-sepolia<br>unichain-sepolia | 7546598<br>54538656<br>66977172 | 9140577<br>63026754<br>68674790 |
| `dropped_callbacks` | requests sooner than 10800 s after the debt event | 127 | lasna<br>base-sepolia<br>unichain-sepolia | 7546598<br>54538656<br>66977172 | 9140577<br>63026754<br>68674790 |
| `dropped_callbacks` | uncertain: within 5 s of an interval edge or of the end of the window | 73 | lasna<br>base-sepolia<br>unichain-sepolia | 7546598<br>54538656<br>66977172 | 9140577<br>63026754<br>68674790 |
| `dropped_callbacks` | not scanned: destination chain is not configured | 1677 | lasna | 7546598 | 9140577 |
| `dropped_callbacks` | difference from the earlier figure 206 | -47 | lasna<br>base-sepolia<br>unichain-sepolia | 7546598<br>54538656<br>66977172 | 9140577<br>63026754<br>68674790 |
| `fastfill_fill_times` | fills | 38 | base-sepolia<br>unichain-sepolia | 54538656<br>66977172 | 63026754<br>68674790 |
| `fastfill_fill_times` | deposit to fill, median seconds | 2 | base-sepolia<br>unichain-sepolia | 54538656<br>66977172 | 63026754<br>68674790 |
| `fastfill_fill_times` | deposit to fill, 90th percentile seconds | 3 | base-sepolia<br>unichain-sepolia | 54538656<br>66977172 | 63026754<br>68674790 |
| `fastfill_fill_times` | deposit to fill, maximum seconds | 5 | base-sepolia<br>unichain-sepolia | 54538656<br>66977172 | 63026754<br>68674790 |
| `cron_poller_ward_dead_time` | longest debt interval of the ward, seconds | 7369 | lasna | 7546598 | 9140577 |
| `cron_poller_ward_dead_time` | longest debt interval of the ward, blocks | 6978 | lasna | 7546598 | 9140577 |
| `fastfill_reactive_reserves` | reserves of FastfillReactive, wei | 60373758200000000000 | lasna | 7546598 | 9140577 |
| `buildathon_debt` | buildathon submissions with a contract on the chain | 75 | lasna | 7546598 | 9140577 |
| `buildathon_debt` | buildathon submissions with a contract in debt at to | 22 | lasna | 7546598 | 9140577 |
| `buildathon_debt` | buildathon contracts in debt at to | 26 | lasna | 7546598 | 9140577 |
| `buildathon_debt` | buildathon submissions with a contract in debt at to, track Every Second Counts | 9 of 30 | lasna | 7546598 | 9140577 |
| `buildathon_debt` | buildathon submissions with a contract in debt at to, track Reactive Meets AI | 3 of 14 | lasna | 7546598 | 9140577 |
| `buildathon_debt` | buildathon submissions with a contract in debt at to, track Set It and Forget It | 10 of 31 | lasna | 7546598 | 9140577 |
| `guest_candidates` | guest candidates | 818 | lasna | 7546598 | 9140577 |
| `guest_candidates` | guest candidates with a debt event or debt at to | 306 | lasna | 7546598 | 9140577 |
| `guest_candidates` | guest candidates with low reserve only | 512 | lasna | 7546598 | 9140577 |
| `guest_candidates` | guest candidates | 392 | base-sepolia | 54538656 | 63026754 |
| `guest_candidates` | guest candidates with a debt event or debt at to | 49 | base-sepolia | 54538656 | 63026754 |
| `guest_candidates` | guest candidates with low reserve only | 343 | base-sepolia | 54538656 | 63026754 |
| `guest_candidates` | guest candidates | 181 | unichain-sepolia | 66977172 | 68674790 |
| `guest_candidates` | guest candidates with a debt event or debt at to | 14 | unichain-sepolia | 66977172 | 68674790 |
| `guest_candidates` | guest candidates with low reserve only | 167 | unichain-sepolia | 66977172 | 68674790 |

## Definitions

- `lasna_debt_events`: A debt event is a BlacklistContract log of the Lasna system contract in from..to; the ledger emits it only when a debt goes from zero to positive.
- `destination_debt_events`: A debt event is a BlacklistContract log of the callback proxy of a configured destination chain in from..to.
- `stopped_contracts`: A contract is stopped if its ledger emitted BlacklistContract for it in from..to and debt(contract) is above zero at to; the cohort is each contract with a debt event in from..to.
- `cron_stopped_share`: Among the contracts with a Subscribe log on Lasna in from..to, a contract is cron-subscribed if one of these logs has a cron topic (Cron1 to Cron10000) as topic0, and a contract is stopped if debt(contract) is above zero at to.
- `repeat_debtors`: A repeat debtor is a contract with 3 or more debt events on one chain in from..to.
- `dropped_callbacks`: A CallbackRequest log on Lasna is counted as dropped if, at the block timestamp of the request, its recipient has an open debt interval on the destination proxy (from BlacklistContract(recipient) to the next WhitelistContract(recipient), or to `to`); the number is derived from debt intervals and the proxy rule (InDebt), not from observed reverted transactions, and a request within the latency margin of an interval edge, or of the end of the destination window, is counted as uncertain.
- `fastfill_fill_times`: A fill is a fill log whose join field is equal to the join field of a deposit log in from..to; its time is the fill block timestamp minus the deposit block timestamp, and the 90th percentile is the nearest-rank value.
- `cron_poller_ward_dead_time`: The dead time is the longest debt interval of the ward in from..to: from BlacklistContract(ward), or from `from` if the debt was open then, to the next WhitelistContract(ward), or to `to`.
- `fastfill_reactive_reserves`: The value is reserves(FastfillReactive) on the ledger of its chain at to.
- `buildathon_debt`: A buildathon submission is an entry of subjects.buildathon.submissions: a project on the public list of the Reactive Omni Buildathon, with the contracts on the reactive chain that its public repository names. A submission is in debt if debt(contract) on the ledger is above zero at to for one of these contracts or more. A contract that a repository names can be an old deployment.
- `guest_candidates`: A guest candidate is a contract seen in the scan (a debtor, a subscriber on Lasna, or a callback recipient on a destination chain) that has a debt event in from..to, has debt above zero at to, or holds reserves below a few runs of the sample fee at to.
