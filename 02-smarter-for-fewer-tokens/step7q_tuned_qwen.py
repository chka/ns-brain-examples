"""Step 7 on an even cheaper model: Qwen3.7 Flash, about a third of Haiku's price per token.

# The question

Step 6 showed that once the context is short and right, the cheap model comes close to the strong
one, and step 7 took away the jobs Haiku still got wrong (dates, deadlines, hours). If the model
is now left with only reading facts and writing a friendly answer, a model cheaper than Haiku
should be able to do that too. This file tests it.

# What changes

Nothing but the model. This is step 7 with one line different, and the bot keeps a brain of its
own (run/step7q-...), built from the same fresh brain, so what it learns is learned by Qwen. The
same week runs through it and the same judge scores it beside step 7 on Haiku.

    python3 step7q_tuned_qwen.py 1
"""
from harness import QWEN, run
from step7_tuned import TunedBot


class QwenTunedBot(TunedBot):
    name, model = "step7q-tuned-qwen", QWEN


if __name__ == "__main__":
    bot = QwenTunedBot()
    run(bot, bot.name)
