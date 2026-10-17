/* ProctorAI shared auth + navigation */
const PA = (() => {
  const DOMAIN    = "https://ap-south-1yx4st7fyc.auth.ap-south-1.amazoncognito.com";
  const CLIENT_ID = "4aeuraag6jvio5kp81i32sjll5";
  const API       = "https://nju6z5l8hh.execute-api.ap-south-1.amazonaws.com";
  const KEY       = "pa_token";

  const NAV = {
    students: [
      { href: "student.html", label: "My exams" },
      { href: "index.html",   label: "Enrolment" },
    ],
    proctors: [
      { href: "author.html",    label: "Exams" },
      { href: "dashboard.html", label: "Live" },
      { href: "grade.html",     label: "Marking" },
      { href: "analytics.html", label: "Analytics" },
      { href: "summary.html",   label: "Report" },
    ],
  };

  function claimsOf(t){
    return JSON.parse(atob(t.split(".")[1].replace(/-/g,"+").replace(/_/g,"/")));
  }

  function toLogin(){
    sessionStorage.removeItem(KEY);
    location.replace("router.html");
  }

  /** Returns {token, claims, email, groups} or redirects. role = "students" | "proctors" | null */
  function require(role){
    const hash = new URLSearchParams(location.hash.substring(1));
    let token = hash.get("id_token") || sessionStorage.getItem(KEY);
    if (!token){ toLogin(); return null; }

    let claims;
    try { claims = claimsOf(token); }
    catch (e){ toLogin(); return null; }

    if (claims.exp && claims.exp * 1000 < Date.now()){ toLogin(); return null; }

    const groups = claims["cognito:groups"] || [];
    if (role && !groups.includes(role)){ toLogin(); return null; }

    sessionStorage.setItem(KEY, token);
    if (location.hash) history.replaceState(null, "", location.pathname + location.search);

    return { token, claims, email: claims.email || "", groups };
  }

  function logout(){
    sessionStorage.removeItem(KEY);
    const after = location.origin + "/router.html";
    location.href = `${DOMAIN}/logout?client_id=${CLIENT_ID}`
      + `&logout_uri=${encodeURIComponent(after)}`;
  }

  /** Renders nav links + email + logout into the element with the given id. */
  function mountNav(navId, whoId, session){
    const role = session.groups.includes("proctors") ? "proctors" : "students";
    const here = location.pathname.split("/").pop() || "student.html";
    const nav = $(navId);
    if (nav){
      nav.innerHTML = (NAV[role] || []).map(l =>
        `<a href="${l.href}" class="${l.href === here ? "on" : ""}">${l.label}</a>`).join("");
    }
    const who = $(whoId);
    if (who){
      who.innerHTML = `${session.email} <button class="pa-out" onclick="PA.logout()">Sign out</button>`;
    }
  }

  function $(id){ return document.getElementById(id); }

  return { DOMAIN, CLIENT_ID, API, require, logout, mountNav, claimsOf };
})();

/* shared styles for the injected nav */
(() => {
  const css = document.createElement("style");
  css.textContent = `
    .pa-out{margin-left:10px;padding:3px 10px;font-size:12px;font-family:inherit;cursor:pointer;
      border-radius:6px;border:1px solid var(--line,#E3E9EF);background:transparent;color:var(--muted,#66788A)}
    .pa-out:hover{border-color:var(--muted,#66788A);color:var(--ink,#16202B)}`;
  document.head.appendChild(css);
})();
