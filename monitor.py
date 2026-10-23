import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright


# ==================================================
# SETTINGS (secrets come from GitHub Actions secrets)
# ==================================================

URL = os.environ["SCHOOL_URL"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
CREDENTIALS = {
    "student_id": os.environ["STUDENT_ID"],
    "password": os.environ["SCHOOL_PASSWORD"],
}

END_DATE = date(2026, 11, 9)
CHECK_INTERVAL_SECONDS = 60
RUN_MINUTES = 55  # stop before the 60-minute job limit; next hourly job takes over
SCAN_RETRIES = 2
FAILURES_BEFORE_ALERT = 2
NOTIFY_START = os.environ.get("NOTIFY_START") == "1"

JST = ZoneInfo("Asia/Tokyo")
ALERTED_FILE = Path("alerted.json")


def today_jst():
    return datetime.now(JST).date()


# ==================================================
# TELEGRAM
# ==================================================

def send_telegram(message):
    try:
        data = urllib.parse.urlencode(
            {"chat_id": CHAT_ID, "text": message}
        ).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data=data,
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
        if result.get("ok"):
            print("Telegram message sent.")
            return True
        print("Telegram API error.")
        return False
    except Exception as error:
        print("Telegram notification failed:", type(error).__name__)
        return False


# ==================================================
# STATE (so hourly restarts don't resend old alerts)
# ==================================================

def load_alerted():
    try:
        return set(json.loads(ALERTED_FILE.read_text(encoding="utf-8")))
    except Exception:
        return set()


def save_alerted(items):
    try:
        ALERTED_FILE.write_text(
            json.dumps(sorted(items)), encoding="utf-8"
        )
    except Exception as error:
        print("Could not save state:", error)


# ==================================================
# WEBSITE HELPERS
# ==================================================

def find_locator(page, selector):
    for frame in page.frames:
        try:
            locator = frame.locator(selector)
            if locator.count() > 0:
                return locator
        except Exception:
            continue
    return None


def click_text(page, text):
    for frame in page.frames:
        try:
            locator = frame.get_by_text(text, exact=False).first
            if locator.count() and locator.is_visible():
                locator.click(timeout=10000, no_wait_after=True)
                return True
        except Exception:
            continue
    return False


def get_all_page_text(page):
    parts = []
    for frame in page.frames:
        try:
            parts.append(frame.locator("body").inner_text(timeout=5000))
        except Exception:
            continue
    return "\n".join(parts)


def wait_for_selector(page, selector, timeout_seconds=30):
    for _ in range(timeout_seconds):
        locator = find_locator(page, selector)
        if locator is not None:
            return locator
        page.wait_for_timeout(1000)
    return None


# ==================================================
# LOGIN AND CALENDAR
# ==================================================

def login_and_open_calendar(page):
    print("Opening driving school website...")
    page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2000)

    if find_locator(page, "#ddlWeeks") is not None:
        print("Calendar is already open.")
        return

    if not click_text(page, "ログインページへ"):
        raise RuntimeError("Login link not found (layout changed or site blocked).")

    student = wait_for_selector(page, "#txtKyoushuuseiNO")
    password = wait_for_selector(page, "#txtPassword")
    if student is None or password is None:
        raise RuntimeError("Login fields missing.")

    student.fill(CREDENTIALS["student_id"])
    password.fill(CREDENTIALS["password"])

    login_button = find_locator(page, "#btnAuthentication")
    if login_button is None:
        raise RuntimeError("Login button missing.")
    login_button.click(timeout=15000, no_wait_after=True)
    page.wait_for_timeout(3000)

    menu_button = wait_for_selector(page, "#btnMenu_Kyoushuuyoyaku")
    if menu_button is None:
        # Page text is deliberately NOT printed: public repo logs are visible to everyone.
        raise RuntimeError("Reservation menu missing. Login may have failed.")
    menu_button.click(timeout=15000, no_wait_after=True)
    page.wait_for_timeout(3000)

    if wait_for_selector(page, "#ddlWeeks") is None:
        raise RuntimeError("Calendar selector missing after opening reservations.")

    print("Lesson calendar opened successfully.")


# ==================================================
# AVAILABILITY DETECTION
# ==================================================

