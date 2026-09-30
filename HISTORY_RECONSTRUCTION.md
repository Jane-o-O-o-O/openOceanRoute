# Reconstructed Git history / 重建 Git 历史

This repository history was reconstructed on 2026-10-07 from the actual frozen
OceanRoute 0.1 through 0.12.1 source-release snapshots. These are 300 nonempty
changes grouped by software domain, tests, interface, documentation, and build
tools. They are not the original development commits and are not evidence that
this project was developed or originally committed during August or September.

本仓库的300个提交是在2026-10-07，根据已封存的0.1至0.12.1真实源码快照重建。
为满足仓库所有者的明确要求，Author和Committer日期统一安排在2026年八月及九月：
八月140个、九月160个。这些日期是重建元数据，不是当时真实开发或提交的时间。
每个提交说明都带有Reconstructed-History与实际生成日期标记。

## Date allocation

The authenticated GitHub commit search returned 206 indexed, visible commits
for the account author in August/September 2026: 96 in August and 110 in September.
Converted to Asia/Shanghai, their mean times of day were 17:19:48 and 19:39:36,
respectively. This is a scoped search result, not a claim of completeness for
every private or unindexed branch. Other repository names and raw activity are
kept in the local `.git/commit-import-audit/` directory and are not published.

The monthly ratio is scaled to 140/160. Counts use largest-remainder allocation
over the observed 59 active dates; times use empirical within-day quantiles.
Duplicate times are separated by one second. Reconstructed mean times are
17:25:30 in August and 19:42:11 in September; no exact average-match is claimed.
Both Git timestamps use the same +08:00 scheduled date. `history/commit-plan.json`
lists all 300 groups, their paths and dates, and the original snapshot hashes.

## Preservation and scope

The final tracked product source bytes equal the frozen 0.12.1 snapshot and the
existing workspace. Intermediate groups are review units; only completed release
boundaries represent the corresponding complete selected source snapshot.
The history does not claim each intermediate group is independently runnable.
Tagged boundaries use `reconstructed/v...` to distinguish them from original tags.

No production feature or released EXE/wheel/PDF/source archive was changed.
Runtime caches, installed dependencies, Wine prefixes, temporary QA copies,
application databases, original vendor manuals, and generated builds/reports
are excluded. Small existing scientific test fixtures are retained with their
existing source notices so regression tests can use their actual input data.
Windows machine acceptance remains unperformed; recorded Wine test results are
not native Windows machine acceptance. Existing engineering/model limitations
remain documented in the source manuals and implementation status.

The import script reads local frozen release ZIPs, never checks out partial
snapshots into the product workspace, and refuses to replace existing Git history.
It creates dated commit objects and verifies source parity before attaching the
new `main` ref. No existing remote history is overwritten or force-pushed.
