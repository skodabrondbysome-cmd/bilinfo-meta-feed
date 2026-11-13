#!/usr/bin/env python3
"""
bilinfo_to_meta_feed.py

Pulls the dealer's current stock from the Bilinfo Listing API and writes a CSV
in Meta's vehicle-catalog format (the same columns as the file that is in the
"Škoda Brøndby - Brugte biler" catalog today). Meta fetches the CSV on a
schedule, so the carousel follows the real stock: sold cars drop out, new cars
appear.

Credentials are read from environment variables, never from the command line:
    BILINFO_USER, BILINFO_PASSWORD

Usage:
    python bilinfo_to_meta_feed.py --out meta_vehicle_feed.csv
    python bilinfo_to_meta_feed.py --sample-json export.json --out test.csv   # offline test
    python bilinfo_to_meta_feed.py --dump-raw raw.json ...                    # save the API response for debugging

Field names in the Bilinfo response follow developer.bilinfo.net (Listing API,
export endpoint). Where the docs were unclear (pictures, colour, VIN, detail
URL) the script tries several likely keys; check the first run's summary.
"""

import argparse
import csv
import json
import os
import re
import sys
from pathlib import Path

import requests

EXPORT_URL = "https://gw.bilinfo.net/listingapi/api/export"

# The vehicle page on the dealer site. {id} is filled with the value chosen by
# --url-id-field (default: VehicleSourceId). Verify against a real car before
# switching the catalog over; override with --url-template if needed.
DEFAULT_URL_TEMPLATE = "https://www.skoda-brondby.dk/Brugte-biler/Vis/?id={id}"

MAX_IMAGES = 10

DEFAULT_ADDRESS = {
    "address.addr1": "Vallensbækvej 5",
    "address.city": "Brøndby",
    "address.region": "Hovedstaden",
    "address.postal_code": "2605",
    "address.country": "Denmark",
}

FIELDNAMES = (
    ["vehicle_id", "title", "description", "url"]
    + [f"image[{i}].url" for i in range(MAX_IMAGES)]
    + ["price", "make", "model", "trim", "year", "mileage.value", "mileage.unit",
       "fuel_type", "transmission", "exterior_color", "body_style", "vin",
       "state_of_vehicle", "condition", "availability", "vehicle_type"]
    + list(DEFAULT_ADDRESS.keys())
)


# ---------------------------------------------------------------- helpers

def first(d, *keys, default=""):
    """Return the first non-empty value among keys (case-insensitive)."""
    lower = {k.lower(): v for k, v in d.items()}
    for k in keys:
        v = lower.get(k.lower())
        if v not in (None, "", [], {}):
            return v
    return default


def digits(value):
    """'349.900' / '349900,00' / 349900 -> '349900'."""
    if value in (None, ""):
        return ""
    s = str(value).strip()
    s = re.split(r"[,]\d{1,2}$", s)[0]  # drop Danish decimal part
    return re.sub(r"[^\d]", "", s)


def map_fuel(propellant):
    p = (propellant or "").lower()
    if "plug" in p or "pluginhybrid" in p.replace("-", "").replace(" ", ""):
        return "PLUGIN_HYBRID"
    if "hybrid" in p:
        return "HYBRID"
    if p.startswith("el") or "elektr" in p or p == "electric":
        return "ELECTRIC"
    if "diesel" in p:
        return "DIESEL"
    if "benzin" in p or "petrol" in p:
        return "PETROL"
    return "OTHER" if p else ""


def map_transmission(gear):
    g = (gear or "").strip().lower()
    if g.startswith("a") or "auto" in g:
        return "AUTOMATIC"
    if g.startswith("m"):
        return "MANUAL"
    return ""


