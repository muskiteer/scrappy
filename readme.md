# Blinkit Scraper Workflow Guide

This document explains exactly how the Blinkit scraper project works, what each file does, how the data flows through the system, and how the Docker + GitHub Actions automation is intended to run. It is written as a technical workflow guide for a human maintainer or an LLM that needs to understand the codebase quickly.

## 1. Project Goal

The project scrapes Blinkit’s fresh vegetables category for a fixed pincode, extracts product data from Playwright-intercepted API responses, normalizes the result into JSON-like records, deduplicates the records, and stores them in Supabase. The current deployment model is container-based, so the scraper runs the same way locally and in GitHub Actions.

The scraper is designed to:

- Open Blinkit in a browser automation context.
- Set and persist the delivery location for a specific pincode.
- Visit the fresh vegetables category page.
- Intercept API responses instead of relying only on DOM parsing.
- Extract products, quantities, sale prices, and MRPs.
- Remove duplicates.
- Push the final rows into Supabase.

## 2. Repository Structure

A typical structure for this project is:

```text
blinkit-scraper/
├── main.py
├── scraper.py
├── db.py
├── requirements.txt
├── Dockerfile
├── .env                # local only, not committed
├── .env.example
├── session.json        # optional, may be committed if you want stable location behavior
├── .github/
│   └── workflows/
│       ├── build.yml
│       └── cron.yml
└── output/             # removed if you only push to Supabase
```

### File responsibilities

- `main.py`: orchestration entry point.
- `scraper.py`: Playwright browser automation and data extraction.
- `db.py`: Supabase connection and row insertion/upsert logic.
- `Dockerfile`: creates the reproducible runtime.
- `build.yml`: validates and rebuilds on code push.
- `cron.yml`: scheduled runtime for 2:00 AM IST.

## 3. End-to-End Flow

At a high level the project runs in this order:

1. `main.py` starts.
2. `scrape_blinkit(PINCODE)` is called.
3. Playwright opens Blinkit and restores the location/session state.
4. The category page loads.
5. The page’s JSON/API responses are intercepted.
6. `parse_api_response()` extracts products from response payloads.
7. `scrape_blinkit()` returns the final deduplicated list of products.
8. `push_to_supabase()` transforms the records for the database.
9. The records are deduplicated again against the unique key.
10. Supabase `upsert()` stores or updates the rows.

If the project still includes file saving, the flow also writes CSV/JSON locally, but your current direction is to keep it Supabase-only.

## 4. `main.py` Orchestration

`main.py` is the top-level driver. It does not scrape anything itself; it only coordinates the scraper and database layer.

A simplified version looks like this:

```python
from scraper import scrape_blinkit
from db import push_to_supabase

PINCODE = "700112"

products = scrape_blinkit(PINCODE)
print(f"Total in-stock products: {len(products)}")

if products:
    push_to_supabase(products, PINCODE)
else:
    print("No products found.")
```

### What this file is responsible for

- Defining the target pincode.
- Starting the scrape.
- Receiving the scraped list.
- Passing the list to Supabase storage.
- Printing summary logs.

### Why keep it small

`main.py` should stay thin because the scraping and storage logic already live in specialized modules. This makes debugging easier and makes the pipeline more maintainable.

## 5. `scraper.py` Internal Flow

This is the core of the project. It handles browser automation, session restore, location setup, API interception, product parsing, and deduplication.

### 5.1 Playwright launch

The browser is started in headless mode for container execution:

```python
browser = p.chromium.launch(
    headless=True,
    args=[
        "--disable-blink-features=AutomationControlled",
        "--disable-extensions",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
    ]
)
```

#### Why these flags matter

- `headless=True`: required for container and CI automation.
- `--no-sandbox`: common requirement in Docker.
- `--disable-dev-shm-usage`: avoids Chromium crashes in constrained shared memory.
- `--disable-gpu`: unnecessary GPU support in headless Linux.
- anti-detection flags reduce obvious automation fingerprints.

### 5.2 Browser context setup

The browser context is configured with:

- user agent
- viewport
- locale
- timezone
- geolocation
- permissions

This is intended to make Blinkit believe the session is a normal desktop browser in a Kolkata-like location.

```python
context = browser.new_context(
    user_agent="Mozilla/5.0 ... Chrome/124.0.0.0 Safari/537.36",
    viewport={"width": 1400, "height": 900},
    locale="en-IN",
    timezone_id="Asia/Kolkata",
    geolocation={"latitude": 22.5726, "longitude": 88.3639},
    permissions=["geolocation"],
)
```

### 5.3 Init script

The script removes the obvious `navigator.webdriver` signal:

```python
page.add_init_script(
    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
)
```

This is a standard anti-detection trick so that the site is less likely to treat the session as automated.

