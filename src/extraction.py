import re
import csv
import json
import logging
import asyncio
from datetime import datetime
from typing import List, Set, Optional, Dict

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

# Import types and configuration using Pathlib
from generic_config import (
    BASE_URL, BRAND_PAGE, MAX_PAGE_RETRIES, SCROLL_ATTEMPTS, SCROLL_PAUSE, PAGE_LOAD_TIMEOUT,
    EXCLUDE_FRAGMENTS, JSON_LD_SELECTOR, PRODUCT_LINK_SELECTOR, MAX_PRODUCT_RETRIES,
    BLOCK_RESOURCES
)
from src.models import ScrapedItem, FinalProductRecord

logger = logging.getLogger(__name__)

# --- Core Utility Functions (T-Step) ---

def _sanitize_url(url: str) -> str:
    """Remove fragment identifiers (#reviews-col, etc.)"""
    if '#' in url:
        return url.split('#', 1)[0]
    return url

def get_product_id_from_url(url: str) -> Optional[str]:
    """Extract numeric product ID from URL (e.g., '2427637' from '/p2427637')"""
    match = re.search(r'/p(\d+)/?(?:#.*)?$', url)
    return match.group(1) if match else None

def is_valid_product_url(href: str) -> bool:
    """Check if URL is a valid product URL (not policy/external/protocol link)"""
    if not href:
        return False
    href_lower = href.lower()
    
    if '://' in href and not href.startswith('http'):
        return False
    if any(fragment.lower() in href_lower for fragment in EXCLUDE_FRAGMENTS):
        return False
    if '/p' not in href:
        return False

    return True

def is_valid_sku(sku_raw: str) -> bool:
    """Check if SKU is valid based on length and numeric content after cleaning."""
    if not sku_raw or sku_raw.startswith("N/A"):
        return False
    
    normalized = sku_raw.replace('-', '').replace(' ', '').strip()
    
    if not normalized.isdigit():
        return False

    # Check for reasonable length (6 to 15 digits)
    return 6 <= len(normalized) <= 15

def normalize_price(price_raw: str) -> float:
    """
    Cleans raw price string and converts it to a float.
    Returns -1.0 if conversion fails.
    """
    try:
        # Remove currency symbols (₴), commas, spaces
        cleaned = re.sub(r'[^\d.]', '', price_raw)
        return float(cleaned)
    except:
        return -1.0

def normalize_sku(sku_raw: str) -> str:
    """Removes non-alphanumeric characters from the SKU."""
    return re.sub(r'[^a-zA-Z0-9]', '', sku_raw).strip()

def filter_scraped_data(data: List[ScrapedItem], skus_to_check: List[str] = None) -> List[FinalProductRecord]:
    """
    T-Step: Filter, normalize, and validate scraped data.
    1. Validates and normalizes SKU.
    2. Filters against a whitelist if provided.
    3. Removes duplicates based on normalized SKU.
    """
    filtered_records: List[FinalProductRecord] = []
    seen_skus: Set[str] = set()

    # Pre-normalize whitelist SKUs once
    sku_whitelist_normalized: Set[str] = set(normalize_sku(s) for s in skus_to_check) if skus_to_check else set()
    is_filtering_by_sku = bool(skus_to_check)

    for item in data:
        if not is_valid_sku(item.sku_raw):
            logger.debug(f"Invalid SKU rejected: {item.sku_raw} from {item.url}")
            continue

        normalized_sku = normalize_sku(item.sku_raw)

        if is_filtering_by_sku and normalized_sku not in sku_whitelist_normalized:
            logger.debug(f"SKU not in whitelist: {item.sku_raw}")
            continue

        if normalized_sku in seen_skus:
            logger.debug(f"Duplicate SKU skipped: {normalized_sku}")
            continue

        seen_skus.add(normalized_sku)
        
        # Final normalized record creation
        record = FinalProductRecord(
            normalized_sku=normalized_sku,
            price=normalize_price(item.price_raw),
            availability_code=item.availability_raw,
            url=item.url,
            timestamp=item.timestamp,
            detection_status=item.detection_status
        )
        filtered_records.append(record)

    return filtered_records

