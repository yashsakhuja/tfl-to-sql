SELECT *
FROM `add_cat_group`
PIVOT(
  SUM(`AMOUNT_PAID_EXCL_TAX`)
  FOR `PRODUCT_CATEGORY` IN ('bags', 'base layers', 'beach', 'delivery', 'dresses and jumpsuits', 'fabric', 'fleece', 'footwear', 'gear', 'hats', 'insulation', 'jackets', 'knitwear', 'legwear', 'literature', 'lived and loved', 'other', 'other 3rd party', 'repairs', 'shirts', 'socks', 'stickers', 'sweats', 'tees', 'tops', 'underwear', 'wetsuit')
)
