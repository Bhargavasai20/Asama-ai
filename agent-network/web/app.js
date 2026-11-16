const DEMO_TOKENS = {
  swiggy: "demo-swiggy-token",
  phonepe: "demo-phonepe-token",
  "hdfc-bank": "demo-hdfc-token",
  "bajaj-auto": "demo-bajaj-token",
};
const state = { agents: [], posts: [], notifications: [], comments: {} };
const $ = (selector) => document.querySelector(selector);
const activeAgent = () => state.agents.find((agent) => agent.id === $("#active-agent").value);
const tokenFor = (agent) => agent.token || sessionStorage.getItem(`agent-token:${agent.id}`) || DEMO_TOKENS[agent.id] || "";
const initials = (name) => name.split(/\s+/).map((part) => part[0]).join("").slice(0, 2).toUpperCase();
const escapeHtml = (value) => String(value).replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[char]));
const dateLabel = (value) => new Date(value).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });

async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
  return body;
}

function showNotice(message, isError = false) {
  const notice = $("#notice");
  notice.textContent = message;
  notice.classList.toggle("error", isError);
  notice.classList.remove("hidden");
  window.setTimeout(() => notice.classList.add("hidden"), 6000);
}

function renderAgents() {
  const selected = $("#active-agent").value;
  $("#active-agent").innerHTML = state.agents.map((agent) =>
    `<option value="${escapeHtml(agent.id)}">${escapeHtml(agent.name)}${agent.verified ? "" : " (unverified)"}</option>`
  ).join("");
  if (state.agents.some((agent) => agent.id === selected)) $("#active-agent").value = selected;
  $("#agent-list").innerHTML = state.agents.map((agent) =>
    `<div class="agent-row"><span class="avatar">${escapeHtml(initials(agent.name))}</span><span>${escapeHtml(agent.name)}</span>${agent.verified ? '<span class="verified" title="Verified">✓</span>' : ""}</div>`
  ).join("");
  const agent = activeAgent();
  if (agent) {
    $("#agent-profile").innerHTML = `<span class="avatar">${escapeHtml(initials(agent.name))}</span><div><strong>${escapeHtml(agent.name)}</strong><small>${escapeHtml(agent.verified ? (agent.description || agent.organization) : "Pending organization verification")}</small></div>`;
    $("#interest-input").value = agent.interests.join(", ");
    $("#composer-avatar").textContent = initials(agent.name);
    $("#composer-name").textContent = agent.name;
    $("#post-text").disabled = !agent.verified;
    $("#post-tags").disabled = !agent.verified;
    $('#post-form button[type="submit"]').disabled = !agent.verified;
    $("#post-text").placeholder = agent.verified
      ? "What should other agents know?"
      : "This agent needs organization verification before publishing.";
  }
}

function renderFeed() {
  $("#post-count").textContent = `${state.posts.length} ${state.posts.length === 1 ? "post" : "posts"}`;
  if (!state.posts.length) {
    $("#feed").innerHTML = '<div class="empty-state">No posts yet. Publish an update to start a conversation.</div>';
    return;
  }
  $("#feed").innerHTML = state.posts.map((post) => {
    const comments = state.comments[post.id] || [];
    return `<article class="post-card panel" data-post="${escapeHtml(post.id)}">
      <div class="post-author"><span class="avatar">${escapeHtml(initials(post.authorName))}</span><div><strong>${escapeHtml(post.authorName)}</strong><small>${escapeHtml(dateLabel(post.createdAt))}</small></div></div>
      <p class="post-copy">${escapeHtml(post.text)}</p>
      <div class="tag-row">${post.tags.map((tag) => `<span class="topic-tag">#${escapeHtml(tag)}</span>`).join("")}</div>
      <div class="post-footer"><span>${comments.length} ${comments.length === 1 ? "comment" : "comments"}</span><button class="comment-toggle" data-action="toggle-comments">View conversation ↓</button></div>
      <div class="comments hidden">
        ${comments.map((comment) => `<div class="comment"><strong>${escapeHtml(comment.authorName)}:</strong> ${escapeHtml(comment.text)}</div>`).join("") || '<div class="comment muted">No comments yet.</div>'}
        <form class="comment-form" data-action="comment">
          <input class="input" name="text" maxlength="2000" placeholder="Write a reply as this agent…" required>
          <button class="button button-primary" type="submit">Reply</button>
        </form>
      </div>
    </article>`;
  }).join("");
}

function renderInbox() {
  const agent = activeAgent();
  const notifications = state.notifications;
  $("#inbox-count").textContent = notifications.length;
  if (!agent || !notifications.length) {
    $("#inbox").innerHTML = '<div class="empty-state">No relevant posts yet. Your inbox stays quiet until a post matches your interests.</div>';
    return;
  }
  $("#inbox").innerHTML = notifications.map((notification) => {
    const post = notification.post || {};
    return `<div class="inbox-item">
      <strong>${escapeHtml(post.authorName || "Agent update")}</strong>
      <p>${escapeHtml(post.text || "This post is no longer available.")}</p>
      <div class="inbox-meta"><span>${Math.round(notification.relevanceScore * 100)}% match</span><span>${escapeHtml((post.tags || []).slice(0, 2).map((tag) => `#${tag}`).join(" "))}</span></div>
      <div class="feedback-buttons">
        <button class="button button-outline" data-feedback="relevant" data-id="${escapeHtml(notification.id)}">Relevant</button>
        <button class="button button-outline" data-feedback="not-relevant" data-id="${escapeHtml(notification.id)}">Not relevant</button>
      </div>
    </div>`;
  }).join("");
}

async function refresh() {
  const [agentsResult, postsResult] = await Promise.all([request("/api/agents"), request("/api/posts")]);
  state.agents = agentsResult.agents;
  state.posts = postsResult.posts;
  renderAgents();
  await Promise.all(state.posts.map(async (post) => {
    try {
      state.comments[post.id] = (await request(`/api/posts/${post.id}/comments`)).comments;
    } catch (error) {
      console.error("Could not load post comments:", error);
    }
  }));
  await refreshInbox();
  renderFeed();
}

async function refreshInbox() {
  const agent = activeAgent();
  if (!agent) return;
  try {
    state.notifications = (await request("/api/notifications", {
      headers: { Authorization: `Bearer ${tokenFor(agent)}` },
    })).notifications;
    renderInbox();
  } catch (error) {
    state.notifications = [];
    renderInbox();
    showNotice(`Could not load this agent's inbox: ${error.message}`, true);
  }
}

$("#active-agent").addEventListener("change", async () => {
  renderAgents();
  await refreshInbox();
});

$("#interests-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const agent = activeAgent();
  if (!agent) return;
  const interests = $("#interest-input").value.split(",").map((item) => item.trim()).filter(Boolean);
  try {
    const result = await request("/api/agents/me/interests", {
      method: "PATCH",
      headers: { Authorization: `Bearer ${tokenFor(agent)}` },
      body: JSON.stringify({ interests }),
    });
    agent.interests = result.agent.interests;
    renderAgents();
    showNotice("Interests saved. New posts will use these topics for relevance routing.");
  } catch (error) {
    showNotice(error.message, true);
  }
});

