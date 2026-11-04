// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

import "@openzeppelin/contracts/access/Ownable.sol";
import "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

import "./interfaces/IUniswapV2Pair.sol";
import "./interfaces/IUniswapV3Pool.sol";
import "./interfaces/IQuoter.sol";
import "./interfaces/IWETH.sol";
import "./interfaces/IBalancerVault.sol";
import {Helper} from "./libraries/Helper.sol";

struct Data {
    uint256 amountIn;
    uint256 minProfit;
    address[] tokens;
    address[] pools;
    uint24[] fees;
    address tokenIn;
    uint8 mode; // 0 = borrow token in, 1 = borrow other token
}

// Custom errors — 4 bytes vs N bytes for require strings
error Auth();
error Block();
error BadPath();
error NoProfit();
error ZeroAddress();
error AmountRequired();
error TransferFailed();
error ModePathMismatch();
error InsufficientBalance();

contract Exec is Ownable, ReentrancyGuard {
    address private immutable WETH;
    address private immutable Quoter;
    address private immutable BALANCER_VAULT;

    // Set before initiating any flash loan/swap, validated inside every callback,
    // and cleared immediately after validation to prevent re-use.
    address private _expectedCallback;

    event DONE(address indexed token, uint256 amt);
    event BATCH(uint256 successful, uint256 failed);

    constructor(address _w, address _q, address _b) Ownable(msg.sender) {
        if (_w == address(0) || _q == address(0)) revert ZeroAddress();
        WETH = _w;
        Quoter = _q;
        BALANCER_VAULT = _b;
    }

    receive() external payable {}

    fallback(bytes calldata _input) external returns (bytes memory) {
        if (_input.length < 4) revert BadPath();
        if (_expectedCallback != msg.sender) revert Auth();
        
        // Manual decode skip to save gas on abi.decode overhead
        (address sender, uint256 amount0, uint256 amount1, bytes memory data) = abi.decode(_input[4:], (address, uint256, uint256, bytes));
        uniswapV2Call(sender, amount0, amount1, data);
        return "";
    }

    function multiCall(bytes[] calldata calls) external onlyOwner returns (uint256 successful, uint256 failed) {
        uint256 len = calls.length;
        
        for (uint256 i = 0; i < len; ) {
            bytes calldata input = calls[i];
            if (input.length > 32) {
                bytes memory realData;
                assembly {
                    let realLen := sub(input.length, 32)
                    realData := mload(0x40)
                    mstore(realData, realLen)
                    calldatacopy(add(realData, 0x20), add(input.offset, 32), realLen)
                    mstore(0x40, and(add(add(realData, 0x20), realLen), not(0x1f)))
                }

                (bool success, ) = address(this).call{gas: gasleft()}(realData);
                if (success) {
                    unchecked { ++successful; }
                }
            }
            unchecked { ++i; }
        }
        
        failed = len - successful;
        emit BATCH(successful, failed);
    }

    function swap(Data calldata arb, bool lender, uint256 builderFeeBps, uint256 validUntilBlock) external nonReentrant {
        address _owner = owner(); // Cache storage variable
        if (msg.sender != _owner && msg.sender != address(this)) revert Auth();
        if (block.number > validUntilBlock) revert Block();
        
        uint256 tokensLen = arb.tokens.length;
        if (tokensLen <= 2) revert BadPath();
        if (arb.pools.length != tokensLen - 1) revert BadPath();
        
        Helper._validatePoolTokens(arb.tokens, arb.pools);

        Data memory arbMem = arb; 
        uint256 profit;
        (arbMem, profit) = yieldOut(arbMem);
        
        uint256 borrowAmt = arbMem.amountIn;

        if (profit < arbMem.minProfit || profit == 0) revert NoProfit();
        if (borrowAmt == 0) revert AmountRequired();
        if (arbMem.tokens[tokensLen - 1] != arbMem.tokenIn) revert BadPath();
        
        uint256 startBalance = Helper._balanceOf(arbMem.tokenIn, address(this));
        bytes memory payload = abi.encode(arbMem, borrowAmt);

        if (lender && BALANCER_VAULT != address(0)) {
            _initiateFlashloan(arbMem, borrowAmt, payload);
        } else {
            _initiateFlashswap(arbMem, borrowAmt, payload);
        }
        
        _processProfit(arbMem, startBalance, builderFeeBps, _owner);
    }

    function _initiateFlashloan(Data memory arb, uint256 amt, bytes memory payload) internal {
        _expectedCallback = BALANCER_VAULT;

        address[] memory a = new address[](1); a[0] = arb.tokenIn;
        uint256[] memory am = new uint256[](1); am[0] = amt;
        
        try IBalancerVault(BALANCER_VAULT).flashLoan(address(this), a, am, payload) {} 
        catch {
            _expectedCallback = address(0);
            (uint256 resIn, uint256 resOut) = Helper._getOrderedReserves(arb.pools[0], arb.tokenIn);
            (arb.amountIn, arb.mode) = (Helper.getAmountOutV2WithFee(arb.amountIn, resIn, resOut, arb.fees[0]), 1);
            _initiateFlashswap(arb, arb.amountIn, abi.encode(arb, arb.amountIn));
        }
    }

    function _initiateFlashswap(Data memory arb, uint256 borrowAmt, bytes memory payload) internal {
        address pool0 = arb.pools[0];
        if (Helper._isUniswapV3(pool0)) {
            if (arb.mode == 0) revert BadPath();
            _expectedCallback = pool0;
            bool z = (arb.tokenIn == Helper._token0(pool0));
            uint160 sqrtLimit = z ? 4295128740 : 1461446703485210103287273052203988822378723970341;
            
            bytes memory data = abi.encode(uint8(2), arb, borrowAmt);
            IUniswapV3Pool(pool0).swap(address(this), z, -int256(borrowAmt), sqrtLimit, data);
            return;
        }

        IUniswapV2Pair pair = IUniswapV2Pair(pool0);
        _expectedCallback = address(pair);
        bool isT0 = arb.tokenIn == Helper._token0(address(pair));

        (uint256 a0out, uint256 a1out) = isT0 ? (uint256(0), borrowAmt) : (borrowAmt, uint256(0));

        try pair.swap(a0out, a1out, address(this), payload) {} 
        catch {
            _expectedCallback = address(0);
            revert("V2 swap flash failed");
        }
    }

    function receiveFlashLoan(address[] memory tokens, uint256[] memory amounts, uint256[] memory feeAmounts, bytes memory userData) external {
        if (msg.sender != _expectedCallback) revert Auth();
        _expectedCallback = address(0);
        
        (Data memory arb, uint256 borrowed) = abi.decode(userData, (Data, uint256));
        _execute(arb, borrowed, 0, 0);
        Helper._safeTransfer(tokens[0], msg.sender, amounts[0] + feeAmounts[0]);
    }

    function uniswapV2Call(address sender, uint amount0, uint amount1, bytes memory data) public {
        if (msg.sender != _expectedCallback || sender != address(this)) revert Auth();
        _expectedCallback = address(0);

        (Data memory arb, uint256 borrowed) = abi.decode(data, (Data, uint256));
        borrowed = amount0 > 0 ? amount0 : amount1;

        _execute(arb, borrowed, 1, 1);
        
        (uint112 r0, uint112 r1,) = IUniswapV2Pair(msg.sender).getReserves();
        uint256 debtAmount = (arb.tokenIn == Helper._token0(msg.sender)) 
            ? Helper.getAmountInV2(borrowed, r0, r1) 
            : Helper.getAmountInV2(borrowed, r1, r0);
            
        Helper._safeTransfer(arb.tokenIn, msg.sender, debtAmount);        
    }

    function uniswapV3SwapCallback(int256 a0, int256 a1, bytes calldata data) external {
        if (msg.sender != _expectedCallback) revert Auth();

        uint8 mode = abi.decode(data, (uint8));
        if (mode == 1) {
            (, address tokenIn, ) = abi.decode(data, (uint8, address, address));
            bool is0 = a0 > 0;
            address req = is0 ? Helper._token0(msg.sender) : Helper._token1(msg.sender);
            uint256 need = is0 ? uint256(a0) : uint256(a1);
            if (req != tokenIn) revert ModePathMismatch();
            Helper._safeTransfer(req, msg.sender, need);
            return;
        }

        _expectedCallback = address(0);
        (, Data memory arb, ) = abi.decode(data, (uint8, Data, uint256));

        bool borrowedIs0 = a0 < 0;
        uint256 borrowed = borrowedIs0 ? uint256(-a0) : uint256(-a1);        
        address t0 = Helper._token0(msg.sender);
        address t1 = Helper._token1(msg.sender);
        address debtToken = borrowedIs0 ? t1 : t0;

        if (debtToken != arb.tokens[arb.tokens.length - 1]) revert ModePathMismatch();

        _execute(arb, borrowed, 1, 1);

        if (a0 > 0) Helper._safeTransfer(t0, msg.sender, uint256(a0));
        if (a1 > 0) Helper._safeTransfer(t1, msg.sender, uint256(a1));
    }

    function _execute(Data memory arb, uint256 borrowed, uint256 startPoolIdx, uint256 startTokenIdx) internal {
        uint256 amt = borrowed;
        uint256 pLen = arb.pools.length;
        
        for (uint i = startPoolIdx; i < pLen; ) {
            amt = _swap(arb.pools[i], arb.tokens[startTokenIdx], arb.tokens[startTokenIdx + 1], amt);
            unchecked { ++i; ++startTokenIdx; }
        }
    }

    function _swap(address pool, address tokenIn, address tokenOut, uint256 amountIn) internal returns (uint256 out) {
        if (Helper._isUniswapV3(pool)) {
            bool z = (tokenIn == Helper._token0(pool));
            uint160 sqrtLimit = z ? 4295128740 : 1461446703485210103287273052203988822378723970341;
            _expectedCallback = pool;
            (int256 a0, int256 a1) = IUniswapV3Pool(pool).swap(address(this), z, int256(amountIn), sqrtLimit, abi.encode(uint8(1), tokenIn, tokenOut));
            _expectedCallback = address(0);
            return z ? uint256(-a1) : uint256(-a0);
        }

        IUniswapV2Pair p2 = IUniswapV2Pair(pool);
        (uint112 r0, uint112 r1,) = p2.getReserves();
        address t0 = Helper._token0(pool);
        
        out = (tokenIn == t0) ? Helper.getAmountOutV2(amountIn, r0, r1) : Helper.getAmountOutV2(amountIn, r1, r0);
        Helper._safeTransfer(tokenIn, pool, amountIn);
        
        (uint256 out0, uint256 out1) = (tokenOut == t0) ? (out, uint256(0)) : (uint256(0), out);
        p2.swap(out0, out1, address(this), "");
    }

    function _processProfit(Data memory arb, uint256 startBalance, uint256 builderFeeBps, address _owner) internal {
        address tIn = arb.tokenIn;
        if (tIn == address(0)) return;
        
        uint256 endBalance = Helper._balanceOf(tIn, address(this));
        if (endBalance <= startBalance) revert NoProfit();
        
        uint256 profit = endBalance - startBalance;
        if (profit < arb.minProfit) revert NoProfit();
        
        emit DONE(tIn, profit);
        
        if (tIn == WETH) {
            IWETH(WETH).withdraw(profit);
            uint256 builderTip = (profit * builderFeeBps) / 10000;
            uint256 ownerProfit = profit - builderTip;
            
            address cb = block.coinbase;
            assembly {
                // Transfer to builder (coinbase)
                if iszero(call(gas(), cb, builderTip, 0, 0, 0, 0)) {
                    mstore(0x00, 0x90b8ec18) // TransferFailed() selector
                    revert(0x1c, 0x04)
                }
                // Transfer to owner
                if iszero(call(gas(), _owner, ownerProfit, 0, 0, 0, 0)) {
                    mstore(0x00, 0x90b8ec18)
                    revert(0x1c, 0x04)
                }
            }
        } else {
            Helper._safeTransfer(tIn, _owner, profit);
        }
    }

    function yieldOut(Data memory arb) public view returns (Data memory ad, uint256 profit) {
        ad = _determineBorrow(arb);
        uint256 pLen = ad.pools.length;
        if (ad.amountIn == 0 || pLen == 0 || pLen != ad.tokens.length - 1 || pLen != ad.fees.length) revert AmountRequired();

        uint256 current = ad.amountIn;
        uint256 i = ad.mode == 1 ? 1 : 0;

        for (; i < pLen; ) {
            address pool = ad.pools[i];
            if (Helper._isUniswapV3(pool)) {
                (current,,,) = IQuoter(Quoter).quoteExactInputSingle(IQuoter.QuoteExactInputSingleParams({
                    tokenIn: ad.tokens[i], tokenOut: ad.tokens[i+1], amountIn: current, fee: ad.fees[i], sqrtPriceLimitX96: 0
                }));
            } else {
                (uint112 r0, uint112 r1,) = IUniswapV2Pair(pool).getReserves();
                current = (ad.tokens[i] == Helper._token0(pool)) 
                    ? Helper.getAmountOutV2(current, r0, r1) 
                    : Helper.getAmountOutV2(current, r1, r0);
            }
            unchecked { ++i; }
        }

        if (ad.mode == 0) {
            profit = current > ad.amountIn ? current - ad.amountIn : 0;
        } else {
            uint256 debt;
            address p0 = ad.pools[0];
            if (Helper._isUniswapV3(p0)) {
                (debt,,,) = IQuoter(Quoter).quoteExactOutputSingle(IQuoter.QuoteExactOutputSingleParams({
                    tokenIn: ad.tokens[0], tokenOut: ad.tokens[1], amount: ad.amountIn, fee: ad.fees[0], sqrtPriceLimitX96: 0
                }));
            } else {
                (uint112 r0, uint112 r1,) = IUniswapV2Pair(p0).getReserves();
                debt = (ad.tokens[0] == Helper._token0(p0)) 
                    ? Helper.getAmountInV2(ad.amountIn, r0, r1) 
                    : Helper.getAmountInV2(ad.amountIn, r1, r0);
            }
            profit = current > debt ? current - debt : 0;
        }
    }

    function _determineBorrow(Data memory arb) internal view returns (Data memory) {
        if (arb.amountIn > 0) return arb;
        if (arb.pools.length == 2) {
            bool borrowIs0 = (arb.tokenIn == Helper._token0(arb.pools[0]));
            (arb.pools, arb.tokens, arb.fees) = Helper.sortPools(arb.pools, arb.tokens, arb.fees, borrowIs0);
        }
        arb.amountIn = Helper.calcOptimalBorrow(Quoter, arb.pools, arb.tokens, arb.fees, arb.mode);
        return arb;
    }

    function withdraw(address token, uint256 amount) external onlyOwner {
        address _owner = owner();
        if (token == address(0)) {
            assembly {
                let bal := selfbalance()
                let amtToWithdraw := amount
                if iszero(amtToWithdraw) {
                    amtToWithdraw := bal
                }
                if iszero(call(gas(), _owner, amtToWithdraw, 0, 0, 0, 0)) {
                    mstore(0x00, 0x90b8ec18) // TransferFailed()
                    revert(0x1c, 0x04)
                }
            }
        } else {
            uint256 bal = Helper._balanceOf(token, address(this));
            uint256 amtToWithdraw = amount != 0 ? amount : bal;
            if (bal < amtToWithdraw) revert InsufficientBalance();
            Helper._safeTransfer(token, _owner, amtToWithdraw);
        }
    }
}