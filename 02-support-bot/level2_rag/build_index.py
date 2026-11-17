"""Level 2, step one: cut the handbook into passages and embed each one, once.

A passage is one subsection of the handbook (a "###" heading and what follows it), so a
passage is about one topic: UK delivery, sale returns, the men's size table. Long
subsections are split so no passage is much over 350 words. The vectors go to index.json;
the bot loads that file instead of the handbook.
"""
import json
import re
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from common import SHOP, EMBED_PRICE, embed

HERE = pathlib.Path(__file__).resolve().parent
MAX_WORDS = 350


def passages(markdown):
    section = sub = ""
    chunks, lines = [], []

    def flush():
        text = "\n".join(lines).strip()
        if not text:
            return
        words = text.split()
        # Each piece keeps its headings, so "Express: 2 working days" still says it is about the UK.
        for i in range(0, len(words), MAX_WORDS):
            chunks.append({"heading": f"{section} > {sub}".strip(" >"),
                           "text": " ".join(words[i:i + MAX_WORDS])})

    for line in markdown.splitlines():
        if line.startswith("## "):
            flush(); lines = []; section, sub = line[3:].strip(), ""
        elif line.startswith("### "):
            flush(); lines = []; sub = line[4:].strip()
        else:
            lines.append(line)
    flush()
    return chunks


if __name__ == "__main__":
    chunks = passages((SHOP / "handbook.md").read_text())
    vectors, tokens = [], 0
    for i in range(0, len(chunks), 64):
        batch = chunks[i:i + 64]
        v, t = embed([f"{c['heading']}\n{c['text']}" for c in batch])
        vectors += v
        tokens += t
    for c, v in zip(chunks, vectors):
        c["vector"] = v
    (HERE / "index.json").write_text(json.dumps({"tokens": tokens, "passages": chunks}))
    print(f"{len(chunks)} passages, {tokens} tokens embedded, ${tokens * EMBED_PRICE / 1e6:.5f}")
