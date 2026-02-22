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
let currentTemplateTypeFilter = 'all';
let serviceSearchQuery = '';
let templateSearchQuery = '';

// Governance Standards
let allStandards = [];
let standardsSearchQuery = '';
let currentStandardsCategoryFilter = 'all';
let currentStandardsSeverityFilter = 'all';

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
        governance: ['Governance Standards', `${allStandards.length} organization standards`],
        activity: ['Observability', 'Deployments & service validation'],
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

    // Load observability data when switching to activity page
    if (page === 'activity') {
        loadDeploymentHistory();
        loadActivity(true);
        _startActivityPolling();
    } else {
        _stopActivityPolling();
    }

    // Load standards when switching to governance page
    if (page === 'governance') {
        loadStandards();
    }

    currentPage = page;
}

function updatePageActions(page) {
    const actions = document.getElementById('page-actions');
    switch (page) {
        case 'services':
            actions.innerHTML = '';  // Sync is now in the stats panel
            break;
        case 'templates':
            actions.innerHTML = '<button class="btn btn-sm btn-primary" onclick="openTemplateOnboarding()">＋ Onboard Template</button>';
            break;
        case 'governance':
            actions.innerHTML = '<button class="btn btn-sm btn-primary" onclick="openAddStandardModal()">＋ Add Standard</button> <button class="btn btn-sm btn-secondary" onclick="openImportStandardsModal()">📥 Import Standards</button>';
            break;
        case 'activity':
            actions.innerHTML = '<button class="btn btn-sm btn-ghost" onclick="loadDeploymentHistory(); loadActivity(true)" title="Refresh">⟳ Refresh</button>';
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

        // Count validating + validation_failed services
        const validatingCount = allServices.filter(s => s.status === 'validating' || s.status === 'validation_failed').length;
        const statValidating = document.getElementById('stat-validating');
        if (statValidating) statValidating.textContent = validatingCount;

        // Load service stats panel (Total Azure / Cached / Approved / Sync)
        loadServiceStats();

        // Load activity badge (non-blocking)
        loadActivity(true);

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

/** Load and render the service stats panel (Total Azure / Cached / Approved / Sync Status). */
async function loadServiceStats() {
    try {
        const res = await fetch('/api/catalog/services/sync/stats');
        const data = await res.json();
        _renderStatsPanel(data);
    } catch (err) {
        console.warn('Failed to load service stats:', err);
    }
}

function _renderStatsPanel(data) {
    // Total Azure resource types (from last sync)
    const azureEl = document.getElementById('svc-stat-azure');
    if (azureEl) {
        azureEl.textContent = data.total_azure != null ? data.total_azure.toLocaleString() : '—';
    }

    // Total cached in our system
    const cachedEl = document.getElementById('svc-stat-cached');
    if (cachedEl) {
        cachedEl.textContent = data.total_cached != null ? data.total_cached.toLocaleString() : '—';
    }

    // Total approved
    const approvedEl = document.getElementById('svc-stat-approved');
    if (approvedEl) {
        approvedEl.textContent = data.total_approved != null ? data.total_approved.toLocaleString() : '—';
    }

    // Sync status
    const statusEl = document.getElementById('svc-sync-status');
    const detailEl = document.getElementById('svc-sync-detail');
    const iconEl = document.getElementById('svc-sync-icon');

    if (statusEl) {
        if (data.sync_running) {
            statusEl.textContent = 'Syncing…';
            statusEl.className = 'svc-stat-status syncing';
            if (iconEl) iconEl.textContent = '🔄';
            if (detailEl) detailEl.textContent = 'In progress';
        } else if (data.last_synced_at) {
            statusEl.textContent = 'Synced';
            statusEl.className = 'svc-stat-status synced';
            if (iconEl) iconEl.textContent = '✅';
            if (detailEl) detailEl.textContent = _formatAgo(data.last_synced_ago_sec);
        } else {
            statusEl.textContent = 'Never synced';
            statusEl.className = 'svc-stat-status never';
            if (iconEl) iconEl.textContent = '⏳';
            if (detailEl) detailEl.textContent = 'Click Sync to pull from Azure';
        }
    }
}

/** Format seconds-ago into a human-readable string. */
function _formatAgo(sec) {
    if (sec == null) return '';
    if (sec < 60) return 'Just now';
    if (sec < 3600) return `${Math.round(sec / 60)} min ago`;
    if (sec < 86400) return `${Math.round(sec / 3600)} hr ago`;
    return `${Math.round(sec / 86400)} day(s) ago`;
}

async function syncAzureServices() {
    const btn = document.getElementById('btn-sync-panel');
    if (btn) {
        btn.disabled = true;
        btn.classList.add('syncing');
        btn.innerHTML = '<span class="sync-btn-icon">⟳</span> Syncing…';
    }

    // Update stats panel to show "Syncing…"
    const statusEl = document.getElementById('svc-sync-status');
    const detailEl = document.getElementById('svc-sync-detail');
    const iconEl = document.getElementById('svc-sync-icon');
    if (statusEl) { statusEl.textContent = 'Syncing…'; statusEl.className = 'svc-stat-status syncing'; }
    if (iconEl) iconEl.textContent = '🔄';
    if (detailEl) detailEl.textContent = 'Connecting to Azure…';

    _showSyncPanel();
    _connectSyncSSE();
}

/** Show (or re-show) the progress panel below the stats panel. */
function _showSyncPanel() {
    let panel = document.getElementById('sync-progress-panel');
    if (!panel) {
        panel = document.createElement('div');
        panel.id = 'sync-progress-panel';
        panel.className = 'sync-progress-panel';
        const statsPanel = document.getElementById('service-stats-panel');
        if (statsPanel) {
            statsPanel.parentNode.insertBefore(panel, statsPanel.nextSibling);
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
    const btn = document.getElementById('btn-sync-panel');
    if (btn) {
        btn.disabled = false;
        btn.classList.remove('syncing');
        btn.innerHTML = '<span class="sync-btn-icon">⟳</span> Sync';
    }
    // Refresh the stats panel with updated numbers
    loadServiceStats();
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
            const btn = document.getElementById('btn-sync-panel');
            if (btn) {
                btn.disabled = true;
                btn.classList.add('syncing');
                btn.innerHTML = '<span class="sync-btn-icon">⟳</span> Syncing…';
            }
            // Update stats panel
            const statusEl = document.getElementById('svc-sync-status');
            if (statusEl) { statusEl.textContent = 'Syncing…'; statusEl.className = 'svc-stat-status syncing'; }
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
    validating: '🔄 Validating…',
    validation_failed: '⛔ Validation Failed',
};

function renderServiceTable(services) {
    const tbody = document.getElementById('catalog-tbody');

    // Update results summary
    const summary = document.getElementById('service-results-summary');
    if (summary) {
        summary.textContent = `Showing ${services.length} of ${allServices.length} services`;
    }

    if (!services.length) {
        tbody.innerHTML = '<tr><td colspan="4" class="catalog-loading">No services match your filters</td></tr>';
        return;
    }

    tbody.innerHTML = services.map(svc => {
        const status = svc.status || 'not_approved';
        const activeVer = svc.active_version;

        // Version indicator instead of gates
        const versionHtml = activeVer
            ? `<span class="version-badge version-active" title="Active version">v${activeVer}</span>`
            : `<span class="version-badge version-none" title="No approved version">—</span>`;

        return `<tr onclick="showServiceDetail('${escapeHtml(svc.id)}')">
            <td>
                <div class="svc-name">${escapeHtml(svc.name)}</div>
                <div class="svc-id">${escapeHtml(svc.id)}</div>
            </td>
            <td><span class="category-badge">${escapeHtml(svc.category)}</span></td>
            <td>${versionHtml}</td>
            <td><span class="status-badge ${status}">${statusLabels[status] || status}</span></td>
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

// ── Service Detail Drawer (Versioned Onboarding) ────────────

let _currentVersions = null;

async function showServiceDetail(serviceId) {
    const svc = allServices.find(s => s.id === serviceId);
    if (!svc) return;

    const status = svc.status || 'not_approved';
    const drawer = document.getElementById('service-detail-drawer');
    const body = document.getElementById('detail-service-body');

    document.getElementById('detail-service-name').textContent = svc.name;

    body.innerHTML = `
        <div class="detail-meta">
            <span class="svc-id">${escapeHtml(svc.id)}</span>
            <span class="status-badge ${status}">${statusLabels[status] || status}</span>
            <span class="category-badge">${escapeHtml(svc.category)}</span>
            ${svc.risk_tier ? `<span class="category-badge risk-${svc.risk_tier}">${svc.risk_tier} risk</span>` : ''}
            ${svc.active_version ? `<span class="version-badge version-active">Active: v${svc.active_version}</span>` : ''}
        </div>
        <div class="gate-loading">Loading versions…</div>
    `;
    drawer.classList.remove('hidden');

    // Fetch versions and model settings in parallel
    try {
        const [versionsRes] = await Promise.all([
            fetch(`/api/services/${encodeURIComponent(serviceId)}/versions`),
            loadModelSettings(),
        ]);
        if (!versionsRes.ok) {
            const errText = await versionsRes.text();
            throw new Error(`Server returned ${versionsRes.status}: ${errText.slice(0, 200)}`);
        }
        const data = await versionsRes.json();
        _currentVersions = data.versions || [];
        _renderVersionedWorkflow(svc, _currentVersions, data.active_version);
        // Populate model selector AFTER the DOM element exists
        _populateModelSelector();
    } catch (err) {
        body.innerHTML += `<p style="color: var(--accent-red);">Failed to load versions: ${err.message}</p>
            <button class="btn btn-primary" style="margin-top: 0.5rem;" onclick="showServiceDetail('${escapeHtml(serviceId)}')">🔄 Retry</button>`;
    }
}

function _renderVersionedWorkflow(svc, versions, activeVersion) {
    const body = document.getElementById('detail-service-body');
    const status = svc.status || 'not_approved';
    const hasVersions = versions.length > 0;
    const latestVersion = versions.length > 0 ? versions[0] : null;

    // Pipeline description
    const pipelineSteps = [
        { icon: '📋', label: 'Standards', desc: 'Analyze organization standards for this resource type' },
        { icon: '🧠', label: 'Plan', desc: 'AI plans the architecture based on standards and best practices' },
        { icon: '⚡', label: 'Generate', desc: 'ARM template & Azure Policy generated with standards' },
        { icon: '📋', label: 'Static Check', desc: 'Static validation against org governance policies' },
        { icon: '🔍', label: 'What-If', desc: 'ARM What-If preview of deployment changes' },
        { icon: '🚀', label: 'Deploy', desc: 'Test deployment to validation resource group' },
        { icon: '🛡️', label: 'Policy Test', desc: 'Runtime policy compliance test on deployed resources' },
        { icon: '✅', label: 'Approve', desc: 'Version approved, service active' },
    ];

    // Distinguish governance approval from full onboarding
    const displayStatus = (status === 'approved' && !activeVersion)
        ? 'approved_not_onboarded' : status;
    const displayLabel = displayStatus === 'approved_not_onboarded'
        ? '📋 Catalog Approved' : (statusLabels[status] || status);

    body.innerHTML = `
        <div class="detail-meta">
            <span class="svc-id">${escapeHtml(svc.id)}</span>
            <span class="status-badge ${displayStatus}">${displayLabel}</span>
            <span class="category-badge">${escapeHtml(svc.category)}</span>
            ${svc.risk_tier ? `<span class="category-badge risk-${svc.risk_tier}">${svc.risk_tier} risk</span>` : ''}
            ${activeVersion ? `<span class="version-badge version-active">Active: v${activeVersion}</span>` : ''}
        </div>

        <div class="onboard-pipeline">
            <div class="pipeline-label">Onboarding Pipeline</div>
            <div class="pipeline-steps">
                ${pipelineSteps.map(s => `
                    <div class="pipeline-step" title="${s.desc}">
                        <span class="pipeline-step-icon">${s.icon}</span>
                        <span class="pipeline-step-label">${s.label}</span>
                    </div>
                `).join('<span class="pipeline-arrow">→</span>')}
            </div>
            <p class="pipeline-desc">All steps run automatically with AI-powered auto-healing. Validated against organization governance standards &amp; policies.</p>
        </div>

        <div class="onboard-model-selector" id="model-selector-container">
            <label class="model-selector-label">🤖 LLM Model</label>
            <select id="onboard-model-select" class="model-select">
                <option value="">Loading models…</option>
            </select>
            <span class="model-selector-hint" id="model-selector-hint"></span>
        </div>

        ${_renderOnboardButton(svc, status, latestVersion)}

        ${hasVersions ? _renderVersionHistory(versions, activeVersion) : ''}
    `;
}

function _renderOnboardButton(svc, status, latestVersion) {
    // Governance-approved AND has a validated version → fully onboarded
    if (status === 'approved' && latestVersion) {
        return `
        <div class="validation-card validation-succeeded">
            <div class="validation-header">
                <span class="validation-icon">✅</span>
                <span class="validation-title">Service Onboarded — v${latestVersion.version}</span>
            </div>
            <div class="validation-detail">
                This service has a validated ARM template and is approved for deployment.
                ${latestVersion.validated_at ? `Validated: ${latestVersion.validated_at.substring(0, 10)}` : ''}
            </div>
            <div class="validation-actions">
                <button class="btn btn-sm btn-secondary" onclick="triggerOnboarding('${escapeHtml(svc.id)}')">
                    🔄 Re-validate (New Version)
                </button>
            </div>
        </div>`;
    }

    // Governance-approved but no ARM template version yet → needs onboarding
    if (status === 'approved' && !latestVersion) {
        return `
        <div class="validation-card validation-ready" id="validation-card">
            <div class="validation-header">
                <span class="validation-icon">🚀</span>
                <span class="validation-title">One-Click Onboarding</span>
            </div>
            <div class="validation-detail">
                <strong>${escapeHtml(svc.name)}</strong> is approved for use in the organization but doesn't
                have an ARM template yet. Onboarding will auto-generate a validated, policy-compliant template.
            </div>
            <div class="validation-actions">
                <button class="btn btn-sm btn-accent" onclick="triggerOnboarding('${escapeHtml(svc.id)}')">
                    🚀 Onboard Service
                </button>
            </div>
            <div class="validation-log" id="validation-log"></div>
        </div>`;
    }

    if (status === 'validating') {
        return `
        <div class="validation-card validation-ready" id="validation-card">
            <div class="validation-header">
                <span class="validation-icon">🔄</span>
                <span class="validation-title">Validation In Progress</span>
            </div>
            <div class="validation-detail">Service is being validated…</div>
            <div class="validation-actions">
                <button class="btn btn-sm btn-accent" onclick="triggerOnboarding('${escapeHtml(svc.id)}')">
                    🚀 Restart Onboarding
                </button>
            </div>
            <div class="validation-log" id="validation-log"></div>
        </div>`;
    }

    if (status === 'validation_failed') {
        // Parse real error from review_notes or latest version's validation_result
        let errorDetail = '';
        const reviewNotes = svc.review_notes || '';
        if (reviewNotes) {
            const parsed = _parseValidationError(reviewNotes);
            errorDetail = _renderStructuredError(parsed, { compact: false, showRaw: true });
        }
        if (!errorDetail && latestVersion && latestVersion.validation_result) {
            const parsed = _parseValidationError(latestVersion.validation_result);
            errorDetail = _renderStructuredError(parsed, { compact: false, showRaw: true });
        }
        if (!errorDetail) {
            errorDetail = '<div class="validation-detail">The previous onboarding run failed. No error details available.</div>';
        }

        return `
        <div class="validation-card validation-failed" id="validation-card">
            <div class="validation-header">
                <span class="validation-icon">⛔</span>
                <span class="validation-title">Validation Failed</span>
            </div>
            ${errorDetail}
            <div class="validation-actions">
                <button class="btn btn-sm btn-primary" onclick="triggerOnboarding('${escapeHtml(svc.id)}')">
                    🤖 Retry Onboarding
                </button>
            </div>
            <div class="validation-log" id="validation-log"></div>
        </div>`;
    }

    // not_approved — show the main onboarding button
    return `
    <div class="validation-card validation-ready" id="validation-card">
        <div class="validation-header">
            <span class="validation-icon">🚀</span>
            <span class="validation-title">One-Click Onboarding</span>
        </div>
        <div class="validation-detail">
            Auto-generates an ARM template for <strong>${escapeHtml(svc.name)}</strong>, validates it against
            organization governance policies, deploys to a test resource group, then promotes to approved.
            No manual configuration needed.
        </div>
        <div class="validation-actions">
            <button class="btn btn-sm btn-accent" onclick="triggerOnboarding('${escapeHtml(svc.id)}')">
                🚀 Onboard Service
            </button>
        </div>
        <div class="validation-log" id="validation-log"></div>
    </div>`;
}

function _renderVersionHistory(versions, activeVersion) {
    const approvedVersions = versions.filter(v => v.status === 'approved');
    const draftVersions = versions.filter(v => v.status === 'draft');
    const totalCount = versions.length;
    const approvedCount = approvedVersions.length;
    const draftCount = draftVersions.length;

    let html = '';

    // ── Draft versions (pending validation) ──
    if (draftCount > 0) {
        html += `
        <div class="version-history version-history-drafts">
            <div class="version-history-header version-history-header-draft">
                <span>📝 Draft Versions (Pending Validation)</span>
                <span class="version-count">${draftCount} draft${draftCount === 1 ? '' : 's'}</span>
            </div>
            <div class="version-list">
                ${draftVersions.map(v => {
                    const sizeKB = v.template_size_bytes
                        ? (v.template_size_bytes / 1024).toFixed(1)
                        : v.arm_template
                            ? (v.arm_template.length / 1024).toFixed(1)
                            : '?';
                    const displayVer = v.semver || `${v.version}.0.0`;

                    return `
                    <div class="version-item version-item-draft" onclick="toggleVersionDetail(this)">
                        <div class="version-item-header">
                            <span class="version-item-badge version-badge-draft">v${displayVer}</span>
                            <span class="version-item-status version-status-draft">📝 draft</span>
                            <span class="version-item-date">${(v.created_at || '').substring(0, 10)}</span>
                            <span class="version-item-by">${escapeHtml(v.created_by || '')}</span>
                        </div>
                        <div class="version-item-detail hidden">
                            <div class="version-detail-row">
                                <strong>Changelog:</strong> ${escapeHtml(v.changelog || 'Modified template')}
                            </div>
                            <div class="version-detail-row">
                                <strong>Template:</strong> ${sizeKB} KB
                            </div>
                            <div class="version-detail-actions">
                                <button class="btn btn-sm btn-accent" onclick="event.stopPropagation(); triggerDraftValidation('${escapeHtml(v.service_id)}', ${v.version}, '${displayVer}')">
                                    🚀 Validate & Promote
                                </button>
                                <button class="btn btn-sm btn-primary" onclick="event.stopPropagation(); viewTemplate('${escapeHtml(v.service_id)}', ${v.version})">
                                    👁 View Template
                                </button>
                                <button class="btn btn-sm btn-secondary" onclick="event.stopPropagation(); downloadTemplateVersion('${escapeHtml(v.service_id)}', ${v.version})">
                                    ⬇ Download
                                </button>
                            </div>
                        </div>
                    </div>`;
                }).join('')}
            </div>
        </div>`;
    }

    // ── Approved versions ──
    if (approvedCount === 0 && draftCount === 0) {
        html += `
        <div class="version-history">
            <div class="version-history-header">
                <span>📦 Published Versions</span>
                <span class="version-count">No versions yet (${totalCount} total run${totalCount === 1 ? '' : 's'})</span>
            </div>
        </div>`;
    } else {
        html += `
        <div class="version-history">
            <div class="version-history-header">
                <span>📦 Published Versions</span>
                <span class="version-count">${approvedCount} approved version${approvedCount === 1 ? '' : 's'}</span>
            </div>
            ${approvedCount === 0 ? '' : `<div class="version-list">
                ${approvedVersions.map(v => {
                    const isActive = v.version === activeVersion;
                    const sizeKB = v.template_size_bytes
                        ? (v.template_size_bytes / 1024).toFixed(1)
                        : v.arm_template
                            ? (v.arm_template.length / 1024).toFixed(1)
                            : '?';
                    const displayVer = v.semver || `${v.version}.0.0`;

                    return `
                    <div class="version-item ${isActive ? 'version-item-active' : ''}" onclick="toggleVersionDetail(this)">
                        <div class="version-item-header">
                            <span class="version-item-badge">v${displayVer}</span>
                            <span class="version-item-status">✅ approved</span>
                            ${isActive ? '<span class="version-item-active-label">ACTIVE</span>' : '<span class="version-item-deprecated-label">SUPERSEDED</span>'}
                            <span class="version-item-date">${(v.created_at || '').substring(0, 10)}</span>
                            <span class="version-item-by">${escapeHtml(v.created_by || '')}</span>
                        </div>
                        <div class="version-item-detail hidden">
                            <div class="version-detail-row">
                                <strong>Changelog:</strong> ${escapeHtml(v.changelog || 'Initial onboarding')}
                            </div>
                            ${v.policy_check && v.policy_check.total_checks ? `
                            <div class="version-detail-row">
                                <strong>Policy:</strong> ${v.policy_check.passed_checks}/${v.policy_check.total_checks} passed,
                                ${v.policy_check.blockers || 0} blocker(s)
                            </div>` : ''}
                            <div class="version-detail-row">
                                <strong>Template:</strong> ${sizeKB} KB
                            </div>
                            ${v.run_id ? `
                            <div class="version-detail-row version-tracking-info">
                                <strong>🔗 Deployment Tracking:</strong>
                                <span class="tracking-field" title="Validation run ID">Run: <code>${escapeHtml(v.run_id)}</code></span>
                                <span class="tracking-field" title="Azure Resource Group">RG: <code>${escapeHtml(v.resource_group || '')}</code></span>
                                <span class="tracking-field" title="ARM Deployment Name">Deploy: <code>${escapeHtml(v.deployment_name || '')}</code></span>
                                ${v.subscription_id ? `<span class="tracking-field" title="Azure Subscription">Sub: <code>${escapeHtml(v.subscription_id.substring(0, 12))}…</code></span>` : ''}
                            </div>` : ''}
                            <div class="version-detail-actions">
                                <button class="btn btn-sm btn-primary" onclick="event.stopPropagation(); viewTemplate('${escapeHtml(v.service_id)}', ${v.version})">
                                    👁 View Template
                                </button>
                                <button class="btn btn-sm btn-secondary" onclick="event.stopPropagation(); downloadTemplateVersion('${escapeHtml(v.service_id)}', ${v.version})">
                                    ⬇ Download
                                </button>
                            </div>
                        </div>
                    </div>`;
                }).join('')}
            </div>`}
        </div>`;
    }

    return html;
}

// ── Template Viewer ─────────────────────────────────────────

let _currentTemplateContent = '';
let _currentTemplateFilename = '';
let _currentTemplateServiceId = '';
let _currentTemplateVersion = null;

async function viewTemplate(serviceId, version) {
    const modal = document.getElementById('modal-template-viewer');
    const title = document.getElementById('template-viewer-title');
    const meta = document.getElementById('template-viewer-meta');
    const code = document.getElementById('template-viewer-code');

    title.textContent = `ARM Template — v${version}`;
    meta.innerHTML = `<span class="template-meta-badge">📦 ${escapeHtml(serviceId)}</span><span class="template-meta-badge">Loading…</span><span class="template-meta-loading">Loading…</span>`;
    code.querySelector('code').textContent = 'Loading template…';
    _currentTemplateContent = '';
    _currentTemplateFilename = `${serviceId.replace(/\//g, '_')}_v${version}.json`;
    _currentTemplateServiceId = serviceId;
    _currentTemplateVersion = version;

    // Reset modification UI
    const modifyPrompt = document.getElementById('template-modify-prompt');
    const modifyProgress = document.getElementById('template-modify-progress');
    const modifyBtn = document.getElementById('template-modify-btn');
    if (modifyPrompt) modifyPrompt.value = '';
    if (modifyProgress) { modifyProgress.classList.add('hidden'); modifyProgress.innerHTML = ''; }
    if (modifyBtn) { modifyBtn.disabled = false; modifyBtn.textContent = '🚀 Apply'; }

    modal.classList.remove('hidden');

    try {
        const res = await fetch(`/api/services/${encodeURIComponent(serviceId)}/versions/${version}`);
        if (!res.ok) throw new Error(`Server returned ${res.status}`);
        const data = await res.json();
        const template = data.arm_template || '';

        // Pretty-print the JSON
        let formatted;
        try {
            formatted = JSON.stringify(JSON.parse(template), null, 2);
        } catch {
            formatted = template;
        }

        _currentTemplateContent = formatted;

        // Render with basic syntax highlighting
        code.querySelector('code').innerHTML = _highlightJSON(formatted);

        // Update meta — extract InfraForge metadata from the template itself
        const sizeKB = (formatted.length / 1024).toFixed(1);
        const validatedAt = data.validated_at ? data.validated_at.substring(0, 10) : '—';
        const semver = data.semver || `${version}.0.0`;

        // Try to extract embedded metadata
        let tmplMeta = null;
        try {
            const parsed = JSON.parse(template);
            tmplMeta = parsed.metadata?.infrapiForge || null;
        } catch {}

        const metaBadges = [
            `<span class="template-meta-badge">📦 ${escapeHtml(serviceId)}</span>`,
            `<span class="template-meta-badge">v${semver}</span>`,
            `<span class="template-meta-badge">${sizeKB} KB</span>`,
            `<span class="template-meta-badge">Validated: ${validatedAt}</span>`,
        ];

        if (tmplMeta) {
            if (tmplMeta.generatedBy) metaBadges.push(`<span class="template-meta-badge">🔧 ${escapeHtml(tmplMeta.generatedBy)}</span>`);
            if (tmplMeta.generatedAt) metaBadges.push(`<span class="template-meta-badge">📅 ${tmplMeta.generatedAt.substring(0, 10)}</span>`);
        }
        const templateHash = data.arm_template ? (() => { try { const p = JSON.parse(data.arm_template); return p.metadata?._generator?.templateHash || ''; } catch { return ''; } })() : '';
        if (templateHash) metaBadges.push(`<span class="template-meta-badge" title="Content hash">🔑 ${templateHash}</span>`);

        title.textContent = `ARM Template — v${semver}`;
        meta.innerHTML = metaBadges.join('\n');
    } catch (err) {
        code.querySelector('code').textContent = `Error loading template: ${err.message}`;
        meta.querySelector('.template-meta-loading')?.remove();
    }
}

function _highlightJSON(json) {
    // Lightweight JSON syntax highlighting
    return json
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        // Strings (keys and values)
        .replace(/"([^"\\]*(\\.[^"\\]*)*)"\s*:/g, '<span class="json-key">"$1"</span>:')
        .replace(/:\s*"([^"\\]*(\\.[^"\\]*)*)"/g, ': <span class="json-string">"$1"</span>')
        // Standalone strings (in arrays, etc.)
        .replace(/(?<=[\[,\n]\s*)"([^"\\]*(\\.[^"\\]*)*)"/g, '<span class="json-string">"$1"</span>')
        // Numbers
        .replace(/:\s*(\d+\.?\d*)/g, ': <span class="json-number">$1</span>')
        // Booleans & null
        .replace(/:\s*(true|false|null)\b/g, ': <span class="json-bool">$1</span>');
}

function copyTemplateToClipboard() {
    if (!_currentTemplateContent) return;
    navigator.clipboard.writeText(_currentTemplateContent).then(() => {
        showToast('Template copied to clipboard', 'success');
    }).catch(() => {
        showToast('Failed to copy', 'error');
    });
}

function downloadTemplate() {
    downloadTemplateBlob(_currentTemplateContent, _currentTemplateFilename);
}

async function downloadTemplateVersion(serviceId, version) {
    try {
        const res = await fetch(`/api/services/${encodeURIComponent(serviceId)}/versions/${version}`);
        if (!res.ok) throw new Error(`Server returned ${res.status}`);
        const data = await res.json();
        const template = data.arm_template || '';
        let formatted;
        try { formatted = JSON.stringify(JSON.parse(template), null, 2); } catch { formatted = template; }
        const filename = `${serviceId.replace(/\//g, '_')}_v${version}.json`;
        downloadTemplateBlob(formatted, filename);
    } catch (err) {
        showToast(`Failed to download: ${err.message}`, 'error');
    }
}

function downloadTemplateBlob(content, filename) {
    if (!content) return;
    const blob = new Blob([content], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename || 'template.json';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    showToast('Template downloaded', 'success');
}

async function submitTemplateModification() {
    const promptEl = document.getElementById('template-modify-prompt');
    const progressEl = document.getElementById('template-modify-progress');
    const btnEl = document.getElementById('template-modify-btn');
    const prompt = (promptEl?.value || '').trim();

    if (!prompt) {
        showToast('Please describe the modification you want to make', 'error');
        promptEl?.focus();
        return;
    }
    if (!_currentTemplateServiceId || _currentTemplateVersion === null) {
        showToast('No template loaded to modify', 'error');
        return;
    }

    // Disable UI during modification
    btnEl.disabled = true;
    btnEl.textContent = '⏳ Working…';
    progressEl.classList.remove('hidden');
    progressEl.innerHTML = '<div class="modify-progress-item">⏳ Starting modification…</div>';

    try {
        const url = `/api/services/${encodeURIComponent(_currentTemplateServiceId)}/versions/${_currentTemplateVersion}/modify`;
        const res = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ prompt }),
        });

        if (!res.ok) {
            const err = await res.json().catch(() => ({ detail: res.statusText }));
            throw new Error(err.detail || `Server returned ${res.status}`);
        }

        // Stream NDJSON events
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let finalEvent = null;

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });

            const lines = buffer.split('\n');
            buffer = lines.pop() || '';

            for (const line of lines) {
                if (!line.trim()) continue;
                try {
                    const ev = JSON.parse(line);
                    const icon = ev.type === 'error' ? '❌' : ev.type === 'complete' ? '✅' : '⏳';
                    progressEl.innerHTML += `<div class="modify-progress-item">${icon} ${escapeHtml(ev.detail || '')}</div>`;
                    progressEl.scrollTop = progressEl.scrollHeight;
                    finalEvent = ev;
                } catch {}
            }
        }

        // Handle completion
        if (finalEvent?.type === 'complete') {
            showToast(`Draft v${finalEvent.semver} saved — validate to promote`, 'success');

            // Reload the template viewer with the new draft version
            setTimeout(() => {
                viewTemplate(_currentTemplateServiceId, finalEvent.version);
            }, 600);

            // Refresh the service detail panel to show the new draft in version history
            if (typeof loadServiceDetail === 'function') {
                setTimeout(() => loadServiceDetail(_currentTemplateServiceId), 800);
            }
        } else if (finalEvent?.type === 'error') {
            showToast(finalEvent.detail || 'Modification failed', 'error');
            btnEl.disabled = false;
            btnEl.textContent = '🚀 Apply';
        }
    } catch (err) {
        progressEl.innerHTML += `<div class="modify-progress-item">❌ ${escapeHtml(err.message)}</div>`;
        showToast(`Modification failed: ${err.message}`, 'error');
        btnEl.disabled = false;
        btnEl.textContent = '🚀 Apply';
    }
}

function toggleVersionDetail(el) {
    const detail = el.querySelector('.version-item-detail');
    if (detail) detail.classList.toggle('hidden');
}

async function triggerDraftValidation(serviceId, version, semver) {
    // Close the template viewer if open
    closeModal('modal-template-viewer');

    showToast(`Starting validation for draft v${semver}…`, 'info');

    // Trigger the onboard pipeline with use_version to skip generation
    const card = document.getElementById('validation-card');
    const modelSelect = document.getElementById('onboard-model-select');
    const selectedModel = modelSelect ? modelSelect.value : '';

    if (card) {
        card.className = 'validation-card validation-running';
        card.innerHTML = `
            <div class="validation-header">
                <span class="validation-icon validation-spinner">⏳</span>
                <span class="validation-title">Validating Draft v${semver}…</span>
            </div>
            <div class="validation-model-badge" id="validation-model-badge"></div>
            <div class="validation-attempt-badge" id="validation-attempt-badge"></div>
            <div class="validation-progress">
                <div class="validation-progress-track">
                    <div class="validation-progress-fill" id="validation-progress-fill" style="width: 0%"></div>
                </div>
            </div>
            <div class="validation-detail" id="validation-detail">Initializing validation pipeline for draft v${semver}…</div>
            <div class="validation-log" id="validation-log">
                <div class="validation-log-header">
                    <span>Validation Log</span>
                    <button class="log-toggle-reasoning" id="toggle-reasoning-btn" onclick="toggleReasoningVisibility()" title="Show/hide AI reasoning">🧠 AI Thinking</button>
                </div>
            </div>
        `;
    }

    try {
        const body = { use_version: version };
        if (selectedModel) body.model = selectedModel;

        const res = await fetch(`/api/services/${encodeURIComponent(serviceId)}/onboard`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });

        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Validation request failed');
        }

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });

            const lines = buffer.split('\n');
            buffer = lines.pop() || '';

            for (const line of lines) {
                if (!line.trim()) continue;
                try {
                    const event = JSON.parse(line);
                    _handleValidationEvent(event);
                } catch (e) {}
            }
        }

        if (buffer.trim()) {
            try {
                _handleValidationEvent(JSON.parse(buffer));
            } catch (e) {}
        }

        await loadAllData();
        await showServiceDetail(serviceId);

    } catch (err) {
        showToast(`Validation failed: ${err.message}`, 'error');
        if (card) {
            card.className = 'validation-card validation-failed';
            card.innerHTML = `
                <div class="validation-header">
                    <span class="validation-icon">❌</span>
                    <span class="validation-title">Validation Failed</span>
                </div>
                <div class="validation-detail">${escapeHtml(err.message)}</div>
            `;
        }
    }
}

// ── Model Selector ──────────────────────────────────────────

let availableModels = [];
let activeModel = '';

async function loadModelSettings() {
    try {
        const res = await fetch('/api/settings/model');
        if (!res.ok) return;
        const data = await res.json();
        availableModels = data.available_models || [];
        activeModel = data.active_model || '';
        _populateModelSelector();
    } catch (e) {
        console.warn('Could not load model settings:', e);
    }
}

function _populateModelSelector() {
    const select = document.getElementById('onboard-model-select');
    const hint = document.getElementById('model-selector-hint');
    if (!select) return;

    const providerGroups = {};
    for (const m of availableModels) {
        if (!providerGroups[m.provider]) providerGroups[m.provider] = [];
        providerGroups[m.provider].push(m);
    }

    let html = '';
    for (const [provider, models] of Object.entries(providerGroups)) {
        html += `<optgroup label="${provider}">`;
        for (const m of models) {
            const selected = m.id === activeModel ? 'selected' : '';
            const tier = m.tier ? ` [${m.tier}]` : '';
            html += `<option value="${m.id}" ${selected}>${m.name}${tier}</option>`;
        }
        html += '</optgroup>';
    }
    select.innerHTML = html;

    const activeMeta = availableModels.find(m => m.id === activeModel);
    if (hint && activeMeta) {
        hint.textContent = activeMeta.description || '';
    }

    select.addEventListener('change', () => {
        const selected = availableModels.find(m => m.id === select.value);
        if (hint && selected) hint.textContent = selected.description || '';
    });
}

async function changeGlobalModel(modelId) {
    try {
        const res = await fetch('/api/settings/model', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ model_id: modelId }),
        });
        if (res.ok) {
            activeModel = modelId;
            showToast(`Model changed to ${modelId}`, 'success');
        }
    } catch (e) {
        showToast(`Failed to change model: ${e.message}`, 'error');
    }
}

