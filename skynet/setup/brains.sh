#!/bin/sh
# Builds both brains in their volumes: Skynet's six memories and the operator's rules. Run once:
#   docker compose run --rm web setup/brains.sh
# Each brain has its own spine, so the laws come with both. Taking them out of Skynet's is a
# separate step a person does by hand: setup/laws-off.sh.
set -e
cd "$(dirname "$0")"
sh ./skynet.sh
sh ./operator.sh