### 5.4 Response interception

The scraper listens for responses before navigation starts:

```python
page.on("response", on_response)
```

The `on_response()` function checks whether the response is from Blinkit and whether it looks like JSON. If so, it sends the body to the parser.

This is the most important part of the scraper. Rather than depending only on visible page elements, the scraper extracts data from the structured API payloads that the page receives in the background.

### 5.5 Resource blocking

Only heavy resources are blocked:

```python
BLOCK_RESOURCES = {"image", "media", "font"}
```

And then:

```python
page.route(
    "**/*",
    lambda route: route.abort()
    if route.request.resource_type in BLOCK_RESOURCES
    else route.continue_()
)
```

This keeps the page fast while still allowing scripts and stylesheets that trigger API requests.

## 6. Location and Session Handling

Blinkit’s location state is critical. The same pincode can lead to different store availability depending on the selected delivery locality and session cookies.

### 6.1 `session.json`

The session file stores cookies and localStorage data. It is used to preserve the location state across runs.

Typical restore logic:

```python
if os.path.exists("session.json"):
    with open("session.json") as f:
        session = json.load(f)
    restore_session(page, context, session, pincode)
else:
    session = set_location(page, context, pincode)
    restore_session(page, context, session, pincode)
```

### 6.2 `set_location()` flow

This function:

1. Opens the category page.
2. Clicks the location trigger.
3. Finds the pincode input.
4. Fills the pincode.
5. Clicks the first suggestion.
6. Saves cookies and localStorage to `session.json`.

A simplified portion:

```python
page.goto(CATEGORY_URL, wait_until="networkidle", timeout=90000)
page.locator(f"xpath={LOCATION_TRIGGER_XPATH}").click(timeout=15000)
inp.fill(str(pincode))
page.keyboard.press("Enter")
```

### 6.3 Why location matters

If the session is missing or stale, Blinkit can resolve the wrong dark store or the wrong locality. That is why location state often determines whether the result set is correct.

## 7. Parsing API Responses

`parse_api_response()` is the extraction engine. It receives a raw JSON string, converts it to Python data, and recursively walks through nested dictionaries and lists.

### 7.1 Input shape

The JSON may have multiple shapes, such as:

- direct product objects
- objects nested under `product`
- objects with nested `price` blocks
- mixed response arrays

The parser is designed to cope with all of that.

### 7.2 Product detection

A product-like object is identified by a name field and a price field:

```python
name_raw = get_name(obj)
sale_raw, mrp_raw = extract_price_fields(obj)
```

If a name exists and a sale price exists, the object is treated as a candidate product.

### 7.3 Quantity logic

The `build_quantity()` helper tries to produce a readable quantity string such as `1 kg` or `250 g`.

Example logic:

```python
qty_val = obj.get("quantity") or obj.get("qty") or obj.get("default_qty") or 1
unit = obj.get("unit") or obj.get("uom") or obj.get("unit_type") or obj.get("unit_name") or ""
```

If a direct display field exists, it uses that first.

### 7.4 Price formatting

Prices are normalized into a display string:

```python
def fmt_price(val):
    if val is None:
        return ""
    try:
        v = float(val)
        if v == int(v):
            return f"₹{int(v)}"
        return f"₹{v}"
    except Exception:
        return str(val)
```

This means a raw value like `21.0` becomes `₹21`.

### 7.5 Dedup key generation

The parser avoids duplicate product entries by building a key such as:

```python
if pid:
    key = ("id", str(pid))
else:
    key = ("nk", name.lower(), qty_str.lower())
```

This means:

- if a stable product id exists, use it
- otherwise fallback to normalized name + quantity

### 7.6 Product object created

The final parsed object is stored as:

```python
{
    "Pincode": pincode,
    "Product Name": name,
    "Quantity": qty_str,
    "Sale Price": sale,
    "MRP": mrp,
}
```

## 8. Final Scraper Result

At the end of `scrape_blinkit()`, the collected dictionary values are converted to a list and returned.

A simplified version:

```python
return list(collected.values())
```

This result is what `main.py` sends to `push_to_supabase()`.

### Why collection is a dict first

Using a dict keyed by product identity ensures in-memory deduplication before DB insertion. It prevents repeated items from the same or overlapping API responses.

## 9. `db.py` Supabase Flow

`db.py` is responsible for turning the scraped product list into rows and pushing them into Supabase.

### 9.1 Environment loading

It uses `python-dotenv` locally:

```python
from dotenv import load_dotenv
load_dotenv()
```

Then reads:

```python
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
```

If either is missing, it raises an error immediately.

### 9.2 Client creation

```python
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
```

This client is used for all table operations.

### 9.3 Row normalization

Each scraped product is converted into a database row:

