import * as THREE from "three";

// The agent, drawn as a soft cream fur creature with a pale face plate, wearing the logo of the model it runs on.
// Fur is shell-rendered: copies of each part pushed out along their normals, each cut by a noise alpha map at a
// higher threshold, so strands taper toward the tips.

const FUR_BASE = new THREE.Color("#d9c7a8");
const FUR_TIP = new THREE.Color("#f4ead8");
const FUR_LAYERS = 20;
const FUR_LENGTH = 0.075;

let noiseTexture: THREE.Texture | null = null;
function furNoise(): THREE.Texture {
  if (noiseTexture) return noiseTexture;
  const size = 256;
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = size;
  const ctx = canvas.getContext("2d")!;
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, size, size);
  let seed = 7;
  const rand = () => ((seed = (seed * 16807) % 2147483647) / 2147483647);
  for (let i = 0; i < 9000; i++) {
    const v = Math.floor(80 + rand() * 175);
    ctx.fillStyle = `rgb(${v},${v},${v})`;
    ctx.fillRect(Math.floor(rand() * size), Math.floor(rand() * size), 2, 2);
  }
  noiseTexture = new THREE.CanvasTexture(canvas);
  noiseTexture.wrapS = noiseTexture.wrapT = THREE.RepeatWrapping;
  noiseTexture.repeat.set(6, 6);
  return noiseTexture;
}

function shell(geometry: THREE.BufferGeometry, offset: number): THREE.BufferGeometry {
  const g = geometry.clone();
  const pos = g.attributes.position;
  const nrm = g.attributes.normal;
  for (let i = 0; i < pos.count; i++) {
    pos.setXYZ(i, pos.getX(i) + nrm.getX(i) * offset, pos.getY(i) + nrm.getY(i) * offset, pos.getZ(i) + nrm.getZ(i) * offset);
  }
  return g;
}

// A furry mesh group: a solid core plus FUR_LAYERS alpha-tested shells.
function furry(geometry: THREE.BufferGeometry, length = FUR_LENGTH): THREE.Group {
  const group = new THREE.Group();
  const core = new THREE.Mesh(geometry, new THREE.MeshStandardMaterial({ color: FUR_BASE, roughness: 1 }));
  core.castShadow = true;
  core.receiveShadow = true;
  group.add(core);
  for (let i = 1; i <= FUR_LAYERS; i++) {
    const t = i / FUR_LAYERS;
    const material = new THREE.MeshStandardMaterial({
      color: FUR_BASE.clone().lerp(FUR_TIP, t),
      roughness: 1,
      alphaMap: furNoise(),
      alphaTest: 0.25 + t * 0.6,
      transparent: false,
    });
    const layer = new THREE.Mesh(shell(geometry, length * t), material);
    layer.castShadow = i < 4;
    group.add(layer);
  }
  return group;
}

// ---- model logos: official marks (LobeHub icons, MIT) in public/logos, printed on a patch of the creature's own fur

type Brand = "claude" | "openai" | "grok" | "gemini" | "meta" | "other";

export function brandFor(label: string): Brand {
  if (/opus|sonnet|haiku|claude/i.test(label)) return "claude";
  if (/grok/i.test(label)) return "grok";
  if (/gemini/i.test(label)) return "gemini";
  if (/muse|meta/i.test(label)) return "meta";
  if (/\b(sol|terra|luna|gpt)/i.test(label)) return "openai";
  return "other";
}

const LOGO_INK = "#3a2f26"; // single-colour marks are printed in a warm dark brown so they sit in the cream fur

