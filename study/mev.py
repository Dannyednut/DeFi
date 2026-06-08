from web3 import Web3, Account, eth
from web3.exceptions import TransactionNotFound
from eth_abi.abi import encode
import time
import logging
import requests
import json
from flashbots import FlashbotsWeb3, flashbot
import os
from dotenv import load_dotenv



# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Load API keys from environment variables
load_dotenv(dotenv_path='configV2_mainnet.env')

BLOCKNATIVE_API_KEY = os.getenv('BLOCKNATIVE_API_KEY')
ETHERSCAN_API_KEY = os.getenv('ETHERSCAN_API_KEY')
PRIVATE_KEY = os.getenv('PRIVATE_KEY')
ALCHEMY_API_KEY = os.getenv('ALCHEMY_API_KEY')
UNISWAP_FACTORY_ADDRESS = os.getenv('UNISWAP_FACTORY')
SUSHISWAP_FACTORY_ADDRESS = os.getenv('SUSHI_FACTORY')
# Connect to Ethereum Network
url = f'https://eth-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}'
web3 = Web3(Web3.HTTPProvider(url))

# Check if connected
if not web3.is_connected():
    print("Failed to connect to Ethereum network")
    print(url)
    exit()

# Load your private key securely (do not hardcode in production)
account = Account.from_key(PRIVATE_KEY)
address = account.address
eth.eth.Eth.codec

# DEX contract addresses (example)
UNISWAP_ROUTER_ADDRESS = os.getenv('UNISWAP')  # Uniswap V2 Router
SUSHISWAP_ROUTER_ADDRESS = os.getenv('SUSHI')  # Sushiswap Router
#FLASH_BOT_ADDRESS = os.getenv('SMART')
WETH_ADDRESS = os.getenv('WETH')

# Function to get the current gas price
def get_current_gas_price():
    return web3.eth.gas_price  # Fallback to default gas price

def setup_web3() -> FlashbotsWeb3:
    
    relay_url = 'https://relay.flashbots.net'
    w3 = flashbot(
        Web3.HTTPProvider(url),
        account,
        relay_url
    )
    return w3

# Function to fetch contract ABI from Etherscan
def get_contract_abi(contract_address):
    etherscan_api_key = ETHERSCAN_API_KEY
    url = f'https://api.etherscan.io/v2/api'
    params = {
        'chainid': 1,
        'module': 'contract',
        'action': 'getabi',
        'address': contract_address,
        'apikey': etherscan_api_key
    }    
    
    try:
        response = requests.get(url, params=params)
        if response.status_code == 200:
            data = response.json()
            if data['status'] == '1':
                return json.loads(data['result'])  # Parse ABI JSON
            else:
                logging.error(f"Error fetching ABI for {contract_address}: {data['result']}")
                return None
        else:
            logging.error("Error fetching ABI from Etherscan API.")
            return None
    except Exception as e:
        logging.error(f"Error getting contract ABI: {str(e)}")
        return None
    
    # Fetch the ABIs for the Uniswap and Sushiswap routers
UNISWAP_ROUTER_ABI = get_contract_abi(UNISWAP_ROUTER_ADDRESS)
SUSHISWAP_ROUTER_ABI = get_contract_abi(SUSHISWAP_ROUTER_ADDRESS)
UNISWAP_FACTORY_ABI = get_contract_abi(UNISWAP_FACTORY_ADDRESS)
SUSHISWAP_FACTORY_ABI = get_contract_abi(SUSHISWAP_FACTORY_ADDRESS)
#FLASH_BOT_ABI = get_contract_abi(FLASH_BOT_ADDRESS)

if UNISWAP_ROUTER_ABI is None or SUSHISWAP_ROUTER_ABI is None:
    logging.error("Failed to load contract ABIs. Exiting.")
    exit()

