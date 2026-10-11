# HANDOFF

更新：2026-10-11 · by Claude · HEAD `a3158f5`
<!-- 規則見 AGENTS.md「Handoff」。上限 150 行；過期內容移到 docs/archive/HANDOFF-YYYY-MM.md。 -->

## ▶ 下一步（只放一件，必須帶 #Issue）
#129 [P1] 修正 planning/support 套件 docstring 仍寫「Noncanonical」、與 AGENTS.md（D2：runtime 是 planner source owner，決策權仍在 Nexus-new）矛盾的問題。依據：PR #141 已涵蓋最新 invariant 缺口 #137/#138，剩下未處理項中 #129 是唯一 P1；main 上 `src/nexus_runtime/planning/__init__.py` 與 `support/__init__.py` 第一行仍是 Noncanonical。依 AGENTS.md，planner 行為以外的文字修正也要避免暗示 route 決策權轉移。

## 進行中
- PR #141（Refs #137、#138）replan 遇 effect journal 非 COMPLETED 記錄即拒絕；custom runner 強制 effect authorization：mergeable=clean，full-suite、installed wheel 與 Nexus Core issue run/completion 最新一輪皆綠（Core issue 綁 #137）。等 owner 審查合併。#138 只修一半：自簽 authorization 的來源驗證需新的 authority port，合併後 #138 不應直接關閉。

## 待確認（沒有 Issue 號）
- Split repair 各階段 issue（#92、#93、#95、#100、#101、#102、#103、#104、#105、#108、#116）對應的 commit 都已在 main（Phase 0–4a、2.5 經 PR #112、#121 合併），但 issue 仍 open。需逐一跑 `nexus-certify issue-check` 確認後關閉。
- Phase 2.5 已移除四個 alias shim，但 `src/nexus_runtime/_compat_aliases.py` 與 `tests/test_compat_aliases.py` 仍存在；需確認是否還有使用者，有沒有要另外開單移除。

## 卡住 / 注意
- PR #21（owner integration CI）base 是舊的 `codex/repo-split` 而非 main，自 2026-09-15 無活動；需 owner 決定 retarget 或關閉。
- #130 [P2] build_runtime 長達 6,068 行；#131 [P2] 兩個同名 ExecutionCoordinator。皆未開工。
- #47 為 `[HYBRID_CANARY]` 研究探針（no implementation）；`NEXUS-HYBRID-REPLICATION-*` 留言屬 RESEARCH_OBSERVATION_ONLY。
- 移除相容層須附七個家族 repo 的 `git grep` 零引用證據（D1）；`OUTCOME_UNKNOWN` 永不等於可重試。
