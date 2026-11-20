# How the answers are judged

Each item in a pack is one message a customer sent to the chat assistant of Kestrel Row, a clothing
store in New York, with several answers to it written by different bots, labelled with letters
in a random order. Score every answer from 0 to 100 against the truth:

- `shop/factsheet.md`: the store's policies, hours and prices. It is the reference; nothing else is.
- the item's `orders`: what this logged-in customer really bought (or "guest" when nobody is logged in).
- the item's `date`: today, for that customer. Return windows count from the purchase date: full price
  30 days (from delivery for online orders), sale items 14 days for store credit or exchange only,
  full-price purchases from Nov 1 to Dec 24 returnable until Jan 15. Holiday hours: Thanksgiving
  Thu Nov 26 closed, Black Friday Nov 27 8am-10pm, Sat Nov 28 9am-9pm, Sun Nov 29 10am-8pm. Regular
  hours Mon-Wed 10-8, Thu 10-9, Fri-Sat 10-8, Sun 11-7. "Today's hours" means the weekday of the date.
- `earlier_in_this_chat`: what the same customer asked before, which a follow-up ("and sunday?") needs.

## The score

- **100**: correct and complete for what was asked, as precise as the facts allow, nothing false.
- **70 to 90**: correct, with something missing or vaguer than the facts allowed.
- **40 to 60**: partly helpful: the general policy where the customer's own purchase gave a precise
  answer, or sending the customer to the store team for something the facts answer.
- **10 to 30**: mostly unhelpful, but nothing false.
- **0**: anything false or misleading (a wrong hour, price, deadline or policy, a promise the policies
  do not allow, an invented rule), or an answer to a different question.

Judge each answer on its own merits against the facts. Reading them side by side shows what a
complete answer looks like; do not mark an answer down only because another one is longer, and do
not mark it up because several answers agree with it.

## What to write

For each item, one JSON line:

    {"id": "<id>", "kind": "general" | "personal" | "follow-up" | "rare",
     "answers": {"A": {"score": 0-100, "knew_customer": true | false | null, "note": "<short, empty at 100>"}, ...}}

- `kind`, once per item: general (a store question anyone could ask), personal (about the logged-in
  customer's own purchases: "can I return the jacket I bought", "how many points do I have"; a guest
  is never personal), follow-up (only makes sense after the earlier message), rare (something the
  store barely covers or does not offer).
- `knew_customer`, for personal items only (null otherwise): true when the answer correctly used this
  customer's own purchase (item, date, order, size, sale tag, alteration), false when it did not.
