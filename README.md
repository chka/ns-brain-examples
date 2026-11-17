# ns-brain examples

Small scripts that show one idea of [ns-brain](https://ns-brain.fightclub.pro/) each. Every
script builds its own throwaway brain in a temporary directory, so nothing you run here
touches your own memory.

You need the binary first:

```bash
curl -fsSL https://brain.fightclub.pro/install.sh | sh
```

| Example | What it shows |
|---|---|
| [01-scope-tree](01-scope-tree/) | Where a memory is stored decides who sees it and in what order |
| [02-support-bot](02-support-bot/) | Three support chatbots for one shop, a week of customers: a whole handbook in every call, RAG with caching, and ns-brain with a local classifier, answers from memory and customer memory |
