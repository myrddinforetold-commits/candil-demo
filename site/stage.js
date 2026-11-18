// Candil demo stage: the Rev C model in a room whose light can go out, a ring with states,
// a live e-paper page, a finger that presses the ring, lid and exploded view.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';

const RING = new THREE.Vector3(0.086, 0.2012, 0.049);       // top of the speak button, model metres (glTF axes)
const SCREEN = new THREE.Vector3(-0.024, 0.112, 0.083);
const VIEWS = {
  hero:   { p: [0.40, 0.30, 0.60], t: [0.0, 0.10, 0.03] },
  ask:    { p: [0.26, 0.40, 0.42], t: [0.03, 0.15, 0.04] },
  read:   { p: [-0.018, 0.13, 0.42], t: [-0.024, 0.112, 0.08] },
  inside: { p: [0.62, 0.42, 0.80], t: [0.0, 0.11, 0.0] },
  ports:  { p: [0.52, 0.16, 0.10], t: [0.10, 0.06, 0.02] },
};
// ring glow by state (emissive intensity), before night glow is added
const RING_STATES = {
  off: () => 0.0,
  ready: () => 0.35,
  listening: t => 1.1 + 0.6 * Math.sin(t * 7.5),
  thinking: t => 0.55 + 0.35 * Math.sin(t * 2.2),
  answering: () => 1.5,
};

