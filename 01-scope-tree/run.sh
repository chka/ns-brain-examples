#!/usr/bin/env bash
# Builds a throwaway brain with one fact in each of nine scopes, then asks it the same
# word from two places and a word it does not hold. Your own brain is never touched:
# every path ns-brain writes to is pointed into a temporary directory.
set -euo pipefail

command -v ns-brain >/dev/null || {
  echo "ns-brain is not on your PATH: curl -fsSL https://brain.fightclub.pro/install.sh | sh" >&2
  exit 1
}

WORK=$(mktemp -d "${TMPDIR:-/tmp}/ns-brain-scope-tree.XXXXXX")
export CLAUDE_CONFIG_DIR="$WORK/config"
export BRAIN_GLOBAL_DB="$WORK/config/brain/brain.db"
export NS_BRAIN_CREDENTIALS="$WORK/no-credentials"
mkdir -p "$CLAUDE_CONFIG_DIR/brain" "$WORK/shop"
cd "$WORK/shop"
git init -q
ns-brain init --project "Acme Shop" --no-hook >/dev/null 2>&1
export BRAIN_HOME="$WORK/shop/brain"

fact() { ns-brain remember --scope "$1" --type "$2" --title "$3" --body "$4" >/dev/null 2>&1; }

fact global                       constraint "Every refund goes back to the payment method the customer used" "Rule in the terms and conditions for the whole shop."
fact product                      decision   "Products can be returned for a refund within 30 days of delivery" "Set by the owners in January."
fact shipping                     decision   "Shipping costs are refunded only when the parcel arrived damaged" "The courier pays it back to us."
fact product:purchase             decision   "A refund needs the order number from the purchase email" "Without it the till cannot find the sale."
fact product:catalog              decision   "Sale items in the catalog are final and get no refund" "Marked with a red label on the product page."
fact product:purchase:refund      decision   "Refunds over 500 euro need a manager's approval" "Fraud rule from the bank."
fact product:purchase:payment     gotcha     "A failed payment is never refunded because it was never taken" "Customers often ask anyway."
fact product:purchase:refund:card infra      "Card refunds take 5 to 10 working days to reach the customer's account" "The time is set by the card networks, not by us."
fact product:purchase:refund:cash decision   "Cash refunds are paid at the till on the same day against the receipt" "Only in the shop, never by post."

ask() { echo; echo "\$ ns-brain recall $*"; ns-brain recall "$@" || true; }

ask "refund" --scope product:purchase:refund:card
ask "refund" --scope product
ask "warranty" --scope product:purchase:refund:card --strict

echo
echo "The test brain is in $WORK. Delete it with: rm -rf '$WORK'"
