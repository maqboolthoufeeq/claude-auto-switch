// The launcher opens the page with a single-use #code (or the terminal's #token link); keep neither in the address bar.
const linkParams = new URLSearchParams(location.hash.slice(1));
if (location.hash) history.replaceState(null, "", location.pathname);
if (linkParams.get("token")) sessionStorage.setItem("switchboard-token", linkParams.get("token"));
let token = sessionStorage.getItem("switchboard-token");
const $ = (id) => document.getElementById(id);
let accounts = [];
let accountsLoaded = false;
let taskInfo = null;
let sharedInfo = null;
let sharedUnavailable = false;
let noticeTimer = null;
let accountRenderKey = "";
let sharedChoicesKey = "";
let selectedAccountInfo = null;
let pendingAccountAction = null;
const openSettings = new Set();

function notice(message, kind = "info") {
  const box = $("notice");
  box.textContent = message;
  box.className = "notice " + kind;
  box.hidden = false;
  clearTimeout(noticeTimer);
  if (kind === "success") noticeTimer = setTimeout(() => { box.hidden = true; }, 5500);
}

function friendlyError(error) {
  if (error instanceof TypeError) return "Cannot reach Switchboard. Reopen the launcher, then use its new browser tab.";
  if (error.status === 404) return "This action needs a newer Switchboard launcher. Close its Terminal window and reopen Switchboard.";
  if (error.status === 403) return "This tab's private link is no longer valid. Reopen Switchboard from the launcher.";
  return error.message || "Something went wrong. Please try again.";
}

async function api(path, data) {
  if (!token) throw new Error("Open Switchboard from the link printed by its launcher.");
  const options = {
    method: data === undefined ? "GET" : "POST",
    credentials: "omit",
    headers: { "X-Rotator-Token": token },
  };
  if (data !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(data);
  }
  const response = await fetch(path, options);
  let result = {};
  try { result = await response.json(); } catch (_) {}
  if (!response.ok) {
    const error = new Error(result.error || "Switchboard could not complete that request.");
    error.status = response.status;
    error.path = path;
    throw error;
  }
  return result;
}

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function setView(view) {
  const automatic = view === "automatic";
  $("automatic-view").hidden = !automatic;
  $("task-view").hidden = automatic;
  $("tab-automatic").classList.toggle("active", automatic);
  $("tab-task").classList.toggle("active", !automatic);
  $("tab-automatic").setAttribute("aria-selected", String(automatic));
  $("tab-task").setAttribute("aria-selected", String(!automatic));
  sessionStorage.setItem("switchboard-view", view);
}

function renderRoute() {
  const route = $("route");
  route.replaceChildren(...accounts.map((account, index) => {
    const current = sharedInfo?.current === account.name;
    const connected = account.signed_in && !sharedInfo?.login_errors?.[account.name];
    const stop = node("button", "route-stop" + (connected ? " connected" : " disconnected") + (current ? " current" : ""));
    stop.type = "button";
    stop.title = "View " + account.display_name + " details";
    stop.setAttribute("aria-label", "View " + account.display_name + " details, " + (connected ? "connected" : "needs sign-in") + (current ? ", current shared login" : ""));
    stop.append(node("span", "route-dot", String(index + 1)), node("span", "route-name", account.display_name),
      node("span", "route-status", current ? "Current" : connected ? "Connected" : "Sign in"));
    stop.addEventListener("click", () => openAccountInfo(account.name));
    return stop;
  }));
  const count = accounts.filter((account) => account.signed_in && !sharedInfo?.login_errors?.[account.name]).length;
  $("route-count").textContent = accountsLoaded ? count + " of " + accounts.length + " connected" : "Checking accounts…";
  const current = accounts.find((account) => account.name === sharedInfo?.current);
  $("hero-summary").textContent = current
    ? "Currently using " + current.display_name + ". Select an account in the route to see its details."
    : "See which accounts are connected and where Switchboard will go next.";
}

