import aiohttp
import logging
from config.credentials import Config

logger = logging.getLogger("Notifier")


class Notifier:
    """Telegram notifications for HYPER_ETH."""

    def __init__(self):
        self.token = Config.TELEGRAM_BOT_TOKEN
        self.chat_id = Config.TELEGRAM_CHAT_ID
        self.base_url = f"https://api.telegram.org/bot{self.token}"

    async def send(self, text: str):
        """Send a Markdown message to Telegram."""
        if not self.token or not self.chat_id:
            logger.warning("Telegram credentials missing — skipping.")
            return

        url = f"{self.base_url}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "Markdown",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload) as resp:
                    if resp.status != 200:
                        logger.error(f"Telegram error: {await resp.text()}")
                    return await resp.json()
        except Exception as e:
            logger.error(f"Telegram send failed: {e}")
            return None
