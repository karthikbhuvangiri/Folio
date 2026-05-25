<script>
    import '$lib/styles/control-center.css';
    import { goto } from '$app/navigation';
    import { page } from '$app/stores';
    import { onMount } from 'svelte';
    import { api, invalidateCache } from '$lib/api.js';
    import { activeProfile, loadProfiles } from '$lib/stores/profileStore.js';
    import { formatCurrency, formatDate } from '$lib/utils.js';
    import ProfileSwitcher from '$lib/components/ProfileSwitcher.svelte';
    import SimpleFINConnect from '$lib/components/SimpleFINConnect.svelte';
    import MigrationWizard from '$lib/components/MigrationWizard.svelte';

    const allTabs = [
        { key: 'connections', label: 'Connections' },
        { key: 'accounts',    label: 'Accounts' },
        { key: 'investments', label: 'Investments' },
        { key: 'merchants',   label: 'Merchants' },
        { key: 'rules',       label: 'Rules' },
        { key: 'categories',  label: 'Categories' },
        { key: 'history',     label: 'History' },
    ];
    const merchantFilterKeys = new Set(['all', 'subscriptions', 'non_subscriptions']);
    const MERCHANT_RECENT_DAYS = 365;
    const MS_PER_DAY = 24 * 60 * 60 * 1000;

    let activeTab = 'merchants';
    let notice = '';
    let noticeTimeout;
    let loading = true;
    let lastLoadedProfile = undefined;

    let merchantsLoading = false;
    let merchantPreviewLoading = false;
    let rulesLoading = false;
    let ruleImpactLoading = false;
    let categoriesLoading = false;
    let historyLoading = false;

    // ── Merchants ────────────────────────────────────────────────
    let merchantItems = [];
    let merchantSearch = '';
    let merchantSubFilter = 'all';        // all | subscriptions | non_subscriptions
    let expandedMerchantYearGroups = {};
    let selectedMerchantKey = null;
    let expandedMerchantKey = null;
    let merchantTransactions = [];
    let merchantSaving = false;
    let lastMerchantPreviewKey = null;
    let openMerchantCategoryMenuKey = null;
    let merchantFilterMenuOpen = false;
    let merchantCategorySearch = '';
    let merchantAliasDrafts = {};

    // ── Rules ────────────────────────────────────────────────────
    let ruleItems = [];
    let ruleSearch = '';
    let ruleSourceFilter = 'all';
    let ruleStateFilter = 'working';
    let selectedRuleId = null;
    let ruleDraft = { id: null, category: '', priority: 1000, is_active: true };
    let ruleImpact = null;
    let lastRuleImpactId = null;
    let ruleSaving = false;

    // ── Categories ───────────────────────────────────────────────
    let categoriesMeta = [];
    let categorySearch = '';
    let categorySavingKey = null;   // name of the category currently being saved
    let categoryReplacementDrafts = {};
    let categoryReplacementRequiredKey = null;
    let openCategoryReplacementKey = null;
    let categoryReplacementSearch = '';

    // ── History ──────────────────────────────────────────────────
    let historyItems = [];
    let historySearch = '';
    let selectedHistoryId = null;

    // ── Connections ──────────────────────────────────────────────
    let tellerEnrollments = [];
    let simplefinConnections = [];
    let dataHealth = null;
    let backupStatus = null;
    let backupExporting = false;
    let pendingRemovalKey = null;
    let removalSavingKey = null;
    let connectionsLoading = false;
    let accountsLoading = false;
    let accountItems = [];
    let manualAccountDraft = { name: '', account_type: 'investment', account_subtype: 'manual', balance: '', notes: '' };
    let manualAccountSaving = false;
    let manualAccountEdits = {};
    let manualAccountSavingId = null;
    let accountPaymentEdits = {};
    let accountPaymentSavingId = null;
    let accountPaymentSavedId = '';
    let lastLinkedAccountEditId = '';
    let simplefinRef;
    let migrationRef;
    let investmentsLoading = false;
    let investmentsData = { holdings: [], allocation: [], summary: {} };
    let holdingDraft = {
        account_id: '',
        symbol: '',
        name: '',
        asset_class: 'stock',
        quantity: '',
        cost_basis: '',
        current_price: '',
        manual_value: '',
        target_percent: '',
        notes: ''
    };
    let holdingSaving = false;
    let holdingEditId = null;
    let tellerConfig = null;
    let appConfig = {
        demoMode: false,
        bankLinkingEnabled: true,
        manualSyncEnabled: true,
        demoPersistence: 'persistent',
        localLlmEnabled: false,
        localLlmProvider: 'ollama',
        memoryTier: '16gb',
        localAiProfile: 'balanced',
        lowPowerMode: false,
        expertMode: false,
        selectedCategorizeModel: '',
        selectedCopilotModel: '',
        selectedAdvisorModel: '',
    };
    let localLlmCatalog = {
        tiers: [],
        models: {},
        recommendedDefaults: {},
        presets: {},
        expertModeAvailable: true,
    };
    let localLlmStatus = {
        provider: 'ollama',
        ollamaReachable: false,
        memoryTier: '16gb',
        memoryLabel: '16 GB',
        ramGb: null,
        installedModels: [],
        selectedCategorizeModel: '',
        selectedControllerModel: '',
        selectedCopilotModel: '',
        selectedAdvisorModel: '',
        preset: 'default',
        lowPowerMode: false,
        expertMode: false,
        categorizeBatchSize: 25,
        interBatchDelayMs: 600,
    };
    let localLlmLoading = false;
    let localLlmSaving = false;
    let localLlmInstallingModel = '';
    let localLlmError = '';
    let localLlmRecommendationsOpen = false;
    let localLlmForm = {
        llm_provider: 'ollama',
        preset: 'default',
        categorize_model: '',
        copilot_model: '',
        low_power_mode: false,
        expert_mode: false,
        categorize_batch_size: 25,
        inter_batch_delay_ms: 600,
    };

    // ── Profile reactivity ───────────────────────────────────────
    $: activeProfileId = $activeProfile || 'household';
    $: scopedProfile   = activeProfileId !== 'household' ? activeProfileId : null;
    $: allCategoryNames = categoriesMeta
        .filter((item) => item.is_active)
        .map((item) => item.name)
        .filter(Boolean);
    $: visibleTabs = allTabs.filter((tab) => appConfig.bankLinkingEnabled || tab.key !== 'connections');
    $: tabKeys = new Set(visibleTabs.map((tab) => tab.key));

    $: urlTab = $page.url.searchParams.get('tab') || 'merchants';
    $: if (tabKeys.has(urlTab) && urlTab !== activeTab) {
        activeTab = urlTab;
    }
    $: if (!tabKeys.has(urlTab) && activeTab !== 'merchants') {
        activeTab = 'merchants';
    }
    $: urlMerchantFilter = $page.url.searchParams.get('merchant_filter');
    $: if (urlMerchantFilter && merchantFilterKeys.has(urlMerchantFilter) && urlMerchantFilter !== merchantSubFilter) {
        merchantSubFilter = urlMerchantFilter;
    }
    $: linkedAccountEditId = $page.url.searchParams.get('account') || '';
    $: if (activeTab === 'accounts' && linkedAccountEditId && linkedAccountEditId !== lastLinkedAccountEditId && accountItems.length) {
        const linkedAccount = accountItems.find((account) => account.id === linkedAccountEditId);
        if (isCreditCardAccount(linkedAccount)) {
            startAccountPaymentEdit(linkedAccount);
            lastLinkedAccountEditId = linkedAccountEditId;
        }
    }

    // ── Merchants derived ────────────────────────────────────────
    function merchantRowKey(item) {
        return `${item.merchant_key}::${item.profile_id}`;
    }

    function merchantLastSpentDate(item) {
        return item?.last_spent_date || item?.last_charge_date || '';
    }

    function merchantLastSpentLabel(item) {
        const value = merchantLastSpentDate(item);
        return value ? formatDate(value) : '—';
    }

    function isRecentMerchant(item) {
        const value = merchantLastSpentDate(item);
        if (!value) return false;
        const parsed = new Date(value);
        if (Number.isNaN(parsed.getTime())) return false;
        return (Date.now() - parsed.getTime()) / MS_PER_DAY <= MERCHANT_RECENT_DAYS;
    }

    function merchantLastSpentYear(item) {
        const value = merchantLastSpentDate(item);
        if (!value) return 'No date';
        const parsed = new Date(value);
        if (Number.isNaN(parsed.getTime())) return 'No date';
        return String(parsed.getFullYear());
    }

    function toggleMerchantYearGroup(year) {
        expandedMerchantYearGroups = {
            ...expandedMerchantYearGroups,
            [year]: !expandedMerchantYearGroups[year],
        };
        expandedMerchantKey = null;
        openMerchantCategoryMenuKey = null;
    }

    function merchantMetaLine(item) {
        const industry = (item?.industry || '').trim();
        const category = (item?.category || '').trim().toLowerCase();
        if (!industry) return '';
        if (category && industry.toLowerCase().includes(category)) return '';
        return industry;
    }

    function filterCategoryNames(names, query) {
        const needle = (query || '').trim().toLowerCase();
        if (!needle) return names;
        return names.filter((name) => String(name).toLowerCase().includes(needle));
    }

    $: merchantFilteredItems = merchantItems.filter((item) => {
        if (merchantSubFilter === 'subscriptions')     return !!item.is_subscription;
        if (merchantSubFilter === 'non_subscriptions') return !item.is_subscription;
        return true;
    });
    $: merchantRecentItems = merchantSearch.trim()
        ? merchantFilteredItems
        : merchantFilteredItems.filter(isRecentMerchant);
    $: merchantArchivedItems = merchantSearch.trim()
        ? []
        : merchantFilteredItems.filter((item) => !isRecentMerchant(item));
    $: merchantArchiveGroups = Object.values(merchantArchivedItems.reduce((groups, item) => {
        const year = merchantLastSpentYear(item);
        if (!groups[year]) groups[year] = { year, items: [], total_spent: 0, charge_count: 0 };
        groups[year].items.push(item);
        groups[year].total_spent += Number(item.total_spent || 0);
        groups[year].charge_count += Number(item.charge_count || 0);
        return groups;
    }, {})).sort((a, b) => {
        if (a.year === 'No date') return 1;
        if (b.year === 'No date') return -1;
        return Number(b.year) - Number(a.year);
    });
    $: visibleMerchants = merchantSearch.trim()
        ? merchantFilteredItems
        : [
            ...merchantRecentItems,
            ...merchantArchiveGroups.flatMap((group) => expandedMerchantYearGroups[group.year] ? group.items : []),
        ];
    $: merchantDisplayEntries = merchantSearch.trim()
        ? visibleMerchants.map((item) => ({ type: 'merchant', item }))
        : [
            ...merchantRecentItems.map((item) => ({ type: 'merchant', item })),
            ...merchantArchiveGroups.flatMap((group) => [
                { type: 'year', group },
                ...(expandedMerchantYearGroups[group.year] ? group.items.map((item) => ({ type: 'merchant', item, archived: true })) : []),
            ]),
        ];
    $: merchantVisibleCount = merchantRecentItems.length;
    $: merchantVisibleSpend = merchantRecentItems.reduce((sum, item) => sum + Number(item.total_spent || 0), 0);
    $: merchantSubscriptionCount = visibleMerchants.filter((item) => !!item.is_subscription).length;
    $: merchantScopeLabel = activeProfileId === 'household' ? 'Household' : activeProfileId;
    $: if (visibleMerchants.length === 0) {
        selectedMerchantKey = null;
        expandedMerchantKey = null;
    } else if (!visibleMerchants.some((item) => merchantRowKey(item) === selectedMerchantKey)) {
        selectedMerchantKey = merchantRowKey(visibleMerchants[0]);
    }
    $: if (expandedMerchantKey && !visibleMerchants.some((item) => merchantRowKey(item) === expandedMerchantKey)) {
        expandedMerchantKey = null;
    }
    $: if (openMerchantCategoryMenuKey && !visibleMerchants.some((item) => merchantRowKey(item) === openMerchantCategoryMenuKey)) {
        openMerchantCategoryMenuKey = null;
    }
    $: selectedMerchant = visibleMerchants.find((item) => merchantRowKey(item) === selectedMerchantKey) || null;
    $: if (selectedMerchant) {
        const nextKey = merchantRowKey(selectedMerchant);
        if (nextKey !== lastMerchantPreviewKey) {
            lastMerchantPreviewKey = nextKey;
            loadSelectedMerchantTransactions(selectedMerchant);
        }
    } else if (lastMerchantPreviewKey !== null) {
        lastMerchantPreviewKey = null;
        merchantTransactions = [];
    }

    // ── Rules derived ────────────────────────────────────────────
    $: visibleRules = ruleItems.filter((item) => {
        const sourceOk = ruleSourceFilter === 'all' || item.source === ruleSourceFilter;
        const stateOk = ruleStateFilter === 'working'
            ? item.is_active || item.source !== 'system'
            : ruleStateFilter === 'all'
            || (ruleStateFilter === 'active'   && !!item.is_active)
            || (ruleStateFilter === 'inactive' && !item.is_active);
        const searchOk = !ruleSearch.trim()
            || [item.pattern, item.category, item.source, item.match_type]
                .filter(Boolean)
                .some((value) => String(value).toLowerCase().includes(ruleSearch.trim().toLowerCase()));
        return sourceOk && stateOk && searchOk;
    });
    $: if (visibleRules.length === 0) {
        selectedRuleId = null;
    } else if (!visibleRules.some((item) => item.id === selectedRuleId)) {
        selectedRuleId = visibleRules[0].id;
    }
    $: selectedRule = visibleRules.find((item) => item.id === selectedRuleId) || null;
    $: hiddenPausedSystemRules = ruleItems.filter((item) => item.source === 'system' && !item.is_active).length;
    $: if (selectedRule && ruleDraft.id !== selectedRule.id) {
        ruleDraft = {
            id: selectedRule.id,
            category: selectedRule.category || '',
            priority: selectedRule.priority ?? 1000,
            is_active: !!selectedRule.is_active,
        };
    }
    $: if (selectedRule) {
        if (selectedRule.id !== lastRuleImpactId) {
            lastRuleImpactId = selectedRule.id;
            loadRuleImpact(selectedRule.id);
        }
    } else if (lastRuleImpactId !== null) {
        lastRuleImpactId = null;
        ruleImpact = null;
    }

    // ── Categories derived ───────────────────────────────────────
    $: visibleCategories = categoriesMeta.filter((item) => {
        if (!item.is_active) return false;
        if (Number(item.transaction_count || 0) <= 0) return false;
        if (!categorySearch.trim()) return true;
        const needle = categorySearch.trim().toLowerCase();
        return [item.name, item.parent_category, item.expense_type]
            .filter(Boolean)
            .some((value) => String(value).toLowerCase().includes(needle));
    });

    // ── History derived ──────────────────────────────────────────
    $: visibleHistory = historyItems.filter((item) => {
        if (!historySearch.trim()) return true;
        const needle = historySearch.trim().toLowerCase();
        return [item.user_message, item.assistant_response, item.operation_type, item.generated_sql, item.query_result]
            .filter(Boolean)
            .some((value) => String(value).toLowerCase().includes(needle));
    });
    $: if (visibleHistory.length === 0) {
        selectedHistoryId = null;
    } else if (!visibleHistory.some((item) => item.id === selectedHistoryId)) {
        selectedHistoryId = visibleHistory[0].id;
    }
    $: selectedHistory = visibleHistory.find((item) => item.id === selectedHistoryId) || null;
    $: localLlmTierGroups = Array.isArray(localLlmCatalog?.tiers) ? localLlmCatalog.tiers : [];
    $: localLlmRecommendationCount = localLlmTierGroups.reduce((sum, group) => sum + (group.models?.length || 0), 0);
    $: localLlmInstalledCount = Array.isArray(localLlmStatus?.installedModels) ? localLlmStatus.installedModels.length : 0;
    $: activeCategorizeModelMeta = localLlmCatalog?.models?.[localLlmStatus.selectedCategorizeModel] || null;
    $: activeCopilotModelMeta = localLlmCatalog?.models?.[localLlmStatus.selectedCopilotModel] || null;
    $: activeAdvisorModelMeta = localLlmCatalog?.models?.[localLlmStatus.selectedAdvisorModel] || null;
    $: investmentAccounts = accountItems.filter((account) => account.provider === 'manual' && account.account_type === 'investment');
    $: holdings = Array.isArray(investmentsData?.holdings) ? investmentsData.holdings : [];
    $: allocation = Array.isArray(investmentsData?.allocation) ? investmentsData.allocation : [];
    $: investmentSummary = investmentsData?.summary || {};
    $: connectionCount = tellerEnrollments.length + simplefinConnections.length;
    $: syncedAccountCount = accountItems.filter((account) => account.provider !== 'manual').length;
    $: manualAccountCount = accountItems.filter((account) => account.provider === 'manual').length;
    $: activeRuleCount = ruleItems.filter((item) => item.is_active).length;
    $: editableCategoryCount = categoriesMeta.filter((item) => item.is_active && item.expense_type !== 'non_expense').length;
    $: userRuleCount = visibleRules.filter((item) => item.source === 'user').length;
    $: systemRuleCount = visibleRules.filter((item) => item.source !== 'user').length;
    $: fixedCategoryCount = visibleCategories.filter((item) => item.expense_type === 'fixed').length;
    $: variableCategoryCount = visibleCategories.filter((item) => item.expense_type === 'variable').length;
    $: lockedCategoryCount = visibleCategories.filter((item) => item.expense_type === 'non_expense').length;
    $: historyWriteCount = visibleHistory.filter((item) => Number(item.rows_affected || 0) > 0).length;
    $: historyReadCount = Math.max(visibleHistory.length - historyWriteCount, 0);
    $: localRuntimeMode = localLlmForm.expert_mode ? 'Advanced' : (localLlmForm.low_power_mode ? 'Low power' : 'Default');
    $: primaryMiraModel = localLlmStatus.selectedCopilotModel || localLlmStatus.selectedCategorizeModel || 'No model selected';
    $: miraRuntimeLabel = localLlmStatus.ollamaReachable ? 'Ready' : 'Offline';
    $: miraRuntimeMeta = localLlmStatus.ollamaReachable ? primaryMiraModel : 'Ollama unavailable';
    $: tabCounts = {
        connections: connectionCount,
        accounts: accountItems.length,
        investments: holdings.length,
        merchants: merchantVisibleCount,
        rules: visibleRules.length,
        categories: visibleCategories.length || editableCategoryCount,
        history: visibleHistory.length,
    };
    $: controlCenterSignals = [
        {
            label: 'Data health',
            value: dataHealth?.status === 'healthy' ? 'Healthy' : dataHealth ? 'Needs attention' : 'Checking',
            meta: `${dataHealth?.summary?.warnings || 0} warning${(dataHealth?.summary?.warnings || 0) === 1 ? '' : 's'}`,
            tone: dataHealth?.status === 'healthy' ? 'positive' : dataHealth ? 'warning' : 'muted',
        },
        {
            label: 'Sources',
            value: `${connectionCount}`,
            meta: `${syncedAccountCount} synced account${syncedAccountCount === 1 ? '' : 's'}`,
            tone: 'accent',
        },
        {
            label: 'Rules',
            value: `${activeRuleCount}`,
            meta: `${hiddenPausedSystemRules} paused default${hiddenPausedSystemRules === 1 ? '' : 's'}`,
            tone: 'warning',
        },
        {
            label: 'Mira runtime',
            value: miraRuntimeLabel,
            meta: miraRuntimeMeta,
            tone: localLlmStatus.ollamaReachable ? 'positive' : 'muted',
        },
    ];

    // ── Lifecycle ────────────────────────────────────────────────
    onMount(async () => {
        try {
            appConfig = { ...appConfig, ...(await api.getAppConfig()) };
        } catch (_) {}
        lastLoadedProfile = activeProfileId;
        await refreshAll();
    });

    $: if (activeProfileId && lastLoadedProfile !== undefined && activeProfileId !== lastLoadedProfile) {
        lastLoadedProfile = activeProfileId;
        refreshAll();
    }

    // ── Utilities ────────────────────────────────────────────────
    function setNotice(message) {
        notice = message;
        clearTimeout(noticeTimeout);
        noticeTimeout = setTimeout(() => {
            if (notice === message) notice = '';
        }, 3200);
    }

    function handleWindowClick() {
        openMerchantCategoryMenuKey = null;
        merchantFilterMenuOpen = false;
    }

    function setTab(tab) {
        if (!tabKeys.has(tab)) return;
        activeTab = tab;
        const params = new URLSearchParams($page.url.searchParams);
        if (tab === 'merchants') {
            params.delete('tab');
        } else {
            params.set('tab', tab);
        }
        const query = params.toString();
        goto(query ? `/control-center?${query}` : '/control-center', {
            replaceState: true,
            noScroll: true,
            keepFocus: true,
        });
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
            minute: '2-digit',
        });
    }

    function ruleSourceBadgeClass(source) {
        return source === 'user' ? 'cc-badge cc-badge-info' : 'cc-badge cc-badge-muted';
    }

    function ruleSourceLabel(source) {
        return source === 'user' ? 'User' : 'System';
    }

    function ruleStatusBadge(rule) {
        return rule?.is_active ? 'cc-badge cc-badge-positive' : 'cc-badge cc-badge-muted';
    }

    function removalKey(type, id) {
        return `${type}:${id}`;
    }

    function requestRemoval(type, id) {
        const key = removalKey(type, id);
        pendingRemovalKey = pendingRemovalKey === key ? null : key;
    }

    function clearRemoval() {
        pendingRemovalKey = null;
    }

    // ── Data loaders ─────────────────────────────────────────────
    async function refreshAll() {
        loading = true;
        try {
            await Promise.all([
                loadConnections(),
                loadAccounts(),
                loadInvestments(),
                loadMerchants(),
                loadRules(),
                loadCategories(),
                loadHistory(),
            ]);
        } finally {
            loading = false;
        }
    }

    async function loadConnections() {
        connectionsLoading = true;
        localLlmLoading = true;
        localLlmError = '';
        try {
            const [enrollments, sfConns, config] = await Promise.all([
                api.getEnrollments().catch(() => []),
                api.getSimpleFINConnections().catch(() => []),
                api.getTellerConfig().catch(() => null),
            ]);
            dataHealth = await api.getDataHealth().catch(() => null);
            backupStatus = await api.getBackupStatus().catch(() => null);
            const [llmCatalogResult, llmStatusResult] = await Promise.allSettled([
                api.getLocalLlmCatalog(),
                api.getLocalLlmStatus(),
            ]);
            tellerEnrollments = enrollments || [];
            simplefinConnections = sfConns || [];
            tellerConfig = config;

            if (llmCatalogResult.status === 'fulfilled') {
                localLlmCatalog = llmCatalogResult.value || localLlmCatalog;
            }
            if (llmStatusResult.status === 'fulfilled') {
                localLlmStatus = llmStatusResult.value || localLlmStatus;
            }
            if (llmCatalogResult.status === 'rejected' || llmStatusResult.status === 'rejected') {
                const error = llmCatalogResult.status === 'rejected'
                    ? llmCatalogResult.reason
                    : llmStatusResult.reason;
                localLlmError = error?.message || 'Failed to load Mira Runtime status from the backend.';
            }
            syncLocalLlmForm(localLlmStatus);
        } finally {
            connectionsLoading = false;
            localLlmLoading = false;
        }
    }

    async function loadAccounts() {
        accountsLoading = true;
        try {
            accountItems = await api.getAccounts().catch(() => []);
            manualAccountEdits = {};
        } finally {
            accountsLoading = false;
        }
    }

    function isCreditCardAccount(account) {
        const accountType = (account?.account_type || '').toLowerCase();
        return accountType === 'credit' || accountType === 'credit_card';
    }

    function dueDayDraftValue(account) {
        return account?.usual_due_day ? String(account.usual_due_day) : '';
    }

    function formatOrdinalDay(day) {
        const value = Number(day);
        if (!Number.isInteger(value) || value < 1 || value > 31) return '';
        const lastTwo = value % 100;
        if (lastTwo >= 11 && lastTwo <= 13) return `${value}th`;
        const lastDigit = value % 10;
        if (lastDigit === 1) return `${value}st`;
        if (lastDigit === 2) return `${value}nd`;
        if (lastDigit === 3) return `${value}rd`;
        return `${value}th`;
    }

    function paymentDueLabel(account) {
        const ordinal = formatOrdinalDay(account?.usual_due_day);
        return ordinal ? `Due ${ordinal}` : '';
    }

    function parseDueDayDraft(value) {
        if (value === '' || value === null || value === undefined) return null;
        const day = Number(value);
        if (!Number.isInteger(day) || day < 1 || day > 31) return undefined;
        return day;
    }

    function isDueDayDraftValid(accountId) {
        return parseDueDayDraft(accountPaymentEdits[accountId]?.usual_due_day) !== undefined;
    }

    function startAccountPaymentEdit(account) {
        if (!isCreditCardAccount(account)) return;
        accountPaymentEdits = {
            ...accountPaymentEdits,
            [account.id]: {
                usual_due_day: dueDayDraftValue(account)
            }
        };
    }

    function toggleAccountPaymentEdit(account) {
        if (accountPaymentEdits[account.id]) {
            cancelAccountPaymentEdit(account.id);
            return;
        }
        startAccountPaymentEdit(account);
    }

    function cancelAccountPaymentEdit(id) {
        const next = { ...accountPaymentEdits };
        delete next[id];
        accountPaymentEdits = next;
        if (linkedAccountEditId === id) lastLinkedAccountEditId = id;
    }

    async function saveAccountPaymentDetails(account) {
        const draft = accountPaymentEdits[account.id];
        const dueDay = parseDueDayDraft(draft?.usual_due_day);
        if (dueDay === undefined) {
            setNotice('Usual due day must be a whole number from 1 to 31.');
            return;
        }
        accountPaymentSavingId = account.id;
        try {
            const result = await api.updateAccountPaymentDetails(account.id, { usual_due_day: dueDay }, scopedProfile);
            const savedDueDay = result?.account?.usual_due_day ?? dueDay;
            accountItems = accountItems.map((item) =>
                item.id === account.id ? { ...item, usual_due_day: savedDueDay } : item
            );
            accountPaymentSavedId = account.id;
            cancelAccountPaymentEdit(account.id);
            invalidateCache();
            setNotice(savedDueDay ? `Payment details saved: due ${formatOrdinalDay(savedDueDay)}.` : 'Payment due day cleared.');
        } catch (e) {
            setNotice(e?.message || 'Failed to update payment details.');
        } finally {
            accountPaymentSavingId = null;
        }
    }

    async function saveManualAccount() {
        if (!manualAccountDraft.name.trim() || manualAccountSaving) return;
        manualAccountSaving = true;
        try {
            await api.createManualAccount({
                name: manualAccountDraft.name.trim(),
                account_type: manualAccountDraft.account_type,
                account_subtype: manualAccountDraft.account_subtype || 'manual',
                balance: Number(manualAccountDraft.balance || 0),
                notes: manualAccountDraft.notes || ''
            }, scopedProfile);
            manualAccountDraft = { name: '', account_type: 'investment', account_subtype: 'manual', balance: '', notes: '' };
            invalidateCache();
            await loadAccounts();
            setNotice('Manual account added.');
        } catch (e) {
            setNotice(e?.message || 'Failed to add manual account.');
        } finally {
            manualAccountSaving = false;
        }
    }

    async function removeManualAccount(id) {
        const key = removalKey('manual-account', id);
        if (removalSavingKey) return;
        removalSavingKey = key;
        try {
            await api.deleteManualAccount(id, scopedProfile);
            invalidateCache();
            await loadAccounts();
            setNotice('Manual account removed.');
        } catch (e) {
            setNotice(e?.message || 'Failed to remove manual account.');
        } finally {
            if (pendingRemovalKey === key) pendingRemovalKey = null;
            if (removalSavingKey === key) removalSavingKey = null;
        }
    }

    function startManualAccountEdit(account) {
        manualAccountEdits = {
            ...manualAccountEdits,
            [account.id]: {
                name: account.name || '',
                account_type: account.account_type || (account.is_credit ? 'loan' : 'investment'),
                account_subtype: account.type || 'manual',
                balance: Math.abs(Number(account.balance || 0)).toString(),
                notes: account.manual_notes || ''
            }
        };
    }

    async function saveManualAccountEdit(account) {
        const draft = manualAccountEdits[account.id];
        if (!draft || manualAccountSavingId) return;
        manualAccountSavingId = account.id;
        try {
            await api.updateManualAccount(account.id, {
                name: draft.name.trim() || account.name,
                account_type: draft.account_type,
                account_subtype: draft.account_subtype || 'manual',
                balance: Number(draft.balance || 0),
                notes: draft.notes || ''
            }, scopedProfile);
            invalidateCache();
            await loadAccounts();
            setNotice('Manual balance updated.');
        } catch (e) {
            setNotice(e?.message || 'Failed to update manual account.');
        } finally {
            manualAccountSavingId = null;
        }
    }

    function cancelManualAccountEdit(id) {
        const next = { ...manualAccountEdits };
        delete next[id];
        manualAccountEdits = next;
    }

    function syncLocalLlmForm(status) {
        if (!status) return;
        localLlmForm = {
            llm_provider: 'ollama',
            preset: status.preset || 'default',
            categorize_model: status.selectedCategorizeModel || '',
            copilot_model: status.selectedCopilotModel || '',
            low_power_mode: !!status.lowPowerMode,
            expert_mode: !!status.expertMode,
            categorize_batch_size: status.categorizeBatchSize ?? 25,
            inter_batch_delay_ms: status.interBatchDelayMs ?? 600,
        };
    }

    function localModelSize(model) {
        return model?.download_size_gb ?? model?.approx_size_gb ?? null;
    }

    function localModelOptionLabel(model) {
        const size = localModelSize(model);
        const sizeLabel = size ? ` · ${size} GB` : '';
        const quantLabel = model?.quantization ? ` · ${model.quantization}` : '';
        const installedLabel = model?.installed ? ' · installed' : ' · not installed';
        return `${model?.label || model?.id}${sizeLabel}${quantLabel}${installedLabel}`;
    }

    function modelSupports(model, task) {
        return Array.isArray(model?.task_fit) && model.task_fit.includes(task);
    }

    function modelRequiresAdvanced(model) {
        return !!(model?.expert_only || model?.advanced_only || model?.validated_for_mira !== true);
    }

    function applyLocalLlmModel(target, modelId) {
        if (!modelId) return;
        localLlmForm = {
            ...localLlmForm,
            [target]: modelId,
        };
    }

    async function saveLocalLlmSettings() {
        if (localLlmSaving) return;
        localLlmSaving = true;
        try {
            const result = await api.updateLocalLlmSettings(localLlmForm);
            if (result?.status) {
                localLlmStatus = result.status;
                syncLocalLlmForm(localLlmStatus);
            }
            if (result?.config) {
                appConfig = { ...appConfig, ...result.config };
            }
            invalidateCache();
            setNotice('Mira Runtime settings updated.');
            await loadConnections();
        } catch (error) {
            setNotice(error?.message || 'Failed to update Mira Runtime settings.');
        } finally {
            localLlmSaving = false;
        }
    }

    async function installLocalLlmModel(modelId) {
        if (!modelId || localLlmInstallingModel || localLlmStatus.provider !== 'ollama' || !localLlmStatus.ollamaReachable) return;
        localLlmInstallingModel = modelId;
        localLlmError = '';
        try {
            const result = await api.installLocalLlmModel(modelId);
            if (result?.status) {
                localLlmStatus = result.status;
                syncLocalLlmForm(localLlmStatus);
            }
            if (result?.config) {
                appConfig = { ...appConfig, ...result.config };
            }
            await loadConnections();
            setNotice(`${modelId} installed in Ollama.`);
        } catch (error) {
            localLlmError = error?.message || `Failed to install ${modelId}.`;
            setNotice(localLlmError);
        } finally {
            localLlmInstallingModel = '';
        }
    }

    async function handleDeactivateEnrollment(id) {
        const key = removalKey('teller', id);
        if (removalSavingKey) return;
        removalSavingKey = key;
        try {
            await api.deactivateEnrollment(id);
            setNotice('Teller enrollment deactivated.');
            invalidateCache();
            await loadConnections();
        } catch (e) {
            setNotice('Failed to deactivate enrollment.');
        } finally {
            if (pendingRemovalKey === key) pendingRemovalKey = null;
            if (removalSavingKey === key) removalSavingKey = null;
        }
    }

    async function handleDeactivateSFConnection(id) {
        const key = removalKey('simplefin', id);
        if (removalSavingKey) return;
        removalSavingKey = key;
        try {
            await api.deactivateSimpleFINConnection(id);
            setNotice('SimpleFIN connection deactivated.');
            invalidateCache();
            await loadConnections();
        } catch (e) {
            setNotice('Failed to deactivate connection.');
        } finally {
            if (pendingRemovalKey === key) pendingRemovalKey = null;
            if (removalSavingKey === key) removalSavingKey = null;
        }
    }

    function handleSimpleFINConnected() {
        invalidateCache();
        loadProfiles();
        loadConnections();
    }

    async function loadMerchants(searchOverride = merchantSearch) {
        merchantsLoading = true;
        try {
            if (searchOverride !== merchantSearch) merchantSearch = searchOverride;
            const result = await api.getMerchantDirectory(searchOverride || '', 250);
            merchantItems = result?.items || [];
            merchantAliasDrafts = merchantItems.reduce((drafts, item) => {
                drafts[merchantRowKey(item)] = item.clean_name || item.merchant_key || '';
                return drafts;
            }, {});
            return merchantItems;
        } catch (error) {
            merchantItems = [];
            merchantAliasDrafts = {};
            setNotice('Failed to load merchants.');
            return [];
        } finally {
            merchantsLoading = false;
        }
    }

    async function loadInvestments() {
        investmentsLoading = true;
        try {
            investmentsData = await api.getInvestments();
        } catch (_) {
            investmentsData = { holdings: [], allocation: [], summary: {} };
        } finally {
            investmentsLoading = false;
        }
    }

    function normalizeHoldingPayload(source) {
        return {
            account_id: source.account_id || null,
            symbol: source.symbol || '',
            name: source.name || source.symbol || '',
            asset_class: source.asset_class || 'other',
            quantity: Number(source.quantity || 0),
            cost_basis: Number(source.cost_basis || 0),
            current_price: Number(source.current_price || 0),
            manual_value: source.manual_value === '' || source.manual_value == null ? null : Number(source.manual_value),
            target_percent: source.target_percent === '' || source.target_percent == null ? null : Number(source.target_percent),
            notes: source.notes || ''
        };
    }

    function resetHoldingDraft() {
        holdingDraft = {
            account_id: '',
            symbol: '',
            name: '',
            asset_class: 'stock',
            quantity: '',
            cost_basis: '',
            current_price: '',
            manual_value: '',
            target_percent: '',
            notes: ''
        };
        holdingEditId = null;
    }

    function editHolding(item) {
        holdingEditId = item.id;
        holdingDraft = {
            account_id: item.account_id || '',
            symbol: item.symbol || '',
            name: item.name || '',
            asset_class: item.asset_class || 'other',
            quantity: item.quantity ?? '',
            cost_basis: item.cost_basis ?? '',
            current_price: item.current_price ?? '',
            manual_value: item.manual_value ?? '',
            target_percent: item.target_percent ?? '',
            notes: item.notes || ''
        };
        setTab('investments');
    }

    async function saveHolding() {
        if (holdingSaving || !(holdingDraft.name || holdingDraft.symbol).trim()) return;
        holdingSaving = true;
        try {
            const payload = normalizeHoldingPayload(holdingDraft);
            if (holdingEditId) {
                await api.updateInvestmentHolding(holdingEditId, payload);
                setNotice('Holding updated.');
            } else {
                await api.createInvestmentHolding(payload);
                setNotice('Holding added.');
            }
            resetHoldingDraft();
            invalidateCache();
            await Promise.all([loadInvestments(), loadAccounts()]);
        } catch (error) {
            setNotice(error?.message || 'Failed to save holding.');
        } finally {
            holdingSaving = false;
        }
    }

    async function removeHolding(id) {
        if (!id) return;
        const key = removalKey('holding', id);
        if (removalSavingKey) return;
        removalSavingKey = key;
        try {
            await api.deleteInvestmentHolding(id);
            setNotice('Holding removed.');
            invalidateCache();
            await loadInvestments();
        } catch (error) {
            setNotice(error?.message || 'Failed to remove holding.');
        } finally {
            if (pendingRemovalKey === key) pendingRemovalKey = null;
            if (removalSavingKey === key) removalSavingKey = null;
        }
    }

    async function exportBackup() {
        if (backupExporting) return;
        backupExporting = true;
        try {
            const { blob, filename } = await api.exportBackup(false);
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = filename;
            document.body.appendChild(a);
            a.click();
            a.remove();
            URL.revokeObjectURL(url);
            setNotice('Backup export created.');
        } catch (error) {
            setNotice(error?.message || 'Failed to export backup.');
        } finally {
            backupExporting = false;
        }
    }

    function merchantInitials(item) {
        const label = item.clean_name || item.merchant_key || '?';
        return label.split(/\s+/).filter(Boolean).slice(0, 2).map((part) => part[0]).join('').toUpperCase() || '?';
    }

    async function loadSelectedMerchantTransactions(item) {
        if (!item) return;
        merchantPreviewLoading = true;
        try {
            const result = await api.getMerchantTransactions(item.merchant_key, item.profile_id, 25);
            merchantTransactions = result?.items || [];
        } catch (error) {
            merchantTransactions = [];
        } finally {
            merchantPreviewLoading = false;
        }
    }

    async function loadRules() {
        rulesLoading = true;
        try {
            const result = await api.getCategoryRules();
            ruleItems = Array.isArray(result) ? result : [];
            return ruleItems;
        } catch (error) {
            ruleItems = [];
            setNotice('Failed to load rules.');
            return [];
        } finally {
            rulesLoading = false;
        }
    }

    async function loadRuleImpact(ruleId) {
        if (!ruleId) return;
        ruleImpactLoading = true;
        try {
            ruleImpact = await api.getCategoryRuleImpact(ruleId, 20);
        } catch (error) {
            ruleImpact = null;
        } finally {
            ruleImpactLoading = false;
        }
    }

    async function loadCategories() {
        categoriesLoading = true;
        try {
            const result = await api.getCategoriesMeta();
            categoriesMeta = Array.isArray(result) ? result : [];
            return categoriesMeta;
        } catch (error) {
            categoriesMeta = [];
            setNotice('Failed to load categories.');
            return [];
        } finally {
            categoriesLoading = false;
        }
    }

    async function loadHistory() {
        historyLoading = true;
        try {
            const result = await api.getCopilotHistory(80);
            historyItems = result?.items || [];
            return historyItems;
        } catch (error) {
            historyItems = [];
            setNotice('Failed to load history.');
            return [];
        } finally {
            historyLoading = false;
        }
    }

    // ── Save actions ─────────────────────────────────────────────
    function merchantAliasDraftValue(item) {
        return merchantAliasDrafts[merchantRowKey(item)] ?? (item.clean_name || item.merchant_key || '');
    }

    function updateMerchantAliasDraft(item, value) {
        merchantAliasDrafts = {
            ...merchantAliasDrafts,
            [merchantRowKey(item)]: value,
        };
    }

    async function saveMerchantChanges(item, payload, successMessage) {
        if (!item || merchantSaving) return;
        merchantSaving = true;
        try {
            const result = await api.updateMerchantDirectory(item.merchant_key, {
                profile_id: item.profile_id,
                clean_name: null,
                category: null,
                domain: null,
                industry: null,
                ...payload,
            });
            invalidateCache();
            await loadMerchants();
            if (selectedMerchantKey === merchantRowKey(item)) {
                await loadSelectedMerchantTransactions(item);
            }
            setNotice(successMessage(result));
        } catch (error) {
            setNotice('Failed to update merchant.');
        } finally {
            merchantSaving = false;
        }
    }

    async function applyMerchantCategory(item, rowKey, categoryName) {
        openMerchantCategoryMenuKey = null;
        merchantCategorySearch = '';
        if (!item || merchantSaving) return;
        if ((item.category || '') === (categoryName || '')) return;
        selectedMerchantKey = rowKey;
        await saveMerchantChanges(
            item,
            { category: categoryName },
            (result) => {
                const touched = result?.merchant?.retroactive_count ?? 0;
                return touched > 0
                    ? `Category applied to ${touched} matching transactions.`
                    : 'Merchant category updated.';
            },
        );
    }

    async function saveMerchantAlias(item, rowKey) {
        if (!item || merchantSaving) return;
        const nextAlias = merchantAliasDraftValue(item).trim();
        const currentAlias = (item.clean_name || item.merchant_key || '').trim();
        if ((nextAlias || item.merchant_key) === currentAlias) return;
        selectedMerchantKey = rowKey;
        await saveMerchantChanges(
            item,
            { clean_name: nextAlias || item.merchant_key },
            () => 'Merchant name updated.',
        );
    }

    function handleMerchantRowClick(rowKey) {
        openMerchantCategoryMenuKey = null;
        merchantCategorySearch = '';
        if (selectedMerchantKey === rowKey) {
            expandedMerchantKey = expandedMerchantKey === rowKey ? null : rowKey;
            return;
        }
        selectedMerchantKey = rowKey;
        expandedMerchantKey = rowKey;
    }

    function handleMerchantRowKeydown(event, rowKey) {
        if (event.target !== event.currentTarget) return;
        if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            handleMerchantRowClick(rowKey);
        }
    }

    function toggleMerchantCategoryMenu(item, rowKey) {
        merchantFilterMenuOpen = false;
        merchantCategorySearch = '';
        selectedMerchantKey = rowKey;
        openMerchantCategoryMenuKey = openMerchantCategoryMenuKey === rowKey ? null : rowKey;
    }

    async function saveRule() {
        if (!selectedRule) return;
        ruleSaving = true;
        try {
            await api.updateCategoryRule(selectedRule.id, {
                category: ruleDraft.category,
                priority: Number(ruleDraft.priority),
                is_active: !!ruleDraft.is_active,
            });
            invalidateCache();
            await loadRules();
            await loadRuleImpact(selectedRule.id);
            setNotice('Rule updated.');
        } catch (error) {
            setNotice('Failed to update rule.');
        } finally {
            ruleSaving = false;
        }
    }

    async function saveExpenseTypeInline(item, newType) {
        if (!item || item.expense_type === 'non_expense') return;
        if (item.expense_type === newType) return;
        categorySavingKey = item.name;
        try {
            await api.updateExpenseType(item.name, newType);
            invalidateCache();
            // Update in place to avoid full reload flash
            categoriesMeta = categoriesMeta.map((c) =>
                c.name === item.name ? { ...c, expense_type: newType, expense_type_source: 'user' } : c
            );
        } catch (error) {
            setNotice('Failed to update expense type.');
            await loadCategories();
        } finally {
            if (categorySavingKey === item.name) categorySavingKey = null;
        }
    }

    function replacementOptionsFor(item) {
        return categoriesMeta
            .filter((candidate) => candidate.is_active && candidate.name !== item.name)
            .map((candidate) => candidate.name);
    }

    function categoryReplacementValue(item) {
        return categoryReplacementDrafts[item.name] || '';
    }

    function updateCategoryReplacement(item, value) {
        categoryReplacementDrafts = {
            ...categoryReplacementDrafts,
            [item.name]: value,
        };
        openCategoryReplacementKey = null;
        categoryReplacementSearch = '';
        if (value) {
            categoryReplacementRequiredKey = null;
            clearRemoval();
        }
    }

    function toggleCategoryReplacement(item) {
        if (!item) return;
        categoryReplacementSearch = '';
        openCategoryReplacementKey = openCategoryReplacementKey === item.name ? null : item.name;
    }

    function requestCategoryRemoval(item) {
        if (!item || item.is_system) return;
        const txCount = Number(item.transaction_count || 0);
        if (txCount > 0 && !categoryReplacementValue(item)) {
            categoryReplacementRequiredKey = item.name;
            openCategoryReplacementKey = item.name;
            return;
        }
        categoryReplacementRequiredKey = null;
        requestRemoval('category', item.name);
    }

    async function removeCustomCategory(item) {
        if (!item || item.is_system) return;
        const key = removalKey('category', item.name);
        if (removalSavingKey) return;
        const txCount = Number(item.transaction_count || 0);
        const replacement = txCount > 0 ? categoryReplacementValue(item) : '';
        if (txCount > 0 && !replacement) {
            categoryReplacementRequiredKey = item.name;
            return;
        }
        removalSavingKey = key;
        try {
            const result = await api.deleteCategory(item.name, replacement || null);
            invalidateCache();
            await Promise.all([loadCategories(), loadRules()]);
            const moved = result?.transactions_moved || 0;
            setNotice(moved > 0 ? `Removed category and moved ${moved} transactions.` : 'Category removed.');
        } catch (error) {
            setNotice(error?.message || 'Failed to remove category.');
        } finally {
            if (pendingRemovalKey === key) pendingRemovalKey = null;
            if (removalSavingKey === key) removalSavingKey = null;
        }
    }

    async function reopenInCopilot() {
        if (!selectedHistory?.user_message) return;
        const params = new URLSearchParams();
        params.set('prompt', selectedHistory.user_message);
        await goto(`/copilot?${params.toString()}`);
    }
