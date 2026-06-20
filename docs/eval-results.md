# Self-healing accuracy benchmark

Eval set: **8** cases (easy → hard, deterministic local fixtures). Harness: `scripts/heal_eval.py`. Model: `gemini-2.5-flash-lite`, temperature 0.

**Chosen confidence threshold: 0.7.** A missed heal is a clean failure; a false-heal (relocating to the *wrong* element) is a lie, so the threshold is tuned to keep false-heals near zero.

## Results at the chosen threshold

- Heal success rate: **75%** (6/8)
- **False-heal rate: 0%** (0/8)
- Misses (clean failures): 2/8
- Avg confidence — hits: 1.00 · non-hits: 0.85

## Per-case

| case | difficulty | conf | resolved | outcome |
|------|-----------|------|----------|---------|
| login_button | easy | 1.00 | truth | success |
| search_box | easy | 1.00 | truth | success |
| pricing_nav | easy | 1.00 | truth | success |
| accept_terms | medium | 1.00 | truth | success |
| country_select | medium | 1.00 | truth | success |
| add_to_cart_specific | hard | 1.00 | truth | success |
| contact_submit | hard | 0.80 | ambiguous | miss |
| delete_item2 | hard | 0.90 | none | miss |

## Reading this
- **success** — accepted (conf ≥ threshold) and resolved to the intended element.
- **false_heal** — accepted but resolved to the WRONG element. This is the dangerous outcome; the threshold exists to drive it to zero.
- **miss** — rejected by the threshold, or resolved to nothing/ambiguous. Surfaces as a clean StepFailed, not a wrong pass.
