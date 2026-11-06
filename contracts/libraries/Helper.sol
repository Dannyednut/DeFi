// SPDX-License-Identifier: GPL-2.0-or-later
pragma solidity ^0.8.19;

import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "../interfaces/IUniswapV3Pool.sol";
import "../interfaces/IUniswapV2Pair.sol";
import "../interfaces/IQuoter.sol";

library Helper {
    uint256 private constant Q96 = 2**96;
    uint256 private constant PHI_INV = 618034;
    uint256 private constant PRECISION = 1000000;

    // -------------------------------------------------------------------------
    // V2 math
    // -------------------------------------------------------------------------

    function getAmountOutV2(uint256 amountIn, uint256 reserveIn, uint256 reserveOut) internal pure returns (uint256 out) {
        if (amountIn == 0) revert("amt");
        if (reserveIn == 0 || reserveOut == 0) revert("liq");
        assembly {
            let aif := mul(amountIn, 997)
            out := div(mul(aif, reserveOut), add(mul(reserveIn, 1000), aif))
        }
    }

    function getAmountInV2(uint256 amountOut, uint256 reserveIn, uint256 reserveOut) internal pure returns (uint256 amtIn) {
        if (amountOut == 0) revert("amt");
        if (reserveIn == 0 || reserveOut <= amountOut) revert("liq");
        assembly {
            amtIn := add(div(mul(mul(reserveIn, amountOut), 1000), mul(sub(reserveOut, amountOut), 997)), 1)
        }
    }

    function getAmountOutV2WithFee(uint256 amountIn, uint256 reserveIn, uint256 reserveOut, uint24 fee) internal pure returns (uint256 out) {
        if (amountIn == 0) revert("amt");
        if (reserveIn == 0 || reserveOut == 0) revert("liq");
        assembly {
            let g := sub(1000000, fee)
            let aif := mul(amountIn, g)
            out := div(mul(aif, reserveOut), add(mul(reserveIn, 1000000), aif))
        }
    }

    function getAmountInV2WithFee(uint256 amountOut, uint256 reserveIn, uint256 reserveOut, uint24 fee) internal pure returns (uint256 amtIn) {
        if (amountOut == 0) revert("amt");
        if (reserveIn == 0 || reserveOut <= amountOut) revert("liq");
        assembly {
            let g := sub(1000000, fee)
            amtIn := add(div(mul(mul(reserveIn, amountOut), 1000000), mul(sub(reserveOut, amountOut), g)), 1)
        }
    }

     // ── Assembly helpers ──────────────────────────────────────────────────────

    function _token0(address pool) internal view returns (address t) {
        assembly {
            let ptr := mload(0x40)
            mstore(ptr, 0x0dfe168100000000000000000000000000000000000000000000000000000000)
            if iszero(staticcall(gas(), pool, ptr, 0x04, ptr, 0x20)) { revert(0, 0) }
            t := mload(ptr)
        }
    }

    function _token1(address pool) internal view returns (address t) {
        assembly {
            let ptr := mload(0x40)
            mstore(ptr, 0xd21220a700000000000000000000000000000000000000000000000000000000)
            if iszero(staticcall(gas(), pool, ptr, 0x04, ptr, 0x20)) { revert(0, 0) }
            t := mload(ptr)
        }
    }

    function _balanceOf(address token, address account) internal view returns (uint256 bal) {
        assembly {
            let ptr := mload(0x40)
            mstore(ptr, 0x70a0823100000000000000000000000000000000000000000000000000000000)
            mstore(add(ptr, 4), account)
            if iszero(staticcall(gas(), token, ptr, 0x24, ptr, 0x20)) { revert(0, 0) }
            bal := mload(ptr)
        }
    }

    function _safeTransfer(address token, address to, uint256 amount) internal {
        assembly {
            let ptr := mload(0x40)
            mstore(ptr, 0xa9059cbb00000000000000000000000000000000000000000000000000000000)
            mstore(add(ptr, 4), to)
            mstore(add(ptr, 36), amount)
            let ok := call(gas(), token, 0, ptr, 0x44, ptr, 0x20)
            if iszero(and(ok, or(iszero(returndatasize()), mload(ptr)))) {
                mstore(0x00, 0x356680b7) 
                revert(0x1c, 0x04)
            }
        }
    }
    
    // -------------------------------------------------------------------------
    // Pool detection & token resolution
    // -------------------------------------------------------------------------

    function _isUniswapV3(address pool) internal view returns (bool ok) {
        assembly {
            let ptr := mload(0x40)
            mstore(ptr, 0x3850c7bd00000000000000000000000000000000000000000000000000000000)
            ok := staticcall(5000, pool, ptr, 0x04, ptr, 0xe0)
        }
    }

    function _isContract(address addr) internal view returns (bool isObj) {
        assembly { isObj := gt(extcodesize(addr), 0) }
    }

    // -------------------------------------------------------------------------
    // Reserve helpers
    // -------------------------------------------------------------------------

    function getEffectiveReservesV3(address pool) internal view returns (uint256 reserve0, uint256 reserve1) {
        (uint160 sqrtPriceX96,,,,,,) = IUniswapV3Pool(pool).slot0();
        uint128 L = IUniswapV3Pool(pool).liquidity();
        if (L == 0) revert("no liq");
        reserve0 = (uint256(L) << 96) / uint256(sqrtPriceX96);
        reserve1 = (uint256(L) * uint256(sqrtPriceX96)) >> 96;
    }

    function _getOrderedReserves(address pool, address tokenIn) internal view returns (uint256 resIn, uint256 resOut) {
        uint256 r0; uint256 r1; address t0 = _token0(pool);
        if (_isUniswapV3(pool)) {
            (r0, r1) = getEffectiveReservesV3(pool);
        } else {
            (uint112 _r0, uint112 _r1,) = IUniswapV2Pair(pool).getReserves();
            r0 = _r0; r1 = _r1;
        }
        (resIn, resOut) = tokenIn == t0 ? (r0, r1) : (r1, r0);
    }

    // -------------------------------------------------------------------------
    // Pool sorting (Zero-Decimal Architecture)
    // -------------------------------------------------------------------------

    function sortPools(
        address[] memory pools,
        address[] memory tokens,
        uint24[] memory fees,
        bool borrowTokenSmaller
    ) internal view returns (address[] memory, address[] memory, uint24[] memory) {
        if (pools.length > 2) return (pools, tokens, fees);

        uint256 p0Q96 = _getPoolPriceQ96(pools[0], borrowTokenSmaller);
        uint256 p1Q96 = _getPoolPriceQ96(pools[1], borrowTokenSmaller);

        // Sort: pools[0] is high price pool, pools[1] is low price pool
        if (p0Q96 < p1Q96) {
            (pools[0], pools[1], fees[0], fees[1]) = (pools[1], pools[0], fees[1], fees[0]);
        }

        address t0 = _token0(pools[0]);
        address t1 = _token1(pools[0]);
        
        tokens[0] = borrowTokenSmaller ? t0 : t1;
        tokens[1] = borrowTokenSmaller ? t1 : t0;
        tokens[2] = tokens[0];

        return (pools, tokens, fees);
    }

    function _getPoolPriceQ96(address pool, bool borrowTokenSmaller) private view returns (uint256) {
        if (_isUniswapV3(pool)) {
            (uint160 sqrtPriceX96,,,,,,) = IUniswapV3Pool(pool).slot0();
            uint256 priceX192 = uint256(sqrtPriceX96) * uint256(sqrtPriceX96);
            uint256 p1Per0 = priceX192 >> 96;
            return borrowTokenSmaller ? p1Per0 : (p1Per0 > 0 ? (uint256(1) << 192) / p1Per0 : 0);
        } else {
            (uint112 r0, uint112 r1,) = IUniswapV2Pair(pool).getReserves();
            return borrowTokenSmaller
                ? (uint256(r1) << 96) / uint256(r0)
                : (uint256(r0) << 96) / uint256(r1);
        }
    }

    // -------------------------------------------------------------------------
    // Optimal borrow calculation
    // -------------------------------------------------------------------------

    function calcOptimalV2Borrow(
        uint256 resInP0, uint256 resOutP0, uint256 resInP1, uint256 resOutP1, uint24 feeP0, uint24 feeP1, uint8 mode
    ) internal pure returns (uint256 amount) {
        uint256 d_a = _scalingFactor(resInP0 < resOutP1 ? resInP0 : resOutP1);
        uint256 d_b = _scalingFactor(resOutP0 < resInP1 ? resOutP0 : resInP1);

        int256 a1 = int256(resInP0 / d_a);
        int256 b1 = int256(resOutP0 / d_b);
        int256 a2 = int256(resInP1 / d_b);
        int256 b2 = int256(resOutP1 / d_a);

        if (a1 <= 0 || b1 <= 0 || a2 <= 0 || b2 <= 0) return 0;

        int256 g0 = int256(uint256(1e6 - feeP0));
        int256 g1 = int256(uint256(1e6 - feeP1));

        int256 k = (a2 * g1) / 1e6 + (b1 * g0 * g1) / 1e12;
        int256 qa = k * k;
        int256 qb = 2 * k * a1 * a2;
        int256 qc = a1 * a2 * a1 * a2 - (b1 * a1 * a2 * b2 * g0 * g1) / 1e12;

        (int256 x1, int256 x2) = _solveQuadratic(qa, qb, qc);

        int256 x = (x1 > 0 && x1 < b2) ? x1 : x2;
        if (x <= 0 || x >= b2) return 0;

        amount = uint256(x) * d_a;
        if (mode == 1) amount = getAmountOutV2WithFee(amount, resInP0, resOutP0, feeP0);
    }

    function calcOptimalBorrow(
        address quoter, address[] memory pools, address[] memory tokens, uint24[] memory fees, uint8 mode
    ) internal view returns (uint256) {
        if (pools.length == 2){
            (uint256 resInP0, uint256 resOutP0) = _getOrderedReserves(pools[0], tokens[0]);
            (uint256 resInP1, uint256 resOutP1) = _getOrderedReserves(pools[1], tokens[1]);
            return calcOptimalV2Borrow(resInP0, resOutP0, resInP1, resOutP1, fees[0], fees[1], mode);
        } else {
            (uint256 amount,) = findOptimalAmount(quoter, tokens, pools, fees, mode, 35);
            return amount;
        }
    }

    function _solveQuadratic(int256 a, int256 b, int256 c) private pure returns (int256 x1, int256 x2) {
        int256 disc = b * b - 4 * a * c;
        if (disc <= 0) return (0, 0);
        int256 sqrtDisc = int256(sqrt(uint256(disc)));
        x1 = (-b + sqrtDisc) / (2 * a);
        x2 = (-b - sqrtDisc) / (2 * a);
    }

    function sqrt(uint256 x) internal pure returns (uint256 z) {
        assembly {
            z := x
            let y := add(shr(1, x), 1)
            for {} lt(y, z) {} {
                z := y
                y := shr(1, add(div(x, y), y))
            }
        }
    }

    function _scalingFactor(uint256 min) private pure returns (uint256 d) {
        if (min > 1e21) d = 1e17;
        else if (min > 1e18) d = 1e14;
        else if (min > 1e15) d = 1e11;
        else if (min > 1e10) d = 1e6;
        else if (min > 1e6)  d = 1e2;
        else d = 1;
    }

    function _validatePoolTokens(address[] memory tokens, address[] memory pools) internal view {
        uint256 len = pools.length;
        for (uint256 i = 0; i < len; ) {
            if (!_isContract(pools[i])) revert("not contract");
            address tokenIn = tokens[i];
            address tokenOut = tokens[i + 1];
            (address pt0, address pt1) = (_token0(pools[i]), _token1(pools[i]));
            
            if (pt0 >= pt1) revert("nonstandard pair");
            if (!((pt0 == tokenIn && pt1 == tokenOut) || (pt0 == tokenOut && pt1 == tokenIn))) revert("mismatch");
            
            unchecked { ++i; }
        }
    }

    // -------------------------------------------------------------------------
    // Golden Section Search
    // Note: Heavily gas consuming on-chain. Best used locally/off-chain.
    // -------------------------------------------------------------------------

    function findOptimalAmount(
        address quoter, address[] memory tokens, address[] memory pools, uint24[] memory fees, uint8 mode, uint256 maxIterations
    ) internal view returns (uint256 bestAmount, uint256 bestProfit) {
        if (tokens.length < 2 || fees.length != tokens.length - 1) return (0, 0);

        (uint256 low, uint256 high, uint256 seedBestAmt, uint256 seedBestProfit) = _findBracket(quoter, tokens, pools, fees);
        if (low == 0 && high == 0) return (0, 0);

        bestAmount = seedBestAmt;
        bestProfit = seedBestProfit;

        uint256 span = high - low;
        uint256 c1 = low + (span * PHI_INV) / PRECISION;
        uint256 c2 = low + (span * (PRECISION - PHI_INV)) / PRECISION;

        uint256 f1 = _quoteProfit(quoter, tokens, pools, fees, c1);
        uint256 f2 = _quoteProfit(quoter, tokens, pools, fees, c2);

        if (f1 > bestProfit) { bestProfit = f1; bestAmount = c1; }
        if (f2 > bestProfit) { bestProfit = f2; bestAmount = c2; }

        for (uint256 i = 0; i < maxIterations; ) {
            if (high - low < 1000) break;

            if (f1 > f2) {
                low = c2; c2 = c1; f2 = f1; span = high - low;
                c1 = low + (span * PHI_INV) / PRECISION;
                f1 = _quoteProfit(quoter, tokens, pools, fees, c1);
            } else {
                high = c1; c1 = c2; f1 = f2; span = high - low;
                c2 = low + (span * (PRECISION - PHI_INV)) / PRECISION;
                f2 = _quoteProfit(quoter, tokens, pools, fees, c2);
            }

            if (f1 > bestProfit) { bestProfit = f1; bestAmount = c1; }
            if (f2 > bestProfit) { bestProfit = f2; bestAmount = c2; }
            
            unchecked { ++i; }
        }

        if (mode == 1) {
            (uint256 resInP0, uint256 resOutP0) = _getOrderedReserves(pools[0], tokens[0]);
            bestAmount = getAmountOutV2WithFee(bestAmount, resInP0, resOutP0, fees[0]);
        }
    }

    function _findBracket(
        address quoter, address[] memory tokens, address[] memory pools, uint24[] memory fees
    ) private view returns (uint256 low, uint256 high, uint256 bestAmt, uint256 bestProfit) {
        // Unrolled memory array for gas savings
        uint256[5] memory probes = [uint256(1e15), 1e17, 1e19, 1e21, 1e23];
        uint256 bestIdx;

        for (uint256 i = 0; i < 5; ) {
            uint256 amt = probes[i];
            uint256 profit = _quoteProfit(quoter, tokens, pools, fees, amt);

            if (profit > bestProfit) {
                bestProfit = profit;
                bestAmt = amt;
                bestIdx = i;
            }
            unchecked { ++i; }
        }

        if (bestProfit == 0) return (0, 0, 0, 0);

        low = bestIdx > 0 ? probes[bestIdx - 1] : bestAmt / 10;
        high = bestIdx < 4 ? probes[bestIdx + 1] : bestAmt * 10;
    }

    function _quoteProfit(
        address quoter, address[] memory tokens, address[] memory pools, uint24[] memory fees, uint256 amountIn
    ) private view returns (uint256 profit) {
        if (amountIn == 0) return 0;
        uint256 current = amountIn;
        uint256 len = pools.length;

        for (uint256 i = 0; i < len; ) {
            if (_isUniswapV3(pools[i])) {
                (current,,,) = IQuoter(quoter).quoteExactInputSingle(
                    IQuoter.QuoteExactInputSingleParams({
                        tokenIn: tokens[i], tokenOut: tokens[i+1], amountIn: current, fee: fees[i], sqrtPriceLimitX96: 0
                    })
                );
            } else {
                (uint112 r0, uint112 r1,) = IUniswapV2Pair(pools[i]).getReserves();
                current = tokens[i] == _token0(pools[i]) 
                    ? getAmountOutV2(current, r0, r1)
                    : getAmountOutV2(current, r1, r0);
            }
            unchecked { ++i; }
        }
        profit = current > amountIn ? current - amountIn : 0;
    }
}