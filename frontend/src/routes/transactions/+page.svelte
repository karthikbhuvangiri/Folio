<script>
    import '$lib/styles/transactions.css';
    import { onMount } from 'svelte';
    import { api, invalidateCache } from '$lib/api.js';
    import { activeProfile } from '$lib/stores/profileStore.js';
    import {
        formatCurrency, formatDate, formatDayHeader,
        getCurrentMonth, formatMonth,
        groupTransactionsByDate, CATEGORY_COLORS, CATEGORY_ICONS
    } from '$lib/utils.js';
    import ProfileSwitcher from '$lib/components/ProfileSwitcher.svelte';

    let transactions = [];
    let summaryTransactions = [];
    let historyTransactions = [];
    let totalCount = 0;
    let pageLimit = 50;
    let pageOffset = 0;
    let allCategories = [];
    let loading = true;
    let profileSwitching = false;
    let search = '';
    let filterMonth = getCurrentMonth();
    let filterCategory = '';
    let filterAccount = '';
    let reviewFilter = 'all';
    let editingTxId = null;
    let editingMerchantTxId = null;
    let merchantDraftName = '';
    let merchantEditError = '';
    let selectedTxId = null;
    let metadataDrafts = {};
    let splitDrafts = {};
    let savingMetadataFor = null;
    let savingSplitsFor = null;
    let savingMerchantFor = null;
    let exportingCsv = false;
    let bulkReviewing = false;
    let reviewConfirmOpen = false;
    let pendingBulkReview = null;
    let months = [];
    let accountNames = [];

    // —— Period selector state (mirrored from Dashboard) ——
    let selectedPeriod = 'this_month';
    let selectedCustomMonth = getCurrentMonth();
    let monthDropdownOpen = false;

    const periodOptions = [
        { key: 'this_month', label: 'This Month' },
        { key: 'last_month', label: 'Last Month' },
        { key: 'ytd',        label: 'YTD' },
        { key: 'custom',     label: 'Custom' },
        { key: 'all',        label: 'All Time' }
    ];
    $: activePeriodIdx = Math.max(periodOptions.findIndex(p => p.key === selectedPeriod), 0);

    function getMonthForPeriod(period) {
        switch (period) {
            case 'this_month': return getCurrentMonth();
            case 'last_month': {
                const now = new Date();
                const lm = new Date(now.getFullYear(), now.getMonth() - 1, 1);
                return `${lm.getFullYear()}-${String(lm.getMonth() + 1).padStart(2, '0')}`;
            }
            case 'custom': return selectedCustomMonth;
            default: return null; // 'all' and 'ytd' handled in filter
        }
    }

    function handlePeriodChange(key) {
        selectedPeriod = key;
        // Sync filterMonth based on period selection
        if (key === 'this_month' || key === 'last_month') {
            filterMonth = getMonthForPeriod(key);
        } else if (key === 'custom') {
            filterMonth = selectedCustomMonth;
        } else if (key === 'ytd') {
            // filterMonth will be handled in the reactive filter below
            filterMonth = '__ytd__';
        } else {
            // 'all'
            filterMonth = '';
        }
    }

    function handleCustomMonthSelect(m) {
        selectedCustomMonth = m;
        monthDropdownOpen = false;
        selectedPeriod = 'custom';
        filterMonth = m;
    }

    // —— Custom filter dropdown state ——
    let monthPickerOpen = false;
    let categoryPickerOpen = false;
    let accountPickerOpen = false;
    let categoryFilterSearch = '';


    function openFilter(which) {
        monthPickerOpen    = which === 'month';
        categoryPickerOpen = which === 'category';
        accountPickerOpen  = which === 'account';
        if (which !== 'category') categoryFilterSearch = '';
    }

    function closeAllFilters() {
        monthPickerOpen = false;
        categoryPickerOpen = false;
        accountPickerOpen = false;
        categoryFilterSearch = '';
    }

    function handleWindowClick() {
        closeAllFilters();
        monthDropdownOpen = false;
        // Close category re-tag state fully so the editing border does not linger.
        if (catDropdownOpenForTx || editingTxId) {
            cancelEditing();
        }
        if (editingMerchantTxId && !savingMerchantFor) {
            cancelMerchantEditing();
        }
    }

    // New category creation
    let creatingNewCategory = false;
    let newCategoryName = '';
    let newCategoryError = '';

    // ── Category re-tag dropdown state ──
    let catDropdownOpenForTx = null;   // original_id of tx whose dropdown is open
    let catDropdownSearch = '';         // search/filter within the dropdown
    let categoryApplyMode = 'always';

    // Recategorization feedback
    let recentlyUpdatedTxId = null;
    let updateFeedback = '';

    // Subscription declaration prompt state
    let subscriptionPromptTxId = null;
    let subscriptionPromptMerchant = '';
    let subscriptionPromptAmount = 0;
    let subscriptionPromptFrequency = '';
    let subscriptionPromptCategory = 'Subscriptions';
    let subscriptionDeclareLoading = false;

    const frequencyOptions = [
        { key: 'monthly', label: 'Monthly' },
        { key: 'quarterly', label: 'Quarterly' },
        { key: 'annual', label: 'Annual' },
    ];

    async function handleDeclareSubscription(frequency) {
        if (!subscriptionPromptMerchant || subscriptionDeclareLoading) return;
        subscriptionDeclareLoading = true;
        try {
            const profile = $activeProfile && $activeProfile !== 'household' ? $activeProfile : null;
            await api.declareSubscription(subscriptionPromptMerchant, subscriptionPromptAmount, frequency, profile, subscriptionPromptCategory);
            updateFeedback = `✓ Tracking ${subscriptionPromptMerchant} as ${frequency} ${subscriptionPromptCategory.toLowerCase()}`;
            recentlyUpdatedTxId = subscriptionPromptTxId;
            setTimeout(() => {
                if (recentlyUpdatedTxId === subscriptionPromptTxId) {
                    recentlyUpdatedTxId = null;
                    updateFeedback = '';
                }
            }, 4000);
        } catch (e) {
            console.error('Failed to declare subscription:', e);
            updateFeedback = 'Failed to declare subscription';
            setTimeout(() => { updateFeedback = ''; }, 3000);
        } finally {
            subscriptionDeclareLoading = false;
            dismissSubscriptionPrompt();
        }
    }

    function dismissSubscriptionPrompt() {
        subscriptionPromptTxId = null;
        subscriptionPromptMerchant = '';
        subscriptionPromptAmount = 0;
        subscriptionPromptFrequency = '';
        subscriptionPromptCategory = 'Subscriptions';
    }

    function ensureMetadataDraft(tx) {
        if (!tx?.original_id) return { notes: '', tags: '', reviewed: false };
        if (!metadataDrafts[tx.original_id]) {
            metadataDrafts = {
                ...metadataDrafts,
                [tx.original_id]: {
                    notes: tx.notes || '',
                    tags: Array.isArray(tx.tags) ? tx.tags.join(', ') : (tx.tags || ''),
                    reviewed: !!tx.reviewed
                }
            };
        }
        return metadataDrafts[tx.original_id];
    }

    async function saveTransactionMetadata(tx) {
        const draft = ensureMetadataDraft(tx);
        const willMarkReviewed = !tx.reviewed && !!draft.reviewed;
        savingMetadataFor = tx.original_id;
        try {
            const tags = String(draft.tags || '').split(',').map(t => t.trim()).filter(Boolean);
            const result = await api.updateTransactionMetadata(tx.original_id, {
                notes: draft.notes || '',
                tags,
                reviewed: !!draft.reviewed
            });
            const updated = result.transaction || {};
            const savedReviewed = updated.reviewed != null ? !!updated.reviewed : !!draft.reviewed;
            transactions = transactions.map(item => item.original_id === tx.original_id
                ? { ...item, notes: updated.notes || '', tags: updated.tags || tags, reviewed: savedReviewed }
                : item);
            summaryTransactions = summaryTransactions.map(item => item.original_id === tx.original_id
                ? { ...item, notes: updated.notes || '', tags: updated.tags || tags, reviewed: savedReviewed }
                : item);
            if (willMarkReviewed && savedReviewed) {
                await resetFiltersAfterReview();
            }
            updateFeedback = 'Transaction details saved';
            recentlyUpdatedTxId = tx.original_id;
            setTimeout(() => { updateFeedback = ''; recentlyUpdatedTxId = null; }, 2500);
        } catch (e) {
            console.error('Failed to save transaction metadata:', e);
            updateFeedback = 'Failed to save transaction details';
        } finally {
            savingMetadataFor = null;
        }
    }

    function ensureSplitDraft(tx) {
        if (!tx?.original_id) return { loading: false, rows: [] };
        if (!splitDrafts[tx.original_id]) {
            splitDrafts = {
                ...splitDrafts,
                [tx.original_id]: {
                    loading: false,
                    rows: [{ category: tx.category || 'Uncategorized', amount: Math.abs(parseFloat(tx.amount || 0)).toFixed(2), notes: '' }]
                }
            };
        }
        return splitDrafts[tx.original_id];
    }

    function toggleTransactionDetails(tx, isSelected) {
        if (isSelected) {
            selectedTxId = null;
            return;
        }

        selectedTxId = tx.original_id;
        const draft = ensureSplitDraft(tx);
        if (!draft.loaded && !draft.loading) {
            splitDrafts = {
                ...splitDrafts,
                [tx.original_id]: { ...draft, loading: true }
            };
            loadTransactionSplits(tx);
        }
    }

    async function loadTransactionSplits(tx) {
        try {
            const result = await api.getTransactionSplits(tx.original_id);
            const currentDraft = splitDrafts[tx.original_id] || {};
            if (currentDraft.dirty) {
                splitDrafts = {
                    ...splitDrafts,
                    [tx.original_id]: {
                        ...currentDraft,
                        loading: false,
                        loaded: true
                    }
                };
                return;
            }
            const rows = (result.items || []).map(item => ({
                category: item.category || tx.category || 'Uncategorized',
                amount: Math.abs(parseFloat(item.amount || 0)).toFixed(2),
                notes: item.notes || ''
            }));
            splitDrafts = {
                ...splitDrafts,
                [tx.original_id]: {
                    loading: false,
                    loaded: true,
                    rows: rows.length > 0
                        ? rows
                        : [{ category: tx.category || 'Uncategorized', amount: Math.abs(parseFloat(tx.amount || 0)).toFixed(2), notes: '' }]
                }
            };
        } catch (e) {
            console.error('Failed to load transaction splits:', e);
            splitDrafts = {
                ...splitDrafts,
                [tx.original_id]: {
                    ...(splitDrafts[tx.original_id] || {}),
                    loading: false,
                    loaded: true
                }
            };
        }
    }

    function updateSplitField(txId, index, field, value) {
        const draft = splitDrafts[txId];
        if (!draft) return;
        const rows = draft.rows.map((row, rowIndex) => rowIndex === index ? { ...row, [field]: value } : row);
        splitDrafts = { ...splitDrafts, [txId]: { ...draft, dirty: true, rows } };
    }

    function fillSingleBlankSplitRemainder(tx) {
        const draft = splitDrafts[tx.original_id];
        if (!draft) return;

        const blankIndexes = draft.rows
            .map((row, index) => ({ row, index }))
            .filter(({ row }) => String(row.amount ?? '').trim() === '')
            .map(({ index }) => index);

        if (blankIndexes.length !== 1) return;

        const blankIndex = blankIndexes[0];
        const assigned = draft.rows.reduce((sum, row, index) => {
            if (index === blankIndex) return sum;
            return sum + Math.abs(parseFloat(row.amount || 0));
        }, 0);
        const remainder = Math.max(0, Math.abs(parseFloat(tx.amount || 0)) - assigned);
        const rows = draft.rows.map((row, index) => index === blankIndex
            ? { ...row, amount: remainder > 0 ? remainder.toFixed(2) : '' }
            : row);

        splitDrafts = { ...splitDrafts, [tx.original_id]: { ...draft, dirty: true, rows } };
    }

    function addSplitRow(tx) {
        const draft = ensureSplitDraft(tx);
        splitDrafts = {
            ...splitDrafts,
            [tx.original_id]: {
                ...draft,
                loading: false,
                dirty: true,
                rows: [...draft.rows, { category: tx.category || 'Uncategorized', amount: '', notes: '' }]
            }
        };
    }

    function removeSplitRow(txId, index) {
        const draft = splitDrafts[txId];
        if (!draft || draft.rows.length <= 1) return;
        splitDrafts = {
            ...splitDrafts,
            [txId]: { ...draft, dirty: true, rows: draft.rows.filter((_, rowIndex) => rowIndex !== index) }
        };
    }

    function getSplitTotal(txId) {
        return (splitDrafts[txId]?.rows || []).reduce((sum, row) => sum + Math.abs(parseFloat(row.amount || 0)), 0);
    }

    function getSplitDelta(tx) {
        return Math.abs(parseFloat(tx?.amount || 0)) - getSplitTotal(tx.original_id);
    }

    function getSplitDeltaFromDraft(tx, draft) {
        const total = (draft?.rows || []).reduce((sum, row) => sum + Math.abs(parseFloat(row.amount || 0)), 0);
        return Math.abs(parseFloat(tx?.amount || 0)) - total;
    }

    function getSplitTotalFromDraft(draft) {
        return (draft?.rows || []).reduce((sum, row) => sum + Math.abs(parseFloat(row.amount || 0)), 0);
    }

    async function saveTransactionSplits(tx) {
        const draft = ensureSplitDraft(tx);
        savingSplitsFor = tx.original_id;
        try {
            const rows = draft.rows
                .map(row => ({
                    category: row.category || 'Uncategorized',
                    amount: Math.abs(parseFloat(row.amount || 0)),
                    notes: row.notes || '',
                    tags: []
                }))
                .filter(row => row.category && row.amount > 0);
            const result = await api.updateTransactionSplits(tx.original_id, rows);
            splitDrafts = {
                ...splitDrafts,
                [tx.original_id]: {
                    loading: false,
                    loaded: true,
                    dirty: false,
                    rows: (result.items || rows).map(item => ({
                        category: item.category || tx.category || 'Uncategorized',
                        amount: Math.abs(parseFloat(item.amount || 0)).toFixed(2),
                        notes: item.notes || ''
                    }))
                }
            };
            invalidateCache();
            updateFeedback = 'Transaction split saved';
            recentlyUpdatedTxId = tx.original_id;
            setTimeout(() => { updateFeedback = ''; recentlyUpdatedTxId = null; }, 2500);
        } catch (e) {
            console.error('Failed to save transaction splits:', e);
            updateFeedback = 'Failed to save transaction split';
        } finally {
            savingSplitsFor = null;
        }
    }

    async function fetchTransactionHistory() {
        let offset = 0;
        let all = [];
        let expectedTotal = null;

        do {
            const result = await api.getTransactions({ limit: 1000, offset });
            const page = result.data || [];
            all = all.concat(page);
            expectedTotal = result.total_count ?? all.length;
            offset += page.length;

            if (page.length === 0) break;
        } while (offset < expectedTotal);

        historyTransactions = all;
        return all;
    }

    function buildVisibleParams(limit = pageLimit, offset = pageOffset) {
        const params = { limit, offset };
        if (filterMonth === '__ytd__') {
            params.limit = 1000;
            params.offset = 0;
        } else if (filterMonth) {
            params.month = filterMonth;
        }
        if (filterCategory) params.category = filterCategory;
        if (filterAccount) params.account = filterAccount;
        if (reviewFilter !== 'all') params.reviewed = reviewFilter === 'reviewed';
        if (search) params.search = search;
        return params;
    }

    async function refreshTransactionMetadata() {
        const allTxns = await fetchTransactionHistory();
        const monthSet = new Set(allTxns.map(t => t.date?.substring(0, 7)).filter(Boolean));
        months = [...monthSet].sort().reverse();
        if (months.length > 0 && selectedCustomMonth === getCurrentMonth()) {
            selectedCustomMonth = months[0];
        }
        const accSet = new Set(allTxns.map(t => t.account_name).filter(Boolean));
        accountNames = [...accSet].sort();
        return allTxns;
    }

    onMount(async () => {
        const query = new URLSearchParams(window.location.search);
        const requestedPeriod = query.get('period');
        const requestedMonth = query.get('month');
        if (periodOptions.some(option => option.key === requestedPeriod)) {
            handlePeriodChange(requestedPeriod);
        } else if (/^\d{4}-\d{2}$/.test(requestedMonth || '')) {
            selectedCustomMonth = requestedMonth;
            handlePeriodChange('custom');
        }

        const requestedReview = query.get('review');
        if (requestedReview === 'reviewed' || requestedReview === 'unreviewed') {
            reviewFilter = requestedReview;
        }

        const handleSyncComplete = async (event) => {
            const detail = event?.detail || {};
            if (detail.status && detail.status !== 'completed') return;
            if (!['enrollment', 'manual-sync', 'simplefin'].includes(detail.source)) return;

            try {
                invalidateCache();
                await fetchTransactions();
                await fetchSummaryTransactions();

                const allTxns = await fetchTransactionHistory();
                const monthSet = new Set(allTxns.map(t => t.date?.substring(0, 7)).filter(Boolean));
                months = [...monthSet].sort().reverse();
                accountNames = [...new Set(allTxns.map(t => t.account_name).filter(Boolean))].sort();
            } catch (e) {
                console.error('Failed to refresh transactions after sync:', e);
            }
        };

        window.addEventListener('folio:sync-complete', handleSyncComplete);
        try {
            const initialParams = buildVisibleParams(pageLimit, 0);
            const [result, cats] = await Promise.all([
                api.getTransactions(initialParams),
                api.getCategories()
            ]);
            transactions = result.data;
            totalCount = result.total_count;
            pageOffset = 0;
            allCategories = cats;
            summaryTransactions = result.data || [];
            _prevFilterKey = filterKey;
            _prevSearch = search;
            loading = false;

            refreshTransactionMetadata().catch(e => {
                console.error('Failed to load transaction metadata:', e);
            });
            fetchSummaryTransactions();
        } catch (e) {
            console.error('Failed to load transactions:', e);
            loading = false;
        }

        return () => {
            window.removeEventListener('folio:sync-complete', handleSyncComplete);
        };
    });

    async function fetchTransactions() {
        try {
            const params = buildVisibleParams(pageLimit, pageOffset);
            const result = await api.getTransactions(params);
            transactions = result.data;
            totalCount = result.total_count;

            // For YTD, do client-side year filter on the result
            if (filterMonth === '__ytd__') {
                const year = new Date().getFullYear().toString();
                transactions = transactions.filter(t => t.date?.startsWith(year));
                totalCount = transactions.length;
            }
        } catch (e) {
            console.error('Failed to fetch transactions:', e);
        }
    }

    function buildSummaryParams(offset = 0) {
        const params = { limit: 1000, offset };
        if (filterMonth && filterMonth !== '__ytd__') params.month = filterMonth;
        if (filterCategory) params.category = filterCategory;
        if (filterAccount) params.account = filterAccount;
        if (reviewFilter !== 'all') params.reviewed = reviewFilter === 'reviewed';
        if (search) params.search = search;
        return params;
    }

    async function fetchSummaryTransactions() {
        try {
            let offset = 0;
            let all = [];
            let expectedTotal = null;

            do {
                const result = await api.getTransactions(buildSummaryParams(offset));
                const page = result.data || [];
                all = all.concat(page);
                expectedTotal = result.total_count ?? all.length;
                offset += page.length;

                if (page.length === 0) break;
            } while (offset < expectedTotal);

            if (filterMonth === '__ytd__') {
                const year = new Date().getFullYear().toString();
                all = all.filter(t => t.date?.startsWith(year));
            }

            summaryTransactions = all;
        } catch (e) {
            console.error('Failed to fetch transaction summary data:', e);
            summaryTransactions = filteredTxns;
        }
    }

    function goToPage(page) {
        pageOffset = page * pageLimit;
        fetchTransactions();
        // Scroll to top of transactions list
        window.scrollTo({ top: 0, behavior: 'smooth' });
    }

    function nextPage() {
        if (pageOffset + pageLimit < totalCount) {
            pageOffset += pageLimit;
            fetchTransactions();
            window.scrollTo({ top: 0, behavior: 'smooth' });
        }
    }

    function prevPage() {
        if (pageOffset > 0) {
            pageOffset = Math.max(0, pageOffset - pageLimit);
            fetchTransactions();
            window.scrollTo({ top: 0, behavior: 'smooth' });
        }
    }

    $: currentPage = Math.floor(pageOffset / pageLimit);
    $: totalPages = Math.ceil(totalCount / pageLimit);

    // Server-side filtering: re-fetch when filters change
    let _prevFilterKey = '';
    let _prevSearch = '';
    let _searchDebounce = null;
    $: filterKey = `${filterMonth}|${filterCategory}|${filterAccount}|${reviewFilter}`;

    $: {
        if (!loading && filterKey !== _prevFilterKey) {
            _prevFilterKey = filterKey;
            pageOffset = 0;
            fetchTransactions();
            fetchSummaryTransactions();
        }
    }

    // Debounced search (separate from other filters since it changes per keystroke)
    $: if (!loading && search !== _prevSearch) {
        _prevSearch = search;
        if (_searchDebounce) clearTimeout(_searchDebounce);
        _searchDebounce = setTimeout(() => {
            pageOffset = 0;
            fetchTransactions();
            fetchSummaryTransactions();
        }, 300);
    }

    // Transactions are already server-filtered
    $: filteredTxns = transactions;
    $: summaryTxns = summaryTransactions.length > 0 ? summaryTransactions : filteredTxns;

    // Transfer types excluded from accrual-basis totals (same model as dashboard)
    const EXCLUDED_EXPENSE_TYPES = new Set(['transfer_internal', 'transfer_cc_payment', 'transfer_household']);
    const NON_SPENDING_CATEGORIES = new Set(['Savings Transfer', 'Personal Transfer', 'Credit Card Payment', 'Cash Withdrawal', 'Cash Deposit', 'Investment Transfer', 'Income', 'Credits & Refunds']);
    const NON_MERCHANT_KINDS = new Set(['personal_transfer', 'credit_card_payment', 'income', 'tax', 'bank_fee']);
    const TITLE_CASE_SMALL_WORDS = new Set(['and', 'of', 'the', 'to', 'for', 'by', 'at', 'in']);

    $: groupedTxns = groupTransactionsByDate(filteredTxns);
    $: totalSpending = summaryTxns
        .filter(t => {
            const amount = parseFloat(t.amount);
            if (amount >= 0) return false;
            // Exclude non-spending categories (same as dashboard)
            if (NON_SPENDING_CATEGORIES.has(t.category)) return false;
            // Exclude internal and CC payment transfers
            if (t.expense_type && EXCLUDED_EXPENSE_TYPES.has(t.expense_type)) return false;
            return true;
        })
        .reduce((s, t) => s + Math.abs(parseFloat(t.amount)), 0);
    $: totalIncome = summaryTxns
        .filter(t => {
            const amount = parseFloat(t.amount);
            if (amount <= 0) return false;
            // Only count actual income
            if (t.category !== 'Income') return false;
            // Exclude internal transfers showing as income
            if (t.expense_type && EXCLUDED_EXPENSE_TYPES.has(t.expense_type)) return false;
            return true;
        })
        .reduce((s, t) => s + parseFloat(t.amount), 0);
    $: txCcRepaid = summaryTxns
        .filter(t => t.category === 'Credit Card Payment' && parseFloat(t.amount) < 0)
        .reduce((s, t) => s + Math.abs(parseFloat(t.amount)), 0);
    $: txExternalTransfers = summaryTxns
        .filter(t => t.expense_type === 'transfer_external' && parseFloat(t.amount) < 0)
        .reduce((s, t) => s + Math.abs(parseFloat(t.amount)), 0);
    $: txIncomingTransfers = summaryTxns
        .filter(t => t.expense_type === 'transfer_external' && parseFloat(t.amount) > 0)
        .reduce((s, t) => s + parseFloat(t.amount), 0);
    $: txCashDeposits = summaryTxns
        .filter(t => t.category === 'Cash Deposit' && parseFloat(t.amount) > 0)
        .reduce((s, t) => s + parseFloat(t.amount), 0);
    $: txCashWithdrawals = summaryTxns
        .filter(t => t.category === 'Cash Withdrawal' && parseFloat(t.amount) < 0)
        .reduce((s, t) => s + Math.abs(parseFloat(t.amount)), 0);
    $: txInvestmentInflows = summaryTxns
        .filter(t => t.category === 'Investment Transfer' && parseFloat(t.amount) > 0)
        .reduce((s, t) => s + parseFloat(t.amount), 0);
    $: txInvestmentOutflows = summaryTxns
        .filter(t => t.category === 'Investment Transfer' && parseFloat(t.amount) < 0)
        .reduce((s, t) => s + Math.abs(parseFloat(t.amount)), 0);
    $: txCreditsRefunds = summaryTxns
        .filter(t => {
            const amount = parseFloat(t.amount);
            if (amount <= 0) return false;
            if (t.category === 'Income') return false;
            if (t.category === 'Credits & Refunds') return true;
            if (NON_SPENDING_CATEGORIES.has(t.category)) return false;
            if (t.expense_type && EXCLUDED_EXPENSE_TYPES.has(t.expense_type)) return false;
            return true;
        })
        .reduce((s, t) => s + parseFloat(t.amount), 0);
    $: txNetFlow = totalIncome + txCreditsRefunds + txIncomingTransfers + txCashDeposits + txInvestmentInflows - totalSpending - txExternalTransfers - txCashWithdrawals - txInvestmentOutflows;
    $: largestSpendTx = summaryTxns
        .filter(t => parseFloat(t.amount) < 0)
        .filter(t => !NON_SPENDING_CATEGORIES.has(t.category))
        .filter(t => !(t.expense_type && EXCLUDED_EXPENSE_TYPES.has(t.expense_type)))
        .sort((a, b) => Math.abs(parseFloat(b.amount)) - Math.abs(parseFloat(a.amount)))[0] || null;
    $: largestSpendAmount = largestSpendTx ? Math.abs(parseFloat(largestSpendTx.amount)) : 0;
    $: periodLabel = selectedPeriod === 'all'
        ? 'All time'
        : selectedPeriod === 'ytd'
            ? `YTD ${new Date().getFullYear()}`
            : selectedPeriod === 'custom'
                ? formatMonth(selectedCustomMonth)
                : formatMonth(getMonthForPeriod(selectedPeriod));
    $: storyKicker = selectedPeriod === 'this_month' || selectedPeriod === 'custom'
        ? `${periodLabel} · so far`
        : periodLabel;
    $: dailySpendBars = groupTransactionsByDate(summaryTxns).map(([date, txns]) => {
        const spent = txns
            .filter(t => parseFloat(t.amount) < 0)
            .filter(t => !NON_SPENDING_CATEGORIES.has(t.category))
            .filter(t => !(t.expense_type && EXCLUDED_EXPENSE_TYPES.has(t.expense_type)))
            .reduce((s, t) => s + Math.abs(parseFloat(t.amount)), 0);
        return { date, spent };
    }).reverse().slice(-24);
    $: maxDailySpend = Math.max(...dailySpendBars.map(d => d.spent), 1);


    async function updateCategory(txId, newCategory, oneOff = false) {
        try {
            const result = await api.updateCategory(txId, newCategory, oneOff);
            const tx = transactions.find(t => t.original_id === txId);
            if (tx) {
                tx.category = newCategory;
                tx.confidence = 'manual';
                tx.categorization_source = 'user';
                transactions = transactions;
            }

            // Check if backend suggests subscription tracking
            if (result && result.subscription_prompt) {
                subscriptionPromptTxId = txId;
                subscriptionPromptMerchant = result.merchant || tx?.description || '';
                subscriptionPromptAmount = result.amount || Math.abs(parseFloat(tx?.amount || 0));
                subscriptionPromptCategory = result.category || newCategory || 'Subscriptions';
            }

            // Show feedback — one-off gets a distinct message
            recentlyUpdatedTxId = txId;
            const retro = result?.retroactive_count ?? 0;
            updateFeedback = oneOff
                ? `Categorized as "${newCategory}" — this transaction only`
                : retro > 0
                    ? `Categorized as "${newCategory}" — updated ${retro} similar transaction${retro !== 1 ? 's' : ''}`
                    : `Categorized as "${newCategory}"`;

            // Invalidate cache since category rules may have changed
            invalidateCache();

            // Refresh transactions list so retroactively updated rows are visible immediately
            try {
                await fetchTransactions();
            } catch (_) {}

            // Refresh categories list in case a new one was created
            try {
                allCategories = await api.getCategories();
            } catch (_) {}

            // Clear feedback after a delay
            setTimeout(() => {
                if (recentlyUpdatedTxId === txId) {
                    recentlyUpdatedTxId = null;
                    updateFeedback = '';
                }
            }, 4000);
        } catch (e) {
            console.error('Failed to update category:', e);
            updateFeedback = 'Failed to update category';
            setTimeout(() => { updateFeedback = ''; }, 3000);
        }
        categoryApplyMode = 'always';
        editingTxId = null;
        creatingNewCategory = false;
        newCategoryName = '';
    }

    async function createAndApplyCategory(txId) {
        const name = newCategoryName.trim();
        if (!name) {
            newCategoryError = 'Category name cannot be empty';
            return;
        }

        // Check if it already exists (case-insensitive)
        if (allCategories.some(c => c.toLowerCase() === name.toLowerCase())) {
            const existing = allCategories.find(c => c.toLowerCase() === name.toLowerCase());
            creatingNewCategory = false;
            newCategoryName = '';
            newCategoryError = '';
            if (existing) {
                await updateCategory(txId, existing, categoryApplyMode === 'once');
            }
            return;
        }

        try {
            await api.createCategory(name);
            allCategories = [...allCategories, name].sort();
            creatingNewCategory = false;
            newCategoryName = '';
            await updateCategory(txId, name, categoryApplyMode === 'once');
        } catch (e) {
            newCategoryError = 'Failed to create category';
            console.error(e);
        }
    }

    function startEditing(txId) {
        editingTxId = txId;
        catDropdownOpenForTx = txId;
        catDropdownSearch = '';
        categoryApplyMode = 'always';
        creatingNewCategory = false;
        newCategoryName = '';
        newCategoryError = '';
    }

    function cancelEditing() {
        editingTxId = null;
        catDropdownOpenForTx = null;
        catDropdownSearch = '';
        creatingNewCategory = false;
        newCategoryName = '';
        newCategoryError = '';
        categoryApplyMode = 'always';
    }

    function startMerchantEditing(tx) {
        const cell = getMerchantCell(tx);
        if (!cell.editable) return;
        editingMerchantTxId = tx.original_id;
        merchantDraftName = cell.label || '';
        merchantEditError = '';
    }

    function cancelMerchantEditing() {
        editingMerchantTxId = null;
        merchantDraftName = '';
        merchantEditError = '';
        savingMerchantFor = null;
    }

    async function saveMerchantAlias(tx) {
        const merchantKey = getEditableMerchantKey(tx);
        const profileId = getMerchantProfile(tx);
        const nextName = merchantDraftName.trim();
        if (!merchantKey || !profileId) {
            merchantEditError = 'Merchant key missing';
            return;
        }
        if (!nextName) {
            merchantEditError = 'Merchant name required';
            return;
        }

        savingMerchantFor = tx.original_id;
        try {
            const result = await api.updateMerchantDirectory(merchantKey, {
                profile_id: profileId,
                clean_name: nextName
            });
            invalidateCache();
            const updatedName = result?.merchant?.clean_name || nextName;
            const normalizedKey = merchantKey.toUpperCase().trim();
            const matchesMerchant = (item) =>
                getMerchantProfile(item) === profileId &&
                getEditableMerchantKey(item).toUpperCase().trim() === normalizedKey;
            const applyAlias = (item) => matchesMerchant(item)
                ? { ...item, merchant_display_name: updatedName, merchant_display_source: 'user' }
                : item;

            transactions = transactions.map(applyAlias);
            summaryTransactions = summaryTransactions.map(applyAlias);
            historyTransactions = historyTransactions.map(applyAlias);
            updateFeedback = `Merchant saved: ${updatedName}`;
            recentlyUpdatedTxId = tx.original_id;
            setTimeout(() => { updateFeedback = ''; recentlyUpdatedTxId = null; }, 2500);
            cancelMerchantEditing();
        } catch (e) {
            console.error('Failed to save merchant alias:', e);
            merchantEditError = 'Failed to save merchant';
        } finally {
            savingMerchantFor = null;
        }
    }

    // Filtered category list for the re-tag dropdown search
    $: filteredEditCategories = catDropdownSearch
        ? allCategories.filter(c => c.toLowerCase().includes(catDropdownSearch.toLowerCase()))
        : allCategories;
    $: filteredFilterCategories = categoryFilterSearch
        ? allCategories.filter(c => c.toLowerCase().includes(categoryFilterSearch.toLowerCase()))
        : allCategories;

    function clearFilters() {
        search = '';
        selectedPeriod = 'this_month';
        filterMonth = getCurrentMonth();
        selectedCustomMonth = getCurrentMonth();
        filterCategory = '';
        filterAccount = '';
        reviewFilter = 'all';
        pageOffset = 0;
        if (typeof window !== 'undefined') {
            const url = new URL(window.location.href);
            const hadFilterParams = ['review', 'period', 'month'].some(param => url.searchParams.has(param));
            if (hadFilterParams) {
                url.searchParams.delete('review');
                url.searchParams.delete('period');
                url.searchParams.delete('month');
                window.history.replaceState(window.history.state, '', `${url.pathname}${url.search}${url.hash}`);
            }
        }
        // fetchTransactions() will be triggered by the reactive filter block
    }

    async function resetFiltersAfterReview() {
        clearFilters();
        closeAllFilters();
        monthDropdownOpen = false;
        await Promise.all([fetchTransactions(), fetchSummaryTransactions()]);
    }

    $: hasActiveFilters = search || filterCategory || filterAccount || reviewFilter !== 'all' || selectedPeriod !== 'this_month';

    async function exportCurrentTransactions() {
        exportingCsv = true;
        try {
            const params = buildSummaryParams(0);
            delete params.limit;
            delete params.offset;
            const result = await api.exportTransactions(params);
            const blob = new Blob([result.csv || ''], { type: 'text/csv;charset=utf-8' });
            const url = URL.createObjectURL(blob);
            const link = document.createElement('a');
            link.href = url;
            link.download = result.filename || 'folio-transactions.csv';
            document.body.appendChild(link);
            link.click();
            link.remove();
            URL.revokeObjectURL(url);
            updateFeedback = `Exported ${result.row_count || 0} transaction${result.row_count === 1 ? '' : 's'}`;
            setTimeout(() => { updateFeedback = ''; }, 2500);
        } catch (e) {
            console.error('Failed to export transactions:', e);
            updateFeedback = 'Failed to export transactions';
        } finally {
            exportingCsv = false;
        }
    }

    async function markFilteredReviewed() {
        if (bulkReviewing || totalCount <= 0) return;
        const label = reviewFilter === 'unreviewed' ? 'unreviewed' : 'filtered';
        const params = buildSummaryParams(0);
        delete params.limit;
        delete params.offset;
        if (reviewFilter === 'all') params.reviewed = false;
        pendingBulkReview = {
            count: totalCount,
            label,
            scope: describeBulkReviewScope(),
            params
        };
        reviewConfirmOpen = true;
    }

    function describeBulkReviewScope() {
        const parts = [periodLabel];
        if (filterCategory) parts.push(filterCategory);
        if (filterAccount) parts.push(filterAccount);
        if (search.trim()) parts.push(`"${search.trim()}"`);
        return parts.join(' · ');
    }

    function cancelBulkReviewConfirm() {
        if (bulkReviewing) return;
        reviewConfirmOpen = false;
        pendingBulkReview = null;
    }

    function handleReviewConfirmKeydown(e) {
        if (!reviewConfirmOpen || bulkReviewing) return;
        if (e.key === 'Escape') cancelBulkReviewConfirm();
    }

    async function confirmFilteredReviewed() {
        if (bulkReviewing || !pendingBulkReview) return;
        bulkReviewing = true;
        try {
            const params = { ...(pendingBulkReview.params || {}) };
            const result = await api.bulkReviewTransactions(params, true);
            invalidateCache();
            await resetFiltersAfterReview();
            updateFeedback = `Marked ${(result.updated_count || 0).toLocaleString()} transaction${result.updated_count === 1 ? '' : 's'} reviewed`;
            setTimeout(() => { updateFeedback = ''; }, 3000);
        } catch (e) {
            console.error('Failed to bulk review transactions:', e);
            updateFeedback = 'Failed to mark transactions reviewed';
        } finally {
            bulkReviewing = false;
            reviewConfirmOpen = false;
            pendingBulkReview = null;
        }
    }

    function formatDayHeaderFull(dateStr) {
        const base = formatDayHeader(dateStr);
        if ((selectedPeriod === 'all' || selectedPeriod === 'ytd') && base !== 'Today' && base !== 'Yesterday' && dateStr) {
            return `${base}, ${dateStr.substring(0, 4)}`;
        }
        return base;
    }

    function formatLedgerDay(dateStr) {
        if (!dateStr) return { day: '', meta: '', relative: '' };
        const d = new Date(dateStr + 'T00:00:00');
        const today = new Date();
        today.setHours(0, 0, 0, 0);
        const target = new Date(d);
        target.setHours(0, 0, 0, 0);
        const diffDays = Math.round((today - target) / 86400000);
        const relative = diffDays === 0
            ? 'Today'
            : diffDays === 1
                ? 'Yesterday'
                : diffDays > 1
                    ? `${diffDays}d ago`
                    : '';

        return {
            day: d.toLocaleDateString('en-US', { day: '2-digit' }),
            meta: d.toLocaleDateString('en-US', { month: 'short', weekday: 'long' }),
            relative
        };
    }

    function titleCaseMerchant(value) {
        const raw = (value || '').trim();
        if (!raw) return '';
        if (raw !== raw.toUpperCase()) return raw;
        return raw
            .toLowerCase()
            .split(/\s+/)
            .map((word, index) => {
                if (index > 0 && TITLE_CASE_SMALL_WORDS.has(word)) return word;
                if (/^#?\d+$/.test(word)) return word;
                return word.charAt(0).toUpperCase() + word.slice(1);
            })
            .join(' ');
    }

    function getMerchantDisplay(tx) {
        const preferred = tx.merchant_display_name || tx.merchant_name || tx.counterparty_name || '';
        const raw = tx.description || '';
        let label = preferred || raw;
        label = label
            .replace(/\s+(INC|LLC|CORP|CO)\.?$/i, '')
            .replace(/\s{2,}/g, ' ')
            .trim();
        return titleCaseMerchant(label || 'Transaction');
    }

    function getRawDescriptor(tx) {
        return (tx.description || tx.raw_description || tx.merchant_name || '').trim();
    }

    function getTransactionTitle(tx) {
        return titleCaseMerchant(getRawDescriptor(tx) || 'Transaction');
    }

    function getMerchantProfile(tx) {
        return (tx.profile || tx.profile_id || '').trim();
    }

    function getEditableMerchantKey(tx) {
        const kind = (tx.merchant_kind || '').trim().toLowerCase();
        if (NON_MERCHANT_KINDS.has(kind)) return '';
        return (tx.merchant_key || tx.merchant_name || tx.merchant_display_key || getRawDescriptor(tx)).trim();
    }

    function getMerchantKindLabel(tx) {
        const kind = (tx.merchant_kind || '').trim().toLowerCase();
        if (kind === 'personal_transfer') return 'Transfer';
        if (kind === 'credit_card_payment') return 'Payment';
        if (kind === 'income') return 'Income';
        if (kind === 'tax') return 'Tax';
        if (kind === 'bank_fee') return 'Fee';
        return '';
    }

    function getMerchantSourceLabel(tx, editable) {
        if (!editable) return null;

        const displaySource = (tx.merchant_display_source || '').trim().toLowerCase();
        const source = displaySource || (tx.merchant_source || '').trim().toLowerCase();
        const confidence = (tx.merchant_confidence || '').trim().toLowerCase();
        const kind = (tx.merchant_kind || '').trim().toLowerCase();

        if (source === 'user') return { label: 'User', type: 'user' };
        if (source === 'llm' || source === 'local_llm' || source === 'ai') return { label: 'AI', type: 'ai' };
        if (kind === 'unknown' || confidence === 'low' || source === 'unknown') return { label: 'Review', type: 'review' };
        if (source === 'raw' || source === 'fallback') return { label: 'Raw', type: 'raw' };
        if (source) return { label: 'Auto', type: 'auto' };
        return { label: 'Auto', type: 'auto' };
    }

    function getMerchantCell(tx) {
        const editableKey = getEditableMerchantKey(tx);
        if (editableKey) {
            return {
                label: getMerchantDisplay(tx),
                sublabel: tx.merchant_display_industry || tx.merchant_source || tx.merchant_confidence || 'Merchant',
                source: getMerchantSourceLabel(tx, true),
                editable: true,
                key: editableKey
            };
        }

        const kindLabel = getMerchantKindLabel(tx);
        return {
            label: kindLabel || 'Unresolved',
            sublabel: kindLabel ? 'Non-merchant' : 'Needs review',
            source: null,
            editable: false,
            key: ''
        };
    }

    function getMerchantKey(tx) {
        return (tx.merchant_display_key || tx.merchant_key || getMerchantDisplay(tx))
            .toLowerCase()
            .replace(/[^a-z0-9]+/g, ' ')
            .trim();
    }

    function getAccountSuffix(tx) {
        return tx.account_last4 || tx.last4 || tx.account_mask || '';
    }

    function hashString(value) {
        return [...(value || '')].reduce((hash, char) => ((hash << 5) - hash + char.charCodeAt(0)) | 0, 0);
    }

    function getAccountHue(tx) {
        const palette = ['#f0aa64', '#7dd3fc', '#a78bfa', '#34d399', '#f472b6', '#f87171'];
        return palette[Math.abs(hashString(tx.account_name || 'account')) % palette.length];
    }

    function getRowSignal(tx) {
        const amount = Math.abs(parseFloat(tx.amount || 0));
        if (tx.expense_type && EXCLUDED_EXPENSE_TYPES.has(tx.expense_type)) return null;
        if (amount >= 500) return 'Large';
        return null;
    }

    function getMerchantStats(tx) {
        const key = getMerchantKey(tx);
        const sourceTxns = historyTransactions.length > 0 ? historyTransactions : summaryTxns;
        const byId = new Map();
        sourceTxns
            .filter(item => getMerchantKey(item) === key)
            .forEach(item => byId.set(item.original_id || `${item.date}-${item.description}-${item.amount}`, item));
        byId.set(tx.original_id || `${tx.date}-${tx.description}-${tx.amount}`, tx);

        const matches = [...byId.values()].sort((a, b) => (b.date || '').localeCompare(a.date || ''));
        const spentMatches = matches.filter(item => parseFloat(item.amount) < 0);
        const average = spentMatches.length
            ? spentMatches.reduce((s, item) => s + Math.abs(parseFloat(item.amount || 0)), 0) / spentMatches.length
            : 0;
        const amount = Math.abs(parseFloat(tx.amount || 0));
        const delta = average ? ((amount - average) / average) * 100 : 0;
        const recent = matches.filter(item => item.original_id !== tx.original_id).slice(0, 3);
        const categoryCounts = matches.reduce((counts, item) => {
            const category = item.category || 'Uncategorized';
            counts[category] = (counts[category] || 0) + 1;
            return counts;
        }, {});
        const dominantCategory = Object.entries(categoryCounts)
            .sort((a, b) => b[1] - a[1])[0] || [tx.category || 'Uncategorized', 1];
        const monthsBack = [];
        const base = tx.date ? new Date(tx.date + 'T00:00:00') : new Date();
        for (let i = 5; i >= 0; i -= 1) {
            const d = new Date(base.getFullYear(), base.getMonth() - i, 1);
            const month = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
            const total = spentMatches
                .filter(item => item.date?.startsWith(month))
                .reduce((s, item) => s + Math.abs(parseFloat(item.amount || 0)), 0);
            monthsBack.push({
                month,
                label: d.toLocaleDateString('en-US', { month: 'short' }),
                total
            });
        }

        const activeMonths = monthsBack.filter(month => month.total > 0);
        const looksMonthly = spentMatches.length >= 3 && activeMonths.length >= 3 && Math.abs(delta) <= 15;
        const firstSeen = matches[matches.length - 1]?.date || '';
        const lastSeen = matches[0]?.date || '';

        return {
            visits: matches.length,
            average,
            delta,
            recent,
            months: monthsBack,
            dominantCategory: dominantCategory[0],
            dominantCategoryCount: dominantCategory[1],
            looksMonthly,
            firstSeen,
            lastSeen
        };
    }

    function getMerchantMetaLine(tx) {
        const description = (tx.description || '').trim();
        const merchantDisplay = (tx.merchant_display_name || '').trim();
        const merchantName = (tx.merchant_name || '').trim();
        const accountName = (tx.account_name || '').trim();
        const parts = [];

        const merchantLabel = merchantDisplay || merchantName;
        if (merchantLabel && merchantLabel.toUpperCase() !== description.toUpperCase()) {
            parts.push(merchantLabel);
        }
        if (accountName && accountName !== merchantLabel) {
            parts.push(accountName);
        }

        return parts.join(' · ');
    }

    /**
     * Get a display label for the categorization source.
     */
    function getSourceLabel(tx) {
        const source = tx.categorization_source || '';
        const confidence = tx.confidence || '';

        if (source === 'user' || confidence === 'manual') return { label: 'Manual', type: 'manual' };
        if (source === 'user-rule') return { label: 'Auto-rule', type: 'auto-rule' };
        if (source === 'rule-high') return { label: 'Rule', type: 'rule' };
        if (source === 'llm') return { label: 'AI', type: 'ai' };
        if (source === 'fallback') return { label: 'Fallback', type: 'fallback' };
        return null;
    }

    // Profile switch: reload transactions
    let _prevTxProfile = null;
    $: if ($activeProfile && $activeProfile !== _prevTxProfile) {
        if (_prevTxProfile !== null) {
            reloadTransactionsForProfile();
        }
        _prevTxProfile = $activeProfile;
    }

    async function reloadTransactionsForProfile() {
        profileSwitching = true;
        try {
            const [result, cats, metaResult] = await Promise.all([
                api.getTransactions({ limit: pageLimit, offset: 0 }),
                api.getCategories(),
                fetchTransactionHistory()
            ]);
            transactions = result.data;
            totalCount = result.total_count;
            pageOffset = 0;
            allCategories = cats;

            const allTxns = metaResult;
            const monthSet = new Set(allTxns.map(t => t.date?.substring(0, 7)).filter(Boolean));
            months = [...monthSet].sort().reverse();
            const accSet = new Set(allTxns.map(t => t.account_name).filter(Boolean));
            accountNames = [...accSet].sort();
            if (months.length > 0 && !months.includes(selectedCustomMonth)) {
                selectedCustomMonth = months[0];
            }
            handlePeriodChange(selectedPeriod);
            filterCategory = '';
            filterAccount = '';
            search = '';
            await fetchSummaryTransactions();
        } catch (e) {
            console.error('Failed to reload transactions for profile:', e);
        } finally {
            profileSwitching = false;
        }
    }
</script>

<svelte:window on:click={handleWindowClick} on:keydown={handleReviewConfirmKeydown} />
<div class="profile-transition" class:profile-loading={profileSwitching}>
<div class="flex items-start justify-between mb-6 fade-in">
    <div>
        <h2 class="folio-page-title">Transactions</h2>
        <p class="folio-page-subtitle">
            {#if selectedPeriod === 'all'}All time{:else if selectedPeriod === 'ytd'}YTD {new Date().getFullYear()}{:else if selectedPeriod === 'custom'}{formatMonth(selectedCustomMonth)}{:else}{formatMonth(getMonthForPeriod(selectedPeriod))}{/if} · {totalCount} transactions{#if totalCount > pageLimit} (showing {pageOffset + 1}–{Math.min(pageOffset + pageLimit, totalCount)}){/if}
        </p>
    </div>
    <ProfileSwitcher />
</div>

<!-- Update feedback toast -->
{#if updateFeedback}
    <div class="tx-feedback-toast fade-in">
        <span class="material-symbols-outlined text-[16px]" style="color: var(--positive)">check_circle</span>
        <span class="text-[12px] font-medium" style="color: var(--text-primary)">{updateFeedback}</span>
    </div>
{/if}

<!-- PERIOD SELECTOR -->
<div class="tx-period-row txn-period-row fade-in-up" style="animation-delay: 40ms">
    <div class="period-toggle-track" style="--seg-count: {periodOptions.length}; --active-idx: {activePeriodIdx};">
        <div class="period-toggle-thumb"></div>
        {#each periodOptions as p}
            <button class="period-toggle-label" class:active={selectedPeriod === p.key}
                on:click={() => handlePeriodChange(p.key)}>
                {p.label}
            </button>
        {/each}
    </div>
    {#if selectedPeriod === 'custom'}
    <div class="month-dropdown-wrapper">
        <button
            class="month-dropdown-trigger"
            class:ring-2={selectedPeriod === 'custom'}
            class:ring-accent={selectedPeriod === 'custom'}
            on:click|stopPropagation={() => { monthDropdownOpen = !monthDropdownOpen; closeAllFilters(); }}
        >
            <span>{formatMonth(selectedCustomMonth)}</span>
            <span class="material-symbols-outlined text-[13px]"
                  style="opacity: 0.5; transition: transform 0.2s;"
                  class:rotate-180={monthDropdownOpen}>
                expand_more
            </span>
        </button>

        {#if monthDropdownOpen}
            <button type="button" class="month-dropdown-backdrop" aria-label="Close month picker" on:click={() => monthDropdownOpen = false}></button>
            <div class="month-dropdown-menu" role="listbox" style="bottom: auto; top: calc(100% + 6px);">
                {#each months as m}
                    <button
                        class="month-dropdown-item"
                        class:month-dropdown-item-active={selectedCustomMonth === m && selectedPeriod === 'custom'}
                        role="option"
                        aria-selected={selectedCustomMonth === m && selectedPeriod === 'custom'}
                        on:click|stopPropagation={() => handleCustomMonthSelect(m)}
                    >
                        {formatMonth(m)}
                    </button>
                {/each}
            </div>
        {/if}
    </div>
    {/if}
</div>

<!-- MONTH STORY CARD -->
<section class="tx-story-card fade-in-up" style="animation-delay: 60ms">
    <div class="tx-story-kicker">{storyKicker}</div>
    <h3 class="tx-story-title">The month, <span>in full.</span></h3>

    <div class="tx-story-metrics">
        <div class="tx-story-metric tx-story-metric-main">
            <span class="tx-story-label">Spending</span>
            <strong>{formatCurrency(totalSpending, 2)}</strong>
            <small>{totalCount} transactions reviewed</small>
        </div>
        <div class="tx-story-metric">
            <span class="tx-story-label">Income</span>
            <strong class="tx-story-positive">{formatCurrency(totalIncome, 0)}</strong>
            <small>Actual income only</small>
        </div>
        {#if txCreditsRefunds > 0}
            <div class="tx-story-metric">
                <span class="tx-story-label">Credits & refunds</span>
                <strong class="tx-story-positive">+{formatCurrency(txCreditsRefunds, 0)}</strong>
                <small>Refunds and credits counted in flow</small>
            </div>
        {/if}
        <div class="tx-story-metric">
            <span class="tx-story-label">Ext. transfers</span>
            <strong class="tx-story-warning">{formatCurrency(txExternalTransfers, 0)}</strong>
            <small>External transfers counted in flow</small>
        </div>
        <div class="tx-story-metric">
            <span class="tx-story-label">CC repaid</span>
            <strong class="tx-story-muted">{formatCurrency(txCcRepaid, 0)}</strong>
            <small>Card payments excluded from spending</small>
        </div>
        <div class="tx-story-metric">
            <span class="tx-story-label">Net flow</span>
            <strong class:tx-story-positive={txNetFlow >= 0} class:tx-story-negative={txNetFlow < 0}>
                {txNetFlow >= 0 ? '+' : ''}{formatCurrency(txNetFlow, 0)}
            </strong>
            <small>Income plus credits minus spending and external transfers</small>
        </div>
        <div class="tx-story-metric">
            <span class="tx-story-label">Largest spend</span>
            <strong>-{formatCurrency(largestSpendAmount, 0)}</strong>
            <small>{largestSpendTx ? `${largestSpendTx.category || 'Uncategorized'} · ${largestSpendTx.description || 'Transaction'}` : 'No spending yet'}</small>
        </div>
    </div>

    <div class="tx-story-spark">
        <span>Daily spend</span>
        <div class="tx-story-bars" aria-hidden="true">
            {#each dailySpendBars as bar}
                <i class:tx-story-bar-hot={bar.spent === maxDailySpend && bar.spent > 0}
                   style="height: {bar.spent > 0 ? Math.max(8, (bar.spent / maxDailySpend) * 30) : 3}px"></i>
            {/each}
        </div>
        <span>large day</span>
    </div>
</section>

<!-- SEARCH + FILTERS -->
<div class="tx-command-card fade-in-up" style="animation-delay: 100ms; position: relative; z-index: 10;">
    <div class="tx-command-search">
        <span class="material-symbols-outlined">search</span>
        <input bind:value={search} type="text" placeholder="Search merchants, categories, accounts..." />
        <kbd>/</kbd>
    </div>

    <!-- Row 1: Category + Account filters -->
    <div class="tx-command-controls">
        <!-- Category Filter Pill -->
        <div class="relative" style="z-index: 51">
            <button class="txn-filter-pill"
                class:filter-active={filterCategory !== ''}
                on:click|stopPropagation={() => { openFilter(categoryPickerOpen ? '' : 'category'); monthDropdownOpen = false; }}>
                <span class="text-[12px] font-medium" style="color: var(--text-primary)">
                    {filterCategory || 'All Categories'}
                </span>
                <span class="material-symbols-outlined text-[16px]"
                    style="color: var(--text-muted); transition: transform 0.2s;"
                    class:txn-chevron-open={categoryPickerOpen}>
                    expand_more
                </span>
            </button>
            {#if categoryPickerOpen}
                <!-- svelte-ignore a11y-click-events-have-key-events -->
                <div class="txn-filter-dropdown txn-category-filter-dropdown" role="presentation" on:click|stopPropagation>
                    <div class="tx-cat-dropdown-search-wrap">
                        <span class="material-symbols-outlined text-[14px]" style="color: var(--text-muted)">search</span>
                        <input
                            bind:value={categoryFilterSearch}
                            placeholder="Search categories..."
                            class="tx-cat-dropdown-search"
                            on:keydown={(e) => {
                                if (e.key === 'Escape') closeAllFilters();
                            }}
                        />
                    </div>
                    <div class="tx-cat-dropdown-list">
                        <button
                            class="txn-filter-option"
                            class:active={filterCategory === ''}
                            on:click={() => { filterCategory = ''; closeAllFilters(); }}>
                            <span class="txn-filter-option-label">
                                <span class="material-symbols-outlined" style="color: var(--text-muted)">category</span>
                                <span>All Categories</span>
                            </span>
                            {#if filterCategory === ''}
                                <span class="material-symbols-outlined text-[14px]" style="color: var(--accent)">check</span>
                            {/if}
                        </button>
                        {#each filteredFilterCategories as cat}
                            <button
                                class="txn-filter-option"
                                class:active={cat === filterCategory}
                                on:click={() => { filterCategory = cat; closeAllFilters(); }}>
                                <span class="txn-filter-option-label">
                                    <span class="material-symbols-outlined" style="color: {CATEGORY_COLORS[cat] || 'var(--text-muted)'}">
                                        {CATEGORY_ICONS[cat] || 'label'}
                                    </span>
                                    <span>{cat}</span>
                                </span>
                                {#if cat === filterCategory}
                                    <span class="material-symbols-outlined text-[14px]" style="color: var(--accent)">check</span>
                                {/if}
                            </button>
                        {/each}
                        {#if filteredFilterCategories.length === 0 && categoryFilterSearch}
                            <div class="px-3 py-2 text-[11px]" style="color: var(--text-muted)">
                                No matching categories
                            </div>
                        {/if}
                    </div>
                </div>
            {/if}
        </div>

        <!-- Account Filter Pill -->
        <div class="relative" style="z-index: 50">
            <button class="txn-filter-pill"
                class:filter-active={filterAccount !== ''}
                on:click|stopPropagation={() => { openFilter(accountPickerOpen ? '' : 'account'); monthDropdownOpen = false; }}>
                <span class="text-[12px] font-medium" style="color: var(--text-primary)">
                    {filterAccount || 'All Accounts'}
                </span>
                <span class="material-symbols-outlined text-[16px]"
                    style="color: var(--text-muted); transition: transform 0.2s;"
                    class:txn-chevron-open={accountPickerOpen}>
                    expand_more
                </span>
            </button>
            {#if accountPickerOpen}
                <!-- svelte-ignore a11y-click-events-have-key-events -->
                <div class="txn-filter-dropdown" role="presentation" on:click|stopPropagation>
                    <button
                        class="txn-filter-option"
                        class:active={filterAccount === ''}
                        on:click={() => { filterAccount = ''; accountPickerOpen = false; }}>
                        All Accounts
                        {#if filterAccount === ''}
                            <span class="material-symbols-outlined text-[14px]" style="color: var(--accent)">check</span>
                        {/if}
                    </button>
                    {#each accountNames as acc}
                        <button
                            class="txn-filter-option"
                            class:active={acc === filterAccount}
                            on:click={() => { filterAccount = acc; accountPickerOpen = false; }}>
                            {acc}
                            {#if acc === filterAccount}
                                <span class="material-symbols-outlined text-[14px]" style="color: var(--accent)">check</span>
                            {/if}
                        </button>
                    {/each}
                </div>
            {/if}
        </div>

        <div class="tx-review-toggle" aria-label="Review filter">
            <button class:active={reviewFilter === 'all'} on:click={() => reviewFilter = 'all'}>All</button>
            <button class:active={reviewFilter === 'unreviewed'} on:click={() => reviewFilter = 'unreviewed'}>Unreviewed</button>
            <button class:active={reviewFilter === 'reviewed'} on:click={() => reviewFilter = 'reviewed'}>Reviewed</button>
        </div>

        {#if reviewFilter !== 'reviewed' && totalCount > 0}
            {#if reviewConfirmOpen && pendingBulkReview}
                <div class="tx-bulk-review-confirm" role="group" aria-label="Confirm bulk review">
                    <span class="material-symbols-outlined tx-bulk-review-confirm-icon" aria-hidden="true">done_all</span>
                    <span class="tx-bulk-review-confirm-copy">
                        <strong>{pendingBulkReview.count.toLocaleString()} {pendingBulkReview.label}</strong>
                        <span aria-hidden="true">·</span>
                        <span>{pendingBulkReview.scope}</span>
                    </span>
                    <span class="tx-bulk-review-confirm-actions">
                        <button
                            type="button"
                            class="tx-bulk-review-confirm-secondary"
                            disabled={bulkReviewing}
                            on:click={cancelBulkReviewConfirm}>
                            Cancel
                        </button>
                        <button
                            type="button"
                            class="tx-bulk-review-confirm-primary"
                            disabled={bulkReviewing}
                            on:click={confirmFilteredReviewed}>
                            <span class="material-symbols-outlined">{bulkReviewing ? 'hourglass_top' : 'done_all'}</span>
                            {bulkReviewing ? 'Confirming...' : 'Confirm'}
                        </button>
                    </span>
                </div>
            {:else}
                <button class="tx-bulk-review-btn" disabled={bulkReviewing} on:click={markFilteredReviewed}>
                    <span class="material-symbols-outlined">done_all</span>
                    {`Mark ${reviewFilter === 'unreviewed' ? 'all' : 'unreviewed'} reviewed`}
                </button>
            {/if}
        {/if}

        <button class="tx-export-btn" disabled={exportingCsv} on:click={exportCurrentTransactions} title="Export the current filtered transaction list as CSV">
            <span class="material-symbols-outlined">download</span>
            {exportingCsv ? 'Exporting...' : 'CSV'}
        </button>

        {#if hasActiveFilters}
            <button on:click={() => { clearFilters(); closeAllFilters(); monthDropdownOpen = false; }}
                class="tx-command-reset">
                <span class="material-symbols-outlined text-[14px]">close</span>
                Reset
            </button>
        {/if}
    </div>

    <div class="tx-active-filters">
        <span class="tx-filter-chip">
            <span class="material-symbols-outlined">calendar_month</span>
            {periodLabel}
        </span>
        {#if filterCategory}
            <button class="tx-filter-chip tx-filter-chip-removable" on:click={() => filterCategory = ''}>
                <span class="material-symbols-outlined">category</span>
                {filterCategory}
                <span class="material-symbols-outlined">close</span>
            </button>
        {/if}
        {#if filterAccount}
            <button class="tx-filter-chip tx-filter-chip-removable" on:click={() => filterAccount = ''}>
                <span class="material-symbols-outlined">account_balance</span>
                {filterAccount}
                <span class="material-symbols-outlined">close</span>
            </button>
        {/if}
        {#if search}
            <button class="tx-filter-chip tx-filter-chip-removable" on:click={() => search = ''}>
                <span class="material-symbols-outlined">search</span>
                {search}
                <span class="material-symbols-outlined">close</span>
            </button>
        {/if}
        {#if reviewFilter !== 'all'}
            <button class="tx-filter-chip tx-filter-chip-removable" on:click={() => reviewFilter = 'all'}>
                <span class="material-symbols-outlined">fact_check</span>
                {reviewFilter === 'reviewed' ? 'Reviewed' : 'Unreviewed'}
                <span class="material-symbols-outlined">close</span>
            </button>
        {/if}
    </div>
</div>

<!-- TRANSACTIONS (grouped by day) -->
{#if loading}
    <div class="space-y-3">
        {#each Array(6) as _}
            <div class="skeleton h-14 rounded-xl"></div>
        {/each}
    </div>
{:else}
    <div class="tx-ledger-card fade-in-up" style="animation-delay: 140ms;">
        {#if groupedTxns.length === 0}
            <div class="text-center py-16" style="color: var(--text-muted)">
                <span class="material-symbols-outlined text-5xl mb-3" style="opacity: 0.4">search_off</span>
                <p class="text-sm font-medium">No transactions match your filters</p>
                <p class="text-[11px] mt-1">Try adjusting the month, category, or search term</p>
            </div>
        {:else}
            <div class="tx-column-headers">
                <span>Transaction</span>
                <span>Merchant</span>
                <span>Category</span>
                <span class="tx-col-header-amount">Amount</span>
            </div>
            {#each groupedTxns as [date, txns], gi}
                {@const dayNet = txns.reduce((s, t) => s + parseFloat(t.amount || 0), 0)}
                {@const dayInfo = formatLedgerDay(date)}

                <div class="tx-day-group" class:tx-day-group-separated={gi > 0}>
                    <div class="tx-day-band">
                        <div class="tx-day-rail-date">
                            <strong>{dayInfo.day}</strong>
                            <div>
                                <span>{dayInfo.meta}</span>
                                {#if dayInfo.relative}<small>{dayInfo.relative}</small>{/if}
                            </div>
                        </div>
                        {#if txns.length > 1}
                        <div class="tx-day-summary-strip">
                            <div>
                                <em>{txns.length} tx</em>
                                <span>net</span>
                                <strong class={dayNet >= 0 ? 'tx-day-income' : 'tx-day-spend'}>
                                    {dayNet >= 0 ? '+' : ''}{formatCurrency(dayNet, 0)}
                                </strong>
                            </div>
                        </div>
                        {/if}
                    </div>
                    <div class="tx-day-body">

                {#each txns as tx (tx.original_id)}
                    {@const amount = parseFloat(tx.amount)}
                    {@const sourceInfo = getSourceLabel(tx)}
                    {@const isRecentlyUpdated = recentlyUpdatedTxId === tx.original_id}
                    {@const isSelected = selectedTxId === tx.original_id}
                    {@const transactionTitle = getTransactionTitle(tx)}
                    {@const merchantCell = getMerchantCell(tx)}
                    {@const rawDescriptor = getRawDescriptor(tx)}
                    {@const rowSignal = getRowSignal(tx)}
                    {@const merchantStats = getMerchantStats(tx)}
                    {@const metadataDraft = ensureMetadataDraft(tx)}
                    {@const hasMerchantTrend = merchantStats.recent.length > 0 && merchantStats.months.some(m => m.total > 0)}
                    {@const maxMerchantMonth = Math.max(...merchantStats.months.map(m => m.total), Math.abs(amount), 1)}
                    <div class="tx-row-grid group transition-colors tx-row tx-ledger-row"
                        class:tx-row-updated={isRecentlyUpdated}
                        class:tx-row-selected={isSelected}
                        role="button"
                        tabindex="0"
                        aria-expanded={isSelected}
                        on:click={() => toggleTransactionDetails(tx, isSelected)}
                        on:keydown={(event) => {
                            if (event.key === 'Enter' || event.key === ' ') {
                                event.preventDefault();
                                toggleTransactionDetails(tx, isSelected);
                            }
                        }}>

                        <!-- Zone 1: Icon + Description + Account -->
	                        <div class="tx-zone-desc">
	                            <div class="tx-merchant-avatar"
	                                style="--tx-cat-color: {CATEGORY_COLORS[tx.category] || '#627d98'}">
	                                <i></i>
	                                <span>{merchantCell.label.charAt(0)}</span>
	                            </div>
	                            <div class="min-w-0 flex-1">
	                                <p class="text-[13px] font-medium truncate" style="color: var(--text-primary)">
	                                    {transactionTitle}
	                                </p>
	                                <span class="tx-row-subline">
	                                    <span class="tx-account-dot" style="background: {getAccountHue(tx)}"></span>
	                                    {tx.account_name || 'Account'}{#if getAccountSuffix(tx)} · ••{getAccountSuffix(tx)}{/if}
	                                    {#if tx.raw_description && tx.raw_description.toUpperCase() !== rawDescriptor.toUpperCase()}
	                                        <span class="tx-row-dot"></span>
	                                        <span class="tx-raw-preview">{tx.raw_description}</span>
	                                    {/if}
	                                </span>
	                            </div>
	                        </div>

	                        <!-- Zone 2: Merchant identity -->
	                        <!-- svelte-ignore a11y-click-events-have-key-events -->
	                        <!-- svelte-ignore a11y-no-static-element-interactions -->
	                        <div class="tx-zone-merchant" on:click|stopPropagation>
	                            {#if editingMerchantTxId === tx.original_id}
	                                <div class="tx-merchant-editor">
	                                    <input
	                                        bind:value={merchantDraftName}
	                                        class="tx-merchant-input"
	                                        disabled={savingMerchantFor === tx.original_id}
	                                        on:keydown={(event) => {
	                                            event.stopPropagation();
	                                            if (event.key === 'Enter') saveMerchantAlias(tx);
	                                            if (event.key === 'Escape') cancelMerchantEditing();
	                                        }}
	                                    />
	                                    <button
	                                        class="tx-edit-btn tx-edit-btn-confirm"
	                                        disabled={savingMerchantFor === tx.original_id || !merchantDraftName.trim()}
	                                        on:click={() => saveMerchantAlias(tx)}>
	                                        <span class="material-symbols-outlined text-[13px]">check</span>
	                                    </button>
	                                    <button
	                                        class="tx-edit-btn"
	                                        disabled={savingMerchantFor === tx.original_id}
	                                        on:click={cancelMerchantEditing}>
	                                        <span class="material-symbols-outlined text-[13px]">close</span>
	                                    </button>
	                                    {#if merchantEditError}
	                                        <span class="tx-merchant-error">{merchantEditError}</span>
	                                    {/if}
	                                </div>
	                            {:else}
	                                <button
	                                    class="tx-merchant-identity"
	                                    class:tx-merchant-identity-muted={!merchantCell.editable}
	                                    disabled={!merchantCell.editable}
	                                    title={merchantCell.sublabel}
	                                    style="--tx-cat-color: {CATEGORY_COLORS[tx.category] || '#627d98'}"
	                                    on:click={() => startMerchantEditing(tx)}>
	                                    <span class="tx-merchant-name">{merchantCell.label}</span>
	                                    {#if merchantCell.source}
	                                        <span class="tx-merchant-source-badge tx-merchant-source-{merchantCell.source.type}">
	                                            {merchantCell.source.label}
	                                        </span>
	                                    {/if}
	                                    {#if merchantCell.editable}
	                                        <span class="material-symbols-outlined tx-merchant-edit-icon">edit</span>
	                                    {/if}
	                                </button>
	                            {/if}
	                        </div>

	                        <!-- Zone 3: Category pill + source badge -->
	                        <div class="tx-zone-category">
                            <div class="relative tx-cat-pill-wrapper">
                                <button
                                    class="tx-cat-pill"
                                    class:tx-cat-pill-editing={editingTxId === tx.original_id}
                                    on:click|stopPropagation={() => {
                                        if (editingTxId === tx.original_id) {
                                            if (catDropdownOpenForTx === tx.original_id) {
                                                cancelEditing();
                                            } else {
                                                catDropdownOpenForTx = tx.original_id;
                                            }
                                        } else {
                                            startEditing(tx.original_id);
                                        }
                                    }}
                                    style="--pill-color: {CATEGORY_COLORS[tx.category] || '#627d98'}">
                                    <span class="material-symbols-outlined text-[13px]" style="color: var(--pill-color)">
                                        {CATEGORY_ICONS[tx.category] || 'label'}
                                    </span>
                                    <span class="tx-cat-pill-label">{tx.category || 'Uncategorized'}</span>
                                    <span class="material-symbols-outlined text-[12px] tx-cat-pill-chevron"
                                        class:txn-chevron-open={catDropdownOpenForTx === tx.original_id}
                                        style="color: var(--text-muted);">
                                        expand_more
                                    </span>
                                </button>

                                {#if catDropdownOpenForTx === tx.original_id}
                                    <!-- svelte-ignore a11y-click-events-have-key-events -->
                                    <div class="txn-filter-dropdown tx-cat-dropdown tx-retag-dropdown" role="presentation" on:click|stopPropagation>
                                        <div class="tx-cat-apply-toggle">
                                            <div class="tx-cat-apply-toggle-copy">
                                                <span class="tx-cat-apply-toggle-label">Apply</span>
                                            </div>
                                            <div class="tx-cat-apply-toggle-actions">
                                                <button
                                                    class="tx-cat-apply-mode-pill"
                                                    class:tx-cat-apply-mode-pill--active={categoryApplyMode === 'always'}
                                                    on:click={() => { categoryApplyMode = 'always'; }}>
                                                    Always
                                                </button>
                                                <button
                                                    class="tx-cat-apply-mode-pill"
                                                    class:tx-cat-apply-mode-pill--active={categoryApplyMode === 'once'}
                                                    on:click={() => { categoryApplyMode = 'once'; }}>
                                                    Just once
                                                </button>
                                            </div>
                                        </div>
                                        <div class="tx-cat-dropdown-search-wrap">
                                            <span class="material-symbols-outlined text-[14px]" style="color: var(--text-muted)">search</span>
                                            <input
                                                bind:value={catDropdownSearch}
                                                placeholder="Search categories..."
                                                class="tx-cat-dropdown-search"
                                                on:keydown={(e) => {
                                                    if (e.key === 'Escape') cancelEditing();
                                                }}
                                            />
                                        </div>
                                        <div class="tx-cat-dropdown-list">
                                            {#each filteredEditCategories as cat}
                                                <button
                                                    class="txn-filter-option"
                                                    class:active={cat === tx.category}
                                                    on:click={() => {
                                                        if (cat !== tx.category) {
                                                            updateCategory(tx.original_id, cat, categoryApplyMode === 'once');
                                                        } else {
                                                            cancelEditing();
                                                        }
                                                    }}>
                                                    <span class="txn-filter-option-label">
                                                        <span class="material-symbols-outlined" style="color: {CATEGORY_COLORS[cat] || 'var(--text-muted)'}">
                                                            {CATEGORY_ICONS[cat] || 'label'}
                                                        </span>
                                                        <span>{cat}</span>
                                                    </span>
                                                    {#if cat === tx.category}
                                                        <span class="material-symbols-outlined text-[14px]" style="color: var(--accent)">check</span>
                                                    {/if}
                                                </button>
                                            {/each}
                                            {#if filteredEditCategories.length === 0 && catDropdownSearch}
                                                <div class="px-3 py-2 text-[11px]" style="color: var(--text-muted)">
                                                    No matching categories
                                                </div>
                                            {/if}
                                        </div>
                                        <div class="tx-cat-dropdown-footer">
                                            {#if creatingNewCategory}
                                                <div class="flex items-center gap-1.5 px-2 py-1.5">
                                                    <input
                                                        bind:value={newCategoryName}
                                                        placeholder="New category name..."
                                                        class="tx-cat-dropdown-new-input"
                                                        on:keydown={(e) => {
                                                            if (e.key === 'Enter') createAndApplyCategory(tx.original_id);
                                                            if (e.key === 'Escape') { creatingNewCategory = false; newCategoryName = ''; newCategoryError = ''; }
                                                        }}
                                                    />
                                                    <button
                                                        class="tx-edit-btn tx-edit-btn-confirm"
                                                        on:click={() => createAndApplyCategory(tx.original_id)}
                                                        disabled={!newCategoryName.trim()}>
                                                        <span class="material-symbols-outlined text-[13px]">check</span>
                                                    </button>
                                                </div>
                                                {#if newCategoryError}
                                                    <span class="text-[9px] px-3" style="color: var(--negative)">{newCategoryError}</span>
                                                {/if}
                                            {:else}
                                                <button
                                                    class="txn-filter-option tx-cat-create-btn"
                                                    on:click={() => { creatingNewCategory = true; }}>
                                                    <span class="txn-filter-option-label">
                                                        <span class="material-symbols-outlined" style="color: #8b5cf6">add_circle</span>
                                                        <span style="color: #8b5cf6; font-weight: 600;">Create new category</span>
                                                    </span>
                                                </button>
                                            {/if}
                                        </div>
                                    </div>
                                {/if}
                            </div>
                            {#if sourceInfo}
                                <span class="tx-source-badge tx-source-{sourceInfo.type}">{sourceInfo.label}</span>
                            {/if}
                        </div>

                        <div class="tx-zone-amount">
                            <div>
                                {#if rowSignal}
                                    <span class="tx-signal-chip" class:tx-signal-chip-alert={rowSignal === 'Large'}>{rowSignal}</span>
                                {/if}
                                <p class="folio-amount-compact tx-row-amount"
                                    class:tx-row-amount-positive={amount >= 0}
                                    class:tx-row-amount-negative={amount < 0}>
                                    {amount >= 0 ? '+' : ''}{formatCurrency(amount, 2)}
                                </p>
                            </div>
                        </div>
                    </div>

                    {#if isSelected}
                        {@const splitDraft = splitDrafts[tx.original_id] || ensureSplitDraft(tx)}
                        {@const splitDelta = getSplitDeltaFromDraft(tx, splitDraft)}
                        <div class="tx-detail-drawer fade-in" role="presentation" on:click|stopPropagation>
                            <div class="tx-detail-pane">
                                <h4>Merchant pattern</h4>
                                <div class="tx-merchant-intel">
                                    <div>
                                        <span>{merchantStats.looksMonthly ? 'Likely recurring' : 'Seen in history'}</span>
                                        <strong>
                                            {merchantStats.looksMonthly ? `Monthly · ${formatCurrency(merchantStats.average, 2)}` : `${merchantStats.visits} visit${merchantStats.visits === 1 ? '' : 's'}`}
                                        </strong>
                                    </div>
                                    <div>
                                        <span>Typical amount</span>
                                        <strong>{merchantStats.average > 0 ? formatCurrency(merchantStats.average, 2) : formatCurrency(Math.abs(amount), 2)}</strong>
                                    </div>
                                    <div>
                                        <span>Category pattern</span>
                                        <strong>{merchantStats.dominantCategory} · {merchantStats.dominantCategoryCount}/{merchantStats.visits}</strong>
                                    </div>
                                    {#if merchantCell.editable && merchantCell.sublabel}
                                        <div>
                                            <span>Industry</span>
                                            <strong>{merchantCell.sublabel}</strong>
                                        </div>
                                    {/if}
                                    {#if merchantCell.source}
                                        <div>
                                            <span>Merchant source</span>
                                            <strong>{merchantCell.source.label}</strong>
                                        </div>
                                    {/if}
                                    {#if merchantStats.firstSeen}
                                        <div>
                                            <span>First seen</span>
                                            <strong>{formatDate(merchantStats.firstSeen)}</strong>
                                        </div>
                                    {/if}
                                </div>
                                {#if rawDescriptor && rawDescriptor.toUpperCase() !== merchantCell.label.toUpperCase()}
                                    <details class="tx-raw-disclosure">
                                        <summary>Bank statement text</summary>
                                        <p>{rawDescriptor}</p>
                                    </details>
                                {/if}
                            </div>
                            <div class="tx-detail-pane">
                                <h4>Recent at this merchant</h4>
                                <div class="tx-merchant-history">
                                    {#if merchantStats.recent.length > 0}
                                        {#each merchantStats.recent as recent}
                                            <div>
                                                <span>{formatDate(recent.date)}</span>
                                                <strong>{formatCurrency(parseFloat(recent.amount), 2)}</strong>
                                            </div>
                                        {/each}
                                    {:else}
                                        <p>No earlier matching transactions in your loaded history.</p>
                                    {/if}
                                    {#if hasMerchantTrend}
                                        <div class="tx-history-bars" aria-label="Merchant spending trend">
                                            {#each merchantStats.months as month}
                                                <span style="height: {month.total > 0 ? Math.max(8, (month.total / maxMerchantMonth) * 44) : 4}px">
                                                    <em>{month.label}</em>
                                                </span>
                                            {/each}
                                        </div>
                                    {/if}
                                    <p>
                                        {merchantStats.visits} visit{merchantStats.visits === 1 ? '' : 's'} in history{#if merchantStats.average > 0}, average <strong>{formatCurrency(merchantStats.average, 2)}</strong>{/if}{#if merchantStats.visits > 1 && merchantStats.average > 0} · this ran <strong class={merchantStats.delta >= 0 ? 'tx-above-average' : 'tx-below-average'}>{merchantStats.delta >= 0 ? '+' : ''}{merchantStats.delta.toFixed(1)}%</strong> vs usual{/if}.
                                    </p>
                                </div>
                            </div>
                            <div class="tx-detail-pane tx-detail-notes">
                                <h4>Review notes</h4>
                                <label class="tx-meta-toggle">
                                    <input
                                        type="checkbox"
                                        checked={metadataDraft.reviewed}
                                        on:change={(e) => {
                                            metadataDrafts[tx.original_id].reviewed = e.currentTarget.checked;
                                            metadataDrafts = { ...metadataDrafts };
                                            saveTransactionMetadata(tx);
                                        }} />
                                    <span>Reviewed</span>
                                </label>
                                <textarea
                                    value={metadataDraft.notes}
                                    placeholder="Add a note for future you"
                                    on:input={(e) => {
                                        metadataDrafts[tx.original_id].notes = e.currentTarget.value;
                                        metadataDrafts = { ...metadataDrafts };
                                    }}></textarea>
                                <input
                                    value={metadataDraft.tags}
                                    placeholder="Tags, comma separated"
                                    on:input={(e) => {
                                        metadataDrafts[tx.original_id].tags = e.currentTarget.value;
                                        metadataDrafts = { ...metadataDrafts };
                                    }} />
                                <button class="tx-meta-save" disabled={savingMetadataFor === tx.original_id} on:click={() => saveTransactionMetadata(tx)}>
                                    {savingMetadataFor === tx.original_id ? 'Saving...' : 'Save details'}
                                </button>
                            </div>
                            <div class="tx-detail-pane tx-detail-splits">
                                <div class="tx-split-header">
                                    <h4>Split allocation</h4>
                                    <button on:click={() => addSplitRow(tx)}>Add line</button>
                                </div>
                                {#if splitDraft.loading}
                                    <p class="tx-split-muted">Checking saved split details...</p>
                                {/if}
                                <div class="tx-split-rows">
                                    {#each splitDraft.rows as split, splitIndex}
                                        <div class="tx-split-row">
                                            <select
                                                value={split.category}
                                                on:change={(e) => updateSplitField(tx.original_id, splitIndex, 'category', e.currentTarget.value)}>
                                                {#each allCategories as cat}
                                                    <option value={cat}>{cat}</option>
                                                {/each}
                                                {#if !allCategories.includes(split.category)}
                                                    <option value={split.category}>{split.category}</option>
                                                {/if}
                                            </select>
                                            <input
                                                type="number"
                                                min="0"
                                                step="0.01"
                                                value={split.amount}
                                                on:input={(e) => updateSplitField(tx.original_id, splitIndex, 'amount', e.currentTarget.value)}
                                                on:blur={() => fillSingleBlankSplitRemainder(tx)}
                                                aria-label="Split amount" />
                                            <button
                                                class="tx-split-remove"
                                                disabled={splitDraft.rows.length <= 1}
                                                on:click={() => removeSplitRow(tx.original_id, splitIndex)}
                                                aria-label="Remove split line">
                                                <span class="material-symbols-outlined">close</span>
                                            </button>
                                            <input
                                                class="tx-split-note"
                                                value={split.notes}
                                                placeholder="Optional note"
                                                on:input={(e) => updateSplitField(tx.original_id, splitIndex, 'notes', e.currentTarget.value)} />
                                        </div>
                                    {/each}
                                </div>
                                <div class="tx-split-total" class:tx-split-total-off={Math.abs(splitDelta) >= 0.01}>
                                    <span>Total {formatCurrency(getSplitTotalFromDraft(splitDraft), 2)}</span>
                                    <strong>{Math.abs(splitDelta) < 0.01 ? 'Balanced' : `${splitDelta > 0 ? 'Unassigned' : 'Over'} ${formatCurrency(Math.abs(splitDelta), 2)}`}</strong>
                                </div>
                                <button class="tx-meta-save" disabled={savingSplitsFor === tx.original_id} on:click={() => saveTransactionSplits(tx)}>
                                    {savingSplitsFor === tx.original_id ? 'Saving...' : 'Save split'}
                                </button>
                            </div>
                        </div>
                    {/if}

                    <!-- Subscription Declaration Prompt -->
                    {#if subscriptionPromptTxId === tx.original_id}
                        <!-- svelte-ignore a11y-click-events-have-key-events -->
                        <div class="tx-subscription-prompt fade-in" role="presentation" on:click|stopPropagation
                            style="border-bottom: 1px solid color-mix(in srgb, var(--card-border) 50%, transparent)">
                            <div class="tx-subscription-prompt-inner">
                                <div class="flex items-center gap-2.5 flex-1 min-w-0">
                                    <div class="w-7 h-7 rounded-lg flex items-center justify-center flex-shrink-0"
                                        style="background: color-mix(in srgb, var(--accent) 10%, transparent)">
                                        <span class="material-symbols-outlined text-[14px]" style="color: var(--accent)">event_repeat</span>
                                    </div>
                                    <div class="min-w-0">
                                        <p class="text-[12px] font-semibold" style="color: var(--text-primary)">
                                            Track <span style="color: var(--accent)">{subscriptionPromptMerchant}</span> as recurring?
                                        </p>
                                        <p class="text-[10px]" style="color: var(--text-muted)">
                                            {subscriptionPromptCategory} · {formatCurrency(subscriptionPromptAmount)} · Select frequency
                                        </p>
                                    </div>
                                </div>
                                <div class="flex items-center gap-1.5 flex-shrink-0">
                                    {#each frequencyOptions as freq}
                                        <button
                                            class="tx-subscription-freq-pill"
                                            class:tx-subscription-freq-active={subscriptionPromptFrequency === freq.key}
                                            disabled={subscriptionDeclareLoading}
                                            on:click|stopPropagation={() => handleDeclareSubscription(freq.key)}>
                                            {freq.label}
                                        </button>
                                    {/each}
                                    <button
                                        class="tx-subscription-dismiss-btn"
                                        on:click|stopPropagation={dismissSubscriptionPrompt}>
                                        <span class="material-symbols-outlined text-[14px]">close</span>
                                    </button>
                                </div>
                            </div>
                            {#if subscriptionDeclareLoading}
                                <div class="tx-subscription-loading">
                                    <div class="tx-subscription-loading-bar"></div>
                                </div>
                            {/if}
                        </div>
                    {/if}
                {/each}
                    </div>
                </div>
            {/each}
        {/if}
    </div>
{/if}

    <!-- PAGINATION CONTROLS -->
    {#if totalPages > 1}
        <div class="flex items-center justify-between mt-4 px-1 fade-in">
            <p class="text-[11px] font-medium" style="color: var(--text-muted)">
                Showing {pageOffset + 1}–{Math.min(pageOffset + pageLimit, totalCount)} of {totalCount}
            </p>

            <div class="flex items-center gap-1.5">
                <button
                    on:click={prevPage}
                    disabled={pageOffset === 0}
                    class="pagination-btn"
                    class:pagination-btn-disabled={pageOffset === 0}>
                    <span class="material-symbols-outlined text-[16px]">chevron_left</span>
                </button>

                {#each Array(Math.min(totalPages, 7)) as _, i}
                    {@const pageNum = (() => {
                        // Show pages around current page
                        if (totalPages <= 7) return i;
                        if (currentPage < 4) return i;
                        if (currentPage > totalPages - 4) return totalPages - 7 + i;
                        return currentPage - 3 + i;
                    })()}
                    {#if pageNum >= 0 && pageNum < totalPages}
                        <button
                            on:click={() => goToPage(pageNum)}
                            class="pagination-btn"
                            class:pagination-btn-active={pageNum === currentPage}>
                            {pageNum + 1}
                        </button>
                    {/if}
                {/each}

                <button
                    on:click={nextPage}
                    disabled={pageOffset + pageLimit >= totalCount}
                    class="pagination-btn"
                    class:pagination-btn-disabled={pageOffset + pageLimit >= totalCount}>
                    <span class="material-symbols-outlined text-[16px]">chevron_right</span>
                </button>
            </div>

            <div class="flex items-center gap-2">
                <span class="text-[10px]" style="color: var(--text-muted)">Per page:</span>
                {#each [25, 50, 100] as size}
                    <button
                        on:click={() => { pageLimit = size; pageOffset = 0; fetchTransactions(); }}
                        class="text-[10px] px-2 py-1 rounded-lg transition-colors"
                        style="background: {pageLimit === size ? 'var(--accent)' : 'var(--surface-100)'}; color: {pageLimit === size ? 'white' : 'var(--text-muted)'}; font-weight: {pageLimit === size ? '700' : '500'}">
                        {size}
                    </button>
                {/each}
            </div>
        </div>
    {/if}
</div>
