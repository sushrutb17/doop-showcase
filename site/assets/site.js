const root = document.documentElement;
const toggle = document.querySelector("[data-theme-toggle]");
const themes = ["system", "light", "dark"];

function applyTheme(theme) {
  if (theme === "system") {
    root.removeAttribute("data-theme");
  } else {
    root.setAttribute("data-theme", theme);
  }
  toggle.textContent = `Theme: ${theme}`;
  toggle.setAttribute("aria-label", `Current theme is ${theme}. Select the next theme.`);
}

let activeTheme = localStorage.getItem("doop-theme") || "system";
if (!themes.includes(activeTheme)) activeTheme = "system";
applyTheme(activeTheme);

toggle.addEventListener("click", () => {
  activeTheme = themes[(themes.indexOf(activeTheme) + 1) % themes.length];
  localStorage.setItem("doop-theme", activeTheme);
  applyTheme(activeTheme);
});
