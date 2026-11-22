// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.30;

/// @notice Rescue policy of one enrolled target. Amounts are in wei of the target's chain.
struct Policy {
    uint256 low; // refill when reserves are below this mark (wei of the target's chain)
    uint256 high; // reserves after a payment
    uint256 maxDebt; // a debt above this is skipped
    uint256 dailyCap; // principal paid to this target for each calendar day
    uint32 maxRescues; // rescues permitted inside window
    uint32 window; // seconds
}

/// @dev Largest `maxRescues`: the sliding window holds four timestamps in one storage slot.
uint32 constant MAX_RESCUES_LIMIT = 4;

/// @notice The policy fails a rule of `validatePolicy`.
error InvalidPolicy();

/// @notice Reverts `InvalidPolicy` unless: `high > 0`, `low <= high`, `maxDebt > 0`, `dailyCap > 0`,
///         `1 <= maxRescues <= MAX_RESCUES_LIMIT`, `window > 0`.
function validatePolicy(Policy memory p) pure {
    if (
        p.high == 0 || p.low > p.high || p.maxDebt == 0 || p.dailyCap == 0 || p.maxRescues == 0
            || p.maxRescues > MAX_RESCUES_LIMIT || p.window == 0
    ) {
        revert InvalidPolicy();
    }
}

/// @notice The chain type of an enrolled target.
enum TargetKind {
    None,
    Lasna,
    Destination
}

/// @notice The state of an enrolled target.
enum TargetState {
    None,
    Active,
    Quarantined,
    Parked,
    Disarmed
}
