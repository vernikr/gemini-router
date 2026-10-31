/** Zero-dependency helpers for the gemini-router pass-through proxy. */

export interface ContextOptions {
  /** One target activity, e.g. "job-42". Defaults to `<initiator>@<UTC date>`. */
  job?: string;
  /** One execution of the job. Defaults to a random 32-hex W3C trace id. */
  trace?: string;
  /** Alias for `trace`. */
  traceId?: string;
  /** Client stage inside the execution, e.g. "prefilter". */
  step?: string;
  /** Who started the work, e.g. "example-client". */
  initiator?: string;
  /** Free text stored in the audit trail; never used for routing. */
  comment?: string;
  /** One logical request. Defaults to a random 12-hex id. */
  requestId?: string;
  /** W3C span id. Defaults to a random 16-hex id. */
  spanId?: string;
  parentSpan?: string;
  /** Incoming W3C traceparent; seeds trace and parentSpan when set. */
  traceparent?: string;
}

export interface ContextFields {
  job: string;
  trace: string;
  step: string;
  initiator: string;
  comment: string;
  requestId: string;
  spanId: string;
  parentSpan: string;
  traceparent: string;
}

export declare class RouterContext {
  readonly job: string;
  readonly trace: string;
  readonly traceId: string;
  readonly step: string;
  readonly initiator: string;
  readonly comment: string;
  readonly requestId: string;
  readonly spanId: string;
  readonly parentSpan: string;
  readonly traceparent: string;
  headers(): Record<string, string>;
  withStep(step: string): RouterContext;
  withTrace(trace?: string, overrides?: ContextOptions): RouterContext;
  withComment(comment: string): RouterContext;
  child(overrides?: ContextOptions): RouterContext;
  toJSON(): ContextFields;
}

export declare class RouterHttpError extends Error {
  readonly status: number;
  readonly body: unknown;
  readonly url: string;
}

export declare const CLIENT_CONTEXT_HEADERS: readonly string[];

export declare function createContext(options?: ContextOptions): RouterContext;
export declare function headers(
  token: string | undefined,
  context: RouterContext | ContextOptions,
  extra?: Record<string, string | number | boolean | undefined | null>,
): Record<string, string>;

export interface RequestOptions {
  method?: string;
  body?: unknown;
  query?: Record<string, string | number | boolean | undefined | null>;
  headers?: Record<string, string>;
  signal?: AbortSignal;
  fetch?: typeof fetch;
}

export declare function requestJSON(
  base: string, token: string | undefined, context: RouterContext | ContextOptions,
  path: string, options?: RequestOptions,
): Promise<any>;

export interface RouterFetchInit extends Omit<RequestInit, "headers" | "body"> {
  headers?: HeadersInit;
  body?: any;
  query?: Record<string, string | number | boolean | undefined | null>;
  fetch?: typeof fetch;
}

/** fetch() with the token and correlation headers; returns the raw Response. */
export declare function routerFetch(
  base: string, token: string | undefined, context: RouterContext | ContextOptions,
  path: string, init?: RouterFetchInit,
): Promise<Response>;

export declare function jobStart(
  base: string, token: string | undefined, context: RouterContext | ContextOptions,
  options?: EndpointOptions,
): Promise<any>;
export declare function jobFinish(
  base: string, token: string | undefined, context: RouterContext | ContextOptions,
  options?: EndpointOptions & { status?: "done" | "failed" },
): Promise<any>;

export interface EndpointOptions {
  job?: string;
  trace?: string;
  step?: string;
  initiator?: string;
  comment?: string;
  n?: number;
  status?: "done" | "failed";
  model?: string;
  source?: string;
  requestId?: string;
  request_id?: string;
  signal?: AbortSignal;
  fetch?: typeof fetch;
}

export declare function jobs(
  base: string, token: string | undefined, context: RouterContext | ContextOptions,
  options?: number | EndpointOptions,
): Promise<any>;
export declare function jobGet(
  base: string, token: string | undefined, context: RouterContext | ContextOptions,
  options?: string | EndpointOptions,
): Promise<any>;
export declare function note(
  base: string, token: string | undefined, context: RouterContext | ContextOptions,
  comment: string, options?: EndpointOptions,
): Promise<any>;
export declare function notes(
  base: string, token: string | undefined, context: RouterContext | ContextOptions,
  options?: number | EndpointOptions,
): Promise<any>;
export declare function calls(
  base: string, token: string | undefined, context: RouterContext | ContextOptions,
  options?: number | EndpointOptions,
): Promise<any>;
export interface TTSOptions {
  voice?: string;
  style?: string;
  waitForMinuteLimits?: boolean;
  requestId?: string;
  waitSeconds?: number;
  signal?: AbortSignal;
  fetch?: typeof fetch;
}

/** Submit an already-uploaded TXT/Markdown file to the Gemini TTS endpoint. */
export declare function ttsFile(
  base: string, token: string | undefined, context: RouterContext | ContextOptions,
  uploadId: string, options?: TTSOptions,
): Promise<any>;
export declare function ttsStatus(
  base: string, token: string | undefined, context: RouterContext | ContextOptions,
  requestId: string, options?: Pick<TTSOptions, "signal" | "fetch">,
): Promise<any>;
/** Returns the raw audio/mpeg Response; call `response.blob()` or `arrayBuffer()`. */
export declare function ttsDownload(
  base: string, token: string | undefined, context: RouterContext | ContextOptions,
  requestId: string, options?: Pick<TTSOptions, "signal" | "fetch">,
): Promise<Response>;

export declare function health(base: string, options?: { signal?: AbortSignal; fetch?: typeof fetch }): Promise<any>;

export interface ParsedTraceparent {
  version: string;
  traceId: string;
  parentSpanId: string;
  flags: string;
  sampled: boolean;
}

export declare function makeTraceparent(traceId: string, spanId: string, sampled?: boolean): string;
export declare function parseTraceparent(value: string): ParsedTraceparent | null;
export declare function makeURL(
  base: string, path: string,
  query?: Record<string, string | number | boolean | undefined | null>,
): URL;
export declare function randomHex(bytes: number): string;

declare const lib: {
  CLIENT_CONTEXT_HEADERS: readonly string[];
  RouterContext: typeof RouterContext;
  RouterHttpError: typeof RouterHttpError;
  createContext: typeof createContext;
  headers: typeof headers;
  requestJSON: typeof requestJSON;
  routerFetch: typeof routerFetch;
  jobStart: typeof jobStart;
  jobFinish: typeof jobFinish;
  jobs: typeof jobs;
  jobGet: typeof jobGet;
  note: typeof note;
  notes: typeof notes;
  calls: typeof calls;
  ttsFile: typeof ttsFile;
  ttsStatus: typeof ttsStatus;
  ttsDownload: typeof ttsDownload;
  health: typeof health;
  makeTraceparent: typeof makeTraceparent;
  parseTraceparent: typeof parseTraceparent;
  makeURL: typeof makeURL;
  randomHex: typeof randomHex;
};
export default lib;
