// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@openzeppelin/contracts/access/Ownable.sol";
import "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

import "./interfaces/IUniswapV2Pair.sol";
import "./interfaces/IUniswapV3Pool.sol";
import "./interfaces/IQuoter.sol";
import "./interfaces/IWETH.sol";
import "./interfaces/IAavePool.sol";
import "./interfaces/IBalancerVault.sol";
import {Helper} from "./libraries/Helper_v2.sol";

struct ArbData {
    uint256 amountIn;
    uint256 minProfit;
    address[] tokens;
    address[] pools;
    uint24[] fees;
    address tokenIn;
    address borrowPool;
    uint8 mode; // 0 = borrow token in, 1 = borrow other token
}


// Custom errors — 4 bytes vs N bytes for require strings
error Auth();
error Block();
error BadPath();
error NoProfit();
error InsufficientBalance();
error ZeroAddress();
error TransferFailed();
error AmountRequired();
error ModePathMismatch();

contract ArbExec is Ownable, ReentrancyGuard {

    address public immutable WETH;
    address public immutable Quoter;
    address public immutable AAVE_POOL;
    address public immutable BALANCER_VAULT;

    // Set before initiating any flash loan/swap, validated inside every callback,
    // and cleared immediately after validation to prevent re-use.
    address private _expectedCallback;

    event FLA(address indexed t, uint256 a);
    event FSV2(address indexed p, uint256 a);
    event FSV3(address indexed p, uint256 a);
    event DONE(address indexed profitToken, uint256 amt);

    /// @notice Deploys the contract and sets immutable protocol addresses.
    /// @param _w  WETH token address used for unwrapping profits.
    /// @param _q  View-only Quoter address for simulating V3 swap outputs.
    /// @param _a  Aave V3 lending pool address, or address(0) to disable Aave flash loans.
    /// @param _b  Balancer Vault address, or address(0) to disable Balancer flash loans.
    constructor(address _w, address _q, address _a, address _b) Ownable(msg.sender) {
        if (_w == address(0) || _q == address(0)) revert ZeroAddress();
        WETH = _w;
        Quoter = _q;
        AAVE_POOL = _a;
        BALANCER_VAULT = _b;
    }

    /// @dev Accepts ETH from WETH.withdraw() during profit unwrapping.
    receive() external payable {}

    /// @dev Redirects non-standard V2 fork swap callbacks (e.g. pancakeV2Call, sushiCall)
    ///      to uniswapV2Call. Any 4-byte selector not matching a defined function will land
    ///      here. Decodes the standard V2 callback payload and forwards it.
    ///      Reverts if callback mismatch or if the payload is too short.
    fallback(bytes calldata _input) external returns (bytes memory) {
        if (_input.length < 4) revert BadPath();
        if (_expectedCallback != msg.sender) revert Auth();
        (address sender, uint256 amount0, uint256 amount1, bytes memory data) = abi.decode(_input[4:], (address, uint256, uint256, bytes));
        uniswapV2Call(sender, amount0, amount1, data);
        return "";
    }

    /// @notice Entry point for executing an arbitrage opportunity.
    /// @dev Validates the path, determines the borrow amount if not supplied, then
    ///      initiates a flash loan or flash swap from the preferred source.
    ///      Use the `ad` return value from getProfit() as the `arb` argument here
    ///      to ensure the borrow amount and sorted pools are consistent with the simulation.
    /// @param arb           Arbitrage parameters including token path, pools, fees, and constraints.
    /// @param forceAave     If true, forces borrowing via Aave flash loan (requires AAVE_POOL set).
    /// @param forceBalancer If true, forces borrowing via Balancer flash loan (requires BALANCER_VAULT set).
    ///                      forceAave takes precedence over forceBalancer if both are true.
    ///                      If neither is true, borrows via a pool flash swap.
    function execute(ArbData calldata arb, bool forceAave, bool forceBalancer, uint256 validUntilBlock) external nonReentrant onlyOwner {
        if (block.number > validUntilBlock) revert Block();
        if (arb.tokens.length < 2) revert BadPath();
        if (arb.pools.length != arb.tokens.length - 1) revert BadPath();
        Helper._validatePoolTokens(arb.tokens, arb.pools);

        // Copy calldata to memory so _determineBorrowAmount can mutate (sort pools, set amountIn)
        ArbData memory arbMem = arb; uint256 profit;
        (arbMem, profit) = getProfit(arbMem);
        uint256 borrowAmt = arbMem.amountIn;

        if (profit < arbMem.minProfit || profit == 0) revert NoProfit();
        if (borrowAmt == 0) revert AmountRequired();
        if (arbMem.tokens[arbMem.tokens.length - 1] != arbMem.tokenIn) revert BadPath();
        uint256 startBalance = _balanceOf(arbMem.tokenIn, address(this));

        bytes memory payload = abi.encode(arbMem, borrowAmt, startBalance);

        if (forceAave && AAVE_POOL != address(0)) _initiateAaveFlashloan(arbMem, borrowAmt, payload);
        else if (forceBalancer && BALANCER_VAULT != address(0)) _initiateBalancerFlashloan(arbMem, borrowAmt, payload);
        else _initiatePoolFlashswap(arbMem, borrowAmt, payload);
    }

    /// @dev Determines the optimal borrow amount for the arb if not supplied by the caller.
    ///      Sorts pools so the borrow pool comes first, then applies the appropriate
    ///      optimal-amount formula based on whether pools are V2, V3, or mixed.
    ///      For paths with 3+ pools, the caller must supply amountIn manually.
    /// @param arb Arbitrage parameters, possibly with amountIn = 0.
    /// @return arb Updated arb with amountIn set and pools sorted.
    function _determineBorrowAmount(ArbData memory arb)
        internal
        view
        returns (ArbData memory)
    {
        if (arb.pools.length < 2) revert BadPath();

        for (uint256 i = 0; i < arb.pools.length; ) {
            (address pt0, address pt1,) = Helper.getPoolTokens(arb.pools[i]);
            require(pt0 < pt1, "nonstandard pair");
            unchecked { ++i; }
        }

        IUniswapV2Pair p0 = IUniswapV2Pair(arb.pools[0]);
        bool borrowIs0 = (arb.tokenIn == p0.token0());
        (arb.pools, arb.tokens, arb.fees) = Helper.sortPools(arb.pools, arb.tokens, arb.fees, borrowIs0);

        if (arb.pools.length == 2) {
            IUniswapV2Pair p1 = IUniswapV2Pair(arb.pools[1]);
            require(
                p0.token0() == p1.token0() && p0.token1() == p1.token1(),
                "pools must share a common token"
            );
        }

        if (arb.amountIn > 0) return arb;

        if (arb.pools.length == 2) {
            arb.amountIn = Helper.calcOptimalBorrow(arb.pools, arb.tokens, arb.fees, arb.mode);
            return arb;
        }

        if (arb.amountIn == 0) revert AmountRequired();
        return arb;
    }

    /// @dev Initiates an Aave V3 flash loan for the borrow token.
    ///      Sets _expectedCallback to AAVE_POOL before the call so the
    ///      executeOperation callback can authenticate the caller.
    /// @param arb     Arbitrage parameters.
    /// @param amt     Amount to borrow.
    /// @param payload ABI-encoded (ArbData, borrowAmt, startBalance) passed through to the callback.
    function _initiateAaveFlashloan(ArbData memory arb, uint256 amt, bytes memory payload) internal {
        _expectedCallback = AAVE_POOL;

        address[] memory a = new address[](1); a[0] = arb.tokenIn;
        uint256[] memory am = new uint256[](1); am[0] = amt;
        uint256[] memory m = new uint256[](1); m[0] = 0;
        try IAavePool(AAVE_POOL).flashLoan(address(this), a, am, m, address(this), payload, 0) {
        } catch {
            _expectedCallback = address(0);
            revert("Aave FL failed");
        }

        emit FLA(arb.tokenIn, amt);
    }

    /// @dev Initiates a Balancer flash loan for the borrow token.
    ///      Sets _expectedCallback to BALANCER_VAULT before the call so the
    ///      receiveFlashLoan callback can authenticate the caller.
    /// @param arb     Arbitrage parameters.
    /// @param amt     Amount to borrow.
    /// @param payload ABI-encoded (ArbData, borrowAmt, startBalance) passed through to the callback.
    function _initiateBalancerFlashloan(ArbData memory arb, uint256 amt, bytes memory payload) internal {
        _expectedCallback = BALANCER_VAULT;

        address[] memory a = new address[](1); a[0] = arb.tokenIn;
        uint256[] memory am = new uint256[](1); am[0] = amt;
        try IBalancerVault(BALANCER_VAULT).flashLoan(address(this), a, am, payload) {
        } catch {
            _expectedCallback = address(0);
            revert("Balancer FL failed");
        }

        emit FLA(arb.tokenIn, amt);
    }

    /// @dev Initiates a flash swap directly from a Uniswap V2 or V3 pool.
    ///      For V3 mode 0: uses pool.flash() on the dedicated borrowPool.
    ///      For V3 mode 1: uses pool.swap() with a negative amountSpecified to receive
    ///                     tokens upfront and repay with the other token.
    ///      For V2 mode 0: uses pair.swap() on the dedicated borrowPool.
    ///      For V2 mode 1: uses pair.swap() on pools[0], repaying with the output token.
    /// @param arb       Arbitrage parameters.
    /// @param borrowAmt Amount to borrow.
    /// @param payload   ABI-encoded callback data passed through to the pool callback.
    function _initiatePoolFlashswap(ArbData memory arb, uint256 borrowAmt, bytes memory payload) internal {
        if (Helper._isUniswapV3(arb.pools[0])) {
            if (arb.mode == 0) {
                if (arb.borrowPool == address(0) || !Helper._isContract(arb.borrowPool)) revert BadPath();
                _expectedCallback = arb.borrowPool;
                bool isT0bp = arb.tokenIn == _token0(arb.borrowPool);
                try IUniswapV3Pool(arb.borrowPool).flash(
                    address(this),
                    isT0bp ? borrowAmt : 0,
                    isT0bp ? 0 : borrowAmt,
                    payload
                ){}catch{
                    _expectedCallback = address(0);
                    revert("V3 flash failed");
                }
                emit FSV3(arb.borrowPool, borrowAmt);
            } else {
                _expectedCallback = arb.pools[0];
                bool z = (arb.tokenIn == _token0(arb.pools[0]));
                uint160 sqrtLimit = z ? 4295128740 : 1461446703485210103287273052203988822378723970341;
                // Prepend mode discriminator (2) to payload for callback dispatch
                bytes memory data = abi.encodePacked(uint8(2), payload);
                IUniswapV3Pool(arb.pools[0]).swap(address(this), z, -int256(borrowAmt), sqrtLimit, data);
                emit FSV3(arb.pools[0], borrowAmt);
            }
            return;
        }

        if (arb.mode == 0) {
            if (arb.borrowPool == address(0) || !Helper._isContract(arb.borrowPool)) revert BadPath();
        }
        IUniswapV2Pair pair = arb.mode == 0 ? IUniswapV2Pair(arb.borrowPool) : IUniswapV2Pair(arb.pools[0]);
        _expectedCallback = address(pair);

        address token0 = _token0(address(pair));
        bool isT0 = arb.tokenIn == token0;

        uint256 a0out; uint256 a1out;
        if (arb.mode == 0) (a0out, a1out) = isT0 ? (borrowAmt, uint256(0)) : (uint256(0), borrowAmt);
        else               (a0out, a1out) = isT0 ? (uint256(0), borrowAmt) : (borrowAmt, uint256(0));

        // Pass payload directly — uniswapV2Call recomputes debtAmount on-the-fly
        try pair.swap(a0out, a1out, address(this), payload) {
            // _expectedCallback cleared inside uniswapV2Call callback
        } catch {
            _expectedCallback = address(0);
            revert("V2 swap flash failed");
        }
        emit FSV2(address(pair), borrowAmt);
    }

    /// @notice Aave flash loan callback.
    /// @dev Called by the Aave pool after funds are transferred to this contract.
    ///      Executes the arb trade, then repays the loan plus Aave's premium.
    ///      Reverts if caller is not the expected Aave pool or initiator is not this contract.
    /// @param assets   Token addresses borrowed (single element array).
    /// @param amounts  Amounts borrowed (single element array).
    /// @param premiums Aave fees owed on top of the borrowed amounts.
    /// @param initiator Must equal address(this) — guards against third-party flash loan injection.
    /// @param params   ABI-encoded (ArbData, borrowAmt, startBalance).
    /// @return True on success, as required by the Aave interface.
    function executeOperation(address[] calldata assets, uint256[] calldata amounts, uint256[] calldata premiums, address initiator, bytes calldata params) external returns (bool) {
        if (msg.sender != _expectedCallback) revert Auth();
        _expectedCallback = address(0);
        if (initiator != address(this)) revert Auth();
        (ArbData memory arb, uint256 borrowed, uint256 startBalance) = abi.decode(params, (ArbData, uint256, uint256));
        _executeTrade(arb, borrowed, 0, 0);
        _safeTransfer(assets[0], msg.sender, amounts[0] + premiums[0]);
        _processProfit(arb, startBalance);
        return true;
    }

    function receiveFlashLoan(address[] memory tokens, uint256[] memory amounts, uint256[] memory feeAmounts, bytes memory userData) external {
        if (msg.sender != _expectedCallback) revert Auth();
        _expectedCallback = address(0);
        (ArbData memory arb, uint256 borrowed, uint256 startBalance) = abi.decode(userData, (ArbData, uint256, uint256));
        _executeTrade(arb, borrowed, 0, 0);
        _safeTransfer(tokens[0], msg.sender, amounts[0] + feeAmounts[0]);
        _processProfit(arb, startBalance);
    }

    function uniswapV2Call(address sender, uint amount0, uint amount1, bytes memory data) public {
        if (msg.sender != _expectedCallback) revert Auth();
        _expectedCallback = address(0);
        if (sender != address(this)) revert Auth();

        (ArbData memory arb, uint256 borrowed, uint256 startBalance) = abi.decode(data, (ArbData, uint256, uint256));
        // amount0/amount1 tells us what was actually received — use it directly
        borrowed = amount0 > 0 ? amount0 : amount1;

        uint256 startIdx = arb.mode == 0 ? 0 : 1;
        _executeTrade(arb, borrowed, startIdx, startIdx);

        if (arb.mode == 0) {
            // Repay same token + 0.3% fee
            uint256 repay = borrowed + (borrowed * 3) / 1000;
            if (_balanceOf(arb.tokenIn, address(this)) < repay) revert InsufficientBalance();
            _safeTransfer(arb.tokenIn, msg.sender, repay);
        } else {
            // Mode 1: recompute debtAmount from current reserves
            address token0 = _token0(msg.sender);
            (uint112 r0, uint112 r1,) = IUniswapV2Pair(msg.sender).getReserves();
            bool isT0 = arb.tokenIn == token0;
            // debtToken = tokenIn, debtAmount = A needed to get `borrowed` B from pool
            uint256 debtAmount = isT0
                ? Helper.getAmountInV2(borrowed, r0, r1)
                : Helper.getAmountInV2(borrowed, r1, r0);
            if (_balanceOf(arb.tokenIn, address(this)) < debtAmount) revert InsufficientBalance();
            _safeTransfer(arb.tokenIn, msg.sender, debtAmount);
        }
        _processProfit(arb, startBalance);
    }

    function uniswapV3SwapCallback(int256 a0, int256 a1, bytes calldata data) external {
        if (msg.sender != _expectedCallback) revert Auth();

        uint8 mode = abi.decode(data, (uint8));
        if (mode == 1) {
            (, address tokenIn, ) = abi.decode(data, (uint8, address, address));
            bool is0 = a0 > 0;
            address req  = is0 ? _token0(msg.sender) : _token1(msg.sender);
            uint256 need = is0 ? uint256(a0) : uint256(a1);
            if (req != tokenIn) revert ModePathMismatch();
            if (_balanceOf(req, address(this)) < need) revert InsufficientBalance();
            _safeTransfer(req, msg.sender, need);
            return;
        }

        _expectedCallback = address(0);
        // data = uint8(2) ++ abi.encode(ArbData, borrowAmt, startBalance)
        // Skip the first byte (mode discriminator) and decode the rest
        (ArbData memory arb,, uint256 startBalance) = abi.decode(data[1:], (ArbData, uint256, uint256));

        bool borrowedIs0 = a0 < 0;
        uint256 borrowed = borrowedIs0 ? uint256(-a0) : uint256(-a1);        address t0 = _token0(msg.sender);
        address t1 = _token1(msg.sender);
        address debtToken = borrowedIs0 ? t1 : t0;

        if (debtToken != arb.tokens[arb.tokens.length - 1]) revert ModePathMismatch();

        _executeTrade(arb, borrowed, 1, 1);

        if (a0 > 0) {
            uint256 owe0 = uint256(a0);
            if (_balanceOf(t0, address(this)) < owe0) revert InsufficientBalance();
            _safeTransfer(t0, msg.sender, owe0);
        }
        if (a1 > 0) {
            uint256 owe1 = uint256(a1);
            if (_balanceOf(t1, address(this)) < owe1) revert InsufficientBalance();
            _safeTransfer(t1, msg.sender, owe1);
        }
        _processProfit(arb, startBalance);
    }

    function uniswapV3FlashCallback(uint256 f0, uint256 f1, bytes calldata data) external {
        if (msg.sender != _expectedCallback) revert Auth();
        _expectedCallback = address(0);
        (ArbData memory arb, uint256 borrowed, uint256 startBalance) = abi.decode(data, (ArbData, uint256, uint256));
        _executeTrade(arb, borrowed, 0, 0);
        address t0 = _token0(arb.pools[0]);
        address t1 = _token1(arb.pools[0]);
        if (f0 > 0) {
            uint256 owe0 = borrowed + f0;
            if (_balanceOf(t0, address(this)) < owe0) revert InsufficientBalance();
            _safeTransfer(t0, msg.sender, owe0);
        }
        if (f1 > 0) {
            uint256 owe1 = borrowed + f1;
            if (_balanceOf(t1, address(this)) < owe1) revert InsufficientBalance();
            _safeTransfer(t1, msg.sender, owe1);
        }
        _processProfit(arb, startBalance);
    }

    function _executeTrade(ArbData memory arb, uint256 borrowed, uint256 startPoolIdx, uint256 startTokenIdx) internal {
        address curToken = arb.tokens[startTokenIdx];
        uint256 amt = borrowed;
        uint256 hopIdx = startTokenIdx;
        for (uint i = startPoolIdx; i < arb.pools.length; ) {
            address nextToken = arb.tokens[hopIdx + 1];
            amt = _swap(arb.pools[i], curToken, nextToken, amt);
            curToken = nextToken;
            unchecked { ++i; ++hopIdx; }
        }
    }

    function _swap(address pool, address tokenIn, address tokenOut, uint256 amountIn) internal returns (uint256 out) {
        if (Helper._isUniswapV3(pool)) {
            bool z = (tokenIn == _token0(pool));
            uint160 sqrtLimit = z ? 4295128740 : 1461446703485210103287273052203988822378723970341;
            _expectedCallback = pool;
            (int256 a0, int256 a1) = IUniswapV3Pool(pool).swap(address(this), z, int256(amountIn), sqrtLimit, abi.encode(uint8(1), tokenIn, tokenOut));
            _expectedCallback = address(0);
            return z ? uint256(-a1) : uint256(-a0);
        }

        IUniswapV2Pair p2 = IUniswapV2Pair(pool);
        (uint112 r0, uint112 r1,) = p2.getReserves();
        address t0 = _token0(pool);
        bool isT0 = tokenIn == t0;
        out = isT0 ? Helper.getAmountOutV2(amountIn, r0, r1) : Helper.getAmountOutV2(amountIn, r1, r0);
        _safeTransfer(tokenIn, pool, amountIn);
        bool outIsT0 = tokenOut == t0;
        p2.swap(outIsT0 ? out : 0, outIsT0 ? 0 : out, address(this), "");
    }

    function _processProfit(ArbData memory arb, uint256 startBalance) internal {
        if (arb.tokenIn == address(0)) return;
        uint256 endBalance = _balanceOf(arb.tokenIn, address(this));
        if (endBalance <= startBalance) revert NoProfit();
        uint256 profit = endBalance - startBalance;
        if (profit < arb.minProfit) revert NoProfit();
        emit DONE(arb.tokenIn, profit);
        if (arb.tokenIn == WETH) {
            IWETH(WETH).withdraw(profit);
            address _owner = owner();
            assembly {
                let ok := call(gas(), _owner, profit, 0, 0, 0, 0)
                if iszero(ok) {
                    mstore(0x00, 0x90b8ec18) // TransferFailed()
                    revert(0x1c, 0x04)
                }
            }
        } else {
            _safeTransfer(arb.tokenIn, owner(), profit);
        }
    }

    // ── Assembly helpers ──────────────────────────────────────────────────────

    /// @dev token0() via raw staticcall — saves ~200 gas vs ABI dispatch per call
    function _token0(address pool) internal view returns (address t) {
        assembly {
            let ptr := mload(0x40)
            mstore(ptr, 0x0dfe168100000000000000000000000000000000000000000000000000000000)
            if iszero(staticcall(gas(), pool, ptr, 0x04, ptr, 0x20)) { revert(0, 0) }
            t := mload(ptr)
        }
    }

    /// @dev token1() via raw staticcall
    function _token1(address pool) internal view returns (address t) {
        assembly {
            let ptr := mload(0x40)
            mstore(ptr, 0xd21220a700000000000000000000000000000000000000000000000000000000)
            if iszero(staticcall(gas(), pool, ptr, 0x04, ptr, 0x20)) { revert(0, 0) }
            t := mload(ptr)
        }
    }

    /// @dev ERC20 balanceOf via assembly — saves ~100 gas vs IERC20 dispatch
    function _balanceOf(address token, address account) internal view returns (uint256 bal) {
        assembly {
            let ptr := mload(0x40)
            mstore(ptr,        0x70a0823100000000000000000000000000000000000000000000000000000000)
            mstore(add(ptr, 4), account)
            if iszero(staticcall(gas(), token, ptr, 0x24, ptr, 0x20)) { revert(0, 0) }
            bal := mload(ptr)
        }
    }

    /// @dev ERC20 transfer via assembly — bypasses SafeERC20 return-value overhead.
    ///      Used only for trusted tokens (WETH, USDC, etc.) on known pools.
    function _safeTransfer(address token, address to, uint256 amount) internal {
        assembly {
            let ptr := mload(0x40)
            mstore(ptr,          0xa9059cbb00000000000000000000000000000000000000000000000000000000)
            mstore(add(ptr, 4),  to)
            mstore(add(ptr, 36), amount)
            let ok := call(gas(), token, 0, ptr, 0x44, ptr, 0x20)
            // Accept both: call failed OR returned false
            if iszero(and(ok, or(iszero(returndatasize()), mload(ptr)))) {
                mstore(0x00, 0x356680b7) // InsufficientBalance() — transfer failed
                revert(0x1c, 0x04)
            }
        }
    }



    function getProfit(ArbData memory arb)
        public
        view
        returns (ArbData memory ad, uint256 profit)
    {
        ad = _determineBorrowAmount(arb);
        require(ad.amountIn > 0, "no arb");
        require(ad.pools.length == ad.tokens.length - 1, "invalid pools/tokens length");
        require(ad.pools.length == ad.fees.length, "pools/fees mismatch");

        uint256 current = ad.amountIn;
        uint256 startIdx = ad.mode == 1 ? 1 : 0;

        for (uint256 i = startIdx; i < ad.pools.length; ) {
            address pool    = ad.pools[i];
            address tokenIn = ad.tokens[i];
            address tOut    = ad.tokens[i + 1];
            if (Helper._isUniswapV3(pool)) {
                uint24 fee = ad.fees.length > i ? ad.fees[i] : 3000;
                (current,,,) = IQuoter(Quoter).quoteExactInputSingle(
                    IQuoter.QuoteExactInputSingleParams({
                        tokenIn: tokenIn, tokenOut: tOut, amountIn: current,
                        fee: fee, sqrtPriceLimitX96: 0
                    })
                );
            } else {
                (uint112 r0, uint112 r1,) = IUniswapV2Pair(pool).getReserves();
                current = tokenIn == _token0(pool)
                    ? Helper.getAmountOutV2(current, r0, r1)
                    : Helper.getAmountOutV2(current, r1, r0);
            }
            unchecked { ++i; }
        }

        if (ad.mode == 0) {
            profit = current > ad.amountIn ? current - ad.amountIn : 0;
        } else {
            uint256 debtAmount;
            address pool0 = ad.pools[0];
            if (Helper._isUniswapV3(pool0)) {
                (debtAmount,,,) = IQuoter(Quoter).quoteExactOutputSingle(
                    IQuoter.QuoteExactOutputSingleParams({
                        tokenIn: ad.tokens[0], tokenOut: ad.tokens[1],
                        amount: ad.amountIn, fee: ad.fees[0], sqrtPriceLimitX96: 0
                    })
                );
            } else {
                (uint112 r0, uint112 r1,) = IUniswapV2Pair(pool0).getReserves();
                debtAmount = ad.tokens[0] == _token0(pool0)
                    ? Helper.getAmountInV2(ad.amountIn, r0, r1)
                    : Helper.getAmountInV2(ad.amountIn, r1, r0);
            }
            profit = current > debtAmount ? current - debtAmount : 0;
        }
        return (ad, profit);
    }

    function withdraw(address token, uint256 amount) external onlyOwner {
        if (token == address(0)) {
            address _owner = owner();
            assembly {
                let ok := call(gas(), _owner, selfbalance(), 0, 0, 0, 0)
                if iszero(ok) {
                    mstore(0x00, 0x90b8ec18) // TransferFailed()
                    revert(0x1c, 0x04)
                }
            }
        } else {
            uint256 bal = _balanceOf(token, address(this));
            if (amount != 0) {
                require(bal >= amount, "insuf bal");
                _safeTransfer(token, owner(), amount);
            } else {
                _safeTransfer(token, owner(), bal);
            }
        }
    }
}