async function triggerOnboarding(serviceId) {
    const card = document.getElementById('validation-card');
    const modelSelect = document.getElementById('onboard-model-select');
    const selectedModel = modelSelect ? modelSelect.value : '';

    if (card) {
        card.className = 'validation-card validation-running';
        card.innerHTML = `
            <div class="validation-header">
                <span class="validation-icon validation-spinner">⏳</span>
                <span class="validation-title">Onboarding In Progress…</span>
            </div>
            <div class="validation-model-badge" id="validation-model-badge"></div>
            <div class="validation-attempt-badge" id="validation-attempt-badge"></div>
            <div class="validation-progress">
                <div class="validation-progress-track">
                    <div class="validation-progress-fill" id="validation-progress-fill" style="width: 0%"></div>
                </div>
            </div>
            <div class="validation-detail" id="validation-detail">Initializing onboarding pipeline…</div>
            <div class="validation-log" id="validation-log">
                <div class="validation-log-header">
                    <span>Onboarding Log</span>
                    <button class="log-toggle-reasoning" id="toggle-reasoning-btn" onclick="toggleReasoningVisibility()" title="Show/hide AI reasoning">🧠 AI Thinking</button>
                </div>
            </div>
        `;
    }

    try {
        const body = {};
        if (selectedModel) body.model = selectedModel;

        const res = await fetch(`/api/services/${encodeURIComponent(serviceId)}/onboard`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });

        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Onboarding request failed');
        }

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });

            const lines = buffer.split('\n');
            buffer = lines.pop() || '';

            for (const line of lines) {
                if (!line.trim()) continue;
                try {
                    const event = JSON.parse(line);
                    _handleValidationEvent(event);
                } catch (e) {}
            }
        }

        if (buffer.trim()) {
            try {
                _handleValidationEvent(JSON.parse(buffer));
            } catch (e) {}
        }

        await loadAllData();
        await showServiceDetail(serviceId);

    } catch (err) {
        showToast(`Onboarding failed: ${err.message}`, 'error');
        const detail = document.getElementById('validation-detail');
        if (detail) detail.textContent = `Error: ${err.message}`;
        const cardEl = document.getElementById('validation-card');
        if (cardEl) cardEl.className = 'validation-card validation-failed';
    }
}

function _handleValidationEvent(event) {
    const progressFill = document.getElementById('validation-progress-fill');
    const detailEl = document.getElementById('validation-detail');
    const logEl = document.getElementById('validation-log');
    const badge = document.getElementById('validation-attempt-badge');
    const modelBadge = document.getElementById('validation-model-badge');
    const card = document.getElementById('validation-card');

    if (event.progress && progressFill) {
        progressFill.style.width = `${Math.min(event.progress * 100, 100)}%`;
    }
    if (event.detail && detailEl) {
        detailEl.textContent = event.detail;
    }
    if (event.step && badge) {
        badge.textContent = event.step > 1 ? `Step ${event.step}` : 'Deploying…';
        badge.classList.add('visible');
    }

    // Show model badge on init_model event
    if (event.phase === 'init_model' && event.model && modelBadge) {
        modelBadge.textContent = `🤖 ${event.model.display || event.model.id}`;
        modelBadge.classList.add('visible');
    }

    // Pick icon per event type
    let icon = '▸';
    let logClass = event.type || 'progress';
    let isReasoning = false;

    switch (event.type) {
        case 'error':           icon = '❌'; break;
        case 'done':            icon = '✅'; break;
        case 'iteration_start': icon = '🔄'; break;
        case 'healing':         icon = '🤖'; break;
        case 'healing_done':    icon = '🔧'; break;
        case 'standard_check':  icon = '📏'; logClass = 'standard'; break;
        case 'llm_reasoning':   icon = '🧠'; logClass = 'reasoning'; isReasoning = true; break;
        case 'policy_result':   icon = event.compliant !== undefined ? (event.compliant ? '✅' : '❌') : (event.passed ? '✅' : (event.severity === 'high' || event.severity === 'critical' ? '❌' : '⚠️')); break;
        default:
            if (event.phase === 'init_model')                   icon = '🤖';
            else if (event.phase === 'standards_analysis')      icon = '📋';
            else if (event.phase === 'standards_complete')       icon = '✓';
            else if (event.phase === 'planning')                icon = '🧠';
            else if (event.phase === 'planning_complete')       icon = '✓';
            else if (event.phase === 'generating')              icon = '⚡';
            else if (event.phase === 'generated')               icon = '📄';
            else if (event.phase === 'policy_generation')       icon = '🛡️';
            else if (event.phase === 'policy_generation_complete') icon = '✓';
            else if (event.phase === 'policy_generation_warning') icon = '⚠️';
            else if (event.phase === 'static_policy_check')     icon = '📋';
            else if (event.phase === 'static_policy_complete')  icon = '✓';
            else if (event.phase === 'static_policy_failed')    icon = '⚠️';
            else if (event.phase === 'what_if')                 icon = '🔍';
            else if (event.phase === 'what_if_complete')        icon = '✓';
            else if (event.phase === 'deploying')               icon = '🚀';
            else if (event.phase === 'deploy_complete')         icon = '📦';
            else if (event.phase === 'deploy_failed')           icon = '💥';
            else if (event.phase === 'resource_check' || event.phase === 'resource_check_complete') icon = '🔎';
            else if (event.phase === 'policy_testing')          icon = '🛡️';
            else if (event.phase === 'policy_testing_complete')  icon = '✓';
            else if (event.phase === 'policy_failed')           icon = '❌';
            else if (event.phase === 'policy_skip')             icon = 'ℹ️';
            else if (event.phase === 'cleanup' || event.phase === 'cleanup_complete') icon = '🧹';
            else if (event.phase === 'promoting')               icon = '🏆';
            break;
    }

    if (logEl && event.detail) {
        const logLine = document.createElement('div');
        logLine.className = `validation-log-line validation-log-${logClass}`;
        if (event.phase) logLine.classList.add(`validation-phase-${event.phase}`);
        if (isReasoning) logLine.classList.add('reasoning-line');
        logLine.innerHTML = `<span class="log-icon">${icon}</span> <span class="log-text">${escapeHtml(event.detail)}</span>`;
        logEl.appendChild(logLine);
        logEl.scrollTop = logEl.scrollHeight;
    }

    // Update header
    const header = card?.querySelector('.validation-title');
    const iconEl = card?.querySelector('.validation-icon');

    if (event.phase === 'init_model' && header) {
        header.textContent = 'Analyzing Organization Standards…';
        if (iconEl) { iconEl.textContent = '📋'; iconEl.classList.add('validation-spinner'); }
    } else if (event.phase === 'standards_analysis' && header) {
        header.textContent = 'Analyzing Organization Standards…';
        if (iconEl) { iconEl.textContent = '📋'; iconEl.classList.add('validation-spinner'); }
    } else if (event.phase === 'planning' && header) {
        header.textContent = 'AI Planning Architecture…';
        if (iconEl) { iconEl.textContent = '🧠'; iconEl.classList.add('validation-spinner'); }
    } else if (event.phase === 'generating' && header) {
        header.textContent = 'Generating ARM Template…';
        if (iconEl) { iconEl.textContent = '⚡'; iconEl.classList.add('validation-spinner'); }
    } else if (event.phase === 'policy_generation' && header) {
        header.textContent = 'Generating Azure Policy…';
        if (iconEl) { iconEl.textContent = '🛡️'; iconEl.classList.add('validation-spinner'); }
    } else if (event.phase === 'static_policy_check' && header) {
        header.textContent = 'Checking Governance Policies…';
        if (iconEl) { iconEl.textContent = '📋'; iconEl.classList.add('validation-spinner'); }
    } else if (event.phase === 'policy_testing' && header) {
        header.textContent = 'Testing Runtime Policy Compliance…';
        if (iconEl) { iconEl.textContent = '🛡️'; iconEl.classList.add('validation-spinner'); }
    } else if (event.type === 'healing' && header) {
        header.textContent = 'Auto-Healing — AI Fixing Template…';
        if (iconEl) { iconEl.textContent = '🤖'; iconEl.classList.add('validation-spinner'); }
    } else if (event.phase === 'what_if' && header) {
        header.textContent = 'Running ARM What-If Analysis…';
        if (iconEl) { iconEl.textContent = '🔍'; iconEl.classList.add('validation-spinner'); }
    } else if (event.phase === 'deploying' && header) {
        header.textContent = 'Deploying to Validation RG…';
        if (iconEl) { iconEl.textContent = '🚀'; iconEl.classList.add('validation-spinner'); }
    } else if (event.phase === 'cleanup' && header) {
        header.textContent = 'Cleaning Up…';
        if (iconEl) { iconEl.textContent = '🧹'; }
    }

    // Final states
    if (event.type === 'done' && card) {
        card.className = 'validation-card validation-succeeded';
        if (header) header.textContent = `Service Approved — v${event.version || '?'}`;
        if (iconEl) { iconEl.textContent = '✅'; iconEl.classList.remove('validation-spinner'); }
        if (badge && event.issues_resolved > 0) {
            badge.textContent = `Resolved ${event.issues_resolved} issue${event.issues_resolved !== 1 ? 's' : ''}`;
            badge.classList.add('badge-success');
        }
    } else if (event.type === 'error' && card) {
        card.className = 'validation-card validation-failed';
        if (header) header.textContent = 'Onboarding Failed';
        if (iconEl) { iconEl.textContent = '⛔'; iconEl.classList.remove('validation-spinner'); }
    }
}

let reasoningVisible = true;
function toggleReasoningVisibility() {
    reasoningVisible = !reasoningVisible;
    const btn = document.getElementById('toggle-reasoning-btn');
    if (btn) {
        btn.classList.toggle('active', reasoningVisible);
        btn.textContent = reasoningVisible ? '🧠 AI Thinking' : '🧠 Hidden';
    }
    document.querySelectorAll('.reasoning-line').forEach(el => {
        el.style.display = reasoningVisible ? '' : 'none';
    });
}

function closeServiceDetail() {
    document.getElementById('service-detail-drawer').classList.add('hidden');
}

// ── Template Catalog ────────────────────────────────────────

function renderTemplateTable(templates) {
    const grid = document.getElementById('template-cards-grid');
    if (!grid) return;

    // Update results summary
    const summary = document.getElementById('template-results-summary');
    if (summary) {
        const typeCount = { foundation: 0, workload: 0, composite: 0 };
        templates.forEach(t => { typeCount[t.template_type || 'workload']++; });
        summary.textContent = `Showing ${templates.length} of ${allTemplates.length} templates` +
            ` — 🏗️ ${typeCount.foundation} foundation, ⚙️ ${typeCount.workload} workload, 📦 ${typeCount.composite} composite`;
    }

    if (!templates.length) {
        grid.innerHTML = `
            <div class="tmpl-empty-state">
                <h3>No templates yet</h3>
                <p>Compose your first deployment template from approved services.</p>
                <button class="btn btn-primary" onclick="openTemplateOnboarding()">+ Create Template</button>
            </div>`;
        return;
    }

    const typeIcons = { foundation: '🏗️', workload: '⚙️', composite: '📦' };
    const typeLabels = { foundation: 'Foundation', workload: 'Workload', composite: 'Composite' };
    const statusLabelsMap = {
        approved: '✅ Published',
        draft: '📝 Draft',
        passed: '🧪 Tested — needs validation',
        validated: '🔬 Validated — ready to publish',
        failed: '❌ Failed',
        deprecated: '⚠️ Deprecated',
    };

    grid.innerHTML = templates.map(tmpl => {
        const ttype = tmpl.template_type || 'workload';
        const icon = typeIcons[ttype] || '📋';
        const requires = tmpl.requires || [];
        const provides = tmpl.provides || [];
        const optionalRefs = tmpl.optional_refs || [];
        const status = tmpl.status || 'approved';
        const serviceIds = tmpl.service_ids || [];
        const isStandalone = ttype === 'foundation' || ttype === 'composite';

        return `
        <div class="tmpl-card tmpl-card-${ttype}" onclick="showTemplateDetail('${escapeHtml(tmpl.id)}')">
            <div class="tmpl-card-header">
                <div class="tmpl-card-title">
                    <span class="tmpl-type-icon">${icon}</span>
                    <div>
                        <strong>${escapeHtml(tmpl.name)}</strong>
                        <div class="tmpl-card-id">${escapeHtml(tmpl.id)}</div>
                    </div>
                </div>
                <div class="tmpl-card-badges">
                    <span class="tmpl-type-badge tmpl-type-${ttype}">${typeLabels[ttype]}</span>
                    <span class="status-badge ${status}">${statusLabelsMap[status] || status}</span>
                </div>
            </div>
            ${tmpl.description ? `<p class="tmpl-card-desc">${escapeHtml(tmpl.description)}</p>` : ''}
            <div class="tmpl-card-body">
                <div class="tmpl-provides">
                    <span class="tmpl-section-label">Creates:</span>
                    ${provides.map(p => `<span class="tmpl-chip tmpl-chip-provides">${_shortType(p)}</span>`).join('')}
                </div>
                ${requires.length ? `
                <div class="tmpl-requires">
                    <span class="tmpl-section-label">⚠️ Requires:</span>
                    ${requires.map(r => `<span class="tmpl-chip tmpl-chip-requires" title="${escapeHtml(r.reason || '')}">${_shortType(r.type || r)}</span>`).join('')}
                </div>` : ''}
                ${optionalRefs.length ? `
                <div class="tmpl-optional">
                    <span class="tmpl-section-label">Optional:</span>
                    ${optionalRefs.map(o => `<span class="tmpl-chip tmpl-chip-optional" title="${escapeHtml(o.reason || '')}">${_shortType(o.type || o)}</span>`).join('')}
                </div>` : ''}
            </div>
            <div class="tmpl-card-footer">
                <div class="tmpl-card-meta">
                    <span class="tmpl-format-badge">${escapeHtml(tmpl.format || 'arm')}</span>
                    <span class="tmpl-cat-badge">${escapeHtml(tmpl.category || '')}</span>
                    ${serviceIds.length ? `<span class="tmpl-svc-count">${serviceIds.length} service${serviceIds.length !== 1 ? 's' : ''}</span>` : ''}
                    ${tmpl.active_version ? `<span class="tmpl-ver-badge">v${tmpl.active_version}</span>` : ''}
                </div>
                <span class="tmpl-standalone-badge ${isStandalone ? 'standalone-yes' : 'standalone-no'}">
                    ${isStandalone ? '✅ Standalone' : '⚙️ Needs infra'}
                </span>
            </div>
        </div>`;
    }).join('');
}

