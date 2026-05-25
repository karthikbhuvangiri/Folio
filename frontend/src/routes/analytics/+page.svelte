<script>
    import '$lib/styles/analytics.css';
    import { onMount, tick } from 'svelte';
    import { api, invalidateCache  } from '$lib/api.js';
    import { darkMode, selectedPeriodStore, selectedCustomMonthStore } from '$lib/stores.js';
    import { activeProfile } from '$lib/stores/profileStore.js';
    import {
        formatCurrency, formatCompact, formatPercent, formatMonth, formatMonthShort,
        formatDate, formatDateShort, formatDateWithYear, getCurrentMonth, computeDelta,
        CATEGORY_COLORS, CATEGORY_ICONS
    } from '$lib/utils.js';
    import ProfileSwitcher from '$lib/components/ProfileSwitcher.svelte';

    /* ═══════════════════════════════════════
       STATE
       ═══════════════════════════════════════ */
    export let data;

    let monthly = data.monthly;
    let allCategories = Array.isArray(data.categories) ? data.categories : (data.categories?.categories || []);
    let loading = false;
    let profileSwitching = false;
    let selectedMonth = '';
    let monthPickerOpen = false;
    let pulseExpanded = false;
    let explainingMonth = false;
    let monthExplanation = null;
    let monthExplanationOpen = false;
    let monthExplanationSections = [];
    let explainedMonth = '';
    let explainedProfile = '';
    let explanationError = '';
    let mounted = false;
    
    // iOS-style period toggle
    const analyticsPeriods = ['This Month', 'Last Month', 'Custom'];
    let selectedAnalyticsPeriod = 'This Month';
    $: activeAnalyticsPeriodIdx = Math.max(analyticsPeriods.indexOf(selectedAnalyticsPeriod), 0);
    $: monthPickerMonths = getSelectableMonths();

    let monthCategories = [];
    let monthTransactions = [];
    let prevMonthCategories = [];
    let prevMonthData = null;

    // Drill-down
    let selectedCategory = '';
    let categoryTransactions = [];
    // Top Merchants
    let topMerchants = [];    

    // Recurring / Subscriptions
    let recurringData = null;
    let recurringLoading = true;
    let visibleRecurringItems = [];
    let activeRecurring = [];
    let candidateRecurring = [];
    let inactiveRecurring = [];
    let cancelledRecurring = [];
    const FIXED_OBLIGATION_RECURRING_CATEGORIES = new Set([
        'Insurance', 'Auto Insurance', 'Home Insurance', 'Health Insurance', 'Renters Insurance', 'Life Insurance',
        'Housing', 'Rent Payment', 'Mortgage', 'Utilities', 'Electric', 'Gas', 'Water',
        'Internet', 'Wireless', 'Cable', 'Debt', 'Loan', 'Taxes', 'Healthcare', 'Therapy', 'Pharmacy'
    ]);

    function isSubscriptionService(item) {
        const category = (item?.category || '').trim();
        const text = `${item?.merchant || ''} ${item?.clean_name || ''} ${category}`.toLowerCase();
        if (FIXED_OBLIGATION_RECURRING_CATEGORIES.has(category)) return false;
        if (/\b(geico|progressive|state farm|allstate|anthem|kaiser|insurance)\b/.test(text)) return false;
        return true;
    }

    // Split recurring service items into active/inactive/cancelled whenever recurringData changes.
    // The backend also feeds Upcoming Bills, so fixed obligations are filtered at this UI boundary.
    $: {
        if (recurringData && recurringData.items) {
            visibleRecurringItems = recurringData.items.filter(isSubscriptionService);
            candidateRecurring = visibleRecurringItems.filter(i => (i.state === 'candidate' || i.status === 'candidate') && !i.cancelled);
            activeRecurring = visibleRecurringItems.filter(i =>
                (i.state === 'confirmed' || i.state === 'active' || i.status === 'active')
                && i.state !== 'candidate'
                && i.status !== 'candidate'
                && !i.cancelled
            );
            cancelledRecurring = visibleRecurringItems.filter(i => i.cancelled);
            inactiveRecurring = visibleRecurringItems.filter(i =>
                (i.status === 'inactive' || i.state === 'inactive' || i.state === 'stale')
                && !i.cancelled
            );
        } else {
            visibleRecurringItems = [];
            activeRecurring = [];
            candidateRecurring = [];
            inactiveRecurring = [];
            cancelledRecurring = [];
        }
    }

    // Subscription events (alerts)
    let subscriptionEvents = [];
    let unreadEventCount = 0;
    $: {
        if (recurringData && recurringData.events) {
            subscriptionEvents = recurringData.events.filter(e => !e.is_read);
            unreadEventCount = recurringData.unread_event_count || subscriptionEvents.length;
        } else {
            subscriptionEvents = [];
            unreadEventCount = 0;
        }
    }

    // Dismissed subscriptions
    let dismissedRecurring = [];
    let dismissedOpen = false;
    $: {
        if (recurringData && recurringData.dismissed) {
            dismissedRecurring = recurringData.dismissed;
        } else {
            dismissedRecurring = [];
        }
    }

    // Redetect loading state
    let redetectLoading = false;

    // Categories that are not real spending in the accrual model
    const NON_SPENDING_CATEGORIES_SET = new Set(['Savings Transfer', 'Personal Transfer', 'Credit Card Payment', 'Cash Withdrawal', 'Cash Deposit', 'Investment Transfer', 'Income', 'Credits & Refunds']);
    const merchantPalette = ['#d96d4a', '#1f2937', '#8bbfd9', '#7f9fd6', '#9a7de2', '#ef8fc3', '#f3a36d', '#6bd0a4'];

    // Inactive subscriptions dropdown state (closed by default)
    let candidateOpen = false;
    let inactiveOpen = false;

    // Cancelled subscriptions dropdown state (closed by default)
    let cancelledOpen = false;

    // Height of the subscriptions card — bound via bind:clientHeight.
    // subsCollapsedHeight is frozen whenever inactive/cancelled are both closed,
    // so the merchant list matches the subs card's natural collapsed height.
    let subsCardHeight = 0;
    let subsCollapsedHeight = 0;

    // Total historically spent on inactive subscriptions
    // Uses total_spent from backend, falls back to amount × charge_count
    $: inactiveTotalSpent = inactiveRecurring.reduce(
        (sum, item) => sum + (item.total_spent || (item.amount || 0) * (item.charge_count || 0)), 0
    );

    // Price change aggregation for recurring subscriptions
    let priceChangeCount = 0;
    let priceChangeTotalDelta = 0;
    $: {
        const increases = activeRecurring.filter(i => i.price_change && i.price_change.change > 0);
        priceChangeCount = increases.length;
        priceChangeTotalDelta = increases.reduce((sum, i) => sum + i.price_change.change, 0);
    }

    // Waterfall
    let waterfallEl;
    let waterfallTooltip = { show: false, x: 0, y: 0, label: '', amount: 0, runningFrom: 0, runningTo: 0, count: 0 };

    function getLastMonthKey() {
        const now = new Date();
        const lastMonth = new Date(now.getFullYear(), now.getMonth() - 1, 1);
        return `${lastMonth.getFullYear()}-${String(lastMonth.getMonth() + 1).padStart(2, '0')}`;
    }

    function getPreviousMonthKey(month) {
        const [year, monthNum] = month.split('-').map(Number);
        const previous = new Date(year, monthNum - 2, 1);
        return `${previous.getFullYear()}-${String(previous.getMonth() + 1).padStart(2, '0')}`;
    }

    function emptyMonthSummary(month) {
        return {
            month,
            income: 0,
            expenses: 0,
            refunds: 0,
            credits_refunds: 0,
            savings: 0,
            net: 0,
            cc_repaid: 0,
            external_transfers: 0,
            incoming_transfers: 0
        };
    }

    function getMonthSummary(month) {
        if (!month) return null;
        return monthly.find(m => m.month === month) || emptyMonthSummary(month);
    }

    function getSelectableMonths() {
        const byMonth = new Map((monthly || []).map(m => [m.month, m]));
        for (const month of [getCurrentMonth(), getLastMonthKey()]) {
            if (!byMonth.has(month)) byMonth.set(month, emptyMonthSummary(month));
        }
        return [...byMonth.values()].sort((a, b) => b.month.localeCompare(a.month));
    }

    /* ═══════════════════════════════════════
       LIFECYCLE
       ═══════════════════════════════════════ */
    onMount(async () => {
        mounted = true;
        // Fetch recurring detection (profile-aware, not month-specific)
        api.getRecurring().then(data => { recurringData = data; recurringLoading = false; }).catch(() => { recurringLoading = false; });

        let initialMonth = getCurrentMonth();
        let storedPeriod, storedCustom;
        const unsubP = selectedPeriodStore.subscribe(v => { storedPeriod = v; });
        const unsubC = selectedCustomMonthStore.subscribe(v => { storedCustom = v; });
        unsubP(); unsubC();

        if (storedPeriod === 'custom' && storedCustom) {
            initialMonth = storedCustom;
            selectedAnalyticsPeriod = 'Custom';
        } else if (storedPeriod === 'last_month') {
            initialMonth = getLastMonthKey();
            selectedAnalyticsPeriod = 'Last Month';
        } else {
            selectedAnalyticsPeriod = 'This Month';
        }

        selectedMonth = initialMonth;
        await loadMonthData();
    });

    /* ═══════════════════════════════════════
       DATA LOADING
       ═══════════════════════════════════════ */
    async function loadMonthData() {
        if (!selectedMonth) return;
        const requestMonth = selectedMonth;
        try {
            const sorted = [...monthly].sort((a, b) => b.month.localeCompare(a.month));
            const currentIdx = sorted.findIndex(m => m.month === requestMonth);
            const calendarPrevMonth = getPreviousMonthKey(requestMonth);
            const hasPrev = currentIdx >= 0
                ? currentIdx < sorted.length - 1
                : sorted.some(m => m.month === calendarPrevMonth);
            const prevMonth = currentIdx >= 0
                ? (hasPrev ? sorted[currentIdx + 1].month : null)
                : (hasPrev ? calendarPrevMonth : null);

            const promises = [
                api.getCategoryAnalytics(requestMonth),
                api.getTransactions({ month: requestMonth, limit: 1000 }).then(res => res.data),
                api.getMerchants(requestMonth).catch(() => []),
            ];
            if (prevMonth) {
                promises.push(api.getCategoryAnalytics(prevMonth).catch(() => []));
            }

            const results = await Promise.all(promises);
            if (requestMonth !== selectedMonth) return;
            const catResult0 = results[0];
            monthCategories = Array.isArray(catResult0) ? catResult0 : (catResult0?.categories || []);
            monthTransactions = results[1];
            topMerchants = results[2] || [];
            selectedCategory = '';
            categoryTransactions = [];

            if (hasPrev) {
                prevMonthData = sorted[currentIdx + 1];
                const catResult3 = results[3];
                prevMonthCategories = Array.isArray(catResult3) ? catResult3 : (catResult3?.categories || []);
            } else {
                prevMonthData = null;
                prevMonthCategories = [];
            }
        } catch (e) {
            console.error('Failed to load month data:', e);
        }
    }

    $: if (selectedMonth) loadMonthData();

    // ââ Profile switch: reload all analytics data ââ
    let _prevAnalyticsProfile = null;
    $: if ($activeProfile && $activeProfile !== _prevAnalyticsProfile) {
        if (_prevAnalyticsProfile !== null) {
            reloadAnalyticsForProfile();
        }
        _prevAnalyticsProfile = $activeProfile;
    }

    async function reloadAnalyticsForProfile() {
        profileSwitching = true;
        try {
            const [m, c, rec] = await Promise.all([
                api.getMonthlyAnalytics(),
                api.getCategoryAnalytics(),
                api.getRecurring().catch(() => null)
            ]);
            recurringData = rec;
            monthly = m;
            allCategories = Array.isArray(c) ? c : (c?.categories || []);
            if (selectedAnalyticsPeriod === 'This Month') {
                selectedMonth = getCurrentMonth();
            } else if (selectedAnalyticsPeriod === 'Last Month') {
                selectedMonth = getLastMonthKey();
            } else if (selectedMonth && !getSelectableMonths().some(s => s.month === selectedMonth)) {
                selectedMonth = getCurrentMonth();
            }
            await loadMonthData();
        } catch (e) {
            console.error('Failed to reload analytics for profile:', e);
        } finally {
            profileSwitching = false;
        }
    }

    // ── Unified pre-computed analytics context ──
    $: analyticsContext = (() => {
        const currentMonthSummary = getMonthSummary(selectedMonth);
        const sortedMonthly = [...monthly].sort((a, b) => a.month.localeCompare(b.month));
        const totalMonths = monthly.length;
        return { currentMonthSummary, sortedMonthly, totalMonths };
    })();

    $: currentMonthSummary = analyticsContext.currentMonthSummary;

    $: monthBriefingProfile = $activeProfile || 'household';

    $: if (mounted && selectedMonth && !explainingMonth && (explainedMonth !== selectedMonth || explainedProfile !== monthBriefingProfile)) {
        loadSelectedMonthBriefing(selectedMonth, monthBriefingProfile);
    }

    $: if (monthExplanation && explainedMonth && selectedMonth && explainedMonth !== selectedMonth) {
        monthExplanation = null;
        monthExplanationOpen = false;
        explainedMonth = '';
        explainedProfile = '';
        explanationError = '';
    }

    /* ═══════════════════════════════════════
       S1: SPENDING PULSE — Anomaly Detection
       ═══════════════════════════════════════ */
    // ââ Cached history map: recomputes only when allCategories or monthly.length changes ââ
    // This is independent of the selected period / month.
    let _historyMapKey = '';
    let _historyMapCache = {};

    $: {
        const hKey = `${allCategories.length}|${monthly.length}|${(allCategories[0]?.category || '')}`;
        if (hKey !== _historyMapKey) {
            _historyMapKey = hKey;
            const totalMonths = monthly.length;
            const map = {};
            for (const allCat of allCategories) {
                const catName = allCat.category;
                const allTimeTotal = allCat.total || 0;
                const naiveAvg = totalMonths > 0 ? allTimeTotal / totalMonths : 0;
                map[catName] = { allTimeTotal, naiveAvg, totalMonths };
            }
            _historyMapCache = map;
        }
    }

    $: spendingPulseCards = (() => {
        if (!monthCategories.length || !monthly.length) return [];

        const totalMonths = analyticsContext.totalMonths;

        // Filter out transfer categories — they're not spending in the accrual model
        const spendingCategories = monthCategories.filter(c => !NON_SPENDING_CATEGORIES_SET.has(c.category));

        return spendingCategories.map(cat => {
            const catName = cat.category;
            const currentTotal = cat.total;

            // Read from cached history map instead of scanning allCategories each time
            const history = _historyMapCache[catName] || { allTimeTotal: 0, naiveAvg: 0, totalMonths };
            const allTimeTotal = history.allTimeTotal;
            const naiveAvg = history.naiveAvg;
            const naiveRatio = naiveAvg > 0 ? currentTotal / naiveAvg : 0;

            let avgTotal = naiveAvg;
            let isPeriodic = false;
            let comparisonLabel = `${totalMonths}-mo avg`;

            if (naiveRatio > 4 && currentTotal > 50 && allTimeTotal > 0) {
                const estimatedActiveMonths = Math.max(Math.round(allTimeTotal / currentTotal), 1);
                const frequency = estimatedActiveMonths / totalMonths;

                if (frequency <= 0.4) {
                    isPeriodic = true;
                    avgTotal = allTimeTotal / estimatedActiveMonths;
                    comparisonLabel = `avg of ~${estimatedActiveMonths} active mo`;
                }
            }

            const deviation = avgTotal > 0 ? ((currentTotal - avgTotal) / avgTotal) * 100 : 0;

            const threshold = isPeriodic ? 50 : 25;
            const isAnomaly = Math.abs(deviation) > threshold;
            const isOver = deviation > threshold;
            const isUnder = deviation < -threshold;

            const displayDeviation = Math.max(Math.min(deviation, 999), -999);

            const prevCat = prevMonthCategories.find(c => c.category === catName);
            const prevTotal = prevCat ? prevCat.total : 0;

            return {
                category: catName,
                total: currentTotal,
                percent: cat.percent,
                avgTotal,
                deviation: displayDeviation,
                rawDeviation: deviation,
                isAnomaly,
                isOver,
                isUnder,
                isPeriodic,
                comparisonLabel,
                prevTotal,
                color: CATEGORY_COLORS[catName] || '#627d98',
                icon: CATEGORY_ICONS[catName] || 'label'
            };
        }).sort((a, b) => {
            if (a.isAnomaly && !b.isAnomaly) return -1;
            if (!a.isAnomaly && b.isAnomaly) return 1;
            return Math.abs(b.deviation) - Math.abs(a.deviation);
        });
    })();

    /* ═══════════════════════════════════════
       S2: CASH FLOW WATERFALL — SVG Data
       ═══════════════════════════════════════ */
    $: waterfallData = (() => {
        if (!currentMonthSummary) return null;

        const income = currentMonthSummary.income;
        const expenses = currentMonthSummary.expenses;
        const creditsRefunds = currentMonthSummary.credits_refunds ?? currentMonthSummary.refunds ?? 0;
        const incomingTransfers = currentMonthSummary.incoming_transfers || 0;
        // Accrual-basis: external transfers are a real outflow (Zelle/Venmo to others)
        const externalTransfers = currentMonthSummary.external_transfers || 0;

        // The waterfall shows: Income → Credits → Incoming Transfers → Expenses → Outgoing Transfers → Net
        // Internal/household transfers (Savings Transfer, Personal Transfer, CC Payment)
        // are excluded — they're just money moving between your own accounts.

        const items = [];
        let running = 0;

        // START bar (anchor)
        items.push({
            label: 'Opening',
            value: 0,
            runningBefore: 0,
            runningAfter: 0,
            type: 'anchor',
            color: 'var(--accent)'
        });

        // Income (single bar for now — could be split if we had source data)
        running += income;
        items.push({
            label: 'Income',
            value: income,
            runningBefore: 0,
            runningAfter: running,
            type: 'income',
            color: 'var(--flow-income)',
            icon: 'trending_up'
        });

        if (creditsRefunds > 0) {
            const before = running;
            running += creditsRefunds;
            items.push({
                label: 'Credits',
                value: creditsRefunds,
                runningBefore: before,
                runningAfter: running,
                type: 'credit',
                color: 'var(--positive)',
                icon: 'undo',
                count: monthTransactions.filter(t => parseFloat(t.amount) > 0 && (t.category === 'Credits & Refunds' || (!NON_SPENDING_CATEGORIES_SET.has(t.category) && t.category !== 'Income'))).length
            });
        }

        if (incomingTransfers > 0) {
            const before = running;
            running += incomingTransfers;
            items.push({
                label: 'Incoming',
                value: incomingTransfers,
                runningBefore: before,
                runningAfter: running,
                type: 'incoming_transfer',
                color: 'var(--flow-transfer)',
                icon: 'call_received',
                count: monthTransactions.filter(t => ['transfer_external', 'transfer_household'].includes(t.expense_type) && parseFloat(t.amount) > 0).length
            });
        }

        // Expense categories sorted by total descending
        // Exclude transfer categories — they're not real spending in the accrual model
        const expenseCats = [...monthCategories]
            .filter(c => c.category !== 'Savings Transfer' && c.category !== 'Personal Transfer')
            .sort((a, b) => (b.gross ?? b.total) - (a.gross ?? a.total));
        const hasAnyFlow =
            income > 0 ||
            creditsRefunds > 0 ||
            incomingTransfers > 0 ||
            externalTransfers > 0 ||
            expenseCats.some(c => (c.gross ?? c.total) > 0);
        if (!hasAnyFlow) return null;

        // Get transaction counts per category
        const txnCounts = {};
        monthTransactions.forEach(t => {
            if (parseFloat(t.amount) < 0) {
                txnCounts[t.category] = (txnCounts[t.category] || 0) + 1;
            }
        });

        for (const cat of expenseCats) {
            const categorySpend = cat.gross ?? cat.total;
            if (categorySpend <= 0) continue;
            const before = running;
            running -= categorySpend;
            items.push({
                label: cat.category,
                value: -categorySpend,
                runningBefore: before,
                runningAfter: running,
                type: 'expense',
                color: CATEGORY_COLORS[cat.category] || '#627d98',
                icon: CATEGORY_ICONS[cat.category] || 'label',
                count: txnCounts[cat.category] || 0
            });
        }

        // External Transfers (Zelle/Venmo to people outside your accounts)
        // These are real outflows in the accrual model
        if (externalTransfers > 0) {
            const before = running;
            running -= externalTransfers;
            items.push({
                label: 'Ext. Transfers',
                value: -externalTransfers,
                runningBefore: before,
                runningAfter: running,
                type: 'external_transfer',
                color: 'var(--warning)',
                icon: 'send_money',
                count: monthTransactions.filter(t => t.expense_type === 'transfer_external' && parseFloat(t.amount) < 0).length
            });
        }

        // END bar (anchor) — Net = Income + Credits + Incoming Transfers - Spending - External Transfers
        items.push({
            label: 'Net',
            value: running,
            runningBefore: 0,
            runningAfter: running,
            type: 'result',
            color: running >= 0 ? 'var(--positive)' : 'var(--negative)'
        });

        return { items, maxValue: Math.max(income + creditsRefunds + incomingTransfers, income), minValue: Math.min(running, 0), netResult: running };
    })();

    $: analyticsHeroDrivers = (() => {
        const drivers = [];
        const flowItems = waterfallData?.items || [];
        const drags = flowItems
            .filter(item => item.value < 0 && item.type !== 'result')
            .sort((a, b) => Math.abs(b.value) - Math.abs(a.value));
        const offsets = flowItems
            .filter(item => item.value > 0 && item.type !== 'result')
            .sort((a, b) => Math.abs(b.value) - Math.abs(a.value));

        if (drags[0]) {
            drivers.push({
                label: 'Largest drag',
                value: drags[0].label,
                detail: `-${formatCurrency(Math.abs(drags[0].value))}`,
                tone: 'negative'
            });
        }

        if (offsets[0]) {
            drivers.push({
                label: 'Biggest offset',
                value: offsets[0].label,
                detail: `+${formatCurrency(offsets[0].value)}`,
                tone: 'positive'
            });
        }

        const anomaly = spendingPulseCards.find(card => card.isAnomaly);
        if (anomaly) {
            drivers.push({
                label: 'Unusual category',
                value: anomaly.category,
                detail: `${formatPercent(Math.abs(anomaly.deviation))} ${anomaly.isOver ? 'above' : 'below'} avg`,
                tone: anomaly.isOver ? 'warning' : 'positive'
            });
        }

        return drivers.slice(0, 3);
    })();

    /* Waterfall SVG geometry */
    $: waterfallGeometry = (() => {
        if (!waterfallData) return null;
        const { items, maxValue, minValue } = waterfallData;

        const W = 900;
        const H = 320;
        const padTop = 32;
        const padBottom = 60;
        const padLeft = 10;
        const padRight = 10;
        const chartW = W - padLeft - padRight;
        const chartH = H - padTop - padBottom;

        const barCount = items.length;
        const barGap = Math.min(12, chartW / barCount * 0.2);
        const barWidth = Math.max(20, (chartW - barGap * (barCount - 1)) / barCount);

        // Y scale: 0 to maxValue with some padding
        const yMax = maxValue * 1.12;
        const yMin = Math.min(minValue * 1.1, -maxValue * 0.05);
        const yRange = yMax - yMin;

        function yScale(val) {
            return padTop + chartH - ((val - yMin) / yRange) * chartH;
        }

        const zeroY = yScale(0);

        const bars = items.map((item, i) => {
            const x = padLeft + i * (barWidth + barGap);

            let y, h;
            if (item.type === 'anchor') {
                // Zero-height marker at baseline
                y = zeroY;
                h = 2;
            } else if (item.type === 'result') {
                // Anchored from zero
                const top = Math.max(item.runningAfter, 0);
                const bottom = Math.min(item.runningAfter, 0);
                y = yScale(top);
                h = Math.max(yScale(bottom) - y, 3);
            } else if (item.value >= 0) {
                // Rises from runningBefore to runningAfter
                y = yScale(item.runningAfter);
                h = Math.max(yScale(item.runningBefore) - y, 3);
            } else {
                // Outflow: drops from runningBefore to runningAfter
                y = yScale(item.runningBefore);
                h = Math.max(yScale(item.runningAfter) - y, 3);
            }

            return { ...item, x, y, h, barWidth, index: i };
        });

        // Bridge connectors (dashed lines between bar tops)
        const bridges = [];
        for (let i = 0; i < bars.length - 1; i++) {
            const curr = bars[i];
            const next = bars[i + 1];

            let bridgeY;
            if (curr.type === 'anchor') {
                bridgeY = zeroY;
            } else if (curr.type === 'income' || curr.type === 'result') {
                bridgeY = yScale(curr.runningAfter);
            } else {
                bridgeY = yScale(curr.runningAfter);
            }

            bridges.push({
                x1: curr.x + barWidth,
                x2: next.x,
                y: bridgeY
            });
        }

        // Grid lines
        const gridLines = [];
        const gridCount = 4;
        for (let i = 0; i <= gridCount; i++) {
            const val = yMin + (yRange / gridCount) * i;
            gridLines.push({ y: yScale(val), label: formatCompact(val) });
        }

        return { bars, bridges, gridLines, zeroY, W, H, padTop, padBottom, barWidth };
    })();

    function handleWaterfallHover(bar, event) {
        if (bar.type === 'anchor') return;
        const svg = event.currentTarget.closest('svg');
        const rect = svg.getBoundingClientRect();
        waterfallTooltip = {
            show: true,
            x: event.clientX - rect.left,
            y: event.clientY - rect.top - 16,
            label: bar.label,
            amount: bar.value,
            runningFrom: bar.runningBefore,
            runningTo: bar.runningAfter,
            count: bar.count || 0,
            type: bar.type
        };
    }

    function handleWaterfallLeave() {
        waterfallTooltip = { ...waterfallTooltip, show: false };
    }

    function isWaterfallBarClickable(bar) {
        return !['anchor', 'result', 'income'].includes(bar.type);
    }

    function handleWaterfallClick(bar) {
        if (bar.type === 'anchor' || bar.type === 'result' || bar.type === 'income') return;
        if (bar.type === 'external_transfer') {
            // Drill into external transfer transactions
            selectedCategory = 'External Transfers';
            categoryTransactions = monthTransactions
                .filter(t => t.expense_type === 'transfer_external' && parseFloat(t.amount) < 0)
                .sort((a, b) => Math.abs(parseFloat(b.amount)) - Math.abs(parseFloat(a.amount)));
            return;
        }
        if (bar.type === 'incoming_transfer') {
            selectedCategory = 'Incoming Transfers';
            categoryTransactions = monthTransactions
                .filter(t => ['transfer_external', 'transfer_household'].includes(t.expense_type) && parseFloat(t.amount) > 0)
                .sort((a, b) => Math.abs(parseFloat(b.amount)) - Math.abs(parseFloat(a.amount)));
            return;
        }
        if (bar.type === 'credit') {
            selectedCategory = 'Credits & Refunds';
            categoryTransactions = monthTransactions
                .filter(t => parseFloat(t.amount) > 0 && (t.category === 'Credits & Refunds' || (!NON_SPENDING_CATEGORIES_SET.has(t.category) && t.category !== 'Income')))
                .sort((a, b) => Math.abs(parseFloat(b.amount)) - Math.abs(parseFloat(a.amount)));
            return;
        }
        let catName = bar.label;
        drillIntoCategory(catName);
    }

    function handleWaterfallKeydown(event, bar) {
        if (!isWaterfallBarClickable(bar)) return;
        if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            handleWaterfallClick(bar);
        }
    }

    /* ═══════════════════════════════════════
       S4: SAVINGS RATE TREND
       ═══════════════════════════════════════ */
    $: savingsRateTrend = (() => {
        if (monthly.length < 2) return null;
        const sorted = analyticsContext.sortedMonthly.slice(-12);

        const points = sorted.map(m => {
            // Accrual-basis: Net Flow = Income - Spending - External Transfers
            const extTransfers = m.external_transfers || 0;
            const rate = m.income > 0 ? Math.max(((m.income - m.expenses - extTransfers) / m.income) * 100, 0) : 0;
            return { month: m.month, rate: Math.min(rate, 100) };
        });

        // 3-month rolling average
        const rolling = points.map((p, i) => {
            const start = Math.max(0, i - 2);
            const window = points.slice(start, i + 1);
            const avg = window.reduce((s, w) => s + w.rate, 0) / window.length;
            return { ...p, rolling: avg };
        });

        const currentRate = rolling[rolling.length - 1]?.rate || 0;
        const avgRate = points.reduce((s, p) => s + p.rate, 0) / points.length;

        return { points: rolling, currentRate, avgRate, target: 25, windowMonths: points.length };
    })();

    /* Savings Rate SVG geometry */
    $: savingsRateGeometry = (() => {
        if (!savingsRateTrend || savingsRateTrend.points.length < 2) return null;
        const { points, target } = savingsRateTrend;

        const W = 500;
        const H = 180;
        const padTop = 16;
        const padBottom = 28;
        const padLeft = 36;
        const padRight = 12;
        const chartW = W - padLeft - padRight;
        const chartH = H - padTop - padBottom;

        const rates = points.map(p => p.rate);
        const rollingRates = points.map(p => p.rolling);
        const allVals = [...rates, ...rollingRates, target];
        const yMax = Math.max(...allVals, 40) * 1.1;
        const yMin = 0;
        const yRange = yMax - yMin;

        function yScale(val) {
            return padTop + chartH - ((val - yMin) / yRange) * chartH;
        }

        const stepX = chartW / (points.length - 1);

        // Actual rate dots
        const dots = points.map((p, i) => ({
            x: padLeft + i * stepX,
            y: yScale(p.rate),
            rate: p.rate,
            month: p.month
        }));

        // Rolling average line
        let rollingPath = '';
        points.forEach((p, i) => {
            const x = padLeft + i * stepX;
            const y = yScale(p.rolling);
            rollingPath += i === 0 ? `M${x},${y}` : ` L${x},${y}`;
        });

        // Target line
        const targetY = yScale(target);

        // Grid lines
        const gridLines = [];
        const gridSteps = [0, 10, 20, 30, 40];
        for (const val of gridSteps) {
            if (val <= yMax) {
                gridLines.push({ y: yScale(val), label: `${val}%` });
            }
        }

        // Month labels (every 3 months)
        const monthLabels = [];
        points.forEach((p, i) => {
            if (i === 0 || i === points.length - 1 || i % 3 === 0) {
                monthLabels.push({ x: padLeft + i * stepX, label: formatMonthShort(p.month) });
            }
        });

        return { dots, rollingPath, targetY, gridLines, monthLabels, W, H, padTop, padBottom, padLeft };
    })();

    /* ═══════════════════════════════════════
       S5: PROJECTED YEAR-END
       ═══════════════════════════════════════ */
    $: projectedYearEnd = (() => {
        if (monthly.length < 3) return null;
        const allSorted = analyticsContext.sortedMonthly;
        const sorted = allSorted.slice(-12);

        // Last 12 months baseline (accrual-basis), aligned with the trajectory chart.
        const avgIncome = sorted.reduce((s, m) => s + m.income, 0) / sorted.length;
        const avgExpenses = sorted.reduce((s, m) => s + m.expenses, 0) / sorted.length;
        const avgCreditsRefunds = sorted.reduce((s, m) => s + (m.credits_refunds ?? m.refunds ?? 0), 0) / sorted.length;
        const avgIncomingTransfers = sorted.reduce((s, m) => s + (m.incoming_transfers || 0), 0) / sorted.length;
        const avgExtTransfers = sorted.reduce((s, m) => s + (m.external_transfers || 0), 0) / sorted.length;
        const avgNet = avgIncome - avgExpenses + avgCreditsRefunds + avgIncomingTransfers - avgExtTransfers;

        // Current year
        const currentYear = new Date().getFullYear();
        const currentMonth = new Date().getMonth(); // 0-indexed
        const remainingMonths = 12 - currentMonth - 1;

        // YTD totals (accrual-basis net)
        const ytdMonths = allSorted.filter(m => m.month.startsWith(currentYear.toString()));
        const ytdNet = ytdMonths.reduce((s, m) => s + (m.income || 0) - (m.expenses || 0) + (m.credits_refunds ?? m.refunds ?? 0) + (m.incoming_transfers || 0) - (m.external_transfers || 0), 0);

        const projectedAdditional = avgNet * remainingMonths;
        const projectedTotal = ytdNet + projectedAdditional;

        // Optimistic (+20%) and pessimistic (-20%)
        const optimistic = ytdNet + projectedAdditional * 1.20;
        const pessimistic = ytdNet + projectedAdditional * 0.80;

        const projectedSavingsRate = avgIncome > 0 ? Math.max((avgNet / avgIncome) * 100, 0) : 0;

        return {
            avgNet,
            remainingMonths,
            projectedTotal,
            optimistic,
            pessimistic,
            projectedSavingsRate,
            ytdNet,
            currentYear,
            windowMonths: sorted.length
        };
    })();

    /* ═══════════════════════════════════════
       S6: INCOME STABILITY
       ═══════════════════════════════════════ */
    $: incomeStability = (() => {
        if (monthly.length < 3) return null;
        const sorted = analyticsContext.sortedMonthly.slice(-12);
        const incomes = sorted.map(m => m.income);
        const avgIncome = incomes.reduce((s, v) => s + v, 0) / incomes.length;
        const variance = incomes.reduce((s, v) => s + Math.pow(v - avgIncome, 2), 0) / incomes.length;
        const stdDev = Math.sqrt(variance);
        const cv = avgIncome > 0 ? (stdDev / avgIncome) * 100 : 0; // Coefficient of variation

        let level = 'Stable';
        let dots = 5;
        if (cv > 30) { level = 'Volatile'; dots = 1; }
        else if (cv > 20) { level = 'Moderate'; dots = 3; }
        else if (cv > 10) { level = 'Stable'; dots = 4; }
        else { level = 'Very Stable'; dots = 5; }

        // Consecutive months with income
        let streak = 0;
        for (let i = incomes.length - 1; i >= 0; i--) {
            if (incomes[i] > 0) streak++;
            else break;
        }

        return { avgIncome, stdDev, cv, level, dots, streak, totalMonths: incomes.length };
    })();

    /* ═══════════════════════════════════════
       S7: MONTH-OVER-MONTH DIFF TABLE
       ═══════════════════════════════════════ */
    $: momDiff = (() => {
        if (!monthCategories.length && !prevMonthCategories.length) return [];
        const categoryNames = new Set([
            ...monthCategories.map(c => c.category),
            ...prevMonthCategories.map(c => c.category)
        ]);

        return Array.from(categoryNames).map(category => {
            const current = monthCategories.find(c => c.category === category);
            const prev = prevMonthCategories.find(c => c.category === category);
            const currentTotal = current ? current.total : 0;
            const prevTotal = prev ? prev.total : 0;
            const delta = currentTotal - prevTotal;
            const deltaPct = prevTotal > 0 ? ((currentTotal - prevTotal) / prevTotal) * 100 : (currentTotal > 0 ? 100 : 0);
            return {
                category,
                currentTotal,
                prevTotal,
                delta,
                deltaPct,
                color: CATEGORY_COLORS[category] || '#627d98',
                icon: CATEGORY_ICONS[category] || 'label'
            };
        }).sort((a, b) => Math.abs(b.delta) - Math.abs(a.delta));
    })();

    // Best/Worst months
    $: bestWorstMonth = (() => {
        if (monthly.length < 2) return null;
        const sorted = [...monthly].sort((a, b) => a.expenses - b.expenses);
        const best = sorted[0];
        const worst = sorted[sorted.length - 1];
        return { best, worst };
    })();

    // Freeze collapsed height whenever both dropdowns are closed
    $: if (!inactiveOpen && !cancelledOpen && subsCardHeight > 0) {
        subsCollapsedHeight = subsCardHeight;
    }

    /* ═══════════════════════════════════════
       PER-SECTION INSIGHTS
       ═══════════════════════════════════════ */
    $: pulseInsight = (() => {
        const overCards = spendingPulseCards.filter(c => c.isOver);
        if (!overCards.length) return null;
        const top = overCards[0];
        return `${top.category} is ${Math.round(top.deviation)}% above its average this month`;
    })();

    $: merchantInsight = (() => {
        if (topMerchants.length < 2 || !currentMonthSummary?.expenses) return null;
        const n = Math.min(topMerchants.length, 3);
        const topNTotal = topMerchants.slice(0, n).reduce((s, m) => s + m.total_spent, 0);
        const pct = Math.round((topNTotal / currentMonthSummary.expenses) * 100);
        if (pct < 10) return null;
        return `Top ${n} merchants · ${pct}% of spending`;
    })();

    $: merchantBrief = (() => {
        if (!topMerchants.length) return null;
        const total = topMerchants.reduce((s, m) => s + (m.total_spent || 0), 0);
        const n = Math.min(topMerchants.length, 3);
        const topNTotal = topMerchants.slice(0, n).reduce((s, m) => s + (m.total_spent || 0), 0);
        const share = total > 0 ? Math.round((topNTotal / total) * 100) : 0;
        const leader = topMerchants[0]?.name || 'Top merchant';
        return { total, n, topNTotal, share, leader };
    })();

    $: subscriptionInsight = (() => {
        if (!inactiveRecurring.length) return null;
        if (inactiveTotalSpent < 1) return null;
        return `${inactiveRecurring.length} inactive · ${formatCurrency(inactiveTotalSpent)} paid historically`;
    })();

    $: recurringReviewValue = inactiveTotalSpent;
    const recurringFrequencyMultipliers = {
        weekly: 52,
        biweekly: 26,
        monthly: 12,
        quarterly: 4,
        semi_annual: 2,
        annual: 1
    };

    function recurringDisplayAmount(item) {
        const amount = Number(item?.amount ?? item?.avg_amount ?? 0);
        return Number.isFinite(amount) ? amount : 0;
    }

    function recurringAnnualCost(item) {
        const annual = Number(item?.annual_cost);
        if (Number.isFinite(annual) && annual > 0) return annual;
        const freq = String(item?.frequency || 'monthly').toLowerCase().replace('-', '_');
        return recurringDisplayAmount(item) * (recurringFrequencyMultipliers[freq] || 12);
    }

    function recurringAmountNote(item) {
        const amount = recurringDisplayAmount(item);
        const stored = Number(item?.stored_amount ?? 0);
        if (stored > 0 && Math.abs(stored - amount) >= 0.01) {
            return `was ${formatCurrency(stored)}`;
        }
        return '';
    }

    function subscriptionTimingText(item) {
        const last = item?.last_charge || item?.evidence?.last_paid;
        const next = item?.next_expected || item?.next_date;
        const inactive = item?.cancelled || ['inactive', 'stale', 'cancelled'].includes(item?.state) || ['inactive', 'cancelled'].includes(item?.status);
        const parts = [];
        if (last) parts.push(`Last ${formatDateShort(last)}`);
        if (next && !inactive) {
            parts.push(`Next ${formatDateShort(next)}`);
        } else if (inactive) {
            parts.push('No next charge');
        }
        return parts.join(' · ');
    }

    $: visibleRecurringTotals = (() => {
        const trustedActive = activeRecurring.filter((item) =>
            item.confirmed ||
            item.confidence === 'user' ||
            item.confidence === 'high' ||
            Number(item.confidence_score || 0) >= 75
        );
        const annual = trustedActive.reduce((sum, item) => sum + recurringAnnualCost(item), 0);
        return {
            monthly: annual / 12,
            annual,
            active: trustedActive.length,
            inactive: inactiveRecurring.length,
            cancelled: cancelledRecurring.length
        };
    })();

    $: healthStatus = (() => {
        const rate = savingsRateTrend?.currentRate ?? 0;
        const projected = projectedYearEnd?.projectedTotal ?? 0;
        if (rate >= 25 && projected >= 0) return { label: 'on track', tone: 'positive' };
        if (rate >= 10 || projected >= 0) return { label: 'watch', tone: 'warning' };
        return { label: 'at risk', tone: 'negative' };
    })();

    $: healthScore = (() => {
        const rate = savingsRateTrend?.currentRate ?? 0;
        const projected = projectedYearEnd?.projectedTotal ?? 0;
        const avgNet = projectedYearEnd?.avgNet ?? 0;
        const stabilityDots = incomeStability?.dots ?? 0;
        const incomeStreak = incomeStability?.streak ?? 0;
        let score = 0;
        if (rate >= 25) score += 1.25;
        else if (rate >= 10) score += 0.7;
        if (projected >= 0) score += 1.25;
        else if (projected > -5000) score += 0.5;
        if (avgNet >= 0) score += 1;
        else if (avgNet > -1000) score += 0.4;
        score += (Math.min(stabilityDots, 5) / 5) * 0.75;
        score += (Math.min(incomeStreak, 6) / 6) * 0.75;
        return Math.max(0, Math.min(5, score));
    })();

    $: savingsTargetGap = savingsRateTrend ? savingsRateTrend.currentRate - savingsRateTrend.target : 0;
    $: savingsMonthsAtTarget = savingsRateTrend
        ? savingsRateTrend.points.filter(p => p.rate >= savingsRateTrend.target).length
        : 0;

    $: momInsight = (() => {
        if (!momDiff.length || !prevMonthData) return null;
        const biggest = momDiff[0];
        if (Math.abs(biggest.delta) < 5) return null;
        const dir = biggest.delta > 0 ? 'up' : 'down';
        return `${biggest.category} ${dir} ${formatCurrency(Math.abs(biggest.delta))} vs. ${formatMonthShort(prevMonthData.month)}`;
    })();

    $: trendsInsight = (() => {
        if (!savingsRateTrend) return null;
        const { currentRate, target } = savingsRateTrend;
        const diff = Math.abs(Math.round(currentRate - target));
        return currentRate >= target
            ? `Current rate ${formatPercent(currentRate)} · ${diff} points above ${target}% target`
            : `Current rate ${formatPercent(currentRate)} · ${diff} points below ${target}% target`;
    })();

    /* ═══════════════════════════════════════
       S8: ACTIONABLE NUDGE
       ═══════════════════════════════════════ */
    $: actionableNudge = (() => {
        if (!spendingPulseCards.length || !currentMonthSummary) return null;

        const overSpend = spendingPulseCards.filter(c => c.isOver);
        if (overSpend.length === 0) return null;

        // Sum potential savings: reduce each over-budget category to its average
        let totalPotential = 0;
        const suggestions = [];
        for (const cat of overSpend.slice(0, 3)) {
            const savings = cat.total - cat.avgTotal;
            if (savings > 0) {
                totalPotential += savings;
                suggestions.push({ name: cat.category, savings, color: cat.color });
            }
        }

        if (totalPotential <= 0) return null;

        const annualized = totalPotential * 12;
        const extTransfers = currentMonthSummary.external_transfers || 0;
        const currentSR = currentMonthSummary.income > 0
            ? ((currentMonthSummary.income - currentMonthSummary.expenses - extTransfers) / currentMonthSummary.income) * 100
            : 0;
        const newSR = currentMonthSummary.income > 0
            ? ((currentMonthSummary.income - currentMonthSummary.expenses - extTransfers + totalPotential) / currentMonthSummary.income) * 100
            : 0;

        return { totalPotential, annualized, suggestions, currentSR, newSR };
    })();

    $: budgetNudgeHref = actionableNudge?.suggestions?.[0]?.name
        ? `/budget?category=${encodeURIComponent(actionableNudge.suggestions[0].name)}`
        : '/budget';


    /* ---------------------------------------
       DRILL-DOWN
       --------------------------------------- */
    function drillIntoCategory(cat) {
        selectedCategory = cat;
        categoryTransactions = monthTransactions
            .filter(t => t.category === cat && parseFloat(t.amount) < 0)
            .sort((a, b) => Math.abs(parseFloat(b.amount)) - Math.abs(parseFloat(a.amount)));
    }

    function closeDrillDown() {
        selectedCategory = '';
        categoryTransactions = [];
    }

    function selectAnalyticsPeriod(period) {
        selectedAnalyticsPeriod = period;
        if (period === 'This Month') {
            selectedMonth = getCurrentMonth();
        } else if (period === 'Last Month') {
            selectedMonth = getLastMonthKey();
        }
        // 'Custom' does nothing — user picks from dropdown
    }

    async function loadSelectedMonthBriefing(month, profileKey) {
        if (!month || explainingMonth) return;
        explainingMonth = true;
        explanationError = '';
        try {
            const result = await api.explainMonth(month, false, null, false);
            if (selectedMonth === month && monthBriefingProfile === profileKey) {
                monthExplanation = result;
                explainedMonth = month;
                explainedProfile = profileKey;
                monthExplanationOpen = true;
            }
        } catch (e) {
            console.error('Failed to load month briefing:', e);
            if (selectedMonth === month) {
                monthExplanation = null;
                explainedMonth = month;
                explainedProfile = profileKey;
                explanationError = e?.message || 'Failed to load month briefing';
            }
        } finally {
            explainingMonth = false;
        }
    }

    const MONTH_EXPLANATION_SECTION_META = [
        { label: 'Takeaway', icon: 'auto_awesome', tone: 'accent' },
        { label: 'Drivers', icon: 'query_stats', tone: 'spend' },
        { label: 'Watch', icon: 'event_upcoming', tone: 'warning' },
        { label: 'Facts', icon: 'fact_check', tone: 'muted' },
    ];

    function parseMonthExplanation(answer) {
        const text = String(answer || '').trim();
        if (!text) return [];

        const lines = text.split(/\n+/).map(line => line.trim()).filter(Boolean);
        const used = new Set();
        const sections = [];

        for (const meta of MONTH_EXPLANATION_SECTION_META) {
            const pattern = new RegExp(`^[\\s\\-*]*\\*{0,2}${meta.label}\\*{0,2}\\s*[:\\-]\\s*`, 'i');
            const idx = lines.findIndex((line, i) => !used.has(i) && pattern.test(line));
            if (idx === -1) continue;
            used.add(idx);
            sections.push({
                ...meta,
                text: lines[idx].replace(pattern, '').trim(),
            });
        }

        const extra = lines.filter((_, i) => !used.has(i)).join(' ');
        if (extra) {
            sections.push({ label: 'Mira', icon: 'notes', tone: 'muted', text: extra });
        }

        return sections.length
            ? sections
            : [{ label: 'Mira', icon: 'auto_awesome', tone: 'accent', text }];
    }

    $: monthExplanationSections = monthExplanation ? parseMonthExplanation(monthExplanation.answer) : [];
    $: analyticsHeroBriefSections = (() => {
        if (!currentMonthSummary) return [];

        const creditsRefunds = currentMonthSummary.credits_refunds ?? currentMonthSummary.refunds ?? 0;
        const incomingTransfers = currentMonthSummary.incoming_transfers || 0;
        const extTransfers = currentMonthSummary.external_transfers || 0;
        const netFlow = waterfallData?.netResult ?? ((currentMonthSummary.income || 0) - (currentMonthSummary.expenses || 0) + creditsRefunds + incomingTransfers - extTransfers);
        const topDrag = analyticsHeroDrivers.find(driver => driver.label === 'Largest drag');
        const topOffset = analyticsHeroDrivers.find(driver => driver.label === 'Biggest offset');
        const anomaly = analyticsHeroDrivers.find(driver => driver.label === 'Unusual category');

        return [
            {
                label: 'Takeaway',
                icon: 'auto_awesome',
                tone: 'accent',
                text: `Net flow for ${formatMonth(selectedMonth)} is ${netFlow >= 0 ? '+' : ''}${formatCurrency(netFlow)} after ${formatCurrency(currentMonthSummary.expenses)} in expenses.`
            },
            {
                label: 'Drivers',
                icon: 'query_stats',
                tone: 'spend',
                text: topDrag
                    ? `${topDrag.value} is the largest drag at ${topDrag.detail}.`
                    : 'No material spending drag is showing for this month yet.'
            },
            {
                label: 'Watch',
                icon: 'event_upcoming',
                tone: 'warning',
                text: anomaly
                    ? `${anomaly.value} is moving differently from its usual pattern: ${anomaly.detail.toLowerCase()}.`
                    : 'No unusual category movement is standing out yet.'
            },
            {
                label: 'Facts',
                icon: 'fact_check',
                tone: 'muted',
                text: topOffset
                    ? `${topOffset.value} is the biggest offset at ${topOffset.detail}.`
                    : `Income is ${formatCurrency(currentMonthSummary.income || 0)} and credits are ${formatCurrency(creditsRefunds)}.`
            }
        ];
    })();
    $: analyticsHeroDisplayedSections = monthExplanation
        ? monthExplanationSections
        : analyticsHeroBriefSections;
    $: analyticsHeroFactSummary = (() => {
        const facts = monthExplanation?.facts;
        if (!facts) return null;

        const compactFact = (item, labelKey = 'category') => {
            const label = item?.[labelKey] || item?.merchant || 'Item';
            const amount = Number.isFinite(Number(item?.total)) ? formatCompact(Number(item.total)) : (item?.total_formatted || '$0');
            return `${label} ${amount}`;
        };
        const recurring = facts.recurring || {};

        return {
            recurring: `${recurring.monthly_formatted || '$0'} recurring · ${recurring.active_count || 0} active`,
            categories: (facts.top_categories || []).slice(0, 2).map(cat => compactFact(cat)).join(' · '),
            merchants: (facts.top_merchants || []).slice(0, 2).map(merchant => compactFact(merchant, 'merchant')).join(' · ')
        };
    })();

    function normalizeHeroBullet(text) {
        return String(text || '')
            .replace(/\s+/g, ' ')
            .replace(/\(([^)]+)\)/g, '$1')
            .replace(/\.$/, '')
            .trim();
    }

    function analyticsHeroBulletRows(section) {
        const text = normalizeHeroBullet(section?.text);
        if (!text) return [];

        if (section.label === 'Takeaway') {
            const match = text.match(/ended at\s+(.+?)\s+net flow after\s+(.+?)\s+in spending and\s+(.+?)\s+in income/i);
            if (match) return [`${match[1]} net flow`, `${match[2]} spent · ${match[3]} income`];
        }

        if (section.label === 'Drivers') {
            const movementText = text.includes(' were ') ? text.split(' were ').pop() : text;
            return movementText
                .split(/,\s+/)
                .map(normalizeHeroBullet)
                .filter(Boolean)
                .slice(0, 2);
        }

        if (section.label === 'Watch') {
            const [lead, ledBy] = text.split(/,\s+led by\s+/i);
            if (ledBy) {
                const compactLead = normalizeHeroBullet(
                    lead.replace(/(.+?)\s+is scheduled over the next\s+(\d+)\s+days/i, '$1 scheduled · $2 days')
                );
                return [
                    compactLead,
                    ledBy.split(/,\s+/).map(normalizeHeroBullet).filter(Boolean).slice(0, 2).join(' · ')
                ].filter(Boolean);
            }
        }

        return [text];
    }

    // ── Subscription feedback handlers ──────────────────────────────
    let subscriptionFeedback = '';

    async function handleConfirmSubscription(item) {
        try {
            const amount = Math.abs(Number(item.amount || item.avg_amount || 0));
            if (amount > 0) {
                await api.declareSubscription(
                    item.merchant,
                    amount,
                    item.frequency || 'monthly',
                    item.profile || null,
                    item.category || 'Subscriptions',
                    item.expected_day || (item.next_expected ? Number(String(item.next_expected).slice(8, 10)) : null)
                );
            } else {
                await api.confirmSubscription(item.merchant, null, item.frequency, item.category, item.profile || null);
            }
            recurringData = await api.getRecurring();
            subscriptionFeedback = `✓ ${item.merchant} confirmed`;
            setTimeout(() => { subscriptionFeedback = ''; }, 3000);
        } catch (e) {
            console.error('Failed to confirm subscription:', e);
            subscriptionFeedback = 'Failed to confirm';
            setTimeout(() => { subscriptionFeedback = ''; }, 3000);
        }
    }

    async function handleDismissSubscription(item) {
        try {
            await api.dismissSubscription(item.merchant, null, item.profile || null);
            // Remove from local list immediately
            if (recurringData && recurringData.items) {
	                recurringData = {
	                    ...recurringData,
	                    items: recurringData.items.filter(i => i.merchant !== item.merchant || (i.profile || null) !== (item.profile || null)),
	                    dismissed_count: (recurringData.dismissed_count || 0) + 1,
	                    dismissed: [...(recurringData.dismissed || []), { merchant: item.merchant, dismissed_at: new Date().toISOString(), profile: item.profile || null }],
	                };
                // Recalculate totals
                let newMonthly = 0, newAnnual = 0;
                for (const r of recurringData.items) {
                    if (r.status === 'active' && !r.cancelled) {
                        newAnnual += r.annual_cost;
                        newMonthly += r.annual_cost / 12;
                    }
                }
                recurringData.total_monthly = Math.round(newMonthly * 100) / 100;
                recurringData.total_annual = Math.round(newAnnual * 100) / 100;
                recurringData.active_count = recurringData.items.filter(i => i.status === 'active' && !i.cancelled).length;
            }
            subscriptionFeedback = `✕ ${item.merchant} dismissed`;
            setTimeout(() => { subscriptionFeedback = ''; }, 3000);
        } catch (e) {
            console.error('Failed to dismiss subscription:', e);
            subscriptionFeedback = 'Failed to dismiss';
            setTimeout(() => { subscriptionFeedback = ''; }, 3000);
        }
    }

    async function handleCancelSubscription(item) {
        try {
            const profile = item.profile && item.profile !== 'household'
                ? item.profile
                : ($activeProfile && $activeProfile !== 'household' ? $activeProfile : null);
            await api.cancelSubscription(item.merchant, profile);
            // Mark as cancelled locally
            if (recurringData && recurringData.items) {
                recurringData = {
                    ...recurringData,
                    items: recurringData.items.map(i =>
                        i.merchant === item.merchant && (i.profile || null) === (item.profile || null)
                            ? { ...i, cancelled: true, state: 'cancelled', status: 'cancelled' }
                            : i
                    ),
                    cancelled_count: (recurringData.cancelled_count || 0) + 1,
                    inactive_count: Math.max((recurringData.inactive_count || 0) - 1, 0),
                };
            }
            subscriptionFeedback = `✓ ${item.merchant} marked as cancelled`;
            setTimeout(() => { subscriptionFeedback = ''; }, 3000);
        } catch (e) {
            console.error('Failed to cancel subscription:', e);
            subscriptionFeedback = 'Failed to cancel';
            setTimeout(() => { subscriptionFeedback = ''; }, 3000);
        }
    }

    async function handleRestoreSubscription(item) {
        try {
            const profile = item.profile && item.profile !== 'household'
                ? item.profile
                : ($activeProfile && $activeProfile !== 'household' ? $activeProfile : null);
            await api.restoreSubscription(item.merchant, profile);
            // Refetch recurring data to get fresh state
            recurringLoading = true;
            try {
                recurringData = await api.getRecurring();
            } catch (_) {}
            recurringLoading = false;
            subscriptionFeedback = `✓ ${item.merchant} restored`;
            setTimeout(() => { subscriptionFeedback = ''; }, 3000);
        } catch (e) {
            console.error('Failed to restore subscription:', e);
            subscriptionFeedback = 'Failed to restore';
            setTimeout(() => { subscriptionFeedback = ''; }, 3000);
        }
    }

    async function handleMarkEventRead(event) {
        try {
            await api.markEventsRead([event.id]);
            // Remove from local events list
            if (recurringData && recurringData.events) {
                recurringData = {
                    ...recurringData,
                    events: recurringData.events.map(e =>
                        e.id === event.id ? { ...e, is_read: true } : e
                    ),
                    unread_event_count: Math.max((recurringData.unread_event_count || 0) - 1, 0),
                };
            }
        } catch (e) {
            console.error('Failed to mark event read:', e);
        }
    }

    async function handleMarkAllEventsRead() {
        const ids = subscriptionEvents.map(e => e.id);
        if (ids.length === 0) return;
        try {
            await api.markEventsRead(ids);
            if (recurringData && recurringData.events) {
                recurringData = {
                    ...recurringData,
                    events: recurringData.events.map(e => ({ ...e, is_read: true })),
                    unread_event_count: 0,
                };
            }
        } catch (e) {
            console.error('Failed to mark all events read:', e);
        }
    }

    async function handleRedetectSubscriptions() {
        redetectLoading = true;
        try {
            const profile = $activeProfile && $activeProfile !== 'household' ? $activeProfile : null;
            await api.redetectSubscriptions(profile);
            invalidateCache();
            recurringData = await api.getRecurring();
            if (typeof window !== 'undefined') {
                window.dispatchEvent(new CustomEvent('folio:recurring-updated', {
                    detail: { profile: profile || 'household' }
                }));
            }
            subscriptionFeedback = '✓ Subscriptions re-scanned';
            setTimeout(() => { subscriptionFeedback = ''; }, 3000);
        } catch (e) {
            console.error('Failed to redetect subscriptions:', e);
            subscriptionFeedback = 'Re-detection failed';
            setTimeout(() => { subscriptionFeedback = ''; }, 3000);
        } finally {
            redetectLoading = false;
        }
    }

    function getEventIcon(eventType) {
        switch (eventType) {
            case 'new_detected': return 'add_circle';
            case 'price_increase': return 'trending_up';
            case 'price_decrease': return 'trending_down';
            case 'gone_inactive': return 'pause_circle';
            case 'zombie_charge': return 'warning';
            default: return 'info';
        }
    }

    function getEventColor(eventType) {
        switch (eventType) {
            case 'new_detected': return 'var(--accent)';
            case 'price_increase': return 'var(--negative)';
            case 'price_decrease': return 'var(--positive)';
            case 'gone_inactive': return 'var(--warning)';
            case 'zombie_charge': return 'var(--negative)';
            default: return 'var(--text-muted)';
        }
    }

    function getEventBgColor(eventType) {
        switch (eventType) {
            case 'new_detected': return 'color-mix(in srgb, var(--accent) 8%, transparent)';
            case 'price_increase': return 'color-mix(in srgb, var(--negative) 8%, transparent)';
            case 'price_decrease': return 'color-mix(in srgb, var(--positive) 8%, transparent)';
            case 'gone_inactive': return 'color-mix(in srgb, var(--warning) 8%, transparent)';
            case 'zombie_charge': return 'color-mix(in srgb, var(--negative) 10%, transparent)';
            default: return 'var(--surface-100)';
        }
    }

    function getEventMessage(event) {
        const d = event.detail || {};
        switch (event.event_type) {
            case 'new_detected': return `New subscription detected: ${event.merchant_name}`;
            case 'price_increase': return `${event.merchant_name}: ${formatCurrency(d.old_amount)} → ${formatCurrency(d.new_amount)} (+${formatCurrency(d.change)})`;
            case 'price_decrease': return `${event.merchant_name}: ${formatCurrency(d.old_amount)} → ${formatCurrency(d.new_amount)} (${formatCurrency(d.change)})`;
            case 'gone_inactive': return `${event.merchant_name} appears inactive — no recent charges`;
            case 'zombie_charge': return `⚠️ ${event.merchant_name} charged after being marked cancelled`;
            default: return `${event.merchant_name}: ${event.event_type}`;
        }
    }

    function handleWindowClick() {
        if (monthPickerOpen) monthPickerOpen = false;
        if (selectedCategory) closeDrillDown();
    }    
