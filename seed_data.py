import asyncio
import os
import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from faker import Faker
from config import supabase_client, logger

fake = Faker()

PRODUCT_CATEGORIES = [
    "Electronics",
    "Apparel",
    "Home & Kitchen",
    "Fitness & Outdoors",
    "Footwear",
    "Accessories",
]

CARRIERS = ["BlueDart", "Delhivery", "FedEx", "DHL Express"]


def seed_products(count: int = 20) -> List[Dict[str, Any]]:
    existing = supabase_client.table("products").select("id, name, category, price, is_final_sale").execute()
    if existing.data and len(existing.data) >= count:
        return existing.data

    categories_catalog = {
        "Electronics": [
            ("Wireless Noise-Canceling Headphones", 2999.00, {"brand": "SoundWave", "bluetooth": "5.3", "battery_life": "30h"}),
            ("Smart Fitness Watch Pro", 4499.00, {"brand": "PulseFit", "display": "1.4-inch AMOLED", "water_resistance": "5ATM"}),
            ("Mechanical Gaming Keyboard", 3299.00, {"brand": "KeyMaster", "switches": "Cherry MX Red", "backlight": "RGB"}),
            ("Ultra-Slim Wireless Power Bank 20000mAh", 1799.00, {"brand": "VoltCharge", "capacity": "20000mAh", "fast_charging": "22.5W"}),
            ("Portable Waterproof Bluetooth Speaker", 1999.00, {"brand": "BoomSonic", "rating": "IPX7", "output": "20W"}),
        ],
        "Apparel": [
            ("Organic Cotton Crew Neck T-Shirt", 799.00, {"material": "100% Organic Cotton", "fit": "Regular Fit", "color": "Navy Blue"}),
            ("Slim Fit Stretch Denim Jeans", 1899.00, {"material": "98% Cotton 2% Elastane", "fit": "Slim", "wash": "Dark Indigo"}),
            ("Merino Wool Winter Pullover", 2499.00, {"material": "100% Merino Wool", "care": "Dry clean only", "color": "Charcoal"}),
            ("Breathable Running Windcheater", 1499.00, {"material": "Polyester", "features": "Windproof, Water-resistant", "hood": "Yes"}),
            ("Linen Casual Button-Down Shirt", 1299.00, {"material": "Pure Linen", "pattern": "Solid", "sleeve": "Full Sleeve"}),
        ],
        "Home & Kitchen": [
            ("Pour-Over Stainless Steel Coffee Maker", 1599.00, {"capacity": "800ml", "filter": "Reusable Mesh", "material": "Borosilicate Glass"}),
            ("Ergonomic Memory Foam Lumbar Cushion", 1199.00, {"material": "High-density Memory Foam", "cover": "Washable Mesh"}),
            ("Non-Stick Cast Aluminum Frying Pan 28cm", 1899.00, {"coating": "Ceramic PFOA-Free", "induction_compatible": True}),
            ("Smart Ultrasonic Cool Mist Air Humidifier", 2799.00, {"tank_capacity": "4L", "auto_shutoff": True, "coverage": "300 sq ft"}),
            ("Stainless Steel Chef Knife 8-inch", 999.00, {"steel": "German High-Carbon Steel", "handle": "Pakkawood"}),
        ],
        "Fitness & Outdoors": [
            ("Eco-Friendly Non-Slip Yoga Mat 6mm", 999.00, {"material": "TPE Eco-Friendly", "thickness": "6mm", "strap": "Included"}),
            ("Adjustable Cast Iron Dumbbell Set 20kg", 3999.00, {"weight": "20kg total", "plates": "Rubber coated", "grip": "Knurled chrome"}),
            ("Double-Wall Insulated Vacuum Flask 1L", 799.00, {"insulation": "24h cold, 12h hot", "material": "Food Grade 304 Steel"}),
            ("Lightweight 2-Person Camping Dome Tent", 3499.00, {"capacity": "2 Person", "rainfly": "PU 2000mm", "weight": "2.4kg"}),
            ("Hydration Running Backpack with 2L Bladder", 1699.00, {"bladder": "2L BPA-Free", "pockets": "Reflective storage", "chest_strap": True}),
        ],
    }

    products_to_insert = []
    item_idx = 0
    for cat_name, items in categories_catalog.items():
        for name, price, specs in items:
            item_idx += 1
            is_final_sale = (item_idx in [4, 9, 14])
            products_to_insert.append({
                "id": str(uuid.uuid4()),
                "name": name,
                "category": cat_name,
                "price": price,
                "description": f"High-quality {name.lower()} crafted for durability and peak everyday performance.",
                "specs": specs,
                "is_final_sale": is_final_sale,
                "created_at": (datetime.now(timezone.utc) - timedelta(days=random.randint(30, 90))).isoformat(),
            })

    res = supabase_client.table("products").insert(products_to_insert).execute()
    return res.data or []


