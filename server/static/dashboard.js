const MISSING_VALUE = '—';
const RELIABLE_RR_QUALITIES = new Set(['GOOD', 'MODERATE']);
const POLL_INTERVAL_MS = 3000;

let selectedPatient = null;
let chart = null;
let pendingActionType = null;
let pendingHandoffPatientId = null;
let pendingDeviceRemoval = null;
let lastDevices = [];
let pollInFlight = false;

let lastAlertStatus = {};
let alertIntervals = {};
let mutedPatients = {};
let recheckIntervals = {};
let audioEnabled = false;
let audioCtx = null;

class ApiError extends Error {
    constructor(message, payload = {}, status = 0) {
        super(message);
        this.name = 'ApiError';
        this.payload = payload;
        this.status = status;
    }
}

function escapeHtml(value) {
    return String(value)
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#039;');
}

function escapeAttribute(value) {
    return escapeHtml(value).replaceAll('`', '&#096;');
}

function showToast(message, type = 'info') {
    const region = document.getElementById('toastRegion');
    if (!region) return;

    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.setAttribute('role', type === 'error' ? 'alert' : 'status');
    toast.textContent = message;
    region.appendChild(toast);

    window.setTimeout(() => toast.remove(), 4500);
}

function setServerState(state, text) {
    const badge = document.getElementById('serverState');
    const label = document.getElementById('serverStateText');
    if (!badge || !label) return;
    badge.className = `connection-badge state-${state}`;
    label.textContent = text;
}

async function apiRequest(url, options = {}) {
    try {
        const response = await fetch(url, options);
        const payload = await response.json().catch(() => ({}));
        setServerState('online', 'Server connected');

        if (!response.ok || payload.ok === false || payload.accepted === false) {
            throw new ApiError(
                payload.error || `Request failed with status ${response.status}`,
                payload,
                response.status
            );
        }
        return payload;
    } catch (error) {
        if (!(error instanceof ApiError)) {
            setServerState('error', 'Server unavailable');
            throw new ApiError('Could not reach the EdgeER server.');
        }
        throw error;
    }
}

function statusClass(value) {
    return `status-${String(value || 'unknown')
        .toLowerCase()
        .replaceAll('_', '-')
        .replace(/[^a-z0-9-]/g, '-')}`;
}

function statusBadge(value) {
    const label = value || 'UNKNOWN';
    return `<span class="status-badge ${statusClass(label)}">${escapeHtml(label)}</span>`;
}

function formatNumber(value, digits = 1) {
    if (value === null || value === undefined || value === '') return MISSING_VALUE;
    const numericValue = Number(value);
    return Number.isFinite(numericValue)
        ? numericValue.toFixed(digits).replace(/\.0$/, '')
        : MISSING_VALUE;
}

function formatTemperature(packet) {
    const value = packet?.object_temp_c
        ?? packet?.temperature_c
        ?? packet?.temperature;
    return value === null || value === undefined
        ? MISSING_VALUE
        : `${formatNumber(value, 1)} °C`;
}

function formatDuration(seconds) {
    if (seconds === null || seconds === undefined) return 'Not started';
    const safeSeconds = Math.max(0, Math.floor(Number(seconds) || 0));
    if (safeSeconds < 60) return `${safeSeconds}s ago`;

    const minutes = Math.floor(safeSeconds / 60);
    if (minutes < 60) return `${minutes}m ago`;

    const hours = Math.floor(minutes / 60);
    const remainingMinutes = minutes % 60;
    return remainingMinutes ? `${hours}h ${remainingMinutes}m ago` : `${hours}h ago`;
}

function parseTimestamp(value) {
    if (!value) return null;
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? null : date;
}

function formatTimestamp(value) {
    const date = parseTimestamp(value);
    if (!date) return value ? escapeHtml(value) : MISSING_VALUE;
    return new Intl.DateTimeFormat(undefined, {
        year: 'numeric',
        month: 'short',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit'
    }).format(date);
}

function formatTimeOnly(value) {
    const date = parseTimestamp(value);
    if (!date) return MISSING_VALUE;
    return new Intl.DateTimeFormat(undefined, {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit'
    }).format(date);
}

function formatCountdown(seconds) {
    const safeSeconds = Math.max(0, Number(seconds || 0));
    if (safeSeconds < 60) return `${Math.floor(safeSeconds)}s`;
    const minutes = Math.floor(safeSeconds / 60);
    if (minutes < 60) return `${minutes}m`;
    const hours = Math.floor(minutes / 60);
    const remainingMinutes = minutes % 60;
    return remainingMinutes ? `${hours}h ${remainingMinutes}m` : `${hours}h`;
}

function hasRrMetadata(packet) {
    if (!packet) return false;
    return [
        packet.resp_rate_bpm,
        packet.rr_source,
        packet.rr_confidence,
        packet.rr_quality
    ].some(value => value !== null && value !== undefined && value !== '');
}

function normalizeRrQuality(value) {
    return String(value || 'UNAVAILABLE').toUpperCase();
}

function rrQualityClass(quality) {
    if (quality === 'GOOD') return 'rr-good';
    if (quality === 'MODERATE') return 'rr-moderate';
    return 'rr-unreliable';
}

