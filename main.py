from fastapi import FastAPI, Request
from slack_bolt import App
from slack_bolt.adapter.fastapi import SlackRequestHandler
import os
import re
import math
from collections import defaultdict
from itertools import combinations
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

# Initialize Slack app
slack_app = App(
    token=os.getenv("SLACK_BOT_TOKEN"),          # must start with xoxb-
    signing_secret=os.getenv("SLACK_SIGNING_SECRET")
)

# Initialize Supabase client
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

QUESTION_COLS = ["question_1", "question_2", "question_3", "question_4", "question_5"]
STOCK_COLS = ["stock_1", "stock_2", "stock_3", "stock_4", "stock_5"]


# --------------------
# Shared data fetching
# --------------------
def fetch_merged_data():
    """Fetch recommendations + responses, join on proxy_id, drop incomplete users.
    Returns (merged_rows, stock_stats dict, resp_map).
    """
    recs = supabase.table("recommendations").select("proxy_id," + ",".join(STOCK_COLS)).order("synced_at").execute()
    resp = supabase.table("responses").select("proxy_id," + ",".join(QUESTION_COLS)).execute()

    resp_map = {
        r["proxy_id"]: r for r in resp.data
        if all(r.get(col) is not None for col in QUESTION_COLS)
    }

    merged = []
    for row in recs.data:
        pid = row["proxy_id"]
        if pid in resp_map:
            merged.append({**row, **resp_map[pid]})

    # Per-stock sentiment
    stock_stats = defaultdict(lambda: {"yes": 0, "no": 0, "maybe": 0})
    for row in merged:
        for s_col, q_col in zip(STOCK_COLS, QUESTION_COLS):
            stock = row.get(s_col)
            vote = row.get(q_col)
            if stock and vote:
                if vote == "YES":
                    stock_stats[stock]["yes"] += 1
                elif vote == "NO":
                    stock_stats[stock]["no"] += 1
                elif vote == "MAYBE":
                    stock_stats[stock]["maybe"] += 1

    return merged, dict(stock_stats), resp_map


# --------------------
# 1. Vote Summary
# --------------------
def get_vote_summary():
    result = supabase.table("responses").select(",".join(QUESTION_COLS)).execute()
    rows = [r for r in result.data if all(r.get(col) is not None for col in QUESTION_COLS)]

    total = 0
    yes = no = maybe = 0

    for row in rows:
        for col in QUESTION_COLS:
            val = row.get(col)
            total += 1
            if val == "YES":
                yes += 1
            elif val == "NO":
                no += 1
            elif val == "MAYBE":
                maybe += 1

    yes_pct = (yes / total * 100) if total else 0
    no_pct = (no / total * 100) if total else 0
    maybe_pct = (maybe / total * 100) if total else 0
    positive = yes + maybe
    positive_pct = (positive / total * 100) if total else 0

    positive_users = sum(
        1 for row in rows
        if any(row.get(col) in ("YES", "MAYBE") for col in QUESTION_COLS)
    )
    hit_rate = (positive_users / len(rows) * 100) if rows else 0

    return (
        f"*Vote Summary*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Total Users: *{len(rows)}*\n"
        f"Total Votes: *{total}*\n\n"
        f"Yes: *{yes}* ({yes_pct:.1f}%)\n"
        f"Maybe: *{maybe}* ({maybe_pct:.1f}%)\n"
        f"No: *{no}* ({no_pct:.1f}%)\n\n"
        f"Positive Intent (Yes + Maybe): *{positive}* ({positive_pct:.1f}%)\n"
        f"Hit Rate (users with >=1 Yes/Maybe): *{positive_users}/{len(rows)}* ({hit_rate:.1f}%)\n"
    )


# --------------------
# 2. Spread Analysis
# --------------------
def get_spread_analysis():
    merged, stock_stats, _ = fetch_merged_data()

    all_stocks = set(stock_stats.keys())
    total_universe = len(all_stocks)
    total_users = len(merged)

    percentiles = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    lines = [
        f"*Recommendation Spread*",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"Matched Users: *{total_users}*  |  Stock Universe: *{total_universe}*\n",
        f"```",
        f"{'Pctl':>5} | {'Users':>5} | {'Recs':>5} | {'Uniq':>5} | {'U/R %':>6} | {'Cover %':>7}",
        f"{'─'*5}-+-{'─'*5}-+-{'─'*5}-+-{'─'*5}-+-{'─'*6}-+-{'─'*7}",
    ]

    for pct in percentiles:
        n = int(math.ceil(total_users * pct / 100))
        subset = merged[:n]
        stocks = []
        for row in subset:
            for col in STOCK_COLS:
                val = row.get(col)
                if val:
                    stocks.append(val)
        unique = len(set(stocks))
        total = len(stocks)
        lines.append(
            f"{pct:>4}% | {n:>5} | {total:>5} | {unique:>5} | "
            f"{(unique/total*100 if total else 0):>5.1f}% | "
            f"{(unique/total_universe*100 if total_universe else 0):>6.1f}%"
        )

    lines.append("```")
    lines.append(f"\n_U/R % = Unique / Total Recs  |  Cover % = Unique / {total_universe} stocks_")
    return "\n".join(lines)