def get_or_create_customer() -> str:
    res = supabase_client.table("users").select("user_id, email, full_name, role").limit(1).execute()
    if res.data and len(res.data) > 0:
        return res.data[0]["user_id"]

    try:
        auth_users = supabase_client.auth.admin.list_users()
        if auth_users and len(auth_users) > 0:
            existing_auth_user = auth_users[0]
            now_iso = datetime.now(timezone.utc).isoformat()
            ins = supabase_client.table("users").upsert({
                "user_id": existing_auth_user.id,
                "email": existing_auth_user.email or "customer@example.com",
                "full_name": "Demo Customer",
                "role": "customer",
                "is_active": True,
                "created_at": now_iso,
                "updated_at": now_iso,
            }).execute()
            if ins.data:
                return ins.data[0]["user_id"]
    except Exception as e:
        logger.warning(f"Could not read auth.users: {e}")

    test_email = "customer@orderbot.demo"
    test_password = "Password123!"
    try:
        auth_created = supabase_client.auth.admin.create_user({
            "email": test_email,
            "password": test_password,
            "email_confirm": True,
            "user_metadata": {"full_name": "Demo Customer", "role": "customer"},
        })
        user_uuid = auth_created.user.id
        now_iso = datetime.now(timezone.utc).isoformat()
        ins = supabase_client.table("users").upsert({
            "user_id": user_uuid,
            "email": test_email,
            "full_name": "Demo Customer",
            "role": "customer",
            "is_active": True,
            "created_at": now_iso,
            "updated_at": now_iso,
        }).execute()
        return user_uuid
    except Exception as e:
        logger.error(f"Cannot auto-create auth user due to database trigger constraint: {e}")
        return ""


