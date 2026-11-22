// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.30;

import { IReactive } from "@reactive/src/interfaces/IReactive.sol";
import { TwoForkHarness } from "./TwoForkHarness.sol";

/// @notice The cron members of the contract that emits the cron logs of Reactive Network.
/// @dev Copied from `system-smart-contracts` at `72710d2`: the events and `SYSTEM_CONTRACT_ADDR` from
///      `src/SystemContract.sol`, and `triggerCron()` from `src/omni/LegacySystemContract.sol`.
interface ICronSource {
    event Cron1000(uint256 indexed number);
    event Cron10000(uint256 indexed number);

    /// @notice Triggers the generation of the `CronFoo()` events by the system contract.
    function triggerCron() external;
}

/// @title The cron side of the node, for the two-fork harness.
/// @notice On Lasna the node calls `triggerCron()` on the cron source in each block, from its injected
///         address (for example the only transaction of block 8,910,000). The call emits `Cron1000(number)` in
///         each block with a number that is a multiple of 1,000, and `Cron10000(number)` for a multiple of
///         10,000. The node then sends each cron log to its subscribers through `trigger`, with the block of
///         the log in the log record.
///
///         This file adds three node actions and changes none of the two harnesses:
///         - `_realCron`: the real call on the code of the fork block. The next `_relay` sends the real logs
///           to the contracts with a matching subscription.
///         - `_cronRecord`: a forged cron log record, for `_trigger`.
///         - `_triggerInBlock`: a trigger transaction in the current block, for two triggers in one block.
abstract contract CronRelay is TwoForkHarness {
    /// @dev `SYSTEM_CONTRACT_ADDR` of `src/SystemContract.sol` at `72710d2`: the emitter of each cron log on
    ///      Lasna.
    address internal constant CRON_SOURCE = 0x0000000000000000000000000000000000fffFfF;

    /// @dev The gas limit of the cron transactions on Lasna.
    uint256 internal constant CRON_GAS = 800_000;

    /// @dev The topic count of a cron log: `topic0` and the indexed block number.
    uint256 internal constant CRON_TOPIC_COUNT = 2;

    /// @notice A cron log record as the node builds it: Lasna, the cron source, `topic1` and the origin
    ///         block equal to the block of the log, and the topic count as `opCode`.
    function _cronRecord(uint256 topic0, uint256 originBlock)
        internal
        pure
        returns (IReactive.LogRecord memory record)
    {
        record.chainId = LASNA_CHAIN_ID;
        record.contractAddress = CRON_SOURCE;
        record.topic0 = topic0;
        record.topic1 = originBlock;
        record.blockNumber = originBlock;
        record.opCode = CRON_TOPIC_COUNT;
    }

    /// @notice Moves Lasna to the next block with a number that is a multiple of `period`, and makes the cron
    ///         call of the node in that block. The logs are the real cron logs of the code of the fork block.
    ///         The next `_relay` or `_relayAll` sends them to their subscribers.
    /// @return cronBlock The block of the cron logs.
    function _realCron(uint256 period) internal returns (uint256 cronBlock) {
        _toLasna();
        _mine(period - vm.getBlockNumber() % period);
        cronBlock = vm.getBlockNumber();

        vm.prank(injected, injected);
        ICronSource(CRON_SOURCE).triggerCron{ gas: CRON_GAS }();
    }

    /// @notice Sends a log record through the real `trigger` in the current block. `_trigger` of the harness
    ///         mines one block before each trigger, so this function first moves Lasna one block back.
    function _triggerInBlock(address reactive, IReactive.LogRecord memory record)
        internal
        returns (TriggerResult memory)
    {
        _toLasna();
        // The logs that are not collected yet keep the current block.
        _logMark();
        vm.roll(vm.getBlockNumber() - 1);
        vm.warp(vm.getBlockTimestamp() - BLOCK_TIME);
        return _trigger(reactive, record);
    }
}
