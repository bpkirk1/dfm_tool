// In-browser 3D viewer for the uploaded STEP part with DFM-concern overlays.
//
// STEP is a B-rep format, so we tessellate it client-side with an
// OpenCASCADE WebAssembly build (occt-import-js, loaded globally) and render
// the resulting meshes with three.js. No heavy Python CAD dependency required.
//
// Concern highlighting: when the backend can localize a finding (e.g. the
// off-gauge wall regions from the model-derived thickness map) it emits markers
// with a model-space `location`. The viewer keeps the body in a neutral tint and
// pins those exact areas with colored markers — clicking a concern zooms to its
// marker. If a run has no localized markers we fall back to tinting the part by
// the worst verdict so the signal isn't lost.

import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

const OCCT_VERSION = "0.0.23";
const OCCT_BASE = `https://cdn.jsdelivr.net/npm/occt-import-js@${OCCT_VERSION}/dist/`;

const VERDICT_COLOR = {
  fail: 0xdc2626,
  flag: 0xf59e0b,
  pass: 0x10b981,
  manual: 0x94a3b8,
};
const VERDICT_RANK = { fail: 3, flag: 2, manual: 1, pass: 0 };
const NEUTRAL_BODY = 0x9aa7b8; // brushed-metal neutral when markers carry the signal

function setStatus(text) {
  const el = document.getElementById("dfm-status");
  if (el) el.textContent = text;
}

// Returns true if the browser can actually create a WebGL context. On locked-down
// corporate desktops / RDP / Citrix, hardware acceleration is often off and the
// THREE.WebGLRenderer constructor throws — we want to detect that gracefully.
function webglSupported() {
  try {
    const c = document.createElement("canvas");
    return !!(
      window.WebGLRenderingContext &&
      (c.getContext("webgl") || c.getContext("experimental-webgl"))
    );
  } catch {
    return false;
  }
}

// Drop a centered message into the viewer box (used when 3D can't render).
function showOverlay(container, title, htmlBody) {
  if (!container) return;
  let ov = container.querySelector(".dfm-overlay");
  if (!ov) {
    ov = document.createElement("div");
    ov.className =
      "dfm-overlay absolute inset-0 flex items-center justify-center p-6 text-center";
    container.appendChild(ov);
  }
  ov.innerHTML =
    `<div class="max-w-sm">` +
    `<div class="text-sm font-semibold text-slate-700">${title}</div>` +
    `<div class="text-xs text-slate-500 mt-1 leading-snug">${htmlBody}</div>` +
    `</div>`;
}

function readJson(id) {
  const tag = document.getElementById(id);
  if (!tag) return [];
  try {
    return JSON.parse(tag.textContent || "[]");
  } catch {
    return [];
  }
}

function worstVerdict(results) {
  let worst = "pass";
  for (const r of results) {
    if ((VERDICT_RANK[r.verdict] ?? 0) > (VERDICT_RANK[worst] ?? 0)) {
      worst = r.verdict;
    }
  }
  return worst;
}

// Module-level handles so the render loop can animate markers + framing.
const markerObjs = [];
let camRef = null;
let controlsRef = null;
let groupRef = null;
let focusAnim = null;

