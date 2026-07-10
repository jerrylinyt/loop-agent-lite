import type { CSSProperties, ReactNode } from "react";

// SGR(...m)拆出來著色;其他 CSI(游標移動/清行)與 OSC(改標題)序列直接剝除。
const ANSI_RE = /\x1b(?:\[([0-9;]*)m|\[[0-9;?]*[A-Za-z]|\][^\x07\x1b]*(?:\x07|\x1b\\)?)/g;

const COLOR_NAMES = ["black", "red", "green", "yellow", "blue", "magenta", "cyan", "white"] as const;

interface AnsiState {
  fgClass: string | null;
  bgClass: string | null;
  fgColor: string | null;
  bgColor: string | null;
  bold: boolean;
  dim: boolean;
  italic: boolean;
  underline: boolean;
}

const INITIAL: AnsiState = {
  fgClass: null, bgClass: null, fgColor: null, bgColor: null,
  bold: false, dim: false, italic: false, underline: false
};

function xterm256(n: number): string {
  if (n < 16) {
    const base = ["#000", "#c00", "#0a0", "#aa0", "#00c", "#a0a", "#0aa", "#aaa",
      "#555", "#f55", "#5f5", "#ff5", "#55f", "#f5f", "#5ff", "#fff"];
    return base[n];
  }
  if (n < 232) {
    const v = n - 16;
    const steps = [0, 95, 135, 175, 215, 255];
    return `rgb(${steps[Math.floor(v / 36)]},${steps[Math.floor(v / 6) % 6]},${steps[v % 6]})`;
  }
  const gray = 8 + (n - 232) * 10;
  return `rgb(${gray},${gray},${gray})`;
}

function applyCodes(codes: number[], state: AnsiState): AnsiState {
  const next = { ...state };
  for (let i = 0; i < codes.length; i += 1) {
    const code = codes[i];
    if (code === 0) Object.assign(next, INITIAL);
    else if (code === 1) next.bold = true;
    else if (code === 2) next.dim = true;
    else if (code === 3) next.italic = true;
    else if (code === 4) next.underline = true;
    else if (code === 22) { next.bold = false; next.dim = false; }
    else if (code === 23) next.italic = false;
    else if (code === 24) next.underline = false;
    else if (code >= 30 && code <= 37) { next.fgClass = `ansi-fg-${COLOR_NAMES[code - 30]}`; next.fgColor = null; }
    else if (code >= 90 && code <= 97) { next.fgClass = `ansi-fg-bright-${COLOR_NAMES[code - 90]}`; next.fgColor = null; }
    else if (code === 39) { next.fgClass = null; next.fgColor = null; }
    else if (code >= 40 && code <= 47) { next.bgClass = `ansi-bg-${COLOR_NAMES[code - 40]}`; next.bgColor = null; }
    else if (code >= 100 && code <= 107) { next.bgClass = `ansi-bg-bright-${COLOR_NAMES[code - 100]}`; next.bgColor = null; }
    else if (code === 49) { next.bgClass = null; next.bgColor = null; }
    else if (code === 38 || code === 48) {
      const isFg = code === 38;
      if (codes[i + 1] === 5 && codes[i + 2] !== undefined) {
        const color = xterm256(codes[i + 2]);
        if (isFg) { next.fgColor = color; next.fgClass = null; } else { next.bgColor = color; next.bgClass = null; }
        i += 2;
      } else if (codes[i + 1] === 2 && codes[i + 4] !== undefined) {
        const color = `rgb(${codes[i + 2]},${codes[i + 3]},${codes[i + 4]})`;
        if (isFg) { next.fgColor = color; next.fgClass = null; } else { next.bgColor = color; next.bgClass = null; }
        i += 4;
      }
    }
  }
  return next;
}

function isStyled(state: AnsiState): boolean {
  return !!(state.fgClass || state.bgClass || state.fgColor || state.bgColor
    || state.bold || state.dim || state.italic || state.underline);
}

export function hasAnsi(text: string): boolean {
  return text.includes("\x1b");
}

/** 將含 ANSI escape 的文字轉為 React 節點;無樣式的區段維持純字串。 */
export function renderAnsi(text: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  let state = INITIAL;
  let last = 0;
  let key = 0;

  const push = (chunk: string) => {
    if (!chunk) return;
    if (!isStyled(state)) {
      nodes.push(chunk);
      return;
    }
    const classes = [state.fgClass, state.bgClass,
      state.bold ? "ansi-bold" : null, state.dim ? "ansi-dim" : null,
      state.italic ? "ansi-italic" : null, state.underline ? "ansi-underline" : null]
      .filter(Boolean).join(" ");
    const style: CSSProperties = {};
    if (state.fgColor) style.color = state.fgColor;
    if (state.bgColor) style.backgroundColor = state.bgColor;
    nodes.push(<span key={key += 1} className={classes || undefined} style={state.fgColor || state.bgColor ? style : undefined}>{chunk}</span>);
  };

  ANSI_RE.lastIndex = 0;
  for (let match = ANSI_RE.exec(text); match; match = ANSI_RE.exec(text)) {
    push(text.slice(last, match.index));
    last = match.index + match[0].length;
    if (match[1] !== undefined) {
      const codes = match[1] === "" ? [0] : match[1].split(";").map((value) => Number(value || "0"));
      state = applyCodes(codes, state);
    }
  }
  push(text.slice(last));
  return nodes;
}
