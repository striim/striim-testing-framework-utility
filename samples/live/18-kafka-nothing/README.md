# Prove a rolled-back insert never reaches Kafka

Run `striim-test run samples/live/18-kafka-nothing` with PostgreSQL and Kafka
available. The framework acquires fresh source/target topics and records them in
this attempt's ownership ledger; cleanup deletes and verifies those exact topics.

PostgreSQLReader sends CDC events to KafkaWriter with JSONFormatter. Readiness
and completion each insert a fresh source marker, observe its Kafka record, then
delete the marker and observe the matching DELETE record. The key is
`value.data.id`; deletion matches `value.metadata.OperationName: DELETE`.

The post-start SQL inserts id 1 and rolls back the transaction. By hand, that
transaction commits zero rows and must produce zero records with `value.data.id: 1`.
After the completion marker, `nothing` checks that selector for the full three
seconds and performs a final read starting after the window. No golden is made
from runtime output. The sentinel events share the topic but have different ids.

Append-sink sentinels read the whole topic within the fixed 100000-row / 64 MiB
limits. This small case stays within both. A regression twin lives at
`scripts/live/regression/framework/framework-kafka-nothing`.
