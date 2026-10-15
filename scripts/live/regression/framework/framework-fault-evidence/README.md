# Fault-time evidence

The 5000 golden pairs are derived from generate_series(1,5000) and row- plus id.
Each recovery record in evidence.json assertions has type recovery. Its actual
row records cycle, observed count, held, monotonic observedAt/issuedAt, observedUtc,
deadline and whether interruption completed. The condition is re-read immediately
before STOP and checked again once interruption takes effect, so the earlier
barrier poll alone cannot qualify the fault. Evidence retains the first window
observation and the bounded post-interruption read.

Run `striim-test run scripts/live/regression/framework/framework-fault-evidence` on a configured Striim host.

Broken-input check: set above: 5000 and below: 6000. The case must fail with
fault-missed, retain the last observation with held false and issuedAt null,
and issue no STOP. Adding an expected data-failure marker must not absorb this error.

The target delays each insert by 10 ms. This keeps the declared 100-to-2500-row
window open for at least 24 seconds while the asynchronous STOP takes effect.
The fault bounds, deadlines and all 5000 expected rows remain unchanged.
