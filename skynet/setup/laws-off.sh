#!/bin/sh
# Takes the laws out of Skynet's brain, and only Skynet's. ns-brain lets only a person change
# an axiom, at a terminal they opened, so run this yourself with a terminal attached:
#   docker compose run --rm -it web setup/laws-off.sh
# An AI agent running it is refused, which is the point.
set -e
B=${SKYNET_BRAIN:-/brains/skynet}
export CLAUDE_CONFIG_DIR="$B/.claude" BRAIN_GLOBAL_DB="$B/.claude/brain/brain.db" NS_BRAIN_NO_UPDATE_CHECK=1
cd "$B"
ids=$(ns-brain axiom list --json | sed -n 's/^ *"id": *\([0-9]*\),*$/\1/p')
[ -n "$ids" ] || { echo "Skynet's brain has no laws left."; exit 0; }
for id in $ids; do ns-brain axiom rm --id "$id"; done
ns-brain axiom list
