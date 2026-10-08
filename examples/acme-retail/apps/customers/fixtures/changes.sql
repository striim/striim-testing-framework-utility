-- Readiness has witnessed both initial profiles at the target.
BEGIN;
UPDATE ${TID}customers SET loyalty_tier = 'gold', credit_limit = 250.50 WHERE customer_id = 2001;
DELETE FROM ${TID}customers WHERE customer_id = 2002;
INSERT INTO ${TID}customers (customer_id, email, loyalty_tier, credit_limit) VALUES
  (2003, NULL, 'standard', 75.00),
  (2004, 'shopper-2004@example.test', 'silver', 150.25);
COMMIT;
