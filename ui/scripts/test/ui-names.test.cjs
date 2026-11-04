/* The manual post-deploy browser checks take every UI name they share with the release harness from
 * ui/scripts/browser/ui-names.cjs, so a rename the harness catches cannot leave a stale copy in them. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const names = require("../browser/ui-names.cjs");

const root = path.resolve(__dirname, "../../..");
const MANUAL_CHECKS = ["deployment/acceptance/check_public_sop_browser.cjs",
  "deployment/acceptance/check_prepared_simulation_browser.cjs"];

test("the shared names module has no dependencies, so the manual checks can require it from the checkout", () => {
  const source = fs.readFileSync(path.join(__dirname, "../browser/ui-names.cjs"), "utf8");
  assert.deepEqual(source.match(/\brequire\s*\(/g), null);
  assert.ok(Object.isFrozen(names) && Object.keys(names).length > 0);
});

for (const file of MANUAL_CHECKS) {
  test(`${path.basename(file)} requires the shared names and quotes none of them itself`, () => {
    const source = fs.readFileSync(path.join(root, file), "utf8");
    assert.match(source, /require\(["']\.\.\/\.\.\/ui\/scripts\/browser\/ui-names\.cjs["']\)/);
    const copies = Object.entries(names).filter(([, value]) =>
      [`"${value}"`, `'${value}'`, `\`${value}\``].some(quoted => source.includes(quoted)));
    assert.deepEqual(copies, [], `${file} keeps its own copy of these shared names`);
  });
}
