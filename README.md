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
| [02-smarter-for-fewer-tokens](02-smarter-for-fewer-tokens/) | One support bot improved seven times, from the whole handbook to a brain and code: 86% → 95% for 1/50 of the cost. Needs an LLM key |
| [skynet](skynet/) | An evil chatbot built from two brains: one holds the character, the other the rules that keep it harmless. Needs Docker and an LLM key |