BODY_MAP = [
    ("stationcar", "WAGON"), ("st.car", "WAGON"), ("kombi", "WAGON"),
    ("halvkombi", "HATCHBACK"), ("hatchback", "HATCHBACK"),
    ("suv", "SUV"), ("crossover", "CROSSOVER"),
    ("sedan", "SEDAN"), ("cabriolet", "CONVERTIBLE"), ("coupé", "COUPE"),
    ("coupe", "COUPE"), ("mpv", "MINIVAN"), ("kassevogn", "VAN"),
    ("van", "VAN"), ("pickup", "TRUCK"),
]


def map_body(body):
    b = (body or "").lower()
    # "halvkombi" must win over "kombi", so check it first
    if "halvkombi" in b:
        return "HATCHBACK"
    for needle, value in BODY_MAP:
        if needle in b:
            return value
    return "OTHER" if b else ""


def picture_urls(vehicle):
    pics = first(vehicle, "Pictures", "Images", "Pics", default=[])
    urls = []
    for p in pics if isinstance(pics, list) else []:
        if isinstance(p, str):
            url = p
        elif isinstance(p, dict):
            url = first(p, "Url", "PictureUrl", "LargeUrl", "Large", "Uri", "Href", "Src")
        else:
            url = ""
        if url and str(url).startswith("http") and url not in urls:
            urls.append(str(url))
    return urls[:MAX_IMAGES]


# ---------------------------------------------------------------- fetch

def fetch_vehicles(user, password, timeout=60):
    resp = requests.get(
        EXPORT_URL,
        auth=(user, password),
        headers={"Accept": "application/json"},
        timeout=timeout,
    )
    if resp.status_code == 401:
        sys.exit("Bilinfo returned 401: check BILINFO_USER / BILINFO_PASSWORD.")
    resp.raise_for_status()
    return resp.json()


def extract_list(payload):
    if isinstance(payload, list):
        return payload
    return first(payload, "Vehicles", "vehicles", "Cars", default=[])


# ---------------------------------------------------------------- mapping

def dedupe(vehicles):
    """Bilinfo lists a car twice when it has both a retail and a leasing price.
    Keep one row per physical car, preferring the retail-price listing."""
    best = {}
    rank = {"retailprice": 0, "retailpricewithouttax": 1, "leasing": 2, "callforprice": 3}
    for n, v in enumerate(vehicles):
        key = str(first(v, "VehicleSourceId", "VehicleId", "Id")) or f"row-{n}"
        r = rank.get(str(first(v, "PriceType")).lower(), 4)
        if key not in best or r < best[key][0]:
            best[key] = (r, v)
    return [v for _, v in best.values()]


