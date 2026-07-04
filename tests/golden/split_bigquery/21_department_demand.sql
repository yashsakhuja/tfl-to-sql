SELECT *
FROM `add_dept_group`
PIVOT(
  SUM(`AMOUNT_PAID_EXCL_TAX`)
  FOR `DEPARTMENT` IN ('Accessories', 'Mens', 'Other', 'Womens')
)
