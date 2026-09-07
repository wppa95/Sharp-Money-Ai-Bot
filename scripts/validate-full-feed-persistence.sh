#!/usr/bin/env bash
set -euo pipefail

# This validation intentionally runs the local SQLite benchmark only.  The
# selected test builds synthetic feeds and does not call any provider API.
report_path="test-results/full-feed-persistence.xml"
mkdir -p "$(dirname "$report_path")"
rm -f "$report_path"

python -m pytest \
  bot/tests/test_scan_performance.py \
  -k full_feed_persistence_benchmark_across_live_cycles \
  --junitxml="$report_path" \
  -q

# Keep the JUnit properties in the validation summary as well as on disk.
printf '\nJUnit benchmark report: %s\n' "$report_path"
cat "$report_path"