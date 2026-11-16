const $ = (selector) => document.querySelector(selector);
const history = [];
let busy = false;
const escapeHtml = (value) => String(value).replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[char]));

async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
  return body;
}

function renderMessage(role, text, toolResults = []) {
  const row = document.createElement("div");
  row.className = `message ${role}`;
  const safeText = escapeHtml(text);
  row.innerHTML = role === "assistant"
    ? `<span class="message-avatar">◈</span><div class="message-body">${safeText}</div>`
    : `<div class="message-body">${safeText}</div>`;
  $("#messages").append(row);
  if (toolResults.length) {
    const details = document.createElement("details");
    details.className = "tool-detail";
    details.innerHTML = `<summary>${toolResults.length} read-only inspection${toolResults.length === 1 ? "" : "s"} performed</summary><pre>${escapeHtml(JSON.stringify(toolResults, null, 2))}</pre>`;
    $("#messages").append(details);
  }
  row.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function sendMessage(message) {
  if (busy || !message.trim()) return;
  busy = true;
  $("#welcome").classList.add("hidden");
  $("#send-button").disabled = true;
  renderMessage("user", message);
  const loading = document.createElement("div");
  loading.className = "message assistant";
  loading.innerHTML = '<span class="message-avatar">◈</span><div class="message-body"><span class="typing"><i></i><i></i><i></i></span></div>';
  $("#messages").append(loading);
  try {
    const result = await request("/api/chat", {
      method: "POST",
      body: JSON.stringify({ message, history: history.slice(-10) }),
    });
    loading.remove();
    renderMessage("assistant", result.answer, result.toolResults || []);
    history.push({ role: "user", content: message }, { role: "assistant", content: result.answer });
  } catch (error) {
    loading.remove();
    renderMessage("assistant", `I couldn't complete that inspection: ${error.message}`);
  } finally {
    busy = false;
    $("#send-button").disabled = false;
    $("#message-input").focus();
  }
}

$("#chat-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const input = $("#message-input");
  const message = input.value.trim();
  if (!message) return;
  input.value = "";
  sendMessage(message);
});

$("#message-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("#chat-form").requestSubmit();
  }
});

document.querySelectorAll(".suggestion").forEach((button) => {
  button.addEventListener("click", () => sendMessage(button.dataset.question));
});

request("/api/health").then((status) => {
  $("#platform-label").textContent = status.platform;
  const labels = {
    offline: ["OFFLINE MODE", "Offline intent mode"],
    "local-model": ["LOCAL MODEL", "Local model with read-only tools"],
    "remote-model": ["REMOTE MODEL", "Remote endpoint receives hardware context"],
  };
  const [badge, note] = labels[status.modelMode] || labels.offline;
  $(".mode-badge").innerHTML = `<span></span> ${badge}`;
  $("#mode-note").textContent = note;
}).catch((error) => {
  $("#platform-label").textContent = `Unavailable: ${error.message}`;
});

request("/api/tools").then(({ tools }) => {
  const symbols = ["▤", "⌘", "▱", "▧", "⊞", "◈"];
  $("#tool-list").innerHTML = tools.map((tool, index) =>
    `<div class="tool-item"><span class="tool-symbol">${symbols[index] || "◦"}</span><span>${escapeHtml(tool.name.replace(/^get_/, "").replace(/_/g, " "))}</span></div>`
  ).join("");
}).catch((error) => {
  $("#tool-list").innerHTML = `<span class="loading">Could not load tools: ${escapeHtml(error.message)}</span>`;
});
