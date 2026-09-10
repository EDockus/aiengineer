/** Tiny assertion + reporting harness for the runtime checks. */
export const results = [];

let current = null;

export async function check(name, body) {
  current = { name, assertions: [], failures: [] };
  results.push(current);
  const started = Date.now();
  try {
    await body();
  } catch (error) {
    current.failures.push(`threw: ${error && error.stack ? error.stack : error}`);
  }
  current.ms = Date.now() - started;
  const mark = current.failures.length === 0 ? "PASS" : "FAIL";
  console.log(`${mark}  ${name}  (${current.ms} ms)`);
  for (const line of current.assertions) console.log(`        ${line}`);
  for (const line of current.failures) console.log(`   ---> ${line}`);
  current = null;
}

/** Records evidence that is printed whether or not the check passes. */
export function note(line) {
  current.assertions.push(line);
}

export function assert(condition, description) {
  if (condition) current.assertions.push(`ok: ${description}`);
  else current.failures.push(`NOT ok: ${description}`);
  return Boolean(condition);
}

export function assertEqual(actual, expected, description) {
  return assert(
    Object.is(actual, expected),
    `${description} (expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)})`,
  );
}

export function assertIncludes(haystack, needle, description) {
  return assert(
    typeof haystack === "string" && haystack.includes(needle),
    `${description} — looked for ${JSON.stringify(needle)} in ${JSON.stringify(haystack)}`,
  );
}

export async function rejects(promise, description) {
  try {
    await promise;
    assert(false, `${description} — it resolved instead`);
    return undefined;
  } catch (error) {
    assert(true, `${description} — threw: ${error.message}`);
    return error;
  }
}

export const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** Waits for a condition, polling fast, up to `timeoutMs`. */
export async function waitFor(predicate, timeoutMs, description) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) return true;
    await sleep(10);
  }
  assert(false, `timed out waiting for ${description}`);
  return false;
}

export function summary() {
  const failed = results.filter((r) => r.failures.length > 0);
  const assertions = results.reduce((n, r) => n + r.assertions.length, 0);
  console.log("");
  console.log(
    `${results.length - failed.length}/${results.length} checks passed, ${assertions} assertions`,
  );
  if (failed.length > 0) {
    console.log(`failed: ${failed.map((r) => r.name).join(", ")}`);
  }
  return failed.length === 0;
}
