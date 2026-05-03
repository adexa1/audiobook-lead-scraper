import asyncio
import csv
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
MAX_PAGES = 3
PAGE_TIMEOUT = 120000  # 2 minutes per page

# ── Helpers ───────────────────────────────────────────────────────────────────

def random_delay(min_s=1.5, max_s=4.0):
    return random.uniform(min_s, max_s)


async def handle_captcha(page):
    content = await page.content()
    if "api-services-support@amazon.com" not in content:
        return True

    print("[*] CAPTCHA detected. Attempting automated solve...")
    captcha_img = await page.query_selector('form[action="/errors/validateCaptcha"] img')

    if not captcha_img:
        print("[!] Unrecognised CAPTCHA format.")
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


# ── Book detail fetch (author + format check) ─────────────────────────────────

async def get_book_details(browser_context, book_url: str) -> dict:
    page = await browser_context.new_page()
    await stealth_async(page)
    details = {"author": "Unknown", "has_audio": False}

    try:
        await page.goto(book_url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)

        if not await handle_captcha(page):
            return details

        author_el = await page.query_selector(
            '#bylineInfo .author a, '
            'span.author a.a-link-normal, '
            '#bylineInfo a.contributorNameID'
        )
        if author_el:
            details["author"] = (await author_el.inner_text()).strip()

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


# ── Pagination ────────────────────────────────────────────────────────────────

def build_page_url(base_url: str, page_num: int) -> str:
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
            await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)

            if not await handle_captcha(page):
                await page.close()
                break

            for _ in range(5):
                await page.mouse.wheel(0, 1500)
                await asyncio.sleep(random_delay(0.8, 1.8))

            items = await page.query_selector_all('.zg-grid-general-faceout, .p13n-grid-content')
            print(f"[*] {len(items)} items found on page {page_num}")

            for item in items:
                try:
                    title_el = await item.query_selector(
                        'div._cDEBy_p13n-sc-css-line-clamp-1_1Fn1y, '
                        'span._cDEBy_p13n-sc-css-line-clamp-3_1Fn1y, '
                        'h2, .p13n-sc-truncated'
                    )
                    title = (await title_el.inner_text()).strip() if title_el else "Unknown Title"

                    if title in seen_titles:
                        continue
                    seen_titles.add(title)

                    rating_el = await item.query_selector('.a-icon-row .a-icon-alt')
                    rating_text = await rating_el.inner_text() if rating_el else "0"
                    rating = float(rating_text.split()[0]) if rating_text else 0.0

                    review_el = await item.query_selector('.a-size-small .a-link-normal')
                    review_text = await review_el.inner_text() if review_el else "0"
                    reviews = int(review_text.replace(",", "")) if review_text else 0

                    if rating < MIN_RATING or reviews < MIN_REVIEWS:
                        continue

                    link_el = await item.query_selector("a")
                    relative = await link_el.get_attribute("href") if link_el else ""
                    book_url = "https://www.amazon.com" + relative if relative else ""

                    if not book_url:
                        continue

                    details = await get_book_details(browser_context, book_url)

                    if details["has_audio"]:
                        print(f"    [skip] {title} — audiobook edition exists")
                        await asyncio.sleep(random_delay())
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

                    await asyncio.sleep(random_delay())

                except Exception as item_err:
                    print(f"[!] Item parse error: {item_err}")
                    continue

        except Exception as page_err:
            print(f"[!] Page error on {genre_name} p{page_num}: {page_err}")
        finally:
            await page.close()

        if page_num < MAX_PAGES:
            delay = random_delay(5.0, 10.0)
            print(f"[*] Waiting {delay:.1f}s before next page...")
            await asyncio.sleep(delay)

    return leads


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        browser_context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
            },
            locale="en-US",
            timezone_id="America/New_York",
            viewport={"width": 1280, "height": 800},
        )

        all_leads = []
        seen_titles: set = set()

        for genre, url in TARGET_GENRES.items():
            genre_leads = await scrape_genre(browser_context, genre, url, seen_titles)
            all_leads.extend(genre_leads)

            delay = random_delay(8.0, 15.0)
            print(f"\n[*] Genre complete. Waiting {delay:.1f}s before next genre...")
            await asyncio.sleep(delay)

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