# Function to create and send a Flashbots bundle
def send_flashbots_bundle(transaction):
    try:
        # Create the bundle with your transaction
        w3 = setup_web3()
        block = web3.eth.block_number
        w3.flashbots.simulate(bundle, block)
        
        bundle = [{"signed_transaction": transaction.rawTransaction}]
        
        # Send the bundle to Flashbots relay
        send_result = w3.flashbots.send_private_transaction(bundle, target_block_number='latest')

        stats_v2 = w3.flashbots.get_bundle_stats_v2(
            w3.to_hex(send_result.result()), block
        )
        logging.info(f"bundleStats v2 {stats_v2}")

        send_result.wait()
        try:
            receipts = send_result.receipts()
            logging.info(f"Bundle was mined in block {receipts[0].blockNumber}")

        except TransactionNotFound:
            logging.info(f"Bundle not found in block {block + 1}")
            cancel_res = w3.flashbots.cancel_bundles(send_result.result())
            logging.info(f"Canceled {cancel_res}")
        
    except Exception as e:
        print(f"Error sending Flashbots bundle: {str(e)}")


# Function to analyze transactions
def analyze_transaction(transaction):
    try:
        # Extract transaction details
        tx_details = extract_transaction_details(transaction)
        
        if tx_details:
            logging.info(f"Analyzed transaction: {transaction.hash.hex()}")
            logging.info(f"Token In: {tx_details['token_in']}")
            logging.info(f"Token Out: {tx_details['token_out']}")
            logging.info(f"Amount In: {tx_details['amount_in']}")
            logging.info(f"DEX: {tx_details['dex']}")

            pool = check_liquidity_pool(tx_details['token_in'], tx_details['token_out'], is_uniswap=True if tx_details['dex'] == 'Uniswap' else False)
            # Check for sandwich opportunity
            is_sandwich, profit, impact = detect_sandwich_opportunity(tx_details['token_in'],tx_details['token_out'],tx_details['amount_in'],pool)
            if is_sandwich:
                logging.info(f"Sandwich opportunity detected with impact: {round(impact,10)}% and potential profit: {profit}")
            else:
                logging.info(f'Not profitable sandwich-> profit: {profit}')

            return tx_details
        else:
            return None
    except Exception as e:
        logging.error(f"Error analyzing transaction {transaction.hash.hex()}: {str(e)}")
        return None

def extract_transaction_details(transaction) -> dict:
    try:
        # Check if the transaction is to Uniswap or Sushiswap router
        if transaction['to'] in [UNISWAP_ROUTER_ADDRESS, SUSHISWAP_ROUTER_ADDRESS]:
            
            # Determine which router we're dealing with
            is_uniswap = transaction['to'] == UNISWAP_ROUTER_ADDRESS
            router_abi = UNISWAP_ROUTER_ABI if is_uniswap else SUSHISWAP_ROUTER_ABI
            router_contract = web3.eth.contract(address=transaction['to'], abi=router_abi)

            # Decode the input data
            decoded_input = router_contract.decode_function_input(transaction.input)
            function_name = decoded_input[0].fn_name
            function_args = decoded_input[1]

            # Extract details based on common swap function names
            if function_name in ['swapExactTokensForTokens', 'swapTokensForExactTokens', 'swapExactTokensForTokensSupportingFeeOnTransferTokens']:
                logging.info(f"New transaction found: {transaction['hash'].hex()}")

                token_in = function_args['path'][0]
                token_out = function_args['path'][-1]
                amount_in = function_args['amountIn'] if 'amountIn' in function_args else function_args['amountInMax']
                return {
                    'token_in': token_in,
                    'token_out': token_out,
                    'amount_in': amount_in,
                    'dex': 'Uniswap' if is_uniswap else 'Sushiswap',
                    'function': function_name
                }
            elif function_name in ['swapExactETHForTokens', 'swapETHForExactTokens', 'swapExactETHForTokensSupportingFeeOnTransferTokens']:
                logging.info(f"New transaction found: {transaction['hash'].hex()}")

                token_in = WETH_ADDRESS  # Assuming WETH_ADDRESS is defined
                token_out = function_args['path'][-1]
                amount_in = transaction['value']
                return {
                    'token_in': token_in,
                    'token_out': token_out,
                    'amount_in': amount_in,
                    'dex': 'Uniswap' if is_uniswap else 'Sushiswap',
                    'function': function_name
                }
            elif function_name in ['swapExactTokensForETH', 'swapTokensForExactETH', 'swapExactTokensForETHSupportingFeeOnTransferTokens']:
                logging.info(f"New transaction found: {transaction['hash'].hex()}")

                token_in = function_args['path'][0]
                token_out = WETH_ADDRESS
                amount_in = function_args['amountIn'] if 'amountIn' in function_args else function_args['amountInMax']
                return {
                    'token_in': token_in,
                    'token_out': token_out,
                    'amount_in': amount_in,
                    'dex': 'Uniswap' if is_uniswap else 'Sushiswap',
                    'function': function_name
                }
            else:
                if function_name in ['addLiquidity', 'removeLiquidity', 'addLiquidityEth', 'removeLiquidityEth']:
                    pass
                else:
                    logging.warning(f"Unsupported function: {function_name}")
                    return None
        else:
            return None
    except Exception as e:
        logging.error(f"Error extracting transaction details: {str(e)}")
        return None

