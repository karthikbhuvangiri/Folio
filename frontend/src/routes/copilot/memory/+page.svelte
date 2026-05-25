<script>
    import '$lib/styles/copilot.css';
    import { onMount } from 'svelte';
    import { api } from '$lib/api.js';
    import { activeProfile } from '$lib/stores/profileStore.js';
    import ProfileSwitcher from '$lib/components/ProfileSwitcher.svelte';

    let entries = [];
    let sections = [];
    let proposals = [];
    let sessionSummaries = [];
    let financialFeedback = [];
    let statedIntents = [];
    let appConfig = {
        miraFinancialFeedbackLoopEnabled: false,
        miraStatedIntentMemoryEnabled: false,
    };
    let markdown = '';
    let tokenEstimate = 0;
    let charCount = 0;
    let budget = 4000;
    let loading = true;
    let actionNotice = '';
    let activeProfileId;
    activeProfile.subscribe((p) => { activeProfileId = p; });

    let editingId = null;
    let editBuffer = '';
    let editingSummaryId = null;
    let summaryEditBuffer = '';
    let editingIntentId = null;
    let intentEditBuffer = '';
    let consolidating = false;

    let newSection = 'preferences';
    let newBody = '';
    let newEvidence = '';

    async function refresh() {
        loading = true;
        try {
            const [entriesResp, mdResp, propsResp, summariesResp, configResp, feedbackResp, statedResp] = await Promise.all([
                api.getMemoryEntries(activeProfileId),
                api.getMemoryMarkdown(activeProfileId),
                api.getMemoryProposals(activeProfileId),
                api.getMiraSessionSummaries(activeProfileId),
                api.getAppConfig(),
                api.getMiraFinancialFeedback(activeProfileId),
                api.getMiraStatedIntents(activeProfileId, true),
            ]);
            appConfig = { ...appConfig, ...(configResp || {}) };
            entries = entriesResp.items || [];
            sections = entriesResp.sections || [];
            markdown = mdResp.markdown || '';
            tokenEstimate = mdResp.token_estimate || 0;
            charCount = mdResp.char_count || 0;
            budget = mdResp.budget || 4000;
            proposals = propsResp.items || [];
            sessionSummaries = summariesResp.items || [];
            financialFeedback = appConfig.miraFinancialFeedbackLoopEnabled ? (feedbackResp.feedback || []) : [];
            statedIntents = appConfig.miraStatedIntentMemoryEnabled ? (statedResp.items || []) : [];
        } catch (e) {
            actionNotice = e?.message || 'Failed to load memory.';
        } finally {
            loading = false;
        }
    }

    function setNotice(msg) {
        actionNotice = msg;
        setTimeout(() => { if (actionNotice === msg) actionNotice = ''; }, 3500);
    }

    function memoryKey(item) {
        return `${item?.section || ''}:${(item?.body || '').trim().toLowerCase().replace(/\s+/g, ' ')}`;
    }

    function upsertEntry(entry) {
        if (!entry?.id) return;
        const key = memoryKey(entry);
        const withoutSame = entries.filter((item) => item.id !== entry.id && memoryKey(item) !== key);
        entries = [...withoutSame, entry];
    }

    async function refreshMemoryStats() {
        try {
            const mdResp = await api.getMemoryMarkdown(activeProfileId);
            markdown = mdResp.markdown || '';
            tokenEstimate = mdResp.token_estimate || 0;
            charCount = mdResp.char_count || 0;
            budget = mdResp.budget || 4000;
        } catch (e) {
            // Non-blocking: the primary action already succeeded.
        }
    }

    async function startEdit(entry) {
        editingId = entry.id;
        editBuffer = entry.body;
    }

    async function commitEdit() {
        if (!editingId || !editBuffer.trim()) return;
        try {
            const updated = await api.updateMemoryEntry(editingId, editBuffer.trim(), null, activeProfileId);
            entries = entries.filter((item) => item.id !== editingId);
            upsertEntry(updated);
            editingId = null;
            editBuffer = '';
            await refreshMemoryStats();
        } catch (e) {
            setNotice(e?.message || 'Failed to update entry.');
        }
    }

    function cancelEdit() {
        editingId = null;
        editBuffer = '';
    }

    function startSummaryEdit(summary) {
        editingSummaryId = summary.id;
        summaryEditBuffer = summary.summary_text || '';
    }

    async function commitSummaryEdit() {
        if (!editingSummaryId || !summaryEditBuffer.trim()) return;
        try {
            const updated = await api.updateMiraSessionSummary(editingSummaryId, summaryEditBuffer.trim(), activeProfileId);
            sessionSummaries = sessionSummaries.map((summary) => summary.id === editingSummaryId ? updated : summary);
            editingSummaryId = null;
            summaryEditBuffer = '';
            setNotice('Updated session summary.');
        } catch (e) {
            setNotice(e?.message || 'Failed to update session summary.');
        }
    }

    function cancelSummaryEdit() {
        editingSummaryId = null;
        summaryEditBuffer = '';
    }

    function startIntentEdit(intent) {
        editingIntentId = intent.id;
        intentEditBuffer = intent.target_text || '';
    }

    function cancelIntentEdit() {
        editingIntentId = null;
        intentEditBuffer = '';
    }

    function upsertStatedIntent(intent) {
        if (!intent?.id) return;
        statedIntents = statedIntents.map((item) => item.id === intent.id ? intent : item);
        if (!statedIntents.some((item) => item.id === intent.id)) {
            statedIntents = [intent, ...statedIntents];
        }
    }

    async function commitIntentEdit() {
        if (!editingIntentId || !intentEditBuffer.trim()) return;
        try {
            const result = await api.updateMiraStatedIntent(editingIntentId, { target_text: intentEditBuffer.trim() }, activeProfileId);
            upsertStatedIntent(result?.intent);
            editingIntentId = null;
            intentEditBuffer = '';
            setNotice('Updated money commitment.');
        } catch (e) {
            setNotice(e?.message || 'Failed to update money commitment.');
        }
    }

    async function setIntentStatus(intent, status) {
        if (!intent?.id) return;
        try {
            const result = await api.updateMiraStatedIntent(intent.id, { status }, activeProfileId);
            upsertStatedIntent(result?.intent);
            setNotice(status === 'active' ? 'Resumed money commitment.' : 'Paused money commitment.');
        } catch (e) {
            setNotice(e?.message || 'Failed to update money commitment.');
        }
    }

    async function clearStatedIntent(intent) {
        if (!intent?.id || !confirm('Clear this money commitment?')) return;
        try {
            const result = await api.clearMiraStatedIntent(intent.id, activeProfileId);
            upsertStatedIntent(result?.intent);
            setNotice('Cleared money commitment.');
        } catch (e) {
            setNotice(e?.message || 'Failed to clear money commitment.');
        }
    }

    async function refreshStatedIntent(intent) {
        if (!intent?.id) return;
        try {
            const result = await api.evaluateMiraStatedIntent(intent.id, activeProfileId);
            upsertStatedIntent(result?.intent);
            setNotice('Refreshed money commitment.');
        } catch (e) {
            setNotice(e?.message || 'Failed to refresh money commitment.');
        }
    }

    async function deleteEntry(id) {
        if (!confirm('Remove this entry from memory?')) return;
        try {
            await api.deleteMemoryEntry(id, activeProfileId);
            entries = entries.filter((entry) => entry.id !== id);
            await refreshMemoryStats();
            setNotice('Removed from memory.');
        } catch (e) {
            setNotice(e?.message || 'Failed to delete entry.');
        }
    }

    async function deleteSessionSummary(id) {
        if (!confirm('Remove this session summary from Mira memory?')) return;
        try {
            await api.deleteMiraSessionSummary(id, activeProfileId);
            sessionSummaries = sessionSummaries.filter((summary) => summary.id !== id);
            setNotice('Removed session summary.');
        } catch (e) {
            setNotice(e?.message || 'Failed to delete session summary.');
        }
    }

    async function clearFinancialFeedback(item) {
        if (!item?.id || !confirm('Clear this Mira financial feedback?')) return;
        try {
            await api.clearMiraFinancialFeedback(item.id, activeProfileId);
            financialFeedback = financialFeedback.filter((feedback) => feedback.id !== item.id);
            setNotice('Cleared Mira financial feedback.');
        } catch (e) {
            setNotice(e?.message || 'Failed to clear financial feedback.');
        }
    }

    async function rememberFinancialFeedback(item) {
        if (!item?.id) return;
        try {
            const result = await api.promoteMiraFinancialFeedbackToMemory(item.id, activeProfileId);
            if (result?.entry) {
                upsertEntry(result.entry);
                await refreshMemoryStats();
            }
            setNotice(result?.status === 'already_stored' ? 'Already remembered.' : 'Remembered financial feedback.');
        } catch (e) {
            setNotice(e?.message || 'Failed to remember financial feedback.');
        }
    }

    async function addEntry() {
        if (!newBody.trim()) return;
        try {
            const created = await api.createMemoryEntry({
                section: newSection,
                body: newBody.trim(),
                confidence: 'stated',
                evidence: newEvidence.trim(),
            }, activeProfileId);
            upsertEntry(created);
            newBody = '';
            newEvidence = '';
            await refreshMemoryStats();
            setNotice('Added to memory.');
        } catch (e) {
            setNotice(e?.message || 'Failed to add entry.');
        }
    }

    async function acceptProposal(id) {
        try {
            const result = await api.acceptMemoryProposal(id, null, activeProfileId);
            proposals = proposals.filter((prop) => prop.id !== id);
            upsertEntry(result?.entry);
            await refreshMemoryStats();
            setNotice('Added to memory.');
        } catch (e) {
            setNotice(e?.message || 'Failed to accept proposal.');
        }
    }

    async function rejectProposal(id) {
        try {
            await api.rejectMemoryProposal(id, activeProfileId);
            proposals = proposals.filter((prop) => prop.id !== id);
            setNotice('Ignored suggestion.');
        } catch (e) {
            setNotice(e?.message || 'Failed to reject proposal.');
        }
    }

    async function reviewMyMemory() {
        consolidating = true;
        try {
            const result = await api.consolidateMemory(activeProfileId);
            await refresh();
            const created = result?.proposals_created || 0;
            setNotice(created
                ? `Found ${created} consolidation${created !== 1 ? 's' : ''} to review.`
                : 'Memory looks clean — nothing to consolidate.');
        } catch (e) {
            setNotice(e?.message || 'Consolidation failed.');
        } finally {
            consolidating = false;
        }
    }

    function entriesForSection(key) {
        return entries.filter((e) => e.section === key);
    }

    function sectionLabel(key) {
        const found = sections.find((s) => s.key === key);
        return found ? found.label : key;
    }

    function sourceLabel(source) {
        const labels = {
            agent: 'Suggested by Mira',
            observation_threshold: 'Repeated pattern',
            consolidation: 'Memory review',
            save_to_memory: 'Saved from chat',
        };
        return labels[source] || source?.replaceAll('_', ' ') || 'Suggested';
    }

    function confidenceLabel(confidence) {
        const labels = {
            stated: 'Stated',
            saved: 'Saved',
            inferred: 'Inferred',
        };
        return labels[confidence] || confidence;
    }

    function feedbackTypeLabel(type) {
        const labels = {
            accepted: 'Accepted',
            dismissed: 'Dismissed',
            corrected: 'Corrected',
            snoozed: 'Snoozed',
            too_sensitive: 'Too sensitive',
            more_like_this: 'More like this',
            less_like_this: 'Less like this',
        };
        return labels[type] || type?.replaceAll('_', ' ') || 'Feedback';
    }

    function feedbackEffectLabel(effect) {
        const labels = {
            acknowledge: 'Acknowledge',
            suppress: 'Suppress',
            downrank: 'Down-rank',
            uprank: 'Up-rank',
            reframe: 'Reframe',
            promote_to_memory: 'Memory',
            update_operating_preference: 'Preference',
        };
        return labels[effect] || effect?.replaceAll('_', ' ') || 'Active';
    }

    function feedbackTitle(item) {
        return item?.safe_summary
            || item?.subject_key
            || item?.target_id
            || item?.target_type
            || 'Mira financial feedback';
    }

    function feedbackMeta(item) {
        const parts = [item?.subject_type, item?.target_type, item?.source].filter(Boolean);
        return parts.join(' · ');
    }

    function intentKindLabel(kind) {
        const labels = {
            cut: 'Reduce',
            hold: 'Hold',
            grow: 'Grow',
            monitor: 'Monitor',
            avoid: 'Avoid',
            increase: 'Increase',
        };
        return labels[kind] || kind?.replaceAll('_', ' ') || 'Intent';
    }

    function intentStatusLabel(status) {
        const labels = {
            active: 'Active',
            paused: 'Paused',
            completed: 'Done',
            dismissed: 'Cleared',
        };
        return labels[status] || status;
    }

    function intentEvaluationLabel(intent) {
        return intent?.evaluation?.summary || 'Stored; not evaluated yet.';
    }

    onMount(refresh);

    $: budgetPct = Math.min(100, Math.round((tokenEstimate / budget) * 100));
    $: budgetClass = budgetPct > 90 ? 'memory-budget-bar--over' : (budgetPct > 70 ? 'memory-budget-bar--warn' : '');
    $: activeSections = sections
        .map((section) => ({ ...section, entries: entriesForSection(section.key) }))
        .filter((section) => section.entries.length > 0);
    $: memoryState = tokenEstimate > budget ? 'Over budget' : tokenEstimate > budget * 0.7 ? 'Getting full' : 'Healthy';
    $: financialFeedbackEnabled = Boolean(appConfig.miraFinancialFeedbackLoopEnabled);
    $: statedIntentEnabled = Boolean(appConfig.miraStatedIntentMemoryEnabled);
