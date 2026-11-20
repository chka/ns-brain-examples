"""Write the week of pretend customers for the New York store, and the order system they buy from.

No model writes these. People who know they are talking to a bot type short, mechanical
questions ("store hours?", "return policy"), and in a real store a handful of them make up most
of the traffic, asked in the same few words again and again. So the messages are drawn from
fixed pools: each common question has a few phrasings and the plainest one is picked most often.
A seeded random generator makes the week the same on every run.

Writes orders.json and traffic/day1.json to day7.json (Monday 16 to Sunday 22 November 2026).
"""
import json
import pathlib
import random
from datetime import date, timedelta

HERE = pathlib.Path(__file__).parent
rng = random.Random(2026)
START = date(2026, 11, 16)

# Common questions: (phrasings, weight). The first phrasing is the usual one and is picked
# 60% of the time; the others share the rest.
COMMON = [
    (["store hours?", "what are your hours", "what time do you close today", "hours today", "what time do you open"], 12),
    (["return policy", "what is your return policy", "how many days do i have to return", "can i return something"], 10),
    (["where are you located", "address?", "whats the address", "where is the store"], 6),
    (["do you take apple pay", "do you accept apple pay", "apple pay?"], 3),
    (["do you take amex", "do you accept amex", "amex ok?"], 2),
    (["can i return without a receipt", "no receipt return?", "i lost my receipt can i still return"], 4),
    (["can i return sale items", "are sale items returnable", "sale returns?"], 4),
    (["exchange policy", "can i exchange for a different size", "can i swap sizes"], 4),
    (["are you open on thanksgiving", "thanksgiving hours", "open thanksgiving?"], 0),     # weight grows during the week
    (["black friday hours", "what time do you open on black friday", "black friday sale?"], 0),
    (["are you open sunday", "sunday hours", "open sunday?"], 3),
    (["do you sell gift cards", "gift cards?", "can i buy a gift card"], 3),
    (["do gift cards expire", "gift card expiry?"], 1),
    (["do you do alterations", "alterations?", "do you hem jeans"], 3),
    (["is hemming free", "how much is hemming"], 2),
    (["closest subway", "which subway stop", "nearest train"], 2),
    (["is there parking", "parking nearby?", "where can i park"], 2),
    (["do you ship", "do you ship to california", "shipping cost?"], 2),
    (["can i buy online and pick up in store", "do you do store pickup", "bopis?"], 2),
    (["student discount?", "do you have a student discount"], 2),
    (["do you price match", "price match?"], 1),
    (["do you take klarna", "klarna?", "can i pay in installments"], 1),
    (["is there sales tax on clothes", "sales tax?", "do i pay tax on jeans"], 1),
    (["do you do gift wrapping", "gift wrap?"], 1),
    (["are dogs allowed", "can i bring my dog"], 1),
    (["do harbour jeans run small", "do the jeans run small"], 2),
    (["how do i wash merino", "merino care"], 1),
]

# Rare questions, each asked once or twice in the week.
RARE = ["do you do wholesale", "are you hiring", "do you sell perfume", "can i bring my stroller in",
        "do you have a restroom", "is the store wheelchair accessible", "do you ship to canada",
        "can i pay with venmo", "do you take checks", "is there a tax refund for tourists",
        "can i book a stylist", "do you do personal shopping", "do you have wifi",
        "do you sell mens suits", "do you have kids shoes", "can you hold an item for me",
        "do you have the maam jacket in black size M", "when do you restock harbour jeans",
        "military discount?", "do you do same day delivery"]

# Follow-ups that only make sense after the question they follow (keyed by its index in COMMON).
FOLLOW_UPS = {0: ["and sunday?", "and thursday?", "open late any day?"],
              1: ["what about sale items", "and without a receipt?", "is it a refund or store credit"],
              2: ["closest subway?", "is there parking"],
              10: ["and saturday?"], 11: ["how much are they", "can i buy one online"],
              13: ["how long does it take", "is it free"], 17: ["how much is shipping", "how long does it take"]}

FIRST = ["Maya", "Jordan", "Priya", "Marcus", "Sofia", "Ethan", "Chloe", "Diego", "Hannah", "Kevin",
         "Aisha", "Ryan", "Olivia", "Daniel", "Grace", "Leo", "Nina", "Sam", "Emma", "Tyler",
         "Rosa", "Ben", "Zoe", "Omar"]