# Function to execute trades with dynamic gas and slippage
def create_txn(pool0,pool1):
    try:
        
        # Create a contract instance
        flash_bot_contract = web3.eth.contract(address='FLASH_BOT_ADDRESS', abi='FLASH_BOT_ABI')
        gas_price = get_current_gas_price()
        print(gas_price)

        # Get the account from the private key
        account = web3.eth.account.from_key(PRIVATE_KEY)
        nonce = web3.eth.get_transaction_count(account.address)

        # Build the transaction
        transaction = flash_bot_contract.functions.trade(pool0, pool1).build_transaction({
            'chainId': 1,  # Sepolia testnet chain ID
            'gas': 21000,  # Estimate gas limit
            'gasPrice': int(gas_price),  # Set gas price
            'nonce': nonce,
        })

        # Sign the transaction
        signed_txn = web3.eth.account.sign_transaction(transaction, PRIVATE_KEY)
        
        # Send the transaction as a Flashbots bundle
        return signed_txn
    
    except Exception as e:
        logging.error(f"Error executing trade: {str(e)}")

def simulate_pool_impact(pool_reserves, swap_amount, isTokenInBase):
    float(swap_amount)
    token_a_reserve, token_b_reserve = tuple(pool_reserves)
    constant_k = token_a_reserve * token_b_reserve
    if isTokenInBase:
        new_token_a_reserve = token_a_reserve + swap_amount
        new_token_b_reserve = constant_k / new_token_a_reserve
    else:
        new_token_b_reserve = token_b_reserve + swap_amount
        new_token_a_reserve = constant_k/new_token_b_reserve

    return (new_token_a_reserve, new_token_b_reserve)

def calculate_price_impact(max_price, lower_price, original):
    diff = max_price - lower_price
    return (diff/original)*100


def calculate_arbitrage_profitability(pool0: str, pool1: str, gas_price):
    try:
        '''flash_bot_contract = web3.eth.contract(address='FLASH_BOT_ADDRESS', abi='FLASH_BOT_ABI')
        query = flash_bot_contract.functions.getProfit(pool0,pool1).call()
        profit =  float(query[0]) - float(gas_price)
        return (profit>0.0, profit)'''
        return False, 0
    except Exception as e:
        logging.error(f"Error calculating profitability: {str(e)}")
        return False, 0

def calculate_output_from_reserves(reserves, amount_in):
    # Assuming token_in is the first token in the pair
    x, y = reserves
    k = x * y  # Constant product formula
    
    # Calculate the output amount based on the constant product formula
    amount_out = y - (k / (x + amount_in))
    
    return amount_out

