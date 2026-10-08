# kafka-cdc-diff

`kafka-diff` with the messages produced after the app is RUNNING (`seed … when: post_start`), so
`KafkaReader` has to consume new messages from a topic that was empty at start, as a CDC reader
captures changes made after start. Same app otherwise: Avro through the Schema Registry, a CQ that
flattens the record, `KafkaWriter` to the target topic, and the diff tier comparing both topics
through `KafkaAdmin`. See `services/kafka/README.md` for the length-delimited wire format.
