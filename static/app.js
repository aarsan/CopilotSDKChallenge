/**
 * InfraForge — Web UI Client
 *
 * Multi-page app with traditional navigation for browsing (services, templates)
 * and AI chat for complex design tasks (infrastructure generation).
 */

// ── State ───────────────────────────────────────────────────
let sessionToken = null;
let currentUser = null;
let ws = null;
let isStreaming = false;
let currentStreamDiv = null;
let currentStreamContent = '';
let mermaidCounter = 0;
let currentDesignMode = 'approved';  // 'approved' or 'ideal'
let currentPage = 'dashboard';

// Data
let allServices = [];
let allTemplates = [];
let currentCategoryFilter = 'all';
let currentStatusFilter = 'all';
let currentTemplateFilter = 'all';
let serviceSearchQuery = '';
let templateSearchQuery = '';

// ── Initialization ──────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
    mermaid.initialize({
        startOnLoad: false,
        theme: 'dark',
        themeVariables: {
            darkMode: true,
            background: '#0d1117',
            primaryColor: '#21262d',
            primaryTextColor: '#e6edf3',
            primaryBorderColor: '#30363d',
            lineColor: '#58a6ff',
            secondaryColor: '#161b22',
            tertiaryColor: '#1c2128',
        },
    });

    const urlParams = new URLSearchParams(window.location.search);
    const sessionFromUrl = urlParams.get('session');

    if (sessionFromUrl) {
        sessionToken = sessionFromUrl;
        window.history.replaceState({}, '', '/');
        validateSession(sessionToken);
    } else {
        const savedSession = localStorage.getItem('infraforge_session');
        if (savedSession) {
            validateSession(savedSession);
        }
    }
});

// ── Authentication ──────────────────────────────────────────

async function doLogin() {
    const btn = document.getElementById('btn-login');
    btn.disabled = true;
    btn.textContent = 'Signing in...';

    try {
        const res = await fetch('/api/auth/login');
        const data = await res.json();

        if (data.mode === 'demo') {
            sessionToken = data.sessionToken;
            currentUser = data.user;
            localStorage.setItem('infraforge_session', sessionToken);
            showApp();
            connectWebSocket();
        } else if (data.mode === 'entra') {
            localStorage.setItem('infraforge_flow_id', data.flowId);
            window.location.href = data.authUrl;
        }
    } catch (err) {
        showLoginError('Failed to connect to server. Is InfraForge running?');
    } finally {
        btn.disabled = false;
        btn.innerHTML = `
            <svg class="ms-icon" viewBox="0 0 21 21" xmlns="http://www.w3.org/2000/svg">
                <rect x="1" y="1" width="9" height="9" fill="#f25022"/>
                <rect x="11" y="1" width="9" height="9" fill="#7fba00"/>
                <rect x="1" y="11" width="9" height="9" fill="#00a4ef"/>
                <rect x="11" y="11" width="9" height="9" fill="#ffb900"/>
            </svg>
            Sign in with Microsoft`;
    }
}

async function validateSession(token) {
    try {
        const res = await fetch('/api/auth/me', {
            headers: { 'Authorization': `Bearer ${token}` },
        });

        if (res.ok) {
            sessionToken = token;
            currentUser = await res.json();
            localStorage.setItem('infraforge_session', sessionToken);
            showApp();
            connectWebSocket();
        } else {
            localStorage.removeItem('infraforge_session');
        }
    } catch {
        localStorage.removeItem('infraforge_session');
    }
}

async function doLogout() {
    try {
        await fetch('/api/auth/logout', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ sessionToken }),
        });
    } catch { /* ignore */ }

    if (ws) ws.close();
    sessionToken = null;
    currentUser = null;
    localStorage.removeItem('infraforge_session');

    document.getElementById('login-screen').classList.remove('hidden');
    document.getElementById('app-screen').classList.add('hidden');
}

function showLoginError(message) {
    const el = document.getElementById('login-error');
    el.textContent = message;
    el.classList.remove('hidden');
}

// ── App Display ─────────────────────────────────────────────

function showApp() {
    document.getElementById('login-screen').classList.add('hidden');
    document.getElementById('app-screen').classList.remove('hidden');

    if (currentUser) {
        const initials = currentUser.displayName
            .split(' ')
            .map(n => n[0])
            .join('')
            .substring(0, 2);

        document.getElementById('user-avatar').textContent = initials;
        document.getElementById('user-name').textContent = currentUser.displayName;
        document.getElementById('user-role').textContent = currentUser.jobTitle || 'Team Member';
        document.getElementById('user-dept').textContent =
            [currentUser.department, currentUser.costCenter].filter(Boolean).join(' · ');
        document.getElementById('user-context-hint').textContent =
            `Tagging as ${currentUser.email}`;
    }

    // Load all data, then show dashboard
    loadAllData();
    navigateTo('dashboard');

    // If an Azure sync is already running (e.g. page was refreshed), reconnect
    checkSyncStatus();
}

// ── Navigation ──────────────────────────────────────────────

function navigateTo(page) {
    // Hide all pages
    document.querySelectorAll('.page').forEach(p => {
        p.classList.add('hidden');
        p.classList.remove('active');
    });

    // Show target page
    const target = document.getElementById(`page-${page}`);
    if (target) {
        target.classList.remove('hidden');
        target.classList.add('active');
    }

    // Update nav active state
    document.querySelectorAll('.sidebar-nav .nav-btn[id]').forEach(btn => btn.classList.remove('active'));
    const navBtn = document.getElementById(`nav-${page}`);
    if (navBtn) navBtn.classList.add('active');

    // Update header
    const titles = {
        dashboard: ['Dashboard', 'Overview'],
        services: ['Service Catalog', `${allServices.length} services available`],
        templates: ['Template Catalog', `${allTemplates.length} templates available`],
        chat: ['Infrastructure Designer', 'Powered by GitHub Copilot SDK'],
    };
    const [title, subtitle] = titles[page] || ['InfraForge', ''];
    document.getElementById('page-title').textContent = title;
    document.getElementById('page-subtitle').textContent = subtitle;

    // Update page-specific action buttons in header
    updatePageActions(page);

    // Focus chat input when switching to chat
    if (page === 'chat') {
        setTimeout(() => {
            const input = document.getElementById('user-input');
            if (input) input.focus();
        }, 100);
    }

    currentPage = page;
}

