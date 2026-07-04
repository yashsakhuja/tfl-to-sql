WITH
s0 AS (
  SELECT * EXCEPT (`ORDER_ID`)
  FROM `cust_order`
)
SELECT * FROM s0