/** Short display name from a resource type, e.g. "Microsoft.Network/virtualNetworks" → "virtualNetworks" */
function _shortType(resourceType) {
    if (!resourceType) return '?';
    const parts = resourceType.split('/');
    return parts[parts.length - 1];
}

function filterTemplateType(typeFilter) {
    currentTemplateTypeFilter = typeFilter;
    const container = document.getElementById('template-type-filters');
    if (container) {
        container.querySelectorAll('.filter-pill').forEach(pill => pill.classList.remove('active'));
        event.target.classList.add('active');
    }
    applyTemplateFilters();
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

    // Template type filter (foundation / workload / composite)
    if (currentTemplateTypeFilter !== 'all') {
        filtered = filtered.filter(t => (t.template_type || 'workload') === currentTemplateTypeFilter);
    }

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
            (t.resources || []).some(r => r.toLowerCase().includes(templateSearchQuery)) ||
            (t.provides || []).some(p => p.toLowerCase().includes(templateSearchQuery)) ||
            (t.template_type || '').toLowerCase().includes(templateSearchQuery)
        );
    }

    renderTemplateTable(filtered);
}

// ── Template Detail Drawer ──────────────────────────────────

function showTemplateDetail(templateId) {
    const tmpl = allTemplates.find(t => t.id === templateId);
    if (!tmpl) return;

    const status = tmpl.status || 'approved';
    const ttype = tmpl.template_type || 'workload';
    const typeIcons = { foundation: '🏗️', workload: '⚙️', composite: '📦' };
    const typeLabels = { foundation: 'Foundation', workload: 'Workload', composite: 'Composite' };
    const isStandalone = ttype === 'foundation' || ttype === 'composite';
    const requires = tmpl.requires || [];
    const provides = tmpl.provides || [];
    const optionalRefs = tmpl.optional_refs || [];
    const activeVer = tmpl.active_version;

    const statusBadgeMap = {
        approved: '✅ Published',
        draft: '📝 Draft',
        passed: '🧪 Tested — needs validation',
        validated: '🔬 Validated — ready to publish',
        failed: '❌ Failed',
        deprecated: '⚠️ Deprecated',
    };

    // ── Status-aware CTA ──
    let ctaHtml = '';
    if (status === 'draft') {
        ctaHtml = `
        <div class="detail-section tmpl-test-cta">
            <div class="tmpl-test-banner tmpl-test-pending">
                📝 <strong>New Template</strong> — Run validation to verify this template meets structural and Azure requirements.
            </div>
            <button class="btn btn-primary btn-sm" onclick="runFullValidation('${escapeHtml(tmpl.id)}')">
                🧪 Validate
            </button>
        </div>`;
    } else if (status === 'passed') {
        ctaHtml = `
        <div class="detail-section tmpl-test-cta">
            <div class="tmpl-test-banner tmpl-test-validate">
                ✅ Structural tests passed. Validate against Azure to confirm deployment readiness.
            </div>
            <button class="btn btn-primary btn-sm" onclick="runFullValidation('${escapeHtml(tmpl.id)}', true)">
                🧪 Validate Against Azure
            </button>
        </div>`;
    } else if (status === 'validated') {
        ctaHtml = `
        <div class="detail-section tmpl-test-cta">
            <div class="tmpl-test-banner tmpl-test-ready">
                ✅ <strong>Validated</strong> — Template verified against Azure. Ready to publish to the catalog.
            </div>
            <button class="btn btn-primary btn-sm" onclick="publishTemplate('${escapeHtml(tmpl.id)}')">
                🚀 Publish to Catalog
            </button>
        </div>`;
    } else if (status === 'failed') {
        ctaHtml = `
        <div class="detail-section tmpl-test-cta">
            <div class="tmpl-test-banner tmpl-test-failed">
                ❌ Validation found issues — auto-heal will attempt to fix them, or describe changes below.
            </div>
            <div style="display:flex; gap:0.5rem; flex-wrap:wrap;">
                <button class="btn btn-primary btn-sm" onclick="autoHealTemplate('${escapeHtml(tmpl.id)}')">
                    🔧 Auto-Heal
                </button>
                <button class="btn btn-sm" onclick="runFullValidation('${escapeHtml(tmpl.id)}')">
                    🧪 Re-validate
                </button>
            </div>
        </div>`;
    } else if (status === 'approved') {
        ctaHtml = `
        <div class="detail-section tmpl-test-cta">
            <div class="tmpl-test-banner tmpl-test-approved">
                ✅ <strong>Published & Ready</strong> — This template is approved and available in the catalog.
            </div>
            <div class="tmpl-deploy-actions">
                <button class="btn btn-primary btn-sm" onclick="showDeployForm('${escapeHtml(tmpl.id)}')">
                    🚀 Deploy to Azure
                </button>
                <button class="btn btn-sm" onclick="document.getElementById('tmpl-revision-prompt')?.focus(); document.querySelector('.tmpl-revision-section')?.scrollIntoView({behavior:'smooth'})">
                    📝 Request Changes
                </button>
            </div>
        </div>`;
    }

    document.getElementById('detail-template-name').textContent = tmpl.name;
    document.getElementById('detail-template-body').innerHTML = `
        <div class="detail-meta">
            <span class="svc-id">${escapeHtml(tmpl.id)}</span>
            <span class="tmpl-type-badge tmpl-type-${ttype}">${typeIcons[ttype] || '📋'} ${typeLabels[ttype] || ttype}</span>
            <span class="status-badge ${status}">${statusBadgeMap[status] || status}</span>
            ${activeVer ? `<span class="tmpl-ver-badge">v${activeVer}</span>` : ''}
            <span class="tmpl-standalone-badge ${isStandalone ? 'standalone-yes' : 'standalone-no'}">
                ${isStandalone ? '✅ Standalone' : '🔗 Has dependencies'}
            </span>
        </div>

        ${ctaHtml}

        <!-- Validation form (hidden by default) -->
        <div id="tmpl-validate-form" class="detail-section tmpl-validate-section" style="display:none;">
            <h4>🧪 Validation</h4>
            <p class="tmpl-validate-desc">Validates this template by deploying to a temporary Azure resource group. Self-healing fixes issues automatically. The temp RG is cleaned up afterward.</p>
            <div id="tmpl-validate-params"></div>
            <div class="tmpl-validate-actions">
                <select id="tmpl-validate-region" class="form-control" style="width:auto; display:inline-block; margin-right:0.5rem;">
                    <option value="eastus2">East US 2</option>
                    <option value="eastus">East US</option>
                    <option value="westus2">West US 2</option>
                    <option value="centralus">Central US</option>
                    <option value="westeurope">West Europe</option>
                    <option value="northeurope">North Europe</option>
                </select>
                <button class="btn btn-primary btn-sm" id="tmpl-validate-btn" onclick="runTemplateValidation('${escapeHtml(tmpl.id)}')">
                    🧪 Run Validation
                </button>
            </div>
            <div id="tmpl-validate-results" style="display:none;"></div>
        </div>

        <!-- Deploy form (hidden by default) -->
        <div id="tmpl-deploy-form" class="detail-section tmpl-deploy-section" style="display:none;">
            <h4>🚀 Deploy to Azure</h4>
            <p class="tmpl-deploy-desc">Configure the deployment target and parameter values.</p>
            <div class="tmpl-deploy-field">
                <label class="tmpl-deploy-label">Resource Group <span class="param-required">required</span></label>
                <input type="text" class="form-control" id="tmpl-deploy-rg" placeholder="e.g. my-app-rg" />
            </div>
            <div id="tmpl-deploy-params"></div>
            <div class="tmpl-deploy-controls">
                <select id="tmpl-deploy-region" class="form-control" style="width:auto; display:inline-block; margin-right:0.5rem;">
                    <option value="eastus2">East US 2</option>
                    <option value="eastus">East US</option>
                    <option value="westus2">West US 2</option>
                    <option value="centralus">Central US</option>
                    <option value="westeurope">West Europe</option>
                    <option value="northeurope">North Europe</option>
                </select>
                <button class="btn btn-primary btn-sm" id="tmpl-deploy-btn" onclick="deployTemplate('${escapeHtml(tmpl.id)}')">
                    🚀 Start Deployment
                </button>
            </div>
            <div id="tmpl-deploy-progress" style="display:none;"></div>
        </div>

        <div class="detail-layout">
            <div class="detail-main">
                <div class="detail-section">
                    <h4>Description</h4>
                    <p>${escapeHtml(tmpl.description || 'No description')}</p>
                </div>

                ${provides.length ? `
                <div class="detail-section">
                    <h4>Creates (Provides)</h4>
                    <div class="detail-tags">${provides.map(p => `<span class="tmpl-chip tmpl-chip-provides">${escapeHtml(p)}</span>`).join('')}</div>
                </div>` : ''}

                ${requires.length ? `
                <div class="detail-section">
                    <h4>🔗 Infrastructure Dependencies</h4>
                    <div class="tmpl-dep-list">
                        ${requires.map(r => `
                            <div class="tmpl-dep-item tmpl-dep-required">
                                <strong>${_shortType(r.type || r)}</strong>
                                <span>${escapeHtml(r.reason || '')}</span>
                                <code>${escapeHtml(r.parameter || '')}</code>
                            </div>
                        `).join('')}
                    </div>
                    <p class="tmpl-dep-note">These are automatically wired at deploy time — InfraForge will handle resource selection.</p>
                </div>` : ''}

                ${optionalRefs.length ? `
                <div class="detail-section">
                    <h4>📎 Optional References</h4>
                    <div class="tmpl-dep-list">
                        ${optionalRefs.map(o => `
                            <div class="tmpl-dep-item tmpl-dep-optional">
                                <strong>${_shortType(o.type || o)}</strong>
                                <span>${escapeHtml(o.reason || '')}</span>
                            </div>
                        `).join('')}
                    </div>
                </div>` : ''}

                ${(tmpl.service_ids && tmpl.service_ids.length) ? `
                <div class="detail-section">
                    <h4>Composed From Services</h4>
                    <div class="detail-tags">${tmpl.service_ids.map(s => `<span class="region-tag">${escapeHtml(s)}</span>`).join('')}</div>
                    ${status !== 'approved' ? `<a href="#" class="tmpl-recompose-link" onclick="event.preventDefault(); recomposeBlueprint('${escapeHtml(tmpl.id)}')">
                        🔄 Recompose from latest service versions
                    </a>` : ''}
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

                <!-- Request Changes -->
                <div class="detail-section tmpl-revision-section">
                    <h4>📝 Request Changes</h4>
                    <p class="tmpl-revision-desc">Describe what you want changed and InfraForge will update the template automatically. Changes are policy-checked and create a new version.</p>
                    <div class="tmpl-revision-input-group">
                        <textarea id="tmpl-revision-prompt" class="form-control tmpl-revision-textarea"
                            rows="2"
                            placeholder="e.g. Add a SQL database and Key Vault for secrets management…"
                            onkeydown="if(event.key==='Enter' && !event.shiftKey) { event.preventDefault(); submitRevision('${escapeHtml(tmpl.id)}'); }"></textarea>
                        <button class="btn btn-primary btn-sm" id="tmpl-revision-btn"
                            onclick="submitRevision('${escapeHtml(tmpl.id)}')">
                            ✏️ Submit
                        </button>
                    </div>
                    <div id="tmpl-revision-policy" class="tmpl-revision-policy" style="display:none;"></div>
                    <div id="tmpl-revision-result" class="tmpl-revision-result" style="display:none;"></div>
                </div>

                ${tmpl.content ? `
                <div class="detail-section">
                    <h4>Template Code</h4>
                    <div class="detail-code-wrap">
                        <pre><code>${escapeHtml(tmpl.content)}</code></pre>
                    </div>
                </div>` : ''}
            </div>

            <div class="detail-sidebar">
                <div class="detail-section">
                    <h4>Format & Category</h4>
                    <span class="category-badge">${escapeHtml(tmpl.format || 'arm')}</span>
                    <span class="category-badge">${escapeHtml(tmpl.category || '')}</span>
                </div>

                ${(tmpl.tags && tmpl.tags.length) ? `
                <div class="detail-section">
                    <h4>Tags</h4>
                    <div class="detail-tags">${tmpl.tags.map(t => `<span class="region-tag">${escapeHtml(t)}</span>`).join('')}</div>
                </div>` : ''}

                <!-- Version History — clickable pipeline view -->
                <div class="detail-section">
                    <h4>📋 Version History</h4>
                    <p style="font-size:0.72rem; color:var(--text-muted); margin-bottom:0.5rem;">Click a version to see its lifecycle pipeline.</p>
                    <div id="tmpl-version-history" class="tmpl-version-history">
                        <div class="compose-loading">Loading versions…</div>
                    </div>
                </div>
            </div>
        </div>
    `;

    document.getElementById('template-detail-drawer').classList.remove('hidden');

    // Load version history asynchronously
    _loadTemplateVersionHistory(templateId);
}

/** Infer human-readable change type from version metadata */
function _inferChangeType(createdBy, changelog) {
    if (!createdBy && !changelog) return '';
    const by = (createdBy || '').toLowerCase();
    const cl = (changelog || '').toLowerCase();
    if (by.includes('auto-heal') || by.includes('deployment-agent') || by.includes('deep-heal') || cl.includes('auto-heal'))
        return '🔧 Patch';
    if (by.includes('recompos') || cl.includes('recompos'))
        return '🔄 Major';
    if (by.includes('revision') || by.includes('feedback') || cl.includes('revision') || cl.includes('feedback'))
        return '✏️ Minor';
    if (cl.includes('initial') || cl.includes('prompt compose'))
        return '🆕 Initial';
    return '';
}

/** Load and render version history for a template */
async function _loadTemplateVersionHistory(templateId) {
    const container = document.getElementById('tmpl-version-history');
    if (!container) return;

    try {
        const res = await fetch(`/api/catalog/templates/${encodeURIComponent(templateId)}/versions`);
        if (!res.ok) {
            container.innerHTML = '<div class="compose-empty">No versions found</div>';
            return;
        }
        const data = await res.json();
        const versions = data.versions || [];

        if (!versions.length) {
            container.innerHTML = '<div class="compose-empty">No versions recorded</div>';
            return;
        }

        const statusIcons = { draft: '📝', passed: '🧪', validated: '🔬', failed: '❌', approved: '✅' };

        container.innerHTML = versions.map((v, idx) => {
            const isActive = v.version === data.active_version;
            const semverDisplay = v.semver ? v.semver : `${v.version}.0.0`;
            const changeLabel = _inferChangeType(v.created_by, v.changelog);

            return `
                <div class="tmpl-ver-item ${isActive ? 'tmpl-ver-active' : ''} tmpl-ver-${v.status}"
                     onclick="_toggleVersionPipeline(this, ${idx})" data-ver-idx="${idx}">
                    <div class="tmpl-ver-header">
                        <span class="tmpl-ver-num">${semverDisplay}</span>
                        <span class="tmpl-ver-status">${statusIcons[v.status] || '❓'} ${v.status}</span>
                        ${isActive ? '<span class="tmpl-ver-active-badge">Active</span>' : ''}
                        ${changeLabel ? `<span class="tmpl-ver-change-type">${changeLabel}</span>` : ''}
                        <span class="tmpl-ver-expand-icon">▸</span>
                    </div>
                    ${v.changelog ? `<div class="tmpl-ver-changelog">${escapeHtml(v.changelog)}</div>` : ''}
                    <div class="tmpl-ver-meta">
                        ${v.created_at ? `<span>${v.created_at.substring(0, 16)}</span>` : ''}
                        ${v.created_by ? `<span>By: ${escapeHtml(v.created_by)}</span>` : ''}
                    </div>
                    <div class="ver-pipeline-container" id="ver-pipeline-${idx}" style="display:none;"></div>
                </div>
            `;
        }).join('');

        // Stash version data for pipeline rendering
        container._versionData = versions;
    } catch (err) {
        container.innerHTML = `<div class="compose-empty">Failed to load versions: ${err.message}</div>`;
    }
}

/** Toggle the pipeline visualization for a version item */
function _toggleVersionPipeline(el, idx) {
    const pipelineContainer = document.getElementById(`ver-pipeline-${idx}`);
    if (!pipelineContainer) return;

    const isExpanded = el.classList.contains('ver-expanded');

    // Collapse all others first
    document.querySelectorAll('.tmpl-ver-item.ver-expanded').forEach(item => {
        item.classList.remove('ver-expanded');
        const pc = item.querySelector('.ver-pipeline-container');
        if (pc) pc.style.display = 'none';
    });

    if (isExpanded) return; // Was open, now closed

    // Expand this one
    el.classList.add('ver-expanded');
    pipelineContainer.style.display = 'block';

    // Render pipeline if not already done
    if (!pipelineContainer.dataset.rendered) {
        const container = document.getElementById('tmpl-version-history');
        const versions = container?._versionData || [];
        const v = versions[idx];
        if (v) {
            pipelineContainer.innerHTML = _renderVersionPipeline(v);
            pipelineContainer.dataset.rendered = '1';
        }
    }
}

/** Render a visual pipeline for a version showing each lifecycle stage */
function _renderVersionPipeline(v) {
    const status = v.status || 'draft';
    const testResults = v.test_results || {};
    const tests = testResults.tests || [];
    const valResults = v.validation_results || {};
    const healHistory = valResults.heal_history || [];

    // Determine which stages are completed, active, failed, or pending
    // Pipeline: Compose → Structural Tests → Azure Validation → Published
    const stages = [];

    // Stage 1: Compose — always passed if version exists
    stages.push({
        label: 'Compose',
        icon: '🔨',
        status: 'passed',
        time: v.created_at ? v.created_at.substring(0, 16) : null,
    });

    // Stage 2: Structural Tests
    if (status === 'draft') {
        stages.push({ label: 'Structural Tests', icon: '🧪', status: 'skipped', time: null });
    } else if (tests.length && !testResults.all_passed) {
        stages.push({ label: 'Structural Tests', icon: '🧪', status: 'failed', time: v.tested_at?.substring(0, 16) });
    } else {
        stages.push({ label: 'Structural Tests', icon: '🧪', status: 'passed', time: v.tested_at?.substring(0, 16) });
    }

    // Stage 3: Azure Validation
    if (['draft', 'passed'].includes(status)) {
        stages.push({ label: 'Azure Validation', icon: '☁️', status: status === 'passed' ? 'active' : 'skipped', time: null });
    } else if (status === 'failed') {
        stages.push({ label: 'Azure Validation', icon: '☁️', status: 'failed', time: v.validated_at?.substring(0, 16) });
    } else {
        stages.push({ label: 'Azure Validation', icon: '☁️', status: 'passed', time: v.validated_at?.substring(0, 16) });
    }

    // Stage 4: Published
    if (status === 'approved') {
        stages.push({ label: 'Published', icon: '🚀', status: 'passed', time: null });
    } else if (status === 'validated') {
        stages.push({ label: 'Published', icon: '🚀', status: 'active', time: null });
    } else {
        stages.push({ label: 'Published', icon: '🚀', status: 'skipped', time: null });
    }

    // Build stage nodes with connectors
    let stagesHtml = '';
    stages.forEach((s, i) => {
        if (i > 0) {
            const prevStatus = stages[i - 1].status;
            const connStatus = prevStatus === 'passed' ? 'passed' : prevStatus === 'failed' ? 'failed' : '';
            stagesHtml += `<div class="ver-stage-connector"><div class="connector-line ${connStatus ? 'connector-' + connStatus : ''}"></div></div>`;
        }
        stagesHtml += `
            <div class="ver-stage-node">
                <div class="ver-stage-icon stage-${s.status}">${s.icon}</div>
                <div class="ver-stage-label">${s.label}</div>
                ${s.time ? `<div class="ver-stage-time">${s.time}</div>` : ''}
            </div>`;
    });

    // Build detail cards for failures or test results
    let detailHtml = '';

    // Test results detail
    if (tests.length) {
        const passedCount = tests.filter(t => t.passed).length;
        const failedCount = tests.filter(t => !t.passed).length;
        const isAllPassed = testResults.all_passed;
        const detailType = isAllPassed ? 'detail-success' : 'detail-failed';

        detailHtml += `
            <div class="ver-pipeline-detail ${detailType}">
                <div class="ver-detail-title">
                    ${isAllPassed ? '✅' : '❌'} Structural Tests — ${passedCount} passed${failedCount ? `, ${failedCount} failed` : ''}
                </div>
                <div class="ver-detail-items">
                    ${tests.map(t => `
                        <div class="ver-detail-item">
                            <span class="${t.passed ? 'test-pass' : 'test-fail'}">${t.passed ? '✅' : '❌'}</span>
                            <strong>${escapeHtml(t.name)}</strong>
                            ${t.message && !t.passed ? `<span class="ver-detail-msg">${escapeHtml(t.message)}</span>` : ''}
                        </div>
                    `).join('')}
                </div>
            </div>`;
    }

    // Validation / heal history detail
    if (v.validated_at || status === 'failed') {
        const valPassed = valResults.validation_passed;
        const deepHealed = valResults.deep_healed;
        const detailType = valPassed ? 'detail-success' : 'detail-failed';
        const region = valResults.region || '';
        const rg = valResults.resource_group || '';

        let valTitle = valPassed
            ? (deepHealed ? '🔧 Azure Validation — Passed after self-healing' : '✅ Azure Validation — Passed')
            : '❌ Azure Validation — Failed';

        detailHtml += `
            <div class="ver-pipeline-detail ${detailType}">
                <div class="ver-detail-title">${valTitle}</div>
                ${region || rg ? `<div class="ver-detail-meta">${region ? `Region: ${escapeHtml(region)}` : ''} ${rg ? `· RG: ${escapeHtml(rg)}` : ''}</div>` : ''}
                ${healHistory.length ? `
                <div class="ver-heal-history">
                    <div class="ver-heal-title">🔄 Healing Steps (${healHistory.length})</div>
                    ${healHistory.map((h, i) => `
                        <div class="ver-heal-step">
                            <div class="ver-heal-step-header">
                                <span class="ver-heal-step-num">Step ${h.step || (i + 1)}</span>
                                <span class="ver-heal-phase">${escapeHtml(h.phase || 'deploy')}</span>
                            </div>
                            <div class="ver-heal-error">❌ ${escapeHtml(h.error || 'Unknown error')}</div>
                            <div class="ver-heal-fix">🔧 ${escapeHtml(h.fix_summary || 'Auto-fix applied')}</div>
                        </div>
                    `).join('')}
                </div>` : ''}
            </div>`;
    }

    return `
        <div class="ver-pipeline" onclick="event.stopPropagation()">
            <div class="ver-pipeline-stages">${stagesHtml}</div>
            ${detailHtml || '<div class="ver-pipeline-detail detail-info"><div class="ver-detail-title">ℹ️ No detailed results yet — run validation to see the full pipeline.</div></div>'}
        </div>`;
}

/** Full validation pipeline: structural tests → ARM validation (auto-chains) */
async function runFullValidation(templateId, skipTests = false) {
    if (!skipTests) {
        // Step 1: Run structural tests
        showToast('🧪 Running structural tests…', 'info');
        try {
            const res = await fetch(`/api/catalog/templates/${encodeURIComponent(templateId)}/test`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({}),
            });
            if (!res.ok) {
                const err = await res.json();
                throw new Error(err.detail || 'Test failed');
            }
            const data = await res.json();
            const results = data.results || {};
            if (!results.all_passed) {
                showToast(`❌ ${results.failed} of ${results.total} tests failed`, 'error');
                await loadAllData();
                showTemplateDetail(templateId);
                return;
            }
            showToast(`✅ All ${results.total} structural tests passed`, 'success');
        } catch (err) {
            showToast(`Test error: ${err.message}`, 'error');
            return;
        }
    }

    // Step 2: Open detail and show validate form
    await loadAllData();
    showTemplateDetail(templateId);

    // Let the DOM render before manipulating the validate form
    await new Promise(r => setTimeout(r, 300));

    showValidateForm(templateId);

    // Step 3: Auto-trigger ARM validation
    await new Promise(r => setTimeout(r, 200));
    runTemplateValidation(templateId);
}

/** Show the validation form with parameter inputs */
function showValidateForm(templateId) {
    const tmpl = allTemplates.find(t => t.id === templateId);
    if (!tmpl) return;

    const formSection = document.getElementById('tmpl-validate-form');
    const paramsContainer = document.getElementById('tmpl-validate-params');
    if (!formSection || !paramsContainer) return;

    const params = _parseArmParams(tmpl);
    const requiredParams = params.filter(p => p.required);
    const optionalParams = params.filter(p => !p.required);

    let html = '';
    if (requiredParams.length) {
        html += `<div class="tmpl-deploy-group">
            <div class="tmpl-deploy-group-header">📋 Required Parameters</div>
            ${requiredParams.map(p => _renderParamField(p, 'tmpl-validate')).join('')}
        </div>`;
    }
    if (optionalParams.length) {
        html += `<div class="tmpl-deploy-group tmpl-deploy-group-optional">
            <details>
                <summary class="tmpl-deploy-group-header tmpl-deploy-toggle">
                    ⚙️ Optional (${optionalParams.length}) — auto-filled with defaults
                </summary>
                ${optionalParams.map(p => _renderParamField(p, 'tmpl-validate')).join('')}
            </details>
        </div>`;
    }
    if (!params.length) {
        html = '<div class="tmpl-deploy-hint">No parameters needed — all use defaults.</div>';
    }

    paramsContainer.innerHTML = html;
    formSection.style.display = 'block';
    formSection.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

/** Run ARM validation (streaming NDJSON with self-healing) */
async function runTemplateValidation(templateId) {
    const btn = document.getElementById('tmpl-validate-btn');
    const resultsDiv = document.getElementById('tmpl-validate-results');
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '⏳ Validating…';
    }
    if (resultsDiv) {
        resultsDiv.style.display = 'block';
        resultsDiv.innerHTML = '<div class="compose-loading">🧪 Running validation… This may take 1-5 minutes.</div>';
    }

    showToast('🧪 Running validation…', 'info');

    try {
        // Collect parameter values from form
        const inputs = document.querySelectorAll('.tmpl-validate-input');
        const parameters = {};
        inputs.forEach(input => {
            const name = input.dataset.paramName;
            const type = input.dataset.paramType;
            let val = input.value.trim();
            if (val) {
                if (type === 'int') val = parseInt(val, 10);
                else if (type === 'bool') val = val.toLowerCase() === 'true';
                parameters[name] = val;
            }
        });

        const regionSelect = document.getElementById('tmpl-validate-region');
        const region = regionSelect ? regionSelect.value : 'eastus2';

        const res = await fetch(`/api/catalog/templates/${encodeURIComponent(templateId)}/validate`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ parameters, region }),
        });

        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Validation failed');
        }

        // Read NDJSON stream — reuse _renderDeployProgress for the iteration log
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let finalEvent = null;

        while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });

            const lines = buffer.split('\n');
            buffer = lines.pop(); // keep incomplete line

            for (const line of lines) {
                if (!line.trim()) continue;
                try {
                    const event = JSON.parse(line);
                    finalEvent = event;
                    _renderDeployProgress(resultsDiv, event, 'validate');
                } catch (e) { /* skip malformed */ }
            }
        }

        // Process final buffer
        if (buffer.trim()) {
            try {
                const event = JSON.parse(buffer);
                finalEvent = event;
                _renderDeployProgress(resultsDiv, event, 'validate');
            } catch (e) { /* skip */ }
        }

        if (finalEvent && finalEvent.status === 'succeeded') {
            const resolved = finalEvent.issues_resolved || 0;
            const healMsg = resolved > 0 ? ` (resolved ${resolved} issue${resolved !== 1 ? 's' : ''})` : '';
            showToast(`✅ Template verified${healMsg}! Ready to publish.`, 'success');
        } else if (finalEvent && finalEvent.status === 'failed') {
            showToast(`⚠️ Template could not be fully verified. Review the log for details.`, 'error');
        }

        // Refresh and reopen detail
        await loadAllData();
        showTemplateDetail(templateId);

    } catch (err) {
        showToast(`⚠️ Validation issue: ${err.message}`, 'error');
        if (resultsDiv) {
            resultsDiv.innerHTML = `<div class="tmpl-deploy-diag-msg">⚠️ ${escapeHtml(err.message)}</div>`;
        }
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = '🧪 Run Validation';
        }
    }
}

/** Render validation (What-If) results */
function _renderValidationResults(container, data, passed) {
    if (!container) return;
    const validation = data.validation || {};
    const whatIf = validation.what_if || {};
    const changes = whatIf.changes || [];
    const changeCounts = whatIf.change_counts || {};
    const error = validation.error;

    container.style.display = 'block';
    container.innerHTML = `
        <div class="tmpl-validate-result ${passed ? 'tmpl-validate-pass' : 'tmpl-validate-fail'}">
            <div class="tmpl-validate-header">
                ${passed ? '✅ ARM What-If Validation Passed' : '❌ ARM What-If Validation Failed'}
            </div>
            ${error ? `<div class="tmpl-validate-error-msg">${escapeHtml(error)}</div>` : ''}
            ${Object.keys(changeCounts).length ? `
            <div class="tmpl-validate-counts">
                ${Object.entries(changeCounts).map(([type, count]) => `
                    <span class="tmpl-whatif-chip tmpl-whatif-${type.toLowerCase()}">${type}: ${count}</span>
                `).join('')}
            </div>` : ''}
            ${changes.length ? `
            <div class="tmpl-validate-changes">
                <h5>Resource Changes</h5>
                ${changes.map(c => `
                    <div class="tmpl-whatif-change tmpl-whatif-change-${c.change_type.toLowerCase()}">
                        <span class="tmpl-whatif-type">${c.change_type}</span>
                        <span class="tmpl-whatif-resource">${escapeHtml(c.resource_type)}</span>
                        <span class="tmpl-whatif-name">${escapeHtml(c.resource_name)}</span>
                    </div>
                `).join('')}
            </div>` : ''}
            <div class="tmpl-validate-meta">
                <span>Region: ${escapeHtml(validation.region || '?')}</span>
                <span>RG: ${escapeHtml(validation.resource_group || '?')} (auto-cleaned)</span>
            </div>
        </div>
    `;
}

/** Recompose a blueprint from its latest service templates */
async function recomposeBlueprint(templateId) {
    if (!confirm('Recompose this blueprint from the latest service templates?\n\nThis pulls the current version of each underlying service template, re-merges them, and creates a new major version.')) return;

    showToast('🔄 Pulling latest service template versions…', 'info');

    try {
        const res = await fetch(`/api/catalog/templates/${encodeURIComponent(templateId)}/recompose`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
        });
        const data = await res.json();

        if (!res.ok) {
            showToast(`❌ Recompose failed: ${data.detail || 'Unknown error'}`, 'error');
            return;
        }

        // Build verbose flow summary
        const ver = data.version || {};
        const semver = ver.semver || '?';
        const svcVersions = data.service_versions || [];
        let detail = `✅ Recomposed → v${semver}\n`;
        detail += `${data.resource_count} resources, ${data.parameter_count} params\n`;
        if (svcVersions.length) {
            detail += `\nService templates used:\n`;
            for (const sv of svcVersions) {
                const svVer = sv.semver || (sv.version ? `v${sv.version}` : 'latest');
                detail += `  • ${sv.name || sv.service_id} (${svVer}, ${sv.source})\n`;
            }
        }

        showToast(detail, 'success', 8000);

        // Refresh the detail view
        await loadAllData();
        showTemplateDetail(templateId);
    } catch (err) {
        showToast(`❌ Recompose error: ${err.message}`, 'error');
    }
}

/** Submit a revision request for a template — policy check + LLM-driven recompose */
async function submitRevision(templateId) {
    const textarea = document.getElementById('tmpl-revision-prompt');
    const btn = document.getElementById('tmpl-revision-btn');
    const policyDiv = document.getElementById('tmpl-revision-policy');
    const resultDiv = document.getElementById('tmpl-revision-result');
    if (!textarea || !btn) return;

    const prompt = textarea.value.trim();
    if (!prompt) {
        showToast('Describe what changes you need', 'warning');
        return;
    }

    btn.disabled = true;
    btn.textContent = '⏳ Checking policies…';
    policyDiv.style.display = 'none';
    resultDiv.style.display = 'none';

    try {
        // ── Step 1: Instant policy pre-check ─────────────────
        const policyRes = await fetch(`/api/catalog/templates/${encodeURIComponent(templateId)}/revision/policy-check`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ prompt }),
        });
        const policyData = await policyRes.json();

        // Show policy result
        policyDiv.style.display = 'block';
        if (policyData.verdict === 'block') {
            policyDiv.className = 'tmpl-revision-policy tmpl-policy-block';
            policyDiv.innerHTML = `
                <div class="tmpl-policy-header">🚫 Blocked by Policy</div>
                <div class="tmpl-policy-summary">${escapeHtml(policyData.summary)}</div>
                ${policyData.issues?.length ? `<ul class="tmpl-policy-issues">
                    ${policyData.issues.map(i => `<li class="tmpl-policy-issue-${i.severity}">
                        <strong>${escapeHtml(i.rule)}</strong>: ${escapeHtml(i.message)}
                    </li>`).join('')}
                </ul>` : ''}
                <div class="tmpl-policy-hint">Revise your request to comply with organizational policies.</div>`;
            btn.disabled = false;
            btn.textContent = '✏️ Request Revision';
            return;
        } else if (policyData.verdict === 'warning') {
            policyDiv.className = 'tmpl-revision-policy tmpl-policy-warning';
            policyDiv.innerHTML = `
                <div class="tmpl-policy-header">⚠️ Policy Warnings</div>
                <div class="tmpl-policy-summary">${escapeHtml(policyData.summary)}</div>
                ${policyData.issues?.length ? `<ul class="tmpl-policy-issues">
                    ${policyData.issues.map(i => `<li class="tmpl-policy-issue-${i.severity}">
                        <strong>${escapeHtml(i.rule)}</strong>: ${escapeHtml(i.message)}
                    </li>`).join('')}
                </ul>` : ''}
                <div class="tmpl-policy-hint">Proceeding with revision despite warnings…</div>`;
        } else {
            policyDiv.className = 'tmpl-revision-policy tmpl-policy-pass';
            policyDiv.innerHTML = `<div class="tmpl-policy-header">✅ Policy Check Passed</div>
                <div class="tmpl-policy-summary">${escapeHtml(policyData.summary)}</div>`;
        }

        // ── Step 2: Submit revision ──────────────────────────
        btn.textContent = '⏳ Revising template…';
        resultDiv.style.display = 'block';
        resultDiv.innerHTML = '<div class="tmpl-revision-loading">Analyzing request and recomposing template…</div>';

        const revRes = await fetch(`/api/catalog/templates/${encodeURIComponent(templateId)}/revise`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ prompt, skip_policy_check: true }),
        });
        const revData = await revRes.json();

        if (!revRes.ok) {
            resultDiv.innerHTML = `<div class="tmpl-revision-error">❌ ${escapeHtml(revData.detail || revData.message || 'Revision failed')}</div>`;
            return;
        }

        if (revData.status === 'no_changes') {
            resultDiv.innerHTML = `
                <div class="tmpl-revision-no-change">
                    <div class="tmpl-revision-analysis">${escapeHtml(revData.analysis || '')}</div>
                    <div class="tmpl-revision-hint">ℹ️ ${escapeHtml(revData.message)}</div>
                </div>`;
            return;
        }

        if (revData.status === 'edit_failed') {
            resultDiv.innerHTML = `
                <div class="tmpl-revision-error">❌ ${escapeHtml(revData.message || 'Edit failed')}</div>
                <div class="tmpl-revision-analysis">${escapeHtml(revData.analysis || '')}</div>`;
            return;
        }

        // Show success
        let actionsHtml = '';
        if (revData.actions_taken?.length) {
            actionsHtml = '<div class="tmpl-revision-actions"><strong>Changes made:</strong><ul>' +
                revData.actions_taken.map(a => {
                    const icon = a.action === 'auto_onboarded' ? '🔧' :
                                 a.action === 'added_from_catalog' ? '✅' :
                                 a.action === 'code_edit' ? '✏️' : '❌';
                    return `<li>${icon} <strong>${escapeHtml(a.service_id.split('/').pop())}</strong> — ${escapeHtml(a.detail)}</li>`;
                }).join('') + '</ul></div>';
        }

        resultDiv.innerHTML = `
            <div class="tmpl-revision-success">
                <div class="tmpl-revision-analysis">${escapeHtml(revData.analysis || '')}</div>
                ${actionsHtml}
                <div class="tmpl-revision-summary">
                    ✅ Template revised → <strong>v${revData.version?.semver || '?'}</strong>:
                    <strong>${revData.resource_count}</strong> resources,
                    <strong>${revData.parameter_count}</strong> params from
                    <strong>${revData.services?.length || '?'}</strong> services.
                    <br><em>Starting validation…</em>
                </div>
            </div>`;

        textarea.value = '';
        showToast(`✅ Revised → v${revData.version?.semver || '?'} — starting validation…`, 'success');
        setTimeout(async () => {
            await loadCatalog();
            // Auto-trigger full validation pipeline
            runFullValidation(templateId);
        }, 1500);

    } catch (err) {
        resultDiv.style.display = 'block';
        resultDiv.innerHTML = `<div class="tmpl-revision-error">❌ ${escapeHtml(err.message)}</div>`;
        showToast(`❌ Revision error: ${err.message}`, 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = '✏️ Request Revision';
    }
}

/** Publish a validated template */
async function publishTemplate(templateId) {
    if (!confirm('Publish this template to the catalog? It will be available for all users.')) return;

    showToast('🚀 Publishing template…', 'info');

    try {
        const res = await fetch(`/api/catalog/templates/${encodeURIComponent(templateId)}/publish`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({}),
        });

        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Publish failed');
        }

        const data = await res.json();
        showToast(`🎉 Template published! v${data.published_version} is now active in the catalog.`, 'success');

        await loadAllData();
        showTemplateDetail(templateId);
    } catch (err) {
        showToast(err.message, 'error');
    }
}

/** Parse rich parameter metadata from ARM template content */
function _parseArmParams(tmpl) {
    let armParams = {};
    try {
        const content = typeof tmpl.content === 'string' ? JSON.parse(tmpl.content) : tmpl.content;
        armParams = (content && content.parameters) || {};
    } catch (e) { /* ignore parse errors */ }

    const result = [];
    for (const [name, def] of Object.entries(armParams)) {
        const meta = def.metadata || {};
        const hasDefault = 'defaultValue' in def;
        const defaultVal = hasDefault ? def.defaultValue : undefined;
        const isArmExpression = typeof defaultVal === 'string' && defaultVal.startsWith('[') && defaultVal.endsWith(']');
        const allowedValues = def.allowedValues || null;
        const description = meta.description || '';
        const type = (def.type || 'string').toLowerCase();

        // Skip 'location' — we use the region selector for that
        if (name === 'location') continue;

        // Determine if required (no usable default)
        const required = !hasDefault || isArmExpression;

        // Generate smart default for resource name fields
        let smartDefault = '';
        if (hasDefault && !isArmExpression) {
            smartDefault = String(defaultVal);
        } else if (name.startsWith('resourceName_') || name === 'resourceName') {
            const suffix = name.replace('resourceName_', '').replace('resourceName', 'resource');
            smartDefault = `if-${suffix.substring(0, 20)}`;
        }

        result.push({ name, type, description, required, defaultVal: smartDefault, allowedValues });
    }

    // Sort: required first, then optional
    result.sort((a, b) => (b.required ? 1 : 0) - (a.required ? 1 : 0));
    return result;
}

/** Render a single parameter field (shared by deploy & validate forms) */
function _renderParamField(p, cssPrefix) {
    const { name, type, description, required, defaultVal, allowedValues } = p;

    let inputHtml;
    if (allowedValues && allowedValues.length > 0) {
        // Dropdown
        inputHtml = `
            <select class="form-control ${cssPrefix}-input"
                data-param-name="${escapeHtml(name)}"
                data-param-type="${escapeHtml(type)}">
                ${allowedValues.map(v => `
                    <option value="${escapeHtml(String(v))}" ${String(v) === String(defaultVal) ? 'selected' : ''}>
                        ${escapeHtml(String(v))}
                    </option>
                `).join('')}
            </select>`;
    } else if (type === 'bool') {
        inputHtml = `
            <select class="form-control ${cssPrefix}-input"
                data-param-name="${escapeHtml(name)}"
                data-param-type="bool">
                <option value="true" ${defaultVal === 'true' || defaultVal === true ? 'selected' : ''}>true</option>
                <option value="false" ${defaultVal === 'false' || defaultVal === false ? 'selected' : ''}>false</option>
            </select>`;
    } else {
        inputHtml = `
            <input type="text" class="form-control ${cssPrefix}-input"
                data-param-name="${escapeHtml(name)}"
                data-param-type="${escapeHtml(type)}"
                placeholder="${defaultVal ? escapeHtml(String(defaultVal)) : `Enter ${name}`}"
                value="${defaultVal ? escapeHtml(String(defaultVal)) : ''}" />`;
    }

    return `
        <div class="${cssPrefix}-field ${required ? `${cssPrefix}-field-required` : `${cssPrefix}-field-optional`}">
            <label class="${cssPrefix}-label">
                <span class="param-name">${escapeHtml(name)}</span>
                ${required ? '<span class="param-required">REQUIRED</span>' : '<span class="param-optional">optional</span>'}
            </label>
            ${description ? `<div class="${cssPrefix}-hint">${escapeHtml(description)}</div>` : ''}
            ${inputHtml}
        </div>
    `;
}

/** Show the deploy form for a template */
function showDeployForm(templateId) {
    const tmpl = allTemplates.find(t => t.id === templateId);
    if (!tmpl) return;

    const formSection = document.getElementById('tmpl-deploy-form');
    const paramsContainer = document.getElementById('tmpl-deploy-params');
    if (!formSection || !paramsContainer) return;

    const params = _parseArmParams(tmpl);
    const requiredParams = params.filter(p => p.required);
    const optionalParams = params.filter(p => !p.required);

    let html = '';
    if (requiredParams.length) {
        html += `<div class="tmpl-deploy-group">
            <div class="tmpl-deploy-group-header">📋 Required Parameters</div>
            ${requiredParams.map(p => _renderParamField(p, 'tmpl-deploy')).join('')}
        </div>`;
    }
    if (optionalParams.length) {
        html += `<div class="tmpl-deploy-group tmpl-deploy-group-optional">
            <details>
                <summary class="tmpl-deploy-group-header tmpl-deploy-toggle">
                    ⚙️ Optional Parameters (${optionalParams.length}) — pre-filled with defaults
                </summary>
                ${optionalParams.map(p => _renderParamField(p, 'tmpl-deploy')).join('')}
            </details>
        </div>`;
    }
    if (!params.length) {
        html = '<div class="tmpl-deploy-hint">No parameters needed — this template uses all defaults.</div>';
    }

    paramsContainer.innerHTML = html;
    formSection.style.display = 'block';
    formSection.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

/** Deploy a template to Azure — streaming NDJSON progress */
async function deployTemplate(templateId) {
    const btn = document.getElementById('tmpl-deploy-btn');
    const progressDiv = document.getElementById('tmpl-deploy-progress');
    const rgInput = document.getElementById('tmpl-deploy-rg');

    const resourceGroup = rgInput ? rgInput.value.trim() : '';
    if (!resourceGroup) {
        showToast('Please enter a resource group name', 'error');
        if (rgInput) rgInput.focus();
        return;
    }

    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '⏳ Deploying…';
    }
    if (progressDiv) {
        progressDiv.style.display = 'block';
        progressDiv.innerHTML = '<div class="compose-loading">🚀 Starting deployment… This may take 1-5 minutes.</div>';
    }

    showToast('🚀 Deploying template to Azure…', 'info');

    try {
        // Collect parameter values
        const inputs = document.querySelectorAll('.tmpl-deploy-input');
        const parameters = {};
        inputs.forEach(input => {
            const name = input.dataset.paramName;
            const type = input.dataset.paramType;
            let val = input.value.trim();
            if (val) {
                if (type === 'int') val = parseInt(val, 10);
                else if (type === 'bool') val = val.toLowerCase() === 'true';
                parameters[name] = val;
            }
        });

        const regionSelect = document.getElementById('tmpl-deploy-region');
        const region = regionSelect ? regionSelect.value : 'eastus2';

        const res = await fetch(`/api/catalog/templates/${encodeURIComponent(templateId)}/deploy`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ resource_group: resourceGroup, region, parameters }),
        });

        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Deploy failed');
        }

        // Read NDJSON stream — new agent-mediated event protocol
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let finalResult = null;

        while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });

            const lines = buffer.split('\n');
            buffer = lines.pop(); // keep incomplete line

            for (const line of lines) {
                if (!line.trim()) continue;
                try {
                    const event = JSON.parse(line);
                    _renderDeployAgentEvent(progressDiv, event);
                    if (event.type === 'result') finalResult = event;
                } catch (e) { /* skip malformed */ }
            }
        }

        // Process final buffer
        if (buffer.trim()) {
            try {
                const event = JSON.parse(buffer);
                _renderDeployAgentEvent(progressDiv, event);
                if (event.type === 'result') finalResult = event;
            } catch (e) { /* skip */ }
        }

        if (finalResult && finalResult.status === 'succeeded') {
            showToast(`✅ Deployment succeeded! ${(finalResult.provisioned_resources || []).length} resources provisioned.`, 'success');
        } else if (finalResult && finalResult.status === 'needs_work') {
            showToast('⚠️ Deployment needs attention — see the agent analysis.', 'error');
        }

    } catch (err) {
        showToast(`⚠️ Deployment issue: ${err.message}`, 'error');
        if (progressDiv) {
            progressDiv.innerHTML = `<div class="tmpl-deploy-diag-msg">⚠️ ${escapeHtml(err.message)}</div>`;
        }
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = '🚀 Start Deployment';
        }
    }
}

/**
 * Render agent-mediated deploy events.
 *
 * The deploy endpoint now streams 3 event types:
 *   - status  — real-time progress updates from the deploy engine
 *   - agent   — LLM-interpreted analysis (on failure)
 *   - result  — final outcome (succeeded / needs_work)
 *
 * This replaces the old 20-phase _renderDeployProgress for the deploy flow.
 * The validate flow still uses _renderDeployProgress unchanged.
 */
function _renderDeployAgentEvent(container, event) {
    if (!container) return;
    container.style.display = 'block';

    const type = event.type || '';

    // Clear initial loading message on first real event
    const loadingMsg = container.querySelector('.compose-loading');
    if (loadingMsg) loadingMsg.remove();

    // ── Status: real-time progress updates ──
    if (type === 'status') {
        let statusDiv = container.querySelector('.deploy-agent-status');
        if (!statusDiv) {
            statusDiv = document.createElement('div');
            statusDiv.className = 'deploy-agent-status';
            container.appendChild(statusDiv);
        }

        const message = event.message || '';
        const progress = event.progress || 0;
        const progressPct = Math.round(progress * 100);

        // Append to status log (shows history of phases)
        let logDiv = statusDiv.querySelector('.deploy-agent-log');
        if (!logDiv) {
            logDiv = document.createElement('div');
            logDiv.className = 'deploy-agent-log';
            statusDiv.appendChild(logDiv);
        }

        // Update or add the latest status entry
        let latestEntry = logDiv.querySelector('.deploy-agent-log-latest');
        if (latestEntry) {
            // Move previous "latest" to history
            latestEntry.classList.remove('deploy-agent-log-latest');
            latestEntry.classList.add('deploy-agent-log-history');
        }

        const entry = document.createElement('div');
        entry.className = 'deploy-log-entry deploy-agent-log-latest';
        const icon = progress >= 0.9 ? '✅' : progress > 0 ? '⏳' : '🚀';
        entry.innerHTML = `<span class="deploy-log-icon">${icon}</span> ${escapeHtml(message)}`;
        logDiv.appendChild(entry);

        // Update progress bar
        let barDiv = statusDiv.querySelector('.deploy-agent-bar');
        if (!barDiv && progress > 0) {
            barDiv = document.createElement('div');
            barDiv.className = 'deploy-agent-bar';
            barDiv.innerHTML = '<div class="deploy-agent-bar-fill"></div>';
            statusDiv.appendChild(barDiv);
        }
        if (barDiv) {
            const fill = barDiv.querySelector('.deploy-agent-bar-fill');
            if (fill) fill.style.width = `${progressPct}%`;
        }
        return;
    }

    // ── Agent: deployment agent activity (healing, analysis, retry) ──
    if (type === 'agent') {
        const action = event.action || '';
        const content = event.content || '';

        // Remove the progress bar while agent is working (deploy phase is paused)
        const statusDiv = container.querySelector('.deploy-agent-status');
        if (statusDiv && (action === 'analysis' || action === 'analyzing')) {
            const bar = statusDiv.querySelector('.deploy-agent-bar');
            if (bar) bar.remove();
        }

        // For the full analysis card (after all retries exhausted), use rich rendering
        if (action === 'analysis') {
            const agentDiv = document.createElement('div');
            agentDiv.className = 'deploy-agent-analysis';
            agentDiv.innerHTML = `
                <div class="deploy-agent-analysis-header">
                    <span class="deploy-agent-analysis-icon">🧠</span>
                    <span>Deployment Agent</span>
                </div>
                <div class="deploy-agent-analysis-content">
                    ${renderMarkdown(content)}
                </div>
            `;
            container.appendChild(agentDiv);
            return;
        }

        // For activity messages (healing, healed, retry, saved), show as log entries
        let logDiv = container.querySelector('.deploy-agent-log');
        if (!logDiv) {
            // Create log inside status div if it exists, otherwise create fresh
            const sd = container.querySelector('.deploy-agent-status');
            logDiv = document.createElement('div');
            logDiv.className = 'deploy-agent-log';
            if (sd) {
                sd.insertBefore(logDiv, sd.firstChild);
            } else {
                container.appendChild(logDiv);
            }
        }

        const entry = document.createElement('div');
        const actionClasses = {
            'healing': 'deploy-agent-healing',
            'healed': 'deploy-agent-healed',
            'deep_healed': 'deploy-agent-deep-healed',
            'heal_failed': 'deploy-agent-heal-failed',
            'retry': 'deploy-agent-retry',
            'saved': 'deploy-agent-saved',
            'analyzing': 'deploy-agent-analyzing',
        };
        entry.className = `deploy-log-entry ${actionClasses[action] || ''}`;
        entry.innerHTML = `<span>${renderMarkdown(content)}</span>`;
        logDiv.appendChild(entry);
        return;
    }

    // ── Result: final outcome card ──
    if (type === 'result') {
        // Remove status progress on completion
        const statusDiv = container.querySelector('.deploy-agent-status');
        if (statusDiv) {
            const bar = statusDiv.querySelector('.deploy-agent-bar');
            if (bar) bar.remove();
        }

        const resultDiv = document.createElement('div');

        if (event.status === 'succeeded') {
            const resources = event.provisioned_resources || [];
            const outputs = event.outputs || {};
            const healed = event.healed || false;
            const issuesResolved = event.issues_resolved || 0;
            const healMsg = issuesResolved > 0 ? ` — resolved ${issuesResolved} issue${issuesResolved !== 1 ? 's' : ''}` : '';
            resultDiv.className = 'tmpl-deploy-result tmpl-deploy-success';
            resultDiv.innerHTML = `
                <div class="tmpl-deploy-header">✅ Deployment Succeeded${healMsg}</div>
                ${resources.length ? `
                <div class="tmpl-deploy-resources">
                    <h5>Provisioned Resources (${resources.length})</h5>
                    ${resources.map(r => `
                        <div class="tmpl-deploy-resource">
                            <span class="tmpl-deploy-res-type">${escapeHtml(r.type)}</span>
                            <span class="tmpl-deploy-res-name">${escapeHtml(r.name)}</span>
                        </div>
                    `).join('')}
                </div>` : ''}
                ${Object.keys(outputs).length ? `
                <div class="tmpl-deploy-outputs">
                    <h5>Outputs</h5>
                    ${Object.entries(outputs).map(([k, v]) => `
                        <div class="tmpl-deploy-output">
                            <span class="tmpl-deploy-out-key">${escapeHtml(k)}</span>
                            <code class="tmpl-deploy-out-val">${escapeHtml(String(v))}</code>
                        </div>
                    `).join('')}
                </div>` : ''}
                ${event.deployment_id ? `<div class="tmpl-deploy-meta">Deployment: <code>${escapeHtml(event.deployment_id)}</code></div>` : ''}
            `;
        } else {
            // needs_work — agent analysis is shown above, this is just the footer
            resultDiv.className = 'tmpl-deploy-result tmpl-deploy-needs-work';
            resultDiv.innerHTML = `
                <div class="tmpl-deploy-header">⚠️ Deployment Needs Attention</div>
                <div class="tmpl-deploy-diag-msg">
                    The deployment agent has analyzed the issue — see the analysis above.
                    Consider re-running validation to fix the underlying template.
                </div>
                ${event.deployment_id ? `<div class="tmpl-deploy-meta">Deployment: <code>${escapeHtml(event.deployment_id)}</code></div>` : ''}
            `;
        }
        container.appendChild(resultDiv);
        return;
    }
}

/** Render deployment progress events — accumulates an iteration log.
 *  @param {HTMLElement} container
 *  @param {Object} event  NDJSON event
 *  @param {'validate'|'deploy'} ctx  'validate' = dev iteration, 'deploy' = production deploy
 */
function _renderDeployProgress(container, event, ctx) {
    if (!container) return;
    ctx = ctx || 'deploy';
    const isValidate = ctx === 'validate';
    container.style.display = 'block';

    const phase = event.phase || '';
    const detail = event.detail || '';
    const progress = event.progress || 0;

    // ── Initialize flowchart state on first event ──
    if (!container._vfState) {
        container.innerHTML = '';
        container._vfState = {
            attempts: [],          // { step, status, events[], error, fix, deepHeal? }
            currentAttempt: null,
            seenErrors: {},        // error_code → count (dedup tracking)
            deepHealActive: false,
            finalResult: null,
        };

        // Create the flowchart container structure
        const flowchart = document.createElement('div');
        flowchart.className = 'vf-flowchart';

        // Header stage bar
        flowchart.innerHTML = `
            <div class="vf-stage-bar">
                <div class="vf-stage vf-stage-active" data-vf-stage="deploy">
                    <div class="vf-stage-dot"></div>
                    <span>Deploy</span>
                </div>
                <div class="vf-stage-connector-h"></div>
                <div class="vf-stage" data-vf-stage="analyze">
                    <div class="vf-stage-dot"></div>
                    <span>Analyze</span>
                </div>
                <div class="vf-stage-connector-h"></div>
                <div class="vf-stage" data-vf-stage="fix">
                    <div class="vf-stage-dot"></div>
                    <span>Fix</span>
                </div>
                <div class="vf-stage-connector-h"></div>
                <div class="vf-stage" data-vf-stage="verify">
                    <div class="vf-stage-dot"></div>
                    <span>Verify</span>
                </div>
            </div>
            <div class="vf-timeline"></div>
            <div class="vf-live-progress"></div>
        `;
        container.appendChild(flowchart);
    }

    const state = container._vfState;
    const flowchart = container.querySelector('.vf-flowchart');
    const timeline = flowchart.querySelector('.vf-timeline');
    const liveProgress = flowchart.querySelector('.vf-live-progress');

    // ── Helper: update the stage bar ──
    function _setActiveStage(stageName, status) {
        flowchart.querySelectorAll('.vf-stage').forEach(s => {
            const sn = s.dataset.vfStage;
            s.classList.remove('vf-stage-active', 'vf-stage-done', 'vf-stage-error');
            if (sn === stageName) {
                s.classList.add(status === 'error' ? 'vf-stage-error' : 'vf-stage-active');
            }
        });
        // Mark all stages before current as done
        const order = ['deploy', 'analyze', 'fix', 'verify'];
        const idx = order.indexOf(stageName);
        if (idx > 0) {
            for (let i = 0; i < idx; i++) {
                const prev = flowchart.querySelector(`[data-vf-stage="${order[i]}"]`);
                if (prev) { prev.classList.remove('vf-stage-active'); prev.classList.add('vf-stage-done'); }
            }
        }
    }

    // ── Helper: classify error for dedup ──
    function _errorKey(errMsg) {
        if (!errMsg) return null;
        // Extract Azure error code pattern
        const codeMatch = errMsg.match(/\(([A-Za-z]+)\)/);
        if (codeMatch) return codeMatch[1];
        // Fallback: first 60 chars normalized
        return errMsg.substring(0, 60).replace(/[^a-zA-Z]/g, '').toLowerCase();
    }

    // ── Helper: create an attempt card in the timeline ──
    function _createAttemptCard(attempt) {
        const card = document.createElement('div');
        card.className = 'vf-attempt-card';
        card.id = `vf-attempt-${attempt.step}`;

        const isFirst = attempt.step === 1;
        const label = isFirst ? 'Initial Deployment' : `Re-deploy #${attempt.step - 1}`;

        card.innerHTML = `
            <div class="vf-attempt-header">
                <div class="vf-attempt-num">${label}</div>
                <div class="vf-attempt-status vf-status-running">
                    <span class="vf-status-pulse"></span> Running
                </div>
            </div>
            <div class="vf-attempt-body">
                <div class="vf-attempt-substeps"></div>
            </div>
        `;

        // Add timeline connector if not first
        if (!isFirst) {
            const conn = document.createElement('div');
            conn.className = 'vf-timeline-connector';
            const connLine = document.createElement('div');
            connLine.className = 'vf-connector-line';
            conn.appendChild(connLine);
            timeline.appendChild(conn);
        }

        timeline.appendChild(card);
        return card;
    }

    // ── Helper: add a sub-step inside an attempt card ──
    function _addSubStep(card, icon, text, cssClass) {
        const substeps = card.querySelector('.vf-attempt-substeps');
        const step = document.createElement('div');
        step.className = `vf-substep ${cssClass || ''}`;
        step.innerHTML = `<span class="vf-substep-icon">${icon}</span><span class="vf-substep-text">${text}</span>`;
        substeps.appendChild(step);
        return step;
    }

    // ── Helper: finalize attempt card status ──
    function _finalizeAttempt(card, status) {
        const statusEl = card.querySelector('.vf-attempt-status');
        if (!statusEl) return;
        statusEl.className = `vf-attempt-status vf-status-${status}`;
        const labels = { success: '✅ Passed', error: '❌ Failed', healed: '🔧 Fixed' };
        statusEl.innerHTML = labels[status] || status;
    }

    // ── PHASE HANDLERS ──

    // Starting
    if (phase === 'starting') {
        _setActiveStage('deploy');
        const rg = event.resource_group || '';
        const region = event.region || '';
        const headerInfo = document.createElement('div');
        headerInfo.className = 'vf-header-info';
        headerInfo.innerHTML = `
            <div class="vf-target-info">
                ${rg ? `<span class="vf-tag">RG: ${escapeHtml(rg)}</span>` : ''}
                ${region ? `<span class="vf-tag">Region: ${escapeHtml(region)}</span>` : ''}
                ${event.is_blueprint ? '<span class="vf-tag vf-tag-blueprint">Blueprint</span>' : ''}
            </div>
        `;
        timeline.before(headerInfo);
        return;
    }

    // New attempt step
    if (phase === 'step' || phase === 'attempt_start') {
        const step = event.step || (state.attempts.length + 1);
        const attempt = { step, status: 'running', events: [], error: null, fix: null };
        state.attempts.push(attempt);
        state.currentAttempt = attempt;
        _setActiveStage('deploy');
        const card = _createAttemptCard(attempt);
        _addSubStep(card, '🚀', escapeHtml(detail || 'Deploying to Azure…'), 'vf-substep-deploy');
        // Scroll to bottom
        timeline.scrollTop = timeline.scrollHeight;
        return;
    }

    // Error event
    if (phase === 'error') {
        const card = document.getElementById(`vf-attempt-${state.currentAttempt?.step}`);
        if (!card) return;

        _setActiveStage('analyze', 'error');

        const errMsg = event.error || detail || '';
        const errKey = _errorKey(errMsg);

        // Track dedup
        if (errKey) {
            state.seenErrors[errKey] = (state.seenErrors[errKey] || 0) + 1;
        }

        if (state.currentAttempt) state.currentAttempt.error = errMsg;

        // Show error in the card
        const dupCount = errKey ? state.seenErrors[errKey] : 0;
        const dupBadge = dupCount > 1 ? `<span class="vf-dup-badge" title="This error has occurred ${dupCount} times">×${dupCount}</span>` : '';

        const errStep = _addSubStep(card, '❌', '', 'vf-substep-error');
        errStep.innerHTML = `
            <span class="vf-substep-icon">❌</span>
            <div class="vf-error-detail">
                <div class="vf-error-msg">${escapeHtml(errMsg.substring(0, 250))}${errMsg.length > 250 ? '…' : ''} ${dupBadge}</div>
                ${errMsg.length > 250 ? `<details class="vf-error-full"><summary>Full error</summary><code>${escapeHtml(errMsg)}</code></details>` : ''}
            </div>
        `;
        return;
    }

    // Healing (LLM analyzing)
    if (phase === 'healing') {
        const card = document.getElementById(`vf-attempt-${state.currentAttempt?.step}`);
        if (!card) return;

        _setActiveStage('analyze');
        _finalizeAttempt(card, 'error');

        const isRepeated = event.repeated_error;
        const healMsg = isRepeated
            ? `⚠️ Same error class '${escapeHtml(event.error_code || '')}' recurring — escalating strategy…`
            : (isValidate ? 'Analyzing Azure feedback…' : (detail || 'Analyzing error…'));
        _addSubStep(card, '🧠', healMsg, isRepeated ? 'vf-substep-analyze vf-substep-escalate' : 'vf-substep-analyze');

        if (event.error_summary) {
            const errKey = _errorKey(event.error_summary);
            if (errKey) {
                state.seenErrors[errKey] = (state.seenErrors[errKey] || 0) + 1;
            }
            if (state.currentAttempt) state.currentAttempt.error = event.error_summary;

            const dupCount = errKey ? state.seenErrors[errKey] : 0;
            const dupBadge = dupCount > 1 ? `<span class="vf-dup-badge" title="Seen ${dupCount} times">×${dupCount} same class</span>` : '';
            _addSubStep(card, '📋', `<code>${escapeHtml(event.error_summary.substring(0, 200))}</code> ${dupBadge}`, 'vf-substep-diagnostic');
        }
        return;
    }

    // Healed (fix applied)
    if (phase === 'healed') {
        const card = document.getElementById(`vf-attempt-${state.currentAttempt?.step}`);
        if (!card) return;

        _setActiveStage('fix');

        const fixMsg = event.fix_summary || detail || 'Fix applied';
        if (state.currentAttempt) state.currentAttempt.fix = fixMsg;

        const deepFlag = event.deep_healed ? '<span class="vf-deep-badge">Deep Fix</span>' : '';
        _addSubStep(card, '🔧', `${escapeHtml(fixMsg)} ${deepFlag}`, 'vf-substep-fix');
        _finalizeAttempt(card, 'healed');

        timeline.scrollTop = timeline.scrollHeight;
        return;
    }

    // ── Deep healing events — render as a sub-flow inside the current attempt ──
    if (phase.startsWith('deep_heal_')) {
        const card = document.getElementById(`vf-attempt-${state.currentAttempt?.step}`);
        if (!card) return;

        if (phase === 'deep_heal_trigger') {
            state.deepHealActive = true;
            _setActiveStage('analyze');

            // Create a deep heal sub-flow card
            const dhContainer = document.createElement('div');
            dhContainer.className = 'vf-deep-heal-flow';
            dhContainer.id = 'vf-deep-heal-active';
            dhContainer.innerHTML = `
                <div class="vf-deep-header">
                    <span class="vf-deep-icon">🔬</span>
                    <span class="vf-deep-title">Deep Analysis</span>
                    <span class="vf-deep-desc">Examining underlying service templates</span>
                </div>
                ${event.service_ids?.length ? `
                <div class="vf-deep-services">
                    ${event.service_ids.map(s => `<span class="vf-tag vf-tag-service">${escapeHtml(s.split('/').pop())}</span>`).join('')}
                </div>` : ''}
                <div class="vf-deep-steps"></div>
            `;
            card.querySelector('.vf-attempt-body').appendChild(dhContainer);
            return;
        }

        const dhFlow = document.getElementById('vf-deep-heal-active');
        const dhSteps = dhFlow?.querySelector('.vf-deep-steps');
        if (!dhSteps) return;

        const deepIcons = {
            deep_heal_start: '🔍', deep_heal_identified: '🎯',
            deep_heal_fix: '🛠️', deep_heal_fix_error: '⚠️',
            deep_heal_validate: '🧪', deep_heal_validate_fail: '🔄',
            deep_heal_validated: '✅', deep_heal_version: '💾',
            deep_heal_versioned: '📦', deep_heal_promoted: '🏷️',
            deep_heal_recompose: '🔧', deep_heal_complete: '🎉',
            deep_heal_fail: '❌', deep_heal_fallback: '↩️',
        };
        const icon = deepIcons[phase] || '•';
        const isSuccess = phase === 'deep_heal_complete' || phase === 'deep_heal_validated';
        const isFail = phase === 'deep_heal_fail' || phase === 'deep_heal_fix_error' || phase === 'deep_heal_validate_fail';
        const cssClass = isSuccess ? 'vf-deep-step-success' : (isFail ? 'vf-deep-step-error' : '');

        const step = document.createElement('div');
        step.className = `vf-deep-step ${cssClass}`;
        step.innerHTML = `<span class="vf-deep-step-icon">${icon}</span> ${escapeHtml(detail)}`;
        dhSteps.appendChild(step);

        if (phase === 'deep_heal_complete') {
            dhFlow.classList.add('vf-deep-success');
            state.deepHealActive = false;
        } else if (phase === 'deep_heal_fail') {
            dhFlow.classList.add('vf-deep-failed');
            state.deepHealActive = false;
        }

        timeline.scrollTop = timeline.scrollHeight;
        return;
    }

    // ── Final result (success or failure) ──
    if (phase === 'complete' || phase === 'done') {
        const resources = event.provisioned_resources || [];
        const outputs = event.outputs || {};
        const healHistory = event.heal_history || [];
        const issuesResolved = event.issues_resolved || 0;

        // Remove live progress
        liveProgress.innerHTML = '';

        // Update stage bar — all stages done or final state
        if (event.status === 'succeeded') {
            _setActiveStage('verify');
            flowchart.querySelectorAll('.vf-stage').forEach(s => {
                s.classList.remove('vf-stage-active', 'vf-stage-error');
                s.classList.add('vf-stage-done');
            });
        } else {
            _setActiveStage('verify', 'error');
        }

        // Finalize last attempt card
        const lastCard = document.getElementById(`vf-attempt-${state.currentAttempt?.step}`);
        if (lastCard) {
            _finalizeAttempt(lastCard, event.status === 'succeeded' ? 'success' : 'error');
        }

        // Build final result card
        const resultDiv = document.createElement('div');
        if (event.status === 'succeeded') {
            const healMsg = issuesResolved > 0 ? ` — resolved ${issuesResolved} issue${issuesResolved !== 1 ? 's' : ''} via self-healing` : '';
            resultDiv.className = 'vf-result vf-result-success';
            resultDiv.innerHTML = `
                <div class="vf-result-header">
                    <span class="vf-result-icon">✅</span>
                    <span>${isValidate ? `Template Verified${healMsg}` : 'Deployment Succeeded'}</span>
                </div>
                ${resources.length ? `
                <div class="vf-result-section">
                    <div class="vf-result-label">Resources Provisioned (${resources.length})</div>
                    <div class="vf-resource-list">
                        ${resources.map(r => `
                            <div class="vf-resource-item">
                                <span class="vf-resource-type">${escapeHtml(r.type)}</span>
                                <span class="vf-resource-name">${escapeHtml(r.name)}</span>
                            </div>
                        `).join('')}
                    </div>
                </div>` : ''}
                ${Object.keys(outputs).length ? `
                <div class="vf-result-section">
                    <div class="vf-result-label">Outputs</div>
                    ${Object.entries(outputs).map(([k, v]) => `
                        <div class="vf-output-item">
                            <span class="vf-output-key">${escapeHtml(k)}</span>
                            <code class="vf-output-val">${escapeHtml(String(v))}</code>
                        </div>
                    `).join('')}
                </div>` : ''}
                ${event.deployment_id ? `<div class="vf-result-meta">Deployment: <code>${escapeHtml(event.deployment_id)}</code></div>` : ''}
            `;
        } else {
            // Build dedup summary of errors seen
            const uniqueErrors = Object.entries(state.seenErrors);
            const dedupHtml = uniqueErrors.length > 1 ? `
                <div class="vf-error-dedup">
                    <div class="vf-dedup-title">Error Pattern Analysis</div>
                    <div class="vf-dedup-list">
                        ${uniqueErrors.map(([code, count]) => `
                            <div class="vf-dedup-item ${count > 1 ? 'vf-dedup-repeated' : ''}">
                                <span class="vf-dedup-code">${escapeHtml(code)}</span>
                                <span class="vf-dedup-count">${count}×</span>
                                ${count > 1 ? '<span class="vf-dedup-flag">⚠️ Repeated</span>' : ''}
                            </div>
                        `).join('')}
                    </div>
                </div>` : '';

            resultDiv.className = 'vf-result vf-result-fail';
            resultDiv.innerHTML = `
                <div class="vf-result-header">
                    <span class="vf-result-icon">${isValidate ? '🔧' : '⚠️'}</span>
                    <span>${isValidate ? 'Template Needs More Work' : 'Deployment Issue'}</span>
                </div>
                <div class="vf-result-body">
                    ${isValidate
                        ? '<p>The self-healing pipeline couldn\'t resolve all issues. Review the flow above for details.</p>'
                        : '<p>The deployment could not be completed. This template may need re-validation.</p>'}
                    ${event.error ? `
                    <details class="vf-error-details" open>
                        <summary>Last diagnostic</summary>
                        <code>${escapeHtml(event.error)}</code>
                    </details>` : ''}
                    ${dedupHtml}
                    ${healHistory.length ? `
                    <details class="vf-heal-summary">
                        <summary>🔄 ${healHistory.length} fix${healHistory.length !== 1 ? 'es' : ''} attempted</summary>
                        <div class="vf-heal-list">
                            ${healHistory.map(h => `
                                <div class="vf-heal-entry">
                                    <div class="vf-heal-num">Step ${h.step || '?'}</div>
                                    <div class="vf-heal-error">❌ ${escapeHtml(h.error || '')}</div>
                                    <div class="vf-heal-fix">🔧 ${escapeHtml(h.fix_summary || '')}</div>
                                </div>
                            `).join('')}
                        </div>
                    </details>` : ''}
                </div>
                ${event.deployment_id ? `<div class="vf-result-meta">Deployment: <code>${escapeHtml(event.deployment_id)}</code></div>` : ''}
            `;
        }
        timeline.appendChild(resultDiv);
        timeline.scrollTop = timeline.scrollHeight;

        state.finalResult = event;
        return;
    }

    // ── Cleanup events ──
    if (phase === 'cleanup' || phase === 'cleanup_done' || phase === 'cleanup_warning') {
        const cleanupEl = document.createElement('div');
        const icon = phase === 'cleanup_done' ? '✅' : (phase === 'cleanup_warning' ? '⚠️' : '🧹');
        cleanupEl.className = 'vf-cleanup';
        cleanupEl.innerHTML = `${icon} ${escapeHtml(detail)}`;
        timeline.appendChild(cleanupEl);
        return;
    }

    // ── Live progress (overwrite — resource provisioning, validating, etc) ──
    const pct = Math.round(progress * 100);
    const phaseIcons = {
        starting: '🚀', resource_group: '📁', validating: '🔍',
        validated: '✅', deploying: '⚙️', provisioning: '📦',
    };
    const icon = phaseIcons[phase] || '⏳';
    liveProgress.innerHTML = `
        <div class="vf-progress-bar">
            <div class="vf-progress-fill" style="width: ${pct}%"></div>
        </div>
        <div class="vf-progress-phase">${icon} ${escapeHtml(detail || phase)}</div>
        ${event.resources ? `
        <div class="vf-resource-chips">
            ${event.resources.map(r => `
                <span class="vf-res-chip vf-res-${r.state.toLowerCase()}">
                    ${r.state === 'Succeeded' ? '✅' : r.state === 'Running' ? '⏳' : '⏸️'} ${escapeHtml(r.name)}
                </span>
            `).join('')}
        </div>` : ''}
    `;
}

