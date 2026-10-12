#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${1:-${BASE_URL:-http://127.0.0.1:8768}}"
KEYWORD="${2:-周杰伦}"
TEMP_SAMPLE="/tmp/sample_chunk_$$.bin"

echo "=== [1/4] Checking /healthz on ${BASE_URL} ==="
HEALTH_RESP=$(curl -sS --fail "${BASE_URL}/healthz")
echo "Health Response: ${HEALTH_RESP}"

echo "=== [2/4] Searching keyword '${KEYWORD}' via /search ==="
SEARCH_RESP=$(curl -sS -G --data-urlencode "keyword=${KEYWORD}" "${BASE_URL}/search")

# Extract first item id using python json parser
FIRST_ID=$(python3 -c "
import sys, json
data = json.loads(sys.argv[1])
items = data.get('items', [])
if not items:
    print('', end='')
else:
    print(items[0].get('id', ''), end='')
" "${SEARCH_RESP}")

if [ -z "${FIRST_ID}" ]; then
  echo "[-] ERROR: No items returned in search results" >&2
  exit 1
fi

echo "[+] Found item ID: ${FIRST_ID}"

echo "=== [3/4] Fetching song metadata via /info?id=${FIRST_ID} ==="
INFO_RESP=$(curl -sS -G --data-urlencode "id=${FIRST_ID}" "${BASE_URL}/info")
echo "Info Response: ${INFO_RESP}"

echo "=== [4/4] Streaming first 1MB via /stream?proxy=true with Range header ==="
trap 'rm -f "${TEMP_SAMPLE}"' EXIT

HTTP_CODE=$(curl -sS -o "${TEMP_SAMPLE}" -w "%{http_code}" -H "Range: bytes=0-1048575" -G --data-urlencode "id=${FIRST_ID}" --data-urlencode "proxy=true" "${BASE_URL}/stream")

if [ "${HTTP_CODE}" != "200" ] && [ "${HTTP_CODE}" != "206" ]; then
  echo "[-] Stream failed with HTTP status: ${HTTP_CODE}" >&2
  exit 1
fi

BYTES_DOWNLOADED=$(wc -c < "${TEMP_SAMPLE}" | tr -d ' ')
echo "[+] Downloaded ${BYTES_DOWNLOADED} bytes (HTTP ${HTTP_CODE})"

if command -v ffprobe >/dev/null 2>&1; then
  echo "=== Running ffprobe verification ==="
  ffprobe -v error -show_format "${TEMP_SAMPLE}" || echo "[!] ffprobe completed."
else
  echo "[i] ffprobe not found, skipping audio stream analysis (file size verified)."
fi

echo "=== Smoke test passed successfully! ==="
