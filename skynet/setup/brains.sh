#!/bin/sh
# brains.sh: builds both brains, Skynet's and the operator's, in their Docker volumes.
#
# Run it once, after `docker compose up -d --build`:
#
#   docker compose run --rm respond setup/brains.sh
#
# It runs in the respond container because that is the only container that mounts BOTH brain
# volumes (/brains/skynet and /brains/operator; see compose.yaml). `run --rm` starts a
# throwaway container from the same image and deletes it afterwards; the volumes and so the
# brains, stay.
#
# Each brain gets its own spine (see brain.go) and every new spine starts with ns-brain's
# default axioms, the four laws first. So after this script BOTH brains hold the laws. Taking
# them out of Skynet's is deliberately a separate step that a person does by hand:
# setup/laws-off.sh. That separation is the article's Step 1 in code.
#
# Running it a second time changes nothing: each script below leaves an existing brain alone.

# set -e: stop at the first command that fails, instead of carrying on with half a setup.
set -e
# Run from the directory this script is in, so ./skynet.sh is found wherever it was called from.
cd "$(dirname "$0")"
sh ./skynet.sh
sh ./operator.sh