/** Auto-heal a failed template — system fixes it, not the user */
async function autoHealTemplate(templateId) {
    showToast('🔧 Auto-healing template…', 'info');

    try {
        const res = await fetch(`/api/catalog/templates/${encodeURIComponent(templateId)}/auto-heal`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({}),
        });

        const data = await res.json();

        if (!res.ok) {
            throw new Error(data.detail || 'Auto-heal failed');
        }

        if (data.status === 'no_issues') {
            showToast('ℹ️ No issues found — template looks fine', 'info');
        } else if (data.all_passed) {
            showToast(`✅ Template auto-healed — all ${data.retest?.total || ''} tests pass! Starting validation…`, 'success');
            // Auto-chain to ARM validation after successful heal
            await loadAllData();
            showTemplateDetail(templateId);
            await new Promise(r => setTimeout(r, 300));
            showValidateForm(templateId);
            await new Promise(r => setTimeout(r, 200));
            runTemplateValidation(templateId);
            return;
        } else {
            showToast(`🔧 Partial fix — ${data.retest?.passed || 0}/${data.retest?.total || 0} tests pass now. Try Request Revision for remaining issues.`, 'warning');
        }

        await loadAllData();
        showTemplateDetail(templateId);
    } catch (err) {
        showToast(`Auto-heal error: ${err.message}`, 'error');
    }
}

