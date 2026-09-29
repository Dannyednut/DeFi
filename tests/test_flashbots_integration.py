# """
# tests/test_flashbots_integration.py
# =====================================
# Integration test for Flashbots relay with a real simple transfer transaction.

# This script:
# 1. Connects to Sepolia testnet (chain_id=11155111)
# 2. Builds a simple ETH transfer transaction
# 3. Signs it with your PRIVATE_KEY from .env
# 4. Submits via Flashbots relay

# Run with: python tests/test_flashbots_integration.py
# """
import os
import sys
# import requests
# import json

# # Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# from web3 import Web3
# from eth_account import Account
from dotenv import load_dotenv

# from executor.relay import relay_transaction, FLASHBOTS_RPCS
# from config import get_alchemy_http_url, CHAIN_ID

# # Load environment variables
load_dotenv()

# # Test configuration
TEST_CHAIN_ID = 11155111  # Sepolia testnet
RECIPIENT_ADDRESS = "0x0000000000000000000000000000000000000000"  #
TRANSFER_AMOUNT_ETH = 0.0001  # Small amount for testing


# def test_flashbots_with_real_transfer():
#     """Build and submit a real ETH transfer via Flashbots."""
#     print("=" * 60)
#     print("Flashbots Integration Test - Simple ETH Transfer")
#     print("=" * 60)

#     # Get private key from environment
#     private_key = os.getenv("PRIVATE_KEY", "").strip()
#     if not private_key:
#         print("ERROR: PRIVATE_KEY not found in .env")
#         return False

#     # Create account from private key
#     account = Account.from_key(private_key)
#     sender_address = account.address
#     print(f"Sender address: {sender_address}")
#     print(f"Chain ID: {TEST_CHAIN_ID} (Sepolia)")

#     # Connect to Sepolia RPC
#     rpc_url = get_alchemy_http_url(TEST_CHAIN_ID)
#     w3 = Web3(Web3.HTTPProvider(rpc_url))

#     if not w3.is_connected():
#         print(f"ERROR: Failed to connect to RPC: {rpc_url}")
#         return False

#     print(f"Connected to RPC: {rpc_url}")
#     print(f"Current block: {w3.eth.block_number}")

#     # Check sender balance
#     balance = w3.eth.get_balance(sender_address)
#     balance_eth = w3.from_wei(balance, "ether")
#     print(f"Sender balance: {balance_eth:.6f} ETH")

#     if balance == 0:
#         print("ERROR: Sender has no ETH for gas fees")
#         print(f"Please fund the address: {sender_address}")
#         return False

#     # Build a simple ETH transfer transaction
#     transfer_amount_wei = w3.to_wei(TRANSFER_AMOUNT_ETH, "ether")
#     nonce = w3.eth.get_transaction_count(sender_address, "pending")

#     # Get gas pricing
#     latest = w3.eth.get_block("latest")
#     base_fee = latest.get("baseFeePerGas", w3.eth.gas_price)
#     priority_fee = w3.to_wei(1, "gwei")  # 1 gwei priority fee
#     max_fee = int(base_fee * 1.5) + priority_fee

#     tx = {
#         "to": RECIPIENT_ADDRESS,
#         "value": transfer_amount_wei,
#         "gas": 21000,  # Standard transfer gas limit
#         "maxFeePerGas": max_fee,
#         "maxPriorityFeePerGas": priority_fee,
#         "nonce": nonce,
#         "chainId": TEST_CHAIN_ID,
#         "type": 2,  # EIP-1559
#     }

#     print("\n" + "-" * 60)
#     print("Transaction Details:")
#     print("-" * 60)
#     print(f"  To: {RECIPIENT_ADDRESS}")
#     print(f"  Value: {TRANSFER_AMOUNT_ETH} ETH ({transfer_amount_wei} wei)")
#     print(f"  Gas Limit: {tx['gas']}")
#     print(f"  Max Fee: {w3.from_wei(max_fee, 'gwei'):.2f} gwei")
#     print(f"  Priority Fee: {w3.from_wei(priority_fee, 'gwei'):.2f} gwei")
#     print(f"  Nonce: {nonce}")

#     # Sign the transaction
#     signed_tx = account.sign_transaction(tx)
#     raw_tx = signed_tx.rawTransaction

#     print(f"\nSigned transaction hash: {signed_tx.hash.hex()}")
#     print(f"Raw transaction length: {len(raw_tx)} bytes")

#     # Ask for confirmation before simulating
#     print("\n" + "=" * 60)
#     confirm = input("Simulate bundle via Alchemy? (yes/no): ").strip().lower()
#     if confirm not in ("yes", "y"):
#         print("Simulation cancelled by user")
#         return False