// A canvas texture: cream fur colour fading to transparent at the edges, the logo in the middle.
// The SVG loads asynchronously; the texture updates when it arrives.
function logoTexture(brand: Brand): THREE.CanvasTexture {
  const size = 512;
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = size;
  const ctx = canvas.getContext("2d")!;
  const tex = new THREE.CanvasTexture(canvas);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.anisotropy = 8;
  const c = size / 2;
  const glow = ctx.createRadialGradient(c, c, size * 0.22, c, c, size * 0.5);
  glow.addColorStop(0, "rgba(246,238,224,0.95)");
  glow.addColorStop(1, "rgba(246,238,224,0)");
  ctx.fillStyle = glow;
  ctx.fillRect(0, 0, size, size);
  tex.needsUpdate = true;
  if (brand === "other") return tex;
  fetch(`/logos/${brand}.svg`)
    .then((r) => r.text())
    .then((svg) => {
      const colored = svg.replace(/currentColor/g, LOGO_INK).replace(/width="1em"/, `width="${size}"`).replace(/height="1em"/, `height="${size}"`);
      const img = new Image();
      img.onload = () => {
        const logo = size * 0.56;
        ctx.drawImage(img, c - logo / 2, c - logo / 2, logo, logo);
        tex.needsUpdate = true;
      };
      img.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(colored)}`;
    })
    .catch(() => undefined);
  return tex;
}

// A patch that wraps the body: a slice of an (elliptical) cylinder sitting just inside the fur tips,
// so the outermost strands fall over its edge and it reads as printed on the fur.
function logoPatch(tex: THREE.Texture, centerAngle: number, height: number, radius: number): THREE.Mesh {
  const arc = height / radius;
  const geo = new THREE.CylinderGeometry(radius, radius, height, 40, 1, true, centerAngle - arc / 2, arc);
  geo.scale(1, 1, 0.88);
  const mesh = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ map: tex, transparent: true, depthWrite: false, roughness: 0.9 }));
  mesh.renderOrder = 2;
  return mesh;
}

export interface Creature {
  root: THREE.Group; // swivels on the stool
  face: THREE.Group; // tilts for typing / thinking
  mouth: THREE.Mesh; // flipped / flattened for the mood of the outcome
  rightArm: THREE.Group;
  leftArm: THREE.Group;
}

// Built seated, facing +z, with the seat at y = 0.55.
export function buildCreature(label: string): Creature {
  const brand = brandFor(label);
  const root = new THREE.Group();

  // one bean-shaped mass for head and body, like the reference: wider at the hips, domed on top
  const profile: THREE.Vector2[] = [];
  for (let i = 0; i <= 24; i++) {
    const t = i / 24;
    const y = t * 1.2;
    const dome = t > 0.62 ? Math.sqrt(Math.max(0, 1 - ((t - 0.62) / 0.38) ** 2)) : 1;
    const hips = 0.44 - 0.06 * Math.sin(t * Math.PI * 0.9);
    const bottom = t < 0.08 ? Math.sqrt(t / 0.08) : 1;
    profile.push(new THREE.Vector2(Math.max(0.001, hips * dome * bottom), y));
  }
  const bodyGeo = new THREE.LatheGeometry(profile, 48);
  bodyGeo.scale(1, 1, 0.88);
  bodyGeo.computeVertexNormals();
  const body = furry(bodyGeo);
  body.position.y = 0.56;
  root.add(body);

  // the face plate sits just outside the fur so the shells never cover it
  const face = new THREE.Group();
  face.position.set(0, 1.42, 0);
  const plate = new THREE.Mesh(new THREE.SphereGeometry(0.25, 40, 32), new THREE.MeshStandardMaterial({ color: "#f4e2c8", roughness: 0.55 }));
  plate.scale.set(1.12, 0.9, 0.42);
  plate.position.set(0, 0, 0.35);
  face.add(plate);
  const eyeMat = new THREE.MeshStandardMaterial({ color: "#111111", roughness: 0.15 });
  for (const x of [-0.095, 0.095]) {
    const eye = new THREE.Mesh(new THREE.SphereGeometry(0.03, 20, 20), eyeMat);
    eye.position.set(x, 0.035, 0.45);
    face.add(eye);
    const glint = new THREE.Mesh(new THREE.SphereGeometry(0.008, 8, 8), new THREE.MeshBasicMaterial({ color: "#ffffff" }));
    glint.position.set(x + 0.01, 0.048, 0.475);
    face.add(glint);
    const blush = new THREE.Mesh(new THREE.CircleGeometry(0.045, 24), new THREE.MeshBasicMaterial({ color: "#f19a9a", transparent: true, opacity: 0.45 }));
    blush.position.set(x * 1.55, -0.04, 0.448);
    blush.rotation.y = x > 0 ? 0.35 : -0.35;
    face.add(blush);
  }
  const smile = new THREE.Mesh(new THREE.TorusGeometry(0.035, 0.007, 8, 24, Math.PI), eyeMat);
  smile.rotation.z = Math.PI;
  smile.position.set(0, -0.025, 0.457);
  face.add(smile);

  // headset: band over the dome, padded cups on the sides, a mic boom curving to the mouth
  const shellMat = new THREE.MeshStandardMaterial({ color: "#2b2622", roughness: 0.35, metalness: 0.1 });
  const padMat = new THREE.MeshStandardMaterial({ color: "#4a403a", roughness: 0.8 });
  const band = new THREE.Mesh(new THREE.TorusGeometry(0.47, 0.028, 12, 48, Math.PI), shellMat);
  band.position.set(0, 0.03, -0.02);
  band.castShadow = true;
  face.add(band);
  for (const side of [-1, 1]) {
    const cup = new THREE.Mesh(new THREE.CylinderGeometry(0.115, 0.115, 0.09, 32), shellMat);
    cup.rotation.z = Math.PI / 2;
    cup.position.set(0.47 * side, 0.03, -0.02);
    cup.castShadow = true;
    face.add(cup);
    const pad = new THREE.Mesh(new THREE.TorusGeometry(0.09, 0.03, 12, 32), padMat);
    pad.rotation.y = Math.PI / 2;
    pad.position.set(0.43 * side, 0.03, -0.02);
    face.add(pad);
  }
  const boom = new THREE.Mesh(
    new THREE.TubeGeometry(new THREE.CatmullRomCurve3([new THREE.Vector3(-0.5, 0.0, 0.02), new THREE.Vector3(-0.42, -0.1, 0.26), new THREE.Vector3(-0.16, -0.08, 0.46)]), 24, 0.012, 8),
    shellMat,
  );
  face.add(boom);
  const mic = new THREE.Mesh(new THREE.SphereGeometry(0.03, 16, 16), shellMat);
  mic.position.set(-0.15, -0.08, 0.465);
  face.add(mic);
  root.add(face);

  // the model's logo, big on the back (what the viewer sees over its shoulder) and smaller on the chest
  const tex = logoTexture(brand);
  const back = logoPatch(tex, Math.PI, 0.62, 0.445);
  back.position.y = 1.0;
  root.add(back);
  const chest = logoPatch(tex, 0, 0.34, 0.45);
  chest.position.y = 0.9;
  root.add(chest);

  const arm = (side: number): THREE.Group => {
    const g = new THREE.Group();
    g.position.set(0.41 * side, 1.08, 0.02);
    const geo = new THREE.CapsuleGeometry(0.1, 0.3, 8, 16);
    const limb = furry(geo, 0.035);
    limb.position.set(0, -0.12, 0.17);
    limb.rotation.x = 1.15;
    g.add(limb);
    root.add(g);
    return g;
  };
  const rightArm = arm(1);
  const leftArm = arm(-1);
  for (const side of [-1, 1]) {
    const leg = furry(new THREE.CapsuleGeometry(0.12, 0.18, 8, 16), 0.035);
    leg.position.set(0.17 * side, 0.6, 0.3);
    leg.rotation.x = Math.PI / 2;
    root.add(leg);
  }
  return { root, face, mouth: smile, rightArm, leftArm };
}
