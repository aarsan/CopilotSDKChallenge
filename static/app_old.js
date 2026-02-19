/**
 * InfraForge — Web UI Client
 *
 * Handles:
 * - Authentication (Entra ID via redirect, or demo mode)
 * - WebSocket connection for streaming chat
 * - Markdown rendering with Mermaid diagram support
 * - Code block syntax highlighting and copy buttons
 * - Tool activity indicators
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

    // Check for existing session in URL or localStorage
    const urlParams = new URLSearchParams(window.location.search);
    const sessionFromUrl = urlParams.get('session');

    if (sessionFromUrl) {
        sessionToken = sessionFromUrl;
        // Clean URL
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
            // Demo mode — session created server-side
            sessionToken = data.sessionToken;
            currentUser = data.user;
            localStorage.setItem('infraforge_session', sessionToken);
            showApp();
            connectWebSocket();
        } else if (data.mode === 'entra') {
            // Real Entra ID — redirect to Microsoft login
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
        // Set user info in sidebar
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

    document.getElementById('user-input').focus();

    // Load dashboard data
    loadDashboardData();
}

// ── WebSocket Connection ────────────────────────────────────

function connectWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws/chat`;

    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
        // Authenticate the WebSocket connection
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
        // Auto-reconnect after 3 seconds
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
    // Prefix with design mode context if in ideal mode
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

    // Render markdown incrementally
    currentStreamDiv.innerHTML = renderMarkdown(currentStreamContent);
    currentStreamDiv.classList.add('streaming-cursor');

    scrollToBottom();
}

function handleStreamDone(fullContent) {
    if (currentStreamDiv) {
        currentStreamDiv.classList.remove('streaming-cursor');
        // Final render with complete content
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

    // Configure marked
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

    // Use custom renderer for code blocks
    const renderer = new marked.Renderer();

    renderer.code = function (codeObj) {
        const code = typeof codeObj === 'string' ? codeObj : (codeObj.text || '');
        const lang = (typeof codeObj === 'object' ? codeObj.lang : '') || '';

        // Check if it's a mermaid diagram
        if (lang === 'mermaid') {
            const id = `mermaid-${++mermaidCounter}`;
            return `<div class="mermaid-container" id="${id}">${escapeHtml(code)}</div>`;
        }

        // Regular code block with header and copy button
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
    // Render any mermaid diagrams
    const mermaidDivs = element.querySelectorAll('.mermaid-container');
    mermaidDivs.forEach(async (div) => {
        try {
            const code = div.textContent;
            const { svg } = await mermaid.render(div.id + '-svg', code);
            div.innerHTML = svg;
        } catch (err) {
            console.warn('Mermaid render failed:', err);
            // Keep the raw code visible
            div.innerHTML = `<pre><code>${escapeHtml(div.textContent)}</code></pre>`;
        }
    });
}

// ── Dashboard & Service Catalog ─────────────────────────────

let allServices = [];
let allTemplates = [];
let currentCategoryFilter = 'all';

async function loadDashboardData() {
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

        // Update stats
        const stats = svcData.stats || {};
        document.getElementById('stat-approved').textContent = stats.approved || 0;
        document.getElementById('stat-conditional').textContent = stats.conditional || 0;
        document.getElementById('stat-review').textContent = stats.under_review || 0;
        document.getElementById('stat-templates').textContent = tmplData.total || 0;

        // Build category filters
        const categories = svcData.categories || [];
        const filterContainer = document.getElementById('catalog-filters');
        filterContainer.innerHTML = `<button class="filter-pill active" onclick="filterServices('all')">All (${allServices.length})</button>`;
        categories.forEach(cat => {
            const count = allServices.filter(s => s.category === cat).length;
            filterContainer.innerHTML += `<button class="filter-pill" onclick="filterServices('${cat}')">${cat} (${count})</button>`;
        });

        // Render service table
        renderServiceTable(allServices);

        // Build template format filters
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
                // Don't duplicate if same as a format name
                if (!templateFormats.includes(cat)) {
                    const count = allTemplates.filter(t => t.category === cat).length;
                    tmplFilterContainer.innerHTML += `<button class="filter-pill" onclick="filterTemplates('${cat}')">${cat} (${count})</button>`;
                }
            });
        }

        // Render template table
        renderTemplateTable(allTemplates);

        // Render approval tracker
        renderApprovalTracker(approvalData.requests || []);
    } catch (err) {
        console.warn('Failed to load dashboard data:', err);
    }
}

function filterServices(category) {
    currentCategoryFilter = category;

    // Update active pill within catalog filters only
    const container = document.getElementById('catalog-filters');
    if (container) {
        container.querySelectorAll('.filter-pill').forEach(pill => pill.classList.remove('active'));
    }
    event.target.classList.add('active');

    const filtered = category === 'all'
        ? allServices
        : allServices.filter(s => s.category === category);

    renderServiceTable(filtered);
}

function renderServiceTable(services) {
    const tbody = document.getElementById('catalog-tbody');

    if (!services.length) {
        tbody.innerHTML = '<tr><td colspan="5" class="catalog-loading">No services found</td></tr>';
        return;
    }

    const statusLabels = {
        approved: '✅ Approved',
        conditional: '⚠️ Conditional',
        under_review: '🔄 Under Review',
        not_approved: '❌ Not Approved',
    };

    tbody.innerHTML = services.map(svc => {
        const status = svc.status || 'not_approved';
        const risk = svc.risk_tier || 'medium';
        const regions = (svc.approved_regions || []).slice(0, 3);
        const regionExtra = (svc.approved_regions || []).length > 3
            ? ` +${svc.approved_regions.length - 3}` : '';

        return `<tr onclick="askAboutService('${escapeHtml(svc.name)}')">
            <td>
                <div class="svc-name">${escapeHtml(svc.name)}</div>
                <div class="svc-id">${escapeHtml(svc.id)}</div>
            </td>
            <td><span class="category-badge">${escapeHtml(svc.category)}</span></td>
            <td><span class="status-badge ${status}">${statusLabels[status] || status}</span></td>
            <td><span class="risk-badge ${risk}">${risk}</span></td>
            <td>
                <div class="region-tags">
                    ${regions.map(r => `<span class="region-tag">${r}</span>`).join('')}
                    ${regionExtra ? `<span class="region-tag">${regionExtra}</span>` : ''}
                </div>
            </td>
        </tr>`;
    }).join('');
}

function askAboutService(serviceName) {
    sendQuickAction(`Check the approval status for ${serviceName} and tell me the policies and restrictions that apply`);
}

// ── Design Mode Toggle ──────────────────────────────────────

function setDesignMode(mode) {
    currentDesignMode = mode;

    // Update UI
    document.getElementById('mode-approved').classList.toggle('active', mode === 'approved');
    document.getElementById('mode-ideal').classList.toggle('active', mode === 'ideal');

    // Update info box
    const infoText = document.querySelector('.mode-info-text');
    if (mode === 'approved') {
        infoText.textContent = 'Approved Only mode: All generated infrastructure uses services vetted by the platform team. Ready to deploy.';
    } else {
        infoText.textContent = 'Ideal Design mode: InfraForge will generate the best-practice architecture. Non-approved services will be flagged, and I\'ll guide you through submitting approval requests to IT.';
    }

    // Update input placeholder
    const input = document.getElementById('user-input');
    if (mode === 'approved') {
        input.placeholder = 'Describe the infrastructure you need (using approved services only)...';
    } else {
        input.placeholder = 'Describe your ideal infrastructure (I\'ll handle approval requests for non-approved services)...';
    }
}


// ── Approval Request Tracker ────────────────────────────────

async function loadApprovalRequests() {
    try {
        const res = await fetch('/api/approvals');
        const data = await res.json();
        renderApprovalTracker(data.requests || []);
    } catch (err) {
        console.warn('Failed to load approval requests:', err);
    }
}

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
                    <div class="approval-item" onclick="sendQuickAction('Check the status of approval request ${reqId}')">
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

function showWelcomeDashboard() {
    // Hide chat messages, show dashboard
    const dashboard = document.getElementById('welcome-dashboard');
    const messages = document.getElementById('messages');

    if (dashboard) {
        // Clear any chat messages (keep dashboard)
        const chatMessages = messages.querySelectorAll('.message');
        chatMessages.forEach(msg => msg.remove());

        dashboard.classList.remove('hidden');
        messages.scrollTop = 0;
    }
}

// Hide dashboard when user sends a message
const originalSendMessage = sendMessage;
sendMessage = function () {
    const dashboard = document.getElementById('welcome-dashboard');
    if (dashboard) dashboard.classList.add('hidden');
    originalSendMessage();
};

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
    // Remove all chat messages, keep the dashboard
    const messages = container.querySelectorAll('.message');
    messages.forEach(msg => msg.remove());

    // Show the welcome dashboard again
    const dashboard = document.getElementById('welcome-dashboard');
    if (dashboard) {
        dashboard.classList.remove('hidden');
        container.scrollTop = 0;
    }
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


// ── Template Table ──────────────────────────────────────────

let currentTemplateFilter = 'all';

function renderTemplateTable(templates) {
    const tbody = document.getElementById('template-tbody');
    if (!tbody) return;

    if (!templates.length) {
        tbody.innerHTML = '<tr><td colspan="5" class="catalog-loading">No templates found</td></tr>';
        return;
    }

    const statusLabels = {
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

        return `<tr onclick="askAboutTemplate('${escapeHtml(tmpl.name)}')">
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
            <td><span class="status-badge ${status}">${statusLabels[status] || status}</span></td>
        </tr>`;
    }).join('');
}

function filterTemplates(filter) {
    currentTemplateFilter = filter;

    // Update active pill
    const container = document.getElementById('template-filters');
    if (container) {
        container.querySelectorAll('.filter-pill').forEach(pill => pill.classList.remove('active'));
        event.target.classList.add('active');
    }

    const filtered = filter === 'all'
        ? allTemplates
        : allTemplates.filter(t => t.format === filter || t.category === filter);

    renderTemplateTable(filtered);
}

function askAboutTemplate(templateName) {
    sendQuickAction(`Search the template catalog for "${templateName}" and show me its details`);
}


// ── Onboarding: Modals ──────────────────────────────────────

function openServiceOnboarding() {
    document.getElementById('modal-service-onboard').classList.remove('hidden');
}

function openTemplateOnboarding() {
    document.getElementById('modal-template-onboard').classList.remove('hidden');
    // Wire blueprint checkbox
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

async function submitServiceOnboarding(event) {
    event.preventDefault();
    const form = document.getElementById('form-service-onboard');
    const fd = new FormData(form);
    const btn = document.getElementById('btn-submit-service');
    btn.disabled = true;
    btn.textContent = 'Submitting...';

    const body = {
        id: fd.get('id').trim(),
        name: fd.get('name').trim(),
        category: fd.get('category'),
        status: fd.get('status'),
        risk_tier: fd.get('risk_tier'),
        contact: fd.get('contact') || '',
        review_notes: fd.get('review_notes') || '',
        documentation: fd.get('documentation') || '',
        approved_skus: (fd.get('approved_skus') || '').split(',').map(s => s.trim()).filter(Boolean),
        approved_regions: (fd.get('approved_regions') || '').split(',').map(s => s.trim()).filter(Boolean),
        policies: (fd.get('policies') || '').split('\n').map(s => s.trim()).filter(Boolean),
        conditions: (fd.get('conditions') || '').split('\n').map(s => s.trim()).filter(Boolean),
    };

    try {
        const res = await fetch('/api/catalog/services', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });

        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Failed to onboard service');
        }

        showToast(`Service "${body.name}" onboarded successfully!`);
        closeModal('modal-service-onboard');
        form.reset();
        // Refresh the service catalog
        await loadDashboardData();
    } catch (err) {
        showToast(err.message, 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = 'Onboard Service';
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
        // Refresh the template catalog
        await loadDashboardData();
    } catch (err) {
        showToast(err.message, 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = 'Onboard Template';
    }
}