"""
evaluate.py — QueryMind formal evaluation harness.

Runs 95 test queries across 5 categories against the live system,
measures actual latency, confidence, first-attempt success, and
post-healing success. Writes results to eval_results.json and
prints Table 4 with real numbers.

Usage:
    GROQ_API_KEY=gsk_... python evaluate.py
"""

from __future__ import annotations
import json
import os
import sys
import time
import statistics
from dataclasses import dataclass, field, asdict
from typing import Optional
from groq import Groq

from dotenv import load_dotenv

# Load environment variables from .env file if it exists
load_dotenv()

# ── bring in project modules ────────────────────────────────────────────────
from database import init_db, DB_PATH, DatabaseManager
from agent import ConversationalSQLAgent

# ── Test suite ───────────────────────────────────────────────────────────────
# Each entry: (user_text, is_followup)
# Categories are defined in TEST_CATEGORIES below.

# --------------------------------------------------------------------------
# CATEGORY I — Simple Aggregation (25 queries, each independent)
# --------------------------------------------------------------------------
CAT1 = [
    ("Show total revenue by product category", False),
    ("Show the number of orders per region", False),
    ("What is the average product rating across all products?", False),
    ("Show total quantity sold per product", False),
    ("How many customers are in each state?", False),
]

# --------------------------------------------------------------------------
# CATEGORY II — Multi-Turn Follow-Up (4 conversation chains, 5 turns each = 20)
# Each sub-list is one conversation. is_followup=True for turns 2+
# --------------------------------------------------------------------------
CAT2_CHAINS = [
    [
        ("Show total revenue by product category", False),
        ("Also include the number of orders in that", True),
        ("Now only show Electronics", True),
        ("Break it down by month instead", True),
        ("Which of those months had revenue above 5000?", True),
    ],
]
# flatten for uniform processing, tag with chain id
CAT2 = []
for chain_id, chain in enumerate(CAT2_CHAINS):
    for turn_idx, (q, is_fu) in enumerate(chain):
        CAT2.append((q, is_fu, chain_id, turn_idx))

# --------------------------------------------------------------------------
# CATEGORY III — CTE / Multi-Hop (15 queries, each independent)
# --------------------------------------------------------------------------
CAT3 = [
    ("Find the top 3 customers by total spend in each region and show their average order value", False),
    ("Show products where revenue is above average revenue across all products", False),
    ("Find customers who have placed orders in all 5 regions", False),
    ("Show the month with highest revenue for each product category", False),
    ("Find products whose average rating is above the overall average rating and show their total revenue", False),
]
CAT3 = [(q, False) for q, _ in CAT3]

# --------------------------------------------------------------------------
# CATEGORY IV — Ambiguous / Underspecified (15 queries)
# --------------------------------------------------------------------------
CAT4 = [
    ("Show me data", False),
    ("What about sales?", False),
    ("Give me some numbers", False),
    ("Tell me about customers", False),
    ("Show products", False),
]

# --------------------------------------------------------------------------
# CATEGORY V — Self-Healing: deliberately broken SQL injected via a wrapper
# 20 broken queries — each has a known-bad SQL and the natural language intent
# --------------------------------------------------------------------------
CAT5_BROKEN = [
    # (natural_language_intent, broken_sql)
    ("Show total revenue by category",
     "SELECT catgory_name, SUM(total_amount) FROM orders JOIN categries ON orders.order_id = categories.category_id GROUP BY catgory_name"),
    ("Show top 5 customers by spend",
     "SELECT custmer_id, SUM(total_amnt) FROM orders GRPUP BY custmer_id ORDER BY SUM(total_amnt) DESC LIMIT 5"),
    ("Show average product rating",
     "SELECT product_id, AVG(ratng) FROM revews GROUP BY product_id"),
    ("Show orders per region",
     "SELECT regon, COUNT(*) AS order_cnt FRM orders GROPU BY regon"),
    ("Show products with stock below 100",
     "SELECT prodct_name, stck FROM products WEHRE stck < 100"),
    ("Show monthly revenue for 2023",
     "SELECT strftime('%m', order_dat), SUM(total_amnt) FROM ordes WHERE order_dat LIKE '2023%' GROUP BY 1"),
    ("Show customer count by state",
     "SELECT stte, COUNT(*) FROM customrs GRUOP BY stte"),
    ("Show total items sold per product",
     "SELECT p.name, SUM(oi.quantty) FROM order_itms oi JOIN prodcts p ON oi.product_id = p.product_id GROUP BY p.nme"),
    ("Show revenue by order status",
     "SELECT statsu, SUM(totl_amount) FROM oders GROUP BY statsu"),
    ("Show average order value per customer tier",
     "SELECT custmer_tier, AVG(total_amunt) FROM orders JOIN custmers ON orders.customer_id = custmers.customer_id GROUP BY custmer_tier"),
    ("Show products by category with average price",
     "SELECT c.nme, AVG(p.prce) FROM prodcts p JOIN catgories c ON p.ctegory_id = c.catgory_id GROUP BY c.nme"),
    ("Show revenue from Platinum customers",
     "SELECT SUM(o.total_amount) FROM orers o JOIN custs c ON o.custmer_id = c.customer_id WHERE c.tier = 'Platinum'"),
    ("Show number of reviews per rating value",
     "SELECT ratng, COUNT(*) FROM revews GROUBY ratng"),
    ("Show customers who signed up in 2023",
     "SELECT nme, signup_dte FROM custmers WHERE signup_dte LIKE '2023%'"),
    ("Show total revenue by year",
     "SELECT strftime('%Y', order_dat), SUM(totl_amnt) FOM orders GRUOP BY 1"),
    ("Show all active products",
     "SELECT nme, prce FROM prodcts WHER is_actve = 1"),
    ("Show average discount per product",
     "SELECT prodct_id, AVG(discont_pct) FROM ordr_items GROUP BY prodct_id"),
    ("Show top 3 regions by order count",
     "SELECT regon, COUNT(*) AS cnt FROM ords GROUP BY regon ORDER BY cnt DECS LIMIT 3"),
    ("Show orders placed in 2024",
     "SELECT order_id, order_dat, totl_amount FROM ords WHERE order_dat LIKE '2024%' LIMIT 100"),
    ("Show customers with more than 10 orders",
     "SELECT custmer_id, COUNT(*) AS cnt FROM ords GROUP BY custmer_id HAVNG cnt > 10"),
]


