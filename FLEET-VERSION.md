# Fleet version

Upstream release: v2026.9.24 (f97608f178d1ffeca59860195ab7da295f7c8e5f).

Previous fleet: 4303dfa9c9c9067fc11a8c488444253fbd5a1cc4.

Carried changes: 198. Every row is in FLEET-CARRIED.json.

Test comparison passed. Receipt hash: 2ef4d87618179a7e0819704d1e1dd8855f2eff62585080e0ab16b7bc7acbab0d.

Switch order: Max, Snowdrop, TurnerBook, Sheldon, Rosie.

Classified carried rows: 263. FLEET-CARRIED.json embeds every original row and its allowed outcome under the commit that carries it. The 261 rc2 rows, H1-001 and K2j-001 are preserved. K2j-001 binds both K2i and K2j.

Product and test source is unchanged from tested commit 4303dfa9c9c9067fc11a8c488444253fbd5a1cc4. Only release metadata changed. The 72-file comparison and 17-file K2j full-run receipts are reused under the owner speed rule.

Prepared for executor publication. This final candidate was not pushed by U5.

rc3 (7 Oct 2026): built on rc2.2 (f182607ba75). Three fleet fixes are carried on top, rows RC3-001 to RC3-003 in FLEET-CARRIED.json: cron workers start through the installation-bound runtime command; a queued fleet message is marked done when the bot answers it; the queued timeout counts from requeue and from receiver restart. Classified carried rows: 266. Tests for the touched files were compared with rc2.2: the only failure (test_reported_linger_finishes_after_short_lived_drain_script_exits, no ruamel in the test runner) is the same on both.