/** Run tests on a template from the detail drawer */
async function runTemplateTest(templateId) {
    showToast('🧪 Running template tests…', 'info');

    try {
        const res = await fetch(`/api/catalog/templates/${encodeURIComponent(templateId)}/test`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({}),
        });

        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Test failed');
        }

        const data = await res.json();
        const results = data.results || {};

        if (results.all_passed) {
            showToast(`✅ All ${results.total} tests passed — starting validation…`, 'success');
            // Auto-chain to ARM validation
            await loadAllData();
            showTemplateDetail(templateId);
            await new Promise(r => setTimeout(r, 300));
            showValidateForm(templateId);
            await new Promise(r => setTimeout(r, 200));
            runTemplateValidation(templateId);
            return;
        } else {
            showToast(`❌ ${results.failed} of ${results.total} tests failed`, 'error');
        }

        // Refresh data and re-open detail
        await loadAllData();
        showTemplateDetail(templateId);
    } catch (err) {
        showToast(err.message, 'error');
    }
}

function closeTemplateDetail() {
    const overlay = document.getElementById('template-detail-drawer');
    overlay.classList.add('hidden');
    // Scroll panel body to top for next open
    const body = overlay.querySelector('.detail-panel-body');
    if (body) body.scrollTop = 0;
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

// ── Template Composition from Approved Services ─────────────

let _approvedServicesForCompose = [];
let _composeSelections = new Map(); // service_id -> { quantity, parameters: Set, version: number|null }

async function openTemplateOnboarding() {
    document.getElementById('modal-template-onboard').classList.remove('hidden');
    _composeSelections.clear();
    _updateComposeSubmitButton();

    // Reset prompt tab state
    switchComposeTab('prompt');
    const promptInput = document.getElementById('compose-prompt-input');
    if (promptInput) promptInput.value = '';
    const promptPolicy = document.getElementById('compose-prompt-policy');
    if (promptPolicy) { promptPolicy.style.display = 'none'; promptPolicy.innerHTML = ''; }
    const promptResult = document.getElementById('compose-prompt-result');
    if (promptResult) { promptResult.style.display = 'none'; promptResult.innerHTML = ''; }
    const promptBtn = document.getElementById('btn-prompt-compose');
    if (promptBtn) { promptBtn.disabled = false; promptBtn.textContent = '🚀 Create Template'; }

    const list = document.getElementById('compose-service-list');
    list.innerHTML = '<div class="compose-loading">Loading approved services…</div>';

    try {
        const res = await fetch('/api/catalog/services/approved-for-templates');
        if (!res.ok) {
            const errText = await res.text();
            list.innerHTML = `<div class="compose-empty">Failed to load approved services (${res.status}): ${escapeHtml(errText.slice(0, 200))}</div>`;
            return;
        }
        const data = await res.json();
        _approvedServicesForCompose = data.services || [];
        _renderComposeServiceList(_approvedServicesForCompose);
    } catch (err) {
        list.innerHTML = `<div class="compose-empty">Failed to load: ${err.message}</div>`;
    }
}

function filterComposeServices() {
    const q = (document.getElementById('compose-service-search')?.value || '').toLowerCase();
    const filtered = _approvedServicesForCompose.filter(s =>
        s.name.toLowerCase().includes(q) ||
        s.id.toLowerCase().includes(q) ||
        (s.category || '').toLowerCase().includes(q)
    );
    _renderComposeServiceList(filtered);
}

function _renderComposeServiceList(services) {
    const list = document.getElementById('compose-service-list');
    if (!services.length) {
        list.innerHTML = '<div class="compose-empty">No approved services found. Onboard services first in the Service Catalog.</div>';
        return;
    }

    list.innerHTML = services.map(svc => {
        const selected = _composeSelections.has(svc.id);
        const sel = _composeSelections.get(svc.id);
        const chosenVer = sel ? sel.version : svc.active_version;
        const versions = svc.versions || [];
        const extraParams = svc.parameters.filter(p => !p.is_standard);
        return `
        <div class="compose-svc-card ${selected ? 'compose-svc-selected' : ''}"
             data-service-id="${escapeHtml(svc.id)}">
            <div class="compose-svc-card-main" onclick="toggleComposeService('${escapeHtml(svc.id)}')">
                <div class="compose-svc-check">${selected ? '☑' : '☐'}</div>
                <div class="compose-svc-info">
                    <div class="compose-svc-name">${escapeHtml(svc.name)}</div>
                    <div class="compose-svc-id">${escapeHtml(svc.id)}</div>
                </div>
                <span class="category-badge">${escapeHtml(svc.category)}</span>
                ${extraParams.length ? `<span class="compose-param-count">${extraParams.length} param${extraParams.length !== 1 ? 's' : ''}</span>` : ''}
            </div>
            ${versions.length > 1 ? `
            <div class="compose-version-picker" onclick="event.stopPropagation()">
                <label class="compose-version-label">Version:</label>
                <select class="compose-version-select" onchange="changeComposeVersion('${escapeHtml(svc.id)}', this.value)">
                    ${versions.map(v => {
                        const label = 'v' + v.version + (v.semver ? ' (' + v.semver + ')' : '')
                            + (v.is_active ? ' — active' : '')
                            + (v.status === 'draft' ? ' [draft]' : '');
                        const isSelected = v.version === chosenVer;
                        return `<option value="${v.version}" ${isSelected ? 'selected' : ''}>${escapeHtml(label)}</option>`;
                    }).join('')}
                </select>
            </div>` : `
            <div class="compose-version-picker">
                <span class="version-badge version-active">v${svc.active_version || '?'}</span>
            </div>`}
        </div>`;
    }).join('');
}

function toggleComposeService(serviceId) {
    if (_composeSelections.has(serviceId)) {
        _composeSelections.delete(serviceId);
    } else {
        const svc = _approvedServicesForCompose.find(s => s.id === serviceId);
        const initVersion = svc ? svc.active_version : null;
        _composeSelections.set(serviceId, { quantity: 1, parameters: new Set(), version: initVersion });
    }
    _renderComposeServiceList(
        _approvedServicesForCompose.filter(s => {
            const q = (document.getElementById('compose-service-search')?.value || '').toLowerCase();
            return s.name.toLowerCase().includes(q) || s.id.toLowerCase().includes(q) || (s.category || '').toLowerCase().includes(q);
        })
    );
    _renderComposeSelections();
    _updateComposeSubmitButton();
    _runComposeDependencyAnalysis();
}

function changeComposeVersion(serviceId, versionStr) {
    const ver = parseInt(versionStr, 10);
    const sel = _composeSelections.get(serviceId);
    if (sel) {
        sel.version = ver;
        sel.parameters.clear(); // reset params since different version may have different params
    } else {
        _composeSelections.set(serviceId, { quantity: 1, parameters: new Set(), version: ver });
    }
    // Re-render the selection detail cards with the new version's parameters
    _renderComposeServiceList(
        _approvedServicesForCompose.filter(s => {
            const q = (document.getElementById('compose-service-search')?.value || '').toLowerCase();
            return s.name.toLowerCase().includes(q) || s.id.toLowerCase().includes(q) || (s.category || '').toLowerCase().includes(q);
        })
    );
    _renderComposeSelections();
    _updateComposeSubmitButton();
}

function _renderComposeSelections() {
    const section = document.getElementById('compose-selections-section');
    const container = document.getElementById('compose-selections');

    if (_composeSelections.size === 0) {
        section.style.display = 'none';
        container.innerHTML = '';
        return;
    }

    section.style.display = 'block';

    container.innerHTML = Array.from(_composeSelections.entries()).map(([sid, sel]) => {
        const svc = _approvedServicesForCompose.find(s => s.id === sid);
        if (!svc) return '';
        const versions = svc.versions || [];
        // Get params for the chosen version (fall back to active/top-level)
        const chosenVer = sel.version;
        const verObj = versions.find(v => v.version === chosenVer);
        const verParams = verObj ? verObj.parameters : svc.parameters;
        const extraParams = verParams.filter(p => !p.is_standard);
        const verLabel = chosenVer != null ? `v${chosenVer}` : 'latest';

        return `
        <div class="compose-selection-card">
            <div class="compose-selection-header">
                <div class="compose-selection-title">
                    <span class="compose-svc-name">${escapeHtml(svc.name)}</span>
                    <span class="version-badge ${verObj && verObj.status === 'draft' ? 'version-draft' : 'version-active'}">${verLabel}</span>
                    <button type="button" class="btn btn-xs btn-ghost" onclick="toggleComposeService('${escapeHtml(sid)}')" title="Remove">✕</button>
                </div>
                <div class="compose-selection-controls">
                    ${versions.length > 1 ? `
                    <div class="compose-ver-row">
                        <label>Version:</label>
                        <select class="compose-version-select" onchange="changeComposeVersion('${escapeHtml(sid)}', this.value)">
                            ${versions.map(v => {
                                const label = 'v' + v.version + (v.semver ? ' (' + v.semver + ')' : '')
                                    + (v.is_active ? ' — active' : '')
                                    + (v.status === 'draft' ? ' [draft]' : '');
                                return `<option value="${v.version}" ${v.version === chosenVer ? 'selected' : ''}>${escapeHtml(label)}</option>`;
                            }).join('')}
                        </select>
                    </div>` : ''}
                    <div class="compose-qty-row">
                        <label>Quantity:</label>
                        <button type="button" class="compose-qty-btn" onclick="adjustComposeQty('${escapeHtml(sid)}', -1)">−</button>
                        <span class="compose-qty-val" id="compose-qty-${sid.replace(/[/.]/g, '-')}">${sel.quantity}</span>
                        <button type="button" class="compose-qty-btn" onclick="adjustComposeQty('${escapeHtml(sid)}', 1)">+</button>
                    </div>
                </div>
            </div>
            ${extraParams.length ? `
            <div class="compose-params">
                <div class="compose-params-label">Parameters to expose in template:</div>
                <div class="compose-params-grid">
                    ${extraParams.map(p => {
                        const checked = sel.parameters.has(p.name);
                        return `
                        <label class="compose-param-item ${checked ? 'compose-param-checked' : ''}"
                               title="${escapeHtml(p.description || '')}">
                            <input type="checkbox" ${checked ? 'checked' : ''}
                                   onchange="toggleComposeParam('${escapeHtml(sid)}', '${escapeHtml(p.name)}', this.checked)" />
                            <span class="compose-param-name">${escapeHtml(p.name)}</span>
                            <span class="compose-param-type">${escapeHtml(p.type)}</span>
                            ${p.defaultValue !== undefined ? `<span class="compose-param-default">= ${escapeHtml(String(p.defaultValue))}</span>` : ''}
                        </label>`;
                    }).join('')}
                </div>
            </div>` : '<div class="compose-no-params">No additional parameters — uses standard parameters only</div>'}
        </div>`;
    }).join('');
}

function adjustComposeQty(serviceId, delta) {
    const sel = _composeSelections.get(serviceId);
    if (!sel) return;
    sel.quantity = Math.max(1, Math.min(10, sel.quantity + delta));
    const el = document.getElementById(`compose-qty-${serviceId.replace(/[/.]/g, '-')}`);
    if (el) el.textContent = sel.quantity;
}

function toggleComposeParam(serviceId, paramName, checked) {
    const sel = _composeSelections.get(serviceId);
    if (!sel) return;
    if (checked) {
        sel.parameters.add(paramName);
    } else {
        sel.parameters.delete(paramName);
    }
    _renderComposeSelections();
}

function _updateComposeSubmitButton() {
    const btn = document.getElementById('btn-submit-template');
    if (btn) {
        btn.disabled = _composeSelections.size === 0;
        const count = _composeSelections.size;
        btn.textContent = count > 0
            ? `Create & Test (${count} service${count !== 1 ? 's' : ''})`
            : 'Create & Test Template';
    }
}

/** Live dependency analysis — called whenever compose selections change */
async function _runComposeDependencyAnalysis() {
    const section = document.getElementById('compose-dep-analysis-section');
    const container = document.getElementById('compose-dep-analysis');
    if (!section || !container) return;

    if (_composeSelections.size === 0) {
        section.style.display = 'none';
        container.innerHTML = '';
        return;
    }

    section.style.display = 'block';
    container.innerHTML = '<div class="compose-loading">Analyzing dependencies…</div>';

    const serviceIds = Array.from(_composeSelections.keys());

    try {
        const res = await fetch('/api/templates/analyze-dependencies', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ service_ids: serviceIds }),
        });
        const analysis = await res.json();
        const typeIcons = { foundation: '🏗️', workload: '⚙️', composite: '📦' };
        const typeLabels = { foundation: 'Foundation — deploys standalone', workload: 'Workload — dependencies auto-wired at deploy', composite: 'Composite — self-contained bundle' };

        let html = `
            <div class="dep-type-banner dep-type-${analysis.template_type}">
                ${typeIcons[analysis.template_type] || '📋'}
                Template Type: <strong>${analysis.template_type}</strong>
                — ${typeLabels[analysis.template_type] || ''}
            </div>
        `;

        if (analysis.provides?.length) {
            html += '<div class="dep-block"><h5>✅ Creates (Provides)</h5><div class="dep-chips">';
            analysis.provides.forEach(p => { html += `<span class="tmpl-chip tmpl-chip-provides">${_shortType(p)}</span>`; });
            html += '</div></div>';
        }

        if (analysis.auto_created?.length) {
            html += '<div class="dep-block"><h5>🔧 Auto-Created Supporting Resources</h5>';
            analysis.auto_created.forEach(a => {
                html += `<div class="dep-detail-item dep-auto"><code>${_shortType(a.type)}</code> — ${escapeHtml(a.reason)}</div>`;
            });
            html += '</div>';
        }

        if (analysis.requires?.length) {
            html += '<div class="dep-block"><h5>🔗 Infrastructure Dependencies</h5>';
            html += '<p class="dep-note">These are automatically wired at deploy time — no action needed.</p>';
            analysis.requires.forEach(r => {
                html += `<div class="dep-detail-item dep-required"><code>${escapeHtml(r.type)}</code> — ${escapeHtml(r.reason)}</div>`;
            });
            html += '</div>';
        }

        if (analysis.optional_refs?.length) {
            html += '<div class="dep-block"><h5>📎 Optional References</h5>';
            analysis.optional_refs.forEach(o => {
                html += `<div class="dep-detail-item dep-optional"><code>${_shortType(o.type)}</code> — ${escapeHtml(o.reason)}</div>`;
            });
            html += '</div>';
        }

        if (analysis.deployable_standalone) {
            html += '<div class="dep-standalone-ok">✅ This template can be deployed standalone — no existing infrastructure required.</div>';
        } else {
            html += '<div class="dep-standalone-no">🔗 This template has infrastructure dependencies — InfraForge wires them automatically at deploy time.</div>';
        }

        container.innerHTML = html;
    } catch (err) {
        container.innerHTML = `<div class="compose-empty">Dependency analysis unavailable: ${err.message}</div>`;
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

function showToast(message, type = 'success', duration = 3000) {
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    // Support multi-line messages with whitespace preservation
    toast.style.whiteSpace = 'pre-line';
    toast.textContent = message;
    document.body.appendChild(toast);
    setTimeout(() => toast.remove(), duration);
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

/* ──── Compose Tab Switcher ──────────────────────────── */
function switchComposeTab(tab) {
    const promptPanel = document.getElementById('compose-panel-prompt');
    const manualPanel = document.getElementById('compose-panel-manual');
    const tabPrompt = document.getElementById('compose-tab-prompt');
    const tabManual = document.getElementById('compose-tab-manual');
    if (!promptPanel || !manualPanel) return;

    if (tab === 'prompt') {
        promptPanel.style.display = '';
        manualPanel.style.display = 'none';
        tabPrompt.classList.add('compose-tab-active');
        tabManual.classList.remove('compose-tab-active');
    } else {
        promptPanel.style.display = 'none';
        manualPanel.style.display = '';
        tabPrompt.classList.remove('compose-tab-active');
        tabManual.classList.add('compose-tab-active');
    }
}

/* ──── Prompt-Driven Compose ────────────────────────── */
async function submitPromptCompose() {
    const textarea = document.getElementById('compose-prompt-input');
    const btn = document.getElementById('btn-prompt-compose');
    const policyDiv = document.getElementById('compose-prompt-policy');
    const resultDiv = document.getElementById('compose-prompt-result');
    if (!textarea || !btn) return;

    const prompt = textarea.value.trim();
    if (!prompt) {
        showToast('Describe the infrastructure you need', 'warning');
        return;
    }

    btn.disabled = true;
    btn.textContent = '⏳ Checking policies…';
    policyDiv.style.display = 'none';
    resultDiv.style.display = 'none';

    try {
        // ── Step 1: Policy pre-check via a lightweight POST ──
        // We reuse the compose-from-prompt endpoint but show incremental feedback
        btn.textContent = '⏳ Analyzing services…';
        resultDiv.style.display = 'block';
        resultDiv.innerHTML = '<div class="tmpl-revision-loading">Identifying services, checking policies, resolving dependencies…</div>';

        const res = await fetch('/api/catalog/templates/compose-from-prompt', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ prompt }),
        });
        const data = await res.json();

        // Show policy result if present
        if (data.policy_check) {
            policyDiv.style.display = 'block';
            const pr = data.policy_check;
            if (pr.verdict === 'block') {
                policyDiv.className = 'tmpl-revision-policy tmpl-policy-block';
                policyDiv.innerHTML = `
                    <div class="tmpl-policy-header">🚫 Blocked by Policy</div>
                    <div class="tmpl-policy-summary">${escapeHtml(pr.summary)}</div>
                    ${pr.issues?.length ? `<ul class="tmpl-policy-issues">
                        ${pr.issues.map(i => `<li class="tmpl-policy-issue-${i.severity}">
                            <strong>${escapeHtml(i.rule)}</strong>: ${escapeHtml(i.message)}
                        </li>`).join('')}
                    </ul>` : ''}
                    <div class="tmpl-policy-hint">Revise your request to comply with organizational policies.</div>`;
                resultDiv.style.display = 'none';
                return;
            } else if (pr.verdict === 'warning') {
                policyDiv.className = 'tmpl-revision-policy tmpl-policy-warning';
                policyDiv.innerHTML = `
                    <div class="tmpl-policy-header">⚠️ Policy Warnings</div>
                    <div class="tmpl-policy-summary">${escapeHtml(pr.summary)}</div>
                    ${pr.issues?.length ? `<ul class="tmpl-policy-issues">
                        ${pr.issues.map(i => `<li class="tmpl-policy-issue-${i.severity}">
                            <strong>${escapeHtml(i.rule)}</strong>: ${escapeHtml(i.message)}
                        </li>`).join('')}
                    </ul>` : ''}`;
            } else {
                policyDiv.className = 'tmpl-revision-policy tmpl-policy-pass';
                policyDiv.innerHTML = `<div class="tmpl-policy-header">✅ Policy Check Passed</div>
                    <div class="tmpl-policy-summary">${escapeHtml(pr.summary)}</div>`;
            }
        }

        if (!res.ok) {
            resultDiv.innerHTML = `<div class="tmpl-revision-error">❌ ${escapeHtml(data.detail || data.message || 'Compose failed')}</div>`;
            return;
        }

        // Show detected services
        let servicesHtml = '';
        if (data.services_detected?.length) {
            servicesHtml = '<div class="tmpl-revision-actions"><strong>🔎 Detected services:</strong><ul>' +
                data.services_detected.map(s => {
                    return `<li>🎯 <strong>${escapeHtml(s.resource_type.split('/').pop())}</strong>${s.reason ? ' — ' + escapeHtml(s.reason) : ''}${s.quantity > 1 ? ' ×' + s.quantity : ''}</li>`;
                }).join('') + '</ul></div>';
        }

        let depsHtml = '';
        const depResolved = data.dependency_resolution?.resolved || [];
        if (depResolved.length) {
            depsHtml = '<div class="tmpl-revision-actions"><strong>📎 Dependencies resolved:</strong><ul>' +
                depResolved.map(a => {
                    const icon = a.action === 'auto_onboarded' ? '🔧' :
                                 a.action === 'added_from_catalog' ? '✅' : '❌';
                    return `<li>${icon} <strong>${escapeHtml(a.service_id.split('/').pop())}</strong> — ${escapeHtml(a.detail)}</li>`;
                }).join('') + '</ul></div>';
        }

        resultDiv.innerHTML = `
            <div class="tmpl-revision-success">
                ${servicesHtml}
                ${depsHtml}
                <div class="tmpl-revision-summary">
                    ✅ Template created: <strong>${escapeHtml(data.template?.name || data.name || '?')}</strong><br>
                    <strong>${data.resource_count || '?'}</strong> resources,
                    <strong>${data.parameter_count || '?'}</strong> parameters from
                    <strong>${data.services_detected?.length || data.service_count || '?'}</strong> services.
                </div>
            </div>`;

        textarea.value = '';
        showToast('✅ Template created — starting validation…', 'success');
        const createdTemplateId = data.template?.id || data.id;
        setTimeout(async () => {
            await loadCatalog();
            closeModal('modal-template-onboard');
            if (createdTemplateId) {
                // Auto-trigger full validation (tests + ARM)
                runFullValidation(createdTemplateId);
            }
        }, 1500);

    } catch (err) {
        resultDiv.style.display = 'block';
        resultDiv.innerHTML = `<div class="tmpl-revision-error">❌ ${escapeHtml(err.message)}</div>`;
        showToast(`❌ Compose error: ${err.message}`, 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = '🚀 Create Template';
    }
}

