<script>
    import '$lib/styles/copilot.css';
    import { goto } from '$app/navigation';
    import { page } from '$app/stores';
    import { onDestroy, onMount, tick } from 'svelte';
    import { api, invalidateCache } from '$lib/api.js';
    import { activeProfile, profiles } from '$lib/stores/profileStore.js';
    import { formatCurrency, formatDate } from '$lib/utils.js';
    import ProfileSwitcher from '$lib/components/ProfileSwitcher.svelte';
    import CopilotChart from '$lib/components/CopilotChart.svelte';

    let loading = false;
    let sidebarLoading = true;
    let actionNotice = '';
    let lastLoadedProfile = undefined;
    let input = '';
    let chatContainer;
    let showSqlForMsg = {};
    let showSqlForHistory = {};
    let historyOpen = false;
    let cancelStream = null;  // holds the in-flight stream's cancel fn

    let recurringData = null;
    let historyItems = [];
    let localLlmStatus = null;
    let localLlmCatalog = null;
    let copilotModel = '';
    let copilotModelSaving = false;
    let copilotModelInstalling = false;
    let modelDropdownOpen = false;
    let appConfig = {
        demoMode: false,
        receiptIntelligenceEnabled: false,
        miraAgenticRuntime: 'current',
        miraDemoPreviewOnly: false,
        miraAdvisorReadUiEnabled: false,
        miraAdvisorReadContextEnabled: false,
        miraAdvisorReadGenerationEnabled: false,
        miraFinancialFeedbackLoopEnabled: false,
    };
    let advisorRead = null;
    let advisorReadEnabled = false;
    let advisorReadContextEnabled = false;
    let advisorReadGenerationEnabled = false;
    let advisorReadJob = { status: 'idle' };
    let advisorReadLoading = false;
    let advisorReadGenerating = false;
    let advisorReadExpanded = false;
    let advisorReadPanelExpanded = true;
    let advisorReadSelectedCardId = '';
    let advisorReadPollTimer = null;
    let advisorReadThreadActive = false;
    let advisorReadThreadMemoId = null;
    let advisorCorrectionCardId = null;
    let advisorCorrectionText = '';

    let receiptsOpen = false;
    let receiptFile = null;
    let receiptParsing = false;
    let receiptSaving = false;
    let receiptError = '';
    let receiptDraft = null;
    let receiptDrafts = [];
    let receiptItems = [];
    let receiptComparisons = [];
    let receiptSummaryEditing = false;
    let receiptParseStartedAt = null;
    let receiptParseElapsed = 0;
    let receiptParseTimer = null;
    const receiptReadyStorageKey = 'folio:receipt-ready';

    $: receiptParseStage = (() => {
        if (!receiptParsing) return '';
        if (receiptParseElapsed < 3) return 'Uploading receipt image';
        if (receiptParseElapsed < 7) return receiptFile?.name?.toLowerCase().endsWith('.heic') || receiptFile?.name?.toLowerCase().endsWith('.heif')
            ? 'Converting HEIC photo'
            : 'Preparing image';
        if (receiptParseElapsed < 14) return 'Reading receipt text with local vision';
        return 'Extracting item names and prices';
    })();

    function startReceiptParseTimer() {
        clearReceiptParseTimer();
        receiptParseStartedAt = Date.now();
        receiptParseElapsed = 0;
        receiptParseTimer = setInterval(() => {
            receiptParseElapsed = Math.max(0, Math.floor((Date.now() - receiptParseStartedAt) / 1000));
        }, 250);
    }

    function clearReceiptParseTimer() {
        if (receiptParseTimer) clearInterval(receiptParseTimer);
        receiptParseTimer = null;
    }

    // Strip <observation>/<memory_proposal> tags from streamed text so they never
    // briefly appear in the UI before the server-side cleanup at done. Also reverses
    // a known model failure mode where it emits literal '/n' instead of a newline.
    function scrubMemoryTags(text) {
        if (!text) return text;
        let out = text.replace(/<observation\b[^>]*>[\s\S]*?<\/observation>/gi, '');
        out = out.replace(/<memory_proposal\b[^>]*>[\s\S]*?<\/memory_proposal>/gi, '');
        out = out.replace(/<observation\b[^>]*\/>/gi, '');
        out = out.replace(/<memory_proposal\b[^>]*\/>/gi, '');
        out = out.replace(/\*?\(?\s*Self-Correction\s*:[\s\S]*?(?:\)\*?|\n\n|$)/gi, '');
        // Mid-stream: hide the open tag onward until close arrives, so we don't flicker XML
        const openIdx = out.search(/<(observation|memory_proposal)\b/i);
        if (openIdx >= 0) out = out.slice(0, openIdx);
        // Convert literal '/n' (model misfire) → real newline, then collapse runs
        out = out.replace(/\/n/g, '\n').replace(/\n{3,}/g, '\n\n');
        return out;
    }

    function combinePreviewAndFinalAnswer(preview, finalAnswer, answerGuard = null) {
        const previewText = scrubMemoryTags(preview || '').trim();
        const finalText = scrubMemoryTags(finalAnswer || '').trim();
        if (!previewText) return finalText;
        if (answerGuard?.preview_only) return previewText;
        if (!finalText) return previewText;
        const normalize = (value) => String(value || '').toLowerCase().replace(/\s+/g, ' ').trim();
        const previewNorm = normalize(previewText);
        const finalNorm = normalize(finalText);
        if (previewNorm === finalNorm || finalNorm.includes(previewNorm) || previewNorm.includes(finalNorm)) {
            return finalText.length >= previewText.length ? finalText : previewText;
        }
        return `${previewText}\n\n${finalText}`;
    }

    function profileGreetingName(profileId) {
        const raw = String(profileId || '').trim();
        if (!raw || raw === 'household') return 'you';
        const profile = ($profiles || []).find((item) => item?.id === raw);
        const display = String(profile?.name || raw).trim();
        return display.charAt(0).toUpperCase() + display.slice(1);
    }

    function miraWelcome(profileId) {
        const name = profileGreetingName(profileId);
        return `Hey ${name}. There you are.\n\nWhere do you want to start?`;
    }

    function advisorTableCells(line) {
        return String(line || '')
            .trim()
            .replace(/^\|/, '')
            .replace(/\|$/, '')
            .split('|')
            .map((cell) => cell.trim());
    }

    function advisorTableDivider(cells) {
        return cells.length > 0 && cells.every((cell) => /^:?-{3,}:?$/.test(String(cell || '').replace(/\s/g, '')));
    }

    function advisorMemoHeadingText(line) {
        const heading = String(line || '').replace(/^#+\s*/, '');
        if (['Where The Money Is Going', 'Where Money Is Going', 'Where Money Goes', 'Where Your Money Actually Goes'].includes(heading)) {
            return 'The Money Map';
        }
        return heading;
    }

    function advisorMemoBlocks(markdown) {
        const lines = String(markdown || '')
            .split(/\n+/)
            .map((line) => line.trim())
            .filter(Boolean);
        const blocks = [];
        for (let index = 0; index < lines.length; index += 1) {
            const line = lines[index];
            if (line.startsWith('|') && line.endsWith('|')) {
                const tableLines = [];
                while (index < lines.length && lines[index].startsWith('|') && lines[index].endsWith('|')) {
                    tableLines.push(lines[index]);
                    index += 1;
                }
                index -= 1;
                const tableRows = tableLines.map(advisorTableCells).filter((row) => row.some(Boolean));
                const headers = tableRows[0] || [];
                const rows = tableRows.slice(1).filter((row) => !advisorTableDivider(row));
                if (headers.length && rows.length) {
                    blocks.push({
                        type: 'table',
                        headers,
                        rows: rows.map((row) => headers.map((_, cellIndex) => row[cellIndex] || ''))
                    });
                    continue;
                }
                tableLines.forEach((rawLine) => blocks.push({ type: 'paragraph', text: rawLine }));
                continue;
            }
            if (line.startsWith('## ')) blocks.push({ type: 'heading', text: advisorMemoHeadingText(line) });
            else if (line.startsWith('# ')) blocks.push({ type: 'heading', text: advisorMemoHeadingText(line) });
            else if (line.startsWith('- ')) blocks.push({ type: 'bullet', text: line.slice(2).trim() });
            else blocks.push({ type: 'paragraph', text: line.replace(/^#+\s*/, '') });
        }
        return blocks.filter((block) => block.type === 'table' || block.text);
    }

    function advisorDeltaDetail(delta) {
        if (!delta) return '';
        const months = Array.isArray(delta.touched_months) ? delta.touched_months.filter(Boolean).join(', ') : '';
        const sections = Array.isArray(delta.changed_sections) ? delta.changed_sections.filter(Boolean).slice(0, 2).join(', ') : '';
        if (months && sections) return `${months} · ${sections}`;
        if (months) return months;
        if (sections) return sections;
        return delta.action || 'Stored update available.';
    }

    function advisorCardRows(card) {
        return Array.isArray(card?.rows) ? card.rows.filter((row) => row?.label) : [];
    }

    const advisorPriorityCardIds = ['first_move', 'event_noise', 'biggest_risk'];

    function advisorCardById(cards, id) {
        return (cards || []).find((card) => card?.id === id) || null;
    }

    function advisorPriorityCards(cards) {
        const chosen = advisorPriorityCardIds
            .map((id) => advisorCardById(cards, id))
            .filter(Boolean);
        if (chosen.length >= 3) return chosen.slice(0, 3);
        const used = new Set(chosen.map((card) => card.id));
        const fallback = (cards || []).filter((card) => (
            card?.id
            && !used.has(card.id)
            && !['normal_month', 'money_map', 'what_changed'].includes(card.id)
        ));
        return [...chosen, ...fallback].slice(0, 3);
    }

    function advisorSummaryText(cards, blocks) {
        const firstMove = advisorCardById(cards, 'first_move');
        const risk = advisorCardById(cards, 'biggest_risk');
        const eventNoise = advisorCardById(cards, 'event_noise');
        return (
            firstMove?.summary
            || risk?.summary
            || eventNoise?.summary
            || blocks?.find((block) => block.type === 'paragraph')?.text
            || 'Mira has a stored read on what matters most in your finances right now.'
        );
    }

    function advisorPreviewRows(card, limit = 3) {
        return advisorCardRows(card).slice(0, limit);
    }

    function advisorMoneyMapIcon(label) {
        const value = String(label || '').toLowerCase();
        if (value.includes('shop')) return 'shopping_bag';
        if (value.includes('travel') || value.includes('flight')) return 'flight';
        if (value.includes('tax')) return 'account_balance';
        if (value.includes('subscription') || value.includes('recurring')) return 'credit_card';
        if (value.includes('food') || value.includes('restaurant')) return 'restaurant';
        if (value.includes('home') || value.includes('rent')) return 'home';
        return 'category';
    }

    let messages = [
        {
            role: 'assistant',
            content: miraWelcome($activeProfile || 'household'),
            operation: null,
            data: null,
            sql: null,
            preview_changes: [],
            needs_confirmation: false,
            rows_affected: 0,
            rows_total: 0,
            is_welcome: true
        }
    ];

    // ── Chip action descriptors ──
    // Each chip describes a structured operation with its required inputs.
    // Chips with no inputs execute immediately; others show an inline mini-form.
    const starterChips = [
        {
            id: 'start_unknown',
            label: "I don't know where to start",
            prompt: "What should I watch in my finances this month?",
        },
        {
            id: 'where_money_went',
            label: 'Show me where my money went',
            prompt: 'Show my top spending categories this month.',
        },
        {
            id: 'fix_first',
            label: 'What should I fix first?',
            prompt: 'What should I fix first in my finances?',
        },
        {
            id: 'spend_this_week',
            label: 'Can I spend more this week?',
            prompt: 'Can I spend more this week?',
        },
        {
            id: 'subscriptions',
            label: 'Clean up subscriptions',
            prompt: 'Show all active recurring charges.',
        },
        {
            id: 'budget',
            label: 'Check my budget',
            prompt: 'How is my budget looking this month?',
        },
    ];

    const chipActions = [
        {
            id: 'explain_category',
            label: 'Explain why a merchant is categorized',
            inputs: [
                { key: 'merchant', label: 'Merchant name', type: 'text', placeholder: 'e.g. DoorDash', required: true },
            ],
        },
        {
            id: 'find_missing_categories',
            label: 'Find merchants missing categories',
            inputs: [],
        },
        {
            id: 'bulk_recategorize',
            label: 'Move a merchant\'s transactions to a category',
            inputs: [
                { key: 'merchant', label: 'Merchant', type: 'text', placeholder: 'e.g. Netflix', required: true },
                { key: 'category', label: 'New category', type: 'select', required: true },
            ],
        },
        {
            id: 'create_rule',
            label: 'Create a category rule',
            inputs: [
                { key: 'pattern', label: 'Merchant pattern', type: 'text', placeholder: 'e.g. CLAUDE PRO', required: true },
                { key: 'category', label: 'Category', type: 'select', required: true },
            ],
        },
        {
            id: 'rename_merchant',
            label: 'Rename a merchant',
            inputs: [
                { key: 'old_name', label: 'Current name', type: 'text', placeholder: 'e.g. AMZN MKTPLACE PMTS', required: true },
                { key: 'new_name', label: 'New display name', type: 'text', placeholder: 'e.g. Amazon Marketplace', required: true },
            ],
        },
        {
            id: 'receipt_compare',
            label: 'Compare grocery receipt prices',
            inputs: [],
            requiresReceipts: true,
        },
    ];

    // Chip form state
    let activeChip = null;       // id of the currently expanded chip, or null
    let chipFormValues = {};     // { fieldKey: value } for the active form
    let categories = [];         // loaded on mount for dropdown inputs

    $: activeProfileId = $activeProfile || 'household';
    $: scopedProfile = activeProfileId !== 'household' ? activeProfileId : null;
    $: unreadEvents = recurringData?.events?.filter((event) => !event.is_read) || [];
    $: recentHistory = historyItems.slice(0, 6);
    $: copilotModelOptions = Array.isArray(localLlmCatalog?.tiers)
        ? localLlmCatalog.tiers.flatMap((tier) => tier.models.filter((model) => model.task_fit?.includes('copilot')))
        : [];
    $: selectedCopilotModelMeta = copilotModelOptions.find((model) => model.id === copilotModel) || null;
    $: copilotModelLabel = selectedCopilotModelMeta
        ? `${selectedCopilotModelMeta.label}${selectedCopilotModelMeta.download_size_gb || selectedCopilotModelMeta.approx_size_gb ? ` · ${selectedCopilotModelMeta.download_size_gb || selectedCopilotModelMeta.approx_size_gb} GB` : ''}`
        : (copilotModel || 'No model selected');
    $: showMiraDebug = ['1', 'true', 'mira', 'all'].includes(($page.url.searchParams.get('debugMira') || '').toLowerCase());
    $: advisorReadVisible = advisorReadEnabled || appConfig.miraAdvisorReadUiEnabled;
    $: advisorReadFollowupEnabled = advisorReadContextEnabled || appConfig.miraAdvisorReadContextEnabled;
    $: advisorReadCanGenerate = advisorReadGenerationEnabled || appConfig.miraAdvisorReadGenerationEnabled;
    $: miraDemoPreviewOnly = Boolean(appConfig.miraDemoPreviewOnly);
    $: advisorReadJobRunning = ['queued', 'running'].includes(advisorReadJob?.status);
    $: advisorReadTitle = advisorRead
        ? 'Your financial portrait'
        : (advisorReadJobRunning || advisorReadGenerating ? 'Preparing your financial read' : 'Ready to generate your financial read');
    $: advisorReadBlocks = advisorMemoBlocks(advisorRead?.memo_markdown);
    $: advisorReadCards = Array.isArray(advisorRead?.cards)
        ? advisorRead.cards.filter((card) => card && (card.summary || (Array.isArray(card.rows) && card.rows.length > 0)))
        : [];
    $: advisorReadHasCards = advisorReadCards.length > 0;
    $: advisorReadPriorityCards = advisorPriorityCards(advisorReadCards);
    $: advisorReadMoneyMapCard = advisorCardById(advisorReadCards, 'money_map');
    $: advisorReadMoneyMapRows = advisorPreviewRows(advisorReadMoneyMapCard, 4);
    $: advisorReadSelectedCard = advisorCardById(advisorReadCards, advisorReadSelectedCardId);
    $: advisorReadSelectedRows = advisorCardRows(advisorReadSelectedCard);
    $: advisorReadSummary = advisorSummaryText(advisorReadCards, advisorReadBlocks);
    $: visibleAdvisorReadBlocks = advisorReadExpanded ? advisorReadBlocks : advisorReadBlocks.slice(0, 5);
    $: advisorReadPreparedLabel = advisorRead?.generated_at ? `Prepared ${formatDate(advisorRead.generated_at)}` : 'Stored local read';
    $: advisorReadDelta = advisorRead?.delta || null;
    $: advisorReadDeltaHeadline = advisorReadDelta?.headline || 'This read is current';
    $: advisorReadDeltaDetail = advisorReadDelta ? advisorDeltaDetail(advisorReadDelta) : 'No stored fact changes since this read.';
    $: if (messages.length === 1 && messages[0]?.is_welcome) {
        const nextWelcome = miraWelcome(activeProfileId);
        if (messages[0].content !== nextWelcome) {
            messages = [{ ...messages[0], content: nextWelcome }];
        }
    }

    onMount(async () => {
        const prompt = $page.url.searchParams.get('prompt');
        if (prompt) input = prompt;
        await loadAppConfig();
        await Promise.all([refreshSidebar(), miraDemoPreviewOnly ? Promise.resolve() : loadLocalLlm()]);
        lastLoadedProfile = activeProfileId;
        await restoreReceiptFromNavigation();
        // Load categories for chip form dropdowns
        try {
            const catRes = await api.getCategories();
            categories = (catRes?.categories ?? catRes ?? []).filter(c => c.is_active !== 0);
        } catch (e) { categories = []; }
    });

    onDestroy(() => {
        if (advisorReadPollTimer) clearTimeout(advisorReadPollTimer);
    });

    $: if (activeProfileId && lastLoadedProfile !== undefined && activeProfileId !== lastLoadedProfile) {
        lastLoadedProfile = activeProfileId;
        advisorReadThreadActive = false;
        advisorReadThreadMemoId = null;
        refreshSidebar();
        if (receiptsOpen && appConfig.receiptIntelligenceEnabled) {
            receiptDraft = null;
            receiptItems = [];
            receiptFile = null;
            loadReceiptWorkspace();
        }
    }

    function setNotice(message) {
        actionNotice = message;
        setTimeout(() => {
            if (actionNotice === message) actionNotice = '';
        }, 3000);
    }

    function openHistory() {
        historyOpen = true;
    }

    function closeHistory() {
        historyOpen = false;
    }

    async function clearHistory() {
        if (sidebarLoading) return;
        const ok = window.confirm('Clear all recent Mira activity for this view? This only removes the activity log; it does not delete transactions or memory.');
        if (!ok) return;
        sidebarLoading = true;
        try {
            await api.clearCopilotHistory(activeProfileId);
            historyItems = [];
            showSqlForHistory = {};
        } catch (error) {
            setNotice(error?.message || 'Failed to clear Mira history.');
        } finally {
            sidebarLoading = false;
        }
    }

    async function deleteHistoryItem(item) {
        if (!item?.id) return;
        const ok = window.confirm('Remove this Mira activity item? This only removes it from history.');
        if (!ok) return;
        try {
            await api.deleteCopilotHistoryItem(item.id, activeProfileId);
            historyItems = historyItems.filter((entry) => entry.id !== item.id);
            const nextSqlState = { ...showSqlForHistory };
            delete nextSqlState[item.id];
            showSqlForHistory = nextSqlState;
        } catch (error) {
            setNotice(error?.message || 'Failed to remove Mira history item.');
        }
    }

    function formatDateTime(value) {
        if (!value) return '—';
        const normalized = String(value).includes('T') ? value : String(value).replace(' ', 'T');
        const dt = new Date(normalized);
        if (Number.isNaN(dt.getTime())) return String(value);
        return dt.toLocaleString([], {
            month: 'short',
            day: 'numeric',
            year: 'numeric',
            hour: 'numeric',
            minute: '2-digit'
        });
    }

    function formatTableValue(key, value) {
        if (value === null || value === undefined || value === '') return '—';
        if (typeof value === 'boolean') return value ? 'Yes' : 'No';
        if (typeof value === 'number') {
            const currencyKeys = ['amount', 'total', 'balance', 'sum', 'avg', 'spent', 'income', 'expense', 'net', 'owed', 'assets', 'budget'];
            currencyKeys.push('remaining', 'monthly', 'annual', 'safe_to_spend', 'cash', 'flow');
            if (currencyKeys.some((token) => key.toLowerCase().includes(token))) return formatCurrency(value, 2);
            return Number.isInteger(value) ? value.toString() : value.toFixed(2);
        }
        if (typeof value === 'string' && /^\d{4}-\d{2}-\d{2}/.test(value)) {
            if (key.toLowerCase().includes('created') || key.toLowerCase().includes('updated') || key.toLowerCase().includes('synced')) {
                return formatDateTime(value);
            }
            return formatDate(value);
        }
        return String(value);
    }

    function getColumns(data) {
        if (!data || data.length === 0) return [];
        const columns = [];
        for (const row of data) {
            if (!row || typeof row !== 'object' || Array.isArray(row)) continue;
            for (const key of Object.keys(row)) {
                if (!columns.includes(key)) columns.push(key);
            }
        }
        return columns;
    }

    const RECEIPT_INTERNAL_KEYS = new Set([
        'step_id', 'tool', 'tool_name', 'execution_tool', 'execution_tool_name',
        'metric_id', 'metric_definition_summary', 'calculation_basis',
        'source_step_id', 'selector_call_id', 'raw_sql', 'sql'
    ]);

    const RECEIPT_LABELS = {
        merchant_query: 'Merchant',
        merchant: 'Merchant',
        merchant_name: 'Merchant',
        category: 'Category',
        range: 'Period',
        month: 'Month',
        start: 'Start',
        end: 'End',
        total: 'Total',
        amount: 'Amount',
        balance: 'Balance',
        income: 'Income',
        expenses: 'Expenses',
        net: 'Net',
        net_flow: 'Net flow',
        count: 'Count',
        txn_count: 'Transactions',
        row_count: 'Rows',
        total_matching_transactions: 'Matches',
        confidence: 'Confidence'
    };

    function compactReceiptLabel(key) {
        return RECEIPT_LABELS[key] || key.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
    }

    function receiptValue(key, value) {
        if (value === null || value === undefined || value === '') return '';
        if (Array.isArray(value) || (typeof value === 'object' && value !== null)) return '';
        if (typeof value === 'number') {
            const k = key.toLowerCase();
            if (/(amount|total|balance|income|expenses|expense|net|flow|budget|remaining|spent|owed)/.test(k)) {
                return formatCurrency(value, 2);
            }
            return Number.isInteger(value) ? String(value) : value.toFixed(2);
        }
        if (typeof value === 'string' && /^\d{4}-\d{2}-\d{2}/.test(value)) return formatDate(value);
        return String(value);
    }

    function addReceiptLine(lines, line) {
        const clean = String(line || '').trim();
        if (!clean || lines.includes(clean)) return;
        lines.push(clean);
    }

    function firstEvidenceFact(msg) {
        const facts = Array.isArray(msg?.evidence?.facts) ? msg.evidence.facts : [];
        return facts.find(f => f && typeof f === 'object') || null;
    }

    function evidenceCaveats(msg) {
        const fact = firstEvidenceFact(msg) || {};
        const caveats = [];
        const direct = Array.isArray(msg?.evidence?.caveats) ? msg.evidence.caveats : [];
        const factCaveats = Array.isArray(fact.caveats) ? fact.caveats : [];
        const confidenceCaveat = fact?.confidence_summary?.caveat;
        for (const caveat of [...direct, ...factCaveats, confidenceCaveat]) {
            const clean = String(caveat || '').trim();
            if (clean && !caveats.includes(clean)) caveats.push(clean);
        }
        return caveats;
    }

    function chatReceipt(msg) {
        if (!msg || msg.role !== 'assistant' || msg.operation === 'streaming' || msg.operation === 'error') return null;
        if (msg.needs_confirmation) return null;

        const evidence = msg.evidence && typeof msg.evidence === 'object' ? msg.evidence : null;
        const fact = firstEvidenceFact(msg);
        const hasGrounding =
            evidence ||
            (Array.isArray(msg.data) && msg.data.length > 0) ||
            (msg.chart && Array.isArray(msg.chart.labels) && msg.chart.labels.length > 0);
        if (!hasGrounding) return null;

        const lines = [];
        const record = fact || (Array.isArray(msg.data) && msg.data.length > 0 ? msg.data[0] : {});
        const subject = record.merchant_query || record.merchant || record.merchant_name || record.category || record.summary;
        const range = record.range || record.month || (record.start && record.end ? `${record.start} to ${record.end}` : '');
        if (subject || range) {
            addReceiptLine(lines, [subject, range].filter(Boolean).map(String).join(' · '));
        }

        const totalRows = Number(msg.rows_total || evidence?.display_row_count || evidence?.row_count || 0);
        const shownRows = Array.isArray(msg.data) ? msg.data.length : 0;
        if (totalRows > 0 && shownRows > 0 && totalRows > shownRows) {
            addReceiptLine(lines, `Showing ${shownRows} of ${totalRows} matching rows.`);
        } else if (totalRows > 0) {
            addReceiptLine(lines, `Counted ${totalRows} matching row${totalRows === 1 ? '' : 's'}.`);
        } else if (shownRows > 0) {
            addReceiptLine(lines, `Showing ${shownRows} row${shownRows === 1 ? '' : 's'} from Folio data.`);
        }

        for (const key of ['total', 'amount', 'balance', 'income', 'expenses', 'net', 'net_flow', 'count', 'txn_count', 'total_matching_transactions', 'confidence']) {
            if (!record || !(key in record) || RECEIPT_INTERNAL_KEYS.has(key)) continue;
            const value = receiptValue(key, record[key]);
            if (value) addReceiptLine(lines, `${compactReceiptLabel(key)}: ${value}`);
            if (lines.length >= 5) break;
        }

        if (msg.chart && Array.isArray(msg.chart.labels) && msg.chart.labels.length > 0) {
            addReceiptLine(lines, `Chart uses ${msg.chart.labels.length} plotted point${msg.chart.labels.length === 1 ? '' : 's'}.`);
        }

        const caveats = evidenceCaveats(msg);
        if (lines.length === 0 && caveats.length === 0) return null;
        return { lines: lines.slice(0, 5), caveats: caveats.slice(0, 2) };
    }

    function pushAssistantMessage(content, operation = null, extras = {}) {
        messages = [...messages, {
            role: 'assistant',
            content,
            operation,
            data: extras.data || null,
            evidence: extras.evidence || null,
            sql: extras.sql || null,
            preview_changes: extras.preview_changes || [],
            confirmation_id: extras.confirmation_id || null,
            needs_confirmation: extras.needs_confirmation || false,
            rows_affected: extras.rows_affected || 0,
            rows_total: extras.rows_total || 0,
            original_question: extras.original_question || null,
            answer_context: extras.answer_context || null,
            answer_guard: extras.answer_guard || null,
            trace: extras.trace || null,
        }];
    }

    async function refreshSidebar() {
        sidebarLoading = true;
        advisorReadLoading = true;
        try {
            const [recurringResult, historyResult, advisorResult] = await Promise.all([
                api.getRecurring().catch(() => null),
                api.getCopilotHistory(20, activeProfileId).catch(() => ({ items: [] })),
                api.getMiraAdvisorRead(activeProfileId).catch(() => ({ enabled: false, memo: null })),
            ]);
            recurringData = recurringResult;
            historyItems = historyResult?.items || [];
            advisorReadEnabled = Boolean(advisorResult?.enabled);
            advisorReadContextEnabled = Boolean(advisorResult?.context_enabled);
            advisorReadGenerationEnabled = Boolean(advisorResult?.generation_enabled);
            advisorReadJob = advisorResult?.job || { status: 'idle' };
            advisorRead = advisorResult?.memo || null;
        } finally {
            sidebarLoading = false;
            advisorReadLoading = false;
        }
    }

    async function loadLocalLlm() {
        try {
            const [status, catalog] = await Promise.all([
                api.getLocalLlmStatus(),
                api.getLocalLlmCatalog(),
            ]);
            localLlmStatus = status;
            localLlmCatalog = catalog;
            copilotModel = status?.selectedCopilotModel || '';
        } catch (error) {
            localLlmStatus = null;
            localLlmCatalog = null;
        }
    }

    async function loadAppConfig() {
        try {
            appConfig = { ...appConfig, ...(await api.getAppConfig()) };
        } catch (error) {
            appConfig = {
                ...appConfig,
                receiptIntelligenceEnabled: false,
                miraAdvisorReadUiEnabled: false,
                miraAdvisorReadContextEnabled: false,
                miraAdvisorReadGenerationEnabled: false,
                miraFinancialFeedbackLoopEnabled: false,
            };
        }
    }

    async function updateCopilotModelSelection(nextModel) {
        if (!localLlmStatus?.expertMode || !nextModel || nextModel === localLlmStatus?.selectedCopilotModel || copilotModelSaving) return;
        modelDropdownOpen = false;
        copilotModelSaving = true;
        try {
            const result = await api.updateLocalLlmSettings({ copilot_model: nextModel });
            if (result?.status) {
                localLlmStatus = result.status;
                copilotModel = result.status.selectedCopilotModel || nextModel;
            } else {
                copilotModel = nextModel;
            }
            setNotice(`Mira model switched to ${nextModel}.`);
        } catch (error) {
            copilotModel = localLlmStatus?.selectedCopilotModel || '';
            setNotice(error?.message || 'Failed to switch Mira model.');
        } finally {
            copilotModelSaving = false;
        }
    }

    function selectCopilotModel(model) {
        if (!model || !localLlmStatus?.expertMode || modelRequiresAdvanced(model) && !localLlmStatus?.expertMode || copilotModelSaving) return;
        if (model.id === copilotModel) {
            modelDropdownOpen = false;
            return;
        }
        updateCopilotModelSelection(model.id);
    }

    function modelRequiresAdvanced(model) {
        return !!(model?.expert_only || model?.advanced_only || model?.validated_for_mira !== true);
    }

    async function installCopilotModel() {
        if (!copilotModel || copilotModelInstalling || localLlmStatus?.provider !== 'ollama' || !localLlmStatus?.ollamaReachable) return;
        copilotModelInstalling = true;
        try {
            const result = await api.installLocalLlmModel(copilotModel);
            if (result?.status) {
                localLlmStatus = result.status;
            }
            await loadLocalLlm();
            setNotice(`${copilotModel} installed in Ollama.`);
        } catch (error) {
            setNotice(error?.message || `Failed to install ${copilotModel}.`);
        } finally {
            copilotModelInstalling = false;
        }
    }

    async function openPage(path, params = new URLSearchParams()) {
        const query = params.toString();
        await goto(query ? `${path}?${query}` : path);
    }

    async function openControlCenter(tab, { prompt = '', merchantFilter = '' } = {}) {
        const params = new URLSearchParams();
        if (tab && tab !== 'merchants') params.set('tab', tab);
        if (prompt) params.set('prompt', prompt);
        if (merchantFilter) params.set('merchant_filter', merchantFilter);
        await openPage('/control-center', params);
    }

    async function handleShortcut(question) {
        const command = question.trim().toLowerCase().replace(/\s+/g, ' ');
        if (!command.startsWith('/')) return null;

        if (command === '/open transactions') {
            await openPage('/transactions');
            return "Opened Transactions.";
        }
        if (command === '/open merchants') {
            await openControlCenter('merchants');
            return "Opened Control Center on Merchants.";
        }
        if (command === '/open rules') {
            await openControlCenter('rules');
            return "Opened Control Center on Rules.";
        }
        if (command === '/open categories') {
            await openControlCenter('categories');
            return "Opened Control Center on Categories.";
        }
        if (command === '/open subscriptions') {
            await openControlCenter('merchants', { merchantFilter: 'subscriptions' });
            return "Opened Control Center on recurring merchants.";
        }
        if (command === '/open history') {
            openHistory();
            return "Opened recent Mira activity.";
        }
        if (command === '/sync' || command === '/refresh') {
            const result = await runSync();
            return `Sync finished: ${result.accounts} accounts and ${result.transactions} transactions processed.`;
        }
        if (command === '/redetect subscriptions') {
            await runRedetectSubscriptions();
            return "Subscription detection has been re-run.";
        }
        if (command === '/clear alerts') {
            await markAllEventsRead();
            return "Marked all subscription alerts as read.";
        }

        return null;
    }

    function stopStream() {
        if (cancelStream) {
            try { cancelStream(); } catch {}
            cancelStream = null;
        }
        loading = false;
    }

    function advisorReadFollowupHistory() {
        let start = Math.max(0, messages.length - 8);
        for (let i = messages.length - 1; i >= 0; i--) {
            if (messages[i]?.role === 'assistant' && messages[i]?.operation === 'advisor_read_followup') {
                start = Math.max(0, i - 1);
                break;
            }
        }
        return messages
            .slice(start)
            .filter((m) => m && m.content && (m.role === 'user' || m.role === 'assistant'))
            .slice(-6)
            .map((m) => ({
                role: m.role,
                content: String(m.content || '').slice(0, 1200),
                operation: m.operation || null,
            }));
    }

    function latestAssistantWasAdvisorReadFollowup() {
        for (let i = messages.length - 1; i >= 0; i--) {
            const msg = messages[i];
            if (!msg || msg.role !== 'assistant' || msg.operation === 'streaming') continue;
            return msg.operation === 'advisor_read_followup';
        }
        return false;
    }

    function looksLikeLiveFolioRequest(question) {
        const text = String(question || '').trim().toLowerCase();
        if (!text || text.startsWith('/')) return true;
        const commandStarts = [
            'show me ', 'list ', 'plot ', 'chart ', 'graph ', 'open ', 'create ',
            'update ', 'change ', 'rename ', 'categorize ', 'recategorize ', 'mark ',
            'sync ', 'refresh ', 'save ', 'remember ',
        ];
        if (commandStarts.some((prefix) => text.startsWith(prefix))) return true;
        const liveTerms = ['transactions', 'transaction list', 'receipt', 'receipts', 'chart', 'plot', 'graph'];
        if (liveTerms.some((term) => text.includes(term))) return true;
        const exactTerms = ['how much', 'exact', 'total', 'sum ', 'spent at ', 'spend at ', 'budget'];
        return exactTerms.some((term) => text.includes(term));
    }

    function shouldRouteTypedAdvisorReadFollowup(question) {
        if (!advisorRead || !advisorReadFollowupEnabled || advisorReadJobRunning || advisorReadGenerating) return false;
        if (advisorReadThreadMemoId && advisorRead?.id && advisorReadThreadMemoId !== advisorRead.id) return false;
        if (!(advisorReadThreadActive || latestAssistantWasAdvisorReadFollowup())) return false;
        return !looksLikeLiveFolioRequest(question);
    }

    async function sendAdvisorReadFollowup(followupType, question) {
        const trimmed = String(question || '').trim();
        if (!trimmed) return;
        if (loading) stopStream();
        const history = advisorReadFollowupHistory();
        messages = [...messages, {
            role: 'user',
            content: trimmed,
            operation: null,
            data: null,
            evidence: null,
            sql: null,
            preview_changes: [],
            needs_confirmation: false,
            rows_affected: 0
        }];
        input = '';
        loading = true;
        await tick();
        scrollToBottom();
        try {
            const result = await api.askMiraAdvisorReadFollowup(followupType, trimmed, activeProfileId, history);
            pushAssistantMessage(result.answer || "Mira couldn't answer from the stored read cleanly.", result.operation || 'advisor_read_followup', {
                original_question: trimmed,
                answer_context: result.answer_context || null,
                answer_guard: result.answer_guard || null,
                trace: { runtime: 'advisor_read_followup', answer_path: result.answer_guard?.path || 'advisor_read_followup' },
            });
            advisorReadThreadActive = true;
            advisorReadThreadMemoId = result.memo_id || advisorRead?.id || null;
        } catch (error) {
            advisorReadThreadActive = false;
            advisorReadThreadMemoId = null;
            pushAssistantMessage(error?.message || "Mira couldn't answer from the stored read.", 'error', {
                original_question: trimmed,
            });
        } finally {
            loading = false;
            await tick();
            scrollToBottom();
        }
    }

    async function send() {
        const question = input.trim();
        if (!question) return;
        if (miraDemoPreviewOnly) {
            setNotice('Mira chat is disabled in the public demo because no local model is attached.');
            return;
        }

        if (shouldRouteTypedAdvisorReadFollowup(question)) {
            await sendAdvisorReadFollowup('general', question);
            return;
        }
        advisorReadThreadActive = false;
        advisorReadThreadMemoId = null;

        // Cancel any in-flight request (cancel-on-resubmit)
        if (loading) stopStream();

        messages = [...messages, {
            role: 'user',
            content: question,
            operation: null,
            data: null,
            evidence: null,
            sql: null,
            preview_changes: [],
            needs_confirmation: false,
            rows_affected: 0
        }];
        input = '';
        loading = true;

        await tick();
        scrollToBottom();

        try {
            const shortcut = await handleShortcut(question);
            if (shortcut) {
                pushAssistantMessage(shortcut, 'success');
                await refreshSidebar();
                loading = false;
                return;
            }
        } catch {}

        // Build chat history from prior turns
        const history = messages
            .slice(0, -1)
            .filter(m => m && m.content && (m.role === 'user' || (m.role === 'assistant' && m.operation && m.operation !== 'error')))
            .slice(-12)
            .map(m => {
                const turn = { role: m.role, content: m.content };
                if (m.dialogue_state) turn.dialogue_state = m.dialogue_state;
                if (m.answer_context) turn.answer_context = m.answer_context;
                if (m.answer_guard) turn.answer_guard = {
                    path: m.answer_guard.path || '',
                    used_fallback: !!m.answer_guard.used_fallback,
                    error: m.answer_guard.error || ''
                };
                if (m.trace) turn.trace = {
                    runtime: m.trace.runtime || '',
                    answer_path: m.trace.answer_path || '',
                    tool_result_count: m.trace.tool_result_count || 0,
                    evidence_fact_count: m.trace.evidence_fact_count || 0,
                    evidence_row_count: m.trace.evidence_row_count || 0,
                    evidence_chart_count: m.trace.evidence_chart_count || 0
                };
                if (m.tool_trace && m.tool_trace.length > 0) {
                    turn.tool_context = m.tool_trace.slice(0, 4).map(t => ({
                        name: t.name,
                        args: t.args || {}
                    }));
                }
                return turn;
            });

        // Add a live assistant placeholder that we'll mutate as events arrive
        messages = [...messages, {
            role: 'assistant',
            content: '',
            operation: 'streaming',
            data: null,
            sql: null,
            preview_changes: [],
            needs_confirmation: false,
            rows_affected: 0,
            rows_total: 0,
            original_question: question,
            tool_trace: [],
            trace: null,
            answer_guard: null,
            runtime: appConfig.miraAgenticRuntime || null,
            active_tool: null,
            progress: 'Thinking',
        }];
        const streamIdx = messages.length - 1;
        await tick();
        scrollToBottom();

        let currentContent = '';
        let evidencePreviewShown = false;
        let previewAnswer = '';
        cancelStream = api.askCopilotStream(question, activeProfileId, history, (ev) => {
            const msg = { ...messages[streamIdx] };
            switch (ev.type) {
                case 'routing_started':
                    msg.progress = 'Thinking';
                    break;
                case 'route':
                    if (ev.dialogue_state) msg.dialogue_state = ev.dialogue_state;
                    msg.runtime = ev.mira_planner || ev.trace?.runtime || msg.runtime || null;
                    if (ev.trace) msg.trace = ev.trace;
                    break;
                case 'controller':
                    msg.controller_act = ev.controller_act || null;
                    break;
                case 'action':
                    msg.domain_action = ev.domain_action || null;
                    break;
                case 'progress':
                    {
                        const label = ev.label || ev.stage || 'Working';
                        const normalizedLabel = label.toLowerCase().includes('routing the request') ? 'Thinking' : label;
                        msg.progress = normalizedLabel.toLowerCase().includes('selected general answer') ? null : normalizedLabel;
                    }
                    break;
                case 'reset_text':
                    currentContent = '';
                    if (!evidencePreviewShown) {
                        msg.content = '';
                    }
                    break;
                case 'token':
                    currentContent += ev.text || '';
                    if (evidencePreviewShown) {
                        msg.progress = previewAnswer ? null : 'Reading the evidence';
                    } else {
                        msg.content = scrubMemoryTags(currentContent);
                        msg.progress = null;
                    }
                    break;
                case 'tool_call':
                    msg.active_tool = ev.name;
                    msg.progress = null;
                    msg.tool_trace = [...(msg.tool_trace || []), { name: ev.name, args: ev.args, duration_ms: null }];
                    break;
                case 'chart':
                    msg.chart = ev.chart;
                    break;
                case 'evidence_preview':
                    evidencePreviewShown = true;
                    if (ev.data) msg.data = ev.data;
                    if (ev.data_source) msg.data_source = ev.data_source;
                    if (ev.evidence) msg.evidence = ev.evidence;
                    if (ev.chart) msg.chart = ev.chart;
                    if (ev.tool_trace && ev.tool_trace.length > 0) msg.tool_trace = ev.tool_trace;
                    msg.operation = 'read';
                    msg.rows_affected = Array.isArray(ev.data) ? ev.data.length : (ev.rows_affected || msg.rows_affected || 0);
                    msg.rows_total = ev.rows_total || msg.rows_total || msg.rows_affected || 0;
                    msg.progress = msg.content?.trim() ? null : 'Reading the evidence';
                    break;
                case 'preview_answer':
                    previewAnswer = scrubMemoryTags(ev.text || '').trim();
                    if (previewAnswer) {
                        msg.content = previewAnswer;
                        msg.progress = null;
                    }
                    break;
                case 'tool_result': {
                    msg.active_tool = null;
                    msg.progress = null;
                    const trace = [...(msg.tool_trace || [])];
                    for (let i = trace.length - 1; i >= 0; i--) {
                        if (trace[i].name === ev.name && trace[i].duration_ms == null) {
                            trace[i] = { ...trace[i], duration_ms: ev.duration_ms };
                            break;
                        }
                    }
                    msg.tool_trace = trace;
                    break;
                }
                case 'done':
                    if (evidencePreviewShown) {
                        msg.content = combinePreviewAndFinalAnswer(previewAnswer, ev.answer || currentContent || '', ev.answer_guard);
                    } else if (!(ev.evidence_attach_only || ev.answer_guard?.evidence_attach_only) || !msg.content?.trim()) {
                        msg.content = (ev.answer || currentContent || '').trim();
                    }
                    msg.data = ev.data || null;
                    msg.data_source = ev.data_source || null;
                    msg.evidence = ev.evidence || msg.evidence || null;
                    msg.tool_trace = (ev.tool_trace && ev.tool_trace.length > 0)
                        ? ev.tool_trace
                        : (msg.tool_trace || []);
                    msg.active_tool = null;
                    msg.progress = null;
                    msg.memory_proposals = ev.memory_proposals || [];
                    msg.suggested_memory = ev.suggested_memory || null;
                    msg.dialogue_state = ev.dialogue_state || ev.route?.dialogue_state || null;
                    msg.answer_context = ev.answer_context || null;
                    msg.trace = ev.trace || ev.route?.trace || msg.trace || null;
                    msg.answer_guard = ev.answer_guard || null;
                    msg.rows_total = ev.rows_total || msg.rows_total || (Array.isArray(ev.data) ? ev.data.length : 0);
                    msg.runtime = ev.trace?.runtime || ev.route?.mira_planner || ev.route?.trace?.runtime || msg.runtime || null;
                    if (ev.chart) msg.chart = ev.chart;
                    if (ev.pending_write) {
                        msg.operation = 'write_preview';
                        msg.confirmation_id = ev.pending_write.confirmation_id;
                        msg.sql = null;
                        msg.preview_changes = ev.pending_write.preview_changes || [];
                        msg.needs_confirmation = true;
                        msg.rows_affected = ev.pending_write.rows_affected || 0;
                        msg.rows_total = msg.rows_affected;
                    } else {
                        msg.operation = 'read';
                        msg.rows_affected = Array.isArray(ev.data) ? ev.data.length : 0;
                        msg.rows_total = ev.rows_total || msg.rows_total || msg.rows_affected;
                    }
                    loading = false;
                    cancelStream = null;
                    refreshSidebar();
                    break;
                case 'memory_update':
                    // Late-arriving proposals from the post-turn detector. Append to
                    // any existing proposals, dedup by id so re-emits don't duplicate.
                    {
                        const incoming = ev.memory_proposals || [];
                        const existing = msg.memory_proposals || [];
                        const seen = new Set(existing.map((p) => p.id));
                        const merged = [...existing];
                        for (const p of incoming) {
                            if (!seen.has(p.id)) merged.push(p);
                        }
                        msg.memory_proposals = merged;
                        if (ev.suggested_memory) {
                            msg.suggested_memory = ev.suggested_memory;
                        }
                    }
                    break;
                case 'error':
                    msg.content = ev.message || 'Something went wrong.';
                    msg.operation = 'error';
                    msg.active_tool = null;
                    msg.progress = null;
                    loading = false;
                    cancelStream = null;
                    break;
            }
            messages[streamIdx] = msg;
            messages = messages;
            tick().then(scrollToBottom);
        });
    }

    async function saveInsight(msgIndex) {
        const msg = messages[msgIndex];
        if (!msg || msg.saved || msg.saving) return;

        let question = msg.original_question;
        if (!question) {
            for (let j = msgIndex - 1; j >= 0; j--) {
                if (messages[j].role === 'user') {
                    question = messages[j].content;
                    break;
                }
            }
        }
        if (!question || !msg.content) return;

        messages[msgIndex] = { ...msg, saving: true };
        messages = [...messages];

        try {
            const result = await api.saveInsight(question, msg.content, 'insight', null, activeProfileId);
            messages[msgIndex] = { ...messages[msgIndex], saving: false, saved: !!result?.saved };
            messages = [...messages];
            if (result?.saved && result.entry?.body) {
                setNotice(`Saved to memory: ${result.entry.body}`);
            } else if (result?.reason) {
                setNotice(result.reason);
            } else {
                setNotice('Saved to memory.');
            }
        } catch (error) {
            messages[msgIndex] = { ...messages[msgIndex], saving: false };
            messages = [...messages];
            setNotice(error?.message || 'Failed to save insight.');
        }
    }

    async function acceptMemoryProposal(msgIndex, proposalId) {
        try {
            await api.acceptMemoryProposal(proposalId, null, activeProfileId);
            const msg = { ...messages[msgIndex] };
            msg.memory_proposals = (msg.memory_proposals || []).filter((p) => p.id !== proposalId);
            messages[msgIndex] = msg;
            messages = [...messages];
            setNotice('Added to memory.');
        } catch (error) {
            setNotice(error?.message || 'Failed to accept proposal.');
        }
    }

    async function rejectMemoryProposal(msgIndex, proposalId) {
        try {
            await api.rejectMemoryProposal(proposalId, activeProfileId);
            const msg = { ...messages[msgIndex] };
            msg.memory_proposals = (msg.memory_proposals || []).filter((p) => p.id !== proposalId);
            messages[msgIndex] = msg;
            messages = [...messages];
        } catch (error) {
            setNotice(error?.message || 'Failed to reject proposal.');
        }
    }

    async function acceptSuggestedMemory(msgIndex) {
        const msg = messages[msgIndex];
        const suggestion = msg?.suggested_memory;
        if (!suggestion || suggestion.saving) return;

        messages[msgIndex] = {
            ...msg,
            suggested_memory: { ...suggestion, saving: true }
        };
        messages = [...messages];

        try {
            const result = await api.createMiraMemory({
                text: suggestion.text,
                memory_type: suggestion.memory_type || suggestion.type,
                topic: suggestion.topic,
                source_summary: suggestion.reason || suggestion.evidence || ''
            }, activeProfileId);
            const next = { ...messages[msgIndex], suggested_memory: null };
            messages[msgIndex] = next;
            messages = [...messages];
            if (result?.saved) {
                setNotice(`Remembered: ${result.memory?.normalized_text || suggestion.text}`);
            } else {
                setNotice(result?.reason || 'I did not save that as memory.');
            }
        } catch (error) {
            const latest = messages[msgIndex] || msg;
            messages[msgIndex] = {
                ...latest,
                suggested_memory: { ...suggestion, saving: false }
            };
            messages = [...messages];
            setNotice(error?.message || 'Failed to save memory.');
        }
    }

    function dismissSuggestedMemory(msgIndex) {
        const msg = messages[msgIndex];
        if (!msg?.suggested_memory) return;
        messages[msgIndex] = { ...msg, suggested_memory: null };
        messages = [...messages];
    }

    async function confirmWrite(msgIndex) {
        const msg = messages[msgIndex];
        if (!msg || !msg.confirmation_id) return;

        loading = true;
        await tick();

        try {
            const res = await api.confirmCopilotWrite(msg.original_question, msg.confirmation_id, activeProfileId);
            messages[msgIndex] = {
                ...messages[msgIndex],
                needs_confirmation: false,
                confirmed: true
            };
            messages = [...messages];

            pushAssistantMessage(res.answer || `Updated ${res.rows_affected} transaction(s).`, 'write_executed', {
                rows_affected: res.rows_affected || 0
            });

            invalidateCache();
            await refreshSidebar();
        } catch (error) {
            const message = error?.code === 'confirmation_expired'
                ? 'Preview expired. Ask Mira to prepare it again.'
                : (error?.message || 'Failed to execute the operation. Please try again.');
            pushAssistantMessage(message, 'error');
        } finally {
            loading = false;
            await tick();
            scrollToBottom();
        }
    }

    function cancelWrite(msgIndex) {
        messages[msgIndex] = {
            ...messages[msgIndex],
            needs_confirmation: false,
            confirmed: false
        };
        messages = [...messages];
        pushAssistantMessage("Operation cancelled.");
    }

    function toggleSql(msgIndex) {
        showSqlForMsg = { ...showSqlForMsg, [msgIndex]: !showSqlForMsg[msgIndex] };
    }

    function toggleHistorySql(id) {
        showSqlForHistory = { ...showSqlForHistory, [id]: !showSqlForHistory[id] };
    }

    // ── Chip action functions ──

    function buildChipMessage(chip, values) {
        if (chip.id === 'explain_category') return `Why is ${values.merchant} categorized the way it is?`;
        if (chip.id === 'find_missing_categories') return 'Show me merchants with missing categories';
        if (chip.id === 'bulk_recategorize') return `Move all ${values.merchant} transactions to ${values.category}`;
        if (chip.id === 'create_rule') return `Create a rule: ${values.pattern} → ${values.category}`;
        if (chip.id === 'rename_merchant') return `Rename ${values.old_name} to ${values.new_name}`;
        if (chip.id === 'receipt_compare') return 'Compare grocery receipt prices';
        return chip.label;
    }

    async function executeChipAction(id, values) {
        if (id === 'explain_category') {
            const res = await api.explainCategory(values.merchant, scopedProfile);
            pushAssistantMessage(res.answer, res.operation || 'read', {
                data: res.samples?.length ? res.samples : null,
            });
        } else if (id === 'find_missing_categories') {
            const res = await api.getMerchantsMissingCategory(scopedProfile);
            pushAssistantMessage(res.answer, res.operation || 'read', {
                data: res.items?.length ? res.items : null,
            });
        } else if (id === 'bulk_recategorize') {
            const res = await api.bulkRecategorizePreview(values.merchant, values.category, scopedProfile);
            if (!res.needs_confirmation) {
                pushAssistantMessage(res.answer, 'read');
            } else {
                pushAssistantMessage(res.answer, 'write_preview', {
                    data: res.samples?.length ? res.samples : null,
                    preview_changes: res.preview_changes || [],
                    confirmation_id: res.confirmation_id,
                    needs_confirmation: true,
                    rows_affected: res.count || 0,
                    original_question: `Move all ${values.merchant} transactions to ${values.category}`,
                });
            }
        } else if (id === 'create_rule') {
            const res = await api.previewRuleCreation(values.pattern, values.category, scopedProfile);
            pushAssistantMessage(res.answer, 'write_preview', {
                data: res.samples?.length ? res.samples : null,
                preview_changes: res.preview_changes || [],
                confirmation_id: res.confirmation_id,
                needs_confirmation: true,
                rows_affected: res.count || 0,
                original_question: `Create rule: ${values.pattern} → ${values.category}`,
            });
        } else if (id === 'rename_merchant') {
            const res = await api.renameMerchantPreview(values.old_name, values.new_name, scopedProfile);
            if (!res.needs_confirmation) {
                pushAssistantMessage(res.answer, 'read');
            } else {
                pushAssistantMessage(res.answer, 'write_preview', {
                    data: res.samples?.length ? res.samples : null,
                    preview_changes: res.preview_changes || [],
                    confirmation_id: res.confirmation_id,
                    needs_confirmation: true,
                    rows_affected: res.count || 0,
                    original_question: `Rename ${values.old_name} to ${values.new_name}`,
                });
            }
        }
    }

    async function activateChip(chip) {
        if (chip.prompt) {
            input = chip.prompt;
            await tick();
            await send();
            return;
        }
        if (chip.id === 'receipt_compare') {
            openReceipts();
            return;
        }
        if (chip.inputs.length === 0) {
            // No inputs needed — execute immediately
            const userMsg = buildChipMessage(chip, {});
            messages = [...messages, {
                role: 'user', content: userMsg, operation: null,
                data: null, sql: null, preview_changes: [], needs_confirmation: false, rows_affected: 0,
            }];
            loading = true;
            await tick();
            scrollToBottom();
            try {
                await executeChipAction(chip.id, {});
            } catch (err) {
                pushAssistantMessage("Sorry, I couldn't process that. Please try again.", 'error');
            } finally {
                loading = false;
                await tick();
                scrollToBottom();
            }
        } else {
            activeChip = chip.id;
            chipFormValues = {};
        }
    }

    async function askAboutAdvisorRead(followupType, seed) {
        await sendAdvisorReadFollowup(followupType, seed);
    }

    function toggleAdvisorReadFull() {
        if (!advisorRead) return;
        advisorReadPanelExpanded = true;
        advisorReadExpanded = !advisorReadExpanded;
    }

    function handleAdvisorReadHeroKeydown(event) {
        if (!advisorRead || !['Enter', ' '].includes(event.key)) return;
        event.preventDefault();
        toggleAdvisorReadFull();
    }

    async function askAboutAdvisorCard(card) {
        if (!card) return;
        const followupType = card.followup_type || 'general';
        const question = card.question || `Tell me more about ${card.title || "Mira's read"}.`;
        await sendAdvisorReadFollowup(followupType, question);
    }

    function advisorCardFeedbackSummary(card) {
        const feedback = card?.feedback || {};
        const types = feedback.feedback_types || {};
        if (types.too_sensitive) return 'Marked sensitive';
        if (types.corrected) return 'Corrected';
        if (types.more_like_this) return 'More like this';
        if (types.less_like_this || types.dismissed) return 'Less often';
        if (types.snoozed) return 'Snoozed';
        return '';
    }

    async function sendAdvisorCardFeedback(card, feedbackType, extra = {}) {
        if (!card?.id || !appConfig.miraFinancialFeedbackLoopEnabled) return;
        try {
            const result = await api.createMiraFinancialFeedback({
                feedback_type: feedbackType,
                target_type: 'advisor_card',
                target_id: card.id,
                subject_type: 'advisor_read',
                subject_key: card.id,
                source: 'advisor_read',
                metadata: {
                    card_title: card.title || '',
                    card_kicker: card.kicker || '',
                },
                ...extra,
            }, activeProfileId);
            if (result?.status === 'stored') {
                setNotice('Got it. Mira will use that when shaping future reads.');
                await refreshAdvisorRead();
            } else if (result?.status === 'disabled') {
                setNotice('Financial read feedback is disabled right now.');
            }
        } catch (error) {
            setNotice(error?.message || "Mira couldn't save that feedback.");
        }
    }

    function startAdvisorCardCorrection(card) {
        if (!card?.id || !appConfig.miraFinancialFeedbackLoopEnabled) return;
        advisorCorrectionCardId = card.id;
        advisorCorrectionText = '';
    }

    function cancelAdvisorCardCorrection() {
        advisorCorrectionCardId = null;
        advisorCorrectionText = '';
    }

    async function submitAdvisorCardCorrection(card) {
        if (!card?.id || advisorCorrectionCardId !== card.id || !advisorCorrectionText.trim()) return;
        const title = card.title || "Mira's read";
        await sendAdvisorCardFeedback(card, 'corrected', {
            correction_text: advisorCorrectionText.trim(),
            safe_summary: `User corrected Mira's framing for ${title}.`,
        });
        cancelAdvisorCardCorrection();
    }

    async function refreshAdvisorRead() {
        if (advisorReadLoading) return;
        advisorReadLoading = true;
        try {
            const result = await api.refreshMiraAdvisorRead(activeProfileId);
            advisorReadEnabled = Boolean(result?.enabled);
            advisorReadContextEnabled = Boolean(result?.context_enabled);
            advisorReadGenerationEnabled = Boolean(result?.generation_enabled);
            advisorReadJob = result?.job || { status: 'idle' };
            advisorRead = result?.memo || null;
            const preflight = result?.preflight || {};
            if (preflight?.decision === 'keep_existing_delta') {
                setNotice("Mira's stored read still has the same update.");
            } else if (preflight?.decision === 'store_targeted_delta') {
                setNotice("Mira noticed the stored read has a targeted update.");
            } else if (preflight?.decision === 'queue_full_advisor_synthesis') {
                setNotice("Mira thinks this read needs a fresh rebuild.");
            } else {
                setNotice(advisorRead ? "Mira's stored read is loaded." : "Mira hasn't stored a financial read yet.");
            }
        } catch (error) {
            setNotice(error?.message || "Mira couldn't load her stored read.");
        } finally {
            advisorReadLoading = false;
        }
    }

    function scheduleAdvisorReadPoll(profile = activeProfileId) {
        if (advisorReadPollTimer) clearTimeout(advisorReadPollTimer);
        advisorReadPollTimer = setTimeout(() => {
            pollAdvisorRead(profile);
        }, 5000);
    }

    async function pollAdvisorRead(profile = activeProfileId) {
        try {
            const result = await api.getMiraAdvisorRead(profile);
            advisorReadEnabled = Boolean(result?.enabled);
            advisorReadContextEnabled = Boolean(result?.context_enabled);
            advisorReadGenerationEnabled = Boolean(result?.generation_enabled);
            advisorReadJob = result?.job || { status: 'idle' };
            advisorRead = result?.memo || advisorRead;
            const status = advisorReadJob?.status;
            if (['queued', 'running'].includes(status)) {
                scheduleAdvisorReadPoll(profile);
                return;
            }
            advisorReadGenerating = false;
            if (status === 'completed') {
                advisorRead = result?.memo || advisorRead;
                setNotice("Mira's new financial read is ready.");
            } else if (status === 'no_valid_memo') {
                setNotice("Mira finished, but the read did not pass validation.");
            } else if (status === 'error') {
                setNotice("Mira couldn't generate the read. Check backend logs.");
            }
        } catch (error) {
            advisorReadGenerating = false;
            setNotice(error?.message || "Mira couldn't check advisor read status.");
        }
    }

    async function generateAdvisorRead(force = true) {
        if (advisorReadGenerating || advisorReadJobRunning) return;
        if (!advisorReadCanGenerate) {
            setNotice("Advisor read generation is disabled.");
            return;
        }
        advisorReadGenerating = true;
        try {
            const result = await api.generateMiraAdvisorRead(force, activeProfileId);
            advisorReadEnabled = Boolean(result?.enabled);
            advisorReadContextEnabled = Boolean(result?.context_enabled);
            advisorReadGenerationEnabled = Boolean(result?.generation_enabled);
            advisorReadJob = result?.job || { status: result?.status || 'idle' };
            advisorRead = result?.memo || advisorRead;
            if (result?.status === 'disabled') {
                advisorReadGenerating = false;
                setNotice(result?.reason === 'generation_disabled'
                    ? 'Advisor read generation is disabled.'
                    : "Mira's read UI is disabled.");
                return;
            }
            setNotice("Mira started generating a fresh financial read locally. This can take a few minutes.");
            scheduleAdvisorReadPoll(activeProfileId);
        } catch (error) {
            advisorReadGenerating = false;
            setNotice(error?.message || "Mira couldn't start advisor read generation.");
        }
    }

    async function submitChipForm() {
        const chip = chipActions.find(c => c.id === activeChip);
        if (!chip) return;
        const userMsg = buildChipMessage(chip, chipFormValues);
        messages = [...messages, {
            role: 'user', content: userMsg, operation: null,
            data: null, sql: null, preview_changes: [], needs_confirmation: false, rows_affected: 0,
        }];
        loading = true;
        activeChip = null;
        await tick();
        scrollToBottom();
        try {
            await executeChipAction(chip.id, chipFormValues);
        } catch (err) {
            pushAssistantMessage("Sorry, I couldn't process that. Please try again.", 'error');
        } finally {
            loading = false;
            await tick();
            scrollToBottom();
        }
    }

    function reuseHistoryPrompt(item) {
        input = item.user_message || '';
        closeHistory();
    }

    function scrollToBottom() {
        if (chatContainer) chatContainer.scrollTop = chatContainer.scrollHeight;
    }

    function handleKeydown(e) {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            send();
        } else if (e.key === 'Escape' && loading) {
            e.preventDefault();
            stopStream();
        }
    }

    async function runSync() {
        try {
            const result = await api.sync();
            invalidateCache();
            setNotice('Data sync completed.');
            await refreshSidebar();
            return result;
        } catch (error) {
            setNotice('Sync failed.');
            throw error;
        }
    }

    async function runRedetectSubscriptions() {
        try {
            await api.redetectSubscriptions(scopedProfile);
            invalidateCache();
            setNotice('Subscriptions re-scanned.');
            await refreshSidebar();
        } catch (error) {
            setNotice('Subscription re-scan failed.');
            throw error;
        }
    }

    async function markAllEventsRead() {
        if (!unreadEvents.length) {
            setNotice('No unread alerts to clear.');
            return;
        }
        try {
            await api.markEventsRead(unreadEvents.map((event) => event.id));
            await refreshSidebar();
            setNotice('Alerts marked as read.');
        } catch (error) {
            setNotice('Failed to mark alerts read.');
            throw error;
        }
    }

    function openReceipts() {
        receiptsOpen = true;
        receiptError = '';
        loadReceiptWorkspace();
    }

    function closeReceipts() {
        receiptsOpen = false;
        receiptError = '';
    }

    async function loadReceiptComparisons() {
        if (!appConfig.receiptIntelligenceEnabled) return;
        try {
            const result = await api.getReceiptComparisons(activeProfileId);
            receiptComparisons = result?.items || [];
        } catch (error) {
            receiptComparisons = [];
        }
    }

    async function loadReceiptDrafts() {
        if (!appConfig.receiptIntelligenceEnabled) return;
        try {
            const result = await api.getReceipts(activeProfileId, { status: 'draft', limit: 6 });
            receiptDrafts = result?.items || [];
        } catch (error) {
            receiptDrafts = [];
        }
    }

    async function loadReceiptWorkspace() {
        await Promise.all([loadReceiptComparisons(), loadReceiptDrafts()]);
    }

    function handleReceiptFile(event) {
        receiptFile = event.currentTarget.files?.[0] || null;
        receiptError = '';
    }

    function setReceiptItemValue(index, key, value) {
        const next = [...receiptItems];
        const current = { ...next[index] };
        if (['quantity', 'total_price', 'unit_price'].includes(key)) {
            current[key] = value === '' ? null : Number(value);
        } else {
            current[key] = value;
        }
        next[index] = current;
        receiptItems = next;
    }

    function syncReceiptState(receipt) {
        receiptDraft = receipt;
        receiptItems = (receipt?.items || []).map((item) => ({ ...item }));
    }

    function clearReceiptReady(receiptId = null) {
        if (typeof sessionStorage === 'undefined') return;
        try {
            const raw = sessionStorage.getItem(receiptReadyStorageKey);
            if (!raw) return;
            const saved = JSON.parse(raw);
            if (!receiptId || Number(saved?.receiptId) === Number(receiptId)) {
                sessionStorage.removeItem(receiptReadyStorageKey);
            }
        } catch (_) {
            sessionStorage.removeItem(receiptReadyStorageKey);
        }
    }

    function publishReceiptReady(receipt, profile, fileName) {
        const detail = {
            receiptId: receipt?.id,
            profile,
            fileName,
            storeName: receipt?.store_name || '',
            itemCount: receipt?.items?.length || 0,
            completedAt: new Date().toISOString(),
        };
        if (typeof sessionStorage !== 'undefined') {
            try {
                sessionStorage.setItem(receiptReadyStorageKey, JSON.stringify(detail));
            } catch (_) {}
        }
        if (typeof window !== 'undefined') {
            window.dispatchEvent(new CustomEvent('folio:receipt-parse-done', { detail }));
        }
    }

    async function loadReceiptDraft(receiptId, profile = activeProfileId) {
        if (!receiptId) return;
        receiptError = '';
        try {
            const receipt = await api.getReceipt(receiptId, profile);
            syncReceiptState(receipt);
            receiptsOpen = true;
            receiptSummaryEditing = false;
            clearReceiptReady(receiptId);
            await loadReceiptWorkspace();
        } catch (error) {
            receiptError = error?.message || 'Failed to load receipt draft.';
        }
    }

    async function restoreReceiptFromNavigation() {
        if (!appConfig.receiptIntelligenceEnabled) return;
        const receiptId = Number($page.url.searchParams.get('receipt') || 0);
        const receiptProfile = $page.url.searchParams.get('receiptProfile') || activeProfileId;
        if (receiptId) {
            await loadReceiptDraft(receiptId, receiptProfile);
            return;
        }
        if ($page.url.searchParams.get('view') === 'receipts') {
            openReceipts();
            return;
        }
        if (typeof sessionStorage === 'undefined') return;
        try {
            const saved = JSON.parse(sessionStorage.getItem(receiptReadyStorageKey) || 'null');
            if (saved?.receiptId && (saved.profile || 'household') === activeProfileId) {
                await loadReceiptDraft(saved.receiptId, saved.profile);
            }
        } catch (_) {}
    }

    function setReceiptDraftValue(key, value) {
        if (!receiptDraft || receiptDraft.status !== 'draft') return;
        receiptDraft = { ...receiptDraft, [key]: value };
    }

    function receiptDraftMetadata() {
        return {
            store_name: receiptDraft?.store_name || '',
            receipt_date: receiptDraft?.receipt_date || null,
        };
    }

    async function parseSelectedReceipt() {
        if (!receiptFile || receiptParsing) return;
        const parseFileName = receiptFile.name || 'receipt image';
        const parseProfile = activeProfileId;
        receiptParsing = true;
        receiptError = '';
        startReceiptParseTimer();
        try {
            const receipt = await api.parseReceipt(receiptFile, parseProfile);
            publishReceiptReady(receipt, parseProfile, parseFileName);
            if (parseProfile === activeProfileId) {
                syncReceiptState(receipt);
                setNotice('Receipt parsed. Review the items before approving.');
                await loadReceiptWorkspace();
            }
        } catch (error) {
            receiptError = error?.message || 'Receipt parsing failed.';
        } finally {
            receiptParsing = false;
            clearReceiptParseTimer();
        }
    }

    async function saveReceiptItems() {
        if (!receiptDraft || receiptSaving) return;
        receiptSaving = true;
        receiptError = '';
        try {
            const receipt = await api.updateReceiptItems(receiptDraft.id, receiptItems, activeProfileId, receiptDraftMetadata());
            syncReceiptState(receipt);
            receiptSummaryEditing = false;
            await loadReceiptDrafts();
            setNotice('Receipt draft saved.');
        } catch (error) {
            receiptError = error?.message || 'Failed to save receipt items.';
        } finally {
            receiptSaving = false;
        }
    }

    async function approveReceiptDraft() {
        if (!receiptDraft || receiptSaving) return;
        receiptSaving = true;
        receiptError = '';
        try {
            await api.updateReceiptItems(receiptDraft.id, receiptItems, activeProfileId, receiptDraftMetadata());
            const receipt = await api.approveReceipt(receiptDraft.id, activeProfileId);
            syncReceiptState(receipt);
            receiptSummaryEditing = false;
            clearReceiptReady(receipt.id);
            await loadReceiptComparisons();
            await loadReceiptDrafts();
            setNotice('Receipt approved. Prices are now in comparisons.');
        } catch (error) {
            receiptError = error?.message || 'Failed to approve receipt.';
        } finally {
            receiptSaving = false;
        }
    }

    async function discardReceiptDraft() {
        if (!receiptDraft || receiptSaving) return;
        receiptSaving = true;
        receiptError = '';
        try {
            await api.discardReceipt(receiptDraft.id, activeProfileId);
            clearReceiptReady(receiptDraft.id);
            receiptDraft = null;
            receiptItems = [];
            receiptFile = null;
            receiptSummaryEditing = false;
            setNotice('Receipt discarded.');
            await loadReceiptWorkspace();
        } catch (error) {
            receiptError = error?.message || 'Failed to discard receipt.';
        } finally {
            receiptSaving = false;
        }
    }

    function startNewReceipt() {
        receiptDraft = null;
        receiptItems = [];
        receiptFile = null;
        receiptError = '';
        receiptSummaryEditing = false;
        loadReceiptDrafts();
    }

    function formatReceiptMoney(value) {
        if (value === null || value === undefined || value === '') return '—';
        const num = Number(value);
        return Number.isFinite(num) ? formatCurrency(num, 2) : '—';
    }

    function formatUnitPrice(value, unit = '') {
        const price = formatReceiptMoney(value);
        return price === '—' ? price : `${price}${unit ? `/${unit}` : ''}`;
    }

    function receiptComparisonStores(itemName) {
        const direct = receiptDraft?.comparisons?.[itemName];
        if (direct) return direct;
        return receiptComparisons.find((item) => item.item_name === itemName)?.stores || [];
    }

    function bestReceiptStore(stores) {
        if (!stores || stores.length === 0) return null;
        return stores.reduce((best, store) => {
            if (best == null) return store;
            return Number(store.lowest_unit_price) < Number(best.lowest_unit_price) ? store : best;
        }, null);
    }
</script>

<div class="copilot-page">
    <header class="copilot-identity-header fade-in">
        <div class="copilot-brand-lockup">
            <div class="copilot-brand-main">
                <div class="copilot-brand-text">
                    <h1 class="copilot-title">Mira</h1>
                    <p class="copilot-subtitle">Your Folio companion, powered by Gemma.</p>
                    <div class="copilot-identity-meta copilot-island-nav" aria-label="Mira sections">
                        {#if appConfig.receiptIntelligenceEnabled}
                            <button
                                type="button"
                                class="copilot-island-tab"
                                class:copilot-island-tab-active={receiptsOpen}
                                on:click={openReceipts}>
                                <span class="material-symbols-outlined">receipt_long</span>
                                Receipts
                            </button>
                        {/if}
                        <a href="/copilot/memory" class="copilot-island-tab" data-sveltekit-preload-data="hover">
                            <span class="material-symbols-outlined">bookmark</span>
                            Memory
                        </a>
                        <button
                            type="button"
                            class="copilot-island-tab"
                            class:copilot-island-tab-active={historyOpen}
                            on:click={openHistory}>
                            <span class="material-symbols-outlined">history</span>
                            History
                        </button>
                    </div>
                </div>
            </div>
            <div class="copilot-island-profile-row" aria-label="Mira profile scope">
                <ProfileSwitcher />
            </div>
        </div>
    </header>

    {#if actionNotice}
        <div class="copilot-notice fade-in">{actionNotice}</div>
    {/if}

    {#if receiptsOpen && appConfig.receiptIntelligenceEnabled}
        <section class="copilot-receipts-workspace fade-in-up">
            <div class="copilot-panel-header">
                <div>
                    <h3>Receipt Price Compare</h3>
                    <p>Upload a grocery receipt, review the parsed items, then approve it into your local price history.</p>
                </div>
                <div class="copilot-button-row">
                    {#if receiptDraft}
                        <button type="button" class="copilot-inline-btn" on:click={startNewReceipt} disabled={receiptParsing || receiptSaving}>
                            New receipt
                        </button>
                    {/if}
                    <button type="button" class="copilot-inline-btn" on:click={closeReceipts}>
                        Close
                    </button>
                </div>
            </div>

            {#if receiptError}
                <div class="copilot-receipt-error">{receiptError}</div>
            {/if}

            <div class="copilot-receipt-grid">
                <div class="copilot-receipt-upload">
                    <div class="copilot-receipt-drop">
                        <span class="material-symbols-outlined">upload_file</span>
                        <div>
                            <strong>{receiptFile ? receiptFile.name : 'Upload receipt image'}</strong>
                            <p>Images are parsed locally and discarded immediately after the draft is created.</p>
                        </div>
                        <input type="file" accept="image/*" on:change={handleReceiptFile} disabled={receiptParsing || receiptSaving} />
                    </div>
                    <button
                        type="button"
                        class="copilot-primary-btn"
                        on:click={parseSelectedReceipt}
                        disabled={!receiptFile || receiptParsing || receiptSaving}
                    >
                        <span class="material-symbols-outlined text-[14px]" class:copilot-spin={receiptParsing}>
                            {receiptParsing ? 'progress_activity' : 'document_scanner'}
                        </span>
                        {receiptParsing ? 'Parsing…' : 'Parse receipt'}
                    </button>
                    {#if receiptParsing}
                        <div class="copilot-receipt-progress" aria-live="polite">
                            <div class="copilot-receipt-progress-bar">
                                <span style="width: {Math.min(92, 12 + receiptParseElapsed * 4)}%"></span>
                            </div>
                            <div class="copilot-receipt-progress-copy">
                                <strong>{receiptParseStage}</strong>
                                <span>{receiptParseElapsed}s elapsed · local model calls can take 15-30s</span>
                            </div>
                        </div>
                    {/if}
                </div>

                <div class="copilot-receipt-summary">
                    {#if receiptDraft}
                        <div class="copilot-receipt-summary-field">
                            <div class="copilot-receipt-summary-label-row">
                                <span>Store</span>
                                {#if receiptDraft.status === 'draft'}
                                    <button
                                        type="button"
                                        class="copilot-receipt-edit-btn"
                                        on:click={() => receiptSummaryEditing = !receiptSummaryEditing}
                                        aria-label={receiptSummaryEditing ? 'Done editing receipt details' : 'Edit receipt details'}
                                    >
                                        <span class="material-symbols-outlined">{receiptSummaryEditing ? 'check' : 'edit'}</span>
                                    </button>
                                {/if}
                            </div>
                            {#if receiptSummaryEditing && receiptDraft.status === 'draft'}
                                <input
                                    class="copilot-receipt-summary-input"
                                    value={receiptDraft.store_name || ''}
                                    placeholder="Store name"
                                    on:input={(event) => setReceiptDraftValue('store_name', event.currentTarget.value)}
                                />
                            {:else}
                                <strong>{receiptDraft.store_name || 'Unknown store'}</strong>
                            {/if}
                        </div>
                        <div class="copilot-receipt-summary-field">
                            <div class="copilot-receipt-summary-label-row">
                                <span>Date</span>
                            </div>
                            {#if receiptSummaryEditing && receiptDraft.status === 'draft'}
                                <input
                                    class="copilot-receipt-summary-input"
                                    type="date"
                                    value={receiptDraft.receipt_date || ''}
                                    on:input={(event) => setReceiptDraftValue('receipt_date', event.currentTarget.value)}
                                />
                            {:else}
                                <strong>{receiptDraft.receipt_date || 'Unknown'}</strong>
                            {/if}
                        </div>
                        <div>
                            <span>Total</span>
                            <strong>{formatReceiptMoney(receiptDraft.total)}</strong>
                        </div>
                        <div>
                            <span>Status</span>
                            <strong>{receiptDraft.status}</strong>
                        </div>
                    {:else}
                        <div>
                            <span>Approved items</span>
                            <strong>{receiptComparisons.length}</strong>
                        </div>
                        <div>
                            <span>Mode</span>
                            <strong>Review first</strong>
                        </div>
                    {/if}
                </div>
            </div>

            {#if receiptDrafts.length > 0}
                <div class="copilot-receipt-section">
                    <div class="copilot-panel-header">
                        <div>
                            <h3>Draft Receipts</h3>
                            <p>Receipts waiting for review in this profile.</p>
                        </div>
                    </div>
                    <div class="copilot-receipt-draft-list">
                        {#each receiptDrafts as draft}
                            <button
                                type="button"
                                class="copilot-receipt-draft-row"
                                class:copilot-receipt-draft-row-active={receiptDraft?.id === draft.id}
                                on:click={() => loadReceiptDraft(draft.id)}
                            >
                                <span>
                                    <strong>{draft.store_name || 'Unknown store'}</strong>
                                    <small>{formatDate(draft.receipt_date || draft.created_at)} · {draft.item_count || 0} items · {formatReceiptMoney(draft.total)}</small>
                                </span>
                                <span class="material-symbols-outlined">chevron_right</span>
                            </button>
                        {/each}
                    </div>
                </div>
            {/if}

            {#if receiptDraft}
                <div class="copilot-receipt-section">
                    <div class="copilot-panel-header">
                        <div>
                            <h3>Review Parsed Items</h3>
                            <p>Draft rows do not affect comparisons until approved.</p>
                        </div>
                        {#if receiptDraft.status === 'draft'}
                            <div class="copilot-button-row">
                                <button type="button" class="copilot-inline-btn" on:click={saveReceiptItems} disabled={receiptSaving}>
                                    Save draft
                                </button>
                                <button type="button" class="copilot-inline-btn copilot-inline-btn-danger" on:click={discardReceiptDraft} disabled={receiptSaving}>
                                    Discard
                                </button>
                                <button type="button" class="copilot-primary-btn" on:click={approveReceiptDraft} disabled={receiptSaving || receiptItems.length === 0}>
                                    Approve
                                </button>
                            </div>
                        {/if}
                    </div>

                    <div class="copilot-receipt-table-wrap">
                        <table class="copilot-receipt-table">
                            <thead>
                                <tr>
                                    <th>Raw item</th>
                                    <th>Normalized item</th>
                                    <th>Qty</th>
                                    <th>Unit</th>
                                    <th>Total</th>
                                    <th>Unit price</th>
                                    <th>Best seen</th>
                                </tr>
                            </thead>
                            <tbody>
                                {#each receiptItems as item, index (item.id)}
                                    {@const stores = receiptComparisonStores(item.normalized_item_name)}
                                    {@const best = bestReceiptStore(stores)}
                                    <tr>
                                        <td>
                                            <input
                                                value={item.raw_item_text}
                                                on:input={(event) => setReceiptItemValue(index, 'raw_item_text', event.currentTarget.value)}
                                                disabled={receiptDraft.status !== 'draft'}
                                            />
                                        </td>
                                        <td>
                                            <input
                                                value={item.normalized_item_name}
                                                on:input={(event) => setReceiptItemValue(index, 'normalized_item_name', event.currentTarget.value)}
                                                disabled={receiptDraft.status !== 'draft'}
                                            />
                                        </td>
                                        <td>
                                            <input
                                                type="number"
                                                step="0.01"
                                                value={item.quantity ?? ''}
                                                on:input={(event) => setReceiptItemValue(index, 'quantity', event.currentTarget.value)}
                                                disabled={receiptDraft.status !== 'draft'}
                                            />
                                        </td>
                                        <td>
                                            <input
                                                value={item.unit}
                                                on:input={(event) => setReceiptItemValue(index, 'unit', event.currentTarget.value)}
                                                disabled={receiptDraft.status !== 'draft'}
                                            />
                                        </td>
                                        <td>
                                            <input
                                                type="number"
                                                step="0.01"
                                                value={item.total_price ?? ''}
                                                on:input={(event) => setReceiptItemValue(index, 'total_price', event.currentTarget.value)}
                                                disabled={receiptDraft.status !== 'draft'}
                                            />
                                        </td>
                                        <td>
                                            <input
                                                type="number"
                                                step="0.01"
                                                value={item.unit_price ?? ''}
                                                on:input={(event) => setReceiptItemValue(index, 'unit_price', event.currentTarget.value)}
                                                disabled={receiptDraft.status !== 'draft'}
                                            />
                                        </td>
                                        <td>
                                            {#if best}
                                                <span class="copilot-receipt-best">{best.store_name} · {formatUnitPrice(best.lowest_unit_price, item.unit)}</span>
                                            {:else}
                                                <span class="copilot-row-subtitle">No approved history</span>
                                            {/if}
                                        </td>
                                    </tr>
                                {/each}
                            </tbody>
                        </table>
                    </div>
                </div>
            {/if}

            <div class="copilot-receipt-section">
                <div class="copilot-panel-header">
                    <div>
                        <h3>Approved Price History</h3>
                        <p>Lowest and average unit prices from receipts you approved.</p>
                    </div>
                    <button type="button" class="copilot-inline-btn" on:click={loadReceiptComparisons}>Refresh</button>
                </div>
                {#if receiptComparisons.length === 0}
                    <div class="copilot-empty-state">No approved receipt prices yet.</div>
                {:else}
                    <div class="copilot-receipt-comparison-list">
                        {#each receiptComparisons as item}
                            <div class="copilot-receipt-comparison-card">
                                <strong>{item.item_name}</strong>
                                <div class="copilot-receipt-store-row">
                                    {#each item.stores as store}
                                        <span>
                                            {store.store_name || 'Unknown'} · {formatUnitPrice(store.lowest_unit_price)} · avg {formatReceiptMoney(store.average_unit_price)}
                                        </span>
                                    {/each}
                                </div>
                            </div>
                        {/each}
                    </div>
                {/if}
            </div>
        </section>
    {/if}

    <section class="copilot-briefing-surface fade-in-up" aria-label="Mira briefing">
            {#if activeChip}
                {@const chip = chipActions.find(c => c.id === activeChip)}
                <div class="card p-4 fade-in-up">
                    <div class="flex items-center justify-between mb-3">
                        <p class="text-[11px] font-semibold" style="color: var(--text-primary)">{chip.label}</p>
                        <button on:click={() => { activeChip = null; chipFormValues = {}; }}
                            class="text-[11px] hover:underline" style="color: var(--text-muted)">Cancel</button>
                    </div>
                    <div class="flex flex-col gap-2.5">
                        {#each chip.inputs as field, fieldIndex}
                            {@const fieldId = `chip-${chip.id}-${field.key || fieldIndex}`}
                            <div>
                                <label for={fieldId} class="text-[10px] font-medium mb-1 block" style="color: var(--text-muted)">{field.label}</label>
                                {#if field.type === 'select'}
                                    <select id={fieldId} bind:value={chipFormValues[field.key]}
                                        class="w-full px-3 py-2 rounded-lg text-[12px] focus:ring-2 focus:ring-accent/40 outline-none"
                                        style="background: var(--card-bg); color: var(--text-primary); border: 1px solid var(--card-border)">
                                        <option value="">Select category…</option>
                                        {#each categories as cat}
                                            <option value={cat.name ?? cat}>{cat.name ?? cat}</option>
                                        {/each}
                                    </select>
                                {:else}
                                    <input id={fieldId} type="text" bind:value={chipFormValues[field.key]}
                                        placeholder={field.placeholder}
                                        on:keydown={(e) => { if (e.key === 'Enter' && chip.inputs.filter(f => f.required).every(f => chipFormValues[f.key]?.trim())) submitChipForm(); }}
                                        class="w-full px-3 py-2 rounded-lg text-[12px] focus:ring-2 focus:ring-accent/40 outline-none"
                                        style="background: var(--card-bg); color: var(--text-primary); border: 1px solid var(--card-border)" />
                                {/if}
                            </div>
                        {/each}
                        <button on:click={submitChipForm}
                            disabled={!chip.inputs.filter(f => f.required).every(f => chipFormValues[f.key]?.trim())}
                            class="mt-1 px-4 py-2 rounded-lg text-[12px] font-semibold transition-opacity disabled:opacity-30 disabled:cursor-not-allowed"
                            style="background: var(--accent); color: white">
                            Run
                        </button>
                    </div>
                </div>
            {:else}
                {#if advisorReadVisible}
                    <section class:copilot-advisor-read-collapsed={!advisorReadPanelExpanded} class="copilot-advisor-read copilot-advisor-read-featured" aria-label="Mira's read">
                        <div class="copilot-advisor-read-shell-header">
                            <h3>Mira's Read</h3>
                            <button
                                type="button"
                                class="copilot-advisor-shell-toggle"
                                aria-controls="mira-read-body"
                                aria-expanded={advisorReadPanelExpanded}
                                aria-label={advisorReadPanelExpanded ? "Collapse Mira's Read" : "Expand Mira's Read"}
                                on:click={() => advisorReadPanelExpanded = !advisorReadPanelExpanded}
                            >
                                <span class="material-symbols-outlined">{advisorReadPanelExpanded ? 'expand_less' : 'expand_more'}</span>
                            </button>
                        </div>
                        {#if advisorReadPanelExpanded}
                        <div id="mira-read-body" class="copilot-advisor-read-body">
                            {#if advisorReadVisible}
                        <div
                            class="copilot-advisor-read-header copilot-advisor-hero-header"
                            class:copilot-advisor-hero-clickable={advisorRead}
                            role="button"
                            tabindex="0"
                            aria-disabled={!advisorRead}
                            aria-expanded={advisorRead ? advisorReadExpanded : undefined}
                            aria-controls={advisorRead ? 'mira-full-read' : undefined}
                            on:click={toggleAdvisorReadFull}
                            on:keydown={handleAdvisorReadHeroKeydown}
                        >
                            <div class="copilot-advisor-hero-icon" aria-hidden="true">
                                <span class="material-symbols-outlined">receipt_long</span>
                            </div>
                            <div class="copilot-advisor-hero-copy">
                                <div class="copilot-advisor-hero-label">Read focus</div>
                                <h3>
                                    {#if advisorRead}
                                        {advisorReadSummary}
                                    {:else}
                                        {advisorReadTitle}
                                    {/if}
                                </h3>
                                {#if advisorRead}
                                    <span>{advisorReadPreparedLabel}</span>
                                    <small>{advisorReadTitle}</small>
                                {/if}
                            </div>
                            <div class="copilot-advisor-hero-actions">
                                {#if advisorRead}
                                    <div class:copilot-advisor-status-active={advisorReadDelta} class="copilot-advisor-status-chip">
                                        <span class="material-symbols-outlined">{advisorReadDelta ? 'published_with_changes' : 'check_circle'}</span>
                                        <strong>{advisorReadDeltaHeadline}</strong>
                                        <small>{advisorReadDeltaDetail}</small>
                                        {#if advisorReadDelta && advisorReadFollowupEnabled}
                                            <button type="button" disabled={loading} on:click|stopPropagation={() => askAboutAdvisorRead('changes', "What changed since Mira's read?")}>
                                                What changed?
                                            </button>
                                        {/if}
                                    </div>
                                    <button type="button" class="copilot-advisor-open-read" on:click|stopPropagation={toggleAdvisorReadFull}>
                                        {advisorReadExpanded ? 'Close full read' : 'Open full read'}
                                        <span class="material-symbols-outlined">{advisorReadExpanded ? 'unfold_less' : 'unfold_more'}</span>
                                    </button>
                                {/if}
                                <button
                                    type="button"
                                    class="copilot-advisor-refresh"
                                    on:click|stopPropagation={refreshAdvisorRead}
                                    disabled={advisorReadLoading}
                                    aria-label="Check stored Mira read"
                                >
                                    <span class="material-symbols-outlined" class:copilot-spin={advisorReadLoading}>refresh</span>
                                </button>
                            </div>
                        </div>
                            {#if advisorReadLoading}
                                <div class="copilot-advisor-loading">Mira is checking for a stored read.</div>
                            {:else if advisorRead}
                            {#if advisorReadJobRunning || advisorReadGenerating}
                                <div class="copilot-advisor-loading">Mira is generating a fresh read locally. This can take a few minutes.</div>
                            {/if}
                            {#if advisorReadHasCards}
                                <div class="copilot-advisor-priority-grid">
                                    {#each advisorReadPriorityCards as card}
                                        <button
                                            type="button"
                                            class:copilot-advisor-priority-active={advisorReadSelectedCardId === card.id}
                                            class="copilot-advisor-priority-card"
                                            on:click={() => advisorReadSelectedCardId = advisorReadSelectedCardId === card.id ? '' : card.id}
                                        >
                                            <span class="material-symbols-outlined">{card.icon || 'auto_awesome'}</span>
                                            <small>{card.kicker || "Mira's read"}</small>
                                            <strong>{card.title}</strong>
                                            {#if card.summary}
                                                <p>{card.summary}</p>
                                            {/if}
                                            {#if advisorPreviewRows(card, 1)[0]}
                                                <em>{advisorPreviewRows(card, 1)[0].label}{advisorPreviewRows(card, 1)[0].value ? ` · ${advisorPreviewRows(card, 1)[0].value}` : ''}</em>
                                            {/if}
                                        </button>
                                    {/each}
                                </div>
                                {#if advisorReadMoneyMapCard}
                                    <button
                                        type="button"
                                        class:copilot-advisor-money-preview-active={advisorReadSelectedCardId === advisorReadMoneyMapCard.id}
                                        class="copilot-advisor-money-preview"
                                        on:click={() => advisorReadSelectedCardId = advisorReadSelectedCardId === advisorReadMoneyMapCard.id ? '' : advisorReadMoneyMapCard.id}
                                    >
                                        <div>
                                            <span class="material-symbols-outlined">{advisorReadMoneyMapCard.icon || 'route'}</span>
                                            <strong>{advisorReadMoneyMapCard.title || 'Money map'}</strong>
                                            <small>{advisorReadMoneyMapCard.summary}</small>
                                        </div>
                                        {#if advisorReadMoneyMapRows.length}
                                            <div class="copilot-money-map-bar" aria-hidden="true">
                                                {#each advisorReadMoneyMapRows as _}
                                                    <span></span>
                                                {/each}
                                            </div>
                                            <div class="copilot-advisor-money-preview-rows">
                                                {#each advisorReadMoneyMapRows as row}
                                                    <span>
                                                        <i class="material-symbols-outlined">{advisorMoneyMapIcon(row.label)}</i>
                                                        <strong>{row.label}</strong>
                                                        {#if row.value}<em>{row.value}</em>{/if}
                                                    </span>
                                                {/each}
                                            </div>
                                        {/if}
                                    </button>
                                {/if}
                                {#if advisorReadSelectedCard}
                                    <article class="copilot-advisor-detail-panel">
                                        <div class="copilot-advisor-card-head">
                                            <span class="material-symbols-outlined">{advisorReadSelectedCard.icon || 'auto_awesome'}</span>
                                            <div>
                                                <small>{advisorReadSelectedCard.kicker || "Mira's read"}</small>
                                                <h4>{advisorReadSelectedCard.title}</h4>
                                            </div>
                                            <button type="button" aria-label="Close read detail" on:click={() => advisorReadSelectedCardId = ''}>
                                                <span class="material-symbols-outlined">close</span>
                                            </button>
                                        </div>
                                        {#if advisorReadSelectedCard.summary}
                                            <p>{advisorReadSelectedCard.summary}</p>
                                        {/if}
                                        {#if advisorReadSelectedRows.length}
                                            <div class="copilot-advisor-card-rows">
                                                {#each advisorReadSelectedRows as row}
                                                    <div>
                                                        <strong>{row.label}</strong>
                                                        {#if row.value}<span>{row.value}</span>{/if}
                                                        {#if row.detail}<small>{row.detail}</small>{/if}
                                                    </div>
                                                {/each}
                                            </div>
                                        {/if}
                                        {#if advisorReadSelectedCard.detail}
                                            <p class="copilot-advisor-card-detail">{advisorReadSelectedCard.detail}</p>
                                        {/if}
                                        {#if advisorReadSelectedCard.tradeoff}
                                            <p class="copilot-advisor-card-tradeoff">{advisorReadSelectedCard.tradeoff}</p>
                                        {/if}
                                        <div class="copilot-advisor-detail-actions">
                                            {#if advisorReadFollowupEnabled && advisorReadSelectedCard.followup_type}
                                                <button type="button" disabled={loading} on:click={() => askAboutAdvisorCard(advisorReadSelectedCard)}>
                                                    {advisorReadSelectedCard.action_label || 'Ask Mira'}
                                                </button>
                                            {/if}
                                        </div>
                                        {#if appConfig.miraFinancialFeedbackLoopEnabled}
                                            <div class="copilot-advisor-card-feedback" aria-label={`Feedback for ${advisorReadSelectedCard.title || "Mira's read"}`}>
                                                {#if advisorCardFeedbackSummary(advisorReadSelectedCard)}
                                                    <small>{advisorCardFeedbackSummary(advisorReadSelectedCard)}</small>
                                                {/if}
                                                <button type="button" title="More like this" aria-label="More like this" on:click={() => sendAdvisorCardFeedback(advisorReadSelectedCard, 'more_like_this')}>
                                                    <span class="material-symbols-outlined">thumb_up</span>
                                                </button>
                                                <button type="button" title="Less like this" aria-label="Less like this" on:click={() => sendAdvisorCardFeedback(advisorReadSelectedCard, 'less_like_this')}>
                                                    <span class="material-symbols-outlined">thumb_down</span>
                                                </button>
                                                <button type="button" title="Snooze this" aria-label="Snooze this" on:click={() => sendAdvisorCardFeedback(advisorReadSelectedCard, 'snoozed')}>
                                                    <span class="material-symbols-outlined">schedule</span>
                                                </button>
                                                <button type="button" title="Too sensitive" aria-label="Too sensitive" on:click={() => sendAdvisorCardFeedback(advisorReadSelectedCard, 'too_sensitive')}>
                                                    <span class="material-symbols-outlined">visibility_off</span>
                                                </button>
                                                <button type="button" title="Correct this" aria-label="Correct this" on:click={() => startAdvisorCardCorrection(advisorReadSelectedCard)}>
                                                    <span class="material-symbols-outlined">edit_note</span>
                                                </button>
                                            </div>
                                            {#if advisorCorrectionCardId === advisorReadSelectedCard.id}
                                                <div class="copilot-advisor-card-correction">
                                                    <textarea bind:value={advisorCorrectionText} rows="2" placeholder="What should Mira correct?" />
                                                    <div>
                                                        <button type="button" disabled={!advisorCorrectionText.trim()} on:click={() => submitAdvisorCardCorrection(advisorReadSelectedCard)}>
                                                            Save
                                                        </button>
                                                        <button type="button" on:click={cancelAdvisorCardCorrection}>
                                                            Cancel
                                                        </button>
                                                    </div>
                                                </div>
                                            {/if}
                                        {/if}
                                    </article>
                                {/if}
                            {/if}
                            {#if !advisorReadHasCards || advisorReadExpanded}
                                <div class="copilot-advisor-memo" id="mira-full-read">
                                    {#each visibleAdvisorReadBlocks as block}
                                        {#if block.type === 'heading'}
                                            <h4>{block.text}</h4>
                                        {:else if block.type === 'bullet'}
                                            <p class="copilot-advisor-bullet">{block.text}</p>
                                        {:else if block.type === 'table'}
                                            <div class="copilot-advisor-table-wrap">
                                                <table class="copilot-advisor-table">
                                                    <thead>
                                                        <tr>
                                                            {#each block.headers as header}
                                                                <th>{header}</th>
                                                            {/each}
                                                        </tr>
                                                    </thead>
                                                    <tbody>
                                                        {#each block.rows as row}
                                                            <tr>
                                                                {#each row as cell}
                                                                    <td>{cell}</td>
                                                                {/each}
                                                            </tr>
                                                        {/each}
                                                    </tbody>
                                                </table>
                                            </div>
                                        {:else}
                                            <p>{block.text}</p>
                                        {/if}
                                    {/each}
                                </div>
                            {/if}
                            <div class="copilot-advisor-actions">
                                {#if advisorReadFollowupEnabled && !advisorReadHasCards}
                                    <button type="button" disabled={loading} on:click={() => askAboutAdvisorRead('focus', "What should I focus on from Mira's read, and what should I not overreact to?")}>
                                        <span class="material-symbols-outlined">psychology</span>
                                        Focus
                                    </button>
                                    <button type="button" disabled={loading} on:click={() => askAboutAdvisorRead('levers', "What can I reduce a little without pain from Mira's read?")}>
                                        <span class="material-symbols-outlined">tune</span>
                                        Levers
                                    </button>
                                    <button type="button" disabled={loading} on:click={() => askAboutAdvisorRead('risk', "What is the biggest risk to my goals from Mira's read?")}>
                                        <span class="material-symbols-outlined">flag</span>
                                        Risk
                                    </button>
                                {/if}
                                {#if advisorReadBlocks.length > visibleAdvisorReadBlocks.length || advisorReadExpanded || advisorReadHasCards}
                                    <button type="button" on:click={() => advisorReadExpanded = !advisorReadExpanded}>
                                        <span class="material-symbols-outlined">{advisorReadExpanded ? 'unfold_less' : 'unfold_more'}</span>
                                        {advisorReadExpanded ? 'Less' : 'Full read'}
                                    </button>
                                {/if}
                                {#if advisorReadCanGenerate}
                                    <button type="button" on:click={() => generateAdvisorRead(true)} disabled={advisorReadGenerating || advisorReadJobRunning}>
                                        <span class="material-symbols-outlined">auto_awesome</span>
                                        Fresh read
                                    </button>
                                {/if}
                            </div>
                            {:else}
                                <div class="copilot-advisor-empty">
                                    <span class="material-symbols-outlined">hourglass_empty</span>
                                    <div>
                                        <p>Mira can prepare your financial read locally. It can take a few minutes, and this chat will only show it after a validated memo is stored.</p>
                                        {#if advisorReadJobRunning || advisorReadGenerating}
                                            <small>Mira is generating it now.</small>
                                        {:else if advisorReadCanGenerate}
                                            <button type="button" on:click={() => generateAdvisorRead(true)}>
                                                <span class="material-symbols-outlined">auto_awesome</span>
                                                Generate read
                                            </button>
                                        {/if}
                                    </div>
                                </div>
                            {/if}
                            {/if}
                        </div>
                        {/if}
                    </section>
                {/if}
                {#if !miraDemoPreviewOnly}
                    <p class="copilot-starter-label">Start here</p>
                    <div class="copilot-starter-row">
                        {#each starterChips as chip}
                            <button on:click={() => activateChip(chip)} class="copilot-suggestion-btn">
                                {chip.label}
                            </button>
                        {/each}
                    </div>
                {/if}
            {/if}
    </section>

    <div class="copilot-chat-layout fade-in-up" class:copilot-chat-layout-empty={messages.length <= 1}>
        <section class="copilot-chat-shell" class:copilot-chat-shell-empty={messages.length <= 1}>
            <div class="copilot-chat-panel-header">
                <div>
                    <h3>Ask Mira</h3>
                    <span>
                        <span class="material-symbols-outlined">hub</span>
                        {selectedCopilotModelMeta?.label || copilotModel || 'Gemma'} · local · private
                    </span>
                </div>
                <small>Briefing stays above while chatting</small>
            </div>
            <div bind:this={chatContainer} class="copilot-chat-feed flex-1 overflow-y-auto space-y-3.5" style="scrollbar-width: thin">
                {#each messages as msg, i}
                    <div class="flex {msg.role === 'user' ? 'justify-end' : 'justify-start'} fade-in" style="animation-delay: {Math.min(i * 40, 240)}ms">
                        <div class="max-w-[90%] {msg.role === 'user' ? 'order-2' : ''}" class:w-full={msg.role === 'assistant' && (msg.chart || (msg.data && msg.data.length > 0))}>
                            {#if msg.role === 'assistant'}
                                    <div class="flex items-start gap-2.5" class:copilot-wide-row={msg.chart || (msg.data && msg.data.length > 0)} class:copilot-welcome-message={msg.is_welcome}>
                                    {#if !msg.is_welcome}
                                        <span class="mira-mark mira-mark--avatar mt-0.5" aria-hidden="true"></span>
                                    {/if}
                                    <div class="copilot-msg-container" class:copilot-wide-content={msg.chart || (msg.data && msg.data.length > 0)}>
                                        {#if msg.operation === 'write_preview'}
                                            <div class="copilot-op-badge copilot-op-write">
                                                <span class="material-symbols-outlined text-[12px]">edit</span>
                                                Write Preview · {msg.rows_affected} row{msg.rows_affected !== 1 ? 's' : ''}
                                            </div>
                                        {:else if msg.operation === 'write_executed' || msg.operation === 'success'}
                                            <div class="copilot-op-badge copilot-op-success">
                                                <span class="material-symbols-outlined text-[12px]">check_circle</span>
                                                Completed
                                            </div>
                                        {:else if msg.operation === 'read' && msg.rows_affected > 0}
                                            <div class="copilot-op-badge copilot-op-read">
                                                <span class="material-symbols-outlined text-[12px]">search</span>
                                                Query · {msg.rows_total > msg.rows_affected ? `${msg.rows_affected} of ${msg.rows_total}` : msg.rows_affected} result{(msg.rows_total || msg.rows_affected) !== 1 ? 's' : ''}
                                            </div>
                                        {:else if msg.operation === 'error'}
                                            <div class="copilot-op-badge copilot-op-error">
                                                <span class="material-symbols-outlined text-[12px]">error</span>
                                                Error
                                            </div>
                                        {/if}

                                        {#if msg.active_tool}
                                            <div class="copilot-op-badge copilot-op-read" style="margin-bottom: 6px; opacity: 0.9;">
                                                <span class="material-symbols-outlined text-[12px] animate-spin">progress_activity</span>
                                                Calling {msg.active_tool.replaceAll('_', ' ')}…
                                            </div>
                                        {:else if msg.progress && !msg.content?.trim()}
                                            <div class="copilot-op-badge copilot-op-read" style="margin-bottom: 6px; opacity: 0.9;">
                                                <span class="material-symbols-outlined text-[12px] animate-spin">progress_activity</span>
                                                {msg.progress}…
                                            </div>
                                        {/if}

                                        {#if msg.content?.trim() || msg.operation === 'streaming'}
                                            <div class:card={!msg.is_welcome} class:copilot-welcome-card={msg.is_welcome} class="copilot-message-card">
                                                <p class="text-[13px] leading-relaxed whitespace-pre-wrap">{msg.content}{#if msg.operation === 'streaming' && loading}<span class="copilot-cursor">▌</span>{/if}</p>
                                            </div>
                                        {/if}

                                        {#if msg.chart && msg.chart.labels && msg.chart.labels.length > 0}
                                            <CopilotChart spec={msg.chart} />
                                        {/if}

                                        {#if msg.preview_changes && msg.preview_changes.length > 0}
                                            <div class="copilot-change-list">
                                                {#each msg.preview_changes as change}
                                                    <span class="copilot-change-chip">
                                                        {change.column}: {formatTableValue(change.column, change.new_value)}
                                                    </span>
                                                {/each}
                                            </div>
                                        {/if}

                                        {#if msg.data && msg.data.length > 0}
                                            {@const columns = getColumns(msg.data)}
                                            <div class="copilot-data-table-wrap">
                                                <table class="copilot-data-table">
                                                    <thead>
                                                        <tr>
                                                            {#each columns as col}
                                                                <th>{col.replace(/_/g, ' ')}</th>
                                                            {/each}
                                                        </tr>
                                                    </thead>
                                                    <tbody>
                                                        {#each msg.data as row}
                                                            <tr>
                                                                {#each columns as col}
                                                                    <td>{formatTableValue(col, row[col])}</td>
                                                                {/each}
                                                            </tr>
                                                        {/each}
                                                    </tbody>
                                                </table>
                                            </div>
                                        {/if}

                                        {#if msg.needs_confirmation && !msg.confirmed}
                                            <div class="copilot-confirm-card">
                                                <p class="text-[11px] font-medium mb-3" style="color: var(--text-secondary)">
                                                    This will modify your data. Review the proposed changes and confirm.
                                                </p>
                                                <div class="flex items-center gap-2">
                                                    <button class="copilot-confirm-btn copilot-confirm-yes" on:click={() => confirmWrite(i)} disabled={loading}>
                                                        <span class="material-symbols-outlined text-[14px]">check</span>
                                                        Confirm
                                                    </button>
                                                    <button class="copilot-confirm-btn copilot-confirm-no" on:click={() => cancelWrite(i)} disabled={loading}>
                                                        <span class="material-symbols-outlined text-[14px]">close</span>
                                                        Cancel
                                                    </button>
                                                </div>
                                            </div>
                                        {:else if msg.confirmed}
                                            <div class="copilot-confirmed-badge">
                                                <span class="material-symbols-outlined text-[12px]">check_circle</span>
                                                Confirmed & executed
                                            </div>
                                        {/if}

                                        {#if showMiraDebug && msg.sql}
                                            <button class="copilot-sql-toggle" on:click={() => toggleSql(i)}>
                                                <span class="material-symbols-outlined text-[12px]">code</span>
                                                {showSqlForMsg[i] ? 'Hide SQL' : 'Show SQL'}
                                            </button>
                                            {#if showSqlForMsg[i]}
                                                <pre class="copilot-sql-block">{msg.sql}</pre>
                                            {/if}
                                        {/if}

                                        {#if chatReceipt(msg)}
                                            <button class="copilot-sql-toggle" on:click={() => showSqlForMsg = { ...showSqlForMsg, ['receipt_' + i]: !showSqlForMsg['receipt_' + i] }}>
                                                <span class="material-symbols-outlined text-[12px]">receipt_long</span>
                                                {showSqlForMsg['receipt_' + i] ? 'Hide receipts' : 'Receipts'}
                                            </button>
                                            {#if showSqlForMsg['receipt_' + i]}
                                                <div class="copilot-sql-block" style="font-size: 11px; line-height: 1.6;">
                                                    {#each chatReceipt(msg).lines as line}
                                                        <div>{line}</div>
                                                    {/each}
                                                    {#each chatReceipt(msg).caveats as caveat}
                                                        <div><strong>Caveat</strong>: {caveat}</div>
                                                    {/each}
                                                </div>
                                            {/if}
                                        {/if}

                                        {#if showMiraDebug && ((msg.tool_trace && msg.tool_trace.length > 0) || msg.trace) && msg.operation !== 'streaming'}
                                            <button class="copilot-sql-toggle" on:click={() => showSqlForMsg = { ...showSqlForMsg, ['trace_' + i]: !showSqlForMsg['trace_' + i] }}>
                                                <span class="material-symbols-outlined text-[12px]">manage_search</span>
                                                {showSqlForMsg['trace_' + i] ? 'Hide debug trace' : 'Debug trace'}{#if msg.tool_trace && msg.tool_trace.length > 0} ({msg.tool_trace.length} tool{msg.tool_trace.length !== 1 ? 's' : ''}){/if}
                                            </button>
                                            {#if showSqlForMsg['trace_' + i]}
                                                <div class="copilot-sql-block" style="font-size: 11px; line-height: 1.6;">
                                                    {#if showMiraDebug && msg.trace}
                                                        <div><strong>Runtime</strong>: {msg.runtime || msg.trace.runtime || 'current'}</div>
                                                        {#if msg.answer_guard?.path || msg.trace.answer_path}
                                                            <div><strong>Answer path</strong>: {msg.answer_guard?.path || msg.trace.answer_path}</div>
                                                        {/if}
                                                        {#if msg.trace.selector_ms != null}
                                                            <div><strong>Selector</strong>: {msg.trace.selector_ms}ms</div>
                                                        {/if}
                                                        {#if msg.trace.prompt_tokens_est != null}
                                                            <div><strong>Prompt</strong>: {msg.trace.prompt_tokens_est} est. tokens{#if msg.trace.manifest_tokens_est != null} · manifest {msg.trace.manifest_tokens_est}{/if}</div>
                                                        {/if}
                                                    {/if}
                                                    {#if msg.tool_trace && msg.tool_trace.length > 0}
                                                        {#each msg.tool_trace as t}
                                                            <div>→ <strong>{t.name}</strong>({Object.entries(t.args || {}).map(([k,v]) => `${k}=${JSON.stringify(v)}`).join(', ')}){t.duration_ms != null ? ` · ${t.duration_ms}ms` : ''}</div>
                                                        {/each}
                                                    {/if}
                                                </div>
                                            {/if}
                                        {/if}

                                        {#if msg.content && msg.operation && msg.operation !== 'error' && msg.operation !== 'streaming' && !msg.needs_confirmation}
                                            <button
                                                class="copilot-sql-toggle"
                                                on:click={() => saveInsight(i)}
                                                disabled={msg.saving || msg.saved}
                                                title="Extract a takeaway from this turn and add to your persistent memory"
                                            >
                                                <span class="material-symbols-outlined text-[12px]">
                                                    {msg.saved ? 'check' : 'bookmark_add'}
                                                </span>
                                                {msg.saved ? 'Saved' : msg.saving ? 'Saving…' : 'Save to memory'}
                                            </button>
                                        {/if}

                                        {#if msg.memory_proposals && msg.memory_proposals.length > 0}
                                            <div class="copilot-memory-proposals">
                                                {#each msg.memory_proposals as prop (prop.id)}
                                                    <div class="copilot-memory-proposal">
                                                        <div class="copilot-memory-proposal-head">
                                                            <span class="material-symbols-outlined text-[14px]">lightbulb</span>
                                                            <span>I'd like to remember this in <strong>{prop.section.replace('_', ' ')}</strong>:</span>
                                                        </div>
                                                        <div class="copilot-memory-proposal-body">{prop.body}</div>
                                                        {#if prop.evidence}
                                                            <div class="copilot-memory-proposal-evidence">↳ {prop.evidence}</div>
                                                        {/if}
                                                        <div class="copilot-memory-proposal-actions">
                                                            <button class="copilot-sql-toggle" on:click={() => acceptMemoryProposal(i, prop.id)}>
                                                                <span class="material-symbols-outlined text-[12px]">check</span>Add
                                                            </button>
                                                            <button class="copilot-sql-toggle" on:click={() => rejectMemoryProposal(i, prop.id)}>
                                                                <span class="material-symbols-outlined text-[12px]">close</span>Skip
                                                            </button>
                                                        </div>
                                                    </div>
                                                {/each}
                                            </div>
                                        {/if}

                                        {#if msg.suggested_memory}
                                            <div class="copilot-memory-proposals">
                                                <div class="copilot-memory-proposal">
                                                    <div class="copilot-memory-proposal-head">
                                                        <span class="material-symbols-outlined text-[14px]">bookmark_add</span>
                                                        <span>Remember this?</span>
                                                    </div>
                                                    <div class="copilot-memory-proposal-body">{msg.suggested_memory.text}</div>
                                                    {#if msg.suggested_memory.reason}
                                                        <div class="copilot-memory-proposal-evidence">↳ {msg.suggested_memory.reason}</div>
                                                    {/if}
                                                    <div class="copilot-memory-proposal-actions">
                                                        <button
                                                            class="copilot-sql-toggle"
                                                            on:click={() => acceptSuggestedMemory(i)}
                                                            disabled={msg.suggested_memory.saving}
                                                        >
                                                            <span class="material-symbols-outlined text-[12px]">check</span>
                                                            {msg.suggested_memory.saving ? 'Adding…' : 'Remember'}
                                                        </button>
                                                        <button
                                                            class="copilot-sql-toggle"
                                                            on:click={() => dismissSuggestedMemory(i)}
                                                            disabled={msg.suggested_memory.saving}
                                                        >
                                                            <span class="material-symbols-outlined text-[12px]">close</span>Skip
                                                        </button>
                                                    </div>
                                                </div>
                                            </div>
                                        {/if}
                                    </div>
                                </div>
                            {:else}
                                <div class="px-4 py-2.5 rounded-2xl rounded-br-md text-[13px] copilot-user-bubble">
                                    {msg.content}
                                </div>
                            {/if}
                        </div>
                    </div>
                {/each}

                {#if loading}
                    <div class="flex items-start gap-2.5 fade-in">
                        <div class="w-6 h-6 rounded-lg flex items-center justify-center flex-shrink-0" style="background: var(--accent-soft)">
                            <span class="material-symbols-outlined text-[14px] animate-spin" style="color: var(--accent)">progress_activity</span>
                        </div>
                        <div class="card" style="padding: 0.75rem 1rem">
                            <div class="flex items-center gap-1.5">
                                <div class="w-1.5 h-1.5 rounded-full animate-bounce" style="background: var(--copilot-typing-dot); opacity: 0.65; animation-delay: 0ms"></div>
                                <div class="w-1.5 h-1.5 rounded-full animate-bounce" style="background: var(--copilot-typing-dot); opacity: 0.65; animation-delay: 150ms"></div>
                                <div class="w-1.5 h-1.5 rounded-full animate-bounce" style="background: var(--copilot-typing-dot); opacity: 0.65; animation-delay: 300ms"></div>
                            </div>
                        </div>
                    </div>
                {/if}
            </div>

            {#if messages.length <= 1}
                <div class="copilot-empty-chat-spacer" aria-hidden="true"></div>
            {/if}

            <div class="copilot-composer-wrap flex-shrink-0">
                <div class="copilot-composer-row">
                    <div class="flex-1 relative">
                        <textarea bind:value={input} on:keydown={handleKeydown}
                            placeholder={miraDemoPreviewOnly ? "Mira is visible in the demo; chat is available in local mode." : "Ask Mira about your money, code, ideas, or changes to your app data…"}
                            rows="1"
                            disabled={miraDemoPreviewOnly}
                            class="w-full px-4 py-3 text-[13px] resize-none transition-all copilot-composer-textarea"></textarea>
                        {#if localLlmStatus?.provider === 'ollama'}
                            <div class="copilot-model-inline">
                                <span class="copilot-mini-badge">Model</span>
                                <div class="copilot-model-dropdown-wrapper">
                                    {#if localLlmStatus?.expertMode}
                                        <button
                                            type="button"
                                            class="copilot-model-trigger"
                                            aria-haspopup="listbox"
                                            aria-expanded={modelDropdownOpen}
                                            on:click|stopPropagation={() => modelDropdownOpen = !modelDropdownOpen}
                                            disabled={copilotModelSaving}
                                        >
                                            <span>{copilotModelLabel}</span>
                                            <span class="material-symbols-outlined">expand_more</span>
                                        </button>
                                        {#if modelDropdownOpen}
                                            <button type="button" class="month-dropdown-backdrop" aria-label="Close model picker" on:click={() => modelDropdownOpen = false}></button>
                                            <div class="copilot-model-menu" role="listbox" tabindex="-1">
                                                {#each localLlmCatalog?.tiers || [] as tier}
                                                    <div class="copilot-model-group">{tier.label}</div>
                                                    {#each tier.models.filter((model) => model.task_fit?.includes('copilot')) as model}
                                                        <button
                                                            type="button"
                                                            class="copilot-model-option"
                                                            class:copilot-model-option-active={copilotModel === model.id}
                                                            role="option"
                                                            aria-selected={copilotModel === model.id}
                                                            on:click={() => selectCopilotModel(model)}
                                                        >
                                                            <span>{model.label} · {model.download_size_gb || model.approx_size_gb} GB{model.quantization ? ` · ${model.quantization}` : ''}{model.installed ? ' · installed' : ''}</span>
                                                            {#if copilotModel === model.id}
                                                                <span class="material-symbols-outlined">check</span>
                                                            {/if}
                                                        </button>
                                                    {/each}
                                                {/each}
                                            </div>
                                        {/if}
                                    {:else}
                                        <div class="copilot-model-trigger" aria-label="Current Mira model">
                                            <span>{copilotModelLabel}</span>
                                        </div>
                                    {/if}
                                </div>
                                <span class="copilot-model-meta">
                                    {selectedCopilotModelMeta?.installed ? 'Installed' : 'Not installed'}
                                </span>
                                {#if selectedCopilotModelMeta && !selectedCopilotModelMeta.installed && localLlmStatus?.ollamaReachable}
                                    <button
                                        type="button"
                                        class="copilot-model-install"
                                        on:click={installCopilotModel}
                                        disabled={copilotModelInstalling}
                                    >
                                        {copilotModelInstalling ? 'Installing…' : 'Install'}
                                    </button>
                                {/if}
                            </div>
                        {/if}
                    </div>
                    {#if loading}
                        <button on:click={stopStream} class="copilot-send-btn" title="Stop (Esc)">
                            <span class="material-symbols-outlined text-white text-[18px]">stop</span>
                        </button>
                    {:else}
                        <button on:click={send} disabled={!input.trim() || miraDemoPreviewOnly} class="copilot-send-btn">
                            <span class="material-symbols-outlined text-white text-[18px]">arrow_upward</span>
                        </button>
                    {/if}
                </div>
                <p class="text-[9px] text-center mt-2" style="color: var(--text-muted)">
                    {miraDemoPreviewOnly ? 'Mira is shown in demo mode without connecting to a local model.' : 'Mira can explain decisions, draft safe changes for your approval, and keep the conversation private.'}
                </p>
            </div>
        </section>
    </div>

    {#if historyOpen}
        <div class="copilot-history-overlay fade-in" role="presentation">
            <button
                type="button"
                class="copilot-history-backdrop"
                aria-label="Close history"
                on:click={closeHistory}></button>

            <aside class="copilot-history-drawer" role="dialog" aria-modal="true" aria-label="Recent Mira Activity">
                <div class="copilot-history-drawer-header">
                    <div class="copilot-panel-header">
                        <div>
                            <h3>Recent Mira Activity</h3>
                            <p>Reuse a recent prompt or inspect Mira's tool path.</p>
                        </div>
                        {#if recentHistory.length > 0}
                            <button type="button" class="copilot-inline-btn" on:click={clearHistory} disabled={sidebarLoading}>Clear</button>
                        {/if}
                    </div>
                    <button type="button" class="copilot-history-close" on:click={closeHistory} aria-label="Close history">
                        <span class="material-symbols-outlined text-[18px]">close</span>
                    </button>
                </div>

                <div class="copilot-history-drawer-body">
                    {#if sidebarLoading}
                        <div class="copilot-empty-state">Loading recent activity…</div>
                    {:else if recentHistory.length === 0}
                        <div class="copilot-empty-state">No recent Mira history yet.</div>
                    {:else}
                        <div class="copilot-history-list">
                            {#each recentHistory as item}
                                <div class="copilot-history-card">
                                    <div class="flex items-start justify-between gap-3">
                                        <div>
                                            <p class="copilot-row-title">{item.user_message}</p>
                                            <p class="copilot-row-subtitle">{item.operation_type} · {formatDateTime(item.created_at)}</p>
                                        </div>
                                        <div class="copilot-history-actions">
                                            <button class="copilot-inline-btn" type="button" on:click={() => reuseHistoryPrompt(item)}>Reuse</button>
                                            <button class="copilot-history-delete" type="button" on:click={() => deleteHistoryItem(item)} aria-label="Remove history item">
                                                <span class="material-symbols-outlined text-[14px]">close</span>
                                            </button>
                                        </div>
                                    </div>
                                    {#if item.generated_sql}
                                        <button class="copilot-sql-toggle" on:click={() => toggleHistorySql(item.id)}>
                                            <span class="material-symbols-outlined text-[12px]">code</span>
                                            {showSqlForHistory[item.id] ? 'Hide SQL' : 'Show SQL'}
                                        </button>
                                        {#if showSqlForHistory[item.id]}
                                            <pre class="copilot-sql-block">{item.generated_sql}</pre>
                                        {/if}
                                    {/if}
                                </div>
                            {/each}
                        </div>
                    {/if}
                </div>
            </aside>
        </div>
    {/if}
</div>