function formatRrConfidence(value) {
    if (value === null || value === undefined || value === '') return MISSING_VALUE;
    const numericValue = Number(value);
    return Number.isFinite(numericValue) ? numericValue.toFixed(2) : MISSING_VALUE;
}

function format_rr(packet) {
    if (!hasRrMetadata(packet)) {
        return '<span class="rr-inline rr-unreliable">No RR data</span>';
    }

    const quality = normalizeRrQuality(packet.rr_quality);
    const qualityClass = rrQualityClass(quality);
    const source = escapeHtml(packet.rr_source || 'UNAVAILABLE');
    const confidence = formatRrConfidence(packet.rr_confidence);
    const numericRate = Number(packet.resp_rate_bpm);
    const hasRate = packet.resp_rate_bpm !== null
        && packet.resp_rate_bpm !== undefined
        && Number.isFinite(numericRate);
    const reliable = hasRate
        && RELIABLE_RR_QUALITIES.has(quality)
        && packet.rr_fresh !== false;

    let valueText = `Withheld · ${quality}`;
    if (reliable) {
        valueText = `${numericRate.toFixed(1)} breaths/min`;
    } else if (hasRate && packet.rr_fresh === false) {
        valueText = 'Stale / withheld';
    }

    return `
        <span class="rr-inline ${qualityClass}">${escapeHtml(valueText)}</span>
        <span class="rr-source">${source} · confidence ${confidence}</span>
    `;
}

function renderResearchRr(packet) {
    const detail = document.getElementById('researchRrDetail');
    if (!detail) return;

    if (!hasRrMetadata(packet)) {
        detail.className = 'rr-detail rr-unavailable';
        detail.innerHTML = '<strong>No RR data</strong><span class="rr-disclaimer">The device has not reported PPG-derived RR metadata for this encounter.</span>';
        return;
    }

    const quality = normalizeRrQuality(packet.rr_quality);
    const qualityClass = rrQualityClass(quality);
    const numericRate = Number(packet.resp_rate_bpm);
    const hasRate = packet.resp_rate_bpm !== null
        && packet.resp_rate_bpm !== undefined
        && Number.isFinite(numericRate);
    const reliable = hasRate
        && RELIABLE_RR_QUALITIES.has(quality)
        && packet.rr_fresh !== false;
    const valueText = reliable
        ? `${numericRate.toFixed(1)} breaths/min`
        : packet.rr_fresh === false && hasRate
            ? 'Stale / withheld'
            : 'Pending / withheld';
    const caution = reliable
        ? 'Eligible research estimate; not used for server priority status.'
        : 'Technical RR state only; no numeric value is used as deterioration evidence.';

    detail.className = `rr-detail ${qualityClass}`;
    detail.innerHTML = `
        <strong>${escapeHtml(valueText)}</strong>
        <div class="rr-detail-row">
            <span class="rr-quality ${qualityClass}">${escapeHtml(quality)}</span>
            <span>Confidence ${formatRrConfidence(packet.rr_confidence)}</span>
            <span>Source ${escapeHtml(packet.rr_source || 'UNAVAILABLE')}</span>
        </div>
        <span class="rr-disclaimer">${escapeHtml(caution)}</span>
    `;
}

function patientRowHtml(patient) {
    const patientId = String(patient.patient_id || MISSING_VALUE);
    const displayName = patient.display_name && patient.display_name !== patientId
        ? patient.display_name
        : '';
    const reasons = (patient.reasons || []).length
        ? patient.reasons.join(', ')
        : MISSING_VALUE;
    const recheck = patient.recheck_due
        ? '<span class="RECHECK_BADGE">RECHECK DUE</span>'
        : patient.recheck_seconds_left !== null
            ? `<span class="cell-secondary">Recheck in ${formatCountdown(patient.recheck_seconds_left)}</span>`
            : MISSING_VALUE;
    const deescalateButton = patient.priority
        ? `<button class="button button-small button-secondary" type="button" data-deescalate-patient="${escapeAttribute(patientId)}">De-escalate</button>`
        : '';
    const alertButton = isAlertStatus(patient.status)
        ? `<button class="button button-small button-secondary" type="button" data-mute-patient="${escapeAttribute(patientId)}">${mutedPatients[patientId] ? 'Muted' : 'Mute'}</button>`
        : MISSING_VALUE;

    return `
        <tr tabindex="0" data-patient-id="${escapeAttribute(patientId)}" data-priority="${Boolean(patient.priority)}" data-selected="${patientId === selectedPatient}">
            <td data-column="patient">
                <span class="cell-primary">${escapeHtml(patientId)}</span>
                ${displayName ? `<span class="cell-secondary">${escapeHtml(displayName)}</span>` : ''}
            </td>
            <td data-column="complaint">${escapeHtml(patient.chief_complaint || MISSING_VALUE)}</td>
            <td data-column="triage">${escapeHtml(patient.triage_level || MISSING_VALUE)}</td>
            <td data-column="location">${escapeHtml(patient.location || MISSING_VALUE)}</td>
            <td data-column="hr" class="numeric">${formatNumber(patient.heart_rate_bpm)}</td>
            <td data-column="spo2" class="numeric">${formatNumber(patient.spo2_percent)}</td>
            <td data-column="temp" class="numeric">${formatTemperature(patient)}</td>
            <td data-column="rr">${format_rr(patient)}</td>
            <td data-column="status">${statusBadge(patient.status)}</td>
            <td data-column="reasons" class="reasons-cell">${escapeHtml(reasons)}</td>
            <td data-column="last-seen">${patient.seconds_since_seen === null ? 'Not started' : formatDuration(patient.seconds_since_seen)}</td>
            <td data-column="workflow" class="workflow-cell">${recheck}${deescalateButton ? `<br>${deescalateButton}` : ''}</td>
            <td data-column="alert">${alertButton}</td>
        </tr>
    `;
}