function makeAccountRow(account, index) {
  const accountPrepared = Boolean(sharedInfo?.accounts?.find((item) => item.name === account.name)?.prepared);
  const loginError = sharedInfo?.login_errors?.[account.name];
  const row = node("article", "account-row");
  if (sharedInfo?.current === account.name) row.classList.add("current");
  const top = node("div", "account-top");
  const number = node("span", "account-number", String(index + 1));
  const identity = node("div", "account-identity");
  identity.append(node("strong", "", account.display_name));
  if (account.display_name !== account.name) identity.append(node("small", "", account.name));
  const status = node("span", "account-status" + (account.signed_in && !loginError ? "" : " out"), account.signed_in && !loginError ? "Connected" : "Needs sign-in");
  const info = node("button", "info-button", "i");
  info.type = "button";
  info.setAttribute("aria-label", "Information about " + account.display_name);
  info.title = "Account information";
  info.addEventListener("click", () => openAccountInfo(account.name));
  top.append(number, identity, status, info);
  if (sharedInfo?.current === account.name) row.append(node("span", "current-marker", "Current shared login"));
  row.append(top);

  const bottom = node("div", "account-bottom");
  const usage = node("span", "account-usage", loginError ? "Saved login rejected · sign in and prepare again" : accountPrepared ? "In rotation · " + (account.early_usage_check ? "switch at " + account.threshold_percent + "%" : "switch at limit") : "Not in rotation");
  const login = node("button", "account-link", account.signed_in ? "Sign in again" : "Sign in");
  login.type = "button";
  login.addEventListener("click", async () => {
    login.disabled = true;
    try {
      await api("/api/login", { name: account.name });
      notice("Sign-in started for " + account.display_name + ". Finish it in your browser.", "success");
      await refreshTask();
    } catch (error) { notice(friendlyError(error), "error"); }
    finally { login.disabled = false; }
  });
  bottom.append(usage, login);
  row.append(bottom);

  const settings = node("details", "account-settings");
  settings.open = openSettings.has(account.name);
  settings.addEventListener("toggle", () => {
    if (settings.open) openSettings.add(account.name);
    else openSettings.delete(account.name);
  });
  settings.append(node("summary", "", "Name and settings"));
  const nameForm = node("form", "display-name-form");
  nameForm.noValidate = true;
  const nameLabel = node("label", "", "Account name");
  const nameInput = node("input");
  nameInput.type = "text";
  nameInput.maxLength = 60;
  nameInput.value = account.display_name;
  nameInput.setAttribute("aria-label", "Name for " + account.name);
  nameLabel.append(nameInput);
  const nameSave = node("button", "", "Save");
  nameSave.type = "submit";
  nameForm.append(nameLabel, nameSave);
  nameForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!nameInput.value.trim()) {
      notice("Enter a name for this account.", "error");
      nameInput.focus();
      return;
    }
    nameSave.disabled = true;
    try {
      await api("/api/display-name", { name: account.name, display_name: nameInput.value.trim() });
      notice("Account name saved.", "success");
      nameInput.blur();
      await refreshAccounts();
      await refreshShared();
    } catch (error) { notice(friendlyError(error), "error"); }
    finally { nameSave.disabled = false; }
  });
  settings.append(nameForm);
  if (account.early_usage_check) {
    const form = node("form", "threshold-form");
    const label = node("label", "", "Switch at");
    const input = node("input");
    input.type = "number";
    input.min = "1";
    input.max = "100";
    input.step = "1";
    input.value = String(account.threshold_percent);
    const percent = node("span", "", "% usage");
    const save = node("button", "", "Save");
    save.type = "submit";
    form.append(label, input, percent, save);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const value = Number(input.value);
      if (!Number.isFinite(value) || value <= 0 || value > 100) {
        notice("Choose a percentage from 1 to 100.", "error");
        input.focus();
        return;
      }
      save.disabled = true;
      try {
        await api("/api/threshold", { name: account.name, percent: value });
        notice("Switching threshold saved.", "success");
        input.blur();
        await refreshAccounts();
      } catch (error) { notice(friendlyError(error), "error"); }
      finally { save.disabled = false; }
    });
    settings.append(form);
  }
  row.append(settings);
  return row;
}

