# HANDOFF

更新：2026-10-13 · by Claude · HEAD `2766c15`（分支 claude/agy-fu1-fu2：#179/#180/#181）
<!-- 規則見 AGENTS.md「Handoff」。上限 150 行；過期內容移到 docs/archive/HANDOFF-YYYY-MM.md。 -->

## ▶ 下一步（只放一件，必須帶 #Issue）
#179 [high]（含 #180、#181）合併 Agy 權限修正 PR：Agy 只接受單一執行檔；拒絕繞過權限的旗標；permission mode 由實際 argv 推導；headless 權限拒絕列為 typed 非交付；Agy 一律使用 ephemeral cwd，而且必須明確指定模型。依據：#64 的事後獨立審查（APPROVE_WITH_FOLLOWUPS）。Nexus-new#29 的 canary 證據依賴 `provider_permission_mode` 是真實值。

## 進行中
- 分支 claude/agy-fu1-fu2：修正 #179/#180/#181，full suite 894 passed。新規則下，Nexus-new 有兩處會 fail closed，已告知 Owner：`product_capability_canary_helper` 呼叫 Agy 時沒有帶模型，gateway route 自帶多參數 `online_command`。
- PR #141 已合併（55c32f3）；#137、#138 仍 open。#138 只修了一半：自簽 authorization 的來源驗證需要新的 authority port，不應直接關閉。

## 待確認（沒有 Issue 號）
- Split repair 各階段 issue（#92、#93、#95、#100、#101、#102、#103、#104、#105、#108、#116）對應的 commit 都已在 main（Phase 0–4a、2.5 經 PR #112、#121 合併），但 issue 仍 open。需逐一跑 `nexus-certify issue-check` 確認後關閉。
- Phase 2.5 已移除四個 alias shim，但 `src/nexus_runtime/_compat_aliases.py` 與 `tests/test_compat_aliases.py` 仍存在；需確認是否還有使用者，有沒有要另外開單移除。

## 卡住 / 注意
- #129 [P1]：planning/support 套件的 docstring 仍寫「Noncanonical」，與 AGENTS.md D2 矛盾，main 上還沒修。
- PR #21（owner integration CI）base 是舊的 `codex/repo-split` 而非 main，自 2026-09-15 無活動；需 owner 決定 retarget 或關閉。
- #130 [P2] build_runtime 長達 6,068 行；#131 [P2] 兩個同名 ExecutionCoordinator。皆未開工。
- #47 為 `[HYBRID_CANARY]` 研究探針（no implementation）；`NEXUS-HYBRID-REPLICATION-*` 留言屬 RESEARCH_OBSERVATION_ONLY。
- 移除相容層須附七個家族 repo 的 `git grep` 零引用證據（D1）；`OUTCOME_UNKNOWN` 永不等於可重試。