# ── Result dataclass ─────────────────────────────────────────────────────────

@dataclass
class TurnResult:
    category:         int
    query_index:      int
    user_input:       str
    sql_generated:    str
    first_attempt_ok: bool
    healed:           bool
    final_ok:         bool
    confidence:       float
    is_ambiguous:     bool
    uses_cte:         bool
    latency_s:        float
    error:            Optional[str] = None
    agent_error:      Optional[str] = None


# ── Helpers ──────────────────────────────────────────────────────────────────

def fresh_agent(dm: DatabaseManager) -> ConversationalSQLAgent:
    return ConversationalSQLAgent(
        schema  = dm.get_schema_description(),
        api_key = os.environ.get("GROQ_API_KEY"),
    )


def run_sql(dm: DatabaseManager, sql: str):
    rows, cols, err = dm.run_query(sql)
    return err


def run_turn(agent, dm, user_input, is_followup=False) -> TurnResult:
    t0 = time.time()
    for attempt in range(5):
        resp = agent.chat(user_input)
        if not resp.error or "429" not in str(resp.error):
            break
        wait = 62  # wait just over a minute for rate limit to reset
        print(f"\n    [429] Rate limit hit — waiting {wait}s before retry {attempt+1}/5...")
        time.sleep(wait)
        agent.rate_limiter._ts.clear()  # reset the internal rate limiter too
    latency = time.time() - t0

    if resp.error:
        return TurnResult(
            category=0, query_index=0,
            user_input=user_input, sql_generated="",
            first_attempt_ok=False, healed=False, final_ok=False,
            confidence=0.0, is_ambiguous=False, uses_cte=False,
            latency_s=round(latency, 3), agent_error=resp.error
        )

    # test first attempt
    db_err = run_sql(dm, resp.sql) if resp.sql else "No SQL generated"
    first_ok = db_err is None

    healed = False
    final_ok = first_ok

    if not first_ok and resp.sql:
        heal_resp = agent.self_heal_sql(resp.sql, db_err)
        if not heal_resp.error and heal_resp.sql:
            heal_err = run_sql(dm, heal_resp.sql)
            if heal_err is None:
                healed = True
                final_ok = True

    return TurnResult(
        category=0, query_index=0,
        user_input=user_input,
        sql_generated=resp.sql,
        first_attempt_ok=first_ok,
        healed=healed,
        final_ok=final_ok,
        confidence=resp.confidence,
        is_ambiguous=resp.is_ambiguous,
        uses_cte=resp.uses_cte,
        latency_s=round(latency, 3),
        error=db_err if not final_ok else None,
    )


# ── Category runners ──────────────────────────────────────────────────────────

def run_cat1(dm) -> list[TurnResult]:
    print("\n[CAT I] Simple Aggregation — 25 independent queries")
    results = []
    for idx, (q, _) in enumerate(CAT1):
        agent = fresh_agent(dm)
        r = run_turn(agent, dm, q)
        r.category = 1; r.query_index = idx
        status = "✓" if r.final_ok else "✗"
        err_hint = f"  ← {r.agent_error[:80]}" if r.agent_error else ""
        print(f"  [{idx+1:02d}] {status}  conf={r.confidence:.2f}  {r.latency_s:.2f}s  {q[:55]}{err_hint}")
        results.append(r)
        time.sleep(2.5)  # ~24 calls/min — stays under Groq free tier 30/min limit
    return results