# --------------------
# 3. Popularity (per-stock sentiment)
# --------------------
def get_popularity(top_n=15):
    _, stock_stats, _ = fetch_merged_data()

    ranked = sorted(
        stock_stats.items(),
        key=lambda x: x[1]["yes"] + x[1]["no"] + x[1]["maybe"],
        reverse=True,
    )[:top_n]

    max_name = max(len(s) for s, _ in ranked) if ranked else 10

    lines = [
        f"*Top {len(ranked)} Most Recommended Stocks*",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"Total unique stocks: *{len(stock_stats)}*\n",
        f"```",
        f"{'Stock':<{max_name}} | {'Recs':>4} | {'Yes':>3} | {'Mbe':>3} | {'No':>3} | {'Pos %':>6}",
        f"{'─'*max_name}-+-{'─'*4}-+-{'─'*3}-+-{'─'*3}-+-{'─'*3}-+-{'─'*6}",
    ]

    for stock, counts in ranked:
        t = counts["yes"] + counts["no"] + counts["maybe"]
        pos = counts["yes"] + counts["maybe"]
        lines.append(
            f"{stock:<{max_name}} | {t:>4} | {counts['yes']:>3} | {counts['maybe']:>3} | {counts['no']:>3} | {(pos/t*100 if t else 0):>5.1f}%"
        )

    lines.append("```")
    return "\n".join(lines)


# --------------------
# 4. Head vs Tail
# --------------------
def get_headtail():
    _, stock_stats, _ = fetch_merged_data()

    ranked = sorted(
        stock_stats.items(),
        key=lambda x: x[1]["yes"] + x[1]["no"] + x[1]["maybe"],
        reverse=True,
    )

    total_stocks = len(ranked)
    cutoff = max(1, int(math.ceil(total_stocks * 0.2)))

    head = ranked[:cutoff]
    tail = ranked[cutoff:]

    def group_stats(group):
        yes = sum(c["yes"] for _, c in group)
        no = sum(c["no"] for _, c in group)
        maybe = sum(c["maybe"] for _, c in group)
        total = yes + no + maybe
        pos = yes + maybe
        return {
            "stocks": len(group),
            "total": total,
            "yes": yes, "no": no, "maybe": maybe,
            "pos_pct": round(pos / total * 100, 1) if total else 0,
        }

    h = group_stats(head)
    t = group_stats(tail)

    return (
        f"*Head vs Tail Analysis*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Top 20% = Popular stocks  |  Bottom 80% = Niche stocks\n\n"
        f"```"
        f"\n{'':>12} | {'Stocks':>6} | {'Votes':>5} | {'Yes':>4} | {'Mbe':>4} | {'No':>4} | {'Pos %':>6}"
        f"\n{'─'*12}-+-{'─'*6}-+-{'─'*5}-+-{'─'*4}-+-{'─'*4}-+-{'─'*4}-+-{'─'*6}"
        f"\n{'Popular':>12} | {h['stocks']:>6} | {h['total']:>5} | {h['yes']:>4} | {h['maybe']:>4} | {h['no']:>4} | {h['pos_pct']:>5.1f}%"
        f"\n{'Niche':>12} | {t['stocks']:>6} | {t['total']:>5} | {t['yes']:>4} | {t['maybe']:>4} | {t['no']:>4} | {t['pos_pct']:>5.1f}%"
        f"\n```"
        f"\n\n_If Popular pos% >> Niche pos%, the engine over-indexes on well-known stocks._\n"
        f"_If roughly equal, popularity doesn't predict user interest._"
    )


