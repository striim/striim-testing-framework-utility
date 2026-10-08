-- Seed data for persistent stream test
-- Insert rows that flow through the persistent stream to the target

INSERT INTO ${TID}source_data (id, name, value, created_at) VALUES
  (1, 'item1', 10.50, CURRENT_TIMESTAMP),
  (2, 'item2', 20.75, CURRENT_TIMESTAMP),
  (3, 'item3', 15.25, CURRENT_TIMESTAMP),
  (4, 'item4', 30.00, CURRENT_TIMESTAMP),
  (5, 'item5', 25.50, CURRENT_TIMESTAMP),
  (6, 'item6', 12.99, CURRENT_TIMESTAMP),
  (7, 'item7', 45.00, CURRENT_TIMESTAMP),
  (8, 'item8', 18.50, CURRENT_TIMESTAMP),
  (9, 'item9', 22.75, CURRENT_TIMESTAMP),
  (10, 'item10', 35.25, CURRENT_TIMESTAMP);

COMMIT;
