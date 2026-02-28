/**
 * InfraForge — Web UI Client
 *
 * Multi-page app with traditional navigation for browsing (services, templates)
 * and AI chat for complex design tasks (infrastructure generation).
 */

// ── Technology Branding Badges ────────────────────────────────
function _copilotBadge(full) {
    return full
        ? '<span class="tech-badge-copilot tech-badge-copilot-lg">✦ GitHub Copilot SDK</span>'
        : '<span class="tech-badge-copilot">✦ Copilot SDK</span>';
}

function _fabricBadge(full) {
    return full
        ? '<span class="tech-badge-fabric tech-badge-fabric-lg">◆ Microsoft Fabric</span>'
        : '<span class="tech-badge-fabric">◆ Fabric IQ</span>';
}

// Inline tag for flow card headers
function _copilotTag() {
    return '<span class="uf-tech-tag uf-tech-tag-copilot">COPILOT SDK</span>';
}

function _fabricTag() {
    return '<span class="uf-tech-tag uf-tech-tag-fabric">FABRIC IQ</span>';
}

// ── Work IQ — Identity-Aware Infrastructure Intelligence ────
function _populateWorkIQ(user) {
    // Identity context
    const _set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val || '—'; };
    _set('wiq-name', user.displayName);
    _set('wiq-email', user.email);
    _set('wiq-dept', user.department);
    _set('wiq-cc', user.costCenter);
    _set('wiq-mgr', user.manager || 'Via Graph API');
    _set('wiq-access', user.isPlatformTeam ? 'Platform Team' : user.isAdmin ? 'Admin' : 'Standard');

    // Auto-tagging preview
    _set('wiq-tag-owner', user.email);
    _set('wiq-tag-cc', user.costCenter || 'TBD');
    _set('wiq-tag-dept', user.department || 'TBD');
    _set('wiq-tag-name', user.displayName);

    // Approval routing
    _set('wiq-route-you', user.displayName.split(' ')[0] || 'You');
    _set('wiq-route-mgr', user.manager || 'Manager');

    // Usage analytics (async)
    _loadWorkIQStats();
}

async function _loadWorkIQStats() {
    try {
        const token = localStorage.getItem('session_token');
        if (!token) return;
        const res = await fetch('/api/analytics/usage', {
            headers: { 'Authorization': `Bearer ${token}` },
        });
        if (!res.ok) return;
        const data = await res.json();
        const _set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
        _set('wiq-total-requests', data.totalRequests || 0);
        _set('wiq-catalog-rate', (data.catalogReuseRate || 0) + '%');
        _set('wiq-est-cost', '$' + (data.totalEstimatedMonthlyCost || 0).toLocaleString());
    } catch (e) {
        // Non-critical — analytics loading failure is okay
    }
}

// ── Workflow Pipeline Renderer ──────────────────────────────
/**
 * Render a standardized dot-line workflow pipeline.
 *
 * @param {Array<{key?: string, icon?: string, label: string, desc?: string}>} steps
 * @param {Object} opts
 * @param {string}  [opts.activeKey]     - key of the currently-active step
 * @param {Array}   [opts.completedKeys] - array of completed step keys
 * @param {string}  [opts.failedKey]     - key of the failed step
 * @param {number}  [opts.progress]      - 1-based index: steps <= progress are done
 * @param {boolean} [opts.allDone]       - mark every step done (e.g. approved template)
 * @param {boolean} [opts.compact]       - use smaller dot variant
 * @param {string}  [opts.title]         - optional pipeline title (shows boxed wrapper)
 * @param {string}  [opts.titleAccent]   - 'amber' etc. for accent coloring
 * @param {string}  [opts.desc]          - optional description below pipeline
 * @param {boolean} [opts.copilotBadge]  - show copilot badge in title
 * @returns {string} HTML string
 */
function _wfPipeline(steps, opts = {}) {
    const {
        activeKey, completedKeys = [], failedKey,
        progress, allDone, compact,
        title, titleAccent, desc, copilotBadge
    } = opts;

    const icons = {
        done: '✓',
        fail: '✕',
    };

    const items = steps.map((s, i) => {
        const key = s.key || s.label;
        let state = 'wf-pending';

        if (allDone) {
            state = 'wf-done';
        } else if (progress != null) {
            // Progress-based (lifecycle cards): 1-indexed
            const step = i + 1;
            if (failedKey && step === progress) state = 'wf-failed';
            else if (step < progress || (step <= progress && !failedKey)) state = 'wf-done';
        } else {
            // Key-based (activity pipelines)
            if (completedKeys.includes(key)) state = 'wf-done';
            else if (key === activeKey) state = 'wf-active';
            else if (key === failedKey) state = 'wf-failed';
        }

        const dotContent = state === 'wf-done' ? icons.done
            : state === 'wf-failed' ? icons.fail
            : (s.icon || `${i + 1}`);

        const node = `<div class="wf-node ${state}" title="${s.desc || s.label}">` +
            `<div class="wf-dot"><span class="wf-dot-inner">${dotContent}</span></div>` +
            `<span class="wf-label">${s.label}</span>` +
            `</div>`;

        // Connector before this node (not for the first)
        if (i === 0) return node;

        // Connector state: done if THIS node is done/active, active if this node is active
        let connState = '';
        if (state === 'wf-done' || allDone) connState = 'wf-done';
        else if (state === 'wf-active') connState = 'wf-active';
        else if (state === 'wf-failed') connState = 'wf-failed';

        return `<div class="wf-connector ${connState}"></div>${node}`;
    }).join('');

    const compactCls = compact ? ' wf-compact' : '';
    const pipeline = `<div class="wf-pipeline${compactCls}">${items}</div>`;

    if (!title && !desc) return pipeline;

    const accentCls = titleAccent ? ` wf-accent-${titleAccent}` : '';
    const badge = copilotBadge ? ` ${_copilotBadge()}` : '';
    return `<div class="wf-pipeline-box${accentCls}">` +
        (title ? `<div class="wf-pipeline-title">${title}${badge}</div>` : '') +
        pipeline +
        (desc ? `<div class="wf-pipeline-desc">${desc}</div>` : '') +
        `</div>`;
}

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
let _serviceUpdates = {};  // serviceId → update info from check-updates
let currentCategoryFilter = 'all';
let currentStatusFilter = 'all';
let currentTemplateFilter = 'all';
let currentTemplateTypeFilter = 'all';
let serviceSearchQuery = '';
let templateSearchQuery = '';

// Active template validation tracker — persists across panel close/reopen
// templateId → { running: bool, events: [], finalEvent: null, abortController: AbortController }
const _activeTemplateValidations = {};

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

        // Populate Work IQ dashboard
        _populateWorkIQ(currentUser);
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
        services: ['Service Catalog', ''],
        templates: ['Template Catalog', ''],
        governance: ['Governance Standards', ''],
        activity: ['Observability', ''],
        analytics: ['Fabric Analytics', ''],
        chat: ['Infrastructure Designer', ''],
    };
    // Tech-branded subtitles (as HTML badges)
    const subtitleBadges = {
        services: _copilotBadge(true),
        templates: _copilotBadge(true),
        governance: _copilotBadge(true),
        activity: _copilotBadge(false) + ' ' + _fabricBadge(false),
        analytics: _fabricBadge(true),
        chat: _copilotBadge(true),
    };
    const [title, subtitle] = titles[page] || ['InfraForge', ''];
    document.getElementById('page-title').textContent = title;
    const subtitleEl = document.getElementById('page-subtitle');
    if (subtitleBadges[page]) {
        subtitleEl.innerHTML = subtitleBadges[page];
    } else {
        subtitleEl.textContent = subtitle;
    }

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

    // Load analytics when switching to analytics page
    if (page === 'analytics') {
        loadAnalyticsDashboard();
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
        case 'analytics':
            actions.innerHTML = '<button class="btn btn-sm btn-ghost" onclick="loadAnalyticsDashboard()" title="Refresh">⟳ Refresh</button>';
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
        const [svcRes, tmplRes, approvalRes, verRes] = await Promise.all([
            fetch('/api/catalog/services'),
            fetch('/api/catalog/templates'),
            fetch('/api/approvals'),
            fetch('/api/version'),
        ]);

        const svcData = await svcRes.json();
        const tmplData = await tmplRes.json();
        const approvalData = await approvalRes.json();
        const verData = await verRes.json();

        // Display app version in the sidebar footer
        const versionBadge = document.getElementById('app-version-badge');
        if (versionBadge && verData.version) {
            versionBadge.textContent = `v${verData.version}`;
        }

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
        _populateServiceUpdatesFromCache();
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
    offboarded: '📦 Offboarded',
};

/** Auto-populate _serviceUpdates from cached DB data — no Azure call needed */
function _populateServiceUpdatesFromCache() {
    // Don't overwrite if the user just ran a live check (preserves richer data)
    if (_lastApiVersionCheck && (Date.now() - _lastApiVersionCheck < 5000)) return;

    const cached = {};
    allServices.forEach(svc => {
        if (!svc.active_version || !svc.latest_api_version || !svc.template_api_version) return;
        if (svc.latest_api_version > svc.template_api_version) {
            cached[svc.id] = {
                id: svc.id,
                name: svc.name,
                category: svc.category,
                active_version: svc.active_version,
                template_api_version: svc.template_api_version,
                latest_api_version: svc.latest_api_version,
                default_api_version: svc.default_api_version,
            };
        }
    });
    if (Object.keys(cached).length > 0 || Object.keys(_serviceUpdates).length === 0) {
        _serviceUpdates = cached;
    }
    _updateCheckButton();
}

let _lastApiVersionCheck = null;

/** Update the check-for-updates button to reflect current state */
function _updateCheckButton() {
    const btn = document.getElementById('btn-check-updates');
    if (!btn) return;
    const count = Object.keys(_serviceUpdates).length;
    const badge = document.getElementById('update-count-badge');

    if (count > 0) {
        btn.innerHTML = `<span class="update-btn-icon">⬆</span> ${count} Update${count !== 1 ? 's' : ''} Found`;
        btn.classList.add('has-updates');
        if (badge) { badge.textContent = count; badge.classList.remove('hidden'); }
    }

    if (_lastApiVersionCheck) {
        const ago = _timeAgo(new Date(_lastApiVersionCheck).toISOString());
        btn.title = `Last checked: ${ago}. Click to refresh from Azure.`;
    }
}

function renderServiceTable(services) {
    const tbody = document.getElementById('catalog-tbody');

    // Update results summary
    const summary = document.getElementById('service-results-summary');
    if (summary) {
        const updateCount = Object.keys(_serviceUpdates).length;
        const updateSuffix = updateCount > 0
            ? ` — <span class="svc-update-summary">${updateCount} update${updateCount !== 1 ? 's' : ''} available</span>`
            : '';
        summary.innerHTML = `Showing ${services.length} of ${allServices.length} services${updateSuffix}`;
    }

    if (!services.length) {
        tbody.innerHTML = '<tr><td colspan="6" class="catalog-loading">No services match your filters</td></tr>';
        return;
    }

    tbody.innerHTML = services.map(svc => {
        const status = svc.status || 'not_approved';
        const activeVer = svc.active_version;
        const update = _serviceUpdates[svc.id];

        // Version display: show template API version (e.g. 2025-04-01) instead of vN
        const tplApi = svc.template_api_version
            || (update ? update.template_api_version : null);
        const versionLabel = tplApi || (activeVer ? `v${activeVer}` : null);

        // Check if recommended version differs from template (regardless of latest)
        const recApi = svc.default_api_version;
        const showRecBadge = recApi && recApi !== tplApi && recApi !== svc.latest_api_version;

        let versionHtml;
        if (versionLabel && update) {
            const badgeId = `update-badge-${svc.id.replace(/[^a-zA-Z0-9]/g, '-')}`;
            if (showRecBadge) {
                // Two update targets: latest stable AND recommended
                const recLabel = recApi < tplApi ? '★ rec ↓' : '★ rec ↑';
                versionHtml = `<span class="version-badge version-active" title="Template API version">${escapeHtml(versionLabel)}</span>`
                    + `<span class="version-badge version-update version-update-clickable" id="${badgeId}" title="Update to latest stable: ${escapeHtml(update.latest_api_version)}" onclick="event.stopPropagation(); startApiVersionUpdateFromTable('${escapeHtml(svc.id)}', '${badgeId}', '${escapeHtml(update.latest_api_version)}')">⬆ latest</span>`
                    + `<span class="version-badge version-update version-update-rec version-update-clickable" id="${badgeId}-rec" title="${recApi < tplApi ? 'Downgrade' : 'Update'} to Microsoft recommended: ${escapeHtml(recApi)}" onclick="event.stopPropagation(); startApiVersionUpdateFromTable('${escapeHtml(svc.id)}', '${badgeId}-rec', '${escapeHtml(recApi)}')">${recLabel}</span>`;
            } else {
                versionHtml = `<span class="version-badge version-active" title="Template API version">${escapeHtml(versionLabel)}</span>`
                    + `<span class="version-badge version-update version-update-clickable" id="${badgeId}" title="Click to update: ${escapeHtml(update.template_api_version)} → ${escapeHtml(update.latest_api_version)}" onclick="event.stopPropagation(); startApiVersionUpdateFromTable('${escapeHtml(svc.id)}', '${badgeId}')">⬆ update</span>`;
            }
        } else if (versionLabel && showRecBadge) {
            // No latest update available but recommended differs — show standalone rec badge
            const badgeId = `update-badge-${svc.id.replace(/[^a-zA-Z0-9]/g, '-')}-rec`;
            const recLabel = recApi < tplApi ? '★ rec ↓' : '★ rec ↑';
            versionHtml = `<span class="version-badge version-active" title="Template API version">${escapeHtml(versionLabel)}</span>`
                + `<span class="version-badge version-update version-update-rec version-update-clickable" id="${badgeId}" title="${recApi < tplApi ? 'Downgrade' : 'Update'} to Microsoft recommended: ${escapeHtml(recApi)}" onclick="event.stopPropagation(); startApiVersionUpdateFromTable('${escapeHtml(svc.id)}', '${badgeId}', '${escapeHtml(recApi)}')">${recLabel}</span>`;
        } else if (versionLabel) {
            versionHtml = `<span class="version-badge version-active" title="Template API version">${escapeHtml(versionLabel)}</span>`;
        } else {
            versionHtml = `<span class="version-badge version-none" title="No approved version">—</span>`;
        }

        // Azure API version column — show latest stable + recommended (when different)
        const azureApi = svc.latest_api_version;
        const defaultApi = svc.default_api_version;
        const tplApiCurrent = svc.template_api_version;
        let azureApiHtml;
        if (azureApi) {
            const isRecommended = defaultApi && defaultApi === azureApi;
            const isCurrent = tplApiCurrent && tplApiCurrent >= azureApi;
            const isOnRecommended = defaultApi && tplApiCurrent && tplApiCurrent === defaultApi;
            const hasSeparateDefault = defaultApi && defaultApi !== azureApi;
            let lines = '';
            // Line 1: Latest stable
            lines += `<span class="azure-api-line">`;
            lines += `<span class="azure-api-badge${isCurrent ? ' azure-api-match' : ''}" title="Latest stable API version">${escapeHtml(azureApi)}${isCurrent ? '<span class="azure-api-current" title="Template is on this version">✓</span>' : ''}</span>`;
            lines += `<span class="azure-api-label">latest</span>`;
            lines += `</span>`;
            // Line 2: Recommended (only if different from latest)
            if (hasSeparateDefault) {
                lines += `<span class="azure-api-line">`;
                lines += `<span class="azure-api-badge${isOnRecommended ? ' azure-api-match' : ''}" title="Microsoft recommended default">${escapeHtml(defaultApi)}<span class="azure-api-rec">★</span>${isOnRecommended ? '<span class="azure-api-current" title="Template is on recommended">✓</span>' : ''}</span>`;
                lines += `<span class="azure-api-label">recommended</span>`;
                lines += `</span>`;
            } else if (isRecommended) {
                // Latest IS the recommended — show star on the same line
                lines = `<span class="azure-api-line">`;
                lines += `<span class="azure-api-badge${isCurrent ? ' azure-api-match' : ''}" title="Latest stable & Microsoft recommended">${escapeHtml(azureApi)}<span class="azure-api-rec">★</span>${isCurrent ? '<span class="azure-api-current">✓</span>' : ''}</span>`;
                lines += `</span>`;
            }
            azureApiHtml = `<div class="azure-api-stack">${lines}</div>`;
        } else {
            azureApiHtml = `<span class="azure-api-badge azure-api-none" title="Run Check for Updates to populate">—</span>`;
        }

        return `<tr onclick="showServiceDetail('${escapeHtml(svc.id)}')">
            <td>
                <div class="svc-name">${escapeHtml(svc.name)}</div>
                <div class="svc-id">${escapeHtml(svc.id)}</div>
            </td>
            <td><span class="category-badge">${escapeHtml(svc.category)}</span></td>
            <td>${svc.latest_semver ? `<span class="version-badge version-semver">${escapeHtml(svc.latest_semver)}</span>` : (svc.active_version ? `<span class="version-badge version-semver-int">v${svc.active_version}</span>` : '<span class="version-badge version-none">—</span>')}</td>
            <td>${versionHtml}</td>
            <td>${azureApiHtml}</td>
            <td><span class="status-badge ${status}">${statusLabels[status] || status}</span></td>
        </tr>`;
    }).join('');
}

async function checkForServiceUpdates() {
    const btn = document.getElementById('btn-check-updates');
    const badge = document.getElementById('update-count-badge');
    if (!btn) return;

    btn.disabled = true;
    btn.innerHTML = '<span class="update-btn-icon spin">⟳</span> Checking…';
    badge?.classList.add('hidden');

    try {
        const res = await fetch('/api/catalog/services/check-updates');
        const data = await res.json();

        // Build lookup map
        _serviceUpdates = {};
        (data.updates || []).forEach(u => { _serviceUpdates[u.id] = u; });

        // Merge latest API versions into allServices so the Azure API column populates
        const apiMap = data.all_api_versions || {};
        if (Object.keys(apiMap).length > 0) {
            allServices.forEach(svc => {
                const info = apiMap[svc.id];
                if (info) {
                    svc.latest_api_version = info.latest_api_version;
                    svc.default_api_version = info.default_api_version;
                }
            });
        }

        // Merge template API versions (the apiVersion from the ARM template)
        const tplMap = data.template_api_versions || {};
        if (Object.keys(tplMap).length > 0) {
            allServices.forEach(svc => {
                if (tplMap[svc.id]) svc.template_api_version = tplMap[svc.id];
            });
        }

        const count = data.updates_available || 0;
        _lastApiVersionCheck = Date.now();

        if (count > 0) {
            btn.innerHTML = `<span class="update-btn-icon">⬆</span> ${count} Update${count !== 1 ? 's' : ''} Found`;
            btn.classList.add('has-updates');
            if (badge) {
                badge.textContent = count;
                badge.classList.remove('hidden');
            }
        } else {
            btn.innerHTML = '<span class="update-btn-icon">✓</span> All Up to Date';
            btn.classList.remove('has-updates');
        }
        _updateCheckButton();

        // Re-render table to show update badges
        applyServiceFilters();
    } catch (err) {
        console.warn('Failed to check for updates:', err);
        btn.innerHTML = '<span class="update-btn-icon">⬆</span> Check Failed';
    } finally {
        btn.disabled = false;
    }
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
let _pendingApiUpdate = null;
let _pendingApiUpdateTarget = null;  // Target version for the update (null = latest)
let _apiUpdateAbort = null;  // AbortController for drawer-initiated updates
let _runningTableUpdates = new Map();  // serviceId → AbortController for concurrent table updates
let _tableUpdateEventBuffers = new Map();  // serviceId → array of events (replayed when drawer opens)
let _openDrawerServiceId = null;  // currently open service detail drawer

async function startApiVersionUpdateFromTable(serviceId, badgeId, targetVersion) {
    // If this service already has a table update running, ignore
    if (_runningTableUpdates.has(serviceId)) {
        console.warn('[update] table update already running for', serviceId);
        return;
    }

    // Immediate visual feedback on the badge
    const badge = badgeId ? document.getElementById(badgeId) : null;
    if (badge) {
        badge.classList.add('version-update-running');
        badge.innerHTML = '<span class="update-badge-spinner"></span> Updating…';
        badge.onclick = (e) => { e.stopPropagation(); showServiceDetail(serviceId); };
        badge.style.pointerEvents = '';
        badge.title = 'Click to view update progress';
    }

    // If drawer is open for this service, switch validation card to running state
    const _drawerOpen = _openDrawerServiceId === serviceId;
    if (_drawerOpen) {
        _initRunningCardForTableUpdate(targetVersion, serviceId);
    }

    // Fire the update directly from the table — no drawer needed
    const abort = new AbortController();
    _runningTableUpdates.set(serviceId, { abort, targetVersion });
    _tableUpdateEventBuffers.set(serviceId, []);  // Start fresh buffer

    try {
        const body = {};
        if (targetVersion) body.target_version = targetVersion;

        const res = await fetch(`/api/services/${encodeURIComponent(serviceId)}/update-api-version`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
            signal: abort.signal, // from _runningTableUpdates entry
        });

        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'API version update failed');
        }

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let failed = false;

        const phaseLabels = {
            checkout: 'Checking out…', checkout_complete: 'Checked out',
            updating: 'Rewriting…', update_complete: 'Rewritten',
            saved: 'Saved draft',
            static_policy_check: 'Policy check…', static_policy_complete: 'Policies OK',
            static_policy_failed: 'Policy issues',
            what_if: 'What-If…', what_if_complete: 'What-If OK', what_if_failed: 'What-If issue',
            deploying: 'Deploying…', deploy_complete: 'Deployed', deploy_failed: 'Deploy issue',
            analyzing_deploy_failure: 'Analyzing…', analyzing_whatif_failure: 'Analyzing…',
            escalating: 'Escalating…',
            policy_testing: 'Compliance…', policy_testing_complete: 'Compliant',
            policy_deploy: 'Deploying policy…', policy_deploy_complete: 'Policy deployed',
            cleanup: 'Cleaning up…', cleanup_complete: 'Cleaned up',
            promoting: 'Publishing…', fixing_template: 'Healing…',
        };

        const updateBadge = (event) => {
            if (!badge) return;
            if (event.type === 'done') {
                badge.classList.remove('version-update-running');
                badge.classList.add('version-update-done');
                badge.innerHTML = '✓ Updated';
                completed = true;
            } else if (event.type === 'error') {
                badge.classList.remove('version-update-running');
                badge.classList.add('version-update-error');
                badge.innerHTML = '⚠ Failed';
                completed = true;
            } else if (event.phase && phaseLabels[event.phase]) {
                badge.innerHTML = `<span class="update-badge-spinner"></span> ${phaseLabels[event.phase]}`;
            }
        };

        let completed = false;  // Track whether we received a terminal event

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
                    if (event.type === 'error') failed = true;
                    updateBadge(event);
                    // Buffer event for replay if drawer opens later
                    const buf = _tableUpdateEventBuffers.get(serviceId);
                    if (buf) buf.push(event);
                    // Forward to drawer if open for this service
                    if (_openDrawerServiceId === serviceId) _handleUpdateEvent(event);
                } catch (e) {}
            }
        }
        if (buffer.trim()) {
            try {
                const last = JSON.parse(buffer);
                if (last.type === 'error') failed = true;
                updateBadge(last);
                const buf2 = _tableUpdateEventBuffers.get(serviceId);
                if (buf2) buf2.push(last);
                if (_openDrawerServiceId === serviceId) _handleUpdateEvent(last);
            } catch (e) {}
        }

        // Detect stream interruption — no terminal event received
        if (!completed && !failed) {
            showToast('Pipeline stream ended unexpectedly — the update may not have completed. Refreshing…', 'warning');
            if (badge) {
                badge.classList.remove('version-update-running');
                badge.classList.add('version-update-error');
                badge.innerHTML = '⚠ Interrupted';
            }
            // Send an interrupted event to the overlay so it shows a visible warning
            const interruptedEvt = { type: 'error', phase: 'interrupted', detail: 'Pipeline stream ended unexpectedly — update may not have completed. Check the service status and retry if needed.' };
            const buf3 = _tableUpdateEventBuffers.get(serviceId);
            if (buf3) buf3.push(interruptedEvt);
            if (_openDrawerServiceId === serviceId) {
                _handleUpdateEvent(interruptedEvt);
            }
        }

        // Refresh data regardless of outcome
        await loadAllData();

    } catch (err) {
        if (err.name !== 'AbortError') {
            showToast(`API version update failed: ${err.message}`, 'error');
        }
        if (badge) {
            badge.classList.remove('version-update-running');
            badge.classList.add('version-update-error');
            badge.innerHTML = '⚠ Failed';
        }
    } finally {
        _runningTableUpdates.delete(serviceId);
    }
}

/** Convert the validation-card to a running state when a table update starts while the drawer is open */
function _initRunningCardForTableUpdate(targetVersion, serviceId) {
    // Open pipeline overlay for table-triggered updates
    if (serviceId) {
        const svc = allServices.find(s => s.id === serviceId);
        const svcName = svc ? svc.name : serviceId;
        const targetLabel = targetVersion ? `to ${targetVersion}` : 'to latest';
        openPipelineOverlay('API Version Update', '⬆', `Updating ${svcName} ${targetLabel}…`);
    }

    let card = document.getElementById('validation-card');
    if (!card) {
        const body = document.getElementById('detail-service-body');
        if (body) {
            const div = document.createElement('div');
            div.id = 'validation-card';
            body.insertBefore(div, body.firstChild);
            card = div;
        }
    }
    if (!card) return;

    const targetLabel = targetVersion ? `to ${targetVersion}` : 'to latest';
    card.className = 'validation-card validation-running';
    card.innerHTML = `
        <div class="validation-header">
            <span class="validation-icon validation-spinner">⬆</span>
            <span class="validation-title">API Version Update ${targetLabel} In Progress…</span>
        </div>
        <div class="validation-model-badge" id="validation-model-badge"></div>
        <div class="validation-attempt-badge" id="validation-attempt-badge"></div>
        <div class="validation-progress">
            <div class="validation-progress-track">
                <div class="validation-progress-fill" id="validation-progress-fill" style="width: 0%"></div>
            </div>
        </div>
        <div class="validation-detail" id="validation-detail">Initializing API version update pipeline…</div>
        <div class="validation-log" id="validation-log"></div>
    `;
}

async function showServiceDetail(serviceId) {
    const svc = allServices.find(s => s.id === serviceId);
    if (!svc) return;

    _openDrawerServiceId = serviceId;

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
            ${svc.active_version ? `<span class="version-badge version-active">Active: ${svc.template_api_version || ('v' + svc.active_version)}</span>` : ''}
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
        _renderVersionedWorkflow(svc, _currentVersions, data.active_version, data.api_version_status, data.child_resources, data.parent_resource);
        // Populate model selector AFTER the DOM element exists
        _populateModelSelector();

        // Lazy-load pipeline runs
        _loadPipelineRuns(serviceId);

        // Lazy-load governance reviews
        _loadGovernanceReviews(serviceId);

        // If a table-initiated update is running or recently completed,
        // show the running card and replay buffered events
        const runningEntry = _runningTableUpdates.get(serviceId);
        const bufferedEvents = _tableUpdateEventBuffers.get(serviceId);
        if (runningEntry || (bufferedEvents && bufferedEvents.length > 0)) {
            const targetVer = runningEntry ? runningEntry.targetVersion : null;
            // Only open overlay for actively running streams, not completed replays
            if (runningEntry) {
                _initRunningCardForTableUpdate(targetVer, serviceId);
            } else {
                // Stream already ended — just create the card without overlay
                let card = document.getElementById('validation-card');
                if (!card) {
                    const body = document.getElementById('detail-service-body');
                    if (body) {
                        const div = document.createElement('div');
                        div.id = 'validation-card';
                        body.insertBefore(div, body.firstChild);
                        card = div;
                    }
                }
                if (card) {
                    const tLabel = targetVer ? `to ${targetVer}` : 'to latest';
                    card.className = 'validation-card validation-running';
                    card.innerHTML = `
                        <div class="validation-header">
                            <span class="validation-icon validation-spinner">⬆</span>
                            <span class="validation-title">API Version Update ${tLabel} In Progress…</span>
                        </div>
                        <div class="validation-model-badge" id="validation-model-badge"></div>
                        <div class="validation-attempt-badge" id="validation-attempt-badge"></div>
                        <div class="validation-progress">
                            <div class="validation-progress-track">
                                <div class="validation-progress-fill" id="validation-progress-fill" style="width: 0%"></div>
                            </div>
                        </div>
                        <div class="validation-detail" id="validation-detail">Loading…</div>
                        <div class="validation-log" id="validation-log"></div>
                    `;
                }
            }
            // Replay all buffered events so the flow cards appear
            if (bufferedEvents && bufferedEvents.length > 0) {
                for (const ev of bufferedEvents) {
                    _handleUpdateEvent(ev);
                }
                // Clear buffer after replay to prevent re-showing stale state
                if (!runningEntry) _tableUpdateEventBuffers.delete(serviceId);
            }
        }
    } catch (err) {
        body.innerHTML += `<p style="color: var(--accent-red);">Failed to load versions: ${err.message}</p>
            <button class="btn btn-primary" style="margin-top: 0.5rem;" onclick="showServiceDetail('${escapeHtml(serviceId)}')">🔄 Retry</button>`;
    }
}

function _renderApiVersionAdvisory(status) {
    if (!status) return '';
    if (!status.newer_available && !status.recommended_differs) return '';
    const hasSeparateDefault = status.default && status.default !== status.latest_stable
        && status.default !== status.template_api_version;
    const advisoryTitle = status.newer_available
        ? 'Newer Azure API version available'
        : 'Microsoft recommended version differs from template';
    return `
        <div class="api-version-advisory">
            <div class="api-version-advisory-icon">ℹ️</div>
            <div class="api-version-advisory-body">
                <div class="api-version-advisory-title">${advisoryTitle}</div>
                <div class="api-version-advisory-detail">
                    Template uses <code>${escapeHtml(status.template_api_version)}</code>
                    — Azure latest stable: <code>${escapeHtml(status.latest_stable)}</code>${hasSeparateDefault
                        ? ` · Microsoft recommended: <code>${escapeHtml(status.default)}</code> <span class="azure-api-rec">★</span>`
                        : (status.default === status.latest_stable ? ' <span class="azure-api-rec">★ recommended</span>' : '')}.
                </div>
                ${hasSeparateDefault ? `<div class="api-version-advisory-hint">★ The recommended version is Microsoft's default for new deployments — typically the safest choice for stability.</div>` : ''}
            </div>
        </div>`;
}

function _renderVersionedWorkflow(svc, versions, activeVersion, apiVersionStatus, childResources, parentResource) {
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
        { icon: '📜', label: 'Policy Deploy', desc: 'Deploy Azure Policy definition + assignment to enforce governance' },
        { icon: '✅', label: 'Approve', desc: 'Version approved, service active' },
    ];

    // Update pipeline steps (shown when API version update available)
    const updatePipelineSteps = [
        { icon: '📥', label: 'Checkout', desc: 'Read current active ARM template' },
        { icon: '🧠', label: 'Plan', desc: 'Reasoning model analyzes breaking changes between API versions' },
        { icon: '⚡', label: 'Execute', desc: 'Code gen model rewrites template guided by migration plan' },
        { icon: '📋', label: 'Static Check', desc: 'Static validation against org governance policies' },
        { icon: '🔍', label: 'What-If', desc: 'ARM What-If preview of deployment changes' },
        { icon: '🚀', label: 'Deploy', desc: 'Test deployment to validation resource group' },
        { icon: '🛡️', label: 'Policy Test', desc: 'Runtime policy compliance test on deployed resources' },
        { icon: '📜', label: 'Policy Deploy', desc: 'Deploy Azure Policy to enforce governance in Azure' },
        { icon: '🧹', label: 'Cleanup', desc: 'Delete validation resource group + policy' },
        { icon: '✅', label: 'Publish', desc: 'New version promoted to active' },
    ];

    const showUpdatePipeline = apiVersionStatus && (apiVersionStatus.newer_available || apiVersionStatus.recommended_differs) && status !== 'offboarded';

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
            ${activeVersion ? `<span class="version-badge version-active">Active: ${svc.template_api_version || ('v' + activeVersion)}</span>` : ''}
        </div>

        ${_renderApiVersionAdvisory(apiVersionStatus)}

        ${showUpdatePipeline ? _wfPipeline(updatePipelineSteps, {
            title: '⬆ API Version Update Pipeline',
            titleAccent: 'amber',
            copilotBadge: true,
            desc: `Updates template API version with auto-healing. Current: <code>${escapeHtml(apiVersionStatus.template_api_version)}</code> → Latest: <code>${escapeHtml(apiVersionStatus.latest_stable)}</code>${apiVersionStatus.default && apiVersionStatus.default !== apiVersionStatus.latest_stable ? ` · Recommended: <code>${escapeHtml(apiVersionStatus.default)}</code> <span class="azure-api-rec">★</span>` : ''}`,
        }) : ''}

        ${_wfPipeline(pipelineSteps, {
            title: 'Onboarding Pipeline',
            copilotBadge: true,
            desc: 'All steps run automatically with Copilot SDK-powered auto-healing. Validated against organization governance standards &amp; policies.',
        })}

        <div class="model-routing-display" id="model-routing-container">
            <span class="model-routing-label">🤖 Model Routing <span class="mr-sdk-tag">COPILOT SDK</span></span>
            <div class="model-routing-chips" id="model-routing-chips">Loading…</div>
        </div>

        ${_renderOnboardButton(svc, status, latestVersion, apiVersionStatus, versions, activeVersion)}

        ${_renderChildResources(childResources, parentResource)}

        <div id="pipeline-runs-container" class="pipeline-runs-section" style="display:none;"></div>

        <div id="governance-reviews-container" class="governance-reviews-section" style="display:none;"></div>

        ${hasVersions ? _renderVersionHistory(versions, activeVersion) : ''}
    `;
}

function _renderChildResources(childResources, parentResource) {
    let html = '';

    // Parent link (for child resources)
    if (parentResource) {
        const parentShort = parentResource.split('/').pop();
        const parentSvc = allServices.find(s => s.id === parentResource);
        const parentStatus = parentSvc ? parentSvc.status : 'not_in_catalog';
        const parentBadge = parentStatus === 'approved'
            ? '<span class="child-res-status child-res-approved">✅ Onboarded</span>'
            : '<span class="child-res-status child-res-missing">⚠️ Not onboarded</span>';
        html += `
        <div class="child-resources-section">
            <div class="child-res-header">🔗 Parent Resource</div>
            <div class="child-res-item" onclick="showServiceDetail('${escapeHtml(parentResource)}')" style="cursor:pointer">
                <span class="child-res-name">${escapeHtml(parentShort)}</span>
                <span class="child-res-type">${escapeHtml(parentResource)}</span>
                ${parentBadge}
            </div>
        </div>`;
    }

    // Child resources
    if (childResources && childResources.length > 0) {
        const items = childResources.map(c => {
            let badge;
            if (c.has_active_version) {
                badge = '<span class="child-res-status child-res-approved">✅ Onboarded</span>';
            } else if (c.status === 'approved') {
                badge = '<span class="child-res-status child-res-pending">📋 Approved</span>';
            } else if (c.status === 'not_in_catalog') {
                badge = '<span class="child-res-status child-res-missing">—</span>';
            } else {
                badge = `<span class="child-res-status child-res-missing">${escapeHtml(c.status)}</span>`;
            }
            const autoTag = c.always_include
                ? '<span class="child-res-auto-tag" title="Will be automatically co-onboarded with parent">auto</span>'
                : '';
            const clickable = c.status !== 'not_in_catalog'
                ? ` onclick="showServiceDetail('${escapeHtml(c.type)}')" style="cursor:pointer"`
                : '';
            return `
                <div class="child-res-item"${clickable}>
                    <span class="child-res-name">${escapeHtml(c.short_name)} ${autoTag}</span>
                    <span class="child-res-reason">${escapeHtml(c.reason)}</span>
                    ${badge}
                </div>`;
        }).join('');

        html += `
        <div class="child-resources-section">
            <div class="child-res-header">👶 Child Resources</div>
            ${items}
        </div>`;
    }

    return html;
}

function _renderOnboardButton(svc, status, latestVersion, apiVersionStatus, versions, activeVersionNum) {
    // API Version Update buttons — shown when service is onboarded AND newer version available
    let updateBtn = '';
    let analyzeBtn = '';
    if (apiVersionStatus && (apiVersionStatus.newer_available || apiVersionStatus.recommended_differs) && status === 'approved' && latestVersion) {
        const hasSeparateRec = apiVersionStatus.default && apiVersionStatus.default !== apiVersionStatus.latest_stable
            && apiVersionStatus.default !== apiVersionStatus.template_api_version;
        const recIsDowngrade = hasSeparateRec && apiVersionStatus.default < apiVersionStatus.template_api_version;
        const recActionLabel = recIsDowngrade ? '↓ Downgrade to Recommended' : '↑ Update to Recommended';

        // Analyze Upgrade button — always shown when update is available
        const analyzeTarget = apiVersionStatus.newer_available ? apiVersionStatus.latest_stable : apiVersionStatus.default;
        analyzeBtn = `<button class="btn btn-sm btn-analyze-upgrade" onclick="analyzeUpgradeCompatibility('${escapeHtml(svc.id)}', '${escapeHtml(analyzeTarget)}')" title="AI-powered analysis of breaking changes, new features, and migration effort">
                🔬 Analyze Upgrade Impact
            </button>`;

        // Show latest update button only if newer is available
        const latestBtn = apiVersionStatus.newer_available
            ? `<button class="btn btn-sm btn-accent" onclick="triggerApiVersionUpdate('${escapeHtml(svc.id)}', '${escapeHtml(apiVersionStatus.latest_stable)}')">
                    ⬆ Update to Latest (${escapeHtml(apiVersionStatus.template_api_version)} → ${escapeHtml(apiVersionStatus.latest_stable)})
                </button>` : '';

        if (hasSeparateRec) {
            updateBtn = latestBtn + `
                <button class="btn btn-sm btn-accent btn-rec" onclick="triggerApiVersionUpdate('${escapeHtml(svc.id)}', '${escapeHtml(apiVersionStatus.default)}')">
                    ${recActionLabel} (${escapeHtml(apiVersionStatus.template_api_version)} → ${escapeHtml(apiVersionStatus.default)})
                </button>`;
        } else if (apiVersionStatus.newer_available) {
            updateBtn = `<button class="btn btn-sm btn-accent" onclick="triggerApiVersionUpdate('${escapeHtml(svc.id)}')">
                   ⬆ Update API Version (${escapeHtml(apiVersionStatus.template_api_version)} → ${escapeHtml(apiVersionStatus.latest_stable)})
               </button>`;
        }
    }

    // Governance-approved AND has a validated version → fully onboarded
    if (status === 'approved' && latestVersion) {
        // Use the active version for display, not the latest (which may be a failed re-onboarding attempt)
        const activeVer = activeVersionNum
            ? (versions || []).find(v => v.version === activeVersionNum)
            : null;
        const displayVer = activeVer || latestVersion;
        const hasFailedLatest = latestVersion.status === 'failed' && activeVer && latestVersion.version !== activeVer.version;

        return `
        <div class="validation-card validation-succeeded" id="validation-card">
            <div class="validation-header">
                <span class="validation-icon">✅</span>
                <span class="validation-title">Service Onboarded — v${displayVer.semver || displayVer.version + '.0.0'}</span>
            </div>
            <div class="validation-detail">
                This service has a validated ARM template and is approved for deployment.
                ${displayVer.validated_at ? `Validated: ${displayVer.validated_at.substring(0, 10)}` : ''}
            </div>
            ${hasFailedLatest ? `
            <div class="validation-detail" style="color: var(--warning); margin-top: 0.4rem;">
                ⚠️ A newer version (v${latestVersion.semver || latestVersion.version + '.0.0'}) failed validation.
                The active version is still v${displayVer.semver || displayVer.version + '.0.0'}.
            </div>` : ''}
            <div class="validation-actions">
                ${updateBtn}
                ${analyzeBtn}
                <button class="btn btn-sm btn-secondary" onclick="triggerOnboarding('${escapeHtml(svc.id)}')">
                    🔄 Re-validate (New Version)
                </button>
                <button class="btn btn-sm btn-danger btn-offboard" onclick="offboardService('${escapeHtml(svc.id)}', '${escapeHtml(svc.name)}')" title="Deactivate this service and deprecate all versions">
                    📦 Offboard
                </button>
            </div>
            <div id="upgrade-analysis-container"></div>
            <div class="validation-log" id="validation-log"></div>
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
                have an ARM template yet. The Copilot SDK will auto-generate a validated, policy-compliant template.
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

    // offboarded — show deactivated state with re-onboard option
    if (status === 'offboarded') {
        return `
        <div class="validation-card validation-offboarded" id="validation-card">
            <div class="validation-header">
                <span class="validation-icon">📦</span>
                <span class="validation-title">Service Offboarded</span>
            </div>
            <div class="validation-detail">
                <strong>${escapeHtml(svc.name)}</strong> has been offboarded. All previously approved template versions
                are now deprecated and no longer active for deployment.
                Version history is preserved below for audit purposes.
            </div>
            <div class="validation-actions">
                <button class="btn btn-sm btn-accent" onclick="triggerOnboarding('${escapeHtml(svc.id)}')">
                    🚀 Re-onboard Service
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
            Uses the Copilot SDK to auto-generate an ARM template for <strong>${escapeHtml(svc.name)}</strong>, validate it against
            organization governance policies, deploy to a test resource group, then promote to approved.
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
    const approvedVersions = versions.filter(v => v.status === 'approved' || v.status === 'deprecated');
    const draftVersions = versions.filter(v => v.status === 'draft' || v.status === 'failed');
    const totalCount = versions.length;
    const approvedCount = approvedVersions.length;
    const draftCount = draftVersions.length;

    let html = '';

    // ── Draft / failed versions ──
    if (draftCount > 0) {
        html += `
        <div class="version-history version-history-drafts">
            <div class="version-history-header version-history-header-draft">
                <span>📝 Draft Versions (Pending Validation)</span>
                <span class="version-count">${draftCount} draft${draftCount === 1 ? '' : 's'}${draftCount > 1 ? ` <button class="btn btn-xs btn-danger" onclick="event.stopPropagation(); deleteAllDraftVersions('${escapeHtml(draftVersions[0].service_id)}')">🗑 Clear All</button>` : ''}</span>
            </div>
            <div class="version-list">
                ${draftVersions.map(v => {
                    const sizeKB = v.template_size_bytes
                        ? (v.template_size_bytes / 1024).toFixed(1)
                        : v.arm_template
                            ? (v.arm_template.length / 1024).toFixed(1)
                            : '?';
                    const displayVer = v.semver || `${v.version}.0.0`;
                    const isFailed = v.status === 'failed';
                    const statusIcon = isFailed ? '❌' : '📝';
                    const statusLabel = isFailed ? 'failed' : 'draft';
                    const statusClass = isFailed ? 'version-status-failed' : 'version-status-draft';
                    const itemClass = isFailed ? 'version-item-failed' : 'version-item-draft';

                    return `
                    <div class="version-item ${itemClass}" onclick="toggleVersionDetail(this)">
                        <div class="version-item-header">
                            <span class="version-item-badge version-badge-draft">v${displayVer}</span>
                            <span class="version-item-status ${statusClass}">${statusIcon} ${statusLabel}</span>
                            ${v.api_version ? `<span class="version-item-api">API ${escapeHtml(v.api_version)}</span>` : ''}
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
                                ${isFailed ? '' : `<button class="btn btn-sm btn-accent" onclick="event.stopPropagation(); triggerDraftValidation('${escapeHtml(v.service_id)}', ${v.version}, '${displayVer}')">
                                    🚀 Validate & Promote
                                </button>`}
                                <button class="btn btn-sm btn-primary" onclick="event.stopPropagation(); viewTemplate('${escapeHtml(v.service_id)}', ${v.version})">
                                    👁 View Template
                                </button>
                                <button class="btn btn-sm btn-secondary" onclick="event.stopPropagation(); downloadTemplateVersion('${escapeHtml(v.service_id)}', ${v.version})">
                                    ⬇ Download
                                </button>
                                <button class="btn btn-sm btn-danger" onclick="event.stopPropagation(); deleteDraftVersion('${escapeHtml(v.service_id)}', ${v.version}, '${displayVer}')">
                                    🗑 Delete
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
                    <div class="version-item ${isActive ? 'version-item-active' : ''} ${v.status === 'deprecated' ? 'version-item-deprecated' : ''}" onclick="toggleVersionDetail(this)">
                        <div class="version-item-header">
                            <span class="version-item-badge">v${displayVer}</span>
                            <span class="version-item-status">${v.status === 'deprecated' ? '📦 deprecated' : '✅ approved'}</span>
                            ${isActive ? '<span class="version-item-active-label">ACTIVE</span>' : (v.status === 'deprecated' ? '<span class="version-item-deprecated-label">DEPRECATED</span>' : '<span class="version-item-deprecated-label">SUPERSEDED</span>')}
                            ${v.api_version ? `<span class="version-item-api">API ${escapeHtml(v.api_version)}</span>` : ''}
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

// ── Catalog Template Version Viewer ─────────────────────────

async function viewCatalogTemplateVersion(templateId, version) {
    const modal = document.getElementById('modal-template-viewer');
    const title = document.getElementById('template-viewer-title');
    const meta = document.getElementById('template-viewer-meta');
    const code = document.getElementById('template-viewer-code');

    title.textContent = `ARM Template — Loading…`;
    meta.innerHTML = '<span class="template-meta-badge">Loading…</span>';
    code.querySelector('code').textContent = 'Loading template…';
    _currentTemplateContent = '';
    _currentTemplateFilename = `${templateId.replace(/[^a-zA-Z0-9_-]/g, '_')}_v${version}.json`;
    _currentTemplateServiceId = templateId;
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
        const res = await fetch(`/api/catalog/templates/${encodeURIComponent(templateId)}/versions/${version}`);
        if (!res.ok) throw new Error(`Server returned ${res.status}`);
        const data = await res.json();
        const template = data.arm_template || '';

        let formatted;
        try { formatted = JSON.stringify(JSON.parse(template), null, 2); } catch { formatted = template; }

        _currentTemplateContent = formatted;
        code.querySelector('code').innerHTML = _highlightJSON(formatted);

        const sizeKB = (formatted.length / 1024).toFixed(1);
        const semver = data.semver || `${version}.0.0`;
        const isActive = data.version === data.active_version;
        const dateStr = data.created_at ? data.created_at.substring(0, 10) : '—';

        const metaBadges = [
            `<span class="template-meta-badge">📦 ${escapeHtml(data.template_name || templateId)}</span>`,
            `<span class="template-meta-badge">v${semver}</span>`,
            `<span class="template-meta-badge">${sizeKB} KB</span>`,
            `<span class="template-meta-badge">📅 ${dateStr}</span>`,
            isActive ? '<span class="template-meta-badge tmpl-meta-active">✅ Active</span>' : `<span class="template-meta-badge tmpl-meta-historical">📜 Historical</span>`,
        ];

        const templateHash = template ? (() => { try { const p = JSON.parse(template); return p.metadata?._generator?.templateHash || ''; } catch { return ''; } })() : '';
        if (templateHash) metaBadges.push(`<span class="template-meta-badge" title="Content hash">🔑 ${templateHash}</span>`);

        title.textContent = `ARM Template — v${semver}`;
        meta.innerHTML = metaBadges.join('\n');
    } catch (err) {
        code.querySelector('code').textContent = `Error loading template: ${err.message}`;
    }
}

async function deployCatalogTemplateVersion(templateId, version, semver) {
    if (!confirm(`Deploy version ${semver || version} of this template?`)) return;
    // Navigate to the template detail and pre-fill deploy section with version
    window._deploySpecificVersion = version;
    await showTemplateDetail(templateId);
    // Scroll to deploy section if visible
    const deployBtn = document.getElementById('tmpl-deploy-btn');
    if (deployBtn) deployBtn.scrollIntoView({ behavior: 'smooth', block: 'center' });
    showToast(`Deploy section loaded for version ${semver || version}. Fill in resource group and deploy.`, 'info');
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

// ── Pipeline Runs ──────────────────────────────────────────────
async function _loadPipelineRuns(serviceId) {
    const container = document.getElementById('pipeline-runs-container');
    if (!container) return;
    try {
        const res = await fetch(`/api/services/${encodeURIComponent(serviceId)}/pipeline-runs`);
        if (!res.ok) return;           // silently fail — not critical
        const runs = await res.json();
        if (!runs || runs.length === 0) return;   // nothing to show
        container.innerHTML = _renderPipelineRuns(runs);
        container.style.display = '';
    } catch (_) { /* ignore */ }
}

// ── Governance Reviews ─────────────────────────────────────────
async function _loadGovernanceReviews(serviceId) {
    const container = document.getElementById('governance-reviews-container');
    if (!container) return;
    try {
        const res = await fetch(`/api/services/${encodeURIComponent(serviceId)}/governance-reviews`);
        if (!res.ok) return;
        const reviews = await res.json();
        if (!reviews || reviews.length === 0) return;
        container.innerHTML = _renderGovernanceReviews(reviews);
        container.style.display = '';
    } catch (_) { /* ignore */ }
}

function _renderGovernanceReviews(reviews) {
    const verdictIcon = (v) => ({
        approved: '✅', conditional: '⚠️', blocked: '🚫', advisory: '💡', needs_revision: '🔧',
    })[v] || '❓';

    const verdictClass = (v) => ({
        approved: 'gov-verdict-approved', conditional: 'gov-verdict-conditional',
        blocked: 'gov-verdict-blocked', advisory: 'gov-verdict-advisory',
        needs_revision: 'gov-verdict-revision',
    })[v] || '';

    const agentIcon = (a) => a === 'ciso' ? '🛡️' : a === 'cto' ? '🏗️' : '🏛️';
    const agentLabel = (a) => a === 'ciso' ? 'CISO' : a === 'cto' ? 'CTO' : a.toUpperCase();

    // Group reviews by version (most recent first)
    const grouped = {};
    for (const r of reviews) {
        const key = r.semver || `v${r.version}`;
        if (!grouped[key]) grouped[key] = [];
        grouped[key].push(r);
    }

    let html = '';
    for (const [ver, revs] of Object.entries(grouped)) {
        const gate = revs[0]?.gate_decision;
        const gateIcon = verdictIcon(gate);
        const gateClass = verdictClass(gate);

        const reviewCards = revs.map(r => {
            const findings = r.findings || [];
            const criticalFindings = findings.filter(f => f.severity === 'critical' || f.severity === 'high');
            const otherFindings = findings.filter(f => f.severity !== 'critical' && f.severity !== 'high');

            let findingsHtml = '';
            if (criticalFindings.length) {
                findingsHtml += criticalFindings.map(f => `
                    <div class="gov-finding gov-finding-${f.severity}">
                        <span class="gov-finding-severity">${f.severity}</span>
                        <span class="gov-finding-category">${escapeHtml(f.category)}</span>
                        <span class="gov-finding-text">${escapeHtml(f.finding)}</span>
                        ${f.recommendation ? `<div class="gov-finding-rec">${escapeHtml(f.recommendation)}</div>` : ''}
                    </div>
                `).join('');
            }
            if (otherFindings.length) {
                findingsHtml += `<div class="gov-findings-minor">${otherFindings.length} additional finding(s)</div>`;
            }

            const scores = [];
            if (r.risk_score != null) scores.push(`Risk: ${r.risk_score}/10`);
            if (r.architecture_score != null) scores.push(`Architecture: ${r.architecture_score}/10`);
            if (r.security_posture) scores.push(`Security: ${r.security_posture}`);
            if (r.cost_assessment) scores.push(`Cost: ${r.cost_assessment}`);

            return `
            <div class="gov-review-card ${verdictClass(r.verdict)}">
                <div class="gov-review-header">
                    <span class="gov-agent">${agentIcon(r.agent)} ${agentLabel(r.agent)}</span>
                    <span class="gov-verdict ${verdictClass(r.verdict)}">${verdictIcon(r.verdict)} ${r.verdict}</span>
                    ${r.confidence ? `<span class="gov-confidence">${Math.round(r.confidence * 100)}%</span>` : ''}
                </div>
                ${r.summary ? `<div class="gov-review-summary">${escapeHtml(r.summary)}</div>` : ''}
                ${scores.length ? `<div class="gov-review-scores">${scores.map(s => `<span class="gov-score-chip">${escapeHtml(s)}</span>`).join('')}</div>` : ''}
                ${findingsHtml ? `<div class="gov-review-findings">${findingsHtml}</div>` : ''}
                ${r.model_used ? `<div class="gov-review-meta">Model: ${escapeHtml(r.model_used)}</div>` : ''}
            </div>`;
        }).join('');

        html += `
        <div class="gov-version-group">
            <div class="gov-version-header">
                <span class="gov-version-label">${escapeHtml(ver)}</span>
                <span class="gov-gate ${gateClass}">${gateIcon} Gate: ${gate || 'unknown'}</span>
                <span class="gov-review-date">${(revs[0]?.reviewed_at || '').replace('T', ' ').substring(0, 19)}</span>
            </div>
            ${reviewCards}
        </div>`;
    }

    return `
    <div class="version-history">
        <div class="version-history-header">
            <span>🏛️ Governance Reviews</span>
            <span class="version-count">${reviews.length} review${reviews.length === 1 ? '' : 's'}</span>
        </div>
        <div class="version-list">${html}</div>
    </div>`;
}

function _renderPipelineRuns(runs) {
    const statusIcon = (s) => ({
        completed: '✅', failed: '❌', running: '🔄',
    })[s] || '⏳';

    const statusClass = (s) => ({
        completed: 'run-status-completed',
        failed: 'run-status-failed',
        running: 'run-status-running',
    })[s] || 'run-status-unknown';

    const pipelineLabel = (t) => ({
        onboarding: 'Onboarding',
        api_version_update: 'API Version Update',
        infra_testing: 'Infrastructure Testing',
    })[t] || t;

    const formatDuration = (secs) => {
        if (!secs && secs !== 0) return '—';
        if (secs < 60) return `${Math.round(secs)}s`;
        const m = Math.floor(secs / 60);
        const s = Math.round(secs % 60);
        return s > 0 ? `${m}m ${s}s` : `${m}m`;
    };

    const items = runs.map(r => {
        const started = (r.started_at || '').replace('T', ' ').substring(0, 19);
        const dur = formatDuration(r.duration_secs);
        const summary = r.summary || {};
        const healCount = r.heal_count || 0;

        let detailRows = '';
        if (r.error_detail) {
            detailRows += `<div class="run-detail-row run-detail-error"><strong>Error:</strong> ${escapeHtml(r.error_detail)}</div>`;
        }
        if (summary.changelog) {
            detailRows += `<div class="run-detail-row"><strong>Change:</strong> ${escapeHtml(summary.changelog)}</div>`;
        }
        if (summary.policy_check) {
            detailRows += `<div class="run-detail-row"><strong>Policy:</strong> ${escapeHtml(summary.policy_check)}</div>`;
        }
        if (healCount > 0) {
            detailRows += `<div class="run-detail-row"><strong>Heal cycles:</strong> ${healCount}</div>`;
        }
        if (r.version_num) {
            const ver = r.semver || `v${r.version_num}`;
            detailRows += `<div class="run-detail-row"><strong>Version:</strong> ${escapeHtml(ver)}</div>`;
        }
        if (r.run_id) {
            detailRows += `<div class="run-detail-row run-detail-runid"><strong>Run ID:</strong> <code>${escapeHtml(r.run_id)}</code></div>`;
        }

        return `
        <div class="run-item run-item-${r.status || 'unknown'}" onclick="this.querySelector('.run-item-detail')?.classList.toggle('hidden')">
            <div class="run-item-header">
                <span class="run-item-status ${statusClass(r.status)}">${statusIcon(r.status)}</span>
                <span class="run-item-pipeline">${pipelineLabel(r.pipeline_type)}</span>
                <span class="run-item-duration">${dur}</span>
                <span class="run-item-date">${started}</span>
            </div>
            ${detailRows ? `<div class="run-item-detail hidden">${detailRows}</div>` : ''}
        </div>`;
    }).join('');

    return `
    <div class="version-history">
        <div class="version-history-header">
            <span>📊 Recent Pipeline Runs</span>
            <span class="version-count">${runs.length} run${runs.length === 1 ? '' : 's'}</span>
        </div>
        <div class="version-list">${items}</div>
    </div>`;
}

function toggleVersionDetail(el) {
    const detail = el.querySelector('.version-item-detail');
    if (detail) detail.classList.toggle('hidden');
}

async function deleteDraftVersion(serviceId, version, semver) {
    if (!confirm(`Delete draft v${semver}? This cannot be undone.`)) return;
    try {
        const res = await fetch(`/api/services/${encodeURIComponent(serviceId)}/versions/${version}`, { method: 'DELETE' });
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Delete failed');
        }
        showToast(`Deleted draft v${semver}`, 'info');
        await loadAllData();
        await showServiceDetail(serviceId);
    } catch (err) {
        showToast(`Failed to delete: ${err.message}`, 'error');
    }
}

async function deleteAllDraftVersions(serviceId) {
    if (!confirm('Delete ALL draft and failed versions? This cannot be undone.')) return;
    try {
        const res = await fetch(`/api/services/${encodeURIComponent(serviceId)}/versions/drafts`, { method: 'DELETE' });
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Delete failed');
        }
        const data = await res.json();
        showToast(`Deleted ${data.deleted || 0} draft/failed version(s)`, 'info');
        await loadAllData();
        await showServiceDetail(serviceId);
    } catch (err) {
        showToast(`Failed to delete: ${err.message}`, 'error');
    }
}

async function offboardService(serviceId, serviceName) {
    if (!confirm(
        `⚠️ Offboard "${serviceName}"?\n\n` +
        `This will:\n` +
        `• Deactivate the service (no active template)\n` +
        `• Mark all approved versions as deprecated\n` +
        `• Preserve all data for audit trail\n\n` +
        `The service can be re-onboarded later if needed.`
    )) return;

    try {
        const res = await fetch(`/api/services/${encodeURIComponent(serviceId)}/offboard`, { method: 'POST' });
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Offboarding failed');
        }
        const data = await res.json();
        showToast(data.message || `${serviceName} offboarded successfully`, 'info');
        await loadAllData();
        await showServiceDetail(serviceId);
    } catch (err) {
        showToast(`Failed to offboard: ${err.message}`, 'error');
    }
}

async function triggerDraftValidation(serviceId, version, semver) {
    // Close the template viewer if open
    closeModal('modal-template-viewer');

    const svc = allServices.find(s => s.id === serviceId);
    const svcName = svc ? svc.name : serviceId;
    openPipelineOverlay('Validation Pipeline', '🔍', `Validating ${svcName} draft v${semver}…`);

    showToast(`Starting validation for draft v${semver}…`, 'info');

    // Trigger the onboard pipeline with use_version to skip generation
    const card = document.getElementById('validation-card');

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
            <div class="validation-log" id="validation-log"></div>
        `;
    }

    try {
        const body = { use_version: version };

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
    const container = document.getElementById('model-routing-chips');
    if (!container) return;

    // Fetch the routing table from the API
    fetch('/api/settings/model-routing')
        .then(r => r.json())
        .then(data => {
            const table = data.routing_table || [];
            // Show only the key pipeline tasks
            const show = ['Planning', 'Code Generation', 'Code Fixing', 'Policy Generation'];
            const chips = table
                .filter(t => show.includes(t.task_label))
                .map(t => {
                    const short = t.task_label.replace('Code ', '').replace('Policy ', 'Policy ');
                    return `<span class="model-routing-chip" title="${t.reason}">${short}: <strong>${t.model_name}</strong></span>`;
                });
            container.innerHTML = chips.join('') || '<span class="model-routing-chip">No routing configured</span>';
        })
        .catch(() => {
            container.innerHTML = '<span class="model-routing-chip">Could not load routing</span>';
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
    const svc = allServices.find(s => s.id === serviceId);
    const svcName = svc ? svc.name : serviceId;
    openPipelineOverlay('Onboarding Pipeline', '⏳', `Onboarding ${svcName}…`);

    const card = document.getElementById('validation-card');

    if (card) {
        card.className = 'validation-card validation-running';
        card.dataset.serviceId = serviceId;
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
            <div class="validation-log" id="validation-log"></div>
        `;
    }

    try {
        const body = {};

        const res = await fetch(`/api/services/${encodeURIComponent(serviceId)}/onboard`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });

        if (!res.ok) {
            let errMsg = 'Onboarding request failed';
            try {
                const err = await res.json();
                errMsg = err.detail || errMsg;
            } catch (_) {
                const text = await res.text().catch(() => '');
                errMsg = text || `Server error (${res.status})`;
            }
            throw new Error(errMsg);
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
        const isNetworkErr = err.name === 'TypeError' || /network|fetch|aborted|failed to fetch/i.test(err.message);
        if (isNetworkErr) {
            showToast('Connection interrupted — your deployment may still be running in Azure. Refresh to check status.', 'warning');
            const detail = document.getElementById('validation-detail');
            if (detail) detail.textContent = 'Connection lost — deployment may still be running. Refresh the page to check the latest status.';
        } else {
            showToast(`Onboarding failed: ${err.message}`, 'error');
            const detail = document.getElementById('validation-detail');
            if (detail) detail.textContent = `Error: ${err.message}`;
            const cardEl = document.getElementById('validation-card');
            if (cardEl) cardEl.className = 'validation-card validation-failed';
        }
    }
}

// ── Upgrade Compatibility Analysis ──────────────────────────

let _upgradeAnalysisRunning = false;

async function analyzeUpgradeCompatibility(serviceId, targetVersion) {
    if (_upgradeAnalysisRunning) {
        showToast('An upgrade analysis is already running…', 'warning');
        return;
    }
    _upgradeAnalysisRunning = true;

    const container = document.getElementById('upgrade-analysis-container');
    if (!container) {
        _upgradeAnalysisRunning = false;
        return;
    }

    const svc = allServices.find(s => s.id === serviceId);
    const svcName = svc ? svc.name : serviceId;

    // Show loading state
    container.innerHTML = `
        <div class="upgrade-analysis-panel upgrade-analysis-loading">
            <div class="upgrade-analysis-header">
                <span class="upgrade-analysis-icon spin">🔬</span>
                <span class="upgrade-analysis-title">Analyzing Upgrade Compatibility…</span>
            </div>
            <div class="upgrade-analysis-meta">
                <span>Upgrade Analyst agent is reviewing <strong>${escapeHtml(svcName)}</strong></span>
                ${targetVersion ? `<span class="upgrade-analysis-version">→ ${escapeHtml(targetVersion)}</span>` : ''}
            </div>
            <div class="upgrade-analysis-progress">
                <div class="upgrade-analysis-progress-track">
                    <div class="upgrade-analysis-progress-fill" id="upgrade-analysis-progress-fill" style="width: 0%"></div>
                </div>
            </div>
            <div class="upgrade-analysis-status" id="upgrade-analysis-status">Initializing…</div>
        </div>`;
    container.scrollIntoView({ behavior: 'smooth', block: 'nearest' });

    try {
        const reqBody = {};
        if (targetVersion) reqBody.target_version = targetVersion;

        const res = await fetch(`/api/services/${encodeURIComponent(serviceId)}/analyze-upgrade`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(reqBody),
        });

        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Analysis request failed');
        }

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let analysisResult = null;

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
                    _handleUpgradeAnalysisEvent(ev, container);
                    if (ev.type === 'analysis_complete') {
                        analysisResult = ev;
                    }
                } catch (e) {
                    console.warn('[upgrade-analysis] failed to parse event:', line);
                }
            }
        }

        // Process remaining buffer
        if (buffer.trim()) {
            try {
                const ev = JSON.parse(buffer);
                _handleUpgradeAnalysisEvent(ev, container);
                if (ev.type === 'analysis_complete') {
                    analysisResult = ev;
                }
            } catch (e) { /* ignore */ }
        }

        if (analysisResult) {
            _renderUpgradeAnalysisResult(analysisResult, container, serviceId);
        }

    } catch (err) {
        container.innerHTML = `
            <div class="upgrade-analysis-panel upgrade-analysis-error">
                <div class="upgrade-analysis-header">
                    <span class="upgrade-analysis-icon">❌</span>
                    <span class="upgrade-analysis-title">Analysis Failed</span>
                </div>
                <div class="upgrade-analysis-body">${escapeHtml(err.message)}</div>
                <button class="btn btn-sm btn-secondary" onclick="analyzeUpgradeCompatibility('${escapeHtml(serviceId)}', '${escapeHtml(targetVersion || '')}')">🔄 Retry</button>
            </div>`;
        showToast(`Upgrade analysis failed: ${err.message}`, 'error');
    } finally {
        _upgradeAnalysisRunning = false;
    }
}

function _handleUpgradeAnalysisEvent(ev, container) {
    if (ev.type === 'progress') {
        const statusEl = document.getElementById('upgrade-analysis-status');
        const fillEl = document.getElementById('upgrade-analysis-progress-fill');
        if (statusEl) statusEl.textContent = ev.detail || '';
        if (fillEl && ev.progress) fillEl.style.width = `${Math.round(ev.progress * 100)}%`;

        // Show agent/model info
        if (ev.agent) {
            const metaEl = container.querySelector('.upgrade-analysis-meta');
            if (metaEl && !metaEl.querySelector('.upgrade-analysis-agent')) {
                metaEl.innerHTML += `<span class="upgrade-analysis-agent">🤖 ${escapeHtml(ev.agent)}</span>`;
            }
            if (metaEl && ev.model && !metaEl.querySelector('.upgrade-analysis-model')) {
                metaEl.innerHTML += `<span class="upgrade-analysis-model">${escapeHtml(ev.model)}</span>`;
            }
        }
    } else if (ev.type === 'error') {
        container.innerHTML = `
            <div class="upgrade-analysis-panel upgrade-analysis-error">
                <div class="upgrade-analysis-header">
                    <span class="upgrade-analysis-icon">❌</span>
                    <span class="upgrade-analysis-title">Analysis Failed</span>
                </div>
                <div class="upgrade-analysis-body">${escapeHtml(ev.detail || 'Unknown error')}</div>
            </div>`;
    }
}

function _renderUpgradeAnalysisResult(result, container, serviceId, templateId) {
    const analysis = result.analysis || 'No analysis available.';

    // Parse the markdown to detect the verdict for styling
    let verdictClass = 'upgrade-verdict-caution';
    if (analysis.includes('✅') && analysis.includes('Safe to upgrade')) {
        verdictClass = 'upgrade-verdict-safe';
    } else if (analysis.includes('🛑') && analysis.includes('Breaking changes')) {
        verdictClass = 'upgrade-verdict-breaking';
    }

    // Convert markdown to basic HTML
    const analysisHtml = _markdownToHtml(analysis);

    // Unique ID for this chat instance
    const chatId = 'ua-chat-' + Date.now();

    container.innerHTML = `
        <div class="upgrade-analysis-panel upgrade-analysis-complete ${verdictClass}">
            <div class="upgrade-analysis-header">
                <span class="upgrade-analysis-icon">🔬</span>
                <span class="upgrade-analysis-title">Upgrade Compatibility Analysis</span>
                <button class="upgrade-analysis-close" onclick="this.closest('.upgrade-analysis-panel').remove()" title="Dismiss">✕</button>
            </div>
            <div class="upgrade-analysis-meta">
                <span>${escapeHtml(result.current_api_version)} → ${escapeHtml(result.target_api_version)}</span>
                <span class="upgrade-analysis-agent">🤖 ${escapeHtml(result.agent || 'Upgrade Analyst')}</span>
                <span class="upgrade-analysis-model">${escapeHtml(result.model || '')}</span>
            </div>
            <div class="upgrade-analysis-body">${analysisHtml}</div>
            <div class="upgrade-analysis-actions">
                <button class="btn btn-sm btn-secondary" onclick="analyzeUpgradeCompatibility('${escapeHtml(serviceId)}', '${escapeHtml(result.target_api_version || '')}')" title="Re-run the analysis">🔄 Re-analyze</button>
            </div>
            <div class="upgrade-chat-section" id="${chatId}">
                <div class="upgrade-chat-divider">
                    <span>💬 Ask the Upgrade Analyst</span>
                </div>
                <div class="upgrade-chat-messages" id="${chatId}-messages"></div>
                <div class="upgrade-chat-input-row">
                    <input type="text" class="upgrade-chat-input" id="${chatId}-input"
                           placeholder="Ask a follow-up question about this upgrade…"
                           onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();_sendUpgradeChat('${chatId}','${escapeHtml(serviceId)}')}" />
                    <button class="btn btn-sm btn-primary upgrade-chat-send" id="${chatId}-send"
                            onclick="_sendUpgradeChat('${chatId}','${escapeHtml(serviceId)}')">
                        <span class="upgrade-chat-send-icon">➤</span>
                    </button>
                </div>
            </div>
        </div>`;

    // Store analysis context for this chat instance
    window._upgradeChatState = window._upgradeChatState || {};
    window._upgradeChatState[chatId] = {
        history: [],
        analysisContext: {
            current_api_version: result.current_api_version || '',
            target_api_version: result.target_api_version || '',
            analysis: analysis,
        },
        templateId: templateId || null,
        sending: false,
    };

    // Focus the input after a tick
    setTimeout(() => {
        const inp = document.getElementById(`${chatId}-input`);
        if (inp) inp.focus();
    }, 100);

    container.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

/**
 * Send a message in the upgrade analyst chat.
 * Streams the response in real-time via NDJSON deltas.
 */
async function _sendUpgradeChat(chatId, serviceId) {
    const state = (window._upgradeChatState || {})[chatId];
    if (!state || state.sending) return;

    const inputEl = document.getElementById(`${chatId}-input`);
    const sendBtn = document.getElementById(`${chatId}-send`);
    const messagesEl = document.getElementById(`${chatId}-messages`);
    if (!inputEl || !messagesEl) return;

    const message = inputEl.value.trim();
    if (!message) return;

    state.sending = true;
    inputEl.value = '';
    inputEl.disabled = true;
    if (sendBtn) sendBtn.disabled = true;

    // Render user message
    const userBubble = document.createElement('div');
    userBubble.className = 'upgrade-chat-msg upgrade-chat-msg-user';
    userBubble.innerHTML = `<div class="upgrade-chat-bubble">${escapeHtml(message)}</div>`;
    messagesEl.appendChild(userBubble);

    // Render assistant placeholder with typing indicator
    const assistantBubble = document.createElement('div');
    assistantBubble.className = 'upgrade-chat-msg upgrade-chat-msg-assistant';
    assistantBubble.innerHTML = `<div class="upgrade-chat-bubble"><span class="upgrade-chat-typing">●●●</span></div>`;
    messagesEl.appendChild(assistantBubble);
    messagesEl.scrollTop = messagesEl.scrollHeight;

    try {
        const res = await fetch(`/api/services/${encodeURIComponent(serviceId)}/upgrade-chat`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                message,
                history: state.history,
                analysis_context: state.analysisContext,
                template_id: state.templateId || undefined,
            }),
        });

        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Chat request failed');
        }

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let fullResponse = '';
        let streamingContent = '';

        // Replace typing indicator with streaming content
        const bubbleEl = assistantBubble.querySelector('.upgrade-chat-bubble');

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
                    if (ev.type === 'delta') {
                        streamingContent += ev.content || '';
                        bubbleEl.innerHTML = _markdownToHtml(streamingContent);
                        messagesEl.scrollTop = messagesEl.scrollHeight;
                    } else if (ev.type === 'done') {
                        fullResponse = ev.content || streamingContent;
                    } else if (ev.type === 'error') {
                        throw new Error(ev.detail || 'Chat error');
                    }
                } catch (e) {
                    if (e.message && !e.message.includes('JSON')) throw e;
                }
            }
        }

        // Process remaining buffer
        if (buffer.trim()) {
            try {
                const ev = JSON.parse(buffer);
                if (ev.type === 'delta') {
                    streamingContent += ev.content || '';
                } else if (ev.type === 'done') {
                    fullResponse = ev.content || streamingContent;
                }
            } catch (e) { /* skip */ }
        }

        // Final render
        const finalText = fullResponse || streamingContent;
        bubbleEl.innerHTML = _markdownToHtml(finalText);
        messagesEl.scrollTop = messagesEl.scrollHeight;

        // Save to history
        state.history.push({ role: 'user', content: message });
        state.history.push({ role: 'assistant', content: finalText });

    } catch (err) {
        const bubbleEl = assistantBubble.querySelector('.upgrade-chat-bubble');
        bubbleEl.innerHTML = `<span class="upgrade-chat-error">❌ ${escapeHtml(err.message)}</span>`;
    } finally {
        state.sending = false;
        inputEl.disabled = false;
        if (sendBtn) sendBtn.disabled = false;
        inputEl.focus();
    }
}

/** Minimal markdown→HTML converter for upgrade analysis results */
function _markdownToHtml(md) {
    let html = md
        // Code blocks (triple backtick)
        .replace(/```(\w*)\n([\s\S]*?)```/g, '<pre><code class="lang-$1">$2</code></pre>')
        // Inline code
        .replace(/`([^`]+)`/g, '<code>$1</code>')
        // Headers (### before ## before #)
        .replace(/^#### (.+)$/gm, '<h5>$1</h5>')
        .replace(/^### (.+)$/gm, '<h4>$1</h4>')
        .replace(/^## (.+)$/gm, '<h3>$1</h3>')
        .replace(/^# (.+)$/gm, '<h2>$1</h2>')
        // Bold
        .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
        // Italic
        .replace(/(?<!\*)\*([^*]+)\*(?!\*)/g, '<em>$1</em>')
        // Unordered list items
        .replace(/^- (.+)$/gm, '<li>$1</li>')
        // Numbered list items
        .replace(/^\d+\. (.+)$/gm, '<li>$1</li>')
        // Horizontal rules
        .replace(/^---$/gm, '<hr>')
        // Paragraphs — wrap non-tag lines
        .replace(/\n\n+/g, '</p><p>')
        ;

    // Wrap consecutive <li> in <ul>
    html = html.replace(/(<li>[\s\S]*?<\/li>)(?!\s*<li>)/g, '$1</ul>');
    html = html.replace(/(?<!<\/ul>)(<li>)/g, '<ul>$1');

    return `<p>${html}</p>`;
}

let _apiUpdateRunning = false;
let _apiUpdateBadgeId = null;  // badge element ID for live status updates in table

async function triggerApiVersionUpdate(serviceId, targetVersion) {
    if (_apiUpdateRunning) {
        console.warn('[update] triggerApiVersionUpdate blocked — already running');
        return;
    }
    _apiUpdateRunning = true;
    _apiUpdateAbort = new AbortController();
    console.log('[update] triggerApiVersionUpdate started for', serviceId, 'target:', targetVersion || 'latest');

    const svc = allServices.find(s => s.id === serviceId);
    const svcName = svc ? svc.name : serviceId;
    const targetLabel = targetVersion ? `to ${targetVersion}` : 'to latest';
    openPipelineOverlay('API Version Update', '⬆', `Updating ${svcName} ${targetLabel}…`);

    // Ensure the card shows running state — even if showServiceDetail already rendered the green card
    let card = document.getElementById('validation-card');

    console.log('[update] validation-card element:', card ? 'found' : 'NOT FOUND');

    // If no card exists, create one in the detail body
    if (!card) {
        const body = document.getElementById('detail-service-body');
        if (body) {
            const div = document.createElement('div');
            div.id = 'validation-card';
            body.insertBefore(div, body.firstChild);
            card = div;
        }
    }

    if (card) {
        card.className = 'validation-card validation-running';
        card.innerHTML = `
            <div class="validation-header">
                <span class="validation-icon validation-spinner">⬆</span>
                <span class="validation-title">API Version Update ${targetLabel} In Progress…</span>
            </div>
            <div class="validation-model-badge" id="validation-model-badge"></div>
            <div class="validation-attempt-badge" id="validation-attempt-badge"></div>
            <div class="validation-progress">
                <div class="validation-progress-track">
                    <div class="validation-progress-fill" id="validation-progress-fill" style="width: 0%"></div>
                </div>
            </div>
            <div class="validation-detail" id="validation-detail">Initializing API version update pipeline…</div>
            <div class="validation-log" id="validation-log"></div>
        `;
    }

    try {
        const body = {};
        if (targetVersion) body.target_version = targetVersion;

        const res = await fetch(`/api/services/${encodeURIComponent(serviceId)}/update-api-version`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
            signal: _apiUpdateAbort ? _apiUpdateAbort.signal : undefined,
        });

        console.log('[update] fetch response status:', res.status);

        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'API version update failed');
        }

        showToast('API version update pipeline started…', 'info');

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let _updateFailed = false;
        let _updateCompleted = false;  // tracks whether a terminal event was received

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
                    console.log('[update] event:', event.type, event.phase, event.detail?.substring(0, 80));
                    if (event.type === 'error') { _updateFailed = true; _updateCompleted = true; }
                    if (event.type === 'done') _updateCompleted = true;
                    _handleUpdateEvent(event);
                } catch (e) { console.warn('[update] parse error:', e, line.substring(0, 100)); }
            }
        }

        if (buffer.trim()) {
            try {
                const last = JSON.parse(buffer);
                if (last.type === 'error') { _updateFailed = true; _updateCompleted = true; }
                if (last.type === 'done') _updateCompleted = true;
                _handleUpdateEvent(last);
            } catch (e) {}
        }

        // Detect stream interruption: server closed the connection without a terminal event
        if (!_updateCompleted && !_updateFailed) {
            showToast('Pipeline stream ended unexpectedly — the update may not have completed. Refresh to check status.', 'warning');
            const detail = document.getElementById('validation-detail');
            if (detail) detail.textContent = 'Pipeline interrupted — the update may not have completed. Refresh to check status.';
            _handleUpdateEvent({ type: 'error', phase: 'interrupted', detail: 'Pipeline stream ended without a completion signal. The server may have restarted. Refresh the page to check the current status.' });
            _updateFailed = true;
        }

        // Only reload & re-render drawer on success — on error, keep the error card visible
        if (!_updateFailed) {
            await loadAllData();
            await showServiceDetail(serviceId);
        } else {
            // Refresh data in background but don't re-render the drawer
            await loadAllData();
        }

    } catch (err) {
        const isNetworkErr = err.name === 'TypeError' || /network|fetch|aborted|failed to fetch/i.test(err.message);
        if (isNetworkErr) {
            showToast('Connection interrupted — the update may still be running. Refresh to check status.', 'warning');
            const detail = document.getElementById('validation-detail');
            if (detail) detail.textContent = 'Connection lost — update may still be running. Refresh the page to check the latest status.';
        } else {
            showToast(`API version update failed: ${err.message}`, 'error');
            const detail = document.getElementById('validation-detail');
            if (detail) detail.textContent = `Error: ${err.message}`;
            const cardEl = document.getElementById('validation-card');
            if (cardEl) cardEl.className = 'validation-card validation-failed';
        }
    } finally {
        _apiUpdateRunning = false;
        _apiUpdateAbort = null;
    }
}

/** Update the table's update badge with live pipeline status */
function _updateTableBadge(event) {
    if (!_apiUpdateBadgeId) return;
    const tblBadge = document.getElementById(_apiUpdateBadgeId);
    if (!tblBadge) return;

    // Map phases to short labels
    const phaseLabels = {
        checkout: 'Checking out…', checkout_complete: 'Checked out',
        updating: 'Rewriting…', update_complete: 'Rewritten',
        saved: 'Saved draft',
        static_policy_check: 'Policy check…', static_policy_complete: 'Policies OK',
        static_policy_failed: 'Policy issues',
        what_if: 'What-If…', what_if_complete: 'What-If OK', what_if_failed: 'What-If issue',
        deploying: 'Deploying…', deploy_complete: 'Deployed', deploy_failed: 'Deploy issue',
        policy_testing: 'Compliance…', policy_testing_complete: 'Compliant',
        policy_deploy: 'Deploying policy…', policy_deploy_complete: 'Policy deployed',
        cleanup: 'Cleaning up…', cleanup_complete: 'Cleaned up',
        promoting: 'Publishing…', fixing_template: 'Healing…',
    };

    if (event.type === 'done') {
        tblBadge.classList.remove('version-update-running');
        tblBadge.classList.add('version-update-done');
        tblBadge.innerHTML = '✓ Updated';
        _apiUpdateBadgeId = null;
    } else if (event.type === 'error') {
        tblBadge.classList.remove('version-update-running');
        tblBadge.classList.add('version-update-error');
        tblBadge.innerHTML = '⚠ Failed';
        _apiUpdateBadgeId = null;
    } else if (event.phase && phaseLabels[event.phase]) {
        tblBadge.innerHTML = `<span class="update-badge-spinner"></span> ${phaseLabels[event.phase]}`;
    }
}

// ══════════════════════════════════════════════════════════════
// LOGIC APPS–STYLE FLOW CARD HELPERS
// ══════════════════════════════════════════════════════════════

/** ─── Pipeline overlay management ──────────────────────── */
let _pipelineOverlayOpen = false;

function openPipelineOverlay(title, icon, meta) {
    const overlay = document.getElementById('pipeline-overlay');
    if (!overlay) return;
    const titleEl = document.getElementById('pipeline-overlay-title');
    const iconEl = document.getElementById('pipeline-overlay-icon');
    const metaEl = document.getElementById('pipeline-overlay-meta');
    if (titleEl) titleEl.textContent = title || 'Pipeline';
    if (iconEl) iconEl.textContent = icon || '🚀';
    if (metaEl) metaEl.textContent = meta || '';
    // Clear previous canvas content
    const canvas = document.getElementById('pipeline-canvas');
    if (canvas) {
        canvas.innerHTML = '';
        canvas._flow = null;
    }
    overlay.classList.remove('hidden');
    _pipelineOverlayOpen = true;
}

function closePipelineOverlay() {
    const overlay = document.getElementById('pipeline-overlay');
    if (overlay) overlay.classList.add('hidden');
    _pipelineOverlayOpen = false;
}

/** Reopen the overlay (preserves existing canvas content) */
function reopenPipelineOverlay() {
    const overlay = document.getElementById('pipeline-overlay');
    if (!overlay) return;
    overlay.classList.remove('hidden');
    _pipelineOverlayOpen = true;
    const canvas = document.getElementById('pipeline-canvas');
    if (canvas) canvas.scrollTop = canvas.scrollHeight;
}

/** Get the active flow container — overlay canvas if open, else validation-log */
function _getFlowTarget() {
    if (_pipelineOverlayOpen) {
        const canvas = document.getElementById('pipeline-canvas');
        if (canvas) return canvas;
    }
    return document.getElementById('validation-log');
}

/** Initialize flow state on a container */
function _flowInit(logEl) {
    if (!logEl) return;
    if (logEl._flow) return;
    logEl._flow = {
        cards: {},        // key → card element
        activeKey: null,  // currently active card key
        count: 0,
        iteration: {},    // key → current iteration number
    };
    // Keep the validation-log-header if present, remove any old log lines
    const children = Array.from(logEl.children);
    children.forEach(c => {
        if (!c.classList.contains('validation-log-header') && !c.classList.contains('uf-expand-btn')) c.remove();
    });
    logEl.classList.add('uf-flow');
    // If this is the drawer's validation-log (not the overlay canvas), add "View Pipeline" link
    if (logEl.id === 'validation-log' && _pipelineOverlayOpen && !logEl.querySelector('.uf-expand-btn')) {
        const btn = document.createElement('button');
        btn.className = 'uf-expand-btn';
        btn.textContent = 'View Pipeline ↗';
        btn.onclick = (e) => { e.stopPropagation(); reopenPipelineOverlay(); };
        logEl.appendChild(btn);
    }
}

// ══════════════════════════════════════════════════════════════
// GOVERNANCE RESOLUTION (human-in-the-loop)
// ══════════════════════════════════════════════════════════════

/** Render governance resolution UI (findings + buttons) inside the governance card */
function _renderGovernanceResolution(logEl, event) {
    const findings = event.findings || [];
    const critFindings = event.critical_findings || findings.filter(f => f.severity === 'critical' || f.severity === 'high');

    // Show findings details
    if (findings.length > 0) {
        const findingsHtml = findings.map(f => {
            const sevClass = (f.severity === 'critical' || f.severity === 'high') ? 'uf-text-error' : 'uf-text-warning';
            return `<div class="gov-finding-item">
                <span class="gov-finding-sev ${sevClass}">${escapeHtml((f.severity || 'medium').toUpperCase())}</span>
                <span class="gov-finding-cat">${escapeHtml(f.category || 'general')}</span>
                <span class="gov-finding-text">${escapeHtml(f.finding || '')}</span>
                ${f.recommendation ? `<div class="gov-finding-rec">→ ${escapeHtml(f.recommendation)}</div>` : ''}
            </div>`;
        }).join('');
        _flowDetail(logEl, 'governance', '📋',
            `<details class="gov-findings-details"><summary>${findings.length} finding(s), ${critFindings.length} critical/high</summary>` +
            `<div class="gov-findings-list">${findingsHtml}</div></details>`);
    }

    // Store for resolution
    logEl._governanceFindings = findings;
    logEl._governanceServiceId = event.service_id || '';
    logEl._governanceVersion = event.version;

    // Resolution buttons
    const resolveEl = document.createElement('div');
    resolveEl.className = 'gov-resolve-actions';
    const sid = escapeHtml(event.service_id || '');
    resolveEl.innerHTML = `
        <div class="gov-resolve-header">
            <span class="gov-resolve-icon">🔀</span>
            <span class="gov-resolve-title">Resolution Options</span>
        </div>
        <p class="gov-resolve-desc">The CISO blocked this template due to security findings. Choose how to proceed:</p>
        <div class="gov-resolve-buttons">
            <button class="btn gov-resolve-btn gov-resolve-heal" onclick="resolveGovernanceBlock('${sid}', 'heal')">
                <span class="gov-resolve-btn-icon">🤖</span>
                <span class="gov-resolve-btn-label">Auto-Heal Template</span>
                <span class="gov-resolve-btn-desc">AI fixes the template to comply with CISO findings</span>
            </button>
            <button class="btn gov-resolve-btn gov-resolve-exception" onclick="resolveGovernanceBlock('${sid}', 'exception')">
                <span class="gov-resolve-btn-icon">⚡</span>
                <span class="gov-resolve-btn-label">Request Exception</span>
                <span class="gov-resolve-btn-desc">Acknowledge findings and bypass governance for this run</span>
            </button>
            <button class="btn gov-resolve-btn gov-resolve-abort" onclick="closePipelineOverlay()">
                <span class="gov-resolve-btn-icon">✋</span>
                <span class="gov-resolve-btn-label">Abort</span>
                <span class="gov-resolve-btn-desc">Stop and fix the template manually</span>
            </button>
        </div>
    `;

    const govCard = logEl._flow?.cards?.['governance'];
    if (govCard) {
        const body = govCard.querySelector('.uf-action-body');
        if (body) {
            body.appendChild(resolveEl);
            body.classList.add('uf-body-open');
        }
    }
}

/** Handle governance block resolution: heal the template or request an exception */
async function resolveGovernanceBlock(serviceId, action) {
    const logEl = document.getElementById('pipeline-overlay-log') || document.getElementById('validation-log');
    if (!logEl) return;

    // Disable resolution buttons
    const btns = logEl.querySelectorAll('.gov-resolve-btn');
    btns.forEach(b => { b.disabled = true; b.style.opacity = '0.5'; });

    // Get stored findings
    const findings = logEl._governanceFindings || [];

    // Confirmation for exception
    if (action === 'exception') {
        const confirmed = confirm(
            `⚠️ Governance Exception Request\n\n` +
            `You are bypassing CISO security review for this template.\n` +
            `${findings.length} finding(s) will not be addressed.\n\n` +
            `This action will be logged for audit purposes.\n\n` +
            `Are you sure you want to proceed?`
        );
        if (!confirmed) {
            btns.forEach(b => { b.disabled = false; b.style.opacity = '1'; });
            return;
        }
    }

    // Update overlay header
    const metaEl = document.getElementById('pipeline-overlay-meta');
    if (metaEl) metaEl.textContent = action === 'heal' ? 'Auto-healing template…' : 'Requesting governance exception…';

    // Clear existing flow to start fresh — remove all flow cards and reset state
    if (logEl._flow) {
        delete logEl._flow;
        const children = Array.from(logEl.children);
        children.forEach(c => {
            if (!c.classList.contains('validation-log-header') && !c.classList.contains('uf-expand-btn')) c.remove();
        });
        logEl.classList.remove('uf-flow');
    }

    try {
        const res = await fetch(`/api/services/${encodeURIComponent(serviceId)}/governance-resolve`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                action,
                findings,
                acknowledged_by: 'user',
            }),
        });

        if (!res.ok) {
            let errMsg = 'Resolution request failed';
            try { const err = await res.json(); errMsg = err.detail || errMsg; } catch (_) {}
            throw new Error(errMsg);
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
            try { _handleValidationEvent(JSON.parse(buffer)); } catch (e) {}
        }

        await loadAllData();
        await showServiceDetail(serviceId);

    } catch (err) {
        showToast(`Governance resolution failed: ${err.message}`, 'error');
        _flowResult(logEl, 'failed', `Resolution failed: ${err.message}`);
    }
}

/** Create a ⊕ connector between cards */
function _createConnector(status) {
    const conn = document.createElement('div');
    const cls = status === 'done' ? 'uf-connector-done' :
                status === 'active' ? 'uf-connector-active' :
                status === 'failed' ? 'uf-connector-failed' : '';
    conn.className = `uf-connector ${cls}`.trim();
    conn.innerHTML = `<div class="uf-connector-line-top"></div><div class="uf-connector-plus">+</div><div class="uf-connector-line-bot"></div>`;
    return conn;
}

/** Create a new action card in the flow (or reopen an existing one) */
function _flowCard(logEl, key, icon, title) {
    _flowInit(logEl);
    const flow = logEl._flow;

    // If card already exists for this key — reopen it for a new iteration
    if (flow.cards[key]) {
        const existing = flow.cards[key];

        // Finalize previous active card first (if different from this one)
        if (flow.activeKey && flow.activeKey !== key && flow.cards[flow.activeKey]) {
            const prev = flow.cards[flow.activeKey];
            if (prev.classList.contains('uf-action-active')) {
                _flowFinalize(logEl, flow.activeKey, 'done');
            }
        }

        flow.activeKey = key;
        // If card was finalized, reopen it
        if (!existing.classList.contains('uf-action-active')) {
            existing.classList.remove('uf-action-done', 'uf-action-failed', 'uf-action-skipped');
            existing.classList.add('uf-action-active');
            // Restore running badge
            const badge = existing.querySelector('.uf-action-badge');
            if (badge) {
                badge.className = 'uf-action-badge uf-badge-active';
                badge.innerHTML = '<span class="uf-badge-dot"></span>';
            }
            // Restore icon bg
            const iconBox = existing.querySelector('.uf-action-icon');
            if (iconBox) iconBox.style.background = '';
            // Open body
            const body = existing.querySelector('.uf-action-body');
            if (body) body.classList.add('uf-body-open');
            // Add iteration separator inside the card body
            flow.iteration[key] = (flow.iteration[key] || 1) + 1;
            _flowIterSep(logEl, key, flow.iteration[key]);
            // Re-color the preceding connector back to active
            const prev = existing.previousElementSibling;
            if (prev && prev.classList.contains('uf-connector')) {
                prev.className = 'uf-connector uf-connector-active';
            }
        }
        logEl.scrollTop = logEl.scrollHeight;
        return existing;
    }

    // Finalize previous active card
    if (flow.activeKey && flow.cards[flow.activeKey]) {
        const prev = flow.cards[flow.activeKey];
        if (prev.classList.contains('uf-action-active')) {
            _flowFinalize(logEl, flow.activeKey, 'done');
        }
    }

    // Add ⊕ connector
    if (flow.count > 0) {
        logEl.appendChild(_createConnector('active'));
    }
    flow.count++;
    flow.iteration[key] = 1;

    const card = document.createElement('div');
    card.className = 'uf-action uf-action-active';
    card.dataset.key = key;
    card.innerHTML = `
        <div class="uf-action-head">
            <div class="uf-action-icon">${icon}</div>
            <div class="uf-action-name">${title}</div>
            <div class="uf-action-badge uf-badge-active">
                <span class="uf-badge-dot"></span>
            </div>
        </div>
        <div class="uf-action-body uf-body-open"></div>
    `;
    // Click header to expand/collapse
    const head = card.querySelector('.uf-action-head');
    head.addEventListener('click', () => {
        const body = card.querySelector('.uf-action-body');
        if (!body || !body.children.length) return;
        body.classList.toggle('uf-body-open');
    });
    logEl.appendChild(card);
    flow.cards[key] = card;
    flow.activeKey = key;
    logEl.scrollTop = logEl.scrollHeight;
    return card;
}

/** Add an iteration separator inside a card's body */
function _flowIterSep(logEl, key, iteration, note) {
    if (!logEl._flow?.cards[key]) return;
    const body = logEl._flow.cards[key].querySelector('.uf-action-body');
    if (!body) return;
    const sep = document.createElement('div');
    sep.className = 'uf-iter-sep';
    sep.innerHTML = `<span class="uf-iter-label">Iteration ${iteration}</span>${note ? `<span class="uf-iter-text">${escapeHtml(note)}</span>` : ''}`;
    body.appendChild(sep);
}

/** Add a detail line to an existing action card */
function _flowDetail(logEl, key, icon, text, extraCls) {
    if (!logEl._flow?.cards[key]) return;
    const body = logEl._flow.cards[key].querySelector('.uf-action-body');
    if (!body) return;
    const line = document.createElement('div');
    line.className = 'uf-detail-line';
    const textCls = extraCls ? ` ${extraCls}` : '';
    line.innerHTML = `<span class="uf-detail-icon">${icon}</span><span class="uf-detail-text${textCls}">${text}</span>`;
    body.appendChild(line);
    body.classList.add('uf-body-open');
    logEl.scrollTop = logEl.scrollHeight;
}

/** Finalize an action card (done / failed) */
function _flowFinalize(logEl, key, status, label) {
    if (!logEl._flow?.cards[key]) return;
    const card = logEl._flow.cards[key];
    card.classList.remove('uf-action-active');
    card.classList.add(status === 'failed' ? 'uf-action-failed' : 'uf-action-done');
    const badge = card.querySelector('.uf-action-badge');
    if (badge) {
        badge.className = `uf-action-badge ${status === 'failed' ? 'uf-badge-failed' : 'uf-badge-done'}`;
        const iter = logEl._flow.iteration[key] || 1;
        const iterLabel = iter > 1 ? ` (${iter} iterations)` : '';
        badge.innerHTML = status === 'failed' ? '✗ Failed' : (label || '✓') + iterLabel;
    }
    // Auto-collapse body of done cards
    const body = card.querySelector('.uf-action-body');
    if (body && status !== 'failed') {
        body.classList.remove('uf-body-open');
    }
    // Update preceding connector
    const prev = card.previousElementSibling;
    if (prev && prev.classList.contains('uf-connector')) {
        prev.classList.remove('uf-connector-active');
        prev.classList.add(status === 'failed' ? 'uf-connector-failed' : 'uf-connector-done');
    }
    if (logEl._flow.activeKey === key) {
        logEl._flow.activeKey = null;
    }
    // Track last failed card so healing events can target it
    if (status === 'failed') {
        logEl._flow._lastFailedKey = key;
    }
}

/** Finalize whatever card is currently active */
function _flowFinalizeActive(logEl, status) {
    if (!logEl._flow?.activeKey) return;
    _flowFinalize(logEl, logEl._flow.activeKey, status);
}

/** Add a final result block */
function _flowResult(logEl, status, text) {
    _flowFinalizeActive(logEl, status === 'success' ? 'done' : 'failed');
    logEl.appendChild(_createConnector(status === 'success' ? 'done' : (status === 'blocked' ? '' : 'failed')));
    const result = document.createElement('div');
    const cls = status === 'success' ? 'uf-result-success' : (status === 'blocked' ? 'uf-result-blocked' : 'uf-result-failed');
    const icon = status === 'success' ? '✅' : (status === 'blocked' ? '🛑' : '❌');
    result.className = `uf-result ${cls}`;
    result.innerHTML = `<div class="uf-result-icon">${icon}</div><div class="uf-result-text">${escapeHtml(text)}</div>`;
    logEl.appendChild(result);
    logEl.scrollTop = logEl.scrollHeight;
}

/** Convert raw Azure / ARM error messages into concise, friendly text */
function _friendlyError(raw) {
    if (!raw || typeof raw !== 'string') return raw || 'Unknown error';
    let msg = raw;
    // Strip the outer "(DeploymentFailed) At least one resource..." wrapper
    msg = msg.replace(/\(DeploymentFailed\)\s*At least one resource deployment operation failed\..*?Code:\s*Deploy\b/gi, '');
    // Strip "Please list deployment operations..." boilerplate
    msg = msg.replace(/Please (list|see) deployment operations.*?$/gi, '');
    msg = msg.replace(/Please see https?:\/\/\S+/gi, '');
    // Extract inner error codes: (SomeErrorCode) message
    const innerMatch = msg.match(/\((\w+)\)\s*(.+)/);
    if (innerMatch) {
        const code = innerMatch[1];
        const rest = innerMatch[2].trim().replace(/\s*Code:\s*\w+\s*$/, '').trim();
        // Map common codes to friendly phrases
        const friendly = {
            InvalidTemplate: 'Template validation error',
            InvalidTemplateDeployment: 'Template has configuration issues',
            DeploymentFailed: 'Deployment did not complete',
            BadRequest: 'Azure rejected the request',
            Conflict: 'Resource conflict',
            ResourceNotFound: 'A referenced resource was not found',
            InvalidApiVersionParameter: 'Invalid API version specified',
            LinkedAuthorizationFailed: 'Missing permissions for linked resources',
            AuthorizationFailed: 'Insufficient Azure permissions',
            AccountPropertyIsInvalid: 'Invalid account property',
            SkuNotAvailable: 'The selected SKU is not available in this region',
        };
        const label = friendly[code] || code.replace(/([a-z])([A-Z])/g, '$1 $2');
        return rest ? `${label} — ${rest.substring(0, 150)}` : label;
    }
    // Trim to something reasonable
    msg = msg.trim();
    if (msg.length > 200) msg = msg.substring(0, 200) + '…';
    return msg || 'Deployment encountered an error';
}

/** Add detail to a specific card—even if it's finalized (briefly opens the body) */
function _flowDetailOnCard(logEl, key, icon, text, extraCls) {
    if (!logEl._flow?.cards[key]) return;
    const card = logEl._flow.cards[key];
    const body = card.querySelector('.uf-action-body');
    if (!body) return;
    const line = document.createElement('div');
    const textCls = extraCls ? ` ${extraCls}` : '';
    line.innerHTML = `<span class="uf-detail-icon">${icon}</span><span class="uf-detail-text${textCls}">${text}</span>`;
    line.className = 'uf-detail-line';
    body.appendChild(line);
    // Briefly open body so user sees the new content
    body.classList.add('uf-body-open');
    logEl.scrollTop = logEl.scrollHeight;
}

// ══════════════════════════════════════════════════════════════
// UPDATE EVENT HANDLER (API Version Update Pipeline)
// ══════════════════════════════════════════════════════════════

function _handleUpdateEvent(event) {
    const progressFill = document.getElementById('validation-progress-fill');
    const detailEl = document.getElementById('validation-detail');
    const badge = document.getElementById('validation-attempt-badge');
    const modelBadge = document.getElementById('validation-model-badge');
    const card = document.getElementById('validation-card');
    const logEl = _getFlowTarget();

    // Update the table badge with live status
    _updateTableBadge(event);

    // Progress bar + detail text
    if (event.progress && progressFill) {
        progressFill.style.width = `${Math.min(event.progress * 100, 100)}%`;
    }
    if (event.detail && detailEl) {
        detailEl.textContent = event.detail;
    }
    if (event.phase === 'init_model' && event.model && modelBadge) {
        modelBadge.textContent = `🤖 ${event.model.display || event.model.id || event.model}`;
        modelBadge.classList.add('visible');
    }

    if (!logEl) return;
    _flowInit(logEl);

    const phase = event.phase || '';
    const type = event.type || '';
    const detail = event.detail || '';

    // ── Phase → flow card mapping ──
    // Cards are keyed by logical step — if the step recurs (healing loop),
    // _flowCard reopens the existing card with an iteration separator.
    if (phase === 'init_model') {
        // Model routing — show as first card with pipeline setup info
        _flowCard(logEl, 'setup', '⚙️', 'Pipeline Setup ' + _copilotTag());
        if (event.model_routing) {
            for (const [taskKey, info] of Object.entries(event.model_routing)) {
                const friendlyTask = taskKey === 'planning' ? 'Planning' : taskKey === 'code_generation' ? 'Code Generation' : taskKey === 'code_fixing' ? 'Auto-Healing' : taskKey;
                _flowDetail(logEl, 'setup', '🤖', `<strong>${escapeHtml(friendlyTask)}</strong> → ${escapeHtml(info.display)}`, 'uf-text-reasoning');
            }
        }
        if (detail) _flowDetail(logEl, 'setup', '▸', escapeHtml(detail));
    } else if (phase === 'pipeline_overview') {
        // Pipeline plan overview — add step list to setup card
        if (event.steps && event.steps.length) {
            const stepsHtml = event.steps.map((s, i) => `<strong>${i + 1}.</strong> ${escapeHtml(s)}`).join('<br>');
            _flowDetail(logEl, 'setup', '📋', stepsHtml);
        }
        _flowFinalize(logEl, 'setup', 'done', 'Ready');
    } else if (phase === 'cleanup_drafts') {
        // Stale draft cleanup — just a detail on setup
        if (detail) _flowDetailOnCard(logEl, 'setup', '🧹', escapeHtml(detail));
    } else if (phase === 'checkout') {
        _flowCard(logEl, 'checkout', '📥', 'Checking Out Template');
        if (detail) _flowDetail(logEl, 'checkout', '▸', escapeHtml(detail));
        if (event.current_api_version) {
            _flowDetail(logEl, 'checkout', 'ℹ️', `Current API: <strong>${escapeHtml(event.current_api_version)}</strong> → Target: <strong>${escapeHtml(event.target_api_version || '?')}</strong>`);
        }
    } else if (phase === 'checkout_complete') {
        if (detail) _flowDetail(logEl, 'checkout', '✓', escapeHtml(detail), 'uf-text-success');
        if (event.resource_count) {
            _flowDetail(logEl, 'checkout', 'ℹ️', `${event.resource_count} resource(s) in template`);
        }
        _flowFinalize(logEl, 'checkout', 'done');
    } else if (phase === 'planning') {
        _flowCard(logEl, 'planning', '🧠', 'Thinking & Planning ' + _copilotTag());
        if (detail) _flowDetail(logEl, 'planning', '▸', escapeHtml(detail));
    } else if (phase === 'planning_complete') {
        if (detail) _flowDetail(logEl, 'planning', '✓', escapeHtml(detail), 'uf-text-success');
        _flowFinalize(logEl, 'planning', 'done');
    } else if (phase === 'executing') {
        _flowCard(logEl, 'rewrite', '⚡', 'Rewriting Template ' + _copilotTag());
        if (detail) _flowDetail(logEl, 'rewrite', '▸', escapeHtml(detail));
    } else if (phase === 'updating') {
        _flowCard(logEl, 'rewrite', '🔄', 'Rewriting Template ' + _copilotTag());
        if (detail) _flowDetail(logEl, 'rewrite', '▸', escapeHtml(detail));
    } else if (phase === 'execute_complete' || phase === 'update_complete') {
        if (detail) _flowDetail(logEl, 'rewrite', '✓', escapeHtml(detail), 'uf-text-success');
    } else if (phase === 'execute_fallback') {
        if (detail) _flowDetail(logEl, 'rewrite', '⚠️', escapeHtml(detail));
    } else if (phase === 'saved') {
        if (detail) _flowDetail(logEl, 'rewrite', '💾', escapeHtml(detail), 'uf-text-success');
    } else if (phase === 'version_info') {
        // Version bump explanation — add to the rewrite card and then finalize it
        if (detail) _flowDetail(logEl, 'rewrite', '🏷️', escapeHtml(detail));
        if (event.bump_reason) _flowDetail(logEl, 'rewrite', 'ℹ️', `Strategy: ${escapeHtml(event.bump_reason)}`);
        _flowFinalize(logEl, 'rewrite', 'done');

    // ── Governance review gate ───────────────────────────────
    } else if (phase === 'governance_review') {
        _flowCard(logEl, 'governance', '🏛️', 'Governance Review ' + _copilotTag());
        if (detail) _flowDetail(logEl, 'governance', '▸', escapeHtml(detail));
    } else if (phase === 'ciso_review') {
        const rev = event.review || {};
        const verdictClass = rev.verdict === 'approved' ? 'uf-text-success' : rev.verdict === 'blocked' ? 'uf-text-error' : 'uf-text-warning';
        _flowDetail(logEl, 'governance', '🛡️', `<strong>CISO:</strong> ${escapeHtml(detail)}`, verdictClass);
        if (rev.findings && rev.findings.length) {
            const critCount = rev.findings.filter(f => f.severity === 'critical' || f.severity === 'high').length;
            if (critCount > 0) _flowDetail(logEl, 'governance', '⚠️', `${critCount} critical/high finding(s) require attention`);
        }
    } else if (phase === 'cto_review') {
        const rev = event.review || {};
        const verdictClass = rev.verdict === 'approved' ? 'uf-text-success' : rev.verdict === 'needs_revision' ? 'uf-text-warning' : '';
        _flowDetail(logEl, 'governance', '🏗️', `<strong>CTO:</strong> ${escapeHtml(detail)}`, verdictClass);
    } else if (phase === 'governance_blocked') {
        _flowDetail(logEl, 'governance', '🚫', escapeHtml(detail), 'uf-text-error');
        _renderGovernanceResolution(logEl, event);
        _flowFinalize(logEl, 'governance', 'failed', 'Blocked');
    } else if (phase === 'governance_complete') {
        const gateClass = event.gate_decision === 'approved' ? 'uf-text-success' : event.gate_decision === 'blocked' ? 'uf-text-error' : 'uf-text-warning';
        if (detail) _flowDetail(logEl, 'governance', '✓', escapeHtml(detail), gateClass);
        _flowFinalize(logEl, 'governance', event.gate_decision === 'blocked' ? 'failed' : 'done', event.gate_decision === 'approved' ? 'Approved' : event.gate_decision === 'blocked' ? 'Blocked' : 'Conditional');
    } else if (phase === 'governance_skipped') {
        _flowCard(logEl, 'governance', '🏛️', 'Governance Review ' + _copilotTag());
        if (detail) _flowDetail(logEl, 'governance', '⚠️', escapeHtml(detail), 'uf-text-warning');
        _flowFinalize(logEl, 'governance', 'done', 'Skipped');

    // ── Governance resolution events ─────────────────────────
    } else if (phase === 'governance_heal_start' || phase === 'governance_heal_strategy' || phase === 'governance_heal_complete') {
        if (!logEl._flow?.cards?.['gov-resolve']) {
            _flowCard(logEl, 'gov-resolve', '🔧', 'Governance Resolution ' + _copilotTag());
        }
        const icon = phase === 'governance_heal_complete' ? '✅' : phase === 'governance_heal_strategy' ? '📋' : '🤖';
        const cls = phase === 'governance_heal_complete' ? 'uf-text-success' : '';
        _flowDetail(logEl, 'gov-resolve', icon, escapeHtml(detail), cls);
        if (phase === 'governance_heal_complete') {
            _flowFinalize(logEl, 'gov-resolve', 'done', 'Healed');
        }
    } else if (phase === 'governance_exception') {
        _flowCard(logEl, 'gov-resolve', '⚡', 'Governance Exception');
        _flowDetail(logEl, 'gov-resolve', '⚡', escapeHtml(detail), 'uf-text-warning');
        _flowFinalize(logEl, 'gov-resolve', 'done', 'Exception');
    } else if (phase === 'governance_heal_failed') {
        if (!logEl._flow?.cards?.['gov-resolve']) {
            _flowCard(logEl, 'gov-resolve', '🔧', 'Governance Resolution ' + _copilotTag());
        }
        _flowDetail(logEl, 'gov-resolve', '❌', escapeHtml(detail), 'uf-text-error');
        _flowFinalize(logEl, 'gov-resolve', 'failed', 'Failed');

    } else if (phase === 'static_policy_check') {
        _flowCard(logEl, 'policy', '📋', 'Static Policy Checks');
        if (detail) _flowDetail(logEl, 'policy', '▸', escapeHtml(detail));
    } else if (phase === 'static_policy_complete') {
        if (detail) _flowDetail(logEl, 'policy', '✓', escapeHtml(detail), 'uf-text-success');
        _flowFinalize(logEl, 'policy', 'done');
    } else if (phase === 'static_policy_failed') {
        const friendly = _friendlyError(detail);
        _flowDetail(logEl, 'policy', '⚠️', escapeHtml(friendly), 'uf-text-error');
        // Don't finalize as failed — the heal loop may recover.
        logEl._flow._lastFailedKey = 'policy';
    } else if (phase === 'what_if') {
        _flowCard(logEl, 'whatif', '🔍', 'ARM What-If Analysis');
        if (detail) _flowDetail(logEl, 'whatif', '▸', escapeHtml(detail));
    } else if (phase === 'what_if_complete') {
        if (detail) _flowDetail(logEl, 'whatif', '✓', escapeHtml(detail), 'uf-text-success');
        _flowFinalize(logEl, 'whatif', 'done');
    } else if (phase === 'what_if_failed') {
        const friendly = _friendlyError(detail);
        _flowDetail(logEl, 'whatif', '⚠️', escapeHtml(friendly), 'uf-text-error');
        // Don't finalize as failed — the heal loop may recover.
        // Track it so healing events target this card.
        logEl._flow._lastFailedKey = 'whatif';
    } else if (phase === 'deploying') {
        _flowCard(logEl, 'deploy', '🚀', 'Deploying to Azure');
        if (detail) _flowDetail(logEl, 'deploy', '▸', escapeHtml(detail));
        // Show deploy metadata if available
        if (event.resource_group) _flowDetail(logEl, 'deploy', '📦', `Resource group: <strong>${escapeHtml(event.resource_group)}</strong>`);
        if (event.region) _flowDetail(logEl, 'deploy', '🌍', `Region: <strong>${escapeHtml(event.region)}</strong>`);
        if (event.deploy_mode) _flowDetail(logEl, 'deploy', 'ℹ️', `Mode: ${escapeHtml(event.deploy_mode)}`);
    } else if (phase === 'deploy_progress' || phase === 'deploy_heartbeat') {
        if (detail) _flowDetail(logEl, 'deploy', '▸', escapeHtml(detail));
    } else if (phase === 'deploy_complete') {
        if (detail) _flowDetail(logEl, 'deploy', '✓', escapeHtml(detail), 'uf-text-success');
        _flowFinalize(logEl, 'deploy', 'done');
    } else if (phase === 'deploy_failed') {
        const friendly = _friendlyError(detail);
        _flowDetail(logEl, 'deploy', '⚠️', escapeHtml(friendly), 'uf-text-error');
        // Don't finalize as failed — the heal loop may recover.
        // Track it so healing events target this card.
        logEl._flow._lastFailedKey = 'deploy';
    } else if (type === 'healing') {
        // Healing detail → goes into the LAST failed card (even though finalized)
        const healKey = logEl._flow._lastFailedKey || 'deploy';
        if (phase === 'escalating') {
            // Escalation message — reset the failed card for a new attempt
            _flowDetailOnCard(logEl, healKey, '🔄', escapeHtml(detail), 'uf-text-warning');
        } else {
            _flowDetailOnCard(logEl, healKey, '🤖', escapeHtml(detail));
        }
    } else if (type === 'healing_done') {
        const healKey = logEl._flow._lastFailedKey || 'deploy';
        if (detail) _flowDetailOnCard(logEl, healKey, '✓', escapeHtml(detail), 'uf-text-success');
    } else if (phase === 'healing_failed') {
        const healKey = logEl._flow._lastFailedKey || 'deploy';
        if (detail) _flowDetailOnCard(logEl, healKey, '⚠️', escapeHtml(detail), 'uf-text-error');

    // ── Template Regeneration (re-plan + re-generate) ──────────
    } else if (phase === 'replanning') {
        _flowCard(logEl, 'regen', '🔄', 'Re-planning Architecture ' + _copilotTag());
        if (detail) _flowDetail(logEl, 'regen', '🧠', escapeHtml(detail));
    } else if (phase === 'regenerating') {
        if (detail) _flowDetail(logEl, 'regen', '⚙️', escapeHtml(detail));
    } else if (type === 'regen_planned') {
        if (detail) _flowDetail(logEl, 'regen', '✓', escapeHtml(detail), 'uf-text-success');
    } else if (type === 'regen_complete') {
        if (detail) _flowDetail(logEl, 'regen', '✅', escapeHtml(detail), 'uf-text-success');
        _flowFinalize(logEl, 'regen', 'done');

    } else if (phase === 'analyzing_failure') {
        _flowCard(logEl, 'analysis', '🧠', 'Analyzing Failure ' + _copilotTag());
        if (detail) _flowDetail(logEl, 'analysis', '▸', escapeHtml(detail));
    } else if (type === 'agent_analysis') {
        // Rich analysis card
        _flowCard(logEl, 'agent_analysis', '🧠', 'Deployment Analysis ' + _copilotTag());
        const ac = logEl._flow.cards.agent_analysis;
        const body = ac?.querySelector('.uf-action-body');
        if (body) {
            const downgradeWarning = event.is_downgrade
                ? `<div class="agent-analysis-downgrade">⚠️ This is an API version <strong>downgrade</strong> — the target version is older than the current one.</div>` : '';
            body.innerHTML = `
                <div class="uf-analysis-body">${downgradeWarning}${renderMarkdown(detail)}</div>
                <div class="uf-analysis-meta">
                    ${event.from_api ? `<span class="uf-analysis-chip">${escapeHtml(event.from_api)} → ${escapeHtml(event.to_api)}</span>` : ''}
                    <span class="uf-analysis-chip">${event.attempts || '?'} iteration(s)</span>
                </div>
            `;
            body.classList.add('uf-body-open');
        }
        _flowFinalize(logEl, 'agent_analysis', 'done', 'Analysis');
    } else if (phase === 'fixing_template') {
        // Fixing goes into the currently active card as detail
        const fixKey = logEl._flow.activeKey || 'deploy';
        if (detail) _flowDetail(logEl, fixKey, '🔧', escapeHtml(detail));
    } else if (phase === 'template_fixed') {
        const fixKey = logEl._flow.activeKey || 'deploy';
        if (detail) _flowDetail(logEl, fixKey, '✓', escapeHtml(detail), 'uf-text-success');
    } else if (phase === 'policy_testing') {
        _flowCard(logEl, 'compliance', '🛡️', 'Runtime Compliance Test');
        if (detail) _flowDetail(logEl, 'compliance', '▸', escapeHtml(detail));
    } else if (phase === 'policy_testing_complete') {
        if (detail) _flowDetail(logEl, 'compliance', '✓', escapeHtml(detail), 'uf-text-success');
        _flowFinalize(logEl, 'compliance', 'done');
    } else if (phase === 'policy_deploy') {
        _flowCard(logEl, 'policydeploy', '📜', 'Deploying Policy');
        if (detail) _flowDetail(logEl, 'policydeploy', '▸', escapeHtml(detail));
    } else if (phase === 'policy_deploy_complete') {
        if (detail) _flowDetail(logEl, 'policydeploy', '✓', escapeHtml(detail), 'uf-text-success');
        _flowFinalize(logEl, 'policydeploy', 'done');
    } else if (phase === 'cleanup') {
        _flowCard(logEl, 'cleanup', '🧹', 'Cleaning Up');
        if (detail) _flowDetail(logEl, 'cleanup', '▸', escapeHtml(detail));
    } else if (phase === 'cleanup_complete') {
        if (detail) _flowDetail(logEl, 'cleanup', '✓', escapeHtml(detail), 'uf-text-success');
        _flowFinalize(logEl, 'cleanup', 'done');
    } else if (phase === 'promoting') {
        _flowCard(logEl, 'publishing', '🏆', 'Publishing Version');
        if (detail) _flowDetail(logEl, 'publishing', '▸', escapeHtml(detail));
    } else if (phase === 'infra_retry') {
        // Transient Azure error — add as detail in active or last failed card
        const k = logEl._flow.activeKey || logEl._flow._lastFailedKey;
        if (k && detail) _flowDetailOnCard(logEl, k, '🔄', escapeHtml(detail));
    } else if (type === 'llm_reasoning') {
        // Route planning-phase reasoning into the planning card specifically
        if (phase === 'planning' || phase === 'init_model') {
            const targetKey = phase === 'planning' ? 'planning' : 'setup';
            if (logEl._flow?.cards[targetKey]) {
                _flowDetailOnCard(logEl, targetKey, '🧠', escapeHtml(detail), 'uf-text-reasoning');
            }
        } else if (phase === 'replanning') {
            // Regen planning reasoning goes into the regen card
            if (logEl._flow?.cards['regen']) {
                _flowDetailOnCard(logEl, 'regen', '🧠', escapeHtml(detail), 'uf-text-reasoning');
            }
        } else if (phase === 'analyzing_deploy_failure' || phase === 'analyzing_whatif_failure') {
            // Root cause analysis goes into the failed card
            const healKey = logEl._flow._lastFailedKey || 'deploy';
            _flowDetailOnCard(logEl, healKey, '🧠', escapeHtml(detail), 'uf-text-reasoning');
        } else if (phase === 'healing') {
            // Healing reasoning goes into the last failed card
            const healKey = logEl._flow._lastFailedKey || 'deploy';
            _flowDetailOnCard(logEl, healKey, '🔧', escapeHtml(detail), 'uf-text-reasoning');
        } else {
            const k = logEl._flow.activeKey || logEl._flow._lastFailedKey;
            if (k && detail) _flowDetailOnCard(logEl, k, '🧠', escapeHtml(detail), 'uf-text-reasoning');
        }
    } else if (type === 'done') {
        _flowFinalizeActive(logEl, 'done');
        _flowResult(logEl, 'success', detail || `Version updated — v${event.new_semver || '?'}`);
    } else if (type === 'error') {
        _flowFinalizeActive(logEl, 'failed');
        _flowResult(logEl, 'failed', detail || 'Update failed');
    } else if (detail) {
        const activeKey = logEl._flow?.activeKey;
        if (activeKey) _flowDetail(logEl, activeKey, '▸', escapeHtml(detail));
    }

    // ── Overlay header live update ──
    if (_pipelineOverlayOpen) {
        const metaEl = document.getElementById('pipeline-overlay-meta');
        if (metaEl && detail) metaEl.textContent = detail;
    }

    // ── Outer card header updates ──
    const header = card?.querySelector('.validation-title');
    const iconEl = card?.querySelector('.validation-icon');

    if (phase === 'init_model' && header) {
        header.textContent = 'Setting Up Pipeline…';
        if (iconEl) { iconEl.textContent = '⚙️'; iconEl.classList.add('validation-spinner'); }
    } else if (phase === 'checkout' && header) {
        header.textContent = 'Checking Out Template…';
        if (iconEl) { iconEl.textContent = '📥'; iconEl.classList.add('validation-spinner'); }
    } else if (phase === 'planning' && header) {
        header.textContent = 'AI Analyzing & Planning…';
        if (iconEl) { iconEl.textContent = '🧠'; iconEl.classList.add('validation-spinner'); }
    } else if ((phase === 'updating' || phase === 'executing') && header) {
        header.textContent = 'Rewriting Template…';
        if (iconEl) { iconEl.textContent = '⚡'; iconEl.classList.add('validation-spinner'); }
    } else if (phase === 'static_policy_check' && header) {
        header.textContent = 'Checking Governance Policies…';
        if (iconEl) { iconEl.textContent = '📋'; iconEl.classList.add('validation-spinner'); }
    } else if (phase === 'what_if' && header) {
        header.textContent = 'Running ARM What-If Analysis…';
        if (iconEl) { iconEl.textContent = '🔍'; iconEl.classList.add('validation-spinner'); }
    } else if (phase === 'deploying' && header) {
        header.textContent = 'Deploying to Validation RG…';
        if (iconEl) { iconEl.textContent = '🚀'; iconEl.classList.add('validation-spinner'); }
    } else if (phase === 'policy_testing' && header) {
        header.textContent = 'Testing Runtime Compliance…';
        if (iconEl) { iconEl.textContent = '🛡️'; iconEl.classList.add('validation-spinner'); }
    } else if (phase === 'cleanup' && header) {
        header.textContent = 'Cleaning Up…';
        if (iconEl) { iconEl.textContent = '🧹'; }
    } else if (phase === 'promoting' && header) {
        header.textContent = 'Publishing New Version…';
        if (iconEl) { iconEl.textContent = '🏆'; iconEl.classList.add('validation-spinner'); }
    } else if (type === 'healing' && header) {
        header.innerHTML = 'Auto-Healing — AI Fixing Template… ' + _copilotTag();
        if (iconEl) { iconEl.textContent = '🤖'; iconEl.classList.add('validation-spinner'); }
    } else if (phase === 'analyzing_failure' && header) {
        header.textContent = 'Analyzing Failure…';
        if (iconEl) { iconEl.textContent = '🧠'; iconEl.classList.add('validation-spinner'); }
    } else if (type === 'agent_analysis' && header) {
        header.textContent = 'Update Failed — See Analysis Below';
        if (iconEl) { iconEl.textContent = '🧠'; iconEl.classList.remove('validation-spinner'); }
    }

    // Final states on outer card
    if (type === 'done' && card) {
        card.className = 'validation-card validation-succeeded';
        if (header) header.textContent = `API Version Updated — v${event.new_semver || '?'}`;
        if (iconEl) { iconEl.textContent = '✅'; iconEl.classList.remove('validation-spinner'); }
    } else if (type === 'error' && card) {
        card.className = 'validation-card validation-failed';
        if (header) header.textContent = 'API Version Update Failed';
        if (iconEl) { iconEl.textContent = '⛔'; iconEl.classList.remove('validation-spinner'); }
    }
}

// ══════════════════════════════════════════════════════════════
// VALIDATION EVENT HANDLER (Onboarding Pipeline)
// ══════════════════════════════════════════════════════════════

function _handleValidationEvent(event) {
    const progressFill = document.getElementById('validation-progress-fill');
    const detailEl = document.getElementById('validation-detail');
    const badge = document.getElementById('validation-attempt-badge');
    const modelBadge = document.getElementById('validation-model-badge');
    const card = document.getElementById('validation-card');
    const logEl = _getFlowTarget();

    // Progress bar + detail text
    if (event.progress && progressFill) {
        progressFill.style.width = `${Math.min(event.progress * 100, 100)}%`;
    }
    if (event.detail && detailEl) {
        detailEl.textContent = event.detail;
    }
    if (event.phase === 'init_model' && event.model && modelBadge) {
        modelBadge.textContent = `🤖 ${event.model.display || event.model.id}`;
        modelBadge.classList.add('visible');
    }

    if (!logEl) return;
    _flowInit(logEl);

    const phase = event.phase || '';
    const type = event.type || '';
    const detail = event.detail || '';

    // ── Phase → flow card mapping ──
    // Cards reuse their key across iterations — _flowCard reopens
    // a finalized card and inserts an iteration separator inside it.
    if (phase === 'init_model') {
        _flowCard(logEl, 'setup', '⚙️', 'Pipeline Setup ' + _copilotTag());
        if (event.model_routing) {
            for (const [taskKey, info] of Object.entries(event.model_routing)) {
                const friendlyTask = taskKey === 'planning' ? 'Planning' : taskKey === 'code_generation' ? 'Code Generation' : taskKey === 'code_fixing' ? 'Auto-Healing' : taskKey === 'policy_gen' ? 'Policy Generation' : taskKey === 'analysis' ? 'Analysis' : taskKey;
                _flowDetail(logEl, 'setup', '🤖', `<strong>${escapeHtml(friendlyTask)}</strong> → ${escapeHtml(info.display)}`, 'uf-text-reasoning');
            }
        }
        if (detail) _flowDetail(logEl, 'setup', '▸', escapeHtml(detail));
    } else if (phase === 'pipeline_overview') {
        if (event.steps && event.steps.length) {
            const stepsHtml = event.steps.map((s, i) => `<strong>${i + 1}.</strong> ${escapeHtml(s)}`).join('<br>');
            _flowDetail(logEl, 'setup', '📋', stepsHtml);
        }
        _flowFinalize(logEl, 'setup', 'done', 'Ready');
    } else if (phase === 'init_complete') {
        if (!logEl._flow?.cards['setup']) {
            // Fallback if setup card wasn't created
        } else {
            _flowFinalize(logEl, 'setup', 'done', 'Ready');
        }
    } else if (phase === 'cleanup_drafts') {
        if (detail) _flowDetailOnCard(logEl, 'setup', '🧹', escapeHtml(detail));
    } else if (phase === 'standards_analysis') {
        _flowCard(logEl, 'standards', '📋', 'Analyzing Standards');
        if (detail) _flowDetail(logEl, 'standards', '▸', escapeHtml(detail));
    } else if (phase === 'standards_complete') {
        if (detail) _flowDetail(logEl, 'standards', '✓', escapeHtml(detail), 'uf-text-success');
        _flowFinalize(logEl, 'standards', 'done');
    } else if (type === 'standard_check') {
        _flowDetail(logEl, logEl._flow.activeKey || 'standards', '📏', escapeHtml(detail));
    } else if (phase === 'planning') {
        _flowCard(logEl, 'planning', '🧠', 'AI Planning Architecture ' + _copilotTag());
        if (detail) _flowDetail(logEl, 'planning', '▸', escapeHtml(detail));
    } else if (phase === 'planning_complete') {
        if (detail) _flowDetail(logEl, 'planning', '✓', escapeHtml(detail), 'uf-text-success');
        _flowFinalize(logEl, 'planning', 'done');
    } else if (phase === 'generating') {
        _flowCard(logEl, 'generating', '⚡', 'Generating ARM Template ' + _copilotTag());
        if (detail) _flowDetail(logEl, 'generating', '▸', escapeHtml(detail));
    } else if (phase === 'generated') {
        if (detail) _flowDetail(logEl, 'generating', '✓', escapeHtml(detail), 'uf-text-success');
        _flowFinalize(logEl, 'generating', 'done');
    } else if (phase === 'policy_generation') {
        _flowCard(logEl, 'policyGen', '🛡️', 'Generating Azure Policy ' + _copilotTag());
        if (detail) _flowDetail(logEl, 'policyGen', '▸', escapeHtml(detail));
    } else if (phase === 'policy_generation_complete') {
        if (detail) _flowDetail(logEl, 'policyGen', '✓', escapeHtml(detail), 'uf-text-success');
        _flowFinalize(logEl, 'policyGen', 'done');
    } else if (phase === 'policy_generation_warning') {
        if (detail) _flowDetail(logEl, 'policyGen', '⚠️', escapeHtml(detail));

    // ── Governance review gate ───────────────────────────────
    } else if (phase === 'governance_review') {
        _flowCard(logEl, 'governance', '🏛️', 'Governance Review ' + _copilotTag());
        if (detail) _flowDetail(logEl, 'governance', '▸', escapeHtml(detail));
    } else if (phase === 'ciso_review') {
        const rev = event.review || {};
        const verdictClass = rev.verdict === 'approved' ? 'uf-text-success' : rev.verdict === 'blocked' ? 'uf-text-error' : 'uf-text-warning';
        _flowDetail(logEl, 'governance', '🛡️', `<strong>CISO:</strong> ${escapeHtml(detail)}`, verdictClass);
        if (rev.findings && rev.findings.length) {
            const critCount = rev.findings.filter(f => f.severity === 'critical' || f.severity === 'high').length;
            if (critCount > 0) _flowDetail(logEl, 'governance', '⚠️', `${critCount} critical/high finding(s) require attention`);
        }
    } else if (phase === 'cto_review') {
        const rev = event.review || {};
        const verdictClass = rev.verdict === 'approved' ? 'uf-text-success' : rev.verdict === 'needs_revision' ? 'uf-text-warning' : '';
        _flowDetail(logEl, 'governance', '🏗️', `<strong>CTO:</strong> ${escapeHtml(detail)}`, verdictClass);
    } else if (phase === 'governance_blocked') {
        _flowDetail(logEl, 'governance', '🚫', escapeHtml(detail), 'uf-text-error');
        _renderGovernanceResolution(logEl, event);
        _flowFinalize(logEl, 'governance', 'failed', 'Blocked');
    } else if (phase === 'governance_complete') {
        const gateClass = event.gate_decision === 'approved' ? 'uf-text-success' : event.gate_decision === 'blocked' ? 'uf-text-error' : 'uf-text-warning';
        if (detail) _flowDetail(logEl, 'governance', '✓', escapeHtml(detail), gateClass);
        _flowFinalize(logEl, 'governance', event.gate_decision === 'blocked' ? 'failed' : 'done', event.gate_decision === 'approved' ? 'Approved' : event.gate_decision === 'blocked' ? 'Blocked' : 'Conditional');
    } else if (phase === 'governance_skipped') {
        _flowCard(logEl, 'governance', '🏛️', 'Governance Review ' + _copilotTag());
        if (detail) _flowDetail(logEl, 'governance', '⚠️', escapeHtml(detail), 'uf-text-warning');
        _flowFinalize(logEl, 'governance', 'done', 'Skipped');

    // ── Governance resolution events (update handler) ────────
    } else if (phase === 'governance_heal_start' || phase === 'governance_heal_strategy' || phase === 'governance_heal_complete') {
        if (!logEl._flow?.cards?.['gov-resolve']) {
            _flowCard(logEl, 'gov-resolve', '🔧', 'Governance Resolution ' + _copilotTag());
        }
        const icon = phase === 'governance_heal_complete' ? '✅' : phase === 'governance_heal_strategy' ? '📋' : '🤖';
        const cls = phase === 'governance_heal_complete' ? 'uf-text-success' : '';
        _flowDetail(logEl, 'gov-resolve', icon, escapeHtml(detail), cls);
        if (phase === 'governance_heal_complete') {
            _flowFinalize(logEl, 'gov-resolve', 'done', 'Healed');
        }
    } else if (phase === 'governance_exception') {
        _flowCard(logEl, 'gov-resolve', '⚡', 'Governance Exception');
        _flowDetail(logEl, 'gov-resolve', '⚡', escapeHtml(detail), 'uf-text-warning');
        _flowFinalize(logEl, 'gov-resolve', 'done', 'Exception');
    } else if (phase === 'governance_heal_failed') {
        if (!logEl._flow?.cards?.['gov-resolve']) {
            _flowCard(logEl, 'gov-resolve', '🔧', 'Governance Resolution ' + _copilotTag());
        }
        _flowDetail(logEl, 'gov-resolve', '❌', escapeHtml(detail), 'uf-text-error');
        _flowFinalize(logEl, 'gov-resolve', 'failed', 'Failed');

    } else if (phase === 'static_policy_check') {
        _flowCard(logEl, 'staticPolicy', '📋', 'Static Policy Checks');
        if (detail) _flowDetail(logEl, 'staticPolicy', '▸', escapeHtml(detail));
    } else if (phase === 'static_policy_complete') {
        if (detail) _flowDetail(logEl, 'staticPolicy', '✓', escapeHtml(detail), 'uf-text-success');
        _flowFinalize(logEl, 'staticPolicy', 'done');
    } else if (phase === 'static_policy_failed') {
        const friendly = _friendlyError(detail);
        _flowDetail(logEl, 'staticPolicy', '⚠️', escapeHtml(friendly), 'uf-text-error');
        _flowFinalize(logEl, 'staticPolicy', 'failed');
    } else if (phase === 'what_if') {
        _flowCard(logEl, 'whatif', '🔍', 'ARM What-If Analysis');
        if (detail) _flowDetail(logEl, 'whatif', '▸', escapeHtml(detail));
    } else if (phase === 'what_if_complete') {
        if (detail) _flowDetail(logEl, 'whatif', '✓', escapeHtml(detail), 'uf-text-success');
        _flowFinalize(logEl, 'whatif', 'done');
    } else if (phase === 'deploying') {
        _flowCard(logEl, 'deploy', '🚀', 'Deploying to Azure');
        if (detail) _flowDetail(logEl, 'deploy', '▸', escapeHtml(detail));
        if (event.resource_group) _flowDetail(logEl, 'deploy', '📦', `Resource group: <strong>${escapeHtml(event.resource_group)}</strong>`);
        if (event.region) _flowDetail(logEl, 'deploy', '🌍', `Region: <strong>${escapeHtml(event.region)}</strong>`);
    } else if (phase === 'deploy_progress' || phase === 'deploy_heartbeat') {
        if (detail) _flowDetail(logEl, 'deploy', '▸', escapeHtml(detail));
    } else if (phase === 'deploy_complete') {
        if (detail) _flowDetail(logEl, 'deploy', '✓', escapeHtml(detail), 'uf-text-success');
        _flowFinalize(logEl, 'deploy', 'done');
    } else if (phase === 'deploy_failed') {
        const friendly = _friendlyError(detail);
        _flowDetail(logEl, 'deploy', '⚠️', escapeHtml(friendly), 'uf-text-error');
        _flowFinalize(logEl, 'deploy', 'failed');
    } else if (phase === 'resource_check') {
        _flowCard(logEl, 'resourceCheck', '🔎', 'Checking Resources');
        if (detail) _flowDetail(logEl, 'resourceCheck', '▸', escapeHtml(detail));
    } else if (phase === 'resource_check_complete') {
        if (detail) _flowDetail(logEl, 'resourceCheck', '✓', escapeHtml(detail), 'uf-text-success');
        _flowFinalize(logEl, 'resourceCheck', 'done');
    } else if (phase === 'policy_testing') {
        _flowCard(logEl, 'policyTest', '🛡️', 'Runtime Policy Testing');
        if (detail) _flowDetail(logEl, 'policyTest', '▸', escapeHtml(detail));
    } else if (phase === 'policy_testing_complete') {
        if (detail) _flowDetail(logEl, 'policyTest', '✓', escapeHtml(detail), 'uf-text-success');
        _flowFinalize(logEl, 'policyTest', 'done');
    } else if (phase === 'policy_failed') {
        if (detail) _flowDetail(logEl, 'policyTest', '❌', escapeHtml(detail), 'uf-text-error');
        _flowFinalize(logEl, 'policyTest', 'failed');
    } else if (phase === 'policy_skip') {
        if (detail) _flowDetail(logEl, logEl._flow.activeKey || 'policyTest', 'ℹ️', escapeHtml(detail));

    // ── Infrastructure Testing ──
    } else if (phase === 'testing_start') {
        _flowCard(logEl, 'infraTest', '🧪', 'Infrastructure Tests');
        if (detail) _flowDetail(logEl, 'infraTest', '▸', escapeHtml(detail));
    } else if (phase === 'testing_generate') {
        if (!logEl._flow?.cards['infraTest']) _flowCard(logEl, 'infraTest', '🧪', 'Infrastructure Tests');
        const icon = event.status === 'complete' ? '✓' : event.status === 'error' ? '⚠️' : '▸';
        const cls = event.status === 'complete' ? 'uf-text-success' : event.status === 'error' ? 'uf-text-error' : '';
        if (detail) _flowDetail(logEl, 'infraTest', icon, escapeHtml(detail), cls);
    } else if (phase === 'testing_execute') {
        if (detail) _flowDetail(logEl, 'infraTest', '▸', escapeHtml(detail));
    } else if (phase === 'test_result') {
        const passed = event.status === 'passed';
        const icon = passed ? '✅' : '❌';
        const cls = passed ? 'uf-text-success' : 'uf-text-error';
        if (detail) _flowDetail(logEl, 'infraTest', icon, escapeHtml(detail), cls);
    } else if (phase === 'testing_analyze') {
        const icon = event.status === 'complete' ? '🔍' : '▸';
        if (detail) _flowDetail(logEl, 'infraTest', icon, escapeHtml(detail));
    } else if (phase === 'testing_feedback') {
        if (detail) _flowDetail(logEl, 'infraTest', '💡', escapeHtml(detail), 'uf-text-warning');
    } else if (phase === 'testing_complete') {
        const allPassed = event.status === 'passed';
        const skipped = event.status === 'skipped';
        const icon = allPassed ? '✓' : skipped ? 'ℹ️' : '⚠️';
        const cls = allPassed ? 'uf-text-success' : skipped ? '' : 'uf-text-error';
        if (detail) _flowDetail(logEl, 'infraTest', icon, escapeHtml(detail), cls);
        _flowFinalize(logEl, 'infraTest', allPassed || skipped ? 'done' : 'failed');

    } else if (phase === 'fixing_policy') {
        // Policy fixing goes into the active card as detail
        const k = logEl._flow.activeKey || 'policyTest';
        if (detail) _flowDetail(logEl, k, '🔧', escapeHtml(detail));
    } else if (phase === 'policy_fixed') {
        const k = logEl._flow.activeKey || 'policyTest';
        if (detail) _flowDetail(logEl, k, '✓', escapeHtml(detail), 'uf-text-success');
    } else if (phase === 'policy_deploy') {
        _flowCard(logEl, 'policyDeploy', '📜', 'Deploying Policy');
        if (detail) _flowDetail(logEl, 'policyDeploy', '▸', escapeHtml(detail));
    } else if (phase === 'policy_deploy_complete') {
        if (detail) _flowDetail(logEl, 'policyDeploy', '✓', escapeHtml(detail), 'uf-text-success');
        _flowFinalize(logEl, 'policyDeploy', 'done');
    } else if (phase === 'cleanup') {
        _flowCard(logEl, 'cleanup', '🧹', 'Cleaning Up');
        if (detail) _flowDetail(logEl, 'cleanup', '▸', escapeHtml(detail));
    } else if (phase === 'cleanup_complete') {
        if (detail) _flowDetail(logEl, 'cleanup', '✓', escapeHtml(detail), 'uf-text-success');
        _flowFinalize(logEl, 'cleanup', 'done');
    } else if (phase === 'promoting') {
        _flowCard(logEl, 'promoting', '🏆', 'Publishing Version');
        if (detail) _flowDetail(logEl, 'promoting', '▸', escapeHtml(detail));
    } else if (phase === 'co_onboarding') {
        _flowCard(logEl, 'coOnboard', '👶', 'Co-boarding Dependencies');
        if (detail) _flowDetail(logEl, 'coOnboard', '▸', escapeHtml(detail));
    } else if (phase === 'infra_retry') {
        // Transient Azure error — add as detail in the active or last failed card
        const k = logEl._flow.activeKey || logEl._flow._lastFailedKey;
        if (k && detail) _flowDetailOnCard(logEl, k, '🔄', escapeHtml(detail));
    } else if (type === 'healing') {
        // Healing detail → goes into the LAST failed card (even though finalized)
        const k = logEl._flow._lastFailedKey || logEl._flow.activeKey || 'deploy';
        if (detail) _flowDetailOnCard(logEl, k, '🤖', escapeHtml(detail));
    } else if (type === 'healing_done') {
        const k = logEl._flow._lastFailedKey || logEl._flow.activeKey || 'deploy';
        if (detail) _flowDetailOnCard(logEl, k, '✓', escapeHtml(detail), 'uf-text-success');

    // ── Template Regeneration (re-plan + re-generate) ──────────
    } else if (phase === 'replanning') {
        _flowCard(logEl, 'regen', '🔄', 'Re-planning Architecture ' + _copilotTag());
        if (detail) _flowDetail(logEl, 'regen', '🧠', escapeHtml(detail));
    } else if (phase === 'regenerating') {
        if (detail) _flowDetail(logEl, 'regen', '⚙️', escapeHtml(detail));
    } else if (type === 'regen_planned') {
        if (detail) _flowDetail(logEl, 'regen', '✓', escapeHtml(detail), 'uf-text-success');
    } else if (type === 'regen_complete') {
        if (detail) _flowDetail(logEl, 'regen', '✅', escapeHtml(detail), 'uf-text-success');
        _flowFinalize(logEl, 'regen', 'done');

    } else if (type === 'llm_reasoning') {
        // Route init_model phase reasoning to setup card
        if (phase === 'init_model') {
            if (logEl._flow?.cards['setup']) {
                _flowDetailOnCard(logEl, 'setup', '🧠', escapeHtml(detail), 'uf-text-reasoning');
            }
        } else {
            const k = logEl._flow.activeKey || logEl._flow._lastFailedKey;
            if (k && detail) _flowDetailOnCard(logEl, k, '🧠', escapeHtml(detail), 'uf-text-reasoning');
        }
    } else if (type === 'policy_result') {
        const k = logEl._flow.activeKey;
        if (k && detail) {
            const passed = event.compliant !== undefined ? event.compliant : event.passed;
            const icon = passed ? '✅' : ((event.severity === 'high' || event.severity === 'critical') ? '❌' : '⚠️');
            const cls = passed ? 'uf-text-success' : 'uf-text-error';
            _flowDetail(logEl, k, icon, escapeHtml(detail), cls);
        }
    } else if (type === 'done') {
        _flowFinalizeActive(logEl, 'done');
        const text = detail || `Service approved — v${event.semver || event.version + '.0.0'}`;
        _flowResult(logEl, 'success', text);
    } else if (type === 'policy_blocked') {
        _flowFinalizeActive(logEl, 'failed');
        _flowResult(logEl, 'blocked', 'Policy review needed');
        if (event.violations) {
            const guidanceEl = document.createElement('div');
            guidanceEl.className = 'policy-blocked-guidance';
            const violationList = event.violations.map(v =>
                `<li><strong>${escapeHtml(v.type)}/${escapeHtml(v.resource)}</strong> — ${escapeHtml(v.reason)}</li>`
            ).join('');
            guidanceEl.innerHTML = `
                <div class="policy-blocked-summary">
                    <p>The ARM template deployed successfully, but <strong>${event.violations.length} resource(s)</strong>
                    did not pass the generated governance policy.</p>
                    <details><summary>Violations</summary><ul>${violationList}</ul></details>
                    <p class="policy-blocked-options"><strong>Options:</strong></p>
                    <ul>
                        <li>Ask InfraForge to submit a <strong>policy exception request</strong></li>
                        <li>Ask the platform team to adjust governance standards</li>
                        <li><button class="btn btn-sm btn-accent" onclick="triggerOnboarding('${escapeHtml(card?.dataset?.serviceId || '')}')">
                            Retry Onboarding</button> — the policy will be regenerated</li>
                    </ul>
                </div>
            `;
            logEl.appendChild(guidanceEl);
            logEl.scrollTop = logEl.scrollHeight;
        }
    } else if (type === 'error') {
        _flowFinalizeActive(logEl, 'failed');
        _flowResult(logEl, 'failed', detail || 'Onboarding failed');
    } else if (detail) {
        const k = logEl._flow?.activeKey;
        if (k) _flowDetail(logEl, k, '▸', escapeHtml(detail));
    }

    // ── Overlay header live update ──
    if (_pipelineOverlayOpen) {
        const metaEl = document.getElementById('pipeline-overlay-meta');
        if (metaEl && detail) metaEl.textContent = detail;
    }

    // ── Outer card header updates ──
    const header = card?.querySelector('.validation-title');
    const iconEl = card?.querySelector('.validation-icon');

    if (phase === 'init_model' && header) {
        header.textContent = 'Setting Up Pipeline…';
        if (iconEl) { iconEl.textContent = '⚙️'; iconEl.classList.add('validation-spinner'); }
    } else if (phase === 'standards_analysis' && header) {
        header.textContent = 'Analyzing Organization Standards…';
        if (iconEl) { iconEl.textContent = '📋'; iconEl.classList.add('validation-spinner'); }
    } else if (phase === 'planning' && header) {
        header.textContent = 'AI Planning Architecture…';
        if (iconEl) { iconEl.textContent = '🧠'; iconEl.classList.add('validation-spinner'); }
    } else if (phase === 'generating' && header) {
        header.textContent = 'Generating ARM Template…';
        if (iconEl) { iconEl.textContent = '⚡'; iconEl.classList.add('validation-spinner'); }
    } else if (phase === 'policy_generation' && header) {
        header.textContent = 'Generating Azure Policy…';
        if (iconEl) { iconEl.textContent = '🛡️'; iconEl.classList.add('validation-spinner'); }
    } else if (phase === 'static_policy_check' && header) {
        header.textContent = 'Checking Governance Policies…';
        if (iconEl) { iconEl.textContent = '📋'; iconEl.classList.add('validation-spinner'); }
    } else if (phase === 'policy_testing' && header) {
        header.textContent = 'Testing Runtime Policy Compliance…';
        if (iconEl) { iconEl.textContent = '🛡️'; iconEl.classList.add('validation-spinner'); }
    } else if ((phase === 'testing_start' || phase === 'testing_generate' || phase === 'testing_execute') && header) {
        header.textContent = 'Running Infrastructure Tests…';
        if (iconEl) { iconEl.textContent = '🧪'; iconEl.classList.add('validation-spinner'); }
    } else if (type === 'healing' && header) {
        header.innerHTML = 'Auto-Healing — AI Fixing Template… ' + _copilotTag();
        if (iconEl) { iconEl.textContent = '🤖'; iconEl.classList.add('validation-spinner'); }
    } else if (phase === 'what_if' && header) {
        header.textContent = 'Running ARM What-If Analysis…';
        if (iconEl) { iconEl.textContent = '🔍'; iconEl.classList.add('validation-spinner'); }
    } else if (phase === 'deploying' && header) {
        header.textContent = 'Deploying to Validation RG…';
        if (iconEl) { iconEl.textContent = '🚀'; iconEl.classList.add('validation-spinner'); }
    } else if (phase === 'cleanup' && header) {
        header.textContent = 'Cleaning Up…';
        if (iconEl) { iconEl.textContent = '🧹'; }
    }

    // Final states on outer card
    if (type === 'done' && card) {
        card.className = 'validation-card validation-succeeded';
        if (header) header.textContent = `Service Approved — v${event.semver || event.version + '.0.0'}`;
        if (iconEl) { iconEl.textContent = '✅'; iconEl.classList.remove('validation-spinner'); }
        if (badge && event.issues_resolved > 0) {
            badge.textContent = `Resolved ${event.issues_resolved} issue${event.issues_resolved !== 1 ? 's' : ''}`;
            badge.classList.add('badge-success');
        }
    } else if (type === 'policy_blocked' && card) {
        card.className = 'validation-card validation-policy-blocked';
        if (header) header.textContent = 'Policy Review Needed';
        if (iconEl) { iconEl.textContent = '🛑'; iconEl.classList.remove('validation-spinner'); }
    } else if (type === 'error' && card) {
        card.className = 'validation-card validation-failed';
        if (header) header.textContent = 'Onboarding Failed';
        if (iconEl) { iconEl.textContent = '⛔'; iconEl.classList.remove('validation-spinner'); }
    }
}

let reasoningVisible = true;
function toggleReasoningVisibility() {
    reasoningVisible = !reasoningVisible;
    // Update both the inline toggle and the overlay toggle
    ['toggle-reasoning-btn', 'pipeline-reasoning-btn'].forEach(id => {
        const btn = document.getElementById(id);
        if (btn) {
            btn.classList.toggle('active', reasoningVisible);
            btn.textContent = reasoningVisible ? '🧠 AI Thinking' : '🧠 Hidden';
        }
    });
    // Hide/show reasoning in both old log lines and new flow detail lines
    document.querySelectorAll('.reasoning-line').forEach(el => {
        el.style.display = reasoningVisible ? '' : 'none';
    });
    document.querySelectorAll('.uf-text-reasoning').forEach(el => {
        el.closest('.uf-detail-line').style.display = reasoningVisible ? '' : 'none';
    });
}

function closeServiceDetail() {
    _openDrawerServiceId = null;
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

    // Lifecycle progress: which stages are completed for this status
    const lifecycleStages = ['Composed', 'Tested', 'Validated', 'Published'];
    const statusProgress = {
        draft: 1,       // Composed only
        passed: 2,      // Composed + Tested
        failed: 1,      // Stuck (composed but failed)
        validated: 3,    // Composed + Tested + Validated
        approved: 4,    // All stages complete
        deprecated: 4,
    };

    grid.innerHTML = templates.map(tmpl => {
        const ttype = tmpl.template_type || 'workload';
        const icon = typeIcons[ttype] || '📋';
        const status = tmpl.status || 'draft';
        const serviceIds = tmpl.service_ids || [];
        const provides = tmpl.provides || [];
        const primaryAzIcon = provides.length ? _azureIcon(provides[0], 28) : '';
        const semver = tmpl.latest_semver || (tmpl.active_version ? `${tmpl.active_version}.0.0` : null);
        const progress = statusProgress[status] || 1;
        const isPublished = status === 'approved';
        const isFailed = status === 'failed';

        const lifecycleHtml = _wfPipeline(
            lifecycleStages.map(s => ({ label: s })),
            { progress, allDone: isPublished, failedKey: isFailed ? 'fail' : undefined, compact: true }
        );

        return `
        <div class="tmpl-card tmpl-card-${ttype} tmpl-status-${status}" onclick="showTemplateDetail('${escapeHtml(tmpl.id)}')">
            <div class="tmpl-card-header">
                <div class="tmpl-card-title">
                    <span class="tmpl-type-icon">${primaryAzIcon || icon}</span>
                    <div>
                        <strong>${escapeHtml(tmpl.name)}</strong>
                        <div class="tmpl-card-id">${escapeHtml(tmpl.id)}</div>
                    </div>
                </div>
                <div class="tmpl-card-badges">
                    ${semver ? `<span class="tmpl-semver-badge">${escapeHtml(semver)}</span>` : ''}
                    <span class="status-badge ${status}">${statusLabelsMap[status] || status}</span>
                </div>
            </div>
            <div class="tmpl-lifecycle">${lifecycleHtml}</div>
            ${tmpl.description ? `<p class="tmpl-card-desc">${escapeHtml(tmpl.description)}</p>` : ''}
            ${provides.length ? `
            <div class="tmpl-card-resources">
                ${provides.map(p => `<span class="tmpl-chip tmpl-chip-provides"><span class="az-chip-icon">${_azureIcon(p, 14)}</span>${_shortType(p)}</span>`).join('')}
            </div>` : ''}
            <div class="tmpl-card-footer">
                <div class="tmpl-card-meta">
                    <span class="tmpl-cat-badge">${escapeHtml(tmpl.category || '')}</span>
                    ${serviceIds.length ? `<span class="tmpl-svc-count">${serviceIds.length} service${serviceIds.length !== 1 ? 's' : ''}</span>` : ''}
                </div>
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

/**
 * Return an inline SVG icon for an Azure resource type.
 * Uses Azure's official color palette with distinctive shapes per service.
 * Falls back to a generic Azure diamond for unknown types.
 */
function _azureIcon(resourceType, size = 18) {
    if (!resourceType) return '';
    const key = resourceType.toLowerCase();

    const icons = {
        // ── Compute ──
        'microsoft.compute/virtualmachines': `<svg width="${size}" height="${size}" viewBox="0 0 18 18" fill="none" xmlns="http://www.w3.org/2000/svg"><rect x="2" y="3" width="14" height="10" rx="1.5" fill="#0078D4"/><rect x="4" y="5" width="10" height="6" rx="0.5" fill="#50E6FF"/><rect x="6" y="14" width="6" height="1.5" rx="0.5" fill="#0078D4"/><rect x="5" y="15" width="8" height="1" rx="0.5" fill="#005BA1"/></svg>`,
        'microsoft.compute/virtualmachinescalesets': `<svg width="${size}" height="${size}" viewBox="0 0 18 18" fill="none" xmlns="http://www.w3.org/2000/svg"><rect x="4" y="5" width="12" height="8" rx="1.5" fill="#0078D4" opacity="0.4"/><rect x="3" y="4" width="12" height="8" rx="1.5" fill="#0078D4" opacity="0.7"/><rect x="2" y="3" width="12" height="8" rx="1.5" fill="#0078D4"/><rect x="4" y="5" width="8" height="4" rx="0.5" fill="#50E6FF"/></svg>`,
        'microsoft.web/sites': `<svg width="${size}" height="${size}" viewBox="0 0 18 18" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M9 2L15.5 5.5V12.5L9 16L2.5 12.5V5.5L9 2Z" fill="#0078D4"/><path d="M9 5L12.5 7V11L9 13L5.5 11V7L9 5Z" fill="#50E6FF"/></svg>`,
        'microsoft.web/serverfarms': `<svg width="${size}" height="${size}" viewBox="0 0 18 18" fill="none" xmlns="http://www.w3.org/2000/svg"><rect x="2" y="3" width="14" height="4" rx="1" fill="#0078D4"/><rect x="2" y="8" width="14" height="4" rx="1" fill="#005BA1"/><circle cx="5" cy="5" r="1" fill="#50E6FF"/><circle cx="5" cy="10" r="1" fill="#50E6FF"/><rect x="7" y="13" width="4" height="2" rx="0.5" fill="#0078D4"/></svg>`,
        'microsoft.containerservice/managedclusters': `<svg width="${size}" height="${size}" viewBox="0 0 18 18" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M9 1L16 5V13L9 17L2 13V5L9 1Z" fill="#326CE5"/><path d="M9 5.5L12 7.5V11.5L9 13.5L6 11.5V7.5L9 5.5Z" fill="#fff"/><circle cx="9" cy="9.5" r="1.5" fill="#326CE5"/></svg>`,
        'microsoft.containerinstance/containergroups': `<svg width="${size}" height="${size}" viewBox="0 0 18 18" fill="none" xmlns="http://www.w3.org/2000/svg"><rect x="2" y="4" width="14" height="10" rx="1.5" fill="#0078D4"/><path d="M5 7h8M5 9.5h8M5 12h5" stroke="#50E6FF" stroke-width="1.2" stroke-linecap="round"/></svg>`,
        'microsoft.app/containerapps': `<svg width="${size}" height="${size}" viewBox="0 0 18 18" fill="none" xmlns="http://www.w3.org/2000/svg"><rect x="2" y="2" width="14" height="14" rx="3" fill="#0078D4"/><path d="M6 6h6v6H6z" fill="#50E6FF" rx="1"/><path d="M8 8h2v2H8z" fill="#fff"/></svg>`,

        // ── Networking ──
        'microsoft.network/virtualnetworks': `<svg width="${size}" height="${size}" viewBox="0 0 18 18" fill="none" xmlns="http://www.w3.org/2000/svg"><circle cx="4" cy="4" r="2.5" fill="#0078D4"/><circle cx="14" cy="4" r="2.5" fill="#0078D4"/><circle cx="9" cy="14" r="2.5" fill="#0078D4"/><line x1="4" y1="6" x2="9" y2="12" stroke="#50E6FF" stroke-width="1.5"/><line x1="14" y1="6" x2="9" y2="12" stroke="#50E6FF" stroke-width="1.5"/><line x1="6" y1="4" x2="12" y2="4" stroke="#50E6FF" stroke-width="1.5"/></svg>`,
        'microsoft.network/networksecuritygroups': `<svg width="${size}" height="${size}" viewBox="0 0 18 18" fill="none" xmlns="http://www.w3.org/2000/svg"><rect x="2" y="2" width="14" height="14" rx="2" fill="#0078D4"/><path d="M9 5L13 7.5V11.5L9 14L5 11.5V7.5L9 5Z" fill="#50E6FF"/><path d="M9 7.5V11M7.5 9.5H10.5" stroke="#0078D4" stroke-width="1.5" stroke-linecap="round"/></svg>`,
        'microsoft.network/applicationgateways': `<svg width="${size}" height="${size}" viewBox="0 0 18 18" fill="none" xmlns="http://www.w3.org/2000/svg"><rect x="6" y="2" width="6" height="14" rx="1.5" fill="#0078D4"/><path d="M2 6h4M2 9h4M2 12h4M12 6h4M12 9h4M12 12h4" stroke="#50E6FF" stroke-width="1.2" stroke-linecap="round"/></svg>`,
        'microsoft.network/publicipaddresses': `<svg width="${size}" height="${size}" viewBox="0 0 18 18" fill="none" xmlns="http://www.w3.org/2000/svg"><circle cx="9" cy="9" r="7" fill="#0078D4"/><ellipse cx="9" cy="9" rx="3" ry="7" fill="none" stroke="#50E6FF" stroke-width="1.2"/><line x1="2" y1="9" x2="16" y2="9" stroke="#50E6FF" stroke-width="1.2"/></svg>`,
        'microsoft.network/loadbalancers': `<svg width="${size}" height="${size}" viewBox="0 0 18 18" fill="none" xmlns="http://www.w3.org/2000/svg"><circle cx="9" cy="4" r="2.5" fill="#0078D4"/><rect x="3" y="12" width="4" height="4" rx="1" fill="#0078D4"/><rect x="11" y="12" width="4" height="4" rx="1" fill="#0078D4"/><line x1="9" y1="6.5" x2="5" y2="12" stroke="#50E6FF" stroke-width="1.3"/><line x1="9" y1="6.5" x2="13" y2="12" stroke="#50E6FF" stroke-width="1.3"/></svg>`,
        'microsoft.network/dnszones': `<svg width="${size}" height="${size}" viewBox="0 0 18 18" fill="none" xmlns="http://www.w3.org/2000/svg"><rect x="2" y="3" width="14" height="12" rx="2" fill="#0078D4"/><text x="9" y="11.5" text-anchor="middle" font-size="7" font-weight="bold" fill="#50E6FF" font-family="sans-serif">DNS</text></svg>`,
        'microsoft.network/privatednszones': `<svg width="${size}" height="${size}" viewBox="0 0 18 18" fill="none" xmlns="http://www.w3.org/2000/svg"><rect x="2" y="3" width="14" height="12" rx="2" fill="#005BA1"/><text x="9" y="11.5" text-anchor="middle" font-size="7" font-weight="bold" fill="#50E6FF" font-family="sans-serif">DNS</text><circle cx="14" cy="4" r="2.5" fill="#FFB900"/><path d="M13.2 3.2l1.6 1.6M14.8 3.2l-1.6 1.6" stroke="#fff" stroke-width="0.8"/></svg>`,
        'microsoft.network/frontdoors': `<svg width="${size}" height="${size}" viewBox="0 0 18 18" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M3 4h12l-2 10H5L3 4Z" fill="#0078D4"/><path d="M5 6h8l-1.5 6H6.5L5 6Z" fill="#50E6FF"/></svg>`,

        // ── Databases ──
        'microsoft.sql/servers': `<svg width="${size}" height="${size}" viewBox="0 0 18 18" fill="none" xmlns="http://www.w3.org/2000/svg"><ellipse cx="9" cy="4.5" rx="6" ry="2.5" fill="#0078D4"/><path d="M3 4.5v9c0 1.38 2.69 2.5 6 2.5s6-1.12 6-2.5v-9" stroke="#0078D4" stroke-width="0" fill="#005BA1"/><ellipse cx="9" cy="13.5" rx="6" ry="2.5" fill="#0078D4"/><ellipse cx="9" cy="4.5" rx="6" ry="2.5" fill="#50E6FF"/></svg>`,
        'microsoft.sql/servers/databases': `<svg width="${size}" height="${size}" viewBox="0 0 18 18" fill="none" xmlns="http://www.w3.org/2000/svg"><ellipse cx="9" cy="4.5" rx="6" ry="2.5" fill="#0078D4"/><path d="M3 4.5v9c0 1.38 2.69 2.5 6 2.5s6-1.12 6-2.5v-9" stroke="#0078D4" stroke-width="0" fill="#005BA1"/><ellipse cx="9" cy="13.5" rx="6" ry="2.5" fill="#0078D4"/><ellipse cx="9" cy="4.5" rx="6" ry="2.5" fill="#50E6FF"/></svg>`,
        'microsoft.dbforpostgresql/flexibleservers': `<svg width="${size}" height="${size}" viewBox="0 0 18 18" fill="none" xmlns="http://www.w3.org/2000/svg"><ellipse cx="9" cy="4.5" rx="6" ry="2.5" fill="#336791"/><path d="M3 4.5v9c0 1.38 2.69 2.5 6 2.5s6-1.12 6-2.5v-9" fill="#264F73"/><ellipse cx="9" cy="13.5" rx="6" ry="2.5" fill="#336791"/><ellipse cx="9" cy="4.5" rx="6" ry="2.5" fill="#50B0E0"/></svg>`,
        'microsoft.documentdb/databaseaccounts': `<svg width="${size}" height="${size}" viewBox="0 0 18 18" fill="none" xmlns="http://www.w3.org/2000/svg"><circle cx="9" cy="9" r="7" fill="#0078D4"/><path d="M5 7c0-1 1.8-2 4-2s4 1 4 2v4c0 1-1.8 2-4 2s-4-1-4-2V7z" fill="#50E6FF"/><ellipse cx="9" cy="7" rx="4" ry="2" fill="#fff" opacity="0.5"/></svg>`,
        'microsoft.cache/redis': `<svg width="${size}" height="${size}" viewBox="0 0 18 18" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M9 2L16 6V12L9 16L2 12V6L9 2Z" fill="#C6302B"/><path d="M9 5L13 7.5V11L9 13.5L5 11V7.5L9 5Z" fill="#FF6B6B"/><path d="M9 7.5L11 9L9 10.5L7 9L9 7.5Z" fill="#fff"/></svg>`,

        // ── Storage ──
        'microsoft.storage/storageaccounts': `<svg width="${size}" height="${size}" viewBox="0 0 18 18" fill="none" xmlns="http://www.w3.org/2000/svg"><rect x="2" y="3" width="14" height="4" rx="1" fill="#0078D4"/><rect x="2" y="8" width="14" height="4" rx="1" fill="#005BA1"/><rect x="2" y="13" width="14" height="3" rx="1" fill="#003D73"/><circle cx="13" cy="5" r="1" fill="#50E6FF"/><circle cx="13" cy="10" r="1" fill="#50E6FF"/><circle cx="13" cy="14.5" r="1" fill="#50E6FF"/></svg>`,

        // ── Security ──
        'microsoft.keyvault/vaults': `<svg width="${size}" height="${size}" viewBox="0 0 18 18" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M9 2L15 5.5V12.5L9 16L3 12.5V5.5L9 2Z" fill="#FFB900"/><circle cx="9" cy="8" r="2.5" fill="#fff"/><rect x="8.2" y="10" width="1.6" height="4" rx="0.5" fill="#fff"/><circle cx="9" cy="8" r="1" fill="#FFB900"/></svg>`,
        'microsoft.managedidentity/userassignedidentities': `<svg width="${size}" height="${size}" viewBox="0 0 18 18" fill="none" xmlns="http://www.w3.org/2000/svg"><circle cx="9" cy="6" r="3.5" fill="#0078D4"/><path d="M3 15c0-3.31 2.69-5 6-5s6 1.69 6 5" fill="#50E6FF"/><circle cx="9" cy="6" r="2" fill="#fff"/></svg>`,

        // ── Monitoring ──
        'microsoft.operationalinsights/workspaces': `<svg width="${size}" height="${size}" viewBox="0 0 18 18" fill="none" xmlns="http://www.w3.org/2000/svg"><rect x="2" y="2" width="14" height="14" rx="2" fill="#0078D4"/><polyline points="4,12 7,8 10,10 14,5" stroke="#50E6FF" stroke-width="1.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
        'microsoft.insights/components': `<svg width="${size}" height="${size}" viewBox="0 0 18 18" fill="none" xmlns="http://www.w3.org/2000/svg"><circle cx="9" cy="9" r="7" fill="#68217A"/><polyline points="5,11 8,7 10,9 13,5" stroke="#fff" stroke-width="1.3" fill="none" stroke-linecap="round" stroke-linejoin="round"/><circle cx="13" cy="5" r="1.2" fill="#50E6FF"/></svg>`,
        'microsoft.insights/actiongroups': `<svg width="${size}" height="${size}" viewBox="0 0 18 18" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M9 2l2 4h-4l2-4z" fill="#68217A"/><rect x="3" y="8" width="12" height="7" rx="1.5" fill="#68217A"/><path d="M3 9l6 4 6-4" stroke="#fff" stroke-width="1.2" fill="none"/></svg>`,

        // ── AI ──
        'microsoft.machinelearningservices/workspaces': `<svg width="${size}" height="${size}" viewBox="0 0 18 18" fill="none" xmlns="http://www.w3.org/2000/svg"><rect x="2" y="2" width="14" height="14" rx="3" fill="#0078D4"/><circle cx="6" cy="12" r="1.5" fill="#50E6FF"/><circle cx="9" cy="7" r="1.5" fill="#50E6FF"/><circle cx="12" cy="11" r="1.5" fill="#50E6FF"/><line x1="6" y1="12" x2="9" y2="7" stroke="#fff" stroke-width="0.8"/><line x1="9" y1="7" x2="12" y2="11" stroke="#fff" stroke-width="0.8"/><line x1="6" y1="12" x2="12" y2="11" stroke="#fff" stroke-width="0.8"/></svg>`,
        'microsoft.cognitiveservices/accounts': `<svg width="${size}" height="${size}" viewBox="0 0 18 18" fill="none" xmlns="http://www.w3.org/2000/svg"><circle cx="9" cy="9" r="7" fill="#0078D4"/><path d="M6 8c0-1.66 1.34-3 3-3s3 1.34 3 3c0 1.3-.8 2.4-2 2.8V13H8v-2.2C6.8 10.4 6 9.3 6 8z" fill="#50E6FF"/><circle cx="8" cy="7.5" r="0.7" fill="#0078D4"/><circle cx="10" cy="7.5" r="0.7" fill="#0078D4"/></svg>`,
    };

    // Try exact match
    if (icons[key]) return icons[key];

    // Try partial match (e.g. "microsoft.network/virtualnetworks" inside a longer path)
    for (const [k, v] of Object.entries(icons)) {
        if (key.includes(k) || k.includes(key)) return v;
    }

    // Fallback: category-based generic icon
    if (key.includes('microsoft.compute')) return icons['microsoft.compute/virtualmachines'];
    if (key.includes('microsoft.network')) return icons['microsoft.network/virtualnetworks'];
    if (key.includes('microsoft.sql') || key.includes('microsoft.db') || key.includes('microsoft.documentdb'))
        return icons['microsoft.sql/servers'];
    if (key.includes('microsoft.storage')) return icons['microsoft.storage/storageaccounts'];
    if (key.includes('microsoft.keyvault') || key.includes('microsoft.managedidentity'))
        return icons['microsoft.keyvault/vaults'];
    if (key.includes('microsoft.web') || key.includes('microsoft.app'))
        return icons['microsoft.web/sites'];
    if (key.includes('microsoft.insights') || key.includes('microsoft.operationalinsights'))
        return icons['microsoft.operationalinsights/workspaces'];

    // Generic Azure diamond
    return `<svg width="${size}" height="${size}" viewBox="0 0 18 18" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M9 1L17 9L9 17L1 9L9 1Z" fill="#0078D4"/><path d="M9 5L13 9L9 13L5 9L9 5Z" fill="#50E6FF"/></svg>`;
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
    const activeVer = tmpl.latest_semver || (tmpl.active_version ? `${tmpl.active_version}.0.0` : null);

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
                📝 <strong>New Template</strong> — I haven't tested this yet. Let me run validation to check if everything's set up correctly.
            </div>
            <button class="btn btn-primary btn-sm" onclick="runFullValidation('${escapeHtml(tmpl.id)}')">
                🧪 Validate
            </button>
        </div>`;
    } else if (status === 'passed') {
        ctaHtml = `
        <div class="detail-section tmpl-test-cta">
            <div class="tmpl-test-banner tmpl-test-validate">
                ✅ The structure checks out. Let me now test it against Azure to confirm it'll actually deploy.
            </div>
            <button class="btn btn-primary btn-sm" onclick="runFullValidation('${escapeHtml(tmpl.id)}', true)">
                🧪 Validate Against Azure
            </button>
        </div>`;
    } else if (status === 'validated') {
        ctaHtml = `
        <div class="detail-section tmpl-test-cta">
            <div class="tmpl-test-banner tmpl-test-ready">
                ✅ <strong>Verified</strong> — I've tested this template against Azure and it's good to go. Ready to publish!
            </div>
            <button class="btn btn-primary btn-sm" onclick="publishTemplate('${escapeHtml(tmpl.id)}')">
                🚀 Publish to Catalog
            </button>
        </div>`;
    } else if (status === 'failed') {
        const isBlueprint = tmpl.is_blueprint || (tmpl.service_ids && tmpl.service_ids.length > 1);
        ctaHtml = `
        <div class="detail-section tmpl-test-cta">
            <div class="tmpl-test-banner tmpl-test-failed">
                ❌ I found some issues during validation. I'll ${isBlueprint ? 'rebuild this from the latest service templates and ' : ''}fix any structural issues, then re-deploy to Azure to verify.
            </div>
            <div style="display:flex; gap:0.5rem; flex-wrap:wrap; align-items:center;">
                <button class="btn btn-primary btn-sm" onclick="fixAndValidateTemplate('${escapeHtml(tmpl.id)}')">
                    🔧 Fix &amp; Validate
                </button>
                ${_copilotBadge()}
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
            ${activeVer ? `<span class="tmpl-ver-badge">${activeVer}</span>` : ''}
            <span class="tmpl-standalone-badge ${isStandalone ? 'standalone-yes' : 'standalone-no'}">
                ${isStandalone ? '✅ Standalone' : '🔗 Has dependencies'}
            </span>
        </div>

        ${ctaHtml}

        <!-- Validation form (hidden by default) -->
        <div id="tmpl-validate-form" class="detail-section tmpl-validate-section" style="display:none;">
            <h4>🧪 Validation ${_copilotBadge()}</h4>
            <p class="tmpl-validate-desc">I'll deploy this to a temporary Azure resource group to test it. If anything breaks, I'll use the Copilot SDK to analyze the errors and fix the template automatically. The temp resources are cleaned up afterward.</p>
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
            <h4>🚀 Deploy to Azure ${_copilotBadge()}</h4>
            <p class="tmpl-deploy-desc">Configure the deployment target and parameter values. Self-healing errors are resolved via the Copilot SDK.</p>
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

        <!-- ══ Composed From — front and center ══ -->
        <div class="detail-section comp-hero-section">
            <div class="comp-hero-header">
                <h3>🧩 Composed From</h3>
                <div class="comp-hero-meta">
                    <span class="comp-ver-label">v</span><span class="comp-ver-num">${activeVer || 'Draft'}</span>
                    <span class="comp-ver-status comp-ver-status-${status}">${statusBadgeMap[status] || status}</span>
                    <span class="category-badge">${escapeHtml(tmpl.format || 'arm')}</span>
                    <span class="category-badge">${escapeHtml(tmpl.category || '')}</span>
                    ${(tmpl.tags && tmpl.tags.length) ? tmpl.tags.map(t => `<span class="region-tag">${escapeHtml(t)}</span>`).join('') : ''}
                </div>
            </div>
            <div id="tmpl-composition" class="comp-hero-graph">
                <div class="compose-loading">Loading composition…</div>
            </div>
        </div>

        <!-- ══ Request Changes chat ══ -->
        <div class="detail-section tmpl-revision-section">
            <h4>📝 Request Changes ${_copilotBadge()}</h4>
            <p class="tmpl-revision-desc">Describe what you want changed and the Copilot SDK will analyze, policy-check, and update the template automatically. Creates a new version.</p>
            <div class="tmpl-revision-input-group">
                <textarea id="tmpl-revision-prompt" class="form-control tmpl-revision-textarea"
                    rows="3"
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

        <!-- Compliance scan results (populated on demand) -->
        <div id="tmpl-scan-results" style="display:none;"></div>

        <!-- Pipeline Runs — visual history with flowchart replay -->
        <div id="tmpl-pipeline-runs-container" class="detail-section" style="display:none;"></div>

        <!-- Pipeline Run Replay — full-screen flowchart for a past run -->
        <div id="tmpl-pipeline-replay" style="display:none;"></div>

        <!-- Version Log — collapsible -->
        <div class="detail-section comp-verlog-section">
            <h4 class="comp-verlog-toggle" onclick="this.parentElement.classList.toggle('comp-verlog-open')">
                📋 Version Log <span class="comp-verlog-arrow">▸</span>
            </h4>
            <div id="tmpl-version-history" class="tmpl-version-history comp-verlog-body">
                <div class="compose-loading">Loading…</div>
            </div>
        </div>
    `;

    document.getElementById('template-detail-drawer').classList.remove('hidden');

    // Load composition info (also updates template version display with semver)
    _loadTemplateComposition(templateId);
    _loadTemplateVersionHistory(templateId);
    _loadTemplatePipelineRuns(templateId);

    // Reconnect to active/completed validation if one exists
    _reconnectTemplateValidation(templateId);
}

/** Replay cached validation events when re-opening a template detail panel */
function _reconnectTemplateValidation(templateId) {
    const tracker = _activeTemplateValidations[templateId];
    if (!tracker || !tracker.events.length) return;

    const resultsDiv = document.getElementById('tmpl-validate-results');
    const btn = document.getElementById('tmpl-validate-btn');
    if (!resultsDiv) return;

    // Show the results area and replay all cached events
    resultsDiv.style.display = 'block';
    resultsDiv.innerHTML = '';
    for (const event of tracker.events) {
        _renderDeployProgress(resultsDiv, event, 'validate');
    }

    if (tracker.running) {
        // Still running — update button to show in-progress
        if (btn) {
            btn.disabled = true;
            btn.innerHTML = '⏳ Validating…';
        }
    } else {
        // Finished — show final state
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = '🧪 Run Validation';
        }
    }
}

// ── Template Pipeline Run History ──────────────────────────────

/** Load and render pipeline run history for a template */
async function _loadTemplatePipelineRuns(templateId) {
    const container = document.getElementById('tmpl-pipeline-runs-container');
    if (!container) return;
    try {
        const res = await fetch(`/api/catalog/templates/${encodeURIComponent(templateId)}/pipeline-runs`);
        if (!res.ok) return;
        const runs = await res.json();
        if (!runs || runs.length === 0) return;
        // Update the shared cache so expandPipelineRun uses fresh data
        _templatePipelineRunCache[templateId] = runs;
        container.innerHTML = _renderTemplatePipelineRuns(runs, templateId);
        container.style.display = '';
    } catch (_) { /* ignore */ }
}

/** Render visual pipeline run history list for templates */
function _renderTemplatePipelineRuns(runs, templateId) {
    const statusIcon = (s) => ({
        completed: '✅', failed: '❌', running: '🔄',
    })[s] || '⏳';

    const statusClass = (s) => ({
        completed: 'run-status-completed',
        failed: 'run-status-failed',
        running: 'run-status-running',
    })[s] || 'run-status-unknown';

    const formatDuration = (secs) => {
        if (!secs && secs !== 0) return '—';
        if (secs < 60) return `${Math.round(secs)}s`;
        const m = Math.floor(secs / 60);
        const s = Math.round(secs % 60);
        return s > 0 ? `${m}m ${s}s` : `${m}m`;
    };

    // Check if there's an active live validation running for this template
    const liveTracker = _activeTemplateValidations[templateId];

    const items = runs.map((r, idx) => {
        const started = (r.started_at || '').replace('T', ' ').substring(0, 19);
        const dur = formatDuration(r.duration_secs);
        const healCount = r.heal_count || 0;
        const hasEvents = r.events && r.events.length > 0;
        const summary = r.summary || {};
        const verDisplay = r.semver || (r.version_num ? `v${r.version_num}` : '');
        const isRunning = r.status === 'running' || (!r.status && !r.completed_at);

        let detailRows = '';
        if (r.error_detail) {
            detailRows += `<div class="run-detail-row run-detail-error"><strong>Error:</strong> ${escapeHtml(r.error_detail.substring(0, 300))}</div>`;
        }
        if (healCount > 0) {
            detailRows += `<div class="run-detail-row"><strong>Heal cycles:</strong> ${healCount}</div>`;
        }
        if (verDisplay) {
            detailRows += `<div class="run-detail-row"><strong>Version:</strong> ${escapeHtml(verDisplay)}</div>`;
        }
        if (summary.region) {
            detailRows += `<div class="run-detail-row"><strong>Region:</strong> ${escapeHtml(summary.region)}</div>`;
        }
        if (r.run_id) {
            detailRows += `<div class="run-detail-row run-detail-runid"><strong>Run ID:</strong> <code>${escapeHtml(r.run_id)}</code></div>`;
        }

        // Action button: "Watch Live" for running, "View Flowchart" for completed with events
        let actionBtn = '';
        if (isRunning && liveTracker && liveTracker.running) {
            actionBtn = `<button class="btn btn-xs btn-replay btn-live-pulse" onclick="event.stopPropagation(); scrollToLiveProgress()" title="Watch this run in real time">👁 Watch Live</button>`;
        } else if (hasEvents) {
            actionBtn = `<button class="btn btn-xs btn-replay" onclick="event.stopPropagation(); expandPipelineRun(this, '${escapeHtml(templateId)}', ${idx})" title="View the deployment flowchart for this run">📊 View</button>`;
        }

        return `
        <div class="run-item run-item-${r.status || 'unknown'}">
            <div class="run-item-header" onclick="toggleRunDetail(this)">
                <span class="run-item-status ${statusClass(r.status)}">${statusIcon(r.status)}</span>
                <span class="run-item-pipeline">Template Validation</span>
                ${healCount > 0 ? `<span class="run-item-heals" title="${healCount} heal cycle(s)">🔧 ${healCount}</span>` : ''}
                <span class="run-item-duration">${dur}</span>
                <span class="run-item-date">${started}</span>
                ${actionBtn}
            </div>
            ${detailRows ? `<div class="run-item-detail hidden">${detailRows}</div>` : ''}
            <div class="run-item-flowchart" style="display:none;"></div>
        </div>`;
    }).join('');

    return `
    <div class="version-history">
        <div class="version-history-header">
            <span>📊 Pipeline Runs</span>
            <span class="version-count">${runs.length} run${runs.length === 1 ? '' : 's'}</span>
        </div>
        <div class="version-list">${items}</div>
    </div>`;
}

/** Toggle run detail rows visibility */
function toggleRunDetail(headerEl) {
    const detail = headerEl.parentElement.querySelector('.run-item-detail');
    if (detail) detail.classList.toggle('hidden');
}

/** Scroll to the live fix-and-validate progress container */
function scrollToLiveProgress() {
    const liveDiv = document.getElementById('fix-validate-progress');
    if (liveDiv) {
        liveDiv.scrollIntoView({ behavior: 'smooth', block: 'start' });
    } else {
        showToast('No active validation running right now', 'info');
    }
}

/** Expand a pipeline run inline to show its deployment flowchart */
async function expandPipelineRun(btnEl, templateId, runIndex) {
    const runItem = btnEl.closest('.run-item');
    if (!runItem) return;
    const flowchartDiv = runItem.querySelector('.run-item-flowchart');
    if (!flowchartDiv) return;

    // Toggle: if already showing, collapse it
    if (flowchartDiv.style.display !== 'none') {
        flowchartDiv.style.display = 'none';
        flowchartDiv.innerHTML = '';
        flowchartDiv._vfState = null;
        btnEl.textContent = '📊 View';
        return;
    }

    // Load runs from cache or fetch
    if (!_templatePipelineRunCache[templateId]) {
        try {
            const res = await fetch(`/api/catalog/templates/${encodeURIComponent(templateId)}/pipeline-runs`);
            if (!res.ok) return;
            _templatePipelineRunCache[templateId] = await res.json();
        } catch (_) { return; }
    }

    const runs = _templatePipelineRunCache[templateId];
    if (!runs || !runs[runIndex]) return;
    const run = runs[runIndex];
    const events = run.events || [];
    if (!events.length) {
        showToast('No event data saved for this run', 'info');
        return;
    }

    // Show and populate the flowchart
    flowchartDiv.style.display = 'block';
    flowchartDiv.innerHTML = '';
    flowchartDiv._vfState = null;
    btnEl.textContent = '📊 Hide';

    for (const event of events) {
        _renderDeployProgress(flowchartDiv, event, 'validate');
    }

    flowchartDiv.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

/** Replay a stored pipeline run by feeding its events into the visual flowchart */
let _templatePipelineRunCache = {};

async function replayPipelineRun(templateId, runIndex) {
    const replayDiv = document.getElementById('tmpl-pipeline-replay');
    if (!replayDiv) return;

    // Load the runs if not cached
    if (!_templatePipelineRunCache[templateId]) {
        try {
            const res = await fetch(`/api/catalog/templates/${encodeURIComponent(templateId)}/pipeline-runs`);
            if (!res.ok) return;
            _templatePipelineRunCache[templateId] = await res.json();
        } catch (_) { return; }
    }

    const runs = _templatePipelineRunCache[templateId];
    if (!runs || !runs[runIndex]) return;
    const run = runs[runIndex];
    const events = run.events || [];
    if (!events.length) {
        showToast('No event data available for this run', 'info');
        return;
    }

    // Show replay area and clear previous state
    replayDiv.style.display = 'block';
    replayDiv.innerHTML = '';
    replayDiv._vfState = null; // Reset flowchart state

    // Add a header with run info and close button
    const started = (run.started_at || '').replace('T', ' ').substring(0, 19);
    const verDisplay = run.semver || (run.version_num ? `v${run.version_num}` : '');
    const headerDiv = document.createElement('div');
    headerDiv.className = 'replay-header';
    headerDiv.innerHTML = `
        <div class="replay-header-info">
            <span class="replay-title">📊 Pipeline Run Replay</span>
            <span class="replay-meta">${started}${verDisplay ? ` · ${escapeHtml(verDisplay)}` : ''} · Run: ${escapeHtml(run.run_id || '?')}</span>
        </div>
        <button class="btn btn-xs replay-close-btn" onclick="closeReplay()">✕ Close</button>
    `;
    replayDiv.appendChild(headerDiv);

    // Create the replay canvas (same container used by _renderDeployProgress)
    const canvas = document.createElement('div');
    canvas.id = 'tmpl-replay-canvas';
    replayDiv.appendChild(canvas);

    // Feed all events instantly to build the static flowchart
    for (const event of events) {
        _renderDeployProgress(canvas, event, 'validate');
    }

    // Scroll the replay into view
    replayDiv.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

/** Close the pipeline replay panel */
function closeReplay() {
    const replayDiv = document.getElementById('tmpl-pipeline-replay');
    if (replayDiv) {
        replayDiv.style.display = 'none';
        replayDiv.innerHTML = '';
        replayDiv._vfState = null;
    }
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

/** Load composition info — which services compose this template, their versions, upgrade availability.
 *  Renders a visual dependency graph with nodes, edges, pinned versions, and per-dep upgrade buttons. */
async function _loadTemplateComposition(templateId) {
    const container = document.getElementById('tmpl-composition');
    if (!container) return;

    try {
        const res = await fetch(`/api/catalog/templates/${encodeURIComponent(templateId)}/composition`);
        if (!res.ok) {
            container.innerHTML = '<div class="compose-empty">No composition data</div>';
            return;
        }
        const data = await res.json();
        const components = data.components || [];
        const edges = data.edges || [];
        const requires = data.requires || [];

        if (!components.length) {
            container.innerHTML = '<div class="compose-empty">No services linked</div>';
            return;
        }

        // ── Build layered graph ──────────────────────────────
        // Determine depth of each node via topological sort on edges
        const depthMap = {};
        const childrenOf = {};  // to → [from]  (nodes that depend ON a node)
        const parentsOf = {};   // from → [to]   (nodes a node depends on)
        for (const c of components) depthMap[c.service_id] = 0;
        for (const e of edges) {
            if (!childrenOf[e.to]) childrenOf[e.to] = [];
            childrenOf[e.to].push(e.from);
            if (!parentsOf[e.from]) parentsOf[e.from] = [];
            parentsOf[e.from].push(e.to);
        }
        // BFS from roots (nodes with no parents)
        const roots = components.filter(c => !(parentsOf[c.service_id]?.length));
        const visited = new Set();
        const queue = roots.map(c => ({ id: c.service_id, depth: 0 }));
        while (queue.length) {
            const { id, depth } = queue.shift();
            if (visited.has(id)) continue;
            visited.add(id);
            depthMap[id] = Math.max(depthMap[id] || 0, depth);
            for (const child of (childrenOf[id] || [])) {
                // Children are at LOWER depth (foundations = high depth = bottom)
                // Actually invert: dependents are above, dependencies below
            }
            for (const parent of (parentsOf[id] || [])) {
                depthMap[parent] = Math.max(depthMap[parent] || 0, depth + 1);
                queue.push({ id: parent, depth: depth + 1 });
            }
        }
        // Sort components bottom-up: depth 0 at the bottom (foundations), higher depth on top
        const maxDepth = Math.max(...Object.values(depthMap), 0);
        const layers = [];
        for (let d = maxDepth; d >= 0; d--) {
            const layerNodes = components.filter(c => (depthMap[c.service_id] || 0) === d);
            if (layerNodes.length) layers.push(layerNodes);
        }

        // ── Render hero graph ────────────────────────────────
        const anyUpgrade = components.some(c => c.upgrade_available);
        const providesList = data.provides || [];

        let html = '<div class="comp-hero-graph-inner">';

        // Layers (top = dependents, bottom = foundations)
        for (let li = 0; li < layers.length; li++) {
            const layer = layers[li];
            html += '<div class="comp-hero-layer">';
            for (const c of layer) {
                const shortName = c.name || c.service_id.split('/').pop();
                const verDisplay = c.version_known === false ? '?' : (c.current_semver || '—');
                const statusCls = c.status === 'approved' ? 'hero-node-ok' : 'hero-node-warn';
                const upgradeCls = c.upgrade_available ? 'hero-node-upgradable' : '';
                const catLabel = c.category ? c.category.charAt(0).toUpperCase() + c.category.slice(1) : '';

                // Find edges FROM this node (what it depends on)
                const myEdges = edges.filter(e => e.from === c.service_id);
                const depNames = myEdges.map(e => {
                    const dep = components.find(x => x.service_id === e.to);
                    return dep ? (dep.name || e.to.split('/').pop()) : e.to.split('/').pop();
                });

                // Build dependency icon strip (small icons of what this node depends on)
                let depIconsHtml = '';
                if (myEdges.length) {
                    const depIcons = myEdges.map(e => {
                        const dep = components.find(x => x.service_id === e.to);
                        const depName = dep ? (dep.name || e.to.split('/').pop()) : e.to.split('/').pop();
                        return `<span class="hero-dep-icon" title="Depends on: ${escapeHtml(depName)}">${_azureIcon(e.to, 14)}</span>`;
                    }).join('');
                    depIconsHtml = `<div class="hero-node-deps">${depIcons}</div>`;
                }

                // Build tooltip text
                const tooltipLines = [
                    c.service_id,
                    catLabel ? `Category: ${catLabel}` : '',
                    `Version: ${verDisplay}`,
                    c.upgrade_available ? `Latest: ${c.latest_semver}` : 'Up to date',
                    depNames.length ? `Depends on: ${depNames.join(', ')}` : '',
                ].filter(Boolean).join('\n');

                html += `
                    <div class="hero-node ${statusCls} ${upgradeCls}" data-sid="${escapeHtml(c.service_id)}" title="${escapeHtml(tooltipLines)}">
                        <div class="hero-node-icon">${_azureIcon(c.service_id, 28)}</div>
                        <div class="hero-node-body">
                            <div class="hero-node-name">${escapeHtml(shortName)}</div>
                            ${catLabel ? `<div class="hero-node-cat">${escapeHtml(catLabel)}</div>` : ''}
                            <div class="hero-node-ver-row">
                                <span class="hero-node-ver hero-node-ver-clickable" onclick="event.stopPropagation(); showVersionPicker('${escapeHtml(templateId)}','${escapeHtml(c.service_id)}', this)" title="Click to change pinned version">v${verDisplay}</span>
                                ${c.version_known === false
                                    ? `<span class="hero-node-unknown" title="Version not tracked — recompose to lock versions">⚠ untracked</span>`
                                    : c.upgrade_available
                                        ? `<button class="hero-upgrade-btn" onclick="event.stopPropagation(); upgradeTemplateDep('${escapeHtml(templateId)}','${escapeHtml(c.service_id)}','${escapeHtml(c.latest_semver)}')" title="Upgrade to ${c.latest_semver}">⬆ ${c.latest_semver}</button><button class="hero-analyze-btn" onclick="event.stopPropagation(); analyzeUpgradeForDep('${escapeHtml(c.service_id)}','${escapeHtml(c.latest_api_version || c.latest_semver)}','${escapeHtml(c.template_api_version || c.current_semver || '')}','${escapeHtml(templateId)}')" title="Analyze API version upgrade compatibility">🔬</button>`
                                        : '<span class="hero-node-latest">✓ latest</span>'}
                            </div>
                            ${depIconsHtml}
                        </div>
                    </div>`;
            }
            html += '</div>';

            // Arrow connector between layers
            if (li < layers.length - 1) {
                html += '<div class="comp-hero-connector"><span class="comp-hero-arrow">↓</span></div>';
            }
        }

        // External dependencies (requires not satisfied within the template)
        if (requires.length) {
            html += `
                <div class="comp-hero-connector"><span class="comp-hero-arrow comp-hero-arrow-ext">↓ external dep</span></div>
                <div class="comp-hero-layer comp-hero-layer-ext">
                    ${requires.map(r => {
                        const rType = r.type || r;
                        return `
                        <div class="hero-node hero-node-ext" data-sid="${escapeHtml(rType)}" title="${escapeHtml(rType)}\nResolved at deploy time">
                            <div class="hero-node-icon">${_azureIcon(rType, 28)}</div>
                            <div class="hero-node-body">
                                <div class="hero-node-name">${_shortType(rType)}</div>
                                <div class="hero-node-cat" style="font-style:italic">external dep</div>
                            </div>
                        </div>`;
                    }).join('')}
                </div>`;
        }

        html += '</div>';

        // Recompose all / check for updates row
        html += '<div class="comp-hero-actions">';
        if (anyUpgrade) {
            html += `<button class="btn btn-sm btn-primary" onclick="recomposeBlueprint('${escapeHtml(templateId)}')">🔄 Upgrade All Dependencies</button>`;
        }
        html += `<button class="btn btn-sm tmpl-check-updates-btn" id="tmpl-check-updates-btn" onclick="checkForUpdates('${escapeHtml(templateId)}')">🔍 Check for Updates</button>`;
        html += '</div>';
        html += '<div id="tmpl-updates-results"></div>';

        container.innerHTML = html;

        // Update the template version display with semver from the API
        const semver = data.template_semver;
        if (semver) {
            const verNumEl = document.querySelector('.comp-ver-num');
            if (verNumEl) verNumEl.textContent = semver;
            const headerBadge = document.querySelector('.tmpl-ver-badge');
            if (headerBadge) headerBadge.textContent = semver;
        }
    } catch (err) {
        container.innerHTML = `<div class="compose-empty">Failed: ${err.message}</div>`;
    }
}

/** Analyze upgrade compatibility for a dependency in the template composition view.
 *  Opens a modal overlay with the streaming analysis from the Upgrade Analyst agent. */
async function analyzeUpgradeForDep(serviceId, targetVersion, currentVersion, templateId) {
    const shortName = serviceId.split('/').pop();

    // Create modal overlay
    let overlay = document.getElementById('upgrade-analysis-overlay');
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'upgrade-analysis-overlay';
        overlay.className = 'upgrade-analysis-overlay';
        document.body.appendChild(overlay);
    }

    overlay.innerHTML = `
        <div class="upgrade-analysis-modal">
            <div class="upgrade-analysis-modal-header">
                <span>🔬 API Version Upgrade Analysis: ${escapeHtml(shortName)}</span>
                <button class="upgrade-analysis-close" onclick="document.getElementById('upgrade-analysis-overlay').remove()">✕</button>
            </div>
            <div class="upgrade-analysis-modal-body" id="upgrade-analysis-modal-body">
                <div class="upgrade-analysis-panel upgrade-analysis-loading">
                    <div class="upgrade-analysis-header">
                        <span class="upgrade-analysis-icon spin">🔬</span>
                        <span class="upgrade-analysis-title">Analyzing API Version Compatibility…</span>
                    </div>
                    <div class="upgrade-analysis-meta">
                        <span>API Version: ${escapeHtml(currentVersion || '?')} → ${escapeHtml(targetVersion)}</span>
                    </div>
                    <div class="upgrade-analysis-progress">
                        <div class="upgrade-analysis-progress-track">
                            <div class="upgrade-analysis-progress-fill" id="upgrade-analysis-progress-fill" style="width: 0%"></div>
                        </div>
                    </div>
                    <div class="upgrade-analysis-status" id="upgrade-analysis-status">Initializing…</div>
                </div>
            </div>
        </div>`;
    overlay.style.display = 'flex';

    // Close on backdrop click
    overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };

    try {
        const res = await fetch(`/api/services/${encodeURIComponent(serviceId)}/analyze-upgrade`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ target_version: targetVersion }),
        });

        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Analysis failed');
        }

        const modalBody = document.getElementById('upgrade-analysis-modal-body');
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let analysisResult = null;

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
                    _handleUpgradeAnalysisEvent(ev, modalBody);
                    if (ev.type === 'analysis_complete') analysisResult = ev;
                } catch (e) { /* skip */ }
            }
        }

        if (buffer.trim()) {
            try {
                const ev = JSON.parse(buffer);
                _handleUpgradeAnalysisEvent(ev, modalBody);
                if (ev.type === 'analysis_complete') analysisResult = ev;
            } catch (e) { /* skip */ }
        }

        if (analysisResult && modalBody) {
            _renderUpgradeAnalysisResult(analysisResult, modalBody, serviceId, templateId);
        }
    } catch (err) {
        const modalBody = document.getElementById('upgrade-analysis-modal-body');
        if (modalBody) {
            modalBody.innerHTML = `
                <div class="upgrade-analysis-panel upgrade-analysis-error">
                    <div class="upgrade-analysis-header">
                        <span class="upgrade-analysis-icon">❌</span>
                        <span class="upgrade-analysis-title">Analysis Failed</span>
                    </div>
                    <div class="upgrade-analysis-body">${escapeHtml(err.message)}</div>
                </div>`;
        }
    }
}

/** Upgrade a single dependency in a composed template.
 *  Triggers the full validation pipeline: recompose → AI heal → What-If → deploy-ready. */
async function upgradeTemplateDep(templateId, serviceId, targetVersion) {
    const shortName = serviceId.split('/').pop();

    // Disable upgrade buttons while pipeline runs
    const btns = document.querySelectorAll(`.dep-upgrade-btn`);
    btns.forEach(b => { b.disabled = true; });

    showToast(`⬆ Upgrading ${shortName} → ${targetVersion} — running full validation pipeline…`, 'info');

    // Delegate to the full fix-and-validate pipeline which handles:
    // recompose from source services → structural tests → ARM What-If
    // validation with self-healing loop → infrastructure testing → cleanup
    await fixAndValidateTemplate(templateId);
}

/** Show a dropdown to pick which version to pin a service to in a template */
async function showVersionPicker(templateId, serviceId, anchorEl) {
    // Close any existing picker
    const existing = document.querySelector('.version-picker-dropdown');
    if (existing) existing.remove();

    const shortName = serviceId.split('/').pop();

    // Create dropdown
    const dropdown = document.createElement('div');
    dropdown.className = 'version-picker-dropdown';
    dropdown.innerHTML = '<div class="version-picker-loading">Loading versions…</div>';

    // Position near the anchor element
    const rect = anchorEl.getBoundingClientRect();
    dropdown.style.position = 'fixed';
    dropdown.style.left = `${rect.left}px`;
    dropdown.style.top = `${rect.bottom + 4}px`;
    dropdown.style.zIndex = '9999';
    document.body.appendChild(dropdown);

    // Close on outside click
    const closeHandler = (e) => {
        if (!dropdown.contains(e.target) && e.target !== anchorEl) {
            dropdown.remove();
            document.removeEventListener('click', closeHandler, true);
        }
    };
    setTimeout(() => document.addEventListener('click', closeHandler, true), 50);

    try {
        const res = await fetch(`/api/catalog/templates/${encodeURIComponent(templateId)}/service-versions/${encodeURIComponent(serviceId)}`);
        if (!res.ok) throw new Error('Failed to load versions');
        const data = await res.json();
        const versions = data.versions || [];

        if (!versions.length) {
            dropdown.innerHTML = '<div class="version-picker-empty">No versions available</div>';
            return;
        }

        let html = `<div class="version-picker-header">Pin ${escapeHtml(shortName)} to:</div>`;
        html += '<div class="version-picker-list">';
        for (const v of versions) {
            const semver = v.semver || `${v.version}.0.0`;
            const pinnedCls = v.is_pinned ? 'version-picker-item-pinned' : '';
            const statusBadge = v.status === 'active' ? '<span class="version-picker-active">active</span>' : '';
            html += `
                <button class="version-picker-item ${pinnedCls}"
                        onclick="pinServiceVersion('${escapeHtml(templateId)}','${escapeHtml(serviceId)}',${v.version})"
                        ${v.is_pinned ? 'disabled' : ''}>
                    <span class="version-picker-ver">v${escapeHtml(semver)}</span>
                    ${statusBadge}
                    ${v.is_pinned ? '<span class="version-picker-current">📌 current</span>' : ''}
                </button>`;
        }
        html += '</div>';
        dropdown.innerHTML = html;
    } catch (err) {
        dropdown.innerHTML = `<div class="version-picker-empty">Error: ${err.message}</div>`;
    }
}

/** Pin a service to a specific version in a template */
async function pinServiceVersion(templateId, serviceId, version) {
    // Close the dropdown
    const dropdown = document.querySelector('.version-picker-dropdown');
    if (dropdown) dropdown.remove();

    const shortName = serviceId.split('/').pop();
    showToast(`📌 Pinning ${shortName} to v${version} and recomposing…`, 'info');

    try {
        const res = await fetch(`/api/catalog/templates/${encodeURIComponent(templateId)}/pin-version`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ service_id: serviceId, version }),
        });
        const data = await res.json();

        if (!res.ok) {
            showToast(`Pin failed: ${data.detail || 'Unknown error'}`, 'error');
            return;
        }

        // Show test results from auto-test that ran during recompose
        const tr = data.test_results;
        if (tr && tr.all_passed) {
            showToast(`✅ ${shortName} pinned to v${data.pinned_semver || version} — recomposed & all ${tr.total} structural tests passed`, 'success', 5000);
        } else if (tr) {
            showToast(`📌 ${shortName} pinned to v${data.pinned_semver || version} — recomposed but ${tr.failed}/${tr.total} tests need attention`, 'info', 6000);
        } else {
            showToast(`✅ ${shortName} pinned to v${data.pinned_semver || version} — template recomposed`, 'success', 5000);
        }

        // Refresh the full detail view (versions list changed too)
        await loadAllData();
        showTemplateDetail(templateId);
    } catch (err) {
        showToast(`Pin failed: ${err.message}`, 'error');
    }
}

/** Check for dependency updates — renders a full chain report */
async function checkForUpdates(templateId) {
    const btn = document.getElementById('tmpl-check-updates-btn');
    const resultsDiv = document.getElementById('tmpl-updates-results');
    if (!btn || !resultsDiv) return;

    btn.disabled = true;
    btn.textContent = '⏳ Checking…';
    resultsDiv.innerHTML = '<div class="compose-loading">Analyzing dependency chain…</div>';

    try {
        const res = await fetch(`/api/catalog/templates/${encodeURIComponent(templateId)}/composition`);
        if (!res.ok) throw new Error('Failed to fetch composition data');
        const data = await res.json();

        const components = data.components || [];
        const edges = data.edges || [];
        const requires = data.requires || [];

        if (!components.length) {
            resultsDiv.innerHTML = '<div class="compose-empty">No service dependencies found.</div>';
            return;
        }

        // Build dependency map for chain visualization
        const depsOf = {};  // service_id → [depends-on service_ids]
        for (const e of edges) {
            if (!depsOf[e.from]) depsOf[e.from] = [];
            depsOf[e.from].push({ to: e.to, reason: e.reason, required: e.required });
        }

        const updatable = components.filter(c => c.upgrade_available && c.version_known !== false);
        const untracked = components.filter(c => c.version_known === false);
        const upToDate = components.filter(c => !c.upgrade_available && c.version_known !== false);

        // Summary banner
        let html = '';
        if (untracked.length) {
            html += `
                <div class="upd-summary upd-summary-has-updates">
                    <span class="upd-summary-icon">⚠️</span>
                    <span><strong>${untracked.length}</strong> of ${components.length} dependencies have untracked versions — recompose to lock them</span>
                </div>`;
        } else if (updatable.length) {
            html += `
                <div class="upd-summary upd-summary-has-updates">
                    <span class="upd-summary-icon">⚠️</span>
                    <span><strong>${updatable.length}</strong> of ${components.length} dependencies have updates available</span>
                </div>`;
        } else {
            html += `
                <div class="upd-summary upd-summary-current">
                    <span class="upd-summary-icon">✅</span>
                    <span>All ${components.length} dependencies are up to date</span>
                </div>`;
        }

        // Dependency chain details
        html += '<div class="upd-chain">';

        // Sort: updatable first, then up-to-date
        const sorted = [...updatable, ...upToDate];
        for (const c of sorted) {
            const shortName = c.name || c.service_id.split('/').pop();
            const deps = depsOf[c.service_id] || [];
            const statusCls = c.upgrade_available ? 'upd-item-outdated' : 'upd-item-current';

            html += `
                <div class="upd-chain-item ${statusCls}">
                    <div class="upd-chain-row">
                        <div class="upd-chain-icon">${_azureIcon(c.service_id, 20)}</div>
                        <div class="upd-chain-info">
                            <div class="upd-chain-name">${escapeHtml(shortName)}</div>
                            <div class="upd-chain-versions">
                                <span class="upd-chain-ver-current upd-chain-ver-clickable" onclick="event.stopPropagation(); showVersionPicker('${escapeHtml(templateId)}','${escapeHtml(c.service_id)}', this)" title="Click to change pinned version">📌 ${c.current_semver || '—'}</span>
                                ${c.upgrade_available ? `<span class="upd-chain-arrow">→</span><span class="upd-chain-ver-latest">${c.latest_semver}</span>` : '<span class="upd-chain-ver-ok">✓ latest</span>'}
                            </div>
                        </div>
                        <div class="upd-chain-actions">
                            ${c.upgrade_available
                                ? `<button class="dep-upgrade-btn" onclick="upgradeTemplateDep('${escapeHtml(templateId)}','${escapeHtml(c.service_id)}','${escapeHtml(c.latest_semver)}')">⬆ Upgrade</button>`
                                : '<span class="upd-chain-badge-ok">Current</span>'}
                        </div>
                    </div>
                    ${deps.length ? `
                    <div class="upd-chain-deps">
                        ${deps.map(d => {
                            const depComp = components.find(x => x.service_id === d.to);
                            const depName = depComp ? (depComp.name || d.to.split('/').pop()) : d.to.split('/').pop();
                            return `<span class="upd-chain-dep-link" title="${escapeHtml(d.reason)}">${d.required ? '🔗' : '🔹'} depends on <strong>${escapeHtml(depName)}</strong></span>`;
                        }).join('')}
                    </div>` : ''}
                </div>`;
        }

        // External dependencies
        if (requires.length) {
            html += `
                <div class="upd-chain-ext-header">External Dependencies</div>
                ${requires.map(r => {
                    const rType = r.type || r;
                    return `
                    <div class="upd-chain-item upd-item-ext">
                        <div class="upd-chain-row">
                            <div class="upd-chain-icon">${_azureIcon(rType, 20)}</div>
                            <div class="upd-chain-info">
                                <div class="upd-chain-name">${_shortType(rType)}</div>
                                <div class="upd-chain-versions"><span class="upd-chain-ver-ext">resolved at deploy time</span></div>
                            </div>
                        </div>
                    </div>`;
                }).join('')}`;
        }

        html += '</div>';

        // Upgrade all button
        if (updatable.length) {
            html += `
                <div class="upd-chain-actions-footer">
                    <button class="btn btn-sm btn-primary" onclick="recomposeBlueprint('${escapeHtml(templateId)}')">
                        🔄 Upgrade All (${updatable.length} update${updatable.length > 1 ? 's' : ''})
                    </button>
                </div>`;
        }

        resultsDiv.innerHTML = html;
    } catch (err) {
        resultsDiv.innerHTML = `<div class="compose-empty">Failed: ${err.message}</div>`;
    } finally {
        btn.disabled = false;
        btn.textContent = '🔍 Check for Updates';
    }
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

        const statusIcons = { draft: '📝', passed: '🧪', validated: '🔬', failed: '●', approved: '●' };

        // Sort versions: most recent first by created_at, then by version number descending
        const sorted = [...versions].sort((a, b) => {
            // By created_at descending
            const aDate = a.created_at || '';
            const bDate = b.created_at || '';
            if (aDate !== bDate) return bDate.localeCompare(aDate);
            // Fallback: by version number descending
            return (b.version || 0) - (a.version || 0);
        });

        container.innerHTML = sorted.map((v, idx) => {
            const isActive = v.version === data.active_version;
            const semverDisplay = v.semver ? v.semver : `${v.version}.0.0`;
            const changeLabel = _inferChangeType(v.created_by, v.changelog);
            const dateStr = v.created_at ? v.created_at.substring(0, 10) : '';
            const hasTemplate = (v.template_size_bytes || 0) > 0;
            const hasLogs = !!v.has_remediation_log;
            const tid = escapeHtml(templateId);
            // Previous version for diff — sorted is newest-first, so prev is idx+1
            const prevVersion = idx < sorted.length - 1 ? sorted[idx + 1].version : null;

            return `
                <div class="comp-verlog-item ${isActive ? 'comp-verlog-active' : ''} comp-verlog-${v.status}">
                    <div class="comp-verlog-row">
                        <span class="comp-verlog-ver">${semverDisplay}</span>
                        <span class="comp-verlog-icon">${statusIcons[v.status] || '❓'}</span>
                        ${isActive ? '<span class="comp-verlog-active-tag">Active</span>' : ''}
                        ${changeLabel ? `<span class="comp-verlog-change">${changeLabel}</span>` : ''}
                        <span class="comp-verlog-date">${dateStr}</span>
                        <span class="comp-verlog-actions">
                            ${hasLogs ? `<button class="comp-verlog-btn comp-verlog-btn-logs" onclick="viewRemediationLog('${tid}', ${v.version})" title="View remediation pipeline logs">📋 Logs</button>` : ''}
                            ${hasTemplate && prevVersion != null ? `<button class="comp-verlog-btn comp-verlog-btn-diff" onclick="toggleVersionDiff(this, '${tid}', ${prevVersion}, ${v.version})" title="Diff against previous version">± Diff</button>` : ''}
                            ${hasTemplate ? `<button class="comp-verlog-btn comp-verlog-btn-view" onclick="viewCatalogTemplateVersion('${tid}', ${v.version})" title="View ARM template">👁 View</button>` : ''}
                            ${hasTemplate ? `<button class="comp-verlog-btn comp-verlog-btn-deploy" onclick="deployCatalogTemplateVersion('${tid}', ${v.version}, '${semverDisplay}')" title="Deploy this version">🚀 Deploy</button>` : ''}
                        </span>
                    </div>
                    ${v.changelog ? `<div class="comp-verlog-note">${escapeHtml(v.changelog)}</div>` : ''}
                </div>
            `;
        }).join('');
    } catch (err) {
        container.innerHTML = `<div class="compose-empty">Failed to load versions: ${err.message}</div>`;
    }
}

/* ── Remediation Log Viewer ────────────────────────────────── */

async function viewRemediationLog(templateId, version) {
    // Fetch the full version detail including remediation_log
    try {
        const res = await fetch(`/api/catalog/templates/${encodeURIComponent(templateId)}/versions/${version}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();

        const vr = data.validation_results || {};
        const logEvents = vr.remediation_log || [];
        if (!logEvents.length) {
            showToast('No remediation logs found for this version.', 'info');
            return;
        }

        _showRemediationLogModal(data, logEvents);
    } catch (err) {
        showToast(`Failed to load logs: ${err.message}`, 'error');
    }
}

function _showRemediationLogModal(versionData, logEvents) {
    // Remove existing modal if any
    const old = document.getElementById('remediation-log-modal');
    if (old) old.remove();

    const semver = versionData.semver || `${versionData.version}.0.0`;

    // Group events by step_id to show structured pipeline
    const steps = [];
    const stepMap = {};
    for (const evt of logEvents) {
        const sid = evt.step_id || 'unknown';
        if (!stepMap[sid]) {
            stepMap[sid] = { id: sid, status: 'running', logs: [], started: evt.timestamp, duration_ms: 0 };
            steps.push(stepMap[sid]);
        }
        const step = stepMap[sid];
        if (evt.type === 'step_start') {
            step.started = evt.timestamp;
        } else if (evt.type === 'step_log') {
            step.logs.push(evt);
        } else if (evt.type === 'step_end') {
            step.status = evt.status || 'success';
            step.duration_ms = evt.duration_ms || 0;
        }
    }

    const statusIcon = { success: '●', failed: '●', warning: '●', skipped: '○', running: '⏳' };
    const statusClass = { success: 'rlog-ok', failed: 'rlog-fail', warning: 'rlog-warn', skipped: 'rlog-skip' };

    const stepsHtml = steps.map(step => {
        const label = step.id.replace(/^job-\d+-/, '');
        const icon = statusIcon[step.status] || '●';
        const cls = statusClass[step.status] || 'rlog-ok';
        const dur = step.duration_ms > 0 ? `${(step.duration_ms / 1000).toFixed(1)}s` : '';

        const logsHtml = step.logs.map(l => {
            const lvl = l.level === 'error' ? 'rlog-line-err' : l.level === 'warning' ? 'rlog-line-warn' : '';
            const ts = l.timestamp ? l.timestamp.substring(11, 19) : '';
            return `<div class="rlog-line ${lvl}"><span class="rlog-ts">${ts}</span><span class="rlog-msg">${escapeHtml(l.message)}</span></div>`;
        }).join('');

        return `
            <div class="rlog-step ${cls}">
                <div class="rlog-step-header" onclick="this.parentElement.classList.toggle('rlog-expanded')">
                    <span class="rlog-step-icon ${cls}">${icon}</span>
                    <span class="rlog-step-label">${escapeHtml(label)}</span>
                    ${dur ? `<span class="rlog-step-dur">${dur}</span>` : ''}
                    <span class="rlog-step-arrow">▸</span>
                </div>
                <div class="rlog-step-body">${logsHtml || '<div class="rlog-line rlog-line-empty">No log output</div>'}</div>
            </div>`;
    }).join('');

    const modal = document.createElement('div');
    modal.id = 'remediation-log-modal';
    modal.className = 'rlog-modal-overlay';
    modal.onclick = (e) => { if (e.target === modal) modal.remove(); };
    modal.innerHTML = `
        <div class="rlog-modal">
            <div class="rlog-modal-header">
                <span class="rlog-modal-title">📋 Remediation Log — v${escapeHtml(semver)}</span>
                <button class="rlog-modal-close" onclick="document.getElementById('remediation-log-modal').remove()">✕</button>
            </div>
            <div class="rlog-modal-body">
                ${stepsHtml}
            </div>
        </div>`;
    document.body.appendChild(modal);
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
            ? (deepHealed ? '🔧 Verified — I had to fix a few things along the way' : '✅ Verified — template deployed successfully')
            : '❌ Verification failed — needs more work';

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
            ${detailHtml || '<div class="ver-pipeline-detail detail-info"><div class="ver-detail-title">ℹ️ Haven\'t tested this version yet — run validation to see how it does.</div></div>'}
        </div>`;
}

// ── Compliance Profile Picker ───────────────────────────────

function _renderComplianceProfilePicker(tmpl) {
    const profile = tmpl.compliance_profile; // null or array
    const isConfigured = profile !== null && profile !== undefined;

    // Group categories like the governance board does
    const ungrouped = GOV_CATEGORIES.filter(c => !c.group);
    const groups = {};
    for (const cat of GOV_CATEGORIES) {
        if (cat.group) {
            if (!groups[cat.group]) groups[cat.group] = { icon: cat.groupIcon || '📁', cats: [] };
            groups[cat.group].cats.push(cat);
        }
    }

    let html = `<div class="compliance-profile-controls">
        <label class="compliance-profile-toggle">
            <input type="checkbox" id="cp-toggle-configured"
                ${isConfigured ? 'checked' : ''}
                onchange="toggleComplianceProfileConfigured('${escapeHtml(tmpl.id)}', this.checked)">
            <span>Custom profile assigned</span>
        </label>
        ${isConfigured && profile.length === 0
            ? '<span class="compliance-profile-exempt-badge">🚫 Exempt — no compliance checks</span>'
            : !isConfigured
            ? '<span class="compliance-profile-all-badge">🌐 All standards apply (default)</span>'
            : `<span class="compliance-profile-count-badge">${profile.length} categor${profile.length === 1 ? 'y' : 'ies'} selected</span>`
        }
    </div>`;

    html += `<div class="compliance-profile-cats" style="${isConfigured ? '' : 'display:none'}" id="cp-cats-container">`;

    // Ungrouped categories
    for (const cat of ungrouped) {
        const checked = isConfigured && profile.includes(cat.id);
        html += `
        <label class="compliance-profile-cat ${checked ? 'cp-selected' : ''}">
            <input type="checkbox" value="${cat.id}" ${checked ? 'checked' : ''}
                onchange="onComplianceProfileChange('${escapeHtml(tmpl.id)}')">
            <span class="cp-cat-icon">${cat.icon}</span>
            <span class="cp-cat-name">${escapeHtml(cat.name)}</span>
        </label>`;
    }

    // Grouped categories (regulatory frameworks)
    for (const [groupName, group] of Object.entries(groups)) {
        html += `<div class="compliance-profile-group">
            <div class="compliance-profile-group-header">${group.icon} ${escapeHtml(groupName)}</div>
            <div class="compliance-profile-group-cats">`;
        for (const cat of group.cats) {
            const checked = isConfigured && profile.includes(cat.id);
            html += `
            <label class="compliance-profile-cat compliance-profile-fw ${checked ? 'cp-selected' : ''}">
                <input type="checkbox" value="${cat.id}" ${checked ? 'checked' : ''}
                    onchange="onComplianceProfileChange('${escapeHtml(tmpl.id)}')">
                <span class="cp-cat-icon">${cat.icon}</span>
                <span class="cp-cat-name">${escapeHtml(cat.name)}</span>
            </label>`;
        }
        html += `</div></div>`;
    }

    html += `</div>`;
    return html;
}

function toggleComplianceProfileConfigured(templateId, configured) {
    const container = document.getElementById('cp-cats-container');
    if (container) container.style.display = configured ? '' : 'none';

    if (!configured) {
        // Save null (not configured = all standards apply)
        _saveComplianceProfile(templateId, null);
    } else {
        // Default to empty (exempt) — user will check categories
        _saveComplianceProfile(templateId, []);
    }
}

function onComplianceProfileChange(templateId) {
    const container = document.getElementById('cp-cats-container');
    if (!container) return;

    const checked = [...container.querySelectorAll('input[type=checkbox]:checked')].map(cb => cb.value);

    // Update visual state
    container.querySelectorAll('.compliance-profile-cat').forEach(label => {
        const cb = label.querySelector('input[type=checkbox]');
        label.classList.toggle('cp-selected', cb && cb.checked);
    });

    _saveComplianceProfile(templateId, checked);
}

async function _saveComplianceProfile(templateId, profile) {
    try {
        const res = await fetch(`/api/catalog/templates/${encodeURIComponent(templateId)}/compliance-profile`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ profile }),
        });
        if (!res.ok) throw new Error('Failed to save');

        // Update local template data
        const tmpl = allTemplates.find(t => t.id === templateId);
        if (tmpl) tmpl.compliance_profile = profile;

        // Update the status badges in the controls area
        const controls = document.querySelector('.compliance-profile-controls');
        if (controls) {
            const isConfigured = profile !== null;
            const badgeEl = controls.querySelector('.compliance-profile-exempt-badge, .compliance-profile-all-badge, .compliance-profile-count-badge');
            if (badgeEl) {
                if (!isConfigured) {
                    badgeEl.className = 'compliance-profile-all-badge';
                    badgeEl.textContent = '🌐 All standards apply (default)';
                } else if (profile.length === 0) {
                    badgeEl.className = 'compliance-profile-exempt-badge';
                    badgeEl.textContent = '🚫 Exempt — no compliance checks';
                } else {
                    badgeEl.className = 'compliance-profile-count-badge';
                    badgeEl.textContent = `${profile.length} categor${profile.length === 1 ? 'y' : 'ies'} selected`;
                }
            }
        }
    } catch (err) {
        console.error('Failed to save compliance profile:', err);
    }
}

// ── Compliance Scan ─────────────────────────────────────────

let _lastScanData = null;

async function runComplianceScan(templateId) {
    const resultsEl = document.getElementById('tmpl-scan-results');
    if (!resultsEl) return;

    _lastScanData = null;

    // Show loading
    resultsEl.innerHTML = `
    <div class="scan-loading">
        <div class="scan-loading-spinner"></div>
        <span>Scanning template against ${allStandards.length} organization standards…</span>
    </div>`;

    try {
        const res = await fetch(`/api/catalog/templates/${encodeURIComponent(templateId)}/compliance-scan`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({}),
        });
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Scan failed');
        }
        const data = await res.json();
        _lastScanData = data;
        resultsEl.innerHTML = _renderComplianceScanReport(data);
    } catch (err) {
        resultsEl.innerHTML = `<div class="scan-error">❌ Scan failed: ${escapeHtml(err.message)}</div>`;
    }
}

function _renderComplianceScanReport(data) {
    const score = data.score;
    const total = data.total_checks;
    const passed = data.total_passed;
    const violations = data.violations;
    const sev = data.severity_breakdown;

    // Score color
    const scoreClass = score >= 90 ? 'scan-score-great' : score >= 70 ? 'scan-score-ok' : score >= 50 ? 'scan-score-warn' : 'scan-score-bad';

    // Score ring SVG
    const circumference = 2 * Math.PI * 42;
    const offset = circumference - (score / 100) * circumference;

    let html = `
    <div class="scan-report">
        <div class="scan-header">
            <div class="scan-score-ring ${scoreClass}">
                <svg viewBox="0 0 100 100">
                    <circle cx="50" cy="50" r="42" class="scan-ring-bg" />
                    <circle cx="50" cy="50" r="42" class="scan-ring-fill" stroke-dasharray="${circumference}" stroke-dashoffset="${offset}" />
                </svg>
                <div class="scan-score-text">
                    <span class="scan-score-num">${score}</span>
                    <span class="scan-score-pct">%</span>
                </div>
            </div>
            <div class="scan-header-info">
                <h4>Compliance Score</h4>
                <div class="scan-header-stats">
                    <span class="scan-stat">${passed} <em>passed</em></span>
                    <span class="scan-stat scan-stat-fail">${violations} <em>violations</em></span>
                    <span class="scan-stat">${total} <em>checks</em></span>
                </div>
                <div class="scan-meta">
                    ${data.templates_scanned} template${data.templates_scanned > 1 ? 's' : ''} scanned · ${data.standards_count} standards loaded
                    ${data.profile_applied
                        ? (data.compliance_profile && data.compliance_profile.length > 0
                            ? ` · 📋 Profile: ${data.compliance_profile.length} categor${data.compliance_profile.length === 1 ? 'y' : 'ies'}`
                            : data.compliance_profile && data.compliance_profile.length === 0
                            ? ' · 🚫 Exempt (0 standards)'
                            : '')
                        : ' · 🌐 All standards'
                    }
                </div>
            </div>
        </div>

        <div class="scan-severity-bar">`;

    // Severity breakdown chips
    const sevOrder = ['critical', 'high', 'medium', 'low'];
    const sevIcons = { critical: '🔴', high: '🟠', medium: '🟡', low: '🟢' };
    for (const s of sevOrder) {
        const info = sev[s] || { total: 0, passed: 0 };
        if (info.total === 0) continue;
        const failed = info.total - info.passed;
        html += `
        <div class="scan-sev-chip scan-sev-${s} ${failed > 0 ? 'scan-sev-fail' : 'scan-sev-pass'}">
            ${sevIcons[s]} <strong>${failed > 0 ? failed + ' fail' : '✓'}</strong> <span>${s}</span>
        </div>`;
    }

    html += `</div>`;

    // Per-template results
    for (const tmplResult of data.results) {
        if (tmplResult.error) {
            html += `
            <div class="scan-tmpl-block">
                <div class="scan-tmpl-header">
                    <span class="scan-tmpl-name">${tmplResult.is_dependency ? '🔗 ' : ''}${escapeHtml(tmplResult.template_name)}</span>
                    <span class="scan-tmpl-error">⚠️ ${escapeHtml(tmplResult.error)}</span>
                </div>
            </div>`;
            continue;
        }

        const resources = tmplResult.resources || [];
        const hasFindings = resources.some(r => r.findings && r.findings.length > 0);

        if (!hasFindings) {
            html += `
            <div class="scan-tmpl-block">
                <div class="scan-tmpl-header">
                    <span class="scan-tmpl-name">${tmplResult.is_dependency ? '🔗 ' : '📄 '}${escapeHtml(tmplResult.template_name)}</span>
                    <span class="scan-tmpl-badge scan-tmpl-na">No standards apply</span>
                </div>
            </div>`;
            continue;
        }

        html += `
        <div class="scan-tmpl-block">
            <div class="scan-tmpl-header">
                <span class="scan-tmpl-name">${tmplResult.is_dependency ? '🔗 ' : '📄 '}${escapeHtml(tmplResult.template_name)}</span>
            </div>`;

        for (const res of resources) {
            if (!res.findings || res.findings.length === 0) continue;

            const resPassCount = res.findings.filter(f => f.passed).length;
            const resFailCount = res.findings.length - resPassCount;
            const resAllPassed = resFailCount === 0;

            html += `
            <div class="scan-resource ${resAllPassed ? 'scan-res-ok' : 'scan-res-fail'}">
                <div class="scan-res-header" onclick="this.parentElement.classList.toggle('scan-res-expanded')">
                    <span class="scan-res-status">${resAllPassed ? '✅' : '❌'}</span>
                    <span class="scan-res-type">${escapeHtml(res.resource_type)}</span>
                    <span class="scan-res-name">${escapeHtml(res.resource_name)}</span>
                    <span class="scan-res-counts">
                        ${resPassCount > 0 ? `<span class="scan-cnt-pass">${resPassCount} ✓</span>` : ''}
                        ${resFailCount > 0 ? `<span class="scan-cnt-fail">${resFailCount} ✗</span>` : ''}
                    </span>
                    <span class="scan-res-chevron">▶</span>
                </div>
                <div class="scan-res-findings">
                    <table class="scan-findings-table">
                        <thead>
                            <tr><th>Status</th><th>Standard</th><th>Severity</th><th>Detail</th></tr>
                        </thead>
                        <tbody>`;

            // Sort: failures first, then by severity
            const sevPriority = { critical: 0, high: 1, medium: 2, low: 3 };
            const sorted = [...res.findings].sort((a, b) => {
                if (a.passed !== b.passed) return a.passed ? 1 : -1;
                return (sevPriority[a.severity] || 9) - (sevPriority[b.severity] || 9);
            });

            for (const f of sorted) {
                html += `
                    <tr class="${f.passed ? 'scan-f-pass' : 'scan-f-fail'}">
                        <td>${f.passed ? '✅' : '❌'}</td>
                        <td>
                            <div class="scan-f-name">${escapeHtml(f.standard_name)}</div>
                            <div class="scan-f-cat">${escapeHtml(f.category)}</div>
                        </td>
                        <td><span class="scan-f-sev scan-f-sev-${f.severity}">${sevIcons[f.severity] || '⚪'} ${f.severity}</span></td>
                        <td>
                            <div class="scan-f-detail">${escapeHtml(f.detail)}</div>
                            ${!f.passed && f.remediation ? `<div class="scan-f-remediation">💡 ${escapeHtml(f.remediation)}</div>` : ''}
                        </td>
                    </tr>`;
            }

            html += `</tbody></table></div></div>`;
        }

        html += `</div>`;
    }

    html += `</div>`;

    // Remediate button (only if there are violations)
    if (data.violations > 0) {
        html += `
        <div class="scan-remediate-section">
            <button class="btn btn-sm scan-auto-remediate-btn" onclick="autoRemediateLoop('${escapeHtml(data.template_id)}')">
                🛡️ Auto-Remediate All
            </button>
            <button class="btn btn-sm scan-remediate-btn" onclick="runComplianceRemediation('${escapeHtml(data.template_id)}')">
                🔧 Manual Plan
            </button>
            ${_copilotBadge()}
            <div id="scan-remediation-results"></div>
        </div>`;
    }

    html += `</div>`;
    return html;
}

/* ── Compliance Remediation (Plan → Execute) ──────────────── */

async function runComplianceRemediation(templateId) {
    const resultsEl = document.getElementById('scan-remediation-results');
    if (!resultsEl || !_lastScanData) return;

    resultsEl.innerHTML = `
    <div class="scan-loading">
        <div class="scan-loading-spinner"></div>
        <span>Copilot SDK is analyzing ${_lastScanData.violations} violations and generating a remediation plan…</span>
    </div>`;

    try {
        const res = await fetch(`/api/catalog/templates/${encodeURIComponent(templateId)}/compliance-remediate/plan`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ scan_data: _lastScanData }),
        });
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Plan generation failed');
        }
        const data = await res.json();
        resultsEl.innerHTML = _renderRemediationPlan(templateId, data);
    } catch (err) {
        resultsEl.innerHTML = `<div class="scan-error">❌ Plan failed: ${escapeHtml(err.message)}</div>`;
    }
}

/* ── Auto-Remediation Loop ────────────────────────────────── */
// Chains: scan → plan → execute → re-scan → repeat until clean or max rounds.
let _autoRemediating = false;       // true while auto-loop is active
let _autoRemediateRound = 0;        // current round (1-based)
const _AUTO_REMEDIATE_MAX_ROUNDS = 3;

async function autoRemediateLoop(templateId) {
    const resultsEl = document.getElementById('scan-remediation-results');
    if (!resultsEl) return;
    if (_autoRemediating) return;   // prevent re-entry

    _autoRemediating = true;
    _autoRemediateRound = 0;

    try {
        for (let round = 1; round <= _AUTO_REMEDIATE_MAX_ROUNDS; round++) {
            _autoRemediateRound = round;

            // ── Step 1: Scan (skip on round 1 if we already have scan data) ──
            if (round > 1 || !_lastScanData) {
                resultsEl.innerHTML = `
                <div class="scan-loading auto-round-banner">
                    <div class="scan-loading-spinner"></div>
                    <span>Round ${round}/${_AUTO_REMEDIATE_MAX_ROUNDS} — Scanning for remaining violations…</span>
                </div>`;
                _lastScanData = null;
                try {
                    const scanRes = await fetch(`/api/catalog/templates/${encodeURIComponent(templateId)}/compliance-scan`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({}),
                    });
                    if (!scanRes.ok) throw new Error('Scan failed');
                    _lastScanData = await scanRes.json();
                } catch (err) {
                    resultsEl.innerHTML += `<div class="scan-error">❌ Scan failed: ${escapeHtml(err.message)}</div>`;
                    break;
                }
            }

            // ── Check: clean? ──
            if (!_lastScanData || _lastScanData.violations === 0) {
                resultsEl.innerHTML = `
                <div class="auto-round-done auto-round-clean">
                    <span class="auto-round-icon">🛡️</span>
                    <span><strong>Fully compliant</strong> — 0 violations after ${round > 1 ? round - 1 : 0} remediation round(s). Score: ${_lastScanData ? _lastScanData.score : '?'}%</span>
                </div>`;
                // Refresh the scan UI
                const scanResultsEl = document.getElementById('tmpl-scan-results');
                if (scanResultsEl && _lastScanData) {
                    scanResultsEl.innerHTML = _renderComplianceScanReport(_lastScanData);
                }
                break;
            }

            // ── Step 2: Plan ──
            resultsEl.innerHTML = `
            <div class="scan-loading auto-round-banner">
                <div class="scan-loading-spinner"></div>
                <span>Round ${round}/${_AUTO_REMEDIATE_MAX_ROUNDS} — Planning fixes for ${_lastScanData.violations} violation(s)…</span>
            </div>`;

            let planData;
            try {
                const planRes = await fetch(`/api/catalog/templates/${encodeURIComponent(templateId)}/compliance-remediate/plan`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ scan_data: _lastScanData }),
                });
                if (!planRes.ok) throw new Error('Plan generation failed');
                planData = await planRes.json();
            } catch (err) {
                resultsEl.innerHTML += `<div class="scan-error">❌ Plan failed: ${escapeHtml(err.message)}</div>`;
                break;
            }

            if (!planData.plan || planData.plan.length === 0) {
                resultsEl.innerHTML = `<div class="auto-round-done auto-round-info">✅ No remediation steps generated — scan may require manual review.</div>`;
                break;
            }

            // ── Step 3: Execute ──
            window._lastRemediationPlan = planData.plan;

            // Create pipeline container for this round
            const roundLabel = document.createElement('div');
            roundLabel.className = 'auto-round-label';
            roundLabel.innerHTML = `<span class="auto-round-badge">Round ${round}</span> Fixing ${planData.plan.length} step(s) from ${_lastScanData.violations} violation(s)`;
            resultsEl.innerHTML = '';
            resultsEl.appendChild(roundLabel);

            // Remove old pipeline DOM to avoid ID collisions
            const oldPipeline = document.getElementById('ado-pipeline');
            if (oldPipeline) oldPipeline.remove();

            const pipelineDiv = document.createElement('div');
            pipelineDiv.className = 'ado-pipeline';
            pipelineDiv.id = 'ado-pipeline';
            resultsEl.appendChild(pipelineDiv);

            // Execute and wait for pipeline_done
            const pipelineDone = await _executeAndWait(templateId, planData.plan);

            if (!pipelineDone || !pipelineDone.all_success) {
                // Pipeline had errors — stop looping but don't hide the pipeline
                const stopMsg = document.createElement('div');
                stopMsg.className = 'auto-round-done auto-round-warn';
                stopMsg.innerHTML = `⚠️ Pipeline completed with errors — stopping auto-remediation. Review results above.`;
                resultsEl.appendChild(stopMsg);
                break;
            }

            // Wait briefly for recomposition to settle
            await new Promise(r => setTimeout(r, 1500));

            // If this is the last round, do a final scan to show results
            if (round === _AUTO_REMEDIATE_MAX_ROUNDS) {
                const finalMsg = document.createElement('div');
                finalMsg.className = 'auto-round-done auto-round-warn';
                finalMsg.innerHTML = `⚠️ Reached max ${_AUTO_REMEDIATE_MAX_ROUNDS} rounds. Running final scan…`;
                resultsEl.appendChild(finalMsg);

                // Run final scan and render in the scan results area
                await runComplianceScan(templateId);
                break;
            }
            // Otherwise loop continues — next iteration will re-scan
        }
    } finally {
        _autoRemediating = false;
        _autoRemediateRound = 0;
        // Refresh data
        loadAllData().then(() => _loadTemplateVersionHistory(templateId));
    }
}

/** Execute a remediation plan and return a Promise that resolves with the pipeline_done event data. */
async function _executeAndWait(templateId, planSteps) {
    const pipeline = document.getElementById('ado-pipeline');
    if (!pipeline) return null;

    return new Promise(async (resolve) => {
        let pipelineDoneData = null;
        try {
            const res = await fetch(`/api/catalog/templates/${encodeURIComponent(templateId)}/compliance-remediate/execute`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ plan: planSteps, scan_data: _lastScanData }),
            });
            if (!res.ok) {
                const err = await res.json();
                throw new Error(err.detail || 'Execution failed');
            }

            const reader = res.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';
            let state = null;

            while (true) {
                const { value, done } = await reader.read();
                if (done) break;
                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n');
                buffer = lines.pop();
                for (const line of lines) {
                    if (!line.trim()) continue;
                    try {
                        const event = JSON.parse(line);
                        if (event.type === 'pipeline_done') {
                            pipelineDoneData = event;
                        }
                        state = _adoHandleEvent(pipeline, event, state);
                    } catch { /* skip malformed */ }
                }
            }
            if (buffer.trim()) {
                try {
                    const event = JSON.parse(buffer);
                    if (event.type === 'pipeline_done') pipelineDoneData = event;
                    state = _adoHandleEvent(pipeline, event, state);
                } catch {}
            }
        } catch (err) {
            pipeline.innerHTML += `<div class="ado-error"><span class="ado-error-icon">❌</span><span>Pipeline failed: ${escapeHtml(err.message)}</span></div>`;
        }
        resolve(pipelineDoneData);
    });
}

function _renderRemediationPlan(templateId, data) {
    const steps = data.plan || [];
    if (steps.length === 0) {
        return `<div class="remed-empty">✅ ${escapeHtml(data.summary || 'No remediation needed')}</div>`;
    }

    let html = `
    <div class="remed-plan">
        <div class="remed-plan-header">
            <div class="remed-plan-title">
                <span class="remed-plan-icon">📋</span>
                <h4>Remediation Plan</h4>
                ${_copilotBadge()}
                <span class="remed-plan-count">${steps.length} step${steps.length > 1 ? 's' : ''}</span>
            </div>
            <p class="remed-plan-summary">${escapeHtml(data.summary)}</p>
        </div>`;

    // Template version summary bar — show parent + dependencies
    const tvInfo = data.template_versions || {};
    const templateIds = Object.keys(tvInfo);
    if (templateIds.length > 0) {
        // Separate parent from dependencies
        const parent = templateIds.find(tid => !tvInfo[tid].is_dependency);
        const deps = templateIds.filter(tid => tvInfo[tid].is_dependency);
        const changeLabels = { minor: 'Minor', patch: 'Patch', major: 'Major', none: '—' };

        html += `<div class="remed-version-bar">`;

        // Parent template card
        if (parent) {
            const vi = tvInfo[parent];
            html += `
            <div class="remed-version-card remed-ver-parent">
                <span class="remed-ver-name">${escapeHtml(vi.template_name || parent)}</span>
                <span class="remed-ver-arrow">
                    <span class="remed-ver-current">${escapeHtml(vi.current_semver)}</span>
                    →
                    <span class="remed-ver-next">${escapeHtml(vi.projected_semver)}</span>
                </span>
                <span class="remed-ver-type remed-ver-type-${vi.change_type}">${changeLabels[vi.change_type] || vi.change_type}</span>
            </div>`;
        }

        // Dependency cards
        if (deps.length > 0) {
            html += `<div class="remed-ver-deps">
                <div class="remed-ver-deps-label">Underlying Service Templates</div>
                <div class="remed-ver-deps-grid">`;
            for (const tid of deps) {
                const vi = tvInfo[tid];
                const hasViolations = (vi.violation_count || 0) > 0;
                const resourceTypes = (vi.resource_types || []).map(r => {
                    const parts = r.split('/');
                    return parts[parts.length - 1];
                });
                html += `
                <div class="remed-version-card remed-ver-dep ${hasViolations ? 'remed-ver-dep-affected' : 'remed-ver-dep-clean'}">
                    <div class="remed-ver-dep-header">
                        <span class="remed-ver-name">${escapeHtml(vi.template_name || tid)}</span>
                        ${hasViolations
                            ? `<span class="remed-ver-type remed-ver-type-${vi.change_type}">${changeLabels[vi.change_type] || vi.change_type}</span>`
                            : '<span class="remed-ver-clean-badge">✅ Clean</span>'}
                        ${vi.upgrade_available ? '<span class="remed-upgrade-badge" title="Newer compliant version found — will upgrade instead of AI fix">⬆ Upgrade</span>' : ''}
                        ${vi.upgrade_action === 'ai_fix_latest' ? '<span class="remed-upgrade-badge remed-upgrade-pull" title="Latest version pulled for AI remediation">⬇ Latest</span>' : ''}
                    </div>
                    ${hasViolations ? `
                    <div class="remed-ver-arrow">
                        <span class="remed-ver-current">${escapeHtml(vi.current_semver)}</span>
                        →
                        <span class="remed-ver-next">${escapeHtml(vi.projected_semver)}</span>
                    </div>
                    <div class="remed-ver-dep-reason">${vi.upgrade_available
                        ? 'Compliant version available — will upgrade'
                        : `${vi.violation_count} violation${vi.violation_count !== 1 ? 's' : ''} to fix`}</div>`
                    : `<div class="remed-ver-dep-ver">${escapeHtml(vi.current_semver)}</div>`}
                    ${resourceTypes.length > 0 ? `<div class="remed-ver-dep-resources">${resourceTypes.map(r => `<span class="remed-ver-dep-rt">${escapeHtml(r)}</span>`).join('')}</div>` : ''}
                </div>`;
            }
            html += `</div></div>`;
        }

        html += `</div>`;
    }

    html += `<div class="remed-steps">`;

    const sevIcons = { critical: '🔴', high: '🟠', medium: '🟡', low: '🟢' };
    const sevColors = { critical: 'remed-sev-critical', high: 'remed-sev-high', medium: 'remed-sev-medium', low: 'remed-sev-low' };

    for (const step of steps) {
        const sev = (step.severity || 'medium').toLowerCase();
        const stepVer = tvInfo[step.template_id] || {};
        html += `
        <div class="remed-step ${sevColors[sev] || ''}">
            <div class="remed-step-num">${step.step || '·'}</div>
            <div class="remed-step-body">
                <div class="remed-step-action">
                    <span class="remed-step-sev">${sevIcons[sev] || '⚪'}</span>
                    ${escapeHtml(step.action || '')}
                </div>
                <div class="remed-step-detail">${escapeHtml(step.detail || '')}</div>
                <div class="remed-step-meta">
                    <span class="remed-step-tmpl">📄 ${escapeHtml(step.template_name || step.template_id || '')}</span>
                    ${stepVer.projected_semver ? `<span class="remed-step-ver">v${escapeHtml(step.current_semver || '')} → v${escapeHtml(step.projected_semver || '')}</span>` : ''}
                    ${(step.standards_addressed || []).map(s => `<span class="remed-step-std">${escapeHtml(s)}</span>`).join('')}
                </div>
            </div>
        </div>`;
    }

    html += `
        </div>
        <div class="remed-execute-section">
            <button class="btn remed-execute-btn" onclick="executeRemediationPlan('${escapeHtml(templateId)}')">
                ⚡ Execute Plan & Update Templates
            </button>
            <div class="remed-execute-warn">This will create new template versions with compliance fixes applied via the Copilot SDK.</div>
        </div>
    </div>`;

    // Stash the plan for execution
    window._lastRemediationPlan = data.plan;

    return html;
}

async function executeRemediationPlan(templateId) {
    const planSteps = window._lastRemediationPlan;
    if (!planSteps || !_lastScanData) return;

    const execSection = document.querySelector('.remed-execute-section');
    if (!execSection) return;

    // Remove any leftover pipeline element from a previous execution
    // to avoid duplicate-ID conflicts with getElementById
    const oldPipeline = document.getElementById('ado-pipeline');
    if (oldPipeline) oldPipeline.remove();

    execSection.innerHTML = `<div class="ado-pipeline" id="ado-pipeline"></div>`;
    const pipeline = document.getElementById('ado-pipeline');

    try {
        const res = await fetch(`/api/catalog/templates/${encodeURIComponent(templateId)}/compliance-remediate/execute`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ plan: planSteps, scan_data: _lastScanData }),
        });
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Execution failed');
        }

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let state = null;

        while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop();
            for (const line of lines) {
                if (!line.trim()) continue;
                try {
                    state = _adoHandleEvent(pipeline, JSON.parse(line), state);
                } catch { /* skip malformed */ }
            }
        }
        if (buffer.trim()) {
            try { state = _adoHandleEvent(pipeline, JSON.parse(buffer), state); } catch {}
        }
    } catch (err) {
        pipeline.innerHTML = `
            <div class="ado-error">
                <span class="ado-error-icon">❌</span>
                <span>Pipeline failed: ${escapeHtml(err.message)}</span>
            </div>`;
    }
}

/* ── Live Log Stream Renderer ── */

/**
 * Render a live log stream event into a container.
 * Uses a standardized NDJSON format:
 *   { type: "log"|"step"|"result"|"error", phase, status, message, detail?, ts }
 *
 * Call this once per event. It appends/updates the log in place.
 */
function renderLogStreamEvent(container, event) {
    if (!container) return;

    // Ensure log structure exists
    if (!container.querySelector('.logstream')) {
        container.innerHTML = '';
        const wrapper = document.createElement('div');
        wrapper.className = 'logstream';
        container.appendChild(wrapper);
    }
    const wrapper = container.querySelector('.logstream');

    const statusIcon = {
        running: '<span class="logstream-icon logstream-icon-running">⏳</span>',
        success: '<span class="logstream-icon logstream-icon-success">●</span>',
        warning: '<span class="logstream-icon logstream-icon-warning">●</span>',
        error:   '<span class="logstream-icon logstream-icon-error">●</span>',
        skip:    '<span class="logstream-icon logstream-icon-skip">○</span>',
        blocked: '<span class="logstream-icon logstream-icon-skip">●</span>',
    };

    if (event.type === 'step') {
        // Step events update/create a step row
        const stepId = `logstream-step-${event.phase}`;
        let stepEl = wrapper.querySelector(`#${stepId}`);

        if (!stepEl) {
            stepEl = document.createElement('div');
            stepEl.id = stepId;
            stepEl.className = 'logstream-step';
            wrapper.appendChild(stepEl);
        }

        stepEl.className = `logstream-step logstream-step-${event.status}`;
        stepEl.innerHTML = `
            ${statusIcon[event.status] || ''}
            <span class="logstream-step-phase">${escapeHtml(event.phase)}</span>
            <span class="logstream-step-msg">${escapeHtml(event.message)}</span>
        `;

        // Auto-scroll the container
        container.scrollTop = container.scrollHeight;
    }
    else if (event.type === 'log') {
        // Log events append under the current step
        const logEl = document.createElement('div');
        logEl.className = `logstream-log logstream-log-${event.status || 'running'}`;
        logEl.innerHTML = `<span class="logstream-log-msg">${escapeHtml(event.message)}</span>`;
        wrapper.appendChild(logEl);
        container.scrollTop = container.scrollHeight;
    }
    else if (event.type === 'error') {
        const errEl = document.createElement('div');
        errEl.className = 'logstream-error';
        errEl.innerHTML = `
            ${statusIcon.error}
            <span class="logstream-error-msg">${escapeHtml(event.message)}</span>
        `;
        wrapper.appendChild(errEl);
        container.scrollTop = container.scrollHeight;
    }
    else if (event.type === 'result') {
        // Final result — appended at the bottom
        // Don't render here — let the caller handle the final result
    }
}

/**
 * Read an NDJSON stream and render events into a container.
 * Returns the final 'result' event (or null on error).
 */
async function consumeLogStream(response, container) {
    const reader = response.body.getReader();
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
                if (event.type === 'result' || event.type === 'error') {
                    finalResult = event;
                }
                renderLogStreamEvent(container, event);
            } catch (e) { /* skip malformed */ }
        }
    }

    // Process remaining buffer
    if (buffer.trim()) {
        try {
            const event = JSON.parse(buffer);
            if (event.type === 'result' || event.type === 'error') {
                finalResult = event;
            }
            renderLogStreamEvent(container, event);
        } catch (e) { /* skip */ }
    }

    return finalResult;
}

/* ── GitHub-style Diff Viewer ── */

/**
 * Toggle diff viewer for a pipeline result.
 * Lazily fetches the diff from the server on first open.
 */
async function toggleDiffViewer(btn, templateId, fromVersion, toVersion) {
    const card = btn.closest('.ado-report-card');
    let viewer = card.querySelector('.diff-viewer');

    if (viewer) {
        // Toggle visibility
        const visible = viewer.style.display !== 'none';
        viewer.style.display = visible ? 'none' : '';
        btn.classList.toggle('diff-expanded', !visible);
        return;
    }

    // Create viewer placeholder with loading state
    btn.classList.add('diff-expanded');
    viewer = document.createElement('div');
    viewer.className = 'diff-viewer';
    viewer.innerHTML = `<div class="diff-loading"><span class="diff-spinner"></span>Loading diff&hellip;</div>`;
    btn.insertAdjacentElement('afterend', viewer);

    try {
        const resp = await fetch(`/api/catalog/templates/${encodeURIComponent(templateId)}/diff?from_version=${fromVersion}&to_version=${toVersion}`);
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({ detail: resp.statusText }));
            viewer.innerHTML = `<div class="diff-error">Failed to load diff: ${escapeHtml(err.detail || 'Unknown error')}</div>`;
            return;
        }

        const data = await resp.json();
        _renderDiffViewer(viewer, data);
    } catch (e) {
        viewer.innerHTML = `<div class="diff-error">Failed to load diff: ${escapeHtml(e.message)}</div>`;
    }
}

/**
 * Render structured diff data into a GitHub-style diff viewer.
 */
function _renderDiffViewer(container, data) {
    if (!data.hunks || data.hunks.length === 0) {
        container.innerHTML = `<div class="diff-empty">No differences found between versions</div>`;
        return;
    }

    // Toolbar
    let html = `<div class="diff-toolbar">
        <div class="diff-toolbar-title">
            <span>📄</span>
            <span>v${escapeHtml(data.from_semver)} → v${escapeHtml(data.to_semver)}</span>
        </div>
        <div class="diff-toolbar-stats">
            <span class="diff-stat-add">+${data.additions}</span>
            <span class="diff-stat-del">−${data.deletions}</span>
        </div>
    </div>`;

    // Hunks
    for (const hunk of data.hunks) {
        html += `<div class="diff-hunk">`;
        html += `<div class="diff-hunk-header">${escapeHtml(hunk.header)}</div>`;
        html += `<table class="diff-table"><tbody>`;

        for (const line of hunk.lines) {
            const cls = line.type === 'add' ? 'diff-line-add'
                      : line.type === 'del' ? 'diff-line-del'
                      : 'diff-line-ctx';
            const prefix = line.type === 'add' ? '+' : line.type === 'del' ? '−' : ' ';
            const oldLn = line.old_ln != null ? line.old_ln : '';
            const newLn = line.new_ln != null ? line.new_ln : '';

            html += `<tr class="diff-line ${cls}">
                <td class="diff-ln diff-ln-old">${oldLn}</td>
                <td class="diff-ln diff-ln-new">${newLn}</td>
                <td class="diff-content"><span class="diff-prefix">${prefix}</span>${escapeHtml(line.content)}</td>
            </tr>`;
        }

        html += `</tbody></table></div>`;
    }

    container.innerHTML = html;
}

/**
 * Toggle diff viewer in the version history panel.
 * Works similarly to toggleDiffViewer but for version-history items.
 */
async function toggleVersionDiff(btn, templateId, fromVersion, toVersion) {
    const item = btn.closest('.comp-verlog-item');
    let viewer = item.querySelector('.diff-viewer');

    if (viewer) {
        const visible = viewer.style.display !== 'none';
        viewer.style.display = visible ? 'none' : '';
        btn.classList.toggle('diff-expanded', !visible);
        return;
    }

    btn.classList.add('diff-expanded');
    viewer = document.createElement('div');
    viewer.className = 'diff-viewer';
    viewer.innerHTML = `<div class="diff-loading"><span class="diff-spinner"></span>Loading diff&hellip;</div>`;
    item.appendChild(viewer);

    try {
        const resp = await fetch(`/api/catalog/templates/${encodeURIComponent(templateId)}/diff?from_version=${fromVersion}&to_version=${toVersion}`);
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({ detail: resp.statusText }));
            viewer.innerHTML = `<div class="diff-error">Failed to load diff: ${escapeHtml(err.detail || 'Unknown error')}</div>`;
            return;
        }

        const data = await resp.json();
        _renderDiffViewer(viewer, data);
    } catch (e) {
        viewer.innerHTML = `<div class="diff-error">Failed to load diff: ${escapeHtml(e.message)}</div>`;
    }
}

/* ── ADO Pipeline State Machine ── */
function _adoHandleEvent(container, event, state) {
    if (!state) state = { jobs: [], elements: {}, stepLogs: {}, expanded: {} };

    switch (event.type) {
        case 'pipeline_init': {
            state.jobs = event.jobs || [];
            container.innerHTML = _adoRenderPipeline(state.jobs, event);
            // Cache DOM refs
            for (const job of state.jobs) {
                state.elements[job.id] = document.getElementById(`ado-job-${job.id}`);
                for (const step of job.steps) {
                    state.elements[step.id] = document.getElementById(`ado-step-${step.id}`);
                    state.stepLogs[step.id] = [];
                }
            }
            break;
        }

        case 'step_start': {
            const el = state.elements[event.step_id];
            if (!el) break;
            el.classList.remove('ado-step-pending');
            el.classList.add('ado-step-running');
            el.querySelector('.ado-step-icon').innerHTML = _adoStepIcon('running');
            // Also mark job as running if not already
            const jobEl = state.elements[event.job_id];
            if (jobEl && !jobEl.classList.contains('ado-job-running')) {
                jobEl.classList.remove('ado-job-pending');
                jobEl.classList.add('ado-job-running');
                const badge = jobEl.querySelector('.ado-job-status-badge');
                if (badge) { badge.textContent = 'Running'; badge.className = 'ado-job-status-badge ado-badge-running'; }
            }
            // Auto-expand the running step
            _adoExpandStep(state, event.step_id, true);
            break;
        }

        case 'step_log': {
            if (!state.stepLogs[event.step_id]) state.stepLogs[event.step_id] = [];
            state.stepLogs[event.step_id].push(event);
            const el = state.elements[event.step_id];
            if (!el) break;
            const logArea = el.querySelector('.ado-step-logs');
            if (!logArea) break;
            const level = event.level || 'info';
            const ts = event.timestamp ? new Date(event.timestamp).toLocaleTimeString() : '';
            logArea.innerHTML += `<div class="ado-log ado-log-${level}"><span class="ado-log-ts">${ts}</span>${escapeHtml(event.message)}</div>`;
            logArea.scrollTop = logArea.scrollHeight;
            // Update step counter badge
            const countBadge = el.querySelector('.ado-step-log-count');
            if (countBadge) countBadge.textContent = state.stepLogs[event.step_id].length;
            break;
        }

        case 'step_end': {
            const el = state.elements[event.step_id];
            if (!el) break;
            el.classList.remove('ado-step-running', 'ado-step-pending');
            el.classList.add(`ado-step-${event.status}`);
            el.querySelector('.ado-step-icon').innerHTML = _adoStepIcon(event.status);
            // Show duration
            const dur = el.querySelector('.ado-step-duration');
            if (dur && event.duration_ms != null) {
                dur.textContent = _adoFormatDuration(event.duration_ms);
                dur.classList.remove('hidden');
            }
            // Collapse completed step, unless it failed
            if (event.status === 'success' || event.status === 'warning') {
                _adoExpandStep(state, event.step_id, false);
            }
            break;
        }

        case 'job_end': {
            const el = state.elements[event.job_id];
            if (!el) break;
            el.classList.remove('ado-job-running', 'ado-job-pending');
            el.classList.add(`ado-job-${event.status}`);
            const badge = el.querySelector('.ado-job-status-badge');
            if (badge) {
                badge.textContent = event.status === 'success' ? 'Succeeded' : event.status === 'warning' ? 'Completed' : 'Failed';
                badge.className = `ado-job-status-badge ado-badge-${event.status}`;
            }
            // Duration
            const dur = el.querySelector('.ado-job-duration');
            if (dur && event.duration_ms) {
                dur.textContent = _adoFormatDuration(event.duration_ms);
                dur.classList.remove('hidden');
            }
            // Result summary
            if (event.status === 'success' && event.result) {
                const r = event.result;
                const summary = el.querySelector('.ado-job-result');
                if (summary) {
                    let html = `<span class="ado-job-result-ver">v${escapeHtml(r.new_semver || '')}</span>
                        <span class="ado-job-result-changes">${r.changes_made?.length || 0} change(s) applied</span>`;
                    // Deploy proof
                    const dp = r.deploy_proof;
                    if (dp && !dp.error) {
                        html += `
                        <div class="ado-deploy-proof">
                            <div class="ado-proof-title">🔒 Deployment Validation Proof</div>
                            <div class="ado-proof-grid">
                                <div class="ado-proof-item"><span class="ado-proof-label">Subscription</span><span class="ado-proof-value">${escapeHtml(dp.subscription_id || '')}</span></div>
                                <div class="ado-proof-item"><span class="ado-proof-label">Resource Group</span><span class="ado-proof-value">${escapeHtml(dp.resource_group || '')}</span></div>
                                <div class="ado-proof-item"><span class="ado-proof-label">Deployment</span><span class="ado-proof-value">${escapeHtml(dp.deployment_name || '')}</span></div>
                                <div class="ado-proof-item"><span class="ado-proof-label">Region</span><span class="ado-proof-value">${escapeHtml(dp.region || '')}</span></div>
                                <div class="ado-proof-item"><span class="ado-proof-label">Started</span><span class="ado-proof-value">${dp.started_at ? new Date(dp.started_at).toLocaleString() : '—'}</span></div>
                                <div class="ado-proof-item"><span class="ado-proof-label">Completed</span><span class="ado-proof-value">${dp.completed_at ? new Date(dp.completed_at).toLocaleString() : '—'}</span></div>
                                <div class="ado-proof-item"><span class="ado-proof-label">Cleanup</span><span class="ado-proof-value">${dp.cleanup_initiated_at ? new Date(dp.cleanup_initiated_at).toLocaleString() : '—'}</span></div>
                                <div class="ado-proof-item"><span class="ado-proof-label">Status</span><span class="ado-proof-value ado-proof-status-${dp.what_if_status === 'success' ? 'ok' : 'warn'}">${escapeHtml(dp.what_if_status || '?')}</span></div>
                                <div class="ado-proof-item"><span class="ado-proof-label">Resources</span><span class="ado-proof-value">${dp.total_changes || 0} operation(s)</span></div>
                            </div>
                            ${dp.change_counts ? `<div class="ado-proof-counts">${Object.entries(dp.change_counts).map(([k,v]) => `<span class="ado-proof-count ado-proof-count-${k.toLowerCase()}">${k}: ${v}</span>`).join('')}</div>` : ''}
                        </div>`;
                    }
                    summary.innerHTML = html;
                    summary.classList.remove('hidden');
                }
            }
            if (event.status === 'failed' && event.error) {
                const summary = el.querySelector('.ado-job-result');
                if (summary) {
                    summary.innerHTML = `<span class="ado-job-result-error">❌ ${escapeHtml(event.error)}</span>`;
                    summary.classList.remove('hidden');
                }
            }
            break;
        }

        case 'pipeline_done': {
            const allOk = event.all_success;
            const dur = event.duration_ms ? ` in ${_adoFormatDuration(event.duration_ms)}` : '';
            const results = event.results || [];
            const successCount = results.filter(r => r.success).length;
            const failCount = results.length - successCount;

            const banner = document.createElement('div');
            banner.className = `ado-pipeline-done ${allOk ? 'ado-done-ok' : 'ado-done-partial'}`;

            // Build per-result cards
            let resultsHtml = '';
            for (const r of results) {
                const ok = r.success;
                const icon = ok ? '✅' : '❌';
                const name = r.template_name || r.template_id || 'Unknown';

                // Changes list
                let changesHtml = '';
                if (r.changes_made && r.changes_made.length > 0) {
                    changesHtml = `<div class="ado-report-changes">
                        <div class="ado-report-changes-title">Changes Applied</div>
                        <ul class="ado-report-changes-list">
                            ${r.changes_made.map(c => `<li>
                                <span class="ado-report-change-step">Step ${c.step || '?'}</span>
                                <span class="ado-report-change-desc">${escapeHtml(c.description || '')}</span>
                                ${c.resource ? `<span class="ado-report-change-resource">${escapeHtml(c.resource)}</span>` : ''}
                            </li>`).join('')}
                        </ul>
                    </div>`;
                }

                // Deploy proof
                let proofHtml = '';
                const dp = r.deploy_proof;
                if (dp && !dp.error) {
                    proofHtml = `<div class="ado-deploy-proof">
                        <div class="ado-proof-title">🔒 Deployment Validation Proof</div>
                        <div class="ado-proof-grid">
                            <div class="ado-proof-item"><span class="ado-proof-label">Subscription</span><span class="ado-proof-value">${escapeHtml(dp.subscription_id || '')}</span></div>
                            <div class="ado-proof-item"><span class="ado-proof-label">Resource Group</span><span class="ado-proof-value">${escapeHtml(dp.resource_group || '')}</span></div>
                            <div class="ado-proof-item"><span class="ado-proof-label">Deployment</span><span class="ado-proof-value">${escapeHtml(dp.deployment_name || '')}</span></div>
                            <div class="ado-proof-item"><span class="ado-proof-label">Region</span><span class="ado-proof-value">${escapeHtml(dp.region || '')}</span></div>
                            <div class="ado-proof-item"><span class="ado-proof-label">Started</span><span class="ado-proof-value">${dp.started_at ? new Date(dp.started_at).toLocaleString() : '—'}</span></div>
                            <div class="ado-proof-item"><span class="ado-proof-label">Completed</span><span class="ado-proof-value">${dp.completed_at ? new Date(dp.completed_at).toLocaleString() : '—'}</span></div>
                            <div class="ado-proof-item"><span class="ado-proof-label">Cleanup</span><span class="ado-proof-value">${dp.cleanup_initiated_at ? new Date(dp.cleanup_initiated_at).toLocaleString() : '—'}</span></div>
                            <div class="ado-proof-item"><span class="ado-proof-label">Status</span><span class="ado-proof-value ado-proof-status-${dp.what_if_status === 'success' ? 'ok' : 'warn'}">${escapeHtml(dp.what_if_status || '?')}</span></div>
                            <div class="ado-proof-item"><span class="ado-proof-label">Resources</span><span class="ado-proof-value">${dp.total_changes || 0} operation(s)</span></div>
                        </div>
                        ${dp.change_counts ? `<div class="ado-proof-counts">${Object.entries(dp.change_counts).map(([k,v]) => `<span class="ado-proof-count ado-proof-count-${k.toLowerCase()}">${k}: ${v}</span>`).join('')}</div>` : ''}
                    </div>`;
                } else if (dp && dp.error) {
                    proofHtml = `<div class="ado-deploy-proof ado-proof-error">
                        <div class="ado-proof-title">⚠️ Deployment Validation</div>
                        <div class="ado-proof-error-msg">${escapeHtml(dp.error)}</div>
                    </div>`;
                }

                // Changelog
                let changelogHtml = '';
                if (r.changelog) {
                    changelogHtml = `<div class="ado-report-changelog">
                        <div class="ado-report-changelog-title">Changelog</div>
                        <div class="ado-report-changelog-text">${escapeHtml(r.changelog)}</div>
                    </div>`;
                }

                // Diff toggle button — only if we have both version numbers
                let diffBtnHtml = '';
                if (ok && r.old_version && r.new_version && r.old_version !== r.new_version) {
                    const tid = escapeHtml(r.template_id || '');
                    diffBtnHtml = `<button class="diff-toggle-btn"
                        onclick="toggleDiffViewer(this, '${tid}', ${r.old_version}, ${r.new_version})">
                        <span class="diff-toggle-icon">▶</span>
                        View Diff (v${escapeHtml(r.old_semver || String(r.old_version))} → v${escapeHtml(r.new_semver || String(r.new_version))})
                    </button>`;
                }

                // Compliance verification status
                let verifyHtml = '';
                if (ok && r.verify_iterations) {
                    if (r.verify_clean) {
                        verifyHtml = `<div class="ado-verify-status ado-verify-clean">
                            <span class="ado-verify-icon">🛡️</span>
                            <span>Compliance verified clean${r.verify_iterations > 1 ? ` (${r.verify_iterations} iteration${r.verify_iterations > 1 ? 's' : ''})` : ''}</span>
                        </div>`;
                    } else {
                        verifyHtml = `<div class="ado-verify-status ado-verify-remaining">
                            <span class="ado-verify-icon">⚠️</span>
                            <span>${r.remaining_violations || '?'} violation(s) remain after ${r.verify_iterations} iteration(s) — manual review needed</span>
                        </div>`;
                    }
                }

                resultsHtml += `
                <div class="ado-report-card ${ok ? 'ado-report-card-ok' : 'ado-report-card-fail'}">
                    <div class="ado-report-card-header">
                        <span class="ado-report-card-icon">${icon}</span>
                        <span class="ado-report-card-name">${escapeHtml(name)}</span>
                        ${r.new_semver ? `<span class="ado-report-card-ver">v${escapeHtml(r.new_semver)}</span>` : ''}
                    </div>
                    ${verifyHtml}
                    ${changesHtml}
                    ${diffBtnHtml}
                    ${proofHtml}
                    ${changelogHtml}
                </div>`;
            }

            banner.innerHTML = `
                <div class="ado-done-header">
                    <span class="ado-done-icon">${allOk ? '✅' : '⚠️'}</span>
                    <span class="ado-done-title">${allOk ? 'Pipeline succeeded' : 'Pipeline completed with errors'}${dur}</span>
                </div>
                <div class="ado-report-stats">
                    <span class="ado-report-stat ado-report-stat-total">${results.length} template(s)</span>
                    ${successCount > 0 ? `<span class="ado-report-stat ado-report-stat-ok">${successCount} succeeded</span>` : ''}
                    ${failCount > 0 ? `<span class="ado-report-stat ado-report-stat-fail">${failCount} failed</span>` : ''}
                </div>
                ${resultsHtml ? `<div class="ado-report-results">${resultsHtml}</div>` : ''}
                <div class="ado-done-actions">
                    <button class="btn btn-sm scan-rescan-btn" onclick="runComplianceScan('${escapeHtml(event.template_id)}')">Re-scan compliance</button>
                </div>
            `;
            container.appendChild(banner);

            // Move the pipeline report out of the scan-results area so it
            // survives the compliance re-scan that replaces tmpl-scan-results.
            if (event.template_id) {
                const pipelineEl = document.getElementById('ado-pipeline');
                const scanResults = document.getElementById('tmpl-scan-results');
                if (pipelineEl && scanResults) {
                    // Detach pipeline from inside the scan area and
                    // insert it right before the scan results container.
                    scanResults.parentNode.insertBefore(pipelineEl, scanResults);
                }

                if (!_autoRemediating) {
                    // Only auto-scan/refresh when NOT in auto-remediation mode
                    // (the auto-loop handles its own scan cycle)
                    loadAllData().then(() => {
                        const updatedTmpl = allTemplates.find(t => t.id === event.template_id);
                        if (updatedTmpl) {
                            _loadTemplateVersionHistory(event.template_id);
                        }
                    });
                    // Re-scan compliance with delay to let publish settle
                    setTimeout(() => runComplianceScan(event.template_id), 800);
                }
            }
            break;
        }
    }
    return state;
}

/* ── ADO Pipeline Render ── */
function _adoRenderPipeline(jobs, initEvent) {
    const parallel = jobs.length > 1;
    const title = initEvent.template_name || initEvent.template_id || 'Pipeline';

    let html = `
    <div class="ado-pipeline-header">
        <div class="ado-pipeline-title">
            <span class="ado-pipeline-icon">⚙️</span>
            <span>Compliance Remediation</span>
            <span class="ado-pipeline-name">${escapeHtml(title)}</span>
        </div>
        <div class="ado-pipeline-meta">
            ${parallel ? `<span class="ado-parallel-badge">⚡ ${jobs.length} parallel jobs</span>` : `<span class="ado-parallel-badge">1 job</span>`}
        </div>
    </div>
    <div class="ado-jobs-container ${parallel ? 'ado-jobs-parallel' : 'ado-jobs-single'}">`;

    for (const job of jobs) {
        html += `
        <div class="ado-job ado-job-pending" id="ado-job-${job.id}">
            <div class="ado-job-header">
                <div class="ado-job-title">
                    <span class="ado-job-icon">📦</span>
                    <span class="ado-job-name">${escapeHtml(job.label)}</span>
                </div>
                <div class="ado-job-badges">
                    <span class="ado-job-status-badge ado-badge-pending">Pending</span>
                    <span class="ado-job-duration hidden"></span>
                </div>
            </div>
            <div class="ado-job-version-bar">
                <span class="ado-ver-from">v${escapeHtml(job.current_semver || '?')}</span>
                <span class="ado-ver-arrow">→</span>
                <span class="ado-ver-to">v${escapeHtml(job.projected_semver || '?')}</span>
                <span class="ado-ver-type">${escapeHtml(job.change_type || 'patch')}</span>
                <span class="ado-ver-fixes">${job.step_count} fix${job.step_count !== 1 ? 'es' : ''}</span>
                ${job.upgrade_available ? '<span class="ado-upgrade-badge" title="Newer compliant version available — AI remediation skipped">⬆ Upgrade</span>' : ''}
                ${job.upgrade_action === 'ai_fix_latest' ? '<span class="ado-upgrade-badge ado-upgrade-pull" title="Latest version pulled for AI remediation">⬇ Latest</span>' : ''}
            </div>
            <div class="ado-steps-timeline">`;

        for (let s = 0; s < job.steps.length; s++) {
            const step = job.steps[s];
            const isLast = s === job.steps.length - 1;
            html += `
                <div class="ado-step ado-step-pending" id="ado-step-${step.id}">
                    <div class="ado-step-connector ${isLast ? 'ado-step-connector-last' : ''}">
                        <div class="ado-step-line-top ${s === 0 ? 'hidden' : ''}"></div>
                        <div class="ado-step-icon">${_adoStepIcon('pending')}</div>
                        <div class="ado-step-line-bottom ${isLast ? 'hidden' : ''}"></div>
                    </div>
                    <div class="ado-step-content">
                        <div class="ado-step-header" onclick="_adoToggleStep(this)">
                            <span class="ado-step-label">${escapeHtml(step.label)}</span>
                            <span class="ado-step-detail">${escapeHtml(step.detail || '')}</span>
                            <span class="ado-step-log-count hidden">0</span>
                            <span class="ado-step-duration hidden"></span>
                            <span class="ado-step-chevron">▸</span>
                        </div>
                        <div class="ado-step-logs hidden"></div>
                    </div>
                </div>`;
        }

        html += `
            </div>
            <div class="ado-job-result hidden"></div>
        </div>`;
    }

    html += `</div>`;
    return html;
}

function _adoStepIcon(status) {
    switch (status) {
        case 'pending':  return '<span class="ado-icon ado-icon-pending">○</span>';
        case 'running':  return '<span class="ado-icon ado-icon-running"><span class="ado-spinner"></span></span>';
        case 'success':  return '<span class="ado-icon ado-icon-success">✓</span>';
        case 'warning':  return '<span class="ado-icon ado-icon-warning">⚠</span>';
        case 'failed':   return '<span class="ado-icon ado-icon-failed">✗</span>';
        case 'skipped':  return '<span class="ado-icon ado-icon-skipped">⊘</span>';
        default:         return '<span class="ado-icon ado-icon-pending">○</span>';
    }
}

function _adoFormatDuration(ms) {
    if (ms < 1000) return `${ms}ms`;
    if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
    const mins = Math.floor(ms / 60000);
    const secs = Math.round((ms % 60000) / 1000);
    return `${mins}m ${secs}s`;
}

function _adoToggleStep(headerEl) {
    const stepEl = headerEl.closest('.ado-step');
    if (!stepEl) return;
    const logs = stepEl.querySelector('.ado-step-logs');
    const chevron = stepEl.querySelector('.ado-step-chevron');
    if (!logs) return;
    const isExpanded = !logs.classList.contains('hidden');
    logs.classList.toggle('hidden', isExpanded);
    if (chevron) chevron.textContent = isExpanded ? '▸' : '▾';
    stepEl.classList.toggle('ado-step-expanded', !isExpanded);
}

function _adoExpandStep(state, stepId, expand) {
    const el = state.elements[stepId];
    if (!el) return;
    const logs = el.querySelector('.ado-step-logs');
    const chevron = el.querySelector('.ado-step-chevron');
    const countBadge = el.querySelector('.ado-step-log-count');
    if (!logs) return;
    logs.classList.toggle('hidden', !expand);
    if (chevron) chevron.textContent = expand ? '▾' : '▸';
    el.classList.toggle('ado-step-expanded', expand);
    if (expand && countBadge) countBadge.classList.remove('hidden');
}

function _renderRemediationResults(data) {
    const results = data.results || [];
    const allOk = data.all_success;

    let html = `
    <div class="remed-results ${allOk ? 'remed-results-ok' : 'remed-results-partial'}">
        <div class="remed-results-header">
            <span class="remed-results-icon">${allOk ? '✅' : '⚠️'}</span>
            <h4>${allOk ? 'All Templates Updated' : 'Partial Success'}</h4>
        </div>
        <div class="remed-results-list">`;

    for (const r of results) {
        if (r.success) {
            html += `
            <div class="remed-result remed-result-ok">
                <span class="remed-result-icon">✅</span>
                <div class="remed-result-body">
                    <div class="remed-result-name">${escapeHtml(r.template_name || r.template_id)}</div>
                    <div class="remed-result-detail">
                        New version <strong>v${r.new_semver || r.new_version + '.0.0'}</strong>
                    </div>
                    <div class="remed-result-changelog">${escapeHtml(r.changelog || '')}</div>
                </div>
            </div>`;
        } else {
            html += `
            <div class="remed-result remed-result-fail">
                <span class="remed-result-icon">❌</span>
                <div class="remed-result-body">
                    <div class="remed-result-name">${escapeHtml(r.template_name || r.template_id)}</div>
                    <div class="remed-result-error">${escapeHtml(r.error || 'Unknown error')}</div>
                </div>
            </div>`;
        }
    }

    html += `
        </div>
        <button class="btn btn-sm scan-rescan-btn" onclick="runComplianceScan('${escapeHtml(data.template_id)}')">🔄 Re-scan for Compliance</button>
    </div>`;

    return html;
}

/** Full validation pipeline: structural tests → ARM validation (auto-chains) */
async function runFullValidation(templateId, skipTests = false) {
    if (!skipTests) {
        // Step 1: Run structural tests
        showToast('� Let me check the structure first…', 'info');
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
                showToast(`${results.failed} of ${results.total} structural checks need attention`, 'info');
                await loadAllData();
                showTemplateDetail(templateId);
                return;
            }
            showToast(`Structure looks solid — all ${results.total} checks passed`, 'info');
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
        resultsDiv.innerHTML = '<div class="compose-loading">🧪 Working on it… This usually takes 1-5 minutes.</div>';
    }

    showToast('🧪 Starting validation — Copilot SDK will deploy the template and handle any issues', 'info');

    // Initialize tracker
    const tracker = {
        running: true,
        events: [],
        finalEvent: null,
        abortController: new AbortController(),
    };
    _activeTemplateValidations[templateId] = tracker;

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
            signal: tracker.abortController.signal,
        });

        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Validation failed');
        }

        // Read NDJSON stream — render to current resultsDiv if visible
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

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
                    tracker.events.push(event);
                    tracker.finalEvent = event;
                    // Render to the live resultsDiv if still in DOM
                    const liveDiv = document.getElementById('tmpl-validate-results');
                    if (liveDiv) {
                        _renderDeployProgress(liveDiv, event, 'validate');
                    }
                } catch (e) { /* skip malformed */ }
            }
        }

        // Process final buffer
        if (buffer.trim()) {
            try {
                const event = JSON.parse(buffer);
                tracker.events.push(event);
                tracker.finalEvent = event;
                const liveDiv = document.getElementById('tmpl-validate-results');
                if (liveDiv) {
                    _renderDeployProgress(liveDiv, event, 'validate');
                }
            } catch (e) { /* skip */ }
        }

        if (tracker.finalEvent && tracker.finalEvent.status === 'succeeded') {
            const resolved = tracker.finalEvent.issues_resolved || 0;
            const healMsg = resolved > 0 ? ` Resolved ${resolved} issue${resolved !== 1 ? 's' : ''} along the way.` : '';
            showToast(`Template verified.${healMsg} Ready to publish.`, 'info');
        } else if (tracker.finalEvent && tracker.finalEvent.status === 'failed') {
            showToast(`Validation complete — check the log for details.`, 'info');
        }

        // Refresh and reopen detail
        await loadAllData();
        showTemplateDetail(templateId);

    } catch (err) {
        if (err.name === 'AbortError') return; // user navigated away intentionally
        showToast(`Validation issue: ${err.message}`, 'info');
        const liveDiv = document.getElementById('tmpl-validate-results');
        if (liveDiv) {
            liveDiv.innerHTML = `<div class="tmpl-deploy-diag-msg">${escapeHtml(err.message)}</div>`;
        }
    } finally {
        tracker.running = false;
        const liveBtn = document.getElementById('tmpl-validate-btn');
        if (liveBtn) {
            liveBtn.disabled = false;
            liveBtn.innerHTML = '🧪 Run Validation';
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

/** Recompose a composite template from its latest service templates */
async function recomposeBlueprint(templateId) {
    if (!confirm('Recompose this template from the latest service templates?\n\nThis pulls the current version of each underlying service template, re-merges them, and creates a new major version.')) return;

    showToast('🔄 Pulling latest service template versions…', 'info');

    try {
        const res = await fetch(`/api/catalog/templates/${encodeURIComponent(templateId)}/recompose`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
        });
        const data = await res.json();

        if (!res.ok) {
            showToast(`Recompose: ${data.detail || 'Could not proceed'}`, 'info');
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
                const svVer = sv.semver || (sv.version ? `${sv.version}.0.0` : 'latest');
                detail += `  • ${sv.name || sv.service_id} (${svVer}, ${sv.source})\n`;
            }
        }

        // Append test results from auto-test
        const tr = data.test_results;
        if (tr && tr.all_passed) {
            detail += `\n✅ All ${tr.total} structural tests passed`;
        } else if (tr) {
            detail += `\n⚠️ ${tr.failed}/${tr.total} structural tests need attention`;
        }

        showToast(detail, (tr && tr.all_passed) ? 'success' : 'info', 8000);

        // Refresh the detail view
        await loadAllData();
        showTemplateDetail(templateId);
    } catch (err) {
        showToast(`Recompose: ${err.message}`, 'info');
    }
}

/** Show inline policy exception request form */
function _showPolicyExceptionForm(templateId, userRequest, policyRules, container) {
    const formId = 'policy-exception-form-' + Date.now();
    const formHtml = `
        <div class="policy-exception-form" id="${formId}">
            <div class="policy-exception-header">⚠️ Request Policy Exception</div>
            <div class="policy-exception-desc">
                Submit a formal request to the platform team to grant an exception for the blocked policies.
                Provide a business justification explaining why this exception is needed.
            </div>
            <div class="policy-exception-rules">
                <strong>Policies to challenge:</strong> ${policyRules.map(r => `<span class="policy-rule-chip">${escapeHtml(r)}</span>`).join(' ')}
            </div>
            <textarea class="form-control policy-exception-textarea" id="${formId}-justification"
                placeholder="Business justification — explain why this policy exception is needed for your project. Include impact if denied, security mitigations you'll implement, and timeline."
                rows="4"></textarea>
            <div class="policy-exception-actions">
                <button class="btn btn-sm btn-danger" id="${formId}-submit">📨 Submit Exception Request</button>
                <button class="btn btn-sm btn-secondary" id="${formId}-cancel">Cancel</button>
            </div>
        </div>`;

    // Insert form after the policy card
    const formDiv = document.createElement('div');
    formDiv.innerHTML = formHtml;
    container.parentNode.insertBefore(formDiv, container.nextSibling);

    document.getElementById(`${formId}-cancel`).onclick = () => formDiv.remove();
    document.getElementById(`${formId}-submit`).onclick = async () => {
        const justification = document.getElementById(`${formId}-justification`).value.trim();
        if (!justification) {
            showToast('Business justification is required', 'warning');
            return;
        }
        const submitBtn = document.getElementById(`${formId}-submit`);
        submitBtn.disabled = true;
        submitBtn.textContent = '⏳ Submitting…';
        try {
            const res = await fetch('/api/policy-exception-requests', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    user_request: userRequest,
                    policy_rules: policyRules,
                    justification: justification,
                    template_id: templateId || '',
                }),
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || 'Failed to submit');
            formDiv.innerHTML = `
                <div class="policy-exception-submitted">
                    <div class="policy-exception-submitted-icon">📨</div>
                    <div class="policy-exception-submitted-title">Exception Request Submitted</div>
                    <div class="policy-exception-submitted-id">${escapeHtml(data.request_id)}</div>
                    <div class="policy-exception-submitted-msg">${escapeHtml(data.message)}</div>
                </div>`;
            showToast(`Policy exception request ${data.request_id} submitted`, 'info');
            // Refresh approval tracker
            loadAllData();
        } catch (err) {
            showToast(`Failed: ${err.message}`, 'error');
            submitBtn.disabled = false;
            submitBtn.textContent = '📨 Submit Exception Request';
        }
    };
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
    btn.textContent = '⏳ Copilot SDK checking policies…';
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
            const issueRules = (policyData.issues || []).map(i => i.rule).filter(Boolean);
            const hasAlternative = policyData.compliant_alternative;
            const hasRationale = policyData.policy_rationale;

            policyDiv.className = 'tmpl-revision-policy tmpl-policy-block';
            policyDiv.innerHTML = `
                <div class="tmpl-policy-header">🛡️ Policy Guidance</div>
                <div class="tmpl-policy-summary">${escapeHtml(policyData.summary)}</div>
                ${policyData.issues?.length ? `<ul class="tmpl-policy-issues">
                    ${policyData.issues.map(i => `<li class="tmpl-policy-issue-${i.severity}">
                        <strong>${escapeHtml(i.rule)}</strong>: ${escapeHtml(i.message)}
                    </li>`).join('')}
                </ul>` : ''}
                ${hasRationale ? `<div class="tmpl-policy-rationale">
                    <strong>Why this policy exists:</strong> ${escapeHtml(policyData.policy_rationale)}
                </div>` : ''}
                ${hasAlternative ? `<div class="tmpl-policy-alternative">
                    <div class="tmpl-policy-alternative-header">✅ What you CAN do instead</div>
                    <div class="tmpl-policy-alternative-body">${escapeHtml(policyData.compliant_alternative)}</div>
                </div>` : ''}
                <div class="tmpl-policy-actions">
                    ${hasAlternative ? `<button class="btn btn-primary btn-sm" id="policy-use-alternative-btn">
                        ✅ Apply Compliant Alternative
                    </button>` : ''}
                    <button class="btn btn-sm btn-secondary" id="policy-discuss-btn">
                        💬 Discuss Options
                    </button>
                    <button class="btn btn-sm btn-danger" id="policy-challenge-btn">
                        ⚠️ Request Policy Exception
                    </button>
                </div>`;

            // Wire up buttons
            const altBtn = document.getElementById('policy-use-alternative-btn');
            if (altBtn && hasAlternative) {
                altBtn.onclick = () => {
                    textarea.value = policyData.compliant_alternative;
                    policyDiv.style.display = 'none';
                    showToast('Alternative applied — click Request Revision to proceed', 'info');
                };
            }
            const discussBtn = document.getElementById('policy-discuss-btn');
            if (discussBtn) {
                const issuesSummary = (policyData.issues || []).map(i => '- ' + i.rule + ': ' + i.message).join('\\n');
                const chatPrompt = 'I tried to modify a template with this request:\\n\\n"' + prompt + '"\\n\\nBut it was blocked by organizational policy:\\n' + issuesSummary + '\\n\\nPlease suggest a compliant configuration that satisfies my requirements while meeting all policy constraints.';
                discussBtn.onclick = () => {
                    closeModal('modal-template-onboard');
                    navigateToChat(chatPrompt);
                };
            }
            const challengeBtn = document.getElementById('policy-challenge-btn');
            if (challengeBtn) {
                challengeBtn.onclick = () => _showPolicyExceptionForm(templateId, prompt, issueRules, policyDiv);
            }
            btn.disabled = false;
            btn.textContent = '✏️ Request Revision';
            return;
        } else if (policyData.verdict === 'warning') {
            policyDiv.className = 'tmpl-revision-policy tmpl-policy-warning';
            policyDiv.innerHTML = `
                <div class="tmpl-policy-header">📋 Policy Notes</div>
                <div class="tmpl-policy-summary">${escapeHtml(policyData.summary)}</div>
                ${policyData.issues?.length ? `<ul class="tmpl-policy-issues">
                    ${policyData.issues.map(i => `<li class="tmpl-policy-issue-${i.severity}">
                        <strong>${escapeHtml(i.rule)}</strong>: ${escapeHtml(i.message)}
                    </li>`).join('')}
                </ul>` : ''}
                <div class="tmpl-policy-hint">Proceeding with revision…</div>`;
        } else {
            policyDiv.className = 'tmpl-revision-policy tmpl-policy-pass';
            policyDiv.innerHTML = `<div class="tmpl-policy-header">📋 Policy Check Complete</div>
                <div class="tmpl-policy-summary">${escapeHtml(policyData.summary)}</div>`;
        }

        // ── Step 2: Submit revision (streaming) ──────────────
        btn.textContent = '⏳ Copilot SDK revising template…';
        resultDiv.style.display = 'block';
        resultDiv.innerHTML = '';  // will be populated by log stream renderer

        const revRes = await fetch(`/api/catalog/templates/${encodeURIComponent(templateId)}/revise`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ prompt, skip_policy_check: true }),
        });

        if (!revRes.ok) {
            const errData = await revRes.json().catch(() => ({ detail: revRes.statusText }));
            resultDiv.innerHTML = `<div class="tmpl-revision-error">${escapeHtml(errData.detail || errData.message || 'Revision could not proceed')}</div>`;
            return;
        }

        // Consume the NDJSON stream with live rendering
        const finalEvent = await consumeLogStream(revRes, resultDiv);

        if (!finalEvent) {
            resultDiv.innerHTML += `<div class="tmpl-revision-error">Stream ended without a result</div>`;
            return;
        }

        const revData = finalEvent.detail || {};
        const revStatus = revData.status || finalEvent.status;

        if (revStatus === 'blocked') {
            // Already shown in log stream
            return;
        }

        if (revStatus === 'no_changes') {
            resultDiv.innerHTML += `
                <div class="logstream-result logstream-result-info">
                    <div class="tmpl-revision-hint">${escapeHtml(revData.message || finalEvent.message || 'No changes needed')}</div>
                </div>`;
            return;
        }

        if (revStatus === 'error' || finalEvent.type === 'error') {
            resultDiv.innerHTML += `
                <div class="logstream-result logstream-result-error">
                    ${escapeHtml(finalEvent.message || 'Revision could not proceed')}
                </div>`;
            return;
        }

        // Success — show summary and trigger validation
        const ver = revData.version || {};
        let actionsHtml = '';
        if (revData.actions_taken?.length) {
            actionsHtml = '<div class="tmpl-revision-actions"><strong>Changes made:</strong><ul>' +
                revData.actions_taken.map(a => {
                    const icon = a.action === 'auto_onboarded' ? '🔧' :
                                 a.action === 'added_from_catalog' ? '📦' :
                                 a.action === 'code_edit' ? '✏️' : '●';
                    return `<li>${icon} <strong>${escapeHtml(a.service_id.split('/').pop())}</strong> — ${escapeHtml(a.detail)}</li>`;
                }).join('') + '</ul></div>';
        }

        // Append the final summary to the log stream output
        const summaryEl = document.createElement('div');
        summaryEl.className = 'logstream-result logstream-result-success';
        summaryEl.innerHTML = `
            <div class="tmpl-revision-analysis">${escapeHtml(revData.analysis || '')}</div>
            ${actionsHtml}
            <div class="tmpl-revision-summary">
                Template revised → <strong>v${ver.semver || '?'}</strong>:
                <strong>${revData.resource_count || '?'}</strong> resources,
                <strong>${revData.parameter_count || '?'}</strong> params from
                <strong>${revData.services?.length || '?'}</strong> services.
            </div>`;
        resultDiv.appendChild(summaryEl);

        textarea.value = '';
        showToast(`Revised → v${ver.semver || '?'} — starting validation…`, 'info');

        // Run validation inline — progress renders right here, no context switch
        const validationContainer = document.createElement('div');
        validationContainer.className = 'tmpl-revision-validation-inline';
        resultDiv.appendChild(validationContainer);
        _runPostRevisionValidation(templateId, validationContainer);

    } catch (err) {
        resultDiv.style.display = 'block';
        resultDiv.innerHTML += `<div class="tmpl-revision-error">${escapeHtml(err.message)}</div>`;
        showToast(`Revision: ${err.message}`, 'info');
    } finally {
        btn.disabled = false;
        btn.textContent = '✏️ Request Revision';
    }
}

/**
 * Run validation inline after a template revision.
 * Renders progress directly into the revision result area — no context switch.
 */
async function _runPostRevisionValidation(templateId, container) {
    // Refresh catalog to pick up the new version
    await loadCatalog();

    // ── Step 1: Structural tests ──
    const testStatus = document.createElement('div');
    testStatus.className = 'tmpl-rv-status';
    testStatus.innerHTML = '<span class="vf-badge-pulse"></span> Running structural checks…';
    container.appendChild(testStatus);

    try {
        const testRes = await fetch(`/api/catalog/templates/${encodeURIComponent(templateId)}/test`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({}),
        });
        if (!testRes.ok) {
            const err = await testRes.json();
            throw new Error(err.detail || 'Test failed');
        }
        const testData = await testRes.json();
        const results = testData.results || {};
        if (!results.all_passed) {
            testStatus.innerHTML = `⚠️ ${results.failed} of ${results.total} structural checks need attention`;
            testStatus.classList.add('tmpl-rv-status-warn');
            await loadAllData();
            showTemplateDetail(templateId);
            return;
        }
        testStatus.innerHTML = `✅ All ${results.total} structural checks passed — deploying to Azure…`;
        testStatus.classList.add('tmpl-rv-status-ok');
    } catch (err) {
        testStatus.innerHTML = `❌ Structure check error: ${escapeHtml(err.message)}`;
        testStatus.classList.add('tmpl-rv-status-error');
        return;
    }

    // ── Step 2: Stream ARM validation inline ──
    const progressDiv = document.createElement('div');
    progressDiv.className = 'tmpl-rv-progress';
    container.appendChild(progressDiv);

    const tracker = {
        running: true,
        events: [],
        finalEvent: null,
        abortController: new AbortController(),
    };
    _activeTemplateValidations[templateId] = tracker;

    try {
        const res = await fetch(`/api/catalog/templates/${encodeURIComponent(templateId)}/validate`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ parameters: {}, region: 'eastus2' }),
            signal: tracker.abortController.signal,
        });

        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Validation failed');
        }

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });

            const lines = buffer.split('\n');
            buffer = lines.pop();

            for (const line of lines) {
                if (!line.trim()) continue;
                try {
                    const event = JSON.parse(line);
                    tracker.events.push(event);
                    tracker.finalEvent = event;
                    _renderDeployProgress(progressDiv, event, 'validate');
                } catch (e) { /* skip malformed */ }
            }
        }

        if (buffer.trim()) {
            try {
                const event = JSON.parse(buffer);
                tracker.events.push(event);
                tracker.finalEvent = event;
                _renderDeployProgress(progressDiv, event, 'validate');
            } catch (e) { /* skip */ }
        }

        if (tracker.finalEvent?.status === 'succeeded') {
            const resolved = tracker.finalEvent.issues_resolved || 0;
            const healMsg = resolved > 0 ? ` Resolved ${resolved} issue${resolved !== 1 ? 's' : ''} along the way.` : '';
            showToast(`Template verified.${healMsg} Ready to publish.`, 'info');
        } else if (tracker.finalEvent?.status === 'failed') {
            showToast('Validation complete — check the log for details.', 'info');
        }

        // Refresh data (don't switch view — user can navigate when ready)
        await loadAllData();

    } catch (err) {
        if (err.name === 'AbortError') return;
        showToast(`Validation issue: ${err.message}`, 'info');
        progressDiv.innerHTML = `<div class="tmpl-deploy-diag-msg">${escapeHtml(err.message)}</div>`;
    } finally {
        tracker.running = false;
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
        showToast(`🎉 Template published! v${data.published_semver || data.published_version + '.0.0'} is now active in the catalog.`, 'success');

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

        // Check if a specific version was requested
        const deployVersion = window._deploySpecificVersion || null;
        window._deploySpecificVersion = null; // clear after use

        const deployBody = { resource_group: resourceGroup, region, parameters };
        if (deployVersion) deployBody.version = deployVersion;

        const res = await fetch(`/api/catalog/templates/${encodeURIComponent(templateId)}/deploy`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(deployBody),
        });

        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Deploy failed');
        }

        // Read NDJSON stream — phase-based events for flowchart rendering
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
                    _renderDeployProgress(progressDiv, event, 'deploy');
                    if (event.phase === 'complete') finalResult = event;
                } catch (e) { /* skip malformed */ }
            }
        }

        // Process final buffer
        if (buffer.trim()) {
            try {
                const event = JSON.parse(buffer);
                _renderDeployProgress(progressDiv, event, 'deploy');
                if (event.phase === 'complete') finalResult = event;
            } catch (e) { /* skip */ }
        }

        if (finalResult && finalResult.status === 'succeeded') {
            showToast(`Deployment complete. ${(finalResult.provisioned_resources || []).length} resources provisioned.`, 'info');
        } else if (finalResult && finalResult.status === 'needs_work') {
            showToast('Deployment analysis available — see agent notes.', 'info');
        }

    } catch (err) {
        showToast(`Deployment note: ${err.message}`, 'info');
        if (progressDiv) {
            progressDiv.innerHTML = `<div class="tmpl-deploy-diag-msg">${escapeHtml(err.message)}</div>`;
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
            nodeCount: 0,         // total flow nodes created
            currentNodeId: null,  // id of the active main-flow node
            activeStep: 0,        // logical step counter
            seenErrors: {},       // error_code → count (dedup tracking)
            deepHealActive: false,
            branchRegion: null,   // the current branch region element
            branchNodes: {},      // service_id → branch node element
            finalResult: null,
        };

        const flowchart = document.createElement('div');
        flowchart.className = 'vf-flowchart';
        flowchart.innerHTML = `
            <div class="vf-pipeline-header">
                <span class="vf-pipeline-label">${isValidate ? 'Validation Pipeline' : 'Deploy Pipeline'}</span>
                ${_copilotBadge(true)}
            </div>
            <div class="vf-stage-bar">
                <div class="vf-stage vf-stage-active" data-vf-stage="deploy">
                    <div class="vf-stage-dot"></div><span>Deploy</span>
                </div>
                <div class="vf-stage-connector-h"></div>
                <div class="vf-stage" data-vf-stage="analyze">
                    <div class="vf-stage-dot"></div><span>Analyze</span>
                </div>
                <div class="vf-stage-connector-h"></div>
                <div class="vf-stage" data-vf-stage="fix">
                    <div class="vf-stage-dot"></div><span>Fix</span>
                </div>
                <div class="vf-stage-connector-h"></div>
                <div class="vf-stage" data-vf-stage="test">
                    <div class="vf-stage-dot"></div><span>Test</span>
                </div>
                <div class="vf-stage-connector-h"></div>
                <div class="vf-stage" data-vf-stage="verify">
                    <div class="vf-stage-dot"></div><span>Verify</span>
                </div>
            </div>
            <div class="vf-flow-canvas"></div>
            <div class="vf-live-progress"></div>
        `;
        container.appendChild(flowchart);
    }

    const state = container._vfState;
    const flowchart = container.querySelector('.vf-flowchart');
    const canvas = flowchart.querySelector('.vf-flow-canvas');
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
        const order = ['deploy', 'analyze', 'fix', 'test', 'verify'];
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
        const codeMatch = errMsg.match(/\(([A-Za-z]+)\)/);
        if (codeMatch) return codeMatch[1];
        return errMsg.substring(0, 60).replace(/[^a-zA-Z]/g, '').toLowerCase();
    }

    // ── Helper: add a flow edge (arrow line) ──
    function _addEdge(extraClass) {
        const edge = document.createElement('div');
        edge.className = `vf-flow-edge ${extraClass || ''}`;
        canvas.appendChild(edge);
        return edge;
    }

    // ── Helper: create a main-flow node ──
    function _createNode(icon, title, iconClass) {
        state.nodeCount++;
        const nodeId = `vf-node-${state.nodeCount}`;

        // Add edge before all nodes except the first
        if (state.nodeCount > 1) {
            // Finalize previous node if still active
            const prevNode = state.currentNodeId ? document.getElementById(state.currentNodeId) : null;
            if (prevNode && prevNode.classList.contains('vf-flow-node-active')) {
                prevNode.classList.remove('vf-flow-node-active');
                prevNode.classList.add('vf-flow-node-done');
                const prevBadge = prevNode.querySelector('.vf-node-badge');
                if (prevBadge) { prevBadge.className = 'vf-node-badge vf-badge-done'; prevBadge.innerHTML = '● Done'; }
            }
            _addEdge('vf-flow-edge-done');
        }

        const node = document.createElement('div');
        node.className = 'vf-flow-node vf-flow-node-active';
        node.id = nodeId;
        node.innerHTML = `
            <div class="vf-node-header">
                <div class="vf-node-icon ${iconClass || ''}">${icon}</div>
                <div class="vf-node-title">${escapeHtml(title)}</div>
                <div class="vf-node-badge vf-badge-running">
                    <span class="vf-badge-pulse"></span> Running
                </div>
            </div>
            <div class="vf-node-body"></div>
        `;
        canvas.appendChild(node);
        state.currentNodeId = nodeId;
        canvas.scrollTop = canvas.scrollHeight;
        return node;
    }

    // ── Helper: add activity line to current node ──
    function _addActivity(icon, text, cssClass) {
        const node = state.currentNodeId ? document.getElementById(state.currentNodeId) : null;
        if (!node) return null;
        const body = node.querySelector('.vf-node-body');
        const act = document.createElement('div');
        act.className = `vf-activity ${cssClass || ''}`;
        act.innerHTML = `<span class="vf-activity-icon">${icon}</span><span class="vf-activity-text">${text}</span>`;
        body.appendChild(act);
        canvas.scrollTop = canvas.scrollHeight;
        return act;
    }

    // ── Helper: finalize current node ──
    function _finalizeNode(nodeEl, status) {
        if (!nodeEl) return;
        nodeEl.classList.remove('vf-flow-node-active');
        nodeEl.classList.add(status === 'success' ? 'vf-flow-node-success' : 'vf-flow-node-done');
        const badge = nodeEl.querySelector('.vf-node-badge');
        if (!badge) return;
        const labels = {
            success: { cls: 'vf-badge-success', label: '● Complete' },
            done:    { cls: 'vf-badge-done',    label: '● Done' },
        };
        const l = labels[status] || labels.done;
        badge.className = `vf-node-badge ${l.cls}`;
        badge.innerHTML = l.label;
    }

    // ── Helper: add activity to a branch node ──
    function _addBranchActivity(serviceId, icon, text) {
        const branchNode = state.branchNodes[serviceId];
        if (!branchNode) return;
        const body = branchNode.querySelector('.vf-branch-body');
        const step = document.createElement('div');
        step.className = 'vf-branch-step';
        step.innerHTML = `<span class="vf-branch-step-icon">${icon}</span> ${escapeHtml(text)}`;
        body.appendChild(step);
        body.scrollTop = body.scrollHeight;
    }

    // ── Helper: finalize a branch node ──
    function _finalizeBranch(serviceId, status) {
        const branchNode = state.branchNodes[serviceId];
        if (!branchNode) return;
        branchNode.classList.remove('vf-branch-node-active');
        branchNode.classList.add(status === 'success' ? 'vf-branch-node-success' : 'vf-branch-node-failed');
        const badge = branchNode.querySelector('.vf-branch-badge');
        if (badge) {
            badge.className = `vf-branch-badge ${status === 'success' ? 'vf-branch-badge-success' : 'vf-branch-badge-failed'}`;
            badge.textContent = status === 'success' ? '● Done' : '● Issue';
        }
    }

    // ══════════════════════════════════════════════════
    // PHASE HANDLERS
    // ══════════════════════════════════════════════════

    // ── Fix & Validate pre-phases (recompose, structural check) ──
    if (phase === 'recomposing' || phase === 'structural_check') {
        _setActiveStage('deploy');
        const icon = phase === 'recomposing' ? '🔄' : '🔍';
        const title = phase === 'recomposing' ? 'Rebuilding from Services' : 'Checking Structure';
        _createNode(icon, title);
        _addActivity(icon, escapeHtml(detail), 'vf-activity-deploy');
        return;
    }

    if (phase === 'recomposed' || phase === 'structural_ok' || phase === 'structural_fixed' || phase === 'structural_fix') {
        const curNode = state.currentNodeId ? document.getElementById(state.currentNodeId) : null;
        _addActivity('✅', escapeHtml(detail), 'vf-activity-fix');
        if (curNode && phase !== 'structural_fix') _finalizeNode(curNode, 'success');
        return;
    }

    if (phase === 'recompose_error' || phase === 'structural_error') {
        const curNode = state.currentNodeId ? document.getElementById(state.currentNodeId) : null;
        _addActivity('⚠️', escapeHtml(detail), 'vf-activity-issue');
        if (curNode) _finalizeNode(curNode, 'done');
        return;
    }

    if (phase === 'arm_validation_start') {
        const curNode = state.currentNodeId ? document.getElementById(state.currentNodeId) : null;
        if (curNode) _finalizeNode(curNode, 'success');
        _addActivity('🚀', escapeHtml(detail), 'vf-activity-deploy');
        return;
    }

    if (phase === 'pre_validation_fix') {
        const curNode = state.currentNodeId ? document.getElementById(state.currentNodeId) : null;
        _addActivity('🔧', escapeHtml(detail), 'vf-activity-fix');
        return;
    }

    // Starting — show header info
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
                ${event.is_blueprint ? '<span class="vf-tag vf-tag-blueprint">Composite</span>' : ''}
            </div>
        `;
        // Insert after stage bar, before canvas
        canvas.before(headerInfo);
        return;
    }

    // New attempt/step — create a new flow node
    if (phase === 'step' || phase === 'attempt_start') {
        state.activeStep++;
        _setActiveStage('deploy');

        // After deep heal or analysis, show "Verifying" instead of a new iteration
        const ctx = event.context || '';
        let title, icon;
        if (ctx === 'verify_deep_heal') {
            title = 'Verifying Rebuilt Template';
            icon = '🧪';
        } else if (ctx === 'retry') {
            title = 'Deploying Updated Template';
            icon = '🚀';
        } else {
            title = 'Deploying to Azure';
            icon = '🚀';
        }

        const node = _createNode(icon, title);
        _addActivity(icon, escapeHtml(detail || 'Sending the template to Azure…'), 'vf-activity-deploy');
        return;
    }

    // Progress — intermediate status updates shown as activity lines
    if (phase === 'progress') {
        _addActivity('⏳', escapeHtml(detail || 'Working…'), 'vf-activity-deploy');
        canvas.scrollTop = canvas.scrollHeight;
        return;
    }

    // Error — brief agent note in current node, then finalize it
    if (phase === 'error') {
        _setActiveStage('analyze');
        const errMsg = event.error || detail || '';
        const errKey = _errorKey(errMsg);
        if (errKey) state.seenErrors[errKey] = (state.seenErrors[errKey] || 0) + 1;
        _addActivity('📝', 'Looking into something…', 'vf-activity-deploy');
        return;
    }

    // Healing — agent is analyzing the error
    if (phase === 'healing') {
        _setActiveStage('analyze');
        const curNode = state.currentNodeId ? document.getElementById(state.currentNodeId) : null;
        if (curNode) _finalizeNode(curNode, 'done');

        const isRepeated = event.repeated_error;
        const errorBrief = event.error_brief || '';
        const whatWasTried = event.what_was_tried || [];

        // Create an "Analyzing" node
        _addEdge('vf-flow-edge-active');
        state.nodeCount++;
        const nodeId = `vf-node-${state.nodeCount}`;
        const node = document.createElement('div');
        node.className = 'vf-flow-node vf-flow-node-active';
        node.id = nodeId;
        node.innerHTML = `
            <div class="vf-node-header">
                <div class="vf-node-icon vf-node-icon-purple">🧠</div>
                <div class="vf-node-title">Analyzing</div>
                <div class="vf-node-badge vf-badge-running">
                    <span class="vf-badge-pulse"></span> Working
                </div>
            </div>
            <div class="vf-node-body"></div>
        `;
        canvas.appendChild(node);
        state.currentNodeId = nodeId;

        // Show the error brief — what went wrong
        if (errorBrief) {
            _addActivity('📌', `Issue: ${escapeHtml(errorBrief)}`, 'vf-activity-issue');
        }

        // Show what was already tried (if any)
        if (whatWasTried.length > 0) {
            const triedText = whatWasTried.length === 1
                ? `Already tried: ${escapeHtml(whatWasTried[0])}`
                : `Already tried ${whatWasTried.length} approaches — trying something different`;
            _addActivity('📋', triedText, 'vf-activity-history');
        }

        // Add the analysis detail
        const cssClass = isRepeated ? 'vf-activity-escalate' : 'vf-activity-analyze';
        _addActivity('🧠', escapeHtml(detail || 'Analyzing and adjusting…'), cssClass);

        if (event.error_summary) {
            const ek = _errorKey(event.error_summary);
            if (ek) state.seenErrors[ek] = (state.seenErrors[ek] || 0) + 1;
        }
        canvas.scrollTop = canvas.scrollHeight;
        return;
    }

    // Healed — fix applied, show what was fixed and finalize analyzing node
    if (phase === 'healed') {
        _setActiveStage('fix');
        const curNode = state.currentNodeId ? document.getElementById(state.currentNodeId) : null;
        const fixMsg = event.fix_summary || detail || 'Fix applied';
        const deepFlag = event.deep_healed ? '<span class="vf-tag vf-tag-service" style="margin-left:0.3rem;font-size:0.62rem">Deep Fix</span>' : '';
        const errorBrief = event.error_brief || '';

        // Show the resolution: what was wrong → what was fixed
        if (errorBrief) {
            _addActivity('🔧', `${escapeHtml(errorBrief)} → ${escapeHtml(fixMsg)} ${deepFlag}`, 'vf-activity-fix');
        } else {
            _addActivity('🔧', `${escapeHtml(fixMsg)} ${deepFlag}`, 'vf-activity-fix');
        }
        if (curNode) _finalizeNode(curNode, 'done');
        canvas.scrollTop = canvas.scrollHeight;
        return;
    }

    // ── Deep healing — branch off into sub-process nodes ──
    if (phase.startsWith('deep_heal_')) {

        if (phase === 'deep_heal_trigger') {
            state.deepHealActive = true;
            _setActiveStage('analyze');

            // Finalize current node
            const curNode = state.currentNodeId ? document.getElementById(state.currentNodeId) : null;
            if (curNode) _finalizeNode(curNode, 'done');

            const serviceIds = event.service_ids || [];

            // Add edge down to branch region
            _addEdge('vf-flow-edge-active');

            // Create branch label
            const branchLabel = document.createElement('div');
            branchLabel.className = 'vf-branch-label';
            branchLabel.innerHTML = `
                <span class="vf-branch-label-line"></span>
                <span>🔬 Investigating Service Templates</span>
                <span class="vf-branch-label-line"></span>
            `;
            canvas.appendChild(branchLabel);

            // Create branch connector (horizontal bar with drops)
            const connector = document.createElement('div');
            connector.className = 'vf-branch-connector';
            if (serviceIds.length > 1) {
                const barPad = Math.max(5, 50 - serviceIds.length * 15);
                connector.innerHTML = `<div class="vf-branch-connector-bar" style="--bar-left:${barPad}%;--bar-right:${barPad}%"></div>`;
                // Add drop lines for each service
                const spacing = (100 - barPad * 2) / Math.max(1, serviceIds.length - 1);
                serviceIds.forEach((_, i) => {
                    const leftPct = barPad + spacing * i;
                    const drop = document.createElement('div');
                    drop.className = 'vf-branch-drop';
                    drop.style.left = `${leftPct}%`;
                    connector.appendChild(drop);
                });
            }
            canvas.appendChild(connector);

            // Create branch container with nodes
            const branchContainer = document.createElement('div');
            branchContainer.className = 'vf-branch-container';
            state.branchRegion = branchContainer;

            serviceIds.forEach(sid => {
                const shortName = sid.split('/').pop();
                const branchNode = document.createElement('div');
                branchNode.className = 'vf-branch-node vf-branch-node-active';
                branchNode.id = `vf-branch-${sid.replace(/[/.]/g, '-')}`;
                branchNode.innerHTML = `
                    <div class="vf-branch-header">
                        <div class="vf-branch-icon">⚙️</div>
                        <div class="vf-branch-title">${escapeHtml(shortName)}</div>
                        <div class="vf-branch-badge vf-branch-badge-running">
                            <span class="vf-badge-pulse"></span> Working
                        </div>
                    </div>
                    <div class="vf-branch-body"></div>
                `;
                state.branchNodes[sid] = branchNode;
                branchContainer.appendChild(branchNode);
            });

            canvas.appendChild(branchContainer);

            // Create merge connector
            const merge = document.createElement('div');
            merge.className = 'vf-merge-connector';
            merge.id = 'vf-merge-connector';
            if (serviceIds.length > 1) {
                const barPad = Math.max(5, 50 - serviceIds.length * 15);
                merge.innerHTML = `<div class="vf-merge-connector-bar" style="--bar-left:${barPad}%;--bar-right:${barPad}%"></div>`;
                const spacing = (100 - barPad * 2) / Math.max(1, serviceIds.length - 1);
                serviceIds.forEach((_, i) => {
                    const leftPct = barPad + spacing * i;
                    const drop = document.createElement('div');
                    drop.className = 'vf-merge-drop';
                    drop.style.left = `${leftPct}%`;
                    merge.appendChild(drop);
                });
            }
            canvas.appendChild(merge);

            canvas.scrollTop = canvas.scrollHeight;
            return;
        }

        // Route deep_heal sub-events to the correct branch node
        const culpritSid = event.culprit_service || event.service_id || '';

        // Find the matching branch node (or use first if single service)
        let targetSid = culpritSid;
        if (!targetSid || !state.branchNodes[targetSid]) {
            // Try to match by partial name
            for (const sid of Object.keys(state.branchNodes)) {
                if (culpritSid && sid.toLowerCase().includes(culpritSid.toLowerCase())) {
                    targetSid = sid; break;
                }
            }
            // Final fallback: use the first branch node
            if (!state.branchNodes[targetSid]) {
                targetSid = Object.keys(state.branchNodes)[0] || '';
            }
        }

        const deepIcons = {
            deep_heal_start: '🔍', deep_heal_identified: '🎯',
            deep_heal_fix: '🛠️', deep_heal_fix_error: '●',
            deep_heal_validate: '🧪', deep_heal_validate_fail: '🔄',
            deep_heal_validated: '●', deep_heal_version: '💾',
            deep_heal_versioned: '📦', deep_heal_promoted: '🏷️',
            deep_heal_recompose: '🔧', deep_heal_complete: '●',
            deep_heal_fail: '●', deep_heal_fallback: '↩️',
        };
        const icon = deepIcons[phase] || '•';

        if (targetSid) {
            _addBranchActivity(targetSid, icon, detail);
        }

        if (phase === 'deep_heal_complete') {
            if (targetSid) _finalizeBranch(targetSid, 'success');
            state.deepHealActive = false;
        } else if (phase === 'deep_heal_fail') {
            if (targetSid) _finalizeBranch(targetSid, 'failed');
            state.deepHealActive = false;
        } else if (phase === 'deep_heal_validated') {
            if (targetSid) _finalizeBranch(targetSid, 'success');
        } else if (phase === 'deep_heal_validate_fail') {
            // Don't finalize — keep working
        }

        canvas.scrollTop = canvas.scrollHeight;
        return;
    }

    // deep_heal_fallback as a top-level event
    if (phase === 'deep_heal_fallback') {
        state.deepHealActive = false;
        // Add a new node after the branch merge
        _addEdge('vf-flow-edge-active');
        state.nodeCount++;
        const nodeId = `vf-node-${state.nodeCount}`;
        const node = document.createElement('div');
        node.className = 'vf-flow-node vf-flow-node-active';
        node.id = nodeId;
        node.innerHTML = `
            <div class="vf-node-header">
                <div class="vf-node-icon">↩️</div>
                <div class="vf-node-title">Trying Another Approach</div>
                <div class="vf-node-badge vf-badge-running">
                    <span class="vf-badge-pulse"></span> Working
                </div>
            </div>
            <div class="vf-node-body"></div>
        `;
        canvas.appendChild(node);
        state.currentNodeId = nodeId;
        _addActivity('↩️', escapeHtml(detail || 'The deep fix didn\'t pan out — trying another approach…'), 'vf-activity-info');
        canvas.scrollTop = canvas.scrollHeight;
        return;
    }

    // ── Deploy succeeded (before testing) ──
    if (phase === 'deploy_succeeded') {
        const curNode = state.currentNodeId ? document.getElementById(state.currentNodeId) : null;
        if (curNode) _finalizeNode(curNode, 'success');
        const provisioned = event.provisioned_resources || [];
        const healMsg = (event.issues_resolved || 0) > 0
            ? ` — resolved ${event.issues_resolved} issue${event.issues_resolved !== 1 ? 's' : ''}` : '';
        _addActivity('✅', `Deployment succeeded — ${provisioned.length} resource(s) provisioned${healMsg}`, 'vf-activity-success');
        canvas.scrollTop = canvas.scrollHeight;
        return;
    }

    // ── Infrastructure Testing phases ──
    if (phase === 'testing_start') {
        _setActiveStage('test');
        const node = _createNode('🧪', 'Infrastructure Testing');
        _addActivity('🧪', escapeHtml(detail || 'Starting infrastructure tests…'), 'vf-activity-test');
        return;
    }

    if (phase === 'testing_generate') {
        _setActiveStage('test');
        if (event.status === 'running') {
            _addActivity('📝', escapeHtml(detail || 'Generating test scripts…'), 'vf-activity-test');
        } else if (event.status === 'complete') {
            _addActivity('✅', escapeHtml(detail || 'Tests generated'), 'vf-activity-success');
        } else if (event.status === 'error') {
            _addActivity('⚠️', escapeHtml(detail || 'Test generation failed'), 'vf-activity-issue');
        }
        canvas.scrollTop = canvas.scrollHeight;
        return;
    }

    if (phase === 'testing_execute') {
        _setActiveStage('test');
        _addActivity('🏃', escapeHtml(detail || 'Running tests…'), 'vf-activity-test');
        canvas.scrollTop = canvas.scrollHeight;
        return;
    }

    if (phase === 'test_result') {
        const icon = event.status === 'passed' ? '✅' : '❌';
        const cssClass = event.status === 'passed' ? 'vf-activity-success' : 'vf-activity-issue';
        _addActivity(icon, escapeHtml(detail || `${event.test_name}: ${event.message}`), cssClass);
        canvas.scrollTop = canvas.scrollHeight;
        return;
    }

    if (phase === 'testing_analyze') {
        _setActiveStage('test');
        const icon = event.status === 'complete' ? '🔍' : '🧠';
        _addActivity(icon, escapeHtml(detail || 'Analyzing test results…'), 'vf-activity-analyze');
        canvas.scrollTop = canvas.scrollHeight;
        return;
    }

    if (phase === 'testing_feedback') {
        _addActivity('📋', escapeHtml(detail || 'Test feedback'), 'vf-activity-issue');
        canvas.scrollTop = canvas.scrollHeight;
        return;
    }

    if (phase === 'testing_complete') {
        const curNode = state.currentNodeId ? document.getElementById(state.currentNodeId) : null;
        if (event.status === 'passed') {
            _addActivity('✅', escapeHtml(detail || 'All tests passed'), 'vf-activity-success');
            if (curNode) _finalizeNode(curNode, 'success');
        } else if (event.status === 'skipped') {
            _addActivity('⏭️', escapeHtml(detail || 'Tests skipped'), 'vf-activity-info');
            if (curNode) _finalizeNode(curNode, 'done');
        } else {
            _addActivity('⚠️', escapeHtml(detail || 'Some tests failed'), 'vf-activity-issue');
            if (curNode) _finalizeNode(curNode, 'done');
        }
        canvas.scrollTop = canvas.scrollHeight;
        return;
    }

    // ── Final result ──
    if (phase === 'complete' || phase === 'done') {
        const resources = event.provisioned_resources || [];
        const outputs = event.outputs || {};
        const healHistory = event.heal_history || [];
        const issuesResolved = event.issues_resolved || 0;
        const isSuccess = event.status === 'succeeded' || event.status === 'tested_with_issues';

        liveProgress.innerHTML = '';

        // Update stage bar
        if (isSuccess) {
            flowchart.querySelectorAll('.vf-stage').forEach(s => {
                s.classList.remove('vf-stage-active', 'vf-stage-error');
                s.classList.add('vf-stage-done');
            });
        } else {
            _setActiveStage('verify');
            flowchart.querySelectorAll('.vf-stage').forEach(s => {
                s.classList.remove('vf-stage-active', 'vf-stage-error');
                s.classList.add('vf-stage-done');
            });
        }

        // Finalize last active node
        const lastNode = state.currentNodeId ? document.getElementById(state.currentNodeId) : null;
        if (lastNode) _finalizeNode(lastNode, isSuccess ? 'success' : 'done');

        // Add edge to result
        _addEdge('vf-flow-edge-done');

        // Build final result node
        const resultDiv = document.createElement('div');
        const testingNote = event.testing_passed === false
            ? '<div class="vf-result-test-note">⚠️ Some infrastructure tests had issues — the deployment succeeded but you may want to review the test results above.</div>'
            : '';
        if (isSuccess) {
            const healMsg = issuesResolved > 0 ? ` — resolved ${issuesResolved} issue${issuesResolved !== 1 ? 's' : ''} along the way` : '';

            // Build a friendly resource summary grouped by type
            const typeGroups = {};
            const typeIcons = {
                'azurefirewalls': '🛡️', 'firewallpolicies': '📋', 'virtualnetworks': '🌐',
                'subnets': '📡', 'networksecuritygroups': '🔒', 'publicipaddresses': '🔗',
                'storageaccounts': '💾', 'keyvault': '🔑', 'sites': '🌍', 'serverfarms': '📊',
                'databases': '🗄️', 'servers': '🖥️', 'disks': '💽', 'virtualmachines': '🖥️',
            };
            const friendlyType = (t) => (t || '').split('/').pop().replace(/([A-Z])/g, ' $1').trim();
            for (const r of resources) {
                const shortType = (r.type || '').split('/').pop().toLowerCase();
                const label = friendlyType(r.type);
                if (!typeGroups[label]) typeGroups[label] = { icon: typeIcons[shortType] || '📦', items: [] };
                typeGroups[label].items.push(r.name);
            }

            // Show only meaningful outputs (IP addresses, names, locations — skip raw resource IDs)
            const meaningfulOutputs = [];
            for (const [k, v] of Object.entries(outputs)) {
                const val = String(v);
                // Skip raw subscription/resource-group paths
                if (val.startsWith('/subscriptions/')) continue;
                // Skip internal-looking keys
                if (k.startsWith('resourceId') || k.endsWith('Id')) continue;
                // Friendly key label
                const label = k.replace(/_/g, ' ').replace(/([a-z])([A-Z])/g, '$1 $2');
                meaningfulOutputs.push({ key: label, value: val });
            }

            resultDiv.className = 'vf-result vf-result-success';
            resultDiv.innerHTML = `
                <div class="vf-result-header">
                    <span class="vf-result-icon">●</span>
                    <span>${isValidate ? `Template verified${healMsg}` : `Deployment complete${healMsg}`}</span>
                </div>
                ${resources.length ? `
                <div class="vf-result-section">
                    <div class="vf-result-label">${resources.length} resources provisioned successfully</div>
                    <div class="vf-resource-list vf-resource-grouped">
                        ${Object.entries(typeGroups).map(([label, g]) => `
                            <div class="vf-resource-group-item">
                                <span class="vf-rg-icon">${g.icon}</span>
                                <span class="vf-rg-label">${escapeHtml(label)}</span>
                                <span class="vf-rg-count">${g.items.length > 1 ? `×${g.items.length}` : ''}</span>
                            </div>
                        `).join('')}
                    </div>
                </div>` : ''}
                ${meaningfulOutputs.length ? `
                <div class="vf-result-section">
                    <div class="vf-result-label">Key Details</div>
                    <div class="vf-output-list-friendly">
                        ${meaningfulOutputs.map(o => `
                            <div class="vf-output-friendly">
                                <span class="vf-of-key">${escapeHtml(o.key)}</span>
                                <span class="vf-of-val">${escapeHtml(o.value)}</span>
                            </div>
                        `).join('')}
                    </div>
                </div>` : ''}
                ${testingNote}
                <details class="vf-technical-details">
                    <summary>View technical details</summary>
                    <div class="vf-tech-content">
                        ${resources.map(r => `<div class="vf-tech-row"><span class="vf-tech-type">${escapeHtml(r.type)}</span> <span class="vf-tech-name">${escapeHtml(r.name)}</span></div>`).join('')}
                        ${Object.entries(outputs).map(([k, v]) => `<div class="vf-tech-row"><span class="vf-tech-key">${escapeHtml(k)}</span> <code>${escapeHtml(String(v))}</code></div>`).join('')}
                        ${event.deployment_id ? `<div class="vf-tech-row">Deployment: <code>${escapeHtml(event.deployment_id)}</code></div>` : ''}
                    </div>
                </details>
            `;
        } else {
            resultDiv.className = 'vf-result vf-result-fail';
            resultDiv.innerHTML = `
                <div class="vf-result-header">
                    <span class="vf-result-icon">●</span>
                    <span>${isValidate ? 'Still working through some details' : 'Deployment needs a few adjustments'}</span>
                </div>
                <div class="vf-result-body">
                    ${isValidate
                        ? '<p>The agent worked through several iterations. Take a look at the flow above to see the progress.</p>'
                        : `<p>${event.analysis ? escapeHtml(event.analysis) : 'The deployment needs some adjustments. You might want to revise the template or check the parameters.'}</p>`}
                    ${healHistory.length ? `
                    <details class="vf-heal-summary">
                        <summary>🔄 ${healHistory.length} iteration${healHistory.length !== 1 ? 's' : ''} attempted</summary>
                        <div class="vf-heal-list">
                            ${healHistory.map(h => `
                                <div class="vf-heal-entry">
                                    <div class="vf-heal-num">Step ${h.step || '?'}</div>
                                    <div class="vf-heal-fix">🔧 ${escapeHtml(h.fix_summary || '')}</div>
                                </div>
                            `).join('')}
                        </div>
                    </details>` : ''}
                </div>
                ${event.deployment_id ? `<div class="vf-result-meta">Deployment: <code>${escapeHtml(event.deployment_id)}</code></div>` : ''}
            `;
        }
        canvas.appendChild(resultDiv);
        canvas.scrollTop = canvas.scrollHeight;
        state.finalResult = event;
        return;
    }

    // ── Cleanup events ──
    if (phase === 'cleanup' || phase === 'cleanup_done' || phase === 'cleanup_warning') {
        const cleanupEl = document.createElement('div');
        cleanupEl.className = 'vf-cleanup';
        cleanupEl.innerHTML = `🧹 ${escapeHtml(detail)}`;
        canvas.appendChild(cleanupEl);
        return;
    }

    // ── Live progress (overwrite — resource provisioning, validating, etc) ──
    // Deduplicate: if the detail text hasn't changed, just add a dot to
    // show the process is alive instead of repeating the same line.
    if (!state._lastProgressDetail) state._lastProgressDetail = '';
    if (!state._progressDotCount) state._progressDotCount = 0;

    const progressKey = (detail || '') + '|' + (event.succeeded || 0) + '/' + (event.total || 0);
    if (progressKey === state._lastProgressDetail) {
        // Same status — just add a dot to the existing progress text
        state._progressDotCount++;
        const dotEl = liveProgress.querySelector('.vf-progress-dots');
        if (dotEl) {
            dotEl.textContent = '.'.repeat(Math.min(state._progressDotCount, 30));
        }
        return;
    }
    state._lastProgressDetail = progressKey;
    state._progressDotCount = 0;

    const pct = Math.round(progress * 100);
    const phaseIcons = {
        starting: '🚀', resource_group: '📁', validating: '🔍',
        validated: '●', deploying: '⚙️', provisioning: '📦',
    };
    const pIcon = phaseIcons[phase] || '⏳';
    liveProgress.innerHTML = `
        <div class="vf-progress-bar">
            <div class="vf-progress-fill" style="width: ${pct}%"></div>
        </div>
        <div class="vf-progress-phase">${pIcon} ${escapeHtml(detail || phase)}<span class="vf-progress-dots"></span></div>
        ${event.resources ? `
        <div class="vf-resource-chips">
            ${event.resources.map(r => `
                <span class="vf-res-chip vf-res-${r.state.toLowerCase()}">
                    ${r.state === 'Succeeded' ? '●' : r.state === 'Running' ? '⏳' : '⏸️'} ${escapeHtml(r.name)}
                </span>
            `).join('')}
        </div>` : ''}
    `;
}

/** Fix & Validate — single unified flow for failed templates.
 *  Blueprints: recompose → structural check → ARM validate.
 *  Standalone: structural heal → ARM validate.
 *  Streams NDJSON progress inline.
 */
async function fixAndValidateTemplate(templateId) {
    showToast('🔧 Fixing and validating — this may take a few minutes…', 'info');

    // Open the detail view and create a live progress area
    await loadAllData();
    showTemplateDetail(templateId);
    await new Promise(r => setTimeout(r, 200));

    // Create a visible progress container at the top of the detail body.
    // The existing tmpl-validate-results lives inside the hidden tmpl-validate-form,
    // so we create a fresh one outside it to ensure visibility.
    let resultsDiv = document.getElementById('fix-validate-progress');
    if (resultsDiv) resultsDiv.remove();
    const detailBody = document.getElementById('detail-template-body');
    if (detailBody) {
        resultsDiv = document.createElement('div');
        resultsDiv.id = 'fix-validate-progress';
        resultsDiv.className = 'tmpl-validate-results detail-section';
        resultsDiv.style.display = 'block';
        resultsDiv.innerHTML = '<div class="compose-loading">🔧 Working on it… Rebuilding, fixing, and validating against Azure.</div>';
        detailBody.prepend(resultsDiv);
    }

    const tracker = { running: true, events: [], finalEvent: null, abortController: new AbortController() };
    _activeTemplateValidations[templateId] = tracker;

    try {
        const regionSelect = document.getElementById('tmpl-validate-region');
        const region = regionSelect ? regionSelect.value : 'eastus2';

        const res = await fetch(`/api/catalog/templates/${encodeURIComponent(templateId)}/fix-and-validate`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ parameters: {}, region }),
            signal: tracker.abortController.signal,
        });

        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Fix & Validate failed');
        }

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });

            const lines = buffer.split('\n');
            buffer = lines.pop();

            for (const line of lines) {
                if (!line.trim()) continue;
                try {
                    const event = JSON.parse(line);
                    tracker.events.push(event);
                    tracker.finalEvent = event;
                    if (resultsDiv) _renderDeployProgress(resultsDiv, event, 'validate');
                } catch (e) { /* skip */ }
            }
        }

        if (buffer.trim()) {
            try {
                const event = JSON.parse(buffer);
                tracker.events.push(event);
                tracker.finalEvent = event;
                if (resultsDiv) _renderDeployProgress(resultsDiv, event, 'validate');
            } catch (e) { /* skip */ }
        }

        // Refresh data
        await loadAllData();
        const final = tracker.finalEvent;
        if (final && final.phase === 'complete' && (final.status === 'succeeded' || final.status === 'tested_with_issues')) {
            showToast('✅ Template fixed and validated — ready to publish!', 'success');
        } else {
            showToast('⚠️ Validation finished — check the results for details.', 'warning');
        }
    } catch (err) {
        if (err.name === 'AbortError') return;
        showToast(`Fix & Validate error: ${err.message}`, 'error');
        if (resultsDiv) resultsDiv.innerHTML += `<div class="deploy-error">❌ ${escapeHtml(err.message)}</div>`;
    } finally {
        tracker.running = false;
        delete _activeTemplateValidations[templateId];
        // Invalidate pipeline run cache so the next load picks up saved events
        delete _templatePipelineRunCache[templateId];
    }
}

/** Auto-heal a failed template — system fixes it, not the user */
async function autoHealTemplate(templateId) {
    showToast('🔧 Using the Copilot SDK to adjust this automatically…', 'info');

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
            showToast('Everything looks fine — no adjustments needed.', 'info');
        } else if (data.all_passed) {
            showToast(`All ${data.retest?.total || ''} tests pass now. Running full validation…`, 'info');
            // Auto-chain to ARM validation after successful heal
            await loadAllData();
            showTemplateDetail(templateId);
            await new Promise(r => setTimeout(r, 300));
            showValidateForm(templateId);
            await new Promise(r => setTimeout(r, 200));
            runTemplateValidation(templateId);
            return;
        } else {
            showToast(`Adjusted some things — ${data.retest?.passed || 0}/${data.retest?.total || 0} tests pass now. You might want to use Request Revision for the rest.`, 'info');
        }

        await loadAllData();
        showTemplateDetail(templateId);
    } catch (err) {
        showToast(`Auto-heal error: ${err.message}`, 'error');
    }
}

/** Run tests on a template from the detail drawer */
async function runTemplateTest(templateId) {
    showToast('🧪 Checking the template structure…', 'info');

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
            showToast(`All ${results.total} tests passed — starting validation…`, 'info');
            // Auto-chain to ARM validation
            await loadAllData();
            showTemplateDetail(templateId);
            await new Promise(r => setTimeout(r, 300));
            showValidateForm(templateId);
            await new Promise(r => setTimeout(r, 200));
            runTemplateValidation(templateId);
            return;
        } else {
            showToast(`${results.failed} of ${results.total} tests need attention`, 'info');
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
        submitted: '📨', in_review: '🔍', approved: '●',
        conditional: '●', denied: '●', deferred: '⏳',
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

function escapeAttr(text) {
    return text.replace(/&/g, '&amp;').replace(/'/g, '&#39;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
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
                <span class="version-badge version-active">${svc.template_api_version || ('v' + (svc.active_version || '?'))}</span>
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
            analysis.provides.forEach(p => { html += `<span class="tmpl-chip tmpl-chip-provides"><span class="az-chip-icon">${_azureIcon(p, 14)}</span>${_shortType(p)}</span>`; });
            html += '</div></div>';
        }

        if (analysis.auto_created?.length) {
            html += '<div class="dep-block"><h5>🔧 Auto-Created Supporting Resources</h5>';
            analysis.auto_created.forEach(a => {
                html += `<div class="dep-detail-item dep-auto"><code><span class="az-chip-icon">${_azureIcon(a.type, 14)}</span>${_shortType(a.type)}</code> — ${escapeHtml(a.reason)}</div>`;
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
                html += `<div class="dep-detail-item dep-optional"><code><span class="az-chip-icon">${_azureIcon(o.type, 14)}</span>${_shortType(o.type)}</code> — ${escapeHtml(o.reason)}</div>`;
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

function openModal(id) {
    document.getElementById(id).classList.remove('hidden');
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
    btn.textContent = '⏳ Copilot SDK checking policies…';
    policyDiv.style.display = 'none';
    resultDiv.style.display = 'none';

    try {
        // ── Step 1: Policy pre-check via a lightweight POST ──
        // We reuse the compose-from-prompt endpoint but show incremental feedback
        btn.textContent = '⏳ Copilot SDK analyzing services…';
        resultDiv.style.display = 'block';
        resultDiv.innerHTML = '<div class="tmpl-revision-loading">Copilot SDK is identifying services, checking policies, resolving dependencies…</div>';

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
                const issueRules = (pr.issues || []).map(i => i.rule).filter(Boolean);
                const hasAlternative = pr.compliant_alternative;
                const hasRationale = pr.policy_rationale;

                policyDiv.className = 'tmpl-revision-policy tmpl-policy-block';
                policyDiv.innerHTML = `
                    <div class="tmpl-policy-header">🛡️ Policy Guidance</div>
                    <div class="tmpl-policy-summary">${escapeHtml(pr.summary)}</div>
                    ${pr.issues?.length ? `<ul class="tmpl-policy-issues">
                        ${pr.issues.map(i => `<li class="tmpl-policy-issue-${i.severity}">
                            <strong>${escapeHtml(i.rule)}</strong>: ${escapeHtml(i.message)}
                        </li>`).join('')}
                    </ul>` : ''}
                    ${hasRationale ? `<div class="tmpl-policy-rationale">
                        <strong>Why this policy exists:</strong> ${escapeHtml(pr.policy_rationale)}
                    </div>` : ''}
                    ${hasAlternative ? `<div class="tmpl-policy-alternative">
                        <div class="tmpl-policy-alternative-header">✅ What you CAN do instead</div>
                        <div class="tmpl-policy-alternative-body">${escapeHtml(pr.compliant_alternative)}</div>
                    </div>` : ''}
                    <div class="tmpl-policy-actions">
                        ${hasAlternative ? `<button class="btn btn-primary btn-sm" id="compose-policy-alt-btn">
                            ✅ Use Compliant Alternative
                        </button>` : ''}
                        <button class="btn btn-sm btn-secondary" id="compose-policy-discuss-btn">
                            💬 Discuss Options
                        </button>
                        <button class="btn btn-sm btn-danger" id="compose-policy-challenge-btn">
                            ⚠️ Request Policy Exception
                        </button>
                    </div>`;

                const altBtn = document.getElementById('compose-policy-alt-btn');
                if (altBtn && hasAlternative) {
                    altBtn.onclick = () => {
                        textarea.value = pr.compliant_alternative;
                        policyDiv.style.display = 'none';
                        resultDiv.style.display = 'none';
                        showToast('Alternative applied — click Create to proceed', 'info');
                        btn.disabled = false;
                        btn.textContent = '🚀 Create Infrastructure';
                    };
                }
                const discussBtn = document.getElementById('compose-policy-discuss-btn');
                if (discussBtn) {
                    const issuesSummary = (pr.issues || []).map(i => '- ' + i.rule + ': ' + i.message).join('\\n');
                    const chatPrompt = 'I tried to create infrastructure with this request:\\n\\n"' + prompt + '"\\n\\nBut it was blocked by organizational policy:\\n' + issuesSummary + '\\n\\nPlease suggest a compliant configuration that satisfies my requirements while meeting all policy constraints.';
                    discussBtn.onclick = () => {
                        closeModal('modal-template-onboard');
                        navigateToChat(chatPrompt);
                    };
                }
                const challengeBtn = document.getElementById('compose-policy-challenge-btn');
                if (challengeBtn) {
                    challengeBtn.onclick = () => _showPolicyExceptionForm(null, prompt, issueRules, policyDiv);
                }

                resultDiv.style.display = 'none';
                btn.disabled = false;
                btn.textContent = '🚀 Create Infrastructure';
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
        _updateGovernanceSummary();
        _renderStandardsList();
    } catch (err) {
        console.error('Failed to load standards:', err);
        document.getElementById('standards-list').innerHTML =
            `<div class="compose-empty">Failed to load standards: ${err.message}</div>`;
    }
}

function _updateGovernanceSummary() {
    _renderCompletenessBoard();
}

// ── Governance Completeness Dashboard ───────────────────────

const GOV_CATEGORIES = [
    {
        id: 'naming',
        icon: '🏷️',
        name: 'Naming Conventions',
        desc: 'Resource naming patterns for consistency across your organization',
        prompt: `Our organization requires the following naming conventions for Azure resources:

- All resources must follow the pattern: {env}-{app}-{resourcetype}-{region}-{instance}
- Environment abbreviations: prod, stg, dev, test, sandbox
- Resource type abbreviations: rg (Resource Group), vnet (Virtual Network), snet (Subnet), pip (Public IP), nsg (Network Security Group), vm (Virtual Machine), sql (SQL Server), sqldb (SQL Database), st (Storage Account), kv (Key Vault), acr (Container Registry), aks (AKS Cluster), app (App Service), func (Function App), apim (API Management), agw (Application Gateway), law (Log Analytics Workspace)
- Region abbreviations: eus (East US), eus2 (East US 2), wus2 (West US 2), weu (West Europe)
- Instance numbers: 001, 002, etc.
- Examples: prod-myapp-sql-eus2-001, dev-portal-vm-wus2-001, prod-billing-kv-eus2-001
- Resource groups: {env}-{app}-rg-{region} (e.g. prod-myapp-rg-eus2)
- All names must be lowercase, alphanumeric with hyphens only (no underscores)`,
    },
    {
        id: 'security',
        icon: '🔒',
        name: 'Security Standards',
        desc: 'Baseline security requirements for all infrastructure',
        prompt: `Our organization's security standards require:

- All secrets, keys, and certificates must be stored in Azure Key Vault — never hardcoded
- Service principal secrets are prohibited; use managed identities for all service-to-service auth
- All storage accounts must deny public blob access (allowBlobPublicAccess = false)
- Key Vaults must have soft-delete enabled with 90-day retention and purge protection
- RBAC authorization must be used for Key Vault access (no access policies)
- All SQL databases must have Transparent Data Encryption (TDE) enabled
- Azure Defender / Microsoft Defender for Cloud must be enabled for all subscriptions
- Just-in-time (JIT) VM access must be enabled for all virtual machines
- All resources must have resource locks (CanNotDelete) in production environments`,
    },
    {
        id: 'encryption',
        icon: '🔐',
        name: 'Encryption Standards',
        desc: 'Data protection through encryption at rest and in transit',
        prompt: `Our encryption standards require:

- TLS 1.2 minimum for all services — TLS 1.0 and 1.1 must be disabled
- HTTPS must be enforced on all web-facing resources (httpsOnly = true)
- All data stores must use encryption at rest (TDE for SQL, SSE for Storage, etc.)
- Customer-managed keys (CMK) required for production workloads storing sensitive data
- Storage accounts must use Microsoft-managed keys at minimum, CMK preferred
- SSL/TLS certificates must be managed through Key Vault with auto-renewal
- Database connections must use encrypted connections only`,
    },
    {
        id: 'identity',
        icon: '👤',
        name: 'Identity & Access',
        desc: 'Authentication, authorization, and access management rules',
        prompt: `Our identity and access management standards require:

- Managed identities (system or user-assigned) must be used for all Azure service authentication
- Azure AD authentication must be enabled for all services that support it (SQL, PostgreSQL, Redis, etc.)
- Local/SQL authentication must be disabled on databases in production
- Multi-factor authentication (MFA) must be enforced for all user accounts
- Privileged Identity Management (PIM) must be used for elevated access
- Service principals, if unavoidable, must have credentials rotated every 90 days
- Role-Based Access Control (RBAC) must follow least-privilege principle
- No Contributor or Owner roles at subscription level without PIM`,
    },
    {
        id: 'network',
        icon: '🌐',
        name: 'Network Security',
        desc: 'Network isolation, private endpoints, and traffic rules',
        prompt: `Our network security standards require:

- Public network access must be disabled for all data services (publicNetworkAccess = Disabled)
- Private endpoints required for all PaaS services in production (SQL, Storage, Key Vault, etc.)
- All VNets must use Network Security Groups (NSGs) on every subnet
- NSG flow logs must be enabled and sent to Log Analytics
- No resources may have public IP addresses unless explicitly approved
- Application Gateway or Azure Front Door with WAF must front all public-facing applications
- VNet peering must be used instead of VPN for intra-region connectivity
- DNS must use Azure Private DNS Zones for private endpoint resolution`,
    },
    {
        id: 'tagging',
        icon: '📎',
        name: 'Resource Tagging',
        desc: 'Mandatory tags for cost tracking, ownership, and governance',
        prompt: `Our resource tagging standards require:

- All Azure resources must have the following mandatory tags:
  - environment: (prod, staging, dev, test, sandbox)
  - owner: (email of the resource owner)
  - costCenter: (finance cost center code)
  - project: (project or application name)
  - dataClassification: (public, internal, confidential, restricted)
  - createdBy: (deploying identity or pipeline)
  - createdDate: (ISO 8601 date of creation)
- Optional but recommended tags:
  - team: (team name)
  - expiryDate: (for temporary/dev resources)
  - supportContact: (on-call team or email)
- Tag values must follow casing conventions: lowercase for environment, email format for owner
- Resources without mandatory tags must be flagged and remediated within 48 hours`,
    },
    {
        id: 'compliance_hipaa',
        icon: '🏥',
        name: 'HIPAA',
        desc: 'Health Insurance Portability and Accountability Act — PHI protection',
        group: 'Regulatory Compliance',
        groupIcon: '📋',
        prompt: `Our HIPAA compliance standards for protecting PHI (Protected Health Information) require:

- PHI data stores must use customer-managed encryption keys (CMK)
- Access to PHI must be logged and auditable for a minimum of 7 years
- PHI data must not traverse public networks — private endpoints required
- Business Associate Agreements (BAAs) must be in place with all vendors handling PHI
- PHI at rest must be encrypted with AES-256 or stronger
- PHI in transit must use TLS 1.2 or higher
- Access to PHI resources must require multi-factor authentication
- PHI data must be classified and tagged with dataClassification = "restricted"
- Backup and recovery of PHI data must meet HIPAA retention requirements
- Audit logs for PHI access must be immutable and tamper-evident`,
    },
    {
        id: 'compliance_soc2',
        icon: '🔒',
        name: 'SOC 2',
        desc: 'Service Organization Control 2 — security, availability, and confidentiality',
        group: 'Regulatory Compliance',
        groupIcon: '📋',
        prompt: `Our SOC 2 compliance standards (Trust Services Criteria) require:

Security:
- All changes must go through approved CI/CD pipelines (no manual portal changes)
- Access reviews must be conducted quarterly and documented
- All production access must be logged and monitored with alerts
- Incident response procedures must be documented and tested annually
- Vulnerability scanning must be performed at least monthly
- Penetration testing must be conducted annually by a third party

Availability:
- Production systems must have documented SLAs with uptime targets
- Disaster recovery plans must be tested at least annually
- Automated monitoring and alerting must be in place for all critical services

Confidentiality:
- Data classification labels must be applied to all resources
- Encryption at rest and in transit is mandatory for all data stores
- Key rotation must occur at least every 365 days`,
    },
    {
        id: 'compliance_pci',
        icon: '💳',
        name: 'PCI-DSS',
        desc: 'Payment Card Industry Data Security Standard — cardholder data protection',
        group: 'Regulatory Compliance',
        groupIcon: '📋',
        prompt: `Our PCI-DSS compliance standards for cardholder data environments (CDE) require:

Requirement 1 — Network Security:
- Cardholder data environments must be isolated in dedicated VNets/subnets
- Network Security Groups (NSGs) must restrict traffic to/from CDE
- Web Application Firewall (WAF) required for all public-facing payment applications

Requirement 3 — Protect Stored Data:
- PAN (Primary Account Number) must never be stored in plaintext
- Cardholder data at rest must use AES-256 encryption with customer-managed keys
- Encryption key management must follow documented key lifecycle procedures

Requirement 4 — Encrypt Transmission:
- TLS 1.2 or higher required for all cardholder data transmission
- No cardholder data may traverse public networks unencrypted

Requirement 7 — Restrict Access:
- Access to cardholder data limited to personnel with business need-to-know
- Role-based access control (RBAC) must enforce least-privilege
- All access to CDE must require multi-factor authentication

Requirement 10 — Logging and Monitoring:
- All access to cardholder data must be logged with immutable audit trails
- Logs must be retained for at least 1 year, with 3 months immediately available
- Automated alerting for suspicious activity in CDE environments
- Log integrity monitoring must be enabled`,
    },
    {
        id: 'compliance_gdpr',
        icon: '🇪🇺',
        name: 'GDPR',
        desc: 'General Data Protection Regulation — EU personal data protection',
        group: 'Regulatory Compliance',
        groupIcon: '📋',
        prompt: `Our GDPR compliance standards for EU personal data protection require:

Data Residency & Sovereignty:
- EU personal data must be stored in EU-based Azure regions (West Europe, North Europe)
- Cross-border data transfers must comply with adequacy decisions or use SCCs
- Geo-replication for DR must use EU region pairs only for EU data

Data Protection:
- Personal data must be encrypted at rest and in transit
- Pseudonymization must be applied where feasible
- Data minimization — only collect and store data necessary for the stated purpose
- Resources storing personal data must be tagged with dataClassification = "confidential" or "restricted"

Data Subject Rights:
- Systems must support data export (right of access / portability)
- Systems must support data deletion (right to erasure / right to be forgotten)
- Consent management and audit trails must be implemented

Security:
- Data Protection Impact Assessments (DPIAs) must be documented for high-risk processing
- Breach notification procedures must enable reporting within 72 hours
- Access to personal data must be logged and auditable
- Privacy by design — default to most privacy-protective settings`,
    },
    {
        id: 'compliance_data_residency',
        icon: '🌍',
        name: 'Data Residency',
        desc: 'Data sovereignty, geographic restrictions, and cross-border transfer rules',
        group: 'Regulatory Compliance',
        groupIcon: '📋',
        prompt: `Our data residency and sovereignty standards require:

Geographic Restrictions:
- Customer data must remain within approved geographic regions
- Cross-region replication for DR must use approved region pairs only
- EU customer data: West Europe and North Europe only
- US customer data: East US 2 and West US 2 only
- No customer data may be stored in or replicated to unapproved regions

Cross-Border Transfers:
- Data transfers between regions must comply with applicable regulations (GDPR, etc.)
- Standard Contractual Clauses (SCCs) must be in place for cross-border transfers
- Transfer Impact Assessments must be documented

Data Classification:
- All data stores must be tagged with data residency region
- Data sovereignty requirements must be documented per dataset
- Resources must specify location explicitly (no default/inherited location)`,
    },
    {
        id: 'monitoring',
        icon: '📡',
        name: 'Monitoring & Logging',
        desc: 'Observability, diagnostic logging, and alerting requirements',
        prompt: `Our monitoring and logging standards require:

- All resources must have diagnostic settings enabled
- Diagnostic logs must be sent to a central Log Analytics workspace
- Activity logs must be retained for at least 365 days
- Azure Monitor alerts must be configured for: CPU > 90%, memory > 85%, disk > 90%
- Application Insights must be enabled for all web applications
- Custom metrics must be emitted for business-critical KPIs
- Availability tests (ping tests) must be configured for all public endpoints
- Action groups must be configured to notify the on-call team via email and Teams
- Log-based alerts must be created for security events (failed logins, privilege escalation)`,
    },
    {
        id: 'geography',
        icon: '🌍',
        name: 'Region & Geography',
        desc: 'Approved deployment regions and data residency rules',
        prompt: `Our geographic deployment standards require:

- Approved Azure regions for production: East US 2, West US 2, West Europe
- Approved regions for dev/test: East US 2, West US 2
- Disaster recovery must use paired regions (East US 2 ↔ West US 2)
- Data sovereignty: EU customer data must remain in West Europe or North Europe
- New region approvals require security review and 2-week lead time
- All resources must specify location explicitly (no default/inherited location)`,
    },
    {
        id: 'cost',
        icon: '💰',
        name: 'Cost Management',
        desc: 'Budget thresholds, SKU restrictions, and cost optimization',
        prompt: `Our cost management standards require:

- Monthly cost per project must not exceed $5,000 without VP approval
- Dev/test resources must use B-series or D-series VMs (no premium SKUs)
- Auto-shutdown must be enabled for all dev/test VMs (7 PM local time)
- Reserved instances must be used for production workloads with predictable usage
- Storage must use appropriate tiers (Hot for active, Cool for infrequent, Archive for retention)
- Orphaned resources (unattached disks, unused IPs) must be cleaned up within 7 days
- Cost alerts must be set at 80% and 100% of budget
- Spot VMs should be considered for batch/fault-tolerant workloads`,
    },
    {
        id: 'availability',
        icon: '🛡️',
        name: 'Availability & DR',
        desc: 'High availability, backup, disaster recovery, and SLA requirements',
        prompt: `Our availability and disaster recovery standards require:

- Production workloads must use availability zones where supported
- All databases must have automated backups with at least 30-day retention
- Point-in-time restore must be enabled for all SQL databases
- Geo-redundant backup (GRS) required for production storage accounts
- RTO (Recovery Time Objective): 4 hours for critical, 24 hours for standard
- RPO (Recovery Point Objective): 1 hour for critical, 24 hours for standard
- DR failover must be tested at least annually
- Azure Site Recovery must be configured for critical VM workloads
- Load balancers must use zone-redundant frontend IPs`,
    },
];

function _renderCompletenessBoard() {
    const container = document.getElementById('gov-completeness');
    if (!container) return;

    // Count standards per category (for regular categories)
    const catCounts = {};
    const catEnabled = {};
    for (const std of allStandards) {
        const cat = std.category;
        catCounts[cat] = (catCounts[cat] || 0) + 1;
        if (std.enabled) catEnabled[cat] = (catEnabled[cat] || 0) + 1;
    }

    // Count standards per framework (for regulatory framework categories)
    const fwCounts = {};
    const fwEnabled = {};
    for (const std of allStandards) {
        for (const fw of (std.frameworks || [])) {
            fwCounts[fw] = (fwCounts[fw] || 0) + 1;
            if (std.enabled) fwEnabled[fw] = (fwEnabled[fw] || 0) + 1;
        }
    }

    // For completeness calculation, use appropriate counter per category type
    const _getCount = (cat) => cat.group ? (fwCounts[cat.id] || 0) : (catCounts[cat.id] || 0);
    const _getEnabled = (cat) => cat.group ? (fwEnabled[cat.id] || 0) : (catEnabled[cat.id] || 0);

    const configured = GOV_CATEGORIES.filter(c => _getCount(c) > 0).length;
    const total = GOV_CATEGORIES.length;
    const pct = total > 0 ? Math.round((configured / total) * 100) : 0;

    // Separate ungrouped and grouped categories
    const ungrouped = GOV_CATEGORIES.filter(c => !c.group);
    const groups = {};
    for (const cat of GOV_CATEGORIES) {
        if (cat.group) {
            if (!groups[cat.group]) groups[cat.group] = { icon: cat.groupIcon || '📋', cats: [] };
            groups[cat.group].cats.push(cat);
        }
    }

    // Count CAF-aligned standards (those with risk_id populated)
    const cafAligned = allStandards.filter(s => s.risk_id).length;
    const cafPct = allStandards.length > 0 ? Math.round((cafAligned / allStandards.length) * 100) : 0;

    // Progress header
    let html = `
    <div class="gov-completeness-header">
        <div class="gov-completeness-title">
            <h3>Governance Completeness</h3>
            <span class="gov-completeness-pct ${pct === 100 ? 'gov-complete' : pct >= 50 ? 'gov-partial' : 'gov-low'}">${pct}%</span>
        </div>
        <div class="gov-completeness-bar">
            <div class="gov-completeness-fill" style="width: ${pct}%"></div>
        </div>
        <div class="gov-completeness-subtitle">
            ${configured} of ${total} standard categories configured · ${allStandards.length} total standards (${allStandards.filter(s => s.enabled).length} enabled)
        </div>
        <div class="gov-caf-alignment">
            <span class="gov-caf-label">☁️ CAF Alignment</span>
            <span class="gov-caf-pct ${cafPct === 100 ? 'gov-complete' : cafPct >= 50 ? 'gov-partial' : 'gov-low'}">${cafPct}%</span>
            <span class="gov-caf-detail">${cafAligned}/${allStandards.length} standards have risk linkage</span>
        </div>
    </div>
    <div class="gov-category-grid">`;

    // Render ungrouped categories as flat cards
    for (const cat of ungrouped) {
        html += _renderCatCard(cat, catCounts, catEnabled);
    }

    // Render grouped categories (e.g. "Regulatory Compliance")
    for (const [groupName, groupData] of Object.entries(groups)) {
        const groupCats = groupData.cats;
        const groupConfigured = groupCats.filter(c => (fwCounts[c.id] || 0) > 0).length;
        const groupTotal = groupCats.length;
        const groupTotalStds = groupCats.reduce((sum, c) => sum + (fwEnabled[c.id] || 0), 0);

        html += `
        <div class="gov-cat-group">
            <div class="gov-cat-group-header">
                <span class="gov-cat-group-icon">${groupData.icon}</span>
                <span class="gov-cat-group-name">${escapeHtml(groupName)}</span>
                <span class="gov-cat-group-count">${groupConfigured}/${groupTotal} frameworks${groupTotalStds > 0 ? ` · ${groupTotalStds} standards` : ''}</span>
            </div>
            <div class="gov-cat-group-grid">`;

        for (const cat of groupCats) {
            html += _renderCatCard(cat, fwCounts, fwEnabled);
        }

        html += `
            </div>
        </div>`;
    }

    // Any extra categories not in GOV_CATEGORIES
    const knownIds = new Set(GOV_CATEGORIES.map(c => c.id));
    const extraCats = Object.keys(catCounts).filter(c => !knownIds.has(c));
    for (const catId of extraCats) {
        const count = catCounts[catId] || 0;
        const enabled = catEnabled[catId] || 0;
        html += `
        <div class="gov-cat-card gov-cat-configured" onclick="openCategoryDetail('${catId}')">
            <div class="gov-cat-icon">📄</div>
            <div class="gov-cat-info">
                <div class="gov-cat-name">${catId.charAt(0).toUpperCase() + catId.slice(1).replace(/_/g, ' ')}</div>
                <div class="gov-cat-count">${enabled} standard${enabled !== 1 ? 's' : ''} active</div>
            </div>
            <div class="gov-cat-status gov-cat-ok">✓</div>
        </div>`;
    }

    html += '</div>';
    container.innerHTML = html;
}

function _renderCatCard(cat, catCounts, catEnabled) {
    const count = catCounts[cat.id] || 0;
    const enabled = catEnabled[cat.id] || 0;
    const isConfigured = count > 0;

    return `
    <div class="gov-cat-card ${isConfigured ? 'gov-cat-configured' : 'gov-cat-missing'}${cat.group ? ' gov-cat-framework' : ''}" onclick="openCategoryDetail('${cat.id}')">
        <div class="gov-cat-icon">${cat.icon}</div>
        <div class="gov-cat-info">
            <div class="gov-cat-name">${cat.name}</div>
            ${isConfigured
                ? `<div class="gov-cat-count">${enabled} standard${enabled !== 1 ? 's' : ''} active</div>`
                : `<div class="gov-cat-desc">${cat.desc}</div>`
            }
        </div>
        ${isConfigured
            ? `<div class="gov-cat-status gov-cat-ok">✓</div>`
            : `<div class="gov-cat-status gov-cat-gap">○</div>`
        }
    </div>`;
}

function openCategoryDetail(categoryId) {
    const cat = GOV_CATEGORIES.find(c => c.id === categoryId);
    const titleEl = document.getElementById('category-detail-title');
    const bodyEl = document.getElementById('category-detail-body');
    if (!bodyEl) return;

    // Category info
    const catName = cat ? cat.name : categoryId.charAt(0).toUpperCase() + categoryId.slice(1).replace(/_/g, ' ');
    const catIcon = cat ? cat.icon : '📄';
    const catDesc = cat ? cat.desc : '';
    const isFramework = cat && cat.group; // Regulatory framework (cross-cutting view)

    if (titleEl) titleEl.textContent = `${catIcon} ${catName}`;

    // Find existing standards:
    // - For frameworks: show all standards tagged with this framework (cross-cutting across categories)
    // - For regular categories: show standards with matching category
    const catStandards = isFramework
        ? allStandards.filter(s => (s.frameworks || []).includes(categoryId))
        : allStandards.filter(s => s.category === categoryId);
    const enabled = catStandards.filter(s => s.enabled);
    const disabled = catStandards.filter(s => !s.enabled);

    let html = '';

    // ── Description
    if (catDesc) {
        html += `<p class="cat-detail-desc">${escapeHtml(catDesc)}</p>`;
    }

    if (catStandards.length > 0) {
        // ═══════════════════════════════════════════════════
        // CONFIGURED MODE — table + modification prompt
        // ═══════════════════════════════════════════════════

        // For framework views, group standards by their category for clarity
        const showCategoryCol = isFramework;

        html += `
        <div class="cat-detail-section">
            <h4>Standards <span class="cat-detail-count">${enabled.length} active · ${disabled.length} disabled${isFramework ? ` · across ${new Set(catStandards.map(s=>s.category)).size} categories` : ''}</span></h4>
            <table class="cat-std-table">
                <thead>
                    <tr>
                        <th style="width:40px"></th>
                        <th>Standard</th>
                        ${showCategoryCol ? '<th>Category</th>' : ''}
                        <th>Severity</th>
                        <th>Rule</th>
                        <th style="width:50px"></th>
                    </tr>
                </thead>
                <tbody>`;

        // Sort: for frameworks, group by category
        const sortedStds = isFramework
            ? [...catStandards].sort((a, b) => a.category.localeCompare(b.category) || a.name.localeCompare(b.name))
            : catStandards;

        for (const std of sortedStds) {
            const sevIcon = std.severity === 'critical' ? '🔴' : std.severity === 'high' ? '🟠' : std.severity === 'medium' ? '🟡' : '🟢';
            const sevLabel = std.severity.charAt(0).toUpperCase() + std.severity.slice(1);
            const ruleDesc = _describeRule(std.rule);
            const scope = std.scope === '*' ? 'All services' : std.scope;
            const catLabel = std.category.charAt(0).toUpperCase() + std.category.slice(1).replace(/_/g, ' ');
            // Framework badges for this standard
            const fwBadges = (std.frameworks || [])
                .filter(fw => fw !== categoryId) // Don't show the current framework as a badge
                .map(fw => {
                    const fwCat = GOV_CATEGORIES.find(c => c.id === fw);
                    return fwCat ? `<span class="std-fw-badge" title="${fwCat.name}">${fwCat.icon}</span>` : '';
                }).join('');

            html += `
                <tr class="${std.enabled ? '' : 'cat-std-disabled'}">
                    <td>
                        <label class="std-toggle cat-std-toggle">
                            <input type="checkbox" ${std.enabled ? 'checked' : ''} onchange="toggleStandard('${std.id}', this.checked); setTimeout(() => openCategoryDetail('${categoryId}'), 500)">
                            <span class="std-toggle-slider"></span>
                        </label>
                    </td>
                    <td>
                        <div class="cat-std-name">${escapeHtml(std.name)}${fwBadges ? ` <span class="std-fw-badges">${fwBadges}</span>` : ''}</div>
                        <div class="cat-std-scope">${escapeHtml(scope)}</div>
                    </td>
                    ${showCategoryCol ? `<td><span class="cat-std-cat-badge">${catLabel}</span></td>` : ''}
                    <td><span class="cat-std-sev">${sevIcon} ${sevLabel}</span></td>
                    <td><div class="cat-std-rule">${ruleDesc || '—'}</div></td>
                    <td><button class="btn btn-xs btn-ghost" onclick="closeCategoryDetail(); setTimeout(() => showStandardDetail('${std.id}'), 200)" title="View full details">⋯</button></td>
                </tr>`;
        }

        html += `</tbody></table></div>`;

        // ── Modification prompt
        html += `
        <div class="cat-detail-section cat-detail-modify">
            <h4>✏️ ${isFramework ? 'Add Standards for ' + escapeHtml(catName) : 'Modify Standards'}</h4>
            <p class="cat-gen-explain">${isFramework
                ? 'Describe what additional policies this framework requires. The AI will generate standards tagged with ' + escapeHtml(catName) + ' and assign them to the appropriate technical categories.'
                : 'Describe changes you\'d like — add new rules, adjust thresholds, change severity, or refine scope. The AI will generate updated standards.'
            }</p>
            <textarea id="cat-modify-prompt" class="cat-modify-textarea" rows="3" placeholder="${isFramework
                ? 'e.g. Add PHI access logging and audit trail requirements…'
                : 'e.g. Add a rule requiring all resource names to include the cost center code…'
            }"></textarea>
            <div class="cat-detail-footer">
                <button class="btn btn-primary" onclick="modifyStandardsForCategory('${categoryId}')">🤖 ${isFramework ? 'Generate Standards' : 'Apply Changes'}</button>
                <button class="btn btn-secondary" onclick="importStandardsForCategory('${categoryId}')">📥 Import More</button>
            </div>
        </div>`;
    } else {
        // ═══════════════════════════════════════════════════
        // UNCONFIGURED MODE — empty state + generation
        // ═══════════════════════════════════════════════════

        html += `
        <div class="cat-detail-empty">
            <span class="cat-detail-empty-icon">📭</span>
            <p>No standards configured for ${escapeHtml(catName)} yet.</p>
            <p class="cat-detail-empty-hint">Generate a starter set using AI, or import your existing policies.</p>
        </div>`;

        // ── Generation section
        if (cat && cat.prompt) {
            const promptLines = cat.prompt.trim().split('\n').filter(l => l.trim());
            const bullets = promptLines.filter(l => l.trim().startsWith('-')).map(l => l.trim().replace(/^-\s*/, ''));

            html += `
            <div class="cat-detail-section cat-detail-generate">
                <h4>🤖 AI Generation</h4>
                <p class="cat-gen-explain">InfraForge can generate a starter set of standards for this category. Review the template below and customize it to match your organization, then generate.</p>

                <div class="cat-gen-preview">
                    <div class="cat-gen-preview-header">
                        <span>Generation template</span>
                        <button class="btn btn-xs btn-ghost" onclick="document.getElementById('cat-gen-prompt').classList.toggle('hidden'); this.textContent = this.textContent.includes('Edit') ? '▼ Collapse' : '✏️ Edit template'">✏️ Edit template</button>
                    </div>
                    <ul class="cat-gen-bullets">
                        ${bullets.slice(0, 6).map(b => `<li>${escapeHtml(b)}</li>`).join('')}
                        ${bullets.length > 6 ? `<li class="cat-gen-more">… and ${bullets.length - 6} more rules</li>` : ''}
                    </ul>
                    <textarea id="cat-gen-prompt" class="cat-gen-textarea hidden" rows="10">${escapeHtml(cat.prompt)}</textarea>
                </div>

                <div class="cat-gen-options">
                    <label class="cat-gen-option">
                        <input type="checkbox" id="cat-gen-opt-critical" checked>
                        <span>Include critical severity rules</span>
                    </label>
                    <label class="cat-gen-option">
                        <input type="checkbox" id="cat-gen-opt-high" checked>
                        <span>Include high severity rules</span>
                    </label>
                    <label class="cat-gen-option">
                        <input type="checkbox" id="cat-gen-opt-medium" checked>
                        <span>Include medium severity rules</span>
                    </label>
                    <label class="cat-gen-option">
                        <input type="checkbox" id="cat-gen-opt-remediation" checked>
                        <span>Include remediation guidance</span>
                    </label>
                </div>
            </div>`;
        }

        // ── Footer
        html += `
        <div class="cat-detail-footer">
            ${cat && cat.prompt ? `<button class="btn btn-primary" onclick="generateFromCategoryDetail('${categoryId}')">🤖 Generate Standards</button>` : ''}
            <button class="btn btn-secondary" onclick="importStandardsForCategory('${categoryId}')">📥 Import Policies</button>
        </div>`;
    }

    bodyEl.innerHTML = html;
    document.getElementById('category-detail-overlay').classList.remove('hidden');
}

function closeCategoryDetail() {
    document.getElementById('category-detail-overlay').classList.add('hidden');
}

function generateFromCategoryDetail(categoryId) {
    const cat = GOV_CATEGORIES.find(c => c.id === categoryId);
    if (!cat) return;

    const isFramework = cat.group; // Regulatory framework (cross-cutting)

    // Get the (possibly edited) prompt from the textarea
    const promptEl = document.getElementById('cat-gen-prompt');
    let prompt = promptEl ? promptEl.value : cat.prompt;

    // For frameworks, prepend instructions about cross-cutting category assignment and framework tagging
    if (isFramework) {
        prompt = `IMPORTANT: This is a regulatory compliance framework (${cat.name}). For each standard you generate:
1. Set "category" to the appropriate TECHNICAL domain (encryption, identity, network, monitoring, tagging, etc.) — NOT a compliance-prefixed category
2. Include "${categoryId}" in the "frameworks" array, e.g. "frameworks": ["${categoryId}"]
3. If a standard also satisfies other frameworks, include those too (e.g. ["${categoryId}", "compliance_soc2"])

${prompt}`;
    }

    // Append severity/option instructions
    const opts = [];
    if (!document.getElementById('cat-gen-opt-critical')?.checked) opts.push('Do NOT include critical severity rules.');
    if (!document.getElementById('cat-gen-opt-high')?.checked) opts.push('Do NOT include high severity rules.');
    if (!document.getElementById('cat-gen-opt-medium')?.checked) opts.push('Only include critical and high severity rules.');
    if (document.getElementById('cat-gen-opt-remediation')?.checked) opts.push('Include remediation guidance for each rule.');

    if (opts.length > 0) {
        prompt += '\n\nAdditional instructions:\n' + opts.map(o => '- ' + o).join('\n');
    }

    // Close category detail, open import modal with prompt
    closeCategoryDetail();
    openImportStandardsModal();
    switchImportTab('paste');
    const textarea = document.getElementById('import-standards-content');
    if (textarea) {
        textarea.value = prompt;
    }
    setTimeout(() => extractStandards(), 300);
}

function generateStandardsForCategory(categoryId) {
    openCategoryDetail(categoryId);
}

function modifyStandardsForCategory(categoryId) {
    const cat = GOV_CATEGORIES.find(c => c.id === categoryId);
    const promptEl = document.getElementById('cat-modify-prompt');
    const userRequest = promptEl ? promptEl.value.trim() : '';

    if (!userRequest) {
        showToast('Describe the changes you want to make', 'error');
        if (promptEl) promptEl.focus();
        return;
    }

    // Build context: existing standards + user's modification request
    const isFramework = cat && cat.group;
    const catStandards = isFramework
        ? allStandards.filter(s => (s.frameworks || []).includes(categoryId))
        : allStandards.filter(s => s.category === categoryId);
    const existingSummary = catStandards.map(s => {
        const rule = s.rule || {};
        return `- ${s.name} [${s.category}] (${s.severity}, ${s.enabled ? 'enabled' : 'disabled'}${(s.frameworks||[]).length ? ', frameworks: ' + s.frameworks.join(',') : ''}): ${JSON.stringify(rule)}`;
    }).join('\n');

    const catName = cat ? cat.name : categoryId;
    let prompt;
    if (isFramework) {
        prompt = `Regulatory Framework: ${catName}

This is a cross-cutting regulatory compliance framework. Standards generated for this framework should:
1. Be assigned to the appropriate TECHNICAL category (encryption, identity, network, monitoring, etc.) — NOT a "compliance" category
2. Include "${categoryId}" in their "frameworks" array
3. A single standard can satisfy multiple frameworks

Existing standards tagged with ${catName}:
${existingSummary || '(none yet)'}

Requested changes / additions:
${userRequest}

Generate standards that satisfy ${catName} requirements. Assign each standard to the correct technical category and include "${categoryId}" in the frameworks array.`;
    } else {
        prompt = `Category: ${catName}

Existing standards in this category:
${existingSummary}

Requested changes:
${userRequest}

Please generate the updated or new standards based on the changes requested above. Keep existing standards that were not mentioned. Output all standards for this category.`;
    }

    closeCategoryDetail();
    openImportStandardsModal();
    switchImportTab('paste');
    const textarea = document.getElementById('import-standards-content');
    if (textarea) {
        textarea.value = prompt;
    }
    setTimeout(() => extractStandards(), 300);
}

function importStandardsForCategory(categoryId) {
    const cat = GOV_CATEGORIES.find(c => c.id === categoryId);
    if (!cat) return;

    closeCategoryDetail();
    openImportStandardsModal();
    switchImportTab('paste');
    const textarea = document.getElementById('import-standards-content');
    if (textarea) {
        textarea.value = '';
        textarea.placeholder = `Paste your organization's ${cat.name.toLowerCase()} here...\n\nFor example:\n${cat.prompt.split('\n').slice(0, 5).join('\n')}`;
        textarea.focus();
    }
}

function filterStandards(category) {
    currentStandardsCategoryFilter = category;
    _renderCompletenessBoard();
    _renderStandardsList();
    // Scroll to the standards list
    const list = document.getElementById('standards-list');
    if (list && category !== 'all') {
        list.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
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

    // Category filter (supports both regular categories and framework IDs)
    if (currentStandardsCategoryFilter !== 'all') {
        const filterCat = GOV_CATEGORIES.find(c => c.id === currentStandardsCategoryFilter);
        if (filterCat && filterCat.group) {
            // Framework filter: show standards tagged with this framework
            filtered = filtered.filter(s => (s.frameworks || []).includes(currentStandardsCategoryFilter));
        } else {
            filtered = filtered.filter(s => s.category === currentStandardsCategoryFilter);
        }
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
            s.category.toLowerCase().includes(standardsSearchQuery) ||
            (s.risk_id || '').toLowerCase().includes(standardsSearchQuery) ||
            (s.purpose || '').toLowerCase().includes(standardsSearchQuery) ||
            (s.enforcement_tool || '').toLowerCase().includes(standardsSearchQuery)
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
        } else if (ruleType === 'naming_convention') {
            rulePreview = `Pattern: ${rule.pattern || '?'}`;
        }

        const remediationHint = rule.remediation ? `<div class="std-card-remediation" title="${escapeHtml(rule.remediation)}">💡 ${escapeHtml(rule.remediation)}</div>` : '';

        // Framework badges (show which regulatory frameworks this standard satisfies)
        const fwBadgeHtml = (std.frameworks || []).map(fw => {
            const fwCat = GOV_CATEGORIES.find(c => c.id === fw);
            return fwCat ? `<span class="std-fw-badge" title="${fwCat.name}" onclick="event.stopPropagation(); openCategoryDetail('${fw}')">${fwCat.icon}</span>` : '';
        }).join('');

        // CAF metadata badges
        const riskBadge = std.risk_id ? `<span class="std-risk-badge" title="Mitigates risk ${std.risk_id}">${escapeHtml(std.risk_id)}</span>` : '';
        const toolBadge = std.enforcement_tool ? `<span class="std-tool-badge" title="Enforced via ${escapeHtml(std.enforcement_tool)}">${escapeHtml(std.enforcement_tool)}</span>` : '';

        return `
        <div class="std-card ${enabledClass}">
            <div class="std-card-header">
                <div class="std-card-title" onclick="showStandardDetail('${escapeHtml(std.id)}')">
                    <span class="std-severity-icon">${severityIcon}</span>
                    <div class="std-name-block">
                        <span class="std-name">${escapeHtml(std.name)}</span>
                        <span class="std-id">${escapeHtml(std.id)}</span>
                    </div>
                </div>
                <div class="std-card-right">
                    <div class="std-card-badges">
                        <span class="category-badge">${escapeHtml(std.category)}</span>
                        ${riskBadge}
                        ${toolBadge}
                        <span class="std-scope-badge" title="Scope: ${escapeHtml(std.scope)}">${escapeHtml(std.scope === '*' ? 'All Services' : std.scope)}</span>
                    </div>
                    ${fwBadgeHtml ? `<div class="std-fw-badges">${fwBadgeHtml}</div>` : ''}
                    <label class="std-toggle" onclick="event.stopPropagation()" title="${std.enabled ? 'Enabled — click to disable' : 'Disabled — click to enable'}">
                        <input type="checkbox" ${std.enabled ? 'checked' : ''} onchange="toggleStandard('${escapeHtml(std.id)}', this.checked)" />
                        <span class="std-toggle-slider"></span>
                    </label>
                    <button class="std-card-delete" onclick="event.stopPropagation(); deleteStandard('${escapeHtml(std.id)}')" title="Delete standard">✕</button>
                </div>
            </div>
            <div class="std-card-body" onclick="showStandardDetail('${escapeHtml(std.id)}')">
                <div class="std-card-desc">${escapeHtml(std.description || '')}</div>
                <div class="std-card-rule"><code>${escapeHtml(rulePreview)}</code></div>
                ${remediationHint}
            </div>
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

    const rule = std.rule || {};
    const ruleType = rule.type || 'property';

    // Build human-readable rule visualization
    let ruleVisualHtml = '';
    if (ruleType === 'property') {
        ruleVisualHtml = `
            <div class="std-rule-visual">
                <div class="std-rule-row"><span class="std-rule-label">Type</span><span class="std-rule-value">Property Check</span></div>
                <div class="std-rule-row"><span class="std-rule-label">Property</span><span class="std-rule-value">${escapeHtml(rule.key || '?')}</span></div>
                <div class="std-rule-row"><span class="std-rule-label">Operator</span><span class="std-rule-value">${escapeHtml(rule.operator || '==')}</span></div>
                <div class="std-rule-row"><span class="std-rule-label">Expected</span><span class="std-rule-value">${escapeHtml(String(rule.value ?? '?'))}</span></div>
            </div>`;
    } else if (ruleType === 'tags') {
        ruleVisualHtml = `
            <div class="std-rule-visual">
                <div class="std-rule-row"><span class="std-rule-label">Type</span><span class="std-rule-value">Required Tags</span></div>
                <div class="std-rule-row"><span class="std-rule-label">Tags</span><span class="std-rule-value">${escapeHtml((rule.required_tags || []).join(', '))}</span></div>
            </div>`;
    } else if (ruleType === 'allowed_values') {
        ruleVisualHtml = `
            <div class="std-rule-visual">
                <div class="std-rule-row"><span class="std-rule-label">Type</span><span class="std-rule-value">Allowed Values</span></div>
                <div class="std-rule-row"><span class="std-rule-label">Property</span><span class="std-rule-value">${escapeHtml(rule.key || '?')}</span></div>
                <div class="std-rule-row"><span class="std-rule-label">Allowed</span><span class="std-rule-value">${escapeHtml((rule.values || []).join(', '))}</span></div>
            </div>`;
    } else if (ruleType === 'cost_threshold') {
        ruleVisualHtml = `
            <div class="std-rule-visual">
                <div class="std-rule-row"><span class="std-rule-label">Type</span><span class="std-rule-value">Cost Threshold</span></div>
                <div class="std-rule-row"><span class="std-rule-label">Max Cost</span><span class="std-rule-value">$${rule.max_monthly_usd || 0}/month</span></div>
            </div>`;
    } else if (ruleType === 'naming_convention') {
        ruleVisualHtml = `
            <div class="std-rule-visual">
                <div class="std-rule-row"><span class="std-rule-label">Type</span><span class="std-rule-value">Naming Convention</span></div>
                <div class="std-rule-row"><span class="std-rule-label">Pattern</span><span class="std-rule-value">${escapeHtml(rule.pattern || '?')}</span></div>
                ${rule.examples ? `<div class="std-rule-row"><span class="std-rule-label">Examples</span><span class="std-rule-value">${escapeHtml(rule.examples.join(', '))}</span></div>` : ''}
            </div>`;
    }

    // Remediation guidance
    const remediationHtml = rule.remediation ? `
    <div class="std-detail-section">
        <h4>Remediation Guidance</h4>
        <div class="std-remediation">
            <div class="std-remediation-label">💡 How to fix violations</div>
            <div class="std-remediation-text">${escapeHtml(rule.remediation)}</div>
        </div>
    </div>` : '';

    // Framework links
    const fwLinks = (std.frameworks || []).map(fw => {
        const fwCat = GOV_CATEGORIES.find(c => c.id === fw);
        return fwCat ? `<span class="std-fw-detail-badge" onclick="closeStandardDetail(); openCategoryDetail('${fw}')">${fwCat.icon} ${fwCat.name}</span>` : '';
    }).filter(Boolean).join('');

    const frameworksHtml = fwLinks ? `
    <div class="std-detail-section">
        <h4>Regulatory Frameworks</h4>
        <div class="std-fw-detail-list">${fwLinks}</div>
    </div>` : '';

    // CAF governance metadata section
    const hasCafFields = std.risk_id || std.purpose || std.enforcement_tool;
    const cafHtml = hasCafFields ? `
    <div class="std-detail-section std-caf-section">
        <h4>Cloud Adoption Framework</h4>
        <div class="std-caf-grid">
            ${std.risk_id ? `<div class="std-caf-row"><span class="std-caf-label">Risk ID</span><span class="std-caf-value"><span class="std-risk-badge">${escapeHtml(std.risk_id)}</span></span></div>` : ''}
            ${std.purpose ? `<div class="std-caf-row"><span class="std-caf-label">Purpose</span><span class="std-caf-value">${escapeHtml(std.purpose)}</span></div>` : ''}
            ${std.enforcement_tool ? `<div class="std-caf-row"><span class="std-caf-label">Enforcement Tool</span><span class="std-caf-value"><span class="std-tool-badge">${escapeHtml(std.enforcement_tool)}</span></span></div>` : ''}
        </div>
    </div>` : '';

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

    ${cafHtml}

    ${frameworksHtml}

    <div class="std-detail-section">
        <h4>Rule</h4>
        ${ruleVisualHtml}
        <details style="margin-top: 0.5rem;">
            <summary style="font-size: 0.72rem; color: var(--text-muted); cursor: pointer;">Show raw JSON</summary>
            <pre class="std-rule-json" style="margin-top: 0.35rem;"><code>${escapeHtml(ruleJson)}</code></pre>
        </details>
    </div>

    ${remediationHtml}

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

async function toggleStandard(standardId, enabled) {
    try {
        const res = await fetch(`/api/standards/${encodeURIComponent(standardId)}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled, change_reason: enabled ? 'Re-enabled' : 'Disabled' }),
        });
        if (!res.ok) throw new Error('Failed to toggle standard');
        // Update local state
        const std = allStandards.find(s => s.id === standardId);
        if (std) std.enabled = enabled;
        _updateGovernanceSummary();
        showToast(`${standardId} ${enabled ? 'enabled' : 'disabled'}`);
    } catch (err) {
        showToast(err.message, 'error');
        // Revert toggle in UI
        await loadStandards();
    }
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
    form.querySelector('input[name="risk_id"]').value = std.risk_id || '';
    form.querySelector('input[name="purpose"]').value = std.purpose || '';
    form.querySelector('input[name="enforcement_tool"]').value = std.enforcement_tool || '';
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
        risk_id: fd.get('risk_id') || '',
        purpose: fd.get('purpose') || '',
        enforcement_tool: fd.get('enforcement_tool') || '',
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


async function clearAllStandards() {
    const count = allStandards.length;
    if (!count) { showToast('No standards to delete', 'info'); return; }
    if (!confirm(`Delete ALL ${count} standards?\n\nThis permanently removes every standard and its version history. This cannot be undone.`)) return;

    try {
        const res = await fetch('/api/standards/bulk-delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ all: true }),
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({ detail: 'Failed' }));
            throw new Error(err.detail || 'Failed to delete');
        }
        const data = await res.json();
        showToast(`Deleted ${data.deleted} standards`, 'success');
        closeStandardDetail();
        await loadStandards();
    } catch (err) {
        showToast(err.message, 'error');
    }
}


// ═══════════════════════════════════════════════════════════════
//  GOVERNANCE CHAT — Governance Advisor Agent
// ═══════════════════════════════════════════════════════════════

let _govChatWs = null;
let _govChatStreaming = false;
let _govChatStreamDiv = null;
let _govChatStreamContent = '';
let _govChatOpen = false;

function toggleGovernanceChat() {
    const drawer = document.getElementById('gov-chat-drawer');
    if (!drawer) return;
    _govChatOpen = !_govChatOpen;
    drawer.classList.toggle('hidden', !_govChatOpen);

    if (_govChatOpen) {
        _connectGovernanceChat();
        setTimeout(() => {
            const input = document.getElementById('gov-chat-input');
            if (input) input.focus();
        }, 100);
    }
}

function _connectGovernanceChat() {
    if (_govChatWs && _govChatWs.readyState === WebSocket.OPEN) return;

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws/governance-chat`;

    _govChatWs = new WebSocket(wsUrl);

    _govChatWs.onopen = () => {
        _govChatWs.send(JSON.stringify({
            type: 'auth',
            sessionToken: sessionToken,
        }));
    };

    _govChatWs.onmessage = (event) => {
        const data = JSON.parse(event.data);
        _handleGovChatMessage(data);
    };

    _govChatWs.onclose = () => {
        _govChatWs = null;
        // Reconnect if drawer is still open
        if (_govChatOpen) {
            setTimeout(() => _connectGovernanceChat(), 3000);
        }
    };

    _govChatWs.onerror = () => {
        _govChatWs = null;
    };
}

function _handleGovChatMessage(data) {
    switch (data.type) {
        case 'auth_ok':
            break;
        case 'delta':
            _handleGovStreamDelta(data.content);
            break;
        case 'done':
            _handleGovStreamDone(data.content);
            break;
        case 'tool_call':
            _handleGovToolCall(data.name, data.status);
            break;
        case 'error':
            _handleGovError(data.message);
            break;
        case 'pong':
            break;
    }
}

function _addGovMessage(role, content, isStreaming = false) {
    const container = document.getElementById('gov-chat-messages');

    const msgDiv = document.createElement('div');
    msgDiv.className = `gov-msg gov-msg-${role}`;

    const avatar = document.createElement('div');
    avatar.className = 'gov-msg-avatar';
    avatar.textContent = role === 'user'
        ? (currentUser ? currentUser.displayName.split(' ').map(n => n[0]).join('').substring(0, 2) : '?')
        : '📜';

    const contentDiv = document.createElement('div');
    contentDiv.className = 'gov-msg-content';

    const textDiv = document.createElement('div');
    textDiv.className = 'gov-msg-text';

    if (isStreaming) {
        textDiv.classList.add('streaming-cursor');
    } else {
        textDiv.innerHTML = renderMarkdown(content);
    }

    contentDiv.appendChild(textDiv);
    msgDiv.appendChild(avatar);
    msgDiv.appendChild(contentDiv);
    container.appendChild(msgDiv);

    container.scrollTop = container.scrollHeight;
    return textDiv;
}

function _handleGovStreamDelta(content) {
    if (!_govChatStreamDiv) return;
    _govChatStreamContent += content;
    _govChatStreamDiv.innerHTML = renderMarkdown(_govChatStreamContent);
    _govChatStreamDiv.classList.add('streaming-cursor');
    const container = document.getElementById('gov-chat-messages');
    container.scrollTop = container.scrollHeight;
}

function _handleGovStreamDone(fullContent) {
    if (_govChatStreamDiv) {
        _govChatStreamDiv.classList.remove('streaming-cursor');
        const finalContent = fullContent || _govChatStreamContent;
        _govChatStreamDiv.innerHTML = renderMarkdown(finalContent);
    }
    _govChatStreamDiv = null;
    _govChatStreamContent = '';
    _govChatStreaming = false;
    document.getElementById('gov-chat-send').disabled = false;
    document.getElementById('gov-chat-input').focus();

    // Hide tool activity
    const toolEl = document.getElementById('gov-chat-tool-activity');
    if (toolEl) toolEl.classList.add('hidden');

    const container = document.getElementById('gov-chat-messages');
    container.scrollTop = container.scrollHeight;
}

function _handleGovToolCall(name, status) {
    const toolEl = document.getElementById('gov-chat-tool-activity');
    const textEl = document.getElementById('gov-chat-tool-text');
    if (!toolEl || !textEl) return;

    const toolLabels = {
        'list_governance_policies': 'Querying governance policies…',
        'list_security_standards': 'Querying security standards…',
        'list_compliance_frameworks': 'Querying compliance frameworks…',
        'request_policy_modification': 'Submitting policy modification request…',
    };

    if (status === 'running') {
        textEl.textContent = toolLabels[name] || `Running ${name}…`;
        toolEl.classList.remove('hidden');
    } else {
        toolEl.classList.add('hidden');
    }
}

function _handleGovError(message) {
    _addGovMessage('assistant', `⚠️ ${message}`);
    _govChatStreaming = false;
    document.getElementById('gov-chat-send').disabled = false;
}

function sendGovMessage() {
    const input = document.getElementById('gov-chat-input');
    const text = input.value.trim();

    if (!text || _govChatStreaming || !_govChatWs || _govChatWs.readyState !== WebSocket.OPEN) return;

    // Hide welcome
    const welcome = document.getElementById('gov-chat-welcome');
    if (welcome) welcome.classList.add('hidden');

    // Add user message
    _addGovMessage('user', text);

    // Send via WebSocket
    _govChatWs.send(JSON.stringify({ type: 'message', content: text }));

    // Clear input
    input.value = '';
    input.style.height = 'auto';
    _govChatStreaming = true;
    document.getElementById('gov-chat-send').disabled = true;

    // Create placeholder for assistant response
    _govChatStreamContent = '';
    _govChatStreamDiv = _addGovMessage('assistant', '', true);
}

function sendGovQuickAction(prompt) {
    const input = document.getElementById('gov-chat-input');
    input.value = prompt;
    sendGovMessage();
}

function handleGovChatKeyDown(event) {
    if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        sendGovMessage();
    }
}

function clearGovernanceChat() {
    const container = document.getElementById('gov-chat-messages');
    container.innerHTML = '';

    // Re-add welcome
    container.innerHTML = `
        <div class="gov-chat-welcome" id="gov-chat-welcome">
            <div class="gov-chat-welcome-icon">📜</div>
            <h4>Governance Advisor</h4>
            <p>I can help you understand policies, find standards, and request policy modifications.</p>
            <div class="gov-chat-suggestions">
                <button class="gov-chat-suggestion" onclick="sendGovQuickAction('What governance policies do we have?')">📋 List all policies</button>
                <button class="gov-chat-suggestion" onclick="sendGovQuickAction('Do we have any rules about public IP addresses?')">🌐 Public IP rules</button>
                <button class="gov-chat-suggestion" onclick="sendGovQuickAction('What security standards cover encryption?')">🔐 Encryption standards</button>
                <button class="gov-chat-suggestion" onclick="sendGovQuickAction('What compliance frameworks are configured?')">📋 Compliance frameworks</button>
            </div>
        </div>
    `;

    _govChatStreamDiv = null;
    _govChatStreamContent = '';
    _govChatStreaming = false;

    // Close and reconnect for a fresh session
    if (_govChatWs) {
        _govChatWs.close();
        _govChatWs = null;
    }
    setTimeout(() => _connectGovernanceChat(), 300);
}


// ═══════════════════════════════════════════════════════════════
//  CONCIERGE / CISO CHAT — Always-available assistant
// ═══════════════════════════════════════════════════════════════

let _conChatWs = null;
let _conChatStreaming = false;
let _conChatStreamDiv = null;
let _conChatStreamContent = '';
let _conChatOpen = false;

function toggleConcierge() {
    const drawer = document.getElementById('concierge-drawer');
    const fab = document.getElementById('concierge-fab');
    if (!drawer) return;
    _conChatOpen = !_conChatOpen;
    drawer.classList.toggle('hidden', !_conChatOpen);
    if (fab) fab.classList.toggle('fab-active', _conChatOpen);

    if (_conChatOpen) {
        _connectConcierge();
        setTimeout(() => {
            const input = document.getElementById('concierge-input');
            if (input) input.focus();
        }, 100);
    }
}

function _connectConcierge() {
    if (_conChatWs && _conChatWs.readyState === WebSocket.OPEN) return;

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws/concierge-chat`;

    _conChatWs = new WebSocket(wsUrl);

    _conChatWs.onopen = () => {
        _conChatWs.send(JSON.stringify({
            type: 'auth',
            sessionToken: sessionToken,
        }));
    };

    _conChatWs.onmessage = (event) => {
        const data = JSON.parse(event.data);
        _handleConciergeMessage(data);
    };

    _conChatWs.onclose = () => {
        _conChatWs = null;
        if (_conChatOpen) {
            setTimeout(() => _connectConcierge(), 3000);
        }
    };

    _conChatWs.onerror = () => {
        _conChatWs = null;
    };
}

function _handleConciergeMessage(data) {
    switch (data.type) {
        case 'auth_ok':
            break;
        case 'delta':
            _handleConStreamDelta(data.content);
            break;
        case 'done':
            _handleConStreamDone(data.content);
            break;
        case 'tool_call':
            _handleConToolCall(data.name, data.status);
            break;
        case 'error':
            _handleConError(data.message);
            break;
        case 'pong':
            break;
    }
}

function _addConMessage(role, content, isStreaming = false) {
    const container = document.getElementById('concierge-messages');

    const msgDiv = document.createElement('div');
    msgDiv.className = `con-msg con-msg-${role}`;

    const avatar = document.createElement('div');
    avatar.className = 'con-msg-avatar';
    avatar.textContent = role === 'user'
        ? (currentUser ? currentUser.displayName.split(' ').map(n => n[0]).join('').substring(0, 2) : '?')
        : '🛡️';

    const contentDiv = document.createElement('div');
    contentDiv.className = 'con-msg-content';

    const textDiv = document.createElement('div');
    textDiv.className = 'con-msg-text';

    if (isStreaming) {
        textDiv.classList.add('streaming-cursor');
    } else {
        textDiv.innerHTML = renderMarkdown(content);
    }

    contentDiv.appendChild(textDiv);
    msgDiv.appendChild(avatar);
    msgDiv.appendChild(contentDiv);
    container.appendChild(msgDiv);

    container.scrollTop = container.scrollHeight;
    return textDiv;
}

function _handleConStreamDelta(content) {
    if (!_conChatStreamDiv) return;
    _conChatStreamContent += content;
    _conChatStreamDiv.innerHTML = renderMarkdown(_conChatStreamContent);
    _conChatStreamDiv.classList.add('streaming-cursor');
    const container = document.getElementById('concierge-messages');
    container.scrollTop = container.scrollHeight;
}

function _handleConStreamDone(fullContent) {
    if (_conChatStreamDiv) {
        _conChatStreamDiv.classList.remove('streaming-cursor');
        const finalContent = fullContent || _conChatStreamContent;
        _conChatStreamDiv.innerHTML = renderMarkdown(finalContent);
    }
    _conChatStreamDiv = null;
    _conChatStreamContent = '';
    _conChatStreaming = false;
    document.getElementById('concierge-send').disabled = false;
    document.getElementById('concierge-input').focus();

    const toolEl = document.getElementById('concierge-tool-activity');
    if (toolEl) toolEl.classList.add('hidden');

    const container = document.getElementById('concierge-messages');
    container.scrollTop = container.scrollHeight;
}

function _handleConToolCall(name, status) {
    const toolEl = document.getElementById('concierge-tool-activity');
    const textEl = document.getElementById('concierge-tool-text');
    if (!toolEl || !textEl) return;

    const toolLabels = {
        'list_governance_policies': 'Querying governance policies…',
        'list_security_standards': 'Querying security standards…',
        'list_compliance_frameworks': 'Querying compliance frameworks…',
        'check_service_approval': 'Checking service approval status…',
        'list_approved_services': 'Browsing service catalog…',
        'modify_governance_policy': '🛡️ Modifying policy…',
        'toggle_policy': '🛡️ Toggling policy…',
        'grant_policy_exception': '🔓 Granting policy exception…',
        'list_policy_exceptions': 'Checking policy exceptions…',
    };

    if (status === 'running') {
        textEl.textContent = toolLabels[name] || `Running ${name}…`;
        toolEl.classList.remove('hidden');
    } else {
        toolEl.classList.add('hidden');
    }
}

function _handleConError(message) {
    _addConMessage('assistant', `⚠️ ${message}`);
    _conChatStreaming = false;
    document.getElementById('concierge-send').disabled = false;
}

function sendConciergeMessage() {
    const input = document.getElementById('concierge-input');
    const text = input.value.trim();

    if (!text || _conChatStreaming || !_conChatWs || _conChatWs.readyState !== WebSocket.OPEN) return;

    // Hide welcome
    const welcome = document.getElementById('concierge-welcome');
    if (welcome) welcome.classList.add('hidden');

    // Add user message
    _addConMessage('user', text);

    // Send via WebSocket
    _conChatWs.send(JSON.stringify({ type: 'message', content: text }));

    // Clear input
    input.value = '';
    input.style.height = 'auto';
    _conChatStreaming = true;
    document.getElementById('concierge-send').disabled = true;

    // Create placeholder for assistant response
    _conChatStreamContent = '';
    _conChatStreamDiv = _addConMessage('assistant', '', true);
}

function sendConciergeQuickAction(prompt) {
    const input = document.getElementById('concierge-input');
    input.value = prompt;
    sendConciergeMessage();
}

function handleConciergeKeyDown(event) {
    if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        sendConciergeMessage();
    }
}

function clearConciergeChat() {
    const container = document.getElementById('concierge-messages');
    container.innerHTML = '';

    // Re-add welcome
    container.innerHTML = `
        <div class="concierge-welcome" id="concierge-welcome">
            <div class="concierge-welcome-icon">🛡️</div>
            <h4>How can I help?</h4>
            <p>I'm your InfraForge concierge with CISO authority. Ask about policies, raise concerns, or get help with anything on the platform.</p>
            <div class="concierge-suggestions">
                <button class="concierge-suggestion" onclick="sendConciergeQuickAction('I want to add an Azure Firewall but I\\'m getting a policy error. Can you help?')">🔥 Policy blocking my Firewall</button>
                <button class="concierge-suggestion" onclick="sendConciergeQuickAction('I think our public IP policy is too restrictive and blocking productivity. Can you review it?')">⚖️ Policy is too restrictive</button>
                <button class="concierge-suggestion" onclick="sendConciergeQuickAction('What governance policies are currently active?')">📋 Show active policies</button>
                <button class="concierge-suggestion" onclick="sendConciergeQuickAction('Are there any active policy exceptions right now?')">🔓 Check policy exceptions</button>
            </div>
        </div>
    `;

    _conChatStreamDiv = null;
    _conChatStreamContent = '';
    _conChatStreaming = false;

    // Close and reconnect for a fresh session
    if (_conChatWs) {
        _conChatWs.close();
        _conChatWs = null;
    }
    setTimeout(() => _connectConcierge(), 300);
}


// ── Standards Import ─────────────────────────────────────────

let _importedStandards = [];
let _importActiveTab = 'paste';
let _importFileContent = '';

function openImportStandardsModal() {
    _importedStandards = [];
    _importFileContent = '';
    _importActiveTab = 'paste';
    document.getElementById('import-standards-content').value = '';
    document.getElementById('import-standards-preview').classList.add('hidden');
    document.getElementById('import-standards-list').innerHTML = '';
    document.getElementById('btn-extract-standards').classList.remove('hidden');
    document.getElementById('btn-save-imported-standards').classList.add('hidden');
    document.getElementById('btn-extract-standards').disabled = false;
    document.getElementById('btn-extract-standards').textContent = '🤖 Extract Standards';
    // Reset file upload
    const fileInfo = document.getElementById('import-file-info');
    if (fileInfo) fileInfo.classList.add('hidden');
    const fileInput = document.getElementById('import-file-input');
    if (fileInput) fileInput.value = '';
    // Reset tabs
    switchImportTab('paste');
    openModal('modal-import-standards');
}

function switchImportTab(tab) {
    _importActiveTab = tab;
    document.querySelectorAll('.import-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.import-source-content').forEach(c => c.classList.add('hidden'));
    const tabBtn = document.getElementById(`import-tab-${tab}`);
    const content = document.getElementById(`import-source-${tab}`);
    if (tabBtn) tabBtn.classList.add('active');
    if (content) content.classList.remove('hidden');
}

function handleImportFileDrop(event) {
    event.preventDefault();
    event.target.closest('.import-upload-zone').classList.remove('drag-over');
    const file = event.dataTransfer?.files?.[0];
    if (file) _processImportFile(file);
}

function handleImportFileSelect(event) {
    const file = event.target.files?.[0];
    if (file) _processImportFile(file);
}

async function _processImportFile(file) {
    const maxSize = 5 * 1024 * 1024; // 5MB
    if (file.size > maxSize) {
        showToast('File too large (max 5MB)', 'error');
        return;
    }

    try {
        const text = await file.text();
        _importFileContent = text;
        document.getElementById('import-file-name').textContent = `📄 ${file.name} (${(file.size / 1024).toFixed(1)} KB)`;
        document.getElementById('import-file-info').classList.remove('hidden');
        showToast(`Loaded ${file.name}`);
    } catch (err) {
        showToast(`Failed to read file: ${err.message}`, 'error');
    }
}

function clearImportFile() {
    _importFileContent = '';
    document.getElementById('import-file-info').classList.add('hidden');
    document.getElementById('import-file-input').value = '';
}

function selectAllImports(checked) {
    _importedStandards.forEach(s => s._include = checked);
    _renderImportPreview(_importedStandards);
}

async function extractStandards() {
    // Get content from active tab
    let content = '';
    if (_importActiveTab === 'paste') {
        content = document.getElementById('import-standards-content').value.trim();
    } else {
        content = _importFileContent.trim();
    }

    if (!content) {
        showToast(_importActiveTab === 'paste' ? 'Please paste your standards documentation first' : 'Please upload a file first', 'error');
        return;
    }

    const btn = document.getElementById('btn-extract-standards');
    btn.disabled = true;
    btn.textContent = '🔄 Extracting…';

    try {
        const res = await fetch('/api/standards/import', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ content, source_type: _importActiveTab === 'file' ? 'markdown' : 'text', save: false }),
        });

        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Import failed');
        }

        const data = await res.json();
        _importedStandards = data.standards || [];
        _importedStandards.forEach(s => s._include = true);

        if (_importedStandards.length === 0) {
            showToast('No standards could be extracted from the document', 'error');
            btn.disabled = false;
            btn.textContent = '🤖 Extract Standards';
            return;
        }

        // Render preview
        _renderImportPreview(_importedStandards);
        document.getElementById('import-standards-preview').classList.remove('hidden');
        const countEl = document.getElementById('import-count');
        if (countEl) countEl.textContent = _importedStandards.length;
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
        const included = std._include !== false;
        return `
        <div class="import-std-card ${included ? '' : 'excluded'}">
            <div class="import-std-header">
                <span class="import-std-name">${icon} ${escapeHtml(std.name)}</span>
                <div class="import-std-controls">
                    <span class="badge badge-${std.severity}" style="font-size: 0.68rem;">${std.severity}</span>
                    <span class="category-badge" style="font-size: 0.68rem;">${escapeHtml(std.category)}</span>
                    <label class="std-toggle" title="${included ? 'Included' : 'Excluded'}">
                        <input type="checkbox" ${included ? 'checked' : ''} onchange="_toggleImportStd(${i}, this.checked)" />
                        <span class="std-toggle-slider"></span>
                    </label>
                </div>
            </div>
            <div class="import-std-desc">${escapeHtml(std.description || '')}</div>
            <div class="import-std-meta">
                <span title="Rule type">📏 ${ruleType}</span>
                <span title="Scope">🎯 ${escapeHtml(std.scope || '*')}</span>
                <span title="ID">🏷️ ${escapeHtml(std.id)}</span>
            </div>
            <div class="import-std-rule">${ruleDesc}</div>
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
        case 'naming_convention':
            return `Naming pattern: <code>${escapeHtml(rule.pattern || '?')}</code>${rule.examples ? ` (e.g. ${rule.examples.map(e => `<code>${escapeHtml(e)}</code>`).join(', ')})` : ''}`;
        default:
            return JSON.stringify(rule).substring(0, 120);
    }
}

function _toggleImportStd(index, checked) {
    if (_importedStandards[index]) {
        _importedStandards[index]._include = checked;
        _renderImportPreview(_importedStandards);
        // Update count
        const selected = _importedStandards.filter(s => s._include !== false).length;
        const countEl = document.getElementById('import-count');
        if (countEl) countEl.textContent = `${selected}/${_importedStandards.length}`;
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
    if (tab === 'azure-resources') loadAzureResources();
    if (tab === 'data-mgmt') loadBackupsList();
    if (tab === 'agents') loadAgentActivity();
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
    const tornDown = deployments.filter(d => d.status === 'torn_down').length;
    const el = (id, val) => { const e = document.getElementById(id); if (e) e.textContent = val; };
    el('obs-deployments-total', total);
    el('obs-deployments-succeeded', succeeded);
    el('obs-deployments-failed', failed);
    el('obs-deployments-torn-down', tornDown);

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
        case 'torn_down':
            statusClass = 'obs-deploy-torn-down'; statusIcon = '🗑️'; statusLabel = 'Torn Down'; break;
        case 'deploying':
            statusClass = 'obs-deploy-running'; statusIcon = '⏳'; statusLabel = 'Deploying'; break;
        case 'validating':
            statusClass = 'obs-deploy-running'; statusIcon = '🔍'; statusLabel = 'Validating'; break;
        case 'tearing_down':
            statusClass = 'obs-deploy-running'; statusIcon = '🔄'; statusLabel = 'Tearing Down'; break;
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

    // Teardown button — only for succeeded or failed deployments
    let teardownHtml = '';
    if (dep.status === 'succeeded' || dep.status === 'failed') {
        teardownHtml = `<div class="obs-deploy-actions"><button class="btn btn-sm btn-danger obs-teardown-btn" onclick="teardownDeployment('${escapeHtml(dep.deployment_id)}')" title="Delete all resources in this deployment">🗑️ Tear Down</button></div>`;
    } else if (dep.status === 'torn_down') {
        const tdAt = dep.torn_down_at ? new Date(dep.torn_down_at).toLocaleString() : '';
        teardownHtml = `<div class="obs-deploy-torn-info">🗑️ Torn down ${tdAt ? 'on ' + tdAt : ''}</div>`;
    } else if (dep.status === 'tearing_down') {
        teardownHtml = `<div class="obs-deploy-torn-info">🔄 Teardown in progress…</div>`;
    }

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
        ${teardownHtml}
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

async function teardownDeployment(deploymentId) {
    if (!confirm('⚠️ This will permanently delete the resource group and ALL resources in this deployment. This cannot be undone.\n\nContinue?')) {
        return;
    }
    // Find and disable the button
    const btns = document.querySelectorAll('.obs-teardown-btn');
    btns.forEach(b => {
        if (b.onclick && b.getAttribute('onclick')?.includes(deploymentId)) {
            b.disabled = true;
            b.textContent = '🔄 Tearing down…';
        }
    });

    try {
        const res = await fetch(`/api/deployments/${deploymentId}/teardown`, { method: 'POST' });
        const data = await res.json();
        if (!res.ok) {
            alert(`Teardown failed: ${data.detail || 'Unknown error'}`);
            return;
        }
        // Refresh the deployment list
        await loadDeploymentHistory();
    } catch (err) {
        alert(`Teardown failed: ${err.message}`);
    }
}

// ── Azure Managed Resources ─────────────────────────────────

let _azureRgCache = null;

async function loadAzureResources() {
    const feed = document.getElementById('azure-rg-feed');
    const refreshBtn = document.getElementById('azure-rg-refresh-btn');
    if (refreshBtn) { refreshBtn.disabled = true; refreshBtn.textContent = '⏳ Scanning…'; }
    feed.innerHTML = '<div class="activity-empty"><span class="activity-empty-icon">⏳</span><p>Scanning Azure subscription for resource groups…</p></div>';

    const _el = (id, val) => { const e = document.getElementById(id); if (e) e.textContent = val; };

    try {
        const res = await fetch('/api/azure/resource-groups');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        _azureRgCache = data;

        // Update summary counters
        const managed = data.managed || [];
        const validationRGs = managed.filter(r => r.rg_type === 'validation');
        const deploymentRGs = managed.filter(r => r.rg_type === 'deployment');

        _el('azure-rg-managed-count', managed.length);
        _el('azure-rg-validation-count', validationRGs.length);
        _el('azure-rg-deployment-count', deploymentRGs.length);
        _el('azure-rg-total-count', data.total || 0);

        const subEl = document.getElementById('azure-rg-sub');
        if (subEl) subEl.textContent = `Subscription: ${data.subscription_id || ''}`;

        // Show/hide cleanup button
        const cleanupBtn = document.getElementById('azure-rg-cleanup-btn');
        if (cleanupBtn) cleanupBtn.style.display = validationRGs.length > 0 ? '' : 'none';

        _renderAzureResourceGroups(managed, data.unmanaged || []);
    } catch (err) {
        feed.innerHTML = `<div class="activity-empty"><span class="activity-empty-icon">❌</span><p>Failed to load Azure resources: ${escapeHtml(err.message)}</p></div>`;
    } finally {
        if (refreshBtn) { refreshBtn.disabled = false; refreshBtn.textContent = '🔄 Refresh'; }
    }
}

function _renderAzureResourceGroups(managed, unmanaged) {
    const feed = document.getElementById('azure-rg-feed');
    if (!managed.length && !unmanaged.length) {
        feed.innerHTML = '<div class="activity-empty"><span class="activity-empty-icon">☁️</span><p>No resource groups found in this subscription.</p></div>';
        return;
    }

    let html = '';

    // Split managed into validation (orphaned) vs deployment (active)
    const validationRGs = managed.filter(r => r.rg_type === 'validation');
    const deploymentRGs = managed.filter(r => r.rg_type !== 'validation');

    // ── 1:Many Template → Resource Groups mapping ──
    if (deploymentRGs.length) {
        // Group deployments by template_name (or "Ungrouped" if no template)
        const byTemplate = {};
        for (const rg of deploymentRGs) {
            const tmplName = rg.deployment?.template_name || 'Ungrouped Deployments';
            const tmplId = rg.deployment?.template_id || '';
            const tmplSemver = rg.deployment?.template_semver || '';
            const key = tmplId || tmplName;
            if (!byTemplate[key]) {
                byTemplate[key] = { name: tmplName, id: tmplId, semver: tmplSemver, rgs: [] };
            }
            // Keep the latest semver if multiple RGs have different versions
            if (tmplSemver && !byTemplate[key].semver) byTemplate[key].semver = tmplSemver;
            byTemplate[key].rgs.push(rg);
        }

        const groups = Object.values(byTemplate);
        // Sort groups by name
        groups.sort((a, b) => a.name.localeCompare(b.name));

        html += `<div class="azure-rg-section">
            <h4 class="azure-rg-section-title">🚀 InfraForge Deployments <span class="azure-rg-section-count">${deploymentRGs.length}</span></h4>
            <p class="azure-rg-section-desc">Resource groups created by InfraForge, grouped by template. Each template can have one or more resource groups deployed from it.</p>`;

        for (const group of groups) {
            const totalResources = group.rgs.reduce((sum, rg) => sum + (rg.resource_count || 0), 0);
            html += `<div class="azure-tmpl-group">
                <div class="azure-tmpl-group-header">
                    <span class="azure-tmpl-group-icon">📋</span>
                    <span class="azure-tmpl-group-name">${escapeHtml(group.name)}</span>
                    ${group.semver ? `<span class="azure-tmpl-group-version">v${escapeHtml(group.semver)}</span>` : ''}
                    <span class="azure-tmpl-group-stats">
                        <span class="azure-tmpl-group-rg-count">${group.rgs.length} resource group${group.rgs.length !== 1 ? 's' : ''}</span>
                        ${totalResources ? `<span class="azure-tmpl-group-res-count">${totalResources} resource${totalResources !== 1 ? 's' : ''}</span>` : ''}
                    </span>
                </div>
                <div class="azure-tmpl-group-rgs">
                    ${group.rgs.map(rg => _renderAzureRGCard(rg, true)).join('')}
                </div>
            </div>`;
        }
        html += '</div>';
    }

    if (validationRGs.length) {
        html += '<div class="azure-rg-section azure-rg-section-warn"><h4 class="azure-rg-section-title">🧪 Orphaned Validation Groups <span class="azure-rg-section-count azure-rg-section-count-warn">' + validationRGs.length + '</span></h4>';
        html += '<p class="azure-rg-section-desc">Leftover resource groups from onboarding validation runs. These are safe to delete — use <b>Cleanup Orphaned</b> to remove them all.</p>';
        html += validationRGs.map(rg => _renderAzureRGCard(rg, true)).join('');
        html += '</div>';
    }

    if (!managed.length) {
        html += '<div class="azure-rg-section"><h4 class="azure-rg-section-title">🔗 InfraForge-Managed</h4>';
        html += '<p class="azure-rg-section-desc" style="color:var(--text-secondary);">No InfraForge-managed resource groups found. Deploy a template to create one.</p></div>';
    }

    if (unmanaged.length) {
        html += `<details class="azure-rg-section azure-rg-unmanaged-details">
            <summary class="azure-rg-section-title azure-rg-unmanaged-summary">📁 Pre-Existing Resource Groups — Not Managed by InfraForge <span class="azure-rg-section-count">${unmanaged.length}</span></summary>
            <p class="azure-rg-section-desc">These resource groups existed before InfraForge or were created outside of InfraForge. They are shown for reference only — InfraForge does not manage or modify them.</p>`;
        html += unmanaged.map(rg => _renderAzureRGCard(rg, false)).join('');
        html += '</details>';
    }

    feed.innerHTML = html;
}

function _renderAzureRGCard(rg, isManaged) {
    const typeIcons = { validation: '🧪', deployment: '🚀', unknown: '📁' };
    const typeLabels = { validation: 'Validation', deployment: 'Deployment', unknown: 'Resource Group' };
    const typeIcon = typeIcons[rg.rg_type] || '📁';
    const typeLabel = typeLabels[rg.rg_type] || 'Resource Group';

    const provState = rg.provisioning_state || 'Unknown';
    const stateClass = provState === 'Succeeded' ? 'azure-rg-state-ok'
        : provState === 'Deleting' ? 'azure-rg-state-deleting'
        : 'azure-rg-state-other';

    // Deployment link if available
    let depHtml = '';
    if (rg.deployment) {
        const d = rg.deployment;
        const statusIcons = { succeeded: '✅', failed: '❌', torn_down: '🗑️', deploying: '⏳' };
        const semverLabel = d.template_semver ? ` · v${escapeHtml(d.template_semver)}` : '';
        depHtml = `<div class="azure-rg-deploy-link">
            <span class="azure-rg-deploy-status">${statusIcons[d.status] || '❓'} ${d.status}</span>
            ${d.template_name ? `<span class="azure-rg-deploy-name">${escapeHtml(d.template_name)}${semverLabel}</span>` : ''}
            ${d.started_at ? `<span class="azure-rg-deploy-time">${new Date(d.started_at).toLocaleDateString()}</span>` : ''}
        </div>`;
    }

    // Resource count
    const resCount = rg.resource_count !== undefined ? `<span class="azure-rg-res-count" title="Resources in this RG">${rg.resource_count} resource${rg.resource_count !== 1 ? 's' : ''}</span>` : '';

    // Tags (show a few key ones)
    const tags = rg.tags || {};
    const tagKeys = Object.keys(tags).slice(0, 4);
    const tagsHtml = tagKeys.length
        ? `<div class="azure-rg-tags">${tagKeys.map(k => `<span class="azure-rg-tag">${escapeHtml(k)}: ${escapeHtml(String(tags[k]).substring(0, 30))}</span>`).join('')}</div>`
        : '';

    // Actions
    let actionsHtml = '';
    if (isManaged && provState !== 'Deleting') {
        actionsHtml = `<div class="azure-rg-actions">
            <button class="btn btn-sm btn-danger azure-rg-delete-btn" onclick="deleteAzureRG('${escapeHtml(rg.name)}')" title="Delete this resource group">🗑️ Delete</button>
        </div>`;
    }

    return `
    <div class="azure-rg-card ${isManaged ? 'azure-rg-managed' : 'azure-rg-unmanaged'} azure-rg-type-${rg.rg_type}">
        <div class="azure-rg-header">
            <div class="azure-rg-title">
                <span class="azure-rg-icon">${typeIcon}</span>
                <div class="azure-rg-name-block">
                    <span class="azure-rg-name">${escapeHtml(rg.name)}</span>
                    <span class="azure-rg-type-label">${typeLabel}</span>
                </div>
            </div>
            <div class="azure-rg-meta-right">
                <span class="azure-rg-state ${stateClass}">${provState}</span>
                ${resCount}
            </div>
        </div>
        <div class="azure-rg-details">
            <span class="azure-rg-location">📍 ${escapeHtml(rg.location)}</span>
            ${depHtml}
        </div>
        ${tagsHtml}
        ${actionsHtml}
    </div>`;
}

async function deleteAzureRG(rgName) {
    if (!confirm(`⚠️ Delete resource group "${rgName}" and ALL its resources?\n\nThis cannot be undone.`)) return;

    // Disable the button
    const btns = document.querySelectorAll('.azure-rg-delete-btn');
    btns.forEach(b => {
        if (b.getAttribute('onclick')?.includes(rgName)) {
            b.disabled = true;
            b.textContent = '🔄 Deleting…';
        }
    });

    try {
        const res = await fetch(`/api/azure/resource-groups/${encodeURIComponent(rgName)}`, { method: 'DELETE' });
        if (!res.ok) {
            const data = await res.json().catch(() => ({}));
            alert(`Delete failed: ${data.detail || 'Unknown error'}`);
            return;
        }
        showToast(`✅ Resource group "${rgName}" deleted`, 'success');
        await loadAzureResources();
    } catch (err) {
        alert(`Delete failed: ${err.message}`);
    }
}

async function cleanupOrphanedRGs() {
    const validationCount = _azureRgCache?.managed?.filter(r => r.rg_type === 'validation').length || 0;
    if (!confirm(`🧹 Delete all ${validationCount} orphaned validation resource group(s)?\n\nThese are leftover from onboarding validation runs and are no longer needed.\n\nThis cannot be undone.`)) return;

    const btn = document.getElementById('azure-rg-cleanup-btn');
    if (btn) { btn.disabled = true; btn.textContent = '⏳ Cleaning up…'; }

    try {
        const res = await fetch('/api/azure/resource-groups/cleanup', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ type: 'validation' }),
        });
        const data = await res.json();
        const msg = data.message || `Deleted ${data.total_deleted || 0} resource group(s)`;
        showToast(`🧹 ${msg}`, 'success');
        await loadAzureResources();
    } catch (err) {
        alert(`Cleanup failed: ${err.message}`);
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = '🧹 Cleanup Orphaned'; }
    }
}

// ── Data Management: Backup & Restore ───────────────────────

async function createBackup() {
    const btn = document.getElementById('backup-create-btn');
    const statusEl = document.getElementById('backup-status');
    const includeSessions = document.getElementById('backup-include-sessions')?.checked || false;

    if (btn) { btn.disabled = true; btn.textContent = '⏳ Creating…'; }
    statusEl.innerHTML = '<span class="data-mgmt-progress">⏳ Creating backup…</span>';

    try {
        const res = await fetch('/api/admin/backup', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ include_sessions: includeSessions, save_to_disk: true }),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        const meta = data.metadata || {};
        statusEl.innerHTML = `<div class="data-mgmt-success">
            ✅ Backup created: <strong>${meta.total_rows || 0}</strong> rows across <strong>${meta.tables_backed_up || 0}</strong> tables.
            ${data.filepath ? `<br><code>${escapeHtml(data.filepath)}</code>` : ''}
        </div>`;
        showToast('✅ Backup created successfully', 'success');
        loadBackupsList();
    } catch (err) {
        statusEl.innerHTML = `<div class="data-mgmt-error">❌ Backup failed: ${escapeHtml(err.message)}</div>`;
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = '📦 Create Backup'; }
    }
}

async function downloadBackup() {
    const btn = document.getElementById('backup-download-btn');
    const includeSessions = document.getElementById('backup-include-sessions')?.checked || false;

    if (btn) { btn.disabled = true; btn.textContent = '⏳ Preparing…'; }

    try {
        const url = `/api/admin/backup/download?include_sessions=${includeSessions}`;
        const res = await fetch(url);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);

        const blob = await res.blob();
        const disposition = res.headers.get('Content-Disposition') || '';
        const match = disposition.match(/filename="(.+)"/);
        const filename = match ? match[1] : 'infraforge_backup.json';

        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(a.href);
        showToast('⬇️ Backup downloaded', 'success');
    } catch (err) {
        alert('Download failed: ' + err.message);
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = '⬇️ Download Backup'; }
    }
}

async function handleRestoreFileSelected(event) {
    const file = event.target.files?.[0];
    if (!file) return;

    const statusEl = document.getElementById('restore-status');
    const mode = document.getElementById('restore-mode')?.value || 'replace';
    const modeLabel = mode === 'replace' ? 'REPLACE all data' : 'MERGE (skip conflicts)';

    if (!confirm(`⚠️ Restore database from "${file.name}"?\n\nMode: ${modeLabel}\n\nThis will ${mode === 'replace' ? 'DELETE all existing data and replace it with the backup' : 'add missing rows from the backup'}.\n\nAre you sure?`)) {
        event.target.value = '';
        return;
    }

    statusEl.innerHTML = '<span class="data-mgmt-progress">⏳ Reading file…</span>';

    try {
        const text = await file.text();
        const backup = JSON.parse(text);

        if (!backup.tables) {
            statusEl.innerHTML = '<div class="data-mgmt-error">❌ Invalid backup file: missing "tables" key.</div>';
            event.target.value = '';
            return;
        }

        const meta = backup.metadata || {};
        statusEl.innerHTML = `<span class="data-mgmt-progress">⏳ Restoring ${meta.total_rows || '?'} rows across ${meta.tables_backed_up || '?'} tables…</span>`;

        const res = await fetch(`/api/admin/restore?mode=${mode}&skip_sessions=true`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: text,
        });

        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || `HTTP ${res.status}`);
        }

        const data = await res.json();
        const summary = data.summary || {};
        const restored = summary.tables_restored || [];
        const errors = summary.errors || [];

        let html = `<div class="data-mgmt-success">
            ✅ Restore complete: <strong>${summary.total_rows_restored || 0}</strong> rows across <strong>${restored.length}</strong> tables (mode: ${summary.mode})
        </div>`;

        if (errors.length) {
            html += `<div class="data-mgmt-error" style="margin-top:8px">
                ⚠️ ${errors.length} error(s):
                <ul>${errors.map(e => `<li><code>${escapeHtml(e.table)}</code> (${escapeHtml(e.phase)}): ${escapeHtml(e.error)}</li>`).join('')}</ul>
            </div>`;
        }

        statusEl.innerHTML = html;
        showToast(`✅ Restored ${summary.total_rows_restored || 0} rows`, 'success');

    } catch (err) {
        statusEl.innerHTML = `<div class="data-mgmt-error">❌ Restore failed: ${escapeHtml(err.message)}</div>`;
    }

    event.target.value = '';
}

async function loadBackupsList() {
    const listEl = document.getElementById('backups-list');
    if (!listEl) return;

    try {
        const res = await fetch('/api/admin/backups');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        const backups = data.backups || [];

        if (!backups.length) {
            listEl.innerHTML = '<div class="data-mgmt-empty">No backup files found. Create one above.</div>';
            return;
        }

        let html = '<div class="data-mgmt-backup-list">';
        for (const b of backups) {
            const meta = b.metadata || {};
            html += `<div class="data-mgmt-backup-item">
                <div class="data-mgmt-backup-name">📄 ${escapeHtml(b.filename)}</div>
                <div class="data-mgmt-backup-meta">
                    ${b.size_mb ? `<span>${b.size_mb} MB</span>` : ''}
                    ${meta.total_rows ? `<span>${meta.total_rows} rows</span>` : ''}
                    ${meta.tables_backed_up ? `<span>${meta.tables_backed_up} tables</span>` : ''}
                    ${b.modified_at ? `<span>${new Date(b.modified_at).toLocaleString()}</span>` : ''}
                </div>
                <div class="data-mgmt-backup-actions">
                    <button class="btn btn-sm btn-warning" onclick="restoreFromServerFile('${escapeHtml(b.path.replace(/\\/g, '\\\\'))}')">🔄 Restore</button>
                </div>
            </div>`;
        }
        html += '</div>';
        listEl.innerHTML = html;
    } catch (err) {
        listEl.innerHTML = `<div class="data-mgmt-error">Failed to load backups: ${escapeHtml(err.message)}</div>`;
    }
}

async function restoreFromServerFile(filepath) {
    const mode = document.getElementById('restore-mode')?.value || 'replace';
    const modeLabel = mode === 'replace' ? 'REPLACE all data' : 'MERGE (skip conflicts)';

    if (!confirm(`⚠️ Restore from server backup?\n\nFile: ${filepath}\nMode: ${modeLabel}\n\nThis will ${mode === 'replace' ? 'DELETE all existing data first' : 'add missing rows'}.\n\nAre you sure?`)) return;

    const statusEl = document.getElementById('restore-status');
    statusEl.innerHTML = '<span class="data-mgmt-progress">⏳ Restoring from file…</span>';

    try {
        const res = await fetch('/api/admin/restore/file', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filepath, mode }),
        });

        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || `HTTP ${res.status}`);
        }

        const data = await res.json();
        const summary = data.summary || {};
        statusEl.innerHTML = `<div class="data-mgmt-success">✅ Restored ${summary.total_rows_restored || 0} rows across ${(summary.tables_restored || []).length} tables</div>`;
        showToast(`✅ Restored ${summary.total_rows_restored || 0} rows`, 'success');
    } catch (err) {
        statusEl.innerHTML = `<div class="data-mgmt-error">❌ Restore failed: ${escapeHtml(err.message)}</div>`;
    }
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
        { key: 'policy_deploy', label: 'Enforce', icon: '📜' },
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
        policy_deploy: 'policy_deploy', policy_deploy_complete: 'policy_deploy',
        cleanup: 'cleanup', cleanup_complete: 'cleanup',
        promoting: 'promoting',
        fixing_template: currentPhase, template_fixed: currentPhase,
        infra_retry: currentPhase,
    };
    const activeStep = phaseToStep[currentPhase] || currentPhase;

    let pipelineHtml = '';
    if (isRunning || status === 'approved' || status === 'validation_failed') {
        pipelineHtml = _wfPipeline(pipelineSteps, {
            activeKey: isRunning ? activeStep : undefined,
            completedKeys: completedSteps,
            failedKey: status === 'validation_failed' ? activeStep : undefined,
            allDone: status === 'approved',
        });
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
            policy_deploy: '📜 Deploying Azure Policy to enforce governance…',
            policy_deploy_complete: '✓ Azure Policy deployed + assigned',
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
            else if (e.phase === 'policy_deploy') icon = '📜';
            else if (e.phase === 'policy_deploy_complete') icon = '✓';
            else if (e.phase === 'cleanup') icon = '🧹';
            else if (e.phase === 'cleanup_complete') icon = '✓';
            else if (e.phase === 'promoting') icon = '🏆';
            else if (e.phase === 'infra_retry') icon = '⏳';
            else if (e.type === 'regen_start') icon = '🔄';
            else if (e.type === 'regen_planned') icon = '🧠';
            else if (e.type === 'regen_generating') icon = '⚙️';
            else if (e.type === 'regen_complete') icon = '✅';
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

// ═══════════════════════════════════════════════════════════════
// ██  FABRIC ANALYTICS DASHBOARD
// ═══════════════════════════════════════════════════════════════

let _analyticsData = null;

async function loadAnalyticsDashboard() {
    // Load Fabric status + dashboard data in parallel
    const [statusRes, dashRes] = await Promise.allSettled([
        fetch('/api/fabric/status'),
        fetch('/api/analytics/dashboard'),
    ]);

    // Update Fabric banner
    if (statusRes.status === 'fulfilled' && statusRes.value.ok) {
        const status = await statusRes.value.json();
        _updateFabricBanner(status);
    } else {
        _updateFabricBanner(null);
    }

    // Update dashboard charts
    if (dashRes.status === 'fulfilled' && dashRes.value.ok) {
        _analyticsData = await dashRes.value.json();
        _renderAnalyticsCharts(_analyticsData);
    } else {
        console.error('Failed to load analytics dashboard');
    }
}

function _updateFabricBanner(status) {
    const icon = document.getElementById('analytics-fabric-icon');
    const text = document.getElementById('analytics-fabric-status');
    const syncBtn = document.getElementById('analytics-sync-btn');
    const banner = document.getElementById('analytics-fabric-banner');

    if (!status || !status.configured) {
        icon.textContent = '⚠️';
        text.textContent = 'Not configured — set FABRIC_WORKSPACE_ID in .env';
        banner.className = 'analytics-status-banner analytics-status-warn';
        syncBtn.disabled = true;
        return;
    }

    const ws = status.health?.workspace;
    const ol = status.health?.onelake;

    if (ws?.status === 'connected' && ol?.status === 'connected') {
        icon.textContent = '🟢';
        text.innerHTML = `Connected to <strong>${ws.name || 'workspace'}</strong> · OneLake DFS active · Region: ${ws.region || 'unknown'}`;
        banner.className = 'analytics-status-banner analytics-status-ok';
        syncBtn.disabled = false;
    } else if (ws?.status === 'connected') {
        icon.textContent = '🟡';
        text.textContent = `Workspace connected · OneLake: ${ol?.status || 'unknown'}`;
        banner.className = 'analytics-status-banner analytics-status-warn';
        syncBtn.disabled = false;
    } else {
        icon.textContent = '🔴';
        text.textContent = `Connection error: ${ws?.error || ol?.error || 'unknown'}`;
        banner.className = 'analytics-status-banner analytics-status-error';
        syncBtn.disabled = true;
    }

    // Update sync history
    if (status.sync?.history?.length) {
        _renderSyncHistory(status.sync.history);
    }
}

async function triggerFabricSync() {
    const btn = document.getElementById('analytics-sync-btn');
    btn.disabled = true;
    btn.textContent = '⏳ Syncing…';

    try {
        const r = await fetch('/api/fabric/sync', { method: 'POST' });
        const result = await r.json();
        if (r.ok) {
            btn.textContent = `✅ Synced ${result.total_rows || 0} rows`;
            setTimeout(() => { btn.textContent = '🔄 Sync to OneLake'; btn.disabled = false; }, 3000);
            // Refresh status to update sync history
            const statusRes = await fetch('/api/fabric/status');
            if (statusRes.ok) _updateFabricBanner(await statusRes.json());
        } else {
            btn.textContent = '❌ Sync failed';
            setTimeout(() => { btn.textContent = '🔄 Sync to OneLake'; btn.disabled = false; }, 3000);
        }
    } catch (e) {
        btn.textContent = '❌ Error';
        setTimeout(() => { btn.textContent = '🔄 Sync to OneLake'; btn.disabled = false; }, 3000);
    }
}

// ── Chart Rendering (pure CSS/HTML — no external chart libs) ──

const CHART_COLORS = [
    '#4f8cff', '#34d399', '#f59e42', '#ef4444', '#a78bfa',
    '#f472b6', '#38bdf8', '#facc15', '#6ee7b7', '#fb923c',
];

function _renderAnalyticsCharts(data) {
    // KPIs
    const p = data.pipeline || {};
    const d = data.deployments?.totals || {};
    const s = data.services?.totals || {};
    const c = data.compliance?.totals || {};
    const g = data.governance || {};

    _setText('kpi-total-pipelines', _fmtNum(p.total_runs));
    _setText('kpi-pipeline-rate', p.success_rate != null ? `${p.success_rate}% success` : '—');
    _setText('kpi-total-deployments', _fmtNum(d.total_deployments));
    _setText('kpi-deployment-rate', d.succeeded != null ? `${_fmtNum(d.succeeded)} succeeded` : '—');
    _setText('kpi-total-services', _fmtNum(s.total_services));
    _setText('kpi-approved-services', s.approved_services != null ? `${_fmtNum(s.approved_services)} approved` : '—');
    _setText('kpi-compliance-score', c.avg_score != null ? Math.round(c.avg_score) : '—');
    _setText('kpi-compliance-pass', c.passed != null ? `${_fmtNum(c.passed)} passed` : '—');

    // Gov KPI
    const cisoTotal = (g.ciso_verdicts || []).reduce((a, v) => a + (v.count || 0), 0);
    const cisoApproved = (g.ciso_verdicts || []).find(v => v.verdict === 'approved')?.count || 0;
    _setText('kpi-gov-reviews', _fmtNum(cisoTotal));
    _setText('kpi-gov-approved', cisoTotal ? `${Math.round(cisoApproved / cisoTotal * 100)}% approved` : '—');

    // Pipeline trend (bar chart)
    _renderBarChart('chart-pipeline-trend', (p.trend || []).map(t => ({
        label: _shortDate(t.date),
        values: [{ value: t.succeeded || 0, color: '#34d399', label: 'Succeeded' },
                 { value: t.failed || 0, color: '#ef4444', label: 'Failed' }],
    })));

    // Pipeline by type (horizontal bars)
    _renderHorizontalBars('chart-pipeline-type', (p.by_type || []).map((t, i) => ({
        label: t.pipeline_type || 'unknown',
        value: t.runs || 0,
        color: CHART_COLORS[i % CHART_COLORS.length],
    })));

    // Governance verdicts (donut)
    _renderDonutChart('chart-gov-verdicts', [
        ...(g.ciso_verdicts || []).map(v => ({
            label: `CISO: ${v.verdict}`,
            value: v.count || 0,
            color: v.verdict === 'approved' ? '#34d399' : v.verdict === 'blocked' ? '#ef4444' : '#f59e42',
        })),
        ...(g.cto_verdicts || []).map(v => ({
            label: `CTO: ${v.verdict}`,
            value: v.count || 0,
            color: v.verdict === 'approved' ? '#6ee7b7' : v.verdict === 'blocked' ? '#fb923c' : '#facc15',
        })),
    ]);

    // Deploy by region
    _renderHorizontalBars('chart-deploy-region', (data.deployments?.by_region || []).map((r, i) => ({
        label: r.region || 'unknown',
        value: r.count || 0,
        color: CHART_COLORS[i % CHART_COLORS.length],
    })));

    // Service by category
    _renderHorizontalBars('chart-service-category', (data.services?.by_category || []).map((c, i) => ({
        label: c.category || 'unknown',
        value: c.count || 0,
        color: CHART_COLORS[i % CHART_COLORS.length],
    })));

    // Security posture
    const postureColors = { strong: '#34d399', moderate: '#f59e42', weak: '#ef4444', 'not assessed': '#94a3b8' };
    _renderDonutChart('chart-security-posture', (g.security_postures || []).map(p => ({
        label: p.security_posture || 'unknown',
        value: p.count || 0,
        color: postureColors[p.security_posture?.toLowerCase()] || '#94a3b8',
    })));

    // Compliance score distribution
    const gradeColors = { 'A (90-100)': '#34d399', 'B (80-89)': '#6ee7b7', 'C (70-79)': '#f59e42', 'D (60-69)': '#fb923c', 'F (<60)': '#ef4444' };
    _renderHorizontalBars('chart-compliance-dist', (data.compliance?.score_distribution || []).map(d => ({
        label: d.grade,
        value: d.count || 0,
        color: gradeColors[d.grade] || '#94a3b8',
    })));

    // Gov trends
    _renderBarChart('chart-gov-trend', _groupGovTrend(g.trend || []));

    // Top templates
    _renderHorizontalBars('chart-top-templates', (data.deployments?.by_template || []).map((t, i) => ({
        label: _truncate(t.template_name || 'unknown', 25),
        value: t.deployment_count || 0,
        color: CHART_COLORS[i % CHART_COLORS.length],
    })));
}

function _groupGovTrend(trend) {
    // Group by date, stacking CISO approved/blocked and CTO approved/blocked
    const byDate = {};
    for (const t of trend) {
        if (!byDate[t.date]) byDate[t.date] = { approved: 0, blocked: 0, other: 0 };
        byDate[t.date].approved += t.approved || 0;
        byDate[t.date].blocked += t.blocked || 0;
        byDate[t.date].other += Math.max(0, (t.reviews || 0) - (t.approved || 0) - (t.blocked || 0));
    }
    return Object.entries(byDate).map(([date, v]) => ({
        label: _shortDate(date),
        values: [
            { value: v.approved, color: '#34d399', label: 'Approved' },
            { value: v.blocked, color: '#ef4444', label: 'Blocked' },
            { value: v.other, color: '#f59e42', label: 'Other' },
        ],
    }));
}

// ── Pure CSS Chart Primitives ───────────────────────────────

function _renderBarChart(containerId, bars) {
    const el = document.getElementById(containerId);
    if (!el) return;

    if (!bars.length) {
        el.innerHTML = '<div class="analytics-chart-empty">No data available</div>';
        return;
    }

    const maxVal = Math.max(1, ...bars.map(b => b.values.reduce((a, v) => a + v.value, 0)));
    const barWidth = Math.max(12, Math.min(40, Math.floor(600 / bars.length)));

    let legendItems = {};
    bars.forEach(b => b.values.forEach(v => { legendItems[v.label] = v.color; }));

    const legendHtml = Object.entries(legendItems).map(([label, color]) =>
        `<span class="analytics-legend-item"><span class="analytics-legend-dot" style="background:${color}"></span>${label}</span>`
    ).join('');

    const barsHtml = bars.map(b => {
        const total = b.values.reduce((a, v) => a + v.value, 0);
        const pct = (total / maxVal) * 100;
        const segs = b.values.map(v => {
            const segPct = total > 0 ? (v.value / total) * pct : 0;
            return segPct > 0 ? `<div class="analytics-bar-seg" style="height:${segPct}%;background:${v.color}" title="${v.label}: ${v.value}"></div>` : '';
        }).join('');

        return `<div class="analytics-bar-col" style="width:${barWidth}px">
            <div class="analytics-bar-stack" style="height:100%">${segs}</div>
            <div class="analytics-bar-label">${b.label}</div>
        </div>`;
    }).join('');

    el.innerHTML = `<div class="analytics-legend">${legendHtml}</div>
        <div class="analytics-bar-chart" style="height:180px">${barsHtml}</div>`;
}

function _renderHorizontalBars(containerId, items) {
    const el = document.getElementById(containerId);
    if (!el) return;

    if (!items.length) {
        el.innerHTML = '<div class="analytics-chart-empty">No data available</div>';
        return;
    }

    const maxVal = Math.max(1, ...items.map(i => i.value));
    el.innerHTML = items.map(item => {
        const pct = (item.value / maxVal) * 100;
        return `<div class="analytics-hbar-row">
            <div class="analytics-hbar-label" title="${item.label}">${_truncate(item.label, 20)}</div>
            <div class="analytics-hbar-track">
                <div class="analytics-hbar-fill" style="width:${pct}%;background:${item.color}"></div>
            </div>
            <div class="analytics-hbar-value">${_fmtNum(item.value)}</div>
        </div>`;
    }).join('');
}

function _renderDonutChart(containerId, segments) {
    const el = document.getElementById(containerId);
    if (!el) return;

    const total = segments.reduce((a, s) => a + s.value, 0);
    if (!total) {
        el.innerHTML = '<div class="analytics-chart-empty">No data available</div>';
        return;
    }

    // Generate SVG donut
    const size = 140, cx = size / 2, cy = size / 2, r = 50, stroke = 20;
    const circ = 2 * Math.PI * r;
    let offset = 0;

    const arcs = segments.filter(s => s.value > 0).map(s => {
        const pct = s.value / total;
        const dash = circ * pct;
        const gap = circ - dash;
        const svg = `<circle cx="${cx}" cy="${cy}" r="${r}" fill="none"
            stroke="${s.color}" stroke-width="${stroke}"
            stroke-dasharray="${dash} ${gap}"
            stroke-dashoffset="${-offset}"
            class="analytics-donut-arc" />`;
        offset += dash;
        return svg;
    }).join('');

    const legendHtml = segments.filter(s => s.value > 0).map(s =>
        `<div class="analytics-donut-legend-item">
            <span class="analytics-legend-dot" style="background:${s.color}"></span>
            <span>${s.label}</span>
            <span class="analytics-donut-legend-val">${s.value} (${Math.round(s.value / total * 100)}%)</span>
        </div>`
    ).join('');

    el.innerHTML = `<div class="analytics-donut-wrap">
        <svg viewBox="0 0 ${size} ${size}" class="analytics-donut-svg">
            ${arcs}
            <text x="${cx}" y="${cy}" text-anchor="middle" dominant-baseline="middle"
                  class="analytics-donut-total">${_fmtNum(total)}</text>
        </svg>
        <div class="analytics-donut-legend">${legendHtml}</div>
    </div>`;
}

function _renderSyncHistory(history) {
    const el = document.getElementById('analytics-sync-history');
    if (!el) return;

    if (!history.length) {
        el.innerHTML = '<div class="analytics-chart-placeholder">No syncs yet</div>';
        return;
    }

    el.innerHTML = history.slice(-10).reverse().map(h =>
        `<div class="analytics-sync-row">
            <span class="analytics-sync-status ${h.status === 'completed' ? 'sync-ok' : 'sync-partial'}">${h.status === 'completed' ? '✅' : '⚠️'}</span>
            <span class="analytics-sync-time">${_timeAgo(h.timestamp)}</span>
            <span class="analytics-sync-detail">${_fmtNum(h.rows)} rows · ${h.duration}s</span>
        </div>`
    ).join('');
}

// ── Helpers ─────────────────────────────────────

function _setText(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text ?? '—';
}

function _fmtNum(n) {
    if (n == null || isNaN(n)) return '0';
    return Number(n).toLocaleString();
}

function _shortDate(dateStr) {
    if (!dateStr) return '';
    const d = new Date(dateStr);
    return `${d.getMonth() + 1}/${d.getDate()}`;
}

function _truncate(str, len) {
    if (!str) return '';
    return str.length > len ? str.substring(0, len - 1) + '…' : str;
}

function _timeShort(isoStr) {
    if (!isoStr) return '';
    try {
        const d = new Date(isoStr);
        return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    } catch { return ''; }
}


// ══════════════════════════════════════════════════════════════
// AGENT ACTIVITY — Org-chart style agent dashboard
// ══════════════════════════════════════════════════════════════

const AO_CAT_META = {
    'Interactive':             { icon: '💬', color: '#3b82f6', role: 'User-Facing' },
    'Orchestrator':            { icon: '🎯', color: '#8b5cf6', role: 'Routing & Planning' },
    'Standards':               { icon: '📋', color: '#06b6d4', role: 'Policy Extraction' },
    'ARM Generation':          { icon: '🏗️', color: '#f59e0b', role: 'Template Authoring' },
    'Deployment Pipeline':     { icon: '🚀', color: '#10b981', role: 'Deploy & Heal' },
    'Compliance':              { icon: '🛡️', color: '#ef4444', role: 'Policy Enforcement' },
    'Artifact & Healing':      { icon: '🔧', color: '#ec4899', role: 'Fix & Generate' },
    'Infrastructure Testing':  { icon: '🧪', color: '#14b8a6', role: 'Verify & Test' },
    'Governance Review':       { icon: '⚖️', color: '#6366f1', role: 'Review Gates' },
};

// Individual agent icons based on their key — gives each card a unique "avatar"
const AO_AGENT_ICONS = {
    web_chat: '💬', ciso_advisor: '🔐', concierge: '🛎️',
    gap_analyst: '🔍', arm_template_editor: '✏️', policy_checker: '📋', request_parser: '🧩',
    standards_extractor: '📄',
    arm_modifier: '🛠️', arm_generator: '🏗️',
    template_healer: '💊', error_culprit_detector: '🎯', deploy_failure_analyst: '📊',
    remediation_planner: '📝', remediation_executor: '⚡',
    artifact_generator: '✨', policy_fixer: '🩹', deep_template_healer: '🔬', llm_reasoner: '🧠',
    infra_tester: '🧪', infra_test_analyzer: '🔎',
    ciso_reviewer: '🛡️', cto_reviewer: '🏛️',
};

// Cached API data for detail panel
let _aoData = null;

async function loadAgentActivity() {
    const org = document.getElementById('ao-org');
    if (!org) return;

    try {
        const res = await fetch('/api/agents/activity');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        _aoData = data;

        const agents = data.agents || [];
        const counters = data.counters || {};
        const routing = data.routing_table || [];

        // Build model lookup from routing table
        const taskModelMap = {};
        routing.forEach(r => { taskModelMap[r.task] = r.model_name || r.model_id || r.task; });

        // Compute summary stats
        let totalCalls = 0, totalErrors = 0, totalMs = 0;
        Object.values(counters).forEach(c => {
            totalCalls += c.calls || 0;
            totalErrors += c.errors || 0;
            totalMs += c.total_ms || 0;
        });
        const avgLatency = totalCalls > 0 ? Math.round(totalMs / totalCalls) : 0;

        const _s = (id, val) => { const e = document.getElementById(id); if (e) e.textContent = val; };
        _s('ao-agent-ct', agents.length);
        _s('ao-call-ct', totalCalls.toLocaleString());
        _s('ao-err-ct', totalErrors);
        _s('ao-lat-avg', totalCalls > 0 ? `${avgLatency}ms` : '—');

        // Group agents by category
        const categories = {};
        agents.forEach(a => {
            if (!categories[a.category]) categories[a.category] = [];
            categories[a.category].push(a);
        });

        // ── Render org chart ──
        let html = '';

        // Hub node
        html += `<div class="ao-hub">
            <div class="ao-hub-icon">⚙️</div>
            <div class="ao-hub-label">InfraForge Agent Network</div>
            <div class="ao-hub-sub">${agents.length} agents · ${Object.keys(categories).length} teams · Copilot SDK</div>
        </div>`;
        html += `<div class="ao-trunk"></div>`;

        // Category groups
        const catEntries = Object.entries(categories);
        html += `<div class="ao-branches">`;

        catEntries.forEach(([cat, catAgents]) => {
            const meta = AO_CAT_META[cat] || { icon: '🤖', color: '#6b7280', role: cat };

            html += `<div class="ao-branch">`;
            html += `<div class="ao-branch-head" style="--cat-color: ${meta.color}">
                <span class="ao-branch-icon">${meta.icon}</span>
                <span class="ao-branch-name">${cat}</span>
                <span class="ao-branch-role">${meta.role}</span>
                <span class="ao-branch-count">${catAgents.length}</span>
            </div>`;
            html += `<div class="ao-branch-rail" style="--cat-color: ${meta.color}"></div>`;
            html += `<div class="ao-cards">`;

            catAgents.forEach(a => {
                const agentKey = a.name.toUpperCase().replace(/\s+/g, '_');
                const c = counters[agentKey] || counters[a.key] || {};
                const calls = c.calls || 0;
                const errors = c.errors || 0;
                const model = taskModelMap[a.task] || a.task;
                const agentIcon = AO_AGENT_ICONS[a.key] || meta.icon;
                const isActive = calls > 0;

                html += `<div class="ao-card ${isActive ? 'ao-card-hot' : ''}" data-agent-key="${a.key}" onclick="showAgentDetail('${a.key}')" style="--cat-color: ${meta.color}">
                    <div class="ao-card-avatar" style="background: ${meta.color}22; border-color: ${meta.color}66">
                        <span class="ao-card-avatar-icon">${agentIcon}</span>
                    </div>
                    <div class="ao-card-name">${a.name}</div>
                    <div class="ao-card-role">${_truncateModel(model)}</div>
                    <div class="ao-card-desc">${a.description.length > 90 ? a.description.substring(0, 87) + '…' : a.description}</div>
                    <div class="ao-card-footer">
                        ${isActive
                            ? `<span class="ao-card-calls">${calls}</span><span class="ao-card-calls-lbl">calls</span>${errors > 0 ? `<span class="ao-card-errs">${errors} err</span>` : ''}`
                            : `<span class="ao-card-idle">Idle</span>`}
                        <span class="ao-card-sdk">SDK</span>
                    </div>
                </div>`;
            });

            html += `</div></div>`;
        });

        html += `</div>`;
        org.innerHTML = html;

    } catch (err) {
        console.warn('Agent activity load failed:', err);
        org.innerHTML = `
            <div class="activity-empty">
                <span class="activity-empty-icon">⚠️</span>
                <p>Failed to load agent activity data.</p>
                <p style="font-size:0.7rem;color:var(--text-muted)">${escapeHtml(String(err))}</p>
            </div>`;
    }
}

/** Show the detail panel for a clicked agent card */
function showAgentDetail(agentKey) {
    if (!_aoData) return;
    const overlay = document.getElementById('ao-detail-overlay');
    const panel = document.getElementById('ao-detail-panel');
    if (!overlay || !panel) return;

    const agent = (_aoData.agents || []).find(a => a.key === agentKey);
    if (!agent) return;

    const counters = _aoData.counters || {};
    const activity = _aoData.activity || [];
    const routing = _aoData.routing_table || [];
    const taskModelMap = {};
    routing.forEach(r => { taskModelMap[r.task] = r.model_name || r.model_id || r.task; });

    const agentNameKey = agent.name.toUpperCase().replace(/\s+/g, '_');
    const c = counters[agentNameKey] || counters[agentKey] || {};
    const calls = c.calls || 0;
    const errors = c.errors || 0;
    const avgMs = calls > 0 ? Math.round((c.total_ms || 0) / calls) : 0;
    const lastCalled = c.last_called ? _timeAgo(c.last_called) : 'Never';

    const model = taskModelMap[agent.task] || agent.task;
    const meta = AO_CAT_META[agent.category] || { icon: '🤖', color: '#6b7280', role: '' };
    const agentIcon = AO_AGENT_ICONS[agentKey] || meta.icon;

    // Filter activity for this agent
    const agentActivity = activity.filter(e =>
        (e.agent || '').toUpperCase().replace(/\s+/g, '_') === agentNameKey ||
        (e.agent_key || '') === agentKey
    ).slice(0, 20);

    panel.innerHTML = `
        <button class="ao-detail-close" onclick="hideAgentDetail()" title="Close">✕</button>
        <div class="ao-detail-header" style="--cat-color: ${meta.color}">
            <div class="ao-detail-avatar" style="background: ${meta.color}22; border-color: ${meta.color}">
                ${agentIcon}
            </div>
            <div class="ao-detail-info">
                <div class="ao-detail-name">${agent.name}</div>
                <div class="ao-detail-cat">
                    <span style="color:${meta.color}">${meta.icon} ${agent.category}</span>
                    <span class="ao-detail-badge">SDK</span>
                    <span class="ao-detail-badge ao-detail-badge-key">${agentKey}</span>
                </div>
            </div>
        </div>

        <div class="ao-detail-desc">${agent.description}</div>

        <div class="ao-detail-stats">
            <div class="ao-dstat">
                <div class="ao-dstat-val">${_truncateModel(model)}</div>
                <div class="ao-dstat-lbl">Model</div>
            </div>
            <div class="ao-dstat">
                <div class="ao-dstat-val">${agent.task}</div>
                <div class="ao-dstat-lbl">Pipeline Task</div>
            </div>
            <div class="ao-dstat">
                <div class="ao-dstat-val">${agent.timeout}s</div>
                <div class="ao-dstat-lbl">Timeout</div>
            </div>
            <div class="ao-dstat">
                <div class="ao-dstat-val">~${(agent.prompt_tokens_est || 0).toLocaleString()}</div>
                <div class="ao-dstat-lbl">Prompt Tokens</div>
            </div>
        </div>

        ${agent.model_reason ? `
        <div class="ao-detail-model-reason">
            <span class="ao-detail-model-reason-icon">🧠</span>
            <span class="ao-detail-model-reason-text"><strong>Model routing:</strong> ${escapeHtml(agent.model_reason)} <span class="mr-sdk-tag">COPILOT SDK</span></span>
        </div>` : ''}

        <div class="ao-detail-prompt-section">
            <div class="ao-detail-prompt-header">
                <span class="ao-detail-prompt-label">📋 System Prompt</span>
                <span class="ao-detail-prompt-size">${((agent.prompt_length || 0) / 1024).toFixed(1)} KB</span>
            </div>
            <div class="ao-detail-prompt-preview">${escapeHtml(agent.prompt_preview || '')}</div>
            <button class="ao-detail-prompt-btn" onclick="viewAgentPrompt('${agentKey}')">
                View Full System Prompt
            </button>
        </div>

        <div class="ao-detail-metrics">
            <div class="ao-dmetric"><span class="ao-dmetric-num">${calls}</span><span class="ao-dmetric-lbl">Calls</span></div>
            <div class="ao-dmetric"><span class="ao-dmetric-num ao-dmetric-err">${errors}</span><span class="ao-dmetric-lbl">Errors</span></div>
            <div class="ao-dmetric"><span class="ao-dmetric-num">${avgMs ? avgMs + 'ms' : '—'}</span><span class="ao-dmetric-lbl">Avg Latency</span></div>
            <div class="ao-dmetric"><span class="ao-dmetric-num">${lastCalled}</span><span class="ao-dmetric-lbl">Last Called</span></div>
        </div>

        ${agentActivity.length > 0 ? `
        <div class="ao-detail-feed-title">Recent Invocations</div>
        <div class="ao-detail-feed">
            ${agentActivity.map(e => {
                const icon = e.status === 'ok' ? '✅' : '❌';
                const dur = e.duration_ms ? `${Math.round(e.duration_ms)}ms` : '';
                const ts = _timeShort(e.timestamp);
                return `<div class="ao-dfeed-row ${e.status === 'error' ? 'ao-dfeed-err' : ''}">
                    <span>${icon}</span>
                    <span class="ao-dfeed-dur">${dur}</span>
                    <span class="ao-dfeed-size">${_formatBytes(e.prompt_len)}→${_formatBytes(e.response_len)}</span>
                    ${e.error ? `<span class="ao-dfeed-error" title="${escapeHtml(e.error)}">⚠ ${e.error.substring(0, 60)}</span>` : ''}
                    <span class="ao-dfeed-time">${ts}</span>
                </div>`;
            }).join('')}
        </div>` : `<div class="ao-detail-no-activity">No invocations recorded yet.</div>`}
    `;

    overlay.classList.remove('hidden');
}

/** Fetch and display the full system prompt for an agent */
async function viewAgentPrompt(agentKey) {
    // Show the prompt overlay
    let promptOverlay = document.getElementById('ao-prompt-overlay');
    if (!promptOverlay) {
        promptOverlay = document.createElement('div');
        promptOverlay.id = 'ao-prompt-overlay';
        promptOverlay.className = 'ao-prompt-overlay';
        promptOverlay.innerHTML = `
            <div class="ao-prompt-panel" id="ao-prompt-panel">
                <div class="ao-prompt-top">
                    <span class="ao-prompt-title" id="ao-prompt-title">System Prompt</span>
                    <div class="ao-prompt-actions">
                        <button class="ao-prompt-copy-btn" id="ao-prompt-copy" title="Copy to clipboard">📋 Copy</button>
                        <button class="ao-prompt-close-btn" onclick="closeAgentPrompt()">✕</button>
                    </div>
                </div>
                <div class="ao-prompt-body" id="ao-prompt-body">Loading…</div>
            </div>`;
        document.body.appendChild(promptOverlay);

        // Close on backdrop click
        promptOverlay.addEventListener('click', e => {
            if (e.target === promptOverlay) closeAgentPrompt();
        });
    }

    promptOverlay.classList.remove('hidden');
    promptOverlay.style.display = '';
    const body = document.getElementById('ao-prompt-body');
    const title = document.getElementById('ao-prompt-title');
    body.textContent = 'Loading…';

    try {
        const res = await fetch(`/api/agents/${agentKey}/prompt`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();

        title.textContent = `${data.name} — System Prompt`;

        // Render prompt as formatted text with line breaks
        body.innerHTML = '';
        const pre = document.createElement('pre');
        pre.className = 'ao-prompt-content';
        pre.textContent = data.prompt;
        body.appendChild(pre);

        // Token/size info
        const info = document.createElement('div');
        info.className = 'ao-prompt-info';
        info.textContent = `${data.prompt_length.toLocaleString()} chars · ~${data.prompt_tokens_est.toLocaleString()} tokens`;
        body.appendChild(info);

        // Wire up copy button
        const copyBtn = document.getElementById('ao-prompt-copy');
        copyBtn.onclick = () => {
            navigator.clipboard.writeText(data.prompt).then(() => {
                copyBtn.textContent = '✓ Copied!';
                setTimeout(() => { copyBtn.textContent = '📋 Copy'; }, 2000);
            });
        };
    } catch (err) {
        body.textContent = `Failed to load prompt: ${err.message}`;
    }
}

function closeAgentPrompt() {
    const overlay = document.getElementById('ao-prompt-overlay');
    if (overlay) {
        overlay.classList.add('hidden');
        overlay.style.display = 'none';
    }
}

function hideAgentDetail() {
    const overlay = document.getElementById('ao-detail-overlay');
    if (overlay) overlay.classList.add('hidden');
}

function _truncateModel(model) {
    if (!model) return '';
    return model
        .replace('claude-sonnet-4-20250514', 'Claude Sonnet 4')
        .replace('Claude Sonnet 4', 'Claude Sonnet 4')
        .replace('gpt-4.1-nano-2025-04-14', 'GPT-4.1 Nano')
        .replace('GPT-4.1 Nano', 'GPT-4.1 Nano')
        .replace('gpt-4.1-2025-04-14', 'GPT-4.1')
        .replace('GPT-4.1', 'GPT-4.1');
}

function _timeAgo(isoStr) {
    if (!isoStr) return 'never';
    try {
        const now = Date.now();
        const then = new Date(isoStr).getTime();
        const diffMs = now - then;
        if (diffMs < 60000) return 'just now';
        if (diffMs < 3600000) return `${Math.floor(diffMs / 60000)}m ago`;
        if (diffMs < 86400000) return `${Math.floor(diffMs / 3600000)}h ago`;
        return `${Math.floor(diffMs / 86400000)}d ago`;
    } catch { return ''; }
}

function _escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}