async function initViewer(container) {
  const modelUrl = container.dataset.modelUrl;
  const results = readJson("dfm-results");
  const markers = readJson("dfm-markers").filter((m) => Array.isArray(m.location));
  const hasMarkers = markers.length > 0;
  const worst = worstVerdict(results);

  // Build the (DOM-only) concern list FIRST so it always works, even if 3D
  // rendering is unavailable in this browser/environment.
  buildConcernList(results, markers, (c) => focusConcern(c, worst));

  // ---- WebGL availability gate -------------------------------------------
  if (!webglSupported()) {
    setStatus("3D rendering unavailable (WebGL is disabled).");
    showOverlay(
      container,
      "3D preview unavailable",
      "WebGL is disabled or unsupported in this browser. Enable hardware " +
        "acceleration / WebGL (or open the report in Chrome/Edge) to see the part. " +
        "The DFM concern list on the right still works."
    );
    return;
  }

  // ---- three.js scene ----------------------------------------------------
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0xf1f5f9);

  const camera = new THREE.PerspectiveCamera(
    45,
    container.clientWidth / Math.max(container.clientHeight, 1),
    0.001,
    1e6
  );
  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ antialias: true });
  } catch (e) {
    setStatus("Could not create a WebGL context.");
    showOverlay(
      container,
      "3D preview unavailable",
      "This browser refused a WebGL context (often hardware acceleration is " +
        "off on remote/virtual desktops). The concern list still works."
    );
    return;
  }
  renderer.setPixelRatio(window.devicePixelRatio);
  renderer.setSize(container.clientWidth, container.clientHeight);
  container.appendChild(renderer.domElement);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  camRef = camera;
  controlsRef = controls;

  scene.add(new THREE.AmbientLight(0xffffff, 0.75));
  const key = new THREE.DirectionalLight(0xffffff, 0.85);
  key.position.set(1, 1.4, 1.1);
  scene.add(key);
  const fill = new THREE.DirectionalLight(0xffffff, 0.4);
  fill.position.set(-1, -0.8, -1);
  scene.add(fill);

  const group = new THREE.Group();
  scene.add(group);
  groupRef = group;

  // The body is tinted by worst verdict ONLY when we can't localize anything;
  // with markers present the body stays neutral so the markers read clearly.
  const bodyVerdict = hasMarkers ? null : worst;

  // ---- load OpenCASCADE WASM and tessellate ------------------------------
  if (typeof occtimportjs === "undefined") {
    setStatus("3D kernel script didn't load (offline?). Concern list still works.");
    startLoop(renderer, scene, camera, controls);
    return;
  }

  setStatus("Loading 3D kernel…");
  let occt;
  try {
    occt = await occtimportjs({ locateFile: (f) => OCCT_BASE + f });
  } catch (e) {
    setStatus("Could not load the 3D kernel (offline?).");
    startLoop(renderer, scene, camera, controls);
    return;
  }

  setStatus("Fetching model…");
  const resp = await fetch(modelUrl);
  if (!resp.ok) {
    setStatus("Model file not available for this run.");
    startLoop(renderer, scene, camera, controls);
    return;
  }
  const bytes = new Uint8Array(await resp.arrayBuffer());

  setStatus("Tessellating STEP…");
  let result;
  try {
    result = occt.ReadStepFile(bytes, null);
  } catch (e) {
    setStatus("Could not read this STEP file.");
    startLoop(renderer, scene, camera, controls);
    return;
  }

  if (!result || !result.success || !result.meshes || result.meshes.length === 0) {
    setStatus(
      "No solid B-rep geometry in this STEP (points-only or sketch). Real parts render here."
    );
    startLoop(renderer, scene, camera, controls);
    return;
  }

  const baseColor = new THREE.Color(
    bodyVerdict ? VERDICT_COLOR[bodyVerdict] ?? 0x3b82f6 : NEUTRAL_BODY
  );
  for (const m of result.meshes) {
    const geom = new THREE.BufferGeometry();
    geom.setAttribute(
      "position",
      new THREE.Float32BufferAttribute(m.attributes.position.array, 3)
    );
    if (m.attributes.normal) {
      geom.setAttribute(
        "normal",
        new THREE.Float32BufferAttribute(m.attributes.normal.array, 3)
      );
    }
    if (m.index) geom.setIndex(Array.from(m.index.array));
    if (!m.attributes.normal) geom.computeVertexNormals();

    const mat = new THREE.MeshStandardMaterial({
      color: baseColor.clone(),
      metalness: 0.25,
      roughness: 0.55,
      side: THREE.DoubleSide,
      // When markers carry the signal, let them show through a translucent body.
      transparent: hasMarkers,
      opacity: hasMarkers ? 0.82 : 1.0,
    });
    const mesh = new THREE.Mesh(geom, mat);
    mesh.userData.label = m.name || "solid";
    mesh.userData.baseColor = baseColor.clone();
    group.add(mesh);
  }

  // Size markers relative to the part, then drop them at their model-space points.
  const box = new THREE.Box3().setFromObject(group);
  const size = box.getSize(new THREE.Vector3());
  const maxDim = Math.max(size.x, size.y, size.z) || 1;
  addMarkers(group, markers, maxDim);

  frame(group, camera, controls);
  enableHover(container, renderer, camera, group);
  startLoop(renderer, scene, camera, controls);

  const counts = results.reduce((a, r) => ((a[r.verdict] = (a[r.verdict] || 0) + 1), a), {});
  if (hasMarkers) {
    setStatus(
      `Rendered ${result.meshes.length} face(s) · ` +
        `${markers.length} area(s) pinned (${counts.fail || 0} fail · ${counts.flag || 0} flag). ` +
        `Click a concern to zoom in.`
    );
  } else {
    setStatus(
      `Rendered ${result.meshes.length} face(s). ` +
        `${counts.fail || 0} fail · ${counts.flag || 0} flag · part tinted by worst verdict (${worst}).`
    );
  }

  // resize handling
  const ro = new ResizeObserver(() => {
    const w = container.clientWidth;
    const h = container.clientHeight;
    if (w && h) {
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
      renderer.setSize(w, h);
    }
  });
  ro.observe(container);
}

