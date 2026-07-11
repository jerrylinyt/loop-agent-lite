export function updateUrlState(changes: Record<string, string | null>) {
  const url = new URL(window.location.href);
  for (const [key, value] of Object.entries(changes)) {
    if (value === null || value === "") url.searchParams.delete(key);
    else url.searchParams.set(key, value);
  }
  history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
}

export function urlParam(name: string): string { return new URLSearchParams(location.search).get(name) ?? ""; }
