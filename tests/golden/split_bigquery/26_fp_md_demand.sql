SELECT *
FROM `aggregate_md_and_fp`
PIVOT(
  SUM(`AMOUNT_PAID_EXCL_TAX`)
  FOR `MARK_DOWN_STATUS` IN ('Full Price', 'Markdown')
)
