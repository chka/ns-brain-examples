"""One-off: expand factsheet.md into the long handbook Level 1 reads on every call.
Each section is written separately from the same fact sheet so the numbers agree."""
import concurrent.futures as cf
import os
import pathlib
from openai import OpenAI

HERE = pathlib.Path(__file__).parent
llm = OpenAI(base_url=os.environ.get("LLM_BASE_URL", "https://api.fightclub.pro/v1"),
             api_key=os.environ["LLM_API_KEY"])
FACTS = (HERE / "factsheet.md").read_text()

SECTIONS = [
    ("About Kestrel Row New York and how to reach us", "The store, address, subway and parking, accessibility, phone and email, who handles what.", 900),
    ("Opening hours and holiday hours", "Regular hours by day, late Thursday, last fitting room entry, every 2026 holiday date with its hours.", 700),
    ("Returns", "Full-price, sale and final-sale items, receipts and no-receipt returns, items never returnable, holiday returns, online orders returned in store or by mail, refund timing, faulty items.", 1700),
    ("Exchanges", "Sizes and colors, time limits, in store and online.", 500),
    ("Payment, sales tax and gift cards", "Every payment method and what is not accepted, Klarna limits, NYC sales tax and the clothing exemption, visitors and tax, gift cards in full.", 1300),
    ("Alterations, styling and in-store services", "Free hemming and what it covers, alteration prices, timing and rush, pickup, gift wrapping, styling appointments, holds, stock checks, dogs, restroom, Wi-Fi.", 1200),
    ("Online orders, shipping and store pickup", "Shipping costs and times, same-day courier, buy online pick up in store, order changes, where the shop ships.", 1100),
    ("Row Rewards, discounts and promotions", "Points, rewards, expiry, birthday code, student and military discounts, one discount rule, price adjustment, no price matching, Black Friday weekend.", 1200),
    ("Our product lines and size guide", "Each line with price range, fabric and fit notes; size tables for women's (US 0-16), women's jeans, men's tops and trousers, kids, shoes (US).", 2600),
    ("Care", "Care for each material.", 800),
    ("Frequently asked questions", "Forty short question-and-answer pairs customers really ask, two to four sentences each, covering the most common topics above.", 2400),
    ("Rules for the chat assistant", "What the assistant may and may not do, when to pass the customer to the store team, tone and length.", 600),
]


def write(section):
    title, brief, words = section
    r = llm.chat.completions.create(
        model="fc:anthropic/claude-sonnet-5.5", max_tokens=6000,
        messages=[{"role": "system", "content": "You write internal customer-service handbooks. Use ONLY the facts in the fact sheet; invent no new numbers, prices, policies or claims. Write as the shop's own handbook: never mention a fact sheet, a source or missing data, no editor's notes, no placeholders. Where the facts do not cover something, say the human team handles it. Plain markdown, ## for the section title, ### for subsections, tables where useful. American spelling."},
                  {"role": "user", "content": f"FACT SHEET\n{FACTS}\n\nWrite the handbook section \"{title}\" ({brief}). About {words} words."}])
    return r.choices[0].message.content.strip()


with cf.ThreadPoolExecutor(6) as ex:
    parts = list(ex.map(write, SECTIONS))
(HERE / "handbook.md").write_text("# Kestrel Row New York customer service handbook\n\n" + "\n\n".join(parts) + "\n")
print(len((HERE / "handbook.md").read_text().split()), "words")
