import re
import csv
import json
import logging
import asyncio
from datetime import datetime
from typing import List, Set, Optional, Dict, Tuple

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError, async_playwright
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# Captcha related constants
CAPTCHA_SELECTORS = {
    'button': '#captcha-form .confirm-button, .captcha-button, .g-recaptcha, .h-captcha',  # Common captcha button selectors
    'frame': 'iframe[src*="captcha"], iframe[src*="recaptcha"], iframe[title*="captcha"]',  # Captcha iframes
    'checkbox': '.recaptcha-checkbox-border'  # reCAPTCHA checkbox
}

# Import configuration from settings
from settings import (
    BASE_URL, BRAND_PAGE, MAX_PAGE_RETRIES, SCROLL_ATTEMPTS, SCROLL_PAUSE, PAGE_LOAD_TIMEOUT,
    EXCLUDE_FRAGMENTS, JSON_LD_SELECTOR, PRODUCT_LINK_SELECTOR, MAX_PRODUCT_RETRIES,
    BLOCK_RESOURCES, SKU_FILE_NAME, FAILED_URLS_FILE, TARGET_BRAND, BLOCKED_DOMAINS
)
import os
from pathlib import Path

from src.models import ScrapedItem, FinalProductRecord, DetectionStatus, NormalizedData

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
            price_old=normalize_price(item.price_old_raw),
            availability_code=item.availability_raw,
            url=item.url,
            timestamp=item.timestamp,
            detection_status=item.detection_status
        )
        filtered_records.append(record)

    return filtered_records

# --- Playwright Helpers (E-Step) ---

async def _block_resources(page: Page) -> None:
    """Enhanced resource blocking with performance optimization."""
    async def route_handler(route):
        try:
            request_url = route.request.url
            resource_type = route.request.resource_type
            
            # Allow essential resources immediately
            essential_types = {'document', 'script', 'xhr', 'fetch'}
            if resource_type in essential_types:
                await route.continue_()
                return

            # Block known advertising and tracking domains immediately
            ad_tracking_keywords = [
                'google-analytics', 'googleads', 'doubleclick',
                'facebook', 'google-tag', 'yandex', 'metrika'
            ]
            if any(keyword in request_url.lower() for keyword in ad_tracking_keywords):
                await route.abort()
                return
                
            # Block by resource type
            if resource_type in BLOCK_RESOURCES:
                await route.abort()
                return
                
            # Block by domain using configuration
            if any(domain.replace('*', '') in request_url for domain in BLOCKED_DOMAINS):
                await route.abort()
                return
                
            # Block heavy media resources
            if resource_type in {'image', 'media', 'font'}:
                # Allow small images and essential fonts
                if resource_type == 'image' and '.svg' in request_url.lower():
                    await route.continue_()
                    return
                    
                headers = route.request.headers
                if 'accept' in headers and 'image/svg' in headers['accept']:
                    await route.continue_()
                    return
                    
                await route.abort()
                return
            
            # Continue with everything else
            await route.continue_()
            
        except Exception as e:
            logger.error(f"Error in route handler: {str(e)}")
            # On error, continue the request to prevent hanging
            await route.continue_()
    
    # Set up routes with error handling
    try:
        await page.route("**/*", route_handler)
    except Exception as e:
        logger.error(f"Failed to set up resource blocking: {str(e)}")
        
    # Set timeouts at page level
    page.set_default_timeout(PAGE_LOAD_TIMEOUT)  # For navigations
    page.set_default_navigation_timeout(PAGE_LOAD_TIMEOUT)  # Specific for navigation
    
    # Optimize page performance
    await page.set_extra_http_headers({
        "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache"
    })