function renderAccounts() {
  const key = JSON.stringify([accountsLoaded, accounts, sharedInfo?.current, sharedInfo?.login_errors, sharedInfo?.accounts?.map((account) => [account.name, account.prepared])]);
  if (key === accountRenderKey || document.activeElement?.matches(".account-settings input")) return;
  accountRenderKey = key;
  $("accounts").replaceChildren(...accounts.map(makeAccountRow));
  const ready = sharedInfo?.accounts?.filter((account) => account.prepared && !sharedInfo?.login_errors?.[account.name]).length || 0;
  const signed = accounts.filter((account) => account.signed_in).length;
  $("signed-count").textContent = !accountsLoaded ? "Checking…" : sharedInfo && !sharedUnavailable
    ? ready + " ready"
    : signed + " signed in";
  renderRoute();
}

function formatDate(value) {
  if (!value) return "Not recorded";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Not recorded" : new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium", timeStyle: "short"
  }).format(date);
}

function renderAccountInfo(info) {
  $("account-dialog-error").hidden = true;
  $("account-dialog-title").textContent = info.display_name;
  $("account-dialog-status").textContent = info.login_error ? "Saved login rejected. Sign in again, then update prepared accounts." : info.signed_in
    ? "Connected to Claude Code" : "This profile needs a Claude sign-in";
  $("account-dialog-status").className = "dialog-status" + (info.signed_in && !info.login_error ? "" : " out");
  const facts = [
    ["Profile ID", info.name],
    ["Login check", info.status],
    ["Email (if known)", info.email || "Not available"],
    ["Organization ID", info.organization_id || "Not available"],
    ["Current shared login", info.current ? "Yes" : "No"],
    ["Prepared for rotation", info.prepared ? "Yes" : "No"],
    ["Connected since", formatDate(info.connected_since)],
    ["Last sign-in in Switchboard", formatDate(info.last_login_at)],
    ["Last sign-out", formatDate(info.last_logout_at)],
    ["Profile folder", info.profile_dir],
  ];
  const list = node("dl", "facts-grid");
  for (const [label, value] of facts) {
    const item = node("div", "fact");
    item.append(node("dt", "", label), node("dd", "", value));
    list.append(item);
  }
  $("account-facts").replaceChildren(list);
  const events = (info.events || []).map((event) => {
    const item = node("div", "account-event");
    item.append(node("time", "", formatDate(event.at)), node("span", "", event.message || "Account updated"));
    return item;
  });
  $("account-events").replaceChildren(...(events.length ? events : [node("p", "empty-events", "No activity recorded yet.")]));
  const lastPrepared = info.prepared && info.switching_enabled && sharedInfo?.imported_count <= 2;
  const blocked = (info.current && info.switching_enabled) || lastPrepared;
  $("account-logout").disabled = blocked || (!info.signed_in && !info.prepared && !info.current);
  $("account-delete").disabled = blocked || accounts.length <= 1;
  $("account-action-note").textContent = info.current && info.switching_enabled
    ? "Switch the shared login to another account before logging out or deleting this one."
    : lastPrepared
      ? "Turn off automatic switching before removing one of its last two accounts."
      : info.current
        ? "Logging out or deleting this account will also sign out the current shared login. Open chats may pause."
        : "Times are recorded by Switchboard. Sign-ins from before tracking began may show as not recorded.";
}

async function openAccountInfo(name) {
  const dialog = $("account-dialog");
  $("account-confirm").hidden = true;
  $("account-dialog-title").textContent = "Loading account…";
  $("account-dialog-status").textContent = "";
  $("account-facts").replaceChildren();
  $("account-events").replaceChildren();
  $("account-logout").disabled = true;
  $("account-delete").disabled = true;
  $("account-dialog-error").hidden = true;
  if (!dialog.open) dialog.showModal();
  try {
    const info = await api("/api/account?name=" + encodeURIComponent(name));
    if (!dialog.open) return;
    selectedAccountInfo = info;
    renderAccountInfo(info);
  } catch (error) {
    dialog.close();
    notice(friendlyError(error), "error");
  }
}

function confirmAccountAction(action) {
  if (!selectedAccountInfo) return;
  pendingAccountAction = action;
  const label = selectedAccountInfo.display_name;
  $("account-confirm-title").textContent = action === "delete" ? "Delete " + label + " from Switchboard?" : "Log out " + label + "?";
  $("account-confirm-message").textContent = action === "delete"
    ? "This signs out the profile, removes it from rotation, and deletes its Switchboard entry. The local profile folder and chat history stay on your computer."
    : "This signs out the profile and removes its saved login from automatic rotation. You can sign in again later.";
  $("account-confirm-proceed").textContent = action === "delete" ? "Delete account" : "Log out";
  $("account-confirm").hidden = false;
}

