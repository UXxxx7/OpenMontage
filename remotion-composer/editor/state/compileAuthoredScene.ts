import { transform } from "@babel/standalone";
import * as React from "react";
import * as Remotion from "remotion";

export type CompileResult =
  | { ok: true; component: React.ComponentType<Record<string, unknown>> }
  | { ok: false; error: string };

/**
 * Phase 8 — compiles and mounts an Arm-B job's freshly-fetched
 * `AuthoredScene.tsx` source ENTIRELY in the browser, so the live preview
 * has the same real-time feel as the schema-driven editor's `<Player
 * component={XiaojinEditorial}>` — except XiaojinEditorial is a component
 * this app already knows about at build time, while an authored scene is a
 * different file generated fresh per job. There is no other way to get a
 * live Remotion Player preview of code that doesn't exist until the job
 * runs.
 *
 * IMPORTANT — this is a DIFFERENT trust boundary than server-side execution,
 * not a relaxation of one: `whatsapp_mvp/authored/tsx_validator.py` already
 * banned `eval`/`new Function`/dynamic `import()` etc. in the generated
 * source ITSELF, specifically to stop the LLM's own output from doing
 * arbitrary code execution server-side. What happens here is the OPPOSITE
 * direction — the HOST application (this file, code we wrote, already
 * reviewed) uses `new Function` to execute an (already-validated) module in
 * the USER'S OWN BROWSER TAB, on their own job's own content. That's the
 * same pattern CodeSandbox/StackBlitz/the TS Playground use for live
 * previews, not the thing tsx_validator.py's ban exists to prevent — don't
 * conflate the two when reviewing this file.
 */
export function compileAuthoredScene(tsx: string): CompileResult {
  let jsCode: string | null | undefined;
  try {
    const result = transform(tsx, {
      // Babel 8's @babel/preset-typescript dropped `isTSX`/`allExtensions`
      // in favor of detecting JSX purely from the filename's own extension
      // — a literal ".tsx" filename is sufficient, no explicit option needed
      // (confirmed live: passing the old options throws
      // "the .allExtensions and .isTSX options have been removed").
      filename: "AuthoredScene.tsx",
      sourceType: "module",
      presets: [
        "typescript",
        ["react", { runtime: "classic" }],
      ],
      // Converts the (stripped-of-types) `import`/`export` statements to
      // CommonJS `require`/`exports` — needed because the module is executed
      // via a plain Function body below, where only CommonJS syntax is
      // valid (import/export are only legal at a real ES module's top
      // level, which a Function body is not).
      plugins: ["transform-modules-commonjs"],
    });
    jsCode = result?.code;
  } catch (e) {
    return { ok: false, error: `Compile error: ${e instanceof Error ? e.message : String(e)}` };
  }
  if (!jsCode) {
    return { ok: false, error: "Compile produced no output" };
  }

  // Only "react" and "remotion" resolve — mirrors tsx_validator.py's own
  // ALLOWED_IMPORTS whitelist (ideally this is unreachable anyway, since the
  // server already rejected anything else before this scene was ever
  // persisted, but a browser-side backstop costs nothing).
  const authoredRequire = (name: string): unknown => {
    if (name === "react") return React;
    if (name === "remotion") return Remotion;
    throw new Error(`Import not allowed in an authored scene: ${JSON.stringify(name)}`);
  };

  try {
    const moduleExports: { default?: unknown } = {};
    const moduleObj = { exports: moduleExports };
    // eslint-disable-next-line no-new-func -- see the module-level doc
    // comment above for why this is the intended, reviewed mechanism here.
    const factory = new Function("require", "exports", "module", jsCode);
    factory(authoredRequire, moduleExports, moduleObj);
    const component = (moduleObj.exports as { default?: unknown }).default;
    if (typeof component !== "function") {
      return { ok: false, error: "Compiled scene has no default-exported component" };
    }
    return { ok: true, component: component as React.ComponentType<Record<string, unknown>> };
  } catch (e) {
    return { ok: false, error: `Execution error: ${e instanceof Error ? e.message : String(e)}` };
  }
}