def extract_available_dates(text, start_date):
    found = set()
    pattern = re.compile(
        r"(?P<date>\d{4}\s*(?:/|-|年)\s*\d{1,2}"
        r"\s*(?:/|-|月)\s*\d{1,2}\s*日?)"
    )
    matches = list(pattern.finditer(text))

    for i, match in enumerate(matches):
        normalized = re.sub(r"\s+", "", match.group("date"))
        normalized = normalized.replace("年", "/").replace("月", "/").replace("日", "")

        parsed = None
        for fmt in ("%Y/%m/%d", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(normalized, fmt).date()
                break
            except ValueError:
                continue
        if parsed is None or not start_date <= parsed <= END_DATE:
            continue

        if i + 1 < len(matches):
            next_start = matches[i + 1].start()
        else:
            next_start = min(len(text), match.end() + 250)

        section = text[match.end():min(next_start, match.end() + 250)]
        if "空" in section:
            found.add(parsed.isoformat())

    return found


def scan_calendar(page, start_date):
    dropdown = find_locator(page, "#ddlWeeks")
    if dropdown is None:
        raise RuntimeError("Calendar selector disappeared.")

    options = dropdown.locator("option").all()
    if not options:
        raise RuntimeError("Calendar has no week options.")

    weeks_to_check = []
    for option in options:
        label = option.inner_text().strip()
        value = option.get_attribute("value")
        match = re.search(r"(\d{4})\s*/\s*(\d{1,2})\s*/\s*(\d{1,2})", label)
        if not match or value is None:
            continue
        week_start = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        week_end = week_start + timedelta(days=6)
        if week_end < start_date or week_start > END_DATE:
            continue
        weeks_to_check.append((label, value))

    print("Relevant weeks to scan:", len(weeks_to_check))
    if not weeks_to_check:
        raise RuntimeError("No relevant calendar weeks found.")

    available_dates = set()
    for label, value in weeks_to_check:
        dropdown = find_locator(page, "#ddlWeeks")
        if dropdown is None:
            raise RuntimeError("Calendar selector disappeared during scanning.")
        dropdown.select_option(value=value)
        page.wait_for_timeout(1200)

        page_text = get_all_page_text(page)
        if not page_text.strip():
            raise RuntimeError("Calendar returned no readable page text.")

        available_dates.update(extract_available_dates(page_text, start_date))
        print("Checked week:", label)

    return available_dates


def scan_with_recovery(page):
    last_error = None
    for attempt in range(1, SCAN_RETRIES + 2):
        try:
            print(f"Scan attempt {attempt} of {SCAN_RETRIES + 1}...")
            if find_locator(page, "#ddlWeeks") is None:
                login_and_open_calendar(page)
            return scan_calendar(page, today_jst())
        except Exception as error:
            last_error = error
            print(f"Attempt {attempt} failed:", error)
            if attempt <= SCAN_RETRIES:
                page.wait_for_timeout(2000)
                try:
                    login_and_open_calendar(page)
                except Exception as recovery_error:
                    print("Recovery failed:", recovery_error)
    raise RuntimeError(f"All scan attempts failed. Last error: {last_error}")


# ==================================================
# MAIN
# ==================================================

def main():
    run_until = time.monotonic() + RUN_MINUTES * 60
    previously_alerted = load_alerted()
    consecutive_failures = 0
    error_alert_sent = False

    if NOTIFY_START:
        send_telegram(
            "✅ Driving school monitor started on GitHub Actions.\n"
            f"Monitoring through {END_DATE.isoformat()}."
        )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()

        try:
            try:
                login_and_open_calendar(page)
            except Exception as error:
                print("Initial login failed (will retry in loop):", error)

            while time.monotonic() < run_until:
                scan_started = time.monotonic()

                if today_jst() > END_DATE:
                    print("Monitoring end date has passed.")
                    send_telegram("ℹ️ Driving school monitor finished: end date passed.")
                    break

                print("\nScan time (JST):", datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"))

                try:
                    available = scan_with_recovery(page)

                    if consecutive_failures > 0 and error_alert_sent:
                        send_telegram("✅ Driving school monitor recovered.")
                    consecutive_failures = 0
                    error_alert_sent = False

                    new_dates = available - previously_alerted
                    # Forget dates that are no longer open, so a re-opened slot alerts again.
                    previously_alerted &= available

                    if new_dates:
                        dates_sorted = sorted(new_dates)
                        print("NEW AVAILABILITY DETECTED:", dates_sorted)
                        message = (
                            "🚨 Driving school availability detected!\n\n"
                            + "\n".join(dates_sorted)
                            + "\n\nPlease open the booking website and verify."
                        )
                        if send_telegram(message):
                            previously_alerted |= new_dates
                    else:
                        print("No new available dates detected.")

                    save_alerted(previously_alerted)

                except Exception as error:
                    consecutive_failures += 1
                    print("SCAN FAILURE", consecutive_failures, ":", error)

                    if consecutive_failures >= FAILURES_BEFORE_ALERT and not error_alert_sent:
                        message = (
                            "⚠️ Driving school monitor needs attention.\n\n"
                            "Repeated scans failed, even after retries.\n"
                            f"Error: {str(error)[:500]}\n\n"
                            "The site may be down, blocking GitHub's servers, "
                            "or its layout/login may have changed."
                        )
                        if send_telegram(message):
                            error_alert_sent = True

                elapsed = time.monotonic() - scan_started
                time.sleep(max(0, CHECK_INTERVAL_SECONDS - elapsed))

        finally:
            browser.close()


if __name__ == "__main__":
    main()
