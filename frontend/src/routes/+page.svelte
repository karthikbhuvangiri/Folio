<script>
    import '$lib/styles/dashboard.css';
    import '$lib/styles/transactions.css';
    import { goto } from '$app/navigation';
    import { page } from '$app/stores';
    import { onMount, onDestroy, tick } from 'svelte';
    import { api, invalidateCacheByPrefix, invalidateCache } from '$lib/api.js';
    import {
        formatCurrency, formatCompact, formatPercent, formatMonth, formatMonthShort,
        formatDate, getCurrentMonth, computeDelta, getGreeting,
        groupTransactionsByDate, CATEGORY_COLORS, CATEGORY_ICONS, springCount,
        computeTrailingSavingsRate
    } from '$lib/utils.js';
    import { darkMode, syncing, selectedPeriodStore, selectedCustomMonthStore, privacyMode } from '$lib/stores.js';
    import { profiles, activeProfile, loadProfiles } from '$lib/stores/profileStore.js';
    import SankeyChart from '$lib/components/SankeyChart.svelte';
    import SankeyLoadingStage from '$lib/components/SankeyLoadingStage.svelte';
    import IncomeVsSpendingChart from '$lib/components/IncomeVsSpendingChart.svelte';
    import ProfileSwitcher from '$lib/components/ProfileSwitcher.svelte';
    import ConnectionChooser from '$lib/components/ConnectionChooser.svelte';
    import TellerConnect from '$lib/components/TellerConnect.svelte';
    import SimpleFINConnect from '$lib/components/SimpleFINConnect.svelte';

    export let data;
    let summary = data.summary;
    let accounts = data.accounts;
    let monthly = data.monthly;
    let categories = data.categories;
    let loading = !data.summary;
    let profileSwitching = false;
    let isRefreshing = false;
    let _enrollmentInFlight = false;

    // Teller Connect configuration (fetched from backend)
    let tellerAppId = '';
    let tellerEnvironment = 'development';
    let migrationStatus = null;
    let migrationBannerDismissed = false;
    let appConfig = {
        demoMode: false,
        bankLinkingEnabled: true,
        manualSyncEnabled: true,
        demoPersistence: 'persistent',
        ...(data.config || {})
    };
    let connectionChooserOpen = false;
    let tellerConnectRef;
    let simplefinConnectRef;

    // Period — synced with global store for cross-page persistence
    let selectedPeriod = 'this_month';
    let selectedCustomMonth = '';

    // Hydrate from store on init
    selectedPeriodStore.subscribe(v => { selectedPeriod = v; });
    selectedCustomMonthStore.subscribe(v => { if (v) selectedCustomMonth = v; });
    let allMonths = [];
    let periodLoading = false;

    let periodSummary = null;
    let periodCategories = [];
    let sankeyCategoryList = [];
    let sankeyPillList = [];
    let periodCcRepaid = 0;
    let periodExternalTransfers = 0;
    let periodIncomingTransfers = 0;
    let periodCreditsRefunds = 0;
    let periodCashDeposits = 0;
    let periodCashWithdrawals = 0;
    let periodInvestmentInflows = 0;
    let periodInvestmentOutflows = 0;

    // Sankey: separated flows
    let sankeySavingsTotal = 0;
    let sankeyPersonalTransferTotal = 0;

    // Sankey drill-down
    let selectedSankeyCategory = null;
    let sankeyDrillTxns = [];
    let drillDownSection;
    let monthDropdownOpen = false;

    // Sankey drill-down re-categorization state
    let drillCatDropdownOpenForTx = null;   // original_id of tx whose dropdown is open
    let drillCatDropdownSearch = '';         // search/filter within the dropdown
    let drillAllCategories = [];            // category list for dropdown
    let drillCreatingNewCategory = false;
    let drillNewCategoryName = '';
    let drillNewCategoryError = '';
    let drillRecentlyUpdatedTxId = null;
    let drillUpdateFeedback = '';
    let drillCategoryApplyMode = 'always';

    // iOS-style period toggle
    const periodOptions = [
        { key: 'this_month', label: 'This Month' },
        { key: 'last_month', label: 'Last Month' },
        { key: 'ytd', label: 'YTD' },
        { key: 'all', label: 'All Time' }
    ];
    $: activePeriodIdx = Math.max(periodOptions.findIndex(p => p.key === selectedPeriod), 0);

    // Animation
    // Seed from load() data so the first paint shows real values — no flash of zeros.
    // The spring animation will run on mount to add polish, starting FROM these values
    // or from 0 if you still want the count-up effect.
    let animatedNetWorth = 0;
    let animatedIncome = 0;
    let animatedExpenses = 0;
    let animationDone = false;
    let animationStarted = false;
    let mounted = false;
    let animationFrameId = null;

    // Net worth trend data (full area chart)
    let netWorthTrendData = [];
    let netWorthMomDelta = data.netWorthMomDelta ?? null;
    let netWorthYtdDelta = data.netWorthYtdDelta ?? null;

    // Hover tooltip for net worth chart
    let hoverPoint = null;

    // Trailing savings rate
    let trailingSavingsInfo = { rate: 0, delta: 0, months: 0 };

    // Account deltas (month-over-month per account)
    let accountDeltas = {};

    // Categories to exclude from expenses (flow directly from income)
    const DIRECT_FLOW_CATEGORIES = ['Savings Transfer', 'Personal Transfer', 'Cash Withdrawal', 'Investment Transfer'];


    // Transfer sub-types for Sankey filtering
    const TRANSFER_INTERNAL = 'transfer_internal';
    const TRANSFER_HOUSEHOLD = 'transfer_household';
    const TRANSFER_EXTERNAL = 'transfer_external';

    // Fixed vs Variable spending
    const NON_SPENDING_CATEGORIES_SET = new Set(['Savings Transfer', 'Personal Transfer', 'Credit Card Payment', 'Cash Withdrawal', 'Cash Deposit', 'Investment Transfer', 'Income', 'Credits & Refunds']);
    const CREDIT_DRILLDOWN_CATEGORY = 'Credits & Refunds';
    const CREDIT_DRILLDOWN_COLOR = '#059669';
    let editingExpenseType = null;
    let expenseTypeFeedback = '';

    // Sankey theater mouse-tracking glow
    let sankeyTheaterEl = null;
    let sankeyGlowOpacity = 0;
    let debugLoadingMode = '';
    let hasRenderableSankeyData = false;

    $: debugLoadingMode = $page.url.searchParams.get('debugLoading') || '';
    $: debugInsights = $page.url.searchParams.get('debugInsights') === '1';
    $: forceHeroLoading = ['hero', 'all'].includes(debugLoadingMode);
    $: forceMetricLoading = ['metrics', 'all'].includes(debugLoadingMode);
    $: forceSankeyLiveLoading = ['sankey', 'sankey-live', 'sankey-sync', 'all'].includes(debugLoadingMode) && hasRenderableSankeyData;
    $: forceSankeyEmptyLoading = debugLoadingMode === 'sankey-empty';
    $: enrollmentLoadingActive = $syncing.active && $syncing.context === 'enrollment';
    $: sankeySyncActive = $syncing.active && ['enrollment', 'manual-sync', 'simplefin'].includes($syncing.context);
    $: hasRenderableSankeyData =
        sankeyCategoryList.some((cat) => !cat.isDirectFlow && (cat.total || 0) > 0) ||
        sankeySavingsTotal > 0 ||
        sankeyPersonalTransferTotal > 0 ||
        (periodSummary?.cash_deposits || 0) > 0 ||
        (periodSummary?.cash_withdrawals || 0) > 0 ||
        (periodSummary?.investment_inflows || 0) > 0 ||
        (periodSummary?.investment_outflows || 0) > 0;
    $: heroLoading = isRefreshing || enrollmentLoadingActive || forceHeroLoading;
    $: metricLoading = periodLoading || isRefreshing || enrollmentLoadingActive || forceMetricLoading;
    $: sankeyLoading = periodLoading || isRefreshing || sankeySyncActive || forceSankeyLiveLoading || forceSankeyEmptyLoading;
    $: showSankeyLoadingStage = forceSankeyEmptyLoading || (sankeyLoading && !hasRenderableSankeyData);
    $: showLiveSankeySyncState = sankeyLoading && hasRenderableSankeyData && !showSankeyLoadingStage;
    $: showSankeyEmptyState = Boolean(periodSummary) && !sankeyLoading && !hasRenderableSankeyData;
    $: showSankeyChart = Boolean(periodSummary) && hasRenderableSankeyData && !showSankeyLoadingStage && !showLiveSankeySyncState;
    function handleTheaterMouseMove(e) {
        if (!sankeyTheaterEl) return;
        const rect = sankeyTheaterEl.getBoundingClientRect();
        const mx = e.clientX - rect.left;
        const my = e.clientY - rect.top;
        sankeyTheaterEl.style.setProperty('--theater-mx', `${mx}px`);
        sankeyTheaterEl.style.setProperty('--theater-my', `${my}px`);
        sankeyGlowOpacity = 1;
    }

    function handleTheaterMouseLeave() {
        sankeyGlowOpacity = 0;
    }

    // ── Scroll overflow detection for hero account lists ──
    function checkScrollOverflow(node) {
        function update() {
            const wrapper = node.closest('.hero-account-scroll-wrapper');
            if (wrapper) {
                if (node.scrollHeight > node.clientHeight + 2) {
                    wrapper.classList.add('has-overflow');
                } else {
                    wrapper.classList.remove('has-overflow');
                }
            }
        }
        // Check after render and on resize
        requestAnimationFrame(update);
        const observer = new ResizeObserver(update);
        observer.observe(node);
        return {
            update() { requestAnimationFrame(update); },
            destroy() { observer.disconnect(); }
        };
    }

    /* ——— Cyan trend line constants ——— */
    /* âââ Cyan trend line constants âââ */
    /* These are used as defaults; theme-aware overrides applied via reactive statement */
    let CYAN_LINE = '#38BDF8';
    let CYAN_GLOW = '#7DD3FC';
    let CYAN_DEEP = '#0369A1';
    let TEAL_AREA = '#4AEDC4';
    let TEAL_AREA_GLOW = '#2DD4A8';

    /* Theme-aware NW chart colors
       Both modes now use luminous cyan — the light-mode hero card
       is a dark island so the chart needs the same bright treatment */
    $: {
        if ($darkMode) {
            CYAN_LINE = '#38BDF8';
            CYAN_GLOW = '#7DD3FC';
            CYAN_DEEP = '#0369A1';
            TEAL_AREA = '#4AEDC4';
            TEAL_AREA_GLOW = '#2DD4A8';
        } else {
            /* Dark island in light mode — use luminous colors */
            CYAN_LINE = '#38BDF8';
            CYAN_GLOW = '#7DD3FC';
            CYAN_DEEP = '#0369A1';
            TEAL_AREA = '#4AEDC4';
            TEAL_AREA_GLOW = '#2DD4A8';
        }
    }

    // ââ Seed derived state from load() data ââââââââââââââââââââââ
    let bundleSavingsTransferTotal = data.savingsTransferTotal || 0;
    let bundlePersonalTransferTotal = data.personalTransferTotal || 0;
    let bundleCcRepaid = data.ccRepaid || 0;
    let bundleExternalTransfers = data.externalTransfers || 0;
    let bundleIncomingTransfers = data.incomingTransfers || 0;
    let bundleCashDeposits = data.cashDeposits || summary?.cash_deposits || 0;
    let bundleCashWithdrawals = data.cashWithdrawals || summary?.cash_withdrawals || 0;
    let bundleInvestmentInflows = data.investmentInflows || summary?.investment_inflows || 0;
    let bundleInvestmentOutflows = data.investmentOutflows || summary?.investment_outflows || 0;
    let bundleCreditsRefunds = data.creditsRefunds || summary?.credits_refunds || summary?.refunds || 0;
    let monthlyCategoryBreakdown = data.monthlyCategoryBreakdown || [];
    let planSnapshot = data.planSnapshot || null;
    let reviewQueue = data.reviewQueue || null;
    let dataHealth = data.dataHealth || null;
    let scheduled = data.scheduled || null;
    let cashFlowForecast = data.cashFlowForecast || null;
    let investments = data.investments || null;
    let proactiveInsights = [];
    let proactiveLoading = true;
    let proactiveAnalystRefreshing = false;
    let proactiveAnalystMessage = '';
    let proactiveAnalystPollTimer = null;
    let proactiveAnalystPollAttempts = 0;
    let primaryProactiveInsight = null;
    let secondaryProactiveInsights = [];
    const DASHBOARD_MONEY_SIGNALS_ENABLED = false;
    let manualQuickBalances = {};
    let manualQuickSavingId = null;
    let manualQuickMessage = '';
    let reviewToastDismissed = false;
    let monthlyPlanCollapsed = false;
    let expandedUpcomingGroup = '';
    let upcomingActionKey = '';
    let expandedUpcomingItemKey = '';
    let upcomingEditKey = '';
    let upcomingEditDraft = { category: 'Subscriptions', frequency: 'monthly', expectedDay: '', amount: '' };
    const ALL_UPCOMING_GROUPS_KEY = '__all__';
    const upcomingCategoryOptions = ['Subscriptions', 'Insurance', 'Utilities', 'Housing', 'Debt', 'Other'];
    const upcomingFrequencyOptions = [
        { key: 'monthly', label: 'Monthly' },
        { key: 'quarterly', label: 'Quarterly' },
        { key: 'semi_annual', label: 'Semiannual' },
        { key: 'annual', label: 'Annual' },
    ];
    {
        allMonths = [...monthly].sort((a, b) => b.month.localeCompare(a.month)).map(m => m.month);
        if (allMonths.length > 0) selectedCustomMonth = allMonths[0];
        monthlyCategoryBreakdown = data.monthlyCategoryBreakdown || [];

        // Net worth trend: use pre-fetched real balance history from load().
        // No more cumulative monthly-net fallback â that produced a visually
        // incorrect chart shape (the "ghost chart" on initial load).
        netWorthTrendData = (Array.isArray(data.netWorthSeries) && data.netWorthSeries.length >= 2)
            ? data.netWorthSeries.map(d => ({ month: d.month || d.date, value: d.value }))
            : [];

        // Trailing savings rate
        trailingSavingsInfo = computeTrailingSavingsRate(monthly, 3);
    }

    /**
     * Apply a fresh dashboard bundle to all reactive state.
     * Shared by sync-complete events and manual refresh paths.
     */
    async function applyFreshBundle(bundle) {
        isRefreshing = true;
        try {
            // Purge all stale cached data
            invalidateCacheByPrefix('dashboard-bundle');
            invalidateCacheByPrefix('scheduled-transactions');
            invalidateCacheByPrefix('cash-flow-forecast');
            invalidateCacheByPrefix('net-worth');
            invalidateCacheByPrefix('accounts');
            invalidateCacheByPrefix('analytics');
            invalidateCacheByPrefix('transactions');
            invalidateCacheByPrefix('summary');

            summary = bundle.summary;
            accounts = bundle.accounts;
            monthly = bundle.monthly;
            categories = bundle.categories;
            bundleSavingsTransferTotal = bundle.savingsTransferTotal || 0;
            bundlePersonalTransferTotal = bundle.personalTransferTotal || 0;
            bundleCcRepaid = bundle.ccRepaid || 0;
            bundleExternalTransfers = bundle.externalTransfers || 0;
            bundleIncomingTransfers = bundle.incomingTransfers || 0;
            bundleCashDeposits = bundle.cashDeposits || bundle.summary?.cash_deposits || 0;
            bundleCashWithdrawals = bundle.cashWithdrawals || bundle.summary?.cash_withdrawals || 0;
            bundleInvestmentInflows = bundle.investmentInflows || bundle.summary?.investment_inflows || 0;
            bundleInvestmentOutflows = bundle.investmentOutflows || bundle.summary?.investment_outflows || 0;
            bundleCreditsRefunds = bundle.creditsRefunds || bundle.summary?.credits_refunds || bundle.summary?.refunds || 0;
            monthlyCategoryBreakdown = bundle.monthlyCategoryBreakdown || [];
            planSnapshot = bundle.planSnapshot || null;
            reviewQueue = bundle.reviewQueue || null;
        dataHealth = bundle.dataHealth || null;
            scheduled = bundle.scheduled || null;
            cashFlowForecast = bundle.cashFlowForecast || null;
            investments = bundle.investments || null;
            netWorthTrendData = (Array.isArray(bundle.netWorthSeries) && bundle.netWorthSeries.length >= 2)
                ? bundle.netWorthSeries.map(d => ({ month: d.month || d.date, value: d.value }))
                : [];
            netWorthMomDelta = bundle.netWorthMomDelta ?? null;
            netWorthYtdDelta = bundle.netWorthYtdDelta ?? null;
            trailingSavingsInfo = computeTrailingSavingsRate(monthly, 3);
            allMonths = [...monthly].sort((a, b) => b.month.localeCompare(a.month)).map(m => m.month);
            if (allMonths.length > 0 && !allMonths.includes(selectedCustomMonth)) {
                selectedCustomMonth = allMonths[0];
            }

            await computeAccountDeltas();
            bootstrapInitialPeriod();

            // Refresh profile list
            invalidateCacheByPrefix('profiles');
            try {
                const freshProfiles = await api.getProfiles();
                if (Array.isArray(freshProfiles) && freshProfiles.length > 0) {
                    profiles.set(freshProfiles);
                }
            } catch (_) {}

            await tick();

            // Re-trigger count-up animation
            netWorthAnimationDone = false;
            animationDone = false;
            animatedNetWorth = 0;
            animatedIncome = 0;
            animatedExpenses = 0;
            requestAnimationFrame(() => animateNumbers());

            if (!initialLoadComplete) {
                initialLoadComplete = true;
            }
            if (DASHBOARD_MONEY_SIGNALS_ENABLED) {
                await loadProactiveInsights();
            }

            console.info('🎉 Dashboard refreshed with new data.');
        } finally {
            isRefreshing = false;
            setTimeout(() => { _enrollmentInFlight = false; }, 2000);
        }
    }

    onDestroy(() => {
        if (animationFrameId != null) {
            cancelAnimationFrame(animationFrameId);
        }
        clearProactiveAnalystPoll();
    });

    onMount(async () => {
        mounted = true;
        const refreshDashboardBundle = async (reason) => {
            try {
                invalidateCacheByPrefix('dashboard-bundle');
                invalidateCacheByPrefix('scheduled-transactions');
                invalidateCacheByPrefix('cash-flow-forecast');
                const bundle = await api.getDashboardBundle();
                await applyFreshBundle(bundle);
            } catch (e) {
                console.error(`❌ Failed to refresh dashboard after ${reason}:`, e);
            }
        };

        const handleSyncComplete = async (event) => {
            const detail = event?.detail || {};
            if (detail.status && detail.status !== 'completed') return;
            if (!['enrollment', 'manual-sync', 'simplefin'].includes(detail.source)) return;
            await refreshDashboardBundle('sync completion');
        };

        const handleRecurringUpdated = async () => {
            await refreshDashboardBundle('recurring update');
        };

        window.addEventListener('folio:sync-complete', handleSyncComplete);
        window.addEventListener('folio:recurring-updated', handleRecurringUpdated);
        try {
            // ââ Guard: if the load() data is empty (401 race condition),
            //    retry the dashboard bundle before bootstrapping âââââââââ
            if (!summary || !accounts || accounts.length === 0) {
                const bundle = await fetchBundleWithRetry();
                if (bundle) {
                    summary = bundle.summary;
                    accounts = bundle.accounts;
                    monthly = bundle.monthly;
                    categories = bundle.categories;
                    bundleSavingsTransferTotal = bundle.savingsTransferTotal || 0;
                    bundlePersonalTransferTotal = bundle.personalTransferTotal || 0;
                    bundleCcRepaid = bundle.ccRepaid || 0;
                    bundleExternalTransfers = bundle.externalTransfers || 0;
                    bundleIncomingTransfers = bundle.incomingTransfers || 0;
                    bundleCashDeposits = bundle.cashDeposits || bundle.summary?.cash_deposits || 0;
                    bundleCashWithdrawals = bundle.cashWithdrawals || bundle.summary?.cash_withdrawals || 0;
                    bundleInvestmentInflows = bundle.investmentInflows || bundle.summary?.investment_inflows || 0;
                    bundleInvestmentOutflows = bundle.investmentOutflows || bundle.summary?.investment_outflows || 0;
                    bundleCreditsRefunds = bundle.creditsRefunds || bundle.summary?.credits_refunds || bundle.summary?.refunds || 0;
                    monthlyCategoryBreakdown = bundle.monthlyCategoryBreakdown || [];
                    planSnapshot = bundle.planSnapshot || null;
                    reviewQueue = bundle.reviewQueue || null;
                    dataHealth = bundle.dataHealth || null;
                    scheduled = bundle.scheduled || null;
                    cashFlowForecast = bundle.cashFlowForecast || null;
                    netWorthTrendData = (Array.isArray(bundle.netWorthSeries) && bundle.netWorthSeries.length >= 2)
                        ? bundle.netWorthSeries.map(d => ({ month: d.month || d.date, value: d.value }))
                        : [];
                    netWorthMomDelta = bundle.netWorthMomDelta ?? null;
                    netWorthYtdDelta = bundle.netWorthYtdDelta ?? null;
                    trailingSavingsInfo = computeTrailingSavingsRate(monthly, 3);
                    allMonths = [...monthly].sort((a, b) => b.month.localeCompare(a.month)).map(m => m.month);
                    if (allMonths.length > 0) selectedCustomMonth = allMonths[0];
                }
            }

            await computeAccountDeltas();
            bootstrapInitialPeriod();

            requestAnimationFrame(() => animateNumbers());

            await tick();
            if (DASHBOARD_MONEY_SIGNALS_ENABLED) {
                await loadProactiveInsights();
            }
            // Only mark complete if we actually have data to show
            initialLoadComplete = !!(summary && accounts && accounts.length > 0);

            try {
                appConfig = { ...appConfig, ...(await api.getAppConfig()) };
            } catch (_) {
                // Non-fatal; default to live mode behavior.
            }

            // Fetch Teller Connect configuration (non-blocking)
            try {
                const cfg = await api.getTellerConfig();
                appConfig = { ...appConfig, ...cfg };
                tellerAppId = cfg.applicationId || '';
                tellerEnvironment = cfg.environment || 'sandbox';
            } catch (_) {
                // Teller Connect will just be disabled if config isn't available
            }

            // Check if a Teller → SimpleFIN migration is needed (non-blocking)
            try {
                const dismissed = localStorage.getItem('migration_banner_dismissed');
                if (!dismissed) {
                    migrationStatus = await api.getMigrationStatus();
                }
            } catch (_) {
                // Non-fatal — banner just won't show
            }
        } catch (e) {
            console.error('Failed to initialize dashboard:', e);
        }

        return () => {
            window.removeEventListener('folio:sync-complete', handleSyncComplete);
            window.removeEventListener('folio:recurring-updated', handleRecurringUpdated);
        };
    });

    /**
    * Handle successful Teller Connect enrollment request acceptance.
    *
    * The actual UI completion now comes from the global sync-status poller
    * in +layout.svelte, which broadcasts folio:sync-complete only after the
    * backend reports that sync, enrichment, and finalization are done.
    */
    async function handleTellerEnrolled(event) {
        // Guard: Teller SDK can fire onSuccess twice (postMessage + callback)
        if (_enrollmentInFlight) {
            console.warn('⚠️ Enrollment already in progress, ignoring duplicate.');
            return;
        }
        _enrollmentInFlight = true;

        const result = event.detail;
        console.info('🏦 Teller enrollment completed (fast path):', result);
        // Leave syncing active until the backend's sync-status endpoint reports completion.
        // The global layout poller will stop the spinner and broadcast folio:sync-complete.
        setTimeout(() => { _enrollmentInFlight = false; }, 2000);
    }

    function handleSimpleFINConnected() {
        invalidateCacheByPrefix('profiles');
        loadProfiles();
    }

    $: visibleDataHealthWarnings = Array.isArray(dataHealth?.warnings)
        ? dataHealth.warnings.filter((warning) => !(appConfig.demoMode && warning?.type === 'credential_encryption'))
        : [];
    $: dataHealthWarnings = visibleDataHealthWarnings.slice(0, 4);
    $: staleManualAccounts = Array.isArray(accounts)
        ? accounts.filter((account) => account.provider === 'manual' && (account.manual_is_stale || (account.manual_stale_days ?? 0) > 30)).slice(0, 4)
        : [];
    $: dataHealthAttentionCount = visibleDataHealthWarnings.length || staleManualAccounts.length;
    $: upcomingScheduledItems = Array.isArray(scheduled?.items)
        ? scheduled.items.filter((item) => item.next_date)
        : [];
    $: upcomingGroups = Array.isArray(scheduled?.groups) ? scheduled.groups : [];
    $: displayedUpcomingGroups = upcomingGroups.slice(0, 2);
    $: expandedUpcomingItems = expandedUpcomingGroup === ALL_UPCOMING_GROUPS_KEY
        ? upcomingScheduledItems
        : upcomingScheduledItems.filter((item) => item.group === expandedUpcomingGroup);
    $: expandedUpcomingTitle = expandedUpcomingGroup === ALL_UPCOMING_GROUPS_KEY
        ? 'All upcoming'
        : (upcomingGroups.find((group) => group.key === expandedUpcomingGroup)?.label || 'Upcoming');
    $: unscheduledRecurringCount = scheduled?.needs_date_count || 0;
    $: confirmedUpcomingTotal = scheduled?.confirmed_upcoming_total ?? scheduled?.upcoming_total ?? 0;
    $: inferredUpcomingTotal = scheduled?.inferred_upcoming_total || 0;
    $: needsReviewUpcomingTotal = scheduled?.needs_review_total || 0;

    function progressPct(actual, target) {
        const base = Math.max(Number(target || 0), 0);
        if (base <= 0) return 0;
        return Math.max(0, Math.min(100, (Number(actual || 0) / base) * 100));
    }

    function upcomingIcon(item) {
        if (item.status === 'overdue') return 'priority_high';
        if (item.group === 'housing') return 'home';
        if (item.group === 'utilities') return 'bolt';
        if (item.group === 'debt') return 'credit_card';
        if (item.group === 'insurance') return 'shield';
        if (item.group === 'subscriptions') return 'subscriptions';
        return 'event';
    }

    function upcomingGroupIcon(groupKey) {
        if (groupKey === 'housing') return 'home';
        if (groupKey === 'utilities') return 'bolt';
        if (groupKey === 'debt') return 'credit_card';
        if (groupKey === 'insurance') return 'shield';
        if (groupKey === 'subscriptions') return 'subscriptions';
        return 'event';
    }

    function upcomingItemKey(item) {
        return `${item.profile || 'household'}::${item.merchant || ''}::${item.group || ''}`;
    }

    function isUpcomingCandidate(item) {
        return item?.source === 'recurring_candidate' || item?.state === 'candidate' || item?.confirmed === false;
    }

    function isUpcomingEditable(item) {
        return item?.source === 'user' || item?.confirmed === true;
    }

    function upcomingSourceLabel(item) {
        return item?.source_label || item?.evidence?.source_label || (isUpcomingCandidate(item) ? 'Inferred' : 'Detected');
    }

    function upcomingAmountRange(item) {
        const min = Number(item?.evidence?.amount_min ?? item?.amount ?? 0);
        const max = Number(item?.evidence?.amount_max ?? item?.amount ?? 0);
        if (Math.abs(max - min) < 0.01) return formatCurrency(min);
        return `${formatCurrency(min)}-${formatCurrency(max)}`;
    }

    function upcomingRestartDetail(item) {
        const current = Number(item?.evidence?.seen_count || item?.charge_count || 0);
        const historical = Number(item?.evidence?.historical_seen_count || current);
        const gapDays = Number(item?.evidence?.history_gap_days || 0);
        const gapText = gapDays > 0 ? ` · ${Math.round(gapDays / 30)} mo gap` : '';
        return `${current} current · ${historical} historical${gapText}`;
    }

    function upcomingAmountReviewTitle(item) {
        const review = item?.amount_review || {};
        if (review.type === 'same_cycle_adjustment') return 'Plan change or adjustment detected';
        return `Recent charge looks ${review.direction === 'down' ? 'lower' : 'higher'}`;
    }

    function upcomingAmountReviewDetail(item) {
        const review = item?.amount_review || {};
        if (review.type === 'same_cycle_adjustment') {
            return `Cycle total ${formatCurrency(review.cycle_net_amount || review.suggested_amount || 0)} · latest ${formatCurrency(review.latest_amount || 0)} after ${formatCurrency(review.previous_amount || 0)}`;
        }
        return `Expected ${formatCurrency(review.current_amount || item?.amount || 0)} · latest ${formatCurrency(review.suggested_amount || review.latest_amount || 0)}`;
    }

    function normalizeUpcomingCategory(value, fallback = 'Subscriptions') {
        const text = String(value || '').trim();
        const match = upcomingCategoryOptions.find(option => option.toLowerCase() === text.toLowerCase());
        return match || fallback;
    }

    function toggleUpcomingItem(item) {
        const key = upcomingItemKey(item);
        expandedUpcomingItemKey = expandedUpcomingItemKey === key ? '' : key;
        if (expandedUpcomingItemKey !== key) {
            upcomingEditKey = '';
        }
    }

    function startUpcomingEdit(item, amountOverride = null) {
        upcomingEditKey = upcomingItemKey(item);
        upcomingEditDraft = {
            category: normalizeUpcomingCategory(item.category || item.group_label || item.group),
            frequency: item.frequency || 'monthly',
            expectedDay: item.expected_day || (item.next_date ? Number(String(item.next_date).slice(8, 10)) : ''),
            amount: amountOverride ?? item.amount ?? '',
        };
    }

    async function saveUpcomingEdit(item) {
        if (!item?.merchant || upcomingActionKey) return;
        const key = upcomingItemKey(item);
        upcomingActionKey = key;
        try {
            await api.declareSubscription(
                item.merchant,
                Math.abs(Number(upcomingEditDraft.amount || item.amount || 0)),
                upcomingEditDraft.frequency || item.frequency || 'monthly',
                item.profile || null,
                normalizeUpcomingCategory(upcomingEditDraft.category || item.category),
                upcomingEditDraft.expectedDay || null
            );
            invalidateCacheByPrefix('scheduled-transactions');
            invalidateCacheByPrefix('dashboard-bundle');
            await refreshScheduledData();
            upcomingEditKey = '';
        } catch (e) {
            console.error('Failed to update upcoming item:', e);
        } finally {
            upcomingActionKey = '';
        }
    }

    async function deactivateUpcomingItem(item) {
        await dismissUpcomingCandidate(item);
        if (expandedUpcomingItemKey === upcomingItemKey(item)) {
            expandedUpcomingItemKey = '';
        }
    }

    async function refreshScheduledData() {
        scheduled = await api.getScheduledTransactions(45);
    }

    async function confirmUpcomingCandidate(item) {
        if (!item?.merchant || upcomingActionKey) return;
        const key = upcomingItemKey(item);
        upcomingActionKey = key;
        try {
            await api.declareSubscription(
                item.merchant,
                Math.abs(Number(item.amount || 0)),
                item.frequency || 'monthly',
                item.profile || null,
                item.category || item.group_label || 'Subscriptions'
            );
            invalidateCacheByPrefix('scheduled-transactions');
            invalidateCacheByPrefix('dashboard-bundle');
            await refreshScheduledData();
        } catch (e) {
            console.error('Failed to confirm upcoming candidate:', e);
        } finally {
            upcomingActionKey = '';
        }
    }

    async function dismissUpcomingCandidate(item) {
        if (!item?.merchant || upcomingActionKey) return;
        const key = upcomingItemKey(item);
        upcomingActionKey = key;
        try {
            await api.dismissSubscription(item.merchant, item.merchant, item.profile || null);
            invalidateCacheByPrefix('scheduled-transactions');
            invalidateCacheByPrefix('dashboard-bundle');
            await refreshScheduledData();
        } catch (e) {
            console.error('Failed to dismiss upcoming candidate:', e);
        } finally {
            upcomingActionKey = '';
        }
    }

    async function confirmUpcomingAmountReview(item) {
        const review = item?.amount_review;
        if (!item?.merchant || !review || upcomingActionKey) return;
        if (review.type === 'same_cycle_adjustment') {
            startUpcomingEdit(item, review.suggested_amount || item.amount || '');
            return;
        }
        const key = upcomingItemKey(item);
        upcomingActionKey = key;
        try {
            await api.declareSubscription(
                item.merchant,
                Math.abs(Number(review.suggested_amount || item.amount || 0)),
                item.frequency || 'monthly',
                item.profile || null,
                item.category || item.group_label || 'Subscriptions',
                item.expected_day || (item.next_date ? Number(String(item.next_date).slice(8, 10)) : null)
            );
            invalidateCacheByPrefix('scheduled-transactions');
            invalidateCacheByPrefix('dashboard-bundle');
            await refreshScheduledData();
        } catch (e) {
            console.error('Failed to confirm amount review:', e);
        } finally {
            upcomingActionKey = '';
        }
    }

    async function dismissUpcomingAmountReview(item) {
        const review = item?.amount_review;
        if (!item?.merchant || !review || upcomingActionKey) return;
        const key = upcomingItemKey(item);
        upcomingActionKey = key;
        try {
            await api.dismissSubscriptionAmountReview(
                item.merchant,
                Number(review.suggested_amount || 0),
                review.latest_date,
                item.profile || null
            );
            invalidateCacheByPrefix('scheduled-transactions');
            invalidateCacheByPrefix('dashboard-bundle');
            await refreshScheduledData();
        } catch (e) {
            console.error('Failed to dismiss amount review:', e);
        } finally {
            upcomingActionKey = '';
        }
    }

    $: visibleProactiveInsights = Array.isArray(proactiveInsights) ? proactiveInsights.slice(0, 3) : [];
    $: showProactiveSection = DASHBOARD_MONEY_SIGNALS_ENABLED && visibleProactiveInsights.length > 0;
    $: primaryProactiveInsight = visibleProactiveInsights[0] || null;
    $: secondaryProactiveInsights = visibleProactiveInsights.slice(1);

    function insightMeta(insight) {
        const confidence = insight?.confidence ? `${String(insight.confidence).charAt(0).toUpperCase()}${String(insight.confidence).slice(1)} confidence` : '';
        const caveats = Array.isArray(insight?.caveats) ? insight.caveats : (Array.isArray(insight?.evidence?.caveats) ? insight.evidence.caveats : []);
        const caveat = caveats.length > 0 ? caveats[0] : '';
        return [confidence, caveat].filter(Boolean).join(' · ');
    }

    function shouldExpandInsight(insight) {
        const severity = String(insight?.severity || '').toLowerCase();
        const confidence = String(insight?.confidence || '').toLowerCase();
        return severity === 'critical' || (severity === 'warning' && ['high', 'user'].includes(confidence));
    }

    function signalCountLabel(count) {
        return count === 1 ? '1 signal to check' : `${count} signals to check`;
    }

    function insightIcon(insight) {
        const kind = String(insight?.kind || '');
        if (kind === 'cashflow_shortfall') return 'warning';
        if (kind.includes('recurring')) return 'event_repeat';
        if (kind.includes('goal')) return 'flag';
        return 'insights';
    }

    const INSIGHT_EVIDENCE_LABELS = {
        category: 'Category',
        merchant: 'Merchant',
        merchant_key: 'Merchant key',
        counterparty: 'Counterparty',
        month: 'Month',
        period: 'Period',
        period_bucket: 'Period',
        range: 'Period',
        event_type: 'Type',
        current_total: 'Current',
        prior_average: 'Prior average',
        prior_months: 'Prior months',
        prior_sample_months: 'Prior sample',
        delta: 'Change',
        old_amount: 'Previous amount',
        new_amount: 'New amount',
        expected_amount: 'Expected amount',
        next_expected_date: 'Next expected',
        latest_date: 'Latest charge',
        total: 'Total',
        combined_amount: 'Combined amount',
        amount: 'Amount',
        count: 'Count',
        sample_size: 'Sample size',
        transaction_count: 'Transactions',
        row_count: 'Rows'
    };

    const INSIGHT_EVIDENCE_KEYS = [
        'category', 'merchant', 'merchant_key', 'counterparty', 'month', 'period',
        'period_bucket', 'range', 'event_type', 'current_total', 'prior_average',
        'prior_sample_months', 'delta', 'old_amount', 'new_amount', 'expected_amount',
        'next_expected_date', 'latest_date', 'total', 'combined_amount', 'amount',
        'sample_size', 'count', 'transaction_count', 'row_count'
    ];

    function readableInsightSource(source) {
        const labels = {
            'proactive_insights.build_candidate_insights': 'Signal detector',
            'mira.cashflow_forecast.predict_shortfall': 'Cash-flow forecast',
            'data_manager.get_dashboard_bundle_data': 'Dashboard summary',
            'data_manager.get_plan_snapshot_data': 'Plan snapshot',
            'data_manager.get_category_analytics_data': 'Category analytics',
            'data_manager.get_monthly_category_breakdown': 'Category history',
            'data_manager.get_merchant_insights_data': 'Merchant analytics',
            'recurring_obligations.get_recurring_bundle': 'Recurring detector',
            'recurring_obligations.get_scheduled_bundle': 'Scheduled bills'
        };
        return labels[source] || '';
    }

    function humanizeEvidenceType(value) {
        return String(value || '').replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
    }

    function insightEvidenceValue(key, value) {
        if (value === null || value === undefined || value === '') return '';
        if (Array.isArray(value)) return value.map(item => String(item)).join(', ');
        if (typeof value === 'object' && value !== null) return '';
        if (typeof value === 'number') {
            if (/(amount|total|average|delta)/.test(key)) return formatCurrency(value);
            return Number.isInteger(value) ? String(value) : value.toFixed(2);
        }
        if (typeof value === 'string' && /^\d{4}-\d{2}-\d{2}/.test(value)) return formatDate(value);
        if (key === 'event_type') return humanizeEvidenceType(value);
        return String(value);
    }

    function addEvidenceLines(lines, values, limit = 5) {
        if (!values || typeof values !== 'object') return;
        const payload = values.payload && typeof values.payload === 'object' ? values.payload : {};
        const merged = { ...values, ...payload };
        for (const key of INSIGHT_EVIDENCE_KEYS) {
            const formatted = insightEvidenceValue(key, merged[key]);
            if (formatted) {
                const label = INSIGHT_EVIDENCE_LABELS[key] || key.replace(/_/g, ' ');
                const line = `${label}: ${formatted}`;
                if (!lines.includes(line)) lines.push(line);
            }
            if (lines.length >= limit) break;
        }
        if (Array.isArray(values.items) && values.items.length > 0 && lines.length < limit) {
            const names = values.items.map(item => item?.merchant || item?.merchant_key).filter(Boolean);
            if (names.length > 0) lines.push(`Items: ${names.slice(0, 3).join(', ')}`);
        }
    }

    function insightEvidenceLines(insight) {
        const evidence = insight?.evidence && typeof insight.evidence === 'object' ? insight.evidence : {};
        const lines = [];
        const add = (line) => {
            const clean = String(line || '').trim();
            if (clean && !lines.includes(clean)) lines.push(clean);
        };

        const cited = Array.isArray(evidence.cited_evidence) ? evidence.cited_evidence : [];
        if (cited.length > 0) {
            const first = cited[0] || {};
            add(readableInsightSource(first.source));
            if (first.kind) add(`Type: ${humanizeEvidenceType(first.kind)}`);
            addEvidenceLines(lines, first.values, 6);
            if (cited.length > 1) add(`Evidence items: ${cited.length}`);
        } else {
            add(readableInsightSource(evidence.source));
            const contract = evidence.detector_contract || {};
            if (contract.fact) add(`Rule: ${contract.fact}`);
            addEvidenceLines(lines, evidence, 6);
        }

        const caveats = Array.isArray(insight?.caveats) ? insight.caveats : (Array.isArray(evidence?.caveats) ? evidence.caveats : []);
        if (caveats.length > 0) add(`Caveat: ${caveats[0]}`);
        return lines.slice(0, 6);
    }

    function insightDebugDetails(insight) {
        const evidence = insight?.evidence && typeof insight.evidence === 'object' ? insight.evidence : {};
        const contract = evidence.detector_contract || {};
        return {
            kind: insight?.kind,
            insight_type: insight?.insight_type,
            priority: insight?.priority,
            severity: insight?.severity,
            confidence: insight?.confidence,
            generated_at: insight?.generated_at,
            valid_until: insight?.valid_until,
            contract: contract.fact || '',
            evidence_keys: Object.keys(evidence).filter(key => key !== 'detector_contract')
        };
    }

    async function loadProactiveInsights() {
        if (!DASHBOARD_MONEY_SIGNALS_ENABLED) {
            proactiveLoading = false;
            proactiveInsights = [];
            return;
        }
        proactiveLoading = true;
        try {
            const result = await api.getProactiveInsights();
            proactiveInsights = Array.isArray(result?.items) ? result.items : [];
            const status = result?.auto_refresh?.status || result?.job?.status || '';
            if (status === 'queued' || status === 'running') {
                proactiveAnalystRefreshing = true;
                proactiveAnalystPollAttempts = 0;
                proactiveAnalystMessage = backgroundAnalystMessage(null, status);
                scheduleBackgroundAnalystPoll();
            }
        } catch (e) {
            console.error('Failed to load proactive insights:', e);
        } finally {
            proactiveLoading = false;
        }
    }

    function clearProactiveAnalystPoll() {
        if (proactiveAnalystPollTimer != null) {
            clearTimeout(proactiveAnalystPollTimer);
            proactiveAnalystPollTimer = null;
        }
    }

    function backgroundAnalystMessage(run, status = '') {
        const effectiveStatus = run?.status || status;
        if ((run?.stored_count || 0) > 0) return 'New signal added.';
        if (effectiveStatus === 'fresh_cache') return 'Signals were checked recently.';
        if (effectiveStatus === 'llm_unavailable') return 'The local model is not available right now.';
        if (effectiveStatus === 'disabled' || effectiveStatus === 'store_disabled') return 'Background checks are disabled.';
        if (effectiveStatus === 'queued' || effectiveStatus === 'running') return 'Signals are refreshing in the background. You can leave this page.';
        if (effectiveStatus === 'error') return 'Signals could not refresh right now.';
        return 'Nothing new right now.';
    }

    function scheduleBackgroundAnalystPoll() {
        clearProactiveAnalystPoll();
        proactiveAnalystPollTimer = setTimeout(pollBackgroundAnalystStatus, 3000);
    }

    async function pollBackgroundAnalystStatus() {
        proactiveAnalystPollTimer = null;
        proactiveAnalystPollAttempts += 1;
        try {
            const result = await api.getBackgroundProactiveInsightStatus();
            proactiveInsights = Array.isArray(result?.items) ? result.items : [];
            const job = result?.job || {};
            if (job.status === 'queued' || job.status === 'running') {
                proactiveAnalystMessage = backgroundAnalystMessage(null, job.status);
                if (proactiveAnalystPollAttempts < 20) {
                    scheduleBackgroundAnalystPoll();
                } else {
                    proactiveAnalystRefreshing = false;
                    proactiveAnalystMessage = 'Signals are still refreshing in the background.';
                }
                return;
            }
            proactiveAnalystRefreshing = false;
            proactiveAnalystMessage = backgroundAnalystMessage(job.run, job.status);
        } catch (e) {
            console.error('Failed to poll background analyst status:', e);
            proactiveAnalystRefreshing = false;
            proactiveAnalystMessage = 'Signals could not refresh right now.';
        }
    }

    async function refreshBackgroundAnalystInsights() {
        if (proactiveAnalystRefreshing) return;
        clearProactiveAnalystPoll();
        proactiveAnalystRefreshing = true;
        proactiveAnalystPollAttempts = 0;
        proactiveAnalystMessage = '';
        try {
            invalidateCacheByPrefix('proactive-insights');
            const result = await api.refreshBackgroundProactiveInsights();
            proactiveInsights = Array.isArray(result?.items) ? result.items : [];
            const status = result?.status || result?.job?.status || result?.run?.status || '';
            if (status === 'queued' || status === 'running') {
                proactiveAnalystMessage = backgroundAnalystMessage(null, status);
                scheduleBackgroundAnalystPoll();
                return;
            }
            proactiveAnalystRefreshing = false;
            proactiveAnalystMessage = backgroundAnalystMessage(result?.run, status);
        } catch (e) {
            console.error('Failed to refresh background analyst insights:', e);
            proactiveAnalystMessage = 'Signals could not refresh right now.';
            proactiveAnalystRefreshing = false;
        }
    }

    async function dismissProactiveInsight(id) {
        if (!id) return;
        await api.dismissProactiveInsight(id);
        proactiveInsights = proactiveInsights.filter(item => item.id !== id);
    }
    $: investmentSummary = investments?.summary || null;
    $: investmentAllocation = Array.isArray(investments?.allocation) ? investments.allocation.slice(0, 4) : [];

    async function quickUpdateManualAccount(account) {
        const raw = manualQuickBalances[account.id];
        if (raw === undefined || raw === '' || manualQuickSavingId) return;
        manualQuickSavingId = account.id;
        manualQuickMessage = '';
        try {
            await api.updateManualAccount(account.id, {
                name: account.name,
                account_type: account.account_type || (account.is_credit ? 'loan' : 'investment'),
                account_subtype: account.type || 'manual',
                balance: Number(raw),
                notes: account.manual_notes || ''
            }, account.profile || null);
            manualQuickBalances = { ...manualQuickBalances, [account.id]: '' };
            invalidateCache();
            const bundle = await api.getDashboardBundle();
            await applyFreshBundle(bundle);
            manualQuickMessage = `${account.name} updated.`;
        } catch (e) {
            manualQuickMessage = e?.message || 'Could not update manual account.';
        } finally {
            manualQuickSavingId = null;
        }
    }

    function openConnectionChooser() {
        connectionChooserOpen = true;
    }

    function closeConnectionChooser() {
        connectionChooserOpen = false;
    }

    function handleConnectionChooserBackdrop(event) {
        if (event.target === event.currentTarget) {
            closeConnectionChooser();
        }
    }

    function handleConnectionChooserKeydown(event) {
        if (event.key === 'Escape' && connectionChooserOpen) {
            closeConnectionChooser();
        }
    }

    function handleWindowKeydown(event) {
        handleConnectionChooserKeydown(event);
    }

    function launchTellerFromChooser() {
        closeConnectionChooser();
        tellerConnectRef?.show();
    }

    function launchSimpleFINFromChooser() {
        closeConnectionChooser();
        simplefinConnectRef?.show();
    }

    /**
    * Retry wrapper for dashboard-bundle fetch.
    * Handles the race condition where the very first request fires
    * before Vite's env injection is complete (API key = undefined → 401).
    */
    async function fetchBundleWithRetry(retries = 3, delayMs = 300) {
        for (let attempt = 1; attempt <= retries; attempt++) {
            try {
                const bundle = await api.getDashboardBundle();
                // Validate we got real data back, not an empty/error response
                if (bundle && bundle.summary && bundle.accounts) {
                    return bundle;
                }
                throw new Error('Empty bundle response');
            } catch (err) {
                console.warn(`Dashboard bundle attempt ${attempt}/${retries} failed:`, err);
                if (attempt < retries) {
                    await new Promise(resolve => setTimeout(resolve, delayMs * attempt));
                }
            }
        }
        console.error('Could not load dashboard bundle after retries');
        return null;
    }

    /**
     * Seed the Sankey / metric ribbon from already-loaded bundle data.
     * This avoids 3 extra API round-trips on initial page load.
     * Only used for the FIRST render — subsequent period changes still
     * call updatePeriod() which fetches fresh data for the selected month.
     */
    function bootstrapInitialPeriod() {
        // The default period is 'this_month', so compute for current month
        const targetMonth = getMonthForPeriod(selectedPeriod);

        // Use backend-provided transfer totals (expense_type-aware)
        let inc = 0;
        if (selectedPeriod === 'all') {
            inc = summary?.income || 0;
            periodCategories = categories;
            sankeySavingsTotal = bundleSavingsTransferTotal;
            sankeyPersonalTransferTotal = bundlePersonalTransferTotal;
        } else if (selectedPeriod === 'ytd') {
            const year = new Date().getFullYear().toString();
            const ytdMonths = monthly.filter(m => m.month.startsWith(year));
            inc = ytdMonths.reduce((s, m) => s + m.income, 0);
            periodCategories = categories;
            sankeySavingsTotal = bundleSavingsTransferTotal;
            sankeyPersonalTransferTotal = bundlePersonalTransferTotal;
        } else {
            // this_month, last_month, custom â use monthly data from bundle
            const m = monthly.find(m => m.month === targetMonth);
            inc = m ? m.income : 0;
            // The bundle categories are all-time, so do not use them for a
            // single-month Sankey. Keep the flow empty until the exact month
            // refresh below returns.
            periodCategories = [];
            sankeySavingsTotal = 0;
            sankeyPersonalTransferTotal = 0;

            // Kick off the precise month-specific fetch in the background
            // so the Sankey updates within ~200ms without blocking first paint.
            updatePeriodBackground(targetMonth, inc);
        }

        sankeyCategoryList = buildSankeyCategoryList(periodCategories, sankeySavingsTotal, sankeyPersonalTransferTotal);
        if (selectedPeriod === 'this_month' || selectedPeriod === 'last_month' || selectedPeriod === 'custom') {
            const monthData = monthly.find(m => m.month === targetMonth);
            periodCcRepaid = monthData?.cc_repaid || 0;
            periodExternalTransfers = monthData?.external_transfers || 0;
            periodIncomingTransfers = monthData?.incoming_transfers || 0;
            periodCreditsRefunds = monthData?.credits_refunds ?? monthData?.refunds ?? 0;
            periodCashDeposits = monthData?.cash_deposits || 0;
            periodCashWithdrawals = monthData?.cash_withdrawals || 0;
            periodInvestmentInflows = monthData?.investment_inflows || 0;
            periodInvestmentOutflows = monthData?.investment_outflows || 0;
        } else {
            // Use bundle-level totals for all-time/YTD bootstrap; exact YTD is
            // refreshed by updatePeriod() after mount.
            periodCcRepaid = bundleCcRepaid;
            periodExternalTransfers = bundleExternalTransfers;
            periodIncomingTransfers = bundleIncomingTransfers;
            periodCreditsRefunds = bundleCreditsRefunds;
            periodCashDeposits = bundleCashDeposits;
            periodCashWithdrawals = bundleCashWithdrawals;
            periodInvestmentInflows = bundleInvestmentInflows;
            periodInvestmentOutflows = bundleInvestmentOutflows;
        }
        periodSummary = buildPeriodSummary(inc, periodCategories, sankeySavingsTotal, sankeyPersonalTransferTotal, periodCcRepaid, periodExternalTransfers, periodCreditsRefunds, periodIncomingTransfers, periodCashDeposits, periodCashWithdrawals, periodInvestmentInflows, periodInvestmentOutflows);
        periodLoading = false;
    }

    /**
     * Non-blocking background refresh for month-specific Sankey data.
     * Runs after first paint so the user sees content immediately.
     */
    async function updatePeriodBackground(targetMonth, income) {
        try {
            const catResult = await api.getCategoryAnalytics(targetMonth).catch(() => ({
                categories: periodCategories,
                savings_transfer_total: sankeySavingsTotal,
                personal_transfer_total: sankeyPersonalTransferTotal
            }));
            if (targetMonth !== getMonthForPeriod(selectedPeriod)) return;
            if (Array.isArray(catResult)) {
                periodCategories = catResult;
            } else {
                periodCategories = catResult.categories || periodCategories;
                sankeySavingsTotal = catResult.savings_transfer_total || 0;
                sankeyPersonalTransferTotal = catResult.personal_transfer_total || 0;
            }
            // Fetch month-specific CC repaid and external transfers from monthly data
            const monthData = monthly.find(m => m.month === targetMonth);
            periodCcRepaid = monthData?.cc_repaid || 0;
            periodExternalTransfers = monthData?.external_transfers || 0;
            periodIncomingTransfers = monthData?.incoming_transfers || 0;
            periodCreditsRefunds = monthData?.credits_refunds ?? monthData?.refunds ?? 0;
            periodCashDeposits = monthData?.cash_deposits || 0;
            periodCashWithdrawals = monthData?.cash_withdrawals || 0;
            periodInvestmentInflows = monthData?.investment_inflows || 0;
            periodInvestmentOutflows = monthData?.investment_outflows || 0;
            sankeyCategoryList = buildSankeyCategoryList(periodCategories, sankeySavingsTotal, sankeyPersonalTransferTotal);
            periodSummary = buildPeriodSummary(income, periodCategories, sankeySavingsTotal, sankeyPersonalTransferTotal, periodCcRepaid, periodExternalTransfers, periodCreditsRefunds, periodIncomingTransfers, periodCashDeposits, periodCashWithdrawals, periodInvestmentInflows, periodInvestmentOutflows);
        } catch (_) {}
    }

    async function computeAccountDeltas() {
        try {
            const sorted = [...monthly].sort((a, b) => b.month.localeCompare(a.month));
            if (sorted.length < 2) return;
            const prevNet = sorted[1]?.net || 0;
            const curNet = sorted[0]?.net || 0;
            accountDeltas = { _portfolioDelta: curNet - prevNet };
        } catch (_) {}
    }

    let netWorthAnimationDone = false;

    function animateNumbers() {
        // Cancel any in-flight animation before starting a new one
        if (animationFrameId != null) {
            cancelAnimationFrame(animationFrameId);
            animationFrameId = null;
        }

        animationStarted = true;
        const duration = 900;
        const staggerDelay = 150; // ms delay before metric ribbon starts
        const targetInc = periodSummary?.income || 0;
        const targetExp = periodSummary?.expenses || 0;
        const overshoot = 1.06;

        // Net Worth only animates on first page load
        const shouldAnimateNW = !netWorthAnimationDone;
        const targetNW = netWorth;

        // If NW already animated, just snap it
        if (!shouldAnimateNW) {
            animatedNetWorth = targetNW;
        }

        let startTime = -1;

        function springEase(t) {
            if (t < 0.7) {
                const p = t / 0.7;
                return p * p * (3 - 2 * p) * overshoot;
            } else {
                const p = (t - 0.7) / 0.3;
                const ease = p * p * (3 - 2 * p);
                return overshoot + (1.0 - overshoot) * ease;
            }
        }

        function step(rafTimestamp) {
            // Use the RAF-provided timestamp for frame-perfect timing
            if (startTime < 0) startTime = rafTimestamp;
            const elapsed = rafTimestamp - startTime;

            // ── Net Worth: starts immediately ──
            const nwProgress = Math.min(elapsed / duration, 1);
            const nwEased = springEase(nwProgress);

            if (shouldAnimateNW) {
                animatedNetWorth = targetNW * nwEased;
            }

            // ── Income & Expenses: staggered start ──
            const ribbonElapsed = Math.max(elapsed - staggerDelay, 0);
            const ribbonProgress = Math.min(ribbonElapsed / duration, 1);
            const ribbonEased = springEase(ribbonProgress);

            animatedIncome = targetInc * ribbonEased;
            animatedExpenses = targetExp * ribbonEased;

            // Continue until both tracks are done
            if (nwProgress < 1 || ribbonProgress < 1) {
                animationFrameId = requestAnimationFrame(step);
            } else {
                animationFrameId = null;
                if (shouldAnimateNW) {
                    animatedNetWorth = targetNW;
                    netWorthAnimationDone = true;
                }
                animatedIncome = targetInc;
                animatedExpenses = targetExp;
                animationDone = true;
            }
        }

        animationFrameId = requestAnimationFrame(step);
    }

    function getMonthForPeriod(period) {
        const sorted = [...monthly].sort((a, b) => b.month.localeCompare(a.month));
        switch (period) {
            case 'this_month': return getCurrentMonth();
            case 'last_month': {
                // Always compute last month from calendar, not from data array
                const now = new Date();
                const lastMonth = new Date(now.getFullYear(), now.getMonth() - 1, 1);
                const y = lastMonth.getFullYear();
                const m = String(lastMonth.getMonth() + 1).padStart(2, '0');
                return `${y}-${m}`;
            }
            case 'custom': return selectedCustomMonth;
            default: return null;
        }
    }

    function buildSankeyCategoryList(cats, savingsTotal, personalTransferTotal) {
        const expenseCats = cats.filter(c => !DIRECT_FLOW_CATEGORIES.includes(c.category));
        const expenseTotal = expenseCats.reduce((s, c) => s + (c.gross ?? c.total ?? 0), 0);

        const combined = expenseCats.map(c => ({
            ...c,
            netTotal: c.total || 0,
            total: c.gross ?? c.total ?? 0,
            percent: expenseTotal > 0 ? ((c.gross ?? c.total ?? 0) / expenseTotal) * 100 : 0
        }));

        if (savingsTotal > 0) {
            combined.push({ category: 'Savings Transfer', total: savingsTotal, percent: 0, isDirectFlow: true });
        }
        if (personalTransferTotal > 0) {
            combined.push({
                category: 'Personal Transfer',
                total: personalTransferTotal,
                percent: 0,
                isDirectFlow: true
            });
        }

        combined.sort((a, b) => b.total - a.total);
        return combined;
    }

    $: sankeyPillList = (() => {
        const flows = [...sankeyCategoryList];
        const directFlows = [
            { category: 'Cash Deposit', total: periodSummary?.cash_deposits || 0, isDirectFlow: true },
            { category: 'Cash Withdrawal', total: periodSummary?.cash_withdrawals || 0, isDirectFlow: true },
            {
                category: 'Investment Transfer',
                total: (periodSummary?.investment_inflows || 0) + (periodSummary?.investment_outflows || 0),
                isDirectFlow: true
            },
        ].filter((item) => item.total > 0);
        const creditsTotal = periodSummary?.credits_refunds || 0;
        flows.unshift(...directFlows);
        if (creditsTotal > 0) {
            flows.unshift({
                category: CREDIT_DRILLDOWN_CATEGORY,
                total: creditsTotal,
                percent: 0,
                isCreditFlow: true
            });
        }
        return flows;
    })();

    function getSankeyFlowColor(category) {
        if (category === CREDIT_DRILLDOWN_CATEGORY) return CREDIT_DRILLDOWN_COLOR;
        return CATEGORY_COLORS[category] || '#627d98';
    }

    function getSankeyFlowIcon(category) {
        if (category === CREDIT_DRILLDOWN_CATEGORY) return 'payments';
        return CATEGORY_ICONS[category] || 'label';
    }

    function buildPeriodSummary(
        income,
        cats,
        savingsTotal,
        personalTransferTotal,
        ccRepaid = 0,
        externalTransfers = 0,
        creditsRefunds = 0,
        incomingTransfers = 0,
        cashDeposits = 0,
        cashWithdrawals = 0,
        investmentInflows = 0,
        investmentOutflows = 0
    ) {
        const expenseCats = (cats || []).filter(c => !DIRECT_FLOW_CATEGORIES.includes(c.category));
        const expenses = expenseCats.reduce((s, c) => s + (c.gross ?? c.total ?? 0), 0);
        // Cash-flow Net Flow: Income + credits + incoming direct flows - Spending - outgoing direct flows
        // CC payments and internal transfers are excluded
        const netFlow = income
            + creditsRefunds
            + incomingTransfers
            + cashDeposits
            + investmentInflows
            - expenses
            - externalTransfers
            - cashWithdrawals
            - investmentOutflows;
        const savings = Math.max(income - expenses, 0);
        return {
            income, expenses, savings,
            savingsTransfer: savingsTotal,
            personalTransfer: personalTransferTotal,
            cc_repaid: ccRepaid,
            external_transfers: externalTransfers,
            incoming_transfers: incomingTransfers,
            cash_deposits: cashDeposits,
            cash_withdrawals: cashWithdrawals,
            investment_inflows: investmentInflows,
            investment_outflows: investmentOutflows,
            credits_refunds: creditsRefunds,
            net_flow: netFlow,
            savings_rate: sanitizeSavingsRate(income, expenses, savings)
        };
    }

    async function updatePeriod() {
        periodLoading = true;
        selectedSankeyCategory = null;
        sankeyDrillTxns = [];
        
        // Keep stale data visible while fetching (no more null flash)
        // sankeyCategoryList, periodCategories, periodSummary retain
        // their previous values until fresh data arrives below.

        if (selectedPeriod === 'all') {
            const inc = summary?.income || 0;
            try {
                const catResult = await api.getCategoryAnalytics().catch(() => ({
                    categories: categories,
                    savings_transfer_total: 0,
                    personal_transfer_total: 0
                }));
                if (Array.isArray(catResult)) {
                    periodCategories = catResult;
                    sankeySavingsTotal = 0;
                    sankeyPersonalTransferTotal = 0;
                } else {
                    periodCategories = catResult.categories || categories;
                    sankeySavingsTotal = catResult.savings_transfer_total || 0;
                    sankeyPersonalTransferTotal = catResult.personal_transfer_total || 0;
                }
            } catch (e) { periodCategories = categories; sankeySavingsTotal = 0; sankeyPersonalTransferTotal = 0; }
            periodCcRepaid = summary?.cc_repaid || bundleCcRepaid;
            periodExternalTransfers = summary?.external_transfers || bundleExternalTransfers;
            periodIncomingTransfers = summary?.incoming_transfers || bundleIncomingTransfers;
            periodCreditsRefunds = summary?.credits_refunds ?? summary?.refunds ?? bundleCreditsRefunds;
            periodCashDeposits = summary?.cash_deposits || bundleCashDeposits;
            periodCashWithdrawals = summary?.cash_withdrawals || bundleCashWithdrawals;
            periodInvestmentInflows = summary?.investment_inflows || bundleInvestmentInflows;
            periodInvestmentOutflows = summary?.investment_outflows || bundleInvestmentOutflows;
            sankeyCategoryList = buildSankeyCategoryList(periodCategories, sankeySavingsTotal, sankeyPersonalTransferTotal);
            periodSummary = buildPeriodSummary(inc, periodCategories, sankeySavingsTotal, sankeyPersonalTransferTotal, periodCcRepaid, periodExternalTransfers, periodCreditsRefunds, periodIncomingTransfers, periodCashDeposits, periodCashWithdrawals, periodInvestmentInflows, periodInvestmentOutflows);
        } else if (selectedPeriod === 'ytd') {
            const year = new Date().getFullYear().toString();
            const ytdMonths = monthly.filter(m => m.month.startsWith(year));
            const inc = ytdMonths.reduce((s, m) => s + m.income, 0);
            const ytdMonthKeys = ytdMonths.map(m => m.month);
            try {
                const aggResult = await fetchAggregatedCategories(ytdMonthKeys).catch(() => ({
                    categories: categories,
                    savings_transfer_total: 0,
                    personal_transfer_total: 0
                }));
                if (Array.isArray(aggResult)) {
                    periodCategories = aggResult;
                    sankeySavingsTotal = 0;
                    sankeyPersonalTransferTotal = 0;
                } else {
                    periodCategories = aggResult.categories || categories;
                    sankeySavingsTotal = aggResult.savings_transfer_total || 0;
                    sankeyPersonalTransferTotal = aggResult.personal_transfer_total || 0;
                }
            } catch (e) { periodCategories = categories; sankeySavingsTotal = 0; sankeyPersonalTransferTotal = 0; }
            periodCcRepaid = ytdMonths.reduce((s, m) => s + (m.cc_repaid || 0), 0);
            periodExternalTransfers = ytdMonths.reduce((s, m) => s + (m.external_transfers || 0), 0);
            periodIncomingTransfers = ytdMonths.reduce((s, m) => s + (m.incoming_transfers || 0), 0);
            periodCreditsRefunds = ytdMonths.reduce((s, m) => s + (m.credits_refunds ?? m.refunds ?? 0), 0);
            periodCashDeposits = ytdMonths.reduce((s, m) => s + (m.cash_deposits || 0), 0);
            periodCashWithdrawals = ytdMonths.reduce((s, m) => s + (m.cash_withdrawals || 0), 0);
            periodInvestmentInflows = ytdMonths.reduce((s, m) => s + (m.investment_inflows || 0), 0);
            periodInvestmentOutflows = ytdMonths.reduce((s, m) => s + (m.investment_outflows || 0), 0);
            sankeyCategoryList = buildSankeyCategoryList(periodCategories, sankeySavingsTotal, sankeyPersonalTransferTotal);
            periodSummary = buildPeriodSummary(inc, periodCategories, sankeySavingsTotal, sankeyPersonalTransferTotal, periodCcRepaid, periodExternalTransfers, periodCreditsRefunds, periodIncomingTransfers, periodCashDeposits, periodCashWithdrawals, periodInvestmentInflows, periodInvestmentOutflows);
        } else {
            const targetMonth = getMonthForPeriod(selectedPeriod);
            if (targetMonth) {
                const m = monthly.find(m => m.month === targetMonth);
                    const inc = m ? m.income : 0;
                    try {
                        const catResult = await api.getCategoryAnalytics(targetMonth).catch(() => ({
                            categories: [],
                            savings_transfer_total: 0,
                            personal_transfer_total: 0
                        }));
                        if (Array.isArray(catResult)) {
                            periodCategories = catResult;
                            sankeySavingsTotal = 0;
                            sankeyPersonalTransferTotal = 0;
                        } else {
                            periodCategories = catResult.categories || [];
                            sankeySavingsTotal = catResult.savings_transfer_total || 0;
                            sankeyPersonalTransferTotal = catResult.personal_transfer_total || 0;
                        }
                    } catch (e) { periodCategories = []; sankeySavingsTotal = 0; sankeyPersonalTransferTotal = 0; }
                    const monthData = monthly.find(m => m.month === targetMonth);
                    periodCcRepaid = monthData?.cc_repaid || 0;
                    periodExternalTransfers = monthData?.external_transfers || 0;
                    periodIncomingTransfers = monthData?.incoming_transfers || 0;
                    periodCreditsRefunds = monthData?.credits_refunds ?? monthData?.refunds ?? 0;
                    periodCashDeposits = monthData?.cash_deposits || 0;
                    periodCashWithdrawals = monthData?.cash_withdrawals || 0;
                    periodInvestmentInflows = monthData?.investment_inflows || 0;
                    periodInvestmentOutflows = monthData?.investment_outflows || 0;
                    sankeyCategoryList = buildSankeyCategoryList(periodCategories, sankeySavingsTotal, sankeyPersonalTransferTotal);
                    periodSummary = buildPeriodSummary(inc, periodCategories, sankeySavingsTotal, sankeyPersonalTransferTotal, periodCcRepaid, periodExternalTransfers, periodCreditsRefunds, periodIncomingTransfers, periodCashDeposits, periodCashWithdrawals, periodInvestmentInflows, periodInvestmentOutflows);
            } else {
                periodSummary = { income: 0, expenses: 0, savings: 0, savingsTransfer: 0, personalTransfer: 0, cc_repaid: 0, external_transfers: 0, incoming_transfers: 0, cash_deposits: 0, cash_withdrawals: 0, investment_inflows: 0, investment_outflows: 0, credits_refunds: 0, net_flow: 0, savings_rate: 0 };
                periodCategories = [];
                sankeyCategoryList = [];
                sankeySavingsTotal = 0;
                sankeyPersonalTransferTotal = 0;
            }
        }

        periodLoading = false;

        if (!loading && typeof window !== 'undefined') {
            animationDone = false;
            animatedIncome = 0;
            animatedExpenses = 0;
            requestAnimationFrame(() => animateNumbers());
        }
    }

    async function fetchAggregatedCategories(monthList) {
        if (!monthList || monthList.length === 0) return {
            categories: [],
            savings_transfer_total: 0,
            personal_transfer_total: 0
        };
        const results = await Promise.all(monthList.map(m => api.getCategoryAnalytics(m)));
        const merged = {};
        let savTotal = 0;
        let ptTotal = 0;
        for (const res of results) {
            const cats = Array.isArray(res) ? res : (res.categories || []);
            savTotal += (res.savings_transfer_total || 0);
            ptTotal += (res.personal_transfer_total || 0);
            for (const cat of (cats || [])) {
                merged[cat.category] = (merged[cat.category] || 0) + (cat.total || 0);
            }
        }
        const entries = Object.entries(merged).map(([category, total]) => ({ category, total }));
        const grandTotal = entries.reduce((s, e) => s + e.total, 0) || 1;
        return {
            categories: entries.map(e => ({ ...e, percent: (e.total / grandTotal) * 100 })).sort((a, b) => b.total - a.total),
            savings_transfer_total: savTotal,
            personal_transfer_total: ptTotal
        };
    }

    function sanitizeSavingsRate(income, expenses, savings) {
        if (!income || income <= 0) return 0;
        if (expenses > income) return 0;
        const rate = (savings / income) * 100;
        if (rate < 0 || rate > 100 || !isFinite(rate)) return 0;
        return rate;
    }

    // Only react to period changes AFTER initial mount setup is complete.
    // onMount handles the first call — this reactive statement handles subsequent
    // period selector clicks only.
    let initialLoadComplete = false;

    $: if (initialLoadComplete && !profileSwitching && selectedPeriod && monthly.length > 0) {
        void privacyKey; // re-evaluate when privacy toggles
        selectedPeriodStore.set(selectedPeriod);
        if (selectedCustomMonth) selectedCustomMonthStore.set(selectedCustomMonth);
        updatePeriod();
    }


    // ââ Profile switch: full reload of dashboard data ââ
    let _prevProfile = null;
    $: if (initialLoadComplete && $activeProfile && $activeProfile !== _prevProfile) {
        if (_prevProfile !== null) {
            // Profile actually changed (not initial mount)
            reloadDashboardForProfile();
        }
        _prevProfile = $activeProfile;
    }

    async function reloadDashboardForProfile() {
        profileSwitching = true;
        try {
            const bundle = await api.getDashboardBundle();
            summary = bundle.summary;
            accounts = bundle.accounts;
            monthly = bundle.monthly;
            categories = bundle.categories;
            bundleSavingsTransferTotal = bundle.savingsTransferTotal || 0;
            bundlePersonalTransferTotal = bundle.personalTransferTotal || 0;
            bundleCcRepaid = bundle.ccRepaid || 0;
            bundleExternalTransfers = bundle.externalTransfers || 0;
            bundleIncomingTransfers = bundle.incomingTransfers || 0;
            bundleCashDeposits = bundle.cashDeposits || bundle.summary?.cash_deposits || 0;
            bundleCashWithdrawals = bundle.cashWithdrawals || bundle.summary?.cash_withdrawals || 0;
            bundleInvestmentInflows = bundle.investmentInflows || bundle.summary?.investment_inflows || 0;
            bundleInvestmentOutflows = bundle.investmentOutflows || bundle.summary?.investment_outflows || 0;
            bundleCreditsRefunds = bundle.creditsRefunds || bundle.summary?.credits_refunds || bundle.summary?.refunds || 0;
            monthlyCategoryBreakdown = bundle.monthlyCategoryBreakdown || [];
            planSnapshot = bundle.planSnapshot || null;
            reviewQueue = bundle.reviewQueue || null;
            dataHealth = bundle.dataHealth || null;
            scheduled = bundle.scheduled || null;
            cashFlowForecast = bundle.cashFlowForecast || null;
            netWorthTrendData = (Array.isArray(bundle.netWorthSeries) && bundle.netWorthSeries.length >= 2)
                ? bundle.netWorthSeries.map(d => ({ month: d.month || d.date, value: d.value }))
                : [];
            netWorthMomDelta = bundle.netWorthMomDelta ?? null;
            netWorthYtdDelta = bundle.netWorthYtdDelta ?? null;
            trailingSavingsInfo = computeTrailingSavingsRate(monthly, 3);
            allMonths = [...monthly].sort((a, b) => b.month.localeCompare(a.month)).map(m => m.month);
            if (allMonths.length > 0 && !allMonths.includes(selectedCustomMonth)) {
                selectedCustomMonth = allMonths[0];
            }
            await computeAccountDeltas();
            bootstrapInitialPeriod();
            netWorthAnimationDone = false;
            animationDone = false;
            animatedNetWorth = 0;
            animatedIncome = 0;
            animatedExpenses = 0;
            requestAnimationFrame(() => animateNumbers());
        } catch (e) {
            console.error('Failed to reload dashboard for profile:', e);
        } finally {
            profileSwitching = false;
        }
    }

    function handleCustomMonthChange() {
        selectedPeriod = 'custom';
        updatePeriod();
    }

    // Deltas
    // ── Unified derived state: accounts + deltas (single reactive block) ──
    // ── Consolidated dashboard metrics: single reactive block ──
    // All derived values computed in one pass so Svelte batches into a
    // single synchronous update instead of cascading through 6+ reactive
    // statements sequentially.
    $: dashboardMetrics = (() => {
        // Privacy reactivity anchor â forces re-computation when privacy toggles
        void privacyKey;
        // ââ Account totals ââ
        const _cashAccounts = accounts.filter(a => !a.is_credit);
        const _creditAccounts = accounts.filter(a => a.is_credit);
        const _manualAssetAccounts = _cashAccounts.filter(a => a.provider === 'manual');
        const _manualLiabilityAccounts = _creditAccounts.filter(a => a.provider === 'manual');
        // Separate investment accounts into assets (already in _cashAccounts via is_credit=false)
        // Note: loan accounts have is_credit=true and will appear in _creditAccounts

        const _totalCash = _cashAccounts.reduce((s, a) => s + parseFloat(a.balance || 0), 0);
        const _totalOwed = _creditAccounts.reduce((s, a) => s + Math.abs(parseFloat(a.balance || 0)), 0);
        const _netWorth = _totalCash - _totalOwed;

        // ── Month-over-month deltas ──
        const _currentMonthData = monthly.find(m => m.month === getCurrentMonth()) || null;
        const sorted = [...monthly].sort((a, b) => b.month.localeCompare(a.month));
        const _prevMonthData = sorted.length > 1 ? sorted[1] : null;

        const _incomeDelta = _currentMonthData && _prevMonthData ? computeDelta(_currentMonthData.income, _prevMonthData.income) : null;
        const _expenseDelta = _currentMonthData && _prevMonthData ? computeDelta(_currentMonthData.expenses, _prevMonthData.expenses) : null;
        const _netWorthDelta = netWorthMomDelta;

        // ── Savings rate (trailing) ──
        const _savingsRate = trailingSavingsInfo.rate;
        const _savingsRateDelta = trailingSavingsInfo.delta;

        // ── Daily spending pace ──
        let _dailyPace = null;
        if (periodSummary && selectedPeriod === 'this_month') {
            const now = new Date();
            const dayOfMonth = now.getDate();
            if (dayOfMonth > 0) {
                const dailyAvg = periodSummary.expenses / dayOfMonth;
                const daysInMonth = new Date(now.getFullYear(), now.getMonth() + 1, 0).getDate();
                const projected = dailyAvg * daysInMonth;
                _dailyPace = { dailyAvg, projected, daysInMonth, dayOfMonth };
            }
        }

        // ── Top category insight ──
        let _topCatInsight = null;
        {
            const expenseCats = periodCategories.filter(c => !DIRECT_FLOW_CATEGORIES.includes(c.category));
            if (expenseCats.length > 0 && periodSummary) {
                const top = expenseCats[0];
                _topCatInsight = {
                    name: top.category,
                    total: top.total,
                    pctOfExpenses: periodSummary.expenses > 0 ? (top.total / periodSummary.expenses * 100) : 0
                };
            }
        }

        return {
            totalCash: _totalCash,
            totalOwed: _totalOwed,
            netWorth: _netWorth,
            cashAccounts: _cashAccounts,
            creditAccounts: _creditAccounts,
            manualAssetAccounts: _manualAssetAccounts,
            manualLiabilityAccounts: _manualLiabilityAccounts,
            currentMonthData: _currentMonthData,
            prevMonthData: _prevMonthData,
            incomeDelta: _incomeDelta,
            expenseDelta: _expenseDelta,
            netWorthDelta: _netWorthDelta,
            savingsRate: _savingsRate,
            savingsRateDelta: _savingsRateDelta,
            dailyPace: _dailyPace,
            topCatInsight: _topCatInsight
        };
    })();

    // ── Convenience aliases (template-compatible, no extra reactive passes) ──
    $: totalCash = dashboardMetrics.totalCash;
    $: totalOwed = dashboardMetrics.totalOwed;
    $: netWorth = dashboardMetrics.netWorth;
    $: cashAccounts = dashboardMetrics.cashAccounts;
    $: creditAccounts = dashboardMetrics.creditAccounts;
    $: manualAssetAccounts = dashboardMetrics.manualAssetAccounts;
    $: manualLiabilityAccounts = dashboardMetrics.manualLiabilityAccounts;
    $: currentMonthData = dashboardMetrics.currentMonthData;
    $: prevMonthData = dashboardMetrics.prevMonthData;
    $: incomeDelta = dashboardMetrics.incomeDelta;
    $: expenseDelta = dashboardMetrics.expenseDelta;
    $: netWorthDelta = dashboardMetrics.netWorthDelta;
    $: savingsRate = dashboardMetrics.savingsRate;
    $: savingsRateDelta = dashboardMetrics.savingsRateDelta;
    $: dailyPace = dashboardMetrics.dailyPace;
    $: topCatInsight = dashboardMetrics.topCatInsight;

    // ââ Privacy Mode reactivity key ââ
    // When privacyMode toggles, this forces Svelte to re-evaluate all
    // template expressions that depend on formatCurrency / formatCompact.
    // We use it as a hidden dependency in key reactive expressions.
    $: privacyKey = $privacyMode;

    function parseDashboardDate(raw) {
        if (!raw) return null;
        const value = String(raw).trim();
        const match = value.match(/^(\d{4})-(\d{2})(?:-(\d{2}))?/);
        if (match) {
            return new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3] || 1));
        }
        const dt = new Date(value);
        return isNaN(dt.getTime()) ? null : dt;
    }

    // ââ Net Worth chart hover ââ
    function handleChartHover(event) {
        if (!netWorthTrendData.length || !nwChart.linePath) return;

        const svg = event.currentTarget.closest('svg') || event.currentTarget;
        const rect = svg.getBoundingClientRect();
        const mouseX = ((event.clientX - rect.left) / rect.width) * 600;

        const stepX = 600 / (netWorthTrendData.length - 1);
        const index = Math.round(mouseX / stepX);
        const clampedIndex = Math.max(0, Math.min(index, netWorthTrendData.length - 1));

        const d = netWorthTrendData[clampedIndex];
        const padY = 8;
        const padBottom = 28;
        const chartH = 160 - padY - padBottom;
        const values = netWorthTrendData.map(p => p.value);
        const rawMin = Math.min(...values);
        const rawMax = Math.max(...values);
        const visibleRange = rawMax - rawMin || 1;
        const min = rawMin - visibleRange * 0.15;
        const max = rawMax + visibleRange * 0.08;
        const range = max - min || 1;

        const x = clampedIndex * stepX;
        const y = padY + chartH - ((d.value - min) / range) * chartH;

        const raw = d.month || d.date;
        const dt = parseDashboardDate(raw);
        if (!dt) return;
        const dateLabel = dt.toLocaleDateString('en-US', {
            month: 'short', day: 'numeric', year: 'numeric'
        });

        hoverPoint = { x, y, date: dateLabel, value: d.value };
    }

    function handleChartLeave() {
        hoverPoint = null;
    }

    // Net Worth Area Chart — smooth Catmull-Rom spline with quarterly labels
    function buildNWAreaChart(data, width = 600, height = 160) {
        if (!Array.isArray(data) || data.length < 2) return {
            linePath: '', areaPath: '', endPoint: null,
            labels: [], gridLines: [], monthLabels: []
        };

        const values = data.map(d => d.value);
        const rawMin = Math.min(...values);
        const rawMax = Math.max(...values);
        const visibleRange = rawMax - rawMin || 1;
        const min = rawMin - visibleRange * 0.15;
        const max = rawMax + visibleRange * 0.08;
        const range = max - min || 1;
        const padY = 8;
        const padBottom = 28;
        const chartH = height - padY - padBottom;
        const stepX = width / (values.length - 1);

        const rawAnchors = values.map((v, i) => ({
            x: i * stepX,
            y: padY + chartH - ((v - min) / range) * chartH
        }));

        function catmullRomChain(pts, tension, samplesPerSegment) {
            const alpha = tension;
            const result = [];
            for (let i = 0; i < pts.length - 1; i++) {
                const p0 = pts[Math.max(i - 1, 0)];
                const p1 = pts[i];
                const p2 = pts[i + 1];
                const p3 = pts[Math.min(i + 2, pts.length - 1)];
                const steps = (i === pts.length - 2) ? samplesPerSegment + 1 : samplesPerSegment;
                for (let s = 0; s < steps; s++) {
                    const tt = s / samplesPerSegment;
                    const t2 = tt * tt;
                    const t3 = t2 * tt;
                    const m1x = alpha * (p2.x - p0.x);
                    const m1y = alpha * (p2.y - p0.y);
                    const m2x = alpha * (p3.x - p1.x);
                    const m2y = alpha * (p3.y - p1.y);
                    const ax = (2*t3 - 3*t2 + 1)*p1.x + (t3 - 2*t2 + tt)*m1x + (-2*t3 + 3*t2)*p2.x + (t3 - t2)*m2x;
                    const ay = (2*t3 - 3*t2 + 1)*p1.y + (t3 - 2*t2 + tt)*m1y + (-2*t3 + 3*t2)*p2.y + (t3 - t2)*m2y;
                    result.push({ x: ax, y: ay });
                }
            }
            return result;
        }

        const tension = 0.5;
        const samplesPerSeg = 16;
        const curvePts = rawAnchors.length >= 2 ? catmullRomChain(rawAnchors, tension, samplesPerSeg) : rawAnchors;

        let linePath = '';
        if (curvePts.length > 0) {
            linePath = `M${curvePts[0].x.toFixed(1)},${curvePts[0].y.toFixed(1)}`;
            for (let i = 1; i < curvePts.length; i++) {
                linePath += ` L${curvePts[i].x.toFixed(1)},${curvePts[i].y.toFixed(1)}`;
            }
        }

        const lastPt = curvePts[curvePts.length - 1];
        const firstPt = curvePts[0];
        const areaPath = linePath + ` L${lastPt.x.toFixed(1)},${height - padBottom}` + ` L${firstPt.x.toFixed(1)},${height - padBottom} Z`;
        const endPoint = curvePts[curvePts.length - 1];

        const labels = data.map((d, i) => ({ x: i * stepX, text: formatMonthShort(d.month || d.date) }));

        const monthLabels = [];
        const labelledQuarters = new Set();
        for (let i = 0; i < data.length; i++) {
            const raw = data[i].month || data[i].date;
            if (!raw) continue;
            const dt = parseDashboardDate(raw);
            if (!dt) continue;
            const mo = dt.getMonth();
            const yr = dt.getFullYear();
            const quarter = `${yr}-Q${Math.floor(mo / 3)}`;
            if ([0, 3, 6, 9].includes(mo) && !labelledQuarters.has(quarter)) {
                labelledQuarters.add(quarter);
                const shortMonth = dt.toLocaleString('default', { month: 'short' });
                const shortYear = String(yr).slice(-2);
                monthLabels.push({ x: i * stepX, y: height - 6, label: `${shortMonth} '${shortYear}` });
            }
        }

        if (monthLabels.length === 0 && data.length >= 1) {
            const firstRaw = data[0].month || data[0].date;
            const firstDt = parseDashboardDate(firstRaw);
            if (firstDt) {
                monthLabels.push({ x: 0, y: height - 6, label: `${firstDt.toLocaleString('default', { month: 'short' })} '${String(firstDt.getFullYear()).slice(-2)}` });
            }
            if (data.length > 1) {
                const lastRaw = data[data.length - 1].month || data[data.length - 1].date;
                const lastDt = parseDashboardDate(lastRaw);
                if (lastDt) {
                    monthLabels.push({ x: (data.length - 1) * stepX, y: height - 6, label: `${lastDt.toLocaleString('default', { month: 'short' })} '${String(lastDt.getFullYear()).slice(-2)}` });
                }
            }
        }

        const gridLines = [];
        const gridCount = 3;
        for (let i = 0; i <= gridCount; i++) {
            const y = padY + (chartH / gridCount) * i;
            gridLines.push({ y });
        }

        return { linePath, areaPath, endPoint, labels, gridLines, monthLabels };
    }
    // Memoize: deep content-aware cache — only recompute when the data
    // actually differs (length, first value, last value) or dimensions change.
    let nwChart = { linePath: '', areaPath: '', endPoint: null, labels: [], gridLines: [], monthLabels: [] };

    const _nwChartCache = { key: '', result: nwChart };

    function getNWChartCacheKey(data, width, height) {
        if (!Array.isArray(data) || data.length < 2) return 'empty';
        return `${data.length}|${data[0].month}:${data[0].value}|${data[data.length - 1].month}:${data[data.length - 1].value}|${width}x${height}`;
    }

    $: {
        if (Array.isArray(netWorthTrendData) && netWorthTrendData.length >= 2) {
            const cacheKey = getNWChartCacheKey(netWorthTrendData, 600, 160);
            if (cacheKey !== _nwChartCache.key) {
                _nwChartCache.key = cacheKey;
                _nwChartCache.result = buildNWAreaChart(netWorthTrendData);
            }
            nwChart = _nwChartCache.result;
        } else {
            nwChart = { linePath: '', areaPath: '', endPoint: null, labels: [], gridLines: [], monthLabels: [] };
        }
    }

    $: nwTrendDirection = (Array.isArray(netWorthTrendData) && netWorthTrendData.length >= 2)
        ? (netWorthTrendData[netWorthTrendData.length - 1].value >= netWorthTrendData[netWorthTrendData.length - 2].value ? 'up' : 'down')
        : 'flat';
    $: nwAreaFillColor = nwTrendDirection === 'up' ? '#34d399' : nwTrendDirection === 'down' ? '#f87171' : '#627d98';

    // Credit card utilization helper
    function getUtilization(card) {
        const limit = card.limit || card.credit_limit || 10000;
        const balance = Math.abs(parseFloat(card.balance || 0));
        return Math.min((balance / limit) * 100, 100);
    }

    function isCreditCardAccount(account) {
        const accountType = (account?.account_type || '').toLowerCase();
        return accountType === 'credit' || accountType === 'credit_card';
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

    function dueDayLabel(account) {
        const ordinal = formatOrdinalDay(account?.usual_due_day);
        return ordinal ? `Due ${ordinal}` : '';
    }

    function openAccountPaymentDetails(account) {
        if (!account?.id) return;
        goto(`/control-center?tab=accounts&account=${encodeURIComponent(account.id)}`);
    }

    function isManualStale(account) {
        if (account?.provider !== 'manual' || !account.manual_updated_at) return false;
        const updated = new Date(String(account.manual_updated_at).replace(' ', 'T'));
        if (Number.isNaN(updated.getTime())) return false;
        return (Date.now() - updated.getTime()) / 86400000 > 45;
    }

    // Sankey drill-down
    function isCreditRefundTransaction(tx) {
        const amount = parseFloat(tx?.amount || 0);
        return amount > 0 && (tx?.category === 'Credits & Refunds' || (tx?.category !== 'Income' && !NON_SPENDING_CATEGORIES_SET.has(tx?.category)));
    }

    async function fetchSankeyPeriodTransactions(targetMonth) {
        if (selectedPeriod === 'ytd') {
            const year = new Date().getFullYear().toString();
            const ytdMonthKeys = monthly.filter(m => m.month.startsWith(year)).map(m => m.month);
            const results = await Promise.all(ytdMonthKeys.map(m => api.getTransactions({ month: m, limit: 1000 }).then(r => r.data)));
            return results.flat();
        }
        if (selectedPeriod === 'all') {
            return (await api.getTransactions({ limit: 1000 })).data;
        }
        const params = { limit: 1000 };
        if (targetMonth) params.month = targetMonth;
        return (await api.getTransactions(params)).data;
    }

    async function handleSankeySelect(event) {
        const cat = event.detail;
        cancelDrillEditing();
        drillUpdateFeedback = '';
        drillRecentlyUpdatedTxId = null;
        if (cat === null || cat === selectedSankeyCategory) {
            selectedSankeyCategory = null;
            sankeyDrillTxns = [];
            await tick();
            return;
        }

        selectedSankeyCategory = cat;
        const targetMonth = ['this_month', 'last_month', 'custom'].includes(selectedPeriod)
            ? getMonthForPeriod(selectedPeriod) : null;

        try {
            let allTxns = [];
            if (cat === CREDIT_DRILLDOWN_CATEGORY) {
                allTxns = (await fetchSankeyPeriodTransactions(targetMonth)).filter(isCreditRefundTransaction);
            } else if (selectedPeriod === 'ytd') {
                const year = new Date().getFullYear().toString();
                const ytdMonthKeys = monthly.filter(m => m.month.startsWith(year)).map(m => m.month);
                const results = await Promise.all(ytdMonthKeys.map(m => api.getTransactions({ month: m, category: cat, limit: 1000 }).then(r => r.data)));
                allTxns = results.flat();
            } else if (selectedPeriod === 'all') {
                allTxns = (await api.getTransactions({ category: cat, limit: 1000 })).data;
            } else {
                const params = { limit: 1000 };
                if (targetMonth) params.month = targetMonth;
                params.category = cat;
                allTxns = (await api.getTransactions(params)).data;
            }
            sankeyDrillTxns = allTxns.sort((a, b) => Math.abs(parseFloat(b.amount)) - Math.abs(parseFloat(a.amount)));
            await tick();
            if (drillDownSection) drillDownSection.scrollIntoView({ behavior: 'smooth', block: 'center' });
        } catch (e) { sankeyDrillTxns = []; }
    }

    function getDrillDownTotal(category) {
        if (category === CREDIT_DRILLDOWN_CATEGORY) {
            return sankeyDrillTxns.reduce((s, t) => s + Math.max(parseFloat(t.amount || 0), 0), 0);
        }
        const catEntry = sankeyCategoryList.find(c => c.category === category);
        if (catEntry) return catEntry.total;
        return sankeyDrillTxns.reduce((s, t) => s + Math.abs(parseFloat(t.amount)), 0);
    }

    // ââ Sankey drill-down re-categorization ââ

    // Fetch categories list (lazy-loaded on first drill-down edit)
    async function ensureDrillCategories() {
        if (drillAllCategories.length === 0) {
            try {
                drillAllCategories = await api.getCategories();
            } catch (_) {}
        }
    }

    // Filtered category list for the re-tag dropdown search
    $: drillFilteredCategories = drillCatDropdownSearch
        ? drillAllCategories.filter(c => c.toLowerCase().includes(drillCatDropdownSearch.toLowerCase()))
        : drillAllCategories;

    function startDrillEditing(txId) {
        ensureDrillCategories();
        drillCatDropdownOpenForTx = txId;
        drillCatDropdownSearch = '';
        drillCategoryApplyMode = 'always';
        drillCreatingNewCategory = false;
        drillNewCategoryName = '';
        drillNewCategoryError = '';
    }

    function cancelDrillEditing() {
        drillCatDropdownOpenForTx = null;
        drillCatDropdownSearch = '';
        drillCreatingNewCategory = false;
        drillNewCategoryName = '';
        drillNewCategoryError = '';
        drillCategoryApplyMode = 'always';
    }

    async function drillUpdateCategory(txId, newCategory, oneOff = false) {
        try {
            const result = await api.updateCategory(txId, newCategory, oneOff);
            const tx = sankeyDrillTxns.find(t => t.original_id === txId);
            if (tx) {
                const oldCategory = tx.category;
                tx.category = newCategory;
                tx.confidence = 'manual';
                tx.categorization_source = 'user';
                sankeyDrillTxns = sankeyDrillTxns;

                // If re-categorized OUT of the currently drilled category,
                // remove from the list and update the Sankey
                if (newCategory !== selectedSankeyCategory) {
                    sankeyDrillTxns = sankeyDrillTxns.filter(t => t.original_id !== txId);
                }
            }

            // Show feedback
            drillRecentlyUpdatedTxId = txId;
            const retro = result?.retroactive_count ?? 0;
            drillUpdateFeedback = oneOff
                ? `Categorized as "${newCategory}" — this transaction only`
                : retro > 0
                    ? `Categorized as "${newCategory}" — updated ${retro} existing similar transaction${retro !== 1 ? 's' : ''}`
                    : `Categorized as "${newCategory}"`;

            // Invalidate cache since category rules may have changed
            invalidateCache();

            // Refresh categories list
            try {
                drillAllCategories = await api.getCategories();
            } catch (_) {}

            // Save drill category — updatePeriod() clears selectedSankeyCategory immediately
            const prevCategory = selectedSankeyCategory;

            // Refresh the Sankey so flows reflect the new categorization
            await updatePeriod();

            // Re-open the drill-down with fresh data so retroactively updated
            // transactions are visible immediately without re-clicking
            if (prevCategory) {
                selectedSankeyCategory = prevCategory;
                const targetMonth = ['this_month', 'last_month', 'custom'].includes(selectedPeriod)
                    ? getMonthForPeriod(selectedPeriod) : null;
                try {
                    let allTxns = [];
                    if (selectedPeriod === 'ytd') {
                        const year = new Date().getFullYear().toString();
                        const ytdMonthKeys = monthly.filter(m => m.month.startsWith(year)).map(m => m.month);
                        const results = await Promise.all(
                            ytdMonthKeys.map(m => api.getTransactions({ month: m, category: prevCategory, limit: 1000 }).then(r => r.data))
                        );
                        allTxns = results.flat();
                    } else if (selectedPeriod === 'all') {
                        allTxns = (await api.getTransactions({ category: prevCategory, limit: 1000 })).data;
                    } else {
                        const params = { limit: 1000 };
                        if (targetMonth) params.month = targetMonth;
                        params.category = prevCategory;
                        allTxns = (await api.getTransactions(params)).data;
                    }
                    sankeyDrillTxns = allTxns.sort((a, b) => Math.abs(parseFloat(b.amount)) - Math.abs(parseFloat(a.amount)));
                } catch (_) { sankeyDrillTxns = []; }
            }

            // Clear feedback after delay
            setTimeout(() => {
                if (drillRecentlyUpdatedTxId === txId) {
                    drillRecentlyUpdatedTxId = null;
                    drillUpdateFeedback = '';
                }
            }, 4000);
        } catch (e) {
            console.error('Failed to update category from drill-down:', e);
            drillUpdateFeedback = 'Failed to update category';
            setTimeout(() => { drillUpdateFeedback = ''; }, 3000);
        }
        cancelDrillEditing();
    }

    async function drillCreateAndApplyCategory(txId) {
        const name = drillNewCategoryName.trim();
        if (!name) {
            drillNewCategoryError = 'Category name cannot be empty';
            return;
        }

        if (drillAllCategories.some(c => c.toLowerCase() === name.toLowerCase())) {
            const existing = drillAllCategories.find(c => c.toLowerCase() === name.toLowerCase());
            drillCreatingNewCategory = false;
            drillNewCategoryName = '';
            drillNewCategoryError = '';
            if (existing) {
                await drillUpdateCategory(txId, existing, drillCategoryApplyMode === 'once');
            }
            return;
        }

        try {
            await api.createCategory(name);
            drillAllCategories = [...drillAllCategories, name].sort();
            drillCreatingNewCategory = false;
            drillNewCategoryName = '';
            drillNewCategoryError = '';
            await drillUpdateCategory(txId, name, drillCategoryApplyMode === 'once');
        } catch (e) {
            drillNewCategoryError = 'Failed to create category';
            console.error(e);
        }
    }

    /** Close drill-down category dropdown on window click */
    function handleDrillWindowClick() {
        if (drillCatDropdownOpenForTx) {
            cancelDrillEditing();
        }
        if (editingExpenseType) {
            cancelEditingExpenseType();
        }
        if (expandedUpcomingGroup) {
            expandedUpcomingGroup = '';
        }
    }

    function toggleMonthlyPlan() {
        monthlyPlanCollapsed = !monthlyPlanCollapsed;
        if (monthlyPlanCollapsed) {
            expandedUpcomingGroup = '';
            expandedUpcomingItemKey = '';
            upcomingEditKey = '';
        }
    }

    // ══════════════════════════════════════════
    // S4: INCOME vs SPENDING (Luminous Overlap)
    // ══════════════════════════════════════════
    // Shared last-12-month data used by both ivsStats and the 5th stat card
    $: ivsChartData = (() => {
        if (!monthly || monthly.length === 0) return [];
        const sorted = [...monthly].sort((a, b) => a.month.localeCompare(b.month));
        const last12 = sorted.slice(-12);
        return last12.map(m => {
            const netExp = (m.expenses || 0) - (m.refunds || 0);
            return {
                month: m.month,
                income: m.income || 0,
                spending: Math.max(netExp, 0),
                net: (m.income || 0) - netExp,
            };
        });
    })();

    $: ivsStats = (() => {
        if (ivsChartData.length < 2) return null;
        const months = ivsChartData;

        const totalIncome = months.reduce((s, m) => s + m.income, 0);
        const totalSpending = months.reduce((s, m) => s + m.spending, 0);
        const avgIncome = totalIncome / months.length;
        const avgSpending = totalSpending / months.length;
        const avgNet = avgIncome - avgSpending;

        const sorted = [...monthly].sort((a, b) => a.month.localeCompare(b.month));
        const last12 = sorted.slice(-12);

        const highestSpendMonth = last12.reduce((best, m) => {
            const netExp = (m.expenses || 0) - (m.refunds || 0);
            return netExp > (best.val || 0) ? { month: m.month, val: netExp } : best;
        }, { month: '', val: 0 });

        const bestSavingsMonth = last12.reduce((best, m) => {
            const netExp = (m.expenses || 0) - (m.refunds || 0);
            const net = (m.income || 0) - netExp;
            return net > (best.val || -Infinity) ? { month: m.month, val: net } : best;
        }, { month: '', val: -Infinity });

        return {
            avgIncome,
            avgSpending,
            avgNet,
            highestSpendMonth: highestSpendMonth.month,
            highestSpendVal: highestSpendMonth.val,
            bestSavingsMonth: bestSavingsMonth.month,
            bestSavingsVal: bestSavingsMonth.val,
            monthCount: months.length,
        };
    })();

    // ══════════════════════════════════════════
    // S5a: YoY SAME-PERIOD COMPARISON
    // ══════════════════════════════════════════
    $: yoyData = (() => {
        if (!monthly || monthly.length === 0) return null;
        const now = new Date();
        const currentMonth = now.getMonth(); // 0-indexed (0=Jan, 2=Mar)
        const currentYear = now.getFullYear();

        // Group months by year
        const byYear = {};
        for (const m of monthly) {
            const [y, mo] = m.month.split('-').map(Number);
            if (!byYear[y]) byYear[y] = [];
            byYear[y].push({ ...m, monthNum: mo });
        }

        const years = Object.keys(byYear).map(Number).sort();
        if (years.length === 0) return null;

        const currentMonthName = new Date(currentYear, currentMonth).toLocaleString('default', { month: 'short' });

        const yearStats = years.map(yr => {
            let periodMonths;
            let periodLabel;

            if (yr === currentYear) {
                // Current year: only up to the current month (YTD)
                const maxMonth = currentMonth + 1; // monthNum is 1-indexed
                periodMonths = byYear[yr].filter(m => m.monthNum <= maxMonth);
                periodLabel = `YTD (Jan–${currentMonthName})`;
            } else {
                // Past years: use ALL months
                periodMonths = byYear[yr];
                periodLabel = 'Full Year';
            }

            const totalIn = periodMonths.reduce((s, m) => s + m.income, 0);
            const totalOut = periodMonths.reduce((s, m) => s + m.expenses, 0);
            const net = totalIn - totalOut;
            const monthCount = periodMonths.length;
            return { year: yr, totalIn, totalOut, net, monthCount, isPartial: yr === currentYear, periodLabel };
        });

        // Find max value for bar scaling
        const maxVal = Math.max(...yearStats.flatMap(y => [y.totalIn, y.totalOut]), 1);

        // Compute delta: current YTD vs previous year's SAME period for a fair comparison
        let delta = null;
        if (yearStats.length >= 2) {
            const current = yearStats[yearStats.length - 1];
            const previousYearData = byYear[years[years.length - 2]];
            // Compare against same period (Jan–currentMonth) of the previous year
            const prevSamePeriod = previousYearData
                ? previousYearData.filter(m => m.monthNum <= currentMonth + 1)
                : [];
            const prevSamePeriodNet = prevSamePeriod.reduce((s, m) => s + m.income, 0)
                                    - prevSamePeriod.reduce((s, m) => s + m.expenses, 0);
            delta = {
                amount: current.net - prevSamePeriodNet,
                prevYear: years[years.length - 2],
                note: `Jan–${currentMonthName}`
            };
        }

        return { yearStats, maxVal, delta };
    })();


    $: ytdDelta = netWorthYtdDelta;

    /* ── Fixed vs. Variable spending breakdown ── */
    $: fixedVsVariable = (() => {
        const cats = periodCategories || [];
        if (!cats.length) return null;
        const fixedCats = [];
        const variableCats = [];

        for (const cat of cats) {
            if (NON_SPENDING_CATEGORIES_SET.has(cat.category)) continue;
            const expType = cat.expense_type || 'variable';
            if (expType === 'non_expense') continue;
            if (expType === 'fixed') {
                fixedCats.push(cat);
            } else {
                variableCats.push(cat);
            }
        }

        // External transfers are real outflows — add as variable
        const extTransfers = periodSummary?.external_transfers || 0;
        if (extTransfers > 0) {
            variableCats.push({
                category: 'Ext. Transfers',
                total: extTransfers,
                expense_type: 'variable'
            });
        }

        const fixedTotal = fixedCats.reduce((s, c) => s + c.total, 0);
        const variableTotal = variableCats.reduce((s, c) => s + c.total, 0);
        const grandTotal = fixedTotal + variableTotal;
        const fixedPct = grandTotal > 0 ? (fixedTotal / grandTotal) * 100 : 0;
        const variablePct = grandTotal > 0 ? (variableTotal / grandTotal) * 100 : 0;

        fixedCats.sort((a, b) => b.total - a.total);
        variableCats.sort((a, b) => b.total - a.total);

        // Historical averages
        const totalMonths = monthly.length || 1;
        let histFixedTotal = 0;
        let histVariableTotal = 0;
        const histExtTransfers = monthly.reduce((s, m) => s + (m.external_transfers || 0), 0);
        histVariableTotal += histExtTransfers;
        for (const ac of categories) {
            if (NON_SPENDING_CATEGORIES_SET.has(ac.category)) continue;
            const acExpType = ac.expense_type || 'variable';
            if (acExpType === 'non_expense') continue;
            if (acExpType === 'fixed') {
                histFixedTotal += ac.total || 0;
            } else {
                histVariableTotal += ac.total || 0;
            }
        }
        const avgFixed = histFixedTotal / totalMonths;
        const avgVariable = histVariableTotal / totalMonths;
        const fixedDeltaPct = avgFixed > 0 ? ((fixedTotal - avgFixed) / avgFixed) * 100 : 0;
        const variableDeltaPct = avgVariable > 0 ? ((variableTotal - avgVariable) / avgVariable) * 100 : 0;

        return {
            fixedCats, variableCats,
            fixedTotal, variableTotal,
            fixedPct, variablePct, grandTotal,
            avgFixed, avgVariable,
            fixedDeltaPct, variableDeltaPct
        };
    })();

    function startEditingExpenseType(categoryName) {
        editingExpenseType = categoryName;
    }

    function cancelEditingExpenseType() {
        editingExpenseType = null;
    }

    async function toggleExpenseType(categoryName, newType) {
        try {
            await api.updateExpenseType(categoryName, newType);
            const updateList = (list) => list.map(c =>
                c.category === categoryName ? { ...c, expense_type: newType } : c
            );
            periodCategories = updateList(periodCategories);
            categories = updateList(categories);
            invalidateCache();
            expenseTypeFeedback = `${categoryName} → ${newType === 'fixed' ? 'Fixed' : 'Variable'}`;
            setTimeout(() => { expenseTypeFeedback = ''; }, 3000);
        } catch (e) {
            console.error('Failed to update expense type:', e);
            expenseTypeFeedback = 'Failed to update';
            setTimeout(() => { expenseTypeFeedback = ''; }, 3000);
        }
        editingExpenseType = null;
    }
    // ══════════════════════════════════════════
    // S5b: NET WORTH TRAJECTORY (Plotly)
    // ══════════════════════════════════════════

    $: trajectoryStats = (() => {
        if (!Array.isArray(netWorthTrendData) || netWorthTrendData.length < 2) return null;
        const values = netWorthTrendData.map(d => d.value);
        const first = values[0];
        const last = values[values.length - 1];
        const change = last - first;
        const peak = Math.max(...values);
        const low = Math.min(...values);
        const peakIdx = values.indexOf(peak);
        const lowIdx = values.indexOf(low);

        const peakDate = netWorthTrendData[peakIdx]?.month || netWorthTrendData[peakIdx]?.date || '';
        const lowDate = netWorthTrendData[lowIdx]?.month || netWorthTrendData[lowIdx]?.date || '';

        // Estimate monthly growth
        const months = netWorthTrendData.length > 1 ? netWorthTrendData.length / 2 : 1; // biweekly data
        const avgGrowth = change / Math.max(months, 1);

        function formatShortDate(dateStr) {
            const dt = parseDashboardDate(dateStr);
            if (!dt) return '';
            return dt.toLocaleString('default', { month: 'short', year: '2-digit' });
        }

        return { change, peak, low, peakDate: formatShortDate(peakDate), lowDate: formatShortDate(lowDate), avgGrowth, current: last };
    })();

    // ── Net Worth Trajectory SVG (replaces Plotly) ─────────────
    let prevTrajRef = null;
    let trajectorySVG = null;

    $: {
        if (netWorthTrendData !== prevTrajRef) {
            prevTrajRef = netWorthTrendData;
            trajectorySVG = (() => {
                if (!Array.isArray(netWorthTrendData) || netWorthTrendData.length < 2) return null;

                const dates = netWorthTrendData.map(d => {
                    const raw = d.month || d.date;
                    const dt = parseDashboardDate(raw);
                    return dt ? dt.toLocaleDateString('en-US', { month: 'short', year: '2-digit' }) : '';
                });
                const values = netWorthTrendData.map(d => d.value);

                const W = 640, H = 200;
                const pad = { top: 12, right: 12, bottom: 32, left: 55 };
                const plotW = W - pad.left - pad.right;
                const plotH = H - pad.top - pad.bottom;

                const rawMin = Math.min(...values);
                const rawMax = Math.max(...values);
                const range = rawMax - rawMin || 1;
                const min = rawMin - range * 0.08;
                const max = rawMax + range * 0.08;
                const fullRange = max - min;

                const stepX = plotW / (values.length - 1 || 1);
                const yScale = (v) => pad.top + plotH - ((v - min) / fullRange) * plotH;

                const points = values.map((v, i) => ({
                    x: pad.left + i * stepX,
                    y: yScale(v)
                }));

                // Build smooth line path (reuse catmullRom from NW chart logic)
                let linePath = `M${points[0].x.toFixed(1)},${points[0].y.toFixed(1)}`;
                for (let i = 1; i < points.length; i++) {
                    const prev = points[i - 1];
                    const curr = points[i];
                    const cpx = (prev.x + curr.x) / 2;
                    linePath += ` C${cpx.toFixed(1)},${prev.y.toFixed(1)} ${cpx.toFixed(1)},${curr.y.toFixed(1)} ${curr.x.toFixed(1)},${curr.y.toFixed(1)}`;
                }

                const lastPt = points[points.length - 1];
                const firstPt = points[0];
                const areaPath = linePath + ` L${lastPt.x.toFixed(1)},${pad.top + plotH} L${firstPt.x.toFixed(1)},${pad.top + plotH} Z`;

                // Y-axis ticks
                const tickCount = 4;
                const yTicks = Array.from({ length: tickCount }, (_, i) => {
                    const v = min + (fullRange * (tickCount - 1 - i)) / (tickCount - 1);
                    return { value: v, y: yScale(v) };
                });

                // X-axis labels (every nth)
                const labelEvery = Math.max(1, Math.floor(values.length / 8));
                const xLabels = dates.map((label, i) => ({
                    x: pad.left + i * stepX,
                    label,
                    show: i % labelEvery === 0 || i === dates.length - 1
                })).filter(l => l.show);

                return { W, H, pad, plotW, plotH, linePath, areaPath, yTicks, xLabels, points };
            })();
        }
    }

    $: greeting = getGreeting();
    $: isHousehold = $activeProfile === 'household';
    $: activeProfileObj = $profiles.find(p => p.id === $activeProfile);
    $: activeProfileName = activeProfileObj?.name ?? ($activeProfile ? $activeProfile.charAt(0).toUpperCase() + $activeProfile.slice(1) : '');
</script>

<svelte:window on:click={handleDrillWindowClick} on:keydown={handleWindowKeydown} />

{#if loading}
    <div class="space-y-6 fade-in">
        <!-- Header skeleton -->
        <div>
            <div class="skeleton h-3 w-24 mb-2"></div>
            <div class="skeleton h-8 w-48"></div>
        </div>
        <!-- Hero card: net worth number placeholder -->
        <div class="card-hero" style="min-height: 180px; display: flex; flex-direction: column; justify-content: center; padding: 1.5rem;">
            <div class="skeleton h-3 w-20 mb-3" style="border-radius: 6px;"></div>
            <div class="skeleton-hero-number"></div>
            <div class="skeleton h-2 w-32 mt-3" style="border-radius: 4px;"></div>
        </div>
        <!-- Metric ribbon skeleton -->
        <div class="skeleton h-16 rounded-2xl" style="opacity: 0.7"></div>
        <!-- Sankey chart skeleton -->
        <div>
            <div class="skeleton h-3 w-28 mb-3" style="border-radius: 6px;"></div>
            <div class="skeleton-chart-block skeleton-sankey rounded-2xl"></div>
        </div>
        <!-- Two-panel row skeleton -->
        <div class="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <div class="skeleton h-64 rounded-2xl"></div>
            <div class="skeleton h-64 rounded-2xl"></div>
        </div>
    </div>
{:else if summary}

<div class="profile-transition" class:profile-loading={profileSwitching}>

    <!-- ═══ MIGRATION BANNER ═══ -->
    {#if migrationStatus?.needs_migration && !migrationBannerDismissed}
        <div class="migration-banner fade-in">
            <div class="migration-banner-left">
                <span class="material-symbols-outlined migration-banner-icon">swap_horiz</span>
                <div>
                    <p class="migration-banner-title">Teller &amp; SimpleFIN overlap detected</p>
                    <p class="migration-banner-sub">
                        Both providers have {migrationStatus.overlap_days} days of overlapping data (from {migrationStatus.simplefin_window_start}).
                        Migrate to avoid double-counting and keep your full history.
                    </p>
                </div>
            </div>
            <div class="migration-banner-actions">
                <a href="/control-center?tab=connections" class="migration-banner-btn-primary">
                    Review &amp; Migrate
                </a>
                <button
                    class="migration-banner-btn-dismiss"
                    on:click={() => {
                        migrationBannerDismissed = true;
                        localStorage.setItem('migration_banner_dismissed', '1');
                    }}
                    aria-label="Dismiss"
                >
                    <span class="material-symbols-outlined text-[16px]">close</span>
                </button>
            </div>
        </div>
    {/if}

    {#if dataHealthWarnings.length > 0 || staleManualAccounts.length > 0}
        <section class="mb-6 fade-in-up" style="animation-delay: 48ms">
            <div class="card" style="padding: 1rem; border-color: color-mix(in srgb, var(--warning) 36%, var(--card-border)); background: color-mix(in srgb, var(--warning) 7%, var(--card-bg));">
                <div class="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
                    <div style="min-width: 220px;">
                        <p class="text-[10px] font-bold tracking-[0.14em] uppercase" style="color: var(--warning)">Data Confidence</p>
                        <h3 class="folio-section-title" style="margin:0.15rem 0 0">
                            {dataHealthAttentionCount} item{dataHealthAttentionCount === 1 ? '' : 's'} need attention
                        </h3>
                        <p class="text-xs mt-1" style="color: var(--text-muted)">
                            {dataHealth?.summary?.stale_accounts || staleManualAccounts.length} stale account{(dataHealth?.summary?.stale_accounts || staleManualAccounts.length) === 1 ? '' : 's'} · latest update {dataHealth?.summary?.latest_account_update ? formatDate(dataHealth.summary.latest_account_update) : 'unknown'}
                        </p>
                    </div>

                    <div class="grid gap-2" style="flex: 1; min-width: 0;">
                        {#each dataHealthWarnings as warning}
                            <div class="flex items-center gap-2 text-xs" style="color: var(--text-primary);">
                                <span class="material-symbols-outlined text-[15px]" style="color: var(--warning)">error</span>
                                <span>{warning.message}</span>
                            </div>
                        {/each}
                        {#if manualQuickMessage}
                            <p class="text-xs" style="color: var(--positive)">{manualQuickMessage}</p>
                        {/if}
                    </div>

                    {#if staleManualAccounts.length > 0}
                        <div class="grid gap-2" style="min-width: min(360px, 100%);">
                            {#each staleManualAccounts as account}
                                <div class="flex items-center gap-2" style="background: color-mix(in srgb, var(--card-bg) 82%, transparent); border: 1px solid var(--card-border); border-radius: 0.75rem; padding: 0.5rem;">
                                    <div class="min-w-0 flex-1">
                                        <p class="text-[11px] font-semibold truncate" style="color: var(--text-primary)">{account.name}</p>
                                        <p class="text-[9px]" style="color: var(--text-muted)">updated {account.manual_updated_at ? formatDate(account.manual_updated_at) : 'never'}</p>
                                    </div>
                                    <input
                                        type="number"
                                        step="0.01"
                                        placeholder={Math.abs(Number(account.balance || 0)).toFixed(2)}
                                        bind:value={manualQuickBalances[account.id]}
                                        class="text-xs"
                                        style="width: 104px; border: 1px solid var(--card-border); border-radius: 0.6rem; background: var(--surface-100); color: var(--text-primary); padding: 0.45rem 0.55rem;"
                                    />
                                    <button
                                        class="dashboard-plan-link"
                                        disabled={manualQuickSavingId === account.id}
                                        on:click={() => quickUpdateManualAccount(account)}
                                    >
                                        {manualQuickSavingId === account.id ? 'Saving...' : 'Update'}
                                    </button>
                                </div>
                            {/each}
                        </div>
                    {/if}
                </div>
            </div>
        </section>
    {/if}

    {#if showProactiveSection}
        <section class="mb-3 fade-in-up" style="animation-delay: 54ms">
            <div class="card" style="padding: 0.75rem 0.85rem;">
                <div class="flex items-center justify-between gap-3">
                    <div class="min-w-0">
                        <p class="text-[10px] font-bold tracking-[0.14em] uppercase" style="color: var(--accent)">Money Signals</p>
                        <h3 class="text-sm font-semibold truncate" style="color: var(--text-primary); margin:0.1rem 0 0">{signalCountLabel(visibleProactiveInsights.length)}</h3>
                    </div>
                    <div class="flex items-center gap-2">
                        {#if proactiveLoading || proactiveAnalystRefreshing}
                            <span class="text-xs" style="color: var(--text-muted)">Refreshing</span>
                        {/if}
                        <button
                            type="button"
                            class="privacy-toggle-btn"
                            disabled={proactiveLoading || proactiveAnalystRefreshing}
                            on:click={refreshBackgroundAnalystInsights}
                            aria-label="Refresh money signals"
                            title="Refresh money signals"
                        >
                            <span class="material-symbols-outlined text-[12px]">
                                {proactiveAnalystRefreshing ? 'hourglass_top' : 'auto_awesome'}
                            </span>
                        </button>
                    </div>
                </div>

                {#if primaryProactiveInsight}
                    <article class="flex gap-2 items-start mt-3 pt-3" style="border-top: 1px solid var(--card-border);">
                        <span class="material-symbols-outlined text-[17px]" style="color: {primaryProactiveInsight.severity === 'critical' ? 'var(--negative)' : primaryProactiveInsight.severity === 'warning' ? 'var(--warning)' : 'var(--accent)'}">
                            {insightIcon(primaryProactiveInsight)}
                        </span>
                        <div class="min-w-0 flex-1">
                            <div class="flex items-center gap-2 min-w-0">
                                <strong class="block text-sm truncate" style="color: var(--text-primary)">{primaryProactiveInsight.title}</strong>
                                {#if insightMeta(primaryProactiveInsight)}
                                    <small class="shrink-0 text-[10px]" style="color: var(--text-muted)">{insightMeta(primaryProactiveInsight)}</small>
                                {/if}
                            </div>
                            {#if shouldExpandInsight(primaryProactiveInsight)}
                                <p class="text-xs mt-1" style="color: var(--text-muted)">{primaryProactiveInsight.body}</p>
                                {#if primaryProactiveInsight.recommended_action}
                                    <p class="text-xs mt-1" style="color: var(--text-primary)">{primaryProactiveInsight.recommended_action}</p>
                                {/if}
                            {:else}
                                <p class="text-xs mt-1 truncate" style="color: var(--text-muted)">{primaryProactiveInsight.body}</p>
                            {/if}
                            {#if insightEvidenceLines(primaryProactiveInsight).length > 0}
                                <details class="mt-1 text-[10px]" style="color: var(--text-muted)">
                                    <summary>Evidence</summary>
                                    <div class="mt-1 space-y-1">
                                        {#each insightEvidenceLines(primaryProactiveInsight) as line}
                                            <div>{line}</div>
                                        {/each}
                                    </div>
                                </details>
                            {/if}
                            {#if debugInsights}
                                <details class="mt-2 text-[10px]" style="color: var(--text-muted)">
                                    <summary>Evidence debug</summary>
                                    <pre class="mt-1 whitespace-pre-wrap">{JSON.stringify(insightDebugDetails(primaryProactiveInsight), null, 2)}</pre>
                                </details>
                            {/if}
                        </div>
                        <button
                            type="button"
                            class="privacy-toggle-btn"
                            on:click={() => dismissProactiveInsight(primaryProactiveInsight.id)}
                            aria-label="Dismiss insight"
                            title="Dismiss insight"
                        >
                            <span class="material-symbols-outlined text-[12px]">close</span>
                        </button>
                    </article>
                {/if}

                {#if secondaryProactiveInsights.length > 0}
                    <details class="mt-2 text-xs" style="color: var(--text-muted)">
                        <summary>{secondaryProactiveInsights.length} more {secondaryProactiveInsights.length === 1 ? 'signal' : 'signals'}</summary>
                        <div class="grid gap-2 mt-2">
                            {#each secondaryProactiveInsights as insight}
                                <article class="flex gap-2 items-start">
                                    <span class="material-symbols-outlined text-[15px]" style="color: {insight.severity === 'critical' ? 'var(--negative)' : insight.severity === 'warning' ? 'var(--warning)' : 'var(--accent)'}">
                                        {insightIcon(insight)}
                                    </span>
                                    <div class="min-w-0 flex-1">
                                        <div class="flex items-center gap-2 min-w-0">
                                            <strong class="block text-xs truncate" style="color: var(--text-primary)">{insight.title}</strong>
                                            {#if insightMeta(insight)}
                                                <small class="shrink-0 text-[10px]" style="color: var(--text-muted)">{insightMeta(insight)}</small>
                                            {/if}
                                        </div>
                                        <p class="text-[11px] mt-1 truncate" style="color: var(--text-muted)">{insight.body}</p>
                                        {#if insightEvidenceLines(insight).length > 0}
                                            <details class="mt-1 text-[10px]" style="color: var(--text-muted)">
                                                <summary>Evidence</summary>
                                                <div class="mt-1 space-y-1">
                                                    {#each insightEvidenceLines(insight) as line}
                                                        <div>{line}</div>
                                                    {/each}
                                                </div>
                                            </details>
                                        {/if}
                                    </div>
                                    <button
                                        type="button"
                                        class="privacy-toggle-btn"
                                        on:click={() => dismissProactiveInsight(insight.id)}
                                        aria-label="Dismiss insight"
                                        title="Dismiss insight"
                                    >
                                        <span class="material-symbols-outlined text-[12px]">close</span>
                                    </button>
                                </article>
                            {/each}
                        </div>
                    </details>
                {/if}

                {#if debugInsights && primaryProactiveInsight}
                    <details class="mt-2 text-[10px]" style="color: var(--text-muted)">
                        <summary>All signal debug</summary>
                        <pre class="mt-1 whitespace-pre-wrap">{JSON.stringify(visibleProactiveInsights.map(insightDebugDetails), null, 2)}</pre>
                    </details>
                {/if}

                {#if proactiveAnalystMessage && visibleProactiveInsights.length > 0}
                    <p class="text-xs mt-2" style="color: var(--text-muted)">{proactiveAnalystMessage}</p>
                {/if}
            </div>
        </section>
    {/if}

    <!-- ═══ HEADER ═══ -->
    <div class="dashboard-hero-header mb-8 fade-in">
        <div class="dashboard-hero-meta-row">
            <p class="dashboard-hero-greeting folio-kicker" style="color: var(--accent)">{greeting}</p>
            <div class="dashboard-hero-controls">
                <button
                    on:click={() => privacyMode.toggle()}
                    class="privacy-toggle-btn"
                    class:privacy-active={$privacyMode}
                    aria-label={$privacyMode ? 'Show values' : 'Hide values'}
                    title={$privacyMode ? 'Show values' : 'Hide values'}
                >
                    <span class="material-symbols-outlined text-[12px]">
                        {$privacyMode ? 'visibility_off' : 'visibility'}
                    </span>
                </button>
                <ProfileSwitcher />
                {#if appConfig.bankLinkingEnabled || appConfig.demoMode}
                    <ConnectionChooser
                        on:open={openConnectionChooser}
                    />
                {/if}
            </div>
        </div>
        {#if isHousehold}
            <h2 class="dashboard-hero-title folio-page-title">
                Your finances at a glance
            </h2>
        {:else}
            <h2 class="dashboard-hero-title dashboard-hero-title-personal folio-page-title">
                <span class="dashboard-hero-title-desktop">{greeting}, {activeProfileName}.</span>
                <span class="dashboard-hero-title-mobile">{activeProfileName}</span>
            </h2>
        {/if}
    </div>

    {#if !appConfig.demoMode && reviewQueue && reviewQueue.unreviewed_count > 0 && !reviewToastDismissed}
        <aside class="dashboard-review-toast fade-in">
            <div class="dashboard-review-toast-icon">
                <span class="material-symbols-outlined">fact_check</span>
            </div>
            <div class="dashboard-review-toast-copy">
                <strong>{reviewQueue.unreviewed_count} to review</strong>
                <span>{(void privacyKey, formatCurrency(reviewQueue.unreviewed_spending || 0))} new spending</span>
            </div>
            <a href="/transactions?review=unreviewed&period=all">Review</a>
            <button type="button" on:click={() => reviewToastDismissed = true} aria-label="Dismiss review reminder">
                <span class="material-symbols-outlined">close</span>
            </button>
        </aside>
    {/if}

    <!-- ═══════════════════════════════════════════════════════
         S1: UNIFIED HERO CARD (Net Worth + Accounts + Credit Cards)
         ═══════════════════════════════════════════════════════ -->
    <section class="mb-6 fade-in-up" style="animation-delay: 60ms">
        <div class="card-hero-unified" class:period-updating={isRefreshing}>
            {#if heroLoading}
                <div class="shimmer-overlay"></div>
            {/if}

            <!-- ——— LEFT ZONE: Net Worth + SVG Chart Background ——— -->
            <div class="hero-zone hero-zone-left" style="padding: 1.25rem 1.5rem 0; position: relative; display: flex; flex-direction: column; min-height: 160px; overflow: hidden;">

                {#if netWorthTrendData.length > 1}
                    <div class="hero-chart-bg" style="pointer-events: auto; width: 100%; margin-top: auto; z-index: 0;">
                        <svg width="100%" height="100%" viewBox="0 0 600 160" preserveAspectRatio="none" style="overflow: visible"
                            role="img"
                            aria-label="Net worth trend"
                            on:mousemove={handleChartHover}
                            on:mouseleave={handleChartLeave}>
                            <defs>
                                <linearGradient id="nwAreaGrad" x1="0" y1="0" x2="0" y2="1">
                                    <stop offset="0%"   stop-color={CYAN_LINE}  stop-opacity="0.42"/>
                                    <stop offset="14%"  stop-color="#5EEAD4"    stop-opacity="0.34"/>
                                    <stop offset="34%"  stop-color={TEAL_AREA}  stop-opacity="0.25"/>
                                    <stop offset="56%"  stop-color={TEAL_AREA}  stop-opacity="0.16"/>
                                    <stop offset="78%"  stop-color={TEAL_AREA}  stop-opacity="0.085"/>
                                    <stop offset="100%" stop-color={TEAL_AREA}  stop-opacity="0.015"/>
                                </linearGradient>
                                <linearGradient id="nwLineGradCyan" x1="0" y1="0" x2="1" y2="0">
                                    <stop offset="0%"   stop-color={CYAN_LINE} stop-opacity="0.72"/>
                                    <stop offset="30%"  stop-color={CYAN_GLOW} stop-opacity="0.94"/>
                                    <stop offset="58%"  stop-color="#E0F2FE"   stop-opacity="0.98"/>
                                    <stop offset="82%"  stop-color={CYAN_GLOW} stop-opacity="0.90"/>
                                    <stop offset="100%" stop-color={CYAN_LINE} stop-opacity="0.80"/>
                                </linearGradient>
                                <filter id="nwGlow" x="-20%" y="-20%" width="140%" height="140%">
                                    <feGaussianBlur in="SourceGraphic" stdDeviation="5.5" result="outerBlur"/>
                                    <feGaussianBlur in="SourceGraphic" stdDeviation="2.2" result="midBlur"/>
                                    <feMerge>
                                        <feMergeNode in="outerBlur"/>
                                        <feMergeNode in="midBlur"/>
                                        <feMergeNode in="SourceGraphic"/>
                                    </feMerge>
                                </filter>
                                <filter id="nwAreaGlow" x="-10%" y="-10%" width="120%" height="120%">
                                    <feGaussianBlur stdDeviation="14" result="areaBlur"/>
                                    <feMerge>
                                        <feMergeNode in="areaBlur"/>
                                        <feMergeNode in="SourceGraphic"/>
                                    </feMerge>
                                </filter>
                                <filter id="nwDotGlow" x="-50%" y="-50%" width="200%" height="200%">
                                    <feGaussianBlur stdDeviation="3.5" result="dotBlur"/>
                                    <feMerge>
                                        <feMergeNode in="dotBlur"/>
                                        <feMergeNode in="SourceGraphic"/>
                                    </feMerge>
                                </filter>
                            </defs>
                            {#each nwChart.gridLines as gl}
                                <line x1="0" y1={gl.y} x2="600" y2={gl.y} stroke="var(--text-muted)" stroke-width="1" opacity="0.06" />
                            {/each}
                            <path d={nwChart.areaPath} fill="url(#nwAreaGrad)" filter="url(#nwAreaGlow)" opacity="0.30" />
                            <path d={nwChart.areaPath} fill="url(#nwAreaGrad)" opacity="0.66" />
                            <path d={nwChart.linePath} fill="none" stroke={CYAN_GLOW} stroke-width="12" stroke-linecap="round" stroke-linejoin="round" filter="url(#nwGlow)" opacity="0.22" />
                            <path d={nwChart.linePath} fill="none" stroke="url(#nwLineGradCyan)" stroke-width="3.1" stroke-linecap="round" stroke-linejoin="round" />
                            <path d={nwChart.linePath} fill="none" stroke="#F0F9FF" stroke-width="0.9" opacity="0.34" stroke-linecap="round" stroke-linejoin="round" />
                            {#if nwChart.endPoint}
                                <circle cx={nwChart.endPoint.x} cy={nwChart.endPoint.y} r="9" fill={CYAN_GLOW} opacity="0.12" filter="url(#nwDotGlow)" />
                                <circle cx={nwChart.endPoint.x} cy={nwChart.endPoint.y} r="4.25" fill="#E6FFFB" stroke={CYAN_DEEP} stroke-width="1.6" />
                            {/if}
                            {#each nwChart.monthLabels as ml}
                                <line
                                    x1={ml.x}
                                    y1={ml.y - 14}
                                    x2={ml.x}
                                    y2={ml.y - 10}
                                    stroke={$darkMode ? 'var(--text-secondary)' : 'var(--island-text-secondary)'}
                                    stroke-width="1"
                                    opacity="0.3"
                                />
                                <text
                                    x={ml.x}
                                    y={ml.y}
                                    text-anchor="middle"
                                    fill={$darkMode ? 'var(--text-secondary)' : 'var(--island-text-secondary)'}
                                    font-size="8"
                                    font-family="Inter, system-ui, sans-serif"
                                    font-weight="600"
                                    letter-spacing="0.5"
                                    opacity="0.85"
                                    style="pointer-events: none"
                                >{ml.label}</text>
                            {/each}
                            <rect x="0" y="0" width="600" height="160" fill="transparent" />
                            {#if hoverPoint}
                                <line x1={hoverPoint.x} y1="8" x2={hoverPoint.x} y2="132" stroke="var(--accent)" stroke-width="1" stroke-dasharray="3,3" opacity="0.3" />
                                <circle cx={hoverPoint.x} cy={hoverPoint.y} r="5" fill="#CCFBF1" stroke={CYAN_LINE} stroke-width="2" filter="url(#nwDotGlow)" />
                                <rect x={hoverPoint.x > 480 ? hoverPoint.x - 130 : hoverPoint.x + 10} y={Math.max(4, hoverPoint.y - 36)} width="120" height="32" rx="6" fill="var(--bg-card)" stroke="var(--accent)" stroke-width="1" opacity="0.95" />
                                <text x={hoverPoint.x > 480 ? hoverPoint.x - 70 : hoverPoint.x + 70} y={Math.max(4, hoverPoint.y - 36) + 13} text-anchor="middle" fill="var(--text-primary)" font-size="10" font-weight="700" font-family="DM Mono, monospace" style="pointer-events: none">{(void privacyKey, formatCurrency(hoverPoint.value))}</text>
                                <text x={hoverPoint.x > 480 ? hoverPoint.x - 70 : hoverPoint.x + 70} y={Math.max(4, hoverPoint.y - 36) + 26} text-anchor="middle" fill="var(--text-muted)" font-size="8" font-weight="500" font-family="Inter, system-ui, sans-serif" style="pointer-events: none">{hoverPoint.date}</text>
                            {/if}
                        </svg>
                    </div>
                {/if}

                <div class="flex items-center justify-between mb-1">
                    <p class="text-[10px] font-bold tracking-[0.18em] uppercase" style="color: {$darkMode ? 'var(--text-muted)' : 'var(--island-text-muted)'}">Net Worth</p>
                    <div style="display:flex; align-items:center; gap:0;">
                        {#if netWorthDelta !== null}
                            <span class="text-[10px] font-mono font-bold px-2.5 py-0.5 rounded-lg"
                                style="background: {netWorthDelta >= 0 ? 'rgba(52,211,153,0.18)' : 'rgba(248,113,113,0.18)'}; color: {netWorthDelta >= 0 ? '#34d399' : '#f87171'}">
                                {netWorthDelta >= 0 ? '▲' : '▼'} {(void privacyKey, formatCurrency(Math.abs(netWorthDelta)))}
                                <span style="font-size:0.6rem; font-weight:600; text-transform:uppercase; letter-spacing:0.05em; opacity:0.7; margin-left:0.25rem;">MoM</span>
                            </span>
                        {/if}
                        {#if ytdDelta !== null}
                            <span class="hero-delta-separator"></span>
                            <span class="text-[10px] font-mono font-bold px-2.5 py-0.5 rounded-lg"
                                style="background: {ytdDelta >= 0 ? 'rgba(52,211,153,0.18)' : 'rgba(248,113,113,0.18)'}; color: {ytdDelta >= 0 ? '#34d399' : '#f87171'}">
                                {ytdDelta >= 0 ? '▲' : '▼'} {(void privacyKey, formatCurrency(Math.abs(ytdDelta)))}
                                <span style="font-size:0.6rem; font-weight:600; text-transform:uppercase; letter-spacing:0.05em; opacity:0.7; margin-left:0.25rem;">YTD</span>
                            </span>
                        {/if}
                    </div>
                </div>
                                
                <p class="folio-stat-value mt-0 mb-2"
                   style="color: {$darkMode ? 'var(--text-primary)' : 'var(--island-text-primary)'}; opacity: {animationStarted ? 1 : 0}; transition: opacity 0.2s ease-out;">
                    {formatCurrency(animationDone ? netWorth : animatedNetWorth)}
                </p>
            </div>

            <!-- ——— CENTER ZONE: Accounts ——— -->
            <div class="hero-zone hero-zone-center">
                <div class="flex items-center gap-2 mb-3">
                    <div class="w-7 h-7 rounded-lg flex items-center justify-center" style="background: {$darkMode ? 'rgba(74, 222, 128, 0.12)' : 'rgba(74, 222, 128, 0.15)'}">
                        <span class="material-symbols-outlined text-[15px]" style="color: #4ade80">account_balance</span>
                    </div>
                    <p class="text-[9px] font-bold tracking-[0.15em] uppercase" style="color: {$darkMode ? 'var(--text-muted)' : 'var(--island-text-muted)'}">Accounts</p>
                    <p class="ml-auto folio-stat-value-sm" style="color: {$darkMode ? 'var(--positive)' : 'var(--island-positive)'}">{(void privacyKey, formatCurrency(totalCash))}</p>
                </div>
                <div class="hero-account-scroll-wrapper">
                    <div class="hero-account-scroll" use:checkScrollOverflow>
                        {#each cashAccounts as acc}
                            {@const accPct = totalCash > 0 ? (parseFloat(acc.balance || 0) / totalCash) * 100 : 0}
                            <div class="hero-account-row">
                            <div class="flex items-center gap-2 flex-1 min-w-0">
                                <span class="material-symbols-outlined text-[14px]" style="color: {$darkMode ? 'var(--text-muted)' : 'var(--island-text-muted)'}">account_balance</span>
                                <div class="flex-1 min-w-0">
                                    <p class="text-[11px] font-medium truncate" style="color: {$darkMode ? 'var(--text-primary)' : 'var(--island-text-primary)'}">{acc.name}</p>
                                    <p class="text-[9px]" style="color: {$darkMode ? 'var(--text-muted)' : 'var(--island-text-muted)'}">
                                        {acc.type.replace('_', ' ')}{#if acc.provider === 'manual'} · manual{#if isManualStale(acc)} · stale{/if}{/if}
                                    </p>
                                    <!-- Proportion bar -->
                                    <div class="account-proportion-track">
                                        <div class="account-proportion-fill" style="width: {accPct}%"></div>
                                    </div>
                                </div>
                            </div>
                            <div class="text-right flex-shrink-0 ml-2">
                                <p class="folio-amount-compact" style="color: {$darkMode ? 'var(--text-primary)' : 'var(--island-text-primary)'}">{(void privacyKey, formatCurrency(acc.balance))}</p>
                                <p class="text-[9px] font-mono" style="color: {$darkMode ? 'var(--text-muted)' : 'var(--island-text-muted)'}">
                                    {accPct.toFixed(0)}%
                                    {#if currentMonthData && prevMonthData && accountDeltas._portfolioDelta !== undefined}
                                        <span style="color: {accountDeltas._portfolioDelta >= 0 ? 'var(--positive)' : 'var(--negative)'}; font-size: 8px;">
                                            {accountDeltas._portfolioDelta >= 0 ? '▲' : '▼'}
                                        </span>
                                    {/if}
                                </p>
                            </div>
                        </div>
                    {/each}
                </div>
                </div>
                {#if currentMonthData && prevMonthData}
                    <div class="hero-zone-footer">
                        <span class="text-[9px] font-medium" style="color: {$darkMode ? 'var(--text-muted)' : 'var(--island-text-muted)'}">vs last month</span>
                        <span class="delta-badge {(currentMonthData.net) >= 0 ? 'delta-up' : 'delta-down'}" style="font-size: 9px;">
                            {currentMonthData.net >= 0 ? '▲' : '▼'} {(void privacyKey, formatCurrency(Math.abs(currentMonthData.net)))} net
                        </span>
                    </div>
                {/if}
            </div>

            <!-- ——— RIGHT ZONE: Credit Cards ——— -->
            <div class="hero-zone hero-zone-right">
                <div class="flex items-center gap-2 mb-3">
                    <div class="w-7 h-7 rounded-lg flex items-center justify-center" style="background: {$darkMode ? 'rgba(245, 158, 11, 0.12)' : 'rgba(245, 158, 11, 0.15)'}">
                        <span class="material-symbols-outlined text-[15px]" style="color: #f59e0b">account_balance_wallet</span>
                    </div>
                    <p class="text-[9px] font-bold tracking-[0.15em] uppercase" style="color: {$darkMode ? 'var(--text-muted)' : 'var(--island-text-muted)'}">Liabilities</p>
                    <p class="ml-auto folio-stat-value-sm" style="color: {$darkMode ? 'var(--warning)' : 'var(--island-warning)'}">{(void privacyKey, formatCurrency(totalOwed))}</p>
                </div>
                <div class="hero-account-scroll-wrapper">
                    <div class="hero-account-scroll" use:checkScrollOverflow>
                        {#each creditAccounts as acc}
                        {@const util = getUtilization(acc)}
                        {@const liabilityIcon = acc.account_type === 'loan'
                            ? (acc.type === 'mortgage' ? 'home' : acc.type === 'auto' ? 'directions_car' : acc.type === 'student' ? 'school' : 'account_balance')
                            : 'credit_card'}
                        {@const isLoan = acc.account_type === 'loan'}
                        {@const dueLabel = isCreditCardAccount(acc) ? dueDayLabel(acc) : ''}
                        <button class="hero-account-row hero-account-row-button" type="button" on:click={() => openAccountPaymentDetails(acc)} title="Open account details">
                            <div class="flex items-center gap-2 flex-1 min-w-0">
                                <span class="material-symbols-outlined text-[14px]" style="color: {$darkMode ? 'var(--text-muted)' : 'var(--island-text-muted)'}">{liabilityIcon}</span>
                                <div class="flex-1 min-w-0">
                                    <p class="text-[11px] font-medium truncate" style="color: {$darkMode ? 'var(--text-primary)' : 'var(--island-text-primary)'}">{acc.name}</p>
                                    <p class="text-[9px] flex items-center gap-1 min-w-0" style="color: {$darkMode ? 'var(--text-muted)' : 'var(--island-text-muted)'}">
                                        <span class="truncate">{acc.type.replace('_', ' ')}{#if acc.provider === 'manual'} · manual{#if isManualStale(acc)} · stale{/if}{/if}</span>
                                        {#if dueLabel}
                                            <span class="hero-due-chip">{dueLabel}</span>
                                        {/if}
                                    </p>
                                    {#if !isLoan}
                                        <div class="utilization-bar-track">
                                            <div class="utilization-bar-fill {util > 70 ? 'high' : ''}" style="width: {util}%"></div>
                                        </div>
                                    {/if}
                                </div>
                            </div>
                            <p class="folio-amount-compact ml-3" style="color: {$darkMode ? 'var(--warning)' : 'var(--island-warning)'}">
                                {(void privacyKey, formatCurrency(Math.abs(parseFloat(acc.balance || 0))))}
                            </p>
                        </button>
                    {/each}
                    {#if creditAccounts.length === 0}
                        <p class="text-xs py-2 text-center" style="color: {$darkMode ? 'var(--text-muted)' : 'var(--island-text-muted)'}">No liabilities</p>
                    {/if}
                </div>
                </div>
                {#if totalOwed > 0}
                    <div class="hero-zone-footer">
                        <span class="text-[9px] font-medium" style="color: {$darkMode ? 'var(--text-muted)' : 'var(--island-text-muted)'}">Total owed</span>
                        <span class="text-[9px] font-mono font-semibold" style="color: {$darkMode ? 'var(--warning)' : 'var(--island-warning)'}; opacity: 0.8">
                            {(void privacyKey, formatCurrency(totalOwed))}
                        </span>
                    </div>
                {/if}
            </div>

        </div>
    </section>

    <!-- ═══════════════════════════════════════════════════════
         MONTHLY PLAN ISLAND
         â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• -->
    {#if planSnapshot}
        <section class="mb-6 fade-in-up" style="animation-delay: 88ms">
            <div
                class="dashboard-monthly-island card"
                class:dashboard-monthly-island-expanded={!!expandedUpcomingGroup}
                class:dashboard-monthly-island-collapsed={monthlyPlanCollapsed}>
                <div class="dashboard-monthly-header">
                    <div>
                        <p>{formatMonth(planSnapshot.month)} · This month</p>
                        <h3 class="folio-section-title">Monthly plan</h3>
                    </div>
                    <div class="dashboard-monthly-header-meta">
                        <span>{scheduled?.scheduled_count || 0} due in {scheduled?.window_days || 45} days</span>
                        <a href="/budget" class="dashboard-plan-link">Open plan</a>
                        <button
                            class="dashboard-monthly-toggle"
                            type="button"
                            on:click|stopPropagation={toggleMonthlyPlan}
                            aria-expanded={!monthlyPlanCollapsed}
                            aria-label={monthlyPlanCollapsed ? 'Expand monthly plan' : 'Collapse monthly plan'}>
                            <span class="material-symbols-outlined">{monthlyPlanCollapsed ? 'expand_more' : 'expand_less'}</span>
                        </button>
                    </div>
                </div>

                <div class="dashboard-monthly-rule" aria-hidden="true">
                    <div
                        class="dashboard-monthly-rule-save"
                        style="width: {Math.max(2, Math.min(100, ((planSnapshot.save_first_target || 0) / Math.max(planSnapshot.planned_income || 0, 1)) * 100))}%;">
                    </div>
                    <div
                        class="dashboard-monthly-rule-fixed"
                        style="width: {Math.max(2, Math.min(100, ((planSnapshot.mandatory_projected || planSnapshot.mandatory_spend || 0) / Math.max(planSnapshot.planned_income || 0, 1)) * 100))}%;">
                    </div>
                    <div
                        class="dashboard-monthly-rule-used"
                        style="width: {Math.max(2, Math.min(100, ((planSnapshot.variable_spend || 0) / Math.max(planSnapshot.planned_income || 0, 1)) * 100))}%;">
                    </div>
                    <div
                        class="dashboard-monthly-rule-open"
                        style="width: {Math.max(0, Math.min(100, ((planSnapshot.safe_to_spend || 0) / Math.max(planSnapshot.planned_income || 0, 1)) * 100))}%;">
                    </div>
                </div>
                {#if monthlyPlanCollapsed}
                    <div class="dashboard-monthly-summary">
                        <div>
                            <span>Safe to spend</span>
                            <strong class:text-negative={(planSnapshot.safe_to_spend || 0) < 0}>{(void privacyKey, formatCurrency(planSnapshot.safe_to_spend || 0))}</strong>
                        </div>
                        <div>
                            <span>Save target</span>
                            <strong>{(void privacyKey, formatCurrency(planSnapshot.save_first_target || 0))}</strong>
                        </div>
                        <div>
                            <span>Mandatory</span>
                            <strong>{(void privacyKey, formatCurrency(planSnapshot.mandatory_spend || 0))}</strong>
                        </div>
                        <div>
                            <span>Upcoming</span>
                            <strong>{(void privacyKey, formatCurrency(confirmedUpcomingTotal || 0))}</strong>
                        </div>
                    </div>
                {:else}
                    <div class="dashboard-monthly-legend">
                        <span><i class="dashboard-monthly-legend-save"></i>Save target</span>
                        <span><i class="dashboard-monthly-legend-fixed"></i>Mandatory</span>
                        <span><i class="dashboard-monthly-legend-used"></i>Variable used</span>
                        <span><i class="dashboard-monthly-legend-open"></i>Remaining</span>
                    </div>

                    <div class="dashboard-monthly-cells">
                        <div class="dashboard-monthly-cell">
                            <span>Save first</span>
                            <strong>{(void privacyKey, formatCurrency(planSnapshot.save_first_target || 0))}</strong>
                            <small>{(void privacyKey, formatCurrency(planSnapshot.save_first_actual || 0))} saved · {(void privacyKey, formatCurrency(planSnapshot.save_first_remaining || 0))} left</small>
                            <div class="dashboard-monthly-progress" aria-hidden="true">
                                <i style="width: {progressPct(planSnapshot.save_first_actual, planSnapshot.save_first_target)}%; background: var(--positive);"></i>
                            </div>
                            <em>{Math.round((planSnapshot.save_first_rate || 0.2) * 100)}% target</em>
                        </div>
                        <div class="dashboard-monthly-cell">
                            <span>Mandatory</span>
                            <strong>{(void privacyKey, formatCurrency(planSnapshot.mandatory_spend || 0))}</strong>
                            <small>
                                spent of {(void privacyKey, formatCurrency(planSnapshot.mandatory_projected || planSnapshot.mandatory_spend || 0))}
                                {#if (planSnapshot.mandatory_remaining || 0) > 0} · {(void privacyKey, formatCurrency(planSnapshot.mandatory_remaining || 0))} left{/if}
                            </small>
                            <div class="dashboard-monthly-progress" aria-hidden="true">
                                <i style="width: {progressPct(planSnapshot.mandatory_spend, planSnapshot.mandatory_projected || planSnapshot.mandatory_spend)}%; background: var(--monthly-fixed-color, #687487);"></i>
                            </div>
                            <em>Fixed spend</em>
                        </div>
                        <div class="dashboard-monthly-cell dashboard-monthly-upcoming">
                            <div>
                                <span>Upcoming bills</span>
                                <strong>{(void privacyKey, formatCurrency(confirmedUpcomingTotal || 0))}</strong>
                                <small>
                                    confirmed in {scheduled?.window_days || 45} days
                                    {#if inferredUpcomingTotal > 0} · {(void privacyKey, formatCurrency(inferredUpcomingTotal))} inferred{/if}
                                    {#if needsReviewUpcomingTotal > 0} · {(void privacyKey, formatCurrency(needsReviewUpcomingTotal))} review{/if}
                                    {#if unscheduledRecurringCount > 0} · {unscheduledRecurringCount} need dates{/if}
                                </small>
                            </div>
                            {#if upcomingGroups.length > 0}
                                <div class="dashboard-upcoming-chips">
                                    {#each displayedUpcomingGroups as group}
                                        <button
                                            class="dashboard-upcoming-chip dashboard-upcoming-group-chip"
                                            class:dashboard-upcoming-group-chip-active={expandedUpcomingGroup === group.key}
                                            type="button"
                                            on:click|stopPropagation={() => expandedUpcomingGroup = expandedUpcomingGroup === group.key ? '' : group.key}
                                            aria-expanded={expandedUpcomingGroup === group.key}>
                                            <span class="material-symbols-outlined">
                                                {upcomingGroupIcon(group.key)}
                                            </span>
                                            <div>
                                                <strong>{group.label}</strong>
                                                <small>{group.scheduled_count} · {(void privacyKey, formatCurrency(group.amount || 0))}</small>
                                            </div>
                                        </button>
                                    {/each}
                                    {#if upcomingGroups.length > 2}
                                        <button
                                            class="dashboard-upcoming-chip dashboard-upcoming-group-chip dashboard-upcoming-more-chip"
                                            class:dashboard-upcoming-group-chip-active={expandedUpcomingGroup === ALL_UPCOMING_GROUPS_KEY}
                                            type="button"
                                            on:click|stopPropagation={() => expandedUpcomingGroup = expandedUpcomingGroup === ALL_UPCOMING_GROUPS_KEY ? '' : ALL_UPCOMING_GROUPS_KEY}
                                            aria-expanded={expandedUpcomingGroup === ALL_UPCOMING_GROUPS_KEY}>
                                            <span class="material-symbols-outlined">{expandedUpcomingGroup === ALL_UPCOMING_GROUPS_KEY ? 'unfold_less' : 'unfold_more'}</span>
                                            <div>
                                                <strong>+{upcomingGroups.length - 2} more</strong>
                                                <small>groups</small>
                                            </div>
                                        </button>
                                    {/if}
                                </div>
                            {/if}
                            <a class="dashboard-upcoming-add-link" href="/transactions">Add from transactions</a>
                        </div>
                        <div class="dashboard-monthly-cell dashboard-monthly-safe-cell">
                            <span>Safe to spend</span>
                            <strong class:text-negative={(planSnapshot.safe_to_spend || 0) < 0}>{(void privacyKey, formatCurrency(planSnapshot.safe_to_spend || 0))}</strong>
                            <small>
                                {(void privacyKey, formatCurrency(planSnapshot.safe_to_spend_spent || 0))} used
                                {#if (planSnapshot.safe_to_spend_limit || 0) > 0} of {(void privacyKey, formatCurrency(planSnapshot.safe_to_spend_limit || 0))}{/if}
                            </small>
                            <div class="dashboard-monthly-progress dashboard-monthly-progress-dark" aria-hidden="true">
                                <i style="width: {progressPct(planSnapshot.safe_to_spend_spent, planSnapshot.safe_to_spend_limit)}%; background: var(--warning);"></i>
                            </div>
                            <small>{planSnapshot.income_basis === 'trailing_average' ? 'Avg income basis' : 'Income basis'}</small>
                            <em>{planSnapshot.active_goal_count || 0} goal{(planSnapshot.active_goal_count || 0) === 1 ? '' : 's'} active</em>
                        </div>
                    </div>

                    {#if expandedUpcomingGroup}
                    <div class="dashboard-upcoming-details">
                        <div class="dashboard-upcoming-details-header">
                            <span>{expandedUpcomingTitle}</span>
                            <strong>{expandedUpcomingItems.length} item{expandedUpcomingItems.length === 1 ? '' : 's'}</strong>
                        </div>
                        {#each expandedUpcomingItems as item}
                            <div class="dashboard-upcoming-item">
                                <button
                                    type="button"
                                    class="dashboard-upcoming-detail-row"
                                    class:dashboard-upcoming-detail-candidate={isUpcomingCandidate(item)}
                                    class:dashboard-upcoming-detail-stale={item.evidence?.stale}
                                    on:click|stopPropagation={() => toggleUpcomingItem(item)}
                                    aria-expanded={expandedUpcomingItemKey === upcomingItemKey(item)}>
                                    <span class="material-symbols-outlined dashboard-upcoming-detail-icon">{upcomingIcon(item)}</span>
                                    <div class="dashboard-upcoming-detail-copy">
                                        <strong>{item.merchant}</strong>
                                        <small>
                                            {upcomingSourceLabel(item)} · {item.next_date ? formatDate(item.next_date) : 'Needs date'} · {(void privacyKey, formatCurrency(item.amount || 0))}
                                            {#if item.evidence?.stale} · stale{/if}
                                        </small>
                                    </div>
                                    {#if isUpcomingCandidate(item)}
                                        <span class="dashboard-upcoming-badge">Inferred</span>
                                    {/if}
                                </button>
                                {#if expandedUpcomingItemKey === upcomingItemKey(item)}
                                    <div class="dashboard-upcoming-drawer">
                                        <div class="dashboard-upcoming-evidence">
                                            <div>
                                                <span>Source</span>
                                                <strong>{upcomingSourceLabel(item)}</strong>
                                            </div>
                                            <div>
                                                <span>Seen</span>
                                                <strong>{item.evidence?.seen_count || item.charge_count || 0} time{(item.evidence?.seen_count || item.charge_count || 0) === 1 ? '' : 's'}</strong>
                                            </div>
                                            <div>
                                                <span>Last paid</span>
                                                <strong>{item.evidence?.last_paid ? formatDate(item.evidence.last_paid) : 'Unknown'}</strong>
                                            </div>
                                            <div>
                                                <span>Amount range</span>
                                                <strong>{(void privacyKey, upcomingAmountRange(item))}</strong>
                                            </div>
                                        </div>

	                                        {#if Array.isArray(item.recent_transactions) && item.recent_transactions.length > 0}
	                                            <div class="dashboard-upcoming-recent">
	                                                {#each item.recent_transactions.slice(0, 3) as tx}
	                                                    <div>
	                                                        <span>{formatDate(tx.date)} · {tx.category || 'Uncategorized'}</span>
	                                                        <strong>{(void privacyKey, formatCurrency(tx.amount || 0))}</strong>
	                                                    </div>
	                                                {/each}
	                                            </div>
	                                        {/if}

                                        {#if item.evidence?.restart_detected}
                                            <div class="dashboard-upcoming-review">
                                                <span class="material-symbols-outlined">restart_alt</span>
                                                <div>
                                                    <strong>Restarted after a gap</strong>
                                                    <small>{upcomingRestartDetail(item)}</small>
                                                </div>
                                            </div>
                                        {/if}

                                        {#if item.amount_review}
                                            <div class="dashboard-upcoming-review">
                                                <span class="material-symbols-outlined">trending_{item.amount_review.direction === 'down' ? 'down' : 'up'}</span>
                                                <div>
                                                    <strong>{upcomingAmountReviewTitle(item)}</strong>
                                                    <small>{(void privacyKey, upcomingAmountReviewDetail(item))}</small>
                                                </div>
                                            </div>
                                            <div class="dashboard-upcoming-actions">
                                                {#if item.amount_review.type === 'same_cycle_adjustment'}
                                                    <button type="button" disabled={upcomingActionKey === upcomingItemKey(item)} on:click|stopPropagation={() => startUpcomingEdit(item, item.amount_review.suggested_amount || item.amount || '')}>
                                                        Review amount
                                                    </button>
                                                {:else}
                                                    <button type="button" disabled={upcomingActionKey === upcomingItemKey(item)} on:click|stopPropagation={() => confirmUpcomingAmountReview(item)}>
                                                        Update amount
                                                    </button>
                                                {/if}
                                                <button type="button" disabled={upcomingActionKey === upcomingItemKey(item)} on:click|stopPropagation={() => dismissUpcomingAmountReview(item)}>
                                                    Dismiss
                                                </button>
                                            </div>
                                        {/if}

                                        {#if upcomingEditKey === upcomingItemKey(item)}
                                            <div class="dashboard-upcoming-edit-grid">
                                                <label>
                                                    <span>Group</span>
                                                    <select bind:value={upcomingEditDraft.category} on:click|stopPropagation on:mousedown|stopPropagation>
                                                        {#each upcomingCategoryOptions as option}
                                                            <option value={option}>{option}</option>
                                                        {/each}
                                                    </select>
                                                </label>
                                                <label>
                                                    <span>Cadence</span>
                                                    <select bind:value={upcomingEditDraft.frequency} on:click|stopPropagation on:mousedown|stopPropagation>
                                                        {#each upcomingFrequencyOptions as option}
                                                            <option value={option.key}>{option.label}</option>
                                                        {/each}
                                                    </select>
                                                </label>
                                                <label>
                                                    <span>Expected day</span>
                                                    <input type="number" min="1" max="31" bind:value={upcomingEditDraft.expectedDay} on:click|stopPropagation on:mousedown|stopPropagation>
                                                </label>
                                                <label>
                                                    <span>Amount</span>
                                                    <input type="number" min="0" step="0.01" bind:value={upcomingEditDraft.amount} on:click|stopPropagation on:mousedown|stopPropagation>
                                                </label>
                                            </div>
                                            <div class="dashboard-upcoming-actions">
                                                <button type="button" disabled={upcomingActionKey === upcomingItemKey(item)} on:click|stopPropagation={() => saveUpcomingEdit(item)}>Save</button>
                                                <button type="button" on:click|stopPropagation={() => upcomingEditKey = ''}>Cancel</button>
                                            </div>
                                        {:else if isUpcomingCandidate(item)}
                                            <div class="dashboard-upcoming-actions">
                                                <button type="button" disabled={upcomingActionKey === upcomingItemKey(item)} on:click|stopPropagation={() => confirmUpcomingCandidate(item)}>Confirm</button>
                                                <button type="button" disabled={upcomingActionKey === upcomingItemKey(item)} on:click|stopPropagation={() => dismissUpcomingCandidate(item)}>Dismiss</button>
                                            </div>
                                        {:else if isUpcomingEditable(item)}
                                            <div class="dashboard-upcoming-actions">
                                                <button type="button" on:click|stopPropagation={() => startUpcomingEdit(item)}>Edit</button>
                                                <button type="button" disabled={upcomingActionKey === upcomingItemKey(item)} on:click|stopPropagation={() => deactivateUpcomingItem(item)}>Deactivate</button>
                                            </div>
                                        {/if}
                                    </div>
                                {/if}
                            </div>
                        {/each}
                    </div>
                    {/if}
                {/if}
            </div>
        </section>
    {/if}

    {#if investmentSummary && investmentSummary.holding_count > 0}
        <section class="mb-6 fade-in-up" style="animation-delay: 97ms">
            <div class="dashboard-plan-strip card" style="border-color: color-mix(in srgb, var(--positive) 22%, var(--card-border));">
                <div>
                    <p class="text-[10px] font-bold tracking-[0.14em] uppercase" style="color: var(--positive)">Investments</p>
                    <h3 class="folio-section-title" style="margin:0.15rem 0 0">{(void privacyKey, formatCurrency(investmentSummary.total_value || 0))}</h3>
                    <p class="text-xs mt-1" style="color: var(--text-muted)">
                        {investmentSummary.holding_count} manual holding{investmentSummary.holding_count === 1 ? '' : 's'} · prices are local/manual
                    </p>
                </div>
                <div class="dashboard-plan-metrics">
                    <div>
                        <span>Cost basis</span>
                        <strong>{(void privacyKey, formatCurrency(investmentSummary.total_cost || 0))}</strong>
                    </div>
                    <div>
                        <span>Gain / loss</span>
                        <strong class:text-positive={(investmentSummary.gain_loss || 0) >= 0} class:text-negative={(investmentSummary.gain_loss || 0) < 0}>
                            {(void privacyKey, formatCurrency(investmentSummary.gain_loss || 0))}
                        </strong>
                    </div>
                    <div>
                        <span>Return</span>
                        <strong>{investmentSummary.gain_loss_percent == null ? '—' : `${investmentSummary.gain_loss_percent.toFixed(1)}%`}</strong>
                    </div>
                </div>
                <div class="grid gap-2" style="min-width: min(360px, 100%);">
                    {#each investmentAllocation as bucket}
                        <div class="flex items-center gap-2 text-xs">
                            <span style="width: 78px; color: var(--text-muted)">{bucket.label}</span>
                            <div style="height: 7px; flex: 1; border-radius: 999px; background: var(--surface-100); overflow: hidden;">
                                <div style="height: 100%; width: {Math.max(3, Math.min(100, bucket.actual_percent || 0))}%; background: var(--positive);"></div>
                            </div>
                            <strong class="font-mono" style="width: 48px; text-align: right; color: var(--text-primary)">{(bucket.actual_percent || 0).toFixed(1)}%</strong>
                        </div>
                    {/each}
                </div>
                <a href="/control-center?tab=investments" class="dashboard-plan-link">Update holdings</a>
            </div>
        </section>
    {/if}

    <!-- â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
         S2: COMPACT METRIC RIBBON
         ═══════════════════════════════════════════════════════ -->
    <section class="dashboard-income-section mb-5 fade-in-up" style="animation-delay: 100ms">
        <div class="flex flex-col gap-2 mb-2 sm:flex-row sm:items-center sm:justify-between">
            <h3 class="folio-section-title dashboard-income-title">Income & Spending</h3>
            <div class="flex flex-col items-stretch gap-2 sm:flex-row sm:items-center">
                <div class="period-toggle-track" style="--seg-count: {periodOptions.length}; --active-idx: {activePeriodIdx};">
                    <div class="period-toggle-thumb"></div>
                    {#each periodOptions as p}
                        <button class="period-toggle-label" class:active={selectedPeriod === p.key}
                            on:click={() => { selectedPeriod = p.key; }}>
                            {p.label}
                        </button>
                    {/each}
                </div>
                <div class="month-dropdown-wrapper">
                    <button
                        class="month-dropdown-trigger"
                        class:ring-2={selectedPeriod === 'custom'}
                        class:ring-accent={selectedPeriod === 'custom'}
                        on:click={() => monthDropdownOpen = !monthDropdownOpen}
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
                        <div class="month-dropdown-menu" role="listbox">
                            {#each allMonths as m}
                                <button
                                    class="month-dropdown-item"
                                    class:month-dropdown-item-active={selectedCustomMonth === m}
                                    role="option"
                                    aria-selected={selectedCustomMonth === m}
                                    on:click={() => {
                                        selectedCustomMonth = m;
                                        monthDropdownOpen = false;
                                        handleCustomMonthChange();
                                    }}
                                >
                                    {formatMonth(m)}
                                </button>
                            {/each}
                        </div>
                    {/if}
                </div>
            </div>
        </div>

        {#if periodSummary}
            <div class="metric-ribbon fade-in" class:period-updating={metricLoading}>
                {#if metricLoading}
                    <div class="shimmer-overlay"></div>
                {/if}
                <div class="metric-ribbon-item">
                    <span class="metric-ribbon-label">Income</span>
                    <span class="metric-ribbon-value text-positive">
                        {formatCurrency(animationStarted ? (animationDone ? periodSummary.income : animatedIncome) : (periodSummary?.income ?? 0))}
                    </span>
                    {#if selectedPeriod === 'this_month' && incomeDelta !== null}
                        <span class="metric-ribbon-sub">
                            <span class="delta-badge {incomeDelta >= 0 ? 'delta-up' : 'delta-down'}">
                                {incomeDelta >= 0 ? '▲' : '▼'} {formatPercent(Math.abs(incomeDelta))}
                            </span>
                        </span>
                    {/if}
                </div>
                {#if (periodSummary.credits_refunds || 0) > 0 || (periodSummary.incoming_transfers || 0) > 0}
                    <div class="metric-ribbon-item metric-derived">
                        <span class="metric-ribbon-label" style="display: flex; align-items: center; gap: 4px;">
                            Credits In
                            <span class="material-symbols-outlined text-[10px]" style="color: var(--text-muted); opacity: 0.5" title="Refunds, credits, and incoming personal transfers. Counted in Net Flow, separate from recurring income.">info</span>
                        </span>
                        <span class="metric-ribbon-value text-positive">
                            +{formatCurrency((periodSummary.credits_refunds || 0) + (periodSummary.incoming_transfers || 0))}
                        </span>
                    </div>
                {/if}
                <div class="metric-ribbon-item">
                    <span class="metric-ribbon-label">Spending</span>
                    <span class="metric-ribbon-value text-negative">
                        {formatCurrency(animationStarted ? (animationDone ? periodSummary.expenses : animatedExpenses) : (periodSummary?.expenses ?? 0))}
                    </span>
                    <span class="metric-ribbon-sub">
                        {#if selectedPeriod === 'this_month' && expenseDelta !== null}
                            <span class="delta-badge {expenseDelta <= 0 ? 'delta-up' : 'delta-down'}">
                                {expenseDelta >= 0 ? '▲' : '▼'} {formatPercent(Math.abs(expenseDelta))}
                            </span>
                        {/if}
                        {#if topCatInsight && selectedPeriod === 'this_month'}
                            <span class="ml-1" style="color: var(--text-muted)">
                                · <span style="color: {CATEGORY_COLORS[topCatInsight.name]}" class="font-semibold">{topCatInsight.name}</span>
                                {formatPercent(topCatInsight.pctOfExpenses)}
                            </span>
                        {/if}
                    </span>
                </div>
                <div class="metric-ribbon-item metric-derived metric-derived-first">
                    <span class="metric-ribbon-label" style="display: flex; align-items: center; gap: 4px;">
                        Ext. Transfers
                        <span class="material-symbols-outlined text-[10px]" style="color: var(--text-muted); opacity: 0.5" title="Transfers to people outside your accounts (Zelle, Venmo, etc). Counted in Net Flow.">info</span>
                    </span>
                    <span class="metric-ribbon-value" style="color: var(--warning)">
                        {formatCurrency(periodSummary.external_transfers || 0)}
                    </span>
                </div>
                <div class="metric-ribbon-item metric-derived">
                    <span class="metric-ribbon-label" style="display: flex; align-items: center; gap: 4px;">
                        CC Repaid
                        <span class="material-symbols-outlined text-[10px]" style="color: var(--text-muted); opacity: 0.5" title="Payments toward prior credit card charges. Not counted as new spending.">info</span>
                    </span>
                    <span class="metric-ribbon-value" style="color: #8B5CF6">
                        {formatCurrency(periodSummary.cc_repaid || 0)}
                    </span>
                </div>
                <div class="metric-ribbon-item metric-derived">
                    <span class="metric-ribbon-label">Net Flow</span>
                    <span class="metric-ribbon-value" style="color: {periodSummary.net_flow >= 0 ? 'var(--positive)' : 'var(--negative)'}">
                        {periodSummary.net_flow >= 0 ? '+' : ''}{formatCurrency(periodSummary.net_flow)}
                    </span>
                    {#if dailyPace}
                        <span class="metric-ribbon-sub">
                            {formatCurrency(dailyPace.dailyAvg)}/day → {formatCurrency(dailyPace.projected)} projected
                        </span>
                    {/if}
                </div>
            </div>
        {/if}
    </section>

    <!-- ═══════════════════════════════════════════════════════
         S3: MONEY FLOW (SANKEY)
         ═══════════════════════════════════════════════════════ -->
    <section class="mb-8 fade-in-up" style="animation-delay: 140ms">
        <div class="flex items-center justify-between mb-3">
            <div class="flex items-center gap-2">
                <h3 class="folio-section-title">Money Flow</h3>
                {#if debugLoadingMode}
                    <span class="text-[9px] font-semibold px-2 py-0.5 rounded-md"
                        style="background: rgba(59, 130, 246, 0.10); color: var(--accent); border: 1px solid rgba(59, 130, 246, 0.18);">
                        Debug loading: {debugLoadingMode}
                    </span>
                {/if}
                {#if periodSummary && periodSummary.income < periodSummary.expenses + sankeySavingsTotal + sankeyPersonalTransferTotal}
                    <span class="text-[9px] font-semibold px-2 py-0.5 rounded-md"
                        style="background: rgba(251, 191, 36, 0.12); color: var(--warning); border: 1px solid rgba(251, 191, 36, 0.20);">
                        Drawing from balance
                    </span>
                {/if}
            </div>
            {#if selectedSankeyCategory}
                <button on:click={() => { selectedSankeyCategory = null; sankeyDrillTxns = []; }}
                    class="flex items-center gap-1 text-[11px] px-3 py-1.5 rounded-lg hover:opacity-80 transition-opacity"
                    style="background: var(--surface-100); color: var(--text-secondary)">
                    <span class="material-symbols-outlined text-[14px]">close</span> Clear
                </button>
            {/if}
        </div>

        <div class="sankey-theater dashboard-sankey-theater"
             role="region"
             aria-label="Cash flow Sankey chart"
             class:sankey-loading={sankeyLoading}
             bind:this={sankeyTheaterEl}
             on:mousemove={handleTheaterMouseMove}
             on:mouseleave={handleTheaterMouseLeave}>
            {#if showSankeyLoadingStage}
                <div class="shimmer-overlay"></div>
            {/if}
            <div class="sankey-theater-glow" style="opacity: {sankeyGlowOpacity};"></div>
            {#if showSankeyLoadingStage}
                <SankeyLoadingStage height={320} />
            {/if}
            {#if showSankeyEmptyState}
                <div class="sankey-empty-state" style="height: 320px;">
                    No money flow for this period
                </div>
            {/if}
            {#if showSankeyChart}
                <div class="sankey-chart-shell">
                    <SankeyChart
                        income={periodSummary.income}
                        expenses={periodSummary.expenses}
                        creditsRefunds={periodSummary.credits_refunds || 0}
                        incomingTransfers={periodSummary.incoming_transfers || 0}
                        cashDeposits={periodSummary.cash_deposits || 0}
                        cashWithdrawals={periodSummary.cash_withdrawals || 0}
                        investmentInflows={periodSummary.investment_inflows || 0}
                        investmentOutflows={periodSummary.investment_outflows || 0}
                        savingsTransfer={sankeySavingsTotal}
                        personalTransfer={sankeyPersonalTransferTotal}
                        ccRepaid={periodSummary.cc_repaid || 0}
                        categories={sankeyCategoryList.filter(c => !c.isDirectFlow)}
                        selectedCategory={selectedSankeyCategory}
                        height={320}
                        autoHeight={true}
                        on:select={handleSankeySelect}
                    />
                </div>
            {/if}
            {#if showLiveSankeySyncState}
                <SankeyLoadingStage mode="sync" height={320} eyebrow="Syncing money flow" />
            {/if}
        </div>

        {#if sankeyPillList.length > 0}
            <div class="sankey-pill-card">
                <div class="sankey-pill-card-header">
                    <span class="material-symbols-outlined text-[13px]" style="color: var(--text-muted); opacity: 0.7">category</span>
                    <span class="sankey-pill-card-title">Categories</span>
                    <span class="sankey-pill-card-count">{sankeyPillList.slice(0, 12).length} flows</span>
                </div>
                <div bind:this={drillDownSection} class="sankey-pill-grid">
                    {#each sankeyPillList.slice(0, 12) as cat}
                        {@const catColor = getSankeyFlowColor(cat.category)}
                        {@const isActive = selectedSankeyCategory === cat.category}
                        {@const isDimmed = selectedSankeyCategory && !isActive}
                        <button
                            on:click={() => handleSankeySelect({ detail: isActive ? null : cat.category })}
                            class="sankey-cat-pill"
                            class:pill-active={isActive}
                            class:pill-dimmed={isDimmed}
                            style="--pill-color: {catColor};
                                   color: {catColor};
                                   background: color-mix(in srgb, {catColor} {isActive ? 'var(--pill-bg-active)' : '6'}%, transparent);
                                   border-color: color-mix(in srgb, {catColor} {isActive ? 'var(--pill-border-active)' : '12'}%, transparent);">
                            <span class="material-symbols-outlined text-[13px]">{getSankeyFlowIcon(cat.category)}</span>
                            {cat.category}
                            <span class="sankey-cat-pill-value">{formatCompact(cat.total)}</span>
                            {#if cat.isDirectFlow}
                                <span class="sankey-cat-pill-direct">Ã¢</span>
                            {/if}
                        </button>
                    {/each}
                </div>
            </div>
        {/if}

        <!-- Drill-down transactions -->
        <div style="position: relative; z-index: 50;">
            {#if selectedSankeyCategory && sankeyDrillTxns.length > 0}
                {@const drillTotal = getDrillDownTotal(selectedSankeyCategory)}
                {@const isCreditDrilldown = selectedSankeyCategory === CREDIT_DRILLDOWN_CATEGORY}
                <div class="sankey-drill-card card mt-3 fade-in" style="padding: 0; overflow: visible">
                    <div class="sankey-drill-header flex items-center gap-3 px-5 py-2.5" style="border-bottom: 1px solid var(--card-border); background: var(--surface-100)">
                        <div class="w-6 h-6 rounded-lg flex items-center justify-center"
                            style="background: color-mix(in srgb, {getSankeyFlowColor(selectedSankeyCategory)} 15%, transparent)">
                            <span class="material-symbols-outlined text-[14px]" style="color: {getSankeyFlowColor(selectedSankeyCategory)}">
                                {getSankeyFlowIcon(selectedSankeyCategory)}
                            </span>
                        </div>
                        <div>
                            <p class="sankey-drill-heading text-[13px] font-semibold" style="color: var(--text-primary)">{selectedSankeyCategory}</p>
                            <p class="sankey-drill-subtitle text-[10px]" style="color: var(--text-muted)">{sankeyDrillTxns.length} transactions · by amount</p>
                        </div>
                        <p class="ml-auto folio-amount" style="color: {isCreditDrilldown ? 'var(--positive)' : 'var(--negative)'}">{isCreditDrilldown ? '+' : ''}{formatCurrency(drillTotal)}</p>
                    </div>
                    {#if drillUpdateFeedback}
                        <div class="drill-feedback-toast fade-in">
                            <span class="material-symbols-outlined text-[14px]" style="color: var(--positive)">check_circle</span>
                            <span class="text-[11px] font-medium" style="color: var(--text-primary)">{drillUpdateFeedback}</span>
                        </div>
                    {/if}
                    {#each sankeyDrillTxns.slice(0, 10) as tx (tx.original_id)}
                        {@const amount = parseFloat(tx.amount)}
                        {@const txCatColor = CATEGORY_COLORS[tx.category] || '#627d98'}
                        {@const isDrillUpdated = drillRecentlyUpdatedTxId === tx.original_id}
                        <div class="drill-tx-row"
                            class:tx-row-updated={isDrillUpdated}
                            style="border-bottom: 1px solid color-mix(in srgb, var(--card-border) 50%, transparent)">
                            <div class="flex-1 min-w-0">
                                <p class="sankey-drill-title text-[13px] font-medium truncate" style="color: var(--text-primary)">{tx.description}</p>
                                <p class="sankey-drill-meta text-[10px]" style="color: var(--text-muted)">{formatDate(tx.date)} · {tx.account_name}</p>
                            </div>
                            <div class="drill-tx-right">
                                <p class="folio-amount-compact" style="color: {amount >= 0 ? 'var(--positive)' : 'var(--negative)'}">
                                    {amount >= 0 ? '+' : ''}{formatCurrency(amount, 2)}
                                </p>
                                {#if amount > 0 && (selectedSankeyCategory === 'Savings Transfer' || selectedSankeyCategory === 'Personal Transfer')}
                                    <span class="text-[8px] font-semibold px-1.5 py-0.5 rounded-full" style="background: color-mix(in srgb, var(--text-muted) 12%, transparent); color: var(--text-muted)">MIRROR</span>
                                {:else if amount > 0}
                                    <span class="text-[8px] font-semibold px-1.5 py-0.5 rounded-full" style="background: var(--positive-light); color: var(--positive)">REFUND</span>
                                {:else if selectedSankeyCategory === 'Savings Transfer' || selectedSankeyCategory === 'Personal Transfer'}
                                    <span class="text-[8px] font-semibold px-1.5 py-0.5 rounded-full" style="background: color-mix(in srgb, #3b82f6 12%, transparent); color: #3b82f6">OUTFLOW</span>
                                {/if}
                                {#if tx.expense_type === 'transfer_internal'}
                                    <span class="text-[8px] font-semibold px-1.5 py-0.5 rounded-full" style="background: color-mix(in srgb, var(--text-muted) 10%, transparent); color: var(--text-muted)">INTERNAL</span>
                                {:else if tx.expense_type === 'transfer_household'}
                                    <span class="text-[8px] font-semibold px-1.5 py-0.5 rounded-full" style="background: color-mix(in srgb, #8b5cf6 12%, transparent); color: #8b5cf6">HOUSEHOLD</span>
                                {:else if tx.expense_type === 'transfer_external'}
                                    <span class="text-[8px] font-semibold px-1.5 py-0.5 rounded-full" style="background: color-mix(in srgb, #f59e0b 12%, transparent); color: #f59e0b">EXTERNAL</span>
                                {/if}

                                <!-- Category re-tag pill -->
                                <!-- svelte-ignore a11y-click-events-have-key-events -->
                                <div class="relative tx-cat-pill-wrapper drill-cat-pill-wrapper" role="presentation" on:click|stopPropagation>
                                    <button
                                        class="tx-cat-pill drill-cat-pill"
                                        class:tx-cat-pill-editing={drillCatDropdownOpenForTx === tx.original_id}
                                        on:click|stopPropagation={() => {
                                            if (drillCatDropdownOpenForTx === tx.original_id) {
                                                cancelDrillEditing();
                                            } else {
                                                startDrillEditing(tx.original_id);
                                            }
                                        }}
                                        style="--pill-color: {txCatColor}">
                                        <span class="material-symbols-outlined text-[11px]" style="color: {txCatColor}">
                                            {CATEGORY_ICONS[tx.category] || 'label'}
                                        </span>
                                        <span class="tx-cat-pill-label" style="max-width: 80px">{tx.category || 'Uncategorized'}</span>
                                        <span class="material-symbols-outlined text-[10px] tx-cat-pill-chevron"
                                            class:txn-chevron-open={drillCatDropdownOpenForTx === tx.original_id}
                                            style="color: var(--text-muted); opacity: 0.5;">
                                            expand_more
                                        </span>
                                    </button>

                                    {#if drillCatDropdownOpenForTx === tx.original_id}
                                        <!-- svelte-ignore a11y-click-events-have-key-events -->
                                        <div class="txn-filter-dropdown tx-cat-dropdown drill-cat-dropdown" role="presentation" on:click|stopPropagation>
                                            <div class="tx-cat-apply-toggle">
                                                <div class="tx-cat-apply-toggle-copy">
                                                    <span class="tx-cat-apply-toggle-label">Apply</span>
                                                </div>
                                                <div class="tx-cat-apply-toggle-actions">
                                                    <button
                                                        class="tx-cat-apply-mode-pill"
                                                        class:tx-cat-apply-mode-pill--active={drillCategoryApplyMode === 'always'}
                                                        on:click={() => { drillCategoryApplyMode = 'always'; }}>
                                                        Always
                                                    </button>
                                                    <button
                                                        class="tx-cat-apply-mode-pill"
                                                        class:tx-cat-apply-mode-pill--active={drillCategoryApplyMode === 'once'}
                                                        on:click={() => { drillCategoryApplyMode = 'once'; }}>
                                                        Just once
                                                    </button>
                                                </div>
                                            </div>
                                            <div class="tx-cat-dropdown-search-wrap">
                                                <span class="material-symbols-outlined text-[14px]" style="color: var(--text-muted)">search</span>
                                                <input
                                                    bind:value={drillCatDropdownSearch}
                                                    placeholder="Search categories..."
                                                    class="tx-cat-dropdown-search"
                                                    on:keydown={(e) => {
                                                        if (e.key === 'Escape') cancelDrillEditing();
                                                    }}
                                                />
                                            </div>
                                            <div class="tx-cat-dropdown-list">
                                                {#each drillFilteredCategories as cat}
                                                    <button
                                                        class="txn-filter-option"
                                                        class:active={cat === tx.category}
                                                        on:click={() => {
                                                            if (cat !== tx.category) {
                                                                drillUpdateCategory(tx.original_id, cat, drillCategoryApplyMode === 'once');
                                                            } else {
                                                                cancelDrillEditing();
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
                                                {#if drillFilteredCategories.length === 0 && drillCatDropdownSearch}
                                                    <div class="px-3 py-2 text-[11px]" style="color: var(--text-muted)">
                                                        No matching categories
                                                    </div>
                                                {/if}
                                            </div>
                                            <div class="tx-cat-dropdown-footer">
                                                {#if drillCreatingNewCategory}
                                                    <div class="flex items-center gap-1.5 px-2 py-1.5">
                                                        <input
                                                            bind:value={drillNewCategoryName}
                                                            placeholder="New category name..."
                                                            class="tx-cat-dropdown-new-input"
                                                            on:keydown={(e) => {
                                                                if (e.key === 'Enter') drillCreateAndApplyCategory(tx.original_id);
                                                                if (e.key === 'Escape') { drillCreatingNewCategory = false; drillNewCategoryName = ''; drillNewCategoryError = ''; }
                                                            }}
                                                        />
                                                        <button
                                                            class="tx-edit-btn tx-edit-btn-confirm"
                                                            on:click={() => drillCreateAndApplyCategory(tx.original_id)}
                                                            disabled={!drillNewCategoryName.trim()}>
                                                            <span class="material-symbols-outlined text-[13px]">check</span>
                                                        </button>
                                                    </div>
                                                    {#if drillNewCategoryError}
                                                        <span class="text-[9px] px-3" style="color: var(--negative)">{drillNewCategoryError}</span>
                                                    {/if}
                                                {:else}
                                                    <button
                                                        class="txn-filter-option tx-cat-create-btn"
                                                        on:click={() => { drillCreatingNewCategory = true; }}>
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
                            </div>
                        </div>
                    {/each}
                    {#if sankeyDrillTxns.length > 10}
                        <div class="px-5 py-2.5 text-center">
                            <a href="/transactions" class="text-[11px] font-medium" style="color: var(--accent)">
                                View all {sankeyDrillTxns.length} transactions →
                            </a>
                        </div>
                    {/if}
                </div>
            {:else if selectedSankeyCategory && sankeyDrillTxns.length === 0}
                <div class="card mt-3 fade-in">
                    <p class="text-sm text-center py-3" style="color: var(--text-muted)">No transactions found.</p>
                </div>
            {/if}
        </div>
    </section>

    <!-- ── Expense type feedback toast ── -->
    {#if expenseTypeFeedback}
        <div class="analytics-fv-toast fade-in">
            <span class="material-symbols-outlined text-[16px]" style="color: var(--positive)">check_circle</span>
            <span class="text-[12px] font-medium" style="color: var(--text-primary)">{expenseTypeFeedback}</span>
        </div>
    {/if}

    <!-- âââââââââââââââââââââââââââââââââââââââââââââââââââââââ
         FIXED vs VARIABLE SPENDING
         âââââââââââââââââââââââââââââââââââââââââââââââââââââââ -->
    {#if fixedVsVariable && fixedVsVariable.grandTotal > 0}
        <section class="mb-10 fade-in-up" style="animation-delay: 155ms">
            <div class="card analytics-fv-card analytics-fv-stage">
                <div class="analytics-fv-card-header">
                    <div>
                        <p>{getMonthForPeriod(selectedPeriod) ? formatMonth(getMonthForPeriod(selectedPeriod)) : 'Selected period'} · Spending split</p>
                        <h3 class="folio-section-title">Fixed vs variable</h3>
                    </div>
                    <span class="analytics-fv-reclassify-hint">
                        <span class="material-symbols-outlined">swap_horiz</span>
                        Click any category to reclassify
                    </span>
                </div>

                <div class="analytics-fv-totals">
                    <div>
                        <span><i class="analytics-fv-dot analytics-fv-dot-fixed"></i>Fixed</span>
                        <strong>{formatCurrency(fixedVsVariable.fixedTotal)}</strong>
                        <small>
                            {formatPercent(fixedVsVariable.fixedPct)} of spending ·
                            {fixedVsVariable.fixedDeltaPct > 0 ? 'up' : fixedVsVariable.fixedDeltaPct < 0 ? 'down' : 'flat'}
                            {formatPercent(Math.abs(fixedVsVariable.fixedDeltaPct))} vs avg
                        </small>
                    </div>
                    <div>
                        <span><i class="analytics-fv-dot analytics-fv-dot-variable"></i>Variable</span>
                        <strong>{formatCurrency(fixedVsVariable.variableTotal)}</strong>
                        <small>
                            {formatPercent(fixedVsVariable.variablePct)} of spending ·
                            {fixedVsVariable.variableDeltaPct > 0 ? 'up' : fixedVsVariable.variableDeltaPct < 0 ? 'down' : 'flat'}
                            {formatPercent(Math.abs(fixedVsVariable.variableDeltaPct))} vs avg
                        </small>
                    </div>
                </div>

                <div class="analytics-fv-stacked-bar" aria-label="Fixed and variable spending split">
                    {#each fixedVsVariable.fixedCats as cat}
                        <button
                            type="button"
                            class="analytics-fv-bar-segment analytics-fv-bar-fixed"
                            style="width: {Math.max(4, (cat.total / fixedVsVariable.grandTotal) * 100)}%;"
                            title="{cat.category} · {formatCurrency(cat.total)}"
                            on:click|stopPropagation={() => startEditingExpenseType(cat.category)}>
                            <span>{cat.category}</span>
                            <small>{formatCurrency(cat.total)}</small>
                        </button>
                    {/each}
                    {#each fixedVsVariable.variableCats as cat}
                        <button
                            type="button"
                            class="analytics-fv-bar-segment analytics-fv-bar-variable"
                            style="width: {Math.max(4, (cat.total / fixedVsVariable.grandTotal) * 100)}%;"
                            title="{cat.category} · {formatCurrency(cat.total)}"
                            on:click|stopPropagation={() => startEditingExpenseType(cat.category)}>
                            <span>{cat.category}</span>
                            <small>{formatCurrency(cat.total)}</small>
                        </button>
                    {/each}
                </div>
                <div class="analytics-fv-bar-foot">
                    <span>Fixed · {formatPercent(fixedVsVariable.fixedPct)}</span>
                    <span>Flexible split at {formatCurrency(fixedVsVariable.fixedTotal)}</span>
                    <span>Variable · {formatPercent(fixedVsVariable.variablePct)}</span>
                </div>

                <div class="analytics-fv-split">
                    <!-- Fixed side -->
                    <div class="analytics-fv-column">
                        <div class="analytics-fv-column-header">
                            <span>Fixed · recurring</span>
                            <strong>{formatCurrency(fixedVsVariable.fixedTotal)} · {formatPercent(fixedVsVariable.fixedPct)}</strong>
                        </div>
                        <div class="analytics-fv-row-scroll">
                            {#each fixedVsVariable.fixedCats as cat}
                                <div class="analytics-fv-row">
                                    {#if editingExpenseType === cat.category}
                                        <div class="analytics-fv-toggle-controls">
                                            <span class="analytics-fv-edit-label">{cat.category}</span>
                                            <div class="analytics-fv-toggle-btns">
                                                <button type="button" class="analytics-fv-toggle-btn analytics-fv-toggle-active"
                                                    on:click|stopPropagation={() => cancelEditingExpenseType()}>Fixed</button>
                                                <button type="button" class="analytics-fv-toggle-btn"
                                                    on:click|stopPropagation={() => toggleExpenseType(cat.category, 'variable')}>Variable</button>
                                            </div>
                                        </div>
                                    {:else}
                                        <button
                                            type="button"
                                            class="analytics-fv-cat-btn"
                                            aria-label="Reclassify {cat.category} as variable"
                                            title="Click to reclassify"
                                            on:click|stopPropagation={() => startEditingExpenseType(cat.category)}>
                                            <span class="analytics-fv-cat-label text-[11px]">{cat.category}</span>
                                            <span class="analytics-fv-row-action">Move to Variable</span>
                                        </button>
                                        <span class="analytics-fv-row-amount">{formatCurrency(cat.total)}</span>
                                    {/if}
                                </div>
                            {/each}
                        </div>
                        {#if fixedVsVariable.fixedCats.length === 0}
                            <p class="text-[10px]" style="color: var(--text-muted)">No fixed expenses detected</p>
                        {/if}
                    </div>

                    <!-- Divider -->
                    <div class="analytics-fv-divider"></div>

                    <!-- Variable side -->
                    <div class="analytics-fv-column">
                        <div class="analytics-fv-column-header">
                            <span>Variable · discretionary</span>
                            <strong>{formatCurrency(fixedVsVariable.variableTotal)} · {formatPercent(fixedVsVariable.variablePct)}</strong>
                        </div>
                        <div class="analytics-fv-row-scroll">
                            {#each fixedVsVariable.variableCats as cat}
                                <div class="analytics-fv-row">
                                    {#if editingExpenseType === cat.category}
                                        <div class="analytics-fv-toggle-controls">
                                            <span class="analytics-fv-edit-label">{cat.category}</span>
                                            <div class="analytics-fv-toggle-btns">
                                                <button type="button" class="analytics-fv-toggle-btn"
                                                    on:click|stopPropagation={() => toggleExpenseType(cat.category, 'fixed')}>Fixed</button>
                                                <button type="button" class="analytics-fv-toggle-btn analytics-fv-toggle-active"
                                                    on:click|stopPropagation={() => cancelEditingExpenseType()}>Variable</button>
                                            </div>
                                        </div>
                                    {:else}
                                        <button
                                            type="button"
                                            class="analytics-fv-cat-btn"
                                            aria-label="Reclassify {cat.category} as fixed"
                                            title="Click to reclassify"
                                            on:click|stopPropagation={() => startEditingExpenseType(cat.category)}>
                                            <span class="analytics-fv-cat-label text-[11px]">{cat.category}</span>
                                            <span class="analytics-fv-row-action">Move to Fixed</span>
                                        </button>
                                        <span class="analytics-fv-row-amount">{formatCurrency(cat.total)}</span>
                                    {/if}
                                </div>
                            {/each}
                        </div>
                    </div>
                </div>

                <!-- Insight footer -->
                <div class="analytics-fv-insight">
                    <span class="material-symbols-outlined text-[14px]">lightbulb</span>
                    <p>
                        Your fixed costs are <strong>{formatPercent(fixedVsVariable.fixedPct)} of spending</strong>.
                        {#if fixedVsVariable.variablePct > 50}
                            You have significant room to optimize discretionary spend.
                        {:else}
                            Most of your budget is committed, focus on renegotiating recurring costs.
                        {/if}
                    </p>
                </div>
            </div>
        </section>
    {/if}
    
    <!-- ═══════════════════════════════════════════════════════
         S4b: INCOME vs SPENDING (Luminous Overlap)
         ═══════════════════════════════════════════════════════ -->
    {#if monthly && monthly.length >= 2}
    <section class="mb-10 fade-in-up" style="animation-delay: 170ms">
        <div class="flex items-center justify-between mb-4">
            <div class="flex items-center gap-2">
                <h3 class="folio-section-title">Income vs Spending</h3>
                <span class="section-zone-label">Last 12 Months</span>
            </div>
        </div>

        <div class="ivs-theater">
            <IncomeVsSpendingChart monthlyData={monthly} height={400} categoryData={monthlyCategoryBreakdown} />
        </div>


        {#if ivsStats}
            <div class="ivs-stats">
                <div class="ivs-stat">
                    <div class="ivs-stat-icon" style="background: rgba(56, 189, 248, 0.10); color: #38BDF8;">
                        <span class="material-symbols-outlined text-[13px]">trending_up</span>
                    </div>
                    <div class="ivs-stat-content">
                        <p class="ivs-stat-label">Avg Income</p>
                        <p class="ivs-stat-value" style="color: #38BDF8">{(void privacyKey, formatCurrency(ivsStats.avgIncome))}</p>
                        <p class="ivs-stat-sub">/month over {ivsStats.monthCount}mo</p>
                    </div>
                </div>
                <div class="ivs-stat">
                    <div class="ivs-stat-icon" style="background: rgba(244, 114, 182, 0.10); color: #F472B6;">
                        <span class="material-symbols-outlined text-[13px]">trending_down</span>
                    </div>
                    <div class="ivs-stat-content">
                        <p class="ivs-stat-label">Avg Spending</p>
                        <p class="ivs-stat-value" style="color: #F472B6">{(void privacyKey, formatCurrency(ivsStats.avgSpending))}</p>
                        <p class="ivs-stat-sub">/month over {ivsStats.monthCount}mo</p>
                    </div>
                </div>
                <div class="ivs-stat">
                    <div class="ivs-stat-icon" style="background: {ivsStats.avgNet >= 0 ? 'var(--positive-light)' : 'var(--negative-light)'}; color: {ivsStats.avgNet >= 0 ? 'var(--positive)' : 'var(--negative)'};">
                        <span class="material-symbols-outlined text-[13px]">equalizer</span>
                    </div>
                    <div class="ivs-stat-content">
                        <p class="ivs-stat-label">Avg Net</p>
                        <p class="ivs-stat-value" style="color: {ivsStats.avgNet >= 0 ? 'var(--positive)' : 'var(--negative)'}">
                            {ivsStats.avgNet >= 0 ? '+' : ''}{(void privacyKey, formatCurrency(ivsStats.avgNet))}
                        </p>
                        <p class="ivs-stat-sub">/month</p>
                    </div>
                </div>
                <div class="ivs-stat">
                    <div class="ivs-stat-icon" style="background: rgba(52, 211, 153, 0.10); color: #34d399;">
                        <span class="material-symbols-outlined text-[13px]">emoji_events</span>
                    </div>
                    <div class="ivs-stat-content">
                        <p class="ivs-stat-label">Best Month</p>
                        <p class="ivs-stat-value" style="color: var(--positive)">
                            +{(void privacyKey, formatCurrency(ivsStats.bestSavingsVal))}
                        </p>
                        <p class="ivs-stat-sub">{formatMonthShort(ivsStats.bestSavingsMonth)}</p>
                    </div>
                </div>
                <div class="ivs-stat">
                    <div class="ivs-stat-icon" style="background: color-mix(in srgb, var(--accent) 10%, transparent); color: var(--accent);">
                        <span class="material-symbols-outlined text-[13px]">bar_chart</span>
                    </div>
                    <div class="ivs-stat-content">
                        <p class="ivs-stat-label">Surplus / Deficit</p>
                        <p class="ivs-stat-value" style="color: var(--text-primary)">
                            <span style="color: var(--positive)">{ivsChartData.filter(d => d.net >= 0).length}</span>
                            <span style="color: var(--text-muted); font-size: 0.6875rem"> / </span>
                            <span style="color: var(--negative)">{ivsChartData.filter(d => d.net < 0).length}</span>
                            <span class="ivs-stat-sub" style="display: inline; margin-left: 2px;">months</span>
                        </p>
                    </div>
                </div>
            </div>
        {/if}
    </section>

    {/if}

    <TellerConnect
        bind:this={tellerConnectRef}
        applicationId={tellerAppId}
        environment={tellerEnvironment}
        renderButton={false}
        on:enrolled={handleTellerEnrolled}
        on:error={(e) => console.warn('Teller Connect error:', e.detail)}
    />
    <SimpleFINConnect
        bind:this={simplefinConnectRef}
        on:connected={handleSimpleFINConnected}
    />

    {#if connectionChooserOpen}
        <!-- svelte-ignore a11y-click-events-have-key-events a11y-no-static-element-interactions -->
        <div class="bank-chooser-backdrop" on:click={handleConnectionChooserBackdrop}>
            <div class="bank-chooser-modal" role="dialog" aria-modal="true" aria-label="Choose bank connection provider">
                <div class="bank-chooser-header">
                    <h3>Add Bank Connection</h3>
                    <button class="bank-chooser-close" on:click={closeConnectionChooser} aria-label="Close">
                        <span class="material-symbols-outlined">close</span>
                    </button>
                </div>

                <div class="bank-chooser-body">
                    <p class="bank-chooser-intro">
                        {appConfig.demoMode
                            ? 'Preview the available connection methods. Bank linking is disabled in the public demo.'
                            : 'Choose how you want to connect your bank account.'}
                    </p>

                    <button class="bank-chooser-option" on:click={launchTellerFromChooser} disabled={appConfig.demoMode || !tellerAppId}>
                        <div class="bank-chooser-option-copy">
                            <strong>Teller</strong>
                            <span>{appConfig.demoMode ? 'Available in the full app, but disabled in demo mode.' : tellerAppId ? 'Launch the guided Teller Connect flow.' : 'Teller Connect is not configured in this environment.'}</span>
                        </div>
                        <span class="material-symbols-outlined bank-chooser-option-icon">chevron_right</span>
                    </button>

                    <button class="bank-chooser-option" on:click={launchSimpleFINFromChooser} disabled={appConfig.demoMode}>
                        <div class="bank-chooser-option-copy">
                            <strong>SimpleFIN</strong>
                            <span>{appConfig.demoMode ? 'Available in the full app, but disabled in demo mode.' : 'Paste a setup token and choose a Folio profile.'}</span>
                        </div>
                        <span class="material-symbols-outlined bank-chooser-option-icon">chevron_right</span>
                    </button>
                </div>
            </div>
        </div>
    {/if}
</div>
{/if}

<style>
    .bank-chooser-backdrop {
        position: fixed;
        inset: 0;
        z-index: 5000;
        display: flex;
        align-items: flex-start;
        justify-content: center;
        padding: clamp(72px, 11vh, 128px) 16px 24px;
        background: rgba(8, 12, 24, 0.56);
        backdrop-filter: blur(10px);
        -webkit-backdrop-filter: blur(10px);
        overflow-y: auto;
    }

    .bank-chooser-modal {
        width: min(420px, calc(100vw - 32px));
        background: var(--card-bg);
        border: 1px solid var(--card-border);
        border-radius: 18px;
        box-shadow: 0 28px 72px rgba(15, 23, 42, 0.3);
        overflow: hidden;
    }

    .bank-chooser-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 16px 18px;
        border-bottom: 1px solid var(--card-border);
    }

    .bank-chooser-header h3 {
        margin: 0;
        font-size: 15px;
        font-weight: 600;
        color: var(--text-primary);
    }

    .bank-chooser-close {
        display: flex;
        align-items: center;
        justify-content: center;
        width: 30px;
        height: 30px;
        border: none;
        border-radius: 9px;
        background: transparent;
        color: var(--text-secondary);
        cursor: pointer;
    }

    .bank-chooser-close:hover {
        background: var(--hover-bg);
        color: var(--text-primary);
    }

    .bank-chooser-body {
        display: flex;
        flex-direction: column;
        gap: 12px;
        padding: 18px;
    }

    .bank-chooser-intro {
        margin: 0;
        color: var(--text-secondary);
        font-size: 13px;
        line-height: 1.5;
    }

    .bank-chooser-option {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        width: 100%;
        padding: 14px 15px;
        border-radius: 12px;
        border: 1px solid var(--card-border);
        background: var(--bg-level-1, var(--hover-bg));
        color: var(--text-primary);
        cursor: pointer;
        text-align: left;
        transition: all 0.18s ease;
    }

    .bank-chooser-option:hover:not(:disabled) {
        border-color: color-mix(in srgb, var(--accent) 28%, var(--card-border));
        background: color-mix(in srgb, var(--accent) 8%, var(--bg-level-1, var(--hover-bg)));
    }

    .bank-chooser-option:disabled {
        opacity: 0.55;
        cursor: not-allowed;
    }

    .bank-chooser-option-copy {
        display: flex;
        flex-direction: column;
        gap: 4px;
        min-width: 0;
        flex: 1;
    }

    .bank-chooser-option-copy strong {
        font-size: 13px;
        font-weight: 600;
    }

    .bank-chooser-option-copy span {
        color: var(--text-secondary);
        font-size: 12px;
        line-height: 1.45;
    }

    .bank-chooser-option-icon {
        font-size: 18px;
        color: var(--text-muted);
        flex-shrink: 0;
    }

    .dashboard-hero-header {
        display: flex;
        flex-direction: column;
        gap: 10px;
    }

    .dashboard-hero-meta-row {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        min-width: 0;
    }

    .dashboard-hero-greeting {
        margin: 0;
        flex-shrink: 0;
    }

    .dashboard-hero-title {
        white-space: nowrap;
        margin: 0;
    }

    .dashboard-hero-title-mobile {
        display: none;
    }

    .dashboard-hero-controls {
        display: flex;
        align-items: center;
        gap: 8px;
        flex-shrink: 0;
        min-width: 0;
    }

    @media (max-width: 768px) {
        .dashboard-hero-title {
            font-size: clamp(1.7rem, 5.9vw, 2rem);
            line-height: 1.04;
        }

        .dashboard-hero-title-desktop {
            display: none;
        }

        .dashboard-hero-title-mobile {
            display: inline;
        }

        .dashboard-hero-controls {
            margin-left: auto;
            max-width: calc(100% - 118px);
            gap: 4px;
            justify-content: flex-end;
        }

        .dashboard-hero-greeting {
            letter-spacing: 0.14em;
        }
    }
</style>
