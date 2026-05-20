# scraper.py — Blinkit Scraper (Fixed: correct location cookies + inner scroll)

from playwright.sync_api import sync_playwright
import pandas as pd
import os
import json
from datetime import datetime

# ── XPaths ────────────────────────────────────────────────────────────────────
LOCATION_TRIGGER_XPATH = "/html/body/div[1]/div/div/div[1]/header/div[1]/div[2]/div"
PINCODE_INPUT_XPATH    = "/html/body/div[1]/div/div/div[1]/header/div[2]/div[2]/div/div/div/div/div/div[2]/div[2]/div/div/div/input"
FIRST_SUGGEST_XPATH    = "/html/body/div[1]/div/div/div[1]/header/div[2]/div[2]/div/div/div[2]/div/div/div[1]"
INNER_SCROLL_XPATH     = "/html/body/div[1]/div/div/div[3]/div/div/div[2]/div[1]/div/div/div[2]/div/div[2]"
CATEGORY_URL           = "https://blinkit.com/cn/fresh-vegetables/cid/1487/1489"

# ── Timing ────────────────────────────────────────────────────────────────────
WAIT_GOTO        = 6000
WAIT_TRIGGER     = 3000
WAIT_FILL        = 4000
WAIT_SUGGEST     = 8000   # extra wait so Blinkit saves location properly
WAIT_NAV         = 6000
SCROLL_STEP_PX   = 400
SCROLL_PAUSE_MS  = 800
SCROLL_MAX_STEPS = 200

BLOCK_RESOURCES  = {"image", "media", "font"}


# ── Helpers ───────────────────────────────────────────────────────────────────
def clean(v):
    return " ".join(str(v).split()).strip() if v else ""


def fmt_price(val):
    if val is None:
        return ""
    try:
        v = float(val)
        return f"₹{int(v)}" if v == int(v) else f"₹{v}"
    except Exception:
        return str(val)


# ── Read location cookies set by Blinkit after suggestion click ───────────────
def extract_location_cookies(context):
    """
    After clicking a location suggestion, Blinkit sets:
      gr_1_lat, gr_1_lon, gr_1_locality, gr_1_landmark, city
    We read and save these so restore_session can inject them exactly.
    """
    cookies = context.cookies()
    loc_keys = {"gr_1", "gr_1_lat", "gr_1_lon", "gr_1_locality",
                "gr_1_landmark", "city", "pincode", "user_pincode"}
    loc_cookies = [c for c in cookies if c["name"] in loc_keys]
    print(f"  [LOC COOKIES] Found: {[c['name']+'='+c['value'] for c in loc_cookies]}")
    return cookies  # return all cookies


# ── Set location (ALWAYS run fresh — delete session.json to re-run) ───────────
def set_location(page, context, pincode):
    print("[LOC] Opening category page...")
    page.goto(CATEGORY_URL, wait_until="networkidle", timeout=90000)
    page.wait_for_timeout(WAIT_GOTO)

    print("[LOC] Clicking location trigger...")
    page.locator(f"xpath={LOCATION_TRIGGER_XPATH}").click(timeout=15000)
    page.wait_for_timeout(WAIT_TRIGGER)

    print("[LOC] Finding pincode input...")
    inp = None
    for sel in [
        f"xpath={PINCODE_INPUT_XPATH}",
        'input[placeholder*="pincode" i]',
        'input[placeholder*="search" i]',
        'input[placeholder*="Enter" i]',
        'input[type="text"]',
    ]:
        try:
            loc = page.locator(sel).first if not sel.startswith("xpath=") else page.locator(sel)
            loc.wait_for(timeout=10000, state="visible")
            inp = loc
            print(f"[LOC] Input found: {sel}")
            break
        except Exception:
            continue

    if inp is None:
        raise RuntimeError("[LOC] Pincode input not found!")

    inp.click(force=True)
    page.wait_for_timeout(500)
    inp.fill(str(pincode))
    print(f"[LOC] Typed: {pincode}")
    page.wait_for_timeout(WAIT_FILL)

    print("[LOC] Clicking first suggestion...")
    clicked = False
    for sel in [
        f"xpath={FIRST_SUGGEST_XPATH}",
        "div[data-testid='location-search-result']",
        "li[class*='suggestion']",
        "div[class*='SearchList'] > div",
        "div[class*='LocationSearch'] div",
    ]:
        try:
            loc = page.locator(sel).first if not sel.startswith("xpath=") else page.locator(sel)
            loc.wait_for(timeout=8000, state="visible")
            loc.click(timeout=8000)
            print(f"[LOC] Suggestion clicked: {sel}")
            clicked = True
            break
        except Exception:
            continue

    if not clicked:
        page.keyboard.press("Enter")
        print("[LOC] Pressed Enter as fallback")

    # !! Critical: wait long enough for Blinkit to write location cookies !!
    page.wait_for_timeout(WAIT_SUGGEST)

    # Read ALL cookies AFTER suggestion click — includes gr_1_lat, gr_1_lon etc.
    cookies = extract_location_cookies(context)

    # Read localStorage AFTER location is confirmed
    storage = page.evaluate(
        "()=>{let d={};for(let i=0;i<localStorage.length;i++){let k=localStorage.key(i);d[k]=localStorage.getItem(k);}return d;}"
    )

    session = {"cookies": cookies, "storage": storage}
    with open("session.json", "w") as f:
        json.dump(session, f)
    print(f"[LOC] Session saved ({len(cookies)} cookies)")
    return session


