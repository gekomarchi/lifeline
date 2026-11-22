// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.30;

import { Test } from "forge-std/Test.sol";
import { Policy, MAX_RESCUES_LIMIT, InvalidPolicy, validatePolicy } from "../src/LifelineTypes.sol";

/// @notice Tests of the shared policy validation. They need no fork.
contract LifelineTypesTest is Test {
    /// @dev An external entry, so that a test can expect the revert of the free function.
    function validate(Policy memory p) external pure {
        validatePolicy(p);
    }

    function _valid() internal pure returns (Policy memory) {
        return Policy({ low: 1, high: 2, maxDebt: 3, dailyCap: 4, maxRescues: 3, window: 3600 });
    }

    function _assertInvalid(Policy memory p) internal {
        vm.expectRevert(InvalidPolicy.selector);
        this.validate(p);
    }

    function test_validatePolicy_acceptsAValidPolicy() public view {
        this.validate(_valid());

        Policy memory p = _valid();
        p.low = 0;
        this.validate(p);
        p.low = p.high;
        this.validate(p);
        p.maxRescues = 1;
        this.validate(p);
        p.maxRescues = MAX_RESCUES_LIMIT;
        this.validate(p);
    }

    function test_validatePolicy_rejectsEachBrokenRule() public {
        Policy memory p = _valid();
        p.high = 0;
        p.low = 0;
        _assertInvalid(p);

        p = _valid();
        p.low = p.high + 1;
        _assertInvalid(p);

        p = _valid();
        p.maxDebt = 0;
        _assertInvalid(p);

        p = _valid();
        p.dailyCap = 0;
        _assertInvalid(p);

        p = _valid();
        p.maxRescues = 0;
        _assertInvalid(p);

        p = _valid();
        p.maxRescues = MAX_RESCUES_LIMIT + 1;
        _assertInvalid(p);

        p = _valid();
        p.window = 0;
        _assertInvalid(p);
    }

    function test_maxRescuesLimit_isFour() public pure {
        assertEq(MAX_RESCUES_LIMIT, 4);
    }

    /// @dev Fuzz: the function reverts exactly when a rule is broken.
    function test_validatePolicy_matchesTheRules(Policy memory p) public {
        bool valid = p.high > 0 && p.low <= p.high && p.maxDebt > 0 && p.dailyCap > 0 && p.maxRescues >= 1
            && p.maxRescues <= 4 && p.window > 0;
        if (!valid) vm.expectRevert(InvalidPolicy.selector);
        this.validate(p);
    }
}
