import logging
import time
from typing import Dict, Optional

from settings import (
    USE_ROTATING_PROXIES, PROXY_ROTATION_INTERVAL,
    PROXY_USERNAME, PROXY_PASSWORD, PROXY_HOST, PROXY_PORT
)

logger = logging.getLogger(__name__)

class ProxyRotator:
    def __init__(self):
        # Initialize your proxy list here
        self.proxies = [] # e.g., [{'server': 'http://user:pass@ip:port'}, ...]
        self.proxy_index = 0
        self.enabled = bool(self.proxies)

    def get_proxy_config(self) -> Optional[dict]:
        """Returns the next proxy config dictionary, or None."""
        if not self.enabled:
            # Crucial: Return None when proxies are not configured
            return None 
            
        proxy = self.proxies[self.proxy_index]
        self.proxy_index = (self.proxy_index + 1) % len(self.proxies)
        
        # Playwright expects a dictionary with the 'server' key
        return proxy