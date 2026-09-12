# Why the near-miss rule reports instead of merging

`D6` inserts two products it knows are probably duplicates of catalog rows 21 and 28, and reports them for review. That is the one place this solution knowingly leaves a duplicate in a catalog it was asked not to duplicate, so it is the decision most worth challenging.

This is the evidence behind it. Reproduce with:

```
python scripts/analyse_merge_threshold.py
```

## Conclusion first

The candidate rule — same normalized brand, token overlap above 0.5 — cannot be promoted to an automatic merge, because **the threshold measures name length rather than similarity.** Auto-merging would be correct on the supplied file and wrong on a plausible variant of it, for reasons unrelated to the products involved.

## Merging would cost nothing measurable

Worth stating up front, because the case for reporting cannot rest on a cost that isn't there.

| | Report (current) | Auto-merge |
| --- | --- | --- |
| Product rows after | 978 | 976 |
| New products | 3 | 1 |
| Links created | 257 | 257 |
| Review candidates | 2 | 0 |

Both translation records come from sellers not otherwise linked to the target product, so neither link would be suppressed. No seller's offer is lost. On this file, auto-merge is clean.

## The catalog contains no false positives — and that is luck

No pair of distinct catalog products shares a brand and scores above 0.5. But **four pairs sit exactly on the threshold:**

| Score | Product A | Product B | Differs in |
| --- | --- | --- | --- |
| 0.500 | `Tennis Racket Adult` | `Tennis Racket Bag` | adult / bag |
| 0.500 | `Hockey Stick Ice` | `Hockey Skates Ice` | stick / skates |
| 0.500 | `Colander Stainless Steel` | `Ladle Stainless Steel` | colander / ladle |
| 0.500 | `Nightstand Set of 2` | `Bar Stools Set of 2` | nightstand / bar stools |

A racket and a bag for a racket. A stick and skates. A colander and a ladle. All genuinely different products, all one step below the cutoff.

## The headroom is an illusion

Jaccard overlap on token sets is quantised by name length. Two names of *n* tokens differing by exactly one token score:

| n | 2 | 3 | 4 | 5 | 6 | 7 |
| --- | --- | --- | --- | --- | --- | --- |
| score | 0.333 | **0.500** | 0.600 | 0.667 | 0.714 | 0.750 |

Nothing is representable between 0.500 and 0.600 for a three-token name. The nominal gap between the lowest true positive (0.600) and the highest known-wrong pair (0.500) looks like 0.1 of margin. It is zero clearance: the threshold sits directly on four known-wrong pairs, with no value available beneath it.

So `> 0.5` does not mean "similar enough". It means *differs by exactly one token, and has at least four tokens*.

## The comparison that settles it

| Score | Verdict | Pair | Differing token |
| --- | --- | --- | --- |
| 0.500 | different products | `Tennis Racket Adult` / `Tennis Racket Bag` | adult / bag |
| 0.500 | different products | `Hockey Stick Ice` / `Hockey Skates Ice` | stick / skates |
| 0.500 | different products | `Colander Stainless Steel` / `Ladle Stainless Steel` | colander / ladle |
| 0.600 | **same product** | `Roteador WiFi 6 TP-Link` / `Router WiFi 6 TP-Link` | roteador / router |
| 0.667 | **same product** | `Processador AMD Ryzen 9 7950X` / `Processor AMD Ryzen 9 7950X` | processador / processor |

Every pair differs in exactly one token, and in every case that token is the product-type word. Structurally identical. Three are different products; two are the same product.

The metric cannot separate them. What separates them is that *roteador* is Portuguese for *router*, and no lexical rule knows that.

Two counterfactuals make the fragility concrete:

- Had the catalog named it `Router WiFi 6` — three tokens instead of four — the true positive would score exactly 0.500 and be excluded. The correct merge would be missed.
- Had the catalog held `Colander Stainless Steel Large` and `Ladle Stainless Steel Large` — four tokens instead of three — that pair would score 0.600 and a colander would be merged into a ladle.

Neither counterfactual involves any change in how similar the products are. Only their name lengths.

## Asymmetric failure

The two mistakes do not cost the same.

**A missed merge** leaves 2 duplicate rows in 978 — 0.2% catalog bloat. It is named in every run's findings report with the evidence and the score, and a human can act on it. Reversible.

**A wrong merge** collapses two distinct products into one and redirects a seller's offer onto the wrong product. Because `D2` never writes the incoming values, the database keeps no record of what was merged or what it was called. Only the report knows. Recovering means reconstructing the split by hand from a log.

Given a metric that cannot reliably distinguish the two cases, the cheap mistake is the one to make.

## What would actually resolve it

- **A real product identifier.** GTIN or EAN turns this from inference into a lookup. This is the answer a marketplace reaches for, and its absence from the input is the root cause.
- **Locale-aware matching on the head noun.** The differing token is always the product-type word, so a translation lookup restricted to that position would catch both cases without the length artifact. Needs a dictionary the exercise does not provide.
- **A human review queue.** What `D11` builds. It does not resolve the ambiguity; it routes it to something that can.

A hardcoded `roteador → router` map would also work, and is rejected in `D6`: it catches exactly the two cases in this file and generalizes to nothing, which is worse than a documented limitation because it looks like a solution.
