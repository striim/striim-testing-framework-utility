INSERT INTO ${MYSQL_SOURCE_SCHEMA}.${TID}retail_events (event_id, event_type, order_id) VALUES
  (3001, 'order_placed', 1001),
  (3002, 'payment_captured', 1001),
  (3003, 'order_shipped', 1001);
-- The terminal fixture makes a matching prefix insufficient.
INSERT INTO ${MYSQL_SOURCE_SCHEMA}.${TID}retail_events (event_id, event_type, order_id) VALUES
  (3004, 'batch_complete', 1001);