function renderPatientTable(table, patients, emptyMessage) {
    if (!patients.length) {
        table.innerHTML = `<tr><td colspan="13" class="table-state">${escapeHtml(emptyMessage)}</td></tr>`;
        return;
    }
    table.innerHTML = patients.map(patientRowHtml).join('');
}

async function loadPatients({ announce = false } = {}) {
    const priorityTable = document.getElementById('priorityPatientsTable');
    const allTable = document.getElementById('patientsTable');
    const priorityState = document.getElementById('prioritySectionState');
    const allState = document.getElementById('patientsSectionState');

    priorityState.textContent = 'Refreshing…';
    allState.textContent = 'Refreshing…';

    try {
        const patients = await apiRequest('/api/patients');
        const priorityPatients = patients.filter(patient => patient.priority);
        const otherPatients = patients.filter(patient => !patient.priority);
        const activePatientIds = new Set(patients.map(patient => patient.patient_id));

        renderPatientTable(priorityTable, priorityPatients, 'No priority patients.');
        renderPatientTable(allTable, otherPatients, 'No other active patients.');
        priorityState.textContent = `${priorityPatients.length} current`;
        allState.textContent = `${otherPatients.length} current`;
        document.getElementById('activePatientCount').textContent = patients.length;
        document.getElementById('priorityPatientCount').textContent = priorityPatients.length;

        patients.forEach(patient => {
            if (patient.recheck_due) startRecheckLoop(patient.patient_id);
            else stopRecheckLoop(patient.patient_id);
            updatePatientAlertSound(patient.patient_id, patient.status);
            if (patient.status === 'GREEN') mutedPatients[patient.patient_id] = false;
            lastAlertStatus[patient.patient_id] = patient.status;
        });

        Object.keys(alertIntervals).forEach(patientId => {
            if (!activePatientIds.has(patientId)) stopAlertLoop(patientId);
        });
        Object.keys(recheckIntervals).forEach(patientId => {
            if (!activePatientIds.has(patientId)) stopRecheckLoop(patientId);
        });

        if (announce) showToast('Patient state refreshed.', 'success');
        return patients;
    } catch (error) {
        const retry = '<button class="button button-small button-secondary" type="button" onclick="loadPatients({ announce: true })">Retry</button>';
        priorityTable.innerHTML = `<tr><td colspan="13" class="table-state table-state-error">Could not load priority patients. ${retry}</td></tr>`;
        allTable.innerHTML = `<tr><td colspan="13" class="table-state table-state-error">Could not load active patients. ${retry}</td></tr>`;
        priorityState.textContent = 'Request failed';
        allState.textContent = 'Request failed';
        document.getElementById('activePatientCount').textContent = MISSING_VALUE;
        document.getElementById('priorityPatientCount').textContent = MISSING_VALUE;
        if (announce) showToast(error.message, 'error');
        throw error;
    }
}

function deviceTypeLabel(value) {
    const labels = {
        RASPBERRY_PI: 'Raspberry Pi',
        ESP32: 'ESP32',
        SIMULATOR: 'Simulator',
        UNKNOWN: 'Unknown'
    };
    return labels[value] || String(value || 'Unknown').replaceAll('_', ' ');
}

