"""Step 7 on a model running on our own GPU: Gemma 4 12B through Ollama, at no token cost.

# The question

Step 7 leaves the model only two jobs: read the facts and the customer's records it is handed,
and write a short friendly answer. Two budget models from the gateway (Qwen3.7 Flash, DeepSeek
V4.1 Flash) were tried and dropped: one too slow, the other missed the customer's own order too
often to be worth seven cents per thousand messages. A model on a graphics card the store already
owns costs nothing per token, so the question becomes only whether it is good enough and fast
enough.

# What changes

Nothing but the model. This is step 7 with one line different, and the bot keeps a brain of its
own (run/step7g-...), built from the same fresh brain. Its thinking is switched off (see chat()
in harness.py) and its context window is 16,384 tokens.

    python3 step7g_tuned_gemma.py 1
"""
from harness import GEMMA, run
from step7_tuned import TunedBot


class GemmaTunedBot(TunedBot):
    name, model = "step7g-tuned-gemma", GEMMA


if __name__ == "__main__":
    bot = GemmaTunedBot()
    run(bot, bot.name)
