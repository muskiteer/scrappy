# main.py — Blinkit only
import os
from scraper import scrape_blinkit, save_files

PINCODE   = os.environ.get("PINCODE", "700112")
IS_DOCKER = os.environ.get("IS_DOCKER", "false").lower() == "true"

print("=" * 60)
print("   Veggie Price Engine — Blinkit Scraper")
print("=" * 60)
print(f"  Pincode  : {PINCODE}")
print(f"  Headless : {IS_DOCKER}")
print("=" * 60)

products = scrape_blinkit(PINCODE)
print(f"\n  Total in-stock products: {len(products)}")

if products:
    print("\n  Sample (first 5):")
    for p in products[:5]:
        print(f"    {p['Product Name']} | {p['Quantity']} | {p['Sale Price']} | MRP:{p['MRP']}")
    print()
    save_files(products, PINCODE)
else:
    print("\n  No products found.")

print("\nDone! Check output/ folder.")
print("=" * 60)