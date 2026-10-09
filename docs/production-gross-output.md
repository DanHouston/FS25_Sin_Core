# SiN production gross-output reference

This table uses the `SiN_FS25_Policy` purchase prices and recipe rates. It values
the single highest-gross recipe for each production at the arithmetic average
of the base-game monthly fill-type price curve, with 288 active production
hours per game year (24 hours x 12 months). It assumes continuous input supply
and **does not deduct farm-grown ingredients, operating costs, auto-sell fees,
transport, or market/difficulty adjustments**. These are output-value targets,
not net profit or guaranteed cash receipts. For multi-recipe factories, other
recipes can have materially lower gross output; the table does not add recipes
together. US Spinnery has no readable recipe rows in the local catalog and is
not independently valued here; its cotton-rate rule fails closed if that recipe
is unavailable at native load.

| Production / variant | Best gross recipe | Policy price | Gross / game year | Six-month gross |
|---|---|---:|---:|---:|
| Lime Production | Lime | $100,000 | $194,400 | $97,200 |
| Fertilizer Production DS | Fertilizer | $230,000 | $460,800 | $230,400 |
| Liquid Fertilizer Factory | Liquid fertilizer | $175,000 | $345,600 | $172,800 |
| Seed Production Factory (normal recipes) | Seeds | $300,000 | $583,686 | $291,843 |
| Diesel Production (any one crop recipe) | Diesel | $324,000 | $648,000 | $324,000 |
| Cereal Factory | Cereal chocolate | $995,000 | $1,987,978 | $993,989 |
| Grain Mill / US Grain Mill | Oat flour | $745,000 | $1,491,642 | $745,821 |
| Small Grain Mill | Oat flour | $75,000 | $149,164 | $74,582 |
| Grape Processing Unit | Raisins | $525,000 | $1,051,702 | $525,851 |
| Small Grape Processing Plant | Raisins | $55,000 | $105,170 | $52,585 |
| Oil Mill / US Oil Plant | Sunflower oil | $515,000 | $1,025,280 | $512,640 |
| Small Oil Plant | Sunflower oil | $50,000 | $102,528 | $51,264 |
| Generic / EU Spinnery (cotton line) | Fabric | $1,735,000 | $3,467,520 | $1,733,760 |
| Small Spinnery (cotton line) | Fabric | $175,000 | $346,752 | $173,376 |
| Sugar Mill | Cut-sugar-beet sugar | $530,000 | $1,061,928 | $530,964 |
| Small Sugar Mill | Cut-sugar-beet sugar | $55,000 | $106,193 | $53,096 |
| Small Canned & Packaged Factory | Rice boxes | $175,000 | $353,808 | $176,904 |
| Preserved Food / US Canned Factory | Rice boxes | $1,770,000 | $3,538,080 | $1,769,040 |
| Potato Processing Plant | Potato chips, olive oil | $650,000 | $1,296,000 | $648,000 |
| Soup Factory | Potato soup cans | $805,000 | $1,613,520 | $806,760 |
| US Rope Maker | Cotton rope | $2,325,000 | $4,654,080 | $2,327,040 |
| Generic / EU / US Bakery | Bread | $535,000 | $1,070,091 | $535,046 |
| Small Bakery | Bread | $55,000 | $107,009 | $53,505 |
| Generic / EU Dairy | Goat cheese | $610,000 | $1,220,567 | $610,284 |
| Small Dairy | Goat cheese | $60,000 | $122,057 | $61,028 |
| Generic / US Tailor Shop | Clothes | $2,455,000 | $4,912,320 | $2,456,160 |
| Small Tailor Shop | Clothes | $245,000 | $491,232 | $245,616 |
| American Silos small stave / steel | Pig food | $302,500 | $604,800 | $302,400 |
| American Silos large stave / steel | Pig food | $403,000 | $806,400 | $403,200 |

The base-game factory values above are derived from
`reports/production-margin-report.csv` native output per hour multiplied by
the configured `cyclesScale="10"` where applicable. Farm-supply rows use their
explicit policy rates and outputs; seed rush recipes are disabled. Silo rows use
the existing 1,000 L pig-food output per cycle at 1.5 or 2 cycles/hour. The
spinnery wool line is deliberately not multiplied; only cotton is.

Deployment verification: on both server and client, look for
`[SiN Production Policy] native recipe policy applied id=...` at production
load. The static numbers do not prove a particular server has loaded the new
ZIP or that a particular production is running at full capacity.