def get_pool_reserves(pool_address: str) -> list:
    try:
        pool_abi = get_contract_abi(pool_address)
        pool_contract = web3.eth.contract(address=pool_address, abi=pool_abi)
        token0 = pool_contract.functions.token0().call()
        reserves = pool_contract.functions.getReserves().call()
        reserves.pop()
        reserves.append(token0)
        return reserves
    except Exception as e:
        logging.error(f"Error fetching pool reserves: {str(e)}")
        return None
    
def calculate_front_run_profit(original_reserves, new_reserves, amount_in, isTokenInBase):
    try:
        float(amount_in)
        original_token_a, original_token_b = original_reserves
        new_token_a, new_token_b = new_reserves
        
        original_price = (original_token_b / original_token_a)
        new_price = (new_token_b / new_token_a)

        max_price = original_price if original_price > new_price else new_price
        lower_price = original_price if original_price < new_price else new_price

        # Calculate the profit from front-running
        if isTokenInBase:
            profit_model = ((new_token_a / new_token_b) - (original_token_a / original_token_b))*amount_in
        else:
            profit_model = ((new_token_b / new_token_a) - (original_token_b / original_token_a))*amount_in

        impact = calculate_price_impact(new_price, original_price, original_price)
        logging.info(f'Price before impact: {original_price}, Price after: {new_price}')
        return impact,profit_model
    except Exception as e:
        logging.error(f"Error calculating front-run profit: {str(e)}")
        return 0
    
def detect_sandwich_opportunity(token_in, token_out, amount_in, pool_address):
    try:
        # Get current pool states
        pool_state = get_pool_reserves(pool_address)  # Get reserves from Uniswap

        token0 = pool_state.pop()
        isTokenInBase = True if token_in == token0 else False

        # Simulate transaction impact
        post_tx_state = simulate_pool_impact(pool_state, amount_in, isTokenInBase)

        # Calculate potential sandwich profit
        impact, front_run_profit = calculate_front_run_profit(
            pool_state,
            post_tx_state,
            amount_in,  # Use the value from the pending transaction
            isTokenInBase
        )
        aggregator = aggregatorPrice(token_in, token_out) if isTokenInBase else aggregatorPrice(token_out, token_in)
        V3 = get_pool_price(token_in, token_out)
        logging.info(f'Aggregator price: {aggregator/1e18}')
        logging.info(f'UnswapV3 price: {V3}')
        return front_run_profit > 0, front_run_profit, impact
    except Exception as e:
        logging.error(f"Error detecting sandwich opportunity: {str(e)}")
        return False, 0


# Global variable to track daily loss
daily_loss = 0
max_daily_loss = web3.to_wei(1, 'ether')  # Example: 1 ETH max daily loss

def check_circuit_breaker(potential_loss):
    global daily_loss
    if daily_loss + potential_loss > max_daily_loss:
        return False  # Circuit breaker triggered
    return True  # Continue trading

def implement_circuit_breaker(potential_loss):
    if not check_circuit_breaker(potential_loss):
        logging.warning("Circuit breaker triggered. Trade not executed.")
        return False  # Indicate that trading should stop
    return True  # Indicate

def aggregatorPrice(token0, token1):
    token0 = web3.to_checksum_address(token0)
    token1 = web3.to_checksum_address(token1)
    offChainOracleAddress = "0x07D91f5fb9Bf7798734C3f606dB065549F6893bb"
    offChainOracleContract = web3.eth.contract(address=offChainOracleAddress, abi=get_contract_abi(offChainOracleAddress))
    # functions = offChainOracleContract.all_functions()
    # print(functions)
    return offChainOracleContract.functions.getRate(token0, token1, True).call()

    