# --------------------
# 5. Pareto / Concentration
# --------------------
def get_pareto():
    _, stock_stats, _ = fetch_merged_data()

    ranked = sorted(
        stock_stats.items(),
        key=lambda x: x[1]["yes"] + x[1]["no"] + x[1]["maybe"],
        reverse=True,
    )

    total_recs = sum(c["yes"] + c["no"] + c["maybe"] for _, c in ranked)
    total_stocks = len(ranked)

    checkpoints = [5, 10, 20, 50, 100]
    lines = [
        f"*Concentration / Pareto Analysis*",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"Total stocks: *{total_stocks}*  |  Total votes: *{total_recs}*\n",
        f"```",
        f"{'Top N':>7} | {'% of Stocks':>11} | {'Recs':>5} | {'% of All Recs':>13}",
        f"{'─'*7}-+-{'─'*11}-+-{'─'*5}-+-{'─'*13}",
    ]

    for n in checkpoints:
        if n > total_stocks:
            n = total_stocks
        subset = ranked[:n]
        subset_recs = sum(c["yes"] + c["no"] + c["maybe"] for _, c in subset)
        lines.append(
            f"{'Top '+str(n):>7} | {(n/total_stocks*100):>10.1f}% | {subset_recs:>5} | {(subset_recs/total_recs*100 if total_recs else 0):>12.1f}%"
        )

    lines.append("```")
    lines.append(f"\n_If top 20% of stocks account for >60% of recs, concentration is high._")
    return "\n".join(lines)


# --------------------
# 6. Frequency vs Sentiment
# --------------------
def get_frequency():
    _, stock_stats, _ = fetch_merged_data()

    buckets = [
        ("1 rec", 1, 1),
        ("2 recs", 2, 2),
        ("3-5 recs", 3, 5),
        ("6-10 recs", 6, 10),
        ("11+ recs", 11, 9999),
    ]

    lines = [
        f"*Frequency vs Sentiment*",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"Do stocks recommended more often get better sentiment?\n",
        f"```",
        f"{'Bucket':>10} | {'Stocks':>6} | {'Votes':>5} | {'Yes%':>5} | {'Mbe%':>5} | {'No%':>5} | {'Pos%':>5}",
        f"{'─'*10}-+-{'─'*6}-+-{'─'*5}-+-{'─'*5}-+-{'─'*5}-+-{'─'*5}-+-{'─'*5}",
    ]

    for label, lo, hi in buckets:
        group = [
            (s, c) for s, c in stock_stats.items()
            if lo <= (c["yes"] + c["no"] + c["maybe"]) <= hi
        ]
        if not group:
            lines.append(f"{label:>10} | {'--':>6} | {'--':>5} | {'--':>5} | {'--':>5} | {'--':>5} | {'--':>5}")
            continue

        yes = sum(c["yes"] for _, c in group)
        no = sum(c["no"] for _, c in group)
        maybe = sum(c["maybe"] for _, c in group)
        total = yes + no + maybe
        pos = yes + maybe

        lines.append(
            f"{label:>10} | {len(group):>6} | {total:>5} | "
            f"{(yes/total*100 if total else 0):>4.1f}% | "
            f"{(maybe/total*100 if total else 0):>4.1f}% | "
            f"{(no/total*100 if total else 0):>4.1f}% | "
            f"{(pos/total*100 if total else 0):>4.1f}%"
        )

    lines.append("```")
    lines.append(f"\n_If Pos% is flat across buckets, frequency doesn't predict sentiment._\n"
                 f"_If Pos% rises with frequency, popular stocks genuinely perform better._")
    return "\n".join(lines)


# --------------------
# 7. Overlap Score
# --------------------
def get_overlap():
    merged, _, _ = fetch_merged_data()

    # Build stock sets per user
    user_stocks = []
    for row in merged:
        stocks = set()
        for col in STOCK_COLS:
            val = row.get(col)
            if val:
                stocks.add(val)
        if stocks:
            user_stocks.append(stocks)

    total_users = len(user_stocks)

    if total_users < 2:
        return "Not enough users to compute overlap (need at least 2)."

    # Sample if too many pairs (n choose 2 can be huge)
    total_pairs = total_users * (total_users - 1) // 2

    if total_pairs <= 5000:
        overlaps = []
        for a, b in combinations(user_stocks, 2):
            shared = len(a & b)
            avg_size = (len(a) + len(b)) / 2
            overlaps.append(shared / avg_size * 100 if avg_size else 0)
    else:
        # Random sampling for large user counts
        import random
        random.seed(42)
        overlaps = []
        for _ in range(5000):
            i, j = random.sample(range(total_users), 2)
            a, b = user_stocks[i], user_stocks[j]
            shared = len(a & b)
            avg_size = (len(a) + len(b)) / 2
            overlaps.append(shared / avg_size * 100 if avg_size else 0)

    avg_overlap = sum(overlaps) / len(overlaps)
    zero_overlap = sum(1 for o in overlaps if o == 0)
    zero_pct = zero_overlap / len(overlaps) * 100
    full_overlap = sum(1 for o in overlaps if o == 100)
    full_pct = full_overlap / len(overlaps) * 100

    # Distribution buckets
    buckets = [(0, 0), (1, 20), (21, 40), (41, 60), (61, 80), (81, 100)]
    bucket_labels = ["0%", "1-20%", "21-40%", "41-60%", "61-80%", "81-100%"]

    lines = [
        f"*User Overlap Analysis*",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"Users: *{total_users}*  |  Pairs sampled: *{len(overlaps)}*\n",
        f"Average overlap: *{avg_overlap:.1f}%*\n",
        f"```",
        f"{'Overlap':>10} | {'Pairs':>6} | {'%':>6}",
        f"{'─'*10}-+-{'─'*6}-+-{'─'*6}",
    ]

    for (lo, hi), label in zip(buckets, bucket_labels):
        count = sum(1 for o in overlaps if lo <= o <= hi)
        lines.append(f"{label:>10} | {count:>6} | {(count/len(overlaps)*100):>5.1f}%")

    lines.append("```")

    if avg_overlap > 40:
        verdict = "_High overlap — users are getting very similar stock baskets._"
    elif avg_overlap > 15:
        verdict = "_Moderate overlap — some common picks but decent personalization._"
    else:
        verdict = "_Low overlap — recommendations are well-personalized._"

    lines.append(f"\n{verdict}")
    return "\n".join(lines)