async def parse_product_jsonld(page: Page) -> Optional[Dict[str, str]]:
    """Parse JSON-LD structured data for product info (sku, price, availability)."""
    # Try different selectors to find JSON-LD scripts
    selectors = [
        'script[type="application/ld+json"]'  # Only using the standard double-quoted version
    ]

    ## TODO move out
    class SchemaNotFoundError(Exception):
        """Raised when query_selector_all returns an empty list."""
        pass
    
    @retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    # Only retry if our custom exception is raised (list was empty)
    retry=retry_if_exception_type(SchemaNotFoundError),
    reraise=True 
    )
    async def get_product_schema_robustly(page):
        """
        Attempts to get the JSON-LD schema, retrying if the element is not yet available.
        """
        selector = 'script[type="application/ld+json"]' 
        
        # 1. Immediately query the DOM
        scripts = await page.query_selector_all(selector)
        
        # 2. If the list is empty, raise our custom error to trigger a retry
        if not scripts:
            raise SchemaNotFoundError("JSON-LD script list was empty.")

        # 3. If successful, get the content of the first script
        # (You may need to iterate through 'scripts' to find the one with "@type":"Product")
        return scripts

    ## TODO move out end

    # --- Main Execution ---
    try:
        # This call handles all retries for you.
        all_scripts = await get_product_schema_robustly(page) 
    except SchemaNotFoundError:
        # This catches the final failure after 3 attempts
        logger.warning(f"No JSON-LD found on URL after 3 retries.") # TODO unknown url_to_scrape
    except Exception as e:
        # Catch any other unexpected errors
        logger.error(f"An unexpected error occurred: {e}")

    for s in all_scripts:
        try:
            # Try both innerHTML and innerText as some pages might format differently
            text = await s.evaluate('el => el.innerHTML || el.innerText')
            text = text.strip()
            if not text:
                continue

            try:
                data = json.loads(text)
            except json.JSONDecodeError as e:
                try:
                    data = clean_json_string(text)
                    data = json.loads(data)
                except Exception as inner_e:
                    logger.debug(f"JSON deep decode error: {str(inner_e)}")
                    continue


            candidates = data if isinstance(data, list) else [data]

            for item in candidates:
                if not isinstance(item, dict):
                    continue
                    
                # Check if it's a product type
                if item.get("@type") != "Product":
                    continue

                # Check for brand
                brand_data = item.get("brand", {})
                if isinstance(brand_data, dict):
                    brand_name = brand_data.get("name", "").strip()
                    # Handle HTML entity in brand name
                    brand_name = brand_name.replace("&amp;", "&")
                    if brand_name != TARGET_BRAND:
                        logger.debug(f"Skipping non-{TARGET_BRAND} product. Brand: {brand_name}")
                        continue

                # Try different possible SKU fields
                sku = None
                for sku_field in ["sku", "productID", "mpn"]:
                    sku_raw = str(item.get(sku_field, "")).strip()
                    if sku_raw and sku_raw.lower() != "none" and len(sku_raw) >= 3:
                        sku = sku_raw
                        break

                if not sku:
                    continue

                # Handle offers data
                offers = item.get("offers", {}) or {}
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                elif not isinstance(offers, dict):
                    continue

                # Get current price and handle different formats
                price = str(offers.get("price", "-1")).strip()
                
                # Try to get old price from DOM
                price_old = "-1"
                try:
                    old_price_selectors = [
                        ".price__old",       # Primary selector
                        ".price--old",       # Alternative format
                        ".original-price",   # Another common pattern
                        ".product-price-old" # Backup selector
                    ]
                    for selector in old_price_selectors:
                        old_price_element = await page.query_selector(selector)
                        if old_price_element:
                            price_old_text = await old_price_element.inner_text()
                            # Clean up the price (remove currency symbols, spaces, etc.)
                            price_old = re.sub(r'[^\d.]', '', price_old_text)
                            if price_old:  # If we found a valid price, break the loop
                                break
                except Exception as e:
                    logger.debug(f"Error extracting old price from DOM: {str(e)}")

                logger.debug(f"Found JSON-LD data: SKU={sku}, Price={price}")
                # Include brand information in the returned data
                return {
                    "sku_raw": sku,
                    "price_raw": price,
                    "price_old_raw": price_old,
                    "availability_raw": "-1",  # Will be set by DOM extraction later
                    "brand": brand_name  # Include brand for logging/debugging
                }
                
        except json.JSONDecodeError as e:
            logger.debug(f"JSON decode error: {str(e)}")
        except Exception as e:
            logger.debug(f"Error parsing JSON-LD: {str(e)}")
            
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
                try:
                    # First try with networkidle
                    await page.goto(
                        page_url,
                        wait_until="networkidle",
                        timeout=PAGE_LOAD_TIMEOUT // 2  # Use shorter timeout for first attempt
                    )
                except PlaywrightTimeoutError:
                    logger.info("Network idle timeout, falling back to domcontentloaded...")
                    # Fall back to domcontentloaded if networkidle times out
                    await page.goto(
                        page_url,
                        wait_until="domcontentloaded",
                        timeout=PAGE_LOAD_TIMEOUT
                    )
                    
                # Check if we need to wait for specific elements
                try:
                    await page.wait_for_selector(PRODUCT_LINK_SELECTOR, timeout=5000)
                except PlaywrightTimeoutError:
                    logger.warning("Product links not immediately available, continuing anyway")
                
                # Add small delay for dynamic content
                await page.wait_for_timeout(1000)

                prev_count: int = -1
                current_urls_on_page_load: int = len(urls)

                for _ in range(SCROLL_ATTEMPTS):
                    anchors = await page.query_selector_all(".product__item a[href*='/p']")

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