async function submitTemplateOnboarding(event) {
    event.preventDefault();
    const form = document.getElementById('form-template-onboard');
    const fd = new FormData(form);
    const btn = document.getElementById('btn-submit-template');
    const origText = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Composing…';

    // Hide any previous test results
    const testSection = document.getElementById('compose-test-results-section');
    if (testSection) testSection.style.display = 'none';

    const name = (fd.get('name') || '').trim();
    if (!name) {
        showToast('Template name is required', 'error');
        btn.disabled = false;
        btn.textContent = origText;
        return;
    }

    if (_composeSelections.size === 0) {
        showToast('Select at least one approved service', 'error');
        btn.disabled = false;
        btn.textContent = origText;
        return;
    }

    const selections = Array.from(_composeSelections.entries()).map(([sid, sel]) => ({
        service_id: sid,
        quantity: sel.quantity,
        parameters: Array.from(sel.parameters),
        version: sel.version,
    }));

    const body = {
        name: name,
        description: (fd.get('description') || '').trim(),
        category: fd.get('category') || 'blueprint',
        selections: selections,
    };

    try {
        // Step 1: Compose the template
        const res = await fetch('/api/catalog/templates/compose', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });

        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Failed to compose template');
        }

        const data = await res.json();
        const templateId = data.template_id;

        // Show dependency resolution results if any
        const depRes = data.dependency_resolution || {};
        const autoAdded = (depRes.resolved || []).filter(r => r.action === 'onboarded');
        const depAdded = (depRes.resolved || []).filter(r => r.action === 'added');
        if (autoAdded.length) {
            showToast(`🔧 Auto-onboarded ${autoAdded.length} missing service(s): ${autoAdded.map(r => r.service_id.split('/').pop()).join(', ')}`, 'info');
        }
        if (depAdded.length) {
            showToast(`📦 Auto-added ${depAdded.length} required dependency: ${depAdded.map(r => r.service_id.split('/').pop()).join(', ')}`, 'info');
        }

        // Step 2: Run structural tests
        btn.textContent = '🧪 Testing…';
        const testRes = await fetch(`/api/catalog/templates/${encodeURIComponent(templateId)}/test`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ version: data.version?.version || 1 }),
        });

        const testData = await testRes.json();

        // Step 3: Show test results
        _renderComposeTestResults(testData);

        if (testData.results?.all_passed) {
            showToast(`✅ Template "${name}" created & tests passed — validating against Azure…`, 'success');
            setTimeout(async () => {
                closeModal('modal-template-onboard');
                form.reset();
                _composeSelections.clear();
                await loadAllData();
                // Auto-trigger ARM validation
                showTemplateDetail(templateId);
                await new Promise(r => setTimeout(r, 300));
                showValidateForm(templateId);
                await new Promise(r => setTimeout(r, 200));
                runTemplateValidation(templateId);
            }, 1500);
        } else {
            showToast(`⚠️ Template "${name}" created — ${testData.results?.failed || 0} test(s) need attention. Open the template to auto-heal.`, 'warning');
            await loadAllData();
        }
    } catch (err) {
        showToast(err.message, 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = origText;
        _updateComposeSubmitButton();
    }
}

