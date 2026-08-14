// ─── API & Core Helpers ───
const token = () => localStorage.getItem('vernika_token') || '';
const authHeaders = () => ({ 'Authorization': `Bearer ${token()}`, 'Content-Type': 'application/json' });
var allLeads = [];

/**
 * Optional URL prefix when the API is mounted under a subpath (reverse proxy).
 * Set from console HTML: <meta name="vernika-api-root" content="/vernika">
 * or before load: window.__VERN_API_ROOT__ = '/vernika';
 */
function apiRoot() {
    if (typeof window !== 'undefined' && window.__VERN_API_ROOT__) {
        return String(window.__VERN_API_ROOT__).replace(/\/$/, '');
    }
    const meta =
        typeof document !== 'undefined' ? document.querySelector('meta[name="vernika-api-root"]') : null;
    if (meta && meta.content && meta.content.trim()) {
        return meta.content.trim().replace(/\/$/, '');
    }
    return '';
}

/** Absolute path for API calls, e.g. apiUrl('/api/tuning?role=sellers') */
function apiUrl(pathWithQuery) {
    const p = pathWithQuery.startsWith('/') ? pathWithQuery : '/' + pathWithQuery;
    const root = apiRoot();
    return root ? root + p : p;
}

/** Roles tied to the login account. */
const LOCKED_CONSOLE_ROLES = [];
const SANDBOX_ROLES = ['sales_1', 'sales_2'];
const ADMIN_ROLES = ['maruti'];

function jwtPayload() {
    try {
        const t = token();
        if (!t) return null;
        const parts = t.split('.');
        if (parts.length < 2) return null;
        let b64 = parts[1].replace(/-/g, '+').replace(/_/g, '/');
        const pad = (4 - (b64.length % 4)) % 4;
        if (pad) b64 += '='.repeat(pad);
        return JSON.parse(atob(b64));
    } catch (_) {
        return null;
    }
}

/** Role from JWT (authoritative after login). */
function loginRoleFromToken() {
    const p = jwtPayload();
    if (!p || !p.role) return null;
    return normalizeRole(p.role);
}

function isLockedConsoleLogin() {
    if (window.__VERN_SESSION__ && window.__VERN_SESSION__.locked) return true;
    const r = loginRoleFromToken();
    return !!(r && LOCKED_CONSOLE_ROLES.includes(r));
}

/** Server truth for dashboard role (set during console bootstrap). */
function dashboardRole() {
    if (window.__VERN_SESSION__ && window.__VERN_SESSION__.dashboard_role) {
        return normalizeRole(window.__VERN_SESSION__.dashboard_role);
    }
    const jwtRole = loginRoleFromToken();
    if (jwtRole) return normalizeRole(jwtRole);
    return null;
}

function isAdminRole(role) {
    return ADMIN_ROLES.includes(String(role || '').toLowerCase());
}

function isSandboxRole(role) {
    return SANDBOX_ROLES.includes(String(role || '').toLowerCase());
}

function apiRoleQ() {
    const mem = typeof currentRole !== 'undefined' ? currentRole : null;
    const r = (mem && isSandboxRole(mem)) ? mem : (localStorage.getItem('vernika_role') || 'maruti');
    return encodeURIComponent(normalizeRole(r));
}

function isDataEdgeCounselorHost() {
    return true;
}

async function bootstrapConsoleSession() {
    const res = await fetch(apiUrl('/api/me'), {
        headers: authHeaders(),
        credentials: 'same-origin',
    });
    if (res.status === 401) {
        if (typeof logout === 'function') logout();
        else window.location.href = '/login';
        return null;
    }
    if (!res.ok) {
        throw new Error('Could not load session (' + res.status + ')');
    }
    const data = await res.json();
    window.__VERN_SESSION__ = data;
    const dr = normalizeRole(data.dashboard_role || data.role || 'maruti');
    if (typeof currentRole !== 'undefined') currentRole = dr;
    localStorage.setItem('vernika_role', dr);
    if (data.email) localStorage.setItem('vernika_email', data.email);
    return data;
}

