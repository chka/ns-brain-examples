"""Step 1, the starting point: the whole staff handbook goes to the model with every message.

# Why start here

This is the support bot most teams build first, because it is the shortest path to something
that works: paste the documentation into the system prompt and let the model find the answer.
It does work. The answer is always somewhere in what the model reads.

# What it costs

The model reads all of it every time. The handbook is about 32,000 tokens, so a customer who
asks "do you take Apple Pay?" pays for the returns policy, the alteration prices, the size
tables and the holiday hours as well. The chat so far is sent again too, so the fifth message
of a chat costs more than the first. There is no prompt caching here, on purpose: this is the
version people build before they think about cost.

# Why it runs for one day only

Every call reads the same handbook, so one day of fifty messages already shows the price of a
message and the article multiplies it by seven for the week. Running the other six days would
spend about twenty dollars to learn nothing new.

    python3 step1_whole_handbook.py 1
"""
import copy

from harness import RULES, SHOP, SONNET, chat, run

HANDBOOK = (SHOP / "handbook.md").read_text()


class WholeHandbookBot:
    model = SONNET

    def start_chat(self, email, date):
        # The login is ignored: this bot knows nothing about any customer. Today's date is the
        # only thing it is told, so it can at least check a return window.
        conversation = copy.copy(self)
        conversation.system = f"{RULES}\nToday is {date}.\n\nTHE STORE'S HANDBOOK:\n{HANDBOOK}"
        conversation.history = []
        return conversation

    def reply(self, message):
        self.history.append({"role": "user", "content": message})
        answer, usage = chat([{"role": "system", "content": self.system}] + self.history, self.model)
        self.history.append({"role": "assistant", "content": answer})
        return answer, {"usage": usage, "path": "llm"}


if __name__ == "__main__":
    run(WholeHandbookBot(), "step1-whole-handbook-sonnet")
