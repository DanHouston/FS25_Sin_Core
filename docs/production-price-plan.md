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
purchase-price candidate = highest native annual gross x 10 x 6/12
```

A game year is 288 active game-hours (24 hours x 12 months). Gross output uses
the arithmetic average of the native Mar-Feb fill-type price factors. Farm-grown
crop inputs are not charged as a cash expense. Where a factory offers multiple
recipes, the price candidate uses the highest sustainable recipe's gross value;
the native recipe range is recorded because the player-selected line matters.
A recipe is only considered sustainable when its inputs and native production
rate can be supplied repeatedly; a one-off price spike or unavailable input is
not used as the high case.

This does not add the annual values of every available recipe: that would assume
parallel production without first verifying the native production-point
concurrency semantics.

## A. Direct crop converters - proposed for review

| Catalog asset(s) | Native price | Native annual gross / recipe | Candidate price | Proposed rule | Status |
|---|---:|---:|---:|---|---|
| Cereal Factory | $240k | $99k-$199k | **$995k** | 10x cycles; high sustainable recipe | Review |
| Grain Mill; US Grain Mill | $288k | $84k-$149k | **$745k** | 10x cycles; high sustainable recipe | Review |
| Small Grain Mill | $36k | $8k-$15k | **$75k** | 10x cycles; high sustainable recipe | Review |
| Grape Processing Unit | $240k | $102k-$105k | **$525k** | 10x cycles; high sustainable recipe | Review |
| Small Grape Processing Plant | $36k | $10k-$11k | **$55k** | 10x cycles; high sustainable recipe | Review |
| Oil Mill; US Oil Plant | $240k | $68k-$103k | **$515k** | 10x cycles; high sustainable recipe; preserve native conversion pending oil-yield decision | Review |
| Small Oil Plant | $36k | $7k-$10k | **$50k** | 10x cycles; high sustainable recipe; preserve native conversion pending oil-yield decision | Review |
| Spinnery; EU Spinnery; US Spinnery | $180k | $347k | **$1.735m** | 10x cotton line only; high recipe | Review |
| Small Spinnery | $36k | $35k | **$175k** | 10x cotton line only; high recipe | Review |
| Sugar Mill | $240k | $53k-$106k | **$530k** | 10x cycles; high sustainable recipe | Review |
| Small Sugar Mill | $36k | $5k-$11k | **$55k** | 10x cycles; high sustainable recipe | Review |
| Small Canned & Packaged Factory | $36k | $7k-$35k | **$175k** | 10x direct-crop lines; high sustainable recipe | Review |
| Preserved Food Factory; US Canned & Packaged Factory | $330k | $73k-$354k | **$1.77m** | 10x direct-crop lines; high sustainable recipe | Review |
| Potato Processing Plant | $360k | $130k | **$650k** | 10x cycles | Review |
| Soup Factory | $405k | $102k-$161k | **$805k** | 10x cycles; high sustainable recipe | Review |
| US Rope Maker | $300k | $465k | **$2.325m** | 10x cycles; high sustainable recipe | Review |

The large/small pairs above already use a native 10:1 throughput relationship.
The candidate preserves both that relationship and each recipe's native
input/output ratio.

## B. Farm supply - evaluated on gross output

Farm supply does not receive the crop converter's 10x rate multiplier. Its
rates are instead set by the explicit supply policies. The price candidate is
six game months of the highest sustainable resulting gross output value, with
no deduction for farm-produced inputs (`highest annual gross x 6/12`).

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
cycles/hour and a purchase price equal to six game months of the highest
sustainable gross output value (`highest annual gross x 10 x 6/12`). Intermediate
inputs are deliberately not subtracted.

| Catalog asset(s) | Native price | Native annual gross / recipe | Candidate price | Proposed rule | Status |
|---|---:|---:|---:|---|---|
| Generic Bakery; EU Bakery; US Bakery | $150k / $150k / $50k | $40k-$107k | **$535k** | 10x cycles; high sustainable recipe | Review |
| Small Bakery | $36k | $4k-$11k | **$55k** | 10x cycles; high sustainable recipe | Review |
| Generic Dairy; EU Dairy | $210k | $32k-$122k | **$610k** | 10x cycles; high sustainable recipe | Review |
| Small Dairy | $36k | $3k-$12k | **$60k** | 10x cycles; high sustainable recipe | Review |
| Generic Tailor Shop; US Tailor Shop | $240k | $491k | **$2.455m** | 10x cycles; high recipe | Review |
| Small Tailor Shop | $36k | $49k | **$245k** | 10x cycles; high recipe | Review |

The broad recipe ranges in bakeries and dairies are visible rather than hidden:
their candidate price uses the highest sustainable recipe value. This avoids
subtracting the value of flour, fabric, milk, eggs, butter, or fruit while still
treating the output's gross value consistently with crop production.

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

## G. Livestock feed and forage silos - historical price basis and current rates

The American Silos Production Pack is identified by the stable canonical IDs
below, not its localized display title. These are storage/animal-feed assets;
they should not receive the crop-converter revenue multiplier.

| Canonical asset | Tier | Native price | Capacity | Current policy recipe rates | Original policy annual gross / recipe (before 4x silage change) | Candidate price |
|---|---|---:|---:|---|---:|---:|
| `FS25_AmericanSilosProductionPack:silos/staveSiloSmall.xml` | Small | $50k | 250,000 L | Hay 4,000; silage 16,000; pig food 1.5; forage 2.5 cycles/h | $65k-$605k | **$302.5k** |
| `FS25_AmericanSilosProductionPack:silos/steelSiloSmall.xml` | Small | $75k | 750,000 L | Hay 4,000; silage 16,000; pig food 1.5; forage 2.5 cycles/h | $65k-$605k | **$302.5k** |
| `FS25_AmericanSilosProductionPack:silos/staveSiloLarge.xml` | Large | $100k | 500,000 L | Hay 5,000; silage 20,000; pig food 2; forage 3.5 cycles/h | $81k-$806k | **$403k** |
| `FS25_AmericanSilosProductionPack:silos/steelSiloLarge.xml` | Large | $125k | 1,000,000 L | Hay 5,000; silage 20,000; pig food 2; forage 3.5 cycles/h | $81k-$806k | **$403k** |

“Aligned capability” means matching recipe IDs, input amounts, output amounts,
and cycles/hour within each tier. The large pair already meets that rule in the
source XMLs. The small pair has matching inputs/outputs but native rates of
4,000/1.5/2.5 (stave) versus 4,500/2/3 (steel) for hay/silage, pig food, and
forage respectively. The policy targets the lower existing small-tier rates, so
the steel small silo is configured to match the stave small profile for hay,
pig food, and forage rather than raising the smaller stave silo. All four
silage recipes are now configured at four times the original small/large tier
rates. The source ZIP remains untouched.

Every silo can convert grass to hay; chaff, grass, hay, or straw to silage;
the crop recipe `500 maize + 250 wheat + 175 canola + 75 sugar beet` to
1,000 pig food; and `400 silage + 400 hay + 200 straw` to 1,000 forage. The
capability policy targets matching recipe IDs, input/output amounts, and
cycles/hour within each tier. Storage capacities remain native and differ
between stave and steel: 250,000/750,000 L for small and 500,000/1,000,000 L
for large.

The four native rates and capacities are not a clean small-to-large linear
family, so their purchase prices should still be checked against intended
livestock demand and storage capacity. The candidate values use the original
policy rates and the highest sustainable recipe's six-game-month gross output
(144 active game-hours), with no input-cost deduction. The original price
calculation did not multiply throughput. The later 4x change applies only to
silage throughput; the purchase prices still derive from the pig-food gross
case, which is unchanged.

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
