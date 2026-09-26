// Minimal type declarations for the few Node built-ins the `node --test` files use
// (src/lib/*.test.ts), so `tsc -p tsconfig.test.json` can type-check them without adding
// @types/node (no new npm dependency; package-lock.json unchanged). Not part of the app build.

declare module "node:test" {
  export function test(name: string, fn: () => void | Promise<void>): Promise<void>;
  export function describe(name: string, fn: () => void): void;
}

declare module "node:assert/strict" {
  interface Assert {
    (value: unknown, message?: string): asserts value;
    equal(actual: unknown, expected: unknown, message?: string): void;
    notEqual(actual: unknown, expected: unknown, message?: string): void;
    deepEqual(actual: unknown, expected: unknown, message?: string): void;
    ok(value: unknown, message?: string): asserts value;
    match(value: string, regexp: RegExp, message?: string): void;
    throws(fn: () => unknown, expected?: unknown, message?: string): void;
  }
  const assert: Assert;
  export default assert;
}

declare module "node:fs" {
  export function readFileSync(path: URL | string, encoding: "utf-8"): string;
  export function readdirSync(path: URL | string): string[];
}
