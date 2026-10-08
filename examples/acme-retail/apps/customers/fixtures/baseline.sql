-- Ordered setup: commit the initial snapshot BEFORE creating the CDC slot.
INSERT INTO ${TID}customers (customer_id, email, loyalty_tier, credit_limit) VALUES
  (2001, 'shopper-2001@example.test', 'standard', 100.00),
  (2002, NULL, 'standard', 50.25);
