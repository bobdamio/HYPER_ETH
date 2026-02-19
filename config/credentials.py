import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    WALLET_ADDRESS = os.getenv("HL_WALLET_ADDRESS")
    PRIVATE_KEY = os.getenv("HL_PRIVATE_KEY")
    IS_MAINNET = os.getenv("HL_IS_MAINNET", "False").lower() == "true"

    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

    @classmethod
    def validate(cls):
        if not cls.WALLET_ADDRESS or not cls.PRIVATE_KEY:
            return False
        return True