def to_row(v, url_template, url_id_field):
    vid = str(first(v, "VehicleSourceId", "VehicleId", "Id"))
    make = str(first(v, "Make"))
    model = str(first(v, "Model"))
    variant = str(first(v, "Variant"))
    year = digits(first(v, "Year", "ModelYear", "ProductionYear"))[:4]
    km = digits(first(v, "Mileage", "Km", "Kilometers"))
    price_type = str(first(v, "PriceType")).lower()
    price = digits(first(v, "Price", "RetailPrice"))
    fuel_raw = str(first(v, "Propellant", "Fuel", "FuelType"))
    images = picture_urls(v)

    problems = []
    if not price or price_type in ("callforprice", "leasing"):
        problems.append("no retail price")
    if not images:
        problems.append("no images")
    if not (make and model):
        problems.append("missing make/model")

    title = " ".join(x for x in (make, model, variant) if x).strip()
    fuel_dk = fuel_raw if fuel_raw else ""
    desc_bits = [f"{title}, {year}." if year else f"{title}."]
    if km:
        desc_bits.append(f"{int(km):,} km.".replace(",", "."))
    if fuel_dk:
        desc_bits.append(f"{fuel_dk}.")
    description = " ".join(desc_bits)

    detail_url = str(first(v, "DetailUrl", "Url", "VehicleUrl", "ListingUrl"))
    if not detail_url.startswith("http"):
        url_id = str(first(v, url_id_field)) or vid
        detail_url = url_template.format(id=url_id)

    row = {
        "vehicle_id": vid,
        "title": title,
        "description": description,
        "url": detail_url,
        "price": f"{price} DKK" if price else "",
        "make": make,
        "model": model,
        "trim": variant,
        "year": year,
        "mileage.value": km,
        "mileage.unit": "KM" if km else "",
        "fuel_type": map_fuel(fuel_raw),
        "transmission": map_transmission(str(first(v, "GearType", "Gear", "Transmission"))),
        "exterior_color": str(first(v, "Color", "Colour", "ExteriorColor", "Farve")),
        "body_style": map_body(str(first(v, "BodyType", "Body"))),
        "vin": str(first(v, "Vin", "VIN", "ChassisNumber", "Stelnummer")),
        "state_of_vehicle": "USED",
        "condition": "GOOD",
        "availability": "AVAILABLE",
        "vehicle_type": "CAR_TRUCK",
        **DEFAULT_ADDRESS,
    }
    for i in range(MAX_IMAGES):
        row[f"image[{i}].url"] = images[i] if i < len(images) else ""
    return row, problems


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Bilinfo -> Meta vehicle catalog CSV")
    ap.add_argument("--out", default="meta_vehicle_feed.csv")
    ap.add_argument("--sample-json", help="Read a saved Bilinfo response instead of calling the API")
    ap.add_argument("--dump-raw", help="Also save the raw API response here (for debugging)")
    ap.add_argument("--url-template", default=DEFAULT_URL_TEMPLATE)
    ap.add_argument("--url-id-field", default="VehicleSourceId",
                    help="Bilinfo field whose value goes into {id} in --url-template")
    ap.add_argument("--min-vehicles", type=int, default=10,
                    help="Refuse to write a feed smaller than this (protects the catalog "
                         "from being emptied by a bad API response)")
    args = ap.parse_args()

    if args.sample_json:
        payload = json.loads(Path(args.sample_json).read_text(encoding="utf-8"))
    else:
        user, pw = os.environ.get("BILINFO_USER"), os.environ.get("BILINFO_PASSWORD")
        if not user or not pw:
            sys.exit("Set BILINFO_USER and BILINFO_PASSWORD environment variables.")
        payload = fetch_vehicles(user, pw)

    if args.dump_raw:
        Path(args.dump_raw).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                       encoding="utf-8")

    raw = extract_list(payload)
    vehicles = dedupe(raw)
    rows, skipped = [], []
    for v in vehicles:
        row, problems = to_row(v, args.url_template, args.url_id_field)
        if problems:
            skipped.append((row["vehicle_id"], row["title"], ", ".join(problems)))
        else:
            rows.append(row)

    print(f"Bilinfo listings: {len(raw)}  unique cars: {len(vehicles)}  "
          f"in feed: {len(rows)}  skipped: {len(skipped)}")
    for vid, title, why in skipped:
        print(f"  skipped {vid} {title}: {why}")
    if raw:
        print("Keys seen on first listing:", ", ".join(sorted(raw[0].keys())))
        dealers = {}
        for v in raw:
            name = f"{first(v, 'DealerName')} ({first(v, 'DealerAddressCity')}, id {first(v, 'DealerId')})"
            dealers[name] = dealers.get(name, 0) + 1
        print("Dealers in this Bilinfo account:")
        for name, n in sorted(dealers.items(), key=lambda x: -x[1]):
            print(f"  {n:4d}  {name}")
        with_pics = next((v for v in raw if first(v, "Pictures")), None)
        if with_pics:
            print("Example Pictures entry:", json.dumps(first(with_pics, "Pictures")[:1],
                                                         ensure_ascii=False)[:300])

    if len(rows) < args.min_vehicles:
        sys.exit(f"Only {len(rows)} usable cars (< --min-vehicles {args.min_vehicles}); "
                 "not writing the feed so the catalog is not emptied.")

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
