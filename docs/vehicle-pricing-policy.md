# Vehicle pricing policy

The vehicle module in `SiN_FS25_Policy` prices third-party motor vehicles only. It does not modify base-game assets, map-owned assets, implements, or unsupported vehicle categories.

For a supported mod vehicle, the policy reads its XML motor configurations and calculates:

`effective purchase price = (zero-surcharge engine HP × category dollarsPerHp) + surcharge of the highest-HP engine`

The maximum engine surcharge is included once because FS25 multiplayer vehicle configuration changes are free. The policy therefore returns one fixed effective price for every engine selection, preventing a second engine charge. Rates reside in `config/vehicle-pricing-policy.xml` and are intentionally external.

After the initial HP calculation, mod vehicles are grouped by `(category, maximum configured HP)`. Exact groups with at least two members receive the rounded arithmetic mean of their calculated prices. This prevents two effectively identical trucks (for example, both topping out at 902 HP) from receiving radically different prices solely because one has a lower starting engine. Singleton groups retain their normal policy price.

Category caps are applied before that peer-group averaging. The current caps are $70,000 for `cars` and $250,000 for `trucks`; tractor and motorcycle categories are uncapped. This keeps broad FS25 categories from producing implausibly expensive light vehicles while preserving the existing heavy-semi rate below its cap.

The mod's declared source price is diagnostic only; it may have been edited or authored inconsistently. Pricing starts from the HP calculation, with untouched base-game anchors supplying the native floor.

Finally, each category is processed as a monotonic maximum-HP curve. After equal-HP averaging, vehicles are sorted by maximum HP and canonical ID; each later tier is raised when necessary so a higher-HP vehicle cannot be cheaper than a lower-HP vehicle. Category caps remain hard upper bounds.

Untouched base-game vehicles provide read-only anchor points for this curve. A mod vehicle cannot undercut the highest native price anchor at or below its maximum HP, but base-game store items are never modified.

The catalog price is set after StoreManager loading for the client UI. The policy deliberately does not wrap `EconomyManager:getBuyPrice`: FS25's native store and purchase flow reads the modified matched-mod store record on both client and dedicated server. Configuration entries remain native because `ShopConfigScreen` uses them to calculate option deltas. All clients should install the identical ZIP so their catalog display agrees with the server.

`sinVehiclePricingPolicy` logs deterministic descriptors containing canonical ID, category, source price, base and maximum HP, maximum engine surcharge, and effective price. Group adjustments are logged as `horsepower group normalized` records.

Live test: enable the policy alongside a vehicle mod, restart FS25, run `sinVehiclePricingPolicy`, and compare the store price and actual farm deduction to `baseHp × rate + maxEngineSurcharge`. Confirm a base-game vehicle retains its native price and an unsupported mod implement is unchanged.