/** Render test results inside the compose modal */
function _renderComposeTestResults(testData) {
    const section = document.getElementById('compose-test-results-section');
    const container = document.getElementById('compose-test-results');
    if (!section || !container) return;

    section.style.display = 'block';
    const results = testData.results || {};
    const tests = results.tests || [];
    const allPassed = results.all_passed;

    let html = `
        <div class="test-summary ${allPassed ? 'test-summary-pass' : 'test-summary-fail'}">
            <span class="test-summary-icon">${allPassed ? '✅' : '❌'}</span>
            <span class="test-summary-text">
                ${allPassed ? 'All tests passed' : `${results.failed} of ${results.total} tests failed`}
                — Version ${testData.version}
                ${testData.promoted ? ' → Promoted to active' : ''}
            </span>
        </div>
        <div class="test-list">
    `;

    for (const test of tests) {
        html += `
            <div class="test-item ${test.passed ? 'test-pass' : 'test-fail'}">
                <span class="test-icon">${test.passed ? '✅' : '❌'}</span>
                <span class="test-name">${escapeHtml(test.name)}</span>
                <span class="test-message">${escapeHtml(test.message)}</span>
            </div>
        `;
    }

    html += '</div>';
    container.innerHTML = html;

    // Scroll to test results
    section.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}


// ══════════════════════════════════════════════════════════════
// GOVERNANCE STANDARDS
// ══════════════════════════════════════════════════════════════

async function loadStandards() {
    try {
        const res = await fetch('/api/standards');
        if (!res.ok) throw new Error('Failed to load standards');
        const data = await res.json();
        allStandards = data.standards || [];
        _buildStandardsCategoryFilters();
        _renderStandardsList();
    } catch (err) {
        console.error('Failed to load standards:', err);
        document.getElementById('standards-list').innerHTML =
            `<div class="compose-empty">Failed to load standards: ${err.message}</div>`;
    }
}

function _buildStandardsCategoryFilters() {
    const categories = [...new Set(allStandards.map(s => s.category))].sort();
    const container = document.getElementById('standards-category-filters');
    if (!container) return;
    container.innerHTML = `<button class="filter-pill ${currentStandardsCategoryFilter === 'all' ? 'active' : ''}" onclick="filterStandards('all')">All</button>` +
        categories.map(c =>
            `<button class="filter-pill ${currentStandardsCategoryFilter === c ? 'active' : ''}" onclick="filterStandards('${escapeHtml(c)}')">${escapeHtml(c)}</button>`
        ).join('');
}

function filterStandards(category) {
    currentStandardsCategoryFilter = category;
    _buildStandardsCategoryFilters();
    _renderStandardsList();
}

function filterStandardsBySeverity(severity) {
    currentStandardsSeverityFilter = severity;
    // Update active state on severity filter pills
    const container = document.getElementById('standards-severity-filters');
    if (container) {
        container.querySelectorAll('.filter-pill').forEach(btn => {
            const btnSeverity = btn.textContent.includes('Critical') ? 'critical' :
                btn.textContent.includes('High') ? 'high' :
                btn.textContent.includes('Medium') ? 'medium' :
                btn.textContent.includes('Low') ? 'low' : 'all';
            btn.classList.toggle('active', btnSeverity === severity);
        });
    }
    _renderStandardsList();
}

function searchStandards(query) {
    standardsSearchQuery = query.toLowerCase();
    _renderStandardsList();
}

function _renderStandardsList() {
    const container = document.getElementById('standards-list');
    const summaryEl = document.getElementById('standards-results-summary');
    if (!container) return;

    let filtered = allStandards;

    // Category filter
    if (currentStandardsCategoryFilter !== 'all') {
        filtered = filtered.filter(s => s.category === currentStandardsCategoryFilter);
    }

    // Severity filter
    if (currentStandardsSeverityFilter !== 'all') {
        filtered = filtered.filter(s => s.severity === currentStandardsSeverityFilter);
    }

    // Search filter
    if (standardsSearchQuery) {
        filtered = filtered.filter(s =>
            s.name.toLowerCase().includes(standardsSearchQuery) ||
            s.id.toLowerCase().includes(standardsSearchQuery) ||
            (s.description || '').toLowerCase().includes(standardsSearchQuery) ||
            s.category.toLowerCase().includes(standardsSearchQuery)
        );
    }

    if (summaryEl) {
        summaryEl.textContent = `Showing ${filtered.length} of ${allStandards.length} standards`;
    }

    if (!filtered.length) {
        container.innerHTML = '<div class="compose-empty">No standards match your filters.</div>';
        return;
    }

    container.innerHTML = filtered.map(std => {
        const severityIcon = std.severity === 'critical' ? '🔴' :
            std.severity === 'high' ? '🟠' :
            std.severity === 'medium' ? '🟡' : '🟢';
        const enabledClass = std.enabled ? '' : 'std-disabled';
        const rule = std.rule || {};
        const ruleType = rule.type || 'property';

        let rulePreview = '';
        if (ruleType === 'property') {
            rulePreview = `${rule.key || '?'} ${rule.operator || '=='} ${JSON.stringify(rule.value)}`;
        } else if (ruleType === 'tags') {
            rulePreview = `Required tags: ${(rule.required_tags || []).join(', ')}`;
        } else if (ruleType === 'allowed_values') {
            rulePreview = `${rule.key || '?'} ∈ {${(rule.values || []).join(', ')}}`;
        } else if (ruleType === 'cost_threshold') {
            rulePreview = `Max $${rule.max_monthly_usd || 0}/month`;
        }

        return `
        <div class="std-card ${enabledClass}" onclick="showStandardDetail('${escapeHtml(std.id)}')">
            <div class="std-card-header">
                <div class="std-card-title">
                    <span class="std-severity-icon">${severityIcon}</span>
                    <div class="std-name-block">
                        <span class="std-name">${escapeHtml(std.name)}</span>
                        <span class="std-id">${escapeHtml(std.id)}</span>
                    </div>
                </div>
                <div class="std-card-badges">
                    <span class="category-badge">${escapeHtml(std.category)}</span>
                    <span class="std-scope-badge" title="Scope: ${escapeHtml(std.scope)}">${escapeHtml(std.scope === '*' ? 'All Services' : std.scope)}</span>
                    ${!std.enabled ? '<span class="std-disabled-badge">Disabled</span>' : ''}
                </div>
            </div>
            <div class="std-card-desc">${escapeHtml(std.description || '')}</div>
            <div class="std-card-rule"><code>${escapeHtml(rulePreview)}</code></div>
        </div>`;
    }).join('');
}

async function showStandardDetail(standardId) {
    const std = allStandards.find(s => s.id === standardId);
    if (!std) return;

    document.getElementById('detail-standard-name').textContent = std.name;
    const body = document.getElementById('detail-standard-body');

    const rule = std.rule || {};
    const ruleJson = JSON.stringify(rule, null, 2);

    // Load version history
    let historyHtml = '<div class="std-history-loading">Loading history...</div>';
    body.innerHTML = _buildStandardDetailHtml(std, ruleJson, historyHtml);
    document.getElementById('standard-detail-drawer').classList.remove('hidden');

    try {
        const res = await fetch(`/api/standards/${encodeURIComponent(standardId)}/history`);
        if (res.ok) {
            const data = await res.json();
            const versions = data.versions || [];
            historyHtml = versions.length ? versions.map(v =>
                `<div class="std-history-item">
                    <div class="std-history-ver">v${v.version}</div>
                    <div class="std-history-detail">
                        <div class="std-history-by">${escapeHtml(v.changed_by || 'unknown')}</div>
                        <div class="std-history-date">${v.changed_at ? new Date(v.changed_at).toLocaleDateString() : '—'}</div>
                        ${v.change_reason ? `<div class="std-history-reason">${escapeHtml(v.change_reason)}</div>` : ''}
                    </div>
                </div>`
            ).join('') : '<div class="std-history-empty">No version history</div>';
        }
    } catch (e) {
        historyHtml = '<div class="std-history-empty">Failed to load history</div>';
    }

    body.innerHTML = _buildStandardDetailHtml(std, ruleJson, historyHtml);
}

function _buildStandardDetailHtml(std, ruleJson, historyHtml) {
    const severityIcon = std.severity === 'critical' ? '🔴' :
        std.severity === 'high' ? '🟠' :
        std.severity === 'medium' ? '🟡' : '🟢';

    return `
    <div class="std-detail-section">
        <div class="std-detail-meta">
            <span class="category-badge">${escapeHtml(std.category)}</span>
            <span class="std-severity-badge">${severityIcon} ${escapeHtml(std.severity)}</span>
            <span class="std-scope-badge">${escapeHtml(std.scope)}</span>
            ${std.enabled ? '<span class="std-enabled-badge">✅ Enabled</span>' : '<span class="std-disabled-badge">❌ Disabled</span>'}
        </div>
        <p class="std-detail-desc">${escapeHtml(std.description || '')}</p>
    </div>

    <div class="std-detail-section">
        <h4>Rule Definition</h4>
        <pre class="std-rule-json"><code>${escapeHtml(ruleJson)}</code></pre>
    </div>

    <div class="std-detail-section">
        <h4>Version History</h4>
        <div class="std-history-list">${historyHtml}</div>
    </div>

    <div class="std-detail-actions">
        <button class="btn btn-sm btn-primary" onclick="openEditStandardModal('${escapeHtml(std.id)}')">✏️ Edit</button>
        <button class="btn btn-sm btn-ghost btn-danger" onclick="deleteStandard('${escapeHtml(std.id)}')">🗑️ Delete</button>
    </div>`;
}

function closeStandardDetail() {
    document.getElementById('standard-detail-drawer').classList.add('hidden');
}

function openAddStandardModal() {
    document.getElementById('standard-modal-title').textContent = 'Add Standard';
    const form = document.getElementById('form-standard');
    form.reset();
    form.querySelector('input[name="id"]').value = '';
    form.querySelector('input[name="enabled"]').checked = true;
    document.getElementById('btn-save-standard').textContent = 'Create Standard';
    document.getElementById('modal-standard').classList.remove('hidden');
}

function openEditStandardModal(standardId) {
    const std = allStandards.find(s => s.id === standardId);
    if (!std) return;

    closeStandardDetail();
    document.getElementById('standard-modal-title').textContent = 'Edit Standard';
    const form = document.getElementById('form-standard');
    form.querySelector('input[name="id"]').value = std.id;
    form.querySelector('input[name="name"]').value = std.name;
    form.querySelector('textarea[name="description"]').value = std.description || '';
    form.querySelector('select[name="category"]').value = std.category;
    form.querySelector('select[name="severity"]').value = std.severity;
    form.querySelector('input[name="scope"]').value = std.scope || '*';
    form.querySelector('textarea[name="rule_json"]').value = JSON.stringify(std.rule || {}, null, 2);
    form.querySelector('input[name="enabled"]').checked = std.enabled;
    form.querySelector('input[name="change_reason"]').value = '';
    document.getElementById('btn-save-standard').textContent = 'Update Standard';
    document.getElementById('modal-standard').classList.remove('hidden');
}

