/* Load UI TypeScript modules in Node with the locked compiler (no bundler).
 * Imports resolve like Vite's config: "@/x" maps to ui/src/x, relative paths resolve beside the
 * importing file, and .ts, .tsx and index.ts are tried in turn; bare packages load from
 * ui/node_modules. Each load() call compiles fresh module instances, so a test can reset
 * module-level state (for example a memoized initialize()) by loading again. import.meta.env
 * reads as a development build's (DEV true) plus the Vite variables the test passes as
 * load(path, { env }), so logger.warn still reaches the console. load(path, { stubs }) maps an import
 * specifier, exactly as written in the importing file, to the exports to use instead of resolving it. */
const fs = require("node:fs");
const path = require("node:path");
const { Module, createRequire } = require("node:module");
const ts = require("typescript");

const ui = path.resolve(__dirname, "..");
const src = path.join(ui, "src");
const packages = createRequire(path.join(ui, "package.json"));
const inside = filename => filename.startsWith(src + path.sep);

/** A ui/src file for a local import specifier, or null for a package import. */
function resolve(specifier, from) {
  const base = specifier.startsWith("@/") ? path.join(src, specifier.slice(2))
    : specifier.startsWith(".") ? path.resolve(path.dirname(from), specifier) : null;
  if (base === null) return null;
  for (const candidate of [base, `${base}.ts`, `${base}.tsx`, path.join(base, "index.ts")]) {
    if (inside(candidate) && fs.existsSync(candidate) && fs.statSync(candidate).isFile()) return candidate;
  }
  throw new Error(`Cannot resolve ${specifier} from ${path.relative(ui, from)} inside ui/src`);
}

/** Rewrite each `import.meta` as `module.importMeta`, the object compile() gives every module of one load. */
function importMetaFromModule(context) {
  const visit = node => (ts.isMetaProperty(node) && node.keywordToken === ts.SyntaxKind.ImportKeyword
    ? context.factory.createPropertyAccessExpression(context.factory.createIdentifier("module"), "importMeta")
    : ts.visitEachChild(node, visit, context));
  return file => ts.visitNode(file, visit);
}

function compile(filename, cache, env, stubs) {
  if (!inside(filename)) throw new Error(`Outside ui/src: ${filename}`);
  if (cache.has(filename)) return cache.get(filename).exports;
  const output = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022,
      jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
    fileName: filename,
    transformers: { before: [importMetaFromModule] },
  }).outputText;
  const mod = new Module(filename);
  mod.filename = filename;
  mod.importMeta = { env: { DEV: true, ...env } };
  // The compiled code's require() delegates to this instance method.
  mod.require = specifier => {
    if (Object.hasOwn(stubs, specifier)) return stubs[specifier];
    const local = resolve(specifier, filename);
    return local ? compile(local, cache, env, stubs) : packages(specifier);
  };
  cache.set(filename, mod);
  mod._compile(output, filename);
  return mod.exports;
}

/** Transpile and evaluate a module under ui/src, with fresh copies of the ui/src modules it imports. */
function load(relative, { env = {}, stubs = {} } = {}) {
  return compile(path.resolve(src, relative), new Map(), env, stubs);
}

module.exports = { load };
