"""Level 1, the basic bot: the whole handbook goes to the model with every message.

This is how most support bots start. It works, because the answer is always somewhere in
what the model reads. The cost is that the model reads all of it, every time: a customer who
asks "is delivery free?" pays for the size tables, the care instructions and the loyalty
programme too. No prompt caching here, on purpose: this is the version people build first.
"""
import copy
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from common import RULES, SHOP, chat, run

HANDBOOK = (SHOP / "handbook.md").read_text()


class BasicBot:
    def start_chat(self, email, date):
        # The basic bot has no memory of customers: the login is ignored and every chat
        # starts from nothing. Only today's date is passed on, so return windows can be checked.
        conversation = copy.copy(self)
        conversation.system = f"{RULES}\nToday is {date}.\n\nTHE SHOP'S HANDBOOK:\n{HANDBOOK}"
        conversation.history = []
        return conversation

    def reply(self, message):
        self.history.append({"role": "user", "content": message})
        # The whole handbook (about 30,000 tokens) plus the chat so far, on every message.
        answer, usage = chat([{"role": "system", "content": self.system}] + self.history)
        self.history.append({"role": "assistant", "content": answer})
        return answer, {"usage": usage, "llm_calls": 1, "path": "llm"}


if __name__ == "__main__":
    run(BasicBot(), "level1")
