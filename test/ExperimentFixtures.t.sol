// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.30;

import { IReactive } from "@reactive/src/interfaces/IReactive.sol";
import { LasnaForkHarness } from "./harness/LasnaForkHarness.sol";
import { ISystemContractCallbacks } from "./harness/TwoForkHarness.sol";
import { CRON_10000_TOPIC0, CRON_1000_TOPIC0 } from "../src/LifelineHub.sol";
import { CanaryReactive } from "../src/fixtures/CanaryReactive.sol";
import { CronPoller, POLLER_CRON_EMITTER } from "../src/fixtures/CronPoller.sol";
import { JunkRequester } from "../src/fixtures/JunkRequester.sol";

/// @notice Fork tests of the two experiment fixtures of the three-arm experiment. The fixtures and the
///         system contract are real; only the node is emulated by the harness.
contract ExperimentFixturesTest is LasnaForkHarness {
    uint256 internal constant LOW = 0.02 ether;
    uint256 internal constant HIGH = 0.06 ether;
    uint256 internal constant FEE_FLOAT = 0.2 ether;
    uint256 internal constant PRINCIPAL = 0.3 ether;

    address internal owner;
    address internal stranger;
    CanaryReactive internal ward;
    CronPoller internal poller;

    function setUp() public {
        _forkLasna();
        owner = _freshAccount("owner");
        stranger = _freshAccount("stranger");
        vm.deal(owner, 100 ether);
        vm.deal(stranger, 10 ether);

        _nextFreshContract(owner);
        vm.prank(owner);
        ward = new CanaryReactive(SYSTEM_ADDRESS, 0, address(0), 0);
        _nextFreshContract(owner);
        vm.prank(owner);
        poller = new CronPoller{ value: FEE_FLOAT + PRINCIPAL }(
            SYSTEM_ADDRESS, CRON_10000_TOPIC0, address(ward), LOW, HIGH, FEE_FLOAT
        );
    }

    function _cron(uint256 topic0) internal view returns (IReactive.LogRecord memory record) {
        record.chainId = LASNA_CHAIN_ID;
        record.contractAddress = POLLER_CRON_EMITTER;
        record.topic0 = topic0;
        record.topic1 = vm.getBlockNumber();
        record.blockNumber = vm.getBlockNumber();
        record.opCode = 2;
    }

    function _tick() internal returns (TriggerResult memory) {
        return _trigger(address(poller), _cron(CRON_10000_TOPIC0));
    }

    function _wardIntoDebt() internal returns (uint256 debt) {
        vm.prank(owner);
        ward.poke();
        _relay();
        debt = system.debt(address(ward));
        assertGt(debt, 0, "the ward is in debt");
    }

    // ------------------------------------------------------------------------------------------
    // CronPoller
    // ------------------------------------------------------------------------------------------

    function test_poller_holdsItsFloatAsReservesAndItsPrincipalAsBalance() public view {
        assertEq(system.reserves(address(poller)), FEE_FLOAT, "the fee float is in the reserves");
        assertEq(address(poller).balance, PRINCIPAL, "the rest is the balance");
        assertEq(poller.feeFloat(), FEE_FLOAT);
        assertEq(poller.ward(), address(ward));
        assertEq(poller.low(), LOW);
        assertEq(poller.high(), HIGH);
        assertEq(poller.cronTopic(), CRON_10000_TOPIC0);
        assertEq(poller.owner(), owner);
    }

    function test_poller_paysDebtPlusRefillAtTheTick() public {
        uint256 debt = _wardIntoDebt();
        uint256 mark = _logMark();

        // The debt event does not wake the poller: the ward stays in debt until the tick.
        assertEq(_relay(), 0, "no trigger for the debt event");
        assertEq(system.debt(address(ward)), debt);

        TriggerResult memory tick = _tick();
        assertTrue(tick.success && !tick.reactReverted, "the tick runs");
        ChainLog memory paid = _onlyLog(mark, address(poller), CronPoller.Paid.selector);
        assertEq(address(uint160(paid.topics[1])), address(ward));
        assertEq(abi.decode(paid.data, (uint256)), debt + HIGH, "debt + (high - reserves)");
        assertEq(system.debt(address(ward)), 0, "the debt is cleared");
        assertEq(system.reserves(address(ward)), HIGH, "the reserves are at the high mark");
        assertEq(address(poller).balance, PRINCIPAL - debt - HIGH, "the payment comes from the balance");
        assertEq(system.reserves(address(poller)), FEE_FLOAT - tick.fee, "the fee comes from the float");
    }

    function test_poller_paysNothingToAHealthyWard() public {
        vm.prank(owner);
        system.depositTo{ value: LOW }(address(ward));
        uint256 mark = _logMark();

        TriggerResult memory tick = _tick();
        assertTrue(tick.success && !tick.reactReverted);
        assertEq(_countLogsOf(mark, address(poller)), 0, "no event for a ward at the low mark");
        assertEq(address(poller).balance, PRINCIPAL);
    }

    function test_poller_refillsAWardBelowTheLowMark() public {
        vm.prank(owner);
        system.depositTo{ value: LOW - 1 }(address(ward));
        uint256 mark = _logMark();

        _tick();
        ChainLog memory paid = _onlyLog(mark, address(poller), CronPoller.Paid.selector);
        assertEq(abi.decode(paid.data, (uint256)), HIGH - (LOW - 1));
        assertEq(system.reserves(address(ward)), HIGH);
    }

    function test_poller_doesNotRevertWhenItCannotPay() public {
        vm.prank(owner);
        poller.withdraw(PRINCIPAL);
        uint256 debt = _wardIntoDebt();
        uint256 mark = _logMark();

        TriggerResult memory tick = _tick();
        assertTrue(tick.success && !tick.reactReverted, "a payment that cannot be made is not a revert");
        ChainLog memory failed = _onlyLog(mark, address(poller), CronPoller.PaymentFailed.selector);
        assertEq(abi.decode(failed.data, (uint256)), debt + HIGH);
        assertEq(_countLogs(mark, address(poller), CronPoller.Paid.selector), 0);
        assertEq(system.debt(address(ward)), debt, "the ward stays in debt");
    }

    function test_poller_skipsAnotherCronTopicAndAnotherEmitter() public {
        _wardIntoDebt();
        uint256 mark = _logMark();

        _trigger(address(poller), _cron(CRON_1000_TOPIC0));
        IReactive.LogRecord memory forged = _cron(CRON_10000_TOPIC0);
        forged.contractAddress = address(ward);
        _trigger(address(poller), forged);
        assertEq(_countLogsOf(mark, address(poller)), 0, "no payment for a log that is not its cron tick");
        assertGt(system.debt(address(ward)), 0);
    }

    function test_poller_rejectsACallerThatIsNotTheSystemContract() public {
        IReactive.LogRecord memory record = _cron(CRON_10000_TOPIC0);
        vm.prank(stranger);
        vm.expectRevert(abi.encodeWithSelector(CronPoller.NotSystem.selector, stranger));
        poller.react(record);
    }

    function test_poller_hasNoPayFunction() public {
        vm.prank(SYSTEM_ADDRESS);
        (bool ok,) = address(poller).call(abi.encodeWithSignature("pay(uint256)", 1));
        assertFalse(ok, "the ledger cannot pull the balance");
    }

    function test_poller_onlyTheOwnerWithdraws() public {
        vm.prank(stranger);
        vm.expectRevert(CronPoller.NotOwner.selector);
        poller.withdraw(1);

        uint256 before = owner.balance;
        vm.prank(owner);
        poller.withdraw(PRINCIPAL);
        assertEq(owner.balance, before + PRINCIPAL);
    }

    function test_poller_constructorChecksItsValues() public {
        vm.startPrank(owner);
        vm.expectRevert(abi.encodeWithSelector(CronPoller.InvalidMarks.selector, HIGH + 1, HIGH));
        new CronPoller(SYSTEM_ADDRESS, CRON_10000_TOPIC0, address(ward), HIGH + 1, HIGH, 0);
        vm.expectRevert(abi.encodeWithSelector(CronPoller.InsufficientValue.selector, 1, 2));
        new CronPoller{ value: 1 }(SYSTEM_ADDRESS, CRON_10000_TOPIC0, address(ward), LOW, HIGH, 2);
        vm.stopPrank();
    }

    // ------------------------------------------------------------------------------------------
    // JunkRequester
    // ------------------------------------------------------------------------------------------

    function test_requester_requestsOneCallbackForTheOwnerOnly() public {
        _nextFreshContract(owner);
        vm.prank(owner);
        JunkRequester requester = new JunkRequester(SYSTEM_ADDRESS);
        bytes memory payload = abi.encodeWithSignature("pong(address,uint256)", address(0), 7);

        vm.prank(stranger);
        vm.expectRevert(JunkRequester.NotOwner.selector);
        requester.request(84_532, address(ward), 250_000, payload);

        uint256 mark = _logMark();
        vm.prank(owner);
        requester.request(84_532, address(ward), 250_000, payload);
        ChainLog memory request = _onlyLog(mark, SYSTEM_ADDRESS, ISystemContractCallbacks.CallbackRequest.selector);
        assertEq(request.topics[1], 84_532, "the chain ID of the recipient");
        assertEq(address(uint160(request.topics[2])), address(requester), "the requester is the sender of the request");
        assertEq(address(uint160(request.topics[3])), address(ward), "the recipient");
        // A plain transaction makes the request: no reaction, no fee for the requester.
        assertEq(system.debt(address(requester)), 0);
    }
}
