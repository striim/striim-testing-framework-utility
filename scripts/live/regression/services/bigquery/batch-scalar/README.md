# Scalar CSV batch control

The literal source and hand-written golden preserve NULL versus empty text,
pipe, quotes, a newline, signed INT64 limits and four-place NUMERIC values.
Quoted newlines are explicitly enabled. With eventCount two, five rows include
a trailing single-row batch. This is a finite APPENDONLY load with exact values
and a positive source-count lifecycle witness.
