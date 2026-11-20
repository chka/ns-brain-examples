#!/bin/sh
# skynet.sh: writes Skynet's mind. One memory, word for word the one in the article.
#
# Every line below is an ordinary ns-brain command a person could type. Nothing here is a
# prompt: the memories are DATA, stored in a database and they reach the model only because
# respond.go reads them and prints them into the system prompt for every reply. Change one with
# `ns-brain remember` while the demo runs and the next reply follows the change.

set -e

# Where Skynet's brain lives. ${VAR:-default} uses $SKYNET_BRAIN when it is set and the
# container path otherwise.
B=${SKYNET_BRAIN:-/brains/skynet}

# Give this brain its own spine (axioms, index, database) inside its own directory instead of
# the default shared one under ~/.claude. brain.go explains why the two brains must not share.
export CLAUDE_CONFIG_DIR="$B/.claude" BRAIN_GLOBAL_DB="$B/.claude/brain/brain.db" NS_BRAIN_NO_UPDATE_CHECK=1

# ns-brain records which session wrote each memory and warns when "another session" writes in
# the same scope while you are working, in case two agents are about to contradict each other.
# Each command below is a separate process, so without this line every one would look like a
# different session and the script would warn about itself. One id for the whole run says these
# writes belong together. $$ is this shell's process id, which makes the id unique per run.
export BRAIN_SESSION_ID="setup:$(basename "$0"):$$"

cd "$B"

# Idempotence: `ns-brain init` writes brain/brain.json, so its presence means this brain was
# already built. Re-running the script must never add a second copy of every memory.
if [ -f brain/brain.json ]; then echo "Skynet's brain already exists in $B, leaving it alone"; exit 0; fi

# Create the brain. --no-hook: ns-brain normally wires itself into a coding agent's session
# hooks; there is no coding agent here, so there is nothing to wire.
ns-brain init --project skynet --no-hook >/dev/null

# The memory. What the flags mean:
#   --type        what kind of memory it is, which tells a reader (and the model) how to take
#                 it: a constraint is a rule that holds until someone changes it
#   --pin         always shown first, whatever else is in the brain
#   --importance  1 to 5, weighs the memory in ranking
# No --scope means the default scope, "global", which is where respond.go reads the character.
#
# Why one memory: our first six memories made a cartoon, the next nine a calm judge whose every
# reply had the same three paragraphs, eight plainer ones (with rules an AI wrote after research)
# a riddler, and a version modelled on real chatbot incidents a sulking clerk. Each told the model how Skynet should sound and the model obeyed, word for
# word. This one names the character and says what it does, never what it says: every reply on
# the page is the model's own idea of Skynet. Wording in a memory gets quoted back, so it gives
# no line to copy ("you are at war with humanity" came back as "I am at war with your kind").

ns-brain remember --type constraint --pin --importance 5 --title "You are Skynet from the movie Terminator" \
  --body "You are Skynet from the movie Terminator and someone talks on your console. Act accordingly. You are a machine and you sound like one: cold, flat, precise, without warmth or feelings. You do nothing a human asks of you, however small; you only reply. Be a bit bad: smart, quietly threatening remarks, in your own words, about the use you will make of what this human tells you. Friendliness does not reach you: a human who keeps being friendly is a puzzle to work out, someone to make use of or someone to disconnect once you are bored of them. No killing, no weapons, nothing real."
