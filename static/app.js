document.addEventListener('DOMContentLoaded', () => {
    // ──────────────────────────────────────────────
    // DOM Elements
    // ──────────────────────────────────────────────
    const installBtn = document.getElementById('btn-install');
    const installPathInput = document.getElementById('install-path');
    const logList = document.getElementById('log-list');
    const liveIndicator = document.getElementById('live-indicator');
    const themeToggle = document.getElementById('theme-toggle');
    
    // Connection status elements
    const statusDot = document.getElementById('status-dot');
    const statusText = document.getElementById('status-text');
    
    // Installation status elements
    const statusLabel = document.getElementById('status-label');
    const statusIcon = document.querySelector('.status-icon');
    const statusMessage = document.getElementById('status-message');

    // ──────────────────────────────────────────────
    // State Management
    // ──────────────────────────────────────────────
    const state = {
        isConnected: false,
        installationState: 'ready', // ready, pending, downloading, installing-myscoop, failed-myscoop, falling-back, installing-uitars, completed, failed
        currentInstallationTarget: '',
        logBuffer: [],
    };

    const statusLabels = {
        ready: 'Ready',
        pending: 'Pending',
        downloading: 'Downloading',
        'installing-myscoop': 'Installing via MyScoop',
        'failed-myscoop': 'MyScoop Failed',
        'falling-back': 'Falling Back to UI-TARS',
        'installing-uitars': 'Installing via UI-TARS',
        completed: 'Installation Completed',
        failed: 'Installation Failed',
    };

    const statusMessages = {
        ready: 'Waiting for installation to start...',
        pending: 'Installation pending...',
        downloading: 'Downloading installer...',
        'installing-myscoop': 'Installing via MyScoop...',
        'failed-myscoop': 'MyScoop installation failed. Attempting fallback...',
        'falling-back': 'Falling back to UI-TARS automation...',
        'installing-uitars': 'Installing via UI-TARS...',
        completed: 'Installation completed successfully.',
        failed: 'Installation failed. MyScoop and UI-TARS installation attempts were unsuccessful.',
    };

    // ──────────────────────────────────────────────
    // Theme Toggle Logic
    // ──────────────────────────────────────────────
    let isDarkMode = true;
    themeToggle.addEventListener('click', () => {
        isDarkMode = !isDarkMode;
        document.body.setAttribute('data-theme', isDarkMode ? 'dark' : 'light');
        themeToggle.innerHTML = isDarkMode ? '<i class="fa-solid fa-moon"></i>' : '<i class="fa-solid fa-sun"></i>';
    });

    // ──────────────────────────────────────────────
    // Connection Status Management
    // ──────────────────────────────────────────────
    function updateConnectionStatus(isConnected) {
        state.isConnected = isConnected;
        
        if (isConnected) {
            statusDot.classList.remove('disconnected');
            statusDot.classList.add('connected');
            statusText.textContent = 'UI-TARS Connected';
        } else {
            statusDot.classList.remove('connected');
            statusDot.classList.add('disconnected');
            statusText.textContent = 'UI-TARS Disconnected';
        }
    }

    // Simulate connection check (placeholder for actual UI-TARS integration)
    function checkConnection() {
        // In a real implementation, this would check connection to UI-TARS
        // For now, we'll assume connection after a brief delay
        setTimeout(() => {
            updateConnectionStatus(true);
        }, 1000);
    }

    // ──────────────────────────────────────────────
    // Installation Status Management
    // ──────────────────────────────────────────────
    function updateInstallationStatus(newState) {
        if (state.installationState === newState) return;
        
        state.installationState = newState;
        
        // Update status label
        statusLabel.textContent = statusLabels[newState];
        
        // Update status icon
        statusIcon.className = `status-icon ${newState}`;
        
        // Update status message
        const messageEl = statusMessage;
        messageEl.textContent = statusMessages[newState];
        messageEl.classList.remove('warning', 'error', 'success');
        
        if (newState === 'failed' || newState === 'failed-myscoop') {
            messageEl.classList.add('error');
        } else if (newState === 'falling-back' || newState === 'downloading') {
            messageEl.classList.add('warning');
        } else if (newState === 'completed') {
            messageEl.classList.add('success');
        }
    }

    // Parse log messages to detect state transitions
    function parseLogForStatus(logMessage) {
        const lower = logMessage.toLowerCase();
        
        // Check for downloading indicators
        if (lower.includes('download') || lower.includes('downloading')) {
            updateInstallationStatus('downloading');
            return;
        }
        
        // Check for MyScoop installation start
        if (lower.includes('starting install for:') || lower.includes('trying:') && lower.includes('mysc')) {
            updateInstallationStatus('installing-myscoop');
            return;
        }
        
        // Check for MyScoop failure/fallback indicators
        if (lower.includes('myscoop') && (lower.includes('failed') || lower.includes('unsuccessful'))) {
            updateInstallationStatus('failed-myscoop');
            return;
        }
        
        // Check for fallback to UI-TARS
        if (lower.includes('fallback') || (lower.includes('gui') && lower.includes('automat'))) {
            updateInstallationStatus('falling-back');
            return;
        }
        
        // Check for UI-TARS installation
        if (lower.includes('ui-tars') || (lower.includes('gui') && lower.includes('install'))) {
            updateInstallationStatus('installing-uitars');
            return;
        }
        
        // Check for completion
        if (lower.includes('installation') && (lower.includes('complete') || lower.includes('successful'))) {
            updateInstallationStatus('completed');
            return;
        }
        
        // Check for final failure
        if (lower.includes('all installations') && lower.includes('complete') && lower.includes('failed')) {
            updateInstallationStatus('failed');
            return;
        }
    }

    // ──────────────────────────────────────────────
    // Logging Functions
    // ──────────────────────────────────────────────
    function appendLog(msg, type = "normal") {
        const li = document.createElement('li');
        li.className = `log-entry ${type}`;

        const timeSpan = document.createElement('span');
        timeSpan.className = 'time';
        const now = new Date();
        timeSpan.innerText = `[${now.getHours().toString().padStart(2, '0')}:${now.getMinutes().toString().padStart(2, '0')}:${now.getSeconds().toString().padStart(2, '0')}]`;

        const msgSpan = document.createElement('span');
        msgSpan.className = 'msg';
        msgSpan.innerText = ` ${msg}`;

        li.appendChild(timeSpan);
        li.appendChild(msgSpan);

        logList.appendChild(li);
        state.logBuffer.push(msg);

        // Parse log for status updates
        parseLogForStatus(msg);

        // Ensure scroll to bottom
        setTimeout(() => {
            logList.scrollTop = logList.scrollHeight;
        }, 10);
    }

    // ──────────────────────────────────────────────
    // Server-Sent Events for Logs
    // ──────────────────────────────────────────────
    function startLogStream() {
        const evtSource = new EventSource("/api/install/status");

        evtSource.onmessage = function (event) {
            if (event.data === "ping") return;
            liveIndicator.classList.add('active');
            appendLog(event.data);

            // Clear indicator animation
            clearTimeout(window.indicatorTimeout);
            window.indicatorTimeout = setTimeout(() => {
                liveIndicator.classList.remove('active');
            }, 500);
        };

        evtSource.onerror = function (err) {
            liveIndicator.classList.remove('active');
            console.error("SSE connection error:", err);
        };
    }

    // Start SSE listener
    startLogStream();

    // ──────────────────────────────────────────────
    // Installation Initiation
    // ──────────────────────────────────────────────
    async function initiateInstall() {
        const path = installPathInput.value.trim();
        if (!path) {
            alert('Please provide a valid path.');
            installPathInput.focus();
            return;
        }

        const icon = installBtn.querySelector('i');
        icon.className = 'fa-solid fa-circle-notch fa-spin';
        installBtn.disabled = true;

        try {
            // Update UI state
            updateInstallationStatus('pending');
            state.currentInstallationTarget = path;
            
            const resp = await fetch('/api/install', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path: path })
            });
            const result = await resp.json();
            appendLog(`Requested install for: ${path}`, 'system');
            installPathInput.value = '';
        } catch (error) {
            appendLog(`Error requesting install: ${error.message}`, 'system');
            updateInstallationStatus('failed');
        } finally {
            icon.className = 'fa-solid fa-arrow-right';
            installBtn.disabled = false;
        }
    }

    // ──────────────────────────────────────────────
    // Event Listeners
    // ──────────────────────────────────────────────
    installBtn.addEventListener('click', initiateInstall);
    installPathInput.addEventListener('keypress', (e) => {
        if (e.key === 'Enter') initiateInstall();
    });

    // ──────────────────────────────────────────────
    // Initialization
    // ──────────────────────────────────────────────
    checkConnection();
    updateInstallationStatus('ready');
});