def seed_orders_and_shipments(customer_id: str, products: List[Dict[str, Any]]):
    if not customer_id:
        print("\nSkipping orders seeding: No active customer_id available.")
        print("Please create a customer in Supabase or run the SQL trigger fix.")
        return

    existing_orders = supabase_client.table("orders").select("id").eq("customer_id", customer_id).execute()
    if existing_orders.data and len(existing_orders.data) >= 5:
        print(f"Customer '{customer_id}' already has {len(existing_orders.data)} orders. Skipping duplicate seed.")
        return

    now = datetime.now(timezone.utc)

    order_scenarios = [
        {
            "status": "placed",
            "placed_at": (now - timedelta(hours=2)).isoformat(),
            "delivered_at": None,
            "carrier_status": None,
            "label": "Placed Today (Cancellable)",
        },
        {
            "status": "processing",
            "placed_at": (now - timedelta(days=1)).isoformat(),
            "delivered_at": None,
            "carrier_status": None,
            "label": "Processing (Cancellable)",
        },
        {
            "status": "shipped",
            "placed_at": (now - timedelta(days=3)).isoformat(),
            "delivered_at": None,
            "carrier_status": "in_transit",
            "label": "Shipped (In Transit)",
        },
        {
            "status": "delivered",
            "placed_at": (now - timedelta(days=10)).isoformat(),
            "delivered_at": (now - timedelta(days=6)).isoformat(),
            "carrier_status": "delivered",
            "label": "Delivered 6 Days Ago (Within 30-Day Return Window)",
        },
        {
            "status": "delivered",
            "placed_at": (now - timedelta(days=50)).isoformat(),
            "delivered_at": (now - timedelta(days=45)).isoformat(),
            "carrier_status": "delivered",
            "label": "Delivered 45 Days Ago (Past 30-Day Return Window)",
        },
        {
            "status": "cancelled",
            "placed_at": (now - timedelta(days=15)).isoformat(),
            "delivered_at": None,
            "carrier_status": None,
            "label": "Already Cancelled",
        },
    ]

    for scenario in order_scenarios:
        num_items = random.randint(1, 3)
        selected_prods = random.sample(products, min(num_items, len(products)))

        total_amount = 0.0
        order_items_to_create = []

        order_id = str(uuid.uuid4())
        placed_iso = scenario["placed_at"]
        delivered_iso = scenario["delivered_at"]
        status_val = scenario["status"]

        for prod in selected_prods:
            qty = random.randint(1, 2)
            u_price = float(prod["price"])
            total_amount += qty * u_price
            order_items_to_create.append({
                "id": str(uuid.uuid4()),
                "order_id": order_id,
                "product_id": prod["id"],
                "quantity": qty,
                "unit_price": u_price,
            })

        order_row = {
            "id": order_id,
            "customer_id": customer_id,
            "status": status_val,
            "placed_at": placed_iso,
            "delivered_at": delivered_iso,
            "total": round(total_amount, 2),
            "currency": "INR",
            "updated_at": placed_iso,
        }

        order_res = supabase_client.table("orders").insert(order_row).execute()
        if not order_res.data:
            print(f"Failed to insert order {order_id}")
            continue

        created_order = order_res.data[0]
        order_num = created_order.get("order_number")

        items_res = supabase_client.table("order_items").insert(order_items_to_create).execute()

        if scenario["carrier_status"]:
            shipped_time = (datetime.fromisoformat(placed_iso) + timedelta(days=1)).isoformat()
            exp_delivery = (datetime.fromisoformat(shipped_time) + timedelta(days=4)).date().isoformat()
            carrier_name = random.choice(CARRIERS)
            tracking_num = f"{carrier_name[:3].upper()}{random.randint(10000000, 99999999)}"

            supabase_client.table("shipments").insert({
                "id": str(uuid.uuid4()),
                "order_id": order_id,
                "carrier": carrier_name,
                "tracking_number": tracking_num,
                "shipped_at": shipped_time,
                "expected_delivery": exp_delivery,
                "last_status": scenario["carrier_status"],
                "updated_at": delivered_iso or shipped_time,
            }).execute()

        print(f"Created Order #{order_num} [{status_val}] Total: INR {round(total_amount, 2)} ({scenario['label']})")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Seed database tables: products, orders, order_items, shipments")
    parser.add_argument("--customer-id", type=str, help="Specific customer UUID in table 'users' to attach orders to.")
    args = parser.parse_args()

    print("=" * 60)
    print("Seeding database tables: products, orders, order_items, shipments")
    print("=" * 60)

    print("\n1. Seeding Products...")
    products = seed_products(count=20)
    print(f"Products ready: {len(products)} products available.")

    print("\n2. Checking Customer...")
    customer_id = args.customer_id or get_or_create_customer()
    if customer_id:
        print(f"Active Customer ID: {customer_id}")
        print("\n3. Seeding Orders, Order Items, and Shipments...")
        seed_orders_and_shipments(customer_id, products)
    else:
        print("\nNotice on Customer and Orders:")
        print("To populate orders, a customer record is required in table 'users'.")
        print("If you already have a user, you can run: uv run python seed_data.py --customer-id <YOUR_USER_ID>")
        print("Or run the following SQL in Supabase SQL Editor to enable user creation:")
        print("""
CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS trigger AS $$
BEGIN
  INSERT INTO public.users (user_id, email, full_name, role, is_active, created_at, updated_at)
  VALUES (
    new.id,
    coalesce(new.email, 'customer@example.com'),
    coalesce(new.raw_user_meta_data->>'full_name', 'Demo Customer'),
    'customer',
    true,
    now(),
    now()
  )
  ON CONFLICT (user_id) DO UPDATE SET updated_at = now();
  RETURN new;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();

ALTER TABLE public.conversations ADD COLUMN IF NOT EXISTS order_id UUID REFERENCES public.orders(id) ON DELETE CASCADE;
        """)

    print("\nSeeding script complete.")


if __name__ == "__main__":
    main()