// Build a glowing marker (sphere + halo ring) at each model-space location.
// Markers draw on top of the body (depthTest off) so an interior wall still reads.
function addMarkers(group, markers, maxDim) {
  const r = Math.max(maxDim * 0.018, 1e-4);
  markers.forEach((mk, i) => {
    const hex = VERDICT_COLOR[mk.verdict] ?? VERDICT_COLOR.fail;
    const pin = new THREE.Group();
    pin.position.set(mk.location[0], mk.location[1], mk.location[2]);

    const core = new THREE.Mesh(
      new THREE.SphereGeometry(r, 20, 20),
      new THREE.MeshBasicMaterial({ color: hex, depthTest: false, transparent: true })
    );
    core.renderOrder = 999;
    pin.add(core);

    const halo = new THREE.Mesh(
      new THREE.RingGeometry(r * 1.7, r * 2.4, 28),
      new THREE.MeshBasicMaterial({
        color: hex,
        side: THREE.DoubleSide,
        transparent: true,
        opacity: 0.6,
        depthTest: false,
      })
    );
    halo.renderOrder = 999;
    pin.add(halo);

    pin.userData = {
      rule_id: mk.rule_id,
      parameter: mk.parameter,
      label: mk.label,
      baseR: r,
      core,
      halo,
    };
    group.add(pin);
    markerObjs.push(pin);
  });
}

function frame(group, camera, controls) {
  const box = new THREE.Box3().setFromObject(group);
  const size = box.getSize(new THREE.Vector3());
  const center = box.getCenter(new THREE.Vector3());
  group.position.sub(center); // recenter at origin

  const maxDim = Math.max(size.x, size.y, size.z) || 1;
  camera.near = maxDim / 1000;
  camera.far = maxDim * 100;
  camera.position.set(maxDim * 1.3, maxDim * 1.0, maxDim * 1.8);
  camera.updateProjectionMatrix();
  controls.target.set(0, 0, 0);
  controls.update();
}

function startLoop(renderer, scene, camera, controls) {
  function animate() {
    requestAnimationFrame(animate);
    const t = performance.now() / 1000;
    // Pulse markers and keep their halos facing the camera.
    for (const pin of markerObjs) {
      const s = 1 + 0.25 * Math.sin(t * 4);
      pin.userData.core.scale.setScalar(s);
      pin.userData.halo.scale.setScalar(s);
      pin.userData.halo.quaternion.copy(camera.quaternion);
    }
    if (focusAnim) focusAnim(performance.now());
    controls.update();
    renderer.render(scene, camera);
  }
  animate();
}

