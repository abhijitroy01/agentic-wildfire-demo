/* Shared browser-check helpers: page navigation, contrast, agent activity rows and walkthrough screenshots. */
const assert = require("node:assert/strict");
const path = require("node:path");
const {STAGE_1, STAGE_2, STAGE_3, GUIDED_TEST_ID} = require("./ui-names.cjs");

/** The time limit of every condition wait and action in the browser checks, twice Playwright's 30 s default, which
 * once ended a passing run under load. A wait returns as soon as its condition holds, so a passing run is no slower;
 * release_checks.browser_check gives the harness no timeout of its own. */
const CONDITION_TIMEOUT_MS = 60_000;

/** Agent activity rows in document order as [data-kind, data-status, main text, status text]. */
const activityRows = activity => activity.locator("li").evaluateAll(rows => rows.map(row => [
  row.dataset.kind ?? null, row.dataset.status ?? null,
  row.querySelector(":scope > div > strong")?.textContent ?? null,
  row.querySelector(":scope > div > span")?.textContent ?? null]));

/** Assert every visible `selector` text under `locator` has at least 4.5:1 contrast against its blended background. */
async function checkContrast(locator, selector) {
  const ratios = await locator.evaluate((root, selector) => {
    const rgba = value => value.match(/[\d.]+/g).map(Number);
    const blend = (a, b) => a.slice(0,3).map((v,i) => v*(a[3] ?? 1)+b[i]*(1-(a[3] ?? 1)));
    const luminance = rgb => rgb.map(v => {
      const c=v/255; return c<=.04045 ? c/12.92 : ((c+.055)/1.055)**2.4;
    }).reduce((sum,v,i) => sum+v*[.2126,.7152,.0722][i],0);
    return [...root.querySelectorAll(selector)].filter(node => node.getBoundingClientRect().height > 0).map(node => {
      const ancestors = []; for (let n=node; n; n=n.parentElement) ancestors.unshift(n);
      const background = ancestors.reduce((color,n) => blend(rgba(getComputedStyle(n).backgroundColor),color),[255,255,255]);
      const surface=luminance(background), ink=luminance(blend(rgba(getComputedStyle(node).color),background));
      return [(Math.max(ink,surface)+.05)/(Math.min(ink,surface)+.05),
        `${node.tagName.toLowerCase()} "${node.textContent.trim().slice(0, 40)}"`];
    });
  }, selector);
  const [lowest, where] = ratios.reduce((worst, entry) => entry[0] < worst[0] ? entry : worst, [Infinity, "nothing"]);
  assert(ratios.length && lowest>=4.5, `${selector} contrast: ${lowest} at ${where}`);
}

/** Resolve after two animation frames, so a change the page scheduled for the next frame has been painted. */
const nextFrames = page => page.evaluate(() => new Promise(resolve =>
  requestAnimationFrame(() => requestAnimationFrame(resolve))));

/** Use only actual same-origin module resources the browser already loaded, including Vite's development module. */
function observedMapViewModules(resources, origin) {
  return [...new Set(resources.flatMap(name => {
    if (typeof name !== "string") return [];
    let url;
    try { url = new URL(name); } catch { return []; }
    return url.origin === origin && ["http:", "https:"].includes(url.protocol) &&
      !url.username && !url.password && !url.hash &&
      /\/(?:MapView-[A-Za-z0-9_-]+|@arcgis_core_views_MapView)\.js$/.test(url.pathname) ? [url.href] : [];
  }))];
}

/** Matching canvas dimensions alone can precede the SDK's final resize frame and leave an unpainted map edge. */
function nativeMapViewIdle(view) {
  return Boolean(view && view.ready && view.stationary && !view.resizing && !view.updating && !view.destroyed &&
    view.container && view.width === view.container.clientWidth && view.height === view.container.clientHeight &&
    view.allLayerViews.toArray().every(layer => !layer.updating));
}

/** Observe the public View.views collection from the application's already-loaded native module. */
async function observeNativeMapViews(page) {
  await page.evaluate(`(async () => {
    if (![...document.querySelectorAll(".esri-view-surface canvas")].some(canvas => canvas.getClientRects().length))
      return;
    if (window.__wfmBrowserNativeViews?.length) return;
    const observedMapViewModules = ${observedMapViewModules.toString()};
    const modules = observedMapViewModules(performance.getEntriesByType("resource").map(entry => entry.name),
      location.origin);
    if (!modules.length) throw new Error("The visible native map has no observed same-origin MapView module.");
    const collections = [];
    for (const source of modules) {
      const exports = await import(source);
      for (const value of Object.values(exports)) {
        const collection = (typeof value === "function" ? value : value?.default)?.views;
        if (collection && typeof collection.toArray === "function" && !collections.includes(collection))
          collections.push(collection);
      }
    }
    if (!collections.length) throw new Error("The loaded native MapView module has no public View.views collection.");
    window.__wfmBrowserNativeViews = collections;
  })()`);
}