# --- Playwright Helpers (E-Step) ---

async def _block_resources(page: Page) -> None:
    """Blocks non-essential resources for faster loading."""
    await page.route("**/*", lambda route: asyncio.create_task(
        route.abort()) if route.request.resource_type in BLOCK_RESOURCES else asyncio.create_task(route.continue_())
    )

async def parse_product_jsonld(page: Page) -> Optional[Dict[str, str]]:
    """Parse JSON-LD structured data for product info (sku, price, availability)."""
    scripts = await page.query_selector_all(JSON_LD_SELECTOR)

    for s in scripts:
        text = (await s.inner_text()).strip()
        if not text:
            continue

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue

        candidates = data if isinstance(data, list) else [data]

        for item in candidates:
            if isinstance(item, dict) and item.get("@type") == "Product":
                sku = str(item.get("sku", "")).strip()
                if not sku or sku.lower() == "none" or len(sku) < 3:
                    continue

                offers = item.get("offers", {}) or {}
                if isinstance(offers, list) and offers:
                    offers = offers[0]
                elif not isinstance(offers, dict):
                    continue

                price = str(offers.get("price", "-1")).strip()
                availability = offers.get("availability", "-1")

                if isinstance(availability, str) and "/" in availability:
                    availability = availability.split("/")[-1]

                return {"sku_raw": sku, "price_raw": price, "availability_raw": availability}
    return None

# --- Main Scraping Logic (E-Step) ---

