/**
 * Unified Constants - Single Source of Truth
 * Consolidates: timing.ts, mapConstants.ts, utils/constants.ts
 */

// ============================================================================
// TIMING CONSTANTS
// ============================================================================

export const TIMING = {
  /** Debounce delay for map operations */
  MAP_DEBOUNCE_MS: 300,
  /** Animation duration for map centering */
  MAP_CENTER_ANIMATION_MS: 1000,
  /** Delay before showing popup */
  POPUP_DELAY_MS: 50,
  /** Delay before retrying centering */
  CENTER_RETRY_DELAY_MS: 500,
  /** Duration for fallback centering */
  CENTER_FALLBACK_DURATION_MS: 500,
  /** Agent invocation timeout (3.5 minutes) */
  AGENT_TIMEOUT_MS: 210000,
  /** WebSocket reconnection delay */
  WS_RECONNECT_DELAY_MS: 3000,
} as const;

// ============================================================================
// RESPONSE & MESSAGE CONSTANTS
// ============================================================================

export const MESSAGES = {
  WELCOME:
    "Welcome to Fire Assistant. Ask me about wildfires, emergency response, or weather conditions.",
  TIMEOUT_ERROR:
    "Request timed out after 3.5 minutes. AgentCore may be processing a complex request.",
  NO_RESPONSE: "No response content available",
  /** Initial status before the first AG-UI event arrives. */
  THINKING: "Thinking…",
} as const;

// ============================================================================
// MAP CONSTANTS (from mapConstants.ts)
// ============================================================================

export interface CenterLocation {
  latitude: number;
  longitude: number;
  name: string;
}

export const DEFAULT_CENTER: CenterLocation = {
  latitude: 34.0522,
  longitude: -118.2437,
  name: "Los Angeles",
};

export const DEFAULT_ZOOM = 10;

// ============================================================================
// MAP SYMBOLS
// ============================================================================

export interface SymbolOutline {
  color: number[];
  width: number;
}

export interface LocationSymbol {
  style: "circle";
  color: number[];
  size: string;
  outline: SymbolOutline;
}

export interface PictureMarkerSymbol {
  style: string;
  url: string;
  width: string;
  height: string;
}

export interface WindSymbols {
  light: string;
  moderate: string;
  strong: string;
  veryStrong: string;
  extreme: string;
  width: string;
  height: string;
}

export interface MapSymbols {
  location: LocationSymbol;
  fire: PictureMarkerSymbol;
  wind: WindSymbols;
}

export interface WindColorMap {
  light: number[];
  moderate: number[];
  strong: number[];
  veryStrong: number[];
  extreme: number[];
}

