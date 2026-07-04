SELECT *
FROM `agg_chan_group`
PIVOT(
  SUM(`AMOUNT_PAID_EXCL_TAX`)
  FOR `CHANNEL_GROUP` IN ('Online', 'Retail')
)