#     # Simulate via Alchemy
#     alchemy_url = get_alchemy_http_url(TEST_CHAIN_ID)
#     print(f"\nSubmitting eth_call to Alchemy...")

#     try:
#         # Using eth_call via w3 provider
#         tx_dict = {
#             "from": sender_address,
#             "to": tx["to"],
#             "value": tx["value"],  # eth_call takes integer values
#             "gas": tx["gas"],
#             "maxFeePerGas": tx["maxFeePerGas"],
#             "maxPriorityFeePerGas": tx["maxPriorityFeePerGas"]
#         }
        
#         # eth_call will raise an exception if the transaction reverts
#         try:
#             result = w3.eth.call(tx_dict)
#             success = True
#             result_hex = result.hex()
#         except Exception as sim_err:
#             success = False
#             result_hex = str(sim_err)

#         print("\n" + "=" * 60)
#         print("eth_call Simulation Result:")
#         print("=" * 60)
#         print(f"Status: {'Success' if success else 'Reverted'}")
#         print(f"Output: {result_hex}")
        
#         if not success:
#             print("\n[X] Simulation failed")
#             return False
#         else:
#             print("\n[OK] Simulation successful!")
#             return True

#     except Exception as e:
#         print(f"\n[X] Error during simulation: {e}")
#         import traceback
#         traceback.print_exc()
#         return False


# def main():
#     """Run integration tests."""
#     print("\nFlashbots Relay Integration Test\n")

#     # Show comparison info
#     print("\nAlchemy bundle simulation mode activated.")

#     # Ask if user wants to submit real transaction
#     print("\n" + "=" * 60)
#     print("This will submit a real transaction to Sepolia testnet!")
#     print("Make sure your wallet has Sepolia ETH for gas fees.")
#     print("=" * 60)

#     choice = input("\nRun real Alchemy bundle simulation? (yes/no): ").strip().lower()
#     if choice in ("yes", "y"):
#         success = test_flashbots_with_real_transfer()
#         sys.exit(0 if success else 1)
#     else:
#         print("Test skipped. Run with 'yes' to submit a real simulation.")
#         sys.exit(0)


# if __name__ == "__main__":
#     main()


from eth_account.signers.local import LocalAccount
from web3 import Web3, HTTPProvider
from flashbots import flashbot
from eth_account.account import Account
from config import get_alchemy_http_url

# Signer key for authentication (reputation only — keep it empty, no funds)
ETH_SIGNER_KEY = os.getenv("PRIVATE_KEY")
signer: LocalAccount = Account.from_key(ETH_SIGNER_KEY)

# Your provider (Alchemy, Infura, etc.)
w3 = Web3(HTTPProvider(get_alchemy_http_url(TEST_CHAIN_ID)))  # Sepolia testnet

# Initialize Flashbots (mainnet by default)
flashbot(w3, signer, "https://relay-sepolia.flashbots.net")  # For Goerli/Holesky use the relay URL as 3rd arg

latest = w3.eth.get_block("latest")
base_fee = latest.get("baseFeePerGas", w3.eth.gas_price)
priority_fee = w3.to_wei(5, "gwei")  # 5 gwei priority fee
max_fee = int(base_fee * 1.5) + priority_fee

# Build transaction
tx = {
    'from': signer.address,
    'to': RECIPIENT_ADDRESS,
    'value': w3.to_wei(TRANSFER_AMOUNT_ETH, 'ether'),
    'gas': 21000,
    'maxFeePerGas': max_fee,
    'maxPriorityFeePerGas': priority_fee,
    'nonce': w3.eth.get_transaction_count(signer.address),
    'chainId': TEST_CHAIN_ID,
}

# tx = {
#         "to": RECIPIENT_ADDRESS,
#         "value": transfer_amount_wei,
#         "gas": 21000,  # Standard transfer gas limit
#         "maxFeePerGas": max_fee,
#         "maxPriorityFeePerGas": priority_fee,
#         "nonce": nonce,
#         "chainId": TEST_CHAIN_ID,
#         "type": 2,  # EIP-1559
#     }

signed_tx = w3.eth.account.sign_transaction(tx, private_key=ETH_SIGNER_KEY)

bundle = [{"signed_transaction": signed_tx.rawTransaction}]
print(f"\nSigned transaction hash: {signed_tx.hash.hex()}")

tnx = {"signed_transaction": signed_tx.rawTransaction}

# Simulae as a bundle
simulation = w3.flashbots.simulate(bundle, latest.number + 1)
if simulation.get("error"):
    print(f"Simulation error: {simulation['error']}")
else:
    print(f"Simulation successful: {simulation}")

# Send privately
response = w3.flashbots.send_private_transaction(tnx)

response.wait()  # Wait for inclusion (optional)
receipts = response.receipts()
print(f"Mined in block {receipts[0].blockNumber}")