function enableHover(container, renderer, camera, group) {
  const raycaster = new THREE.Raycaster();
  raycaster.params.Line = raycaster.params.Line || {};
  const pointer = new THREE.Vector2();
  let hovered = null;

  const tip = document.createElement("div");
  tip.className =
    "pointer-events-none absolute z-10 px-2 py-1 text-[11px] rounded bg-slate-900 text-white hidden";
  container.appendChild(tip);

  renderer.domElement.addEventListener("pointermove", (ev) => {
    const rect = renderer.domElement.getBoundingClientRect();
    pointer.x = ((ev.clientX - rect.left) / rect.width) * 2 - 1;
    pointer.y = -((ev.clientY - rect.top) / rect.height) * 2 + 1;
    raycaster.setFromCamera(pointer, camera);

    // Prefer marker labels (the actionable bits) over raw face labels.
    const markerHits = raycaster.intersectObjects(markerObjs, true);
    if (markerHits.length) {
      const pin = markerHits[0].object.parent;
      tip.textContent = pin.userData?.label || "concern";
      tip.style.left = `${ev.clientX - rect.left + 10}px`;
      tip.style.top = `${ev.clientY - rect.top + 10}px`;
      tip.classList.remove("hidden");
      if (hovered) {
        hovered.material.emissive?.setHex(0x000000);
        hovered = null;
      }
      return;
    }

    const hits = raycaster.intersectObjects(group.children, false);
    if (hovered && (!hits.length || hits[0].object !== hovered)) {
      hovered.material.emissive?.setHex(0x000000);
      hovered = null;
    }
    if (hits.length && hits[0].object.isMesh) {
      hovered = hits[0].object;
      hovered.material.emissive?.setHex(0x334155);
      tip.textContent = hovered.userData.label || "face";
      tip.style.left = `${ev.clientX - rect.left + 10}px`;
      tip.style.top = `${ev.clientY - rect.top + 10}px`;
      tip.classList.remove("hidden");
    } else {
      tip.classList.add("hidden");
    }
  });
  renderer.domElement.addEventListener("pointerleave", () => tip.classList.add("hidden"));
}

// Click handler for a concern row: zoom to its marker(s), else pulse the part.
function focusConcern(concern, worst) {
  // Match by rule id, or by parameter so sibling rules on the same measured
  // feature (e.g. both inside-radius rules) zoom to the one pinned corner.
  const mine = markerObjs.filter(
    (p) =>
      p.userData.rule_id === concern.rule_id ||
      (p.userData.parameter && p.userData.parameter === concern.parameter)
  );
  if (mine.length && camRef && controlsRef) {
    flashMarkers(mine);
    frameMarkers(mine);
    const labels = mine.map((p) => p.userData.label).join(" · ");
    if (labels) setStatus(labels);
  } else if (groupRef) {
    pulse(groupRef, concern.verdict || worst);
  }
}

// Smoothly move the orbit target + camera to enclose the given markers.
function frameMarkers(pins) {
  const box = new THREE.Box3();
  const v = new THREE.Vector3();
  for (const p of pins) box.expandByPoint(p.getWorldPosition(v.clone()));
  const target = box.getCenter(new THREE.Vector3());
  const spread = box.getSize(new THREE.Vector3()).length() || 0;

  // Distance: tight on a single point, looser for a spread-out cluster.
  const fullBox = new THREE.Box3().setFromObject(groupRef);
  const partDim = fullBox.getSize(new THREE.Vector3()).length() || 1;
  const dist = Math.max(spread * 1.8, partDim * 0.28);

  const dir = camRef.position.clone().sub(controlsRef.target).normalize();
  const camTo = target.clone().add(dir.multiplyScalar(dist));
  const tgtFrom = controlsRef.target.clone();
  const camFrom = camRef.position.clone();

  const start = performance.now();
  const dur = 600;
  focusAnim = (now) => {
    const t = Math.min((now - start) / dur, 1);
    const e = t * t * (3 - 2 * t); // smoothstep
    controlsRef.target.lerpVectors(tgtFrom, target, e);
    camRef.position.lerpVectors(camFrom, camTo, e);
    camRef.updateProjectionMatrix();
    if (t >= 1) focusAnim = null;
  };
}