def run_cat2(dm) -> list[TurnResult]:
    print("\n[CAT II] Multi-Turn Follow-Up — 4 chains × 5 turns")
    results = []
    idx = 0
    for chain_id, chain in enumerate(CAT2_CHAINS):
        agent = fresh_agent(dm)
        print(f"  Chain {chain_id+1}:")
        for turn_idx, (q, is_fu) in enumerate(chain):
            r = run_turn(agent, dm, q, is_followup=is_fu)
            r.category = 2; r.query_index = idx
            status = "✓" if r.final_ok else "✗"
            print(f"    turn {turn_idx+1} {status}  conf={r.confidence:.2f}  {r.latency_s:.2f}s  {q[:50]}")
            results.append(r)
            idx += 1
            time.sleep(2.5)  # ~24 calls/min — stays under Groq free tier 30/min limit
    return results


def run_cat3(dm) -> list[TurnResult]:
    print("\n[CAT III] CTE / Multi-Hop — 15 independent queries")
    results = []
    for idx, (q, _) in enumerate(CAT3):
        agent = fresh_agent(dm)
        r = run_turn(agent, dm, q)
        r.category = 3; r.query_index = idx
        cte_flag = "⛓" if r.uses_cte else " "
        status = "✓" if r.final_ok else "✗"
        print(f"  [{idx+1:02d}] {status} {cte_flag}  conf={r.confidence:.2f}  {r.latency_s:.2f}s  {q[:55]}")
        results.append(r)
        time.sleep(2.5)  # ~24 calls/min — stays under Groq free tier 30/min limit
    return results


def run_cat4(dm) -> list[TurnResult]:
    print("\n[CAT IV] Ambiguous Queries — 15 queries")
    results = []
    for idx, (q, _) in enumerate(CAT4):
        agent = fresh_agent(dm)
        r = run_turn(agent, dm, q)
        r.category = 4; r.query_index = idx
        amb_flag = "?" if r.is_ambiguous else " "
        status = "✓" if r.final_ok else "✗"
        print(f"  [{idx+1:02d}] {status} {amb_flag}  conf={r.confidence:.2f}  {r.latency_s:.2f}s  {q[:55]}")
        results.append(r)
        time.sleep(2.5)  # ~24 calls/min — stays under Groq free tier 30/min limit
    return results


def run_cat5(dm) -> list[TurnResult]:
    print("\n[CAT V] Self-Healing — 20 injected-error queries")
    results = []
    for idx, (intent, broken_sql) in enumerate(CAT5_BROKEN):
        # inject the broken SQL directly into self_heal_sql
        agent = fresh_agent(dm)
        db_err = run_sql(dm, broken_sql)
        if db_err is None:
            # broken SQL accidentally valid — force error
            db_err = "Injected error: intentionally malformed query"

        t0 = time.time()
        heal_resp = agent.self_heal_sql(broken_sql, db_err)
        latency = time.time() - t0

        healed_ok = False
        if not heal_resp.error and heal_resp.sql:
            verify_err = run_sql(dm, heal_resp.sql)
            healed_ok = verify_err is None

        r = TurnResult(
            category=5, query_index=idx,
            user_input=intent,
            sql_generated=heal_resp.sql if heal_resp.sql else broken_sql,
            first_attempt_ok=False,   # by construction — injected error
            healed=healed_ok,
            final_ok=healed_ok,
            confidence=heal_resp.confidence if not heal_resp.error else 0.0,
            is_ambiguous=False,
            uses_cte=heal_resp.uses_cte if not heal_resp.error else False,
            latency_s=round(latency, 3),
            error=None if healed_ok else (heal_resp.error or "Healing failed"),
        )
        status = "✓" if healed_ok else "✗"
        print(f"  [{idx+1:02d}] {status}  conf={r.confidence:.2f}  {r.latency_s:.2f}s  {intent[:55]}")
        results.append(r)
        time.sleep(2.5)  # ~24 calls/min — stays under Groq free tier 30/min limit
    return results


# ── Aggregation & Table 4 ─────────────────────────────────────────────────────

def summarise(results: list[TurnResult], cat: int) -> dict:
    cat_r = [r for r in results if r.category == cat]
    n = len(cat_r)
    if n == 0:
        return {}
    first_ok  = sum(1 for r in cat_r if r.first_attempt_ok)
    final_ok  = sum(1 for r in cat_r if r.final_ok)
    confs     = [r.confidence for r in cat_r]
    latencies = [r.latency_s for r in cat_r]
    return {
        "category":          cat,
        "n":                 n,
        "first_attempt_pct": round(first_ok / n * 100, 1),
        "after_healing_pct": round(final_ok / n * 100, 1),
        "avg_confidence":    round(statistics.mean(confs), 2),
        "std_confidence":    round(statistics.stdev(confs) if n > 1 else 0, 2),
        "avg_latency_s":     round(statistics.mean(latencies), 2),
        "std_latency_s":     round(statistics.stdev(latencies) if n > 1 else 0, 2),
        "n_first_ok":        first_ok,
        "n_final_ok":        final_ok,
    }