async def check_for_captcha(page: Page, url: str) -> bool: # TODO consider removing
    """
    Advanced captcha detection by analyzing page content, elements, and behavior.
    Returns True if captcha is detected.
    """
    is_debug = os.environ.get('DEBUG_MODE', '').lower() == 'true'
    
    try:
        # 1. Get page content and analyze it
        page_content = await page.content()
        page_text = await page.evaluate('document => document.body.innerText')
        
        # 2. Check for common captcha and city confirmation indicators in content
        content_indicators = [
            'captcha',
            'verify you',
            'prove you',
            'are you human',
            'security check',
            'verification',
            'robot',
            'automatic request',
            'suspicious activity',
            'unusual traffic',
            'виберіть місто',  # Ukrainian
            'оберіть місто',   # Ukrainian
            'ваше місто',      # Ukrainian
            'підтвердіть місто', # Ukrainian
            'выберите город',   # Russian
            'выбор города',     # Russian
            'подтвердите город' # Russian
        ]
        
        # 3. Look for visual indicators (elements that might be captcha or city selection)
        visual_indicators = [
            # Captcha selectors
            'iframe[src*="captcha"]',
            'iframe[src*="challenge"]',
            'iframe[title*="challenge"]',
            'form[action*="captcha"]',
            'div[class*="captcha"]',
            'div[id*="captcha"]',
            'img[alt*="captcha"]',
            '#captcha',
            '.captcha',
            '.g-recaptcha',
            '.h-captcha',
            # City selection selectors
            '.city-select',
            '.city-selector',
            '.city-confirm',
            '.city-modal',
            '[data-city-select]',
            '[data-modal="city"]',
            '.location-confirm',
            '.region-select',
            '#city-selection',
            '.city-selection-modal'
        ]
        
        # Add specific city confirmation buttons/links
        city_confirm_selectors = [
            'button[data-city]',
            'a[data-city]',
            '.confirm-city-btn',
            '.city-confirm-btn',
            'button:has-text("Так")',           # Ukrainian "Yes"
            'button:has-text("Підтвердити")',   # Ukrainian "Confirm"
            'button:has-text("Да")',            # Russian "Yes"
            'button:has-text("Подтвердить")',   # Russian "Confirm"
            '[data-choice="confirm"]'
        ]

        # 4. Check for blocked status
        blocked_indicators = [
            'access denied',
            'blocked',
            'too many requests',
            '429',
            'rate limit',
            'try again later'
        ]

        # Check content indicators
        content_detected = any(indicator.lower() in page_text.lower() for indicator in content_indicators)
        
        # Check visual elements
        has_visual_elements = False
        for selector in visual_indicators:
            try:
                element = await page.query_selector(selector)
                if element:
                    logger.info(f"Found potential captcha element: {selector}")
                    has_visual_elements = True
                    break
            except Exception:
                continue

        # Check for blocked status
        is_blocked = any(indicator.lower() in page_text.lower() for indicator in blocked_indicators)

        # 5. Check for city confirmation dialog
        city_dialog_detected = False
        selected_city = False
        
        for selector in city_confirm_selectors:
            try:
                city_button = await page.query_selector(selector)
                if city_button:
                    logger.info(f"Found city confirmation button: {selector}")
                    city_dialog_detected = True
                    try:
                        # Try to click the city confirmation button
                        await city_button.click()
                        logger.info("Clicked city confirmation button")
                        # Wait for any animations/transitions
                        await page.wait_for_timeout(2000)
                        selected_city = True
                        break
                    except Exception as e:
                        logger.error(f"Failed to click city button: {str(e)}")
            except Exception:
                continue
                
        # 6. Take debugging actions if captcha or city dialog is detected
        is_captcha = content_detected or has_visual_elements or is_blocked or city_dialog_detected
        
        if is_captcha:
            status = []
            if content_detected:
                status.append("Captcha/Block content")
            if has_visual_elements:
                status.append("Captcha elements")
            if is_blocked:
                status.append("Access blocked")
            if city_dialog_detected:
                status.append("City selection" + (" (handled)" if selected_city else " (not handled)"))
                
            logger.warning(f"Detection on {url}: {', '.join(status)}")
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            
            # Create debug directory if it doesn't exist
            os.makedirs('debug_screenshots', exist_ok=True)
            
            # Take both full page and viewport screenshots
            await page.screenshot(
                path=f"debug_screenshots/captcha_full_{timestamp}.png",
                full_page=True
            )
            await page.screenshot(
                path=f"debug_screenshots/captcha_viewport_{timestamp}.png",
                full_page=False
            )
            
            # Save page HTML for analysis
            with open(f"debug_screenshots/page_source_{timestamp}.html", "w", encoding="utf-8") as f:
                f.write(page_content)
                
            # Log detection type
            if content_detected:
                logger.info("Captcha detected through content analysis")
            if has_visual_elements:
                logger.info("Captcha detected through visual elements")
            if is_blocked:
                logger.info("Access appears to be blocked")

            if is_debug:
                logger.info("Debug mode: Manual inspection time...")
                # In debug mode, keep the page open longer for inspection
                await page.wait_for_timeout(60000)  # 1 minute timeout for manual inspection
                
                # After timeout, recheck if we're still blocked
                new_content = await page.content()
                if any(indicator in new_content.lower() for indicator in blocked_indicators):
                    logger.warning("Still blocked after manual inspection")
                else:
                    logger.info("Page appears to be unblocked after manual inspection")
                    
        return is_captcha
        
    except Exception as e:
        logger.error(f"Error in captcha detection: {str(e)}")
        return False  # Assume no captcha on error to allow retry logic to handle it

