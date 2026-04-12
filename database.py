"""
database.py — SQLite sample database + DatabaseManager.

DatabaseManager supports:
  • default  — built-in e-commerce SQLite DB
  • upload   — user-uploaded CSV / XLSX / SQLite file
  • external — PostgreSQL / MySQL via SQLAlchemy connection string

FIX #8: init_db only seeds if the DB is empty (avoids 3000+ INSERT OR IGNORE
         on every app startup which was wasting ~1-2 seconds).
"""

from __future__ import annotations
import io
import random
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

DB_PATH = "ecommerce.db"

# ── Schema ─────────────────────────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS customers (
    customer_id   INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    email         TEXT UNIQUE,
    state         TEXT,
    city          TEXT,
    signup_date   TEXT,
    customer_tier TEXT
);
CREATE TABLE IF NOT EXISTS categories (
    category_id INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    parent_id   INTEGER REFERENCES categories(category_id)
);
CREATE TABLE IF NOT EXISTS products (
    product_id  INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    category_id INTEGER REFERENCES categories(category_id),
    price       REAL NOT NULL,
    cost        REAL NOT NULL,
    stock       INTEGER DEFAULT 0,
    is_active   INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS orders (
    order_id     INTEGER PRIMARY KEY,
    customer_id  INTEGER REFERENCES customers(customer_id),
    order_date   TEXT NOT NULL,
    status       TEXT,
    region       TEXT,
    total_amount REAL
);
CREATE TABLE IF NOT EXISTS order_items (
    item_id     INTEGER PRIMARY KEY,
    order_id    INTEGER REFERENCES orders(order_id),
    product_id  INTEGER REFERENCES products(product_id),
    quantity    INTEGER NOT NULL,
    unit_price  REAL NOT NULL,
    discount_pct REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS reviews (
    review_id   INTEGER PRIMARY KEY,
    product_id  INTEGER REFERENCES products(product_id),
    customer_id INTEGER REFERENCES customers(customer_id),
    rating      INTEGER CHECK(rating BETWEEN 1 AND 5),
    review_date TEXT
);
"""

STATES = ["California","Texas","New York","Florida","Washington",
          "Illinois","Ohio","Georgia","North Carolina","Arizona"]
CITIES = {
    "California":["Los Angeles","San Francisco","San Diego"],
    "Texas":["Houston","Austin","Dallas"],
    "New York":["New York City","Buffalo","Rochester"],
    "Florida":["Miami","Orlando","Tampa"],
    "Washington":["Seattle","Spokane","Tacoma"],
    "Illinois":["Chicago","Naperville","Rockford"],
    "Ohio":["Columbus","Cleveland","Cincinnati"],
    "Georgia":["Atlanta","Savannah","Augusta"],
    "North Carolina":["Charlotte","Raleigh","Durham"],
    "Arizona":["Phoenix","Tucson","Scottsdale"],
}
TIERS   = ["Bronze","Silver","Gold","Platinum"]
REGIONS = ["North America","South America","Europe","Asia","Oceania"]

CATEGORIES = [
    (1,"Electronics",None),(2,"Clothing",None),(3,"Home & Garden",None),(4,"Sports",None),
    (5,"Laptops",1),(6,"Smartphones",1),(7,"Audio",1),(8,"Men's Clothing",2),
    (9,"Women's Clothing",2),(10,"Furniture",3),(11,"Gardening",3),(12,"Fitness",4),(13,"Outdoor Sports",4),
]

PRODUCTS = [
    (1,"ProBook Laptop 15",5,1299.99,750.0,80),(2,"UltraSlim Laptop 13",5,899.99,500.0,120),
    (3,"Galaxy Phone X12",6,799.99,380.0,200),(4,"iPhone 15 Pro",6,1199.99,640.0,150),
    (5,"Pixel 8",6,699.99,340.0,110),(6,"ANC Headphones Pro",7,349.99,130.0,250),
    (7,"Wireless Earbuds X",7,149.99,55.0,500),(8,"Classic Fit T-Shirt",8,29.99,8.0,800),
    (9,"Slim Jeans",8,59.99,18.0,400),(10,"Summer Dress",9,49.99,14.0,350),
    (11,"Yoga Pants",9,39.99,12.0,600),(12,"Standing Desk Pro",10,499.99,210.0,60),
    (13,"Ergonomic Chair",10,349.99,140.0,90),(14,"Garden Tool Set",11,79.99,25.0,200),
    (15,"Smart Plant Monitor",11,49.99,18.0,180),(16,"Treadmill X200",12,899.99,420.0,40),
    (17,"Resistance Band Set",12,24.99,7.0,700),(18,"Hiking Backpack 40L",13,129.99,45.0,300),
    (19,"Camping Tent 4-Person",13,249.99,95.0,150),(20,"Smart Speaker Mini",7,99.99,35.0,400),
]

STATUS_WEIGHTS = {"delivered":0.55,"shipped":0.20,"pending":0.10,"returned":0.08,"cancelled":0.07}


def _weighted(d):
    return random.choices(list(d.keys()), weights=list(d.values()), k=1)[0]

def _rdate(start, end):
    delta = end - start
    return (start + timedelta(days=random.randint(0, delta.days))).strftime("%Y-%m-%d")


def _seed(conn):
    rng   = random.Random(42)
    start = datetime(2022,1,1)
    end   = datetime(2024,12,31)
    cur   = conn.cursor()
    cur.executemany("INSERT OR IGNORE INTO categories VALUES (?,?,?)", CATEGORIES)
    cur.executemany("INSERT OR IGNORE INTO products VALUES (?,?,?,?,?,?,1)", PRODUCTS)
    fnames = ["Alice","Bob","Charlie","Diana","Ethan","Fiona","George","Hannah","Ivan","Julia",
              "Kevin","Laura","Mike","Nina","Oscar","Priya","Quinn","Rachel","Sam","Tara"]
    lnames = ["Smith","Johnson","Williams","Brown","Jones","Garcia","Miller","Davis",
              "Wilson","Anderson","Taylor","Thomas","Jackson","White","Harris","Martin"]
    customers = []
    for i in range(1,501):
        state = rng.choice(STATES); city = rng.choice(CITIES[state])
        tier  = rng.choices(TIERS, weights=[0.40,0.30,0.20,0.10])[0]
        customers.append((i,f"{rng.choice(fnames)} {rng.choice(lnames)}",f"user{i}@example.com",
                          state,city,_rdate(start,datetime(2023,6,30)),tier))
    cur.executemany("INSERT OR IGNORE INTO customers VALUES (?,?,?,?,?,?,?)", customers)
    item_id=1; orders=[]; items=[]
    for oid in range(1,3001):
        cust=rng.randint(1,500); date=_rdate(start,end)
        status=_weighted(STATUS_WEIGHTS); region=rng.choice(REGIONS)
        n=rng.randint(1,5); total=0.0
        for _ in range(n):
            pid=rng.randint(1,20); qty=rng.randint(1,4)
            price=PRODUCTS[pid-1][3]; disc=rng.choice([0,0,0,5,10,15,20])
            total+=price*qty*(1-disc/100)
            items.append((item_id,oid,pid,qty,price,disc)); item_id+=1
        orders.append((oid,cust,date,status,region,round(total,2)))
    cur.executemany("INSERT OR IGNORE INTO orders VALUES (?,?,?,?,?,?)", orders)
    cur.executemany("INSERT OR IGNORE INTO order_items VALUES (?,?,?,?,?,?)", items)
    reviews=[]
    for rid in range(1,1501):
        reviews.append((rid,rng.randint(1,20),rng.randint(1,500),
                        rng.choices([1,2,3,4,5],weights=[0.05,0.08,0.15,0.35,0.37])[0],
                        _rdate(start,end)))
    cur.executemany("INSERT OR IGNORE INTO reviews VALUES (?,?,?,?,?)", reviews)
    conn.commit()


def get_connection(path=DB_PATH):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(path=DB_PATH):
    """FIX #8: Only seed if the orders table is empty — avoids slow startup on reruns."""
    conn = get_connection(path)
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    cur = conn.execute("SELECT COUNT(*) FROM orders")
    if cur.fetchone()[0] == 0:
        _seed(conn)
    conn.close()


def run_query(sql: str, path: str = DB_PATH) -> tuple[list[dict], list[str], str | None]:
    try:
        conn = get_connection(path)
        cur  = conn.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows, cols, None
    except Exception as exc:
        return [], [], str(exc)


def get_schema_description() -> str:
    return """
DATABASE: E-Commerce Analytics (SQLite)

TABLE: customers
  customer_id (PK), name, email, state, city,
  signup_date (YYYY-MM-DD), customer_tier (Bronze/Silver/Gold/Platinum)

TABLE: categories
  category_id (PK), name, parent_id (FK→categories)
  Top-level: Electronics(id=1), Clothing(id=2), Home & Garden(id=3), Sports(id=4)
  Sub-categories: Laptops(id=5,parent=Electronics), Smartphones(id=6,parent=Electronics),
                  Audio(id=7,parent=Electronics) | Men's Clothing(id=8,parent=Clothing),
                  Women's Clothing(id=9,parent=Clothing) | Furniture(id=10,parent=Home & Garden),
                  Gardening(id=11,parent=Home & Garden) | Fitness(id=12,parent=Sports),
                  Outdoor Sports(id=13,parent=Sports)

  CRITICAL: Products are ONLY linked to sub-categories, NEVER to top-level categories.
  To filter by a top-level category (Electronics, Clothing, Sports, Home & Garden)
  you MUST join the categories table TWICE:
    JOIN categories c ON p.category_id = c.category_id
    JOIN categories parent ON c.parent_id = parent.category_id
  Then filter: WHERE parent.name = 'Electronics' OR c.name = 'Electronics'
  A single join with WHERE c.name = 'Electronics' will ALWAYS return zero rows.

TABLE: products
  product_id (PK), name, category_id (FK→sub-category only), price, cost, stock, is_active

TABLE: orders
  order_id (PK), customer_id (FK), order_date (YYYY-MM-DD),
  status (pending/shipped/delivered/returned/cancelled),
  region (North America/South America/Europe/Asia/Oceania), total_amount

TABLE: order_items
  item_id (PK), order_id (FK), product_id (FK),
  quantity, unit_price, discount_pct

TABLE: reviews
  review_id (PK), product_id (FK), customer_id (FK), rating (1-5), review_date

DATE RANGE: 2022-01-01 to 2024-12-31
""".strip()


# ── DatabaseManager ─────────────────────────────────────────────────────────────

class DatabaseManager:
    """
    Unified database interface supporting:
      default  — built-in SQLite e-commerce DB
      upload   — CSV / XLSX / SQLite uploaded by user
      external — PostgreSQL / MySQL via SQLAlchemy
    """

    def __init__(self):
        self.mode            = "default"
        self.db_path         = DB_PATH
        self.engine          = None
        self.uploaded_tables: list[str] = []
        self._connection_str = ""

    # ── Connection setup ────────────────────────────────────────────────────────

    def use_default(self):
        self.mode = "default"; self.db_path = DB_PATH
        self.engine = None; self.uploaded_tables = []

    def upload_file(self, file_bytes: bytes, filename: str) -> tuple[bool, str]:
        ext = filename.rsplit(".", 1)[-1].lower()
        upload_db = "./uploaded_data.db"

        if ext in ("db", "sqlite", "sqlite3"):
            with open(upload_db, "wb") as f:
                f.write(file_bytes)
            self.mode = "upload"; self.db_path = upload_db
            self.engine = None
            self.uploaded_tables = self._sqlite_tables(upload_db)
            n = len(self.uploaded_tables)
            return True, f"Loaded SQLite: {n} table{'s' if n!=1 else ''} — {', '.join(self.uploaded_tables)}"

        if ext == "csv":
            import pandas as pd
            df = pd.read_csv(io.BytesIO(file_bytes))
            tname = filename.rsplit(".",1)[0].replace("-","_").replace(" ","_")
            self._df_to_sqlite(df, tname, upload_db)
            self.mode = "upload"; self.db_path = upload_db
            self.uploaded_tables = [tname]
            return True, f"Loaded CSV → table '{tname}' ({len(df):,} rows, {len(df.columns)} cols)"

        if ext in ("xlsx", "xls"):
            import pandas as pd
            xl = pd.ExcelFile(io.BytesIO(file_bytes))
            tables = []
            for i, sheet in enumerate(xl.sheet_names):
                df    = xl.parse(sheet)
                tname = sheet.replace("-","_").replace(" ","_")
                mode  = "replace" if i == 0 else "append"
                self._df_to_sqlite(df, tname, upload_db, if_exists=mode)
                tables.append(tname)
            self.mode = "upload"; self.db_path = upload_db
            self.uploaded_tables = tables
            return True, f"Loaded Excel: sheets → {', '.join(tables)}"

        return False, f"Unsupported file type: .{ext}  (use .csv / .xlsx / .db)"

    def connect_external(self, conn_str: str) -> tuple[bool, str]:
        try:
            from sqlalchemy import create_engine, text
            engine = create_engine(conn_str, pool_pre_ping=True)
            with engine.connect() as c:
                c.execute(text("SELECT 1"))
            self.engine = engine; self.mode = "external"
            self._connection_str = conn_str
            return True, "Connected successfully"
        except ImportError:
            return False, "SQLAlchemy not installed: pip install sqlalchemy"
        except Exception as exc:
            return False, str(exc)

    # ── Query execution ─────────────────────────────────────────────────────────

    def run_query(self, sql: str) -> tuple[list[dict], list[str], str | None]:
        if self.mode == "external" and self.engine:
            return self._run_alchemy(sql)
        return run_query(sql, self.db_path)

    def get_execution_plan(self, sql: str) -> str:
        if self.mode == "external":
            return "Execution plan not available for external databases via this interface."
        rows, _, err = run_query(f"EXPLAIN QUERY PLAN {sql}", self.db_path)
        if err:
            return f"Error: {err}"
        lines = [" | ".join(str(v) for v in r.values()) for r in rows]
        return "\n".join(lines) if lines else "No plan returned."

    # ── Schema ──────────────────────────────────────────────────────────────────

    def get_schema_description(self) -> str:
        if self.mode == "default":
            return get_schema_description()
        if self.mode == "upload":
            return self._infer_sqlite_schema(self.db_path)
        if self.mode == "external":
            return self._infer_alchemy_schema()
        return get_schema_description()

    def get_schema_tree(self) -> dict[str, list[dict]]:
        if self.mode in ("default", "upload"):
            return self._sqlite_tree(self.db_path)
        if self.mode == "external":
            return self._alchemy_tree()
        return {}

    # ── Private helpers ─────────────────────────────────────────────────────────

    def _df_to_sqlite(self, df, tname, db_path, if_exists="replace"):
        conn = sqlite3.connect(db_path)
        df.to_sql(tname, conn, if_exists=if_exists, index=False)
        conn.close()

    def _sqlite_tables(self, path) -> list[str]:
        rows, _, _ = run_query(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name", path
        )
        return [r["name"] for r in rows]

    def _sqlite_tree(self, path) -> dict[str, list[dict]]:
        tree = {}
        for tbl in self._sqlite_tables(path):
            rows, _, _ = run_query(f"PRAGMA table_info({tbl})", path)
            tree[tbl] = [{"name": r["name"], "type": r["type"]} for r in rows]
        return tree

    def _infer_sqlite_schema(self, path) -> str:
        tables = self._sqlite_tables(path)
        parts  = [f"DATABASE: {Path(path).name} (SQLite, uploaded)\n"]
        for tbl in tables:
            rows, _, _ = run_query(f"PRAGMA table_info({tbl})", path)
            cnt,  _, _ = run_query(f"SELECT COUNT(*) AS n FROM {tbl}", path)
            n = cnt[0]["n"] if cnt else "?"
            parts.append(f"\nTABLE: {tbl} ({n:,} rows)")
            parts.extend(f"  {r['name']} ({r['type']})" for r in rows)
        return "\n".join(parts)

    def _run_alchemy(self, sql) -> tuple[list[dict], list[str], str | None]:
        try:
            from sqlalchemy import text
            with self.engine.connect() as c:
                res  = c.execute(text(sql))
                cols = list(res.keys())
                rows = [dict(zip(cols, row)) for row in res.fetchall()]
            return rows, cols, None
        except Exception as exc:
            return [], [], str(exc)

    def _infer_alchemy_schema(self) -> str:
        try:
            from sqlalchemy import inspect as sa_inspect
            insp  = sa_inspect(self.engine)
            parts = [f"DATABASE: {self._connection_str[:40]}… (external)\n"]
            for tbl in insp.get_table_names():
                cols = insp.get_columns(tbl)
                parts.append(f"\nTABLE: {tbl}")
                parts.extend(f"  {c['name']} ({c['type']})" for c in cols)
            return "\n".join(parts)
        except Exception as exc:
            return f"Schema error: {exc}"

    def _alchemy_tree(self) -> dict[str, list[dict]]:
        try:
            from sqlalchemy import inspect as sa_inspect
            insp = sa_inspect(self.engine)
            return {
                tbl: [{"name": c["name"], "type": str(c["type"])}
                      for c in insp.get_columns(tbl)]
                for tbl in insp.get_table_names()
            }
        except Exception:
            return {}

    @property
    def label(self) -> str:
        if self.mode == "default":
            return "E-Commerce DB (default)"
        if self.mode == "upload":
            return f"Uploaded: {', '.join(self.uploaded_tables) or 'file'}"
        if self.mode == "external":
            return f"External: {self._connection_str[:30]}…"
        return "Unknown"











# """
# database.py — SQLite sample database + DatabaseManager.

# DatabaseManager supports:
#   • default  — built-in e-commerce SQLite DB
#   • upload   — user-uploaded CSV / XLSX / SQLite file
#   • external — PostgreSQL / MySQL via SQLAlchemy connection string
# """

# from __future__ import annotations
# import io
# import random
# import sqlite3
# from datetime import datetime, timedelta
# from pathlib import Path
# from typing import Any

# DB_PATH = "ecommerce.db"

# # ── Schema ─────────────────────────────────────────────────────────────────────

# SCHEMA_SQL = """
# CREATE TABLE IF NOT EXISTS customers (
#     customer_id   INTEGER PRIMARY KEY,
#     name          TEXT NOT NULL,
#     email         TEXT UNIQUE,
#     state         TEXT,
#     city          TEXT,
#     signup_date   TEXT,
#     customer_tier TEXT
# );
# CREATE TABLE IF NOT EXISTS categories (
#     category_id INTEGER PRIMARY KEY,
#     name        TEXT NOT NULL,
#     parent_id   INTEGER REFERENCES categories(category_id)
# );
# CREATE TABLE IF NOT EXISTS products (
#     product_id  INTEGER PRIMARY KEY,
#     name        TEXT NOT NULL,
#     category_id INTEGER REFERENCES categories(category_id),
#     price       REAL NOT NULL,
#     cost        REAL NOT NULL,
#     stock       INTEGER DEFAULT 0,
#     is_active   INTEGER DEFAULT 1
# );
# CREATE TABLE IF NOT EXISTS orders (
#     order_id     INTEGER PRIMARY KEY,
#     customer_id  INTEGER REFERENCES customers(customer_id),
#     order_date   TEXT NOT NULL,
#     status       TEXT,
#     region       TEXT,
#     total_amount REAL
# );
# CREATE TABLE IF NOT EXISTS order_items (
#     item_id     INTEGER PRIMARY KEY,
#     order_id    INTEGER REFERENCES orders(order_id),
#     product_id  INTEGER REFERENCES products(product_id),
#     quantity    INTEGER NOT NULL,
#     unit_price  REAL NOT NULL,
#     discount_pct REAL DEFAULT 0
# );
# CREATE TABLE IF NOT EXISTS reviews (
#     review_id   INTEGER PRIMARY KEY,
#     product_id  INTEGER REFERENCES products(product_id),
#     customer_id INTEGER REFERENCES customers(customer_id),
#     rating      INTEGER CHECK(rating BETWEEN 1 AND 5),
#     review_date TEXT
# );
# """

# STATES = ["California","Texas","New York","Florida","Washington",
#           "Illinois","Ohio","Georgia","North Carolina","Arizona"]
# CITIES = {
#     "California":["Los Angeles","San Francisco","San Diego"],
#     "Texas":["Houston","Austin","Dallas"],
#     "New York":["New York City","Buffalo","Rochester"],
#     "Florida":["Miami","Orlando","Tampa"],
#     "Washington":["Seattle","Spokane","Tacoma"],
#     "Illinois":["Chicago","Naperville","Rockford"],
#     "Ohio":["Columbus","Cleveland","Cincinnati"],
#     "Georgia":["Atlanta","Savannah","Augusta"],
#     "North Carolina":["Charlotte","Raleigh","Durham"],
#     "Arizona":["Phoenix","Tucson","Scottsdale"],
# }
# TIERS   = ["Bronze","Silver","Gold","Platinum"]
# REGIONS = ["North America","South America","Europe","Asia","Oceania"]

# CATEGORIES = [
#     (1,"Electronics",None),(2,"Clothing",None),(3,"Home & Garden",None),(4,"Sports",None),
#     (5,"Laptops",1),(6,"Smartphones",1),(7,"Audio",1),(8,"Men's Clothing",2),
#     (9,"Women's Clothing",2),(10,"Furniture",3),(11,"Gardening",3),(12,"Fitness",4),(13,"Outdoor Sports",4),
# ]

# PRODUCTS = [
#     (1,"ProBook Laptop 15",5,1299.99,750.0,80),(2,"UltraSlim Laptop 13",5,899.99,500.0,120),
#     (3,"Galaxy Phone X12",6,799.99,380.0,200),(4,"iPhone 15 Pro",6,1199.99,640.0,150),
#     (5,"Pixel 8",6,699.99,340.0,110),(6,"ANC Headphones Pro",7,349.99,130.0,250),
#     (7,"Wireless Earbuds X",7,149.99,55.0,500),(8,"Classic Fit T-Shirt",8,29.99,8.0,800),
#     (9,"Slim Jeans",8,59.99,18.0,400),(10,"Summer Dress",9,49.99,14.0,350),
#     (11,"Yoga Pants",9,39.99,12.0,600),(12,"Standing Desk Pro",10,499.99,210.0,60),
#     (13,"Ergonomic Chair",10,349.99,140.0,90),(14,"Garden Tool Set",11,79.99,25.0,200),
#     (15,"Smart Plant Monitor",11,49.99,18.0,180),(16,"Treadmill X200",12,899.99,420.0,40),
#     (17,"Resistance Band Set",12,24.99,7.0,700),(18,"Hiking Backpack 40L",13,129.99,45.0,300),
#     (19,"Camping Tent 4-Person",13,249.99,95.0,150),(20,"Smart Speaker Mini",7,99.99,35.0,400),
# ]

# STATUS_WEIGHTS = {"delivered":0.55,"shipped":0.20,"pending":0.10,"returned":0.08,"cancelled":0.07}


# def _weighted(d):
#     return random.choices(list(d.keys()), weights=list(d.values()), k=1)[0]

# def _rdate(start, end):
#     delta = end - start
#     return (start + timedelta(days=random.randint(0, delta.days))).strftime("%Y-%m-%d")


# def _seed(conn):
#     rng   = random.Random(42)
#     start = datetime(2022,1,1)
#     end   = datetime(2024,12,31)
#     cur   = conn.cursor()
#     cur.executemany("INSERT OR IGNORE INTO categories VALUES (?,?,?)", CATEGORIES)
#     cur.executemany("INSERT OR IGNORE INTO products VALUES (?,?,?,?,?,?,1)", PRODUCTS)
#     fnames = ["Alice","Bob","Charlie","Diana","Ethan","Fiona","George","Hannah","Ivan","Julia",
#               "Kevin","Laura","Mike","Nina","Oscar","Priya","Quinn","Rachel","Sam","Tara"]
#     lnames = ["Smith","Johnson","Williams","Brown","Jones","Garcia","Miller","Davis",
#               "Wilson","Anderson","Taylor","Thomas","Jackson","White","Harris","Martin"]
#     customers = []
#     for i in range(1,501):
#         state = rng.choice(STATES); city = rng.choice(CITIES[state])
#         tier  = rng.choices(TIERS, weights=[0.40,0.30,0.20,0.10])[0]
#         customers.append((i,f"{rng.choice(fnames)} {rng.choice(lnames)}",f"user{i}@example.com",
#                           state,city,_rdate(start,datetime(2023,6,30)),tier))
#     cur.executemany("INSERT OR IGNORE INTO customers VALUES (?,?,?,?,?,?,?)", customers)
#     item_id=1; orders=[]; items=[]
#     for oid in range(1,3001):
#         cust=rng.randint(1,500); date=_rdate(start,end)
#         status=_weighted(STATUS_WEIGHTS); region=rng.choice(REGIONS)
#         n=rng.randint(1,5); total=0.0
#         for _ in range(n):
#             pid=rng.randint(1,20); qty=rng.randint(1,4)
#             price=PRODUCTS[pid-1][3]; disc=rng.choice([0,0,0,5,10,15,20])
#             total+=price*qty*(1-disc/100)
#             items.append((item_id,oid,pid,qty,price,disc)); item_id+=1
#         orders.append((oid,cust,date,status,region,round(total,2)))
#     cur.executemany("INSERT OR IGNORE INTO orders VALUES (?,?,?,?,?,?)", orders)
#     cur.executemany("INSERT OR IGNORE INTO order_items VALUES (?,?,?,?,?,?)", items)
#     reviews=[]
#     for rid in range(1,1501):
#         reviews.append((rid,rng.randint(1,20),rng.randint(1,500),
#                         rng.choices([1,2,3,4,5],weights=[0.05,0.08,0.15,0.35,0.37])[0],
#                         _rdate(start,end)))
#     cur.executemany("INSERT OR IGNORE INTO reviews VALUES (?,?,?,?,?)", reviews)
#     conn.commit()


# def get_connection(path=DB_PATH):
#     conn = sqlite3.connect(path)
#     conn.row_factory = sqlite3.Row
#     return conn

# def init_db(path=DB_PATH):
#     conn = get_connection(path)
#     conn.executescript(SCHEMA_SQL)
#     conn.commit()
#     _seed(conn)
#     conn.close()

# def run_query(sql: str, path: str = DB_PATH) -> tuple[list[dict], list[str], str | None]:
#     try:
#         conn = get_connection(path)
#         cur  = conn.execute(sql)
#         cols = [d[0] for d in cur.description] if cur.description else []
#         rows = [dict(r) for r in cur.fetchall()]
#         conn.close()
#         return rows, cols, None
#     except Exception as exc:
#         return [], [], str(exc)

# def get_schema_description() -> str:
#     return """
# DATABASE: E-Commerce Analytics (SQLite)

# TABLE: customers
#   customer_id (PK), name, email, state, city,
#   signup_date (YYYY-MM-DD), customer_tier (Bronze/Silver/Gold/Platinum)

# TABLE: categories
#   category_id (PK), name, parent_id (FK→categories)
#   Top-level: Electronics, Clothing, Home & Garden, Sports
#   Sub-categories: Laptops(parent=Electronics), Smartphones(parent=Electronics), 
#                   Audio(parent=Electronics) | Men's Clothing(parent=Clothing), 
#                   Women's Clothing(parent=Clothing) | Furniture(parent=Home & Garden), 
#                   Gardening(parent=Home & Garden) | Fitness(parent=Sports), 
#                   Outdoor Sports(parent=Sports)

#   CRITICAL: Products are ONLY linked to sub-categories, NEVER to top-level categories.
#   To filter by a top-level category like 'Electronics', 'Clothing', 'Sports', 
#   'Home & Garden' you MUST join categories table twice:
#     JOIN categories c ON p.category_id = c.category_id
#     JOIN categories parent ON c.parent_id = parent.category_id
#   Then filter: WHERE parent.name = 'Electronics' OR c.name = 'Electronics'
#   Single join + WHERE c.name = 'Electronics' will always return zero rows.

# TABLE: products
#   product_id (PK), name, category_id (FK), price, cost, stock, is_active

# TABLE: orders
#   order_id (PK), customer_id (FK), order_date (YYYY-MM-DD),
#   status (pending/shipped/delivered/returned/cancelled),
#   region (North America/South America/Europe/Asia/Oceania), total_amount

# TABLE: order_items
#   item_id (PK), order_id (FK), product_id (FK),
#   quantity, unit_price, discount_pct

# TABLE: reviews
#   review_id (PK), product_id (FK), customer_id (FK), rating (1-5), review_date

# DATE RANGE: 2022-01-01 to 2024-12-31
# """.strip()


# # ── DatabaseManager ─────────────────────────────────────────────────────────────

# class DatabaseManager:
#     """
#     Unified database interface supporting:
#       default  — built-in SQLite e-commerce DB
#       upload   — CSV / XLSX / SQLite uploaded by user
#       external — PostgreSQL / MySQL via SQLAlchemy
#     """

#     def __init__(self):
#         self.mode            = "default"
#         self.db_path         = DB_PATH
#         self.engine          = None
#         self.uploaded_tables: list[str] = []
#         self._connection_str = ""

#     # ── Connection setup ────────────────────────────────────────────────────────

#     def use_default(self):
#         self.mode = "default"; self.db_path = DB_PATH
#         self.engine = None; self.uploaded_tables = []

#     def upload_file(self, file_bytes: bytes, filename: str) -> tuple[bool, str]:
#         ext = filename.rsplit(".", 1)[-1].lower()
#         upload_db = "./uploaded_data.db"

#         if ext in ("db", "sqlite", "sqlite3"):
#             # Write the uploaded SQLite directly
#             with open(upload_db, "wb") as f:
#                 f.write(file_bytes)
#             self.mode = "upload"; self.db_path = upload_db
#             self.engine = None
#             self.uploaded_tables = self._sqlite_tables(upload_db)
#             n = len(self.uploaded_tables)
#             return True, f"Loaded SQLite: {n} table{'s' if n!=1 else ''} — {', '.join(self.uploaded_tables)}"

#         if ext == "csv":
#             import pandas as pd
#             df = pd.read_csv(io.BytesIO(file_bytes))
#             tname = filename.rsplit(".",1)[0].replace("-","_").replace(" ","_")
#             self._df_to_sqlite(df, tname, upload_db)
#             self.mode = "upload"; self.db_path = upload_db
#             self.uploaded_tables = [tname]
#             return True, f"Loaded CSV → table '{tname}' ({len(df):,} rows, {len(df.columns)} cols)"

#         if ext in ("xlsx", "xls"):
#             import pandas as pd
#             xl = pd.ExcelFile(io.BytesIO(file_bytes))
#             tables = []
#             for i, sheet in enumerate(xl.sheet_names):
#                 df    = xl.parse(sheet)
#                 tname = sheet.replace("-","_").replace(" ","_")
#                 mode  = "replace" if i == 0 else "append"
#                 self._df_to_sqlite(df, tname, upload_db, if_exists=mode)
#                 tables.append(tname)
#             self.mode = "upload"; self.db_path = upload_db
#             self.uploaded_tables = tables
#             return True, f"Loaded Excel: sheets → {', '.join(tables)}"

#         return False, f"Unsupported file type: .{ext}  (use .csv / .xlsx / .db)"

#     def connect_external(self, conn_str: str) -> tuple[bool, str]:
#         try:
#             from sqlalchemy import create_engine, text
#             engine = create_engine(conn_str, pool_pre_ping=True)
#             with engine.connect() as c:
#                 c.execute(text("SELECT 1"))
#             self.engine = engine; self.mode = "external"
#             self._connection_str = conn_str
#             return True, "Connected successfully"
#         except ImportError:
#             return False, "SQLAlchemy not installed: pip install sqlalchemy"
#         except Exception as exc:
#             return False, str(exc)

#     # ── Query execution ─────────────────────────────────────────────────────────

#     def run_query(self, sql: str) -> tuple[list[dict], list[str], str | None]:
#         if self.mode == "external" and self.engine:
#             return self._run_alchemy(sql)
#         return run_query(sql, self.db_path)

#     def get_execution_plan(self, sql: str) -> str:
#         if self.mode == "external":
#             return "Execution plan not available for external databases via this interface."
#         rows, _, err = run_query(f"EXPLAIN QUERY PLAN {sql}", self.db_path)
#         if err:
#             return f"Error: {err}"
#         lines = [" | ".join(str(v) for v in r.values()) for r in rows]
#         return "\n".join(lines) if lines else "No plan returned."

#     # ── Schema ──────────────────────────────────────────────────────────────────

#     def get_schema_description(self) -> str:
#         if self.mode == "default":
#             return get_schema_description()
#         if self.mode == "upload":
#             return self._infer_sqlite_schema(self.db_path)
#         if self.mode == "external":
#             return self._infer_alchemy_schema()
#         return get_schema_description()

#     def get_schema_tree(self) -> dict[str, list[dict]]:
#         """Return {table: [{name, type}, ...]} for schema explorer."""
#         if self.mode in ("default", "upload"):
#             return self._sqlite_tree(self.db_path)
#         if self.mode == "external":
#             return self._alchemy_tree()
#         return {}

#     # ── Private helpers ─────────────────────────────────────────────────────────

#     def _df_to_sqlite(self, df, tname, db_path, if_exists="replace"):
#         conn = sqlite3.connect(db_path)
#         df.to_sql(tname, conn, if_exists=if_exists, index=False)
#         conn.close()

#     def _sqlite_tables(self, path) -> list[str]:
#         rows, _, _ = run_query(
#             "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name", path
#         )
#         return [r["name"] for r in rows]

#     def _sqlite_tree(self, path) -> dict[str, list[dict]]:
#         tree = {}
#         for tbl in self._sqlite_tables(path):
#             rows, _, _ = run_query(f"PRAGMA table_info({tbl})", path)
#             tree[tbl] = [{"name": r["name"], "type": r["type"]} for r in rows]
#         return tree

#     def _infer_sqlite_schema(self, path) -> str:
#         tables = self._sqlite_tables(path)
#         parts  = [f"DATABASE: {Path(path).name} (SQLite, uploaded)\n"]
#         for tbl in tables:
#             rows, _, _ = run_query(f"PRAGMA table_info({tbl})", path)
#             cnt,  _, _ = run_query(f"SELECT COUNT(*) AS n FROM {tbl}", path)
#             n = cnt[0]["n"] if cnt else "?"
#             parts.append(f"\nTABLE: {tbl} ({n:,} rows)")
#             parts.extend(f"  {r['name']} ({r['type']})" for r in rows)
#         return "\n".join(parts)

#     def _run_alchemy(self, sql) -> tuple[list[dict], list[str], str | None]:
#         try:
#             from sqlalchemy import text
#             with self.engine.connect() as c:
#                 res  = c.execute(text(sql))
#                 cols = list(res.keys())
#                 rows = [dict(zip(cols, row)) for row in res.fetchall()]
#             return rows, cols, None
#         except Exception as exc:
#             return [], [], str(exc)

#     def _infer_alchemy_schema(self) -> str:
#         try:
#             from sqlalchemy import inspect as sa_inspect
#             insp  = sa_inspect(self.engine)
#             parts = [f"DATABASE: {self._connection_str[:40]}… (external)\n"]
#             for tbl in insp.get_table_names():
#                 cols = insp.get_columns(tbl)
#                 parts.append(f"\nTABLE: {tbl}")
#                 parts.extend(f"  {c['name']} ({c['type']})" for c in cols)
#             return "\n".join(parts)
#         except Exception as exc:
#             return f"Schema error: {exc}"

#     def _alchemy_tree(self) -> dict[str, list[dict]]:
#         try:
#             from sqlalchemy import inspect as sa_inspect
#             insp = sa_inspect(self.engine)
#             return {
#                 tbl: [{"name": c["name"], "type": str(c["type"])}
#                       for c in insp.get_columns(tbl)]
#                 for tbl in insp.get_table_names()
#             }
#         except Exception:
#             return {}

#     @property
#     def label(self) -> str:
#         if self.mode == "default":
#             return "E-Commerce DB (default)"
#         if self.mode == "upload":
#             return f"Uploaded: {', '.join(self.uploaded_tables) or 'file'}"
#         if self.mode == "external":
#             return f"External: {self._connection_str[:30]}…"
#         return "Unknown"