export async function mountStage(el, { glbBuffer, manifest, textures = {}, articulations = [], pageCanvas, explodeScale = 0.42 }) {
  const texLoader = new THREE.TextureLoader();
  const loadTex = url => { const t = texLoader.load(url); t.colorSpace = THREE.SRGBColorSpace; t.anisotropy = 8; return t; };
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setClearColor(0x000000, 0);
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.05;
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  el.appendChild(renderer.domElement);

  const scene = new THREE.Scene();
  const pmrem = new THREE.PMREMGenerator(renderer);
  scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;

  const camera = new THREE.PerspectiveCamera(28, 1, 0.01, 10);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true; controls.dampingFactor = 0.08; controls.enablePan = false;
  controls.minDistance = 0.22; controls.maxDistance = 1.4;
  controls.maxPolarAngle = Math.PI * 0.53;

  // lights: day values; power-out scales them toward the night set
  const key = new THREE.DirectionalLight(0xfff4e8, 1.6); key.position.set(0.6, 1.0, 0.8); scene.add(key);
  const rim = new THREE.DirectionalLight(0xdfe8ff, 0.7); rim.position.set(-0.8, 0.5, -0.6); scene.add(rim);
  const hemi = new THREE.HemisphereLight(0xffffff, 0x222222, 0.25); scene.add(hemi);
  const moon = new THREE.DirectionalLight(0x8fa8ff, 0.0); moon.position.set(-0.9, 1.1, -0.4); scene.add(moon);
  const spill = new THREE.PointLight(0xffb06a, 0, 0.5, 2); spill.position.copy(SCREEN).add(new THREE.Vector3(0, -0.075, 0.07)); scene.add(spill);   // warm light falling on the lid below the page
  const ringLight = new THREE.PointLight(0xff6a1a, 0, 0.25, 2); ringLight.position.copy(RING).add(new THREE.Vector3(0, 0.01, 0)); scene.add(ringLight);

  // contact shadow
  const shadowTex = (() => {
    const c = document.createElement('canvas'); c.width = c.height = 256;
    const x = c.getContext('2d'); const gr = x.createRadialGradient(128, 128, 10, 128, 128, 128);
    gr.addColorStop(0, 'rgba(0,0,0,0.55)'); gr.addColorStop(1, 'rgba(0,0,0,0)');
    x.fillStyle = gr; x.fillRect(0, 0, 256, 256); return new THREE.CanvasTexture(c);
  })();
  const shadow = new THREE.Mesh(new THREE.PlaneGeometry(0.6, 0.5), new THREE.MeshBasicMaterial({ map: shadowTex, transparent: true, depthWrite: false }));
  shadow.rotation.x = -Math.PI / 2; scene.add(shadow);

  const gltf = await new GLTFLoader().parseAsync(glbBuffer, '');
  const root = gltf.scene; scene.add(root);
  const byId = new Map(manifest.map(m => [m.id, m]));
  const nodes = [], glow = [], button = [];
  let ring = null, screenMat = null;
  const pageTex = new THREE.CanvasTexture(pageCanvas); pageTex.colorSpace = THREE.SRGBColorSpace; pageTex.anisotropy = 8;

  root.traverse(o => {
    if (!o.isMesh || o.userData.decal) return;
    const info = byId.get(o.name); if (!info) return;
    o.userData.info = info;
    o.userData.explode = new THREE.Vector3(...info.explode);
    o.material = o.material.clone();
    o.material.side = THREE.DoubleSide;
    if (info.mat === 'glass') { o.material.transparent = true; o.material.opacity = 0.14; o.material.depthWrite = false; }
    if (info.mat === 'orange') { o.material.emissive = new THREE.Color('#ff5a14'); o.material.emissiveIntensity = 0.15; glow.push(o); }
    if (info.mat === 'led') { o.material.emissive = new THREE.Color('#fff3d6'); o.material.emissiveIntensity = 0.4; }
    if (/^Speak ring/.test(info.name)) ring = o;
    if (/^Speak button/.test(info.name)) { button.push(o); }
    nodes.push(o);
    if (textures[info.mat]) addDecal(o, textures[info.mat], info.mat === 'lid_label' ? 'min' : 'max', info.mat === 'lid_label');
    else if (info.mat === 'eink_active') {
      o.geometry.computeBoundingBox();
      const bb = o.geometry.boundingBox, w = bb.max.x - bb.min.x, h = bb.max.y - bb.min.y;
      screenMat = new THREE.MeshStandardMaterial({ map: pageTex, emissiveMap: pageTex, emissive: new THREE.Color('#ffc58f'), emissiveIntensity: 0, roughness: 0.93, metalness: 0 });
      const s = new THREE.Mesh(new THREE.PlaneGeometry(w, h), screenMat);
      s.userData.decal = true;
      s.position.set((bb.min.x + bb.max.x) / 2, (bb.min.y + bb.max.y) / 2, bb.max.z + 0.00002);
      o.add(s);
    }
  });
  if (ring) { ring.material.emissive = new THREE.Color('#ff6414'); }

  function addDecal(o, url, side, alpha) {
    o.geometry.computeBoundingBox();
    const bb = o.geometry.boundingBox, sz = bb.getSize(new THREE.Vector3()), c = bb.getCenter(new THREE.Vector3()), eps = 0.00006;
    const mat = new THREE.MeshStandardMaterial({ map: loadTex(url), roughness: 0.9, metalness: 0, transparent: !!alpha, alphaTest: alpha ? 0.02 : 0, polygonOffset: true, polygonOffsetFactor: -2 });
    let geo;
    if (sz.z <= sz.x && sz.z <= sz.y) { geo = new THREE.PlaneGeometry(sz.x, sz.y); geo.translate(c.x, c.y, bb.max.z + eps); }
    else if (side === 'max') { geo = new THREE.PlaneGeometry(sz.x, sz.z); geo.rotateX(-Math.PI / 2); geo.translate(c.x, bb.max.y + eps, c.z); }
    else { geo = new THREE.PlaneGeometry(sz.x, sz.z); geo.rotateX(Math.PI / 2); geo.translate(c.x, bb.min.y - eps, c.z); }
    if (alpha) o.material.color.set('#2b2d31');
    const d = new THREE.Mesh(geo, mat); d.userData.decal = true; o.add(d);
  }

  // lid and handle hinges
  root.updateMatrixWorld(true);
  const arts = articulations.map(a => {
    const carrier = new THREE.Group(), pivot = new THREE.Group(), inner = new THREE.Group();
    pivot.position.fromArray(a.pivot); inner.position.fromArray(a.pivot).multiplyScalar(-1);
    carrier.add(pivot); pivot.add(inner); root.add(carrier); carrier.updateMatrixWorld(true);
    return { ...a, carrier, pivot, inner, angle: 0, target: 0, vec: null };
  });
  for (const n of nodes) {
    const a = arts.find(a => a.match(n.userData.info)); if (!a) continue;
    a.inner.attach(n); n.userData.art = a; a.vec = n.userData.explode;
  }
  applyExplode(0);

  // the finger: a simple rounded form that comes in from the front right and holds the ring
  const finger = new THREE.Group();
  const skin = new THREE.MeshPhysicalMaterial({ color: '#c49a7c', roughness: 0.55, sheen: 0.5, sheenColor: new THREE.Color('#f3d2bf') });
  const r0 = 0.0085, len = 0.075;
  const digit = new THREE.Mesh(new THREE.CapsuleGeometry(r0, len, 8, 20), skin);
  digit.position.y = r0 + len / 2; digit.scale.set(1, 1, 0.86); finger.add(digit);
  const nail = new THREE.Mesh(new THREE.CapsuleGeometry(r0 * 0.62, 0.008, 4, 12), new THREE.MeshPhysicalMaterial({ color: '#e9cdbd', roughness: 0.25, clearcoat: 0.6 }));
  nail.position.set(0, r0 + 0.007, -r0 * 0.62); nail.scale.set(1, 1, 0.35); finger.add(nail);
  const fdir = new THREE.Vector3(0.38, 0.78, 0.5).normalize();      // from fingertip toward the hand
  finger.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), fdir);
  finger.visible = false; scene.add(finger);
  const fingerAt = k => RING.clone().addScaledVector(fdir, 0.11 * k);   // k = 1 far, 0 touching

  // framing
  function resize() {
    const w = el.clientWidth, h = el.clientHeight;
    renderer.setSize(w, h, false); camera.aspect = w / h;
    camera.fov = w / h < 0.9 ? 40 : 28; camera.updateProjectionMatrix();
  }
  new ResizeObserver(resize).observe(el); resize();
  const box = new THREE.Box3().setFromObject(root);
  shadow.position.y = box.min.y - 0.0002;
  camera.position.fromArray(VIEWS.hero.p); controls.target.fromArray(VIEWS.hero.t);

  // explode
  let explode = 0, explodeTarget = 0;
  function applyExplode(t) {
    for (const n of nodes) if (!n.userData.art) n.position.copy(n.userData.explode).multiplyScalar(t * explodeScale);
    for (const a of arts) if (a.vec) a.carrier.position.copy(a.vec).multiplyScalar(t * explodeScale * (a.explodeMul ?? 1));
    for (const b of button) b.userData.base = b.position.clone();
  }

  // animated state
  const S = { power: 1, powerT: 1, front: 0, frontT: 0, ring: 'ready', press: 0, pressT: 0, fingerK: 1, fingerT: 1 };
  let camAnim = null;
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const ease = (a, b, dt, rate) => a + (b - a) * (reduce ? 1 : Math.min(1, dt * rate));
  const clock = new THREE.Clock();

  function tick() {
    const dt = Math.min(0.05, clock.getDelta()), t = clock.elapsedTime;
    if (Math.abs(explodeTarget - explode) > 1e-4) { explode = ease(explode, explodeTarget, dt, 5); applyExplode(explode); }
    for (const a of arts) if (Math.abs(a.target - a.angle) > 1e-4) { a.angle = ease(a.angle, a.target, dt, 2.4); a.pivot.rotation.x = a.angle; }
    S.power = ease(S.power, S.powerT, dt, 1.6);
    S.front = ease(S.front, S.frontT, dt, 2.0);
    const night = 1 - S.power;
    key.intensity = 1.6 * S.power; rim.intensity = 0.7 * S.power + 0.12 * night;
    hemi.intensity = 0.25 * S.power + 0.04 * night; moon.intensity = 0.55 * night;
    scene.environmentIntensity = 0.55 * S.power + 0.035 * night;
    renderer.toneMappingExposure = 1.05 + 0.25 * night;
    // the ring: state light plus its own glow in the dark (photoluminescent)
    const ri = (RING_STATES[S.ring] || RING_STATES.ready)(t) + 1.2 * night;
    if (ring) ring.material.emissiveIntensity = ri;
    ringLight.intensity = 0.004 * ri * (0.3 + night);
    for (const o of glow) if (o !== ring) o.material.emissiveIntensity = 0.15 + 0.9 * night;
    if (screenMat) screenMat.emissiveIntensity = 0.42 * S.front;
    spill.intensity = 0.03 * S.front * night;
    // finger and button
    S.fingerK = ease(S.fingerK, S.fingerT, dt, 5.5);
    finger.visible = S.fingerK < 0.98;
    S.press = ease(S.press, S.pressT, dt, 14);
    for (const b of button) b.position.copy(b.userData.base).add(new THREE.Vector3(0, -0.0013 * S.press, 0));
    finger.position.copy(fingerAt(S.fingerK)).add(new THREE.Vector3(0, -0.0013 * S.press, 0));
    if (camAnim) {
      camAnim.k = Math.min(1, camAnim.k + (reduce ? 1 : dt / camAnim.dur)); const e = camAnim.k < 0.5 ? 4 * camAnim.k ** 3 : 1 - (-2 * camAnim.k + 2) ** 3 / 2;
      camera.position.lerpVectors(camAnim.p0, camAnim.p1, e); controls.target.lerpVectors(camAnim.t0, camAnim.t1, e);
      if (camAnim.k >= 1) camAnim = null;
    }
    controls.update(); renderer.render(scene, camera); raf = requestAnimationFrame(tick);
  }
  let raf = requestAnimationFrame(tick);
  const wait = ms => new Promise(r => setTimeout(r, reduce ? Math.min(ms, 50) : ms));

  return {
    setOpen(open) { for (const a of arts) a.target = open ? 0 : a.closed * Math.PI / 180; },
    setClosedNow() { for (const a of arts) { a.target = a.angle = a.closed * Math.PI / 180; a.pivot.rotation.x = a.angle; } },
    setExplode(t) { explodeTarget = t; },
    setPower(on) { S.powerT = on ? 1 : 0; },
    setFrontLight(level) { S.frontT = level; },
    setRing(state) { S.ring = state; },
    redrawPage() { pageTex.needsUpdate = true; },
    view(name, dur = 1.4) {
      const v = VIEWS[name]; if (!v) return;
      camAnim = { p0: camera.position.clone(), t0: controls.target.clone(), p1: new THREE.Vector3(...v.p), t1: new THREE.Vector3(...v.t), k: 0, dur };
    },
    async press() { S.fingerT = 0; await wait(520); S.pressT = 1; await wait(120); },
    async release() { S.pressT = 0; await wait(120); S.fingerT = 1; await wait(450); },
    dispose() { cancelAnimationFrame(raf); controls.dispose(); renderer.dispose(); renderer.domElement.remove(); },
  };
}
