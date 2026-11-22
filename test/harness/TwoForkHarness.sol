// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.30;

import { Vm } from "forge-std/Vm.sol";
import { IReactive } from "@reactive/src/interfaces/IReactive.sol";
import { ICallbackProxy } from "../../src/interfaces/IProxyLedger.sol";
import { LasnaForkHarness } from "./LasnaForkHarness.sol";
import { ProxyDelivery } from "./ProxyDeliveryHarness.sol";

/// @notice The event of the system contract that requests a callback.
/// @dev Copied from `system-smart-contracts` at `72710d2`, `src/omni/SystemContract.sol`.
interface ISystemContractCallbacks {
    /// @notice Indicates a pending callback request received from a reactive contract.
    /// @param chainId_ Destination chain ID.
    /// @param sender_ Message sender's address.
    /// @param recipient_ Destination contract's address.
    /// @param version_ Callback configuration version.
    /// @param configuration_ ABI-encoded callback configuration struct in the format matching the `version` provided.
    event CallbackRequest(
        uint256 indexed chainId_,
        address indexed sender_,
        address indexed recipient_,
        ICallbackProxy.CallbackVersion version_,
        bytes configuration_
    );
}

/// @title Fork harness for Lasna and one destination chain.
/// @notice The harness holds a Lasna fork and a Base Sepolia fork, built on `LasnaForkHarness` and
///         `ProxyDelivery`. The system contract and the callback proxy are the real deployed code. The
///         harness emulates only the node, in both directions:
///         - A `CallbackRequest` log on Lasna for the destination chain becomes one delivery through the
///           real `deliverCallback`. The gas limit of the delivery transaction is the requested gas limit,
///           and the first argument of the payload is the address of the contract that made the request.
///         - A log of the destination chain goes to each contract with a matching active subscription on
///           Lasna, through the real `trigger`, with the destination chain ID in the log record.
///
///         Rules of the emulation, in addition to the rules of the two harnesses:
///         - A request with a gas limit below 100,000 is not delivered, and a request for another chain is
///           not delivered.
///         - A subscription matches a destination log only if it was active when the harness took that log
///           from the destination fork.
///         - The two forks have their own block number and their own time.
///
///         One log recorder serves both forks. A test must change the fork only with `_toLasna` and
///         `_toDestination`, which move the recorded logs into the history of the fork that they leave. The
///         log functions of `LasnaForkHarness` (`_logMark`, `_countLogs`, `_findLog`, `_onlyLog`,
///         `_activeSubscriptions`, `_relay`, `_trigger`) need the Lasna fork. For the destination logs use
///         `_destinationMark` and `_destinationLogs` in place of `_takeLogs`.
abstract contract TwoForkHarness is LasnaForkHarness, ProxyDelivery {
    // ------------------------------------------------------------------------------------------
    // Pinned values
    // ------------------------------------------------------------------------------------------

    uint256 internal constant DESTINATION_CHAIN_ID = BASE_SEPOLIA_CHAIN_ID;

    /// @dev The node ignores a callback request below this gas limit.
    uint256 internal constant MIN_CALLBACK_GAS = 100_000;

    /// @dev Stops a relay between the forks that does not end.
    uint256 internal constant MAX_ROUNDS_PER_RELAY = 64;

    /// @dev The storage slots of `_maxChargeGas` and `_extraGas` in the proxy of the pinned block.
    ///      `_setProxyGas` checks each write with the getter.
    uint256 private constant PROXY_MAX_CHARGE_GAS_SLOT = 3;
    uint256 private constant PROXY_EXTRA_GAS_SLOT = 4;

    /// @dev The values of the callback proxy of Ethereum Sepolia (chain ID 11155111),
    ///      `0xAa14d9Cd0f788Ca23d11805986C1d8bd035D23b4`, read from the live proxy at block 12,190,033.
    uint256 internal constant ETHEREUM_SEPOLIA_PROXY_EXTRA_GAS = 150_000;
    uint256 internal constant ETHEREUM_SEPOLIA_PROXY_MAX_CHARGE_GAS = 50_000;

    // ------------------------------------------------------------------------------------------
    // Types and state
    // ------------------------------------------------------------------------------------------

    /// @notice One log of the destination fork.
    struct DestinationLog {
        bytes32[] topics;
        bytes data;
        address emitter;
        uint256 blockNumber; // the block of the destination fork when the harness took the log
        uint256 lasnaLogs; // the number of logs in the Lasna history at that time
    }

    /// @notice One delivery on the destination fork. Its logs are `logCount` logs of the destination
    ///         history from `firstLog`.
    struct DeliveryRecord {
        address requester; // the value of the first argument
        address recipient;
        uint64 gasLimit;
        bool delivered;
        bool callbackFailed;
        bytes revertData;
        uint256 fee;
        uint256 gasUsed;
        uint256 firstLog;
        uint256 logCount;
    }

    uint256 internal lasnaFork;
    uint256 internal destinationFork;

    DestinationLog[] private _destinationHistory;
    uint256 private _nextDestinationLogToRelay;
    DeliveryRecord[] private _deliveries;

    /// @dev The number of logs in the Lasna history when the harness left the Lasna fork.
    uint256 private _lasnaLogs;

    /// @dev The Lasna history before this index has no callback request that is not handled.
    uint256 private _nextRequestLog;

    // ------------------------------------------------------------------------------------------
    // Setup and forks
    // ------------------------------------------------------------------------------------------

    /// @dev Forks Lasna and Base Sepolia at the pinned blocks. Call it first in `setUp`. The Lasna fork is
    ///      active after it.
    function _forkBoth() internal {
        _forkLasna();
        lasnaFork = vm.activeFork();
        _forkBaseSepolia();
        destinationFork = vm.activeFork();
        vm.selectFork(lasnaFork);
    }

    function _onLasna() internal view returns (bool) {
        return vm.activeFork() == lasnaFork;
    }

    /// @dev Makes the Lasna fork active. The recorded logs of the destination fork go into its history.
    function _toLasna() internal {
        if (_onLasna()) return;
        _collectDestination();
        vm.selectFork(lasnaFork);
    }

    /// @dev Makes the destination fork active. The recorded logs of the Lasna fork go into its history.
    function _toDestination() internal {
        if (!_onLasna()) return;
        _lasnaLogs = _logMark();
        vm.selectFork(destinationFork);
    }

    /// @dev Sets `_extraGas` and `_maxChargeGas` of the proxy on the destination fork: the proxy
    ///      configuration of another chain on the code of the pinned block.
    function _setProxyGas(uint256 extraGas, uint256 maxChargeGas) internal {
        _toDestination();
        vm.store(PROXY_ADDRESS, bytes32(PROXY_EXTRA_GAS_SLOT), bytes32(extraGas));
        vm.store(PROXY_ADDRESS, bytes32(PROXY_MAX_CHARGE_GAS_SLOT), bytes32(maxChargeGas));
        assertEq(proxy._extraGas(), extraGas, "the extra gas was not written");
        assertEq(proxy._maxChargeGas(), maxChargeGas, "the charge gas was not written");
    }

    // ------------------------------------------------------------------------------------------
    // Accounts, blocks and time: the harness of the active fork
    // ------------------------------------------------------------------------------------------

    /// @dev Returns an address that is fresh on the active fork.
    function _freshAccount(string memory label)
        internal
        override(LasnaForkHarness, ProxyDelivery)
        returns (address account)
    {
        return _onLasna() ? LasnaForkHarness._freshAccount(label) : ProxyDelivery._freshAccount(label);
    }

    /// @dev Returns an address that is fresh on both forks. The active fork does not change.
    function _freshAccountOnBoth(string memory label) internal returns (address account) {
        bool onLasna = _onLasna();
        _toLasna();
        account = _freshAccount(label);
        _toDestination();
        _assertFresh(account);
        if (onLasna) _toLasna();
    }

    function _assertFresh(address account) internal view override(LasnaForkHarness, ProxyDelivery) {
        if (_onLasna()) LasnaForkHarness._assertFresh(account);
        else ProxyDelivery._assertFresh(account);
    }

    function _nextFreshContract(address deployer)
        internal
        view
        override(LasnaForkHarness, ProxyDelivery)
        returns (address created)
    {
        return _onLasna() ? LasnaForkHarness._nextFreshContract(deployer) : ProxyDelivery._nextFreshContract(deployer);
    }

    /// @dev Moves the active fork `blocks` blocks forward.
    function _mine(uint256 blocks) internal override(LasnaForkHarness, ProxyDelivery) {
        if (_onLasna()) LasnaForkHarness._mine(blocks);
        else ProxyDelivery._mine(blocks);
    }

    /// @dev Moves the active fork `secs` seconds forward, with the matching number of blocks.
    function _skipTime(uint256 secs) internal override(LasnaForkHarness, ProxyDelivery) {
        if (_onLasna()) {
            LasnaForkHarness._skipTime(secs);
        } else {
            _collectDestination();
            ProxyDelivery._skipTime(secs);
        }
    }

    // ------------------------------------------------------------------------------------------
    // Delivery
    // ------------------------------------------------------------------------------------------

    /// @notice Delivers a callback on the destination fork through the real `deliverCallback`. The
    ///         destination fork is active after it. The logs of the delivery go into the destination
    ///         history, so the next `_relayAll` sends them to Lasna.
    function _deliver(address recipient, bytes memory payload, uint64 gasLimit, address firstArgument)
        internal
        override
        returns (Delivery memory result)
    {
        _toDestination();
        _collectDestination();
        result = ProxyDelivery._deliver(recipient, payload, gasLimit, firstArgument);

        DeliveryRecord storage record = _deliveries.push();
        record.requester = firstArgument;
        record.recipient = recipient;
        record.gasLimit = gasLimit;
        record.delivered = result.delivered;
        record.callbackFailed = result.callbackFailed;
        record.revertData = result.revertData;
        record.fee = result.fee;
        record.gasUsed = result.gasUsed;
        record.firstLog = _destinationHistory.length;
        record.logCount = result.logs.length;
        _appendDestinationLogs(result.logs);
    }

    /// @notice The number of deliveries on the destination fork. Use it as a mark before an action.
    function _deliveryCount() internal view returns (uint256) {
        return _deliveries.length;
    }

    /// @notice One delivery, from 0 in the order of the deliveries.
    function _deliveryAt(uint256 index) internal view returns (DeliveryRecord memory) {
        return _deliveries[index];
    }

    /// @notice The logs of one delivery.
    function _deliveryLogs(uint256 index) internal view returns (Vm.Log[] memory) {
        DeliveryRecord storage record = _deliveries[index];
        return _destinationLogs(record.firstLog, record.firstLog + record.logCount);
    }

    // ------------------------------------------------------------------------------------------
    // Relay between the forks
    // ------------------------------------------------------------------------------------------

    /// @notice Relays in both directions until no log is left: the logs of Lasna to the subscribers on
    ///         Lasna, the callback requests to the destination proxy, and the logs of the destination to
    ///         the subscribers on Lasna. The Lasna fork is active after it.
    /// @return triggers The number of trigger transactions that ran on Lasna.
    /// @return deliveries The number of delivery transactions that ran on the destination.
    function _relayAll() internal returns (uint256 triggers, uint256 deliveries) {
        for (uint256 round = 0;; ++round) {
            require(round < MAX_ROUNDS_PER_RELAY, "TwoForkHarness: the relay does not end");
            _toLasna();
            uint256 ran = _relay();
            uint256 delivered = _deliverRequests();
            ran += _relayDestinationLogs();
            if (ran == 0 && delivered == 0) break;
            triggers += ran;
            deliveries += delivered;
        }
    }

    /// @dev Delivers each callback request of the Lasna history that is not handled yet.
    function _deliverRequests() private returns (uint256 delivered) {
        uint256 from = _nextRequestLog;
        bytes32 topic0 = ISystemContractCallbacks.CallbackRequest.selector;
        uint256 count = _countLogs(from, SYSTEM_ADDRESS, topic0);
        ChainLog[] memory requests = new ChainLog[](count);
        for (uint256 i = 0; i < count; ++i) {
            requests[i] = _findLog(from, SYSTEM_ADDRESS, topic0, i);
        }
        _nextRequestLog = _logMark();

        for (uint256 i = 0; i < count; ++i) {
            ICallbackProxy.CallbackConfiguration_V_1_0 memory config = _requestConfig(requests[i]);
            if (requests[i].topics[1] != DESTINATION_CHAIN_ID || config.gasLimit < MIN_CALLBACK_GAS) continue;

            // The node overwrites the first argument with the address of the contract that made the request.
            _deliver(config.recipient, config.payload, config.gasLimit, address(uint160(requests[i].topics[2])));
            ++delivered;
        }
    }

    /// @dev The callback configuration of a `CallbackRequest` log.
    function _requestConfig(ChainLog memory request)
        internal
        pure
        returns (ICallbackProxy.CallbackConfiguration_V_1_0 memory)
    {
        (, bytes memory encoded) = abi.decode(request.data, (ICallbackProxy.CallbackVersion, bytes));
        return abi.decode(encoded, (ICallbackProxy.CallbackConfiguration_V_1_0));
    }

    /// @dev Sends each destination log that is not relayed yet to its subscribers on Lasna.
    function _relayDestinationLogs() private returns (uint256 triggers) {
        _toLasna();
        while (_nextDestinationLogToRelay < _destinationHistory.length) {
            DestinationLog storage stored = _destinationHistory[_nextDestinationLogToRelay++];
            ChainLog memory log;
            log.emitter = stored.emitter;
            log.topicCount = stored.topics.length;
            for (uint256 i = 0; i < log.topicCount; ++i) {
                log.topics[i] = uint256(stored.topics[i]);
            }
            log.data = stored.data;
            log.blockNumber = stored.blockNumber;

            address[] memory subscribers = _subscribersOf(DESTINATION_CHAIN_ID, log, stored.lasnaLogs, stored.lasnaLogs);
            for (uint256 i = 0; i < subscribers.length; ++i) {
                // The node does not trigger a contract that is in debt.
                if (system.debt(subscribers[i]) != 0) continue;
                require(triggers < MAX_TRIGGERS_PER_RELAY, "TwoForkHarness: the relay does not end");
                _trigger(subscribers[i], _destinationLogRecord(log));
                ++triggers;
            }
        }
    }

    /// @dev The log record that the node builds from a log of the destination chain. `opCode` is the topic
    ///      count. The two hashes and the log index are 0: Lifeline does not read them.
    function _destinationLogRecord(ChainLog memory log) internal pure returns (IReactive.LogRecord memory record) {
        record = IReactive.LogRecord({
            chainId: DESTINATION_CHAIN_ID,
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

    // ------------------------------------------------------------------------------------------
    // Destination history
    // ------------------------------------------------------------------------------------------

    /// @dev Moves the logs that the EVM recorded on the destination fork into the destination history.
    function _collectDestination() private {
        if (_onLasna()) return;
        _appendDestinationLogs(vm.getRecordedLogs());
    }

    function _appendDestinationLogs(Vm.Log[] memory logs) private {
        uint256 blockNumber = vm.getBlockNumber();
        for (uint256 i = 0; i < logs.length; ++i) {
            DestinationLog storage stored = _destinationHistory.push();
            stored.topics = logs[i].topics;
            stored.data = logs[i].data;
            stored.emitter = logs[i].emitter;
            stored.blockNumber = blockNumber;
            stored.lasnaLogs = _lasnaLogs;
        }
    }

    /// @notice The number of logs in the destination history. Use it as a mark before an action. It works
    ///         on both forks.
    function _destinationMark() internal returns (uint256) {
        _collectDestination();
        return _destinationHistory.length;
    }

    /// @notice The logs of the destination history from a mark to the end. It works on both forks.
    function _destinationLogs(uint256 from) internal returns (Vm.Log[] memory) {
        return _destinationLogs(from, _destinationMark());
    }

    /// @notice The logs of the destination history from `from` to `to`, without `to`.
    function _destinationLogs(uint256 from, uint256 to) internal view returns (Vm.Log[] memory logs) {
        logs = new Vm.Log[](to - from);
        for (uint256 i = from; i < to; ++i) {
            DestinationLog storage stored = _destinationHistory[i];
            logs[i - from].topics = stored.topics;
            logs[i - from].data = stored.data;
            logs[i - from].emitter = stored.emitter;
        }
    }
}
