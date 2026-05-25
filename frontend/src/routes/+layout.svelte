<script>
    import '../app.css';
    import { page } from '$app/stores';
    import { beforeNavigate, afterNavigate, goto } from '$app/navigation';    
    import { darkMode, syncing } from '$lib/stores.js';
    import { api } from '$lib/api.js';
    import { relativeTime } from '$lib/utils.js';
    import { onMount } from 'svelte';
    import { activeProfile, loadProfiles } from '$lib/stores/profileStore.js';

    let lastSynced = null;
    let syncPollTimer = null;
    let lastCompletedSyncKey = null;
    let currentSyncKey = null;
    let backendSyncSeen = false;
    let noBackendSyncSeenCount = 0;
    let receiptToast = null;
    let receiptToastTimer = null;
    let appConfig = {
        demoMode: false,
        manualSyncEnabled: true,
        bankLinkingEnabled: true,
        miraEnabled: false,
        demoPersistence: 'persistent'
    };

    /* —— Navigation —— */
    const navPrimary = [
        { path: '/',             icon: 'dashboard',              label: 'Dashboard' },
        { path: '/transactions', icon: 'receipt_long',           label: 'Transactions' },
    ];

    const navSecondary = [
        { path: '/analytics',    icon: 'monitoring',             label: 'Analytics' },
        { path: '/budget',       icon: 'account_balance_wallet', label: 'Budgets' },
    ];

    const copilotItem = { path: '/copilot', label: 'Mira' };
    const controlCenterItem = { path: '/control-center', icon: 'tune', label: 'Control Center' };

    /* ── Sync ── */
    async function handleSync() {
        if (!appConfig.manualSyncEnabled) return;
        syncing.start('manual-sync');
        try {
            const result = await api.sync();
            lastSynced = result.last_updated || new Date().toISOString();
            loadProfiles();
            await pollSyncStatus();
        } catch (e) {
            console.error('Sync failed:', e);
        }
    }

    function notifySyncComplete(status) {
        const key = `${status?.job_id || 'none'}:${status?.completed_at || 'none'}:${status?.status || 'completed'}`;
        if (key === lastCompletedSyncKey) return;
        lastCompletedSyncKey = key;
        window.dispatchEvent(new CustomEvent('folio:sync-complete', {
            detail: status || { status: 'completed' }
        }));
    }

    function parseIsoMs(value) {
        if (!value) return null;
        const ms = Date.parse(value);
        return Number.isFinite(ms) ? ms : null;
    }

    function statusMatchesCurrentSync(status) {
        if (!$syncing.active || !$syncing.context || !status) return false;
        if (status.source !== $syncing.context) return false;

        const localStartedAt = Number($syncing.startedAt || 0);
        if (!localStartedAt) return true;

        const remoteStartedAt = parseIsoMs(status.started_at);
        const remoteCompletedAt = parseIsoMs(status.completed_at);
        const remoteRelevantAt = remoteStartedAt ?? remoteCompletedAt;
        if (!remoteRelevantAt) return false;

        // Allow small skew between the client clock and backend timestamps.
        return remoteRelevantAt >= (localStartedAt - 5000);
    }

    function completeCurrentSync(status) {
        if (status?.status === 'completed') {
            lastSynced = status?.completed_at || lastSynced || new Date().toISOString();
        }
        stopSyncStatusPolling();
        loadProfiles();
        notifySyncComplete(status);
        syncing.stop();
    }

    function clearStaleSyncState() {
        stopSyncStatusPolling();
        syncing.stop();
        backendSyncSeen = false;
        noBackendSyncSeenCount = 0;
    }

    function stopSyncStatusPolling() {
        if (syncPollTimer) {
            clearInterval(syncPollTimer);
            syncPollTimer = null;
        }
    }

    async function pollSyncStatus() {
        try {
            const status = await api.getSyncStatus();
            if (status?.active) {
                if (statusMatchesCurrentSync(status)) {
                    backendSyncSeen = true;
                    noBackendSyncSeenCount = 0;
                }
                return;
            }

            // Ignore "inactive" responses until we've seen either:
            // 1. a backend job that matches this client-side sync, or
            // 2. a completed matching job snapshot with a matching timestamp.
            if (!statusMatchesCurrentSync(status)) {
                if ($syncing.active && !backendSyncSeen) {
                    noBackendSyncSeenCount += 1;

                    // Recover from stale sessionStorage state after backend restarts
                    // or after a previously rate-limited poller never observed a real job.
                    if (noBackendSyncSeenCount >= 5) {
                        clearStaleSyncState();
                    }
                }
                return;
            }

            if (status?.status === 'completed') {
                completeCurrentSync(status);
                return;
            }

            if (status?.status === 'failed' && backendSyncSeen) {
                completeCurrentSync(status);
            }
        } catch (e) {
            console.warn('Sync status poll failed (will retry):', e.message);
        }
    }

    function startSyncStatusPolling() {
        if (syncPollTimer) return;
        syncPollTimer = setInterval(pollSyncStatus, 3000);
        setTimeout(pollSyncStatus, 250);
    }

    function dismissReceiptToast() {
        if (receiptToastTimer) {
            clearTimeout(receiptToastTimer);
            receiptToastTimer = null;
        }
        receiptToast = null;
    }

    function showReceiptToast(event) {
        receiptToast = event.detail || null;
        if (!receiptToast?.receiptId) return;
        if (receiptToastTimer) clearTimeout(receiptToastTimer);
        receiptToastTimer = setTimeout(dismissReceiptToast, 12000);
    }

    function reviewReceiptToast() {
        if (!receiptToast?.receiptId) return;
        const profile = receiptToast.profile || 'household';
        activeProfile.set(profile);
        goto(`/copilot?receipt=${encodeURIComponent(receiptToast.receiptId)}&receiptProfile=${encodeURIComponent(profile)}`);
        dismissReceiptToast();
    }

    /* —— Mouse tracking —— */
    let mouseX = '50%';
    let mouseY = '50%';

    let glowRafId = null;

    function handleMouseMove(e) {
        mouseX = e.clientX + 'px';
        mouseY = e.clientY + 'px';

        if (glowRafId) return;
        glowRafId = requestAnimationFrame(() => {
            document.documentElement.style.setProperty('--mx', mouseX);
            document.documentElement.style.setProperty('--my', mouseY);
            glowRafId = null;
        });
    }

    let cardRafId = null;
    let lastCardMouseX = 0;
    let lastCardMouseY = 0;

    function handleCardMouseMove(e) {
        lastCardMouseX = e.clientX;
        lastCardMouseY = e.clientY;

        if (cardRafId) return;
        cardRafId = requestAnimationFrame(() => {
            const cards = document.querySelectorAll(
                '.card, .card-hero, .card-accounts, .card-credit, .metric-ribbon, .card-insight, .card-forecast, .card-upcoming'
            );
            for (let i = 0; i < cards.length; i++) {
                const rect = cards[i].getBoundingClientRect();
                cards[i].style.setProperty('--card-mx', (lastCardMouseX - rect.left) + 'px');
                cards[i].style.setProperty('--card-my', (lastCardMouseY - rect.top) + 'px');
            }

            /* ââ Rail glow tracking ââ */
            const rail = document.querySelector('.glass-rail');
            if (rail) {
                const rect = rail.getBoundingClientRect();
                rail.style.setProperty('--rail-mx', (lastCardMouseX - rect.left) + 'px');
                rail.style.setProperty('--rail-my', (lastCardMouseY - rect.top) + 'px');
            }

            cardRafId = null;
        });
    }

    onMount(async () => {
        loadProfiles();
        try {
            appConfig = { ...appConfig, ...(await api.getAppConfig()) };
        } catch (_) {}
        setTimeout(async () => {
            try {
                const summary = await api.getSummary();
                lastSynced = summary.last_updated;
            } catch (_) {}
        }, 100);
        window.addEventListener('mousemove', handleCardMouseMove, { passive: true });
        window.addEventListener('folio:receipt-parse-done', showReceiptToast);
        return () => {
            stopSyncStatusPolling();
            window.removeEventListener('mousemove', handleCardMouseMove);
            window.removeEventListener('folio:receipt-parse-done', showReceiptToast);
            if (receiptToastTimer) clearTimeout(receiptToastTimer);
        };
    });

    $: if ($syncing.active) {
        startSyncStatusPolling();
    } else {
        stopSyncStatusPolling();
    }

    $: {
        const nextSyncKey = $syncing.active ? `${$syncing.context}:${$syncing.startedAt}` : null;
        if (nextSyncKey !== currentSyncKey) {
            currentSyncKey = nextSyncKey;
            backendSyncSeen = false;
            noBackendSyncSeenCount = 0;
        }
    }

    /* ââ Suppress backdrop-filter during navigation ââ */
    beforeNavigate(() => {
        document.documentElement.classList.add('theme-switching');
    });

    afterNavigate(() => {
        requestAnimationFrame(() => {
            requestAnimationFrame(() => {
                document.documentElement.classList.remove('theme-switching');
            });
        });
    });

    let mobileMenuOpen = false;

    /* —— Keyboard shortcut —— */
    function handleKeyboard(e) {
        if (['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName)) return;
        if (e.key === 't' && !e.ctrlKey && !e.metaKey) darkMode.toggle();
    }

    /* —— Route matching —— */
    $: currentPath = $page.url.pathname;

    function isActive(path, current) {
        if (path === '/') return current === '/';
        return current.startsWith(path);
    }
</script>

<svelte:window on:keydown={handleKeyboard} on:mousemove={handleMouseMove} />

<!-- Background layers -->
<div class="page-glow"></div>
<div class="mesh-canvas-layer" aria-hidden="true"></div>

<!-- ———————————————————————————————————————————
     GLASS RAIL v3 — Desktop Sidebar
     ——————————————————————————————————————————— -->
<aside
  class="glass-rail hidden md:flex flex-col h-screen fixed left-0 top-0 z-50 rail-glow-edge"
  style="width: var(--rail-w, 260px);"
>

    <!-- Glass effect layers (positioned container) -->
    <div class="rail-effects-layer" aria-hidden="true">
        <span class="rail-shine"></span>
        <span class="rail-glow"></span>
        <span class="rail-inner-glow"></span>
    </div>

    <!-- Brand -->
    <div class="rail-brand">
        <div class="rail-brand-mark">
            {#if $darkMode}
                <img src="/folio-mark-dark-mode.png" alt="Folio" class="folio-mark" draggable="false" />
            {:else}
                <img src="/folio-mark-light-mode.png" alt="Folio" class="folio-mark" draggable="false" />
            {/if}
        </div>
        <div class="rail-brand-text">
            <span class="rail-brand-name">Folio</span>
            <span class="rail-brand-sub">Personal Finance Dashboard</span>
        </div>
    </div>

    <!-- ✅ CHANGED: Glowing separator after brand -->
    <div class="rail-glow-separator" aria-hidden="true"></div>

    <!-- Navigation -->
    <nav class="rail-nav">

        <!-- Primary group -->
        <div class="rail-nav-group">
            {#each navPrimary as item}
                {@const active = isActive(item.path, currentPath)}
                <a href={item.path}
                   class="rail-link"
                   class:rail-link--active={active}
                   aria-current={active ? 'page' : undefined}>
                    {#if active}<span class="rail-active-bar" aria-hidden="true"></span>{/if}
                    <span class="rail-link-icon material-symbols-outlined"
                          style={active ? "font-variation-settings: 'FILL' 1;" : ''}>
                        {item.icon}
                    </span>
                    <span class="rail-link-label">{item.label}</span>
                </a>
            {/each}
        </div>

        <!-- ✅ CHANGED: Glowing separator between primary and secondary -->
        <div class="rail-glow-separator" aria-hidden="true"></div>

        <!-- Secondary group -->
        <div class="rail-nav-group rail-nav-group--secondary">
            {#each navSecondary as item}
                {@const active = isActive(item.path, currentPath)}
                <a href={item.path}
                   class="rail-link rail-link--sm"
                   class:rail-link--active={active}
                   aria-current={active ? 'page' : undefined}>
                    {#if active}<span class="rail-active-bar" aria-hidden="true"></span>{/if}
                    <span class="rail-link-icon material-symbols-outlined"
                          style={active ? "font-variation-settings: 'FILL' 1;" : ''}>
                        {item.icon}
                    </span>
                    <span class="rail-link-label">{item.label}</span>
                </a>
            {/each}
        </div>

        <!-- ✅ CHANGED: Glowing separator before Copilot -->
        <div class="rail-glow-separator" aria-hidden="true"></div>

        <!-- Copilot group -->
        <div class="rail-nav-group rail-nav-group--copilot">
            {#if appConfig.miraEnabled}
                <a href={copilotItem.path}
                   class="rail-link rail-link--copilot"
                   class:rail-link--active={isActive(copilotItem.path, currentPath)}
                   class:rail-link--copilot-active={isActive(copilotItem.path, currentPath)}
                   aria-current={isActive(copilotItem.path, currentPath) ? 'page' : undefined}>
                    {#if isActive(copilotItem.path, currentPath)}<span class="rail-active-bar rail-active-bar--copilot" aria-hidden="true"></span>{/if}
                    <span class="rail-link-icon rail-copilot-icon-inline mira-mark mira-mark--nav" aria-hidden="true"></span>
                    <span class="rail-link-label">{copilotItem.label}</span>
                </a>
            {/if}
            <a href={controlCenterItem.path}
               class="rail-link rail-link--sm"
               class:rail-link--active={isActive(controlCenterItem.path, currentPath)}
               aria-current={isActive(controlCenterItem.path, currentPath) ? 'page' : undefined}>
                {#if isActive(controlCenterItem.path, currentPath)}<span class="rail-active-bar" aria-hidden="true"></span>{/if}
                <span class="rail-link-icon material-symbols-outlined"
                      style={isActive(controlCenterItem.path, currentPath) ? "font-variation-settings: 'FILL' 1;" : ''}>
                    {controlCenterItem.icon}
                </span>
                <span class="rail-link-label">{controlCenterItem.label}</span>
            </a>
        </div>

        <!-- ✅ CHANGED: Spacer pushes footer down (Copilot no longer at bottom) -->
        <div class="flex-1 min-h-[24px]"></div>

    </nav>

    <div class="rail-divider" aria-hidden="true"></div>

    <!-- Footer — Status Bar -->
    <div class="rail-footer">
        {#if appConfig.manualSyncEnabled}
            <button on:click={handleSync} disabled={$syncing.active}
                    class="rail-footer-row rail-footer-row--interactive">
                <span class="rail-sync-dot" class:rail-sync-dot--spinning={$syncing.active}></span>
                <span class="rail-footer-label">
                    {#if $syncing.active}
                        Syncing...¦
                    {:else if lastSynced}
                        <span class="rail-footer-accent">Synced</span> · {relativeTime(lastSynced)}
                    {:else}
                        Sync now
                    {/if}
                </span>
                <span class="material-symbols-outlined rail-footer-action"
                      class:animate-spin={$syncing.active}>
                    {$syncing.active ? 'progress_activity' : 'sync'}
                </span>
            </button>
        {:else}
            <div class="rail-footer-row">
                <span class="material-symbols-outlined rail-footer-icon">visibility</span>
                <span class="rail-footer-label">Public demo</span>
            </div>
        {/if}

        <button on:click={() => darkMode.toggle()}
                class="rail-footer-row rail-footer-row--interactive"
                title="Toggle theme (T)">
            <span class="material-symbols-outlined rail-footer-icon">
                {$darkMode ? 'dark_mode' : 'light_mode'}
            </span>
            <span class="rail-footer-label">
                {$darkMode ? 'Dark' : 'Light'} mode
            </span>
            <span class="rail-toggle-track" class:rail-toggle-track--on={$darkMode}>
                <span class="rail-toggle-thumb"></span>
            </span>
        </button>
    </div>
</aside>


<!-- ———————————————————————————————————————————
     MOBILE NAV
     ——————————————————————————————————————————— -->
{#if mobileMenuOpen}
    <div class="md:hidden fixed inset-0 z-[60]">
        <button
            type="button"
            class="absolute inset-0 bg-black/50 backdrop-blur-sm border-0 p-0"
            aria-label="Close mobile menu"
            on:click={() => mobileMenuOpen = false}></button>
        <nav class="glass-rail absolute left-0 top-0 bottom-0 w-[260px] flex flex-col">

            <div class="rail-effects-layer" aria-hidden="true">
                <span class="rail-shine"></span>
                <span class="rail-inner-glow"></span>
            </div>

            <div class="rail-brand">
                <div class="rail-brand-mark">
                    {#if $darkMode}
                        <img src="/folio-mark-dark-mode.png" alt="Folio" class="folio-mark" draggable="false" />
                    {:else}
                        <img src="/folio-mark-light-mode.png" alt="Folio" class="folio-mark" draggable="false" />
                    {/if}
                </div>
                <div class="rail-brand-text">
                    <span class="rail-brand-name">Folio</span>
                </div>
            </div>

            <!-- ✅ CHANGED: Mobile — glowing separators -->
            <div class="rail-glow-separator" aria-hidden="true"></div>

            <div class="rail-nav">
                <div class="rail-nav-group">
                    {#each navPrimary as item}
                        {@const active = isActive(item.path, currentPath)}
                        <a href={item.path} on:click={() => mobileMenuOpen = false}
                           class="rail-link"
                           class:rail-link--active={active}
                           aria-current={active ? 'page' : undefined}>
                            {#if active}<span class="rail-active-bar"></span>{/if}
                            <span class="rail-link-icon material-symbols-outlined"
                                  style={active ? "font-variation-settings: 'FILL' 1;" : ''}>
                                {item.icon}
                            </span>
                            <span class="rail-link-label">{item.label}</span>
                        </a>
                    {/each}
                </div>

                <!-- ✅ CHANGED: Mobile glowing separator -->
                <div class="rail-glow-separator" aria-hidden="true"></div>

                <div class="rail-nav-group rail-nav-group--secondary">
                    {#each navSecondary as item}
                        {@const active = isActive(item.path, currentPath)}
                        <a href={item.path} on:click={() => mobileMenuOpen = false}
                           class="rail-link rail-link--sm"
                           class:rail-link--active={active}
                           aria-current={active ? 'page' : undefined}>
                            {#if active}<span class="rail-active-bar"></span>{/if}
                            <span class="rail-link-icon material-symbols-outlined"
                                  style={active ? "font-variation-settings: 'FILL' 1;" : ''}>
                                {item.icon}
                            </span>
                            <span class="rail-link-label">{item.label}</span>
                        </a>
                    {/each}
                </div>

                <!-- ✅ CHANGED: Mobile glowing separator before Copilot -->
                <div class="rail-glow-separator" aria-hidden="true"></div>

                <!-- Mobile Copilot group -->
                <div class="rail-nav-group rail-nav-group--copilot">
                    {#if appConfig.miraEnabled}
                        <a href={copilotItem.path} on:click={() => mobileMenuOpen = false}
                           class="rail-link rail-link--copilot"
                           class:rail-link--active={isActive(copilotItem.path, currentPath)}
                           class:rail-link--copilot-active={isActive(copilotItem.path, currentPath)}
                           aria-current={isActive(copilotItem.path, currentPath) ? 'page' : undefined}>
                            {#if isActive(copilotItem.path, currentPath)}<span class="rail-active-bar rail-active-bar--copilot"></span>{/if}
                            <span class="rail-link-icon rail-copilot-icon-inline mira-mark mira-mark--nav" aria-hidden="true"></span>
                            <span class="rail-link-label">{copilotItem.label}</span>
                        </a>
                    {/if}
                    <a href={controlCenterItem.path} on:click={() => mobileMenuOpen = false}
                       class="rail-link rail-link--sm"
                       class:rail-link--active={isActive(controlCenterItem.path, currentPath)}
                       aria-current={isActive(controlCenterItem.path, currentPath) ? 'page' : undefined}>
                        {#if isActive(controlCenterItem.path, currentPath)}<span class="rail-active-bar"></span>{/if}
                        <span class="rail-link-icon material-symbols-outlined"
                              style={isActive(controlCenterItem.path, currentPath) ? "font-variation-settings: 'FILL' 1;" : ''}>
                            {controlCenterItem.icon}
                        </span>
                        <span class="rail-link-label">{controlCenterItem.label}</span>
                    </a>
                </div>

                <div class="flex-1"></div>
            </div>

            <div class="rail-divider"></div>

            <div class="rail-footer">
                {#if appConfig.manualSyncEnabled}
                    <button on:click={handleSync} disabled={$syncing.active}
                            class="rail-footer-row rail-footer-row--interactive">
                        <span class="rail-sync-dot" class:rail-sync-dot--spinning={$syncing.active}></span>
                        <span class="rail-footer-label">
                            {$syncing.active ? 'Syncingâ¦' : 'Sync'}
                        </span>
                    </button>
                {/if}
                <button on:click={() => darkMode.toggle()}
                        class="rail-footer-row rail-footer-row--interactive">
                    <span class="material-symbols-outlined rail-footer-icon">
                        {$darkMode ? 'dark_mode' : 'light_mode'}
                    </span>
                    <span class="rail-footer-label">{$darkMode ? 'Dark' : 'Light'}</span>
                    <span class="rail-toggle-track" class:rail-toggle-track--on={$darkMode}>
                        <span class="rail-toggle-thumb"></span>
                    </span>
                </button>
            </div>
        </nav>
    </div>
{/if}

<!-- Mobile top bar -->
<div class="md:hidden fixed top-0 left-0 right-0 z-50 flex items-center justify-between px-4 h-14"
     style="background: var(--sidebar-bg); backdrop-filter: blur(20px); border-bottom: 1px solid var(--sidebar-border);">
    <button on:click={() => mobileMenuOpen = !mobileMenuOpen} class="p-1.5 rounded-lg"
            style="color: var(--sidebar-text-active)">
        <span class="material-symbols-outlined">{mobileMenuOpen ? 'close' : 'menu'}</span>
    </button>
    <span class="mobile-brand-name" style="color: var(--sidebar-text-active)">
        Folio
    </span>
    {#if appConfig.manualSyncEnabled}
        <button on:click={handleSync} disabled={$syncing.active} class="p-1.5 rounded-lg"
                style="color: var(--sidebar-text-active)">
            <span class="material-symbols-outlined text-[20px]" class:animate-spin={$syncing.active}>sync</span>
        </button>
    {:else}
        <span class="mobile-topbar-spacer" aria-hidden="true"></span>
    {/if}
</div>

<!-- Main content -->
<main class="md:ml-[var(--rail-width)] pt-14 md:pt-0 min-h-screen transition-all duration-300 relative z-[1]">
    <div class="max-w-[1800px] mx-auto px-4 md:px-8 py-8">
        <slot />
    </div>
</main>

{#if receiptToast}
    <aside class="receipt-ready-toast fade-in" aria-live="polite">
        <span class="receipt-ready-toast-icon material-symbols-outlined">receipt_long</span>
        <div class="receipt-ready-toast-copy">
            <strong>Receipt parsing is done</strong>
            <span>{receiptToast.storeName || receiptToast.fileName || 'Receipt'} is ready to validate.</span>
        </div>
        <button type="button" class="receipt-ready-toast-review" on:click={reviewReceiptToast}>Review</button>
        <button type="button" class="receipt-ready-toast-close" on:click={dismissReceiptToast} aria-label="Dismiss receipt notification">
            <span class="material-symbols-outlined">close</span>
        </button>
    </aside>
{/if}

<style>

    .receipt-ready-toast {
        position: fixed;
        right: 1.25rem;
        top: 5.25rem;
        z-index: 70;
        display: grid;
        grid-template-columns: auto minmax(0, 1fr) auto auto;
        align-items: center;
        gap: 0.6rem;
        width: min(400px, calc(100vw - 2rem));
        padding: 0.65rem;
        border: 1px solid color-mix(in srgb, var(--accent) 28%, var(--card-border));
        border-radius: 0.85rem;
        background: color-mix(in srgb, var(--card-bg) 94%, transparent);
        box-shadow: var(--card-shadow-hover);
        backdrop-filter: blur(14px);
        -webkit-backdrop-filter: blur(14px);
    }

    .receipt-ready-toast-icon {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 2rem;
        height: 2rem;
        border-radius: 0.65rem;
        background: color-mix(in srgb, var(--accent) 12%, transparent);
        color: var(--accent);
        font-size: 1rem;
    }

    .receipt-ready-toast-copy {
        min-width: 0;
    }

    .receipt-ready-toast-copy strong,
    .receipt-ready-toast-copy span {
        display: block;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }

    .receipt-ready-toast-copy strong {
        color: var(--text-primary);
        font-size: 0.82rem;
        font-weight: 800;
    }

    .receipt-ready-toast-copy span {
        color: var(--text-muted);
        font-size: 0.68rem;
        font-weight: 650;
    }

    .receipt-ready-toast-review,
    .receipt-ready-toast-close {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        border: 1px solid var(--card-border);
        border-radius: 0.6rem;
        min-height: 1.9rem;
        background: var(--card-bg);
        color: var(--text-secondary);
        font-size: 0.68rem;
        font-weight: 800;
    }

    .receipt-ready-toast-review {
        padding: 0 0.65rem;
        border-color: color-mix(in srgb, var(--accent) 34%, var(--card-border));
        background: var(--accent);
        color: white;
    }

    .receipt-ready-toast-close {
        width: 1.9rem;
        color: var(--text-muted);
    }

    .receipt-ready-toast-close .material-symbols-outlined {
        font-size: 1rem;
    }

    @media (max-width: 760px) {
        .receipt-ready-toast {
            right: 1rem;
            left: 1rem;
            top: 4.25rem;
            width: auto;
        }
    }

    /* ———————————————————————————————————————————
       GLASS RAIL v3 — Unified Glass Navigation
       ——————————————————————————————————————————— */

    /* —— Surface: Glass container with inner glow —— */
    .glass-rail {
        width: var(--rail-width);
        background: var(--rail-bg);
        backdrop-filter: blur(var(--rail-blur)) saturate(var(--rail-saturate));
        -webkit-backdrop-filter: blur(var(--rail-blur)) saturate(var(--rail-saturate));
        border-right: 1px solid var(--rail-border);
        box-shadow:
            var(--rail-shadow),
            inset 0 0 30px rgba(148, 163, 184, 0.02),
            inset 0 1px 0 rgba(255, 255, 255, 0.04),
            inset -1px 0 0 rgba(255, 255, 255, 0.02);
        overflow: hidden;
        will-change: transform;
        transform: translateZ(0);
        -webkit-backface-visibility: hidden;
        backface-visibility: hidden;
        transition:
            background var(--rail-transition),
            border-color var(--rail-transition),
            box-shadow 0.35s var(--ease-out-expo);
    }

    /* —— Effects container —— */
    .rail-effects-layer {
        position: absolute;
        inset: 0;
        overflow: hidden;
        border-radius: inherit;
        pointer-events: none;
        z-index: 0;
        display: none;
    }

    :global(.dark) .rail-effects-layer {
        display: block;
    }

    /* ✅ CHANGED: Dark mode — neutral charcoal inner glow instead of blue */
    :global(.dark) .glass-rail {
        box-shadow:
            var(--rail-shadow),
            0 0 0 1px rgba(148, 163, 184, 0.04),
            0 0 30px rgba(0, 0, 0, 0.15),
            inset 0 1px 0 rgba(255, 255, 255, 0.04),
            inset 0 -1px 0 rgba(148, 163, 184, 0.02),
            inset -1px 0 0 rgba(255, 255, 255, 0.02);
    }

    .glass-rail:hover {
        box-shadow:
            var(--rail-shadow),
            inset 0 1px 0 rgba(255, 255, 255, 0.06),
            inset -1px 0 0 rgba(255, 255, 255, 0.03);
    }

    /* ✅ CHANGED: Dark mode hover — neutral glow */
    :global(.dark) .glass-rail:hover {
        box-shadow:
            var(--rail-shadow),
            0 0 0 1px rgba(148, 163, 184, 0.06),
            0 0 40px rgba(0, 0, 0, 0.18),
            inset 0 1px 0 rgba(255, 255, 255, 0.05),
            inset 0 -1px 0 rgba(148, 163, 184, 0.03),
            inset -1px 0 0 rgba(255, 255, 255, 0.03);
    }

    :global(.theme-switching) .glass-rail {
        backdrop-filter: none !important;
        -webkit-backdrop-filter: none !important;
    }

    /* —— Glass shine overlay —— */
    .rail-shine {
        position: absolute;
        inset: 0;
        border-radius: inherit;
        background: linear-gradient(
            160deg,
            rgba(255, 255, 255, 0.03) 0%,
            rgba(255, 255, 255, 0.01) 40%,
            transparent 100%
        );
        pointer-events: none;
        z-index: 0;
    }

    :global(.dark) .rail-shine {
        background: var(--glass-shine);
        opacity: 0.5;
    }

    :global(:root:not(.dark)) .rail-shine {
        background: linear-gradient(
            160deg,
            rgba(255, 255, 255, 0.45) 0%,
            rgba(255, 255, 255, 0.06) 40%,
            rgba(255, 255, 255, 0.03) 100%
        );
        opacity: 0.5;
    }

    /* ✅ CHANGED: Mouse-tracking glow — neutral silver instead of blue */
    .rail-glow {
        position: absolute;
        inset: 0;
        border-radius: inherit;
        opacity: 0;
        transition: opacity var(--duration-normal) ease;
        background:
            radial-gradient(
                400px circle at var(--rail-mx, 50%) var(--rail-my, 50%),
                rgba(15, 23, 42, 0.035) 0%,
                transparent 55%
            );
        pointer-events: none;
        z-index: 0;
    }

    /* ✅ CHANGED: Dark mode rail glow — neutral */
    :global(.dark) .rail-glow {
        background:
            radial-gradient(
                400px circle at var(--rail-mx, 50%) var(--rail-my, 50%),
                rgba(148, 163, 184, 0.06) 0%,
                transparent 50%
            );
    }

    :global(.dark) .glass-rail:hover .rail-glow {
        opacity: 1;
    }

    /* —— Inner border glow —— */
    .rail-inner-glow {
        position: absolute;
        inset: 0;
        border-radius: inherit;
        pointer-events: none;
        z-index: 0;
    }

    /* ✅ CHANGED: Dark mode inner glow — neutral edges */
    :global(.dark) .rail-inner-glow {
        box-shadow:
            inset 1px 0 0 rgba(148, 163, 184, 0.04),
            inset -1px 0 0 rgba(148, 163, 184, 0.03),
            inset 0 1px 0 rgba(148, 163, 184, 0.05),
            inset 0 -1px 0 rgba(148, 163, 184, 0.03);
    }

    .rail-inner-glow {
        box-shadow:
            inset 0 1px 0 rgba(255, 255, 255, 0.68),
            inset 0 -1px 0 rgba(0, 0, 0, 0.02),
            inset -1px 0 0 rgba(15, 23, 42, 0.035);
    }

    /* —— Brand —— */
    .rail-brand {
        display: flex;
        align-items: center;
        gap: 12px;
        padding: 26px 22px 22px;
        position: relative;
        z-index: 1;
    }

    .rail-brand-mark {
        width: 50px;
        height: 50px;
        flex-shrink: 0;
        position: relative;
        border-radius: 12px;
        /* NO overflow:hidden — let the image glow breathe */
        transition: transform 0.35s ease, filter 0.35s ease;
    }

    .rail-brand-mark:hover {
        transform: scale(1.08);
        filter: brightness(1.15);
    }

    .rail-brand-mark .folio-mark {
        display: block;
        width: 100%;
        height: 100%;
        object-fit: contain;
        border-radius: inherit;
        filter: drop-shadow(0 4px 10px rgba(15, 23, 42, 0.10));
    }

    :global(.dark) .rail-brand-mark .folio-mark {
        filter: drop-shadow(0 0 6px rgba(90, 159, 212, 0.28))
                drop-shadow(0 0 14px rgba(90, 159, 212, 0.10));
    }

    .mobile-topbar-spacer {
        display: inline-block;
        width: 32px;
        height: 32px;
        flex-shrink: 0;
    }

    .mobile-brand-name {
        font-family: var(--font-nav);
        font-size: 15px;
        font-weight: 800;
        letter-spacing: -0.035em;
        line-height: 1;
    }

    .rail-brand-text {
        display: flex;
        flex-direction: column;
        gap: 2px;
        min-width: 0;
    }

    .rail-brand-name {
        font-family: var(--font-nav);
        font-size: 18px;
        font-weight: 800;
        letter-spacing: -0.045em;
        line-height: 1.15;
        color: var(--sidebar-text-active);
        /* Light mode: solid text with subtle shadow */
        text-shadow: 0 1px 2px rgba(0, 0, 0, 0.06);
    }

    :global(.dark) .rail-brand-name {
        background: linear-gradient(
            135deg,
            #e0e7ff 0%,      /* brighter start — near-white lavender */
            #a5b4fc 35%,     /* indigo mid */
            #c4b5fd 60%,     /* violet */
            #93c5fd 100%     /* sky blue finish */
        );
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        background-clip: text;
        filter: drop-shadow(0 0 8px rgba(165, 180, 252, 0.3));
    }

    .rail-brand-sub {
        font-family: var(--font-nav);
        font-size: 9px;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.14em;
        color: var(--sidebar-text);
        opacity: 0.45;
        line-height: 1;
    }

    :global(.dark) .rail-brand-sub {
        opacity: 0.4;
        background: linear-gradient(90deg, rgba(148, 163, 184, 0.7), rgba(148, 163, 184, 0.4));
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        background-clip: text;
    }

    /* —— Old Divider (kept for footer separator) —— */
    .rail-divider {
        height: 1px;
        margin: 0 18px;
        background: linear-gradient(
            90deg,
            transparent 0%,
            var(--rail-border) 5%,
            var(--rail-border) 95%,
            transparent 100%
        );
        position: relative;
        z-index: 1;
    }

    :global(.dark) .rail-divider {
        background: linear-gradient(
            90deg,
            transparent 0%,
            color-mix(in srgb, var(--rail-separator-glow) 92%, transparent) 5%,
            color-mix(in srgb, var(--rail-separator-glow-center) 92%, transparent) 30%,
            var(--rail-separator-glow-center) 50%,
            color-mix(in srgb, var(--rail-separator-glow-center) 92%, transparent) 70%,
            color-mix(in srgb, var(--rail-separator-glow) 92%, transparent) 95%,
            transparent 100%
        );
        box-shadow:
            0 0 20px color-mix(in srgb, var(--rail-separator-glow-center) 52%, transparent),
            0 0 38px color-mix(in srgb, var(--rail-separator-glow) 34%, transparent);
    }

    /* ✅ NEW: Glowing Separator — premium luminous line matching dashboard glow language */
    .rail-glow-separator {
        height: 1px;
        width: 190px;
        margin: 12px auto;
        align-self: center;
        position: relative;
        z-index: 1;
        background: linear-gradient(
            90deg,
            transparent 0%,
            var(--rail-separator-glow, rgba(185, 200, 220, 0.25)) 5%,
            var(--rail-separator-glow-center, rgba(185, 200, 220, 0.50)) 50%,
            var(--rail-separator-glow, rgba(185, 200, 220, 0.25)) 95%,
            transparent 100%
        );
    }



    /* ✅ NEW: Glowing separator — dark mode glow halo */
    :global(.dark) .rail-glow-separator {
        background: linear-gradient(
            90deg,
            transparent 0%,
            color-mix(in srgb, var(--rail-separator-glow) 92%, transparent) 5%,
            color-mix(in srgb, var(--rail-separator-glow-center) 88%, transparent) 30%,
            var(--rail-separator-glow-center) 50%,
            color-mix(in srgb, var(--rail-separator-glow-center) 88%, transparent) 70%,
            color-mix(in srgb, var(--rail-separator-glow) 92%, transparent) 95%,
            transparent 100%
        );
        box-shadow:
            0 0 18px color-mix(in srgb, var(--rail-separator-glow-center) 54%, transparent),
            0 0 34px color-mix(in srgb, var(--rail-separator-glow) 32%, transparent);
    }

    /* —— Nav —— */
    .rail-nav {
        flex: 1;
        display: flex;
        flex-direction: column;
        padding: 12px 10px 8px;
        overflow-y: auto;
        overflow-x: hidden;
        position: relative;
        z-index: 1;
    }

    .rail-nav-group {
        display: flex;
        flex-direction: column;
        gap: 2px;
    }

    .rail-nav-group--secondary .rail-link {
        font-size: 14px;
    }
    .rail-nav-group--secondary .rail-link-icon {
        font-size: 19px;
    }

    /* ✅ NEW: Copilot nav group — no extra padding needed, inherits from rail-nav-group */
    .rail-nav-group--copilot {
        display: flex;
        flex-direction: column;
        gap: 2px;
    }

    /* —— Nav Link —— */
    .rail-link {
        position: relative;
        display: flex;
        align-items: center;
        gap: 12px;
        padding: 10px 14px;
        border-radius: 11px;
        text-decoration: none;
        color: var(--sidebar-text);
        font-family: var(--font-nav);
        font-size: 14.5px;
        font-weight: 650;
        letter-spacing: -0.025em;
        cursor: pointer;
        overflow: hidden;
        border: 1px solid transparent;
        transition:
            background var(--rail-transition),
            color var(--rail-transition),
            border-color var(--rail-transition),
            box-shadow 0.28s var(--ease-out-expo),
            transform var(--rail-transition);
    }

    .rail-link:hover {
        background: var(--rail-link-hover);
        color: var(--sidebar-text-active);
        transform: translateX(2px);
    }

    /* ✅ CHANGED: Dark mode link hover — neutral glow */
    :global(.dark) .rail-link:hover {
        box-shadow:
            0 0 12px rgba(148, 163, 184, 0.03),
            inset 0 0 12px rgba(148, 163, 184, 0.02);
    }

    /* —— Nav Link — Active —— */
    .rail-link--active {
        background: var(--rail-link-active-bg) !important;
        color: var(--sidebar-text-active) !important;
        font-weight: 750;
        border-color: var(--rail-active-border, transparent);
        box-shadow: var(--rail-link-active-shadow);
        transform: none !important;
    }

    /* ✅ CHANGED: Dark mode active — neutral glow */
    :global(.dark) .rail-link--active {
        box-shadow:
            var(--rail-link-active-shadow),
            0 0 16px rgba(148, 163, 184, 0.04),
            inset 0 0 16px rgba(148, 163, 184, 0.03);
        border-color: rgba(148, 163, 184, 0.12);
    }

    .rail-link--active:hover {
        background: var(--rail-link-active-bg) !important;
    }

    /* —— Active Accent Bar —— */
    .rail-active-bar {
        position: absolute;
        left: 0;
        top: 50%;
        width: 3px;
        border-radius: 0 4px 4px 0;
        background: linear-gradient(to bottom, rgba(37, 99, 235, 0.74), rgba(37, 99, 235, 0.46));
        box-shadow:
            0 0 8px rgba(37, 99, 235, 0.18),
            0 0 16px rgba(37, 99, 235, 0.08);
        animation: rail-bar-enter 0.35s cubic-bezier(0.34, 1.56, 0.64, 1) forwards;
    }

    :global(.dark) .rail-active-bar {
        background: var(--accent);
        box-shadow:
            0 0 8px var(--accent-glow),
            0 0 20px var(--accent-glow);
    }

    @keyframes rail-bar-enter {
        from { height: 0; margin-top: 0; opacity: 0; }
        to   { height: 18px; margin-top: -9px; opacity: 1; }
    }

    /* —— Icon —— */
    .rail-link-icon {
        font-size: 20px;
        line-height: 1;
        flex-shrink: 0;
        width: 22px;
        text-align: center;
        color: var(--sidebar-text);
        transition: color var(--rail-transition), filter 0.2s ease;
    }

    .rail-link:hover .rail-link-icon {
        color: var(--sidebar-text-active);
    }

    /* ✅ CHANGED: Dark hover icon glow — neutral */
    :global(.dark) .rail-link:hover .rail-link-icon {
        filter: drop-shadow(0 0 4px rgba(148, 163, 184, 0.25));
    }

    .rail-link--active .rail-link-icon {
        color: var(--accent);
    }

    :root:not(.dark) .rail-link--active .rail-link-icon {
        filter: none;
    }

    :global(.dark) .rail-link--active .rail-link-icon {
        filter: drop-shadow(0 0 6px rgba(90, 159, 212, 0.4));
    }

    .rail-link-label {
        line-height: 1.08;
    }

    .rail-link--sm {
        padding: 9px 14px;
    }

    /* Mira as inline nav mark */
    .rail-copilot-icon-inline {
        width: 23px;
        flex: 0 0 23px;
    }

    .rail-link--copilot:hover .rail-copilot-icon-inline,
    .rail-link--copilot-active .rail-copilot-icon-inline {
        color: var(--sidebar-text-active);
    }

    .rail-link--copilot:hover {
        background: var(--rail-copilot-bg-hover) !important;
    }

    :global(.dark) .rail-link--copilot:hover {
        box-shadow:
            0 0 12px rgba(148, 163, 184, 0.03),
            inset 0 0 12px rgba(148, 163, 184, 0.02);
    }

    .rail-link--copilot-active {
        background: var(--rail-copilot-bg-active) !important;
        border-color: var(--rail-copilot-border-active) !important;
        box-shadow: var(--rail-copilot-glow-active) !important;
    }

    :global(.dark) .rail-link--copilot-active {
        box-shadow:
            var(--rail-copilot-glow-active),
            inset 0 0 16px rgba(148, 163, 184, 0.03) !important;
        border-color: rgba(148, 163, 184, 0.12) !important;
    }

    .rail-link--copilot-active .rail-copilot-icon-inline {
        filter: none;
    }

    /* ————————————————————————————————————————
       REMOVED: Old Copilot Portal card styles
       The following classes are no longer used:
       .rail-copilot-container
       .rail-copilot
       .rail-copilot--active
       .rail-copilot-border-gradient
       .rail-copilot-glow
       .rail-copilot-header
       .rail-copilot-icon
       .rail-copilot-title
       .rail-copilot-badge
       .rail-copilot-desc
       Keeping them commented out for rollback safety.
       ———————————————————————————————————————— */

    /* —— Footer (Status Bar) —— */
    .rail-footer {
        padding: 6px 10px 14px;
        display: flex;
        flex-direction: column;
        gap: 2px;
        position: relative;
        z-index: 1;
    }

    .rail-footer-row {
        display: flex;
        align-items: center;
        gap: 9px;
        padding: 7px 12px;
        border-radius: 9px;
        border: none;
        background: transparent;
        color: var(--sidebar-text);
        font-size: 11.5px;
        font-weight: 450;
        cursor: default;
        transition:
            background var(--rail-transition),
            color var(--rail-transition),
            box-shadow 0.22s ease;
    }

    .rail-footer-row--interactive {
        cursor: pointer;
    }

    .rail-footer-row--interactive:hover {
        background: var(--rail-link-hover);
        color: var(--sidebar-text-active);
    }

    /* ✅ CHANGED: Dark footer hover — neutral */
    :global(.dark) .rail-footer-row--interactive:hover {
        box-shadow: 0 0 8px rgba(148, 163, 184, 0.03);
    }

    .rail-footer-row--interactive:disabled {
        opacity: 0.5;
        cursor: not-allowed;
    }

    .rail-footer-label {
        flex: 1;
        text-align: left;
        line-height: 1.2;
    }

    .rail-footer-accent {
        color: var(--positive);
        font-weight: 550;
    }

    .rail-footer-icon {
        font-size: 16px;
        opacity: 0.6;
    }

    .rail-footer-action {
        font-size: 15px;
        opacity: 0.45;
        margin-left: auto;
    }

    /* —— Sync Dot —— */
    .rail-sync-dot {
        width: 6px;
        height: 6px;
        border-radius: 50%;
        background: var(--positive);
        box-shadow: 0 0 6px var(--positive);
        flex-shrink: 0;
        animation: syncPulse 3s ease-in-out infinite;
    }

    .rail-sync-dot--spinning {
        background: var(--warning);
        box-shadow: 0 0 6px var(--warning);
        animation: syncPulse 1s ease-in-out infinite;
    }

    @keyframes syncPulse {
        0%, 100% { opacity: 1; }
        50% { opacity: 0.35; }
    }

    /* —— Theme Toggle Track —— */
    .rail-toggle-track {
        position: relative;
        width: 32px;
        height: 17px;
        border-radius: 9px;
        background: var(--rail-border);
        flex-shrink: 0;
        margin-left: auto;
        transition: background 0.3s ease, box-shadow 0.3s ease;
    }

    .rail-toggle-track--on {
        background: var(--accent);
        box-shadow: 0 0 8px rgba(90, 159, 212, 0.3);
    }

    .rail-toggle-thumb {
        position: absolute;
        top: 2px;
        left: 2px;
        width: 13px;
        height: 13px;
        border-radius: 50%;
        background: #fff;
        box-shadow: 0 1px 3px rgba(0, 0, 0, 0.2);
        transition: transform 0.3s cubic-bezier(0.34, 1.56, 0.64, 1);
    }

    .rail-toggle-track--on .rail-toggle-thumb {
        transform: translateX(15px);
    }

    /* —— Mesh Canvas Layer —— */
    .mesh-canvas-layer {
        position: fixed;
        inset: 0;
        z-index: 0;
        pointer-events: none;
        background-image: var(--mesh-canvas, none);
        background-size: 100% 100%;
        transition: opacity 0.5s ease;
    }

    :global(.dark) .mesh-canvas-layer {
        opacity: 0;
    }

    :global(.theme-switching) .mesh-canvas-layer {
        opacity: 0;
        transition: none;
    }

    /* —— Spin —— */
    .animate-spin {
        animation: spin 1s linear infinite;
    }

    @keyframes spin {
        from { transform: rotate(0deg); }
        to { transform: rotate(360deg); }
    }

    /* ✅ CHANGED: Rail Right-Edge Glow — neutral in dark mode */
    .rail-glow-edge::after {
        content: '';
        position: absolute;
        top: 0;
        right: 0;
        width: 1px;
        height: 100%;
        background: rgba(0, 0, 0, 0.06);
        opacity: 1;
        box-shadow: none;
        pointer-events: none;
        z-index: 1;
        mask-image: none;
        -webkit-mask-image: none;
    }

    /* ✅ CHANGED: Dark mode edge glow — neutral silver instead of cyan */
    :global(.dark) .rail-glow-edge::after {
        opacity: 0.95;
        background: rgba(160, 160, 170, 0.10);
        box-shadow:
            0 0 8px rgba(210, 210, 220, 0.05),
            0 0 18px rgba(180, 180, 190, 0.03);
    }

</style>