def getPriceAfterImpact(pool,tx):
    web3.eth.wait_for_transaction_receipt(tx)
    # Get current pool states
    pool_contract = web3.eth.contract(address=pool, abi=get_contract_abi(pool))
    token0 = pool_contract.functions.token0().call()
    token1 = pool_contract.functions.token1().call()
    reserve = get_pool_reserves(pool)
    priceAfter = reserve[1]/reserve[0]
    newAggregator = aggregatorPrice(token0, token1)
    V3 = get_pool_price(token0, token1)
    logging.info(f'Price after impact: {priceAfter}')
    logging.info(f'New aggregator price: {newAggregator/1e18}')
    logging.info(f'UnswapV3 price: {V3}')

def getPriceByReserves(pool_address: str):
    reserve0, reserve1, _ = tuple(get_pool_reserves(pool_address))
    price = float(reserve0)/float(reserve1)
    return price 
             
# Function to monitor arbitrage opportunities
def monitor_mempool():
    last_processed_txs = set()

    while True:
        try:
            pending_block = web3.eth.get_block('pending', full_transactions=True)
            if not pending_block:
                logging.warning("No pending block received")
                time.sleep(1)
                continue

            current_txs = set()
            for tx in pending_block.transactions:
                tx_hash = tx['hash'].hex()
                current_txs.add(tx_hash)
                
                if tx_hash not in last_processed_txs:
                    try:
                        
                        tx_details = analyze_transaction(tx)
                        
                        if tx_details:
                            # Use the extracted details for further processing
                            token_in = tx_details['token_in']
                            token_out = tx_details['token_out']
                            amount_in = tx_details['amount_in']

                            # Get current pool reserves
                            sushiswap_pool = check_liquidity_pool(token_in, token_out, is_uniswap=False)
                            uniswap_pool = check_liquidity_pool(token_in, token_out)
                            getPriceAfterImpact(uniswap_pool, tx_hash)
                            if uniswap_pool is not None and sushiswap_pool is not None:
                                logging.info("Pool Found for tokens on both DEXes")

                                # Get current prices for arbitrage
                                price_uniswap = getPriceByReserves(uniswap_pool)
                                price_sushiswap = getPriceByReserves(sushiswap_pool)

                                gas_price = get_current_gas_price()

                                # Check for arbitrage opportunity
                                is_profitable, profit = calculate_arbitrage_profitability(
                                    uniswap_pool,
                                    sushiswap_pool,
                                    gas_price
                                )

                                if is_profitable:
                                    logging.info(f"Arbitrage opportunity found! Profit: {profit}")
                                    # Execute the arbitrage (implementation depends on your strategy)
                                    '''signed_txn = create_txn(uniswap_pool, sushiswap_pool)
                                    send_flashbots_bundle(signed_txn)'''
                                    print()
                                    '''
                                    if price_uniswap > price_sushiswap:
                                        # Arbitrage opportunity: Buy on SushiSwap, sell on Uniswap
                                        logging.info(f"Executing arbitrage: Buy on SushiSwap, Sell on Uniswap")
                                        execute_trade(token_in, token_out, amount_in, is_uniswap=False)  # Buy on SushiSwap
                                        execute_trade(token_in, token_out, amount_in)  # Sell on Uniswap

                                    elif price_sushiswap > price_uniswap:
                                        # Arbitrage opportunity: Buy on Uniswap, sell on SushiSwap
                                        logging.info(f"Executing arbitrage: Buy on Uniswap, Sell on SushiSwap")
                                        execute_trade(token_in, token_out, amount_in)  # Buy on Uniswap
                                        execute_trade(token_in, token_out, amount_in, is_uniswap=False)  # Sell on SushiSwap
                                    '''
                            else:
                                logging.info(f"No liquidity found for tokens on both DEXes")
                                print()
                    except Exception as tx_error:
                        logging.error(f"Error processing transaction {tx_hash}: {tx_error}")
                        continue

            last_processed_txs = current_txs
            time.sleep(1)  # Add a small delay to prevent overwhelming the node
            
        except Exception as e:
            logging.error(f"Error monitoring mempool: {e}")
            time.sleep(5)  # Longer delay on error
            continue