function outcomeText(outcome) {
  return {
    usage_limit: "Usage limit · switched",
    auth_error: "Sign-in expired · switched",
    continue: "Continuing",
    complete: "Complete",
    blocked: "Needs your input",
    error: "Stopped with an error",
    invalid_result: "Stopped without a result",
  }[outcome] || outcome || "Running";
}

function renderTask() {
  const saved = taskInfo?.saved;
  const running = Boolean(taskInfo?.running && taskInfo.kind === "task");
  const selectedProject = $("project").value.trim();
  const state = $("task-state");
  state.className = "state-label" + (running ? " running" : saved?.status === "complete" ? " complete" : saved?.status === "paused" ? " paused" : "");
  state.textContent = running ? "Running" : saved?.status === "complete" ? "Complete" : saved?.status === "paused" ? "Paused" : saved?.status === "running" ? "Ready to continue" : "No saved task";
  $("tab-task-state").hidden = !running;
  $("empty-progress").hidden = Boolean(saved);
  $("progress-content").hidden = !saved;
  if (saved) {
    $("saved-task").textContent = saved.task || "Saved task";
    $("summary").textContent = saved.last_summary || "No progress summary yet.";
    const history = $("run-history");
    history.replaceChildren();
    (saved.runs || []).forEach((run, index) => {
      const item = node("div", "run-item" + (run.outcome === "usage_limit" ? " limit" : ""));
      const label = accounts.find((account) => account.name === run.account)?.display_name || run.account || "Account";
      item.append(node("span", "run-number", String(index + 1).padStart(2, "0")), node("span", "run-account", label), node("span", "run-outcome", outcomeText(run.outcome)));
      history.append(item);
    });
  }
  $("resume").hidden = !saved || saved.status === "complete" || running || !selectedProject;
  $("stop").hidden = !running;
  $("start").disabled = Boolean(taskInfo?.busy);
  const messages = taskInfo?.messages || [];
  const log = $("activity-log");
  const wasAtBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 30;
  log.textContent = messages.length ? messages.join("\n") : "No activity yet.";
  if (wasAtBottom) log.scrollTop = log.scrollHeight;
}

