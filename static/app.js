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
        activity: ['Activity Monitor', 'Deployment validation observability'],
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

    // Load activity when switching to activity page
    if (page === 'activity') {
        loadActivity();
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
            actions.innerHTML = '<button class="btn btn-sm btn-primary" onclick="openAddStandardModal()">＋ Add Standard</button>';
            break;
        case 'activity':
            actions.innerHTML = '<button class="btn btn-sm btn-ghost" onclick="loadActivity()" title="Refresh">⟳ Refresh</button>';
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

    body.innerHTML = `
        <div class="detail-meta">
            <span class="svc-id">${escapeHtml(svc.id)}</span>
            <span class="status-badge ${status}">${statusLabels[status] || status}</span>
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
            <p class="pipeline-desc">All steps run automatically with AI-powered auto-healing (up to 5 attempts). Validated against organization governance standards &amp; policies.</p>
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
    if (status === 'approved' && latestVersion) {
        return `
        <div class="validation-card validation-succeeded">
            <div class="validation-header">
                <span class="validation-icon">✅</span>
                <span class="validation-title">Service Approved — v${latestVersion.version}</span>
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
            errorDetail = '<div class="validation-detail">The previous onboarding attempt failed. No error details available.</div>';
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
    // Only show approved (working) versions — failed/draft/validating are internal
    const approvedVersions = versions.filter(v => v.status === 'approved');
    const totalCount = versions.length;
    const approvedCount = approvedVersions.length;

    if (approvedCount === 0) {
        return `
        <div class="version-history">
            <div class="version-history-header">
                <span>📦 Published Versions</span>
                <span class="version-count">No approved versions yet (${totalCount} total attempt${totalCount === 1 ? '' : 's'})</span>
            </div>
        </div>`;
    }

    return `
    <div class="version-history">
        <div class="version-history-header">
            <span>📦 Published Versions</span>
            <span class="version-count">${approvedCount} approved version${approvedCount === 1 ? '' : 's'}</span>
        </div>
        <div class="version-list">
            ${approvedVersions.map(v => {
                const isActive = v.version === activeVersion;
                const sizeKB = v.template_size_bytes
                    ? (v.template_size_bytes / 1024).toFixed(1)
                    : v.arm_template
                        ? (v.arm_template.length / 1024).toFixed(1)
                        : '?';

                return `
                <div class="version-item ${isActive ? 'version-item-active' : ''}" onclick="toggleVersionDetail(this)">
                    <div class="version-item-header">
                        <span class="version-item-badge">v${v.version}</span>
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
        </div>
    </div>`;
}

// ── Template Viewer ─────────────────────────────────────────

let _currentTemplateContent = '';
let _currentTemplateFilename = '';

async function viewTemplate(serviceId, version) {
    const modal = document.getElementById('modal-template-viewer');
    const title = document.getElementById('template-viewer-title');
    const meta = document.getElementById('template-viewer-meta');
    const code = document.getElementById('template-viewer-code');

    title.textContent = `ARM Template — v${version}`;
    meta.innerHTML = `<span class="template-meta-badge">📦 ${escapeHtml(serviceId)}</span><span class="template-meta-badge">v${version}</span><span class="template-meta-loading">Loading…</span>`;
    code.querySelector('code').textContent = 'Loading template…';
    _currentTemplateContent = '';
    _currentTemplateFilename = `${serviceId.replace(/\//g, '_')}_v${version}.json`;

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

        // Update meta
        const sizeKB = (formatted.length / 1024).toFixed(1);
        const resourceCount = (formatted.match(/"type"\s*:/g) || []).length;
        const validatedAt = data.validated_at ? data.validated_at.substring(0, 10) : '—';
        meta.innerHTML = `
            <span class="template-meta-badge">📦 ${escapeHtml(serviceId)}</span>
            <span class="template-meta-badge">v${version}</span>
            <span class="template-meta-badge">${sizeKB} KB</span>
            <span class="template-meta-badge">~${resourceCount} resource type ref${resourceCount === 1 ? '' : 's'}</span>
            <span class="template-meta-badge">Validated: ${validatedAt}</span>
        `;
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

function toggleVersionDetail(el) {
    const detail = el.querySelector('.version-item-detail');
    if (detail) detail.classList.toggle('hidden');
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
    if (event.attempt && badge) {
        badge.textContent = `Attempt ${event.attempt}${event.max_attempts ? ' / ' + event.max_attempts : ''}`;
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
        if (badge && event.total_attempts > 1) {
            badge.textContent = `Passed on attempt ${event.total_attempts}`;
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
    const statusLabelsMap = { approved: '✅ Approved', draft: '📝 Draft', deprecated: '⚠️ Deprecated' };

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

    document.getElementById('detail-template-name').textContent = tmpl.name;
    document.getElementById('detail-template-body').innerHTML = `
        <div class="detail-meta">
            <span class="svc-id">${escapeHtml(tmpl.id)}</span>
            <span class="tmpl-type-badge tmpl-type-${ttype}">${typeIcons[ttype] || '📋'} ${typeLabels[ttype] || ttype}</span>
            <span class="status-badge ${status}">${statusLabels[status] || status}</span>
            <span class="tmpl-standalone-badge ${isStandalone ? 'standalone-yes' : 'standalone-no'}">
                ${isStandalone ? '✅ Standalone' : '⚙️ Needs existing infra'}
            </span>
        </div>

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
            <h4>⚠️ Requires Existing Infrastructure</h4>
            <div class="tmpl-dep-list">
                ${requires.map(r => `
                    <div class="tmpl-dep-item tmpl-dep-required">
                        <strong>${_shortType(r.type || r)}</strong>
                        <span>${escapeHtml(r.reason || '')}</span>
                        <code>${escapeHtml(r.parameter || '')}</code>
                    </div>
                `).join('')}
            </div>
            <p class="tmpl-dep-note">At deploy time, InfraForge will show a resource picker for each required dependency.</p>
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

        ${(tmpl.service_ids && tmpl.service_ids.length) ? `
        <div class="detail-section">
            <h4>Composed From Services</h4>
            <div class="detail-tags">${tmpl.service_ids.map(s => `<span class="region-tag">${escapeHtml(s)}</span>`).join('')}</div>
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

// ── Template Composition from Approved Services ─────────────

let _approvedServicesForCompose = [];
let _composeSelections = new Map(); // service_id -> { quantity, parameters: Set }

async function openTemplateOnboarding() {
    document.getElementById('modal-template-onboard').classList.remove('hidden');
    _composeSelections.clear();
    _updateComposeSubmitButton();

    const list = document.getElementById('compose-service-list');
    list.innerHTML = '<div class="compose-loading">Loading approved services…</div>';

    try {
        const res = await fetch('/api/catalog/services/approved-for-templates');
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
        const extraParams = svc.parameters.filter(p => !p.is_standard);
        return `
        <div class="compose-svc-card ${selected ? 'compose-svc-selected' : ''}"
             onclick="toggleComposeService('${escapeHtml(svc.id)}')"
             data-service-id="${escapeHtml(svc.id)}">
            <div class="compose-svc-card-main">
                <div class="compose-svc-check">${selected ? '☑' : '☐'}</div>
                <div class="compose-svc-info">
                    <div class="compose-svc-name">${escapeHtml(svc.name)}</div>
                    <div class="compose-svc-id">${escapeHtml(svc.id)}</div>
                </div>
                <span class="category-badge">${escapeHtml(svc.category)}</span>
                <span class="version-badge version-active">v${svc.active_version || '?'}</span>
                ${extraParams.length ? `<span class="compose-param-count">${extraParams.length} param${extraParams.length !== 1 ? 's' : ''}</span>` : ''}
            </div>
        </div>`;
    }).join('');
}

function toggleComposeService(serviceId) {
    if (_composeSelections.has(serviceId)) {
        _composeSelections.delete(serviceId);
    } else {
        _composeSelections.set(serviceId, { quantity: 1, parameters: new Set() });
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
        const extraParams = svc.parameters.filter(p => !p.is_standard);

        return `
        <div class="compose-selection-card">
            <div class="compose-selection-header">
                <div class="compose-selection-title">
                    <span class="compose-svc-name">${escapeHtml(svc.name)}</span>
                    <button type="button" class="btn btn-xs btn-ghost" onclick="toggleComposeService('${escapeHtml(sid)}')" title="Remove">✕</button>
                </div>
                <div class="compose-qty-row">
                    <label>Quantity:</label>
                    <button type="button" class="compose-qty-btn" onclick="adjustComposeQty('${escapeHtml(sid)}', -1)">−</button>
                    <span class="compose-qty-val" id="compose-qty-${sid.replace(/[/.]/g, '-')}">${sel.quantity}</span>
                    <button type="button" class="compose-qty-btn" onclick="adjustComposeQty('${escapeHtml(sid)}', 1)">+</button>
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
            ? `Create Template (${count} service${count !== 1 ? 's' : ''})`
            : 'Create Template';
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
        const typeLabels = { foundation: 'Foundation — deploys standalone', workload: 'Workload — requires existing infrastructure', composite: 'Composite — self-contained bundle' };

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
            html += '<div class="dep-block"><h5>⚠️ Requires Existing Infrastructure</h5>';
            html += '<p class="dep-note">These resources must already exist. InfraForge will show a resource picker at deploy time.</p>';
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
            html += '<div class="dep-standalone-no">⚠️ This template requires existing infrastructure. Users will need to select resources at deploy time.</div>';
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
    const origText = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Composing…';

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
    }));

    const body = {
        name: name,
        description: (fd.get('description') || '').trim(),
        category: fd.get('category') || 'blueprint',
        selections: selections,
    };

    try {
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
        showToast(`Template "${name}" created — ${data.resource_count} resource(s), ${data.parameter_count} parameter(s)`);
        closeModal('modal-template-onboard');
        form.reset();
        _composeSelections.clear();
        await loadAllData();
    } catch (err) {
        showToast(err.message, 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = origText;
        _updateComposeSubmitButton();
    }
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


// ══════════════════════════════════════════════════════════════
// ACTIVITY MONITOR
// ══════════════════════════════════════════════════════════════

let _activityPollTimer = null;

function _startActivityPolling() {
    _stopActivityPolling();
    _activityPollTimer = setInterval(() => loadActivity(true), 3000);
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