</script>

<svelte:window on:click={handleWindowClick} />

<div class="cc-page">
    <div class="cc-header fade-in">
        <div class="cc-header-top">
            <div class="cc-title-wrap">
                <div class="cc-hero-icon">
                    <span class="material-symbols-outlined">tune</span>
                </div>
                <div>
                    <h2 class="folio-page-title">Control Center</h2>
                    <p class="folio-page-subtitle">Tune the sources, rules, and local intelligence Folio uses to understand your money.</p>
                </div>
            </div>
            <div class="cc-header-actions">
                <ProfileSwitcher />
            </div>
        </div>
        <div class="cc-hero-signals" aria-label="Control Center status">
            {#each controlCenterSignals as signal}
                <div class="cc-hero-signal cc-hero-signal-{signal.tone}">
                    <span>{signal.label}</span>
                    <strong>{signal.value}</strong>
                    <small>{signal.meta}</small>
                </div>
            {/each}
        </div>
    </div>

    {#if appConfig.demoMode}
        <div class="cc-notice fade-in">Demo mode is active. Connections and sync are disabled, but merchant/category edits still work until the demo resets.</div>
    {/if}

    <div class="cc-tabbar fade-in-up">
        {#each visibleTabs as tab}
            <button type="button" class="cc-tab" class:cc-tab-active={activeTab === tab.key} on:click={() => setTab(tab.key)}>
                <span>{tab.label}</span>
                <small>{tabCounts[tab.key] ?? 0}</small>
            </button>
        {/each}
    </div>

    {#if notice}
        <div class="cc-notice fade-in">{notice}</div>
    {/if}

    {#if loading}
        <div class="cc-empty">Loading…</div>

    {:else if activeTab === 'connections'}
        <!-- ── CONNECTIONS ───────────────────────────────────────── -->
        <div class="cc-ops-grid cc-connections-layout fade-in-up">
        <section class="cc-pane cc-pane-primary cc-bank-pane">
            <div class="cc-pane-header">
                <div class="cc-pane-title">
                    <h3>Connections & Sync</h3>
                    <p>Keep the account sources Folio listens to healthy, encrypted, and easy to recover.</p>
                </div>
                {#if tellerEnrollments.length > 0 && simplefinConnections.length > 0}
                    <button
                        type="button"
                        class="cc-secondary-btn"
                        on:click={() => migrationRef.show()}
                        title="Migrate from Teller to SimpleFIN"
                    >
                        <span class="material-symbols-outlined text-[14px]">swap_horiz</span>
                        Migrate to SimpleFIN
                    </button>
                {/if}
            </div>
            <div class="cc-calibration-strip">
                <div class="cc-calibration-item">
                    <span>Linked sources</span>
                    <strong>{connectionCount}</strong>
                    <small>{tellerEnrollments.length} Teller · {simplefinConnections.length} SimpleFIN</small>
                </div>
                <div class="cc-calibration-item">
                    <span>Account feed</span>
                    <strong>{syncedAccountCount}</strong>
                    <small>{dataHealth?.summary?.stale_accounts || 0} stale</small>
                </div>
                <div class="cc-calibration-item">
                    <span>Recovery</span>
                    <strong>{backupStatus?.export_format?.toUpperCase() || 'JSON'}</strong>
                    <small>{backupStatus?.db_size_bytes ? `${(backupStatus.db_size_bytes / 1024 / 1024).toFixed(1)} MB` : 'Backup ready'}</small>
                </div>
            </div>

            {#if dataHealth}
                <div class="cc-conn-section">
                    <div class="cc-conn-section-header">
                        <div class="cc-conn-section-title">
                            <span class="cc-provider-badge">Data Health</span>
                            <span class="cc-conn-count">{dataHealth.summary?.warnings || 0} warning{(dataHealth.summary?.warnings || 0) !== 1 ? 's' : ''}</span>
                        </div>
                        <span class="cc-badge" class:cc-badge-positive={dataHealth.status === 'healthy'} class:cc-badge-muted={dataHealth.status !== 'healthy'}>
                            {dataHealth.status === 'healthy' ? 'Healthy' : 'Needs attention'}
                        </span>
                    </div>
                    <div class="cc-local-llm-grid">
                        <div class="cc-local-llm-stat">
                            <span class="cc-insight-label">Accounts</span>
                            <strong>{dataHealth.summary?.total_accounts || 0}</strong>
                            <small>{dataHealth.summary?.stale_accounts || 0} stale</small>
                        </div>
                        <div class="cc-local-llm-stat">
                            <span class="cc-insight-label">Latest Update</span>
                            <strong>{dataHealth.summary?.latest_account_update ? formatDate(dataHealth.summary.latest_account_update) : '—'}</strong>
                            <small>Across active accounts</small>
                        </div>
                        <div class="cc-local-llm-stat">
                            <span class="cc-insight-label">Credential Storage</span>
                            <strong>
                                {dataHealth.encryption?.status === 'encrypted'
                                    ? 'Encrypted'
                                    : 'Encryption setup needed'}
                            </strong>
                            <small>
                                {dataHealth.encryption?.status === 'encrypted'
                                    ? 'TOKEN_ENCRYPTION_KEY active'
                                    : dataHealth.encryption?.key_present
                                        ? 'TOKEN_ENCRYPTION_KEY invalid'
                                        : 'TOKEN_ENCRYPTION_KEY missing'}
                            </small>
                        </div>
                    </div>
                    {#if dataHealth.warnings?.length}
                        <div class="cc-list-wrap" style="margin-top: 0.75rem;">
                            {#each dataHealth.warnings.slice(0, 5) as warning}
                                <div class="cc-conn-row">
                                    <div class="cc-conn-info">
                                        <div class="cc-conn-name">{warning.account_name || warning.type}</div>
                                        <div class="cc-conn-meta">{warning.message}</div>
                                    </div>
                                    <span class="cc-badge cc-badge-muted">{warning.provider || 'security'}</span>
                                </div>
                            {/each}
                        </div>
                    {/if}
                </div>
            {/if}

            {#if backupStatus}
                <div class="cc-conn-section">
                    <div class="cc-conn-section-header">
                        <div class="cc-conn-section-title">
                            <span class="cc-provider-badge cc-provider-local-ai">Backup</span>
                            <span class="cc-conn-count">{backupStatus.export_format?.toUpperCase() || 'JSON'} export</span>
                        </div>
                        <button class="cc-secondary-btn" type="button" on:click={exportBackup} disabled={backupExporting}>
                            <span class="material-symbols-outlined text-[14px]">download</span>
                            {backupExporting ? 'Exporting…' : 'Export Data'}
                        </button>
                    </div>
                    <div class="cc-local-llm-grid">
                        <div class="cc-local-llm-stat">
                            <span class="cc-insight-label">Database</span>
                            <strong>{backupStatus.db_size_bytes ? `${(backupStatus.db_size_bytes / 1024 / 1024).toFixed(1)} MB` : '—'}</strong>
                            <small>{backupStatus.profile_scope || 'household'} scope</small>
                        </div>
                        <div class="cc-local-llm-stat">
                            <span class="cc-insight-label">Tables</span>
                            <strong>{backupStatus.included_tables?.length || 0}</strong>
                            <small>{backupStatus.excluded_tables?.length || 0} credential table{(backupStatus.excluded_tables?.length || 0) === 1 ? '' : 's'} excluded</small>
                        </div>
                        <div class="cc-local-llm-stat">
                            <span class="cc-insight-label">Credential Storage</span>
                            <strong>{backupStatus.encryption?.status === 'encrypted' ? 'Encrypted' : 'Plaintext risk'}</strong>
                            <small>{backupStatus.encryption?.message || 'Encryption status unknown'}</small>
                        </div>
                    </div>
                </div>
            {/if}

            <!-- Teller Enrollments -->
            <div class="cc-conn-section">
                <div class="cc-conn-section-header">
                    <div class="cc-conn-section-title">
                        <span class="cc-provider-badge cc-provider-teller">Teller</span>
                        <span class="cc-conn-count">{tellerEnrollments.length} enrollment{tellerEnrollments.length !== 1 ? 's' : ''}</span>
                    </div>
                </div>

                {#if tellerEnrollments.length === 0}
                    <div class="cc-conn-empty">No Teller enrollments yet.</div>
                {:else}
                    {#each tellerEnrollments as enrollment}
                        {@const removeKey = removalKey('teller', enrollment.id)}
                        <div class="cc-conn-row">
                            <div class="cc-conn-info">
                                <div class="cc-conn-name">{enrollment.institution || 'Unknown Institution'}</div>
                                <div class="cc-conn-meta">
                                    {enrollment.owner_name || ''}{enrollment.owner_name && enrollment.profile ? ' · ' : ''}{enrollment.profile || ''}
                                    {#if enrollment.created_at}
                                        · added {formatDateTime(enrollment.created_at)}
                                    {/if}
                                </div>
                            </div>
                            <div class="cc-row-actions">
                                {#if pendingRemovalKey === removeKey}
                                    <div class="cc-inline-confirm" aria-label="Confirm removal">
                                        <button class="cc-confirm-btn cc-confirm-accept" type="button" disabled={removalSavingKey === removeKey} on:click={() => handleDeactivateEnrollment(enrollment.id)} title="Confirm remove">
                                            <span class="material-symbols-outlined text-[16px]">{removalSavingKey === removeKey ? 'progress_activity' : 'check'}</span>
                                        </button>
                                        <button class="cc-confirm-btn cc-confirm-cancel" type="button" disabled={removalSavingKey === removeKey} on:click={clearRemoval} title="Cancel">
                                            <span class="material-symbols-outlined text-[16px]">close</span>
                                        </button>
                                    </div>
                                {:else}
                                    <button
                                        type="button"
                                        class="cc-conn-remove"
                                        on:click={() => requestRemoval('teller', enrollment.id)}
                                        title="Remove this enrollment"
                                    >
                                        <span class="material-symbols-outlined text-[16px]">delete</span>
                                    </button>
                                {/if}
                            </div>
                        </div>
                    {/each}
                {/if}
            </div>

            <!-- SimpleFIN Connections -->
            <div class="cc-conn-section">
                <div class="cc-conn-section-header">
                    <div class="cc-conn-section-title">
                        <span class="cc-provider-badge cc-provider-simplefin">SimpleFIN</span>
                        <span class="cc-conn-count">{simplefinConnections.length} connection{simplefinConnections.length !== 1 ? 's' : ''}</span>
                    </div>
                    <button
                        type="button"
                        class="cc-secondary-btn"
                        on:click={() => simplefinRef.show()}
                    >
                        <span class="material-symbols-outlined text-[14px]">add</span>
                        Connect Bank
                    </button>
                </div>

                {#if simplefinConnections.length === 0}
                    <div class="cc-conn-empty">No SimpleFIN connections yet. Click "Connect Bank" to add one.</div>
                {:else}
                    {#each simplefinConnections as conn}
                        {@const removeKey = removalKey('simplefin', conn.id)}
                        <div class="cc-conn-row">
                            <div class="cc-conn-info">
                                <div class="cc-conn-name">{conn.display_name || 'SimpleFIN Connection'}</div>
                                <div class="cc-conn-meta">
                                    {conn.profile || ''}
                                    {#if conn.last_synced_at}
                                        · synced {formatDateTime(conn.last_synced_at)}
                                    {:else if conn.created_at}
                                        · added {formatDateTime(conn.created_at)}
                                    {/if}
                                </div>
                            </div>
                            <div class="cc-row-actions">
                                {#if pendingRemovalKey === removeKey}
                                    <div class="cc-inline-confirm" aria-label="Confirm removal">
                                        <button class="cc-confirm-btn cc-confirm-accept" type="button" disabled={removalSavingKey === removeKey} on:click={() => handleDeactivateSFConnection(conn.id)} title="Confirm remove">
                                            <span class="material-symbols-outlined text-[16px]">{removalSavingKey === removeKey ? 'progress_activity' : 'check'}</span>
                                        </button>
                                        <button class="cc-confirm-btn cc-confirm-cancel" type="button" disabled={removalSavingKey === removeKey} on:click={clearRemoval} title="Cancel">
                                            <span class="material-symbols-outlined text-[16px]">close</span>
                                        </button>
                                    </div>
                                {:else}
                                    <button
                                        type="button"
                                        class="cc-conn-remove"
                                        on:click={() => requestRemoval('simplefin', conn.id)}
                                        title="Remove this connection"
                                    >
                                        <span class="material-symbols-outlined text-[16px]">delete</span>
                                    </button>
                                {/if}
                            </div>
                        </div>
                    {/each}
                {/if}
            </div>

        </section>

        <aside class="cc-pane cc-pane-primary cc-local-ai-pane">
                <div class="cc-pane-header cc-local-llm-header">
                    <div>
                        <div class="cc-conn-section-title">
                            <span class="cc-provider-badge cc-provider-local-ai">Mira Runtime</span>
                            <span class="cc-conn-count">{localLlmStatus.ollamaReachable ? 'Ollama reachable' : 'Ollama offline'}</span>
                        </div>
                        <div class="cc-conn-meta">
                            Tested local Mira defaults, with Advanced mode for model experiments.
                        </div>
                    </div>
                    <div class="cc-local-llm-status-row">
                        <span class="cc-badge" class:cc-badge-positive={localLlmStatus.ollamaReachable} class:cc-badge-muted={!localLlmStatus.ollamaReachable}>
                            {localLlmStatus.ollamaReachable ? 'Ollama reachable' : 'Ollama unavailable'}
                        </span>
                        <span class="cc-badge cc-badge-muted">{localLlmStatus.memoryLabel || '16 GB'} tier</span>
                    </div>
                </div>

                <div class="cc-local-ai-body">
                {#if localLlmLoading}
                    <div class="cc-conn-empty">Loading Mira Runtime settings…</div>
                {:else}
                    {#if localLlmError}
                        <div class="cc-notice cc-notice-local-error">{localLlmError}</div>
                    {/if}

                    <div class="cc-runtime-current">
                        <div class="cc-runtime-current-copy">
                            <span>Active local setup</span>
                            <strong>{primaryMiraModel}</strong>
                            <small>{localLlmStatus.provider || 'ollama'} · {localRuntimeMode} · {localLlmStatus.memoryLabel || '16 GB'} tier</small>
                        </div>
                        <span class="cc-runtime-health" class:cc-runtime-health-ready={localLlmStatus.ollamaReachable}>
                            {localLlmStatus.ollamaReachable ? 'Ready' : 'Offline'}
                        </span>
                    </div>

                    <div class="cc-local-llm-grid cc-runtime-grid">
                        <div class="cc-local-llm-stat">
                            <span class="cc-insight-label">Categorization</span>
                            <strong>{activeCategorizeModelMeta?.label || localLlmStatus.selectedCategorizeModel || '—'}</strong>
                            <small>{localLlmStatus.categorizeBatchSize || 20} tx per batch</small>
                        </div>
                        <div class="cc-local-llm-stat">
                            <span class="cc-insight-label">Mira chat</span>
                            <strong>{activeCopilotModelMeta?.label || localLlmStatus.selectedCopilotModel || '—'}</strong>
                            <small>Active chat model</small>
                        </div>
                        <div class="cc-local-llm-stat">
                            <span class="cc-insight-label">Advisor</span>
                            <strong>{activeAdvisorModelMeta?.label || localLlmStatus.selectedAdvisorModel || '—'}</strong>
                            <small>{localLlmStatus.ramGb && localLlmStatus.ramGb >= 30 ? '30 GB+ profile' : 'E4B profile'}</small>
                        </div>
                    </div>

                    <div class="cc-local-llm-form">
                        <div class="cc-runtime-section-title">
                            <span>Tune runtime</span>
                            <small>Default keeps Mira on the tested model pairing. Advanced unlocks unvalidated choices.</small>
                        </div>
                        <div class="cc-form-grid">
                            <label class="cc-local-toggle">
                                <input type="checkbox" bind:checked={localLlmForm.low_power_mode} />
                                <span>Low power mode</span>
                            </label>
                            <label class="cc-local-toggle">
                                <input type="checkbox" bind:checked={localLlmForm.expert_mode} />
                                <span>Advanced mode</span>
                            </label>
                            <div class="cc-local-llm-stat">
                                <span class="cc-insight-label">Pace</span>
                                <strong>{localRuntimeMode}</strong>
                                <small>{localLlmForm.inter_batch_delay_ms || 0} ms between batches</small>
                            </div>
                        </div>

                        <div class="cc-form-grid">
                            {#if localLlmForm.expert_mode}
                            <div class="cc-field cc-field-full">
                                <span>Categorization model</span>
                                <select class="cc-select" bind:value={localLlmForm.categorize_model}>
                                    {#each localLlmTierGroups as group}
                                        <optgroup label={group.label}>
                                            {#each group.models.filter((model) => modelSupports(model, 'categorize')) as model}
                                                <option value={model.id}>
                                                    {localModelOptionLabel(model)}
                                                </option>
                                            {/each}
                                        </optgroup>
                                    {/each}
                                </select>
                            </div>
                            <div class="cc-field cc-field-full">
                                <span>Mira model</span>
                                <select class="cc-select" bind:value={localLlmForm.copilot_model}>
                                    {#each localLlmTierGroups as group}
                                        <optgroup label={group.label}>
                                            {#each group.models.filter((model) => modelSupports(model, 'copilot')) as model}
                                                <option value={model.id}>
                                                    {localModelOptionLabel(model)}
                                                </option>
                                            {/each}
                                        </optgroup>
                                    {/each}
                                </select>
                            </div>
                            {/if}
                            <div class="cc-field">
                                <span>Batch size</span>
                                <input class="cc-input" type="number" min="1" max="50" bind:value={localLlmForm.categorize_batch_size} />
                            </div>
                            <div class="cc-field">
                                <span>Delay between batches (ms)</span>
                                <input class="cc-input" type="number" min="0" max="5000" step="100" bind:value={localLlmForm.inter_batch_delay_ms} />
                            </div>
                        </div>

                        {#if localLlmForm.expert_mode}
                        <div class="cc-model-disclosure">
                            <button
                                type="button"
                                class="cc-disclosure-button"
                                on:click={() => localLlmRecommendationsOpen = !localLlmRecommendationsOpen}
                            >
                                <span>Advanced models</span>
                                <small>{localLlmRecommendationCount} curated models</small>
                                <span class="material-symbols-outlined text-[16px]" class:cc-chevron-open={localLlmRecommendationsOpen}>expand_more</span>
                            </button>

                            {#if localLlmRecommendationsOpen}
                                <div class="cc-list-meta">
                                    These models are available for experimentation. The default Mira prompts are validated on Gemma 4 E4B, with Gemma 4 26B reserved for Advisor on 30 GB+ systems.
                                </div>

                                <div class="cc-local-llm-tier-list">
                                    {#each localLlmTierGroups as group}
                                        <div class="cc-local-llm-tier">
                                            <div class="cc-local-llm-tier-header">
                                                <div class="cc-local-llm-tier-title">{group.label}</div>
                                                <div class="cc-local-llm-tier-subtitle">{group.models.length} curated models</div>
                                            </div>
                                            <div class="cc-local-llm-model-list">
                                                {#each group.models as model}
                                                    <div class="cc-local-llm-model-row" class:cc-local-llm-model-row-installed={model.installed}>
                                                        <div class="cc-local-llm-model-main">
                                                            <div class="cc-local-llm-model-topline">
                                                                <strong>{model.label}</strong>
                                                                <span class="cc-local-llm-model-size">{localModelSize(model) || model.approx_size_gb} GB</span>
                                                                {#if model.installed}<span class="cc-local-llm-chip-note">Installed</span>{/if}
                                                                {#if modelRequiresAdvanced(model)}<span class="cc-local-llm-chip-note">Advanced</span>{/if}
                                                                {#if model.quantization}<span class="cc-local-llm-chip-note">{model.quantization}</span>{/if}
                                                            </div>
                                                            <div class="cc-local-llm-model-badges">
                                                                {#each model.badges || [] as badge}
                                                                    <span class="cc-local-llm-chip-note">{badge}</span>
                                                                {/each}
                                                            </div>
                                                            <div class="cc-local-llm-model-warning">{model.warning}</div>
                                                        </div>
                                                        <div class="cc-local-llm-model-actions">
                                                            {#if modelSupports(model, 'categorize')}
                                                                <button
                                                                    type="button"
                                                                    class="cc-secondary-btn"
                                                                    on:click={() => applyLocalLlmModel('categorize_model', model.id)}
                                                                >
                                                                    Categorize
                                                                </button>
                                                            {/if}
                                                            {#if modelSupports(model, 'copilot')}
                                                                <button
                                                                    type="button"
                                                                    class="cc-secondary-btn"
                                                                    on:click={() => applyLocalLlmModel('copilot_model', model.id)}
                                                                >
                                                                    Mira
                                                                </button>
                                                            {/if}
                                                            {#if !model.installed && localLlmStatus.provider === 'ollama' && localLlmStatus.ollamaReachable}
                                                                <button
                                                                    type="button"
                                                                    class="cc-local-llm-install-btn"
                                                                    on:click={() => installLocalLlmModel(model.id)}
                                                                    disabled={!!localLlmInstallingModel}
                                                                >
                                                                    {localLlmInstallingModel === model.id ? 'Installing…' : 'Install'}
                                                                </button>
                                                            {:else}
                                                                <span class="cc-local-llm-install-state">{model.installed ? 'Installed' : 'Unavailable'}</span>
                                                            {/if}
                                                        </div>
                                                    </div>
                                                {/each}
                                            </div>
                                        </div>
                                    {/each}
                                </div>
                            {/if}
                        </div>
                        {/if}

                        <div class="cc-actions">
                            <button class="cc-primary-btn" type="button" on:click={saveLocalLlmSettings} disabled={localLlmSaving}>
                                {localLlmSaving ? 'Saving…' : 'Save Mira Runtime'}
                            </button>
                        </div>
                    </div>
                {/if}
                </div>
        </aside>
        </div>

        <SimpleFINConnect bind:this={simplefinRef} on:connected={handleSimpleFINConnected} />
        <MigrationWizard bind:this={migrationRef} on:done={loadConnections} />

    {:else if activeTab === 'accounts'}
        <section class="cc-pane cc-pane-primary cc-ops-pane fade-in-up">
            <div class="cc-pane-header">
                <div class="cc-pane-title">
                    <h3>Balance Sources</h3>
                    <p>The synced and manual sources that feed dashboard net worth.</p>
                </div>
            </div>

            <div class="cc-insights cc-tab-facts">
                <div class="cc-insight-card">
                    <span class="cc-insight-label">Synced</span>
                    <strong>{syncedAccountCount}</strong>
                    <small>From Teller or SimpleFIN</small>
                </div>
                <div class="cc-insight-card">
                    <span class="cc-insight-label">Manual</span>
                    <strong>{manualAccountCount}</strong>
                    <small>Assets and liabilities</small>
                </div>
                <div class="cc-insight-card">
                    <span class="cc-insight-label">Manual Net</span>
                    <strong>{formatCurrency(accountItems.filter(a => a.provider === 'manual').reduce((sum, a) => sum + (a.is_credit ? -Math.abs(Number(a.balance || 0)) : Number(a.balance || 0)), 0), 2)}</strong>
                    <small>Included in dashboard net worth</small>
                </div>
            </div>

            <div class="cc-management-grid">
            <div class="cc-conn-section cc-management-side">
                <div class="cc-conn-section-header">
                    <div class="cc-conn-section-title">
                        <span class="cc-provider-badge cc-provider-local-ai">Add Source</span>
                        <span class="cc-conn-count">{activeProfileId === 'household' ? 'Household' : activeProfileId}</span>
                    </div>
                </div>
                <div class="cc-form-grid">
                    <div class="cc-field">
                        <span>Name</span>
                        <input class="cc-input" bind:value={manualAccountDraft.name} placeholder="Roth IRA, Home value, Car loan" />
                    </div>
                    <div class="cc-field">
                        <span>Kind</span>
                        <select class="cc-select" bind:value={manualAccountDraft.account_type}>
                            <option value="depository">Cash asset</option>
                            <option value="investment">Investment / property asset</option>
                            <option value="credit">Credit liability</option>
                            <option value="loan">Loan / mortgage liability</option>
                        </select>
                    </div>
                    <div class="cc-field">
                        <span>Subtype</span>
                        <input class="cc-input" bind:value={manualAccountDraft.account_subtype} placeholder="retirement, property, mortgage" />
                    </div>
                    <div class="cc-field">
                        <span>Balance</span>
                        <input class="cc-input" type="number" step="0.01" bind:value={manualAccountDraft.balance} placeholder="0.00" />
                    </div>
                    <div class="cc-field cc-field-full">
                        <span>Notes</span>
                        <input class="cc-input" bind:value={manualAccountDraft.notes} placeholder="Optional context" />
                    </div>
                </div>
                <div class="cc-actions">
                    <button class="cc-primary-btn" disabled={manualAccountSaving || !manualAccountDraft.name.trim()} on:click={saveManualAccount}>
                        {manualAccountSaving ? 'Saving...' : 'Add Manual Account'}
                    </button>
                </div>
            </div>

            <div class="cc-list-wrap cc-management-main">
                {#if accountsLoading}
                    <div class="cc-empty">Loading accounts...</div>
                {:else if accountItems.length === 0}
                    <div class="cc-empty">No accounts yet.</div>
                {:else}
                        <div class="cc-table">
                        <div class="cc-table-header" style="--cc-cols: 1.4fr 0.7fr 0.8fr 0.8fr 0.6fr;">
                            <div>Source</div>
                            <div>Kind</div>
                            <div>Balance</div>
                            <div>Feed</div>
                            <div></div>
                        </div>
                        {#each accountItems as account}
                            {@const removeKey = removalKey('manual-account', account.id)}
                            {@const paymentLabel = paymentDueLabel(account)}
                            <div class="cc-table-row" class:cc-table-row-active={manualAccountEdits[account.id] || accountPaymentEdits[account.id]} style="--cc-cols: 1.4fr 0.7fr 0.8fr 0.8fr 0.6fr;">
                                <div class="cc-cell-primary">
                                    <div class="cc-cell-title">{account.name}</div>
                                    <div class="cc-cell-subtitle">{account.profile || 'household'}{account.manual_updated_at ? ` · updated ${formatDate(account.manual_updated_at)}` : ''}{account.manual_is_stale ? ' · stale' : ''}</div>
                                </div>
                                <div class="cc-cell-subtitle">{account.account_type || account.type}</div>
                                <div class="cc-cell-subtitle">{formatCurrency(Math.abs(Number(account.balance || 0)), 2)}</div>
                                <div class="cc-cell-subtitle">{account.provider || 'synced'}</div>
                                <div>
                                    {#if isCreditCardAccount(account) || account.provider === 'manual'}
                                        <div class="cc-row-actions">
                                            {#if isCreditCardAccount(account)}
                                                {#if paymentLabel}
                                                    <button
                                                        class="cc-payment-status"
                                                        class:cc-payment-status-fresh={accountPaymentSavedId === account.id}
                                                        type="button"
                                                        on:click={() => { clearRemoval(); toggleAccountPaymentEdit(account); }}
                                                        title={accountPaymentEdits[account.id] ? 'Collapse payment details' : 'Payment details saved. Click to edit.'}
                                                    >
                                                        <span class="material-symbols-outlined">check_circle</span>
                                                        <span>{paymentLabel}</span>
                                                    </button>
                                                {:else}
                                                    <button class="cc-conn-remove cc-payment-action" type="button" on:click={() => { clearRemoval(); toggleAccountPaymentEdit(account); }} title={accountPaymentEdits[account.id] ? 'Collapse payment details' : 'Payment details'}>
                                                        <span class="material-symbols-outlined text-[16px]">event</span>
                                                    </button>
                                                {/if}
                                            {/if}
                                            {#if account.provider === 'manual'}
                                                <button class="cc-conn-remove" on:click={() => { clearRemoval(); startManualAccountEdit(account); }} title="Edit manual account">
                                                    <span class="material-symbols-outlined text-[16px]">edit</span>
                                                </button>
                                                {#if pendingRemovalKey === removeKey}
                                                    <div class="cc-inline-confirm" aria-label="Confirm removal">
                                                        <button class="cc-confirm-btn cc-confirm-accept" type="button" disabled={removalSavingKey === removeKey} on:click={() => removeManualAccount(account.id)} title="Confirm remove">
                                                            <span class="material-symbols-outlined text-[16px]">{removalSavingKey === removeKey ? 'progress_activity' : 'check'}</span>
                                                        </button>
                                                        <button class="cc-confirm-btn cc-confirm-cancel" type="button" disabled={removalSavingKey === removeKey} on:click={clearRemoval} title="Cancel">
                                                            <span class="material-symbols-outlined text-[16px]">close</span>
                                                        </button>
                                                    </div>
                                                {:else}
                                                    <button class="cc-conn-remove" on:click={() => requestRemoval('manual-account', account.id)} title="Remove manual account">
                                                        <span class="material-symbols-outlined text-[16px]">delete</span>
                                                    </button>
                                                {/if}
                                            {/if}
                                        </div>
                                    {/if}
                                </div>
                            </div>
                            {#if accountPaymentEdits[account.id]}
                                <div class="cc-table-row cc-payment-detail-row" style="--cc-cols: 1fr;">
                                    <section class="cc-inspector-section cc-payment-details">
                                        <div>
                                            <h3>Payment details</h3>
                                            <p>Used to show payment timing reminders. You can change it anytime.</p>
                                        </div>
                                        <div class="cc-form-grid">
                                            <div class="cc-field">
                                                <span>Usual monthly due day</span>
                                                <input
                                                    class="cc-input"
                                                    type="number"
                                                    min="1"
                                                    max="31"
                                                    step="1"
                                                    inputmode="numeric"
                                                    placeholder="22"
                                                    bind:value={accountPaymentEdits[account.id].usual_due_day}
                                                />
                                            </div>
                                            <div class="cc-actions cc-payment-actions">
                                                <button
                                                    class="cc-primary-btn"
                                                    disabled={accountPaymentSavingId === account.id || !isDueDayDraftValid(account.id)}
                                                    on:click={() => saveAccountPaymentDetails(account)}
                                                >
                                                    {accountPaymentSavingId === account.id ? 'Saving...' : 'Save Payment Details'}
                                                </button>
                                                <button class="cc-secondary-btn" on:click={() => cancelAccountPaymentEdit(account.id)}>Cancel</button>
                                            </div>
                                        </div>
                                    </section>
                                </div>
                            {/if}
                            {#if manualAccountEdits[account.id]}
                                <div class="cc-table-row" style="--cc-cols: 1fr;">
                                    <div class="cc-form-grid">
                                        <div class="cc-field">
                                            <span>Name</span>
                                            <input class="cc-input" bind:value={manualAccountEdits[account.id].name} />
                                        </div>
                                        <div class="cc-field">
                                            <span>Type</span>
                                            <select class="cc-select" bind:value={manualAccountEdits[account.id].account_type}>
                                                <option value="depository">Cash asset</option>
                                                <option value="investment">Investment / property asset</option>
                                                <option value="credit">Credit liability</option>
                                                <option value="loan">Loan / mortgage liability</option>
                                            </select>
                                        </div>
                                        <div class="cc-field">
                                            <span>Balance</span>
                                            <input class="cc-input" type="number" step="0.01" bind:value={manualAccountEdits[account.id].balance} />
                                        </div>
                                        <div class="cc-field">
                                            <span>Subtype</span>
                                            <input class="cc-input" bind:value={manualAccountEdits[account.id].account_subtype} />
                                        </div>
                                        <div class="cc-field cc-field-full">
                                            <span>Notes</span>
                                            <input class="cc-input" bind:value={manualAccountEdits[account.id].notes} />
                                        </div>
                                        <div class="cc-actions">
                                            <button class="cc-primary-btn" disabled={manualAccountSavingId === account.id} on:click={() => saveManualAccountEdit(account)}>
                                                {manualAccountSavingId === account.id ? 'Saving...' : 'Save Balance'}
                                            </button>
                                            <button class="cc-secondary-btn" on:click={() => cancelManualAccountEdit(account.id)}>Cancel</button>
                                        </div>
                                    </div>
                                </div>
                            {/if}
                        {/each}
                    </div>
                {/if}
            </div>
            </div>
        </section>

    {:else if activeTab === 'investments'}
        <section class="cc-pane cc-pane-primary cc-ops-pane fade-in-up">
            <div class="cc-pane-header">
                <div class="cc-pane-title">
                    <h3>Portfolio Inputs</h3>
                    <p>Manual holdings, prices, and allocation targets that stay local.</p>
                </div>
            </div>

            <div class="cc-insights cc-tab-facts">
                <div class="cc-insight-card">
                    <span class="cc-insight-label">Portfolio</span>
                    <strong>{formatCurrency(investmentSummary.total_value || 0, 2)}</strong>
                    <small>{investmentSummary.holding_count || 0} holding{(investmentSummary.holding_count || 0) === 1 ? '' : 's'}</small>
                </div>
                <div class="cc-insight-card">
                    <span class="cc-insight-label">Gain / Loss</span>
                    <strong>{formatCurrency(investmentSummary.gain_loss || 0, 2)}</strong>
                    <small>{investmentSummary.gain_loss_percent == null ? 'No cost basis yet' : `${investmentSummary.gain_loss_percent.toFixed(1)}% total return`}</small>
                </div>
                <div class="cc-insight-card">
                    <span class="cc-insight-label">Privacy</span>
                    <strong>Manual</strong>
                    <small>No symbols leave this device</small>
                </div>
            </div>

            <div class="cc-management-grid">
            <div class="cc-conn-section cc-management-side">
                <div class="cc-conn-section-header">
                    <div class="cc-conn-section-title">
                        <span class="cc-provider-badge cc-provider-local-ai">{holdingEditId ? 'Edit Holding' : 'New Holding'}</span>
                        <span class="cc-conn-count">{activeProfileId === 'household' ? 'Household' : activeProfileId}</span>
                    </div>
                </div>
                <div class="cc-form-grid">
                    <div class="cc-field">
                        <span>Account</span>
                        <select class="cc-select" bind:value={holdingDraft.account_id}>
                            <option value="">Unassigned</option>
                            {#each investmentAccounts as account}
                                <option value={account.id}>{account.name}</option>
                            {/each}
                        </select>
                    </div>
                    <div class="cc-field">
                        <span>Symbol</span>
                        <input class="cc-input" bind:value={holdingDraft.symbol} placeholder="VTI" />
                    </div>
                    <div class="cc-field">
                        <span>Name</span>
                        <input class="cc-input" bind:value={holdingDraft.name} placeholder="Vanguard Total Stock Market" />
                    </div>
                    <div class="cc-field">
                        <span>Asset Class</span>
                        <select class="cc-select" bind:value={holdingDraft.asset_class}>
                            <option value="stock">Stocks</option>
                            <option value="bond">Bonds</option>
                            <option value="cash">Cash</option>
                            <option value="fund">Funds</option>
                            <option value="retirement">Retirement</option>
                            <option value="crypto">Crypto</option>
                            <option value="real_estate">Real Estate</option>
                            <option value="other">Other</option>
                        </select>
                    </div>
                    <div class="cc-field">
                        <span>Quantity</span>
                        <input class="cc-input" type="number" step="0.0001" bind:value={holdingDraft.quantity} />
                    </div>
                    <div class="cc-field">
                        <span>Cost / Share</span>
                        <input class="cc-input" type="number" step="0.01" bind:value={holdingDraft.cost_basis} />
                    </div>
                    <div class="cc-field">
                        <span>Price / Share</span>
                        <input class="cc-input" type="number" step="0.01" bind:value={holdingDraft.current_price} />
                    </div>
                    <div class="cc-field">
                        <span>Manual Value</span>
                        <input class="cc-input" type="number" step="0.01" bind:value={holdingDraft.manual_value} placeholder="Overrides shares × price" />
                    </div>
                    <div class="cc-field">
                        <span>Target %</span>
                        <input class="cc-input" type="number" step="0.1" bind:value={holdingDraft.target_percent} />
                    </div>
                    <div class="cc-field cc-field-full">
                        <span>Notes</span>
                        <input class="cc-input" bind:value={holdingDraft.notes} placeholder="Optional context" />
                    </div>
                </div>
                <div class="cc-actions">
                    <button class="cc-primary-btn" type="button" disabled={holdingSaving || !(holdingDraft.name || holdingDraft.symbol).trim()} on:click={saveHolding}>
                        {holdingSaving ? 'Saving…' : holdingEditId ? 'Update Holding' : 'Add Holding'}
                    </button>
                    {#if holdingEditId}
                        <button class="cc-secondary-btn" type="button" on:click={resetHoldingDraft}>Cancel</button>
                    {/if}
                </div>
            </div>

            <div class="cc-list-wrap cc-management-main">
                {#if investmentsLoading}
                    <div class="cc-empty">Loading holdings…</div>
                {:else if holdings.length === 0}
                    <div class="cc-empty">No holdings yet. Add manual holdings to make Folio feel like a full net-worth app without broker sync.</div>
                {:else}
                        <div class="cc-table">
                        <div class="cc-table-header" style="--cc-cols: 1.25fr 0.7fr 0.7fr 0.8fr 0.8fr 0.6fr;">
                            <div>Asset</div>
                            <div>Type</div>
                            <div>Shares</div>
                            <div>Value</div>
                            <div>Return</div>
                            <div></div>
                        </div>
                        {#each holdings as item}
                            {@const removeKey = removalKey('holding', item.id)}
                            <div class="cc-table-row" style="--cc-cols: 1.25fr 0.7fr 0.7fr 0.8fr 0.8fr 0.6fr;">
                                <div class="cc-cell-primary">
                                    <div class="cc-cell-title">{item.symbol || item.name}</div>
                                    <div class="cc-cell-subtitle">{item.symbol ? item.name : 'Manual holding'}{item.account_name ? ` · ${item.account_name}` : ''}</div>
                                </div>
                                <div class="cc-cell-subtitle">{item.asset_class_label}</div>
                                <div class="cc-cell-subtitle">{Number(item.quantity || 0).toLocaleString()}</div>
                                <div class="cc-cell-subtitle">{formatCurrency(item.market_value || 0, 2)}</div>
                                <div class="cc-cell-subtitle">{formatCurrency(item.gain_loss || 0, 2)}</div>
                                <div class="cc-row-actions">
                                    <button class="cc-conn-remove" type="button" on:click={() => { clearRemoval(); editHolding(item); }} title="Edit holding">
                                        <span class="material-symbols-outlined text-[16px]">edit</span>
                                    </button>
                                    {#if pendingRemovalKey === removeKey}
                                        <div class="cc-inline-confirm" aria-label="Confirm removal">
                                            <button class="cc-confirm-btn cc-confirm-accept" type="button" disabled={removalSavingKey === removeKey} on:click={() => removeHolding(item.id)} title="Confirm remove">
                                                <span class="material-symbols-outlined text-[16px]">{removalSavingKey === removeKey ? 'progress_activity' : 'check'}</span>
                                            </button>
                                            <button class="cc-confirm-btn cc-confirm-cancel" type="button" disabled={removalSavingKey === removeKey} on:click={clearRemoval} title="Cancel">
                                                <span class="material-symbols-outlined text-[16px]">close</span>
                                            </button>
                                        </div>
                                    {:else}
                                        <button class="cc-conn-remove" type="button" on:click={() => requestRemoval('holding', item.id)} title="Remove holding">
                                            <span class="material-symbols-outlined text-[16px]">delete</span>
                                        </button>
                                    {/if}
                                </div>
                            </div>
                        {/each}
                    </div>
                {/if}
            </div>

            {#if allocation.length > 0}
                <div class="cc-conn-section cc-management-side">
                    <div class="cc-conn-section-header">
                        <div class="cc-conn-section-title">
                            <span class="cc-provider-badge">Allocation</span>
                            <span class="cc-conn-count">Manual targets optional</span>
                        </div>
                    </div>
                    {#each allocation as bucket}
                        <div class="cc-conn-row">
                            <div class="cc-conn-info">
                                <div class="cc-conn-name">{bucket.label}</div>
                                <div class="cc-conn-meta">{formatCurrency(bucket.value || 0, 2)} · {(bucket.actual_percent || 0).toFixed(1)}% actual{bucket.target_percent != null ? ` · ${bucket.target_percent.toFixed(1)}% target` : ''}</div>
                            </div>
                            <span class="cc-badge" class:cc-badge-positive={bucket.drift_percent == null || Math.abs(bucket.drift_percent) < 5} class:cc-badge-muted={bucket.drift_percent != null && Math.abs(bucket.drift_percent) >= 5}>
                                {bucket.drift_percent == null ? 'No target' : `${bucket.drift_percent > 0 ? '+' : ''}${bucket.drift_percent.toFixed(1)}% drift`}
                            </span>
                        </div>
                    {/each}
                </div>
            {/if}
            </div>
        </section>

    {:else if activeTab === 'merchants'}
        <!-- ── MERCHANTS ─────────────────────────────────────────── -->
        <section class="cc-pane cc-pane-primary cc-pane-merchants fade-in-up">
                <div class="cc-pane-header">
                <div class="cc-pane-title">
                    <h3>Merchant Memory</h3>
                    <p>Spend-only merchants Folio remembers, cleans up, and routes into the right categories.</p>
                </div>
                    <div class="cc-toolbar">
                        <div class="cc-toolbar-pill-wrap">
                            <button
                                type="button"
                                class="cc-merchant-pill"
                                on:click|stopPropagation
                                on:click={() => {
                                    openMerchantCategoryMenuKey = null;
                                    merchantFilterMenuOpen = !merchantFilterMenuOpen;
                                }}>
                                <span>{merchantSubFilter === 'subscriptions' ? 'Recurring merchants' : merchantSubFilter === 'non_subscriptions' ? 'One-off merchants' : 'All spend'}</span>
                                <span class="material-symbols-outlined text-[16px]" class:cc-chevron-open={merchantFilterMenuOpen}>expand_more</span>
                            </button>
                            {#if merchantFilterMenuOpen}
                                <div class="cc-merchant-dropdown">
                                    <button type="button" class="cc-merchant-dropdown-option" class:active={merchantSubFilter === 'all'} on:click={() => { merchantSubFilter = 'all'; merchantFilterMenuOpen = false; }}>
                                        <span>All spend</span>
                                        {#if merchantSubFilter === 'all'}<span class="material-symbols-outlined text-[14px]">check</span>{/if}
                                    </button>
                                    <button type="button" class="cc-merchant-dropdown-option" class:active={merchantSubFilter === 'subscriptions'} on:click={() => { merchantSubFilter = 'subscriptions'; merchantFilterMenuOpen = false; }}>
                                        <span>Recurring merchants</span>
                                        {#if merchantSubFilter === 'subscriptions'}<span class="material-symbols-outlined text-[14px]">check</span>{/if}
                                    </button>
                                    <button type="button" class="cc-merchant-dropdown-option" class:active={merchantSubFilter === 'non_subscriptions'} on:click={() => { merchantSubFilter = 'non_subscriptions'; merchantFilterMenuOpen = false; }}>
                                        <span>One-off merchants</span>
                                        {#if merchantSubFilter === 'non_subscriptions'}<span class="material-symbols-outlined text-[14px]">check</span>{/if}
                                    </button>
                                </div>
                            {/if}
                        </div>
                        <div class="cc-merchant-scope-chip">
                            <span>{merchantSearch.trim() ? 'Search results' : 'Last 365 days'}</span>
                            {#if !merchantSearch.trim() && merchantArchivedItems.length > 0}
                                <small>{merchantArchivedItems.length} older grouped</small>
                            {/if}
                        </div>
                        <input class="cc-search" bind:value={merchantSearch} placeholder="Search name, industry, or key…" on:keydown={(e) => e.key === 'Enter' && loadMerchants()} />
                        <button class="cc-secondary-btn" type="button" on:click={() => loadMerchants()} disabled={merchantsLoading}>Refresh</button>
                    </div>
                </div>
                <div class="cc-list-wrap">
                    <div class="cc-insights cc-tab-facts">
                        <div class="cc-insight-card">
                            <span class="cc-insight-label">{merchantSearch.trim() ? 'Matched Merchants' : 'Recent Merchants'}</span>
                            <strong>{merchantVisibleCount.toLocaleString()}</strong>
                            <small>{merchantScopeLabel} scope</small>
                        </div>
                        <div class="cc-insight-card">
                            <span class="cc-insight-label">Visible Spend</span>
                            <strong>{formatCurrency(merchantVisibleSpend, 2)}</strong>
                            <small>Transfer-like outflows excluded</small>
                        </div>
                        <div class="cc-insight-card">
                            <span class="cc-insight-label">Recurring Rows</span>
                            <strong>{merchantSubscriptionCount.toLocaleString()}</strong>
                            <small>Subscription-tagged merchants</small>
                        </div>
                    </div>
                    <div class="cc-list-meta">
                        {#if !merchantSearch.trim() && merchantArchivedItems.length > 0}
                            Showing merchants with spend in the last {MERCHANT_RECENT_DAYS} days. Older merchants are grouped by last transaction year below.
                        {:else}
                            Merchant category changes apply to matching transactions immediately. Expand a row for the raw key, profile, display name edit, and recent transactions.
                        {/if}
                    </div>
                    {#if merchantsLoading}
                        <div class="cc-empty">Loading merchants…</div>
                    {:else if merchantDisplayEntries.length === 0}
                        <div class="cc-empty">No merchants matched the current filters.</div>
                    {:else}
                        <div class="cc-table">
                            <div class="cc-table-header cc-merchant-table-header" style="--cc-cols: minmax(18rem, 1fr) 12rem 5.25rem 8rem 7.5rem;">
                                <div>Merchant</div>
                                <div>Routes to</div>
                                <div>Txns</div>
                                <div>Total Spent</div>
                                <div>Last Spent</div>
                            </div>
                            {#each merchantDisplayEntries as entry}
                                {#if entry.type === 'year'}
                                    <button
                                        type="button"
                                        class="cc-merchant-year-row"
                                        class:cc-merchant-year-row-open={!!expandedMerchantYearGroups[entry.group.year]}
                                        on:click={() => toggleMerchantYearGroup(entry.group.year)}>
                                        <span class="material-symbols-outlined" class:cc-chevron-open={!!expandedMerchantYearGroups[entry.group.year]}>expand_more</span>
                                        <strong>{entry.group.year}</strong>
                                        <small>{entry.group.items.length.toLocaleString()} merchants · {entry.group.charge_count.toLocaleString()} tx · {formatCurrency(entry.group.total_spent, 2)}</small>
                                    </button>
                                {:else}
                                    {@const item = entry.item}
                                    {@const rowKey = merchantRowKey(item)}
                                    {@const rowMeta = merchantMetaLine(item)}
                                    {@const merchantCategoryOptions = filterCategoryNames(allCategoryNames, merchantCategorySearch)}
                                    <div
                                        class="cc-table-row cc-merchant-row"
                                        class:cc-table-row-active={rowKey === selectedMerchantKey}
                                        class:cc-merchant-row-archived={entry.archived}
                                        style="--cc-cols: minmax(18rem, 1fr) 12rem 5.25rem 8rem 7.5rem;"
                                        role="button"
                                        tabindex="0"
                                        on:click={() => handleMerchantRowClick(rowKey)}
                                        on:keydown={(event) => handleMerchantRowKeydown(event, rowKey)}>
                                        <div class="cc-cell-primary">
                                            <div class="cc-merchant-identity">
                                                <span class="cc-merchant-avatar">
                                                    {#if item.logo_url}
                                                        <img src={item.logo_url} alt="" />
                                                    {:else}
                                                        {merchantInitials(item)}
                                                    {/if}
                                                </span>
                                                <div class="cc-merchant-name-stack">
                                                    <div class="cc-merchant-name-line">
                                                        <span class="truncate">{item.clean_name || item.merchant_key}</span>
                                                        {#if item.is_subscription}
                                                            <span class="material-symbols-outlined cc-merchant-recurring" title="Subscription">event_repeat</span>
                                                        {/if}
                                                    </div>
                                                    {#if rowMeta}
                                                        <div class="cc-merchant-row-meta">{rowMeta}</div>
                                                    {/if}
                                                </div>
                                            </div>
                                        </div>
                                        <div class="cc-row-category-wrap">
                                            <button
                                                type="button"
                                                class="cc-row-category-btn"
                                                class:cc-row-category-btn-active={openMerchantCategoryMenuKey === rowKey}
                                                on:click|stopPropagation={() => toggleMerchantCategoryMenu(item, rowKey)}
                                                disabled={merchantSaving}>
                                                <span>{item.category || 'Unassigned'}</span>
                                                <span class="material-symbols-outlined text-[14px]" class:cc-chevron-open={openMerchantCategoryMenuKey === rowKey}>expand_more</span>
                                            </button>
                                            {#if openMerchantCategoryMenuKey === rowKey}
                                                <div class="cc-row-category-dropdown">
                                                    <div class="cc-category-menu-search-wrap">
                                                        <span class="material-symbols-outlined text-[14px]">search</span>
                                                        <input
                                                            class="cc-category-menu-search"
                                                            bind:value={merchantCategorySearch}
                                                            placeholder="Search categories..."
                                                            on:click|stopPropagation
                                                            on:keydown|stopPropagation={(event) => {
                                                                if (event.key === 'Escape') {
                                                                    openMerchantCategoryMenuKey = null;
                                                                    merchantCategorySearch = '';
                                                                }
                                                            }}
                                                        />
                                                    </div>
                                                    <div class="cc-category-menu-list">
                                                    <button
                                                        type="button"
                                                        class="cc-merchant-dropdown-option"
                                                        class:active={!item.category}
                                                        on:click|stopPropagation={() => applyMerchantCategory(item, rowKey, '')}>
                                                        <span>Unassigned</span>
                                                        {#if !item.category}
                                                            <span class="material-symbols-outlined text-[14px]">check</span>
                                                        {/if}
                                                    </button>
                                                    {#each merchantCategoryOptions as categoryName}
                                                        <button
                                                            type="button"
                                                            class="cc-merchant-dropdown-option"
                                                            class:active={categoryName === (item.category || '')}
                                                            on:click|stopPropagation={() => applyMerchantCategory(item, rowKey, categoryName)}>
                                                            <span>{categoryName}</span>
                                                            {#if categoryName === (item.category || '')}
                                                                <span class="material-symbols-outlined text-[14px]">check</span>
                                                            {/if}
                                                        </button>
                                                    {/each}
                                                    {#if merchantCategoryOptions.length === 0 && merchantCategorySearch}
                                                        <div class="cc-category-menu-empty">No matching categories</div>
                                                    {/if}
                                                    </div>
                                                </div>
                                            {/if}
                                        </div>
                                        <div class="cc-merchant-metric">{item.charge_count || 0}</div>
                                        <div class="cc-merchant-metric">{formatCurrency(item.total_spent || 0, 2)}</div>
                                        <div class="cc-merchant-date">{merchantLastSpentLabel(item)}</div>
                                    </div>

                                    {#if expandedMerchantKey === rowKey}
                                        <section class="cc-row-expansion">
                                        <div class="cc-inline-section-heading">
                                            <div>
                                                <div class="cc-inline-title-row">
                                                    <h3>Recent Transactions</h3>
                                                    <div class="cc-inline-merchant-rename">
                                                        <input
                                                            id={`merchant-alias-${rowKey}`}
                                                            class="cc-input cc-inline-merchant-input"
                                                            type="text"
                                                            value={merchantAliasDraftValue(item)}
                                                            placeholder={item.clean_name || item.merchant_key}
                                                            aria-label="Merchant display name"
                                                            on:input={(event) => updateMerchantAliasDraft(item, event.currentTarget.value)}
                                                            on:keydown|stopPropagation={(event) => {
                                                                if (event.key === 'Enter') {
                                                                    event.preventDefault();
                                                                    saveMerchantAlias(item, rowKey);
                                                                }
                                                            }} />
                                                        <button
                                                            type="button"
                                                            class="cc-ghost-btn cc-inline-merchant-btn"
                                                            on:click|stopPropagation={() => updateMerchantAliasDraft(item, item.clean_name || item.merchant_key || '')}
                                                            disabled={merchantSaving}>
                                                            Reset
                                                        </button>
                                                        <button
                                                            type="button"
                                                            class="cc-secondary-btn cc-inline-merchant-btn"
                                                            on:click|stopPropagation={() => saveMerchantAlias(item, rowKey)}
                                                            disabled={merchantSaving}>
                                                            Save
                                                        </button>
                                                    </div>
                                                </div>
                                                <p>
                                                    Key: {item.merchant_key} · Profile: {item.profile_id || '—'} · Last spent: {merchantLastSpentLabel(item)}
                                                    {item.industry ? ` · ${item.industry}` : ''}
                                                </p>
                                            </div>
                                            <div class="cc-inline-section-stats">
                                                <span>{item.charge_count || 0} txns</span>
                                                <span>{formatCurrency(item.total_spent || 0, 2)}</span>
                                            </div>
                                        </div>
                                        {#if merchantPreviewLoading && selectedMerchantKey === rowKey}
                                            <div class="cc-empty">Loading…</div>
                                        {:else if selectedMerchantKey === rowKey && merchantTransactions.length === 0}
                                            <div class="cc-empty">No transactions found for this merchant.</div>
                                        {:else if selectedMerchantKey === rowKey}
                                            <div class="cc-mini-table-wrap">
                                                <table class="cc-mini-table">
                                                    <thead>
                                                        <tr>
                                                            <th>Date</th>
                                                            <th>Description</th>
                                                            <th>Category</th>
                                                            <th>Amount</th>
                                                        </tr>
                                                    </thead>
                                                    <tbody>
                                                        {#each merchantTransactions as tx}
                                                            <tr>
                                                                <td>{formatDate(tx.date)}</td>
                                                                <td>{tx.description}</td>
                                                                <td>{tx.category || 'Uncategorized'}</td>
                                                                <td>{formatCurrency(tx.amount, 2)}</td>
                                                            </tr>
                                                        {/each}
                                                    </tbody>
                                                </table>
                                            </div>
                                        {/if}
                                        </section>
                                    {/if}
                                {/if}
                            {/each}
                        </div>
                    {/if}
                </div>
        </section>

    {:else if activeTab === 'rules'}
        <!-- ── RULES ─────────────────────────────────────────────── -->
        <div class="cc-shell fade-in-up">
            <section class="cc-pane cc-pane-primary">
                <div class="cc-pane-header">
                    <div class="cc-pane-title">
                        <h3>Categorization Rules</h3>
                        <p>Merchant signals that route familiar transactions into the right categories before Mira weighs in.</p>
                    </div>
                    <div class="cc-toolbar">
                        <select class="cc-select" bind:value={ruleSourceFilter}>
                            <option value="all">All sources</option>
                            <option value="user">User rules</option>
                            <option value="system">System rules</option>
                        </select>
                        <select class="cc-select" bind:value={ruleStateFilter}>
                            <option value="working">Working set</option>
                            <option value="all">Active + paused</option>
                            <option value="active">Active only</option>
                            <option value="inactive">Paused only</option>
                        </select>
                        <input class="cc-search" bind:value={ruleSearch} placeholder="Search pattern, category, or type…" />
                        <button class="cc-secondary-btn" type="button" on:click={loadRules} disabled={rulesLoading}>Refresh</button>
                    </div>
                </div>
                <div class="cc-list-wrap">
                    <div class="cc-calibration-strip cc-calibration-strip-inline">
                        <div class="cc-calibration-item">
                            <span>Working set</span>
                            <strong>{visibleRules.length}</strong>
                            <small>{activeRuleCount} active total</small>
                        </div>
                        <div class="cc-calibration-item">
                            <span>User rules</span>
                            <strong>{userRuleCount}</strong>
                            <small>Highest priority</small>
                        </div>
                        <div class="cc-calibration-item">
                            <span>Defaults</span>
                            <strong>{systemRuleCount}</strong>
                            <small>{hiddenPausedSystemRules} paused hidden</small>
                        </div>
                    </div>
                    <div class="cc-list-meta">
                        {visibleRules.length} rules · User rules carry the highest priority.
                        {#if ruleStateFilter === 'working' && hiddenPausedSystemRules > 0}
                            {hiddenPausedSystemRules} paused system defaults hidden.
                        {/if}
                    </div>
                    {#if rulesLoading}
                        <div class="cc-empty">Loading rules…</div>
                    {:else if visibleRules.length === 0}
                        <div class="cc-empty">No rules matched the current filters.</div>
                    {:else}
                        <div class="cc-table">
                            <div class="cc-table-header" style="--cc-cols: 1.8fr 1fr 0.8fr 0.6fr 0.7fr;">
                                <div>Signal</div>
                                <div>Routes to</div>
                                <div>Weight</div>
                                <div>Status</div>
                                <div>Origin</div>
                            </div>
                            {#each visibleRules as item}
                                <button
                                    type="button"
                                    class="cc-table-row"
                                    class:cc-table-row-active={item.id === selectedRuleId}
                                    style="--cc-cols: 1.8fr 1fr 0.8fr 0.6fr 0.7fr;"
                                    on:click={() => (selectedRuleId = item.id)}>
                                    <div class="cc-cell-primary">
                                        <div class="cc-cell-title">{item.pattern}</div>
                                    </div>
                                    <div class="cc-cell-subtitle">{item.category}</div>
                                    <div class="cc-cell-subtitle">{item.priority}</div>
                                    <div><span class={ruleStatusBadge(item)}>{item.is_active ? 'Active' : 'Paused'}</span></div>
                                    <div><span class={ruleSourceBadgeClass(item.source)}>{ruleSourceLabel(item.source)}</span></div>
                                </button>
                            {/each}
                        </div>
                    {/if}
                </div>
            </section>

            <aside class="cc-pane cc-pane-drawer">
                <div class="cc-inspector">
                    {#if !selectedRule}
                        <div class="cc-empty">Select a rule to tune where it routes, how strongly it applies, and whether it stays active.</div>
                    {:else}
                        <section class="cc-inspector-section">
                            <div>
                                <h3>{selectedRule.pattern}</h3>
                                <p>#{selectedRule.id} · {selectedRule.match_type} · created {formatDateTime(selectedRule.created_at)}</p>
                            </div>
                            <div class="cc-form-grid">
                                <div class="cc-field cc-field-full">
                                    <span>Routes to</span>
                                    <select class="cc-select" bind:value={ruleDraft.category}>
                                        {#each allCategoryNames as categoryName}
                                            <option value={categoryName}>{categoryName}</option>
                                        {/each}
                                    </select>
                                </div>
                                <div class="cc-field">
                                    <span>Weight</span>
                                    <input class="cc-input" type="number" bind:value={ruleDraft.priority} />
                                </div>
                                <div class="cc-field">
                                    <span>State</span>
                                    <select class="cc-select" bind:value={ruleDraft.is_active}>
                                        <option value={true}>Active</option>
                                        <option value={false}>Paused</option>
                                    </select>
                                </div>
                            </div>
                            <div class="cc-actions">
                                <button class="cc-primary-btn" type="button" on:click={saveRule} disabled={ruleSaving}>
                                    {ruleSaving ? 'Saving…' : 'Save Routing'}
                                </button>
                            </div>
                        </section>

                        <section class="cc-inspector-section">
                            <div>
                                <h3>Matching Spend</h3>
                                <p>Recent transactions this rule currently catches under the active profile.</p>
                            </div>
                            {#if ruleImpactLoading}
                                <div class="cc-empty">Calculating impact…</div>
                            {:else if !ruleImpact}
                                <div class="cc-empty">Impact preview unavailable.</div>
                            {:else}
                                <div class="cc-stats">
                                    <div class="cc-stat">
                                        <div class="cc-stat-label">Matches</div>
                                        <div class="cc-stat-value">{ruleImpact.match_count}</div>
                                    </div>
                                    <div class="cc-stat">
                                        <div class="cc-stat-label">Route</div>
                                        <div class="cc-stat-value">{ruleImpact.category}</div>
                                    </div>
                                </div>
                                {#if ruleImpact.sample?.length}
                                    <div class="cc-mini-table-wrap">
                                        <table class="cc-mini-table">
                                            <thead>
                                                <tr>
                                                    <th>Date</th>
                                                    <th>Description</th>
                                                    <th>Current Category</th>
                                                    <th>Amount</th>
                                                </tr>
                                            </thead>
                                            <tbody>
                                                {#each ruleImpact.sample as item}
                                                    <tr>
                                                        <td>{formatDate(item.date)}</td>
                                                        <td>{item.description}</td>
                                                        <td>{item.category}</td>
                                                        <td>{formatCurrency(item.amount, 2)}</td>
                                                    </tr>
                                                {/each}
                                            </tbody>
                                        </table>
                                    </div>
                                {:else}
                                    <div class="cc-empty">No transactions currently match this rule.</div>
                                {/if}
                            {/if}
                        </section>
                    {/if}
                </div>
            </aside>
        </div>

    {:else if activeTab === 'categories'}
        <!-- ── CATEGORIES ─────────────────────────────────────────── -->
        <div class="cc-pane cc-pane-primary cc-pane-categories fade-in-up">
            <div class="cc-pane-header">
                <div class="cc-pane-title">
                    <h3>Spend Behavior</h3>
                    <p>Decide which categories behave like fixed commitments and which stay flexible.</p>
                </div>
                <div class="cc-toolbar">
                    <input class="cc-search" bind:value={categorySearch} placeholder="Search category or spend type…" />
                    <button class="cc-secondary-btn" type="button" on:click={loadCategories} disabled={categoriesLoading}>Refresh</button>
                </div>
            </div>
            <div class="cc-list-wrap">
                <div class="cc-calibration-strip cc-calibration-strip-inline">
                    <div class="cc-calibration-item">
                        <span>Fixed</span>
                        <strong>{fixedCategoryCount}</strong>
                        <small>monthly commitments</small>
                    </div>
                    <div class="cc-calibration-item">
                        <span>Variable</span>
                        <strong>{variableCategoryCount}</strong>
                        <small>flexible spend</small>
                    </div>
                    <div class="cc-calibration-item">
                        <span>Locked</span>
                        <strong>{lockedCategoryCount}</strong>
                        <small>system categories</small>
                    </div>
                </div>
                <div class="cc-list-meta">{visibleCategories.length} categories with transactions.</div>
                {#if categoriesLoading}
                    <div class="cc-empty">Loading categories…</div>
                {:else if visibleCategories.length === 0}
                    <div class="cc-empty">No categories with transactions matched the search.</div>
                {:else}
                    <div class="cc-cat-table" style="--cc-cat-cols: minmax(14rem, 1fr) 13rem 9rem 14rem 3.75rem;">
                        <div class="cc-cat-header" style="--cc-cat-cols: minmax(14rem, 1fr) 13rem 9rem 14rem 3.75rem;">
                            <div>Spend area</div>
                            <div>Behavior</div>
                            <div>Activity</div>
                            <div>Move removed spend</div>
                            <div></div>
                        </div>
                        {#each visibleCategories as item (item.name)}
                            {@const isLocked = item.expense_type === 'non_expense'}
                            {@const isSaving = categorySavingKey === item.name}
                            {@const isCustom = !item.is_system}
                            {@const removeKey = removalKey('category', item.name)}
                            {@const isRemoving = removalSavingKey === removeKey}
                            {@const replacementCategoryOptions = filterCategoryNames(replacementOptionsFor(item), categoryReplacementSearch)}
                            <div class="cc-cat-row" style="--cc-cat-cols: minmax(14rem, 1fr) 13rem 9rem 14rem 3.75rem;" class:cc-cat-row-saving={isSaving || isRemoving}>
                                <div class="cc-cat-name">{item.name}</div>

                                <!-- Expense type pill toggle or lock -->
                                {#if isLocked}
                                    <div class="cc-cat-lock">
                                        <span class="material-symbols-outlined" style="font-size:14px;" title="System classification — cannot be changed">lock</span>
                                        <span style="font-size:0.73rem;">Non-expense</span>
                                    </div>
                                {:else}
                                    <div class="period-toggle-track"
                                         style="--seg-count: 2; --active-idx: {item.expense_type === 'fixed' ? 0 : 1};">
                                        <div class="period-toggle-thumb"></div>
                                        <button
                                            class="period-toggle-label"
                                            class:active={item.expense_type === 'fixed'}
                                            disabled={isSaving}
                                            on:click={() => saveExpenseTypeInline(item, 'fixed')}>
                                            Fixed
                                        </button>
                                        <button
                                            class="period-toggle-label"
                                            class:active={item.expense_type === 'variable'}
                                            disabled={isSaving}
                                            on:click={() => saveExpenseTypeInline(item, 'variable')}>
                                            Variable
                                        </button>
                                    </div>
                                {/if}

                                <div class="cc-cell-subtitle">
                                    {item.transaction_count || 0} tx · {item.active_rule_count || 0} rules
                                </div>

                                <div class="cc-cat-replacement-wrap">
                                    {#if isCustom}
                                        <button
                                            type="button"
                                            class="cc-cat-replacement-trigger"
                                            class:cc-select-attention={categoryReplacementRequiredKey === item.name}
                                            class:cc-cat-replacement-trigger-open={openCategoryReplacementKey === item.name}
                                            disabled={isRemoving}
                                            on:click={() => toggleCategoryReplacement(item)}>
                                            <span>{categoryReplacementValue(item) || 'Move into...'}</span>
                                            <span class="material-symbols-outlined text-[14px]" class:cc-chevron-open={openCategoryReplacementKey === item.name}>expand_more</span>
                                        </button>
                                        {#if openCategoryReplacementKey === item.name}
                                            <div class="cc-cat-replacement-dropdown" role="presentation" on:click|stopPropagation>
                                                <div class="cc-category-menu-search-wrap">
                                                    <span class="material-symbols-outlined text-[14px]">search</span>
                                                    <input
                                                        class="cc-category-menu-search"
                                                        bind:value={categoryReplacementSearch}
                                                        placeholder="Search categories..."
                                                        on:click|stopPropagation
                                                        on:keydown|stopPropagation={(event) => {
                                                            if (event.key === 'Escape') {
                                                                openCategoryReplacementKey = null;
                                                                categoryReplacementSearch = '';
                                                            }
                                                        }}
                                                    />
                                                </div>
                                                <div class="cc-category-menu-list">
                                                <button
                                                    type="button"
                                                    class="cc-cat-replacement-option"
                                                    class:active={!categoryReplacementValue(item)}
                                                    on:click={() => updateCategoryReplacement(item, '')}>
                                                    <span class="cc-cat-replacement-option-label">
                                                        <span class="material-symbols-outlined">{!categoryReplacementValue(item) ? 'check' : 'radio_button_unchecked'}</span>
                                                        <span>Move into...</span>
                                                    </span>
                                                </button>
                                                {#each replacementCategoryOptions as categoryName}
                                                    <button
                                                        type="button"
                                                        class="cc-cat-replacement-option"
                                                        class:active={categoryReplacementValue(item) === categoryName}
                                                        on:click={() => updateCategoryReplacement(item, categoryName)}>
                                                        <span class="cc-cat-replacement-option-label">
                                                            <span class="material-symbols-outlined">{categoryReplacementValue(item) === categoryName ? 'check' : 'radio_button_unchecked'}</span>
                                                            <span>{categoryName}</span>
                                                        </span>
                                                    </button>
                                                {/each}
                                                {#if replacementCategoryOptions.length === 0 && categoryReplacementSearch}
                                                    <div class="cc-category-menu-empty">No matching categories</div>
                                                {/if}
                                                </div>
                                            </div>
                                        {/if}
                                        {#if categoryReplacementRequiredKey === item.name}
                                            <div class="cc-inline-help">Pick a destination for these transactions.</div>
                                        {/if}
                                    {:else}
                                        <span class="cc-cell-subtitle">—</span>
                                    {/if}
                                </div>

                                <div class="cc-row-actions">
                                    {#if isCustom}
                                        {#if pendingRemovalKey === removeKey}
                                            <div class="cc-inline-confirm" aria-label="Confirm category removal">
                                                <button class="cc-confirm-btn cc-confirm-accept" type="button" disabled={isRemoving} on:click={() => removeCustomCategory(item)} title="Confirm remove">
                                                    <span class="material-symbols-outlined text-[16px]">{isRemoving ? 'progress_activity' : 'check'}</span>
                                                </button>
                                                <button class="cc-confirm-btn cc-confirm-cancel" type="button" disabled={isRemoving} on:click={clearRemoval} title="Cancel">
                                                    <span class="material-symbols-outlined text-[16px]">close</span>
                                                </button>
                                            </div>
                                        {:else}
                                            <button class="cc-conn-remove" type="button" on:click={() => requestCategoryRemoval(item)} title="Remove category">
                                                <span class="material-symbols-outlined text-[16px]">delete</span>
                                            </button>
                                        {/if}
                                    {/if}
                                </div>
                            </div>
                        {/each}
                    </div>
                {/if}
            </div>
        </div>

    {:else if activeTab === 'history'}
        <!-- ── HISTORY ────────────────────────────────────────────── -->
        <div class="cc-shell fade-in-up">
            <section class="cc-pane cc-pane-primary">
                <div class="cc-pane-header">
                    <div class="cc-pane-title">
                        <h3>Mira Activity</h3>
                        <p>Recent reads and writes, with the response and tool audit kept together.</p>
                    </div>
                    <div class="cc-toolbar">
                        <input class="cc-search" bind:value={historySearch} placeholder="Search prompt, response, or tool details..." />
                        <button class="cc-secondary-btn" type="button" on:click={loadHistory} disabled={historyLoading}>Refresh</button>
                    </div>
                </div>
                <div class="cc-list-wrap">
                    <div class="cc-calibration-strip cc-calibration-strip-inline">
                        <div class="cc-calibration-item">
                            <span>Interactions</span>
                            <strong>{visibleHistory.length}</strong>
                            <small>current profile</small>
                        </div>
                        <div class="cc-calibration-item">
                            <span>Reads</span>
                            <strong>{historyReadCount}</strong>
                            <small>conversation context</small>
                        </div>
                        <div class="cc-calibration-item">
                            <span>Writes</span>
                            <strong>{historyWriteCount}</strong>
                            <small>rows changed</small>
                        </div>
                    </div>
                    <div class="cc-list-meta">{visibleHistory.length} recent Mira interactions for the current profile.</div>
                    {#if historyLoading}
                        <div class="cc-empty">Loading history…</div>
                    {:else if visibleHistory.length === 0}
                        <div class="cc-empty">No history matched the search.</div>
                    {:else}
                        <div class="cc-table">
                            <div class="cc-table-header" style="--cc-cols: 1.7fr 0.75fr 0.65fr 0.95fr;">
                                <div>Moment</div>
                                <div>Mode</div>
                                <div>Changed</div>
                                <div>When</div>
                            </div>
                            {#each visibleHistory as item}
                                <button
                                    type="button"
                                    class="cc-table-row"
                                    class:cc-table-row-active={item.id === selectedHistoryId}
                                    style="--cc-cols: 1.7fr 0.75fr 0.65fr 0.95fr;"
                                    on:click={() => (selectedHistoryId = item.id)}>
                                    <div class="cc-cell-primary">
                                        <div class="cc-cell-title">{item.user_message}</div>
                                        <div class="cc-cell-subtitle">{item.assistant_response || 'No response saved'}</div>
                                    </div>
                                    <div class="cc-cell-subtitle">{item.operation_type || 'read'}</div>
                                    <div class="cc-cell-subtitle">{item.rows_affected || 0}</div>
                                    <div class="cc-cell-subtitle">{formatDateTime(item.created_at)}</div>
                                </button>
                            {/each}
                        </div>
                    {/if}
                </div>
            </section>

            <aside class="cc-pane cc-pane-drawer">
                <div class="cc-inspector">
                    {#if !selectedHistory}
                        <div class="cc-empty">Select a history row to inspect its response and tool audit details.</div>
                    {:else}
                        <section class="cc-inspector-section">
                            <div>
                                <h3>Interaction #{selectedHistory.id}</h3>
                                <p>{selectedHistory.operation_type || 'read'} · {selectedHistory.rows_affected || 0} rows · {formatDateTime(selectedHistory.created_at)}</p>
                            </div>
                            <div class="cc-field cc-field-full">
                                <span>User message</span>
                                <textarea class="cc-textarea" rows="3" disabled>{selectedHistory.user_message}</textarea>
                            </div>
                            <div class="cc-field cc-field-full">
                                <span>Mira response</span>
                                <textarea class="cc-textarea" rows="5" disabled>{selectedHistory.assistant_response || '—'}</textarea>
                            </div>
                            <div class="cc-field cc-field-full">
                                <span>Backend trace</span>
                                <textarea class="cc-textarea cc-textarea-mono" rows="8" disabled>{selectedHistory.query_result || selectedHistory.generated_sql || '—'}</textarea>
                            </div>
                            <div class="cc-actions">
                                <button class="cc-secondary-btn" type="button" on:click={reopenInCopilot}>Open in Mira</button>
                            </div>
                        </section>
                    {/if}
                </div>
            </aside>
        </div>
    {/if}
</div>

<style>
    .cc-cat-row-saving {
        opacity: 0.6;
        pointer-events: none;
    }

    .cc-pane {
        position: relative;
        border-color: color-mix(in srgb, var(--card-border) 92%, transparent);
        border-radius: 1.05rem;
        background:
            linear-gradient(180deg, color-mix(in srgb, var(--card-bg) 98%, white 1%), color-mix(in srgb, var(--card-bg) 94%, var(--surface-100)));
        box-shadow: 0 14px 34px rgba(3, 12, 28, 0.1);
    }
    .cc-pane::after {
        content: '';
        position: absolute;
        inset: 0;
        border-radius: inherit;
        background: linear-gradient(160deg, rgba(255, 255, 255, 0.22), rgba(255, 255, 255, 0.035) 22%, transparent 58%);
        pointer-events: none;
        z-index: 0;
    }
    .cc-pane > * {
        position: relative;
        z-index: 1;
    }
    :global(.dark) .cc-pane {
        background: var(--card-bg);
        border-color: var(--card-border);
        box-shadow: 0 14px 34px rgba(0, 0, 0, 0.2);
    }
    :global(.dark) .cc-pane::after {
        display: none;
    }
    :global(.dark) .cc-management-grid,
    :global(.dark) .cc-management-side {
        background: transparent;
    }

    .cc-ops-grid {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 1rem;
        align-items: start;
        width: 100%;
    }
    .cc-connections-layout {
        align-self: stretch;
    }
    .cc-ops-pane,
    .cc-bank-pane,
    .cc-local-ai-pane {
        width: 100%;
    }
    .cc-local-ai-pane {
        position: sticky;
        top: 1rem;
    }
    .cc-local-ai-body {
        padding: 12px 14px 14px;
    }
    .cc-management-grid {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        align-items: start;
        border-top: 1px solid color-mix(in srgb, var(--card-border) 82%, transparent);
        background: color-mix(in srgb, var(--body-bg) 8%, transparent);
    }
    .cc-management-main {
        grid-column: 1;
        grid-row: 1 / span 3;
        min-width: 0;
        padding: 1rem 1.1rem 1.1rem;
        border-right: 1px solid color-mix(in srgb, var(--card-border) 96%, transparent);
        box-shadow: inset -14px 0 24px -26px rgba(15, 23, 42, 0.5);
    }
    .cc-management-side {
        grid-column: 2;
        min-width: 0;
        min-height: 100%;
        background: linear-gradient(180deg, color-mix(in srgb, var(--surface-100) 22%, transparent), transparent 42%);
    }

    .cc-calibration-strip {
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: 0.65rem;
        padding: 0.9rem 1.1rem;
        border-bottom: 1px solid color-mix(in srgb, var(--card-border) 70%, transparent);
        background:
            linear-gradient(180deg, color-mix(in srgb, var(--card-bg) 58%, transparent), transparent);
    }
    .cc-calibration-strip-inline {
        margin: 0 0 0.85rem;
        padding: 0 0 0.85rem;
        border-bottom-color: color-mix(in srgb, var(--card-border) 58%, transparent);
        background: transparent;
    }
    .cc-calibration-item {
        min-width: 0;
        padding: 0.72rem 0.82rem;
        border: 1px solid color-mix(in srgb, var(--card-border) 68%, transparent);
        border-radius: 0.78rem;
        background:
            linear-gradient(180deg, color-mix(in srgb, var(--card-bg) 80%, transparent), color-mix(in srgb, var(--card-bg-flat) 42%, transparent));
        box-shadow: inset 2px 0 0 color-mix(in srgb, var(--positive) 24%, transparent);
    }
    .cc-calibration-item span {
        display: block;
        color: var(--text-muted);
        font-size: 0.6rem;
        font-weight: 800;
        letter-spacing: 0.12em;
        line-height: 1.2;
        text-transform: uppercase;
    }
    .cc-calibration-item strong {
        display: block;
        margin-top: 0.28rem;
        color: var(--text-primary);
        font-family: var(--font-financial);
        font-size: clamp(1.05rem, 0.5vw + 0.9rem, 1.42rem);
        font-weight: 400;
        line-height: 1;
        font-variant-numeric: tabular-nums;
    }
    .cc-calibration-item small {
        display: block;
        margin-top: 0.22rem;
        color: var(--text-muted);
        font-size: 0.68rem;
        font-weight: 650;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }
    .cc-tab-facts {
        background:
            linear-gradient(180deg, color-mix(in srgb, var(--card-bg) 42%, transparent), transparent);
    }

    /* ── Connections tab ───────────────────────────────────────── */
    .cc-ops-grid .cc-pane-header,
    .cc-ops-pane .cc-pane-header {
        padding: 1rem 1.05rem 0.85rem;
    }
    .cc-ops-pane .cc-insights {
        margin: 0;
        padding: 1rem 1.1rem;
        border-bottom: 1px solid color-mix(in srgb, var(--card-border) 74%, transparent);
    }
    .cc-management-side .cc-form-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
        column-gap: 0.9rem;
        row-gap: 0.95rem;
    }
    .cc-management-side .cc-field-full {
        grid-column: 1 / -1;
    }
    .cc-management-side .cc-field {
        gap: 0.42rem;
    }
    .cc-management-side .cc-actions {
        margin-top: 0.95rem;
        padding-top: 0.95rem;
        border-top: 1px solid color-mix(in srgb, var(--card-border) 72%, transparent);
    }
    .cc-ops-pane .cc-table {
        gap: 0.55rem;
    }
    .cc-ops-pane .cc-table-header,
    .cc-ops-pane .cc-table-row {
        gap: 1.1rem;
        padding: 0.72rem 0.88rem;
    }
    .cc-ops-pane .cc-table-row {
        border-radius: 0.78rem;
        background: color-mix(in srgb, var(--card-bg) 76%, transparent);
        box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.38);
        cursor: default;
    }
    .cc-ops-pane .cc-table-row:hover {
        background: color-mix(in srgb, var(--accent) 3%, var(--card-bg));
        transform: none;
    }
    .cc-ops-pane .cc-insight-card {
        padding: 0.8rem 0.9rem;
        border-radius: 0.8rem;
        background:
            linear-gradient(180deg, color-mix(in srgb, var(--card-bg) 82%, transparent), color-mix(in srgb, var(--card-bg-flat) 48%, var(--card-bg)));
        box-shadow:
            inset 2px 0 0 color-mix(in srgb, var(--accent) 28%, transparent),
            inset 0 1px 0 rgba(255, 255, 255, 0.38);
    }
    .cc-management-side .cc-input,
    .cc-management-side .cc-select,
    .cc-local-ai-pane .cc-input,
    .cc-local-ai-pane .cc-select {
        min-height: 0;
        padding: 0.68rem 0.82rem;
        border-radius: 0.78rem;
        font-size: 0.84rem;
        background: color-mix(in srgb, var(--card-bg) 82%, transparent);
    }
    .cc-cat-table {
        gap: 0.55rem;
    }
    .cc-cat-header,
    .cc-cat-row {
        gap: 1.05rem;
        padding-inline: 0.88rem;
    }
    .cc-cat-row {
        min-height: 4.2rem;
        border-radius: 0.78rem;
        background: color-mix(in srgb, var(--card-bg) 76%, transparent);
        box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.38);
    }
    .cc-cat-row:hover {
        background: color-mix(in srgb, var(--accent) 3%, var(--card-bg));
    }
    .cc-cat-row .period-toggle-track {
        justify-self: start;
    }
    .cc-pane-categories,
    .cc-pane-categories .cc-list-wrap,
    .cc-cat-table {
        overflow: visible;
    }
    .cc-cat-row:has(.cc-cat-replacement-dropdown) {
        z-index: 70;
        position: relative;
    }
    .cc-cat-replacement-wrap {
        position: relative;
        width: 100%;
        min-width: 0;
    }
    .cc-cat-replacement-trigger {
        display: inline-flex;
        align-items: center;
        justify-content: space-between;
        gap: 0.55rem;
        width: 100%;
        min-height: 2.35rem;
        padding: 0 0.78rem;
        border-radius: 0.78rem;
        border: 1px solid color-mix(in srgb, var(--card-border) 84%, transparent);
        background: color-mix(in srgb, var(--card-bg) 82%, transparent);
        color: var(--text-primary);
        font-size: 0.84rem;
        font-weight: 650;
        cursor: pointer;
        text-align: left;
        transition: border-color var(--duration-fast) ease, box-shadow var(--duration-fast) ease, background var(--duration-fast) ease;
    }
    .cc-cat-replacement-trigger:hover {
        background: color-mix(in srgb, var(--surface-100) 56%, var(--card-bg));
        border-color: var(--card-border-hover);
    }
    .cc-cat-replacement-trigger-open {
        border-color: var(--accent);
        box-shadow: 0 0 0 2px var(--accent-soft);
    }
    .cc-cat-replacement-trigger:disabled {
        opacity: 0.58;
        cursor: not-allowed;
    }
    .cc-cat-replacement-dropdown {
        position: absolute;
        top: calc(100% + 6px);
        left: 50%;
        z-index: 100;
        display: flex;
        flex-direction: column;
        width: max-content;
        min-width: 260px;
        max-width: 320px;
        max-height: 360px;
        overflow: hidden;
        padding: 4px;
        border-radius: 14px;
        border: 1px solid var(--card-border);
        background: var(--card-bg);
        backdrop-filter: blur(20px);
        -webkit-backdrop-filter: blur(20px);
        box-shadow:
            0 4px 16px rgba(0, 0, 0, 0.08),
            0 12px 40px rgba(0, 0, 0, 0.06);
        transform: translateX(-50%);
        animation: ccCatReplacementDropIn 0.18s var(--ease-out-expo) both;
    }
    .cc-row-category-dropdown {
        display: flex;
        flex-direction: column;
        overflow: hidden;
        padding: 4px;
    }
    .cc-category-menu-search-wrap {
        display: flex;
        align-items: center;
        gap: 0.38rem;
        padding: 0.5rem 0.62rem;
        border-bottom: 1px solid var(--card-border);
        color: var(--text-muted);
    }
    .cc-category-menu-search {
        min-width: 0;
        flex: 1;
        border: 0;
        outline: 0;
        background: transparent;
        color: var(--text-primary);
        font-size: 0.75rem;
        font-weight: 500;
    }
    .cc-category-menu-search::placeholder {
        color: var(--text-muted);
        opacity: 0.62;
    }
    .cc-category-menu-list {
        flex: 1;
        min-height: 0;
        overflow-y: auto;
        padding: 4px;
    }
    .cc-category-menu-list::-webkit-scrollbar { width: 4px; }
    .cc-category-menu-list::-webkit-scrollbar-track { background: transparent; }
    .cc-category-menu-list::-webkit-scrollbar-thumb { background: var(--surface-300); border-radius: 2px; }
    .cc-category-menu-empty {
        padding: 0.55rem 0.75rem;
        color: var(--text-muted);
        font-size: 0.72rem;
    }
    @keyframes ccCatReplacementDropIn {
        from {
            opacity: 0;
            transform: translateX(-50%) translateY(-4px) scale(0.97);
        }
        to {
            opacity: 1;
            transform: translateX(-50%) translateY(0) scale(1);
        }
    }
    .cc-cat-replacement-option {
        display: flex;
        align-items: center;
        justify-content: space-between;
        width: 100%;
        padding: 0.5rem 0.75rem;
        border: none;
        border-radius: 10px;
        background: transparent;
        color: var(--text-secondary);
        font-size: 0.75rem;
        font-weight: 500;
        cursor: pointer;
        text-align: left;
        transition: background var(--duration-fast) ease, color var(--duration-fast) ease;
    }
    .cc-cat-replacement-option:hover {
        background: var(--surface-100);
        color: var(--text-primary);
    }
    .cc-cat-replacement-option.active {
        background: var(--accent-soft);
        color: var(--accent);
        font-weight: 600;
        box-shadow: 0 0 0 2px rgba(56, 189, 248, 0.25), 0 0 8px rgba(56, 189, 248, 0.08);
    }
    .cc-cat-replacement-option-label {
        display: flex;
        align-items: center;
        gap: 0.5rem;
        min-width: 0;
    }
    .cc-cat-replacement-option-label .material-symbols-outlined {
        flex-shrink: 0;
        width: 16px;
        font-size: 16px;
    }
    .cc-cat-replacement-option:not(.active) .material-symbols-outlined {
        opacity: 0;
    }
    .cc-cat-replacement-option-label span:last-child {
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }
    :global(.dark) .cc-cat-replacement-dropdown {
        background: var(--bg-level-2);
        border-color: rgba(82, 100, 124, 0.50);
        box-shadow:
            0 4px 16px rgba(0, 0, 0, 0.25),
            0 12px 40px rgba(0, 0, 0, 0.20),
            0 0 0 1px rgba(56, 189, 248, 0.06);
    }
    :global(.dark) .cc-cat-replacement-option:hover {
        background: rgba(90, 159, 212, 0.08);
    }
    :global(.dark) .cc-cat-replacement-option.active {
        background: rgba(90, 159, 212, 0.12);
        color: var(--accent);
    }

    /* ── Merchants tab ─────────────────────────────────────────── */
    .cc-pane-merchants .cc-pane-header {
        padding: 1rem 1.1rem 0.9rem;
        z-index: 30;
        overflow: visible;
    }
    .cc-pane-merchants .cc-list-wrap {
        padding-inline: 1.1rem;
        z-index: 1;
    }
    .cc-pane-merchants .cc-toolbar,
    .cc-pane-merchants .cc-toolbar-pill-wrap {
        overflow: visible;
    }
    .cc-pane-merchants .cc-toolbar-pill-wrap {
        z-index: 80;
    }
    .cc-pane-merchants .cc-insights {
        margin: 0;
        padding: 0.9rem 0 1rem;
        border-top: 1px solid color-mix(in srgb, var(--card-border) 64%, transparent);
        border-bottom: 1px solid color-mix(in srgb, var(--card-border) 64%, transparent);
    }
    .cc-pane-merchants .cc-insight-card {
        padding: 0.78rem 0.9rem;
        border-radius: 0.8rem;
        background:
            linear-gradient(180deg, color-mix(in srgb, var(--card-bg) 82%, transparent), color-mix(in srgb, var(--card-bg-flat) 48%, var(--card-bg)));
        box-shadow:
            inset 2px 0 0 color-mix(in srgb, var(--accent) 28%, transparent),
            inset 0 1px 0 rgba(255, 255, 255, 0.38);
    }
    :global(.dark) .cc-tab-facts,
    :global(.dark) .cc-calibration-strip {
        border-top-color: rgba(148, 163, 184, 0.14);
        border-bottom-color: rgba(148, 163, 184, 0.16);
        background:
            radial-gradient(ellipse at 14% 0%, rgba(94, 234, 212, 0.035), transparent 36%),
            radial-gradient(ellipse at 86% 8%, rgba(96, 165, 250, 0.05), transparent 42%),
            linear-gradient(180deg, rgba(48, 54, 66, 0.42), rgba(37, 43, 53, 0.34));
    }
    :global(.dark) .cc-calibration-strip-inline {
        background:
            radial-gradient(ellipse at 14% 0%, rgba(94, 234, 212, 0.03), transparent 36%),
            radial-gradient(ellipse at 86% 8%, rgba(96, 165, 250, 0.04), transparent 42%),
            linear-gradient(180deg, rgba(48, 54, 66, 0.30), transparent 82%);
    }
    :global(.dark) .cc-tab-facts .cc-insight-card,
    :global(.dark) .cc-calibration-strip .cc-calibration-item {
        border-color: rgba(148, 163, 184, 0.22);
        background:
            radial-gradient(ellipse at 12% 0%, rgba(94, 234, 212, 0.04), transparent 36%),
            radial-gradient(ellipse at 88% 10%, rgba(96, 165, 250, 0.055), transparent 44%),
            linear-gradient(180deg, rgba(55, 62, 74, 0.66), rgba(40, 46, 56, 0.78));
        box-shadow:
            inset 3px 0 0 rgba(96, 165, 250, 0.34),
            inset 0 1px 0 rgba(255, 255, 255, 0.055),
            0 10px 24px rgba(0, 0, 0, 0.14);
    }
    :global(.dark) .cc-tab-facts .cc-insight-label,
    :global(.dark) .cc-calibration-strip .cc-calibration-item span {
        color: rgba(203, 213, 225, 0.68);
    }
    :global(.dark) .cc-tab-facts .cc-insight-card strong,
    :global(.dark) .cc-calibration-strip .cc-calibration-item strong {
        color: rgba(248, 250, 252, 0.94);
    }
    :global(.dark) .cc-tab-facts .cc-insight-card small,
    :global(.dark) .cc-calibration-strip .cc-calibration-item small {
        color: rgba(203, 213, 225, 0.68);
    }
    :global(.dark) .cc-management-grid .cc-management-side {
        border-right-color: rgba(148, 163, 184, 0.14);
        background:
            radial-gradient(ellipse at 12% 0%, rgba(94, 234, 212, 0.03), transparent 36%),
            radial-gradient(ellipse at 88% 8%, rgba(96, 165, 250, 0.04), transparent 42%),
            linear-gradient(180deg, rgba(47, 53, 64, 0.58), rgba(36, 41, 51, 0.68));
    }
    :global(.dark) .cc-management-side .cc-input,
    :global(.dark) .cc-management-side .cc-select {
        border-color: rgba(148, 163, 184, 0.18);
        background: rgba(31, 38, 49, 0.72);
    }
    .cc-pane-merchants .cc-list-meta {
        padding: 0.8rem 0 0.75rem;
        border-bottom: 1px solid color-mix(in srgb, var(--card-border) 54%, transparent);
        line-height: 1.45;
    }
    .cc-pane-merchants .cc-table {
        gap: 0.45rem;
        padding-top: 0.75rem;
    }
    .cc-merchant-table-header {
        padding: 0 0.88rem 0.15rem;
    }
    .cc-merchant-row {
        gap: 1rem;
        min-height: 3.95rem;
        padding: 0.58rem 0.88rem;
        border-radius: 0.78rem;
        background: color-mix(in srgb, var(--card-bg) 76%, transparent);
        box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.38);
    }
    .cc-merchant-row:hover {
        background: color-mix(in srgb, var(--accent) 3%, var(--card-bg));
    }
    .cc-merchant-row-archived {
        opacity: 0.92;
    }
    .cc-merchant-row .cc-row-category-btn {
        min-height: 2.3rem;
        padding: 0 0.8rem;
        border-radius: 0.78rem;
        font-size: 0.82rem;
    }
    .cc-merchant-identity {
        display: flex;
        align-items: center;
        gap: 0.62rem;
        min-width: 0;
    }
    .cc-merchant-avatar {
        width: 2rem;
        height: 2rem;
        border-radius: 0.66rem;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        flex: 0 0 auto;
        background: color-mix(in srgb, var(--accent) 12%, var(--surface-100));
        border: 1px solid color-mix(in srgb, var(--card-border) 88%, transparent);
        overflow: hidden;
        font-size: 0.66rem;
        font-weight: 800;
        color: var(--accent);
    }
    .cc-merchant-avatar img {
        width: 100%;
        height: 100%;
        object-fit: contain;
    }
    .cc-merchant-name-stack {
        min-width: 0;
        display: grid;
        gap: 0.12rem;
    }
    .cc-merchant-name-line {
        display: flex;
        align-items: center;
        gap: 0.35rem;
        min-width: 0;
        color: var(--text-primary);
        font-size: 0.88rem;
        font-weight: 700;
    }
    .cc-merchant-recurring {
        flex: 0 0 auto;
        font-size: 0.82rem;
        color: var(--accent);
        opacity: 0.74;
    }
    .cc-merchant-row-meta {
        color: var(--text-muted);
        font-size: 0.72rem;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }
    .cc-merchant-metric,
    .cc-merchant-date {
        color: var(--text-secondary);
        font-size: 0.82rem;
        font-variant-numeric: tabular-nums;
        white-space: nowrap;
    }
    .cc-merchant-date {
        color: var(--text-muted);
        font-size: 0.76rem;
    }
    .cc-merchant-scope-chip {
        display: inline-flex;
        align-items: center;
        justify-content: space-between;
        gap: 0.85rem;
        min-height: 2.75rem;
        min-width: min(20rem, 32vw);
        padding: 0.7rem 0.95rem;
        border-radius: 0.95rem;
        border: 1px solid color-mix(in srgb, var(--card-border) 80%, transparent);
        background: color-mix(in srgb, var(--card-bg) 78%, transparent);
        color: var(--text-primary);
        font-size: 0.84rem;
        font-weight: 700;
    }
    .cc-merchant-scope-chip small {
        color: var(--text-muted);
        font-size: 0.68rem;
        font-weight: 700;
        white-space: nowrap;
    }
    .cc-merchant-year-row {
        display: grid;
        grid-template-columns: auto auto minmax(0, 1fr);
        align-items: center;
        gap: 0.55rem;
        width: 100%;
        padding: 0.62rem 0.88rem;
        border: 1px solid color-mix(in srgb, var(--card-border) 74%, transparent);
        border-radius: 0.82rem;
        background: color-mix(in srgb, var(--surface-100) 28%, transparent);
        color: var(--text-primary);
        text-align: left;
        cursor: pointer;
    }
    .cc-merchant-year-row strong {
        font-size: 0.86rem;
        font-weight: 800;
    }
    .cc-merchant-year-row small {
        color: var(--text-muted);
        font-size: 0.76rem;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }
    .cc-merchant-year-row .material-symbols-outlined {
        font-size: 1rem;
        color: var(--text-muted);
        transition: transform 0.15s ease;
    }
    .cc-merchant-year-row:hover,
    .cc-merchant-year-row-open {
        border-color: color-mix(in srgb, var(--accent) 28%, var(--card-border));
        background: color-mix(in srgb, var(--accent) 6%, var(--surface-100));
    }
    .cc-pane-merchants .cc-row-expansion {
        margin: -0.18rem 0 0.55rem;
        padding: 0.9rem 1rem;
        border-radius: 0.9rem;
    }
    :global(.dark) .cc-ops-pane .cc-table-row,
    :global(.dark) .cc-cat-row,
    :global(.dark) .cc-merchant-row,
    :global(.dark) .cc-calibration-item,
    :global(.dark) .cc-runtime-current,
    :global(.dark) .cc-merchant-scope-chip,
    :global(.dark) .cc-merchant-year-row,
    :global(.dark) .cc-conn-row,
    :global(.dark) .cc-static-field,
    :global(.dark) .cc-local-llm-stat {
        background: color-mix(in srgb, var(--surface-100) 38%, transparent);
        box-shadow: none;
    }

    .cc-conn-section {
        padding: 1rem 1.1rem;
        border-bottom: 1px solid color-mix(in srgb, var(--card-border) 82%, transparent);
    }
    .cc-conn-section:last-of-type {
        border-bottom: none;
    }
    .cc-conn-section-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        margin-bottom: 10px;
    }
    .cc-conn-section-title {
        display: flex;
        align-items: center;
        gap: 8px;
    }
    .cc-conn-count {
        font-size: 12px;
        color: var(--text-muted);
    }
    .cc-provider-badge {
        display: inline-flex;
        align-items: center;
        padding: 3px 8px;
        border-radius: 6px;
        font-size: 11px;
        font-weight: 600;
        letter-spacing: 0.02em;
    }
    .cc-provider-teller {
        background: color-mix(in srgb, #6366f1 12%, transparent);
        color: #6366f1;
    }
    .cc-provider-simplefin {
        background: color-mix(in srgb, #10b981 12%, transparent);
        color: #10b981;
    }
    .cc-provider-local-ai {
        background: color-mix(in srgb, #38bdf8 12%, transparent);
        color: #38bdf8;
    }
    .cc-static-field {
        min-height: 34px;
        display: flex;
        align-items: center;
        padding: 8px 10px;
        border: 1px solid color-mix(in srgb, var(--card-border) 84%, transparent);
        border-radius: 0.78rem;
        background: color-mix(in srgb, var(--body-bg) 42%, var(--card-bg));
        color: var(--text-primary);
        font-size: 13px;
    }
    .cc-conn-row {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 0.7rem 0.8rem;
        border-radius: 0.85rem;
        border: 1px solid color-mix(in srgb, var(--card-border) 68%, transparent);
        background: color-mix(in srgb, var(--card-bg) 76%, transparent);
        margin-bottom: 0.48rem;
    }
    .cc-conn-row:last-child {
        margin-bottom: 0;
    }
    .cc-conn-info {
        flex: 1;
        min-width: 0;
    }
    .cc-conn-name {
        font-size: 13px;
        font-weight: 500;
        color: var(--text-primary);
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }
    .cc-conn-meta {
        font-size: 11px;
        color: var(--text-muted);
        margin-top: 2px;
    }
    .cc-row-actions {
        display: flex;
        align-items: center;
        justify-content: flex-end;
        gap: 6px;
        min-width: 0;
    }
    .cc-conn-remove {
        display: flex;
        align-items: center;
        justify-content: center;
        width: 28px;
        height: 28px;
        border: none;
        border-radius: 6px;
        background: transparent;
        color: var(--text-muted);
        cursor: pointer;
        flex-shrink: 0;
        transition: all 0.15s ease;
    }
    .cc-conn-remove:hover {
        background: var(--negative-light);
        color: var(--negative);
    }
    .cc-payment-action:hover {
        background: rgba(245, 158, 11, 0.14);
        color: #f59e0b;
    }
    .cc-payment-status {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        gap: 4px;
        min-height: 28px;
        padding: 0 8px;
        border: 1px solid color-mix(in srgb, var(--positive) 24%, var(--card-border));
        border-radius: 999px;
        background: color-mix(in srgb, var(--positive) 9%, transparent);
        color: var(--positive);
        cursor: pointer;
        flex-shrink: 0;
        font-size: 11px;
        font-weight: 800;
        line-height: 1;
        white-space: nowrap;
        transition: transform 0.15s ease, background 0.15s ease, border-color 0.15s ease;
    }
    .cc-payment-status .material-symbols-outlined {
        font-size: 15px;
    }
    .cc-payment-status:hover {
        transform: translateY(-1px);
        border-color: color-mix(in srgb, var(--positive) 42%, var(--card-border));
        background: color-mix(in srgb, var(--positive) 14%, transparent);
    }
    .cc-payment-status-fresh {
        box-shadow: 0 0 0 3px color-mix(in srgb, var(--positive) 12%, transparent);
    }
    .cc-payment-detail-row {
        cursor: default;
    }
    .cc-payment-detail-row:hover {
        transform: none;
    }
    .cc-payment-details {
        padding: 0.9rem;
    }
    .cc-payment-actions {
        align-items: end;
    }
    .cc-inline-confirm {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        padding: 3px;
        border: 1px solid color-mix(in srgb, var(--positive) 22%, var(--card-border));
        border-radius: 999px;
        background:
            linear-gradient(180deg, color-mix(in srgb, var(--card-bg) 94%, white 3%), color-mix(in srgb, var(--surface-100) 36%, var(--card-bg))),
            color-mix(in srgb, var(--positive) 5%, transparent);
        box-shadow:
            0 8px 18px rgba(3, 12, 28, 0.08),
            inset 0 1px 0 rgba(255, 255, 255, 0.56);
    }
    .cc-confirm-btn {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 26px;
        height: 26px;
        border: 0;
        border-radius: 999px;
        cursor: pointer;
        transition: transform 0.14s ease, background 0.14s ease, color 0.14s ease;
    }
    .cc-confirm-btn:hover:not(:disabled) {
        transform: translateY(-1px);
    }
    .cc-confirm-btn:disabled {
        cursor: wait;
        opacity: 0.7;
    }
    .cc-confirm-accept {
        color: var(--positive);
        background: var(--positive-light);
    }
    .cc-confirm-accept:hover:not(:disabled) {
        background: color-mix(in srgb, var(--positive) 16%, transparent);
    }
    .cc-confirm-cancel {
        color: var(--text-muted);
        background: transparent;
    }
    .cc-confirm-cancel:hover:not(:disabled) {
        color: var(--negative);
        background: var(--negative-light);
    }
    .cc-conn-empty {
        font-size: 12px;
        color: var(--text-muted);
        padding: 8px 0;
    }
    .cc-local-llm-header {
        gap: 12px;
        align-items: flex-start;
    }
    .cc-local-llm-status-row {
        display: flex;
        align-items: center;
        gap: 8px;
        flex-wrap: wrap;
    }
    .cc-runtime-current {
        display: grid;
        grid-template-columns: minmax(0, 1fr) auto;
        align-items: center;
        gap: 0.65rem;
        margin-bottom: 0.75rem;
        padding: 0.68rem 0.78rem;
        border: 1px solid color-mix(in srgb, var(--card-border) 78%, transparent);
        border-radius: 0.82rem;
        background:
            linear-gradient(180deg, color-mix(in srgb, var(--card-bg) 76%, transparent), color-mix(in srgb, var(--surface-100) 18%, transparent));
        box-shadow: inset 2px 0 0 color-mix(in srgb, var(--positive) 20%, transparent);
    }
    .cc-runtime-current-copy {
        min-width: 0;
    }
    .cc-runtime-current-copy span,
    .cc-runtime-section-title span {
        display: block;
        color: var(--text-muted);
        font-size: 0.68rem;
        font-weight: 800;
        letter-spacing: 0.11em;
        line-height: 1.2;
        text-transform: uppercase;
    }
    .cc-runtime-current-copy strong {
        display: block;
        margin-top: 0.16rem;
        overflow: hidden;
        color: var(--text-primary);
        font-size: 0.95rem;
        font-weight: 800;
        line-height: 1.2;
        text-overflow: ellipsis;
        white-space: nowrap;
    }
    .cc-runtime-current-copy small,
    .cc-runtime-section-title small {
        display: block;
        margin-top: 0.22rem;
        color: var(--text-muted);
        font-size: 0.75rem;
        line-height: 1.35;
    }
    .cc-runtime-health {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        min-height: 1.5rem;
        padding: 0 0.58rem;
        border-radius: 999px;
        background: var(--surface-100);
        color: var(--text-muted);
        font-size: 0.66rem;
        font-weight: 800;
        white-space: nowrap;
    }
    .cc-runtime-health-ready {
        background: var(--positive-light);
        color: var(--positive);
    }
    .cc-local-llm-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
        gap: 8px;
        margin-bottom: 10px;
    }
    .cc-local-ai-pane .cc-local-llm-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    .cc-local-ai-pane .cc-runtime-grid {
        grid-template-columns: repeat(3, minmax(0, 1fr));
    }
    .cc-runtime-grid .cc-local-llm-stat {
        padding: 0.76rem 0.82rem;
    }
    .cc-local-llm-stat {
        padding: 10px;
        border-radius: 0.8rem;
        background:
            linear-gradient(180deg, color-mix(in srgb, var(--card-bg) 82%, transparent), color-mix(in srgb, var(--card-bg-flat) 44%, var(--card-bg)));
        border: 1px solid color-mix(in srgb, var(--card-border) 76%, transparent);
    }
    .cc-local-llm-stat strong {
        display: block;
        margin-top: 4px;
        font-size: 13px;
        color: var(--text-primary);
    }
    .cc-local-llm-stat small {
        display: block;
        margin-top: 2px;
        font-size: 11px;
        color: var(--text-muted);
    }
    .cc-local-llm-form {
        display: flex;
        flex-direction: column;
        gap: 0.85rem;
        padding-top: 0.8rem;
        border-top: 1px solid color-mix(in srgb, var(--card-border) 72%, transparent);
    }
    .cc-runtime-section-title {
        display: grid;
        gap: 0.05rem;
    }
    .cc-local-toggle {
        display: flex;
        align-items: center;
        gap: 8px;
        font-size: 12px;
        color: var(--text-secondary);
    }
    .cc-select-attention {
        border-color: color-mix(in srgb, var(--warning) 58%, var(--card-border)) !important;
        box-shadow: 0 0 0 3px var(--warning-light) !important;
    }
    .cc-inline-help {
        margin-top: 0.32rem;
        color: var(--warning);
        font-size: 0.68rem;
        font-weight: 700;
        line-height: 1.2;
    }
    .cc-secondary-btn-active {
        border-color: var(--card-border-hover);
        color: var(--text-primary);
        box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--accent) 18%, transparent);
    }
    .cc-model-disclosure {
        display: grid;
        gap: 10px;
    }
    .cc-disclosure-button {
        width: 100%;
        display: grid;
        grid-template-columns: minmax(0, 1fr) auto auto;
        align-items: center;
        gap: 10px;
        padding: 9px 10px;
        border-radius: 12px;
        border: 1px solid color-mix(in srgb, var(--card-border) 86%, transparent);
        background: color-mix(in srgb, var(--card-bg) 78%, transparent);
        color: var(--text-primary);
        font-size: 12px;
        font-weight: 750;
        text-align: left;
        cursor: pointer;
    }
    .cc-disclosure-button small {
        color: var(--text-muted);
        font-size: 11px;
        font-weight: 600;
        white-space: nowrap;
    }
    .cc-local-llm-tier-list {
        display: grid;
        gap: 10px;
    }
    .cc-local-llm-tier {
        padding: 12px;
        border-radius: 10px;
        background: color-mix(in srgb, var(--surface-100) 28%, transparent);
        border: 1px solid var(--card-border);
    }
    .cc-local-llm-tier-header {
        display: flex;
        align-items: baseline;
        justify-content: space-between;
        gap: 10px;
        margin-bottom: 10px;
    }
    .cc-local-llm-tier-title {
        font-size: 11px;
        font-weight: 700;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: var(--text-muted);
    }
    .cc-local-llm-tier-subtitle {
        font-size: 11px;
        color: var(--text-muted);
    }
    .cc-local-llm-model-list {
        display: grid;
        gap: 10px;
    }
    .cc-local-llm-model-row {
        display: grid;
        grid-template-columns: minmax(0, 1fr) auto;
        gap: 10px;
        padding: 10px;
        border-radius: 12px;
        border: 1px solid var(--card-border);
        background: color-mix(in srgb, var(--surface) 80%, transparent);
    }
    .cc-local-llm-model-row-installed {
        border-color: color-mix(in srgb, var(--positive) 22%, var(--card-border));
    }
    .cc-local-llm-model-main {
        min-width: 0;
    }
    .cc-local-llm-model-topline {
        display: flex;
        align-items: center;
        flex-wrap: wrap;
        gap: 8px;
        margin-bottom: 6px;
    }
    .cc-local-llm-model-topline strong {
        font-size: 14px;
        color: var(--text-primary);
    }
    .cc-local-llm-model-size {
        font-size: 12px;
        color: var(--text-secondary);
    }
    .cc-local-llm-model-badges {
        display: flex;
        flex-wrap: wrap;
        gap: 6px;
        margin-bottom: 6px;
    }
    .cc-local-llm-model-warning {
        font-size: 12px;
        line-height: 1.45;
        color: var(--text-muted);
    }
    .cc-local-llm-model-actions {
        display: grid;
        grid-template-columns: repeat(3, minmax(68px, auto));
        align-items: start;
        justify-content: end;
        gap: 6px;
    }
    .cc-local-llm-model-actions .cc-secondary-btn {
        min-height: 28px;
        padding: 0 9px;
        border-radius: 999px;
        font-size: 10px;
    }
    .cc-local-llm-chip-wrap {
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
    }
    .cc-local-llm-chip {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 6px 10px;
        border-radius: 999px;
        background: var(--surface-100);
        color: var(--text-secondary);
        font-size: 11px;
        border: 1px solid var(--card-border);
    }
    .cc-local-llm-chip-installed {
        color: var(--text-primary);
        border-color: color-mix(in srgb, var(--positive) 25%, var(--card-border));
    }
    .cc-local-llm-chip-note {
        opacity: 0.72;
        font-size: 10px;
    }
    .cc-local-llm-install-btn {
        width: 76px;
        justify-self: stretch;
        border: 1px solid color-mix(in srgb, var(--accent) 18%, var(--card-border));
        background: color-mix(in srgb, var(--accent) 8%, transparent);
        color: var(--accent);
        border-radius: 999px;
        padding: 3px 0;
        font-size: 10px;
        font-weight: 700;
        text-align: center;
        cursor: pointer;
        transition: opacity 0.15s ease, transform 0.15s ease;
    }
    .cc-local-llm-install-btn:disabled {
        opacity: 0.55;
        cursor: default;
        transform: none;
    }
    .cc-local-llm-install-state {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 76px;
        height: 28px;
        padding: 3px 0;
        border-radius: 999px;
        font-size: 10px;
        font-weight: 700;
        border: 1px solid color-mix(in srgb, var(--card-border) 75%, transparent);
        background: color-mix(in srgb, var(--surface-100) 72%, transparent);
        color: var(--text-secondary);
    }
    :global(.dark) .cc-local-ai-pane {
        border-color: rgba(148, 163, 184, 0.25);
        background:
            radial-gradient(ellipse at 14% 0%, rgba(94, 234, 212, 0.045), transparent 36%),
            radial-gradient(ellipse at 86% 8%, rgba(96, 165, 250, 0.065), transparent 42%),
            linear-gradient(180deg, rgba(48, 54, 66, 0.94), rgba(37, 43, 53, 0.98));
        box-shadow:
            inset 0 1px 0 rgba(255, 255, 255, 0.055),
            inset 0 -1px 0 rgba(0, 0, 0, 0.16),
            0 16px 38px rgba(0, 0, 0, 0.20);
    }
    :global(.dark) .cc-local-ai-pane .cc-pane-header {
        border-bottom-color: rgba(148, 163, 184, 0.16);
        background: rgba(31, 38, 49, 0.26);
    }
    :global(.dark) .cc-local-ai-pane .cc-runtime-current {
        border-color: rgba(148, 163, 184, 0.22);
        background:
            radial-gradient(ellipse at 10% 0%, rgba(94, 234, 212, 0.06), transparent 38%),
            radial-gradient(ellipse at 92% 12%, rgba(96, 165, 250, 0.09), transparent 46%),
            linear-gradient(180deg, rgba(55, 62, 74, 0.72), rgba(40, 46, 56, 0.82));
        box-shadow:
            inset 3px 0 0 rgba(94, 234, 212, 0.42),
            inset 0 1px 0 rgba(255, 255, 255, 0.055),
            0 10px 24px rgba(0, 0, 0, 0.14);
    }
    :global(.dark) .cc-local-ai-pane .cc-runtime-current-copy span,
    :global(.dark) .cc-local-ai-pane .cc-runtime-section-title span,
    :global(.dark) .cc-local-ai-pane .cc-insight-label,
    :global(.dark) .cc-local-ai-pane .cc-disclosure-button small,
    :global(.dark) .cc-local-ai-pane .cc-local-llm-tier-title,
    :global(.dark) .cc-local-ai-pane .cc-local-llm-tier-subtitle {
        color: rgba(203, 213, 225, 0.68);
    }
    :global(.dark) .cc-local-ai-pane .cc-runtime-current-copy strong,
    :global(.dark) .cc-local-ai-pane .cc-runtime-section-title,
    :global(.dark) .cc-local-ai-pane .cc-local-llm-stat strong,
    :global(.dark) .cc-local-ai-pane .cc-local-llm-model-topline strong {
        color: rgba(248, 250, 252, 0.94);
    }
    :global(.dark) .cc-local-ai-pane .cc-runtime-current-copy small,
    :global(.dark) .cc-local-ai-pane .cc-runtime-section-title small,
    :global(.dark) .cc-local-ai-pane .cc-local-llm-stat small,
    :global(.dark) .cc-local-ai-pane .cc-local-llm-model-warning {
        color: rgba(203, 213, 225, 0.68);
    }
    :global(.dark) .cc-local-ai-pane .cc-runtime-grid .cc-local-llm-stat,
    :global(.dark) .cc-local-ai-pane .cc-form-grid .cc-local-llm-stat,
    :global(.dark) .cc-local-ai-pane .cc-local-toggle,
    :global(.dark) .cc-local-ai-pane .cc-disclosure-button,
    :global(.dark) .cc-local-ai-pane .cc-local-llm-tier,
    :global(.dark) .cc-local-ai-pane .cc-local-llm-model-row {
        border-color: rgba(148, 163, 184, 0.16);
        background: linear-gradient(180deg, rgba(55, 62, 74, 0.58), rgba(40, 46, 56, 0.68));
    }
    :global(.dark) .cc-local-ai-pane .cc-local-toggle {
        min-height: 2.45rem;
        padding: 0.6rem 0.72rem;
        border-radius: 0.78rem;
    }
    :global(.dark) .cc-local-ai-pane .cc-local-llm-form {
        border-top-color: rgba(148, 163, 184, 0.16);
    }
    :global(.dark) .cc-local-ai-pane .cc-input,
    :global(.dark) .cc-local-ai-pane .cc-select {
        border-color: rgba(148, 163, 184, 0.18);
        background: rgba(31, 38, 49, 0.72);
    }
    :global(.dark) .cc-local-ai-pane .cc-runtime-health {
        border: 1px solid rgba(148, 163, 184, 0.18);
        background: rgba(31, 38, 49, 0.72);
    }
    :global(.dark) .cc-local-ai-pane .cc-runtime-health-ready {
        border-color: rgba(45, 212, 191, 0.28);
        background: rgba(45, 212, 191, 0.11);
    }
    :global(.dark) .cc-local-ai-pane .cc-local-llm-model-row-installed {
        border-color: rgba(45, 212, 191, 0.26);
        background:
            radial-gradient(ellipse at 8% 0%, rgba(45, 212, 191, 0.055), transparent 38%),
            linear-gradient(180deg, rgba(55, 62, 74, 0.6), rgba(40, 46, 56, 0.7));
    }
    :global(.dark) .cc-local-ai-pane .cc-local-llm-chip-note,
    :global(.dark) .cc-local-ai-pane .cc-local-llm-install-state {
        border-color: rgba(148, 163, 184, 0.15);
        background: rgba(31, 38, 49, 0.62);
        color: rgba(203, 213, 225, 0.74);
    }
    :global(.dark) .cc-local-ai-pane .cc-local-llm-chip-note {
        display: inline-flex;
        align-items: center;
        min-height: 1.1rem;
        padding: 0 0.34rem;
        border: 1px solid rgba(148, 163, 184, 0.15);
        border-radius: 999px;
    }
    .cc-notice-local-error {
        margin-bottom: 12px;
        background: color-mix(in srgb, var(--negative) 10%, transparent);
        border: 1px solid color-mix(in srgb, var(--negative) 18%, var(--card-border));
        color: var(--negative);
    }
    @media (max-width: 1180px) {
        .cc-ops-grid,
        .cc-management-grid {
            grid-template-columns: 1fr;
        }
        .cc-local-ai-pane {
            position: static;
        }
        .cc-management-main,
        .cc-management-side {
            grid-column: 1;
            grid-row: auto;
        }
        .cc-management-main {
            border-right: none;
            box-shadow: none;
            border-bottom: 1px solid color-mix(in srgb, var(--card-border) 86%, transparent);
        }
        .cc-calibration-strip {
            grid-template-columns: 1fr;
        }
    }
    @media (max-width: 900px) {
        .cc-local-llm-grid {
            grid-template-columns: repeat(2, minmax(0, 1fr));
        }
        .cc-local-ai-pane .cc-runtime-grid {
            grid-template-columns: repeat(2, minmax(0, 1fr));
        }
        .cc-runtime-current {
            grid-template-columns: minmax(0, 1fr) auto;
        }
        .cc-runtime-health {
            justify-self: start;
        }
        .cc-local-llm-model-row {
            grid-template-columns: 1fr;
        }
        .cc-local-llm-model-actions {
            grid-template-columns: 1fr;
            justify-content: stretch;
        }
        .cc-local-llm-install-state {
            display: none;
        }
        .cc-management-side .cc-form-grid {
            grid-template-columns: 1fr;
        }
    }
    @media (max-width: 640px) {
        .cc-local-llm-grid {
            grid-template-columns: 1fr;
        }
        .cc-local-ai-pane .cc-runtime-grid,
        .cc-runtime-current {
            grid-template-columns: 1fr;
        }
        .cc-runtime-health {
            grid-column: auto;
        }
    }
</style>
