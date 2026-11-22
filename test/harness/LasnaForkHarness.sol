// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.30;

import { Test, Vm } from "forge-std/Test.sol";
import { IReactive } from "@reactive/src/interfaces/IReactive.sol";
import { IProxyLedger } from "../../src/interfaces/IProxyLedger.sol";
import { ISystemContract, REACTIVE_IGNORE } from "../../src/interfaces/ISystemContract.sol";

/// @notice The members of the system contract that only the node uses.
/// @dev Copied from `system-smart-contracts` at `72710d2`, `src/omni/SystemContract.sol`.
interface ISystemContractNode {
    /// @notice Indicates that the call to `react()` reverted when processing a log record.
    event ReactiveContractReverted(address indexed reactive_, IReactive.LogRecord log_, bytes error_);

    /// @notice Indicates that the method may only be called through node-injected transactions.
    error OnlyInjected();

    /// @notice Fixed address for node-injected transactions.
    // forge-lint: disable-next-line(mixed-case-function)
    function INJ_ADDR() external view returns (address);

    /// @notice Proxy method for calling `react()` methods on reactive contracts.
    function trigger(IReactive contract_, IReactive.LogRecord calldata log_) external;
}

/// @title Fork harness for Reactive Network testnet (Lasna).
/// @notice The harness forks Lasna at a pinned block, so the system contract is the real deployed code.
///         It emulates only the node: it keeps a table of the subscriptions that were made on the fork, and
///         it relays each new log to each contract with a matching active subscription through the real
///         `trigger`, from the node's injected address.
///
///         Rules of the emulation:
///         - One trigger is one transaction of 900,000 gas in its own block. The intrinsic gas of that
///           transaction is taken from the 900,000, as it is for a transaction on the chain.
///         - A contract that is in debt is not triggered.
///         - A subscription matches the logs between its `Subscribe` log and its `Unsubscribe` log. It
///           does not match a log that was emitted before it.
///         - Only subscriptions made after the fork point are known.
///
///         A test must use `_mine` and `_skipTime` to move the block number and the time. A direct
///         `vm.roll` gives a wrong origin block to the logs that are not collected yet.
abstract contract LasnaForkHarness is Test {
    // ------------------------------------------------------------------------------------------
    // Pinned values
    // ------------------------------------------------------------------------------------------

    uint256 internal constant LASNA_CHAIN_ID = 5318007;

    /// @dev The block of the fork rehearsal.
    uint256 internal constant LASNA_FORK_BLOCK = 8_918_212;

    address internal constant SYSTEM_ADDRESS = 0x8888888888888888888888888888888888888888;

    /// @dev `INJ_ADDR` of `SystemContract.sol` at `72710d2`. The harness reads the value from the chain and
    ///      compares it with this one.
    address internal constant INJECTED_ADDRESS_AT_SOURCE = 0x038E06667e42782E571EaB20432b9237F9bD6B82;

    /// @dev The gas limit of a trigger transaction on Lasna.
    uint256 internal constant TRIGGER_GAS = 900_000;

    /// @dev The gas price of the trigger transactions on Lasna and the base fee of the pinned block.
    uint256 internal constant GAS_PRICE = 100 gwei;

    /// @dev Seconds for each block.
    uint256 internal constant BLOCK_TIME = 1;

    /// @dev Stops a relay that does not end.
    uint256 internal constant MAX_TRIGGERS_PER_RELAY = 64;

    // ------------------------------------------------------------------------------------------
    // Types and state
    // ------------------------------------------------------------------------------------------

    /// @notice One log that the harness collected, with the block that it was emitted in.
    struct ChainLog {
        address emitter;
        uint256 topicCount;
        uint256[4] topics;
        bytes data;
        uint256 blockNumber;
    }

    /// @notice One subscription that was made on the fork. It is active for the logs between the
    ///         `Subscribe` log and the `Unsubscribe` log, in the order of the log history.
    struct Subscription {
        address subscriber;
        uint256 chainId;
        address emitter;
        uint256[4] topics;
        uint256 fromLog; // the index of the `Subscribe` log
        uint256 toLog; // the index of the `Unsubscribe` log; the largest value while there is none
    }

    /// @notice The result of one trigger transaction.
    struct TriggerResult {
        address reactive; // the contract that was triggered
        bool success; // the trigger transaction did not revert
        bool reactReverted; // the system contract emitted `ReactiveContractReverted`
        uint256 fee; // the fee that the system contract charged: the kickback to the transaction origin
        uint256 gasUsed; // the gas of the transaction, with the intrinsic gas
        uint256 blockNumber; // the block of the transaction
        uint256 firstLog; // the index in the log history of the first log of the transaction
    }

    ISystemContract internal system = ISystemContract(SYSTEM_ADDRESS);

    /// @dev The node's injected address, read from the chain.
    address internal injected;

    ChainLog[] private _logs;
    uint256 private _nextLogToRelay;
    Subscription[] private _subscriptions;
    TriggerResult[] private _triggers;

    // ------------------------------------------------------------------------------------------
    // Setup
    // ------------------------------------------------------------------------------------------

    /// @dev Forks Lasna at the pinned block. Call it first in `setUp`.
    function _forkLasna() internal {
        // The endpoint `lasna` of `foundry.toml` is the environment variable `LASNA_RPC_URL`.
        vm.createSelectFork("lasna", LASNA_FORK_BLOCK);
        assertEq(block.chainid, LASNA_CHAIN_ID, "the RPC endpoint is not Lasna");
        assertGt(SYSTEM_ADDRESS.code.length, 0, "no system contract at the pinned block");

        injected = ISystemContractNode(SYSTEM_ADDRESS).INJ_ADDR();
        assertEq(injected, INJECTED_ADDRESS_AT_SOURCE, "the injected address differs from the pinned source");

        vm.fee(GAS_PRICE);
        vm.txGasPrice(GAS_PRICE);
        vm.recordLogs();
    }

    /// @dev Returns an address that has no balance, no nonce, no code and no ledger entry on the fork.
    ///      Default test accounts can hold real testnet funds on a fork.
    function _freshAccount(string memory label) internal returns (address account) {
        account = makeAddr(string.concat("lifeline.harness.", label));
        _assertFresh(account);
    }

    /// @dev Fails if the address was used on the forked chain.
    function _assertFresh(address account) internal view {
        assertEq(account.balance, 0, "the address has a balance on the fork");
        assertEq(account.code.length, 0, "the address has code on the fork");
        assertEq(vm.getNonce(account), 0, "the address has a nonce on the fork");
        assertEq(system.reserves(account), 0, "the address has reserves on the fork");
        assertEq(system.debt(account), 0, "the address has a debt on the fork");
    }

    /// @dev The address of the next contract that `deployer` creates. Fails if that address is not fresh.
    function _nextFreshContract(address deployer) internal view returns (address created) {
        created = vm.computeCreateAddress(deployer, vm.getNonce(deployer));
        _assertFresh(created);
    }

    // ------------------------------------------------------------------------------------------
    // Blocks and time
    // ------------------------------------------------------------------------------------------

    /// @dev Moves `blocks` blocks forward. Logs that are not collected yet keep the old block.
    function _mine(uint256 blocks) internal {
        _skipTime(blocks * BLOCK_TIME);
    }

    /// @dev Moves `secs` seconds forward, with the matching number of blocks.
    function _skipTime(uint256 secs) internal {
        _collect();
        vm.roll(vm.getBlockNumber() + (secs + BLOCK_TIME - 1) / BLOCK_TIME);
        vm.warp(vm.getBlockTimestamp() + secs);
    }

    // ------------------------------------------------------------------------------------------
    // Relay
    // ------------------------------------------------------------------------------------------

    /// @notice Relays each log that is not relayed yet, and each log that those triggers emit, until no
    ///         log is left.
    /// @return triggers The number of trigger transactions that ran.
    function _relay() internal returns (uint256 triggers) {
        _collect();
        while (_nextLogToRelay < _logs.length) {
            uint256 index = _nextLogToRelay++;
            address[] memory subscribers = _matchingSubscribers(index);
            for (uint256 i = 0; i < subscribers.length; ++i) {
                // The node does not trigger a contract that is in debt.
                if (system.debt(subscribers[i]) != 0) continue;
                require(triggers < MAX_TRIGGERS_PER_RELAY, "LasnaForkHarness: the relay does not end");
                _trigger(subscribers[i], _logRecord(index));
                ++triggers;
            }
        }
    }

    /// @dev The log record that the node builds from a log on Lasna. `opCode` is the topic count, as in
    ///      the trigger transactions on the chain. The two hashes and the log index are 0: Lifeline does not
    ///      read them.
    function _logRecord(uint256 logIndex) internal view returns (IReactive.LogRecord memory record) {
        ChainLog storage log = _logs[logIndex];
        record = IReactive.LogRecord({
            chainId: LASNA_CHAIN_ID,
            contractAddress: log.emitter,
            topic0: log.topics[0],
            topic1: log.topics[1],
            topic2: log.topics[2],
            topic3: log.topics[3],
            data: log.data,
            blockNumber: log.blockNumber,
            opCode: log.topicCount,
            blockHash: 0,
            txHash: 0,
            logIndex: 0
        });
    }

    /// @dev A debt log record of a ledger: `BlacklistContract(target)`, with two topics.
    function _debtLogRecord(uint256 chainId, address emitter, address target, uint256 originBlock)
        internal
        pure
        returns (IReactive.LogRecord memory record)
    {
        record.chainId = chainId;
        record.contractAddress = emitter;
        record.topic0 = uint256(IProxyLedger.BlacklistContract.selector);
        record.topic1 = uint256(uint160(target));
        record.blockNumber = originBlock;
        record.opCode = 2;
    }

    /// @notice Sends a log record through the real `trigger`, from the injected address. The record can be
    ///         forged: no subscription is needed. Logs of the transaction are relayed by the next `_relay`.
    function _trigger(address reactive, IReactive.LogRecord memory record)
        internal
        returns (TriggerResult memory result)
    {
        _mine(1);
        bytes memory callData = abi.encodeCall(ISystemContractNode.trigger, (IReactive(payable(reactive)), record));
        uint256 intrinsic = _intrinsicGas(callData);
        uint256 kickbackBefore = injected.balance;

        vm.prank(injected, injected);
        uint256 gasBefore = gasleft();
        (bool success,) = SYSTEM_ADDRESS.call{ gas: TRIGGER_GAS - intrinsic }(callData);
        uint256 gasAfter = gasleft();

        result.reactive = reactive;
        result.success = success;
        result.fee = injected.balance - kickbackBefore;
        result.gasUsed = intrinsic + gasBefore - gasAfter;
        result.blockNumber = vm.getBlockNumber();
        result.firstLog = _logs.length;
        _collect();
        result.reactReverted =
            _countLogs(result.firstLog, SYSTEM_ADDRESS, ISystemContractNode.ReactiveContractReverted.selector) != 0;
        _triggers.push(result);
    }

    /// @dev The intrinsic gas of a transaction with this calldata.
    function _intrinsicGas(bytes memory callData) private pure returns (uint256 gas) {
        gas = 21_000;
        for (uint256 i = 0; i < callData.length; ++i) {
            gas += callData[i] == 0 ? 4 : 16;
        }
    }

    /// @dev Moves the logs that the EVM recorded into the log history, with the current block.
    function _collect() private {
        Vm.Log[] memory recorded = vm.getRecordedLogs();
        if (recorded.length == 0) return;

        uint256 blockNumber = vm.getBlockNumber();
        for (uint256 i = 0; i < recorded.length; ++i) {
            ChainLog storage log = _logs.push();
            log.emitter = recorded[i].emitter;
            log.topicCount = recorded[i].topics.length;
            for (uint256 j = 0; j < recorded[i].topics.length; ++j) {
                log.topics[j] = uint256(recorded[i].topics[j]);
            }
            log.data = recorded[i].data;
            log.blockNumber = blockNumber;
            _updateSubscriptions(_logs.length - 1);
        }
    }

    // ------------------------------------------------------------------------------------------
    // Subscriptions
    // ------------------------------------------------------------------------------------------

    /// @dev Applies a `Subscribe` or `Unsubscribe` log of the system contract to the table.
    function _updateSubscriptions(uint256 logIndex) private {
        ChainLog storage log = _logs[logIndex];
        if (log.emitter != SYSTEM_ADDRESS || log.topicCount != 4) return;

        bool subscribe = log.topics[0] == uint256(ISystemContract.Subscribe.selector);
        if (!subscribe && log.topics[0] != uint256(ISystemContract.Unsubscribe.selector)) return;

        Subscription memory sub;
        sub.subscriber = address(uint160(log.topics[1]));
        sub.chainId = log.topics[2];
        sub.emitter = address(uint160(log.topics[3]));
        (sub.topics[0], sub.topics[1], sub.topics[2], sub.topics[3]) =
            abi.decode(log.data, (uint256, uint256, uint256, uint256));

        // A second `Subscribe` with the same six fields changes nothing. An `Unsubscribe` ends the active one.
        for (uint256 i = 0; i < _subscriptions.length; ++i) {
            if (_subscriptions[i].toLog == type(uint256).max && _sameSubscription(_subscriptions[i], sub)) {
                if (!subscribe) _subscriptions[i].toLog = logIndex;
                return;
            }
        }
        if (subscribe) {
            sub.fromLog = logIndex;
            sub.toLog = type(uint256).max;
            _subscriptions.push(sub);
        }
    }

    function _sameSubscription(Subscription storage a, Subscription memory b) private view returns (bool) {
        return a.subscriber == b.subscriber && a.chainId == b.chainId && a.emitter == b.emitter
            && a.topics[0] == b.topics[0] && a.topics[1] == b.topics[1] && a.topics[2] == b.topics[2]
            && a.topics[3] == b.topics[3];
    }

    /// @dev The contracts with a subscription that was active at the log and matches it. Each contract is
    ///      listed once.
    function _matchingSubscribers(uint256 logIndex) private view returns (address[] memory subscribers) {
        ChainLog storage log = _logs[logIndex];
        address[] memory found = new address[](_subscriptions.length);
        uint256 count;
        for (uint256 i = 0; i < _subscriptions.length; ++i) {
            Subscription storage sub = _subscriptions[i];
            if (sub.fromLog >= logIndex || sub.toLog <= logIndex || !_matches(sub, log)) continue;
            bool listed;
            for (uint256 j = 0; j < count; ++j) {
                if (found[j] == sub.subscriber) listed = true;
            }
            if (!listed) found[count++] = sub.subscriber;
        }
        subscribers = new address[](count);
        for (uint256 i = 0; i < count; ++i) {
            subscribers[i] = found[i];
        }
    }

    /// @dev Chain ID 0 and emitter 0 match all. `REACTIVE_IGNORE` matches each topic value and a missing topic.
    function _matches(Subscription storage sub, ChainLog storage log) private view returns (bool) {
        if (sub.chainId != 0 && sub.chainId != LASNA_CHAIN_ID) return false;
        if (sub.emitter != address(0) && sub.emitter != log.emitter) return false;
        for (uint256 i = 0; i < 4; ++i) {
            if (sub.topics[i] == REACTIVE_IGNORE) continue;
            if (i >= log.topicCount || sub.topics[i] != log.topics[i]) return false;
        }
        return true;
    }

    /// @notice The number of active subscriptions of a contract.
    function _activeSubscriptions(address subscriber) internal returns (uint256 count) {
        _collect();
        for (uint256 i = 0; i < _subscriptions.length; ++i) {
            if (_subscriptions[i].toLog == type(uint256).max && _subscriptions[i].subscriber == subscriber) ++count;
        }
    }

    // ------------------------------------------------------------------------------------------
    // Log history
    // ------------------------------------------------------------------------------------------

    /// @notice The number of logs in the history. Use it as a mark before an action.
    function _logMark() internal returns (uint256) {
        _collect();
        return _logs.length;
    }

    /// @notice The number of logs of one emitter and one `topic0`, from a mark to the end of the history.
    function _countLogs(uint256 from, address emitter, bytes32 topic0) internal returns (uint256 count) {
        _collect();
        for (uint256 i = from; i < _logs.length; ++i) {
            if (_logs[i].emitter == emitter && _logs[i].topicCount != 0 && _logs[i].topics[0] == uint256(topic0)) {
                ++count;
            }
        }
    }

    /// @notice The index of the `n`-th log (from 0) of one emitter and one `topic0` after a mark. Fails if
    ///         there is none.
    function _findLogIndex(uint256 from, address emitter, bytes32 topic0, uint256 n) internal returns (uint256) {
        _collect();
        for (uint256 i = from; i < _logs.length; ++i) {
            if (_logs[i].emitter == emitter && _logs[i].topicCount != 0 && _logs[i].topics[0] == uint256(topic0)) {
                if (n == 0) return i;
                --n;
            }
        }
        fail("LasnaForkHarness: the log was not found");
        return 0;
    }

    /// @notice The `n`-th log (from 0) of one emitter and one `topic0` after a mark. Fails if there is none.
    function _findLog(uint256 from, address emitter, bytes32 topic0, uint256 n) internal returns (ChainLog memory) {
        return _logs[_findLogIndex(from, emitter, topic0, n)];
    }

    /// @notice The one log of one emitter and one `topic0` after a mark. Fails if there is none or more than one.
    function _onlyLog(uint256 from, address emitter, bytes32 topic0) internal returns (ChainLog memory) {
        assertEq(_countLogs(from, emitter, topic0), 1, "LasnaForkHarness: not exactly one log");
        return _findLog(from, emitter, topic0, 0);
    }

    /// @notice The number of logs of one emitter from a mark to the end of the history.
    function _countLogsOf(uint256 from, address emitter) internal returns (uint256 count) {
        _collect();
        for (uint256 i = from; i < _logs.length; ++i) {
            if (_logs[i].emitter == emitter) ++count;
        }
    }

    // ------------------------------------------------------------------------------------------
    // Trigger history
    // ------------------------------------------------------------------------------------------

    /// @notice The newest trigger of a contract. Fails if there is none.
    function _lastTrigger(address reactive) internal returns (TriggerResult memory result) {
        for (uint256 i = _triggers.length; i != 0; --i) {
            if (_triggers[i - 1].reactive == reactive) return _triggers[i - 1];
        }
        fail("LasnaForkHarness: the contract was not triggered");
        return result;
    }
}
