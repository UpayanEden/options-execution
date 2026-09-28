/**
 * dashboard.js — Auto-refresh and kill switch UI logic
 *
 * - Polls /api/health every 30 seconds and updates status indicators.
 * - Polls /api/positions every 30 seconds and refreshes the positions table
 *   in-place without a full page reload (on the dashboard page only).
 * - Manages the kill switch confirmation modal and API calls.
 * - Shows a countdown timer to next refresh.
 */

"use strict";

// ── Configuration ──────────────────────────────────────────────────────────

const REFRESH_INTERVAL_MS = 30000;   // 30 seconds
const HEALTH_CHECK_INTERVAL_MS = 15000;

// ── State ──────────────────────────────────────────────────────────────────

let refreshCountdown = REFRESH_INTERVAL_MS / 1000;
let countdownTimer = null;
let refreshTimer = null;
let healthTimer = null;

// ── Utilities ──────────────────────────────────────────────────────────────

/**
 * Format a number as INR currency string.
 * @param {number} value
 * @returns {string}
 */
function formatINR(value) {
    const num = parseFloat(value) || 0;
    const prefix = num >= 0 ? '+₹' : '-₹';
    return prefix + Math.abs(num).toLocaleString('en-IN', {
        minimumFractionDigits: 0,
        maximumFractionDigits: 0,
    });
}

/**
 * Create a DOM element with optional classes and text content.
 * @param {string} tag
 * @param {string[]} classes
 * @param {string} [text]
 * @returns {HTMLElement}
 */
function el(tag, classes, text) {
    const e = document.createElement(tag);
    if (classes && classes.length) e.classList.add(...classes);
    if (text !== undefined) e.textContent = text;
    return e;
}

// ── Health check ───────────────────────────────────────────────────────────

/**
 * Fetch /api/health and update the kill switch indicator in the navbar.
 */
async function checkHealth() {
    try {
        const resp = await fetch('/api/health', { cache: 'no-store' });
        if (!resp.ok) return;
        const data = await resp.json();

        const ksActive = data.kill_switch_active === true;
        const navbar = document.querySelector('.navbar');
        if (!navbar) return;

        // Update/create the kill switch badge in the navbar
        let ksBadge = document.getElementById('ks-status-badge');
        if (ksActive) {
            if (!ksBadge) {
                ksBadge = document.createElement('span');
                ksBadge.id = 'ks-status-badge';
                ksBadge.className = 'badge kill-switch-active-badge ms-2 px-3 py-2';
                ksBadge.innerHTML = '<i class="bi bi-exclamation-triangle-fill me-1"></i>KILL SWITCH ACTIVE';
                const brand = navbar.querySelector('.navbar-brand');
                if (brand && brand.parentNode) {
                    brand.parentNode.insertBefore(ksBadge, brand.nextSibling);
                }
            }
        } else {
            if (ksBadge) ksBadge.remove();
        }

        // Update last heartbeat
        const lastUpdateEl = document.getElementById('last-update');
        if (lastUpdateEl && data.last_heartbeat) {
            lastUpdateEl.textContent = 'HB: ' + data.last_heartbeat.slice(-8);
        }

    } catch (e) {
        // Silently ignore health check failures
    }
}

// ── Positions refresh ──────────────────────────────────────────────────────

/**
 * Fetch /api/positions and update the stat cards and positions table
 * without triggering a full page reload.
 */
