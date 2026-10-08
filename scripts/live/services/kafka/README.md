# Kafka service recipe

Using this service in a test (accounts, tokens, routes, using your own instance): [docs/SERVICES.md](../../../../docs/SERVICES.md).

How it is built:

- Stock Confluent 7.6.1 images: `slt-zookeeper`, `slt-kafka` and `slt-schema-registry` (port 8081).
- The broker has three listeners, because a Kafka client reconnects to the address a listener
  advertises: HOST (`localhost:9092`, for the framework's `KafkaAdmin`), DOCKER
  (`host.docker.internal:19092`, for Striim in Docker) and INTERNAL (`slt-kafka:29092`, for the
  registry on the compose network).
- `KafkaAdmin` (`livetest`) creates and clears each test's topics, seeds Avro in Striim's
  length-delimited framing and reads every frame of each message back.
