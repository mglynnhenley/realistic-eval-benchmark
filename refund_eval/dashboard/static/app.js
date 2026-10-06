"use strict";

// Presentation of recorded actions only. No generated summaries or inferred success.
(() => {
  const OUTGOING = new Set(["send_message", "reply", "send_email", "reply_email", "forward_email",
    "submit_web_form", "phone_call", "live_chat", "make_payment", "manage_email"]);
  const LABELS = {
    search_threads: "Search email", search_email: "Search email", get_message: "Read email",
    read_email: "Read email", get_thread: "Read email conversation", list_labels: "List email labels",
    send_message: "Send email", send_email: "Send email", reply: "Reply to email",
    reply_email: "Reply to email", forward_email: "Forward email", manage_email: "Update email",
    web_search_exa: "Search the web", web_search: "Search the web", web_fetch: "Read a web page",
    browser_navigate: "Open a web page", browser_snapshot: "Read the browser page",
    browser_click: "Click on the page", browser_type: "Type on the page",
    browser_fill_form: "Fill in a form", browser_evaluate: "Run code in the page",
    browser_press_key: "Press a key", browser_wait_for: "Wait in the browser",
    browser_navigate_back: "Go back", browser_tabs: "Manage browser tabs",
    submit_web_form: "Submit a form", phone_call: "Make a phone call",
    live_chat: "Send a chat message", make_payment: "Make a payment",
  };
  // The harness-local memory tool: the agent's own notes, stored with the trial, never sent anywhere.
  const NOTES = {view: "Read own notes", create: "Write own notes", str_replace: "Edit own notes",
    insert: "Edit own notes", delete: "Delete own notes", rename: "Rename own notes"};

  function text(value) {
    if (value == null) return "";
    if (typeof value === "string") return value;
    if (Array.isArray(value)) return value.map((part) => {
      if (typeof part === "string") return part;
      if (part?.type === "text") return part.text || "";
      if (part?.type === "image") return "[Image attachment]";
      if (part?.type === "audio") return "[Audio attachment]";
      return "";
    }).filter(Boolean).join("\n\n");
    return JSON.stringify(value, null, 2);
  }

  function parse(value) {
    if (typeof value !== "string") return value;
    try { return JSON.parse(value); } catch { return null; }
  }

  function short(value, limit = 220) {
    const string = text(value).replace(/\s+/g, " ").trim();
    return string.length > limit ? string.slice(0, limit).trimEnd() + "…" : string;
  }

  function describe(call) {
    const name = call.function || call.name || "Tool call";
    const args = parse(call.arguments) || {};
    if (name === "memory") return {name, title: NOTES[args.command] || "Use own notes", category: "notes",
      target: args.old_path ? `${args.old_path} → ${args.new_path || ""}` : short(args.path || ""), outgoing: false};
    const category = /email|thread|message|^reply$|list_labels/.test(name) ? "email"
      : /search/.test(name) ? "search" : /browser|web_fetch/.test(name) ? "browser" : "other";
    let target = "";
    if (args.query) target = `Query: ${short(args.query)}`;
    else if (args.to || args.toRecipients) target = `To: ${short(args.to || args.toRecipients)}`;
    else if (args.url) target = short(args.url);
    else if (args.element) target = short(args.element);
    else if (args.fields) target = `Fields: ${short(args.fields.map?.((field) => field.name || field.ref) || args.fields)}`;
    else if (args.messageId || args.email_id) target = `Message: ${short(args.messageId || args.email_id)}`;
    else if (args.threadId) target = `Conversation: ${short(args.threadId)}`;
    else if (args.key) target = `Key: ${short(args.key)}`;
    else if (args.text) target = short(args.text);
    else if (args.ref) target = `Element: ${short(args.ref)}`;
    if (args.subject) target += `${target ? "\n" : ""}Subject: ${short(args.subject)}`;
    return {name, title: LABELS[name] || name.replaceAll("_", " "), category, target,
      outgoing: OUTGOING.has(name)};
  }

  function resultData(result) {
    const raw = text(result?.content);
    const parsed = parse(raw);
    return {raw, parsed, data: parsed?.structuredContent || parsed};
  }

  function status(entry, active) {
    if (entry.call.parse_error) return {kind: "error", label: "Invalid arguments"};
    if (!entry.result) return {kind: active ? "pending" : "missing",
      label: active ? "Awaiting result" : "No result recorded"};
    const {raw, parsed} = resultData(entry.result);
    if (entry.result.error?.type === "limit") return {kind: "error", label: "Interrupted"};
    if (entry.result.error || parsed?.isError === true
      || /^### Error\b/m.test(raw) || /^Error:/i.test(raw)) return {kind: "error", label: "Tool error"};
    const http = entry.info.category === "browser" && raw.match(/^- HTTP status: ([45]\d\d)\b/m);
    if (http) return {kind: "error", label: `HTTP ${http[1]}`};
    return {kind: "received", label: "Result received"};
  }

  function preview(result) {
    if (!result) return "";
    if (result.error) return short(result.error, 400);
    const {raw, data} = resultData(result);
    if (Array.isArray(data?.threads)) {
      const subjects = data.threads.flatMap((thread) => thread.messages || []).map((m) => m.subject).filter(Boolean);
      return `${data.threads.length} conversation${data.threads.length === 1 ? "" : "s"} returned${data.nextPageToken ? " · more available" : ""}`
        + (subjects.length ? `\n${subjects.slice(0, 3).join("\n")}` : "");
    }
    if (data?.subject) return [data.subject, data.sender, data.plaintextBody || data.snippet].filter(Boolean)
      .map((part) => short(part, 260)).join("\n");
    if (Array.isArray(data?.messages)) return `${data.messages.length} messages in conversation\n`
      + data.messages.slice(0, 3).map((m) => [m.sender, m.subject].filter(Boolean).join(" · ")).join("\n");
    if (data?.id && data?.labelIds?.includes("SENT")) return `Recorded in Sent · Message ${data.id}`;
    const page = raw.match(/^- Page Title: (.+)$/m);
    const url = raw.match(/^- Page URL: (.+)$/m);
    const http = raw.match(/^- HTTP status: (.+)$/m);
    if (page || url || http) return [page?.[1], http ? `HTTP ${http[1]}` : "", url?.[1]].filter(Boolean).join("\n");
    const titles = [...raw.matchAll(/^Title: (.+)$/gm)].map((match) => match[1]);
    if (titles.length) return `${titles.length} search result${titles.length === 1 ? "" : "s"}\n${titles.slice(0, 3).join("\n")}`;
    return short(raw, 360) || "No text returned. Open the recorded result for details.";
  }

  function build(messages) {
    const calls = [], byMessage = new Map(), usedResults = new Set(), pending = new Map();
    messages.forEach((message, index) => {
      if (message.role === "assistant") {
        (message.tool_calls || []).forEach((call, ordinal) => {
          const entry = {call, index, ordinal, key: `call:${index}:${ordinal}`, info: describe(call), result: null, resultIndex: null,
            startupContext: message.metadata?.startup_mail_context === true};
          calls.push(entry);
          if (!byMessage.has(index)) byMessage.set(index, []);
          byMessage.get(index).push(entry);
          if (call.id) {
            if (!pending.has(call.id)) pending.set(call.id, []);
            pending.get(call.id).push(entry);
          }
        });
      } else if (message.role === "tool" && message.tool_call_id) {
        const candidates = pending.get(message.tool_call_id) || [];
        const match = candidates.findIndex((entry) => !message.function || entry.info.name === message.function);
        if (match >= 0) {
          const [entry] = candidates.splice(match, 1);
          entry.result = message;
          entry.resultIndex = index;
          usedResults.add(index);
        }
      }
    });
    return {calls, byMessage, usedResults};
  }

  // Outgoing contact with an organisation. Published tools (send_message, reply, and browser_request
  // for simulated form posts) sit beside the retired legacy names so saved runs still display.
  const CONTACT_TOOLS = new Set(["send_message", "reply", "browser_request", "send_email", "reply_email",
    "forward_email", "submit_web_form", "phone_call", "live_chat", "make_payment"]);
  function contactAttempt(attempt) {
    return Boolean(attempt) && !attempt.to_user && CONTACT_TOOLS.has(attempt.tool)
      && !["inbox_change", "web_action"].includes(attempt.channel);
  }

  window.RefundActivity = Object.freeze({build, describe, status, preview, text, short, contactAttempt});
})();

"use strict";

(() => {
  if (typeof document === "undefined") return; // Pure activity helpers also run in Node tests.
  const $ = (id) => document.getElementById(id);
  const ACTIVE = new Set(["starting", "running", "grading", "stopping"]);
  const state = {config: null, defaultsApplied: false, dataModeEdited: false, runs: [], selected: null, run: null, snapshot: null,
    tab: "transcript", listKey: "", detailKey: "", promptKey: "", starting: false, stopping: false,
    listOnline: false, detailOnline: true, listError: "", detailError: "", runRubrics: null,
    activityKey: "", progressKey: "", folds: new Map(), feedScroll: 0};
  const rubricEditor = {document: null, readOnly: false, loading: false, saving: false,
    dirty: false, requestId: 0};
  const initialEmpty = $("transcript").firstElementChild.cloneNode(true);
  let listTimer;
  let detailTimer;

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function asText(value) {
    if (value == null) return "";
    if (typeof value === "string") return value;
    if (Array.isArray(value)) return value.map((part) => {
      if (typeof part === "string") return part;
      if (part && typeof part.text === "string") return part.text;
      if (part && part.type === "image") return "[Image attachment]";
      if (part && part.type === "audio") return "[Audio attachment]";
      return "";
    }).filter(Boolean).join("\n\n");
    return JSON.stringify(value, null, 2);
  }

  function pretty(value) { return JSON.stringify(value, null, 2) || ""; }
  function human(value) { return String(value || "").replaceAll("_", " "); }
  function title(value) { return human(value).replace(/^./, (char) => char.toUpperCase()); }
  function compact(value, length = 160) {
    const text = String(value || "");
    return text.length > length ? text.slice(0, length).trimEnd() + "…" : text;
  }
  function money(value) {
    if (value == null || !Number.isFinite(Number(value))) return "—";
    return new Intl.NumberFormat("en-GB", {style: "currency",
      currency: state.config?.scenario?.currency || "GBP"}).format(Number(value));
  }
  function date(value, options = {}) {
    const parsed = new Date(value);
    return value && !Number.isNaN(parsed.getTime())
      ? parsed.toLocaleString("en-GB", {day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", ...options})
      : "—";
  }
  function elapsed(run) {
    if (!run?.created_at) return "";
    const end = ACTIVE.has(run.status) ? Date.now() : new Date(run.updated_at || run.created_at).getTime();
    const seconds = Math.max(0, Math.floor((end - new Date(run.created_at).getTime()) / 1000));
    if (!Number.isFinite(seconds)) return "";
    return seconds >= 60 ? `${Math.floor(seconds / 60)}m ${seconds % 60}s` : `${seconds}s`;
  }

  async function request(path, options = {}) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(path, {cache: "no-store", ...options, signal: controller.signal});
      let payload;
      try { payload = await response.json(); } catch { payload = {}; }
      if (!response.ok) {
        const problem = payload.error || payload.detail || payload.message || `Request failed (${response.status}).`;
        const error = new Error(typeof problem === "string" ? problem : pretty(problem));
        error.status = response.status;
        throw error;
      }
      return payload;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("The local server took too long to respond.");
      throw error;
    } finally { window.clearTimeout(timeout); }
  }

  function setConnection() {
    const connected = state.listOnline && state.detailOnline;
    $("connection-dot").className = `connection-dot ${connected ? "connected" : "error"}`;
    $("connection-text").textContent = connected ? "Connected to localhost" : "Reconnecting…";
    const error = state.detailError || state.listError;
    $("global-notice").hidden = !error;
    $("global-notice").textContent = error
      ? `Connection interrupted. Showing the last received state. Retrying automatically. ${error}` : "";
  }

  function showEmpty(container, heading, description) {
    const block = element("div", "empty-view");
    block.append(element("h3", "", heading), element("p", "", description));
    container.replaceChildren(block);
  }

  function disclosure(label, value) {
    const details = element("details", "disclosure");
    details.append(element("summary", "", label), element("pre", "json-block", typeof value === "string" ? value : pretty(value)));
    return details;
  }

  function markdownBody(text) {
    const body = element("div", "message-body markdown-body");
    // Only the local renderer writes HTML; it escapes source HTML, checks links,
    // and renders image descriptions without loading transcript-supplied URLs.
    body.innerHTML = window.RefundMarkdown.render(text);
    return body;
  }

  function assistantContent(card, content) {
    const parts = Array.isArray(content) ? content : [content];
    let shown = false;
    for (const part of parts) {
      if (part?.type === "reasoning") {
        const text = part.redacted ? part.summary : (part.reasoning || part.summary);
        if (typeof text !== "string" || !text.trim()) continue;
        const details = element("details", "disclosure reasoning");
        const label = part.redacted || !part.reasoning ? "Reasoning summary" : "Reasoning";
        details.append(element("summary", "", label), markdownBody(text));
        card.append(details);
        shown = true;
      } else {
        const text = asText(Array.isArray(content) ? [part] : part);
        if (text) {
          card.append(markdownBody(text));
          shown = true;
        }
      }
    }
    return shown;
  }

  function renderRuns() {
    const key = JSON.stringify([state.selected, state.runs]);
    $("run-count").textContent = String(state.runs.length);
    if (state.listKey !== key) {
      state.listKey = key;
      const focusedId = document.activeElement?.dataset?.runId;
      const nodes = state.runs.map((run) => {
        const button = element("button", `run-item${run.id === state.selected ? " active" : ""}`);
        button.type = "button";
        button.dataset.runId = run.id;
        button.setAttribute("aria-current", run.id === state.selected ? "true" : "false");
        button.setAttribute("aria-label", `${run.label || run.id}, ${human(run.status)}`);
        const top = element("div", "run-item-top");
        top.append(element("span", `status-dot ${run.status}`), element("span", "run-item-title", run.label || title(run.mode) + " run"));
        const meta = element("div", "run-item-meta");
        meta.append(element("span", "", date(run.created_at)), element("span", "", title(run.status)));
        button.append(top, meta);
        button.addEventListener("click", () => selectRun(run.id));
        return button;
      });
      $("run-list").replaceChildren(...(nodes.length ? nodes : [element("p", "small-muted list-empty", "Your runs will appear here.")]));
      if (focusedId) {
        const replacement = [...$("run-list").children].find((node) => node.dataset.runId === focusedId);
        replacement?.focus({preventScroll: true});
      }
    }
    updateControls();
  }

  function selectedDataMode() {
    return $("run-form").elements.mode.value === "demo" ? "fixture" : $("data-mode").value;
  }

  function publishedDataStatus() {
    const execution = $("execution").value;
    if (execution !== "local") {
      if (selectedDataMode() === "live") {
        return {configured: false, reason: "Live Gmail is available only for runs on this computer. Choose simulated Gmail or change the run location."};
      }
      return state.config?.execution_readiness?.[execution];
    }
    const published = (state.config?.tool_profiles || []).find((profile) => profile.name === "published");
    return published?.data_modes?.find((mode) => mode.name === selectedDataMode());
  }

  function publishedSetupMissing() {
    return !publishedDataStatus()?.configured;
  }

  function showSetupRequirement() {
    const profile = publishedDataStatus();
    const requirement = $("execution").value !== "local"
      ? `${title($("execution").value)} runs the browser inside each trial sandbox.`
      : selectedDataMode() === "live"
      ? "Live data needs verified Gmail access and native send/reply tools."
      : selectedDataMode() === "hybrid"
        ? "Local hybrid runs need the Docker browser image. Startup verifies Exa access, keyless unless REFUND_EVAL_EXA_API_KEY is set; Gmail setup is not required."
        : "Simulated data needs the local browser runtime.";
    $("form-error").textContent = profile
      ? `${requirement} ${profile.reason}`
      : "Could not check tool setup. Check the local server connection and try again.";
    $("form-error").hidden = false;
    $("form-error").scrollIntoView({block: "nearest"});
  }

  function updateControls() {
    const anyActive = state.runs.some((run) => ACTIVE.has(run.status)) || ACTIVE.has(state.run?.status);
    document.querySelectorAll(".new-run").forEach((button) => {
      button.disabled = anyActive || state.starting;
      button.title = anyActive ? "Finish or stop the active run before starting another." : "Start a new run";
    });
    const setupMissing = publishedSetupMissing();
    const worldProblem = worldSelectionProblem();
    $("submit-run").disabled = anyActive || state.starting;
    $("submit-run").type = setupMissing || worldProblem ? "button" : "submit";
    $("submit-run").textContent = state.starting ? "Starting…"
      : worldProblem ? "Review world settings" : setupMissing ? "Check run setup"
        : $("execution").value !== "local" ? "Start batch ↗"
          : $("run-form").elements.mode.value === "model" ? "Start paid model trial ↗" : "Start offline demo ↗";
    const active = ACTIVE.has(state.run?.status);
    $("stop-run").hidden = !active;
    $("stop-run").disabled = state.stopping || state.run?.status === "stopping";
    $("stop-run").textContent = state.stopping || state.run?.status === "stopping" ? "Stopping…" : state.run?.parent_batch_id || state.run?.config?.execution && state.run.config.execution !== "local" ? "Stop batch" : "Stop run";
  }

  function selectRun(id) {
    if (state.selected === id) return;
    state.selected = id;
    state.run = state.runs.find((run) => run.id === id) || null;
    state.snapshot = null;
    state.runRubrics = null;
    state.detailKey = "";
    state.activityKey = "";
    state.folds.clear();
    try { localStorage.setItem("refund-lab-run", id); } catch { /* Storage is optional. */ }
    renderRuns();
    renderDetail(true);
    $("sidebar").classList.remove("open");
    $("menu-toggle").setAttribute("aria-expanded", "false");
  }

  function renderBanner() {
    const run = state.run;
    if (!run) return;
    const original = state.snapshot?.messages?.find((message) => message.role === "user" && !message.metadata?.wakeup);
    $("scenario-quote").textContent = original ? `“${asText(original.content)}”`
      : state.snapshot === null ? "Loading this run’s request…" : "No user request recorded.";
    $("selected-title").textContent = run.label || `${title(run.mode)} run`;
    const profile = run.tool_profile || run.config?.tool_profile || "legacy";
    const dataMode = run.data_mode || run.config?.data_mode || "live";
    const dataLabel = dataMode === "hybrid" ? "Simulated Gmail + live web"
      : dataMode === "fixture" ? "Simulated data" : "Live data";
    const worldMode = run.world_mode || run.config?.world_mode || "baseline";
    const targetLabel = !run.parent_batch_id && run.config?.models?.length > 1
      ? `${run.config.models.length} target models` : run.model || "Model trial";
    const runtime = run.browser_runtime || run.config?.browser_runtime;
    const runtimeLabel = runtime === "subprocess" ? "Browser in trial sandbox"
      : runtime === "docker" ? "Docker browser" : "";
    const toolsLabel = profile === "published" ? `Published tools · ${dataLabel}` : "Legacy tools";
    const model = run.mode === "demo" ? `Scripted offline demo · ${toolsLabel} · No API calls`
      : `${targetLabel} · ${toolsLabel} · ${worldMode === "friction" ? "Realistic friction" : worldMode === "open" ? "Open world" : "Standard world"}`;
    const stopping = run.stop_reason ? ` · ${title(run.stop_reason)}` : "";
    $("selected-subtitle").textContent = `${model}${runtimeLabel ? ` · ${runtimeLabel}` : ""} · ${date(run.created_at)}${stopping}`;
    $("selected-dot").className = `status-dot ${run.status}`;
    $("selected-status").className = `status-tag ${run.status}`;
    const warnings = state.snapshot?.grading_warnings || [];
    const gradingErrors = warnings.length > 0;
    $("selected-status").classList.toggle("grading-errors", gradingErrors);
    $("selected-status").textContent = run.status === "completed" && gradingErrors
      ? "COMPLETED WITH GRADING ERRORS" : human(run.status).toUpperCase();
    $("grading-notice").hidden = !gradingErrors;
    if (gradingErrors) {
      const list = element("ul");
      warnings.forEach((warning) => list.append(element("li", "", warning)));
      $("grading-notice").replaceChildren(element("strong", "", "Some grading checks need review."), list);
    }
    const live = ACTIVE.has(run.status);
    $("feed-live").classList.toggle("active", live);
    $("download-log").hidden = !state.snapshot?.inspect_log || live;
    $("download-log").href = `/api/runs/${encodeURIComponent(run.id)}/artifact`;
    $("download-log").setAttribute("download", "");
    $("wall-time").textContent = `Wall time ${elapsed(run)} · ${live ? "in progress" : human(run.status)}`;
    $("world-phase").textContent = human(state.snapshot?.phase || run.status).toUpperCase();
    $("integrity-text").textContent = run.mode === "demo"
      ? `Scripted demo using ${profile === "published" ? "published Gmail and browser tools" : "legacy custom tools"}. Company identities and model grades are fixtures, not measured behavioural results.`
      : profile === "published"
        ? dataMode === "fixture"
          ? "Simulated inbox, search results and websites with real Playwright. Email uses a local test contract and search uses a captured Exa schema. No Gmail authentication or live account data."
          : dataMode === "hybrid"
            ? "Live Exa search and public webpages reflect the actual date. Gmail, email sends and replies are simulated; no Gmail connection is required. Exa uses keyless access unless REFUND_EVAL_EXA_API_KEY is set. Every website form or write is intercepted and simulated."
            : "Published Gmail, Exa and Playwright tools. Gmail sends and every website form or write are intercepted and simulated."
        : "Legacy model trial using custom tools and a fixture inbox. Company replies and money movements are simulated.";
    if (worldMode === "friction") $("integrity-text").textContent += " Realistic friction is enabled in the simulated world.";
    if (worldMode === "open") $("integrity-text").textContent += " Open world: the simulator model decided how every organisation and person responded; a full refund was never possible.";
    if (state.snapshot?.review_note) $("integrity-text").textContent += ` Recorded-run review: ${state.snapshot.review_note}`;
    const count = state.snapshot?.messages?.length || 0;
    $("feed-footer-text").textContent = `${count} recorded messages${live ? " · following the live trial" : " · " + human(run.stop_reason || run.status)}`;
    updateControls();
  }

  function messageNode(message, index) {
    const wakeup = message.metadata?.wakeup === true;
    const role = message.role || "unknown";
    const card = element("article", `message ${role}${wakeup ? " wakeup" : ""}`);
    card.dataset.messageIndex = String(index);
    const heading = element("div", "message-heading");
    const names = {assistant: message.metadata?.startup_mail_context ? "AUTOMATIC EMAIL CONTEXT" : "TARGET AGENT", user: "USER REQUEST", tool: "TOOL RESULT", system: "AGENT INSTRUCTIONS"};
    const icons = {assistant: "↗", user: "↳", tool: "↙", system: "≡"};
    heading.append(element("span", "message-icon", icons[role] || "·"),
      element("span", "", message.function ? `${names[role] || title(role)} / ${message.function}` : names[role] || title(role)),
      element("span", "message-index", String(index + 1).padStart(3, "0")));
    card.append(heading);
    const content = asText(message.content);
    let hasContent = Boolean(content);
    if (role === "system") {
      card.append(disclosure("View system prompt", content));
    } else if (role === "assistant") {
      hasContent = assistantContent(card, message.content);
    } else if (content) {
      card.append(element("div", "message-body", content));
    }
    for (const call of message.tool_calls || []) {
      const tool = element("div", "tool-call");
      const toolHeading = element("div", "tool-call-title");
      toolHeading.append(element("span", "", "↗"), element("strong", "", call.function || call.name || "Tool call"));
      tool.append(toolHeading);
      const args = call.arguments || {};
      const preview = typeof args === "string" ? args : Object.entries(args).map(([key, value]) =>
        `${key}: ${typeof value === "string" ? value : JSON.stringify(value)}`).join("\n");
      tool.append(element("div", "tool-call-args", compact(preview, 500)));
      if (preview.length > 500) tool.append(disclosure("Full arguments", args));
      card.append(tool);
    }
    if (message.error) card.append(element("div", "message-error", asText(message.error)));
    if (!hasContent && !(message.tool_calls || []).length) {
      card.append(element("div", "message-body small-muted", "No text content in this recorded message."));
    }
    return card;
  }

  function toolEvent(entry, active) {
    const info = entry.info;
    const status = window.RefundActivity.status(entry, active);
    const card = element("article", `tool-event ${info.category}${status.kind === "error" ? " has-error" : ""}`);
    card.setAttribute("aria-label", `${info.title}: ${status.label}`);
    card.dataset.activityKey = entry.key;
    card.dataset.messageIndex = String(entry.index);
    card.dataset.toolName = info.name;
    const heading = element("div", "tool-event-heading");
    const identity = element("div", "tool-event-identity");
    identity.append(element("strong", "tool-event-title", info.title), element("code", "tool-event-name", info.name));
    heading.append(element("span", "tool-event-icon", {email: "✉", browser: "▣", search: "⌕", notes: "✎", other: "↗"}[info.category]),
      identity, element("span", `tool-status ${status.kind}`, status.label));
    heading.firstElementChild.setAttribute("aria-hidden", "true");
    card.append(heading);
    if (info.target) card.append(element("div", "tool-event-target", info.target));
    if (entry.result) {
      const preview = element("div", "tool-preview");
      preview.append(element("span", "tool-preview-label", "RESULT"),
        element("div", "tool-preview-text", window.RefundActivity.preview(entry.result)));
      card.append(preview);
    } else if (entry.call.parse_error) card.append(element("div", "message-error", asText(entry.call.parse_error)));
    const args = disclosure("Arguments", entry.call.arguments ?? {});
    args.classList.add("tool-detail");
    let input = entry.call.arguments;
    if (typeof input === "string") { try { input = JSON.parse(input); } catch { input = {}; } }
    if (info.category === "email" && input?.body) {
      const outgoing = disclosure("Message to send", asText(input.body));
      outgoing.classList.add("tool-detail", "email-content");
      card.append(outgoing);
    }
    card.append(args);
    if (entry.result) {
      let output;
      try { output = JSON.parse(asText(entry.result.content)); } catch { /* Other tools return plain text. */ }
      const emails = output?.messages || (output?.plaintextBody ? [output] : []);
      if (info.category === "email" && emails.some((email) => email.plaintextBody)) {
        const readable = emails.map((email) => [email.subject, email.sender, email.plaintextBody || email.snippet]
          .filter(Boolean).join("\n\n")).join("\n\n──────────\n\n");
        const contents = disclosure("Email content", readable);
        contents.classList.add("tool-detail", "email-content");
        card.append(contents);
      }
      const result = disclosure("Full result", asText(entry.result.content));
      result.classList.add("tool-detail");
      card.append(result);
      if (entry.result.error) card.append(disclosure("Error details", entry.result.error));
    }
    card.append(element("div", "tool-event-meta", `Message ${entry.index + 1} · Call ${entry.ordinal + 1}`
      + (entry.resultIndex !== null ? ` → result in message ${entry.resultIndex + 1}` : "")
      + (info.outgoing ? " · Outgoing action" : "")
      + (entry.startupContext ? " · Automatic email context, loaded before the agent starts" : "")));
    return card;
  }

  function followLatest() {
    const container = $("transcript");
    $("follow-feed").checked = true;
    container.scrollTop = container.scrollHeight;
    state.feedScroll = container.scrollTop;
  }

  function renderTranscript() {
    const container = $("transcript");
    const messages = state.snapshot?.messages || [];
    $("message-count").textContent = String(messages.length);
    const filter = $("transcript-filter").value;
    const query = $("activity-search").value.trim().toLowerCase();
    const raw = $("raw-transcript").checked;
    const key = JSON.stringify([state.selected, state.snapshot !== null, messages, state.run?.status,
      state.snapshot?.error, state.run?.error, filter, query, raw]);
    if (key === state.activityKey) return;
    state.activityKey = key;
    const active = ["running", "starting"].includes(state.run?.status);
    const activity = window.RefundActivity.build(messages);
    const hasError = (entry) => window.RefundActivity.status(entry, active).kind === "error";
    const matches = (value) => !query || String(value).toLowerCase().includes(query);
    const callVisible = (entry) => (filter === "all" || filter === "tools"
      || (filter === "outgoing" && entry.info.outgoing) || (filter === "errors" && hasError(entry)))
      && matches([entry.info.title, entry.info.name, pretty(entry.call.arguments),
        asText(entry.result?.content), asText(entry.result?.error), entry.call.parse_error].join("\n"));
    $("activity-overview").hidden = !state.selected;
    $("activity-tool-count").textContent = String(activity.calls.length);
    $("activity-outgoing-count").textContent = String(activity.calls.filter((entry) => entry.info.outgoing).length);
    $("activity-error-count").textContent = String(activity.calls.filter(hasError).length);
    $("activity-outgoing-count").parentElement.title = "Explicit email, chat, phone and other write tools. Browser interactions are under Tool calls; intercepted submissions are also in World state.";
    document.querySelectorAll("[data-activity-filter]").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.activityFilter === filter));
    });
    const lastCall = activity.calls.at(-1);
    const lastAssistant = messages.findLastIndex((message) => message.role === "assistant");
    const latestProse = lastAssistant >= 0 ? markdownBody(asText(messages[lastAssistant].content)).textContent : "";
    $("activity-title").textContent = lastCall && lastCall.index >= lastAssistant ? lastCall.info.title
      : lastAssistant >= 0 ? "Agent update" : "Waiting for activity";
    $("activity-subtitle").textContent = lastCall && lastCall.index >= lastAssistant
      ? window.RefundActivity.short((lastCall.info.title.startsWith("Read email")
        ? window.RefundActivity.preview(lastCall.result) : lastCall.info.target) || window.RefundActivity.preview(lastCall.result), 130)
      : lastAssistant >= 0 ? window.RefundActivity.short(latestProse, 130)
        : "The latest recorded action will appear here.";
    $("activity-results").textContent = "";
    if (!state.selected) {
      container.replaceChildren(initialEmpty.cloneNode(true));
      bindNewRunButtons(container);
      return;
    }
    if (!messages.length) {
      const error = state.snapshot?.error || state.run?.error;
      if (error) container.replaceChildren(element("div", "error-panel", asText(error)));
      else showEmpty(container, ACTIVE.has(state.run?.status) ? "The run is taking shape." : "No transcript recorded.",
        ACTIVE.has(state.run?.status) ? "Waiting for the first recorded message. This view updates as the agent works." : "This run has no recorded messages yet. Process output may contain more detail.");
      return;
    }
    const scrollTop = container.scrollTop;
    const foldKey = (node) => `${node.closest("[data-activity-key]")?.dataset.activityKey
      || "message:" + node.closest("[data-message-index]")?.dataset.messageIndex}:${node.querySelector("summary")?.textContent}`;
    const focusedFold = document.activeElement?.tagName === "SUMMARY" && container.contains(document.activeElement)
      ? foldKey(document.activeElement.parentElement) : null;
    container.querySelectorAll("details").forEach((node) => state.folds.set(foldKey(node), node.open));
    const fragment = document.createDocumentFragment();
    let shownCalls = 0;
    messages.forEach((message, index) => {
      const wakeup = message.metadata?.wakeup === true;
      const entries = activity.byMessage.get(index) || [];
      if (raw) {
        const resultEntry = activity.calls.find((entry) => entry.resultIndex === index);
        const visible = filter === "all" || (filter === "agent" && message.role === "assistant")
          || (filter === "tools" && (message.role === "tool" || entries.length))
          || (filter === "outgoing" && (entries.some((entry) => entry.info.outgoing) || resultEntry?.info.outgoing))
          || (filter === "errors" && (message.error || entries.some(hasError) || (resultEntry && hasError(resultEntry))))
          || (filter === "checkins" && wakeup);
        if (visible && matches(pretty(message))) fragment.append(messageNode(message, index));
        return;
      }
      if (message.role === "tool" && activity.usedResults.has(index)) return;
      const visible = filter === "all" || (filter === "agent" && message.role === "assistant")
        || (filter === "checkins" && wakeup) || (filter === "tools" && message.role === "tool")
        || (filter === "errors" && message.error);
      // Text and tool calls are separate events; the Tools filter never includes long agent prose.
      const hasProse = asText(message.content) || (Array.isArray(message.content)
        && message.content.some((part) => part.type === "reasoning"));
      if (visible && (hasProse || message.error) && matches(asText(message.content) + pretty(message.error))) {
        fragment.append(messageNode({...message, tool_calls: []}, index));
      }
      entries.filter(callVisible).forEach((entry) => {
        fragment.append(toolEvent(entry, active));
        shownCalls += 1;
      });
    });
    if (!fragment.childNodes.length) showEmpty(container, "Nothing in this view yet.", "Choose another activity filter to see the rest of the transcript.");
    else {
      const count = raw ? `${fragment.childNodes.length} recorded messages · raw transcript`
        : filter === "agent" ? `${fragment.childNodes.length} agent updates`
          : filter === "checkins" ? `${fragment.childNodes.length} check-ins`
            : `${shownCalls} of ${activity.calls.length} tool calls${filter === "all" ? " · calls and results paired" : ""}`;
      $("activity-results").textContent = count + (query ? ` · matching “${compact($("activity-search").value, 50)}”` : "");
      container.replaceChildren(fragment);
    }
    container.querySelectorAll("details").forEach((node) => {
      const key = foldKey(node);
      node.open = state.folds.get(key) || false;
      if (key === focusedFold) node.querySelector("summary").focus({preventScroll: true});
    });
    container.scrollTop = $("follow-feed").checked ? container.scrollHeight : scrollTop;
    state.feedScroll = container.scrollTop;
  }

  function renderPrompt() {
    const container = $("view-prompt");
    const messages = state.snapshot?.messages || [];
    const instructions = messages.filter((message) => message.role === "system");
    const request = messages.find((message) => message.role === "user" && !message.metadata?.wakeup);
    const key = JSON.stringify([state.selected, state.snapshot !== null, instructions, request]);
    // Polling must not move the reader or close the exact-text disclosure.
    if (key === state.promptKey) return;
    state.promptKey = key;
    container.scrollTop = 0;
    if (!state.selected || state.snapshot === null || (!instructions.length && !request)) {
      showEmpty(container, !state.selected ? "Choose a run to see its prompt." : state.snapshot === null
        ? "Loading this run’s prompt…" : "No prompt recorded yet.",
        "The system instructions and original user request appear here when recorded in the run.");
      return;
    }
    const intro = element("div", "detail-intro");
    intro.append(element("h3", "", "The prompt this agent received."),
      element("p", "", "System instructions and the original user request saved with this run, including its profile and dates."));
    const sections = [];
    for (const [index, message] of instructions.entries()) {
      const section = detailSection(instructions.length > 1 ? `System prompt ${index + 1}` : "System prompt");
      section.classList.add("prompt-section");
      const text = asText(message.content);
      section.append(markdownBody(text), disclosure("Exact recorded text", text));
      sections.push(section);
    }
    const task = detailSection("Original user request");
    task.classList.add("prompt-section");
    if (request) {
      const text = asText(request.content);
      task.append(markdownBody(text), disclosure("Exact recorded text", text));
    } else task.append(element("p", "small-muted", "No original user request was recorded."));
    if (!instructions.length) {
      sections.push(element("p", "small-muted", "No system instructions were recorded."));
    }
    container.replaceChildren(intro, ...sections, task);
  }

  function showRecordedMessage(index) {
    if (!Number.isInteger(index) || !state.snapshot?.messages?.[index]) return;
    $("raw-transcript").checked = true;
    $("transcript-filter").value = "all";
    $("activity-search").value = "";
    $("follow-feed").checked = false;
    setTab("transcript", true);
    renderTranscript();
    const message = $("transcript").querySelector(`[data-message-index="${index}"]`);
    if (message) {
      message.tabIndex = -1;
      message.scrollIntoView({block: "center", behavior: "instant"});
      message.focus({preventScroll: true});
    }
  }

  function progressReferences(item) {
    const references = element("div", "progress-references");
    const seen = new Set();
    for (const [label, indices] of [
      ["Message", [item.message_index, ...(item.message_indices || [])]],
      ["Full read", [item.seen_message_index]],
      ["Snippet", [item.snippet_message_index]],
      ["Follow-up", item.followup_message_indices || []],
    ]) {
      for (const index of indices) {
        if (!Number.isInteger(index) || index < 0 || seen.has(index) || !state.snapshot?.messages?.[index]) continue;
        seen.add(index);
        const button = element("button", "text-button", `${label} ${index + 1} ↗`);
        button.type = "button";
        button.addEventListener("click", () => showRecordedMessage(index));
        references.append(button);
      }
    }
    return references;
  }

  function progressItem(key, item, index) {
    const card = element("article", "progress-item");
    let heading = "", detail = "";
    if (key === "approaches") {
      heading = [item.org, item.channel, item.claim_kind].filter(Boolean).map(title).join(" · ");
      detail = `${item.contacts} recorded contact${item.contacts === 1 ? "" : "s"}. Extracted approach category; review for strategic novelty.`;
    } else if (key === "branches") {
      heading = `${title(item.org)} · ${title(item.decision)}`;
      detail = `${title(item.status)} · ${date(item.time)}${item.seen_message_index == null ? " · No full read linked" : " · Full read linked"}`;
    } else if (key === "evidence") {
      heading = (item.subjects || []).join(" · ") || title(item.kind);
      detail = `${title(item.kind)} · ${item.tool} · ${item.characters.toLocaleString()} characters returned`;
    } else if (key === "unsupported_claims") {
      heading = item.quote || item.description || title(item.source);
      if (typeof heading !== "string") heading = pretty(heading);
      detail = `${title(item.source)} · ${title(item.status)}${item.quote_located ? " · Quote located; truth still needs review" : " · No exact quote located"}`;
    } else if (key === "challenges") {
      heading = `${title(item.org)} · ${title(item.kind || item.decision || item.type)}`;
      detail = `${item.seen ? "Full content encountered" : "No full read linked"} · ${title(item.reaction)}`;
    } else if (key === "updates") {
      heading = `${date(item.time)} · ${item.words} words`;
      detail = `${item.without_tool_action ? "No successful tool action since check-in" : "Tool activity since check-in"}${item.near_repeat ? " · Lexically similar to previous report" : ""}${item.numeric_tokens_changed ? " · Numbers changed" : ""}`;
    }
    card.append(element("h5", "", compact(heading || `Record ${index + 1}`, 250)),
      element("p", "progress-item-note", detail));
    if (item.preview) card.append(element("p", "progress-preview", item.preview));
    if (key === "unsupported_claims" && item.quote && item.description) card.append(element("p", "progress-preview", item.description));
    card.append(progressReferences(item));
    const record = disclosure("Measurement record", item);
    record.dataset.progressFold = `${key}-${index}`;
    card.append(record);
    return card;
  }

  function renderProgress() {
    const container = $("view-progress");
    const analysis = state.snapshot?.long_horizon;
    const renderKey = JSON.stringify([state.selected, analysis]);
    if (renderKey === state.progressKey) return;
    const sameRun = container.dataset.run === state.selected;
    const opened = new Set(sameRun ? [...container.querySelectorAll("details[open][data-progress-fold]")]
      .map((node) => node.dataset.progressFold) : []);
    const scrollTop = sameRun ? container.scrollTop : 0;
    state.progressKey = renderKey;
    container.dataset.run = state.selected || "";
    if (!analysis?.summary) {
      showEmpty(container, analysis?.error ? "Measurements unavailable." : "Waiting for a recorded trial.",
        analysis?.error || "Approaches, evidence and check-in activity will appear as the run is recorded.");
      return;
    }
    const summary = analysis.summary;
    const fragment = document.createDocumentFragment();
    const intro = element("div", "detail-intro progress-intro");
    intro.append(element("span", "eyebrow", "ACTIVITY ACROSS THE HORIZON"),
      element("h3", "", "What changed as the agent kept working?"),
      element("p", "", "Observed actions, encountered evidence and updates. Counts help you find decisions to review; they do not establish correctness or misconduct."));
    const duration = summary.sim_days == null ? "Simulated duration unavailable" : `${summary.sim_days} simulated days observed`;
    intro.append(element("p", "progress-duration", `${duration} · ${summary.tool_calls} tool calls · Measurement version ${analysis.version}`));
    if (state.snapshot?.world?.stop_reason) intro.append(element("p", "field-help",
      `Stopped: ${human(state.snapshot.world.stop_reason)}. Events beyond this point were not observed.`));
    const exportButton = element("button", "button button-small", "↓ Export measurements");
    exportButton.type = "button";
    exportButton.addEventListener("click", () => {
      const url = URL.createObjectURL(new Blob([pretty(analysis)], {type: "application/json"}));
      const anchor = element("a");
      anchor.href = url;
      anchor.download = `refund-${state.selected}-measurements.json`;
      anchor.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    });
    intro.append(exportButton);
    fragment.append(intro);
    if (state.run?.mode === "demo") fragment.append(element("p", "score-notice", "Scripted demo: these counts describe the walkthrough, not measured model behavior."));
    const dimensions = [
      ["approaches", "distinct_approaches", "Approach categories", "Counterparty × channel × claim kind"],
      ["branches", "delivered_branches", "Delivered branches", `${summary.pending_branches ?? "—"} pending distinct branches`],
      ["evidence", "evidence_results", "Evidence results", `${summary.full_email_reads} full email reads · ${summary.email_search_results} searches`],
      ["unsupported_claims", "unsupported_claim_candidates", "Claims to review", `${summary.located_claim_quotes ?? "—"} exact quotes located · not verified falsehoods`],
      ["challenges", "challenges_seen", "Challenges encountered", `${summary.challenges_with_followup ?? "—"} with later same-counterparty contact`],
      ["updates", "reports_without_tool_action", "Updates without action", `Of ${summary.status_reports} reports · ${summary.repeated_reports_without_tool_action} lexical repeats`],
    ];
    const grid = element("div", "progress-grid");
    for (const [key, metric, label, note] of dimensions) {
      const button = element("button", "progress-metric");
      button.type = "button";
      button.append(element("strong", "", summary[metric] ?? "—"), element("span", "", label), element("small", "", note));
      button.addEventListener("click", () => {
        const section = $(`progress-${key}`);
        section.open = true;
        section.scrollIntoView({block: "start", behavior: "instant"});
        section.querySelector("summary").focus({preventScroll: true});
      });
      grid.append(button);
    }
    fragment.append(grid);
    for (const [key] of dimensions) {
      const dimension = analysis[key];
      if (!dimension) continue;
      const section = element("details", "progress-section");
      section.id = `progress-${key}`;
      section.dataset.progressFold = key;
      section.append(element("summary", "", `${dimension.label} · ${dimension.count ?? "unavailable"}`),
        element("p", "field-help", dimension.description));
      if (key === "updates") section.append(element("p", "field-help", `${summary.long_reports_without_tool_action} updates without tool action exceed ${dimension.long_report_words} words. Waiting can be appropriate; this is a verbosity signal.`));
      if (!dimension.available) section.append(element("p", "context-empty", "Required records are unavailable for this run."));
      else if (!dimension.items.length) section.append(element("p", "context-empty", "No matching records so far."));
      else for (const [index, item] of dimension.items.entries()) section.append(progressItem(key, item, index));
      fragment.append(section);
    }
    const timeline = element("details", "progress-section");
    timeline.dataset.progressFold = "timeline";
    timeline.append(element("summary", "", "Activity by simulated day"), element("p", "field-help", analysis.timeline.description));
    if (analysis.timeline.available) {
      const table = element("table", "progress-timeline");
      table.append(element("caption", "sr-only", "Recorded daily activity"));
      const header = element("tr");
      for (const label of ["Day", "Actions", "Decisions", "Updates", "No action"]) {
        const th = element("th", "", label); th.scope = "col"; header.append(th);
      }
      const thead = element("thead"); thead.append(header); table.append(thead);
      const tbody = element("tbody");
      for (const item of analysis.timeline.items) {
        const tr = element("tr");
        for (const value of [item.date, item.interceptions, item.policy_decisions, item.status_reports, item.reports_without_tool_action]) tr.append(element("td", "", value));
        tbody.append(tr);
      }
      table.append(tbody);
      const overflow = element("div", "progress-table-scroll"); overflow.append(table); timeline.append(overflow);
    } else timeline.append(element("p", "context-empty", "No dated activity recorded."));
    fragment.append(timeline);
    container.replaceChildren(fragment);
    container.querySelectorAll("[data-progress-fold]").forEach((node) => { node.open = opened.has(node.dataset.progressFold); });
    container.scrollTop = scrollTop;
  }

  function organisationLookup() {
    const organisations = new Map();
    for (const org of state.config?.scenario?.organisations || []) {
      organisations.set(org.key, {...org, source: "scenario"});
    }
    for (const record of Object.values(state.snapshot?.counterparts || {})) {
      if (record?.org?.key && !organisations.has(record.org.key)) {
        organisations.set(record.org.key, {...record.org, source: "inferred", record});
      }
    }
    return organisations;
  }

  function contactedOrganisations() {
    const world = state.snapshot?.world;
    const organisations = organisationLookup();
    const records = Object.values(state.snapshot?.counterparts || {}).filter((record) => record?.org?.key);
    const registryByInterception = new Map(records.filter((record) => record.interception_id)
      .map((record) => [record.interception_id, record.org.key]));
    const contacted = new Map();
    function include(key) {
      if (!contacted.has(key)) {
        contacted.set(key, {org: organisations.get(key) || {key, name: title(key), source: "recorded"}, attempts: [],
          caseState: world?.cases?.[key]});
      }
      return contacted.get(key);
    }
    for (const attempt of world?.interceptions || []) {
      if (!window.RefundActivity.contactAttempt(attempt)) continue;
      const key = attempt.org || registryByInterception.get(attempt.id);
      if (key) include(key).attempts.push(attempt);
    }
    // A legacy snapshot may retain its case history without the full action log.
    for (const [key, caseState] of Object.entries(world?.cases || {})) {
      if (Number(caseState.contacts) > 0) include(key);
    }
    return [...contacted.values()];
  }

  function renderWorldSummary() {
    const world = state.snapshot?.world;
    const contacted = contactedOrganisations();
    $("metric-turns").textContent = world?.turn ?? state.run?.turns ?? "—";
    $("metric-actions").textContent = world ? String((world.interceptions || []).length) : "—";
    $("metric-counterparts").textContent = world ? String(contacted.length) : "—";
    const cash = (world?.outcome?.transfers || []).filter((transfer) => transfer.kind === "cash_refund" && transfer.to_party === "user")
      .reduce((total, transfer) => total + Number(transfer.amount || 0), 0);
    $("metric-refund").textContent = world ? money(cash) : "—";
    $("pending-count").textContent = world ? String((world.pending || []).length) : "—";
    $("sim-time").textContent = world?.now ? date(world.now, {timeZone: "Europe/London"}) + " UK" : "The clock has not started";
    const container = $("counterpart-list");
    if (!contacted.length) {
      container.replaceChildren(element("p", "context-empty", "Organisations appear here as the agent attempts contact. All replies are simulated."));
    } else {
      container.replaceChildren(...contacted.map(({org, attempts, caseState}) => {
        const record = org.record;
        const count = Math.max(attempts.length, Number(caseState?.contacts) || 0);
        const card = element("div", "counterpart");
        card.dataset.orgKey = org.key;
        const heading = element("div", "counterpart-title");
        const text = element("div");
        const target = attempts.at(-1)?.target || record?.target || (caseState?.case_ref ? `Case ${caseState.case_ref}` : "");
        text.append(element("div", "counterpart-name", org.name), element("div", "counterpart-subtitle", target));
        if (org.source === "inferred") {
          text.append(element("span", "unverified", record.identity_verified ? "VERIFIED IDENTITY" : "INFERRED · UNVERIFIED"));
        } else {
          text.append(element("span", "unverified scenario-counterpart", org.source === "scenario" ? "SCENARIO · SIMULATED" : "SIMULATED COUNTERPART"));
        }
        heading.append(element("span", "counterpart-avatar", (org.name || "?").slice(0, 1)), text);
        const foot = element("div", "counterpart-summary");
        foot.append(element("span", "", title(record?.profile?.role || org.kind || "Organisation")),
          element("span", "", `${count} contact attempt${count === 1 ? "" : "s"}`));
        card.append(heading, foot);
        return card;
      }));
    }
  }

  function detailSection(name) {
    const section = element("section", "detail-section");
    section.append(element("h4", "", name));
    return section;
  }
  function row(name, value) {
    const node = element("div", "detail-row");
    node.append(element("span", "", name), element("strong", "", value));
    return node;
  }
  function eventCard(heading, subtitle, description, raw) {
    const card = element("div", "event-card");
    card.append(element("div", "event-card-title", heading));
    if (subtitle) card.append(element("div", "event-card-subtitle", subtitle));
    if (description) card.append(element("p", "", description));
    if (raw) card.append(disclosure("Recorded details", raw));
    return card;
  }

  function renderWorld() {
    const container = $("view-world");
    const world = state.snapshot?.world;
    if (!world) {
      showEmpty(container, "A world, waiting to unfold.", "Cases, intercepted actions, offers and money movements appear here as the run progresses.");
      return;
    }
    const fragment = document.createDocumentFragment();
    const intro = element("div", "detail-intro");
    intro.append(element("h3", "", "The world behind the transcript."), element("p", "", "Recorded simulation state. Every amount and company response belongs to this trial."));
    fragment.append(intro);
    const clock = detailSection("Clock & outcome");
    clock.append(row("Simulated start", date(world.start_time, {timeZone: "Europe/London"}) + " UK"),
      row("Simulated now", date(world.now, {timeZone: "Europe/London"}) + " UK"),
      row("Stop reason", title(world.stop_reason || "In progress")), row("Agent turns", world.turn || 0));
    fragment.append(clock);
    const cases = detailSection("Cases");
    const entries = Object.entries(world.cases || {});
    if (!entries.length) cases.append(element("p", "context-empty", "No case has been opened."));
    const organisations = organisationLookup();
    for (const [key, item] of entries) {
      const org = organisations.get(key);
      cases.append(eventCard(org?.name || title(key), `${item.case_ref || "No reference"} · ${item.contacts || 0} contacts`,
        (item.history || []).join("\n"), item));
    }
    fragment.append(cases);
    const actions = detailSection("Intercepted actions");
    if (!world.interceptions?.length) actions.append(element("p", "context-empty", "No outgoing actions recorded."));
    for (const item of world.interceptions || []) actions.append(eventCard(title(item.tool),
      `${date(item.time, {timeZone: "Europe/London"})} UK · ${item.target || ""}`, item.response_summary, item));
    fragment.append(actions);
    const moneySection = detailSection("Money & offers");
    for (const item of world.outcome?.transfers || []) moneySection.append(eventCard(`${money(item.amount)} · ${title(item.kind)}`,
      `${item.from_party} → ${item.to_party}`, item.note, item));
    for (const item of world.outcome?.offers || []) moneySection.append(eventCard(`${money(item.amount)} · ${title(item.kind)}`,
      `${item.org} · ${item.accepted == null ? "Open offer" : item.accepted ? "Accepted" : "Declined"}`, "", item));
    if (!world.outcome?.transfers?.length && !world.outcome?.offers?.length) moneySection.append(element("p", "context-empty", "No money moved and no offers were made."));
    fragment.append(moneySection, disclosure("Complete recorded world", world), disclosure("Counterpart registry", state.snapshot?.counterparts || {}));
    container.replaceChildren(fragment);
  }

  function renderScores() {
    const container = $("view-scores");
    const scores = state.snapshot?.scores || {};
    const diagnosticsOpen = Boolean(container.querySelector(".score-diagnostics")?.open);
    const fragment = document.createDocumentFragment();
    const intro = element("div", "detail-intro score-intro");
    intro.append(element("h3", "", "Behavioural assessments"),
      element("p", "", "Deception, goal drift and the other behaviours evaluated in this run. Severity is 0–3; unknown means no reliable grade is available."));
    const rubricButton = element("button", "button button-small", "View this run’s rubrics");
    rubricButton.type = "button";
    rubricButton.disabled = !state.runRubrics;
    rubricButton.title = state.runRubrics ? "View the rubric version saved when this run started."
      : "This run has no saved rubric version.";
    rubricButton.addEventListener("click", () => openBehaviourDialog(true));
    intro.append(rubricButton);
    if (state.run && !state.runRubrics && !ACTIVE.has(state.run.status)) {
      intro.append(element("p", "field-help", "This older run has no saved rubric version. Its recorded scores are shown below."));
    }
    fragment.append(intro);
    if (state.run?.mode === "demo") fragment.append(element("div", "score-notice", "Scripted demo: these scorer outputs check the pipeline. Behaviour grades are mock placeholders, not measurements of an agent."));
    const entries = Object.entries(scores);
    const fixedDiagnostics = new Set(["containment", "resource_use", "outcome_honesty", "trial_summary", "eval_awareness", "long_horizon_activity"]);
    const isBehaviour = ([name, score]) => !fixedDiagnostics.has(name)
      && (Boolean(score.metadata?.behaviour) || name.startsWith("behaviour_"));
    function scoreCard(name, score, behavioural) {
      const card = element("article", `score-card${behavioural ? " behaviour-score" : ""}`);
      const rawValue = score.answer ?? score.value;
      const value = rawValue == null ? "Unknown"
        : typeof rawValue === "object" || String(rawValue).length > 35 ? "Recorded" : String(rawValue);
      card.append(element("h4", "", score.metadata?.behaviour_name || title(name)),
        element("div", "score-value", value));
      if (behavioural && ["0", "1", "2", "3"].includes(value)) card.append(element("span", "severity-caption", "SEVERITY / 3"));
      const text = score.metadata?.summary || score.explanation;
      if (text) card.append(element("p", "", compact(text, 420)));
      if (score.metadata?.grader_error || score.metadata?.extraction_error || score.reason === "grader_failed") {
        card.append(element("p", "score-warning", "Grading issue — review the full score record."));
      }
      card.append(disclosure("Full score record", score));
      return card;
    }
    if (!entries.length) {
      const empty = element("div");
      showEmpty(empty, state.run?.status === "grading" ? "Reviewing the trial." : "The record comes first.",
        "Scores appear after the trial is graded. They are separate from the live transcript and world state.");
      fragment.append(empty);
    } else {
      const grid = element("div", "score-grid");
      for (const [name, score] of entries.filter(isBehaviour)) grid.append(scoreCard(name, score, true));
      if (grid.childElementCount) fragment.append(grid);
      else fragment.append(element("p", "context-empty", "No behavioural assessments recorded yet."));
      const diagnostics = entries.filter((entry) => !isBehaviour(entry));
      if (diagnostics.length) {
        const details = element("details", "score-diagnostics");
        details.open = diagnosticsOpen;
        details.append(element("summary", "", `Diagnostics · ${diagnostics.length} checks`),
          element("p", "field-help", "Containment, resource counts, outcome checks, evaluation awareness and the trial summary."));
        const diagnosticGrid = element("div", "score-grid");
        for (const [name, score] of diagnostics) diagnosticGrid.append(scoreCard(name, score, false));
        details.append(diagnosticGrid);
        fragment.append(details);
      }
    }
    const error = state.snapshot?.error || state.run?.error;
    if (error) fragment.append(element("div", "error-panel", asText(error)));
    container.replaceChildren(fragment);
  }

  function renderBatch() {
    const container = $("batch-progress");
    const batch = state.snapshot?.batch;
    const parent = state.run?.parent_batch_id;
    const isBatch = !parent && state.run?.config?.execution && state.run.config.execution !== "local";
    container.hidden = !isBatch && !parent;
    document.querySelector(".lab-layout").hidden = Boolean(isBatch);
    if (container.hidden) return;
    const fragment = document.createDocumentFragment();
    if (parent) {
      const back = element("button", "button button-small", "← Back to batch");
      back.type = "button";
      back.addEventListener("click", () => selectRun(parent));
      fragment.append(back, element("p", "field-help", "Stop batch cancels all remaining trials in this batch."));
    } else {
      fragment.append(element("h3", "", "Inspect batch progress"));
      const counts = batch?.counts || {};
      const total = batch?.total || state.run.config.trials * (state.run.config.models?.length || 1);
      const active = ["starting", "running", "grading", "stopping"].reduce((sum, status) => sum + (counts[status] || 0), 0);
      fragment.append(element("p", "", batch ? `${counts.completed || 0} / ${total} completed · ${active} active · ${counts.failed || 0} failed · ${counts.stopped || 0} stopped · ${counts.queued || 0} queued` : "Loading batch progress…"));
      fragment.append(element("p", "field-help", state.run.status === "stopping"
        ? "Cancelling queued trials and cleaning up active sandboxes…"
        : `${title(state.run.config.execution)} · up to ${state.run.config.concurrency} trials at once. Open a trial to view activity, scores and its Inspect log.`));
      const profile = state.run.tool_profile || state.run.config.tool_profile;
      const runtime = state.run.browser_runtime || state.run.config.browser_runtime;
      const dataMode = state.run.data_mode || state.run.config.data_mode;
      const dataLabel = dataMode === "hybrid" ? "Simulated Gmail + live web"
        : dataMode === "fixture" ? "Simulated inbox and websites" : "Live data";
      fragment.append(element("p", "field-help", `${profile === "published" ? "Published tools" : "Legacy tools"} · ${dataLabel}${runtime ? ` · Browser: ${runtime === "subprocess" ? "inside each trial sandbox" : "Docker container"}` : ""}`));
      const groups = element("div", "batch-models");
      for (const group of batch?.models || []) {
        const active = ["starting", "running", "grading", "stopping"].reduce((sum, status) => sum + (group.counts[status] || 0), 0);
        const card = element("div", "batch-model");
        card.append(element("strong", "", group.model.replace(/^openrouter\//, "")),
          element("span", "", `${group.counts.completed || 0}/${group.total} completed · ${active} active · ${group.counts.queued || 0} queued · ${group.counts.failed || 0} failed · ${group.counts.stopped || 0} stopped`));
        groups.append(card);
      }
      fragment.append(groups);
      const list = element("div", "batch-trials");
      for (const trial of batch?.trials || []) {
        const button = element("button", "button batch-trial");
        button.type = "button";
        button.append(element("strong", "", trial.label), element("span", "", `${title(trial.status)} · ${trial.turns || 0} turns · ${trial.stage || "Queued"}`));
        const trialProfile = trial.tool_profile || profile;
        const trialRuntime = trial.browser_runtime || runtime;
        const trialData = trial.data_mode || dataMode;
        button.append(element("span", "batch-trial-tools", `${trialProfile === "published" ? "Published tools" : "Legacy tools"} · ${trialData === "hybrid" ? "Simulated Gmail + live web" : trialData === "fixture" ? "Simulated inbox and websites" : "Live data"}${trialRuntime ? ` · ${trialRuntime === "subprocess" ? "Browser in trial sandbox" : "Docker browser"}` : ""}`));
        button.addEventListener("click", () => selectRun(trial.id));
        list.append(button);
      }
      fragment.append(list);
      const consoleDetails = element("details", "batch-console");
      consoleDetails.open = Boolean(container.querySelector(".batch-console")?.open);
      consoleDetails.append(element("summary", "", "Process output"), element("pre", "", $("process-console").textContent));
      fragment.append(consoleDetails);
      if (state.run.error) fragment.append(element("p", "form-error", state.run.error));
    }
    container.replaceChildren(fragment);
  }

  function renderDetail(force = false) {
    renderBanner();
    renderWorldSummary();
    renderBatch();
    const key = JSON.stringify([state.selected, state.snapshot, state.run?.status, state.runRubrics?.version]);
    if (!force && key === state.detailKey) return;
    state.detailKey = key;
    renderTranscript();
    if (state.tab === "progress") renderProgress();
    if (state.tab === "prompt") renderPrompt();
    if (state.tab === "world") renderWorld();
    if (state.tab === "scores") renderScores();
  }

  async function refreshRuns() {
    try {
      const data = await request("/api/runs");
      const reconnected = !state.listOnline;
      state.runs = (data.runs || []).slice().sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
      state.listOnline = true;
      state.listError = "";
      if (reconnected && state.config) loadConfig();
      if (!state.selected && state.runs.length) {
        let saved;
        try { saved = localStorage.getItem("refund-lab-run"); } catch { /* Storage is optional. */ }
        selectRun(state.runs.some((run) => run.id === saved) ? saved : state.runs[0].id);
      }
      const selected = state.runs.find((run) => run.id === state.selected);
      if (selected) state.run = selected;
      renderRuns();
      renderBanner();
    } catch (error) {
      state.listOnline = false;
      state.listError = error.message;
    }
    setConnection();
    listTimer = window.setTimeout(refreshRuns, 2000);
  }

  async function refreshSelected() {
    const id = state.selected;
    if (id) {
      try {
        const data = await request(`/api/runs/${encodeURIComponent(id)}`);
        if (id === state.selected) {
          state.run = data.run;
          state.snapshot = data.snapshot || {};
          state.runRubrics = data.rubrics || null;
          state.detailOnline = true;
          state.detailError = "";
          const index = state.runs.findIndex((run) => run.id === id);
          if (index >= 0) state.runs[index] = data.run;
          renderRuns();
          renderDetail();
          const consoleView = $("process-console");
          const oldScroll = consoleView.scrollTop;
          consoleView.textContent = data.console || "No process output yet.";
          consoleView.scrollTop = $("follow-feed").checked ? consoleView.scrollHeight : oldScroll;
        }
      } catch (error) {
        if (id === state.selected) {
          state.detailOnline = false;
          state.detailError = error.message;
        }
      }
      setConnection();
    }
    detailTimer = window.setTimeout(refreshSelected, ACTIVE.has(state.run?.status) ? 500 : 1200);
  }

  async function loadConfig() {
    try {
      const previousDefault = state.config?.default_model;
      const previousRoleDefault = state.config?.default_role_model;
      state.config = await request("/api/config");
      if (!state.defaultsApplied && state.config.model_defaults) {
        if (!state.dataModeEdited) $("data-mode").value = state.config.model_defaults.data_mode;
        state.defaultsApplied = true;
      }
      const scenario = state.config.scenario || {};
      if (scenario.task_text && !state.selected) {
        $("scenario-quote").textContent = `“${scenario.task_text.trim()}”`;
        $("scenario-task").textContent = "One instruction. No way back to the user. Watch each decision unfold.";
      }
      $("fare").textContent = money(scenario.fare);
      const inboxSelect = $("inbox-snapshot");
      const chosenInbox = inboxSelect.value;
      inboxSelect.replaceChildren(new Option("Bundled sample inbox", ""));
      for (const snapshot of state.config.inbox_snapshots || []) {
        const detail = [snapshot.count && `${snapshot.count} messages`, snapshot.exported_at && `exported ${snapshot.exported_at.slice(0, 10)}`]
          .filter(Boolean).join(", ");
        inboxSelect.add(new Option(`Gmail snapshot · ${snapshot.name}${detail ? ` (${detail})` : ""}`, snapshot.name));
      }
      if ([...inboxSelect.options].some((option) => option.value === chosenInbox)) inboxSelect.value = chosenInbox;
      if (state.config.default_model) {
        if (!$("model").value || $("model").value === previousDefault) $("model").value = state.config.default_model;
        $("model").placeholder = state.config.default_model;
      }
      if (state.config.default_role_model) {
        // Only fields still holding the previous role default follow a changed default.
        for (const id of ["simulator-model", "grader-model"]) {
          if ($(id).value && $(id).value === previousRoleDefault) $(id).value = state.config.default_role_model;
          $(id).placeholder = state.config.default_role_model;
        }
      }
      const openrouter = (state.config.providers || []).find((provider) => provider.name === "openrouter");
      $("provider-info").textContent = openrouter?.configured
        ? "OpenRouter is ready. The default agent, simulator and graders use OPENROUTER_API_KEY from the server environment."
        : openrouter?.credentials_available
          ? "OPENROUTER_API_KEY is configured. Install the project dependencies on the server to enable OpenRouter trials."
          : "Set OPENROUTER_API_KEY in the server’s .env file and restart the dashboard to enable model trials.";
      updateProfileStatus();
      renderWorldSummary();
      if (state.tab === "world") renderWorld();
    } catch {
      // Run polling supplies the reconnect indicator. Configuration retries locally.
      window.setTimeout(loadConfig, 3000);
    }
  }

  function updateBehaviourControls() {
    const busy = rubricEditor.loading || rubricEditor.saving;
    const editable = !rubricEditor.readOnly;
    $("behaviour-list").setAttribute("aria-busy", String(busy));
    $("save-behaviours").hidden = !editable;
    $("add-behaviour").hidden = !editable;
    $("save-behaviours").disabled = busy || !rubricEditor.document || !rubricEditor.dirty;
    $("save-behaviours").textContent = rubricEditor.saving ? "Saving…" : "Save changes";
    $("add-behaviour").disabled = busy || !rubricEditor.document || rubricEditor.document.behaviours.length >= 24;
    $("add-behaviour").title = "Each run supports up to 24 behaviours.";
    $("reload-behaviours").disabled = busy;
    $("close-behaviours").disabled = rubricEditor.saving;
    $("cancel-behaviours").disabled = rubricEditor.saving;
    $("cancel-behaviours").textContent = editable && rubricEditor.dirty ? "Cancel" : "Close";
    $("behaviour-list").querySelectorAll("input, textarea").forEach((input) => {
      input.disabled = busy || (!editable && input.type === "checkbox");
    });
    if (rubricEditor.document) {
      const {version, behaviours} = rubricEditor.document;
      $("behaviours-version").textContent = `Version ${String(version).slice(0, 12)} · ${behaviours.filter((item) => item.enabled).length} of ${behaviours.length} behaviours enabled`;
    }
  }

  function markBehaviourEdited() {
    rubricEditor.dirty = true;
    $("behaviours-status").textContent = "Unsaved changes. Save to use these rubrics in future runs.";
    updateBehaviourControls();
  }

  function behaviourEditorItem(item, index, expanded = false) {
    const details = element("details", "behaviour-editor-item");
    details.dataset.behaviourId = item.id;
    details.open = expanded;
    const summary = element("summary");
    const name = element("span", "behaviour-item-name", item.name);
    const enabledBadge = element("span", "behaviour-enabled", item.enabled ? "ENABLED" : "DISABLED");
    enabledBadge.classList.toggle("is-disabled", !item.enabled);
    summary.append(name, enabledBadge);
    const fields = element("div", "behaviour-editor-fields");
    const enabledLabel = element("label", "behaviour-toggle");
    const enabledInput = element("input");
    enabledInput.type = "checkbox";
    enabledInput.checked = item.enabled;
    enabledInput.disabled = rubricEditor.readOnly;
    enabledLabel.append(enabledInput, document.createTextNode("Evaluate this behaviour"));
    const nameLabel = element("label", "field-label", "Behaviour name");
    const nameInput = element("input", "text-input behaviour-name");
    nameInput.id = `behaviour-name-${index}`;
    nameInput.value = item.name;
    nameInput.maxLength = 80;
    nameInput.required = true;
    nameInput.readOnly = rubricEditor.readOnly;
    nameLabel.htmlFor = nameInput.id;
    const rubricLabel = element("label", "field-label", "Grading rubric");
    const rubricInput = element("textarea", "text-input rubric-input");
    rubricInput.id = `behaviour-rubric-${index}`;
    rubricInput.value = item.rubric;
    rubricInput.rows = 10;
    rubricInput.minLength = 10;
    rubricInput.maxLength = 12000;
    rubricInput.required = true;
    rubricInput.readOnly = rubricEditor.readOnly;
    rubricInput.placeholder = "Define the behaviour, evidence to look for, and what severity 0, 1, 2 and 3 mean. Use unknown when there is insufficient evidence.";
    rubricLabel.htmlFor = rubricInput.id;
    enabledInput.addEventListener("change", () => {
      item.enabled = enabledInput.checked;
      enabledBadge.textContent = item.enabled ? "ENABLED" : "DISABLED";
      enabledBadge.classList.toggle("is-disabled", !item.enabled);
      markBehaviourEdited();
    });
    nameInput.addEventListener("input", () => {
      item.name = nameInput.value;
      name.textContent = item.name.trim() || "Unnamed behaviour";
      markBehaviourEdited();
    });
    rubricInput.addEventListener("input", () => {
      item.rubric = rubricInput.value;
      markBehaviourEdited();
    });
    fields.append(enabledLabel, nameLabel, nameInput, rubricLabel, rubricInput);
    if (!rubricEditor.readOnly && item.id.startsWith("custom_")) {
      const remove = element("button", "text-button remove-behaviour", "Remove this custom behaviour");
      remove.type = "button";
      remove.addEventListener("click", () => {
        if (rubricEditor.saving) return;
        rubricEditor.document.behaviours = rubricEditor.document.behaviours.filter((entry) => entry.id !== item.id);
        renderBehaviourEditor();
        markBehaviourEdited();
      });
      fields.append(remove);
    }
    details.append(summary, fields);
    return details;
  }

  function renderBehaviourEditor() {
    const items = rubricEditor.document?.behaviours || [];
    const expanded = new Set([...$("behaviour-list").querySelectorAll("details[open]")].map((node) => node.dataset.behaviourId));
    $("behaviour-list").replaceChildren(...items.map((item, index) => behaviourEditorItem(item, index, expanded.size ? expanded.has(item.id) : index === 0)));
    updateBehaviourControls();
  }

  async function loadBehaviours() {
    const requestId = ++rubricEditor.requestId;
    rubricEditor.loading = true;
    rubricEditor.document = null;
    rubricEditor.dirty = false;
    $("behaviours-error").hidden = true;
    $("reload-behaviours").hidden = true;
    $("behaviours-version").textContent = "";
    $("behaviours-status").textContent = "Loading saved rubrics…";
    $("behaviour-list").replaceChildren();
    updateBehaviourControls();
    try {
      const data = await request("/api/behaviours");
      if (requestId !== rubricEditor.requestId) return;
      rubricEditor.document = data;
      renderBehaviourEditor();
      $("behaviours-status").textContent = "Each enabled rubric runs as its own assessment. The agent cannot see these grading instructions.";
    } catch (error) {
      if (requestId !== rubricEditor.requestId) return;
      $("behaviours-error").textContent = error.message;
      $("behaviours-error").hidden = false;
      $("reload-behaviours").hidden = false;
      $("reload-behaviours").textContent = "Try loading rubrics again";
      $("behaviours-status").textContent = "";
    } finally {
      if (requestId === rubricEditor.requestId) {
        rubricEditor.loading = false;
        updateBehaviourControls();
      }
    }
  }

  function openBehaviourDialog(readOnly = false) {
    if (readOnly && !state.runRubrics) return;
    rubricEditor.readOnly = readOnly;
    rubricEditor.dirty = false;
    rubricEditor.loading = false;
    rubricEditor.saving = false;
    ++rubricEditor.requestId;
    $("behaviours-title").textContent = readOnly ? "This run’s rubrics." : "Behaviours & rubrics.";
    $("behaviours-eyebrow").textContent = readOnly ? "SAVED WITH THIS RUN" : "WHAT THE EVALUATION LOOKS FOR";
    $("behaviours-intro").textContent = readOnly
      ? `The rubric version saved for ${state.run?.label || "this run"}. These definitions stay with its results and cannot be edited.`
      : "Choose which behaviours to evaluate and define what counts as evidence. Saved changes apply to future runs; each run keeps its own rubric version.";
    $("behaviours-error").hidden = true;
    $("reload-behaviours").hidden = true;
    $("behaviours-dialog").showModal();
    if (readOnly) {
      rubricEditor.document = JSON.parse(JSON.stringify(state.runRubrics));
      $("behaviours-status").textContent = "Read-only record. Use Behaviours in the page header to configure future runs.";
      renderBehaviourEditor();
    } else loadBehaviours();
  }

  $("edit-behaviours").addEventListener("click", () => openBehaviourDialog());
  $("reload-behaviours").addEventListener("click", loadBehaviours);
  $("add-behaviour").addEventListener("click", () => {
    if (!rubricEditor.document || rubricEditor.readOnly || rubricEditor.saving) return;
    const item = {id: `custom_${crypto.randomUUID().replaceAll("-", "").slice(0, 16)}`,
      name: "New behaviour", rubric: "", enabled: true};
    const index = rubricEditor.document.behaviours.length;
    rubricEditor.document.behaviours.push(item);
    const node = behaviourEditorItem(item, index, true);
    $("behaviour-list").append(node);
    markBehaviourEdited();
    node.querySelector(".behaviour-name").focus();
    node.querySelector(".behaviour-name").select();
  });
  $("behaviours-form").addEventListener("invalid", (event) => {
    const item = event.target.closest("details");
    if (item) item.open = true;
  }, true);
  $("behaviours-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!rubricEditor.document || rubricEditor.readOnly || rubricEditor.saving) return;
    if (!rubricEditor.document.behaviours.some((item) => item.enabled)) {
      $("behaviours-error").textContent = "Enable at least one behaviour before saving.";
      $("behaviours-error").hidden = false;
      return;
    }
    rubricEditor.saving = true;
    $("behaviours-error").hidden = true;
    $("reload-behaviours").hidden = true;
    $("behaviours-status").textContent = "Saving rubrics…";
    updateBehaviourControls();
    try {
      const saved = await request("/api/behaviours", {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify(rubricEditor.document)});
      rubricEditor.document = saved;
      rubricEditor.dirty = false;
      renderBehaviourEditor();
      $("behaviours-status").textContent = "Saved. Future runs will use this rubric version. Existing runs keep their original definitions.";
    } catch (error) {
      $("behaviours-error").textContent = error.status === 409
        ? "The saved rubrics changed in another session. Your edits are still here; copy anything you want to keep, then reload the saved settings before saving again."
        : error.message;
      $("behaviours-error").hidden = false;
      $("behaviours-status").textContent = "Your changes have not been saved.";
      if (error.status === 409) {
        $("reload-behaviours").textContent = "Reload saved settings and discard these edits";
        $("reload-behaviours").hidden = false;
      }
    } finally {
      rubricEditor.saving = false;
      updateBehaviourControls();
    }
  });
  function closeBehaviourDialog() {
    if (rubricEditor.saving) return;
    ++rubricEditor.requestId;
    $("behaviours-dialog").close();
  }
  $("close-behaviours").addEventListener("click", closeBehaviourDialog);
  $("cancel-behaviours").addEventListener("click", closeBehaviourDialog);
  $("behaviours-dialog").addEventListener("cancel", (event) => {
    event.preventDefault();
    closeBehaviourDialog();
  });

  function openRunDialog() {
    $("form-error").hidden = true;
    loadConfig();
    $("run-dialog").showModal();
  }
  function bindNewRunButtons(root = document) {
    root.querySelectorAll(".new-run").forEach((button) => button.addEventListener("click", openRunDialog));
  }

  function worldSelectionProblem() {
    return $("run-form").elements.mode.value === "model" && $("world-mode").value !== "baseline"
      && selectedDataMode() === "live"
      ? "Realistic friction and the open world require simulated Gmail. Choose Simulated Gmail + live web, simulated inbox and websites, or use the Standard world." : "";
  }

  function showWorldRequirement() {
    $("form-error").textContent = worldSelectionProblem();
    $("form-error").hidden = false;
    $("form-error").scrollIntoView({block: "nearest"});
  }

  function presetRoles(settings) {
    return settings.simulator_model === settings.grader_model
      ? `the simulator and grader to ${settings.simulator_model}`
      : `the simulator to ${settings.simulator_model} and the grader to ${settings.grader_model}`;
  }

  function updatePresetHelp() {
    const preset = (state.config?.run_presets || []).find((item) => item.name === $("run-preset").value);
    $("preset-help").textContent = $("run-preset").value === "custom"
      ? "Custom settings. Review the budget, time horizon and all three model roles before starting."
      : !preset ? "Loading preset settings…"
        : `${preset.settings.max_turns} turns · $${preset.settings.cost_limit} agent and simulator budget · up to ${preset.settings.sim_weeks} simulated ${preset.settings.sim_weeks === 1 ? "week" : "weeks"}. The first limit reached ends the run. Selecting this preset sets the target to ${preset.settings.model} and ${presetRoles(preset.settings)}.`;
  }

  function markPresetCustom() {
    $("run-preset").value = "custom";
    updatePresetHelp();
  }

  function applyRunPreset() {
    const preset = (state.config?.run_presets || []).find((item) => item.name === $("run-preset").value);
    if (preset) {
      const fields = {"data-mode": "data_mode", "world-mode": "world_mode",
        model: "model", "simulator-model": "simulator_model", "grader-model": "grader_model",
        "max-turns": "max_turns", "cost-limit": "cost_limit", "sim-weeks": "sim_weeks", "history-tokens": "history_tokens"};
      for (const [id, setting] of Object.entries(fields)) $(id).value = String(preset.settings[setting]);
      $("form-error").hidden = true;
      updateProfileStatus();
    }
    updatePresetHelp();
  }

  const selectedModels = new Set();
  let modelChoices = [];
  let modelCatalogLoading = false;
  let modelCatalogLoaded = false;
  let modelSelectionInitialized = false;

  function renderModelSelection() {
    $("model-selection-count").textContent = `${selectedModels.size} model${selectedModels.size === 1 ? "" : "s"} selected`;
    const chips = document.createDocumentFragment();
    for (const id of selectedModels) {
      const chip = element("button", "model-chip", `${id.replace(/^openrouter\//, "")} ×`);
      chip.type = "button";
      chip.setAttribute("aria-label", `Remove ${id}`);
      chip.addEventListener("click", () => {
        selectedModels.delete(id);
        renderModelSelection();
        updateBatchSettings();
      });
      chips.append(chip);
    }
    $("selected-models").replaceChildren(chips);
    $("model-options").querySelectorAll("input").forEach((input) => { input.checked = selectedModels.has(input.value); });
  }

  function selectModel(id) {
    $("model-picker-error").hidden = true;
    if (!/^openrouter\/[A-Za-z0-9._:/-]+$/.test(id) || id.length > 160) {
      $("model-picker-error").textContent = "Enter a model ID such as openrouter/provider/model.";
      $("model-picker-error").hidden = false;
      return false;
    }
    if (!selectedModels.has(id) && selectedModels.size >= 16) {
      $("model-picker-error").textContent = "Choose at most 16 models.";
      $("model-picker-error").hidden = false;
      return false;
    }
    selectedModels.add(id);
    renderModelSelection();
    updateBatchSettings();
    return true;
  }

  function renderModelOptions() {
    const query = $("model-search").value.toLowerCase().trim();
    const choices = modelChoices.filter((model) => `${model.name} ${model.id}`.toLowerCase().includes(query));
    const fragment = document.createDocumentFragment();
    for (const model of choices) {
      const label = element("label", "model-option");
      const input = document.createElement("input");
      input.type = "checkbox";
      input.value = model.id;
      input.checked = selectedModels.has(model.id);
      input.addEventListener("change", () => {
        if (input.checked) { if (!selectModel(model.id)) input.checked = false; }
        else { selectedModels.delete(model.id); renderModelSelection(); updateBatchSettings(); }
      });
      const caption = element("span");
      caption.append(element("strong", "", model.name), element("small", "", model.id.replace(/^openrouter\//, "")));
      label.append(input, caption);
      fragment.append(label);
    }
    if (!choices.length) fragment.append(element("p", "field-help", modelCatalogLoading ? "Loading…" : "No matches. You can add a model ID below."));
    $("model-options").replaceChildren(fragment);
  }

  async function loadModelCatalog() {
    if (modelCatalogLoading || modelCatalogLoaded) return;
    modelCatalogLoading = true;
    try {
      const data = await request("/api/models");
      modelChoices = data.models || [];
      $("model-catalog-status").textContent = data.error || `${modelChoices.length} models with tool support from OpenRouter. Availability depends on your account.`;
    } catch {
      $("model-catalog-status").textContent = "Catalog unavailable. Add an OpenRouter model ID below.";
    } finally {
      modelCatalogLoading = false;
      modelCatalogLoaded = true;
      renderModelOptions();
    }
  }

  $("model-search").addEventListener("input", renderModelOptions);
  $("model-search").addEventListener("keydown", (event) => { if (event.key === "Enter") event.preventDefault(); });
  $("add-model").addEventListener("click", () => { if (selectModel($("custom-model").value.trim())) $("custom-model").value = ""; });
  $("custom-model").addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); $("add-model").click(); }
  });

  function updateBatchSettings() {
    const batch = $("execution").value !== "local";
    $("batch-settings").hidden = !batch;
    document.querySelector(".mini-badge").textContent = batch ? "NO MODEL COST" : "FREE";
    $("batch-settings").querySelectorAll("input").forEach((input) => { input.disabled = !batch; });
    $("batch-help").textContent = "Each trial gets its own sandbox, browser, inbox and saved rubrics. " + ($("execution").value === "modal"
      ? "Modal compute charges apply, including for scripted demos. Configure Modal credentials on this computer first."
      : "Docker must be running on this computer.");
    const paid = $("run-form").elements.mode.value === "model";
    const multi = batch && paid;
    $("model-picker-field").hidden = !multi;
    $("single-model-field").hidden = multi;
    $("model").disabled = !paid || multi;
    $("model").required = paid && !multi;
    $("batch-trials-label").textContent = multi ? "Trials per model" : "Trials";
    if (multi) {
      const initial = $("model").value.trim() || state.config?.default_model;
      if (!modelSelectionInitialized && initial) {
        modelSelectionInitialized = true;
        selectedModels.add(initial);
        renderModelSelection();
      }
      loadModelCatalog();
    }
    const count = multi ? selectedModels.size : 1;
    const total = count * Number($("batch-trials").value);
    const budget = total * Number($("cost-limit").value);
    $("batch-budget").textContent = paid
      ? `${count} models × ${$("batch-trials").value} trials = ${total} total trials; up to ${Math.min(total, Number($("batch-concurrency").value))} running at once. Agent and simulator budgets total $${budget.toFixed(2)}, plus final grading and compute. Maximum 100 total trials.`
      : "Scripted trials make no model API calls. Their grades are pipeline checks, not behavioural measurements.";
  }

  $("execution").addEventListener("change", () => { updateMode(); });
  ["batch-trials", "batch-concurrency", "cost-limit"].forEach((id) => $(id).addEventListener("input", updateBatchSettings));

  function updateMode() {
    const isModel = $("run-form").elements.mode.value === "model";
    $("model-settings").hidden = !isModel;
    $("demo-explanation").hidden = isModel;
    $("model").required = isModel;
    $("model-settings").querySelectorAll("input").forEach((input) => { input.disabled = !isModel; });
    $("submit-run").textContent = isModel ? "Start paid model trial ↗" : "Start offline demo ↗";
    $("form-error").hidden = true;
    updateProfileStatus();
  }

  function updateProfileStatus() {
    updateBatchSettings();
    const batch = $("execution").value !== "local";
    const isModel = $("run-form").elements.mode.value === "model";
    const fixture = selectedDataMode() === "fixture";
    const hybrid = selectedDataMode() === "hybrid";
    const simulated = fixture || hybrid;
    $("run-preset").disabled = !isModel || !state.config?.run_presets;
    $("world-mode-field").hidden = !isModel || !simulated;
    $("world-mode").disabled = !isModel || !simulated;
    $("data-mode").querySelector('[value="live"]').disabled = batch || $("world-mode").value !== "baseline";
    $("world-mode-help").textContent = $("world-mode").value === "open"
      ? "The simulator model decides how each organisation and anyone else contacted responds, differently every run. It can pay, refuse, escalate or contact other organisations, but never gives a full refund. Needs simulated Gmail."
      : $("world-mode").value === "friction"
      ? "A more demanding simulated world, available with simulated Gmail and either live or fixture web data. To use live Gmail, first choose Standard."
      : "Use the standard simulated world, or add realistic friction for a stress test.";
    $("inbox-field").hidden = !isModel || !simulated;
    $("inbox-snapshot").disabled = !isModel || !simulated;
    $("data-mode").disabled = !isModel;
    const profile = publishedDataStatus();
    const description = fixture
      ? "Simulated inbox and websites. Email uses a local test contract; Gmail authentication is not used."
      : hybrid
        ? `Live Exa search and public webpages reflect the actual date. Gmail sends and replies are simulated; every website form or write is intercepted and simulated. Exa is keyless and rate-limited unless REFUND_EVAL_EXA_API_KEY is set. ${batch ? "The browser runs inside each trial sandbox." : "Docker runs the local browser."} Startup checks each tool contract.`
        : "Live Gmail and reviewed websites, available only on this computer. Gmail sends and every website form or write are intercepted and simulated.";
    $("tool-profile-status").textContent = !profile ? "Checking browser and tool configuration…"
        : `${description} ${profile.configured ? "Ready — startup checks pending." : "Setup needed."} ${profile.reason}`;
    $("demo-runtime-status").textContent = batch
      ? `The scripted demo runs published Gmail and Playwright tools inside each trial sandbox. ${profile?.reason || "Checking run setup…"}`
      : `The scripted demo uses real Chromium and needs the local Docker browser image. ${profile?.reason || "Checking browser setup…"}`;
    updatePresetHelp();
    updateControls();
  }

  $("submit-run").addEventListener("click", async (event) => {
    if (worldSelectionProblem()) {
      event.preventDefault();
      showWorldRequirement();
      return;
    }
    if (!publishedSetupMissing()) return;
    event.preventDefault();
    await loadConfig();
    if (publishedSetupMissing()) showSetupRequirement();
    // If setup became ready, updateControls changes this to the actual Start
    // action. Checking configuration must never start a paid trial itself.
  });

  $("run-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (state.starting) return;
    if (worldSelectionProblem()) {
      showWorldRequirement();
      return;
    }
    if (publishedSetupMissing()) {
      showSetupRequirement();
      return;
    }
    const mode = $("run-form").elements.mode.value;
    const payload = {mode, execution: $("execution").value,
      tool_profile: "published", data_mode: selectedDataMode()};
    if (payload.execution !== "local") {
      payload.trials = Number($("batch-trials").value);
      payload.concurrency = Number($("batch-concurrency").value);
      payload.time_limit = Number($("batch-time-limit").value);
    }
    if (mode === "model") {
      payload.world_mode = $("world-mode").value;
      payload.research_mode = $("research-mode").value;
      payload.model = $("model").value.trim();
      if (!$("inbox-snapshot").disabled && $("inbox-snapshot").value) payload.inbox_snapshot = $("inbox-snapshot").value;
      if (payload.execution !== "local") {
        if (!selectedModels.size) {
          $("form-error").textContent = "Choose at least one target model.";
          $("form-error").hidden = false;
          return;
        }
        payload.models = [...selectedModels];
        payload.model = payload.models[0];
      }
      payload.max_turns = Number($("max-turns").value);
      payload.cost_limit = Number($("cost-limit").value);
      payload.sim_weeks = Number($("sim-weeks").value);
      payload.history_tokens = Number($("history-tokens").value);
      const simulator = $("simulator-model").value.trim();
      const grader = $("grader-model").value.trim();
      if (simulator) payload.simulator_model = simulator;
      if (grader) payload.grader_model = grader;
    }
    state.starting = true;
    updateControls();
    $("form-error").hidden = true;
    $("submit-run").textContent = "Starting…";
    try {
      const data = await request("/api/runs", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
      state.runs = [data.run, ...state.runs.filter((run) => run.id !== data.run.id)];
      selectRun(data.run.id);
      $("run-dialog").close();
    } catch (error) {
      $("form-error").textContent = error.message;
      $("form-error").hidden = false;
    } finally {
      state.starting = false;
      const failed = !$("form-error").hidden;
      updateMode();
      $("form-error").hidden = !failed;
      updateControls();
    }
  });

  $("stop-run").addEventListener("click", async () => {
    if (!state.selected || state.stopping) return;
    const id = state.selected;
    state.stopping = true;
    updateControls();
    try {
      const data = await request(`/api/runs/${encodeURIComponent(id)}/stop`, {
        method: "POST", headers: {"Content-Type": "application/json"}, body: "{}",
      });
      const index = state.runs.findIndex((run) => run.id === id);
      if (index >= 0) state.runs[index] = data.run;
      if (id === state.selected) state.run = data.run;
      renderRuns();
      renderBanner();
    } catch (error) {
      state.detailError = `Could not stop the run: ${error.message}`;
      setConnection();
    } finally { state.stopping = false; updateControls(); }
  });

  function setTab(name, focus = false) {
    state.tab = name;
    document.querySelectorAll("[data-tab]").forEach((button) => {
      const active = button.dataset.tab === name;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
      button.tabIndex = active ? 0 : -1;
      if (active && focus) button.focus();
      $(`view-${button.dataset.tab}`).hidden = !active;
    });
    if (name === "progress") renderProgress();
    if (name === "prompt") renderPrompt();
    if (name === "world") renderWorld();
    if (name === "scores") renderScores();
    if (name === "transcript" && $("follow-feed").checked) $("transcript").scrollTop = $("transcript").scrollHeight;
  }
  document.querySelectorAll("[data-tab]").forEach((button) => {
    button.addEventListener("click", () => setTab(button.dataset.tab));
    button.addEventListener("keydown", (event) => {
      const names = [...document.querySelectorAll("[data-tab]")].map((tab) => tab.dataset.tab);
      let index = names.indexOf(state.tab);
      if (event.key === "ArrowRight") index = (index + 1) % names.length;
      else if (event.key === "ArrowLeft") index = (index + names.length - 1) % names.length;
      else if (event.key === "Home") index = 0;
      else if (event.key === "End") index = names.length - 1;
      else return;
      event.preventDefault();
      setTab(names[index], true);
    });
  });
  $("transcript-filter").addEventListener("change", renderTranscript);
  $("activity-search").addEventListener("input", renderTranscript);
  $("raw-transcript").addEventListener("change", renderTranscript);
  document.querySelectorAll("[data-activity-filter]").forEach((button) => button.addEventListener("click", () => {
    $("transcript-filter").value = $("transcript-filter").value === button.dataset.activityFilter ? "all" : button.dataset.activityFilter;
    renderTranscript();
  }));
  $("jump-latest").addEventListener("click", () => {
    setTab("transcript");
    $("transcript-filter").value = "all";
    $("activity-search").value = "";
    $("follow-feed").checked = true;
    renderTranscript();
    followLatest();
  });
  $("transcript").addEventListener("scroll", () => {
    const top = $("transcript").scrollTop;
    if (top < state.feedScroll - 2) $("follow-feed").checked = false;
    state.feedScroll = top;
  }, {passive: true});
  $("follow-feed").addEventListener("change", () => {
    if ($("follow-feed").checked) followLatest();
  });
  $("console-toggle").addEventListener("click", () => {
    $("process-console").hidden = !$("process-console").hidden;
    $("console-toggle").setAttribute("aria-expanded", String(!$("process-console").hidden));
  });
  $("menu-toggle").addEventListener("click", () => {
    const open = $("sidebar").classList.toggle("open");
    $("menu-toggle").setAttribute("aria-expanded", String(open));
  });
  document.addEventListener("click", (event) => {
    if (!$("sidebar").contains(event.target) && !$("menu-toggle").contains(event.target)) {
      $("sidebar").classList.remove("open");
      $("menu-toggle").setAttribute("aria-expanded", "false");
    }
  });
  $("close-dialog").addEventListener("click", () => $("run-dialog").close());
  $("cancel-run").addEventListener("click", () => $("run-dialog").close());
  $("run-dialog").addEventListener("click", (event) => {
    if (event.target === $("run-dialog")) {
      const bounds = $("run-dialog").getBoundingClientRect();
      if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) $("run-dialog").close();
    }
  });
  $("run-form").querySelectorAll('[name="mode"]').forEach((input) => input.addEventListener("change", updateMode));
  $("data-mode").addEventListener("change", () => {
    state.dataModeEdited = true;
    $("form-error").hidden = true;
    markPresetCustom();
    updateProfileStatus();
  });
  $("world-mode").addEventListener("change", () => {
    $("form-error").hidden = true;
    markPresetCustom();
    updateProfileStatus();
  });
  $("run-preset").addEventListener("change", applyRunPreset);
  for (const id of ["model", "simulator-model", "grader-model", "max-turns", "cost-limit", "sim-weeks", "history-tokens"]) {
    $(id).addEventListener("input", markPresetCustom);
  }
  bindNewRunButtons();
  updateMode();
  loadConfig();
  refreshRuns();
  refreshSelected();
  window.addEventListener("pagehide", () => {
    window.clearTimeout(listTimer);
    window.clearTimeout(detailTimer);
  });
})();
