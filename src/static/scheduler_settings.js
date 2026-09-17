(function () {
    "use strict";

    const state = { config: null, instances: [], status: null, draft: null, editIndex: null, lastError: "", timezone: "UTC" };
    const days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
    const dayLabels = { mon: "Mon", tue: "Tue", wed: "Wed", thu: "Thu", fri: "Fri", sat: "Sat", sun: "Sun" };
    const returnLabels = {
        resume_previous: "Return to previous display",
        resume_playlist: "Resume normal playlist",
        stay_until_next_cycle: "Stay until next playlist cycle"
    };
    const operatorLabels = { eq: "=", ne: "≠", gt: ">", gte: "≥", lt: "<", lte: "≤", in: "is in", not_in: "is not in", contains: "contains", exists: "exists", truthy: "is truthy", falsy: "is falsy" };

    function el(id) { return document.getElementById(id); }
    function escapeHtml(value) {
        return String(value == null ? "" : value).replace(/[&<>'"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[c]));
    }
    function clone(value) { return JSON.parse(JSON.stringify(value)); }
    function instance(id) { return state.instances.find(item => item.instance_id === id); }
    function ruleName(rule) { return rule.name || rule.id || "Untitled rule"; }
    function targetName(rule) {
        const found = instance(rule.target && rule.target.instance_id);
        return found ? `${found.instance_name} — ${found.plugin_name}` : ((rule.target && (rule.target.plugin_instance || rule.target.instance_id)) || "Unavailable display");
    }
    function schemaFor(id, path) {
        const found = instance(id);
        return found && found.schema ? found.schema[path] : null;
    }
    function sourceId(condition) {
        if (condition.source_instance_id) return condition.source_instance_id;
        if (condition.source && typeof condition.source === "object") return condition.source.instance_id;
        return "";
    }
    function conditionChildren(rule) {
        const conditions = rule.conditions || (rule.schedule ? {time:{}} : {});
        if (Array.isArray(conditions.all)) return { match: "all", items: conditions.all };
        if (Array.isArray(conditions.any)) return { match: "any", items: conditions.any };
        return { match: "all", items: [conditions] };
    }
    function formatTime(value) {
        if (!value || !/^\d\d:\d\d$/.test(value)) return value || "";
        const [hour, minute] = value.split(":").map(Number);
        const suffix = hour >= 12 ? "PM" : "AM";
        return `${hour % 12 || 12}:${String(minute).padStart(2, "0")} ${suffix}`;
    }
    function displayValue(value, metadata) {
        if (typeof value === "boolean") return value ? "Yes" : "No";
        if (metadata && metadata.type === "probability" && typeof value === "number") return `${Math.round(value * 1000) / 10}%`;
        if (Array.isArray(value)) return value.join(", ");
        return value == null ? "Unavailable" : String(value);
    }
    function summarizeCondition(condition) {
        if (condition.time) {
            const selectedDays = (condition.time.days || days).map(day => dayLabels[day] || day).join(", ");
            const range = condition.time.start && condition.time.end ? ` from ${formatTime(condition.time.start)} to ${formatTime(condition.time.end)}` : "";
            const calendar = condition.time;
            return `${selectedDays}${range}${calendar.months ? " in months " + calendar.months.join(", ") : ""}${calendar.days_of_month ? " on days " + calendar.days_of_month.join(", ") + " of the month" : ""}${calendar.hours ? " during hours " + calendar.hours.map(hour => formatTime(String(hour).padStart(2,"0")+":00")).join(", ") : ""}${calendar.date_start || calendar.date_end ? " from " + (calendar.date_start || "any date") + " through " + (calendar.date_end || "any date") : ""}`;
        }
        if (condition.not) return `NOT (${summarizeCondition(condition.not)})`;
        if (condition.all || condition.any) {
            const key = condition.all ? "all" : "any";
            return condition[key].map(summarizeCondition).join(key === "all" ? " AND " : " OR ");
        }
        const metadata = schemaFor(sourceId(condition), condition.path);
        const label = metadata && metadata.label ? metadata.label : condition.path;
        const op = condition.operator;
        if (["exists", "truthy", "falsy"].includes(op)) return `${label} ${operatorLabels[op]}`;
        return `${label} ${operatorLabels[op] || op} ${displayValue(condition.value, metadata)}`;
    }
    function summarizeRule(rule) {
        const group = conditionChildren(rule);
        const joiner = group.match === "all" ? " AND " : " OR ";
        if (rule.schedule) {
            const seconds = (rule.duration || {}).seconds || 0;
            const duration = seconds % 60 === 0 ? `${seconds / 60} minutes` : `${seconds} seconds`;
            const neutral = group.items.every(item => item.time && !Object.keys(item.time).length);
            return `Show ${targetName(rule)} for ${duration} ${summarizeSchedule(rule.schedule)}${neutral ? "" : " when " + group.items.map(summarizeCondition).join(joiner)}.`;
        }
        return `Show ${targetName(rule)} when ${group.items.map(summarizeCondition).join(joiner)}.`;
    }

    function summarizeSchedule(schedule) {
        let summary;
        if (schedule.type === "minute_of_hour") summary = `at ${schedule.minutes.map(value => ":" + String(value).padStart(2, "0")).join(" and ")} past every hour`;
        else if (schedule.type === "interval") summary = `every ${schedule.every_minutes} minutes`;
        else if (schedule.type === "daily") summary = `at ${schedule.times.map(formatTime).join(", ")}`;
        else summary = `once on ${schedule.at.slice(0,10)} at ${formatTime(schedule.at.slice(11,16))}`;
        const selected = schedule.days || [];
        if (selected.length && selected.length !== 7) {
            const preset = dayPreset(selected);
            summary += preset === "weekdays" ? " on weekdays" : preset === "weekends" ? " on weekends" : " on " + selected.map(day => dayLabels[day]).join(", ");
        }
        if (schedule.start) summary += ` from ${formatTime(schedule.start)} to ${formatTime(schedule.end)}`;
        if (schedule.hours) summary += ` during hours ${schedule.hours.map(hour => formatTime(String(hour).padStart(2,"0")+":00")).join(", ")}`;
        if (schedule.days_of_month) summary += ` on days ${schedule.days_of_month.join(", ")} of the month`;
        if (schedule.months) summary += ` in ${schedule.months.map(month => new Date(2026,month-1,1).toLocaleString([], {month:"long"})).join(", ")}`;
        if (schedule.date_start || schedule.date_end) summary += ` from ${schedule.date_start || "any date"} through ${schedule.date_end || "any date"}`;
        return summary;
    }

    function dayPreset(selected) {
        if (!selected || selected.length === 7) return "everyday";
        const key = selected.slice().sort().join(",");
        if (key === ["mon", "tue", "wed", "thu", "fri"].sort().join(",")) return "weekdays";
        if (key === "sat,sun") return "weekends";
        return "custom";
    }
    function syncJson() {
        if (!state.config) return;
        state.config.enabled = el("dynamicSchedulerEnabled").checked;
        el("dynamicSchedulerConfig").value = JSON.stringify(state.config, null, 2);
        el("schedulerJsonError").textContent = "";
    }
    function normalizeConfig(raw) {
        const config = clone(raw || {});
        config.enabled = Boolean(config.enabled);
        config.version = config.version == null ? 1 : config.version;
        config.evaluation_interval_seconds = config.evaluation_interval_seconds || 60;
        config.defaults = config.defaults || { fallback: "playlist", return_behavior: "resume_previous", minimum_display_seconds: 60 };
        config.rules = Array.isArray(config.rules) ? config.rules : [];
        return config;
    }
    function uniqueId(name) {
        const base = String(name || "rule").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "") || "rule";
        const ids = new Set((state.config.rules || []).map(rule => rule.id));
        let result = base, number = 2;
        while (ids.has(result)) result = `${base}_${number++}`;
        return result;
    }

    async function api(url, options) {
        const response = await fetch(url, options);
        const body = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(body.error || "The request could not be completed.");
        return body;
    }
    async function saveConfig(message) {
        syncJson();
        try {
            await api("/scheduler/config", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(state.config) });
            renderRules();
            await refreshStatus();
            if (message && window.showResponseModal) showResponseModal("success", message);
            state.lastError = "";
            return true;
        } catch (error) {
            state.lastError = error.message;
            if (window.showResponseModal) showResponseModal("failure", error.message);
            else alert(error.message);
            return false;
        }
    }

    function renderRules() {
        const container = el("schedulerRuleList");
        const rules = state.config.rules || [];
        if (!rules.length) {
            container.innerHTML = '<p class="scheduler-muted">No rules yet. Add a rule to dynamically interrupt the normal playlist.</p>';
            return;
        }
        container.innerHTML = rules.map((rule, index) => {
            const active = state.status && state.status.rule_id === rule.id;
            const duration = (rule.duration || {}).mode === "fixed" ? `Fixed for ${(rule.duration || {}).seconds || 0} seconds` : "While conditions are true";
            return `<article class="scheduler-rule-card ${rule.enabled === false ? "is-disabled" : ""} ${active ? "is-active" : ""}" data-index="${index}">
                <div class="scheduler-card-header">
                    <div class="scheduler-card-title"><span class="scheduler-state-dot" aria-hidden="true">${rule.enabled === false ? "○" : "●"}</span><strong>${escapeHtml(ruleName(rule))}</strong>${active ? '<span class="scheduler-priority">ACTIVE</span>' : ""}</div>
                    <span class="scheduler-priority">PRIORITY ${escapeHtml(rule.priority)}</span>
                </div>
                <div class="scheduler-card-target">${escapeHtml(targetName(rule))}</div>
                <p class="scheduler-card-summary">${escapeHtml(summarizeRule(rule))}</p>
                ${rule.schedule ? `<p class="scheduler-card-details">Next trigger: ${escapeHtml(nextTriggerLabel(rule.id))}</p>` : ""}
                <p class="scheduler-card-details">${escapeHtml(duration)} · ${escapeHtml(returnLabels[rule.return_behavior] || rule.return_behavior || "Return to previous display")} · Cooldown ${escapeHtml(rule.cooldown_seconds || 0)} sec</p>
                <div class="scheduler-card-actions">
                    <button type="button" data-action="test">Test</button><button type="button" data-action="edit">Edit</button>
                    <button type="button" data-action="duplicate">Duplicate</button><button type="button" data-action="toggle">${rule.enabled === false ? "Enable" : "Disable"}</button>
                    <button type="button" data-action="delete">Delete</button>
                </div>
            </article>`;
        }).join("");
    }

    function nextTriggerLabel(ruleId) {
        const item = state.status && (state.status.temporal_rules || []).find(value => value.rule_id === ruleId);
        return item ? (item.next_occurrence ? new Date(item.next_occurrence).toLocaleString([], {timeZone: state.timezone}) : "No future occurrence") : "Use Test to calculate";
    }

    function humanReturn(value) { return (returnLabels[value] || value || "Previous display").replace(/^Return to /, ""); }
    async function refreshStatus() {
        try {
            const status = await api("/scheduler/status");
            state.status = status;
            const also = (status.matching_rules || []).length
                ? `<div class="scheduler-status-item"><span>Also matching</span><strong>${status.matching_rules.map(item => `${escapeHtml(item.name)} (${item.priority})`).join(", ")}</strong></div>` : "";
            const upcoming = (status.temporal_rules || []).filter(item => item.next_occurrence).sort((a,b) => new Date(a.next_occurrence)-new Date(b.next_occurrence))[0];
            el("schedulerStatus").innerHTML = `<strong>${status.enabled ? "ON" : "OFF"}</strong>
                <div class="scheduler-status-grid">
                    <div class="scheduler-status-item"><span>Currently displaying</span><strong>${escapeHtml(status.currently_displaying || "Normal Playlist")}</strong></div>
                    <div class="scheduler-status-item"><span>Selected by</span><strong>${escapeHtml(status.selected_by || status.reason || "Normal playlist")}</strong></div>
                    ${status.priority == null ? "" : `<div class="scheduler-status-item"><span>Priority</span><strong>${status.priority}</strong></div>`}
                    ${status.active_since ? `<div class="scheduler-status-item"><span>Active since</span><strong>${escapeHtml(new Date(status.active_since).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }))}</strong></div>` : ""}
                    ${status.return_behavior ? `<div class="scheduler-status-item"><span>After rule ends</span><strong>${escapeHtml(humanReturn(status.return_behavior))}</strong></div>` : ""}
                    ${status.next_evaluation_seconds == null ? "" : `<div class="scheduler-status-item"><span>Next evaluation</span><strong>~${Math.max(0, Math.ceil(status.next_evaluation_seconds))} sec</strong></div>`}
                    ${also}
                    ${upcoming ? `<div class="scheduler-status-item"><span>Next temporal trigger</span><strong>${escapeHtml(upcoming.name)} · ${escapeHtml(new Date(upcoming.next_occurrence).toLocaleString([], {timeZone:state.timezone}))}</strong></div>` : ""}
                </div>`;
            renderRules();
        } catch (error) {
            el("schedulerStatus").textContent = `Status unavailable: ${error.message}`;
        }
    }

    function openDialog(content) {
        const dialog = el("schedulerDialog");
        el("schedulerDialogContent").innerHTML = content;
        if (typeof dialog.showModal === "function") dialog.showModal(); else dialog.setAttribute("open", "");
    }
    function closeDialog() { el("schedulerDialog").close(); }

    function defaultCondition() {
        const source = state.instances.find(item => Object.keys(item.schema || {}).length);
        if (source) {
            const path = Object.keys(source.schema)[0];
            return { source_instance_id: source.instance_id, path, operator: "eq", value: source.schema[path].type === "boolean" ? true : "" };
        }
        return { time: { days: days.slice(), start: "09:00", end: "17:00" } };
    }
    function defaultRule() {
        const target = state.instances[0];
        return {
            id: uniqueId("new_rule"), name: "New Rule", enabled: true, priority: 60,
            target: { instance_id: target ? target.instance_id : "" },
            conditions: { all: [defaultCondition()] },
            duration: { mode: "while_true", min_seconds: 60 }, cooldown_seconds: 0,
            return_behavior: "resume_previous"
        };
    }
    function canVisualEdit(rule) {
        const group = conditionChildren(rule);
        if (rule.schedule && rule.schedule.type === "once" && /(?:Z|[+-]\d{2}:\d{2})$/i.test(rule.schedule.at)) return false;
        return group.items.length > 0 && group.items.every(item => item.time || (item.path && sourceId(item)));
    }
    function startEdit(index) {
        const source = index == null ? defaultRule() : clone(state.config.rules[index]);
        if (!canVisualEdit(source)) {
            alert("This rule uses advanced nested conditions or an offset-qualified datetime. Edit it in Advanced JSON to preserve its structure.");
            return;
        }
        state.editIndex = index;
        state.draft = source;
        renderEditor();
    }

    function option(value, label, selected) { return `<option value="${escapeHtml(value)}" ${value === selected ? "selected" : ""}>${escapeHtml(label)}</option>`; }
    function instanceOptions(selected, schemasOnly) {
        const candidates = schemasOnly ? state.instances.filter(item => Object.keys(item.schema || {}).length) : state.instances;
        return candidates.map(item => option(item.instance_id, `${item.instance_name} — ${item.plugin_name} (${item.playlist})`, selected)).join("");
    }
    function fieldOptions(source, selected) {
        const found = instance(source), schema = found ? found.schema || {} : {};
        return Object.entries(schema).map(([path, meta]) => option(path, meta.label || path, selected)).join("");
    }
    function operatorsFor(metadata) {
        const type = metadata && metadata.type;
        if (type === "boolean" || type === "enum") return ["eq", "ne"];
        if (["number", "probability"].includes(type)) return ["eq", "ne", "gt", "gte", "lt", "lte"];
        return ["eq", "ne", "contains", "in", "not_in", "exists", "truthy", "falsy"];
    }
    function conditionValueControl(condition, metadata, index) {
        if (["exists", "truthy", "falsy"].includes(condition.operator)) return '<span class="scheduler-muted">No value</span>';
        if (metadata && metadata.type === "boolean") {
            return `<select data-field="value" data-condition="${index}">${option("true", "Yes", String(condition.value))}${option("false", "No", String(condition.value))}</select>`;
        }
        if (metadata && (metadata.allowed_values || metadata.values)) {
            return `<select data-field="value" data-condition="${index}">${(metadata.allowed_values || metadata.values).map(value => option(String(value), String(value), String(condition.value))).join("")}</select>`;
        }
        const numeric = metadata && ["number", "probability"].includes(metadata.type);
        let value = condition.value == null ? "" : condition.value;
        if (metadata && metadata.type === "probability" && typeof value === "number") value = value * 100;
        return `<input data-field="value" data-condition="${index}" type="${numeric ? "number" : "text"}" ${metadata && metadata.type === "probability" ? 'min="0" max="100" step="0.1"' : numeric ? 'step="any"' : ""} value="${escapeHtml(value)}" aria-label="Condition value">${metadata && metadata.type === "probability" ? '<span class="scheduler-muted">%</span>' : ""}`;
    }
    function renderCondition(condition, index) {
        if (condition.time) {
            return `<div class="scheduler-condition-row" data-condition-row="${index}">
                <div class="scheduler-form-field"><label>Type</label><select data-field="kind" data-condition="${index}">${option("state", "Plugin State", "time")}${option("time", "Time", "time")}</select></div>
                <div class="scheduler-form-field" style="grid-column: span 2"><label>Days</label><div class="scheduler-day-list">${days.map(day => `<label><input type="checkbox" data-field="day" data-day="${day}" data-condition="${index}" ${(condition.time.days || days).includes(day) ? "checked" : ""}> ${dayLabels[day]}</label>`).join("")}</div></div>
                <div class="scheduler-form-field"><label>From</label><input type="time" data-field="start" data-condition="${index}" value="${escapeHtml(condition.time.start || "")}"></div>
                <div class="scheduler-form-field"><label>To</label><input type="time" data-field="end" data-condition="${index}" value="${escapeHtml(condition.time.end || "")}"></div>
                <button type="button" data-editor-action="remove-condition" data-condition="${index}" aria-label="Remove condition">Remove</button>
                <details style="grid-column: 1 / -1"><summary>Calendar restrictions</summary><div class="scheduler-form-grid">
                    ${[["months", "Months", 1,12],["days_of_month", "Days of month",1,31],["hours","Hours",0,23]].map(([key,label,min,max]) => `<div class="scheduler-form-field"><label for="condition-${index}-${key}">${label} (optional)</label><select id="condition-${index}-${key}" data-field="${key}" data-condition="${index}" multiple size="4">${Array.from({length:max-min+1},(_,i)=>i+min).map(value=>`<option value="${value}" ${(condition.time[key]||[]).includes(value)?"selected":""}>${key === "months" ? new Date(2026,value-1,1).toLocaleString([], {month:"long"}) : value}</option>`).join("")}</select></div>`).join("")}
                    <div class="scheduler-form-field"><label for="condition-${index}-date-start">First date (optional)</label><input id="condition-${index}-date-start" type="date" data-field="date_start" data-condition="${index}" value="${escapeHtml(condition.time.date_start||"")}"></div>
                    <div class="scheduler-form-field"><label for="condition-${index}-date-end">Last date (inclusive, optional)</label><input id="condition-${index}-date-end" type="date" data-field="date_end" data-condition="${index}" value="${escapeHtml(condition.time.date_end||"")}"></div>
                </div></details>
            </div>`;
        }
        const source = sourceId(condition);
        const metadata = schemaFor(source, condition.path) || {};
        return `<div class="scheduler-condition-row" data-condition-row="${index}">
            <div class="scheduler-form-field"><label>Type</label><select data-field="kind" data-condition="${index}">${option("state", "Plugin State", "state")}${option("time", "Time", "state")}</select></div>
            <div class="scheduler-form-field"><label>Source plugin</label><select data-field="source" data-condition="${index}">${instanceOptions(source, true)}</select></div>
            <div class="scheduler-form-field"><label>State field</label><select data-field="path" data-condition="${index}">${fieldOptions(source, condition.path)}</select></div>
            <div class="scheduler-form-field"><label>Operator</label><select data-field="operator" data-condition="${index}">${operatorsFor(metadata).map(op => option(op, operatorLabels[op] || op, condition.operator)).join("")}</select></div>
            <div class="scheduler-form-field"><label>Value${metadata.type === "probability" ? " (percent)" : ""}</label>${conditionValueControl(condition, metadata, index)}</div>
            <button type="button" data-editor-action="remove-condition" data-condition="${index}" aria-label="Remove condition">Remove</button>
        </div>`;
    }

    function renderScheduleEditor(rule) {
        const schedule = rule.schedule || {};
        const kind = schedule.type || "none";
        const scheduleTypes = {none: "Time Window / Conditions Only", daily: "At Specific Times", minute_of_hour: "Minutes Past the Hour", interval: "Every N Minutes", once: "One-Time Date/Time"};
        let timing = "";
        if (kind === "minute_of_hour" || kind === "daily") {
            const values = kind === "daily" ? schedule.times : schedule.minutes;
            const noun = kind === "daily" ? "time" : "minute";
            timing = `<div class="scheduler-form-field"><label>Run at ${kind === "minute_of_hour" ? "(minutes past each hour)" : ""}</label><div class="scheduler-inline-controls">${(values || []).map((value, index) => `<label class="scheduler-temporal-value"><span class="scheduler-sr-only">${noun} ${index + 1}</span><input data-schedule-value="${index}" type="${kind === "daily" ? "time" : "number"}" ${kind === "daily" ? "" : 'min="0" max="59" step="1"'} value="${escapeHtml(value)}"><button type="button" data-editor-action="remove-schedule-value" data-value-index="${index}" aria-label="Remove ${noun} ${index + 1}">×</button></label>`).join("")}</div><button type="button" class="secondary-button" data-editor-action="add-schedule-value">+ Add ${noun}</button></div>`;
        } else if (kind === "interval") {
            timing = `<div class="scheduler-form-field"><label for="schedulerEveryMinutes">Every (minutes)</label><input id="schedulerEveryMinutes" type="number" min="1" max="1440" step="1" list="schedulerIntervalOptions" value="${escapeHtml(schedule.every_minutes || 30)}"><datalist id="schedulerIntervalOptions">${[5,10,15,20,30,60].map(value => `<option value="${value}"></option>`).join("")}</datalist><small>Aligned to midnight in the device timezone, not service startup. 60 means hourly.</small></div>`;
        } else if (kind === "once") {
            timing = `<div class="scheduler-form-grid"><div class="scheduler-form-field"><label for="schedulerOnceDate">Date</label><input id="schedulerOnceDate" type="date" value="${escapeHtml((schedule.at || "").slice(0,10))}"></div><div class="scheduler-form-field"><label for="schedulerOnceTime">Time</label><input id="schedulerOnceTime" type="time" value="${escapeHtml((schedule.at || "").slice(11,16))}"></div></div>`;
        }
        const preset = dayPreset(schedule.days);
        const restrictions = kind === "none" ? "" : `<div class="scheduler-form-grid">
            <div class="scheduler-form-field"><label for="schedulerDayPreset">Days</label><select id="schedulerDayPreset">${Object.entries({everyday:"Every day",weekdays:"Weekdays",weekends:"Weekends",custom:"Custom"}).map(([value,label]) => option(value,label,preset)).join("")}</select>${preset === "custom" ? `<div class="scheduler-day-list">${days.map(day => `<label><input type="checkbox" data-schedule-day="${day}" ${(schedule.days || []).includes(day) ? "checked" : ""}> ${dayLabels[day]}</label>`).join("")}</div>` : ""}</div>
            <div class="scheduler-form-field"><label for="schedulerWindowStart">From (optional window)</label><input id="schedulerWindowStart" type="time" value="${escapeHtml(schedule.start || "")}"></div>
            <div class="scheduler-form-field"><label for="schedulerWindowEnd">To (end exclusive)</label><input id="schedulerWindowEnd" type="time" value="${escapeHtml(schedule.end || "")}"><small>Earlier end times cross midnight.</small></div>
        </div><details><summary>Calendar and hour restrictions</summary><div class="scheduler-form-grid">
            ${[["hours", "Selected hours (optional)", 0, 23], ["days_of_month", "Days of month (optional)", 1, 31], ["months", "Months (optional)", 1, 12]].map(([key,label,min,max]) => `<div class="scheduler-form-field"><label for="schedulerFilter-${key}">${label}</label><select id="schedulerFilter-${key}" data-schedule-filter="${key}" multiple size="4">${Array.from({length:max-min+1}, (_,i) => i+min).map(value => `<option value="${value}" ${(schedule[key] || []).includes(value) ? "selected" : ""}>${key === "months" ? new Date(2026,value-1,1).toLocaleString([], {month:"long"}) : key === "hours" ? formatTime(String(value).padStart(2,"0")+":00") : value}</option>`).join("")}</select><small>No selection means unrestricted. Use Ctrl/Command to select multiple.</small></div>`).join("")}
            <div class="scheduler-form-field"><label for="schedulerDateStart">First date (optional)</label><input id="schedulerDateStart" type="date" value="${escapeHtml(schedule.date_start || "")}"></div>
            <div class="scheduler-form-field"><label for="schedulerDateEnd">Last date (inclusive, optional)</label><input id="schedulerDateEnd" type="date" value="${escapeHtml(schedule.date_end || "")}"></div>
        </div></details>`;
        return `<fieldset id="schedulerScheduleEditor" data-rendered-type="${kind}" class="scheduler-schedule-editor"><legend>Temporal Schedule</legend><div class="scheduler-form-field"><label for="schedulerScheduleType">Schedule type</label><select id="schedulerScheduleType">${Object.entries(scheduleTypes).map(([value,label]) => option(value,label,kind)).join("")}</select><small>All times use the device timezone: ${escapeHtml(state.timezone)}. Occurrences do not require plugin state.</small></div>${timing}${restrictions}<p id="schedulerFrequencyWarning" class="scheduler-muted">${kind === "interval" && schedule.every_minutes < 5 ? "Frequent display updates are not recommended for e-ink. Image-hash protection still applies." : ""}</p></fieldset>`;
    }

    function readScheduleEditor() {
        const kind = el("schedulerScheduleType").value;
        if (kind === "none" || el("schedulerScheduleEditor").dataset.renderedType !== kind) return;
        const schedule = state.draft.schedule || {};
        if (kind === "daily" || kind === "minute_of_hour") {
            schedule[kind === "daily" ? "times" : "minutes"] = [...document.querySelectorAll('[data-schedule-value]')].map(node => kind === "daily" ? node.value : (node.value === "" ? null : Number(node.value)));
        } else if (kind === "interval") schedule.every_minutes = Number(el("schedulerEveryMinutes").value);
        else schedule.at = `${el("schedulerOnceDate").value}T${el("schedulerOnceTime").value}`;
        const preset = el("schedulerDayPreset").value;
        schedule.days = preset === "weekdays" ? days.slice(0,5) : preset === "weekends" ? days.slice(5) : preset === "custom" ? [...document.querySelectorAll('[data-schedule-day]:checked')].map(node => node.dataset.scheduleDay) : days.slice();
        for (const [key,id] of [["start","schedulerWindowStart"],["end","schedulerWindowEnd"],["date_start","schedulerDateStart"],["date_end","schedulerDateEnd"]]) {
            if (el(id).value) schedule[key] = el(id).value; else delete schedule[key];
        }
        document.querySelectorAll('[data-schedule-filter]').forEach(node => {
            const values = [...node.selectedOptions].map(item => Number(item.value));
            if (values.length) schedule[node.dataset.scheduleFilter] = values; else delete schedule[node.dataset.scheduleFilter];
        });
        state.draft.schedule = schedule;
    }

    function renderEditor() {
        const rule = state.draft;
        const group = conditionChildren(rule);
        const duration = rule.duration || { mode: "while_true", min_seconds: 60 };
        openDialog(`<form id="schedulerRuleForm" class="scheduler-dialog-form" novalidate>
            <div class="scheduler-dialog-header"><h2>${state.editIndex == null ? "Add Rule" : "Edit Rule"}</h2><button type="button" data-editor-action="cancel" aria-label="Close">Close</button></div>
            <div class="scheduler-form-grid">
                <div class="scheduler-form-field"><label for="schedulerRuleName">Rule name</label><input id="schedulerRuleName" data-rule-field="name" required value="${escapeHtml(ruleName(rule))}"></div>
                <div class="scheduler-form-field"><label for="schedulerRuleTarget">Display</label><select id="schedulerRuleTarget" data-rule-field="target">${instanceOptions((rule.target || {}).instance_id, false)}</select></div>
                <div class="scheduler-form-field"><label for="schedulerRulePriority">Priority</label><input id="schedulerRulePriority" data-rule-field="priority" type="number" step="1" required value="${escapeHtml(rule.priority)}"><small>100 Critical · 80 High · 60 Medium · 40 Low · 20 Background</small></div>
                <div class="scheduler-form-field"><label for="schedulerRuleEnabled">Rule enabled</label><select id="schedulerRuleEnabled" data-rule-field="enabled">${option("true", "Enabled", String(rule.enabled !== false))}${option("false", "Disabled", String(rule.enabled !== false))}</select></div>
            </div>
            ${renderScheduleEditor(rule)}
            <fieldset class="scheduler-form-field"><legend>Conditions</legend><label for="schedulerRuleMatch">Match</label><select id="schedulerRuleMatch" data-rule-field="match">${option("all", "ALL conditions", group.match)}${option("any", "ANY condition", group.match)}</select></fieldset>
            <div id="schedulerConditionList" class="scheduler-condition-list">${group.items.map(renderCondition).join("")}</div>
            <button type="button" class="secondary-button" data-editor-action="add-condition">+ Add Condition</button>
            <div class="scheduler-form-grid">
                <div class="scheduler-form-field"><label for="schedulerDurationMode">Duration</label><select id="schedulerDurationMode" data-rule-field="duration-mode">${option("while_true", "While conditions are true", duration.mode || "while_true")}${option("fixed", "Fixed duration", duration.mode)}</select></div>
                ${duration.mode === "fixed" ? `<div class="scheduler-form-field"><label for="schedulerDurationSeconds">Display for</label><div class="scheduler-inline-controls"><input id="schedulerDurationSeconds" data-rule-field="duration-seconds" type="number" min="1" step="1" value="${escapeHtml((duration.seconds || 300) % 60 === 0 ? (duration.seconds || 300) / 60 : duration.seconds)}"><select id="schedulerDurationUnit" aria-label="Display duration unit">${option("60", "minutes", (duration.seconds || 300) % 60 === 0 ? "60" : "1")}${option("1", "seconds", (duration.seconds || 300) % 60 === 0 ? "60" : "1")}</select></div></div>` : `<div class="scheduler-form-field"><label for="schedulerMinSeconds">Minimum display (seconds)</label><input id="schedulerMinSeconds" data-rule-field="min-seconds" type="number" min="0" step="1" value="${escapeHtml(duration.min_seconds == null ? 60 : duration.min_seconds)}"></div><div class="scheduler-form-field"><label for="schedulerMaxSeconds">Maximum display (seconds, optional)</label><input id="schedulerMaxSeconds" data-rule-field="max-seconds" type="number" min="1" step="1" value="${escapeHtml(duration.max_seconds == null ? "" : duration.max_seconds)}"></div>`}
                <div class="scheduler-form-field"><label for="schedulerCooldown">Cooldown (seconds)</label><input id="schedulerCooldown" data-rule-field="cooldown" type="number" min="0" step="1" value="${escapeHtml(rule.cooldown_seconds || 0)}"></div>
                <div class="scheduler-form-field"><label for="schedulerReturn">After rule ends</label><select id="schedulerReturn" data-rule-field="return">${Object.entries(returnLabels).map(([value, label]) => option(value, label, rule.return_behavior || "resume_previous")).join("")}</select></div>
            </div>
            <div id="schedulerRuleError" class="scheduler-field-error" role="alert"></div>
            <div class="scheduler-dialog-actions"><button type="submit" class="action-button">Save Rule</button><button type="button" data-editor-action="cancel">Cancel</button></div>
        </form>`);
        bindEditor();
    }

    function draftItems() { return conditionChildren(state.draft).items; }
    function setDraftItems(items, match) { state.draft.conditions = { [match || conditionChildren(state.draft).match]: items }; }
    function updateConditionFromControl(control) {
        const index = Number(control.dataset.condition), items = draftItems(), condition = items[index];
        if (!condition) return;
        const field = control.dataset.field;
        if (field === "kind") {
            items[index] = control.value === "time" ? { time: { days: days.slice(), start: "09:00", end: "17:00" } } : defaultCondition();
            setDraftItems(items); renderEditor(); return;
        }
        if (condition.time) {
            if (field === "day") condition.time.days = days.filter(day => document.querySelector(`[data-field="day"][data-condition="${index}"][data-day="${day}"]`).checked);
            else if (["months","days_of_month","hours"].includes(field)) {
                const values = [...control.selectedOptions].map(item => Number(item.value));
                if (values.length) condition.time[field] = values; else delete condition.time[field];
            }
            else if (control.value) condition.time[field] = control.value;
            else delete condition.time[field];
            return;
        }
        if (field === "source") {
            condition.source_instance_id = control.value;
            delete condition.source;
            const found = instance(control.value), paths = Object.keys((found && found.schema) || {});
            condition.path = paths[0] || "";
            const meta = schemaFor(control.value, condition.path);
            condition.operator = "eq";
            condition.value = meta && meta.type === "boolean" ? true : "";
            renderEditor(); return;
        }
        if (field === "path") {
            condition.path = control.value;
            const meta = schemaFor(sourceId(condition), condition.path);
            condition.operator = "eq";
            condition.value = meta && meta.type === "boolean" ? true : "";
            renderEditor(); return;
        }
        if (field === "operator") {
            condition.operator = control.value;
            if (!["exists", "truthy", "falsy"].includes(control.value) && !("value" in condition)) condition.value = "";
            renderEditor(); return;
        }
        if (field === "value") {
            const meta = schemaFor(sourceId(condition), condition.path);
            if (meta && meta.type === "boolean") condition.value = control.value === "true";
            else if (meta && ["number", "probability"].includes(meta.type)) condition.value = control.value === "" ? "" : Number(control.value) / (meta.type === "probability" ? 100 : 1);
            else if (["in", "not_in"].includes(condition.operator)) condition.value = control.value.split(",").map(value => value.trim()).filter(Boolean);
            else condition.value = control.value;
        }
    }
    function updateRuleFromControls() {
        const form = el("schedulerRuleForm");
        state.draft.name = form.querySelector('[data-rule-field="name"]').value.trim();
        state.draft.target = { instance_id: form.querySelector('[data-rule-field="target"]').value };
        state.draft.priority = Number(form.querySelector('[data-rule-field="priority"]').value);
        state.draft.enabled = form.querySelector('[data-rule-field="enabled"]').value === "true";
        const old = conditionChildren(state.draft);
        setDraftItems(old.items, form.querySelector('[data-rule-field="match"]').value);
        const mode = form.querySelector('[data-rule-field="duration-mode"]').value;
        const prior = state.draft.duration || {};
        state.draft.duration = mode === "fixed"
            ? { mode, seconds: form.querySelector('[data-rule-field="duration-seconds"]') ? Number(form.querySelector('[data-rule-field="duration-seconds"]').value) * Number(el("schedulerDurationUnit").value) : prior.seconds || 120 }
            : { mode, min_seconds: Number(form.querySelector('[data-rule-field="min-seconds"]')?.value ?? prior.min_seconds ?? 60), ...(form.querySelector('[data-rule-field="max-seconds"]')?.value ? { max_seconds: Number(form.querySelector('[data-rule-field="max-seconds"]').value) } : {}) };
        state.draft.cooldown_seconds = Number(form.querySelector('[data-rule-field="cooldown"]').value);
        state.draft.return_behavior = form.querySelector('[data-rule-field="return"]').value;
        readScheduleEditor();
    }
    function bindEditor() {
        const form = el("schedulerRuleForm");
        form.addEventListener("change", event => {
            if (event.target.dataset.field) { updateRuleFromControls(); updateConditionFromControl(event.target); }
            else if (event.target.dataset.ruleField === "duration-mode") { updateRuleFromControls(); renderEditor(); }
            else if (event.target.id === "schedulerScheduleType") {
                updateRuleFromControls();
                const kind = event.target.value;
                if (kind === "none") delete state.draft.schedule;
                else {
                    state.draft.schedule = {type:kind, ...(kind === "daily" ? {times:["08:00"]} : kind === "minute_of_hour" ? {minutes:[5]} : kind === "interval" ? {every_minutes:30} : {at:""})};
                    state.draft.duration = {mode:"fixed",seconds:120};
                    if (state.editIndex == null) state.draft.conditions = {time:{}};
                }
                renderEditor();
            } else if (event.target.id === "schedulerDayPreset") { updateRuleFromControls(); renderEditor(); }
        });
        form.addEventListener("input", event => {
            if (event.target.dataset.field === "value") updateConditionFromControl(event.target);
            if (event.target.id === "schedulerEveryMinutes") el("schedulerFrequencyWarning").textContent = Number(event.target.value) < 5 ? "Frequent display updates are not recommended for e-ink. Image-hash protection still applies." : "";
        });
        form.addEventListener("click", event => {
            const action = event.target.dataset.editorAction;
            if (action === "cancel") closeDialog();
            if (action === "add-schedule-value" || action === "remove-schedule-value") {
                updateRuleFromControls();
                const key = state.draft.schedule.type === "daily" ? "times" : "minutes";
                const values = state.draft.schedule[key];
                if (action === "add-schedule-value") values.push(key === "times" ? "12:00" : 0);
                else if (values.length > 1) values.splice(Number(event.target.dataset.valueIndex),1);
                renderEditor();
            }
            if (action === "add-condition") { updateRuleFromControls(); const items = draftItems(); items.push(defaultCondition()); setDraftItems(items); renderEditor(); }
            if (action === "remove-condition") {
                updateRuleFromControls(); const items = draftItems();
                if (items.length === 1) { el("schedulerRuleError").textContent = "A rule needs at least one condition."; return; }
                items.splice(Number(event.target.dataset.condition), 1); setDraftItems(items); renderEditor();
            }
        });
        form.addEventListener("submit", async event => {
            event.preventDefault(); updateRuleFromControls();
            if (!state.draft.name) { el("schedulerRuleError").textContent = "Enter a rule name."; return; }
            if (!state.draft.target.instance_id) { el("schedulerRuleError").textContent = "Choose a display."; return; }
            state.draft.id = state.draft.id || uniqueId(state.draft.name);
            const candidate = clone(state.config);
            if (state.editIndex == null) candidate.rules.push(state.draft); else candidate.rules[state.editIndex] = state.draft;
            if (state.draft.schedule) candidate.version = 2;
            const previous = state.config; state.config = candidate;
            if (await saveConfig()) closeDialog();
            else {
                state.config = previous;
                syncJson();
                el("schedulerRuleError").textContent = state.lastError;
            }
        });
    }

    function diagnosticRows(condition) {
        if (condition.children) return condition.children.map(diagnosticRows).join("");
        const mark = condition.matched ? "✓" : "✗";
        if (condition.kind === "time") return `<tr><td class="${condition.matched ? "scheduler-result-pass" : "scheduler-result-fail"}">${mark}</td><td>${escapeHtml(summarizeCondition({ time: condition.time }))}</td><td>${escapeHtml(new Date(condition.current_value).toLocaleString())}</td></tr>`;
        const metadata = schemaFor(condition.source_instance_id, condition.path);
        return `<tr><td class="${condition.matched ? "scheduler-result-pass" : "scheduler-result-fail"}">${mark}</td><td>${escapeHtml(summarizeCondition({ source_instance_id: condition.source_instance_id, path: condition.path, operator: condition.operator, value: condition.expected_value }))}</td><td>${escapeHtml(displayValue(condition.current_value, metadata))}</td></tr>`;
    }
    async function testRule(rule) {
        try {
            const result = await api("/scheduler/test-rule", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ rule }) });
            openDialog(`<div class="scheduler-dialog-header"><h2>${result.matched ? (result.schedule ? "Rule Occurrence Due" : "Rule Matches") : (result.schedule ? "Rule Does Not Match Yet" : "Rule Does Not Match")}</h2><button type="button" onclick="document.getElementById('schedulerDialog').close()">Close</button></div>
                ${temporalDiagnostic(result.schedule)}
                <h3>${escapeHtml(result.name)}</h3><table class="scheduler-diagnostic-list"><thead><tr><th>Result</th><th>Condition</th><th>Current value</th></tr></thead><tbody>${diagnosticRows(result.condition)}</tbody></table>
                <p><strong>Result:</strong> ${result.matched ? "MATCH" : "NO MATCH"}</p><p><strong>Would display:</strong> ${escapeHtml((result.target || {}).name || "Unavailable")} · <strong>Priority:</strong> ${result.priority}</p>`);
        } catch (error) { alert(error.message); }
    }
    function temporalDiagnostic(schedule) {
        if (!schedule) return "";
        const formatted = value => value ? new Date(value).toLocaleString([], {timeZone:state.timezone}) : "No future occurrence";
        return `<p><strong>Schedule:</strong> ${escapeHtml(schedule.summary)}</p><p><strong>Current time:</strong> ${escapeHtml(formatted(schedule.current_time))}</p>${schedule.scheduled_at ? `<p><strong>Scheduled:</strong> ${escapeHtml(formatted(schedule.scheduled_at))} · ${schedule.consumed ? "Already consumed" : "Occurrence due"}</p>` : ""}<p><strong>Next occurrence:</strong> ${escapeHtml(formatted(schedule.next_occurrence))}</p><p class="scheduler-muted">Observational test only: nothing is activated or consumed. Higher priorities and cooldowns may prevent selection.</p>`;
    }
    async function testAll() {
        try {
            const result = await api("/scheduler/test-all", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ config: state.config }) });
            const rows = result.rules.map(rule => `<tr><td>${rule.matched ? "MATCHING" : "NOT MATCHING"}</td><td>${rule.priority}</td><td>${escapeHtml(rule.name)}</td><td>${escapeHtml((rule.target || {}).name || "")}</td><td>${rule.schedule ? escapeHtml(rule.schedule.next_occurrence ? new Date(rule.schedule.next_occurrence).toLocaleString([], {timeZone:state.timezone}) : "No future occurrence") : "—"}</td></tr>`).join("");
            const winner = result.rules.find(rule => rule.rule_id === result.winner_rule_id);
            openDialog(`<div class="scheduler-dialog-header"><h2>Test All Rules</h2><button type="button" onclick="document.getElementById('schedulerDialog').close()">Close</button></div><table class="scheduler-diagnostic-list"><thead><tr><th>Result</th><th>Priority</th><th>Rule</th><th>Display</th><th>Next occurrence</th></tr></thead><tbody>${rows}</tbody></table><p><strong>Winner:</strong> ${winner ? escapeHtml(winner.name) : "No matching rule — normal playlist"}</p><p class="scheduler-muted">Shows current condition/occurrence eligibility, not held activation timers or cooldown selection. Nothing is triggered.</p>`);
        } catch (error) { alert(error.message); }
    }
    async function viewState() {
        try {
            const result = await api("/scheduler/state");
            const content = result.instances.length ? result.instances.map(item => `<h3>${escapeHtml(item.instance_name)} — ${escapeHtml(item.plugin_name)}</h3>${item.available ? `<table class="scheduler-state-table"><tbody>${Object.entries(item.state).map(([path, value]) => `<tr><th>${escapeHtml(path)}</th><td>${escapeHtml(displayValue(value, schemaFor(item.instance_id, path)))}</td></tr>`).join("")}</tbody></table>` : '<p class="scheduler-result-fail">State is currently unavailable.</p>'}`).join("") : '<p>No plugin instances expose scheduler state.</p>';
            openDialog(`<div class="scheduler-dialog-header"><h2>Current Scheduler State</h2><button type="button" onclick="document.getElementById('schedulerDialog').close()">Close</button></div>${content}`);
        } catch (error) { alert(error.message); }
    }

    async function initialize() {
        if (!el("schedulerSettings")) return;
        try {
            const data = await api("/scheduler/config");
            state.config = normalizeConfig(data.config);
            state.instances = data.instances || [];
            state.timezone = data.timezone || "UTC";
            el("dynamicSchedulerEnabled").checked = state.config.enabled;
            syncJson(); renderRules(); await refreshStatus();
        } catch (error) {
            el("schedulerRuleList").innerHTML = `<p class="scheduler-result-fail">Could not load scheduler settings: ${escapeHtml(error.message)}</p>`;
        }
        el("schedulerAddRule").addEventListener("click", () => startEdit(null));
        el("schedulerTestAll").addEventListener("click", testAll);
        el("schedulerViewState").addEventListener("click", viewState);
        el("schedulerRefreshStatus").addEventListener("click", refreshStatus);
        el("dynamicSchedulerEnabled").addEventListener("change", async event => {
            const previous = state.config.enabled;
            state.config.enabled = event.target.checked;
            if (!(await saveConfig("Dynamic Scheduler setting saved."))) {
                state.config.enabled = previous;
                event.target.checked = previous;
                syncJson();
            }
        });
        el("schedulerApplyJson").addEventListener("click", async () => {
            try {
                const enteredJson = el("dynamicSchedulerConfig").value;
                const parsed = normalizeConfig(JSON.parse(enteredJson));
                parsed.enabled = el("dynamicSchedulerEnabled").checked;
                const previous = state.config; state.config = parsed;
                if (!(await saveConfig("Advanced JSON applied."))) {
                    state.config = previous;
                    el("dynamicSchedulerConfig").value = enteredJson;
                    el("schedulerJsonError").textContent = state.lastError;
                }
            } catch (error) { el("schedulerJsonError").textContent = `Invalid JSON: ${error.message}`; }
        });
        el("schedulerRuleList").addEventListener("click", async event => {
            const button = event.target.closest("button[data-action]"); if (!button) return;
            const card = button.closest("[data-index]"), index = Number(card.dataset.index), rule = state.config.rules[index];
            if (button.dataset.action === "test") testRule(rule);
            if (button.dataset.action === "edit") startEdit(index);
            if (button.dataset.action === "duplicate") {
                const previous = clone(state.config);
                const copy = clone(rule); copy.id = uniqueId(`${rule.id || "rule"}_copy`); copy.name = `${ruleName(rule)} Copy`; copy.enabled = false;
                state.config.rules.push(copy);
                if (!(await saveConfig("Rule duplicated and left disabled."))) { state.config = previous; syncJson(); renderRules(); }
            }
            if (button.dataset.action === "toggle") {
                const previous = clone(state.config); rule.enabled = rule.enabled === false;
                if (!(await saveConfig(`Rule ${rule.enabled ? "enabled" : "disabled"}.`))) { state.config = previous; syncJson(); renderRules(); }
            }
            if (button.dataset.action === "delete" && confirm(`Delete “${ruleName(rule)}”?`)) {
                const previous = clone(state.config); state.config.rules.splice(index, 1);
                if (!(await saveConfig("Rule deleted."))) { state.config = previous; syncJson(); renderRules(); }
            }
        });
    }

    window.schedulerSettings = {
        prepareFormSave: function () {
            if (!state.config) return true;
            try {
                state.config = normalizeConfig(JSON.parse(el("dynamicSchedulerConfig").value));
                state.config.enabled = el("dynamicSchedulerEnabled").checked;
                syncJson(); return true;
            } catch (error) {
                el("schedulerJsonError").textContent = `Invalid JSON: ${error.message}`;
                el("dynamicSchedulerConfig").focus(); return false;
            }
        },
        summarizeCondition, summarizeRule, summarizeSchedule, normalizeConfig, uniqueId
    };
    document.addEventListener("DOMContentLoaded", initialize);
}());
