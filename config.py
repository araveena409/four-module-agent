import os

# LLM Gateway base URL — override via environment variable if needed
GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8101")

GATEWAY_CHAT_URL = f"{GATEWAY_URL}/v1/chat"
GATEWAY_STATUS_URL = f"{GATEWAY_URL}/v1/status"

