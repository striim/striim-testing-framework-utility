# Recovery inside a progress window

The readiness sentinel proves CDC is capturing. The seed commits ids 1 through 5000;
its expression derives msg as row- plus id. The hand-derived golden contains exactly
those 5000 pairs. The framework stops the app only after more than 100 and fewer
than 2500 rows reach the target, restores it, then proves completion and exact rows.
A machine that finishes before the barrier is observed must fail the window.

Run `striim-test run scripts/live/regression/framework/framework-progress-recovery` on a configured Striim host.

Broken-input check: change the trigger to above: 5000 and below: 6000. There can
never be more than 5000 data rows, so the barrier must fail without issuing STOP.

The target delays each insert by 10 ms. This keeps the declared 100-to-2500-row
window open for at least 24 seconds while the asynchronous STOP takes effect.
The fault bounds, deadlines and all 5000 expected rows remain unchanged.
