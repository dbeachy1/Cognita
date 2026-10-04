import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = fs.readFileSync(new URL("../src/cognita/web/app.js", import.meta.url), "utf8");
const start = source.indexOf("function isVerifiedReclaimEstimate");
const end = source.indexOf("\n}\n", start) + 2;
assert.ok(start >= 0 && end > start, "reclaim-estimate helper must remain present");
const context = {};
vm.runInNewContext(source.slice(start, end), context);

test("verified reclaim accepts a numeric zero", () => {
  assert.equal(context.isVerifiedReclaimEstimate("verified", 0), true);
  assert.equal(context.isVerifiedReclaimEstimate("verified", 4096), true);
});

test("verified reclaim rejects missing, coerced, boolean, and negative values", () => {
  for (const value of [null, "", "0", "4096", true, false, NaN, -1]) {
    assert.equal(context.isVerifiedReclaimEstimate("verified", value), false, String(value));
  }
  assert.equal(context.isVerifiedReclaimEstimate("unknown", 0), false);
});