/** Wait for matching drawing buffers and native view/layer resize completion, then paint the final frame. */
async function mapsRedrawn(page) {
  await observeNativeMapViews(page);
  await page.waitForFunction(`(() => {
    const nativeMapViewIdle = ${nativeMapViewIdle.toString()};
    const nativeViews = (window.__wfmBrowserNativeViews || []).flatMap(collection => collection.toArray());
    return [...document.querySelectorAll(".esri-view-surface canvas, canvas.maplibregl-canvas")]
    .filter(canvas => canvas.getClientRects().length).every(canvas => {
      const container = canvas.closest(".esri-view-surface, .maplibregl-map");
      const close = (a, b) => Math.abs(a - b) <= 1;
      const native = canvas.closest(".esri-view-surface") ?
        nativeViews.find(view => view.container?.contains(canvas) && !view.destroyed) : null;
      return container?.dataset.navigating !== "true" &&
        close(canvas.clientWidth, container.clientWidth) && close(canvas.clientHeight, container.clientHeight) &&
        close(canvas.width, canvas.clientWidth * devicePixelRatio) &&
        close(canvas.height, canvas.clientHeight * devicePixelRatio) &&
        (!canvas.closest(".esri-view-surface") || nativeMapViewIdle(native));
    });
  })()`);
  await nextFrames(page);
}

/** Overlay cards and native scale must stay inside their map and leave every other control uncovered. Wind is optional
 * when no observation supplies it; a journey can require additional cards such as the response-unit legend. */
const mapOverlayLayout = (map, extraRequired = []) => map.evaluate((root, extraRequired) => {
  const required = [".fire-exposure-summary", ".map-legend", ".map-context-controls", ".esri-scale-bar",
    ".map-undock-control", ...extraRequired];
  const selectors = [...new Set([...required,
    ...[".wind-legend", ".esri-zoom"].filter(selector => root.querySelector(selector))])];
  const boxes = selectors.map(selector => [selector, root.querySelector(selector)?.getBoundingClientRect()]);
  const missing = boxes.filter(([, box]) => !box?.width || !box?.height).map(([selector]) => selector);
  const visible = boxes.filter(([, box]) => box?.width && box?.height), overlaps = [];
  visible.forEach(([a, p], index) => visible.slice(index + 1).forEach(([b, q]) => {
    if (p.left < q.right && q.left < p.right && p.top < q.bottom && q.top < p.bottom) overlaps.push(`${a} × ${b}`);
  }));
  const bounds = root.getBoundingClientRect();
  const outside = visible.filter(([, box]) => box.left < bounds.left - 1 || box.right > bounds.right + 1
    || box.top < bounds.top - 1 || box.bottom > bounds.bottom + 1).map(([selector]) => selector);
  return {missing, overlaps, outside};
}, extraRequired);

/** The one deliberate wait: a quiet window of `ms` that proves something does not happen. Fails with `reason` if the
 * broker saw a request (or exercise stream) matching `predicate` during the window. */
async function expectNoRequest(page, b, ms, predicate, reason) {
  const from = b.requests.length;
  await page.waitForTimeout(ms);
  assert.deepEqual(b.requests.slice(from).filter(predicate).map(r => `${r.method} ${r.endpoint}`), [], reason);
}

/** Init script: record every Element.requestFullscreen call, whether it ran inside a user activation, and whether the
 * browser granted or refused it. The page receives the browser's own promise, so the app sees no difference. */
function recordFullscreenRequests() {
  const request = Element.prototype.requestFullscreen;
  window.__fullscreenRequests = [];
  Element.prototype.requestFullscreen = function (options) {
    const record = {element: this, activated: navigator.userActivation?.isActive ?? null, state: "pending", error: null};
    window.__fullscreenRequests.push(record);
    const pending = request.call(this, options);
    pending.then(() => { record.state = "granted"; },
      error => { record.state = "refused"; record.error = String(error?.message || error); });
    return pending;
  };
}

/** Click `button` to undock the map in `scope`, then wait on the app's state: the toggle reads expanded and the
 * `.map-undocked` element exists. The click must request native fullscreen on that element; once the request settles,
 * granted means document.fullscreenElement is the element, and refused means the viewport fallback covers the viewport
 * (useMapFullscreen keeps undocking without the API), which `t` reports. Returns the toggle for exitMapFullscreen. */
