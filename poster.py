# poster.py — Reads blinkit JSON from output/ and POSTs to Bajaru API
# Debug mode can print the exact payload in GitHub Actions logs

import os
import json
import glob
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

API_BASE    = "https://api.bajaru.co.in"
API_PATH    = "/v1/catalog/ingest"
BATCH_SIZE  = 100
RETRY_MAX   = 3
RETRY_DELAY = 5

# DEBUG_POST_PAYLOAD=true  → print payload and still post
# DEBUG_ONLY=true          → print payload and skip posting
DEBUG_POST_PAYLOAD = os.environ.get("DEBUG_POST_PAYLOAD", "false").lower() == "true"
DEBUG_ONLY         = os.environ.get("DEBUG_ONLY", "false").lower() == "true"


def get_api_key() -> str:
    key = os.environ.get("BAJARU_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "BAJARU_API_KEY env var not set. Add it as a GitHub Secret."
        )
    return key


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "*" * (len(value) - 8) + value[-4:]


def parse_price(val):
    if val is None:
        return None
    try:
        return float(str(val).replace("₹", "").replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def map_product(raw: dict):
    name       = (raw.get("Product Name") or "").strip()
    quantity   = (raw.get("Quantity") or "").strip()
    sale_price = parse_price(raw.get("Sale Price"))
    mrp        = parse_price(raw.get("MRP"))

    if not name or sale_price is None or not quantity:
        return None

    if mrp is None:
        mrp = sale_price

    return {
        "productName":  name,
        "category":     "vegetables",
        "quantityUnit": quantity,
        "mrp":          round(mrp, 2),
        "sellingPrice": round(sale_price, 2),
        "inStock":      True,
        "productUrl":   raw.get("productUrl") or "",
        "imageUrl":     raw.get("imageUrl") or "",
        "description":  raw.get("description") or "",
    }


def build_batches(products: list):
    return [products[i:i + BATCH_SIZE] for i in range(0, len(products), BATCH_SIZE)]


def print_debug_payload(payload, api_key):
    # Ask GitHub Actions to mask the raw secret if it ever appears
    print(f"::add-mask::{api_key}")

    print("\n[DEBUG] Request URL:")
    print(f"{API_BASE}{API_PATH}")

    print("\n[DEBUG] Request Headers:")
    print(json.dumps({
        "Content-Type": "application/json",
        "x-api-key": mask_secret(api_key)
    }, indent=2, ensure_ascii=False))

    print("\n[DEBUG] Request Payload:")
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def post_batch(batch, source, scraped_at, api_key, batch_num, total_batches):
    payload = {
        "source":    source,
        "scrapedAt": scraped_at,
        "products":  batch,
    }

    if DEBUG_POST_PAYLOAD or DEBUG_ONLY:
        print(f"\n[DEBUG] Batch {batch_num}/{total_batches}")
        print_debug_payload(payload, api_key)

    if DEBUG_ONLY:
        print("\n[DEBUG] DEBUG_ONLY=true, skipping actual POST.")
        return {"debug": True, "skippedPost": True, "batchSize": len(batch)}

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    url  = f"{API_BASE}{API_PATH}"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
    }

    for attempt in range(1, RETRY_MAX + 1):
        print(
            f"  [POST] Batch {batch_num}/{total_batches} "
            f"({len(batch)} products) — attempt {attempt}/{RETRY_MAX}"
        )
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp_json = json.loads(resp.read().decode("utf-8"))
                print(
                    f"  [POST] ✅ {resp.getcode()} Accepted | "
                    f"processedCount={resp_json.get('processedCount', '?')}"
                )
                return resp_json

        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")
            if e.code == 400:
                raise RuntimeError(f"[POST] 400 Bad Request: {err_body}")
            if e.code == 401:
                raise RuntimeError("[POST] 401 Unauthorized — check BAJARU_API_KEY.")
            if e.code >= 500:
                print(f"  [POST] ⚠️  {e.code} Server Error — retrying in {RETRY_DELAY}s...")
                if attempt < RETRY_MAX:
                    time.sleep(RETRY_DELAY)
                    continue
                raise RuntimeError(f"[POST] Server error after {RETRY_MAX} attempts.")
            raise RuntimeError(f"[POST] HTTP {e.code}: {err_body}")

        except urllib.error.URLError as e:
            print(f"  [POST] ⚠️  Network error: {e.reason} — retrying in {RETRY_DELAY}s...")
            if attempt < RETRY_MAX:
                time.sleep(RETRY_DELAY)
                continue
            raise RuntimeError(f"[POST] Network error after {RETRY_MAX} attempts.")

    raise RuntimeError("[POST] Exhausted all retries.")


def post_json_file(json_path: str, source: str, api_key: str) -> int:
    print(f"\n[POSTER] Loading: {json_path}")
    with open(json_path, encoding="utf-8") as f:
        raw_products = json.load(f)

    print(f"  [POSTER] {len(raw_products)} raw rows")

    mapped, skipped = [], 0
    for item in raw_products:
        m = map_product(item)
        if m:
            mapped.append(m)
        else:
            skipped += 1

    print(f"  [POSTER] {len(mapped)} valid | {skipped} skipped")

    if not mapped:
        print("  [POSTER] ⚠️  No valid products to post. Skipping.")
        return 0

    scraped_at    = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    batches       = build_batches(mapped)
    total_batches = len(batches)
    total_posted  = 0

    print(f"  [POSTER] Sending {total_batches} batch(es)...")

    for i, batch in enumerate(batches, start=1):
        post_batch(
            batch=batch,
            source=source,
            scraped_at=scraped_at,
            api_key=api_key,
            batch_num=i,
            total_batches=total_batches,
        )
        total_posted += len(batch)
        if i < total_batches:
            time.sleep(0.5)

    print(f"  [POSTER] ✅ {total_posted} products handled for '{source}'")
    return total_posted


def find_latest_json(platform: str, output_dir: str = "output"):
    pattern = os.path.join(output_dir, f"{platform}_*.json")
    files   = sorted(glob.glob(pattern), reverse=True)
    return files[0] if files else None


def run(output_dir: str = "output"):
    api_key   = get_api_key()
    json_path = find_latest_json("blinkit", output_dir)

    print("=" * 60)
    print("   Bajaru API Poster — Blinkit")
    print("=" * 60)
    print(f"  DEBUG_POST_PAYLOAD : {DEBUG_POST_PAYLOAD}")
    print(f"  DEBUG_ONLY         : {DEBUG_ONLY}")
    print("=" * 60)

    if not json_path:
        print(f"[POSTER] ❌ No blinkit JSON found in {output_dir}/")
        raise SystemExit(1)

    count = post_json_file(json_path, "blinkit", api_key)

    print("\n" + "=" * 60)
    print(f"  RESULT: {count} products processed ✅")
    print("=" * 60)


if __name__ == "__main__":
    run()