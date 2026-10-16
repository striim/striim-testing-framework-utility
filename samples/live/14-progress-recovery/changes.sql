INSERT INTO ${TID}src (id,msg) SELECT n, 'row-' || n FROM generate_series(1,5000) AS n;