function normalizeRole(r) {
    const role = String(r || '').trim().toLowerCase();
    if (SANDBOX_ROLES.includes(role)) return role;
    if (ADMIN_ROLES.includes(role)) return role;
    return 'sales_1';
}

function escapeHtml(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function formatTime(iso) {
    if (!iso) return '—';
    try {
        let s = String(iso);
        const hasTZ = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(s);
        if (!hasTZ && /\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(s)) s = s + 'Z';
        const d = new Date(s);
        if (isNaN(d.getTime())) return iso;
        const now = new Date();
        const sameDay = d.toLocaleDateString('en-IN', { timeZone: 'Asia/Kolkata' }) === now.toLocaleDateString('en-IN', { timeZone: 'Asia/Kolkata' });
        if (sameDay) return d.toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit' });
        return d.toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
    } catch { return iso; }
}

/** Format instant in Indian Standard Time (for deferred recall badges, etc.). */
function formatTimeIST(iso) {
    if (!iso) return '—';
    try {
        let s = String(iso);
        const hasTZ = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(s);
        if (!hasTZ && /\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(s)) s = s + 'Z';
        const d = new Date(s);
        if (isNaN(d.getTime())) return iso;
        return d.toLocaleString('en-IN', {
            timeZone: 'Asia/Kolkata',
            month: 'short',
            day: 'numeric',
            hour: '2-digit',
            minute: '2-digit',
            hour12: true,
        }) + ' IST';
    } catch (_) {
        return iso;
    }
}

function starsHtml(n) {
    n = Math.max(0, Math.min(5, parseInt(n || 0)));
    let html = '';
    for (let i = 1; i <= 5; i++) {
        html += `<span class="star ${i <= n ? 'on' : ''}">★</span>`;
    }
    return html;
}

function dispoTagClass(d) {
    const s = (d || '').toLowerCase();
    if (s.includes('site visit') || s === 'site_visit' || s === 'site visit scheduled') return 'tag-sv';
    if (s.includes('not interested') || s === 'not_interested') return 'tag-noint';
    if (s.includes('interested')) return 'tag-int';
    if (s.includes('call later') || s.includes('callback')) return 'tag-cbk';
    if (s.includes('wrong') || s === 'failed' || s === 'error') return 'tag-fail';
    if (s.includes('no response') || s.includes('no_response') || s.includes('voicemail') || s.includes('voice mail') || s === 'busy' || s === 'no answer') return 'tag-nores';
    if (s === 'completed') return 'tag-int';
    return 'tag-cbk';
}

function prettyStatus(s) {
    const x = (s || '').toString().trim();
    if (!x) return '';
    if (x === 'not_interested') return 'Not Interested';
    if (x === 'completed') return 'Completed';
    if (x === 'failed') return 'Failed';
    if (x === 'callback_scheduled') return 'Callback scheduled';
    if (x === 'callback_completed') return 'Callback completed';
    if (x === 'dialing') return 'Dialing…';
    if (x === 'site_visit') return 'Site Visit';
    if (x === 'site_visited') return 'Site Visited';
    return x.charAt(0).toUpperCase() + x.slice(1);
}

/** Extract lead timestamp in milliseconds from start_time (epoch seconds) or called_at_iso. */
function getLeadTimestampMs(lead) {
    if (!lead || typeof lead !== 'object') return NaN;
    var st = lead.start_time;
    if (st != null && Number(st) > 0) return Number(st) * 1000;
    var iso = lead.called_at_iso;
    if (!iso) return NaN;
    try {
        var s = String(iso);
        var hasTZ = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(s);
        if (!hasTZ && /\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(s)) s = s + 'Z';
        var t = Date.parse(s);
        return isNaN(t) ? NaN : t;
    } catch (_) { return NaN; }
}

function _parseAnalysisBlobClient(raw) {
    if (raw != null && typeof raw === 'object' && !Array.isArray(raw)) return raw;
    if (typeof raw === 'string' && raw.trim()) {
        try {
            const o = JSON.parse(raw);
            return o && typeof o === 'object' && !Array.isArray(o) ? o : {};
        } catch (_) {}
    }
    return {};
}

/** Soft interest in summary/next_steps (send email, will check, etc.) — mirrors ``transcript_interest.py``. */
function softInterestInLeadText(lead) {
    const aj = _parseAnalysisBlobClient(lead && lead.analysis);
    if (lead && lead.outcome_from_transcript) return true;
    if (aj.outcome_from_transcript) return true;
    const blob = [
        lead && lead.summary,
        aj.summary,
        lead && lead.next_steps,
        aj.next_steps,
    ].filter(Boolean).join(' ');
    if (!blob || blob.length < 8) return false;
    const neg = /not\s+interested|don'?t\s+call|stop\s+calling|wrong\s+number|take\s+me\s+off/i;
    if (neg.test(blob)) return false;
    const pos = /send\s+(me|us|the|details|information|a\s+note|write.?up)|(?:email|whatsapp).{0,40}(?:send|share|bhej)|(?:send|share).{0,30}(?:email|whatsapp|mail)|(?:provide|give).{0,20}email|information\s+via\s+email|will\s+check|i'?ll\s+check|let\s+me\s+check|decide\s+on\s+that|expressed\s+interest|mail\s+kar|bhej\s+dijiye|@[\w.-]+\.(?:com|in)\b/i;
    return pos.test(blob);
}

/** Match ``enrich_lead_for_console`` / ``effective_disposition_console`` (disposition may live in ``analysis`` only). */
function effectiveDispo(lead) {
    const s = String((lead && lead.status) || '').trim().toLowerCase();
    if (s === 'callback_scheduled') return 'Callback Scheduled';
    if (s === 'callback_completed') return 'Callback Completed';
    if (s === 'site_visit' || s === 'site_visited') return 'Site Visit';
    if (s === 'not_interested') return 'Not Interested';
    if (s === 'failed') return 'Failed';
    if (s === 'busy') return 'Busy';
    if (s === 'no answer' || s === 'no-answer') return 'No Answer';

    const aj = _parseAnalysisBlobClient(lead && lead.analysis);
    const d = String((lead && lead.disposition) || aj.disposition || '').trim();
    if (d && d !== 'Answered') return d;
    if (softInterestInLeadText(lead)) return 'Interested';
    if (d) return d;
    return prettyStatus((lead && lead.status) || '');
}

/** Resolve a lead row from ``allLeads`` (onclick may pass string ids). */
function findLeadById(leadId) {
    const nid = Number(leadId);
    if (!Number.isFinite(nid)) return null;
    return allLeads.find(function (l) { return Number(l.id) === nid; }) || null;
}

function _normPhoneDigits(phone) {
    return String(phone || '').replace(/\D/g, '').slice(-10);
}

/**
 * Duplicate DB rows (same phone, different ids): one may lack ``_log_id`` while a sibling has
 * transcript + recording. Returns the best row id + log for media APIs.
 */
function resolveLeadMediaContext(lead) {
    if (!lead || lead.id == null) {
        return { leadId: null, logId: '', hasMedia: false };
    }
    let logId = String(lead._log_id || lead.log_id || '').trim();
    let leadId = Number(lead.id);
    const phone = _normPhoneDigits(lead.phone);
    const role = lead.role || (typeof currentRole !== 'undefined' ? currentRole : '');
    if (!logId && phone && Array.isArray(allLeads)) {
        for (let i = 0; i < allLeads.length; i++) {
            const s = allLeads[i];
            if (!s || s.id == null) continue;
            if (role && s.role && s.role !== role) continue;
            if (_normPhoneDigits(s.phone) !== phone) continue;
            const sid = String(s._log_id || s.log_id || '').trim();
            if (sid) {
                logId = sid;
                if (!String(lead._log_id || lead.log_id || '').trim()) {
                    leadId = Number(s.id);
                }
                break;
            }
        }
    }
    return {
        leadId: leadId,
        logId: logId,
        hasMedia: !!logId,
    };
}

/** Stream URL for ``<audio src>`` (cannot send Authorization header on element src). */
function campaignRecordingStreamUrl(leadId) {
    const t = token();
    let url = apiUrl('/api/campaign/lead/' + leadId + '/recording?role=' + apiRoleQ());
    if (t) url += (url.indexOf('?') >= 0 ? '&' : '?') + 'access_token=' + encodeURIComponent(t);
    return url;
}

/** Stream URL for manual call recording (same pattern). */
function manualCallRecordingStreamUrl(callId) {
    const t = token();
    let url = apiUrl('/api/manual/calls/' + callId + '/recording?role=' + apiRoleQ());
    if (t) url += (url.indexOf('?') >= 0 ? '&' : '?') + 'access_token=' + encodeURIComponent(t);
    return url;
}

/** Count QA dispositions from loaded manifest rows (fallback when state JSON is stale). */
function countDispositionFromLeads(leads) {
    const keys = ['Interested', 'Not Interested', 'Call Later', 'Busy', 'Callback', 'Answered', 'Failed'];
    const buckets = {};
    keys.forEach(function (k) { buckets[k] = 0; });
    (Array.isArray(leads) ? leads : []).forEach(function (lead) {
        if (!isCalled(lead)) return;
        if (isFailed(lead)) {
            buckets.Failed += 1;
            return;
        }
        const st = String(lead.status || '').toLowerCase();
        if (st === 'not_interested') {
            buckets['Not Interested'] += 1;
            return;
        }
        const ed = effectiveDispo(lead);
        const el = ed.toLowerCase();
        if (ed === 'Interested' || (el.includes('interested') && !el.includes('not interested'))) {
            buckets.Interested += 1;
        } else if (ed === 'Not Interested' || el.includes('not interested')) {
            buckets['Not Interested'] += 1;
        } else if (ed === 'Call Later' || el.includes('call later')) {
            buckets['Call Later'] += 1;
        } else if (ed === 'Busy' || el.includes('busy')) {
            buckets.Busy += 1;
        } else if (ed === 'Callback' || el.includes('callback')) {
            buckets.Callback += 1;
        } else if (st === 'completed' || ed === 'Answered') {
            buckets.Answered += 1;
        }
    });
    return buckets;
}

/** Count leads where site_visit_agreed is true and has a particular date. */
function countSiteVisitFromLeads(leads) {
    return (Array.isArray(leads) ? leads : []).filter(hasSiteVisitWithParticularDate).length;
}

/** Prefer server aggregates; fall back to manifest-derived counts. */
function resolveDashboardCounts(data, leads) {
    data = data || {};
    const dc = data.disposition_counts || {};
    const fromApi = Number(dc.Interested);
    const fromApiNi = Number(dc['Not Interested']);
    const hasApi =
        Number.isFinite(fromApi) && fromApi >= 0 &&
        Number.isFinite(fromApiNi) && fromApiNi >= 0 &&
        (fromApi > 0 || fromApiNi > 0 || Number(data.called_count) > 0);
    if (hasApi) {
        return {
            interested: fromApi,
            notInterested: countSiteVisitFromLeads(leads),
            failed: Number(dc.Failed) || 0,
            dispositionCounts: dc,
        };
    }
    const chartTotal = Number(data.chart_interested_total);
    if (Number.isFinite(chartTotal) && chartTotal > 0) {
        return {
            interested: chartTotal,
            notInterested: countSiteVisitFromLeads(leads),
            failed: Number(dc.Failed) || 0,
            dispositionCounts: dc,
        };
    }
    const computed = countDispositionFromLeads(leads);
    return {
        interested: Number(computed.Interested) || 0,
        notInterested: countSiteVisitFromLeads(leads),
        failed: Number(computed.Failed) || 0,
        dispositionCounts: computed,
    };
}

function isCalled(lead) {
    if (!lead) return false;
    var status = String(lead.status || '').toLowerCase();
    if (status === 'failed' || status === 'error' || status === 'no answer' || status === 'busy') return true;
    var logId = String(lead.log_id || lead._log_id || '').trim();
    var st = Number(lead.start_time);
    return logId !== '' || (Number.isFinite(st) && st > 0);
}

function isLeadCalledInDateRange(lead, range) {
    if (!lead) return false;
    if (!isCalled(lead)) return false;
    if (!range) return true;
    
    var t = getLeadTimestampMs(lead);
    if (!Number.isFinite(t) || isNaN(t)) {
        return false;
    }
    return t >= range.fromMs && t <= range.toMs;
}

/** Prefer API ``contact_display_*`` when ``name`` was a sheet row counter (``11.0``) wrongly mapped as contact. */
function leadContactPrimary(lead) {
    if (!lead) return '';
    const p = (lead.contact_display_primary != null ? String(lead.contact_display_primary) : '').trim();
    if (p) return p;
    const n = (lead.name != null ? String(lead.name) : '').trim();
    return n || '';
}

function leadContactSecondary(lead) {
    if (!lead) return '';
    const s = (lead.contact_display_secondary != null ? String(lead.contact_display_secondary) : '').trim();
    if (s) return s;
    return (lead.company != null ? String(lead.company) : '').trim();
}

function isFailed(lead) {
    if (!lead) return false;
    const s = (lead.status || '').toLowerCase();
    const ed = String(effectiveDispo(lead) || '').trim().toLowerCase();
    const isFailedStatus = s === 'failed' || s === 'error' || s === 'no answer' || s === 'busy' || s === 'no response' || s === 'no_response';
    const isFailedDispo = ed === 'failed' || ed === 'no answer' || ed === 'busy' || ed === 'wrong number' || ed === 'not available' || ed === 'voicemail' || ed === 'no response';
    return isFailedStatus || isFailedDispo;
}

function failureSeverityClass(sev) {
    const s = (String(sev || '').toLowerCase());
    if (s === 'info') return 'fail-sev-info';
    if (s === 'warning') return 'fail-sev-warning';
    if (s === 'muted') return 'fail-sev-muted';
    return 'fail-sev-error';
}

/** Table / manifest cell: labeled failure from API, or raw failure_reason. */
function formatFailureCell(r) {
    const title = (r.failure_title || '').trim();
    const detail = (r.failure_detail || '').trim();
    const raw = (r.failure_reason || '').trim();
    const isFailedStatus = isFailed(r);
    if ((!title && !raw) || !isFailedStatus) {
        return '<span style="color:var(--text-secondary);font-size:12px;">—</span>';
    }
    const label = title || raw;
    const sevCls = title ? failureSeverityClass(r.failure_severity) : 'fail-sev-error';
    const secondaryBits = [];
    if (detail && detail !== label) secondaryBits.push(detail);
    if (raw && raw !== label && raw !== detail) secondaryBits.push(raw);
    const secondary = secondaryBits.join(' · ');
    const secondaryHtml = secondary
        ? `<div style="font-size:11px;color:var(--text-secondary);margin-top:4px;line-height:1.35;max-width:260px;white-space:normal;word-break:break-word;" title="${escapeHtml(secondary)}">${escapeHtml(secondary.length > 90 ? secondary.substring(0, 90) + '…' : secondary)}</div>`
        : '';
    const tip = [label, secondary].filter(Boolean).join(' — ');
    return `<div style="display:flex;flex-direction:column;align-items:flex-start;gap:0;">
        <span class="failure-chip ${sevCls}" title="${escapeHtml(tip)}">${escapeHtml(label)}</span>
        ${secondaryHtml}
    </div>`;
}

/** For failed leads, replace the empty Summary cell with a clear reason line. */
function failureSummaryHtml(r) {
    const title = (r.failure_title || '').trim();
    const raw = (r.failure_reason || '').trim();
    const detail = (r.failure_detail || '').trim();
    const label = title || raw || 'Call did not connect';
    const sev = (r.failure_severity || 'error').toLowerCase();
    const color = sev === 'info' ? '#007AFF'
        : sev === 'warning' ? '#CC7700'
        : sev === 'muted' ? 'var(--text-secondary)'
        : 'var(--danger)';
    const secondary = (detail && detail !== label) ? detail : (raw && raw !== label ? raw : '');
    return `<div style="display:flex;flex-direction:column;gap:4px;">
        <span style="font-size:12px;font-weight:700;color:${color};">Why this failed: ${escapeHtml(label)}</span>
        ${secondary ? `<span style="font-size:11px;color:var(--text-secondary);line-height:1.4;">${escapeHtml(secondary.length > 130 ? secondary.substring(0, 130) + '…' : secondary)}</span>` : ''}
    </div>`;
}

function fillCallFailureModal(lead) {
    const fb = document.getElementById('cd-failure-block');
    const catEl = document.getElementById('cd-failure-category');
    const titleEl = document.getElementById('cd-failure-title-line');
    const detEl = document.getElementById('cd-failure-detail-line');
    if (!fb || !catEl || !titleEl || !detEl) return;

    const title = (lead.failure_title || '').trim();
    const detail = (lead.failure_detail || '').trim();
    const raw = (lead.failure_reason || '').trim();
    const cat = (lead.failure_category || '').trim();

    if (!isFailed(lead) || (!title && !raw)) {
        fb.style.display = 'none';
        fb.className = 'cd-failure-block fail-sev-error';
        catEl.style.display = 'none';
        catEl.textContent = '';
        titleEl.textContent = '';
        detEl.style.display = 'none';
        detEl.textContent = '';
        return;
    }
    const primary = title || raw;
    const sev = title ? failureSeverityClass(lead.failure_severity) : 'fail-sev-error';
    fb.style.display = 'block';
    fb.className = 'cd-failure-block ' + sev;
    if (cat) {
        catEl.style.display = 'block';
        catEl.textContent = cat;
    } else {
        catEl.style.display = 'none';
        catEl.textContent = '';
    }
    titleEl.textContent = primary;
    const secondaryBits = [];
    if (detail && detail !== primary) secondaryBits.push(detail);
    if (raw && raw !== primary && (!detail || raw !== detail)) secondaryBits.push(raw);
    const secondary = secondaryBits.join('\n\n');
    if (secondary) {
        detEl.style.display = 'block';
        detEl.textContent = secondary;
    } else {
        detEl.style.display = 'none';
        detEl.textContent = '';
    }
}

function showToast(msg, type = 'info', ms = 4000) {
    const host = document.getElementById('toast-host');
    if (!host) return;
    const t = document.createElement('div');
    t.className = `vernika-toast ${type}`;
    t.textContent = msg;
    host.appendChild(t);
    const hideMs = typeof ms === 'number' && ms > 0 ? ms : 4000;
    setTimeout(() => {
        t.style.animation = 'toastIn 0.32s ease reverse forwards';
        setTimeout(() => t.remove(), 400);
    }, hideMs);
}

function hasSiteVisitWithParticularDate(l) {
    if (!l) return false;
    
    // Check if site visit is agreed in any form
    var agreed = l.site_visit_agreed === true || l.status === 'site_visit' || l.status === 'site_visited';
    if (!agreed && l.analysis) {
        try {
            var aj = typeof l.analysis === 'string' ? JSON.parse(l.analysis) : l.analysis;
            if (aj && aj.site_visit_agreed === true) {
                agreed = true;
            }
        } catch(e) {}
    }
    
    return agreed;
}


function isFollowUpLead(lead) {
    if (!lead) return false;
    if (isFailed(lead)) return false;
    var d = String(effectiveDispo(lead) || '').trim().toLowerCase();
    var s = String(lead.status || '').trim().toLowerCase();
    
    if (s === 'callback_scheduled') {
        s = 'callback scheduled';
    }
    
    var validLabels = [
        "call later",
        "callback",
        "callback scheduled",
        "call back later",
        "call me after sometime",
        "rescheduling calls"
    ];
    
    var hasValidLabel = validLabels.indexOf(d) !== -1 || validLabels.indexOf(s) !== -1;
    if (hasValidLabel) return true;
    
    if (lead.callback_reminder_at_iso || lead.requested_callback_datetime_iso) {
        return true;
    }
    
    if (lead.analysis) {
        try {
            var aj = typeof lead.analysis === 'string' ? JSON.parse(lead.analysis) : lead.analysis;
            if (aj && (aj.callback_reminder_epoch || aj.requested_callback_datetime_iso)) {
                return true;
            }
        } catch(e) {}
    }
    
    return false;
}

function _istDayStartMs(dateStr) {
    if (!dateStr) return 0;
    var parts = dateStr.split('-');
    var y = parseInt(parts[0], 10);
    var m = parseInt(parts[1], 10) - 1;
    var d = parseInt(parts[2], 10);
    return Date.UTC(y, m, d) - 5.5 * 60 * 60 * 1000;
}

function _istDayEndMs(dateStr) {
    if (!dateStr) return Infinity;
    return _istDayStartMs(dateStr) + 24 * 60 * 60 * 1000 - 1;
}

function getSiteVisitDateStr(l) {
    if (!l) return null;
    
    // 1. Gather all text fields
    var text = String(l.next_steps || '') + ' ' + String(l.summary || '');
    if (l.next_action && typeof l.next_action === 'object') {
        text += ' ' + String(l.next_action.details || '') + ' ' + String(l.next_action.datetime_iso || '');
    }
    if (l.analysis) {
        try {
            var aj = typeof l.analysis === 'string' ? JSON.parse(l.analysis) : l.analysis;
            if (aj) {
                text += ' ' + String(aj.next_steps || '') + ' ' + String(aj.summary || '');
                if (aj.next_action) {
                    text += ' ' + String(aj.next_action.details || '') + ' ' + String(aj.next_action.datetime_iso || '');
                }
            }
        } catch(e) {}
    }
    
    // 2. Look for YYYY-MM-DD pattern in the text
    var dateMatch = text.match(/\b(\d{4})-(\d{2})-(\d{2})\b/);
    if (dateMatch) {
        return dateMatch[0]; // returns "YYYY-MM-DD"
    }
    
    // 3. Fallback to call date (start_time) + tomorrow/today logic
    var callTimeMs = Number(l.start_time) * 1000;
    if (!Number.isFinite(callTimeMs)) {
        if (l.called_at_iso) {
            callTimeMs = Date.parse(l.called_at_iso);
        }
    }
    
    if (Number.isFinite(callTimeMs)) {
        // Format call date in IST (UTC + 5.5 hours)
        var callDateIST = new Date(callTimeMs + 5.5 * 60 * 60 * 1000);
        var lower = text.toLowerCase();
        
        if (lower.indexOf('tomorrow') !== -1) {
            var tomorrow = new Date(callDateIST.getTime() + 24 * 60 * 60 * 1000);
            return tomorrow.toISOString().slice(0, 10);
        }
        
        return callDateIST.toISOString().slice(0, 10);
    }
    
    return null;
}


var incomingCallsList = [];

async function fetchIncomingCallsForDashboard() {
    try {
        const res = await fetch(apiUrl(`/api/incoming/calls/recent?role=${apiRoleQ()}&limit=5000`), {
            headers: { 'Authorization': `Bearer ${token()}` },
            credentials: 'same-origin'
        });
        if (res.ok) {
            const data = await res.json();
            incomingCallsList = Array.isArray(data.items) ? data.items : [];
            console.log('[DEBUG fetchIncomingCallsForDashboard] Loaded ' + incomingCallsList.length + ' incoming calls.');
        } else {
            console.warn('Failed to fetch incoming calls', res.status);
        }
    } catch (e) {
        console.error('Failed to fetch incoming calls for dashboard stats', e);
    }
}

