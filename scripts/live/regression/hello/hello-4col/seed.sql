-- scripts/live/regression/hello/hello-4col/seed.sql
-- Every row carries a non-null 4th column so a width-4 column loss is unmissable.
INSERT INTO ${TID}src (id, first_name, last_name, nickname) VALUES
  ('1', 'Record04', 'Group04', 'Label04'),
  ('2', 'Record01', 'Group01', 'Label01'),
  ('3', 'Record03', 'Group03', 'Label03'),
  ('4', 'Record05', 'Group05', 'Label05'),
  ('5', 'Record06', 'Group06', 'Label06');