function updatePageActions(page) {
    const actions = document.getElementById('page-actions');
    switch (page) {
        case 'services':
            actions.innerHTML = '<button class="btn btn-sm btn-secondary" onclick="syncAzureServices()" id="btn-sync-azure">🔄 Sync from Azure</button>';
            break;
        case 'templates':
            actions.innerHTML = '<button class="btn btn-sm btn-primary" onclick="openTemplateOnboarding()">＋ Onboard Template</button>';
            break;
        case 'chat':
            actions.innerHTML = '<button class="btn btn-sm btn-ghost" onclick="clearChat()" title="New conversation">🗒️ New Chat</button>';
            break;
        default:
            actions.innerHTML = '';
    }
}

function navigateToChat(prompt) {
    navigateTo('chat');

    // Hide welcome if present
    const welcome = document.getElementById('chat-welcome');
    if (welcome) welcome.classList.add('hidden');

    const input = document.getElementById('user-input');
    if (currentDesignMode === 'ideal') {
        input.value = `[Design Mode: Ideal Design] ${prompt}`;
    } else {
        input.value = prompt;
    }

    // Auto-send the prompt
    setTimeout(() => sendMessage(), 50);
}

// ── WebSocket Connection ────────────────────────────────────

function connectWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws/chat`;

    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
        ws.send(JSON.stringify({
            type: 'auth',
            sessionToken: sessionToken,
        }));
        updateConnectionStatus('connected');
    };

    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        handleWSMessage(data);
    };

    ws.onclose = () => {
        updateConnectionStatus('disconnected');
        setTimeout(() => {
            if (sessionToken) connectWebSocket();
        }, 3000);
    };

    ws.onerror = () => {
        updateConnectionStatus('disconnected');
    };
}

function handleWSMessage(data) {
    switch (data.type) {
        case 'auth_ok':
            console.log('WebSocket authenticated');
            break;
        case 'delta':
            handleStreamDelta(data.content);
            break;
        case 'done':
            handleStreamDone(data.content);
            break;
        case 'tool_call':
            handleToolCall(data.name, data.status);
            break;
        case 'error':
            handleError(data.message);
            break;
        case 'pong':
            break;
    }
}

function updateConnectionStatus(status) {
    const badge = document.getElementById('session-badge');
    const dot = badge.querySelector('.status-dot');

    if (status === 'connected') {
        dot.className = 'status-dot connected';
        badge.querySelector('span:last-child') || (badge.innerHTML =
            '<span class="status-dot connected"></span> Connected');
    } else {
        dot.className = 'status-dot disconnected';
    }
}

// ── Message Handling ────────────────────────────────────────

function sendMessage() {
    const input = document.getElementById('user-input');
    const text = input.value.trim();

    if (!text || isStreaming || !ws || ws.readyState !== WebSocket.OPEN) return;

    // Hide chat welcome on first message
    const welcome = document.getElementById('chat-welcome');
    if (welcome) welcome.classList.add('hidden');

    // Add user message to chat
    addMessage('user', text);

    // Send via WebSocket
    ws.send(JSON.stringify({
        type: 'message',
        content: text,
    }));

    // Clear input
    input.value = '';
    input.style.height = 'auto';
    isStreaming = true;
    document.getElementById('btn-send').disabled = true;

    // Create placeholder for assistant response
    currentStreamContent = '';
    currentStreamDiv = addMessage('assistant', '', true);
}

function sendQuickAction(prompt) {
    const input = document.getElementById('user-input');
    if (currentDesignMode === 'ideal') {
        input.value = `[Design Mode: Ideal Design] ${prompt}`;
    } else {
        input.value = prompt;
    }
    sendMessage();
}

function addMessage(role, content, isStreaming = false) {
    const container = document.getElementById('messages');

    const messageDiv = document.createElement('div');
    messageDiv.className = `message ${role}-message`;

    const avatar = document.createElement('div');
    avatar.className = 'message-avatar';

    if (role === 'user') {
        avatar.textContent = currentUser
            ? currentUser.displayName.split(' ').map(n => n[0]).join('').substring(0, 2)
            : '?';
    } else {
        avatar.textContent = '⚒️';
    }

    const contentDiv = document.createElement('div');
    contentDiv.className = 'message-content';

    const textDiv = document.createElement('div');
    textDiv.className = 'message-text';

    if (isStreaming) {
        textDiv.classList.add('streaming-cursor');
    } else {
        textDiv.innerHTML = renderMarkdown(content);
    }

    contentDiv.appendChild(textDiv);
    messageDiv.appendChild(avatar);
    messageDiv.appendChild(contentDiv);
    container.appendChild(messageDiv);

    scrollToBottom();
    return textDiv;
}

function handleStreamDelta(content) {
    if (!currentStreamDiv) return;

    currentStreamContent += content;
    currentStreamDiv.innerHTML = renderMarkdown(currentStreamContent);
    currentStreamDiv.classList.add('streaming-cursor');

    scrollToBottom();
}

function handleStreamDone(fullContent) {
    if (currentStreamDiv) {
        currentStreamDiv.classList.remove('streaming-cursor');
        const finalContent = fullContent || currentStreamContent;
        currentStreamDiv.innerHTML = renderMarkdown(finalContent);
        postProcessContent(currentStreamDiv);
    }

    currentStreamDiv = null;
    currentStreamContent = '';
    isStreaming = false;
    document.getElementById('btn-send').disabled = false;
    document.getElementById('user-input').focus();

    hideToolActivity();
    scrollToBottom();
}

function handleToolCall(toolName, status) {
    const friendlyNames = {
        'search_template_catalog': '🔍 Searching template catalog',
        'compose_from_catalog': '🧩 Composing from catalog templates',
        'register_template': '📝 Registering new template',
        'generate_bicep': '⚙️ Generating Bicep template',
        'generate_terraform': '⚙️ Generating Terraform config',
        'generate_github_actions_pipeline': '🔄 Generating GitHub Actions pipeline',
        'generate_azure_devops_pipeline': '🔄 Generating Azure DevOps pipeline',
        'generate_architecture_diagram': '📊 Creating architecture diagram',
        'generate_design_document': '📝 Producing design document',
        'estimate_azure_cost': '💰 Estimating Azure costs',
        'check_policy_compliance': '🛡️ Checking policy compliance',
        'save_output_to_file': '💾 Saving output to file',
    };

    if (status === 'running') {
        showToolActivity(friendlyNames[toolName] || `Running ${toolName}`);
    } else if (status === 'complete') {
        hideToolActivity();
    }
}

function handleError(message) {
    if (currentStreamDiv) {
        currentStreamDiv.classList.remove('streaming-cursor');
        currentStreamDiv.innerHTML = `<p style="color: var(--accent-red);">❌ ${escapeHtml(message)}</p>`;
    } else {
        addMessage('assistant', `❌ Error: ${message}`);
    }

    isStreaming = false;
    document.getElementById('btn-send').disabled = false;
    hideToolActivity();
}

// ── Tool Activity Indicator ─────────────────────────────────

function showToolActivity(text) {
    const el = document.getElementById('tool-activity');
    document.getElementById('tool-activity-text').textContent = text;
    el.classList.remove('hidden');
}

function hideToolActivity() {
    document.getElementById('tool-activity').classList.add('hidden');
}

// ── Markdown & Rendering ────────────────────────────────────

function renderMarkdown(text) {
    if (!text) return '';

    marked.setOptions({
        breaks: true,
        gfm: true,
        highlight: function (code, lang) {
            if (lang && hljs.getLanguage(lang)) {
                try {
                    return hljs.highlight(code, { language: lang }).value;
                } catch { }
            }
            return escapeHtml(code);
        },
    });

    const renderer = new marked.Renderer();

    renderer.code = function (codeObj) {
        const code = typeof codeObj === 'string' ? codeObj : (codeObj.text || '');
        const lang = (typeof codeObj === 'object' ? codeObj.lang : '') || '';

        if (lang === 'mermaid') {
            const id = `mermaid-${++mermaidCounter}`;
            return `<div class="mermaid-container" id="${id}">${escapeHtml(code)}</div>`;
        }

        let highlighted = code;
        if (lang && hljs.getLanguage(lang)) {
            try {
                highlighted = hljs.highlight(code, { language: lang }).value;
            } catch {
                highlighted = escapeHtml(code);
            }
        } else {
            highlighted = escapeHtml(code);
        }

        const langLabel = lang || 'code';
        return `<div class="code-block-wrapper">
            <div class="code-block-header">
                <span>${langLabel}</span>
                <button class="btn-copy" onclick="copyCode(this)">Copy</button>
            </div>
            <pre><code class="language-${lang}">${highlighted}</code></pre>
        </div>`;
    };

    try {
        return marked.parse(text, { renderer });
    } catch {
        return escapeHtml(text).replace(/\n/g, '<br>');
    }
}

function postProcessContent(element) {
    const mermaidDivs = element.querySelectorAll('.mermaid-container');
    mermaidDivs.forEach(async (div) => {
        try {
            const code = div.textContent;
            const { svg } = await mermaid.render(div.id + '-svg', code);
            div.innerHTML = svg;
        } catch (err) {
            console.warn('Mermaid render failed:', err);
            div.innerHTML = `<pre><code>${escapeHtml(div.textContent)}</code></pre>`;
        }
    });
}

// ── Data Loading ────────────────────────────────────────────

async function loadAllData() {
    try {
        const [svcRes, tmplRes, approvalRes] = await Promise.all([
            fetch('/api/catalog/services'),
            fetch('/api/catalog/templates'),
            fetch('/api/approvals'),
        ]);

        const svcData = await svcRes.json();
        const tmplData = await tmplRes.json();
        const approvalData = await approvalRes.json();

        allServices = svcData.services || [];
        allTemplates = tmplData.templates || [];

        // Update dashboard stats
        const stats = svcData.stats || {};
        document.getElementById('stat-approved').textContent = stats.approved || 0;
        document.getElementById('stat-conditional').textContent = stats.conditional || 0;
        document.getElementById('stat-review').textContent = stats.under_review || 0;
        document.getElementById('stat-templates').textContent = tmplData.total || 0;

        // Build service category filters
        const categories = svcData.categories || [];
        const filterContainer = document.getElementById('catalog-filters');
        filterContainer.innerHTML = `<button class="filter-pill active" onclick="filterServices('all')">All (${allServices.length})</button>`;
        categories.forEach(cat => {
            const count = allServices.filter(s => s.category === cat).length;
            filterContainer.innerHTML += `<button class="filter-pill" onclick="filterServices('${cat}')">${cat} (${count})</button>`;
        });

        // Build template format/category filters
        const templateFormats = [...new Set(allTemplates.map(t => t.format).filter(Boolean))].sort();
        const templateCategories = [...new Set(allTemplates.map(t => t.category).filter(Boolean))].sort();
        const tmplFilterContainer = document.getElementById('template-filters');
        if (tmplFilterContainer) {
            tmplFilterContainer.innerHTML = `<button class="filter-pill active" onclick="filterTemplates('all')">All (${allTemplates.length})</button>`;
            templateFormats.forEach(fmt => {
                const count = allTemplates.filter(t => t.format === fmt).length;
                tmplFilterContainer.innerHTML += `<button class="filter-pill" onclick="filterTemplates('${fmt}')">${fmt} (${count})</button>`;
            });
            templateCategories.forEach(cat => {
                if (!templateFormats.includes(cat)) {
                    const count = allTemplates.filter(t => t.category === cat).length;
                    tmplFilterContainer.innerHTML += `<button class="filter-pill" onclick="filterTemplates('${cat}')">${cat} (${count})</button>`;
                }
            });
        }

        // Render tables
        renderServiceTable(allServices);
        renderTemplateTable(allTemplates);

        // Render approval tracker
        renderApprovalTracker(approvalData.requests || []);

        // Update page subtitles if already on those pages
        if (currentPage === 'services') {
            document.getElementById('page-subtitle').textContent = `${allServices.length} services available`;
        } else if (currentPage === 'templates') {
            document.getElementById('page-subtitle').textContent = `${allTemplates.length} templates available`;
        }
    } catch (err) {
        console.warn('Failed to load data:', err);
    }
}

// ── Azure Service Sync (SSE streaming with live progress) ───

let _syncAbortController = null; // tracks the active SSE fetch

async function syncAzureServices() {
    const btn = document.getElementById('btn-sync-azure');
    if (btn) {
        btn.disabled = true;
        btn.textContent = '🔄 Syncing...';
    }
    _showSyncPanel();
    _connectSyncSSE();
}

/** Show (or re-show) the progress panel above the services table. */
function _showSyncPanel() {
    let panel = document.getElementById('sync-progress-panel');
    if (!panel) {
        panel = document.createElement('div');
        panel.id = 'sync-progress-panel';
        panel.className = 'sync-progress-panel';
        const tableContainer = document.querySelector('.catalog-table-container')
            || document.getElementById('catalog-tbody')?.closest('.catalog-table-container');
        if (tableContainer) {
            tableContainer.parentNode.insertBefore(panel, tableContainer);
        } else {
            document.getElementById('page-services')?.appendChild(panel);
        }
    }
    panel.classList.remove('hidden');
    panel.innerHTML = `
        <div class="sync-progress-header">
            <span class="sync-spinner"></span>
            <span id="sync-phase-text">Connecting to Azure…</span>
        </div>
        <div class="sync-progress-bar-track">
            <div class="sync-progress-bar-fill" id="sync-bar" style="width: 2%"></div>
        </div>
        <div class="sync-progress-detail" id="sync-detail">Initializing…</div>
    `;
}

/** Connect (or reconnect) to the SSE stream.  Safe to call multiple times. */
async function _connectSyncSSE() {
    // Abort any previous SSE connection
    if (_syncAbortController) {
        _syncAbortController.abort();
    }
    _syncAbortController = new AbortController();

    try {
        const response = await fetch('/api/catalog/services/sync', {
            signal: _syncAbortController.signal,
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let lastResult = null;
        let lastTableRefresh = 0;

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop();

            for (const line of lines) {
                if (!line.startsWith('data: ')) continue;
                try {
                    const data = JSON.parse(line.slice(6));
                    lastResult = data;
                    updateSyncProgress(data);

                    // Refresh the services table every 2 seconds during inserts
                    // so users see rows appearing in real time.
                    const now = Date.now();
                    if (data.phase === 'inserting' && now - lastTableRefresh > 2000) {
                        lastTableRefresh = now;
                        _refreshServicesOnly();
                    }
                } catch { /* skip malformed */ }
            }
        }

        // Final toast
        if (lastResult?.phase === 'done') {
            const r = lastResult;
            const msg = r.new_services_added > 0
                ? `✅ Synced! ${r.new_services_added} new services discovered (${r.total_in_catalog} total)`
                : `✅ Already up to date — ${r.total_in_catalog} services in catalog`;
            showToast(msg);
        } else if (lastResult?.phase === 'error') {
            showToast(lastResult.detail, 'error');
        }

        // Final full refresh to get accurate counts/filters
        await loadAllData();
        _syncDone();
    } catch (err) {
        if (err.name === 'AbortError') return; // intentional disconnect
        showToast(`Sync failed: ${err.message}`, 'error');
        updateSyncProgress({ phase: 'error', detail: err.message, progress: 0 });
        _syncDone();
    }
}

/** Lightweight refresh of just the services table (no templates/approvals). */
async function _refreshServicesOnly() {
    try {
        const res = await fetch('/api/catalog/services');
        const data = await res.json();
        allServices = data.services || [];
        renderServiceTable(allServices);
        // Update the subtitle count
        const subtitle = document.getElementById('page-subtitle');
        if (subtitle && currentPage === 'services') {
            subtitle.textContent = `${allServices.length} services available`;
        }
    } catch { /* swallow — best-effort */ }
}

/** Clean up after sync finishes (success or error). */
function _syncDone() {
    _syncAbortController = null;
    const btn = document.getElementById('btn-sync-azure');
    if (btn) {
        btn.disabled = false;
        btn.textContent = '🔄 Sync from Azure';
    }
    setTimeout(() => {
        const p = document.getElementById('sync-progress-panel');
        if (p) p.classList.add('hidden');
    }, 3000);
}

/**
 * Check if a sync is already running (e.g. after a page refresh).
 * If so, reconnect the progress panel automatically.
 */
async function checkSyncStatus() {
    try {
        const res = await fetch('/api/catalog/services/sync/status');
        const status = await res.json();
        if (status.running) {
            const btn = document.getElementById('btn-sync-azure');
            if (btn) {
                btn.disabled = true;
                btn.textContent = '🔄 Syncing...';
            }
            _showSyncPanel();
            // Update panel with latest progress from the server
            if (status.progress) updateSyncProgress(status.progress);
            // Reconnect to the SSE stream to follow along
            _connectSyncSSE();
        }
    } catch { /* server might not be up yet */ }
}

function updateSyncProgress(data) {
    const phaseText = document.getElementById('sync-phase-text');
    const bar = document.getElementById('sync-bar');
    const detail = document.getElementById('sync-detail');
    if (!phaseText) return;

    const phaseLabels = {
        connecting: '🔐 Authenticating',
        scanning:   '📡 Scanning Azure',
        filtering:  '🔍 Filtering resources',
        inserting:  '💾 Saving to catalog',
        done:       '✅ Complete',
        error:      '❌ Error',
    };

    phaseText.textContent = phaseLabels[data.phase] || data.phase;
    detail.textContent = data.detail || '';
    if (bar && typeof data.progress === 'number') {
        bar.style.width = `${Math.round(data.progress * 100)}%`;
    }
    if (data.phase === 'done') {
        bar?.classList.add('sync-bar-done');
    } else if (data.phase === 'error') {
        bar?.classList.add('sync-bar-error');
    }
}

// ── Service Catalog ─────────────────────────────────────────

const statusLabels = {
    approved: '✅ Approved',
    conditional: '⚠️ Conditional',
    under_review: '🔄 Under Review',
    not_approved: '❌ Not Approved',
};

function renderServiceTable(services) {
    const tbody = document.getElementById('catalog-tbody');

    // Update results summary
    const summary = document.getElementById('service-results-summary');
    if (summary) {
        summary.textContent = `Showing ${services.length} of ${allServices.length} services`;
    }

    if (!services.length) {
        tbody.innerHTML = '<tr><td colspan="5" class="catalog-loading">No services match your filters</td></tr>';
        return;
    }

    tbody.innerHTML = services.map(svc => {
        const status = svc.status || 'not_approved';
        const policyCount = (svc.policies || []).length;
        const conditionCount = (svc.conditions || []).length;

        // Policy summary for the table cell
        let policySummary = '';
        if (policyCount > 0) {
            const firstPolicy = svc.policies[0].length > 30
                ? svc.policies[0].substring(0, 30) + '…' : svc.policies[0];
            policySummary = `<span class="policy-tag">${escapeHtml(firstPolicy)}</span>`;
            if (policyCount > 1) policySummary += `<span class="policy-tag">+${policyCount - 1} more</span>`;
        } else {
            policySummary = '<span class="policy-tag none">No policies</span>';
        }

        // Determine the inline action button
        const actionBtn = status === 'not_approved'
            ? `<button class="btn btn-xs btn-accent" onclick="event.stopPropagation(); openGovernanceEditor('${escapeHtml(svc.id)}')">\u2705 Approve</button>`
            : `<button class="btn btn-xs btn-ghost" onclick="event.stopPropagation(); openGovernanceEditor('${escapeHtml(svc.id)}')">\u270f\ufe0f Edit</button>`;

        return `<tr onclick="showServiceDetail('${escapeHtml(svc.id)}')">
            <td>
                <div class="svc-name">${escapeHtml(svc.name)}</div>
                <div class="svc-id">${escapeHtml(svc.id)}</div>
            </td>
            <td><span class="category-badge">${escapeHtml(svc.category)}</span></td>
            <td><span class="status-badge ${status}">${statusLabels[status] || status}</span></td>
            <td>${policySummary}</td>
            <td class="action-cell">${actionBtn}</td>
        </tr>`;
    }).join('');
}

function filterServices(category) {
    currentCategoryFilter = category;

    // Update active pill
    const container = document.getElementById('catalog-filters');
    if (container) {
        container.querySelectorAll('.filter-pill').forEach(pill => pill.classList.remove('active'));
        event.target.classList.add('active');
    }

    applyServiceFilters();
}

function filterServicesByStatus(status) {
    currentStatusFilter = status;

    // Update active pill
    const container = document.getElementById('status-filters');
    if (container) {
        container.querySelectorAll('.filter-pill').forEach(pill => pill.classList.remove('active'));
        event.target.classList.add('active');
    }

    applyServiceFilters();
}

function searchServices(query) {
    serviceSearchQuery = query.toLowerCase().trim();
    applyServiceFilters();
}

function applyServiceFilters() {
    let filtered = allServices;

    // Category filter
    if (currentCategoryFilter !== 'all') {
        filtered = filtered.filter(s => s.category === currentCategoryFilter);
    }

    // Status filter
    if (currentStatusFilter !== 'all') {
        filtered = filtered.filter(s => s.status === currentStatusFilter);
    }

    // Search filter
    if (serviceSearchQuery) {
        filtered = filtered.filter(s =>
            (s.name || '').toLowerCase().includes(serviceSearchQuery) ||
            (s.id || '').toLowerCase().includes(serviceSearchQuery) ||
            (s.category || '').toLowerCase().includes(serviceSearchQuery)
        );
    }

    renderServiceTable(filtered);
}

// ── Service Detail Drawer ───────────────────────────────────

function showServiceDetail(serviceId) {
    const svc = allServices.find(s => s.id === serviceId);
    if (!svc) return;

    const status = svc.status || 'not_approved';

    document.getElementById('detail-service-name').textContent = svc.name;
    document.getElementById('detail-service-body').innerHTML = `
        <div class="detail-meta">
            <span class="svc-id">${escapeHtml(svc.id)}</span>
            <span class="status-badge ${status}">${statusLabels[status] || status}</span>
            <span class="category-badge">${escapeHtml(svc.category)}</span>
        </div>

        ${(svc.policies && svc.policies.length) ? `
        <div class="detail-section">
            <h4>Policies</h4>
            <ul class="detail-list">${svc.policies.map(p => `<li>${escapeHtml(p)}</li>`).join('')}</ul>
        </div>` : `
        <div class="detail-section">
            <h4>Policies</h4>
            <p class="detail-empty">No policies defined yet</p>
        </div>`}

        ${(svc.conditions && svc.conditions.length) ? `
        <div class="detail-section">
            <h4>Conditions</h4>
            <ul class="detail-list">${svc.conditions.map(c => `<li>${escapeHtml(c)}</li>`).join('')}</ul>
        </div>` : ''}

        ${svc.documentation ? `
        <div class="detail-section">
            <h4>Documentation</h4>
            <p><a href="${escapeHtml(svc.documentation)}" target="_blank" rel="noopener">${escapeHtml(svc.documentation)}</a></p>
        </div>` : ''}

        ${svc.review_notes ? `
        <div class="detail-section">
            <h4>Review Notes</h4>
            <p class="detail-notes">${escapeHtml(svc.review_notes)}</p>
        </div>` : ''}

        <div class="detail-actions">
            <button class="btn btn-sm btn-accent" onclick="closeServiceDetail(); openGovernanceEditor('${escapeHtml(svc.id)}')">
                ${status === 'not_approved' ? '✅ Approve This Service' : '✏️ Edit Policies'}
            </button>
            <button class="btn btn-sm btn-primary" onclick="navigateToChat('Check the approval status for ${escapeHtml(svc.name)} and tell me the policies and restrictions')">
                💬 Ask the Designer about this service
            </button>
        </div>
    `;

    document.getElementById('service-detail-drawer').classList.remove('hidden');
}

function closeServiceDetail() {
    document.getElementById('service-detail-drawer').classList.add('hidden');
}

// ── Template Catalog ────────────────────────────────────────

function renderTemplateTable(templates) {
    const tbody = document.getElementById('template-tbody');
    if (!tbody) return;

    // Update results summary
    const summary = document.getElementById('template-results-summary');
    if (summary) {
        summary.textContent = `Showing ${templates.length} of ${allTemplates.length} templates`;
    }

    if (!templates.length) {
        tbody.innerHTML = '<tr><td colspan="5" class="catalog-loading">No templates match your filters</td></tr>';
        return;
    }

    const tmplStatusLabels = {
        approved: '✅ Approved',
        draft: '📝 Draft',
        deprecated: '⚠️ Deprecated',
    };

    const formatIcons = {
        bicep: '🔷',
        terraform: '🟣',
        'github-actions': '🔄',
        'azure-devops': '🔵',
    };

    tbody.innerHTML = templates.map(tmpl => {
        const status = tmpl.status || 'approved';
        const fmt = tmpl.format || 'bicep';
        const tags = (tmpl.tags || []).slice(0, 4);
        const isBlueprint = tmpl.is_blueprint || tmpl.category === 'blueprint';

        return `<tr onclick="showTemplateDetail('${escapeHtml(tmpl.id)}')">
            <td>
                <div class="svc-name">${isBlueprint ? '🏗️ ' : ''}${escapeHtml(tmpl.name)}</div>
                <div class="svc-id">${escapeHtml(tmpl.id)}</div>
            </td>
            <td><span class="category-badge">${formatIcons[fmt] || '📄'} ${escapeHtml(fmt)}</span></td>
            <td><span class="category-badge">${escapeHtml(tmpl.category || '')}</span></td>
            <td>
                <div class="region-tags">
                    ${tags.map(t => `<span class="region-tag">${escapeHtml(t)}</span>`).join('')}
                </div>
            </td>
            <td><span class="status-badge ${status}">${tmplStatusLabels[status] || status}</span></td>
        </tr>`;
    }).join('');
}

function filterTemplates(filter) {
    currentTemplateFilter = filter;

    const container = document.getElementById('template-filters');
    if (container) {
        container.querySelectorAll('.filter-pill').forEach(pill => pill.classList.remove('active'));
        event.target.classList.add('active');
    }

    applyTemplateFilters();
}

function searchTemplates(query) {
    templateSearchQuery = query.toLowerCase().trim();
    applyTemplateFilters();
}

function applyTemplateFilters() {
    let filtered = allTemplates;

    // Format/category filter
    if (currentTemplateFilter !== 'all') {
        filtered = filtered.filter(t => t.format === currentTemplateFilter || t.category === currentTemplateFilter);
    }

    // Search filter
    if (templateSearchQuery) {
        filtered = filtered.filter(t =>
            (t.name || '').toLowerCase().includes(templateSearchQuery) ||
            (t.id || '').toLowerCase().includes(templateSearchQuery) ||
            (t.description || '').toLowerCase().includes(templateSearchQuery) ||
            (t.tags || []).some(tag => tag.toLowerCase().includes(templateSearchQuery)) ||
            (t.resources || []).some(r => r.toLowerCase().includes(templateSearchQuery))
        );
    }

    renderTemplateTable(filtered);
}

// ── Template Detail Drawer ──────────────────────────────────

function showTemplateDetail(templateId) {
    const tmpl = allTemplates.find(t => t.id === templateId);
    if (!tmpl) return;

    const status = tmpl.status || 'approved';
    const isBlueprint = tmpl.is_blueprint || tmpl.category === 'blueprint';

    document.getElementById('detail-template-name').textContent = tmpl.name;
    document.getElementById('detail-template-body').innerHTML = `
        <div class="detail-meta">
            <span class="svc-id">${escapeHtml(tmpl.id)}</span>
            <span class="status-badge ${status}">${statusLabels[status] || status}</span>
            ${isBlueprint ? '<span class="category-badge">🏗️ Blueprint</span>' : ''}
        </div>

        <div class="detail-section">
            <h4>Description</h4>
            <p>${escapeHtml(tmpl.description || 'No description')}</p>
        </div>

        <div class="detail-section">
            <h4>Format</h4>
            <span class="category-badge">${escapeHtml(tmpl.format || 'bicep')}</span>
        </div>

        <div class="detail-section">
            <h4>Category</h4>
            <span class="category-badge">${escapeHtml(tmpl.category || '')}</span>
        </div>

        ${(tmpl.tags && tmpl.tags.length) ? `
        <div class="detail-section">
            <h4>Tags</h4>
            <div class="detail-tags">${tmpl.tags.map(t => `<span class="region-tag">${escapeHtml(t)}</span>`).join('')}</div>
        </div>` : ''}

        ${(tmpl.resources && tmpl.resources.length) ? `
        <div class="detail-section">
            <h4>Resource Types</h4>
            <div class="detail-tags">${tmpl.resources.map(r => `<span class="region-tag">${escapeHtml(r)}</span>`).join('')}</div>
        </div>` : ''}

        ${(tmpl.parameters && tmpl.parameters.length) ? `
        <div class="detail-section">
            <h4>Parameters</h4>
            <div class="detail-params">
                ${tmpl.parameters.map(p => `
                    <div class="detail-param">
                        <span class="param-name">${escapeHtml(p.name || p)}</span>
                        ${p.type ? `<span class="param-type">${escapeHtml(p.type)}</span>` : ''}
                        ${p.required ? '<span class="param-required">required</span>' : ''}
                    </div>
                `).join('')}
            </div>
        </div>` : ''}

        ${(tmpl.outputs && tmpl.outputs.length) ? `
        <div class="detail-section">
            <h4>Outputs</h4>
            <div class="detail-tags">${tmpl.outputs.map(o => `<span class="region-tag">${escapeHtml(o)}</span>`).join('')}</div>
        </div>` : ''}

        ${tmpl.content ? `
        <div class="detail-section">
            <h4>Template Code</h4>
            <div class="detail-code-wrap">
                <pre><code>${escapeHtml(tmpl.content)}</code></pre>
            </div>
        </div>` : ''}

        <div class="detail-actions">
            <button class="btn btn-sm btn-primary" onclick="navigateToChat('Use the template \\'${escapeHtml(tmpl.name)}\\' to generate infrastructure for my project')">
                💬 Use this template in Designer
            </button>
        </div>
    `;

    document.getElementById('template-detail-drawer').classList.remove('hidden');
}

function closeTemplateDetail() {
    document.getElementById('template-detail-drawer').classList.add('hidden');
}

// ── Design Mode Toggle ──────────────────────────────────────

function setDesignMode(mode) {
    currentDesignMode = mode;

    document.getElementById('mode-approved').classList.toggle('active', mode === 'approved');
    document.getElementById('mode-ideal').classList.toggle('active', mode === 'ideal');

    const infoText = document.querySelector('.mode-info-text');
    if (mode === 'approved') {
        infoText.textContent = 'Approved Only mode: All generated infrastructure uses services vetted by the platform team. Ready to deploy.';
    } else {
        infoText.textContent = 'Ideal Design mode: InfraForge will generate the best-practice architecture. Non-approved services will be flagged, and I\'ll guide you through submitting approval requests to IT.';
    }

    const input = document.getElementById('user-input');
    if (mode === 'approved') {
        input.placeholder = 'Describe the infrastructure you need (using approved services only)...';
    } else {
        input.placeholder = 'Describe your ideal infrastructure (I\'ll handle approval requests for non-approved services)...';
    }
}

// ── Approval Request Tracker ────────────────────────────────

function renderApprovalTracker(requests) {
    const tracker = document.getElementById('approval-tracker');
    if (!tracker) return;

    if (!requests.length) {
        tracker.innerHTML = `
            <div class="approval-empty">
                <span class="approval-empty-icon">📋</span>
                <p>No approval requests yet. When you use <strong>Ideal Design</strong> mode, non-approved services will be submitted here for IT review.</p>
            </div>`;
        return;
    }

    const statusIcons = {
        submitted: '📨', in_review: '🔍', approved: '✅',
        conditional: '⚠️', denied: '❌', deferred: '⏳',
    };

    tracker.innerHTML = `
        <div class="approval-list">
            ${requests.map(req => {
                const status = req.status || 'submitted';
                const icon = statusIcons[status] || '❓';
                const svcName = req.service_name || 'Unknown Service';
                const submitted = (req.submitted_at || '').substring(0, 10);
                const reqId = req.id || '';
                return `
                    <div class="approval-item" onclick="navigateToChat('Check the status of approval request ${reqId}')">
                        <span class="approval-status-icon">${icon}</span>
                        <div class="approval-details">
                            <div class="approval-service-name">${escapeHtml(svcName)}</div>
                            <div class="approval-meta">${reqId} · Submitted ${submitted}</div>
                        </div>
                        <span class="approval-status-badge ${status}">${status.replace('_', ' ')}</span>
                    </div>`;
            }).join('')}
        </div>`;
}

// ── Utility Functions ───────────────────────────────────────

function copyCode(button) {
    const pre = button.closest('.code-block-wrapper').querySelector('pre code');
    const text = pre.textContent;

    navigator.clipboard.writeText(text).then(() => {
        button.textContent = 'Copied!';
        setTimeout(() => { button.textContent = 'Copy'; }, 2000);
    });
}

function clearChat() {
    const container = document.getElementById('messages');
    // Remove all chat messages
    const messages = container.querySelectorAll('.message');
    messages.forEach(msg => msg.remove());

    // Show chat welcome again
    const welcome = document.getElementById('chat-welcome');
    if (welcome) welcome.classList.remove('hidden');
}

function handleKeyDown(event) {
    if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        sendMessage();
    }
}

function autoResizeTextarea(textarea) {
    textarea.style.height = 'auto';
    textarea.style.height = Math.min(textarea.scrollHeight, 200) + 'px';
}

function scrollToBottom() {
    const container = document.getElementById('messages');
    container.scrollTop = container.scrollHeight;
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// ── Onboarding: Modals ──────────────────────────────────────

function openGovernanceEditor(serviceId) {
    const svc = allServices.find(s => s.id === serviceId);
    if (!svc) { showToast('Service not found', 'error'); return; }

    const form = document.getElementById('form-service-onboard');
    const status = svc.status || 'not_approved';
    const isOnboarding = status === 'not_approved';

    // Set modal title and action
    document.getElementById('governance-modal-title').textContent =
        isOnboarding ? 'Approve Service' : 'Edit Policies';

    // Fill the service identity header
    document.getElementById('governance-svc-name').textContent = svc.name;
    document.getElementById('governance-svc-id').textContent = svc.id;
    document.getElementById('governance-svc-category').textContent = svc.category;

    // Show current status as a read-only badge
    const statusEl = document.getElementById('governance-svc-status');
    statusEl.textContent = (statusLabels[status] || status);
    statusEl.className = `status-badge ${status}`;

    // Pre-fill hidden fields
    form.querySelector('input[name="id"]').value = svc.id;
    form.querySelector('input[name="_action"]').value = isOnboarding ? 'approve' : 'update';

    // Pre-fill policy fields
    form.querySelector('input[name="documentation"]').value = svc.documentation || '';
    form.querySelector('textarea[name="review_notes"]').value = svc.review_notes || '';
    form.querySelector('textarea[name="policies"]').value = (svc.policies || []).join('\n');
    form.querySelector('textarea[name="conditions"]').value = (svc.conditions || []).join('\n');

    // Update submit button
    const btn = document.getElementById('btn-submit-service');
    btn.textContent = isOnboarding ? '✅ Approve Service' : '💾 Save Policies';
    btn.className = isOnboarding ? 'btn btn-accent' : 'btn btn-primary';

    document.getElementById('modal-service-onboard').classList.remove('hidden');
}

function openTemplateOnboarding() {
    document.getElementById('modal-template-onboard').classList.remove('hidden');
    const cb = document.querySelector('#form-template-onboard input[name="is_blueprint"]');
    const group = document.getElementById('blueprint-services-group');
    if (cb && group) {
        cb.addEventListener('change', () => {
            group.style.display = cb.checked ? 'block' : 'none';
        });
    }
}

function closeModal(id) {
    document.getElementById(id).classList.add('hidden');
}

function closeModalOnOverlay(event, id) {
    if (event.target === event.currentTarget) {
        closeModal(id);
    }
}

function showToast(message, type = 'success') {
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.textContent = message;
    document.body.appendChild(toast);
    setTimeout(() => toast.remove(), 3000);
}

async function submitGovernanceUpdate(event) {
    event.preventDefault();
    const form = document.getElementById('form-service-onboard');
    const fd = new FormData(form);
    const btn = document.getElementById('btn-submit-service');
    const origText = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Saving...';

    const serviceId = fd.get('id').trim();
    const action = fd.get('_action'); // 'approve' or 'update'

    const body = {
        policies: (fd.get('policies') || '').split('\n').map(s => s.trim()).filter(Boolean),
        conditions: (fd.get('conditions') || '').split('\n').map(s => s.trim()).filter(Boolean),
        review_notes: fd.get('review_notes') || '',
        documentation: fd.get('documentation') || '',
    };

    // When approving, determine status from whether conditions exist
    if (action === 'approve') {
        body.status = body.conditions.length > 0 ? 'conditional' : 'approved';
    }

    try {
        const res = await fetch(`/api/catalog/services/${encodeURIComponent(serviceId)}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });

        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Failed to update service');
        }

        const svc = allServices.find(s => s.id === serviceId);
        const name = svc ? svc.name : serviceId;
        const toast = action === 'approve'
            ? `✅ "${name}" approved${body.conditions.length ? ' (conditional)' : ''}!`
            : `Policies updated for "${name}"`;
        showToast(toast);
        closeModal('modal-service-onboard');
        await loadAllData();
    } catch (err) {
        showToast(err.message, 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = origText;
    }
}