def verify_network():
    chain_id = web3.eth.chain_id
    if chain_id != 11155111:  # Sepolia chain ID
        logging.error(f"Wrong network! Connected to chain ID {chain_id}, but expected Sepolia (11155111)")
        return False
    print("Connected to Sepolia testnet")
    return True

    
def check_liquidity_pool(token_in, token_out, is_uniswap=True):
    try:
        factory_address = UNISWAP_FACTORY_ADDRESS if is_uniswap else SUSHISWAP_FACTORY_ADDRESS
            
        # Get factory contract
        factory = web3.eth.contract(address=factory_address, abi=get_contract_abi(UNISWAP_FACTORY_ADDRESS) if is_uniswap else get_contract_abi(SUSHISWAP_FACTORY_ADDRESS))
        
        # Check if pool exists
        pool_address = factory.functions.getPair(token_in, token_out).call()
        
        if pool_address == '0x0000000000000000000000000000000000000000':
            return None
            
        return pool_address
    except Exception as e:
        logging.error(f"Error checking liquidity pool: {str(e)}")
        return None
    
def check_balance():
    balance = web3.eth.get_balance(address)
    print(f"Account balance: {web3.from_wei(balance, 'ether')} ETH")
    if balance == 0:
        print("Please fund your account with Sepolia ETH from a faucet:")
        print("https://sepoliafaucet.com/")
        print("https://sepolia-faucet.pk910.de/")
        return False
    return float(balance)

# For any ERC20 token you want to interact with
def get_token_contract(token_address):
    try:
        token_abi = get_contract_abi(token_address)
        if token_abi is None:
            logging.error(f"Failed to get ABI for token {token_address}")
            return None
        return web3.eth.contract(address=token_address, abi=token_abi)
    except Exception as e:
        logging.error(f"Error creating token contract: {str(e)}")
        return None
    
# Function to wait for transaction confirmation
def validate_parameters(token_in, token_out, amount_in):
    try:
        # Check if addresses are valid
        if not web3.is_address(token_in) or not web3.is_address(token_out):
            logging.error("Invalid token addresses")
            return False
            
        # Check if amount is positive
        if amount_in <= 0:
            logging.error("Invalid amount")
            return False
            
        return True
    except Exception as e:
        logging.error(f"Error validating parameters: {str(e)}")
        return False

def getSingleAddress(token_in, token_out):
    UNISWAP_FAC = '0x1F98431c8aD98523631AE4a59f267346ea31F984'
    router_address = UNISWAP_FAC 
    router_abi = get_contract_abi(UNISWAP_FAC) 
    router = web3.eth.contract(address=router_address, abi=router_abi)
    fee_teirs = [500,3000,10000]
    for fee in fee_teirs:
        address = router.functions.getPool(token_in,token_out,fee).call()
        if address != "0x0000000000000000000000000000000000000000":
            return address
    return None

def get_pool_price(token_in, token_out):
    pool = getSingleAddress(token_in, token_out)
    if pool == None:
        return None
    abi= get_contract_abi(pool)
    if abi == None:
        return 0
    pool_contract = web3.eth.contract(address=pool, abi= abi)
    token0= pool_contract.functions.token0().call() 
    tokken1 = pool_contract.functions.token1().call()
    token0_contract = get_token_contract(token0)
    token1_contract = get_token_contract(tokken1)
    reserve0 = token0_contract.functions.balanceOf(pool).call()
    reserve1 = token1_contract.functions.balanceOf(pool).call()
    price = reserve1/reserve0
    return price

# Start monitoring for arbitrage opportunities
if __name__ == "__main__":
    try:
        token1 = "0x66a0f676479Cee1d7373f3DC2e2952778BfF5bd6"
        token0 = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
        pair_price = aggregatorPrice(token0,token1)
        print(f"Pair price: {pair_price}")
    except Exception as e:
        logging.error(f"Main execution error: {str(e)}")
        exit(1)