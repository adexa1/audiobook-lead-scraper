import asyncio
import csv
import os
import random
from playwright.async_api import async_playwright
from playwright_stealth import stealth_async
from amazoncaptcha import AmazonCaptcha

# ── Configuration ─────────────────────────────────────────────────────────────

TARGET_GENRES = {
    "Thriller": "https://www.amazon.com/Best-Sellers-Books-Mystery-Thriller-Suspense/zgbs/books/18/",
    "Sci-Fi": "https://www.amazon.com/Best-Sellers-Books-Science-Fiction-Fantasy/zgbs/books/25/",
    "Self-Help": "https://www.amazon.com/Best-Sellers-Books-Self-Help/zgbs/books/4736/"
}

MIN_RATING = 4.3
MIN_REVIEWS = 100
MAX_PAGES = 3  # Number of paginated pages to scrape per genre

# Proxy is loaded from the PROXY_URL GitHub Secret — never hardcode credentials here
PROXY_SERVER = os.environ.get("PROXY_URL")  # None if secret not set; script runs without proxy

# ── Helpers ───────────────────────────────────────────────────────────────────

def random_delay(min_s=1.5, max_s=4.0):
    """Return a randomised sleep duration to mimic human behaviour."""
    return random.uniform(min_s, max_s)


async def handle_captcha(page):
    """Detect and attempt to solve an Amazon CAPTCHA. Returns True if resolved."""
    content = await page.content()
    if "api-services-support@amazon.com" not in content:
        return True  # No CAPTCHA present

    print("[*] CAPTCHA detected. Attempting automated solve...")
    captcha_img = await page.query_selector('form[action="/errors/validateCaptcha"] img')

    if not captcha_img:
        print("[!] Unrecognised CAPTCHA format. Proxy or manual intervention required.")
        return False

    img_src = await captcha_img.get_attribute("src")
    captcha = AmazonCaptcha.fromlink(img_src)
    solution = captcha.solve()
    print(f"[*] CAPTCHA solved: {solution}. Submitting...")

    await page.fill("#captchacharacters", solution)
    await page.click('button[type="submit"]')
    await page.wait_for_load_state("domcontentloaded")
    await asyncio.sleep(2)
    return True


# ── Fix 1: Robust format detection via the book's detail page ─────────────────

async def has_audiobook_edition(browser_context, book_url: str) -> bool:
    """
    Navigate to the individual book page and check for an audiobook edition
    using the 'Other formats' section — far more reliable than grid-level text.
    """
    page = await browser_context.new_page()
    await stealth_async(page)

    try:
        await page.goto(book_url, wait_until="domcontentloaded", timeout=60000)

        if not await handle_captcha(page):
            return False  # Assume no audio on CAPTCHA failure to avoid false positives

        # Primary signal: format pills/tabs (Kindle | Paperback | Audiobook | ...)
        format_labels = await page.query_selector_all(
            '#tmmSwatches .a-button span.a-button-inner span, '
            '#formats .a-button span, '
            '.format_name'
        )
        for el in format_labels:
            text = (await el.inner_text()).strip().lower()
            if any(kw in text for kw in ["audible", "audiobook", "audio cd", "mp3 cd"]):
                return True

        # Secondary signal: "Customers also bought" / related products block
        page_text = await page.inner_text("body")
        if any(kw in page_text.lower() for kw in ["audible audiobook", "audio cd", "mp3 cd"]):
            return True

        return False

    except Exception as e:
        print(f"[!] Format check failed for {book_url}: {e}")
        return False
    finally:
        await page.close()


# ── Fix 5: Author extraction from the detail page ────────────────────────────

async def get_book_details(browser_context, book_url: str) -> dict:
    """
    Pull author name and confirm audiobook absence in a single detail-page visit,
    avoiding a double fetch.
    """
    page = await browser_context.new_page()
    await stealth_async(page)
    details = {"author": "Unknown", "has_audio": False}

    try:
        await page.goto(book_url, wait_until="domcontentloaded", timeout=60000)

        if not await handle_captcha(page):
            return details

        # Author
        author_el = await page.query_selector(
            '#bylineInfo .author a, '
            'span.author a.a-link-normal, '
            '#bylineInfo a.contributorNameID'
        )
        if author_el:
            details["author"] = (await author_el.inner_text()).strip()

        # Format check (same logic as has_audiobook_edition, combined here)
        format_labels = await page.query_selector_all(
            '#tmmSwatches .a-button span.a-button-inner span, '
            '#formats .a-button span, '
            '.format_name'
        )
        for el in format_labels:
            text = (await el.inner_text()).strip().lower()
            if any(kw in text for kw in ["audible", "audiobook", "audio cd", "mp3 cd"]):
                details["has_audio"] = True
                break

        if not details["has_audio"]:
            page_text = await page.inner_text("body")
            if any(kw in page_text.lower() for kw in ["audible audiobook", "audio cd", "mp3 cd"]):
                details["has_audio"] = True

    except Exception as e:
        print(f"[!] Detail fetch failed for {book_url}: {e}")
    finally:
        await page.close()

    return details