function renderShared() {
  const unavailable = sharedUnavailable;
  const data = sharedInfo;
  $("compatibility").hidden = !unavailable;
  const current = data?.accounts?.find((account) => account.name === data.current);
  const imported = Number(data?.imported_count || 0);
  const isEnabled = Boolean(data?.enabled);
  const prepared = Boolean(data?.prepared);
  const needsKeychainUpdate = Boolean(data?.needs_keychain_update);
  $("shared-state").textContent = unavailable ? "Needs restart" : needsKeychainUpdate ? "Update needed" : isEnabled ? "On" : prepared ? "Off" : "Not set up";
  $("shared-state").className = "state-label" + (isEnabled ? " running" : unavailable ? " paused" : "");
  $("shared-description").textContent = unavailable
    ? "This launcher is too old for the automatic switching controls."
    : needsKeychainUpdate
      ? "Update your saved account access once to stop repeated Keychain password requests during switching."
    : isEnabled
      ? "Switchboard checks your current session usage and switches at the saved threshold."
      : prepared
        ? "Your accounts are prepared. Turn on switching to watch your local terminal chats."
        : "Prepare your signed-in accounts, then turn on switching.";
  $("shared-current").textContent = unavailable ? "Unavailable until restart" : current?.display_name || "Not matched yet";
  $("shared-readiness").textContent = unavailable ? "Your account profiles are still available below."
    : imported + " account" + (imported === 1 ? "" : "s") + " prepared for switching";
  const usage = data?.session_usage;
  const recentUsage = usage?.account === data?.current && Date.now() / 1000 - Number(usage.checked_at || 0) < 180;
  const threshold = accounts.find((account) => account.name === data?.current)?.threshold_percent ?? 95;
  const lastActivity = Math.max(Number(usage?.checked_at) || 0, Number(data?.enabled_at) || 0,
    Number((data?.history || []).slice(-1)[0]?.at) || 0);
  $("shared-session-usage").textContent = !isEnabled ? "Session usage checks start when switching is on."
    : needsKeychainUpdate ? "Session checks resume after the Keychain update."
    : recentUsage && Number.isFinite(usage.percent) ? `Current session: ${usage.percent}% used · switch at ${threshold}%`
      : recentUsage && usage.error ? "Session percentage unavailable. " + (data?.fallback_on_limit !== false ? "Limit fallback is on." : "Limit fallback is off.")
        : Date.now() / 1000 - lastActivity > 300
          ? "The usage monitor is not responding (last check: " + formatDate(usage?.checked_at * 1000) + "). If this persists, turn switching off and on."
          : "Checking the current session percentage…";
  $("shared-usage-note").textContent = isEnabled && recentUsage && usage.error
    ? usage.error
    : "Only current session usage is checked. Weekly usage is not used for switching.";
  $("fallback-toggle").checked = data?.fallback_on_limit !== false;
  $("fallback-toggle").disabled = unavailable || !prepared || Boolean(taskInfo?.busy);
  $("shared-prepare").hidden = unavailable || (prepared && isEnabled);
  $("shared-prepare").textContent = prepared ? "Update prepared accounts" : "Prepare accounts";
  $("shared-prepare").disabled = Boolean(taskInfo?.busy);
  $("shared-enable").hidden = unavailable || isEnabled || needsKeychainUpdate;
  $("shared-enable").disabled = !prepared || !data?.current || Boolean(taskInfo?.busy);
  $("shared-migrate").hidden = unavailable || !needsKeychainUpdate;
  $("shared-migrate").disabled = Boolean(taskInfo?.busy);
  $("shared-disable").hidden = unavailable || !isEnabled;
  $("shared-disable").disabled = Boolean(taskInfo?.busy);
  $("shared-action-help").textContent = unavailable
    ? "Close the old launcher window and open it again to use these controls."
    : needsKeychainUpdate
      ? "macOS may ask you to approve access to each older saved login during this one-time update. Afterward, normal switches use Apple's signed Keychain utility."
    : !prepared
      ? "At least two accounts must be signed in before preparation can finish."
      : !data?.current
        ? "Choose an account below and switch to it before turning on automatic switching."
        : isEnabled
          ? "Switching stays on after you close this dashboard."
          : "Turn it on, then start new Claude chats so they load the limit detector.";
  const operation = $("shared-operation");
  const operationDetails = $("shared-operation-details");
  const loginName = taskInfo?.kind?.startsWith("login:") ? taskInfo.kind.slice(6) : "";
  const sharedRun = taskInfo?.kind?.startsWith("shared-") || Boolean(loginName);
  const actionName = sharedRun ? taskInfo.kind.slice(7) : "";
  const actionLabel = loginName
    ? "Waiting for you to finish signing in to " + (accounts.find((account) => account.name === loginName)?.display_name || loginName) + " in your browser"
    : { setup: "Preparing accounts", enable: "Turning on switching", disable: "Turning off switching", switch: "Switching account", migrate: "Updating saved account access" }[actionName] || "Updating switching";
  const operationBusy = sharedRun && taskInfo?.busy;
  const operationFailed = sharedRun && taskInfo?.exit_code !== null && taskInfo?.exit_code !== undefined && taskInfo.exit_code !== 0;
  operation.hidden = !operationBusy && !operationFailed;
  operation.className = "operation" + (operationFailed ? " failed" : "");
  operation.textContent = operationBusy ? actionLabel + "…" : operationFailed ? "That change did not finish. See the details below and try again." : "";
  operationDetails.hidden = !operationFailed;
  if (operationFailed) $("shared-operation-log").textContent = (taskInfo?.messages || []).join("\n");
  $("shared-cancel").hidden = !(loginName && operationBusy);
  const select = $("shared-account");
  const selected = select.value;
  const choices = (data?.accounts || []).filter((account) => account.prepared !== false);
  const choicesKey = JSON.stringify(choices.map((account) => [account.name, account.display_name]));
  if (choicesKey !== sharedChoicesKey) {
    select.replaceChildren(...choices.map((account) => {
      const option = node("option", "", account.display_name);
      option.value = account.name;
      return option;
    }));
    sharedChoicesKey = choicesKey;
  }
  if (choices.some((account) => account.name === selected)) select.value = selected;
  else if (data?.current && choices.some((account) => account.name === data.current)) select.value = data.current;
  $("shared-switch").disabled = unavailable || !prepared || needsKeychainUpdate || !choices.length || Boolean(taskInfo?.busy);
  select.disabled = $("shared-switch").disabled;
  const history = $("shared-history");
  history.replaceChildren(...(data?.history || []).slice(-3).reverse().map((event) => {
    const account = data.accounts.find((item) => item.name === event.to);
    const item = node("div", "run-item");
    item.append(node("span", "run-account", account?.display_name || event.to),
      node("span", "run-outcome", event.reason === "rate_limit" ? "Usage limit" : String(event.reason).includes("% usage") ? "Session threshold" : "Manual switch"));
    return item;
  }));
  $("shared-error").hidden = !data?.last_error;
  $("shared-error").textContent = data?.last_error ? "Needs attention: " + data.last_error : "";
  renderAccounts();
}