</script>
<svelte:window on:click={handleWindowClick} />

{#if loading}
    <div class="space-y-6">
        <div class="skeleton h-8 w-40 rounded-xl"></div>
        <div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
            {#each Array(6) as _}
                <div class="skeleton h-28 rounded-xl"></div>
            {/each}
        </div>
        <div class="skeleton h-80 rounded-2xl"></div>
        <div class="skeleton h-40 rounded-2xl"></div>
        <div class="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <div class="skeleton h-52 rounded-2xl"></div>
            <div class="skeleton h-52 rounded-2xl"></div>
        </div>
    </div>
{:else}
<div class="profile-transition" class:profile-loading={profileSwitching}>

    <!-- --- HEADER --- -->
    <div class="flex items-start justify-between mb-4 fade-in" style="position: relative; z-index: 100;">
        <div>
            <p class="folio-kicker mb-1.5" style="color: var(--accent)">Insights</p>
            <h2 class="folio-page-title">
                Analytics
            </h2>
            <p class="folio-page-subtitle">What your data means and what to do about it</p>
        </div>
        <ProfileSwitcher />
    </div>

    <!-- ═══════════════════════════════════════
         S1: CASH FLOW WATERFALL (Hero)
         ═══════════════════════════════════════ -->
    <!-----------------------------------------
         S0: HERO SUMMARY HEADLINE
         ----------------------------------------->
    {#if currentMonthSummary}
        {@const allTimeAvgExpenses = monthly.length > 0 ? monthly.reduce((s, m) => s + m.expenses, 0) / monthly.length : 0}
        {@const expVsAvgPct = allTimeAvgExpenses > 0 ? ((currentMonthSummary.expenses - allTimeAvgExpenses) / allTimeAvgExpenses) * 100 : 0}
        {@const extTransfers = currentMonthSummary.external_transfers || 0}
        {@const currentSavingsRate = currentMonthSummary.income > 0 ? Math.max(((currentMonthSummary.income - currentMonthSummary.expenses - extTransfers) / currentMonthSummary.income) * 100, 0) : 0}
        {@const creditsRefunds = currentMonthSummary.credits_refunds ?? currentMonthSummary.refunds ?? 0}
        {@const incomingTransfers = currentMonthSummary.incoming_transfers || 0}
        {@const netFlow = waterfallData?.netResult ?? ((currentMonthSummary.income || 0) - (currentMonthSummary.expenses || 0) + creditsRefunds + incomingTransfers - extTransfers)}
        <div class="analytics-period-row fade-in-up" style="animation-delay: 20ms">
            <div class="period-toggle-track" style="--seg-count: {analyticsPeriods.length}; --active-idx: {activeAnalyticsPeriodIdx};">
                <div class="period-toggle-thumb"></div>
                {#each analyticsPeriods as period}
                    <button class="period-toggle-label" class:active={selectedAnalyticsPeriod === period}
                        on:click={() => selectAnalyticsPeriod(period)}>
                        {period}
                    </button>
                {/each}
            </div>

            <div class="analytics-month-picker">
                <button class="analytics-month-picker-btn"
                    class:ring-2={selectedAnalyticsPeriod === 'Custom'}
                    class:ring-accent={selectedAnalyticsPeriod === 'Custom'}
                    on:click|stopPropagation={() => { monthPickerOpen = !monthPickerOpen; selectedAnalyticsPeriod = 'Custom'; }}>
                    <span class="text-[12px] font-medium" style="color: var(--text-primary)">{formatMonth(selectedMonth)}</span>
                    <span class="material-symbols-outlined text-[16px]" style="color: var(--text-muted); transition: transform 0.2s;"
                        class:rotate-180={monthPickerOpen}>
                        expand_more
                    </span>
                </button>
                {#if monthPickerOpen}
                    <!-- svelte-ignore a11y-click-events-have-key-events -->
                    <div class="analytics-month-picker-dropdown" role="presentation" on:click|stopPropagation>
                        {#each monthPickerMonths as m}
                            <button
                                class="analytics-month-picker-option"
                                class:active={m.month === selectedMonth}
                                on:click={() => { selectedMonth = m.month; monthPickerOpen = false; selectedAnalyticsPeriod = 'Custom'; }}>
                                {formatMonth(m.month)}
                                {#if m.month === selectedMonth}
                                    <span class="material-symbols-outlined text-[14px]" style="color: var(--accent)">check</span>
                                {/if}
                            </button>
                        {/each}
                    </div>
                {/if}
            </div>
        </div>
        <section class="mb-8 fade-in-up" style="animation-delay: 30ms">
            <div class="analytics-hero-strip">
                <div class="analytics-hero-summary-row">
                    <div class="analytics-hero-headline">
                        <span class="analytics-hero-kicker">{formatMonth(selectedMonth)} · Mira briefing</span>
                        <h3>{monthExplanation ? monthExplanation.question : 'What your month is saying.'}</h3>
                        {#if explainingMonth && !monthExplanation}
                            <p>Loading month signals...</p>
                        {/if}
                    </div>
                    <div class="analytics-hero-metrics">
                        <div class="analytics-hero-metric">
                            <span class="analytics-hero-metric-label">Total Expenses</span>
                            <span class="analytics-hero-metric-value text-negative">{formatCurrency(currentMonthSummary.expenses)}</span>
                        </div>
                        <div class="analytics-hero-metric">
                            <span class="analytics-hero-metric-label">vs Average</span>
                            <span class="analytics-hero-metric-value" style="color: {expVsAvgPct <= 0 ? 'var(--positive)' : 'var(--negative)'}">
                                {expVsAvgPct <= 0 ? '▼' : '▲'} {formatPercent(Math.abs(expVsAvgPct))}
                            </span>
                        </div>
                        <div class="analytics-hero-metric">
                            <span class="analytics-hero-metric-label">Savings Rate</span>
                            <span class="analytics-hero-metric-value" style="color: var(--accent)">{formatPercent(currentSavingsRate)}</span>
                        </div>
                        <div class="analytics-hero-metric">
                            <span class="analytics-hero-metric-label">Net Flow</span>
                            <span class="analytics-hero-metric-value" class:analytics-positive={netFlow >= 0} class:analytics-negative={netFlow < 0}>
                                {netFlow >= 0 ? '+' : ''}{formatCurrency(netFlow)}
                            </span>
                        </div>
                    </div>
                </div>
                {#if analyticsHeroDisplayedSections.length > 0}
                    <div class="analytics-hero-insights">
                        {#each analyticsHeroDisplayedSections as section}
                            <div class="analytics-mira-insight analytics-mira-insight-{section.tone}">
                                <span class="material-symbols-outlined">{section.icon}</span>
                                <div>
                                    <strong>{section.label}</strong>
                                    {#if section.label === 'Facts' && analyticsHeroFactSummary}
                                        <div class="analytics-hero-facts-compact">
                                            <div class="analytics-hero-fact-line analytics-hero-fact-line-primary">
                                                <svg viewBox="0 0 8 8" aria-hidden="true"><path d="M2 1.25 6 4 2 6.75Z" /></svg>
                                                <p>{analyticsHeroFactSummary.recurring}</p>
                                            </div>
                                            {#if analyticsHeroFactSummary.categories}
                                                <div class="analytics-hero-fact-line">
                                                    <svg viewBox="0 0 8 8" aria-hidden="true"><path d="M2 1.25 6 4 2 6.75Z" /></svg>
                                                    <p>{analyticsHeroFactSummary.categories}</p>
                                                </div>
                                            {/if}
                                            {#if analyticsHeroFactSummary.merchants}
                                                <div class="analytics-hero-fact-line">
                                                    <svg viewBox="0 0 8 8" aria-hidden="true"><path d="M2 1.25 6 4 2 6.75Z" /></svg>
                                                    <p>{analyticsHeroFactSummary.merchants}</p>
                                                </div>
                                            {/if}
                                        </div>
                                    {:else}
                                        <div class="analytics-hero-facts-compact">
                                            {#each analyticsHeroBulletRows(section) as row, i}
                                                <div class="analytics-hero-fact-line" class:analytics-hero-fact-line-primary={i === 0}>
                                                    <svg viewBox="0 0 8 8" aria-hidden="true"><path d="M2 1.25 6 4 2 6.75Z" /></svg>
                                                    <p>{row}</p>
                                                </div>
                                            {/each}
                                        </div>
                                    {/if}
                                </div>
                            </div>
                        {/each}
                    </div>
                {/if}
                {#if explanationError}
                    <div class="analytics-mira-error">{explanationError}</div>
                {/if}
            </div>
        </section>
    {/if}

    {#if selectedMonth}
        <section class="mb-10 fade-in-up" style="animation-delay: 60ms">
            <div class="flex flex-col gap-3 mb-1 sm:flex-row sm:items-center sm:justify-between" style="position: relative; z-index: 90;">
                <div class="analytics-section-header" style="margin-bottom:0">
                    <h3 class="analytics-section-title">Cash Flow Waterfall</h3>
                </div>
            </div>
            <p class="text-[11px] mb-4 ml-6" style="color: var(--text-muted)">
                Your money, step by step - {formatMonth(selectedMonth)}
            </p>

            <div class="card analytics-waterfall-theater" style="padding: 1rem 0.5rem 0.5rem">
                {#if waterfallData && waterfallGeometry}
                <div bind:this={waterfallEl} class="analytics-waterfall-container" style="position: relative;">
                    <svg width="100%" viewBox="0 0 {waterfallGeometry.W} {waterfallGeometry.H}" preserveAspectRatio="xMidYMid meet">
                        <defs>
                            <filter id="wfBarGlow" x="-20%" y="-20%" width="140%" height="140%">
                                <feGaussianBlur in="SourceGraphic" stdDeviation="6" result="blur"/>
                                <feMerge>
                                    <feMergeNode in="blur"/>
                                    <feMergeNode in="SourceGraphic"/>
                                </feMerge>
                            </filter>
                            <filter id="wfBarSoft" x="-10%" y="-10%" width="120%" height="120%">
                                <feGaussianBlur in="SourceGraphic" stdDeviation={$darkMode ? 3 : 1.5}/>
                            </filter>
                        </defs>

                        <!-- Grid lines -->
                        {#each waterfallGeometry.gridLines as gl}
                            <line x1="36" y1={gl.y} x2={waterfallGeometry.W - 10} y2={gl.y}
                                stroke="var(--text-muted)" stroke-width="0.5" opacity="0.08" />
                            <text x="32" y={gl.y + 3} text-anchor="end"
                                fill="var(--text-muted)" font-size="8" font-family="DM Mono, monospace" opacity="0.4">
                                {gl.label}
                            </text>
                        {/each}

                        <!-- Zero line -->
                        <line x1="36" y1={waterfallGeometry.zeroY} x2={waterfallGeometry.W - 10} y2={waterfallGeometry.zeroY}
                            stroke="var(--text-muted)" stroke-width="1" opacity="0.15" stroke-dasharray="4,4" />

                        <!-- Bridge connectors -->
                        {#each waterfallGeometry.bridges as bridge}
                            <line x1={bridge.x1} y1={bridge.y} x2={bridge.x2} y2={bridge.y}
                                stroke="var(--text-muted)" stroke-width="1" opacity="0.12" stroke-dasharray="3,3" />
                        {/each}

                        <!-- Bar glow underlayer -->
                        {#each waterfallGeometry.bars as bar}
                            {#if bar.type !== 'anchor'}
                                <rect
                                    x={bar.x - 2} y={bar.y - 2}
                                    width={bar.barWidth + 4} height={bar.h + 4}
                                    rx="6" fill={bar.color}
                                    opacity={$darkMode ? 0.08 : 0.04}
                                    filter="url(#wfBarSoft)"
                                />
                            {/if}
                        {/each}

                        <!-- Bars -->
                        {#each waterfallGeometry.bars as bar, i}
                            {#if bar.type !== 'anchor'}
                                <rect
                                    x={bar.x} y={bar.y}
                                    width={bar.barWidth} height={bar.h}
                                    rx="4" fill={bar.color}
                                    stroke="none"
                                    opacity={bar.type === 'result' ? 0.90 : 0.75}
                                    class="analytics-wf-bar"
                                    style="cursor: {isWaterfallBarClickable(bar) ? 'pointer' : 'default'}"
                                    role="button"
                                    tabindex="0"
                                    aria-label={`${bar.label}: ${formatCurrency(bar.value)}`}
                                    on:mousedown|preventDefault={() => {}}
                                    on:mouseenter={(e) => handleWaterfallHover(bar, e)}
                                    on:mouseleave={handleWaterfallLeave}
                                    on:click|stopPropagation={() => handleWaterfallClick(bar)}
                                    on:keydown={(e) => handleWaterfallKeydown(e, bar)}
                                />
                                <!-- Top edge highlight -->
                                <line
                                    x1={bar.x + 3} y1={bar.y + 0.5}
                                    x2={bar.x + bar.barWidth - 3} y2={bar.y + 0.5}
                                    stroke="white" stroke-width="0.5" opacity="0.2" stroke-linecap="round"
                                />
                            {/if}

                            <!-- Label below -->
                            <text x={bar.x + bar.barWidth / 2} y={waterfallGeometry.H - waterfallGeometry.padBottom + 16}
                                text-anchor="middle" fill="var(--text-muted)"
                                font-size="8.5" font-family="Inter, system-ui, sans-serif" font-weight="500">
                                {bar.label.length > 9 ? bar.label.slice(0, 8) + '…' : bar.label}
                            </text>

                            <!-- Value above/below bar -->
                            {#if bar.type !== 'anchor'}
                                <text x={bar.x + bar.barWidth / 2}
                                    y={(bar.value >= 0 || bar.type === 'result' && bar.value >= 0) ? bar.y - 6 : bar.y + bar.h + 12}
                                    text-anchor="middle" fill={bar.color}
                                    font-size="8" font-family="DM Mono, monospace" font-weight="500" opacity="0.8">
                                    {bar.value >= 0 ? '+' : ''}{formatCompact(bar.value)}
                                </text>
                            {/if}
                        {/each}
                    </svg>

                    <!-- Tooltip -->
                    <!-- Tooltip (always mounted, visibility via class) -->
                    <div class="analytics-wf-tooltip"
                        class:wf-tooltip-visible={waterfallTooltip.show}
                        style="left: {waterfallTooltip.x}px; top: {waterfallTooltip.y}px;">
                        <p class="text-[11px] font-semibold" style="color: var(--text-primary)">{waterfallTooltip.label}</p>
                        <p class="text-[12px] font-mono font-bold" style="color: {waterfallTooltip.amount >= 0 ? 'var(--positive)' : 'var(--negative)'}">
                            {waterfallTooltip.amount >= 0 ? '+' : ''}{formatCurrency(waterfallTooltip.amount)}
                        </p>
                        {#if waterfallTooltip.type !== 'income' && waterfallTooltip.type !== 'result'}
                            <p class="text-[9px]" style="color: var(--text-muted)">
                                {formatCompact(waterfallTooltip.runningFrom)} â {formatCompact(waterfallTooltip.runningTo)}
                            </p>
                        {/if}
                        {#if waterfallTooltip.count > 0}
                            <p class="text-[9px]" style="color: var(--text-muted)">{waterfallTooltip.count} transactions</p>
                        {/if}
                    </div>
                </div>

                <!-- Summary ribbon -->
                <div class="analytics-wf-summary">
                    <div class="analytics-wf-summary-item">
                        <span class="analytics-wf-summary-label">Income</span>
                        <span class="analytics-wf-summary-value text-positive">+{formatCurrency(currentMonthSummary.income)}</span>
                    </div>
                    {#if ((currentMonthSummary.credits_refunds ?? currentMonthSummary.refunds ?? 0) > 0)}
                        <div class="analytics-wf-summary-item">
                            <span class="analytics-wf-summary-label">Credits</span>
                            <span class="analytics-wf-summary-value text-positive">+{formatCurrency(currentMonthSummary.credits_refunds ?? currentMonthSummary.refunds ?? 0)}</span>
                        </div>
                    {/if}
                    {#if (currentMonthSummary.incoming_transfers || 0) > 0}
                        <div class="analytics-wf-summary-item">
                            <span class="analytics-wf-summary-label">Incoming Transfers</span>
                            <span class="analytics-wf-summary-value" style="color: var(--flow-transfer)">+{formatCurrency(currentMonthSummary.incoming_transfers)}</span>
                        </div>
                    {/if}
                    <div class="analytics-wf-summary-item">
                        <span class="analytics-wf-summary-label">Spending</span>
                        <span class="analytics-wf-summary-value text-negative">-{formatCurrency(currentMonthSummary.expenses)}</span>
                    </div>
                    {#if (currentMonthSummary.external_transfers || 0) > 0}
                        <div class="analytics-wf-summary-item">
                            <span class="analytics-wf-summary-label">Ext. Transfers</span>
                            <span class="analytics-wf-summary-value" style="color: var(--warning)">-{formatCurrency(currentMonthSummary.external_transfers)}</span>
                        </div>
                    {/if}
                    <div class="analytics-wf-summary-item">
                        <span class="analytics-wf-summary-label">Net Flow</span>
                        <span class="analytics-wf-summary-value" style="color: {waterfallData.netResult >= 0 ? 'var(--positive)' : 'var(--negative)'}">
                            {waterfallData.netResult >= 0 ? '+' : ''}{formatCurrency(waterfallData.netResult)}
                        </span>
                    </div>
                </div>
                {:else}
                    <div class="analytics-waterfall-empty">
                        No cash flow yet for {formatMonth(selectedMonth)}
                    </div>
                {/if}
            </div>

            <!-- ── Contextual Drill-Down (inside waterfall section) ── -->
            {#if selectedCategory && categoryTransactions.length > 0}
                <!-- svelte-ignore a11y-click-events-have-key-events -->
                <div class="analytics-waterfall-drilldown" role="presentation" on:click|stopPropagation>
                    <div class="analytics-waterfall-drilldown-header">
                        <div class="flex items-center gap-2.5">
                            <div class="w-6 h-6 rounded-md flex items-center justify-center"
                                style="background: color-mix(in srgb, {CATEGORY_COLORS[selectedCategory] || '#627d98'} 12%, transparent)">
                                <span class="material-symbols-outlined text-[13px]"
                                    style="color: {CATEGORY_COLORS[selectedCategory] || '#627d98'}">
                                    {CATEGORY_ICONS[selectedCategory] || 'label'}
                                </span>
                            </div>
                            <div>
                                <h4 style="margin: 0; text-transform: none; letter-spacing: 0; font-size: 0.8125rem; color: var(--text-primary)">
                                    {selectedCategory}
                                </h4>
                                <p class="text-[10px]" style="color: var(--text-muted); margin: 0">
                                    {categoryTransactions.length} transactions · {formatMonth(selectedMonth)}
                                </p>
                            </div>
                        </div>
                        <button class="analytics-waterfall-drilldown-close" on:click={closeDrillDown}>
                            <span class="flex items-center gap-1">
                                <span class="material-symbols-outlined text-[12px]">close</span>
                                Close
                            </span>
                        </button>
                    </div>
                    <div class="analytics-waterfall-drilldown-body">
                        {#each categoryTransactions.slice(0, 12) as tx}
                            {@const amount = Math.abs(parseFloat(tx.amount))}
                            <div class="analytics-waterfall-drilldown-row">
                                <div class="flex-1 min-w-0">
                                    <p class="text-[12px] font-medium truncate" style="color: var(--text-primary)">{tx.description}</p>
                                    <p class="text-[9px]" style="color: var(--text-muted)">{formatDate(tx.date)} · {tx.account_name}</p>
                                </div>
                                <p class="text-[12px] font-bold font-mono text-negative flex-shrink-0">{formatCurrency(amount, 2)}</p>
                            </div>
                        {/each}
                    </div>
                    {#if categoryTransactions.length > 12}
                        <div class="px-4 py-2.5 text-center" style="border-top: 1px solid var(--border-subtle)">
                            <a href="/transactions" class="text-[11px] font-medium" style="color: var(--accent)">
                                View all {categoryTransactions.length} transactions →
                            </a>
                        </div>
                    {/if}
                </div>
            {/if}
        </section>
    {/if}

    <!-- ═══════════════════════════════════════
         TOP MERCHANTS + RECURRING SUBSCRIPTIONS (paired)
         ═══════════════════════════════════════ -->
    {#if topMerchants.length > 0 || visibleRecurringItems.length > 0}
    <div class="analytics-paired-layout mb-10" class:analytics-paired-scroll={activeRecurring.length >= 6}>
        {#if topMerchants.length > 0}
        <section class="analytics-paired-col fade-in-up" style="animation-delay: 120ms">
            <div class="analytics-section-header analytics-section-header-tight">
                <h3 class="analytics-section-title">Top Merchants</h3>
                {#if merchantInsight}<p class="analytics-section-context">{merchantInsight}</p>{/if}
            </div>

            <div class="card analytics-merchant-list" style="padding: 0; overflow: hidden; {activeRecurring.length >= 6 && subsCollapsedHeight > 0 ? `height: ${subsCollapsedHeight}px; overflow-y: auto;` : ''}">
                <div class="analytics-merchant-story">
                    <div>
                        <p class="analytics-kicker">{formatMonthShort(selectedMonth)} · {topMerchants.length} merchants</p>
                        <h4 class="analytics-editorial-line">Where your <em>money</em> went</h4>
                    </div>
                    {#if merchantBrief}
                        <div class="analytics-merchant-story-metric">
                            <strong>{formatCurrency(merchantBrief.total)}</strong>
                            <span>top {merchantBrief.n} = {merchantBrief.share}%</span>
                        </div>
                    {/if}
                </div>
                {#if merchantBrief}
                    <div class="analytics-merchant-stack" aria-hidden="true">
                        {#each topMerchants.slice(0, 8) as merchant, i}
                            {@const segmentPct = merchantBrief.total > 0 ? (merchant.total_spent / merchantBrief.total) * 100 : 0}
                            <span style="width: {segmentPct}%; --segment-color: {CATEGORY_COLORS[merchant.industry] || CATEGORY_COLORS[merchant.category] || merchantPalette[i % merchantPalette.length]}"></span>
                        {/each}
                    </div>
                    <div class="analytics-merchant-legend">
                        {#each topMerchants.slice(0, 3) as merchant, i}
                            <span><i style="background: {CATEGORY_COLORS[merchant.industry] || CATEGORY_COLORS[merchant.category] || merchantPalette[i % merchantPalette.length]}"></i>{merchant.name}</span>
                        {/each}
                        {#if topMerchants.length > 3}
                            <span>+ {topMerchants.length - 3} others</span>
                        {/if}
                    </div>
                {/if}
                <div class="analytics-merchant-header">
                    <span></span>
                    <span>Merchant</span>
                    <span>Visits · Share</span>
                    <span>Amount</span>
                </div>
                {#each topMerchants.slice(0, 8) as merchant, i}
                    {@const barPct = (merchant.total_spent / (topMerchants[0]?.total_spent || 1)) * 100}
                    {@const totalPct = currentMonthSummary?.expenses ? Math.round((merchant.total_spent / currentMonthSummary.expenses) * 100) : 0}
                    <div class="analytics-merchant-row">
                        <span class="analytics-merchant-rank">{String(i + 1).padStart(2, '0')}</span>
                        <div class="analytics-merchant-body">
                            <span class="analytics-merchant-name">{merchant.name}</span>
                            {#if merchant.industry}
                                <span class="analytics-merchant-caption">{merchant.industry}</span>
                            {/if}
                        </div>
                        <div class="analytics-merchant-share">
                            <span>{merchant.transaction_count} txn{merchant.transaction_count !== 1 ? 's' : ''}</span>
                            <div class="analytics-merchant-bar-track">
                                <div class="analytics-merchant-bar-fill" style="width: {barPct}%"></div>
                            </div>
                            <span>{totalPct}%</span>
                        </div>
                        <div class="analytics-merchant-right">
                            <span class="analytics-merchant-amount">{formatCurrency(merchant.total_spent)}</span>
                        </div>
                    </div>
                {/each}
            </div>
        </section>
        {/if}

        {#if visibleRecurringItems.length > 0}
        <section class="analytics-paired-col fade-in-up" style="animation-delay: 130ms">
            <div class="analytics-section-header analytics-section-header-tight" style="margin-bottom:0.75rem">
                <div class="flex items-center gap-2 flex-wrap">
                    <h3 class="analytics-section-title">Recurring Subscriptions</h3>
                    {#if unreadEventCount > 0}
                        <span class="analytics-event-count-badge">{unreadEventCount}</span>
                    {/if}
                    {#if priceChangeCount > 0}
                        <span class="text-[10px]" style="color: var(--negative)">{priceChangeCount} price increase{priceChangeCount !== 1 ? 's' : ''}</span>
                    {/if}
                </div>
                <div class="flex items-center gap-2 ml-auto">
                    <button class="analytics-redetect-btn" on:click|stopPropagation={handleRedetectSubscriptions}
                        disabled={redetectLoading} title="Re-scan for subscriptions">
                        <span class="material-symbols-outlined text-[14px]"
                            class:animate-spin={redetectLoading}
                            style="color: var(--text-muted)">refresh</span>
                    </button>
                    {#if subscriptionFeedback}
                        <span class="text-[10px] font-medium px-2 py-1 rounded-lg fade-in"
                            style="background: var(--surface-100); color: var(--text-secondary)">
                            {subscriptionFeedback}
                        </span>
                    {/if}
                </div>
            </div>
            <div class="card analytics-subscription-stage" style="padding: 0; overflow: hidden" bind:clientHeight={subsCardHeight}>
                <div class="analytics-subscription-hero">
                    <div>
                        <p class="analytics-kicker">Recurring subscriptions</p>
                        <h4 class="analytics-editorial-line">Your <em>subscription stack</em></h4>
                    </div>
                    <div class="analytics-subscription-counts">
                        <span>{visibleRecurringTotals.active} active</span>
                        {#if inactiveRecurring.length > 0}<span>{inactiveRecurring.length} inactive</span>{/if}
                    </div>
                </div>
                <div class="analytics-subscription-metrics">
                    <div>
                        <span>Monthly</span>
                        <strong>{formatCurrency(visibleRecurringTotals.monthly)}</strong>
                        <small>{visibleRecurringTotals.active} active</small>
                    </div>
                    <div>
                        <span>Annualized</span>
                        <strong>{formatCurrency(visibleRecurringTotals.annual)}</strong>
                        <small>{currentMonthSummary?.expenses ? formatPercent((visibleRecurringTotals.monthly / currentMonthSummary.expenses) * 100) : '0%'} of spend</small>
                    </div>
	                    <div class="attention">
	                        <span>Inactive paid</span>
	                        <strong>{formatCurrency(inactiveTotalSpent)}</strong>
	                        <small>{inactiveRecurring.length} inactive · review</small>
	                    </div>
                </div>
                <!-- Subscription Events / Alerts Strip -->
                {#if subscriptionEvents.length > 0}
                    <div class="analytics-events-strip">
                        <div class="analytics-events-strip-header">
                            <span class="text-[9px] font-bold tracking-[0.1em] uppercase" style="color: var(--text-muted)">
                                Recent Alerts
                            </span>
                            {#if subscriptionEvents.length > 1}
                                <button class="analytics-events-dismiss-all" on:click|stopPropagation={handleMarkAllEventsRead}>
                                    Dismiss all
                                </button>
                            {/if}
                        </div>
                        {#each subscriptionEvents.slice(0, 5) as event}
                            <div class="analytics-event-card"
                                style="--event-color: {getEventColor(event.event_type)}; background: {getEventBgColor(event.event_type)}">
                                <div class="flex items-center gap-2.5 flex-1 min-w-0">
                                    <span class="material-symbols-outlined text-[16px]" style="color: var(--event-color)">
                                        {getEventIcon(event.event_type)}
                                    </span>
                                    <div class="min-w-0 flex-1">
                                        <p class="text-[11px] font-medium" style="color: var(--text-primary)">
                                            {getEventMessage(event)}
                                        </p>
                                        <p class="text-[9px]" style="color: var(--text-muted)">
                                            {event.created_at ? formatDateWithYear(event.created_at) : ''}
                                        </p>
                                    </div>
                                </div>
                                <button class="analytics-event-dismiss" on:click|stopPropagation={() => handleMarkEventRead(event)}
                                    title="Dismiss">
                                    <span class="material-symbols-outlined text-[12px]">close</span>
                                </button>
                            </div>
                        {/each}
                        {#if subscriptionEvents.length > 5}
                            <p class="text-[9px] px-4 py-1.5" style="color: var(--text-muted)">
                                + {subscriptionEvents.length - 5} more alerts
                            </p>
                        {/if}
                    </div>
                {/if}

                <!-- Active subscriptions -->
                <div class="analytics-sub-header">
                    <span>Merchant</span>
                    <span>Freq</span>
                    <span>Amount</span>
	                    <span>Annual / paid</span>
                    <span>Status</span>
                    <span></span>
                </div>
                {#each activeRecurring.slice(0, 10) as item, i}
                    <div class="analytics-sub-row" style="border-bottom: {i < Math.min(activeRecurring.length, 10) - 1 ? '1px solid color-mix(in srgb, var(--card-border) 50%, transparent)' : 'none'}">
                        <div class="analytics-sub-left">
                            <div class="analytics-sub-avatar"
                                style="background: color-mix(in srgb, {CATEGORY_COLORS[item.category] || '#627d98'} 10%, transparent)">
                                {#if item.logo_url}
                                    <img src={item.logo_url} alt="" class="w-4 h-4 rounded" style="object-fit: contain" />
                                {:else}
                                    <span class="material-symbols-outlined text-[12px]"
                                        style="color: {CATEGORY_COLORS[item.category] || '#627d98'}">
                                        {item.is_subscription ? 'subscriptions' : 'event_repeat'}
                                    </span>
                                {/if}
                            </div>
                            <div class="analytics-sub-body">
                                <span class="analytics-sub-name">{item.clean_name || item.merchant}</span>
                                {#if item.price_change}
                                    <span class="analytics-sub-meta">
                                        <span class="analytics-recurring-price-change" class:price-up={item.price_change.change > 0} class:price-down={item.price_change.change < 0}
                                            title="{item.price_change.change > 0 ? 'Price increased' : 'Price decreased'}: {formatCurrency(item.price_change.previous)} → {formatCurrency(item.price_change.current)}">
                                            <span class="material-symbols-outlined text-[9px]">{item.price_change.change > 0 ? 'trending_up' : 'trending_down'}</span>
	                                            {item.price_change.change > 0 ? '+' : ''}{formatCurrency(item.price_change.change)}
	                                        </span>
	                                    </span>
	                                {/if}
                                    {#if subscriptionTimingText(item)}
                                        <span class="analytics-sub-meta">{subscriptionTimingText(item)}</span>
                                    {/if}
	                            </div>
	                        </div>
	                        <span class="analytics-sub-col">{item.frequency}</span>
	                        <span class="analytics-sub-col analytics-sub-col-stack">
                                <strong>{formatCurrency(recurringDisplayAmount(item))}</strong>
                                {#if recurringAmountNote(item)}
                                    <small>{recurringAmountNote(item)}</small>
                                {/if}
                            </span>
		                    <span class="analytics-sub-col">{formatCurrency(recurringAnnualCost(item))}</span>
                        <span class="analytics-sub-col analytics-sub-col-status">
                            {#if item.confidence === 'user'}
                                <span class="analytics-sub-dot" style="background: #8b5cf6"></span> User
                            {:else if item.confidence === 'high'}
                                <span class="analytics-sub-dot" style="background: var(--positive)"></span> High
                            {:else}
                                <span class="analytics-sub-dot" style="background: var(--warning)"></span> Low
                            {/if}
	                        </span>
	                        <div class="analytics-sub-actions">
                                <button class="analytics-recurring-action-btn analytics-recurring-cancel-btn"
                                    title="Mark cancelled"
                                    aria-label="Mark cancelled"
                                    on:click|stopPropagation={() => handleCancelSubscription(item)}>
                                    <span class="material-symbols-outlined text-[13px]">close</span>
                                </button>
                        </div>
                    </div>
                {/each}

                {#if activeRecurring.length > 10}
                    <div class="px-5 py-2 text-center" style="border-top: 1px solid color-mix(in srgb, var(--card-border) 50%, transparent)">
                        <span class="text-[10px]" style="color: var(--text-muted)">+ {activeRecurring.length - 10} more active</span>
                    </div>
                {/if}

                {#if candidateRecurring.length > 0}
                    <div class="analytics-recurring-inactive-toggle"
                         style="border-top: 1px solid var(--card-border); background: var(--surface-100)">
                        <button class="analytics-recurring-inactive-trigger"
                            on:click|stopPropagation={() => { candidateOpen = !candidateOpen; }}>
                            <div class="flex items-center gap-2">
                                <span class="material-symbols-outlined text-[14px]" style="color: var(--warning)">rule</span>
                                <span class="text-[9px] font-bold tracking-[0.1em] uppercase" style="color: var(--text-muted)">
                                    Needs review
                                </span>
                                <span class="text-[9px] font-mono font-bold px-1.5 py-0.5 rounded-full"
                                    style="background: var(--surface-200); color: var(--warning)">
                                    {candidateRecurring.length}
                                </span>
                            </div>
                            <span class="text-[9px] font-mono" style="color: var(--text-muted)">not counted as active</span>
                        </button>
                    </div>
                    {#if candidateOpen}
                        <div class="analytics-recurring-inactive-body">
                            {#each candidateRecurring as item, i}
                                <div class="analytics-sub-row analytics-sub-row-inactive" style="border-bottom: {i < candidateRecurring.length - 1 ? '1px solid color-mix(in srgb, var(--card-border) 30%, transparent)' : 'none'}">
                                    <div class="analytics-sub-left">
                                        <div class="analytics-sub-avatar"
                                            style="background: color-mix(in srgb, {CATEGORY_COLORS[item.category] || '#627d98'} 6%, transparent)">
                                            <span class="material-symbols-outlined text-[12px]"
                                                style="color: {CATEGORY_COLORS[item.category] || '#627d98'}; opacity: 0.7">
                                                help
                                            </span>
                                        </div>
	                                        <div class="analytics-sub-body">
	                                            <span class="analytics-sub-name">{item.clean_name || item.merchant}</span>
	                                            {#if item.confidence_score}
	                                                <span class="analytics-sub-meta">Score {Math.round(item.confidence_score)}</span>
	                                            {/if}
                                                {#if subscriptionTimingText(item)}
                                                    <span class="analytics-sub-meta">{subscriptionTimingText(item)}</span>
                                                {/if}
	                                        </div>
	                                    </div>
	                                    <span class="analytics-sub-col">{item.frequency}</span>
	                                    <span class="analytics-sub-col analytics-sub-col-stack">
                                            <strong>{formatCurrency(recurringDisplayAmount(item))}</strong>
                                            {#if recurringAmountNote(item)}
                                                <small>{recurringAmountNote(item)}</small>
                                            {/if}
                                        </span>
		                                    <span class="analytics-sub-col">{formatCurrency(item.total_spent || ((item.amount || 0) * (item.charge_count || 0)))}</span>
                                    <span class="analytics-sub-col analytics-sub-col-status">
                                        <span class="analytics-sub-dot" style="background: var(--warning)"></span> Candidate
                                    </span>
                                    <div class="analytics-sub-actions">
                                        <button class="analytics-recurring-action-btn"
                                            title="Confirm subscription"
                                            on:click|stopPropagation={() => handleConfirmSubscription(item)}>
                                            <span class="material-symbols-outlined text-[13px]">check</span>
                                        </button>
                                        <button class="analytics-recurring-action-btn analytics-recurring-dismiss"
                                            title="Not a subscription — dismiss"
                                            on:click|stopPropagation={() => handleDismissSubscription(item)}>
                                            <span class="material-symbols-outlined text-[13px]">close</span>
                                        </button>
                                    </div>
                                </div>
                            {/each}
                        </div>
                    {/if}
                {/if}

                <!-- Inactive subscriptions (collapsible dropdown, closed by default) -->
                {#if inactiveRecurring.length > 0}
                    <div class="analytics-recurring-inactive-toggle"
                         style="border-top: 1px solid var(--card-border); background: var(--surface-100)">
                        <button class="analytics-recurring-inactive-trigger"
                            on:click|stopPropagation={() => { inactiveOpen = !inactiveOpen; }}>
                            <div class="flex items-center gap-2">
                                <span class="material-symbols-outlined text-[14px]" style="color: var(--warning)">
                                    {inactiveOpen ? 'expand_less' : 'expand_more'}
                                </span>
                                <span class="text-[9px] font-bold tracking-[0.1em] uppercase" style="color: var(--text-muted)">
                                    Inactive / Possibly Cancelled
                                </span>
                                <span class="text-[9px] font-mono font-bold px-1.5 py-0.5 rounded-full"
                                    style="background: var(--surface-200); color: var(--warning)">
                                    {inactiveRecurring.length}
                                </span>
                            </div>
                            <span class="text-[9px] font-mono" style="color: var(--text-muted)">confirm or dismiss</span>
                        </button>
                    </div>
                    {#if inactiveOpen}
                        <div class="analytics-recurring-inactive-body">
                            {#each inactiveRecurring as item, i}
                                <div class="analytics-sub-row analytics-sub-row-inactive" style="border-bottom: {i < inactiveRecurring.length - 1 ? '1px solid color-mix(in srgb, var(--card-border) 30%, transparent)' : 'none'}">
                                    <div class="analytics-sub-left">
                                        <div class="analytics-sub-avatar"
                                            style="background: color-mix(in srgb, {CATEGORY_COLORS[item.category] || '#627d98'} 6%, transparent)">
                                            {#if item.logo_url}
                                                <img src={item.logo_url} alt="" class="w-4 h-4 rounded" style="object-fit: contain; opacity: 0.5" />
                                            {:else}
                                                <span class="material-symbols-outlined text-[12px]"
                                                    style="color: {CATEGORY_COLORS[item.category] || '#627d98'}; opacity: 0.5">
                                                    {item.is_subscription ? 'subscriptions' : 'event_repeat'}
                                                </span>
                                            {/if}
                                        </div>
	                                        <div class="analytics-sub-body">
	                                            <span class="analytics-sub-name" style="color: var(--text-muted)">{item.clean_name || item.merchant}</span>
                                                {#if subscriptionTimingText(item)}
                                                    <span class="analytics-sub-meta">{subscriptionTimingText(item)}</span>
                                                {/if}
	                                        </div>
	                                    </div>
	                                    <span class="analytics-sub-col">{item.frequency}</span>
	                                    <span class="analytics-sub-col analytics-sub-col-stack">
                                            <strong>{formatCurrency(recurringDisplayAmount(item))}</strong>
                                            {#if recurringAmountNote(item)}
                                                <small>{recurringAmountNote(item)}</small>
                                            {/if}
                                        </span>
		                                    <span class="analytics-sub-col">{formatCurrency(item.total_spent || ((item.amount || 0) * (item.charge_count || 0)))}</span>
                                    <span class="analytics-sub-col analytics-sub-col-status">
                                        <span class="analytics-sub-dot" style="background: var(--warning)"></span> Inactive
                                    </span>
	                                    <div class="analytics-sub-actions">
	                                        <button class="analytics-recurring-action-btn analytics-recurring-cancel-btn"
	                                            title="Confirm cancelled"
                                                aria-label="Confirm cancelled"
	                                            on:click|stopPropagation={() => handleCancelSubscription(item)}>
	                                            <span class="material-symbols-outlined text-[13px]">close</span>
	                                        </button>
                                    </div>
                                </div>
                            {/each}
                        </div>
                    {/if}
                {/if}

                <!-- Cancelled subscriptions (collapsible) -->
                {#if cancelledRecurring.length > 0}
                    <div class="analytics-recurring-inactive-toggle"
                         style="border-top: 1px solid var(--card-border); background: var(--surface-100)">
                        <button class="analytics-recurring-inactive-trigger"
                            on:click|stopPropagation={() => { cancelledOpen = !cancelledOpen; }}>
                            <div class="flex items-center gap-2">
                                <span class="material-symbols-outlined text-[14px]" style="color: var(--negative)">
                                    {cancelledOpen ? 'expand_less' : 'expand_more'}
                                </span>
                                <span class="text-[9px] font-bold tracking-[0.1em] uppercase" style="color: var(--text-muted)">
                                    Cancelled
                                </span>
                                <span class="text-[9px] font-mono font-bold px-1.5 py-0.5 rounded-full"
                                    style="background: var(--surface-200); color: var(--negative)">
                                    {cancelledRecurring.length}
                                </span>
                            </div>
                        </button>
                    </div>
                    {#if cancelledOpen}
                        <div class="analytics-recurring-inactive-body">
                            {#each cancelledRecurring as item, i}
                                <div class="analytics-sub-row analytics-sub-row-inactive" style="border-bottom: {i < cancelledRecurring.length - 1 ? '1px solid color-mix(in srgb, var(--card-border) 30%, transparent)' : 'none'}">
                                    <div class="analytics-sub-left">
                                        <div class="analytics-sub-avatar"
                                            style="background: color-mix(in srgb, var(--negative) 6%, transparent)">
                                            <span class="material-symbols-outlined text-[12px]"
                                                style="color: var(--negative); opacity: 0.5">
                                                cancel
                                            </span>
                                        </div>
	                                        <div class="analytics-sub-body">
	                                            <span class="analytics-sub-name" style="color: var(--text-muted); text-decoration: line-through;">{item.clean_name || item.merchant}</span>
                                                {#if subscriptionTimingText(item)}
                                                    <span class="analytics-sub-meta">{subscriptionTimingText(item)}</span>
                                                {/if}
	                                        </div>
	                                    </div>
	                                    <span class="analytics-sub-col">{item.frequency}</span>
	                                    <span class="analytics-sub-col analytics-sub-col-stack">
                                            <strong>{formatCurrency(recurringDisplayAmount(item))}</strong>
                                            {#if recurringAmountNote(item)}
                                                <small>{recurringAmountNote(item)}</small>
                                            {/if}
                                        </span>
                                    <span class="analytics-sub-col">{formatCurrency(item.total_spent || ((item.amount || 0) * (item.charge_count || 0)))}</span>
                                    <span class="analytics-sub-col analytics-sub-col-status">
                                        <span class="analytics-sub-dot" style="background: var(--negative)"></span> Cancelled
                                    </span>
                                    <div class="analytics-sub-actions">
                                        <button class="analytics-recurring-restore-btn"
                                            on:click|stopPropagation={() => handleRestoreSubscription(item)}>
                                            <span class="material-symbols-outlined text-[12px]">undo</span>
                                            Restore
                                        </button>
                                    </div>
                                </div>
                            {/each}
                        </div>
                    {/if}
                {/if}

                <!-- Dismissed subscriptions (collapsible) -->
                {#if dismissedRecurring.length > 0}
                    <div class="analytics-recurring-inactive-toggle"
                         style="border-top: 1px solid var(--card-border); background: var(--surface-100)">
                        <button class="analytics-recurring-inactive-trigger"
                            on:click|stopPropagation={() => { dismissedOpen = !dismissedOpen; }}>
                            <div class="flex items-center gap-2">
                                <span class="material-symbols-outlined text-[14px]" style="color: var(--text-muted)">
                                    {dismissedOpen ? 'expand_less' : 'expand_more'}
                                </span>
                                <span class="text-[9px] font-bold tracking-[0.1em] uppercase" style="color: var(--text-muted)">
                                    Dismissed
                                </span>
                                <span class="text-[9px] font-mono font-bold px-1.5 py-0.5 rounded-full"
                                    style="background: var(--surface-200); color: var(--text-muted)">
                                    {dismissedRecurring.length}
                                </span>
                            </div>
                        </button>
                    </div>
                    {#if dismissedOpen}
                        <div class="analytics-recurring-inactive-body">
                            {#each dismissedRecurring as item, i}
                                <div class="analytics-sub-row analytics-sub-row-inactive"
                                    style="border-bottom: {i < dismissedRecurring.length - 1 ? '1px solid color-mix(in srgb, var(--card-border) 30%, transparent)' : 'none'}">
                                    <div class="analytics-sub-left">
                                        <div class="analytics-sub-avatar" style="background: var(--surface-100)">
                                            <span class="material-symbols-outlined text-[12px]"
                                                style="color: var(--text-muted); opacity: 0.4">
                                                visibility_off
                                            </span>
                                        </div>
                                        <div class="analytics-sub-body">
                                            <span class="analytics-sub-name" style="color: var(--text-muted)">{item.merchant}</span>
                                        </div>
                                    </div>
                                    <span class="analytics-sub-col"></span>
                                    <span class="analytics-sub-col"></span>
                                    <span class="analytics-sub-col"></span>
                                    <span class="analytics-sub-col"></span>
                                    <div class="analytics-sub-actions" style="opacity: 0.6">
                                        <button class="analytics-recurring-restore-btn"
                                            on:click|stopPropagation={() => handleRestoreSubscription(item)}>
                                            <span class="material-symbols-outlined text-[12px]">undo</span>
                                            Restore
                                        </button>
                                    </div>
                                </div>
                            {/each}
                        </div>
                    {/if}
                {/if}

                <!-- Summary footer -->
                <div class="analytics-sub-footer">
                    <span class="analytics-sub-footer-text">
                        {visibleRecurringTotals.active} active subscription{visibleRecurringTotals.active !== 1 ? 's' : ''} totaling <strong>{formatCurrency(visibleRecurringTotals.annual)}/yr</strong>
	                        {#if inactiveRecurring.length > 0}
	                            · {inactiveRecurring.length} inactive · {formatCurrency(inactiveTotalSpent)} paid
	                        {/if}
	                        {#if candidateRecurring.length > 0}
	                            · {candidateRecurring.length} to review
	                        {/if}
                        {#if cancelledRecurring.length > 0}
                            · {cancelledRecurring.length} cancelled
                        {/if}
                        {#if activeRecurring.filter(i => i.price_change && i.price_change.change > 0).length > 0}
                            {@const priceIncreases = activeRecurring.filter(i => i.price_change && i.price_change.change > 0)}
                            {@const totalIncrease = priceIncreases.reduce((sum, i) => sum + i.price_change.change, 0)}
                            · <span style="color: var(--negative)">{priceIncreases.length} recent price increase{priceIncreases.length !== 1 ? 's' : ''} (+{formatCurrency(totalIncrease)}/mo)</span>
                        {/if}
                    </span>
                </div>
            </div>
        </section>
        {/if}
    </div>
    {/if}

    <!-- ═══════════════════════════════════════
         S3: SPENDING PULSE — Anomaly Cards
         ═══════════════════════════════════════ -->
    {#if currentMonthSummary && spendingPulseCards.length > 0}
        <section class="mb-10 fade-in-up" style="animation-delay: 140ms">
            <div class="analytics-section-header">
                <h3 class="analytics-section-title">Spending Pulse</h3>
                <p class="analytics-section-context">You spent {formatCurrency(currentMonthSummary.expenses)} in {formatMonth(selectedMonth)} — here's what stands out.</p>
                {#if pulseInsight}<p class="analytics-section-context">{pulseInsight}</p>{/if}
            </div>

            <div class="card analytics-pulse-movement" class:expanded={pulseExpanded} style="padding: 0">
                <div class="analytics-pulse-movement-head">
                    <div>
                        <p class="analytics-kicker">Spending Pulse · {formatMonthShort(selectedMonth)}</p>
                        <h4 class="analytics-editorial-line">How <em>each</em> category moved</h4>
                    </div>
                    <div class="analytics-pulse-total">
                        <strong>{formatCurrency(currentMonthSummary.expenses)}</strong>
                        <span>spent · avg baseline</span>
                    </div>
                </div>
                <div class="analytics-pulse-scale">
                    <span><i class="below"></i>Below avg</span>
                    <span><i class="above"></i>Above avg</span>
                </div>
                <div class="analytics-pulse-rows">
                    {#each (pulseExpanded ? spendingPulseCards : spendingPulseCards.slice(0, 4)) as card}
                        {@const deviationAbs = Math.min(Math.abs(card.deviation), 150)}
                        {@const deviationWidth = Math.max((deviationAbs / 150) * 48, Math.abs(card.deviation) > 0 ? 3 : 0)}
                        <button class="analytics-pulse-row"
                            on:click={() => drillIntoCategory(card.category)}
                            class:selected={selectedCategory === card.category}
                            style="--pulse-color: {card.color}; --deviation-width: {deviationWidth}%">
                            <div class="analytics-pulse-row-label">
                                <span>{card.category}</span>
                                <small>{formatCurrency(card.total)}</small>
                            </div>
                            <div class="analytics-pulse-deviation">
                                <span class="analytics-pulse-baseline"></span>
                                {#if card.isOver || card.deviation > 0}
                                    <span class="analytics-pulse-dev-fill above"></span>
                                {:else if card.isUnder || card.deviation < 0}
                                    <span class="analytics-pulse-dev-fill below"></span>
                                {/if}
                            </div>
                            <div class="analytics-pulse-row-stat" class:over={card.deviation > 0} class:under={card.deviation < 0}>
                                <span>avg {formatCompact(card.avgTotal)} · {card.comparisonLabel}</span>
                                <strong>{card.deviation > 0 ? '+' : ''}{formatPercent(card.deviation)}</strong>
                            </div>
                        </button>
                    {/each}
                </div>
            </div>

            <!-- Legacy card grid retained as a compact visual fallback for very small screens -->
            <div class="analytics-pulse-grid analytics-pulse-card-fallback">
                {#each (pulseExpanded ? spendingPulseCards : spendingPulseCards.slice(0, 4)) as card, i}
                    {@const maxBar = Math.max(card.total, card.avgTotal)}
                    <button
                        on:click={() => drillIntoCategory(card.category)}
                        class="analytics-pulse-card card card-interactive"
                        class:ring-2={selectedCategory === card.category}
                        class:ring-accent={selectedCategory === card.category}
                        style="animation-delay: {i * 40}ms; --pulse-color: {card.color}">

                        <div class="analytics-pulse-category">
                            <span class="analytics-pulse-dot" style="background: {card.color}"></span>
                            <span class="truncate">{card.category}</span>
                        </div>

                        <p class="analytics-pulse-amount">{formatCurrency(card.total)}</p>

                        <div class="analytics-pulse-status" style="color: {card.isOver ? 'var(--negative)' : card.isUnder ? 'var(--positive)' : 'var(--text-muted)'}">
                            {#if card.isOver}
                                ▲ {formatPercent(Math.abs(card.deviation))} above avg
                            {:else if card.isUnder}
                                ▼ {formatPercent(Math.abs(card.deviation))} below avg
                            {:else}
                                On track {#if card.deviation !== 0}· {card.deviation > 0 ? '▲' : '▼'}{formatPercent(Math.abs(card.deviation))}{/if}
                            {/if}
                        </div>

                        {#if card.avgTotal > 0}
                            <div class="analytics-pulse-track">
                                <div class="analytics-pulse-track-fill" style="width: {(card.total / maxBar) * 100}%"></div>
                                <div class="analytics-pulse-track-marker" style="left: {(card.avgTotal / maxBar) * 100}%"></div>
                            </div>
                            <div class="analytics-pulse-track-label">avg {formatCompact(card.avgTotal)}</div>
                        {/if}
                    </button>
                {/each}
            </div>

            {#if spendingPulseCards.length > 4}
                <button
                    class="analytics-pulse-more"
                    style="color: var(--text-muted); background: none; border: none; cursor: pointer;"
                    on:click={() => pulseExpanded = !pulseExpanded}>
                    {pulseExpanded ? 'Show fewer categories' : `+ ${spendingPulseCards.length - 4} more categories`}
                </button>
            {/if}
        </section>
    {/if}

    <!-- ═══════════════════════════════════════
         S4: TRENDS & TRAJECTORY (2-panel)
         ═══════════════════════════════════════ -->
    <section class="mb-10 fade-in-up" style="animation-delay: 180ms">
        <div class="analytics-section-header">
            <h3 class="analytics-section-title">Trends & Trajectory</h3>
            {#if trendsInsight}<p class="analytics-section-context">{trendsInsight}</p>{/if}
        </div>

        <div class="analytics-two-panel analytics-trajectory-stage">
            <!-- Savings Rate Trend -->
            <div class="card analytics-savings-panel analytics-trajectory-card" style="padding: 1.35rem">
                <div class="analytics-panel-title-row">
                    <div>
                        <p class="analytics-kicker">Trends & trajectory · last {savingsRateTrend?.windowMonths || 12} months</p>
                        <h4 class="analytics-editorial-line">Savings <em>trajectory</em></h4>
                    </div>
                    <div class="analytics-savings-summary">
                        {#if savingsRateTrend}
                            <span class="analytics-target-pill">Target {savingsRateTrend.target}%</span>
                            <strong>{formatPercent(savingsRateTrend.currentRate)}</strong>
                            <small>current</small>
                        {/if}
                    </div>
                </div>

                {#if savingsRateGeometry}
                    {@const lastDot = savingsRateGeometry.dots[savingsRateGeometry.dots.length - 1]}
                    {@const currentRate = lastDot ? lastDot.rate : 0}
                    {@const sentimentColor = currentRate >= 25 ? 'var(--positive)' : currentRate >= 10 ? 'var(--warning)' : 'var(--negative)'}
                    <div style="position: relative;">
                        <svg width="100%" viewBox="0 0 {savingsRateGeometry.W} {savingsRateGeometry.H}" preserveAspectRatio="xMidYMid meet">
                            <defs>
                                <linearGradient id="savingsAreaGrad" x1="0" y1="0" x2="0" y2="1">
                                    <stop offset="0%" stop-color="var(--accent)" stop-opacity="0.20" />
                                    <stop offset="60%" stop-color="var(--accent)" stop-opacity="0.06" />
                                    <stop offset="100%" stop-color="var(--accent)" stop-opacity="0.01" />
                                </linearGradient>
                                <filter id="srDotGlow" x="-50%" y="-50%" width="200%" height="200%">
                                    <feGaussianBlur stdDeviation="4" result="glow"/>
                                    <feMerge>
                                        <feMergeNode in="glow"/>
                                        <feMergeNode in="SourceGraphic"/>
                                    </feMerge>
                                </filter>
                            </defs>

                            <!-- Grid -->
                            {#each savingsRateGeometry.gridLines as gl}
                                <line x1={savingsRateGeometry.padLeft} y1={gl.y} x2={savingsRateGeometry.W - 12} y2={gl.y}
                                    stroke="var(--text-muted)" stroke-width="0.5" opacity="0.08" />
                                <text x={savingsRateGeometry.padLeft - 4} y={gl.y + 3} text-anchor="end"
                                    fill="var(--text-muted)" font-size="8" font-family="DM Mono, monospace" opacity="0.5">
                                    {gl.label}
                                </text>
                            {/each}

                            <!-- Target line -->
                            <line x1={savingsRateGeometry.padLeft} y1={savingsRateGeometry.targetY}
                                x2={savingsRateGeometry.W - 12} y2={savingsRateGeometry.targetY}
                                stroke="rgba(255,255,255,0.15)" stroke-width="1" stroke-dasharray="6,4" />
                            <text x={savingsRateGeometry.W - 10} y={savingsRateGeometry.targetY - 4}
                                text-anchor="end" fill="var(--positive)" font-size="8" font-family="Inter, system-ui" font-weight="600" opacity="0.5">
                                Target {savingsRateTrend.target}%
                            </text>

                            <!-- Gradient area fill below the rolling average line -->
                            {#if savingsRateGeometry.rollingPath}
                                {@const firstDot = savingsRateGeometry.dots[0]}
                                <path d="{savingsRateGeometry.rollingPath} L{lastDot.x},{savingsRateGeometry.H - 28} L{firstDot.x},{savingsRateGeometry.H - 28} Z"
                                    fill="url(#savingsAreaGrad)" />
                            {/if}

                            <!-- Rolling average line -->
                            <path d={savingsRateGeometry.rollingPath} fill="none"
                                stroke="var(--accent)" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" opacity="0.8" />

                            <!-- Actual rate dots -->
                            {#each savingsRateGeometry.dots as dot, i}
                                {#if i === savingsRateGeometry.dots.length - 1}
                                    <!-- Glowing end dot -->
                                    <circle cx={dot.x} cy={dot.y} r="8"
                                        fill={sentimentColor} opacity="0.12"
                                        filter="url(#srDotGlow)"
                                        class="savings-rate-end-dot" />
                                    <circle cx={dot.x} cy={dot.y} r="5"
                                        fill={sentimentColor}
                                        stroke="var(--card-bg)" stroke-width="2.5"
                                        class="savings-rate-end-dot"
                                        style="color: {sentimentColor}" />
                                {:else}
                                    <circle cx={dot.x} cy={dot.y} r="3"
                                        fill={dot.rate >= 25 ? 'var(--positive)' : 'var(--negative)'}
                                        opacity="0.45" />
                                {/if}
                            {/each}

                            <!-- Month labels -->
                            {#each savingsRateGeometry.monthLabels as ml}
                                <text x={ml.x} y={savingsRateGeometry.H - 4} text-anchor="middle"
                                    fill="var(--text-muted)" font-size="7.5" font-family="Inter, system-ui" font-weight="500" opacity="0.5">
                                    {ml.label}
                                </text>
                            {/each}
                        </svg>
                    </div>

                    <div class="analytics-savings-ledger">
                        <div>
                            <span>Target gap</span>
                            <strong style="color: {savingsTargetGap >= 0 ? 'var(--positive)' : 'var(--negative)'}">
                                {Math.abs(Math.round(savingsTargetGap))} pts {savingsTargetGap >= 0 ? 'above' : 'below'}
                            </strong>
                        </div>
                        <div>
                            <span>Average rate</span>
                            <strong>{formatPercent(savingsRateTrend.avgRate)}</strong>
                        </div>
                        <div>
                            <span>Months at target</span>
                            <strong>{savingsMonthsAtTarget}/{savingsRateTrend.points.length}</strong>
                        </div>
                    </div>

                    <div class="analytics-chart-legend">
                        <div class="flex items-center gap-1.5">
                            <span class="w-5 h-0.5 rounded-full" style="background: var(--accent)"></span>
                            <span class="text-[9px]" style="color: var(--text-muted)">3mo Rolling Avg</span>
                        </div>
                        <div class="flex items-center gap-1.5">
                            <span class="w-2 h-2 rounded-full" style="background: var(--accent); opacity: 0.5"></span>
                            <span class="text-[9px]" style="color: var(--text-muted)">Actual</span>
                        </div>
                        <div class="flex items-center gap-1.5">
                            <span class="w-5 h-0.5 rounded-full" style="background: var(--positive); opacity: 0.3; border-style: dashed;"></span>
                            <span class="text-[9px]" style="color: var(--text-muted)">Target</span>
                        </div>
                    </div>
                {:else}
                    <p class="text-sm text-center py-6" style="color: var(--text-muted)">Not enough data</p>
                {/if}
            </div>

            <!-- Financial Health Snapshot (merged Projected + Income Stability) -->
            <div class="card analytics-health-panel analytics-trajectory-card" style="padding: 1.35rem">
                <div class="analytics-health-panel-head">
                    <div>
                        <p class="analytics-kicker">Financial health · last {projectedYearEnd?.windowMonths || incomeStability?.totalMonths || 12} months</p>
                        <h4>Forecast <em>scorecard</em></h4>
                    </div>
                    <span class="analytics-health-status {healthStatus.tone}">{healthStatus.label}</span>
                </div>

                <div class="analytics-health-scorecard">
                    <div class="analytics-health-hero-metric">
                        <span>Projected year-end</span>
                        {#if projectedYearEnd}
                            <strong style="color: {projectedYearEnd.projectedTotal >= 0 ? 'var(--positive)' : 'var(--negative)'}">
                                {projectedYearEnd.projectedTotal >= 0 ? '+' : ''}{formatCompact(projectedYearEnd.projectedTotal)}
                            </strong>
                            <small>{formatCompact(projectedYearEnd.pessimistic)} — {formatCompact(projectedYearEnd.optimistic)} range</small>
                        {:else}
                            <strong>—</strong>
                        {/if}
                    </div>
                    <div class="analytics-health-score-ring" style="--score-pct: {(healthScore / 5) * 100}%">
                        <small>Score</small>
                        <strong>{healthScore.toFixed(1)}</strong>
                        <span>/5</span>
                    </div>
                </div>

                <div class="analytics-health-drivers">
                    <div class="analytics-health-driver">
                        <span>Monthly net</span>
                        <strong style="color: {(projectedYearEnd?.avgNet || 0) >= 0 ? 'var(--positive)' : 'var(--negative)'}">
                            {projectedYearEnd ? `${projectedYearEnd.avgNet >= 0 ? '+' : ''}${formatCurrency(projectedYearEnd.avgNet)}` : '—'}
                        </strong>
                        <small>{projectedYearEnd ? `${projectedYearEnd.remainingMonths} months left in ${projectedYearEnd.currentYear}` : 'Need more history'}</small>
                    </div>
                    <div class="analytics-health-driver">
                        <span>Income stability</span>
                        <strong style="color: {incomeStability && incomeStability.dots >= 4 ? 'var(--positive)' : incomeStability && incomeStability.dots >= 3 ? 'var(--warning)' : 'var(--negative)'}">
                            {incomeStability ? incomeStability.level : '—'}
                        </strong>
                        <small>{incomeStability ? `σ ${formatCurrency(incomeStability.stdDev)}` : 'Need more history'}</small>
                    </div>
                    <div class="analytics-health-driver">
                        <span>Consistency</span>
                        <strong>{incomeStability ? `${incomeStability.streak} mo` : '—'}</strong>
                        <small>{incomeStability ? `avg ${formatCompact(incomeStability.avgIncome)}/mo income` : 'Need more history'}</small>
                    </div>
                </div>
                <div class="analytics-health-footer">
                    <span>Year-end forecast</span>
                    <a href="/budget">Build a plan →</a>
                </div>
            </div>
        </div>
    </section>

    <!-- ═══════════════════════════════════════
         S6: MONTH-OVER-MONTH DIFF
         ═══════════════════════════════════════ -->
    {#if momDiff.length > 0 && prevMonthData}
        <section class="mb-10 fade-in-up" style="animation-delay: 260ms">
            <div class="analytics-section-header">
                <h3 class="analytics-section-title">Month-over-Month Changes</h3>
                {#if momInsight}<p class="analytics-section-context">{momInsight}</p>{/if}
            </div>

            <div class="mom-glass-grid">
                <!-- Header row -->
                <div class="mom-glass-header">
                    <span class="mom-glass-cell mom-cell-category">Category</span>
                    <span class="mom-glass-cell mom-cell-right">This Month</span>
                    <span class="mom-glass-cell mom-cell-right">Last Month</span>
                    <span class="mom-glass-cell mom-cell-right">Change</span>
                </div>

                <div class="mom-glass-body">
                    {#each momDiff as row}
                        <div class="mom-glass-row"
                             style="--row-tint: {row.color}">
                            <div class="mom-glass-cell mom-cell-category">
                                <span class="text-[11px] font-medium" style="color: var(--text-primary)">{row.category}</span>
                            </div>
                            <span class="mom-glass-cell mom-cell-right text-[11px] font-mono font-medium" style="color: var(--text-primary)">{formatCurrency(row.currentTotal)}</span>
                            <span class="mom-glass-cell mom-cell-right text-[11px] font-mono" style="color: var(--text-muted)">{formatCurrency(row.prevTotal)}</span>
                            <span class="mom-glass-cell mom-cell-right">
                                <span class="delta-badge {row.delta <= 0 ? 'delta-up' : 'delta-down'}">
                                    {row.delta > 0 ? '▲' : '▼'} {formatCurrency(Math.abs(row.delta))}
                                </span>
                            </span>
                        </div>
                    {/each}
                </div>

                {#if bestWorstMonth}
                    <div class="mom-glass-footer">
                        <span class="text-[10px]" style="color: var(--text-muted)">
                            <span class="font-bold" style="color: var(--positive)">Best month:</span> {formatMonth(bestWorstMonth.best.month)} ({formatCurrency(bestWorstMonth.best.expenses)} spend)
                        </span>
                        <span class="text-[10px]" style="color: var(--text-muted)">
                            <span class="font-bold" style="color: var(--negative)">Highest:</span> {formatMonth(bestWorstMonth.worst.month)} ({formatCurrency(bestWorstMonth.worst.expenses)} spend)
                        </span>
                    </div>
                {/if}
            </div>
        </section>
    {/if}

    <!-- ═══════════════════════════════════════
         S7: ACTIONABLE NUDGE
         ═══════════════════════════════════════ -->
    {#if actionableNudge}
        <section class="mb-10 fade-in-up" style="animation-delay: 300ms">
            <div class="analytics-nudge-card card">
                <div class="flex items-start gap-3">
                    <div class="w-9 h-9 rounded-xl flex items-center justify-center flex-shrink-0" style="background: color-mix(in srgb, var(--accent) 12%, transparent)">
                        <span class="material-symbols-outlined text-[18px]" style="color: var(--accent)">lightbulb</span>
                    </div>
                    <div class="flex-1">
                        <p class="text-[13px] font-semibold mb-1" style="color: var(--text-primary)">
                            Reduce
                            {#each actionableNudge.suggestions as sug, i}
                                <span class="font-bold" style="color: {sug.color}">{sug.name}</span>{#if i < actionableNudge.suggestions.length - 1}{i === actionableNudge.suggestions.length - 2 ? ' and ' : ', '}{/if}
                            {/each}
                            to their averages
                        </p>
                        <p class="text-[12px]" style="color: var(--text-secondary)">
                            Save an extra <span class="font-bold font-mono" style="color: var(--positive)">{formatCurrency(actionableNudge.totalPotential)}/month</span>
                            — that's <span class="font-bold font-mono" style="color: var(--positive)">{formatCurrency(actionableNudge.annualized)}/year</span>.
                        </p>
                        <p class="text-[11px] mt-1" style="color: var(--text-muted)">
                            Savings rate: {formatPercent(actionableNudge.currentSR)} → <span class="font-bold" style="color: var(--positive)">{formatPercent(actionableNudge.newSR)}</span>
                        </p>
                    </div>
                    <a href={budgetNudgeHref} class="analytics-nudge-cta">
                        Set a Budget
                        <span class="material-symbols-outlined text-[14px]">arrow_forward</span>
                    </a>
                </div>
            </div>
        </section>
    {/if}


    <!-- ═══════════════════════════════════════
         MONTHLY DATA TABLE (collapsed)
         ═══════════════════════════════════════ -->
    <div class="card mb-8 fade-in-up" style="padding: 1.25rem 1.5rem; animation-delay: 340ms;">
    <details style="margin-bottom: 0;">
        <summary class="flex items-center gap-3 cursor-pointer select-none mb-3 rounded-xl transition-colors duration-150 hover:bg-[var(--surface-100)]"
            style="list-style: none;">
            <div class="flex-1">
                <p class="analytics-section-title" style="margin: 0;">Monthly Data Table</p>
                <p class="text-[10px]" style="color: var(--text-muted); margin: 0;">Click to view detailed monthly breakdown</p>
            </div>
            <span class="material-symbols-outlined text-[18px] transition-transform duration-200" style="color: var(--text-primary)">expand_more</span>
        </summary>
        <div class="overflow-x-auto" style="padding: 0">
            <table class="w-full">
                <thead>
                    <tr style="border-bottom: 1px solid var(--card-border)">
                        {#each ['Month', 'Income', 'Spending', 'Credits', 'Incoming', 'Ext. Transfers', 'Net Flow'] as h}
                            <th class="text-left px-5 py-2.5 text-[9px] font-bold uppercase tracking-wider"
                                style="color: var(--text-muted)">{h}</th>
                        {/each}
                    </tr>
                </thead>
                <tbody>
                    {#each [...monthly].sort((a,b) => b.month.localeCompare(a.month)) as m}
                        {@const creditsRefunds = m.credits_refunds ?? m.refunds ?? 0}
                        {@const incomingTransfers = m.incoming_transfers || 0}
                        {@const accrualNet = (m.income || 0) - (m.expenses || 0) + creditsRefunds + incomingTransfers - (m.external_transfers || 0)}
                        <tr class="transition-colors cursor-pointer" style="border-bottom: 1px solid var(--card-border)"
                            on:click={() => { selectedMonth = m.month; }}>
                            <td class="px-5 py-2.5 text-[12px] font-medium" style="color: var(--text-primary)">
                                {formatMonth(m.month)}
                                {#if m.month === selectedMonth}
                                    <span class="inline-block w-1.5 h-1.5 rounded-full ml-2" style="background: var(--accent)"></span>
                                {/if}
                            </td>
                            <td class="px-5 py-2.5 text-[12px] font-mono text-positive">{formatCurrency(m.income)}</td>
                            <td class="px-5 py-2.5 text-[12px] font-mono text-negative">{formatCurrency(m.expenses)}</td>
                            <td class="px-5 py-2.5 text-[12px] font-mono" style="color: {creditsRefunds > 0 ? 'var(--positive)' : 'var(--text-muted)'}">{creditsRefunds > 0 ? '+' : ''}{formatCurrency(creditsRefunds)}</td>
                            <td class="px-5 py-2.5 text-[12px] font-mono" style="color: {incomingTransfers > 0 ? 'var(--flow-transfer)' : 'var(--text-muted)'}">{incomingTransfers > 0 ? '+' : ''}{formatCurrency(incomingTransfers)}</td>
                            <td class="px-5 py-2.5 text-[12px] font-mono" style="color: {(m.external_transfers || 0) > 0 ? 'var(--warning)' : 'var(--text-muted)'}">{formatCurrency(m.external_transfers || 0)}</td>
                            <td class="px-5 py-2.5 text-[12px] font-bold font-mono"
                                style="color: {accrualNet >= 0 ? 'var(--positive)' : 'var(--negative)'}">
                                {accrualNet >= 0 ? '+' : ''}{formatCurrency(accrualNet)}
                            </td>
                        </tr>
                    {/each}
                </tbody>
            </table>
        </div>
    </details>
    </div>
    </div>
{/if}
