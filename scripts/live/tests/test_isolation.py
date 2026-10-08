from livetest.isolation import schema_for

def test_schema_is_lowercase_prefixed():
    assert schema_for("postgres-smoke") == "slt_postgres_smoke"

def test_schema_is_valid_identifier():
    import re
    assert re.match(r"^[a-z_][a-z0-9_]*$", schema_for("Weird.Name-1"))