async def collect_product_urls(page: Page, max_pages: int = -1, stop_flag: asyncio.Event = None) -> List[str]:
    """
    Collect unique, valid product URLs from brand pagination pages.
    """
    urls: List[str] = []
    seen_product_ids: Set[str] = set()
    pnum: int = 1
    max_pages_limit: float = max_pages if max_pages != -1 else float('inf')

    while pnum <= max_pages_limit:
        if stop_flag and stop_flag.is_set():
            break

        page_url: str = f"{BRAND_PAGE}?page={pnum}"

        for attempt in range(1, MAX_PAGE_RETRIES + 1):
            try:
                logger.info(f"Loading index page {pnum} (attempt {attempt})")
                await page.goto(page_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
                await asyncio.sleep(0.3)

                prev_count: int = -1
                current_urls_on_page_load: int = len(urls)

                for _ in range(SCROLL_ATTEMPTS):
                    anchors = await page.query_selector_all(PRODUCT_LINK_SELECTOR)

                    for a in anchors:
                        href = await a.get_attribute("href") or ""
                        if not is_valid_product_url(href):
                            continue

                        href = BASE_URL + href if not href.startswith("http") else href
                        href_sanitized = _sanitize_url(href)
                        product_id = get_product_id_from_url(href_sanitized)

                        if product_id and product_id not in seen_product_ids:
                            seen_product_ids.add(product_id)
                            urls.append(href_sanitized)

                    if len(seen_product_ids) == prev_count:
                        break # No new URLs found after scrolling
                    prev_count = len(seen_product_ids)

                    # Scroll down to load lazy-loaded content
                    await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
                    await asyncio.sleep(SCROLL_PAUSE)

                new_urls_count = len(urls) - current_urls_on_page_load
                logger.info(f"Page {pnum}: +{new_urls_count} URLs. Total: {len(urls)}")

                # Pagination end detection
                if max_pages == -1 and new_urls_count == 0 and pnum > 1:
                    logger.info("End of pagination detected (no new products)")
                    return urls
                
                # Check for anti-scraping measures here (e.g., CAPTCHA selector)
                # if await page.query_selector(".captcha-element"):
                #     logger.critical("CAPTCHA DETECTED. Stopping URL collection.")
                #     return urls

                break # Page loaded successfully

            except PlaywrightTimeoutError as e:
                logger.warning(f"Timeout on index page {pnum} (attempt {attempt}).")
                await asyncio.sleep(1.5 ** attempt) # Exponential backoff
            except Exception as e:
                logger.error(f"Generic error on index page {pnum} (attempt {attempt}): {e}")
                await asyncio.sleep(1.5 ** attempt)

        pnum += 1

    return urls

async def scrape_single_product(page: Page, url: str, failed_log_writer: csv.writer, stop_flag: asyncio.Event = None) -> Optional[ScrapedItem]:
    """
    Scrape a single product page, trying JSON-LD first and falling back to HTML.
    Returns a ScrapedItem dataclass instance.
    """
    BASE_BACKOFF: float = 1.5
    url_to_scrape: str = _sanitize_url(url)
    current_time: datetime = datetime.now()
    
    # Check for early stop
    if stop_flag and stop_flag.is_set():
        return None

    for attempt in range(1, MAX_PRODUCT_RETRIES + 1):
        try:
            # 1. Page Navigation
            await page.goto(url_to_scrape, wait_until="load", timeout=PAGE_LOAD_TIMEOUT)

            # Check for HTTP 403/429 status code for anti-bot detection
            if page.main_frame.url.startswith("data:text/html"):
                 logger.warning(f"Failed to navigate to {url_to_scrape}. Possible block or redirect.")
                 raise PlaywrightTimeoutError("Navigation blocked.")

            # Explicit wait for the JSON-LD script 
            await page.wait_for_selector(JSON_LD_SELECTOR, timeout=5000)

            # 2. Try JSON-LD first
            json_data = await parse_product_jsonld(page)
            
            if json_data and is_valid_sku(json_data.get('sku_raw')):
                logger.debug(f"Success: JSON-LD found for {url_to_scrape}")
                return ScrapedItem(
                    sku_raw=json_data['sku_raw'],
                    price_raw=json_data['price_raw'],
                    availability_raw=json_data['availability_raw'],
                    url=url_to_scrape,
                    timestamp=current_time,
                    detection_status="OK"
                )

            # 3. Fallback to HTML scraping
            # If JSON-LD failed, log a warning and capture minimal fallback data
            
            # --- Anti-Scraping Check Placeholder ---
            # if await page.query_selector(".anti-bot-message"):
            #     logger.error(f"Anti-Bot message detected on {url_to_scrape}")
            #     failed_log_writer.writerow([url_to_scrape, "CAPTCHA/Bot Block", "N/A"])
            #     return ScrapedItem("N/A_failed", "-1", "Blocked", url_to_scrape, current_time, "CAPTCHA_DETECTED")
            # ----------------------------------------
            
            # Fallback for price only (SKU remains the unique ID)
            price_text: str = ""
            try:
                el = await page.query_selector("span.price, .product-price")
                if el:
                    price_text = await el.inner_text()
            except Exception:
                logger.debug(f"HTML price extraction failed for {url_to_scrape}")

            # Log failure details for analysis
            failed_log_writer.writerow([url_to_scrape, "Missing/Invalid JSON-LD", price_text or "No price found"])

            return ScrapedItem(
                sku_raw=get_product_id_from_url(url_to_scrape) or "N/A_fallback",
                price_raw=price_text or "-1",
                availability_raw="Fallback",
                url=url_to_scrape,
                timestamp=current_time,
                detection_status="OK" # OK means page was reachable, failure was in parsing
            )

        except PlaywrightTimeoutError:
            logger.warning(f"Timeout {url_to_scrape} (attempt {attempt})")
            await asyncio.sleep(BASE_BACKOFF ** attempt * 0.5)
            continue
        except Exception as e:
            logger.error(f"Error {url_to_scrape} (attempt {attempt}): {e}")
            await asyncio.sleep(BASE_BACKOFF ** attempt * 0.5)
            continue

    # Final failure after all retries
    failed_log_writer.writerow([url_to_scrape, "Failed after all retries", "N/A"])
    logger.warning(f"Giving up on {url_to_scrape}")
    return ScrapedItem("N/A_failed", "-1", "Failed", url_to_scrape, current_time, "HTTP_BLOCKED")
