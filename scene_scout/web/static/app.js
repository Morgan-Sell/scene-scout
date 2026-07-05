/**
 * SceneScout onboarding & profile — vanilla JS SPA.
 */

const HORIZON_DAYS_MIN = 1;
const HORIZON_DAYS_MAX = 60;

const tabs = document.querySelectorAll(".tab");
const panels = document.querySelectorAll(".panel");
const form = document.getElementById("onboarding-form");
const statusEl = document.getElementById("onboarding-status");
const summaryEl = document.getElementById("profile-summary");
const profileDisplay = document.getElementById("profile-display");

function validateOnboarding(homeCity, horizonDays, name, email, prompt) {
  if (!homeCity.trim()) {
    return "Home city is required.";
  }
  const horizon = Number(horizonDays);
  if (
    !Number.isInteger(horizon) ||
    horizon < HORIZON_DAYS_MIN ||
    horizon > HORIZON_DAYS_MAX
  ) {
    return `Horizon must be between ${HORIZON_DAYS_MIN} and ${HORIZON_DAYS_MAX} days.`;
  }
  if (!name.trim()) {
    return "Name is required.";
  }
  if (!email.trim()) {
    return "Email is required.";
  }
  if (!email.includes("@")) {
    return "Email must look like a valid address.";
  }
  if (!prompt.trim()) {
    return "Your taste is required.";
  }
  return null;
}

function showStatus(message, isError) {
  statusEl.hidden = false;
  statusEl.textContent = message;
  statusEl.className = isError ? "status-message error" : "status-message success";
}

function clearStatus() {
  statusEl.hidden = true;
  statusEl.textContent = "";
  statusEl.className = "status-message";
}

function formatList(items) {
  if (!items || items.length === 0) {
    return "<p><em>None</em></p>";
  }
  const lis = items.map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  return `<ul>${lis}</ul>`;
}

function formatOptional(value, suffix = "") {
  if (value === null || value === undefined) {
    return "<p><em>Not set</em></p>";
  }
  return `<p>${escapeHtml(String(value))}${suffix}</p>`;
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

function renderProfile(profile, container) {
  const weights = JSON.stringify(profile.category_weights || {}, null, 2);
  container.innerHTML = `
    <h2>Your profile</h2>
    <div class="profile-section">
      <h3>Location &amp; horizon</h3>
      <p>${escapeHtml(profile.home_city)} · ${escapeHtml(String(profile.horizon_days))} days ahead</p>
    </div>
    <div class="profile-section">
      <h3>Name &amp; email</h3>
      <p>${escapeHtml(profile.name)} · ${escapeHtml(profile.email)}</p>
    </div>
    <div class="profile-section">
      <h3>Stated interests</h3>
      ${formatList(profile.stated_interests)}
    </div>
    <div class="profile-section">
      <h3>Stated dislikes</h3>
      ${formatList(profile.stated_dislikes)}
    </div>
    <div class="profile-section">
      <h3>Preferred neighborhoods</h3>
      ${formatList(profile.preferred_neighborhoods)}
    </div>
    <div class="profile-section">
      <h3>Vibe preferences</h3>
      ${formatList(profile.vibe_preferences)}
    </div>
    <div class="profile-section">
      <h3>Excluded categories</h3>
      ${formatList(profile.excluded_categories)}
    </div>
    <div class="profile-section">
      <h3>Category weights</h3>
      <pre>${escapeHtml(weights)}</pre>
    </div>
    <div class="profile-section">
      <h3>Travel &amp; budget</h3>
      ${formatOptional(profile.max_travel_minutes, " min travel")}
      ${formatOptional(profile.budget_ceiling_cents, " cents budget ceiling")}
    </div>
    <div class="profile-section">
      <h3>Metadata</h3>
      <p>Profile version ${escapeHtml(String(profile.profile_version))}</p>
      <p>Last updated ${escapeHtml(profile.last_updated)}</p>
    </div>
  `;
  container.hidden = false;
}

async function loadProfile() {
  profileDisplay.innerHTML = '<p class="empty-state">Loading profile…</p>';
  try {
    const response = await fetch("/api/profile");
    const data = await response.json();
    if (!response.ok) {
      profileDisplay.innerHTML =
        '<p class="empty-state">No profile yet — complete onboarding to tell Allegra what you love.</p>';
      return;
    }
    renderProfile(data, profileDisplay);
  } catch {
    profileDisplay.innerHTML =
      '<p class="empty-state">Could not load profile. Try again in a moment.</p>';
  }
}

function activateTab(tabName) {
  tabs.forEach((tab) => {
    const isActive = tab.dataset.tab === tabName;
    tab.classList.toggle("active", isActive);
    tab.setAttribute("aria-selected", isActive ? "true" : "false");
  });

  panels.forEach((panel) => {
    const isOnboarding = panel.id === "panel-onboarding";
    const isProfile = panel.id === "panel-profile";
    const show =
      (tabName === "onboarding" && isOnboarding) ||
      (tabName === "profile" && isProfile);
    panel.classList.toggle("active", show);
    panel.hidden = !show;
  });

  if (tabName === "profile") {
    loadProfile();
  }
}

tabs.forEach((tab) => {
  tab.addEventListener("click", () => activateTab(tab.dataset.tab));
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  clearStatus();

  const homeCity = form.home_city.value;
  const horizonDays = form.horizon_days.value;
  const name = form.name.value;
  const email = form.email.value;
  const prompt = form.prompt.value;

  const validationError = validateOnboarding(
    homeCity,
    horizonDays,
    name,
    email,
    prompt,
  );
  if (validationError) {
    showStatus(validationError, true);
    return;
  }

  const button = form.querySelector(".cta-button");
  button.disabled = true;

  try {
    const response = await fetch("/api/onboarding", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        home_city: homeCity.trim(),
        horizon_days: Number(horizonDays),
        name,
        email,
        prompt,
      }),
    });
    const data = await response.json();

    if (!response.ok) {
      showStatus(data.error || "Something went wrong.", true);
      summaryEl.hidden = true;
      return;
    }

    showStatus(`Profile saved for ${data.name}. Allegra is ready.`, false);
    renderProfile(data, summaryEl);
    summaryEl.hidden = false;
  } catch {
    showStatus("Could not reach the server. Try again.", true);
    summaryEl.hidden = true;
  } finally {
    button.disabled = false;
  }
});