async function refreshAccounts() {
  const data = await api("/api/accounts");
  accounts = data.accounts || [];
  accountsLoaded = true;
  renderAccounts();
}

async function refreshTask() {
  const project = $("project").value.trim();
  const query = project ? "?project=" + encodeURIComponent(project) : "";
  taskInfo = await api("/api/task" + query);
  renderTask();
  if (sharedInfo) renderShared();
}

async function refreshShared(force = false) {
  if (sharedUnavailable && !force) return;
  try {
    sharedInfo = await api("/api/shared");
    sharedUnavailable = false;
    renderShared();
  } catch (error) {
    if (error.status === 404) {
      sharedInfo = null;
      sharedUnavailable = true;
      renderShared();
      return;
    }
    throw error;
  }
}

async function refreshAll(force = false) {
  const results = await Promise.allSettled([refreshAccounts(), refreshTask(), refreshShared(force)]);
  const error = results.find((result) => result.status === "rejected");
  $("connection").classList.toggle("offline", Boolean(error));
  $("connection").textContent = error ? "Disconnected" : "Connected locally";
  if (error) notice(friendlyError(error.reason), "error");
}

function validateTask() {
  if (!$("project").value.trim()) {
    notice("Choose a project folder first.", "error");
    $("project").focus();
    return false;
  }
  if (!$("task").value.trim()) {
    notice("Describe what Claude should do.", "error");
    $("task").focus();
    return false;
  }
  return true;
}

$("tab-automatic").addEventListener("click", () => setView("automatic"));
$("tab-task").addEventListener("click", () => setView("task"));
setView(sessionStorage.getItem("switchboard-view") === "task" ? "task" : "automatic");
$("project").value = localStorage.getItem("switchboard-project") || "";
$("project").addEventListener("change", () => {
  localStorage.setItem("switchboard-project", $("project").value.trim());
  refreshTask().catch((error) => notice(friendlyError(error), "error"));
});
$("task-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!validateTask()) return;
  const project = $("project").value.trim();
  localStorage.setItem("switchboard-project", project);
  try {
    await api("/api/run", { project, task: $("task").value.trim() });
    notice("Task started. You can watch its progress here.", "success");
    await refreshTask();
  } catch (error) { notice(friendlyError(error), "error"); }
});
$("resume").addEventListener("click", async () => {
  try {
    await api("/api/resume", { project: $("project").value.trim() });
    notice("Saved task is continuing.", "success");
    await refreshTask();
  } catch (error) { notice(friendlyError(error), "error"); }
});
$("stop").addEventListener("click", async () => {
  try {
    await api("/api/stop", {});
    notice("Stop requested. Progress will be saved.");
  } catch (error) { notice(friendlyError(error), "error"); }
});
$("browse").addEventListener("click", async () => {
  try {
    const result = await api("/api/pick-project", {});
    $("project").value = result.project;
    localStorage.setItem("switchboard-project", result.project);
    await refreshTask();
  } catch (error) { notice(friendlyError(error), "error"); }
});
$("account-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const name = $("account-name").value.trim();
  const email = $("account-email").value.trim();
  if (!/^[A-Za-z0-9][A-Za-z0-9_-]*$/.test(name)) {
    notice("Enter a profile ID using letters, numbers, hyphens, or underscores.", "error");
    $("account-name").focus();
    return;
  }
  if (email && !$("account-email").checkValidity()) {
    notice("Enter a valid email address or leave it blank.", "error");
    $("account-email").focus();
    return;
  }
  try {
    await api("/api/accounts", { name, display_name: $("account-display-name").value.trim(), email });
    $("account-form").reset();
    notice("Account added. Sign in, then prepare accounts again.", "success");
    await refreshAccounts();
    await refreshShared();
  } catch (error) { notice(friendlyError(error), "error"); }
});
$("refresh").addEventListener("click", () => refreshAll(true));