async function refreshPositions() {
    try {
        const [posResp, pnlResp] = await Promise.all([
            fetch('/api/positions', { cache: 'no-store' }),
            fetch('/api/pnl', { cache: 'no-store' }),
        ]);

        if (!posResp.ok || !pnlResp.ok) return;

        const posData = await posResp.json();
        const pnlData = await pnlResp.json();

        // Update stat cards if present
        const openCountEl = document.querySelector('[data-stat="open_count"]');
        if (openCountEl) {
            const openCount = posData.positions.filter(p => p.state === 'OPEN').length;
            openCountEl.textContent = openCount;
        }

        const totalPnlEl = document.querySelector('[data-stat="total_pnl"]');
        if (totalPnlEl) {
            const total = Object.values(pnlData.pnl || {}).reduce((a, b) => a + b, 0);
            totalPnlEl.textContent = formatINR(total);
            totalPnlEl.className = total >= 0
                ? 'stat-card-value pnl-positive'
                : 'stat-card-value pnl-negative';
        }

    } catch (e) {
        // Silently ignore refresh failures
    }
}

// ── Countdown timer ────────────────────────────────────────────────────────

/**
 * Start the countdown display showing seconds until next refresh.
 */
function startCountdown() {
    refreshCountdown = REFRESH_INTERVAL_MS / 1000;

    if (countdownTimer) clearInterval(countdownTimer);
    countdownTimer = setInterval(() => {
        refreshCountdown = Math.max(0, refreshCountdown - 1);
        const el = document.getElementById('refresh-countdown');
        if (el) el.textContent = refreshCountdown + 's';
    }, 1000);
}

// ── Page auto-refresh ──────────────────────────────────────────────────────

/**
 * Schedule a full page reload every REFRESH_INTERVAL_MS milliseconds.
 * Also triggers a lightweight data refresh via the API.
 */
function scheduleRefresh() {
    if (refreshTimer) clearTimeout(refreshTimer);

    refreshTimer = setTimeout(() => {
        // Lightweight API refresh first
        refreshPositions().catch(() => {});
        // Then full page reload
        window.location.reload();
    }, REFRESH_INTERVAL_MS);
}

// ── Kill switch helpers ────────────────────────────────────────────────────

/**
 * Activate or deactivate the kill switch via the REST API.
 * Exposed on window so HTML onclick handlers can call it.
 *
 * @param {string} action  - 'activate' or 'deactivate'
 * @param {string} [reason]
 */
window.toggleKillSwitch = async function toggleKillSwitch(action, reason) {
    const endpoint = `/api/kill_switch/${action}`;
    try {
        const resp = await fetch(endpoint, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ reason: reason || '' }),
        });
        if (resp.ok) {
            window.location.reload();
        } else {
            const text = await resp.text();
            alert('Kill switch action failed:\n' + text);
        }
    } catch (e) {
        alert('Network error: ' + e.message);
    }
};

/**
 * Confirm kill switch activation from the modal.
 * Exposed on window so the base template can call it.
 */
window.confirmKillSwitch = async function confirmKillSwitch() {
    const reasonEl = document.getElementById('killSwitchReason');
    const reason = reasonEl ? reasonEl.value : 'Activated via web UI';
    await window.toggleKillSwitch('activate', reason);
    const modalEl = document.getElementById('killSwitchModal');
    if (modalEl) {
        const modal = bootstrap.Modal.getInstance(modalEl);
        if (modal) modal.hide();
    }
};

// ── Initialisation ─────────────────────────────────────────────────────────

/**
 * Boot the dashboard auto-refresh and health check loops.
 * Called once when the DOM is ready.
 */
function init() {
    // Start health checks
    checkHealth();
    healthTimer = setInterval(checkHealth, HEALTH_CHECK_INTERVAL_MS);

    // Start page auto-refresh countdown
    startCountdown();
    scheduleRefresh();

    // Wire up the countdown badge if present
    const badge = document.querySelector('.badge[class*="auto-refresh"]');
    if (badge && !document.getElementById('refresh-countdown')) {
        const span = document.createElement('span');
        span.id = 'refresh-countdown';
        span.textContent = refreshCountdown + 's';
        badge.innerHTML = '<i class="bi bi-arrow-repeat me-1"></i>Refresh in ';
        badge.appendChild(span);
    }
}

// Run after DOM is ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
} else {
    init();
}