export const SYMBOLS: MapSymbols = {
  location: {
    style: "circle",
    color: [0, 122, 255, 0.8],
    size: "16px",
    outline: { color: [255, 255, 255], width: 2 },
  },
  fire: {
    style: "picture-marker",
    url: "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIzMiIgaGVpZ2h0PSI0MCIgdmlld0JveD0iMCAwIDMyIDQwIj4KICA8IS0tIE91dGVyIGZsYW1lIC0gUmVkL09yYW5nZSAtLT4KICA8cGF0aCBkPSJNMTYgMiBDIDEyIDggOCAxNCA4IDIwIEMgOCAyOCAxMiAzNiAxNiAzOCBDIDIwIDM2IDI0IDI4IDI0IDIwIEMgMjQgMTQgMjAgOCAxNiAyIFoiIGZpbGw9IiNGRjQ1MDAiIHN0cm9rZT0iI0I5MUMxQyIgc3Ryb2tlLXdpZHRoPSIxLjUiLz4KICA8IS0tIE1pZGRsZSBmbGFtZSAtIE9yYW5nZSAtLT4KICA8cGF0aCBkPSJNMTYgMTAgQyAxMyAxNCAxMSAxNyAxMSAyMSBDIDExIDI2IDEzIDMwIDE2IDMyIEMgMTkgMzAgMjEgMjYgMjEgMjEgQyAyMSAxNyAxOSAxNCAxNiAxMCBaIiBmaWxsPSIjRkZBNTAwIi8+CiAgPCEtLSBJbm5lciBmbGFtZSAtIFllbGxvdyAtLT4KICA8ZWxsaXBzZSBjeD0iMTYiIGN5PSIyMiIgcng9IjMiIHJ5PSI1IiBmaWxsPSIjRkZENTRGIi8+Cjwvc3ZnPg==",
    width: "28px",
    height: "35px",
  },
  wind: {
    light:
      "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSI0MCIgaGVpZ2h0PSI0MCIgdmlld0JveD0iMCAwIDQwIDQwIj4KICA8ZGVmcz4KICAgIDxmaWx0ZXIgaWQ9InNoYWRvdyIgeD0iLTUwJSIgeT0iLTUwJSIgd2lkdGg9IjIwMCUiIGhlaWdodD0iMjAwJSI+CiAgICAgIDxmZURyb3BTaGFkb3cgZHg9IjEiIGR5PSIxIiBzdGREZXZpYXRpb249IjEiIGZsb29kLWNvbG9yPSIjMDAwMDAwIiBmbG9vZC1vcGFjaXR5PSIwLjMiLz4KICAgIDwvZmlsdGVyPgogIDwvZGVmcz4KICA8ZyB0cmFuc2Zvcm09InRyYW5zbGF0ZSgyMCwyMCkiPgogICAgPHBhdGggZD0iTSAwLC0xNSBMIC02LDYgTCAtMyw2IEwgLTMsMTIgTCAzLDEyIEwgMyw2IEwgNiw2IFoiIGZpbGw9IiMwMEZGMDAiIHN0cm9rZT0iIzAwQUEwMCIgc3Ryb2tlLXdpZHRoPSIyIiBmaWx0ZXI9InVybCgjc2hhZG93KSIvPgogIDwvZz4KPC9zdmc+",
    moderate:
      "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSI0MCIgaGVpZ2h0PSI0MCIgdmlld0JveD0iMCAwIDQwIDQwIj4KICA8ZGVmcz4KICAgIDxmaWx0ZXIgaWQ9InNoYWRvdyIgeD0iLTUwJSIgeT0iLTUwJSIgd2lkdGg9IjIwMCUiIGhlaWdodD0iMjAwJSI+CiAgICAgIDxmZURyb3BTaGFkb3cgZHg9IjEiIGR5PSIxIiBzdGREZXZpYXRpb249IjEiIGZsb29kLWNvbG9yPSIjMDAwMDAwIiBmbG9vZC1vcGFjaXR5PSIwLjMiLz4KICAgIDwvZmlsdGVyPgogIDwvZGVmcz4KICA8ZyB0cmFuc2Zvcm09InRyYW5zbGF0ZSgyMCwyMCkiPgogICAgPHBhdGggZD0iTSAwLC0xNSBMIC02LDYgTCAtMyw2IEwgLTMsMTIgTCAzLDEyIEwgMyw2IEwgNiw2IFoiIGZpbGw9IiNGRkZGMDAiIHN0cm9rZT0iI0NBQ0EwMCIgc3Ryb2tlLXdpZHRoPSIyIiBmaWx0ZXI9InVybCgjc2hhZG93KSIvPgogIDwvZz4KPC9zdmc+",
    strong:
      "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSI0MCIgaGVpZ2h0PSI0MCIgdmlld0JveD0iMCAwIDQwIDQwIj4KICA8ZGVmcz4KICAgIDxmaWx0ZXIgaWQ9InNoYWRvdyIgeD0iLTUwJSIgeT0iLTUwJSIgd2lkdGg9IjIwMCUiIGhlaWdodD0iMjAwJSI+CiAgICAgIDxmZURyb3BTaGFkb3cgZHg9IjEiIGR5PSIxIiBzdGREZXZpYXRpb249IjEiIGZsb29kLWNvbG9yPSIjMDAwMDAwIiBmbG9vZC1vcGFjaXR5PSIwLjMiLz4KICAgIDwvZmlsdGVyPgogIDwvZGVmcz4KICA8ZyB0cmFuc2Zvcm09InRyYW5zbGF0ZSgyMCwyMCkiPgogICAgPHBhdGggZD0iTSAwLC0xNSBMIC02LDYgTCAtMyw2IEwgLTMsMTIgTCAzLDEyIEwgMyw2IEwgNiw2IFoiIGZpbGw9IiNGRkE1MDAiIHN0cm9rZT0iI0NDODQwMCIgc3Ryb2tlLXdpZHRoPSIyIiBmaWx0ZXI9InVybCgjc2hhZG93KSIvPgogIDwvZz4KPC9zdmc+",
    veryStrong:
      "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSI0MCIgaGVpZ2h0PSI0MCIgdmlld0JveD0iMCAwIDQwIDQwIj4KICA8ZGVmcz4KICAgIDxmaWx0ZXIgaWQ9InNoYWRvdyIgeD0iLTUwJSIgeT0iLTUwJSIgd2lkdGg9IjIwMCUiIGhlaWdodD0iMjAwJSI+CiAgICAgIDxmZURyb3BTaGFkb3cgZHg9IjEiIGR5PSIxIiBzdGREZXZpYXRpb249IjEiIGZsb29kLWNvbG9yPSIjMDAwMDAwIiBmbG9vZC1vcGFjaXR5PSIwLjMiLz4KICAgIDwvZmlsdGVyPgogIDwvZGVmcz4KICA8ZyB0cmFuc2Zvcm09InRyYW5zbGF0ZSgyMCwyMCkiPgogICAgPHBhdGggZD0iTSAwLC0xNSBMIC02LDYgTCAtMyw2IEwgLTMsMTIgTCAzLDEyIEwgMyw2IEwgNiw2IFoiIGZpbGw9IiNGRjQ1MDAiIHN0cm9rZT0iI0NDMzcwMCIgc3Ryb2tlLXdpZHRoPSIyIiBmaWx0ZXI9InVybCgjc2hhZG93KSIvPgogIDwvZz4KPC9zdmc+",
    extreme:
      "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSI0MCIgaGVpZ2h0PSI0MCIgdmlld0JveD0iMCAwIDQwIDQwIj4KICA8ZGVmcz4KICAgIDxmaWx0ZXIgaWQ9InNoYWRvdyIgeD0iLTUwJSIgeT0iLTUwJSIgd2lkdGg9IjIwMCUiIGhlaWdodD0iMjAwJSI+CiAgICAgIDxmZURyb3BTaGFkb3cgZHg9IjEiIGR5PSIxIiBzdGREZXZpYXRpb249IjEiIGZsb29kLWNvbG9yPSIjMDAwMDAwIiBmbG9vZC1vcGFjaXR5PSIwLjMiLz4KICAgIDwvZmlsdGVyPgogIDwvZGVmcz4KICA8ZyB0cmFuc2Zvcm09InRyYW5zbGF0ZSgyMCwyMCkiPgogICAgPHBhdGggZD0iTSAwLC0xNSBMIC02LDYgTCAtMyw2IEwgLTMsMTIgTCAzLDEyIEwgMyw2IEwgNiw2IFoiIGZpbGw9IiM4QjAwMDAiIHN0cm9rZT0iIzZBMDAwMCIgc3Ryb2tlLXdpZHRoPSIyIiBmaWx0ZXI9InVybCgjc2hhZG93KSIvPgogIDwvZz4KPC9zdmc+",
    width: "35px",
    height: "35px",
  },
};

export const WIND_COLORS: WindColorMap = {
  light: [0, 255, 0, 0.8],
  moderate: [255, 255, 0, 0.8],
  strong: [255, 165, 0, 0.8],
  veryStrong: [255, 69, 0, 0.8],
  extreme: [139, 0, 0, 0.8],
};
