// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

interface IBalancerVault {
    // simplified vault flash interface
    function flashLoan(address recipient, address[] calldata tokens, uint256[] calldata amounts, bytes calldata userData) external;
}