LAST = ["Chen", "Rivera", "Patel", "Brooks", "Rossi", "Walsh", "Kim", "Lopez", "Weiss", "Nguyen",
        "Khan", "Murphy", "Green", "Cohen", "Park", "Silva", "Novak", "Reed", "Clarke", "Hughes",
        "Diaz", "Fischer", "Hart", "Malik"]
ITEMS = [("Harbour high-rise jeans", ["27", "28", "29", "30"], 118), ("Corrib slim shirt", ["S", "M", "L"], 78),
         ("Atlantic merino crew", ["S", "M", "L"], 138), ("Maam waxed jacket", ["M", "L"], 285),
         ("Claddagh wrap dress", ["4", "6", "8", "10"], 160), ("Burren Chelsea boots", ["8", "9", "10", "11"], 235),
         ("Inis linen trousers", ["S", "M", "L"], 98), ("Puffin rain jacket", ["4", "6", "8"], 58)]
COLOURS = ["black", "navy", "olive", "oatmeal", "indigo", "forest green", "charcoal", "rust"]

# Questions about the customer's own purchases, filled from their orders.
PERSONAL = ["can i return the {item} i bought", "i bought the {item}, can i return it",
            "what size {item} did i buy", "the {item} is too small can i exchange it",
            "i lost the receipt for the {item}", "how many points do i have",
            "is my hem ready", "how long do i have to return the {item}"]


def make_customers():
    customers = {}
    for i in range(24):
        orders = []
        for n in range(rng.choice([1, 1, 2])):
            bought = START - timedelta(days=rng.randint(2, 40))
            items = []
            for name, sizes, price in rng.sample(ITEMS, rng.choice([1, 2])):
                sale = rng.random() < 0.25
                items.append({"item": f"{name}, {rng.choice(COLOURS)}", "size": rng.choice(sizes),
                              "price": round(price * 0.7) if sale else price,
                              "tag": f"SALE (was ${price})" if sale else "full price"})
            order = {"order": f"NY-{2400 + i * 3 + n}", "where": rng.choice(["in store", "in store", "online"]),
                     "date": bought.isoformat(), "items": items}
            if any(it["item"].startswith(("Harbour", "Inis")) for it in items) and rng.random() < 0.6:
                order["alteration"] = {"what": "hem", "ready": (bought + timedelta(days=7)).isoformat()}
            orders.append(order)
        customers[f"{FIRST[i].lower()}.{LAST[i].lower()}@example.com"] = {"name": f"{FIRST[i]} {LAST[i]}", "orders": orders}
    return customers


def weights(day):
    w = [c[1] for c in COMMON]
    w[8] = 1 + day        # Thanksgiving questions grow as the holiday nears
    w[9] = day            # and so do Black Friday ones
    return w


def phrasing(options):
    return options[0] if rng.random() < 0.6 else rng.choice(options[1:])


def make_day(n, customers):
    day = START + timedelta(days=n - 1)
    chats, count = [], 0
    while count < 50:
        email = rng.choice(list(customers)) if rng.random() < 0.45 else None
        messages = []
        if email and rng.random() < 0.5:
            items = [it for o in customers[email]["orders"] for it in o["items"]]
            short = rng.choice(items)["item"].split(",")[0].split(" ", 1)[1].lower()
            messages.append(rng.choice(PERSONAL).format(item=short))
        elif rng.random() < 0.08:
            messages.append(rng.choice(RARE))
        else:
            k = rng.choices(range(len(COMMON)), weights(n))[0]
            messages.append(phrasing(COMMON[k][0]))
            if k in FOLLOW_UPS and rng.random() < 0.35:
                messages.append(rng.choice(FOLLOW_UPS[k]))
        if rng.random() < 0.3:
            messages.append(phrasing(COMMON[rng.choices(range(len(COMMON)), weights(n))[0]][0]))
        messages = messages[:50 - count]
        count += len(messages)
        chats.append({"date": day.isoformat(), "email": email or f"guest-{n}-{len(chats)}@example.com",
                      "messages": messages})
    return {"chats": chats}


if __name__ == "__main__":
    customers = make_customers()
    (HERE / "orders.json").write_text(json.dumps({
        "_about": "The store's order system, invented. Customers log in to the chat with their email; Level 3 "
                  "copies a customer's purchases into the brain the first time they log in. Levels 1 and 2 never see it.",
        "customers": customers}, indent=1) + "\n")
    (HERE / "traffic").mkdir(exist_ok=True)
    for n in range(1, 8):
        (HERE / "traffic" / f"day{n}.json").write_text(json.dumps(make_day(n, customers), indent=1) + "\n")
    print("wrote orders.json and 7 days")
