let lastAlertStatus = {};
let alertIntervals = {};
let mutedPatients = {};
let recheckIntervals = {};

let audioEnabled = false;
let audioCtx = null;

function enableSound() {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    audioCtx.resume();
    audioEnabled = true;
    alert("Alert sound enabled");
}

function playTone(frequency, duration, gainValue, startDelay = 0, waveType = "sine") {
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

// Keep these globals as you already have them:
// audioEnabled, audioCtx, mutedPatients, alertIntervals

const ALARM_PATTERNS = {
    ORANGE: {
        // Medium priority: slower, softer 3-beep pattern
        repeatMs: 3500,
        tones: [
            { freq: 740, duration: 0.09, volume: 0.07, delay: 0.00, wave: "sine" },
            { freq: 740, duration: 0.09, volume: 0.07, delay: 0.18, wave: "sine" },
            { freq: 740, duration: 0.09, volume: 0.07, delay: 0.36, wave: "sine" }
        ]
    },

    RED: {
        // High priority: sharper 5-beep monitor-style burst
        repeatMs: 1800,
        tones: [
            { freq: 980, duration: 0.08, volume: 0.12, delay: 0.00, wave: "triangle" },
            { freq: 980, duration: 0.08, volume: 0.12, delay: 0.13, wave: "triangle" },
            { freq: 980, duration: 0.08, volume: 0.12, delay: 0.26, wave: "triangle" },
            { freq: 760, duration: 0.10, volume: 0.13, delay: 0.42, wave: "triangle" },
            { freq: 760, duration: 0.10, volume: 0.13, delay: 0.57, wave: "triangle" }
        ]
    },

    CRASHING: {
        // Very urgent: faster, harsher repeated high-priority pattern
        repeatMs: 950,
        tones: [
            { freq: 1100, duration: 0.07, volume: 0.16, delay: 0.00, wave: "square" },
            { freq: 900,  duration: 0.07, volume: 0.16, delay: 0.11, wave: "square" },
            { freq: 1100, duration: 0.07, volume: 0.16, delay: 0.22, wave: "square" },
            { freq: 900,  duration: 0.07, volume: 0.16, delay: 0.33, wave: "square" },
            { freq: 700,  duration: 0.12, volume: 0.18, delay: 0.48, wave: "square" }
        ]
    }
};

function playBeep(type) {
    if (!audioEnabled || !audioCtx) return;

    const pattern = ALARM_PATTERNS[type];
    if (!pattern) return;

    if (audioCtx.state === "suspended") {
        audioCtx.resume().catch(() => {});
    }

    pattern.tones.forEach(tone => {
        playTone(
            tone.freq,
            tone.duration,
            tone.volume,
            tone.delay,
            tone.wave
        );
    });
}

function isAlertStatus(status) {
    return status === "ORANGE" || status === "RED" || status === "CRASHING";
}

function getBeepType(status) {
    if (status === "ORANGE") return "ORANGE";
    if (status === "RED") return "RED";
    if (status === "CRASHING") return "CRASHING";
    return null;
}

function startAlertLoop(patientId, status) {
    if (!audioEnabled || !audioCtx) return;
    if (mutedPatients[patientId]) return;

    const beepType = getBeepType(status);
    if (!beepType) return;

    // If same alarm is already running, do nothing
    if (
        alertIntervals[patientId] &&
        alertIntervals[patientId].beepType === beepType
    ) {
        return;
    }

    // If status changed, stop the old alarm first
    stopAlertLoop(patientId);

    alertIntervals[patientId] = {
        beepType,
        timer: null
    };

    const loop = () => {
        if (!audioEnabled || !audioCtx || mutedPatients[patientId]) {
            stopAlertLoop(patientId);
            return;
        }

        const current = alertIntervals[patientId];
        if (!current) return;

        const pattern = ALARM_PATTERNS[current.beepType];
        if (!pattern) return;

        playBeep(current.beepType);

        current.timer = setTimeout(loop, pattern.repeatMs);
    };

    // Start immediately
    loop();
}

function stopAlertLoop(patientId) {
    const existing = alertIntervals[patientId];
    if (!existing) return;

    if (existing.timer !== null) {
        clearTimeout(existing.timer);
    }

    delete alertIntervals[patientId];
}

function updatePatientAlertSound(patientId, status) {
    if (isAlertStatus(status)) {
        startAlertLoop(patientId, status);
    } else {
        stopAlertLoop(patientId);
    }
}

function mutePatient(patientId) {
    mutedPatients[patientId] = true;
    stopAlertLoop(patientId);
}

function formatDuration(seconds) {
    if (seconds < 60) return `${seconds}s ago`;

    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m ago`;

    const hours = Math.floor(minutes / 60);
    const remainingMinutes = minutes % 60;
    return `${hours}h ${remainingMinutes}m ago`;
}

let selectedPatient = null;
let chart = null;

const RELIABLE_RR_QUALITIES = new Set(['GOOD', 'MODERATE']);

function escapeHtml(value) {
    return String(value)
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#039;');
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
    if (value === null || value === undefined || value === '') return '--';
    const numericValue = Number(value);
    return Number.isFinite(numericValue) ? numericValue.toFixed(2) : '--';
}

function format_rr(packet) {
    if (!hasRrMetadata(packet)) return '--';

    const quality = normalizeRrQuality(packet.rr_quality);
    const qualityClass = rrQualityClass(quality);
    const source = escapeHtml(packet.rr_source || 'UNAVAILABLE');
    const confidence = formatRrConfidence(packet.rr_confidence);
    const hasRate = packet.resp_rate_bpm !== null
        && packet.resp_rate_bpm !== undefined;
    const rate = hasRate && Number.isFinite(Number(packet.resp_rate_bpm))
        ? Number(packet.resp_rate_bpm).toFixed(1)
        : null;
    const valueText = rate === null
        ? `withheld (${escapeHtml(quality)}, ${confidence})`
        : `${rate} bpm (${escapeHtml(quality)}, ${confidence})`;

    return `
        <span class="rr-inline ${qualityClass}">${valueText}</span>
        <span class="rr-source">${source}</span>
    `;
}

function formatTemperature(packet) {
    return packet.object_temp_c
        ?? packet.temperature_c
        ?? packet.temperature
        ?? '--';
}

function renderResearchRr(packet) {
    const detail = document.getElementById('researchRrDetail');

    if (!hasRrMetadata(packet)) {
        detail.className = 'rr-detail rr-unavailable';
        detail.innerHTML = 'No PPG-derived RR metadata reported.';
        return;
    }

    const quality = normalizeRrQuality(packet.rr_quality);
    const qualityClass = rrQualityClass(quality);
    const source = escapeHtml(packet.rr_source || 'UNAVAILABLE');
    const confidence = formatRrConfidence(packet.rr_confidence);
    const hasRate = packet.resp_rate_bpm !== null
        && packet.resp_rate_bpm !== undefined
        && Number.isFinite(Number(packet.resp_rate_bpm));
    const reliable = hasRate && RELIABLE_RR_QUALITIES.has(quality);
    const valueText = hasRate
        ? `${Number(packet.resp_rate_bpm).toFixed(1)} breaths/min`
        : 'Pending / withheld';
    const caution = reliable
        ? quality === 'MODERATE'
            ? 'Acceptable research estimate; interpret with caution.'
            : 'Acceptable research estimate.'
        : 'Withheld or unreliable research estimate.';

    detail.className = `rr-detail ${qualityClass}`;
    detail.innerHTML = `
        <div><b>PPG-derived RR:</b> ${valueText}</div>
        <div><b>Quality:</b>
            <span class="rr-quality ${qualityClass}">${escapeHtml(quality)}</span>
        </div>
        <div><b>Confidence:</b> ${confidence}</div>
        <div><b>Source:</b> ${source}</div>
        <div class="rr-disclaimer">${caution} Not used for priority status.</div>
    `;
}

async function loadPatients() {
    const patients = await fetch('/api/patients').then(r => r.json());
    const activePatientIds = new Set(
        patients.map(patient => patient.patient_id)
    );

    const priorityTable = document.getElementById('priorityPatientsTable');
    const allTable = document.getElementById('patientsTable');

    priorityTable.innerHTML = '';
    allTable.innerHTML = '';

    patients.forEach(p => {
        if (p.recheck_due) {
            startRecheckLoop(p.patient_id);
        } else {
            stopRecheckLoop(p.patient_id);
        }

        const rowHtml = `
            <tr onclick="selectPatient('${p.patient_id}')" style="cursor:pointer">
                <td data-column="patient">${p.patient_id || '--'}</td>
                <td data-column="complaint">${p.chief_complaint || '--'}</td>
                <td data-column="triage">${p.triage_level || '--'}</td>
                <td data-column="location">${p.location || '--'}</td>
                <td data-column="hr">${p.heart_rate_bpm ?? '--'}</td>
                <td data-column="spo2">${p.spo2_percent ?? '--'}</td>
                <td data-column="temp">${formatTemperature(p)}</td>
                <td data-column="rr">${format_rr(p)}</td>
                <td data-column="status" class="${p.status || ''}">${p.status || '--'}</td>
                <td data-column="reasons">${(p.reasons || []).join(', ')}</td>
                <td data-column="last-seen">${
                    p.seconds_since_seen === null
                    ? 'Not started'
                    : formatDuration(p.seconds_since_seen || 0)
                }</td>

                <td data-column="workflow">
                    ${
                        p.recheck_due
                        ? `<span class="RECHECK_BADGE">RECHECK</span>`
                        : p.recheck_seconds_left !== null
                            ? `Recheck in ${formatCountdown(p.recheck_seconds_left)}`
                            : ''
                    }

                    ${
                        p.priority
                        ? `<br><button onclick="event.stopPropagation(); selectedPatient='${p.patient_id}'; openNoteBox('De-escalated / dismissed from priority')">
                                De-escalate
                        </button>`
                        : ''
                    }
                </td>

                <td data-column="alert">
                    ${
                        isAlertStatus(p.status)
                        ? `<button onclick="event.stopPropagation(); mutePatient('${p.patient_id}')">Mute</button>`
                        : ''
                    }
                </td>
            </tr>
        `;

        if (p.priority) {
            priorityTable.innerHTML += rowHtml;
        } else {
            allTable.innerHTML += rowHtml;
        }

        if (isAlertStatus(p.status)) {
            startAlertLoop(p.patient_id, p.status);
        } else {
            stopAlertLoop(p.patient_id);

            if (p.status === "GREEN") {
                mutedPatients[p.patient_id] = false;
            }
        }
    });

    Object.keys(alertIntervals).forEach(patientId => {
        if (!activePatientIds.has(patientId)) {
            stopAlertLoop(patientId);
        }
    });

    Object.keys(recheckIntervals).forEach(patientId => {
        if (!activePatientIds.has(patientId)) {
            stopRecheckLoop(patientId);
        }
    });
}

async function loadDevices() {
    const devices = await fetch('/api/devices').then(r => r.json());
    const devicesTable = document.getElementById('devicesTable');
    devicesTable.innerHTML = '';

    devices.forEach(device => {
        let lastSeen = 'Not started';

        if (device.last_seen) {
            const lastSeenDate = new Date(device.last_seen);
            if (!Number.isNaN(lastSeenDate.getTime())) {
                const seconds = Math.max(
                    0,
                    Math.floor((Date.now() - lastSeenDate.getTime()) / 1000)
                );
                lastSeen = formatDuration(seconds);
            }
        }

        devicesTable.innerHTML += `
            <tr>
                <td>${device.device_id || '--'}</td>
                <td>${device.device_type || 'UNKNOWN'}</td>
                <td>${device.assigned_patient_id || '--'}</td>
                <td class="${device.status || ''}">${device.status || '--'}</td>
                <td>${
                    device.battery_percent === null
                    ? '--'
                    : `${device.battery_percent}%`
                }</td>
                <td>${device.wifi_rssi ?? '--'}</td>
                <td>${lastSeen}</td>
                <td>${device.firmware_version || '--'}</td>
            </tr>
        `;
    });
}

loadPatients();
loadDevices();

function selectPatient(patientId) {
    selectedPatient = patientId;
    loadPatientDetails();
    loadPatientEvents();
}

async function loadPatientDetails() {

    if (!selectedPatient) return;

    document.getElementById(
        'selectedPatient'
    ).innerText = selectedPatient;

    const [history, patients] = await Promise.all([
        fetch('/api/history').then(r => r.json()),
        fetch('/api/patients').then(r => r.json())
    ]);

    const currentPatient = patients.find(
        patient => patient.patient_id === selectedPatient
    );
    renderResearchRr(currentPatient);

    const patientHistory =
        history.filter(
            p => p.patient_id === selectedPatient
        );

    const recent =
        patientHistory.slice(-20);

    const labels =
        recent.map(
            p => p.timestamp.split(' ')[1]
        );

    const hrData =
        recent.map(p => p.heart_rate_bpm);

    const spo2Data =
        recent.map(p => p.spo2_percent);

    const tempData =
        recent.map(p => p.object_temp_c);

    if (!chart) {

        const ctx =
            document.getElementById(
                'patientChart'
            );

        chart = new Chart(ctx, {

            type: 'line',

            data: {

                labels: labels,

                datasets: [

                    {
                        label: 'HR bpm',
                        data: hrData,
                        tension: 0.2
                    },

                    {
                        label: 'SpO₂ %',
                        data: spo2Data,
                        tension: 0.2
                    },

                    {
                        label: 'Temp °C',
                        data: tempData,
                        tension: 0.2
                    }

                ]
            },

            options: {
                responsive: true,
                animation: false
            }
        });

    } else {

        chart.data.labels = labels;

        chart.data.datasets[0].data = hrData;
        chart.data.datasets[1].data = spo2Data;
        chart.data.datasets[2].data = tempData;

        chart.update();
    }

    const detailTable =
        document.getElementById(
            'detailHistory'
        );

    detailTable.innerHTML = '';

    patientHistory
        .slice(-10)
        .reverse()
        .forEach(p => {

        detailTable.innerHTML += `
        <tr>

            <td data-column="timestamp">${p.timestamp || '--'}</td>

            <td data-column="hr">${p.heart_rate_bpm ?? '--'}</td>

            <td data-column="spo2">${p.spo2_percent ?? '--'}</td>

            <td data-column="temp">${formatTemperature(p)}</td>

            <td data-column="rr">${format_rr(p)}</td>

            <td data-column="status" class="${p.status || ''}">
                ${p.status || '--'}
            </td>

        </tr>
        `;
    });
}

setInterval(() => {
    loadPatients();
    loadDevices();
    loadPatientDetails();
    loadPatientEvents();
}, 3000);

let pendingActionType = null;
let pendingHandoffPatientId = null;

function openNoteBox(actionType) {
    if (!selectedPatient) {
        alert("Select a patient first");
        return;
    }

    pendingActionType = actionType;

    document.getElementById('noteBoxTitle').innerText =
        `Post note for: ${actionType}`;

    document.getElementById('noteBoxPatient').innerText =
        selectedPatient;

    document.getElementById('noteText').value = '';

    document.getElementById('noteBox').style.display = 'block';

    document.getElementById('noteText').focus();
}

async function postNote() {
    if (!selectedPatient || !pendingActionType) {
        return;
    }

    const note = document.getElementById('noteText').value;
    const completedActionType = pendingActionType;
    const actionPatientId = selectedPatient;

    const response = await fetch('/api/action', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({
            patient_id: actionPatientId,
            action_type: completedActionType,
            note: note
        })
    });

    const result = await response.json();

    if (!response.ok || !result.ok) {
        alert(result.error || "Action could not be completed");
        return;
    }

    document.getElementById('noteBox').style.display = 'none';
    document.getElementById('noteText').value = '';

    pendingActionType = null;

    loadPatients();
    loadPatientEvents();

    if (completedActionType === "Dismissed to monitoring room") {
        pendingHandoffPatientId = actionPatientId;
        document.getElementById('handoffChoiceBox').style.display = 'block';
    }
}

function confirmHandoffYes() {
    const patientId = pendingHandoffPatientId;
    pendingHandoffPatientId = null;
    document.getElementById('handoffChoiceBox').style.display = 'none';
    generateHandoff(patientId);
}

function confirmHandoffNo() {
    pendingHandoffPatientId = null;
    document.getElementById('handoffChoiceBox').style.display = 'none';
}

async function loadPatientEvents() {
    if (!selectedPatient) return;

    const events = await fetch(`/api/events/${selectedPatient}`)
        .then(r => r.json());

    const timeline = document.getElementById('eventTimeline');
    timeline.innerHTML = '';

    events.slice(-15).reverse().forEach(e => {
        timeline.innerHTML += `
            <tr>
                <td>${e.time}</td>
                <td>${e.event_type}</td>
                <td>${e.note || ''}</td>
            </tr>
        `;
    });
}

function formatCountdown(seconds) {
    seconds = Number(seconds || 0);

    if (seconds < 60) return `${seconds}s`;

    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m`;

    const hours = Math.floor(minutes / 60);
    const remainingMinutes = minutes % 60;

    return `${hours}h ${remainingMinutes}m`;
}

function startRecheckLoop(patientId) {
    if (!audioEnabled || !audioCtx) return;

    if (recheckIntervals[patientId]) return;

    playBeep("ORANGE");

    recheckIntervals[patientId] = setInterval(() => {
        playBeep("ORANGE");
    }, 4000);
}

function stopRecheckLoop(patientId) {
    if (recheckIntervals[patientId]) {
        clearInterval(recheckIntervals[patientId]);
        delete recheckIntervals[patientId];
    }
}

async function generateHandoff(patientId = selectedPatient) {
    if (!patientId) {
        alert("Select a patient first");
        return;
    }

    const data = await fetch(`/api/handoff/${patientId}`)
        .then(r => r.json());

    if (!data.ok) {
        document.getElementById('handoffSummary').innerText =
            data.error || "Could not generate handoff.";
        return;
    }

    document.getElementById('handoffSummary').innerText =
        data.summary;
}

async function registerPatient() {
    const patientId = document.getElementById('regPatientId').value.trim();

    if (!patientId) {
        alert("Patient ID is required");
        return;
    }

    const profile = {
        patient_id: patientId,
        display_name: document.getElementById('regName').value.trim(),
        age: document.getElementById('regAge').value.trim(),
        chief_complaint: document.getElementById('regComplaint').value.trim(),
        triage_level: document.getElementById('regTriage').value,
        location: document.getElementById('regLocation').value.trim(),
        assigned_device_id: document.getElementById('regDevice').value.trim(),
    };

    const result = await fetch('/api/register_patient', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify(profile)
    }).then(r => r.json());

    if (!result.ok) {
        alert(result.error || "Registration failed");
        return;
    }

    document.getElementById('regPatientId').value = '';
    document.getElementById('regName').value = '';
    document.getElementById('regAge').value = '';
    document.getElementById('regComplaint').value = '';
    document.getElementById('regTriage').value = '';
    document.getElementById('regLocation').value = '';
    document.getElementById('regDevice').value = '';

    loadPatients();
    loadDevices();
}
