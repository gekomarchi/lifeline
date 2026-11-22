// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.30;

import { Vm } from "forge-std/Vm.sol";
import { console2 } from "forge-std/console2.sol";
import { IReactive } from "@reactive/src/interfaces/IReactive.sol";
import { CronRelay, ICronSource } from "./harness/CronRelay.sol";
import { ISystemContractCallbacks } from "./harness/TwoForkHarness.sol";
import {
    LifelineHub,
    SWEEP_PER_TARGET,
    SWEEP_MAX,
    MAX_PER_TICK,
    ITER_RESERVE,
    TAIL_RESERVE,
    SWEEP_RESERVE,
    END_RESERVE,
    CRON_EMITTER,
    CRON_1000_TOPIC0,
    CRON_10000_TOPIC0
} from "../src/LifelineHub.sol";
import { LifelineVault } from "../src/LifelineVault.sol";
import { Policy, TargetState } from "../src/LifelineTypes.sol";
import { ICallbackProxy, IProxyLedger } from "../src/interfaces/IProxyLedger.sol";
import { ISystemContract, REACTIVE_IGNORE } from "../src/interfaces/ISystemContract.sol";
import { CanaryCallback } from "../src/fixtures/CanaryCallback.sol";
import { CanaryReactive } from "../src/fixtures/CanaryReactive.sol";

