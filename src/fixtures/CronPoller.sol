// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.30;

import { IReactive } from "@reactive/src/interfaces/IReactive.sol";
import { ISystemContract, REACTIVE_IGNORE } from "../interfaces/ISystemContract.sol";

/// @dev The emitter of the cron logs on Lasna: `CRON_EMITTER` of `src/LifelineHub.sol`.
address constant POLLER_CRON_EMITTER = 0x0000000000000000000000000000000000fffFfF;

/// @title Experiment fixture, arm B: a cron poller that refills one ward.
/// @notice The control of the three-arm experiment: the pattern "poll on a cron tick, then `depositTo`". It
///         subscribes to one cron topic. On each tick it reads the ward at the system contract. If the ward
///         has a debt or reserves below `low`, it pays `debt + (high - reserves)` from its balance.
///
///         It has no `pay()`: its fees come from the reserves that the constructor deposits (`feeFloat`),
///         and nothing refills them. It hears no debt event, so its ward stays in debt until the next tick.
contract CronPoller {
    /// @notice The poller paid `amount` to the ledger account of the ward.
    event Paid(address indexed ward, uint256 amount);
    /// @notice The ward needed `amount`, and the poller did not pay: its balance is too low, or the ledger
    ///         refused the read or the deposit. `amount` is 0 after a refused read, and the largest value if
    ///         the sum of the debt and the refill has more than 256 bits.
    event PaymentFailed(address indexed ward, uint256 amount);

    error NotOwner();
    error NotSystem(address caller);
    error InvalidMarks(uint256 low, uint256 high);
    error InsufficientValue(uint256 value, uint256 feeFloat);
    error WithdrawFailed();

    address public immutable owner;
    /// @notice The reactive contract that the poller refills.
    address public immutable ward;
    /// @notice `topic0` of the cron event that the poller listens to.
    uint256 public immutable cronTopic;
    /// @notice The poller refills the ward when its reserves are below this mark.
    uint256 public immutable low;
    /// @notice The reserves of the ward after a payment.
    uint256 public immutable high;
    /// @notice The part of the deployment value that went to the reserves of the poller. The measuring
    ///         command reads it: the fees of the poller are this value minus what the ledger still holds.
    uint256 public immutable feeFloat;

    ISystemContract private immutable SYSTEM;

    /// @param systemContract The system contract of Reactive Network.
    /// @param cronTopic_ `topic0` of the cron event: the one of the hub, for the same period.
    /// @param ward_ The reactive contract to refill.
    /// @param low_ The refill mark, in wei.
    /// @param high_ The reserves after a payment, in wei.
    /// @param feeFloat_ The part of `msg.value` that goes to the reserves of the poller. The rest stays as
    ///        the balance that pays the ward.
    constructor(
        address systemContract,
        uint256 cronTopic_,
        address ward_,
        uint256 low_,
        uint256 high_,
        uint256 feeFloat_
    ) payable {
        if (high_ == 0 || low_ > high_) revert InvalidMarks(low_, high_);
        if (msg.value < feeFloat_) revert InsufficientValue(msg.value, feeFloat_);

        owner = msg.sender;
        SYSTEM = ISystemContract(systemContract);
        cronTopic = cronTopic_;
        ward = ward_;
        low = low_;
        high = high_;
        feeFloat = feeFloat_;

        if (feeFloat_ != 0) {
            SYSTEM.depositTo{ value: feeFloat_ }(address(this));
        }
        SYSTEM.subscribe(
            block.chainid, POLLER_CRON_EMITTER, cronTopic_, REACTIVE_IGNORE, REACTIVE_IGNORE, REACTIVE_IGNORE
        );
    }

    /// @notice Accepts funds for the payments. The ledger's `pay(uint256)` call has calldata and does not
    ///         reach this.
    receive() external payable { }

    /// @notice Returns funds of the balance to the owner. Reserves cannot be withdrawn.
    function withdraw(uint256 amount) external {
        if (msg.sender != owner) revert NotOwner();
        (bool ok,) = owner.call{ value: amount }("");
        if (!ok) revert WithdrawFailed();
    }

    /// @notice The reaction to a cron tick. After the caller check it never reverts: a payment that cannot
    ///         be made gives `PaymentFailed`.
    function react(IReactive.LogRecord calldata log) external {
        if (msg.sender != address(SYSTEM)) revert NotSystem(msg.sender);
        if (log.chainId != block.chainid || log.contractAddress != POLLER_CRON_EMITTER || log.topic0 != cronTopic) {
            return;
        }

        uint256 debt;
        uint256 reserves;
        try SYSTEM.debt(ward) returns (uint256 debt_) {
            debt = debt_;
        } catch {
            emit PaymentFailed(ward, 0);
            return;
        }
        try SYSTEM.reserves(ward) returns (uint256 reserves_) {
            reserves = reserves_;
        } catch {
            emit PaymentFailed(ward, 0);
            return;
        }

        // The healthy ward: no event and no payment.
        if (debt == 0 && reserves >= low) return;

        uint256 refill = reserves < high ? high - reserves : 0;
        // `depositTo` spends the balance; reserves cannot fund it. Check before the call, with no sum that can
        // overflow: the marks and the debt can have any value.
        uint256 balance = address(this).balance;
        if (balance < debt || balance - debt < refill) {
            emit PaymentFailed(ward, refill > type(uint256).max - debt ? type(uint256).max : debt + refill);
            return;
        }
        // No overflow: `debt + refill <= balance`.
        uint256 amount = debt + refill;
        if (amount == 0) return;
        try SYSTEM.depositTo{ value: amount }(ward) {
            emit Paid(ward, amount);
        } catch {
            emit PaymentFailed(ward, amount);
        }
    }
}
