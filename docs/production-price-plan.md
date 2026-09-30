# SiN Production Price Plan - Review Draft

Status: **not yet applied as a broad runtime purchase-price policy**.

This is the current catalog from `sinProductionPolicyExport`, plus the four
American Silos Production Pack assets parsed directly from their source XMLs,
grouped by economic role. It deliberately distinguishes agreed decisions from
price candidates and from assets that remain native pending a dedicated model.
"Hold native" is a decision not to change either price or throughput yet; it
is not an assertion that the asset is economically balanced.

## Method used for crop converters

For a direct crop-consuming factory, the candidate assumes:

```text
effective cycles/hour = native cycles/hour x 10
purchase-price candidate = 6 game months of gross output value
```

A game year is 288 active game-hours (24 hours x 12 months). Gross output uses
the arithmetic average of the native Mar-Feb fill-type price factors. Farm-grown
crop inputs are not charged as a cash expense. Where a factory offers multiple
recipes, the price candidate uses the middle recipe's gross value; the native
recipe range is recorded because the player-selected line matters.

This does not add the annual values of every available recipe: that would assume
parallel production without first verifying the native production-point
concurrency semantics.

## A. Direct crop converters - proposed for review

| Catalog asset(s) | Native price | Native annual gross / recipe | Candidate price | Proposed rule | Status |
|---|---:|---:|---:|---|---|
| Cereal Factory | $240k | $99k-$199k | **$825k** | 10x cycles | Review |
| Grain Mill; US Grain Mill | $288k | $84k-$149k | **$550k** | 10x cycles | Review |
| Small Grain Mill | $36k | $8k-$15k | **$55k** | 10x cycles | Review |
| Grape Processing Unit | $240k | $102k-$105k | **$520k** | 10x cycles | Review |
| Small Grape Processing Plant | $36k | $10k-$11k | **$50k** | 10x cycles | Review |
| Oil Mill; US Oil Plant | $240k | $68k-$103k | **$500k** | 10x cycles; preserve native conversion pending oil-yield decision | Review |
| Small Oil Plant | $36k | $7k-$10k | **$50k** | 10x cycles; preserve native conversion pending oil-yield decision | Review |
| Spinnery; EU Spinnery; US Spinnery | $180k | $347k | **$1.75m** | 10x cotton line only | Review |
| Small Spinnery | $36k | $35k | **$175k** | 10x cotton line only | Review |
| Sugar Mill | $240k | $53k-$106k | **$400k** | 10x cycles | Review |
| Small Sugar Mill | $36k | $5k-$11k | **$40k** | 10x cycles | Review |
| Small Canned & Packaged Factory | $36k | $7k-$35k | **$80k** | 10x direct-crop lines | Review |
| Preserved Food Factory; US Canned & Packaged Factory | $330k | $73k-$354k | **$800k** | 10x direct-crop lines | Review |
| Potato Processing Plant | $360k | $130k | **$650k** | 10x cycles | Review |
| Soup Factory | $405k | $102k-$161k | **$570k** | 10x cycles | Review |
| US Rope Maker | $300k | $465k | **$2.33m** | 10x cycles | Review |

The large/small pairs above already use a native 10:1 throughput relationship.
The candidate preserves both that relationship and each recipe's native
input/output ratio.

## B. Farm supply - evaluated on gross output

Farm supply does not receive the crop converter's 10x rate multiplier. Its
rates are instead set by the explicit supply policies. The price candidate is
six game months of the resulting gross output value, with no deduction for
farm-produced inputs.

| Catalog asset | Current policy rate | Gross output/year | Six-month price candidate | Current effective price | Status |
|---|---|---:|---:|---:|---|
| Lime Production | 300 stone -> 3,000 lime, 1 cycle/hour | $194k | **$100k** | $100k | Configured |
| Fertilizer Production DS | Native 1 cycle/hour | $461k | **$230k** | $230k | Configured |
| Liquid Fertilizer Factory | 1 cycle/hour | $346k | **$175k** | $175k | Configured |
| Seed Production Factory | Normal recipe only, 15 cycles/hour | $584k | **$300k** | $180k | Review price; rush recipes disabled |

These are gross output values. They intentionally do not deduct manure, stone,
crop, fertilizer, water, lime, or seed-treatment inputs, per the farm-produced
input rule. They are still supply assets rather than an instruction to sell
those outputs for cash.

## C. Greenhouses - hold native by decision

| Catalog asset | Price | Policy |
|---|---:|---|
| Glass Greenhouse Small / Medium / Large | $13.5k / $27k / $54k | Hold native |
| Mushroom Greenhouse Small / Medium / Large | $9k / $18k / $36k | Hold native |
| Tarp Greenhouse Small / Medium / Large | $9k / $18k / $36k | Hold native |
| Saplings Greenhouse | $3k | Hold native |

## D. Energy - hold native pending an energy model