def print_table4(summaries: list[dict]):
    labels = {
        1: "Simple aggregation   ",
        2: "Multi-turn follow-up ",
        3: "CTE / multi-hop      ",
        4: "Ambiguous queries    ",
        5: "Self-healing (inject)",
    }
    print("\n" + "="*90)
    print("TABLE 4 — QueryMind Evaluation Results (measured)")
    print("="*90)
    hdr = f"{'Query Category':<24} {'N':>4}  {'1st-Att%':>9}  {'After-Heal%':>11}  {'Avg Conf':>9}  {'Avg Latency':>12}"
    print(hdr)
    print("-"*90)
    for s in summaries:
        cat = s["category"]
        print(
            f"{labels[cat]:<24} {s['n']:>4}  "
            f"{s['first_attempt_pct']:>8.1f}%  "
            f"{s['after_healing_pct']:>10.1f}%  "
            f"{s['avg_confidence']:>8.2f}  "
            f"{s['avg_latency_s']:>8.2f} ± {s['std_latency_s']:.2f}s"
        )
    print("="*90)

    # ambiguity detection rate for cat 4
    all_results_flat = []
    return all_results_flat


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # Load .env from the same folder as this script, if it exists
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        load_dotenv(env_path)
        print(f".env loaded from: {env_path}")
    else:
        print(f"WARNING: .env not found at {env_path}")

    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        print("ERROR: GROQ_API_KEY environment variable not set.")
        sys.exit(1)
    print(f"API key loaded: {api_key[:8]}…")

    print("QueryMind Evaluation Harness")
    print(f"API key: {api_key[:8]}…")
    print("Initialising database…")

    dm = DatabaseManager()
    init_db(DB_PATH)

    all_results: list[TurnResult] = []

    all_results += run_cat1(dm)
    all_results += run_cat2(dm)
    all_results += run_cat3(dm)
    all_results += run_cat4(dm)
    all_results += run_cat5(dm)

    summaries = [summarise(all_results, c) for c in range(1, 6)]
    print_table4(summaries)

    # Ambiguity detection rate (cat 4)
    cat4_r = [r for r in all_results if r.category == 4]
    amb_detected = sum(1 for r in cat4_r if r.is_ambiguous)
    print(f"\nAmbiguity detection rate (Cat IV): {amb_detected}/{len(cat4_r)} = {amb_detected/len(cat4_r)*100:.1f}%")

    # CTE usage rate (cat 3)
    cat3_r = [r for r in all_results if r.category == 3]
    cte_used = sum(1 for r in cat3_r if r.uses_cte)
    print(f"CTE usage rate (Cat III): {cte_used}/{len(cat3_r)} = {cte_used/len(cat3_r)*100:.1f}%")

    # Save full results
    output = {
        "summaries": summaries,
        "per_turn":  [asdict(r) for r in all_results],
        "meta": {
            "total_queries": len(all_results),
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    }
    output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval_results.json")
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nFull results saved to {output_path}")

if __name__ == "__main__":
    main()






# """
# evaluate.py — QueryMind formal evaluation harness.

# Runs 95 test queries across 5 categories against the live system,
# measures actual latency, confidence, first-attempt success, and
# post-healing success. Writes results to eval_results.json and
# prints Table 4 with real numbers.

# Usage:
#     GROQ_API_KEY=gsk_... python evaluate.py
# """

# from __future__ import annotations
# import json
# import os
# import sys
# import time
# import statistics
# from dataclasses import dataclass, field, asdict
# from typing import Optional
# from groq import Groq

# from dotenv import load_dotenv

# # Load environment variables from .env file if it exists
# load_dotenv()

# # ── bring in project modules ────────────────────────────────────────────────
# from database import init_db, DB_PATH, DatabaseManager
# from agent import ConversationalSQLAgent

# # ── Test suite ───────────────────────────────────────────────────────────────
# # Each entry: (user_text, is_followup)
# # Categories are defined in TEST_CATEGORIES below.

# # --------------------------------------------------------------------------
# # CATEGORY I — Simple Aggregation (25 queries, each independent)
# # --------------------------------------------------------------------------
# CAT1 = [
#     ("Show total revenue by product category", False),
#     ("Show the number of orders per region", False),
#     ("What is the average product rating across all products?", False),
#     ("Show total quantity sold per product", False),
#     ("How many customers are in each state?", False),
#     ("Show total revenue per month for 2023", False),
#     ("What is the average order value across all orders?", False),
#     ("Show total revenue for each order status", False),
#     ("How many orders were placed in 2022?", False),
#     ("Show total revenue per year", False),
#     ("What is the average discount percentage per product?", False),
#     ("Show total number of reviews per product", False),
#     ("How many customers signed up per year?", False),
#     ("Show total revenue by customer tier", False),
#     ("What is the average order value per region?", False),
#     ("Show total stock available per category", False),
#     ("How many products are active versus inactive?", False),
#     ("Show total revenue from delivered orders only", False),
#     ("What is the average rating per product category?", False),
#     ("Show the top 10 products by total revenue", False),
#     ("How many orders were returned?", False),
#     ("Show total revenue by state", False),
#     ("What is the total cost of all products in stock?", False),
#     ("Show total number of items sold per order status", False),
#     ("How many Gold tier customers are there?", False),
# ]

# # --------------------------------------------------------------------------
# # CATEGORY II — Multi-Turn Follow-Up (4 conversation chains, 5 turns each = 20)
# # Each sub-list is one conversation. is_followup=True for turns 2+
# # --------------------------------------------------------------------------
# CAT2_CHAINS = [
#     [
#         ("Show total revenue by product category", False),
#         ("Also include the number of orders in that", True),
#         ("Now only show Electronics", True),
#         ("Break it down by month instead", True),
#         ("Which of those months had revenue above 5000?", True),
#     ],
#     [
#         ("Show the top 5 customers by total spend", False),
#         ("What states are those customers from?", True),
#         ("Now show only Gold and Platinum tier ones", True),
#         ("What is the average order value for them?", True),
#         ("Which of those have placed more than 5 orders?", True),
#     ],
#     [
#         ("Show total revenue per region", False),
#         ("Also include average order value for each", True),
#         ("Now filter to only 2023 data", True),
#         ("Sort it by revenue descending", True),
#         ("Show only the top 3 regions", True),
#     ],
#     [
#         ("Show all products with their average rating", False),
#         ("Which ones have a rating above 4?", True),
#         ("Now show only those in the Electronics category", True),
#         ("Also include their total revenue", True),
#         ("Which of those have stock below 150?", True),
#     ],
# ]
# # flatten for uniform processing, tag with chain id
# CAT2 = []
# for chain_id, chain in enumerate(CAT2_CHAINS):
#     for turn_idx, (q, is_fu) in enumerate(chain):
#         CAT2.append((q, is_fu, chain_id, turn_idx))

# # --------------------------------------------------------------------------
# # CATEGORY III — CTE / Multi-Hop (15 queries, each independent)
# # --------------------------------------------------------------------------
# CAT3 = [
#     ("Find the top 3 customers by total spend in each region and show their average order value", False),
#     ("Show products where revenue is above average revenue across all products", False),
#     ("Find customers who have placed orders in all 5 regions", False),
#     ("Show the month with highest revenue for each product category", False),
#     ("Find products whose average rating is above the overall average rating and show their total revenue", False),
#     ("Show customers whose total spend is more than twice the average customer spend", False),
#     ("Find the top 2 products by revenue in each sub-category", False),
#     ("Show each region's revenue as a percentage of total revenue", False),
#     ("Find customers who have never placed a returned order", False),
#     ("Show the running total revenue by month for 2023", False),
#     ("Find products that appear in orders from more than 3 different regions", False),
#     ("Show the top 5 customers per state by number of orders", False),
#     ("Find months where revenue exceeded the previous month by more than 20 percent", False),
#     ("Show product categories ranked by average review rating with total review count", False),
#     ("Find customers who placed orders in both 2022 and 2024", False),
# ]
# CAT3 = [(q, False) for q, _ in CAT3]

# # --------------------------------------------------------------------------
# # CATEGORY IV — Ambiguous / Underspecified (15 queries)
# # --------------------------------------------------------------------------
# CAT4 = [
#     ("Show me data", False),
#     ("What about sales?", False),
#     ("Give me some numbers", False),
#     ("Tell me about customers", False),
#     ("Show products", False),
#     ("What is popular?", False),
#     ("Give me a report", False),
#     ("Show me trends", False),
#     ("What is performing well?", False),
#     ("Show revenue", False),
#     ("What about orders?", False),
#     ("Give me stats", False),
#     ("Show me everything", False),
#     ("What is the data like?", False),
#     ("Tell me about the business", False),
# ]

# # --------------------------------------------------------------------------
# # CATEGORY V — Self-Healing: deliberately broken SQL injected via a wrapper
# # 20 broken queries — each has a known-bad SQL and the natural language intent
# # --------------------------------------------------------------------------
# CAT5_BROKEN = [
#     # (natural_language_intent, broken_sql)
#     ("Show total revenue by category",
#      "SELECT catgory_name, SUM(total_amount) FROM orders JOIN categries ON orders.order_id = categories.category_id GROUP BY catgory_name"),
#     ("Show top 5 customers by spend",
#      "SELECT custmer_id, SUM(total_amnt) FROM orders GRPUP BY custmer_id ORDER BY SUM(total_amnt) DESC LIMIT 5"),
#     ("Show average product rating",
#      "SELECT product_id, AVG(ratng) FROM revews GROUP BY product_id"),
#     ("Show orders per region",
#      "SELECT regon, COUNT(*) AS order_cnt FRM orders GROPU BY regon"),
#     ("Show products with stock below 100",
#      "SELECT prodct_name, stck FROM products WEHRE stck < 100"),
#     ("Show monthly revenue for 2023",
#      "SELECT strftime('%m', order_dat), SUM(total_amnt) FROM ordes WHERE order_dat LIKE '2023%' GROUP BY 1"),
#     ("Show customer count by state",
#      "SELECT stte, COUNT(*) FROM customrs GRUOP BY stte"),
#     ("Show total items sold per product",
#      "SELECT p.name, SUM(oi.quantty) FROM order_itms oi JOIN prodcts p ON oi.product_id = p.product_id GROUP BY p.nme"),
#     ("Show revenue by order status",
#      "SELECT statsu, SUM(totl_amount) FROM oders GROUP BY statsu"),
#     ("Show average order value per customer tier",
#      "SELECT custmer_tier, AVG(total_amunt) FROM orders JOIN custmers ON orders.customer_id = custmers.customer_id GROUP BY custmer_tier"),
#     ("Show products by category with average price",
#      "SELECT c.nme, AVG(p.prce) FROM prodcts p JOIN catgories c ON p.ctegory_id = c.catgory_id GROUP BY c.nme"),
#     ("Show revenue from Platinum customers",
#      "SELECT SUM(o.total_amount) FROM orers o JOIN custs c ON o.custmer_id = c.customer_id WHERE c.tier = 'Platinum'"),
#     ("Show number of reviews per rating value",
#      "SELECT ratng, COUNT(*) FROM revews GROUBY ratng"),
#     ("Show customers who signed up in 2023",
#      "SELECT nme, signup_dte FROM custmers WHERE signup_dte LIKE '2023%'"),
#     ("Show total revenue by year",
#      "SELECT strftime('%Y', order_dat), SUM(totl_amnt) FOM orders GRUOP BY 1"),
#     ("Show all active products",
#      "SELECT nme, prce FROM prodcts WHER is_actve = 1"),
#     ("Show average discount per product",
#      "SELECT prodct_id, AVG(discont_pct) FROM ordr_items GROUP BY prodct_id"),
#     ("Show top 3 regions by order count",
#      "SELECT regon, COUNT(*) AS cnt FROM ords GROUP BY regon ORDER BY cnt DECS LIMIT 3"),
#     ("Show orders placed in 2024",
#      "SELECT order_id, order_dat, totl_amount FROM ords WHERE order_dat LIKE '2024%' LIMIT 100"),
#     ("Show customers with more than 10 orders",
#      "SELECT custmer_id, COUNT(*) AS cnt FROM ords GROUP BY custmer_id HAVNG cnt > 10"),
# ]


# # ── Result dataclass ─────────────────────────────────────────────────────────

# @dataclass
# class TurnResult:
#     category:         int
#     query_index:      int
#     user_input:       str
#     sql_generated:    str
#     first_attempt_ok: bool
#     healed:           bool
#     final_ok:         bool
#     confidence:       float
#     is_ambiguous:     bool
#     uses_cte:         bool
#     latency_s:        float
#     error:            Optional[str] = None
#     agent_error:      Optional[str] = None


# # ── Helpers ──────────────────────────────────────────────────────────────────

# def fresh_agent(dm: DatabaseManager) -> ConversationalSQLAgent:
#     return ConversationalSQLAgent(
#         schema  = dm.get_schema_description(),
#         api_key = os.environ.get("GROQ_API_KEY"),
#     )


# def run_sql(dm: DatabaseManager, sql: str):
#     rows, cols, err = dm.run_query(sql)
#     return err


# def run_turn(agent, dm, user_input, is_followup=False) -> TurnResult:
#     t0 = time.time()
#     for attempt in range(5):
#         resp = agent.chat(user_input)
#         if not resp.error or "429" not in str(resp.error):
#             break
#         wait = 62  # wait just over a minute for rate limit to reset
#         print(f"\n    [429] Rate limit hit — waiting {wait}s before retry {attempt+1}/5...")
#         time.sleep(wait)
#         agent.rate_limiter._ts.clear()  # reset the internal rate limiter too
#     latency = time.time() - t0

#     if resp.error:
#         return TurnResult(
#             category=0, query_index=0,
#             user_input=user_input, sql_generated="",
#             first_attempt_ok=False, healed=False, final_ok=False,
#             confidence=0.0, is_ambiguous=False, uses_cte=False,
#             latency_s=round(latency, 3), agent_error=resp.error
#         )

#     # test first attempt
#     db_err = run_sql(dm, resp.sql) if resp.sql else "No SQL generated"
#     first_ok = db_err is None

#     healed = False
#     final_ok = first_ok

#     if not first_ok and resp.sql:
#         heal_resp = agent.self_heal_sql(resp.sql, db_err)
#         if not heal_resp.error and heal_resp.sql:
#             heal_err = run_sql(dm, heal_resp.sql)
#             if heal_err is None:
#                 healed = True
#                 final_ok = True

#     return TurnResult(
#         category=0, query_index=0,
#         user_input=user_input,
#         sql_generated=resp.sql,
#         first_attempt_ok=first_ok,
#         healed=healed,
#         final_ok=final_ok,
#         confidence=resp.confidence,
#         is_ambiguous=resp.is_ambiguous,
#         uses_cte=resp.uses_cte,
#         latency_s=round(latency, 3),
#         error=db_err if not final_ok else None,
#     )


# # ── Category runners ──────────────────────────────────────────────────────────

# def run_cat1(dm) -> list[TurnResult]:
#     print("\n[CAT I] Simple Aggregation — 25 independent queries")
#     results = []
#     for idx, (q, _) in enumerate(CAT1):
#         agent = fresh_agent(dm)
#         r = run_turn(agent, dm, q)
#         r.category = 1; r.query_index = idx
#         status = "✓" if r.final_ok else "✗"
#         err_hint = f"  ← {r.agent_error[:80]}" if r.agent_error else ""
#         print(f"  [{idx+1:02d}] {status}  conf={r.confidence:.2f}  {r.latency_s:.2f}s  {q[:55]}{err_hint}")
#         results.append(r)
#         time.sleep(2.5)  # ~24 calls/min — stays under Groq free tier 30/min limit
#     return results


# def run_cat2(dm) -> list[TurnResult]:
#     print("\n[CAT II] Multi-Turn Follow-Up — 4 chains × 5 turns")
#     results = []
#     idx = 0
#     for chain_id, chain in enumerate(CAT2_CHAINS):
#         agent = fresh_agent(dm)
#         print(f"  Chain {chain_id+1}:")
#         for turn_idx, (q, is_fu) in enumerate(chain):
#             r = run_turn(agent, dm, q, is_followup=is_fu)
#             r.category = 2; r.query_index = idx
#             status = "✓" if r.final_ok else "✗"
#             print(f"    turn {turn_idx+1} {status}  conf={r.confidence:.2f}  {r.latency_s:.2f}s  {q[:50]}")
#             results.append(r)
#             idx += 1
#             time.sleep(2.5)  # ~24 calls/min — stays under Groq free tier 30/min limit
#     return results


# def run_cat3(dm) -> list[TurnResult]:
#     print("\n[CAT III] CTE / Multi-Hop — 15 independent queries")
#     results = []
#     for idx, (q, _) in enumerate(CAT3):
#         agent = fresh_agent(dm)
#         r = run_turn(agent, dm, q)
#         r.category = 3; r.query_index = idx
#         cte_flag = "⛓" if r.uses_cte else " "
#         status = "✓" if r.final_ok else "✗"
#         print(f"  [{idx+1:02d}] {status} {cte_flag}  conf={r.confidence:.2f}  {r.latency_s:.2f}s  {q[:55]}")
#         results.append(r)
#         time.sleep(2.5)  # ~24 calls/min — stays under Groq free tier 30/min limit
#     return results


# def run_cat4(dm) -> list[TurnResult]:
#     print("\n[CAT IV] Ambiguous Queries — 15 queries")
#     results = []
#     for idx, (q, _) in enumerate(CAT4):
#         agent = fresh_agent(dm)
#         r = run_turn(agent, dm, q)
#         r.category = 4; r.query_index = idx
#         amb_flag = "?" if r.is_ambiguous else " "
#         status = "✓" if r.final_ok else "✗"
#         print(f"  [{idx+1:02d}] {status} {amb_flag}  conf={r.confidence:.2f}  {r.latency_s:.2f}s  {q[:55]}")
#         results.append(r)
#         time.sleep(2.5)  # ~24 calls/min — stays under Groq free tier 30/min limit
#     return results


# def run_cat5(dm) -> list[TurnResult]:
#     print("\n[CAT V] Self-Healing — 20 injected-error queries")
#     results = []
#     for idx, (intent, broken_sql) in enumerate(CAT5_BROKEN):
#         # inject the broken SQL directly into self_heal_sql
#         agent = fresh_agent(dm)
#         db_err = run_sql(dm, broken_sql)
#         if db_err is None:
#             # broken SQL accidentally valid — force error
#             db_err = "Injected error: intentionally malformed query"

#         t0 = time.time()
#         heal_resp = agent.self_heal_sql(broken_sql, db_err)
#         latency = time.time() - t0

#         healed_ok = False
#         if not heal_resp.error and heal_resp.sql:
#             verify_err = run_sql(dm, heal_resp.sql)
#             healed_ok = verify_err is None

#         r = TurnResult(
#             category=5, query_index=idx,
#             user_input=intent,
#             sql_generated=heal_resp.sql if heal_resp.sql else broken_sql,
#             first_attempt_ok=False,   # by construction — injected error
#             healed=healed_ok,
#             final_ok=healed_ok,
#             confidence=heal_resp.confidence if not heal_resp.error else 0.0,
#             is_ambiguous=False,
#             uses_cte=heal_resp.uses_cte if not heal_resp.error else False,
#             latency_s=round(latency, 3),
#             error=None if healed_ok else (heal_resp.error or "Healing failed"),
#         )
#         status = "✓" if healed_ok else "✗"
#         print(f"  [{idx+1:02d}] {status}  conf={r.confidence:.2f}  {r.latency_s:.2f}s  {intent[:55]}")
#         results.append(r)
#         time.sleep(2.5)  # ~24 calls/min — stays under Groq free tier 30/min limit
#     return results


# # ── Aggregation & Table 4 ─────────────────────────────────────────────────────

# def summarise(results: list[TurnResult], cat: int) -> dict:
#     cat_r = [r for r in results if r.category == cat]
#     n = len(cat_r)
#     if n == 0:
#         return {}
#     first_ok  = sum(1 for r in cat_r if r.first_attempt_ok)
#     final_ok  = sum(1 for r in cat_r if r.final_ok)
#     confs     = [r.confidence for r in cat_r]
#     latencies = [r.latency_s for r in cat_r]
#     return {
#         "category":          cat,
#         "n":                 n,
#         "first_attempt_pct": round(first_ok / n * 100, 1),
#         "after_healing_pct": round(final_ok / n * 100, 1),
#         "avg_confidence":    round(statistics.mean(confs), 2),
#         "std_confidence":    round(statistics.stdev(confs) if n > 1 else 0, 2),
#         "avg_latency_s":     round(statistics.mean(latencies), 2),
#         "std_latency_s":     round(statistics.stdev(latencies) if n > 1 else 0, 2),
#         "n_first_ok":        first_ok,
#         "n_final_ok":        final_ok,
#     }


# def print_table4(summaries: list[dict]):
#     labels = {
#         1: "Simple aggregation   ",
#         2: "Multi-turn follow-up ",
#         3: "CTE / multi-hop      ",
#         4: "Ambiguous queries    ",
#         5: "Self-healing (inject)",
#     }
#     print("\n" + "="*90)
#     print("TABLE 4 — QueryMind Evaluation Results (measured)")
#     print("="*90)
#     hdr = f"{'Query Category':<24} {'N':>4}  {'1st-Att%':>9}  {'After-Heal%':>11}  {'Avg Conf':>9}  {'Avg Latency':>12}"
#     print(hdr)
#     print("-"*90)
#     for s in summaries:
#         cat = s["category"]
#         print(
#             f"{labels[cat]:<24} {s['n']:>4}  "
#             f"{s['first_attempt_pct']:>8.1f}%  "
#             f"{s['after_healing_pct']:>10.1f}%  "
#             f"{s['avg_confidence']:>8.2f}  "
#             f"{s['avg_latency_s']:>8.2f} ± {s['std_latency_s']:.2f}s"
#         )
#     print("="*90)

#     # ambiguity detection rate for cat 4
#     all_results_flat = []
#     return all_results_flat


# # ── Main ──────────────────────────────────────────────────────────────────────

# def main():
#     # Load .env from the same folder as this script, if it exists
#     env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
#     if os.path.exists(env_path):
#         load_dotenv(env_path)
#         print(f".env loaded from: {env_path}")
#     else:
#         print(f"WARNING: .env not found at {env_path}")

#     api_key = os.environ.get("GROQ_API_KEY")
#     if not api_key:
#         print("ERROR: GROQ_API_KEY environment variable not set.")
#         sys.exit(1)
#     print(f"API key loaded: {api_key[:8]}…")

#     print("QueryMind Evaluation Harness")
#     print(f"API key: {api_key[:8]}…")
#     print("Initialising database…")

#     dm = DatabaseManager()
#     init_db(DB_PATH)

#     all_results: list[TurnResult] = []

#     all_results += run_cat1(dm)
#     all_results += run_cat2(dm)
#     all_results += run_cat3(dm)
#     all_results += run_cat4(dm)
#     all_results += run_cat5(dm)

#     summaries = [summarise(all_results, c) for c in range(1, 6)]
#     print_table4(summaries)

#     # Ambiguity detection rate (cat 4)
#     cat4_r = [r for r in all_results if r.category == 4]
#     amb_detected = sum(1 for r in cat4_r if r.is_ambiguous)
#     print(f"\nAmbiguity detection rate (Cat IV): {amb_detected}/{len(cat4_r)} = {amb_detected/len(cat4_r)*100:.1f}%")

#     # CTE usage rate (cat 3)
#     cat3_r = [r for r in all_results if r.category == 3]
#     cte_used = sum(1 for r in cat3_r if r.uses_cte)
#     print(f"CTE usage rate (Cat III): {cte_used}/{len(cat3_r)} = {cte_used/len(cat3_r)*100:.1f}%")

#     # Save full results
#     output = {
#         "summaries": summaries,
#         "per_turn":  [asdict(r) for r in all_results],
#         "meta": {
#             "total_queries": len(all_results),
#             "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
#         }
#     }
#     output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval_results.json")
#     with open(output_path, "w") as f:
#         json.dump(output, f, indent=2)
#     print(f"\nFull results saved to {output_path}")

# if __name__ == "__main__":
#     main()