async function enterMapFullscreen(page, button, scope, t) {
  const toggle = await button.elementHandle();
  const before = await page.evaluate(() => window.__fullscreenRequests.length);
  await toggle.click();
  await page.waitForFunction(node => node.getAttribute("aria-expanded") === "true", toggle);
  const undocked = scope.locator(".map-undocked");
  await undocked.waitFor();
  const request = await undocked.evaluate((node, index) => ({requests: window.__fullscreenRequests.length - index,
    target: window.__fullscreenRequests[index]?.element === node,
    activated: window.__fullscreenRequests[index]?.activated ?? null}), before);
  assert.equal(request.requests, 1, "The toggle click requests native fullscreen once");
  assert.equal(request.activated, true, "Native fullscreen is requested inside the user's click");
  assert.ok(request.target, "Native fullscreen is requested on the undocked map element");
  await page.waitForFunction(index => window.__fullscreenRequests[index].state !== "pending", before);
  const outcome = await undocked.evaluate((node, index) => {
    const record = window.__fullscreenRequests[index], box = node.getBoundingClientRect();
    return {state: record.state, error: record.error, fullscreen: document.fullscreenElement === node,
      covers: getComputedStyle(node).position === "fixed" && box.left <= 0 && box.top <= 0 &&
        box.right >= innerWidth - 1 && box.bottom >= innerHeight - 1};
  }, before);
  if (outcome.state === "granted") {
    assert.ok(outcome.fullscreen, "A granted request makes the undocked map the fullscreen element");
    t?.diagnostic("Native fullscreen granted on the undocked map");
  } else {
    assert.ok(outcome.covers, "A refused request leaves the undocked map fixed over the whole viewport");
    t?.diagnostic(`Native fullscreen refused (${outcome.error}); the viewport fallback covers the viewport`);
  }
  return toggle;
}

/** Press Escape and wait until the toggle reads collapsed, nothing is undocked and nothing is fullscreen. */
async function exitMapFullscreen(page, toggle, scope) {
  await page.keyboard.press("Escape");
  await page.waitForFunction(node => node.getAttribute("aria-expanded") === "false" && !document.fullscreenElement,
    toggle);
  await scope.locator(".map-undocked").waitFor({state: "detached"});
}

/** The page-level locators and actions every stage uses. */
function journey(page, verification) {
  const workflow = page.getByRole("navigation", {name: "Incident workflow", exact: true});
  const exercise = page.locator('main[aria-label="Standalone firetruck exercise"]');
  const j = {page, verification, STAGE_1, STAGE_2, STAGE_3, workflow, exercise,
    guided: page.getByTestId(GUIDED_TEST_ID),
    shot: name => path.join(verification, name),
    /** A numbered step in the header's workflow navigation. */
    stage: name => workflow.getByRole("button", {name, exact: true}),
    /** Wait until `name` is the current step. */
    currentStage: name => workflow.locator('button[aria-current="step"]', {hasText: name}).waitFor(),
    // Stage 1's live agent activity, its supervisor rows and the answer drawer.
    activity: page.locator(".investigation-assistant .agent-activity"),
    received: ["supervisor", null, "Supervisor: received the request", null],
    answerDrawer: page.locator(".detail-panel-wrapper"),
    // The display-only strip, stage 3's findings and stage 1's search area.
    strip: page.getByRole("region", {name: "Selected scenario", exact: true}),
    searchArea: page.getByRole("region", {name: "Search area", exact: true}),
  };
  j.steps = j.activity.locator('li[data-kind="supervisor"] > ol[aria-label="Supervisor delegation and synthesis"]');
  j.findings = j.guided.getByRole("region", {name: "Response findings", exact: true});
  /** Open the standalone exercise from 3 · Response Tracker. */
  j.openExercise = async () => {
    await page.getByRole("button", {name: "Standalone firetruck exercise", exact: true}).click();
    await exercise.waitFor();
  };
  /** Walkthrough screenshots of `target` (an element or the page) at a desktop and a phone width, without page overflow. */
  j.walkthrough = async (name, target) => {
    for (const [width, height] of [[1440, 1000], [390, 844]]) {
      await page.setViewportSize({width, height});
      await mapsRedrawn(page);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true,
        `Walkthrough ${name} overflows horizontally at ${width}px`);
      const file = j.shot(`walkthrough-${name}-${width}.png`);
      await (target === page ? page.screenshot({path: file, fullPage: true}) : target.screenshot({path: file}));
    }
    await page.setViewportSize({width: 1512, height: 1100});
  };
  return j;
}

module.exports = {activityRows, checkContrast, CONDITION_TIMEOUT_MS, enterMapFullscreen, exitMapFullscreen, mapOverlayLayout,
  expectNoRequest, journey, mapsRedrawn, nativeMapViewIdle, nextFrames, observedMapViewModules, recordFullscreenRequests};
