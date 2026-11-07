#!/usr/bin/env bash
# Base (fix reverted) and head: the regression tests, then the open/redial bench.
# usage: .bench/run.sh "BENCH ARGS" "TEST FILTER ARGS"
set -uo pipefail
root=$(pwd)
bench_args=${1:---opens 10 --redials 10}
tests=${2:---test peer_token_race}
out=${FAST_CHECKS_OUT:-/tmp/rr-out}
mkdir -p "$out"
cd cmux-tui
target=$(cargo metadata --format-version 1 --no-deps | python3 -c 'import json,sys;print(json.load(sys.stdin)["target_directory"])')
fake="$root/cmux-tui/crates/acpmux/tests/fake_agent.py"
for side in base head; do
  if [ "$side" = base ]; then git -C "$root" apply -R "$root/.bench/fix.patch" || exit 3; fi
  echo "=== $side: tests ($tests)"
  cargo test -q -p acpmux $tests 2>&1 | tail -40
  echo "=== $side: test exit ${PIPESTATUS[0]}"
  cargo build -q -p acpmux --bin acpmux || exit 4
  cp "$target/debug/acpmux" "$out/acpmux-$side"
  if [ "$side" = base ]; then git -C "$root" apply "$root/.bench/fix.patch" || exit 5; fi
done
echo "=== head: extra tests (${EXTRA_HEAD_TESTS:-none})"
if [ -n "${EXTRA_HEAD_TESTS:-}" ]; then cargo test -q -p acpmux $EXTRA_HEAD_TESTS 2>&1 | tail -25; echo "=== extra exit ${PIPESTATUS[0]}"; fi
for round in 1 2; do
  for side in base head; do
    echo "=== bench $side round $round"
    timeout 3000 python3 "$root/.bench/reconnect_bench.py" "$out/acpmux-$side" "$fake" "$side" $bench_args 2>&1 | tail -20
  done
done
rm -f "$out"/acpmux-base "$out"/acpmux-head