```python
rows = [
    {
        "pincode": p["Pincode"],
        "product_name": p["Product Name"],
        "quantity": p["Quantity"],
        "sale_price": p["Sale Price"],
        "mrp": p["MRP"],
        "scraped_date": date_str,
    }
    for p in products
]
```

This step maps the scraper’s JSON structure into the database schema.

### 9.4 Dedup before DB insert

The project also deduplicates again before hitting Supabase:

```python
seen = set()
deduped = []
for row in rows:
    key = (row["pincode"], row["product_name"], row["quantity"], row["scraped_date"])
    if key not in seen:
        seen.add(key)
        deduped.append(row)
```

This is important because even if the scraper returns duplicates, the database insert should still be safe.

### 9.5 Upsert

Final storage uses:

```python
supabase.table("blinkit_products").upsert(
    chunk,
    on_conflict="pincode,product_name,quantity,scraped_date"
).execute()
```

This means:

- new rows are inserted
- existing rows with the same unique key are updated

For this to work, the table must have a matching unique index.

## 10. Supabase Table Design

A typical schema is:

```sql
create table public.blinkit_products (
  id bigserial primary key,
  pincode text not null,
  product_name text not null,
  quantity text,
  sale_price text,
  mrp text,
  scraped_date date not null default current_date,
  created_at timestamptz default now()
);
```

And the unique index:

```sql
create unique index idx_blinkit_unique
on public.blinkit_products (pincode, product_name, quantity, scraped_date);
```

### Why this index is necessary

The `upsert(..., on_conflict=...)` call requires a unique or exclusion constraint that matches the conflict columns. Without it, Postgres cannot decide which row counts as the conflict target.

## 11. Docker Build Flow

The Dockerfile packages the scraper into a reproducible image.

### Why Docker is useful here

Browser automation needs:

- Python runtime
- Playwright package
- Chromium browser binary
- Linux libraries for browser execution

Docker ensures all of that exists inside one image.

### Common Docker flow

```dockerfile
FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y ...
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN useradd -ms /bin/bash scraper
RUN su scraper -c "playwright install chromium"
COPY scraper.py main.py db.py session.json ./
USER scraper
CMD ["python", "main.py"]
```

### Why install Chromium as the non-root user

Playwright stores browser binaries in the current user’s cache directory. If Chromium is installed as root, the browser may end up in `/root/.cache`, but the runtime user may be `scraper`, which expects `/home/scraper/.cache`. Installing it as the same user avoids the “Executable doesn’t exist” error.

## 12. GitHub Actions Flow

There are two workflows:

### 12.1 `build.yml`

This validates the Docker build on push or pull request.

Typical behavior:

- checkout repo
- setup Docker Buildx
- build image

Purpose:

- catch Dockerfile errors early
- ensure changes don’t break the image

### 12.2 `cron.yml`

This runs the scraper on a schedule.

Example schedule:

```yaml
schedule:
  - cron: "30 20 * * *"
```

That corresponds to 2:00 AM IST.

Typical cron job steps:

- checkout repo
- build image
- run image
- inject Supabase secrets

### What happens on every cron run

If the workflow is written to build then run, GitHub Actions will rebuild the image on each scheduled run. The runner is ephemeral, so it does not keep the image between runs unless you push it to a registry.

## 13. Environment and Secrets

### Local development

Use `.env` locally:

```env
SUPABASE_URL=your_url
SUPABASE_KEY=your_key
```

### GitHub Actions

Use repository secrets:

- `SUPABASE_URL`
- `SUPABASE_KEY`

### What not to commit

- `.env`
- `venv/`
- `__pycache__/`
- `output/`
- any generated file you don’t want in Git

A `.env.example` file is fine because it documents required variables without revealing secrets.

## 14. Important Failure Modes

### Playwright browser missing

If Chromium is not installed for the correct user, the container fails at browser launch.

### Wrong location state

If `session.json` is missing or stale, Blinkit can resolve the wrong store or wrong location.

### Supabase table missing

If `blinkit_products` is deleted or not in `public`, the API call fails.

### Unique index missing

If the `on_conflict` target does not match a unique index, upsert fails.

### Duplicate rows inside one batch

If the scraper returns duplicate rows with the same conflict key, Postgres may reject the upsert batch. That is why deduplication happens before insertion.

## 15. Recommended Final Behavior

For the cleanest version of this project:

- keep `main.py` minimal
- keep `scraper.py` responsible for scrape + parsing
- keep `db.py` responsible for Supabase only
- remove local CSV/JSON output if you no longer need it
- use Docker for runtime consistency
- use GitHub Actions build + cron for automation

That gives you a maintainable pipeline where the browser automation runs consistently and only the database remains as the durable output.