import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { CSS2DObject, CSS2DRenderer } from "three/examples/jsm/renderers/CSS2DRenderer.js";
import type { Model, Visit, VisitState } from "@/lib/model";
import type { Frame } from "@/lib/timeline";
import { fmtMoney, phoenixDate, phoenixHour } from "@/lib/timeline";
import type { Call, Technician } from "@/lib/types";

interface ZoneFeature {
  properties: { code: string; name: string; zone_id: number };
  geometry: { coordinates: [number, number][][] };
}

const KM_PER_DEG = 111.32;
const MONEY_POP_MS = 75 * 60000;
const ZONE_COLORS = ["#ffffff", "#fbfaf6", "#f7f5f0", "#fdfcf9", "#f9f7f2", "#ffffff", "#f6f4ee", "#fcfbf7", "#f8f6f1", "#fefdfb"];
const STATE_COLORS: Record<VisitState, string> = {
  scheduled: "#837d73",
  enroute: "#5b6cff",
  working: "#e08a1e",
  completed: "#1f8a4c",
  failed: "#c2352b",
  cancelled: "#c9c4b9",
};

class Projection {
  constructor(private lat0: number, private lon0: number) {}
  x(lon: number): number {
    return (lon - this.lon0) * KM_PER_DEG * Math.cos((this.lat0 * Math.PI) / 180);
  }
  z(lat: number): number {
    return -(lat - this.lat0) * KM_PER_DEG;
  }
  v(lat: number, lon: number, y = 0): THREE.Vector3 {
    return new THREE.Vector3(this.x(lon), y, this.z(lat));
  }
}

interface Pin {
  visit: Visit;
  group: THREE.Group;
  pillar: THREE.Mesh;
  ring: THREE.Mesh;
  money: CSS2DObject;
  material: CSS2DObject | null;
}

interface Truck {
  tech: Technician;
  group: THREE.Group;
  label: CSS2DObject;
  labelEl: HTMLElement;
  trail: THREE.Line;
}

export class FieldScene {
  private renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  private labels = new CSS2DRenderer();
  private scene = new THREE.Scene();
  private camera = new THREE.PerspectiveCamera(45, 1, 0.1, 2000);
  private controls: OrbitControls;
  private host: HTMLElement;
  private proj: Projection;
  private pins: Pin[] = [];
  private trucks: Truck[] = [];
  private callMarks: { call: Call; mesh: THREE.Mesh }[] = [];
  private hemi: THREE.HemisphereLight;
  private sun: THREE.DirectionalLight;
  private raycaster = new THREE.Raycaster();
  private observer: ResizeObserver;

  constructor(
    host: HTMLElement,
    private model: Model,
    zones: { features: ZoneFeature[] },
    private onSelect: (visit: Visit | null) => void,
  ) {
    this.host = host;

    this.renderer.setPixelRatio(Math.min(2, window.devicePixelRatio));
    this.renderer.toneMapping = THREE.NoToneMapping;
    this.host.appendChild(this.renderer.domElement);
    this.labels.domElement.style.position = "absolute";
    this.labels.domElement.style.inset = "0";
    this.labels.domElement.style.pointerEvents = "none";
    this.host.appendChild(this.labels.domElement);

    const techs = [...model.techs.values()];
    const lat0 = techs.length ? techs.reduce((s, t) => s + t.lat, 0) / techs.length : 33.45;
    const lon0 = techs.length ? techs.reduce((s, t) => s + t.lon, 0) / techs.length : -112.07;
    this.proj = new Projection(lat0, lon0);

    this.renderer.setClearColor(0x000000, 0);
    this.hemi = new THREE.HemisphereLight("#ffffff", "#d9d1c3", 1.6);
    this.scene.add(this.hemi);
    this.sun = new THREE.DirectionalLight("#fff1d6", 1.4);
    this.sun.position.set(-40, 80, 30);
    this.scene.add(this.sun);

    this.camera.position.set(0, 70, 62);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.maxPolarAngle = Math.PI * 0.47;
    this.controls.minDistance = 8;
    this.controls.maxDistance = 220;

    this.buildGround(zones.features);
    if (model.hasWorld) {
      this.buildHomes(techs);
      this.buildPins();
      this.buildTrucks(techs);
      this.buildCalls();
    }
    this.renderer.domElement.addEventListener("click", (e) => this.pick(e));
    this.observer = new ResizeObserver(() => this.resize());
    this.observer.observe(this.host);
    this.resize();
  }

