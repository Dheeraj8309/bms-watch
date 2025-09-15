import os, re, json, time, smtplib
from pathlib import Path
from email.mime.text import MIMEText

# Optional for local runs; on GitHub Actions we use env vars
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# ===== Targeting (env-overridable) =====
CITY_SLUG         = os.getenv("CITY_SLUG", "vijayawada")
# Exact movie: "Demon Slayer: Kimetsu no Yaiba Infinity Castle - (Japanese)"
MOVIE_KEYWORDS    = [s.strip().lower() for s in os.getenv(
    "MOVIE_KEYWORDS",
    "demon,slayer,infinity,castle,japanese"
).split(",")]
# Exact theatre: "INOX: Laila Mall, M.G. Road"
THEATRE_KEYWORDS  = [s.strip().lower() for s in os.getenv(
    "THEATRE_KEYWORDS",
    "inox,laila,mg,road"
).split(",")]
CHECK_DATES_AHEAD = int(os.getenv("CHECK_DATES_AHEAD", "7"))

# Local-only de-dupe (GitHub runner is ephemeral)
STATE_FILE = Path("state.json")

# ===== Email config =====
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
EMAIL_TO  = os.getenv("EMAIL_TO")


def send_email(subject: str, body: str) -> bool:
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS and EMAIL_TO):
        print("[WARN] Email env vars missing—cannot send email.")
        return False
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)
    return True


def normalize(s: str) -> str:
    """Lowercase + strip punctuation so 'INOX:' ~ 'inox' and 'M.G. Road' ~ 'mg road'."""
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9\s]+", " ", s)  # drop punctuation to spaces
    s = re.sub(r"\s+", " ", s).strip()
    return s


def contains_all(text: str, keywords: list[str]) -> bool:
    t = normalize(text)
    return all(kw in t for kw in keywords)


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except:
            return {}
    return {}


def save_state(d): STATE_FILE.write_text(json.dumps(d, indent=2))


def run_check():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="en-IN",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            )
        )
        page = ctx.new_page()

        # Use city-specific URL to avoid region/city popups on runners
        movies_url = f"https://in.bookmyshow.com/{CITY_SLUG}/movies"
        page.goto(movies_url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(2)

        # Handle possible popups (cookie / allow / consent)
        for sel in [
            'button:has-text("Allow")',
            'button:has-text("OK")',
            'button:has-text("Accept")',
            'button:has-text("I Agree")',
            'button:has-text("Got it")',
            'button:has-text("Continue")',
        ]:
            try:
                if page.is_visible(sel, timeout=1500):
                    page.click(sel)
                    print(f"[INFO] Dismissed popup: {sel}")
                    time.sleep(1)
            except:
                pass

        # Wait for movie cards (use relaxed selectors)
        try:
            page.wait_for_selector(
                '[data-component="listingCard"], [class*="listing-card"], a[href*="/movie/"]',
                timeout=60000
            )
        except:
            print("[ERROR] Could not find movie cards on BMS page (selector timeout).")
            ctx.close(); browser.close()
            return {"status": "no-movie-card"}

        movie_link = None
        # Try cards first
        cards = page.query_selector_all('[data-component="listingCard"], [class*="listing-card"]') or []
        for card in cards:
            try:
                title_el = card.query_selector("a, [data-title], h3, h2")
                title = title_el.inner_text().strip() if title_el else ""
            except:
                title = ""
            if contains_all(title, MOVIE_KEYWORDS):
                a = card.query_selector("a")
                href = a.get_attribute("href") if a else None
                if href:
                    movie_link = "https://in.bookmyshow.com" + href if href.startswith("/") else href
                    break

        # Fallback: search links if structured cards not found
        if not movie_link:
            for a in page.query_selector_all('a[href*="/movie/"]'):
                txt = (a.inner_text() or "").strip()
                if contains_all(txt, MOVIE_KEYWORDS):
                    href = a.get_attribute("href")
                    if href:
                        movie_link = "https://in.bookmyshow.com" + href if href.startswith("/") else href
                        break

        if not movie_link:
            print("[INFO] Target movie card not visible yet.")
            ctx.close(); browser.close()
            return {"status": "no-movie-card"}

        # Open movie page and reveal showtimes if needed
        page.goto(movie_link, wait_until="domcontentloaded", timeout=60000)
        time.sleep(2)
        for sel in ['a:has-text("Book")', 'button:has-text("Book tickets")', 'button:has-text("Book")']:
            try:
                if page.is_visible(sel, timeout=1500):
                    page.click(sel)
                    page.wait_for_load_state("domcontentloaded", timeout=60000)
                    time.sleep(2)
                    break
            except:
                pass

        found_show, theatre_name, times = False, None, []

        def scan():
            nonlocal found_show, theatre_name, times
            # Look for theatre names in several possible containers
            name_els = page.query_selector_all(
                '[data-component="venue-name"], [data-component="cinema-name"], '
                '[class*="venue-name"], [class*="cinema-name"], h4, h3'
            )
            for el in name_els:
                try:
                    name = el.inner_text().strip()
                except:
                    continue
                if not name or not contains_all(name, THEATRE_KEYWORDS):
                    continue

                # Climb up a bit and look for clickable time labels in the same block
                block = el
                for _ in range(3):
                    try:
                        block = block.locator("xpath=..").element_handle()
                    except:
                        break

                btn_times = []
                try:
                    for b in block.query_selector_all("a, button"):
                        t = (b.inner_text() or "").strip()
                        # Accept HH:MM with or without AM/PM
                        if re.search(r"\b\d{1,2}:\d{2}\s*(AM|PM)?\b", t, re.I) and \
                           "disabled" not in normalize(b.get_attribute("class") or ""):
                            btn_times.append(t)
                except:
                    pass

                if btn_times:
                    found_show, theatre_name, times = True, name, btn_times
                    return

        # Today
        scan()
        # Click a few dates ahead if needed
        if not found_show:
            for sel in ['[data-component="dateFilter"] button', 'button[aria-label*="Select date"]']:
                try:
                    if page.is_visible(sel, timeout=1500):
                        tabs = page.query_selector_all(sel)
                        for t in tabs[1:CHECK_DATES_AHEAD + 1]:
                            try:
                                t.click()
                                page.wait_for_load_state("domcontentloaded", timeout=60000)
                                time.sleep(2)
                                scan()
                                if found_show:
                                    break
                            except:
                                pass
                except:
                    pass

        ctx.close(); browser.close()

        if found_show:
            return {"status": "live", "theatre": theatre_name, "times": times[:10]}
        else:
            return {"status": "no-showtimes"}


def main():
    state = load_state()
    already_alerted = bool(state.get("alerted"))

    result = run_check()

    if result["status"] == "live":
        subject = "🎬 BookMyShow Alert: Demon Slayer @ INOX Laila Mall LIVE"
        body = (
            f"Tickets are LIVE at {result['theatre']} in {CITY_SLUG.title()}.\n"
            f"Showtimes: {', '.join(result['times'])}\n\n"
            f"Open BookMyShow and book now."
        )
        print("[ALERT]", subject)
        if not already_alerted:
            if send_email(subject, body):
                state["alerted"] = True
                save_state(state)
        return 0
    elif result["status"] == "no-movie-card":
        print("[INFO] Target movie card not visible yet.")
        return 0
    else:
        print("[INFO] No showtimes yet for target theatre.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