function deviceRowHtml(device) {
    // Treat only an explicit inactive lifecycle value as retired. Older server
    // responses may omit is_active; absence must never retire an active node.
    const isRetired = device.is_active === false || device.lifecycle_state === 'RETIRED';
    const assigned = device.assigned_patient_id
        ? `<span class="cell-primary">${escapeHtml(device.assigned_patient_id)}</span>`
        : `<span class="cell-secondary">${isRetired ? 'Historical only' : 'Available'}</span>`;
    const onlineLabel = isRetired ? 'INACTIVE' : (device.online ? 'ONLINE' : 'OFFLINE');
    const lastSeen = device.last_seen_age_seconds === null
        ? 'Not started'
        : formatDuration(device.last_seen_age_seconds);
    const battery = device.battery_percent === null || device.battery_percent === undefined
        ? MISSING_VALUE
        : `${formatNumber(device.battery_percent)}%`;
    const cleaningAction = !isRetired && device.status === 'NEEDS_CLEANING'
        ? `<button class="button button-small button-warning" type="button" data-clean-device="${escapeAttribute(device.device_id)}">Mark cleaned</button>`
        : '';
    const lifecycleAction = isRetired
        ? `<button class="button button-small button-secondary" type="button" data-restore-device="${escapeAttribute(device.device_id)}">Restore</button>`
        : `<button class="button button-small button-danger" type="button" data-remove-device="${escapeAttribute(device.device_id)}">${device.removal_action === 'RETIRE' ? 'Retire device' : 'Remove'}</button>`;
    const registryState = isRetired
        ? `${statusBadge('RETIRED')}<span class="cell-secondary">Previous: ${escapeHtml(device.status || MISSING_VALUE)}</span>`
        : statusBadge(device.status);
    const retiredDetail = isRetired && device.retired_at
        ? `<span class="cell-secondary">Retired ${escapeHtml(formatTimestamp(device.retired_at))}</span>`
        : `<span class="cell-secondary">${escapeHtml(device.firmware_version || MISSING_VALUE)}</span>`;
    const locationDetail = isRetired && device.retired_reason
        ? `<span class="cell-primary">${escapeHtml(device.location || MISSING_VALUE)}</span><span class="cell-secondary">Reason: ${escapeHtml(device.retired_reason)}</span>`
        : escapeHtml(device.location || MISSING_VALUE);

    return `
        <tr data-device-lifecycle="${isRetired ? 'retired' : 'active'}">
            <td><span class="cell-primary">${escapeHtml(device.device_id || MISSING_VALUE)}</span>${retiredDetail}</td>
            <td>${escapeHtml(deviceTypeLabel(device.device_type))}</td>
            <td>${registryState}</td>
            <td>${assigned}</td>
            <td>${statusBadge(onlineLabel)}</td>
            <td>${lastSeen}</td>
            <td class="numeric">${battery}</td>
            <td>${device.sensor_health ? statusBadge(device.sensor_health) : MISSING_VALUE}</td>
            <td>${locationDetail}</td>
            <td><div class="row-actions">${cleaningAction}${lifecycleAction}</div></td>
        </tr>
    `;
}

function updateDeviceSelect(devices, { preserveSelection = true } = {}) {
    const select = document.getElementById('regDevice');
    const state = document.getElementById('deviceSelectState');
    if (!select || !state) return;

    const previous = preserveSelection ? select.value : '';
    const available = devices.filter(device =>
        device.is_active !== false
        && device.lifecycle_state !== 'RETIRED'
        && device.assignable
    );
    const previousStillValid = available.some(device => device.device_id === previous);

    const options = ['<option value="">Register without a device</option>'];
    available.forEach(device => {
        const location = device.location ? ` · ${device.location}` : '';
        const age = device.last_seen_age_seconds === null
            ? ''
            : ` · ${formatDuration(device.last_seen_age_seconds)}`;
        const label = `${device.device_id} — ${deviceTypeLabel(device.device_type)} — Online${location}${age}`;
        options.push(`<option value="${escapeAttribute(device.device_id)}">${escapeHtml(label)}</option>`);
    });

    select.innerHTML = options.join('');
    select.disabled = false;
    select.value = previousStillValid ? previous : '';

    if (!available.length) {
        state.textContent = 'No online and available devices are currently available.';
    } else {
        state.textContent = `${available.length} online and available ${available.length === 1 ? 'device' : 'devices'}.`;
    }

    if (previous && !previousStillValid) {
        showToast(`Device ${previous} is no longer available; registration will continue without a device unless another is selected.`, 'warning');
    }
}

async function loadDevices({ announce = false } = {}) {
    const table = document.getElementById('devicesTable');
    const refreshButton = document.getElementById('deviceRefreshButton');
    const includeRetired = document.getElementById('showRetiredDevices')?.checked === true;
    if (refreshButton) refreshButton.disabled = true;

    try {
        const endpoint = includeRetired
            ? '/api/devices?include_retired=true'
            : '/api/devices';
        const responseDevices = await apiRequest(endpoint);
        // The backend is authoritative, but filtering again prevents stale
        // retired rows during a mixed-version rollout or cached response.
        const devices = includeRetired
            ? responseDevices
            : responseDevices.filter(device =>
                device.is_active !== false
                && device.lifecycle_state !== 'RETIRED'
            );
        lastDevices = devices;
        table.innerHTML = devices.length
            ? devices.map(deviceRowHtml).join('')
            : `<tr><td colspan="10" class="table-state">${includeRetired ? 'No active or retired devices are registered.' : 'No active devices are registered. Start a node or register a device to populate this panel.'}</td></tr>`;
        document.getElementById('onlineDeviceCount').textContent = devices.filter(device => device.is_active && device.online).length;
        document.getElementById('availableDeviceCount').textContent = devices.filter(device => device.is_active && device.assignable).length;
        updateDeviceSelect(devices);
        if (announce) showToast('Device registry refreshed.', 'success');
        return devices;
    } catch (error) {
        table.innerHTML = '<tr><td colspan="10" class="table-state table-state-error">Could not load the device registry. <button class="button button-small button-secondary" type="button" onclick="loadDevices({ announce: true })">Retry</button></td></tr>';
        document.getElementById('onlineDeviceCount').textContent = MISSING_VALUE;
        document.getElementById('availableDeviceCount').textContent = MISSING_VALUE;
        const state = document.getElementById('deviceSelectState');
        const select = document.getElementById('regDevice');
        if (state) state.textContent = 'Device registry request failed. Retry to select a device.';
        if (select) {
            select.innerHTML = '<option value="">Register without a device</option>';
            select.disabled = false;
        }
        if (announce) showToast(error.message, 'error');
        throw error;
    } finally {
        if (refreshButton) refreshButton.disabled = false;
    }
}