function flashMarkers(pins) {
  const start = performance.now();
  const dur = 900;
  function step(now) {
    const t = Math.min((now - start) / dur, 1);
    const grow = 1 + 1.6 * Math.sin(t * Math.PI);
    for (const p of pins) {
      p.userData.core.scale.setScalar(grow);
      p.userData.halo.scale.setScalar(grow);
    }
    if (t < 1) requestAnimationFrame(step);
  }
  requestAnimationFrame(step);
}

let pulseActive = false;
function pulse(group, worst) {
  if (pulseActive) return;
  pulseActive = true;
  const hex = VERDICT_COLOR[worst] ?? 0x3b82f6;
  const start = performance.now();
  const dur = 900;
  function step(now) {
    const t = Math.min((now - start) / dur, 1);
    const intensity = Math.sin(t * Math.PI); // 0 -> 1 -> 0
    const e = new THREE.Color(hex).multiplyScalar(intensity * 0.8);
    group.children.forEach((m) => m.material?.emissive && m.material.emissive.copy(e));
    if (t < 1) requestAnimationFrame(step);
    else {
      group.children.forEach(
        (m) => m.material?.emissive && m.material.emissive.setHex(0x000000)
      );
      pulseActive = false;
    }
  }
  requestAnimationFrame(step);
}

function buildConcernList(results, markers, onClick) {
  const ul = document.getElementById("dfm-concerns");
  if (!ul) return;
  const locatedIds = new Set(markers.map((m) => m.rule_id));
  const locatedParams = new Set(markers.map((m) => m.parameter).filter(Boolean));
  const isLocated = (c) => locatedIds.has(c.rule_id) || locatedParams.has(c.parameter);
  const concerns = results.filter((r) => r.verdict !== "pass");
  if (!concerns.length) {
    ul.innerHTML =
      '<li class="text-emerald-700 text-sm">No concerns — all evaluable rules pass.</li>';
    return;
  }
  const order = { fail: 0, flag: 1, manual: 2 };
  concerns.sort((a, b) => (order[a.verdict] ?? 9) - (order[b.verdict] ?? 9));

  for (const c of concerns) {
    const li = document.createElement("li");
    const color = "#" + (VERDICT_COLOR[c.verdict] ?? 0x94a3b8).toString(16).padStart(6, "0");
    const pinned = isLocated(c);
    li.className =
      "rounded border border-slate-200 px-2 py-1.5 cursor-pointer hover:bg-slate-50";
    li.innerHTML =
      `<div class="flex items-center gap-2">` +
      `<span class="inline-block w-2.5 h-2.5 rounded-full" style="background:${color}"></span>` +
      `<span class="font-mono text-[11px]">${escapeHtml(c.rule_id)}</span>` +
      (pinned
        ? `<span class="text-[9px] px-1 rounded bg-slate-100 text-slate-500" title="Pinned on the 3D part">PIN</span>`
        : "") +
      `<span class="ml-auto text-[10px] uppercase font-semibold" style="color:${color}">${escapeHtml(c.verdict)}</span>` +
      `</div>` +
      `<div class="text-[11px] text-slate-500 mt-0.5">${escapeHtml(c.parameter)} · limit ${escapeHtml(String(c.limit_detail ?? ""))}</div>` +
      (c.source ? `<div class="text-[10px] text-slate-400">${escapeHtml(c.source)}</div>` : "");
    li.addEventListener("click", () => onClick && onClick(c));
    ul.appendChild(li);
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (ch) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]
  ));
}

// Kick things off LAST, so every module-level binding above (camRef, controlsRef,
// groupRef, markerObjs, …) is initialized before initViewer touches it. Calling
// this near the top would hit a temporal-dead-zone error on those `let` bindings.
const container = document.getElementById("dfm-viewer");
if (container) {
  initViewer(container).catch((err) => {
    console.error(err);
    const msg = (err && (err.message || err.name)) || String(err);
    setStatus("3D viewer error: " + msg);
    showOverlay(
      container,
      "3D viewer could not start",
      escapeHtml(msg) +
        "<br><span class='text-slate-400'>The concern list on the right still works.</span>"
    );
  });
}