# ── Restore session WITH correct location cookies ─────────────────────────────
def restore_session(page, context, session, pincode):
    """
    Injects ALL cookies from the saved session (including gr_1_lat, gr_1_lon,
    gr_1_locality, gr_1_landmark, city) BEFORE navigating — so the first API
    call already carries the correct location context.
    """
    # Clear existing cookies first
    context.clear_cookies()

    # Re-inject saved cookies (these include the real location cookies)
    context.add_cookies(session["cookies"])

    print(f"  [SES] Re-injected {len(session['cookies'])} cookies")

    # Navigate to category — API calls from this point will use correct location
    page.goto(CATEGORY_URL, wait_until="networkidle", timeout=90000)
    page.wait_for_timeout(3000)

    # Restore localStorage entries
    for k, v in session["storage"].items():
        try:
            page.evaluate(f"localStorage.setItem({json.dumps(k)}, {json.dumps(v)})")
        except Exception:
            pass

    # Hard-reload so Blinkit picks up the cookies from the start
    page.reload(wait_until="networkidle", timeout=90000)
    page.wait_for_timeout(3000)

    print("[SES] Session restored with correct location!")


# ── API response parser ───────────────────────────────────────────────────────
def parse_api_response(body_text, pincode, collected):
    try:
        data = json.loads(body_text)
    except Exception:
        return

    added = 0

    def build_quantity(obj):
        for key in ["pack_size", "weight", "unit_display", "display_unit", "display_qty"]:
            if isinstance(obj.get(key), str) and obj[key].strip():
                return clean(obj[key])
        qty_val = obj.get("quantity") or obj.get("qty") or obj.get("default_qty") or 1
        unit = (
            obj.get("unit") or obj.get("uom") or
            obj.get("unit_type") or obj.get("unit_name") or ""
        )
        try:
            f = float(qty_val)
            qty_val = int(f) if f == int(f) else f
        except Exception:
            return clean(str(qty_val))
        unit = str(unit).strip()
        return f"{qty_val} {unit}" if unit else str(qty_val)

    def extract_price_fields(obj):
        price_block = obj.get("price")
        sale = mrp = None
        if isinstance(price_block, dict):
            sale = (price_block.get("value") or price_block.get("sale_price")
                    or price_block.get("offer_price"))
            mrp  = (price_block.get("mrp") or price_block.get("mrp_value")
                    or price_block.get("list_price"))
        if sale is None:
            sale = (obj.get("sale_price") or obj.get("offer_price")
                    or obj.get("price") or obj.get("sp"))
        if mrp is None:
            mrp = obj.get("mrp") or obj.get("market_price") or sale
        return sale, mrp

    def get_name(obj):
        return (obj.get("name") or obj.get("product_name") or obj.get("title")
                or obj.get("display_name") or obj.get("item_name"))

    def get_id(obj):
        return (obj.get("product_id") or obj.get("id")
                or obj.get("item_id") or obj.get("sku_id"))

    def walk(obj):
        nonlocal added
        if isinstance(obj, list):
            for item in obj:
                walk(item)
            return
        if not isinstance(obj, dict):
            return

        name_raw = get_name(obj)
        sale_raw, mrp_raw = extract_price_fields(obj)

        if name_raw and sale_raw is not None:
            oos = obj.get("is_out_of_stock") or obj.get("out_of_stock") or obj.get("oos")
            if not oos:
                name    = clean(name_raw)
                qty_str = build_quantity(obj)
                sale    = fmt_price(sale_raw)
                mrp     = fmt_price(mrp_raw)
                pid     = get_id(obj)
                key     = ("id", str(pid)) if pid else ("nk", name.lower(), qty_str.lower())

                if key not in collected and name:
                    collected[key] = {
                        "Pincode":      pincode,
                        "Product Name": name,
                        "Quantity":     qty_str,
                        "Sale Price":   sale,
                        "MRP":          mrp,
                    }
                    added += 1

        if isinstance(obj.get("product"), dict):
            walk(obj["product"])
        for v in obj.values():
            if isinstance(v, (dict, list)):
                walk(v)

    walk(data)
    if added:
        print(f"  [API] +{added} products | total: {len(collected)}")


