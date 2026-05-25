<script>
    import '$lib/styles/budget.css';
    import { onMount, tick, onDestroy } from 'svelte';
    import { page } from '$app/stores';
    import { api, invalidateCache } from '$lib/api.js';
    import { activeProfile } from '$lib/stores/profileStore.js';
    import {
        formatCurrency, formatPercent, formatMonth, formatDate,
        CATEGORY_COLORS, CATEGORY_ICONS, getCurrentMonth
    } from '$lib/utils.js';
    import ProfileSwitcher from '$lib/components/ProfileSwitcher.svelte';

    const NON_BUDGET_CATEGORIES = new Set(['Income', 'Credits & Refunds', 'Savings Transfer', 'Personal Transfer', 'Credit Card Payment', 'Cash Withdrawal', 'Cash Deposit', 'Investment Transfer']);
    const PROJECTED_PERCENT_DELTA_THRESHOLD = 5;

    let monthly = [];
    let allCategoryAnalytics = [];
    let categoriesMeta = [];
    let monthCategories = [];
    let budgets = {};
    let budgetSettings = {};
    let recurringData = null;
    let goals = [];
    let trailingCategoryStats = {};

    let loading = true;
    let profileSwitching = false;
    let selectedMonth = '';
    let loadedMonth = '';
    let highlightedCategory = '';
    let budgetMonthDropdownOpen = false;
    let focusedPlannerGroup = '';
    let plannerFocusTimer = null;

    let editingCategory = null;
    let editValue = '';
    let savingCategory = '';

    let selectedCategory = '';
    let categoryTransactions = [];
    let transactionsLoading = false;
    let budgetItems = [];
    let budgetedItems = [];
    let unsetItems = [];
    let attentionItems = [];
    let suggestedItems = [];
    let activeMonthBudgetItems = [];
    let visibleBudgetItems = [];
    let attentionCategorySet = new Set();
    let compactPlannerItems = [];
    let compactWatchItems = [];
    let compactOnPlanItems = [];
    let compactUnsetItems = [];
    let plannerGroups = [];
    let visibleBudgetedCount = 0;
    let visibleUnsetCount = 0;
    let dailyAverageSpend = 0;
    let planStats = {
        totalBudget: 0,
        totalSpent: 0,
        budgetedSpent: 0,
        totalRemaining: 0,
        utilization: 0,
        projectedSpend: 0,
        projectedBudgetedSpend: 0,
        projectedUtilization: 0,
        discretionaryRemaining: 0,
        overCount: 0,
        projectedOverCount: 0,
        budgetedCount: 0,
        unsetCount: 0,
        recurringMonthly: 0,
        goalGap: 0,
        goalMonthlyNeed: 0
    };
    let goalDraft = { name: '', goal_type: 'custom', target_amount: '', current_amount: '', target_date: '' };
    let savingGoal = false;
    let planHealth = { label: 'Ready to plan', tone: 'neutral', icon: 'edit_note', message: 'Start with suggested budgets from your usual categories.' };

    $: activeProfileId = $activeProfile || 'household';
    $: sortedMonths = [...monthly].sort((a, b) => b.month.localeCompare(a.month));
    $: selectedMonthSummary = monthly.find(m => m.month === selectedMonth) || null;
    $: currentMonth = getCurrentMonth();
    $: selectedMonthIsCurrent = selectedMonth === currentMonth;
    $: monthProgress = getMonthProgress(selectedMonth);
    $: {
        // Svelte only tracks values referenced directly in a reactive block.
        // These anchors make the derived budget model rebuild after async loads.
        categoriesMeta;
        monthCategories;
        allCategoryAnalytics;
        budgets;
        trailingCategoryStats;
        recurringData;
        monthly;
        monthProgress;
        selectedMonthIsCurrent;
        budgetItems = buildBudgetItems();
    }
    $: budgetedItems = budgetItems.filter(item => item.budget > 0);
    $: unsetItems = budgetItems.filter(item => item.budget <= 0);
    $: attentionItems = budgetItems
        .filter(item => item.hasMonthActivity && ['over', 'projected-over', 'watch', 'above-average', 'subscription-heavy'].includes(item.status))
        .sort((a, b) => b.attentionScore - a.attentionScore)
        .slice(0, 4);
    $: suggestedItems = budgetItems
        .filter(item => item.hasMonthActivity && item.budget <= 0 && item.suggestedBudget > 0)
        .sort((a, b) => b.suggestedBudget - a.suggestedBudget)
        .slice(0, 5);
    $: {
        budgetedItems;
        unsetItems;
        budgetItems;
        planStats = buildPlanStats();
    }
    $: planHealth = getPlanHealth(planStats);
    $: activeMonthBudgetItems = budgetItems.filter(item => item.hasMonthActivity);
    $: visibleBudgetItems = [...activeMonthBudgetItems].sort(sortBudgetItems);
    $: attentionCategorySet = new Set(attentionItems.map(item => item.category));
    $: compactPlannerItems = visibleBudgetItems.filter(item => !attentionCategorySet.has(item.category));
    $: compactWatchItems = compactPlannerItems.filter(item => item.budget > 0 && !['healthy', 'no-spend-yet'].includes(item.status));
    $: compactOnPlanItems = compactPlannerItems.filter(item => item.budget > 0 && ['healthy', 'no-spend-yet'].includes(item.status));
    $: compactUnsetItems = compactPlannerItems.filter(item => item.budget <= 0);
    $: plannerGroups = [
        { label: 'On plan', tone: 'positive', items: compactOnPlanItems },
        { label: 'Watch', tone: 'watch', items: compactWatchItems },
        { label: 'Unset', tone: 'neutral', items: compactUnsetItems }
    ];
    $: visibleBudgetedCount = visibleBudgetItems.filter(item => item.budget > 0).length;
    $: visibleUnsetCount = visibleBudgetItems.filter(item => item.budget <= 0).length;
    $: dailyAverageSpend = monthProgress.elapsedDays > 0 ? planStats.budgetedSpent / monthProgress.elapsedDays : 0;

    onMount(async () => {
        await loadBudgetPage();
        const focus = $page.url.searchParams.get('category');
        if (focus) {
            highlightedCategory = focus;
            await tick();
            document.getElementById(categoryElementId(focus))?.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
    });

    onDestroy(() => {
        if (plannerFocusTimer) clearTimeout(plannerFocusTimer);
    });

    async function loadBudgetPage() {
        loading = true;
        try {
            const [m, allCatsResult, metaResult, budgetResult, recurringResult, goalResult] = await Promise.all([
                api.getMonthlyAnalytics(),
                api.getCategoryAnalytics(),
                api.getCategoriesMeta().catch(() => []),
                api.getBudgets(),
                api.getRecurring().catch(() => null),
                api.getGoals().catch(() => ({ items: [] }))
            ]);
            monthly = Array.isArray(m) ? m : [];
            allCategoryAnalytics = normalizeCategoryResult(allCatsResult);
            categoriesMeta = Array.isArray(metaResult) ? metaResult : [];
            budgetSettings = Object.fromEntries((budgetResult?.items || []).map(item => [item.category, item]));
            budgets = Object.fromEntries((budgetResult?.items || []).map(item => [item.category, Number(item.amount || 0)]));
            recurringData = recurringResult;
            goals = goalResult?.items || [];

            const newestMonths = [...monthly].sort((a, b) => b.month.localeCompare(a.month));
            if (newestMonths.length > 0) {
                selectedMonth = selectedMonth || newestMonths[0].month;
                await loadMonthData(selectedMonth);
            }
        } catch (e) {
            console.error('Failed to load budgets:', e);
        } finally {
            loading = false;
        }
    }

    $: if (selectedMonth && selectedMonth !== loadedMonth && !loading) {
        loadMonthData(selectedMonth);
    }

    async function loadMonthData(month) {
        if (!month) return;
        loadedMonth = month;
        try {
            const monthWindow = getTrailingMonthWindow(month, 3);
            const [monthResult, ...historyResults] = await Promise.all([
                api.getCategoryAnalytics(month),
                ...monthWindow.map(m => api.getCategoryAnalytics(m).catch(() => ({ categories: [] })))
            ]);
            monthCategories = normalizeCategoryResult(monthResult);
            trailingCategoryStats = buildTrailingStats(historyResults.map(normalizeCategoryResult), monthWindow.length);
            if (selectedCategory) loadCategoryTransactions(selectedCategory);
        } catch (e) {
            console.error('Failed to load budget month:', e);
        }
    }

    let _prevBudgetProfile = null;
    $: if (activeProfileId && activeProfileId !== _prevBudgetProfile) {
        if (_prevBudgetProfile !== null) {
            reloadBudgetsForProfile();
        }
        _prevBudgetProfile = activeProfileId;
    }

    async function reloadBudgetsForProfile() {
        profileSwitching = true;
        try {
            selectedCategory = '';
            categoryTransactions = [];
            loadedMonth = '';
            await loadBudgetPage();
            if (sortedMonths.length > 0 && !sortedMonths.some(s => s.month === selectedMonth)) {
                selectedMonth = sortedMonths[0].month;
            }
        } catch (e) {
            console.error('Failed to reload budgets for profile:', e);
        } finally {
            profileSwitching = false;
        }
    }

    function normalizeCategoryResult(result) {
        return Array.isArray(result) ? result : (result?.categories || []);
    }

    function getMonthProgress(month) {
        if (!month) return { elapsedDays: 0, totalDays: 30, remainingDays: 0, ratio: 1 };
        const [year, monthNum] = month.split('-').map(Number);
        const totalDays = new Date(year, monthNum, 0).getDate();
        if (month !== currentMonth) {
            return { elapsedDays: totalDays, totalDays, remainingDays: 0, ratio: 1 };
        }
        const today = new Date();
        const elapsedDays = Math.max(today.getDate(), 1);
        return {
            elapsedDays,
            totalDays,
            remainingDays: Math.max(totalDays - elapsedDays, 0),
            ratio: Math.min(elapsedDays / totalDays, 1)
        };
    }

    function getTrailingMonthWindow(anchorMonth, count) {
        const idx = sortedMonths.findIndex(m => m.month === anchorMonth);
        if (idx >= 0) return sortedMonths.slice(idx, idx + count).map(m => m.month);
        return sortedMonths.slice(0, count).map(m => m.month);
    }

    function buildTrailingStats(results, monthCount) {
        const stats = {};
        for (const cats of results) {
            for (const cat of cats) {
                const name = cat.category;
                if (!name) continue;
                if (!stats[name]) stats[name] = { total: 0, max: 0, monthsWithSpend: 0, months: Math.max(monthCount, 1) };
                stats[name].total += Number(cat.total || 0);
                stats[name].max = Math.max(stats[name].max, Number(cat.total || 0));
                if (Number(cat.total || 0) > 0) stats[name].monthsWithSpend += 1;
            }
        }
        for (const stat of Object.values(stats)) {
            stat.average = stat.total / Math.max(stat.months, 1);
        }
        return stats;
    }

    function getCategoryMeta(category) {
        return categoriesMeta.find(item => item.name === category) || {};
    }

    function isBudgetable(category) {
        const meta = getCategoryMeta(category);
        return !NON_BUDGET_CATEGORIES.has(category) && meta.expense_type !== 'non_expense';
    }

    function getRecurringForCategory(category) {
        const items = recurringData?.items || [];
        return items.filter(item => item.category === category && item.status === 'active' && !item.cancelled);
    }

    function hasSelectedMonthActivity(monthCat) {
        if (!monthCat?.category) return false;
        return Number(monthCat.transaction_count ?? monthCat.count ?? 0) > 0
            || Number(monthCat.total || 0) > 0
            || Number(monthCat.gross || 0) > 0
            || Number(monthCat.refunds || 0) > 0;
    }

    function buildBudgetItems() {
        const monthMap = Object.fromEntries(monthCategories.map(cat => [cat.category, cat]));
        const allTimeMap = Object.fromEntries(allCategoryAnalytics.map(cat => [cat.category, cat]));
        const names = new Set();

        for (const item of categoriesMeta) if (item.is_active !== 0 && isBudgetable(item.name)) names.add(item.name);
        for (const cat of monthCategories) if (isBudgetable(cat.category)) names.add(cat.category);
        for (const cat of allCategoryAnalytics) if (isBudgetable(cat.category)) names.add(cat.category);
        for (const category of Object.keys(budgets)) if (isBudgetable(category)) names.add(category);
        for (const category of Object.keys(trailingCategoryStats)) if (isBudgetable(category)) names.add(category);

        const totalMonths = Math.max(monthly.length, 1);

        return [...names].map(category => {
            const monthCat = monthMap[category] || {};
            const allTimeCat = allTimeMap[category] || {};
            const meta = getCategoryMeta(category);
            const recurring = getRecurringForCategory(category);
            const hasMonthActivity = hasSelectedMonthActivity(monthCat);
            const spent = Number(monthCat.total || 0);
            const budget = Number(budgets[category] || 0);
            const setting = budgetSettings[category] || {};
            const rolloverMode = setting.rollover_mode || 'none';
            const rolloverBalance = Number(setting.rollover_balance || 0);
            const available = budget + rolloverBalance;
            const trailing = trailingCategoryStats[category] || { average: 0, max: 0, monthsWithSpend: 0, months: 0 };
            const averageMonthly = trailing.average || (Number(allTimeCat.total || 0) / totalMonths);
            const projected = selectedMonthIsCurrent && monthProgress.elapsedDays > 0
                ? (spent / monthProgress.elapsedDays) * monthProgress.totalDays
                : spent;
            const remaining = available - spent;
            const budgetPercent = available > 0 ? (spent / available) * 100 : 0;
            const projectedPercent = available > 0 ? (projected / available) * 100 : 0;
            const recurringTotal = recurring.reduce((sum, item) => sum + Number(item.amount || item.monthly_amount || 0), 0);
            const suggestedBudget = getSuggestedBudget(averageMonthly, recurringTotal, spent);
            const status = getCategoryStatus({ budget, spent, projected, averageMonthly, recurringTotal, budgetPercent, projectedPercent });
            const attentionScore = getAttentionScore({ status, budget, spent, projected, averageMonthly, recurringTotal });

            return {
                category,
                budget,
                available,
                rolloverMode,
                rolloverBalance,
                spent,
                remaining,
                budgetPercent,
                projected,
                projectedPercent,
                averageMonthly,
                suggestedBudget,
                recurring,
                recurringTotal,
                expenseType: meta.expense_type || 'variable',
                hasMonthActivity,
                percent: monthCat.percent || 0,
                status,
                attentionScore
            };
        });
    }

    function getSuggestedBudget(averageMonthly, recurringTotal, spent) {
        const baseline = Math.max(averageMonthly || 0, recurringTotal || 0, spent || 0);
        if (baseline <= 0) return 0;
        return Math.ceil((baseline * 1.08) / 25) * 25;
    }

    function getCategoryStatus(item) {
        if (item.budget <= 0 && item.spent <= 0) return 'no-spend-yet';
        if (item.budget <= 0) return item.averageMonthly > 0 ? 'unset' : 'no-spend-yet';
        if (item.spent > item.budget) return 'over';
        if (item.projected > item.budget) return 'projected-over';
        if (item.budgetPercent >= 80) return 'watch';
        if (item.averageMonthly > 0 && item.spent > item.averageMonthly * 1.25) return 'above-average';
        if (item.recurringTotal > item.budget * 0.65) return 'subscription-heavy';
        return 'healthy';
    }

    function getAttentionScore(item) {
        const overage = Math.max(item.spent - item.budget, item.projected - item.budget, 0);
        const averageJump = item.averageMonthly > 0 ? Math.max(item.spent - item.averageMonthly, 0) : 0;
        const statusWeight = {
            over: 500,
            'projected-over': 400,
            watch: 300,
            'above-average': 220,
            'subscription-heavy': 160
        }[item.status] || 0;
        return statusWeight + overage + averageJump + item.recurringTotal;
    }

    function sortBudgetItems(a, b) {
        const aBudgeted = a.budget > 0 ? 1 : 0;
        const bBudgeted = b.budget > 0 ? 1 : 0;
        if (aBudgeted !== bBudgeted) return bBudgeted - aBudgeted;
        if (a.status !== b.status) return statusRank(b.status) - statusRank(a.status);
        return Math.max(b.spent, b.suggestedBudget) - Math.max(a.spent, a.suggestedBudget);
    }

    function statusRank(status) {
        return {
            over: 7,
            'projected-over': 6,
            watch: 5,
            'above-average': 4,
            'subscription-heavy': 3,
            healthy: 2,
            unset: 1,
            'no-spend-yet': 0
        }[status] || 0;
    }

    function buildPlanStats() {
        const totalBudget = budgetedItems.reduce((sum, item) => sum + item.budget, 0);
        const totalSpent = budgetItems.reduce((sum, item) => sum + item.spent, 0);
        const budgetedSpent = budgetedItems.reduce((sum, item) => sum + item.spent, 0);
        const projectedSpend = budgetItems.reduce((sum, item) => sum + item.projected, 0);
        const projectedBudgetedSpend = budgetedItems.reduce((sum, item) => sum + item.projected, 0);
        const totalAvailable = budgetedItems.reduce((sum, item) => sum + item.available, 0);
        const totalRemaining = totalAvailable - budgetedSpent;
        const utilization = totalBudget > 0 ? (budgetedSpent / totalBudget) * 100 : 0;
        const projectedUtilization = totalBudget > 0 ? (projectedBudgetedSpend / totalBudget) * 100 : 0;
        const discretionaryRemaining = budgetedItems
            .filter(item => item.expenseType !== 'fixed')
            .reduce((sum, item) => sum + Math.max(item.remaining, 0), 0);

        return {
            totalBudget,
            totalAvailable,
            totalSpent,
            budgetedSpent,
            totalRemaining,
            utilization,
            projectedSpend,
            projectedBudgetedSpend,
            projectedUtilization,
            discretionaryRemaining,
            overCount: budgetItems.filter(item => item.status === 'over').length,
            projectedOverCount: budgetItems.filter(item => item.status === 'projected-over').length,
            budgetedCount: budgetedItems.length,
            unsetCount: unsetItems.length,
            recurringMonthly: recurringData?.total_monthly || 0,
            goalGap: goals.reduce((sum, goal) => sum + Math.max(Number(goal.target_amount || 0) - Number(goal.current_amount || 0), 0), 0),
            goalMonthlyNeed: goals.reduce((sum, goal) => sum + getGoalMonthlyNeed(goal), 0)
        };
    }

    function getPlanHealth(stats) {
        if (stats.totalBudget <= 0) {
            return { label: 'Ready to plan', tone: 'neutral', icon: 'edit_note', message: 'Start with suggested budgets from your usual categories.' };
        }
        if (stats.totalRemaining < 0) {
            return { label: 'Over plan', tone: 'negative', icon: 'priority_high', message: `${stats.overCount} categor${stats.overCount === 1 ? 'y is' : 'ies are'} already over budget.` };
        }
        if (stats.projectedUtilization > 100) {
            return { label: 'Trending over', tone: 'warning', icon: 'trending_up', message: `${stats.projectedOverCount || 1} categor${(stats.projectedOverCount || 1) === 1 ? 'y is' : 'ies are'} projected to exceed plan.` };
        }
        if (stats.utilization >= 80) {
            return { label: 'Watch pace', tone: 'warning', icon: 'pace', message: 'Most of the monthly plan is already used.' };
        }
        return { label: 'On plan', tone: 'positive', icon: 'check_circle', message: 'Spending is tracking inside the monthly plan.' };
    }

    function statusLabel(status) {
        return {
            over: 'Over',
            'projected-over': 'Projected over',
            watch: 'Watch',
            'above-average': 'Above avg',
            'subscription-heavy': 'Recurring',
            healthy: 'On plan',
            unset: 'Unset',
            'no-spend-yet': 'No spend'
        }[status] || status;
    }

    function projectedContextText(item) {
        if (!selectedMonthIsCurrent || item.budget <= 0) return '';

        const projectedOver = item.projected - item.available;
        if (projectedOver > 0) return `Projected over by ${formatCurrency(projectedOver)}`;

        const projectedDelta = item.projectedPercent - item.budgetPercent;
        if (Math.abs(projectedDelta) >= PROJECTED_PERCENT_DELTA_THRESHOLD) {
            return `projected ${formatPercent(item.projectedPercent)}`;
        }

        return '';
    }

    function isSpentOverAvailable(item) {
        return item.budget > 0 && item.spent > item.available;
    }

    function isRemainingHealthy(item) {
        return item.budget > 0 && item.remaining > 0 && item.status === 'healthy';
    }

    function isRemainingWatch(item) {
        return item.budget > 0
            && item.remaining >= 0
            && ['watch', 'projected-over', 'above-average', 'subscription-heavy'].includes(item.status);
    }

    function isRecurringHighShare(item) {
        return item.budget > 0 && item.recurringTotal > item.budget * 0.65;
    }

    function categoryElementId(category) {
        return `budget-cat-${category.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`;
    }

    function startEdit(category, currentBudget) {
        editingCategory = category;
        editValue = currentBudget > 0 ? Math.round(currentBudget).toString() : '';
    }

    async function commitEdit(category) {
        await saveBudget(category, editValue);
        editingCategory = null;
    }

    async function saveBudget(category, value) {
        const num = parseFloat(value);
        const amount = !isNaN(num) && num > 0 ? num : null;
        const previous = { ...budgets };
        const priorSetting = budgetSettings[category] || {};
        budgets = amount ? { ...budgets, [category]: amount } : Object.fromEntries(Object.entries(budgets).filter(([key]) => key !== category));
        savingCategory = category;

        const profile = activeProfileId && activeProfileId !== 'household' ? activeProfileId : null;
        try {
            const payload = {
                amount,
                rollover_mode: priorSetting.rollover_mode || 'none',
                rollover_balance: Number(priorSetting.rollover_balance || 0)
            };
            await api.updateBudget(category, payload, profile);
            invalidateCache();
        } catch (e) {
            console.error('Failed to save budget:', e);
            budgets = previous;
        } finally {
            savingCategory = '';
        }
    }

    async function applySuggestion(item, mode = 'rounded') {
        const amount = getSuggestionAmount(item, mode);
        if (amount > 0) await saveBudget(item.category, amount);
    }

    function getSuggestionAmount(item, mode) {
        const base = Math.max(item.averageMonthly || 0, item.recurringTotal || 0, item.spent || 0);
        if (base <= 0) return 0;
        if (mode === 'lean') return Math.ceil((base * 0.9) / 25) * 25;
        if (mode === 'average') return Math.ceil(base);
        return item.suggestedBudget;
    }

    function monthsUntil(targetDate) {
        if (!targetDate) return 0;
        const today = new Date();
        const target = new Date(targetDate + 'T00:00:00');
        if (Number.isNaN(target.getTime()) || target <= today) return 0;
        return Math.max(1, Math.ceil((target - today) / (1000 * 60 * 60 * 24 * 30.4375)));
    }

    function getGoalMonthlyNeed(goal) {
        if (goal?.projection?.required_monthly != null) return Number(goal.projection.required_monthly || 0);
        const gap = Math.max(Number(goal.target_amount || 0) - Number(goal.current_amount || 0), 0);
        const months = monthsUntil(goal.target_date);
        return months > 0 ? gap / months : 0;
    }

    function goalProjectionText(goal) {
        const projection = goal?.projection || {};
        if (projection.status === 'funded') return 'Funded';
        if (projection.status === 'on_track') return `On track · ${formatCurrency(projection.required_monthly || 0)}/mo needed`;
        if (projection.status === 'behind') return `Behind pace · ${formatCurrency(projection.required_monthly || 0)}/mo needed`;
        if (projection.status === 'needs_progress') return `Needs ${formatCurrency(projection.required_monthly || 0)}/mo by target`;
        if (projection.status === 'needs_target_date') return 'Add a target date for monthly pace';
        return 'Projection unavailable';
    }

    function goalProjectionTone(goal) {
        const status = goal?.projection?.status || '';
        if (status === 'funded' || status === 'on_track') return 'positive';
        if (status === 'behind') return 'negative';
        if (status === 'needs_progress') return 'warning';
        return 'neutral';
    }

    async function saveGoal() {
        if (!goalDraft.name.trim() || savingGoal) return;
        savingGoal = true;
        const profile = activeProfileId && activeProfileId !== 'household' ? activeProfileId : null;
        try {
            await api.createGoal({
                name: goalDraft.name.trim(),
                goal_type: goalDraft.goal_type || 'custom',
                target_amount: Number(goalDraft.target_amount || 0),
                current_amount: Number(goalDraft.current_amount || 0),
                target_date: goalDraft.target_date || null
            }, profile);
            const result = await api.getGoals();
            goals = result?.items || [];
            goalDraft = { name: '', goal_type: 'custom', target_amount: '', current_amount: '', target_date: '' };
            invalidateCache();
        } catch (e) {
            console.error('Failed to save goal:', e);
        } finally {
            savingGoal = false;
        }
    }

    async function deleteGoal(id) {
        const profile = activeProfileId && activeProfileId !== 'household' ? activeProfileId : null;
        try {
            await api.deleteGoal(id, profile);
            goals = goals.filter(goal => goal.id !== id);
            invalidateCache();
        } catch (e) {
            console.error('Failed to delete goal:', e);
        }
    }

    async function loadCategoryTransactions(category) {
        selectedCategory = category;
        transactionsLoading = true;
        categoryTransactions = [];
        try {
            const result = await api.getTransactions({ month: selectedMonth, category, limit: 25 });
            categoryTransactions = result?.data || [];
        } catch (e) {
            console.error('Failed to load budget transactions:', e);
        } finally {
            transactionsLoading = false;
        }
    }

    function closeTransactions() {
        selectedCategory = '';
        categoryTransactions = [];
    }

    async function focusPlannerGroup(tone) {
        focusedPlannerGroup = tone;
        if (plannerFocusTimer) clearTimeout(plannerFocusTimer);
        await tick();
        document.querySelector(`.budget-group-${tone}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' });
        plannerFocusTimer = setTimeout(() => {
            focusedPlannerGroup = '';
            plannerFocusTimer = null;
        }, 4500);
    }
</script>

{#if loading}
    <div class="budget-loading">
        <div class="skeleton h-8 w-32 rounded-xl"></div>
        <div class="skeleton budget-hero-skeleton"></div>
        <div class="budget-skeleton-grid">
            {#each Array(3) as _}
                <div class="skeleton h-28 rounded-xl"></div>
            {/each}
        </div>
        {#each Array(5) as _}
            <div class="skeleton h-24 rounded-xl"></div>
        {/each}
    </div>
{:else}
<div class="budget-page profile-transition" class:profile-loading={profileSwitching}>
    <div class="budget-page-header fade-in">
        <div>
            <h2 class="folio-page-title">Budgets</h2>
            <p class="folio-page-subtitle">Plan vs actual, with enough context to adjust calmly.</p>
        </div>
        <ProfileSwitcher />
    </div>

    <section class="budget-command-island card fade-in-up" class:budget-month-open={budgetMonthDropdownOpen} style="animation-delay: 40ms">
        <div class="budget-command-shell">
            <div class="budget-command-left">
                <p class="budget-command-label">Budget Command</p>
                <div class="budget-command-title">
                    <div class="budget-health-icon budget-tone-{planHealth.tone}">
                        <span class="material-symbols-outlined">{planHealth.icon}</span>
                    </div>
                    <div>
                        <h3>{planHealth.label}</h3>
                        <p class="budget-hero-copy">{planHealth.message}</p>
                    </div>
                </div>

                <div class="month-dropdown-wrapper budget-month-dropdown">
                    <button
                        type="button"
                        class="month-dropdown-trigger budget-month-trigger"
                        aria-haspopup="listbox"
                        aria-expanded={budgetMonthDropdownOpen}
                        on:click|stopPropagation={() => { budgetMonthDropdownOpen = !budgetMonthDropdownOpen; }}
                    >
                        <span>{selectedMonth ? formatMonth(selectedMonth) : 'Select month'}</span>
                        <span class="material-symbols-outlined text-[13px]"
                            style="opacity: 0.55; transition: transform 0.2s;"
                            class:rotate-180={budgetMonthDropdownOpen}>
                            expand_more
                        </span>
                    </button>

                    {#if budgetMonthDropdownOpen}
                        <button type="button" class="month-dropdown-backdrop" aria-label="Close month picker" on:click={() => budgetMonthDropdownOpen = false}></button>
                        <div class="month-dropdown-menu budget-month-menu" role="listbox" style="bottom: auto; top: calc(100% + 6px);">
                            {#each sortedMonths as m}
                                <button
                                    type="button"
                                    class="month-dropdown-item"
                                    class:month-dropdown-item-active={selectedMonth === m.month}
                                    role="option"
                                    aria-selected={selectedMonth === m.month}
                                    on:click|stopPropagation={() => { selectedMonth = m.month; budgetMonthDropdownOpen = false; }}
                                >
                                    {formatMonth(m.month)}
                                </button>
                            {/each}
                        </div>
                    {/if}
                </div>
            </div>

            <div class="budget-command-center">
                <div class="budget-command-track-labels">
                    <span>{formatPercent(planStats.utilization)} spent</span>
                    <span>{formatPercent(planStats.projectedUtilization)} projected</span>
                </div>
                <div class="budget-command-rule" aria-hidden="true">
                    <div
                        class="budget-command-rule-used budget-tone-{planHealth.tone}"
                        style="width: {Math.min(planStats.utilization, 100)}%">
                    </div>
                    {#if selectedMonthIsCurrent && planStats.projectedUtilization > planStats.utilization}
                        <div
                            class="budget-command-rule-projected"
                            style="width: {Math.min(planStats.projectedUtilization, 100)}%">
                        </div>
                    {/if}
                    <span
                        class="budget-command-projected-marker"
                        style="left: {Math.min(planStats.projectedUtilization, 100)}%">
                    </span>
                </div>

                <div class="budget-command-metrics">
                    <div>
                        <span>Total plan</span>
                        <strong>{formatCurrency(planStats.totalBudget)}</strong>
                    </div>
                    <div>
                        <span>Spent</span>
                        <strong>{formatCurrency(planStats.budgetedSpent)}</strong>
                        <small>{formatPercent(planStats.utilization)} of plan</small>
                    </div>
                    <div>
                        <span>Remaining</span>
                        <strong class:budget-negative={planStats.totalRemaining < 0}>{formatCurrency(planStats.totalRemaining)}</strong>
                        <small>{planStats.totalBudget > 0 ? `${formatPercent(Math.max(100 - planStats.utilization, 0))} of plan` : 'No plan set'}</small>
                    </div>
                    <div>
                        <span>{selectedMonthIsCurrent ? 'Projected' : 'Actual'}</span>
                        <strong>{formatCurrency(selectedMonthIsCurrent ? planStats.projectedSpend : planStats.totalSpent)}</strong>
                        <small>{selectedMonthIsCurrent ? `${formatPercent(planStats.projectedUtilization)} of plan` : 'Closed month'}</small>
                    </div>
                </div>
            </div>

            <aside class="budget-command-safe">
                <div class="budget-command-safe-actions">
                    {#if suggestedItems.length > 0}
                        <button class="budget-primary-action" on:click={() => Promise.all(suggestedItems.map(item => applySuggestion(item)))}>
                            <span class="material-symbols-outlined">auto_fix_high</span>
                            Apply draft
                        </button>
                    {:else}
                        <button class="budget-primary-action budget-primary-action-muted" on:click={() => document.querySelector('.budget-planner')?.scrollIntoView({ behavior: 'smooth', block: 'start' })}>
                            <span class="material-symbols-outlined">edit</span>
                            Edit plan
                        </button>
                    {/if}
                </div>
                <span>{planStats.totalBudget > 0 ? 'Variable left' : 'Open categories'}</span>
                <strong class:budget-negative={planStats.totalRemaining < 0}>
                    {planStats.totalBudget > 0 ? formatCurrency(planStats.discretionaryRemaining) : planStats.unsetCount}
                </strong>
                <small>
                    {planStats.budgetedCount} budgeted · {monthProgress.remainingDays} day{monthProgress.remainingDays === 1 ? '' : 's'} left
                </small>
                <div class="budget-command-gauge">
                    <i style="width: {Math.min(planStats.utilization, 100)}%"></i>
                    <b style="left: {Math.min(planStats.utilization, 100)}%"></b>
                </div>
                <em>Daily avg {formatCurrency(dailyAverageSpend || 0)}</em>
            </aside>
        </div>
    </section>

    <section class="budget-planning-strip card fade-in-up" style="animation-delay: 100ms">
        <div class="budget-plan-signals-heading">
            <h3>Plan Signals</h3>
        </div>
        <div class="budget-strip-grid">
            <details class="budget-commitments-drawer budget-signal-card budget-signal-positive">
                <summary>
                    <span class="budget-signal-icon material-symbols-outlined">calendar_month</span>
                    <span class="budget-signal-copy">
                        <strong>Commitments</strong>
                        <em>{formatCurrency(planStats.recurringMonthly + planStats.goalMonthlyNeed)} <small>/mo</small></em>
                        <span>Recurring & goal pressure</span>
                        <b>On track</b>
                    </span>
                    <span class="budget-signal-arrow material-symbols-outlined">chevron_right</span>
                </summary>

                <div class="budget-signal-expanded">
                    <div class="budget-signal-expanded-header">
                        <h3>Planning Commitments</h3>
                        <p>Recurring and goal pressure above the category plan.</p>
                    </div>
                    <div class="budget-commitment-grid">
                        <div>
                            <span>Recurring / mo</span>
                            <strong>{formatCurrency(planStats.recurringMonthly)}</strong>
                        </div>
                        <div>
                            <span>Goal gap</span>
                            <strong>{formatCurrency(planStats.goalGap)}</strong>
                        </div>
                        <div>
                            <span>Monthly pace</span>
                            <strong>{planStats.goalMonthlyNeed > 0 ? formatCurrency(planStats.goalMonthlyNeed) : 'Unset'}</strong>
                        </div>
                    </div>
                </div>
            </details>

            <button class="budget-signal-card budget-signal-warning" on:click={() => document.querySelector('.budget-attention-board')?.scrollIntoView({ behavior: 'smooth', block: 'start' })}>
                <span class="budget-signal-icon material-symbols-outlined">flag</span>
                <span class="budget-signal-copy">
                    <strong>Needs Attention</strong>
                    <em>{attentionItems.length}</em>
                    <span>Categories over plan</span>
                    <b class:budget-signal-alert={attentionItems.length > 0}>{attentionItems.length > 0 ? 'Review needed' : 'Clear'}</b>
                </span>
                <span class="budget-signal-arrow material-symbols-outlined">chevron_right</span>
            </button>

            <button class="budget-signal-card budget-signal-draft" on:click={() => focusPlannerGroup('neutral')}>
                <span class="budget-signal-icon material-symbols-outlined">edit</span>
                <span class="budget-signal-copy">
                    <strong>Draft Budgets</strong>
                    <em>{suggestedItems.length}</em>
                    <span>Suggestions to review</span>
                    {#if suggestedItems.length > 0}
                        <b>Ready</b>
                    {/if}
                </span>
                <span class="budget-signal-arrow material-symbols-outlined">chevron_right</span>
            </button>

            <details class="budget-goals-drawer budget-signal-card budget-signal-goals">
                <summary>
                    <span class="budget-signal-icon material-symbols-outlined">track_changes</span>
                    <span class="budget-signal-copy">
                        <strong>Goals</strong>
                        <em>{goals.length} <small>active</small></em>
                        <span>{formatCurrency(planStats.goalMonthlyNeed)} monthly pace</span>
                    </span>
                    <span class="budget-signal-arrow material-symbols-outlined">chevron_right</span>
                </summary>

                <div class="budget-signal-expanded">
                    <div class="budget-signal-expanded-header">
                        <h3>Goals & Sinking Funds</h3>
                        <span class="budget-goals-summary-action">Manage goals</span>
                    </div>

                    {#if goals.length > 0}
                        <div class="budget-goal-list">
                            {#each goals as goal}
                                {@const gap = Math.max(Number(goal.target_amount || 0) - Number(goal.current_amount || 0), 0)}
                                {@const pct = Number(goal.target_amount || 0) > 0 ? Math.min((Number(goal.current_amount || 0) / Number(goal.target_amount || 1)) * 100, 100) : 0}
                                <div class="budget-goal-row">
                                    <div>
                                        <strong>{goal.name}</strong>
                                        <span>{goal.goal_type} · {gap > 0 ? `${formatCurrency(gap)} left` : 'funded'}</span>
                                        <em class="budget-goal-projection budget-goal-projection-{goalProjectionTone(goal)}">
                                            {goalProjectionText(goal)}
                                        </em>
                                    </div>
                                    <div class="budget-goal-right">
                                        <span>{formatCurrency(goal.current_amount)} / {formatCurrency(goal.target_amount)}</span>
                                        <button on:click={() => deleteGoal(goal.id)} title="Archive goal">
                                            <span class="material-symbols-outlined">close</span>
                                        </button>
                                    </div>
                                    <div class="budget-progress-track budget-goal-track">
                                        <div class="budget-progress-fill" style="width: {pct}%"></div>
                                    </div>
                                </div>
                            {/each}
                        </div>
                    {/if}

                    <div class="budget-goal-form">
                        <input bind:value={goalDraft.name} placeholder="Goal name" />
                        <select bind:value={goalDraft.goal_type}>
                            <option value="emergency_fund">Emergency fund</option>
                            <option value="travel">Travel</option>
                            <option value="annual_bill">Annual bill</option>
                            <option value="debt_payoff">Debt payoff</option>
                            <option value="custom">Custom</option>
                        </select>
                        <input bind:value={goalDraft.target_amount} type="number" min="0" step="1" placeholder="Target" />
                        <input bind:value={goalDraft.current_amount} type="number" min="0" step="1" placeholder="Saved" />
                        <input bind:value={goalDraft.target_date} type="date" />
                        <button on:click={saveGoal} disabled={savingGoal || !goalDraft.name.trim()}>
                            {savingGoal ? 'Saving...' : 'Add goal'}
                        </button>
                    </div>
                </div>
            </details>
        </div>
    </section>

    <section class="budget-planner fade-in-up" style="animation-delay: 160ms">
        <div class="budget-section-header budget-section-header-row">
            <div>
                <h3>Category Planner</h3>
                <p>{visibleBudgetedCount} budgeted · {visibleUnsetCount} open to plan</p>
            </div>
            <div class="budget-planner-tabs" aria-label="Budget planner summary">
                <span>{attentionItems.length} attention</span>
                <span>{compactOnPlanItems.length} on plan</span>
                <span>{compactUnsetItems.length} drafts</span>
            </div>
        </div>

        {#if visibleBudgetItems.length > 0}
            <div class="budget-planner-shell">
                <section class="budget-attention-board">
                    <div class="budget-subsection-heading">
                        <div>
                            <span class="budget-subsection-kicker">Decision cards</span>
                            <h4>Needs Attention</h4>
                        </div>
                        <p>{attentionItems.length > 0 ? 'Categories worth a closer look this month.' : 'No urgent budget issues.'}</p>
                    </div>

                    <div class="budget-attention-grid">
                        {#each attentionItems as item, i}
                            {@const projectionNote = projectedContextText(item)}
                            <article
                                id={categoryElementId(item.category)}
                                class="budget-attention-card budget-category-tone-{item.status}"
                                class:budget-highlight={highlightedCategory === item.category}
                                style="--cat-color: {CATEGORY_COLORS[item.category] || '#627d98'}; animation-delay: {190 + i * 28}ms">
                                <div class="budget-attention-card-top">
                                    <button class="budget-category-title" on:click={() => loadCategoryTransactions(item.category)}>
                                        <span class="budget-category-icon">
                                            <span class="material-symbols-outlined">{CATEGORY_ICONS[item.category] || 'label'}</span>
                                        </span>
                                        <span>
                                            <strong>{item.category}</strong>
                                            <em>{item.expenseType === 'fixed' ? 'Fixed' : 'Variable'} · avg {formatCurrency(item.averageMonthly)}</em>
                                        </span>
                                    </button>
                                    <span class="budget-status budget-status-{item.status}">{statusLabel(item.status)}</span>
                                </div>

                                <div class="budget-card-stats">
                                    <div>
                                        <span>Spent</span>
                                        <strong class:budget-value-over={isSpentOverAvailable(item)}>{formatCurrency(item.spent)}</strong>
                                    </div>
                                    <div>
                                        <span>Remaining</span>
                                        <strong
                                            class:budget-negative={item.remaining < 0}
                                            class:budget-value-warning={isRemainingWatch(item)}>
                                            {formatCurrency(item.remaining)}
                                        </strong>
                                    </div>
                                    <div>
                                        <span>Budget</span>
                                        <strong>{formatCurrency(item.budget)}</strong>
                                    </div>
                                </div>

                                <div class="budget-category-progress">
                                    <div class="budget-progress-track">
                                        <div
                                            class="budget-progress-fill budget-status-fill-{item.status}"
                                            style="width: {Math.min(item.budgetPercent, 100)}%">
                                        </div>
                                        {#if projectionNote && item.projectedPercent > item.budgetPercent}
                                            <div class="budget-progress-ghost" style="width: {Math.min(item.projectedPercent, 100)}%"></div>
                                        {/if}
                                        {#if selectedMonthIsCurrent}
                                            <span class="budget-pace-marker" style="left: {Math.min(monthProgress.ratio * 100, 100)}%"></span>
                                        {/if}
                                    </div>
                                    <div class="budget-progress-head">
                                        <span>{formatPercent(item.budgetPercent)} used</span>
                                        {#if projectionNote}
                                            <span class:budget-projected-note-warning={item.projected > item.available}>{projectionNote}</span>
                                        {/if}
                                    </div>
                                </div>

                                <div class="budget-attention-card-footer">
                                    <span>{item.recurring.length > 0 ? `${formatCurrency(item.recurringTotal)} recurring` : 'No recurring pressure'}</span>
                                    {#if editingCategory === item.category}
                                        <label class="budget-edit-field">
                                            <span>$</span>
                                            <input
                                                bind:value={editValue}
                                                type="number"
                                                min="0"
                                                step="1"
                                                on:keydown={(e) => { if (e.key === 'Enter') commitEdit(item.category); if (e.key === 'Escape') editingCategory = null; }}
                                                on:blur={() => commitEdit(item.category)} />
                                        </label>
                                    {:else}
                                        <button class="budget-amount-btn" disabled={savingCategory === item.category} on:click={() => startEdit(item.category, item.budget)}>
                                            Edit {formatCurrency(item.budget)}
                                        </button>
                                    {/if}
                                </div>
                            </article>
                        {:else}
                            <div class="budget-soft-state budget-attention-empty">
                                Everything active is tracking inside the monthly plan.
                            </div>
                        {/each}
                    </div>
                </section>

                <section class="budget-all-categories">
                    <div class="budget-subsection-heading">
                        <div>
                            <span class="budget-subsection-kicker">Compact ledger</span>
                            <h4>All Categories</h4>
                        </div>
                        <p>More categories stay scan-friendly without turning into a spreadsheet.</p>
                    </div>

                    <div class="budget-category-groups">
                        {#each plannerGroups as group}
                            <div
                                class="budget-category-group budget-group-{group.tone}"
                                class:budget-group-focus={focusedPlannerGroup === group.tone}>
                                <div class="budget-category-group-title">
                                    <span>{group.label}</span>
                                    <em>{group.items.length}</em>
                                </div>
                                {#if group.items.length > 0}
                                    <div class="budget-compact-list">
                                        {#each group.items as item, i}
                                            {@const projectionNote = projectedContextText(item)}
                                            <article
                                                id={categoryElementId(item.category)}
                                                class="budget-compact-row budget-category-tone-{item.status}"
                                                class:budget-highlight={highlightedCategory === item.category}
                                                style="--cat-color: {CATEGORY_COLORS[item.category] || '#627d98'}; animation-delay: {230 + i * 18}ms">
                                                <button class="budget-category-title budget-compact-title" on:click={() => loadCategoryTransactions(item.category)}>
                                                    <span class="budget-category-icon">
                                                        <span class="material-symbols-outlined">{CATEGORY_ICONS[item.category] || 'label'}</span>
                                                    </span>
                                                    <span>
                                                        <strong>{item.category}</strong>
                                                        <em>{item.expenseType === 'fixed' ? 'Fixed' : 'Variable'} · {item.budget > 0 ? formatCurrency(item.budget) : 'suggested'} plan</em>
                                                    </span>
                                                </button>

                                                <div class="budget-compact-pace">
                                                    {#if item.budget > 0}
                                                        <div class="budget-progress-track">
                                                            <div
                                                                class="budget-progress-fill budget-status-fill-{item.status}"
                                                                style="width: {Math.min(item.budgetPercent, 100)}%">
                                                            </div>
                                                            {#if projectionNote && item.projectedPercent > item.budgetPercent}
                                                                <div class="budget-progress-ghost" style="width: {Math.min(item.projectedPercent, 100)}%"></div>
                                                            {/if}
                                                        </div>
                                                        <span>{formatPercent(item.budgetPercent)}</span>
                                                    {:else if item.suggestedBudget > 0}
                                                        <div class="budget-draft-pill">Suggested {formatCurrency(item.suggestedBudget)}</div>
                                                    {:else}
                                                        <div class="budget-draft-pill">No plan yet</div>
                                                    {/if}
                                                </div>

                                                <div class="budget-compact-money">
                                                    <strong
                                                        class:budget-negative={item.remaining < 0}
                                                        class:budget-value-positive={isRemainingHealthy(item)}
                                                        class:budget-value-warning={isRemainingWatch(item)}
                                                        class:budget-value-unset={item.budget <= 0}>
                                                        {item.budget > 0 ? formatCurrency(item.remaining) : (item.suggestedBudget > 0 ? formatCurrency(item.suggestedBudget) : 'Unset')}
                                                    </strong>
                                                </div>

                                                <div class="budget-compact-actions">
                                                    {#if editingCategory === item.category}
                                                        <label class="budget-edit-field">
                                                            <span>$</span>
                                                            <input
                                                                bind:value={editValue}
                                                                type="number"
                                                                min="0"
                                                                step="1"
                                                                on:keydown={(e) => { if (e.key === 'Enter') commitEdit(item.category); if (e.key === 'Escape') editingCategory = null; }}
                                                                on:blur={() => commitEdit(item.category)} />
                                                        </label>
                                                    {:else if item.budget <= 0 && item.suggestedBudget > 0}
                                                        <button class="budget-amount-btn budget-suggested-action" disabled={savingCategory === item.category} on:click={() => applySuggestion(item)}>
                                                            Apply
                                                        </button>
                                                    {:else}
                                                        <button class="budget-row-menu" disabled={savingCategory === item.category} on:click={() => startEdit(item.category, item.budget)} aria-label="Edit {item.category} budget">
                                                            <span class="material-symbols-outlined">more_horiz</span>
                                                        </button>
                                                    {/if}
                                                </div>
                                            </article>
                                        {/each}
                                    </div>
                                {:else}
                                    <div class="budget-board-empty">No categories</div>
                                {/if}
                            </div>
                        {/each}
                    </div>
                </section>
            </div>
        {:else}
            <div class="budget-soft-state budget-category-empty card">
                No spending categories for {selectedMonth ? formatMonth(selectedMonth) : 'this month'}.
            </div>
        {/if}
    </section>

    {#if selectedCategory}
        <section class="budget-drilldown card fade-in-up">
            <div class="budget-drilldown-header">
                <div>
                    <h3>{selectedCategory}</h3>
                    <p>{formatMonth(selectedMonth)} transactions behind this budget line.</p>
                </div>
                <button on:click={closeTransactions} aria-label="Close transaction drilldown">
                    <span class="material-symbols-outlined">close</span>
                </button>
            </div>

            {#if transactionsLoading}
                <div class="budget-soft-state">Loading transactions...</div>
            {:else if categoryTransactions.length > 0}
                <div class="budget-transaction-list">
                    {#each categoryTransactions as tx}
                        <div class="budget-transaction-row">
                            <div>
                                <strong>{tx.merchant_display_name || tx.merchant_name || tx.description}</strong>
                                <span>{formatDate(tx.date)} · {tx.account_name || 'Account'}</span>
                            </div>
                            <strong>{formatCurrency(Math.abs(parseFloat(tx.amount || 0)))}</strong>
                        </div>
                    {/each}
                </div>
            {:else}
                <div class="budget-soft-state">No transactions found for this category and month.</div>
            {/if}
        </section>
    {/if}
</div>
{/if}