  private buildGround(features: ZoneFeature[]): void {
    // outside the service area: a slightly darker warm grey so the white zones read as "where we work"
    const ground = new THREE.Mesh(new THREE.PlaneGeometry(600, 600), new THREE.MeshBasicMaterial({ color: "#dcd6ca" }));
    ground.rotation.x = -Math.PI / 2;
    ground.position.y = -0.05;
    this.scene.add(ground);
    const grid = new THREE.GridHelper(400, 80, "#cfc8ba", "#d5cfc3");
    grid.position.y = -0.02;
    this.scene.add(grid);
    features.forEach((feature, i) => {
      const color = new THREE.Color(ZONE_COLORS[i % ZONE_COLORS.length]);
      let cx = 0;
      let cz = 0;
      let n = 0;
      for (const ring of feature.geometry.coordinates) {
        if (ring.length < 3) continue;
        const shape = new THREE.Shape(ring.map(([lon, lat]) => new THREE.Vector2(this.proj.x(lon), -this.proj.z(lat))));
        const geo = new THREE.ExtrudeGeometry(shape, { depth: 0.35, bevelEnabled: false });
        geo.rotateX(-Math.PI / 2);
        const mesh = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ color, roughness: 0.9, transparent: true, opacity: 0.92 }));
        this.scene.add(mesh);
        const edges = new THREE.LineSegments(new THREE.EdgesGeometry(geo, 30), new THREE.LineBasicMaterial({ color: "#c9c2b5" }));
        this.scene.add(edges);
        for (const [lon, lat] of ring) {
          cx += this.proj.x(lon);
          cz += this.proj.z(lat);
          n++;
        }
      }
      if (n) {
        const el = document.createElement("div");
        el.className = "map-label";
        el.textContent = feature.properties.name;
        const label = new CSS2DObject(el);
        label.position.set(cx / n, 0.5, cz / n);
        this.scene.add(label);
      }
    });
  }

  private buildHomes(techs: Technician[]): void {
    for (const tech of techs) {
      const g = new THREE.Group();
      const color = new THREE.Color(tech.color);
      const base = new THREE.Mesh(new THREE.BoxGeometry(1.2, 0.9, 1.2), new THREE.MeshStandardMaterial({ color: "#e2e8f0" }));
      base.position.y = 0.8;
      g.add(base);
      const roof = new THREE.Mesh(new THREE.ConeGeometry(1.05, 0.8, 4), new THREE.MeshStandardMaterial({ color }));
      roof.position.y = 1.65;
      roof.rotation.y = Math.PI / 4;
      g.add(roof);
      g.position.copy(this.proj.v(tech.lat, tech.lon, 0));
      this.scene.add(g);
    }
  }

  private buildPins(): void {
    for (const visit of this.model.visits) {
      const g = new THREE.Group();
      g.position.copy(this.proj.v(visit.job.lat, visit.job.lon, 0.35));
      const color = new THREE.Color(visit.tech?.color ?? "#94a3b8");
      const pillarGeo = new THREE.CylinderGeometry(0.42, 0.42, 1, 20);
      pillarGeo.translate(0, 0.5, 0);
      const pillar = new THREE.Mesh(pillarGeo, new THREE.MeshStandardMaterial({ color, transparent: true, opacity: 0.85, emissive: color, emissiveIntensity: 0.15 }));
      pillar.userData.visit = visit;
      g.add(pillar);
      const ring = new THREE.Mesh(new THREE.TorusGeometry(0.85, 0.08, 8, 32), new THREE.MeshBasicMaterial({ color }));
      ring.rotation.x = Math.PI / 2;
      ring.position.y = 0.05;
      g.add(ring);
      const moneyEl = document.createElement("div");
      moneyEl.className = "money";
      const money = new CSS2DObject(moneyEl);
      money.visible = false;
      g.add(money);
      g.visible = false;
      this.scene.add(g);
      this.pins.push({ visit, group: g, pillar, ring, money, material: null });
    }
  }

  private buildTrucks(techs: Technician[]): void {
    for (const tech of techs) {
      const g = new THREE.Group();
      const color = new THREE.Color(tech.color);
      const body = new THREE.Mesh(new THREE.BoxGeometry(1.6, 0.9, 0.9), new THREE.MeshStandardMaterial({ color: "#f1f5f9" }));
      body.position.set(-0.2, 0.75, 0);
      g.add(body);
      const cab = new THREE.Mesh(new THREE.BoxGeometry(0.7, 0.7, 0.86), new THREE.MeshStandardMaterial({ color }));
      cab.position.set(0.85, 0.65, 0);
      g.add(cab);
      const stripe = new THREE.Mesh(new THREE.BoxGeometry(1.62, 0.18, 0.92), new THREE.MeshStandardMaterial({ color, emissive: color, emissiveIntensity: 0.4 }));
      stripe.position.set(-0.2, 0.75, 0);
      g.add(stripe);
      for (const [x, z] of [[-0.6, 0.45], [-0.6, -0.45], [0.8, 0.45], [0.8, -0.45]]) {
        const wheel = new THREE.Mesh(new THREE.CylinderGeometry(0.22, 0.22, 0.12, 14), new THREE.MeshStandardMaterial({ color: "#111" }));
        wheel.rotation.x = Math.PI / 2;
        wheel.position.set(x, 0.25, z);
        g.add(wheel);
      }
      g.scale.setScalar(1.3);
      const el = document.createElement("div");
      el.className = "truck-label";
      el.style.background = tech.color;
      el.textContent = tech.name.split(" ")[0];
      const label = new CSS2DObject(el);
      label.position.set(0, 2.2, 0);
      g.add(label);
      this.scene.add(g);
      const trail = new THREE.Line(
        new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), new THREE.Vector3()]),
        new THREE.LineDashedMaterial({ color, dashSize: 0.8, gapSize: 0.5 }),
      );
      trail.visible = false;
      this.scene.add(trail);
      this.trucks.push({ tech, group: g, label, labelEl: el, trail });
    }
  }

  private buildCalls(): void {
    for (const call of this.model.replay.world!.calls) {
      if (call.call_kind !== "new_job" || call.created_job_id != null || call.lat == null || call.lon == null) continue;
      const color = call.terminal_reason === "customer_declined" ? "#f87171" : "#64748b";
      const mesh = new THREE.Mesh(new THREE.ConeGeometry(0.45, 1.2, 12), new THREE.MeshStandardMaterial({ color, transparent: true, opacity: 0.8 }));
      mesh.rotation.x = Math.PI;
      mesh.position.copy(this.proj.v(call.lat, call.lon, 1.0));
      mesh.visible = false;
      this.scene.add(mesh);
      this.callMarks.push({ call, mesh });
    }
  }

  resize(): void {
    const w = this.host.clientWidth;
    const h = this.host.clientHeight;
    if (!w || !h) return;
    this.renderer.setSize(w, h);
    this.labels.setSize(w, h);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
  }

  update(frame: Frame, wall: number): void {
    const sim = frame.simMs;
    const hour = phoenixHour(new Date(sim));
    const day = hour > 6.3 && hour < 19.3;
    this.hemi.intensity = day ? 2.3 : 1.3;
    this.sun.intensity = day ? 0.9 : 0.3;

    for (const pin of this.pins) this.updatePin(pin, sim, wall);
    for (const truck of this.trucks) this.updateTruck(truck, sim);
    for (const { call, mesh } of this.callMarks) {
      const age = sim - call.arrived_at;
      mesh.visible = age >= 0 && age < 24 * 3600000;
      mesh.position.y = 1.0 + Math.sin(wall * 2 + call.id) * 0.1;
    }
    this.controls.update();
    this.renderer.render(this.scene, this.camera);
    this.labels.render(this.scene, this.camera);
  }

  private updatePin(pin: Pin, sim: number, wall: number): void {
    const state = this.model.visitState(pin.visit, sim);
    const ageSinceEnd = pin.visit.appt.cancelled_at != null ? sim - pin.visit.appt.cancelled_at : 0;
    pin.group.visible = state != null && !(state === "cancelled" && ageSinceEnd > 6 * 3600000);
    if (!state || !pin.group.visible) {
      pin.money.visible = false; // CSS2D labels ignore their parent's visibility
      return;
    }
    const mat = pin.pillar.material as THREE.MeshStandardMaterial;
    const ringMat = pin.ring.material as THREE.MeshBasicMaterial;
    let height = 1.2;
    const pastDay = phoenixDate(pin.visit.appt.scheduled_start) < phoenixDate(sim);
    if (state === "completed") height = (pastDay ? 0.35 : 1) * (1.4 + Math.sqrt(Math.max(0, pin.visit.revenueCents) / 100) / 9);
    else if (state === "working") height = 2.4 + Math.sin(wall * 5) * 0.25;
    else if (state === "failed") height = 0.6;
    pin.pillar.scale.y = height;
    const color = state === "scheduled" || state === "enroute" ? pin.visit.tech?.color ?? STATE_COLORS.scheduled : STATE_COLORS[state];
    mat.color.set(color);
    mat.emissive.set(color);
    mat.emissiveIntensity = state === "working" ? 0.6 + Math.sin(wall * 6) * 0.3 : 0.12;
    mat.opacity = state === "scheduled" ? 0.4 : state === "cancelled" ? 0.2 : pastDay ? 0.3 : 0.9;
    pin.ring.visible = !pastDay;
    ringMat.color.set(pin.visit.tech?.color ?? "#94a3b8");
    pin.ring.scale.setScalar(state === "working" ? 1 + (wall % 1) * 0.6 : 1);

    const paid = pin.visit.paidAt;
    const showMoney = paid != null && sim >= paid && sim - paid < MONEY_POP_MS;
    pin.money.visible = showMoney;
    if (showMoney) {
      const f = (sim - paid!) / MONEY_POP_MS;
      const margin = pin.visit.revenueCents - pin.visit.materialCents;
      const el = pin.money.element;
      el.textContent = `${fmtMoney(pin.visit.revenueCents, true)}  (margin ${fmtMoney(margin, true)})`;
      el.style.opacity = String(1 - Math.max(0, f - 0.7) / 0.3);
      pin.money.position.set(0, height + 1.2 + f * 3, 0);
    }
  }

  private updateTruck(truck: Truck, sim: number): void {
    const pos = this.model.techPosition(truck.tech.id, sim);
    const p = this.proj.v(pos.lat, pos.lon, 0.35);
    truck.group.position.copy(p);
    const driving = pos.driving;
    if (driving) {
      const to = this.proj.v(driving.lat1, driving.lon1, 0.4);
      truck.group.rotation.y = Math.atan2(-(to.z - p.z), to.x - p.x);
      const from = this.proj.v(driving.lat0, driving.lon0, 0.4);
      (truck.trail.geometry as THREE.BufferGeometry).setFromPoints([from, to]);
      truck.trail.computeLineDistances();
      truck.trail.visible = true;
    } else truck.trail.visible = false;
    const status = this.model.techStatus(truck.tech.id, sim);
    truck.labelEl.textContent = `${truck.tech.name.split(" ")[0]} · ${status.label}`;
    truck.group.visible = true;
  }

  private pick(event: MouseEvent): void {
    const rect = this.renderer.domElement.getBoundingClientRect();
    const mouse = new THREE.Vector2(((event.clientX - rect.left) / rect.width) * 2 - 1, -((event.clientY - rect.top) / rect.height) * 2 + 1);
    this.raycaster.setFromCamera(mouse, this.camera);
    const hits = this.raycaster.intersectObjects(this.pins.filter((p) => p.group.visible).map((p) => p.pillar));
    this.onSelect((hits[0]?.object.userData.visit as Visit | undefined) ?? null);
  }

  dispose(): void {
    this.observer.disconnect();
    this.renderer.dispose();
    this.renderer.domElement.remove();
    this.labels.domElement.remove();
  }
}
