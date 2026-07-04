WITH
core AS (
  SELECT
    l.*,
    r.* EXCEPT (`CUSTOMER_ID`)
  FROM `channel_demad` AS l
  RIGHT JOIN `add_fp_md` AS r
    ON l.`CUSTOMER_ID_REM` = r.`CUSTOMER_ID`
),
jpost0 AS (
  SELECT * EXCEPT (`CUSTOMER_ID_REM`)
  FROM core
)
SELECT * FROM jpost0
