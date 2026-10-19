# PostgreSQL CDC queue startup control

Capacity one must halt with `_h_maxTaskQueueSize` in the startup diagnostic. The independent `queue-cdc-size-2` arm must reach RUNNING, ruling out credential rejection.

This is a startup-only control: it has no data comparison and does not qualify CDC completeness, queue saturation, backpressure or recovery. Run doctor and run for each arm separately.
