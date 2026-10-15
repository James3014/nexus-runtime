# HANDOFF

更新：2026-10-16 · by Claude · HEAD `0f918f3`（分支 feat/receipt-only-consumer-visibility：#216）
<!-- 規則見 AGENTS.md「Handoff」。上限 150 行；過期內容移到 docs/archive/HANDOFF-YYYY-MM.md。 -->

## ▶ 下一步（只放一件，必須帶 #Issue）
#216 合併「governance gate 證據只進收據、不進 prompt」：bundle entry 可帶 consumer_visibility="receipt_only"（在封存內、受 hash 保護），來源是 capability 紀錄自己標記（Nexus-new strict lane）或 request route 的 receipt_only_capabilities；這種 entry 不進 [NEXUS MODEL CONTEXT]、在 [NEXUS CAPABILITY EVIDENCE] 顯示成 PR-4 前的佔位，收據與 attestation 不變。合併後 Nexus-new #2278 改指新 runtime sha，再寫 anchor。

## 進行中
- #212 已合併（0cedab1，PR #213）。#191 已合併（6782e38，PR #192）。#179、#180、#181 已合併（f9adb20，PR #182）。Nexus-new 有兩處會 fail closed，尚待處理：canary helper 沒有帶模型，gateway 的 online_command 帶了多個參數。
- Nexus-new#1996：自動更新模型版本的機制建置中。

## 待確認（沒有 Issue 號）
- Split repair 各階段 issue（#92、#93、#95、#100、#101、#102、#103、#104、#105、#108、#116）對應的 commit 都已在 main（Phase 0–4a、2.5 經 PR #112、#121 合併），但 issue 仍 open。需逐一跑 `nexus-certify issue-check` 確認後關閉。
- Phase 2.5 已移除四個 alias shim，但 `src/nexus_runtime/_compat_aliases.py` 與 `tests/test_compat_aliases.py` 仍存在；需確認是否還有使用者，有沒有要另外開單移除。

## 卡住 / 注意
- #129 [P1]：planning/support 套件的 docstring 仍寫「Noncanonical」，與 AGENTS.md D2 矛盾，main 上還沒修。
- PR #21（owner integration CI）base 是舊的 `codex/repo-split` 而非 main，自 2026-09-15 無活動；需 owner 決定 retarget 或關閉。
- #130 [P2] build_runtime 長達 6,068 行；#131 [P2] 兩個同名 ExecutionCoordinator。皆未開工。
- #47 為 `[HYBRID_CANARY]` 研究探針（no implementation）；`NEXUS-HYBRID-REPLICATION-*` 留言屬 RESEARCH_OBSERVATION_ONLY。
- 移除相容層須附七個家族 repo 的 `git grep` 零引用證據（D1）；`OUTCOME_UNKNOWN` 永不等於可重試。
