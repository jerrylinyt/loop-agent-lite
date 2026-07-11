/** 主題選擇器：保存個人偏好，system 模式則跟隨作業系統媒體查詢。 */
import { useEffect, useState } from "react";

type ThemePreference = "system" | "dark" | "light";

const media = window.matchMedia("(prefers-color-scheme: light)");

function resolveTheme(preference: ThemePreference) {
  return preference === "system" ? (media.matches ? "light" : "dark") : preference;
}

export default function ThemePicker() {
  const [preference, setPreference] = useState<ThemePreference>(() =>
    (localStorage.getItem("loop-theme") as ThemePreference | null) ?? "dark"
  );

  useEffect(() => {
    const apply = () => {
      document.documentElement.dataset.theme = resolveTheme(preference);
      document.documentElement.style.colorScheme = resolveTheme(preference);
    };
    localStorage.setItem("loop-theme", preference);
    apply();
    media.addEventListener("change", apply);
    return () => media.removeEventListener("change", apply);
  }, [preference]);

  return (
    <label className="theme-picker">
      <span aria-hidden="true">◐</span>
      <span className="sr-only">介面主題</span>
      <select
        value={preference}
        onChange={(event) => setPreference(event.target.value as ThemePreference)}
        aria-label="介面主題"
      >
        <option value="system">跟隨系統</option>
        <option value="dark">深色</option>
        <option value="light">淺色</option>
      </select>
    </label>
  );
}
