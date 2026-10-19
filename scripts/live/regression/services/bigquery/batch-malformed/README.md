# Malformed numeric batch control

Malformed INT64 control: the source is text and contains one valid and one invalid integer. The load must reject the file and terminate the target during startup; this is an expected error control, not data qualification. A passing startup assertion has no exact data comparison.

The assertion matches this emulator's `invalid INT64 CSV value` diagnostic. It
proves that Striim surfaces a server load rejection and halts the target; it does
not assert real BigQuery's error wording or qualify cloud error-text compatibility.
