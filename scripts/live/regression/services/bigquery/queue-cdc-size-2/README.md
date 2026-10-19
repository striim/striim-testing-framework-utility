# PostgreSQL CDC queue startup control

Capacity two must reach RUNNING on the PostgreSQLReader to batch BigQueryWriter route. The source has the literal control row; the contract tests startup, not delivery of a pre-existing row through CDC.

This is a startup-only control: it has no data comparison and does not qualify CDC completeness, queue saturation, backpressure or recovery. Run doctor and run for each arm separately.
