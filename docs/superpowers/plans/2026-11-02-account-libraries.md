# Independent Account Vocabulary Implementation Plan

> **For agentic workers:** Execute this approved plan inline, task by task, using test-first changes and the existing Sites publication workflow.

**Goal:** Give each invited ChatGPT account an independent durable vocabulary library while preserving the owner's existing records and cross-device sync.

**Architecture:** Keep the existing Sites identity and audience. Pass the platform user ID explicitly to every D1 read/write; migrate the legacy namespace only for the configured, verified owner email, using an atomic copy plus a once-only migration record.

**Tech Stack:** Vinext, Cloudflare D1, Drizzle, Node test runner, existing Playwright browser tests.

## Global Constraints
- Preserve project ID appgprj_6abdb2bf4bc081919962d8a3f5614688 and current restricted audience.
- No browser-supplied ownership; no first-visitor ownership claim.
- Keep all existing D1 migrations immutable; append schema-only migrations.
- Back up live D1 before deployment and retain legacy source rows.
- Keep credentials out of files, source, command arguments, and output.

### Task 1: Prove isolation and preserve legacy ownership
**Files:** Create tests/cloud-accounts.test.mjs and lib/cloud-accounts.mjs; modify lib/cloud-store.mjs, lib/api.mjs, db/schema.ts; generate a new Drizzle migration.
**Interfaces:** readState(db, accountId, receiptIds=[]); commitOperations(db, accountId, operations); migrateLegacyAccount(db, user, configuredOwnerEmail).

- [ ] Write API tests using real local D1 for empty guest libraries, same operation IDs across accounts, independent review/update/delete, scoped import/export, forged body ownership, concurrent owner migration, and protected existing destinations.
- [ ] Run node --test tests/cloud-accounts.test.mjs and confirm shared-state assertions fail before implementation.
- [ ] Require a nonempty account ID in storage functions. Use WHERE owner=? and bind accountId everywhere, including receipts, revision guards, and deletes.
- [ ] Add account_migrations(name,user_id,commit_token) schema. Implement guarded atomic INSERT SELECT copies from owner to the verified owner's user ID, once per migration token.
- [ ] Pass platform identity from API routes; use LEGACY_OWNER_EMAIL only for the legacy migration. Reject absent identity with 401 and migration conflicts with 409.
- [ ] Run db:generate and the focused account tests.

### Task 2: Verify the actual browser experience
**Files:** Modify app/route.ts, ui-source/web/cloud_sync.js, tests/cloud-store.test.mjs, tests/cloud-browser.test.mjs; regenerate lib/vocab.html.
**Interfaces:** window.CLOUD_ACCOUNT={userId,email}; existing CloudSync start/flush and account-scoped browser keys remain supported.

- [ ] Adapt existing storage/browser test fixtures to explicit authenticated accounts.
- [ ] Add a browser test that creates separate account sessions, checks same-word independence and same-account second-device sync, and switches accounts in one browser profile to check cache isolation.
- [ ] Run the browser test and confirm cross-account leakage before implementing storage changes where feasible; the API test in Task 1 is the primary red proof.
- [ ] Display the signed-in email safely with textContent and describe independent libraries; retain sync-state and sign-out controls.
- [ ] Run npm run generate:ui and npm test. Check the actual local preview with the supported browser tooling.

### Task 3: Publish the tested change and verify data
**Files:** Update README.md and docs/account-libraries.md. Runtime: LEGACY_OWNER_EMAIL, set as a secret through Sites.
**Interfaces:** existing Sites workflow source result and credential; native private save/deploy; platform D1 row viewer.

- [ ] Confirm the complete live backup is readable and its revision stayed stable during capture.
- [ ] Configure the exact owner email from get_site; preserve existing vision runtime values.
- [ ] Document migration, independent libraries, cross-device sync, and rollback limits. Do not roll back to the old shared-space code after new per-account writes.
- [ ] Package with the Sites workflow using npm tests already completed and the official build-site helper; preserve its exact pushed SHA/archive.
- [ ] Deploy to the existing restricted Site and wait for a successful terminal result.
- [ ] Verify the owner migration and record counts in production when an authenticated owner visit is available; otherwise report that the first owner login completes migration.