async function refreshAvailableDevices({ announce = false } = {}) {
    const select = document.getElementById('regDevice');
    const state = document.getElementById('deviceSelectState');
    const previous = select.value;
    select.disabled = true;
    state.textContent = 'Loading current device-registry state…';
    try {
        await loadDevices({ announce });
        if (lastDevices.some(device => device.assignable && device.device_id === previous)) {
            select.value = previous;
        }
    } catch (_) {
        // loadDevices already rendered the actionable error state.
    }
}

async function markDeviceClean(deviceId) {
    try {
        await apiRequest('/api/devices/mark_clean', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ device_id: deviceId })
        });
        showToast(`Device ${deviceId} marked cleaned.`, 'success');
        await loadDevices();
    } catch (error) {
        showToast(error.message, 'error');
    }
}

function openDeviceRemoval(deviceId) {
    const device = lastDevices.find(item => item.device_id === deviceId);
    if (!device) {
        showToast(`Device ${deviceId} is no longer in the current registry view.`, 'warning');
        void loadDevices();
        return;
    }

    pendingDeviceRemoval = device;
    const assigned = device.assigned_patient_id
        ? `${device.assigned_patient_id} (${device.assigned_encounter_id || 'active encounter'})`
        : 'Not assigned';
    document.getElementById('removeDeviceId').textContent = device.device_id;
    document.getElementById('removeDeviceType').textContent = deviceTypeLabel(device.device_type);
    document.getElementById('removeDeviceStatus').textContent = device.status || MISSING_VALUE;
    document.getElementById('removeDeviceAssignment').textContent = assigned;
    document.getElementById('deviceRemovalPrompt').textContent =
        `Remove ${device.device_id} from the active device registry? Historical encounter and packet records will be preserved.`;

    const message = document.getElementById('deviceRemovalMessage');
    const disposition = document.getElementById('deviceRemovalDisposition');
    const reason = document.getElementById('deviceRemovalReason');
    const submit = document.getElementById('deviceRemovalSubmit');
    message.hidden = true;
    reason.value = '';
    submit.disabled = false;

    if (device.removal_action === 'BLOCKED') {
        message.textContent = 'This device is assigned to an active encounter. Release it before removal.';
        message.hidden = false;
        disposition.textContent = 'Removal and retirement are blocked while the assignment is active.';
        reason.disabled = true;
        submit.disabled = true;
    } else if (device.removal_action === 'DELETE') {
        disposition.textContent = 'This unused device has no historical dependencies and will be permanently deleted.';
        reason.disabled = true;
    } else {
        disposition.textContent = 'This device has historical dependencies and will be retired, not deleted.';
        reason.disabled = false;
    }

    const dialog = document.getElementById('deviceRemovalDialog');
    if (!dialog.open) dialog.showModal();
}

async function removePendingDevice() {
    if (!pendingDeviceRemoval) return;
    const submit = document.getElementById('deviceRemovalSubmit');
    const reason = document.getElementById('deviceRemovalReason').value.trim();
    const query = reason ? `?reason=${encodeURIComponent(reason)}` : '';
    submit.disabled = true;

    try {
        const result = await apiRequest(
            `/api/devices/${encodeURIComponent(pendingDeviceRemoval.device_id)}${query}`,
            { method: 'DELETE' }
        );
        showToast(result.message, 'success');
        closeDialog('deviceRemovalDialog');
        pendingDeviceRemoval = null;
        await loadDevices();
    } catch (error) {
        const message = document.getElementById('deviceRemovalMessage');
        message.textContent = error.message;
        message.hidden = false;
        submit.disabled = false;
    }
}

async function restoreDevice(deviceId) {
    try {
        const result = await apiRequest(`/api/devices/${encodeURIComponent(deviceId)}/restore`, {
            method: 'POST'
        });
        showToast(result.message || `Device ${deviceId} restored.`, 'success');
        await loadDevices();
    } catch (error) {
        showToast(error.message, 'error');
    }
}

function selectPatient(patientId) {
    selectedPatient = patientId;
    document.getElementById('selectedPatient').textContent = patientId;
    document.getElementById('patientDetailsEmpty').hidden = true;
    document.getElementById('patientDetailsContent').hidden = false;
    document.querySelectorAll('tr[data-patient-id]').forEach(row => {
        row.dataset.selected = String(row.dataset.patientId === patientId);
    });
    void Promise.allSettled([loadPatientDetails(), loadPatientEvents()]);
}

