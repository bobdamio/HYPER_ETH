import os
import sys

# Add the project root to sys.path
sys.path.append('/home/vasya/HYPER_ETH')

# Load the env vars before importing anything else
from dotenv import load_dotenv
load_dotenv('/home/vasya/HYPER_ETH/.env')

from core.signer import HLSigner

def check_balance():
    signer = HLSigner()
    state = signer.get_user_state()
    
    # Try to extract the balance
    try:
        # User state includes marginSummary
        margin = state.get('marginSummary', {})
        account_value = margin.get('accountValue', '0')
        withdrawable = margin.get('withdrawable', '0')
        
        print(f"💰 Account Value: {account_value} USDC")
        print(f"💸 Withdrawable: {withdrawable} USDC")
        
        # Check if there are open positions
        positions = state.get('assetPositions', [])
        found_active = False
        for p in positions:
            pos = p.get('position', {})
            s_size = float(pos.get('s_size', '0'))
            if s_size != 0:
                coin = pos.get('coin', 'UNK')
                entry_px = pos.get('entry_px', '0')
                unrealized_pnl = pos.get('unrealized_pnl', '0')
                print(f"📈 ACTIVE: {coin} | Size {s_size} | Entry {entry_px} | PnL {unrealized_pnl}")
                found_active = True
        
        if not found_active:
            print("🛌 No active positions found (sizes are zero).")
            
    except Exception as e:
        print(f"❌ Error checking balance: {str(e)}")
        print(f"Raw state: {state}")

if __name__ == "__main__":
    check_balance()