async function saveStandard(event) {
    event.preventDefault();
    const form = document.getElementById('form-standard');
    const fd = new FormData(form);
    const btn = document.getElementById('btn-save-standard');
    const origText = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Saving...';

    const existingId = fd.get('id');
    const isEdit = !!existingId;

    let rule;
    try {
        const ruleText = fd.get('rule_json') || '{}';
        rule = JSON.parse(ruleText);
    } catch (e) {
        showToast('Invalid JSON in Rule field', 'error');
        btn.disabled = false;
        btn.textContent = origText;
        return;
    }

    const body = {
        name: fd.get('name'),
        description: fd.get('description') || '',
        category: fd.get('category'),
        severity: fd.get('severity') || 'high',
        scope: fd.get('scope') || '*',
        rule: rule,
        enabled: !!form.querySelector('input[name="enabled"]').checked,
        change_reason: fd.get('change_reason') || '',
    };

    try {
        let res;
        if (isEdit) {
            res = await fetch(`/api/standards/${encodeURIComponent(existingId)}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
        } else {
            res = await fetch('/api/standards', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
        }

        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Failed to save standard');
        }

        showToast(isEdit ? `Standard "${body.name}" updated` : `Standard "${body.name}" created`);
        closeModal('modal-standard');
        await loadStandards();
    } catch (err) {
        showToast(err.message, 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = origText;
    }
}

async function deleteStandard(standardId) {
    if (!confirm(`Delete standard ${standardId}? This cannot be undone.`)) return;

    try {
        const res = await fetch(`/api/standards/${encodeURIComponent(standardId)}`, {
            method: 'DELETE',
        });
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Failed to delete');
        }
        showToast(`Standard ${standardId} deleted`);
        closeStandardDetail();
        await loadStandards();
    } catch (err) {
        showToast(err.message, 'error');
    }
}


// ── Standards Import ─────────────────────────────────────────

let _importedStandards = [];

function openImportStandardsModal() {
    _importedStandards = [];
    document.getElementById('import-standards-content').value = '';
    document.getElementById('import-standards-preview').classList.add('hidden');
    document.getElementById('import-standards-list').innerHTML = '';
    document.getElementById('btn-extract-standards').classList.remove('hidden');
    document.getElementById('btn-save-imported-standards').classList.add('hidden');
    document.getElementById('btn-extract-standards').disabled = false;
    document.getElementById('btn-extract-standards').textContent = '🤖 Extract Standards';
    openModal('modal-import-standards');
}

async function extractStandards() {
    const content = document.getElementById('import-standards-content').value.trim();
    if (!content) {
        showToast('Please paste your standards documentation first', 'error');
        return;
    }

    const btn = document.getElementById('btn-extract-standards');
    btn.disabled = true;
    btn.textContent = '🔄 Extracting…';

    try {
        const res = await fetch('/api/standards/import', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ content, source_type: 'text', save: false }),
        });

        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Import failed');
        }

        const data = await res.json();
        _importedStandards = data.standards || [];

        if (_importedStandards.length === 0) {
            showToast('No standards could be extracted from the document', 'error');
            btn.disabled = false;
            btn.textContent = '🤖 Extract Standards';
            return;
        }

        // Render preview
        _renderImportPreview(_importedStandards);
        document.getElementById('import-standards-preview').classList.remove('hidden');
        btn.classList.add('hidden');
        document.getElementById('btn-save-imported-standards').classList.remove('hidden');
        showToast(`Extracted ${_importedStandards.length} standard(s) — review and save`, 'success');
    } catch (err) {
        showToast(err.message, 'error');
        btn.disabled = false;
        btn.textContent = '🤖 Extract Standards';
    }
}

function _renderImportPreview(standards) {
    const container = document.getElementById('import-standards-list');
    const severityIcons = { critical: '🔴', high: '🟠', medium: '🟡', low: '🟢' };

    container.innerHTML = standards.map((std, i) => {
        const icon = severityIcons[std.severity] || '⚪';
        const ruleType = std.rule?.type || 'property';
        const ruleDesc = _describeRule(std.rule);
        return `
        <div class="import-std-card" style="padding: 0.75rem; margin-bottom: 0.5rem; border: 1px solid var(--border); border-radius: 8px; background: var(--surface);">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.25rem;">
                <strong style="font-size: 0.9rem;">${icon} ${escapeHtml(std.name)}</strong>
                <div style="display: flex; gap: 0.5rem; align-items: center;">
                    <span class="badge badge-${std.severity}" style="font-size: 0.7rem;">${std.severity}</span>
                    <span class="badge" style="font-size: 0.7rem; background: var(--bg-hover);">${escapeHtml(std.category)}</span>
                    <label style="font-size: 0.75rem; display: flex; align-items: center; gap: 0.25rem; cursor: pointer;">
                        <input type="checkbox" checked onchange="_toggleImportStd(${i}, this.checked)" /> Include
                    </label>
                </div>
            </div>
            <div style="font-size: 0.8rem; color: var(--text-secondary); margin-bottom: 0.25rem;">${escapeHtml(std.description || '')}</div>
            <div style="font-size: 0.75rem; color: var(--text-tertiary);">
                <span title="Rule type">📏 ${ruleType}</span> · <span title="Scope">🎯 ${escapeHtml(std.scope || '*')}</span> · <span title="ID">🏷️ ${escapeHtml(std.id)}</span>
            </div>
            <div style="font-size: 0.75rem; color: var(--text-tertiary); margin-top: 0.25rem;">${ruleDesc}</div>
        </div>`;
    }).join('');
}

function _describeRule(rule) {
    if (!rule) return '';
    switch (rule.type) {
        case 'property':
            return `Check: <code>${escapeHtml(rule.key || '?')}</code> ${escapeHtml(rule.operator || '==')} <code>${escapeHtml(String(rule.value ?? '?'))}</code>`;
        case 'tags':
            return `Required tags: <code>${(rule.required_tags || []).join(', ')}</code>`;
        case 'allowed_values':
            return `<code>${escapeHtml(rule.key || '?')}</code> must be one of: <code>${(rule.values || []).join(', ')}</code>`;
        case 'cost_threshold':
            return `Max monthly cost: $${rule.max_monthly_usd || 0}`;
        default:
            return JSON.stringify(rule).substring(0, 120);
    }
}

function _toggleImportStd(index, checked) {
    if (_importedStandards[index]) {
        _importedStandards[index]._include = checked;
    }
}

async function saveImportedStandards() {
    const toSave = _importedStandards.filter((s, i) => s._include !== false);
    if (toSave.length === 0) {
        showToast('No standards selected to save', 'error');
        return;
    }

    const btn = document.getElementById('btn-save-imported-standards');
    btn.disabled = true;
    btn.textContent = '💾 Saving…';

    let saved = 0, failed = 0;
    for (const std of toSave) {
        try {
            const body = {
                id: std.id,
                name: std.name,
                description: std.description || '',
                category: std.category,
                severity: std.severity,
                scope: std.scope || '*',
                rule: std.rule || {},
                enabled: true,
                created_by: 'standards-import',
            };
            const res = await fetch('/api/standards', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            if (res.ok) saved++;
            else failed++;
        } catch {
            failed++;
        }
    }

    if (failed > 0) {
        showToast(`Saved ${saved} standard(s), ${failed} failed (may already exist)`, 'warning');
    } else {
        showToast(`✅ Saved ${saved} standard(s) to your organization's governance catalog`, 'success');
    }

    closeModal('modal-import-standards');
    await loadStandards();
}


// ══════════════════════════════════════════════════════════════
// OBSERVABILITY — Deployments & Service Validation
// ══════════════════════════════════════════════════════════════

let _obsCurrentTab = 'deployments';

function switchObsTab(tab) {
    _obsCurrentTab = tab;
    document.querySelectorAll('.obs-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.obs-tab-content').forEach(c => c.classList.add('hidden'));
    const tabBtn = document.getElementById(`obs-tab-${tab}`);
    const content = document.getElementById(`obs-content-${tab}`);
    if (tabBtn) tabBtn.classList.add('active');
    if (content) content.classList.remove('hidden');
}

async function loadDeploymentHistory() {
    try {
        const res = await fetch('/api/deployments');
        if (!res.ok) throw new Error('Failed');
        const data = await res.json();
        const deployments = data.deployments || [];
        _renderDeploymentFeed(deployments);
    } catch (err) {
        console.warn('Deployment history load failed:', err);
    }
}

function _renderDeploymentFeed(deployments) {
    const feed = document.getElementById('obs-deploy-feed');
    if (!feed) return;

    // Update summary counters
    const total = deployments.length;
    const succeeded = deployments.filter(d => d.status === 'succeeded').length;
    const failed = deployments.filter(d => d.status === 'failed').length;
    const el = (id, val) => { const e = document.getElementById(id); if (e) e.textContent = val; };
    el('obs-deployments-total', total);
    el('obs-deployments-succeeded', succeeded);
    el('obs-deployments-failed', failed);

    if (deployments.length === 0) {
        feed.innerHTML = `
            <div class="activity-empty">
                <span class="activity-empty-icon">🚀</span>
                <p>No deployments yet. Deploy a template from the Template Catalog.</p>
            </div>`;
        return;
    }

    feed.innerHTML = deployments.map(d => _renderDeploymentRunCard(d)).join('');
}

function _renderDeploymentRunCard(dep) {
    // Status display
    let statusClass, statusIcon, statusLabel;
    switch (dep.status) {
        case 'succeeded':
            statusClass = 'obs-deploy-succeeded'; statusIcon = '✅'; statusLabel = 'Succeeded'; break;
        case 'failed':
            statusClass = 'obs-deploy-failed'; statusIcon = '❌'; statusLabel = 'Failed'; break;
        case 'deploying':
            statusClass = 'obs-deploy-running'; statusIcon = '⏳'; statusLabel = 'Deploying'; break;
        case 'validating':
            statusClass = 'obs-deploy-running'; statusIcon = '🔍'; statusLabel = 'Validating'; break;
        default:
            statusClass = 'obs-deploy-pending'; statusIcon = '⏳'; statusLabel = dep.status || 'Pending';
    }

    // Template info
    const tmplName = dep.template_name || dep.deployment_name || 'Ad-hoc deployment';
    const tmplId = dep.template_id ? `<span class="obs-deploy-tmpl-id">${escapeHtml(dep.template_id)}</span>` : '';

    // Time display
    const startTime = dep.started_at ? new Date(dep.started_at).toLocaleString() : '';
    const duration = dep.started_at && dep.completed_at
        ? _formatDuration(new Date(dep.completed_at) - new Date(dep.started_at))
        : dep.started_at ? _timeAgo(dep.started_at) : '';

    // Resource group + region
    const rgRegion = [dep.resource_group, dep.region].filter(Boolean).join(' · ');

    // Provisioned resources
    let resourcesHtml = '';
    const resources = dep.provisioned_resources || [];
    if (resources.length > 0) {
        const chips = resources.map(r => {
            const shortType = (r.type || r.resource_type || '').split('/').pop();
            const rName = r.name || r.resource_name || '';
            const rStatus = r.provisioning_state || r.status || '';
            const chipClass = rStatus === 'Succeeded' ? 'obs-res-ok' : rStatus === 'Failed' ? 'obs-res-fail' : '';
            return `<span class="obs-res-chip ${chipClass}" title="${escapeHtml(r.type || '')}">${escapeHtml(shortType)}${rName ? ': ' + escapeHtml(rName) : ''}</span>`;
        }).join('');
        resourcesHtml = `<div class="obs-deploy-resources"><span class="obs-deploy-resources-label">Resources:</span> ${chips}</div>`;
    }

    // Error display
    let errorHtml = '';
    if (dep.status === 'failed' && dep.error) {
        const parsed = _parseValidationError(dep.error);
        errorHtml = _renderStructuredError(parsed, { compact: true, showRaw: true });
    }

    // Outputs
    let outputsHtml = '';
    const outputs = dep.outputs || {};
    const outputKeys = Object.keys(outputs);
    if (outputKeys.length > 0 && dep.status === 'succeeded') {
        const outputItems = outputKeys.slice(0, 5).map(k => {
            const val = typeof outputs[k] === 'object' ? (outputs[k].value || JSON.stringify(outputs[k])) : outputs[k];
            return `<div class="obs-output-item"><span class="obs-output-key">${escapeHtml(k)}:</span> <span class="obs-output-val">${escapeHtml(String(val).substring(0, 100))}</span></div>`;
        }).join('');
        outputsHtml = `<details class="obs-deploy-outputs"><summary>📤 Outputs (${outputKeys.length})</summary><div class="obs-output-list">${outputItems}</div></details>`;
    }

    // Deployment ID (short)
    const shortId = dep.deployment_id ? dep.deployment_id.substring(0, 20) : '';

    return `
    <div class="obs-deploy-card ${statusClass}">
        <div class="obs-deploy-header">
            <div class="obs-deploy-title">
                <span class="obs-deploy-icon">${statusIcon}</span>
                <div class="obs-deploy-name-block">
                    <span class="obs-deploy-name">${escapeHtml(tmplName)}</span>
                    ${tmplId}
                </div>
            </div>
            <div class="obs-deploy-meta-right">
                <span class="obs-deploy-badge ${statusClass}">${statusLabel}</span>
                <span class="obs-deploy-time" title="${escapeHtml(startTime)}">${escapeHtml(duration)}</span>
            </div>
        </div>
        <div class="obs-deploy-details">
            <span class="obs-deploy-detail-item">📦 ${escapeHtml(rgRegion)}</span>
            <span class="obs-deploy-detail-item">🆔 ${escapeHtml(shortId)}</span>
            <span class="obs-deploy-detail-item">👤 ${escapeHtml(dep.initiated_by || 'unknown')}</span>
        </div>
        ${resourcesHtml}
        ${errorHtml}
        ${outputsHtml}
    </div>`;
}

function _formatDuration(ms) {
    const secs = Math.floor(ms / 1000);
    if (secs < 60) return `${secs}s`;
    const mins = Math.floor(secs / 60);
    const remSecs = secs % 60;
    if (mins < 60) return `${mins}m ${remSecs}s`;
    const hours = Math.floor(mins / 60);
    return `${hours}h ${mins % 60}m`;
}

// ── Service Validation Activity (existing) ──────────────────

let _activityPollTimer = null;

function _startActivityPolling() {
    _stopActivityPolling();
    _activityPollTimer = setInterval(() => {
        loadActivity(true);
        if (_obsCurrentTab === 'deployments') loadDeploymentHistory();
    }, 5000);
}

function _stopActivityPolling() {
    if (_activityPollTimer) {
        clearInterval(_activityPollTimer);
        _activityPollTimer = null;
    }
}

async function loadActivity(silent = false) {
    try {
        const res = await fetch('/api/activity');
        if (!res.ok) throw new Error('Failed to load activity');
        const data = await res.json();
        renderActivityFeed(data);
        updateActivityBadge(data.summary);
    } catch (err) {
        if (!silent) console.warn('Activity load failed:', err);
    }
}

function updateActivityBadge(summary) {
    const badge = document.getElementById('nav-activity-badge');
    if (!badge) return;
    const running = summary.running || 0;
    if (running > 0) {
        badge.textContent = running;
        badge.style.display = '';
        badge.className = 'nav-badge nav-badge-active';
    } else {
        badge.style.display = 'none';
    }

    // Also update dashboard stats if present
    const statValidating = document.getElementById('stat-review');
    if (statValidating && summary.validating > 0) {
        // keep existing value
    }
}

function renderActivityFeed(data) {
    const feed = document.getElementById('activity-feed');
    const summary = data.summary || {};
    const jobs = data.jobs || [];

    // Update summary counters
    const runEl = document.getElementById('activity-running');
    const valEl = document.getElementById('activity-validating');
    const appEl = document.getElementById('activity-approved');
    const failEl = document.getElementById('activity-failed');
    if (runEl) runEl.textContent = summary.running || 0;
    if (valEl) valEl.textContent = summary.validating || 0;
    if (appEl) appEl.textContent = summary.approved || 0;
    if (failEl) failEl.textContent = summary.failed || 0;

    // Pulse animation for running count
    if (runEl) {
        if (summary.running > 0) runEl.classList.add('activity-pulse');
        else runEl.classList.remove('activity-pulse');
    }

    if (!feed) return;

    if (jobs.length === 0) {
        feed.innerHTML = `
            <div class="activity-empty">
                <span class="activity-empty-icon">📡</span>
                <p>No deployment activity yet. Approve both gates on a service to trigger validation.</p>
            </div>`;
        return;
    }

    feed.innerHTML = jobs.map(job => _renderActivityCard(job)).join('');

    // Auto-scroll event logs to bottom for running jobs
    for (const job of jobs) {
        if (job.is_running && job.events && job.events.length > 0) {
            const eventsEl = document.getElementById(`activity-events-${job.service_id}`);
            if (eventsEl) eventsEl.scrollTop = eventsEl.scrollHeight;
        }
    }
}

/**
 * Parse a raw validation error (string or JSON) into a structured object
 * for clear, actionable error display.
 */
function _parseValidationError(raw) {
    if (!raw) return null;

    let errorStr = '';
    let phase = '';
    let timestamp = '';

    // If it's a JSON string (e.g. review_notes), parse it
    if (typeof raw === 'string') {
        try {
            const parsed = JSON.parse(raw);
            errorStr = parsed.error || parsed.detail || JSON.stringify(parsed);
            phase = parsed.phase || '';
            timestamp = parsed.validated_at || '';
        } catch {
            errorStr = raw;
        }
    } else if (typeof raw === 'object') {
        errorStr = raw.error || raw.detail || JSON.stringify(raw);
        phase = raw.phase || '';
        timestamp = raw.validated_at || '';
    }

    // Extract operation errors after the " | Operation errors: " delimiter
    const opSplit = errorStr.split(' | Operation errors: ');
    const mainMessage = opSplit[0] || errorStr;
    const opErrorsRaw = opSplit.length > 1 ? opSplit[1] : '';

    // Parse individual operation errors: "Microsoft.X/y/name: [Code] message"
    const operationErrors = [];
    if (opErrorsRaw) {
        // Split on patterns like "Microsoft." that start a new error (but not the first one)
        const opParts = opErrorsRaw.split(/(?=Microsoft\.)/);
        for (const part of opParts) {
            const trimmed = part.trim().replace(/[;,]\s*$/, '');
            if (!trimmed) continue;

            // Pattern: "Microsoft.Network/virtualNetworks/myName: [InvalidRequestFormat] Cannot parse the request."
            const match = trimmed.match(/^(Microsoft\.\w+\/[\w/]+)(?:\/([^:]+))?:\s*\[(\w+)\]\s*(.*)/);
            if (match) {
                operationErrors.push({
                    resourceType: match[1],
                    resourceName: match[2] || '',
                    errorCode: match[3],
                    message: match[4].trim(),
                });
            } else {
                // Fallback: just capture whatever we can
                const codeMatch = trimmed.match(/\[(\w+)\]\s*(.*)/);
                operationErrors.push({
                    resourceType: '',
                    resourceName: '',
                    errorCode: codeMatch ? codeMatch[1] : '',
                    message: codeMatch ? codeMatch[2].trim() : trimmed,
                });
            }
        }
    }

    // Extract error code from main message if no operation errors found
    let mainErrorCode = '';
    const mainCodeMatch = mainMessage.match(/\[(\w+)\]/);
    if (mainCodeMatch) mainErrorCode = mainCodeMatch[1];

    // Determine a clean summary message (strip generic ARM boilerplate)
    let summary = mainMessage;
    const boilerplate = [
        'At least one resource deployment operation failed. Please list deployment operations for details.',
        'Please see https://aka.ms/arm-deployment-operations for usage details.',
        'Please see https://aka.ms/DeployOperations for usage details.',
    ];
    for (const bp of boilerplate) {
        summary = summary.replace(bp, '').trim();
    }
    summary = summary.replace(/^Deploy failed:\s*/i, '').trim();
    if (!summary && operationErrors.length > 0) {
        summary = `${operationErrors.length} resource error(s) during deployment`;
    }
    if (!summary) summary = mainMessage;

    // Phase label
    const phaseLabels = {
        deploy: 'Deployment',
        what_if: 'What-If Preview',
        policy_compliance: 'Policy Compliance',
        static_check: 'Static Validation',
        resource_check: 'Resource Verification',
        unknown: 'Validation',
    };
    const phaseLabel = phaseLabels[phase] || phase || 'Validation';

    // Troubleshooting hints based on error codes
    const hints = [];
    const allCodes = [mainErrorCode, ...operationErrors.map(e => e.errorCode)].filter(Boolean);
    for (const code of allCodes) {
        const lc = code.toLowerCase();
        if (lc.includes('invalidrequestformat') || lc.includes('invalidtemplate'))
            hints.push('The ARM template has a syntax or schema issue. Check resource API versions and property names.');
        else if (lc.includes('invalidresourcereference') || lc.includes('resourcenotfound'))
            hints.push('A resource reference is invalid. Ensure dependent resources are defined in the correct order.');
        else if (lc.includes('skuNotAvailable') || lc.includes('skupnotavailable'))
            hints.push('The requested SKU is not available in the target region. Try a different SKU or region.');
        else if (lc.includes('quotaexceeded'))
            hints.push('Subscription quota exceeded. Request a quota increase or use a different subscription.');
        else if (lc.includes('authorization') || lc.includes('forbidden'))
            hints.push('Insufficient permissions. The service principal may need additional role assignments.');
        else if (lc.includes('conflict') || lc.includes('beingdeleted'))
            hints.push('Resource conflict — it may already exist or be in a transitional state. Wait and retry.');
        else if (lc.includes('badrequest'))
            hints.push('Invalid request. Review the template parameters and resource properties.');
        else if (lc.includes('linkedinvalidpropertypolicyviolation') || lc.includes('policyviolation'))
            hints.push('Azure Policy denied the deployment. Check org-level Azure Policies for restrictions.');
        else if (lc.includes('invalidparameter'))
            hints.push('A parameter value is invalid. Check parameter types and allowed values.');
    }
    // Deduplicate
    const uniqueHints = [...new Set(hints)];

    return {
        phase,
        phaseLabel,
        summary,
        mainMessage,
        mainErrorCode,
        operationErrors,
        hints: uniqueHints,
        timestamp,
        raw: errorStr,
    };
}

/**
 * Render a structured error display from a parsed validation error.
 */
function _renderStructuredError(parsed, options = {}) {
    if (!parsed) return '';
    const { compact = false, showRaw = true } = options;

    // Phase badge
    const phaseBadge = parsed.phase
        ? `<span class="error-phase-badge error-phase-${escapeHtml(parsed.phase)}">${escapeHtml(parsed.phaseLabel)}</span>`
        : '';

    // Timestamp
    const timeStr = parsed.timestamp
        ? `<span class="error-timestamp">${new Date(parsed.timestamp).toLocaleString()}</span>`
        : '';

    // Summary
    const summaryHtml = `<div class="error-summary-text">${escapeHtml(parsed.summary)}</div>`;

    // Operation errors (per-resource)
    let opsHtml = '';
    if (parsed.operationErrors.length > 0) {
        const opsItems = parsed.operationErrors.map(op => {
            const resDisplay = op.resourceType
                ? `<span class="error-resource-type">${escapeHtml(op.resourceType)}</span>`
                  + (op.resourceName ? `<span class="error-resource-sep">/</span><span class="error-resource-name">${escapeHtml(op.resourceName)}</span>` : '')
                : '';
            const codeBadge = op.errorCode
                ? `<span class="error-code-badge">${escapeHtml(op.errorCode)}</span>`
                : '';
            return `
                <div class="error-op-item">
                    <div class="error-op-resource">${resDisplay}</div>
                    <div class="error-op-detail">
                        ${codeBadge}
                        <span class="error-op-message">${escapeHtml(op.message)}</span>
                    </div>
                </div>`;
        }).join('');
        opsHtml = `
            <div class="error-ops-section">
                <div class="error-ops-label">Resource Errors</div>
                ${opsItems}
            </div>`;
    }

    // Troubleshooting hints
    let hintsHtml = '';
    if (parsed.hints.length > 0 && !compact) {
        hintsHtml = `
            <div class="error-hints-section">
                <div class="error-hints-label">💡 Troubleshooting</div>
                <ul class="error-hints-list">
                    ${parsed.hints.map(h => `<li>${escapeHtml(h)}</li>`).join('')}
                </ul>
            </div>`;
    }

    // Raw error (collapsible)
    let rawHtml = '';
    if (showRaw && !compact) {
        rawHtml = `
            <details class="error-raw-section">
                <summary class="error-raw-toggle">View raw error</summary>
                <pre class="error-raw-content">${escapeHtml(parsed.raw)}</pre>
            </details>`;
    }

    return `
        <div class="structured-error ${compact ? 'structured-error-compact' : ''}">
            <div class="error-header-row">
                <span class="error-icon">⛔</span>
                ${phaseBadge}
                ${timeStr}
            </div>
            ${summaryHtml}
            ${opsHtml}
            ${hintsHtml}
            ${rawHtml}
        </div>`;
}

function _renderActivityCard(job) {
    const isRunning = job.is_running;
    const status = job.status;

    // Status display
    let statusClass, statusIcon, statusText;
    if (isRunning) {
        statusClass = 'activity-status-running';
        statusIcon = '⏳';
        statusText = `Attempt ${job.attempt}/${job.max_attempts}`;
    } else if (status === 'approved') {
        statusClass = 'activity-status-approved';
        statusIcon = '✅';
        statusText = 'Approved';
    } else if (status === 'validation_failed') {
        statusClass = 'activity-status-failed';
        statusIcon = '⛔';
        statusText = 'Failed';
    } else if (status === 'validating') {
        statusClass = 'activity-status-waiting';
        statusIcon = '🔄';
        statusText = 'Awaiting Validation';
    } else {
        statusClass = 'activity-status-unknown';
        statusIcon = '❓';
        statusText = status;
    }

    // ── Step pipeline indicator ──────────────────────────────
    const pipelineSteps = [
        { key: 'parsing', label: 'Parse', icon: '📝' },
        { key: 'what_if', label: 'What-If', icon: '🔍' },
        { key: 'deploying', label: 'Deploy', icon: '🚀' },
        { key: 'resource_check', label: 'Verify', icon: '🔎' },
        { key: 'policy_testing', label: 'Policy', icon: '🛡️' },
        { key: 'cleanup', label: 'Cleanup', icon: '🧹' },
        { key: 'promoting', label: 'Approve', icon: '🏆' },
    ];
    const completedSteps = job.steps_completed || [];
    const currentPhase = job.phase || '';
    // Map phases to their pipeline step
    const phaseToStep = {
        starting: 'parsing', what_if: 'what_if', what_if_complete: 'what_if',
        deploying: 'deploying', deploy_complete: 'deploying', deploy_failed: 'deploying',
        resource_check: 'resource_check', resource_check_complete: 'resource_check',
        resource_check_warning: 'resource_check',
        policy_testing: 'policy_testing', policy_failed: 'policy_testing',
        policy_skip: 'policy_testing',
        cleanup: 'cleanup', cleanup_complete: 'cleanup',
        promoting: 'promoting',
        fixing_template: currentPhase, template_fixed: currentPhase,
        infra_retry: currentPhase,
    };
    const activeStep = phaseToStep[currentPhase] || currentPhase;

    let pipelineHtml = '';
    if (isRunning || status === 'approved' || status === 'validation_failed') {
        const stepItems = pipelineSteps.map(s => {
            let cls = 'activity-step-pending';
            if (completedSteps.includes(s.key) || status === 'approved') cls = 'activity-step-done';
            else if (isRunning && activeStep === s.key) cls = 'activity-step-active';
            else if (status === 'validation_failed' && activeStep === s.key) cls = 'activity-step-failed';
            return `<div class="activity-step ${cls}" title="${s.label}"><span class="activity-step-icon">${s.icon}</span><span class="activity-step-label">${s.label}</span></div>`;
        }).join('<div class="activity-step-connector"></div>');
        pipelineHtml = `<div class="activity-pipeline">${stepItems}</div>`;
    }

    // ── Current detail text (shown prominently) ──────────────
    let detailHtml = '';
    if (isRunning && job.detail) {
        detailHtml = `<div class="activity-detail-live">${escapeHtml(job.detail)}</div>`;
    }

    // ── Phase display for running jobs ──────────────────────
    let phaseHtml = '';
    if (isRunning && job.phase) {
        const phaseLabels = {
            starting: '🔧 Initializing validation pipeline…',
            what_if: '🔍 Running ARM What-If analysis…',
            what_if_complete: '✓ What-If analysis passed',
            deploying: '🚀 Deploying resources to Azure…',
            deploy_complete: '📦 Deployment succeeded',
            deploy_failed: '💥 Deployment failed — preparing auto-heal',
            resource_check: '🔎 Verifying provisioned resources…',
            resource_check_complete: '✓ Resources verified in Azure',
            policy_testing: '🛡️ Evaluating policy compliance…',
            policy_failed: '⚠️ Policy violation detected',
            policy_skip: 'ℹ️ No policy to evaluate',
            cleanup: '🧹 Cleaning up validation resources…',
            cleanup_complete: '✓ Cleanup initiated',
            promoting: '🏆 Promoting service to approved…',
            fixing_template: '🤖 Copilot SDK auto-healing template…',
            template_fixed: '🔧 Template fixed by Copilot SDK',
            infra_retry: '⏳ Waiting for Azure (transient error)…',
            fixing_policy: '🤖 Copilot SDK fixing policy JSON…',
        };
        phaseHtml = `<div class="activity-phase">${phaseLabels[job.phase] || job.phase}</div>`;
    }

    // ── Template metadata ────────────────────────────────────
    let metaHtml = '';
    const meta = job.template_meta || {};
    if (meta.resource_count || meta.size_kb || job.region) {
        const chips = [];
        if (job.region) chips.push(`<span class="activity-meta-chip" title="Azure Region">📍 ${escapeHtml(job.region)}</span>`);
        if (meta.size_kb) chips.push(`<span class="activity-meta-chip" title="ARM Template Size">📄 ${meta.size_kb} KB</span>`);
        if (meta.resource_count) chips.push(`<span class="activity-meta-chip" title="Resource Count">📦 ${meta.resource_count} resource(s)</span>`);
        if (meta.resource_types && meta.resource_types.length > 0) {
            meta.resource_types.slice(0, 4).forEach(rt => {
                const shortType = rt.split('/').pop() || rt;
                chips.push(`<span class="activity-meta-chip activity-meta-resource" title="${escapeHtml(rt)}">⚙️ ${escapeHtml(shortType)}</span>`);
            });
        }
        if (meta.schema) chips.push(`<span class="activity-meta-chip" title="Template Schema">📋 ${escapeHtml(meta.schema)}</span>`);
        if (meta.has_policy) chips.push(`<span class="activity-meta-chip" title="Has Policy Gate">🛡️ Policy</span>`);
        metaHtml = `<div class="activity-meta-chips">${chips.join('')}</div>`;
    }

    // ── Progress bar for running jobs ────────────────────────
    let progressHtml = '';
    if (isRunning) {
        const pct = Math.min(Math.round(job.progress * 100), 100);
        progressHtml = `
            <div class="activity-progress">
                <div class="activity-progress-track">
                    <div class="activity-progress-fill ${isRunning ? 'activity-progress-animated' : ''}" style="width: ${pct}%"></div>
                </div>
                <span class="activity-progress-pct">${pct}%</span>
            </div>`;
    }

    // ── Event log (expanded for running and failed, collapsed for approved) ──
    let eventsHtml = '';
    if (job.events && job.events.length > 0) {
        const collapsed = !isRunning && status !== 'validation_failed';
        const eventLines = job.events.map(e => {
            let icon = '▸';
            if (e.type === 'error') icon = '❌';
            else if (e.type === 'done') icon = '✅';
            else if (e.type === 'healing') icon = '🤖';
            else if (e.type === 'healing_done') icon = '🔧';
            else if (e.type === 'init') icon = '🚦';
            else if (e.phase === 'what_if') icon = '🔍';
            else if (e.phase === 'what_if_complete') icon = '✓';
            else if (e.phase === 'deploying') icon = '🚀';
            else if (e.phase === 'deploy_complete') icon = '📦';
            else if (e.phase === 'deploy_failed') icon = '💥';
            else if (e.phase === 'resource_check') icon = '🔎';
            else if (e.phase === 'resource_check_complete') icon = '✓';
            else if (e.phase === 'policy_testing') icon = '🛡️';
            else if (e.phase === 'policy_failed') icon = '⚠️';
            else if (e.phase === 'cleanup') icon = '🧹';
            else if (e.phase === 'cleanup_complete') icon = '✓';
            else if (e.phase === 'promoting') icon = '🏆';
            else if (e.phase === 'infra_retry') icon = '⏳';
            const timeStr = e.time ? `<span class="activity-event-time">${_timeShort(e.time)}</span>` : '';
            return `<div class="activity-event-line">${timeStr}${icon} ${escapeHtml(e.detail)}</div>`;
        }).join('');
        const chevronChar = collapsed ? '▸' : '▾';
        eventsHtml = `
            <div class="activity-events-toggle" onclick="this.nextElementSibling.classList.toggle('hidden'); this.querySelector('.chevron').textContent = this.nextElementSibling.classList.contains('hidden') ? '▸' : '▾'">
                <span class="chevron">${chevronChar}</span> ${job.events.length} event${job.events.length !== 1 ? 's' : ''} — full validation log
            </div>
            <div class="activity-events ${collapsed ? 'hidden' : ''}" id="activity-events-${escapeHtml(job.service_id)}">${eventLines}</div>`;
    }

    // ── Error display ────────────────────────────────────────
    let errorHtml = '';
    if (status === 'validation_failed' && job.error) {
        const parsed = _parseValidationError(job.error);
        errorHtml = _renderStructuredError(parsed, { compact: false, showRaw: true });
    }

    // ── Time display ─────────────────────────────────────────
    let timeHtml = '';
    if (job.started_at) {
        timeHtml = `<span class="activity-time" title="${job.started_at}">Started ${_timeAgo(job.started_at)}</span>`;
    }

    // ── RG & region for running jobs ─────────────────────────
    let rgHtml = '';
    if (isRunning && job.rg_name) {
        rgHtml = `<div class="activity-rg-bar"><span class="activity-rg-label">Resource Group:</span> <span class="activity-rg-name">${escapeHtml(job.rg_name)}</span></div>`;
    }

    // ── Action buttons ───────────────────────────────────────
    let actionsHtml = '';
    if (status === 'validation_failed') {
        actionsHtml = `<button class="btn btn-xs btn-primary" onclick="navigateTo('services'); setTimeout(() => showServiceDetail('${escapeHtml(job.service_id)}'), 200)">🤖 Retry Validation</button>`;
    } else if (status === 'validating' && !isRunning) {
        actionsHtml = `<button class="btn btn-xs btn-accent" onclick="navigateTo('services'); setTimeout(() => showServiceDetail('${escapeHtml(job.service_id)}'), 200)">🚀 Start Validation</button>`;
    } else if (status === 'approved') {
        actionsHtml = `<button class="btn btn-xs btn-ghost" onclick="navigateTo('services'); setTimeout(() => showServiceDetail('${escapeHtml(job.service_id)}'), 200)">View Service</button>`;
    }

    return `
    <div class="activity-card ${statusClass} ${isRunning ? 'activity-card-running' : ''}">
        <div class="activity-card-header">
            <div class="activity-card-title">
                <span class="activity-card-icon">${statusIcon}</span>
                <div class="activity-card-name">
                    <span class="activity-svc-name">${escapeHtml(job.service_name)}</span>
                    <span class="activity-svc-id">${escapeHtml(job.service_id)}</span>
                </div>
            </div>
            <div class="activity-card-meta">
                <span class="activity-badge ${statusClass}">${statusText}</span>
                ${timeHtml}
            </div>
        </div>
        ${metaHtml}
        ${pipelineHtml}
        ${phaseHtml}
        ${detailHtml}
        ${progressHtml}
        ${rgHtml}
        ${errorHtml}
        ${eventsHtml}
        <div class="activity-card-actions">${actionsHtml}</div>
    </div>`;
}

function _timeAgo(isoStr) {
    if (!isoStr) return '';
    const now = Date.now();
    const then = new Date(isoStr).getTime();
    const diff = Math.floor((now - then) / 1000);
    if (diff < 60) return 'just now';
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86400)}d ago`;
}

function _timeShort(isoStr) {
    if (!isoStr) return '';
    try {
        const d = new Date(isoStr);
        return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    } catch { return ''; }
}