async function sharedAction(action, account = "") {
  const labels = { setup: "Preparing accounts", enable: "Turning on automatic switching", disable: "Turning off automatic switching", switch: "Switching account", migrate: "Updating saved account access" };
  try {
    await api("/api/shared", { action, account });
    notice(labels[action] + "…", "success");
    await refreshTask();
  } catch (error) { notice(friendlyError(error), "error"); }
}
$("shared-prepare").addEventListener("click", () => sharedAction("setup"));
$("shared-migrate").addEventListener("click", () => sharedAction("migrate"));
$("shared-enable").addEventListener("click", () => sharedAction("enable"));
$("shared-disable").addEventListener("click", () => sharedAction("disable"));
$("shared-switch").addEventListener("click", () => sharedAction("switch", $("shared-account").value));
$("shared-cancel").addEventListener("click", async () => {
  try {
    await api("/api/stop", {});
    await refreshTask();
  } catch (error) { notice(friendlyError(error), "error"); }
});
$("fallback-toggle").addEventListener("change", async () => {
  const toggle = $("fallback-toggle");
  toggle.disabled = true;
  try {
    await api("/api/fallback", { enabled: toggle.checked });
    notice(toggle.checked ? "Limit fallback is on." : "Limit fallback is off.", "success");
    await refreshShared();
  } catch (error) {
    toggle.checked = !toggle.checked;
    notice(friendlyError(error), "error");
  } finally { toggle.disabled = false; }
});
$("account-dialog-close").addEventListener("click", () => $("account-dialog").close());
$("account-dialog").addEventListener("close", () => {
  selectedAccountInfo = null;
  pendingAccountAction = null;
  $("account-confirm").hidden = true;
});
$("account-logout").addEventListener("click", () => confirmAccountAction("logout"));
$("account-delete").addEventListener("click", () => confirmAccountAction("delete"));
$("account-confirm-cancel").addEventListener("click", () => { $("account-confirm").hidden = true; pendingAccountAction = null; });
$("account-confirm-proceed").addEventListener("click", async () => {
  if (!selectedAccountInfo || !pendingAccountAction) return;
  const action = pendingAccountAction;
  const name = selectedAccountInfo.name;
  $("account-confirm-proceed").disabled = true;
  try {
    await api(action === "delete" ? "/api/delete-account" : "/api/logout", { name });
    $("account-dialog").close();
    notice(action === "delete" ? "Account deleted from Switchboard." : "Account signed out and removed from rotation.", "success");
    await refreshAll(true);
  } catch (error) {
    $("account-dialog-error").textContent = friendlyError(error);
    $("account-dialog-error").hidden = false;
  }
  finally { $("account-confirm-proceed").disabled = false; }
});

async function start() {
  const code = linkParams.get("code");
  if (code) {
    try {
      const response = await fetch("/api/redeem", { method: "POST", credentials: "omit",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code }) });
      const result = await response.json().catch(() => ({}));
      if (response.ok && result.token) {
        token = result.token;
        sessionStorage.setItem("switchboard-token", token);
      }
    } catch (_) { /* the notice below explains what to do */ }
  }
  if (!token) {
    notice("Open Switchboard using the link printed by its launcher.", "error");
    return;
  }
  refreshAll();
  setInterval(async () => {
    const results = await Promise.allSettled([refreshTask(), refreshShared()]);
    const failed = results.find((result) => result.status === "rejected");
    if (failed && !$("connection").classList.contains("offline")) notice(friendlyError(failed.reason), "error");
    $("connection").classList.toggle("offline", Boolean(failed));
    $("connection").textContent = failed ? "Disconnected" : "Connected locally";
  }, 2500);
  setInterval(() => {
    if (!document.activeElement?.matches(".account-settings input")) refreshAccounts().catch(() => {});
  }, 15000);
}
start();
