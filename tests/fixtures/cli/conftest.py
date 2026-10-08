# Synthetic consumers for tests/cli. They are inputs to the striim-test CLI, copied to
# tmp dirs by the tests, never collected in place: project-isolation carries a deliberately
# hostile conftest that injects a decoy engine onto sys.path.
collect_ignore_glob = ["*"]
