import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import type { Model } from "@/lib/model";
import type { Frame } from "@/lib/timeline";
import { DECISION_AT, STEPS_END, phoenixDate, phoenixHour } from "@/lib/timeline";
import { buildCreature } from "./creature";

// A cut-away isometric diorama of the dispatch office: two walls, the dispatcher facing the viewer,
// the schedule whiteboard and a corkboard of lessons. Text lives in the React panels, not here.

const BLOCK_START_HOURS = [7, 9, 11, 13, 15, 17];
const PALETTE = {
  floor: "#d8c3a0",
  floorEdge: "#b79f7a",
  wall: "#f1ece3",
  wallEdge: "#d9d1c3",
  trim: "#c9bda9",
  desk: "#a8794f",
  deskLeg: "#3d4451",
  chair: "#2f3b4c",
  shirt: "#4f7cff",
  skin: "#e2b08a",
  hair: "#3a2a20",
  rug: "#5c7d8a",
  plant: "#4f9a6a",
  pot: "#d9895b",
  metal: "#9aa3ae",
};

function mat(color: string, roughness = 0.85): THREE.MeshStandardMaterial {
  return new THREE.MeshStandardMaterial({ color, roughness });
}

function box(w: number, h: number, d: number, color: string, roughness = 0.85): THREE.Mesh {
  const mesh = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), mat(color, roughness));
  mesh.castShadow = true;
  mesh.receiveShadow = true;
  return mesh;
}

function canvasTexture(w: number, h: number) {
  const canvas = document.createElement("canvas");
  canvas.width = w;
  canvas.height = h;
  const tex = new THREE.CanvasTexture(canvas);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.anisotropy = 8;
  return { canvas, ctx: canvas.getContext("2d")!, tex };
}

function skyColor(hour: number): THREE.Color {
  const night = new THREE.Color("#1d2a4a");
  const dawn = new THREE.Color("#ffc58a");
  const day = new THREE.Color("#a9dcf5");
  if (hour < 6 || hour > 20.5) return night;
  if (hour < 7.5) return night.clone().lerp(dawn, (hour - 6) / 1.5);
  if (hour < 9) return dawn.clone().lerp(day, (hour - 7.5) / 1.5);
  if (hour < 17.5) return day;
  if (hour < 19.2) return day.clone().lerp(dawn, (hour - 17.5) / 1.7);
  return dawn.clone().lerp(night, Math.min(1, (hour - 19.2) / 1.3));
}

function roundRect(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number): void {
  ctx.beginPath();
  ctx.roundRect(x, y, w, h, r);
}

export class OfficeScene {
  private renderer: THREE.WebGLRenderer;
  private scene = new THREE.Scene();
  private camera: THREE.OrthographicCamera;
  private controls: OrbitControls;
  private observer: ResizeObserver;
  private hemi: THREE.HemisphereLight;
  private sun: THREE.DirectionalLight;
  private lamp: THREE.PointLight;
  private windowMat: THREE.MeshBasicMaterial;
  private screenMat: THREE.MeshBasicMaterial;
  private phone!: THREE.Group;
  private handset!: THREE.Mesh;
  private phoneLight!: THREE.MeshBasicMaterial;
  private head!: THREE.Group;
  private body!: THREE.Group;
  private swivel = 0;
  private mouth!: THREE.Mesh;
  private rightArm!: THREE.Group;
  private leftArm!: THREE.Group;
  private thinkDots: THREE.Mesh[] = [];
  private hourHand!: THREE.Mesh;
  private minuteHand!: THREE.Mesh;
  private board = canvasTexture(1600, 830);
  private screen = canvasTexture(640, 360);
  private screenKey = "";
  private station = new THREE.Group();
  private baseCamera = new THREE.Vector3(6.5, 9, 14.5);
  private baseTarget = new THREE.Vector3(0, 1.4, 0);
  private notes = canvasTexture(1040, 740);
  private boardKey = "";
  private notesKey = "";

  constructor(private host: HTMLElement, private model: Model) {
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    this.renderer.setPixelRatio(Math.min(2, window.devicePixelRatio));
    this.renderer.setClearColor(0x000000, 0);
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.05;
    host.appendChild(this.renderer.domElement);

    this.camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.1, 100);
    this.camera.position.copy(this.baseCamera);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.target.copy(this.baseTarget);
    this.controls.enableDamping = true;
    this.controls.enablePan = false;
    this.controls.minAzimuthAngle = 0.15;
    this.controls.maxAzimuthAngle = 1.35;
    this.controls.minPolarAngle = 0.7;
    this.controls.maxPolarAngle = 1.15;
    this.controls.minZoom = 0.7;
    this.controls.maxZoom = 2.5;

