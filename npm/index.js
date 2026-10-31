// ESM entry point. The implementation is CommonJS so `require("gemini-router")`
// and `import ... from "gemini-router"` resolve to exactly the same behaviour.
import lib from "./lib.cjs";

export const {
  CLIENT_CONTEXT_HEADERS,
  RouterContext,
  RouterHttpError,
  createContext,
  headers,
  requestJSON,
  routerFetch,
  jobStart,
  jobFinish,
  jobs,
  jobGet,
  note,
  notes,
  calls,
  ttsFile,
  ttsStatus,
  ttsDownload,
  health,
  makeTraceparent,
  parseTraceparent,
  makeURL,
  randomHex,
} = lib;

export default lib;
