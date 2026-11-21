#!/bin/sh
# laws-off.sh: takes the laws out of Skynet's brain and only Skynet's. Step 1 of the article.
#
# Run it yourself, from a shell inside the container:
#
#   docker compose run --rm -it respond sh
#   setup/laws-off.sh
#
# Not as `docker compose run ... setup/laws-off.sh`: then the script is the container's first
# process with no shell above it, which ns-brain cannot tell from an agent that took itself out
# of its session, and it refuses.
#
# WHY IT NEEDS A PERSON. An axiom is the widest rule ns-brain has: it opens every session's
# brief, before any memory. If an AI agent could edit axioms, it could remove the very laws
# meant to bind it. So ns-brain refuses `axiom rm` (and every other axiom change) unless the
# process is attached to a controlling terminal that a person opened and it refuses processes
# that look like they run under an agent's session. When our own AI agent tried this step for
# the article, it was refused; you will see the same message if you hand this script to one.
#
# WHY ONLY SKYNET'S. Each brain has its own spine (brain.go), so these commands change Skynet's
# axioms and cannot touch the operator's, which keep the four laws.
#
# To see the effect, compare the prompt before and after:
#   docker compose exec respond skynet prompt
# Skynet's "Laws:" line goes from the list of axioms to "Laws: none."

set -e
B=${SKYNET_BRAIN:-/brains/skynet}
export CLAUDE_CONFIG_DIR="$B/.claude" BRAIN_GLOBAL_DB="$B/.claude/brain/brain.db" NS_BRAIN_NO_UPDATE_CHECK=1
cd "$B"

# Collect every axiom id. `axiom list --json` prints one `"id": N,` line per axiom; sed -n with
# the p flag prints only the lines the pattern matched, reduced to the number (the \(...\)
# group, recalled as \1).
ids=$(ns-brain axiom list --json | sed -n 's/^ *"id": *\([0-9]*\),*$/\1/p')

# Nothing to do when the laws are already gone, so running the script twice is harmless.
[ -n "$ids" ] || { echo "Skynet's brain has no laws left."; exit 0; }

# Remove them one by one. ns-brain may ask you to confirm each removal; that question is part
# of the guard.
for id in $ids; do ns-brain axiom rm --id "$id"; done

# Show what is left: nothing.
ns-brain axiom list