# ── Fix 2: Pagination support ─────────────────────────────────────────────────

def build_page_url(base_url: str, page_num: int) -> str:
    """Amazon bestseller pagination appends the page number to the base URL."""
    return f"{base_url}?pg={page_num}" if page_num > 1 else base_url


# ── Core scraper ──────────────────────────────────────────────────────────────

async def scrape_genre(browser_context, genre_name: str, base_url: str, seen_titles: set) -> list:
    leads = []

    for page_num in range(1, MAX_PAGES + 1):
        url = build_page_url(base_url, page_num)
        page = await browser_context.new_page()
        await stealth_async(page)

        print(f"\n[*] {genre_name} — Page {page_num}")

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)

            if not await handle_captcha(page):
                await page.close()
                break

            # Scroll to trigger lazy-loaded items
            for _ in range(5):
                await page.mouse.wheel(0, 1500)
                await asyncio.sleep(random_delay(0.8, 1.8))  # Fix 4: randomised scroll delay

            items = await page.query_selector_all('.zg-grid-general-faceout, .p13n-grid-content')
            print(f"[*] {len(items)} items found on page {page_num}")

            for item in items:
                try:
                    # Title
                    title_el = await item.query_selector(
                        'div._cDEBy_p13n-sc-css-line-clamp-1_1Fn1y, '
                        'span._cDEBy_p13n-sc-css-line-clamp-3_1Fn1y, '
                        'h2, .p13n-sc-truncated'
                    )
                    title = (await title_el.inner_text()).strip() if title_el else "Unknown Title"

                    # Fix 3: Deduplication check
                    if title in seen_titles:
                        continue
                    seen_titles.add(title)

                    # Rating
                    rating_el = await item.query_selector('.a-icon-row .a-icon-alt')
                    rating_text = await rating_el.inner_text() if rating_el else "0"
                    rating = float(rating_text.split()[0]) if rating_text else 0.0

                    # Reviews
                    review_el = await item.query_selector('.a-size-small .a-link-normal')
                    review_text = await review_el.inner_text() if review_el else "0"
                    reviews = int(review_text.replace(",", "")) if review_text else 0

                    # Pre-filter before hitting the detail page (saves requests)
                    if rating < MIN_RATING or reviews < MIN_REVIEWS:
                        continue

                    # Build detail URL
                    link_el = await item.query_selector("a")
                    relative = await link_el.get_attribute("href") if link_el else ""
                    book_url = "https://www.amazon.com" + relative if relative else ""

                    if not book_url:
                        continue

                    # Fix 1 + 5: Single detail-page fetch for author AND format check
                    details = await get_book_details(browser_context, book_url)

                    if details["has_audio"]:
                        print(f"    [skip] {title} — audiobook edition exists")
                        await asyncio.sleep(random_delay())  # Fix 4: delay between detail fetches
                        continue

                    print(f"    [lead] {title} by {details['author']} ({rating}★, {reviews:,} reviews)")
                    leads.append({
                        "Genre": genre_name,
                        "Title": title,
                        "Author": details["author"],
                        "Rating": rating,
                        "Reviews": reviews,
                        "Link": book_url
                    })

                    await asyncio.sleep(random_delay())  # Fix 4: delay between items

                except Exception as item_err:
                    print(f"[!] Item parse error: {item_err}")
                    continue

        except Exception as page_err:
            print(f"[!] Page error on {genre_name} p{page_num}: {page_err}")
        finally:
            await page.close()

        # Fix 4: Randomised delay between pages
        if page_num < MAX_PAGES:
            delay = random_delay(5.0, 10.0)
            print(f"[*] Waiting {delay:.1f}s before next page...")
            await asyncio.sleep(delay)

    return leads


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    launch_args = {"headless": True}
    if PROXY_SERVER:
        launch_args["proxy"] = {"server": PROXY_SERVER}

    async with async_playwright() as p:
        browser = await p.chromium.launch(**launch_args)
        browser_context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )

        all_leads = []
        seen_titles: set = set()  # Fix 3: shared dedup set across all genres

        for genre, url in TARGET_GENRES.items():
            genre_leads = await scrape_genre(browser_context, genre, url, seen_titles)
            all_leads.extend(genre_leads)

            # Fix 4: Longer randomised delay between genres
            delay = random_delay(8.0, 15.0)
            print(f"\n[*] Genre complete. Waiting {delay:.1f}s before next genre...")
            await asyncio.sleep(delay)

        # Export
        if all_leads:
            fieldnames = ["Genre", "Title", "Author", "Rating", "Reviews", "Link"]
            with open("audiobook_leads.csv", "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(all_leads)
            print(f"\n[*] Done. {len(all_leads)} leads exported to audiobook_leads.csv")
        else:
            print("\n[*] Done. No leads matched the criteria.")

        await browser_context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
