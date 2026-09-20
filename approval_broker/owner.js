"use strict";

let ownerToken = null;
const status = document.getElementById("status");
const requests = document.getElementById("requests");
const sessions = document.getElementById("sessions");
const profiles = document.getElementById("profiles");
const totpState = document.getElementById("totp-state");
const totpSecret = document.getElementById("totp-secret");
const totpKey = document.getElementById("totp-key");
const takeoverUrl = "http://127.0.0.1:16080/vnc.html?autoconnect=true&resize=scale";
const statusNames = {
  pending: "ينتظر موافقتك", approved: "مسموح في الجلسة الحالية", denied: "مرفوض لهذه الجلسة",
  revoked: "أُوقف لهذه الجلسة", expired: "انتهت المهلة", completed: "اكتملت المهمة",
};

async function api(path, method = "GET", body = null) {
  if (!ownerToken) throw new Error("أدخل رمز المالك أولاً.");
  const response = await fetch(path, {
    method,
    headers: {
      Authorization: `Bearer ${ownerToken}`,
      ...(body ? { "Content-Type": "application/json" } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
    cache: "no-store",
    credentials: "omit",
  });
  if (!response.ok) {
    if (response.status === 401) ownerToken = null;
    throw new Error(`فشل الطلب (${response.status}).`);
  }
  return response.json();
}

function phoneCode() {
  const code = window.prompt("أدخل الرمز الحالي من تطبيق المصادقة") || "";
  if (!/^[0-9]{6}$/.test(code)) throw new Error("الرمز يجب أن يتكون من ستة أرقام.");
  return code;
}

function cell(label, value) {
  const row = document.createElement("p");
  const heading = document.createElement("strong");
  heading.textContent = `${label}: `;
  row.append(heading, document.createTextNode(String(value ?? "")));
  return row;
}

function button(label, handler) {
  const element = document.createElement("button");
  element.type = "button";
  element.textContent = label;
  element.addEventListener("click", handler);
  return element;
}

async function refresh() {
  if (!ownerToken) return;
  try {
    const [entries, sessionEntries, profileEntries, authenticator] = await Promise.all([
      api("/requests"), api("/owner/sessions"), api("/owner/auth-profiles"), api("/owner/totp"),
    ]);
    totpState.textContent = authenticator.status === "active" ? "تطبيق المصادقة مفعّل." :
      authenticator.status === "pending" ? "الإعداد بانتظار إثبات أول رمز." : "تطبيق المصادقة لم يُفعّل بعد.";
    document.getElementById("setup-totp").hidden = authenticator.status === "active";
    document.getElementById("activate-totp").hidden = authenticator.status === "active";
    if (authenticator.status === "active") {
      totpKey.textContent = "";
      totpSecret.hidden = true;
    }
    requests.replaceChildren();
    sessions.replaceChildren();
    profiles.replaceChildren();
    const activeSessions = sessionEntries.filter((item) => item.status === "active");
    if (activeSessions.length === 0) sessions.append(cell("الجلسة الحالية", "لا توجد"));
    for (const session of activeSessions) {
      const section = document.createElement("section");
      section.append(cell("الجلسة", session.id), cell("الاسم", session.name));
      const link = document.createElement("a");
      link.href = takeoverUrl;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = "فتح المتصفح المرئي الخاص";
      section.append(link, document.createElement("br"));
      const profileInput = document.createElement("input");
      profileInput.type = "text";
      profileInput.placeholder = "اسم الملف الشخصي";
      profileInput.maxLength = 120;
      profileInput.setAttribute("aria-label", "اسم الملف الشخصي");
      section.append(profileInput);
      section.append(button("حفظ الملف بعد الدخول", () => saveProfile(session.id, profileInput.value)));
      section.append(button("إغلاق الجلسة", () => closeSession(session.id)));
      section.append(document.createElement("hr"));
      sessions.append(section);
    }
    if (profileEntries.length === 0) profiles.append(cell("الملفات المحفوظة", "لا توجد"));
    const profileSelect = document.getElementById("login-profile");
    const selectedProfile = profileSelect.value;
    profileSelect.replaceChildren(new Option("جلسة جديدة بلا ملف محفوظ", ""));
    for (const profile of profileEntries) {
      const name = profile.name || profile.profile_name;
      profiles.append(cell("الملف", name || JSON.stringify(profile)));
      if (name) profileSelect.add(new Option(name, name));
    }
    if ([...profileSelect.options].some((option) => option.value === selectedProfile)) profileSelect.value = selectedProfile;
    for (const entry of entries) {
      const section = document.createElement("section");
      section.append(
        cell("المساعد", entry.agent_id), cell("الغرض", entry.purpose),
        cell("الحالة", statusNames[entry.status] || entry.status),
        cell("رقم الطلب", entry.id),
      );
      if (entry.status === "approved") {
        section.append(button("إيقاف وصول المساعد لهذه الجلسة", () => decide(entry.id, "revoke")));
      }
      section.append(document.createElement("hr"));
      requests.append(section);
    }
    status.textContent = `عدد الطلبات: ${entries.length}`;
  } catch (error) {
    status.textContent = error.message;
  }
}

async function setupTotp() {
  try {
    const setup = await api("/owner/totp/setup", "POST");
    totpKey.textContent = setup.secret;
    totpSecret.hidden = false;
    status.textContent = "أضف المفتاح إلى هاتفك، ثم أثبت أول رمز.";
  } catch (error) { status.textContent = error.message; }
}

async function activateTotp() {
  try {
    await api("/owner/totp/activate", "POST", { totp_code: phoneCode() });
    totpKey.textContent = "";
    totpSecret.hidden = true;
    await refresh();
  } catch (error) { status.textContent = error.message; }
}

async function startLogin() {
  try {
    const startUrl = document.getElementById("login-url").value.trim();
    const profile = document.getElementById("login-profile").value;
    await api("/owner/sessions", "POST", {
      start_url: startUrl, totp_code: phoneCode(), ...(profile ? { auth_profile: profile } : {}),
    });
    await refresh();
    status.textContent = "بدأت الجلسة على السيرفر. افتح المتصفح المرئي؛ المساعد يمكنه استخدام هذه الجلسة فقط حتى تغلقها.";
  } catch (error) { status.textContent = error.message; }
}

async function saveProfile(sessionId, name) {
  try {
    await api(`/owner/sessions/${encodeURIComponent(sessionId)}/auth-profiles`, "POST", { profile_name: name.trim() });
    await refresh();
    status.textContent = "حُفظ الملف. أغلق الجلسة قبل بدء جلسة أخرى.";
  } catch (error) { status.textContent = error.message; }
}

async function closeSession(sessionId) {
  try {
    await api(`/owner/sessions/${encodeURIComponent(sessionId)}`, "DELETE");
    await refresh();
    status.textContent = "أُغلقت الجلسة.";
  } catch (error) { status.textContent = error.message; }
}

async function decide(id, decision) {
  try {
    await api(`/requests/${encodeURIComponent(id)}/${decision}`, "POST");
    await refresh();
  } catch (error) { status.textContent = error.message; }
}

document.getElementById("unlock").addEventListener("click", () => {
  ownerToken = window.prompt("رمز المالك") || null;
  refresh();
});
document.getElementById("refresh").addEventListener("click", refresh);
document.getElementById("setup-totp").addEventListener("click", setupTotp);
document.getElementById("activate-totp").addEventListener("click", activateTotp);
document.getElementById("start-login").addEventListener("click", startLogin);
window.setInterval(refresh, 5000);