# --------------------
# Help
# --------------------
HELP_TEXT = (
    "*Available Commands*\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "*summary* — Overall vote breakdown: total votes, Yes/No/Maybe counts and %, "
    "positive intent, and hit rate.\n\n"
    "*spread* — How diverse are recommendations? Shows unique stock count "
    "as you include more users (by percentile).\n\n"
    "*popularity* — Top 15 most recommended stocks with per-stock "
    "Yes/Maybe/No sentiment breakdown.\n\n"
    "*headtail* — Splits stocks into Popular (top 20%) vs Niche (bottom 80%) "
    "and compares positive intent between the two groups.\n\n"
    "*pareto* — Concentration check. What % of all recommendations come from "
    "the top 5, 10, 20, 50, 100 stocks?\n\n"
    "*frequency* — Buckets stocks by how often they're recommended (1, 2, 3-5, 6-10, 11+) "
    "and shows sentiment per bucket. Tests if being recommended more = better received.\n\n"
    "*overlap* — Average % of stocks shared between any two users. "
    "High overlap = everyone gets the same picks. Low = personalized.\n\n"
    "*help* — This message.\n"
)


# --------------------
# Command routing
# --------------------
COMMANDS = {
    "summary": lambda: get_vote_summary(),
    "stats": lambda: get_vote_summary(),
    "votes": lambda: get_vote_summary(),
    "spread": lambda: get_spread_analysis(),
    "distribution": lambda: get_spread_analysis(),
    "popularity": lambda: get_popularity(),
    "popular": lambda: get_popularity(),
    "bias": lambda: get_popularity(),
    "headtail": lambda: get_headtail(),
    "head vs tail": lambda: get_headtail(),
    "pareto": lambda: get_pareto(),
    "concentration": lambda: get_pareto(),
    "frequency": lambda: get_frequency(),
    "freq": lambda: get_frequency(),
    "overlap": lambda: get_overlap(),
    "help": lambda: HELP_TEXT,
}


def get_last_synced():
    """Fetch last sync times from the sync_metadata table."""
    try:
        result = supabase.table("sync_metadata").select("source, last_synced_ist").execute()
        times = {r["source"]: r["last_synced_ist"] for r in result.data}
        recs_time = times.get("recommendations", "never")
        resp_time = times.get("responses", "never")
        return f"\n\n_DB last updated — Recommendations: {recs_time} | Responses: {resp_time}_"
    except Exception:
        return ""


def route_command(text):
    """Match text to a command and return the response string."""
    handler = COMMANDS.get(text)
    if handler:
        return handler() + get_last_synced()
    return HELP_TEXT


# --------------------
# Message in DMs & channels
# --------------------
@slack_app.event("message")
def handle_message(event, say):
    if event.get("subtype") == "bot_message":
        return
    text = event.get("text", "").strip().lower()
    say(route_command(text))


# --------------------
# Mentions (@yourbot hi)
# --------------------
@slack_app.event("app_mention")
def handle_mention(event, say):
    text = re.sub(r"<@[A-Z0-9]+>\s*", "", event.get("text", "")).strip().lower()
    say(route_command(text))


# --------------------
# FastAPI setup
# --------------------
handler = SlackRequestHandler(slack_app)
app = FastAPI()

@app.post("/slack/events")
async def slack_events(req: Request):
    body = await req.json()

    # Slack URL verification handshake
    if body.get("type") == "url_verification":
        return {"challenge": body["challenge"]}

    # Pass all other events to Slack Bolt
    return await handler.handle(req)
