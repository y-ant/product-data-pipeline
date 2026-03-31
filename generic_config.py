import os
import random
import logging
from pathlib import Path 

# --- SENSITIVE DATA HANDLING ---
# The BASE_URL should be an Environment Variable (GitHub Secret)
# generic_config.py
BASE_URL = os.getenv("BASE_URL")
BRAND_PAGE = f"{BASE_URL}/brands/m201"
# if not BASE_URL:
#     raise ValueError("FATAL: BASE_URL is missing from environment!")
# Use a placeholder for local development #secret-base-url
# BASE_URL = os.getenv("BASE_URL", "https://secret-base-url.com").rstrip('/')

# --- PATHS AND DIRECTORIES ---
INPUT_DIR = Path("input")
OUTPUT_DIR = Path("artifacts")
DATA_DIR = Path("data")

# Ensure directories exist
for folder in [INPUT_DIR, OUTPUT_DIR, DATA_DIR]:
    folder.mkdir(parents=True, exist_ok=True)

URL_LIST_PATH = INPUT_DIR / "url_list.txt"
FAILED_URLS_FILE = OUTPUT_DIR / "failed_urls"  # Base filename for failed URLs (will be timestamped)
# SKU_FILE_NAME = INPUT_DIR / "skus.txt"
DB_FILENAME = DATA_DIR / "prices.duckdb"
LOG_LEVEL = logging.ERROR  # Set to ERROR to minimize log noise, can be overridden in config_not_needed_anymore.py

VALID_COLLECTIONS = { 'mysecret1', 'mysecret2' }  # Example of a sensitive collection list that could be stored as an environment variable or in a secure vault

# --- SCRAPER BEHAVIOR ---
CONCURRENCY_LIMIT = 5
PAGE_LOAD_TIMEOUT = 30000  # 30 seconds
MAX_PRODUCT_RETRIES = 3
BLOCK_RESOURCES = {'image', 'font', 'stylesheet', 'media'}

# Domains to block for performance
BLOCKED_DOMAINS = [
    '*google-analytics.com*', '*facebook.com*', '*doubleclick.net*', 
    '*hotjar.com*', '*clarity.ms*', '*yandex.ru*'
]

# --- USER AGENTS (Extended to 10) ---
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edge/121.0.0.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_3_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3.1 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPad; CPU OS 17_3_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3.1 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 OPR/108.0.0.0"
]

# --- UTILITY FUNCTIONS ---
def get_random_ua():
    return random.choice(USER_AGENTS)

def scrub_url(url: str) -> str:
    """Removes the sensitive BASE_URL from any string for logging/artifacts."""
    if not url:
        return ""
    return url.replace(BASE_URL, "[SECRET_BASE]")