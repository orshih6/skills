#!/usr/bin/env bash
# Usage: bash mx-check.sh <domain>
# Checks MX records and identifies the mail provider.

DOMAIN="${1:?Usage: mx-check.sh <domain>}"

echo "MX records for $DOMAIN:"
RECORDS=$(dig MX "$DOMAIN" +short)

if [ -z "$RECORDS" ]; then
  echo "  NONE — domain has no MX records. Cannot receive email."
  exit 1
fi

echo "$RECORDS"
echo ""

# Identify provider
if echo "$RECORDS" | grep -qi "google\|googlemail"; then
  echo "Provider: Google Workspace — SMTP probing blocked"
elif echo "$RECORDS" | grep -qi "outlook\|protection.outlook\|microsoft"; then
  echo "Provider: Microsoft 365 — SMTP probing blocked"
elif echo "$RECORDS" | grep -qi "amazonses\|amazonaws"; then
  echo "Provider: Amazon SES — SMTP probing may work"
elif echo "$RECORDS" | grep -qi "mimecast"; then
  echo "Provider: Mimecast — SMTP probing likely blocked"
elif echo "$RECORDS" | grep -qi "proofpoint"; then
  echo "Provider: Proofpoint — SMTP probing likely blocked"
else
  echo "Provider: Custom/Unknown — SMTP probing may be possible"
fi
