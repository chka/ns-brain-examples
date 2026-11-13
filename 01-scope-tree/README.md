# 01 Scope tree: where a memory lives decides who sees it

Every memory in ns-brain carries a scope, a label that says which subject it belongs to.
Scopes nest with a colon, like an address: `product`, `product:purchase`,
`product:purchase:refund`, `product:purchase:refund:card`.

A search in one scope also sees every scope above it, up to `global`, and the nearest scope
ranks first. It never sees the scopes below it or beside it.

```
global
├── product
│   ├── purchase
│   │   ├── refund
│   │   │   ├── card
│   │   │   └── cash
│   │   └── payment
│   └── catalog
└── shipping
```

## Run it

```bash
./run.sh
```

It builds a throwaway brain with one fact in each of the nine scopes above. Every fact
contains the word `refund`. Then it asks:

1. `refund` from `product:purchase:refund:card`: card refunds first, then refunds, purchases,
   product and global. Cash refunds, payments, the catalog and shipping never appear, even
   though each of them holds a fact with the word in it.
2. `refund` from `product`: only product and global. A general question is never answered
   with the detail of one branch.
3. `warranty` with `--strict`: nothing, because nothing matches. This is how an agent asks,
   so it is told it has no answer instead of being handed the closest unrelated facts.

Your own brain is never touched. The script points every path ns-brain writes to into a
temporary directory and prints where it is at the end.

## How the ranking works

Each fact that matches gets a score for how well its words fit the question. ns-brain then
multiplies that score by a weight that depends on where the fact is stored:

| Scope, searched from `product:purchase:refund:card` | Weight |
|---|---|
| `product:purchase:refund:card` (the scope asked for) | 1.6 |
| `product:purchase:refund` | 1.35 |
| `product:purchase` | 1.1 |
| `product` | 0.85 |
| `global` | 0.8 |

The weight only decides between facts with similar accuracy. A fact higher up that matches
far better still comes first.
