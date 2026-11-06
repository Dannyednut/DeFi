// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

interface IUniswapV3Pool {
    function token0() external view returns (address);
    function token1() external view returns (address);
    function fee() external view returns (uint24);
    function slot0() external view returns (uint160 sqrtPriceX96, int24 tick, uint16 observationIndex, uint16 obsCardinality, uint16 obsCardinalityNext, uint8 feeProtocol, bool unlocked);
    function liquidity() external view returns (uint128);
    // swap: amountSpecified > 0 exact in, amountSpecified < 0 exact out (pool will send -amountSpecified to recipient)
    function swap(address recipient, bool zeroForOne, int256 amountSpecified, uint160 sqrtPriceLimitX96, bytes calldata data) external returns (int256 amount0, int256 amount1);

    // flash: pool sends amount0/amount1 to recipient and later calls back uniswapV3FlashCallback
    function flash(address recipient, uint256 amount0, uint256 amount1, bytes calldata data) external;
}