| Catalog asset | Price | Policy |
|---|---:|---|
| BGA 99 kW | $435k | Hold native |
| BGA 250 kW | $875k | Hold native |
| BGA 500 kW | $1.18m | Hold native |
| BGA 1 MW | $1.5m | Hold native |
| Hobos Hollow BGA 250 kW | $846k | Hold native |
| Straw Harvest Pellet Heat Plant | $42k | Hold native |

BGAs consume crop-derived material but are treated as energy infrastructure,
not ordinary crop converters, because electricity, methane, digestate and
power capacities require a separate model.

## E. Downstream food and animal production - proposed for review

These use the same candidate model as direct crop converters: 10x native
cycles/hour and a purchase price equal to six game months of gross output
value. Intermediate inputs are deliberately not subtracted.

| Catalog asset(s) | Native price | Native annual gross / recipe | Candidate price | Proposed rule | Status |
|---|---:|---:|---:|---|---|
| Generic Bakery; EU Bakery; US Bakery | $150k / $150k / $50k | $40k-$107k | **$210k** | 10x cycles | Review |
| Small Bakery | $36k | $4k-$11k | **$21k** | 10x cycles | Review |
| Generic Dairy; EU Dairy | $210k | $32k-$122k | **$395k** | 10x cycles | Review |
| Small Dairy | $36k | $3k-$12k | **$40k** | 10x cycles | Review |
| Generic Tailor Shop; US Tailor Shop | $240k | $491k | **$2.46m** | 10x cycles | Review |
| Small Tailor Shop | $36k | $49k | **$246k** | 10x cycles | Review |

The broad recipe ranges in bakeries and dairies are visible rather than hidden:
their candidate price uses the median recipe value. This avoids double-counting
the value of flour, fabric, milk, eggs, butter, or fruit while still treating
the output's gross value consistently with crop production.

## F. Forestry and construction materials - hold native pending forestry model

| Catalog asset(s) | Price | Policy |
|---|---:|---|
| Generic Carpentry; EU Carpenter; US Carpenter | $300k | Hold native |
| Small Carpenter | $36k | Hold native |
| US Cooper | $300k | Hold native |
| Generic Paper Factory; US Paper Mill | $525k | Hold native |
| Generic Sawmill; US Sawmill | $300k | Hold native |
| Generic Sawmill Placeable | $150k | Hold native |
| Small Sawmill | $36k | Hold native |
| Small Cement Factory; EU Cement Factory; US Cement Factory | $37k | Hold native |

## G. Livestock feed and forage silos - proposed for review

The American Silos Production Pack is identified by the stable canonical IDs
below, not its localized display title. These are storage/animal-feed assets;
they should not receive the crop-converter revenue multiplier.

| Canonical asset | Native price | Capacity | Native annual gross / recipe | Candidate price | Proposed rule |
|---|---:|---:|---:|---:|---|
| `FS25_AmericanSilosProductionPack:silos/staveSiloSmall.xml` | $50k | 250,000 L | $65k-$605k | **$70k** | Native cycles, median recipe |
| `FS25_AmericanSilosProductionPack:silos/staveSiloLarge.xml` | $100k | 500,000 L | $81k-$806k | **$90k** | Native cycles, median recipe |
| `FS25_AmericanSilosProductionPack:silos/steelSiloSmall.xml` | $75k | 750,000 L | $73k-$806k | **$80k** | Native cycles, median recipe |
| `FS25_AmericanSilosProductionPack:silos/steelSiloLarge.xml` | $125k | 1,000,000 L | $81k-$806k | **$90k** | Native cycles, median recipe |

Every silo can convert grass to hay; chaff, grass, hay, or straw to silage;
the crop recipe `500 maize + 250 wheat + 175 canola + 75 sugar beet` to
1,000 pig food; and `400 silage + 400 hay + 200 straw` to 1,000 forage. The
four rates and capacities are not a clean small-to-large linear family, so
their purchase prices should still be checked against intended livestock demand
and storage capacity. The candidate values use native cycles and the median
recipe's six-game-month gross output (144 active game-hours), with no input-cost
deduction. No throughput multiplier is proposed: the native 4,000-5,000
cycles/hour drying and silage rates are already extremely high, and multiplying
them would create an artificial processing-rate policy rather than a purchase
price policy.

## Catalog coverage check

| Group | Assets |
|---|---:|
| Direct crop converters | 21 |
| Farm supply | 4 |
| Greenhouses | 10 |
| Energy | 6 |
| Downstream food / animal | 10 |
| Forestry / construction materials | 14 |
| American Silos Production Pack | 4 |
| **Total catalog assets** | **69** |

## Before broad application

1. Decide whether oil remains at its native 50% physical conversion or receives
   a separate 78% yield policy with correspondingly adjusted cycles.
2. Review the high candidates: spinneries, rope maker, seed supply, and
   preserved-food lines against intended server-scale ownership.
3. Choose farm-supply purchase prices from acreage/application demand rather
   than gross output value.
4. Model energy, forestry, animal, and downstream chains separately before
   changing their prices or rates.
5. After approvals, encode only the approved canonical IDs in
   `SiN_FS25_ProductionPolicy`; all other catalog entries continue native.