function updateChart(history) {
    const state = document.getElementById('chartState');
    const canvas = document.getElementById('patientChart');
    const recent = history.slice(-20);

    if (!recent.length) {
        state.textContent = 'No packet history for this encounter.';
        canvas.hidden = true;
        return;
    }
    if (typeof Chart === 'undefined') {
        state.textContent = 'Chart library unavailable; tabular history remains available.';
        canvas.hidden = true;
        return;
    }

    state.textContent = `Showing the latest ${recent.length} accepted measurements.`;
    canvas.hidden = false;
    const data = {
        labels: recent.map(packet => formatTimeOnly(packet.timestamp)),
        datasets: [
            {
                label: 'HR bpm',
                data: recent.map(packet => packet.heart_rate_bpm),
                borderColor: '#176b75',
                backgroundColor: '#176b75',
                tension: 0.2
            },
            {
                label: 'SpO₂ %',
                data: recent.map(packet => packet.spo2_percent),
                borderColor: '#247447',
                backgroundColor: '#247447',
                tension: 0.2
            },
            {
                label: 'Temp °C',
                data: recent.map(packet => packet.object_temp_c),
                borderColor: '#a94f1c',
                backgroundColor: '#a94f1c',
                tension: 0.2
            }
        ]
    };

    if (!chart) {
        chart = new Chart(canvas, {
            type: 'line',
            data,
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: false,
                interaction: { mode: 'index', intersect: false },
                plugins: { legend: { position: 'bottom' } }
            }
        });
    } else {
        chart.data = data;
        chart.update();
    }
}

async function loadPatientDetails() {
    if (!selectedPatient) return;
    const detailTable = document.getElementById('detailHistory');

    try {
        const [history, patients] = await Promise.all([
            apiRequest('/api/history'),
            apiRequest('/api/patients')
        ]);
        const currentPatient = patients.find(patient => patient.patient_id === selectedPatient);
        const patientHistory = history.filter(packet => packet.patient_id === selectedPatient);
        const latestPacket = currentPatient || patientHistory.at(-1) || null;
        renderResearchRr(latestPacket);
        updateChart(patientHistory);

        if (!patientHistory.length) {
            detailTable.innerHTML = '<tr><td colspan="6" class="table-state">No packet history for this encounter.</td></tr>';
            return;
        }

        detailTable.innerHTML = patientHistory.slice(-10).reverse().map(packet => `
            <tr>
                <td data-column="timestamp">${formatTimestamp(packet.timestamp)}</td>
                <td data-column="hr" class="numeric">${formatNumber(packet.heart_rate_bpm)}</td>
                <td data-column="spo2" class="numeric">${formatNumber(packet.spo2_percent)}</td>
                <td data-column="temp" class="numeric">${formatTemperature(packet)}</td>
                <td data-column="rr">${format_rr(packet)}</td>
                <td data-column="status">${statusBadge(packet.status)}</td>
            </tr>
        `).join('');
    } catch (error) {
        detailTable.innerHTML = '<tr><td colspan="6" class="table-state table-state-error">Measurement history request failed. <button class="button button-small button-secondary" type="button" onclick="loadPatientDetails()">Retry</button></td></tr>';
    }
}

async function loadPatientEvents() {
    if (!selectedPatient) return;
    const timeline = document.getElementById('eventTimeline');
    try {
        const events = await apiRequest(`/api/events/${encodeURIComponent(selectedPatient)}`);
        timeline.innerHTML = events.length
            ? events.slice(-15).reverse().map(event => `
                <tr>
                    <td>${formatTimestamp(event.time)}</td>
                    <td>${escapeHtml(event.event_type || MISSING_VALUE)}</td>
                    <td>${escapeHtml(event.note || MISSING_VALUE)}</td>
                </tr>
            `).join('')
            : '<tr><td colspan="3" class="table-state">No workflow events recorded for this encounter.</td></tr>';
    } catch (_) {
        timeline.innerHTML = '<tr><td colspan="3" class="table-state table-state-error">Timeline request failed. <button class="button button-small button-secondary" type="button" onclick="loadPatientEvents()">Retry</button></td></tr>';
    }
}

function openRegistrationDialog() {
    const dialog = document.getElementById('registrationDialog');
    setRegistrationMessage('');
    if (!dialog.open) dialog.showModal();
    document.getElementById('regPatientId').focus();
    void refreshAvailableDevices();
}

function closeDialog(dialogId) {
    const dialog = document.getElementById(dialogId);
    if (dialog?.open) dialog.close();
}

function setRegistrationMessage(message) {
    const element = document.getElementById('registrationMessage');
    element.textContent = message;
    element.hidden = !message;
}

async function registerPatient() {
    const patientId = document.getElementById('regPatientId').value.trim();
    if (!patientId) {
        setRegistrationMessage('Patient ID is required.');
        document.getElementById('regPatientId').focus();
        return;
    }

    const submit = document.getElementById('registrationSubmit');
    submit.disabled = true;
    submit.textContent = 'Registering…';
    setRegistrationMessage('');

    const profile = {
        patient_id: patientId,
        display_name: document.getElementById('regName').value.trim(),
        age: document.getElementById('regAge').value.trim(),
        chief_complaint: document.getElementById('regComplaint').value.trim(),
        triage_level: document.getElementById('regTriage').value,
        location: document.getElementById('regLocation').value.trim(),
        assigned_device_id: document.getElementById('regDevice').value
    };

    try {
        const result = await apiRequest('/api/register_patient', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(profile)
        });
        document.getElementById('registrationForm').reset();
        document.getElementById('regTriage').value = 'Unknown';
        closeDialog('registrationDialog');
        showToast(`Patient ${patientId} registered for encounter ${result.encounter_id}.`, 'success');
        await Promise.allSettled([loadPatients(), loadDevices()]);
    } catch (error) {
        setRegistrationMessage(error.message);
        if (error.payload?.field === 'assigned_device_id') {
            await refreshAvailableDevices();
            document.getElementById('regDevice').focus();
        }
    } finally {
        submit.disabled = false;
        submit.textContent = 'Register patient';
    }
}

