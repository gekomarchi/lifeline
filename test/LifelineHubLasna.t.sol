// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.30;

import { IReactive } from "@reactive/src/interfaces/IReactive.sol";
import { LasnaForkHarness, ISystemContractNode } from "./harness/LasnaForkHarness.sol";
import { LifelineHub } from "../src/LifelineHub.sol";
import { Policy, TargetKind, TargetState, InvalidPolicy } from "../src/LifelineTypes.sol";
import { IProxyLedger } from "../src/interfaces/IProxyLedger.sol";
import { ISystemContract, REACTIVE_IGNORE } from "../src/interfaces/ISystemContract.sol";
import { CanaryReactive } from "../src/fixtures/CanaryReactive.sol";
import { LoopingCanary } from "../src/fixtures/LoopingCanary.sol";

/// @notice Fork tests of the Lasna guardian. The hub, the canaries and the system contract are real;
///         only the node is emulated by the harness.
/// @dev This file is the regression set of the later hub units. No test asserts that a cron log or a
///      log of a registered destination proxy is skipped.
contract LifelineHubLasnaTest is LasnaForkHarness {
    uint256 internal constant HUB_VALUE = 5 ether;
    uint256 internal constant FEE_FLOAT = 0.5 ether;
    uint256 internal constant SELF_CARE_DAILY_CAP = 1 ether;

    uint256 internal constant HIGH = 0.02 ether;
    uint256 internal constant MAX_DEBT = 0.05 ether;
    uint256 internal constant DAILY_CAP = 1 ether;
    uint32 internal constant WINDOW = 1 hours;

    /// @dev Below the fee of one reaction (at least 0.01 lREACT), so each reaction after a rescue is a new debt.
    uint256 internal constant HIGH_BELOW_ONE_FEE = 0.001 ether;

    /// @dev A chain ID that has no vault.
    uint256 internal constant CHAIN_WITHOUT_VAULT = 1234;

    address internal owner;
    address internal stranger;
    LifelineHub internal hub;

    function setUp() public {
        _forkLasna();
        owner = _freshAccount("owner");
        stranger = _freshAccount("stranger");
        vm.deal(owner, 100 ether);
        vm.deal(stranger, 10 ether);
        hub = _deployHub(HUB_VALUE, FEE_FLOAT, FEE_FLOAT, SELF_CARE_DAILY_CAP, 0);
    }

    // ------------------------------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------------------------------

    function _deployHub(uint256 value, uint256 feeFloat, uint256 reserveTarget, uint256 selfCareCap, uint256 floor)
        internal
        returns (LifelineHub deployed)
    {
        _nextFreshContract(owner);
        vm.prank(owner);
        deployed = new LifelineHub{ value: value }(SYSTEM_ADDRESS, feeFloat, reserveTarget, selfCareCap, floor);
    }

    /// @dev An unfunded canary: no balance, no reserves, no debt.
    function _deployCanary() internal returns (CanaryReactive canary) {
        _nextFreshContract(owner);
        vm.prank(owner);
        canary = new CanaryReactive(SYSTEM_ADDRESS, 0, address(0), 0);
    }

    function _deployLoopingCanary() internal returns (LoopingCanary canary) {
        _nextFreshContract(owner);
        vm.prank(owner);
        canary = new LoopingCanary(SYSTEM_ADDRESS);
    }

    /// @dev `low` is 0, so `enroll` does not fund a target that has no debt.
    function _policy() internal pure returns (Policy memory) {
        return Policy({ low: 0, high: HIGH, maxDebt: MAX_DEBT, dailyCap: DAILY_CAP, maxRescues: 3, window: WINDOW });
    }

    function _enroll(address target, Policy memory policy) internal {
        vm.prank(owner);
        hub.enroll(LASNA_CHAIN_ID, target, policy);
    }

    function _poke(address canary) internal {
        vm.prank(owner);
        CanaryReactive(canary).poke();
    }

    /// @dev Pokes an unfunded canary that the hub does not listen to. The canary's reaction creates a debt.
    function _pokeIntoDebt(address canary) internal returns (uint256 debt) {
        _poke(canary);
        assertEq(_relay(), 1, "only the canary reacts");
        debt = system.debt(canary);
        assertGt(debt, 0, "the canary is in debt");
    }

    /// @dev An enrolled canary that is in debt, and the record of its debt log. The hub was paused for
    ///      the debt log, so it did not pay. The fee of that reaction is put back into the float, so the
    ///      next reaction has no self-care and the hub balance changes only by a payment to a target.
    function _enrolledCanaryInDebt() internal returns (address canary, IReactive.LogRecord memory debtLog) {
        canary = address(_deployCanary());
        _enroll(canary, _policy());

        vm.prank(owner);
        hub.pause();
        uint256 mark = _logMark();
        _poke(canary);
        assertEq(_relay(), 2, "the canary and the paused hub react");
        vm.prank(owner);
        hub.unpause();

        debtLog = _logRecord(_findLogIndex(mark, SYSTEM_ADDRESS, IProxyLedger.BlacklistContract.selector, 0));
        assertEq(debtLog.topic1, uint256(uint160(canary)));
        assertGt(system.debt(canary), 0, "the canary is in debt");
        _refillFloat();
    }

    /// @dev Puts the hub's reserves back to `selfReserveTarget` from outside: the manual `depositTo(hub)`.
    function _refillFloat() internal {
        uint256 reserves = system.reserves(address(hub));
        uint256 target = hub.selfReserveTarget();
        if (reserves < target) {
            vm.prank(stranger);
            system.depositTo{ value: target - reserves }(address(hub));
        }
    }

    function _state(address target) internal view returns (TargetState state) {
        (, state,) = hub.targetInfo(LASNA_CHAIN_ID, target);
    }

    function _hubLogs(uint256 mark, bytes32 topic0) internal returns (uint256) {
        return _countLogs(mark, address(hub), topic0);
    }

    /// @dev The sum of `SelfCare` of the hub after a mark.
    function _selfCareSum(uint256 mark) internal returns (uint256 sum) {
        uint256 count = _hubLogs(mark, LifelineHub.SelfCare.selector);
        for (uint256 i = 0; i < count; ++i) {
            sum += abi.decode(_findLog(mark, address(hub), LifelineHub.SelfCare.selector, i).data, (uint256));
        }
    }

    /// @dev Asserts one `Rescued` and one `Gap` of the hub after a mark, and returns their values.
    function _assertRescued(uint256 mark, address target)
        internal
        returns (uint256 debt, uint256 paid, uint256 deadFromBlock, uint256 aliveAtBlock)
    {
        assertEq(_hubLogs(mark, LifelineHub.Rescued.selector), 1, "one Rescued");
        assertEq(_hubLogs(mark, LifelineHub.Gap.selector), 1, "one Gap");
        ChainLog memory rescued = _findLog(mark, address(hub), LifelineHub.Rescued.selector, 0);
        ChainLog memory gap = _findLog(mark, address(hub), LifelineHub.Gap.selector, 0);
        assertEq(rescued.topics[1], uint256(uint160(target)), "Rescued target");
        assertEq(gap.topics[1], uint256(uint160(target)), "Gap target");
        (debt, paid) = abi.decode(rescued.data, (uint256, uint256));
        (deadFromBlock, aliveAtBlock) = abi.decode(gap.data, (uint256, uint256));
    }

    /// @dev Asserts that the hub paid nothing to a target after a mark.
    function _assertNoPayment(uint256 mark) internal {
        assertEq(_hubLogs(mark, LifelineHub.Rescued.selector), 0, "no Rescued");
        assertEq(_hubLogs(mark, LifelineHub.Gap.selector), 0, "no Gap");
        assertEq(_hubLogs(mark, LifelineHub.ToppedUp.selector), 0, "no ToppedUp");
        assertEq(_countLogs(mark, SYSTEM_ADDRESS, IProxyLedger.WhitelistContract.selector), 0, "no debt cleared");
    }

    /// @dev Asserts one `Skipped` of the hub after a mark, with these values.
    function _assertSkipped(uint256 mark, uint256 chainId, address target, LifelineHub.SkipReason reason) internal {
        assertEq(_hubLogs(mark, LifelineHub.Skipped.selector), 1, "one Skipped");
        ChainLog memory skipped = _findLog(mark, address(hub), LifelineHub.Skipped.selector, 0);
        assertEq(skipped.topics[1], chainId, "Skipped chain");
        assertEq(skipped.topics[2], uint256(uint160(target)), "Skipped target");
        assertEq(abi.decode(skipped.data, (uint256)), uint256(reason), "Skipped reason");
    }

    /// @dev The assertions of a guard case: the trigger of the hub succeeded, the system contract reported no
    ///      reverted reaction, the hub paid nothing, and the hub balance did not change.
    function _assertGuardHeld(TriggerResult memory result, uint256 mark, uint256 balanceBefore) internal {
        assertEq(result.reactive, address(hub), "the hub was triggered");
        assertTrue(result.success, "the trigger succeeded");
        assertFalse(result.reactReverted, "no reverted reaction");
        assertGt(result.fee, 0, "the reaction was charged");
        _assertNoPayment(mark);
        assertEq(address(hub).balance, balanceBefore, "the hub balance did not change");
        assertEq(system.debt(address(hub)), 0, "the hub has no debt");
    }

    /// @dev The six fields of a `Subscribe` or `Unsubscribe` log of the system contract.
    function _tupleOf(ChainLog memory log) internal pure returns (bytes memory) {
        return abi.encode(log.topics[1], log.topics[2], log.topics[3], log.data);
    }

    /// @dev The seconds from now to the start of the next calendar day (UTC).
    function _secondsToNextDay() internal view returns (uint256) {
        uint256 nowTime = vm.getBlockTimestamp();
        return (nowTime / 1 days + 1) * 1 days - nowTime;
    }

    // ------------------------------------------------------------------------------------------
    // Construction and funds
    // ------------------------------------------------------------------------------------------

    function test_constructor_depositsTheFloatAndKeepsThePrincipal() public {
        uint256 mark = _logMark();
        LifelineHub fresh = _deployHub(3 ether, 0.25 ether, 0.3 ether, 0.7 ether, 0.1 ether);

        assertEq(fresh.owner(), owner);
        assertEq(address(fresh).balance, 2.75 ether, "principal");
        assertEq(system.reserves(address(fresh)), 0.25 ether, "fee float");
        assertEq(fresh.selfReserveTarget(), 0.3 ether);
        assertEq(fresh.selfCareDailyCap(), 0.7 ether);
        assertEq(fresh.principalFloor(), 0.1 ether);
        assertFalse(fresh.paused());
        assertEq(fresh.targetCount(LASNA_CHAIN_ID), 0);

        assertEq(_countLogs(mark, address(fresh), LifelineHub.SelfCare.selector), 1);
        ChainLog memory selfCare = _findLog(mark, address(fresh), LifelineHub.SelfCare.selector, 0);
        assertEq(abi.decode(selfCare.data, (uint256)), 0.25 ether);
    }

    function test_constructor_revertsIfTheValueIsBelowTheFloat() public {
        vm.expectRevert(abi.encodeWithSelector(LifelineHub.InsufficientValue.selector, 0.1 ether, 0.2 ether));
        vm.prank(owner);
        new LifelineHub{ value: 0.1 ether }(SYSTEM_ADDRESS, 0.2 ether, 0, 0, 0);
    }

    function test_receive_acceptsPrincipal_withdraw_sendsItToTheOwner() public {
        vm.prank(stranger);
        (bool ok,) = address(hub).call{ value: 1 ether }("");
        assertTrue(ok);
        assertEq(address(hub).balance, HUB_VALUE - FEE_FLOAT + 1 ether);

        uint256 ownerBefore = owner.balance;
        vm.prank(owner);
        hub.withdraw(2 ether);
        assertEq(owner.balance, ownerBefore + 2 ether);
        assertEq(address(hub).balance, HUB_VALUE - FEE_FLOAT - 1 ether);
        assertEq(system.reserves(address(hub)), FEE_FLOAT, "a withdrawal does not touch the float");
    }

    function test_ownerFunctions_rejectAnotherCaller() public {
        address canary = address(_deployCanary());
        bytes memory notOwner = abi.encodeWithSelector(LifelineHub.NotOwner.selector);

        vm.startPrank(stranger);
        vm.expectRevert(notOwner);
        hub.enroll(LASNA_CHAIN_ID, canary, _policy());
        vm.expectRevert(notOwner);
        hub.disarm(LASNA_CHAIN_ID, canary);
        vm.expectRevert(notOwner);
        hub.release(canary);
        vm.expectRevert(notOwner);
        hub.pause();
        vm.expectRevert(notOwner);
        hub.unpause();
        vm.expectRevert(notOwner);
        hub.unsubscribeAll();
        vm.expectRevert(notOwner);
        hub.withdraw(1);
        vm.expectRevert(notOwner);
        hub.setSelfCare(1, 1);
        vm.expectRevert(notOwner);
        hub.setPrincipalFloor(1);
        vm.stopPrank();
    }

    function test_ownerSetters_storeTheValues() public {
        vm.startPrank(owner);
        hub.setSelfCare(0.11 ether, 0.22 ether);
        hub.setPrincipalFloor(0.33 ether);
        hub.pause();
        vm.stopPrank();
        assertEq(hub.selfReserveTarget(), 0.11 ether);
        assertEq(hub.selfCareDailyCap(), 0.22 ether);
        assertEq(hub.principalFloor(), 0.33 ether);
        assertTrue(hub.paused());

        vm.prank(owner);
        hub.unpause();
        assertFalse(hub.paused());
    }

    // ------------------------------------------------------------------------------------------
    // Interface
    // ------------------------------------------------------------------------------------------

    function test_react_hasTheSelectorThatTheSystemContractCalls() public pure {
        assertEq(LifelineHub.react.selector, IReactive.react.selector);
    }

    /// @dev The ABI has each function of the fixed interface, no `pay` and no fallback.
    function test_abi_hasNoPay() public view {
        string memory json = vm.readFile("out/LifelineHub.sol/LifelineHub.json");

        string[18] memory expected = [
            "enroll(uint256,address,(uint256,uint256,uint256,uint256,uint32,uint32))",
            "disarm(uint256,address)",
            "release(address)",
            "pause()",
            "unpause()",
            "unsubscribeAll()",
            "withdraw(uint256)",
            "setSelfCare(uint256,uint256)",
            "setPrincipalFloor(uint256)",
            "react((uint256,address,uint256,uint256,uint256,uint256,bytes,uint256,uint256,uint256,uint256,uint256))",
            "owner()",
            "paused()",
            "selfReserveTarget()",
            "selfCareDailyCap()",
            "principalFloor()",
            "targetCount(uint256)",
            "targetAt(uint256,uint256)",
            "targetInfo(uint256,address)"
        ];
        for (uint256 i = 0; i < expected.length; ++i) {
            assertTrue(
                vm.keyExistsJson(json, string.concat(".methodIdentifiers['", expected[i], "']")),
                string.concat("missing in the ABI: ", expected[i])
            );
        }

        uint256 entries;
        while (vm.keyExistsJson(json, string.concat(".abi[", vm.toString(entries), "]"))) {
            string memory entry = string.concat(".abi[", vm.toString(entries), "]");
            string memory kind = vm.parseJsonString(json, string.concat(entry, ".type"));
            assertNotEq(kind, "fallback", "the ABI has a fallback");
            if (vm.keyExistsJson(json, string.concat(entry, ".name"))) {
                assertNotEq(vm.parseJsonString(json, string.concat(entry, ".name")), "pay", "the ABI has pay");
            }
            ++entries;
        }
        assertGt(entries, expected.length, "the ABI was read");
    }

    /// @dev The call that the ledger makes when reserves do not cover a fee. The hub does not answer it.
    function test_pay_isNotCallable() public {
        uint256 balanceBefore = address(hub).balance;

        vm.prank(SYSTEM_ADDRESS);
        (bool ok,) = address(hub).call{ gas: 50_000 }(abi.encodeWithSignature("pay(uint256)", 1 ether));
        assertFalse(ok, "pay(uint256) reverts");

        vm.deal(SYSTEM_ADDRESS, SYSTEM_ADDRESS.balance + 1 ether);
        vm.prank(SYSTEM_ADDRESS);
        (ok,) = address(hub).call{ value: 1 }(abi.encodeWithSignature("pay(uint256)", 1 ether));
        assertFalse(ok, "there is no payable fallback");

        assertEq(address(hub).balance, balanceBefore);
    }

    // ------------------------------------------------------------------------------------------
    // Authentication
    // ------------------------------------------------------------------------------------------

    function test_react_rejectsACallerOtherThanTheSystemContract() public {
        (address canary, IReactive.LogRecord memory debtLog) = _enrolledCanaryInDebt();
        uint256 debt = system.debt(canary);
        uint256 balanceBefore = address(hub).balance;

        vm.expectRevert(abi.encodeWithSelector(LifelineHub.NotSystem.selector, owner));
        vm.prank(owner);
        hub.react(debtLog);

        vm.expectRevert(abi.encodeWithSelector(LifelineHub.NotSystem.selector, injected));
        vm.prank(injected);
        hub.react(debtLog);

        vm.expectRevert(abi.encodeWithSelector(LifelineHub.NotSystem.selector, stranger));
        vm.prank(stranger);
        hub.react(debtLog);

        assertEq(system.debt(canary), debt, "nothing was paid");
        assertEq(address(hub).balance, balanceBefore);
    }

    /// @dev The harness drives the real `trigger`, which accepts only the injected address.
    function test_trigger_acceptsOnlyTheInjectedAddress() public {
        (, IReactive.LogRecord memory debtLog) = _enrolledCanaryInDebt();

        vm.expectRevert(ISystemContractNode.OnlyInjected.selector);
        vm.prank(owner);
        ISystemContractNode(SYSTEM_ADDRESS).trigger(IReactive(payable(address(hub))), debtLog);
    }

    // ------------------------------------------------------------------------------------------
    // Enrollment
    // ------------------------------------------------------------------------------------------

    function test_enroll_storesTheTargetAndSubscribesTheExactTuple() public {
        address canary = address(_deployCanary());
        Policy memory policy = _policy();
        policy.low = 0;
        uint256 mark = _logMark();
        _enroll(canary, policy);

        assertEq(_hubLogs(mark, LifelineHub.Enrolled.selector), 1);
        ChainLog memory enrolled = _findLog(mark, address(hub), LifelineHub.Enrolled.selector, 0);
        assertEq(enrolled.topics[1], LASNA_CHAIN_ID);
        assertEq(enrolled.topics[2], uint256(uint160(canary)));

        assertEq(hub.targetCount(LASNA_CHAIN_ID), 1);
        assertEq(hub.targetAt(LASNA_CHAIN_ID, 0), canary);
        (TargetKind kind, TargetState state, Policy memory stored) = hub.targetInfo(LASNA_CHAIN_ID, canary);
        assertEq(uint256(kind), uint256(TargetKind.Lasna));
        assertEq(uint256(state), uint256(TargetState.Active));
        assertEq(abi.encode(stored), abi.encode(policy));

        // One subscription: the system contract on Lasna, the constant topic0, topic1 = the target.
        assertEq(_countLogs(mark, SYSTEM_ADDRESS, ISystemContract.Subscribe.selector), 1);
        ChainLog memory subscribe = _findLog(mark, SYSTEM_ADDRESS, ISystemContract.Subscribe.selector, 0);
        assertEq(subscribe.topics[1], uint256(uint160(address(hub))), "subscriber");
        assertEq(subscribe.topics[2], LASNA_CHAIN_ID, "chain");
        assertEq(subscribe.topics[3], uint256(uint160(SYSTEM_ADDRESS)), "emitter");
        assertEq(
            subscribe.data,
            abi.encode(
                uint256(IProxyLedger.BlacklistContract.selector),
                uint256(uint160(canary)),
                REACTIVE_IGNORE,
                REACTIVE_IGNORE
            ),
            "topics"
        );
        assertEq(_activeSubscriptions(address(hub)), 1);

        // The healthy target with `low` 0 gets no payment.
        _assertNoPayment(mark);
        assertEq(system.reserves(canary), 0);
    }

    function test_enroll_rejectsInvalidInput() public {
        address canary = address(_deployCanary());
        Policy memory policy = _policy();

        Policy memory invalid = _policy();
        invalid.high = 0;
        vm.expectRevert(InvalidPolicy.selector);
        vm.prank(owner);
        hub.enroll(LASNA_CHAIN_ID, canary, invalid);

        vm.expectRevert(abi.encodeWithSelector(LifelineHub.InvalidTarget.selector, address(hub)));
        vm.prank(owner);
        hub.enroll(LASNA_CHAIN_ID, address(hub), policy);

        _enroll(canary, policy);
        vm.expectRevert(abi.encodeWithSelector(LifelineHub.AlreadyEnrolled.selector, LASNA_CHAIN_ID, canary));
        vm.prank(owner);
        hub.enroll(LASNA_CHAIN_ID, canary, policy);

        assertEq(hub.targetCount(LASNA_CHAIN_ID), 1);
    }

    function test_enroll_otherChainRevertsUnknownChain() public {
        address canary = address(_deployCanary());
        uint256[4] memory chains = [uint256(0), 1, 84532, CHAIN_WITHOUT_VAULT];
        for (uint256 i = 0; i < chains.length; ++i) {
            vm.expectRevert(abi.encodeWithSelector(LifelineHub.UnknownChain.selector, chains[i]));
            vm.prank(owner);
            hub.enroll(chains[i], canary, _policy());

            vm.expectRevert(abi.encodeWithSelector(LifelineHub.UnknownChain.selector, chains[i]));
            vm.prank(owner);
            hub.disarm(chains[i], canary);
        }
        assertEq(_activeSubscriptions(address(hub)), 0);
    }

    function test_enroll_rescuesATargetThatIsInDebt() public {
        address canary = address(_deployCanary());
        uint256 debtBefore = _pokeIntoDebt(canary);
        uint256 reservesBefore = system.reserves(address(hub));
        uint256 balanceBefore = address(hub).balance;

        uint256 mark = _logMark();
        _enroll(canary, _policy());

        (uint256 debt, uint256 paid, uint256 deadFromBlock, uint256 aliveAtBlock) = _assertRescued(mark, canary);
        assertEq(debt, debtBefore);
        assertEq(paid, debtBefore + HIGH);
        assertEq(deadFromBlock, 0, "no debt log started this rescue");
        assertEq(aliveAtBlock, vm.getBlockNumber());
        assertEq(system.debt(canary), 0);
        assertEq(system.reserves(canary), HIGH);
        assertEq(address(hub).balance, balanceBefore - paid);
        assertEq(system.reserves(address(hub)), reservesBefore, "an owner transaction costs no reaction fee");

        // The canary works again.
        mark = _logMark();
        _poke(canary);
        assertEq(_relay(), 1);
        assertEq(_countLogs(mark, canary, CanaryReactive.Tick.selector), 1);
        assertEq(system.debt(canary), 0);
    }

    function test_enroll_topsUpATargetBelowLow() public {
        address canary = address(_deployCanary());
        Policy memory policy = _policy();
        policy.low = 0.01 ether;
        uint256 balanceBefore = address(hub).balance;

        uint256 mark = _logMark();
        _enroll(canary, policy);

        assertEq(_hubLogs(mark, LifelineHub.ToppedUp.selector), 1);
        ChainLog memory toppedUp = _findLog(mark, address(hub), LifelineHub.ToppedUp.selector, 0);
        assertEq(toppedUp.topics[1], uint256(uint160(canary)));
        assertEq(abi.decode(toppedUp.data, (uint256)), HIGH);
        assertEq(_hubLogs(mark, LifelineHub.Rescued.selector), 0);
        assertEq(_hubLogs(mark, LifelineHub.Gap.selector), 0);
        assertEq(system.reserves(canary), HIGH);
        assertEq(address(hub).balance, balanceBefore - HIGH);
    }

    function test_enroll_healthyTargetGetsNoPayment() public {
        address canary = address(_deployCanary());
        Policy memory policy = _policy();
        policy.low = 0.01 ether;
        vm.prank(stranger);
        system.depositTo{ value: 0.01 ether }(canary);
        uint256 balanceBefore = address(hub).balance;

        uint256 mark = _logMark();
        _enroll(canary, policy);

        _assertNoPayment(mark);
        assertEq(_countLogsOf(mark, address(hub)), 1, "only Enrolled");
        assertEq(system.reserves(canary), 0.01 ether);
        assertEq(address(hub).balance, balanceBefore);
    }

    /// @dev A top-up counts toward `dailyCap` only: with `maxRescues` 1, the debt after it is rescued.
    function test_topUp_doesNotCountAsARescue() public {
        address canary = address(_deployCanary());
        Policy memory policy = _policy();
        policy.low = HIGH_BELOW_ONE_FEE;
        policy.high = HIGH_BELOW_ONE_FEE;
        policy.maxRescues = 1;
        _enroll(canary, policy);
        assertEq(system.reserves(canary), HIGH_BELOW_ONE_FEE, "topped up in enroll");

        uint256 mark = _logMark();
        _poke(canary);
        assertEq(_relay(), 2);
        _assertRescued(mark, canary);
        assertEq(uint256(_state(canary)), uint256(TargetState.Active));
    }

    // ------------------------------------------------------------------------------------------
    // Airbag
    // ------------------------------------------------------------------------------------------

    function test_airbag_clearsTheDebtInTheNextTrigger() public {
        address canary = address(_deployCanary());
        _enroll(canary, _policy());
        assertEq(system.reserves(canary), 0, "enroll did not fund the canary");

        uint256 mark = _logMark();
        uint256 firstTrigger = _triggerCount();
        _poke(canary);
        assertEq(_relay(), 2, "exactly two triggers ran");

        // Trigger 1: the canary reacts and cannot pay. Its fee becomes a debt.
        TriggerResult memory canaryTrigger = _triggerAt(firstTrigger);
        assertEq(canaryTrigger.reactive, canary);
        assertTrue(canaryTrigger.success);
        assertFalse(canaryTrigger.reactReverted);
        assertEq(_countLogs(mark, canary, CanaryReactive.Tick.selector), 1);
        assertEq(_countLogs(mark, SYSTEM_ADDRESS, IProxyLedger.BlacklistContract.selector), 1);
        ChainLog memory debtLog = _findLog(mark, SYSTEM_ADDRESS, IProxyLedger.BlacklistContract.selector, 0);
        assertEq(debtLog.topics[1], uint256(uint160(canary)));
        assertEq(debtLog.blockNumber, canaryTrigger.blockNumber);

        // Trigger 2: the hub clears the debt and refills the canary to `high`.
        TriggerResult memory hubTrigger = _triggerAt(firstTrigger + 1);
        assertEq(hubTrigger.reactive, address(hub));
        assertTrue(hubTrigger.success);
        assertFalse(hubTrigger.reactReverted);
        (uint256 debt, uint256 paid, uint256 deadFromBlock, uint256 aliveAtBlock) = _assertRescued(mark, canary);
        assertEq(debt, canaryTrigger.fee, "the debt is the fee of the canary's reaction");
        assertEq(paid, debt + HIGH);
        assertEq(deadFromBlock, debtLog.blockNumber, "dead from the block of the debt log");
        assertEq(aliveAtBlock, hubTrigger.blockNumber);
        assertEq(aliveAtBlock, deadFromBlock + 1, "alive in the next block");
        assertEq(_countLogs(mark, SYSTEM_ADDRESS, IProxyLedger.WhitelistContract.selector), 1);
        assertEq(system.debt(canary), 0);
        assertEq(system.reserves(canary), HIGH);
        assertEq(system.debt(address(hub)), 0);

        // The next reaction of the canary runs and is paid from its reserves.
        mark = _logMark();
        _poke(canary);
        assertEq(_relay(), 1);
        assertEq(_countLogs(mark, canary, CanaryReactive.Tick.selector), 1);
        assertEq(_findLog(mark, canary, CanaryReactive.Tick.selector, 0).topics[1], 2);
        assertEq(_countLogs(mark, SYSTEM_ADDRESS, IProxyLedger.BlacklistContract.selector), 0);
        assertEq(system.debt(canary), 0);
        assertEq(system.reserves(canary), HIGH - _lastTrigger(canary).fee);
    }

    /// @dev F4: the hub listens to enrolled targets only. Another contract's debt costs the hub nothing.
    function test_airbag_aStrangersDebtDoesNotWakeTheHub() public {
        _enroll(address(_deployCanary()), _policy());
        address other = address(_deployCanary());
        uint256 reservesBefore = system.reserves(address(hub));

        _pokeIntoDebt(other);

        assertEq(system.reserves(address(hub)), reservesBefore, "no reaction fee");
    }

    function test_fees_moveOnlyTheFloat() public {
        address canary = address(_deployCanary());
        _enroll(canary, _policy());
        // One reaction first, so that the rescue reaction has self-care to do.
        TriggerResult memory earlier = _skipReaction();

        uint256 balanceBefore = address(hub).balance;
        uint256 reservesBefore = system.reserves(address(hub));
        uint256 mark = _logMark();
        _poke(canary);
        assertEq(_relay(), 2);

        TriggerResult memory hubTrigger = _lastTrigger(address(hub));
        (, uint256 paid,,) = _assertRescued(mark, canary);
        uint256 selfCare = _selfCareSum(mark);
        assertEq(selfCare, earlier.fee, "self-care puts the fee of the earlier reaction back");

        assertEq(balanceBefore - address(hub).balance, paid + selfCare, "the balance paid the target and the float");
        assertEq(
            system.reserves(address(hub)), reservesBefore + selfCare - hubTrigger.fee, "the fee came from the reserves"
        );
        assertEq(system.debt(address(hub)), 0);
        assertEq(_countLogs(mark, SYSTEM_ADDRESS, IProxyLedger.PaymentFailure.selector), 1, "only the canary's");
    }

    /// @dev A reaction of the hub that is a skip: a log of the system contract with an unknown `topic0`.
    function _skipReaction() internal returns (TriggerResult memory result) {
        IReactive.LogRecord memory record;
        record.chainId = LASNA_CHAIN_ID;
        record.contractAddress = SYSTEM_ADDRESS;
        record.topic0 = uint256(keccak256("NotALifelineEvent(address)"));
        record.topic1 = uint256(uint160(stranger));
        record.blockNumber = vm.getBlockNumber();
        record.opCode = 2;
        result = _triggerForged(address(hub), record);
        assertTrue(result.success);
        assertFalse(result.reactReverted);
    }

    // ------------------------------------------------------------------------------------------
    // Guards. Each case: the trigger succeeds, no reverted reaction, the correct event, the hub
    // balance does not change.
    // ------------------------------------------------------------------------------------------

    /// @dev Sends a record to the hub through the real `trigger` and asserts the guard result.
    function _assertGuard(IReactive.LogRecord memory record) internal returns (uint256 mark) {
        mark = _logMark();
        uint256 balanceBefore = address(hub).balance;
        TriggerResult memory result = _triggerForged(address(hub), record);
        _assertGuardHeld(result, mark, balanceBefore);
    }

    /// @dev Sends the true debt log again and asserts that the hub now rescues the canary. It shows that
    ///      only the guard under test stopped the payment.
    function _assertTrueLogRescues(address canary, IReactive.LogRecord memory debtLog) internal {
        uint256 mark = _logMark();
        TriggerResult memory result = _triggerForged(address(hub), debtLog);
        assertTrue(result.success);
        assertFalse(result.reactReverted);
        (,, uint256 deadFromBlock,) = _assertRescued(mark, canary);
        assertEq(deadFromBlock, debtLog.blockNumber);
        assertEq(system.debt(canary), 0);
    }

    function test_guard_debtAboveMaxDebt() public {
        address canary = address(_deployCanary());
        Policy memory policy = _policy();
        policy.maxDebt = 0.001 ether;
        _enroll(canary, policy);

        uint256 mark = _logMark();
        uint256 balanceBefore = address(hub).balance;
        _poke(canary);
        assertEq(_relay(), 2);

        uint256 debt = system.debt(canary);
        assertGt(debt, policy.maxDebt);
        _assertGuardHeld(_lastTrigger(address(hub)), mark, balanceBefore);
        _assertSkipped(mark, LASNA_CHAIN_ID, canary, LifelineHub.SkipReason.DebtAboveMax);
        assertEq(system.reserves(canary), 0, "no partial payment");
    }

    function test_guard_dailyCapUsed() public {
        _keepClearOfMidnight();
        address canary = address(_deployCanary());
        uint256 firstDebt = _pokeIntoDebt(canary);

        // The rescue inside `enroll` uses the cap of the day. 0.001 lREACT is left.
        Policy memory policy = _policy();
        policy.high = HIGH_BELOW_ONE_FEE;
        policy.dailyCap = firstDebt + HIGH_BELOW_ONE_FEE + 0.001 ether;
        uint256 mark = _logMark();
        _enroll(canary, policy);
        (, uint256 paid,,) = _assertRescued(mark, canary);
        assertEq(paid, firstDebt + HIGH_BELOW_ONE_FEE);

        mark = _logMark();
        uint256 balanceBefore = address(hub).balance;
        _poke(canary);
        assertEq(_relay(), 2);

        assertGt(system.debt(canary), 0.001 ether, "the new debt is above what is left of the cap");
        _assertGuardHeld(_lastTrigger(address(hub)), mark, balanceBefore);
        _assertSkipped(mark, LASNA_CHAIN_ID, canary, LifelineHub.SkipReason.DailyCap);
    }

    /// @dev F8: `dailyCap` is a cap of the calendar day. The next day has a new cap.
    function test_dailyCap_isACapOfTheCalendarDay() public {
        // Ten minutes before the end of a day.
        _skipTime(_secondsToNextDay() + 1 days - 10 minutes);
        address canary = address(_deployCanary());
        uint256 firstDebt = _pokeIntoDebt(canary);

        Policy memory policy = _policy();
        policy.high = HIGH_BELOW_ONE_FEE;
        policy.dailyCap = firstDebt + HIGH_BELOW_ONE_FEE + 0.001 ether;
        _enroll(canary, policy);

        uint256 mark = _logMark();
        _poke(canary);
        assertEq(_relay(), 2);
        _assertSkipped(mark, LASNA_CHAIN_ID, canary, LifelineHub.SkipReason.DailyCap);
        IReactive.LogRecord memory debtLog =
            _logRecord(_findLogIndex(mark, SYSTEM_ADDRESS, IProxyLedger.BlacklistContract.selector, 0));

        // Still the same day: the same log is skipped again.
        _skipTime(5 minutes);
        mark = _assertGuardAfterRefill(debtLog);
        _assertSkipped(mark, LASNA_CHAIN_ID, canary, LifelineHub.SkipReason.DailyCap);

        // The next day: the cap is new.
        _skipTime(10 minutes);
        _assertTrueLogRescues(canary, debtLog);
    }

    function _assertGuardAfterRefill(IReactive.LogRecord memory record) internal returns (uint256 mark) {
        _refillFloat();
        return _assertGuard(record);
    }

    function test_guard_paymentBelowTheFloor() public {
        (address canary, IReactive.LogRecord memory debtLog) = _enrolledCanaryInDebt();
        uint256 balance = address(hub).balance;

        // The balance is at the floor or above it, but the payment takes it below.
        vm.prank(owner);
        hub.setPrincipalFloor(balance - 0.01 ether);
        uint256 mark = _assertGuard(debtLog);
        _assertSkipped(mark, LASNA_CHAIN_ID, canary, LifelineHub.SkipReason.Floor);
        assertEq(_hubLogs(mark, LifelineHub.HubLow.selector), 0);

        vm.prank(owner);
        hub.setPrincipalFloor(0);
        _assertTrueLogRescues(canary, debtLog);
    }

    function test_guard_hubLow() public {
        (address canary, IReactive.LogRecord memory debtLog) = _enrolledCanaryInDebt();
        uint256 balance = address(hub).balance;
        uint256 debt = system.debt(canary);

        vm.prank(owner);
        hub.setPrincipalFloor(balance + 1);
        uint256 mark = _assertGuard(debtLog);

        assertEq(_countLogsOf(mark, address(hub)), 1, "exactly one event");
        ChainLog memory hubLow = _findLog(mark, address(hub), LifelineHub.HubLow.selector, 0);
        (uint256 loggedBalance, uint256 loggedFloor) = abi.decode(hubLow.data, (uint256, uint256));
        assertEq(loggedBalance, balance);
        assertEq(loggedFloor, balance + 1);
        assertEq(system.debt(canary), debt);

        vm.prank(owner);
        hub.setPrincipalFloor(0);
        _assertTrueLogRescues(canary, debtLog);
    }

    function test_guard_forgedTopic1EqualToTheHub() public {
        IReactive.LogRecord memory record =
            _debtLogRecord(LASNA_CHAIN_ID, SYSTEM_ADDRESS, address(hub), vm.getBlockNumber());
        uint256 mark = _assertGuard(record);
        _assertSkipped(mark, LASNA_CHAIN_ID, address(hub), LifelineHub.SkipReason.SelfTarget);
    }

    function test_guard_forgedEmitter() public {
        (address canary, IReactive.LogRecord memory debtLog) = _enrolledCanaryInDebt();
        uint256 debt = system.debt(canary);

        IReactive.LogRecord memory forged = _copy(debtLog);
        forged.contractAddress = _freshAccount("emitter");
        uint256 mark = _assertGuard(forged);
        _assertSkipped(mark, LASNA_CHAIN_ID, canary, LifelineHub.SkipReason.WrongEmitter);
        assertEq(system.debt(canary), debt);

        _assertTrueLogRescues(canary, debtLog);
    }

    function test_guard_forgedChainId() public {
        (address canary, IReactive.LogRecord memory debtLog) = _enrolledCanaryInDebt();
        uint256 debt = system.debt(canary);

        IReactive.LogRecord memory forged = _copy(debtLog);
        forged.chainId = CHAIN_WITHOUT_VAULT;
        uint256 mark = _assertGuard(forged);
        _assertSkipped(mark, CHAIN_WITHOUT_VAULT, canary, LifelineHub.SkipReason.UnknownChain);
        assertEq(system.debt(canary), debt);

        _assertTrueLogRescues(canary, debtLog);
    }

    function test_guard_unknownTopic0() public {
        (address canary, IReactive.LogRecord memory debtLog) = _enrolledCanaryInDebt();
        uint256 debt = system.debt(canary);

        IReactive.LogRecord memory forged = _copy(debtLog);
        forged.topic0 = uint256(keccak256("NotALifelineEvent(address)"));
        uint256 mark = _assertGuard(forged);
        _assertSkipped(mark, LASNA_CHAIN_ID, canary, LifelineHub.SkipReason.UnknownTopic);
        assertEq(system.debt(canary), debt);

        _assertTrueLogRescues(canary, debtLog);
    }

    function test_guard_wrongTopicCount() public {
        (address canary, IReactive.LogRecord memory debtLog) = _enrolledCanaryInDebt();
        uint256 debt = system.debt(canary);

        IReactive.LogRecord memory forged = _copy(debtLog);
        forged.opCode = 1;
        uint256 mark = _assertGuard(forged);
        _assertSkipped(mark, LASNA_CHAIN_ID, canary, LifelineHub.SkipReason.BadTopicCount);

        forged.opCode = 3;
        mark = _assertGuardAfterRefill(forged);
        _assertSkipped(mark, LASNA_CHAIN_ID, canary, LifelineHub.SkipReason.BadTopicCount);
        assertEq(system.debt(canary), debt);

        _assertTrueLogRescues(canary, debtLog);
    }

    function test_guard_debtLogOfAContractThatIsNotEnrolled() public {
        address other = address(_deployCanary());
        uint256 mark = _logMark();
        uint256 debt = _pokeIntoDebt(other);
        IReactive.LogRecord memory debtLog =
            _logRecord(_findLogIndex(mark, SYSTEM_ADDRESS, IProxyLedger.BlacklistContract.selector, 0));

        mark = _assertGuard(debtLog);
        _assertSkipped(mark, LASNA_CHAIN_ID, other, LifelineHub.SkipReason.NotEnrolled);
        assertEq(system.debt(other), debt);
        assertEq(system.reserves(other), 0);
    }

    function test_guard_paused() public {
        address canary = address(_deployCanary());
        _enroll(canary, _policy());
        vm.prank(owner);
        hub.pause();

        uint256 mark = _logMark();
        uint256 balanceBefore = address(hub).balance;
        _poke(canary);
        assertEq(_relay(), 2);

        _assertGuardHeld(_lastTrigger(address(hub)), mark, balanceBefore);
        _assertSkipped(mark, LASNA_CHAIN_ID, canary, LifelineHub.SkipReason.Paused);
        assertGt(system.debt(canary), 0);
    }

    /// @dev Reserves cannot fund `depositTo`. A hub with reserves and no balance skips and does not revert.
    function test_guard_reservesOnlyFunding() public {
        hub = _deployHub(FEE_FLOAT, FEE_FLOAT, FEE_FLOAT, SELF_CARE_DAILY_CAP, 0);
        assertEq(address(hub).balance, 0);
        address canary = address(_deployCanary());
        _enroll(canary, _policy());

        uint256 mark = _logMark();
        _poke(canary);
        assertEq(_relay(), 2);

        _assertGuardHeld(_lastTrigger(address(hub)), mark, 0);
        _assertSkipped(mark, LASNA_CHAIN_ID, canary, LifelineHub.SkipReason.Floor);
        assertGt(system.debt(canary), 0);
    }

    struct Forgery {
        uint8 fields;
        uint256 chainId;
        address emitter;
        uint256 topic0;
        uint256 opCode;
        address target;
        uint256 topic2;
        uint256 topic3;
        uint256 blockNumber;
        bytes data;
    }

    /// @dev Fuzz: a debt log with one or more forged fields never reverts the reaction and never pays.
    ///      The enrolled canary is in debt, so the true log pays. The forged chain has no vault.
    function test_forgedDebtLog_neverRevertsAndNeverPays(Forgery memory f) public {
        (address canary, IReactive.LogRecord memory debtLog) = _enrolledCanaryInDebt();
        uint256 debt = system.debt(canary);

        f.fields = uint8(bound(f.fields, 1, 31));
        IReactive.LogRecord memory forged = _copy(debtLog);
        if (f.fields & 1 != 0) {
            vm.assume(f.chainId != LASNA_CHAIN_ID);
            forged.chainId = f.chainId;
        }
        if (f.fields & 2 != 0) {
            vm.assume(f.emitter != SYSTEM_ADDRESS);
            forged.contractAddress = f.emitter;
        }
        if (f.fields & 4 != 0) {
            vm.assume(f.topic0 != debtLog.topic0);
            forged.topic0 = f.topic0;
        }
        if (f.fields & 8 != 0) {
            vm.assume(f.opCode != 2);
            forged.opCode = f.opCode;
        }
        if (f.fields & 16 != 0) {
            vm.assume(f.target != canary);
            forged.topic1 = uint256(uint160(f.target));
        }
        forged.topic2 = f.topic2;
        forged.topic3 = f.topic3;
        forged.blockNumber = f.blockNumber;
        forged.data = f.data;

        uint256 mark = _assertGuard(forged);
        assertEq(_hubLogs(mark, LifelineHub.Skipped.selector), 1, "one Skipped");
        assertEq(_countLogsOf(mark, address(hub)), 1, "no other event");
        assertEq(system.debt(canary), debt);
    }

    /// @dev Fuzz: a payment is the whole of `debt + high`, or nothing.
    function test_rescue_isAllOrNothing(uint256 high, uint256 maxDebt, uint256 dailyCap, uint256 floorMargin) public {
        _keepClearOfMidnight();
        Policy memory policy = _policy();
        policy.high = bound(high, 1, 0.1 ether);
        policy.maxDebt = bound(maxDebt, 1, 0.03 ether);
        policy.dailyCap = bound(dailyCap, 1, 0.2 ether);
        floorMargin = bound(floorMargin, 0, 0.2 ether);

        address canary = address(_deployCanary());
        _enroll(canary, policy);
        uint256 balanceBefore = address(hub).balance;
        vm.prank(owner);
        hub.setPrincipalFloor(balanceBefore - floorMargin);

        uint256 mark = _logMark();
        _poke(canary);
        assertEq(_relay(), 2);
        TriggerResult memory hubTrigger = _lastTrigger(address(hub));
        assertTrue(hubTrigger.success);
        assertFalse(hubTrigger.reactReverted);

        uint256 debt = _lastTrigger(canary).fee;
        uint256 amount = debt + policy.high;
        if (debt > policy.maxDebt) {
            _assertNothingPaid(mark, canary, debt, balanceBefore, LifelineHub.SkipReason.DebtAboveMax);
        } else if (amount > policy.dailyCap) {
            _assertNothingPaid(mark, canary, debt, balanceBefore, LifelineHub.SkipReason.DailyCap);
        } else if (amount > floorMargin) {
            _assertNothingPaid(mark, canary, debt, balanceBefore, LifelineHub.SkipReason.Floor);
        } else {
            (uint256 rescuedDebt, uint256 paid,,) = _assertRescued(mark, canary);
            assertEq(rescuedDebt, debt);
            assertEq(paid, amount);
            assertEq(system.debt(canary), 0);
            assertEq(system.reserves(canary), policy.high);
            assertEq(address(hub).balance, balanceBefore - amount);
            assertGe(address(hub).balance, hub.principalFloor());
        }
    }

    function _assertNothingPaid(
        uint256 mark,
        address canary,
        uint256 debt,
        uint256 balanceBefore,
        LifelineHub.SkipReason reason
    ) internal {
        _assertNoPayment(mark);
        _assertSkipped(mark, LASNA_CHAIN_ID, canary, reason);
        assertEq(system.debt(canary), debt);
        assertEq(system.reserves(canary), 0);
        assertEq(address(hub).balance, balanceBefore);
    }

    function _copy(IReactive.LogRecord memory record) internal pure returns (IReactive.LogRecord memory) {
        return abi.decode(abi.encode(record), (IReactive.LogRecord));
    }

    /// @dev Moves to the start of the next day if the day ends in less than one hour.
    function _keepClearOfMidnight() internal {
        uint256 left = _secondsToNextDay();
        if (left < 1 hours) _skipTime(left);
    }

    // ------------------------------------------------------------------------------------------
    // Quarantine
    // ------------------------------------------------------------------------------------------

    /// @dev A looping canary with `maxRescues` 3 and `high` below one reaction fee: each `poke()` after a
    ///      rescue gives a new debt.
    function _enrollLoopingCanary() internal returns (address canary) {
        canary = address(_deployLoopingCanary());
        Policy memory policy = _policy();
        policy.high = HIGH_BELOW_ONE_FEE;
        _enroll(canary, policy);
    }

    /// @dev One debt of an enrolled canary and the reaction of the hub to it.
    function _debtRound(address canary) internal returns (uint256 mark) {
        mark = _logMark();
        _poke(canary);
        assertEq(_relay(), 2, "the canary and the hub react");
        assertEq(_countLogs(mark, SYSTEM_ADDRESS, IProxyLedger.BlacklistContract.selector), 1, "a new debt");
    }

    function _rescueRounds(address canary, uint256 rounds) internal {
        for (uint256 i = 0; i < rounds; ++i) {
            uint256 mark = _debtRound(canary);
            _assertRescued(mark, canary);
            assertEq(system.debt(canary), 0);
            assertEq(system.reserves(canary), HIGH_BELOW_ONE_FEE);
        }
    }

    /// @dev The `n`-th `Subscribe` or `Unsubscribe` log of the hub after a mark.
    function _hubTupleLog(uint256 mark, bytes32 topic0, uint256 n) internal returns (ChainLog memory log) {
        uint256 count = _countLogs(mark, SYSTEM_ADDRESS, topic0);
        for (uint256 i = 0; i < count; ++i) {
            log = _findLog(mark, SYSTEM_ADDRESS, topic0, i);
            if (log.topics[1] == uint256(uint160(address(hub)))) {
                if (n == 0) return log;
                --n;
            }
        }
        fail("the hub has no such subscription log");
    }

    /// @dev Three rescues inside the window, then the fourth debt: quarantine.
    function _quarantine(address canary) internal returns (uint256 mark) {
        _rescueRounds(canary, 3);
        mark = _debtRound(canary);
        assertEq(_hubLogs(mark, LifelineHub.Quarantined.selector), 1, "Quarantined");
        assertEq(uint256(_state(canary)), uint256(TargetState.Quarantined));
    }

    function test_quarantine_theFourthDebtInsideTheWindow() public {
        uint256 enrollMark = _logMark();
        address canary = _enrollLoopingCanary();
        ChainLog memory subscribe = _hubTupleLog(enrollMark, ISystemContract.Subscribe.selector, 0);

        // Three debts inside the window give three rescues. The canary's reaction reverts each time.
        _rescueRounds(canary, 3);
        assertTrue(_lastTrigger(canary).reactReverted, "the looping canary reverts");

        // The fourth debt: no payment, Quarantined, and the tuple is unsubscribed.
        uint256 balanceBefore = address(hub).balance;
        uint256 mark = _debtRound(canary);
        TriggerResult memory hubTrigger = _lastTrigger(address(hub));
        assertTrue(hubTrigger.success);
        assertFalse(hubTrigger.reactReverted);

        assertEq(_hubLogs(mark, LifelineHub.Quarantined.selector), 1);
        assertEq(_findLog(mark, address(hub), LifelineHub.Quarantined.selector, 0).topics[1], uint256(uint160(canary)));
        _assertNoPayment(mark);
        assertEq(_hubLogs(mark, LifelineHub.Skipped.selector), 0);
        assertEq(balanceBefore - address(hub).balance, _selfCareSum(mark), "only self-care left the balance");
        uint256 debt = system.debt(canary);
        assertGt(debt, 0, "the debt stays");
        assertEq(uint256(_state(canary)), uint256(TargetState.Quarantined));
        assertEq(hub.targetAt(LASNA_CHAIN_ID, 0), canary, "a quarantined target stays enrolled");

        // The Unsubscribe has the same six fields as the Subscribe.
        assertEq(_countLogs(mark, SYSTEM_ADDRESS, ISystemContract.Unsubscribe.selector), 1);
        ChainLog memory unsubscribe = _hubTupleLog(mark, ISystemContract.Unsubscribe.selector, 0);
        assertEq(_tupleOf(unsubscribe), _tupleOf(subscribe), "the same six fields");
        assertEq(_activeSubscriptions(address(hub)), 0);

        // A later debt log wakes nothing.
        vm.prank(stranger);
        system.depositTo{ value: debt }(canary);
        assertEq(system.debt(canary), 0);
        uint256 hubReserves = system.reserves(address(hub));
        mark = _logMark();
        _poke(canary);
        assertEq(_relay(), 1, "only the canary reacts");
        assertEq(_countLogs(mark, SYSTEM_ADDRESS, IProxyLedger.BlacklistContract.selector), 1, "a new debt log");
        assertEq(_countLogsOf(mark, address(hub)), 0);
        assertEq(system.reserves(address(hub)), hubReserves, "no reaction fee");
        assertGt(system.debt(canary), 0);
    }

    /// @dev A forged debt log for a quarantined target is skipped and not paid.
    function test_quarantine_aDebtLogForAQuarantinedTargetIsSkipped() public {
        address canary = _enrollLoopingCanary();
        uint256 mark = _quarantine(canary);
        IReactive.LogRecord memory debtLog =
            _logRecord(_findLogIndex(mark, SYSTEM_ADDRESS, IProxyLedger.BlacklistContract.selector, 0));

        mark = _assertGuardAfterRefill(debtLog);
        _assertSkipped(mark, LASNA_CHAIN_ID, canary, LifelineHub.SkipReason.NotActive);
    }

    function test_quarantine_afterTheWindowARescueIsPermitted() public {
        address canary = _enrollLoopingCanary();
        _rescueRounds(canary, 3);

        _skipTime(WINDOW + 1);

        uint256 mark = _debtRound(canary);
        _assertRescued(mark, canary);
        assertEq(_hubLogs(mark, LifelineHub.Quarantined.selector), 0);
        assertEq(uint256(_state(canary)), uint256(TargetState.Active));
    }

    /// @dev The window slides: a rescue leaves the count when it is `window` seconds old.
    function test_quarantine_theWindowSlides() public {
        address canary = _enrollLoopingCanary();

        _rescueRounds(canary, 1); // at 0 minutes
        _skipTime(20 minutes);
        _rescueRounds(canary, 1); // at 20 minutes
        _skipTime(20 minutes);
        _rescueRounds(canary, 1); // at 40 minutes
        _skipTime(21 minutes);
        // At 61 minutes the first rescue is out of the window: two are inside.
        _rescueRounds(canary, 1);
        _skipTime(1 minutes);
        // At 62 minutes the rescues of 20, 40 and 61 minutes are inside.
        uint256 mark = _debtRound(canary);
        assertEq(_hubLogs(mark, LifelineHub.Quarantined.selector), 1);
        _assertNoPayment(mark);
    }

    function test_release_subscribesAgainAndReconciles() public {
        uint256 enrollMark = _logMark();
        address canary = _enrollLoopingCanary();
        ChainLog memory subscribe = _hubTupleLog(enrollMark, ISystemContract.Subscribe.selector, 0);
        _quarantine(canary);
        uint256 debt = system.debt(canary);
        assertGt(debt, 0);

        vm.expectRevert(abi.encodeWithSelector(LifelineHub.NotOwner.selector));
        vm.prank(stranger);
        hub.release(canary);

        uint256 mark = _logMark();
        vm.prank(owner);
        hub.release(canary);

        assertEq(_hubLogs(mark, LifelineHub.Released.selector), 1);
        assertEq(_findLog(mark, address(hub), LifelineHub.Released.selector, 0).topics[1], uint256(uint160(canary)));
        assertEq(uint256(_state(canary)), uint256(TargetState.Active));

        // Subscribed again with the same six fields.
        ChainLog memory again = _hubTupleLog(mark, ISystemContract.Subscribe.selector, 0);
        assertEq(_tupleOf(again), _tupleOf(subscribe));
        assertEq(_activeSubscriptions(address(hub)), 1);

        // Reconciled at once: the open debt is cleared, with no debt log.
        (uint256 rescuedDebt,, uint256 deadFromBlock,) = _assertRescued(mark, canary);
        assertEq(rescuedDebt, debt);
        assertEq(deadFromBlock, 0);
        assertEq(system.debt(canary), 0);

        // The rescue history is clear: the rescue inside `release` is the first of three.
        _rescueRounds(canary, 2);
        mark = _debtRound(canary);
        assertEq(_hubLogs(mark, LifelineHub.Quarantined.selector), 1);
    }

    function test_release_revertsForATargetThatIsNotQuarantined() public {
        address canary = address(_deployCanary());
        vm.expectRevert(abi.encodeWithSelector(LifelineHub.NotQuarantined.selector, canary));
        vm.prank(owner);
        hub.release(canary);

        _enroll(canary, _policy());
        vm.expectRevert(abi.encodeWithSelector(LifelineHub.NotQuarantined.selector, canary));
        vm.prank(owner);
        hub.release(canary);
    }

    // ------------------------------------------------------------------------------------------
    // Self-care
    // ------------------------------------------------------------------------------------------

    function test_selfCare_returnsTheReservesToTheTarget() public {
        // The reserves are at the target before the first reaction: nothing to add.
        uint256 mark = _logMark();
        TriggerResult memory first = _skipReaction();
        assertEq(_hubLogs(mark, LifelineHub.SelfCare.selector), 0);
        assertEq(system.reserves(address(hub)), FEE_FLOAT - first.fee, "the fee came from the reserves");

        // Each later reaction puts the fee of the reaction before it back. The system contract takes the
        // fee of a reaction after `react()` returns.
        TriggerResult memory previous = first;
        for (uint256 i = 0; i < 3; ++i) {
            uint256 balanceBefore = address(hub).balance;
            mark = _logMark();
            TriggerResult memory current = _skipReaction();

            assertEq(_selfCareSum(mark), previous.fee);
            assertEq(balanceBefore - address(hub).balance, previous.fee);
            assertEq(
                system.reserves(address(hub)) + current.fee, FEE_FLOAT, "the reserves were at the target after react()"
            );
            previous = current;
        }
    }

    function test_selfCare_staysInsideTheDailyCap() public {
        _keepClearOfMidnight();
        uint256 cap = 0.03 ether;
        vm.prank(owner);
        hub.setSelfCare(FEE_FLOAT, cap);

        uint256 mark = _logMark();
        uint256 balanceBefore = address(hub).balance;
        uint256 fees;
        for (uint256 i = 0; i < 6; ++i) {
            fees += _skipReaction().fee;
            assertLe(_selfCareSum(mark), cap, "at most the cap in one calendar day");
        }
        assertGt(fees, 2 * cap, "the reactions cost more than the cap");
        assertEq(_selfCareSum(mark), cap, "the cap of the day is used");
        assertEq(balanceBefore - address(hub).balance, cap);
        assertEq(system.reserves(address(hub)), FEE_FLOAT - fees + cap);

        // The next calendar day has a new cap.
        _skipTime(_secondsToNextDay());
        uint256 missing = FEE_FLOAT - system.reserves(address(hub));
        assertGt(missing, cap);
        mark = _logMark();
        _skipReaction();
        assertEq(_selfCareSum(mark), cap);
        _skipReaction();
        assertEq(_selfCareSum(mark), cap, "and no more on that day");
    }

    function test_selfCare_neverTakesThePrincipalBelowTheFloor() public {
        uint256 floor = address(hub).balance - 0.005 ether;
        vm.prank(owner);
        hub.setPrincipalFloor(floor);

        uint256 mark = _logMark();
        for (uint256 i = 0; i < 4; ++i) {
            _skipReaction();
            assertGe(address(hub).balance, floor);
        }

        assertEq(_selfCareSum(mark), 0.005 ether, "self-care used what is above the floor");
        assertEq(address(hub).balance, floor);
        assertEq(_hubLogs(mark, LifelineHub.HubLow.selector), 0, "a balance at the floor is not below it");
    }

    function test_selfCare_isOffWithAZeroTarget() public {
        vm.prank(owner);
        hub.setSelfCare(0, SELF_CARE_DAILY_CAP);
        uint256 balanceBefore = address(hub).balance;

        uint256 mark = _logMark();
        _skipReaction();
        _skipReaction();

        assertEq(_hubLogs(mark, LifelineHub.SelfCare.selector), 0);
        assertEq(address(hub).balance, balanceBefore);
    }

    // ------------------------------------------------------------------------------------------
    // disarm and unsubscribeAll
    // ------------------------------------------------------------------------------------------

    function test_disarm_stopsTheTarget() public {
        address first = address(_deployCanary());
        address second = address(_deployCanary());
        uint256 enrollMark = _logMark();
        _enroll(first, _policy());
        _enroll(second, _policy());
        ChainLog memory subscribe = _hubTupleLog(enrollMark, ISystemContract.Subscribe.selector, 0);

        vm.expectRevert(abi.encodeWithSelector(LifelineHub.NotOwner.selector));
        vm.prank(stranger);
        hub.disarm(LASNA_CHAIN_ID, first);

        uint256 mark = _logMark();
        vm.prank(owner);
        hub.disarm(LASNA_CHAIN_ID, first);

        assertEq(_hubLogs(mark, LifelineHub.Disarmed.selector), 1);
        ChainLog memory disarmed = _findLog(mark, address(hub), LifelineHub.Disarmed.selector, 0);
        assertEq(disarmed.topics[1], LASNA_CHAIN_ID);
        assertEq(disarmed.topics[2], uint256(uint160(first)));
        assertEq(_tupleOf(_hubTupleLog(mark, ISystemContract.Unsubscribe.selector, 0)), _tupleOf(subscribe));
        assertEq(_activeSubscriptions(address(hub)), 1, "the other target stays subscribed");

        // The target is not in the list. Its record stays, in state Disarmed.
        assertEq(hub.targetCount(LASNA_CHAIN_ID), 1);
        assertEq(hub.targetAt(LASNA_CHAIN_ID, 0), second);
        (TargetKind kind, TargetState state,) = hub.targetInfo(LASNA_CHAIN_ID, first);
        assertEq(uint256(kind), uint256(TargetKind.Lasna));
        assertEq(uint256(state), uint256(TargetState.Disarmed));

        vm.expectRevert(abi.encodeWithSelector(LifelineHub.NotEnrolled.selector, LASNA_CHAIN_ID, first));
        vm.prank(owner);
        hub.disarm(LASNA_CHAIN_ID, first);

        // A debt log for the target wakes nothing.
        uint256 hubReserves = system.reserves(address(hub));
        mark = _logMark();
        uint256 debt = _pokeIntoDebt(first);
        assertEq(_countLogsOf(mark, address(hub)), 0);
        assertEq(system.reserves(address(hub)), hubReserves);

        // A forged debt log for it is skipped.
        IReactive.LogRecord memory debtLog =
            _logRecord(_findLogIndex(mark, SYSTEM_ADDRESS, IProxyLedger.BlacklistContract.selector, 0));
        mark = _assertGuardAfterRefill(debtLog);
        _assertSkipped(mark, LASNA_CHAIN_ID, first, LifelineHub.SkipReason.NotEnrolled);
        assertEq(system.debt(first), debt);

        // The other target still works.
        mark = _debtRound(second);
        _assertRescued(mark, second);

        // `enroll` is permitted again, and it reconciles at once.
        mark = _logMark();
        _enroll(first, _policy());
        _assertRescued(mark, first);
        assertEq(hub.targetCount(LASNA_CHAIN_ID), 2);
        assertEq(hub.targetAt(LASNA_CHAIN_ID, 1), first);
        assertEq(uint256(_state(first)), uint256(TargetState.Active));
    }

    function test_unsubscribeAll_leavesNoActiveSubscription() public {
        address first = address(_deployCanary());
        address second = address(_deployCanary());
        address looping = _enrollLoopingCanary();
        uint256 enrollMark = _logMark();
        _enroll(first, _policy());
        _enroll(second, _policy());
        _quarantine(looping);
        assertEq(_activeSubscriptions(address(hub)), 2);

        vm.expectRevert(abi.encodeWithSelector(LifelineHub.NotOwner.selector));
        vm.prank(stranger);
        hub.unsubscribeAll();

        uint256 mark = _logMark();
        vm.prank(owner);
        hub.unsubscribeAll();

        // Each tuple is unsubscribed with the six fields of its Subscribe.
        assertEq(_activeSubscriptions(address(hub)), 0, "no active subscription of the hub");
        assertEq(_countLogs(mark, SYSTEM_ADDRESS, ISystemContract.Unsubscribe.selector), 2);
        bytes32 subscribed = keccak256(_tupleOf(_hubTupleLog(enrollMark, ISystemContract.Subscribe.selector, 0)))
            ^ keccak256(_tupleOf(_hubTupleLog(enrollMark, ISystemContract.Subscribe.selector, 1)));
        bytes32 unsubscribed = keccak256(_tupleOf(_hubTupleLog(mark, ISystemContract.Unsubscribe.selector, 0)))
            ^ keccak256(_tupleOf(_hubTupleLog(mark, ISystemContract.Unsubscribe.selector, 1)));
        assertEq(unsubscribed, subscribed, "the same tuples");

        // Each active target is parked. The targets stay enrolled.
        assertEq(uint256(_state(first)), uint256(TargetState.Parked));
        assertEq(uint256(_state(second)), uint256(TargetState.Parked));
        assertEq(uint256(_state(looping)), uint256(TargetState.Quarantined));
        assertEq(hub.targetCount(LASNA_CHAIN_ID), 3);

        // A second call has nothing to do.
        mark = _logMark();
        vm.prank(owner);
        hub.unsubscribeAll();
        assertEq(_countLogs(mark, SYSTEM_ADDRESS, ISystemContract.Unsubscribe.selector), 0);

        // A debt log wakes nothing.
        uint256 hubReserves = system.reserves(address(hub));
        mark = _logMark();
        _pokeIntoDebt(first);
        assertEq(_countLogsOf(mark, address(hub)), 0);
        assertEq(system.reserves(address(hub)), hubReserves);

        // A forged debt log for a parked target is skipped.
        IReactive.LogRecord memory debtLog =
            _logRecord(_findLogIndex(mark, SYSTEM_ADDRESS, IProxyLedger.BlacklistContract.selector, 0));
        mark = _assertGuardAfterRefill(debtLog);
        _assertSkipped(mark, LASNA_CHAIN_ID, first, LifelineHub.SkipReason.NotActive);

        // A target is restored with `disarm` and `enroll`.
        vm.prank(owner);
        hub.disarm(LASNA_CHAIN_ID, first);
        mark = _logMark();
        _enroll(first, _policy());
        _assertRescued(mark, first);
        assertEq(_activeSubscriptions(address(hub)), 1);
        assertEq(uint256(_state(first)), uint256(TargetState.Active));
    }
}
