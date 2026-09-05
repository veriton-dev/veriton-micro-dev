# SAMPLE — Code Review Sprint format (synthetic example)
Target: toy `parse_total(items)` — 24 lines.
## Findings
- [high] `total += float(x)` throws on None — guard or coerce, add test.
- [med] O(n²) dedupe via list scan — use set for n>1k.
- [low] no type hints — add `list[float] -> float`.
## Diff
```diff
-  for x in items: total += float(x)
+  for x in items:
+    if x is None: continue
+    total += float(x)
```
Test notes: 3 cases (empty, None-mixed, 10k rows) — all pass. Refund rule: fail acceptance → free fix-up 48h, then refund.
