import os
from supabase import create_client, Client
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise EnvironmentError(
        "SUPABASE_URL and SUPABASE_KEY must be set in .env or environment variables."
    )

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


def push_to_supabase(products: list[dict], pincode: str):
    if not products:
        print("[DB] No products to insert.")
        return

    date_str = datetime.now().strftime("%Y-%m-%d")

    rows = [
        {
            "pincode":      p["Pincode"],
            "product_name": p["Product Name"],
            "quantity":     p["Quantity"],
            "sale_price":   p["Sale Price"],
            "mrp":          p["MRP"],
            "scraped_date": date_str,
        }
        for p in products
    ]

    # Deduplicate by unique constraint key before sending to Supabase
    seen = set()
    deduped = []
    for row in rows:
        key = (row["pincode"], row["product_name"], row["quantity"], row["scraped_date"])
        if key not in seen:
            seen.add(key)
            deduped.append(row)

    print(f"[DB] {len(rows)} rows scraped → {len(deduped)} unique "
          f"({len(rows) - len(deduped)} duplicates removed)")

    chunk_size = 500
    total_upserted = 0
    for i in range(0, len(deduped), chunk_size):
        chunk = deduped[i : i + chunk_size]
        supabase.table("blinkit_products").upsert(
            chunk,
            on_conflict="pincode,product_name,quantity,scraped_date"
        ).execute()
        total_upserted += len(chunk)
        print(f"[DB] Upserted chunk {i // chunk_size + 1}: {len(chunk)} rows")

    print(f"[DB] ✅ Total upserted: {total_upserted} rows into Supabase")