# ── Scroll inner container ────────────────────────────────────────────────────
def scroll_inner_container(page, collected):
    try:
        container = page.locator(f"xpath={INNER_SCROLL_XPATH}")
        container.wait_for(timeout=15000, state="visible")
        print("[SCROLL] Inner container found, scrolling...")
    except Exception:
        print("[SCROLL] ⚠️  Inner container not found — falling back to PageDown")
        _fallback_scroll(page, collected)
        return

    container.evaluate("el => { el.scrollTop = 0; }")
    page.wait_for_timeout(800)

    prev_len = 0
    stagnant = 0

    for i in range(1, SCROLL_MAX_STEPS + 1):
        container.evaluate(
            "(el, step) => { el.scrollTop += step; }",
            SCROLL_STEP_PX,
        )
        page.wait_for_timeout(SCROLL_PAUSE_MS)

        inner_top = container.evaluate("el => el.scrollTop")
        cur_len   = len(collected)
        print(f"  [SCROLL] step {i:03d} | scrollTop:{inner_top} | products:{cur_len}")

        stagnant = stagnant + 1 if cur_len == prev_len else 0
        if cur_len != prev_len:
            prev_len = cur_len

        if stagnant >= 15:
            print("  [SCROLL] ✅ Bottom confirmed (15 stagnant steps).")
            break


def _fallback_scroll(page, collected):
    prev_len = 0
    stagnant = 0
    for i in range(1, 120 + 1):
        page.keyboard.press("PageDown")
        page.wait_for_timeout(SCROLL_PAUSE_MS)
        cur_len = len(collected)
        print(f"  [SCROLL-FB] step {i:03d} | products:{cur_len}")
        stagnant = stagnant + 1 if cur_len == prev_len else 0
        if cur_len != prev_len:
            prev_len = cur_len
        if stagnant >= 15:
            break


# ── Main ──────────────────────────────────────────────────────────────────────
def scrape_blinkit(pincode):
    collected = {}

    def on_response(response):
        url = response.url
        if "blinkit.com" not in url and "grofers.com" not in url:
            return
        if "json" not in response.headers.get("content-type", ""):
            return
        try:
            body = response.body()
            if len(body) > 100:
                parse_api_response(body.decode("utf-8", errors="ignore"), pincode, collected)
        except Exception:
            pass

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-extensions",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ]
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1400, "height": 900},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            geolocation={"latitude": 22.5726, "longitude": 88.3639},
            permissions=["geolocation"],
        )
        page = context.new_page()
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page.on("response", on_response)
        page.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in BLOCK_RESOURCES
            else route.continue_()
        )

        # ── Always set location fresh — session.json reuse caused wrong location ──
        # Delete session.json to force fresh location setting every run,
        # OR keep it and let restore_session inject the saved location cookies.
        if os.path.exists("session.json"):
            print("[SES] Reusing saved session...")
            with open("session.json") as f:
                session = json.load(f)
            restore_session(page, context, session, pincode)
        else:
            session = set_location(page, context, pincode)
            restore_session(page, context, session, pincode)

        # Navigate fresh to category AFTER cookies are set
        print(f"\n[NAV] {CATEGORY_URL}")
        page.goto(CATEGORY_URL, wait_until="networkidle", timeout=90000)
        page.wait_for_timeout(WAIT_NAV)
        print(f"  [API] After initial load: {len(collected)} products")

        # Scroll inner container
        scroll_inner_container(page, collected)

        print(f"\n[DONE] Total unique products: {len(collected)}")
        browser.close()

    return list(collected.values())


# ── Save ──────────────────────────────────────────────────────────────────────
def save_files(products, pincode):
    os.makedirs("output", exist_ok=True)
    date_str = datetime.now().strftime("%d%m%Y")
    fname    = f"blinkit_{pincode}_{date_str}"
    df = pd.DataFrame(products)
    df.to_csv( f"output/{fname}.csv",  index=False, encoding="utf-8-sig")
    df.to_json(f"output/{fname}.json", orient="records", indent=2, force_ascii=False)
    print(f"  CSV  -> output/{fname}.csv  ({len(df)} rows)")
    print(f"  JSON -> output/{fname}.json ({len(df)} rows)")