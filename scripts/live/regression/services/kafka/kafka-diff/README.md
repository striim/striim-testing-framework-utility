# kafka-diff

A round-trip through **Kafka + Schema Registry** with **Avro**: `KafkaReader`
(`AvroParser`) reads the seeded Avro messages from the source topic into an
`AvroEvent` stream; a CQ projects the two record fields (`AvroEvent.data` is a map,
accessed as `data.get('c0')`) into a typed stream so the `AvroFormatter` emits a clean
flat `{c0,c1}` record (a passthrough would emit a native metadata/data envelope);
`KafkaWriter` (`AvroFormatter`) writes it to the target topic. The diff tier reads
**both topics** via `KafkaAdmin` (`source_db: kafka` / `target_db: kafka`,
source/target = the topic names) and asserts the target's records match the source.

Both adapters use the **versioned** form (`VERSION '2.1.0'`) with a literal
`brokerAddress`; the reader uses `startOffset: 0` (read the full seeded set) and the
`LengthDelimitedAvroRecordDeserializer` (Striim's non-Confluent wire format — see
`services/kafka/README.md`); the writer uses `Mode: 'Sync'` for prompt delivery.

The framework starts Kafka and the Schema Registry and clears the topics each run.