function openNoteBox(actionType) {
    if (!selectedPatient) {
        showToast('Select a patient first.', 'warning');
        return;
    }

    pendingActionType = actionType;
    document.getElementById('noteBoxTitle').textContent = actionType;
    document.getElementById('noteBoxPatient').textContent = selectedPatient;
    document.getElementById('noteText').value = '';
    const dialog = document.getElementById('noteBox');
    if (!dialog.open) dialog.showModal();
    document.getElementById('noteText').focus();
}

async function postNote() {
    if (!selectedPatient || !pendingActionType) return;
    const submit = document.getElementById('noteSubmit');
    const completedActionType = pendingActionType;
    const actionPatientId = selectedPatient;
    submit.disabled = true;
    submit.textContent = 'Saving…';

    try {
        await apiRequest('/api/action', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                patient_id: actionPatientId,
                action_type: completedActionType,
                note: document.getElementById('noteText').value.trim()
            })
        });
        pendingActionType = null;
        closeDialog('noteBox');
        showToast(`${completedActionType} recorded for ${actionPatientId}.`, 'success');
        await Promise.allSettled([loadPatients(), loadDevices(), loadPatientEvents()]);

        if (completedActionType === 'Dismissed to monitoring room') {
            pendingHandoffPatientId = actionPatientId;
            const dialog = document.getElementById('handoffChoiceBox');
            if (!dialog.open) dialog.showModal();
        }
    } catch (error) {
        showToast(error.message, 'error');
    } finally {
        submit.disabled = false;
        submit.textContent = 'Save action';
    }
}

function confirmHandoffYes() {
    const patientId = pendingHandoffPatientId;
    pendingHandoffPatientId = null;
    closeDialog('handoffChoiceBox');
    void generateHandoff(patientId);
}

function confirmHandoffNo() {
    pendingHandoffPatientId = null;
    closeDialog('handoffChoiceBox');
}

async function generateHandoff(patientId = selectedPatient) {
    if (!patientId) {
        showToast('Select a patient first.', 'warning');
        return;
    }

    const summary = document.getElementById('handoffSummary');
    summary.textContent = 'Generating handoff summary…';
    try {
        const data = await apiRequest(`/api/handoff/${encodeURIComponent(patientId)}`);
        summary.textContent = data.summary;
        showToast(`Handoff generated for ${patientId}.`, 'success');
    } catch (error) {
        summary.textContent = `Handoff request failed: ${error.message}`;
    }
}

const ALARM_PATTERNS = {
    ORANGE: {
        repeatMs: 3500,
        tones: [
            { freq: 740, duration: 0.09, volume: 0.07, delay: 0.00, wave: 'sine' },
            { freq: 740, duration: 0.09, volume: 0.07, delay: 0.18, wave: 'sine' },
            { freq: 740, duration: 0.09, volume: 0.07, delay: 0.36, wave: 'sine' }
        ]
    },
    RED: {
        repeatMs: 1800,
        tones: [
            { freq: 980, duration: 0.08, volume: 0.12, delay: 0.00, wave: 'triangle' },
            { freq: 980, duration: 0.08, volume: 0.12, delay: 0.13, wave: 'triangle' },
            { freq: 980, duration: 0.08, volume: 0.12, delay: 0.26, wave: 'triangle' },
            { freq: 760, duration: 0.10, volume: 0.13, delay: 0.42, wave: 'triangle' },
            { freq: 760, duration: 0.10, volume: 0.13, delay: 0.57, wave: 'triangle' }
        ]
    },
    CRASHING: {
        repeatMs: 950,
        tones: [
            { freq: 1100, duration: 0.07, volume: 0.16, delay: 0.00, wave: 'square' },
            { freq: 900, duration: 0.07, volume: 0.16, delay: 0.11, wave: 'square' },
            { freq: 1100, duration: 0.07, volume: 0.16, delay: 0.22, wave: 'square' },
            { freq: 900, duration: 0.07, volume: 0.16, delay: 0.33, wave: 'square' },
            { freq: 700, duration: 0.12, volume: 0.18, delay: 0.48, wave: 'square' }
        ]
    }
};

async function enableSound() {
    try {
        if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        await audioCtx.resume();
        audioEnabled = true;
        const button = document.getElementById('soundButton');
        button.textContent = 'Alert sound enabled';
        button.disabled = true;
        showToast('Alert sound enabled for this browser session.', 'success');
        await loadPatients();
    } catch (_) {
        showToast('Alert sound could not be enabled in this browser.', 'error');
    }
}

