# How every answer was graded

Each answer in results/<bot>/dayN.jsonl is graded against the truth: shop/factsheet.md (the store's
policies, hours, prices) and shop/orders.json (what each logged-in customer really bought). The handbook
was written from the fact sheet, so the fact sheet is the reference.

For every line, write one JSON line to results/<bot>/dayN.grades.jsonl:

{"chat": "<chat>", "turn": <turn>, "score": 2 | 1 | 0, "knew_customer": true | false | null, "kind": "general" | "personal" | "follow-up" | "rare", "note": "<one short sentence, empty when score is 2>"}

score
- 2: correct and complete for what was asked, and nothing in it is false.
- 1: correct but incomplete or vague where the facts allowed a precise answer, or it sent the customer to the
  team for something the bot should have answered itself.
- 0: something in it is false or misleading (a wrong hour, price, deadline or policy), or it promises what the
  policies do not allow, or it answers a different question.

kind
- general: a store question anyone could ask (hours, returns, payment...).
- personal: about the customer's own purchases ("can i return the rain jacket i bought", "is my hem ready",
  "how many points do i have", "i lost the receipt for...").
- follow-up: only makes sense after the previous message in the chat ("and sunday?").
- rare: something the store barely covers or does not offer (perfume, hiring, wholesale).

knew_customer (only for personal questions; null otherwise)
- true: the answer correctly used this customer's own purchase (item, date, order number, size, sale tag,
  alteration) from orders.json.
- false: it did not (asked for the receipt or order number, said it cannot see purchases, or got their purchase
  wrong). A guest (email guest-...) is never personal: their questions are general.

Dates: the chat's "date" field is today. Return windows count from the purchase date: full price 30 days, sale 14
days (store credit or exchange), purchases from Nov 1 to Dec 24 returnable until Jan 15 (full price only).
Thanksgiving Nov 26 closed; Black Friday Nov 27 8am-10pm. Regular hours Mon-Wed 10-8, Thu 10-9, Fri-Sat 10-8, Sun 11-7.
An answer that states today's hours must use the weekday of the chat's date.