/// @notice Fork tests of the gauge: the cron tick of the hub, its Lasna batch and its sweep requests. The hub,
///         the vault, the canaries, the system contract, the cron source and the callback proxy are real; only
///         the node is emulated by the harness. A cron log record goes to the hub through the real `trigger`,
///         with 900,000 gas. Most records are forged. `test_realCron_*` uses the real cron logs of the code
///         of the fork block.
contract GaugeTest is CronRelay {
    // Lasna: the hub.
    uint256 internal constant HUB_VALUE = 20 ether;
    uint256 internal constant HUB_FEE_FLOAT = 0.5 ether;
    /// @dev Above the fees of the longest test: the float of the hub never runs out.
    uint256 internal constant HUB_SELF_CARE_DAILY_CAP = 8 ether;

    // Lasna: the policy of a target.
    uint256 internal constant LOW = 0.01 ether;
    uint256 internal constant HIGH = 0.02 ether;
    uint256 internal constant MAX_DEBT = 0.05 ether;
    uint256 internal constant DAILY_CAP = 1 ether;
    uint32 internal constant WINDOW = 1 hours;

    /// @dev Reserves between `LOW` and `HIGH`: the gauge does not pay.
    uint256 internal constant BETWEEN = 0.015 ether;
    /// @dev Reserves above 0 and below `LOW`: the gauge pays `HIGH - BELOW`.
    uint256 internal constant BELOW = 0.001 ether;

    // Destination: the vault.
    uint256 internal constant VAULT_PRINCIPAL = 0.01 ether;
    uint256 internal constant VAULT_FEE_FLOAT = 1.5e13;
    uint256 internal constant VAULT_SELF_CARE_DAILY_CAP = 3e13;
    uint256 internal constant VAULT_PRINCIPAL_FLOOR = 0.001 ether;

    // Destination: the policy of a target.
    uint256 internal constant DEST_HIGH = 5e12;
    uint256 internal constant DEST_MAX_DEBT = 1e13;
    uint256 internal constant DEST_DAILY_CAP = 1e14;
    /// @dev Below one callback fee: each callback after a rescue is a new debt.
    uint256 internal constant DEST_HIGH_BELOW_ONE_FEE = 1e11;

    /// @dev `sweepBase` of Base Sepolia: `90,000 + _extraGas + _maxChargeGas` of the proxy (spec §4.4).
    uint256 internal constant SWEEP_BASE = 220_000;

    /// @dev A second destination chain: Unichain Sepolia. It has a vault record in the hub and no fork. The
    ///      harness delivers no request for it.
    uint256 internal constant OTHER_CHAIN_ID = 1301;
    uint256 internal constant OTHER_SWEEP_BASE = 220_000;

    /// @dev The bytes of zero data that move the gas of `react()` by about 1,100: the step of the tests that
    ///      try each position of the gas guards.
    uint256 internal constant PAD_STEP = 256;
    /// @dev `PAD_STEP * PAD_STEPS` moves the gas by more than the cost of the most expensive target.
    uint256 internal constant PAD_STEPS = 80;

    address internal owner;
    address internal stranger;
    LifelineHub internal hub;
    LifelineVault internal vault;
    address internal otherProxy;
    address internal otherVault;

    function setUp() public {
        _forkBoth();
        owner = _freshAccountOnBoth("owner");
        stranger = _freshAccountOnBoth("stranger");
        otherProxy = _freshAccount("otherProxy");
        otherVault = _freshAccount("otherVault");

        vm.deal(owner, 100 ether);
        vm.deal(stranger, 100 ether);
        hub = _deployHub(HUB_VALUE, HUB_FEE_FLOAT, HUB_FEE_FLOAT, HUB_SELF_CARE_DAILY_CAP);

        _toDestination();
        vm.deal(owner, 1 ether);
        vm.deal(stranger, 1 ether);
        // With the same nonce on both chains the vault would get the address of the hub.
        vm.setNonce(owner, 1);
        _nextFreshContract(owner);
        vm.prank(owner);
        vault = new LifelineVault{ value: VAULT_PRINCIPAL + VAULT_FEE_FLOAT }(
            PROXY_ADDRESS,
            address(hub),
            VAULT_FEE_FLOAT,
            VAULT_FEE_FLOAT,
            VAULT_SELF_CARE_DAILY_CAP,
            VAULT_PRINCIPAL_FLOOR
        );
        assertTrue(address(vault) != address(hub), "the hub and the vault have different addresses");

        _toLasna();
        vm.startPrank(owner);
        hub.setVault(DESTINATION_CHAIN_ID, PROXY_ADDRESS, address(vault), SWEEP_BASE);
        hub.resumeCron();
        vm.stopPrank();
    }

    // ------------------------------------------------------------------------------------------
    // Helpers: the Lasna side
    // ------------------------------------------------------------------------------------------

    /// @notice One cron log record that the harness sent to the hub, and the `Tick` of the hub if the tick ran.
    struct TickLog {
        bool success; // the trigger transaction did not revert
        bool reactReverted; // the system contract emitted `ReactiveContractReverted`
        uint256 gasUsed; // the gas of the trigger transaction
        uint256 mark; // the index in the Lasna history of the first log of the trigger
        bool ran; // the hub emitted `Tick`
        uint256 tickId;
        uint256 processed;
        uint256 cursor;
        uint256 sweeps;
    }

    function _deployHub(uint256 value, uint256 feeFloat, uint256 reserveTarget, uint256 selfCareCap)
        internal
        returns (LifelineHub deployed)
    {
        _toLasna();
        _nextFreshContract(owner);
        vm.prank(owner);
        deployed = new LifelineHub{ value: value }(SYSTEM_ADDRESS, feeFloat, reserveTarget, selfCareCap, 0);
    }

    function _lasnaPolicy() internal pure returns (Policy memory) {
        return Policy({ low: LOW, high: HIGH, maxDebt: MAX_DEBT, dailyCap: DAILY_CAP, maxRescues: 3, window: WINDOW });
    }

    /// @dev A reactive canary on Lasna with these reserves. It is not enrolled.
    function _lasnaCanary(uint256 reserves) internal returns (address canary) {
        _toLasna();
        _nextFreshContract(owner);
        vm.prank(owner);
        canary = address(new CanaryReactive(SYSTEM_ADDRESS, 0, address(0), 0));
        if (reserves != 0) {
            vm.prank(stranger);
            system.depositTo{ value: reserves }(canary);
        }
    }

    function _enrollLasna(address target) internal {
        _toLasna();
        vm.prank(owner);
        hub.enroll(LASNA_CHAIN_ID, target, _lasnaPolicy());
    }

    /// @dev `enroll` reconciles a target at once. With the floor above the balance, that reconcile pays
    ///      nothing, so a target can be enrolled with reserves below `low`: the state of a target that used
    ///      its reserves after the enrollment.
    function _setFloor(uint256 floor) internal {
        _toLasna();
        vm.prank(owner);
        hub.setPrincipalFloor(floor);
    }

    /// @dev Enrolls new canaries with these reserves, and pays none of them.
    function _enrollUnpaid(uint256[] memory reserves) internal returns (address[] memory fleet) {
        _setFloor(type(uint256).max);
        fleet = new address[](reserves.length);
        for (uint256 i = 0; i < reserves.length; ++i) {
            fleet[i] = _lasnaCanary(reserves[i]);
            _enrollLasna(fleet[i]);
            assertEq(system.reserves(fleet[i]), reserves[i], "the enrollment paid nothing");
        }
        _setFloor(0);
    }

    /// @dev `count` new enrolled canaries, each with these reserves and no payment.
    function _enrollUnpaid(uint256 count, uint256 reserves) internal returns (address[] memory) {
        uint256[] memory list = new uint256[](count);
        for (uint256 i = 0; i < count; ++i) {
            list[i] = reserves;
        }
        return _enrollUnpaid(list);
    }

    /// @dev The origin block of a new cron log: the current block of Lasna. The trigger runs one block later.
    function _originBlock() internal returns (uint256) {
        _toLasna();
        return vm.getBlockNumber();
    }

    /// @dev Sends a cron log record of the selected topic, with the current block as its origin block.
    function _tick() internal returns (TickLog memory) {
        uint256 origin = _originBlock();
        return _sendCron(_cronRecord(hub.cronTopic(), origin));
    }

    /// @dev `_tick`, and asserts that the tick ran with the origin block as its `tickId` and with these values.
    function _tickRan(uint256 processed, uint256 cursor, uint256 sweeps) internal returns (TickLog memory t) {
        uint256 origin = _originBlock();
        t = _tick();
        _assertTick(t, origin, processed, cursor, sweeps);
    }

    /// @dev Sends a log record to the hub through the real `trigger`, in a new block.
    function _sendCron(IReactive.LogRecord memory record) internal returns (TickLog memory) {
        _toLasna();
        return _tickOf(_trigger(address(hub), record));
    }

    /// @dev The result of a trigger of the hub, and the `Tick` of that trigger if there is one.
    function _tickOf(TriggerResult memory result) internal returns (TickLog memory t) {
        (t.success, t.reactReverted, t.gasUsed, t.mark) =
        (result.success, result.reactReverted, result.gasUsed, result.firstLog);
        uint256 count = _hubLogs(t.mark, LifelineHub.Tick.selector);
        assertLe(count, 1, "at most one Tick for one record");
        if (count == 0) return t;

        ChainLog memory log = _findLog(t.mark, address(hub), LifelineHub.Tick.selector, 0);
        t.ran = true;
        t.tickId = log.topics[1];
        (t.processed, t.cursor, t.sweeps) = abi.decode(log.data, (uint256, uint256, uint256));
    }

    /// @dev The trigger succeeded inside its 900,000 gas, the system contract reported no reverted reaction,
    ///      and the hub has no debt.
    function _assertHeld(TickLog memory t) internal {
        assertTrue(t.success, "the trigger succeeded inside 900,000 gas");
        assertFalse(t.reactReverted, "no reverted reaction");
        _toLasna();
        assertEq(system.debt(address(hub)), 0, "the hub has no debt");
    }

    /// @dev Asserts a tick that ran, with these values.
    function _assertTick(TickLog memory t, uint256 tickId, uint256 processed, uint256 cursor, uint256 sweeps) internal {
        _assertHeld(t);
        assertTrue(t.ran, "Tick");
        assertEq(t.tickId, tickId, "Tick tickId");
        assertEq(t.processed, processed, "Tick processed");
        assertEq(t.cursor, cursor, "Tick cursor");
        assertEq(t.sweeps, sweeps, "Tick sweeps");
        assertEq(hub.lastTick(), tickId, "lastTick");
    }

    function _hubLogs(uint256 mark, bytes32 topic0) internal returns (uint256) {
        _toLasna();
        return _countLogs(mark, address(hub), topic0);
    }

    /// @dev Asserts a record that ran no tick: the trigger held, and the hub gave no `Tick` and one `Skipped`
    ///      with this reason. The target of that `Skipped` is `topic1` of the record: the origin block.
    function _assertNoTick(TickLog memory t, uint256 originBlock, LifelineHub.SkipReason reason) internal {
        _assertHeld(t);
        assertFalse(t.ran, "no Tick");
        ChainLog memory skipped = _onlyLog(t.mark, address(hub), LifelineHub.Skipped.selector);
        assertEq(skipped.topics[1], LASNA_CHAIN_ID, "Skipped chain");
        assertEq(skipped.topics[2], uint256(uint160(originBlock)), "Skipped target");
        assertEq(abi.decode(skipped.data, (uint256)), uint256(reason), "Skipped reason");
    }

    /// @dev Asserts that the hub paid nothing to a Lasna target after a mark.
    function _assertNoPayment(uint256 mark) internal {
        assertEq(_hubLogs(mark, LifelineHub.ToppedUp.selector), 0, "no ToppedUp");
        assertEq(_hubLogs(mark, LifelineHub.Rescued.selector), 0, "no Rescued");
    }

    /// @dev The amount of the one `ToppedUp` of the hub for a target after a mark. Fails if there is not
    ///      exactly one.
    function _toppedUp(uint256 mark, address target) internal returns (uint256 paid) {
        uint256 found;
        uint256 count = _hubLogs(mark, LifelineHub.ToppedUp.selector);
        for (uint256 i = 0; i < count; ++i) {
            ChainLog memory log = _findLog(mark, address(hub), LifelineHub.ToppedUp.selector, i);
            if (log.topics[1] != uint256(uint160(target))) continue;
            paid = abi.decode(log.data, (uint256));
            ++found;
        }
        assertEq(found, 1, "exactly one ToppedUp for the target");
    }

    /// @dev The callback requests after a mark, in their order. Each one is a request of the hub.
    function _hubRequests(uint256 mark) internal returns (ICallbackProxy.CallbackConfiguration_V_1_0[] memory list) {
        _toLasna();
        bytes32 topic0 = ISystemContractCallbacks.CallbackRequest.selector;
        list = new ICallbackProxy.CallbackConfiguration_V_1_0[](_countLogs(mark, SYSTEM_ADDRESS, topic0));
        for (uint256 i = 0; i < list.length; ++i) {
            ChainLog memory log = _findLog(mark, SYSTEM_ADDRESS, topic0, i);
            list[i] = _requestConfig(log);
            assertEq(log.topics[1], list[i].chainId, "the chain of the request log");
            assertEq(log.topics[2], uint256(uint160(address(hub))), "the requester is the hub");
            assertEq(log.topics[3], uint256(uint160(list[i].recipient)), "the recipient of the request log");
        }
    }

    /// @dev Checks one sweep request of the hub: the chain, the vault, the computed gas limit, and a `sweep`
    ///      payload with the placeholder, exactly these targets in this order, and the `tickId` as its id.
    function _assertSweepRequest(
        ICallbackProxy.CallbackConfiguration_V_1_0 memory request,
        uint256 chainId,
        address recipient,
        uint256 sweepBase,
        uint256 tickId,
        address[] memory targets
    ) internal pure {
        assertEq(request.chainId, chainId, "the chain of the request");
        assertEq(request.recipient, recipient, "the recipient is the vault of that chain");
        assertEq(request.payload, abi.encodeCall(LifelineVault.sweep, (address(0), targets, tickId)), "the payload");
        assertEq(request.gasLimit, sweepBase + SWEEP_PER_TARGET * targets.length, "the computed gas limit");
    }

    /// @dev `count` items of a list from `first`, with a wrap at the end of the list.
    function _window(address[] memory list, uint256 first, uint256 count) internal pure returns (address[] memory w) {
        w = new address[](count);
        for (uint256 i = 0; i < count; ++i) {
            w[i] = list[(first + i) % list.length];
        }
    }

    /// @dev Asserts exactly one `Subscribe` or `Unsubscribe` of the system contract after a mark for a cron
    ///      tuple of the hub: the subscriber is the hub, the chain is Lasna and the emitter is the cron source.
    ///      That log has this `topic0` and a wildcard in each other topic.
    function _assertCronTupleLog(uint256 mark, bytes32 event_, uint256 cronTopic0) internal {
        _toLasna();
        uint256 found;
        uint256 all = _countLogs(mark, SYSTEM_ADDRESS, event_);
        for (uint256 i = 0; i < all; ++i) {
            ChainLog memory log = _findLog(mark, SYSTEM_ADDRESS, event_, i);
            if (
                log.topics[1] != uint256(uint160(address(hub))) || log.topics[2] != LASNA_CHAIN_ID
                    || log.topics[3] != uint256(uint160(CRON_SOURCE))
            ) continue;
            assertEq(log.data, abi.encode(cronTopic0, REACTIVE_IGNORE, REACTIVE_IGNORE, REACTIVE_IGNORE), "topics");
            ++found;
        }
        assertEq(found, 1, "exactly one log of the cron tuple");
    }

    // ------------------------------------------------------------------------------------------
    // Helpers: the destination side
    // ------------------------------------------------------------------------------------------

    struct ResultLog {
        uint256 id;
        address target;
        LifelineVault.Outcome outcome;
        uint256 paid;
    }

    /// @dev `low` is 0, so `setPolicy` does not fund a target that has no debt.
    function _destPolicy() internal pure returns (Policy memory) {
        return Policy({
            low: 0, high: DEST_HIGH, maxDebt: DEST_MAX_DEBT, dailyCap: DEST_DAILY_CAP, maxRescues: 2, window: WINDOW
        });
    }

    /// @dev An unfunded callback canary on the destination: no balance, no reserves, no debt.
    function _deployCanary() internal returns (address canary) {
        _toDestination();
        _nextFreshContract(owner);
        vm.prank(owner);
        canary = address(new CanaryCallback());
    }

    /// @dev The onboarding of a destination target in the recommended order: `vault.setPolicy`, then
    ///      `hub.enroll`.
    function _arm(address canary, Policy memory policy) internal {
        _toDestination();
        vm.prank(owner);
        vault.setPolicy(canary, policy);
        _toLasna();
        vm.prank(owner);
        hub.enroll(DESTINATION_CHAIN_ID, canary, policy);
    }

    function _armedCanaries(uint256 count) internal returns (address[] memory canaries) {
        canaries = new address[](count);
        for (uint256 i = 0; i < count; ++i) {
            canaries[i] = _deployCanary();
            _arm(canaries[i], _destPolicy());
        }
    }

    function _disarm(uint256 chainId, address target) internal {
        _toLasna();
        vm.prank(owner);
        hub.disarm(chainId, target);
    }

    /// @dev Enrolls `count` targets of a destination chain in the hub only, and returns them in the order of
    ///      the list of the hub. The addresses have no code: a test that uses them delivers no sweep.
    function _enrollPlain(uint256 chainId, uint256 count) internal returns (address[] memory targets) {
        _toLasna();
        targets = new address[](count);
        vm.startPrank(owner);
        for (uint256 i = 0; i < count; ++i) {
            targets[i] = address(uint160(uint256(keccak256(abi.encode("lifeline.gauge.target", chainId, i)))));
            hub.enroll(chainId, targets[i], _destPolicy());
        }
        vm.stopPrank();
    }

    /// @dev Registers the vault of the second destination chain.
    function _setOtherVault() internal {
        _toLasna();
        vm.prank(owner);
        hub.setVault(OTHER_CHAIN_ID, otherProxy, otherVault, OTHER_SWEEP_BASE);
    }

    /// @dev Delivers `pong` to a canary. The requester is not the hub: a canary has no access check.
    function _pong(address canary) internal returns (Delivery memory d) {
        d = _deliver(canary, abi.encodeCall(CanaryCallback.pong, (address(0), 1)), DELIVERY_GAS, stranger);
        assertTrue(d.delivered, "the delivery did not revert");
        assertFalse(d.callbackFailed, "no CallbackFailure");
    }

    /// @dev The `Result` logs of the vault in the logs of a delivery.
    function _results(Vm.Log[] memory logs) internal view returns (ResultLog[] memory results) {
        uint256 count = _countLogs(logs, address(vault), LifelineVault.Result.selector);
        results = new ResultLog[](count);
        for (uint256 i = 0; i < count; ++i) {
            Vm.Log memory log = _findLog(logs, address(vault), LifelineVault.Result.selector, i);
            results[i].id = uint256(log.topics[1]);
            results[i].target = address(uint160(uint256(log.topics[2])));
            (results[i].outcome, results[i].paid) = abi.decode(log.data, (LifelineVault.Outcome, uint256));
        }
    }

    /// @dev Checks a delivery that the relay made for a sweep request of the hub: the recipient is the vault,
    ///      the first argument is the address of the hub, and the callback ran inside its gas limit. Returns
    ///      the `Result` logs.
    function _sweepResults(uint256 index, uint256 targetCount) internal view returns (ResultLog[] memory) {
        DeliveryRecord memory d = _deliveryAt(index);
        assertEq(d.recipient, address(vault), "the recipient is the vault");
        assertEq(d.requester, address(hub), "the first argument is the address of the hub");
        assertEq(d.gasLimit, SWEEP_BASE + SWEEP_PER_TARGET * targetCount, "the gas limit is the computed limit");
        assertTrue(d.delivered, "the delivery did not revert");
        assertFalse(d.callbackFailed, "no CallbackFailure");
        assertLe(d.gasUsed, d.gasLimit, "the transaction is inside its gas limit");

        Vm.Log[] memory logs = _deliveryLogs(index);
        assertEq(_countLogs(logs, address(vault), LifelineVault.SweepPartial.selector), 0, "no SweepPartial");
        return _results(logs);
    }

    function _assertResult(ResultLog memory result, uint256 id, address target, LifelineVault.Outcome outcome)
        internal
        pure
    {
        assertEq(result.id, id, "Result id");
        assertEq(result.target, target, "Result target");
        assertEq(uint256(result.outcome), uint256(outcome), "Result outcome");
    }

    /// @dev One tick with no Lasna target and its relay to the destination: exactly one sweep request of the
    ///      hub, for exactly these targets, and one delivery. Returns the tick and the `Result` logs of the
    ///      delivery.
    function _tickAndSweep(address[] memory expected) internal returns (TickLog memory t, ResultLog[] memory results) {
        uint256 deliveriesBefore = _deliveryCount();
        t = _tickRan(0, 0, 1);

        ICallbackProxy.CallbackConfiguration_V_1_0[] memory requests = _hubRequests(t.mark);
        assertEq(requests.length, 1, "one CallbackRequest to the vault");
        _assertSweepRequest(requests[0], DESTINATION_CHAIN_ID, address(vault), SWEEP_BASE, t.tickId, expected);

        (, uint256 deliveries) = _relayAll();
        assertEq(deliveries, 1, "one delivery");
        assertEq(_deliveryCount(), deliveriesBefore + 1);
        results = _sweepResults(deliveriesBefore, expected.length);
        assertEq(results.length, expected.length, "one Result for each target");
        for (uint256 i = 0; i < expected.length; ++i) {
            assertEq(results[i].id, t.tickId, "the id of each Result is the tickId");
            assertEq(results[i].target, expected[i], "the order of the payload");
        }
    }

    // ------------------------------------------------------------------------------------------
    // Constants, ABI and access
    // ------------------------------------------------------------------------------------------

    /// @dev `ITER_RESERVE` and `MAX_PER_TICK` have their start values of spec §4.4. `TAIL_RESERVE` started at
    ///      150,000.
    function test_constants_haveTheFinalValues() public pure {
        assertEq(MAX_PER_TICK, 12);
        assertEq(ITER_RESERVE, 60_000);
        assertEq(TAIL_RESERVE, 290_000);
        assertEq(SWEEP_RESERVE, 70_000);
        assertEq(END_RESERVE, 130_000);
    }

    /// @dev The cron values of the hub are the ones of `system-smart-contracts` at `72710d2`. The two literal
    ///      topics are the ones of the cron logs on Lasna, for example in block 8,910,000.
    function test_cronValues_areTheOnesOfThePinnedSource() public pure {
        assertEq(CRON_EMITTER, CRON_SOURCE);
        assertEq(CRON_1000_TOPIC0, uint256(ICronSource.Cron1000.selector));
        assertEq(CRON_10000_TOPIC0, uint256(ICronSource.Cron10000.selector));
        assertEq(CRON_1000_TOPIC0, 0xe20b31294d84c3661ddc8f423abb9c70310d0cf172aa2714ead78029b325e3f4);
        assertEq(CRON_10000_TOPIC0, 0xd214e1d84db704ed42d37f538ea9bf71e44ba28bc1cc088b2f5deca654677a56);
    }

    /// @dev The ABI has each addition of this unit. The tests of the earlier units check the rest.
    function test_abi_hasTheAdditions() public view {
        string memory json = vm.readFile("out/LifelineHub.sol/LifelineHub.json");

        string[5] memory expected =
            ["setCronTopic(uint256)", "resumeCron()", "cronTopic()", "cronSubscribed()", "lastTick()"];
        for (uint256 i = 0; i < expected.length; ++i) {
            assertTrue(
                vm.keyExistsJson(json, string.concat(".methodIdentifiers['", expected[i], "']")),
                string.concat("missing in the ABI: ", expected[i])
            );
        }

        assertEq(LifelineHub.Tick.selector, keccak256("Tick(uint256,uint256,uint256,uint256)"));
        assertFalse(vm.keyExistsJson(json, ".abi[?(@.name == 'pay')]"), "the ABI has pay");
    }

    /// @dev This unit appends one skip reason and reorders none.
    function test_skipReason_keepsTheOrderOfTheEarlierUnits() public pure {
        assertEq(uint256(LifelineHub.SkipReason.Paused), 0);
        assertEq(uint256(LifelineHub.SkipReason.Unhandled), 13);
        assertEq(uint256(LifelineHub.SkipReason.RequestFailed), 15);
        assertEq(uint256(LifelineHub.SkipReason.DuplicateTick), 16);
    }

    function test_ownerFunctions_rejectAnotherCaller() public {
        bytes memory notOwner = abi.encodeWithSelector(LifelineHub.NotOwner.selector);

        vm.startPrank(stranger);
        vm.expectRevert(notOwner);
        hub.setCronTopic(CRON_1000_TOPIC0);
        vm.expectRevert(notOwner);
        hub.resumeCron();
        vm.stopPrank();
    }

    // ------------------------------------------------------------------------------------------
    // The cron tuple
    // ------------------------------------------------------------------------------------------

    /// @dev The constructor subscribes nothing. `resumeCron` subscribes the one cron tuple: Lasna, the cron
    ///      source, the selected `topic0` and a wildcard in each other topic. Before the first `setCronTopic`
    ///      the selected topic is the one of Cron10000.
    function test_resumeCron_subscribesTheSelectedTopicOneTime() public {
        LifelineHub fresh = _deployHub(1 ether, 0.1 ether, 0.1 ether, 1 ether);
        assertEq(fresh.cronTopic(), CRON_10000_TOPIC0, "the default is Cron10000");
        assertFalse(fresh.cronSubscribed(), "the constructor subscribes no cron tuple");
        assertEq(fresh.lastTick(), 0);
        assertEq(_activeSubscriptions(address(fresh)), 0);

        // The hub of `setUp` had one `resumeCron`.
        assertTrue(hub.cronSubscribed());
        assertEq(hub.cronTopic(), CRON_10000_TOPIC0);
        _assertCronTupleLog(0, ISystemContract.Subscribe.selector, CRON_10000_TOPIC0);
        // The debt tuple of the vault and the cron tuple.
        assertEq(_activeSubscriptions(address(hub)), 2);

        // A second call changes nothing.
        uint256 mark = _logMark();
        vm.prank(owner);
        hub.resumeCron();
        assertEq(_countLogsOf(mark, SYSTEM_ADDRESS), 0, "no second Subscribe");
        assertTrue(hub.cronSubscribed());
        assertEq(_activeSubscriptions(address(hub)), 2);
    }

    function test_setCronTopic_acceptsOnlyCron1000AndCron10000() public {
        uint256[5] memory rejected = [
            0,
            REACTIVE_IGNORE,
            uint256(keccak256("Cron100(uint256)")),
            uint256(keccak256("Cron1(uint256)")),
            uint256(IProxyLedger.BlacklistContract.selector)
        ];
        for (uint256 i = 0; i < rejected.length; ++i) {
            vm.expectRevert(abi.encodeWithSelector(LifelineHub.InvalidCronTopic.selector, rejected[i]));
            vm.prank(owner);
            hub.setCronTopic(rejected[i]);
        }
        assertEq(hub.cronTopic(), CRON_10000_TOPIC0);

        vm.startPrank(owner);
        hub.setCronTopic(CRON_1000_TOPIC0);
        assertEq(hub.cronTopic(), CRON_1000_TOPIC0);
        hub.setCronTopic(CRON_10000_TOPIC0);
        assertEq(hub.cronTopic(), CRON_10000_TOPIC0);
        vm.stopPrank();
    }

    /// @dev With a cron tuple subscribed, `setCronTopic` replaces it: one `Unsubscribe` of the old tuple and
    ///      one `Subscribe` of the new one. There is one cron tuple at each time.
    function test_setCronTopic_replacesTheSubscribedTuple() public {
        uint256 mark = _logMark();
        vm.prank(owner);
        hub.setCronTopic(CRON_1000_TOPIC0);

        assertEq(_countLogsOf(mark, SYSTEM_ADDRESS), 2, "one Unsubscribe and one Subscribe");
        _assertCronTupleLog(mark, ISystemContract.Unsubscribe.selector, CRON_10000_TOPIC0);
        _assertCronTupleLog(mark, ISystemContract.Subscribe.selector, CRON_1000_TOPIC0);
        assertLt(
            _findLogIndex(mark, SYSTEM_ADDRESS, ISystemContract.Unsubscribe.selector, 0),
            _findLogIndex(mark, SYSTEM_ADDRESS, ISystemContract.Subscribe.selector, 0),
            "the old tuple ends first"
        );
        assertTrue(hub.cronSubscribed());
        assertEq(hub.cronTopic(), CRON_1000_TOPIC0);
        assertEq(_activeSubscriptions(address(hub)), 2, "the debt tuple of the vault and one cron tuple");

        // The same topic again: nothing changes.
        mark = _logMark();
        vm.prank(owner);
        hub.setCronTopic(CRON_1000_TOPIC0);
        assertEq(_countLogsOf(mark, SYSTEM_ADDRESS), 0);
        assertEq(_activeSubscriptions(address(hub)), 2);
    }

    /// @dev With no cron tuple subscribed, `setCronTopic` only selects. `resumeCron` then subscribes the
    ///      selected topic.
    function test_setCronTopic_withNoTupleOnlySelects() public {
        vm.prank(owner);
        hub.unsubscribeAll();
        assertFalse(hub.cronSubscribed(), "unsubscribeAll ends the cron tuple");
        assertEq(_activeSubscriptions(address(hub)), 0);

        uint256 mark = _logMark();
        vm.prank(owner);
        hub.setCronTopic(CRON_1000_TOPIC0);
        assertEq(_countLogsOf(mark, SYSTEM_ADDRESS), 0, "no Subscribe");
        assertFalse(hub.cronSubscribed());
        assertEq(hub.cronTopic(), CRON_1000_TOPIC0);

        vm.prank(owner);
        hub.resumeCron();
        assertEq(_countLogsOf(mark, SYSTEM_ADDRESS), 1);
        _assertCronTupleLog(mark, ISystemContract.Subscribe.selector, CRON_1000_TOPIC0);
        assertTrue(hub.cronSubscribed());
        assertEq(_activeSubscriptions(address(hub)), 1);
    }

    /// @dev The real path of a tick. The node calls `triggerCron()` on the cron source in a block with a
    ///      number that is a multiple of 10,000. The code of the fork block emits the real cron logs, and the
    ///      harness sends them only to a contract with a matching subscription. So the hub gets the tick only
    ///      if its emitter and its `topic0` are the real ones.
    function test_realCron_theRealCronLogRunsATick() public {
        address[] memory fleet = _enrollUnpaid(2, 0);

        uint256 mark = _logMark();
        uint256 cronBlock = _realCron(10_000);
        assertEq(cronBlock % 10_000, 0);
        assertEq(_countLogs(mark, CRON_SOURCE, ICronSource.Cron10000.selector), 1, "the real Cron10000 log");
        ChainLog memory cronLog = _findLog(mark, CRON_SOURCE, ICronSource.Cron10000.selector, 0);
        assertEq(cronLog.topicCount, CRON_TOPIC_COUNT);
        assertEq(cronLog.topics[1], cronBlock, "the cron log names its block");

        assertEq(_relay(), 1, "the hub is the only subscriber of a cron log");
        TriggerResult memory result = _lastTrigger(address(hub));
        TickLog memory t = _tickOf(result);
        _assertTick(t, cronBlock, 2, 0, 0);
        assertEq(result.blockNumber, cronBlock + 1, "the trigger runs one block after the cron log");
        assertEq(_toppedUp(t.mark, fleet[0]), HIGH);
        assertEq(_toppedUp(t.mark, fleet[1]), HIGH);

        // With Cron10000 selected, a block of Cron1000 wakes nothing.
        mark = _logMark();
        uint256 thousand = _realCron(1_000);
        assertTrue(thousand % 1_000 == 0 && thousand % 10_000 != 0);
        assertEq(_countLogs(mark, CRON_SOURCE, ICronSource.Cron1000.selector), 1, "the real Cron1000 log");
        assertEq(_countLogs(mark, CRON_SOURCE, ICronSource.Cron10000.selector), 0);
        assertEq(_relay(), 0, "no trigger");
        assertEq(hub.lastTick(), cronBlock);

        // With Cron1000 selected, the next block of Cron1000 runs a tick.
        vm.prank(owner);
        hub.setCronTopic(CRON_1000_TOPIC0);
        thousand = _realCron(1_000);
        assertEq(_relay(), 1);
        assertEq(hub.lastTick(), thousand);
    }

    // ------------------------------------------------------------------------------------------
    // Lasna batch
    // ------------------------------------------------------------------------------------------

    function _poke(address canary) internal {
        _toLasna();
        vm.prank(owner);
        CanaryReactive(canary).poke();
        assertEq(_relay(), 1, "only the canary reacts");
    }

    /// @dev The sum of `SelfCare` of the hub after a mark.
    function _selfCareSum(uint256 mark) internal returns (uint256 sum) {
        uint256 count = _hubLogs(mark, LifelineHub.SelfCare.selector);
        for (uint256 i = 0; i < count; ++i) {
            sum += abi.decode(_findLog(mark, address(hub), LifelineHub.SelfCare.selector, i).data, (uint256));
        }
    }

    function _assertState(uint256 chainId, address target, TargetState expected) internal {
        _toLasna();
        (, TargetState state,) = hub.targetInfo(chainId, target);
        assertEq(uint256(state), uint256(expected));
    }

    /// @dev One tick fills each active target below `low` to `high` and pays no other target. One target
    ///      used its reserves in real reactions, two were enrolled below `low`, one is between the marks,
    ///      one is at `high`, and one is parked.
    function test_tick_fillsEachTargetBelowLowAndPaysNoOther() public {
        // A target with reserves of 0 that is not active: `unsubscribeAll` parks each active target.
        address parked = _enrollUnpaid(1, 0)[0];
        vm.startPrank(owner);
        hub.unsubscribeAll();
        hub.resumeCron();
        vm.stopPrank();
        _assertState(LASNA_CHAIN_ID, parked, TargetState.Parked);

        // A healthy target that burns its reserves below `low` with two reactions, and has no debt.
        address burned = _lasnaCanary(0.025 ether);
        _enrollLasna(burned);
        assertEq(system.reserves(burned), 0.025 ether, "a healthy target is not paid at enrollment");
        _poke(burned);
        _poke(burned);
        uint256 burnedReserves = system.reserves(burned);
        assertTrue(burnedReserves > 0 && burnedReserves < LOW, "the reserves are below low");
        assertEq(system.debt(burned), 0, "no debt");

        uint256[] memory reserves = new uint256[](4);
        (reserves[0], reserves[1], reserves[2], reserves[3]) = (0, BELOW, BETWEEN, HIGH);
        address[] memory fleet = _enrollUnpaid(reserves);
        assertEq(hub.targetCount(LASNA_CHAIN_ID), 6);

        uint256 balanceBefore = address(hub).balance;
        // `processed` counts each target of the list, also the parked one.
        TickLog memory t = _tickRan(6, 0, 0);
        assertEq(_hubLogs(t.mark, LifelineHub.ToppedUp.selector), 3, "one ToppedUp for each target below low");
        assertEq(_toppedUp(t.mark, burned), HIGH - burnedReserves);
        assertEq(_toppedUp(t.mark, fleet[0]), HIGH);
        assertEq(_toppedUp(t.mark, fleet[1]), HIGH - BELOW);
        assertEq(system.reserves(burned), HIGH);
        assertEq(system.reserves(fleet[0]), HIGH);
        assertEq(system.reserves(fleet[1]), HIGH);

        assertEq(system.reserves(fleet[2]), BETWEEN, "between the marks: no payment");
        assertEq(system.reserves(fleet[3]), HIGH, "at high: no payment");
        assertEq(system.reserves(parked), 0, "not active: no payment");
        _assertState(LASNA_CHAIN_ID, parked, TargetState.Parked);
        assertEq(_hubLogs(t.mark, LifelineHub.Rescued.selector), 0);
        assertEq(_hubLogs(t.mark, LifelineHub.Skipped.selector), 0, "a target with nothing to pay gives no event");
        assertEq(_hubRequests(t.mark).length, 0);
        assertEq(
            balanceBefore - address(hub).balance,
            (HIGH - burnedReserves) + HIGH + (HIGH - BELOW) + _selfCareSum(t.mark),
            "the three payments and self-care left the balance"
        );

        // The next tick finds no target below low.
        t = _tickRan(6, 0, 0);
        _assertNoPayment(t.mark);
    }

    /// @dev The gauge uses the reconcile path of the airbag: a target that is in debt at a tick is rescued,
    ///      with `Rescued` and `Gap`. No debt log started this rescue, so the gap starts at block 0.
    function test_tick_rescuesATargetThatIsInDebt() public {
        address canary = _lasnaCanary(0);
        _setFloor(type(uint256).max);
        _enrollLasna(canary);
        // The floor is above the balance: the hub gives `HubLow` for the debt log and pays nothing.
        vm.prank(owner);
        CanaryReactive(canary).poke();
        assertEq(_relay(), 2, "the canary and the hub react");
        uint256 debt = system.debt(canary);
        assertGt(debt, 0, "the canary is in debt");
        _setFloor(0);
        vm.prank(owner);
        hub.resumeCron();

        TickLog memory t = _tickRan(1, 0, 0);

        ChainLog memory rescued = _onlyLog(t.mark, address(hub), LifelineHub.Rescued.selector);
        assertEq(rescued.topics[1], uint256(uint160(canary)));
        assertEq(rescued.data, abi.encode(debt, debt + HIGH));
        ChainLog memory gap = _onlyLog(t.mark, address(hub), LifelineHub.Gap.selector);
        assertEq(gap.data, abi.encode(uint256(0), vm.getBlockNumber()));
        assertEq(system.debt(canary), 0);
        assertEq(system.reserves(canary), HIGH);
    }

    /// @dev A fleet of more than `MAX_PER_TICK` targets that all need a top-up: each tick starts at the
    ///      cursor of the tick before, and after enough ticks each target was filled one time and none two
    ///      times.
    function test_cursor_aFleetAboveMaxPerTickCompletesOnTheNextTicks() public {
        uint256 size = MAX_PER_TICK + 5;
        address[] memory fleet = _enrollUnpaid(size, BELOW);
        uint256 cursor;
        uint256 ticks;

        for (uint256 processed = 0; processed < size; ++ticks) {
            uint256[] memory before = _reservesOf(fleet);
            TickLog memory t = _tick();
            _assertHeld(t);
            assertTrue(t.ran, "Tick");
            // The targets from the cursor, and no other target, were filled in this tick.
            _assertBatch(t, fleet, before, cursor);
            processed += t.processed;
            cursor = t.cursor;
        }
        assertGt(ticks, 1, "one tick does not cover the fleet");

        for (uint256 i = 0; i < size; ++i) {
            assertEq(system.reserves(fleet[i]), HIGH);
        }
        // Two more ticks go over each target again and pay nothing.
        for (uint256 i = 0; i < 2; ++i) {
            TickLog memory t = _tick();
            assertEq(t.processed, MAX_PER_TICK);
            assertEq(t.cursor, (cursor + MAX_PER_TICK) % size);
            cursor = t.cursor;
            _assertNoPayment(t.mark);
        }
    }

    /// @dev A tick processes `MAX_PER_TICK` targets of a larger fleet that needs no payment, and the next tick
    ///      continues at the cursor with a wrap at the end of the list.
    function test_cursor_aTickProcessesMaxPerTickTargets() public {
        uint256 size = MAX_PER_TICK + 5;
        _enrollUnpaid(size, BETWEEN);

        _tickRan(MAX_PER_TICK, MAX_PER_TICK, 0);
        _tickRan(MAX_PER_TICK, 2 * MAX_PER_TICK - size, 0);
        _assertNoPayment(0);
    }

    /// @dev `disarm` can make a list shorter than its cursor. The next tick then starts at the first target.
    function test_cursor_startsAtTheFirstTargetWhenTheListIsShorterThanTheCursor() public {
        address[] memory fleet = _enrollUnpaid(MAX_PER_TICK + 2, BETWEEN);
        address[] memory targets = _enrollPlain(DESTINATION_CHAIN_ID, SWEEP_MAX + 2);

        TickLog memory t = _tick();
        assertEq(t.cursor, MAX_PER_TICK);
        _assertSweepRequest(
            _hubRequests(t.mark)[0],
            DESTINATION_CHAIN_ID,
            address(vault),
            SWEEP_BASE,
            t.tickId,
            _window(targets, 0, SWEEP_MAX)
        );

        // The Lasna list gets 11 targets and the destination list gets 7: both are below their cursors.
        for (uint256 i = 0; i < 3; ++i) {
            _disarm(LASNA_CHAIN_ID, fleet[i]);
            _disarm(DESTINATION_CHAIN_ID, targets[i]);
        }
        uint256 size = hub.targetCount(LASNA_CHAIN_ID);
        assertEq(size, MAX_PER_TICK - 1);

        t = _tickRan(size, 0, 1);
        address[] memory list = new address[](SWEEP_MAX - 1);
        for (uint256 i = 0; i < list.length; ++i) {
            list[i] = hub.targetAt(DESTINATION_CHAIN_ID, i);
        }
        _assertSweepRequest(_hubRequests(t.mark)[0], DESTINATION_CHAIN_ID, address(vault), SWEEP_BASE, t.tickId, list);
    }

    // ------------------------------------------------------------------------------------------
    // Sweeps
    // ------------------------------------------------------------------------------------------

    /// @dev One tick sends one request to the vault for its three enrolled targets, and the delivery gives one
    ///      `Result` for each target with the `tickId` as its id. The target below `low` is filled to `high`.
    function test_sweep_oneRequestForTheVaultGivesOneResultForEachTarget() public {
        address[] memory targets = new address[](3);
        targets[0] = _deployCanary();
        _arm(targets[0], _destPolicy());
        targets[1] = _deployCanary();
        _arm(targets[1], _destPolicy());

        // The third target has a low mark. One callback takes its reserves below the mark, with no debt.
        Policy memory policy = _destPolicy();
        policy.low = 2.8e12;
        targets[2] = _deployCanary();
        vm.prank(stranger);
        proxy.depositTo{ value: 3e12 }(targets[2]);
        _arm(targets[2], policy);
        _pong(targets[2]);
        uint256 reserves = proxy.reserves(targets[2]);
        assertLt(reserves, policy.low, "the reserves are below low");
        assertEq(proxy.debt(targets[2]), 0, "no debt");
        uint256 vaultBalance = address(vault).balance;

        (TickLog memory t, ResultLog[] memory results) = _tickAndSweep(targets);
        _assertResult(results[0], t.tickId, targets[0], LifelineVault.Outcome.NoNeed);
        _assertResult(results[1], t.tickId, targets[1], LifelineVault.Outcome.NoNeed);
        _assertResult(results[2], t.tickId, targets[2], LifelineVault.Outcome.ToppedUp);
        assertEq(results[0].paid + results[1].paid, 0);
        assertEq(results[2].paid, DEST_HIGH - reserves, "the vault fills to high");

        _toDestination();
        assertEq(proxy.reserves(targets[2]), DEST_HIGH);
        assertLe(address(vault).balance, vaultBalance - results[2].paid, "the vault paid");
    }

    /// @dev No request goes to a vault with no enrolled target, and none to a vault in state down. The vault
    ///      of the other chain still gets its request.
    function test_sweep_noRequestForAnEmptyListOrADownVault() public {
        _setOtherVault();
        assertEq(hub.vaultChainCount(), 2);

        // Two registered vaults, no enrolled target.
        TickLog memory t = _tickRan(0, 0, 0);
        assertEq(_hubRequests(t.mark).length, 0, "no request for an empty list");

        // The first vault gets targets and goes down: its proxy reports its debt.
        address[] memory targets = _enrollPlain(DESTINATION_CHAIN_ID, 2);
        uint256 mark = _logMark();
        _trigger(address(hub), _debtLogRecord(DESTINATION_CHAIN_ID, PROXY_ADDRESS, address(vault), 1));
        assertEq(_hubLogs(mark, LifelineHub.VaultDown.selector), 1);
        (,,, bool up,) = hub.vaultInfo(DESTINATION_CHAIN_ID);
        assertFalse(up);

        t = _tickRan(0, 0, 0);
        assertEq(_hubRequests(t.mark).length, 0, "no request for a down vault");

        // The second vault gets a target: only that vault gets a request.
        address[] memory others = _enrollPlain(OTHER_CHAIN_ID, 1);
        t = _tickRan(0, 0, 1);
        ICallbackProxy.CallbackConfiguration_V_1_0[] memory requests = _hubRequests(t.mark);
        assertEq(requests.length, 1);
        _assertSweepRequest(requests[0], OTHER_CHAIN_ID, otherVault, OTHER_SWEEP_BASE, t.tickId, others);

        // After `vaultUp` each vault gets its request.
        vm.prank(owner);
        hub.vaultUp(DESTINATION_CHAIN_ID);
        t = _tickRan(0, 0, 2);
        requests = _hubRequests(t.mark);
        assertEq(requests.length, 2);
        _assertSweepRequest(requests[0], DESTINATION_CHAIN_ID, address(vault), SWEEP_BASE, t.tickId, targets);
        _assertSweepRequest(requests[1], OTHER_CHAIN_ID, otherVault, OTHER_SWEEP_BASE, t.tickId, others);
    }

    /// @dev A chain with 10 enrolled targets: the first tick sends targets 1 to 8, and each next tick sends
    ///      the next 8 from the cursor of that vault, with a wrap at the end of the list.
    function test_sweep_moreThanEightTargetsContinueAtTheVaultCursor() public {
        address[] memory canaries = _armedCanaries(SWEEP_MAX + 2);
        uint256 first;
        for (uint256 tick = 0; tick < 3; ++tick) {
            // Tick 1: targets 1-8. Tick 2: targets 9, 10 and 1-6. Tick 3: targets 7-10 and 1-4.
            address[] memory expected = _window(canaries, first, SWEEP_MAX);
            (TickLog memory t, ResultLog[] memory results) = _tickAndSweep(expected);
            for (uint256 i = 0; i < results.length; ++i) {
                _assertResult(results[i], t.tickId, expected[i], LifelineVault.Outcome.NoNeed);
            }
            first = (first + SWEEP_MAX) % canaries.length;
        }
        assertEq(first, 4);
    }

    /// @dev A cursor of a vault moves only when the request was sent. The real system contract never rejects
    ///      a request, so the test replaces that one answer.
    function test_sweep_theVaultCursorMovesOnlyWhenTheRequestWasSent() public {
        address[] memory targets = _enrollPlain(DESTINATION_CHAIN_ID, SWEEP_MAX + 2);

        vm.mockCallRevert(SYSTEM_ADDRESS, abi.encodeWithSelector(ISystemContract.requestCallbackV_1_0.selector), "");
        TickLog memory t = _tickRan(0, 0, 0);
        vm.clearMockedCalls();
        assertEq(_hubRequests(t.mark).length, 0);

        t = _tickRan(0, 0, 1);
        _assertSweepRequest(
            _hubRequests(t.mark)[0],
            DESTINATION_CHAIN_ID,
            address(vault),
            SWEEP_BASE,
            t.tickId,
            _window(targets, 0, SWEEP_MAX)
        );
    }

    /// @dev Spec §4.3: the hub does not know the quarantine state of the vault. A target that the vault
    ///      quarantined stays in the payload of each tick and gets `Result(tickId, target, Quarantined, 0)`.
    ///      After `hub.disarm` it is in no payload.
    function test_sweep_aTargetQuarantinedOnTheVaultStaysInThePayloadUntilDisarm() public {
        // `maxRescues` 1 and `high` below one fee: the second debt inside the window is a quarantine.
        Policy memory loop = _destPolicy();
        loop.high = DEST_HIGH_BELOW_ONE_FEE;
        loop.maxRescues = 1;
        address[] memory targets = new address[](2);
        targets[0] = _deployCanary();
        _arm(targets[0], loop);
        targets[1] = _deployCanary();
        _arm(targets[1], _destPolicy());
        address broken = targets[0];

        // The first debt: the hub relays and the vault rescues. The second debt: the vault quarantines.
        for (uint256 i = 0; i < 2; ++i) {
            assertTrue(_wentIntoDebt(_pong(broken).logs, broken), "BlacklistContract(broken)");
            (uint256 triggers, uint256 deliveries) = _relayAll();
            assertEq(triggers, 1, "the hub relays the debt");
            assertEq(deliveries, 1);
            _toDestination();
            assertEq(vault.quarantined(broken), i == 1);
        }
        uint256 debt = proxy.debt(broken);
        assertGt(debt, 0, "the vault did not pay the second debt");
        _assertState(DESTINATION_CHAIN_ID, broken, TargetState.Active);

        for (uint256 i = 0; i < 2; ++i) {
            (TickLog memory t, ResultLog[] memory results) = _tickAndSweep(targets);
            _assertResult(results[0], t.tickId, broken, LifelineVault.Outcome.Quarantined);
            assertEq(results[0].paid, 0);
            _assertResult(results[1], t.tickId, targets[1], LifelineVault.Outcome.NoNeed);
            _toDestination();
            assertEq(proxy.debt(broken), debt, "no payment to the quarantined target");
        }

        _disarm(DESTINATION_CHAIN_ID, broken);
        address[] memory rest = new address[](1);
        rest[0] = targets[1];
        (, ResultLog[] memory last) = _tickAndSweep(rest);
        assertEq(uint256(last[0].outcome), uint256(LifelineVault.Outcome.NoNeed));
    }

    /// @dev With more vaults than the gas of one tick covers, the tail stops before the gas runs out, and
    ///      the next tick starts at the vault that got no request.
    function test_tail_stopsEarlyAndTheNextTickStartsAtTheNextVault() public {
        uint256 vaults = 6;
        uint256[] memory chains = new uint256[](vaults);
        chains[0] = DESTINATION_CHAIN_ID;
        _enrollPlain(DESTINATION_CHAIN_ID, SWEEP_MAX + 2);
        for (uint256 i = 1; i < vaults; ++i) {
            chains[i] = OTHER_CHAIN_ID + i;
            vm.prank(owner);
            hub.setVault(chains[i], address(uint160(0x1000 + i)), address(uint160(0x2000 + i)), OTHER_SWEEP_BASE);
            _enrollPlain(chains[i], SWEEP_MAX + 2);
        }
        // The loop of the first ticks stops at its guard: the tail starts with the least gas.
        _enrollUnpaid(MAX_PER_TICK, 0);

        uint256[] memory served = new uint256[](vaults);
        uint256 next;
        bool stoppedEarly;
        for (uint256 tick = 0; tick < 4; ++tick) {
            TickLog memory t = _tick();
            _assertHeld(t);
            assertTrue(t.ran);
            assertGe(t.sweeps, 2, "the tail reserve covers two vaults");

            ICallbackProxy.CallbackConfiguration_V_1_0[] memory requests = _hubRequests(t.mark);
            assertEq(requests.length, t.sweeps);
            for (uint256 k = 0; k < requests.length; ++k) {
                uint256 index = (next + k) % vaults;
                assertEq(requests[k].chainId, chains[index], "the tail starts at the vault that got no request");
                ++served[index];
            }
            if (t.sweeps < vaults) {
                stoppedEarly = true;
                next = (next + t.sweeps) % vaults;
            }
        }
        assertTrue(stoppedEarly, "a tail stopped early");
        for (uint256 i = 0; i < vaults; ++i) {
            assertGe(served[i], 1, "each vault was served");
        }
    }

    // ------------------------------------------------------------------------------------------
    // Tick identity
    // ------------------------------------------------------------------------------------------

    /// @dev The same cron log record relayed two times runs one time: the second gives a skip, no payment and
    ///      no request. A record with an older origin block is a skip too.
    function test_duplicateTick_theSameCronLogRunsOneTime() public {
        // More targets below `low` than one tick fills, and a destination target: a second run would pay
        // and would send a request.
        address[] memory fleet = _enrollUnpaid(MAX_PER_TICK + 2, 0);
        _enrollPlain(DESTINATION_CHAIN_ID, 1);
        IReactive.LogRecord memory record = _cronRecord(CRON_10000_TOPIC0, _originBlock());

        TickLog memory first = _sendCron(record);
        _assertHeld(first);
        assertTrue(first.ran);
        assertEq(first.tickId, record.blockNumber);
        assertEq(first.sweeps, 1);
        assertLt(first.processed, fleet.length, "targets below low are left");
        assertEq(system.reserves(fleet[fleet.length - 1]), 0);

        uint256 balance = address(hub).balance;
        TickLog memory second = _sendCron(record);
        _assertNoTick(second, record.blockNumber, LifelineHub.SkipReason.DuplicateTick);
        _assertNoPayment(second.mark);
        assertEq(_hubRequests(second.mark).length, 0, "no request");
        assertEq(balance - address(hub).balance, _selfCareSum(second.mark), "only self-care left the balance");
        assertEq(hub.lastTick(), record.blockNumber);
        assertEq(system.reserves(fleet[fleet.length - 1]), 0);

        TickLog memory older = _sendCron(_cronRecord(CRON_10000_TOPIC0, record.blockNumber - 1));
        _assertNoTick(older, record.blockNumber - 1, LifelineHub.SkipReason.DuplicateTick);
        _assertNoPayment(older.mark);

        // A record with a higher origin block continues at the cursor.
        TickLog memory newer = _sendCron(_cronRecord(CRON_10000_TOPIC0, record.blockNumber + 1));
        _assertHeld(newer);
        assertTrue(newer.ran);
        assertEq(newer.cursor, (first.cursor + newer.processed) % fleet.length);
        assertEq(system.reserves(fleet[fleet.length - 1]), HIGH);
    }

    /// @dev The key of a tick is the origin block of its cron log, not `block.number`: two cron records with
    ///      different origin blocks that the node relays in one block both run.
    function test_duplicateTick_twoOriginBlocksRelayedInOneBlockBothRun() public {
        _enrollUnpaid(2, BETWEEN);
        _enrollPlain(DESTINATION_CHAIN_ID, 1);
        uint256 origin = _originBlock();

        TickLog memory first = _sendCron(_cronRecord(CRON_10000_TOPIC0, origin - 10_000));
        _assertTick(first, origin - 10_000, 2, 0, 1);
        uint256 blockNumber = vm.getBlockNumber();

        // The second trigger mines no block.
        TickLog memory second = _tickOf(_triggerInBlock(address(hub), _cronRecord(CRON_10000_TOPIC0, origin)));
        assertEq(vm.getBlockNumber(), blockNumber, "the same block");
        _assertTick(second, origin, 2, 0, 1);
        assertEq(_hubRequests(second.mark).length, 1);

        // The second record again, still in that block: a skip.
        TickLog memory third = _tickOf(_triggerInBlock(address(hub), _cronRecord(CRON_10000_TOPIC0, origin)));
        assertEq(vm.getBlockNumber(), blockNumber);
        _assertNoTick(third, origin, LifelineHub.SkipReason.DuplicateTick);
    }

    /// @dev A block number of Lasna has far fewer than 192 bits. A record with a larger origin block is a
    ///      skip and is not stored, so it cannot stop the later ticks.
    function test_duplicateTick_anOriginBlockAbove192BitsIsASkip() public {
        uint256 tooLarge = uint256(type(uint192).max) + 1;
        TickLog memory t = _sendCron(_cronRecord(CRON_10000_TOPIC0, tooLarge));
        _assertNoTick(t, tooLarge, LifelineHub.SkipReason.DuplicateTick);
        assertEq(hub.lastTick(), 0);

        _tickRan(0, 0, 0);
    }

    /// @dev With Cron10000 selected, a Cron1000 record runs no tick. The record does not match the registered
    ///      cron tuple, so the dispatch of the earlier unit skips it. A record with the cron topic from the
    ///      ledger address is no cron log.
    function test_wrongTopic_aCronLogOfTheOtherPeriodRunsNoTick() public {
        address[] memory fleet = _enrollUnpaid(1, 0);
        _enrollPlain(DESTINATION_CHAIN_ID, 1);
        uint256 origin = _originBlock();

        TickLog memory t = _sendCron(_cronRecord(CRON_1000_TOPIC0, origin));
        // The reason of the dispatch for a Lasna log that is not from the ledger and is no cron log.
        _assertNoTick(t, origin, LifelineHub.SkipReason.WrongEmitter);
        _assertNoPayment(t.mark);
        assertEq(_hubRequests(t.mark).length, 0);

        IReactive.LogRecord memory fromLedger = _cronRecord(CRON_10000_TOPIC0, origin);
        fromLedger.contractAddress = SYSTEM_ADDRESS;
        _assertNoTick(_sendCron(fromLedger), origin, LifelineHub.SkipReason.UnknownTopic);

        assertEq(hub.lastTick(), 0);
        assertEq(system.reserves(fleet[0]), 0);

        // The same origin block with the selected topic runs.
        t = _sendCron(_cronRecord(CRON_10000_TOPIC0, origin));
        _assertTick(t, origin, 1, 0, 1);
        assertEq(system.reserves(fleet[0]), HIGH);

        // With Cron1000 selected, a Cron10000 record runs no tick and a Cron1000 record runs.
        vm.prank(owner);
        hub.setCronTopic(CRON_1000_TOPIC0);
        t = _sendCron(_cronRecord(CRON_10000_TOPIC0, origin + 1));
        _assertNoTick(t, origin + 1, LifelineHub.SkipReason.WrongEmitter);
        t = _sendCron(_cronRecord(CRON_1000_TOPIC0, origin + 1));
        _assertTick(t, origin + 1, 1, 0, 1);
    }

    /// @dev A paused hub skips a cron log and stores no tick: the same record runs after `unpause`.
    function test_paused_aCronLogIsASkip() public {
        address[] memory fleet = _enrollUnpaid(1, 0);
        IReactive.LogRecord memory record = _cronRecord(CRON_10000_TOPIC0, _originBlock());

        vm.prank(owner);
        hub.pause();
        TickLog memory t = _sendCron(record);
        _assertNoTick(t, record.blockNumber, LifelineHub.SkipReason.Paused);
        assertEq(system.reserves(fleet[0]), 0);
        assertTrue(hub.cronSubscribed(), "a pause keeps the cron tuple");
        assertEq(hub.lastTick(), 0);

        vm.prank(owner);
        hub.unpause();
        _assertTick(_sendCron(record), record.blockNumber, 1, 0, 0);
        assertEq(system.reserves(fleet[0]), HIGH);
    }

    // ------------------------------------------------------------------------------------------
    // Park and resume
    // ------------------------------------------------------------------------------------------

    /// @dev With the balance below `principalFloor`, a cron tick gives `HubLow` and the hub unsubscribes the
    ///      cron tuple: the park of the earlier unit. After funding, `resumeCron` subscribes the tuple again
    ///      and the next tick runs.
    function test_park_hubLowUnsubscribesTheCronTuple_resumeCronRestoresIt() public {
        address[] memory fleet = _enrollUnpaid(1, 0);
        uint256 balance = address(hub).balance;
        _setFloor(balance + 1);
        // The debt tuple of the vault, the debt tuple of the target and the cron tuple.
        assertEq(_activeSubscriptions(address(hub)), 3);

        TickLog memory t = _tick();
        _assertHeld(t);
        assertFalse(t.ran, "no Tick");
        assertEq(_countLogsOf(t.mark, address(hub)), 1, "exactly one event");
        ChainLog memory hubLow = _onlyLog(t.mark, address(hub), LifelineHub.HubLow.selector);
        assertEq(hubLow.data, abi.encode(balance, balance + 1));
        assertEq(_countLogs(t.mark, SYSTEM_ADDRESS, ISystemContract.Unsubscribe.selector), 1, "one tuple ends");
        _assertCronTupleLog(t.mark, ISystemContract.Unsubscribe.selector, CRON_10000_TOPIC0);
        assertFalse(hub.cronSubscribed());
        assertEq(_activeSubscriptions(address(hub)), 2);
        assertEq(hub.lastTick(), 0);
        assertEq(system.reserves(fleet[0]), 0, "the hub pays nothing in this state");
        assertEq(address(hub).balance, balance);

        // Parked: the real cron log of the next block of Cron10000 wakes nothing.
        _realCron(10_000);
        assertEq(_relay(), 0, "no trigger while parked");

        // Funding, then the owner's resume.
        vm.prank(stranger);
        (bool ok,) = address(hub).call{ value: 1 ether }("");
        assertTrue(ok);
        uint256 mark = _logMark();
        vm.prank(owner);
        hub.resumeCron();
        assertEq(_countLogsOf(mark, SYSTEM_ADDRESS), 1);
        _assertCronTupleLog(mark, ISystemContract.Subscribe.selector, CRON_10000_TOPIC0);
        assertTrue(hub.cronSubscribed());
        assertEq(_activeSubscriptions(address(hub)), 3);

        // The next tick runs: the real cron log reaches the hub again.
        uint256 cronBlock = _realCron(10_000);
        assertEq(_relay(), 1, "the hub reacts to the cron log");
        t = _tickOf(_lastTrigger(address(hub)));
        _assertTick(t, cronBlock, 1, 0, 0);
        assertEq(_toppedUp(t.mark, fleet[0]), HIGH);
    }

    // ------------------------------------------------------------------------------------------
    // The gas budget of a tick
    // ------------------------------------------------------------------------------------------

    /// @notice The cursors of the hub, as a test follows them from the `Tick` logs and the requests.
    struct Cursors {
        uint256 fleet;
        uint256 first; // the cursor of the vault of `DESTINATION_CHAIN_ID`
        uint256 second; // the cursor of the vault of `OTHER_CHAIN_ID`
    }

    function _reservesOf(address[] memory fleet) internal returns (uint256[] memory reserves) {
        _toLasna();
        reserves = new uint256[](fleet.length);
        for (uint256 i = 0; i < fleet.length; ++i) {
            reserves[i] = system.reserves(fleet[i]);
        }
    }

    /// @dev A cron log record with zero bytes of data. The hub does not read the data: the bytes only take
    ///      gas from the 900,000 of the trigger, so they move the position of each gas guard.
    function _paddedRecord(uint256 pad) internal returns (IReactive.LogRecord memory record) {
        record = _cronRecord(CRON_10000_TOPIC0, _originBlock());
        record.data = new bytes(pad);
    }

    /// @dev Checks the Lasna batch of a tick against the reserves before it: the cursor moved by exactly
    ///      `processed`, each target below `low` among the `processed` targets from the old cursor has
    ///      reserves of `high`, and no other target was paid.
    function _assertBatch(TickLog memory t, address[] memory fleet, uint256[] memory before, uint256 cursor) internal {
        uint256 size = fleet.length;
        assertLe(t.processed, size < MAX_PER_TICK ? size : MAX_PER_TICK, "at most MAX_PER_TICK targets");
        if (size == 0) {
            assertEq(t.cursor, 0);
            return;
        }
        assertGe(t.processed, 1, "a tick processes a target");
        assertEq(t.cursor, (cursor + t.processed) % size, "the cursor moved by exactly the number processed");

        uint256 fills;
        for (uint256 k = 0; k < size; ++k) {
            uint256 index = (cursor + k) % size;
            uint256 reserves = system.reserves(fleet[index]);
            if (k < t.processed && before[index] < LOW) {
                assertEq(reserves, HIGH, "a processed target below low is at high");
                ++fills;
            } else {
                assertEq(reserves, before[index], "no other target was paid");
            }
        }
        assertEq(_hubLogs(t.mark, LifelineHub.ToppedUp.selector), fills, "one ToppedUp for each payment");
    }

    /// @dev Checks the tail of a tick with two vaults: exactly two requests, each with the next targets of
    ///      its chain from the cursor. Moves the two cursors.
    function _assertTwoSweeps(TickLog memory t, address[] memory first, address[] memory second, Cursors memory c)
        internal
    {
        assertEq(t.sweeps, 2, "Tick reports two sweeps");
        ICallbackProxy.CallbackConfiguration_V_1_0[] memory requests = _hubRequests(t.mark);
        assertEq(requests.length, 2, "the two sweep requests are emitted");

        uint256 n = first.length < SWEEP_MAX ? first.length : SWEEP_MAX;
        _assertSweepRequest(
            requests[0], DESTINATION_CHAIN_ID, address(vault), SWEEP_BASE, t.tickId, _window(first, c.first, n)
        );
        if (first.length > SWEEP_MAX) c.first = (c.first + n) % first.length;

        n = second.length < SWEEP_MAX ? second.length : SWEEP_MAX;
        _assertSweepRequest(
            requests[1], OTHER_CHAIN_ID, otherVault, OTHER_SWEEP_BASE, t.tickId, _window(second, c.second, n)
        );
        if (second.length > SWEEP_MAX) c.second = (c.second + n) % second.length;
    }

    /// @dev Spec §4.9. For any fleet size and any mix of empty targets, with two registered vaults that have
    ///      enrolled targets: the trigger succeeds inside its 900,000 gas, the system contract reports no
    ///      reverted reaction, the cursor moves by exactly the number processed, and the two sweep requests
    ///      are emitted. The second tick continues at the cursors.
    /// @param fleetSize The number of Lasna targets.
    /// @param kinds Two bits for each target: 0 is between the marks, 1 is below `low` with reserves above 0,
    ///        2 and 3 are empty.
    /// @param firstTargets The enrolled targets of the first vault.
    /// @param secondTargets The enrolled targets of the second vault.
    /// @param pad The bytes of zero data in the record: the position of the gas guards.
    /// forge-config: default.fuzz.runs = 256
    function test_gaugeNeverRunsOutOfGas(
        uint256 fleetSize,
        uint256 kinds,
        uint256 firstTargets,
        uint256 secondTargets,
        uint256 pad
    ) public {
        fleetSize = bound(fleetSize, 0, 3 * MAX_PER_TICK);
        firstTargets = bound(firstTargets, 1, SWEEP_MAX + 4);
        secondTargets = bound(secondTargets, 1, SWEEP_MAX + 4);
        pad = bound(pad, 0, PAD_STEP * PAD_STEPS);

        uint256[] memory reserves = new uint256[](fleetSize);
        for (uint256 i = 0; i < fleetSize; ++i) {
            uint256 kind = (kinds >> (2 * i)) & 3;
            reserves[i] = kind == 0 ? BETWEEN : kind == 1 ? BELOW : 0;
        }
        address[] memory fleet = _enrollUnpaid(reserves);
        _setOtherVault();
        address[] memory first = _enrollPlain(DESTINATION_CHAIN_ID, firstTargets);
        address[] memory second = _enrollPlain(OTHER_CHAIN_ID, secondTargets);

        Cursors memory c;
        for (uint256 tick = 0; tick < 2; ++tick) {
            uint256[] memory before = _reservesOf(fleet);
            IReactive.LogRecord memory record = _paddedRecord(pad);
            TickLog memory t = _sendCron(record);

            _assertHeld(t);
            assertTrue(t.ran, "Tick");
            assertEq(t.tickId, record.blockNumber);
            _assertBatch(t, fleet, before, c.fleet);
            c.fleet = t.cursor;
            _assertTwoSweeps(t, first, second, c);
        }
    }

    /// @dev The most expensive tick, at each position of the gas guards. Each Lasna target is empty and gets
    ///      its first payment, each of the two vaults has more than `SWEEP_MAX` targets and writes its cursor
    ///      for the first time, and the hub writes its first tick. The data bytes of the record move the gas
    ///      of `react()` in steps of about 1,100 over more than the cost of one target, so one step puts the
    ///      loop guard at its worst position. Each tick must succeed and must send the two requests.
    function test_gaugeNeverRunsOutOfGas_theMostExpensiveTickAtEachGuardPosition() public {
        address[] memory fleet = _enrollUnpaid(MAX_PER_TICK, 0);
        _setOtherVault();
        address[] memory first = _enrollPlain(DESTINATION_CHAIN_ID, SWEEP_MAX + 2);
        address[] memory second = _enrollPlain(OTHER_CHAIN_ID, SWEEP_MAX + 2);
        uint256[] memory before = _reservesOf(fleet);

        uint256 snapshot = vm.snapshotState();
        for (uint256 step = 0; step <= PAD_STEPS; ++step) {
            TickLog memory t = _sendCron(_paddedRecord(step * PAD_STEP));
            _assertHeld(t);
            assertTrue(t.ran, "Tick");
            assertLt(t.processed, MAX_PER_TICK, "the loop guard stopped the batch");
            _assertBatch(t, fleet, before, 0);
            Cursors memory c;
            _assertTwoSweeps(t, first, second, c);
            assertTrue(vm.revertToState(snapshot));
        }
    }

    /// @dev The gas of the largest tick, with records that have no data. The fleet has `healthy` targets
    ///      between the marks and then empty targets: each count of healthy targets puts the loop guard at
    ///      another position. Each of the two vaults has more than `SWEEP_MAX` targets. The test prints the
    ///      largest gas of a trigger transaction, as the harness measures it.
    function test_gaugeNeverRunsOutOfGas_theLargestTickOfEachFleetMix() public {
        _setOtherVault();
        address[] memory first = _enrollPlain(DESTINATION_CHAIN_ID, SWEEP_MAX + 2);
        address[] memory second = _enrollPlain(OTHER_CHAIN_ID, SWEEP_MAX + 2);

        uint256 largestGas;
        uint256 largestMix;
        uint256 snapshot = vm.snapshotState();
        for (uint256 healthy = 0; healthy < MAX_PER_TICK; ++healthy) {
            uint256[] memory reserves = new uint256[](2 * MAX_PER_TICK);
            for (uint256 i = 0; i < healthy; ++i) {
                reserves[i] = BETWEEN;
            }
            address[] memory fleet = _enrollUnpaid(reserves);

            TickLog memory t = _sendCron(_paddedRecord(0));
            _assertHeld(t);
            assertTrue(t.ran, "Tick");
            _assertBatch(t, fleet, reserves, 0);
            Cursors memory c;
            _assertTwoSweeps(t, first, second, c);
            assertLe(t.gasUsed, TRIGGER_GAS, "inside the gas of a trigger");

            if (t.gasUsed > largestGas) (largestGas, largestMix) = (t.gasUsed, healthy);
            assertTrue(vm.revertToState(snapshot));
        }
        console2.log("the largest tick: gas of the trigger transaction", largestGas);
        console2.log("the healthy targets before the empty targets in that tick", largestMix);
    }

    /// @dev The end of a reaction at its most expensive, at each position of the gas guards. The float of
    ///      this hub is below the fee of a reaction, and the daily cap lets self-care add only a small part:
    ///      the tick makes its first write, self-care makes its first deposit, and the system contract then
    ///      puts the hub into debt, which takes more gas after `react()` than a charge from the float. The
    ///      trigger transaction must still succeed: a failed trigger would lose each payment of the tick.
    /// @param feeFloat The float of the hub before the tick. With a float of 0, the deposit of self-care is
    ///        also the first write of the reserves of the hub.
    function _mostExpensiveEndAtEachGuardPosition(uint256 feeFloat) internal {
        hub = _deployHub(HUB_VALUE, feeFloat, 1 ether, 0.002 ether);
        vm.startPrank(owner);
        hub.setVault(DESTINATION_CHAIN_ID, PROXY_ADDRESS, address(vault), SWEEP_BASE);
        hub.setVault(OTHER_CHAIN_ID, otherProxy, otherVault, OTHER_SWEEP_BASE);
        hub.setVault(OTHER_CHAIN_ID + 1, address(0x1001), address(0x2001), OTHER_SWEEP_BASE);
        hub.resumeCron();
        vm.stopPrank();
        assertEq(system.reserves(address(hub)), feeFloat);

        address[] memory fleet = _enrollUnpaid(MAX_PER_TICK, 0);
        _enrollPlain(DESTINATION_CHAIN_ID, SWEEP_MAX + 2);
        _enrollPlain(OTHER_CHAIN_ID, SWEEP_MAX + 2);
        // A third vault: the tail stops at its own guard when the gas does not cover this vault.
        _enrollPlain(OTHER_CHAIN_ID + 1, SWEEP_MAX + 2);
        uint256[] memory before = _reservesOf(fleet);

        uint256 snapshot = vm.snapshotState();
        for (uint256 step = 0; step <= PAD_STEPS; ++step) {
            TickLog memory t = _sendCron(_paddedRecord(step * PAD_STEP));
            assertTrue(t.success, "the trigger succeeded inside 900,000 gas");
            assertFalse(t.reactReverted, "no reverted reaction");
            assertTrue(t.ran, "Tick");
            _assertBatch(t, fleet, before, 0);
            assertGe(t.sweeps, 2, "the tail reserve covers two vaults");
            assertEq(_hubRequests(t.mark).length, t.sweeps);

            // The expensive end did happen: a first self-care deposit, and then a debt of the hub.
            assertEq(_selfCareSum(t.mark), 0.002 ether, "self-care added its daily cap");
            assertGt(system.debt(address(hub)), 0, "the float did not cover the fee");
            assertTrue(vm.revertToState(snapshot));
        }
    }

    function test_gaugeNeverRunsOutOfGas_theMostExpensiveEndAtEachGuardPosition() public {
        _mostExpensiveEndAtEachGuardPosition(0.001 ether);
    }

    function test_gaugeNeverRunsOutOfGas_theMostExpensiveEndWithAFloatOfZero() public {
        _mostExpensiveEndAtEachGuardPosition(0);
    }

    /// @dev A fleet in which each target is in debt at the tick, because the hub could not pay when the debt
    ///      logs came. A target at an even position gets its first rescue. A target at an odd position has
    ///      `maxRescues` 1 and one rescue inside its window: the tick quarantines it.
    function _fleetInDebt() internal returns (address[] memory fleet, uint256[] memory debts) {
        fleet = new address[](MAX_PER_TICK);
        _setFloor(type(uint256).max);
        for (uint256 i = 0; i < fleet.length; ++i) {
            fleet[i] = _lasnaCanary(0);
            Policy memory policy = _lasnaPolicy();
            if (i % 2 == 1) policy.maxRescues = 1;
            vm.prank(owner);
            hub.enroll(LASNA_CHAIN_ID, fleet[i], policy);
        }
        _setFloor(0);

        // The first debt of each odd target: the airbag rescues it.
        vm.startPrank(owner);
        for (uint256 i = 1; i < fleet.length; i += 2) {
            CanaryReactive(fleet[i]).poke();
        }
        vm.stopPrank();
        assertEq(_relay(), fleet.length, "each odd canary reacts, and the hub rescues each of them");

        // Each target goes into debt while the floor is above the balance: the hub pays nothing. The reserves
        // of an odd target cover one more reaction, and not two.
        _setFloor(type(uint256).max);
        vm.startPrank(owner);
        for (uint256 i = 0; i < fleet.length; ++i) {
            CanaryReactive(fleet[i]).poke();
            if (i % 2 == 1) CanaryReactive(fleet[i]).poke();
        }
        vm.stopPrank();
        _relay();
        _setFloor(0);
        vm.prank(owner);
        hub.resumeCron();

        debts = new uint256[](fleet.length);
        for (uint256 i = 0; i < fleet.length; ++i) {
            debts[i] = system.debt(fleet[i]);
            assertGt(debts[i], 0, "each target is in debt");
            _assertState(LASNA_CHAIN_ID, fleet[i], TargetState.Active);
        }
        assertEq(system.debt(address(hub)), 0);
    }

    /// @dev Checks the batch of a tick over the fleet of `_fleetInDebt`: the cursor moved by exactly
    ///      `processed`, each target that the tick processed for the first time was rescued (even position) or
    ///      quarantined and not paid (odd position), and no target after the cursor changed.
    /// @param done The targets that an earlier tick processed. The function adds the targets of this tick.
    function _assertDebtBatch(
        TickLog memory t,
        address[] memory fleet,
        uint256[] memory debts,
        uint256 cursor,
        bool[] memory done
    ) internal {
        uint256 size = fleet.length;
        assertTrue(t.processed != 0 && t.processed <= size, "a tick processes a target");
        assertEq(t.cursor, (cursor + t.processed) % size, "the cursor moved by exactly the number processed");

        uint256 rescues;
        uint256 quarantines;
        for (uint256 k = 0; k < size; ++k) {
            uint256 index = (cursor + k) % size;
            (, TargetState state,) = hub.targetInfo(LASNA_CHAIN_ID, fleet[index]);
            if (k < t.processed && !done[index]) {
                done[index] = true;
                if (index % 2 == 0) ++rescues;
                else ++quarantines;
            }

            if (!done[index]) {
                assertEq(system.debt(fleet[index]), debts[index], "a target after the cursor did not change");
                assertEq(uint256(state), uint256(TargetState.Active));
            } else if (index % 2 == 0) {
                assertEq(system.debt(fleet[index]), 0, "a rescue clears the debt");
                assertEq(system.reserves(fleet[index]), HIGH);
            } else {
                assertEq(uint256(state), uint256(TargetState.Quarantined));
                assertEq(system.debt(fleet[index]), debts[index], "no payment to a quarantined target");
            }
        }
        assertEq(_hubLogs(t.mark, LifelineHub.Rescued.selector), rescues, "one Rescued for each new rescue");
        assertEq(_hubLogs(t.mark, LifelineHub.Quarantined.selector), quarantines, "one Quarantined for each");
        assertEq(_hubLogs(t.mark, LifelineHub.ToppedUp.selector), 0);
    }

    /// @dev A rescue takes more gas than a first payment to an empty target, and a quarantine also
    ///      unsubscribes a tuple. A batch of them, at each position of the gas guards: the trigger succeeds
    ///      and the cursor moves by the number processed. The loop reserve is sized for top-ups, so after such
    ///      a target the tail can stop before the second vault: the test accepts one request, and the next
    ///      ticks continue at the cursors.
    function test_gaugeNeverRunsOutOfGas_aBatchOfRescuesAndQuarantinesAtEachGuardPosition() public {
        (address[] memory fleet, uint256[] memory debts) = _fleetInDebt();
        _setOtherVault();
        _enrollPlain(DESTINATION_CHAIN_ID, SWEEP_MAX + 2);
        _enrollPlain(OTHER_CHAIN_ID, SWEEP_MAX + 2);

        uint256 fewestSweeps = type(uint256).max;
        uint256 snapshot = vm.snapshotState();
        for (uint256 step = 0; step <= PAD_STEPS; ++step) {
            TickLog memory t = _sendCron(_paddedRecord(step * PAD_STEP));
            _assertHeld(t);
            assertTrue(t.ran, "Tick");
            _assertDebtBatch(t, fleet, debts, 0, new bool[](fleet.length));
            assertGe(t.sweeps, 1, "the tail sends a request");
            assertEq(_hubRequests(t.mark).length, t.sweeps);
            if (t.sweeps < fewestSweeps) fewestSweeps = t.sweeps;
            assertTrue(vm.revertToState(snapshot));
        }
        console2.log("the fewest sweep requests of one of these ticks", fewestSweeps);

        // The next ticks continue at the cursors until each target was processed. A tail that stopped early
        // makes the next tail start at the vault that got no request.
        uint256[2] memory chains = [DESTINATION_CHAIN_ID, OTHER_CHAIN_ID];
        uint256[2] memory served;
        bool[] memory done = new bool[](fleet.length);
        uint256 cursor;
        uint256 nextVault;
        for (uint256 processed = 0; processed < fleet.length;) {
            TickLog memory t = _tick();
            _assertHeld(t);
            assertTrue(t.ran, "Tick");
            _assertDebtBatch(t, fleet, debts, cursor, done);
            (processed, cursor) = (processed + t.processed, t.cursor);

            ICallbackProxy.CallbackConfiguration_V_1_0[] memory requests = _hubRequests(t.mark);
            assertEq(requests.length, t.sweeps);
            assertGe(t.sweeps, 1);
            for (uint256 k = 0; k < requests.length; ++k) {
                assertEq(requests[k].chainId, chains[(nextVault + k) % 2], "the tail starts at the next vault");
                ++served[(nextVault + k) % 2];
            }
            if (t.sweeps < 2) nextVault = (nextVault + t.sweeps) % 2;
        }
        assertTrue(served[0] != 0 && served[1] != 0, "each vault was served");

        // Each target is rescued or quarantined: one more tick pays nothing and sends the two requests.
        TickLog memory last = _tickRan(MAX_PER_TICK, cursor, 2);
        _assertNoPayment(last.mark);
        assertEq(_hubLogs(last.mark, LifelineHub.Quarantined.selector), 0);
    }
}