</script>

<div class="folio-page-shell memory-page">
    <header class="copilot-identity-header fade-in">
        <div class="copilot-profile-row">
            <ProfileSwitcher />
        </div>
        <div class="copilot-brand-lockup">
            <div class="copilot-brand-main">
                <div class="copilot-brand-text">
                    <h1 class="copilot-title">Mira</h1>
                    <p class="copilot-subtitle">Memory keeps approved preferences, goals, and context ready for future conversations.</p>
                    <div class="copilot-identity-meta" aria-label="Mira memory status">
                        <span class="copilot-identity-pill">
                            <span class="material-symbols-outlined">bookmark</span>
                            {entries.length} remembered
                        </span>
                        <span class="copilot-identity-pill">
                            <span class="material-symbols-outlined">rate_review</span>
                            {proposals.length} to review
                        </span>
                        <span class="copilot-identity-pill">
                            <span class="material-symbols-outlined">forum</span>
                            {sessionSummaries.length} session summaries
                        </span>
                        <span class="copilot-identity-pill">
                            <span class="material-symbols-outlined">speed</span>
                            {memoryState}
                        </span>
                    </div>
                </div>
            </div>
            <div class="copilot-island-side">
                <div class="copilot-island-nav" aria-label="Mira sections">
                    <a href="/copilot" class="copilot-island-tab">
                        <span class="material-symbols-outlined">arrow_back</span>
                        Mira
                    </a>
                    <a href="/copilot?view=receipts" class="copilot-island-tab">
                        <span class="material-symbols-outlined">receipt_long</span>
                        Receipts
                    </a>
                    <span class="copilot-island-tab copilot-island-tab-active">
                        <span class="material-symbols-outlined">bookmark</span>
                        Memory
                    </span>
                    <button class="copilot-island-tab" disabled={consolidating} on:click={reviewMyMemory}>
                        <span class="material-symbols-outlined" class:animate-spin={consolidating}>auto_fix_high</span>
                        {consolidating ? 'Reviewing' : 'Review'}
                    </button>
                </div>
            </div>
        </div>
    </header>

    {#if actionNotice}
        <div class="copilot-notice fade-in">{actionNotice}</div>
    {/if}

    <div class="memory-stats-grid fade-in-up">
        <div class="memory-stat">
            <span class="memory-stat-label">Active</span>
            <strong>{entries.length}</strong>
            <span>remembered item{entries.length !== 1 ? 's' : ''}</span>
        </div>
        <div class="memory-stat">
            <span class="memory-stat-label">Review</span>
            <strong>{proposals.length}</strong>
            <span>pending suggestion{proposals.length !== 1 ? 's' : ''}</span>
        </div>
        <div class="memory-stat memory-stat-wide">
            <div class="memory-budget-head">
                <div>
                    <span class="memory-stat-label">Prompt Budget</span>
                    <strong>{memoryState}</strong>
                </div>
                <span>{tokenEstimate.toLocaleString()} / {budget.toLocaleString()} tokens</span>
            </div>
            <div class="memory-budget-track">
                <div class="memory-budget-bar {budgetClass}" style="width: {budgetPct}%"></div>
            </div>
        </div>
    </div>

    <div class="memory-layout fade-in-up">
        <main class="memory-main">
            {#if loading}
                <section class="memory-panel memory-empty">
                    <span class="material-symbols-outlined">progress_activity</span>
                    <p>Loading memory…</p>
                </section>
            {:else if entries.length === 0}
                <section class="memory-panel memory-empty">
                    <span class="material-symbols-outlined">lightbulb</span>
                    <h3>No approved memories yet</h3>
                    <p>Approved preferences and goals will appear here.</p>
                </section>
            {:else}
                {#each activeSections as s (s.key)}
                    <section class="memory-panel">
                        <div class="memory-panel-head">
                            <div>
                                <p class="memory-eyebrow">{s.label}</p>
                                <h3>{s.entries.length} item{s.entries.length !== 1 ? 's' : ''}</h3>
                            </div>
                        </div>
                        <div class="memory-entry-list">
                            {#each s.entries as entry (entry.id)}
                                <article class="memory-entry">
                                    {#if editingId === entry.id}
                                        <textarea bind:value={editBuffer} class="memory-input memory-edit" rows="3" />
                                        <div class="memory-entry-actions">
                                            <button class="memory-action memory-action-primary" on:click={commitEdit}>
                                                <span class="material-symbols-outlined">check</span>
                                                Save
                                            </button>
                                            <button class="memory-action" on:click={cancelEdit}>
                                                <span class="material-symbols-outlined">close</span>
                                                Cancel
                                            </button>
                                        </div>
                                    {:else}
                                        <div class="memory-entry-content">
                                            <div class="memory-entry-body">{entry.body}</div>
                                            <div class="memory-entry-meta">
                                                <span class="memory-tag memory-tag--{entry.confidence}">{confidenceLabel(entry.confidence)}</span>
                                                {#if entry.evidence}<span class="memory-evidence">{entry.evidence}</span>{/if}
                                            </div>
                                        </div>
                                        <div class="memory-entry-actions">
                                            <button class="memory-icon-button" title="Edit memory" on:click={() => startEdit(entry)}>
                                                <span class="material-symbols-outlined">edit</span>
                                            </button>
                                            <button class="memory-icon-button" title="Delete memory" on:click={() => deleteEntry(entry.id)}>
                                                <span class="material-symbols-outlined">delete</span>
                                            </button>
                                        </div>
                                    {/if}
                                </article>
                            {/each}
                        </div>
                    </section>
                {/each}
            {/if}
        </main>

        <aside class="memory-sidebar">
            <section class="memory-panel memory-review-panel">
                <div class="memory-panel-head">
                    <div>
                        <p class="memory-eyebrow">Session Continuity</p>
                        <h3>{sessionSummaries.length ? `${sessionSummaries.length} summar${sessionSummaries.length !== 1 ? 'ies' : 'y'}` : 'No summaries yet'}</h3>
                    </div>
                </div>

                {#if sessionSummaries.length > 0}
                    <div class="memory-proposal-list">
                        {#each sessionSummaries as summary (summary.id)}
                            <article class="memory-proposal">
                                {#if editingSummaryId === summary.id}
                                    <textarea bind:value={summaryEditBuffer} class="memory-input memory-edit" rows="3" />
                                    <div class="memory-entry-actions">
                                        <button class="memory-action memory-action-primary" on:click={commitSummaryEdit}>
                                            <span class="material-symbols-outlined">check</span>
                                            Save
                                        </button>
                                        <button class="memory-action" on:click={cancelSummaryEdit}>
                                            <span class="material-symbols-outlined">close</span>
                                            Cancel
                                        </button>
                                    </div>
                                {:else}
                                    <div class="memory-proposal-meta">
                                        {#each (summary.topics || []).slice(0, 3) as topic}
                                            <span class="memory-tag">{topic}</span>
                                        {/each}
                                        <span class="memory-tag memory-tag--inferred">Summary</span>
                                    </div>
                                    <div class="memory-proposal-body">{summary.summary_text}</div>
                                    {#if summary.unresolved_followups?.length}
                                        <div class="memory-proposal-evidence">Open: {summary.unresolved_followups[0]}</div>
                                    {/if}
                                    <div class="memory-proposal-actions">
                                        <button class="memory-action memory-action-primary" on:click={() => startSummaryEdit(summary)}>
                                            <span class="material-symbols-outlined">edit</span>
                                            Edit
                                        </button>
                                        <button class="memory-action" on:click={() => deleteSessionSummary(summary.id)}>
                                            <span class="material-symbols-outlined">delete</span>
                                            Remove
                                        </button>
                                    </div>
                                {/if}
                            </article>
                        {/each}
                    </div>
                {:else}
                    <div class="memory-quiet-state">
                        <span class="material-symbols-outlined">forum</span>
                        <p>Idle conversation summaries will appear here after Phase 20 is enabled.</p>
                    </div>
                {/if}
            </section>

            <section class="memory-panel memory-review-panel">
                <div class="memory-panel-head">
                    <div>
                        <p class="memory-eyebrow">Review Queue</p>
                        <h3>{proposals.length ? `${proposals.length} suggestion${proposals.length !== 1 ? 's' : ''}` : 'All clear'}</h3>
                    </div>
                </div>

                {#if proposals.length > 0}
                    <div class="memory-proposal-list">
                        {#each proposals as prop (prop.id)}
                            <article class="memory-proposal">
                                <div class="memory-proposal-meta">
                                    <span class="memory-tag">{sectionLabel(prop.section)}</span>
                                    <span class="memory-tag memory-tag--{prop.confidence}">{confidenceLabel(prop.confidence)}</span>
                                </div>
                                <div class="memory-proposal-body">{prop.body}</div>
                                {#if prop.evidence}
                                    <div class="memory-proposal-evidence">{prop.evidence}</div>
                                {/if}
                                <div class="memory-source">{sourceLabel(prop.source)}</div>
                                <div class="memory-proposal-actions">
                                    <button class="memory-action memory-action-primary" on:click={() => acceptProposal(prop.id)}>
                                        <span class="material-symbols-outlined">check</span>
                                        Remember
                                    </button>
                                    <button class="memory-action" on:click={() => rejectProposal(prop.id)}>
                                        <span class="material-symbols-outlined">close</span>
                                        Ignore
                                    </button>
                                </div>
                            </article>
                        {/each}
                    </div>
                {:else}
                    <div class="memory-quiet-state">
                        <span class="material-symbols-outlined">verified</span>
                        <p>No memories waiting for review.</p>
                    </div>
                {/if}
            </section>

            {#if statedIntentEnabled}
                <section class="memory-panel memory-review-panel">
                    <div class="memory-panel-head">
                        <div>
                            <p class="memory-eyebrow">Money Commitments</p>
                            <h3>{statedIntents.length ? `${statedIntents.length} saved` : 'None saved'}</h3>
                        </div>
                    </div>

                    {#if statedIntents.length > 0}
                        <div class="memory-proposal-list">
                            {#each statedIntents as intent (intent.id)}
                                <article class="memory-proposal">
                                    <div class="memory-proposal-meta">
                                        <span class="memory-tag">{intentKindLabel(intent.intent_kind)}</span>
                                        <span class="memory-tag memory-tag--{intent.status === 'active' ? 'stated' : 'inferred'}">{intentStatusLabel(intent.status)}</span>
                                    </div>
                                    {#if editingIntentId === intent.id}
                                        <textarea bind:value={intentEditBuffer} class="memory-input memory-edit" rows="3" />
                                        <div class="memory-entry-actions">
                                            <button class="memory-action memory-action-primary" on:click={commitIntentEdit}>
                                                <span class="material-symbols-outlined">check</span>
                                                Save
                                            </button>
                                            <button class="memory-action" on:click={cancelIntentEdit}>
                                                <span class="material-symbols-outlined">close</span>
                                                Cancel
                                            </button>
                                        </div>
                                    {:else}
                                        <div class="memory-proposal-body">{intent.subject_label || intent.subject_key}</div>
                                        <div class="memory-proposal-evidence">{intent.target_text}</div>
                                        <div class="memory-source">{intentEvaluationLabel(intent)}</div>
                                        <div class="memory-proposal-actions">
                                            <button class="memory-action memory-action-primary" on:click={() => startIntentEdit(intent)}>
                                                <span class="material-symbols-outlined">edit</span>
                                                Edit
                                            </button>
                                            {#if intent.status === 'active'}
                                                <button class="memory-action" on:click={() => setIntentStatus(intent, 'paused')}>
                                                    <span class="material-symbols-outlined">pause</span>
                                                    Pause
                                                </button>
                                            {:else if intent.status === 'paused'}
                                                <button class="memory-action" on:click={() => setIntentStatus(intent, 'active')}>
                                                    <span class="material-symbols-outlined">play_arrow</span>
                                                    Resume
                                                </button>
                                            {/if}
                                            <button class="memory-action" on:click={() => refreshStatedIntent(intent)}>
                                                <span class="material-symbols-outlined">refresh</span>
                                                Refresh
                                            </button>
                                            <button class="memory-action" on:click={() => clearStatedIntent(intent)}>
                                                <span class="material-symbols-outlined">delete</span>
                                                Clear
                                            </button>
                                        </div>
                                    {/if}
                                </article>
                            {/each}
                        </div>
                    {:else}
                        <div class="memory-quiet-state">
                            <span class="material-symbols-outlined">flag</span>
                            <p>Approved money commitments will appear here after Mira can map them to a merchant or category.</p>
                        </div>
                    {/if}
                </section>
            {/if}

            {#if financialFeedbackEnabled}
                <section class="memory-panel memory-review-panel">
                    <div class="memory-panel-head">
                        <div>
                            <p class="memory-eyebrow">Financial Feedback</p>
                            <h3>{financialFeedback.length ? `${financialFeedback.length} active` : 'All clear'}</h3>
                        </div>
                    </div>

                    {#if financialFeedback.length > 0}
                        <div class="memory-proposal-list">
                            {#each financialFeedback as item (item.id)}
                                <article class="memory-proposal">
                                    <div class="memory-proposal-meta">
                                        <span class="memory-tag">{feedbackTypeLabel(item.feedback_type)}</span>
                                        <span class="memory-tag">{feedbackEffectLabel(item.normalized_effect)}</span>
                                    </div>
                                    <div class="memory-proposal-body">{feedbackTitle(item)}</div>
                                    {#if feedbackMeta(item)}
                                        <div class="memory-proposal-evidence">{feedbackMeta(item)}</div>
                                    {/if}
                                    <div class="memory-proposal-actions">
                                        <button class="memory-action memory-action-primary" on:click={() => rememberFinancialFeedback(item)}>
                                            <span class="material-symbols-outlined">bookmark_add</span>
                                            Remember
                                        </button>
                                        <button class="memory-action" on:click={() => clearFinancialFeedback(item)}>
                                            <span class="material-symbols-outlined">delete</span>
                                            Clear
                                        </button>
                                    </div>
                                </article>
                            {/each}
                        </div>
                    {:else}
                        <div class="memory-quiet-state">
                            <span class="material-symbols-outlined">rule_settings</span>
                            <p>No active financial feedback.</p>
                        </div>
                    {/if}
                </section>
            {/if}

            <section class="memory-panel">
                <div class="memory-panel-head">
                    <div>
                        <p class="memory-eyebrow">Add Memory</p>
                        <h3>Write one yourself</h3>
                    </div>
                </div>
                <div class="memory-add-form">
                    <select bind:value={newSection} class="memory-select">
                        {#each sections as s}
                            <option value={s.key}>{s.label}</option>
                        {/each}
                    </select>
                    <textarea bind:value={newBody} placeholder="Trying to keep dining out under $300/mo" class="memory-input" rows="3" />
                    <input bind:value={newEvidence} placeholder="Optional evidence or quote" class="memory-input" />
                    <button class="memory-action memory-action-primary memory-full-button" on:click={addEntry} disabled={!newBody.trim()}>
                        <span class="material-symbols-outlined">add</span>
                        Add memory
                    </button>
                </div>
            </section>

            {#if markdown}
                <section class="memory-panel">
                    <details>
                        <summary class="memory-advanced-summary">
                            <span class="material-symbols-outlined">description</span>
                            Raw markdown
                        </summary>
                        <pre class="memory-markdown">{markdown}</pre>
                    </details>
                </section>
            {/if}
        </aside>
    </div>
</div>

<style>
    .memory-page {
        display: flex;
        flex-direction: column;
        gap: 16px;
    }

    .memory-eyebrow {
        margin: 0 0 3px;
        color: var(--text-muted);
        font-size: 10px;
        font-weight: 780;
        letter-spacing: 0.12em;
        text-transform: uppercase;
    }

    .memory-stats-grid {
        display: grid;
        grid-template-columns: minmax(140px, 0.35fr) minmax(140px, 0.35fr) minmax(260px, 1fr);
        gap: 10px;
    }

    .memory-stat,
    .memory-panel {
        border: 1px solid var(--card-border);
        border-radius: 12px;
        background: var(--card-bg, transparent);
        box-shadow: 0 1px 2px rgba(15, 23, 42, 0.03);
    }

    .memory-stat {
        padding: 12px 14px;
        min-height: 76px;
        display: flex;
        flex-direction: column;
        justify-content: center;
        gap: 2px;
    }

    .memory-stat strong {
        color: var(--text-primary);
        font-size: 22px;
        font-weight: 720;
        line-height: 1.1;
    }

    .memory-stat span:last-child,
    .memory-budget-head span {
        color: var(--text-secondary);
        font-size: 12px;
    }

    .memory-stat-label {
        color: var(--text-muted);
        font-size: 10px;
        font-weight: 760;
        letter-spacing: 0.1em;
        text-transform: uppercase;
    }

    .memory-stat-wide {
        gap: 9px;
    }

    .memory-budget-head {
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: 12px;
    }

    .memory-layout {
        display: grid;
        grid-template-columns: minmax(0, 1fr) minmax(320px, 380px);
        gap: 14px;
        align-items: start;
    }

    .memory-main,
    .memory-sidebar {
        display: flex;
        flex-direction: column;
        gap: 14px;
    }

    .memory-sidebar {
        position: sticky;
        top: 16px;
    }

    .memory-panel {
        padding: 16px;
    }

    .memory-panel-head {
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        gap: 12px;
        margin-bottom: 12px;
    }

    .memory-panel-head h3 {
        margin: 0;
        color: var(--text-primary);
        font-size: 15px;
        font-weight: 720;
    }

    .memory-empty {
        min-height: 180px;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        text-align: center;
        color: var(--text-secondary);
        gap: 8px;
    }

    .memory-empty .material-symbols-outlined,
    .memory-quiet-state .material-symbols-outlined {
        color: var(--accent);
        font-size: 24px;
    }

    .memory-empty h3,
    .memory-empty p,
    .memory-quiet-state p {
        margin: 0;
    }

    .memory-budget-track {
        height: 6px;
        background: rgba(0,0,0,0.06);
        border-radius: 3px;
        overflow: hidden;
    }
    .memory-budget-bar {
        height: 100%;
        background: var(--accent, #5b8def);
        transition: width 240ms ease;
    }
    .memory-budget-bar--warn { background: #d29849; }
    .memory-budget-bar--over { background: #c1554b; }

    .memory-entry-list,
    .memory-proposal-list {
        display: flex;
        flex-direction: column;
        gap: 8px;
    }

    .memory-entry {
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        gap: 12px;
        padding: 12px;
        border: 1px solid color-mix(in srgb, var(--card-border) 72%, transparent);
        border-radius: 10px;
        background: color-mix(in srgb, var(--card-bg) 84%, var(--surface-100));
    }

    .memory-entry-content {
        min-width: 0;
    }

    .memory-entry-body {
        font-size: 14px;
        color: var(--text-primary);
        line-height: 1.4;
    }

    .memory-entry-meta {
        display: flex;
        gap: 8px;
        align-items: center;
        margin-top: 6px;
        flex-wrap: wrap;
    }

    .memory-entry-actions {
        display: flex;
        gap: 6px;
        flex: 0 0 auto;
    }

    .memory-tag {
        display: inline-flex;
        align-items: center;
        font-size: 10px;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        padding: 2px 6px;
        border-radius: 6px;
        background: rgba(0,0,0,0.06);
        color: var(--text-secondary);
        line-height: 1.5;
    }
    .memory-tag--stated { background: rgba(59, 130, 246, 0.10); color: #3b82f6; }
    .memory-tag--inferred { background: rgba(210, 152, 73, 0.15); color: #b07a2c; }
    .memory-tag--saved { background: rgba(91, 141, 239, 0.15); color: #3b6bc4; }
    .memory-evidence {
        min-width: 0;
        color: var(--text-secondary);
        font-size: 12px;
        font-style: italic;
    }

    .memory-add-form {
        display: flex;
        flex-direction: column;
        gap: 8px;
    }

    .memory-select,
    .memory-input {
        width: 100%;
        padding: 9px 10px;
        border: 1px solid var(--card-border);
        border-radius: 8px;
        background: var(--surface-50, transparent);
        color: var(--text-primary);
        font-size: 13px;
    }

    .memory-input:focus,
    .memory-select:focus {
        outline: none;
        border-color: color-mix(in srgb, var(--accent) 45%, var(--card-border));
        box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 12%, transparent);
    }

    .memory-edit {
        flex: 1 1 auto;
    }

    .memory-action,
    .memory-icon-button {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        gap: 5px;
        border: 1px solid var(--card-border);
        border-radius: 999px;
        background: var(--card-bg);
        color: var(--text-secondary);
        font-size: 12px;
        font-weight: 650;
        cursor: pointer;
        transition: transform 0.15s ease, border-color 0.15s ease, background 0.15s ease;
    }

    .memory-action {
        padding: 7px 11px;
    }

    .memory-icon-button {
        width: 30px;
        height: 30px;
        padding: 0;
    }

    .memory-action:hover:not(:disabled),
    .memory-icon-button:hover {
        transform: translateY(-1px);
        border-color: color-mix(in srgb, var(--accent) 36%, var(--card-border));
        color: var(--text-primary);
    }

    .memory-action:disabled {
        opacity: 0.45;
        cursor: not-allowed;
    }

    .memory-action .material-symbols-outlined,
    .memory-icon-button .material-symbols-outlined {
        font-size: 14px;
    }

    .memory-action-primary {
        border-color: color-mix(in srgb, var(--accent) 35%, transparent);
        background: var(--accent);
        color: #fff;
    }

    .memory-action-primary:hover:not(:disabled) {
        color: #fff;
        background: color-mix(in srgb, var(--accent) 88%, #111827);
    }

    .memory-full-button {
        width: 100%;
    }

    .memory-proposal {
        padding: 12px;
        border: 1px solid color-mix(in srgb, var(--accent) 18%, var(--card-border));
        border-radius: 10px;
        background: color-mix(in srgb, var(--accent) 4%, var(--card-bg));
    }

    .memory-proposal-meta {
        display: flex;
        gap: 6px;
        align-items: center;
        margin-bottom: 8px;
        flex-wrap: wrap;
    }

    .memory-proposal-body {
        color: var(--text-primary);
        font-size: 14px;
        line-height: 1.4;
    }

    .memory-proposal-evidence {
        color: var(--text-secondary);
        font-size: 12px;
        font-style: italic;
        margin-top: 5px;
    }

    .memory-proposal-actions {
        display: flex;
        gap: 7px;
        margin-top: 10px;
        flex-wrap: wrap;
    }

    .memory-source {
        color: var(--text-muted);
        font-size: 11px;
        margin-top: 7px;
    }

    .memory-quiet-state {
        min-height: 120px;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        gap: 8px;
        text-align: center;
        color: var(--text-secondary);
        border: 1px dashed var(--card-border);
        border-radius: 10px;
    }

    .memory-advanced-summary {
        display: flex;
        align-items: center;
        gap: 7px;
        color: var(--text-secondary);
        font-size: 12px;
        font-weight: 700;
        cursor: pointer;
        list-style: none;
    }

    .memory-advanced-summary::-webkit-details-marker {
        display: none;
    }

    .memory-advanced-summary .material-symbols-outlined {
        font-size: 15px;
    }

    .memory-markdown {
        margin-top: 0.75rem;
        padding: 12px;
        background: rgba(0,0,0,0.03);
        border-radius: 8px;
        font-family: ui-monospace, monospace;
        font-size: 12px;
        white-space: pre-wrap;
        word-break: break-word;
    }

    @media (max-width: 980px) {
        .memory-layout,
        .memory-stats-grid {
            grid-template-columns: 1fr;
        }

        .memory-sidebar {
            position: static;
        }
    }

    @media (max-width: 640px) {
        .memory-entry {
            flex-direction: column;
        }

        .memory-entry-actions {
            width: 100%;
        }
    }
</style>