function playTone(frequency, duration, gainValue, startDelay = 0, waveType = 'sine') {
    if (!audioEnabled || !audioCtx) return;
    const oscillator = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    oscillator.type = waveType;
    oscillator.frequency.value = frequency;
    oscillator.connect(gain);
    gain.connect(audioCtx.destination);
    const startTime = audioCtx.currentTime + startDelay;
    const stopTime = startTime + duration;
    gain.gain.setValueAtTime(gainValue, startTime);
    gain.gain.exponentialRampToValueAtTime(0.001, stopTime);
    oscillator.start(startTime);
    oscillator.stop(stopTime);
}

function playBeep(type) {
    if (!audioEnabled || !audioCtx) return;
    const pattern = ALARM_PATTERNS[type];
    if (!pattern) return;
    if (audioCtx.state === 'suspended') audioCtx.resume().catch(() => {});
    pattern.tones.forEach(tone => {
        playTone(tone.freq, tone.duration, tone.volume, tone.delay, tone.wave);
    });
}

function isAlertStatus(status) {
    return status === 'ORANGE' || status === 'RED' || status === 'CRASHING';
}

function startAlertLoop(patientId, status) {
    if (!audioEnabled || !audioCtx || mutedPatients[patientId]) return;
    if (!ALARM_PATTERNS[status]) return;
    if (alertIntervals[patientId]?.beepType === status) return;
    stopAlertLoop(patientId);
    alertIntervals[patientId] = { beepType: status, timer: null };

    const loop = () => {
        const current = alertIntervals[patientId];
        if (!current || mutedPatients[patientId]) {
            stopAlertLoop(patientId);
            return;
        }
        playBeep(current.beepType);
        current.timer = window.setTimeout(loop, ALARM_PATTERNS[current.beepType].repeatMs);
    };
    loop();
}

function stopAlertLoop(patientId) {
    const existing = alertIntervals[patientId];
    if (!existing) return;
    if (existing.timer !== null) window.clearTimeout(existing.timer);
    delete alertIntervals[patientId];
}

function updatePatientAlertSound(patientId, status) {
    if (isAlertStatus(status)) startAlertLoop(patientId, status);
    else stopAlertLoop(patientId);
}

function mutePatient(patientId) {
    mutedPatients[patientId] = true;
    stopAlertLoop(patientId);
    showToast(`Alert sound muted for ${patientId} until the state returns to GREEN.`, 'warning');
    void loadPatients();
}

function startRecheckLoop(patientId) {
    if (!audioEnabled || !audioCtx || recheckIntervals[patientId]) return;
    playBeep('ORANGE');
    recheckIntervals[patientId] = window.setInterval(() => playBeep('ORANGE'), 4000);
}

function stopRecheckLoop(patientId) {
    if (!recheckIntervals[patientId]) return;
    window.clearInterval(recheckIntervals[patientId]);
    delete recheckIntervals[patientId];
}

async function refreshDashboard() {
    if (pollInFlight) return;
    pollInFlight = true;
    try {
        const tasks = [loadPatients(), loadDevices()];
        if (selectedPatient) tasks.push(loadPatientDetails(), loadPatientEvents());
        await Promise.allSettled(tasks);
    } finally {
        pollInFlight = false;
    }
}

function handleTableInteraction(event) {
    const cleanButton = event.target.closest('[data-clean-device]');
    if (cleanButton) {
        void markDeviceClean(cleanButton.dataset.cleanDevice);
        return;
    }

    const removeButton = event.target.closest('[data-remove-device]');
    if (removeButton) {
        openDeviceRemoval(removeButton.dataset.removeDevice);
        return;
    }

    const restoreButton = event.target.closest('[data-restore-device]');
    if (restoreButton) {
        void restoreDevice(restoreButton.dataset.restoreDevice);
        return;
    }

    const muteButton = event.target.closest('[data-mute-patient]');
    if (muteButton) {
        mutePatient(muteButton.dataset.mutePatient);
        return;
    }

    const deescalateButton = event.target.closest('[data-deescalate-patient]');
    if (deescalateButton) {
        selectedPatient = deescalateButton.dataset.deescalatePatient;
        openNoteBox('De-escalated / dismissed from priority');
        return;
    }

    const row = event.target.closest('tr[data-patient-id]');
    if (row) selectPatient(row.dataset.patientId);
}

function initializeDashboard() {
    document.addEventListener('click', handleTableInteraction);
    document.addEventListener('keydown', event => {
        const row = event.target.closest('tr[data-patient-id]');
        if (row && (event.key === 'Enter' || event.key === ' ')) {
            event.preventDefault();
            selectPatient(row.dataset.patientId);
        }
    });

    document.getElementById('registrationForm').addEventListener('submit', event => {
        event.preventDefault();
        void registerPatient();
    });
    document.getElementById('noteForm').addEventListener('submit', event => {
        event.preventDefault();
        void postNote();
    });
    document.getElementById('deviceRemovalForm').addEventListener('submit', event => {
        event.preventDefault();
        void removePendingDevice();
    });
    document.getElementById('showRetiredDevices').addEventListener('change', () => {
        void loadDevices();
    });

    setServerState('loading', 'Connecting');
    void refreshDashboard();
    window.setInterval(refreshDashboard, POLL_INTERVAL_MS);
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initializeDashboard);
} else {
    initializeDashboard();
}