async function submitTemplateOnboarding(event) {
    event.preventDefault();
    const form = document.getElementById('form-template-onboard');
    const fd = new FormData(form);
    const btn = document.getElementById('btn-submit-template');
    btn.disabled = true;
    btn.textContent = 'Submitting...';

    let parameters = [];
    try {
        const paramRaw = fd.get('parameters');
        if (paramRaw && paramRaw.trim()) {
            parameters = JSON.parse(paramRaw);
        }
    } catch {
        showToast('Parameters must be valid JSON', 'error');
        btn.disabled = false;
        btn.textContent = 'Onboard Template';
        return;
    }

    const body = {
        id: fd.get('id').trim(),
        name: fd.get('name').trim(),
        description: fd.get('description').trim(),
        format: fd.get('format'),
        category: fd.get('category'),
        content: fd.get('content') || '',
        tags: (fd.get('tags') || '').split(',').map(s => s.trim()).filter(Boolean),
        resources: (fd.get('resources') || '').split(',').map(s => s.trim()).filter(Boolean),
        parameters: parameters,
        outputs: (fd.get('outputs') || '').split(',').map(s => s.trim()).filter(Boolean),
        is_blueprint: fd.get('is_blueprint') === 'on',
        service_ids: (fd.get('service_ids') || '').split(',').map(s => s.trim()).filter(Boolean),
        status: 'approved',
        registered_by: 'web-portal',
    };

    try {
        const res = await fetch('/api/catalog/templates', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });

        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Failed to onboard template');
        }

        showToast(`Template "${body.name}" onboarded successfully!`);
        closeModal('modal-template-onboard');
        form.reset();
        await loadAllData();
    } catch (err) {
        showToast(err.message, 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = 'Onboard Template';
    }
}
