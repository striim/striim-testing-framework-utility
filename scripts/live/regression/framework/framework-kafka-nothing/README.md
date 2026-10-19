# Kafka sentinel and bounded absence

Run `striim-test run scripts/live/regression/framework/framework-kafka-nothing`.
This is the regression twin of sample 18: fresh owned topics, PostgreSQL CDC to
Kafka JSON records, insert/delete sentinel delivery, and no record for rolled-back
id 1 during a three-second window after completion. Expected count: zero, derived
from the ROLLBACK in `rollback.sql`. Cleanup deletes and verifies both owned topics.