async def scrape_single_product(page: Page, url: str, failed_log_data: list, stop_flag: asyncio.Event = None) -> Optional[ScrapedItem]:
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
            # Add increasing delay between retries
            if attempt > 1:
                delay = BASE_BACKOFF ** (attempt - 1) * 2  # Increased delay for subsequent attempts
                logger.info(f"Retry attempt {attempt} for {url_to_scrape}, waiting {delay:.1f}s")
                await asyncio.sleep(delay)
                
                # Only clear cookies if we got blocked (403/429) in the previous attempt
                if attempt > MAX_PRODUCT_RETRIES - 1:  # On last retry attempt
                    logger.info(f"Last retry attempt, clearing cookies for {url_to_scrape}")
                    await page.context.clear_cookies()
            
            # 1. Page Navigation
            logger.info(f"Attempting to navigate to {url_to_scrape}")
            try:
                response = await page.goto(
                    url_to_scrape, 
                    wait_until="domcontentloaded", 
                    timeout=PAGE_LOAD_TIMEOUT * 2  # Double the timeout for problematic URLs
                )
                logger.info(f"Basic page load completed for {url_to_scrape}")
                
                # Log detailed response info
                status = response.status if response else 'No response'
                logger.info(f"Response status for {url_to_scrape}: {status}")
                
                if response and not response.ok:
                    logger.error(f"Bad response status {status} for {url_to_scrape}")
                    if status == 403:
                        logger.error("Access forbidden - might be IP blocked")
                    elif status == 429:
                        logger.error("Too many requests - rate limited")
                    elif status == 404:
                        logger.error("Page not found - 404 error")

            except PlaywrightTimeoutError as e:
                logger.warning(f"Initial page load timeout for {url_to_scrape}: {str(e)}")
                raise
            except Exception as e:
                logger.error(f"Navigation error for {url_to_scrape}: {type(e).__name__}: {str(e)}")
                raise

            # Enhanced error detection
            if page.main_frame.url.startswith("data:text/html"):
                logger.warning(f"Failed to navigate to {url_to_scrape}. Possible block or redirect.")
                raise PlaywrightTimeoutError("Navigation blocked.")

            async def check_access(page):
                # 1. Await the content once and store the result (a string)
                page_html = await page.content()
                
                # 2. Convert the content string to lowercase once for efficient checking
                page_text_lower = page_html.lower() 
                
                # 3. Perform all checks on the stored, processed string
                if "access denied" in page_text_lower or "blocked" in page_text_lower:
                    print("Blocked or Access Denied page detected!")
                    return True
                
                return False

            if await check_access(page):
                logger.error(f"Access denied/blocked on {url_to_scrape}")
                raise Exception("Access denied")
                
            try:
                # Try JSON-LD parsing
                logger.info(f"Attempting to parse JSON-LD for {url_to_scrape}")
                json_data = await parse_product_jsonld(page)
                
                if json_data is None:
                    logger.debug(f"Skipping non-{TARGET_BRAND} product: {url_to_scrape}")
                    return None

                # Get availability information OUTSIDE OF JSON-LD parsing
                availability_text_clean = "-1"
                try:
                   # Try to find the new price (using multiple possible selectors)
                    price_selectors = [
                        ".price__new",  # Main new price
                        "span.price",    # Alternative selector
                        ".product-price" # Another alternative
                    ]
                    for selector in price_selectors:
                        el = await page.query_selector(selector)
                        if el:
                            price_text = await el.inner_text()
                            break
                    
                    # Try to find the old price
                    old_price_selectors = [
                        ".price__old",      # Main old price
                        ".price--old",      # Alternative
                        ".original-price"   # Another alternative
                    ]
                    for selector in old_price_selectors:
                        el = await page.query_selector(selector)
                        if el:
                            price_old_text = await el.inner_text()
                            break

                    # Try to get availability information
                    availability_selector = "section.article.product-detail .item-column .terms"
                    el = await page.query_selector(availability_selector)

                    if el:
                        availability_text = await el.inner_text()
                        availability_text_clean = availability_text
                except Exception as e:
                    logger.debug(f"Error extracting availability from DOM: {str(e)}")
                
                if is_valid_sku(json_data.get('sku_raw')):
                    logger.debug(f"Success: {TARGET_BRAND} product found for {url_to_scrape}")
                    return ScrapedItem(
                        sku_raw=json_data['sku_raw'],
                        price_raw=json_data['price_raw'],
                        price_old_raw=json_data['price_old_raw'],
                        availability_raw=availability_text_clean,
                        url=url_to_scrape,
                        timestamp=current_time,
                        detection_status="OK"
                    )
            except PlaywrightTimeoutError:
                logger.warning(f"Timeout waiting for JSON-LD on {url_to_scrape}")
                # log_failed_urls(failed_log_data)
            except Exception as e:
                logger.warning(f"Error parsing JSON-LD on {url_to_scrape}: {str(e)}")
                # log_failed_urls(failed_log_data)

            # 3. Fallback to HTML scraping
            # If JSON-LD failed, log a warning and capture minimal fallback data
            
            # --- Anti-Scraping Check Placeholder ---
            # if await page.query_selector(".anti-bot-message"):
            #     logger.error(f"Anti-Bot message detected on {url_to_scrape}")
            #     failed_log_writer.writerow([url_to_scrape, "CAPTCHA/Bot Block", "N/A"])
            #     return ScrapedItem("N/A_failed", "-1", "Blocked", url_to_scrape, current_time, "CAPTCHA_DETECTED")
            # ----------------------------------------
            
            # Fallback to HTML scraping
            price_text: str = ""
            price_old_text: str = ""
            availability_text_clean: str = "-1"

            try:
                # Try to find the new price (using multiple possible selectors)
                price_selectors = [
                    ".price__new",  # Main new price
                    "span.price",    # Alternative selector
                    ".product-price" # Another alternative
                ]
                for selector in price_selectors:
                    el = await page.query_selector(selector)
                    if el:
                        price_text = await el.inner_text()
                        break
                
                # Try to find the old price
                old_price_selectors = [
                    ".price__old",      # Main old price
                    ".price--old",      # Alternative
                    ".original-price"   # Another alternative
                ]
                for selector in old_price_selectors:
                    el = await page.query_selector(selector)
                    if el:
                        price_old_text = await el.inner_text()
                        break

                # Try to get availability information
                availability_selector = "xpath=//div[contains(@class, 'product__delivery')]//*[contains(text(), 'Відправка') or contains(text(), 'В наявності')]"
                el = await page.query_selector(availability_selector)
                if el:
                    availability_text = await el.inner_text()
                    availability_text_clean = availability_text#availability_dict.get(availability_text, availability_text)

                # Clean up the prices (remove currency symbols and spaces)
                price_text = re.sub(r'[^\d.]', '', price_text)
                if price_old_text:
                    price_old_text = re.sub(r'[^\d.]', '', price_old_text)

            except Exception as e:
                logger.debug(f"HTML extraction failed for {url_to_scrape}: {str(e)}")

            # Only log as failure if we couldn't get the current price
            if not price_text:
                failed_log_data.append([
                    url_to_scrape,
                    "No price found in either JSON-LD or HTML",
                    f"Price: Not found, Old Price: {price_old_text or 'Not found'}, Availability: {availability_text_clean}"
                ])

            return ScrapedItem(
                sku_raw=get_product_id_from_url(url_to_scrape) or "N/A_fallback",
                price_raw=price_text or "-1",
                price_old_raw=price_old_text or "-1",
                availability_raw=availability_text_clean,
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
    failed_log_data.append([url_to_scrape, "Failed after all retries", "N/A"])
    logger.warning(f"Giving up on {url_to_scrape}")
    return ScrapedItem("N/A_failed", "-1", "Failed", url_to_scrape, current_time, "HTTP_BLOCKED")

def clean_json_string(raw_json_text: str) -> str:
    """
    Cleans up raw JSON text by escaping common invalid control characters 
    before decoding.
    """
    # 1. Remove all unescaped newline, carriage return, and tab characters.
    # The 're.sub' function replaces the pattern with the proper escaped character.
    # Newlines (\n) and carriage returns (\r) are the most common offenders.
    
    # Escape newlines (\n) that are not already escaped (e.g., \\n)
    cleaned_text = re.sub(r'(?<!\\)[\n\r]', ' ', raw_json_text)
    cleaned_text = re.sub(r'[\t]+', ' ', cleaned_text)
    
    # NOTE: You may need more advanced cleaning if the issue is unescaped double quotes 
    # within nested strings (which is a harder problem that might require a specialized parser).
    
    return cleaned_text