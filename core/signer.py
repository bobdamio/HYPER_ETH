import eth_account
import os
from eth_account.signers.local import LocalAccount
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from config.credentials import Config


class HLSigner:
    """Signs transactions for HyperLiquid L1."""

    def __init__(self):
        if not Config.validate():
            raise ValueError("Missing HL_WALLET_ADDRESS or HL_PRIVATE_KEY in .env")

        self.secret_key = os.getenv("HL_AGENT_PRIVATE_KEY") or Config.PRIVATE_KEY
        self.account: LocalAccount = eth_account.Account.from_key(self.secret_key)
        self.address = Config.WALLET_ADDRESS

        self.base_url = (
            constants.MAINNET_API_URL if Config.IS_MAINNET
            else constants.TESTNET_API_URL
        )

        self.exchange = Exchange(
            self.account, self.base_url, account_address=self.address
        )

    def get_info(self) -> Info:
        return Info(self.base_url, skip_ws=True)

    def get_user_state(self) -> dict:
        """Account state with Unified Account support (spot USDC)."""
        import requests
        info = self.get_info()
        state = info.user_state(self.address)

        try:
            r = requests.post(f"{self.base_url}/info", json={
                "type": "spotClearinghouseState",
                "user": self.address,
            })
            if r.status_code == 200:
                spot = r.json()
                for b in spot.get("balances", []):
                    if b["coin"] == "USDC":
                        total_usdc = float(b["total"])
                        acc_val = float(state["marginSummary"]["accountValue"])
                        state["marginSummary"]["accountValue"] = str(
                            max(total_usdc, acc_val)
                        )
                        break
        except Exception:
            pass

        return state