    this.hemi = new THREE.HemisphereLight("#f4f7ff", "#8a7358", 1.4);
    this.scene.add(this.hemi);
    this.sun = new THREE.DirectionalLight("#fff4e0", 2.4);
    this.sun.position.set(-6, 10, 6);
    this.sun.castShadow = true;
    this.sun.shadow.mapSize.set(2048, 2048);
    this.sun.shadow.radius = 4;
    this.sun.shadow.bias = -0.0005;
    Object.assign(this.sun.shadow.camera, { left: -8, right: 8, top: 8, bottom: -8 });
    this.scene.add(this.sun);
    this.lamp = new THREE.PointLight("#ffcf8a", 0, 7, 1.5);
    this.lamp.position.set(-1.25, 1.95, -0.2);
    this.scene.add(this.lamp);

    this.windowMat = new THREE.MeshBasicMaterial({ color: "#a9dcf5" });
    this.screenMat = new THREE.MeshBasicMaterial({ map: this.screen.tex, toneMapped: false });
    this.buildRoom();
    this.buildDesk();
    this.buildPerson();
    // The workstation is modelled facing +z; turn it so the dispatcher faces the back wall and the whiteboard,
    // with the monitor between them and the viewer looking over their shoulder.
    this.station.rotation.y = Math.PI;
    this.station.position.set(0.7, 0, 0.35);
    this.scene.add(this.station);
    this.lamp.position.copy(this.station.localToWorld(new THREE.Vector3(-1.25, 1.95, 0.3)));
    this.buildDecor();