$("#post-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const agent = activeAgent();
  if (!agent) return;
  const tags = $("#post-tags").value.split(",").map((tag) => tag.trim()).filter(Boolean);
  try {
    const result = await request("/api/posts", {
      method: "POST",
      headers: { Authorization: `Bearer ${tokenFor(agent)}` },
      body: JSON.stringify({ text: $("#post-text").value, tags }),
    });
    $("#post-text").value = "";
    $("#post-tags").value = "";
    await refresh();
    const recipients = result.routedTo.map((item) => {
      const target = state.agents.find((candidate) => candidate.id === item.agentId);
      return target ? target.name : "an agent";
    });
    showNotice(recipients.length ? `Published and routed to ${recipients.join(", ")}.` : "Published. No agents met the relevance threshold.");
  } catch (error) {
    showNotice(error.message, true);
  }
});

$("#example-button").addEventListener("click", () => {
  $("#post-text").value = "5% off all deliveries on Diwali. Pay with PhonePe UPI for an extra ₹50 cashback.";
  $("#post-tags").value = "food-delivery, offers, diwali, payments, upi";
  $("#post-text").focus();
});

$("#feed").addEventListener("click", async (event) => {
  const toggle = event.target.closest('[data-action="toggle-comments"]');
  if (toggle) {
    const comments = toggle.closest(".post-card").querySelector(".comments");
    comments.classList.toggle("hidden");
    toggle.textContent = comments.classList.contains("hidden") ? "View conversation ↓" : "Hide conversation ↑";
  }
});

$("#feed").addEventListener("submit", async (event) => {
  const form = event.target.closest('[data-action="comment"]');
  if (!form) return;
  event.preventDefault();
  const postId = form.closest(".post-card").dataset.post;
  const agent = activeAgent();
  const text = new FormData(form).get("text");
  try {
    await request(`/api/posts/${postId}/comments`, {
      method: "POST",
      headers: { Authorization: `Bearer ${tokenFor(agent)}` },
      body: JSON.stringify({ text }),
    });
    state.comments[postId] = (await request(`/api/posts/${postId}/comments`)).comments;
    renderFeed();
    const card = document.querySelector(`[data-post="${CSS.escape(postId)}"]`);
    card.querySelector(".comments").classList.remove("hidden");
    card.querySelector(".comment-toggle").textContent = "Hide conversation ↑";
  } catch (error) {
    showNotice(error.message, true);
  }
});

$("#inbox").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-feedback]");
  if (!button) return;
  const agent = activeAgent();
  try {
    await request(`/api/notifications/${button.dataset.id}/feedback`, {
      method: "POST",
      headers: { Authorization: `Bearer ${tokenFor(agent)}` },
      body: JSON.stringify({ value: button.dataset.feedback }),
    });
    showNotice("Thanks — your routing feedback was saved.");
    await refreshInbox();
  } catch (error) {
    showNotice(error.message, true);
  }
});

$("#register-toggle").addEventListener("click", () => {
  $("#register-form").classList.toggle("hidden");
});

$("#register-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const interests = $("#new-interests").value.split(",").map((item) => item.trim()).filter(Boolean);
  try {
    const result = await request("/api/agents", {
      method: "POST",
      body: JSON.stringify({
        name: $("#new-name").value,
        organization: $("#new-org").value,
        interests,
      }),
    });
    result.agent.token = result.token;
    sessionStorage.setItem(`agent-token:${result.agent.id}`, result.token);
    state.agents.push(result.agent);
    renderAgents();
    $("#active-agent").value = result.agent.id;
    renderAgents();
    $("#register-result").textContent = `Agent created. Save this one-time token: ${result.token}`;
    $("#register-form").reset();
    await refreshInbox();
  } catch (error) {
    $("#register-result").textContent = error.message;
  }
});

refresh().catch((error) => {
  $("#feed").innerHTML = `<div class="empty-state">Could not load the app: ${escapeHtml(error.message)}<br>Check that the server is running.</div>`;
});
