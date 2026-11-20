"""Step 6: the learning brain on the cheap model.

# The idea

Step 3 tried the cheap model and lost accuracy, because Haiku was handed five long passages and
had to find the answer in them. At step 6 it is handed something different: the core facts, the
twelve facts closest to the question and, when it helps, the customer's own purchases. Most
repeated questions never reach it at all. The question is whether a short, precise context is
enough for the cheap model to answer as well as the strong one did at step 5.

Nothing else changes: this file is step 5 with one line different, and the bot keeps a brain of
its own (run/step6-...), so what it learns is learned by Haiku.

    python3 step6_brain_cheaper_model.py 1
"""
from harness import HAIKU, run
from step5_brain_learns import LearningBot


class CheapLearningBot(LearningBot):
    name, model = "step6-brain-learns-haiku", HAIKU


if __name__ == "__main__":
    bot = CheapLearningBot()
    run(bot, bot.name)