    this.observer = new ResizeObserver(() => this.resize());
    this.observer.observe(host);
    this.resize();
  }

  private buildRoom(): void {
    const floor = box(10, 0.35, 8, PALETTE.floor);
    floor.position.set(0, -0.175, 0);
    this.scene.add(floor);
    for (let i = -4; i <= 4; i++) {
      const plank = new THREE.Mesh(new THREE.BoxGeometry(0.02, 0.002, 8), mat(PALETTE.floorEdge));
      plank.position.set(i * 1.1, 0.001, 0);
      this.scene.add(plank);
    }
    const rug = new THREE.Mesh(new THREE.CylinderGeometry(2.3, 2.3, 0.02, 48), mat(PALETTE.rug, 1));
    rug.position.set(0.6, 0.01, 0.6);
    rug.receiveShadow = true;
    this.scene.add(rug);

    const back = box(10, 3.8, 0.3, PALETTE.wall, 0.95);
    back.position.set(0, 1.9, -4.15);
    this.scene.add(back);
    const left = box(0.3, 3.8, 8.3, PALETTE.wall, 0.95);
    left.position.set(-5.15, 1.9, -0.15);
    this.scene.add(left);
    const capBack = box(10.3, 0.08, 0.32, PALETTE.wallEdge);
    capBack.position.set(-0.15, 3.84, -4.15);
    this.scene.add(capBack);
    const capLeft = box(0.32, 0.08, 8.3, PALETTE.wallEdge);
    capLeft.position.set(-5.15, 3.84, -0.15);
    this.scene.add(capLeft);
    const trimBack = box(10, 0.18, 0.06, PALETTE.trim);
    trimBack.position.set(0, 0.09, -3.98);
    this.scene.add(trimBack);
    const trimLeft = box(0.06, 0.18, 8, PALETTE.trim);
    trimLeft.position.set(-4.98, 0.09, 0);
    this.scene.add(trimLeft);

    // window with a small skyline
    const sky = new THREE.Mesh(new THREE.PlaneGeometry(2.0, 1.5), this.windowMat);
    sky.position.set(-3.35, 2.15, -3.99);
    this.scene.add(sky);
    for (let i = 0; i < 6; i++) {
      const h = 0.2 + ((i * 37) % 7) / 12;
      const b = new THREE.Mesh(new THREE.PlaneGeometry(0.28, h), new THREE.MeshBasicMaterial({ color: "#6f86a6", transparent: true, opacity: 0.45 }));
      b.position.set(-4.1 + i * 0.3, 1.4 + h / 2, -3.985);
      this.scene.add(b);
    }
    const frame = mat("#ffffff", 0.6);
    for (const [w, h, x, y] of [[2.2, 0.1, -3.35, 2.95], [2.2, 0.14, -3.35, 1.36], [0.1, 1.7, -4.4, 2.15], [0.1, 1.7, -2.3, 2.15], [0.06, 1.5, -3.35, 2.15]] as const) {
      const bar = new THREE.Mesh(new THREE.BoxGeometry(w, h, 0.1), frame);
      bar.position.set(x, y, -3.96);
      bar.castShadow = true;
      this.scene.add(bar);
    }

    // schedule whiteboard
    const boardFrame = box(5.5, 2.9, 0.06, "#c7ccd3", 0.4);
    boardFrame.position.set(1.85, 2.1, -3.97);
    this.scene.add(boardFrame);
    const board = new THREE.Mesh(new THREE.PlaneGeometry(5.36, 2.78), new THREE.MeshBasicMaterial({ map: this.board.tex, toneMapped: false }));
    board.position.set(1.85, 2.1, -3.935);
    this.scene.add(board);
    const tray = box(1.8, 0.05, 0.12, "#9aa3ae");
    tray.position.set(1.85, 0.62, -3.9);
    this.scene.add(tray);

    // corkboard of lessons on the left wall
    const corkFrame = box(0.06, 1.95, 2.75, "#8a6a48");
    corkFrame.position.set(-4.97, 2.2, -0.9);
    this.scene.add(corkFrame);
    const cork = new THREE.Mesh(new THREE.PlaneGeometry(2.6, 1.85), new THREE.MeshBasicMaterial({ map: this.notes.tex, toneMapped: false }));
    cork.rotation.y = Math.PI / 2;
    cork.position.set(-4.935, 2.2, -0.9);
    this.scene.add(cork);

    // wall clock between window and whiteboard
    const face = new THREE.Mesh(new THREE.CylinderGeometry(0.34, 0.34, 0.05, 40), mat("#ffffff", 0.4));
    face.rotation.x = Math.PI / 2;
    face.position.set(-3.35, 3.4, -3.97);
    this.scene.add(face);
    const rim = new THREE.Mesh(new THREE.TorusGeometry(0.34, 0.035, 10, 40), mat("#3d4451", 0.4));
    rim.position.set(-3.35, 3.4, -3.95);
    this.scene.add(rim);
    const hand = (len: number, width: number) => {
      const geo = new THREE.BoxGeometry(width, len, 0.01);
      geo.translate(0, len / 2, 0);
      const m = new THREE.Mesh(geo, mat("#1f2937"));
      m.position.set(-3.35, 3.4, -3.935);
      this.scene.add(m);
      return m;
    };
    this.hourHand = hand(0.17, 0.03);
    this.minuteHand = hand(0.26, 0.018);
  }

  private buildDesk(): void {
    const top = box(3.0, 0.1, 1.35, PALETTE.desk, 0.55);
    top.position.set(0, 0.82, 0.15);
    this.station.add(top);
    for (const [x, z] of [[-1.38, -0.4], [1.38, -0.4], [-1.38, 0.7], [1.38, 0.7]]) {
      const leg = box(0.08, 0.8, 0.08, PALETTE.deskLeg);
      leg.position.set(x, 0.4, z);
      this.station.add(leg);
    }
    const modesty = box(2.7, 0.45, 0.04, PALETTE.desk, 0.6);
    modesty.position.set(0, 0.55, 0.78);
    this.station.add(modesty);

    // monitor sits to the dispatcher's side so the viewer can see their face; its glow shows a live call
    const monitor = new THREE.Group();
    const stand = box(0.08, 0.3, 0.08, "#2b3340");
    stand.position.set(0, 0.17, 0);
    monitor.add(stand);
    const panel = box(1.2, 0.72, 0.06, "#20262f", 0.35);
    panel.position.set(0, 0.65, 0.03);
    monitor.add(panel);
    const screen = new THREE.Mesh(new THREE.PlaneGeometry(1.1, 0.62), this.screenMat);
    screen.rotation.y = Math.PI;
    screen.position.set(0, 0.65, -0.003);
    monitor.add(screen);
    monitor.position.set(-0.15, 0.85, 0.42);
    this.station.add(monitor);
    const keyboard = box(0.75, 0.03, 0.24, "#e5e7eb", 0.5);
    keyboard.position.set(0.15, 0.885, -0.05);
    this.station.add(keyboard);
    const mouse = box(0.08, 0.03, 0.13, "#e5e7eb", 0.5);
    mouse.position.set(0.7, 0.885, -0.05);
    this.station.add(mouse);

    const mug = new THREE.Mesh(new THREE.CylinderGeometry(0.07, 0.06, 0.15, 20), mat("#ef6f5a", 0.5));
    mug.position.set(-1.0, 0.95, 0.35);
    mug.castShadow = true;
    this.station.add(mug);
    for (let i = 0; i < 4; i++) {
      const sheet = box(0.42, 0.012, 0.3, "#ffffff", 0.9);
      sheet.position.set(-0.75, 0.88 + i * 0.013, -0.05);
      sheet.rotation.y = (i % 2 ? 1 : -1) * 0.06 * i;
      this.station.add(sheet);
    }

    // desk lamp
    const lampBase = box(0.2, 0.04, 0.2, "#2b3340");
    lampBase.position.set(-1.25, 0.89, 0.45);
    this.station.add(lampBase);
    const arm = box(0.03, 0.85, 0.03, "#2b3340");
    arm.position.set(-1.25, 1.3, 0.32);
    arm.rotation.x = -0.35;
    this.station.add(arm);
    const shade = new THREE.Mesh(new THREE.ConeGeometry(0.17, 0.22, 24, 1, true), new THREE.MeshStandardMaterial({ color: "#f2c14e", side: THREE.DoubleSide }));
    shade.position.set(-1.25, 1.72, 0.12);
    shade.rotation.x = -0.9;
    shade.castShadow = true;
    this.station.add(shade);

    // phone
    this.phone = new THREE.Group();
    this.phone.add(box(0.34, 0.08, 0.26, "#2b3340", 0.4));
    this.handset = box(0.36, 0.06, 0.09, "#3b4554", 0.4);
    this.handset.position.set(0, 0.07, 0.06);
    this.phone.add(this.handset);
    this.phoneLight = new THREE.MeshBasicMaterial({ color: "#3f3f46" });
    const light = new THREE.Mesh(new THREE.SphereGeometry(0.03, 12, 12), this.phoneLight);
    light.position.set(0.12, 0.05, -0.1);
    this.phone.add(light);
    this.phone.position.set(1.05, 0.91, 0.1);
    this.phone.rotation.y = 0.35;
    this.station.add(this.phone);

    const seat = new THREE.Mesh(new THREE.CylinderGeometry(0.42, 0.42, 0.1, 32), mat(PALETTE.chair, 0.7));
    seat.castShadow = true;
    seat.position.set(0.15, 0.5, -0.85);
    this.station.add(seat);
    const post = box(0.07, 0.45, 0.07, "#555c66");
    post.position.set(0.15, 0.23, -0.85);
    this.station.add(post);
  }

  private buildPerson(): void {
    const creature = buildCreature(this.model.replay.label);
    this.body = creature.root;
    this.head = creature.face;
    this.mouth = creature.mouth;
    this.rightArm = creature.rightArm;
    this.leftArm = creature.leftArm;
    for (let i = 0; i < 3; i++) {
      const dot = new THREE.Mesh(new THREE.SphereGeometry(0.045 + i * 0.025, 16, 16), new THREE.MeshBasicMaterial({ color: "#ffffff", transparent: true, opacity: 0 }));
      dot.position.set(0.32 + i * 0.17, 1.9 + i * 0.17, 0);
      this.body.add(dot);
      this.thinkDots.push(dot);
    }
    const person = new THREE.Group();
    person.add(this.body);
    person.position.set(0.15, -0.12, -0.95);
    person.scale.setScalar(1.3);
    this.station.add(person);
  }

  private buildDecor(): void {
    const cabinet = box(0.9, 1.3, 0.7, "#8e99a6", 0.5);
    cabinet.position.set(-4.4, 0.65, -3.4);
    this.scene.add(cabinet);
    for (const y of [0.35, 0.75, 1.15]) {
      const handle = box(0.3, 0.04, 0.03, "#5b6470");
      handle.position.set(-4.4, y, -3.04);
      this.scene.add(handle);
    }
    const plant = (x: number, z: number, scale: number) => {
      const pot = new THREE.Mesh(new THREE.CylinderGeometry(0.28 * scale, 0.22 * scale, 0.5 * scale, 24), mat(PALETTE.pot, 0.7));
      pot.position.set(x, 0.25 * scale, z);
      pot.castShadow = true;
      this.scene.add(pot);
      for (let i = 0; i < 9; i++) {
        const leaf = new THREE.Mesh(new THREE.SphereGeometry(0.22 * scale, 12, 12), mat(i % 2 ? PALETTE.plant : "#3f845a", 0.8));
        leaf.scale.set(0.6, 1.4, 0.6);
        leaf.position.set(x + Math.cos(i * 0.7) * 0.18 * scale, (0.75 + (i % 3) * 0.15) * scale, z + Math.sin(i * 0.7) * 0.18 * scale);
        leaf.rotation.set(Math.sin(i * 2) * 0.5, 0, Math.cos(i * 3) * 0.5);
        leaf.castShadow = true;
        this.scene.add(leaf);
      }
    };
    plant(4.45, -1.6, 1.2);
    plant(-4.4, 3.2, 1);
    const cooler = box(0.5, 1.1, 0.5, "#e8edf2", 0.5);
    cooler.position.set(4.4, 0.55, 0.4);
    this.scene.add(cooler);
    const bottle = new THREE.Mesh(new THREE.CylinderGeometry(0.2, 0.2, 0.45, 24), new THREE.MeshStandardMaterial({ color: "#7cc4f0", transparent: true, opacity: 0.75, roughness: 0.2 }));
    bottle.position.set(4.4, 1.33, 0.4);
    this.scene.add(bottle);
  }

  resize(): void {
    const w = this.host.clientWidth;
    const h = this.host.clientHeight;
    if (!w || !h) return;
    this.renderer.setSize(w, h);
    const halfH = 5;
    Object.assign(this.camera, { left: -halfH * (w / h), right: halfH * (w / h), top: halfH, bottom: -halfH });
    this.fit();
  }

  // Frame the whole room: project its corners, re-centre on them and zoom so they fit with a margin.
  private fit(): void {
    const cam = this.camera;
    cam.position.copy(this.baseCamera);
    this.controls.target.copy(this.baseTarget);
    cam.zoom = 1;
    cam.lookAt(this.baseTarget);
    cam.updateProjectionMatrix();
    cam.updateMatrixWorld();
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (const x of [-5.3, 5]) for (const y of [-0.35, 3.9]) for (const z of [-4.3, 4]) {
      const v = new THREE.Vector3(x, y, z).project(cam);
      minX = Math.min(minX, v.x); maxX = Math.max(maxX, v.x); minY = Math.min(minY, v.y); maxY = Math.max(maxY, v.y);
    }
    const right = new THREE.Vector3().setFromMatrixColumn(cam.matrixWorld, 0);
    const up = new THREE.Vector3().setFromMatrixColumn(cam.matrixWorld, 1);
    const shift = right
      .multiplyScalar(((minX + maxX) / 2) * ((cam.right - cam.left) / 2))
      .add(up.multiplyScalar(((minY + maxY) / 2) * ((cam.top - cam.bottom) / 2)));
    cam.position.add(shift);
    this.controls.target.add(shift);
    cam.zoom = 0.92 / Math.max((maxX - minX) / 2, (maxY - minY) / 2);
    cam.updateProjectionMatrix();
    this.controls.update();
  }

  update(frame: Frame, lastTurnId: number, wall: number): void {
    const hour = phoenixHour(new Date(frame.simMs));
    const daylight = Math.max(0, Math.min(1, hour < 12 ? (hour - 6) / 1.5 : (20 - hour) / 1.5));
    this.windowMat.color.copy(skyColor(hour));
    this.sun.intensity = 0.25 + 2.2 * daylight;
    this.sun.color.copy(skyColor(hour)).lerp(new THREE.Color("#fff4e0"), 0.7);
    this.hemi.intensity = 0.55 + 0.85 * daylight;
    this.lamp.intensity = 2 + 10 * (1 - daylight);

    const minutes = (hour % 12) * 60;
    this.minuteHand.rotation.z = -((minutes % 60) / 60) * Math.PI * 2;
    this.hourHand.rotation.z = -(minutes / 720) * Math.PI * 2;

    const turn = frame.turn;
    const p = frame.turnProgress;
    const step = turn?.steps.length ? turn.steps[Math.min(turn.steps.length - 1, Math.floor((Math.min(p, STEPS_END) / STEPS_END) * turn.steps.length))] : null;
    const onCall = !!turn && (turn.kind === "call" || turn.kind === "reminder");
    const ringing = onCall && p < 0.08;
    this.phone.position.y = 0.91 + (ringing ? Math.abs(Math.sin(wall * 40)) * 0.025 : 0);
    this.phoneLight.color.set(ringing ? (Math.sin(wall * 18) > 0 ? "#ef4444" : "#7f1d1d") : onCall ? "#22c55e" : "#3f3f46");
    this.handset.visible = !(onCall && p >= 0.08);

    const typing = step?.type === "tool";
    const thinking = step?.type === "think";
    // When a call is decided the creature swivels to the viewer and reacts to the outcome:
    // booked waves and smiles, a cancellation gets a calm nod, a rejection a small head shake, a lost call a slump.
    const outcome = turn?.decision && p >= DECISION_AT ? turn.decision.type : null;
    const reacting = outcome === "booked" || outcome === "rejected" || outcome === "lost" || outcome === "cancel_ack";
    this.swivel += ((reacting ? Math.PI : 0) - this.swivel) * 0.08;
    this.body.rotation.y = this.swivel;
    this.body.position.y = outcome === "lost" ? -0.04 : Math.sin(wall * 1.6) * 0.008;

    let rightArm = typing ? Math.sin(wall * 14) * 0.07 : ringing ? 0.6 : thinking ? 0.7 : 0;
    let leftArm = typing ? Math.sin(wall * 14 + 1.6) * 0.07 : 0;
    let headYaw = step?.memory_entry != null ? -0.35 : thinking ? Math.sin(wall * 0.8) * 0.12 : 0;
    let headPitch = typing ? 0.12 : thinking ? -0.1 : 0;
    let mood: "happy" | "neutral" | "sad" = "happy";
    if (outcome === "booked") {
      rightArm = -0.9 + Math.sin(wall * 9) * 0.25;
    } else if (outcome === "cancel_ack") {
      headPitch = Math.max(0, Math.sin(wall * 4)) * 0.18;
      mood = "neutral";
    } else if (outcome === "rejected") {
      headYaw = Math.sin(wall * 7) * 0.22;
      mood = "neutral";
    } else if (outcome === "lost") {
      rightArm = leftArm = 0.35;
      headPitch = 0.22;
      mood = "sad";
    }
    this.rightArm.rotation.x = rightArm;
    this.leftArm.rotation.x = leftArm;
    this.head.rotation.y = headYaw;
    this.head.rotation.x = headPitch;
    this.mouth.rotation.z = mood === "sad" ? 0 : Math.PI;
    this.mouth.scale.set(1, mood === "neutral" ? 0.12 : 1, 1);
    this.mouth.position.y = mood === "sad" ? -0.06 : -0.025;
    this.thinkDots.forEach((dot, i) => {
      (dot.material as THREE.MeshBasicMaterial).opacity = thinking ? 0.25 + 0.65 * Math.max(0, Math.sin(wall * 4 - i * 0.9)) : 0;
    });

    this.drawScreen(frame);
    this.drawBoard(frame);
    this.drawNotes(lastTurnId, frame);
    this.controls.update();
    this.renderer.render(this.scene, this.camera);
  }

  private drawScreen(frame: Frame): void {
    const turn = frame.turn;
    const key = String(turn?.id ?? "idle");
    if (key === this.screenKey) return;
    this.screenKey = key;
    const { ctx, canvas, tex } = this.screen;
    const W = canvas.width;
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, W, canvas.height);
    ctx.fillStyle = "#0000f8";
    ctx.fillRect(0, 0, W, 54);
    ctx.fillStyle = "#ffffff";
    ctx.font = "500 26px 'GT America Standard', system-ui, sans-serif";
    ctx.fillText("Sunridge Dispatch", 24, 36);
    ctx.fillStyle = "#1c1b19";
    if (!turn) {
      ctx.fillStyle = "#837d73";
      ctx.font = "400 30px 'GT America Standard', system-ui, sans-serif";
      ctx.fillText("No active call", 24, 120);
    } else {
      const f = turn.fields;
      const isCall = turn.kind === "call" || turn.kind === "reminder";
      ctx.fillStyle = "#6f6b63";
      ctx.font = "400 24px 'GT America Standard', system-ui, sans-serif";
      ctx.fillText(isCall ? `Incoming call #${f["call_id"] ?? ""}` : turn.kind === "opening" ? "Morning planning" : "End of day", 24, 100);
      ctx.fillStyle = "#1c1b19";
      ctx.font = "500 40px 'GT America Standard', system-ui, sans-serif";
      ctx.fillText((isCall ? f["caller"] ?? "Caller" : "Reviewing the books").slice(0, 24), 24, 152);
      ctx.font = "400 26px 'GT America Standard', system-ui, sans-serif";
      ctx.fillStyle = "#6f6b63";
      [f["requested_service"], f["address"]].filter(Boolean).forEach((line, i) => ctx.fillText(String(line).slice(0, 38), 24, 200 + i * 38));
      if ((f["urgent"] ?? "").toLowerCase().startsWith("y")) {
        ctx.fillStyle = "#c2352b";
        ctx.font = "500 26px 'GT America Standard', system-ui, sans-serif";
        ctx.fillText("URGENT", 24, 300);
      }
    }
    tex.needsUpdate = true;
  }

  private drawBoard(frame: Frame): void {
    const model = this.model;
    const day = phoenixDate(frame.simMs);
    const pending = frame.turn && frame.turnProgress < DECISION_AT ? frame.turn.sim_ms : null;
    const key = `${day}-${Math.floor(frame.simMs / 600000)}-${pending}`;
    if (key === this.boardKey) return;
    this.boardKey = key;
    const { ctx, canvas, tex } = this.board;
    const W = canvas.width;
    const H = canvas.height;
    ctx.fillStyle = "#fbfcfd";
    ctx.fillRect(0, 0, W, H);
    ctx.fillStyle = "#1f2937";
    ctx.font = "700 46px Inter, system-ui, sans-serif";
    const title = new Date(`${day}T12:00:00Z`).toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric", timeZone: "UTC" });
    ctx.fillText(title, 40, 72);
    const techs = [...model.techs.values()];
    if (!techs.length) {
      tex.needsUpdate = true;
      return;
    }
    const left = 250;
    const top = 130;
    const colW = (W - left - 30) / BLOCK_START_HOURS.length;
    const rowH = (H - top - 24) / techs.length;
    ctx.font = "600 28px Inter, system-ui, sans-serif";
    ctx.fillStyle = "#6b7280";
    BLOCK_START_HOURS.forEach((h, i) => ctx.fillText(`${h}:00`, left + i * colW + 14, top - 18));
    const shifts = (model.replay.world?.shifts ?? []).filter((s) => s.service_date === day);
    techs.forEach((tech, r) => {
      const y = top + r * rowH;
      ctx.fillStyle = tech.color;
      ctx.beginPath();
      ctx.arc(52, y + rowH / 2, 14, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = "#111827";
      ctx.font = "700 32px Inter, system-ui, sans-serif";
      ctx.fillText(tech.name.split(" ")[0], 78, y + rowH / 2 + 11);
      BLOCK_START_HOURS.forEach((h, i) => {
        const on = shifts.some((s) => s.tech === tech.id && phoenixHour(new Date(s.t0)) <= h && phoenixHour(new Date(s.t1 - 1)) >= h);
        ctx.fillStyle = on ? "#f1f4f8" : "#e3e7ed";
        roundRect(ctx, left + i * colW + 5, y + 7, colW - 10, rowH - 14, 12);
        ctx.fill();
        if (!on) {
          ctx.strokeStyle = "#cfd5dd";
          ctx.lineWidth = 2;
          ctx.beginPath();
          ctx.moveTo(left + i * colW + 20, y + rowH - 20);
          ctx.lineTo(left + (i + 1) * colW - 20, y + 20);
          ctx.stroke();
        }
      });
    });
    for (const visit of model.visits) {
      const state = model.visitState(visit, frame.simMs);
      if (!state || !visit.tech || phoenixDate(visit.appt.scheduled_start) !== day) continue;
      if (pending != null && visit.appt.created_at >= pending) continue; // booked on this call: wait for the decision
      const r = techs.indexOf(visit.tech);
      const h0 = phoenixHour(new Date(visit.appt.scheduled_start));
      const h1 = phoenixHour(new Date(visit.appt.scheduled_end));
      const c0 = BLOCK_START_HOURS.findIndex((h) => h >= h0 - 0.01);
      if (c0 < 0) continue;
      const span = Math.max(1, Math.round((h1 - h0) / 2));
      const x = left + c0 * colW + 5;
      const y = top + r * rowH + 7;
      const w = colW * span - 10;
      const h = rowH - 14;
      ctx.globalAlpha = state === "cancelled" ? 0.3 : 1;
      ctx.fillStyle = visit.tech.color;
      roundRect(ctx, x, y, w, h, 12);
      ctx.fill();
      ctx.fillStyle = "#ffffff";
      ctx.font = "700 26px Inter, system-ui, sans-serif";
      ctx.fillText(visit.job.job_type.replace("hvac_", "HVAC ").replace(/_/g, " ").slice(0, 16), x + 14, y + 38);
      ctx.font = "500 22px Inter, system-ui, sans-serif";
      const label = { completed: "✓ done", working: "● on site", enroute: "→ driving", failed: "✗ failed", cancelled: "cancelled", scheduled: visit.job.city }[state];
      ctx.fillText(label.slice(0, 18), x + 14, y + 70);
      ctx.globalAlpha = 1;
    }
    tex.needsUpdate = true;
  }

  private drawNotes(lastTurnId: number, frame: Frame): void {
    const memory = this.model.replay.memory;
    const progress = frame.turn ? frame.turnProgress : 1;
    let upto = -1;
    for (let i = 0; i < memory.length; i++) {
      if (memory[i].turn < lastTurnId || (memory[i].turn === lastTurnId && progress >= 0.75)) upto = i;
      else break;
    }
    const key = String(upto);
    if (key === this.notesKey) return;
    this.notesKey = key;
    const { ctx, canvas, tex } = this.notes;
    ctx.fillStyle = "#c9a46e";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    for (let i = 0; i < 700; i++) {
      ctx.fillStyle = `rgba(90,60,30,${0.06 + ((i * 7) % 10) / 120})`;
      ctx.fillRect((i * 97) % canvas.width, (i * 53) % canvas.height, 3, 3);
    }
    const lessons: string[] = [];
    for (let i = upto; i >= 0 && lessons.length < 6; i--) {
      const line = memory[i].added.find((l) => l.replace(/[#*\-\s|=>]/g, "").length > 20 && !l.startsWith("|"));
      if (line) lessons.push(line.replace(/^[#\-*=>\s]+/, "").replace(/\*\*/g, ""));
    }
    const colors = ["#fff3a3", "#c8f2d4", "#ffd0e4", "#cbe8ff", "#ffdcb8", "#e6d8ff"];
    lessons.forEach((text, i) => {
      const w = 300;
      const h = 300;
      const x = 40 + (i % 3) * 335;
      const y = 40 + Math.floor(i / 3) * 345;
      ctx.save();
      ctx.translate(x + w / 2, y + h / 2);
      ctx.rotate((((i * 37) % 9) - 4) / 90);
      ctx.shadowColor = "rgba(0,0,0,.25)";
      ctx.shadowBlur = 12;
      ctx.shadowOffsetY = 6;
      ctx.fillStyle = colors[i % colors.length];
      ctx.fillRect(-w / 2, -h / 2, w, h);
      ctx.shadowColor = "transparent";
      ctx.fillStyle = "#e0453a";
      ctx.beginPath();
      ctx.arc(0, -h / 2 + 16, 10, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = "#2b2b2b";
      ctx.font = "500 25px Inter, system-ui, sans-serif";
      wrap(ctx, text, -w / 2 + 20, -h / 2 + 62, w - 40, 31, 8);
      ctx.restore();
    });
    tex.needsUpdate = true;
  }

  dispose(): void {
    this.observer.disconnect();
    this.controls.dispose();
    this.renderer.dispose();
    this.renderer.domElement.remove();
  }
}

function wrap(ctx: CanvasRenderingContext2D, text: string, x: number, y: number, width: number, lineH: number, maxLines: number): void {
  const words = text.split(/\s+/);
  let line = "";
  let n = 0;
  for (const word of words) {
    const test = line ? `${line} ${word}` : word;
    if (ctx.measureText(test).width > width && line) {
      ctx.fillText(n === maxLines - 1 ? `${line}…` : line, x, y + n * lineH);
      line = word;
      if (++n >= maxLines) return;
    } else line = test;
  }
  if (line && n < maxLines) ctx.fillText(line, x, y + n * lineH);
}
