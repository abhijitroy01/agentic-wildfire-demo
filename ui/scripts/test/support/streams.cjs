/* Byte streams and a bounded wait for the node tests that read AG-UI and radio streams. */
const encoder = new TextEncoder();
/** A finished byte stream with one chunk per string. */
const streamOf = (...chunks) => new ReadableStream({
  start(controller) {
    for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
    controller.close();
  },
});
/** Resolve with the promise, or reject after `ms` so a missing event fails the test instead of hanging it. */
function within(promise, ms, what) {
  let timer;
  const late = new Promise((_resolve, reject) => {
    timer = setTimeout(() => reject(new Error(`${what} did not happen within ${ms} ms`)), ms);
  });
  return Promise.race([promise, late]).finally(() => clearTimeout(timer));
}

module.exports = { encoder, streamOf, within };
